"""MAIN, CAPTURE and Worker share one full-replacement breakpoint owner."""

from uuid import UUID

import pytest

from onec_runtime.breakpoint_workspace import (
    BreakpointWorkspaceController, BreakpointWorkspaceOutcomeUnknown,
    WorkspaceSnapshot,
)
from onec_runtime.errors import ProtocolError
from onec_runtime.execution.breakpoint_routes import RouteBreakpointWorkspace
from onec_runtime.rdbg.models import ModuleLocation
from onec_runtime.stop_routing import BreakpointRegistry


SERVICE = ModuleLocation("ExtensionModule", "", UUID(int=1), UUID(int=2), 1)
CAPTURE = ModuleLocation("ExtensionModule", "", UUID(int=1), UUID(int=3), 7)
SUCCESSOR = ModuleLocation("ExtensionModule", "", UUID(int=1), UUID(int=4), 11)
USER = ModuleLocation("ConfigurationModule", "", UUID(int=4), UUID(int=5), 9)
WORKER = ModuleLocation(
    "ExtMDModule",
    "e1cib/tempstorage/00000000-0000-0000-0000-000000000001?seanceId=fake",
    UUID(int=6), UUID(int=7), 2,
)


class Port:
    def __init__(self) -> None:
        self.calls: list[tuple[ModuleLocation, ...]] = []
        self.failure: BaseException | None = None

    def set_breakpoints(self, locations: tuple[ModuleLocation, ...]) -> None:
        self.calls.append(locations)
        if self.failure is not None:
            raise self.failure


def owner() -> tuple[RouteBreakpointWorkspace, BreakpointWorkspaceController, Port]:
    fallback = Port()
    workspace = BreakpointWorkspaceController(
        fallback, WorkspaceSnapshot(0, SERVICE, (), (), (), False)
    )
    return RouteBreakpointWorkspace(workspace), workspace, fallback


def test_main_full_install_preserves_worker_slots_and_uses_admitted_port() -> None:
    routes, workspace, fallback = owner()
    admitted = Port()
    worker = workspace.prepare(
        captures=(), ordinary_users=(), worker_slots=(WORKER,), shielded=False,
    )
    workspace.install(worker, port=admitted)
    admitted.calls.clear()

    receipt = routes.install_main(
        BreakpointRegistry(SERVICE, (CAPTURE,), (USER,)), port=admitted,
    )

    assert routes.worker_owner is workspace
    assert workspace.confirmed_snapshot.effective_locations == (
        SERVICE, CAPTURE, USER, WORKER,
    )
    assert admitted.calls == [(SERVICE, CAPTURE, USER, WORKER)]
    assert fallback.calls == []
    assert receipt.requested_digest == workspace.confirmed_snapshot.digest


def test_capture_shield_and_restore_keep_user_and_worker_breakpoints() -> None:
    routes, workspace, fallback = owner()
    admitted = Port()
    routes.install_main(BreakpointRegistry(SERVICE, (CAPTURE,), (USER,)), port=admitted)
    worker = workspace.prepare(
        captures=(CAPTURE,), ordinary_users=(USER,),
        worker_slots=(WORKER,), shielded=False,
    )
    workspace.install(worker, port=admitted)
    admitted.calls.clear()

    routes.shield_capture(port=admitted)
    assert workspace.confirmed_snapshot.shielded
    routes.restore_capture(port=admitted)

    assert admitted.calls == [
        (SERVICE, USER, WORKER),
        (SERVICE, CAPTURE, USER, WORKER),
    ]
    assert fallback.calls == []


def test_unknown_breakpoint_write_quarantines_all_routes() -> None:
    routes, workspace, fallback = owner()
    admitted = Port()
    admitted.failure = TimeoutError("unknown transport outcome")
    with pytest.raises(BreakpointWorkspaceOutcomeUnknown):
        routes.install_main(BreakpointRegistry(SERVICE, (CAPTURE,)), port=admitted)
    with pytest.raises(BreakpointWorkspaceOutcomeUnknown):
        routes.restore_capture(port=admitted)
    assert fallback.calls == []
    assert workspace.confirmed_snapshot.captures == ()


def test_idle_capture_plan_is_local_and_requires_controller_admission_to_apply() -> None:
    routes, workspace, fallback = owner()
    original = BreakpointRegistry(SERVICE)
    planned = routes.plan_idle_captures(original, (CAPTURE,))
    assert original.captures == ()
    assert planned.captures == (CAPTURE,)
    assert workspace.confirmed_snapshot.captures == ()
    assert fallback.calls == []


def test_captured_successor_rearm_replaces_full_workspace_on_admitted_port() -> None:
    routes, workspace, fallback = owner()
    admitted = Port()
    routes.install_main(BreakpointRegistry(SERVICE, (CAPTURE,)), port=admitted)
    admitted.calls.clear()
    routes.rearm_captured_successor(
        BreakpointRegistry(SERVICE, (CAPTURE,)), (SUCCESSOR,), port=admitted,
    )
    assert admitted.calls == [(SERVICE, SUCCESSOR)]
    assert workspace.confirmed_snapshot.captures == (SUCCESSOR,)
    assert fallback.calls == []


def test_captured_successor_rearm_refuses_stale_registry() -> None:
    routes, workspace, fallback = owner()
    admitted = Port()
    routes.install_main(BreakpointRegistry(SERVICE, (CAPTURE,)), port=admitted)
    admitted.calls.clear()
    with pytest.raises(ProtocolError, match="stale"):
        routes.rearm_captured_successor(
            BreakpointRegistry(SERVICE, (SUCCESSOR,)), (SUCCESSOR,), port=admitted,
        )
    assert admitted.calls == []
    assert workspace.confirmed_snapshot.captures == (CAPTURE,)
    assert fallback.calls == []
