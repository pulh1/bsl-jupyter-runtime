from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import os
import subprocess
from time import monotonic, sleep
from typing import BinaryIO
from uuid import uuid4

import psutil

from onec_runtime.config import RuntimeConfig
from onec_runtime.errors import ProcessStartError, TargetLost


@dataclass
class OwnedProcess:
    process: subprocess.Popen[bytes] = field(repr=False)
    stdout_path: Path
    stderr_path: Path
    _streams: tuple[BinaryIO, BinaryIO] = field(repr=False)

    @property
    def pid(self) -> int:
        return self.process.pid

    def ensure_running(self) -> None:
        returncode = self.process.poll()
        if returncode is not None:
            raise TargetLost(
                f"Owned process {self.process.pid} exited with code {returncode}; "
                f"see {self.stderr_path}"
            )

    def close(self, timeout_s: float = 10.0, *, graceful_timeout_s: float = 0.0) -> None:
        deadline = monotonic() + max(0.0, timeout_s)
        try:
            if graceful_timeout_s > 0 and self.process.poll() is None:
                try:
                    self.process.wait(timeout=min(graceful_timeout_s, max(0.0, deadline - monotonic())))
                except subprocess.TimeoutExpired:
                    pass
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=max(0.0, deadline - monotonic()))
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    try:
                        self.process.wait(timeout=max(0.0, deadline - monotonic()))
                    except subprocess.TimeoutExpired:
                        raise TargetLost(
                            f"Owned process {self.pid} did not exit before the cleanup deadline"
                        ) from None
        finally:
            for stream in self._streams:
                stream.close()


def read_debug_server_notification(path: Path) -> tuple[str, int]:
    payload = path.read_bytes()
    encoding = "utf-16" if payload.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    value = payload.decode(encoding).strip()
    host, port_text = value.rsplit(":", 1)
    port = int(port_text)
    if not host or not 0 < port <= 65535:
        raise ValueError(f"Invalid debug server notification: {value!r}")
    return host, port


def debug_server_command(config: RuntimeConfig, notify_path: Path) -> list[str]:
    return [
        str(config.debug_server_exe),
        f"--addr={config.debug_host}",
        f"--portRange={config.debug_port_from}:{config.debug_port_to}",
        f"--ownerPID={os.getpid()}",
        f"--notify={notify_path}",
    ]


def debuggee_command(
    config: RuntimeConfig,
    debug_port: int,
    *,
    execute_external: bool = True,
    thick_client: bool = False,
    startup_parameter: str | None = None,
) -> list[str]:
    command = [
        str(config.designer_exe if thick_client else config.client_exe),
        "ENTERPRISE",
        "/Lru",
        *config.infobase_arguments,
        "/TCOMP",
        "-SDC",
        "/DisableStartupDialogs",
        "/DisableStartupMessages",
        "/Out",
        str(config.logs_dir / "1c-messages.log"),
        "/TechnicalSpecialistMode",
        "/DEBUG",
        "-http",
        "-attach",
        "/DEBUGGERURL",
        f"http://{config.debug_host}:{debug_port}",
        "/O",
        "Normal",
        "/WA-",
        "/N",
        config.username,
        "/P",
        config.password,
    ]
    if execute_external:
        infobase_argument_end = command.index(config.infobase_arguments[0]) + 2
        command[infobase_argument_end:infobase_argument_end] = [
            "/Execute",
            str(config.kernel_epf),
        ]
    if startup_parameter is not None:
        command.extend(("/C", startup_parameter))
    return command


class FileModeProcesses:
    """Own the client and, only for a file infobase, its private debugger."""
    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config
        self.debug_server: OwnedProcess | None = None
        self.debuggee: OwnedProcess | None = None

    def _spawn(
        self,
        command: list[str],
        stem: str,
        *,
        hide_window: bool = False,
    ) -> OwnedProcess:
        self.config.logs_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = self.config.logs_dir / f"{stem}.stdout.log"
        stderr_path = self.config.logs_dir / f"{stem}.stderr.log"
        stdout = stdout_path.open("wb")
        stderr = stderr_path.open("wb")
        startupinfo = None
        if hide_window and os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
        try:
            process = subprocess.Popen(
                command,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                startupinfo=startupinfo,
            )
        except OSError as error:
            stdout.close()
            stderr.close()
            raise ProcessStartError(
                f"Unable to start owned process (OS error {error.errno}); see {stderr_path}"
            ) from None
        return OwnedProcess(process, stdout_path, stderr_path, (stdout, stderr))

    def start_debug_server(self, timeout_s: float = 30.0) -> int:
        if self.config.is_server_infobase:
            return self.config.debug_port
        if self.debug_server is not None:
            raise ProcessStartError("Debug server is already owned by this probe")
        notify_path = self.config.logs_dir / f"dbgs-notify-{uuid4()}.txt"
        self.debug_server = self._spawn(debug_server_command(self.config, notify_path), "dbgs")
        deadline = monotonic() + timeout_s
        while monotonic() < deadline:
            self.debug_server.ensure_running()
            try:
                _, port = read_debug_server_notification(notify_path)
                notify_path.unlink(missing_ok=True)
                return port
            except (FileNotFoundError, PermissionError, UnicodeDecodeError, ValueError):
                pass
            sleep(0.05)
        raise ProcessStartError(f"dbgs.exe did not publish a port within {timeout_s} seconds")

    def start_debuggee(
        self,
        debug_port: int,
        *,
        execute_external: bool = True,
        thick_client: bool = False,
        startup_parameter: str | None = None,
    ) -> OwnedProcess:
        if self.debuggee is not None:
            raise ProcessStartError("1C debuggee is already owned by this probe")
        if (
            not self.config.is_server_infobase
            and not (self.config.infobase_dir / "1Cv8.1CD").is_file()
        ):
            raise ProcessStartError("The dedicated file infobase does not exist")
        if execute_external and not self.config.kernel_epf.is_file():
            raise ProcessStartError("Kernel.epf has not been built")
        (self.config.logs_dir / "1c-messages.log").unlink(missing_ok=True)
        self.debuggee = self._spawn(
            debuggee_command(
                self.config,
                debug_port,
                execute_external=execute_external,
                thick_client=thick_client,
                startup_parameter=startup_parameter,
            ),
            "1cv8c",
            hide_window=True,
        )
        return self.debuggee

    def ensure_running(self) -> None:
        if self.debuggee is None:
            raise TargetLost("The owned 1C client process is missing")
        if not self.config.is_server_infobase:
            if self.debug_server is None:
                raise TargetLost("The owned file-mode process pair is incomplete")
            self.debug_server.ensure_running()
        self.debuggee.ensure_running()

    def sample_memory(self) -> dict[str, int | None]:
        result: dict[str, int | None] = {"python": None, "dbgs": None, "onec": None}
        pids = {
            "python": os.getpid(),
            "dbgs": self.debug_server.pid if self.debug_server else None,
            "onec": self.debuggee.pid if self.debuggee else None,
        }
        for name, pid in pids.items():
            if pid is None:
                continue
            try:
                result[name] = psutil.Process(pid).memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                result[name] = None
        return result

    def close(self, *, graceful_client_timeout_s: float = 0.0) -> None:
        self.close_debuggee(graceful_timeout_s=graceful_client_timeout_s)
        self.close_debug_server()

    def close_debuggee(self, timeout_s: float = 10.0, *, graceful_timeout_s: float = 0.0) -> None:
        if self.debuggee is None:
            return
        if graceful_timeout_s > 0:
            self.debuggee.close(timeout_s, graceful_timeout_s=graceful_timeout_s)
        else:
            self.debuggee.close(timeout_s)
        self.debuggee = None

    def close_debug_server(self, timeout_s: float = 10.0) -> None:
        if self.debug_server is None:
            return
        self.debug_server.close(timeout_s)
        self.debug_server = None

    def __enter__(self) -> "FileModeProcesses":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
