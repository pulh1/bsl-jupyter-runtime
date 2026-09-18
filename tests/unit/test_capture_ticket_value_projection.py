"""Bounded public frame-variable pages use the CAPTURE ticket owner."""

from __future__ import annotations

from onec_runtime.capture_inspection import DebugFrame
from onec_runtime.capture_values import (
    CaptureValuePolicy,
    UnavailableValueNode,
    ValueNode,
    ValueShape,
)
from onec_runtime.execution.capture.inspection import NativeVariablePage
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
        self.requests: list[tuple[int, int, int]] = []
        self.variable_requests: list[tuple[str, int]] = []

    def submit_capture_variable_page(self, *, stack_level: int, start: int, stop: int):  # type: ignore[no-untyped-def]
        self.requests.append((stack_level, start, stop))
        names = ("Сумма", "СложныйОбъект", "Третье")
        selected = names[start:stop]
        return Ticket(NativeVariablePage(
            selected, len(names), stop if stop < len(names) else None,
        ))

    def submit_capture_variable(self, name: str, *, stack_level: int = 0):
        self.variable_requests.append((name, stack_level))
        return Ticket(FrameVariable(name, "Число", "PRIVATE_PAYROLL_VALUE", 1))


def test_frame_variable_page_projects_only_names_from_owned_ticket() -> None:
    from onec_runtime.execution.capture.value_projection import (
        CaptureTicketValueProjection,
    )

    scope = ready_scope()
    controller = PageController(scope)
    projection = CaptureTicketValueProjection(
        controller, scope, policy=CaptureValuePolicy(max_items=2),
    )

    page = projection.frame(0).variables[:2]

    assert controller.requests == [(0, 0, 2)]
    assert page.total == 3 and page.next_cursor == 2
    assert [node.name for node in page.items] == ["Сумма", "СложныйОбъект"]
    assert all(isinstance(node, UnavailableValueNode) for node in page.items)
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
    assert controller.requests == [(0, 0, 1)]


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
    assert controller.requests == [(0, 0, 1)]
    assert controller.variable_requests == [("Сумма", 0)]
    assert "PRIVATE_PAYROLL_VALUE" not in repr(selected)
