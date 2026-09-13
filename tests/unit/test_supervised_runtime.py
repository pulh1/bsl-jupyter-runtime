from __future__ import annotations

from hashlib import sha256
import json
import multiprocessing
from multiprocessing.connection import Connection
from pathlib import Path
from threading import Event, Thread, Timer
from time import monotonic
from uuid import UUID

import pytest

import onec_runtime.supervisor as supervisor_module
from onec_runtime.artifacts import ExistingArtifactSink
from onec_runtime.controller_worker import (
    BarrierMode,
    ControllerWorker,
    PhaseBarrier,
)
from onec_runtime.errors import (
    CommandTimeout,
    ControllerUnavailable,
    InvalidMessageSequence,
    LeaseConflict,
    LeaseExpired,
    ProcessStartError,
    StaleRuntimeGeneration,
)
from onec_runtime.fault_injection import FaultPoint
from onec_runtime.recovery_journal import RecoveryJournal
from onec_runtime.supervised_runtime import SupervisedRuntime
from onec_runtime.supervisor_model import GenerationLifecycle, TerminationCause
from onec_runtime.supervisor_protocol import ControlMessage, MessageKind


OWNER_A = UUID("11111111-1111-1111-1111-111111111111")
OWNER_B = UUID("22222222-2222-2222-2222-222222222222")


class SyntheticRuntimeDriver:
    def prepare_debug_ui(self) -> None:
        return None

    def attach_runtime(self) -> None:
        return None

    def execute_phase(self, point: FaultPoint, barrier: PhaseBarrier) -> object:
        barrier(point)
        return {"phase": point.value}

    def status(self) -> dict[str, object]:
        return {"state": "captured"}

    def close(self) -> None:
        return None


def synthetic_worker_main(generation_id: int, connection: Connection) -> None:
    ControllerWorker(
        generation_id,
        SyntheticRuntimeDriver(),
        connection=connection,
    ).run()


def wrong_startup_worker_main(generation_id: int, connection: Connection) -> None:
    from onec_runtime.supervisor_protocol import MessageSender

    connection.send(MessageSender(generation_id).create(MessageKind.READY))
    Event().wait()


def attach_fatal_worker_main(generation_id: int, connection: Connection) -> None:
    from onec_runtime.supervisor_protocol import MessageSender

    sender = MessageSender(generation_id)
    connection.send(sender.create(MessageKind.DEBUG_READY))
    connection.recv()
    connection.send(
        sender.create(MessageKind.FATAL, error_type="SyntheticAttachFailure")
    )
    Event().wait()


def exit_on_status_worker_main(generation_id: int, connection: Connection) -> None:
    from onec_runtime.supervisor_protocol import MessageSender

    sender = MessageSender(generation_id)
    connection.send(sender.create(MessageKind.DEBUG_READY))
    connection.recv()
    connection.send(sender.create(MessageKind.READY))
    request = connection.recv()
    if request.kind is not MessageKind.STATUS:
        raise RuntimeError("Synthetic worker expected STATUS")


class SyntheticGenerationProcesses:
    DBGS_PID = 41001
    ONEC_PID = 41002

    def __init__(
        self,
        generation_id: int,
        worker_target: object,
    ) -> None:
        self._generation_id = generation_id
        self._worker_target = worker_target
        self._context = multiprocessing.get_context("spawn")
        self._parent_connection, self._child_connection = self._context.Pipe()
        self._controller: multiprocessing.Process | None = None
        self._controller_exit_code: int | None = None
        self._dbgs_alive = False
        self._onec_alive = False

    @property
    def controller_pid(self) -> int | None:
        process = self._controller
        if process is None or not process.is_alive():
            return None
        return process.pid

    @property
    def dbgs_pid(self) -> int | None:
        return self.DBGS_PID if self._dbgs_alive else None

    @property
    def onec_pid(self) -> int | None:
        return self.ONEC_PID if self._onec_alive else None

    def start_debug_server(self) -> int:
        self._dbgs_alive = True
        return 1550

    def start_controller(self, debug_port: int, run_dir: Path) -> None:
        del debug_port, run_dir
        self._controller = self._context.Process(
            target=self._worker_target,
            args=(self._generation_id, self._child_connection),
            name=f"synthetic-controller-{self._generation_id}",
        )
        try:
            self._controller.start()
        finally:
            self._child_connection.close()

    def start_debuggee(self, debug_port: int) -> None:
        del debug_port
        self._onec_alive = True

    def send(self, message: ControlMessage) -> None:
        self._parent_connection.send(message)

    def receive(self, timeout_s: float) -> ControlMessage:
        if not self._parent_connection.poll(timeout_s):
            raise CommandTimeout(
                f"Controller did not publish a message within {timeout_s} seconds"
            )
        return self._parent_connection.recv()

    def controller_exitcode(self) -> int | None:
        process = self._controller
        if process is None:
            return self._controller_exit_code
        return process.exitcode

    def terminate_controller(self, timeout_s: float) -> None:
        process = self._controller
        if process is not None:
            if process.is_alive():
                process.terminate()
                process.join(timeout_s)
            if process.is_alive():
                process.kill()
                process.join(timeout_s)
            self._controller_exit_code = process.exitcode
            process.close()
            self._controller = None
        try:
            self._parent_connection.close()
        except OSError:
            pass

    def terminate_onec(self, timeout_s: float) -> None:
        del timeout_s
        self._onec_alive = False

    def terminate_dbgs(self, timeout_s: float) -> None:
        del timeout_s
        self._dbgs_alive = False

    def all_stopped(self) -> bool:
        return (
            self.controller_exitcode() is not None
            and not self._onec_alive
            and not self._dbgs_alive
        )


class CountingSyntheticProcesses(SyntheticGenerationProcesses):
    def __init__(self, generation_id: int, worker_target: object) -> None:
        super().__init__(generation_id, worker_target)
        self.receive_calls = 0

    def receive(self, timeout_s: float) -> ControlMessage:
        self.receive_calls += 1
        return super().receive(timeout_s)


class PidGuardedSyntheticProcesses(SyntheticGenerationProcesses):
    @property
    def onec_pid(self) -> int | None:
        if not self._onec_alive:
            raise AssertionError("A failed startup must not activate the generation")
        return super().onec_pid


class StartupCleanupFailureProcesses(SyntheticGenerationProcesses):
    def terminate_dbgs(self, timeout_s: float) -> None:
        del timeout_s

    def force_cleanup(self) -> None:
        super().terminate_controller(2.0)
        super().terminate_onec(0.0)
        super().terminate_dbgs(0.0)


class NonStoppingSyntheticProcesses(SyntheticGenerationProcesses):
    def __init__(self, generation_id: int, worker_target: object) -> None:
        super().__init__(generation_id, worker_target)
        self.controller_termination_calls = 0

    def terminate_controller(self, timeout_s: float) -> None:
        self.controller_termination_calls += 1
        super().terminate_controller(timeout_s)

    def terminate_onec(self, timeout_s: float) -> None:
        del timeout_s

    def terminate_dbgs(self, timeout_s: float) -> None:
        del timeout_s

    def force_cleanup(self) -> None:
        super().terminate_controller(2.0)
        super().terminate_onec(0.0)
        super().terminate_dbgs(0.0)


class SlowCleanupSyntheticProcesses(SyntheticGenerationProcesses):
    def __init__(self, generation_id: int, worker_target: object) -> None:
        super().__init__(generation_id, worker_target)
        self._controller_termination_calls = 0

    def terminate_controller(self, timeout_s: float) -> None:
        self._controller_termination_calls += 1
        if self._controller_termination_calls > 1:
            Event().wait(0.8)
        super().terminate_controller(timeout_s)


class BlockingCleanupSyntheticProcesses(SyntheticGenerationProcesses):
    def __init__(self, generation_id: int, worker_target: object) -> None:
        super().__init__(generation_id, worker_target)
        self._controller_termination_calls = 0
        self.cleanup_entered = Event()
        self.release_cleanup = Event()

    def terminate_controller(self, timeout_s: float) -> None:
        self._controller_termination_calls += 1
        if self._controller_termination_calls == 2:
            self.cleanup_entered.set()
            assert self.release_cleanup.wait(timeout=2.0)
        super().terminate_controller(timeout_s)


class RetryingCleanupSyntheticProcesses(SyntheticGenerationProcesses):
    def __init__(self, generation_id: int, worker_target: object) -> None:
        super().__init__(generation_id, worker_target)
        self.calls: list[str] = []
        self._onec_termination_calls = 0

    def terminate_controller(self, timeout_s: float) -> None:
        self.calls.append("controller")
        super().terminate_controller(timeout_s)

    def terminate_onec(self, timeout_s: float) -> None:
        self.calls.append("onec")
        self._onec_termination_calls += 1
        if self._onec_termination_calls == 1:
            raise RuntimeError("synthetic 1C cleanup failure")
        super().terminate_onec(timeout_s)

    def terminate_dbgs(self, timeout_s: float) -> None:
        self.calls.append("dbgs")
        super().terminate_dbgs(timeout_s)


class DeadlineCleanupProcesses:
    def __init__(self) -> None:
        self.controller_pid: int | None = 51001
        self.dbgs_pid: int | None = 51002
        self.onec_pid: int | None = 51003
        self.calls: list[str] = []

    def controller_exitcode(self) -> int | None:
        return None

    def terminate_controller(self, timeout_s: float) -> None:
        del timeout_s
        self.calls.append("controller")
        self.controller_pid = None

    def terminate_onec(self, timeout_s: float) -> None:
        del timeout_s
        self.calls.append("onec")
        self.onec_pid = None

    def terminate_dbgs(self, timeout_s: float) -> None:
        del timeout_s
        self.calls.append("dbgs")
        self.dbgs_pid = None

    def all_stopped(self) -> bool:
        return self.controller_pid is self.dbgs_pid is self.onec_pid is None


def synthetic_runtime(
    tmp_path: Path,
    *,
    heartbeat_timeout_s: float = 1.5,
    lease_ttl_s: float = 2.0,
) -> SupervisedRuntime:
    journal = RecoveryJournal(
        lambda name, value: ExistingArtifactSink(tmp_path).append_jsonl(name, value)
    )
    return SupervisedRuntime(
        processes_factory=lambda generation_id: SyntheticGenerationProcesses(
            generation_id,
            synthetic_worker_main,
        ),
        journal=journal,
        heartbeat_timeout_s=heartbeat_timeout_s,
        lease_ttl_s=lease_ttl_s,
    )


def test_invalid_startup_message_fails_closed_without_activation(
    tmp_path: Path,
) -> None:
    created: list[PidGuardedSyntheticProcesses] = []

    def processes_factory(generation_id: int) -> PidGuardedSyntheticProcesses:
        processes = PidGuardedSyntheticProcesses(
            generation_id,
            wrong_startup_worker_main,
        )
        created.append(processes)
        return processes

    runtime = SupervisedRuntime(
        processes_factory=processes_factory,
        run_dir=tmp_path,
    )
    try:
        with pytest.raises(InvalidMessageSequence, match="debug_ready"):
            runtime.start()

        terminal = runtime.wait_terminated(timeout_s=1.0)

        assert terminal.termination_cause is TerminationCause.STARTUP_FAILED
        assert created[0].all_stopped()
    finally:
        if created:
            created[0].terminate_controller(2.0)
            created[0].terminate_onec(0.0)
            created[0].terminate_dbgs(0.0)


def test_startup_cleanup_failure_preserves_original_error(tmp_path: Path) -> None:
    created: list[StartupCleanupFailureProcesses] = []

    def processes_factory(generation_id: int) -> StartupCleanupFailureProcesses:
        processes = StartupCleanupFailureProcesses(
            generation_id,
            wrong_startup_worker_main,
        )
        created.append(processes)
        return processes

    runtime = SupervisedRuntime(processes_factory=processes_factory, run_dir=tmp_path)
    try:
        with pytest.raises(InvalidMessageSequence, match="debug_ready"):
            runtime.start()

        with pytest.raises(StaleRuntimeGeneration):
            runtime.status("observer")
    finally:
        if created:
            created[0].force_cleanup()


def test_attach_fatal_is_typed_and_fails_startup_closed(tmp_path: Path) -> None:
    runtime = SupervisedRuntime(
        processes_factory=lambda generation_id: SyntheticGenerationProcesses(
            generation_id,
            attach_fatal_worker_main,
        ),
        run_dir=tmp_path,
    )
    try:
        with pytest.raises(
            ControllerUnavailable,
            match="SyntheticAttachFailure",
        ):
            runtime.start()

        terminal = runtime.wait_terminated(timeout_s=1.0)

        assert terminal.termination_cause is TerminationCause.STARTUP_FAILED
    finally:
        runtime.close()


def test_process_factory_failure_is_fenced_before_the_next_start(
    tmp_path: Path,
) -> None:
    created: list[SyntheticGenerationProcesses] = []
    attempts = 0

    def processes_factory(generation_id: int) -> SyntheticGenerationProcesses:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ProcessStartError("synthetic adapter construction failed")
        processes = SyntheticGenerationProcesses(
            generation_id,
            synthetic_worker_main,
        )
        created.append(processes)
        return processes

    runtime = SupervisedRuntime(processes_factory=processes_factory, run_dir=tmp_path)
    try:
        with pytest.raises(ProcessStartError, match="adapter construction"):
            runtime.start()

        failed = runtime.wait_terminated(timeout_s=0.1)

        replacement = runtime.start()

        assert failed.termination_cause is TerminationCause.STARTUP_FAILED
        assert replacement.generation_id == 2
    finally:
        runtime.close()


def test_termination_timeout_keeps_the_generation_fenced(tmp_path: Path) -> None:
    created: list[NonStoppingSyntheticProcesses] = []

    def processes_factory(generation_id: int) -> NonStoppingSyntheticProcesses:
        processes = NonStoppingSyntheticProcesses(
            generation_id,
            synthetic_worker_main,
        )
        created.append(processes)
        return processes

    runtime = SupervisedRuntime(processes_factory=processes_factory, run_dir=tmp_path)
    try:
        runtime.start()
        runtime.kill_controller()
        started = monotonic()

        with pytest.raises(CommandTimeout):
            runtime.wait_terminated(timeout_s=0.2)

        assert 0.2 <= monotonic() - started <= 0.6
        assert created[0].controller_termination_calls <= 6
        with pytest.raises(StaleRuntimeGeneration):
            runtime.status("observer")
    finally:
        if created:
            created[0].force_cleanup()


@pytest.mark.timeout(3)
def test_cleanup_retries_stop_when_the_generation_deadline_is_exhausted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(supervisor_module, "TERMINATION_BOUND_S", 0.05)
    created: list[NonStoppingSyntheticProcesses] = []

    def processes_factory(generation_id: int) -> NonStoppingSyntheticProcesses:
        processes = NonStoppingSyntheticProcesses(
            generation_id,
            synthetic_worker_main,
        )
        created.append(processes)
        return processes

    runtime = SupervisedRuntime(processes_factory=processes_factory, run_dir=tmp_path)
    try:
        runtime.start()
        runtime.kill_controller()

        with pytest.raises(CommandTimeout):
            runtime.wait_terminated(timeout_s=0.2)

        assert created[0].controller_termination_calls == 2
    finally:
        if created:
            created[0].force_cleanup()


def test_wait_timeout_is_bounded_while_cleanup_continues(tmp_path: Path) -> None:
    runtime = SupervisedRuntime(
        processes_factory=lambda generation_id: SlowCleanupSyntheticProcesses(
            generation_id,
            synthetic_worker_main,
        ),
        run_dir=tmp_path,
    )
    try:
        runtime.start()
        runtime.kill_controller()
        started = monotonic()

        with pytest.raises(CommandTimeout):
            runtime.wait_terminated(timeout_s=0.2)

        assert 0.2 <= monotonic() - started <= 0.6
        terminal = runtime.wait_terminated(timeout_s=2.0)
        assert terminal.termination_cause is TerminationCause.CONTROLLER_EXIT
    finally:
        runtime.close()


def test_close_does_not_replace_a_failed_cleanup_owner_after_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processes = DeadlineCleanupProcesses()
    runtime = SupervisedRuntime(lambda generation_id: processes)
    generation = runtime._supervisor.begin_generation()
    runtime._supervisor.activate(generation)
    runtime._supervisor.begin_termination(TerminationCause.REQUESTED)
    previous_owner = Thread()
    previous_owner.start()
    previous_owner.join()
    runtime._termination_thread = previous_owner
    runtime._termination_error = RuntimeError("previous cleanup failed")
    monkeypatch.setattr(
        runtime._supervisor,
        "termination_time_remaining",
        lambda: 0.0,
    )

    with pytest.raises(CommandTimeout, match="cleanup"):
        runtime.close()

    assert runtime._termination_thread is previous_owner
    assert processes.calls == []
    assert runtime._supervisor.status().lifecycle is GenerationLifecycle.TERMINATING


def test_close_does_not_replace_owner_when_deadline_expires_before_owner_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processes = DeadlineCleanupProcesses()
    runtime = SupervisedRuntime(lambda generation_id: processes)
    generation = runtime._supervisor.begin_generation()
    runtime._supervisor.activate(generation)
    runtime._supervisor.begin_termination(TerminationCause.REQUESTED)
    previous_owner = Thread()
    previous_owner.start()
    previous_owner.join()
    runtime._termination_thread = previous_owner
    runtime._termination_error = RuntimeError("previous cleanup failed")
    remaining = iter((1.0, 0.0, 0.0))
    monkeypatch.setattr(
        runtime._supervisor,
        "termination_time_remaining",
        lambda: next(remaining),
    )

    with pytest.raises(CommandTimeout, match="cleanup"):
        runtime.close()

    assert runtime._termination_thread is previous_owner
    assert processes.calls == []
    assert runtime._supervisor.status().lifecycle is GenerationLifecycle.TERMINATING


def test_close_retries_one_failed_cleanup_owner_before_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processes = DeadlineCleanupProcesses()
    runtime = SupervisedRuntime(lambda generation_id: processes)
    generation = runtime._supervisor.begin_generation()
    runtime._supervisor.activate(generation)
    runtime._supervisor.begin_termination(TerminationCause.REQUESTED)
    previous_owner = Thread()
    previous_owner.start()
    previous_owner.join()
    runtime._termination_thread = previous_owner
    runtime._termination_error = RuntimeError("previous cleanup failed")
    monkeypatch.setattr(
        runtime._supervisor,
        "termination_time_remaining",
        lambda: 1.0,
    )

    runtime.close()

    assert runtime._termination_thread is not previous_owner
    assert processes.calls == ["controller", "onec", "dbgs"]
    assert runtime._supervisor.status().lifecycle is GenerationLifecycle.TERMINATED


@pytest.mark.timeout(3)
def test_close_does_not_take_cleanup_ownership_after_its_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(supervisor_module, "TERMINATION_BOUND_S", 0.05)
    created: list[BlockingCleanupSyntheticProcesses] = []

    def processes_factory(generation_id: int) -> BlockingCleanupSyntheticProcesses:
        processes = BlockingCleanupSyntheticProcesses(
            generation_id,
            synthetic_worker_main,
        )
        created.append(processes)
        return processes

    runtime = SupervisedRuntime(processes_factory=processes_factory, run_dir=tmp_path)
    runtime.start()
    runtime.kill_controller()
    runtime._poll_supervisor()
    assert created[0].cleanup_entered.wait(timeout=1.0)
    delayed_release = Timer(0.3, created[0].release_cleanup.set)
    delayed_release.start()
    started = monotonic()

    try:
        with pytest.raises(CommandTimeout, match="cleanup"):
            runtime.close()
        elapsed = monotonic() - started
        assert elapsed <= 0.2
    finally:
        created[0].release_cleanup.set()
        delayed_release.join(timeout=1.0)

    terminal = runtime.wait_terminated(timeout_s=2.0)
    assert terminal.termination_cause is TerminationCause.CONTROLLER_EXIT


def test_partial_cleanup_continues_in_order_and_retries_to_terminal(
    tmp_path: Path,
) -> None:
    created: list[RetryingCleanupSyntheticProcesses] = []

    def processes_factory(generation_id: int) -> RetryingCleanupSyntheticProcesses:
        processes = RetryingCleanupSyntheticProcesses(
            generation_id,
            synthetic_worker_main,
        )
        created.append(processes)
        return processes

    runtime = SupervisedRuntime(processes_factory=processes_factory, run_dir=tmp_path)
    try:
        runtime.start()
        runtime.kill_controller()

        terminal = runtime.wait_terminated(timeout_s=2.0)

        assert terminal.termination_cause is TerminationCause.CONTROLLER_EXIT
        assert created[0].calls == [
            "controller",
            "controller",
            "onec",
            "dbgs",
            "controller",
            "onec",
            "dbgs",
        ]
        assert created[0].all_stopped()
    finally:
        runtime.close()


def test_real_controller_exit_terminates_and_fences_old_handle(tmp_path: Path) -> None:
    runtime = synthetic_runtime(tmp_path)
    try:
        first = runtime.start()
        lease = runtime.grant_lease(first, OWNER_A)
        runtime.kill_controller()
        terminal = runtime.wait_terminated(timeout_s=5.0)
        second = runtime.start()

        assert terminal.termination_cause is TerminationCause.CONTROLLER_EXIT
        assert second.generation_id > first.generation_id
        with pytest.raises(StaleRuntimeGeneration):
            runtime.execute_phase(lease, FaultPoint.AFTER_CAPTURE_CHECKPOINT)
    finally:
        runtime.close()


def test_rejected_start_preserves_the_active_generation_facade(tmp_path: Path) -> None:
    runtime = synthetic_runtime(tmp_path)
    try:
        generation = runtime.start()

        with pytest.raises(RuntimeError, match="ACTIVE"):
            runtime.start()

        status = runtime.status("observer-after-rejected-start")
        runtime.kill_controller()
        terminal = runtime.wait_terminated(timeout_s=5.0)

        assert status.kind is MessageKind.STATUS_RESULT
        assert status.payload["state"] == "captured"
        assert terminal.handle == generation
        assert terminal.termination_cause is TerminationCause.CONTROLLER_EXIT
    finally:
        runtime.close()


def test_response_wait_fences_controller_loss_and_aborts_promptly(
    tmp_path: Path,
) -> None:
    created: list[CountingSyntheticProcesses] = []

    def processes_factory(generation_id: int) -> CountingSyntheticProcesses:
        processes = CountingSyntheticProcesses(
            generation_id,
            exit_on_status_worker_main,
        )
        created.append(processes)
        return processes

    runtime = SupervisedRuntime(processes_factory=processes_factory, run_dir=tmp_path)
    try:
        runtime.start()
        calls_before = created[0].receive_calls
        runtime._send(MessageKind.STATUS, observer_id="controller-loss")
        started = monotonic()

        with pytest.raises(ControllerUnavailable, match="controller_exit"):
            runtime._wait_for_response(
                MessageKind.STATUS_RESULT,
                timeout_s=0.5,
            )

        elapsed = monotonic() - started
        assert 0.05 <= elapsed <= 0.3
        assert created[0].receive_calls - calls_before <= 3
        terminal = runtime.wait_terminated(timeout_s=2.0)
        assert terminal.termination_cause is TerminationCause.CONTROLLER_EXIT
    finally:
        runtime.close()


def test_rival_lease_response_is_raised_as_typed_conflict(tmp_path: Path) -> None:
    runtime = synthetic_runtime(tmp_path)
    try:
        generation = runtime.start()
        runtime.grant_lease(generation, OWNER_A)

        with pytest.raises(LeaseConflict):
            runtime.grant_lease(generation, OWNER_B)
    finally:
        runtime.close()


def test_invalid_ttl_is_rejected_before_supervisor_sends_a_lease_command(
    tmp_path: Path,
) -> None:
    runtime = synthetic_runtime(tmp_path)
    try:
        generation = runtime.start()

        with pytest.raises(ValueError, match="ttl_s"):
            runtime.grant_lease(generation, OWNER_A, ttl_s=True)

        lease = runtime.grant_lease(generation, OWNER_B, ttl_s=95.0)
        assert lease.lease_epoch == 1
    finally:
        runtime.close()


def test_observer_status_requires_no_lease(tmp_path: Path) -> None:
    runtime = synthetic_runtime(tmp_path)
    try:
        runtime.start()

        status = runtime.status("read-only-observer")

        assert status.kind is MessageKind.STATUS_RESULT
        assert status.payload["state"] == "captured"
        assert status.payload["lease_epoch"] is None
    finally:
        runtime.close()


@pytest.mark.timeout(3)
def test_lease_expiry_response_is_raised_as_typed_error(tmp_path: Path) -> None:
    runtime = synthetic_runtime(tmp_path, lease_ttl_s=0.1)
    try:
        generation = runtime.start()
        lease = runtime.grant_lease(generation, OWNER_A)

        with pytest.raises(LeaseExpired):
            runtime.execute_phase(
                lease,
                FaultPoint.AFTER_CAPTURE_CHECKPOINT,
                barrier_mode=BarrierMode.LEASE_EXPIRY,
            )
    finally:
        runtime.close()


def test_lease_evidence_hashes_uuid_bytes_without_raw_owner(tmp_path: Path) -> None:
    runtime = synthetic_runtime(tmp_path)
    try:
        generation = runtime.start()
        runtime.grant_lease(generation, OWNER_A)

        evidence = (tmp_path / "supervisor.jsonl").read_text(encoding="utf-8")

        assert str(OWNER_A) not in evidence
        assert sha256(OWNER_A.bytes).hexdigest() in evidence
        assert json.loads(evidence.splitlines()[-1])["event"] == "lease_granted"
    finally:
        runtime.close()


@pytest.mark.timeout(8)
def test_hung_controller_is_killed_after_progress_timeout(tmp_path: Path) -> None:
    runtime = synthetic_runtime(tmp_path, heartbeat_timeout_s=1.5)
    try:
        generation = runtime.start()
        lease = runtime.grant_lease(generation, OWNER_A)
        runtime.execute_phase(
            lease,
            FaultPoint.AFTER_CAPTURE_CHECKPOINT,
            barrier_mode=BarrierMode.HANG,
            wait_for_result=False,
        )
        runtime.wait_for_event(MessageKind.PHASE_REACHED, timeout_s=2.0)
        started = monotonic()
        terminal = runtime.wait_terminated(timeout_s=5.0)

        assert terminal.termination_cause is TerminationCause.CONTROLLER_HUNG
        assert 1.5 <= monotonic() - started <= 4.0
    finally:
        runtime.close()
