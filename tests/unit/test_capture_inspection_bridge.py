"""Facade-facing CAPTURE stack and bounded variable inspection."""

import pytest

from onec_runtime.bsl.module_syntax import ModuleIdentity
from onec_runtime.capture_inspection import ResolvedFrameSource
from onec_runtime.capture_source import SourceVersionRef
from onec_runtime.capture_values import UnavailableValueNode
from onec_runtime.errors import StaleCaptureError
from onec_runtime.execution.capture.inspection import NativeVariablePage
from onec_runtime.execution.capture.public_inspection import CaptureInspectionBridge
from onec_runtime.rdbg.models import FrameVariable

from test_capture_stack_inventory_adapter import ready_scope


class _Ticket:
    def __init__(self, value: object) -> None:
        self.value = value

    def wait_initiator(self) -> object:
        return self.value

    def detach_waiter(self) -> None:
        pass


class _Controller:
    def __init__(self, scope) -> None:  # type: ignore[no-untyped-def]
        self.capture_scope = scope
        self.failed_capture_cell = True
        self.page_requests: list[tuple[int, int, int]] = []

    def submit_capture_variable_page(self, *, stack_level: int, start: int, stop: int):  # type: ignore[no-untyped-def]
        self.page_requests.append((stack_level, start, stop))
        return _Ticket(NativeVariablePage(("Сумма",), 1, None))

    def submit_capture_variable(self, name: str, *, stack_level: int = 0):
        return _Ticket(FrameVariable(name, "Число", "private", None))


def test_stack_and_frame_variables_remain_available_after_failed_capture_cell() -> None:
    scope = ready_scope()
    controller = _Controller(scope)
    inspection = CaptureInspectionBridge(controller).current()

    assert inspection.stack[:2].total == 2
    frame = inspection.frame(0)
    page = frame.variables[:1]

    assert controller.failed_capture_cell is True
    assert [node.name for node in page.items] == ["Сумма"]
    assert isinstance(page.items[0], UnavailableValueNode)
    assert controller.page_requests == [(0, 0, 1)]


def test_inspection_handle_becomes_stale_after_capture_resume() -> None:
    scope = ready_scope()
    controller = _Controller(scope)
    inspection = CaptureInspectionBridge(controller).current()
    stack = inspection.stack
    controller.capture_scope = None
    scope.mark_closed()

    with pytest.raises(StaleCaptureError):
        _ = stack[:1]
    with pytest.raises(StaleCaptureError):
        inspection.frame(0)
    with pytest.raises(StaleCaptureError):
        _ = inspection.context.variables[:1]


def test_bridge_passes_session_source_resolution_to_saved_stack() -> None:
    scope = ready_scope()
    controller = _Controller(scope)
    source = "Procedure RunFixture()\nEndProcedure"
    resolved = ResolvedFrameSource(
        "Common.RunFixture", 1,
        ModuleIdentity("opaque", "worker", "common", "fixture", "Module"),
        SourceVersionRef.worker(
            artifact_id="bridge-fixture", generation=1, source_text=source,
        ),
    )
    calls: list[tuple[int, ...]] = []

    def resolve_sources(frames):  # type: ignore[no-untyped-def]
        calls.append(tuple(frame.level for frame in frames))
        return tuple(resolved if frame.level == 0 else None for frame in frames)

    inspection = CaptureInspectionBridge(
        controller, resolve_sources=resolve_sources,
    ).current()
    assert inspection.stack[:1].frames[0].source == "Common.RunFixture"
    assert calls == [(0,)]
