"""Dynamic value and bounded table projection through one CAPTURE ticket."""

import json
from threading import Event

import pytest

from onec_runtime.errors import BslExecutionError, ProtocolError
from onec_runtime.execution.arbiter import RdbgArbiter, RouteToken, Settlement
from onec_runtime.execution.capture.ticket_materialization import WorkerTransferCatalog
from onec_runtime.execution.dynamic_value_materialization import (
    CaptureDynamicValueMaterialization,
)
from onec_runtime.table_materialization import ReferencePolicy
from onec_runtime.value_materialization import MaterializationOptions

from test_capture_stack_inventory_adapter import ready_scope
from test_capture_ticket_data_plane import Ticket, TicketController
from test_compact_table import compact_payload
from test_main_idle_materialization import Session


def _service(controller, scope, catalog):
    return CaptureDynamicValueMaterialization(
        controller,
        scope,
        runtime_generation=7,
        context_generation=4,
        worker_catalog_snapshot=lambda: catalog[0],
    )


def test_dynamic_materialize_selects_the_value_payload_decoder() -> None:
    scope = ready_scope()
    controller = TicketController(scope)
    controller.materialization_ticket = Ticket(json.dumps({
        "version": 1, "root": {"t": "number", "v": "9"},
    }).encode())
    catalog = [WorkerTransferCatalog(1, ())]

    assert _service(controller, scope, catalog).materialize(
        "Контекст.Сумма", MaterializationOptions(max_bytes=4096),
    ) == 9
    assert "СериализоватьКомпактнуюТаблицу" in controller.requested[0][1].instruction
    assert "СериализоватьЗначение" in controller.requested[0][1].instruction


def test_head_to_df_transfers_only_selected_table_rows() -> None:
    scope = ready_scope()
    controller = TicketController(scope)
    controller.materialization_ticket = Ticket(compact_payload())
    catalog = [WorkerTransferCatalog(1, ())]

    frame = _service(controller, scope, catalog).head_to_df(
        "Контекст.Таблица",
        2,
        policy=ReferencePolicy(
            ref_columns={"Employee": "both", "Department": "uuid"},
        ),
    )

    assert frame["Name"].tolist() == ["Alice", "Bob"]
    instruction = controller.requested[0][1].instruction
    assert "Для ИндексПроекции = 0 По Мин(Контекст.Таблица.Количество() - 1, 1)" in instruction
    assert "СериализоватьКомпактнуюТаблицу" in instruction
    assert "СериализоватьЗначение" not in instruction


@pytest.mark.parametrize("count", [0, 10_001])
def test_head_to_df_enforces_the_public_row_bound(count: int) -> None:
    scope = ready_scope()
    controller = TicketController(scope)
    catalog = [WorkerTransferCatalog(1, ())]

    with pytest.raises(ValueError, match="between 1 and 10000"):
        _service(controller, scope, catalog).head_to_df("Контекст.Таблица", count)
    assert controller.requested == []


def test_capture_bsl_failure_keeps_the_ready_scope() -> None:
    scope = ready_scope()
    controller = TicketController(scope)
    controller.materialization_ticket = Ticket(error=BslExecutionError("planned"))
    catalog = [WorkerTransferCatalog(1, ())]

    with pytest.raises(BslExecutionError, match="planned"):
        _service(controller, scope, catalog).materialize("Контекст.Сумма")
    assert controller.capture_scope is scope


def test_dynamic_value_rejects_private_worker_root_before_ticket() -> None:
    scope = ready_scope()
    controller = TicketController(scope)
    service = _service(controller, scope, [WorkerTransferCatalog(1, ())])

    with pytest.raises(ProtocolError, match="Worker generation"):
        service.materialize("Контекст.RuntimeWorkerActiveGeneration")
    with pytest.raises(ProtocolError, match="Worker generation"):
        service.head_to_df("Контекст.RuntimeWorkerPinnedOperationGeneration", 2)
    assert controller.requested == []


def test_worker_catalog_change_rejects_before_capture_dispatch() -> None:
    scope = ready_scope()
    controller = TicketController(scope)
    catalog = [WorkerTransferCatalog(1, ())]

    def submit(plan, *, _before_first_effect=None):
        controller.requested.append(("materialization", plan, _before_first_effect))
        catalog[0] = WorkerTransferCatalog(2, ())
        assert _before_first_effect is not None
        _before_first_effect()
        raise AssertionError("catalog preflight must reject before dispatch")

    controller.submit_capture_materialization = submit
    with pytest.raises(ProtocolError, match="Worker catalog changed"):
        _service(controller, scope, catalog).materialize("Контекст.Сумма")
    assert controller.capture_scope is scope


def test_capture_materialize_timeout_detaches_only_local_waiter() -> None:
    scope = ready_scope()
    route = RouteToken("runtime", 1, 0, "capture")
    arbiter = RdbgArbiter(Session([]), route)
    release = Event()
    entered = Event()
    payload = b'{"version":1,"root":{"t":"number","v":"9"}}'

    class Controller(TicketController):
        def submit_capture_materialization(self, plan, *, _before_first_effect=None):
            def operation(_port):
                if _before_first_effect is not None:
                    _before_first_effect()
                entered.set()
                release.wait(3)
                return Settlement(payload)

            ticket = arbiter.submit(route, operation)
            self.materialization_ticket = ticket
            arbiter.dispatch(ticket)
            return ticket

    controller = Controller(scope)
    service = _service(controller, scope, [WorkerTransferCatalog(1, ())])
    try:
        with pytest.raises(TimeoutError, match="Local waiter interval"):
            service.materialize("Контекст.Сумма", timeout_s=0.02)
        assert entered.is_set()
        ticket = controller.materialization_ticket
        assert ticket.status().waiter_detached is True
        assert ticket.status().settled is False
        assert controller.capture_scope is scope

        release.set()
        assert ticket.wait_settled(1) == payload
        assert ticket.status().settled is True
        assert service.materialize("Контекст.Сумма", timeout_s=1) == 9
        assert controller.capture_scope is scope
    finally:
        release.set()
        arbiter.close(timeout=3)
