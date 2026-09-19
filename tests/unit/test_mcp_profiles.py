from __future__ import annotations

import asyncio
import json

import pytest
from mcp.client import Client
from pydantic import ValidationError

import onec_runtime_mcp.server as mcp_server

from onec_runtime_mcp.agent.contracts import (
    AgentOperationState,
    FailureCategory,
    MethodFailure,
    OperationDescriptor,
    RetrySafety,
    ServiceResponse,
    StateChanged,
)
from onec_runtime_mcp.agent.capture_service import _capture_operation_recovery
from onec_runtime_mcp.agent.mcp_profiles import (
    AGENT_TOOL_NAMES,
    EXPERT_TOOL_NAMES,
    McpProfile,
    tool_names,
)
from onec_runtime_mcp.server import create_mcp_server


class FakeClient:
    def call(self, method: str, arguments: dict[str, object]) -> ServiceResponse:
        return ServiceResponse.success({"method": method, "arguments": arguments})


class FacadeRecordingClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def call(self, method: str, arguments: dict[str, object]) -> ServiceResponse:
        self.calls.append((method, arguments))
        if method == "code.run":
            return ServiceResponse.success(
                OperationDescriptor(
                    "op-1",
                    AgentOperationState.QUEUED,
                    "runtime-1",
                    1,
                    "cell-1",
                    2,
                    "a" * 64,
                )
            )
        if method == "operation.view":
            return ServiceResponse.success(
                {
                    "operation": {
                        "operation_id": "op-1",
                        "kind": "code_run",
                        "runtime_id": "runtime-1",
                        "runtime_generation": 1,
                        "cell_id": "cell-1",
                        "revision": 2,
                        "source_sha256": "a" * 64,
                    },
                    "state": "queued",
                    "messages": [],
                    "next_message_cursor": 0,
                    "next_event_cursor": 1,
                    "changed_variables": [],
                    "change_confidence": "unknown",
                    "outputs": {},
                    "capture": None,
                    "failure": None,
                    "recovery": [],
                    "truncation": {
                        "messages": False,
                        "changed_variables": False,
                        "outputs": False,
                    },
                    "execution_provenance": None,
                }
            )
        raise AssertionError(method)


def _canonical_operation_view_wire() -> dict[str, object]:
    return {
        "operation": {
            "operation_id": "op-failed",
            "kind": "code_run",
            "runtime_id": "runtime-1",
            "runtime_generation": 1,
            "cell_id": "cell-1",
            "revision": 2,
            "source_sha256": "a" * 64,
        },
        "state": "failed",
        "messages": [],
        "next_message_cursor": 0,
        "next_event_cursor": 4,
        "changed_variables": [],
        "change_confidence": "unknown",
        "outputs": {},
        "capture": None,
        "failure": None,
        "recovery": [],
        "truncation": {
            "messages": False,
            "changed_variables": False,
            "outputs": False,
        },
        "execution_provenance": {
            "visible_source_sha256": "a" * 64,
            "executed_source_sha256": "b" * 64,
            "source_map_sha256": "c" * 64,
            "mode": "main",
            "worker_generation": None,
            "worker_manifest_sha256": None,
        },
    }


def _tools(profile: McpProfile | None = None) -> tuple[object, ...]:
    async def scenario() -> tuple[object, ...]:
        server = (
            create_mcp_server(FakeClient())
            if profile is None
            else create_mcp_server(FakeClient(), profile=profile)
        )
        async with Client(server) as client:
            return tuple((await client.list_tools()).tools)

    return asyncio.run(scenario())


def test_default_profile_lists_only_compact_agent_palette() -> None:
    tools = _tools()
    assert {tool.name for tool in tools} == AGENT_TOOL_NAMES
    assert len(tools) == 16
    assert "operation.output" not in AGENT_TOOL_NAMES
    assert not any(name.startswith("capture.") for name in AGENT_TOOL_NAMES)


def test_expert_profile_preserves_complete_domain_catalog() -> None:
    tools = _tools(McpProfile.EXPERT)
    assert {tool.name for tool in tools} == EXPERT_TOOL_NAMES
    assert len(tools) == 51
    assert AGENT_TOOL_NAMES < EXPERT_TOOL_NAMES
    assert "operation.output" in EXPERT_TOOL_NAMES


def test_capture_profile_adds_only_capture_intents_to_the_compact_agent_palette() -> None:
    tools = _tools(McpProfile.CAPTURE)
    assert len(tools) == 22
    assert {tool.name for tool in tools} == AGENT_TOOL_NAMES | {
        "capture.run_until",
        "capture.inspect",
        "capture.stack",
        "capture.frame",
        "capture.hypothesis",
        "capture.continue",
    }


def test_agent_annotations_match_actual_side_effects() -> None:
    tools = {tool.name: tool for tool in _tools(McpProfile.AGENT)}
    expected = {
        "workspace.open": (True, False, True),
        "workspace.status": (True, False, True),
        "workspace.variables": (False, False, True),
        "runtime.ensure": (False, False, True),
        "runtime.restart": (False, True, False),
        "runtime.close": (False, True, False),
        "code.list": (False, False, True),
        "code.get": (True, False, True),
        "code.put": (False, True, False),
        "code.run": (False, True, False),
        "code.run_inline": (False, True, False),
        "operation.wait": (True, False, True),
        "value.inspect": (True, False, True),
        "value.materialize": (False, False, False),
        "value.to_df": (False, False, False),
        "python.run": (False, False, False),
    }
    assert {
        name: (
            tool.annotations.read_only_hint,
            tool.annotations.destructive_hint,
            tool.annotations.idempotent_hint,
        )
        for name, tool in tools.items()
    } == expected


def test_agent_code_run_routes_through_facade_and_returns_operation_view() -> None:
    service = FacadeRecordingClient()

    async def scenario() -> object:
        async with Client(create_mcp_server(service)) as client:
            return await client.call_tool(
                "code.run",
                {
                    "cell_id": "cell-1",
                    "revision": 2,
                    "source_sha256": "a" * 64,
                    "observe": {
                        "items": [],
                        "budget_profile": "agent_metadata",
                    },
                },
            )

    result = asyncio.run(scenario())

    assert result.structured_content["ok"] is True
    assert result.structured_content["value"]["operation"]["operation_id"] == "op-1"
    assert service.calls == [
        (
            "code.run",
            {
                "cell_id": "cell-1",
                "revision": 2,
                "source_sha256": "a" * 64,
                "wait_s": 0,
                "inputs": {},
                "observe": {"items": [], "budget_profile": "agent_metadata"},
            },
        ),
        (
            "operation.view",
            {"operation_id": "op-1", "after_message_cursor": 0},
        ),
    ]


def test_failed_agent_operation_carries_exact_public_diagnostic_through_mcp() -> None:
    """Break caught: MCP rejects canonical diagnostic/state_changed failure fields."""
    view = _canonical_operation_view_wire()
    view["failure"] = {
        "stage": "execution",
        "partial_results": {},
        "continue_state": None,
        "state_changed": "no",
        "diagnostic": {
            "diagnostic_id": "d" * 64,
            "stage": "execution",
            "mapping_confidence": "exact",
            "visible_location": {
                "line": 1,
                "column": 1,
                "span": {"start": 0, "end": 1},
            },
            "related_visible_span": None,
            "excerpt": None,
            "synthetic_region": None,
        },
    }
    class FailedOperationClient:
        def call(self, method: str, arguments: dict[str, object]) -> ServiceResponse:
            if method == "operation.wait":
                return ServiceResponse.success(
                    OperationDescriptor(
                        "op-failed",
                        AgentOperationState.FAILED,
                        "runtime-1",
                        1,
                        "cell-1",
                        2,
                        "a" * 64,
                    )
                )
            if method == "operation.view":
                return ServiceResponse.success(view)
            raise AssertionError((method, arguments))

    async def scenario() -> object:
        async with Client(create_mcp_server(FailedOperationClient())) as client:
            return await client.call_tool(
                "operation.wait",
                {"operation_id": "op-failed"},
            )

    result = asyncio.run(scenario())

    assert result.is_error is False
    failure = result.structured_content["value"]["failure"]
    assert failure == view["failure"]
    assert "platform_diagnostic" not in json.dumps(
        result.structured_content,
        ensure_ascii=False,
    )


def test_agent_and_capture_profiles_cannot_explain_raw_failure() -> None:
    """Break caught: the expert diagnostic tool is registered in a compact palette."""
    assert "operation.explain_failure" not in tool_names(McpProfile.AGENT)
    assert "operation.explain_failure" not in tool_names(McpProfile.CAPTURE)
    assert "operation.explain_failure" in tool_names(McpProfile.EXPERT)


def test_acceptance_expert_diagnostic_is_bounded_and_expert_only_via_sdk() -> None:
    """Break caught: compact MCP gains private detail or expert loses its route."""
    from test_mcp_server import _expert_failure_value

    expected = _expert_failure_value()

    class ExpertFailureClient:
        def call(
            self,
            method: str,
            arguments: dict[str, object],
        ) -> ServiceResponse:
            assert method == "operation.explain_failure"
            assert arguments == {"operation_id": "operation-failed"}
            return ServiceResponse.success(expected)

    async def scenario() -> tuple[set[str], set[str], set[str], object]:
        listed: list[set[str]] = []
        for profile in (McpProfile.AGENT, McpProfile.CAPTURE):
            async with Client(
                create_mcp_server(FakeClient(), profile=profile)
            ) as compact:
                listed.append(
                    {tool.name for tool in (await compact.list_tools()).tools}
                )
        async with Client(
            create_mcp_server(
                ExpertFailureClient(),
                profile=McpProfile.EXPERT,
            )
        ) as expert:
            expert_tools = {
                tool.name for tool in (await expert.list_tools()).tools
            }
            result = await expert.call_tool(
                "operation.explain_failure",
                {"operation_id": "operation-failed"},
            )
        return listed[0], listed[1], expert_tools, result

    agent_tools, capture_tools, expert_tools, result = asyncio.run(scenario())

    assert "operation.explain_failure" not in agent_tools
    assert "operation.explain_failure" not in capture_tools
    assert "operation.explain_failure" in expert_tools
    assert result.is_error is False
    value = result.structured_content["value"]
    assert value["diagnostic"] == expected["diagnostic"]
    details = value["diagnostic_details"]
    assert set(details) == {
        "diagnostic_id",
        "runtime_summary",
        "stage",
        "mapping_confidence",
        "visible_location",
        "related_visible_span",
        "excerpt",
        "synthetic_region",
        "lowered_location",
        "platform_diagnostic",
        "platform_diagnostic_sha256",
        "platform_diagnostic_truncated",
        "platform_diagnostic_redacted",
        "execution_artifact_sha256",
        "source_map_sha256",
        "worker_generation",
        "worker_manifest_sha256",
    }
    assert len(details["platform_diagnostic"].encode("utf-8")) <= 64 * 1024
    assert details["platform_diagnostic_redacted"] is True
    encoded = json.dumps(result.structured_content, ensure_ascii=False)
    for forbidden in (
        "FULL_VISIBLE_SOURCE",
        "FULL_LOWERED_SOURCE",
        "FULL_EXECUTED_SOURCE",
        "worker_admission_token",
        "9182",
        "private-connection",
    ):
        assert forbidden not in encoded


def test_agent_operation_wire_requires_outer_provenance_key_even_when_null() -> None:
    """Break caught: omitted execution_provenance is silently defaulted to null."""
    view = _canonical_operation_view_wire()
    view.pop("execution_provenance")

    with pytest.raises(ValidationError):
        mcp_server._AgentOperationViewWire.model_validate(view)


def test_agent_provenance_wire_requires_both_nullable_worker_keys() -> None:
    """Break caught: omitted Worker provenance keys are silently synthesized."""
    for omitted in ("worker_generation", "worker_manifest_sha256"):
        provenance = dict(
            _canonical_operation_view_wire()["execution_provenance"]  # type: ignore[arg-type]
        )
        provenance.pop(omitted)

        with pytest.raises(ValidationError):
            mcp_server._OperationExecutionProvenanceWire.model_validate(provenance)


def test_agent_provenance_wire_rejects_extra_private_key() -> None:
    """Break caught: MCP provenance widening admits private capability metadata."""
    provenance = dict(
        _canonical_operation_view_wire()["execution_provenance"]  # type: ignore[arg-type]
    )
    provenance["worker_admission_token"] = "private"

    with pytest.raises(ValidationError):
        mcp_server._OperationExecutionProvenanceWire.model_validate(provenance)


@pytest.mark.parametrize("worker_generation", [True, "7"], ids=["bool", "string"])
def test_agent_provenance_wire_rejects_non_exact_worker_generation(
    worker_generation: object,
) -> None:
    """Break caught: Pydantic coercion turns malformed generation data into int."""
    provenance = dict(
        _canonical_operation_view_wire()["execution_provenance"]  # type: ignore[arg-type]
    )
    provenance["worker_generation"] = worker_generation

    with pytest.raises(ValidationError):
        mcp_server._OperationExecutionProvenanceWire.model_validate(provenance)


def test_agent_value_tools_expose_named_budget_profiles_not_raw_budgets() -> None:
    tools = {tool.name: tool for tool in _tools(McpProfile.AGENT)}

    for name in {"value.inspect", "value.materialize", "value.to_df"}:
        properties = tools[name].input_schema["properties"]
        assert "budget_profile" in properties
        assert "budget" not in properties


def test_agent_operation_tools_publish_one_canonical_output_schema() -> None:
    tools = {tool.name: tool for tool in _tools(McpProfile.AGENT)}
    expected_fields = {
        "operation",
        "state",
        "messages",
        "next_message_cursor",
        "next_event_cursor",
        "changed_variables",
        "change_confidence",
        "outputs",
        "capture",
        "failure",
        "recovery",
        "truncation",
        "execution_provenance",
    }

    schemas = []
    for name in {"code.run", "code.run_inline", "operation.wait"}:
        schema = tools[name].output_schema
        view = schema["$defs"]["_AgentOperationViewWire"]
        assert set(view["properties"]) == expected_fields
        schemas.append(view)
    assert schemas[0] == schemas[1] == schemas[2]

    definitions = tools["operation.wait"].output_schema["$defs"]
    provenance = definitions["_OperationExecutionProvenanceWire"]
    assert set(provenance["properties"]) == {
        "visible_source_sha256",
        "executed_source_sha256",
        "source_map_sha256",
        "mode",
        "worker_generation",
        "worker_manifest_sha256",
    }
    assert provenance["additionalProperties"] is False
    assert set(provenance["required"]) == set(provenance["properties"])
    assert "execution_provenance" in view["required"]
    assert provenance["properties"]["visible_source_sha256"]["pattern"] == (
        "^[0-9a-f]{64}$"
    )

    inline = tools["code.run_inline"].input_schema["properties"]
    assert inline["language"]["const"] == "bsl"
    assert inline["mode"]["const"] == "main"


def test_agent_schemas_publish_exact_observation_and_proxy_contracts() -> None:
    tools = {tool.name: tool for tool in _tools(McpProfile.AGENT)}
    run_schema = tools["code.run"].input_schema
    definitions = run_schema["$defs"]

    plan = definitions["_ObservationPlanWire"]
    assert set(plan["properties"]) == {"items", "budget_profile"}
    item = definitions["_ObservationItemWire"]
    assert set(item["properties"]) == {"alias", "source", "result", "select"}
    temporary = definitions["_TemporaryTableSourceWire"]
    assert set(temporary["properties"]) == {"kind", "manager_id", "table"}
    assert "name" not in temporary["properties"]

    output = tools["operation.wait"].output_schema
    view = output["$defs"]["_AgentOperationViewWire"]
    state = view["properties"]["state"]
    assert "$ref" in state or "enum" in state
    proxy = output["$defs"]["_ProxyDescriptorWire"]
    assert set(proxy["properties"]) == {
        "proxy_id",
        "realm",
        "lifetime",
        "qualified_name",
        "type_name",
        "version",
        "consistency",
        "fence",
        "provenance",
        "capabilities",
        "known_size",
        "bounded_preview",
    }


def test_capture_profile_adds_only_the_paged_inspection_intent() -> None:
    tools = {tool.name: tool for tool in _tools(McpProfile.CAPTURE)}

    assert "capture.inspect" in tools
    assert "capture.hypothesis" in tools
    assert "capture.variables" not in tools
    assert "capture.temporary_tables" not in tools
    properties = tools["capture.inspect"].input_schema["properties"]
    assert set(properties) == {"fence", "filters", "cursor", "limit", "observe"}
    definitions = tools["capture.inspect"].input_schema["$defs"]
    assert "_CaptureInspectObservationPlanWire" in definitions
    assert "_ObservationPlanWire" not in definitions
    assert definitions["_CaptureManagerOriginWire"]["properties"]["namespace"]["const"] == "frame"
    hypothesis = tools["capture.hypothesis"].input_schema["properties"]
    assert set(hypothesis) == {"fence", "code_ref", "request_id", "observe", "wait_s"}


def test_capture_actual_sdk_schema_bounds_points_code_ref_and_table_rows_truthfully() -> None:
    tools = {tool.name: tool for tool in _tools(McpProfile.CAPTURE)}
    run_until = tools["capture.run_until"].input_schema
    points = run_until["properties"]["points"]
    assert (points["minItems"], points["maxItems"]) == (1, 32)

    hypothesis = tools["capture.hypothesis"].input_schema
    assert hypothesis["$defs"]["_CaptureCodeRefWire"]["additionalProperties"] is False
    assert (
        hypothesis["$defs"]["_CaptureTableRowsSelectionWire"]
        ["properties"]["limit"]["maximum"]
        == 100
    )
    assert "_CaptureHypothesisObservationPlanWire" in hypothesis["$defs"]

    # The capture restriction must not weaken the global Domain/Agent profile.
    code_run = tools["code.run"].input_schema
    assert (
        code_run["$defs"]["_TableRowsSelectionWire"]
        ["properties"]["limit"]["maximum"]
        == 10_000
    )


def test_capture_hypothesis_publishes_exact_recursive_view_schema_and_annotations() -> None:
    # Break caught: a dict[str, object] capture payload can recursively admit a
    # prepared proof or source text that is outside the public contract.
    tool = {tool.name: tool for tool in _tools(McpProfile.CAPTURE)}[
        "capture.hypothesis"
    ]

    assert (
        tool.annotations.read_only_hint,
        tool.annotations.destructive_hint,
        tool.annotations.idempotent_hint,
    ) == (False, True, True)
    definitions = tool.output_schema["$defs"]
    capture = definitions["_CaptureViewWire"]
    assert set(capture["properties"]) == {
        "fence",
        "location",
        "inspection",
        "dirty_roots",
        "paused",
        "mutable_object_caveat",
        "recovery",
    }
    assert set(definitions["_CaptureInspectionWire"]["properties"]) == {
        "fence",
        "variables",
        "temporary_table_managers",
        "temporary_tables",
        "cursor",
        "limit",
        "total_variables",
        "next_cursor",
        "truncated",
    }
    plan = tool.input_schema["$defs"]["_CaptureHypothesisObservationPlanWire"]
    assert plan["properties"]["items"]["maxItems"] == 100
    selection = tool.input_schema["$defs"]["_CaptureTableRowsSelectionWire"]
    assert selection["properties"]["limit"]["maximum"] == 100
    assert selection["properties"]["columns"]["maxItems"] == 100


def _assert_recursively_bounded_output_schema(schema: dict[str, object]) -> None:
    """Reject open recursive containers in an official SDK tools/list schema."""
    definitions = schema.get("$defs", {})
    assert isinstance(definitions, dict)
    visited: set[str] = set()

    def visit(node: object, path: str) -> None:
        assert isinstance(node, dict), path
        reference = node.get("$ref")
        if isinstance(reference, str):
            assert reference.startswith("#/$defs/"), (path, reference)
            name = reference.rsplit("/", 1)[1]
            if name in visited:
                return
            visited.add(name)
            visit(definitions[name], f"$defs.{name}")
            return
        for keyword in ("anyOf", "oneOf", "allOf"):
            branches = node.get(keyword, ())
            if branches:
                assert isinstance(branches, list), (path, keyword)
                for index, branch in enumerate(branches):
                    visit(branch, f"{path}.{keyword}[{index}]")
        node_type = node.get("type")
        if node_type == "string":
            assert (
                "maxLength" in node or "enum" in node or "const" in node
            ), path
        if node_type == "array":
            assert "maxItems" in node, path
            visit(node["items"], f"{path}.items")
        if node_type == "object":
            additional = node.get("additionalProperties", False)
            if additional is not False:
                assert additional is not True and additional != {}, path
                assert "maxProperties" in node, path
                property_names = node.get("propertyNames")
                assert isinstance(property_names, dict), path
                assert "maxLength" in property_names, path
                visit(additional, f"{path}.additionalProperties")
            properties = node.get("properties", {})
            assert isinstance(properties, dict), path
            for name, child in properties.items():
                visit(child, f"{path}.{name}")

    visit(schema, "$root")


def test_capture_tools_publish_recursively_bounded_typed_output_schemas_via_official_sdk() -> None:
    # Break caught: generic dict[str, object] response annotations admit
    # unbounded strings/arrays/objects and private runtime artifacts at any
    # recursive depth even when the input schema is bounded.
    tools = {tool.name: tool for tool in _tools(McpProfile.CAPTURE)}

    for name in {"capture.run_until", "capture.inspect", "capture.hypothesis"}:
        _assert_recursively_bounded_output_schema(tools[name].output_schema)

    for name in {"capture.run_until", "capture.hypothesis"}:
        failure = tools[name].output_schema["$defs"]["_CaptureFailureWire"]
        assert set(failure["properties"]) == {
            "category",
            "code",
            "retryable",
            "state_changed",
            "stage",
            "message",
            "details",
            "partial_results",
            "continue_state",
            "recovery",
        }
        assert failure["additionalProperties"] is False


def test_capture_mcp_failure_is_finite_private_and_carries_executable_recovery() -> None:
    # Break caught: MethodFailure.current_state previously crossed the capture
    # MCP boundary as an arbitrary object, including exception text, RDBG IDs,
    # PIDs, tokens, and prepared-artifact internals.
    sentinel = "PRIVATE_SENTINEL RDBG pid=4242 token=capture_prepared_secret catalog-id"

    class FailureClient:
        def call(self, method: str, arguments: dict[str, object]) -> ServiceResponse:
            assert method == "capture.hypothesis"
            return ServiceResponse.fail(
                MethodFailure(
                    FailureCategory.PLATFORM_FAILURE,
                    StateChanged.UNKNOWN,
                    RetrySafety.AFTER_STATUS_CHECK,
                    {
                        "stage": "capture_preparation",
                        "runtime_state": "unknown",
                        "continue_state": "outcome_unknown",
                        "private_artifact": sentinel,
                        "recovery": [
                            {"method": "workspace.status", "arguments": {}},
                            {
                                "method": "operation.wait",
                                "arguments": {
                                    "operation_id": "capture-operation",
                                    "timeout_s": 0,
                                    "after_event_cursor": 0,
                                    "after_message_cursor": 0,
                                },
                            },
                            {
                                "method": "runtime.close",
                                "arguments": {"policy": "abort_generation"},
                            },
                            {
                                "method": "runtime.restart",
                                "arguments": {"policy": "abort_generation"},
                            },
                        ],
                    },
                    partial_results={"private": sentinel},
                    diagnostic_id="diag-private-prepared-artifact",
                )
            )

    async def scenario() -> object:
        async with Client(
            create_mcp_server(FailureClient(), profile=McpProfile.CAPTURE)
        ) as client:
            return await client.call_tool(
                "capture.hypothesis",
                {
                    "fence": {
                        "capture_intent_id": "intent",
                        "operation_id": "capture-operation",
                        "source_revision": 1,
                        "source_sha256": "a" * 64,
                        "capture_generation": 1,
                        "stop_sequence": 1,
                    },
                    "code_ref": {
                        "cell_id": "capture-cell",
                        "revision": 1,
                        "source_sha256": "b" * 64,
                    },
                    "request_id": "capture-request",
                },
            )

    result = asyncio.run(scenario())
    failure = result.structured_content["failure"]
    assert set(failure) == {
        "category",
        "code",
        "retryable",
        "state_changed",
        "stage",
        "message",
        "details",
        "partial_results",
        "continue_state",
        "recovery",
    }
    assert failure["stage"] == "capture_preparation"
    assert failure["continue_state"] == "outcome_unknown"
    assert failure["recovery"] == [
        {"method": "workspace.status", "arguments": {}},
        {
            "method": "operation.wait",
            "arguments": {
                "operation_id": "capture-operation",
                "timeout_s": 0,
                "after_event_cursor": 0,
                "after_message_cursor": 0,
            },
        },
        {
            "method": "runtime.close",
            "arguments": {"policy": "abort_generation"},
        },
        {
            "method": "runtime.restart",
            "arguments": {"policy": "abort_generation"},
        },
    ]
    encoded = json.dumps(result.structured_content, ensure_ascii=False, sort_keys=True)
    for private in (
        "PRIVATE_SENTINEL",
        "RDBG",
        "4242",
        "capture_prepared_secret",
        "catalog-id",
        "private_artifact",
        "diag-private-prepared-artifact",
    ):
        assert private not in encoded


def test_every_uncertain_capture_path_recovery_is_callable_by_actual_capture_sdk() -> None:
    # These are the five production branches named by the review.  They share
    # one generator so a branch cannot silently drift back to an expert-only
    # method while the transport schema continues to look valid.
    path_actions = {
        path: _capture_operation_recovery("op-recovery")
        for path in (
            "correlation",
            "no_stop",
            "transport",
            "disarm",
            "arm_cleanup",
        )
    }

    class RecoveryClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def call(self, method: str, arguments: dict[str, object]) -> ServiceResponse:
            self.calls.append((method, arguments))
            if method == "operation.wait":
                return ServiceResponse.success(
                    OperationDescriptor(
                        "op-recovery",
                        AgentOperationState.UNKNOWN,
                        "runtime-public",
                        1,
                        "capture-cell",
                        1,
                        "a" * 64,
                    )
                )
            if method == "operation.view":
                return ServiceResponse.success(
                    {
                        "operation": {
                            "operation_id": "op-recovery",
                            "kind": "capture_run_until",
                            "runtime_id": "runtime-public",
                            "runtime_generation": 1,
                            "cell_id": "capture-cell",
                            "revision": 1,
                            "source_sha256": "a" * 64,
                        },
                        "state": "unknown",
                        "messages": [],
                        "next_message_cursor": 0,
                        "next_event_cursor": 1,
                        "changed_variables": [],
                        "change_confidence": "unknown",
                        "outputs": {},
                        "capture": None,
                        "failure": {
                            "stage": "capture_correlation",
                            "partial_results": {},
                        },
                        "recovery": [],
                        "truncation": {
                            "messages": False,
                            "changed_variables": False,
                            "outputs": False,
                        },
                    }
                )
            return ServiceResponse.success(
                {"method": method, "arguments": arguments}
            )

    recovery_client = RecoveryClient()

    async def scenario() -> tuple[set[str], list[tuple[str, str, bool]]]:
        observations: list[tuple[str, str, bool]] = []
        async with Client(
            create_mcp_server(recovery_client, profile=McpProfile.CAPTURE)
        ) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            for path, actions in path_actions.items():
                for action in actions:
                    result = await client.call_tool(
                        action.method, dict(action.arguments)
                    )
                    observations.append(
                        (path, action.method, result.is_error is True)
                    )
            return names, observations

    names, observations = asyncio.run(scenario())

    assert observations
    assert all(method in names for _, method, _ in observations)
    assert all(is_error is False for _, _, is_error in observations)
    assert {path for path, _, _ in observations} == set(path_actions)
    assert {method for _, method, _ in observations} == {
        "operation.wait",
        "runtime.close",
    }


def test_capture_continue_serializes_exact_bounded_attempt_evidence_without_private_ids() -> None:
    partial_results = {
        f"root_{index}": (
            "succeeded" if index < 99 else "outcome_unknown"
        )
        for index in range(100)
    }
    expected_view = {
        "operation": {
            "operation_id": "continuation-operation",
            "kind": "capture_continue",
            "runtime_id": "runtime-public",
            "runtime_generation": 3,
            "cell_id": None,
            "revision": None,
            "source_sha256": None,
        },
        "state": "unknown",
        "messages": [],
        "next_message_cursor": 0,
        "next_event_cursor": 1,
        "changed_variables": [],
        "change_confidence": "unknown",
        "outputs": {},
        "capture": None,
        "failure": {
            "stage": "capture_writeback",
            "partial_results": partial_results,
            "continue_state": "unattempted",
            "state_changed": None,
            "diagnostic": None,
        },
        "recovery": [
            {"method": "workspace.status", "arguments": {}},
            {
                "method": "operation.wait",
                "arguments": {
                    "operation_id": "continuation-operation",
                    "timeout_s": 0,
                    "after_event_cursor": 0,
                    "after_message_cursor": 0,
                },
            },
            {
                "method": "runtime.close",
                "arguments": {"policy": "abort_generation"},
            },
            {
                "method": "runtime.restart",
                "arguments": {"policy": "abort_generation"},
            },
        ],
        "truncation": {
            "messages": False,
            "changed_variables": False,
            "outputs": False,
        },
        "execution_provenance": None,
    }

    class ContinuationClient:
        def call(self, method: str, arguments: dict[str, object]) -> ServiceResponse:
            assert method == "capture.continue"
            return ServiceResponse.success(expected_view)

    async def scenario() -> object:
        async with Client(
            create_mcp_server(ContinuationClient(), profile=McpProfile.CAPTURE)
        ) as client:
            return await client.call_tool(
                "capture.continue",
                {
                    "fence": {
                        "capture_intent_id": "intent",
                        "operation_id": "capture-operation",
                        "source_revision": 1,
                        "source_sha256": "a" * 64,
                        "capture_generation": 2,
                        "stop_sequence": 4,
                    },
                    "request_id": "continuation-request",
                },
            )

    result = asyncio.run(scenario())
    failure = result.structured_content["value"]["failure"]
    assert failure == {
        "stage": "capture_writeback",
        "partial_results": partial_results,
        "continue_state": "unattempted",
        "state_changed": None,
        "diagnostic": None,
    }
    encoded = json.dumps(result.structured_content, sort_keys=True)
    assert len(failure["partial_results"]) == 100
    assert "attempt_id" not in encoded
    assert "ticket_id" not in encoded


def test_capture_hypothesis_returns_the_exact_canonical_view_through_official_sdk() -> None:
    expected_view = {
        "operation": {
            "operation_id": "hypothesis-operation",
            "kind": "capture_hypothesis",
            "runtime_id": "runtime-public",
            "runtime_generation": 1,
            "cell_id": "capture-cell",
            "revision": 1,
            "source_sha256": "b" * 64,
        },
        "state": "captured",
        "messages": ["bounded message"],
        "next_message_cursor": 1,
        "next_event_cursor": 2,
        "changed_variables": [],
        "change_confidence": "unknown",
        "outputs": {},
        "capture": None,
        "failure": None,
        "recovery": [],
        "truncation": {
            "messages": False,
            "changed_variables": False,
            "outputs": False,
        },
        "execution_provenance": None,
    }

    class SuccessClient:
        def call(self, method: str, arguments: dict[str, object]) -> ServiceResponse:
            assert method == "capture.hypothesis"
            return ServiceResponse.success(expected_view)

    async def scenario() -> object:
        async with Client(
            create_mcp_server(SuccessClient(), profile=McpProfile.CAPTURE)
        ) as client:
            return await client.call_tool(
                "capture.hypothesis",
                {
                    "fence": {
                        "capture_intent_id": "intent",
                        "operation_id": "capture-operation",
                        "source_revision": 1,
                        "source_sha256": "a" * 64,
                        "capture_generation": 1,
                        "stop_sequence": 1,
                    },
                    "code_ref": {
                        "cell_id": "capture-cell",
                        "revision": 1,
                        "source_sha256": "b" * 64,
                    },
                    "request_id": "capture-request",
                },
            )

    result = asyncio.run(scenario())
    assert result.is_error is False
    assert result.structured_content == {
        "ok": True,
        "value": expected_view,
        "failure": None,
    }
