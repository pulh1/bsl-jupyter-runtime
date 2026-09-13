"""Shared exact-identity process tracking for live integration evidence."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path
import subprocess
from threading import Event, Lock, Thread

import psutil


def _normalized_path(value: str | Path) -> str:
    return str(Path(value).resolve()).casefold()


class ProcessState(StrEnum):
    ABSENT = "absent"
    ALIVE = "alive"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    role: str
    pid: int
    create_time: float
    executable: str

    def __post_init__(self) -> None:
        if not self.role.isidentifier() or len(self.role) > 64:
            raise ValueError("process role is invalid")
        if type(self.pid) is not int or self.pid <= 0:
            raise ValueError("process PID is invalid")
        if not isfinite(self.create_time) or self.create_time <= 0:
            raise ValueError("process create_time is invalid")
        if not Path(self.executable).is_absolute():
            raise ValueError("process executable must be absolute")

    def private_wire(self) -> dict[str, object]:
        return {
            "role": self.role,
            "pid": self.pid,
            "create_time": self.create_time,
            "exe": self.executable,
        }


@dataclass(frozen=True, slots=True)
class ApprovedExecutablePolicy:
    python_executable: str
    platform_bin: str

    def __post_init__(self) -> None:
        if not Path(self.python_executable).is_absolute():
            raise ValueError("approved Python executable must be absolute")
        if not Path(self.platform_bin).is_absolute():
            raise ValueError("approved platform bin must be absolute")
        object.__setattr__(
            self, "python_executable", str(Path(self.python_executable).resolve())
        )
        object.__setattr__(self, "platform_bin", str(Path(self.platform_bin).resolve()))

    def expected_platform_executable(self, role: str) -> str:
        name = {
            "designer": "1cv8.exe",
            "onec": "1cv8c.exe",
            "dbgs": "dbgs.exe",
        }.get(role)
        if name is None:
            raise ValueError("role has no approved platform executable")
        return str((Path(self.platform_bin) / name).resolve())


@dataclass(frozen=True, slots=True)
class CanonicalDatabaseIdentity:
    filesystem_key: str
    size: int

    def __post_init__(self) -> None:
        if not self.filesystem_key or type(self.size) is not int or self.size < 0:
            raise ValueError("database filesystem identity is invalid")


def canonical_database_identity(infobase: Path) -> CanonicalDatabaseIdentity:
    """Use the database file identity so junction/short-path aliases converge."""
    database = Path(infobase).resolve(strict=True) / "1Cv8.1CD"
    try:
        stat = database.stat()
    except OSError as error:
        raise AssertionError("target database identity is unverifiable") from error
    if not database.is_file() or stat.st_dev < 0 or stat.st_ino <= 0:
        raise AssertionError("target database identity is unverifiable")
    return CanonicalDatabaseIdentity(
        f"{int(stat.st_dev):x}:{int(stat.st_ino):x}", int(stat.st_size)
    )


def database_identity_sha256(identity: CanonicalDatabaseIdentity) -> str:
    if not isinstance(identity, CanonicalDatabaseIdentity):
        raise TypeError("database identity must be canonical")
    digest = sha256()
    for part in (
        "onec-capture-database-v1",
        identity.filesystem_key,
        str(identity.size),
    ):
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def adopt_owned_root(
    process: psutil.Process,
    *,
    role: str,
    policy: ApprovedExecutablePolicy,
    required_cmdline: Sequence[str],
) -> ProcessIdentity:
    if role not in {"service", "mcp_a", "mcp_b"}:
        raise AssertionError("owned root role is invalid")
    try:
        executable = str(Path(process.exe()).resolve())
        command = tuple(process.cmdline())
        created = process.create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError) as error:
        raise AssertionError("owned root identity is unverifiable") from error
    if _normalized_path(executable) != _normalized_path(policy.python_executable):
        raise AssertionError("owned root is not the approved Python executable")
    expected = tuple(required.casefold() for required in required_cmdline)
    folded = tuple(
        argument.casefold() for argument in command if isinstance(argument, str)
    )
    contains_exact_sequence = any(
        folded[index : index + len(expected)] == expected
        for index in range(max(0, len(folded) - len(expected) + 1))
    )
    if (
        not command
        or not expected
        or any(not isinstance(required, str) or not required for required in required_cmdline)
        or len(folded) != len(command)
        or not contains_exact_sequence
    ):
        raise AssertionError("owned root command line is unverifiable")
    return ProcessIdentity(role, process.pid, created, executable)


def adopt_owned_descendant(
    process: psutil.Process,
    *,
    parent: ProcessIdentity,
    policy: ApprovedExecutablePolicy,
) -> ProcessIdentity:
    try:
        parent_pid = process.ppid()
        executable = str(Path(process.exe()).resolve())
        name = process.name().casefold()
        created = process.create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError) as error:
        raise AssertionError("owned descendant identity is unverifiable") from error
    if parent_pid != parent.pid:
        raise AssertionError("owned descendant parent identity changed")
    role = {
        "1cv8.exe": "designer",
        "1cv8c.exe": "onec",
        "dbgs.exe": "dbgs",
    }.get(name)
    if role is None:
        raise AssertionError("owned descendant executable name is not approved")
    expected = policy.expected_platform_executable(role)
    if _normalized_path(executable) != _normalized_path(expected):
        raise AssertionError("owned descendant is outside approved platform root")
    return ProcessIdentity(role, process.pid, created, executable)


_INFOBASE_SELECTORS = {
    "/f": "file",
    "-f": "file",
    "/s": "server",
    "-s": "server",
    "/ibname": "registered",
    "-ibname": "registered",
    "/ibconnectionstring": "connection_string",
    "-ibconnectionstring": "connection_string",
}


def _selector_value(value: object) -> str:
    if not isinstance(value, str):
        raise AssertionError("1C command line is unverifiable")
    stripped = value.strip()
    if not stripped:
        raise AssertionError("1C command line is unverifiable")
    quote_count = stripped.count('"')
    if quote_count:
        if quote_count != 2 or not (
            stripped.startswith('"') and stripped.endswith('"')
        ):
            raise AssertionError("1C command line is unverifiable")
        stripped = stripped[1:-1]
    if not stripped or '"' in stripped:
        raise AssertionError("1C command line is unverifiable")
    return stripped


def _command_infobase(command: Sequence[str]) -> Path:
    selectors: list[tuple[str, str]] = []
    index = 0
    while index < len(command):
        argument = command[index]
        if not isinstance(argument, str):
            raise AssertionError("1C command line is unverifiable")
        folded = argument.casefold()
        kind = _INFOBASE_SELECTORS.get(folded)
        if kind is not None:
            if index + 1 >= len(command):
                raise AssertionError("1C command line is unverifiable")
            next_argument = command[index + 1]
            if (
                not isinstance(next_argument, str)
                or any(
                    next_argument.casefold().startswith(token)
                    for token in _INFOBASE_SELECTORS
                )
            ):
                raise AssertionError("1C command line is unverifiable")
            selectors.append((kind, _selector_value(next_argument)))
            index += 2
            continue
        matching_tokens = tuple(
            token for token in _INFOBASE_SELECTORS if folded.startswith(token)
        )
        if matching_tokens:
            token = max(matching_tokens, key=len)
            suffix = argument[len(token) :]
            if not suffix.startswith('"'):
                raise AssertionError("1C command line is unverifiable")
            selectors.append(
                (_INFOBASE_SELECTORS[token], _selector_value(suffix))
            )
            index += 1
            continue
        if '"' in argument:
            _selector_value(argument)
        index += 1
    if len(selectors) != 1 or selectors[0][0] != "file":
        raise AssertionError("1C target identity is unverifiable")
    return Path(selectors[0][1])


def target_database_process_count(
    infobase: Path,
    *,
    processes: Sequence[psutil.Process] | None = None,
) -> int:
    target = canonical_database_identity(infobase)
    observed = (
        tuple(processes)
        if processes is not None
        else tuple(psutil.process_iter(("name",)))
    )
    count = 0
    for process in observed:
        try:
            name = process.name().casefold()
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError) as error:
            raise AssertionError("1C process observation is unverifiable") from error
        if name not in {"1cv8.exe", "1cv8c.exe"}:
            continue
        try:
            command = tuple(process.cmdline())
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError) as error:
            raise AssertionError("1C command line is unverifiable") from error
        if not command:
            raise AssertionError("1C command line is unverifiable")
        candidate = _command_infobase(command)
        if not candidate.is_absolute():
            try:
                process_cwd = Path(process.cwd())
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError) as error:
                raise AssertionError("1C target identity is unverifiable") from error
            if not process_cwd.is_absolute():
                raise AssertionError("1C target identity is unverifiable")
            candidate = process_cwd / candidate
        try:
            opened = canonical_database_identity(candidate)
        except (AssertionError, OSError) as error:
            raise AssertionError("1C target identity is unverifiable") from error
        if opened.filesystem_key == target.filesystem_key:
            count += 1
    return count


def assert_target_database_not_open(
    infobase: Path,
    *,
    processes: Sequence[psutil.Process] | None = None,
) -> None:
    if target_database_process_count(infobase, processes=processes):
        raise AssertionError("dedicated target infobase is already open")


def terminate_owned_popen(process: object, *, timeout_s: float = 5.0) -> None:
    """Terminate the exact child handle returned by Popen, adoption-independent."""
    poll = getattr(process, "poll", None)
    terminate = getattr(process, "terminate", None)
    wait = getattr(process, "wait", None)
    kill = getattr(process, "kill", None)
    if not all(callable(item) for item in (poll, terminate, wait, kill)):
        raise TypeError("owned process handle is invalid")
    if poll() is not None:
        return
    terminate()
    try:
        wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        kill()
        wait(timeout=timeout_s)


def process_matches_identity(process: psutil.Process, identity: ProcessIdentity) -> bool | None:
    """Return exact identity match, absence, or unverifiable state."""
    try:
        return (
            process.pid == identity.pid
            and process.create_time() == identity.create_time
            and str(Path(process.exe()).resolve()).casefold()
            == identity.executable.casefold()
        )
    except psutil.NoSuchProcess:
        return False
    except (psutil.AccessDenied, OSError):
        return None


def open_process_identity(
    identity: ProcessIdentity,
    *,
    process_factory: Callable[[int], psutil.Process] = psutil.Process,
) -> tuple[ProcessState, psutil.Process | None]:
    try:
        process = process_factory(identity.pid)
    except psutil.NoSuchProcess:
        return ProcessState.ABSENT, None
    except (psutil.AccessDenied, OSError):
        return ProcessState.UNKNOWN, None
    matches = process_matches_identity(process, identity)
    if matches is True:
        return ProcessState.ALIVE, process
    if matches is False:
        # A live process with the same PID but a different create_time/exe is
        # unrelated.  The owned identity is absent and must never be touched.
        return ProcessState.ABSENT, None
    return ProcessState.UNKNOWN, None


def process_identity_state(
    identity: ProcessIdentity,
    *,
    process_factory: Callable[[int], psutil.Process] = psutil.Process,
) -> ProcessState:
    return open_process_identity(identity, process_factory=process_factory)[0]


def terminate_exact_process(
    identity: ProcessIdentity,
    *,
    process_factory: Callable[[int], psutil.Process] = psutil.Process,
    timeout_s: float = 5.0,
) -> None:
    state, process = open_process_identity(identity, process_factory=process_factory)
    if state is ProcessState.ABSENT:
        return
    if state is not ProcessState.ALIVE or process is None:
        raise AssertionError(f"owned {identity.role} identity is not safely terminable")
    if process_matches_identity(process, identity) is not True:
        raise AssertionError(f"owned {identity.role} identity changed before terminate")
    process.terminate()
    try:
        process.wait(timeout=timeout_s)
    except psutil.TimeoutExpired:
        if process_matches_identity(process, identity) is not True:
            raise AssertionError(f"owned {identity.role} identity changed before kill")
        process.kill()
        process.wait(timeout=timeout_s)
    if process_identity_state(identity, process_factory=process_factory) is not ProcessState.ABSENT:
        raise AssertionError(f"owned {identity.role} process remains alive")


def identity_states(
    identities: Sequence[ProcessIdentity],
    *,
    process_factory: Callable[[int], psutil.Process] = psutil.Process,
) -> tuple[ProcessState, ...]:
    return tuple(
        process_identity_state(identity, process_factory=process_factory)
        for identity in identities
    )


class OwnedProcessTracker:
    """Persist exact descendants only while each immutable root still matches."""

    def __init__(
        self,
        private_path: Path,
        *,
        attempt: int,
        policy: ApprovedExecutablePolicy,
    ) -> None:
        if type(attempt) is not int or attempt <= 0:
            raise ValueError("attempt must be positive")
        if not isinstance(policy, ApprovedExecutablePolicy):
            raise TypeError("owned process tracker requires an executable policy")
        self._private_path = private_path
        self._attempt = attempt
        self._policy = policy
        self._roots: dict[int, ProcessIdentity] = {}
        self._identities: dict[tuple[int, float, str], ProcessIdentity] = {}
        self._lock = Lock()
        self._stop = Event()
        self._errors: list[str] = []
        self._thread = Thread(target=self._sample_loop, daemon=True)
        self._thread.start()

    def add_root(
        self,
        pid: int,
        role: str,
        *,
        required_cmdline: Sequence[str],
    ) -> ProcessIdentity:
        identity = adopt_owned_root(
            psutil.Process(pid),
            role=role,
            policy=self._policy,
            required_cmdline=required_cmdline,
        )
        self._record_identity(identity)
        with self._lock:
            self._roots[pid] = identity
        return identity

    def identities(self) -> tuple[ProcessIdentity, ...]:
        with self._lock:
            return tuple(self._identities.values())

    def stop(self) -> list[str]:
        self._stop.set()
        self._thread.join(timeout=2)
        if self._thread.is_alive():
            self._error("SamplerJoinTimeout")
        try:
            self._sample_once()
            self._persist()
        except BaseException as error:
            self._error(type(error).__name__)
        with self._lock:
            return list(self._errors)

    def _sample_loop(self) -> None:
        while not self._stop.wait(0.02):
            try:
                self._sample_once()
            except BaseException as error:
                self._error(type(error).__name__)

    def _sample_once(self) -> None:
        with self._lock:
            roots = tuple(self._roots.values())
        pending = list(roots)
        visited: set[tuple[int, float, str]] = set()
        while pending:
            identity = pending.pop(0)
            identity_key = (
                identity.pid,
                identity.create_time,
                identity.executable.casefold(),
            )
            if identity_key in visited:
                continue
            visited.add(identity_key)
            state, process = open_process_identity(identity)
            if state is ProcessState.ABSENT:
                continue
            if state is ProcessState.UNKNOWN or process is None:
                self._error("RootIdentityUnverifiable")
                continue
            try:
                descendants = process.children(recursive=False)
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError) as error:
                self._error(type(error).__name__)
                continue
            for child in descendants:
                try:
                    name = child.name().casefold()
                except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                    self._error("DescendantIdentityUnverifiable")
                    continue
                if name not in {"1cv8.exe", "1cv8c.exe", "dbgs.exe"}:
                    continue
                try:
                    adopted = adopt_owned_descendant(
                        child, parent=identity, policy=self._policy
                    )
                except AssertionError:
                    self._error("DescendantPolicyMismatch")
                    continue
                self._record_identity(adopted)
                pending.append(adopted)

    def _record_identity(self, identity: ProcessIdentity) -> None:
        key = (identity.pid, identity.create_time, identity.executable.casefold())
        with self._lock:
            self._identities[key] = identity
        self._persist()

    def _persist(self) -> None:
        with self._lock:
            payload = [item.private_wire() for item in self._identities.values()]
        self._private_path.parent.mkdir(parents=True, exist_ok=True)
        self._private_path.write_text(
            json.dumps(
                {"attempt": self._attempt, "owned": payload},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def _error(self, name: str) -> None:
        with self._lock:
            self._errors.append(name)


__all__ = [
    "ApprovedExecutablePolicy",
    "CanonicalDatabaseIdentity",
    "OwnedProcessTracker",
    "ProcessIdentity",
    "ProcessState",
    "adopt_owned_descendant",
    "adopt_owned_root",
    "assert_target_database_not_open",
    "canonical_database_identity",
    "database_identity_sha256",
    "identity_states",
    "open_process_identity",
    "process_identity_state",
    "process_matches_identity",
    "terminate_exact_process",
    "terminate_owned_popen",
    "target_database_process_count",
]
