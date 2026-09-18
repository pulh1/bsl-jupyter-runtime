"""Worker mutation helpers use the admitted arbiter port on each route."""

from __future__ import annotations

from collections import deque
from threading import current_thread
from uuid import UUID

import pytest

from onec_runtime.breakpoint_workspace import (
    BreakpointWorkspaceController, WorkspaceSnapshot,
)
from onec_runtime.errors import BslExecutionError, EvaluationDispatchUnknown, ProtocolError
from onec_runtime.execution.arbiter import RdbgArbiter, RouteToken, Settlement
from onec_runtime.execution.breakpoint_routes import RouteBreakpointWorkspace
from onec_runtime.execution.capture.cell_evaluator import CaptureCellEvaluator
from onec_runtime.execution.capture.executor import CaptureExecutor
from onec_runtime.execution.capture.operation_executor import CaptureCellOperationExecutor
from onec_runtime.execution.controller.controller import ExecutionController
from onec_runtime.execution.capture.scope import CaptureContextState, CaptureScope
from onec_runtime.execution.main import MainExecutor, MainOperation, MainPhase
from onec_runtime.rdbg.models import EvaluationResult, FrameVariable, ModuleLocation, StopEvent
from onec_runtime.stop_routing import BreakpointRegistry
from onec_runtime.table_value import evaluation_to_python
from arbiter_test_cleanup import confirm_test_server_terminated

from test_execution_controller_routes import CompleteSession
from test_execution_route_sequence import BUSINESS, CAPTURE_STOP, KERNEL, MAIN_STOP, TARGET


WORKER_POINT = ModuleLocation(
    "ExtensionModule", "", UUID(int=41), UUID(int=42), 110, "Worker",
)


def _runner(route_provider, session, *, registry_provider=None,
            collect_messages=False):
    from onec_runtime.execution.worker_mutation import WorkerMutationInstructionRunner
    from onec_runtime.execution.capture.messages import CaptureMessageCollector

    registry = BreakpointRegistry(KERNEL, (BUSINESS,))
    workspace = RouteBreakpointWorkspace(BreakpointWorkspaceController(
        session,
        WorkspaceSnapshot(0, KERNEL, registry.captures, (), (WORKER_POINT,), False),
    ))
    return WorkerMutationInstructionRunner(
        CaptureCellOperationExecutor(
            CaptureCellEvaluator(),
            message_collector=CaptureMessageCollector() if collect_messages else None,
        ),
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


def test_worker_mutation_on_main_route_uses_one_eval_without_command_loop() -> None:
    from onec_runtime.execution.worker_mutation import MainPausedWorkerRoute

    class MainHelperSession(CompleteSession):
        def wait_evaluation_event(self, pending, *, timeout_s, on_transport_dispatch):
            if self.expression.startswith(
                "RuntimeKernelServer.ВыполнитьКодВКонтекстеMain("
            ):
                on_transport_dispatch()
                self._record("wait_eval")
                self.pending = None
                return EvaluationResult(pending.result_id, "Число", "3", False)
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
        assert previous_main.command_id == 17
        assert previous_main.phase is MainPhase.COMPLETED
        assert session.expression.startswith(
            "RuntimeKernelServer.ВыполнитьКодВКонтекстеMain("
        )
        assert "РезультатИнструкции = Результат;" in session.expression
        assert "modify" not in [name for name, _thread in session.calls]
        assert "continue" not in [name for name, _thread in session.calls]
        assert [name for name, _thread in session.calls].count("start_eval") == 1
        assert len({thread for _name, thread in session.calls}) == 1
        assert session.calls[0][1] is not current_thread()
    finally:
        if arbiter.active_ticket is not None:
            confirm_test_server_terminated(
                arbiter, ticket, arbiter.current_route, session, TARGET,
            )
        else:
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


def test_worker_capture_helper_settles_with_shared_message_collector() -> None:
    from onec_runtime.execution.worker_mutation import CapturePausedWorkerRoute

    session = CompleteSession()
    arbiter = RdbgArbiter(session, RouteToken("runtime", 2, 0, "capture-1"))
    main = MainOperation(17, TARGET)
    main.stopped(CAPTURE_STOP, MainPhase.SUSPENDED_CAPTURE)
    scope = _capture_scope(17)
    runner = _runner(
        lambda: CapturePausedWorkerRoute(main, scope), session,
        collect_messages=True,
    )
    ticket = arbiter.submit(
        arbiter.current_route,
        lambda port: Settlement(runner(port, "Результат = 3;")),
    )
    arbiter.dispatch(ticket)
    try:
        assert not ticket.wait_unknown(3)
        assert ticket.wait_settled(3) is True
        assert main.phase is MainPhase.SUSPENDED_CAPTURE
        assert scope.context_state is CaptureContextState.READY
        assert arbiter.active_ticket is None
    finally:
        if arbiter.active_ticket is not None:
            confirm_test_server_terminated(
                arbiter, ticket, arbiter.current_route, session, TARGET,
            )
        else:
            arbiter.close(timeout=3)


def test_worker_helper_leaves_controller_sequence_for_next_user_main() -> None:
    class CommandLoopSession(CompleteSession):
        command_id: int | None = None
        completed_command_id = 0

        def __init__(self):
            super().__init__(capture_count=0)
            service = StopEvent(
                TARGET, KERNEL, MAIN_STOP.reason, stop_by_breakpoint=True,
            )
            self.stops = deque((service, service, service))

        def modify(self, variable, value_expression, *, on_transport_dispatch):
            if variable == "ИдентификаторКоманды":
                self.command_id = int(value_expression)
            return super().modify(
                variable, value_expression,
                on_transport_dispatch=on_transport_dispatch,
            )

        def continue_(self, *, on_transport_dispatch):
            if self.command_id is not None and self.command_id > self.completed_command_id:
                self.completed_command_id = self.command_id
            return super().continue_(on_transport_dispatch=on_transport_dispatch)

        def wait_evaluation_event(self, pending, *, timeout_s, on_transport_dispatch):
            if self.expression.startswith(
                "RuntimeKernelServer.ВыполнитьКодВКонтекстеMain("
            ):
                on_transport_dispatch()
                self._record("wait_eval")
                self.pending = None
                return EvaluationResult(pending.result_id, "Число", "3", False)
            if self.expression == "ЗавершеннаяКоманда":
                on_transport_dispatch()
                self._record("wait_eval")
                self.pending = None
                return EvaluationResult(
                    pending.result_id, "Число", str(self.completed_command_id), False,
                )
            return super().wait_evaluation_event(
                pending, timeout_s=timeout_s,
                on_transport_dispatch=on_transport_dispatch,
            )

    session = CommandLoopSession()
    arbiter = RdbgArbiter(session, RouteToken("runtime", 1, 0, "main"))
    controller = ExecutionController(
        arbiter,
        MainExecutor(poll_interval_s=0.1),
        CaptureExecutor(None, KERNEL, decode_command_id=evaluation_to_python),
        CaptureCellEvaluator(),
        BreakpointRegistry(KERNEL, (BUSINESS,)),
        runtime_generation=1,
        initial_target_id=TARGET,
    )
    runner = _runner(controller.worker_mutation_route, session)
    try:
        first = controller.submit_main("Результат = 1;")
        first.wait_settled(3)
        previous = controller.main_operation
        assert previous is not None and previous.command_id == 1
        assert session.completed_command_id == 1

        worker = arbiter.submit(
            arbiter.current_route,
            lambda port: Settlement(runner(port, "Результат = 3;")),
        )
        arbiter.dispatch(worker)
        assert worker.wait_settled(3) == 3
        assert session.completed_command_id == 1
        assert controller._command_sequence == 1
        assert controller.main_operation is previous

        user = controller.submit_main("Результат = 4;")
        user.wait_settled(3)
        assert controller.main_operation is not None
        assert controller.main_operation.command_id == 2
        assert session.completed_command_id == 2
        assert controller._command_sequence == 2
    finally:
        arbiter.close(timeout=3)


def test_worker_main_helper_cannot_overtake_armed_capture_ticket() -> None:
    session = CompleteSession(capture_count=0)
    arbiter = RdbgArbiter(session, RouteToken("runtime", 1, 0, "main"))
    controller = ExecutionController(
        arbiter,
        MainExecutor(poll_interval_s=0.1),
        CaptureExecutor(None, KERNEL, decode_command_id=evaluation_to_python),
        CaptureCellEvaluator(),
        BreakpointRegistry(KERNEL, (BUSINESS,)),
        runtime_generation=1,
        initial_target_id=TARGET,
    )
    try:
        planned = controller.prepare_capture_ticket()
        assert planned.expected_operation_id == 1
        with pytest.raises(ProtocolError, match="armed CAPTURE ticket"):
            controller.worker_mutation_route()
        assert controller._command_sequence == 0
        assert not arbiter.has_pending_operations
    finally:
        arbiter.close(timeout=3)


def test_worker_mutation_unknown_capture_eval_stays_with_same_arbiter_ticket() -> None:
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
        confirm_test_server_terminated(
            arbiter, ticket, arbiter.current_route, session, TARGET,
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


def test_worker_mutation_unknown_main_eval_retains_exact_pending_capability() -> None:
    from onec_runtime.execution.worker_mutation import MainPausedWorkerRoute

    class AmbiguousEvaluationSession(CompleteSession):
        def __init__(self) -> None:
            super().__init__(capture_count=0)

        def start_evaluation(self, expression, **options):
            pending = super().start_evaluation(expression, **options)
            if expression.startswith(
                "RuntimeKernelServer.ВыполнитьКодВКонтекстеMain("
            ):
                raise EvaluationDispatchUnknown(pending)
            return pending

    session = AmbiguousEvaluationSession()
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
        assert ticket.status().pending_capability is session.pending
        assert "continue" not in [name for name, _thread in session.calls]
        assert "wait_stop" not in [name for name, _thread in session.calls]
        confirm_test_server_terminated(
            arbiter, ticket, arbiter.current_route, session, TARGET,
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
        def __init__(self) -> None:
            super().__init__(capture_count=0)
            self.workspaces: list[tuple[ModuleLocation, ...]] = []

        def set_breakpoints(self, locations, *, on_transport_dispatch):
            self.workspaces.append(tuple(locations))
            return super().set_breakpoints(
                locations, on_transport_dispatch=on_transport_dispatch,
            )

        def wait_evaluation_event(self, pending, *, timeout_s, on_transport_dispatch):
            if self.expression.startswith(
                "RuntimeKernelServer.ВыполнитьКодВКонтекстеMain("
            ):
                on_transport_dispatch()
                self._record("wait_eval")
                self.pending = None
                return EvaluationResult(
                    pending.result_id, "Число", "3", False,
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
