from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import nbformat
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from onec_runtime_mcp.agent.contracts import BackendExecution, CapabilityMode, RuntimeDescriptor
from onec_runtime_mcp.agent.service import AgentWorkspaceService
from onec_runtime_mcp.agent.service_server import AgentControlServer


class FakeRuntimeBackend:
    runtime_id = "runtime-test"

    def execute_bsl(self, source: str) -> BackendExecution:
        return BackendExecution.completed(messages=(), result_present=False)

    def status(self) -> RuntimeDescriptor:
        return RuntimeDescriptor(self.runtime_id, 1, "ready", CapabilityMode.EXPERIMENT)

    def close(self) -> None:
        return None


class FakeRuntimeFactory:
    def start(self, *, mode: CapabilityMode) -> FakeRuntimeBackend:
        return FakeRuntimeBackend()


def _seed_service(tmp_path: Path) -> AgentControlServer:
    source = "Пароль = 'secret source; platform-target-uuid';"
    cell = nbformat.v4.new_code_cell(source=source, id="cell-main")
    cell.metadata["onec_runtime"] = {
        "revision": 1,
        "language": "bsl",
        "mode": "main",
        "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
    }
    nbformat.write(nbformat.v4.new_notebook(cells=[cell]), tmp_path / "demo.ipynb")
    service = AgentWorkspaceService(tmp_path, FakeRuntimeFactory(), maximum_mode=CapabilityMode.EXPERIMENT)
    server = AgentControlServer(service, tmp_path)
    server.start()
    return server


async def _stdio_snapshot(parameters: StdioServerParameters) -> tuple[dict[str, object], dict[str, object], str]:
    with tempfile.TemporaryFile(mode="w+") as stderr:
        async with stdio_client(parameters, errlog=stderr) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                status = await session.call_tool("workspace.status", {})
                listed = await session.call_tool("code.list", {"container": "demo.ipynb"})
        stderr.seek(0)
        stderr_text = stderr.read()
    assert status.structured_content is not None
    assert listed.structured_content is not None
    return status.structured_content, listed.structured_content, stderr_text


def _raw_stdio_tools(parameters: StdioServerParameters) -> bytes:
    process = subprocess.Popen(
        [parameters.command, *parameters.args],
        cwd=parameters.cwd,
        env=os.environ | (parameters.env or {}),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    try:
        for message in (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "workspace.status", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "code.list", "arguments": {"container": "demo.ipynb"}}},
        ):
            process.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
            process.stdin.flush()
        responses = [process.stdout.readline() for _ in range(4)]
        assert all(responses)
        return b"".join(responses)
    finally:
        process.stdin.close()
        process.wait(timeout=10)


def test_mcp_stdio_reconnect_preserves_service_and_revision_without_protocol_leaks(tmp_path: Path) -> None:
    service_server = _seed_service(tmp_path)
    descriptor = str(service_server.endpoint.descriptor_path)
    source_root = str(Path(__file__).resolve().parents[2] / "src")
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "onec_runtime_mcp.mcp_entrypoint", "--workspace", str(tmp_path)],
        cwd=tmp_path,
        env={
            "ONEC_RUNTIME_SERVICE_DESCRIPTOR": descriptor,
            "ONEC_RUNTIME_CONTROL_TIMEOUT_S": "180",
            "PYTHONPATH": source_root,
        },
    )
    try:
        first_status, first_list, first_stderr = asyncio.run(_stdio_snapshot(parameters))
        second_status, second_list, second_stderr = asyncio.run(_stdio_snapshot(parameters))
        raw_stdout = _raw_stdio_tools(parameters)
    finally:
        service_server.close()

    assert first_status["ok"] is True
    assert second_status["ok"] is True
    assert first_status["value"]["service_instance_id"] == second_status["value"]["service_instance_id"]
    assert first_list["value"] == second_list["value"]
    assert first_list["value"] == [{
        "cell_id": "cell-main",
        "revision": 1,
        "language": "bsl",
            "mode": "main",
            "source_sha256": hashlib.sha256("Пароль = 'secret source; platform-target-uuid';".encode()).hexdigest(),
            "outputs": [],
        }]
    protocol_bytes = raw_stdout + json.dumps(
        [first_status, first_list, second_status, second_list, first_stderr, second_stderr],
        ensure_ascii=False,
    ).encode("utf-8")
    protocol_lower = protocol_bytes.lower()
    for forbidden in (
        service_server.token,
        "secret source",
        "platform-target-uuid",
        "rdbg",
        "ONEC_RUNTIME_CONTROL_TIMEOUT_S",
    ):
        assert forbidden.encode("utf-8").lower() not in protocol_lower
    assert str(os.getpid()).encode("utf-8") not in protocol_bytes


def test_python_proxy_survives_mcp_stdio_frontend_reconnect(tmp_path: Path) -> None:
    service_server = _seed_service(tmp_path)
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "onec_runtime_mcp.mcp_entrypoint",
            "--workspace",
            str(tmp_path),
            "--profile",
            "expert",
        ],
        cwd=tmp_path,
        env={
            "ONEC_RUNTIME_SERVICE_DESCRIPTOR": str(
                service_server.endpoint.descriptor_path
            ),
            "ONEC_RUNTIME_CONTROL_TIMEOUT_S": "180",
            "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        },
    )

    async def run_first() -> str:
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(
                    "python.run",
                    {
                        "code": "private_value = 40\nanswer = private_value + 2",
                        "inputs": {},
                        "outputs": ["answer"],
                    },
                )
                assert result.structured_content is not None
                return result.structured_content["value"]["outputs"]["answer"][
                    "proxy_id"
                ]

    async def inspect_second(proxy_id: str) -> tuple[dict[str, object], dict[str, object]]:
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                variables = await session.call_tool("python.variables", {})
                inspection = await session.call_tool(
                    "python.inspect", {"proxy_id": proxy_id}
                )
                assert variables.structured_content is not None
                assert inspection.structured_content is not None
                return variables.structured_content, inspection.structured_content

    try:
        proxy_id = asyncio.run(run_first())
        variables, inspection = asyncio.run(inspect_second(proxy_id))
    finally:
        service_server.close()

    assert [item["qualified_name"] for item in variables["value"]] == [
        "python.answer"
    ]
    assert inspection["value"]["preview"] == 42
