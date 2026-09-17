"""CAPTURE stop setup is an executor sequence over a narrow debugger port."""

from uuid import UUID

import pytest

from onec_runtime.errors import BslExecutionError, ProtocolError
from onec_runtime.execution.capture.executor import (
    CaptureCommandMismatchError,
    CaptureExecutor,
)
from onec_runtime.execution.capture.scope import (
    CaptureContextState,
    CaptureFrameIdentity,
    CaptureScope,
    CaptureSetupStage,
)
from onec_runtime.rdbg.models import (
    EvaluationResult,
    FrameVariable,
    LocalVariablesResult,
    ModuleLocation,
    StackFrame,
    StopEvent,
    TargetId,
)
from onec_runtime.table_value import evaluation_to_python


TARGET = TargetId(UUID(int=1), "test")
KERNEL = ModuleLocation("ExtensionModule", "", UUID(int=2), UUID(int=3), 60, "Runtime")
BUSINESS = ModuleLocation("ExtensionModule", "", UUID(int=2), UUID(int=3), 50, "Runtime")
STOP = StopEvent(
    TARGET,
    BUSINESS,
    "callStackFormed",
    stack=(BUSINESS, BUSINESS, KERNEL),
    stack_frames=tuple(
        StackFrame(TARGET, level, location)
        for level, location in enumerate((BUSINESS, BUSINESS, KERNEL))
    ),
)


def evaluated(type_name: str, presentation: str, error: str = "") -> EvaluationResult:
    return EvaluationResult(UUID(int=4), type_name, presentation, bool(error), error)


class SetupPort:
    def __init__(self, command_id: int = 42, *, begin_error: str = "") -> None:
        self.command_id = command_id
        self.begin_error = begin_error
        self.calls: list[tuple[str, object]] = []

    def local_variables(self, stack_level: int = 0) -> LocalVariablesResult:
        self.calls.append(("locals", stack_level))
        if stack_level == 0:
            return LocalVariablesResult(
                UUID(int=5), (FrameVariable("Amount", "Число", "7"),)
            )
        if stack_level == 1:
            return LocalVariablesResult(UUID(int=6), ())
        return LocalVariablesResult(
            UUID(int=6),
            tuple(
                FrameVariable(name, "Строка", "")
                for name in ("Контекст", "ТекущаяИнструкция", "ИдентификаторКоманды")
            ),
        )

    def evaluate(self, expression: str, *, stack_level: int = 0) -> EvaluationResult:
        self.calls.append(("eval", (expression, stack_level)))
        if expression.startswith("ПоместитьВоВременноеХранилище("):
            return evaluated("Строка", '"temporary-address"')
        if expression == "ИдентификаторКоманды":
            return evaluated("Число", str(self.command_id))
        if "НачатьКонтекстОтладки" in expression:
            return evaluated("Булево", "Истина", self.begin_error)
        raise AssertionError(expression)


def test_executor_prepares_scope_in_order_without_publishing_it() -> None:
    port = SetupPort()
    scope = CaptureScope.from_stop(7, 42, STOP, 3)

    opened = CaptureExecutor(port, KERNEL, decode_command_id=evaluation_to_python).open_scope(scope)

    assert opened.variables == scope.frame_variables
    assert opened.observed_command_id == 42
    assert scope.context_state is CaptureContextState.OPENING
    assert scope.frame_identity is CaptureFrameIdentity.CONFIRMED
    assert scope.setup_stage is CaptureSetupStage.CONTEXT_BEGUN
    assert scope.published is False
    assert [kind for kind, _ in port.calls] == [
        "locals", "eval", "locals", "locals", "eval", "eval"
    ]
    assert port.calls[3] == ("locals", 2)
    assert port.calls[4] == ("eval", ("ИдентификаторКоманды", 2))
    assert port.calls[5][1][1] == 2


def test_long_lived_capture_executor_uses_the_port_of_each_stop() -> None:
    executor = CaptureExecutor(None, KERNEL, decode_command_id=evaluation_to_python)
    first_port = SetupPort()
    second_port = SetupPort()
    first_scope = CaptureScope.from_stop(7, 42, STOP, 3)
    second_scope = CaptureScope.from_stop(7, 42, STOP, 4)

    executor.open_scope(first_scope, port=first_port)
    assert len(first_port.calls) == 6
    assert second_port.calls == []

    executor.open_scope(second_scope, port=second_port)
    assert len(first_port.calls) == 6
    assert len(second_port.calls) == 6
    assert first_scope.setup_stage is CaptureSetupStage.CONTEXT_BEGUN
    assert second_scope.setup_stage is CaptureSetupStage.CONTEXT_BEGUN


def test_executor_rejects_other_main_command_before_opening_context() -> None:
    port = SetupPort(command_id=41)
    scope = CaptureScope.from_stop(7, 42, STOP, 3)

    with pytest.raises(CaptureCommandMismatchError, match="Captured command 41"):
        CaptureExecutor(port, KERNEL, decode_command_id=evaluation_to_python).open_scope(scope)

    assert scope.observed_main_command_id == 41
    assert scope.frame_identity is CaptureFrameIdentity.UNVERIFIED
    assert scope.setup_stage is CaptureSetupStage.KERNEL_FRAME_FOUND
    assert not any("НачатьКонтекстОтладки" in str(call) for call in port.calls)


def test_executor_reports_begin_failure_without_changing_scope_lifecycle() -> None:
    port = SetupPort(begin_error="planned BSL failure")
    scope = CaptureScope.from_stop(7, 42, STOP, 3)

    with pytest.raises(BslExecutionError, match="planned BSL failure"):
        CaptureExecutor(port, KERNEL, decode_command_id=evaluation_to_python).open_scope(scope)

    assert scope.frame_identity is CaptureFrameIdentity.CONFIRMED
    assert scope.context_state is CaptureContextState.OPENING
    assert scope.setup_stage is CaptureSetupStage.MAIN_ID_CONFIRMED


def test_executor_requires_exact_stack_mapping() -> None:
    malformed_stop = StopEvent(TARGET, BUSINESS, "callStackFormed", stack=(BUSINESS,))
    scope = CaptureScope.from_stop(7, 42, malformed_stop, 3)
    port = SetupPort()

    with pytest.raises(ProtocolError, match="exact stack mapping"):
        CaptureExecutor(port, KERNEL, decode_command_id=evaluation_to_python).open_scope(scope)

    assert scope.setup_stage is CaptureSetupStage.CONTEXT_TRANSFERRED
