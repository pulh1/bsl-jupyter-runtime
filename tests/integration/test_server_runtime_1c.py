"""Opt-in smoke against a dedicated, prepared client-server infobase."""
from __future__ import annotations

import os
from pathlib import Path

import psutil
import pytest

from onec_runtime.config import RuntimeConfig
from onec_runtime.session import ExtensionMode, RuntimeSession, RuntimeSessionConfig


@pytest.mark.live_1c
@pytest.mark.integration
def test_server_runtime_executes_and_preserves_shared_services(tmp_path: Path) -> None:
    if os.environ.get("ONEC_SERVER_LIVE") != "1":
        pytest.skip("Set ONEC_SERVER_LIVE=1 for the prepared dedicated server base")
    for key in ("ONEC_PLATFORM_BIN", "ONEC_DEMO_CONNECTION_STRING", "ONEC_DEMO_USER"):
        if not os.environ.get(key):
            pytest.fail(f"Missing environment variable {key}")
    shared = {
        process.pid: process.create_time()
        for process in psutil.process_iter(["name"])
        if (process.info["name"] or "").casefold()
        in {"dbgs.exe", "ragent.exe", "rmngr.exe", "rphost.exe"}
    }
    runtime = RuntimeSession.start(RuntimeSessionConfig(
        RuntimeConfig(
            tmp_path, Path(os.environ["ONEC_PLATFORM_BIN"]),
            connection_string=os.environ["ONEC_DEMO_CONNECTION_STRING"],
            username=os.environ["ONEC_DEMO_USER"],
            password=os.environ.get("ONEC_DEMO_PASSWORD", ""),
            debug_host=os.environ.get("ONEC_RUNTIME_DEBUG_HOST", "127.0.0.1"),
            debug_port=int(os.environ.get("ONEC_RUNTIME_DEBUG_PORT", "1550")),
        ),
        tmp_path / "evidence", extension_mode=ExtensionMode.MANUAL,
    ))
    try:
        owned = runtime.owned_process_snapshot()
        assert len(owned) == 1
        reply = runtime.execute_bsl("СохраненноеЗначение = 40; Результат = СохраненноеЗначение + 2;")
        assert reply.succeeded and int(reply.result) == 42
        reply = runtime.execute_bsl("Результат = СохраненноеЗначение + 3;")
        assert reply.succeeded and int(reply.result) == 43
    finally:
        runtime.close()
    runtime.close()  # Closing an already closed server session is harmless.
    for identity in owned:
        assert not psutil.pid_exists(identity["pid"])
    for pid, created in shared.items():
        assert psutil.Process(pid).create_time() == created
