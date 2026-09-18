"""Public value projections stay bounded and use verified ticket bytes."""

from __future__ import annotations

from base64 import b64encode
from collections import deque
from decimal import Decimal
from hashlib import sha256

import pytest

from onec_runtime.errors import ProtocolError
from onec_runtime.execution.capture.ticket_materialization import WorkerTransferCatalog
from onec_runtime.execution.value_projection_service import ValueProjectionService
from onec_runtime.performance_profile import PhaseRecorder


VALUE_PAYLOAD = b'{"version":1,"root":{"t":"number","v":"12.50"}}'
TABLE_PAYLOAD = (
    b'{"version":1,"columns":["Amount"],"kinds":["number"],'
    b'"reference_modes":{}}\n[12.5]\n'
)


class TicketPort:
    """External ticket boundary; exercise real admission and integrity decoders."""

    def __init__(self, *payloads: bytes, kind: str = "value") -> None:
        self.payloads = deque(payloads)
        self.kind = kind
        self.instructions: list[str] = []
        self.catalogs: list[WorkerTransferCatalog] = []
        self.kind_reads: list[str] = []
        self.selected: list[tuple[object, ...]] = []

    def transfer(self, plan, *, catalog, timeout_s):  # type: ignore[no-untyped-def]
        self.instructions.append(plan.instruction)
        self.catalogs.append(catalog)
        payload = self.payloads.popleft()
        digest = sha256(payload).hexdigest()
        encoded = b64encode(payload).decode("ascii")
        metadata = f"R|7|1|{len(payload)}|{digest}|{len(encoded)}"
        plan.admit_metadata(metadata)
        return plan.decode(metadata, encoded)

    def inspect_kind(self, handle, *, catalog, timeout_s):  # type: ignore[no-untyped-def]
        self.kind_reads.append(handle)
        self.catalogs.append(catalog)
        return self.kind

    def validate_selected_table_handle(self, handle):  # type: ignore[no-untyped-def]
        self.kind_reads.append(handle)

    def transfer_selected_table(self, handle, policy, *, max_rows, max_bytes,
                                catalog, timeout_s, relative_offset=0,
                                relative_limit=None):  # type: ignore[no-untyped-def]
        self.selected.append((
            handle, policy, max_rows, max_bytes, catalog, timeout_s,
            relative_offset, relative_limit,
        ))
        return self.payloads.popleft()


def _service(port: TicketPort) -> ValueProjectionService:
    return ValueProjectionService(
        port,
        runtime_generation=7,
        context_generation=1,
        worker_catalog_snapshot=lambda: WorkerTransferCatalog(3, ()),
    )


def test_table_projection_transfers_only_selected_rows_and_decodes_frame() -> None:
    """Break: a small slice downloads a whole table or uses the wrong range."""

    port = TicketPort(TABLE_PAYLOAD)
    frame = _service(port).project_to_df(
        "e1cRuntimeКонтекст.Таблица", {"offset": 10, "limit": 2}, max_rows=2,
    )

    assert frame["Amount"].tolist() == [12.5]
    assert len(port.instructions) == 1
    assert "Скопировать(СтрокиПроекции" in port.instructions[0]
    assert "Для ИндексПроекции = 10 По Мин(e1cRuntimeКонтекст.Таблица.Количество() - 1, 11)" in port.instructions[0]
    assert port.catalogs == [WorkerTransferCatalog(3, ())]


def test_value_projection_preserves_typed_result() -> None:
    """Break: a bounded array selection returns wire text or a table decoder."""

    port = TicketPort(VALUE_PAYLOAD)
    assert _service(port).project_value(
        "e1cRuntimeКонтекст.Числа", {"offset": 2, "limit": 1}, max_items=1,
    ) == Decimal("12.50")
    assert port.kind_reads == []
    assert "Для ИндексПроекции = 2 По Мин(e1cRuntimeКонтекст.Числа.Количество() - 1, 2)" in port.instructions[0]


def test_value_projection_selects_table_decoder_inside_one_ticket() -> None:
    """Break: a sliced table materialize call is decoded as a typed array."""

    port = TicketPort(TABLE_PAYLOAD)
    frame = _service(port).project_value(
        "e1cRuntimeКонтекст.Таблица", {"offset": 5, "limit": 1}, max_items=1,
    )
    assert frame["Amount"].tolist() == [12.5]
    assert len(port.instructions) == 1
    assert port.kind_reads == []
    assert "СериализоватьКомпактнуюТаблицу" in port.instructions[0]


def test_mismatched_projection_payload_is_not_exposed() -> None:
    """Break: a value envelope is returned as a compact table projection."""

    port = TicketPort(VALUE_PAYLOAD)
    with pytest.raises(ProtocolError, match="payload kind"):
        _service(port).project_value_payload(
            "e1cRuntimeКонтекст.Таблица", kind="table_rows", offset=0, limit=1,
            columns=(), names=(), max_depth=1, max_items=1,
            max_rows=1, max_bytes=1024,
        )


@pytest.mark.parametrize(
    ("handle", "selection"),
    [
        ("e1cRuntimeКонтекст.А;Выполнить(1)", {"offset": 0, "limit": 1}),
        ("e1cRuntimeКонтекст.Таблица", {"offset": 0, "limit": 0}),
        ("e1cRuntimeКонтекст.Таблица", {"offset": 10_000_000, "limit": 1}),
    ],
)
def test_invalid_projection_is_rejected_before_dispatch(
    handle: str, selection: dict[str, int],
) -> None:
    """Break: an invalid handle or unbounded range reaches the 1C ticket."""

    port = TicketPort(TABLE_PAYLOAD)
    with pytest.raises((ProtocolError, ValueError)):
        _service(port).project_to_df(handle, selection, max_rows=10)
    assert port.instructions == []


def test_kind_reader_accepts_only_known_materialization_routes() -> None:
    """Break: raw debugger result text becomes a public kind discriminator."""

    port = TicketPort(kind="<debugger-xml>")
    with pytest.raises(ProtocolError, match="kind"):
        _service(port).materialization_kind("e1cRuntimeКонтекст.Значение")
    assert port.kind_reads == ["e1cRuntimeКонтекст.Значение"]


def test_invalid_value_budget_is_rejected_before_kind_inspection() -> None:
    """Break: an invalid projection budget still sends a kind helper to 1C."""

    port = TicketPort(VALUE_PAYLOAD)
    with pytest.raises((ProtocolError, ValueError)):
        _service(port).project_value(
            "e1cRuntimeКонтекст.Числа", {"offset": 0, "limit": 1},
            max_items=1, max_bytes=0,
        )
    assert port.kind_reads == []
    assert port.instructions == []


def test_direct_value_payload_is_typed_serialization_not_debugger_text() -> None:
    """Break: direct payload route leaks unchecked transport output."""

    port = TicketPort(VALUE_PAYLOAD)
    assert _service(port).materialize_value_payload(
        "e1cRuntimeКонтекст.Число", max_depth=2, max_items=5, max_bytes=1024,
    ) == VALUE_PAYLOAD
    assert "СериализоватьЗначение" in port.instructions[0]
    assert b"debugger" not in VALUE_PAYLOAD


def test_direct_table_payload_uses_compact_serializer_and_bounded_rows() -> None:
    """Break: public payload path returns a full or typed-value transfer."""

    port = TicketPort(TABLE_PAYLOAD)
    assert _service(port).materialize_table_payload(
        "e1cRuntimeКонтекст.Таблица", max_rows=2, max_bytes=1024,
    ) == TABLE_PAYLOAD
    assert "СериализоватьКомпактнуюТаблицу" in port.instructions[0]
    assert port.catalogs == [WorkerTransferCatalog(3, ())]


def test_selected_table_payload_and_relative_projection_use_opaque_ticket_port() -> None:
    handle = "capture_table_" + "a" * 32
    port = TicketPort(TABLE_PAYLOAD, TABLE_PAYLOAD)
    service = _service(port)
    profiler = PhaseRecorder()
    assert service.materialization_kind(handle) == "table"
    assert service.materialize_table_payload(
        handle, max_rows=5, max_bytes=1024, profiler=profiler,
    ) == TABLE_PAYLOAD
    assert [event.phase for event in profiler.events] == ["table.routed_transfer"]
    frame = service.project_to_df(
        handle, {"offset": 2, "limit": 1}, max_rows=5, max_bytes=1024,
    )
    assert frame["Amount"].tolist() == [12.5]
    assert port.instructions == []
    assert port.selected[0][0] == handle
    assert port.selected[1][-2:] == (2, 1)
    assert port.kind_reads == [handle]
