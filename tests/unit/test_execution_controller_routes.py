"""The controller keeps one MAIN operation across a CAPTURE stop."""

from collections import deque

from onec_runtime.execution.arbiter import RdbgArbiter, RouteToken
from onec_runtime.execution.capture.cell_evaluator import CaptureCellEvaluator
from onec_runtime.execution.capture.executor import CaptureExecutor
from onec_runtime.execution.main import MainExecutor, MainPhase
from onec_runtime.rdbg.models import EvaluationResult, StopEvent
from onec_runtime.stop_routing import BreakpointRegistry
from onec_runtime.table_value import evaluation_to_python

from test_execution_route_sequence import (
    BUSINESS,
    CAPTURE_STOP,
    KERNEL,
    MAIN_STOP,
    RouteSession,
    TARGET,
)


class CompleteSession(RouteSession):
    def __init__(self, capture_count: int = 1) -> None:
        super().__init__()
        self.stops = deque(
            [
                StopEvent(
                    CAPTURE_STOP.target_id,
                    CAPTURE_STOP.location,
                    CAPTURE_STOP.reason,
                    stop_by_breakpoint=True,
                    stack=CAPTURE_STOP.stack,
                    stack_frames=CAPTURE_STOP.stack_frames,
                )
                for _ in range(capture_count)
            ]
            + [StopEvent(TARGET, KERNEL, MAIN_STOP.reason, stop_by_breakpoint=True)]
        )

    def wait_evaluation_event(self, pending, *, timeout_s, on_transport_dispatch):
        on_transport_dispatch()
        if self.expression == "ЗавершеннаяКоманда":
            self._record("wait_eval")
            self.pending = None
            return EvaluationResult(pending.result_id, "Число", "1", False)
        if self.expression == "Результат":
            self._record("wait_eval")
            self.pending = None
            return EvaluationResult(pending.result_id, "Число", "3", False)
        if self.expression == "Ошибка":
            self._record("wait_eval")
            self.pending = None
            return EvaluationResult(pending.result_id, "Строка", '""', False)
        return super().wait_evaluation_event(
            pending, timeout_s=timeout_s,
            on_transport_dispatch=lambda: None,
        )


def test_controller_runs_main_capture_cell_resume_and_completion_with_one_owner() -> None:
    from onec_runtime.execution.controller.controller import (
        ExecutionController,
        MainYieldKind,
    )

    session = CompleteSession()
    route = RouteToken("runtime-1", 1, 0, "main")
    arbiter = RdbgArbiter(session, route)
    controller = ExecutionController(
        arbiter,
        MainExecutor(poll_interval_s=0.1),
        CaptureExecutor(None, KERNEL, decode_command_id=evaluation_to_python),
        CaptureCellEvaluator(),
        BreakpointRegistry(KERNEL, (BUSINESS,)),
        runtime_generation=1,
    )
    try:
        main_ticket = controller.submit_main("Результат = 1;")
        first = main_ticket.wait(3)
        assert first.kind is MainYieldKind.CAPTURE
        main = controller.main_operation
        assert main is not None and main.command_id == 1
        assert main.phase is MainPhase.SUSPENDED_CAPTURE
        scope = controller.capture_scope
        assert scope is first.scope and scope.published

        cell_ticket = controller.submit_capture_cell("Результат = 2;")
        cell_result = cell_ticket.wait(3)
        assert cell_result.error_occurred is False
        assert controller.main_operation is main
        assert controller.capture_scope is scope

        resume_ticket = controller.submit_resume()
        completed = resume_ticket.wait(3)
        assert completed.kind is MainYieldKind.COMPLETED
        assert completed.completion.result == 3
        assert main.phase is MainPhase.COMPLETED
        assert controller.capture_scope is None
        assert scope.frame_identity.value == "released"
        assert len({thread for _, thread in session.calls}) == 1
    finally:
        arbiter.close(timeout=3)


def test_second_capture_stop_has_new_scope_but_same_main_operation() -> None:
    from onec_runtime.execution.controller.controller import (
        ExecutionController,
        MainYieldKind,
    )

    session = CompleteSession(capture_count=2)
    arbiter = RdbgArbiter(session, RouteToken("runtime-1", 1, 0, "main"))
    controller = ExecutionController(
        arbiter,
        MainExecutor(poll_interval_s=0.1),
        CaptureExecutor(None, KERNEL, decode_command_id=evaluation_to_python),
        CaptureCellEvaluator(),
        BreakpointRegistry(KERNEL, (BUSINESS,)),
        runtime_generation=1,
    )
    try:
        first = controller.submit_main("Результат = 1;").wait(3)
        assert first.kind is MainYieldKind.CAPTURE
        first_scope = first.scope
        main = first.operation

        second = controller.submit_resume().wait(3)
        assert second.kind is MainYieldKind.CAPTURE
        assert second.operation is main
        assert second.scope is not first_scope
        assert second.scope.identity.local_stop_sequence == 2
        assert first_scope.frame_identity.value == "released"

        completed = controller.submit_resume().wait(3)
        assert completed.kind is MainYieldKind.COMPLETED
        assert completed.operation is main
        assert main.phase is MainPhase.COMPLETED
        assert len({thread for _, thread in session.calls}) == 1
    finally:
        arbiter.close(timeout=3)
