"""One public value entry point chooses the controller's exact stopped route."""

import json
from decimal import Decimal

import pytest

from onec_runtime.execution.arbiter import RdbgArbiter, RouteToken
from onec_runtime.execution.capture.manager_metadata import CaptureSelectedTableDescriptor
from onec_runtime.execution.capture.selected_table_materialization import CaptureSelectedTableTransferRequest
from onec_runtime.execution.capture.ticket_materialization import WorkerTransferCatalog
from onec_runtime.errors import ProtocolError
from onec_runtime.execution.main.idle_materialization import MainIdleTargetFence
from onec_runtime.execution.worker_activation import WorkerMaterializationSnapshot
from onec_runtime.table_materialization import ReferencePolicy
from onec_runtime.value_materialization import MaterializationOptions

from test_capture_stack_inventory_adapter import ready_scope
from test_capture_ticket_data_plane import Ticket
from test_main_idle_materialization import Session, TARGET


def test_value_router_uses_capture_ticket_for_current_scope() -> None:
    from onec_runtime.execution.value_materialization_router import (
        ValueMaterializationRouter,
    )

    payload = json.dumps({
        "version": 1, "root": {"t": "number", "v": "9"},
    }).encode("utf-8")
    scope = ready_scope()

    class Controller:
        capture_scope = scope
        plan = None

        def value_route_snapshot(self):
            return scope

        def main_idle_fence(self):
            return None

        def main_idle_fence_in_ticket(self):
            return None

        def submit_capture_materialization(self, plan, *, _before_first_effect=None):
            assert _before_first_effect is not None
            _before_first_effect()
            self.plan = plan
            return Ticket(payload)

    session = Session([])
    arbiter = RdbgArbiter(session, RouteToken("runtime", 1, 0, "main"))
    controller = Controller()
    router = ValueMaterializationRouter(
        controller, arbiter,
        runtime_generation=7, context_generation=4,
        worker_catalog_snapshot=lambda: WorkerMaterializationSnapshot(0, ()),
    )
    try:
        assert router.materialize_value(
            "Контекст.Сумма", MaterializationOptions(max_bytes=4096),
        ) == Decimal("9")
        assert "СериализоватьЗначение" in controller.plan.instruction
        assert session.calls == []
    finally:
        arbiter.close(timeout=3)


def test_value_router_uses_main_idle_ticket_after_capture_is_gone() -> None:
    from onec_runtime.execution.value_materialization_router import (
        ValueMaterializationRouter,
    )

    from base64 import b64encode
    from hashlib import sha256

    payload = b'{"version":1,"root":{"t":"number","v":"12.50"}}'
    encoded = b64encode(payload).decode("ascii")
    session = Session([
        f"R|7|4|{len(payload)}|{sha256(payload).hexdigest()}|{len(encoded)}",
        encoded, "Истина",
    ])
    route = RouteToken("runtime", 1, 0, "main")
    arbiter = RdbgArbiter(session, route)
    fence = MainIdleTargetFence(route, TARGET)

    class Controller:
        capture_scope = None

        def value_route_snapshot(self):
            return fence

        def main_idle_fence(self):
            return fence

        def main_idle_fence_in_ticket(self):
            return fence

    router = ValueMaterializationRouter(
        Controller(), arbiter,
        runtime_generation=7, context_generation=4,
        worker_catalog_snapshot=lambda: WorkerMaterializationSnapshot(0, ()),
    )
    try:
        assert router.materialize_value(
            "Контекст.Сумма", MaterializationOptions(max_bytes=4096),
        ) == Decimal("12.50")
        assert len([call for call in session.calls if call[0] == "start"]) == 3
    finally:
        arbiter.close(timeout=3)


def test_value_router_builds_bounded_head_on_the_capture_ticket() -> None:
    from onec_runtime.execution.value_materialization_router import (
        ValueMaterializationRouter,
    )

    from test_compact_table import compact_payload

    scope = ready_scope()

    class Controller:
        capture_scope = scope
        plan = None

        def value_route_snapshot(self):
            return scope

        def main_idle_fence(self):
            return None

        def main_idle_fence_in_ticket(self):
            return None

        def submit_capture_materialization(self, plan, *, _before_first_effect=None):
            assert _before_first_effect is not None
            _before_first_effect()
            self.plan = plan
            return Ticket(compact_payload())

    arbiter = RdbgArbiter(Session([]), RouteToken("runtime", 1, 0, "main"))
    controller = Controller()
    router = ValueMaterializationRouter(
        controller, arbiter,
        runtime_generation=7, context_generation=4,
        worker_catalog_snapshot=lambda: WorkerMaterializationSnapshot(0, ()),
    )
    try:
        frame = router.head_to_df(
            "Контекст.Таблица", 2,
            policy=ReferencePolicy(
                ref_columns={"Employee": "both", "Department": "uuid"},
            ),
        )
        assert frame["Name"].tolist() == ["Alice", "Bob"]
        assert "Для ИндексПроекции = 0 По Мин(Контекст.Таблица.Количество() - 1, 1)" in controller.plan.instruction
    finally:
        arbiter.close(timeout=3)


def test_dynamic_materialize_forwards_local_wait_budget_on_both_routes() -> None:
    from onec_runtime.execution.value_materialization_router import ValueMaterializationRouter

    scope = ready_scope()
    route = RouteToken("runtime", 1, 0, "main")
    fence = MainIdleTargetFence(route, TARGET)
    arbiter = RdbgArbiter(Session([]), route)

    class Controller:
        capture_scope = scope
        selected = scope

        def value_route_snapshot(self):
            return self.selected

        def main_idle_fence(self):
            return fence

        def main_idle_fence_in_ticket(self):
            return fence

    class Service:
        def __init__(self):
            self.calls = []

        def materialize(self, handle, options=None, *, table_policy=None, timeout_s=None):
            self.calls.append((handle, timeout_s))
            return "value"

    controller = Controller()
    router = ValueMaterializationRouter(
        controller, arbiter,
        runtime_generation=7, context_generation=4,
        worker_catalog_snapshot=lambda: WorkerMaterializationSnapshot(0, ()),
    )
    capture_service = Service()
    main_service = Service()
    router._capture_dynamic = lambda _scope: capture_service
    router._main = main_service
    try:
        with pytest.raises(ValueError, match="timeout_s"):
            router.materialize("Контекст.X", timeout_s=0)
        assert capture_service.calls == []
        assert router.materialize("Контекст.X", timeout_s=0.25) == "value"
        controller.selected = fence
        assert router.materialize("Контекст.X", timeout_s=0.5) == "value"
        assert capture_service.calls == [("Контекст.X", 0.25)]
        assert main_service.calls == [("Контекст.X", 0.5)]
    finally:
        arbiter.close(timeout=3)


def test_selected_table_router_passes_deferred_request_on_capture_only() -> None:
    from onec_runtime.execution.value_materialization_router import ValueMaterializationRouter

    scope = ready_scope()
    handle = "capture_table_" + "b" * 32
    descriptor = CaptureSelectedTableDescriptor(
        scope, "Query", ("Manager",), "Staff", 3, 5, (),
    )

    class Controller:
        capture_scope = scope
        selected = scope
        request = None

        def value_route_snapshot(self):
            return self.selected

        def main_idle_fence(self):
            return None

        def main_idle_fence_in_ticket(self):
            return None

        def require_capture_table_descriptor(self, requested, actual_scope):
            assert requested == handle and actual_scope is scope
            return descriptor

        def submit_capture_materialization(self, request, *, _before_first_effect=None):
            assert _before_first_effect is not None
            _before_first_effect()
            self.request = request
            return Ticket(b"private verified bytes")

    arbiter = RdbgArbiter(Session([]), RouteToken("runtime", 1, 0, "main"))
    controller = Controller()
    router = ValueMaterializationRouter(
        controller, arbiter,
        runtime_generation=7, context_generation=4,
        worker_catalog_snapshot=lambda: WorkerMaterializationSnapshot(3, ()),
    )
    try:
        router.validate_selected_table_handle(handle)
        assert router.transfer_selected_table(
            handle, ReferencePolicy(), max_rows=5, max_bytes=2048,
            catalog=WorkerTransferCatalog(3, ()), timeout_s=None,
            relative_offset=2, relative_limit=1,
        ) == b"private verified bytes"
        assert isinstance(controller.request, CaptureSelectedTableTransferRequest)
        assert controller.request.relative_offset == 2
        assert controller.request.relative_limit == 1
        controller.selected = None
        with pytest.raises(ProtocolError):
            router.transfer_selected_table(
                handle, ReferencePolicy(), max_rows=5, max_bytes=2048,
                catalog=WorkerTransferCatalog(3, ()), timeout_s=None,
            )
    finally:
        arbiter.close(timeout=3)


def test_selected_table_to_df_uses_same_deferred_route() -> None:
    from test_compact_table import compact_payload
    from onec_runtime.execution.value_materialization_router import ValueMaterializationRouter

    scope = ready_scope()
    handle = "capture_table_" + "c" * 32

    class Controller:
        capture_scope = scope
        request = None

        def value_route_snapshot(self):
            return scope

        def main_idle_fence(self):
            return None

        def main_idle_fence_in_ticket(self):
            return None

        def submit_capture_materialization(self, request, *, _before_first_effect=None):
            _before_first_effect()
            self.request = request
            return Ticket(compact_payload())

    arbiter = RdbgArbiter(Session([]), RouteToken("runtime", 1, 0, "main"))
    controller = Controller()
    router = ValueMaterializationRouter(
        controller, arbiter,
        runtime_generation=7, context_generation=4,
        worker_catalog_snapshot=lambda: WorkerMaterializationSnapshot(3, ()),
    )
    try:
        frame = router.to_df(
            handle, max_rows=5,
            policy=ReferencePolicy(
                ref_columns={"Employee": "both", "Department": "uuid"},
            ),
        )
        assert frame["Name"].tolist() == ["Alice", "Bob"]
        assert isinstance(controller.request, CaptureSelectedTableTransferRequest)
        assert controller.request.handle == handle
    finally:
        arbiter.close(timeout=3)
