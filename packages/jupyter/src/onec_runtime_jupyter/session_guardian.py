"""Release a bound server session when an abruptly stopped kernel cannot do it.

VS Code can terminate a raw kernel and its children without running Python's
shutdown hooks.  The guardian is created by WMI as a sibling, so it survives
that process-tree termination.  It never owns or stops the shared 1C debugger.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from time import monotonic, sleep
from typing import Callable
from uuid import UUID, uuid4

import psutil

from onec_runtime.rdbg.models import ModuleLocation, TargetId
from onec_runtime.rdbg.session import RdbgSession, SessionState
from onec_runtime.rdbg.transport import RdbgTransport


_UUID_PATTERN = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass(frozen=True, slots=True)
class GuardLease:
    owner_pid: int
    owner_create_time: float
    client_pid: int
    client_create_time: float
    client_executable: str
    ui_id: UUID
    client_target_id: UUID
    session_id: UUID
    session_number: int | None
    infobase_id: UUID
    config_version: str
    alias: str
    debug_host: str
    debug_port: int
    server_host: str
    platform_bin: str

    @classmethod
    def from_runtime(cls, runtime: object) -> GuardLease | None:
        config = getattr(getattr(runtime, "config", None), "runtime", None)
        if config is None or not config.is_server_infobase:
            return None
        bound = runtime._rdbg._bound_client_target  # type: ignore[attr-defined]
        if (
            bound is None
            or bound.seance_id is None
            or bound.infobase_instance_id is None
            or bound.infobase_alias.casefold() != config.infobase_debug_alias.casefold()
        ):
            raise ValueError("Server guardian requires an authenticated bound client")
        owned = runtime.owned_process_snapshot()  # type: ignore[attr-defined]
        if len(owned) != 1 or owned[0]["role"] != "onec":
            raise ValueError("Server guardian requires one owned 1C client")
        owner = psutil.Process(os.getpid())
        return cls(
            owner_pid=owner.pid,
            owner_create_time=owner.create_time(),
            client_pid=int(owned[0]["pid"]),
            client_create_time=float(owned[0]["create_time"]),
            client_executable=str(owned[0]["executable"]),
            ui_id=runtime._rdbg.ui_id,  # type: ignore[attr-defined]
            client_target_id=bound.id,
            session_id=bound.seance_id,
            session_number=bound.seance_no,
            infobase_id=bound.infobase_instance_id,
            config_version=bound.config_version,
            alias=config.infobase_debug_alias,
            debug_host=config.debug_host,
            debug_port=config.debug_port,
            server_host=config.infobase_arguments[1].split("\\", 1)[0],
            platform_bin=str(config.platform_bin),
        )

    def to_json(self) -> str:
        return json.dumps(
            {key: str(value) if isinstance(value, UUID) else value
             for key, value in asdict(self).items()},
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, payload: str) -> GuardLease:
        raw = json.loads(payload)
        if not isinstance(raw, dict) or set(raw) != set(cls.__dataclass_fields__):
            raise ValueError("Invalid kernel guardian lease")
        for key in ("ui_id", "client_target_id", "session_id", "infobase_id"):
            raw[key] = UUID(raw[key])
        lease = cls(**raw)
        if (
            type(lease.owner_pid) is not int or lease.owner_pid <= 0
            or type(lease.client_pid) is not int or lease.client_pid <= 0
            or type(lease.debug_port) is not int or not 0 < lease.debug_port < 65536
            or not lease.alias or not lease.server_host
        ):
            raise ValueError("Invalid kernel guardian lease identity")
        return lease


@dataclass(slots=True)
class GuardianHandle:
    lease_path: Path
    pid: int

    def stop(self) -> None:
        self.lease_path.with_suffix(".stop").touch(exist_ok=True)


def _process_alive(pid: int, create_time: float) -> bool:
    try:
        process = psutil.Process(pid)
        return abs(process.create_time() - create_time) < 0.01
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        # Missing permission is not evidence that the kernel has died.
        return True


def _owner_alive(lease: GuardLease) -> bool:
    return _process_alive(lease.owner_pid, lease.owner_create_time)


def _close_owned_client(lease: GuardLease) -> bool:
    try:
        process = psutil.Process(lease.client_pid)
        if (
            abs(process.create_time() - lease.client_create_time) >= 0.01
            or os.path.normcase(str(Path(process.exe()).resolve()))
            != os.path.normcase(str(Path(lease.client_executable).resolve()))
        ):
            return False
        process.terminate()
        try:
            process.wait(timeout=3.0)
        except psutil.TimeoutExpired:
            process.kill()
            process.wait(timeout=3.0)
        return True
    except psutil.NoSuchProcess:
        return False


def _spawn_outside_kernel_tree(command: list[str]) -> int:
    """WMI creates the process under WmiPrvSE, not under the raw kernel."""
    command_line = subprocess.list2cmdline(command).replace("'", "''")
    script = (
        "$p = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
        f"-Arguments @{{CommandLine='{command_line}'}}; "
        "if ($p.ReturnValue -ne 0) { throw ('WMI Create failed: ' + $p.ReturnValue) }; "
        "$p.ProcessId"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=15, creationflags=_CREATE_NO_WINDOW,
        check=True,
    )
    return int(result.stdout.strip().splitlines()[-1])


def start_guardian(runtime: object) -> GuardianHandle | None:
    lease = GuardLease.from_runtime(runtime)
    if lease is None or os.name != "nt":
        return None
    workspace = Path(runtime.config.runtime.workspace)  # type: ignore[attr-defined]
    directory = workspace / ".runtime" / "kernel-guardians"
    directory.mkdir(parents=True, exist_ok=True)
    lease_path = directory / f"{uuid4()}.json"
    temporary = lease_path.with_suffix(".tmp")
    temporary.write_text(lease.to_json(), encoding="utf-8")
    temporary.replace(lease_path)
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.is_file():
        lease_path.unlink(missing_ok=True)
        raise RuntimeError("Python windowless executable is required for 1C kernel cleanup")
    try:
        pid = _spawn_outside_kernel_tree(
            [str(pythonw), str(Path(__file__).resolve()), str(lease_path)]
        )
        ready = lease_path.with_suffix(".ready")
        deadline = monotonic() + 10.0
        while monotonic() < deadline:
            if ready.exists():
                return GuardianHandle(lease_path, pid)
            if not psutil.pid_exists(pid):
                break
            sleep(0.05)
        raise RuntimeError("1C kernel guardian did not become ready")
    except BaseException:
        lease_path.with_suffix(".stop").touch(exist_ok=True)
        lease_path.unlink(missing_ok=True)
        raise


def _terminate_via_rdbg(lease: GuardLease) -> None:
    # The old UI is still registered for a short time after the raw kernel is
    # killed.  Reusing its exact identity avoids a competing UI registration.
    location = ModuleLocation("ExtensionModule", "", UUID(int=1), UUID(int=1), 1)
    with RdbgTransport(lease.debug_host, lease.debug_port) as transport:
        session = RdbgSession(
            transport, location, alias=lease.alias, ui_id=lease.ui_id,
            server_target_type="Server",
        )
        session.state = SessionState.ATTACHED
        session._bound_client_target = TargetId(
            lease.client_target_id, lease.alias, lease.session_id,
            seance_no=lease.session_number,
            infobase_instance_id=lease.infobase_id,
            config_version=lease.config_version,
        )
        try:
            session.terminate_bound_server_session()
        finally:
            session.detach()


def _rac_output(command: list[str]) -> str:
    result = subprocess.run(
        command, capture_output=True, timeout=8, creationflags=_CREATE_NO_WINDOW,
    )
    if result.returncode:
        raise RuntimeError(f"1C administrative command failed: {command[1:3]}")
    return result.stdout.decode("utf-8", errors="replace").lstrip("\ufeff")


def _field(output: str, name: str) -> str | None:
    match = re.search(rf"(?m)^{re.escape(name)}\s*:\s*(\S+)", output)
    return match.group(1) if match else None


def _terminate_exact_rac_session(
    lease: GuardLease, runner: Callable[[list[str]], str], *, port: int = 1665,
) -> bool:
    rac = str(Path(lease.platform_bin) / "rac.exe")
    endpoint = f"localhost:{port}"
    clusters = re.findall(rf"(?mi)^cluster\s*:\s*({_UUID_PATTERN})", runner([rac, "cluster", "list", endpoint]))
    if not clusters:
        raise RuntimeError("1C administrative cluster discovery is incomplete")
    for cluster in clusters:
        base = [rac, "session", "info", f"--cluster={cluster}", f"--session={lease.session_id}", endpoint]
        try:
            info = runner(base)
        except RuntimeError:
            continue
        if (
            _field(info, "session") != str(lease.session_id)
            or _field(info, "infobase") != str(lease.infobase_id)
            or _field(info, "app-id") != "1CV8C"
        ):
            continue
        runner([
            rac, "session", "terminate", f"--cluster={cluster}",
            f"--session={lease.session_id}", endpoint,
        ])
        deadline = monotonic() + 5.0
        while monotonic() < deadline:
            try:
                remaining = runner(base)
            except RuntimeError:
                return True
            if _field(remaining, "session") != str(lease.session_id):
                return True
            sleep(0.1)
        return False
    return False


def _terminate_via_rac(lease: GuardLease) -> bool:
    platform = Path(lease.platform_bin)
    if not (platform / "ras.exe").is_file() or not (platform / "rac.exe").is_file():
        return False
    host, sep, port_text = lease.server_host.rpartition(":")
    if sep and port_text.isdecimal():
        agent = f"{host}:{int(port_text) - 1}"
    else:
        agent = f"{lease.server_host}:1540"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    ras = subprocess.Popen(
        [str(platform / "ras.exe"), "cluster", f"--port={port}", agent],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=_CREATE_NO_WINDOW,
    )
    try:
        deadline = monotonic() + 5.0
        while monotonic() < deadline:
            try:
                return _terminate_exact_rac_session(lease, _rac_output, port=port)
            except RuntimeError:
                if ras.poll() is not None:
                    raise
                sleep(0.1)
        raise RuntimeError("1C administration server did not become ready")
    finally:
        if ras.poll() is None:
            ras.terminate()
            try:
                ras.wait(timeout=3)
            except subprocess.TimeoutExpired:
                ras.kill()
                ras.wait(timeout=3)


def _cleanup(lease: GuardLease) -> dict[str, object]:
    errors: list[str] = []
    rdbg_done = False
    rac_done = False
    client_closed = False
    try:
        _terminate_via_rdbg(lease)
        rdbg_done = True
    except Exception as error:
        errors.append("RDBG:" + type(error).__name__)
    try:
        rac_done = _terminate_via_rac(lease)
    except Exception as error:
        errors.append("RAC:" + type(error).__name__)
    try:
        client_closed = _close_owned_client(lease)
    except Exception as error:
        errors.append("client:" + type(error).__name__)
    return {"session_id": str(lease.session_id), "rdbg": rdbg_done,
            "rac": rac_done, "client_closed": client_closed, "errors": errors}


def recover_failed_guards(config: object) -> tuple[str, ...]:
    """Retry completed failed cleanups before a new server session starts."""
    runtime = getattr(config, "runtime", None)
    if runtime is None or not runtime.is_server_infobase:
        return ()
    directory = Path(runtime.workspace) / ".runtime" / "kernel-guardians"
    if not directory.is_dir():
        return ()
    server_host = runtime.infobase_arguments[1].split("\\", 1)[0]
    failures: list[str] = []
    for path in directory.glob("*.json"):
        if path.name.endswith(".outcome.json"):
            continue
        outcome_path = path.with_suffix(".outcome.json")
        try:
            lease = GuardLease.from_json(path.read_text(encoding="utf-8"))
            if (
                lease.alias.casefold() != runtime.infobase_debug_alias.casefold()
                or lease.server_host.casefold() != server_host.casefold()
                or _owner_alive(lease)
            ):
                continue
            if outcome_path.is_file():
                previous = json.loads(outcome_path.read_text(encoding="utf-8"))
                if previous.get("stopped") or previous.get("rdbg") or previous.get("rac"):
                    continue
            else:
                ready = path.with_suffix(".ready")
                if ready.is_file():
                    identity = json.loads(ready.read_text(encoding="utf-8"))
                    if _process_alive(identity["pid"], identity["create_time"]):
                        # Its guardian is still working. Do not compete with it.
                        continue
            result = _cleanup(lease)
            outcome_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            if result.get("rdbg") or result.get("rac"):
                path.unlink(missing_ok=True)
            else:
                failures.append(str(lease.session_id))
        except (OSError, ValueError, TypeError):
            failures.append(path.stem)
    return tuple(failures)


def run_guardian(lease_path: Path) -> None:
    lease = GuardLease.from_json(lease_path.read_text(encoding="utf-8"))
    ready = lease_path.with_suffix(".ready")
    stop = lease_path.with_suffix(".stop")
    outcome = lease_path.with_suffix(".outcome.json")
    process = psutil.Process(os.getpid())
    ready.write_text(
        json.dumps({"pid": process.pid, "create_time": process.create_time()}),
        encoding="utf-8",
    )
    while not stop.exists() and _owner_alive(lease):
        sleep(0.25)
    if stop.exists():
        result: dict[str, object] = {"session_id": str(lease.session_id), "stopped": True}
    else:
        result = _cleanup(lease)
    outcome.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    if stop.exists() or result.get("rdbg") or result.get("rac"):
        lease_path.unlink(missing_ok=True)
    ready.unlink(missing_ok=True)
    stop.unlink(missing_ok=True)


if __name__ == "__main__":
    run_guardian(Path(sys.argv[1]))
