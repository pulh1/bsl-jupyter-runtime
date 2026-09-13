from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import importlib
import importlib.util
import json
from pathlib import Path
import socket
import subprocess
from threading import Event
import traceback
from types import ModuleType

import pytest

from onec_runtime.config import RuntimeConfig
from onec_runtime.errors import ExtensionLifecycleError


GET = "config extensions properties get --extension=OnecInteractiveRuntime"
SET = (
    "config extensions properties set --extension=OnecInteractiveRuntime "
    "--safe-mode=no"
)
PROPERTIES = {
    "name": "OnecInteractiveRuntime",
    "safe-mode": True,
    "unsafe-action-protection": True,
    "active": True,
    "hash-sum": "unchanged-artifact",
    "purpose": "AddOn",
}


def agent_module() -> ModuleType:
    # A missing core implementation must fail an assertion, not test collection.
    assert importlib.util.find_spec("onec_runtime.configurator_agent") is not None
    return importlib.import_module("onec_runtime.configurator_agent")


def runtime_config(tmp_path: Path, *, server: bool = False) -> RuntimeConfig:
    platform = tmp_path / "platform"
    platform.mkdir(exist_ok=True)
    for name in ("1cv8.exe", "1cv8c.exe", "dbgs.exe"):
        (platform / name).touch()
    return RuntimeConfig(
        workspace=tmp_path / "workspace",
        platform_bin=platform,
        connection_string='Srvr="server";Ref="reference";' if server else None,
        username="private-agent-user",
        password="private-agent-password",
    )


class Channel:
    """External SSH boundary with a mutable extension and real JSON replies."""

    def __init__(
        self,
        *,
        initial: dict[str, object] | None = None,
        after_changes: dict[str, object] | None = None,
        replies: dict[str, bytes] | None = None,
    ) -> None:
        self.properties = dict(PROPERTIES if initial is None else initial)
        self.after_changes = after_changes or {}
        self.replies = replies or {}
        self.commands: list[str] = []
        self.pending = bytearray(b"1C Designer Shell\r\ndesigner> ")
        self.timeouts: list[float] = []
        self.closed = False

    def sendall(self, payload: bytes) -> None:
        command = payload.decode("utf-8").rstrip("\n")
        self.commands.append(command)
        if command in self.replies:
            reply = self.replies[command]
        elif command == GET:
            reply = json.dumps([
                {"type": "extension-properties", "body": self.properties}
            ]).encode("utf-8")
        else:
            if command == SET:
                self.properties["safe-mode"] = False
                self.properties.update(self.after_changes)
            reply = b'[{"type":"success","body":[]}]'
        self.pending = bytearray(reply + b"\r\ndesigner> ")

    def recv(self, size: int) -> bytes:
        # Partial responses also exercise prompt framing across recv calls.
        count = min(size, 7)
        reply = bytes(self.pending[:count])
        del self.pending[:count]
        return reply

    def settimeout(self, timeout: float) -> None:
        self.timeouts.append(timeout)

    def invoke_shell(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def use_channel(monkeypatch: pytest.MonkeyPatch, agent: ModuleType, channel: Channel) -> None:
    @contextmanager
    def owned_agent(_config: RuntimeConfig, _logs: Path):
        try:
            yield channel
        finally:
            channel.close()

    monkeypatch.setattr(agent, "_owned_agent", owned_agent)


@pytest.mark.parametrize("safe_mode", [True, False])
def test_prepares_only_runtime_safe_mode_and_reads_back_other_properties(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, safe_mode: bool
) -> None:
    agent = agent_module()
    channel = Channel(initial={**PROPERTIES, "safe-mode": safe_mode})
    use_channel(monkeypatch, agent, channel)

    result = agent.prepare_extension(runtime_config(tmp_path), tmp_path / "logs")

    assert result == {
        "before": {**PROPERTIES, "safe-mode": safe_mode},
        "after": {**PROPERTIES, "safe-mode": False},
    }
    assert channel.commands == [
        "options set --output-format=json",
        "common connect-ib",
        GET,
        *([SET] if safe_mode else []),
        GET,
        "common disconnect-ib",
    ]
    assert channel.closed


@pytest.mark.parametrize("server", [False, True])
def test_extension_editor_reuses_one_agent_across_mutation_and_designer_dump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: bool,
) -> None:
    agent = agent_module()
    channel = Channel()
    use_channel(monkeypatch, agent, channel)
    config = runtime_config(tmp_path, server=server)
    cfe = tmp_path / "runtime.cfe"
    cfe.write_bytes(b"packaged")
    designer_events: list[int] = []

    with agent.edit_extension(config, tmp_path / "logs") as editor:
        editor.load_cfe(cfe)
        editor.apply()
        with editor.designer_access():
            designer_events.append(len(channel.commands))
        properties = editor.disable_safe_mode()

    dynamic = "--dynamic-disable " if server else ""
    assert channel.commands == [
        "options set --output-format=json",
        "common connect-ib",
        f'config load-cfg --file="{cfe}" --extension=OnecInteractiveRuntime',
        f"config update-db-cfg {dynamic}--extension=OnecInteractiveRuntime",
        "common disconnect-ib",
        "common connect-ib",
        GET,
        SET,
        GET,
        "common disconnect-ib",
    ]
    assert designer_events == [5]
    assert properties["after"]["safe-mode"] is False
    assert channel.closed


def test_extension_editor_allows_long_load_and_database_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = agent_module()
    channel = Channel()
    use_channel(monkeypatch, agent, channel)
    observed_timeouts: list[float] = []
    original_read = agent._read_prompt

    def observed_read(channel: Channel, *, timeout: float) -> bytes:
        observed_timeouts.append(timeout)
        return original_read(channel, timeout=timeout)

    monkeypatch.setattr(agent, "_read_prompt", observed_read)
    with agent.edit_extension(runtime_config(tmp_path, server=True), tmp_path / "logs") as editor:
        editor.load_cfe(tmp_path / "runtime.cfe")
        editor.apply()

    timeouts_by_command = dict(zip(channel.commands, observed_timeouts, strict=True))
    assert timeouts_by_command[
        f'config load-cfg --file="{tmp_path / "runtime.cfe"}" --extension=OnecInteractiveRuntime'
    ] == 300.0
    assert timeouts_by_command[
        "config update-db-cfg --dynamic-disable --extension=OnecInteractiveRuntime"
    ] == 300.0


def test_extension_editor_releases_infobase_after_rejected_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = agent_module()
    config = runtime_config(tmp_path, server=True)
    apply = "config update-db-cfg --dynamic-disable --extension=OnecInteractiveRuntime"
    channel = Channel(replies={
        apply: json.dumps([{
            "type": "error",
            "message": f"Rejected for {config.username} / {config.password}",
            "error-type": "failure",
            "body": [],
        }]).encode("utf-8"),
    })
    use_channel(monkeypatch, agent, channel)

    with pytest.raises(ExtensionLifecycleError, match="rejected"):
        with agent.edit_extension(config, tmp_path / "logs") as editor:
            editor.apply()

    assert channel.commands[-1] == "common disconnect-ib"
    assert channel.closed
    log = (tmp_path / "logs" / "commands.log").read_text(encoding="utf-8")
    assert config.username not in log
    assert config.password not in log


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"safe-mode": True}, "disable safe-mode"),
        ({"unsafe-action-protection": False}, "other extension properties"),
        ({"hash-sum": "other-artifact"}, "other extension properties"),
        ({"new-property": True}, "other extension properties"),
        ({"active": False}, "active"),
    ],
)
def test_rejects_refusal_or_changes_beyond_safe_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    change: dict[str, object], message: str,
) -> None:
    agent = agent_module()
    channel = Channel(after_changes=change)
    use_channel(monkeypatch, agent, channel)

    with pytest.raises(ExtensionLifecycleError, match=message):
        agent.prepare_extension(runtime_config(tmp_path), tmp_path / "logs")

    assert channel.commands[-1] == "common disconnect-ib"
    assert channel.closed


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {},
        {**PROPERTIES, "name": "ForeignExtension"},
        {**PROPERTIES, "safe-mode": "yes"},
        {**PROPERTIES, "safe-mode": 1},
        {**PROPERTIES, "unsafe-action-protection": None},
        {**PROPERTIES, "active": 1},
        {**PROPERTIES, "active": False},
        {key: value for key, value in PROPERTIES.items() if key != "safe-mode"},
        {key: value for key, value in PROPERTIES.items() if key != "unsafe-action-protection"},
        {key: value for key, value in PROPERTIES.items() if key != "active"},
    ],
)
def test_invalid_or_foreign_properties_are_rejected_before_any_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: object,
) -> None:
    agent = agent_module()
    payload = json.dumps([{"type": "extension-properties", "body": body}]).encode()
    channel = Channel(replies={GET: payload})
    use_channel(monkeypatch, agent, channel)

    with pytest.raises(ExtensionLifecycleError, match="active OnecInteractiveRuntime"):
        agent.prepare_extension(runtime_config(tmp_path), tmp_path / "logs")

    assert SET not in channel.commands
    assert channel.commands[-1] == "common disconnect-ib"


@pytest.mark.parametrize(
    "payload",
    [b"not json", b"\xff", b"{}", b"[]", b"[1]", b'[{"type":"success","body":[]}]',
     json.dumps([{"type": "extension-properties", "body": PROPERTIES}] * 2).encode()],
)
def test_missing_or_malformed_response_cannot_authorize_a_property_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: bytes,
) -> None:
    agent = agent_module()
    channel = Channel(replies={GET: payload})
    use_channel(monkeypatch, agent, channel)

    with pytest.raises(ExtensionLifecycleError):
        agent.prepare_extension(runtime_config(tmp_path), tmp_path / "logs")

    assert SET not in channel.commands
    assert channel.closed


@pytest.mark.parametrize("command", ["options set --output-format=json", "common connect-ib", GET, SET])
def test_command_rejection_is_credential_safe_and_disconnect_cannot_hide_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str,
) -> None:
    agent = agent_module()
    config = runtime_config(tmp_path)
    payload = json.dumps([{
        "type": "error", "message": f"Rejected {config.username} / {config.password}",
        "error-type": "failure", "body": [],
    }]).encode()
    channel = Channel(replies={command: payload, "common disconnect-ib": payload})
    use_channel(monkeypatch, agent, channel)

    with pytest.raises(ExtensionLifecycleError, match="rejected") as error:
        agent.prepare_extension(config, tmp_path / "logs")

    assert command in str(error.value)
    assert channel.closed
    log = (tmp_path / "logs" / "commands.log").read_text(encoding="utf-8")
    assert config.password not in log + str(error.value)
    assert config.username not in log + str(error.value)


class Process:
    def __init__(self, *, shutdown_hangs: bool = False) -> None:
        self.returncode: int | None = None
        self.shutdown_hangs = shutdown_hangs
        self.terminated = False
        self.killed = False
        self.wait_timeouts: list[float] = []

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float) -> int:
        self.wait_timeouts.append(timeout)
        if self.shutdown_hangs and not self.killed:
            raise subprocess.TimeoutExpired("agent", timeout)
        self.returncode = 0
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


def native_boundary(
    monkeypatch: pytest.MonkeyPatch, agent: ModuleType, channel: Channel,
    process: Process, *, connect_error: Exception | None = None,
    close_error: Exception | None = None, missing_transport: bool = False,
    disconnect_reason: str | None = None,
):
    launch: list[tuple[list[str], dict[str, object]]] = []
    credentials: list[dict[str, object]] = []
    clients = []

    class Transport:
        def __init__(self):
            self.disconnect_reason = disconnect_reason

        def open_session(self, *, timeout: float):
            assert 0 < timeout <= 30
            return channel

    class Client:
        def __init__(self):
            self.closed = False
            clients.append(self)

        def set_missing_host_key_policy(self, _policy):
            pass

        def connect(self, host, **kwargs):
            credentials.append({"host": host, **kwargs})
            if connect_error:
                raise connect_error

        def get_transport(self):
            return None if missing_transport else Transport()

        def close(self):
            self.closed = True
            if close_error:
                raise close_error

    def popen(command, **kwargs):
        launch.append((list(command), kwargs))
        return process

    monkeypatch.setattr(agent.subprocess, "Popen", popen)
    monkeypatch.setattr(agent.paramiko, "SSHClient", Client)
    monkeypatch.setattr(agent, "_available_loopback_port", lambda: 15439)
    return launch, credentials, clients


@pytest.mark.parametrize("server", [False, True])
@pytest.mark.parametrize("authentication_fails", [False, True])
def test_owned_agent_targets_file_or_server_and_keeps_credentials_in_ssh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: bool,
    authentication_fails: bool,
) -> None:
    agent = agent_module()
    config = runtime_config(tmp_path, server=server)
    channel, process = Channel(), Process()
    auth_error = agent.paramiko.AuthenticationException(config.password) if authentication_fails else None
    launch, credentials, clients = native_boundary(
        monkeypatch, agent, channel, process, connect_error=auth_error,
    )

    if authentication_fails:
        with pytest.raises(ExtensionLifecycleError, match="rejected SSH authentication") as caught:
            agent.prepare_extension(config, tmp_path / "logs")
        assert "1C client login may still succeed" in str(caught.value)
        assert config.password not in "".join(traceback.format_exception(caught.value))
        assert process.terminated
    else:
        assert agent.prepare_extension(config, tmp_path / "logs")["after"]["safe-mode"] is False
        assert channel.closed
        assert channel.commands[-1] == "common shutdown"
    assert process.poll() == 0
    assert all(client.closed for client in clients)
    command, options = launch[0]
    assert command[:4] == [
        str(config.designer_exe), "DESIGNER",
        "/S" if server else "/F",
        "server\\reference" if server else str(config.infobase_dir),
    ]
    assert command[command.index("/AgentListenAddress") + 1] == "127.0.0.1"
    assert command[command.index("/AgentPort") + 1] == "15439"
    assert config.username not in " ".join(command)
    assert config.password not in " ".join(command)
    assert credentials[0]["host"] == "127.0.0.1"
    assert credentials[0]["username"] == config.username
    assert credentials[0]["password"] == config.password
    assert credentials[0]["look_for_keys"] is False
    assert credentials[0]["allow_agent"] is False
    assert credentials[0]["transport_factory"] is agent._DiagnosticTransport
    assert credentials[0]["auth_timeout"] > 2
    for option in ("timeout", "banner_timeout", "auth_timeout"):
        assert 0 < credentials[0][option] <= 30
    assert options["shell"] is False
    assert options["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert all(options[stream] == subprocess.DEVNULL for stream in ("stdin", "stdout", "stderr"))
    assert not Path(command[command.index("/AgentBaseDir") + 1]).exists()


def test_empty_password_agent_failure_does_not_imply_client_login_is_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = agent_module()
    config = replace(runtime_config(tmp_path), password="")
    channel, process = Channel(), Process()
    native_boundary(
        monkeypatch, agent, channel, process,
        connect_error=agent.paramiko.AuthenticationException("rejected"),
    )

    with pytest.raises(ExtensionLifecycleError, match="rejected SSH authentication") as caught:
        agent.prepare_extension(config, tmp_path / "logs")

    assert "empty password" in str(caught.value)
    assert "1C client login may still succeed" in str(caught.value)
    assert "MANUAL" in str(caught.value)


@pytest.mark.parametrize("error_type", ("auth", "transport"))
def test_agent_reports_designer_lock_instead_of_authentication_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_type: str,
) -> None:
    agent = agent_module()
    config = runtime_config(tmp_path, server=True)
    channel, process = Channel(), Process()
    native_boundary(
        monkeypatch, agent, channel, process,
        connect_error=(
            agent.paramiko.AuthenticationException(
                "Authentication failed: transport shut down or saw EOF"
            ) if error_type == "auth" else agent.paramiko.SSHException("EOF")
        ),
        disconnect_reason="Cannot lock the infobase because it is open in Designer.",
    )

    with pytest.raises(ExtensionLifecycleError, match="open in Designer") as caught:
        agent.prepare_extension(config, tmp_path / "logs")

    assert "close" in str(caught.value).lower()
    assert process.terminated


def test_agent_transport_preserves_ssh_disconnect_reason() -> None:
    agent = agent_module()
    with socket.socket() as connection:
        transport = agent._DiagnosticTransport(connection)
        try:
            packet = agent.paramiko.Message()
            packet.add_int(0xFE000000)
            packet.add_string("Cannot lock the infobase because it is open in Designer.")
            packet.rewind()
            transport._parse_disconnect(packet)
            assert transport.disconnect_reason == (
                "Cannot lock the infobase because it is open in Designer."
            )
        finally:
            transport.close()


def test_cleanup_failures_do_not_hide_readback_failure_or_skip_killing_owned_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = agent_module()
    config = runtime_config(tmp_path)
    channel = Channel(after_changes={"safe-mode": True}, replies={
        "common disconnect-ib": b'[{"type":"error","message":"disconnect failed"}]',
    })
    process = Process(shutdown_hangs=True)
    _launch, _credentials, clients = native_boundary(
        monkeypatch, agent, channel, process, close_error=OSError(config.password),
    )

    def close_channel():
        raise OSError(config.password)

    monkeypatch.setattr(channel, "close", close_channel)
    with pytest.raises(ExtensionLifecycleError, match="disable safe-mode") as caught:
        agent.prepare_extension(config, tmp_path / "logs")

    assert config.password not in "".join(traceback.format_exception(caught.value))
    assert all(client.closed for client in clients)
    assert process.terminated and process.killed
    assert process.poll() == 0
    assert all(0 < timeout <= 10 for timeout in process.wait_timeouts)


def test_missing_transport_cleans_up_own_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = agent_module()
    process = Process()
    _launch, _credentials, clients = native_boundary(
        monkeypatch, agent, Channel(), process, missing_transport=True,
    )

    with pytest.raises(ExtensionLifecycleError, match="transport"):
        agent.prepare_extension(runtime_config(tmp_path), tmp_path / "logs")

    assert process.terminated and process.poll() == 0
    assert all(client.closed for client in clients)


def test_connection_retries_stop_at_deadline_and_close_every_failed_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = agent_module()
    config = runtime_config(tmp_path)
    process = Process()
    _launch, credentials, clients = native_boundary(
        monkeypatch, agent, Channel(), process, connect_error=OSError(config.password),
    )
    ticks = iter(index / 10 for index in range(100))
    monkeypatch.setattr(agent, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(agent, "sleep", lambda _seconds: None)
    monkeypatch.setattr(agent, "_START_TIMEOUT_SECONDS", 0.5)

    with pytest.raises(ExtensionLifecycleError, match="loopback SSH endpoint") as caught:
        agent.prepare_extension(config, tmp_path / "logs")

    assert 1 <= len(credentials) <= 5
    assert all(client.closed for client in clients)
    assert process.terminated and process.poll() == 0
    assert config.password not in "".join(traceback.format_exception(caught.value))


def test_shell_acknowledgement_has_deadline_even_when_channel_io_timeout_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = agent_module()
    channel, process = Channel(), Process()
    closed = Event()
    monkeypatch.setattr(channel, "invoke_shell", lambda: closed.wait(timeout=1.0))
    monkeypatch.setattr(channel, "close", closed.set)
    monkeypatch.setattr(agent, "_COMMAND_TIMEOUT_SECONDS", 0.01)
    native_boundary(monkeypatch, agent, channel, process)

    with pytest.raises(ExtensionLifecycleError, match="shell.*timed out"):
        agent.prepare_extension(runtime_config(tmp_path), tmp_path / "logs")

    assert closed.is_set()
    assert process.poll() == 0


@pytest.mark.parametrize("closed", [False, True])
def test_response_timeout_and_closed_channel_fail_with_core_error(
    monkeypatch: pytest.MonkeyPatch, closed: bool,
) -> None:
    agent = agent_module()
    channel = Channel()
    ticks = iter(index / 10 for index in range(20))
    monkeypatch.setattr(agent, "monotonic", lambda: next(ticks))

    def recv(_size):
        if closed:
            return b""
        raise socket.timeout()

    monkeypatch.setattr(channel, "recv", recv)
    with pytest.raises(ExtensionLifecycleError, match="closed|timed out"):
        agent._read_prompt(channel, timeout=0.5)


def test_launch_failure_identifies_start_boundary_and_removes_private_agent_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = agent_module()
    config = runtime_config(tmp_path)
    launched_bases: list[Path] = []

    def failed_popen(command, **_kwargs):
        launched_bases.append(Path(command[command.index("/AgentBaseDir") + 1]))
        raise PermissionError(config.password)

    monkeypatch.setattr(agent.subprocess, "Popen", failed_popen)
    monkeypatch.setattr(agent, "_available_loopback_port", lambda: 15439)
    with pytest.raises(ExtensionLifecycleError, match="could not start") as caught:
        agent.prepare_extension(config, tmp_path / "logs")

    assert config.password not in "".join(traceback.format_exception(caught.value))
    assert len(launched_bases) == 1 and not launched_bases[0].exists()


def test_property_preservation_distinguishes_json_booleans_from_numbers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = agent_module()
    channel = Channel(
        initial={**PROPERTIES, "additional-properties": {"order": 1}},
        after_changes={"additional-properties": {"order": True}},
    )
    use_channel(monkeypatch, agent, channel)

    with pytest.raises(ExtensionLifecycleError, match="other extension properties"):
        agent.prepare_extension(runtime_config(tmp_path), tmp_path / "logs")


@pytest.mark.parametrize("primary_is_core_error", [False, True])
@pytest.mark.parametrize("stop_failure", ["signals", "final-wait"])
def test_incomplete_process_cleanup_is_preserved_on_public_error_and_can_be_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    primary_is_core_error: bool, stop_failure: str,
) -> None:
    agent = agent_module()
    config = runtime_config(tmp_path)
    events: list[str] = []

    class BlockedProcess(Process):
        blocked = True

        def terminate(self):
            events.append("terminate")
            if self.blocked and stop_failure == "signals":
                raise OSError(config.password)

        def kill(self):
            events.append("kill")
            if self.blocked and stop_failure == "signals":
                raise OSError(config.password)

        def wait(self, timeout):
            events.append("wait")
            if self.blocked:
                raise subprocess.TimeoutExpired("agent", timeout)
            return super().wait(timeout)

    channel, process = Channel(), BlockedProcess()
    launch, _credentials, clients = native_boundary(monkeypatch, agent, channel, process)
    original_send = channel.sendall

    def sendall(payload):
        if payload == (GET + "\n").encode():
            if primary_is_core_error:
                raise ExtensionLifecycleError("primary property failure")
            error = OSError(config.password)
            error.add_note(config.username)  # Foreign notes must also stay private.
            raise error
        if payload == b"common disconnect-ib\n":
            raise OSError(config.password)
        original_send(payload)

    monkeypatch.setattr(channel, "sendall", sendall)
    with pytest.raises(ExtensionLifecycleError) as caught:
        agent.prepare_extension(config, tmp_path / "logs")

    public_error = caught.value
    if primary_is_core_error:
        assert str(public_error) == "primary property failure"
    retry = getattr(public_error, "retry_cleanup", None)
    assert callable(retry)
    notes = "\n".join(public_error.__notes__)
    assert "cleanup" in notes and "disconnect failed" in notes
    rendered = "".join(traceback.format_exception(public_error))
    assert config.username not in rendered and config.password not in rendered
    command = launch[0][0]
    base = Path(command[command.index("/AgentBaseDir") + 1])
    assert base.exists(), "The running agent's private base must remain until it stops"

    with pytest.raises(ExtensionLifecycleError, match="cleanup") as retry_error:
        retry()
    assert callable(getattr(retry_error.value, "retry_cleanup", None))
    assert config.password not in "".join(traceback.format_exception(retry_error.value))

    def closed_again():
        pytest.fail("Already closed SSH resources must not be closed again")

    monkeypatch.setattr(channel, "close", closed_again)
    for client in clients:
        monkeypatch.setattr(client, "close", closed_again)
    process.blocked = False
    retry()
    assert process.poll() == 0 and not base.exists()
    completed_events = list(events)
    retry()
    assert events == completed_events


@pytest.mark.parametrize("resource", ["channel", "client", "base", "authentication-client"])
def test_cleanup_retry_retains_only_failed_ssh_or_temporary_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resource: str,
) -> None:
    agent = agent_module()
    config = runtime_config(tmp_path)
    channel, process = Channel(), Process()
    allowed = False
    cleanup_calls: list[str] = []
    original_temporary_directory = agent.tempfile.TemporaryDirectory

    class BlockedBase:
        def __init__(self, *args, **kwargs):
            self.directory = original_temporary_directory(*args, **kwargs)
            self.name = self.directory.name

        def cleanup(self):
            cleanup_calls.append("base")
            if not allowed:
                raise OSError(config.password)
            self.directory.cleanup()

    if resource == "base":
        monkeypatch.setattr(agent.tempfile, "TemporaryDirectory", BlockedBase)
    auth_error = agent.paramiko.AuthenticationException(config.password) if resource == "authentication-client" else None
    client_error = OSError(config.password) if resource in {"client", "authentication-client"} else None
    launch, _credentials, clients = native_boundary(
        monkeypatch, agent, channel, process,
        connect_error=auth_error, close_error=client_error,
    )

    def close_channel():
        cleanup_calls.append("channel")
        if not allowed:
            raise OSError(config.password)
        channel.closed = True

    if resource == "channel":
        monkeypatch.setattr(channel, "close", close_channel)
    with pytest.raises(ExtensionLifecycleError) as caught:
        agent.prepare_extension(config, tmp_path / "logs")

    assert process.poll() == 0
    retry = getattr(caught.value, "retry_cleanup", None)
    assert callable(retry)
    assert config.password not in "".join(traceback.format_exception(caught.value))
    if resource == "authentication-client":
        assert "rejected SSH authentication" in str(caught.value)
    if resource in {"client", "authentication-client"}:
        for client in clients:
            monkeypatch.setattr(client, "close", lambda: cleanup_calls.append("client"))

    def stopped_again():
        pytest.fail("Retry must not signal a process already stopped")

    monkeypatch.setattr(process, "terminate", stopped_again)
    monkeypatch.setattr(process, "kill", stopped_again)
    allowed = True
    retry()
    completed_calls = list(cleanup_calls)
    assert resource.removeprefix("authentication-") in completed_calls
    command = launch[0][0]
    assert not Path(command[command.index("/AgentBaseDir") + 1]).exists()
    retry()
    assert cleanup_calls == completed_calls
