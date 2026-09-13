"""Static MCP exposure profiles over the stable Domain and Agent APIs."""

from __future__ import annotations

from enum import StrEnum


class McpProfile(StrEnum):
    AGENT = "agent"
    CAPTURE = "capture"
    EXPERT = "expert"


AGENT_TOOL_NAMES = frozenset(
    {
        "workspace.open",
        "workspace.status",
        "workspace.variables",
        "runtime.ensure",
        "runtime.restart",
        "runtime.close",
        "code.list",
        "code.get",
        "code.put",
        "code.run",
        "code.run_inline",
        "operation.wait",
        "value.inspect",
        "value.materialize",
        "value.to_df",
        "python.run",
    }
)


EXPERT_TOOL_NAMES = frozenset(
    {
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
        "value.inspect", "value.describe", "value.size", "value.preview", "value.get",
        "value.select", "value.snapshot", "value.materialize", "value.to_df", "value.compare",
        "value.release",
        "python.variables", "python.run", "python.inspect", "python.imports", "python.reset",
        "python.status",
    }
)


CAPTURE_TOOL_NAMES = AGENT_TOOL_NAMES | frozenset(
    {"capture.run_until", "capture.inspect", "capture.stack", "capture.frame", "capture.hypothesis", "capture.continue"}
)


def tool_names(profile: McpProfile | str) -> frozenset[str]:
    try:
        normalized = profile if isinstance(profile, McpProfile) else McpProfile(profile)
    except (TypeError, ValueError) as error:
        raise ValueError("unknown MCP profile") from error
    if normalized is McpProfile.AGENT:
        return AGENT_TOOL_NAMES
    if normalized is McpProfile.CAPTURE:
        return CAPTURE_TOOL_NAMES
    return EXPERT_TOOL_NAMES


__all__ = ["AGENT_TOOL_NAMES", "CAPTURE_TOOL_NAMES", "EXPERT_TOOL_NAMES", "McpProfile", "tool_names"]
