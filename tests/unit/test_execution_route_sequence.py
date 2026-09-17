"""Composition contract for one worker across MAIN and CAPTURE routes.

This test exercises the new components directly. The public RuntimeApi cutover
is covered by the separate runtime integration contract.
"""

from collections import deque
from threading import current_thread
from uuid import UUID

from onec_runtime.execution.arbiter import RdbgArbiter, RouteToken, Settlement
from onec_runtime.execution.capture.adapter import CaptureSetupAdapter
from onec_runtime.execution.capture.cell_evaluator import CaptureCellEvaluator
from onec_runtime.execution.capture.executor import CaptureExecutor
from onec_runtime.execution.capture.scope import CaptureScope
from onec_runtime.execution.main import MainExecutor, MainOperation, MainPhase
from onec_runtime.rdbg.models import (
    DebugTarget,
    EvaluationResult,
    FrameVariable,
    LocalVariablesResult,
    ModifyResult,
    ModuleLocation,
    PendingEvaluation,
    StackFrame,
    StopEvent,
    TargetId,
)
from onec_runtime.table_value import evaluation_to_python


TARGET = TargetId(UUID(int=1), "route-test")
BUSINESS = ModuleLocation("ExtensionModule", "", UUID(int=2), UUID(int=3), 50, "Runtime")
KERNEL = ModuleLocation("ExtensionModule", "", UUID(int=2), UUID(int=3), 60, "Runtime")
CAPTURE_STOP = StopEvent(
    TARGET,
    BUSINESS,
    "callStackFormed",
    stack=(BUSINESS, BUSINESS, KERNEL),
    stack_frames=tuple(
        StackFrame(TARGET, level, location)
        for level, location in enumerate((BUSINESS, BUSINESS, KERNEL))
    ),
)
MAIN_STOP = StopEvent(TARGET, KERNEL, "callStackFormed")


class RouteSession:
    def __init__(self) -> None:
        self.target = DebugTarget(TARGET, "CLIENT", "stopped", 1)
        self.stops = deque((CAPTURE_STOP, MAIN_STOP))
        self.calls: list[tuple[str, object]] = []
        self.pending: PendingEvaluation | None = None
        self.expression = ""
        self.next_result_id = 10

    def _record(self, method: str) -> None:
        self.calls.append((method, current_thread()))

    def set_breakpoints(self, locations, *, on_transport_dispatch):
        on_transport_dispatch()
        self._record("set_breakpoints")

    def modify(self, variable, value_expression, *, on_transport_dispatch):
        on_transport_dispatch()
        self._record("modify")
        return ModifyResult(UUID(int=4), "Булево", "Истина", False)

    def continue_(self, *, on_transport_dispatch):
        on_transport_dispatch()
        self._record("continue")

    def wait_for_any_stop(self, *, timeout_s, expected_target, on_transport_dispatch):
        on_transport_dispatch()
        self._record("wait_stop")
        assert expected_target == TARGET
        return self.stops.popleft()

    def local_variables(self, stack_level=0, *, timeout_s, on_transport_dispatch):
        on_transport_dispatch()
        self._record("locals")
        if stack_level == 0:
            names = ("Amount",)
        elif stack_level == 1:
            names = ()
        else:
            names = ("Контекст", "ТекущаяИнструкция", "ИдентификаторКоманды")
        return LocalVariablesResult(
            UUID(int=5), tuple(FrameVariable(name, "Строка", "") for name in names)
        )

    def start_evaluation(
        self, expression, *, max_text_size, stack_level, timeout_s, on_transport_dispatch
    ):
        assert self.pending is None
        on_transport_dispatch()
        self._record("start_eval")
        self.expression = expression
        self.next_result_id += 1
        self.pending = PendingEvaluation(TARGET, UUID(int=self.next_result_id), self)
        return self.pending

    def wait_evaluation_event(self, pending, *, timeout_s, on_transport_dispatch):
        assert pending is self.pending
        on_transport_dispatch()
        self._record("wait_eval")
        expression = self.expression
        self.pending = None
        if expression.startswith("ПоместитьВоВременноеХранилище("):
            return EvaluationResult(pending.result_id, "Строка", '"temporary-address"', False)
        if expression == "ИдентификаторКоманды":
            return EvaluationResult(pending.result_id, "Число", "1", False)
        return EvaluationResult(pending.result_id, "Булево", "Истина", False)


def test_components_handoff_main_capture_cell_and_resume_on_one_rdbg_worker() -> None:
    session = RouteSession()
    main_route = RouteToken("runtime-1", 1, 0, "main-1")
    capture_route = RouteToken("runtime-1", 2, 0, "capture-1")
    resumed_route = RouteToken("runtime-1", 3, 0, "main-1")
    arbiter = RdbgArbiter(session, main_route)
    main = MainOperation(1, TARGET)
    main_executor = MainExecutor(poll_interval_s=0.1)
    capture_executor = CaptureExecutor(None, KERNEL, decode_command_id=evaluation_to_python)
    scopes: list[CaptureScope] = []

    def start(port):
        stop = main_executor.dispatch(
            main,
            "Результат = 1;",
            install_workspace=lambda: port.set_breakpoints((KERNEL, BUSINESS)),
            before_command_write=lambda: None,
            before_continue=lambda: None,
            port=port,
        )
        assert stop is CAPTURE_STOP
        main.stopped(stop, MainPhase.SUSPENDED_CAPTURE)
        port.handoff_route(capture_route)
        scope = CaptureScope.from_stop(1, 1, stop, 1)
        capture_executor.open_scope(scope, port=CaptureSetupAdapter(port))
        scope.mark_ready()
        scopes.append(scope)
        return Settlement(scope)

    try:
        first = arbiter.submit(main_route, start)
        arbiter.dispatch(first)
        scope = first.wait(3)
        assert scope is scopes[0]
        assert main.phase is MainPhase.SUSPENDED_CAPTURE
        assert arbiter.current_route == capture_route

        cell = arbiter.submit(
            capture_route,
            lambda port: Settlement(
                CaptureCellEvaluator().evaluate(scope, "Результат = 2;", port=port)
            ),
        )
        arbiter.dispatch(cell)
        assert cell.wait(3).error_occurred is False
        assert scope.published

        def resume(port):
            main_executor.continue_command(main, port=port)
            port.handoff_route(resumed_route)
            return Settlement(main_executor.await_stop(port=port))

        third = arbiter.submit(capture_route, resume)
        arbiter.dispatch(third)
        assert third.wait(3) is MAIN_STOP
        assert main.phase is MainPhase.RUNNING
        assert arbiter.current_route == resumed_route
        assert len({thread for _, thread in session.calls}) == 1
        assert next(iter({thread for _, thread in session.calls})).name == "rdbg-arbiter"
    finally:
        arbiter.close(timeout=3)
