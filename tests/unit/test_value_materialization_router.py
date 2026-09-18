"""One public value entry point chooses the controller's exact stopped route."""

import json
from decimal import Decimal

from onec_runtime.execution.arbiter import RdbgArbiter, RouteToken
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
