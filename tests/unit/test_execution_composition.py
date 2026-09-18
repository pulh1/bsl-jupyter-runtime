"""One post-bootstrap construction path for the MAIN/CAPTURE execution core."""

from threading import current_thread
from dataclasses import replace
from collections import deque

from onec_runtime.rdbg.models import StopEvent

from onec_runtime.bsl.source_maps import SourceUnitKind, SourceUnitRef, source_sha256
from onec_runtime.execution.composition import build_execution_core
from onec_runtime.execution.controller.controller import MainYieldKind
from onec_runtime.execution.snapshot_binding import RoutePreparationSnapshot
from onec_runtime.execution.breakpoint_routes import RouteBreakpointWorkspace
from onec_runtime.breakpoint_workspace import BreakpointWorkspaceController, WorkspaceSnapshot

from test_execution_controller_routes import BUSINESS, KERNEL, CompleteSession
from test_execution_route_sequence import CAPTURE_STOP, MAIN_STOP, TARGET


def test_composed_core_routes_main_capture_resume_on_one_rdbg_owner() -> None:
    session = CompleteSession(capture_count=1)
    owner = object()

    class Replies:
        def diagnostic_reply(self, diagnostic):
            raise AssertionError(f"Unexpected parser diagnostic: {diagnostic.message}")

        def unavailable_reply(self, unavailable):
            raise AssertionError(f"Unexpected route refusal: {unavailable.reason}")

    core = build_execution_core(
        session,
        KERNEL,
        runtime_generation=1,
        capture_locations=(BUSINESS,),
        snapshot_provider=lambda: RoutePreparationSnapshot(owner, 1, (), ()),
        reply_presenter=Replies(),
    )
    source = "Результат = 1;"
    source_unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL, "composition", 1, source_sha256(source)
    )
    try:
        first = core.pipeline.execute(source, source_unit)
        assert first.kind is MainYieldKind.CAPTURE
        assert core.controller.capture_scope is first.scope
        second = core.controller.submit_resume().wait_settled(3)
        assert second.kind is MainYieldKind.COMPLETED
        assert second.operation is first.operation
        assert {thread for _, thread in session.calls} == {core.arbiter._worker}
        assert core.arbiter._worker is not current_thread()
    finally:
        core.arbiter.close(timeout=3)


def test_composed_routes_preserve_worker_slots_in_main_and_capture_workspace() -> None:
    class ObservedSession(CompleteSession):
        def __init__(self):
            super().__init__()
            self.installed = []

        def set_breakpoints(self, locations, *, on_transport_dispatch):
            self.installed.append(tuple(locations))
            return super().set_breakpoints(
                locations, on_transport_dispatch=on_transport_dispatch,
            )

    session = ObservedSession()
    worker_slot = replace(BUSINESS, line=BUSINESS.line + 1)
    workspace = BreakpointWorkspaceController(
        session, WorkspaceSnapshot(0, KERNEL, (), (), (worker_slot,), False),
    )
    owner = object()

    class Replies:
        def diagnostic_reply(self, diagnostic):
            raise AssertionError(diagnostic.message)

        def unavailable_reply(self, unavailable):
            raise AssertionError(unavailable.reason)

    core = build_execution_core(
        session, KERNEL, runtime_generation=1, capture_locations=(BUSINESS,),
        snapshot_provider=lambda: RoutePreparationSnapshot(owner, 1, (), ()),
        reply_presenter=Replies(),
        breakpoint_routes=RouteBreakpointWorkspace(workspace),
    )
    try:
        first = core.controller.submit_main("Результат = 1;").wait_settled(3)
        assert first.kind is MainYieldKind.CAPTURE
        core.controller.submit_capture_cell("РезультатИнструкции = 2;").wait_settled(3)
        assert session.installed[0] == (KERNEL, BUSINESS, worker_slot)
        assert session.installed[1] == (KERNEL, worker_slot)
        assert session.installed[2] == (KERNEL, BUSINESS, worker_slot)
        assert {thread for _, thread in session.calls} == {core.arbiter._worker}
    finally:
        core.arbiter.close(timeout=3)


def test_resume_rearms_successor_capture_in_same_ticket_before_continue() -> None:
    successor = replace(BUSINESS, line=BUSINESS.line + 3)
    worker_slot = replace(BUSINESS, line=BUSINESS.line + 1)

    class SuccessorSession(CompleteSession):
        def __init__(self):
            super().__init__()
            self.installed = []
            self.stops = deque((
                StopEvent(
                    TARGET, BUSINESS, CAPTURE_STOP.reason,
                    stop_by_breakpoint=True, stack=CAPTURE_STOP.stack,
                    stack_frames=CAPTURE_STOP.stack_frames,
                ),
                StopEvent(
                    TARGET, successor, CAPTURE_STOP.reason,
                    stop_by_breakpoint=True, stack=CAPTURE_STOP.stack,
                    stack_frames=CAPTURE_STOP.stack_frames,
                ),
                StopEvent(TARGET, KERNEL, MAIN_STOP.reason, stop_by_breakpoint=True),
            ))

        def set_breakpoints(self, locations, *, on_transport_dispatch):
            self.installed.append(tuple(locations))
            return super().set_breakpoints(
                locations, on_transport_dispatch=on_transport_dispatch,
            )

    session = SuccessorSession()
    workspace = BreakpointWorkspaceController(
        session, WorkspaceSnapshot(0, KERNEL, (), (), (worker_slot,), False),
    )
    owner = object()

    class Replies:
        def diagnostic_reply(self, diagnostic):
            raise AssertionError(diagnostic.message)

        def unavailable_reply(self, unavailable):
            raise AssertionError(unavailable.reason)

    core = build_execution_core(
        session, KERNEL, runtime_generation=1, capture_locations=(BUSINESS,),
        snapshot_provider=lambda: RoutePreparationSnapshot(owner, 1, (), ()),
        reply_presenter=Replies(),
        breakpoint_routes=RouteBreakpointWorkspace(workspace),
    )
    try:
        first = core.controller.submit_main("Результат = 1;").wait_settled(3)
        assert first.kind is MainYieldKind.CAPTURE
        second = core.controller.submit_resume(
            successor_locations=(successor,),
        ).wait_settled(3)
        assert second.kind is MainYieldKind.CAPTURE
        assert second.operation is first.operation
        assert second.scope is not first.scope
        assert session.installed[:2] == [
            (KERNEL, BUSINESS, worker_slot),
            (KERNEL, successor, worker_slot),
        ]
        assert {thread for _, thread in session.calls} == {core.arbiter._worker}
    finally:
        core.arbiter.close(timeout=3)
