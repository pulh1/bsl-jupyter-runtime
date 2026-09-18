"""Saved CAPTURE frames are lazy and stay fenced through public adapters."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from threading import RLock
from types import SimpleNamespace
from uuid import UUID

from mcp.client import Client
import pytest

from onec_runtime.errors import ProtocolError, StaleCaptureError
from onec_runtime.execution.capture.inspection import (
    TypedNativeVariable, TypedNativeVariablePage,
)
from onec_runtime.execution.capture.public_inspection import CaptureInspectionBridge
from onec_runtime.execution.capture.scope import CaptureScope
from onec_runtime.execution.capture.session_inspection_adapter import (
    SessionCaptureInspectionAdapter,
)
from onec_runtime.rdbg.models import (
    FrameVariable, ModuleLocation, StackFrame, StopEvent, TargetId,
)
from onec_runtime.session import RuntimeSession
from onec_runtime_mcp.agent.capture_contracts import CaptureFence
from onec_runtime_mcp.agent.contracts import ServiceResponse
from onec_runtime_mcp.agent.mcp_profiles import McpProfile
from onec_runtime_mcp.agent.runtime_backend import OnecRuntimeBackend
from onec_runtime_mcp.agent.runtime_session import AgentRuntimeSession
from onec_runtime_mcp.server import create_mcp_server


TARGET = TargetId(UUID("22222222-2222-2222-2222-222222222222"), "DefAlias")
OBJECT = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
PROPERTY = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
BUSINESS = ModuleLocation("ConfigModule", "file:///private/business.bsl", OBJECT, PROPERTY, 53)
CALLER = ModuleLocation("ConfigModule", "file:///private/caller.bsl", OBJECT, PROPERTY, 17)
KERNEL = ModuleLocation("ExtensionModule", "file:///private/kernel.bsl", OBJECT, PROPERTY, 60, "OnecInteractiveRuntime")


class Ticket:
    def __init__(self, result: object) -> None:
        self.result = result

    def wait_initiator(self, timeout: float | None = None) -> object:
        return self.result


class PausedOwner:
    def __init__(self, scope: CaptureScope) -> None:
        self.capture_scope = scope
        self.calls: list[tuple[object, ...]] = []
        self.variables = {
            0: (FrameVariable("Local", "Строка", '"updated"'),),
            1: (
                FrameVariable("Rows", "Массив", "private rows", collection_size=4),
                FrameVariable("Count", "Число", "private 4"),
            ),
        }

    def submit_capture_variable(self, name: str, *, stack_level: int = 0) -> Ticket:
        self.calls.append(("variable", name, stack_level))
        matches = tuple(
            value for value in self.variables[stack_level]
            if value.name.casefold() == name.casefold()
        )
        if len(matches) != 1:
            raise ProtocolError("capture variable is unavailable")
        return Ticket(matches[0])

    def submit_capture_typed_variable_page(
        self, *, stack_level: int, start: int, stop: int,
    ) -> Ticket:
        self.calls.append(("typed_page", stack_level, start, stop))
        variables = self.variables[stack_level]
        selected = variables[start:stop]
        return Ticket(TypedNativeVariablePage(
            tuple(TypedNativeVariable(v.name, v.type_name, v.collection_size) for v in selected),
            len(variables),
            stop if selected and stop < len(variables) else None,
        ))


def paused_adapter() -> tuple[SessionCaptureInspectionAdapter, PausedOwner, CaptureScope]:
    frames = (
        StackFrame(TARGET, 0, BUSINESS),
        StackFrame(TARGET, 1, CALLER),
        StackFrame(TARGET, 2, KERNEL),
    )
    stop = StopEvent(
        TARGET, BUSINESS, "callStackFormed",
        stack=(BUSINESS, CALLER, KERNEL), stack_frames=frames,
    )
    scope = CaptureScope.from_stop(1, 1, stop, 1)
    scope.record_locals((FrameVariable("Local", "Строка", '"secret"'),))
    scope.record_transfer("temporary-address")
    scope.record_kernel_frame(2)
    assert scope.record_main_command(1)
    scope.record_context_begun()
    scope.mark_ready()
    owner = PausedOwner(scope)
    return (
        SessionCaptureInspectionAdapter(owner, CaptureInspectionBridge(owner)),
        owner,
        scope,
    )


def test_saved_stack_and_root_metadata_need_no_remote_ticket() -> None:
    adapter, owner, _scope = paused_adapter()

    stack = adapter.capture_stack(cursor=0, limit=1)
    root = adapter.capture_frame(level=0, cursor=0, limit=20)

    assert stack["total"] == 3
    assert stack["next_cursor"] == 1
    assert stack["frames"][0]["line"] == 53
    assert root["variables"] == ({"name": "Local", "type_name": "Строка"},)
    assert owner.calls == []
    assert "secret" not in str(root)


def test_nonroot_and_named_reads_use_only_requested_frame_ticket() -> None:
    adapter, owner, _scope = paused_adapter()

    page = adapter.capture_frame(level=1, cursor=0, limit=20)
    named = adapter.capture_frame(level=1, cursor=0, limit=20, name="rows")

    assert page["variables"] == (
        {"name": "Rows", "type_name": "Массив", "collection_size": 4},
        {"name": "Count", "type_name": "Число"},
    )
    assert named["variables"] == (
        {"name": "Rows", "type_name": "Массив", "collection_size": 4},
    )
    assert owner.calls == [
        ("typed_page", 1, 0, 20),
        ("variable", "rows", 1),
    ]
    assert "private rows" not in str(page) + str(named)


def test_named_collection_size_is_refreshed_from_each_ticket() -> None:
    adapter, owner, _scope = paused_adapter()

    first = adapter.capture_frame(level=1, cursor=0, limit=20, name="Rows")
    owner.variables[1] = (
        FrameVariable("Rows", "Массив", "private rows", collection_size=5),
    )
    second = adapter.capture_frame(level=1, cursor=0, limit=20, name="Rows")

    assert first["variables"][0]["collection_size"] == 4
    assert second["variables"][0]["collection_size"] == 5
    assert owner.calls == [("variable", "Rows", 1)] * 2
    assert "private rows" not in str(first) + str(second)


def test_named_root_variable_uses_fresh_ticket_but_hides_presentation() -> None:
    adapter, owner, _scope = paused_adapter()

    result = adapter.capture_frame(level=0, cursor=0, limit=20, name="local")

    assert result["variables"] == ({"name": "Local", "type_name": "Строка"},)
    assert owner.calls == [("variable", "local", 0)]
    assert "updated" not in str(result)
    assert "secret" not in str(result)


def test_kernel_coordinates_and_raw_urls_are_hidden_without_ticket() -> None:
    adapter, owner, _scope = paused_adapter()

    stack = adapter.capture_stack(cursor=0, limit=10)

    assert stack["frames"][2] == {
        "level": 2, "runtime_kernel": True, "module_type": None,
        "object_id": None, "property_id": None, "line": None,
        "extension_name": None,
    }
    assert "file:///" not in str(stack)
    with pytest.raises(ProtocolError, match="kernel"):
        adapter.capture_frame(level=2, cursor=0, limit=20)
    with pytest.raises(ProtocolError, match="variable name"):
        adapter.capture_frame(level=1, cursor=0, limit=20, name="Rows.Очистить()")
    assert owner.calls == []


def test_root_frame_is_masked_when_its_module_is_the_runtime_kernel() -> None:
    adapter, owner, scope = paused_adapter()
    frames = (*scope.stack_frames[:2], StackFrame(TARGET, 2, BUSINESS))
    scope.stop = replace(scope.stop, stack_frames=frames)
    scope.stack_frames = frames

    stack = adapter.capture_stack(cursor=0, limit=3)

    assert stack["frames"][0]["runtime_kernel"] is True
    assert stack["frames"][0]["line"] is None
    with pytest.raises(ProtocolError, match="kernel"):
        adapter.capture_frame(level=0, cursor=0, limit=20)
    assert owner.calls == []


def test_invalidated_scope_revokes_stack_and_frame_reads() -> None:
    adapter, owner, scope = paused_adapter()
    scope.invalidate_inspection()

    with pytest.raises(StaleCaptureError):
        adapter.capture_stack(cursor=0, limit=20)
    with pytest.raises(StaleCaptureError):
        adapter.capture_frame(level=0, cursor=0, limit=20)
    assert owner.calls == []


def test_runtime_backend_rechecks_exact_capture_ticket_before_stack_read() -> None:
    adapter, owner, _scope = paused_adapter()
    core = object.__new__(RuntimeSession)
    core._operation_lock = RLock()
    core._active_capture_ticket = SimpleNamespace(
        capture_intent_id="intent", operation_id="operation",
        source_revision=1, source_sha256="a" * 64,
        capture_generation=1, stop_sequence=1,
    )
    core.runtime_api = adapter
    backend = OnecRuntimeBackend("runtime", AgentRuntimeSession(core))
    current = CaptureFence("intent", "operation", 1, "a" * 64, 1, 1)
    stale = CaptureFence("intent", "operation", 1, "a" * 64, 1, 2)

    assert backend.capture_stack(current, cursor=0, limit=20)["total"] == 3
    with pytest.raises(ProtocolError, match="stale"):
        backend.capture_frame(stale, level=0, cursor=0, limit=20)
    assert owner.calls == []


def test_capture_mcp_exposes_stack_and_frame_only_on_explicit_calls() -> None:
    fence = {
        "capture_intent_id": "intent", "operation_id": "operation",
        "source_revision": 1, "source_sha256": "a" * 64,
        "capture_generation": 1, "stop_sequence": 1,
    }
    frame = {
        "level": 0, "runtime_kernel": False, "module_type": "ConfigModule",
        "object_id": str(OBJECT), "property_id": str(PROPERTY),
        "line": 53, "extension_name": "",
    }
    kernel_frame = {
        "level": 1, "runtime_kernel": True, "module_type": None,
        "object_id": None, "property_id": None, "line": None,
        "extension_name": None,
    }

    class ClientBackend:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def call(self, method: str, arguments: dict[str, object]) -> ServiceResponse:
            self.calls.append((method, arguments))
            if method == "capture.stack":
                return ServiceResponse.success({
                    "frames": (frame, kernel_frame), "total": 2, "next_cursor": None,
                })
            if method == "capture.frame":
                variable = {"name": "Rows", "type_name": "Массив"}
                if arguments.get("name") == "Rows":
                    variable = {
                        **variable, "presentation": "Массив",
                        "collection_size": 4,
                    }
                return ServiceResponse.success({
                    "frame": frame,
                    "variables": (variable,),
                    "total": 1, "next_cursor": None,
                })
            raise AssertionError(method)

    backend = ClientBackend()

    async def scenario() -> tuple[set[str], object, object, object]:
        async with Client(create_mcp_server(backend, profile=McpProfile.CAPTURE)) as client:
            names = {item.name for item in (await client.list_tools()).tools}
            assert backend.calls == []
            stack = await client.call_tool("capture.stack", {"fence": fence})
            frame_result = await client.call_tool("capture.frame", {"fence": fence, "level": 0})
            named = await client.call_tool("capture.frame", {"fence": fence, "level": 0, "name": "Rows"})
            return names, stack, frame_result, named

    names, stack, frame_result, named = asyncio.run(scenario())

    assert {"capture.stack", "capture.frame"} <= names
    assert stack.structured_content["value"]["frames"][0]["line"] == 53
    assert stack.structured_content["value"]["frames"][1]["runtime_kernel"] is True
    assert stack.structured_content["value"]["frames"][1]["line"] is None
    assert frame_result.structured_content["value"]["variables"][0]["name"] == "Rows"
    assert named.structured_content["value"]["variables"][0]["collection_size"] == 4
    assert [method for method, _ in backend.calls] == ["capture.stack", "capture.frame", "capture.frame"]
    assert backend.calls[-1][1]["name"] == "Rows"
