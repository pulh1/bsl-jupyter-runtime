from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import nbformat
import pytest
from mcp.client import Client

from onec_runtime_mcp.agent.contracts import (
    AgentOperationState,
    CodeLanguage,
    FailureCategory,
    MethodFailure,
    RetrySafety,
    ServiceResponse,
    StateChanged,
)
from onec_runtime_mcp.server import create_mcp_server
from onec_runtime_mcp.agent.mcp_profiles import McpProfile
from onec_runtime_mcp.agent.service import AgentWorkspaceService


EXPECTED_SLICE_ONE_TOOLS = {
    "workspace.open", "workspace.status", "workspace.capabilities", "workspace.close",
    "workspace.variables", "workspace.variable", "workspace.variable_history",
    "workspace.snapshot_variables", "workspace.delete_variables",
    "runtime.start", "runtime.ensure", "runtime.list", "runtime.select", "runtime.status",
    "runtime.request_mode", "runtime.restart", "runtime.close",
    "code.list", "code.get", "code.put", "code.diff", "code.history", "code.run",
    "code.run_inline", "code.promote", "code.delete",
    "operation.list", "operation.status", "operation.wait", "operation.output",
    "operation.result", "operation.stop_waiting", "operation.abort_generation",
    "operation.explain_failure",
    "value.inspect", "value.describe", "value.size", "value.preview", "value.get", "value.select",
    "value.snapshot", "value.materialize", "value.to_df", "value.compare", "value.release",
    "python.variables", "python.run", "python.inspect", "python.imports", "python.reset", "python.status",
}

EXPECTED_TOOL_ANNOTATIONS = {
    "workspace.open": (True, False, True),
    "workspace.status": (True, False, True),
    "workspace.capabilities": (True, False, True),
    "workspace.close": (False, True, False),
    "workspace.variables": (False, False, True),
    "workspace.variable": (False, False, True),
    "workspace.variable_history": (False, False, True),
    "workspace.snapshot_variables": (False, False, False),
    "workspace.delete_variables": (False, True, False),
    "runtime.start": (False, False, False),
    "runtime.ensure": (False, False, True),
    "runtime.list": (True, False, True),
    "runtime.select": (False, False, True),
    "runtime.status": (True, False, True),
    "runtime.request_mode": (True, False, True),
    "runtime.restart": (False, True, False),
    "runtime.close": (False, True, False),
    "code.list": (False, False, True),
    "code.get": (True, False, True),
    "code.put": (False, True, False),
    "code.diff": (True, False, True),
    "code.history": (True, False, True),
    "code.run": (False, True, False),
    "code.run_inline": (False, True, False),
    "code.promote": (False, True, False),
    "code.delete": (False, True, False),
    "operation.list": (True, False, True),
    "operation.status": (True, False, True),
    "operation.wait": (True, False, True),
    "operation.output": (True, False, True),
    "operation.result": (True, False, True),
    "operation.stop_waiting": (False, False, True),
    "operation.abort_generation": (False, True, False),
    "operation.explain_failure": (True, False, True),
    "value.describe": (True, False, True),
    "value.inspect": (True, False, True),
    "value.size": (True, False, True),
    "value.preview": (True, False, True),
    "value.get": (False, False, False),
    "value.select": (False, False, False),
    "value.snapshot": (False, False, False),
    "value.materialize": (False, False, False),
    "value.to_df": (False, False, False),
    "value.compare": (True, False, True),
    "value.release": (False, True, True),
    "python.variables": (True, False, True),
    "python.run": (False, False, False),
    "python.inspect": (True, False, True),
    "python.imports": (False, False, True),
    "python.reset": (False, True, False),
    "python.status": (False, False, True),
}

EXPECTED_TOOL_DESCRIPTIONS = {
    "workspace.open": "Open and select a project workspace and its notebook code store.",
    "workspace.status": "Return the current workspace selection, runtime summary, and service state.",
    "workspace.capabilities": "Return the API version and capabilities actually available from this service.",
    "workspace.close": "Detach this caller or abort the current runtime generation according to policy.",
    "workspace.variables": "List current user BSL and/or Python variables as generation-fenced proxies with origin-cell provenance.",
    "workspace.variable": "Resolve one exact qualified bsl.* or python.* variable without silently resolving name collisions.",
    "workspace.variable_history": "List version and derivation history for one qualified variable binding.",
    "workspace.snapshot_variables": "Create bounded immutable Python snapshots for selected BSL variables.",
    "workspace.delete_variables": "Delete selected Python bindings only when all expected versions match.",
    "runtime.start": "Start a new supervised 1C runtime; fail if one already exists.",
    "runtime.ensure": "Select a suitable existing 1C runtime or start one; this is the normal agent entrypoint.",
    "runtime.list": "List runtimes available in the current workspace.",
    "runtime.select": "Select an existing runtime for this authenticated caller.",
    "runtime.status": "Return runtime lifecycle, generation, active operation, mode, and health.",
    "runtime.request_mode": "Check whether a capability mode is allowed without exceeding the server maximum.",
    "runtime.restart": "Abort the current generation and start a new one, invalidating its 1C values.",
    "runtime.close": "Explicitly abort and close the selected runtime generation.",
    "code.list": "Select a notebook container, list its cells, and record immutable revision history.",
    "code.get": "Read one exact cell revision with source, metadata, hashes, and provenance.",
    "code.put": "Save a new cell revision using required revision and document-hash conflict fences.",
    "code.diff": "Return a bounded diff between two immutable revisions of a cell.",
    "code.history": "List immutable cell revisions and their recorded executions.",
    "code.run": "Execute one exact saved revision fenced by revision number and source hash.",
    "code.run_inline": "Execute unsaved code as an immutable inline operation without changing the notebook.",
    "code.promote": "Persist the exact visible source of an inline operation as a notebook cell revision.",
    "code.delete": "Delete a saved cell using required revision and document-hash conflict fences.",
    "operation.list": "List active and recent durable operations, optionally filtered by runtime or state.",
    "operation.status": "Return the authoritative current state and event cursor of an operation.",
    "operation.wait": "Wait for operation or event progress without starting another execution.",
    "operation.output": "Read ordered messages after a cursor with an explicit bounded message limit.",
    "operation.result": "Return result metadata when supported; raw 1C values are never returned.",
    "operation.stop_waiting": "Stop only this caller's wait; this does not stop code running in 1C.",
    "operation.abort_generation": "Explicitly terminate the entire runtime generation that owns an operation.",
    "operation.explain_failure": "Return a normalized failure cause and safe recovery actions for an operation.",
    "value.describe": "Return proxy type, lifetime, generation fences, provenance, version and supported actions.",
    "value.inspect": "Inspect proxy metadata and list explicit next actions without hidden scans; costly details require a server budget profile.",
    "value.size": "Measure bounded value dimensions and report accuracy and measurement cost.",
    "value.preview": "Return a bounded wire-safe preview without transferring the complete value.",
    "value.get": "Create a proxy for one named or indexed child without materializing the parent.",
    "value.select": "Create a bounded projected proxy for selected fields or columns.",
    "value.snapshot": "Create a bounded Python snapshot proxy while retaining source provenance.",
    "value.materialize": "Materialize a bounded 1C value into a native child-owned Python object proxy.",
    "value.to_df": "Materialize a bounded tabular 1C value into a child-owned pandas DataFrame proxy.",
    "value.compare": "Compare two same-realm proxies under an explicit bounded policy.",
    "value.release": "Release a proxy handle without deleting its named notebook binding.",
    "python.variables": "List all user-visible Python outputs; imports and intermediate locals remain private.",
    "python.run": "Run Python with proxy inputs and publish only explicitly declared output names.",
    "python.inspect": "Return bounded Python type, shape, dtype, memory and scalar-preview metadata.",
    "python.imports": "List allowlisted Python packages and their installed versions.",
    "python.reset": "Clear the managed Python namespace and invalidate its current proxy generation.",
    "python.status": "Return managed Python generation, health, variable count and resource-limit guarantees.",
}


class FakeServiceClient:
    """A narrow transport fake; MCP behavior stays real and in-process."""

    def __init__(self) -> None:
        self.next_response = ServiceResponse.success({"answer": "ok"})
        self.calls: list[tuple[str, dict[str, object]]] = []

    def call(self, method: str, arguments: dict[str, object]) -> ServiceResponse:
        self.calls.append((method, arguments))
        return self.next_response


@pytest.fixture
def fake_service_client() -> FakeServiceClient:
    return FakeServiceClient()


def test_mcp_lists_only_approved_slice_one_tools(fake_service_client: FakeServiceClient) -> None:
    async def scenario() -> tuple[object, ...]:
        async with Client(create_mcp_server(fake_service_client, profile=McpProfile.EXPERT)) as client:
            return (await client.list_tools()).tools

    tools = asyncio.run(scenario())
    assert {tool.name for tool in tools} == EXPECTED_SLICE_ONE_TOOLS
    assert "capture.run" not in {tool.name for tool in tools}


def test_mcp_forwards_exact_structured_failure(fake_service_client: FakeServiceClient) -> None:
    fake_service_client.next_response = ServiceResponse.fail(
        MethodFailure(
            category=FailureCategory.CONFLICT,
            state_changed=StateChanged.NO,
            safe_to_retry=RetrySafety.AFTER_STATUS_CHECK,
            current_state={"revision": 4},
            diagnostic_id="diag-conflict",
        )
    )

    async def scenario() -> object:
        async with Client(create_mcp_server(fake_service_client, profile=McpProfile.EXPERT)) as client:
            return await client.call_tool(
                "code.put",
                {
                    "cell_id": "cell-main",
                    "source": "Ответ = 2;",
                    "language": "bsl",
                    "mode": "main",
                    "expected_revision": 1,
                    "expected_document_sha256": "a" * 64,
                },
            )

    result = asyncio.run(scenario())
    assert result.structured_content == {
        "ok": False,
        "value": None,
        "failure": {
            "category": "conflict",
            "state_changed": "no",
            "safe_to_retry": "after_status_check",
            "current_state": {"revision": 4},
            "operation_id": None,
            "affected_proxies": [],
            "partial_results": {},
            "event_cursor": None,
            "recommended_actions": [],
            "diagnostic_id": "diag-conflict",
        },
    }
    assert fake_service_client.calls == [
        (
            "code.put",
            {
                "cell_id": "cell-main",
                "source": "Ответ = 2;",
                "language": "bsl",
                "mode": "main",
                "expected_revision": 1,
                "expected_document_sha256": "a" * 64,
            },
        )
    ]


def _expert_failure_value() -> dict[str, object]:
    diagnostic_id = "d" * 64
    public = {
        "diagnostic_id": diagnostic_id,
        "stage": "execution",
        "mapping_confidence": "exact",
        "visible_location": {
            "line": 3,
            "column": 1,
            "span": {"start": 20, "end": 21},
        },
        "related_visible_span": None,
        "excerpt": None,
        "synthetic_region": None,
    }
    return {
        "operation_id": "operation-failed",
        "category": "platform_failure",
        "state": "failed",
        "state_changed": "no",
        "safe_to_retry": "after_status_check",
        "cell_id": "cell-main",
        "revision": 7,
        "source_sha256": "a" * 64,
        "recommended_actions": ["runtime.status", "code.get", "runtime.restart"],
        "diagnostic": public,
        "diagnostic_details": {
            **public,
            "runtime_summary": "BSL execution failed",
            "excerpt": "О",
            "lowered_location": {
                "line": 5,
                "column": 1,
                "offset": 40,
                "span": {"start": 40, "end": 41},
            },
            "platform_diagnostic": "{<Неизвестный модуль>(5,1)}: pid=<redacted>",
            "platform_diagnostic_sha256": "b" * 64,
            "platform_diagnostic_truncated": False,
            "platform_diagnostic_redacted": True,
            "execution_artifact_sha256": "c" * 64,
            "source_map_sha256": "e" * 64,
            "worker_generation": 4,
            "worker_manifest_sha256": "f" * 64,
        },
    }


def test_expert_failure_tool_returns_only_the_bounded_diagnostic_allowlist(
    fake_service_client: FakeServiceClient,
) -> None:
    """Break caught: the expert MCP route has no strict output boundary."""
    fake_service_client.next_response = ServiceResponse.success(
        _expert_failure_value()
    )

    async def scenario() -> object:
        async with Client(
            create_mcp_server(fake_service_client, profile=McpProfile.EXPERT)
        ) as client:
            return await client.call_tool(
                "operation.explain_failure",
                {"operation_id": "operation-failed"},
            )

    result = asyncio.run(scenario())

    assert result.is_error is False
    assert result.structured_content == {
        "ok": True,
        "value": _expert_failure_value(),
        "failure": None,
    }


def test_expert_failure_tool_fails_closed_on_private_output_widening(
    fake_service_client: FakeServiceClient,
) -> None:
    """Break caught: generated source/private admission data reaches MCP output."""
    value = _expert_failure_value()
    details = value["diagnostic_details"]
    assert isinstance(details, dict)
    details["generated_source"] = "FULL_GENERATED_SOURCE token=private"
    details["worker_admission"] = {"pid": 9182}
    fake_service_client.next_response = ServiceResponse.success(value)

    async def scenario() -> object:
        async with Client(
            create_mcp_server(fake_service_client, profile=McpProfile.EXPERT)
        ) as client:
            return await client.call_tool(
                "operation.explain_failure",
                {"operation_id": "operation-failed"},
            )

    result = asyncio.run(scenario())
    encoded = json.dumps(result.structured_content, ensure_ascii=False)

    assert result.structured_content["ok"] is False
    assert "FULL_GENERATED_SOURCE" not in encoded
    assert "worker_admission" not in encoded
    assert "9182" not in encoded


def test_expert_failure_tool_preserves_unredacted_runtime_identity_text(
    fake_service_client: FakeServiceClient,
) -> None:
    """Break caught: expert diagnostics redact platform-emitted text."""
    value = _expert_failure_value()
    details = value["diagnostic_details"]
    assert isinstance(details, dict)
    details["platform_diagnostic"] = (
        "{<Неизвестный модуль>(5,1)}: rdbg_pid=9182 token=private-connection"
    )
    details["platform_diagnostic_redacted"] = False
    fake_service_client.next_response = ServiceResponse.success(value)

    async def scenario() -> object:
        async with Client(
            create_mcp_server(fake_service_client, profile=McpProfile.EXPERT)
        ) as client:
            return await client.call_tool(
                "operation.explain_failure",
                {"operation_id": "operation-failed"},
            )

    result = asyncio.run(scenario())
    encoded = json.dumps(result.structured_content, ensure_ascii=False)

    assert result.structured_content["ok"] is True
    assert "9182" in encoded
    assert "private-connection" in encoded


def test_expert_failure_tool_accepts_64kib_utf8_unicode_diagnostic(
    fake_service_client: FakeServiceClient,
) -> None:
    """Break caught: expert MCP validation treats its 64 KiB cap as characters."""
    value = _expert_failure_value()
    details = value["diagnostic_details"]
    assert isinstance(details, dict)
    platform_diagnostic = "😀" * (64 * 1024 // len("😀".encode("utf-8")))
    details["platform_diagnostic"] = platform_diagnostic
    details["platform_diagnostic_truncated"] = True
    details["platform_diagnostic_redacted"] = False
    fake_service_client.next_response = ServiceResponse.success(value)

    async def scenario() -> object:
        async with Client(
            create_mcp_server(fake_service_client, profile=McpProfile.EXPERT)
        ) as client:
            return await client.call_tool(
                "operation.explain_failure",
                {"operation_id": "operation-failed"},
            )

    result = asyncio.run(scenario())
    diagnostic = result.structured_content["value"]["diagnostic_details"]

    assert result.structured_content["ok"] is True
    assert diagnostic["platform_diagnostic"] == platform_diagnostic
    assert len(platform_diagnostic.encode("utf-8")) == 64 * 1024


def test_expert_failure_tool_canonicalizes_untrusted_failed_response_channel(
    fake_service_client: FakeServiceClient,
) -> None:
    """Break caught: ok=false accepts an unrestricted private failure mapping."""
    fake_service_client.next_response = {  # type: ignore[assignment]
        "ok": False,
        "value": None,
        "failure": {
            "generated_source": "FULL LOWERED SOURCE",
            "worker_admission": {"process_id": 9182},
            "credentials": {"token": "private-failure-token"},
        },
    }

    async def scenario() -> object:
        async with Client(
            create_mcp_server(fake_service_client, profile=McpProfile.EXPERT)
        ) as client:
            return await client.call_tool(
                "operation.explain_failure",
                {"operation_id": "operation-failed"},
            )

    result = asyncio.run(scenario())
    encoded = json.dumps(result.structured_content, ensure_ascii=False)
    failure = result.structured_content["failure"]

    assert result.structured_content["ok"] is False
    assert set(failure) == {
        "category",
        "state_changed",
        "safe_to_retry",
        "current_state",
        "operation_id",
        "affected_proxies",
        "partial_results",
        "event_cursor",
        "recommended_actions",
        "diagnostic_id",
    }
    assert failure["category"] == "platform_failure"
    assert failure["current_state"] == {}
    assert "FULL LOWERED SOURCE" not in encoded
    assert "worker_admission" not in encoded
    assert "9182" not in encoded
    assert "private-failure-token" not in encoded


def test_mcp_tool_schemas_fence_mutations_and_bound_waits(fake_service_client: FakeServiceClient) -> None:
    async def scenario() -> tuple[object, ...]:
        async with Client(create_mcp_server(fake_service_client, profile=McpProfile.EXPERT)) as client:
            return (await client.list_tools()).tools

    by_name = {tool.name: tool for tool in asyncio.run(scenario())}
    for name in {"code.put", "code.promote", "code.delete"}:
        assert {"expected_revision", "expected_document_sha256"} <= set(by_name[name].input_schema["required"])
        assert by_name[name].annotations.read_only_hint is not True
    assert {"revision", "source_sha256"} <= set(by_name["code.run"].input_schema["required"])
    for name in {"code.run", "code.run_inline", "operation.wait"}:
        properties = by_name[name].input_schema["properties"]
        wait_name = "timeout_s" if name == "operation.wait" else "wait_s"
        assert properties[wait_name]["minimum"] == 0
        assert properties[wait_name]["maximum"] == 30


def test_mcp_tool_annotations_are_an_exact_service_semantics_matrix(fake_service_client: FakeServiceClient) -> None:
    async def scenario() -> tuple[object, ...]:
        async with Client(create_mcp_server(fake_service_client, profile=McpProfile.EXPERT)) as client:
            return (await client.list_tools()).tools

    tools = asyncio.run(scenario())
    observed = {
        tool.name: (
            tool.annotations.read_only_hint,
            tool.annotations.destructive_hint,
            tool.annotations.idempotent_hint,
        )
        for tool in tools
    }
    assert observed == EXPECTED_TOOL_ANNOTATIONS


def test_mcp_tools_publish_exact_agent_guidance(fake_service_client: FakeServiceClient) -> None:
    async def scenario() -> tuple[object, ...]:
        async with Client(create_mcp_server(fake_service_client, profile=McpProfile.EXPERT)) as client:
            return (await client.list_tools()).tools

    tools = asyncio.run(scenario())

    assert {tool.name: tool.description for tool in tools} == EXPECTED_TOOL_DESCRIPTIONS


def test_code_list_selects_notebook_and_writes_history_when_guarded(tmp_path: Path) -> None:
    source = "Ответ = 1;"
    cell = nbformat.v4.new_code_cell(source=source, id="cell-main")
    cell.metadata["onec_runtime"] = {
        "revision": 1,
        "language": "bsl",
        "mode": "main",
        "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
    }
    nbformat.write(nbformat.v4.new_notebook(cells=[cell]), tmp_path / "demo.ipynb")
    service = AgentWorkspaceService(tmp_path, object())
    if not service._store.guarded_mutations_available:
        pytest.skip("host cannot provide guarded immutable history")

    async def scenario() -> tuple[object, object]:
        async with Client(create_mcp_server(service, profile=McpProfile.EXPERT)) as client:
            result = await client.call_tool("code.list", {"container": "demo.ipynb"})
            tool = next(item for item in (await client.list_tools()).tools if item.name == "code.list")
        return result, tool

    result, tool = asyncio.run(scenario())
    assert result.is_error is False
    assert tool.annotations.read_only_hint is False
    assert list((tmp_path / ".runtime" / "agent-service" / "code").rglob("*.json"))


def test_mcp_enum_schemas_and_forwarding_follow_domain_enums(fake_service_client: FakeServiceClient) -> None:
    async def scenario() -> tuple[object, ...]:
        async with Client(create_mcp_server(fake_service_client, profile=McpProfile.EXPERT)) as client:
            tools = (await client.list_tools()).tools
            markdown = await client.call_tool("code.list", {"container": "demo.ipynb", "language": "markdown"})
            captured = await client.call_tool("operation.list", {"state": "captured"})
        return tools, markdown, captured

    tools, markdown, captured = asyncio.run(scenario())
    by_name = {tool.name: tool for tool in tools}
    language_schema = by_name["code.list"].input_schema
    state_schema = by_name["operation.list"].input_schema
    assert set(language_schema["$defs"]["CodeLanguage"]["enum"]) == {item.value for item in CodeLanguage}
    assert set(state_schema["$defs"]["AgentOperationState"]["enum"]) == {item.value for item in AgentOperationState}
    assert markdown.is_error is False
    assert captured.is_error is False
    assert fake_service_client.calls == [
        ("code.list", {"container": "demo.ipynb", "filters": {"language": "markdown"}}),
        ("operation.list", {"filters": {"state": "captured"}}),
    ]


def test_mcp_call_does_not_write_to_stdout(fake_service_client: FakeServiceClient, capsys: pytest.CaptureFixture[str]) -> None:
    async def scenario() -> None:
        async with Client(create_mcp_server(fake_service_client, profile=McpProfile.EXPERT)) as client:
            await client.call_tool("workspace.open", {"project": "C:/project"})

    asyncio.run(scenario())
    assert capsys.readouterr().out == ""


def test_mcp_does_not_add_a_source_length_limit_below_the_service_payload_limit(fake_service_client: FakeServiceClient) -> None:
    source = "А" * 5_000

    async def scenario() -> object:
        async with Client(create_mcp_server(fake_service_client, profile=McpProfile.EXPERT)) as client:
            return await client.call_tool("code.run_inline", {"language": "bsl", "mode": "main", "source": source})

    result = asyncio.run(scenario())
    assert result.is_error is False
    assert fake_service_client.calls == [
        ("code.run_inline", {"language": "bsl", "mode": "main", "source": source, "inputs": {}, "wait_s": 0}),
    ]


def test_mcp_rejects_duplicate_registered_names(fake_service_client: FakeServiceClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import onec_runtime_mcp.server as module

    monkeypatch.setattr(module, "_TOOL_NAMES", ("workspace.open", "workspace.open"))
    with pytest.raises(ValueError, match="duplicate MCP tool name"):
        module.create_mcp_server(fake_service_client, profile=McpProfile.EXPERT)


def test_real_mcp_python_run_publishes_only_outputs_and_lists_proxy(
    tmp_path: Path,
) -> None:
    service = AgentWorkspaceService(tmp_path, object())

    async def scenario() -> tuple[object, object, object]:
        async with Client(create_mcp_server(service, profile=McpProfile.EXPERT)) as client:
            run = await client.call_tool(
                "python.run",
                {
                    "code": "private_value = 40\nanswer = private_value + 2",
                    "inputs": {},
                    "outputs": ["answer"],
                },
            )
            variables = await client.call_tool("python.variables", {})
            proxy_id = run.structured_content["value"]["outputs"]["answer"]["proxy_id"]
            inspection = await client.call_tool(
                "python.inspect", {"proxy_id": proxy_id}
            )
            return run, variables, inspection

    try:
        run, variables, inspection = asyncio.run(scenario())
        assert run.structured_content["ok"] is True
        assert [
            item["qualified_name"]
            for item in variables.structured_content["value"]
        ] == ["python.answer"]
        assert inspection.structured_content["value"]["preview"] == 42
    finally:
        service.close()


def test_mcp_forwards_explicit_python_outputs_and_materialization_budget(
    fake_service_client: FakeServiceClient,
) -> None:
    budget = {
        "depth": 8,
        "items": 1000,
        "rows": 1000,
        "bytes": 1024 * 1024,
        "timeout_s": 5,
    }

    async def scenario() -> None:
        async with Client(create_mcp_server(fake_service_client, profile=McpProfile.EXPERT)) as client:
            await client.call_tool(
                "python.run",
                {
                    "code": "result = source.groupby('Kind').sum()",
                    "inputs": {"source": "proxy-frame"},
                    "outputs": ["result"],
                },
            )
            await client.call_tool(
                "value.materialize",
                {
                    "proxy_id": "proxy-onec",
                    "budget": budget,
                    "refs": "both",
                },
            )

    asyncio.run(scenario())
    assert fake_service_client.calls == [
        (
            "python.run",
            {
                "code": "result = source.groupby('Kind').sum()",
                "inputs": {"source": "proxy-frame"},
                "outputs": ["result"],
            },
        ),
        (
            "value.materialize",
            {
                "proxy_id": "proxy-onec",
                "target": "python",
                "policy": {"refs": "both"},
                "budget": budget,
            },
        ),
    ]
