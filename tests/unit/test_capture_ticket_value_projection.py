"""Bounded public frame-variable pages use the CAPTURE ticket owner."""

from __future__ import annotations

from onec_runtime.capture_inspection import DebugFrame
from onec_runtime.capture_values import (
    CaptureValuePolicy,
    UnavailableValueNode,
    ValueNode,
    ValueShape,
    VariableRole,
)
from onec_runtime.execution.capture.inspection import (
    NativeVariablePage, TypedNativeVariable, TypedNativeVariablePage,
)
from onec_runtime.rdbg.models import FrameVariable

from test_capture_stack_inventory_adapter import ready_scope


class Ticket:
    def __init__(self, result: object) -> None:
        self.result = result

    def wait_initiator(self) -> object:
        return self.result

    def detach_waiter(self) -> None:
        pass


class PageController:
    def __init__(self, scope) -> None:  # type: ignore[no-untyped-def]
        self.capture_scope = scope
        self.requests: list[tuple[int, int, int, VariableRole, tuple[str, ...]]] = []
        self.typed_requests: list[tuple[int, int, int, VariableRole, tuple[str, ...]]] = []
        self.variable_requests: list[tuple[str, int]] = []

    def submit_capture_variable_page(  # type: ignore[no-untyped-def]
        self, *, stack_level: int, start: int, stop: int,
        role: VariableRole = VariableRole.VARIABLES,
        parameter_names: tuple[str, ...] = (),
    ):
        self.requests.append((stack_level, start, stop, role, parameter_names))
        names = ("Сумма", "СложныйОбъект", "Третье")
        if role is VariableRole.PARAMETERS:
            names = tuple(name for name in parameter_names if name in names)
        elif role is VariableRole.LOCALS:
            names = tuple(name for name in names if name not in parameter_names)
        selected = names[start:stop]
        return Ticket(NativeVariablePage(
            selected, len(names), stop if selected and stop < len(names) else None,
        ))

    def submit_capture_typed_variable_page(  # type: ignore[no-untyped-def]
        self, *, stack_level: int, start: int, stop: int,
        role: VariableRole = VariableRole.VARIABLES,
        parameter_names: tuple[str, ...] = (),
    ):
        self.typed_requests.append((stack_level, start, stop, role, parameter_names))
        names = ("Сумма", "СложныйОбъект", "Третье")
        if role is VariableRole.PARAMETERS:
            names = tuple(name for name in parameter_names if name in names)
        elif role is VariableRole.LOCALS:
            names = tuple(name for name in names if name not in parameter_names)
        selected = names[start:stop]
        return Ticket(TypedNativeVariablePage(
            tuple(TypedNativeVariable(name, "Число", 1) for name in selected),
            len(names), stop if selected and stop < len(names) else None,
        ))

    def submit_capture_variable(self, name: str, *, stack_level: int = 0):
        self.variable_requests.append((name, stack_level))
        return Ticket(FrameVariable(name, "Число", "PRIVATE_PAYROLL_VALUE", 1))


def test_frame_variable_page_projects_opaque_typed_nodes_from_owned_ticket() -> None:
    from onec_runtime.execution.capture.value_projection import (
        CaptureTicketValueProjection,
    )

    scope = ready_scope()
    controller = PageController(scope)
    projection = CaptureTicketValueProjection(
        controller, scope, policy=CaptureValuePolicy(max_items=2),
    )

    page = projection.frame(0).variables[:2]

    assert controller.typed_requests == [(0, 0, 2, VariableRole.VARIABLES, ())]
    assert page.total == 3 and page.next_cursor == 2
    assert [node.name for node in page.items] == ["Сумма", "СложныйОбъект"]
    assert all(isinstance(node, ValueNode) for node in page.items)
    assert all(node.type_name == "Число" and node.preview == "<captured value>" for node in page.items)
    assert "private" not in repr(page).casefold()


def test_projection_binds_existing_debug_frame_variables_contract() -> None:
    from onec_runtime.execution.capture.value_projection import (
        CaptureTicketValueProjection,
    )

    scope = ready_scope()
    controller = PageController(scope)
    projection = CaptureTicketValueProjection(controller, scope)
    frame = projection.bind_frame(DebugFrame(0, "Обработка.Метод", 1, 1))

    assert isinstance(frame, DebugFrame)
    assert [node.name for node in frame.variables[:1].items] == ["Сумма"]
    assert controller.typed_requests == [(0, 0, 1, VariableRole.VARIABLES, ())]


def test_context_page_and_selected_variable_use_separate_owned_tickets() -> None:
    from onec_runtime.execution.capture.value_projection import (
        CaptureTicketValueProjection,
    )

    scope = ready_scope()
    controller = PageController(scope)
    projection = CaptureTicketValueProjection(controller, scope)

    page = projection.context.variables[:1]
    selected = projection.context.variables["Сумма"]

    assert [node.name for node in page.items] == ["Сумма"]
    assert isinstance(selected, ValueNode)
    assert (selected.name, selected.type_name, selected.preview, selected.size) == (
        "Сумма", "Число", "<captured value>", 1,
    )
    assert selected.shape is ValueShape.UNDOCUMENTED
    assert selected.expandable is False
    assert controller.typed_requests == [(0, 0, 1, VariableRole.VARIABLES, ())]
    assert controller.variable_requests == [("Сумма", 0)]
    assert "PRIVATE_PAYROLL_VALUE" not in repr(selected)


def test_frame_role_pages_forward_source_parameters_to_owned_ticket() -> None:
    from onec_runtime.execution.capture.value_projection import (
        CaptureTicketValueProjection,
    )

    scope = ready_scope()
    controller = PageController(scope)
    projection = CaptureTicketValueProjection(
        controller, scope,
        resolve_parameters=lambda _root: ("Третье", "Сумма"),
    )

    parameters = projection.frame(0).parameters[:1]
    locals_page = projection.frame(0).locals[:1]

    assert [node.name for node in parameters.items] == ["Третье"]
    assert parameters.total == 2 and parameters.next_cursor == 1
    assert [node.name for node in locals_page.items] == ["СложныйОбъект"]
    assert locals_page.total == 1 and locals_page.next_cursor is None
    assert controller.typed_requests == [
        (0, 0, 1, VariableRole.PARAMETERS, ("Третье", "Сумма")),
        (0, 0, 1, VariableRole.LOCALS, ("Третье", "Сумма")),
    ]
    assert all(isinstance(node, ValueNode) for node in (*parameters.items, *locals_page.items))


def test_typed_frame_page_preserves_one_unavailable_item_without_hiding_neighbors() -> None:
    from onec_runtime.execution.capture.value_projection import CaptureTicketValueProjection

    class MixedController(PageController):
        def submit_capture_typed_variable_page(self, **kwargs):  # type: ignore[no-untyped-def]
            return Ticket(TypedNativeVariablePage((
                TypedNativeVariable("Сумма", "Число", 1),
                TypedNativeVariable("СложныйОбъект", None, None),
            ), 2, None))

    scope = ready_scope()
    page = CaptureTicketValueProjection(MixedController(scope), scope).frame(0).variables[:2]

    assert isinstance(page.items[0], ValueNode)
    assert isinstance(page.items[1], UnavailableValueNode)
    assert page.items[1].name == "СложныйОбъект"
