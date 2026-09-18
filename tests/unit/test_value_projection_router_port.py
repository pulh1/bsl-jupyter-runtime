"""Projection plans use the same fenced MAIN/CAPTURE value-route owner."""

from __future__ import annotations

from base64 import b64encode
from hashlib import sha256
from threading import get_ident

import pytest

from onec_runtime.errors import ProtocolError
from onec_runtime.execution.arbiter import RdbgArbiter, RouteToken
from onec_runtime.execution.capture.ticket_materialization import WorkerTransferCatalog
from onec_runtime.execution.capture.materialization import CaptureMaterializationExecutor
from onec_runtime.execution.main.idle_materialization import MainIdleTargetFence
from onec_runtime.execution.value_materialization_router import ValueMaterializationRouter
from onec_runtime.execution.value_projection_service import ValueProjectionService
from onec_runtime.execution.worker_activation import WorkerMaterializationSnapshot

from test_capture_stack_inventory_adapter import ready_scope
from test_capture_ticket_data_plane import Ticket, TicketController
from test_main_idle_materialization import Session, TARGET


def _responses(payload: bytes) -> list[str]:
    encoded = b64encode(payload).decode("ascii")
    return [
        f"R|7|4|{len(payload)}|{sha256(payload).hexdigest()}|{len(encoded)}",
        encoded,
    ]


def _main_router(session: Session, catalog: list[WorkerMaterializationSnapshot]):
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
        worker_catalog_snapshot=lambda: catalog[0],
    )
    return router, arbiter


def test_main_projection_payload_runs_all_remote_steps_on_one_arbiter_thread() -> None:
    """Break: plan execution bypasses MAIN's arbiter for the payload read."""

    payload = b'{"version":1,"root":{"t":"number","v":"12"}}'
    session = Session(_responses(payload))
    catalog = [WorkerMaterializationSnapshot(2, ())]
    router, arbiter = _main_router(session, catalog)
    service = ValueProjectionService(
        router, runtime_generation=7, context_generation=4,
        worker_catalog_snapshot=lambda: WorkerTransferCatalog(2, ()),
    )
    try:
        kind, serialized = service.project_value_payload(
            "e1cRuntimeКонтекст.Массив", kind="slice", offset=3, limit=1,
            columns=(), names=(), max_depth=2, max_items=1,
            max_rows=1, max_bytes=1024,
        )
        assert kind == "value"
        assert serialized == payload
        assert len([call for call in session.calls if call[0] == "start"]) == 2
        assert {call[2] for call in session.calls} == {arbiter._worker.ident}
        assert arbiter._worker.ident != get_ident()
        assert "ЗабратьКомпактнуюМатериализациюИзКонтекста" in session.calls[2][1]
    finally:
        arbiter.close(timeout=3)


def test_kind_inspection_serializes_only_a_bounded_kind_string() -> None:
    """Break: kind-only inspection downloads the value or leaks debugger text."""

    payload = b'{"version":1,"root":{"t":"string","v":"table"}}'
    session = Session(_responses(payload))
    catalog = [WorkerMaterializationSnapshot(0, ())]
    router, arbiter = _main_router(session, catalog)
    service = ValueProjectionService(
        router, runtime_generation=7, context_generation=4,
        worker_catalog_snapshot=lambda: WorkerTransferCatalog(0, ()),
    )
    try:
        assert service.materialization_kind("e1cRuntimeКонтекст.Таблица") == "table"
        instruction = session.calls[0][1]
        assert "ПолучитьВидМатериализации(e1cRuntimeКонтекст.Таблица)" in instruction
        assert "СериализоватьЗначение(ВидМатериализации" in instruction
        assert "СериализоватьКомпактнуюТаблицу" not in instruction
        assert {call[2] for call in session.calls} == {arbiter._worker.ident}
    finally:
        arbiter.close(timeout=3)


def test_main_catalog_change_rejects_before_any_remote_effect() -> None:
    """Break: an old Worker privacy catalog is used to build a new ticket."""

    session = Session([])
    catalog = [WorkerMaterializationSnapshot(3, ())]
    router, arbiter = _main_router(session, catalog)
    try:
        with pytest.raises(ProtocolError, match="Worker catalog changed"):
            router.inspect_kind(
                "e1cRuntimeКонтекст.Значение", catalog=WorkerTransferCatalog(2, ()),
                timeout_s=None,
            )
        assert session.calls == []
    finally:
        arbiter.close(timeout=3)


def test_capture_projection_rechecks_catalog_inside_same_scope_ticket() -> None:
    """Break: Worker publication races a CAPTURE projection after admission."""

    scope = ready_scope()
    catalog = [WorkerMaterializationSnapshot(1, ())]

    class Controller(TicketController):
        def value_route_snapshot(self):
            return scope

        def main_idle_fence(self):
            return None

        def main_idle_fence_in_ticket(self):
            return None

        def submit_capture_materialization(self, plan, *, _before_first_effect=None):
            catalog[0] = WorkerMaterializationSnapshot(2, ())
            assert _before_first_effect is not None
            _before_first_effect()
            self.materialization_ticket = Ticket(b"not dispatched")
            return self.materialization_ticket

    controller = Controller(scope)
    initial_ticket = controller.materialization_ticket
    arbiter = RdbgArbiter(Session([]), RouteToken("runtime", 1, 0, "main"))
    router = ValueMaterializationRouter(
        controller, arbiter,
        runtime_generation=7, context_generation=4,
        worker_catalog_snapshot=lambda: catalog[0],
    )
    try:
        with pytest.raises(ProtocolError, match="Worker catalog changed"):
            router.inspect_kind(
                "e1cRuntimeКонтекст.Значение", catalog=WorkerTransferCatalog(1, ()),
                timeout_s=None,
            )
        assert controller.capture_scope is scope
        assert controller.materialization_ticket is initial_ticket
    finally:
        arbiter.close(timeout=3)


def test_capture_kind_inspection_uses_a_valid_private_projection_plan() -> None:
    """Break: kind lookup uses a private key CAPTURE cannot track or clean."""

    scope = ready_scope()
    payload = b'{"version":1,"root":{"t":"string","v":"table"}}'

    class Controller(TicketController):
        def value_route_snapshot(self):
            return scope

        def main_idle_fence(self):
            return None

        def main_idle_fence_in_ticket(self):
            return None

        def submit_capture_materialization(self, plan, *, _before_first_effect=None):
            CaptureMaterializationExecutor._validate(scope, plan)
            assert _before_first_effect is not None
            _before_first_effect()
            return Ticket(payload)

    arbiter = RdbgArbiter(Session([]), RouteToken("runtime", 1, 0, "main"))
    router = ValueMaterializationRouter(
        Controller(scope), arbiter,
        runtime_generation=7, context_generation=4,
        worker_catalog_snapshot=lambda: WorkerMaterializationSnapshot(1, ()),
    )
    try:
        assert router.inspect_kind(
            "e1cRuntimeКонтекст.Таблица", catalog=WorkerTransferCatalog(1, ()), timeout_s=None,
        ) == "table"
    finally:
        arbiter.close(timeout=3)
