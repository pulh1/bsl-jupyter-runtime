from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from onec_runtime_mcp.agent.capture_contracts import CaptureFence
from onec_runtime_mcp.agent.capture_service import CaptureService
from onec_runtime_mcp.agent.observation import (
    ManagerOrigin,
    ObservationItem,
    ObservationPlan,
    ObservationSource,
    ObservationSourceKind,
    SelectionKind,
    ValueSelection,
)
from onec_runtime_mcp.agent.onec_values import OnecValueResolver
from onec_runtime_mcp.agent.proxies import ProxyRealm, ProxyRegistry, ReleasedProxy
from onec_runtime_mcp.agent.value_service import ValueService
from onec_runtime.errors import ProtocolError
from onec_runtime.prototype_runtime import (
    OperationHandle,
    OperationState,
    PrototypeRuntimeController,
)
from onec_runtime.rdbg.models import (
    CollectionCell,
    CollectionRow,
    EvaluationResult,
    FrameVariable,
    ModuleLocation,
    PendingEvaluation,
    TargetId,
)


WORKSPACE = Path(__file__).resolve().parents[2]
KERNEL_MODULE = (
    WORKSPACE
    / "onec"
    / "OnecInteractiveRuntime"
    / "CommonModules"
    / "RuntimeKernelServer"
    / "Ext"
    / "Module.bsl"
)


def fence() -> CaptureFence:
    return CaptureFence("intent", "operation", 3, "a" * 64, 1, 1)


class StrictCaptureRdbg:
    def __init__(self) -> None:
        self.evaluations: list[tuple[str, int]] = []
        self.collections: list[tuple[str, int, int, int]] = []
        self.target_id = TargetId(uuid4(), uuid4(), 1)
        self._pending: dict[int, tuple[PendingEvaluation, EvaluationResult]] = {}

    def evaluate(self, expression: str, *, stack_level: int, **_kwargs: object) -> EvaluationResult:
        self.evaluations.append((expression, stack_level))
        return EvaluationResult(uuid4(), "Булево", "Истина", False)

    def evaluate_collection(
        self,
        expression: str,
        *,
        start_index: int,
        page_size: int,
        stack_level: int,
        **_kwargs: object,
    ) -> EvaluationResult:
        self.collections.append((expression, start_index, page_size, stack_level))
        row = CollectionRow(
            0,
            (CollectionCell("Имя", "Строка", '"Employee"', value_string="Employee"),),
        )
        return EvaluationResult(uuid4(), "ТаблицаЗначений", "", False, collection_rows=(row,))

    def start_evaluation(
        self,
        expression: str,
        *,
        stack_level: int,
        timeout_s: float,
        max_text_size: int = 307_200,
        on_transport_dispatch=None,  # type: ignore[no-untyped-def]
    ) -> PendingEvaluation:
        if on_transport_dispatch is not None:
            on_transport_dispatch()
        result = self.evaluate(
            expression,
            stack_level=stack_level,
            timeout_s=timeout_s,
            max_text_size=max_text_size,
        )
        pending = PendingEvaluation(self.target_id, result.result_id, self)
        self._pending[id(pending)] = (pending, result)
        return pending

    def wait_evaluation_event(
        self,
        pending: PendingEvaluation,
        *,
        timeout_s: float,
    ) -> EvaluationResult:
        del timeout_s
        stored, result = self._pending.pop(id(pending))
        assert stored is pending
        return result


def captured_controller(rdbg: StrictCaptureRdbg) -> tuple[PrototypeRuntimeController, str]:
    controller = PrototypeRuntimeController(
        rdbg,  # type: ignore[arg-type]
        ModuleLocation("ExtensionModule", "", None, None, 1, "Runtime"),
    )
    controller.active_operation = OperationHandle(1, "", "")
    controller._capture_target_id = rdbg.target_id
    controller.stop_sequence = 1
    controller.state = OperationState.CAPTURED
    controller._replace_capture_evaluation_coordinator()
    controller.capture_frame_stack_level = 0
    controller.capture_kernel_stack_level = 2
    controller._capture_frame_variables = (FrameVariable("Query", "Query", "<query>"),)
    manager = controller.resolve_capture_manager_origin("Query", ("Manager",))
    return controller, manager["handle"]  # type: ignore[return-value]


def test_production_inventory_reads_descriptor_columns_without_row_materialization() -> None:
    """Break caught: selection=None must not enter any row/result helper."""
    rdbg = StrictCaptureRdbg()
    controller, manager_handle = captured_controller(rdbg)

    result = controller.capture_temporary_tables(
        manager_handle,
        names=("Staff",),
        cursor=0,
        limit=1,
        selection=None,
    )

    assert result["items"][0]["schema"] == ("Employee",)  # type: ignore[index]
    metadata_handle = result["items"][0]["handle"]  # type: ignore[index]
    with pytest.raises(ProtocolError, match="metadata-only"):
        controller.capture_value_handle(metadata_handle)
    assert rdbg.evaluations == [
        ('ТипЗнч(Query.Manager) = Тип("МенеджерВременныхТаблиц")', 0)
    ]
    assert len(rdbg.collections) == 1
    expression, start_index, page_size, stack_level = rdbg.collections[0]
    assert expression.startswith("RuntimeKernelServer.ПолучитьСхемуВременнойТаблицыОтладки(")
    assert "ПолучитьДанные" not in expression
    assert (start_index, page_size, stack_level) == (0, 64, 2)


def test_extension_separates_metadata_from_bounded_descriptor_data_result() -> None:
    """Break caught: a VT descriptor is not itself a value table/query result."""
    source = KERNEL_MODULE.read_text(encoding="utf-8-sig")
    metadata = source.split(
        "Функция ПолучитьСхемуВременнойТаблицыОтладки", 1
    )[1].split("КонецФункции", 1)[0]
    selection = source.split(
        "Функция СохранитьВременнуюТаблицуОтладки", 1
    )[1].split("КонецФункции", 1)[0]

    assert "ОписательТаблицы.Колонки" in metadata
    assert "ПолучитьДанные" not in metadata
    assert "Выгрузить" not in metadata
    assert "СтрокаТаблицы" not in metadata

    assert "ОписательТаблицы.ПолучитьДанные()" in selection
    assert "РезультатДанных.Выбрать()" in selection
    assert "ПодготовитьТабличноеЗначение" not in selection
    assert "Выгрузить" not in selection
    assert selection.index("ОписательТаблицы.ПолучитьДанные()") < selection.index(
        "РезультатДанных.Выбрать()"
    )


class CacheBackend:
    runtime_id = "runtime"

    def __init__(self) -> None:
        self.table_calls = 0

    def validate_value_reference(self, handle: str) -> None:
        del handle

    def frame_variables(self, capture, *, filters, cursor, limit):
        return {"items": (), "total": 0, "next_cursor": None}

    def resolve_manager_origin(self, capture, origin):
        return {
            "key": "manager",
            "handle": "native-manager",
            "type_name": "МенеджерВременныхТаблиц",
        }

    def temporary_tables(self, capture, manager_handle, *, names, cursor, limit, selection):
        self.table_calls += 1
        return {
            "items": (
                {
                    "name": "Staff",
                    "schema": ("Employee",),
                    "handle": "bounded-staff" if selection is not None else "metadata-staff",
                },
            ),
            "next_cursor": None,
        }


def manager_plan() -> ObservationPlan:
    return ObservationPlan(
        (
            ObservationItem(
                "manager",
                ObservationSource(
                    ObservationSourceKind.TEMPORARY_TABLE_MANAGER,
                    origin=ManagerOrigin("frame", "Query", ("Manager",)),
                ),
            ),
        )
    )


def table_plan(
    manager_id: str, selection: ValueSelection | None = None
) -> ObservationPlan:
    return ObservationPlan(
        (
            ObservationItem(
                "table",
                ObservationSource(
                    ObservationSourceKind.TEMPORARY_TABLE,
                    manager_id=manager_id,
                    table="Staff",
                ),
                select=selection,
            ),
        )
    )


def inspect(
    service: CaptureService,
    backend: CacheBackend,
    registry: ProxyRegistry,
    observe: ObservationPlan,
):
    return service.inspect(
        backend,
        registry,
        fence=fence(),
        runtime_id=backend.runtime_id,
        runtime_generation=1,
        context_generation=1,
        filters={},
        cursor=0,
        limit=10,
        observe=observe,
    )


def test_metadata_proxy_does_not_claim_unbounded_table_materialization(tmp_path: Path) -> None:
    """Break caught: inventory metadata must not masquerade as transferred rows."""
    service, backend, registry = CaptureService(tmp_path), CacheBackend(), ProxyRegistry()
    service.activate_capture(fence(), registry)
    manager = inspect(service, backend, registry, manager_plan()).temporary_table_managers[0]

    table = inspect(
        service, backend, registry, table_plan(manager.manager_id)
    ).temporary_tables[0]
    proxy = registry.resolve(table.table_id)

    assert proxy.type_name == "ОписаниеВременнойТаблицы"
    assert proxy.capabilities == ("describe", "size", "select")


def test_reinspection_republishes_a_released_selected_table_without_backend_work(
    tmp_path: Path,
) -> None:
    """Break caught: descriptor cache must never return a released table_id."""
    service, backend, registry = CaptureService(tmp_path), CacheBackend(), ProxyRegistry()
    service.activate_capture(fence(), registry)
    manager = inspect(service, backend, registry, manager_plan()).temporary_table_managers[0]
    selection = ValueSelection(
        SelectionKind.TABLE_ROWS,
        offset=2,
        limit=3,
        columns=("Employee",),
    )
    plan = table_plan(manager.manager_id, selection)

    first = inspect(service, backend, registry, plan).temporary_tables[0]
    release = ValueService(
        registry,
        {ProxyRealm.ONEC: OnecValueResolver(backend, registry)},  # type: ignore[dict-item]
    ).call("value.release", {"proxy_id": first.table_id})
    second = inspect(service, backend, registry, plan).temporary_tables[0]

    assert release.value == {"released": True, "binding_deleted": False}
    assert second.table_id != first.table_id
    with pytest.raises(ReleasedProxy):
        registry.resolve(first.table_id)
    assert registry.resolve(second.table_id).proxy_id == second.table_id
    assert registry.resolver_handle(second.table_id) == "bounded-staff"
    assert backend.table_calls == 1
