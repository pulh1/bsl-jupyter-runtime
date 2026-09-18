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
from weakref import WeakKeyDictionary

from onec_runtime.capture import build_live_capture_root_transfer_call
from onec_runtime.bsl.parser_target import PythonParserTarget
from onec_runtime.errors import BslExecutionError, ProtocolError
from onec_runtime.execution.arbiter import (
    CancelledBeforeEffect,
    ConfirmedFailure,
    ExecutionTicket,
    OutcomeUnknown,
    RdbgArbiter,
    ReadyForPolicy,
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
from onec_runtime.execution.capture.messages import CaptureMessageCollector
from onec_runtime.execution.capture.operation_executor import (
    CaptureCellOperation,
    CaptureCellOperationExecutor,
)
from onec_runtime.execution.capture.policy import CaptureCellPolicy, CapturePreparedPayload
from onec_runtime.execution.capture.scope import (
    CaptureContextState, CaptureFrameIdentity, CaptureScope,
)
from onec_runtime.execution.capture.writeback import CaptureWritebackExecutor
from onec_runtime.execution.breakpoint_routes import RouteBreakpointWorkspace
from onec_runtime.execution.contracts import (
    Accepted, Current, PreparationContext, PreparedCell, Rejected,
    StalePreparation, StalePreparedDispatch, SubmissionReceipt, Unavailable,
)
from onec_runtime.execution.main import MainExecutor, MainOperation, MainPhase
from onec_runtime.execution.main.completion import MainRemoteCompletion, read_main_completion
from onec_runtime.execution.main.idle_materialization import MainIdleTargetFence
from onec_runtime.execution.main.policy import MainCellPolicy, MainPreparedPayload
from onec_runtime.execution.preparation import WorkerCandidateIntent
from onec_runtime.execution.snapshot_binding import (
    RoutePreparationSnapshot, RouteSnapshotGuard, SnapshotRouteBinding,
)
from onec_runtime.execution.worker import (
    WorkerActivationLease, WorkerActivationPort, WorkerActivationUnknown,
)
from onec_runtime.rdbg.models import EvaluationResult, ModuleLocation, StopEvent, TargetId
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
    snapshot_reader: Callable[[], RoutePreparationSnapshot] = field(repr=False)


@dataclass(frozen=True, slots=True)
class _CaptureCellRepair:
    operation: CaptureCellOperation
    scope: CaptureScope
    result_policy: Callable[[object], object]
    policy_bound: bool


class _RawSettlementServices:
    """Keep component callers' raw outcomes until runtime services are bound."""

    def settle_main(self, outcome: object, _payload: MainPreparedPayload) -> object:
        return outcome

    def settle_capture(self, outcome: object, _payload: CapturePreparedPayload) -> object:
        return outcome


class ExecutionController:
    """Choose the current route; executors own protocol command sequences."""

    def bind_worker_activation(self, activation: WorkerActivationPort) -> None:
        """Install the one Worker activation port before route preparation.

        Binding changes the Worker snapshot authority used by subsequent
        preparation, so it is only valid before a MAIN operation or local
        preparation has become live.
        """

        if (
            activation is None
            or not callable(getattr(activation, "pin_active", None))
            or not callable(getattr(activation, "activate", None))
        ):
            raise TypeError("Worker activation port is required")
        with self._lock:
            if self._worker_activation is not None:
                raise ProtocolError("Worker activation is already bound")
            operation = self.main_operation
            if operation is not None and not operation.terminal:
                raise ProtocolError("Worker activation cannot bind during MAIN")
            if self.capture_scope is not None or self._capture_route is not None:
                raise ProtocolError("Worker activation requires an idle controller route")
            if self._preparations:
                raise ProtocolError("Worker activation cannot bind with outstanding preparation")
            if self._main_worker_activation_pending() or self._arbiter.has_pending_operations:
                raise ProtocolError("Worker activation requires an idle RDBG arbiter")
            self._worker_activation = activation

    def main_idle_fence(self) -> MainIdleTargetFence | None:
        """Return the current MAIN-idle route and target only while confirmed idle."""

        with self._lock:
            snapshot = self._value_route_snapshot_locked()
            return snapshot if isinstance(snapshot, MainIdleTargetFence) else None

    def value_route_snapshot(self) -> CaptureScope | MainIdleTargetFence | None:
        """Copy the one route currently safe for value work without RDBG I/O."""

        with self._lock:
            return self._value_route_snapshot_locked()

    def _value_route_snapshot_locked(self) -> CaptureScope | MainIdleTargetFence | None:
        if self._arbiter.has_pending_operations:
            return None
        route = self._arbiter.current_route
        operation = self.main_operation
        scope = self.capture_scope
        if (
            operation is not None
            and operation.phase is MainPhase.SUSPENDED_CAPTURE
            and scope is not None
            and scope.context_state is CaptureContextState.READY
            and scope.frame_identity is CaptureFrameIdentity.CONFIRMED
            and self._capture_route == route
        ):
            return scope
        if (
            scope is not None
            or route.context_id != "main"
            or (operation is not None and not operation.terminal)
        ):
            return None
        target = (
            operation.target
            if operation is not None and operation.target is not None
            else self._initial_target_id
        )
        return None if target is None else MainIdleTargetFence(route, target)

    def worker_mutation_route(self):
        """Identify the stopped route for a helper inside an admitted ticket."""

        from onec_runtime.execution.worker_mutation import (
            CapturePausedWorkerRoute, MainPausedWorkerRoute,
        )

        with self._lock:
            operation = self.main_operation
            scope = self.capture_scope
            if (
                operation is not None
                and operation.phase is MainPhase.SUSPENDED_CAPTURE
                and scope is not None
                and scope.context_state is CaptureContextState.READY
                and scope.frame_identity is CaptureFrameIdentity.CONFIRMED
                and self._capture_route == self._arbiter.current_route
            ):
                return CapturePausedWorkerRoute(operation, scope)
            if scope is None and (operation is None or operation.terminal):
                target_id = (
                    operation.target
                    if operation is not None and operation.target is not None
                    else self._initial_target_id
                )
                if target_id is not None:
                    return MainPausedWorkerRoute(target_id, operation)
            raise ProtocolError("Worker mutation has no confirmed stopped route")

    def status_facts(self):
        """Copy local operation and stop evidence for public status projection.

        This read does not send RDBG or infer loss from a pending wait. Ticket
        phase belongs to the activity selected under this controller lock.
        """

        from onec_runtime.execution.status_projection import (
            ControllerStatusFacts, ExecutionActivity,
        )

        with self._lock:
            operation = self.main_operation
            scope = self.capture_scope
            active = self._arbiter.active_ticket
            ticket = active
            activity = ExecutionActivity.NONE
            if ticket is self._resume_ticket and ticket is not None:
                activity = ExecutionActivity.CAPTURE_RESUME
            elif ticket is self._worker_activation_main_ticket and ticket is not None:
                activity = ExecutionActivity.MAIN
            elif ticket is self._main_stop_ticket and ticket is not None:
                activity = (
                    ExecutionActivity.DEBUG_RESUME
                    if operation is not None
                    and operation.phase is MainPhase.SUSPENDED_USER
                    else ExecutionActivity.MAIN
                )
            elif ticket is not None and ticket in self._capture_cell_operations:
                activity = ExecutionActivity.CAPTURE
            elif ticket is not None:
                activity = ExecutionActivity.MAINTENANCE
            if ticket is None:
                for candidate, kind in (
                    (self._resume_ticket, ExecutionActivity.CAPTURE_RESUME),
                    (self._worker_activation_main_ticket, ExecutionActivity.MAIN),
                    (self._main_stop_ticket, ExecutionActivity.MAIN),
                ):
                    if candidate is not None and not candidate.status().settled:
                        ticket = candidate
                        activity = kind
                        break
            if ticket is None:
                for candidate in self._capture_cell_operations:
                    if not candidate.status().settled:
                        ticket = candidate
                        activity = ExecutionActivity.CAPTURE
                        break
            ticket_phase = None if ticket is None else ticket.status().phase
            if ticket_phase == "settled":
                activity = ExecutionActivity.NONE
            main_succeeded = None
            if operation is not None and operation.phase is MainPhase.COMPLETED:
                completion = operation.completion
                if isinstance(completion, MainRemoteCompletion):
                    main_succeeded = not completion.error
                elif ticket_phase != "unknown":
                    main_succeeded = False
            return ControllerStatusFacts(
                runtime_generation=self._generation,
                command_id=(
                    self._command_sequence if operation is None
                    else operation.command_id
                ),
                main_phase=None if operation is None else operation.phase,
                capture_context_state=(
                    None if scope is None else scope.context_state
                ),
                capture_frame_identity=(
                    None if scope is None else scope.frame_identity
                ),
                capture_setup=(
                    None if scope is None else scope.setup_snapshot()
                ),
                activity=activity,
                ticket_phase=ticket_phase,
                main_succeeded=main_succeeded,
            )

    def configure_capture_points(
        self, locations: tuple[ModuleLocation, ...],
    ) -> None:
        """Replace idle capture locations for the next MAIN dispatch.

        The next MAIN command installs the full workspace through its owned
        arbiter port. Reconfiguring a suspended or running command requires a
        separate workspace transaction and is refused here.
        """

        if type(locations) is not tuple or any(
            not isinstance(location, ModuleLocation) for location in locations
        ):
            raise TypeError("capture locations must be an immutable location tuple")
        with self._lock:
            operation = self.main_operation
            if operation is not None and not operation.terminal:
                raise ProtocolError("capture points cannot change during MAIN")
            if self._main_worker_activation_pending() or self._arbiter.has_pending_operations:
                raise ProtocolError("RDBG activity prevents capture point changes")
            routes = self._breakpoint_routes
            self._registry = (
                routes.plan_idle_captures(self._registry, locations)
                if routes is not None
                else BreakpointRegistry(
                    self._registry.service, locations, self._registry.users,
                )
            )
            self._preparation_revision += 1

    def _install_main_workspace(self, port: SessionPort) -> None:
        routes = self._breakpoint_routes
        if routes is None:
            port.set_breakpoints(self._registry.full_locations)
        else:
            routes.install_main(self._registry, port=port)

    def _shield_capture_workspace(self, port: SessionPort) -> None:
        routes = self._breakpoint_routes
        if routes is None:
            port.set_breakpoints(self._registry.evaluation_locations)
        else:
            routes.shield_capture(port=port)

    def _restore_capture_workspace(self, port: SessionPort) -> None:
        routes = self._breakpoint_routes
        if routes is None:
            port.set_breakpoints(self._registry.full_locations)
        else:
            routes.restore_capture(port=port)

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
        capture_snapshot_provider: (
            Callable[[MainOperation], RoutePreparationSnapshot] | None
        ) = None,
        settlement_services: object | None = None,
        worker_activation: WorkerActivationPort | None = None,
        message_collector: CaptureMessageCollector | None = None,
        breakpoint_routes: RouteBreakpointWorkspace | None = None,
        initial_target_id: TargetId | None = None,
    ) -> None:
        if type(runtime_generation) is not int or runtime_generation <= 0:
            raise ValueError("runtime_generation must be positive")
        if initial_target_id is not None and not isinstance(initial_target_id, TargetId):
            raise TypeError("initial target identity is invalid")
        self._arbiter = arbiter
        self._main_executor = main_executor
        self._capture_executor = capture_executor
        self._capture_cell_executor = CaptureCellOperationExecutor(
            capture_cell_evaluator, message_collector=message_collector,
        )
        self._capture_message_collector = message_collector
        self._capture_inspection_executor = CaptureInspectionExecutor()
        self._capture_materialization_executor = CaptureMaterializationExecutor()
        self._capture_writeback_executor = CaptureWritebackExecutor()
        self._registry = registry
        self._breakpoint_routes = breakpoint_routes
        self._generation = runtime_generation
        self._initial_target_id = initial_target_id
        self._parser_target = parser_target
        self._snapshot_provider = snapshot_provider
        self._capture_snapshot_provider = capture_snapshot_provider
        self._settlement_services = (
            settlement_services if settlement_services is not None else _RawSettlementServices()
        )
        self._worker_activation = worker_activation
        self._lock = RLock()
        self._command_sequence = 0
        self._stop_sequence = 0
        self.main_operation: MainOperation | None = None
        self.capture_scope: CaptureScope | None = None
        self._capture_route: RouteToken | None = None
        self._resume_ticket: ExecutionTicket | None = None
        self._main_stop_ticket: ExecutionTicket | None = None
        self._worker_activation_main_ticket: ExecutionTicket | None = None
        self._main_worker_leases: dict[int, WorkerActivationLease] = {}
        self._preparation_revision = 0
        self._preparations: dict[object, _PreparationRecord] = {}
        self._capture_cell_operations: WeakKeyDictionary[
            ExecutionTicket, _CaptureCellRepair
        ] = WeakKeyDictionary()

    def await_preparation_context(self) -> PreparationContext | Unavailable:
        """Select one stable statement route without reserving RDBG for lowering.

        A MAIN command's stop ticket is observed outside the controller lock.
        Its initiating notebook waiter may detach without cancelling this wait.
        """

        parser_target = self._parser_target
        provider = self._snapshot_provider
        if parser_target is None or provider is None:
            return Unavailable("Statement preparation is not configured")
        while True:
            with self._lock:
                stop_ticket = self._main_stop_ticket
                if stop_ticket is not None and stop_ticket.status().settled:
                    self._main_stop_ticket = None
                    stop_ticket = None
            if stop_ticket is not None:
                try:
                    stop_ticket.wait_settled()
                except Exception:
                    # A settled MAIN error still needs route classification.
                    pass
                continue

            with self._lock:
                operation_before = self.main_operation
                scope_before = self.capture_scope
                capture_reader = self._capture_snapshot_provider
                capture_before = (
                    capture_reader is not None
                    and operation_before is not None
                    and operation_before.phase is MainPhase.SUSPENDED_CAPTURE
                    and scope_before is not None
                    and scope_before.context_state is CaptureContextState.READY
                    and scope_before.frame_identity is CaptureFrameIdentity.CONFIRMED
                )
            snapshot_reader: Callable[[], RoutePreparationSnapshot] = provider
            if capture_before:
                assert capture_reader is not None and operation_before is not None
                snapshot_reader = (
                    lambda reader=capture_reader, captured_operation=operation_before:
                    reader(captured_operation)
                )
            snapshot = snapshot_reader()
            if not isinstance(snapshot, RoutePreparationSnapshot):
                raise TypeError("snapshot provider must return RoutePreparationSnapshot")
            with self._lock:
                stop_ticket = self._main_stop_ticket
                if stop_ticket is not None:
                    if not stop_ticket.status().settled:
                        continue
                    self._main_stop_ticket = None
                operation = self.main_operation
                scope = self.capture_scope
                if operation is not operation_before or scope is not scope_before:
                    continue
                if self._resume_in_flight() or self._arbiter.has_pending_operations:
                    return Unavailable("RDBG operation is still active")
                if (
                    scope is not None
                    and scope.context_state is CaptureContextState.READY
                    and scope.frame_identity is CaptureFrameIdentity.CONFIRMED
                    and operation is not None
                    and operation.phase is MainPhase.SUSPENDED_CAPTURE
                ):
                    if capture_reader is not None and not capture_before:
                        continue
                    owner: MainOperation | CaptureScope | None = scope
                    policy = CaptureCellPolicy(SnapshotRouteBinding(
                        parser_target, owner=snapshot.owner, version=snapshot.version
                    ))
                elif scope is None and (operation is None or operation.terminal):
                    if capture_before:
                        continue
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
                    context, route, self._preparation_revision, snapshot, owner,
                    snapshot_reader,
                )
                # Abandoned local preparations cannot retain an unbounded history.
                if len(self._preparations) > 1024:
                    self._preparations.pop(next(iter(self._preparations)))
                return context

    def validate_preparation(
        self, context: PreparationContext, guards: object
    ) -> Current | StalePreparation | Unavailable:
        if self._snapshot_provider is None:
            return Unavailable("Statement preparation is not configured")
        with self._lock:
            record = self._preparations.get(context.preparation_nonce)
            if record is None or record.context is not context:
                return StalePreparation("preparation nonce is stale")
            snapshot_reader = record.snapshot_reader
        live = snapshot_reader()
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
            or guards.previous_methods != snapshot.previous_methods
            or not isinstance(live, RoutePreparationSnapshot)
            or live.owner is not snapshot.owner
            or live.version != snapshot.version
            or live.namespace_names != snapshot.namespace_names
            or live.worker_exports != snapshot.worker_exports
            or live.previous_methods != snapshot.previous_methods
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
        if self._snapshot_provider is None:
            return Rejected(Unavailable("Statement preparation is not configured"))
        with self._lock:
            record = self._preparations.get(context.preparation_nonce)
            if record is None or record.context is not context:
                return Rejected(StalePreparation("preparation nonce is stale"))
            snapshot_reader = record.snapshot_reader
        live = snapshot_reader()
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
                worker_intent = payload.worker_intent
                deferred_statement = payload.deferred_statement
            else:
                if not isinstance(payload, CapturePreparedPayload):
                    return Rejected(StalePreparation("CAPTURE prepared payload is invalid"))
                statement = payload.statement
                worker_intent = payload.worker_intent
                deferred_statement = payload.deferred_statement
            if worker_intent is not None:
                if statement is not None:
                    return Rejected(StalePreparation("Worker payload has an eager statement"))
                activation = self._worker_activation
                if activation is None:
                    return Rejected(Unavailable("Worker activation is not configured"))
            elif statement is None:
                return Rejected(Unavailable("Worker artifacts or empty statements are unsupported"))
            selected_statement = deferred_statement if worker_intent is not None else statement
            if (
                isinstance(payload, CapturePreparedPayload)
                and selected_statement is not None
                and payload.dirty_roots != selected_statement.lowering.dirty_roots
            ):
                return Rejected(StalePreparation("CAPTURE dirty roots do not match lowering"))
            source = None if selected_statement is None else selected_statement.lowering.source

            def finalizer(raw_outcome: object) -> object:
                return context.policy.settle(
                    raw_outcome, prepared, self._settlement_services
                )

            def preflight() -> None:
                fresh = snapshot_reader()
                with self._lock:
                    if (
                        self._arbiter.current_route != record.route
                        or not isinstance(fresh, RoutePreparationSnapshot)
                        or fresh.owner is not record.snapshot.owner
                        or fresh.version != record.snapshot.version
                        or fresh.namespace_names != record.snapshot.namespace_names
                        or fresh.worker_exports != record.snapshot.worker_exports
                        or fresh.previous_methods != record.snapshot.previous_methods
                    ):
                        raise StalePreparedDispatch(
                            "statement snapshot changed before dispatch"
                        )

            if worker_intent is not None:
                try:
                    ticket = self._submit_worker_cell(
                        context, payload, worker_intent, source,
                        receipt=receipt, preflight=preflight, finalizer=finalizer,
                    )
                except StaleRoute:
                    return Rejected(StalePreparation("arbiter route changed"))
                self._preparations.pop(context.preparation_nonce, None)
                return Accepted(ticket)

            try:
                if isinstance(context.policy, MainCellPolicy):
                    ticket = self.submit_main(
                        source, _receipt=receipt, _before_first_effect=preflight,
                        _finalizer=finalizer,
                        _message_collector_key=statement.message_collector_key,
                        _prepared_payload=payload,
                    )
                else:
                    ticket = self.submit_capture_cell(
                        source, dirty_roots=payload.dirty_roots,
                        _receipt=receipt, _before_first_effect=preflight,
                        _finalizer=finalizer,
                        _prepared_payload=payload,
                        _message_collector_key=statement.message_collector_key,
                        _prepared_namespace_names=record.snapshot.namespace_names,
                    )
            except StaleRoute:
                return Rejected(StalePreparation("arbiter route changed"))
            self._preparations.pop(context.preparation_nonce, None)
            return Accepted(ticket)

    def _submit_worker_cell(
        self,
        context: PreparationContext,
        payload: MainPreparedPayload | CapturePreparedPayload,
        intent: WorkerCandidateIntent,
        source: str | None,
        *,
        receipt: SubmissionReceipt,
        preflight: Callable[[], None],
        finalizer: Callable[[object], object],
    ) -> ExecutionTicket:
        """Run post-admission Worker activation and its optional deferred statement."""

        activation_port = self._worker_activation
        if activation_port is None:
            raise RuntimeError("Worker activation was not checked")
        route = self._arbiter.current_route
        is_main = isinstance(context.policy, MainCellPolicy)
        scope = self.capture_scope
        if not is_main and (
            scope is None
            or scope.context_state is not CaptureContextState.READY
            or self.main_operation is None
            or self.main_operation.phase is not MainPhase.SUSPENDED_CAPTURE
            or self._capture_route != route
        ):
            raise StaleRoute("CAPTURE route changed")

        ticket: ExecutionTicket | None = None

        def schedule_release(lease: WorkerActivationLease, port: SessionPort) -> None:
            def cleanup(cleanup_port: SessionPort) -> Settlement:
                lease.release(port=cleanup_port)
                return Settlement(None)

            port.register_post_settlement_cleanup(cleanup)

        def plan(port: SessionPort) -> ReadyForPolicy:
            lease: WorkerActivationLease | None = None
            outcome_unknown = False
            operation: MainOperation | None = None
            try:
                preflight()
                lease = activation_port.activate(intent, port=port)
                if source is None:
                    if is_main:
                        with self._lock:
                            self._worker_activation_main_ticket = None
                    raw_outcome: object = None
                    if self._route_settlement_service() is not None:
                        from onec_runtime.execution.settlement import WorkerPublished
                        from onec_runtime.runtime_api import OperationState

                        handle = getattr(lease, "handle", None)
                        if handle is None:
                            raise ProtocolError("Worker activation has no generation handle")
                        raw_outcome = WorkerPublished(
                            handle,
                            self._command_sequence if is_main else scope.identity.main_command_id,
                            OperationState.IDLE if is_main else OperationState.CAPTURED,
                        )
                    return ReadyForPolicy(raw_outcome)
                if is_main:
                    with self._lock:
                        if self.main_operation is not None and not self.main_operation.terminal:
                            raise StaleRoute("MAIN operation was admitted during Worker activation")
                        self._command_sequence += 1
                        assert isinstance(payload, MainPreparedPayload)
                        assert payload.deferred_statement is not None
                        operation = MainOperation(
                            self._command_sequence, None,
                            settler=finalizer,
                            message_collector_key=(
                                payload.deferred_statement.message_collector_key
                            ),
                        )
                        settlement = self._route_settlement_service()
                        if settlement is not None:
                            settlement.register_main(
                                operation, payload,
                                prior_capture_sequence=self._stop_sequence,
                            )
                        self.main_operation = operation
                        self.capture_scope = None
                        self._capture_route = None
                        self._resume_ticket = None
                        self._worker_activation_main_ticket = None
                        self._main_worker_leases[operation.command_id] = lease
                    lease = None
                    stop = self._main_executor.dispatch(
                        operation, source,
                        install_workspace=lambda: self._install_main_workspace(port),
                        before_command_write=lambda: None,
                        before_continue=lambda: None,
                        port=port,
                    )
                    outcome = self._route_stop(port, operation, stop)
                    return ReadyForPolicy(outcome.value, next_route=outcome.next_route)

                assert scope is not None
                settlement = self._route_settlement_service()
                if settlement is not None:
                    assert isinstance(payload, CapturePreparedPayload)
                    settlement.register_capture(
                        scope, payload,
                        base_namespace_names=context.capabilities.namespace_names,
                    )
                cell_operation = CaptureCellOperation(scope.identity)
                outcome = self._capture_cell_executor.execute(
                    scope, source, port=port,
                    shield_workspace=self._shield_capture_workspace,
                    restore_workspace=self._restore_capture_workspace,
                    cleanup=lambda worker: None,
                    result_policy=lambda result: result,
                    operation=cell_operation,
                    message_collector_key=(
                        payload.deferred_statement.message_collector_key
                        if self._capture_message_collector is not None
                        and isinstance(payload, CapturePreparedPayload)
                        and payload.deferred_statement is not None
                        else ""
                    ),
                )
                if isinstance(outcome, ConfirmedFailure):
                    raise outcome.error
                return ReadyForPolicy(outcome.value, next_route=outcome.next_route)
            except WorkerActivationUnknown as error:
                outcome_unknown = True
                error.lease.retain_outcome_unknown(port=port)
                raise
            except OutcomeUnknown:
                outcome_unknown = True
                if lease is not None:
                    lease.retain_outcome_unknown(port=port)
                if is_main:
                    with self._lock:
                        if operation is not None:
                            self._retain_main_worker_lease(operation, port)
                        if self.main_operation is not None and not self.main_operation.terminal:
                            self.main_operation.mark_unknown()
                raise
            except BaseException:
                if is_main:
                    with self._lock:
                        if operation is None:
                            self._worker_activation_main_ticket = None
                        elif operation.phase is MainPhase.ADMITTED:
                            operation.fail_before_dispatch()
                            self._schedule_main_worker_lease_release(operation, port)
                raise
            finally:
                if lease is not None and not outcome_unknown:
                    schedule_release(lease, port)

        ticket = self._arbiter.submit(route, plan, finalizer=finalizer)
        if is_main:
            self._worker_activation_main_ticket = ticket
        receipt.adopt(ticket)
        self._preparation_revision += 1
        self._arbiter.dispatch(ticket)
        return ticket

    def request_stop(self, ticket: ExecutionTicket) -> object:
        return self._arbiter.request_stop(ticket)

    def _main_worker_activation_pending(self) -> bool:
        ticket = self._worker_activation_main_ticket
        if ticket is None:
            return False
        if ticket.status().settled:
            self._worker_activation_main_ticket = None
            return False
        return True

    @staticmethod
    def _schedule_worker_lease_release(
        lease: WorkerActivationLease, port: SessionPort,
    ) -> None:
        def cleanup(cleanup_port: SessionPort) -> Settlement:
            lease.release(port=cleanup_port)
            return Settlement(None)

        port.register_post_settlement_cleanup(cleanup)

    def _schedule_main_worker_lease_release(
        self, operation: MainOperation, port: SessionPort,
    ) -> None:
        lease = self._main_worker_leases.get(operation.command_id)
        if lease is None:
            return

        def cleanup(cleanup_port: SessionPort) -> Settlement:
            lease.release(port=cleanup_port)
            with self._lock:
                if self._main_worker_leases.get(operation.command_id) is lease:
                    self._main_worker_leases.pop(operation.command_id)
            return Settlement(None)

        port.register_post_settlement_cleanup(cleanup)

    def _retain_main_worker_lease(
        self, operation: MainOperation, port: SessionPort,
    ) -> None:
        lease = self._main_worker_leases.get(operation.command_id)
        if lease is not None:
            lease.retain_outcome_unknown(port=port)

    def submit_main(
        self, instruction: str, *,
        _receipt: SubmissionReceipt | None = None,
        _before_first_effect: Callable[[], None] | None = None,
        _finalizer: Callable[[object], object] | None = None,
        _message_collector_key: str = "",
        _prepared_payload: MainPreparedPayload | None = None,
    ) -> ExecutionTicket:
        """Admit one MAIN command and return its first-stop ticket."""

        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError("MAIN instruction must be non-empty text")
        with self._lock:
            if self._main_worker_activation_pending():
                raise ProtocolError("A MAIN Worker activation is still active")
            if self.main_operation is not None and not self.main_operation.terminal:
                raise ProtocolError("A MAIN command is still active")
            route = self._arbiter.current_route
            self._command_sequence += 1
            operation = MainOperation(
                self._command_sequence, None,
                settler=_finalizer,
                message_collector_key=_message_collector_key,
            )
            self.main_operation = operation
            self.capture_scope = None
            self._capture_route = None
            self._resume_ticket = None

            def plan(port: SessionPort) -> Settlement | ReadyForPolicy:
                try:
                    if _before_first_effect is not None:
                        _before_first_effect()
                    activation = self._worker_activation
                    if activation is not None:
                        lease = activation.pin_active(port=port)
                        if lease is not None:
                            self._main_worker_leases[operation.command_id] = lease
                    stop = self._main_executor.dispatch(
                        operation,
                        instruction,
                        install_workspace=lambda: self._install_main_workspace(port),
                        before_command_write=lambda: None,
                        before_continue=lambda: None,
                        port=port,
                    )
                    outcome = self._route_stop(port, operation, stop)
                except OutcomeUnknown:
                    self._retain_main_worker_lease(operation, port)
                    raise
                except BaseException:
                    if operation.phase is MainPhase.ADMITTED:
                        operation.fail_before_dispatch()
                        self._schedule_main_worker_lease_release(operation, port)
                    raise
                if operation.settler is None:
                    return outcome
                return ReadyForPolicy(
                    outcome.value, next_route=outcome.next_route
                )

            ticket: ExecutionTicket | None = None
            try:
                settlement = self._route_settlement_service()
                if settlement is not None and _prepared_payload is not None:
                    settlement.register_main(
                        operation, _prepared_payload,
                        prior_capture_sequence=self._stop_sequence,
                    )
                ticket = self._arbiter.submit(route, plan, finalizer=operation.settler)
                self._main_stop_ticket = ticket
                if _receipt is not None:
                    _receipt.adopt(ticket)
                self._preparation_revision += 1
                self._arbiter.dispatch(ticket)
            except BaseException:
                if _receipt is None or _receipt.ticket is None:
                    if settlement is not None and _prepared_payload is not None:
                        settlement.discard_main(operation)
                    if self._main_stop_ticket is ticket:
                        self._main_stop_ticket = None
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
        result_policy: Callable[[object], object] | None = None,
        _receipt: SubmissionReceipt | None = None,
        _before_first_effect: Callable[[], None] | None = None,
        _finalizer: Callable[[object], object] | None = None,
        _prepared_payload: CapturePreparedPayload | None = None,
        _message_collector_key: str = "",
        _prepared_namespace_names: tuple[str, ...] = (),
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
            cell_operation = CaptureCellOperation(scope.identity)

            def plan(port: SessionPort) -> Settlement | ReadyForPolicy | ConfirmedFailure:
                if _before_first_effect is not None:
                    _before_first_effect()
                activation = self._worker_activation
                if activation is not None:
                    lease = activation.pin_active(port=port)
                    if lease is not None:
                        self._schedule_worker_lease_release(lease, port)
                scope.admit_cell_dirty_roots(dirty_roots)
                outcome = self._capture_cell_executor.execute(
                    scope,
                    lowered_source,
                    port=port,
                    shield_workspace=self._shield_capture_workspace,
                    restore_workspace=self._restore_capture_workspace,
                    cleanup=lambda worker: None,
                    result_policy=selected_policy,
                    operation=cell_operation,
                    message_collector_key=(
                        _message_collector_key
                        if self._capture_message_collector is not None else ""
                    ),
                )
                if isinstance(outcome, ConfirmedFailure):
                    return outcome
                if _finalizer is None:
                    return outcome
                return ReadyForPolicy(
                    outcome.value, next_route=outcome.next_route
                )

            settlement = self._route_settlement_service()
            if settlement is not None and _prepared_payload is not None:
                settlement.register_capture(
                    scope, _prepared_payload,
                    base_namespace_names=_prepared_namespace_names,
                )
            try:
                ticket = self._arbiter.submit(route, plan, finalizer=_finalizer)
            except BaseException:
                if settlement is not None and _prepared_payload is not None:
                    settlement.discard_capture(_prepared_payload)
                raise
            if _receipt is not None:
                _receipt.adopt(ticket)
            self._capture_cell_operations[ticket] = _CaptureCellRepair(
                cell_operation, scope, selected_policy, _finalizer is not None
            )
            self._preparation_revision += 1
            self._arbiter.dispatch(ticket)
            return ticket

    def _route_settlement_service(self):
        """Use typed publication only when the composed public service is bound."""

        from onec_runtime.execution.settlement import RouteSettlementService

        service = self._settlement_services
        return service if isinstance(service, RouteSettlementService) else None

    def repair_capture_cell_after_restore(self, ticket: ExecutionTicket) -> None:
        """Resume one ticket whose confirmed eval lost its workspace restore.

        The same operation record supplies the original result. A post-dispatch
        ambiguous restore remains blocked by the arbiter port until its remote
        outcome is established; this method cannot repeat that command blindly.
        """

        with self._lock:
            repair = self._capture_cell_operations.get(ticket)
            scope = self.capture_scope
            if (
                repair is None
                or scope is not repair.scope
                or scope is None
                or scope.context_state is not CaptureContextState.READY
                or scope.frame_identity is not CaptureFrameIdentity.CONFIRMED
                or ticket is not self._arbiter.active_ticket
                or ticket.status().phase != "unknown"
                or repair.operation.confirmed_result is None
                or repair.operation.workspace_restored
                or self._capture_route != self._arbiter.current_route
            ):
                raise ProtocolError("No confirmed CAPTURE restore can be repaired")

            def plan(port: SessionPort) -> Settlement | ReadyForPolicy | ConfirmedFailure:
                outcome = self._capture_cell_executor.repair_confirmed_result(
                    repair.operation,
                    repair.scope,
                    port=port,
                    restore_workspace=self._restore_capture_workspace,
                    cleanup=lambda worker: None,
                    result_policy=repair.result_policy,
                )
                if isinstance(outcome, ConfirmedFailure):
                    return outcome
                if repair.policy_bound:
                    return ReadyForPolicy(
                        outcome.value, next_route=outcome.next_route
                    )
                return outcome

            self._arbiter.reconcile(ticket, plan)

    def reconcile_capture_pending_eval(self, ticket: ExecutionTicket) -> None:
        """Observe an accepted CAPTURE eval and finish its original ticket."""

        with self._lock:
            repair = self._capture_cell_operations.get(ticket)
            scope = self.capture_scope
            status = ticket.status()
            pending = status.pending_capability
            if (
                repair is None
                or scope is not repair.scope
                or scope is None
                or scope.context_state is not CaptureContextState.READY
                or scope.frame_identity is not CaptureFrameIdentity.CONFIRMED
                or ticket is not self._arbiter.active_ticket
                or status.phase != "unknown"
                or pending is None
                or not repair.operation.evaluation_started
                or self._capture_route != self._arbiter.current_route
            ):
                raise ProtocolError("No owned CAPTURE eval can be reconciled")

            awaiting_messages = repair.operation.confirmed_result is not None
            if awaiting_messages and (
                not repair.operation.workspace_restored
                or not repair.operation.message_collection_started
                or repair.operation.message_collection_complete
            ):
                raise ProtocolError("No owned CAPTURE message eval can be reconciled")

            def plan(port: SessionPort) -> Settlement | ReadyForPolicy | ConfirmedFailure:
                reconcile = (
                    self._capture_cell_executor.reconcile_pending_messages
                    if awaiting_messages
                    else self._capture_cell_executor.reconcile_pending_result
                )
                outcome = reconcile(
                    repair.operation,
                    repair.scope,
                    pending,
                    port=port,
                    restore_workspace=self._restore_capture_workspace,
                    cleanup=lambda worker: None,
                    result_policy=repair.result_policy,
                )
                if isinstance(outcome, ConfirmedFailure):
                    return outcome
                if repair.policy_bound:
                    return ReadyForPolicy(
                        outcome.value, next_route=outcome.next_route
                    )
                return outcome

            self._arbiter.reconcile(ticket, plan)

    def submit_resume(
        self, *, dirty_roots: tuple[str, ...] = (),
        successor_locations: tuple[ModuleLocation, ...] | None = None,
    ) -> ExecutionTicket:
        """Rearm successor points, write dirty roots and resume the same MAIN."""

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
            if type(dirty_roots) is not tuple:
                raise TypeError("explicit dirty roots must be an immutable tuple")
            for root in dirty_roots:
                build_live_capture_root_transfer_call(root)
            if scope.writeback_ledger is not None:
                frozen = {root.casefold() for root in scope.dirty_roots}
                if any(root.casefold() not in frozen for root in dirty_roots):
                    raise ProtocolError(
                        "CAPTURE writeback cannot admit a new root after resume began"
                    )
            if successor_locations is not None:
                if type(successor_locations) is not tuple or any(
                    type(location) is not ModuleLocation
                    for location in successor_locations
                ):
                    raise TypeError("successor capture locations are invalid")
                if self._breakpoint_routes is None:
                    raise ProtocolError(
                        "CAPTURE successor rearm requires a shared breakpoint workspace"
                    )

            def plan(port: SessionPort) -> Settlement | ReadyForPolicy:
                try:
                    if scope.writeback_ledger is None:
                        scope.admit_cell_dirty_roots(dirty_roots)
                    if successor_locations is not None:
                        routes = self._breakpoint_routes
                        assert routes is not None
                        routes.rearm_captured_successor(
                            self._registry, successor_locations, port=port,
                        )
                        with self._lock:
                            if (
                                self.capture_scope is not scope
                                or self.main_operation is not operation
                                or self._capture_route != route
                            ):
                                raise ProtocolError(
                                    "CAPTURE successor route changed during rearm"
                                )
                            self._registry = BreakpointRegistry(
                                self._registry.service, successor_locations,
                                self._registry.users,
                            )
                    ledger = scope.begin_writeback()
                    assert scope.kernel_stack_level is not None
                    self._capture_writeback_executor.flush(
                        port, ledger, stack_level=scope.kernel_stack_level
                    )
                    self._capture_executor.end_scope(
                        scope, port=CaptureSetupAdapter(port)
                    )
                    self._main_executor.continue_command(operation, port=port)
                except BaseException:
                    if operation.phase is MainPhase.UNKNOWN:
                        scope.mark_unverified()
                        self._retain_main_worker_lease(operation, port)
                    raise
                scope.mark_closed()
                with self._lock:
                    self.capture_scope = None
                    self._capture_route = None
                    self._resume_ticket = None
                next_route = self._next_route("main")
                port.handoff_route(next_route)
                stop = self._main_executor.await_stop(port=port)
                outcome = self._route_stop(port, operation, stop)
                if operation.settler is None:
                    return outcome
                return ReadyForPolicy(
                    outcome.value, next_route=outcome.next_route
                )

            ticket = self._arbiter.submit(route, plan, finalizer=operation.settler)
            self._resume_ticket = ticket
            self._main_stop_ticket = ticket
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

    def submit_capture_variable_page(
        self, *, stack_level: int, start: int, stop: int,
    ) -> ExecutionTicket:
        """Read a bounded page of safe variable names from the stopped frame."""

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
                page = self._capture_inspection_executor.read_variable_page(
                    scope,
                    stack_level=stack_level,
                    start=start,
                    stop=stop,
                    port=port,
                )
                return Settlement(page)

            ticket = self._arbiter.submit(route, plan)
            self._preparation_revision += 1
            self._arbiter.dispatch(ticket)
            return ticket

    def submit_capture_materialization(
        self, transfer_plan: CaptureMaterializationPlan,
        *, _before_first_effect: Callable[[], None] | None = None,
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
                if _before_first_effect is not None:
                    _before_first_effect()
                return self._capture_materialization_executor.execute(
                    scope,
                    transfer_plan,
                    port=port,
                    shield_workspace=self._shield_capture_workspace,
                    restore_workspace=self._restore_capture_workspace,
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
                    shield_workspace=self._shield_capture_workspace,
                    restore_workspace=self._restore_capture_workspace,
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

            def plan(port: SessionPort) -> Settlement | ReadyForPolicy:
                try:
                    stop = self._main_executor.resume(operation, port=port)
                    outcome = self._route_stop(port, operation, stop)
                    if operation.settler is None:
                        return outcome
                    return ReadyForPolicy(
                        outcome.value, next_route=outcome.next_route
                    )
                except BaseException:
                    if operation.phase is MainPhase.UNKNOWN:
                        self._retain_main_worker_lease(operation, port)
                    raise

            ticket = self._arbiter.submit(
                route, plan, finalizer=operation.settler,
            )
            self._main_stop_ticket = ticket
            self._preparation_revision += 1
            self._arbiter.dispatch(ticket)
            return ticket

    def _route_stop(
        self, port: SessionPort, operation: MainOperation, stop: StopEvent
    ) -> Settlement:
        routes = self._breakpoint_routes
        worker_locations = (
            () if routes is None
            else routes.worker_owner.confirmed_snapshot.worker_slots
        )
        reason = classify_stop(
            stop, self._registry, worker_locations=worker_locations,
        ).reason
        if reason is StopReason.MAIN_SERVICE:
            decoded_error: list[str] = []
            try:
                try:
                    completion = read_main_completion(
                        port, operation,
                        message_collector_key=operation.message_collector_key,
                        on_error_decoded=decoded_error.append,
                    )
                except (ProtocolError, BslExecutionError):
                    if (
                        operation.phase is not MainPhase.COMPLETED
                        or self._route_settlement_service() is None
                    ):
                        raise
                    from onec_runtime.execution.reply_publication import (
                        MainConfirmedDecodeFailure,
                    )

                    return Settlement(MainConfirmedDecodeFailure(
                        operation,
                        remote_error=decoded_error[0] if decoded_error else "",
                    ))
                operation.complete(completion)
                return Settlement(
                    MainYield(MainYieldKind.COMPLETED, operation, completion=completion)
                )
            finally:
                # The matching command ID terminalizes MAIN before result or
                # message decoding. Its pin still needs a child cleanup ticket.
                if operation.terminal:
                    self._schedule_main_worker_lease_release(operation, port)
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
