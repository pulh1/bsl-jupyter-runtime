from __future__ import annotations

from queue import Empty, Queue
from threading import Event, current_thread
from time import monotonic, sleep
from uuid import uuid4

import pytest

from onec_runtime.capture_evaluation import (
    CaptureEvaluationKind,
    CaptureEvaluationState,
    CapturePhase,
)
from onec_runtime.errors import (
    CaptureBusyError,
    CaptureEvaluationPendingError,
    CaptureOutcomeUnknownError,
    CaptureRecoveryRequiredError,
    CommandTimeout,
    StaleCaptureError,
    TargetLost,
)
from onec_runtime.rdbg.models import EvaluationResult, PendingEvaluation

from test_prototype_runtime import (
    CAPTURE_A,
    CAPTURE_B,
    SERVICE,
    TARGET,
    USER,
    ScriptedSession,
    captured_controller,
)


class ControlledCaptureSession(ScriptedSession):
    """Delay coordinator-owned evalExpr results without faking the controller."""

    def __init__(
        self,
        *,
        dispatch_error: BaseException | None = None,
        poll_error: BaseException | None = None,
        fail_workspace_on_call: int | None = None,
    ) -> None:
        super().__init__((CAPTURE_A,), fail_workspace_on_call=fail_workspace_on_call)
        self.dispatch_error = dispatch_error
        self.poll_error = poll_error
        self.accepted = Event()
        self.polling = Event()
        self.events: Queue[EvaluationResult | BaseException] = Queue()
        self.capture_start_count = 0
        self.capture_poll_count = 0
        self.capture_pending: PendingEvaluation | None = None
        self._controlled_owner = object()

    def start_evaluation(
        self,
        expression: str,
        *,
        max_text_size: int = 307_200,
        stack_level: int = 0,
    ) -> PendingEvaluation:
        del max_text_size
        self.calls.append(("start_evaluation", (expression, stack_level)))
        self.capture_start_count += 1
        if self.dispatch_error is not None:
            raise self.dispatch_error
        if self.capture_pending is not None:
            raise AssertionError("controller redispatched while one capability was pending")
        pending = PendingEvaluation(TARGET, uuid4(), self._controlled_owner)
        self.capture_pending = pending
        self.accepted.set()
        return pending

    def wait_evaluation_event(
        self,
        pending: PendingEvaluation,
        *,
        timeout_s: float,
    ) -> EvaluationResult:
        assert pending is self.capture_pending
        self.capture_poll_count += 1
        self.polling.set()
        if self.poll_error is not None:
            raise self.poll_error
        try:
            event = self.events.get(timeout=min(timeout_s, 0.005))
        except Empty:
            raise CommandTimeout("synthetic interval timeout") from None
        if isinstance(event, BaseException):
            raise event
        self.capture_pending = None
        return event

    def complete(
        self,
        presentation: str = "901",
        *,
        type_name: str = "Число",
        error: str = "",
    ) -> None:
        pending = self.capture_pending
        assert pending is not None
        self.events.put(EvaluationResult(
            pending.result_id,
            type_name,
            presentation,
            bool(error),
            error_text=error,
        ))


def eventually(predicate) -> None:  # type: ignore[no-untyped-def]
    deadline = monotonic() + 2
    while not predicate():
        assert monotonic() < deadline, "controller-owned evaluation did not settle"
        sleep(0.002)


def coordinator(controller):  # type: ignore[no-untyped-def]
    value = controller._capture_evaluation_coordinator
    assert value is not None
    return value


def fence(controller):  # type: ignore[no-untyped-def]
    value = controller._capture_evaluation_fence
    assert value is not None
    return value


def close_owner(controller, session: ControlledCaptureSession) -> None:  # type: ignore[no-untyped-def]
    owner = getattr(controller, "_capture_evaluation_coordinator", None)
    if owner is None:
        return
    owner.begin_close()
    session.poll_error = TargetLost("synthetic teardown")
    assert owner.join(2)


def test_acknowledged_timeout_keeps_one_pending_capability_and_rejects_redispatch() -> None:
    session = ControlledCaptureSession()
    controller = captured_controller(session, command_timeout_s=0.02)
    try:
        with pytest.raises(CaptureEvaluationPendingError) as caught:
            controller.execute_capture("РезультатИнструкции = 901;")

        assert session.accepted.is_set()
        assert session.capture_start_count == 1
        assert session.capture_pending is not None
        assert controller.state.value == "evaluating_capture"
        shielded = controller.breakpoint_workspace_owner.confirmed_snapshot
        assert shielded.shielded is True
        assert shielded.effective_locations == (SERVICE, USER)
        status = coordinator(controller).status(fence(controller))
        assert (
            status.phase,
            status.pending_evaluation_id,
            status.evaluation_kind,
        ) == (
            CapturePhase.EVALUATING,
            caught.value.evaluation_id,
            CaptureEvaluationKind.USER_BSL,
        )

        with pytest.raises(CaptureBusyError) as busy:
            controller.execute_capture("РезультатИнструкции = 902;")
        assert busy.value.evaluation_id == caught.value.evaluation_id
        assert session.capture_start_count == 1
        assert controller.breakpoint_workspace_owner.confirmed_snapshot.shielded is True
    finally:
        close_owner(controller, session)


def test_late_success_restores_exact_workspace_and_completed_output_without_redispatch() -> None:
    session = ControlledCaptureSession()
    controller = captured_controller(session, command_timeout_s=0.02)
    try:
        with pytest.raises(CaptureEvaluationPendingError) as caught:
            controller.execute_capture("РезультатИнструкции = 901;")
        session.complete()

        outcome = coordinator(controller).wait(
            fence(controller), caught.value.evaluation_id, timeout_s=1,
        )

        assert outcome.state is CaptureEvaluationState.COMPLETED
        assert outcome.result == 901
        assert controller.state.value == "captured"
        assert controller.pending_capture_evaluation is None
        assert session.capture_start_count == 1
        restored = controller.breakpoint_workspace_owner.confirmed_snapshot
        assert restored.shielded is False
        assert restored.effective_locations == (SERVICE, CAPTURE_A, CAPTURE_B, USER)
    finally:
        close_owner(controller, session)


def test_keyboard_interrupt_detaches_only_waiter_and_late_result_still_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = ControlledCaptureSession()
    controller = captured_controller(session, command_timeout_s=1)
    owner = coordinator(controller)
    original_wait = owner._condition.wait
    caller = current_thread()

    def interrupt_after_acceptance(timeout: float | None = None) -> bool:
        if current_thread() is caller:
            while not session.accepted.is_set():
                original_wait(0.001)
            raise KeyboardInterrupt
        return original_wait(timeout)

    try:
        monkeypatch.setattr(owner._condition, "wait", interrupt_after_acceptance)
        with pytest.raises(KeyboardInterrupt):
            controller.execute_capture("РезультатИнструкции = 901;")
        monkeypatch.setattr(owner._condition, "wait", original_wait)

        status = owner.status(fence(controller))
        assert status.phase is CapturePhase.EVALUATING
        assert status.evaluation_timing.initiating_waiter_detached_ms is not None
        assert session.capture_pending is not None
        assert controller.breakpoint_workspace_owner.confirmed_snapshot.shielded is True

        session.complete()
        outcome = owner.wait(fence(controller), status.pending_evaluation_id, timeout_s=1)
        assert outcome.state is CaptureEvaluationState.COMPLETED
        assert controller.state.value == "captured"
        assert session.capture_start_count == 1
    finally:
        monkeypatch.setattr(owner._condition, "wait", original_wait)
        close_owner(controller, session)


def test_late_bsl_error_is_retained_after_workspace_restore() -> None:
    session = ControlledCaptureSession()
    controller = captured_controller(session, command_timeout_s=0.02)
    try:
        with pytest.raises(CaptureEvaluationPendingError) as caught:
            controller.execute_capture('ВызватьИсключение "boom";')
        session.complete("boom", type_name="Ошибка", error="private platform text")

        outcome = coordinator(controller).wait(
            fence(controller), caught.value.evaluation_id, timeout_s=1,
        )

        assert outcome.state is CaptureEvaluationState.FAILED
        assert outcome.diagnostic is not None
        assert outcome.diagnostic.code == "bsl_error"
        assert controller.state.value == "captured"
        assert controller.breakpoint_workspace_owner.confirmed_snapshot.shielded is False
        assert "private platform text" not in repr(outcome)
        assert session.capture_start_count == 1
    finally:
        close_owner(controller, session)


def test_workspace_restoration_failure_requires_recovery_after_confirmed_result() -> None:
    session = ControlledCaptureSession(fail_workspace_on_call=3)
    controller = captured_controller(session, command_timeout_s=1)
    try:
        session.events.put(EvaluationResult(uuid4(), "Число", "901", False))
        # The controlled session correlates the queued result to the capability below.
        original_wait = session.wait_evaluation_event

        def correlated_wait(pending: PendingEvaluation, *, timeout_s: float) -> EvaluationResult:
            result = original_wait(pending, timeout_s=timeout_s)
            return EvaluationResult(pending.result_id, result.type_name, result.presentation, False)

        session.wait_evaluation_event = correlated_wait  # type: ignore[method-assign]
        with pytest.raises(CaptureRecoveryRequiredError):
            controller.execute_capture("РезультатИнструкции = 901;")

        status = coordinator(controller).status(fence(controller))
        assert status.phase is CapturePhase.RECOVERY_REQUIRED
        assert status.failure is not None
        assert status.failure.code == "workspace_restore_failed"
        assert controller.state.value == "breakpoint_restore_failure"
        assert session.capture_start_count == 1
    finally:
        close_owner(controller, session)


def test_target_loss_stales_capture_without_workspace_restore() -> None:
    session = ControlledCaptureSession(poll_error=TargetLost("private target"))
    controller = captured_controller(session, command_timeout_s=1)
    try:
        with pytest.raises(StaleCaptureError):
            controller.execute_capture("РезультатИнструкции = 901;")

        assert controller.state.value == "lost"
        assert coordinator(controller).status(fence(controller)).phase is CapturePhase.STALE
        assert controller.breakpoint_workspace_owner.confirmed_snapshot.shielded is True
        assert session.capture_start_count == 1
    finally:
        close_owner(controller, session)


def test_transport_entry_without_known_acceptance_is_the_only_outcome_unknown_case() -> None:
    session = ControlledCaptureSession(dispatch_error=OSError("private uncertain dispatch"))
    controller = captured_controller(session, command_timeout_s=1)
    try:
        with pytest.raises(CaptureOutcomeUnknownError):
            controller.execute_capture("РезультатИнструкции = 901;")

        status = coordinator(controller).status(fence(controller))
        assert status.phase is CapturePhase.OUTCOME_UNKNOWN
        assert status.failure is not None
        assert status.failure.code == "dispatch_uncertain"
        assert controller.state.value == "recovering"
        assert session.capture_start_count == 1
        assert session.capture_pending is None
        assert controller.breakpoint_workspace_owner.confirmed_snapshot.shielded is True
    finally:
        close_owner(controller, session)


@pytest.mark.parametrize(
    ("invoke", "expected_kind", "late_type", "late_value"),
    [
        (
            lambda controller: controller.execute_system_capture("Результат = 1;"),
            CaptureEvaluationKind.MATERIALIZATION_HELPER,
            "Число",
            "1",
        ),
        (
            lambda controller: controller.resolve_capture_manager_origin("Скаляр", ()),
            CaptureEvaluationKind.INSPECTION,
            "Булево",
            "Истина",
        ),
        (
            lambda controller: controller.take_context_string(
                "__onec_value_" + "a" * 32, max_text_size=4096,
            ),
            CaptureEvaluationKind.MATERIALIZATION_HELPER,
            "Строка",
            '"payload"',
        ),
        (
            lambda controller: controller.install_capture_worker_generation_pin(
                "a" * 64,
            ),
            CaptureEvaluationKind.MATERIALIZATION_HELPER,
            "Булево",
            "Истина",
        ),
        (
            lambda controller: controller.clear_capture_worker_generation_pin(),
            CaptureEvaluationKind.MATERIALIZATION_HELPER,
            "Булево",
            "Истина",
        ),
        (
            lambda controller: controller.drop_context_value(
                "__onec_value_" + "a" * 32,
            ),
            CaptureEvaluationKind.MATERIALIZATION_HELPER,
            "Неопределено",
            "Неопределено",
        ),
    ],
)
def test_controller_owned_internal_evaluations_use_explicit_kind_and_same_owner(
    invoke, expected_kind, late_type: str, late_value: str,  # type: ignore[no-untyped-def]
) -> None:
    session = ControlledCaptureSession()
    controller = captured_controller(session, command_timeout_s=0.02)
    try:
        with pytest.raises(CaptureEvaluationPendingError) as caught:
            invoke(controller)
        assert caught.value.evaluation_kind is expected_kind
        status = coordinator(controller).status(fence(controller))
        assert status.evaluation_kind is expected_kind
        assert status.phase is CapturePhase.EVALUATING
        assert session.capture_start_count == 1

        session.complete(late_value, type_name=late_type)
        outcome = coordinator(controller).wait(
            fence(controller), caught.value.evaluation_id, timeout_s=1,
        )
        assert outcome.state is CaptureEvaluationState.COMPLETED
        assert outcome.result is None
        assert controller.state.value == "captured"
        assert session.capture_start_count == 1
    finally:
        close_owner(controller, session)
