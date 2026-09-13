from __future__ import annotations

import asyncio
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Event, Lock, Thread
from time import monotonic, sleep
from types import SimpleNamespace
from typing import Any

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
import nbformat
import psutil
import pytest

from integration.evidence.live_evidence import (
    EXPECTED_CALL_SEQUENCE,
    EXPECTED_MESSAGES,
    ExpectedLiveResult,
    build_mcp_zup_fail_observations,
    build_mcp_zup_terminal_fail_observations,
    evidence_identity,
    verify_mcp_zup_evidence,
    write_mcp_zup_evidence,
)
from integration.evidence.value_python_evidence import (
    EXPECTED_CALL_SEQUENCE as EXPECTED_VALUE_PYTHON_CALL_SEQUENCE,
    verify_value_python_evidence,
    write_value_python_evidence,
)
from onec_runtime_mcp.agent.control_protocol import ControlEndpoint
from onec_runtime_mcp.agent.service_client import ServiceClient


_WORKSPACE = Path(__file__).resolve().parents[2]
_NOTEBOOK = _WORKSPACE / "tests" / "fixtures" / "notebooks" / "mcp-zup-main-acceptance.ipynb"
_CONTAINER = "tests/fixtures/notebooks/mcp-zup-main-acceptance.ipynb"
_CELL_ID = "mcp-zup-main-acceptance"
_ATTEMPT = 4
_CONTROL_TIMEOUT_S = 180.0
_PUBLIC_EVIDENCE = (
    _WORKSPACE
    / "docs"
    / "research"
    / "evidence"
    / "2026-08-16-mcp-runtime-foundation"
    / "attempt-4"
)
_PRIVATE_EVIDENCE = (
    _WORKSPACE
    / ".runtime"
    / "agent-service"
    / "live-acceptance-private"
    / "attempt-4-processes.json"
)
_SALT = "onec-mcp-zup-main-2026-08-17-attempt-4"
_VALUE_PYTHON_NOTEBOOK = (
    _WORKSPACE / "tests" / "fixtures" / "notebooks" / "mcp-zup-value-python-acceptance.ipynb"
)
_VALUE_PYTHON_CONTAINER = "tests/fixtures/notebooks/mcp-zup-value-python-acceptance.ipynb"
_VALUE_PYTHON_CELL_ID = "mcp-zup-value-python-acceptance"
_VALUE_PYTHON_PUBLIC_EVIDENCE = (
    _WORKSPACE
    / "docs"
    / "research"
    / "evidence"
    / "2026-08-18-agent-value-python-workspace"
    / "attempt-1"
)
_VALUE_PYTHON_PRIVATE_EVIDENCE = (
    _WORKSPACE
    / ".runtime"
    / "agent-service"
    / "value-python-private"
    / "attempt-1-processes.json"
)


@dataclass(frozen=True, slots=True)
class _ProcessIdentity:
    role: str
    pid: int
    create_time: float
    executable: str

    def private_wire(self) -> dict[str, object]:
        return {
            "role": self.role,
            "pid": self.pid,
            "create_time": self.create_time,
            "executable": self.executable,
        }


@dataclass(frozen=True, slots=True)
class _ControlFiles:
    descriptor: bytes | None
    token: bytes | None


class _McpEnvelopeFailure(AssertionError):
    def __init__(self, method: str, failure: dict[str, object]) -> None:
        super().__init__(f"{method} returned a sanitized MCP failure")
        self.method = method
        self.category = failure.get("category")
        self.state_changed = failure.get("state_changed")
        self.safe_to_retry = failure.get("safe_to_retry")
        current_state = failure.get("current_state")
        configured = (
            current_state.get("configured_timeout_s")
            if isinstance(current_state, dict)
            else None
        )
        self.local_timeout_origin = bool(
            isinstance(current_state, dict)
            and current_state.get("control_transport") == "timeout"
            and type(configured) in {int, float}
        )
        self.configured_timeout_s = (
            float(configured) if self.local_timeout_origin else None
        )
        self.runtime_ensure_duration_s: float | None = None


class _OwnedProcessTracker:
    """Track only exact process identities rooted in this acceptance probe."""

    def __init__(self, private_path: Path, *, attempt: int = _ATTEMPT) -> None:
        self._private_path = private_path
        self._attempt = attempt
        self._roots: dict[int, _ProcessIdentity] = {}
        self._identities: dict[tuple[int, float, str], _ProcessIdentity] = {}
        self._lock = Lock()
        self._stop = Event()
        self._sampling_errors: list[str] = []
        self._thread = Thread(target=self._sample_loop, name="mcp-live-owned-process-tracker", daemon=True)
        self._thread.start()

    def add_root(self, pid: int, role: str) -> _ProcessIdentity:
        identity = self._record(psutil.Process(pid), role)
        with self._lock:
            self._roots[pid] = identity
        return identity

    def identities(self) -> tuple[_ProcessIdentity, ...]:
        with self._lock:
            return tuple(self._identities.values())

    def stop(self) -> list[str]:
        self._stop.set()
        self._thread.join(timeout=2)
        if self._thread.is_alive():
            self._sampling_error("SamplerJoinTimeout")
        try:
            self._sample_once()
            self._persist()
        except BaseException as error:
            self._sampling_error(type(error).__name__)
        with self._lock:
            return list(self._sampling_errors)

    def _sample_loop(self) -> None:
        while not self._stop.wait(0.02):
            try:
                self._sample_once()
            except BaseException as error:
                self._sampling_error(type(error).__name__)

    def _sample_once(self) -> None:
        with self._lock:
            roots = tuple(self._roots.values())
        for identity in roots:
            try:
                root = psutil.Process(identity.pid)
                matches = _process_matches_identity(root, identity)
                if matches is False:
                    self._sampling_error("RootIdentityMismatch")
                    continue
                if matches is None:
                    self._sampling_error("RootIdentityUnverifiable")
                    continue
                descendants = root.children(recursive=True)
            except psutil.NoSuchProcess:
                continue
            except (psutil.AccessDenied, OSError) as error:
                self._sampling_error(type(error).__name__)
                continue
            for process in descendants:
                try:
                    name = process.name().casefold()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
                role = {
                    "1cv8.exe": "designer",
                    "1cv8c.exe": "onec",
                    "dbgs.exe": "dbgs",
                    "python.exe": "python",
                }.get(name)
                if role is not None:
                    try:
                        self._record(process, role)
                    except AssertionError as error:
                        self._sampling_error(type(error.__cause__).__name__)

    def _sampling_error(self, name: str) -> None:
        with self._lock:
            self._sampling_errors.append(name)

    def _record(self, process: psutil.Process, role: str) -> _ProcessIdentity:
        try:
            identity = _ProcessIdentity(
                role=role,
                pid=process.pid,
                create_time=process.create_time(),
                executable=str(Path(process.exe()).resolve()),
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError) as error:
            raise AssertionError(f"unable to record owned {role} process identity") from error
        key = (identity.pid, identity.create_time, identity.executable.casefold())
        with self._lock:
            self._identities[key] = identity
        self._persist()
        return identity

    def _persist(self) -> None:
        with self._lock:
            payload = [item.private_wire() for item in self._identities.values()]
        self._private_path.parent.mkdir(parents=True, exist_ok=True)
        self._private_path.write_text(
            json.dumps({"attempt": self._attempt, "owned": payload}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def _open_identity(
    identity: _ProcessIdentity,
) -> tuple[bool | None, psutil.Process | None]:
    try:
        process = psutil.Process(identity.pid)
        matches = _process_matches_identity(process, identity)
        return matches, process if matches is True else None
    except psutil.NoSuchProcess:
        return False, None
    except (psutil.AccessDenied, OSError):
        return None, None


def _process_matches_identity(
    process: psutil.Process,
    identity: _ProcessIdentity,
) -> bool | None:
    try:
        return (
            abs(process.create_time() - identity.create_time) < 0.001
            and str(Path(process.exe()).resolve()).casefold() == identity.executable.casefold()
        )
    except psutil.NoSuchProcess:
        return False
    except (psutil.AccessDenied, OSError):
        return None


def _identity_alive(identity: _ProcessIdentity) -> bool | None:
    return _open_identity(identity)[0]


def _terminate_exact(identity: _ProcessIdentity) -> None:
    alive, process = _open_identity(identity)
    if alive is False:
        return
    if alive is None or process is None:
        raise AssertionError(f"unable to verify owned {identity.role} process identity")
    if _process_matches_identity(process, identity) is not True:
        raise AssertionError(f"unable to reverify owned {identity.role} process identity")
    process.terminate()
    try:
        process.wait(timeout=5)
    except psutil.TimeoutExpired:
        alive = _identity_alive(identity)
        if alive is None:
            raise AssertionError(f"unable to verify owned {identity.role} process identity")
        if alive:
            process.kill()
            process.wait(timeout=5)
    if _identity_alive(identity) is not False:
        raise AssertionError(f"owned {identity.role} process absence was not proven")


def _snapshot_control_files(descriptor: Path) -> _ControlFiles:
    token = descriptor.with_name("token")
    return _ControlFiles(
        descriptor.read_bytes() if descriptor.is_file() else None,
        token.read_bytes() if token.is_file() else None,
    )


def _restore_control_files_if_owned(
    descriptor: Path,
    *,
    owned: _ControlFiles,
    previous: _ControlFiles,
) -> None:
    current = _snapshot_control_files(descriptor)
    if current != owned:
        raise AssertionError("shared control files changed after owned service publication")
    token = descriptor.with_name("token")
    for path, content in ((descriptor, previous.descriptor), (token, previous.token)):
        if content is None:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        else:
            path.write_bytes(content)


def _find_mcp_process(tracker: _OwnedProcessTracker, role: str) -> _ProcessIdentity:
    known = {item.pid for item in tracker.identities()}
    deadline = monotonic() + 5
    while monotonic() < deadline:
        parent = psutil.Process(os.getpid())
        for process in parent.children(recursive=False):
            if process.pid in known:
                continue
            try:
                command = " ".join(process.cmdline()).casefold()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if "onec_runtime_mcp.mcp_entrypoint" in command:
                return tracker.add_root(process.pid, role)
        sleep(0.02)
    raise AssertionError(f"owned {role} frontend process was not discovered")


def _value(result: Any, method: str) -> dict[str, object] | list[object]:
    payload = result.structured_content
    if isinstance(payload, dict) and payload.get("ok") is False:
        failure = payload.get("failure")
        if isinstance(failure, dict):
            raise _McpEnvelopeFailure(method, failure)
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise AssertionError(f"{method} did not return a successful MCP envelope")
    value = payload.get("value")
    if not isinstance(value, (dict, list)):
        raise AssertionError(f"{method} returned an invalid MCP value")
    return value


async def _run_frontend_a(
    parameters: StdioServerParameters,
    tracker: _OwnedProcessTracker,
    calls: list[str],
) -> dict[str, object]:
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr:
        async with stdio_client(parameters, errlog=stderr) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                frontend = _find_mcp_process(tracker, "mcp_a")
                opened = _value(await session.call_tool("workspace.open", {"project": str(_WORKSPACE)}), "workspace.open")
                calls.append("A:workspace.open")
                ensure_started = monotonic()
                try:
                    runtime = _value(
                        await session.call_tool(
                            "runtime.ensure", {"mode": "experiment"}
                        ),
                        "runtime.ensure",
                    )
                except _McpEnvelopeFailure as failure:
                    failure.runtime_ensure_duration_s = round(
                        monotonic() - ensure_started,
                        6,
                    )
                    raise
                ensure_duration = round(monotonic() - ensure_started, 6)
                calls.append("A:runtime.ensure")
                listed = _value(await session.call_tool("code.list", {"container": _CONTAINER}), "code.list")
                calls.append("A:code.list")
                revision = _value(await session.call_tool("code.get", {"cell_id": _CELL_ID}), "code.get")
                calls.append("A:code.get")
                run_started = monotonic()
                operation = _value(
                    await session.call_tool(
                        "code.run",
                        {
                            "cell_id": _CELL_ID,
                            "revision": revision["revision"],
                            "source_sha256": revision["source_sha256"],
                            "wait_s": 0.1,
                        },
                    ),
                    "code.run",
                )
                run_duration = round(monotonic() - run_started, 6)
                calls.append("A:code.run")
                return {
                    "frontend": frontend,
                    "opened": opened,
                    "runtime": runtime,
                    "listed": listed,
                    "revision": revision,
                    "operation": operation,
                    "ensure_duration_s": ensure_duration,
                    "run_duration_s": run_duration,
                }


async def _run_frontend_b(
    parameters: StdioServerParameters,
    tracker: _OwnedProcessTracker,
    calls: list[str],
    operation_id: str,
) -> dict[str, object]:
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr:
        async with stdio_client(parameters, errlog=stderr) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                frontend = _find_mcp_process(tracker, "mcp_b")
                reconnect_started = monotonic()
                opened = _value(await session.call_tool("workspace.open", {"project": str(_WORKSPACE)}), "workspace.open")
                calls.append("B:workspace.open")
                ensured = _value(await session.call_tool("runtime.ensure", {"mode": "experiment"}), "runtime.ensure")
                calls.append("B:runtime.ensure")
                runtime = _value(await session.call_tool("runtime.status", {}), "runtime.status")
                calls.append("B:runtime.status")
                reconnect_duration = round(monotonic() - reconnect_started, 6)
                recovery_started = monotonic()
                before_wait = _value(await session.call_tool("operation.status", {"operation_id": operation_id}), "operation.status")
                calls.append("B:operation.status")
                waited = _value(
                    await session.call_tool(
                        "operation.wait",
                        {"operation_id": operation_id, "timeout_s": 30, "after_cursor": before_wait["event_cursor"]},
                    ),
                    "operation.wait",
                )
                calls.append("B:operation.wait")
                terminal = _value(await session.call_tool("operation.status", {"operation_id": operation_id}), "operation.status")
                calls.append("B:operation.status")
                output = _value(
                    await session.call_tool(
                        "operation.output",
                        {"operation_id": operation_id, "after_cursor": 0, "messages": 100},
                    ),
                    "operation.output",
                )
                calls.append("B:operation.output")
                result_call = await session.call_tool("operation.result", {"operation_id": operation_id})
                calls.append("B:operation.result")
                result_payload = result_call.structured_content
                history = _value(await session.call_tool("code.history", {"cell_id": _CELL_ID}), "code.history")
                calls.append("B:code.history")
                recovery_duration = round(monotonic() - recovery_started, 6)
                close_started = monotonic()
                closed = _value(await session.call_tool("runtime.close", {"policy": "abort_generation"}), "runtime.close")
                calls.append("B:runtime.close")
                close_duration = round(monotonic() - close_started, 6)
                return {
                    "frontend": frontend,
                    "opened": opened,
                    "ensured": ensured,
                    "runtime": runtime,
                    "before_wait": before_wait,
                    "waited": waited,
                    "terminal": terminal,
                    "output": output,
                    "result": result_payload,
                    "history": history,
                    "closed": closed,
                    "reconnect_duration_s": reconnect_duration,
                    "recovery_duration_s": recovery_duration,
                    "close_duration_s": close_duration,
                }


def _sha_file(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _target_extension_static_check_passed() -> bool:
    path = _WORKSPACE / ".runtime" / "logs" / "check-target-extension-zup.log"
    try:
        data = path.read_bytes()
    except OSError:
        return False
    marker = "Ошибок не обнаружено"
    return any(
        marker.encode(encoding) in data
        for encoding in ("utf-8", "utf-16-le", "cp1251")
    )


def _phase(name: str, started: float) -> dict[str, object]:
    return {"phase": name, "duration_s": round(monotonic() - started, 6)}


def _contains_key(value: object, forbidden: str) -> bool:
    if isinstance(value, dict):
        return forbidden in value or any(_contains_key(item, forbidden) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, forbidden) for item in value)
    return False


def _role_hash(identities: tuple[_ProcessIdentity, ...], role: str) -> str:
    selected = sorted(
        (
            f"{item.pid}|{item.create_time:.6f}|{item.executable.casefold()}"
            for item in identities
            if item.role == role
        )
    )
    if not selected:
        raise AssertionError(f"owned {role} identity was not recorded")
    return evidence_identity(_SALT, role, *selected)


def _failure_value(result: Any, method: str) -> dict[str, object]:
    payload = result.structured_content
    if not isinstance(payload, dict) or payload.get("ok") is not False:
        raise AssertionError(f"{method} did not return a failed MCP envelope")
    failure = payload.get("failure")
    if not isinstance(failure, dict):
        raise AssertionError(f"{method} returned an invalid MCP failure")
    return failure


async def _run_value_python_frontend_a(
    parameters: StdioServerParameters,
    tracker: _OwnedProcessTracker,
    calls: list[str],
) -> dict[str, object]:
    budget = {
        "depth": 8,
        "items": 10_000,
        "rows": 100,
        "bytes": 8 * 1024 * 1024,
        "timeout_s": 30,
    }
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr:
        async with stdio_client(parameters, errlog=stderr) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                frontend = _find_mcp_process(tracker, "mcp_a")
                _value(
                    await session.call_tool(
                        "workspace.open", {"project": str(_WORKSPACE)}
                    ),
                    "workspace.open",
                )
                calls.append("A:workspace.open")
                runtime = _value(
                    await session.call_tool("runtime.ensure", {"mode": "experiment"}),
                    "runtime.ensure",
                )
                calls.append("A:runtime.ensure")
                listed = _value(
                    await session.call_tool(
                        "code.list", {"container": _VALUE_PYTHON_CONTAINER}
                    ),
                    "code.list",
                )
                calls.append("A:code.list")
                revision = _value(
                    await session.call_tool(
                        "code.get", {"cell_id": _VALUE_PYTHON_CELL_ID}
                    ),
                    "code.get",
                )
                calls.append("A:code.get")
                main_started = monotonic()
                operation = _value(
                    await session.call_tool(
                        "code.run",
                        {
                            "cell_id": _VALUE_PYTHON_CELL_ID,
                            "revision": revision["revision"],
                            "source_sha256": revision["source_sha256"],
                            "wait_s": 30,
                        },
                    ),
                    "code.run",
                )
                main_duration = round(monotonic() - main_started, 6)
                calls.append("A:code.run")
                variables = _value(
                    await session.call_tool(
                        "workspace.variables", {"namespace": "bsl"}
                    ),
                    "workspace.variables",
                )
                calls.append("A:workspace.variables")
                table = next(
                    item
                    for item in variables
                    if item["qualified_name"] == "bsl.АгентТаблица"
                )
                table_size = _value(
                    await session.call_tool(
                        "value.size", {"proxy_id": table["proxy_id"]}
                    ),
                    "value.size",
                )
                calls.append("A:value.size")
                to_df_started = monotonic()
                frame = _value(
                    await session.call_tool(
                        "value.to_df",
                        {
                            "proxy_id": table["proxy_id"],
                            "budget": budget,
                            "refs": "both",
                        },
                    ),
                    "value.to_df",
                )
                to_df_duration = round(monotonic() - to_df_started, 6)
                calls.append("A:value.to_df")
                frame_inspection = _value(
                    await session.call_tool(
                        "python.inspect", {"proxy_id": frame["proxy_id"]}
                    ),
                    "python.inspect",
                )
                calls.append("A:python.inspect.frame")
                python_started = monotonic()
                run = _value(
                    await session.call_tool(
                        "python.run",
                        {
                            "code": (
                                "ИтогиПоОрганизации = "
                                "source.groupby('Организация', dropna=False)"
                                ".size().reset_index(name='Количество')"
                            ),
                            "inputs": {"source": frame["proxy_id"]},
                            "outputs": ["ИтогиПоОрганизации"],
                        },
                    ),
                    "python.run",
                )
                python_duration = round(monotonic() - python_started, 6)
                calls.append("A:python.run")
                summary = run["outputs"]["ИтогиПоОрганизации"]
                summary_inspection = _value(
                    await session.call_tool(
                        "python.inspect", {"proxy_id": summary["proxy_id"]}
                    ),
                    "python.inspect",
                )
                calls.append("A:python.inspect.summary")
                return {
                    "frontend": frontend,
                    "runtime": runtime,
                    "listed": listed,
                    "revision": revision,
                    "operation": operation,
                    "variables": variables,
                    "table": table,
                    "table_size": table_size,
                    "frame": frame,
                    "frame_inspection": frame_inspection,
                    "summary": summary,
                    "summary_inspection": summary_inspection,
                    "main_duration_s": main_duration,
                    "to_df_duration_s": to_df_duration,
                    "python_duration_s": python_duration,
                }


async def _run_value_python_frontend_b(
    parameters: StdioServerParameters,
    tracker: _OwnedProcessTracker,
    calls: list[str],
    *,
    onec_proxy_id: str,
    python_proxy_id: str,
) -> dict[str, object]:
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr:
        async with stdio_client(parameters, errlog=stderr) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                frontend = _find_mcp_process(tracker, "mcp_b")
                status = _value(
                    await session.call_tool("workspace.status", {}),
                    "workspace.status",
                )
                calls.append("B:workspace.status")
                variables = _value(
                    await session.call_tool("python.variables", {}),
                    "python.variables",
                )
                calls.append("B:python.variables")
                before = _value(
                    await session.call_tool(
                        "python.inspect", {"proxy_id": python_proxy_id}
                    ),
                    "python.inspect",
                )
                calls.append("B:python.inspect")
                ensured = _value(
                    await session.call_tool("runtime.ensure", {"mode": "experiment"}),
                    "runtime.ensure",
                )
                calls.append("B:runtime.ensure")
                restart_started = monotonic()
                restarted = _value(
                    await session.call_tool(
                        "runtime.restart",
                        {"policy": "abort_generation", "mode": "experiment"},
                    ),
                    "runtime.restart",
                )
                restart_duration = round(monotonic() - restart_started, 6)
                calls.append("B:runtime.restart")
                stale = _failure_value(
                    await session.call_tool(
                        "value.describe", {"proxy_id": onec_proxy_id}
                    ),
                    "value.describe",
                )
                calls.append("B:value.describe.stale")
                after = _value(
                    await session.call_tool(
                        "python.inspect", {"proxy_id": python_proxy_id}
                    ),
                    "python.inspect",
                )
                calls.append("B:python.inspect.after_restart")
                closed = _value(
                    await session.call_tool(
                        "runtime.close", {"policy": "abort_generation"}
                    ),
                    "runtime.close",
                )
                calls.append("B:runtime.close")
                return {
                    "frontend": frontend,
                    "status": status,
                    "variables": variables,
                    "before": before,
                    "ensured": ensured,
                    "restarted": restarted,
                    "stale": stale,
                    "after": after,
                    "closed": closed,
                    "restart_duration_s": restart_duration,
                }


def test_process_identity_access_denied_is_unverifiable(monkeypatch: pytest.MonkeyPatch) -> None:
    owned_executable = str(Path("C:/owned/python.exe").resolve())
    identity = _ProcessIdentity("service", 123, 1.0, owned_executable)

    def denied(_pid: int) -> psutil.Process:
        raise psutil.AccessDenied(pid=123)

    monkeypatch.setattr(psutil, "Process", denied)

    assert _identity_alive(identity) is None
    with pytest.raises(AssertionError, match="unable to verify"):
        _terminate_exact(identity)


def test_mcp_failure_preserves_only_allowlisted_boundary_facts() -> None:
    result = SimpleNamespace(
        structured_content={
            "ok": False,
            "failure": {
                "category": "platform_failure",
                "state_changed": "unknown",
                "safe_to_retry": "after_status_check",
                "current_state": {
                    "control_transport": "timeout",
                    "configured_timeout_s": 10.0,
                },
                "diagnostic_id": "must-not-be-copied",
            },
        }
    )

    with pytest.raises(_McpEnvelopeFailure) as caught:
        _value(result, "runtime.ensure")

    assert caught.value.method == "runtime.ensure"
    assert caught.value.category == "platform_failure"
    assert caught.value.state_changed == "unknown"
    assert caught.value.safe_to_retry == "after_status_check"
    assert caught.value.local_timeout_origin is True
    assert caught.value.configured_timeout_s == 10.0
    assert "must-not-be-copied" not in vars(caught.value).values()


def test_sampler_failure_is_reported_during_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = _OwnedProcessTracker(tmp_path / "private.json")

    def fail_sample() -> None:
        raise OSError("transient persistence failure")

    monkeypatch.setattr(tracker, "_sample_once", fail_sample)
    sleep(0.05)

    assert "OSError" in tracker.stop()


def test_tracker_never_follows_children_of_a_reused_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        def __init__(
            self,
            pid: int,
            created: float,
            executable: str,
            *,
            name: str,
            children: list["FakeProcess"] | None = None,
        ) -> None:
            self.pid = pid
            self._created = created
            self._executable = executable
            self._name = name
            self._children = children or []

        def create_time(self) -> float:
            return self._created

        def exe(self) -> str:
            return self._executable

        def name(self) -> str:
            return self._name

        def children(self, *, recursive: bool) -> list["FakeProcess"]:
            assert recursive is True
            return self._children

    tracker = _OwnedProcessTracker(tmp_path / "private.json")
    tracker._stop.set()
    tracker._thread.join(timeout=2)
    root_executable = str(Path("C:/owned/python.exe").resolve())
    unrelated_child = FakeProcess(
        201,
        3.0,
        str(Path("C:/unrelated/1cv8c.exe").resolve()),
        name="1cv8c.exe",
    )
    original_root = FakeProcess(200, 1.0, root_executable, name="python.exe")
    reused_root = FakeProcess(
        200,
        2.0,
        str(Path("C:/unrelated/python.exe").resolve()),
        name="python.exe",
        children=[unrelated_child],
    )
    observed_roots = iter((original_root, reused_root))
    monkeypatch.setattr(psutil, "Process", lambda _pid: next(observed_roots))

    tracker.add_root(200, "service")
    tracker._sample_once()

    assert [item.role for item in tracker.identities()] == ["service"]
    assert "RootIdentityMismatch" in tracker.stop()


def test_terminate_uses_the_process_object_that_was_identity_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owned_executable = str(Path("C:/owned/python.exe").resolve())
    identity = _ProcessIdentity("service", 123, 1.0, owned_executable)

    class FakeProcess:
        def __init__(self, created: float, executable: str) -> None:
            self.created = created
            self.executable = executable
            self.terminated = False

        def create_time(self) -> float:
            return self.created

        def exe(self) -> str:
            return self.executable

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout: float) -> None:
            assert timeout == 5

    owned = FakeProcess(1.0, owned_executable)
    reused = FakeProcess(2.0, "C:/other/python.exe")
    processes = iter((owned, reused))
    monkeypatch.setattr(psutil, "Process", lambda _pid: next(processes))

    _terminate_exact(identity)

    assert owned.terminated is True
    assert reused.terminated is False


def test_control_file_restore_refuses_concurrent_publication(tmp_path: Path) -> None:
    descriptor = tmp_path / "control" / "endpoint.json"
    descriptor.parent.mkdir()
    token = descriptor.with_name("token")
    descriptor.write_bytes(b"previous descriptor")
    token.write_bytes(b"previous token")
    previous = _snapshot_control_files(descriptor)
    descriptor.write_bytes(b"owned descriptor")
    token.write_bytes(b"owned token")
    owned = _snapshot_control_files(descriptor)
    descriptor.write_bytes(b"concurrent descriptor")

    with pytest.raises(AssertionError, match="changed"):
        _restore_control_files_if_owned(descriptor, owned=owned, previous=previous)

    assert descriptor.read_bytes() == b"concurrent descriptor"
    assert token.read_bytes() == b"owned token"


def test_control_file_restore_restores_exact_stale_snapshot(tmp_path: Path) -> None:
    descriptor = tmp_path / "control" / "endpoint.json"
    descriptor.parent.mkdir()
    token = descriptor.with_name("token")
    descriptor.write_bytes(b"previous descriptor")
    token.write_bytes(b"previous token")
    previous = _snapshot_control_files(descriptor)
    descriptor.write_bytes(b"owned descriptor")
    token.write_bytes(b"owned token")
    owned = _snapshot_control_files(descriptor)

    _restore_control_files_if_owned(descriptor, owned=owned, previous=previous)

    assert descriptor.read_bytes() == b"previous descriptor"
    assert token.read_bytes() == b"previous token"


@pytest.mark.integration
def test_mcp_main_survives_frontend_reconnect_and_closes_explicitly() -> None:
    if os.environ.get("ONEC_RUNTIME_RUN_LIVE_MCP_ZUP") != "1":
        pytest.skip("set ONEC_RUNTIME_RUN_LIVE_MCP_ZUP=1 for the one bounded live attempt")
    required = {
        name: os.environ.get(name, "").strip()
        for name in (
            "ONEC_RUNTIME_PLATFORM_BIN",
            "ONEC_RUNTIME_INFOBASE",
            "ONEC_RUNTIME_USERNAME",
            "ONEC_ZUP_SOURCE_ROOT",
        )
    }
    if not all(required.values()):
        pytest.skip("configured live ZUP environment is incomplete")
    assert not os.environ.get("ONEC_RUNTIME_PASSWORD")
    platform_bin = Path(required["ONEC_RUNTIME_PLATFORM_BIN"]).resolve(strict=True)
    infobase = Path(required["ONEC_RUNTIME_INFOBASE"]).resolve(strict=True)
    assert platform_bin.parent.name == "8.3.27.2170"
    assert (infobase / "1Cv8.1CD").is_file()
    assert Path(required["ONEC_ZUP_SOURCE_ROOT"]).resolve(strict=True).is_dir()
    assert not _PUBLIC_EVIDENCE.exists(), "attempt-4 public evidence already exists; retry is forbidden"
    assert not _PRIVATE_EVIDENCE.exists(), "attempt-4 private evidence already exists; retry is forbidden"

    notebook = nbformat.read(_NOTEBOOK, as_version=4)
    cell = next(item for item in notebook.cells if item.id == _CELL_ID)
    source = str(cell.source)
    assert list(EXPECTED_MESSAGES) == [
        line.split('"')[1] for line in source.splitlines() if line.startswith("Сообщить")
    ]
    source_hash = sha256(source.encode("utf-8")).hexdigest()
    assert source_hash == cell.metadata["onec_runtime"]["source_sha256"]

    env = os.environ | {
        "PYTHONPATH": str(_WORKSPACE / "src"),
        "ONEC_RUNTIME_EVIDENCE_DIR": str(
            _WORKSPACE / ".runtime" / "evidence" / "mcp-zup-main-attempt-4"
        ),
    }
    descriptor = (
        _WORKSPACE
        / ".runtime"
        / "agent-service"
        / "live-acceptance-private"
        / "attempt-4-control"
        / "endpoint.json"
    )
    previous_control = _snapshot_control_files(descriptor)
    assert previous_control == _ControlFiles(None, None), (
        "private attempt control files already exist; retry is forbidden"
    )
    owned_control: _ControlFiles | None = None
    owned_endpoint: ControlEndpoint | None = None
    cleanup_clients: tuple[ServiceClient, ...] = ()
    shutdown_client: ServiceClient | None = None
    service_process: subprocess.Popen[bytes] | None = None
    tracker = _OwnedProcessTracker(_PRIVATE_EVIDENCE)
    calls: list[str] = []
    timings: list[dict[str, object]] = []
    cleanup_errors: list[str] = []
    first: dict[str, object] | None = None
    second: dict[str, object] | None = None
    failure: BaseException | None = None
    failure_boundary = "preflight"
    started_total = monotonic()
    try:
        phase_started = monotonic()
        service_process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "onec_runtime_mcp.agent.service_entrypoint",
                "--workspace",
                str(_WORKSPACE),
                "--maximum-mode",
                "experiment",
                "--service-descriptor",
                str(descriptor),
            ],
            cwd=_WORKSPACE,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        tracker.add_root(service_process.pid, "service")
        deadline = monotonic() + 10
        while monotonic() < deadline:
            if service_process.poll() is not None:
                raise AssertionError("agent runtime service exited before publishing its descriptor")
            if descriptor.is_file() and descriptor.read_bytes() != previous_control.descriptor:
                break
            sleep(0.05)
        if not descriptor.is_file() or descriptor.read_bytes() == previous_control.descriptor:
            raise AssertionError("agent runtime service descriptor was not published")
        owned_endpoint = ControlEndpoint.read(descriptor)
        owned_control = _snapshot_control_files(descriptor)
        if owned_control.token is None:
            raise AssertionError("agent runtime service token was not published")
        cleanup_clients = tuple(
            ServiceClient(owned_endpoint, caller_id=caller, timeout_s=2)
            for caller in ("mcp-live-b", "mcp-live-a")
        )
        shutdown_client = ServiceClient(owned_endpoint, caller_id="cleanup", timeout_s=2)
        timings.append(_phase("service_start", phase_started))

        common = {
            "ONEC_RUNTIME_SERVICE_DESCRIPTOR": str(descriptor),
            "ONEC_RUNTIME_CONTROL_TIMEOUT_S": str(_CONTROL_TIMEOUT_S),
            "PYTHONPATH": str(_WORKSPACE / "src"),
        }
        first_parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "onec_runtime_mcp.mcp_entrypoint", "--workspace", str(_WORKSPACE)],
            cwd=_WORKSPACE,
            env=common | {"ONEC_RUNTIME_CALLER_ID": "mcp-live-a"},
        )
        second_parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "onec_runtime_mcp.mcp_entrypoint", "--workspace", str(_WORKSPACE)],
            cwd=_WORKSPACE,
            env=common | {"ONEC_RUNTIME_CALLER_ID": "mcp-live-b"},
        )

        failure_boundary = "frontend_a_runtime_ensure"
        first = asyncio.run(_run_frontend_a(first_parameters, tracker, calls))
        timings.append({"phase": "runtime_ensure", "duration_s": first["ensure_duration_s"]})
        timings.append({"phase": "code_run", "duration_s": first["run_duration_s"]})
        calls.append("A:frontend.exit")
        first_frontend = first["frontend"]
        assert isinstance(first_frontend, _ProcessIdentity)
        assert _identity_alive(first_frontend) is False
        assert service_process.poll() is None
        identities_after_a = tracker.identities()
        assert any(item.role == "onec" and _identity_alive(item) is True for item in identities_after_a)
        assert any(item.role == "dbgs" and _identity_alive(item) is True for item in identities_after_a)

        failure_boundary = "frontend_b_reconnect"
        operation = first["operation"]
        assert isinstance(operation, dict) and isinstance(operation.get("operation_id"), str)
        second = asyncio.run(
            _run_frontend_b(
                second_parameters,
                tracker,
                calls,
                operation["operation_id"],
            )
        )
        timings.append({"phase": "frontend_reconnect", "duration_s": second["reconnect_duration_s"]})

        runtime_a = first["runtime"]
        runtime_b = second["runtime"]
        ensured_b = second["ensured"]
        terminal = second["terminal"]
        output = second["output"]
        history = second["history"]
        result_payload = second["result"]
        assert all(isinstance(value, dict) for value in (runtime_a, runtime_b, ensured_b, terminal, output, history, result_payload))
        assert runtime_a["runtime_id"] == ensured_b["runtime_id"] == runtime_b["runtime_id"]
        assert runtime_a["generation"] == ensured_b["generation"] == runtime_b["generation"]
        assert terminal["state"] == "completed"
        assert terminal["result_present"] is True
        assert terminal["result_access"] == "unavailable_until_value_proxies"
        assert output["messages"] == list(EXPECTED_MESSAGES)
        assert result_payload["ok"] is False
        assert result_payload["failure"]["category"] == "unsupported"
        assert not _contains_key(result_payload, "result")
        history_operations = history["operations"]
        matching_history = [
            item for item in history_operations if item["operation_id"] == operation["operation_id"]
        ]
        assert len(matching_history) == 1
        revision = first["revision"]
        listed = first["listed"]
        assert isinstance(revision, dict) and isinstance(listed, list)
        listed_cell = next(item for item in listed if item["cell_id"] == _CELL_ID)
        assert revision["source"] == source
        assert revision["source_sha256"] == source_hash == listed_cell["source_sha256"]
        assert revision["revision"] == listed_cell["revision"] == 1
        timings.append({"phase": "operation_recovery", "duration_s": second["recovery_duration_s"]})
        timings.append({"phase": "runtime_close", "duration_s": second["close_duration_s"]})

        failure_boundary = "service_shutdown"
        if shutdown_client is None:
            raise AssertionError("owned service shutdown client was not captured")
        shutdown_client.shutdown()
        calls.append("owner:service.shutdown")
        assert service_process.wait(timeout=10) == 0
    except BaseException as error:
        failure = error
    finally:
        cleanup_started = monotonic()
        if service_process is not None and service_process.poll() is None:
            for client in cleanup_clients:
                try:
                    closed = client.call("runtime.close", {"policy": "abort_generation"})
                    if closed.ok:
                        break
                except BaseException as error:
                    cleanup_errors.append(type(error).__name__)
            if shutdown_client is not None:
                try:
                    shutdown_client.shutdown()
                except BaseException as error:
                    cleanup_errors.append(type(error).__name__)
        if service_process is not None and service_process.poll() is None:
            try:
                service_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        cleanup_errors.extend(tracker.stop())
        identities = tracker.identities()
        for identity in reversed(identities):
            try:
                _terminate_exact(identity)
            except BaseException as error:
                cleanup_errors.append(type(error).__name__)
        if owned_control is not None:
            try:
                _restore_control_files_if_owned(
                    descriptor,
                    owned=owned_control,
                    previous=previous_control,
                )
            except BaseException as error:
                cleanup_errors.append(type(error).__name__)
        timings.append(_phase("owned_cleanup", cleanup_started))
        absent = {item: _identity_alive(item) is False for item in identities}
        _PRIVATE_EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
        _PRIVATE_EVIDENCE.write_text(
            json.dumps(
                {
                    "attempt": _ATTEMPT,
                    "failure_boundary": failure_boundary,
                    "duration_s": round(monotonic() - started_total, 6),
                    "owned": [item.private_wire() | {"absent_after_cleanup": absent[item]} for item in identities],
                    "cleanup_errors": cleanup_errors,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    if failure is not None:
        identities = tracker.identities()
        fail_environment = {
            "schema": "onec-mcp-zup-main-environment-v1",
            "attempt": _ATTEMPT,
            "platform": {
                "version": "8.3.27.2170",
                "profile": "zup",
                "bin_identity_sha256": evidence_identity(_SALT, str(platform_bin).casefold()),
                "executables_sha256": {
                    "1cv8c.exe": _sha_file(platform_bin / "1cv8c.exe"),
                    "dbgs.exe": _sha_file(platform_bin / "dbgs.exe"),
                },
            },
            "database": {
                "target_identity_sha256": evidence_identity(_SALT, str(infobase).casefold()),
                "mutation_performed": False,
            },
        }
        if (
            isinstance(failure, _McpEnvelopeFailure)
            and failure.method == "runtime.ensure"
            and failure.category == "platform_failure"
            and failure.state_changed == "unknown"
            and failure.safe_to_retry == "after_status_check"
        ):
            fail_observations = build_mcp_zup_fail_observations(
                attempt=_ATTEMPT,
                completed_call_sequence=calls,
                attempt_duration_s=monotonic() - started_total,
                configured_control_timeout_s=_CONTROL_TIMEOUT_S,
                static_check_passed=_target_extension_static_check_passed(),
                source_sha256=source_hash,
                artifact_sha256=_sha_file(_NOTEBOOK),
                failure_category=str(failure.category),
                failure_state_changed=str(failure.state_changed),
                failure_safe_to_retry=str(failure.safe_to_retry),
                runtime_ensure_duration_s=failure.runtime_ensure_duration_s,
                local_timeout_origin_observed=failure.local_timeout_origin,
            )
        elif (
            first is not None
            and second is not None
            and isinstance(second.get("terminal"), dict)
            and second["terminal"].get("state") == "unknown"
            and second["terminal"].get("result_present") is False
            and isinstance(second.get("output"), dict)
            and second["output"].get("messages") == []
        ):
            fail_observations = build_mcp_zup_terminal_fail_observations(
                attempt=_ATTEMPT,
                completed_call_sequence=calls,
                configured_control_timeout_s=_CONTROL_TIMEOUT_S,
                source_sha256=source_hash,
                artifact_sha256=_sha_file(_NOTEBOOK),
                terminal_state="unknown",
                result_present=False,
                messages=(),
                timings=tuple(
                    (str(item["phase"]), float(item["duration_s"]))
                    for item in timings
                ),
            )
        else:
            fail_observations = {
                "schema": "onec-mcp-zup-main-observations-v1",
                "attempt": _ATTEMPT,
                "result": "FAIL",
                "failure_boundary": failure_boundary,
                "failure_type": type(failure).__name__,
                "completed_call_sequence": calls,
                "timings": timings,
                "negative_assertions": {
                    name: True
                    for name in (
                        "no_infobase_path",
                        "no_pid",
                        "no_process_command",
                        "no_raw_platform_uuid",
                        "no_raw_rdbg",
                        "no_saved_source",
                        "no_token",
                        "no_username",
                    )
                },
            }
        fail_cleanup = {
            "schema": "onec-mcp-zup-main-cleanup-v1",
            "attempt": _ATTEMPT,
            "all_absent": all(_identity_alive(item) is False for item in identities),
            "owned_cleanup_error_count": len(cleanup_errors),
            "identities": [
                {
                    "role": item.role,
                    "identity_sha256": evidence_identity(
                        _SALT,
                        item.role,
                        f"{item.pid}|{item.create_time:.6f}|{item.executable.casefold()}",
                    ),
                    "absent_after_cleanup": _identity_alive(item) is False,
                }
                for item in identities
            ],
        }
        write_mcp_zup_evidence(
            _PUBLIC_EVIDENCE,
            environment=fail_environment,
            observations=fail_observations,
            cleanup=fail_cleanup,
        )
        verify_mcp_zup_evidence(
            _PUBLIC_EVIDENCE,
            expected_result=ExpectedLiveResult.FAIL,
        )
        raise failure

    assert first is not None and second is not None
    identities = tracker.identities()
    roles = ("service", "mcp_a", "mcp_b", "designer", "dbgs", "onec")
    role_hashes = {role: _role_hash(identities, role) for role in roles}
    assert not cleanup_errors
    assert all(_identity_alive(item) is False for item in identities)
    assert calls == list(EXPECTED_CALL_SEQUENCE)
    operation = first["operation"]
    terminal = second["terminal"]
    before_wait = second["before_wait"]
    waited = second["waited"]
    runtime_a = first["runtime"]
    runtime_b = second["runtime"]
    revision = first["revision"]
    result_payload = second["result"]
    history_operation = next(
        item for item in second["history"]["operations"]
        if item["operation_id"] == operation["operation_id"]
    )
    environment_public = {
        "schema": "onec-mcp-zup-main-environment-v1",
        "attempt": _ATTEMPT,
        "platform": {
            "version": "8.3.27.2170",
            "profile": "zup",
            "bin_identity_sha256": evidence_identity(_SALT, str(platform_bin).casefold()),
            "executables_sha256": {
                "1cv8c.exe": _sha_file(platform_bin / "1cv8c.exe"),
                "dbgs.exe": _sha_file(platform_bin / "dbgs.exe"),
            },
        },
        "database": {
            "target_identity_sha256": evidence_identity(_SALT, str(infobase).casefold()),
            "mutation_performed": False,
        },
    }
    observations_public = {
        "schema": "onec-mcp-zup-main-observations-v1",
        "attempt": _ATTEMPT,
        "result": "PASS",
        "service": {
            "maximum_mode": "experiment",
            "service_identity_sha256": role_hashes["service"],
            "frontend_a_identity_sha256": role_hashes["mcp_a"],
            "frontend_b_identity_sha256": role_hashes["mcp_b"],
            "separate_processes": True,
            "service_alive_after_a_exit": True,
            "onec_alive_after_a_exit": True,
            "control_timeout_s": _CONTROL_TIMEOUT_S,
            "local_timeout_origin_observed": False,
        },
        "code": {
            "cell_id": _CELL_ID,
            "language": "bsl",
            "mode": "main",
            "revision": revision["revision"],
            "source_sha256": revision["source_sha256"],
            "document_sha256": revision["document_sha256"],
            "artifact_sha256": _sha_file(_NOTEBOOK),
        },
        "runtime": {
            "a_identity_sha256": evidence_identity(_SALT, runtime_a["runtime_id"]),
            "b_identity_sha256": evidence_identity(_SALT, runtime_b["runtime_id"]),
            "a_generation": runtime_a["generation"],
            "b_generation": runtime_b["generation"],
        },
        "operation": {
            "identity_sha256": evidence_identity(_SALT, operation["operation_id"]),
            "history_identity_sha256": evidence_identity(_SALT, history_operation["operation_id"]),
            "call_sequence": calls,
            "observed_states": [
                operation["state"],
                before_wait["state"],
                waited["state"],
                terminal["state"],
            ],
            "terminal_state": terminal["state"],
            "messages": second["output"]["messages"],
            "result_present": terminal["result_present"],
            "result_access": terminal["result_access"],
            "result_call_ok": result_payload["ok"],
            "result_failure_category": result_payload["failure"]["category"],
            "no_raw_result": True,
        },
        "timings": timings,
        "negative_assertions": {
            "no_infobase_path": True,
            "no_pid": True,
            "no_process_command": True,
            "no_raw_platform_uuid": True,
            "no_raw_rdbg": True,
            "no_saved_source": True,
            "no_token": True,
            "no_username": True,
        },
    }
    cleanup_public = {
        "schema": "onec-mcp-zup-main-cleanup-v1",
        "attempt": _ATTEMPT,
        "all_absent": True,
        "owned_cleanup_error_count": 0,
        "identities": [
            {"role": role, "identity_sha256": role_hashes[role], "absent_after_cleanup": True}
            for role in roles
        ],
    }
    write_mcp_zup_evidence(
        _PUBLIC_EVIDENCE,
        environment=environment_public,
        observations=observations_public,
        cleanup=cleanup_public,
    )
    verified = verify_mcp_zup_evidence(
        _PUBLIC_EVIDENCE,
        expected_result=ExpectedLiveResult.PASS,
    )
    assert verified["status"] == "PASS"


@pytest.mark.integration
def test_mcp_value_python_workspace_survives_frontend_and_runtime_restart() -> None:
    if os.environ.get("ONEC_RUNTIME_RUN_LIVE_VALUE_PYTHON") != "1":
        pytest.skip("set ONEC_RUNTIME_RUN_LIVE_VALUE_PYTHON=1 for the one bounded live attempt")
    required = {
        name: os.environ.get(name, "").strip()
        for name in (
            "ONEC_RUNTIME_PLATFORM_BIN",
            "ONEC_RUNTIME_INFOBASE",
            "ONEC_RUNTIME_USERNAME",
            "ONEC_ZUP_SOURCE_ROOT",
        )
    }
    if not all(required.values()):
        pytest.skip("configured live ZUP environment is incomplete")
    assert not os.environ.get("ONEC_RUNTIME_PASSWORD")
    platform_bin = Path(required["ONEC_RUNTIME_PLATFORM_BIN"]).resolve(strict=True)
    infobase = Path(required["ONEC_RUNTIME_INFOBASE"]).resolve(strict=True)
    assert platform_bin.parent.name == "8.3.27.2170"
    assert (infobase / "1Cv8.1CD").is_file()
    assert Path(required["ONEC_ZUP_SOURCE_ROOT"]).resolve(strict=True).is_dir()
    assert not _VALUE_PYTHON_PUBLIC_EVIDENCE.exists(), "attempt-1 evidence exists; retry is forbidden"
    assert not _VALUE_PYTHON_PRIVATE_EVIDENCE.exists(), "attempt-1 private evidence exists; retry is forbidden"

    notebook = nbformat.read(_VALUE_PYTHON_NOTEBOOK, as_version=4)
    cell = next(item for item in notebook.cells if item.id == _VALUE_PYTHON_CELL_ID)
    source = str(cell.source)
    source_hash = sha256(source.encode("utf-8")).hexdigest()
    assert source_hash == cell.metadata["onec_runtime"]["source_sha256"]

    env = os.environ | {
        "PYTHONPATH": str(_WORKSPACE / "src"),
        "ONEC_RUNTIME_EVIDENCE_DIR": str(
            _WORKSPACE / ".runtime" / "evidence" / "value-python-attempt-1"
        ),
    }
    descriptor = (
        _WORKSPACE
        / ".runtime"
        / "agent-service"
        / "value-python-private"
        / "attempt-1-control"
        / "endpoint.json"
    )
    previous_control = _snapshot_control_files(descriptor)
    assert previous_control == _ControlFiles(None, None), "attempt control files exist; retry is forbidden"
    owned_control: _ControlFiles | None = None
    service_process: subprocess.Popen[bytes] | None = None
    cleanup_clients: tuple[ServiceClient, ...] = ()
    shutdown_client: ServiceClient | None = None
    tracker = _OwnedProcessTracker(_VALUE_PYTHON_PRIVATE_EVIDENCE, attempt=1)
    calls: list[str] = []
    cleanup_errors: list[str] = []
    first: dict[str, object] | None = None
    second: dict[str, object] | None = None
    failure: BaseException | None = None
    started_total = monotonic()
    try:
        service_process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "onec_runtime_mcp.agent.service_entrypoint",
                "--workspace",
                str(_WORKSPACE),
                "--maximum-mode",
                "experiment",
                "--service-descriptor",
                str(descriptor),
            ],
            cwd=_WORKSPACE,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        tracker.add_root(service_process.pid, "service")
        deadline = monotonic() + 10
        while monotonic() < deadline:
            if service_process.poll() is not None:
                raise AssertionError("agent service exited before descriptor publication")
            if descriptor.is_file():
                break
            sleep(0.05)
        if not descriptor.is_file():
            raise AssertionError("agent service descriptor was not published")
        endpoint = ControlEndpoint.read(descriptor)
        owned_control = _snapshot_control_files(descriptor)
        if owned_control.token is None:
            raise AssertionError("agent service token was not published")
        cleanup_clients = tuple(
            ServiceClient(endpoint, caller_id=caller, timeout_s=180)
            for caller in ("mcp-value-python-b", "mcp-value-python-a")
        )
        shutdown_client = ServiceClient(endpoint, caller_id="cleanup", timeout_s=10)
        common = {
            "ONEC_RUNTIME_SERVICE_DESCRIPTOR": str(descriptor),
            "ONEC_RUNTIME_CONTROL_TIMEOUT_S": "180",
            "PYTHONPATH": str(_WORKSPACE / "src"),
        }
        first_parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "onec_runtime_mcp.mcp_entrypoint", "--workspace", str(_WORKSPACE)],
            cwd=_WORKSPACE,
            env=common | {"ONEC_RUNTIME_CALLER_ID": "mcp-value-python-a"},
        )
        second_parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "onec_runtime_mcp.mcp_entrypoint", "--workspace", str(_WORKSPACE)],
            cwd=_WORKSPACE,
            env=common | {"ONEC_RUNTIME_CALLER_ID": "mcp-value-python-b"},
        )

        first = asyncio.run(
            _run_value_python_frontend_a(first_parameters, tracker, calls)
        )
        calls.append("A:frontend.exit")
        first_frontend = first["frontend"]
        assert isinstance(first_frontend, _ProcessIdentity)
        assert _identity_alive(first_frontend) is False
        assert service_process.poll() is None
        table = first["table"]
        summary = first["summary"]
        assert isinstance(table, dict) and isinstance(summary, dict)
        second = asyncio.run(
            _run_value_python_frontend_b(
                second_parameters,
                tracker,
                calls,
                onec_proxy_id=str(table["proxy_id"]),
                python_proxy_id=str(summary["proxy_id"]),
            )
        )
        if shutdown_client is None:
            raise AssertionError("shutdown client is unavailable")
        shutdown_client.shutdown()
        calls.append("owner:service.shutdown")
        assert service_process.wait(timeout=15) == 0
    except BaseException as error:
        failure = error
    finally:
        if service_process is not None and service_process.poll() is None:
            for client in cleanup_clients:
                try:
                    response = client.call(
                        "runtime.close", {"policy": "abort_generation"}
                    )
                    if response.ok:
                        break
                except BaseException as error:
                    cleanup_errors.append(type(error).__name__)
            if shutdown_client is not None:
                try:
                    shutdown_client.shutdown()
                except BaseException as error:
                    cleanup_errors.append(type(error).__name__)
        if service_process is not None and service_process.poll() is None:
            try:
                service_process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                pass
        cleanup_errors.extend(tracker.stop())
        identities = tracker.identities()
        for identity in reversed(identities):
            try:
                _terminate_exact(identity)
            except BaseException as error:
                cleanup_errors.append(type(error).__name__)
        if owned_control is not None:
            try:
                _restore_control_files_if_owned(
                    descriptor, owned=owned_control, previous=previous_control
                )
            except BaseException as error:
                cleanup_errors.append(type(error).__name__)
        absent = {item: _identity_alive(item) is False for item in identities}
        _VALUE_PYTHON_PRIVATE_EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
        _VALUE_PYTHON_PRIVATE_EVIDENCE.write_text(
            json.dumps(
                {
                    "attempt": 1,
                    "failure_type": None if failure is None else type(failure).__name__,
                    "owned": [
                        item.private_wire() | {"absent_after_cleanup": absent[item]}
                        for item in identities
                    ],
                    "cleanup_errors": cleanup_errors,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    identities = tracker.identities()
    observed_roles = {
        "mcp" if item.role in {"mcp_a", "mcp_b"} else item.role
        for item in identities
        if item.role in {"dbgs", "mcp_a", "mcp_b", "onec", "python", "service"}
    }
    cleanup_public = {
        "owned_process_count": sum(_identity_alive(item) is not False for item in identities),
        "cleanup_errors": cleanup_errors,
        "roles_observed": sorted(observed_roles),
    }
    environment_public = {
        "schema_version": 1,
        "attempt": 1,
        "platform_version": "8.3.27.2170",
        "notebook_source_sha256": source_hash,
    }
    if failure is None:
        assert first is not None and second is not None
        operation = first["operation"]
        variables = first["variables"]
        table_size = first["table_size"]
        frame = first["frame"]
        frame_inspection = first["frame_inspection"]
        summary = first["summary"]
        summary_inspection = first["summary_inspection"]
        assert all(
            isinstance(item, dict)
            for item in (
                operation,
                table_size,
                frame,
                frame_inspection,
                summary,
                summary_inspection,
            )
        )
        assert isinstance(variables, list)
        frame_shape = frame_inspection["shape"]
        summary_shape = summary_inspection["shape"]
        frame_parents = frame["provenance"]["parent_proxy_ids"]
        summary_parents = summary["provenance"]["parent_proxy_ids"]
        observations_public = {
            "calls": calls,
            "main_state": operation["state"],
            "bsl_variables": [item["qualified_name"] for item in variables],
            "table_size": {
                "accuracy": table_size["accuracy"],
                "cost": table_size["cost"],
                "rows": table_size["rows"],
            },
            "dataframe": {
                "rows": frame_shape[0],
                "columns": frame_shape[1],
                "refs": "both",
            },
            "python_summary": {
                "qualified_name": summary["qualified_name"],
                "rows": summary_shape[0],
                "columns": summary_shape[1],
            },
            "provenance_linked": (
                first["table"]["proxy_id"] in frame_parents
                and frame["proxy_id"] in summary_parents
            ),
            "frontend_reconnect": (
                second["before"]["proxy_id"] == summary["proxy_id"]
                and [item["qualified_name"] for item in second["variables"]]
                == ["python.ИтогиПоОрганизации"]
            ),
            "runtime_restarted": (
                first["runtime"]["runtime_id"] != second["restarted"]["runtime_id"]
            ),
            "onec_proxy_stale": second["stale"]["category"] == "stale",
            "python_proxy_valid": (
                second["after"]["proxy_id"] == summary["proxy_id"]
                and second["after"]["shape"] == summary_inspection["shape"]
            ),
            "timings_s": {
                "main": first["main_duration_s"],
                "to_df": first["to_df_duration_s"],
                "python": first["python_duration_s"],
                "restart": second["restart_duration_s"],
                "total": round(monotonic() - started_total, 6),
            },
        }
    else:
        observations_public = {
            "calls": calls,
            "failure_type": type(failure).__name__,
            "timings_s": {
                "main": 0,
                "to_df": 0,
                "python": 0,
                "restart": 0,
                "total": round(monotonic() - started_total, 6),
            },
        }
    write_value_python_evidence(
        _VALUE_PYTHON_PUBLIC_EVIDENCE,
        environment=environment_public,
        observations=observations_public,
        cleanup=cleanup_public,
    )
    verified = verify_value_python_evidence(
        _VALUE_PYTHON_PUBLIC_EVIDENCE,
        expected_result="PASS" if failure is None else "FAIL",
    )
    if failure is not None:
        raise failure
    assert verified["result"] == "PASS"
    assert calls == list(EXPECTED_VALUE_PYTHON_CALL_SEQUENCE)
