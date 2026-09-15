from __future__ import annotations

import pytest

from onec_runtime_mcp.agent.capture_contracts import CaptureFence
from onec_runtime_mcp.agent.capture_service import CaptureService
from onec_runtime_mcp.agent.onec_values import OnecValueResolver
from onec_runtime_mcp.agent.observation import (
    ManagerOrigin,
    ObservationItem,
    ObservationPlan,
    ObservationResult,
    ObservationSource,
    ObservationSourceKind,
    SelectionKind,
    ValueSelection,
)
from onec_runtime_mcp.agent.proxies import ProxyRegistry, SizeAccuracy, StaleProxy, ValueSize
from onec_runtime.errors import ProtocolError


def fence(generation: int = 1) -> CaptureFence:
    return CaptureFence("capture-intent", "op-capture", 7, "a" * 64, generation, generation)


def manager_origin_plan(root: str = "Query", fields: tuple[str, ...] = ("TempManager",)) -> ObservationPlan:
    return ObservationPlan((ObservationItem(
        "manager", ObservationSource(
            ObservationSourceKind.TEMPORARY_TABLE_MANAGER,
            origin=ManagerOrigin("frame", root, fields),
        ), ObservationResult.PROXY,
    ),))


def temporary_table_plan(manager_id: str, table: str, *, result: ObservationResult = ObservationResult.PROXY) -> ObservationPlan:
    return ObservationPlan((ObservationItem(
        "table", ObservationSource(
            ObservationSourceKind.TEMPORARY_TABLE, manager_id=manager_id, table=table
        ), result,
    ),), budget_profile="agent_dataframe" if result is ObservationResult.DATAFRAME else "agent_metadata")


class Frame:
    runtime_id = "runtime-capture"
    is_closed = False

    def __init__(self) -> None:
        self.resolve_origin_calls = 0
        self.table_calls: list[tuple[object, tuple[str, ...] | None]] = []
        self.row_reads = 0
        self.guard_calls: list[str] = []
        self.forbidden_handles: set[str] = set()

    def frame_variables(self, capture, *, filters, cursor, limit):
        items = (
            {"name": "Query", "type_name": "Запрос", "role": "local"},
            {"name": "DirectManager", "type_name": "МенеджерВременныхТаблиц", "role": "temporary_table_manager"},
        )
        items = tuple({**item, "handle": item.get("handle", f"frame-{index}")} for index, item in enumerate(items))
        return {"items": items[cursor : cursor + limit], "total": len(items), "next_cursor": cursor + limit if cursor + limit < len(items) else None}

    def resolve_manager_origin(self, capture: CaptureFence, origin: ManagerOrigin) -> dict[str, object]:
        self.resolve_origin_calls += 1
        if origin.root == "Missing":
            raise KeyError("root is absent")
        if origin.fields == ("Missing",):
            raise KeyError("field is absent")
        return {"key": "same-manager", "handle": "opaque-native-manager", "type_name": "МенеджерВременныхТаблиц"}

    def temporary_tables(self, capture, manager_handle, *, names, cursor, limit, selection):
        self.table_calls.append((manager_handle, names))
        all_tables = (
            {"name": "Staff", "schema": ("Employee", "Date"), "known_size": ValueSize(rows=3, accuracy=SizeAccuracy.EXACT), "handle": "opaque-staff"},
            {"name": "Payroll", "schema": ("Employee", "Amount"), "known_size": ValueSize(rows=2, accuracy=SizeAccuracy.EXACT), "handle": "opaque-payroll"},
        )
        selected = all_tables if names is None else tuple(table for table in all_tables if table["name"] in names)
        return {"items": selected[cursor : cursor + limit], "next_cursor": cursor + limit if cursor + limit < len(selected) else None}

    def materialize_table(self, handle: str, **options: object) -> object:
        self.row_reads += 1
        return []

    def validate_value_reference(self, handle: str) -> None:
        self.guard_calls.append(handle)
        if handle in self.forbidden_handles:
            raise ProtocolError("Worker generation objects are not public values")

    def materialization_kind(
        self, handle: str, *, timeout_s: float | None = None
    ) -> str:
        del timeout_s
        return "table"


def inspect(service: CaptureService, frame: Frame, registry: ProxyRegistry, *, observe=None):
    service.activate_capture(fence(), registry)
    return service.inspect(
        frame, registry, fence=fence(), runtime_id=frame.runtime_id, runtime_generation=1,
        context_generation=1, filters={}, cursor=0, limit=20, observe=observe,
    )


def test_explicit_manager_origin_resolves_once_then_uses_manager_id(tmp_path) -> None:
    service, frame, registry = CaptureService(tmp_path), Frame(), ProxyRegistry()

    first = inspect(service, frame, registry, observe=manager_origin_plan())
    manager = first.temporary_table_managers[0]
    second = inspect(service, frame, registry, observe=temporary_table_plan(manager.manager_id, "Staff"))

    assert frame.resolve_origin_calls == 1
    assert second.temporary_tables[0].name == "Staff"
    assert second.temporary_tables[0].manager_id == manager.manager_id
    assert frame.row_reads == 0


def test_capture_publication_guards_frame_manager_and_table_before_registration(
    tmp_path,
) -> None:
    frame_service = CaptureService(tmp_path / "frame")
    frame_backend = Frame()
    frame_backend.forbidden_handles.add("frame-0")
    frame_registry = ProxyRegistry()
    with pytest.raises(
        ProtocolError,
        match="^Worker generation objects are not public values$",
    ):
        inspect(frame_service, frame_backend, frame_registry)
    assert frame_registry.current() == ()

    manager_service = CaptureService(tmp_path / "manager")
    manager_backend = Frame()
    manager_backend.forbidden_handles.add("opaque-native-manager")
    manager_registry = ProxyRegistry()
    with pytest.raises(
        ProtocolError,
        match="^Worker generation objects are not public values$",
    ):
        inspect(
            manager_service,
            manager_backend,
            manager_registry,
            observe=manager_origin_plan(),
        )
    assert manager_service._managers == {}

    table_service = CaptureService(tmp_path / "table")
    table_backend = Frame()
    table_registry = ProxyRegistry()
    manager = inspect(
        table_service,
        table_backend,
        table_registry,
        observe=manager_origin_plan(),
    ).temporary_table_managers[0]
    table_backend.forbidden_handles.add("opaque-staff")
    with pytest.raises(
        ProtocolError,
        match="^Worker generation objects are not public values$",
    ):
        inspect(
            table_service,
            table_backend,
            table_registry,
            observe=temporary_table_plan(manager.manager_id, "Staff"),
        )
    assert table_service._table_proxies == {}


def test_direct_manager_and_duplicate_aliases_are_discovered_without_recursive_probing(tmp_path) -> None:
    service, frame, registry = CaptureService(tmp_path), Frame(), ProxyRegistry()

    query_item = manager_origin_plan("Query", ("TempManager",)).items[0]
    direct_item = ObservationItem(
        "direct_manager",
        ObservationSource(
            ObservationSourceKind.TEMPORARY_TABLE_MANAGER,
            origin=ManagerOrigin("frame", "DirectManager", ()),
        ),
        ObservationResult.PROXY,
    )
    result = inspect(service, frame, registry, observe=ObservationPlan((query_item, direct_item)))

    assert len(result.temporary_table_managers) == 1
    assert result.temporary_table_managers[0].origin.root == "Query"
    assert frame.resolve_origin_calls == 2


def test_inventory_returns_schema_and_known_size_without_transferring_rows(tmp_path) -> None:
    service, frame, registry = CaptureService(tmp_path), Frame(), ProxyRegistry()
    manager = inspect(service, frame, registry, observe=manager_origin_plan()).temporary_table_managers[0]

    result = inspect(service, frame, registry, observe=temporary_table_plan(manager.manager_id, "Staff"))

    table = result.temporary_tables[0]
    assert table.name == "Staff"
    assert table.schema == ("Employee", "Date")
    assert table.known_size == ValueSize(rows=3, accuracy=SizeAccuracy.EXACT)
    assert frame.row_reads == 0


def test_unknown_manager_or_table_and_non_proxy_observation_fail_closed(tmp_path) -> None:
    service, frame, registry = CaptureService(tmp_path), Frame(), ProxyRegistry()

    with pytest.raises(ValueError, match="manager"):
        inspect(service, frame, registry, observe=temporary_table_plan("not-known", "Staff"))
    manager = inspect(service, frame, registry, observe=manager_origin_plan()).temporary_table_managers[0]
    with pytest.raises(ValueError, match="table"):
        inspect(service, frame, registry, observe=temporary_table_plan(manager.manager_id, "Missing"))
    with pytest.raises(ValueError, match="bounded"):
        inspect(service, frame, registry, observe=temporary_table_plan(manager.manager_id, "Staff", result=ObservationResult.DATAFRAME))
    assert frame.row_reads == 0


def test_manager_and_table_handles_stale_after_capture_generation_changes(tmp_path) -> None:
    service, frame, registry = CaptureService(tmp_path), Frame(), ProxyRegistry()
    manager = inspect(service, frame, registry, observe=manager_origin_plan()).temporary_table_managers[0]
    table = inspect(service, frame, registry, observe=temporary_table_plan(manager.manager_id, "Staff")).temporary_tables[0]

    service.activate_capture(fence(2), registry)

    with pytest.raises(StaleProxy):
        service.manager_handle(manager.manager_id, fence=fence())
    with pytest.raises(StaleProxy):
        registry.resolve(table.table_id)


def test_table_proxy_reuses_the_existing_onec_compact_transfer_resolver(tmp_path) -> None:
    service, frame, registry = CaptureService(tmp_path), Frame(), ProxyRegistry()
    manager = inspect(service, frame, registry, observe=manager_origin_plan()).temporary_table_managers[0]
    table = inspect(service, frame, registry, observe=temporary_table_plan(manager.manager_id, "Staff")).temporary_tables[0]

    assert OnecValueResolver(frame, registry).materialization_kind(table.table_id) == "table"
    assert frame.row_reads == 0
