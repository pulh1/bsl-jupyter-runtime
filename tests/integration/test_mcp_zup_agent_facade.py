from __future__ import annotations

import asyncio
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from threading import Event, Thread
from time import monotonic, sleep
from typing import Any

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
import nbformat
import psutil
import pytest

from capture_evidence_support import find_mcp_process
from onec_runtime_mcp.agent.control_protocol import ControlEndpoint
from integration.evidence.facade_live_evidence import (
    AgentFacadeLiveEvidenceError,
    verify_agent_facade_live_evidence,
    write_agent_facade_live_evidence,
)
from integration.evidence.live_evidence import evidence_identity
from onec_runtime_mcp.agent.mcp_profiles import AGENT_TOOL_NAMES
from onec_runtime_mcp.agent.service_client import ServiceClient
from process_evidence_support import (
    ApprovedExecutablePolicy,
    OwnedProcessTracker,
    ProcessIdentity,
    ProcessState,
    process_identity_state,
    terminate_exact_process,
)


_WORKSPACE = Path(__file__).resolve().parents[2]
_NOTEBOOK = _WORKSPACE / "tests" / "fixtures" / "notebooks" / "mcp-zup-agent-facade-acceptance.ipynb"
_CONTAINER = "tests/fixtures/notebooks/mcp-zup-agent-facade-acceptance.ipynb"
_CELL_ID = "mcp-zup-agent-facade-acceptance"
_ATTEMPT = int(os.environ.get("ONEC_RUNTIME_AGENT_FACADE_ATTEMPT", "2"))
if _ATTEMPT < 2:
    raise ValueError("future Agent facade live attempts start at 2")
_CONTROL_TIMEOUT_S = 180.0
_PUBLIC_EVIDENCE = (
    _WORKSPACE
    / "docs"
    / "research"
    / "evidence"
    / "2026-08-19-agent-mcp-facade"
    / f"attempt-{_ATTEMPT}"
)
_PRIVATE_ROOT = (
    _WORKSPACE / ".runtime" / "agent-service" / "agent-facade-live-private"
)
_PRIVATE_EVIDENCE = _PRIVATE_ROOT / f"attempt-{_ATTEMPT}-processes.json"
_CONTROL_DESCRIPTOR = _PRIVATE_ROOT / f"attempt-{_ATTEMPT}-control" / "endpoint.json"
_SALT = f"onec-agent-mcp-facade-2026-08-19-attempt-{_ATTEMPT}"
_EXPECTED_ROLES = ("service", "mcp_a", "mcp_b", "designer", "dbgs", "onec")
_NEGATIVE_ASSERTIONS = {
    "no_database_path": True,
    "no_pid": True,
    "no_process_command": True,
    "no_raw_rdbg": True,
    "no_saved_source": True,
    "no_token": True,
    "no_username": True,
    "no_value_payload": True,
}
_PID_TEXT = re.compile(r"(?i)\b(?:pid|process[_ ]?id)\s*[:=]\s*\d+\b")


def test_mcp_wire_privacy_gate_rejects_nested_private_field() -> None:
    with pytest.raises(AssertionError, match="private MCP field"):
        _assert_mcp_private_free(
            {"ok": True, "value": {"nested": {"pid": 123}}},
            private_markers=(),
        )
    with pytest.raises(AssertionError, match="private MCP text marker"):
        _assert_mcp_private_free(
            {"error": "raw RDBG transport failure"}, private_markers=()
        )
    with pytest.raises(AssertionError, match="private MCP text marker"):
        _assert_mcp_private_free({"error": "PID=12345"}, private_markers=())


def test_implementation_drift_is_never_publishable() -> None:
    before = {"harness_sha256": "a" * 64}
    with pytest.raises(AssertionError, match="implementation changed"):
        _require_stable_implementation(before, {"harness_sha256": "b" * 64})


def test_inspection_facts_are_derived_from_actions_and_reject_raw_preview() -> None:
    clean = {
        "descriptor": {"bounded_preview": None},
        "known_size": None,
        "bounded_preview": None,
        "actions": [
            {"name": "size", "cost": "bounded_scan", "available": False},
            {"name": "preview", "cost": "bounded_scan", "available": False},
            {"name": "materialize", "cost": "full_scan", "available": False},
            {"name": "to_df", "cost": "full_scan", "available": False},
        ],
    }
    assert _inspection_facts(clean, calls=("B:value.inspect",)) == {
        "inspection_actions": clean["actions"],
        "materialization_calls": 0,
        "raw_value_present": False,
    }
    raw = dict(clean)
    raw["bounded_preview"] = {"type_name": "Число", "scalar": 42, "sample": []}
    with pytest.raises(AssertionError, match="raw value"):
        _inspection_facts(raw, calls=("B:value.inspect",))


def test_negative_assertions_are_derived_from_public_payload() -> None:
    clean = {"runtime": {"identity_sha256": "a" * 64}}
    assert _derive_negative_assertions(
        clean,
        database_path=r"C:\private\base",
        identities=(),
        process_commands=(r"C:\private\1cv8c.exe",),
        saved_source="Результат = 20260819;",
        token="private-token",
        username="Private User",
        value_payload="20260819",
    ) == _NEGATIVE_ASSERTIONS

    leaked = {"runtime": {"token": "private-token"}}
    assert _derive_negative_assertions(
        leaked,
        database_path=r"C:\private\base",
        identities=(),
        process_commands=(),
        saved_source="Результат = 20260819;",
        token="private-token",
        username="Private User",
        value_payload="20260819",
    )["no_token"] is False
    leaked_pid = {"runtime": {"pid": 42}}
    assert _derive_negative_assertions(
        leaked_pid,
        database_path=r"C:\private\base",
        identities=(),
        process_commands=(),
        saved_source="Результат = 20260819;",
        token="private-token",
        username="Private User",
        value_payload="20260819",
    )["no_pid"] is False


@dataclass(frozen=True, slots=True)
class _ControlFiles:
    descriptor: bytes
    token: bytes


def _value(
    result: Any,
    method: str,
    *,
    private_markers: tuple[str, ...] = (),
) -> dict[str, object] | list[object]:
    payload = result.structured_content
    _assert_mcp_private_free(payload, private_markers=private_markers)
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise AssertionError(f"{method} returned a failed MCP envelope")
    value = payload.get("value")
    if not isinstance(value, (dict, list)):
        raise AssertionError(f"{method} returned an invalid MCP value")
    return value


def _assert_mcp_private_free(
    value: object, *, private_markers: tuple[str, ...]
) -> None:
    forbidden = {
        "cmdline",
        "command_line",
        "create_time",
        "database_path",
        "infobase_path",
        "password",
        "pid",
        "process_id",
        "rdbg",
        "token",
        "username",
    }
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in forbidden:
                raise AssertionError(f"private MCP field: {key}")
            _assert_mcp_private_free(item, private_markers=private_markers)
    elif isinstance(value, list):
        for item in value:
            _assert_mcp_private_free(item, private_markers=private_markers)
    elif isinstance(value, str):
        folded = value.casefold()
        if "rdbg" in folded or _PID_TEXT.search(value) is not None:
            raise AssertionError("private MCP text marker")
        for marker in private_markers:
            if marker and marker.casefold() in folded:
                raise AssertionError("private MCP text marker")


def _derive_negative_assertions(
    value: object,
    *,
    database_path: str,
    identities: tuple[ProcessIdentity, ...],
    process_commands: tuple[str, ...],
    saved_source: str,
    token: str,
    username: str,
    value_payload: str,
) -> dict[str, bool]:
    serialized = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).casefold()
    integer_values: list[int] = []
    contains_pid_field = False

    def collect(item: object) -> None:
        nonlocal contains_pid_field
        if type(item) is int:
            integer_values.append(item)
        elif isinstance(item, dict):
            for key, nested in item.items():
                if str(key).casefold() in {"pid", "process_id"}:
                    contains_pid_field = True
                collect(nested)
        elif isinstance(item, list):
            for nested in item:
                collect(nested)

    collect(value)
    private_pids = {item.pid for item in identities}
    return {
        "no_database_path": not database_path
        or database_path.casefold() not in serialized,
        "no_pid": not contains_pid_field
        and not private_pids.intersection(integer_values)
        and _PID_TEXT.search(serialized) is None,
        "no_process_command": all(
            command.casefold() not in serialized for command in process_commands if command
        ),
        "no_raw_rdbg": "rdbg" not in serialized,
        "no_saved_source": not saved_source
        or saved_source.casefold() not in serialized,
        "no_token": not token or token.casefold() not in serialized,
        "no_username": not username or username.casefold() not in serialized,
        "no_value_payload": not value_payload
        or value_payload.casefold() not in serialized,
    }


def _inspection_facts(
    inspection: dict[str, object], *, calls: tuple[str, ...]
) -> dict[str, object]:
    descriptor = inspection.get("descriptor")
    if not isinstance(descriptor, dict):
        raise AssertionError("inspection descriptor is invalid")
    raw_value_present = any(
        item is not None
        for item in (
            inspection.get("bounded_preview"),
            descriptor.get("bounded_preview"),
        )
    )
    if raw_value_present:
        raise AssertionError("value.inspect returned a raw value preview")
    actions = inspection.get("actions")
    if not isinstance(actions, list):
        raise AssertionError("inspection actions are invalid")
    projected: list[dict[str, object]] = []
    for action in actions:
        if not isinstance(action, dict):
            raise AssertionError("inspection action is invalid")
        projected.append(
            {
                "name": action.get("name"),
                "cost": action.get("cost"),
                "available": action.get("available"),
            }
        )
    materialization_calls = sum(
        name in {"B:value.materialize", "B:value.to_df", "B:python.run"}
        for name in calls
    )
    if materialization_calls:
        raise AssertionError("proxy-only acceptance invoked materialization")
    return {
        "inspection_actions": projected,
        "materialization_calls": materialization_calls,
        "raw_value_present": raw_value_present,
    }


def _complete_view(value: object) -> bool:
    return isinstance(value, dict) and value.get("truncation") == {
        "messages": False,
        "changed_variables": False,
        "outputs": False,
    }


def _operation_id(view: dict[str, object]) -> str:
    operation = view.get("operation")
    if not isinstance(operation, dict) or not isinstance(operation.get("operation_id"), str):
        raise AssertionError("AgentOperationView has no operation identity")
    return operation["operation_id"]


async def _frontend_a(
    parameters: StdioServerParameters,
    tracker: OwnedProcessTracker,
    calls: list[str],
    private_markers: tuple[str, ...],
) -> dict[str, object]:
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr:
        async with stdio_client(parameters, errlog=stderr) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools = {item.name for item in (await session.list_tools()).tools}
                assert tools == AGENT_TOOL_NAMES
                frontend = find_mcp_process(
                    tracker,
                    "mcp_a",
                    required_cmdline=(
                        "-m", "onec_runtime_mcp.mcp_entrypoint", "--workspace",
                        str(_WORKSPACE), "--profile", "agent",
                    ),
                )
                _value(
                    await session.call_tool("workspace.open", {"project": str(_WORKSPACE)}),
                    "workspace.open",
                    private_markers=private_markers,
                )
                calls.append("A:workspace.open")
                ensured = _value(
                    await session.call_tool(
                        "runtime.ensure", {"profile": "zup", "mode": "experiment"}
                    ),
                    "runtime.ensure",
                    private_markers=private_markers,
                )
                calls.append("A:runtime.ensure")
                assert isinstance(ensured, dict)
                assert ensured.get("state") in {"queued", "running"}
                assert _complete_view(ensured)
                assert _operation_id(ensured)
                return {"frontend": frontend, "view": ensured}


async def _wait_view(
    session: ClientSession,
    view: dict[str, object],
    calls: list[str],
    call_name: str,
    private_markers: tuple[str, ...],
) -> tuple[dict[str, object], int]:
    deadline = monotonic() + _CONTROL_TIMEOUT_S
    current = view
    count = 0
    first_call = True
    while first_call or current.get("state") in {"queued", "running"}:
        first_call = False
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise AssertionError(f"{call_name} did not reach a terminal state")
        timeout_s = (
            min(30.0, remaining)
            if current.get("state") in {"queued", "running"}
            else 0.0
        )
        value = _value(
            await session.call_tool(
                "operation.wait",
                {
                    "operation_id": _operation_id(current),
                    "timeout_s": timeout_s,
                    "after_event_cursor": current["next_event_cursor"],
                    "after_message_cursor": current["next_message_cursor"],
                },
            ),
            "operation.wait",
            private_markers=private_markers,
        )
        count += 1
        assert isinstance(value, dict) and _complete_view(value)
        current = value
    calls.append(call_name)
    return current, count


async def _frontend_b(
    parameters: StdioServerParameters,
    tracker: OwnedProcessTracker,
    calls: list[str],
    startup_a: dict[str, object],
    private_markers: tuple[str, ...],
) -> dict[str, object]:
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr:
        async with stdio_client(parameters, errlog=stderr) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                frontend = find_mcp_process(
                    tracker,
                    "mcp_b",
                    required_cmdline=(
                        "-m", "onec_runtime_mcp.mcp_entrypoint", "--workspace",
                        str(_WORKSPACE), "--profile", "agent",
                    ),
                )
                _value(
                    await session.call_tool("workspace.open", {"project": str(_WORKSPACE)}),
                    "workspace.open",
                    private_markers=private_markers,
                )
                calls.append("B:workspace.open")
                joined = _value(
                    await session.call_tool(
                        "runtime.ensure", {"profile": "zup", "mode": "experiment"}
                    ),
                    "runtime.ensure",
                    private_markers=private_markers,
                )
                calls.append("B:runtime.ensure")
                assert isinstance(joined, dict) and "operation" in joined
                assert _operation_id(joined) == _operation_id(startup_a)
                assert _complete_view(joined)
                startup_terminal, startup_wait_count = await _wait_view(
                    session,
                    joined,
                    calls,
                    "B:operation.wait",
                    private_markers,
                )
                assert startup_terminal["state"] == "completed"
                ready = _value(
                    await session.call_tool(
                        "runtime.ensure", {"profile": "zup", "mode": "experiment"}
                    ),
                    "runtime.ensure",
                    private_markers=private_markers,
                )
                calls.append("B:runtime.ensure.ready")
                assert isinstance(ready, dict) and ready.get("state") == "idle"
                assert "operation" not in ready

                listed = _value(
                    await session.call_tool("code.list", {"container": _CONTAINER}),
                    "code.list",
                    private_markers=private_markers,
                )
                calls.append("B:code.list")
                revision = _value(
                    await session.call_tool("code.get", {"cell_id": _CELL_ID}),
                    "code.get",
                    private_markers=private_markers,
                )
                calls.append("B:code.get")
                assert isinstance(listed, list) and isinstance(revision, dict)
                run = _value(
                    await session.call_tool(
                        "code.run",
                        {
                            "cell_id": _CELL_ID,
                            "revision": revision["revision"],
                            "source_sha256": revision["source_sha256"],
                            "inputs": {},
                            "wait_s": 0,
                            "observe": {
                                "items": [
                                    {
                                        "alias": "scalar",
                                        "source": {
                                            "kind": "context_binding",
                                            "name": "bsl.АгентСкаляр",
                                        },
                                        "result": "proxy",
                                    }
                                ],
                                "budget_profile": "agent_metadata",
                            },
                        },
                    ),
                    "code.run",
                    private_markers=private_markers,
                )
                calls.append("B:code.run")
                assert isinstance(run, dict) and _complete_view(run)
                main_terminal, main_wait_count = await _wait_view(
                    session,
                    run,
                    calls,
                    "B:operation.wait.main",
                    private_markers,
                )
                assert main_terminal["state"] == "completed"
                outputs = main_terminal.get("outputs")
                assert isinstance(outputs, dict) and set(outputs) == {"scalar"}
                scalar = outputs["scalar"]
                assert isinstance(scalar, dict)
                assert scalar["realm"] == "onec"
                assert scalar["qualified_name"] == "bsl.АгентСкаляр"
                assert scalar["consistency"] == "exact"
                assert scalar["known_size"] is None
                assert scalar["bounded_preview"] is None
                inspection = _value(
                    await session.call_tool(
                        "value.inspect",
                        {
                            "proxy_id": scalar["proxy_id"],
                            "detail": "auto",
                            "budget_profile": "agent_metadata",
                        },
                    ),
                    "value.inspect",
                    private_markers=private_markers,
                )
                calls.append("B:value.inspect")
                assert isinstance(inspection, dict)
                assert inspection["descriptor"]["proxy_id"] == scalar["proxy_id"]
                assert inspection["known_size"] is None
                assert inspection["bounded_preview"] is None
                closed = _value(
                    await session.call_tool(
                        "runtime.close", {"policy": "abort_generation"}
                    ),
                    "runtime.close",
                    private_markers=private_markers,
                )
                calls.append("B:runtime.close")
                return {
                    "frontend": frontend,
                    "joined": joined,
                    "startup_terminal": startup_terminal,
                    "startup_wait_count": startup_wait_count,
                    "ready": ready,
                    "listed": listed,
                    "revision": revision,
                    "run": run,
                    "main_terminal": main_terminal,
                    "main_wait_count": main_wait_count,
                    "scalar": scalar,
                    "inspection": inspection,
                    "closed": closed,
                }


def _sha_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_tree_sha256(root: Path) -> str:
    digest = sha256()
    files = sorted(
        item for item in root.rglob("*") if item.is_file() and item.suffix == ".py"
    )
    if not files:
        raise AssertionError("runtime source tree is empty")
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        payload = path.read_bytes()
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _implementation_identity(
    *, notebook_artifact_sha256: str, notebook_cell_source_sha256: str
) -> dict[str, str]:
    return {
        "harness_sha256": _sha_file(Path(__file__)),
        "verifier_sha256": _sha_file(
            _WORKSPACE / "src" / "onec_runtime" / "agent" / "facade_live_evidence.py"
        ),
        "runtime_tree_sha256": _source_tree_sha256(
            _WORKSPACE / "src" / "onec_runtime"
        ),
        "notebook_artifact_sha256": notebook_artifact_sha256,
        "notebook_cell_source_sha256": notebook_cell_source_sha256,
    }


def _require_stable_implementation(
    before: dict[str, str], after: dict[str, str]
) -> None:
    if before != after:
        raise AssertionError("live implementation changed during the attempt")


def _role_identity(identities: tuple[ProcessIdentity, ...], role: str) -> str:
    selected = [item for item in identities if item.role == role]
    if len(selected) != 1:
        raise AssertionError(f"owned role {role} count is {len(selected)}, expected one")
    item = selected[0]
    return evidence_identity(
        _SALT,
        role,
        f"{item.pid}|{item.create_time:.6f}|{item.executable.casefold()}",
    )


def _operation_submission_count(operation_id: str) -> int:
    journal = _WORKSPACE / ".runtime" / "agent-service" / "operations.jsonl"
    count = 0
    for line in journal.read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        if (
            item.get("event") == "submitted"
            and item.get("operation_id") == operation_id
            and item.get("operation_kind") == "runtime_ensure"
        ):
            count += 1
    return count


def _assert_target_not_open(infobase: Path) -> None:
    target = str(infobase).casefold()
    for process in psutil.process_iter(("name", "cmdline")):
        try:
            if process.info["name"].casefold() in {"1cv8.exe", "1cv8c.exe"}:
                command = " ".join(process.info["cmdline"] or ()).casefold()
                if target in command:
                    raise AssertionError("dedicated target infobase already has a 1C process")
        except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
            continue


@pytest.mark.integration
def test_agent_facade_live_startup_reconnect_proxy_and_cleanup() -> None:
    if os.environ.get("ONEC_RUNTIME_RUN_LIVE_AGENT_FACADE") != "1":
        pytest.skip("set ONEC_RUNTIME_RUN_LIVE_AGENT_FACADE=1 for the one bounded attempt")
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
    assert not _PUBLIC_EVIDENCE.exists(), f"attempt-{_ATTEMPT} public evidence exists"
    assert not _PRIVATE_EVIDENCE.exists(), f"attempt-{_ATTEMPT} private evidence exists"
    assert not _CONTROL_DESCRIPTOR.exists(), f"attempt-{_ATTEMPT} control descriptor exists"
    assert not (_WORKSPACE / ".runtime" / "agent-service" / "runtime-owner.json").exists()
    _assert_target_not_open(infobase)

    notebook = nbformat.read(_NOTEBOOK, as_version=4)
    cell = next(item for item in notebook.cells if item.id == _CELL_ID)
    source = str(cell.source)
    source_hash = sha256(source.encode("utf-8")).hexdigest()
    assert source_hash == cell.metadata["onec_runtime"]["source_sha256"]

    env = os.environ | {
        "PYTHONPATH": str(_WORKSPACE / "src"),
        "ONEC_RUNTIME_EVIDENCE_DIR": str(
            _WORKSPACE
            / ".runtime"
            / "evidence"
            / f"agent-facade-live-attempt-{_ATTEMPT}"
        ),
    }
    tracker = OwnedProcessTracker(
        _PRIVATE_EVIDENCE,
        attempt=_ATTEMPT,
        policy=ApprovedExecutablePolicy(
            python_executable=str(Path(sys.executable).resolve(strict=True)),
            platform_bin=str(platform_bin),
        ),
    )
    service_process: subprocess.Popen[bytes] | None = None
    endpoint: ControlEndpoint | None = None
    owned_control: _ControlFiles | None = None
    cleanup_client: ServiceClient | None = None
    shutdown_client: ServiceClient | None = None
    calls: list[str] = []
    cleanup_errors: list[str] = []
    first: dict[str, object] | None = None
    second: dict[str, object] | None = None
    failure: BaseException | None = None
    failure_boundary = "preflight"
    runtime_closed = False
    runtime_alive_after_a = False
    token_text = ""
    implementation_before = _implementation_identity(
        notebook_artifact_sha256=_sha_file(_NOTEBOOK),
        notebook_cell_source_sha256=source_hash,
    )
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
                str(_CONTROL_DESCRIPTOR),
            ],
            cwd=_WORKSPACE,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        tracker.add_root(
            service_process.pid,
            "service",
            required_cmdline=(
                "-m",
                "onec_runtime_mcp.agent.service_entrypoint",
                "--workspace",
                str(_WORKSPACE),
                "--maximum-mode",
                "experiment",
                "--service-descriptor",
                str(_CONTROL_DESCRIPTOR),
            ),
        )
        deadline = monotonic() + 10
        while monotonic() < deadline and not _CONTROL_DESCRIPTOR.is_file():
            if service_process.poll() is not None:
                raise AssertionError("agent service exited before descriptor publication")
            sleep(0.05)
        endpoint = ControlEndpoint.read(_CONTROL_DESCRIPTOR)
        owned_control = _ControlFiles(
            _CONTROL_DESCRIPTOR.read_bytes(), endpoint.token_path.read_bytes()
        )
        token_payload = json.loads(endpoint.token_path.read_text(encoding="utf-8"))
        if (
            not isinstance(token_payload, dict)
            or set(token_payload) != {"token"}
            or not isinstance(token_payload["token"], str)
            or not token_payload["token"]
        ):
            raise AssertionError("owned control token is invalid")
        token_text = token_payload["token"]
        private_markers = (
            str(infobase),
            str(platform_bin),
            required["ONEC_RUNTIME_USERNAME"],
            token_text,
            str(service_process.pid),
        )
        cleanup_client = ServiceClient(endpoint, caller_id="mcp-agent-b", timeout_s=5)
        shutdown_client = ServiceClient(endpoint, caller_id="cleanup", timeout_s=5)
        common = {
            "ONEC_RUNTIME_SERVICE_DESCRIPTOR": str(_CONTROL_DESCRIPTOR),
            "ONEC_RUNTIME_CONTROL_TIMEOUT_S": str(_CONTROL_TIMEOUT_S),
            "PYTHONPATH": str(_WORKSPACE / "src"),
        }
        first_parameters = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "onec_runtime_mcp.mcp_entrypoint",
                "--workspace",
                str(_WORKSPACE),
                "--profile",
                "agent",
            ],
            cwd=_WORKSPACE,
            env=common | {"ONEC_RUNTIME_CALLER_ID": "mcp-agent-a"},
        )
        second_parameters = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "onec_runtime_mcp.mcp_entrypoint",
                "--workspace",
                str(_WORKSPACE),
                "--profile",
                "agent",
            ],
            cwd=_WORKSPACE,
            env=common | {"ONEC_RUNTIME_CALLER_ID": "mcp-agent-b"},
        )
        failure_boundary = "frontend_a_startup_publication"
        first = asyncio.run(
            _frontend_a(first_parameters, tracker, calls, private_markers)
        )
        calls.append("A:frontend.exit")
        frontend_a = first["frontend"]
        assert isinstance(frontend_a, ProcessIdentity)
        assert process_identity_state(frontend_a) is ProcessState.ABSENT
        assert service_process.poll() is None
        runtime_deadline = monotonic() + 5
        while monotonic() < runtime_deadline:
            runtime_alive_after_a = any(
                item.role in {"designer", "dbgs", "onec"}
                and process_identity_state(item) is ProcessState.ALIVE
                for item in tracker.identities()
            )
            if runtime_alive_after_a:
                break
            sleep(0.02)
        assert runtime_alive_after_a

        failure_boundary = "frontend_b_join_and_main"
        second = asyncio.run(
            _frontend_b(
                second_parameters,
                tracker,
                calls,
                first["view"],  # type: ignore[arg-type]
                private_markers,
            )
        )
        runtime_closed = True
        failure_boundary = "service_shutdown"
        shutdown_client.shutdown()
        calls.append("owner:service.shutdown")
        assert service_process.wait(timeout=10) == 0
    except BaseException as error:
        failure = error
    finally:
        if service_process is not None and service_process.poll() is None:
            if cleanup_client is not None:
                try:
                    response = cleanup_client.call(
                        "runtime.close", {"policy": "abort_generation"}
                    )
                    runtime_closed = runtime_closed or response.ok
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
                terminate_exact_process(identity)
            except BaseException as error:
                cleanup_errors.append(type(error).__name__)
        if owned_control is not None:
            try:
                current = _ControlFiles(
                    _CONTROL_DESCRIPTOR.read_bytes(), endpoint.token_path.read_bytes()
                )
                if current != owned_control:
                    raise AssertionError("owned control files changed during acceptance")
                _CONTROL_DESCRIPTOR.unlink()
                endpoint.token_path.unlink()
            except BaseException as error:
                cleanup_errors.append(type(error).__name__)
        absent = {
            item: process_identity_state(item) is ProcessState.ABSENT
            for item in identities
        }
        _PRIVATE_EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
        _PRIVATE_EVIDENCE.write_text(
            json.dumps(
                {
                    "attempt": _ATTEMPT,
                    "failure_boundary": failure_boundary,
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
    cleanup_public = {
        "schema": "onec-agent-facade-live-cleanup-v1",
        "attempt": _ATTEMPT,
        "runtime_closed_explicitly": runtime_closed if failure is None else False,
        "all_absent": all(
            process_identity_state(item) is ProcessState.ABSENT
            for item in identities
        ),
        "owned_cleanup_error_count": len(cleanup_errors),
        "identities": [
            {
                "role": item.role,
                "identity_sha256": evidence_identity(
                    _SALT,
                    item.role,
                    f"{item.pid}|{item.create_time:.6f}|{item.executable.casefold()}",
                ),
                "absent_after_cleanup": (
                    process_identity_state(item) is ProcessState.ABSENT
                ),
            }
            for item in identities
        ],
    }
    environment_public = {
        "schema": "onec-agent-facade-live-environment-v1",
        "attempt": _ATTEMPT,
        "profile": "agent",
        "maximum_mode": "experiment",
        "platform": {
            "version": "8.3.27.2170",
            "bin_identity_sha256": evidence_identity(_SALT, str(platform_bin).casefold()),
            "executables_sha256": {
                "1cv8c.exe": _sha_file(platform_bin / "1cv8c.exe"),
                "dbgs.exe": _sha_file(platform_bin / "dbgs.exe"),
            },
        },
        "database": {
            "target_identity_sha256": evidence_identity(_SALT, str(infobase).casefold()),
            "test_cell_requests_database_mutation": False,
        },
        "notebook": {
            "artifact_sha256": _sha_file(_NOTEBOOK),
            "cell_source_sha256": source_hash,
        },
        "implementation": implementation_before,
    }
    implementation_stable = True
    try:
        _require_stable_implementation(
            implementation_before,
            _implementation_identity(
                notebook_artifact_sha256=_sha_file(_NOTEBOOK),
                notebook_cell_source_sha256=source_hash,
            ),
        )
    except AssertionError as error:
        implementation_stable = False
        failure = error
        failure_boundary = "implementation_stability"
    if failure is not None:
        observations_public = {
            "schema": "onec-agent-facade-live-observations-v1",
            "attempt": _ATTEMPT,
            "result": "FAIL",
            "call_sequence": calls,
            "failure": {
                "boundary": failure_boundary,
                "type": type(failure).__name__,
            },
        }
        negative = _derive_negative_assertions(
            {
                "environment": environment_public,
                "observations": observations_public,
                "cleanup": cleanup_public,
            },
            database_path=str(infobase),
            identities=identities,
            process_commands=tuple(item.executable for item in identities),
            saved_source=source,
            token=token_text,
            username=required["ONEC_RUNTIME_USERNAME"],
            value_payload="20260819",
        )
        assert negative == _NEGATIVE_ASSERTIONS
        observations_public["negative_assertions"] = negative
        if implementation_stable and not cleanup_errors and identities:
            write_agent_facade_live_evidence(
                _PUBLIC_EVIDENCE,
                environment=environment_public,
                observations=observations_public,
                cleanup=cleanup_public,
            )
            verify_agent_facade_live_evidence(
                _PUBLIC_EVIDENCE,
                expected_result="FAIL",
                expected_implementation=environment_public["implementation"],
            )
        raise failure

    assert first is not None and second is not None
    assert not cleanup_errors
    assert all(
        process_identity_state(item) is ProcessState.ABSENT
        for item in identities
    )
    assert not (_WORKSPACE / ".runtime" / "agent-service" / "runtime-owner.json").exists()
    assert sorted(item.role for item in identities) == sorted(_EXPECTED_ROLES)
    startup_a = first["view"]
    startup_b = second["joined"]
    startup_terminal = second["startup_terminal"]
    ready = second["ready"]
    revision = second["revision"]
    main_terminal = second["main_terminal"]
    scalar = second["scalar"]
    assert all(
        isinstance(item, dict)
        for item in (startup_a, startup_b, startup_terminal, ready, revision, main_terminal, scalar)
    )
    startup_operation_id = _operation_id(startup_a)
    startup_submission_count = _operation_submission_count(startup_operation_id)
    assert startup_submission_count == 1
    assert revision["source_sha256"] == source_hash
    runtime_identity = evidence_identity(_SALT, ready["runtime_id"])
    observations_public = {
        "schema": "onec-agent-facade-live-observations-v1",
        "attempt": _ATTEMPT,
        "result": "PASS",
        "call_sequence": calls,
        "startup": {
            "operation_identity_sha256": evidence_identity(_SALT, startup_operation_id),
            "frontend_a_operation_identity_sha256": evidence_identity(
                _SALT, _operation_id(startup_a)
            ),
            "frontend_b_operation_identity_sha256": evidence_identity(
                _SALT, _operation_id(startup_b)
            ),
            "observed_states": [
                startup_a["state"],
                startup_b["state"],
                startup_terminal["state"],
            ],
            "published_before_ready": True,
            "frontend_a_exited_before_terminal": True,
            "startup_submission_count": startup_submission_count,
            "next_event_cursors": [
                startup_a["next_event_cursor"],
                startup_b["next_event_cursor"],
                startup_terminal["next_event_cursor"],
            ],
            "next_message_cursors": [
                startup_a["next_message_cursor"],
                startup_b["next_message_cursor"],
                startup_terminal["next_message_cursor"],
            ],
            "truncation_complete": [
                _complete_view(startup_a),
                _complete_view(startup_b),
                _complete_view(startup_terminal),
            ],
            "wait_call_count": second["startup_wait_count"],
        },
        "runtime": {
            "ready_identity_sha256": runtime_identity,
            "ready_generation": ready["generation"],
            "ready_state": ready["state"],
            "descriptor_returned": True,
        },
        "code": {
            "cell_id": _CELL_ID,
            "language": revision["language"],
            "mode": revision["mode"],
            "revision": revision["revision"],
            "source_sha256": revision["source_sha256"],
            "document_sha256": revision["document_sha256"],
            "operation_identity_sha256": evidence_identity(
                _SALT, _operation_id(main_terminal)
            ),
            "terminal_state": main_terminal["state"],
            "next_event_cursor": main_terminal["next_event_cursor"],
            "next_message_cursor": main_terminal["next_message_cursor"],
            "truncation_complete": _complete_view(main_terminal),
            "wait_call_count": second["main_wait_count"],
        },
        "observation": {
            "requested_alias": "scalar",
            "requested_binding": "bsl.АгентСкаляр",
            "requested_result": "proxy",
            "budget_profile": "agent_metadata",
            "output_count": len(main_terminal["outputs"]),
            "proxy_realm": scalar["realm"],
            "proxy_qualified_name": scalar["qualified_name"],
            "proxy_consistency": scalar["consistency"],
            "inspect_detail": "auto",
            **_inspection_facts(
                second["inspection"],  # type: ignore[arg-type]
                calls=tuple(calls),
            ),
        },
        "service": {
            "separate_frontends": _role_identity(identities, "mcp_a")
            != _role_identity(identities, "mcp_b"),
            "service_alive_after_a_exit": True,
            "runtime_alive_after_a_exit": runtime_alive_after_a,
        },
    }
    cleanup_public["identities"] = [
        {
            "role": role,
            "identity_sha256": _role_identity(identities, role),
            "absent_after_cleanup": True,
        }
        for role in _EXPECTED_ROLES
    ]
    negative = _derive_negative_assertions(
        {
            "environment": environment_public,
            "observations": observations_public,
            "cleanup": cleanup_public,
        },
        database_path=str(infobase),
        identities=identities,
        process_commands=tuple(item.executable for item in identities),
        saved_source=source,
        token=token_text,
        username=required["ONEC_RUNTIME_USERNAME"],
        value_payload="20260819",
    )
    assert negative == _NEGATIVE_ASSERTIONS
    observations_public["negative_assertions"] = negative
    write_agent_facade_live_evidence(
        _PUBLIC_EVIDENCE,
        environment=environment_public,
        observations=observations_public,
        cleanup=cleanup_public,
    )
    verified = verify_agent_facade_live_evidence(
        _PUBLIC_EVIDENCE,
        expected_result="PASS",
        expected_implementation=environment_public["implementation"],
    )
    assert verified["status"] == "PASS"
