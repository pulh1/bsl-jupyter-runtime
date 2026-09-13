from pathlib import Path
import re
import subprocess

import pytest

import onec_runtime.processes as processes_module
from onec_runtime.config import RuntimeConfig
from onec_runtime.processes import (
    FileModeProcesses,
    OwnedProcess,
    debuggee_command,
    read_debug_server_notification,
)


class FakeOwned:
    def __init__(self, name: str, calls: list[str]) -> None:
        self.name = name
        self.calls = calls

    def close(self, timeout_s: float = 10.0) -> None:
        self.calls.append(self.name)


class FakeClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


class TimeoutThenKilledProcess:
    pid = 1234

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.wait_timeouts: list[float] = []
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return -9 if self.killed else None

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float) -> int:
        self.wait_timeouts.append(timeout)
        if len(self.wait_timeouts) == 1:
            self.clock.value += timeout
            raise subprocess.TimeoutExpired("synthetic", timeout)
        return -9


class RecordingRunningProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.running = True

    def poll(self) -> int | None:
        return None if self.running else 0

    def terminate(self) -> None:
        self.running = False

    def kill(self) -> None:
        self.running = False

    def wait(self, timeout: float) -> int:
        del timeout
        self.running = False
        return 0


def runtime_config(tmp_path: Path) -> RuntimeConfig:
    platform = tmp_path / "bin"
    platform.mkdir()
    for name in ("1cv8.exe", "1cv8c.exe", "dbgs.exe"):
        (platform / name).touch()
    return RuntimeConfig(tmp_path, platform)


def test_reads_utf16_debug_server_notification(tmp_path: Path) -> None:
    notification = tmp_path / "notify.txt"
    notification.write_text("127.0.0.1:1550", encoding="utf-16")

    assert read_debug_server_notification(notification) == ("127.0.0.1", 1550)


def test_extension_debuggee_does_not_execute_external_epf(tmp_path: Path) -> None:
    platform = tmp_path / "bin"
    platform.mkdir()
    for name in ("1cv8.exe", "1cv8c.exe", "dbgs.exe"):
        (platform / name).touch()
    config = RuntimeConfig(tmp_path, platform)

    command = debuggee_command(config, 1550, execute_external=False)

    assert "/Execute" not in command
    assert "/Out" in command
    assert "/DisableStartupDialogs" in command
    assert "/DisableStartupMessages" in command
    assert "/DisplayPerformance" not in command


def test_start_debuggee_discards_stale_enterprise_messages(tmp_path: Path, monkeypatch) -> None:
    config = runtime_config(tmp_path)
    config.infobase_dir.mkdir(parents=True, exist_ok=True)
    (config.infobase_dir / "1Cv8.1CD").touch()
    log_path = config.logs_dir / "1c-messages.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("stale private login", encoding="utf-8")
    processes = FileModeProcesses(config)
    monkeypatch.setattr(processes, "_spawn", lambda *_args, **_kwargs: object())

    processes.start_debuggee(1550, execute_external=False)

    assert not log_path.exists()


def test_debuggee_receives_launch_identity_as_one_startup_parameter(tmp_path: Path) -> None:
    command = debuggee_command(runtime_config(tmp_path), 1550, execute_external=False,
                               startup_parameter="onec-runtime:0123456789abcdef")
    assert command[-2:] == ["/C", "onec-runtime:0123456789abcdef"]


@pytest.mark.parametrize("thick_client", (False, True))
def test_debuggee_pins_one_russian_platform_diagnostic_locale(
    tmp_path: Path,
    thick_client: bool,
) -> None:
    command = debuggee_command(
        runtime_config(tmp_path),
        1550,
        execute_external=False,
        thick_client=thick_client,
    )

    locale_flags = [
        item for item in command
        if re.fullmatch(r"/L[A-Za-z]{2}", item) is not None
    ]
    assert locale_flags == ["/Lru"]
    assert "/Len" not in command


def test_thin_client_command_uses_external_base_and_named_passwordless_user(
    tmp_path: Path,
) -> None:
    platform = tmp_path / "bin"
    platform.mkdir()
    for name in ("1cv8.exe", "1cv8c.exe", "dbgs.exe"):
        (platform / name).touch()
    external_base = tmp_path / "zup"
    external_base.mkdir()
    (external_base / "1Cv8.1CD").touch()
    username = "Савинская З.Ю. (Системный программист)"
    config = RuntimeConfig(
        tmp_path,
        platform,
        connection_string=f'File="{external_base}";',
        username=username,
    )

    command = debuggee_command(config, 1550, execute_external=False)

    assert command[0] == str(config.client_exe)
    assert command[command.index("/F") + 1] == str(external_base.resolve())
    assert command[command.index("/N") + 1] == username
    assert command[command.index("/P") + 1] == ""


def test_file_processes_can_close_debuggee_then_debug_server(tmp_path: Path) -> None:
    calls: list[str] = []
    processes = FileModeProcesses(runtime_config(tmp_path))
    processes.debuggee = FakeOwned("onec", calls)  # type: ignore[assignment]
    processes.debug_server = FakeOwned("dbgs", calls)  # type: ignore[assignment]

    processes.close_debuggee(timeout_s=10.0)
    assert processes.debuggee is None
    assert processes.debug_server is not None

    processes.close_debug_server(timeout_s=10.0)
    assert processes.debug_server is None
    assert calls == ["onec", "dbgs"]

    processes.close_debuggee()
    processes.close_debug_server()
    assert calls == ["onec", "dbgs"]


@pytest.mark.skipif(
    not hasattr(subprocess, "STARTUPINFO"),
    reason="Windows startup window flags are unavailable",
)
def test_windows_hides_enterprise_debuggee_but_not_debug_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = runtime_config(tmp_path)
    config.infobase_dir.mkdir(parents=True)
    (config.infobase_dir / "1Cv8.1CD").touch()
    calls: list[tuple[list[str], dict[str, object]]] = []

    def popen(
        command: list[str],
        **kwargs: object,
    ) -> RecordingRunningProcess:
        calls.append((command, kwargs))
        notification = next(
            (
                item.removeprefix("--notify=")
                for item in command
                if item.startswith("--notify=")
            ),
            None,
        )
        if notification is not None:
            Path(notification).write_text("127.0.0.1:1550", encoding="utf-8")
        return RecordingRunningProcess(1000 + len(calls))

    monkeypatch.setattr(processes_module.subprocess, "Popen", popen)
    processes = FileModeProcesses(config)
    try:
        debug_port = processes.start_debug_server()
        processes.start_debuggee(debug_port, execute_external=False)

        assert calls[0][0][0] == str(config.debug_server_exe)
        assert calls[0][1].get("startupinfo") is None
        assert calls[1][0][0] == str(config.client_exe)
        startupinfo = calls[1][1].get("startupinfo")
        assert startupinfo is not None
        assert startupinfo.dwFlags & subprocess.STARTF_USESHOWWINDOW
        assert startupinfo.wShowWindow == subprocess.SW_HIDE
    finally:
        processes.close()


def test_owned_process_escalation_shares_one_absolute_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    process = TimeoutThenKilledProcess(clock)
    stdout = (tmp_path / "stdout.log").open("wb")
    stderr = (tmp_path / "stderr.log").open("wb")
    owned = OwnedProcess(
        process,  # type: ignore[arg-type]
        tmp_path / "stdout.log",
        tmp_path / "stderr.log",
        (stdout, stderr),
    )
    monkeypatch.setattr(processes_module, "monotonic", clock)

    owned.close(timeout_s=10.0)

    assert process.terminated is True
    assert process.killed is True
    assert process.wait_timeouts == [10.0, 0.0]


@pytest.mark.parametrize("exits_naturally", [True, False])
def test_native_termination_grace_precedes_owned_process_fallback_within_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exits_naturally: bool,
) -> None:
    clock = FakeClock()
    events: list[tuple[str, float]] = []

    class Process(RecordingRunningProcess):
        def wait(self, timeout: float) -> int:
            events.append(("wait", timeout))
            clock.value += timeout
            if exits_naturally or not self.running:
                self.running = False
                return 0
            raise subprocess.TimeoutExpired("synthetic", timeout)

        def terminate(self) -> None:
            events.append(("terminate", clock.value))
            self.running = False

        def kill(self) -> None:
            pytest.fail("native exit or terminate should suffice")

    process = Process(1234)
    stdout = (tmp_path / "stdout.log").open("wb")
    stderr = (tmp_path / "stderr.log").open("wb")
    owned = OwnedProcess(process, tmp_path / "stdout.log", tmp_path / "stderr.log", (stdout, stderr))
    monkeypatch.setattr(processes_module, "monotonic", clock)
    owned.close(timeout_s=2.0, graceful_timeout_s=3.0)
    assert events == ([("wait", 2.0)] if exits_naturally else [("wait", 2.0), ("terminate", 102.0), ("wait", 0.0)])
    assert stdout.closed and stderr.closed
