"""Explicit composition seam for a post-bootstrap execution cutover."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from onec_runtime.breakpoint_workspace import (
    BreakpointWorkspaceController, WorkspaceSnapshot,
)
from onec_runtime.bsl.source_maps import SourceUnitRef
from onec_runtime.errors import ProtocolError
from onec_runtime.execution.arbiter import EvaluationSession
from onec_runtime.execution.breakpoint_routes import RouteBreakpointWorkspace
from onec_runtime.execution.composition import (
    ExecutionCore, bind_worker_universe_activation, build_execution_core,
)
from onec_runtime.execution.contracts import PreparedCell, ReplyPresenter
from onec_runtime.execution.namespace import RuntimeNamespaceOwner
from onec_runtime.execution.provenance import PreparedExecutionProvenanceReader
from onec_runtime.execution.public_facade import PublicExecutionFacade
from onec_runtime.execution.reply_presenter import RuntimeReplyPresenter
from onec_runtime.execution.settlement import RouteSettlementService
from onec_runtime.execution.source_identity import NotebookSourceIdentityFactory
from onec_runtime.execution.status_projection import ExecutionStatusProjection
from onec_runtime.execution.value_materialization_router import ValueMaterializationRouter
from onec_runtime.execution.worker import WorkerActivationPort
from onec_runtime.execution.worker_activation import (
    WorkerActivationSnapshot, WorkerMaterializationSnapshot,
    WorkerUniverseActivationAdapter,
)
from onec_runtime.execution.worker_breakpoint_service import WorkerBreakpointService
from onec_runtime.rdbg.models import DebugTarget, ModuleLocation, TargetId
from onec_runtime.runtime_contracts import OperationExecutionProvenance
from onec_runtime.server_worker import NotebookWorkerArtifactBuilder
from onec_runtime.worker_breakpoints import WorkerBreakpointCoordinator
from onec_runtime.worker_universe import WorkerUniverseRegistry


@dataclass(frozen=True, slots=True)
class PostBootstrapExecution:
    """One independently composed execution core and its public facade."""

    core: ExecutionCore
    facade: PublicExecutionFacade
    status: ExecutionStatusProjection
    source_identity: NotebookSourceIdentityFactory


@dataclass(frozen=True, slots=True)
class FreshPostBootstrapExecution:
    """Owners created from one verified, newly bootstrapped stopped target."""

    execution: PostBootstrapExecution
    namespace: RuntimeNamespaceOwner
    worker_universe: WorkerUniverseRegistry
    breakpoint_workspace: BreakpointWorkspaceController
    breakpoint_routes: RouteBreakpointWorkspace
    worker_breakpoints: WorkerBreakpointCoordinator
    worker_breakpoint_service: WorkerBreakpointService
    worker_activation: WorkerActivationPort


def compose_post_bootstrap_execution(
    session: EvaluationSession,
    service_location: ModuleLocation,
    *,
    runtime_generation: int,
    context_generation: int,
    initial_target_id: TargetId,
    capture_locations: tuple[ModuleLocation, ...],
    namespace: RuntimeNamespaceOwner,
    worker_snapshot: Callable[[], WorkerActivationSnapshot],
    worker_materialization_snapshot: Callable[[], WorkerMaterializationSnapshot],
    settlement_services: RouteSettlementService,
    reply_presenter: ReplyPresenter,
    retained_source_units: Callable[[], Iterable[SourceUnitRef]],
    provenance_reader: (
        Callable[[PreparedCell], OperationExecutionProvenance] | None
    ) = None,
    worker_activation: WorkerActivationPort | None = None,
    breakpoint_routes: RouteBreakpointWorkspace | None = None,
) -> PostBootstrapExecution:
    """Compose the new owner path from already-verified bootstrap resources.

    This factory does not inspect or start a RuntimeSession. Callers provide
    all state owners and public reply/provenance adapters explicitly so a
    later cutover can occur without inheriting legacy RuntimeApi ownership.
    """

    if not isinstance(namespace, RuntimeNamespaceOwner):
        raise TypeError("post-bootstrap namespace owner is required")
    if not isinstance(settlement_services, RouteSettlementService):
        raise TypeError("post-bootstrap settlement service is required")
    if (
        not callable(worker_snapshot)
        or not callable(worker_materialization_snapshot)
        or not callable(retained_source_units)
    ):
        raise TypeError("post-bootstrap state readers must be callable")
    if provenance_reader is not None and not callable(provenance_reader):
        raise TypeError("post-bootstrap provenance reader must be callable")
    source_identity = NotebookSourceIdentityFactory(retained_source_units)
    core = build_execution_core(
        session,
        service_location,
        runtime_generation=runtime_generation,
        capture_locations=capture_locations,
        snapshot_provider=namespace.snapshot,
        capture_snapshot_provider=lambda operation: namespace.snapshot(
            speculative_names=settlement_services.pending_main_names(operation)
        ),
        reply_presenter=reply_presenter,
        settlement_services=settlement_services,
        worker_activation=worker_activation,
        breakpoint_routes=breakpoint_routes,
        initial_target_id=initial_target_id,
    )
    try:
        status = ExecutionStatusProjection(
            controller_facts=core.controller.status_facts,
            worker_snapshot=worker_snapshot,
            namespace=namespace,
        )
        facade = PublicExecutionFacade(
            core.pipeline,
            core.controller,
            core.arbiter,
            source_unit_factory=source_identity,
            source_identity=source_identity,
            status_reader=status.status,
            namespace_reader=status.namespace_snapshot,
            provenance_reader=(
                PreparedExecutionProvenanceReader()
                if provenance_reader is None
                else provenance_reader
            ),
            value_router_factory=lambda handoff: ValueMaterializationRouter(
                core.controller, core.arbiter,
                runtime_generation=runtime_generation,
                context_generation=context_generation,
                worker_catalog_snapshot=worker_materialization_snapshot,
                wait_handoff=handoff,
            ),
        )
    except BaseException:
        core.arbiter.close(timeout=3)
        raise
    return PostBootstrapExecution(core, facade, status, source_identity)


def compose_fresh_post_bootstrap_execution(
    session: EvaluationSession,
    service_location: ModuleLocation,
    *,
    runtime_generation: int,
    stopped_target: DebugTarget,
    capture_locations: tuple[ModuleLocation, ...],
    notebook_builder: NotebookWorkerArtifactBuilder,
    target_profile: str = "notebook-worker",
) -> FreshPostBootstrapExecution:
    """Create all new execution owners after bootstrap stopped one exact target.

    This helper is valid only for a fresh bootstrap: no notebook statement has
    run, no namespace name is confirmed, and no Worker generation exists.
    Those facts justify the empty namespace and context generation ``1``.
    It deliberately remains separate from ``RuntimeSession.start`` until that
    path supplies equivalent authoritative bootstrap evidence.
    """

    target = _fresh_stopped_target(session, stopped_target)
    if type(runtime_generation) is not int or runtime_generation <= 0:
        raise ValueError("runtime generation must be positive")
    if not callable(notebook_builder):
        raise TypeError("notebook Worker artifact builder is required")
    if not isinstance(target_profile, str) or not target_profile:
        raise ValueError("Worker target profile is required")

    # The adapter is bound after the core exists. These readers are never
    # observed before that binding returns, but retain a valid empty snapshot
    # to keep the fresh namespace construction deterministic.
    activation: dict[str, WorkerUniverseActivationAdapter] = {}

    def worker_snapshot() -> WorkerActivationSnapshot:
        adapter = activation.get("adapter")
        if adapter is None:
            return WorkerActivationSnapshot(0, (), None, None)
        return adapter.snapshot()

    def worker_materialization_snapshot() -> WorkerMaterializationSnapshot:
        adapter = activation.get("adapter")
        if adapter is None:
            return WorkerMaterializationSnapshot(0, ())
        return adapter.materialization_snapshot()

    context_generation = 1
    namespace = RuntimeNamespaceOwner(
        runtime_generation, context_generation, worker_snapshot=worker_snapshot,
    )
    worker_universe = WorkerUniverseRegistry(
        runtime_generation=runtime_generation,
        context_generation=context_generation,
    )
    workspace = BreakpointWorkspaceController(
        session,
        WorkspaceSnapshot(0, service_location, capture_locations, (), (), False),
    )
    routes = RouteBreakpointWorkspace(workspace)
    worker_breakpoints = WorkerBreakpointCoordinator(session_id=target.target_id.id)
    settlement = RouteSettlementService(namespace)
    status: dict[str, ExecutionStatusProjection] = {}

    def reply_status():
        try:
            return status["projection"].status()
        except KeyError as error:
            raise RuntimeError("post-bootstrap status is not bound") from error

    composed = compose_post_bootstrap_execution(
        session,
        service_location,
        runtime_generation=runtime_generation,
        context_generation=context_generation,
        initial_target_id=target.target_id,
        capture_locations=capture_locations,
        namespace=namespace,
        worker_snapshot=worker_snapshot,
        worker_materialization_snapshot=worker_materialization_snapshot,
        settlement_services=settlement,
        reply_presenter=RuntimeReplyPresenter(reply_status),
        retained_source_units=settlement.retained_source_units,
        breakpoint_routes=routes,
    )
    status["projection"] = composed.status
    try:
        bound = bind_worker_universe_activation(
            composed.core,
            worker_universe,
            notebook_builder=notebook_builder,
            breakpoints=worker_breakpoints,
            target_profile=target_profile,
        )
        activation["adapter"] = bound

        def require_worker_mutation_boundary() -> None:
            composed.core.controller.worker_mutation_route()

        worker_breakpoint_service = WorkerBreakpointService(
            composed.core.arbiter,
            worker_breakpoints,
            workspace,
            require_mutation_boundary=require_worker_mutation_boundary,
            wait_handoff=composed.facade._wait_handoff,
        )
        composed.facade.bind_worker_breakpoint_service(worker_breakpoint_service)
    except BaseException:
        composed.facade.close()
        raise
    return FreshPostBootstrapExecution(
        composed, namespace, worker_universe, workspace, routes,
        worker_breakpoints, worker_breakpoint_service, bound,
    )


def _fresh_stopped_target(
    session: EvaluationSession, stopped_target: DebugTarget,
) -> DebugTarget:
    """Return the exact target a completed fresh bootstrap left stopped."""

    target = getattr(session, "target", None)
    if (
        not isinstance(stopped_target, DebugTarget)
        or not isinstance(target, DebugTarget)
        or target.target_id != stopped_target.target_id
        or target.state.casefold() != "stopped"
        or stopped_target.state.casefold() != "stopped"
    ):
        raise ProtocolError("fresh post-bootstrap composition requires an exact stopped target")
    return stopped_target


__all__ = [
    "FreshPostBootstrapExecution",
    "PostBootstrapExecution",
    "compose_fresh_post_bootstrap_execution",
    "compose_post_bootstrap_execution",
]
