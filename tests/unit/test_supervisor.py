from dataclasses import dataclass, field
from threading import Event, Lock, Thread
from typing import Callable

import pytest

from onec_runtime.errors import StaleRuntimeGeneration
from onec_runtime.recovery_journal import RecoveryJournal
from onec_runtime.supervisor import RuntimeSupervisor
from onec_runtime.supervisor_model import GenerationLifecycle, TerminationCause
from onec_runtime.supervisor_protocol import MessageKind, MessageSender


@dataclass
class FakeProcesses:
    controller_pid: int | None = 10
    dbgs_pid: int | None = 11
    onec_pid: int | None = 12
    exitcode: int | None = None
    stopped: bool = True
    calls: list[tuple[str, float]] = field(default_factory=list)
    on_terminate: Callable[[], None] | None = None

    def controller_exitcode(self) -> int | None:
        return self.exitcode

    def terminate_controller(self, timeout_s: float) -> None:
        self.calls.append(("controller", timeout_s))
        self.controller_pid = None
        self._after_terminate()

    def terminate_onec(self, timeout_s: float) -> None:
        self.calls.append(("onec", timeout_s))
        self.onec_pid = None
        self._after_terminate()

    def terminate_dbgs(self, timeout_s: float) -> None:
        self.calls.append(("dbgs", timeout_s))
        self.dbgs_pid = None
        self._after_terminate()

    def all_stopped(self) -> bool:
        return self.stopped and self.controller_pid is self.dbgs_pid is self.onec_pid is None

    def _after_terminate(self) -> None:
        if self.on_terminate is not None:
            self.on_terminate()


@dataclass
class FakeClock:
    now: float = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_controller_exit_terminates_generation_in_safe_order() -> None:
    processes = FakeProcesses()
    supervisor = RuntimeSupervisor(lambda generation_id: processes)
    generation = supervisor.begin_generation()
    supervisor.activate(generation)
    processes.exitcode = 99

    supervisor.poll()

    assert supervisor.status().lifecycle is GenerationLifecycle.TERMINATED
    assert processes.calls == [
        ("controller", 2.0),
        ("onec", 10.0),
        ("dbgs", 10.0),
    ]


def test_termination_is_idempotent_and_stale_request_is_fenced() -> None:
    processes = FakeProcesses()
    supervisor = RuntimeSupervisor(lambda generation_id: processes)
    old = supervisor.begin_generation()
    supervisor.activate(old)
    supervisor.terminate(TerminationCause.REQUESTED)
    supervisor.terminate(TerminationCause.CONTROLLER_EXIT)
    new = supervisor.begin_generation()
    supervisor.activate(new)

    assert len(processes.calls) == 3
    with pytest.raises(StaleRuntimeGeneration):
        supervisor.require_active(old)


def test_heartbeat_deadline_terminates_hung_controller_at_exact_boundary() -> None:
    clock = FakeClock()
    processes = FakeProcesses()
    supervisor = RuntimeSupervisor(lambda generation_id: processes, clock=clock)
    generation = supervisor.begin_generation()
    supervisor.activate(generation)
    supervisor.heartbeat(generation, progress_sequence=2, busy_deadline=None)

    clock.advance(1.49)
    supervisor.poll()

    assert supervisor.status().lifecycle is GenerationLifecycle.ACTIVE

    clock.advance(0.01)
    supervisor.poll()

    assert supervisor.status().lifecycle is GenerationLifecycle.TERMINATED
    assert supervisor.status().termination_cause is TerminationCause.CONTROLLER_HUNG


def test_activation_starts_watchdog_before_first_heartbeat() -> None:
    clock = FakeClock()
    processes = FakeProcesses()
    supervisor = RuntimeSupervisor(lambda generation_id: processes, clock=clock)
    generation = supervisor.begin_generation()
    supervisor.activate(generation)

    clock.advance(1.5)
    supervisor.poll()

    assert supervisor.status().lifecycle is GenerationLifecycle.TERMINATED
    assert supervisor.status().termination_cause is TerminationCause.CONTROLLER_HUNG


def test_busy_deadline_suppresses_heartbeat_watchdog_only_until_deadline() -> None:
    clock = FakeClock()
    processes = FakeProcesses()
    supervisor = RuntimeSupervisor(lambda generation_id: processes, clock=clock)
    generation = supervisor.begin_generation()
    supervisor.activate(generation)
    supervisor.heartbeat(generation, progress_sequence=2, busy_deadline=5.0)

    clock.advance(4.99)
    supervisor.poll()

    assert supervisor.status().lifecycle is GenerationLifecycle.ACTIVE

    clock.advance(0.01)
    supervisor.poll()

    assert supervisor.status().termination_cause is TerminationCause.CONTROLLER_HUNG


def test_late_heartbeat_from_terminated_generation_is_ignored_and_journaled() -> None:
    clock = FakeClock()
    journal = RecoveryJournal()
    processes = FakeProcesses()
    supervisor = RuntimeSupervisor(
        lambda generation_id: processes,
        clock=clock,
        journal=journal,
    )
    generation = supervisor.begin_generation()
    supervisor.activate(generation)
    supervisor.terminate(TerminationCause.REQUESTED)

    supervisor.heartbeat(generation, progress_sequence=3, busy_deadline=None)

    assert supervisor.status().lifecycle is GenerationLifecycle.TERMINATED
    assert journal.events[-1].event == "late_heartbeat_ignored"
    assert journal.events[-1].fields == {
        "generation_id": generation.generation_id,
        "progress_sequence": 3,
    }


def test_late_worker_heartbeat_from_replaced_generation_is_ignored_and_journaled() -> None:
    journal = RecoveryJournal()
    supervisor = RuntimeSupervisor(lambda generation_id: FakeProcesses(), journal=journal)
    old = supervisor.begin_generation()
    supervisor.activate(old)
    old_sender = MessageSender(old.generation_id)
    supervisor.terminate(TerminationCause.REQUESTED)
    replacement = supervisor.begin_generation()
    supervisor.activate(replacement)

    supervisor.on_worker_message(
        old_sender.create(MessageKind.HEARTBEAT, progress_sequence=3, busy_deadline=None)
    )

    assert supervisor.status().generation is not None
    assert supervisor.status().generation.handle == replacement
    assert journal.events[-1].event == "late_heartbeat_ignored"
    assert journal.events[-1].fields == {
        "generation_id": old.generation_id,
        "progress_sequence": 3,
    }


def test_worker_heartbeat_uses_sequenced_control_message() -> None:
    clock = FakeClock()
    processes = FakeProcesses()
    supervisor = RuntimeSupervisor(lambda generation_id: processes, clock=clock)
    generation = supervisor.begin_generation()
    supervisor.activate(generation)
    sender = MessageSender(generation.generation_id)

    supervisor.on_worker_message(
        sender.create(MessageKind.HEARTBEAT, progress_sequence=2, busy_deadline=None)
    )
    clock.advance(1.5)
    supervisor.poll()

    assert supervisor.status().termination_cause is TerminationCause.CONTROLLER_HUNG


def test_termination_sets_lifecycle_and_flushes_planned_evidence_before_cleanup() -> None:
    observations: list[tuple[GenerationLifecycle | None, int]] = []
    journal = RecoveryJournal()
    processes = FakeProcesses()
    supervisor = RuntimeSupervisor(lambda generation_id: processes, journal=journal)
    generation = supervisor.begin_generation()
    supervisor.activate(generation)

    processes.on_terminate = lambda: observations.append(
        (supervisor.status().lifecycle, journal.pending_count)
    )
    supervisor.terminate(TerminationCause.REQUESTED)

    assert observations[0] == (GenerationLifecycle.TERMINATING, 0)
    assert [event.event for event in journal.events] == [
        "generation_termination_planned",
        "generation_terminated",
    ]
    assert journal.pending_count == 0


def test_planned_evidence_failure_is_raised_only_after_owned_cleanup() -> None:
    append_calls = 0

    def fail_first_append(stream: str, value: object) -> None:
        nonlocal append_calls
        del stream, value
        append_calls += 1
        if append_calls == 1:
            raise OSError("synthetic evidence sink failure")

    journal = RecoveryJournal(fail_first_append)
    processes = FakeProcesses()
    supervisor = RuntimeSupervisor(lambda generation_id: processes, journal=journal)
    generation = supervisor.begin_generation()
    supervisor.activate(generation)

    with pytest.raises(OSError, match="evidence sink"):
        supervisor.terminate(TerminationCause.REQUESTED)

    assert supervisor.status().lifecycle is GenerationLifecycle.TERMINATED
    assert processes.calls == [
        ("controller", 2.0),
        ("onec", 10.0),
        ("dbgs", 10.0),
    ]
    assert processes.all_stopped()


def test_cleanup_stage_caps_share_the_generation_deadline() -> None:
    clock = FakeClock()

    class DeadlineProcesses(FakeProcesses):
        def terminate_controller(self, timeout_s: float) -> None:
            super().terminate_controller(timeout_s)
            clock.advance(timeout_s)

        def terminate_onec(self, timeout_s: float) -> None:
            super().terminate_onec(timeout_s)
            clock.advance(timeout_s)

        def terminate_dbgs(self, timeout_s: float) -> None:
            super().terminate_dbgs(timeout_s)
            clock.advance(timeout_s)

    def slow_planned_evidence(stream: str, value: object) -> None:
        del stream
        if isinstance(value, dict) and value.get("event") == "generation_termination_planned":
            clock.advance(4.0)

    processes = DeadlineProcesses()
    supervisor = RuntimeSupervisor(
        lambda generation_id: processes,
        clock=clock,
        journal=RecoveryJournal(slow_planned_evidence),
    )
    generation = supervisor.begin_generation()
    supervisor.activate(generation)

    supervisor.terminate(TerminationCause.REQUESTED)

    assert processes.calls == [
        ("controller", 2.0),
        ("onec", 10.0),
        ("dbgs", 9.0),
    ]
    assert clock.now == 25.0


def test_only_one_cleanup_owner_can_execute_a_stage_at_a_time() -> None:
    class BlockingProcesses(FakeProcesses):
        def __init__(self) -> None:
            super().__init__()
            self.first_entered = Event()
            self.release = Event()
            self.concurrent_entry = Event()
            self._active_lock = Lock()
            self._active_stages = 0
            self.max_active_stages = 0

        def terminate_controller(self, timeout_s: float) -> None:
            with self._active_lock:
                self._active_stages += 1
                self.max_active_stages = max(
                    self.max_active_stages,
                    self._active_stages,
                )
                if self._active_stages > 1:
                    self.concurrent_entry.set()
                self.first_entered.set()
            try:
                assert self.release.wait(timeout=2.0)
                super().terminate_controller(timeout_s)
            finally:
                with self._active_lock:
                    self._active_stages -= 1

    processes = BlockingProcesses()
    supervisor = RuntimeSupervisor(lambda generation_id: processes)
    generation = supervisor.begin_generation()
    supervisor.activate(generation)
    supervisor.begin_termination(TerminationCause.REQUESTED)
    errors: list[BaseException] = []

    def complete() -> None:
        try:
            supervisor.complete_termination()
        except BaseException as error:
            errors.append(error)

    first = Thread(target=complete)
    second = Thread(target=complete)
    first.start()
    assert processes.first_entered.wait(timeout=1.0)
    second.start()
    concurrent = processes.concurrent_entry.wait(timeout=0.2)
    processes.release.set()
    first.join(timeout=2.0)
    second.join(timeout=2.0)

    assert concurrent is False
    assert processes.max_active_stages == 1
    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []


def test_incomplete_cleanup_leaves_generation_terminating_without_terminated_event() -> None:
    journal = RecoveryJournal()
    processes = FakeProcesses(stopped=False)
    supervisor = RuntimeSupervisor(lambda generation_id: processes, journal=journal)
    generation = supervisor.begin_generation()
    supervisor.activate(generation)

    with pytest.raises(RuntimeError, match="did not stop"):
        supervisor.terminate(TerminationCause.REQUESTED)

    assert supervisor.status().lifecycle is GenerationLifecycle.TERMINATING
    assert [event.event for event in journal.events] == [
        "generation_termination_planned",
    ]


def test_termination_records_and_flushes_bound_evidence_after_cleanup() -> None:
    clock = FakeClock()
    journal = RecoveryJournal()
    processes = FakeProcesses(on_terminate=lambda: clock.advance(10.0))
    supervisor = RuntimeSupervisor(
        lambda generation_id: processes,
        clock=clock,
        journal=journal,
    )
    generation = supervisor.begin_generation()
    supervisor.activate(generation)

    supervisor.terminate(TerminationCause.REQUESTED)

    assert supervisor.status().lifecycle is GenerationLifecycle.TERMINATED
    assert journal.events[-1].event == "termination_bound_exceeded"
    assert journal.events[-1].fields == {
        "generation_id": generation.generation_id,
        "detected_at": 0.0,
        "terminated_at": 30.0,
        "duration_s": 30.0,
    }
    assert journal.pending_count == 0
