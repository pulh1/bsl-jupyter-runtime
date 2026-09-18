from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Event

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from onec_runtime_mcp.agent.contracts import BackendExecution, CapabilityMode, RuntimeDescriptor
from onec_runtime_mcp.agent.mcp_profiles import AGENT_TOOL_NAMES, EXPERT_TOOL_NAMES
from onec_runtime_mcp.agent.service import AgentWorkspaceService
from onec_runtime_mcp.agent.service_server import AgentControlServer
from onec_runtime.runtime_models import RuntimeNamespaceSnapshot


class _OfflineBackend:
    runtime_id = "runtime-offline"

    def __init__(self) -> None:
        self.closed = False

    def execute_bsl(self, source: str) -> BackendExecution:
        return BackendExecution.completed(messages=(), result_present=False)

    def status(self) -> RuntimeDescriptor:
        return RuntimeDescriptor(
            self.runtime_id,
            1,
            "ready",
            CapabilityMode.EXPERIMENT,
        )

    def namespace_snapshot(self) -> RuntimeNamespaceSnapshot:
        return RuntimeNamespaceSnapshot(1, 1, ())

    def close(self) -> None:
        self.closed = True


class _BlockingOfflineFactory:
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()
        self.start_count = 0
        self.backend = _OfflineBackend()

    def start(self, *, mode: CapabilityMode) -> _OfflineBackend:
        assert mode is CapabilityMode.EXPERIMENT
        self.start_count += 1
        self.started.set()
        assert self.release.wait(timeout=10)
        return self.backend


def _parameters(
    workspace: Path,
    server: AgentControlServer,
    *,
    profile: str,
) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "onec_runtime_mcp.mcp_entrypoint",
            "--workspace",
            str(workspace),
            "--profile",
            profile,
        ],
        cwd=workspace,
        env={
            "ONEC_RUNTIME_SERVICE_DESCRIPTOR": str(server.endpoint.descriptor_path),
            "ONEC_RUNTIME_CONTROL_TIMEOUT_S": "10",
            "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        },
    )


async def _call_tool(
    parameters: StdioServerParameters,
    name: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool(name, arguments)
    assert result.structured_content is not None
    return result.structured_content


async def _listed_tools(parameters: StdioServerParameters) -> tuple[object, ...]:
    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            return tuple((await session.list_tools()).tools)


def test_two_sequential_mcp_frontends_join_one_offline_startup_and_close_owned_runtime(
    tmp_path: Path,
) -> None:
    factory = _BlockingOfflineFactory()
    service = AgentWorkspaceService(
        tmp_path,
        factory,
        maximum_mode=CapabilityMode.EXPERIMENT,
    )
    server = AgentControlServer(service, tmp_path)
    server.start()
    parameters = _parameters(tmp_path, server, profile="agent")
    try:
        first = asyncio.run(
            _call_tool(
                parameters,
                "runtime.ensure",
                {"profile": "offline", "mode": "experiment"},
            )
        )
        assert factory.started.wait(timeout=5)
        second = asyncio.run(
            _call_tool(
                parameters,
                "runtime.ensure",
                {"profile": "offline", "mode": "experiment"},
            )
        )

        assert first["ok"] is True
        assert second["ok"] is True
        first_view = first["value"]
        second_view = second["value"]
        assert isinstance(first_view, dict)
        assert isinstance(second_view, dict)
        assert first_view["operation"]["operation_id"] == second_view["operation"]["operation_id"]
        assert first_view["state"] in {"queued", "running"}
        assert second_view["state"] in {"queued", "running"}
        assert factory.start_count == 1

        factory.release.set()
        terminal = asyncio.run(
            _call_tool(
                parameters,
                "operation.wait",
                {
                    "operation_id": first_view["operation"]["operation_id"],
                    "timeout_s": 5,
                    "after_message_cursor": 0,
                },
            )
        )
        assert terminal["ok"] is True
        assert terminal["value"]["state"] == "completed"
    finally:
        factory.release.set()
        server.close()

    assert factory.backend.closed is True
    assert not (tmp_path / ".runtime" / "agent-service" / "runtime-owner.json").exists()


def test_stdio_profiles_are_isolated_and_do_not_leak_service_private_data(
    tmp_path: Path,
) -> None:
    service = AgentWorkspaceService(
        tmp_path,
        _BlockingOfflineFactory(),
        maximum_mode=CapabilityMode.EXPERIMENT,
    )
    server = AgentControlServer(service, tmp_path)
    server.start()
    try:
        agent = asyncio.run(_listed_tools(_parameters(tmp_path, server, profile="agent")))
        expert = asyncio.run(_listed_tools(_parameters(tmp_path, server, profile="expert")))
    finally:
        server.close()

    agent_names = {tool.name for tool in agent}
    expert_names = {tool.name for tool in expert}
    assert agent_names == AGENT_TOOL_NAMES
    assert expert_names == EXPERT_TOOL_NAMES
    assert agent_names < expert_names
    assert "capture.run_until" not in expert_names
    metadata = json.dumps(
        [tool.model_dump(mode="json") for tool in (*agent, *expert)],
        ensure_ascii=False,
    ).lower()
    for private in (server.token, str(os.getpid()), "rdbg", "runtime-owner.json"):
        assert private.lower() not in metadata


def test_recovered_unknown_startup_remains_quarantined_through_agent_stdio(
    tmp_path: Path,
) -> None:
    operation_id = "startup-before-process-restart"
    journal = tmp_path / ".runtime" / "agent-service" / "operations.jsonl"
    journal.parent.mkdir(parents=True)
    journal.write_text(
        "".join(
            json.dumps(event, separators=(",", ":")) + "\n"
            for event in (
                {
                    "cursor": 1,
                    "event": "submitted",
                    "operation_id": operation_id,
                    "operation_kind": "runtime_ensure",
                    "runtime_id": "",
                    "runtime_generation": None,
                    "code_id": None,
                    "revision": None,
                    "source_sha256": None,
                    "inputs_sha256": "a" * 64,
                    "observation": None,
                    "startup_profile": "offline",
                    "startup_mode": "experiment",
                },
                {"cursor": 2, "event": "started", "operation_id": operation_id},
            )
        ),
        encoding="utf-8",
    )
    factory = _BlockingOfflineFactory()
    service = AgentWorkspaceService(
        tmp_path,
        factory,
        maximum_mode=CapabilityMode.EXPERIMENT,
    )
    server = AgentControlServer(service, tmp_path)
    server.start()
    try:
        result = asyncio.run(
            _call_tool(
                _parameters(tmp_path, server, profile="agent"),
                "runtime.ensure",
                {"profile": "offline", "mode": "experiment"},
            )
        )
    finally:
        server.close()

    assert result["ok"] is True
    assert result["value"]["operation"]["operation_id"] == operation_id
    assert result["value"]["state"] == "unknown"
    assert factory.start_count == 0


def test_palette_measurement_is_deterministic_without_a_runtime(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[2] / "tools" / "measure_mcp_palette.py"
    outputs: list[dict[str, object]] = []
    for profile in ("agent", "expert"):
        destination = tmp_path / f"{profile}.json"
        result = subprocess.run(
            [
                sys.executable,
                str(script),
                "--profile",
                profile,
                "--output",
                str(destination),
            ],
            cwd=Path(__file__).resolve().parents[2],
            env=os.environ | {"PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        outputs.append(json.loads(destination.read_text(encoding="utf-8")))

    repeated = tmp_path / "agent-repeated.json"
    subprocess.run(
        [sys.executable, str(script), "--profile", "agent", "--output", str(repeated)],
        cwd=Path(__file__).resolve().parents[2],
        env=os.environ | {"PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert json.loads(repeated.read_text(encoding="utf-8")) == outputs[0]
    assert outputs[0]["profile"] == "agent"
    assert outputs[0]["tool_count"] == len(AGENT_TOOL_NAMES)
    assert outputs[1]["profile"] == "expert"
    assert outputs[1]["tool_count"] == len(EXPERT_TOOL_NAMES)
    for value in outputs:
        assert type(value["schema_bytes"]) is int and value["schema_bytes"] > 0
        assert type(value["schema_tokens"]) is int and value["schema_tokens"] > 0
        assert isinstance(value["schema_sha256"], str) and len(value["schema_sha256"]) == 64
