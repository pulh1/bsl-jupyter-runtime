from __future__ import annotations

import json
from pathlib import Path

import pytest

from integration.support import configurator_agent
from onec_runtime.config import RuntimeConfig
from onec_runtime.errors import ProcessStartError


def _runtime_config(tmp_path: Path) -> RuntimeConfig:
    platform = tmp_path / "platform"
    platform.mkdir()
    for executable in ("1cv8.exe", "1cv8c.exe", "dbgs.exe"):
        (platform / executable).write_bytes(b"fake")
    infobase = tmp_path / "target" / ".runtime" / "infobase"
    infobase.mkdir(parents=True)
    (infobase / "1Cv8.1CD").write_bytes(b"fake")
    return RuntimeConfig(
        workspace=tmp_path / "target",
        platform_bin=platform,
        connection_string=f'File="{infobase}";',
    )


class _FakeProcess:
    def __init__(self) -> None:
        self.pid = 42
        self.returncode: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return 0

    def kill(self) -> None:
        self.returncode = -9


class _FakeChannel:
    def __init__(self, *, fail_command: str | None = None) -> None:
        self._responses = [b"1C Designer Shell\r\ndesigner> "]
        self.commands: list[str] = []
        self.fail_command = fail_command
        self.closed = False

    def invoke_shell(self) -> None:
        return None

    def settimeout(self, _timeout: float) -> None:
        return None

    def recv(self, _size: int) -> bytes:
        return self._responses.pop(0)

    def sendall(self, payload: bytes) -> None:
        command = payload.decode("utf-8").rstrip("\n")
        self.commands.append(command)
        kind = "error" if command == self.fail_command else "success"
        message = "rejected" if kind == "error" else ""
        response = json.dumps([{"type": kind, "message": message, "body": []}])
        self._responses.append((response + "designer> ").encode("utf-8"))

    def close(self) -> None:
        self.closed = True


class _FakeTransport:
    def __init__(self, channel: _FakeChannel) -> None:
        self.channel = channel

    def open_session(self) -> _FakeChannel:
        return self.channel


class _FakeClient:
    def __init__(self, channel: _FakeChannel) -> None:
        self.channel = channel
        self.connect_kwargs: dict[str, object] = {}
        self.closed = False

    def set_missing_host_key_policy(self, _policy: object) -> None:
        return None

    def connect(self, hostname: str, **kwargs: object) -> None:
        self.connect_kwargs = {"hostname": hostname, **kwargs}

    def get_transport(self) -> _FakeTransport:
        return _FakeTransport(self.channel)

    def close(self) -> None:
        self.closed = True


def test_configures_all_extensions_through_one_owned_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _runtime_config(tmp_path)
    process = _FakeProcess()
    channel = _FakeChannel()
    client = _FakeClient(channel)
    launch_calls: list[list[str]] = []

    def fake_popen(command: list[str], **_kwargs: object) -> _FakeProcess:
        launch_calls.append(list(command))
        return process

    monkeypatch.setattr(configurator_agent.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(configurator_agent.paramiko, "SSHClient", lambda: client)
    monkeypatch.setattr(configurator_agent, "_available_loopback_port", lambda: 15439)

    configurator_agent.configure_extensions_unsafe(
        config,
        ("OnecInteractiveRuntime", "JupyterBslTestFixture"),
        tmp_path / "agent-logs",
    )

    assert launch_calls == [[
        str(config.designer_exe),
        "DESIGNER",
        "/F",
        str(config.infobase_dir),
        "/AgentMode",
        "/AgentSSHHostKeyAuto",
        "/AgentBaseDir",
        str((tmp_path / "agent-logs" / "base").resolve()),
        "/AgentPort",
        "15439",
        "/AgentListenAddress",
        "127.0.0.1",
        "/DisableStartupDialogs",
        "/DisableStartupMessages",
        "/Out",
        str((tmp_path / "agent-logs" / "agent.log").resolve()),
    ]]
    assert client.connect_kwargs["username"] == ""
    assert client.connect_kwargs["password"] == ""
    assert channel.commands == [
        "options set --output-format=json",
        "common connect-ib",
        (
            "config extensions properties set "
            "--extension=OnecInteractiveRuntime --safe-mode=no "
            "--unsafe-action-protection=no"
        ),
        (
            "config extensions properties set "
            "--extension=JupyterBslTestFixture --safe-mode=no "
            "--unsafe-action-protection=no"
        ),
        "common disconnect-ib",
        "common shutdown",
    ]
    assert process.terminated is True
    assert client.closed is True
    transcript = (tmp_path / "agent-logs" / "commands.log").read_text(
        encoding="utf-8"
    )
    assert "type\": \"success" in transcript
    assert "password" not in transcript.casefold()


def test_agent_command_error_is_reported_and_owned_process_is_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _runtime_config(tmp_path)
    command = (
        "config extensions properties set --extension=OnecInteractiveRuntime "
        "--safe-mode=no --unsafe-action-protection=no"
    )
    process = _FakeProcess()
    channel = _FakeChannel(fail_command=command)
    client = _FakeClient(channel)
    monkeypatch.setattr(
        configurator_agent.subprocess, "Popen", lambda *_args, **_kwargs: process
    )
    monkeypatch.setattr(configurator_agent.paramiko, "SSHClient", lambda: client)
    monkeypatch.setattr(configurator_agent, "_available_loopback_port", lambda: 15439)

    with pytest.raises(ProcessStartError, match="Configurator Agent rejected"):
        configurator_agent.configure_extensions_unsafe(
            config,
            ("OnecInteractiveRuntime",),
            tmp_path / "agent-logs",
        )

    assert process.terminated is True
    assert client.closed is True


def test_rejects_invalid_extension_name_before_starting_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _runtime_config(tmp_path)
    monkeypatch.setattr(
        configurator_agent.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("agent must not be started"),
    )

    with pytest.raises(ValueError, match="identifier"):
        configurator_agent.configure_extensions_unsafe(
            config,
            ("bad name",),
            tmp_path / "agent-logs",
        )
