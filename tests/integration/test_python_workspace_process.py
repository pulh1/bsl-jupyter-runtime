from __future__ import annotations

from pathlib import Path
import time

import psutil
import pytest

from onec_runtime_mcp.agent.proxies import StaleProxy
from onec_runtime_mcp.agent.proxies import ProxyRegistry
from onec_runtime_mcp.agent.python_protocol import PythonWorkspaceLimits
from onec_runtime_mcp.agent.python_workspace import PythonWorkspace
from onec_runtime.errors import CommandTimeout, ProtocolError


def limits(*, timeout: float = 5.0) -> PythonWorkspaceLimits:
    return PythonWorkspaceLimits(
        timeout_seconds=timeout,
        max_code_bytes=64 * 1024,
        max_request_bytes=256 * 1024,
        max_response_bytes=256 * 1024,
        max_stdout_bytes=4096,
        max_stderr_bytes=4096,
        max_variables=100,
        allowed_imports=("pandas", "numpy", "math", "time"),
    )


def test_real_worker_keeps_dataframe_and_closes_exact_process(tmp_path: Path) -> None:
    workspace = PythonWorkspace.start(tmp_path, tmp_path / ".runtime", limits())
    try:
        status = workspace.status()
        process = psutil.Process(status.pid)
        identity = (process.pid, process.create_time(), process.exe())

        result = workspace.run(
            "import pandas as pd\nframe = pd.DataFrame({'group': ['a', 'a', 'b'], 'v': [1, 2, 4]})\n"
            "totals = frame.groupby('group', as_index=False)['v'].sum()",
            inputs={},
            outputs=("frame", "totals"),
        )
        inspected = workspace.inspect(result.outputs["totals"].proxy_id)

        assert inspected.type_name == "pandas.DataFrame"
        assert inspected.shape == (2, 2)
    finally:
        workspace.close()

    with pytest.raises(psutil.NoSuchProcess):
        current = psutil.Process(identity[0])
        if (current.create_time(), current.exe()) == identity:
            raise AssertionError("owned Python worker survived close")
        raise psutil.NoSuchProcess(identity[0])


def test_timeout_terminates_generation_and_invalidates_outputs(tmp_path: Path) -> None:
    workspace = PythonWorkspace.start(
        tmp_path,
        tmp_path / ".runtime",
        limits(timeout=0.2),
    )
    proxy = workspace.run("x = 1", inputs={}, outputs=("x",)).outputs["x"]

    with pytest.raises(CommandTimeout):
        workspace.run(
            "import time\ntime.sleep(2)\ny = 2",
            inputs={},
            outputs=("y",),
        )

    with pytest.raises(StaleProxy):
        workspace.inspect(proxy.proxy_id)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and psutil.pid_exists(workspace.worker_pid):
        time.sleep(0.02)
    assert not psutil.pid_exists(workspace.worker_pid)
    workspace.close()


def test_worker_crash_then_restart_advances_generation_and_keeps_old_proxy_stale(
    tmp_path: Path,
) -> None:
    registry = ProxyRegistry()
    first = PythonWorkspace.start(
        tmp_path, tmp_path / ".runtime", limits(), registry=registry
    )
    proxy = first.run("x = 1", inputs={}, outputs=("x",)).outputs["x"]
    old_generation = first.generation
    process = psutil.Process(first.worker_pid)
    process.terminate()
    process.wait(timeout=2.0)

    with pytest.raises(ProtocolError, match="lost"):
        first.status()
    first.close()

    second = PythonWorkspace.start(
        tmp_path, tmp_path / ".runtime", limits(), registry=registry
    )
    try:
        assert second.generation > old_generation
        with pytest.raises(StaleProxy):
            second.inspect(proxy.proxy_id)
    finally:
        second.close()
