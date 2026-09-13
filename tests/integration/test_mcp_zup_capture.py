from __future__ import annotations

import ast
import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from time import monotonic, sleep
from types import SimpleNamespace
from typing import Any

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
import nbformat
import psutil
import pytest
import capture_evidence_support as capture_support
import process_evidence_support as process_support

from capture_evidence_support import (
    file_sha256,
    find_mcp_process,
    mcp_operation_id,
    require_mcp_value,
    tree_sha256,
)
from integration.evidence.capture_live_evidence import (
    CAPTURE_OWNED_ROLES,
    CAPTURE_PASS_CALL_SEQUENCE,
    CaptureAttemptPaths,
    CaptureEvidenceError,
    ExpectedCaptureCleanupFacts,
    ExpectedCaptureResult,
    capture_attempt_paths,
    capture_identity_salt,
    load_capture_published_regression_expectation,
    process_identity_sha256,
    verify_capture_live_evidence,
    verify_capture_published_regression,
    write_capture_live_evidence,
)
from integration.evidence.capture_private_replay import (
    CapturePrivateJournal,
    CapturePrivateReplayError,
    create_capture_private_replay,
    load_capture_private_journal_prefix,
    load_capture_private_replay,
    publish_capture_private_replay,
    record_capture_private_raw_response,
)
from onec_runtime_mcp.agent.control_protocol import ControlEndpoint
from integration.evidence.live_evidence import evidence_identity
from onec_runtime_mcp.agent.mcp_profiles import CAPTURE_TOOL_NAMES
from onec_runtime_mcp.agent.service_client import ServiceClient
from integration.support.zup_capture_fixture import (
    CAPTURE_A_SNAPSHOT_SOURCE,
    PAYROLL_DISCOVERY_SOURCE,
    PAYROLL_MAIN_SOURCE,
)
from integration.support.zup_capture_fixture import locate_payroll_capture_points

from process_evidence_support import (
    ApprovedExecutablePolicy,
    OwnedProcessTracker,
    ProcessIdentity,
    ProcessState,
    identity_states,
    open_process_identity,
    process_identity_state,
    terminate_exact_process,
    terminate_owned_popen,
)


_WORKSPACE = Path(__file__).resolve().parents[2]
_NOTEBOOK = _WORKSPACE / "tests" / "fixtures" / "notebooks" / "mcp-zup-capture-acceptance.ipynb"
_CONTAINER = "tests/fixtures/notebooks/mcp-zup-capture-acceptance.ipynb"
_MAIN_CELL = "zup_capture_main"
_HYPOTHESIS_CELL = "zup_capture_hypothesis"
_LIVE_FLAG = "ONEC_RUNTIME_RUN_LIVE_CAPTURE"


@dataclass(frozen=True, slots=True)
class _CaptureAttemptPolicy:
    attempt: int
    overall_timeout_s: float
    sdk_outer_timeout_s: float
    control_timeout_s: float
    schema_wait_max_s: float


def _capture_attempt_policy(attempt: int) -> _CaptureAttemptPolicy:
    capture_identity_salt(attempt)  # validates the shared exact attempt domain
    if attempt == 1:
        return _CaptureAttemptPolicy(1, 900.0, 190.0, 180.0, 30.0)
    return _CaptureAttemptPolicy(2, 1800.0, 600.0, 180.0, 30.0)


_ATTEMPT = 2
_ATTEMPT_POLICY = _capture_attempt_policy(_ATTEMPT)
_CONTROL_TIMEOUT_S = _ATTEMPT_POLICY.control_timeout_s
_MCP_SCHEMA_WAIT_MAX_S = _ATTEMPT_POLICY.schema_wait_max_s
_ATTEMPT_TIMEOUT_S = _ATTEMPT_POLICY.overall_timeout_s
_SDK_CALL_TIMEOUT_S = _ATTEMPT_POLICY.sdk_outer_timeout_s
_SALT = capture_identity_salt(_ATTEMPT)
_CAPTURE_A_LOCAL_NAMES = ("МенеджерВременныхТаблиц", "ОписаниеОперации")
_DOWNSTREAM_RESULT_LOCAL = "ОписаниеОперации"
_TABLE_NAME = "ВТЗарплатаКВыплате"
_ACTIVE_ATTEMPT_DEADLINE: ContextVar[float | None] = ContextVar(
    "capture_attempt_deadline", default=None
)
_ACTIVE_TRACKER: ContextVar[OwnedProcessTracker | None] = ContextVar(
    "capture_owned_process_tracker", default=None
)
_ACTIVE_PRIVATE_MARKERS: ContextVar[list[str] | None] = ContextVar(
    "capture_private_markers", default=None
)
_ACTIVE_RAW_PRIVATE_MARKERS: ContextVar[tuple[str, ...] | None] = ContextVar(
    "capture_raw_private_markers", default=None
)
_ACTIVE_EXTERNAL_PIDS: ContextVar[tuple[int, ...] | None] = ContextVar(
    "capture_external_private_pids", default=None
)
_ACTIVE_PRIVATE_JOURNAL: ContextVar[CapturePrivateJournal | None] = ContextVar(
    "capture_private_journal", default=None
)


class SnapshotDrift(RuntimeError):
    """A named pre/post evidence input changed during the guarded attempt."""


@dataclass(frozen=True, slots=True)
class _AttemptTwoAuthorization:
    paths: CaptureAttemptPaths
    owner_path: Path
    attempt_one_public: Path
    attempt_one_private: Path
    attempt_one_public_tree_sha256: str
    attempt_one_private_tree_sha256: str


def _verify_attempt_one_public_regression(workspace: Path) -> Path:
    attempt_one = capture_attempt_paths(workspace, attempt=1)
    expectation_path = (
        attempt_one.public.parent / "attempt-1-public-regression.json"
    )
    if not attempt_one.public.is_dir() or not expectation_path.is_file():
        raise AssertionError("immutable attempt 1 public evidence is unavailable")
    expectation = load_capture_published_regression_expectation(expectation_path)
    verified = verify_capture_published_regression(
        attempt_one.public, expectation=expectation
    )
    if verified.get("attempt") != 1 or verified.get("status") != "FAIL":
        raise AssertionError("immutable attempt 1 public regression is invalid")
    return attempt_one.public


def _require_attempt_two_authorization(
    workspace: Path, environment: Mapping[str, str]
) -> _AttemptTwoAuthorization:
    """Fail closed before the single separately authorized attempt-2 invocation."""
    if environment.get("ONEC_RUNTIME_CAPTURE_ATTEMPT") != "2":
        raise AssertionError("the live harness accepts authorized attempt 2 only")
    attempt_one = capture_attempt_paths(workspace, attempt=1)
    attempt_two = capture_attempt_paths(workspace, attempt=2)
    attempt_one_public = _verify_attempt_one_public_regression(workspace)
    if not attempt_one.private.is_dir():
        raise AssertionError("immutable attempt 1 private evidence is unavailable")
    if attempt_two.public.exists() or attempt_two.private.exists():
        raise AssertionError("attempt 2 evidence already exists; overwrite is forbidden")
    owner_path = Path(workspace).resolve() / ".runtime" / "agent-service" / "runtime-owner.json"
    if _path_presence_state(owner_path) != "absent":
        raise AssertionError("a runtime owner is not verifiably absent before attempt 2")
    return _AttemptTwoAuthorization(
        paths=attempt_two,
        owner_path=owner_path,
        attempt_one_public=attempt_one_public,
        attempt_one_private=attempt_one.private,
        attempt_one_public_tree_sha256=tree_sha256(attempt_one_public),
        attempt_one_private_tree_sha256=tree_sha256(attempt_one.private),
    )


def _assert_attempt_one_unchanged(authorization: _AttemptTwoAuthorization) -> None:
    _verify_attempt_one_public_regression(
        authorization.attempt_one_public.parents[4]
    )
    if (
        tree_sha256(authorization.attempt_one_public)
        != authorization.attempt_one_public_tree_sha256
        or tree_sha256(authorization.attempt_one_private)
        != authorization.attempt_one_private_tree_sha256
    ):
        raise AssertionError("immutable attempt 1 changed during attempt 2")


_REQUEST_PHASES = frozenset({"main", "hypothesis", "continue-b", "finish-main"})


def _attempt_request_id(phase: str) -> str:
    if phase not in _REQUEST_PHASES:
        raise ValueError("capture request phase is invalid")
    return f"capture-task-6-{phase}-attempt-{_ATTEMPT}"


def _frontend_environment(control_descriptor: Path) -> dict[str, str]:
    return {
        "ONEC_RUNTIME_SERVICE_DESCRIPTOR": str(control_descriptor),
        "ONEC_RUNTIME_CONTROL_TIMEOUT_S": str(_CONTROL_TIMEOUT_S),
        "PYTHONPATH": str(_WORKSPACE / "src"),
    }


def _active_attempt_deadline() -> float:
    deadline = _ACTIVE_ATTEMPT_DEADLINE.get()
    if deadline is None or deadline <= monotonic():
        raise TimeoutError("live acceptance attempt deadline expired")
    return deadline


async def _bounded_sdk_call(awaitable: Any) -> Any:
    return await capture_support.bounded_sdk_await(
        awaitable,
        attempt_deadline=_active_attempt_deadline(),
        call_timeout_s=_SDK_CALL_TIMEOUT_S,
    )


def _notebook_revisions() -> tuple[dict[str, object], dict[str, object]]:
    notebook = nbformat.read(_NOTEBOOK, as_version=4)
    cells = {cell.id: cell for cell in notebook.cells}
    if set(cells) != {_MAIN_CELL, _HYPOTHESIS_CELL}:
        raise AssertionError("capture notebook cell set is not exact")
    result: list[dict[str, object]] = []
    for cell_id, source, mode in (
        (_MAIN_CELL, PAYROLL_MAIN_SOURCE, "main"),
        (_HYPOTHESIS_CELL, CAPTURE_A_SNAPSHOT_SOURCE, "capture"),
    ):
        cell = cells[cell_id]
        metadata = cell.metadata.get("onec_runtime")
        digest = sha256(str(cell.source).encode("utf-8")).hexdigest()
        if str(cell.source) != source:
            raise AssertionError(f"{cell_id} source differs from the approved source")
        if not isinstance(metadata, dict) or set(metadata) != {
            "language", "mode", "revision", "source_sha256"
        }:
            raise AssertionError(f"{cell_id} metadata is not exact")
        if (
            metadata["language"] != "bsl"
            or metadata["mode"] != mode
            or type(metadata["revision"]) is not int
            or metadata["revision"] <= 0
            or metadata["source_sha256"] != digest
        ):
            raise AssertionError(f"{cell_id} revision fence is invalid")
        result.append(
            {
                "cell_id": cell_id,
                "revision": metadata["revision"],
                "source_sha256": digest,
            }
        )
    return result[0], result[1]


def test_capture_acceptance_notebook_is_exact_and_immutable() -> None:
    main, hypothesis = _notebook_revisions()

    assert main == {
        "cell_id": _MAIN_CELL,
        "revision": 3,
        "source_sha256": sha256(PAYROLL_MAIN_SOURCE.encode("utf-8")).hexdigest(),
    }
    assert hypothesis == {
        "cell_id": _HYPOTHESIS_CELL,
        "revision": 2,
        "source_sha256": sha256(
            CAPTURE_A_SNAPSHOT_SOURCE.encode("utf-8")
        ).hexdigest(),
    }


def _path_identity(value: Path) -> str:
    return evidence_identity(_SALT, str(value.resolve()).casefold())


def _snapshot(
    *, platform_bin: Path, zup_source_root: Path
) -> dict[str, dict[str, object]]:
    def platform_snapshot() -> dict[str, object]:
        return {
            "version": "8.3.27.2170",
            "bin_identity_sha256": _path_identity(platform_bin),
            "service_python_identity_sha256": _path_identity(Path(sys.executable)),
            "service_python_sha256": file_sha256(Path(sys.executable)),
            "executables_sha256": {
                "1cv8.exe": file_sha256(platform_bin / "1cv8.exe"),
                "1cv8c.exe": file_sha256(platform_bin / "1cv8c.exe"),
                "dbgs.exe": file_sha256(platform_bin / "dbgs.exe"),
            },
        }

    def source_snapshot() -> dict[str, object]:
        points = locate_payroll_capture_points(zup_source_root)
        return {
            "tree_sha256": tree_sha256(
                zup_source_root, suffixes=(".bsl", ".mdo", ".xml")
            ),
            "capture_module_sha256": points.source_sha256,
        }

    def notebook_snapshot() -> dict[str, object]:
        main, hypothesis = _notebook_revisions()
        return {
            "artifact_sha256": file_sha256(_NOTEBOOK),
            "main_source_sha256": main["source_sha256"],
            "hypothesis_source_sha256": hypothesis["source_sha256"],
        }

    def implementation_snapshot() -> dict[str, object]:
        return {
            "runtime_tree_sha256": tree_sha256(
                _WORKSPACE / "src" / "onec_runtime", suffixes=(".py",)
            ),
            "harness_sha256": file_sha256(Path(__file__)),
            "capture_evidence_support_sha256": file_sha256(
                Path(capture_support.__file__)
            ),
            "process_evidence_support_sha256": file_sha256(
                Path(process_support.__file__)
            ),
            "verifier_sha256": file_sha256(
                _WORKSPACE
                / "src"
                / "onec_runtime"
                / "agent"
                / "capture_live_evidence.py"
            ),
            "evaluator_sha256": file_sha256(
                _WORKSPACE / "tools" / "eval_mcp_palettes.py"
            ),
        }

    collectors = {
        "platform": platform_snapshot,
        "source": source_snapshot,
        "notebook": notebook_snapshot,
        "implementation": implementation_snapshot,
    }
    return {
        name: capture_support.collect_snapshot_stage(name, collector)
        for name, collector in collectors.items()
    }


def _capture_point(name: str, *, line: int, procedure: str) -> dict[str, object]:
    return {
        "name": name,
        "project": "zup",
        "module": "ВедомостьНаВыплатуЗарплаты",
        "procedure": procedure,
        "line": line,
    }


def _stop_binding(
    location: object, *, point: dict[str, object]
) -> dict[str, object]:
    return {
        "name": point["name"],
        "project": point["project"],
        "module": point["module"],
        "procedure": point["procedure"],
        "line": point["line"],
        "executable_line": getattr(location, "line"),
        "module_type_identity_sha256": evidence_identity(
            _SALT, str(getattr(location, "module_type"))
        ),
        "extension_identity_sha256": evidence_identity(
            _SALT, str(getattr(location, "extension_name") or "base-configuration")
        ),
        "object_identity_sha256": evidence_identity(
            _SALT, str(getattr(location, "object_id"))
        ),
        "property_identity_sha256": evidence_identity(
            _SALT, str(getattr(location, "property_id"))
        ),
    }


def _capture_public(
    view: dict[str, Any], *, stop_binding: dict[str, object]
) -> dict[str, object]:
    capture = view.get("capture")
    if view.get("state") != "captured" or not isinstance(capture, dict):
        raise AssertionError("operation did not retain a paused capture")
    fence = capture.get("fence")
    location = capture.get("location")
    if not isinstance(fence, dict) or not isinstance(location, dict):
        raise AssertionError("capture correlation fence is missing")
    for field in ("name", "project", "module", "procedure", "line", "executable_line"):
        if location.get(field) != stop_binding[field]:
            raise AssertionError(f"resolved stop {field} differs from source binding")
    return {
        "fence": {
            "capture_intent_identity_sha256": evidence_identity(
                _SALT, str(fence["capture_intent_id"])
            ),
            "operation_identity_sha256": evidence_identity(
                _SALT, str(fence["operation_id"])
            ),
            "source_revision": fence["source_revision"],
            "source_sha256": fence["source_sha256"],
            "capture_generation": fence["capture_generation"],
            "stop_sequence": fence["stop_sequence"],
        },
        "location": {
            "name": location["name"],
            "project": location["project"],
            "module": location["module"],
            "procedure": location["procedure"],
            "line": location["line"],
            "executable_line": location["executable_line"],
            "source_revision": location["source_revision"],
            "source_sha256": location["source_sha256"],
            **{
                name: stop_binding[name]
                for name in (
                    "module_type_identity_sha256",
                    "extension_identity_sha256",
                    "object_identity_sha256",
                    "property_identity_sha256",
                )
            },
        },
        "state": "captured",
    }


def _known_rows(value: object) -> int:
    if not isinstance(value, dict):
        raise AssertionError("known size is unavailable")
    rows = value.get("rows")
    if type(rows) is not int or rows < 0:
        raise AssertionError("known row count is unavailable")
    return rows


def _private_wire(items: tuple[ProcessIdentity, ...]) -> list[dict[str, object]]:
    return [item.private_wire() for item in items]


def _snapshot_unrelated_processes() -> tuple[ProcessIdentity, ...]:
    identities: list[ProcessIdentity] = []
    for process in psutil.process_iter(("name",)):
        try:
            name = str(process.info["name"]).casefold()
            role = {
                "1cv8.exe": "unrelated_designer",
                "1cv8c.exe": "unrelated_onec",
                "dbgs.exe": "unrelated_dbgs",
            }.get(name)
            if role is None:
                continue
            identities.append(
                ProcessIdentity(
                    role,
                    process.pid,
                    process.create_time(),
                    str(Path(process.exe()).resolve()),
                )
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            raise AssertionError("unrelated 1C process identity is not verifiable")
    return tuple(identities)


def _record_private_rejection(
    journal: CapturePrivateJournal,
    *,
    index: int,
    method: str,
    call_identity: Mapping[str, object],
    error: BaseException,
) -> None:
    raw = record_capture_private_raw_response(
        journal.private_attempt,
        index=index,
        method=method,
        payload={
            "ok": False,
            "value": None,
            "failure": {"type": type(error).__name__},
        },
    )
    journal.append(
        "call_response", {**call_identity, "accepted": False, "raw": raw}
    )


async def _recorded_call(
    session: ClientSession,
    *,
    method: str,
    arguments: dict[str, object],
    label: str | None,
    calls: list[str],
    raw_index: list[int],
    raw_dir: Path,
    private_journal: CapturePrivateJournal | None = None,
) -> dict[str, Any] | list[Any]:
    journal = private_journal or _ACTIVE_PRIVATE_JOURNAL.get()
    if journal is None:
        raise AssertionError("private replay journal is unavailable")
    raw_index[0] += 1
    index = raw_index[0]
    call_identity = {
        "index": index,
        "stage": label or f"private_call_{index}",
        "public_label": label,
        "operation_group": label,
        "method": method,
    }
    journal.append("call_started", {**call_identity, "arguments": arguments})
    try:
        result = await _bounded_sdk_call(session.call_tool(method, arguments))
    except BaseException as error:
        _record_private_rejection(
            journal,
            index=index,
            method=method,
            call_identity=call_identity,
            error=error,
        )
        raise
    tracker = _ACTIVE_TRACKER.get()
    markers = _ACTIVE_PRIVATE_MARKERS.get()
    raw_markers = _ACTIVE_RAW_PRIVATE_MARKERS.get()
    external_pids = _ACTIVE_EXTERNAL_PIDS.get()
    if (
        tracker is None
        or markers is None
        or raw_markers is None
        or external_pids is None
    ):
        raise AssertionError("live MCP privacy context is unavailable")
    from integration.evidence.capture_live_evidence import (
        assert_mcp_response_private_safe,
    )

    expected_private_fields: dict[str, str] = {}
    if method == "workspace.open":
        expected_private_fields["project_root"] = str(_WORKSPACE)
    elif method == "code.get":
        source_by_cell = {
            _MAIN_CELL: PAYROLL_MAIN_SOURCE,
            _HYPOTHESIS_CELL: CAPTURE_A_SNAPSHOT_SOURCE,
        }
        cell_id = arguments.get("cell_id")
        if cell_id not in source_by_cell:
            raise AssertionError("code.get private source is not pre-bound")
        expected_private_fields["source"] = source_by_cell[cell_id]
    try:
        assert_mcp_response_private_safe(
            result.structured_content,
            method=method,
            expected_private_fields=expected_private_fields,
            private_markers=raw_markers,
            owned_pids=tuple(item.pid for item in tracker.identities()),
            external_pids=external_pids,
        )
    except BaseException as error:
        _record_private_rejection(
            journal,
            index=index,
            method=method,
            call_identity=call_identity,
            error=error,
        )
        raise
    raw = record_capture_private_raw_response(
        journal.private_attempt,
        index=index,
        method=method,
        payload=result.structured_content,
    )
    try:
        capture_support.extend_private_markers_from_response(
            markers, result.structured_content
        )
        value = require_mcp_value(result, method)
    except BaseException:
        journal.append(
            "call_response", {**call_identity, "accepted": False, "raw": raw}
        )
        raise
    journal.append(
        "call_response", {**call_identity, "accepted": True, "raw": raw}
    )
    if label is not None:
        calls.append(label)
    return value


_WAIT_S_METHODS = frozenset(
    {
        "code.run",
        "code.run_inline",
        "capture.run_until",
        "capture.hypothesis",
        "capture.continue",
    }
)
_PENDING_OPERATION_STATES = frozenset({"queued", "running"})


def _operation_control_deadline() -> float:
    return _active_attempt_deadline()


def _schema_wait_slice(control_deadline: float) -> float:
    remaining = control_deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError("MCP operation control deadline expired")
    return min(_MCP_SCHEMA_WAIT_MAX_S, remaining)


async def _bounded_operation_exchange(
    invoke: Callable[[str, dict[str, object]], Awaitable[dict[str, Any] | list[Any]]],
    *,
    method: str,
    arguments: dict[str, object],
    control_deadline: float,
) -> tuple[dict[str, Any] | list[Any], dict[str, Any] | list[Any]]:
    """Submit once, then poll one operation in schema-valid wait slices."""
    submitted_arguments = dict(arguments)
    if method in _WAIT_S_METHODS:
        submitted_arguments["wait_s"] = _schema_wait_slice(control_deadline)
    initial = await invoke(method, submitted_arguments)
    current = initial
    while (
        isinstance(current, dict)
        and current.get("state") in _PENDING_OPERATION_STATES
    ):
        after_event_cursor = current.get("next_event_cursor", 0)
        after_message_cursor = current.get("next_message_cursor", 0)
        if (
            type(after_event_cursor) is not int
            or after_event_cursor < 0
            or type(after_message_cursor) is not int
            or after_message_cursor < 0
        ):
            raise AssertionError("operation cursors are invalid")
        current = await invoke(
            "operation.wait",
            {
                "operation_id": mcp_operation_id(current),
                "timeout_s": _schema_wait_slice(control_deadline),
                "after_event_cursor": after_event_cursor,
                "after_message_cursor": after_message_cursor,
            },
        )
    return initial, current


async def _recorded_operation_call(
    session: ClientSession,
    *,
    method: str,
    arguments: dict[str, object],
    label: str,
    wait_label: str | None = None,
    calls: list[str],
    raw_index: list[int],
    raw_dir: Path,
) -> tuple[dict[str, Any] | list[Any], dict[str, Any] | list[Any]]:
    poll_index = 0

    async def invoke(
        emitted_method: str, emitted_arguments: dict[str, object]
    ) -> dict[str, Any] | list[Any]:
        nonlocal poll_index
        emitted_label = label
        if emitted_method == "operation.wait":
            emitted_label = wait_label if poll_index == 0 else None
            poll_index += 1
        return await _recorded_call(
            session,
            method=emitted_method,
            arguments=emitted_arguments,
            label=emitted_label,
            calls=calls,
            raw_index=raw_index,
            raw_dir=raw_dir,
        )

    return await _bounded_operation_exchange(
        invoke,
        method=method,
        arguments=arguments,
        control_deadline=_operation_control_deadline(),
    )


async def _frontend_a_capture_body(
    parameters: StdioServerParameters,
    *,
    tracker: OwnedProcessTracker,
    calls: list[str],
    raw_index: list[int],
    raw_dir: Path,
    main: dict[str, object],
    hypothesis: dict[str, object],
    point_a: dict[str, object],
) -> dict[str, Any]:
    errlog_path = raw_dir / "mcp-a-stderr.txt"
    raw_dir.mkdir(parents=True, exist_ok=True)
    with errlog_path.open("w+", encoding="utf-8") as errlog:
        async with stdio_client(parameters, errlog=errlog) as (
            read_stream,
            write_stream,
        ):
            async with ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=min(
                    _SDK_CALL_TIMEOUT_S,
                    max(0.001, _active_attempt_deadline() - monotonic()),
                ),
            ) as session:
                await _bounded_sdk_call(session.initialize())
                listed_tools = await _bounded_sdk_call(session.list_tools())
                if {tool.name for tool in listed_tools.tools} != set(
                    CAPTURE_TOOL_NAMES
                ):
                    raise AssertionError("live frontend tool palette is not capture")
                frontend_identity = find_mcp_process(
                    tracker,
                    "mcp_a",
                    required_cmdline=(
                        "-m", "onec_runtime_mcp.mcp_entrypoint", "--workspace",
                        str(_WORKSPACE), "--profile", "capture",
                    ),
                )
                await _recorded_call(
                    session,
                    method="workspace.open",
                    arguments={"project": str(_WORKSPACE)},
                    label="A:workspace.open",
                    calls=calls,
                    raw_index=raw_index,
                    raw_dir=raw_dir,
                )
                ensured, waited = await _recorded_operation_call(
                    session,
                    method="runtime.ensure",
                    arguments={"profile": "zup", "mode": "experiment"},
                    label="A:runtime.ensure",
                    wait_label="A:operation.wait.runtime",
                    calls=calls,
                    raw_index=raw_index,
                    raw_dir=raw_dir,
                )
                if not isinstance(ensured, dict):
                    raise AssertionError("runtime.ensure returned no operation")
                if ensured.get("state") in {"queued", "running"}:
                    if not isinstance(waited, dict) or waited.get("state") != "completed":
                        raise AssertionError("runtime startup did not complete")
                else:
                    # The one-shot harness requires a freshly started dedicated target;
                    # an already-idle descriptor would make the expected call trace false.
                    raise AssertionError("runtime.ensure did not publish a startup operation")
                _, discovery = await _recorded_operation_call(
                    session,
                    method="code.run_inline",
                    arguments={
                        "language": "bsl",
                        "mode": "main",
                        "source": PAYROLL_DISCOVERY_SOURCE,
                        "inputs": {},
                    },
                    label="A:code.run_inline.discovery",
                    calls=calls,
                    raw_index=raw_index,
                    raw_dir=raw_dir,
                )
                if not isinstance(discovery, dict) or discovery.get("state") != "completed":
                    raise AssertionError("approved ZUP discovery did not complete")
                listed = await _recorded_call(
                    session,
                    method="code.list",
                    arguments={"container": _CONTAINER},
                    label="A:code.list",
                    calls=calls,
                    raw_index=raw_index,
                    raw_dir=raw_dir,
                )
                if not isinstance(listed, list) or len(listed) != 2:
                    raise AssertionError("capture notebook was not loaded exactly")
                main_wire = await _recorded_call(
                    session,
                    method="code.get",
                    arguments={"cell_id": _MAIN_CELL},
                    label="A:code.get.main",
                    calls=calls,
                    raw_index=raw_index,
                    raw_dir=raw_dir,
                )
                hypothesis_wire = await _recorded_call(
                    session,
                    method="code.get",
                    arguments={"cell_id": _HYPOTHESIS_CELL},
                    label="A:code.get.hypothesis",
                    calls=calls,
                    raw_index=raw_index,
                    raw_dir=raw_dir,
                )
                for actual, expected in (
                    (main_wire, main),
                    (hypothesis_wire, hypothesis),
                ):
                    if not isinstance(actual, dict) or any(
                        actual[field] != expected[field]
                        for field in ("cell_id", "revision", "source_sha256")
                    ):
                        raise AssertionError("saved notebook revision fence changed")
                run_request = {
                    **main,
                    "points": [point_a],
                    "request_id": _attempt_request_id("main"),
                }
                _, captured = await _recorded_operation_call(
                    session,
                    method="capture.run_until",
                    arguments=run_request,
                    label="A:capture.run_until.a",
                    calls=calls,
                    raw_index=raw_index,
                    raw_dir=raw_dir,
                )
                if not isinstance(captured, dict) or captured.get("state") != "captured":
                    raise AssertionError("capture A was not reached")
                return {
                    "captured": captured,
                    "run_request": run_request,
                    "frontend_identity": frontend_identity,
                }


async def _frontend_a_capture(
    parameters: StdioServerParameters,
    *,
    attempt_deadline: float,
    private_markers: list[str],
    raw_private_markers: tuple[str, ...],
    external_pids: tuple[int, ...],
    tracker: OwnedProcessTracker,
    calls: list[str],
    raw_index: list[int],
    raw_dir: Path,
    private_journal: CapturePrivateJournal,
    main: dict[str, object],
    hypothesis: dict[str, object],
    point_a: dict[str, object],
) -> dict[str, Any]:
    token = _ACTIVE_ATTEMPT_DEADLINE.set(attempt_deadline)
    tracker_token = _ACTIVE_TRACKER.set(tracker)
    markers_token = _ACTIVE_PRIVATE_MARKERS.set(private_markers)
    raw_markers_token = _ACTIVE_RAW_PRIVATE_MARKERS.set(raw_private_markers)
    external_pids_token = _ACTIVE_EXTERNAL_PIDS.set(external_pids)
    journal_token = _ACTIVE_PRIVATE_JOURNAL.set(private_journal)
    try:
        async with asyncio.timeout(max(0.001, attempt_deadline - monotonic())):
            return await _frontend_a_capture_body(
                parameters,
                tracker=tracker,
                calls=calls,
                raw_index=raw_index,
                raw_dir=raw_dir,
                main=main,
                hypothesis=hypothesis,
                point_a=point_a,
            )
    finally:
        _ACTIVE_PRIVATE_JOURNAL.reset(journal_token)
        _ACTIVE_EXTERNAL_PIDS.reset(external_pids_token)
        _ACTIVE_RAW_PRIVATE_MARKERS.reset(raw_markers_token)
        _ACTIVE_PRIVATE_MARKERS.reset(markers_token)
        _ACTIVE_TRACKER.reset(tracker_token)
        _ACTIVE_ATTEMPT_DEADLINE.reset(token)


def _single_mapping(items: object, name: str) -> dict[str, Any]:
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        raise AssertionError(f"{name} did not produce exactly one descriptor")
    return items[0]


def _require_output_alias(
    view: dict[str, Any], *, alias: str, name: str
) -> dict[str, Any]:
    outputs = view.get("outputs")
    if not isinstance(outputs, dict) or set(outputs) != {alias}:
        raise AssertionError(f"{name} output alias is not exact")
    output = outputs[alias]
    if not isinstance(output, dict):
        raise AssertionError(f"{name} output descriptor is invalid")
    return output


async def _expected_stale(
    session: ClientSession,
    *,
    proxy_ids: tuple[str, ...],
    raw_index: list[int],
    raw_dir: Path,
) -> int:
    stale = 0
    for proxy_id in proxy_ids:
        journal = _ACTIVE_PRIVATE_JOURNAL.get()
        if journal is None:
            raise AssertionError("private replay journal is unavailable")
        arguments = {
            "proxy_id": proxy_id,
            "detail": "auto",
            "budget_profile": "agent_metadata",
        }
        raw_index[0] += 1
        index = raw_index[0]
        call_identity = {
            "index": index,
            "stage": f"stale_proxy_check_{index}",
            "public_label": None,
            "operation_group": "stale_proxy_check",
            "method": "value.inspect",
        }
        journal.append("call_started", {**call_identity, "arguments": arguments})
        try:
            result = await _bounded_sdk_call(
                session.call_tool("value.inspect", arguments)
            )
        except BaseException as error:
            _record_private_rejection(
                journal,
                index=index,
                method="value.inspect",
                call_identity=call_identity,
                error=error,
            )
            raise
        tracker = _ACTIVE_TRACKER.get()
        markers = _ACTIVE_PRIVATE_MARKERS.get()
        raw_markers = _ACTIVE_RAW_PRIVATE_MARKERS.get()
        external_pids = _ACTIVE_EXTERNAL_PIDS.get()
        if (
            tracker is None
            or markers is None
            or raw_markers is None
            or external_pids is None
        ):
            raise AssertionError("live MCP privacy context is unavailable")
        from integration.evidence.capture_live_evidence import (
            assert_mcp_response_private_safe,
        )

        try:
            assert_mcp_response_private_safe(
                result.structured_content,
                method="value.inspect",
                expected_private_fields={},
                private_markers=raw_markers,
                owned_pids=tuple(item.pid for item in tracker.identities()),
                external_pids=external_pids,
            )
        except BaseException as error:
            _record_private_rejection(
                journal,
                index=index,
                method="value.inspect",
                call_identity=call_identity,
                error=error,
            )
            raise
        raw = record_capture_private_raw_response(
            journal.private_attempt,
            index=index,
            method="value.inspect",
            payload=result.structured_content,
        )
        journal.append(
            "call_response", {**call_identity, "accepted": False, "raw": raw}
        )
        capture_support.extend_private_markers_from_response(
            markers, result.structured_content
        )
        payload = result.structured_content
        if (
            not isinstance(payload, dict)
            or payload.get("ok") is not False
            or not isinstance(payload.get("failure"), dict)
            or payload["failure"].get("category") != "stale"
        ):
            raise AssertionError("an A-fenced proxy survived capture generation change")
        stale += 1
    return stale


async def _frontend_b_capture_body(
    parameters: StdioServerParameters,
    *,
    tracker: OwnedProcessTracker,
    calls: list[str],
    raw_index: list[int],
    raw_dir: Path,
    first: dict[str, Any],
    hypothesis_ref: dict[str, object],
    point_b: dict[str, object],
) -> dict[str, Any]:
    errlog_path = raw_dir / "mcp-b-stderr.txt"
    with errlog_path.open("w+", encoding="utf-8") as errlog:
        async with stdio_client(parameters, errlog=errlog) as (
            read_stream,
            write_stream,
        ):
            async with ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=min(
                    _SDK_CALL_TIMEOUT_S,
                    max(0.001, _active_attempt_deadline() - monotonic()),
                ),
            ) as session:
                await _bounded_sdk_call(session.initialize())
                frontend_identity = find_mcp_process(
                    tracker,
                    "mcp_b",
                    required_cmdline=(
                        "-m", "onec_runtime_mcp.mcp_entrypoint", "--workspace",
                        str(_WORKSPACE), "--profile", "capture",
                    ),
                )
                await _recorded_call(
                    session,
                    method="workspace.open",
                    arguments={"project": str(_WORKSPACE)},
                    label="B:workspace.open",
                    calls=calls,
                    raw_index=raw_index,
                    raw_dir=raw_dir,
                )
                _, ready = await _recorded_operation_call(
                    session,
                    method="runtime.ensure",
                    arguments={"profile": "zup", "mode": "experiment"},
                    label="B:runtime.ensure",
                    calls=calls,
                    raw_index=raw_index,
                    raw_dir=raw_dir,
                )
                if not isinstance(ready, dict) or ready.get("state") != "captured":
                    raise AssertionError("replacement frontend did not join captured runtime")
                _, replay = await _recorded_operation_call(
                    session,
                    method="capture.run_until",
                    arguments=first["run_request"],
                    label="B:operation.replay",
                    calls=calls,
                    raw_index=raw_index,
                    raw_dir=raw_dir,
                )
                if (
                    not isinstance(replay, dict)
                    or replay.get("state") != "captured"
                    or mcp_operation_id(replay)
                    != mcp_operation_id(first["captured"])
                ):
                    raise AssertionError("capture.run_until replay changed MAIN identity")
                capture_a = replay["capture"]
                if not isinstance(capture_a, dict) or not isinstance(
                    capture_a.get("fence"), dict
                ):
                    raise AssertionError("replayed capture A has no fence")
                fence_a = capture_a["fence"]

                locals_view = await _recorded_call(
                    session,
                    method="capture.inspect",
                    arguments={
                        "fence": fence_a,
                        "filters": {"role": "local"},
                        "cursor": 0,
                        "limit": 20,
                    },
                    label="B:capture.inspect.locals",
                    calls=calls,
                    raw_index=raw_index,
                    raw_dir=raw_dir,
                )
                if not isinstance(locals_view, dict):
                    raise AssertionError("capture A locals inspection is invalid")
                local_descriptors = locals_view.get("variables")
                if not isinstance(local_descriptors, list) or not local_descriptors:
                    raise AssertionError("capture A returned no bounded local proxies")
                local_by_name = {
                    str(item.get("name", "")).casefold(): item
                    for item in local_descriptors
                    if isinstance(item, dict)
                }
                selected_locals = tuple(
                    local_by_name.get(name.casefold()) for name in _CAPTURE_A_LOCAL_NAMES
                )
                if any(not isinstance(item, dict) for item in selected_locals):
                    raise AssertionError("capture A exact allowlisted locals are missing")
                local_proxy_ids = tuple(
                    str(item["proxy_id"]) for item in selected_locals  # type: ignore[index]
                )
                if any(not proxy_id for proxy_id in local_proxy_ids):
                    raise AssertionError("capture A exact local proxies are incomplete")

                manager_view = await _recorded_call(
                    session,
                    method="capture.inspect",
                    arguments={
                        "fence": fence_a,
                        "filters": {},
                        "cursor": 0,
                        "limit": 20,
                        "observe": {
                            "items": [
                                {
                                    "alias": "manager_a",
                                    "source": {
                                        "kind": "temporary_table_manager",
                                        "origin": {
                                            "namespace": "frame",
                                            "root": "МенеджерВременныхТаблиц",
                                            "fields": [],
                                        },
                                    },
                                    "result": "proxy",
                                }
                            ],
                            "budget_profile": "agent_metadata",
                        },
                    },
                    label="B:capture.inspect.manager",
                    calls=calls,
                    raw_index=raw_index,
                    raw_dir=raw_dir,
                )
                if not isinstance(manager_view, dict):
                    raise AssertionError("capture A manager inspection is invalid")
                _require_output_alias(
                    manager_view, alias="manager_a", name="capture A manager"
                )
                manager_a = _single_mapping(
                    manager_view.get("temporary_table_managers"), "capture A manager"
                )
                if manager_a.get("origin") != {
                    "namespace": "frame",
                    "root": "МенеджерВременныхТаблиц",
                    "fields": [],
                }:
                    raise AssertionError("capture A manager origin is not exact")
                table_view = await _recorded_call(
                    session,
                    method="capture.inspect",
                    arguments={
                        "fence": fence_a,
                        "filters": {},
                        "cursor": 0,
                        "limit": 20,
                        "observe": {
                            "items": [
                                {
                                    "alias": "table_a",
                                    "source": {
                                        "kind": "temporary_table",
                                        "manager_id": manager_a["manager_id"],
                                        "table": "ВТЗарплатаКВыплате",
                                    },
                                    "select": {
                                        "kind": "table_rows",
                                        "offset": 0,
                                        "limit": 5,
                                        "columns": [],
                                    },
                                    "result": "proxy",
                                }
                            ],
                            "budget_profile": "agent_metadata",
                        },
                    },
                    label="B:capture.inspect.table",
                    calls=calls,
                    raw_index=raw_index,
                    raw_dir=raw_dir,
                )
                if not isinstance(table_view, dict):
                    raise AssertionError("capture A table inspection is invalid")
                _require_output_alias(
                    table_view, alias="table_a", name="capture A table"
                )
                table_a = _single_mapping(
                    table_view.get("temporary_tables"), "capture A table"
                )
                if (
                    table_a.get("manager_id") != manager_a.get("manager_id")
                    or str(table_a.get("name", "")).casefold()
                    != _TABLE_NAME.casefold()
                ):
                    raise AssertionError("capture A table linkage is not exact")
                frame = await _recorded_call(
                    session,
                    method="value.to_df",
                    arguments={
                        "proxy_id": table_a["table_id"],
                        "budget_profile": "agent_dataframe",
                        "refs": "presentation",
                    },
                    label="B:value.to_df.head",
                    calls=calls,
                    raw_index=raw_index,
                    raw_dir=raw_dir,
                )
                if not isinstance(frame, dict) or not isinstance(
                    frame.get("proxy_id"), str
                ):
                    raise AssertionError("bounded capture A DataFrame was not published")

                hypothesis_wire = await _recorded_call(
                    session,
                    method="code.get",
                    arguments={"cell_id": _HYPOTHESIS_CELL},
                    label="B:code.get.hypothesis",
                    calls=calls,
                    raw_index=raw_index,
                    raw_dir=raw_dir,
                )
                if not isinstance(hypothesis_wire, dict) or any(
                    hypothesis_wire[field] != hypothesis_ref[field]
                    for field in ("cell_id", "revision", "source_sha256")
                ):
                    raise AssertionError("capture hypothesis revision changed")
                _, hypothesis = await _recorded_operation_call(
                    session,
                    method="capture.hypothesis",
                    arguments={
                        "fence": fence_a,
                        "code_ref": hypothesis_ref,
                        "request_id": _attempt_request_id("hypothesis"),
                        "observe": {
                            "items": [
                                {
                                    "alias": "captured_table",
                                    "source": {
                                        "kind": "frame_local",
                                        "name": "ДемоВТДо",
                                    },
                                    "result": "proxy",
                                },
                                {
                                    "alias": "missing",
                                    "source": {
                                        "kind": "frame_local",
                                        "name": "НетТакойПеременной",
                                    },
                                    "result": "proxy",
                                },
                            ],
                            "budget_profile": "agent_metadata",
                        },
                    },
                    label="B:capture.hypothesis",
                    calls=calls,
                    raw_index=raw_index,
                    raw_dir=raw_dir,
                )
                if (
                    not isinstance(hypothesis, dict)
                    or hypothesis.get("state") != "captured"
                    or not isinstance(hypothesis.get("failure"), dict)
                    or hypothesis["failure"].get("stage") != "observation"
                    or set(hypothesis.get("outputs", {})) != {"captured_table"}
                ):
                    raise AssertionError("paused hypothesis did not prove partial observation")
                hypothesis_capture = hypothesis.get("capture")
                if not isinstance(hypothesis_capture, dict):
                    raise AssertionError("hypothesis released the capture")
                dirty_roots = hypothesis_capture.get("dirty_roots")
                if (
                    not isinstance(dirty_roots, list)
                    or not dirty_roots
                    or any(not isinstance(root, str) for root in dirty_roots)
                ):
                    raise AssertionError("hypothesis did not stage ordered dirty roots")

                _, continued = await _recorded_operation_call(
                    session,
                    method="capture.continue",
                    arguments={
                        "fence": fence_a,
                        "next_points": [point_b],
                        "request_id": _attempt_request_id("continue-b"),
                    },
                    label="B:capture.continue",
                    calls=calls,
                    raw_index=raw_index,
                    raw_dir=raw_dir,
                )
                if not isinstance(continued, dict) or continued.get("state") != "captured":
                    raise AssertionError("the one A-to-B continuation did not reach capture B")
                capture_b = continued.get("capture")
                if not isinstance(capture_b, dict) or not isinstance(
                    capture_b.get("fence"), dict
                ):
                    raise AssertionError("capture B fence is missing")
                fence_b = capture_b["fence"]
                old_proxy_ids = local_proxy_ids + (
                    str(table_a["table_id"]),
                    str(frame["proxy_id"]),
                )
                stale_count = await _expected_stale(
                    session,
                    proxy_ids=old_proxy_ids,
                    raw_index=raw_index,
                    raw_dir=raw_dir,
                )
                private_journal = _ACTIVE_PRIVATE_JOURNAL.get()
                if private_journal is None:
                    raise AssertionError("private replay journal is unavailable")
                private_journal.append(
                    "stage",
                    {
                        "name": "B_value_inspect_stale",
                        "public_label": "B:value.inspect.stale",
                    },
                )
                calls.append("B:value.inspect.stale")

                manager_b_view = await _recorded_call(
                    session,
                    method="capture.inspect",
                    arguments={
                        "fence": fence_b,
                        "filters": {
                            "role": "local",
                            "name": _DOWNSTREAM_RESULT_LOCAL,
                        },
                        "cursor": 0,
                        "limit": 20,
                        "observe": {
                            "items": [
                                {
                                    "alias": "manager_b",
                                    "source": {
                                        "kind": "temporary_table_manager",
                                        "origin": {
                                            "namespace": "frame",
                                            "root": "МенеджерВременныхТаблиц",
                                            "fields": [],
                                        },
                                    },
                                    "result": "proxy",
                                }
                            ],
                            "budget_profile": "agent_metadata",
                        },
                    },
                    label="B:capture.inspect.downstream.manager",
                    calls=calls,
                    raw_index=raw_index,
                    raw_dir=raw_dir,
                )
                if not isinstance(manager_b_view, dict):
                    raise AssertionError("capture B manager inspection is invalid")
                _require_output_alias(
                    manager_b_view, alias="manager_b", name="capture B manager"
                )
                manager_b = _single_mapping(
                    manager_b_view.get("temporary_table_managers"), "capture B manager"
                )
                if manager_b.get("origin") != {
                    "namespace": "frame",
                    "root": "МенеджерВременныхТаблиц",
                    "fields": [],
                }:
                    raise AssertionError("capture B manager origin is not exact")
                downstream_locals = manager_b_view.get("variables")
                if not isinstance(downstream_locals, list) or not downstream_locals:
                    raise AssertionError("capture B exposed no bounded downstream result")
                if len(downstream_locals) != 1 or not isinstance(
                    downstream_locals[0], dict
                ):
                    raise AssertionError("capture B exact result local is not singular")
                result_b = downstream_locals[0]
                if (
                    str(result_b.get("name", "")).casefold()
                    != _DOWNSTREAM_RESULT_LOCAL.casefold()
                    or not result_b.get("proxy_id")
                ):
                    raise AssertionError("capture B result proxy is missing")
                table_b_view = await _recorded_call(
                    session,
                    method="capture.inspect",
                    arguments={
                        "fence": fence_b,
                        "filters": {},
                        "cursor": 0,
                        "limit": 20,
                        "observe": {
                            "items": [
                                {
                                    "alias": "table_b",
                                    "source": {
                                        "kind": "temporary_table",
                                        "manager_id": manager_b["manager_id"],
                                        "table": "ВТЗарплатаКВыплате",
                                    },
                                    "select": {
                                        "kind": "table_rows",
                                        "offset": 0,
                                        "limit": 5,
                                        "columns": [],
                                    },
                                    "result": "proxy",
                                }
                            ],
                            "budget_profile": "agent_metadata",
                        },
                    },
                    label="B:capture.inspect.downstream.table",
                    calls=calls,
                    raw_index=raw_index,
                    raw_dir=raw_dir,
                )
                if not isinstance(table_b_view, dict):
                    raise AssertionError("capture B table inspection is invalid")
                _require_output_alias(
                    table_b_view, alias="table_b", name="capture B table"
                )
                table_b = _single_mapping(
                    table_b_view.get("temporary_tables"), "capture B table"
                )
                if (
                    table_b.get("manager_id") != manager_b.get("manager_id")
                    or str(table_b.get("name", "")).casefold()
                    != _TABLE_NAME.casefold()
                ):
                    raise AssertionError("capture B table linkage is not exact")
                await _recorded_call(
                    session,
                    method="value.inspect",
                    arguments={
                        "proxy_id": result_b["proxy_id"],
                        "detail": "auto",
                        "budget_profile": "agent_metadata",
                    },
                    label="B:value.inspect.result",
                    calls=calls,
                    raw_index=raw_index,
                    raw_dir=raw_dir,
                )
                _, terminal = await _recorded_operation_call(
                    session,
                    method="capture.continue",
                    arguments={
                        "fence": fence_b,
                        "next_points": [],
                        "request_id": _attempt_request_id("finish-main"),
                    },
                    label="B:capture.continue.terminal",
                    calls=calls,
                    raw_index=raw_index,
                    raw_dir=raw_dir,
                )
                if (
                    not isinstance(terminal, dict)
                    or terminal.get("state") != "completed"
                    or terminal.get("capture") is not None
                ):
                    raise AssertionError("terminal MAIN did not complete after capture B")
                await _recorded_call(
                    session,
                    method="runtime.close",
                    arguments={"policy": "abort_generation"},
                    label="B:runtime.close",
                    calls=calls,
                    raw_index=raw_index,
                    raw_dir=raw_dir,
                )
                return {
                    "replay": replay,
                    "frontend_identity": frontend_identity,
                    "locals": locals_view,
                    "selected_locals": selected_locals,
                    "manager_a": manager_a,
                    "manager_a_table_count": len(
                        table_view.get("temporary_tables", [])
                    ),
                    "table_a": table_a,
                    "frame": frame,
                    "hypothesis": hypothesis,
                    "dirty_roots": dirty_roots,
                    "continued": continued,
                    "stale_checked": len(old_proxy_ids),
                    "stale_count": stale_count,
                    "old_proxy_ids": old_proxy_ids,
                    "table_b": table_b,
                    "result_b": result_b,
                    "terminal": terminal,
                }


async def _frontend_b_capture(
    parameters: StdioServerParameters,
    *,
    attempt_deadline: float,
    private_markers: list[str],
    raw_private_markers: tuple[str, ...],
    external_pids: tuple[int, ...],
    tracker: OwnedProcessTracker,
    calls: list[str],
    raw_index: list[int],
    raw_dir: Path,
    private_journal: CapturePrivateJournal,
    first: dict[str, Any],
    hypothesis_ref: dict[str, object],
    point_b: dict[str, object],
) -> dict[str, Any]:
    token = _ACTIVE_ATTEMPT_DEADLINE.set(attempt_deadline)
    tracker_token = _ACTIVE_TRACKER.set(tracker)
    markers_token = _ACTIVE_PRIVATE_MARKERS.set(private_markers)
    raw_markers_token = _ACTIVE_RAW_PRIVATE_MARKERS.set(raw_private_markers)
    external_pids_token = _ACTIVE_EXTERNAL_PIDS.set(external_pids)
    journal_token = _ACTIVE_PRIVATE_JOURNAL.set(private_journal)
    try:
        async with asyncio.timeout(max(0.001, attempt_deadline - monotonic())):
            return await _frontend_b_capture_body(
                parameters,
                tracker=tracker,
                calls=calls,
                raw_index=raw_index,
                raw_dir=raw_dir,
                first=first,
                hypothesis_ref=hypothesis_ref,
                point_b=point_b,
            )
    finally:
        _ACTIVE_PRIVATE_JOURNAL.reset(journal_token)
        _ACTIVE_EXTERNAL_PIDS.reset(external_pids_token)
        _ACTIVE_RAW_PRIVATE_MARKERS.reset(raw_markers_token)
        _ACTIVE_PRIVATE_MARKERS.reset(markers_token)
        _ACTIVE_TRACKER.reset(tracker_token)
        _ACTIVE_ATTEMPT_DEADLINE.reset(token)


def _budget(profile: str) -> dict[str, object]:
    budgets = {
        "agent_metadata": {
            "profile": "agent_metadata",
            "max_depth": 1,
            "max_items": 20,
            "max_rows": 20,
            "max_bytes": 16384,
            "timeout_ms": 1000,
            "cost_class": "metadata",
        },
        "agent_dataframe": {
            "profile": "agent_dataframe",
            "max_depth": 8,
            "max_items": 200000,
            "max_rows": 10000,
            "max_bytes": 67108864,
            "timeout_ms": 30000,
            "cost_class": "full_scan",
        },
    }
    try:
        return dict(budgets[profile])
    except KeyError as error:
        raise AssertionError("unknown server-owned budget profile") from error


def _pass_observations(
    *,
    calls: list[str],
    main: dict[str, object],
    hypothesis_ref: dict[str, object],
    first: dict[str, Any],
    second: dict[str, Any],
) -> dict[str, object]:
    replay = second["replay"]
    hypothesis = second["hypothesis"]
    continued = second["continued"]
    terminal = second["terminal"]
    table_a = second["table_a"]
    table_b = second["table_b"]
    frame = second["frame"]
    result_b = second["result_b"]
    locals_view = second["locals"]
    manager_a = second["manager_a"]
    selected_locals = second["selected_locals"]
    old_proxy_ids = second["old_proxy_ids"]
    if not all(
        isinstance(item, dict)
        for item in (
            replay,
            hypothesis,
            continued,
            terminal,
            table_a,
            table_b,
            frame,
            result_b,
            locals_view,
            manager_a,
        )
    ):
        raise AssertionError("live capture derivation inputs are invalid")
    dirty_roots = second["dirty_roots"]
    if not isinstance(dirty_roots, list):
        raise AssertionError("dirty root evidence is invalid")
    roots = [evidence_identity(_SALT, str(root).casefold()) for root in dirty_roots]
    table_a_rows = _known_rows(table_a["known_size"])
    frame_size = frame.get("known_size")
    returned_rows = _known_rows(frame_size)
    if not isinstance(frame_size, dict) or "bytes" not in frame_size:
        raise AssertionError("actual bounded DataFrame transfer bytes are unavailable")
    transfer_bytes = frame_size["bytes"]
    if type(transfer_bytes) is not int or not 0 <= transfer_bytes <= 67108864:
        raise AssertionError("actual bounded DataFrame transfer bytes are invalid")
    schema_a = table_a.get("schema")
    schema_b = table_b.get("schema")
    if (
        not isinstance(schema_a, list)
        or not schema_a
        or not isinstance(schema_b, list)
        or not schema_b
    ):
        raise AssertionError("temporary-table schema metadata is unavailable")
    variables = locals_view.get("variables")
    if not isinstance(variables, list):
        raise AssertionError("local descriptor page is unavailable")
    if (
        not isinstance(selected_locals, tuple)
        or len(selected_locals) != len(_CAPTURE_A_LOCAL_NAMES)
        or not isinstance(old_proxy_ids, tuple)
    ):
        raise AssertionError("exact selected-local/proxy evidence is unavailable")
    frontend_a = first["frontend_identity"]
    frontend_b = second["frontend_identity"]
    if not isinstance(frontend_a, ProcessIdentity) or not isinstance(
        frontend_b, ProcessIdentity
    ):
        raise AssertionError("frontend exact identities are unavailable")
    main_operation = mcp_operation_id(first["captured"])
    old_proxy_hashes = [
        evidence_identity(_SALT, str(proxy_id)) for proxy_id in old_proxy_ids
    ]
    return {
        "schema": "onec-agent-capture-live-observations-v1",
        "attempt": _ATTEMPT,
        "result": "PASS",
        "call_sequence": list(calls),
        "call_metrics": {
            "automatic_hash_calls": sum("value.hash" in call for call in calls),
            "full_materialization_calls": sum(
                "value.materialize" in call for call in calls
            ),
        },
        "code": {
            "main_cell_id": main["cell_id"],
            "main_revision": main["revision"],
            "main_source_sha256": main["source_sha256"],
            "hypothesis_cell_id": hypothesis_ref["cell_id"],
            "hypothesis_revision": hypothesis_ref["revision"],
            "hypothesis_source_sha256": hypothesis_ref["source_sha256"],
        },
        "frontend": {
            "sessions": ["A", "B"],
            "replacement_count": int(frontend_a != frontend_b),
            "frontend_a_identity_sha256": process_identity_sha256(
                _ATTEMPT, frontend_a.private_wire()
            ),
            "frontend_b_identity_sha256": process_identity_sha256(
                _ATTEMPT, frontend_b.private_wire()
            ),
            "frontend_a_state_before_b": first["frontend_state_before_b"],
            "replayed_operation_identity_sha256": evidence_identity(
                _SALT, mcp_operation_id(replay)
            ),
        },
        "capture_a": _capture_public(
            replay, stop_binding=first["stop_binding"]
        ),
        "locals": {
            "selected_count": len(selected_locals),
            "proxy_count": sum(bool(item.get("proxy_id")) for item in selected_locals),
            "selected_name_sha256s": [
                evidence_identity(_SALT, str(item["name"]).casefold())
                for item in selected_locals
            ],
            "budget": _budget("agent_metadata"),
        },
        "manager": {
            "origin": "frame_local",
            "alias": "manager_a",
            "origin_local_name_sha256": evidence_identity(
                _SALT, "МенеджерВременныхТаблиц".casefold()
            ),
            "table_name_sha256": evidence_identity(_SALT, _TABLE_NAME.casefold()),
            "manager_identity_sha256": evidence_identity(
                _SALT, str(manager_a["manager_id"])
            ),
            "table_count": second["manager_a_table_count"],
        },
        "table_a": {
            "alias": "table_a",
            "table_name_sha256": evidence_identity(_SALT, _TABLE_NAME.casefold()),
            "manager_identity_sha256": evidence_identity(
                _SALT, str(manager_a["manager_id"])
            ),
            "proxy_identity_sha256": evidence_identity(
                _SALT, str(table_a["table_id"])
            ),
            "known_size": table_a_rows,
            "schema_column_count": len(schema_a),
            "automatic_hash_calls": 0,
            "full_materialization_calls": 0,
            "head": {
                "requested_rows": 5,
                "returned_rows": returned_rows,
                "returned_columns": len(schema_a),
            "transfer_bytes": transfer_bytes,
                "dataframe_identity_sha256": evidence_identity(
                    _SALT, str(frame["proxy_id"])
                ),
            },
            "budget": _budget("agent_dataframe"),
        },
        "hypothesis": {
            "operation_identity_sha256": evidence_identity(
                _SALT, mcp_operation_id(hypothesis)
            ),
            "operation_state": hypothesis["state"],
            "observation_state": "partial",
            "failure_stage": hypothesis["failure"]["stage"],
            "capture_state_before": replay["state"],
            "capture_state_after": hypothesis["state"],
            "capture_generation_before": replay["capture"]["fence"][
                "capture_generation"
            ],
            "capture_generation_after": hypothesis["capture"]["fence"][
                "capture_generation"
            ],
            "dirty_root_count": len(dirty_roots),
        },
        "continuation": {
            "operation_identity_sha256": evidence_identity(
                _SALT, mcp_operation_id(continued)
            ),
            "call_count": 1,
            "continue_state": "acknowledged",
            "dirty_root_name_sha256s": roots,
            # A captured successor is admitted only after the production
            # continuation evidence revalidates this exact ordered sequence.
            "acknowledged_root_name_sha256s": list(roots),
            "old_proxy_check_count": second["stale_checked"],
            "old_proxy_stale_count": second["stale_count"],
            "old_proxy_identity_sha256s": old_proxy_hashes,
            "stale_proxy_identity_sha256s": list(old_proxy_hashes),
            "old_proxy_capture_generation": 1,
        },
        "capture_b": _capture_public(
            continued, stop_binding=second["stop_binding"]
        ),
        "downstream": {
            "manager_alias": "manager_b",
            "table_alias": "table_b",
            "result_local_name_sha256": evidence_identity(
                _SALT, _DOWNSTREAM_RESULT_LOCAL.casefold()
            ),
            "table_name_sha256": evidence_identity(_SALT, _TABLE_NAME.casefold()),
            "table_proxy_identity_sha256": evidence_identity(
                _SALT, str(table_b["table_id"])
            ),
            "result_proxy_identity_sha256": evidence_identity(
                _SALT, str(result_b["proxy_id"])
            ),
            "selected_rows": _known_rows(table_b["known_size"]),
            "requested_rows": 5,
            "selected_columns": len(schema_b),
            "automatic_hash_calls": 0,
            "full_materialization_calls": 0,
            "budget": _budget("agent_dataframe"),
        },
        "terminal": {
            "origin_main_operation_identity_sha256": evidence_identity(
                _SALT, main_operation
            ),
            "continuation_operation_identity_sha256": evidence_identity(
                _SALT, mcp_operation_id(continued)
            ),
            "terminal_operation_identity_sha256": evidence_identity(
                _SALT, mcp_operation_id(terminal)
            ),
            "finish_continue_call_count": 1,
            "origin_capture_generation": 2,
            "state": terminal["state"],
            "capture_present": terminal.get("capture") is not None,
            "next_event_cursor": terminal["next_event_cursor"],
        },
    }


def _cleanup_public(
    *,
    owned: tuple[ProcessIdentity, ...],
    unrelated: tuple[ProcessIdentity, ...],
    errors: list[str],
    runtime_owner_state: str,
) -> dict[str, object]:
    return {
        "schema": "onec-agent-capture-live-cleanup-v1",
        "attempt": _ATTEMPT,
        "runtime_owner_state": runtime_owner_state,
        "owned": [
            {
                "role": item.role,
                "identity_sha256": process_identity_sha256(
                    _ATTEMPT, item.private_wire()
                ),
                "state": process_identity_state(item).value,
            }
            for item in owned
        ],
        "unrelated": [
            {
                "identity_sha256": process_identity_sha256(
                    _ATTEMPT, item.private_wire()
                ),
                "state": process_identity_state(item).value,
            }
            for item in unrelated
        ],
        "errors": errors,
    }


def _capture_private_fixture(
    *,
    username: str,
    platform_bin: Path,
    infobase: Path,
    zup_source_root: Path,
    python_executable: Path,
    target_database_identity: process_support.CanonicalDatabaseIdentity,
    unrelated: tuple[ProcessIdentity, ...],
    preflight: Mapping[str, object],
    main: Mapping[str, object],
    hypothesis: Mapping[str, object],
    point_a_binding: Mapping[str, object],
    point_b_binding: Mapping[str, object],
) -> dict[str, object]:
    roots = {
        "workspace": str(_WORKSPACE),
        "platform_bin": str(Path(platform_bin).resolve()),
        "infobase": str(Path(infobase).resolve()),
        "source_root": str(Path(zup_source_root).resolve()),
        "python": str(Path(python_executable).resolve()),
    }
    platform = preflight["platform"]
    source = preflight["source"]
    notebook = preflight["notebook"]
    implementation = preflight["implementation"]
    if not all(
        isinstance(item, Mapping)
        for item in (platform, source, notebook, implementation)
    ):
        raise AssertionError("private replay preflight is invalid")
    return {
        "schema": "onec-agent-capture-private-fixture-v1",
        "attempt": 2,
        "authorization": {"capture_attempt": "2", "live_flag": "1"},
        "paths": {"public_leaf": "attempt-2", "private_leaf": "attempt-2"},
        "invocation": {
            "profile": "capture",
            "maximum_mode": "experiment",
            "mutation_requested": False,
            "username": username,
            "roots": roots,
            "request_ids": {
                "main": _attempt_request_id("main"),
                "hypothesis": _attempt_request_id("hypothesis"),
                "continue_b": _attempt_request_id("continue-b"),
                "finish_main": _attempt_request_id("finish-main"),
            },
            "timeouts": {
                "overall_timeout_s": _ATTEMPT_TIMEOUT_S,
                "sdk_outer_timeout_s": _SDK_CALL_TIMEOUT_S,
                "control_timeout_s": _CONTROL_TIMEOUT_S,
                "schema_wait_max_s": _MCP_SCHEMA_WAIT_MAX_S,
            },
        },
        "target_database": {
            "filesystem_key": target_database_identity.filesystem_key,
            "size": target_database_identity.size,
        },
        "unrelated_processes": [item.private_wire() for item in unrelated],
        "private_markers": [
            {"kind": "username", "value": username},
            {"kind": "workspace", "value": roots["workspace"]},
            {"kind": "platform_bin", "value": roots["platform_bin"]},
            {"kind": "infobase", "value": roots["infobase"]},
            {"kind": "source_root", "value": roots["source_root"]},
            {"kind": "discovery_source", "value": PAYROLL_DISCOVERY_SOURCE},
            {"kind": "main_source", "value": PAYROLL_MAIN_SOURCE},
            {"kind": "hypothesis_source", "value": CAPTURE_A_SNAPSHOT_SOURCE},
        ],
        "preflight": dict(preflight),
        "bindings": {
            "platform_sha256": platform.get("bin_identity_sha256"),
            "source_sha256": source.get("tree_sha256"),
            "notebook_sha256": notebook.get("artifact_sha256"),
            "implementation_sha256": implementation.get("runtime_tree_sha256"),
        },
        "pass_contract": {
            "call_sequence": list(CAPTURE_PASS_CALL_SEQUENCE),
            "main": dict(main),
            "hypothesis": dict(hypothesis),
            "stop_bindings": {
                "capture_a": dict(point_a_binding),
                "capture_b": dict(point_b_binding),
            },
            "capture_a_local_names": list(_CAPTURE_A_LOCAL_NAMES),
            "table_name": _TABLE_NAME,
            "downstream_result_local": _DOWNSTREAM_RESULT_LOCAL,
            "budgets": {
                "agent_metadata": _budget("agent_metadata"),
                "agent_dataframe": _budget("agent_dataframe"),
            },
        },
    }


def _complete_capture_private_replay(
    journal: CapturePrivateJournal,
    *,
    cleanup: Mapping[str, object],
    postflight: Mapping[str, object],
    observations: Mapping[str, object],
    raw_error_type: str | None,
    target_database_process_count: int | None,
    control_token_state: str,
    control_descriptor_state: str,
) -> None:
    ledger_path = journal.private_attempt / "owned-processes.json"
    try:
        ledger_payload = ledger_path.read_bytes()
    except OSError as error:
        raise AssertionError("private owned-process ledger is unavailable") from error
    journal.append(
        "cleanup",
        {
            "owned_ledger": {
                "path": "owned-processes.json",
                "sha256": sha256(ledger_payload).hexdigest(),
            },
            "owned": cleanup["owned"],
            "unrelated": cleanup["unrelated"],
            "errors": cleanup["errors"],
            "runtime_owner_state": cleanup["runtime_owner_state"],
            "target_database_process_count": target_database_process_count,
            "control_token_state": control_token_state,
            "control_descriptor_state": control_descriptor_state,
        },
    )
    journal.append("postflight", {"snapshots": dict(postflight)})
    result = observations.get("result")
    if result == "PASS":
        outcome = {"result": "PASS", "observations": dict(observations)}
    elif result == "FAIL":
        failure = observations.get("failure")
        if not isinstance(failure, Mapping) or not isinstance(raw_error_type, str):
            raise AssertionError("private FAIL outcome is unavailable")
        outcome = {
            "result": "FAIL",
            "failure": {
                "boundary": failure["boundary"],
                "public_type": failure["type"],
                "raw_error_type": raw_error_type,
                "last_completed_phase": failure["last_completed_phase"],
            },
        }
    else:
        raise AssertionError("private replay result is unavailable")
    journal.append("outcome", outcome)
    journal.append("complete", {"result": result})


def _remove_control_credentials(
    private_attempt: Path, control_descriptor: Path
) -> None:
    private_attempt = Path(private_attempt).resolve()
    expected_descriptor = private_attempt / "control" / "endpoint.json"
    descriptor = Path(control_descriptor).resolve()
    if descriptor != expected_descriptor:
        raise AssertionError("control credential path is outside private attempt")
    token = descriptor.with_name("token")
    token.unlink(missing_ok=True)
    descriptor.unlink(missing_ok=True)
    if token.exists() or descriptor.exists():
        raise AssertionError("control credentials remain after cleanup")


def _path_presence_state(path: Path) -> str:
    try:
        path.stat()
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unknown"
    return "alive"


@pytest.mark.integration
def test_mcp_zup_capture_live_two_point_acceptance() -> None:
    """One guarded attempt 2; offline preparation never enables this test."""
    if os.environ.get(_LIVE_FLAG) != "1":
        pytest.skip(f"set {_LIVE_FLAG}=1 only after explicit Phase B authorization")
    authorization = _require_attempt_two_authorization(_WORKSPACE, os.environ)
    paths = authorization.paths
    owner_path = authorization.owner_path
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
    if os.environ.get("ONEC_RUNTIME_PASSWORD"):
        raise AssertionError("the approved passwordless acceptance must not set a password")
    platform_bin = Path(required["ONEC_RUNTIME_PLATFORM_BIN"]).resolve(strict=True)
    infobase = Path(required["ONEC_RUNTIME_INFOBASE"]).resolve(strict=True)
    zup_source_root = Path(required["ONEC_ZUP_SOURCE_ROOT"]).resolve(strict=True)
    if platform_bin.parent.name != "8.3.27.2170":
        raise AssertionError("the approved platform version is not selected")
    if not (infobase / "1Cv8.1CD").is_file():
        raise AssertionError("the approved dedicated file infobase is unavailable")
    control_descriptor = paths.private / "control" / "endpoint.json"
    target_database_identity = process_support.canonical_database_identity(infobase)
    process_support.assert_target_database_not_open(infobase)
    unrelated = _snapshot_unrelated_processes()
    preflight = _snapshot(
        platform_bin=platform_bin, zup_source_root=zup_source_root
    )
    main, hypothesis_ref = _notebook_revisions()
    source_points = locate_payroll_capture_points(zup_source_root)
    point_a = _capture_point(
        "capture_a",
        line=source_points.before_limit.line,
        procedure="СоздатьВТЗарплатаКВыплате",
    )
    point_b = _capture_point(
        "capture_b",
        line=source_points.after_cascade.line,
        procedure="ЗарплатаКВыплате",
    )
    point_a_binding = _stop_binding(source_points.before_limit, point=point_a)
    point_b_binding = _stop_binding(source_points.after_cascade, point=point_b)
    private_journal = create_capture_private_replay(
        paths.private,
        fixture=_capture_private_fixture(
            username=required["ONEC_RUNTIME_USERNAME"],
            platform_bin=platform_bin,
            infobase=infobase,
            zup_source_root=zup_source_root,
            python_executable=Path(sys.executable).resolve(strict=True),
            target_database_identity=target_database_identity,
            unrelated=unrelated,
            preflight=preflight,
            main=main,
            hypothesis=hypothesis_ref,
            point_a_binding=point_a_binding,
            point_b_binding=point_b_binding,
        ),
    )
    private_journal.append("stage", {"name": "preflight_complete"})
    (paths.private / "owned-processes.json").write_text(
        json.dumps({"attempt": 2, "owned": []}, ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    raw_dir = paths.private / "raw-mcp"
    tracker: OwnedProcessTracker | None = None
    calls: list[str] = []
    raw_index = [0]
    cleanup_errors: list[str] = []
    service_process: subprocess.Popen[bytes] | None = None
    raw_process_handles: list[subprocess.Popen[bytes]] = []
    cleanup_client: ServiceClient | None = None
    shutdown_client: ServiceClient | None = None
    failure: BaseException | None = None
    first: dict[str, Any] | None = None
    second: dict[str, Any] | None = None
    runtime_closed = False
    attempt_deadline = monotonic() + _ATTEMPT_TIMEOUT_S
    private_markers = [
        required["ONEC_RUNTIME_USERNAME"],
        str(_WORKSPACE),
        str(platform_bin),
        str(infobase),
        str(zup_source_root),
        PAYROLL_DISCOVERY_SOURCE,
        PAYROLL_MAIN_SOURCE,
        CAPTURE_A_SNAPSHOT_SOURCE,
    ]
    raw_private_markers: tuple[str, ...] = ()
    control_token: str | None = None
    env = os.environ | {
        "PYTHONPATH": str(_WORKSPACE / "src"),
        "ONEC_RUNTIME_EVIDENCE_DIR": str(paths.private / "runtime-evidence"),
    }
    try:
        private_journal.append("stage", {"name": "launch_reserved"})
        tracker = OwnedProcessTracker(
            paths.private / "owned-processes.json",
            attempt=_ATTEMPT,
            policy=ApprovedExecutablePolicy(
                python_executable=str(Path(sys.executable).resolve(strict=True)),
                platform_bin=str(platform_bin),
            ),
        )
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
                str(control_descriptor),
            ],
            cwd=_WORKSPACE,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # The raw OS handle is owned immediately; identity adoption may fail.
        raw_process_handles.append(service_process)
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
                str(control_descriptor),
            ),
        )
        deadline = monotonic() + 10
        while monotonic() < deadline and not control_descriptor.is_file():
            if service_process.poll() is not None:
                raise AssertionError("agent service exited before descriptor publication")
            sleep(0.05)
        endpoint = ControlEndpoint.read(control_descriptor)
        token_payload = json.loads(endpoint.token_path.read_text(encoding="utf-8"))
        if (
            not isinstance(token_payload, dict)
            or set(token_payload) != {"token"}
            or not isinstance(token_payload["token"], str)
            or not token_payload["token"]
        ):
            raise AssertionError("owned control token is invalid")
        control_token = token_payload["token"]
        private_markers.extend(
            (
                endpoint.service_instance_id,
                control_token,
            )
        )
        private_journal.append(
            "private_marker",
            {"kind": "service_instance_id", "value": endpoint.service_instance_id},
        )
        raw_private_markers = tuple(private_markers)
        cleanup_client = ServiceClient(endpoint, caller_id="mcp-capture-b", timeout_s=5)
        shutdown_client = ServiceClient(endpoint, caller_id="capture-cleanup", timeout_s=5)
        common = _frontend_environment(control_descriptor)
        first_parameters = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "onec_runtime_mcp.mcp_entrypoint",
                "--workspace",
                str(_WORKSPACE),
                "--profile",
                "capture",
            ],
            cwd=_WORKSPACE,
            env=common | {"ONEC_RUNTIME_CALLER_ID": "mcp-capture-a"},
        )
        second_parameters = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "onec_runtime_mcp.mcp_entrypoint",
                "--workspace",
                str(_WORKSPACE),
                "--profile",
                "capture",
            ],
            cwd=_WORKSPACE,
            env=common | {"ONEC_RUNTIME_CALLER_ID": "mcp-capture-b"},
        )
        first = asyncio.run(
            _frontend_a_capture(
                first_parameters,
                attempt_deadline=attempt_deadline,
                private_markers=private_markers,
                raw_private_markers=raw_private_markers,
                external_pids=tuple(item.pid for item in unrelated),
                tracker=tracker,
                calls=calls,
                raw_index=raw_index,
                raw_dir=raw_dir,
                private_journal=private_journal,
                main=main,
                hypothesis=hypothesis_ref,
                point_a=point_a,
            )
        )
        first_frontend = first.get("frontend_identity")
        if not isinstance(first_frontend, ProcessIdentity):
            raise AssertionError("frontend A identity was not captured")
        frontend_a_state = process_identity_state(first_frontend).value
        first["frontend_state_before_b"] = frontend_a_state
        first["stop_binding"] = point_a_binding
        if frontend_a_state != ProcessState.ABSENT.value:
            raise AssertionError("frontend A is not exactly absent before B")
        private_journal.append(
            "stage",
            {"name": "A_frontend_exit", "public_label": "A:frontend.exit"},
        )
        calls.append("A:frontend.exit")
        second = asyncio.run(
            _frontend_b_capture(
                second_parameters,
                attempt_deadline=attempt_deadline,
                private_markers=private_markers,
                raw_private_markers=raw_private_markers,
                external_pids=tuple(item.pid for item in unrelated),
                tracker=tracker,
                calls=calls,
                raw_index=raw_index,
                raw_dir=raw_dir,
                private_journal=private_journal,
                first=first,
                hypothesis_ref=hypothesis_ref,
                point_b=point_b,
            )
        )
        second["stop_binding"] = point_b_binding
        runtime_closed = True
        shutdown_client.shutdown()
        private_journal.append(
            "stage",
            {
                "name": "owner_service_shutdown",
                "public_label": "owner:service.shutdown",
            },
        )
        calls.append("owner:service.shutdown")
        if service_process.wait(timeout=10) != 0:
            raise AssertionError("agent service returned a non-zero status")
    except BaseException as error:
        failure = error
    finally:
        if service_process is not None and service_process.poll() is None:
            if not runtime_closed and cleanup_client is not None:
                try:
                    response = cleanup_client.call(
                        "runtime.close", {"policy": "abort_generation"}
                    )
                    runtime_closed = response.ok
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
                cleanup_errors.append("ServiceShutdownTimeout")
        if tracker is not None:
            cleanup_errors.extend(tracker.stop())
            for identity in reversed(tracker.identities()):
                try:
                    terminate_exact_process(identity)
                except BaseException as error:
                    cleanup_errors.append(type(error).__name__)
        for handle in reversed(raw_process_handles):
            try:
                terminate_owned_popen(handle, timeout_s=5.0)
            except BaseException as error:
                cleanup_errors.append(type(error).__name__)
        try:
            _remove_control_credentials(paths.private, control_descriptor)
        except BaseException as error:
            cleanup_errors.append(type(error).__name__)

    control_token_state = _path_presence_state(control_descriptor.with_name("token"))
    control_descriptor_state = _path_presence_state(control_descriptor)
    if control_token_state != "absent":
        cleanup_errors.append(
            "ControlTokenCleanupUnverifiable"
            if control_token_state == "unknown"
            else "ControlTokenPresent"
        )
        if failure is None:
            failure = AssertionError("control token remains after exact cleanup")
    if control_descriptor_state != "absent":
        cleanup_errors.append(
            "ControlDescriptorCleanupUnverifiable"
            if control_descriptor_state == "unknown"
            else "ControlDescriptorPresent"
        )
        if failure is None:
            failure = AssertionError("control descriptor remains after exact cleanup")
    all_owned = tracker.identities() if tracker is not None else ()
    if failure is None:
        selected: list[ProcessIdentity] = []
        for role in CAPTURE_OWNED_ROLES:
            matches = [item for item in all_owned if item.role == role]
            if len(matches) != 1:
                failure = AssertionError(
                    f"owned role count is not exact for {role}"
                )
                break
            selected.append(matches[0])
        owned = tuple(selected) if failure is None else all_owned
    else:
        owned = all_owned
    runtime_owner_state = _path_presence_state(owner_path)
    if runtime_owner_state != "absent":
        cleanup_errors.append(
            "RuntimeOwnerCleanupUnverifiable"
            if runtime_owner_state == "unknown"
            else "RuntimeOwnerPresent"
        )
        if failure is None:
            failure = AssertionError("runtime owner cleanup is not verifiably absent")
    target_database_process_count: int | None
    try:
        target_database_process_count = (
            process_support.target_database_process_count(infobase)
        )
    except BaseException as error:
        target_database_process_count = None
        cleanup_errors.append("TargetDatabaseProcessCountUnverifiable")
        if failure is None:
            failure = error
    if target_database_process_count:
        cleanup_errors.append("TargetDatabaseProcessPresent")
        if failure is None:
            failure = AssertionError("target database remains open after cleanup")
    if failure is None and cleanup_errors:
        failure = AssertionError("exact cleanup recorded errors")
    try:
        _assert_attempt_one_unchanged(authorization)
    except BaseException as error:
        if failure is None:
            failure = error
    snapshot_failure: capture_support.SnapshotInputFailure | None = None
    try:
        postflight = _snapshot(
            platform_bin=platform_bin, zup_source_root=zup_source_root
        )
    except capture_support.SnapshotInputFailure as error:
        snapshot_failure = error
        postflight = capture_support.unavailable_snapshot_postflight(
            preflight, error
        )
        failure = error
    drift_names = tuple(
        name for name, value in preflight.items() if value != postflight[name]
    )
    if drift_names and snapshot_failure is None:
        failure = SnapshotDrift("_".join(drift_names))
    environment = {
        "schema": "onec-agent-capture-live-environment-v1",
        "attempt": _ATTEMPT,
        "profile": "capture",
        "maximum_mode": "experiment",
        "database": {
            "target_identity_sha256": process_support.database_identity_sha256(
                target_database_identity
            ),
            "mutation_requested": False,
        },
        "snapshots": {
            name: {"pre": value, "post": postflight[name]}
            for name, value in preflight.items()
        },
    }
    cleanup = _cleanup_public(
        owned=owned,
        unrelated=unrelated,
        errors=cleanup_errors,
        runtime_owner_state=runtime_owner_state,
    )
    expected_result = (
        ExpectedCaptureResult.PASS
        if failure is None
        else ExpectedCaptureResult.FAIL
    )
    if failure is None:
        assert first is not None and second is not None
        observations = _pass_observations(
            calls=calls,
            main=main,
            hypothesis_ref=hypothesis_ref,
            first=first,
            second=second,
        )
    else:
        next_label = (
            CAPTURE_PASS_CALL_SEQUENCE[len(calls)]
            if len(calls) < len(CAPTURE_PASS_CALL_SEQUENCE)
            else "postflight"
        )
        is_snapshot_drift = isinstance(failure, SnapshotDrift)
        is_snapshot_input_failure = isinstance(
            failure, capture_support.SnapshotInputFailure
        )
        observations = {
            "schema": "onec-agent-capture-live-observations-v1",
            "attempt": _ATTEMPT,
            "result": "FAIL",
            "call_sequence": list(calls),
            "failure": {
                "boundary": (
                    "snapshot_drift_" + "_".join(drift_names)
                    if is_snapshot_drift
                    else (
                        f"snapshot_{snapshot_failure.state}_{snapshot_failure.stage}"
                        if is_snapshot_input_failure
                        and snapshot_failure is not None
                        else ("before_" + next_label).replace(":", "_").replace(
                            ".", "_"
                        )
                    )
                ),
                "type": (
                    "SnapshotInputFailure"
                    if is_snapshot_input_failure
                    else type(failure).__name__
                ),
                "last_completed_phase": (
                    "postflight"
                    if is_snapshot_drift or is_snapshot_input_failure
                    else (
                        "preflight"
                        if not calls
                        else calls[-1].replace(":", "_").replace(".", "_")
                    )
                ),
            },
        }
    _complete_capture_private_replay(
        private_journal,
        cleanup=cleanup,
        postflight=postflight,
        observations=observations,
        raw_error_type=None if failure is None else type(failure).__name__,
        target_database_process_count=target_database_process_count,
        control_token_state=control_token_state,
        control_descriptor_state=control_descriptor_state,
    )
    if target_database_process_count != 0:
        if failure is not None:
            raise failure
        raise AssertionError("target database cleanup is not publishable")
    verified = publish_capture_private_replay(
        paths.public,
        private_attempt=paths.private,
        environment=environment,
        observations=observations,
        cleanup=cleanup,
        ephemeral_private_markers=(
            () if control_token is None else (control_token,)
        ),
    )
    assert verified["status"] == expected_result.value
    if failure is not None:
        raise failure


class _FakeProcess:
    def __init__(self, identity: ProcessIdentity, *, create_time: float | None = None) -> None:
        self.pid = identity.pid
        self._created = identity.create_time if create_time is None else create_time
        self._executable = identity.executable
        self.terminated = False

    def create_time(self) -> float:
        return self._created

    def exe(self) -> str:
        return self._executable

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float) -> None:
        assert timeout > 0

    def kill(self) -> None:
        raise AssertionError("the exact process exited after terminate")


def test_exact_process_cleanup_is_pid_reuse_safe_and_tri_state() -> None:
    identity = ProcessIdentity(
        "onec",
        42001,
        1234.5,
        r"C:\Program Files\1cv8\1cv8c.exe",
    )
    live = _FakeProcess(identity)
    state = {"present": True}

    def exact_factory(pid: int):  # type: ignore[no-untyped-def]
        assert pid == identity.pid
        if not state["present"]:
            raise psutil.NoSuchProcess(pid)
        if live.terminated:
            state["present"] = False
            raise psutil.NoSuchProcess(pid)
        return live

    assert open_process_identity(identity, process_factory=exact_factory)[0] is ProcessState.ALIVE
    terminate_exact_process(identity, process_factory=exact_factory)
    assert live.terminated is True
    assert open_process_identity(identity, process_factory=exact_factory)[0] is ProcessState.ABSENT

    reused = _FakeProcess(identity, create_time=identity.create_time + 1)
    terminate_exact_process(identity, process_factory=lambda _pid: reused)
    assert reused.terminated is False

    def denied(_pid: int):  # type: ignore[no-untyped-def]
        raise psutil.AccessDenied(identity.pid)

    assert open_process_identity(identity, process_factory=denied)[0] is ProcessState.UNKNOWN
    assert identity_states((identity,), process_factory=lambda _pid: reused) == (
        ProcessState.ABSENT,
    )


class _ObservedProcess:
    def __init__(
        self,
        *,
        pid: int,
        executable: str,
        command: tuple[str, ...],
        parent_pid: int = 0,
        name: str | None = None,
        cwd: str | None = None,
    ) -> None:
        self.pid = pid
        self._executable = executable
        self._command = command
        self._parent_pid = parent_pid
        self._name = name or Path(executable).name
        self._cwd = cwd

    def create_time(self) -> float:
        return 1000.0 + self.pid

    def exe(self) -> str:
        return self._executable

    def cmdline(self) -> list[str]:
        return list(self._command)

    def ppid(self) -> int:
        return self._parent_pid

    def name(self) -> str:
        return self._name

    def cwd(self) -> str:
        if self._cwd is None:
            raise psutil.AccessDenied(self.pid)
        return self._cwd


def test_approved_executable_policy_rejects_roots_and_descendants_outside_exact_roots(
    tmp_path: Path,
) -> None:
    python_exe = tmp_path / "python" / "python.exe"
    platform_bin = tmp_path / "platform" / "bin"
    policy = process_support.ApprovedExecutablePolicy(
        python_executable=str(python_exe.resolve()),
        platform_bin=str(platform_bin.resolve()),
    )
    service = _ObservedProcess(
        pid=5001,
        executable=str(python_exe.resolve()),
        command=(
            str(python_exe.resolve()),
            "-m",
            "onec_runtime_mcp.agent.service_entrypoint",
        ),
    )
    root = process_support.adopt_owned_root(
        service,
        role="service",
        policy=policy,
        required_cmdline=("onec_runtime_mcp.agent.service_entrypoint",),
    )
    assert root.pid == service.pid

    outside = _ObservedProcess(
        pid=5002,
        executable=str((tmp_path / "outside" / "dbgs.exe").resolve()),
        command=("dbgs.exe",),
        parent_pid=root.pid,
        name="dbgs.exe",
    )
    with pytest.raises(AssertionError, match="approved platform root"):
        process_support.adopt_owned_descendant(
            outside, parent=root, policy=policy
        )

    wrong_parent = _ObservedProcess(
        pid=5003,
        executable=str((platform_bin / "dbgs.exe").resolve()),
        command=("dbgs.exe",),
        parent_pid=9999,
        name="dbgs.exe",
    )
    with pytest.raises(AssertionError, match="parent"):
        process_support.adopt_owned_descendant(
            wrong_parent, parent=root, policy=policy
        )


def test_root_adoption_fails_closed_when_executable_or_cmdline_is_unverifiable(
    tmp_path: Path,
) -> None:
    python_exe = str((tmp_path / "python.exe").resolve())
    policy = process_support.ApprovedExecutablePolicy(
        python_executable=python_exe,
        platform_bin=str((tmp_path / "platform").resolve()),
    )

    class Denied(_ObservedProcess):
        def cmdline(self) -> list[str]:
            raise psutil.AccessDenied(self.pid)

    denied = Denied(pid=5101, executable=python_exe, command=())
    with pytest.raises(AssertionError, match="unverifiable"):
        process_support.adopt_owned_root(
            denied,
            role="mcp_a",
            policy=policy,
            required_cmdline=("onec_runtime_mcp.mcp_entrypoint",),
        )

    wrong_exe = _ObservedProcess(
        pid=5102,
        executable=str((tmp_path / "other-python.exe").resolve()),
        command=("onec_runtime_mcp.mcp_entrypoint",),
    )
    with pytest.raises(AssertionError, match="Python executable"):
        process_support.adopt_owned_root(
            wrong_exe,
            role="mcp_a",
            policy=policy,
            required_cmdline=("onec_runtime_mcp.mcp_entrypoint",),
        )


def test_database_identity_is_alias_stable_and_preflight_is_exact_fail_closed(
    tmp_path: Path,
) -> None:
    first = tmp_path / "base-a"
    second = tmp_path / "base-b"
    first.mkdir()
    second.mkdir()
    database = first / "1Cv8.1CD"
    database.write_bytes(b"offline-test-database-identity")
    os.link(database, second / "1Cv8.1CD")
    identity = process_support.canonical_database_identity(first)
    alias = process_support.canonical_database_identity(second)
    assert identity.filesystem_key == alias.filesystem_key
    assert process_support.database_identity_sha256(identity) == (
        process_support.database_identity_sha256(alias)
    )

    exact = _ObservedProcess(
        pid=5201,
        executable=str((tmp_path / "1cv8c.exe").resolve()),
        command=("1cv8c.exe", "/F", str(second)),
        name="1cv8c.exe",
    )
    assert process_support.target_database_process_count(
        first, processes=(exact,)
    ) == 1
    with pytest.raises(AssertionError, match="already open"):
        process_support.assert_target_database_not_open(first, processes=(exact,))

    unbound = _ObservedProcess(
        pid=5202,
        executable=str((tmp_path / "1cv8c.exe").resolve()),
        command=("1cv8c.exe", "/SomeOption", str(first)),
        name="1cv8c.exe",
    )
    with pytest.raises(AssertionError, match="unverifiable"):
        process_support.assert_target_database_not_open(
            first, processes=(unbound,)
        )

    class Denied(_ObservedProcess):
        def cmdline(self) -> list[str]:
            raise psutil.AccessDenied(self.pid)

    with pytest.raises(AssertionError, match="unverifiable"):
        process_support.assert_target_database_not_open(
            first,
            processes=(
                Denied(
                    pid=5203,
                    executable=str((tmp_path / "1cv8.exe").resolve()),
                    command=(),
                    name="1cv8.exe",
                ),
            ),
        )


@pytest.mark.parametrize(
    "command",
    [
        ("1cv8c.exe",),
        ("1cv8c.exe", "/S", "server\\base"),
        ("1cv8c.exe", "/IBName", "registered-base"),
        ("1cv8c.exe", "/IBConnectionString", "Srvr=server;Ref=base"),
        ("1cv8c.exe", "/F"),
    ],
)
def test_database_preflight_refuses_any_relevant_process_without_provably_different_file_base(
    tmp_path: Path, command: tuple[str, ...]
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "1Cv8.1CD").write_bytes(b"target")
    observed = _ObservedProcess(
        pid=5210,
        executable=str((tmp_path / "1cv8c.exe").resolve()),
        command=command,
        name="1cv8c.exe",
    )

    with pytest.raises(AssertionError, match="unverifiable"):
        process_support.assert_target_database_not_open(
            target, processes=(observed,)
        )


def test_database_preflight_resolves_relative_file_base_against_observed_process_cwd(
    tmp_path: Path,
) -> None:
    observed_cwd = tmp_path / "observed-cwd"
    target = observed_cwd / "target"
    different = observed_cwd / "different"
    target.mkdir(parents=True)
    different.mkdir()
    (target / "1Cv8.1CD").write_bytes(b"target")
    (different / "1Cv8.1CD").write_bytes(b"different")

    exact = _ObservedProcess(
        pid=5220,
        executable=str((tmp_path / "1cv8c.exe").resolve()),
        command=("1cv8c.exe", '/F"target"'),
        name="1cv8c.exe",
        cwd=str(observed_cwd),
    )
    with pytest.raises(AssertionError, match="already open"):
        process_support.assert_target_database_not_open(target, processes=(exact,))

    unrelated = _ObservedProcess(
        pid=5221,
        executable=str((tmp_path / "1cv8.exe").resolve()),
        command=("1cv8.exe", '-F"different"'),
        name="1cv8.exe",
        cwd=str(observed_cwd),
    )
    process_support.assert_target_database_not_open(
        target, processes=(unrelated,)
    )


def test_database_preflight_refuses_relative_file_base_when_process_cwd_is_denied(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "1Cv8.1CD").write_bytes(b"target")
    observed = _ObservedProcess(
        pid=5230,
        executable=str((tmp_path / "1cv8.exe").resolve()),
        command=("1cv8.exe", "/F", "some-relative-base"),
        name="1cv8.exe",
    )

    with pytest.raises(AssertionError, match="unverifiable"):
        process_support.assert_target_database_not_open(
            target, processes=(observed,)
        )


@pytest.mark.parametrize(
    "command",
    [
        ("1cv8c.exe", "/Force"),
        ("1cv8c.exe", '/F"different'),
        ("1cv8c.exe", "/F", '"different'),
        ("1cv8c.exe", "/F", "-Force"),
        ("1cv8c.exe", "/F", "different", "/F", "different"),
        ("1cv8c.exe", "/F", "different", "/F", "other"),
        ("1cv8c.exe", "/F", "different", "/IBName", "registered"),
        ("1cv8c.exe", "/F", "different", "/S", "server\\base"),
        ("1cv8c.exe", "/F=different"),
        ("1cv8c.exe", "/F:different"),
    ],
)
def test_database_preflight_rejects_malformed_duplicate_or_conflicting_selectors(
    tmp_path: Path, command: tuple[str, ...]
) -> None:
    observed_cwd = tmp_path / "observed-cwd"
    target = observed_cwd / "target"
    for name in ("target", "different", "other", "orce", "-Force"):
        base = observed_cwd / name
        base.mkdir(parents=True, exist_ok=True)
        (base / "1Cv8.1CD").write_bytes(name.encode("ascii"))
    observed = _ObservedProcess(
        pid=5240,
        executable=str((tmp_path / "1cv8c.exe").resolve()),
        command=command,
        name="1cv8c.exe",
        cwd=str(observed_cwd),
    )

    with pytest.raises(AssertionError, match="unverifiable"):
        process_support.assert_target_database_not_open(
            target, processes=(observed,)
        )


@pytest.mark.parametrize(
    "selector",
    [
        ("/F", "different"),
        ("-f", '"different"'),
        ('/F"different"',),
        ('-F"different"',),
    ],
)
def test_database_preflight_accepts_one_exact_file_selector_for_a_proven_different_base(
    tmp_path: Path, selector: tuple[str, ...]
) -> None:
    observed_cwd = tmp_path / "observed-cwd"
    target = observed_cwd / "target"
    different = observed_cwd / "different"
    target.mkdir(parents=True)
    different.mkdir()
    (target / "1Cv8.1CD").write_bytes(b"target")
    (different / "1Cv8.1CD").write_bytes(b"different")
    relevant = _ObservedProcess(
        pid=5250,
        executable=str((tmp_path / "1cv8c.exe").resolve()),
        command=("1cv8c.exe", *selector),
        name="1cv8c.exe",
        cwd=str(observed_cwd),
    )
    unrelated = _ObservedProcess(
        pid=5251,
        executable=str((tmp_path / "notepad.exe").resolve()),
        command=("notepad.exe", "/Force", "/F", str(target)),
        name="notepad.exe",
    )

    process_support.assert_target_database_not_open(
        target, processes=(unrelated, relevant)
    )


def _offline_snapshot_fixture() -> dict[str, dict[str, object]]:
    digest = lambda label: sha256(label.encode("utf-8")).hexdigest()
    return {
        "platform": {
            "version": "8.3.27.2170",
            "bin_identity_sha256": digest("bin"),
            "service_python_identity_sha256": digest("python-path"),
            "service_python_sha256": digest("python"),
            "executables_sha256": {
                "1cv8.exe": digest("1cv8"),
                "1cv8c.exe": digest("1cv8c"),
                "dbgs.exe": digest("dbgs"),
            },
        },
        "source": {
            "tree_sha256": digest("source-tree"),
            "capture_module_sha256": digest("capture-module"),
        },
        "notebook": {
            "artifact_sha256": digest("notebook"),
            "main_source_sha256": digest("main"),
            "hypothesis_source_sha256": digest("hypothesis"),
        },
        "implementation": {
            "runtime_tree_sha256": digest("runtime"),
            "harness_sha256": digest("harness"),
            "capture_evidence_support_sha256": digest("capture-support"),
            "process_evidence_support_sha256": digest("process-support"),
            "verifier_sha256": digest("verifier"),
            "evaluator_sha256": digest("evaluator"),
        },
    }


@pytest.mark.parametrize("state", ["missing", "unreadable"])
def test_snapshot_input_failure_after_consumption_publishes_immutable_sanitized_fail(
    tmp_path: Path, state: str
) -> None:
    snapshot_input = tmp_path / "capture-source.bsl"
    snapshot_input.write_text("private source", encoding="utf-8")
    if state == "missing":
        snapshot_input.unlink()

        def collect() -> str:
            return file_sha256(snapshot_input)
    else:

        def collect() -> str:
            raise PermissionError("private unreadable path and content")

    with pytest.raises(capture_support.SnapshotInputFailure) as caught:
        capture_support.collect_snapshot_stage("source", collect)
    assert caught.value.stage == "source"
    assert caught.value.state == state

    preflight = _offline_snapshot_fixture()
    postflight = capture_support.unavailable_snapshot_postflight(
        preflight, caught.value
    )
    assert postflight["source"]["tree_sha256"] != preflight["source"]["tree_sha256"]
    observations = {
        "schema": "onec-agent-capture-live-observations-v1",
        "attempt": 1,
        "result": "FAIL",
        "call_sequence": [],
        "failure": {
            "boundary": f"snapshot_{state}_source",
            "type": "SnapshotInputFailure",
            "last_completed_phase": "postflight",
        },
    }
    database_identity = sha256(b"database").hexdigest()
    environment = {
        "schema": "onec-agent-capture-live-environment-v1",
        "attempt": 1,
        "profile": "capture",
        "maximum_mode": "experiment",
        "database": {
            "target_identity_sha256": database_identity,
            "mutation_requested": False,
        },
        "snapshots": {
            name: {"pre": value, "post": postflight[name]}
            for name, value in preflight.items()
        },
    }
    cleanup = {
        "schema": "onec-agent-capture-live-cleanup-v1",
        "attempt": 1,
        "runtime_owner_state": "absent",
        "owned": [],
        "unrelated": [],
        "errors": [],
    }
    destination = tmp_path / state / "attempt-1"
    write_capture_live_evidence(
        destination,
        environment=environment,
        observations=observations,
        cleanup=cleanup,
    )

    verified = verify_capture_live_evidence(
        destination,
        expected_result=ExpectedCaptureResult.FAIL,
        expected_cleanup_facts=ExpectedCaptureCleanupFacts(
            errors=(),
            cleanup_error_count=0,
            runtime_owner_state="absent",
            owned_roles=(),
            owned_states=(),
            unrelated_states=(),
        ),
        expected_preflight=preflight,
        expected_owned_processes=(),
        expected_unrelated_processes=(),
        expected_database_identity_sha256=database_identity,
        expected_fail_facts=observations,
    )
    assert verified["failure_boundary"] == f"snapshot_{state}_source"
    forged_environment = json.loads(json.dumps(environment))
    forged_environment["snapshots"]["source"]["post"]["tree_sha256"] = sha256(
        b"forged unavailable postflight"
    ).hexdigest()
    with pytest.raises(CaptureEvidenceError, match="snapshot"):
        write_capture_live_evidence(
            tmp_path / f"forged-{state}" / "attempt-1",
            environment=forged_environment,
            observations=observations,
            cleanup=cleanup,
        )
    with pytest.raises(CaptureEvidenceError, match="already exists"):
        write_capture_live_evidence(
            destination,
            environment=environment,
            observations=observations,
            cleanup=cleanup,
        )


def test_raw_popen_handle_cleanup_does_not_depend_on_identity_adoption() -> None:
    class PopenHandle:
        def __init__(self) -> None:
            self.terminated = False
            self.killed = False

        def poll(self) -> int | None:
            return None if not self.terminated else 0

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout: float) -> int:
            assert timeout > 0
            return 0

        def kill(self) -> None:
            self.killed = True

    handle = PopenHandle()
    process_support.terminate_owned_popen(handle, timeout_s=0.1)
    assert handle.terminated is True
    assert handle.killed is False


def test_outer_attempt_timeout_bounds_a_stalled_sdk_call() -> None:
    async def stalled() -> None:
        await asyncio.Event().wait()

    with pytest.raises(TimeoutError):
        asyncio.run(
            capture_support.bounded_sdk_await(
                stalled(),
                attempt_deadline=monotonic() + 0.02,
                call_timeout_s=0.01,
            )
        )


def _copy_attempt_one_history(workspace: Path) -> None:
    source_root = _WORKSPACE / "docs" / "research" / "evidence" / "2026-08-20-mcp-capture"
    destination_root = (
        workspace / "docs" / "research" / "evidence" / "2026-08-20-mcp-capture"
    )
    destination_root.mkdir(parents=True)
    shutil.copytree(source_root / "attempt-1", destination_root / "attempt-1")
    shutil.copy2(
        source_root / "attempt-1-public-regression.json",
        destination_root / "attempt-1-public-regression.json",
    )
    private = capture_attempt_paths(workspace, attempt=1).private
    private.mkdir(parents=True)
    (private / "immutable-private-marker.bin").write_bytes(b"attempt-one-private")


def test_attempt_two_authorization_requires_exact_empty_paths_and_preserves_attempt_one(
    tmp_path: Path,
) -> None:
    _copy_attempt_one_history(tmp_path)
    for unauthorized in (None, "", "1", "3"):
        environment = {} if unauthorized is None else {
            "ONEC_RUNTIME_CAPTURE_ATTEMPT": unauthorized
        }
        with pytest.raises(AssertionError, match="attempt 2"):
            _require_attempt_two_authorization(tmp_path, environment)

    authorization = _require_attempt_two_authorization(
        tmp_path, {"ONEC_RUNTIME_CAPTURE_ATTEMPT": "2"}
    )
    assert authorization.paths == capture_attempt_paths(tmp_path, attempt=2)
    assert authorization.paths.public.exists() is False
    assert authorization.paths.private.exists() is False
    _assert_attempt_one_unchanged(authorization)

    marker = capture_attempt_paths(tmp_path, attempt=1).private / "immutable-private-marker.bin"
    marker.write_bytes(b"mutated")
    with pytest.raises(AssertionError, match="attempt 1"):
        _assert_attempt_one_unchanged(authorization)


@pytest.mark.parametrize("blocker", ["public", "private", "owner"])
def test_attempt_two_authorization_refuses_reuse_or_runtime_owner(
    tmp_path: Path, blocker: str
) -> None:
    _copy_attempt_one_history(tmp_path)
    paths = capture_attempt_paths(tmp_path, attempt=2)
    owner = tmp_path / ".runtime" / "agent-service" / "runtime-owner.json"
    blocked_path = {"public": paths.public, "private": paths.private, "owner": owner}[
        blocker
    ]
    blocked_path.mkdir(parents=True) if blocker != "owner" else blocked_path.parent.mkdir(
        parents=True, exist_ok=True
    )
    if blocker == "owner":
        blocked_path.write_text("{}", encoding="utf-8")
    with pytest.raises(AssertionError, match="already exists|runtime owner"):
        _require_attempt_two_authorization(
            tmp_path, {"ONEC_RUNTIME_CAPTURE_ATTEMPT": "2"}
        )


def test_attempt_two_timeout_layers_match_sdk_schema_and_service_maximum() -> None:
    # The durable attempt budget is not an MCP argument. Individual wait fields
    # remain within official schemas, and the service timeout remains validated.
    from onec_runtime_mcp.agent.mcp_profiles import McpProfile
    from onec_runtime_mcp.mcp_entrypoint import MAX_CONTROL_TIMEOUT_S
    from tools import eval_mcp_palettes as palette

    attempt_one = _capture_attempt_policy(1)
    attempt_two = _capture_attempt_policy(2)
    assert (
        attempt_one.overall_timeout_s,
        attempt_one.sdk_outer_timeout_s,
        attempt_one.control_timeout_s,
        attempt_one.schema_wait_max_s,
    ) == (900.0, 190.0, 180.0, 30.0)
    assert (
        attempt_two.overall_timeout_s,
        attempt_two.sdk_outer_timeout_s,
        attempt_two.control_timeout_s,
        attempt_two.schema_wait_max_s,
    ) == (1800.0, 600.0, 180.0, 30.0)
    assert _ATTEMPT == 2
    assert _ATTEMPT_TIMEOUT_S == 1800.0
    assert _SDK_CALL_TIMEOUT_S == 600.0
    assert _CONTROL_TIMEOUT_S == MAX_CONTROL_TIMEOUT_S == 180.0
    assert _MCP_SCHEMA_WAIT_MAX_S == 30.0
    assert _SALT == capture_identity_salt(2)
    assert _frontend_environment(Path("control/endpoint.json"))[
        "ONEC_RUNTIME_CONTROL_TIMEOUT_S"
    ] == "180.0"

    schemas = {
        item["name"]: item["input_schema"]
        for item in asyncio.run(palette.list_tool_schemas(McpProfile.CAPTURE))
    }
    for method in (
        "code.run_inline",
        "capture.run_until",
        "capture.hypothesis",
        "capture.continue",
    ):
        assert schemas[method]["properties"]["wait_s"]["maximum"] == 30
    assert schemas["operation.wait"]["properties"]["timeout_s"]["maximum"] == 30

    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    offenders: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values, strict=True):
            if (
                isinstance(key, ast.Constant)
                and key.value in {"wait_s", "timeout_s"}
                and isinstance(value, ast.Name)
                and value.id == "_CONTROL_TIMEOUT_S"
            ):
                offenders.append((str(key.value), value.lineno))
    assert offenders == []

    hardcoded_attempt_one_request_ids: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values, strict=True):
            if (
                isinstance(key, ast.Constant)
                and key.value == "request_id"
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
                and "attempt-1" in value.value
            ):
                hardcoded_attempt_one_request_ids.append((value.value, value.lineno))
    assert hardcoded_attempt_one_request_ids == []
    assert {
        _attempt_request_id("main"),
        _attempt_request_id("hypothesis"),
        _attempt_request_id("continue-b"),
        _attempt_request_id("finish-main"),
    } == {
        "capture-task-6-main-attempt-2",
        "capture-task-6-hypothesis-attempt-2",
        "capture-task-6-continue-b-attempt-2",
        "capture-task-6-finish-main-attempt-2",
    }


def _private_replay_test_fixture(tmp_path: Path) -> dict[str, object]:
    digest = lambda label: sha256(label.encode("utf-8")).hexdigest()
    roots = {
        "workspace": str(tmp_path.resolve()),
        "platform_bin": str((tmp_path / "platform").resolve()),
        "infobase": str((tmp_path / "infobase").resolve()),
        "source_root": str((tmp_path / "source").resolve()),
        "python": str((tmp_path / "python.exe").resolve()),
    }
    preflight = {
        "platform": {"bin_identity_sha256": digest("platform")},
        "source": {"tree_sha256": digest("source")},
        "notebook": {"artifact_sha256": digest("notebook")},
        "implementation": {"runtime_tree_sha256": digest("implementation")},
    }
    return {
        "schema": "onec-agent-capture-private-fixture-v1",
        "attempt": 2,
        "authorization": {"capture_attempt": "2", "live_flag": "1"},
        "paths": {"public_leaf": "attempt-2", "private_leaf": "attempt-2"},
        "invocation": {
            "profile": "capture",
            "maximum_mode": "experiment",
            "mutation_requested": False,
            "username": "capture_user",
            "roots": roots,
            "request_ids": {
                "main": "capture-task-6-main-attempt-2",
                "hypothesis": "capture-task-6-hypothesis-attempt-2",
                "continue_b": "capture-task-6-continue-b-attempt-2",
                "finish_main": "capture-task-6-finish-main-attempt-2",
            },
            "timeouts": {
                "overall_timeout_s": 1800.0,
                "sdk_outer_timeout_s": 600.0,
                "control_timeout_s": 180.0,
                "schema_wait_max_s": 30.0,
            },
        },
        "target_database": {"filesystem_key": "1a:2b", "size": 4096},
        "unrelated_processes": [],
        "private_markers": [{"kind": "username", "value": "capture_user"}],
        "preflight": preflight,
        "bindings": {
            "platform_sha256": preflight["platform"]["bin_identity_sha256"],
            "source_sha256": preflight["source"]["tree_sha256"],
            "notebook_sha256": preflight["notebook"]["artifact_sha256"],
            "implementation_sha256": preflight["implementation"][
                "runtime_tree_sha256"
            ],
        },
        "pass_contract": {
            "call_sequence": list(CAPTURE_PASS_CALL_SEQUENCE),
            "main": {},
            "hypothesis": {},
            "stop_bindings": {},
            "capture_a_local_names": [],
            "table_name": "PayrollTable",
            "downstream_result_local": "DescriptionLocal",
            "budgets": {},
        },
    }


def test_live_harness_builds_create_once_private_fixture_before_launch(
    tmp_path: Path,
) -> None:
    base = _private_replay_test_fixture(tmp_path)
    unrelated = (
        ProcessIdentity(
            "unrelated_designer",
            9101,
            1700000101.25,
            str((tmp_path / "unrelated" / "1cv8.exe").resolve()),
        ),
    )
    database = process_support.CanonicalDatabaseIdentity("1a:2b", 4096)

    fixture = _capture_private_fixture(
        username="capture_user",
        platform_bin=tmp_path / "platform",
        infobase=tmp_path / "infobase",
        zup_source_root=tmp_path / "source",
        python_executable=tmp_path / "python.exe",
        target_database_identity=database,
        unrelated=unrelated,
        preflight=base["preflight"],  # type: ignore[arg-type]
        main={"cell_id": "zup_capture_main", "revision": 3, "source_sha256": "a" * 64},
        hypothesis={
            "cell_id": "zup_capture_hypothesis",
            "revision": 2,
            "source_sha256": "b" * 64,
        },
        point_a_binding={"name": "capture_a"},
        point_b_binding={"name": "capture_b"},
    )

    paths = capture_attempt_paths(tmp_path, attempt=2)
    create_capture_private_replay(paths.private, fixture=fixture)

    assert fixture["target_database"] == {"filesystem_key": "1a:2b", "size": 4096}
    assert fixture["unrelated_processes"] == [unrelated[0].private_wire()]
    assert fixture["pass_contract"]["call_sequence"] == list(  # type: ignore[index]
        CAPTURE_PASS_CALL_SEQUENCE
    )
    assert load_capture_private_journal_prefix(paths.private)[0]["kind"] == "fixture"


def test_recorded_call_persists_authenticated_private_replay_event_pair(
    tmp_path: Path,
) -> None:
    paths = capture_attempt_paths(tmp_path, attempt=2)
    journal = create_capture_private_replay(
        paths.private, fixture=_private_replay_test_fixture(tmp_path)
    )

    class Session:
        async def call_tool(
            self, method: str, arguments: dict[str, object]
        ) -> SimpleNamespace:
            assert method == "runtime.status"
            assert arguments == {}
            return SimpleNamespace(
                structured_content={
                    "ok": True,
                    "value": {"state": "ready"},
                    "failure": None,
                }
            )

    class EmptyTracker:
        def identities(self) -> tuple[ProcessIdentity, ...]:
            return ()

    deadline = _ACTIVE_ATTEMPT_DEADLINE.set(monotonic() + 10)
    tracker = _ACTIVE_TRACKER.set(EmptyTracker())  # type: ignore[arg-type]
    markers = _ACTIVE_PRIVATE_MARKERS.set([])
    raw_markers = _ACTIVE_RAW_PRIVATE_MARKERS.set(())
    external_pids = _ACTIVE_EXTERNAL_PIDS.set(())
    calls: list[str] = []
    raw_index = [0]
    try:
        value = asyncio.run(
            _recorded_call(
                Session(),  # type: ignore[arg-type]
                method="runtime.status",
                arguments={},
                label="A:runtime.status",
                calls=calls,
                raw_index=raw_index,
                raw_dir=paths.private / "raw-mcp",
                private_journal=journal,
            )
        )
    finally:
        _ACTIVE_EXTERNAL_PIDS.reset(external_pids)
        _ACTIVE_RAW_PRIVATE_MARKERS.reset(raw_markers)
        _ACTIVE_PRIVATE_MARKERS.reset(markers)
        _ACTIVE_TRACKER.reset(tracker)
        _ACTIVE_ATTEMPT_DEADLINE.reset(deadline)

    assert value == {"state": "ready"}
    assert calls == ["A:runtime.status"]
    assert raw_index == [1]
    prefix = load_capture_private_journal_prefix(paths.private)
    assert [entry["kind"] for entry in prefix] == [
        "fixture",
        "call_started",
        "call_response",
    ]
    assert prefix[-1]["payload"]["accepted"] is True
    assert (
        paths.private / "raw-mcp" / "0001-runtime-status.json"
    ).read_text(encoding="utf-8") == (
        '{"failure":null,"ok":true,"value":{"state":"ready"}}\n'
    )


def test_recorded_call_never_persists_response_containing_control_token(
    tmp_path: Path,
) -> None:
    paths = capture_attempt_paths(tmp_path, attempt=2)
    journal = create_capture_private_replay(
        paths.private, fixture=_private_replay_test_fixture(tmp_path)
    )
    control_token = "sensitive-control-token"

    class Session:
        async def call_tool(
            self, _method: str, _arguments: dict[str, object]
        ) -> SimpleNamespace:
            return SimpleNamespace(
                structured_content={
                    "ok": True,
                    "value": {"detail": control_token},
                    "failure": None,
                }
            )

    class EmptyTracker:
        def identities(self) -> tuple[ProcessIdentity, ...]:
            return ()

    deadline = _ACTIVE_ATTEMPT_DEADLINE.set(monotonic() + 10)
    tracker = _ACTIVE_TRACKER.set(EmptyTracker())  # type: ignore[arg-type]
    markers = _ACTIVE_PRIVATE_MARKERS.set([])
    raw_markers = _ACTIVE_RAW_PRIVATE_MARKERS.set((control_token,))
    external_pids = _ACTIVE_EXTERNAL_PIDS.set(())
    try:
        with pytest.raises(CaptureEvidenceError, match="private marker"):
            asyncio.run(
                _recorded_call(
                    Session(),  # type: ignore[arg-type]
                    method="runtime.status",
                    arguments={},
                    label="A:runtime.status",
                    calls=[],
                    raw_index=[0],
                    raw_dir=paths.private / "raw-mcp",
                    private_journal=journal,
                )
            )
    finally:
        _ACTIVE_EXTERNAL_PIDS.reset(external_pids)
        _ACTIVE_RAW_PRIVATE_MARKERS.reset(raw_markers)
        _ACTIVE_PRIVATE_MARKERS.reset(markers)
        _ACTIVE_TRACKER.reset(tracker)
        _ACTIVE_ATTEMPT_DEADLINE.reset(deadline)

    raw_files = list((paths.private / "raw-mcp").glob("*.json"))
    assert len(raw_files) == 1
    assert control_token not in raw_files[0].read_text(encoding="utf-8")
    prefix = load_capture_private_journal_prefix(paths.private)
    assert prefix[-1]["kind"] == "call_response"
    assert prefix[-1]["payload"]["accepted"] is False


def test_control_credentials_are_removed_from_exact_private_attempt_path(
    tmp_path: Path,
) -> None:
    paths = capture_attempt_paths(tmp_path, attempt=2)
    create_capture_private_replay(
        paths.private, fixture=_private_replay_test_fixture(tmp_path)
    )
    descriptor = paths.private / "control" / "endpoint.json"
    token = descriptor.with_name("token")
    descriptor.parent.mkdir(parents=True)
    descriptor.write_text('{"service_instance_id":"private"}', encoding="utf-8")
    token.write_text('{"token":"ephemeral"}', encoding="utf-8")

    _remove_control_credentials(paths.private, descriptor)

    assert descriptor.exists() is False
    assert token.exists() is False


def test_path_presence_state_distinguishes_absent_alive_and_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alive = tmp_path / "alive.json"
    alive.write_text("{}", encoding="utf-8")
    missing = tmp_path / "missing.json"
    unreadable = tmp_path / "unreadable.json"
    original_stat = Path.stat

    def controlled_stat(path: Path, *args: object, **kwargs: object) -> os.stat_result:
        if path == unreadable:
            raise PermissionError("synthetic access failure")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", controlled_stat)

    assert _path_presence_state(missing) == "absent"
    assert _path_presence_state(alive) == "alive"
    assert _path_presence_state(unreadable) == "unknown"


def test_failed_credential_unlink_is_durable_and_blocks_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = capture_attempt_paths(tmp_path, attempt=2)
    journal = create_capture_private_replay(
        paths.private, fixture=_private_replay_test_fixture(tmp_path)
    )
    ledger_payload = b'{"attempt":2,"owned":[]}\n'
    (paths.private / "owned-processes.json").write_bytes(ledger_payload)
    descriptor = paths.private / "control" / "endpoint.json"
    token = descriptor.with_name("token")
    descriptor.parent.mkdir(parents=True)
    descriptor.write_text('{"service_instance_id":"private"}', encoding="utf-8")
    token.write_text('{"token":"ephemeral"}', encoding="utf-8")
    original_unlink = Path.unlink

    def blocked_unlink(path: Path, *args: object, **kwargs: object) -> None:
        if path == token:
            raise PermissionError("synthetic unlink failure")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", blocked_unlink)
    with pytest.raises(PermissionError, match="synthetic unlink failure"):
        _remove_control_credentials(paths.private, descriptor)

    token_state = _path_presence_state(token)
    descriptor_state = _path_presence_state(descriptor)
    cleanup = {
        "schema": "onec-agent-capture-live-cleanup-v1",
        "attempt": 2,
        "runtime_owner_state": "absent",
        "owned": [],
        "unrelated": [],
        "errors": ["PermissionError"],
    }
    observations = {
        "schema": "onec-agent-capture-live-observations-v1",
        "attempt": 2,
        "result": "FAIL",
        "call_sequence": [],
        "failure": {
            "boundary": "before_A_workspace_open",
            "type": "PermissionError",
            "last_completed_phase": "preflight",
        },
    }
    preflight = _private_replay_test_fixture(tmp_path)["preflight"]
    _complete_capture_private_replay(
        journal,
        cleanup=cleanup,
        postflight=preflight,  # type: ignore[arg-type]
        observations=observations,
        raw_error_type="PermissionError",
        target_database_process_count=0,
        control_token_state=token_state,
        control_descriptor_state=descriptor_state,
    )

    replay = load_capture_private_replay(paths.private)
    assert replay.control_token_state == "alive"
    assert replay.control_descriptor_state == "alive"
    with pytest.raises(CapturePrivateReplayError, match="control credentials"):
        publish_capture_private_replay(
            paths.public,
            private_attempt=paths.private,
            environment={},
            observations={},
            cleanup={},
        )
    assert paths.public.exists() is False


def test_live_harness_completes_fail_replay_before_publication(tmp_path: Path) -> None:
    paths = capture_attempt_paths(tmp_path, attempt=2)
    journal = create_capture_private_replay(
        paths.private, fixture=_private_replay_test_fixture(tmp_path)
    )
    ledger_payload = b'{"attempt":2,"owned":[]}\n'
    (paths.private / "owned-processes.json").write_bytes(ledger_payload)
    cleanup = {
        "schema": "onec-agent-capture-live-cleanup-v1",
        "attempt": 2,
        "runtime_owner_state": "absent",
        "owned": [],
        "unrelated": [],
        "errors": ["TimeoutError"],
    }
    observations = {
        "schema": "onec-agent-capture-live-observations-v1",
        "attempt": 2,
        "result": "FAIL",
        "call_sequence": [],
        "failure": {
            "boundary": "before_A_workspace_open",
            "type": "TimeoutError",
            "last_completed_phase": "preflight",
        },
    }
    preflight = _private_replay_test_fixture(tmp_path)["preflight"]

    _complete_capture_private_replay(
        journal,
        cleanup=cleanup,
        postflight=preflight,  # type: ignore[arg-type]
        observations=observations,
        raw_error_type="TimeoutError",
        target_database_process_count=0,
        control_token_state="absent",
        control_descriptor_state="absent",
    )

    replay = load_capture_private_replay(paths.private)
    assert replay.expected_result is ExpectedCaptureResult.FAIL
    assert replay.expected_fail_facts == observations
    assert load_capture_private_journal_prefix(paths.private)[-1]["kind"] == "complete"


def test_durable_operations_emit_only_sdk_schema_wait_slices_under_overall_deadline() -> None:
    from onec_runtime_mcp.agent.mcp_profiles import McpProfile
    from tools import eval_mcp_palettes as palette

    schemas = {
        item["name"]: item["input_schema"]
        for item in asyncio.run(palette.list_tool_schemas(McpProfile.CAPTURE))
    }
    emitted: list[tuple[str, dict[str, object]]] = []

    async def exercise(method: str) -> None:
        poll_count = 0

        def view(state: str) -> dict[str, Any]:
            return {
                "operation": {"operation_id": f"op-{method}"},
                "state": state,
                "next_event_cursor": poll_count,
                "next_message_cursor": poll_count,
            }

        async def invoke(
            emitted_method: str, arguments: dict[str, object]
        ) -> dict[str, Any]:
            nonlocal poll_count
            emitted.append((emitted_method, dict(arguments)))
            if emitted_method != "operation.wait":
                return view("running")
            poll_count += 1
            return view("completed" if poll_count == 2 else "running")

        _, terminal = await _bounded_operation_exchange(
            invoke,
            method=method,
            arguments={"request_id": f"request-{method}"},
            control_deadline=_operation_control_deadline(),
        )
        assert terminal["state"] == "completed"

    async def scenario() -> None:
        for method in (
            "runtime.ensure",
            "code.run_inline",
            "capture.run_until",
            "capture.hypothesis",
            "capture.continue",
        ):
            await exercise(method)

    token = _ACTIVE_ATTEMPT_DEADLINE.set(monotonic() + _ATTEMPT_TIMEOUT_S)
    try:
        started = monotonic()
        control_deadline = _operation_control_deadline()
        assert control_deadline - started == pytest.approx(
            _ATTEMPT_TIMEOUT_S, abs=0.01
        )
        asyncio.run(scenario())
    finally:
        _ACTIVE_ATTEMPT_DEADLINE.reset(token)

    assert [
        (method, tuple(key for key in ("wait_s", "timeout_s") if key in arguments))
        for method, arguments in emitted
        if "wait_s" in arguments or "timeout_s" in arguments
    ] == [
        ("operation.wait", ("timeout_s",)),
        ("operation.wait", ("timeout_s",)),
        ("code.run_inline", ("wait_s",)),
        ("operation.wait", ("timeout_s",)),
        ("operation.wait", ("timeout_s",)),
        ("capture.run_until", ("wait_s",)),
        ("operation.wait", ("timeout_s",)),
        ("operation.wait", ("timeout_s",)),
        ("capture.hypothesis", ("wait_s",)),
        ("operation.wait", ("timeout_s",)),
        ("operation.wait", ("timeout_s",)),
        ("capture.continue", ("wait_s",)),
        ("operation.wait", ("timeout_s",)),
        ("operation.wait", ("timeout_s",)),
    ]
    for method, arguments in emitted:
        if "wait_s" in arguments:
            advertised = schemas[method]["properties"]["wait_s"]["maximum"]
            assert 0 < float(arguments["wait_s"]) <= advertised == 30
        if "timeout_s" in arguments:
            advertised = schemas[method]["properties"]["timeout_s"]["maximum"]
            assert 0 < float(arguments["timeout_s"]) <= advertised == 30


def test_official_mcp_unknown_executes_advertised_restart_without_resend(
    tmp_path,
) -> None:
    """Break caught: UNKNOWN was documented but no MCP client proved recovery."""
    from capture_evidence_support import run_offline_unknown_recovery_lifecycle

    evidence = asyncio.run(run_offline_unknown_recovery_lifecycle(tmp_path))

    assert evidence.unknown["ok"] is True
    unknown_view = evidence.unknown["value"]
    assert unknown_view["state"] == "unknown"
    advertised = [
        item
        for item in unknown_view["recovery"]
        if item == {
            "method": "runtime.restart",
            "arguments": {"policy": "abort_generation"},
        }
    ]
    assert advertised == [
        {
            "method": "runtime.restart",
            "arguments": {"policy": "abort_generation"},
        }
    ]
    assert evidence.executed_recovery == advertised[0]
    assert evidence.runtime_closing_before_recovery is True

    assert evidence.restarted["ok"] is True
    assert evidence.restarted["value"]["state"] == "ready"
    assert evidence.restarted["value"]["runtime_id"] != evidence.first_runtime_id
    assert evidence.first_backend.close_calls == 1
    assert evidence.first_backend.continue_calls == 1
    assert evidence.first_backend.writeback_roots == ("Сумма", "Порог")
    assert evidence.first_backend.quarantine_calls == 1

    assert evidence.replayed["ok"] is True
    assert (
        evidence.replayed["value"]["operation"]["operation_id"]
        == unknown_view["operation"]["operation_id"]
    )
    assert evidence.first_backend.continue_calls == 1
    assert evidence.second_backend.continue_calls == 0

    assert evidence.closed["ok"] is True
    assert evidence.closed["value"] == {
        "closed": True,
        "policy": "abort_generation",
    }
    assert evidence.closed["failure"] is None
    assert evidence.second_backend.close_calls == 1
    assert evidence.runtime_list == ()
    assert evidence.runtime_owner_exists_after_cleanup is False


def test_palette_eval_uses_offline_registered_handlers_and_records_required_metrics(
    tmp_path: Path,
) -> None:
    script = Path(__file__).resolve().parents[2] / "tools" / "eval_mcp_palettes.py"
    destination = tmp_path / "palette-eval.json"

    completed = subprocess.run(
        [sys.executable, str(script), "--output", str(destination)],
        cwd=Path(__file__).resolve().parents[2],
        env=os.environ
        | {"PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    value = json.loads(destination.read_text(encoding="utf-8"))
    assert set(value) == {"schema", "script_sha256", "bindings", "profiles"}
    assert value["schema"] == "onec-mcp-palette-eval-v1"
    assert len(value["script_sha256"]) == 64
    assert set(value["bindings"]) == {
        "evaluator_source_sha256",
        "sdk_identity",
        "profile_schema_sha256",
        "runtime_identity_sha256",
    }
    assert value["bindings"]["evaluator_source_sha256"] == file_sha256(script)
    assert value["bindings"]["sdk_identity"].startswith("mcp==")
    assert len(value["bindings"]["profile_schema_sha256"]) == 64
    assert len(value["bindings"]["runtime_identity_sha256"]) == 64
    assert [item["profile"] for item in value["profiles"]] == ["capture", "expert"]
    for item in value["profiles"]:
        assert set(item) == {
            "profile",
            "tool_count",
            "calls",
            "invalid_choices",
            "input_schema_tokens",
            "output_schema_tokens",
            "recovery_transitions",
            "workflow_transitions",
            "completed_tasks",
            "task_success",
            "wall_ms",
        }
        assert type(item["tool_count"]) is int and item["tool_count"] > 0
        assert isinstance(item["calls"], list) and item["calls"]
        assert type(item["invalid_choices"]) is int and item["invalid_choices"] >= 0
        assert (
            type(item["input_schema_tokens"]) is int
            and item["input_schema_tokens"] > item["tool_count"] * 10
        )
        assert (
            type(item["output_schema_tokens"]) is int
            and item["output_schema_tokens"] > item["tool_count"] * 10
        )
        assert item["recovery_transitions"]
        assert item["completed_tasks"] == [
            "ensure",
            "capture_a",
            "inspect_frame",
            "bounded_head",
            "hypothesis",
            "continue_once",
            "recover_unknown",
            "close",
        ]
        transitions = item["workflow_transitions"]
        assert isinstance(transitions, list) and len(transitions) == 8
        assert [transition["task"] for transition in transitions] == item[
            "completed_tasks"
        ]
        assert all(transition["succeeded"] is True for transition in transitions)
        continuation = transitions[5]
        assert continuation["from"] == "captured_a"
        assert continuation["to"] == "captured_b"
        assert continuation["tool"] != "operation.wait"
        assert type(item["task_success"]) is bool
        assert type(item["wall_ms"]) is int and item["wall_ms"] > 0
    capture, expert = value["profiles"]
    assert capture["invalid_choices"] == 0
    assert capture["task_success"] is True
    assert expert["invalid_choices"] == 0
    assert expert["task_success"] is True
    assert capture["workflow_transitions"][-2] == {
        "task": "recover_unknown",
        "tool": "runtime.restart",
        "from": "unknown",
        "to": "clean_generation",
        "succeeded": True,
    }
    assert expert["workflow_transitions"][-2] == {
        "task": "recover_unknown",
        "tool": "operation.abort_generation",
        "from": "unknown",
        "to": "clean_generation",
        "succeeded": True,
    }


def test_palette_eval_does_not_count_operation_wait_as_a_continuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tools import eval_mcp_palettes as palette
    from onec_runtime_mcp.agent.mcp_profiles import McpProfile

    schemas = asyncio.run(palette.list_tool_schemas(McpProfile.EXPERT))
    monkeypatch.setitem(
        palette._PROFILE_SCRIPT[McpProfile.EXPERT],
        "continue_once",
        "operation.wait",
    )

    observed = palette.evaluate_profile(McpProfile.EXPERT, schemas, started_ns=0)

    assert observed["task_success"] is False
    assert observed["workflow_transitions"][5] == {
        "task": "continue_once",
        "tool": "operation.wait",
        "from": "captured_a",
        "to": "captured_a",
        "succeeded": False,
    }


def test_palette_eval_rejects_remapped_available_tool_name_oracle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tools import eval_mcp_palettes as palette
    from onec_runtime_mcp.agent.mcp_profiles import McpProfile

    schemas = asyncio.run(palette.list_tool_schemas(McpProfile.CAPTURE))
    monkeypatch.setitem(
        palette._PROFILE_SCRIPT[McpProfile.CAPTURE],
        "continue_once",
        "runtime.close",
    )

    observed = palette.evaluate_profile(
        McpProfile.CAPTURE,
        schemas,
        started_ns=0,
    )

    assert observed["task_success"] is False
    assert "continue_once" not in observed["completed_tasks"]


def test_palette_eval_rejects_same_name_noop_handler() -> None:
    from tools import eval_mcp_palettes as palette
    from onec_runtime_mcp.agent.mcp_profiles import McpProfile

    schemas = asyncio.run(palette.list_tool_schemas(McpProfile.CAPTURE))
    runtime = palette.OfflinePaletteDomainClient(
        McpProfile.CAPTURE,
        faults={"capture.continue": "noop"},
    )

    observed = palette.evaluate_profile(
        McpProfile.CAPTURE,
        schemas,
        started_ns=0,
        runtime=runtime,
    )

    assert observed["task_success"] is False
    assert observed["workflow_transitions"][5] == {
        "task": "continue_once",
        "tool": "capture.continue",
        "from": "captured_a",
        "to": "captured_a",
        "succeeded": False,
    }


@pytest.mark.parametrize("profile_name", ["capture", "expert"])
def test_palette_eval_executes_registered_handlers_and_domain_side_effects(
    profile_name: str,
) -> None:
    from tools import eval_mcp_palettes as palette
    from onec_runtime_mcp.agent.mcp_profiles import McpProfile

    profile = McpProfile(profile_name)
    schemas = asyncio.run(palette.list_tool_schemas(profile))
    runtime = palette.OfflinePaletteDomainClient(profile)

    observed = palette.evaluate_profile(
        profile,
        schemas,
        started_ns=0,
        runtime=runtime,
    )

    assert observed["task_success"] is True
    assert runtime.state == "closed"
    assert runtime.inspected is True
    assert runtime.bounded is True
    assert runtime.hypothesis_recorded is True
    assert [method for method, _ in runtime.calls] == observed["calls"]
    dataframe_call = next(
        arguments
        for method, arguments in runtime.calls
        if method == "value.to_df"
    )
    assert dataframe_call["columns"] == ["Amount"]
    assert dataframe_call["budget"]["rows"] == (
        10_000 if profile is McpProfile.CAPTURE else 5
    )


def test_palette_eval_rejects_malformed_success_result() -> None:
    from tools import eval_mcp_palettes as palette
    from onec_runtime_mcp.agent.mcp_profiles import McpProfile

    schemas = asyncio.run(palette.list_tool_schemas(McpProfile.EXPERT))
    runtime = palette.OfflinePaletteDomainClient(
        McpProfile.EXPERT,
        faults={"value.to_df": "malformed_result"},
    )

    observed = palette.evaluate_profile(
        McpProfile.EXPERT,
        schemas,
        started_ns=0,
        runtime=runtime,
    )

    assert observed["task_success"] is False
    assert "bounded_head" not in observed["completed_tasks"]
    assert observed["workflow_transitions"][3]["succeeded"] is False


def test_palette_eval_rejects_schema_invalid_arguments_before_domain_call() -> None:
    from tools import eval_mcp_palettes as palette
    from onec_runtime_mcp.agent.mcp_profiles import McpProfile

    schemas = asyncio.run(palette.list_tool_schemas(McpProfile.CAPTURE))
    runtime = palette.OfflinePaletteDomainClient(McpProfile.CAPTURE)

    def corrupt_capture_revision(
        task: str,
        _tool: str,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        if task == "capture_a":
            arguments["revision"] = 0
        return arguments

    observed = palette.evaluate_profile(
        McpProfile.CAPTURE,
        schemas,
        started_ns=0,
        runtime=runtime,
        argument_mutator=corrupt_capture_revision,
    )

    assert observed["task_success"] is False
    assert "capture_a" not in observed["completed_tasks"]
    assert all(method != "capture.run_until" for method, _ in runtime.calls)
