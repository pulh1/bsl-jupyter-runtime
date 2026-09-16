from __future__ import annotations

from dataclasses import replace

import pytest

from onec_runtime_mcp.agent.capture_contracts import CaptureFence
from onec_runtime_mcp.agent.capture_service import CaptureService
from onec_runtime_mcp.agent.contracts import ServiceResponse
from onec_runtime_mcp.agent.facade import AgentFacade
from onec_runtime_mcp.agent.contracts import CapabilityMode
from onec_runtime_mcp.agent.service import AgentWorkspaceService, _AdmittedRuntime
from onec_runtime_mcp.agent.observation import (
    ObservationItem,
    ObservationPlan,
    ObservationResult,
    ObservationSource,
    ObservationSourceKind,
)
from onec_runtime_mcp.agent.proxies import ProxyRegistry, StaleProxy, ValueSize


def fence(generation: int = 1) -> CaptureFence:
    return CaptureFence(
        capture_intent_id="capture-intent",
        operation_id="op-capture",
        source_revision=7,
        source_sha256="a" * 64,
        capture_generation=generation,
        stop_sequence=generation,
    )


class Frame:
    runtime_id = "runtime-capture"

    def __init__(self, variables: list[dict[str, object]]) -> None:
        self.variables = variables
        self.value_reads: list[str] = []

    def validate_value_reference(self, handle: str) -> None:
        del handle

    def frame_variables(
        self, capture: CaptureFence, *, filters, cursor, limit,
        timeout_s: float | None = None,
    ) -> dict[str, object]:
        del timeout_s
        assert capture == fence()
        all_items = tuple({**item, "handle": item.get("handle", f"frame-{index}")} for index, item in enumerate(self.variables))
        selected = tuple(item for item in all_items if all(value.casefold() in str(item.get({"type": "type_name"}.get(key, key), "")).casefold() for key, value in filters.items()))
        items = selected[cursor : cursor + limit]
        return {"items": items, "total": len(selected), "next_cursor": cursor + len(items) if cursor + len(items) < len(selected) else None}

    def resolve_manager_origin(self, capture: CaptureFence, origin: object) -> dict[str, object]:
        raise AssertionError("manager resolution was not requested")

    def temporary_tables(self, capture, manager_handle, *, names, cursor, limit, selection):
        raise AssertionError("table inventory was not requested")


def inspect(
    service: CaptureService,
    frame: Frame,
    *,
    cursor: int = 0,
    limit: int = 20,
    filters: dict[str, object] | None = None,
    observe: ObservationPlan | None = None,
):
    service.activate_capture(fence(), ProxyRegistry())
    return service.inspect(
        frame,
        ProxyRegistry(),
        fence=fence(),
        runtime_id=frame.runtime_id,
        runtime_generation=1,
        context_generation=1,
        filters={} if filters is None else filters,
        cursor=cursor,
        limit=limit,
        observe=observe,
    )


def test_inspect_pages_one_hundred_locals_without_reading_values(tmp_path) -> None:
    # Break caught: eagerly resolving frame values leaks data and makes a locals
    # inventory unbounded instead of metadata-only.
    frame = Frame(
        [
            {"name": f"Local{index}", "type_name": "Число", "role": "local"}
            for index in range(100)
        ]
    )

    result = inspect(CaptureService(tmp_path), frame)

    assert result.total_variables == 100
    assert len(result.variables) == 20
    assert result.next_cursor == 20
    assert result.truncated is True
    assert frame.value_reads == []


def test_inspect_filters_metadata_before_paging(tmp_path) -> None:
    frame = Frame(
        [
            {"name": "Amount", "type_name": "Number", "role": "local"},
            {"name": "Manager", "type_name": "TempManager", "role": "parameter"},
            {"name": "Name", "type_name": "String", "role": "local"},
        ]
    )

    result = inspect(
        CaptureService(tmp_path), frame, filters={"name": "man", "role": "parameter", "type": "temp"}
    )

    assert result.total_variables == 1
    assert [variable.name for variable in result.variables] == ["Manager"]
    assert result.next_cursor is None


def test_inspect_rejects_oversized_page_and_unknown_filters_before_frame_access(tmp_path) -> None:
    frame = Frame([])
    service = CaptureService(tmp_path)
    registry = ProxyRegistry()
    service.activate_capture(fence(), registry)

    with pytest.raises(ValueError, match="limit"):
        inspect(service, frame, limit=101)
    with pytest.raises(ValueError, match="filter"):
        inspect(service, frame, filters={"expression": "Manager.Tables"})


def test_frame_proxy_stales_when_capture_generation_changes(tmp_path) -> None:
    registry = ProxyRegistry()
    frame = Frame([{"name": "Amount", "type_name": "Число", "role": "local"}])
    service = CaptureService(tmp_path)
    service.activate_capture(fence(), registry)
    service.activate_capture(fence(), registry)
    service.activate_capture(fence(), registry)

    first = service.inspect(
        frame, registry, fence=fence(), runtime_id=frame.runtime_id, runtime_generation=1,
        context_generation=1, filters={}, cursor=0, limit=20,
    )
    service.activate_capture(fence(2), registry)

    with pytest.raises(StaleProxy):
        registry.resolve(first.variables[0].proxy_id)


def test_reinspection_keeps_a_frame_proxy_alive_until_the_generation_changes(tmp_path) -> None:
    registry = ProxyRegistry()
    frame = Frame([{"name": "Amount", "type_name": "Число", "role": "local"}])
    service = CaptureService(tmp_path)
    service.activate_capture(fence(), registry)

    service.inspect(
        frame, registry, fence=fence(), runtime_id=frame.runtime_id, runtime_generation=1,
        context_generation=1, filters={}, cursor=0, limit=20,
    )
    proxy_id = service.frame_proxy_id("Amount", fence())
    service.inspect(
        frame, registry, fence=fence(), runtime_id=frame.runtime_id, runtime_generation=1,
        context_generation=1, filters={}, cursor=0, limit=20,
    )

    assert registry.resolve(proxy_id).fence.capture_fence == fence()


def test_facade_routes_capture_inspect_with_only_public_fence_and_bounded_arguments() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def call(self, method: str, arguments: dict[str, object]) -> ServiceResponse:
            self.calls.append((method, arguments))
            return ServiceResponse.success({"inspection": "opaque"})

    client = Client()
    result = AgentFacade(client).capture_inspect(
        {
            "fence": {
                "capture_intent_id": "capture-intent",
                "operation_id": "op-capture",
                "source_revision": 7,
                "source_sha256": "a" * 64,
                "capture_generation": 1,
                "stop_sequence": 1,
            },
            "filters": {"name": "Amount"},
            "cursor": 0,
            "limit": 20,
        }
    )

    assert result == {"inspection": "opaque"}
    assert client.calls[0][0] == "capture.inspect"


def test_service_exposes_capture_inspection_only_for_the_active_paused_fence(tmp_path) -> None:
    class Runtime(Frame):
        def __init__(self) -> None:
            super().__init__([{"name": "Amount", "type_name": "Число", "role": "local"}])

        def close(self) -> None:
            pass

    class Factory:
        def start(self, *, mode: CapabilityMode) -> object:
            raise AssertionError("capture.inspect must not start a runtime")

    runtime = Runtime()
    service = AgentWorkspaceService(tmp_path, Factory(), maximum_mode=CapabilityMode.EXPERIMENT)
    service._runtime = _AdmittedRuntime(runtime, runtime.runtime_id, 1, CapabilityMode.EXPERIMENT)  # type: ignore[arg-type]
    service._selected["default"] = runtime.runtime_id
    service._capture.activate_capture(fence(), service._proxy_registry)
    try:
        response = service.call(
            "capture.inspect",
            {
                "fence": {
                    "capture_intent_id": "capture-intent", "operation_id": "op-capture",
                    "source_revision": 7, "source_sha256": "a" * 64,
                    "capture_generation": 1, "stop_sequence": 1,
                },
                "filters": {}, "cursor": 0, "limit": 20,
            },
        )
        assert response.ok
        assert response.value.total_variables == 1
    finally:
        service.close()


def test_capture_stack_and_frame_are_requested_separately_and_fenced(tmp_path) -> None:
    class Runtime(Frame):
        def __init__(self) -> None:
            super().__init__([])
            self.calls: list[str] = []

        def capture_stack(self, capture, *, cursor, limit, timeout_s=None):  # type: ignore[no-untyped-def]
            assert capture == fence() and cursor == 0 and limit == 20
            self.calls.append("stack")
            return {"frames": ({"level": 0, "line": 53},), "total": 1, "next_cursor": None}

        def capture_frame(self, capture, *, level, cursor, limit, name=None, timeout_s=None):  # type: ignore[no-untyped-def]
            assert capture == fence() and level == 0 and cursor == 0 and limit == 20
            self.calls.append("frame")
            return {"frame": {"level": 0, "line": 53}, "variables": (), "total": 0, "next_cursor": None}

        def close(self) -> None:
            pass

    class Factory:
        def start(self, *, mode: CapabilityMode) -> object:
            raise AssertionError("inspection must not start a runtime")

    runtime = Runtime()
    service = AgentWorkspaceService(tmp_path, Factory(), maximum_mode=CapabilityMode.EXPERIMENT)
    service._runtime = _AdmittedRuntime(runtime, runtime.runtime_id, 1, CapabilityMode.EXPERIMENT)  # type: ignore[arg-type]
    service._selected["default"] = runtime.runtime_id
    service._capture.activate_capture(fence(), service._proxy_registry)
    request = {"fence": {
        "capture_intent_id": "capture-intent", "operation_id": "op-capture",
        "source_revision": 7, "source_sha256": "a" * 64,
        "capture_generation": 1, "stop_sequence": 1,
    }}
    try:
        assert runtime.calls == []
        stack = service.call("capture.stack", request)
        assert stack.ok and stack.value["frames"][0]["line"] == 53
        assert runtime.calls == ["stack"]

        frame = service.call("capture.frame", {**request, "level": 0})
        assert frame.ok and frame.value["total"] == 0
        assert runtime.calls == ["stack", "frame"]

        invalid = service.call("capture.frame", {**request, "level": 0, "name": "Rows.Очистить()"})
        assert not invalid.ok
        assert runtime.calls == ["stack", "frame"]

        service._capture.invalidate_capture(service._proxy_registry)
        stale = service.call("capture.stack", request)
        assert not stale.ok
        assert runtime.calls == ["stack", "frame"]
    finally:
        service.close()
