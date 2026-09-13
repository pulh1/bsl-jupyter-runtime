from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys
from time import monotonic, sleep

import nbformat

from onec_runtime_mcp.agent.service_client import ServiceClient


def test_foreground_service_reconnects_and_stops_on_authenticated_shutdown(tmp_path: Path) -> None:
    source = "Пароль = 'super-secret';"
    cell = nbformat.v4.new_code_cell(source=source, id="cell-main")
    cell.metadata["onec_runtime"] = {"revision": 1, "language": "bsl", "mode": "main", "source_sha256": hashlib.sha256(source.encode()).hexdigest()}
    nbformat.write(nbformat.v4.new_notebook(cells=[cell]), tmp_path / "demo.ipynb")
    env = os.environ | {"PYTHONPATH": str(Path.cwd() / "src")}
    process = subprocess.Popen([sys.executable, "-m", "onec_runtime_mcp.agent.service_entrypoint", "--workspace", str(tmp_path)], env=env)
    descriptor = tmp_path / ".runtime" / "agent-service" / "control" / "endpoint.json"
    deadline = monotonic() + 10
    while not descriptor.exists() and monotonic() < deadline: sleep(0.05)
    try:
        first = ServiceClient(descriptor, caller_id="first")
        opened = first.call("workspace.open", {})
        assert opened.ok
        assert first.call("code.list", {"container": "demo.ipynb"}).ok
        second = ServiceClient(descriptor, caller_id="second")
        assert second.call("workspace.status", {}).value["service_instance_id"] == first.call("workspace.status", {}).value["service_instance_id"]
        assert second.call("code.get", {"cell_id": "cell-main"}).value["source"] == source
        second.shutdown()
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None: process.kill(); process.wait(timeout=10)


def test_foreground_service_can_publish_to_probe_owned_descriptor(tmp_path: Path) -> None:
    env = os.environ | {"PYTHONPATH": str(Path.cwd() / "src")}
    descriptor = tmp_path / ".runtime" / "probe-owned" / "endpoint.json"
    default_descriptor = tmp_path / ".runtime" / "agent-service" / "control" / "endpoint.json"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "onec_runtime_mcp.agent.service_entrypoint",
            "--workspace",
            str(tmp_path),
            "--service-descriptor",
            str(descriptor),
        ],
        env=env,
    )
    deadline = monotonic() + 10
    while not descriptor.exists() and monotonic() < deadline:
        sleep(0.05)
    try:
        assert descriptor.is_file()
        assert not default_descriptor.exists()
        client = ServiceClient(descriptor, caller_id="owner")
        client.shutdown()
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
