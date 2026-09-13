from __future__ import annotations

from collections import deque
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from threading import Lock, Thread
from time import monotonic, sleep
from typing import Protocol
from uuid import UUID

from onec_runtime.controller_worker import BarrierMode
from onec_runtime.errors import (
    CommandTimeout,
    ControllerUnavailable,
    InvalidMessageSequence,
    LeaseConflict,
    LeaseExpired,
    StaleLeaseEpoch,
    StaleRuntimeGeneration,
)
from onec_runtime.fault_injection import FaultPoint
from onec_runtime.lease import validate_lease_ttl
from onec_runtime.recovery_journal import RecoveryJournal
from onec_runtime.supervisor import (
    HEARTBEAT_WATCHDOG_S,
    JOURNAL_STREAM,
    WORKER_TERMINATION_GRACE_S,
    GenerationProcesses,
    RuntimeSupervisor,
)
from onec_runtime.supervisor_model import (
    GenerationHandle,
    GenerationLifecycle,
    GenerationRecord,
    LeaseHandle,
    SupervisedOperationHandle,
    TerminationCause,
)
from onec_runtime.supervisor_protocol import (
    ControlMessage,
    MessageKind,
    MessageSender,
)


DEBUG_READY_TIMEOUT_S = 10.0
READY_TIMEOUT_S = 90.0
LEASE_TTL_S = 2.0
SUPERVISOR_POLL_INTERVAL_S = 0.05


class StagedGenerationProcesses(GenerationProcesses, Protocol):
    def start_debug_server(self) -> int:
        raise NotImplementedError

    def start_controller(self, debug_port: int, run_dir: Path) -> None:
        raise NotImplementedError

    def start_debuggee(self, debug_port: int) -> None:
        raise NotImplementedError

    def send(self, message: ControlMessage) -> None:
        raise NotImplementedError

    def receive(self, timeout_s: float) -> ControlMessage:
        raise NotImplementedError


_TYPED_WORKER_ERRORS: dict[str, type[Exception]] = {
    error_type.__name__: error_type
    for error_type in (
        ControllerUnavailable,
        LeaseConflict,
        LeaseExpired,
        StaleLeaseEpoch,
        StaleRuntimeGeneration,
    )
}


class SupervisedRuntime:
    """High-level staged control plane for one supervised runtime generation."""

    def __init__(
        self,
        processes_factory: Callable[[int], StagedGenerationProcesses],
        *,
        journal: RecoveryJournal | None = None,
        heartbeat_timeout_s: float = HEARTBEAT_WATCHDOG_S,
        run_dir: Path | None = None,
        lease_ttl_s: float = LEASE_TTL_S,
    ) -> None:
        self._journal = journal or RecoveryJournal()
        self._run_dir = run_dir or Path.cwd()
        self._lease_ttl_s = validate_lease_ttl(lease_ttl_s)
        self._processes_factory = processes_factory
        self._processes: StagedGenerationProcesses | None = None
        self._sender: MessageSender | None = None
        self._pending: deque[ControlMessage] = deque()
        self._next_operation_id = 0
        self._termination_thread: Thread | None = None
        self._termination_error: BaseException | None = None
        self._termination_thread_lock = Lock()
        self._supervisor = RuntimeSupervisor(
            self._create_processes,
            journal=self._journal,
            heartbeat_timeout_s=heartbeat_timeout_s,
        )

    def start(self) -> GenerationHandle:
        try:
            handle = self._supervisor.begin_generation()
            self._sender = MessageSender(handle.generation_id)
            self._pending.clear()
            self._next_operation_id = 0
            processes = self._require_processes()
            debug_port = processes.start_debug_server()
            processes.start_controller(debug_port, self._run_dir)
            self._receive_startup(MessageKind.DEBUG_READY, DEBUG_READY_TIMEOUT_S)
            processes.start_debuggee(debug_port)
            self._send(MessageKind.DEBUGGEE_STARTED)
            self._receive_startup(MessageKind.READY, READY_TIMEOUT_S)
            self._supervisor.activate(handle)
            return handle
        except BaseException:
            try:
                self._terminate_failed_startup()
            except BaseException:
                pass
            raise

    def grant_lease(
        self,
        generation: GenerationHandle,
        owner_id: UUID,
        *,
        ttl_s: float | None = None,
    ) -> LeaseHandle:
        self._supervisor.require_active(generation)
        validated_ttl_s = validate_lease_ttl(
            self._lease_ttl_s if ttl_s is None else ttl_s
        )
        self._send(
            MessageKind.GRANT_LEASE,
            owner_id=str(owner_id),
            ttl_s=validated_ttl_s,
        )
        response = self._wait_for_response(MessageKind.LEASE_GRANTED)
        handle = LeaseHandle(
            generation_id=generation.generation_id,
            owner_id=owner_id,
            lease_epoch=self._positive_int(
                response.payload["lease_epoch"],
                "lease_epoch",
            ),
        )
        self._record_owner_evidence("lease_granted", handle)
        return handle

    def renew_lease(
        self,
        lease: LeaseHandle,
        *,
        ttl_s: float | None = None,
    ) -> LeaseHandle:
        self._supervisor.require_active(GenerationHandle(lease.generation_id))
        validated_ttl_s = validate_lease_ttl(
            self._lease_ttl_s if ttl_s is None else ttl_s
        )
        self._send(
            MessageKind.RENEW_LEASE,
            owner_id=str(lease.owner_id),
            lease_epoch=lease.lease_epoch,
            ttl_s=validated_ttl_s,
        )
        response = self._wait_for_response(MessageKind.LEASE_GRANTED)
        renewed = LeaseHandle(
            generation_id=lease.generation_id,
            owner_id=lease.owner_id,
            lease_epoch=self._positive_int(
                response.payload["lease_epoch"],
                "lease_epoch",
            ),
        )
        self._record_owner_evidence("lease_renewed", renewed)
        return renewed

    def execute_phase(
        self,
        lease: LeaseHandle,
        point: FaultPoint,
        *,
        barrier_mode: BarrierMode = BarrierMode.CRASH,
        wait_for_result: bool = True,
    ) -> SupervisedOperationHandle | ControlMessage:
        self._supervisor.require_active(GenerationHandle(lease.generation_id))
        self._next_operation_id += 1
        operation = SupervisedOperationHandle(
            generation_id=lease.generation_id,
            operation_id=self._next_operation_id,
            owner_id=lease.owner_id,
            lease_epoch=lease.lease_epoch,
        )
        self._send(
            MessageKind.EXECUTE_PHASE,
            owner_id=str(lease.owner_id),
            lease_epoch=lease.lease_epoch,
            operation_id=operation.operation_id,
            point=point.value,
            barrier_mode=barrier_mode.value,
        )
        if not wait_for_result:
            return operation
        return self._wait_for_response(
            MessageKind.OPERATION_RESULT,
            operation_id=operation.operation_id,
        )

    def wait_for_event(
        self,
        kind: MessageKind,
        *,
        timeout_s: float,
    ) -> ControlMessage:
        return self._wait_for_response(kind, timeout_s=timeout_s)

    def status(self, observer_id: str) -> ControlMessage:
        record = self._supervisor.status().generation
        if record is None:
            raise StaleRuntimeGeneration("No runtime generation is current")
        self._supervisor.require_active(record.handle)
        self._send(MessageKind.STATUS, observer_id=observer_id)
        return self._wait_for_response(MessageKind.STATUS_RESULT)

    def kill_controller(self) -> None:
        record = self._supervisor.status().generation
        if record is None:
            raise StaleRuntimeGeneration("No runtime generation is current")
        self._supervisor.require_active(record.handle)
        self._require_processes().terminate_controller(WORKER_TERMINATION_GRACE_S)

    def wait_terminated(self, timeout_s: float) -> GenerationRecord:
        deadline = monotonic() + timeout_s
        while True:
            status = self._supervisor.status()
            if (
                status.generation is not None
                and status.lifecycle is GenerationLifecycle.TERMINATED
                and self._all_stopped()
                and not self._termination_owner_is_alive()
            ):
                self._raise_terminal_termination_error()
                return status.generation

            remaining = deadline - monotonic()
            if remaining <= 0:
                raise CommandTimeout(
                    f"Runtime generation did not terminate within {timeout_s} seconds"
                )
            self._poll_once(min(SUPERVISOR_POLL_INTERVAL_S, remaining))

    def close(self) -> None:
        status = self._supervisor.status()
        if status.generation is None:
            return
        if status.lifecycle in {
            GenerationLifecycle.STARTING,
            GenerationLifecycle.ACTIVE,
        }:
            self._begin_async_termination(TerminationCause.REQUESTED)
        self._wait_for_cleanup_owner()

    def _create_processes(self, generation_id: int) -> StagedGenerationProcesses:
        self._processes = None
        processes = self._processes_factory(generation_id)
        self._processes = processes
        return processes

    def _receive_startup(self, expected: MessageKind, timeout_s: float) -> None:
        message = self._require_processes().receive(timeout_s)
        self._accept_worker_message(message)
        self._raise_worker_error(message)
        if message.kind is not expected:
            raise InvalidMessageSequence(
                f"Expected {expected.value}, received {message.kind.value}"
            )

    def _send(self, kind: MessageKind, **payload: object) -> None:
        sender = self._sender
        if sender is None:
            raise ControllerUnavailable("No Controller sender exists")
        self._require_processes().send(sender.create(kind, **payload))

    def _wait_for_response(
        self,
        expected: MessageKind,
        *,
        timeout_s: float = READY_TIMEOUT_S,
        operation_id: int | None = None,
    ) -> ControlMessage:
        deadline = monotonic() + timeout_s
        while True:
            pending = self._take_pending(expected, operation_id)
            if pending is not None:
                return pending
            self._raise_if_generation_unavailable()

            remaining = deadline - monotonic()
            if remaining <= 0:
                raise CommandTimeout(
                    f"Controller did not publish {expected.value} within "
                    f"{timeout_s} seconds"
                )
            poll_timeout = min(SUPERVISOR_POLL_INTERVAL_S, remaining)
            poll_started = monotonic()
            message = self._receive_once(poll_timeout)
            if message is not None:
                self._raise_worker_error(message)
                if self._matches(message, expected, operation_id):
                    return message
                if message.kind is not MessageKind.HEARTBEAT:
                    self._pending.append(message)
            self._finish_poll_interval(poll_started, poll_timeout)
            self._poll_supervisor()
            self._raise_if_generation_unavailable()

    def _poll_once(self, timeout_s: float) -> None:
        poll_started = monotonic()
        message = self._receive_once(timeout_s)
        if message is not None and message.kind is not MessageKind.HEARTBEAT:
            self._pending.append(message)
        self._finish_poll_interval(poll_started, timeout_s)
        self._poll_supervisor()

    def _poll_supervisor(self) -> None:
        cause = self._supervisor.detect_termination_cause()
        if cause is not None:
            self._begin_async_termination(cause)
            return
        if self._supervisor.status().lifecycle is GenerationLifecycle.TERMINATING:
            if self._supervisor.termination_time_remaining() > 0:
                self._ensure_termination_thread()

    def _receive_once(self, timeout_s: float) -> ControlMessage | None:
        try:
            message = self._require_processes().receive(timeout_s)
        except (CommandTimeout, EOFError, OSError):
            return None
        self._accept_worker_message(message)
        return message

    def _accept_worker_message(self, message: ControlMessage) -> None:
        self._supervisor.on_worker_message(message)
        if message.kind is MessageKind.LEASE_EXPIRED:
            self._begin_async_termination(TerminationCause.LEASE_EXPIRED)

    def _begin_async_termination(self, cause: TerminationCause) -> None:
        self._supervisor.begin_termination(cause)
        self._ensure_termination_thread()

    def _ensure_termination_thread(self) -> None:
        with self._termination_thread_lock:
            thread = self._termination_thread
            if thread is not None and thread.is_alive():
                return
            if self._supervisor.termination_time_remaining() <= 0:
                return
            self._termination_error = None
            thread = Thread(
                target=self._complete_termination,
                name="onec-runtime-termination",
                daemon=True,
            )
            self._termination_thread = thread
            thread.start()

    def _complete_termination(self) -> None:
        try:
            self._supervisor.complete_termination()
        except BaseException as error:
            self._termination_error = error

    def _raise_if_generation_unavailable(self) -> None:
        status = self._supervisor.status()
        if status.lifecycle not in {
            GenerationLifecycle.TERMINATING,
            GenerationLifecycle.TERMINATED,
        }:
            return
        cause = status.termination_cause
        cause_name = cause.value if cause is not None else "unknown"
        raise ControllerUnavailable(
            f"Runtime generation is unavailable after {cause_name}"
        )

    @staticmethod
    def _finish_poll_interval(started_at: float, interval_s: float) -> None:
        deadline = started_at + interval_s
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                return
            sleep(remaining)

    def _take_pending(
        self,
        expected: MessageKind,
        operation_id: int | None,
    ) -> ControlMessage | None:
        for message in tuple(self._pending):
            self._raise_worker_error(message)
            if self._matches(message, expected, operation_id):
                self._pending.remove(message)
                return message
        return None

    @staticmethod
    def _matches(
        message: ControlMessage,
        expected: MessageKind,
        operation_id: int | None,
    ) -> bool:
        return message.kind is expected and (
            operation_id is None
            or message.payload.get("operation_id") == operation_id
        )

    @staticmethod
    def _raise_worker_error(message: ControlMessage) -> None:
        if message.kind is MessageKind.LEASE_EXPIRED:
            raise LeaseExpired("The generation lease has expired")
        if message.kind is MessageKind.FATAL:
            raise ControllerUnavailable(
                f"Controller failed with {message.payload.get('error_type', 'unknown')}"
            )
        if message.kind is not MessageKind.LEASE_CONFLICT:
            return
        error_name = str(message.payload.get("error_type", "LeaseConflict"))
        error_type = _TYPED_WORKER_ERRORS.get(error_name, LeaseConflict)
        raise error_type(f"Controller rejected request with {error_name}")

    def _record_owner_evidence(self, event: str, lease: LeaseHandle) -> None:
        self._journal.record(
            JOURNAL_STREAM,
            event,
            generation_id=lease.generation_id,
            owner_sha256=sha256(lease.owner_id.bytes).hexdigest(),
            lease_epoch=lease.lease_epoch,
        )
        self._journal.flush()

    def _terminate_failed_startup(self) -> None:
        status = self._supervisor.status()
        if status.generation is None:
            return
        if status.lifecycle is GenerationLifecycle.STARTING:
            self._supervisor.terminate(TerminationCause.STARTUP_FAILED)

    def _require_processes(self) -> StagedGenerationProcesses:
        if self._processes is None:
            raise RuntimeError("No runtime generation processes exist")
        return self._processes

    def _all_stopped(self) -> bool:
        processes = self._processes
        return processes is None or processes.all_stopped()

    def _wait_for_cleanup_owner(self) -> None:
        while True:
            status = self._supervisor.status()
            if status.lifecycle is GenerationLifecycle.TERMINATED:
                thread = self._termination_thread
                if thread is not None and thread.is_alive():
                    remaining = self._supervisor.termination_time_remaining()
                    thread.join(remaining)
                    if thread.is_alive():
                        raise CommandTimeout(
                            "Runtime generation cleanup exceeded its deadline"
                        )
                self._raise_terminal_termination_error()
                return
            if status.lifecycle is not GenerationLifecycle.TERMINATING:
                return

            remaining = self._supervisor.termination_time_remaining()
            if remaining <= 0:
                raise CommandTimeout("Runtime generation cleanup exceeded its deadline")
            self._ensure_termination_thread()
            thread = self._termination_thread
            if thread is None:
                raise RuntimeError("Runtime generation cleanup owner is unavailable")
            thread.join(remaining)
            if thread.is_alive():
                raise CommandTimeout("Runtime generation cleanup exceeded its deadline")

    def _termination_owner_is_alive(self) -> bool:
        thread = self._termination_thread
        return thread is not None and thread.is_alive()

    def _raise_terminal_termination_error(self) -> None:
        error = self._termination_error
        if error is None:
            return
        self._termination_error = None
        raise error

    @staticmethod
    def _positive_int(value: object, field_name: str) -> int:
        if type(value) is not int or value <= 0:
            raise InvalidMessageSequence(f"{field_name} must be a positive integer")
        return value
