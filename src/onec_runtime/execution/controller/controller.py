"""Route MAIN and CAPTURE operations while one arbiter owns RDBG.

This controller is a narrow execution path used by the new component
contract. The public RuntimeApi still uses its transitional controller until
Worker binding, inspection, materialization and writeback are migrated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from threading import RLock
from typing import Callable

from onec_runtime.errors import BslExecutionError, ProtocolError
from onec_runtime.execution.arbiter import (
    ExecutionTicket,
    RdbgArbiter,
    RouteToken,
    SessionPort,
    Settlement,
)
from onec_runtime.execution.capture.adapter import CaptureSetupAdapter
from onec_runtime.execution.capture.cell_evaluator import CaptureCellEvaluator
from onec_runtime.execution.capture.executor import CaptureExecutor
from onec_runtime.execution.capture.inspection import CaptureInspectionExecutor
from onec_runtime.execution.capture.operation_executor import CaptureCellOperationExecutor
from onec_runtime.execution.capture.scope import CaptureContextState, CaptureScope
from onec_runtime.execution.capture.writeback import CaptureWritebackExecutor
from onec_runtime.execution.main import MainExecutor, MainOperation, MainPhase
from onec_runtime.execution.main.completion import MainRemoteCompletion, read_main_completion
from onec_runtime.rdbg.models import EvaluationResult, StopEvent
from onec_runtime.stop_routing import BreakpointRegistry, StopReason, classify_stop


class MainYieldKind(str, Enum):
    CAPTURE = "capture"
    COMPLETED = "completed"
    DEBUG_STOP = "debug_stop"


@dataclass(frozen=True, slots=True)
class MainYield:
    kind: MainYieldKind
    operation: MainOperation = field(repr=False)
    scope: CaptureScope | None = field(default=None, repr=False)
    completion: MainRemoteCompletion | None = field(default=None, repr=False)
    stop: StopEvent | None = field(default=None, repr=False)


class ExecutionController:
    """Choose the current route; executors own protocol command sequences."""

    def __init__(
        self,
        arbiter: RdbgArbiter,
        main_executor: MainExecutor,
        capture_executor: CaptureExecutor,
        capture_cell_evaluator: CaptureCellEvaluator,
        registry: BreakpointRegistry,
        *,
        runtime_generation: int,
    ) -> None:
        if type(runtime_generation) is not int or runtime_generation <= 0:
            raise ValueError("runtime_generation must be positive")
        self._arbiter = arbiter
        self._main_executor = main_executor
        self._capture_executor = capture_executor
        self._capture_cell_executor = CaptureCellOperationExecutor(capture_cell_evaluator)
        self._capture_inspection_executor = CaptureInspectionExecutor()
        self._capture_writeback_executor = CaptureWritebackExecutor()
        self._registry = registry
        self._generation = runtime_generation
        self._lock = RLock()
        self._command_sequence = 0
        self._stop_sequence = 0
        self.main_operation: MainOperation | None = None
        self.capture_scope: CaptureScope | None = None
        self._capture_route: RouteToken | None = None
        self._resume_ticket: ExecutionTicket | None = None

    def submit_main(self, instruction: str) -> ExecutionTicket:
        """Admit one MAIN command and return its first-stop ticket."""

        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError("MAIN instruction must be non-empty text")
        with self._lock:
            if self.main_operation is not None and not self.main_operation.terminal:
                raise ProtocolError("A MAIN command is still active")
            route = self._arbiter.current_route
            self._command_sequence += 1
            operation = MainOperation(self._command_sequence, None)
            self.main_operation = operation
            self.capture_scope = None
            self._capture_route = None
            self._resume_ticket = None

            def plan(port: SessionPort) -> Settlement:
                stop = self._main_executor.dispatch(
                    operation,
                    instruction,
                    install_workspace=lambda: port.set_breakpoints(
                        self._registry.full_locations
                    ),
                    before_command_write=lambda: None,
                    before_continue=lambda: None,
                    port=port,
                )
                return self._route_stop(port, operation, stop)

            try:
                ticket = self._arbiter.submit(route, plan)
                self._arbiter.dispatch(ticket)
            except BaseException:
                self.main_operation = None
                self._command_sequence -= 1
                raise
            return ticket

    def submit_capture_cell(
        self,
        lowered_source: str,
        *,
        dirty_roots: tuple[str, ...] = (),
        result_policy: Callable[[EvaluationResult], object] | None = None,
    ) -> ExecutionTicket:
        """Run one cell inside the current stop; policy interprets its result."""

        with self._lock:
            if self._resume_in_flight():
                raise ProtocolError("CAPTURE resume has already been admitted")
            scope = self.capture_scope
            operation = self.main_operation
            route = self._capture_route
            if (
                scope is None
                or scope.context_state is not CaptureContextState.READY
                or operation is None
                or operation.phase is not MainPhase.SUSPENDED_CAPTURE
                or route is None
            ):
                raise ProtocolError("No ready CAPTURE stop is available")
            selected_policy = result_policy or (lambda result: result)
            scope.admit_cell_dirty_roots(dirty_roots)

            def plan(port: SessionPort) -> Settlement:
                return self._capture_cell_executor.execute(
                    scope,
                    lowered_source,
                    port=port,
                    shield_workspace=lambda worker: worker.set_breakpoints(
                        self._registry.evaluation_locations
                    ),
                    restore_workspace=lambda worker: worker.set_breakpoints(
                        self._registry.full_locations
                    ),
                    cleanup=lambda worker: None,
                    result_policy=selected_policy,
                )

            ticket = self._arbiter.submit(route, plan)
            self._arbiter.dispatch(ticket)
            return ticket

    def submit_resume(self) -> ExecutionTicket:
        """Write dirty roots, close CAPTURE, and resume the same MAIN command."""

        with self._lock:
            if self._resume_in_flight():
                raise ProtocolError("CAPTURE resume has already been admitted")
            scope = self.capture_scope
            operation = self.main_operation
            route = self._capture_route
            if (
                scope is None
                or scope.context_state is not CaptureContextState.READY
                or operation is None
                or operation.phase is not MainPhase.SUSPENDED_CAPTURE
                or route is None
            ):
                raise ProtocolError("No ready CAPTURE stop can be resumed")
            if scope.temporary_cleanup_debts:
                raise ProtocolError("CAPTURE temporary cleanup debt requires repair")

            def plan(port: SessionPort) -> Settlement:
                ledger = scope.begin_writeback()
                assert scope.kernel_stack_level is not None
                self._capture_writeback_executor.flush(
                    port, ledger, stack_level=scope.kernel_stack_level
                )
                self._capture_executor.end_scope(
                    scope, port=CaptureSetupAdapter(port)
                )
                try:
                    self._main_executor.continue_command(operation, port=port)
                except BaseException:
                    if operation.phase is MainPhase.UNKNOWN:
                        scope.mark_unverified()
                    raise
                scope.mark_closed()
                with self._lock:
                    self.capture_scope = None
                    self._capture_route = None
                    self._resume_ticket = None
                next_route = self._next_route("main")
                port.handoff_route(next_route)
                stop = self._main_executor.await_stop(port=port)
                return self._route_stop(port, operation, stop)

            ticket = self._arbiter.submit(route, plan)
            self._resume_ticket = ticket
            self._arbiter.dispatch(ticket)
            return ticket

    def submit_capture_variable(
        self, name: str, *, stack_level: int = 0
    ) -> ExecutionTicket:
        """Read one user-frame variable in the current CAPTURE stop."""

        with self._lock:
            if self._resume_in_flight():
                raise ProtocolError("CAPTURE resume has already been admitted")
            scope = self.capture_scope
            operation = self.main_operation
            route = self._capture_route
            if (
                scope is None
                or scope.context_state is not CaptureContextState.READY
                or operation is None
                or operation.phase is not MainPhase.SUSPENDED_CAPTURE
                or route is None
            ):
                raise ProtocolError("No ready CAPTURE stop is available")

            def plan(port: SessionPort) -> Settlement:
                variable = self._capture_inspection_executor.read_variable(
                    scope, name, stack_level=stack_level, port=port
                )
                return Settlement(variable)

            ticket = self._arbiter.submit(route, plan)
            self._arbiter.dispatch(ticket)
            return ticket

    def _resume_in_flight(self) -> bool:
        ticket = self._resume_ticket
        if ticket is None:
            return False
        if ticket.status().settled:
            self._resume_ticket = None
            return False
        return True

    def submit_resume_debug_stop(self) -> ExecutionTicket:
        """Continue the current user breakpoint in the same MAIN command."""

        with self._lock:
            operation = self.main_operation
            if (
                operation is None
                or operation.phase is not MainPhase.SUSPENDED_USER
                or operation.pending_stop is None
                or self.capture_scope is not None
            ):
                raise ProtocolError("No user breakpoint can be resumed")
            route = self._arbiter.current_route

            def plan(port: SessionPort) -> Settlement:
                stop = self._main_executor.resume(operation, port=port)
                return self._route_stop(port, operation, stop)

            ticket = self._arbiter.submit(route, plan)
            self._arbiter.dispatch(ticket)
            return ticket

    def _route_stop(
        self, port: SessionPort, operation: MainOperation, stop: StopEvent
    ) -> Settlement:
        reason = classify_stop(stop, self._registry).reason
        if reason is StopReason.MAIN_SERVICE:
            completion = read_main_completion(port, operation)
            operation.complete(completion)
            return Settlement(MainYield(MainYieldKind.COMPLETED, operation, completion=completion))
        if reason is StopReason.CAPTURE:
            operation.stopped(stop, MainPhase.SUSPENDED_CAPTURE)
            with self._lock:
                self._stop_sequence += 1
                scope = CaptureScope.from_stop(
                    self._generation,
                    operation.command_id,
                    stop,
                    self._stop_sequence,
                )
                self.capture_scope = scope
                capture_route = self._next_route(f"capture-{self._stop_sequence}")
                self._capture_route = capture_route
            port.handoff_route(capture_route)
            try:
                self._capture_executor.open_scope(
                    scope, port=CaptureSetupAdapter(port)
                )
            except (BslExecutionError, ProtocolError) as error:
                scope.fail_setup(error)
                raise
            except BaseException as error:
                scope.note_setup_uncertain(error)
                raise
            scope.mark_ready()
            return Settlement(MainYield(MainYieldKind.CAPTURE, operation, scope=scope))
        phase = MainPhase.SUSPENDED_USER if reason is StopReason.USER_BREAKPOINT else MainPhase.UNKNOWN
        operation.stopped(stop, phase)
        return Settlement(MainYield(MainYieldKind.DEBUG_STOP, operation, stop=stop))

    def _next_route(self, context_id: str) -> RouteToken:
        current = self._arbiter.current_route
        return RouteToken(current.incarnation, current.epoch + 1, 0, context_id)
