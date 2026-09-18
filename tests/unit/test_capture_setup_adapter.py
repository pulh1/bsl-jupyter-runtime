"""CAPTURE setup uses one owned pending eval per expression."""

from uuid import UUID

import pytest

from onec_runtime.execution.arbiter import OutcomeUnknown
from onec_runtime.execution.capture.executor import CaptureExecutor
from onec_runtime.execution.capture.scope import CaptureScope, CaptureSetupStage
from onec_runtime.execution.evaluation import EvaluationSuspended
from onec_runtime.errors import CommandTimeout
from onec_runtime.rdbg.models import (
    EvaluationResult,
    FrameVariable,
    LocalVariablesResult,
    ModuleLocation,
    PendingEvaluation,
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


class WorkerPort:
    """The fake exposes only worker capabilities, never synchronous evaluate."""

    def __init__(self, *, first_event: StopEvent | BaseException | None = None) -> None:
        self.first_event = first_event
        self.local_levels: list[int] = []
        self.starts: list[tuple[str, int, int, float, PendingEvaluation]] = []
        self.waits: list[tuple[PendingEvaluation, float]] = []
        self.active: PendingEvaluation | None = None
        self.waits_for_active = 0

    def local_variables(self, stack_level: int = 0) -> LocalVariablesResult:
        self.local_levels.append(stack_level)
        if stack_level == 0:
            variables = (FrameVariable("Amount", "Число", "7"),)
        elif stack_level == 1:
            variables = ()
        else:
            variables = tuple(
                FrameVariable(name, "Строка", "")
                for name in ("e1cRuntimeКонтекст", "ТекущаяИнструкция", "ИдентификаторКоманды")
            )
        return LocalVariablesResult(UUID(int=20 + stack_level), variables)

    def start_evaluation(
        self,
        expression: str,
        *,
        max_text_size: int,
        stack_level: int,
        timeout_s: float,
    ) -> PendingEvaluation:
        assert self.active is None
        pending = PendingEvaluation(TARGET, UUID(int=30 + len(self.starts)), self)
        self.active = pending
        self.waits_for_active = 0
        self.starts.append((expression, max_text_size, stack_level, timeout_s, pending))
        return pending

    def wait_evaluation_event(
        self, pending: PendingEvaluation, *, timeout_s: float
    ) -> EvaluationResult | StopEvent:
        assert pending is self.active
        self.waits.append((pending, timeout_s))
        self.waits_for_active += 1
        if self.first_event is not None:
            event = self.first_event
            self.first_event = None
            if isinstance(event, BaseException):
                raise event
            return event
        if self.waits_for_active <= 2:
            raise CommandTimeout("empty event interval")
        expression = self.starts[-1][0]
        if expression.startswith("ПоместитьВоВременноеХранилище("):
            result = EvaluationResult(pending.result_id, "Строка", '"temporary-address"', False)
        elif expression == "ИдентификаторКоманды":
            result = EvaluationResult(pending.result_id, "Число", "42", False)
        elif "НачатьКонтекстОтладки" in expression:
            result = EvaluationResult(pending.result_id, "Булево", "Истина", False)
        else:
            raise AssertionError(expression)
        self.active = None
        return result


def _open_scope(worker: WorkerPort) -> CaptureScope:
    from onec_runtime.execution.capture.adapter import CaptureSetupAdapter

    scope = CaptureScope.from_stop(7, 42, STOP, 3)
    executor = CaptureExecutor(None, KERNEL, decode_command_id=evaluation_to_python)
    executor.open_scope(
        scope,
        port=CaptureSetupAdapter(worker, request_timeout_s=3, wait_interval_s=0.25),
    )
    return scope


def test_capture_setup_reuses_each_pending_across_bounded_wait_intervals() -> None:
    worker = WorkerPort()

    scope = _open_scope(worker)

    assert scope.setup_stage is CaptureSetupStage.CONTEXT_BEGUN
    assert worker.local_levels == [0, 1, 2]
    assert len(worker.starts) == 3
    assert worker.starts[0][0].startswith("ПоместитьВоВременноеХранилище(")
    assert worker.starts[1][0] == "ИдентификаторКоманды"
    assert "НачатьКонтекстОтладки" in worker.starts[2][0]
    assert [start[2] for start in worker.starts] == [0, 2, 2]
    assert all(start[1] == 307_200 and start[3] == 3 for start in worker.starts)
    assert worker.waits == [
        (start[4], 0.25)
        for start in worker.starts
        for _ in range(3)
    ]


def test_capture_setup_propagates_stop_with_original_pending_capability() -> None:
    worker = WorkerPort(first_event=STOP)

    with pytest.raises(EvaluationSuspended) as caught:
        _open_scope(worker)

    assert caught.value.pending is worker.active
    assert caught.value.stop is STOP
    assert len(worker.starts) == 1
    assert worker.waits == [(worker.active, 0.25)]


def test_capture_setup_does_not_retry_unknown_transport_outcome() -> None:
    uncertain = OutcomeUnknown("poll outcome unknown")
    worker = WorkerPort(first_event=uncertain)

    with pytest.raises(OutcomeUnknown) as caught:
        _open_scope(worker)

    assert caught.value is uncertain
    assert worker.active is worker.starts[0][-1]
    assert len(worker.starts) == 1
    assert worker.waits == [(worker.active, 0.25)]
