from __future__ import annotations

import pickle
from multiprocessing import Pipe
from threading import Timer
from time import monotonic
from uuid import UUID

import pytest

from onec_runtime.controller_worker import (
    BarrierMode,
    ControllerWorker,
    PhaseBarrier,
)
from onec_runtime.errors import CommandTimeout, InvalidMessageSequence
from onec_runtime.fault_injection import FaultPoint
from onec_runtime.lease import MAX_LEASE_TTL_S
from onec_runtime.recovery_journal import RecoveryJournal
from onec_runtime.supervisor_protocol import MessageKind, MessageSender


OWNER_A = UUID("11111111-1111-1111-1111-111111111111")
OWNER_B = UUID("22222222-2222-2222-2222-222222222222")
SECRET_BSL = "Сообщить(СекретноеЗначение)"


class SecretLiveValue:
    def __init__(self) -> None:
        self.source = SECRET_BSL


class Clock:
    value = 100.0

    def __call__(self) -> float:
        return self.value


class FakeDriver:
    def __init__(self) -> None:
        self.phases: list[FaultPoint] = []
        self.prepared = False
        self.attached = False
        self.closed = False

    def prepare_debug_ui(self) -> None:
        self.prepared = True

    def attach_runtime(self) -> None:
        self.attached = True

    def execute_phase(self, point, barrier):  # type: ignore[no-untyped-def]
        self.phases.append(point)
        return {"phase": point.value}

    def status(self) -> dict[str, object]:
        return {"state": "captured"}

    def close(self) -> None:
        self.closed = True


class BarrierDriver(FakeDriver):
    def execute_phase(self, point, barrier):  # type: ignore[no-untyped-def]
        self.phases.append(point)
        barrier(point)
        return {"phase": point.value}


class PostBarrierEffectDriver(FakeDriver):
    def __init__(self) -> None:
        super().__init__()
        self.post_barrier_effects: list[str] = []

    def execute_phase(self, point, barrier):  # type: ignore[no-untyped-def]
        self.phases.append(point)
        barrier(point)
        self.post_barrier_effects.append("root_write_sent")
        return {"phase": point.value}


class SecretBearingDriver(FakeDriver):
    def execute_phase(self, point, barrier):  # type: ignore[no-untyped-def]
        self.phases.append(point)
        return {
            "phase": point.value,
            "source": SECRET_BSL,
            "value": SecretLiveValue(),
        }

    def status(self) -> dict[str, object]:
        return {
            "state": "captured",
            "source": SECRET_BSL,
            "live": SecretLiveValue(),
        }


class SecretStateDriver(FakeDriver):
    def status(self) -> dict[str, object]:
        return {"state": SECRET_BSL}


def test_owner_can_mutate_rival_conflicts_and_observer_reads_status() -> None:
    clock = Clock()
    emitted = []
    driver = FakeDriver()
    worker = ControllerWorker(7, driver, emit=emitted.append, clock=clock)
    sender = MessageSender(7)
    worker.handle_message(
        sender.create(
            MessageKind.GRANT_LEASE,
            owner_id=str(OWNER_A),
            ttl_s=2.0,
        )
    )
    granted = emitted[-1]
    worker.handle_message(
        sender.create(
            MessageKind.EXECUTE_PHASE,
            owner_id=str(OWNER_B),
            lease_epoch=granted.payload["lease_epoch"],
            point=FaultPoint.AFTER_CAPTURE_CHECKPOINT.value,
            barrier_mode="crash",
        )
    )
    worker.handle_message(sender.create(MessageKind.STATUS, observer_id="reader"))

    assert driver.phases == []
    assert [item.kind for item in emitted[-2:]] == [
        MessageKind.LEASE_CONFLICT,
        MessageKind.STATUS_RESULT,
    ]


def test_lease_expiry_emits_event_and_never_grants_takeover() -> None:
    clock = Clock()
    emitted = []
    worker = ControllerWorker(7, FakeDriver(), emit=emitted.append, clock=clock)
    sender = MessageSender(7)
    worker.handle_message(
        sender.create(
            MessageKind.GRANT_LEASE,
            owner_id=str(OWNER_A),
            ttl_s=2.0,
        )
    )
    clock.value = 102.0
    worker.poll_once()
    worker.handle_message(
        sender.create(
            MessageKind.GRANT_LEASE,
            owner_id=str(OWNER_B),
            ttl_s=2.0,
        )
    )

    assert [item.kind for item in emitted[-2:]] == [
        MessageKind.LEASE_EXPIRED,
        MessageKind.LEASE_CONFLICT,
    ]


@pytest.mark.parametrize(
    "invalid_ttl",
    [
        True,
        "2.0",
        type("CoerciveTTL", (), {"__float__": lambda self: 2.0})(),
        float("nan"),
        float("inf"),
        0,
        -1.0,
        MAX_LEASE_TTL_S + 0.1,
    ],
)
def test_worker_rejects_invalid_ttl_before_lease_or_driver_mutation(
    invalid_ttl: object,
) -> None:
    driver = FakeDriver()
    emitted = []
    worker = ControllerWorker(7, driver, emit=emitted.append, clock=Clock())
    sender = MessageSender(7)

    with pytest.raises(ValueError, match="ttl_s"):
        worker.handle_message(
            sender.create(
                MessageKind.GRANT_LEASE,
                owner_id=str(OWNER_A),
                ttl_s=invalid_ttl,
            )
        )

    assert emitted == []
    assert driver.phases == []
    worker.handle_message(
        sender.create(
            MessageKind.GRANT_LEASE,
            owner_id=str(OWNER_A),
            ttl_s=95.0,
        )
    )
    assert emitted[-1].payload["lease_epoch"] == 1


def test_generation_epoch_and_sequence_are_validated_before_driver_call() -> None:
    clock = Clock()
    emitted = []
    driver = FakeDriver()
    worker = ControllerWorker(7, driver, emit=emitted.append, clock=clock)
    sender = MessageSender(7)
    worker.handle_message(
        sender.create(MessageKind.GRANT_LEASE, owner_id=str(OWNER_A), ttl_s=2.0)
    )

    wrong_generation = MessageSender(8).create(
        MessageKind.EXECUTE_PHASE,
        owner_id=str(OWNER_A),
        lease_epoch=1,
        point=FaultPoint.AFTER_CAPTURE_CHECKPOINT.value,
        barrier_mode="crash",
    )
    with pytest.raises(InvalidMessageSequence, match="generation"):
        worker.handle_message(wrong_generation)

    wrong_epoch = sender.create(
        MessageKind.EXECUTE_PHASE,
        owner_id=str(OWNER_A),
        lease_epoch=2,
        point=FaultPoint.AFTER_CAPTURE_CHECKPOINT.value,
        barrier_mode="crash",
    )
    worker.handle_message(wrong_epoch)
    with pytest.raises(InvalidMessageSequence, match="sequence"):
        worker.handle_message(wrong_epoch)

    assert driver.phases == []
    assert emitted[-1].kind is MessageKind.LEASE_CONFLICT
    assert emitted[-1].payload == {"error_type": "StaleLeaseEpoch"}


@pytest.mark.parametrize("lease_epoch", [1.9, True, "1"])
def test_lease_epoch_requires_an_exact_positive_integer_before_driver_call(
    lease_epoch: object,
) -> None:
    driver = FakeDriver()
    emitted = []
    worker = ControllerWorker(7, driver, emit=emitted.append, clock=Clock())
    sender = MessageSender(7)
    worker.handle_message(
        sender.create(MessageKind.GRANT_LEASE, owner_id=str(OWNER_A), ttl_s=2.0)
    )

    with pytest.raises(ValueError, match="lease_epoch"):
        worker.handle_message(
            sender.create(
                MessageKind.EXECUTE_PHASE,
                owner_id=str(OWNER_A),
                lease_epoch=lease_epoch,
                point=FaultPoint.AFTER_CAPTURE_CHECKPOINT.value,
                barrier_mode=BarrierMode.CRASH.value,
            )
        )

    assert driver.phases == []


@pytest.mark.parametrize("operation_id", [0, -1, True])
def test_operation_id_requires_an_exact_positive_integer_before_driver_call(
    operation_id: object,
) -> None:
    driver = FakeDriver()
    emitted = []
    worker = ControllerWorker(7, driver, emit=emitted.append, clock=Clock())
    sender = MessageSender(7)
    worker.handle_message(
        sender.create(MessageKind.GRANT_LEASE, owner_id=str(OWNER_A), ttl_s=2.0)
    )

    with pytest.raises(ValueError, match="operation_id"):
        worker.handle_message(
            sender.create(
                MessageKind.EXECUTE_PHASE,
                owner_id=str(OWNER_A),
                lease_epoch=1,
                operation_id=operation_id,
                point=FaultPoint.AFTER_CAPTURE_CHECKPOINT.value,
                barrier_mode=BarrierMode.CRASH.value,
            )
        )

    assert driver.phases == []


def test_operation_result_emits_only_safe_phase_and_operation_metadata() -> None:
    driver = SecretBearingDriver()
    emitted = []
    worker = ControllerWorker(7, driver, emit=emitted.append, clock=Clock())
    sender = MessageSender(7)
    worker.handle_message(
        sender.create(MessageKind.GRANT_LEASE, owner_id=str(OWNER_A), ttl_s=2.0)
    )

    worker.handle_message(
        sender.create(
            MessageKind.EXECUTE_PHASE,
            owner_id=str(OWNER_A),
            lease_epoch=1,
            operation_id=9,
            point=FaultPoint.AFTER_CAPTURE_CHECKPOINT.value,
            barrier_mode=BarrierMode.CRASH.value,
        )
    )

    result = emitted[-1]
    serialized = pickle.dumps(result)
    assert result.kind is MessageKind.OPERATION_RESULT
    assert result.payload == {
        "phase": FaultPoint.AFTER_CAPTURE_CHECKPOINT.value,
        "operation_id": 9,
    }
    assert SECRET_BSL.encode() not in serialized
    assert b"SecretLiveValue" not in serialized


def test_status_result_emits_only_allowlisted_state_and_lease_metadata() -> None:
    driver = SecretBearingDriver()
    emitted = []
    worker = ControllerWorker(7, driver, emit=emitted.append, clock=Clock())
    sender = MessageSender(7)
    worker.handle_message(
        sender.create(MessageKind.GRANT_LEASE, owner_id=str(OWNER_A), ttl_s=2.0)
    )

    worker.handle_message(sender.create(MessageKind.STATUS, observer_id="reader"))

    status = emitted[-1]
    serialized = pickle.dumps(status)
    assert status.kind is MessageKind.STATUS_RESULT
    assert status.payload == {
        "state": "captured",
        "lease_epoch": 1,
        "expires_in_s": 2.0,
        "lease_expired": False,
    }
    assert SECRET_BSL.encode() not in serialized
    assert b"SecretLiveValue" not in serialized


def test_status_maps_an_unrecognized_driver_state_to_safe_metadata() -> None:
    emitted = []
    worker = ControllerWorker(
        7,
        SecretStateDriver(),
        emit=emitted.append,
        clock=Clock(),
    )

    worker.handle_message(MessageSender(7).create(MessageKind.STATUS))

    serialized = pickle.dumps(emitted[-1])
    assert emitted[-1].payload["state"] == "unknown"
    assert SECRET_BSL.encode() not in serialized


def test_expiry_is_published_once_and_post_expiry_execute_is_non_mutating() -> None:
    clock = Clock()
    driver = FakeDriver()
    emitted = []
    worker = ControllerWorker(7, driver, emit=emitted.append, clock=clock)
    sender = MessageSender(7)
    worker.handle_message(
        sender.create(MessageKind.GRANT_LEASE, owner_id=str(OWNER_A), ttl_s=2.0)
    )
    clock.value = 102.0

    worker.poll_once()
    worker.poll_once()
    worker.handle_message(
        sender.create(
            MessageKind.EXECUTE_PHASE,
            owner_id=str(OWNER_A),
            lease_epoch=1,
            point=FaultPoint.AFTER_CAPTURE_CHECKPOINT.value,
            barrier_mode=BarrierMode.CRASH.value,
        )
    )

    assert [message.kind for message in emitted].count(MessageKind.LEASE_EXPIRED) == 1
    assert emitted[-1].kind is MessageKind.LEASE_CONFLICT
    assert emitted[-1].payload == {"error_type": "LeaseExpired"}
    assert driver.phases == []


def test_barrier_flushes_and_publishes_before_waiting() -> None:
    events: list[str] = []
    barrier = PhaseBarrier(
        point=FaultPoint.AFTER_FIRST_ROOT_WRITE,
        publish=lambda kind, payload: events.append(kind.value),
        flush=lambda: events.append("flushed"),
        wait=lambda: events.append("waiting"),
    )
    barrier(FaultPoint.AFTER_CAPTURE_CHECKPOINT)
    barrier(FaultPoint.AFTER_FIRST_ROOT_WRITE)
    assert events == ["flushed", "phase_reached", "waiting"]


def test_hang_barrier_disables_heartbeat_after_phase_publication() -> None:
    events: list[str] = []

    def wait(*, heartbeat_enabled: bool) -> None:
        events.append(f"waiting:{heartbeat_enabled}")
        if heartbeat_enabled:
            events.append("heartbeat")

    barrier = PhaseBarrier(
        point=FaultPoint.AFTER_CONTINUE_ACK,
        mode=BarrierMode.HANG,
        publish=lambda kind, payload: events.append(kind.value),
        flush=lambda: events.append("flushed"),
        wait=wait,
    )

    barrier(FaultPoint.AFTER_CONTINUE_ACK)

    assert events == ["flushed", "phase_reached", "waiting:False"]


def test_pipe_run_bootstraps_handles_shutdown_and_closes_driver() -> None:
    parent, child = Pipe(duplex=True)
    driver = FakeDriver()
    sender = MessageSender(7)
    parent.send(sender.create(MessageKind.DEBUGGEE_STARTED))
    parent.send(sender.create(MessageKind.SHUTDOWN))
    worker = ControllerWorker(7, driver, connection=child, clock=monotonic)

    try:
        worker.run()
        received = []
        while parent.poll():
            received.append(parent.recv())
    finally:
        parent.close()
        child.close()

    assert [message.kind for message in received] == [
        MessageKind.DEBUG_READY,
        MessageKind.READY,
    ]
    assert driver.prepared is True
    assert driver.attached is True
    assert driver.closed is True


def test_shutdown_at_phase_barrier_unwinds_before_driver_can_continue() -> None:
    parent, child = Pipe(duplex=True)
    emitted = []
    driver = PostBarrierEffectDriver()
    sender = MessageSender(7)
    parent.send(sender.create(MessageKind.DEBUGGEE_STARTED))
    parent.send(
        sender.create(
            MessageKind.GRANT_LEASE,
            owner_id=str(OWNER_A),
            ttl_s=2.0,
        )
    )
    parent.send(
        sender.create(
            MessageKind.EXECUTE_PHASE,
            owner_id=str(OWNER_A),
            lease_epoch=1,
            operation_id=1,
            point=FaultPoint.AFTER_CAPTURE_CHECKPOINT.value,
            barrier_mode=BarrierMode.CRASH.value,
        )
    )
    parent.send(sender.create(MessageKind.SHUTDOWN))
    worker = ControllerWorker(
        7,
        driver,
        connection=child,
        emit=emitted.append,
        clock=monotonic,
    )

    try:
        worker.run()
    finally:
        parent.close()
        child.close()

    assert driver.post_barrier_effects == []
    assert driver.closed is True
    assert not any(message.kind is MessageKind.OPERATION_RESULT for message in emitted)


class AdvancingConnection:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.poll_timeouts: list[float] = []

    def poll(self, timeout_s: float) -> bool:
        self.poll_timeouts.append(timeout_s)
        self.clock.value += timeout_s
        return False

    def recv(self):  # type: ignore[no-untyped-def]
        raise AssertionError("recv must not be called without a pending message")

    def send(self, message) -> None:  # type: ignore[no-untyped-def]
        raise AssertionError("explicit emit callback is used")


def test_debuggee_start_wait_is_ninety_seconds_and_fatal_is_sanitized() -> None:
    clock = Clock()
    emitted = []
    driver = FakeDriver()
    connection = AdvancingConnection(clock)
    worker = ControllerWorker(
        7,
        driver,
        connection=connection,  # type: ignore[arg-type]
        emit=emitted.append,
        clock=clock,
    )

    with pytest.raises(CommandTimeout, match="debuggee"):
        worker.run()

    assert clock.value == pytest.approx(190.0)
    assert all(
        timeout == pytest.approx(0.05) for timeout in connection.poll_timeouts
    )
    assert emitted[-1].kind is MessageKind.FATAL
    assert emitted[-1].payload == {"error_type": "CommandTimeout"}
    assert driver.closed is True


def test_unhandled_driver_error_reports_type_without_raw_bsl_data() -> None:
    class SensitiveRuntimeFailure(RuntimeError):
        pass

    class FailingDriver(FakeDriver):
        def attach_runtime(self) -> None:
            raise SensitiveRuntimeFailure("source=Сообщить(secret), value=secret")

    parent, child = Pipe(duplex=True)
    emitted = []
    driver = FailingDriver()
    parent.send(MessageSender(7).create(MessageKind.DEBUGGEE_STARTED))
    worker = ControllerWorker(
        7,
        driver,
        connection=child,
        emit=emitted.append,
        clock=monotonic,
    )

    try:
        with pytest.raises(SensitiveRuntimeFailure):
            worker.run()
    finally:
        parent.close()
        child.close()

    assert emitted[-1].kind is MessageKind.FATAL
    assert emitted[-1].payload == {"error_type": "SensitiveRuntimeFailure"}
    assert driver.closed is True


def test_crash_barrier_keeps_heartbeat_while_awaiting_parent_kill() -> None:
    parent, child = Pipe(duplex=True)
    emitted = []
    driver = BarrierDriver()
    worker = ControllerWorker(
        7,
        driver,
        connection=child,
        emit=emitted.append,
        clock=monotonic,
    )
    sender = MessageSender(7)
    worker.handle_message(
        sender.create(MessageKind.GRANT_LEASE, owner_id=str(OWNER_A), ttl_s=2.0)
    )
    execute = sender.create(
        MessageKind.EXECUTE_PHASE,
        owner_id=str(OWNER_A),
        lease_epoch=1,
        point=FaultPoint.AFTER_CAPTURE_CHECKPOINT.value,
        barrier_mode=BarrierMode.CRASH.value,
    )
    shutdown = sender.create(MessageKind.SHUTDOWN)
    timer = Timer(0.35, parent.send, args=(shutdown,))
    timer.start()

    try:
        worker.handle_message(execute)
    finally:
        timer.join(timeout=1.0)
        parent.close()
        child.close()

    phase_index = next(
        index
        for index, message in enumerate(emitted)
        if message.kind is MessageKind.PHASE_REACHED
    )
    assert any(
        message.kind is MessageKind.HEARTBEAT for message in emitted[phase_index + 1 :]
    )
    assert not any(
        message.kind is MessageKind.OPERATION_RESULT for message in emitted
    )


def test_lease_expiry_barrier_accepts_renewal_until_terminal_expiry() -> None:
    parent, child = Pipe(duplex=True)
    clock = Clock()
    emitted = []
    driver = BarrierDriver()
    worker = ControllerWorker(
        7,
        driver,
        connection=child,
        emit=emitted.append,
        clock=clock,
    )
    sender = MessageSender(7)
    worker.handle_message(
        sender.create(MessageKind.GRANT_LEASE, owner_id=str(OWNER_A), ttl_s=2.0)
    )
    execute = sender.create(
        MessageKind.EXECUTE_PHASE,
        owner_id=str(OWNER_A),
        lease_epoch=1,
        point=FaultPoint.AFTER_CAPTURE_CHECKPOINT.value,
        barrier_mode=BarrierMode.LEASE_EXPIRY.value,
    )
    parent.send(
        sender.create(
            MessageKind.RENEW_LEASE,
            owner_id=str(OWNER_A),
            lease_epoch=1,
            ttl_s=3.0,
        )
    )
    shutdown = sender.create(MessageKind.SHUTDOWN)
    expire = Timer(0.10, setattr, args=(clock, "value", 103.0))
    stop = Timer(0.20, parent.send, args=(shutdown,))
    expire.start()
    stop.start()

    try:
        worker.handle_message(execute)
    finally:
        expire.join(timeout=1.0)
        stop.join(timeout=1.0)
        parent.close()
        child.close()

    kinds = [message.kind for message in emitted]
    assert kinds.count(MessageKind.LEASE_GRANTED) == 2
    assert MessageKind.LEASE_EXPIRED in kinds
    assert kinds.index(MessageKind.LEASE_GRANTED) < kinds.index(MessageKind.LEASE_EXPIRED)
    assert MessageKind.OPERATION_RESULT not in kinds


def test_phase_evidence_is_flushed_before_publication() -> None:
    observations: list[tuple[MessageKind, int]] = []
    journal = RecoveryJournal()
    journal.record("operation-events.jsonl", "checkpoint_complete")
    barrier = PhaseBarrier(
        point=FaultPoint.AFTER_CAPTURE_CHECKPOINT,
        publish=lambda kind, payload: observations.append(
            (kind, journal.pending_count)
        ),
        flush=journal.flush,
        wait=lambda: None,
    )

    barrier(FaultPoint.AFTER_CAPTURE_CHECKPOINT)

    assert observations == [(MessageKind.PHASE_REACHED, 0)]
