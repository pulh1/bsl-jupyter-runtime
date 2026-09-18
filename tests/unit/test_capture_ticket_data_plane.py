"""Fenced CAPTURE inspection through controller tickets and saved stop data."""

from contextlib import contextmanager

import pytest

from onec_runtime.capture_inspection import DebugFrame, RuntimeFrameMarker
from onec_runtime.errors import ProtocolError, StaleCaptureError
from onec_runtime.rdbg.models import FrameVariable

from test_capture_stack_inventory_adapter import ready_scope
from test_capture_materialization_executor import transfer_plan


class Ticket:
    def __init__(self, result: object = None, error: BaseException | None = None):
        self.result = result
        self.error = error
        self.detached = False

    def wait_initiator(self):
        if self.error is not None:
            raise self.error
        return self.result

    def detach_waiter(self):
        self.detached = True


class TicketController:
    def __init__(self, scope):
        self.capture_scope = scope
        self.variable_ticket = Ticket(FrameVariable("Сумма", "Число", "42"))
        self.materialization_ticket = Ticket(b"private-payload")
        self.cleanup_ticket = Ticket(None)
        self.requested: list[tuple[object, ...]] = []

    def submit_capture_variable(self, name: str, *, stack_level: int = 0):
        self.requested.append(("variable", name, stack_level))
        return self.variable_ticket

    def submit_capture_materialization(self, plan, *, _before_first_effect=None):
        self.requested.append(("materialization", plan, _before_first_effect))
        return self.materialization_ticket

    def submit_capture_cleanup_retry(self, key: str):
        self.requested.append(("cleanup", key))
        return self.cleanup_ticket


def test_stack_and_frame_use_saved_scope_and_hide_runtime_coordinates():
    from onec_runtime.execution.capture.data_plane import CaptureTicketDataPlane

    scope = ready_scope()
    controller = TicketController(scope)
    data = CaptureTicketDataPlane(controller, scope)

    visible = data.stack[:2]
    native = data.stack.native[:4]
    first = data.frame(0)

    assert visible.total == 2
    assert any(isinstance(item, RuntimeFrameMarker) for item in visible.frames)
    assert [item.native_level for item in native.frames if isinstance(item, DebugFrame)] == [0, 2, 3]
    assert isinstance(first, DebugFrame)
    assert first.native_level == 0
    assert native.frames[1].runtime_kernel
    assert native.frames[1].physical is None
    assert "private" not in str(visible) + str(native)
    assert controller.requested == []


def test_saved_stack_handle_becomes_stale_after_capture_scope_changes():
    from onec_runtime.execution.capture.data_plane import CaptureTicketDataPlane

    scope = ready_scope()
    controller = TicketController(scope)
    data = CaptureTicketDataPlane(controller, scope)
    stack = data.stack
    assert stack[:1].frames
    controller.capture_scope = None

    with pytest.raises(StaleCaptureError):
        _ = stack[:1]
    with pytest.raises(StaleCaptureError):
        data.frame(0)


def test_invalidated_inspection_rejects_variable_before_ticket_submission():
    from onec_runtime.execution.capture.data_plane import CaptureTicketDataPlane

    scope = ready_scope()
    controller = TicketController(scope)
    data = CaptureTicketDataPlane(controller, scope)
    scope.invalidate_inspection()

    with pytest.raises(StaleCaptureError):
        data.read_private_variable("Сумма")
    assert controller.requested == []


def test_named_private_variable_and_materialization_wait_through_ticket_handoff():
    from onec_runtime.execution.capture.data_plane import CaptureTicketDataPlane

    scope = ready_scope()
    controller = TicketController(scope)
    events: list[str] = []

    @contextmanager
    def handoff():
        events.append("release")
        try:
            yield
        finally:
            events.append("reacquire")

    data = CaptureTicketDataPlane(controller, scope, wait_handoff=handoff)
    variable = data.read_private_variable("Сумма", stack_level=0)
    plan = transfer_plan("__onec_value_" + "b" * 32)
    payload = data.materialize_private_payload(plan)
    data.retry_temporary_cleanup("__onec_value_" + "a" * 32)

    assert variable == FrameVariable("Сумма", "Число", "42")
    assert payload == b"private-payload"
    assert controller.requested == [
        ("variable", "Сумма", 0),
        ("materialization", plan, None),
        ("cleanup", "__onec_value_" + "a" * 32),
    ]
    assert events == ["release", "reacquire"] * 3


def test_interrupted_private_read_detaches_waiter_without_dropping_scope():
    from onec_runtime.execution.capture.data_plane import CaptureTicketDataPlane

    scope = ready_scope()
    controller = TicketController(scope)
    ticket = Ticket(error=KeyboardInterrupt())
    controller.variable_ticket = ticket
    data = CaptureTicketDataPlane(controller, scope)

    with pytest.raises(KeyboardInterrupt):
        data.read_private_variable("Сумма")

    assert ticket.detached is True
    assert controller.capture_scope is scope


def test_private_result_type_is_checked_before_it_can_reach_public_binding():
    from onec_runtime.execution.capture.data_plane import CaptureTicketDataPlane

    scope = ready_scope()
    controller = TicketController(scope)
    controller.variable_ticket = Ticket("raw target presentation")
    data = CaptureTicketDataPlane(controller, scope)

    with pytest.raises(ProtocolError, match="variable result"):
        data.read_private_variable("Сумма")


def test_private_materialization_passes_catalog_guard_into_arbiter_plan():
    from onec_runtime.execution.capture.data_plane import CaptureTicketDataPlane

    scope = ready_scope()
    controller = TicketController(scope)
    guard = lambda: None
    data = CaptureTicketDataPlane(
        controller, scope, before_materialization=guard,
    )
    plan = transfer_plan("__onec_value_" + "b" * 32)

    assert data.materialize_private_payload(plan) == b"private-payload"
    assert controller.requested == [("materialization", plan, guard)]
