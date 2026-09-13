from __future__ import annotations

from dataclasses import asdict, replace
import json

import pytest

from onec_runtime_mcp.agent.capture_contracts import (
    CaptureContinuationRequest,
    CaptureFence,
    CaptureInspection,
    CapturePointRequest,
    CaptureView,
    FrameVariableDescriptor,
    ResolvedCapturePoint,
    TemporaryTableDescriptor,
    TemporaryTableManagerDescriptor,
)
from onec_runtime_mcp.agent.contracts import AgentOperationState, ServiceResponse, to_wire
from onec_runtime_mcp.agent.facade import AgentFacade
from onec_runtime_mcp.agent.facade_contracts import (
    AgentOperationIdentity,
    AgentOperationKind,
    AgentOperationView,
    MutationConfidence,
    OperationTruncation,
    OperationViewFacts,
)
from onec_runtime_mcp.agent.observation import ManagerOrigin, ObservationPlan
from onec_runtime_mcp.agent.proxies import (
    MeasurementCost,
    ProxyConsistency,
    ProxyDescriptor,
    ProxyFence,
    ProxyLifetime,
    ProxyProvenance,
    ProxyRealm,
    ValueBudget,
    ValueSize,
)
from uuid import uuid4


def capture_fence() -> CaptureFence:
    return CaptureFence(
        capture_intent_id="intent-1",
        operation_id="op-1",
        source_revision=7,
        source_sha256="a" * 64,
        capture_generation=3,
        stop_sequence=1,
    )


def frame_fence() -> ProxyFence:
    return ProxyFence(
        runtime_id="runtime-1",
        runtime_generation=5,
        context_generation=2,
        capture_fence=capture_fence(),
    )


def point() -> ResolvedCapturePoint:
    return ResolvedCapturePoint(
        name="before_query",
        project="erp",
        module="ОбщийМодуль.Расчет",
        procedure="Рассчитать",
        line=18,
        source_revision=7,
        source_sha256="a" * 64,
        executable_line=19,
        excerpt="Запрос.Выполнить();",
    )


def frame_variable(name: str = "Сумма", *, capabilities: tuple[str, ...] = ()) -> FrameVariableDescriptor:
    return FrameVariableDescriptor(
        name=name,
        type_name="Число",
        fence=capture_fence(),
        capabilities=capabilities,
    )


def capture_view() -> CaptureView:
    return CaptureView(
        fence=capture_fence(),
        location=point(),
        inspection=None,
        dirty_roots=("Сумма",),
    )


def frame_proxy_wire() -> dict[str, object]:
    return to_wire(
        ProxyDescriptor(
            proxy_id=str(uuid4()),
            realm=ProxyRealm.ONEC,
            lifetime=ProxyLifetime.FRAME,
            qualified_name="bsl.Сумма",
            type_name="Число",
            version=1,
            consistency=ProxyConsistency.EXACT,
            fence=frame_fence(),
            provenance=ProxyProvenance(
                cell_id="cell-main",
                revision=7,
                source_sha256="a" * 64,
                operation_id="op-1",
            ),
            capabilities=("inspect",),
        )
    )  # type: ignore[return-value]


def operation_view_wire() -> dict[str, object]:
    return {
        "operation": {
            "operation_id": "op-1",
            "kind": "capture_run_until",
            "runtime_id": "runtime-1",
            "runtime_generation": 5,
            "cell_id": "cell-main",
            "revision": 7,
            "source_sha256": "a" * 64,
        },
        "state": "captured",
        "messages": [],
        "next_message_cursor": 0,
        "next_event_cursor": 0,
        "changed_variables": [frame_proxy_wire()],
        "change_confidence": "unknown",
        "outputs": {},
        "capture": to_wire(capture_view()),
        "failure": None,
        "recovery": [],
        "truncation": {
            "messages": False,
            "changed_variables": False,
            "outputs": False,
        },
    }


def test_capture_fence_requires_complete_correlation_identity() -> None:
    fence = capture_fence()

    assert to_wire(fence)["capture_generation"] == 3
    with pytest.raises(ValueError):
        replace(fence, source_sha256="")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("capture_intent_id", "intent-1()"),
        ("operation_id", "op[1]"),
        ("source_revision", 0),
        ("source_sha256", "A" * 64),
        ("capture_generation", 0),
        ("stop_sequence", 0),
    ],
)
def test_capture_fence_rejects_stale_or_unsafe_identity_parts(
    field: str, value: object
) -> None:
    with pytest.raises(ValueError):
        replace(capture_fence(), **{field: value})


def test_capture_point_request_uses_source_location_not_debugger_identity() -> None:
    request = CapturePointRequest(
        name="before_query",
        project="erp",
        module="ОбщийМодуль.Расчет",
        procedure="Рассчитать",
        line=18,
    )

    assert to_wire(request)["line"] == 18
    with pytest.raises(ValueError):
        replace(request, procedure="Рассчитать()")
    with pytest.raises(ValueError):
        replace(request, module="ОбщийМодуль.Расчет[0]")


def test_capture_point_request_allows_a_stable_fragment_without_debugger_coordinates() -> None:
    request = CapturePointRequest(
        name="before_query",
        source_fragment="Запрос.Выполнить();",
    )

    assert request.source_fragment == "Запрос.Выполнить();"


def test_frame_fence_carries_runtime_and_complete_capture_identity() -> None:
    fence = frame_fence()

    assert to_wire(fence)["capture_fence"] == to_wire(capture_fence())


def test_manager_origin_is_structured_and_frame_scoped() -> None:
    manager = TemporaryTableManagerDescriptor(
        manager_id="vtm-17",
        origin=ManagerOrigin(
            namespace="frame",
            root="Запрос",
            fields=("МенеджерВременныхТаблиц",),
        ),
        fence=capture_fence(),
        capabilities=("list_tables", "inspect_table", "materialize_table"),
    )

    assert manager.origin.fields == ("МенеджерВременныхТаблиц",)
    assert "Запрос.Менеджер" not in json.dumps(to_wire(manager), ensure_ascii=False)


def test_direct_manager_local_has_empty_fields_and_rejects_calls_or_indexes() -> None:
    direct = ManagerOrigin(namespace="frame", root="Менеджер", fields=())
    assert direct.fields == ()

    for fields in (("Получить()",), ("Таблицы", "0")):
        with pytest.raises(ValueError):
            ManagerOrigin(namespace="frame", root="Запрос", fields=fields)


def test_inspection_rejects_duplicate_manager_and_table_ids() -> None:
    manager = TemporaryTableManagerDescriptor(
        manager_id="vtm-17",
        origin=ManagerOrigin(namespace="frame", root="Менеджер"),
        fence=capture_fence(),
        capabilities=("list_tables",),
    )
    table = TemporaryTableDescriptor(
        table_id="vtt-1",
        manager_id="vtm-17",
        name="ВТКадры",
        fence=capture_fence(),
        known_size=ValueSize.unknown(MeasurementCost.CHEAP),
    )

    with pytest.raises(ValueError):
        CaptureInspection(
            fence=capture_fence(),
            variables=(),
            temporary_table_managers=(manager, manager),
            temporary_tables=(table,),
            cursor=0,
            limit=20,
            total_variables=0,
            next_cursor=None,
            truncated=False,
        )
    with pytest.raises(ValueError):
        CaptureInspection(
            fence=capture_fence(),
            variables=(),
            temporary_table_managers=(manager,),
            temporary_tables=(table, table),
            cursor=0,
            limit=20,
            total_variables=0,
            next_cursor=None,
            truncated=False,
        )


def test_inspection_pages_with_a_bounded_cursor_and_explicit_truncation() -> None:
    inspection = CaptureInspection(
        fence=capture_fence(),
        variables=tuple(
            frame_variable(f"Переменная{index}") for index in range(20)
        ),
        temporary_table_managers=(),
        temporary_tables=(),
        cursor=0,
        limit=20,
        total_variables=100,
        next_cursor=20,
        truncated=True,
    )

    assert inspection.next_cursor == 20
    assert inspection.truncated is True
    with pytest.raises(ValueError):
        replace(inspection, limit=0)
    with pytest.raises(ValueError):
        replace(inspection, next_cursor=101)
    with pytest.raises(ValueError):
        replace(
            inspection,
            variables=(
                FrameVariableDescriptor(
                    name="Сумма",
                    type_name="Число",
                    fence=replace(capture_fence(), stop_sequence=2),
                    capabilities=("inspect",),
                ),
            ),
        )


def test_capture_view_is_typed_nested_content_with_no_raw_frame_values() -> None:
    sentinel = "CAPTURE_RAW_SECRET_SENTINEL_7f6b4a"
    descriptor_wire = to_wire(
        FrameVariableDescriptor(
            name="Пароль",
            type_name="Строка",
            fence=capture_fence(),
            capabilities=("inspect",),
        )
    )
    unsafe_wire = {**descriptor_wire, "value": sentinel}
    assert sentinel in json.dumps(unsafe_wire, ensure_ascii=False)
    with pytest.raises(ValueError):
        FrameVariableDescriptor.from_wire(unsafe_wire)

    inspection = CaptureInspection(
        fence=capture_fence(),
        variables=(
            FrameVariableDescriptor.from_wire(descriptor_wire),
        ),
        temporary_table_managers=(),
        temporary_tables=(),
        cursor=0,
        limit=20,
        total_variables=1,
        next_cursor=None,
        truncated=False,
    )
    view = CaptureView(
        fence=capture_fence(),
        location=point(),
        inspection=inspection,
        dirty_roots=("Сумма",),
        paused=True,
        recovery=(),
    )

    wire = to_wire(view)
    assert wire["paused"] is True
    assert sentinel not in json.dumps(wire, ensure_ascii=False)
    assert "value" not in wire["inspection"]["variables"][0]
    assert sentinel not in repr(view)
    assert sentinel not in repr(asdict(view))


def test_continuation_is_fenced_and_uses_a_bounded_observation_plan() -> None:
    request = CaptureContinuationRequest(
        fence=capture_fence(),
        next_points=(
            CapturePointRequest(
                name="after_query",
                project="erp",
                module="ОбщийМодуль.Расчет",
                procedure="Рассчитать",
                line=25,
            ),
        ),
        observe=ObservationPlan.from_wire(
            {
                "items": [
                    {
                        "alias": "sum",
                        "source": {"kind": "frame_local", "name": "Сумма"},
                    }
                ]
            }
        ),
        budget=ValueBudget(1, 10, 10, 1024, 1.0),
        wait_seconds=1.0,
    )

    assert request.observe is not None
    with pytest.raises(ValueError):
        replace(request, wait_seconds=0.0)


def test_capture_wire_round_trips_through_facade_and_contract_readers() -> None:
    class ViewClient:
        def call(self, method: str, arguments: dict[str, object]) -> ServiceResponse:
            assert method == "operation.view"
            assert arguments["operation_id"] == "op-1"
            return ServiceResponse.success(operation_view_wire())

    view = AgentFacade(ViewClient())._operation_view("op-1", after_message_cursor=0)
    facts = OperationViewFacts.from_wire(
        {
            "changed_variables": [frame_proxy_wire()],
            "outputs": {},
            "capture": to_wire(capture_view()),
        }
    )

    assert view.capture == capture_view()
    assert view.changed_variables[0].fence.capture_fence == capture_fence()
    assert facts.capture == capture_view()
    assert facts.changed_variables[0].fence.capture_fence == capture_fence()


def test_capture_wire_reconstruction_requires_complete_fences_and_views() -> None:
    with pytest.raises(ValueError):
        CaptureFence.from_wire({"capture_intent_id": "intent-1"})
    with pytest.raises(ValueError):
        CaptureView.from_wire(
            {
                key: value
                for key, value in to_wire(capture_view()).items()
                if key != "paused"
            }
        )


def test_capture_inspection_rejects_incoherent_pages_and_oversize_metadata() -> None:
    valid = CaptureInspection(
        fence=capture_fence(),
        variables=(frame_variable(),),
        temporary_table_managers=(),
        temporary_tables=(),
        cursor=0,
        limit=1,
        total_variables=1,
        next_cursor=None,
        truncated=False,
    )

    with pytest.raises(ValueError):
        replace(valid, cursor=2)
    with pytest.raises(ValueError):
        replace(valid, total_variables=2)
    with pytest.raises(ValueError):
        replace(valid, total_variables=2, next_cursor=2, truncated=True)
    with pytest.raises(ValueError):
        replace(valid, variables=(frame_variable("Сумма"), frame_variable("Ставка")), total_variables=2)
    with pytest.raises(ValueError):
        frame_variable(capabilities=tuple(f"cap{i}" for i in range(33)))
    with pytest.raises(ValueError):
        frame_variable(capabilities=("x" * 129,))
    with pytest.raises(ValueError):
        TemporaryTableManagerDescriptor(
            manager_id="vtm-1",
            origin=ManagerOrigin(namespace="frame", root="Менеджер"),
            fence=capture_fence(),
            capabilities=tuple(f"cap{i}" for i in range(33)),
        )
    with pytest.raises(ValueError):
        TemporaryTableDescriptor(
            table_id="vtt-1",
            manager_id="vtm-1",
            name="ВТКадры",
            fence=capture_fence(),
            schema=tuple(f"Поле{i}" for i in range(101)),
        )
    with pytest.raises(ValueError):
        CaptureInspection(
            fence=capture_fence(),
            variables=(),
            temporary_table_managers=(
                TemporaryTableManagerDescriptor(
                    manager_id="vtm-1",
                    origin=ManagerOrigin(namespace="frame", root="Менеджер1"),
                    fence=capture_fence(),
                ),
                TemporaryTableManagerDescriptor(
                    manager_id="vtm-2",
                    origin=ManagerOrigin(namespace="frame", root="Менеджер2"),
                    fence=capture_fence(),
                ),
            ),
            temporary_tables=(),
            cursor=0,
            limit=1,
            total_variables=0,
            next_cursor=None,
            truncated=False,
        )


def test_capture_requires_paused_correlated_outer_operation_view() -> None:
    operation = AgentOperationIdentity(
        operation_id="op-1",
        kind=AgentOperationKind.CAPTURE_RUN_UNTIL,
        runtime_id="runtime-1",
        runtime_generation=5,
        cell_id="cell-main",
        revision=7,
        source_sha256="a" * 64,
    )
    view = AgentOperationView(
        operation=operation,
        state=AgentOperationState.CAPTURED,
        messages=(),
        next_message_cursor=0,
        next_event_cursor=0,
        changed_variables=(),
        change_confidence=MutationConfidence.UNKNOWN,
        outputs={},
        capture=capture_view(),
        failure=None,
        recovery=(),
        truncation=OperationTruncation(),
    )

    with pytest.raises(ValueError):
        replace(capture_view(), paused=False)
    with pytest.raises(ValueError):
        replace(view, state=AgentOperationState.COMPLETED)
    with pytest.raises(ValueError):
        replace(view, operation=replace(operation, operation_id="op-2"))
    # MAIN code identity and independently resolved capture-source identity
    # are both retained; neither may be rewritten to make them appear equal.
    assert replace(view, operation=replace(operation, revision=8)).capture is not None
    assert replace(view, operation=replace(operation, source_sha256="b" * 64)).capture is not None
