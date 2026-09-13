"""Foreground entry point; deliberately does not attempt detached startup."""

from __future__ import annotations

import argparse
import atexit
import os
import signal
from pathlib import Path
from threading import Lock

from onec_runtime_mcp.agent.contracts import CapabilityMode
from onec_runtime_mcp.agent.runtime_backend import OnecRuntimeFactory
from onec_runtime_mcp.agent.service import AgentWorkspaceService
from onec_runtime_mcp.agent.service_server import AgentControlServer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="onec-runtime-service")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--maximum-mode", choices=[item.value for item in CapabilityMode], default=CapabilityMode.OBSERVE.value)
    parser.add_argument("--service-descriptor")
    parsed = parser.parse_args(argv)
    workspace = Path(parsed.workspace).resolve(strict=True)
    service = AgentWorkspaceService(workspace, OnecRuntimeFactory(workspace), maximum_mode=CapabilityMode(parsed.maximum_mode))
    server = AgentControlServer(service, workspace, descriptor_path=parsed.service_descriptor)
    once = Lock(); closed = False
    def close_once(*_args: object) -> None:
        nonlocal closed
        with once:
            if closed: return
            closed = True
        server.close()
    atexit.register(close_once)
    signal.signal(signal.SIGINT, close_once)
    signal.signal(signal.SIGTERM, close_once)
    server.start()
    server.wait_closed()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
