"""Construct one post-bootstrap RDBG owner and its execution route bindings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
from uuid import uuid4

from onec_runtime.bsl.parser_target import PythonParserTarget
from onec_runtime.errors import ProtocolError
from onec_runtime.execution.arbiter import EvaluationSession, RdbgArbiter, RouteToken
from onec_runtime.execution.capture.cell_evaluator import CaptureCellEvaluator
from onec_runtime.execution.capture.executor import CaptureExecutor
from onec_runtime.execution.capture.messages import CaptureMessageCollector
from onec_runtime.execution.breakpoint_routes import RouteBreakpointWorkspace
from onec_runtime.execution.common import NotebookCommonParser
from onec_runtime.execution.contracts import PreparationSnapshots, ReplyPresenter
from onec_runtime.execution.controller.controller import ExecutionController
from onec_runtime.execution.main import MainExecutor
from onec_runtime.execution.main import MainOperation
from onec_runtime.execution.pipeline import CellExecutionPipeline
from onec_runtime.execution.snapshot_binding import RoutePreparationSnapshot
from onec_runtime.execution.worker import WorkerActivationPort
from onec_runtime.execution.worker_activation import WorkerUniverseActivationAdapter
from onec_runtime.execution.worker_breakpoint_workspace import WorkerBreakpointWorkspace
from onec_runtime.execution.worker_mutation import WorkerMutationInstructionRunner
from onec_runtime.rdbg.models import ModuleLocation, TargetId
from onec_runtime.stop_routing import BreakpointRegistry
from onec_runtime.server_worker import NotebookWorkerArtifactBuilder
from onec_runtime.table_value import evaluation_to_python
from onec_runtime.worker_breakpoints import WorkerBreakpointCoordinator
from onec_runtime.worker_universe import WorkerModuleArtifact, WorkerUniverseRegistry


@dataclass(slots=True)
class ExecutionCore:
    """One arbiter, controller and generic cell pipeline for one runtime."""

    arbiter: RdbgArbiter
    controller: ExecutionController
    pipeline: CellExecutionPipeline
    parser_target: PythonParserTarget
    breakpoint_routes: RouteBreakpointWorkspace | None
    worker_activation: WorkerUniverseActivationAdapter | None = None


class _BoundSnapshotReader:
    """Read the immutable snapshot already selected by the controller."""

    def read_for(self, capabilities: object) -> PreparationSnapshots:
        if not isinstance(capabilities, RoutePreparationSnapshot):
            raise TypeError("Execution route has no preparation snapshot")
        return capabilities.for_pipeline()


def build_execution_core(
    session: EvaluationSession,
    service_location: ModuleLocation,
    *,
    runtime_generation: int,
    capture_locations: tuple[ModuleLocation, ...],
    snapshot_provider: Callable[[], RoutePreparationSnapshot],
    capture_snapshot_provider: (
        Callable[[MainOperation], RoutePreparationSnapshot] | None
    ) = None,
    reply_presenter: ReplyPresenter,
    settlement_services: object | None = None,
    worker_activation: WorkerActivationPort | None = None,
    breakpoint_routes: RouteBreakpointWorkspace | None = None,
    initial_target_id: TargetId | None = None,
    parser_target: PythonParserTarget | None = None,
) -> ExecutionCore:
    """Bind MAIN and CAPTURE to one RDBG worker after bootstrap has stopped."""

    parser = parser_target or PythonParserTarget.from_generated()
    registry = BreakpointRegistry(service_location, capture_locations)
    arbiter = RdbgArbiter(
        session, RouteToken(f"runtime-{runtime_generation}-{uuid4().hex}", 1, 0, "main")
    )
    try:
        controller = ExecutionController(
            arbiter,
            MainExecutor(poll_interval_s=6.0),
            CaptureExecutor(
                None, service_location, decode_command_id=evaluation_to_python
            ),
            CaptureCellEvaluator(),
            registry,
            runtime_generation=runtime_generation,
            parser_target=parser,
            snapshot_provider=snapshot_provider,
            capture_snapshot_provider=capture_snapshot_provider,
            settlement_services=settlement_services,
            worker_activation=worker_activation,
            breakpoint_routes=breakpoint_routes,
            initial_target_id=initial_target_id,
            message_collector=(
                CaptureMessageCollector()
                if settlement_services is not None else None
            ),
        )
        pipeline = CellExecutionPipeline(
            NotebookCommonParser(parser), controller, _BoundSnapshotReader(),
            reply_presenter,
        )
    except BaseException:
        arbiter.close(timeout=3)
        raise
    return ExecutionCore(
        arbiter, controller, pipeline, parser, breakpoint_routes,
    )


def bind_worker_universe_activation(
    core: ExecutionCore,
    host: WorkerUniverseRegistry,
    *,
    notebook_builder: NotebookWorkerArtifactBuilder,
    breakpoints: WorkerBreakpointCoordinator,
    worker_breakpoints_present: Callable[[], bool] | None = None,
    target_profile: str = "notebook-worker",
    base_artifacts: tuple[WorkerModuleArtifact, ...] = (),
) -> WorkerUniverseActivationAdapter:
    """Bind Worker publication to an idle post-bootstrap execution core.

    The runner uses the controller's live registry and fresh stopped-route
    view.  Its only debugger capability is the ``SessionPort`` supplied by
    the already admitted activation plan.
    """

    if not isinstance(core, ExecutionCore):
        raise TypeError("post-bootstrap execution core is required")
    routes = core.breakpoint_routes
    if routes is None:
        raise ProtocolError("Worker activation requires a shared breakpoint workspace")
    controller = core.controller
    runner = WorkerMutationInstructionRunner(
        controller._main_executor,
        controller._capture_cell_executor,
        registry_provider=lambda: controller._registry,
        breakpoint_routes=routes,
        route_provider=controller.worker_mutation_route,
    )
    adapter = WorkerUniverseActivationAdapter(
        host,
        notebook_builder=notebook_builder,
        instruction_runner=runner,
        worker_breakpoints_present=(
            worker_breakpoints_present
            if worker_breakpoints_present is not None
            else lambda: bool(breakpoints.list_statuses())
        ),
        breakpoint_workspace=WorkerBreakpointWorkspace(
            breakpoints, routes.worker_owner,
        ),
        target_profile=target_profile,
        base_artifacts=base_artifacts,
    )
    controller.bind_worker_activation(adapter)
    core.worker_activation = adapter
    return adapter
