"""Public lifecycle evidence for confirmed CAPTURE setup failures."""

import pytest

from onec_runtime.errors import BslExecutionError, ProtocolError
from onec_runtime.prototype_runtime import OperationState, PrototypeRuntimeController
from onec_runtime.rdbg.models import EvaluationResult
from onec_runtime.runtime_api import PrototypeRuntimeApi

from test_prototype_runtime import CAPTURE_A, SERVICE, ScriptedSession, evaluation


class FailingSetupSession(ScriptedSession):
    def __init__(self, failure: str) -> None:
        stack = (
            (CAPTURE_A,)
            if failure == "kernel_frame"
            else (CAPTURE_A, CAPTURE_A, SERVICE)
        )
        super().__init__((CAPTURE_A,), stacks=(stack,))
        self.failure = failure

    def evaluate(
        self,
        expression: str,
        *,
        stack_level: int = 0,
        timeout_s: float = 30.0,
        max_text_size: int = 307_200,
    ) -> EvaluationResult:
        if expression.startswith("ПоместитьВоВременноеХранилище("):
            if self.failure == "transfer_bsl":
                self.calls.append(("evaluate", expression))
                return evaluation("Ошибка", "Ошибка", error="transfer rejected")
            if self.failure == "transfer_payload":
                self.calls.append(("evaluate", expression))
                return evaluation("Число", "7")
        if "НачатьКонтекстОтладки" in expression and self.failure == "begin_bsl":
            self.calls.append(("evaluate", (expression, stack_level)))
            return evaluation("Ошибка", "Ошибка", error="begin rejected")
        return super().evaluate(
            expression,
            stack_level=stack_level,
            timeout_s=timeout_s,
            max_text_size=max_text_size,
        )


@pytest.mark.parametrize(
    ("failure", "error_type", "message", "stage", "frame_identity"),
    [
        (
            "transfer_bsl",
            BslExecutionError,
            "transfer rejected",
            "locals_read",
            "unverified",
        ),
        (
            "transfer_payload",
            ProtocolError,
            "temporary-storage address",
            "locals_read",
            "unverified",
        ),
        (
            "kernel_frame",
            ProtocolError,
            "kernel context frame",
            "context_transferred",
            "unverified",
        ),
        (
            "begin_bsl",
            BslExecutionError,
            "begin rejected",
            "main_id_confirmed",
            "confirmed",
        ),
    ],
)
def test_confirmed_capture_setup_error_preserves_stop_and_stage(
    failure: str,
    error_type: type[Exception],
    message: str,
    stage: str,
    frame_identity: str,
) -> None:
    """A confirmed setup rejection must not report the suspended MAIN as lost."""
    session = FailingSetupSession(failure)
    controller = PrototypeRuntimeController(session, SERVICE)
    api = PrototypeRuntimeApi(controller, capture_points=(CAPTURE_A,))

    with pytest.raises(error_type, match=message):
        api.execute_bsl("Результат = 1;")

    status = api.status()
    assert status.state is OperationState.CAPTURE_SETUP_FAILED
    assert status.capture_setup is not None
    assert status.capture_setup.context_state.value == "setup_failed"
    assert status.capture_setup.setup_stage.value == stage
    assert status.capture_setup.frame_identity.value == frame_identity
    assert controller.main_operation is not None
    assert not controller.main_operation.terminal
    assert controller.capture_scope is not None
    assert controller.main_operation.pending_stop is controller.capture_scope.stop

    before = tuple(session.calls)
    with pytest.raises(ProtocolError):
        api.execute_bsl("Результат = 2;")
    with pytest.raises(ProtocolError):
        api.resume_capture()
    assert tuple(session.calls) == before
