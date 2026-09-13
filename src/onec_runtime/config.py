from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path


_CONNECTION_PART = re.compile(
    r'[ \t]*([A-Za-z]+)[ \t]*=[ \t]*"([^"\r\n]*)"[ \t]*(?:;|$)'
)


def _connection_target(value: str) -> tuple[str | None, str | None]:
    """Accept the file and server selectors used by 1C connection strings."""
    if not isinstance(value, str) or not value or "\r" in value or "\n" in value:
        raise ValueError("Invalid connection_string")
    fields: dict[str, str] = {}
    offset = 0
    while offset < len(value):
        match = _CONNECTION_PART.match(value, offset)
        if match is None:
            raise ValueError("Invalid connection_string")
        key, target = match.group(1).casefold(), match.group(2)
        if key not in {"file", "srvr", "ref"} or key in fields or not target:
            raise ValueError("Invalid connection_string")
        if any(ord(character) < 32 for character in target):
            raise ValueError("Invalid connection_string")
        fields[key] = target
        offset = match.end()
        if not value[offset:].strip():
            break
    if set(fields) == {"file"}:
        return fields["file"], None
    if set(fields) == {"srvr", "ref"}:
        return None, fields["srvr"] + "\\" + fields["ref"]
    raise ValueError("Invalid connection_string")


def _default_workspace(file_path: str | None, server_infobase: str | None) -> Path:
    if os.name == "nt":
        user_state = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
    else:
        user_state = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    if file_path is not None:
        identity = "file:" + str(Path(file_path).resolve())
        if os.name == "nt":
            identity = identity.casefold()
    elif server_infobase is not None:
        identity = "server:" + server_infobase.casefold()
    else:
        identity = "temporary"
    key = sha256(identity.encode("utf-8")).hexdigest()[:20]
    return Path(user_state) / "onec-interactive-runtime" / "workspaces" / key


@dataclass(frozen=True)
class RuntimeConfig:
    workspace: Path | str | None = None
    platform_bin: Path | str | None = None
    connection_string: str | None = None
    debug_host: str = "127.0.0.1"
    debug_port_from: int = 1550
    debug_port_to: int = 1559
    username: str = ""
    password: str = field(default="", repr=False)
    debug_port: int = 1550
    debug_alias: str | None = None
    _file_path: Path | None = field(default=None, init=False, repr=False)
    _server_infobase: str | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.platform_bin is None:
            raise TypeError("platform_bin is required")
        file_path, server_infobase = (
            _connection_target(self.connection_string)
            if self.connection_string is not None else (None, None)
        )
        if server_infobase is not None:
            if (
                server_infobase.count("\\") != 1
                or any(character in server_infobase for character in ';"/')
                or any(ord(character) < 32 for character in server_infobase)
                or any(not part or part != part.strip() for part in server_infobase.split("\\"))
            ):
                raise ValueError("Invalid connection_string")
        if type(self.debug_port) is not int or not 0 < self.debug_port <= 65535:
            raise ValueError("Invalid debug_port")
        if self.debug_alias is not None and (
            not isinstance(self.debug_alias, str)
            or not self.debug_alias.strip()
            or any(ord(character) < 32 for character in self.debug_alias)
        ):
            raise ValueError("Invalid debug_alias")
        workspace = self.workspace
        if workspace is None:
            workspace = _default_workspace(file_path, server_infobase)
        object.__setattr__(self, "workspace", Path(workspace).resolve())
        object.__setattr__(self, "platform_bin", Path(self.platform_bin).resolve())
        if file_path is not None:
            object.__setattr__(self, "_file_path", Path(file_path).resolve())
        object.__setattr__(self, "_server_infobase", server_infobase)
        executables = (self.designer_exe, self.client_exe)
        if not self.is_server_infobase:
            executables += (self.debug_server_exe,)
        for executable in executables:
            if not executable.is_file():
                raise ValueError(
                    f"Required 1C executable is missing: {executable.name}"
                )
        if not (0 < self.debug_port_from <= self.debug_port_to <= 65535):
            raise ValueError("Invalid debug server port range")
        if (
            self._file_path is not None
            and not (self._file_path / "1Cv8.1CD").is_file()
        ):
            raise ValueError("External infobase must contain 1Cv8.1CD")

    @property
    def designer_exe(self) -> Path:
        return self.platform_bin / "1cv8.exe"

    @property
    def client_exe(self) -> Path:
        return self.platform_bin / "1cv8c.exe"

    @property
    def debug_server_exe(self) -> Path:
        return self.platform_bin / "dbgs.exe"

    @property
    def runtime_dir(self) -> Path:
        return self.workspace / ".runtime"

    @property
    def infobase_dir(self) -> Path:
        if self.is_server_infobase:
            raise ValueError("A server infobase has no local database directory")
        if self._file_path is not None:
            return self._file_path
        return self.runtime_dir / "infobase"

    @property
    def uses_external_infobase(self) -> bool:
        return self._file_path is not None or self.is_server_infobase

    @property
    def is_server_infobase(self) -> bool:
        return self._server_infobase is not None

    @property
    def infobase_arguments(self) -> tuple[str, str]:
        if self._server_infobase is not None:
            return "/S", self._server_infobase
        return "/F", str(self.infobase_dir)

    @property
    def infobase_identity(self) -> str:
        if self._server_infobase is not None:
            return "server:" + self._server_infobase.casefold()
        return str(self.infobase_dir.resolve())

    @property
    def server_target_type(self) -> str:
        return "Server" if self.is_server_infobase else "ServerEmulation"

    @property
    def infobase_debug_alias(self) -> str:
        if self.debug_alias is not None:
            return self.debug_alias
        if self._server_infobase is not None:
            return self._server_infobase.split("\\", 1)[1]
        return "DefAlias"

    @property
    def build_infobase_dir(self) -> Path:
        return self.runtime_dir / "build-infobase"

    @property
    def build_dir(self) -> Path:
        return self.runtime_dir / "build"

    @property
    def kernel_epf(self) -> Path:
        return self.build_dir / "Kernel.epf"

    @property
    def worker_epf(self) -> Path:
        return self.build_dir / "Worker.epf"

    @property
    def logs_dir(self) -> Path:
        return self.runtime_dir / "logs"

    @property
    def artifacts_dir(self) -> Path:
        return self.workspace / "artifacts"
