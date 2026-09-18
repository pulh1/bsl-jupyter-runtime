"""Shared, public test harness pieces for CAPTURE acceptance evidence.

This module deliberately exposes named helpers instead of making live tests
import underscored implementation details from one another.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
from time import monotonic, sleep
import sys
from typing import Any

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
import psutil

from onec_runtime_mcp.agent.capture_contracts import (
    CaptureFence,
    CaptureView,
    ResolvedCapturePoint,
)
from integration.evidence.capture_live_evidence import snapshot_unavailable_sha256
from onec_runtime_mcp.agent.contracts import CapabilityMode, RuntimeDescriptor
from onec_runtime_mcp.agent.service import AgentWorkspaceService
from onec_runtime_mcp.agent.service_server import AgentControlServer
from onec_runtime.runtime_models import RuntimeNamespaceSnapshot
from process_evidence_support import OwnedProcessTracker, ProcessIdentity


class SnapshotInputFailure(RuntimeError):
    """Sanitized identity of an unavailable post-attempt snapshot input."""

    def __init__(self, stage: str, state: str) -> None:
        if stage not in {"platform", "source", "notebook", "implementation"}:
            raise ValueError("snapshot stage is invalid")
        if state not in {"missing", "unreadable"}:
            raise ValueError("snapshot input state is invalid")
        super().__init__(f"snapshot_{state}_{stage}")
        self.stage = stage
        self.state = state


def collect_snapshot_stage(stage: str, collector: Callable[[], Any]) -> Any:
    """Collect one named snapshot stage without carrying OS details forward."""
    if not callable(collector):
        raise TypeError("snapshot collector must be callable")
    try:
        return collector()
    except SnapshotInputFailure:
        raise
    except FileNotFoundError as error:
        raise SnapshotInputFailure(stage, "missing") from error
    except Exception as error:
        raise SnapshotInputFailure(stage, "unreadable") from error


def unavailable_snapshot_postflight(
    preflight: Mapping[str, Mapping[str, object]],
    failure: SnapshotInputFailure,
) -> dict[str, dict[str, object]]:
    """Build a valid postflight with a deterministic unavailable-stage hash."""
    if not isinstance(failure, SnapshotInputFailure):
        raise TypeError("snapshot failure must be sanitized")
    if set(preflight) != {"platform", "source", "notebook", "implementation"}:
        raise ValueError("preflight snapshot set is not exact")
    postflight = deepcopy({name: dict(value) for name, value in preflight.items()})
    hash_field = {
        "platform": "bin_identity_sha256",
        "source": "tree_sha256",
        "notebook": "artifact_sha256",
        "implementation": "runtime_tree_sha256",
    }[failure.stage]
    prior = postflight[failure.stage].get(hash_field)
    if not isinstance(prior, str):
        raise ValueError("snapshot sentinel target is invalid")
    postflight[failure.stage][hash_field] = snapshot_unavailable_sha256(
        stage=failure.stage,
        state=failure.state,
        prior=prior,
    )
    return postflight


class OfflineUnknownRecoveryBackend:
    """A real service adapter whose one continuation has unknown outcome."""

    def __init__(self, runtime_id: str) -> None:
        self.runtime_id = runtime_id
        self.close_calls = 0
        self.continue_calls = 0
        self.quarantine_calls = 0
        self.writeback_roots: tuple[str, ...] = ()

    def status(self) -> RuntimeDescriptor:
        return RuntimeDescriptor(
            self.runtime_id,
            1,
            "ready",
            CapabilityMode.EXPERIMENT,
        )

    def namespace_snapshot(self) -> RuntimeNamespaceSnapshot:
        return RuntimeNamespaceSnapshot(1, 1, ())

    def owned_process_snapshot(self) -> tuple[dict[str, object], ...]:
        return ()

    def close(self) -> None:
        self.close_calls += 1

    def prepare_capture_successor(self, intent: object, *, attempt: object) -> object:
        assert intent is None
        assert getattr(attempt, "dirty_roots") == ("Сумма", "Порог")
        backend = self

        class Admission:
            arming = None

            def commit(self) -> None:
                raise AssertionError("unknown continuation must not commit")

            def rollback(self) -> None:
                raise AssertionError("unknown continuation must not roll back")

            def quarantine(self) -> None:
                backend.quarantine_calls += 1

        return Admission()

    def continue_capture(
        self,
        *,
        dirty_roots: tuple[str, ...],
        attempt_id: str | None = None,
    ) -> object:
        assert attempt_id
        self.continue_calls += 1
        self.writeback_roots = dirty_roots
        raise OSError("simulated transport loss after Continue")


class OfflineUnknownRecoveryFactory:
    def __init__(self) -> None:
        self.backends: list[OfflineUnknownRecoveryBackend] = []

    def start(self, *, mode: CapabilityMode) -> OfflineUnknownRecoveryBackend:
        assert mode is CapabilityMode.EXPERIMENT
        backend = OfflineUnknownRecoveryBackend(
            f"offline-recovery-{len(self.backends) + 1}"
        )
        self.backends.append(backend)
        return backend


@dataclass(frozen=True, slots=True)
class OfflineUnknownRecoveryEvidence:
    unknown: dict[str, Any]
    executed_recovery: dict[str, object]
    runtime_closing_before_recovery: bool
    restarted: dict[str, Any]
    replayed: dict[str, Any]
    closed: dict[str, Any]
    runtime_list: tuple[object, ...]
    runtime_owner_exists_after_cleanup: bool
    first_runtime_id: str
    first_backend: OfflineUnknownRecoveryBackend
    second_backend: OfflineUnknownRecoveryBackend


def capture_fence_wire() -> dict[str, object]:
    return {
        "capture_intent_id": "capture-intent-offline",
        "operation_id": "capture-operation-offline",
        "source_revision": 1,
        "source_sha256": "a" * 64,
        "capture_generation": 1,
        "stop_sequence": 1,
    }


def capture_continue_arguments() -> dict[str, object]:
    return {
        "fence": capture_fence_wire(),
        "next_points": [],
        "observe": {"items": []},
        "request_id": "offline-unknown-continuation",
        "wait_s": 2.0,
    }


async def call_mcp_tool(
    session: ClientSession,
    method: str,
    arguments: dict[str, object],
) -> dict[str, Any]:
    result = await session.call_tool(method, arguments)
    assert result.structured_content is not None
    return dict(result.structured_content)


async def bounded_sdk_await(
    awaitable: Awaitable[Any],
    *,
    attempt_deadline: float,
    call_timeout_s: float,
) -> Any:
    """Bound one SDK await by both its call budget and the outer attempt."""
    remaining = attempt_deadline - monotonic()
    if remaining <= 0 or call_timeout_s <= 0:
        if hasattr(awaitable, "close"):
            awaitable.close()  # type: ignore[union-attr]
        raise TimeoutError("live acceptance attempt deadline expired")
    async with asyncio.timeout(min(remaining, call_timeout_s)):
        return await awaitable


def require_mcp_value(result: Any, method: str) -> dict[str, Any] | list[Any]:
    """Return a successful official-SDK value without trusting summary flags."""
    payload = result.structured_content
    if not isinstance(payload, dict) or set(payload) != {"ok", "value", "failure"}:
        raise AssertionError(f"{method} returned an invalid MCP envelope")
    if payload["ok"] is not True or payload["failure"] is not None:
        raise AssertionError(f"{method} returned a failed MCP envelope")
    value = payload["value"]
    if not isinstance(value, (dict, list)):
        raise AssertionError(f"{method} returned an invalid MCP value")
    return value


def mcp_operation_id(view: dict[str, Any]) -> str:
    operation = view.get("operation")
    if not isinstance(operation, dict) or not isinstance(
        operation.get("operation_id"), str
    ):
        raise AssertionError("AgentOperationView has no operation identity")
    return operation["operation_id"]


async def wait_mcp_operation(
    session: ClientSession,
    view: dict[str, Any],
    *,
    timeout_s: float,
) -> dict[str, Any]:
    """Perform one bounded wait; callers record the real tool call explicitly."""
    result = await session.call_tool(
        "operation.wait",
        {
            "operation_id": mcp_operation_id(view),
            "timeout_s": timeout_s,
            "after_event_cursor": view.get("next_event_cursor", 0),
            "after_message_cursor": view.get("next_message_cursor", 0),
        },
    )
    value = require_mcp_value(result, "operation.wait")
    if not isinstance(value, dict):
        raise AssertionError("operation.wait returned a non-view")
    return value


def find_mcp_process(
    tracker: OwnedProcessTracker,
    role: str,
    *,
    required_cmdline: tuple[str, ...],
    timeout_s: float = 5.0,
) -> ProcessIdentity:
    """Bind the just-created stdio child to an exact private identity."""
    known = {item.pid for item in tracker.identities()}
    deadline = monotonic() + timeout_s
    while monotonic() < deadline:
        for process in psutil.Process(os.getpid()).children(recursive=False):
            if process.pid in known:
                continue
            try:
                command = " ".join(process.cmdline()).casefold()
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
            if "onec_runtime_mcp.mcp_entrypoint" in command:
                return tracker.add_root(
                    process.pid,
                    role,
                    required_cmdline=required_cmdline,
                )
        sleep(0.02)
    raise AssertionError(f"owned {role} frontend was not discovered")


def record_private_response(
    destination: Path,
    *,
    index: int,
    method: str,
    result: Any,
) -> None:
    """Keep raw SDK envelopes out of the public allowlisted evidence bundle."""
    payload = result.structured_content
    destination.mkdir(parents=True, exist_ok=True)
    safe_method = method.replace(".", "-")
    (destination / f"{index:02d}-{safe_method}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def extend_private_markers_from_response(
    markers: list[str], value: object
) -> None:
    """Remember response-only private identifiers for later publication checks."""
    private_keys = {
        "runtime_id",
        "capture_intent_id",
        "operation_id",
        "proxy_id",
        "manager_id",
        "table_id",
        "token",
    }

    def visit(item: object) -> None:
        if isinstance(item, dict):
            for key, nested in item.items():
                if (
                    isinstance(key, str)
                    and key.casefold() in private_keys
                    and isinstance(nested, str)
                    and nested
                    and nested not in markers
                ):
                    markers.append(nested)
                visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)

    visit(value)


def file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_sha256(root: Path, *, suffixes: tuple[str, ...] | None = None) -> str:
    """Hash relative names and contents, independent of private absolute paths."""
    files = sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and (suffixes is None or path.suffix.casefold() in suffixes)
    )
    if not files:
        raise AssertionError("snapshot tree is empty")
    digest = sha256()
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


async def run_offline_unknown_recovery_lifecycle(
    workspace: Path,
) -> OfflineUnknownRecoveryEvidence:
    """Exercise UNKNOWN -> advertised restart -> replay -> exact cleanup."""
    caller_id = "capture-offline-recovery"
    factory = OfflineUnknownRecoveryFactory()
    service = AgentWorkspaceService(
        workspace,
        factory,
        maximum_mode=CapabilityMode.EXPERIMENT,
    )
    started = service.call(
        "runtime.start",
        {"profile": "offline", "mode": "experiment"},
        caller_id=caller_id,
    )
    assert started.ok
    first_backend = factory.backends[0]
    fence = CaptureFence.from_wire(capture_fence_wire())
    service._capture.activate_capture_view(  # type: ignore[attr-defined]
        CaptureView(
            fence,
            ResolvedCapturePoint(
                "capture_a",
                "offline",
                "Payroll",
                "Run",
                17,
                1,
                "a" * 64,
                17,
                "Выполнить();",
            ),
            None,
            dirty_roots=("Сумма", "Порог"),
        ),
        service._proxy_registry,  # type: ignore[attr-defined]
    )
    server = AgentControlServer(service, workspace)
    server.start()
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "onec_runtime_mcp.mcp_entrypoint",
            "--workspace",
            str(workspace),
            "--profile",
            "capture",
        ],
        cwd=workspace,
        env={
            "ONEC_RUNTIME_SERVICE_DESCRIPTOR": str(
                server.endpoint.descriptor_path
            ),
            "ONEC_RUNTIME_CONTROL_TIMEOUT_S": "10",
            "ONEC_RUNTIME_CALLER_ID": caller_id,
            "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        },
    )
    try:
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                unknown = await call_mcp_tool(
                    session, "capture.continue", capture_continue_arguments()
                )
                runtime_closing = bool(
                    service._runtime is not None  # type: ignore[attr-defined]
                    and service._runtime.closing  # type: ignore[attr-defined]
                )
                recovery = unknown["value"]["recovery"]
                restart = next(
                    item
                    for item in recovery
                    if item == {
                        "method": "runtime.restart",
                        "arguments": {"policy": "abort_generation"},
                    }
                )
                restarted = await call_mcp_tool(
                    session,
                    restart["method"],
                    restart["arguments"],
                )
                replayed = await call_mcp_tool(
                    session, "capture.continue", capture_continue_arguments()
                )
                closed = await call_mcp_tool(
                    session,
                    "runtime.close",
                    {"policy": "abort_generation"},
                )
        listed = service.call("runtime.list", {}, caller_id=caller_id)
        assert listed.ok
        return OfflineUnknownRecoveryEvidence(
            unknown=unknown,
            executed_recovery=dict(restart),
            runtime_closing_before_recovery=runtime_closing,
            restarted=restarted,
            replayed=replayed,
            closed=closed,
            runtime_list=tuple(listed.value),
            runtime_owner_exists_after_cleanup=(
                workspace
                / ".runtime"
                / "agent-service"
                / "runtime-owner.json"
            ).exists(),
            first_runtime_id=first_backend.runtime_id,
            first_backend=first_backend,
            second_backend=factory.backends[1],
        )
    finally:
        server.close()


__all__ = [
    "OfflineUnknownRecoveryBackend",
    "OfflineUnknownRecoveryEvidence",
    "OfflineUnknownRecoveryFactory",
    "SnapshotInputFailure",
    "bounded_sdk_await",
    "call_mcp_tool",
    "capture_continue_arguments",
    "capture_fence_wire",
    "collect_snapshot_stage",
    "file_sha256",
    "extend_private_markers_from_response",
    "find_mcp_process",
    "mcp_operation_id",
    "record_private_response",
    "require_mcp_value",
    "run_offline_unknown_recovery_lifecycle",
    "tree_sha256",
    "unavailable_snapshot_postflight",
    "wait_mcp_operation",
]
