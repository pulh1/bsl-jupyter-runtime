"""Remote-step ownership races, with only the debugger boundary scripted."""
from dataclasses import replace
from threading import Event, Thread

import pytest

import onec_runtime.capture_evaluation as capture
from onec_runtime.errors import CaptureEvaluationPendingError, ProtocolError
from test_capture_evaluation_coordinator import Driver, FENCE, environment


def remote_step(driver):
    assert hasattr(capture, "CaptureRemoteStep"), "remote steps need a coordinator-owned plan"
    return capture.CaptureRemoteStep(driver.dispatch, driver.poll, driver.restore)


def test_cleanup_finishes_before_paused_even_before_caller_unwinds(environment):
    create, threads = environment
    coordinator, driver, _ = create()
    cleaner = Driver()
    lease = capture.CaptureCleanupLease("private-key", remote_step(cleaner))
    caller_unwinding = Event()
    release_caller = Event()
    ticket = coordinator.submit_evaluation(driver.request(cleanup_leases=(lease,)))

    def caller():
        try:
            with pytest.raises(CaptureEvaluationPendingError):
                ticket.wait_initiator(0.01)
        finally:
            caller_unwinding.set()
            assert release_caller.wait(2)

    thread = Thread(target=caller)
    threads.append(thread)
    thread.start()
    try:
        assert caller_unwinding.wait(1)
        assert ticket._record.request.cleanup_leases == (lease,)
        driver.result()
        assert cleaner.polling.wait(1)
        assert coordinator.status(FENCE).phase == capture.CapturePhase.EVALUATING
        assert not driver.dispositions
        cleaner.result()
        outcome = coordinator.wait(FENCE, ticket.evaluation_id, timeout_s=1)
        assert outcome.state == capture.CaptureEvaluationState.COMPLETED
        assert coordinator.status(FENCE).phase == capture.CapturePhase.PAUSED
        assert ticket._record.cleanup_status == "completed"
        assert driver.dispositions == ["release"]
        assert thread.is_alive(), "late cleanup must not depend on caller finally"
        assert outcome.timing.remote_step_count == 2
    finally:
        release_caller.set()


@pytest.mark.parametrize("failure, status, code", [
    ("dispatch", "unknown", "cleanup_uncertain"),
    ("bsl", "failed", "cleanup_failed"),
])
def test_cleanup_remote_failure_quarantines_record(environment, failure, status, code):
    create, _ = environment
    coordinator, driver, _ = create()
    cleaner = Driver()
    step = remote_step(cleaner)
    if failure == "dispatch":
        def uncertain(entered):
            entered()
            raise OSError("private transport address")
        step = replace(step, dispatch=uncertain)
    else:
        cleaner.result(failed=True)
    lease = capture.CaptureCleanupLease("private-key", step)
    ticket = coordinator.submit_evaluation(driver.request(cleanup_leases=(lease,)))
    driver.result()
    outcome = coordinator.wait(FENCE, ticket.evaluation_id, timeout_s=1)
    assert outcome.diagnostic.code == code
    assert coordinator.status(FENCE).phase == capture.CapturePhase.RECOVERY_REQUIRED
    assert ticket._record.cleanup_status == status
    assert coordinator._quarantined.request.cleanup_leases == (lease,)
    assert driver.dispositions == ["quarantine"]


def test_inline_remote_uncertainty_is_not_a_local_policy_error(environment):
    create, _ = environment
    coordinator, driver, _ = create()
    second = Driver()
    step = remote_step(second)
    def uncertain(entered):
        entered()
        raise OSError("private transport address")
    step = replace(step, dispatch=uncertain)
    contexts = []
    def policy(context, result):
        contexts.append(context)
        return context.execute_inline(step)
    request = replace(driver.request(cleanup_leases=()), step_policy=policy)
    ticket = coordinator.submit_evaluation(request)
    driver.result()
    outcome = coordinator.wait(FENCE, ticket.evaluation_id, timeout_s=1)
    assert outcome.diagnostic.code == "dispatch_uncertain"
    assert coordinator.status(FENCE).phase == capture.CapturePhase.RECOVERY_REQUIRED
    with pytest.raises(ProtocolError, match="worker|active"):
        contexts[0].execute_inline(step)


def test_inline_steps_use_active_record_and_reject_recursive_submission(environment):
    create, _ = environment
    coordinator, driver, _ = create()
    second = Driver()
    second.result(17)
    step = remote_step(second)
    contexts = []
    def policy(context, result):
        contexts.append(context)
        with pytest.raises(ProtocolError, match="Recursive"):
            coordinator.submit_evaluation(driver.request())
        return int(context.execute_inline(step).presentation)
    ticket = coordinator.submit_evaluation(replace(
        driver.request(cleanup_leases=()), step_policy=policy,
    ))
    driver.result()
    assert ticket.wait_initiator(1) == 17
    assert ticket._record.remote_step_count == 2
    assert {item[1] for item in driver.callbacks + second.callbacks} == {coordinator._worker.ident}
    with pytest.raises(ProtocolError, match="worker|active"):
        contexts[0].execute_inline(step)


def test_followup_rejected_before_dispatch_still_cleans_initial_temporary_value(environment):
    create, _ = environment
    coordinator, driver, _ = create()
    cleaner = Driver()
    cleaner.result()
    second = remote_step(Driver())
    def reject(entered):
        raise ProtocolError("local pre-dispatch rejection")
    second = replace(second, dispatch=reject)
    ticket = coordinator.submit_evaluation(replace(
        driver.request(cleanup_leases=(capture.CaptureCleanupLease("key", remote_step(cleaner)),)),
        step_policy=lambda context, result: context.execute_inline(second),
    ))
    driver.result()
    outcome = coordinator.wait(FENCE, ticket.evaluation_id, timeout_s=1)
    assert outcome.state == capture.CaptureEvaluationState.FAILED
    assert cleaner.consumed == 1, "creating step succeeded, so cleanup is mandatory"
    assert ticket._record.cleanup_status == "completed"
    assert coordinator.status(FENCE).phase == capture.CapturePhase.PAUSED


def test_stale_inline_context_is_rejected_on_worker_of_next_record(environment):
    create, _ = environment
    coordinator, driver, _ = create()
    contexts = []
    first = coordinator.submit_evaluation(replace(
        driver.request(cleanup_leases=()),
        step_policy=lambda context, result: contexts.append(context) or 42,
    ))
    driver.result()
    assert first.wait_initiator(1) == 42
    next_driver = Driver()
    def policy(context, result):
        with pytest.raises(ProtocolError, match="active record"):
            contexts[0].execute_inline(remote_step(Driver()))
        return 17
    second = coordinator.submit_evaluation(replace(
        next_driver.request(cleanup_leases=()), step_policy=policy,
    ))
    next_driver.result()
    assert second.wait_initiator(1) == 17


def test_hot_reload_late_commit_and_pin_release_do_not_need_callers_target_lock(tmp_path, environment):
    from uuid import UUID
    from onec_runtime.worker_universe import ServerWorkerUniverseRegistry, WorkerUniverseRegistry
    from onec_runtime.rdbg.models import EvaluationResult
    from test_worker_universe import _generation_artifacts, _UniverseTargetExecutor

    create, threads = environment
    coordinator, driver, _ = create()
    a, b, newer_b, _ = _generation_artifacts(tmp_path)
    host = WorkerUniverseRegistry(runtime_generation=7, context_generation=3)
    target = _UniverseTargetExecutor()
    registry = ServerWorkerUniverseRegistry(host, target)
    first = host.prepare((a, b))
    target.acknowledge(first)
    registry.promote(first)
    pin = host.pin_active()
    candidate = host.prepare((a, newer_b))
    target.acknowledge(candidate)
    prepared = registry.prepare_root(candidate, transaction_id=UUID(int=99))
    pending = []
    caller_detached = Event()
    def execute(plan):
        assert not registry._lock._is_owned()
        assert not host._lock._is_owned()
        def release(disposition):
            assert not registry._lock._is_owned()
            assert not host._lock._is_owned()
            assert disposition == "release"
            registry.release_pin(pin)
        ticket = coordinator.submit_evaluation(driver.request(
            kind=capture.CaptureEvaluationKind.MATERIALIZATION_HELPER,
            result_policy=lambda result: plan.commit(result.presentation),
            pin_lease=release, cleanup_leases=(),
        ))
        pending.append((ticket, plan))
        return ticket.wait_initiator(0.01)
    registry._mutation_executor = execute
    def caller():
        with pytest.raises(CaptureEvaluationPendingError):
            registry.swap_root(prepared)
        caller_detached.set()
    thread = Thread(target=caller)
    threads.append(thread)
    thread.start()
    assert caller_detached.wait(1)
    ticket, mutation = pending[0]
    assert registry._mutations
    driver.events.put(EvaluationResult(driver.pending.result_id, "Строка", target(mutation.instruction), False))
    outcome = coordinator.wait(FENCE, ticket.evaluation_id, timeout_s=1)
    assert outcome.state == capture.CaptureEvaluationState.COMPLETED
    assert host.active_handle is candidate.handle
    assert not host._leases
    assert not registry._mutations
