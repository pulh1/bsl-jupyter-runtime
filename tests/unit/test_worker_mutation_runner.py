"""Worker mutation helpers use the admitted arbiter port on each route."""

from __future__ import annotations

from threading import current_thread
from uuid import UUID

from onec_runtime.breakpoint_workspace import (
    BreakpointWorkspaceController, WorkspaceSnapshot,
)
from onec_runtime.errors import BslExecutionError, EvaluationDispatchUnknown
from onec_runtime.execution.arbiter import OutcomeUnknown, RdbgArbiter, RouteToken, Settlement
from onec_runtime.execution.breakpoint_routes import RouteBreakpointWorkspace
from onec_runtime.execution.capture.cell_evaluator import CaptureCellEvaluator
from onec_runtime.execution.capture.operation_executor import CaptureCellOperationExecutor
from onec_runtime.execution.capture.scope import CaptureContextState, CaptureScope
from onec_runtime.execution.main import MainExecutor, MainOperation, MainPhase
from onec_runtime.rdbg.models import EvaluationResult, FrameVariable, ModuleLocation
from onec_runtime.stop_routing import BreakpointRegistry

from test_execution_controller_routes import CompleteSession
from test_execution_route_sequence import BUSINESS, CAPTURE_STOP, KERNEL, TARGET


WORKER_POINT = ModuleLocation(
    "ExtensionModule", "", UUID(int=41), UUID(int=42), 110, "Worker",
)


def _runner(route_provider, session, *, registry_provider=None):
    from onec_runtime.execution.worker_mutation import WorkerMutationInstructionRunner

    registry = BreakpointRegistry(KERNEL, (BUSINESS,))
    workspace = RouteBreakpointWorkspace(BreakpointWorkspaceController(
        session,
        WorkspaceSnapshot(0, KERNEL, registry.captures, (), (WORKER_POINT,), False),
    ))
    return WorkerMutationInstructionRunner(
        MainExecutor(poll_interval_s=0.1),
        CaptureCellOperationExecutor(CaptureCellEvaluator()),
        registry_provider=registry_provider or (lambda: registry),
        breakpoint_routes=workspace,
        route_provider=route_provider,
    )


def _capture_scope(command_id: int) -> CaptureScope:
    scope = CaptureScope.from_stop(1, command_id, CAPTURE_STOP, 1)
    scope.record_locals((FrameVariable("Amount", "Число", "3"),))
    scope.record_transfer("temporary-address")
    scope.record_kernel_frame(2)
    assert scope.record_main_command(command_id)
    scope.record_context_begun()
    scope.mark_ready()
    return scope


def test_worker_mutation_on_main_route_uses_inline_system_command_and_reserved_id() -> None:
    from onec_runtime.execution.worker_mutation import MainPausedWorkerRoute

    class MainHelperSession(CompleteSession):
        helper_command_id: int | None = None

        def modify(self, variable, value_expression, *, on_transport_dispatch):
            if variable == "ИдентификаторКоманды":
                self.helper_command_id = int(value_expression)
            return super().modify(
                variable, value_expression,
                on_transport_dispatch=on_transport_dispatch,
            )

        def wait_evaluation_event(self, pending, *, timeout_s, on_transport_dispatch):
            if self.expression == "ЗавершеннаяКоманда":
                on_transport_dispatch()
                self._record("wait_eval")
                self.pending = None
                return EvaluationResult(
                    pending.result_id, "Число", str(self.helper_command_id), False,
                )
            return super().wait_evaluation_event(
                pending, timeout_s=timeout_s,
                on_transport_dispatch=on_transport_dispatch,
            )

    session = MainHelperSession(capture_count=0)
    arbiter = RdbgArbiter(session, RouteToken("runtime", 1, 0, "main"))
    previous_main = MainOperation(17, TARGET)
    previous_main.remote_completed()
    runner = _runner(lambda: MainPausedWorkerRoute(TARGET, previous_main), session)
    try:
        ticket = arbiter.submit(
            arbiter.current_route,
            lambda port: Settlement(runner(port, "Результат = 3;")),
        )
        arbiter.dispatch(ticket)
        assert ticket.wait_settled(3) == 3
        assert session.helper_command_id == -1
        assert previous_main.command_id == 17
        assert previous_main.phase is MainPhase.COMPLETED
        assert [name for name, _thread in session.calls].count("continue") == 1
        assert len({thread for _name, thread in session.calls}) == 1
        assert session.calls[0][1] is not current_thread()
    finally:
        arbiter.close(timeout=3)


def test_worker_mutation_on_capture_route_preserves_user_main_and_scope() -> None:
    from onec_runtime.execution.worker_mutation import CapturePausedWorkerRoute

    session = CompleteSession()
    arbiter = RdbgArbiter(session, RouteToken("runtime", 2, 0, "capture-1"))
    main = MainOperation(17, TARGET)
    main.stopped(CAPTURE_STOP, MainPhase.SUSPENDED_CAPTURE)
    scope = _capture_scope(17)
    runner = _runner(lambda: CapturePausedWorkerRoute(main, scope), session)
    try:
        ticket = arbiter.submit(
            arbiter.current_route,
            lambda port: Settlement(runner(port, "Результат = 3;")),
        )
        arbiter.dispatch(ticket)
        assert ticket.wait_settled(3) is True
        assert main.command_id == 17
        assert main.phase is MainPhase.SUSPENDED_CAPTURE
        assert scope.context_state is CaptureContextState.READY
        assert "РезультатИнструкции = Результат;" in session.expression
        assert session.expression.startswith(
            "RuntimeKernelServer.ВыполнитьКодВКонтекстеОтладки("
        )
        assert "modify" not in [name for name, _thread in session.calls]
        assert "continue" not in [name for name, _thread in session.calls]
        assert len({thread for _name, thread in session.calls}) == 1
    finally:
        arbiter.close(timeout=3)


def test_worker_mutation_unknown_capture_eval_stays_with_same_arbiter_ticket() -> None:
    from onec_runtime.execution.termination import FileTerminationConfirmed
    from onec_runtime.execution.worker_mutation import CapturePausedWorkerRoute

    class AmbiguousSession(CompleteSession):
        def start_evaluation(self, expression, **options):
            pending = super().start_evaluation(expression, **options)
            if expression.startswith(
                "RuntimeKernelServer.ВыполнитьКодВКонтекстеОтладки("
            ):
                raise EvaluationDispatchUnknown(pending)
            return pending

    session = AmbiguousSession()
    arbiter = RdbgArbiter(session, RouteToken("runtime", 2, 0, "capture-1"))
    main = MainOperation(17, TARGET)
    main.stopped(CAPTURE_STOP, MainPhase.SUSPENDED_CAPTURE)
    scope = _capture_scope(17)
    runner = _runner(lambda: CapturePausedWorkerRoute(main, scope), session)
    try:
        ticket = arbiter.submit(
            arbiter.current_route,
            lambda port: Settlement(runner(port, "Результат = 3;")),
        )
        arbiter.dispatch(ticket)
        assert ticket.wait_unknown(3)
        assert ticket.status().pending_capability is session.pending
        assert arbiter.active_ticket is ticket
        assert main.phase is MainPhase.SUSPENDED_CAPTURE
        assert [name for name, _thread in session.calls].count("set_breakpoints") == 1
        arbiter.retire_terminated_target(
            ticket, arbiter.current_route,
            FileTerminationConfirmed(TARGET, 1234, -15),
        )
    finally:
        if arbiter.active_ticket is None:
            arbiter.close(timeout=3)


def test_worker_mutation_confirmed_capture_bsl_error_keeps_scope_ready() -> None:
    from onec_runtime.execution.worker_mutation import CapturePausedWorkerRoute

    class BslErrorSession(CompleteSession):
        def wait_evaluation_event(self, pending, *, timeout_s, on_transport_dispatch):
            if self.expression.startswith(
                "RuntimeKernelServer.ВыполнитьКодВКонтекстеОтладки("
            ):
                on_transport_dispatch()
                self._record("wait_eval")
                self.pending = None
                return EvaluationResult(
                    pending.result_id, "Ошибка", "", True, "planned helper error",
                )
            return super().wait_evaluation_event(
                pending, timeout_s=timeout_s,
                on_transport_dispatch=on_transport_dispatch,
            )

    session = BslErrorSession()
    arbiter = RdbgArbiter(session, RouteToken("runtime", 2, 0, "capture-1"))
    main = MainOperation(17, TARGET)
    main.stopped(CAPTURE_STOP, MainPhase.SUSPENDED_CAPTURE)
    scope = _capture_scope(17)
    runner = _runner(lambda: CapturePausedWorkerRoute(main, scope), session)
    try:
        ticket = arbiter.submit(
            arbiter.current_route,
            lambda port: Settlement(runner(port, "ВызватьНеисправныйМетод();")),
        )
        arbiter.dispatch(ticket)
        try:
            ticket.wait_settled(3)
        except BslExecutionError as error:
            assert "planned helper error" in str(error)
        else:
            raise AssertionError("Expected a confirmed BSL helper error")
        assert scope.context_state is CaptureContextState.READY
        assert main.command_id == 17
        assert [name for name, _thread in session.calls].count("set_breakpoints") == 2
    finally:
        arbiter.close(timeout=3)


def test_worker_mutation_unknown_main_continue_retains_target_owner() -> None:
    from onec_runtime.execution.termination import FileTerminationConfirmed
    from onec_runtime.execution.worker_mutation import MainPausedWorkerRoute

    class AmbiguousContinueSession(CompleteSession):
        def __init__(self) -> None:
            super().__init__(capture_count=0)

        def continue_(self, *, on_transport_dispatch):
            on_transport_dispatch()
            self._record("continue")
            raise OutcomeUnknown("Continue acknowledgement was lost")

    session = AmbiguousContinueSession()
    arbiter = RdbgArbiter(session, RouteToken("runtime", 1, 0, "main"))
    runner = _runner(lambda: MainPausedWorkerRoute(TARGET), session)
    try:
        ticket = arbiter.submit(
            arbiter.current_route,
            lambda port: Settlement(runner(port, "Результат = 3;")),
        )
        arbiter.dispatch(ticket)
        assert ticket.wait_unknown(3)
        assert arbiter.active_ticket is ticket
        assert "wait_stop" not in [name for name, _thread in session.calls]
        arbiter.retire_terminated_target(
            ticket, arbiter.current_route,
            FileTerminationConfirmed(TARGET, 1234, -15),
        )
    finally:
        if arbiter.active_ticket is None:
            arbiter.close(timeout=3)


def test_main_helper_reads_latest_capture_registry_and_keeps_worker_slots() -> None:
    from onec_runtime.execution.worker_mutation import MainPausedWorkerRoute

    new_capture = ModuleLocation(
        "ExtensionModule", "", UUID(int=51), UUID(int=52), 120, "NewCapture",
    )

    class RecordingSession(CompleteSession):
        helper_id: int | None = None

        def __init__(self) -> None:
            super().__init__(capture_count=0)
            self.workspaces: list[tuple[ModuleLocation, ...]] = []

        def modify(self, variable, value_expression, *, on_transport_dispatch):
            if variable == "ИдентификаторКоманды":
                self.helper_id = int(value_expression)
            return super().modify(
                variable, value_expression,
                on_transport_dispatch=on_transport_dispatch,
            )

        def set_breakpoints(self, locations, *, on_transport_dispatch):
            self.workspaces.append(tuple(locations))
            return super().set_breakpoints(
                locations, on_transport_dispatch=on_transport_dispatch,
            )

        def wait_evaluation_event(self, pending, *, timeout_s, on_transport_dispatch):
            if self.expression == "ЗавершеннаяКоманда":
                on_transport_dispatch()
                self._record("wait_eval")
                self.pending = None
                return EvaluationResult(
                    pending.result_id, "Число", str(self.helper_id), False,
                )
            return super().wait_evaluation_event(
                pending, timeout_s=timeout_s,
                on_transport_dispatch=on_transport_dispatch,
            )

    session = RecordingSession()
    registry = [BreakpointRegistry(KERNEL, (BUSINESS,))]
    arbiter = RdbgArbiter(session, RouteToken("runtime", 1, 0, "main"))
    runner = _runner(
        lambda: MainPausedWorkerRoute(TARGET), session,
        registry_provider=lambda: registry[0],
    )
    registry[0] = BreakpointRegistry(KERNEL, (new_capture,))
    try:
        ticket = arbiter.submit(
            arbiter.current_route,
            lambda port: Settlement(runner(port, "Результат = 3;")),
        )
        arbiter.dispatch(ticket)
        assert ticket.wait_settled(3) == 3
        assert session.workspaces
        assert session.workspaces[-1] == (KERNEL, new_capture, WORKER_POINT)
        assert any(
            new_capture not in locations and WORKER_POINT in locations
            for locations in session.workspaces
        )
    finally:
        arbiter.close(timeout=3)
