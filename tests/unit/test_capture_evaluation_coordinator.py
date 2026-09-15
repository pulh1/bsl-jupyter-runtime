from __future__ import annotations

from dataclasses import replace
from queue import Empty, Queue
from threading import Event, Thread, current_thread
from time import monotonic, sleep
from uuid import UUID, uuid4

import pytest

from onec_runtime.capture_evaluation import (
    CaptureEvaluationCoordinator,
    CaptureEvaluationKind as Kind,
    CaptureEvaluationRequest,
    CaptureEvaluationState as State,
    CaptureFence,
    CapturePhase as Phase,
)
from onec_runtime.errors import (
    BslExecutionError,
    CaptureBusyError,
    CaptureEvaluationDeliveryError,
    CaptureEvaluationPendingError,
    CaptureOutcomeUnknownError,
    CaptureRecoveryRequiredError,
    CommandTimeout,
    NoCaptureEvaluationError,
    ProtocolError,
    StaleCaptureError,
    TargetLost,
)
from onec_runtime.rdbg.models import (
    EvaluationResult,
    ModuleLocation,
    PendingEvaluation,
    StopEvent,
    TargetId,
)
from onec_runtime.recovery_journal import RecoveryJournal


FENCE = CaptureFence(operation_id=7, capture_generation=3, stop_sequence=11)


class Driver:
    """Fake only the remote boundary; count ownership and lease side effects."""

    def __init__(self) -> None:
        self.target = TargetId(UUID(int=1), "synthetic")
        self.pending = PendingEvaluation(self.target, uuid4(), object())
        self.events: Queue[EvaluationResult | StopEvent | BaseException] = Queue()
        self.acknowledged = Event()
        self.polling = Event()
        self.closed = Event()
        self.dispatch_count = 0
        self.consumed = 0
        self.poll_count = 0
        self.continuation_count = 0
        self.callbacks: list[tuple[str, int, bool]] = []
        self.dispositions: list[str] = []
        self.cleanup_count = 0
        self.restore_count = 0

    def called(self, name: str) -> None:
        thread = current_thread()
        assert thread.ident is not None
        self.callbacks.append((name, thread.ident, thread.daemon))

    def dispatch(self, entered):
        self.called("dispatch")
        entered()
        self.dispatch_count += 1
        self.acknowledged.set()
        return self.pending

    def poll(self, pending, timeout_s):
        self.called("poll")
        assert pending is self.pending
        assert 0 < timeout_s <= 0.05
        self.poll_count += 1
        self.polling.set()
        if self.closed.is_set():
            raise TargetLost("private target address")
        try:
            event = self.events.get(timeout=min(timeout_s, 0.005))
        except Empty:
            raise CommandTimeout("private pending result id") from None
        if isinstance(event, BaseException):
            raise event
        self.consumed += 1
        return event

    def restore(self):
        self.called("restore")
        self.restore_count += 1

    def policy(self, result):
        self.called("policy")
        if result.error_occurred:
            raise BslExecutionError("Safe BSL failure")
        return int(result.presentation)

    def continuation(self, value):
        self.called("continuation")
        self.continuation_count += 1
        return value + 1

    def cleanup(self):
        self.called("cleanup")
        self.cleanup_count += 1

    def pin(self, disposition):
        self.called("pin")
        self.dispositions.append(disposition)

    def result(self, value=42, *, failed=False):
        self.events.put(EvaluationResult(
            self.pending.result_id, "Число", str(value), failed,
            error_text="private expression and handle" if failed else "",
        ))

    def request(self, *, kind=Kind.USER_BSL, **changes):
        return replace(CaptureEvaluationRequest(
            fence=FENCE,
            evaluation_kind=kind,
            dispatch=self.dispatch,
            poll=self.poll,
            restore=self.restore,
            result_policy=self.policy,
            pin_lease=self.pin,
            cleanup_leases=(self.cleanup,),
        ), **changes)


@pytest.fixture
def environment():
    coordinators = []
    drivers = []
    threads = []

    def create(*, journal=None):
        journal = journal if journal is not None else RecoveryJournal()
        coordinator = CaptureEvaluationCoordinator(
            FENCE, poll_interval_s=0.01, journal=journal,
        )
        driver = Driver()
        coordinators.append(coordinator)
        drivers.append(driver)
        return coordinator, driver, journal

    yield create, threads
    for coordinator in coordinators:
        coordinator.begin_close()
    for driver in drivers:
        driver.closed.set()
    for coordinator in coordinators:
        assert coordinator.join(2), "coordinator worker survived teardown"
    for thread in threads:
        thread.join(2)
        assert not thread.is_alive(), "test waiter survived teardown"


def eventually(predicate):
    deadline = monotonic() + 2
    while not predicate():
        assert monotonic() < deadline, "coordinator did not make expected progress"
        sleep(0.002)


def test_late_result_has_one_owner_after_repeated_interval_timeouts(environment):
    create, _ = environment
    coordinator, driver, _ = create()
    ticket = coordinator.submit_evaluation(driver.request(
        kind=Kind.PUBLIC_VALUE_GUARD, continuation=driver.continuation,
    ))
    assert driver.polling.wait(1)
    with pytest.raises(CaptureEvaluationPendingError) as caught:
        ticket.wait_initiator(timeout_s=0.02)
    eventually(lambda: driver.poll_count >= 5)
    pending = coordinator.status(FENCE)
    assert (pending.phase, pending.pending_evaluation_id, pending.evaluation_kind) == (
        Phase.EVALUATING, ticket.evaluation_id, Kind.PUBLIC_VALUE_GUARD,
    )
    assert caught.value.evaluation_id == ticket.evaluation_id
    assert pending.evaluation_timing.initiating_waiter_detached_ms is not None
    assert driver.dispositions == []
    assert driver.restore_count == driver.cleanup_count == 0
    for _ in range(3):
        assert coordinator.wait(FENCE, None, 0.005).state is State.PENDING
    driver.result(0)
    outcome = coordinator.wait(FENCE, ticket.evaluation_id, 1)
    assert outcome.state is State.COMPLETED
    assert outcome.result is None
    assert coordinator.wait(FENCE, ticket.evaluation_id, 0) is outcome
    assert coordinator.status(FENCE).phase is Phase.PAUSED
    assert driver.dispatch_count == driver.consumed == driver.restore_count == driver.cleanup_count == 1
    assert driver.continuation_count == 0
    assert driver.dispositions == ["release"]
    assert len({ident for _, ident, _ in driver.callbacks}) == 1
    assert all(daemon for _, _, daemon in driver.callbacks)
    assert driver.callbacks[0][1] != current_thread().ident


def test_observer_timeout_preserves_initiator_and_optional_continuation(environment):
    create, threads = environment
    coordinator, driver, _ = create()
    ticket = coordinator.submit_evaluation(driver.request(continuation=driver.continuation))
    results = Queue()
    initiator = Thread(target=lambda: results.put(ticket.wait_initiator(1)))
    threads.append(initiator)
    initiator.start()
    assert driver.polling.wait(1)
    assert coordinator.wait(FENCE, None, 0.01).state is State.PENDING
    assert coordinator.status(FENCE).evaluation_timing.initiating_waiter_detached_ms is None
    driver.result(41)
    assert results.get(timeout=1) == 42
    assert coordinator.wait(FENCE, None, 1).result == 41
    assert driver.continuation_count == 1


def test_multiple_observers_receive_the_identical_sealed_outcome(environment):
    create, threads = environment
    coordinator, driver, _ = create()
    ticket = coordinator.submit_evaluation(driver.request())
    received = Queue()
    for _ in range(5):
        thread = Thread(target=lambda: received.put(coordinator.wait(FENCE, ticket.evaluation_id, 1)))
        threads.append(thread)
        thread.start()
    assert driver.polling.wait(1)
    driver.result()
    outcomes = [received.get(timeout=1) for _ in range(5)]
    assert all(outcome is outcomes[0] for outcome in outcomes)
    assert driver.consumed == 1


def test_second_submission_is_rejected_before_dispatch(environment):
    create, _ = environment
    coordinator, driver, _ = create()
    ticket = coordinator.submit_evaluation(driver.request())
    assert driver.polling.wait(1)
    other = Driver()
    with pytest.raises(CaptureBusyError) as caught:
        coordinator.submit_evaluation(other.request())
    assert (caught.value.evaluation_id, caught.value.phase) == (ticket.evaluation_id, Phase.EVALUATING)
    assert other.dispatch_count == 0


@pytest.mark.parametrize("initiator", [False, True])
def test_interrupt_only_detaches_the_interrupted_waiter(environment, monkeypatch, initiator):
    create, _ = environment
    coordinator, driver, journal = create()
    ticket = coordinator.submit_evaluation(driver.request(continuation=driver.continuation))
    assert driver.polling.wait(1)
    original = coordinator._condition.wait
    caller = current_thread().ident

    def interrupt_wait(timeout=None):
        if current_thread().ident == caller:
            raise KeyboardInterrupt
        return original(timeout)

    monkeypatch.setattr(coordinator._condition, "wait", interrupt_wait)
    with pytest.raises(KeyboardInterrupt):
        if initiator:
            ticket.wait_initiator(1)
        else:
            coordinator.wait(FENCE, None, 1)
    monkeypatch.setattr(coordinator._condition, "wait", original)
    status = coordinator.status(FENCE)
    assert (status.evaluation_timing.initiating_waiter_detached_ms is not None) is initiator
    driver.result()
    coordinator.wait(FENCE, None, 1)
    assert driver.continuation_count == (0 if initiator else 1)
    detach = [event for event in journal.events if event.event.endswith("initiating_waiter_detached")]
    assert len(detach) == int(initiator)
    if initiator:
        assert detach[0].fields["reason"] == "interrupt"


def test_all_evidence_is_safe_and_poll_journaling_is_bounded(environment):
    create, _ = environment
    coordinator, driver, journal = create()
    ticket = coordinator.submit_evaluation(driver.request())
    assert driver.polling.wait(1)
    with pytest.raises(CaptureEvaluationPendingError):
        ticket.wait_initiator(0)
    eventually(lambda: coordinator.status(FENCE).evaluation_timing.poll_count >= 40)
    before = coordinator.status(FENCE).evaluation_timing
    events_before = len(journal.events)
    eventually(lambda: coordinator.status(FENCE).evaluation_timing.poll_count >= 60)
    during = coordinator.status(FENCE).evaluation_timing
    assert during.last_poll_ms >= before.last_poll_ms
    assert len(journal.events) == events_before
    driver.result()
    outcome = coordinator.wait(FENCE, None, 1)
    eventually(lambda: any(event.event.endswith("outcome_published") for event in journal.events))
    events = journal.events
    assert {event.event for event in events} == {
        "capture_evaluation_record_created", "capture_evaluation_dispatch_entered",
        "capture_evaluation_rdbg_acknowledged", "capture_evaluation_initiating_waiter_detached",
        "capture_evaluation_poll_progress", "capture_evaluation_result_received",
        "capture_evaluation_workspace_restored", "capture_evaluation_outcome_published",
    }
    assert len(events) < 40
    terminal = events[-1]
    assert terminal.fields["poll_count"] == outcome.timing.poll_count
    assert terminal.fields["cleanup_status"] == "completed"
    for event in events:
        assert event.fields["evaluation_id"] == ticket.evaluation_id
        assert event.fields["evaluation_kind"] == "user_bsl"
        assert event.fields["operation_id"] == 7
        assert event.fields["state"] in {"pending", "completed", "failed", "unknown"}
    exposed = repr((events, outcome, coordinator.status(FENCE), ticket, driver.request()))
    for secret in (str(driver.pending.result_id), "private", "synthetic", "Driver", "bound method"):
        assert secret not in exposed
    timing = outcome.timing
    assert timing.remote_step_count == 1
    assert timing.created_at_utc.microsecond == 0
    assert all(getattr(timing, name) is not None for name in (
        "dispatch_entered_ms", "rdbg_acknowledged_ms", "initiating_waiter_detached_ms",
        "last_poll_ms", "result_received_ms", "workspace_restored_ms", "outcome_published_ms",
    ))
    assert timing.dispatch_entered_ms <= timing.rdbg_acknowledged_ms <= timing.result_received_ms
    assert timing.result_received_ms <= timing.workspace_restored_ms <= timing.outcome_published_ms


def test_retention_preserves_user_and_only_qualifying_internal_records(environment):
    create, _ = environment
    coordinator, user, _ = create()
    user_ticket = coordinator.submit_evaluation(user.request())
    user.result(10)
    assert user_ticket.wait_initiator(1) == 10
    user_outcome = coordinator.wait(FENCE, None, 0)

    normal = Driver()
    normal_ticket = coordinator.submit_evaluation(normal.request(kind=Kind.INSPECTION))
    normal.result(20)
    assert normal_ticket.wait_initiator(1) == 20
    assert coordinator.status(FENCE).last_evaluation_id == user_ticket.evaluation_id
    with pytest.raises(NoCaptureEvaluationError):
        coordinator.wait(FENCE, normal_ticket.evaluation_id, 0)

    observed = Driver()
    observed_ticket = coordinator.submit_evaluation(observed.request(kind=Kind.INSPECTION))
    assert coordinator.wait(FENCE, None, 0).state is State.PENDING
    observed.result(30)
    internal_outcome = coordinator.wait(FENCE, None, 1)
    assert internal_outcome.result is None
    assert coordinator.status(FENCE).last_evaluation_id == observed_ticket.evaluation_id
    assert coordinator.status(FENCE).last_user_evaluation_id == user_ticket.evaluation_id
    assert coordinator.wait(FENCE, user_ticket.evaluation_id, 0) is user_outcome

    replacement = Driver()
    replacement_ticket = coordinator.submit_evaluation(replacement.request(kind=Kind.PUBLIC_VALUE_GUARD))
    assert replacement.polling.wait(1)
    with pytest.raises(CaptureEvaluationPendingError):
        replacement_ticket.wait_initiator(0)
    replacement.result(0)
    assert coordinator.wait(FENCE, None, 1).evaluation_id == replacement_ticket.evaluation_id
    with pytest.raises(NoCaptureEvaluationError):
        coordinator.wait(FENCE, observed_ticket.evaluation_id, 0)
    assert coordinator.wait(FENCE, user_ticket.evaluation_id, 0) is user_outcome


@pytest.mark.parametrize("boundary, phase, state, disposition, code", [
    ("before_dispatch", Phase.PAUSED, State.FAILED, "release", "pre_dispatch_failed"),
    ("dispatch", Phase.OUTCOME_UNKNOWN, State.UNKNOWN, "quarantine", "dispatch_uncertain"),
    ("policy", Phase.PAUSED, State.FAILED, "release", "result_policy_failed"),
    ("restore", Phase.RECOVERY_REQUIRED, State.FAILED, "quarantine", "workspace_restore_failed"),
    ("cleanup", Phase.RECOVERY_REQUIRED, State.FAILED, "quarantine", "cleanup_failed"),
    ("cleanup_timeout", Phase.RECOVERY_REQUIRED, State.FAILED, "quarantine", "cleanup_uncertain"),
])
def test_failure_boundaries_keep_result_and_cleanup_distinct(
    environment, boundary, phase, state, disposition, code,
):
    create, _ = environment
    coordinator, driver, journal = create()

    def fail(*args):
        if boundary == "dispatch":
            args[0]()
        error = CommandTimeout if boundary in {"dispatch", "cleanup_timeout"} else ValueError
        raise error("private source handle and target URL")

    changes = {"before_dispatch": "dispatch", "dispatch": "dispatch", "policy": "result_policy",
               "restore": "restore", "cleanup": "cleanup_leases", "cleanup_timeout": "cleanup_leases"}
    name = changes[boundary]
    request = driver.request(kind=Kind.PUBLIC_VALUE_GUARD, **{name: (fail,) if name == "cleanup_leases" else fail})
    ticket = coordinator.submit_evaluation(request)
    if boundary not in {"before_dispatch", "dispatch"}:
        driver.result()
    outcome = coordinator.wait(FENCE, ticket.evaluation_id, 1)
    eventually(lambda: any(event.event.endswith("outcome_published") for event in journal.events))
    assert (outcome.state, outcome.result, outcome.diagnostic.code) == (state, None, code)
    assert coordinator.status(FENCE).phase is phase
    assert driver.dispositions == [disposition]
    assert "private" not in repr((outcome.diagnostic, outcome.error, journal.events))
    if phase is Phase.PAUSED:
        assert coordinator.status(FENCE).failure is None
        assert driver.cleanup_count == int(boundary == "policy")
    else:
        error_type = CaptureOutcomeUnknownError if phase is Phase.OUTCOME_UNKNOWN else CaptureRecoveryRequiredError
        with pytest.raises(error_type):
            coordinator.submit_evaluation(Driver().request())
    if boundary == "policy":
        with pytest.raises(CaptureEvaluationDeliveryError):
            ticket.wait_initiator(0)


def test_confirmed_bsl_failure_cleans_up_without_exposing_raw_debugger_error(environment):
    create, _ = environment
    coordinator, driver, _ = create()
    ticket = coordinator.submit_evaluation(driver.request())
    driver.result(failed=True)
    outcome = coordinator.wait(FENCE, None, 1)
    assert outcome.state is State.FAILED
    assert outcome.diagnostic.code == "bsl_error"
    assert "private" not in outcome.error
    with pytest.raises(BslExecutionError):
        ticket.wait_initiator(0)
    assert coordinator.status(FENCE).phase is Phase.PAUSED
    assert driver.cleanup_count == driver.restore_count == 1
    assert driver.dispositions == ["release"]


def test_unexpected_stop_requires_recovery_without_nested_capture(environment):
    create, _ = environment
    coordinator, driver, journal = create()
    coordinator.submit_evaluation(driver.request())
    driver.events.put(StopEvent(driver.target, ModuleLocation(
        "private module", "private URL", UUID(int=2), UUID(int=3), 19,
    ), "private stop"))
    outcome = coordinator.wait(FENCE, None, 1)
    assert outcome.state is State.FAILED
    assert outcome.diagnostic.code == "unexpected_stop"
    assert coordinator.status(FENCE).phase is Phase.RECOVERY_REQUIRED
    assert driver.restore_count == driver.cleanup_count == 0
    assert driver.dispositions == ["quarantine"]
    eventually(lambda: any(event.event.endswith("outcome_published") for event in journal.events))
    assert journal.events[-1].fields["cleanup_status"] == "not_started"


def test_target_loss_invalidates_fence_and_wakes_waiters(environment):
    create, _ = environment
    coordinator, driver, _ = create()
    ticket = coordinator.submit_evaluation(driver.request())
    driver.events.put(TargetLost("private address"))
    with pytest.raises(StaleCaptureError):
        coordinator.wait(FENCE, None, 1)
    assert coordinator.status(FENCE).phase is Phase.STALE
    with pytest.raises(StaleCaptureError):
        ticket.wait_initiator(0)
    assert driver.dispositions == ["quarantine"]


def test_wrong_fence_and_absent_outcome_never_dispatch(environment):
    create, _ = environment
    coordinator, driver, _ = create()
    assert coordinator.status(FENCE).phase is Phase.PAUSED
    with pytest.raises(NoCaptureEvaluationError):
        coordinator.wait(FENCE, None, 0)
    wrong = replace(FENCE, stop_sequence=12)
    assert coordinator.status(wrong).phase is Phase.STALE
    with pytest.raises(StaleCaptureError):
        coordinator.wait(wrong, None, 0)
    with pytest.raises(StaleCaptureError):
        coordinator.submit_evaluation(driver.request(fence=wrong))
    assert driver.dispatch_count == 0


def test_cleanup_and_pin_callbacks_finish_before_paused_and_outcome_publication(environment):
    create, threads = environment
    coordinator, driver, _ = create()
    cleaning = Event()
    release_cleanup = Event()

    def cleanup():
        driver.cleanup()
        cleaning.set()
        assert release_cleanup.wait(1)

    ticket = coordinator.submit_evaluation(driver.request(cleanup_leases=(cleanup,)))
    assert driver.polling.wait(1)
    with pytest.raises(CaptureEvaluationPendingError):
        ticket.wait_initiator(0)
    driver.result()
    assert cleaning.wait(1)
    try:
        status_queue = Queue()
        thread = Thread(target=lambda: status_queue.put(coordinator.status(FENCE)))
        threads.append(thread)
        thread.start()
        assert status_queue.get(timeout=0.2).phase is Phase.EVALUATING
        assert coordinator.wait(FENCE, None, 0).state is State.PENDING
        assert driver.dispositions == []
    finally:
        release_cleanup.set()
    assert coordinator.wait(FENCE, None, 1).state is State.COMPLETED
    assert driver.dispositions == ["release"]


def test_shutdown_rejects_submission_wakes_waiters_and_does_not_release_live_pin(environment):
    create, threads = environment
    coordinator, driver, _ = create()
    blocked = Event()
    release_poll = Event()

    def poll(pending, timeout_s):
        blocked.set()
        assert release_poll.wait(1)
        raise TargetLost("private address")

    ticket = coordinator.submit_evaluation(driver.request(poll=poll))
    assert blocked.wait(1)
    result = Queue()

    def wait():
        try:
            ticket.wait_initiator(None)
        except StaleCaptureError:
            result.put("closed")

    thread = Thread(target=wait)
    threads.append(thread)
    thread.start()
    try:
        coordinator.begin_close()
        assert result.get(timeout=0.2) == "closed"
        assert not coordinator.join(0.01)
        assert driver.dispositions == []
        with pytest.raises(StaleCaptureError):
            coordinator.submit_evaluation(Driver().request())
    finally:
        release_poll.set()
    assert coordinator.join(1)
    assert driver.dispositions == ["quarantine"]


@pytest.mark.parametrize("observed", [False, True])
def test_settled_internal_retention_does_not_own_private_result(environment, observed):
    import gc
    import weakref

    create, _ = environment
    coordinator, driver, _ = create()
    references = []

    class PrivateValue:
        pass

    def policy(result):
        value = PrivateValue()
        references.append(weakref.ref(value))
        return value

    ticket = coordinator.submit_evaluation(driver.request(kind=Kind.INSPECTION, result_policy=policy))
    if observed:
        coordinator.wait(FENCE, None, 0)
    driver.result()
    value = ticket.wait_initiator(1)
    assert isinstance(value, PrivateValue)
    if observed:
        assert coordinator.wait(FENCE, None, 0).result is None
    del ticket, value
    gc.collect()
    eventually(lambda: references[0]() is None)


@pytest.mark.parametrize("boundary, phase, code", [
    ("dispatch", Phase.PAUSED, "pre_dispatch_failed"),
    ("policy", Phase.PAUSED, "result_policy_failed"),
    ("restore", Phase.RECOVERY_REQUIRED, "workspace_restore_failed"),
])
def test_callback_base_exception_cannot_strand_owned_work(environment, boundary, phase, code):
    create, _ = environment
    coordinator, driver, _ = create()

    def cancelled(*args):
        raise SystemExit("private callback cancellation")

    name = {"dispatch": "dispatch", "policy": "result_policy", "restore": "restore"}[boundary]
    ticket = coordinator.submit_evaluation(driver.request(**{name: cancelled}))
    driver.result()
    outcome = coordinator.wait(FENCE, ticket.evaluation_id, 0.2)
    assert outcome.state is State.FAILED
    assert outcome.diagnostic.code == code
    assert coordinator.status(FENCE).phase is phase


def test_close_before_transport_marker_prevents_dispatch(environment):
    create, _ = environment
    coordinator, driver, _ = create()
    preparing = Event()
    release_prepare = Event()

    def dispatch(entered):
        preparing.set()
        assert release_prepare.wait(1)
        return driver.dispatch(entered)

    coordinator.submit_evaluation(driver.request(dispatch=dispatch))
    assert preparing.wait(1)
    try:
        coordinator.begin_close()
    finally:
        release_prepare.set()
    assert coordinator.join(1)
    assert driver.dispatch_count == 0
    assert driver.dispositions == ["release"]


@pytest.mark.parametrize("boundary", ["before_dispatch", "dispatch", "policy", "continuation", "restore", "cleanup"])
def test_initiator_exception_never_exposes_callback_or_rdbg_secrets(environment, boundary):
    create, _ = environment
    coordinator, driver, _ = create()

    def failed(*args):
        if boundary == "dispatch":
            args[0]()
        raise ValueError("secret_source secret_handle secret_target")

    name = {"before_dispatch": "dispatch", "dispatch": "dispatch", "policy": "result_policy",
            "continuation": "continuation", "restore": "restore", "cleanup": "cleanup_leases"}[boundary]
    ticket = coordinator.submit_evaluation(driver.request(**{name: (failed,) if name == "cleanup_leases" else failed}))
    driver.result()
    with pytest.raises(ProtocolError) as caught:
        ticket.wait_initiator(1)
    exposed = str(caught.value) + repr(caught.value)
    assert all(secret not in exposed for secret in ("secret_source", "secret_handle", "secret_target"))
    if boundary in {"policy", "continuation"}:
        assert isinstance(caught.value, CaptureEvaluationDeliveryError)
        assert caught.value.diagnostic.code == (
            "result_policy_failed" if boundary == "policy" else "continuation_failed"
        )


@pytest.mark.parametrize("messages", [("message",) * 105, ("a" * 1100,)])
def test_truncated_messages_publish_a_repeatable_outcome_without_stranding_worker(environment, messages):
    create, _ = environment
    coordinator, driver, _ = create()
    ticket = coordinator.submit_evaluation(driver.request(seal_messages=lambda: messages))
    driver.result()
    outcome = coordinator.wait(FENCE, None, 0.2)
    assert outcome.state is State.COMPLETED
    assert outcome.timing.outcome_published_ms is not None
    assert outcome.diagnostic.code == "messages_truncated"
    assert len(outcome.messages) <= 100
    assert max(map(len, outcome.messages)) <= 1024
    assert coordinator.wait(FENCE, None, 0) is outcome
    assert ticket.wait_initiator(0) == 42
    assert driver.dispositions == ["release"]


def test_join_requires_a_finite_deadline(environment):
    create, _ = environment
    coordinator, _, _ = create()
    coordinator.begin_close()
    for timeout in (None, float("inf"), float("nan"), -1):
        with pytest.raises(ValueError):
            coordinator.join(timeout)


@pytest.mark.parametrize("initiator", [False, True])
def test_worker_rejects_recursive_wait_without_detaching_initiator(environment, initiator):
    create, _ = environment
    coordinator, driver, _ = create()

    def policy(result):
        with pytest.raises(ProtocolError, match="worker"):
            if initiator:
                ticket.wait_initiator(0)
            else:
                coordinator.wait(FENCE, None, 0)
        return 42

    ticket = coordinator.submit_evaluation(driver.request(result_policy=policy))
    driver.result()
    assert ticket.wait_initiator(1) == 42
    assert coordinator.status(FENCE).evaluation_timing.initiating_waiter_detached_ms is None


def test_delivery_error_bounds_and_sanitizes_its_diagnostic():
    error = CaptureEvaluationDeliveryError("delivery\n" + "x" * 3000)
    assert isinstance(error, ProtocolError)
    assert len(str(error)) <= 1024
    assert "\n" not in str(error)
    assert error.diagnostic.code == "result_delivery_failed"
    assert len(error.diagnostic.message) <= 1024


@pytest.mark.parametrize("boundary", ["deadline", "condition_entry"])
def test_pre_entry_interrupt_detaches_acknowledged_initiator(environment, monkeypatch, boundary):
    import onec_runtime.capture_evaluation as module

    create, _ = environment
    coordinator, driver, journal = create()
    ticket = coordinator.submit_evaluation(driver.request(
        kind=Kind.PUBLIC_VALUE_GUARD, continuation=driver.continuation,
    ))
    assert driver.polling.wait(1)
    caller = current_thread().ident
    armed = True
    original_deadline = module._deadline
    condition_type = type(coordinator._condition)
    original_enter = condition_type.__enter__

    def deadline(timeout_s):
        nonlocal armed
        if current_thread().ident == caller and armed:
            armed = False
            raise KeyboardInterrupt
        return original_deadline(timeout_s)

    def enter(condition):
        nonlocal armed
        if condition is coordinator._condition and current_thread().ident == caller and armed:
            armed = False
            raise KeyboardInterrupt
        return original_enter(condition)

    if boundary == "deadline":
        monkeypatch.setattr(module, "_deadline", deadline)
    else:
        monkeypatch.setattr(condition_type, "__enter__", enter)
    with pytest.raises(KeyboardInterrupt):
        ticket.wait_initiator(1)
    pending = coordinator.status(FENCE)
    assert pending.phase is Phase.EVALUATING
    assert pending.evaluation_timing.initiating_waiter_detached_ms is not None
    driver.result(0)
    outcome = coordinator.wait(FENCE, None, 1)
    eventually(lambda: any(event.event.endswith("outcome_published") for event in journal.events))
    assert outcome.state is State.COMPLETED
    assert driver.continuation_count == 0
    assert driver.restore_count == driver.cleanup_count == 1
    assert driver.dispositions == ["release"]
    names = [event.event for event in journal.events]
    assert names.index("capture_evaluation_initiating_waiter_detached") < names.index("capture_evaluation_outcome_published")


@pytest.mark.parametrize("kind", [Kind.USER_BSL, Kind.PUBLIC_VALUE_GUARD])
def test_delayed_delivery_interrupt_cannot_replace_newer_retained_outcome(environment, monkeypatch, kind):
    create, threads = environment
    coordinator, older, journal = create()
    older_ticket = coordinator.submit_evaluation(older.request(kind=kind))
    coordinator.wait(FENCE, None, 0)  # Both kinds are publicly retained.
    older.result(1)
    older_outcome = coordinator.wait(FENCE, None, 1)
    entered_delivery = Event()
    interrupt_delivery = Event()
    interrupted = Queue()
    original_require = coordinator._require_fence_locked

    def hold_delivery(fence):
        if current_thread() is delivery_thread:
            # Emulate a descheduled original waiter without blocking other
            # callers' condition access. The interrupt is still delivered at
            # the original completed ticket's fence-check boundary.
            entered_delivery.set()
            assert coordinator._condition.wait_for(interrupt_delivery.is_set, timeout=2)
            raise KeyboardInterrupt
        return original_require(fence)

    def deliver():
        try:
            older_ticket.wait_initiator(1)
        except KeyboardInterrupt:
            interrupted.put(True)

    delivery_thread = Thread(target=deliver)
    threads.append(delivery_thread)
    monkeypatch.setattr(coordinator, "_require_fence_locked", hold_delivery)
    delivery_thread.start()
    assert entered_delivery.wait(1)
    try:
        newer = Driver()
        newer_ticket = coordinator.submit_evaluation(newer.request(kind=kind))
        coordinator.wait(FENCE, None, 0)
        newer.result(2)
        newer_outcome = coordinator.wait(FENCE, None, 1)
    finally:
        interrupt_delivery.set()
        with coordinator._condition:
            coordinator._condition.notify_all()
    assert interrupted.get(timeout=1)
    assert coordinator.wait(FENCE, None, 0) is newer_outcome
    assert coordinator.wait(FENCE, newer_ticket.evaluation_id, 0) is newer_outcome
    assert coordinator.status(FENCE).last_evaluation_id == newer_ticket.evaluation_id
    if kind is Kind.USER_BSL:
        assert coordinator.status(FENCE).last_user_evaluation_id == newer_ticket.evaluation_id
    assert older_outcome.timing.initiating_waiter_detached_ms is None
    assert older_ticket._record.initiator_attached
    eventually(lambda: len([event for event in journal.events if event.event.endswith("outcome_published")]) == 2)
    assert not any(event.event.endswith("initiating_waiter_detached") for event in journal.events)
    # No late detachment event may be stranded when the worker becomes idle.
    with coordinator._condition:
        assert not coordinator._events
    coordinator.begin_close()
    assert coordinator.join(1)
    assert not any(event.event.endswith("initiating_waiter_detached") for event in journal.events)


def test_delayed_retention_of_detached_internal_record_preserves_settlement_order(environment):
    create, _ = environment
    coordinator, older, _ = create()
    older_ticket = coordinator.submit_evaluation(older.request(kind=Kind.PUBLIC_VALUE_GUARD))
    assert older.polling.wait(1)
    with pytest.raises(CaptureEvaluationPendingError):
        older_ticket.wait_initiator(0)
    older.result(1)
    coordinator.wait(FENCE, None, 1)
    newer = Driver()
    newer_ticket = coordinator.submit_evaluation(newer.request(kind=Kind.INSPECTION))
    coordinator.wait(FENCE, None, 0)
    newer.result(2)
    newer_outcome = coordinator.wait(FENCE, None, 1)
    # Replay an older qualifying retention notification after the newer one;
    # the observable default and explicit-ID slots must remain chronological.
    with coordinator._condition:
        coordinator._retain_locked(older_ticket._record)
    assert coordinator.wait(FENCE, None, 0) is newer_outcome
    assert coordinator.wait(FENCE, newer_ticket.evaluation_id, 0) is newer_outcome


class PublicationBarrierJournal(RecoveryJournal):
    def __init__(self):
        super().__init__()
        self.entered = Event()
        self.release = Event()
        self.recorded = Event()

    def record(self, stream, event, **fields):
        if event == "capture_evaluation_outcome_published":
            self.entered.set()
            assert self.release.wait(2)
        result = super().record(stream, event, **fields)
        if event == "capture_evaluation_outcome_published":
            self.recorded.set()
        return result


@pytest.mark.parametrize("close_at_barrier", [False, True])
def test_terminal_state_and_timing_are_visible_before_journal_io(environment, close_at_barrier):
    create, _ = environment
    journal = PublicationBarrierJournal()
    coordinator, driver, _ = create(journal=journal)
    ticket = coordinator.submit_evaluation(driver.request(kind=Kind.PUBLIC_VALUE_GUARD))
    coordinator.wait(FENCE, None, 0)
    driver.result(42)
    assert journal.entered.wait(1)
    try:
        status = coordinator.status(FENCE)
        assert status.phase is Phase.PAUSED
        assert status.pending_evaluation_id is None
        assert status.last_evaluation_id == ticket.evaluation_id
        outcome = coordinator.wait(FENCE, None, 0)
        assert outcome.state is State.COMPLETED
        assert outcome.timing is status.evaluation_timing
        assert outcome.timing.elapsed_ms == outcome.timing.outcome_published_ms
        assert outcome.timing.initiating_waiter_detached_ms is None
        assert ticket.wait_initiator(0) == 42
        if close_at_barrier:
            coordinator.begin_close()
            assert not coordinator.join(0.01)
    finally:
        journal.release.set()
    assert journal.recorded.wait(1)
    if close_at_barrier:
        assert coordinator.join(1)
    else:
        assert coordinator.wait(FENCE, None, 0) is outcome
    assert journal.events[-1].event == "capture_evaluation_outcome_published"
    assert journal.events[-1].fields["state"] == "completed"
    assert not any(event.event.endswith("initiating_waiter_detached") for event in journal.events)


def test_pending_detachment_is_included_in_atomic_terminal_timing(environment):
    create, _ = environment
    coordinator, driver, journal = create()
    disposing_pin = Event()
    release_pin = Event()

    def pin(disposition):
        disposing_pin.set()
        assert release_pin.wait(2)
        driver.pin(disposition)

    ticket = coordinator.submit_evaluation(driver.request(
        kind=Kind.PUBLIC_VALUE_GUARD, pin_lease=pin,
    ))
    driver.result(0)
    assert disposing_pin.wait(1)
    try:
        with pytest.raises(CaptureEvaluationPendingError):
            ticket.wait_initiator(0)
        pending = coordinator.status(FENCE)
        assert pending.phase is Phase.EVALUATING
        assert pending.evaluation_timing.outcome_published_ms is None
        detached_ms = pending.evaluation_timing.initiating_waiter_detached_ms
        assert detached_ms is not None
        assert detached_ms <= pending.evaluation_timing.elapsed_ms
    finally:
        release_pin.set()
    outcome = coordinator.wait(FENCE, None, 1)
    assert outcome.state is State.COMPLETED
    assert outcome.timing.initiating_waiter_detached_ms == detached_ms
    assert detached_ms <= outcome.timing.outcome_published_ms == outcome.timing.elapsed_ms
    eventually(lambda: any(event.event.endswith("outcome_published") for event in journal.events))
    names = [event.event for event in journal.events]
    assert names.index("capture_evaluation_initiating_waiter_detached") < names.index("capture_evaluation_outcome_published")
    assert journal.events[-1].fields["initiating_waiter_detached_ms"] == detached_ms
    assert journal.events[-1].fields["state"] == "completed"


def test_close_drains_queued_evidence_in_order_after_publication_barrier(environment):
    create, _ = environment
    journal = PublicationBarrierJournal()
    coordinator, first, _ = create(journal=journal)
    first_ticket = coordinator.submit_evaluation(first.request())
    first.result(1)
    assert journal.entered.wait(1)
    queued = Driver()
    try:
        assert coordinator.wait(FENCE, None, 0).state is State.COMPLETED
        queued_ticket = coordinator.submit_evaluation(queued.request(kind=Kind.INSPECTION))
        coordinator.begin_close()
        assert not coordinator.join(0.01)
    finally:
        journal.release.set()
    assert coordinator.join(1)
    assert queued.dispatch_count == 0
    assert queued.dispositions == ["release"]
    events = journal.events
    first_published = next(index for index, event in enumerate(events) if (
        event.event.endswith("outcome_published") and event.fields["evaluation_id"] == first_ticket.evaluation_id
    ))
    queued_created = next(index for index, event in enumerate(events) if (
        event.event.endswith("record_created") and event.fields["evaluation_id"] == queued_ticket.evaluation_id
    ))
    assert first_published < queued_created
    assert events[-1].fields["evaluation_id"] == queued_ticket.evaluation_id
    assert events[-1].event == "capture_evaluation_outcome_published"
    assert events[-1].fields["state"] == "failed"
    with coordinator._condition:
        assert not coordinator._events


def test_pending_interrupt_detaches_before_condition_exit_allows_late_result(environment, monkeypatch):
    create, threads = environment
    coordinator, driver, journal = create()
    ticket = coordinator.submit_evaluation(driver.request(
        kind=Kind.PUBLIC_VALUE_GUARD, continuation=driver.continuation,
    ))
    assert driver.polling.wait(1)
    coordinator.wait(FENCE, None, 0)
    condition_released = Event()
    resume_handler = Event()
    interrupted = Queue()
    original_wait = coordinator._condition.wait
    condition_type = type(coordinator._condition)
    original_exit = condition_type.__exit__
    armed = True

    def interrupt_wait(timeout=None):
        nonlocal armed
        if current_thread() is initiator and armed:
            armed = False
            raise KeyboardInterrupt
        return original_wait(timeout)

    def pause_after_release(condition, exc_type, exc_value, traceback):
        result = original_exit(condition, exc_type, exc_value, traceback)
        if (condition is coordinator._condition and current_thread() is initiator
                and exc_type is KeyboardInterrupt and not condition_released.is_set()):
            # Schedule the owner after the interrupted wait has released the
            # mutex but before any outer interruption handler can reacquire it.
            condition_released.set()
            assert resume_handler.wait(2)
        return result

    def wait():
        try:
            ticket.wait_initiator(1)
        except KeyboardInterrupt:
            interrupted.put(True)

    initiator = Thread(target=wait)
    threads.append(initiator)
    monkeypatch.setattr(coordinator._condition, "wait", interrupt_wait)
    monkeypatch.setattr(condition_type, "__exit__", pause_after_release)
    initiator.start()
    assert condition_released.wait(1)
    try:
        driver.result(0)
        outcome = coordinator.wait(FENCE, None, 1)
        assert outcome.state is State.COMPLETED
        assert driver.continuation_count == 0
        assert driver.dispatch_count == driver.consumed == 1
        assert driver.restore_count == driver.cleanup_count == 1
        assert driver.dispositions == ["release"]
        assert outcome.timing.initiating_waiter_detached_ms is not None
    finally:
        resume_handler.set()
    assert interrupted.get(timeout=1)
    assert coordinator.wait(FENCE, None, 0) is outcome
    eventually(lambda: any(event.event.endswith("outcome_published") for event in journal.events))
    detach_events = [event for event in journal.events if event.event.endswith("initiating_waiter_detached")]
    assert len(detach_events) == 1  # Inner/outer handling must be idempotent.
    assert detach_events[0].fields["reason"] == "interrupt"
    assert detach_events[0].fields["state"] == "pending"
    terminal = journal.events[-1]
    assert terminal.event == "capture_evaluation_outcome_published"
    assert terminal.fields["initiating_waiter_detached_ms"] == outcome.timing.initiating_waiter_detached_ms
    assert outcome.timing.initiating_waiter_detached_ms <= outcome.timing.outcome_published_ms
