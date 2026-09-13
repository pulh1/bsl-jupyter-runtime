"""Stdio entry point for the replaceable MCP frontend."""

from __future__ import annotations

import argparse
from math import isfinite
import os
import sys
from collections.abc import Mapping
from pathlib import Path

import httpx

from onec_runtime_mcp.agent.control_protocol import ControlEndpoint, ControlProtocolError
from onec_runtime_mcp.agent.service_client import ServiceClient
from onec_runtime_mcp.agent.mcp_profiles import McpProfile
from onec_runtime_mcp.server import create_mcp_server


DEFAULT_CONTROL_TIMEOUT_S = 10.0
MAX_CONTROL_TIMEOUT_S = 180.0


def _configured_control_timeout_s() -> float:
    raw = os.environ.get("ONEC_RUNTIME_CONTROL_TIMEOUT_S")
    if raw is None:
        return DEFAULT_CONTROL_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError as error:
        raise ValueError("control timeout must be a number") from error
    if not isfinite(value) or value <= 0 or value > MAX_CONTROL_TIMEOUT_S:
        raise ValueError(
            f"control timeout must be finite and within (0, {MAX_CONTROL_TIMEOUT_S}]"
        )
    return value


def _configured_mcp_profile() -> McpProfile:
    raw = os.environ.get("ONEC_RUNTIME_MCP_PROFILE", McpProfile.AGENT.value)
    try:
        return McpProfile(raw)
    except ValueError as error:
        raise ValueError("MCP profile must be 'agent' or 'expert'") from error


def _descriptor_path(workspace: Path) -> Path:
    configured = os.environ.get("ONEC_RUNTIME_SERVICE_DESCRIPTOR")
    if configured:
        return Path(configured)
    return workspace / ".runtime" / "agent-service" / "control" / "endpoint.json"


def _connect_existing_service(workspace: Path) -> ServiceClient:
    endpoint = ControlEndpoint.read(_descriptor_path(workspace))
    response = httpx.get(endpoint.url + "/health", timeout=2.0)
    if response.status_code != 200:
        raise RuntimeError("control service health check failed")
    caller_id = os.environ.get("ONEC_RUNTIME_CALLER_ID")
    client = ServiceClient(
        endpoint,
        caller_id=caller_id or None,
        timeout_s=_configured_control_timeout_s(),
    )
    try:
        status = client.call("workspace.status", {})
    except RuntimeError as error:
        raise ValueError("service descriptor authentication failed") from error
    value = status.value
    if (
        not status.ok
        or not isinstance(value, Mapping)
        or value.get("service_instance_id") != endpoint.service_instance_id
    ):
        raise ValueError("service descriptor does not identify the running service")
    return client


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="onec-runtime-mcp")
    parser.add_argument("--workspace", default=str(Path.cwd()))
    parser.add_argument("--profile", choices=tuple(McpProfile), default=None)
    parsed = parser.parse_args(argv)
    try:
        profile = (
            McpProfile(parsed.profile)
            if parsed.profile is not None
            else _configured_mcp_profile()
        )
        workspace = Path(parsed.workspace).resolve(strict=True)
        client = _connect_existing_service(workspace)
    except (ControlProtocolError, OSError, ValueError, RuntimeError, httpx.HTTPError):
        print(
            "onec-runtime-mcp: no reachable agent runtime service; start "
            "onec-runtime-service for this workspace and retry.",
            file=sys.stderr,
        )
        return 2
    create_mcp_server(client, profile=profile).run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
