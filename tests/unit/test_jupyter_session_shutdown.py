"""Owned runtime cleanup must follow kernel lifetime, not frontend lifetime."""

import json
import os
import subprocess
import sys
from pathlib import Path

from IPython.core.interactiveshell import InteractiveShell
from jupyter_client import KernelManager
from traitlets.config import Config
import pytest

from onec_runtime.runtime_api import RuntimeNamespaceSnapshot
from onec_runtime_jupyter import InteractiveRuntimeSession, install_runtime
from onec_runtime_jupyter import session as session_module


class RuntimeResource:
    """Replace the external 1C resource while exercising real adapter hooks."""

    def __init__(self) -> None:
        self.closes = 0
        self.fail = False

    def namespace_snapshot(self):
        return RuntimeNamespaceSnapshot(1, 1, ())

    def require_public_value_handle(self, handle):
        pass

    def close(self):
        self.closes += 1
        if self.fail:
            raise RuntimeError("secret token=private-connection")


def start_owned(monkeypatch, shell, runtime):
    monkeypatch.setattr(session_module.RuntimeSession, "start", lambda config, *, progress: runtime)
    return InteractiveRuntimeSession.start(object(), shell=shell)


def test_start_replaces_owned_session_and_shutdown_closes_current_once(monkeypatch):
    shell = InteractiveShell()
    first, second = RuntimeResource(), RuntimeResource()
    owners = [start_owned(monkeypatch, shell, first), start_owned(monkeypatch, shell, second)]
    try:
        assert first.closes == 1
        assert second.closes == 0
        shell.exit_now = True
        assert first.closes == second.closes == 1
        for owner in owners:
            owner.close()
        assert first.closes == second.closes == 1
    finally:
        for owner in owners:
            owner.close()


def test_replacement_closes_previous_before_starting_next(monkeypatch):
    shell = InteractiveShell()
    first, second = RuntimeResource(), RuntimeResource()
    owners = [start_owned(monkeypatch, shell, first)]
    try:
        def start_next(config, *, progress):
            assert first.closes == 1
            return second

        monkeypatch.setattr(session_module.RuntimeSession, "start", start_next)
        owners.append(InteractiveRuntimeSession.start(object(), shell=shell))
        assert second.closes == 0
    finally:
        for owner in owners:
            owner.close()


def test_failed_previous_close_prevents_replacement_start(monkeypatch):
    shell = InteractiveShell()
    first = RuntimeResource()
    owner = start_owned(monkeypatch, shell, first)
    first.fail = True
    try:
        def unexpected_start(config, *, progress):
            pytest.fail("replacement started before previous cleanup succeeded")

        monkeypatch.setattr(session_module.RuntimeSession, "start", unexpected_start)
        with pytest.raises(RuntimeError, match="secret token"):
            InteractiveRuntimeSession.start(object(), shell=shell)
        assert first.closes == 1
    finally:
        first.fail = False
        owner.close()


def test_start_in_another_shell_keeps_first_session_open(monkeypatch):
    first, second = RuntimeResource(), RuntimeResource()
    owners = [start_owned(monkeypatch, InteractiveShell(), first)]
    try:
        owners.append(start_owned(monkeypatch, InteractiveShell(), second))
        assert first.closes == second.closes == 0
    finally:
        for owner in owners:
            owner.close()


def test_replacement_does_not_close_externally_installed_runtime(monkeypatch):
    shell = InteractiveShell()
    external, owned = RuntimeResource(), RuntimeResource()
    install_runtime(shell, external)
    owner = start_owned(monkeypatch, shell, owned)
    try:
        assert external.closes == 0
        assert owned.closes == 0
    finally:
        owner.close()


def test_failed_new_start_leaves_previous_owner_closed(monkeypatch):
    shell = InteractiveShell()
    previous = RuntimeResource()
    owner = start_owned(monkeypatch, shell, previous)
    try:
        def fail_start(config, *, progress):
            raise RuntimeError("new start failed")

        monkeypatch.setattr(session_module.RuntimeSession, "start", fail_start)
        with pytest.raises(RuntimeError, match="new start failed"):
            InteractiveRuntimeSession.start(object(), shell=shell)
        assert previous.closes == 1
        shell.exit_now = True
        assert previous.closes == 1
    finally:
        owner.close()


def test_explicit_close_unregisters_shutdown_cleanup(monkeypatch):
    shell, runtime = InteractiveShell(), RuntimeResource()
    with start_owned(monkeypatch, shell, runtime):
        pass
    shell.exit_now = True
    assert runtime.closes == 1


def test_shell_shutdown_uses_runtime_shutdown_cleanup_before_ordinary_close(monkeypatch):
    class ShutdownResource(RuntimeResource):
        def __init__(self) -> None:
            super().__init__()
            self.shutdown_closes = 0

        def close_for_kernel_shutdown(self) -> None:
            self.shutdown_closes += 1

    shell, runtime = InteractiveShell(), ShutdownResource()
    owner = start_owned(monkeypatch, shell, runtime)
    shell.exit_now = True
    assert runtime.shutdown_closes == 1
    assert runtime.closes == 0
    owner.close()
    assert runtime.shutdown_closes == 1
    assert runtime.closes == 0


def test_packaged_jupyter_config_allows_server_session_cleanup_to_finish():
    config_path = (
        Path(__file__).resolve().parents[2]
        / "packages/jupyter/jupyter-config/onec-bsl.json"
    )
    manager = KernelManager(
        config=Config(json.loads(config_path.read_text(encoding="utf-8")))
    )
    # jupyter_client sends SIGTERM halfway through this budget.
    assert manager.shutdown_wait_time >= 120


def test_external_runtime_is_not_automatically_owned():
    shell, runtime = InteractiveShell(), RuntimeResource()
    install_runtime(shell, runtime)
    wrapper = InteractiveRuntimeSession(runtime)
    shell.exit_now = True
    assert runtime.closes == 0
    wrapper.close()
    assert runtime.closes == 1


def test_shutdown_failure_is_safe_and_retryable(monkeypatch, caplog):
    shell = InteractiveShell()
    failed = RuntimeResource()
    failed.fail = True
    owner = start_owned(monkeypatch, shell, failed)
    try:
        shell.exit_now = True
        assert failed.closes == 1
        assert caplog.records
        assert "cleanup" in caplog.text.lower()
        assert "secret" not in caplog.text
        assert "private-connection" not in caplog.text
        failed.fail = False
        owner.close()
        owner.close()
        assert failed.closes == 2
    finally:
        failed.fail = False
        owner.close()


def test_guardian_stops_only_after_runtime_cleanup_succeeds(monkeypatch):
    shell, runtime = InteractiveShell(), RuntimeResource()
    stopped: list[str] = []
    monkeypatch.setattr(
        session_module, "start_guardian",
        lambda _runtime: type("Guard", (), {"stop": lambda self: stopped.append("stop")})(),
        raising=False,
    )
    owner = start_owned(monkeypatch, shell, runtime)
    runtime.fail = True
    with pytest.raises(RuntimeError, match="secret token"):
        owner.close()
    assert stopped == []
    runtime.fail = False
    owner.close()
    assert stopped == ["stop"]


def test_guardian_start_failure_closes_new_runtime(monkeypatch):
    shell, runtime = InteractiveShell(), RuntimeResource()
    monkeypatch.setattr(
        session_module, "start_guardian",
        lambda _runtime: (_ for _ in ()).throw(RuntimeError("guardian unavailable")),
        raising=False,
    )
    monkeypatch.setattr(session_module.RuntimeSession, "start", lambda config, *, progress: runtime)
    with pytest.raises(RuntimeError, match="guardian unavailable"):
        InteractiveRuntimeSession.start(object(), shell=shell)
    assert runtime.closes == 1


def test_failed_prior_guard_is_retried_before_starting_next_runtime(monkeypatch):
    shell, runtime = InteractiveShell(), RuntimeResource()
    events: list[str] = []
    monkeypatch.setattr(
        session_module, "recover_failed_guards",
        lambda _config: events.append("recover") or (),
        raising=False,
    )

    def start_next(config, *, progress):
        events.append("start")
        return runtime

    monkeypatch.setattr(session_module.RuntimeSession, "start", start_next)
    owner = InteractiveRuntimeSession.start(object(), shell=shell)
    try:
        assert events == ["recover", "start"]
    finally:
        owner.close()


def environment():
    workspace = Path(__file__).resolve().parents[2]
    result = dict(os.environ)
    result["PYTHONPATH"] = os.pathsep.join(str(workspace / path) for path in (
        "src", "packages/jupyter/src", "packages/mcp/src",
    ))
    result["JUPYTER_PLATFORM_DIRS"] = "1"
    result["PYDEVD_DISABLE_FILE_VALIDATION"] = "1"
    return result


def setup_code(marker: Path, *, close_delay: float = 0) -> str:
    return f'''
from pathlib import Path
from unittest.mock import patch
from onec_runtime.runtime_api import RuntimeNamespaceSnapshot
from onec_runtime_jupyter import InteractiveRuntimeSession
from IPython.core.interactiveshell import InteractiveShell
class Resource:
    def namespace_snapshot(self):
        return RuntimeNamespaceSnapshot(1, 1, ())
    def require_public_value_handle(self, handle):
        pass
    def close(self):
        import time
        time.sleep({close_delay!r})
        with Path({str(marker)!r}).open("a") as output:
            output.write("closed\\n")
with patch("onec_runtime_jupyter.session.RuntimeSession.start", return_value=Resource()):
    owner = InteractiveRuntimeSession.start(object(), shell=InteractiveShell.instance())
'''


def test_normal_python_exit_closes_owned_runtime(tmp_path):
    marker = tmp_path / "closed.txt"
    result = subprocess.run(
        [sys.executable, "-c", setup_code(marker) + "\ndel owner\n"],
        env=environment(), capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert marker.exists(), "normal interpreter exit abandoned the owned runtime"
    assert marker.read_text() == "closed\n"


def test_failed_shell_cleanup_retries_at_python_exit_without_leaking_errors(tmp_path):
    marker = tmp_path / "closed.txt"
    code = setup_code(marker) + '''
original_close = owner.runtime.close
def fail_once():
    owner.runtime.close = original_close
    raise RuntimeError("secret token=private-connection")
owner.runtime.close = fail_once
InteractiveShell.instance().exit_now = True
'''
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=environment(), capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert marker.read_text() == "closed\n"
    assert "cleanup" in result.stderr.lower()
    assert "secret" not in result.stderr
    assert "private-connection" not in result.stderr


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("close_delay", [0, 3.5])
def test_real_kernel_shutdown_closes_before_interpreter_exit(tmp_path, restart, close_delay):
    marker = tmp_path / "closed.txt"
    exit_marker = tmp_path / "closed_before_atexit.txt"
    # The caller owns this deadline: jupyter_client sends SIGTERM halfway
    # through shutdown_wait_time. Give the deliberately slow resource 5 sec.
    # Keep the fast case on the unchanged upstream default as well.
    manager = KernelManager(**({"shutdown_wait_time": 10} if close_delay else {}))
    manager.kernel_spec.argv = [
        sys.executable, "-m", "ipykernel_launcher", "-f", "{connection_file}",
    ]
    manager.start_kernel(env=environment())
    client = manager.blocking_client()
    client.start_channels()
    try:
        client.wait_for_ready(timeout=20)
        code = setup_code(marker, close_delay=close_delay) + f'''
import atexit
atexit.register(lambda: Path({str(exit_marker)!r}).write_text(str(Path({str(marker)!r}).exists())))
'''
        reply = client.execute_interactive(code, timeout=20)
        assert reply["content"]["status"] == "ok", reply
        client.stop_channels()
        assert manager.is_alive()
        assert not marker.exists(), "frontend disconnect must preserve the runtime"
        client = manager.blocking_client()
        client.start_channels()
        client.wait_for_ready(timeout=20)
        reply = client.execute_interactive("assert owner.runtime is not None", timeout=20)
        assert reply["content"]["status"] == "ok", reply
        if restart:
            manager.restart_kernel(now=False)
        else:
            manager.shutdown_kernel(now=False)
        assert marker.exists(), "graceful kernel shutdown abandoned the owned runtime"
        assert marker.read_text() == "closed\n"
        assert exit_marker.read_text() == "True", "cleanup ran too late in atexit"
    finally:
        client.stop_channels()
        if manager.has_kernel:
            manager.shutdown_kernel(now=True)


def test_real_kernel_restart_waits_for_server_shutdown_cleanup(tmp_path):
    marker = tmp_path / "server-closed.txt"
    config_path = (
        Path(__file__).resolve().parents[2]
        / "packages/jupyter/jupyter-config/onec-bsl.json"
    )
    manager = KernelManager(
        config=Config(json.loads(config_path.read_text(encoding="utf-8")))
    )
    manager.kernel_spec.argv = [
        sys.executable, "-m", "ipykernel_launcher", "-f", "{connection_file}",
    ]
    manager.start_kernel(env=environment())
    client = manager.blocking_client()
    client.start_channels()
    try:
        client.wait_for_ready(timeout=20)
        code = setup_code(marker) + f'''
def close_for_kernel_shutdown():
    import time
    time.sleep(3.5)
    Path({str(marker)!r}).write_text("server-terminated\\n")
owner.runtime.close_for_kernel_shutdown = close_for_kernel_shutdown
'''
        reply = client.execute_interactive(code, timeout=20)
        assert reply["content"]["status"] == "ok", reply
        manager.restart_kernel(now=False)
        assert marker.read_text() == "server-terminated\n"
    finally:
        client.stop_channels()
        if manager.has_kernel:
            manager.shutdown_kernel(now=True)
