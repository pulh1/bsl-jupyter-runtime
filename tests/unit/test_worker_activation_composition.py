"""Post-bootstrap Worker activation uses the controller's single RDBG route."""

from uuid import UUID

import pytest

from onec_runtime.breakpoint_workspace import (
    BreakpointWorkspaceController,
    WorkspaceSnapshot,
)
from onec_runtime.execution.breakpoint_routes import RouteBreakpointWorkspace
from onec_runtime.execution.composition import (
    bind_worker_universe_activation,
    build_execution_core,
)
from onec_runtime.execution.snapshot_binding import RoutePreparationSnapshot
from onec_runtime.execution.worker_mutation import MainPausedWorkerRoute
from onec_runtime.execution.main.idle_materialization import MainIdleTargetFence
from onec_runtime.errors import ProtocolError
from onec_runtime.worker_breakpoints import WorkerBreakpointCoordinator
from onec_runtime.worker_universe import WorkerUniverseRegistry

from test_execution_controller_routes import BUSINESS, KERNEL, CompleteSession, TARGET


class _Replies:
    def diagnostic_reply(self, diagnostic):
        raise AssertionError(diagnostic.message)

    def unavailable_reply(self, unavailable):
        raise AssertionError(unavailable.reason)


def _core():
    session = CompleteSession()
    workspace = BreakpointWorkspaceController(
        session, WorkspaceSnapshot(0, KERNEL, (BUSINESS,), (), (), False),
    )
    return build_execution_core(
        session,
        KERNEL,
        runtime_generation=1,
        capture_locations=(BUSINESS,),
        initial_target_id=TARGET,
        snapshot_provider=lambda: RoutePreparationSnapshot(object(), 1, (), ()),
        reply_presenter=_Replies(),
        breakpoint_routes=RouteBreakpointWorkspace(workspace),
    ), workspace


def _bind(core):
    return bind_worker_universe_activation(
        core,
        WorkerUniverseRegistry(runtime_generation=1, context_generation=1),
        notebook_builder=lambda *_args, **_kwargs: None,
        breakpoints=WorkerBreakpointCoordinator(session_id=UUID(int=44)),
    )


def test_post_bootstrap_worker_activation_uses_controller_route_and_workspace_owner() -> None:
    """A changed controller route or workspace owner must break Worker mutation wiring."""

    core, workspace = _core()
    try:
        adapter = _bind(core)

        assert core.worker_activation is adapter
        assert core.controller._worker_activation is adapter
        runner = adapter._instruction_runner
        assert runner._registry_provider() is core.controller._registry
        assert isinstance(runner._route_provider(), MainPausedWorkerRoute)
        assert runner._route_provider().target_id == TARGET
        assert adapter._breakpoint_workspace._workspace is workspace
        materialization = adapter.materialization_snapshot()
        assert materialization.revision == 0
        assert materialization.registrations == ()
    finally:
        core.arbiter.close(timeout=3)


def test_post_bootstrap_worker_activation_rejects_outstanding_preparation() -> None:
    """Binding after a route snapshot could make its Worker guard stale."""

    core, _workspace = _core()
    try:
        assert core.controller.await_preparation_context() is not None

        with pytest.raises(ProtocolError, match="preparation"):
            _bind(core)
    finally:
        core.arbiter.close(timeout=3)


def test_post_bootstrap_worker_activation_rejects_live_main() -> None:
    """Binding while MAIN is suspended could switch activation mid-operation."""

    core, _workspace = _core()
    try:
        core.controller.submit_main("Результат = 1;").wait_settled(3)

        with pytest.raises(ProtocolError, match="MAIN"):
            _bind(core)
    finally:
        core.arbiter.close(timeout=3)


def test_main_idle_fence_is_available_only_before_main_admission() -> None:
    """Materialization must not receive a route fence once MAIN becomes live."""

    core, _workspace = _core()
    try:
        fence = core.controller.main_idle_fence()
        assert isinstance(fence, MainIdleTargetFence)
        assert fence.route == core.arbiter.current_route
        assert fence.target == TARGET

        core.controller.submit_main("Результат = 1;").wait_settled(3)
        assert core.controller.main_idle_fence() is None
    finally:
        core.arbiter.close(timeout=3)
