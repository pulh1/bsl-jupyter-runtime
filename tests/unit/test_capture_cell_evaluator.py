"""The user CAPTURE expression runs once through an owned worker port."""

from uuid import UUID

import pytest

from onec_runtime.errors import CommandTimeout, RdbgTransportTimeout
from onec_runtime.execution.arbiter import OutcomeUnknown
from onec_runtime.execution.capture.scope import CaptureScope
from onec_runtime.execution.evaluation import EvaluationSuspended
from onec_runtime.rdbg.models import (
    EvaluationResult,
    ModuleLocation,
    PendingEvaluation,
    StackFrame,
    StopEvent,
    TargetId,
)


TARGET = TargetId(UUID(int=1), "test")
BUSINESS = ModuleLocation("ExtensionModule", "", UUID(int=2), UUID(int=3), 50, "Runtime")
KERNEL = ModuleLocation("ExtensionModule", "", UUID(int=2), UUID(int=3), 60, "Runtime")
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
PENDING = PendingEvaluation(TARGET, UUID(int=4), object())


def ready_scope() -> CaptureScope:
    scope = CaptureScope.from_stop(7, 42, STOP, 3)
    scope.record_locals(())
    scope.record_transfer("temporary-address")
    scope.record_kernel_frame(2)
    assert scope.record_main_command(42)
    scope.record_context_begun()
    scope.mark_ready()
    return scope


class WorkerPort:
    def __init__(self, events: list[object]) -> None:
        self.events = events
        self.starts: list[tuple[str, int, int, float]] = []
        self.waits: list[tuple[PendingEvaluation, float]] = []

    def start_evaluation(
        self,
        expression: str,
        *,
        max_text_size: int,
        stack_level: int,
        timeout_s: float,
    ) -> PendingEvaluation:
        self.starts.append((expression, max_text_size, stack_level, timeout_s))
        return PENDING

    def wait_evaluation_event(
        self, pending: PendingEvaluation, *, timeout_s: float
    ) -> EvaluationResult | StopEvent:
        assert pending is PENDING
        self.waits.append((pending, timeout_s))
        event = self.events.pop(0)
        if isinstance(event, BaseException):
            raise event
        assert isinstance(event, (EvaluationResult, StopEvent))
        return event


def _evaluate(scope: CaptureScope, worker: WorkerPort) -> EvaluationResult:
    from onec_runtime.execution.capture.cell_evaluator import CaptureCellEvaluator

    return CaptureCellEvaluator(
        request_timeout_s=3, wait_interval_s=0.25
    ).evaluate(scope, 'Результат = "ok";', port=worker)


def test_user_capture_cell_dispatches_once_and_waits_without_execution_deadline() -> None:
    result = EvaluationResult(PENDING.result_id, "Булево", "Истина", False)
    worker = WorkerPort([CommandTimeout("empty interval"), CommandTimeout("empty interval"), result])

    assert _evaluate(ready_scope(), worker) is result

    assert worker.starts == [
        ('RuntimeKernelServer.ВыполнитьКодВКонтекстеОтладки(e1cRuntimeКонтекст, '
         '"Результат = ""ok"";")', 307_200, 2, 3)
    ]
    assert worker.waits == [(PENDING, 0.25)] * 3


def test_user_capture_cell_returns_confirmed_bsl_error_to_result_policy() -> None:
    error_result = EvaluationResult(PENDING.result_id, "Неопределено", "", True, "BSL failed")
    worker = WorkerPort([error_result])

    assert _evaluate(ready_scope(), worker) is error_result
    assert len(worker.starts) == 1


def test_user_capture_cell_propagates_stop_with_the_original_pending_capability() -> None:
    worker = WorkerPort([STOP])

    with pytest.raises(EvaluationSuspended) as caught:
        _evaluate(ready_scope(), worker)

    assert caught.value.pending is PENDING
    assert caught.value.stop is STOP
    assert len(worker.starts) == 1
    assert worker.waits == [(PENDING, 0.25)]


@pytest.mark.parametrize(
    "uncertain",
    [RdbgTransportTimeout("RDBG request timed out"), OutcomeUnknown("remote outcome unknown")],
)
def test_user_capture_cell_never_retries_unknown_remote_outcome(uncertain: BaseException) -> None:
    worker = WorkerPort([uncertain])

    with pytest.raises(type(uncertain)) as caught:
        _evaluate(ready_scope(), worker)

    assert caught.value is uncertain
    assert len(worker.starts) == 1
    assert worker.waits == [(PENDING, 0.25)]


def test_user_capture_cell_requires_ready_context_before_dispatch() -> None:
    scope = CaptureScope.from_stop(7, 42, STOP, 3)
    worker = WorkerPort([])

    with pytest.raises(RuntimeError, match="ready"):
        _evaluate(scope, worker)

    assert worker.starts == []
