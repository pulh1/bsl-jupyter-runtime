from __future__ import annotations

from dataclasses import dataclass, replace
from threading import Lock
from time import monotonic
from typing import Callable, Protocol, cast

from onec_runtime.errors import StaleRuntimeGeneration
from onec_runtime.recovery_journal import RecoveryJournal
from onec_runtime.supervisor_model import (
    GenerationHandle,
    GenerationLifecycle,
    GenerationRecord,
    GenerationRegistry,
    TerminationCause,
)
from onec_runtime.supervisor_protocol import (
    ControlMessage,
    MessageKind,
    MessageReceiver,
    MessageSender,
)


HEARTBEAT_WATCHDOG_S = 1.5
WORKER_TERMINATION_GRACE_S = 2.0
RUNTIME_TERMINATION_GRACE_S = 10.0
TERMINATION_BOUND_S = 25.0
JOURNAL_STREAM = "supervisor.jsonl"


class GenerationProcesses(Protocol):
    controller_pid: int | None
    dbgs_pid: int | None
    onec_pid: int | None

    def controller_exitcode(self) -> int | None:
        raise NotImplementedError

    def terminate_controller(self, timeout_s: float) -> None:
        raise NotImplementedError

    def terminate_onec(self, timeout_s: float) -> None:
        raise NotImplementedError

    def terminate_dbgs(self, timeout_s: float) -> None:
        raise NotImplementedError

    def all_stopped(self) -> bool:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class SupervisorStatus:
    generation: GenerationRecord | None
    lifecycle: GenerationLifecycle | None
    termination_cause: TerminationCause | None
    last_progress_sequence: int | None
    heartbeat_deadline: float | None


class _RuntimeGenerationRegistry(GenerationRegistry):
    """Adds the Supervisor-only fail-closed transition from STARTING."""

    def begin_startup_termination(
        self,
        handle: GenerationHandle,
        cause: TerminationCause,
    ) -> GenerationRecord:
        record = self._require_current(handle)
        self._require_lifecycle(
            record,
            GenerationLifecycle.STARTING,
            "begin startup termination",
        )
        self._current = replace(
            record,
            lifecycle=GenerationLifecycle.TERMINATING,
            termination_cause=cause,
        )
        return self._current


class RuntimeSupervisor:
    """Owns one runtime generation and terminates it on loss of control."""

    def __init__(
        self,
        process_factory: Callable[[int], GenerationProcesses],
        *,
        clock: Callable[[], float] = monotonic,
        journal: RecoveryJournal | None = None,
        heartbeat_timeout_s: float = HEARTBEAT_WATCHDOG_S,
    ) -> None:
        if heartbeat_timeout_s <= 0:
            raise ValueError("heartbeat_timeout_s must be positive")
        self._process_factory = process_factory
        self._clock = clock
        self._journal = journal or RecoveryJournal()
        self._heartbeat_timeout_s = heartbeat_timeout_s
        self._registry = _RuntimeGenerationRegistry()
        self._record: GenerationRecord | None = None
        self._processes: GenerationProcesses | None = None
        self._sender: MessageSender | None = None
        self._receiver: MessageReceiver | None = None
        self._last_progress_sequence: int | None = None
        self._heartbeat_deadline: float | None = None
        self._termination_detected_at: float | None = None
        self._termination_deadline: float | None = None
        self._termination_evidence_errors: list[BaseException] = []
        self._cleanup_lock = Lock()

    def begin_generation(self) -> GenerationHandle:
        record = self._registry.begin_start()
        self._record = record
        self._processes = None
        self._sender = None
        self._receiver = None
        self._processes = self._process_factory(record.handle.generation_id)
        self._sender = MessageSender(record.handle.generation_id)
        self._receiver = MessageReceiver(record.handle.generation_id)
        self._last_progress_sequence = None
        self._heartbeat_deadline = None
        self._termination_detected_at = None
        self._termination_deadline = None
        self._termination_evidence_errors.clear()
        return record.handle

    def activate(self, handle: GenerationHandle) -> GenerationRecord:
        processes = self._require_processes()
        self._record = self._registry.activate(
            handle,
            controller_pid=cast(int, processes.controller_pid),
            dbgs_pid=cast(int, processes.dbgs_pid),
            onec_pid=cast(int, processes.onec_pid),
        )
        self._heartbeat_deadline = self._clock() + self._heartbeat_timeout_s
        return self._record

    def require_active(self, handle: GenerationHandle) -> GenerationRecord:
        return self._registry.require_active(handle)

    def on_worker_message(self, message: ControlMessage) -> None:
        if (
            message.kind is MessageKind.HEARTBEAT
            and (
                self._record is None
                or message.generation_id != self._record.handle.generation_id
            )
        ):
            self.heartbeat(
                GenerationHandle(message.generation_id),
                progress_sequence=int(message.payload["progress_sequence"]),
                busy_deadline=None,
            )
            return

        receiver = self._receiver
        if receiver is None:
            raise RuntimeError("No runtime generation exists")
        receiver.accept(message)
        if message.kind is MessageKind.HEARTBEAT:
            busy_deadline = message.payload.get("busy_deadline")
            self.heartbeat(
                GenerationHandle(message.generation_id),
                progress_sequence=int(message.payload["progress_sequence"]),
                busy_deadline=(
                    float(busy_deadline) if busy_deadline is not None else None
                ),
            )
        elif self._record is not None and (
            self._record.lifecycle is GenerationLifecycle.ACTIVE
        ):
            self._heartbeat_deadline = self._clock() + self._heartbeat_timeout_s

    def heartbeat(
        self,
        handle: GenerationHandle,
        progress_sequence: int,
        busy_deadline: float | None,
    ) -> None:
        try:
            self._registry.require_active(handle)
        except StaleRuntimeGeneration:
            self._journal.record(
                JOURNAL_STREAM,
                "late_heartbeat_ignored",
                generation_id=handle.generation_id,
                progress_sequence=progress_sequence,
            )
            return

        now = self._clock()
        self._last_progress_sequence = progress_sequence
        self._heartbeat_deadline = (
            busy_deadline
            if busy_deadline is not None and busy_deadline > now
            else now + self._heartbeat_timeout_s
        )

    def poll(self) -> None:
        cause = self.detect_termination_cause()
        if cause is not None:
            self.terminate(cause)

    def detect_termination_cause(self) -> TerminationCause | None:
        record = self._record
        if record is None or record.lifecycle is not GenerationLifecycle.ACTIVE:
            return None

        processes = self._require_processes()
        if processes.controller_exitcode() is not None:
            return TerminationCause.CONTROLLER_EXIT

        if (
            self._heartbeat_deadline is not None
            and self._clock() >= self._heartbeat_deadline
        ):
            return TerminationCause.CONTROLLER_HUNG
        return None

    def terminate(self, cause: TerminationCause) -> GenerationRecord:
        record = self.begin_termination(cause)
        if record.lifecycle is GenerationLifecycle.TERMINATED:
            return record
        return self.complete_termination()

    def begin_termination(self, cause: TerminationCause) -> GenerationRecord:
        record = self._record
        if record is None:
            raise RuntimeError("No runtime generation exists")
        if record.lifecycle in {
            GenerationLifecycle.TERMINATING,
            GenerationLifecycle.TERMINATED,
        }:
            return record

        detected_at = self._clock()
        if record.lifecycle is GenerationLifecycle.STARTING:
            record = self._registry.begin_startup_termination(record.handle, cause)
        else:
            record = self._registry.begin_termination(record.handle, cause)
        self._record = record
        self._termination_detected_at = detected_at
        self._termination_deadline = detected_at + TERMINATION_BOUND_S
        self._heartbeat_deadline = None
        self._record_termination_evidence(
            "generation_termination_planned",
            generation_id=record.handle.generation_id,
            cause=cause.value,
            detected_at=detected_at,
        )
        return record

    def complete_termination(self) -> GenerationRecord:
        with self._cleanup_lock:
            return self._complete_termination_owned()

    def _complete_termination_owned(self) -> GenerationRecord:
        record = self._record
        if record is None:
            raise RuntimeError("No runtime generation exists")
        if record.lifecycle is GenerationLifecycle.TERMINATED:
            return record
        if record.lifecycle is not GenerationLifecycle.TERMINATING:
            raise RuntimeError(
                f"Cannot complete termination in {record.lifecycle.name} lifecycle"
            )

        processes = self._processes
        if processes is not None:
            cleanup_errors: list[BaseException] = []
            for terminate_process, timeout_cap_s in (
                (processes.terminate_controller, WORKER_TERMINATION_GRACE_S),
                (processes.terminate_onec, RUNTIME_TERMINATION_GRACE_S),
                (processes.terminate_dbgs, RUNTIME_TERMINATION_GRACE_S),
            ):
                timeout_s = min(
                    timeout_cap_s,
                    self._termination_time_remaining(),
                )
                try:
                    terminate_process(timeout_s)
                except BaseException as error:
                    cleanup_errors.append(error)
            if not processes.all_stopped():
                incomplete = RuntimeError(
                    "Runtime generation processes did not stop"
                )
                if cleanup_errors:
                    raise incomplete from cleanup_errors[0]
                raise incomplete

        detected_at = self._termination_detected_at
        if detected_at is None:
            raise RuntimeError("Termination detection timestamp is unavailable")
        cause = record.termination_cause
        if cause is None:
            raise RuntimeError("Termination cause is unavailable")
        terminated_at = self._clock()
        record = self._registry.finish_termination(record.handle)
        self._record = record
        duration_s = terminated_at - detected_at
        self._record_termination_evidence(
            "generation_terminated",
            generation_id=record.handle.generation_id,
            cause=cause.value,
            detected_at=detected_at,
            terminated_at=terminated_at,
            duration_s=duration_s,
        )
        if duration_s > TERMINATION_BOUND_S:
            self._record_termination_evidence(
                "termination_bound_exceeded",
                generation_id=record.handle.generation_id,
                detected_at=detected_at,
                terminated_at=terminated_at,
                duration_s=duration_s,
            )
        self._raise_termination_evidence_errors()
        return record

    def status(self) -> SupervisorStatus:
        record = self._record
        return SupervisorStatus(
            generation=record,
            lifecycle=record.lifecycle if record is not None else None,
            termination_cause=(
                record.termination_cause if record is not None else None
            ),
            last_progress_sequence=self._last_progress_sequence,
            heartbeat_deadline=self._heartbeat_deadline,
        )

    def termination_time_remaining(self) -> float:
        return self._termination_time_remaining()

    def _require_processes(self) -> GenerationProcesses:
        if self._processes is None:
            raise RuntimeError("No runtime generation processes exist")
        return self._processes

    def _termination_time_remaining(self) -> float:
        deadline = self._termination_deadline
        if deadline is None:
            raise RuntimeError("Termination deadline is unavailable")
        return max(0.0, deadline - self._clock())

    def _record_termination_evidence(self, event: str, **fields: object) -> None:
        try:
            self._journal.record(JOURNAL_STREAM, event, **fields)
            self._journal.flush()
        except BaseException as error:
            self._termination_evidence_errors.append(error)

    def _raise_termination_evidence_errors(self) -> None:
        errors = self._termination_evidence_errors
        if not errors:
            return
        self._termination_evidence_errors = []
        if len(errors) == 1:
            raise errors[0]
        raise BaseExceptionGroup("Termination evidence failures", errors)
