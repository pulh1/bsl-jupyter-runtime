"""Offline handler-bound CAPTURE-vs-expert MCP palette evaluation."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from importlib.metadata import version
import inspect
import json
from pathlib import Path
import re
from time import monotonic_ns
from mcp.client import Client

from onec_runtime_mcp.agent.contracts import ServiceResponse
from onec_runtime_mcp.agent.mcp_profiles import McpProfile, tool_names
from onec_runtime_mcp.server import create_mcp_server


_JSON_LEXEME = re.compile(
    r'"(?:\\.|[^"\\])*"|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?|true|false|null|[{}\[\],:]'
)
_TASKS = (
    "ensure",
    "capture_a",
    "inspect_frame",
    "bounded_head",
    "hypothesis",
    "continue_once",
    "recover_unknown",
    "close",
)
_PROFILE_SCRIPT = {
    McpProfile.CAPTURE: {
        "ensure": "runtime.ensure",
        "capture_a": "capture.run_until",
        "inspect_frame": "capture.inspect",
        "bounded_head": "value.to_df",
        "hypothesis": "capture.hypothesis",
        "continue_once": "capture.continue",
        "recover_unknown": "runtime.restart",
        "close": "runtime.close",
    },
    McpProfile.EXPERT: {
        "ensure": "runtime.ensure",
        "capture_a": "code.run",
        "inspect_frame": "value.inspect",
        "bounded_head": "value.to_df",
        "hypothesis": "code.run",
        "continue_once": "code.run",
        "recover_unknown": "operation.abort_generation",
        "close": "runtime.close",
    },
}


_SOURCE_SHA256 = "a" * 64
_FRAME_PROXY_ID = "proxy-frame-a"


def _capture_fence(generation: int) -> dict[str, object]:
    return {
        "capture_intent_id": f"intent-{generation}",
        "operation_id": "operation-main",
        "source_revision": 1,
        "source_sha256": _SOURCE_SHA256,
        "capture_generation": generation,
        "stop_sequence": generation,
    }


def _capture_view(generation: int) -> dict[str, object]:
    fence = _capture_fence(generation)
    return {
        "fence": fence,
        "location": {
            "name": f"point_{generation}",
            "project": "zup",
            "module": "Payroll.Module",
            "procedure": "Run",
            "line": generation * 10,
            "source_revision": 1,
            "source_sha256": _SOURCE_SHA256,
            "executable_line": generation * 10,
            "excerpt": f"EvalMarker = {generation};",
        },
        "inspection": None,
        "dirty_roots": [],
        "paused": True,
        "mutable_object_caveat": (
            "live mutable-object mutations are immediate and non-transactional"
        ),
        "recovery": [],
    }


def _capture_operation_view(
    generation: int,
    *,
    kind: str,
) -> dict[str, object]:
    return {
        "operation": {
            "operation_id": "operation-main",
            "kind": kind,
            "runtime_id": "runtime-offline",
            "runtime_generation": 1,
            "cell_id": "eval-main",
            "revision": 1,
            "source_sha256": _SOURCE_SHA256,
        },
        "state": "captured",
        "messages": [],
        "next_message_cursor": 0,
        "next_event_cursor": generation,
        "changed_variables": [],
        "change_confidence": "exact",
        "outputs": {},
        "capture": _capture_view(generation),
        "failure": None,
        "recovery": [],
        "truncation": {
            "messages": False,
            "changed_variables": False,
            "outputs": False,
        },
    }


@dataclass(slots=True)
class OfflinePaletteDomainClient:
    """Pure deterministic Domain collaborator behind the registered MCP handlers."""

    profile: McpProfile
    faults: Mapping[str, str] = field(default_factory=dict)
    state: str = "new"
    inspected: bool = False
    bounded: bool = False
    hypothesis_recorded: bool = False
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    def _require(self, condition: bool, message: str) -> None:
        if not condition:
            raise ValueError(message)

    def _successful_value(
        self,
        method: str,
        arguments: dict[str, object],
    ) -> object:
        if method == "runtime.ensure":
            self._require(self.state == "new", "runtime must be new")
            self._require(
                not arguments
                or set(arguments) <= {"mode", "profile"},
                "runtime.ensure arguments are invalid",
            )
            self.state = "ready"
            return {
                "runtime_id": "runtime-offline",
                "generation": 1,
                "state": "ready",
                "mode": "experiment",
                "active_operation_id": None,
                "health": "ready",
            }

        if method == "capture.run_until":
            self._require(self.profile is McpProfile.CAPTURE, "capture-only method")
            self._require(self.state == "ready", "capture A requires ready runtime")
            self._require(arguments.get("cell_id") == "eval-main", "wrong capture cell")
            self._require(arguments.get("revision") == 1, "wrong capture revision")
            self._require(
                arguments.get("source_sha256") == _SOURCE_SHA256,
                "wrong capture source",
            )
            points = arguments.get("points")
            self._require(
                isinstance(points, list)
                and len(points) == 1
                and isinstance(points[0], dict)
                and points[0].get("name") == "point_1",
                "capture A requires its exact point",
            )
            self.state = "captured_a"
            return _capture_operation_view(1, kind="capture_run_until")

        if method == "code.run":
            self._require(self.profile is McpProfile.EXPERT, "expert-only method")
            cell_id = arguments.get("cell_id")
            self._require(arguments.get("revision") == 1, "wrong code revision")
            self._require(
                arguments.get("source_sha256") == _SOURCE_SHA256,
                "wrong code source",
            )
            if cell_id == "eval-capture-a":
                self._require(self.state == "ready", "capture A requires ready runtime")
                self.state = "captured_a"
                return {
                    "operation_id": "operation-main",
                    "state": "captured_a",
                    "capture_generation": 1,
                }
            if cell_id == "eval-hypothesis":
                self._require(
                    self.state == "captured_a" and self.bounded,
                    "hypothesis requires bounded captured evidence",
                )
                self.hypothesis_recorded = True
                return {
                    "operation_id": "operation-hypothesis",
                    "state": "captured_a",
                    "hypothesis_recorded": True,
                }
            if cell_id == "eval-continue":
                self._require(
                    self.state == "captured_a" and self.hypothesis_recorded,
                    "continue requires recorded hypothesis",
                )
                self.state = "captured_b"
                return {
                    "operation_id": "operation-main",
                    "state": "captured_b",
                    "capture_generation": 2,
                }
            raise ValueError("unknown deterministic code cell")

        if method == "capture.inspect":
            self._require(self.profile is McpProfile.CAPTURE, "capture-only method")
            self._require(self.state == "captured_a", "inspect requires capture A")
            self._require(arguments.get("fence") == _capture_fence(1), "wrong fence")
            self._require(
                arguments.get("filters") == {"name": "FrameValue"},
                "wrong frame selection",
            )
            self._require(arguments.get("cursor") == 0, "wrong inspection cursor")
            self._require(arguments.get("limit") == 20, "wrong inspection limit")
            self.inspected = True
            return {
                "fence": _capture_fence(1),
                "variables": [
                    {
                        "name": "FrameValue",
                        "type_name": "ValueTable",
                        "fence": _capture_fence(1),
                        "capabilities": ["inspect", "to_df"],
                        "known_size": None,
                        "proxy_id": _FRAME_PROXY_ID,
                    }
                ],
                "temporary_table_managers": [],
                "temporary_tables": [],
                "cursor": 0,
                "limit": 20,
                "total_variables": 1,
                "next_cursor": None,
                "truncated": False,
            }

        if method == "value.inspect":
            self._require(self.profile is McpProfile.EXPERT, "expert-only method")
            self._require(self.state == "captured_a", "inspect requires capture A")
            self._require(arguments.get("proxy_id") == _FRAME_PROXY_ID, "wrong proxy")
            self._require(arguments.get("detail") == "metadata", "wrong detail")
            self.inspected = True
            return {
                "proxy_id": _FRAME_PROXY_ID,
                "type_name": "ValueTable",
                "inspected": True,
            }

        if method == "value.to_df":
            self._require(self.state == "captured_a", "dataframe requires capture A")
            self._require(self.inspected, "dataframe requires inspection")
            self._require(arguments.get("proxy_id") == _FRAME_PROXY_ID, "wrong proxy")
            self._require(arguments.get("columns") == ["Amount"], "wrong columns")
            budget = arguments.get("budget")
            expected_budget = (
                {
                    "depth": 8,
                    "items": 200_000,
                    "rows": 10_000,
                    "bytes": 64 * 1024 * 1024,
                    "timeout_s": 30.0,
                }
                if self.profile is McpProfile.CAPTURE
                else {
                    "depth": 2,
                    "items": 100,
                    "rows": 5,
                    "bytes": 65_536,
                    "timeout_s": 2.0,
                }
            )
            self._require(
                budget == expected_budget,
                "dataframe requires the exact profile-valid budget",
            )
            self.bounded = True
            return {
                "proxy_id": "proxy-dataframe-a",
                "source_proxy_id": _FRAME_PROXY_ID,
                "rows": 5,
                "bytes": 256,
                "columns": ["Amount"],
            }

        if method == "capture.hypothesis":
            self._require(self.profile is McpProfile.CAPTURE, "capture-only method")
            self._require(
                self.state == "captured_a" and self.bounded,
                "hypothesis requires bounded captured evidence",
            )
            self._require(arguments.get("fence") == _capture_fence(1), "wrong fence")
            code_ref = arguments.get("code_ref")
            self._require(
                code_ref
                == {
                    "cell_id": "eval-hypothesis",
                    "revision": 1,
                    "source_sha256": _SOURCE_SHA256,
                },
                "wrong hypothesis code",
            )
            self.hypothesis_recorded = True
            return _capture_operation_view(1, kind="capture_hypothesis")

        if method == "capture.continue":
            self._require(self.profile is McpProfile.CAPTURE, "capture-only method")
            self._require(
                self.state == "captured_a" and self.hypothesis_recorded,
                "continue requires recorded hypothesis",
            )
            self._require(arguments.get("fence") == _capture_fence(1), "wrong fence")
            points = arguments.get("next_points")
            self._require(
                isinstance(points, list)
                and len(points) == 1
                and isinstance(points[0], dict)
                and points[0].get("name") == "point_2",
                "continue requires exact next point",
            )
            self.state = "captured_b"
            return _capture_operation_view(2, kind="capture_continue")

        if method in {"runtime.restart", "operation.abort_generation"}:
            self._require(self.state == "unknown", "recovery requires unknown state")
            if method == "runtime.restart":
                self._require(
                    arguments == {"policy": "abort_generation"},
                    "restart policy is invalid",
                )
            else:
                self._require(
                    arguments == {"operation_id": "operation-main"},
                    "abort identity is invalid",
                )
            self.state = "clean_generation"
            return {
                "state": "clean_generation",
                "recovered": True,
                "method": method,
            }

        if method == "runtime.close":
            self._require(self.state == "clean_generation", "close requires cleanup")
            self._require(
                arguments == {"policy": "abort_generation"},
                "close policy is invalid",
            )
            self.state = "closed"
            return {"closed": True, "state": "closed"}

        raise ValueError(f"unsupported offline method: {method}")

    def call(self, method: str, arguments: dict[str, object]) -> ServiceResponse:
        self.calls.append((method, dict(arguments)))
        fault = self.faults.get(method)
        if fault == "malformed_result":
            return ServiceResponse.success({"state": "malformed"})
        if fault == "noop":
            if method == "capture.continue":
                return ServiceResponse.success(
                    _capture_operation_view(1, kind="capture_continue")
                )
            return ServiceResponse.success({"state": self.state, "noop": True})
        return ServiceResponse.success(self._successful_value(method, arguments))

    def lose_recovery_acknowledgement(self) -> None:
        self._require(self.state == "captured_b", "unknown injection requires capture B")
        self.state = "unknown"


async def list_tool_schemas(profile: McpProfile) -> list[dict[str, object]]:
    async with Client(
        create_mcp_server(OfflinePaletteDomainClient(profile), profile=profile)
    ) as client:
        listed = (await client.list_tools()).tools
    tools = sorted(
        (tool.model_dump(mode="json") for tool in listed),
        key=lambda item: str(item["name"]),
    )
    if {str(item["name"]) for item in tools} != set(tool_names(profile)):
        raise AssertionError("palette evaluation changed the Agent/Domain boundary")
    return tools


def _tokens(value: object) -> int:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return len(_JSON_LEXEME.findall(canonical))


def _task_arguments(profile: McpProfile, task: str) -> dict[str, object]:
    if task == "ensure":
        return {"mode": "experiment", "profile": "default"}
    if task == "capture_a":
        if profile is McpProfile.CAPTURE:
            return {
                "cell_id": "eval-main",
                "revision": 1,
                "source_sha256": _SOURCE_SHA256,
                "points": [
                    {
                        "name": "point_1",
                        "project": "zup",
                        "module": "Payroll.Module",
                        "procedure": "Run",
                        "line": 10,
                    }
                ],
                "request_id": "request-capture-a",
                "wait_s": 0,
            }
        return {
            "cell_id": "eval-capture-a",
            "revision": 1,
            "source_sha256": _SOURCE_SHA256,
            "inputs": {},
            "wait_s": 0,
        }
    if task == "inspect_frame":
        if profile is McpProfile.CAPTURE:
            return {
                "fence": _capture_fence(1),
                "filters": {"name": "FrameValue"},
                "cursor": 0,
                "limit": 20,
            }
        return {
            "proxy_id": _FRAME_PROXY_ID,
            "detail": "metadata",
            "budget_profile": "agent_metadata",
        }
    if task == "bounded_head":
        common: dict[str, object] = {
            "proxy_id": _FRAME_PROXY_ID,
            "columns": ["Amount"],
            "refs": "presentation",
        }
        if profile is McpProfile.CAPTURE:
            return common | {"budget_profile": "agent_dataframe"}
        return common | {
            "budget": {
                "depth": 2,
                "items": 100,
                "rows": 5,
                "bytes": 65_536,
                "timeout_s": 2.0,
            }
        }
    if task == "hypothesis":
        if profile is McpProfile.CAPTURE:
            return {
                "fence": _capture_fence(1),
                "code_ref": {
                    "cell_id": "eval-hypothesis",
                    "revision": 1,
                    "source_sha256": _SOURCE_SHA256,
                },
                "request_id": "request-hypothesis",
                "wait_s": 0,
            }
        return {
            "cell_id": "eval-hypothesis",
            "revision": 1,
            "source_sha256": _SOURCE_SHA256,
            "inputs": {},
            "wait_s": 0,
        }
    if task == "continue_once":
        if profile is McpProfile.CAPTURE:
            return {
                "fence": _capture_fence(1),
                "request_id": "request-continue",
                "next_points": [
                    {
                        "name": "point_2",
                        "project": "zup",
                        "module": "Payroll.Module",
                        "procedure": "Run",
                        "line": 20,
                    }
                ],
                "wait_s": 0,
            }
        return {
            "cell_id": "eval-continue",
            "revision": 1,
            "source_sha256": _SOURCE_SHA256,
            "inputs": {},
            "wait_s": 0,
        }
    if task == "recover_unknown":
        return (
            {"policy": "abort_generation"}
            if profile is McpProfile.CAPTURE
            else {"operation_id": "operation-main"}
        )
    if task == "close":
        return {"policy": "abort_generation"}
    raise ValueError(f"unknown palette task: {task}")


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _task_succeeded(
    task: str,
    *,
    before: str,
    after: str,
    value: object,
    runtime: OfflinePaletteDomainClient,
) -> bool:
    result = _mapping(value)
    if task == "ensure":
        return before == "new" and after == "ready" and result.get("state") == "ready"
    if task == "capture_a":
        capture = _mapping(result.get("capture"))
        fence = _mapping(capture.get("fence"))
        return (
            before == "ready"
            and after == "captured_a"
            and (
                fence.get("capture_generation") == 1
                or result.get("capture_generation") == 1
            )
        )
    if task == "inspect_frame":
        variables = result.get("variables")
        returned_proxy = (
            isinstance(variables, list)
            and len(variables) == 1
            and isinstance(variables[0], Mapping)
            and variables[0].get("name") == "FrameValue"
            and variables[0].get("proxy_id") == _FRAME_PROXY_ID
        ) or (
            result.get("proxy_id") == _FRAME_PROXY_ID
            and result.get("inspected") is True
        )
        return before == after == "captured_a" and runtime.inspected and returned_proxy
    if task == "bounded_head":
        return (
            before == after == "captured_a"
            and runtime.bounded
            and result.get("source_proxy_id") == _FRAME_PROXY_ID
            and result.get("rows") == 5
            and result.get("columns") == ["Amount"]
        )
    if task == "hypothesis":
        capture = _mapping(result.get("capture"))
        fence = _mapping(capture.get("fence"))
        return (
            before == after == "captured_a"
            and runtime.hypothesis_recorded
            and (
                fence.get("capture_generation") == 1
                or result.get("hypothesis_recorded") is True
            )
        )
    if task == "continue_once":
        capture = _mapping(result.get("capture"))
        fence = _mapping(capture.get("fence"))
        return (
            before == "captured_a"
            and after == "captured_b"
            and (
                fence.get("capture_generation") == 2
                or result.get("capture_generation") == 2
            )
        )
    if task == "recover_unknown":
        return (
            before == "unknown"
            and after == "clean_generation"
            and result.get("recovered") is True
            and result.get("state") == "clean_generation"
        )
    if task == "close":
        return (
            before == "clean_generation"
            and after == "closed"
            and result.get("closed") is True
            and result.get("state") == "closed"
        )
    return False


async def _execute_profile(
    profile: McpProfile,
    schemas: list[dict[str, object]],
    *,
    runtime: OfflinePaletteDomainClient,
    argument_mutator: Callable[
        [str, str, dict[str, object]], dict[str, object]
    ]
    | None,
) -> dict[str, object]:
    available = {str(item["name"]) for item in schemas}
    calls: list[str] = []
    invalid_choices = 0
    completed_tasks: list[str] = []
    recovery_transitions: list[dict[str, str]] = []
    workflow_transitions: list[dict[str, object]] = []
    async with Client(create_mcp_server(runtime, profile=profile)) as client:
        for task in _TASKS:
            chosen = _PROFILE_SCRIPT[profile][task]
            if chosen not in available:
                invalid_choices += 1
                continue
            if task == "recover_unknown":
                try:
                    runtime.lose_recovery_acknowledgement()
                except ValueError:
                    pass
            before = runtime.state
            arguments = _task_arguments(profile, task)
            if argument_mutator is not None:
                arguments = argument_mutator(task, chosen, dict(arguments))
            calls.append(chosen)
            structured: object = None
            try:
                response = await client.call_tool(chosen, arguments)
                envelope = response.structured_content
                if (
                    response.is_error is not True
                    and isinstance(envelope, Mapping)
                    and envelope.get("ok") is True
                    and envelope.get("failure") is None
                ):
                    structured = envelope.get("value")
            except Exception:
                structured = None
            after = runtime.state
            succeeded = _task_succeeded(
                task,
                before=before,
                after=after,
                value=structured,
                runtime=runtime,
            )
            transition = {
                "task": task,
                "tool": chosen,
                "from": before,
                "to": after,
                "succeeded": succeeded,
            }
            workflow_transitions.append(transition)
            if succeeded:
                completed_tasks.append(task)
            else:
                invalid_choices += 1
            if task == "recover_unknown" and succeeded:
                recovery_transitions.append(
                    {"from": "unknown", "via": chosen, "to": "clean_generation"}
                )
    return {
        "profile": profile.value,
        "tool_count": len(schemas),
        "calls": calls,
        "invalid_choices": invalid_choices,
        "input_schema_tokens": _tokens([item.get("input_schema") for item in schemas]),
        "output_schema_tokens": _tokens([item.get("output_schema") for item in schemas]),
        "recovery_transitions": recovery_transitions,
        "workflow_transitions": workflow_transitions,
        "completed_tasks": completed_tasks,
        "task_success": completed_tasks == list(_TASKS) and runtime.state == "closed",
    }


def evaluate_profile(
    profile: McpProfile,
    schemas: list[dict[str, object]],
    *,
    started_ns: int,
    runtime: OfflinePaletteDomainClient | None = None,
    argument_mutator: Callable[
        [str, str, dict[str, object]], dict[str, object]
    ]
    | None = None,
) -> dict[str, object]:
    selected_runtime = runtime or OfflinePaletteDomainClient(profile)
    result = asyncio.run(
        _execute_profile(
            profile,
            schemas,
            runtime=selected_runtime,
            argument_mutator=argument_mutator,
        )
    )
    result["wall_ms"] = max(0, (monotonic_ns() - started_ns) // 1_000_000)
    return result


def evaluate() -> dict[str, object]:
    script_wire = {
        profile.value: [[task, _PROFILE_SCRIPT[profile][task]] for task in _TASKS]
        for profile in (McpProfile.CAPTURE, McpProfile.EXPERT)
    }
    script_sha256 = sha256(
        json.dumps(
            script_wire,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    schemas: dict[McpProfile, list[dict[str, object]]] = {}
    profiles: list[dict[str, object]] = []
    for profile in (McpProfile.CAPTURE, McpProfile.EXPERT):
        started_ns = monotonic_ns()
        profile_schemas = asyncio.run(list_tool_schemas(profile))
        schemas[profile] = profile_schemas
        profiles.append(
            evaluate_profile(profile, profile_schemas, started_ns=started_ns)
        )
    schema_wire = {
        profile.value: value for profile, value in schemas.items()
    }
    canonical_schemas = json.dumps(
        schema_wire,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    runtime_identity = (
        f"{OfflinePaletteDomainClient.__module__}."
        f"{OfflinePaletteDomainClient.__qualname__}\n"
        + inspect.getsource(OfflinePaletteDomainClient)
        + "\n"
        + inspect.getsource(_task_arguments)
        + "\n"
        + inspect.getsource(_task_succeeded)
    ).encode("utf-8")
    return {
        "schema": "onec-mcp-palette-eval-v1",
        "script_sha256": script_sha256,
        "bindings": {
            "evaluator_source_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
            "sdk_identity": f"mcp=={version('mcp')}",
            "profile_schema_sha256": sha256(canonical_schemas).hexdigest(),
            "runtime_identity_sha256": sha256(runtime_identity).hexdigest(),
        },
        "profiles": profiles,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the deterministic offline CAPTURE-vs-expert palette evaluation."
    )
    parser.add_argument("--output", required=True)
    parsed = parser.parse_args(argv)
    destination = Path(parsed.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(
            evaluate(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
