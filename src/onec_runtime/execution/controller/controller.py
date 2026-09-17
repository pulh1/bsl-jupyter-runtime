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

from onec_runtime.bsl.parser_target import PythonParserTarget
from onec_runtime.errors import BslExecutionError, ProtocolError
from onec_runtime.execution.arbiter import (
    CancelledBeforeEffect,
    ExecutionTicket,
    RdbgArbiter,
    RouteToken,
    SessionPort,
    Settlement,
    StaleRoute,
)
from onec_runtime.execution.capture.adapter import CaptureSetupAdapter
from onec_runtime.execution.capture.cell_evaluator import CaptureCellEvaluator
from onec_runtime.execution.capture.executor import CaptureExecutor
from onec_runtime.execution.capture.inspection import CaptureInspectionExecutor
from onec_runtime.execution.capture.materialization import (
    CaptureMaterializationExecutor,
    CaptureMaterializationPlan,
)
from onec_runtime.execution.capture.operation_executor import CaptureCellOperationExecutor
from onec_runtime.execution.capture.policy import CaptureCellPolicy, CapturePreparedPayload
from onec_runtime.execution.capture.scope import (
    CaptureContextState, CaptureFrameIdentity, CaptureScope,
)
from onec_runtime.execution.capture.writeback import CaptureWritebackExecutor
from onec_runtime.execution.contracts import (
    Accepted, Current, PreparationContext, PreparedCell, Rejected,
    StalePreparation, StalePreparedDispatch, SubmissionReceipt, Unavailable,
)
from onec_runtime.execution.main import MainExecutor, MainOperation, MainPhase
from onec_runtime.execution.main.completion import MainRemoteCompletion, read_main_completion
from onec_runtime.execution.main.policy import MainCellPolicy, MainPreparedPayload
from onec_runtime.execution.snapshot_binding import (
    RoutePreparationSnapshot, RouteSnapshotGuard, SnapshotRouteBinding,
)
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


@dataclass(frozen=True, slots=True)
class _PreparationRecord:
    context: PreparationContext
    route: RouteToken
    revision: int
    snapshot: RoutePreparationSnapshot
    owner: MainOperation | CaptureScope | None


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
        parser_target: PythonParserTarget | None = None,
        snapshot_provider: Callable[[], RoutePreparationSnapshot] | None = None,
    ) -> None:
        if type(runtime_generation) is not int or runtime_generation <= 0:
            raise ValueError("runtime_generation must be positive")
        self._arbiter = arbiter
        self._main_executor = main_executor
        self._capture_executor = capture_executor
        self._capture_cell_executor = CaptureCellOperationExecutor(capture_cell_evaluator)
        self._capture_inspection_executor = CaptureInspectionExecutor()
        self._capture_materialization_executor = CaptureMaterializationExecutor()
        self._capture_writeback_executor = CaptureWritebackExecutor()
        self._registry = registry
        self._generation = runtime_generation
        self._parser_target = parser_target
        self._snapshot_provider = snapshot_provider
        self._lock = RLock()
        self._command_sequence = 0
        self._stop_sequence = 0
        self.main_operation: MainOperation | None = None
        self.capture_scope: CaptureScope | None = None
        self._capture_route: RouteToken | None = None
        self._resume_ticket: ExecutionTicket | None = None
        self._preparation_revision = 0
        self._preparations: dict[object, _PreparationRecord] = {}

    def await_preparation_context(self) -> PreparationContext | Unavailable:
        """Select one stable statement route without reserving RDBG for lowering.

        This component boundary reports an active remote operation as typed
        unavailable; waiting across it belongs to the future composition root.
        """

        parser_target = self._parser_target
        provider = self._snapshot_provider
        if parser_target is None or provider is None:
            return Unavailable("Statement preparation is not configured")
        snapshot = provider()
        if not isinstance(snapshot, RoutePreparationSnapshot):
            raise TypeError("snapshot provider must return RoutePreparationSnapshot")
        with self._lock:
            operation = self.main_operation
            scope = self.capture_scope
            if self._resume_in_flight() or self._arbiter.has_pending_operations:
                return Unavailable("RDBG operation is still active")
            if (
                scope is not None
                and scope.context_state is CaptureContextState.READY
                and scope.frame_identity is CaptureFrameIdentity.CONFIRMED
                and operation is not None
                and operation.phase is MainPhase.SUSPENDED_CAPTURE
            ):
                owner: MainOperation | CaptureScope | None = scope
                policy = CaptureCellPolicy(SnapshotRouteBinding(
                    parser_target, owner=snapshot.owner, version=snapshot.version
                ))
            elif scope is None and (operation is None or operation.terminal):
                owner = operation
                policy = MainCellPolicy(SnapshotRouteBinding(
                    parser_target, owner=snapshot.owner, version=snapshot.version
                ))
            else:
                return Unavailable("No stable MAIN or CAPTURE route is available")
            route = self._arbiter.current_route
            token = object()
            nonce = object()
            context = PreparationContext(token, nonce, policy, snapshot)
            self._preparations[nonce] = _PreparationRecord(
                context, route, self._preparation_revision, snapshot, owner
            )
            # Abandoned local preparations cannot retain an unbounded history.
            if len(self._preparations) > 1024:
                self._preparations.pop(next(iter(self._preparations)))
            return context

    def validate_preparation(
        self, context: PreparationContext, guards: object
    ) -> Current | StalePreparation | Unavailable:
        provider = self._snapshot_provider
        if provider is None:
            return Unavailable("Statement preparation is not configured")
        live = provider()
        with self._lock:
            return self._validate_preparation_locked(context, guards, live)

    def _validate_preparation_locked(
        self, context: PreparationContext, guards: object,
        live: RoutePreparationSnapshot,
    ) -> Current | StalePreparation:
        record = self._preparations.get(context.preparation_nonce)
        if record is None or record.context is not context:
            return StalePreparation("preparation nonce is stale")
        if (
            context.route_token is not record.context.route_token
            or context.policy is not record.context.policy
            or context.capabilities is not record.snapshot
            or record.revision != self._preparation_revision
            or record.route != self._arbiter.current_route
        ):
            return StalePreparation("execution route changed")
        operation = self.main_operation
        scope = self.capture_scope
        if isinstance(record.context.policy, MainCellPolicy):
            ready = scope is None and operation is record.owner and (
                operation is None or operation.terminal
            )
        else:
            ready = (
                scope is record.owner
                and scope is not None
                and scope.context_state is CaptureContextState.READY
                and scope.frame_identity is CaptureFrameIdentity.CONFIRMED
                and operation is not None
                and operation.phase is MainPhase.SUSPENDED_CAPTURE
                and not self._resume_in_flight()
            )
        if not ready:
            return StalePreparation("execution context changed")
        if self._arbiter.has_pending_operations:
            return StalePreparation("RDBG operation was admitted during preparation")
        snapshot = record.snapshot
        if (
            not isinstance(guards, RouteSnapshotGuard)
            or guards.owner is not snapshot.owner
            or guards.version != snapshot.version
            or guards.namespace_names != snapshot.namespace_names
            or guards.worker_exports != snapshot.worker_exports
            or not isinstance(live, RoutePreparationSnapshot)
            or live.owner is not snapshot.owner
            or live.version != snapshot.version
            or live.namespace_names != snapshot.namespace_names
            or live.worker_exports != snapshot.worker_exports
        ):
            return StalePreparation("namespace or Worker snapshot changed")
        return Current()

    def submit_cell(
        self, context: PreparationContext, prepared: PreparedCell,
        guards: object, receipt: SubmissionReceipt,
    ) -> Accepted | Rejected:
        """Adopt a one-use statement ticket before worker or transport effects."""

        if not isinstance(receipt, SubmissionReceipt) or receipt.ticket is not None:
            raise TypeError("an empty submission receipt is required")
        provider = self._snapshot_provider
        if provider is None:
            return Rejected(Unavailable("Statement preparation is not configured"))
        live = provider()
        with self._lock:
            validity = self._validate_preparation_locked(context, guards, live)
            if isinstance(validity, StalePreparation):
                return Rejected(validity)
            record = self._preparations[context.preparation_nonce]
            if (
                not isinstance(prepared, PreparedCell)
                or prepared.route_token is not context.route_token
                or prepared.preparation_nonce is not context.preparation_nonce
            ):
                return Rejected(StalePreparation("prepared cell has another route or nonce"))
            payload = prepared.payload
            if isinstance(context.policy, MainCellPolicy):
                if not isinstance(payload, MainPreparedPayload):
                    return Rejected(StalePreparation("MAIN prepared payload is invalid"))
                statement = payload.statement
            else:
                if not isinstance(payload, CapturePreparedPayload):
                    return Rejected(StalePreparation("CAPTURE prepared payload is invalid"))
                statement = payload.statement
            if statement is None:
                return Rejected(Unavailable("Worker artifacts or empty statements are unsupported"))
            if (
                isinstance(payload, CapturePreparedPayload)
                and payload.dirty_roots != statement.lowering.dirty_roots
            ):
                return Rejected(StalePreparation("CAPTURE dirty roots do not match lowering"))
            source = statement.lowering.source

            def preflight() -> None:
                fresh = provider()
                with self._lock:
                    if (
                        self._arbiter.current_route != record.route
                        or not isinstance(fresh, RoutePreparationSnapshot)
                        or fresh.owner is not record.snapshot.owner
                        or fresh.version != record.snapshot.version
                        or fresh.namespace_names != record.snapshot.namespace_names
                        or fresh.worker_exports != record.snapshot.worker_exports
                    ):
                        raise StalePreparedDispatch(
                            "statement snapshot changed before dispatch"
                        )

            try:
                if isinstance(context.policy, MainCellPolicy):
                    ticket = self.submit_main(
                        source, _receipt=receipt, _before_first_effect=preflight
                    )
                else:
                    ticket = self.submit_capture_cell(
                        source, dirty_roots=payload.dirty_roots,
                        _receipt=receipt, _before_first_effect=preflight,
                    )
            except StaleRoute:
                return Rejected(StalePreparation("arbiter route changed"))
            self._preparations.pop(context.preparation_nonce, None)
            return Accepted(ticket)

    def request_stop(self, ticket: ExecutionTicket) -> object:
        return self._arbiter.request_stop(ticket)

    def submit_main(
        self, instruction: str, *,
        _receipt: SubmissionReceipt | None = None,
        _before_first_effect: Callable[[], None] | None = None,
    ) -> ExecutionTicket:
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
                if _before_first_effect is not None:
                    try:
                        _before_first_effect()
                    except BaseException:
                        operation.fail_before_dispatch()
                        raise
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

            ticket: ExecutionTicket | None = None
            try:
                ticket = self._arbiter.submit(route, plan)
                if _receipt is not None:
                    _receipt.adopt(ticket)
                self._preparation_revision += 1
                self._arbiter.dispatch(ticket)
            except BaseException:
                if _receipt is None or _receipt.ticket is None:
                    self.main_operation = None
                    self._command_sequence -= 1
                elif ticket is not None:
                    # A published receipt survives a queue cancellation.  Only
                    # confirmed pre-effect cancellation can terminate MAIN here.
                    if ticket.cancel_queued():
                        operation.fail_before_dispatch()
                    elif ticket.status().settled:
                        try:
                            ticket.wait_settled(timeout=0)
                        except CancelledBeforeEffect:
                            operation.fail_before_dispatch()
                        except Exception:
                            pass
                raise
            return ticket

    def submit_capture_cell(
        self,
        lowered_source: str,
        *,
        dirty_roots: tuple[str, ...] = (),
        result_policy: Callable[[EvaluationResult], object] | None = None,
        _receipt: SubmissionReceipt | None = None,
        _before_first_effect: Callable[[], None] | None = None,
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

            def plan(port: SessionPort) -> Settlement:
                if _before_first_effect is not None:
                    _before_first_effect()
                scope.admit_cell_dirty_roots(dirty_roots)
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
            if _receipt is not None:
                _receipt.adopt(ticket)
            self._preparation_revision += 1
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
            self._preparation_revision += 1
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
            self._preparation_revision += 1
            self._arbiter.dispatch(ticket)
            return ticket

    def submit_capture_materialization(
        self, transfer_plan: CaptureMaterializationPlan
    ) -> ExecutionTicket:
        """Execute one private value transfer inside the current stop."""

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
                return self._capture_materialization_executor.execute(
                    scope,
                    transfer_plan,
                    port=port,
                    shield_workspace=lambda worker: worker.set_breakpoints(
                        self._registry.evaluation_locations
                    ),
                    restore_workspace=lambda worker: worker.set_breakpoints(
                        self._registry.full_locations
                    ),
                )

            ticket = self._arbiter.submit(route, plan)
            self._preparation_revision += 1
            self._arbiter.dispatch(ticket)
            return ticket

    def submit_capture_cleanup_retry(self, key: str) -> ExecutionTicket:
        """Retry a confirmed private-key deletion in the same CAPTURE stop."""

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
            if not any(
                debt.key == key and debt.can_retry_delete
                for debt in scope.temporary_cleanup_debts
            ):
                raise ProtocolError("No confirmed CAPTURE cleanup failure can be retried")

            def plan(port: SessionPort) -> Settlement:
                return self._capture_materialization_executor.retry_confirmed_cleanup(
                    scope,
                    key,
                    port=port,
                    shield_workspace=lambda worker: worker.set_breakpoints(
                        self._registry.evaluation_locations
                    ),
                    restore_workspace=lambda worker: worker.set_breakpoints(
                        self._registry.full_locations
                    ),
                )

            ticket = self._arbiter.submit(route, plan)
            self._preparation_revision += 1
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
            self._preparation_revision += 1
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
