"""Public Worker breakpoint mutations share the post-bootstrap RDBG owner."""

from __future__ import annotations

from contextlib import contextmanager
from threading import Event, RLock, Thread, get_ident
from uuid import UUID

import pytest

from onec_runtime.breakpoint_workspace import (
    BreakpointWorkspaceController,
    BreakpointWorkspaceOutcomeUnknown,
    WorkspaceSnapshot,
)
from onec_runtime.bsl.source_maps import SourceUnitKind, SourceUnitRef
from onec_runtime.errors import ProtocolError
from onec_runtime.execution.arbiter import (
    RdbgArbiter, RouteToken, Settlement, TargetTerminated,
)
from onec_runtime.execution.termination import FileTerminationConfirmed
from onec_runtime.execution.worker_breakpoint_service import WorkerBreakpointService
from onec_runtime.rdbg.models import DebugTarget, ModuleLocation, TargetId
from onec_runtime.worker_breakpoints import (
    WorkerBreakpointCoordinator,
    WorkerBreakpointResolution,
)

from test_worker_breakpoints import _debug_view


SERVICE = ModuleLocation("ExtensionModule", "", UUID(int=1), UUID(int=2), 1)
CAPTURE = ModuleLocation("ExtensionModule", "", UUID(int=1), UUID(int=3), 7)
USER = ModuleLocation("ConfigurationModule", "", UUID(int=4), UUID(int=5), 9)
TARGET = TargetId(UUID(int=6), "test")


class _Session:
    def __init__(self) -> None:
        self.target = DebugTarget(TARGET, "server", "stopped")
        self.calls: list[tuple[ModuleLocation, ...]] = []
        self.threads: list[int] = []
        self.block = False
        self.entered = Event()
        self.release = Event()
        self.failure: BaseException | None = None

    def set_breakpoints(self, locations, *, on_transport_dispatch):
        on_transport_dispatch()
        self.calls.append(locations)
        self.threads.append(get_ident())
        if self.block:
            self.entered.set()
            assert self.release.wait(5)
        if self.failure is not None:
            raise self.failure


class _ForbiddenFallback:
    def set_breakpoints(self, _locations):
        pytest.fail("breakpoints escaped the arbiter port")


@pytest.fixture
def service():
    session = _Session()
    arbiter = RdbgArbiter(session, RouteToken("breakpoints", 1, 0, "main"))
    catalog = WorkerBreakpointCoordinator(session_id=UUID(int=41))
    workspace = BreakpointWorkspaceController(
        _ForbiddenFallback(), WorkspaceSnapshot(0, SERVICE, (CAPTURE,), (USER,), (), False),
    )
    boundary = {"ready": True}

    def require_boundary():
        if not boundary["ready"]:
            raise ProtocolError("Worker breakpoint mutation requires a stable boundary")

    api = WorkerBreakpointService(
        arbiter, catalog, workspace, require_mutation_boundary=require_boundary,
    )
    yield api, catalog, workspace, session, arbiter, boundary
    session.release.set()
    active = arbiter.active_ticket
    if active is not None and active.status().phase == "unknown":
        arbiter.retire_terminated_target(
            active, arbiter.current_route, FileTerminationConfirmed(TARGET, 123, -15),
        )
        with pytest.raises(TargetTerminated):
            active.wait_settled(5)
    arbiter.close(timeout=5)


def test_add_installs_shared_workspace_before_publishing_status(service, tmp_path) -> None:
    api, catalog, workspace, session, arbiter, _boundary = service
    view = _debug_view(tmp_path)
    module = view.modules[0]
    catalog.set_views((view,))

    status = api.add_worker_breakpoint(
        module.source_unit, module.canonical_module, 2,
    )

    assert status.resolution is WorkerBreakpointResolution.RESOLVED
    assert status.installed_binding_count == 1
    assert api.worker_breakpoint_status(status.breakpoint.id) == status
    assert api.list_worker_breakpoints() == (status,)
    assert workspace.confirmed_snapshot.effective_locations == (
        SERVICE, CAPTURE, USER, module.registration.module_location(2),
    )
    assert session.calls == [workspace.confirmed_snapshot.effective_locations]
    assert session.threads == [arbiter._worker.ident]


def test_status_stays_readable_while_toggle_waits_for_remote_install(
    service, tmp_path,
) -> None:
    api, catalog, workspace, session, _arbiter, _boundary = service
    view = _debug_view(tmp_path)
    module = view.modules[0]
    catalog.set_views((view,))
    added = api.add_worker_breakpoint(module.source_unit, module.canonical_module, 2)
    session.block = True
    outcome: list[object] = []

    def disable():
        try:
            outcome.append(api.set_worker_breakpoint_enabled(added.breakpoint.id, False))
        except BaseException as error:
            outcome.append(error)

    worker = Thread(target=disable)
    worker.start()
    try:
        assert session.entered.wait(5)
        assert api.worker_breakpoint_status(added.breakpoint.id).enabled is True
        assert api.list_worker_breakpoints()[0].installed_binding_count == 1
    finally:
        session.release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert len(outcome) == 1
    assert not isinstance(outcome[0], BaseException)
    assert outcome[0].enabled is False
    assert workspace.confirmed_snapshot.effective_locations == (SERVICE, CAPTURE, USER)

    api.remove_worker_breakpoint(added.breakpoint.id)
    assert api.list_worker_breakpoints() == ()


def test_session_caller_lock_is_released_during_remote_install(
    tmp_path,
) -> None:
    view = _debug_view(tmp_path)
    module = view.modules[0]
    catalog = WorkerBreakpointCoordinator(session_id=UUID(int=43))
    catalog.set_views((view,))
    session = _Session()
    session.block = True
    arbiter = RdbgArbiter(session, RouteToken("caller-lock", 1, 0, "main"))
    workspace = BreakpointWorkspaceController(
        _ForbiddenFallback(), WorkspaceSnapshot(0, SERVICE, (), (), (), False),
    )
    caller_lock = RLock()

    @contextmanager
    def handoff():
        caller_lock.release()
        try:
            yield
        finally:
            caller_lock.acquire()

    api = WorkerBreakpointService(
        arbiter, catalog, workspace,
        require_mutation_boundary=lambda: None,
        wait_handoff=handoff,
    )
    outcome: list[object] = []

    def add():
        with caller_lock:
            try:
                outcome.append(
                    api.add_worker_breakpoint(
                        module.source_unit, module.canonical_module, 2,
                    )
                )
            except BaseException as error:
                outcome.append(error)

    worker = Thread(target=add)
    worker.start()
    try:
        assert session.entered.wait(5)
        assert caller_lock.acquire(timeout=1)
        try:
            assert api.list_worker_breakpoints() == ()
        finally:
            caller_lock.release()
    finally:
        session.release.set()
        worker.join(timeout=5)
        arbiter.close(timeout=5)
    assert not worker.is_alive()
    assert len(outcome) == 1
    assert not isinstance(outcome[0], BaseException)
    assert outcome[0].installed_binding_count == 1


def test_unknown_install_keeps_catalog_unpublished_and_owner_fenced(
    service, tmp_path,
) -> None:
    api, catalog, workspace, session, arbiter, _boundary = service
    view = _debug_view(tmp_path)
    module = view.modules[0]
    catalog.set_views((view,))
    session.failure = TimeoutError("private transport detail")

    with pytest.raises(BreakpointWorkspaceOutcomeUnknown, match="outcome is unknown"):
        api.add_worker_breakpoint(module.source_unit, module.canonical_module, 2)

    assert api.list_worker_breakpoints() == ()
    assert workspace.confirmed_snapshot.worker_slots == ()
    with pytest.raises(BreakpointWorkspaceOutcomeUnknown):
        workspace.require_confirmed()
    assert arbiter.active_ticket is not None
    assert arbiter.active_ticket.status().phase == "unknown"


def test_pending_breakpoint_changes_catalog_without_rdbg_write(service) -> None:
    api, _catalog, workspace, session, _arbiter, _boundary = service
    unit = SourceUnitRef(SourceUnitKind.MODULE, "WorkerA", 1, "a" * 64)

    added = api.add_worker_breakpoint(unit, "WorkerA", 2)
    assert added.resolution is WorkerBreakpointResolution.PENDING
    assert added.installed_binding_count == 0
    assert session.calls == []
    assert workspace.confirmed_snapshot.version == 0

    api.remove_worker_breakpoint(added.breakpoint.id)
    assert api.list_worker_breakpoints() == ()
    assert session.calls == []


def test_mutation_boundary_rejects_before_catalog_or_remote_effect(service) -> None:
    api, _catalog, _workspace, session, _arbiter, boundary = service
    unit = SourceUnitRef(SourceUnitKind.MODULE, "WorkerA", 1, "a" * 64)
    boundary["ready"] = False

    with pytest.raises(ProtocolError, match="stable boundary"):
        api.add_worker_breakpoint(unit, "WorkerA", 2)

    assert api.list_worker_breakpoints() == ()
    assert session.calls == []


def test_queued_mutation_rechecks_boundary_before_any_effect(service) -> None:
    api, _catalog, _workspace, session, arbiter, boundary = service
    unit = SourceUnitRef(SourceUnitKind.MODULE, "WorkerA", 1, "a" * 64)
    entered = Event()
    release = Event()

    def blocker(_port):
        entered.set()
        assert release.wait(5)
        return Settlement(None)

    blocking = arbiter.submit(arbiter.current_route, blocker)
    arbiter.dispatch(blocking)
    assert entered.wait(5)
    submitted = Event()
    original_submit = arbiter.submit

    def tracked_submit(route, plan):
        ticket = original_submit(route, plan)
        submitted.set()
        return ticket

    arbiter.submit = tracked_submit
    outcome: list[object] = []

    def add():
        try:
            outcome.append(api.add_worker_breakpoint(unit, "WorkerA", 2))
        except BaseException as error:
            outcome.append(error)

    worker = Thread(target=add)
    worker.start()
    try:
        assert submitted.wait(5)
        boundary["ready"] = False
    finally:
        release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert len(outcome) == 1
    assert isinstance(outcome[0], ProtocolError)
    assert api.list_worker_breakpoints() == ()
    assert session.calls == []


def test_add_returns_its_committed_status_when_later_mutation_runs(
    tmp_path,
) -> None:
    class _StatusGateCoordinator(WorkerBreakpointCoordinator):
        def __init__(self):
            super().__init__(session_id=UUID(int=42))
            self.status_entered = Event()
            self.status_release = Event()

        def status(self, breakpoint_id):
            self.status_entered.set()
            assert self.status_release.wait(5)
            return super().status(breakpoint_id)

    view = _debug_view(tmp_path)
    module = view.modules[0]
    catalog = _StatusGateCoordinator()
    catalog.set_views((view,))
    session = _Session()
    arbiter = RdbgArbiter(session, RouteToken("status-race", 1, 0, "main"))
    workspace = BreakpointWorkspaceController(
        _ForbiddenFallback(), WorkspaceSnapshot(0, SERVICE, (), (), (), False),
    )
    api = WorkerBreakpointService(
        arbiter, catalog, workspace, require_mutation_boundary=lambda: None,
    )
    outcome: list[object] = []

    def add():
        try:
            outcome.append(
                api.add_worker_breakpoint(module.source_unit, module.canonical_module, 2)
            )
        except BaseException as error:
            outcome.append(error)

    worker = Thread(target=add)
    worker.start()
    remove_finished = Event()

    def remove(breakpoint_id):
        try:
            api.remove_worker_breakpoint(breakpoint_id)
        finally:
            remove_finished.set()

    remover = None
    try:
        assert catalog.status_entered.wait(5)
        breakpoint_id = catalog.snapshot().statuses[0].breakpoint.id
        remover = Thread(target=remove, args=(breakpoint_id,))
        remover.start()
        remove_finished.wait(0.2)
    finally:
        catalog.status_release.set()
        worker.join(timeout=5)
        if remover is not None:
            remover.join(timeout=5)
        arbiter.close(timeout=5)
    assert len(outcome) == 1
    assert not isinstance(outcome[0], BaseException)
    assert outcome[0].breakpoint.id == breakpoint_id
    assert outcome[0].installed_binding_count == 1
