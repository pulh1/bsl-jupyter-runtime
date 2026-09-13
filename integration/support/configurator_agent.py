from __future__ import annotations

import json
from pathlib import Path
import socket
import subprocess
from time import monotonic, sleep
from typing import Any

import paramiko

from onec_runtime.config import RuntimeConfig
from onec_runtime.errors import ProcessStartError


_HOST = "127.0.0.1"
_PROMPT = b"designer> "
_START_TIMEOUT_SECONDS = 30.0
_COMMAND_TIMEOUT_SECONDS = 30.0


def _available_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind((_HOST, 0))
        return int(listener.getsockname()[1])


def _read_prompt(channel: Any, *, timeout: float) -> bytes:
    deadline = monotonic() + timeout
    payload = bytearray()
    while not payload.endswith(_PROMPT):
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise ProcessStartError("Configurator Agent command timed out")
        channel.settimeout(min(1.0, remaining))
        try:
            chunk = channel.recv(65536)
        except socket.timeout:
            continue
        if not chunk:
            raise ProcessStartError("Configurator Agent closed its SSH channel")
        payload.extend(chunk)
    return bytes(payload[: -len(_PROMPT)])


def _parse_response(payload: bytes) -> list[dict[str, object]]:
    text = payload.decode("utf-8", errors="replace").strip()
    try:
        response = json.loads(text)
    except json.JSONDecodeError as error:
        raise ProcessStartError(
            "Configurator Agent returned an invalid JSON response"
        ) from error
    if not isinstance(response, list) or not all(
        isinstance(item, dict) for item in response
    ):
        raise ProcessStartError("Configurator Agent returned an invalid response")
    return response


def _run_command(channel: Any, command: str, transcript: list[str]) -> None:
    channel.sendall((command + "\n").encode("utf-8"))
    payload = _read_prompt(channel, timeout=_COMMAND_TIMEOUT_SECONDS)
    rendered = payload.decode("utf-8", errors="replace").strip()
    transcript.extend((f"> {command}", rendered))
    response = _parse_response(payload)
    errors = [
        str(item.get("message") or item.get("error-type") or "unknown error")
        for item in response
        if item.get("type") == "error"
    ]
    if errors:
        raise ProcessStartError(
            "Configurator Agent rejected the command: " + "; ".join(errors)
        )


def _connect_client(port: int, username: str, process: Any) -> Any:
    deadline = monotonic() + _START_TIMEOUT_SECONDS
    last_error: BaseException | None = None
    while monotonic() < deadline:
        if process.poll() is not None:
            raise ProcessStartError(
                f"Configurator Agent exited with code {process.returncode}"
            )
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                _HOST,
                port=port,
                username=username,
                password="",
                look_for_keys=False,
                allow_agent=False,
                timeout=1.0,
                banner_timeout=2.0,
                auth_timeout=2.0,
            )
            return client
        except (OSError, paramiko.SSHException) as error:
            last_error = error
            client.close()
            sleep(0.1)
    raise ProcessStartError(
        "Configurator Agent did not open its SSH endpoint"
    ) from last_error


def _stop_process(process: Any) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10.0)


def configure_extensions_unsafe(
    config: RuntimeConfig,
    extension_names: tuple[str, ...],
    log_root: Path,
) -> None:
    """Disable extension safety through a run-owned Configurator Agent."""

    if not extension_names:
        raise ValueError("extension_names must not be empty")
    if any(
        not isinstance(name, str) or not name.isidentifier()
        for name in extension_names
    ):
        raise ValueError("each extension_name must be an identifier")

    logs = Path(log_root).resolve()
    logs.mkdir(parents=True, exist_ok=True)
    base = logs / "base"
    base.mkdir(exist_ok=False)
    agent_log = logs / "agent.log"
    commands_log = logs / "commands.log"
    port = _available_loopback_port()
    command = [
        str(config.designer_exe),
        "DESIGNER",
        "/F",
        str(config.infobase_dir),
        "/AgentMode",
        "/AgentSSHHostKeyAuto",
        "/AgentBaseDir",
        str(base),
        "/AgentPort",
        str(port),
        "/AgentListenAddress",
        _HOST,
        "/DisableStartupDialogs",
        "/DisableStartupMessages",
        "/Out",
        str(agent_log),
    ]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        creationflags=creationflags,
    )
    client: Any | None = None
    channel: Any | None = None
    transcript: list[str] = []
    try:
        client = _connect_client(port, config.username, process)
        transport = client.get_transport()
        if transport is None:
            raise ProcessStartError("Configurator Agent has no SSH transport")
        channel = transport.open_session()
        channel.invoke_shell()
        _read_prompt(channel, timeout=_COMMAND_TIMEOUT_SECONDS)
        _run_command(channel, "options set --output-format=json", transcript)
        _run_command(channel, "common connect-ib", transcript)
        for name in extension_names:
            _run_command(
                channel,
                "config extensions properties set "
                f"--extension={name} --safe-mode=no "
                "--unsafe-action-protection=no",
                transcript,
            )
        _run_command(channel, "common disconnect-ib", transcript)
        channel.sendall(b"common shutdown\n")
        transcript.append("> common shutdown")
        try:
            process.wait(timeout=10.0)
        except subprocess.TimeoutExpired as error:
            raise ProcessStartError(
                "Configurator Agent did not shut down after the command"
            ) from error
    finally:
        commands_log.write_text("\n".join(transcript) + "\n", encoding="utf-8")
        if channel is not None:
            channel.close()
        if client is not None:
            client.close()
        _stop_process(process)
