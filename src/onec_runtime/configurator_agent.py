"""Prepare the product extension through a run-owned loopback Configurator Agent."""

from __future__ import annotations

from contextlib import contextmanager, suppress
import json
from pathlib import Path
import socket
import subprocess
import tempfile
from threading import Event, Timer
from time import monotonic, sleep
from typing import Callable, Iterator

import paramiko

from onec_runtime.config import RuntimeConfig
from onec_runtime.errors import ExtensionLifecycleError
from onec_runtime.extension_bundle import EXTENSION_NAME


_HOST = "127.0.0.1"
_PROMPT = b"designer> "
_START_TIMEOUT_SECONDS = 30.0
_AUTH_TIMEOUT_SECONDS = 8.0
_COMMAND_TIMEOUT_SECONDS = 30.0
_MUTATION_TIMEOUT_SECONDS = 300.0
_STOP_TIMEOUT_SECONDS = 10.0
_CLEANUP_RETRY_NOTE = (
    "Configurator Agent resource cleanup is incomplete; retry with exception.retry_cleanup()"
)
_DISCONNECT_FAILURE_NOTE = "Configurator Agent infobase disconnect failed"


class _DiagnosticTransport(paramiko.Transport):
    """Retain the Agent's SSH disconnect reason before Paramiko drops it."""

    disconnect_reason: str | None = None

    def _parse_disconnect(self, message: paramiko.Message) -> None:
        message.get_int()
        self.disconnect_reason = message.get_text()
        message.rewind()
        super()._parse_disconnect(message)


def _designer_lock_error(client: paramiko.SSHClient) -> ExtensionLifecycleError | None:
    try:
        transport = client.get_transport()
        reason = getattr(transport, "disconnect_reason", None)
    except Exception:
        return None
    if isinstance(reason, str) and (
        "cannot lock the infobase because it is open in designer" in reason.casefold()
    ):
        return ExtensionLifecycleError(
            "Configurator Agent cannot lock the infobase because it is open in Designer; "
            "close the regular Designer session and retry, or use "
            "ExtensionMode.MANUAL for an already prepared extension"
        )
    return None


def _expose_cleanup_retry(error: BaseException, retry_cleanup: Callable[[], None]) -> None:
    setattr(error, "retry_cleanup", retry_cleanup)
    if _CLEANUP_RETRY_NOTE not in getattr(error, "__notes__", ()):
        error.add_note(_CLEANUP_RETRY_NOTE)


def _available_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind((_HOST, 0))
        return int(listener.getsockname()[1])


def _read_prompt(channel: paramiko.Channel, *, timeout: float) -> bytes:
    deadline = monotonic() + timeout
    payload = bytearray()
    while not payload.endswith(_PROMPT):
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise ExtensionLifecycleError("Configurator Agent response timed out")
        channel.settimeout(min(remaining, 1.0))
        try:
            chunk = channel.recv(65536)
        except socket.timeout:
            continue
        if not chunk:
            raise ExtensionLifecycleError("Configurator Agent closed its SSH channel")
        payload.extend(chunk)
    return bytes(payload[: -len(_PROMPT)])


def _parse_response(payload: bytes) -> list[dict[str, object]]:
    try:
        result = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise ExtensionLifecycleError("Invalid Configurator Agent JSON response") from None
    if not isinstance(result, list) or not result or not all(
        isinstance(item, dict) for item in result
    ):
        raise ExtensionLifecycleError("Invalid Configurator Agent response structure")
    if any(item.get("type") == "error" for item in result):
        # Agent error text can echo credentials; identify our command instead.
        raise ExtensionLifecycleError("Configurator Agent rejected the command")
    return result


def _command(
    channel: paramiko.Channel, command: str, transcript: list[str],
    *, timeout_s: float | None = None,
) -> list[dict[str, object]]:
    timeout = _COMMAND_TIMEOUT_SECONDS if timeout_s is None else timeout_s
    transcript.append("> " + command)
    channel.settimeout(timeout)
    channel.sendall((command + "\n").encode("utf-8"))
    try:
        response = _parse_response(
            _read_prompt(channel, timeout=timeout)
        )
    except ExtensionLifecycleError as error:
        transcript.append("< " + str(error))
        raise ExtensionLifecycleError(f"{error}: {command}") from None
    # Do not persist arbitrary server replies, which may contain credentials.
    transcript.append("< response accepted")
    return response


def _properties(response: list[dict[str, object]]) -> dict[str, object]:
    found = [item.get("body") for item in response if item.get("type") == "extension-properties"]
    if (
        len(found) != 1
        or not isinstance(found[0], dict)
        or found[0].get("name") != EXTENSION_NAME
        or type(found[0].get("safe-mode")) is not bool
        or type(found[0].get("unsafe-action-protection")) is not bool
        or found[0].get("active") is not True
    ):
        raise ExtensionLifecycleError(
            f"Expected the active {EXTENSION_NAME} extension with explicit safety properties"
        )
    return found[0]


def _connect_client(
    config: RuntimeConfig, port: int, process: subprocess.Popen,
    clients: list[paramiko.SSHClient],
) -> paramiko.SSHClient:
    deadline = monotonic() + _START_TIMEOUT_SECONDS
    while (remaining := deadline - monotonic()) > 0:
        if process.poll() is not None:
            raise ExtensionLifecycleError(
                f"Configurator Agent exited with code {process.returncode}; see agent.log"
            )
        client = paramiko.SSHClient()
        clients.append(client)
        connected = False
        try:
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(
                _HOST,
                port=port,
                username=config.username,
                password=config.password,
                look_for_keys=False,
                allow_agent=False,
                timeout=min(remaining, 1.0),
                banner_timeout=min(remaining, 2.0),
                auth_timeout=min(remaining, _AUTH_TIMEOUT_SECONDS),
                transport_factory=_DiagnosticTransport,
            )
            connected = True
            return client
        except paramiko.AuthenticationException:
            if lock_error := _designer_lock_error(client):
                raise lock_error from None
            empty_password = " with an empty password" if not config.password else ""
            manual_hint = (
                " For an already installed and prepared extension, use "
                "extension_mode=ExtensionMode.MANUAL."
                if not config.password else ""
            )
            raise ExtensionLifecycleError(
                "Configurator Agent rejected SSH authentication"
                f"{empty_password}; 1C client login may still succeed "
                f"separately.{manual_hint}"
            ) from None
        except (OSError, paramiko.SSHException):
            if lock_error := _designer_lock_error(client):
                raise lock_error from None
            sleep(min(0.1, max(0.0, deadline - monotonic())))
        finally:
            if not connected:
                try:
                    client.close()
                except Exception:
                    pass  # Keep the failed client in the owned cleanup closure.
                else:
                    clients.remove(client)
    raise ExtensionLifecycleError(
        "Configurator Agent did not open its loopback SSH endpoint"
    )


def _invoke_shell(channel: paramiko.Channel) -> None:
    # Paramiko's shell acknowledgement ignores Channel.settimeout(). Closing
    # the channel releases its waiting event when the peer never acknowledges.
    expired = Event()

    def expire() -> None:
        expired.set()
        with suppress(Exception):
            channel.close()

    timer = Timer(_COMMAND_TIMEOUT_SECONDS, expire)
    timer.daemon = True
    timer.start()
    try:
        channel.invoke_shell()
    except Exception:
        if expired.is_set():
            raise ExtensionLifecycleError("Configurator Agent shell request timed out") from None
        raise ExtensionLifecycleError("Configurator Agent could not open its SSH shell") from None
    finally:
        timer.cancel()
    if expired.is_set():
        raise ExtensionLifecycleError("Configurator Agent shell request timed out")


def _stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=_STOP_TIMEOUT_SECONDS)
    except Exception:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=_STOP_TIMEOUT_SECONDS)


@contextmanager
def _owned_agent(config: RuntimeConfig, logs: Path) -> Iterator[paramiko.Channel]:
    logs.mkdir(parents=True, exist_ok=True)
    port = _available_loopback_port()
    base = tempfile.TemporaryDirectory(prefix="agent-", dir=logs)
    process = None
    clients: list[paramiko.SSHClient] = []
    channel = None
    primary_error: BaseException | None = None

    def retry_cleanup() -> None:
        nonlocal channel, process, base
        cleanup_failed = False
        if channel is not None:
            try:
                channel.close()
            except Exception:
                cleanup_failed = True
            else:
                channel = None
        for pending_client in list(clients):
            try:
                pending_client.close()
            except Exception:
                cleanup_failed = True
            else:
                clients.remove(pending_client)
        if process is not None:
            try:
                _stop_process(process)
            except Exception:
                cleanup_failed = True
            else:
                process = None
        # A still-running agent may need its private base until a later retry.
        if base is not None and process is None:
            try:
                base.cleanup()
            except Exception:
                cleanup_failed = True
            else:
                base = None
        if cleanup_failed:
            error = ExtensionLifecycleError("Configurator Agent resource cleanup failed")
            _expose_cleanup_retry(error, retry_cleanup)
            raise error from None

    try:
        command = [
            str(config.designer_exe), "DESIGNER", *config.infobase_arguments,
            "/AgentMode", "/AgentSSHHostKeyAuto", "/AgentBaseDir", base.name,
            "/AgentPort", str(port), "/AgentListenAddress", _HOST,
            "/DisableStartupDialogs", "/DisableStartupMessages",
            "/Out", str(logs / "agent.log"),
        ]
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError:
            raise ExtensionLifecycleError(
                "Configurator Agent could not start; check the configured 1C executable"
            ) from None
        client = _connect_client(config, port, process, clients)
        transport = client.get_transport()
        if transport is None:
            raise ExtensionLifecycleError("Configurator Agent has no SSH transport")
        channel = transport.open_session(timeout=_COMMAND_TIMEOUT_SECONDS)
        channel.settimeout(_COMMAND_TIMEOUT_SECONDS)
        _invoke_shell(channel)
        _read_prompt(channel, timeout=_COMMAND_TIMEOUT_SECONDS)
        yield channel
    except BaseException as error:
        primary_error = error
        raise
    finally:
        if channel is not None:
            with suppress(Exception):
                channel.settimeout(_STOP_TIMEOUT_SECONDS)
                channel.sendall(b"common shutdown\n")
                process.wait(timeout=_STOP_TIMEOUT_SECONDS)
        try:
            retry_cleanup()
        except ExtensionLifecycleError:
            if primary_error is not None:
                _expose_cleanup_retry(primary_error, retry_cleanup)
            else:
                raise


def _disable_safe_mode(
    channel: paramiko.Channel, transcript: list[str],
) -> dict[str, dict[str, object]]:
    get = f"config extensions properties get --extension={EXTENSION_NAME}"
    before = _properties(_command(channel, get, transcript))
    if before["safe-mode"]:
        _command(
            channel,
            f"config extensions properties set --extension={EXTENSION_NAME} --safe-mode=no",
            transcript,
        )
    after = _properties(_command(channel, get, transcript))
    if after["safe-mode"] is not False:
        raise ExtensionLifecycleError("Configurator Agent did not disable safe-mode")
    # JSON comparison also preserves types: Python treats 1 == True.
    if (
        json.dumps(
            {key: value for key, value in before.items() if key != "safe-mode"},
            sort_keys=True,
        )
        != json.dumps(
            {key: value for key, value in after.items() if key != "safe-mode"},
            sort_keys=True,
        )
    ):
        raise ExtensionLifecycleError("Configurator Agent changed other extension properties")
    return {"before": before, "after": after}


class ExtensionAgentEditor:
    """One connected agent reused across extension mutation and verification."""

    def __init__(
        self, config: RuntimeConfig, channel: paramiko.Channel, transcript: list[str],
    ) -> None:
        self._config = config
        self._channel = channel
        self._transcript = transcript
        self.connected = True

    def connect(self) -> None:
        if not self.connected:
            _command(self._channel, "common connect-ib", self._transcript)
            self.connected = True

    def disconnect(self) -> None:
        if self.connected:
            _command(self._channel, "common disconnect-ib", self._transcript)
            self.connected = False

    @contextmanager
    def designer_access(self) -> Iterator[None]:
        """Release the infobase lock while a headless Designer dumps XML/CFE."""
        self.disconnect()
        primary_error: BaseException | None = None
        try:
            yield
        except BaseException as error:
            primary_error = error
            raise
        finally:
            try:
                self.connect()
            except BaseException:
                if primary_error is None:
                    raise
                primary_error.add_note("Configurator Agent infobase reconnect failed")

    def load_cfe(self, source: Path) -> None:
        if not self.connected:
            raise ExtensionLifecycleError("Configurator Agent is disconnected")
        _command(
            self._channel,
            f'config load-cfg --file="{source.resolve()}" --extension={EXTENSION_NAME}',
            self._transcript,
            timeout_s=_MUTATION_TIMEOUT_SECONDS,
        )

    def apply(self) -> None:
        if not self.connected:
            raise ExtensionLifecycleError("Configurator Agent is disconnected")
        dynamic = "--dynamic-disable " if self._config.uses_external_infobase else ""
        _command(
            self._channel,
            f"config update-db-cfg {dynamic}--extension={EXTENSION_NAME}",
            self._transcript,
            timeout_s=_MUTATION_TIMEOUT_SECONDS,
        )

    def disable_safe_mode(self) -> dict[str, dict[str, object]]:
        if not self.connected:
            raise ExtensionLifecycleError("Configurator Agent is disconnected")
        return _disable_safe_mode(self._channel, self._transcript)


@contextmanager
def edit_extension(
    config: RuntimeConfig, logs: Path,
) -> Iterator[ExtensionAgentEditor]:
    """Keep one owned Agent for mutation, release it for Designer XML dumps."""
    logs = Path(logs).resolve()
    logs.mkdir(parents=True, exist_ok=True)
    transcript: list[str] = []
    primary_error: BaseException | None = None
    try:
        with _owned_agent(config, logs) as channel:
            _command(channel, "options set --output-format=json", transcript)
            _command(channel, "common connect-ib", transcript)
            editor = ExtensionAgentEditor(config, channel, transcript)
            try:
                yield editor
            except BaseException as error:
                primary_error = error
                raise
            finally:
                try:
                    editor.disconnect()
                except BaseException:
                    if primary_error is None:
                        raise
                    primary_error.add_note(_DISCONNECT_FAILURE_NOTE)
    finally:
        try:
            (logs / "commands.log").write_text("\n".join(transcript) + "\n", encoding="utf-8")
        except OSError:
            if primary_error is None:
                raise ExtensionLifecycleError("Configurator Agent command log could not be written") from None
            primary_error.add_note("Configurator Agent command log could not be written")


def prepare_extension(config: RuntimeConfig, logs: Path) -> dict[str, dict[str, object]]:
    """Disable only the verified product extension's safe mode and read it back.

    The lifecycle caller must verify the packaged artifact's permanent identity
    and fingerprints under its infobase lock before calling this function.
    """
    logs = Path(logs).resolve()
    transcript: list[str] = []
    primary_error: BaseException | None = None
    try:
        logs.mkdir(parents=True, exist_ok=True)
        with _owned_agent(config, logs) as channel:
            _command(channel, "options set --output-format=json", transcript)
            _command(channel, "common connect-ib", transcript)
            connected_error: BaseException | None = None
            try:
                return _disable_safe_mode(channel, transcript)
            except BaseException as error:
                connected_error = error
                raise
            finally:
                try:
                    _command(channel, "common disconnect-ib", transcript)
                except Exception:
                    if connected_error is None:
                        raise
                    connected_error.add_note(_DISCONNECT_FAILURE_NOTE)
    except ExtensionLifecycleError as error:
        primary_error = error
        raise
    except Exception as error:
        primary_error = ExtensionLifecycleError(
            "Configurator Agent preparation failed; see commands.log"
        )
        # Preserve our cleanup evidence without copying untrusted exception text.
        for note in getattr(error, "__notes__", ()):
            if note in (_CLEANUP_RETRY_NOTE, _DISCONNECT_FAILURE_NOTE):
                primary_error.add_note(note)
        retry_cleanup = getattr(error, "retry_cleanup", None)
        if callable(retry_cleanup):
            _expose_cleanup_retry(primary_error, retry_cleanup)
        raise primary_error from None
    except BaseException as error:
        primary_error = error
        raise
    finally:
        try:
            (logs / "commands.log").write_text("\n".join(transcript) + "\n", encoding="utf-8")
        except OSError:
            message = "Configurator Agent command log could not be written"
            if primary_error is not None:
                primary_error.add_note(message)
            else:
                raise ExtensionLifecycleError(message) from None


__all__ = ["ExtensionAgentEditor", "edit_extension", "prepare_extension"]
