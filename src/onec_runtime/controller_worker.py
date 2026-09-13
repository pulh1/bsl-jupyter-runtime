from __future__ import annotations

from collections.abc import Callable
from enum import Enum
from multiprocessing.connection import Connection
from threading import Event
from time import monotonic
from typing import Protocol
from uuid import UUID

from onec_runtime.errors import (
    CommandTimeout,
    ControllerUnavailable,
    InvalidMessageSequence,
    LeaseConflict,
    LeaseExpired,
    StaleLeaseEpoch,
)
from onec_runtime.fault_injection import FaultPoint
from onec_runtime.lease import LeaseAuthority, validate_lease_ttl
from onec_runtime.recovery_journal import RecoveryJournal
from onec_runtime.supervisor_model import LeaseHandle
from onec_runtime.supervisor_protocol import (
    ControlMessage,
    MessageKind,
    MessageReceiver,
    MessageSender,
)


WORKER_POLL_INTERVAL_S = 0.05
HEARTBEAT_INTERVAL_S = 0.25
DEBUGGEE_START_TIMEOUT_S = 90.0
SAFE_RUNTIME_STATES = frozenset(
    {
        "idle",
        "main_pending",
        "captured",
        "evaluating_capture",
        "debug_stopped",
        "flushing",
        "partial_writeback_failure",
        "breakpoint_restore_failure",
        "resuming",
        "recovering",
        "lost",
        "completed",
        "failed",
    }
)


class BarrierMode(str, Enum):
    CRASH = "crash"
    HANG = "hang"
    LEASE_EXPIRY = "lease_expiry"


class _BarrierShutdown(BaseException):
    """Unwinds driver execution when shutdown arrives at a phase barrier."""


Publish = Callable[[MessageKind, dict[str, object]], None]
BarrierWait = Callable[..., None]


class PhaseBarrier:
    """Publishes a durable phase boundary before entering its wait mode."""

    def __init__(
        self,
        *,
        point: FaultPoint,
        publish: Publish,
        flush: Callable[[], None],
        wait: BarrierWait,
        mode: BarrierMode = BarrierMode.CRASH,
        payload: dict[str, object] | None = None,
    ) -> None:
        self._point = point
        self._publish = publish
        self._flush = flush
        self._wait = wait
        self._mode = mode
        self._payload = payload or {}
        self._fired = False

    def __call__(self, point: FaultPoint) -> None:
        if self._fired or point is not self._point:
            return
        self._fired = True
        self._flush()
        self._publish(
            MessageKind.PHASE_REACHED,
            {"point": point.value, **self._payload},
        )
        if self._mode is BarrierMode.HANG:
            self._wait(heartbeat_enabled=False)
        else:
            self._wait()


class RuntimeDriver(Protocol):
    def prepare_debug_ui(self) -> None:
        raise NotImplementedError

    def attach_runtime(self) -> None:
        raise NotImplementedError

    def execute_phase(self, point: FaultPoint, barrier: PhaseBarrier) -> object:
        raise NotImplementedError

    def status(self) -> dict[str, object]:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class ControllerWorker:
    """Single-writer child control plane for one supervised generation."""

    def __init__(
        self,
        generation_id: int,
        driver: RuntimeDriver,
        *,
        connection: Connection | None = None,
        emit: Callable[[ControlMessage], None] | None = None,
        clock: Callable[[], float] = monotonic,
        journal: RecoveryJournal | None = None,
    ) -> None:
        if emit is None and connection is None:
            raise ValueError("ControllerWorker requires a connection or emit callback")
        self._generation_id = generation_id
        self._driver = driver
        self._connection = connection
        self._emit = emit if emit is not None else connection.send  # type: ignore[union-attr]
        self._clock = clock
        self._journal = journal or RecoveryJournal()
        self._leases = LeaseAuthority(generation_id, clock=clock)
        self._sender = MessageSender(generation_id)
        self._receiver = MessageReceiver(generation_id)
        self._running = True
        self._at_barrier = False
        self._barrier_mode: BarrierMode | None = None
        self._progress_sequence = 0
        self._last_heartbeat_at = clock()

    def run(self) -> None:
        try:
            try:
                self._run()
            finally:
                self._driver.close()
        except Exception as error:
            self._publish(
                MessageKind.FATAL,
                {"error_type": type(error).__name__},
            )
            raise

    def _run(self) -> None:
        connection = self._require_connection()
        self._driver.prepare_debug_ui()
        self._advance_progress()
        self._publish(MessageKind.DEBUG_READY, {})

        deadline = self._clock() + DEBUGGEE_START_TIMEOUT_S
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise CommandTimeout("Timed out waiting for debuggee start")
            if not connection.poll(min(WORKER_POLL_INTERVAL_S, remaining)):
                continue
            message = connection.recv()
            self._receiver.accept(message)
            if message.kind is not MessageKind.DEBUGGEE_STARTED:
                raise InvalidMessageSequence(
                    f"Unexpected {message.kind.value} before debuggee start"
                )
            self._advance_progress()
            break

        self._driver.attach_runtime()
        self._advance_progress()
        self._publish(MessageKind.READY, {})
        self._last_heartbeat_at = self._clock()

        while self._running:
            self._expire_lease()
            if connection.poll(WORKER_POLL_INTERVAL_S):
                self.handle_message(connection.recv())
            self._emit_heartbeat_if_due()

    def handle_message(self, message: ControlMessage) -> None:
        self._expire_lease()
        self._receiver.accept(message)

        if message.kind is MessageKind.GRANT_LEASE:
            self._grant_lease(message.payload)
        elif message.kind is MessageKind.RENEW_LEASE:
            self._renew_lease(message.payload)
        elif message.kind is MessageKind.EXECUTE_PHASE:
            try:
                self._execute_phase(message.payload)
            except _BarrierShutdown:
                return
        elif message.kind is MessageKind.STATUS:
            self._status()
        elif message.kind is MessageKind.SHUTDOWN:
            self._running = False
        else:
            raise InvalidMessageSequence(
                f"Unexpected worker command {message.kind.value}"
            )
        self._advance_progress()

    def poll_once(self) -> None:
        self._expire_lease()

    def _grant_lease(self, payload: dict[str, object]) -> None:
        try:
            handle = self._leases.grant(
                self._owner(payload),
                ttl_s=validate_lease_ttl(payload["ttl_s"]),
            )
        except (LeaseConflict, LeaseExpired) as error:
            self._publish_lease_conflict(error)
            return
        self._publish_lease_granted(handle)

    def _renew_lease(self, payload: dict[str, object]) -> None:
        try:
            handle = self._leases.renew(
                self._lease_handle(payload),
                ttl_s=validate_lease_ttl(payload["ttl_s"]),
            )
        except (LeaseConflict, LeaseExpired, StaleLeaseEpoch) as error:
            self._publish_lease_conflict(error)
            return
        self._publish_lease_granted(handle)

    def _execute_phase(self, payload: dict[str, object]) -> None:
        if self._at_barrier:
            self._publish_lease_conflict(
                ControllerUnavailable("A phase barrier is already active")
            )
            return
        try:
            self._leases.validate(self._lease_handle(payload))
        except (LeaseConflict, LeaseExpired, StaleLeaseEpoch) as error:
            self._publish_lease_conflict(error)
            return

        point = FaultPoint(str(payload["point"]))
        mode = BarrierMode(str(payload["barrier_mode"]))
        barrier_payload: dict[str, object] = {}
        operation_id_value = payload.get("operation_id")
        operation_id = None
        if operation_id_value is not None:
            operation_id = self._positive_int(operation_id_value, "operation_id")
            barrier_payload["operation_id"] = operation_id

        barrier = PhaseBarrier(
            point=point,
            mode=mode,
            publish=self._publish,
            flush=self._journal.flush,
            wait=self._wait_at_barrier,
            payload=barrier_payload,
        )
        self._at_barrier = True
        self._barrier_mode = mode
        try:
            self._driver.execute_phase(point, barrier)
        finally:
            self._barrier_mode = None
            self._at_barrier = False
        if not self._running:
            return
        result_payload: dict[str, object] = {"phase": point.value}
        if operation_id is not None:
            result_payload["operation_id"] = operation_id
        self._publish(MessageKind.OPERATION_RESULT, result_payload)

    def _status(self) -> None:
        lease = self._leases.status()
        driver_status = self._driver.status()
        state = driver_status.get("state")
        safe_state = (
            state
            if type(state) is str and state in SAFE_RUNTIME_STATES
            else "unknown"
        )
        self._publish(
            MessageKind.STATUS_RESULT,
            {
                "state": safe_state,
                "lease_epoch": lease.lease_epoch,
                "expires_in_s": lease.expires_in_s,
                "lease_expired": lease.expired,
            },
        )

    def _wait_at_barrier(self, *, heartbeat_enabled: bool = True) -> None:
        if not heartbeat_enabled:
            Event().wait()
            return

        connection = self._require_connection()
        while self._running:
            self._expire_lease()
            if connection.poll(WORKER_POLL_INTERVAL_S):
                self.handle_message(connection.recv())
                if not self._running:
                    raise _BarrierShutdown
            self._emit_heartbeat_if_due()
        raise _BarrierShutdown

    def _expire_lease(self) -> None:
        expired = self._leases.expire_if_due()
        if expired is None:
            return
        self._publish(
            MessageKind.LEASE_EXPIRED,
            {"lease_epoch": expired.lease_epoch},
        )

    def _emit_heartbeat_if_due(self) -> None:
        now = self._clock()
        if now - self._last_heartbeat_at < HEARTBEAT_INTERVAL_S:
            return
        self._publish(
            MessageKind.HEARTBEAT,
            {
                "progress_sequence": self._progress_sequence,
                "busy_deadline": None,
            },
        )
        self._last_heartbeat_at = now

    def _publish_lease_granted(self, handle: LeaseHandle) -> None:
        self._publish(
            MessageKind.LEASE_GRANTED,
            {
                "lease_epoch": handle.lease_epoch,
                "expires_in_s": self._leases.status().expires_in_s,
            },
        )

    def _publish_lease_conflict(self, error: Exception) -> None:
        self._publish(
            MessageKind.LEASE_CONFLICT,
            {"error_type": type(error).__name__},
        )

    def _publish(self, kind: MessageKind, payload: dict[str, object]) -> None:
        self._emit(self._sender.create(kind, **payload))

    def _lease_handle(self, payload: dict[str, object]) -> LeaseHandle:
        return LeaseHandle(
            generation_id=self._generation_id,
            owner_id=self._owner(payload),
            lease_epoch=self._positive_int(payload["lease_epoch"], "lease_epoch"),
        )

    @staticmethod
    def _owner(payload: dict[str, object]) -> UUID:
        return UUID(str(payload["owner_id"]))

    @staticmethod
    def _positive_int(value: object, field_name: str) -> int:
        if type(value) is not int:
            raise ValueError(f"{field_name} must be an integer")
        if value <= 0:
            raise ValueError(f"{field_name} must be positive")
        return value

    def _advance_progress(self) -> None:
        self._progress_sequence += 1

    def _require_connection(self) -> Connection:
        if self._connection is None:
            raise RuntimeError("ControllerWorker command loop requires a connection")
        return self._connection


def controller_worker_main(
    generation_id: int,
    connection: Connection,
    driver: RuntimeDriver,
    journal: RecoveryJournal | None = None,
) -> None:
    ControllerWorker(
        generation_id,
        driver,
        connection=connection,
        journal=journal,
    ).run()
