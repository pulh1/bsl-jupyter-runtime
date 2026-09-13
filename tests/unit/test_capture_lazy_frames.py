from __future__ import annotations

import asyncio
from threading import RLock
from types import SimpleNamespace
from uuid import UUID, uuid4

from mcp.client import Client
import pytest

from onec_runtime.errors import ProtocolError
from onec_runtime.session import RuntimeSession
from onec_runtime_mcp.agent.capture_contracts import CaptureFence
from onec_runtime_mcp.agent.contracts import ServiceResponse
from onec_runtime_mcp.agent.mcp_profiles import McpProfile
from onec_runtime_mcp.agent.runtime_backend import OnecRuntimeBackend
from onec_runtime_mcp.agent.runtime_session import AgentRuntimeSession
from onec_runtime.prototype_runtime import OperationState, PrototypeRuntimeController
from onec_runtime.rdbg.models import (
    FrameVariable,
    LocalVariablesResult,
    ModuleLocation,
    StackFrame,
    TargetId,
)
from onec_runtime.runtime_api import PrototypeRuntimeApi
from onec_runtime_mcp.server import create_mcp_server


TARGET = TargetId(UUID("22222222-2222-2222-2222-222222222222"), "DefAlias")
OBJECT = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
PROPERTY = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
BUSINESS = ModuleLocation("ConfigModule", "", OBJECT, PROPERTY, 53)
CALLER = ModuleLocation("ConfigModule", "", OBJECT, PROPERTY, 17)
KERNEL = ModuleLocation("ExtensionModule", "", OBJECT, PROPERTY, 60, "OnecInteractiveRuntime")


class PausedRdbg:
    def __init__(self) -> None:
        self.target = SimpleNamespace(target_id=TARGET)
        self.calls: list[tuple[str, object]] = []
        self.root_variables = (FrameVariable("Local", "Строка", '"updated"'),)

    def local_variables(
        self, stack_level: int, *, timeout_s: float, max_text_size: int,
    ) -> LocalVariablesResult:
        self.calls.append(("local_variables", stack_level))
        assert timeout_s > 0
        assert max_text_size == 512
        if stack_level == 0:
            return LocalVariablesResult(uuid4(), self.root_variables)
        assert stack_level == 1
        return LocalVariablesResult(
            uuid4(),
            (
                FrameVariable("Rows", "Массив", "Массив", collection_size=4),
                FrameVariable("Count", "Число", "4"),
            ),
        )

def paused_api() -> tuple[PrototypeRuntimeApi, PausedRdbg]:
    rdbg = PausedRdbg()
    controller = PrototypeRuntimeController(rdbg, KERNEL)  # type: ignore[arg-type]
    controller.state = OperationState.CAPTURED
    controller.capture_frame_stack_level = 0
    controller._capture_frame_variables = (
        FrameVariable("Local", "Строка", '"secret"'),
    )
    controller._capture_stack_frames = (
        StackFrame(TARGET, 0, BUSINESS),
        StackFrame(TARGET, 1, CALLER),
    )
    controller._capture_target_id = TARGET
    return PrototypeRuntimeApi(controller), rdbg


def test_stack_only_exposes_existing_locations_without_reading_frame_values() -> None:
    api, rdbg = paused_api()

    result = api.capture_stack(cursor=0, limit=1)

    assert result["total"] == 2
    assert result["next_cursor"] == 1
    assert result["frames"][0]["level"] == 0
    assert result["frames"][0]["line"] == 53
    assert rdbg.calls == []


def test_current_capture_frame_reuses_mandatory_local_metadata() -> None:
    api, rdbg = paused_api()

    result = api.capture_frame(level=0, cursor=0, limit=20)

    assert result["variables"] == ({"name": "Local", "type_name": "Строка"},)
    assert rdbg.calls == []


def test_frame_page_reads_only_requested_level_and_keeps_values_hidden() -> None:
    api, rdbg = paused_api()

    result = api.capture_frame(level=1, cursor=0, limit=20)

    assert result["frame"]["level"] == 1
    assert result["variables"] == (
        {"name": "Rows", "type_name": "Массив"},
        {"name": "Count", "type_name": "Число"},
    )
    assert rdbg.calls == [("local_variables", 1)]


def test_named_frame_variable_uses_only_requested_local_metadata_for_size() -> None:
    api, rdbg = paused_api()

    result = api.capture_frame(level=1, cursor=0, limit=20, name="rows")

    assert result["variables"] == (
        {"name": "Rows", "type_name": "Массив", "presentation": "Массив", "collection_size": 4},
    )
    assert rdbg.calls == [("local_variables", 1)]


def test_named_capture_frame_refreshes_current_local_presentation() -> None:
    api, rdbg = paused_api()

    result = api.capture_frame(level=0, cursor=0, limit=20, name="local")

    assert result["variables"] == ({
        "name": "Local", "type_name": "Строка",
        "presentation": '"updated"', "collection_size": None,
    },)
    assert rdbg.calls == [("local_variables", 0)]


def test_named_capture_frame_refreshes_collection_size_after_mutation() -> None:
    api, rdbg = paused_api()
    api._controller._capture_frame_variables = (
        FrameVariable("Rows", "Массив", "Массив", collection_size=1),
    )
    rdbg.root_variables = (FrameVariable("Rows", "Массив", "Массив", collection_size=2),)

    first = api.capture_frame(level=0, cursor=0, limit=20, name="Rows")
    rdbg.root_variables = (FrameVariable("Rows", "Массив", "Массив", collection_size=3),)
    second = api.capture_frame(level=0, cursor=0, limit=20, name="Rows")

    assert first["variables"][0]["collection_size"] == 2
    assert second["variables"][0]["collection_size"] == 3
    assert rdbg.calls == [("local_variables", 0), ("local_variables", 0)]


def test_stack_redacts_kernel_identity_and_all_raw_urls() -> None:
    api, rdbg = paused_api()
    private_path = "file:///C:/server/private/runtime.bsl?session=secret"
    api._controller._capture_stack_frames = (
        StackFrame(TARGET, 0, ModuleLocation("ConfigModule", private_path, OBJECT, PROPERTY, 53)),
        StackFrame(TARGET, 1, ModuleLocation("ExtensionModule", private_path, OBJECT, PROPERTY, 60, "OnecInteractiveRuntime")),
    )
    api._controller.kernel_location = api._controller._capture_stack_frames[1].location

    frames = api.capture_stack(cursor=0, limit=20)["frames"]

    assert frames[0]["module_type"] == "ConfigModule"
    assert "url" not in frames[0]
    assert frames[1] == {
        "level": 1, "runtime_kernel": True, "module_type": None,
        "object_id": None, "property_id": None, "line": None,
        "extension_name": None,
    }
    assert "secret" not in str(frames)
    assert rdbg.calls == []


def test_unknown_name_does_not_evaluate_an_expression() -> None:
    api, rdbg = paused_api()

    with pytest.raises(ProtocolError, match="variable"):
        api.capture_frame(level=1, cursor=0, limit=20, name="Rows.Очистить()")

    assert rdbg.calls == []


def test_runtime_kernel_frame_variables_are_not_exposed() -> None:
    api, rdbg = paused_api()
    api._controller._capture_stack_frames += (StackFrame(TARGET, 2, KERNEL),)

    with pytest.raises(ProtocolError, match="kernel"):
        api.capture_frame(level=2, cursor=0, limit=20)

    assert rdbg.calls == []


def test_kernel_location_is_hidden_even_when_it_is_capture_level_zero() -> None:
    api, rdbg = paused_api()
    api._controller.kernel_location = BUSINESS

    stack = api.capture_stack(cursor=0, limit=20)

    assert stack["frames"][0] == {
        "level": 0, "runtime_kernel": True, "module_type": None,
        "object_id": None, "property_id": None, "line": None,
        "extension_name": None,
    }
    with pytest.raises(ProtocolError, match="kernel"):
        api.capture_frame(level=0, cursor=0, limit=20)
    assert rdbg.calls == []


def test_quarantine_revokes_stack_and_frame_reads_without_resuming() -> None:
    api, rdbg = paused_api()
    api.invalidate_capture_inspection()

    with pytest.raises(ProtocolError, match="quarantined"):
        api.capture_stack(cursor=0, limit=20)
    with pytest.raises(ProtocolError, match="quarantined"):
        api.capture_frame(level=0, cursor=0, limit=20)
    assert rdbg.calls == []


def test_runtime_backend_rechecks_exact_capture_ticket_before_stack_read() -> None:
    api, rdbg = paused_api()
    core = object.__new__(RuntimeSession)
    core._operation_lock = RLock()
    core._active_capture_ticket = SimpleNamespace(
        capture_intent_id="intent", operation_id="operation",
        source_revision=1, source_sha256="a" * 64,
        capture_generation=1, stop_sequence=1,
    )
    core.runtime_api = api
    backend = OnecRuntimeBackend("runtime", AgentRuntimeSession(core))
    current = CaptureFence("intent", "operation", 1, "a" * 64, 1, 1)
    stale = CaptureFence("intent", "operation", 1, "a" * 64, 1, 2)

    assert backend.capture_stack(current, cursor=0, limit=20)["total"] == 2
    with pytest.raises(ProtocolError, match="stale"):
        backend.capture_frame(stale, level=0, cursor=0, limit=20)
    assert rdbg.calls == []


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
