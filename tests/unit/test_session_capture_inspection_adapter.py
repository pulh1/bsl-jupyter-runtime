"""Map fenced CAPTURE inspection to the public Session/MCP wire shape."""

from contextlib import nullcontext
from threading import Event
from types import SimpleNamespace

import pytest

from onec_runtime.errors import ProtocolError, StaleCaptureError
from onec_runtime.execution.arbiter import RdbgArbiter, RouteToken, Settlement
from onec_runtime.execution.capture.public_inspection import CaptureInspectionBridge
from onec_runtime.rdbg.models import FrameVariable

from test_capture_stack_inventory_adapter import ready_scope
from test_main_idle_materialization import Session


class Controller:
    def __init__(self, scope):
        self.capture_scope = scope


def test_saved_stack_pages_keep_native_levels_and_redact_kernel_identity() -> None:
    from onec_runtime.execution.capture.session_inspection_adapter import (
        SessionCaptureInspectionAdapter,
    )

    scope = ready_scope()
    controller = Controller(scope)
    adapter = SessionCaptureInspectionAdapter(controller, CaptureInspectionBridge(controller))

    first = adapter.capture_stack(cursor=0, limit=2)
    last = adapter.capture_stack(cursor=2, limit=2)

    assert first == {
        "frames": (
            {
                "level": 0, "runtime_kernel": False,
                "module_type": "ConfigModule",
                "object_id": "00000000-0000-0000-0000-000000000002",
                "property_id": "00000000-0000-0000-0000-000000000003",
                "line": 10, "extension_name": "",
            },
            {
                "level": 2, "runtime_kernel": True,
                "module_type": None, "object_id": None, "property_id": None,
                "line": None, "extension_name": None,
            },
        ),
        "total": 3,
        "next_cursor": 2,
    }
    assert last["frames"][0]["level"] == 3
    assert last["next_cursor"] is None
    assert "private" not in str(first) + str(last)


def test_root_frame_page_exposes_saved_name_and_type_without_presentation() -> None:
    from onec_runtime.execution.capture.session_inspection_adapter import (
        SessionCaptureInspectionAdapter,
    )

    scope = ready_scope()
    scope.frame_variables = (
        FrameVariable("Local", "Строка", '"secret"'),
        FrameVariable("Rows", "Массив", "private rows", collection_size=4),
    )
    controller = Controller(scope)
    adapter = SessionCaptureInspectionAdapter(controller, CaptureInspectionBridge(controller))

    first = adapter.capture_frame(level=0, cursor=0, limit=1)
    second = adapter.capture_frame(level=0, cursor=1, limit=1)

    assert first["frame"]["level"] == 0
    assert first["variables"] == ({"name": "Local", "type_name": "Строка"},)
    assert first["total"] == 2
    assert first["next_cursor"] == 1
    assert second["variables"] == ({"name": "Rows", "type_name": "Массив"},)
    assert second["next_cursor"] is None
    assert "secret" not in str(first) + str(second)


def test_named_frame_variable_uses_ticket_metadata_without_raw_presentation() -> None:
    from onec_runtime.execution.capture.session_inspection_adapter import (
        SessionCaptureInspectionAdapter,
    )

    class Ticket:
        def wait_initiator(self, timeout=None):
            return FrameVariable("Rows", "Массив", "private presentation", collection_size=4)

        def detach_waiter(self):
            raise AssertionError("successful waiter must remain attached")

    class NamedController(Controller):
        def __init__(self, scope):
            super().__init__(scope)
            self.requests = []

        def submit_capture_variable(self, name, *, stack_level=0):
            self.requests.append((name, stack_level))
            return Ticket()

    scope = ready_scope()
    controller = NamedController(scope)
    adapter = SessionCaptureInspectionAdapter(controller, CaptureInspectionBridge(controller))

    result = adapter.capture_frame(level=3, cursor=0, limit=10, name="rows")

    assert result["frame"]["level"] == 3
    assert result["variables"] == ({
        "name": "Rows", "type_name": "Массив", "collection_size": 4,
    },)
    assert result["total"] == 1
    assert result["next_cursor"] is None
    assert controller.requests == [("rows", 3)]
    assert "private presentation" not in str(result)


def test_named_variable_local_timeout_detaches_waiter_but_keeps_ticket_owned() -> None:
    from onec_runtime.execution.capture.session_inspection_adapter import (
        SessionCaptureInspectionAdapter,
    )

    route = RouteToken("runtime", 1, 0, "capture")
    arbiter = RdbgArbiter(Session([]), route)
    blocker_started = Event()
    release = Event()
    blocker = arbiter.submit(
        route,
        lambda _port: (blocker_started.set(), release.wait(3), Settlement(None))[2],
    )
    arbiter.dispatch(blocker)

    class TicketController(Controller):
        def __init__(self, scope):
            super().__init__(scope)
            self.ticket = None

        def submit_capture_variable(self, name, *, stack_level=0):
            assert (name, stack_level) == ("Rows", 3)
            self.ticket = arbiter.submit(
                route,
                lambda _port: Settlement(FrameVariable("Rows", "Массив", "private")),
            )
            arbiter.dispatch(self.ticket)
            return self.ticket

    controller = TicketController(ready_scope())
    adapter = SessionCaptureInspectionAdapter(
        controller, CaptureInspectionBridge(controller), wait_handoff=nullcontext,
    )
    try:
        assert blocker_started.wait(1)
        with pytest.raises(TimeoutError, match="Local waiter interval"):
            adapter.capture_frame(level=3, cursor=0, limit=1, name="Rows", timeout_s=0.02)
        ticket = controller.ticket
        assert ticket is not None
        assert ticket.status().waiter_detached is True
        assert ticket.status().settled is False

        release.set()
        assert ticket.wait_settled(1).type_name == "Массив"
        assert ticket.status().settled is True
    finally:
        release.set()
        blocker.wait_settled(1)
        arbiter.close(timeout=3)


def test_saved_stack_rejects_released_stop_without_exposing_frames() -> None:
    from onec_runtime.execution.capture.session_inspection_adapter import (
        SessionCaptureInspectionAdapter,
    )

    scope = ready_scope()
    controller = Controller(scope)
    adapter = SessionCaptureInspectionAdapter(controller, CaptureInspectionBridge(controller))
    assert adapter.capture_stack(cursor=0, limit=1)["total"] == 3
    scope.mark_closed()

    with pytest.raises(StaleCaptureError):
        adapter.capture_stack(cursor=0, limit=1)


def test_named_variable_result_is_rejected_after_stop_release() -> None:
    from onec_runtime.execution.capture.session_inspection_adapter import (
        SessionCaptureInspectionAdapter,
    )

    scope = ready_scope()

    class Ticket:
        def wait_initiator(self, timeout=None):
            scope.mark_closed()
            return FrameVariable("Rows", "Массив", "private")

    class TicketController(Controller):
        def submit_capture_variable(self, name, *, stack_level=0):
            return Ticket()

    controller = TicketController(scope)
    adapter = SessionCaptureInspectionAdapter(controller, CaptureInspectionBridge(controller))

    with pytest.raises(StaleCaptureError):
        adapter.capture_frame(level=3, cursor=0, limit=1, name="Rows")


def test_non_root_unnamed_frame_uses_owned_typed_page_without_presentation() -> None:
    from onec_runtime.execution.capture.session_inspection_adapter import (
        SessionCaptureInspectionAdapter,
    )
    from onec_runtime.execution.capture.inspection import (
        TypedNativeVariable, TypedNativeVariablePage,
    )

    class Ticket:
        def wait_initiator(self, timeout=None):
            return TypedNativeVariablePage(
                (TypedNativeVariable("Rows", "Массив", 4),), 2, 1,
            )

    class TypedController(Controller):
        def __init__(self, scope):
            super().__init__(scope)
            self.requests = []

        def submit_capture_typed_variable_page(self, *, stack_level, start, stop):
            self.requests.append((stack_level, start, stop))
            return Ticket()

    scope = ready_scope()
    controller = TypedController(scope)
    adapter = SessionCaptureInspectionAdapter(controller, CaptureInspectionBridge(controller))

    result = adapter.capture_frame(level=3, cursor=0, limit=1)

    assert result["frame"]["level"] == 3
    assert result["variables"] == ({
        "name": "Rows", "type_name": "Массив", "collection_size": 4,
    },)
    assert result["total"] == 2
    assert result["next_cursor"] == 1
    assert controller.requests == [(3, 0, 1)]
    assert "presentation" not in str(result)


def test_named_frame_rejects_mismatched_ticket_result() -> None:
    from onec_runtime.execution.capture.session_inspection_adapter import (
        SessionCaptureInspectionAdapter,
    )

    class Ticket:
        def wait_initiator(self, timeout=None):
            return FrameVariable("Other", "Строка", "private")

    class TicketController(Controller):
        def submit_capture_variable(self, name, *, stack_level=0):
            return Ticket()

    controller = TicketController(ready_scope())
    adapter = SessionCaptureInspectionAdapter(controller, CaptureInspectionBridge(controller))

    with pytest.raises(ProtocolError, match="variable result"):
        adapter.capture_frame(level=3, cursor=0, limit=1, name="Rows")


def test_omitted_stack_timeout_uses_session_local_command_budget() -> None:
    from onec_runtime.execution.capture.session_inspection_adapter import (
        SessionCaptureInspectionAdapter,
    )

    scope = ready_scope()
    controller = Controller(scope)
    times = iter((0.0, 2.0))
    adapter = SessionCaptureInspectionAdapter(
        controller, CaptureInspectionBridge(controller),
        command_timeout_s=1.0, clock=lambda: next(times),
    )

    with pytest.raises(TimeoutError, match="Local CAPTURE inspection interval"):
        adapter.capture_stack(cursor=0, limit=1)


def test_named_wait_is_capped_by_session_local_command_budget() -> None:
    from onec_runtime.execution.capture.session_inspection_adapter import (
        SessionCaptureInspectionAdapter,
    )

    waits = []

    class Ticket:
        def wait_initiator(self, timeout=None):
            waits.append(timeout)
            return FrameVariable("Rows", "Массив", "private")

    class TicketController(Controller):
        def submit_capture_variable(self, name, *, stack_level=0):
            return Ticket()

    controller = TicketController(ready_scope())
    adapter = SessionCaptureInspectionAdapter(
        controller, CaptureInspectionBridge(controller),
        command_timeout_s=2.0, clock=lambda: 0.0,
    )

    result = adapter.capture_frame(
        level=3, cursor=0, limit=1, name="Rows", timeout_s=10.0,
    )

    assert result["variables"] == ({"name": "Rows", "type_name": "Массив"},)
    assert waits == [2.0]


def test_expired_budget_after_ticket_admission_detaches_only_the_waiter() -> None:
    from onec_runtime.execution.capture.session_inspection_adapter import (
        SessionCaptureInspectionAdapter,
    )

    class Ticket:
        detached = False

        def wait_initiator(self, timeout=None):
            raise AssertionError("the local deadline has already expired")

        def status(self):
            return SimpleNamespace(settled=False)

        def detach_waiter(self):
            self.detached = True

    ticket = Ticket()

    class TicketController(Controller):
        def submit_capture_variable(self, name, *, stack_level=0):
            return ticket

    controller = TicketController(ready_scope())
    times = iter((0.0, 2.0))
    adapter = SessionCaptureInspectionAdapter(
        controller, CaptureInspectionBridge(controller),
        command_timeout_s=1.0, clock=lambda: next(times),
    )

    with pytest.raises(TimeoutError, match="Local CAPTURE inspection interval"):
        adapter.capture_frame(level=3, cursor=0, limit=1, name="Rows")
    assert ticket.detached is True


def test_invalid_local_command_budget_is_rejected_at_binding() -> None:
    from onec_runtime.execution.capture.session_inspection_adapter import (
        SessionCaptureInspectionAdapter,
    )

    controller = Controller(ready_scope())
    with pytest.raises(ValueError, match="command_timeout_s"):
        SessionCaptureInspectionAdapter(
            controller, CaptureInspectionBridge(controller), command_timeout_s=None,
        )
