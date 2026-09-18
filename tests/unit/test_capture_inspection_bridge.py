"""Facade-facing CAPTURE stack and bounded variable inspection."""

import pytest

from onec_runtime.bsl.module_syntax import ModuleIdentity
from onec_runtime.capture_inspection import ResolvedFrameSource
from onec_runtime.capture_source import SourceVersionRef
from onec_runtime.capture_values import ValueNode, VariableRole
from onec_runtime.errors import CaptureSourceUnavailableError, StaleCaptureError
from onec_runtime.execution.capture.inspection import (
    TypedNativeVariable, TypedNativeVariablePage,
)
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

    def submit_capture_typed_variable_page(  # type: ignore[no-untyped-def]
        self, *, stack_level: int, start: int, stop: int,
        role: VariableRole = VariableRole.VARIABLES,
        parameter_names: tuple[str, ...] = (),
    ):
        assert role is VariableRole.VARIABLES and not parameter_names
        self.page_requests.append((stack_level, start, stop))
        return _Ticket(TypedNativeVariablePage(
            (TypedNativeVariable("Сумма", "Число", None),), 1, None,
        ))

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
    assert isinstance(page.items[0], ValueNode)
    assert controller.page_requests == [(0, 0, 1)]


def test_stack_business_values_are_bound_but_kernel_values_remain_hidden() -> None:
    controller = _Controller(ready_scope())
    inspection = CaptureInspectionBridge(controller).current()

    business = inspection.stack.native[0]
    assert [node.name for node in business.variables[:1].items] == ["Сумма"]
    kernel = inspection.stack.native[2]
    with pytest.raises(CaptureSourceUnavailableError):
        _ = kernel.variables
    assert controller.page_requests == [(0, 0, 1)]


def test_role_inspection_without_method_source_fails_before_remote_ticket() -> None:
    controller = _Controller(ready_scope())
    frame = CaptureInspectionBridge(controller).current().stack[0]

    with pytest.raises(CaptureSourceUnavailableError, match="method source"):
        _ = frame.locals[:1]
    with pytest.raises(CaptureSourceUnavailableError, match="method source"):
        _ = frame.parameters[:1]
    assert controller.page_requests == []


def test_inspection_handle_becomes_stale_after_capture_resume() -> None:
    scope = ready_scope()
    controller = _Controller(scope)
    inspection = CaptureInspectionBridge(controller).current()
    stack = inspection.stack
    frame = stack[0]
    controller.capture_scope = None
    scope.mark_closed()

    with pytest.raises(StaleCaptureError):
        _ = stack[:1]
    with pytest.raises(StaleCaptureError):
        inspection.frame(0)
    with pytest.raises(StaleCaptureError):
        _ = inspection.context.variables[:1]
    with pytest.raises(StaleCaptureError):
        _ = frame.variables[:1]
    with pytest.raises(StaleCaptureError):
        _ = frame.locals[:1]
    with pytest.raises(StaleCaptureError):
        _ = frame.parameters[:1]
    assert controller.page_requests == []


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


def test_frame_role_pages_and_exact_names_use_saved_source_and_native_level() -> None:
    from onec_runtime.bsl.module_syntax import ModuleIdentity
    from onec_runtime.capture_inspection import ResolvedFrameSource
    from onec_runtime.capture_source import SourceVersionRef

    scope = ready_scope()

    class RoleController(_Controller):
        def __init__(self):
            super().__init__(scope)
            self.roles = []
            self.exact = []

        def submit_capture_typed_variable_page(
            self, *, stack_level, start, stop,
            role=VariableRole.VARIABLES, parameter_names=(),
        ):
            self.roles.append((stack_level, start, stop, role, parameter_names))
            inventory = ("ЛокальныйСчетчик", "НачальноеЗначение")
            if role is VariableRole.PARAMETERS:
                names = ("НачальноеЗначение",)
            elif role is VariableRole.LOCALS:
                names = ("ЛокальныйСчетчик",)
            else:
                names = inventory
            selected = names[start:stop]
            return _Ticket(TypedNativeVariablePage(
                tuple(TypedNativeVariable(name, "Число", None) for name in selected),
                len(names), stop if selected and stop < len(names) else None,
            ))

        def submit_capture_variable(self, name, *, stack_level=0):
            self.exact.append((stack_level, name))
            return _Ticket(FrameVariable(name, "Число", "private", None))

    source = (
        "Процедура ВыполнитьШаг(НачальноеЗначение)\n"
        "ЛокальныйСчетчик = 1;\n"
        "КонецПроцедуры"
    )
    resolved = ResolvedFrameSource(
        "Common.ВыполнитьШаг", 2,
        ModuleIdentity("opaque", "worker", "common", "fixture", "Module"),
        SourceVersionRef.worker(
            artifact_id="role-fixture", generation=1, source_text=source,
        ),
    )
    controller = RoleController()
    inspection = CaptureInspectionBridge(
        controller,
        resolve_sources=lambda frames: tuple(
            resolved if frame.level == 0 else None for frame in frames
        ),
    ).current()
    frame = inspection.stack[0]

    assert [node.name for node in frame.locals[:1].items] == ["ЛокальныйСчетчик"]
    assert [node.name for node in frame.parameters[:1].items] == ["НачальноеЗначение"]
    assert isinstance(frame.locals["ЛокальныйСчетчик"], ValueNode)
    assert isinstance(frame.parameters["НачальноеЗначение"], ValueNode)
    assert controller.roles == [
        (0, 0, 1, VariableRole.LOCALS, ("НачальноеЗначение",)),
        (0, 0, 1, VariableRole.PARAMETERS, ("НачальноеЗначение",)),
    ]
    assert controller.exact == [
        (0, "ЛокальныйСчетчик"), (0, "НачальноеЗначение"),
    ]
