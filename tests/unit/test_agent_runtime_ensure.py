from __future__ import annotations

from pathlib import Path
from threading import Event, Lock
import json

import pytest

from onec_runtime_mcp.agent.contracts import (
    AgentOperationState,
    CapabilityMode,
    FailureCategory,
    OperationDescriptor,
    RuntimeDescriptor,
)
from onec_runtime_mcp.agent.service import AgentWorkspaceService, OwnershipUncertain
from onec_runtime.runtime_models import RuntimeNamespaceSnapshot


class _Backend:
    runtime_id = "runtime-started"

    def __init__(self, mode: CapabilityMode) -> None:
        self.mode = mode
        self.closed = False

    def status(self) -> RuntimeDescriptor:
        return RuntimeDescriptor(self.runtime_id, 1, "ready", self.mode)

    def namespace_snapshot(self) -> RuntimeNamespaceSnapshot:
        return RuntimeNamespaceSnapshot(1, 1, ())

    def close(self) -> None:
        self.closed = True


class _BlockingFactory:
    def __init__(self, *, reconcile_unknown: bool = False) -> None:
        self.entered = Event()
        self.release = Event()
        self._lock = Lock()
        self.starts = 0
        self.backend: _Backend | None = None
        self.reconcile_unknown = reconcile_unknown

    def start(self, *, mode: CapabilityMode) -> _Backend:
        with self._lock:
            self.starts += 1
        self.entered.set()
        assert self.release.wait(5), "test did not release runtime factory"
        self.backend = _Backend(mode)
        return self.backend

    def reconcile_unknown_startup(self, **facts: object) -> bool:
        del facts
        return self.reconcile_unknown


class _PendingCleanupFactory:
    def __init__(self) -> None:
        self.starts = 0
        self.cleanup_calls = 0
        self.cleanup_succeeds = False
        self.backend: _Backend | None = None

    def start(self, *, mode: CapabilityMode) -> _Backend:
        self.starts += 1
        if self.starts == 1:
            error = RuntimeError("private startup failure")
            setattr(error, "retry_cleanup", self._retry_cleanup)
            raise error
        self.backend = _Backend(mode)
        return self.backend

    def _retry_cleanup(self) -> None:
        self.cleanup_calls += 1
        if not self.cleanup_succeeds:
            raise RuntimeError("private cleanup failure")


class _AutoReconcilingPendingCleanupFactory(_PendingCleanupFactory):
    def __init__(self) -> None:
        super().__init__()
        self.cleanup_pending = False

    def start(self, *, mode: CapabilityMode) -> _Backend:
        if self.cleanup_pending and not self.reconcile_unknown_startup():
            raise RuntimeError("previous cleanup is incomplete")
        try:
            return super().start(mode=mode)
        except BaseException:
            self.cleanup_pending = True
            raise

    def reconcile_unknown_startup(self, **facts: object) -> bool:
        del facts
        if not self.cleanup_pending:
            return False
        try:
            self._retry_cleanup()
        except BaseException:
            return False
        self.cleanup_pending = False
        return True


def _service(tmp_path: Path, factory: _BlockingFactory) -> AgentWorkspaceService:
    return AgentWorkspaceService(
        tmp_path,
        factory,
        maximum_mode=CapabilityMode.EXPERIMENT,
    )


def test_absent_ensure_returns_published_startup_operation_and_repeat_joins_it(
    tmp_path: Path,
) -> None:
    factory = _BlockingFactory()
    service = _service(tmp_path, factory)
    try:
        first = service.call(
            "runtime.ensure",
            {"profile": "zup", "mode": "experiment"},
            caller_id="agent-a",
        )
        assert first.ok is True
        assert isinstance(first.value, OperationDescriptor)
        assert first.value.state in {
            AgentOperationState.QUEUED,
            AgentOperationState.RUNNING,
        }
        assert service.call(
            "operation.status",
            {"operation_id": first.value.operation_id},
            caller_id="agent-a",
        ).ok
        assert factory.entered.wait(2)

        second = service.call(
            "runtime.ensure",
            {"profile": "zup", "mode": "observe"},
            caller_id="agent-b",
        )
        assert second.ok is True
        assert second.value.operation_id == first.value.operation_id
        assert factory.starts == 1

        factory.release.set()
        terminal = service.call(
            "operation.wait",
            {"operation_id": first.value.operation_id, "timeout_s": 2},
            caller_id="agent-b",
        )
        assert terminal.ok is True
        assert terminal.value.state is AgentOperationState.COMPLETED

        ready = service.call(
            "runtime.ensure",
            {"profile": "zup", "mode": "observe"},
            caller_id="agent-b",
        )
        assert ready.ok is True
        assert isinstance(ready.value, RuntimeDescriptor)
        assert ready.value.runtime_id == "runtime-started"
        assert factory.starts == 1
    finally:
        factory.release.set()
        service.close()


def test_incompatible_ensure_conflicts_without_starting_second_runtime(tmp_path: Path) -> None:
    factory = _BlockingFactory()
    service = _service(tmp_path, factory)
    try:
        started = service.call(
            "runtime.ensure",
            {"profile": "zup", "mode": "experiment"},
            caller_id="agent-a",
        )
        assert started.ok is True
        assert factory.entered.wait(2)

        conflict = service.call(
            "runtime.ensure",
            {"profile": "empty", "mode": "observe"},
            caller_id="agent-b",
        )
        assert conflict.ok is False
        assert conflict.failure.category is FailureCategory.CONFLICT
        assert factory.starts == 1
        assert service.call(
            "operation.status",
            {"operation_id": started.value.operation_id},
            caller_id="agent-a",
        ).ok
    finally:
        factory.release.set()
        service.close()


def test_restart_recovers_unknown_startup_ownership_and_never_bootstraps_second_runtime(
    tmp_path: Path,
) -> None:
    journal = tmp_path / ".runtime" / "agent-service" / "operations.jsonl"
    journal.parent.mkdir(parents=True)
    operation_id = "startup-before-service-restart"
    events = [
        {
            "cursor": 1,
            "event": "submitted",
            "operation_id": operation_id,
            "operation_kind": "runtime_ensure",
            "runtime_id": "",
            "runtime_generation": None,
            "code_id": None,
            "revision": None,
            "source_sha256": None,
            "inputs_sha256": "a" * 64,
            "observation": None,
            "startup_profile": "zup",
            "startup_mode": "experiment",
        },
        {"cursor": 2, "event": "started", "operation_id": operation_id},
    ]
    journal.write_text(
        "".join(json.dumps(item, separators=(",", ":")) + "\n" for item in events),
        encoding="utf-8",
    )
    factory = _BlockingFactory()
    service = _service(tmp_path, factory)
    try:
        response = service.call(
            "runtime.ensure",
            {"profile": "zup", "mode": "observe"},
            caller_id="reconnected-agent",
        )

        assert response.ok is True
        assert response.value.operation_id == operation_id
        assert response.value.state is AgentOperationState.UNKNOWN
        assert factory.starts == 0
    finally:
        service.close()


def test_explicit_restart_abandons_recovered_unknown_startup_and_unwedges_service(
    tmp_path: Path,
) -> None:
    journal = tmp_path / ".runtime" / "agent-service" / "operations.jsonl"
    journal.parent.mkdir(parents=True)
    operation_id = "startup-before-service-restart"
    journal.write_text(
        "".join(
            json.dumps(item, separators=(",", ":")) + "\n"
            for item in (
                {
                    "cursor": 1,
                    "event": "submitted",
                    "operation_id": operation_id,
                    "operation_kind": "runtime_ensure",
                    "runtime_id": "",
                    "runtime_generation": None,
                    "code_id": None,
                    "revision": None,
                    "source_sha256": None,
                    "inputs_sha256": "a" * 64,
                    "observation": None,
                    "startup_profile": "zup",
                    "startup_mode": "experiment",
                },
                {"cursor": 2, "event": "started", "operation_id": operation_id},
            )
        ),
        encoding="utf-8",
    )
    factory = _BlockingFactory(reconcile_unknown=True)
    service = _service(tmp_path, factory)
    try:
        holder: list[object] = []

        def restart() -> None:
            holder.append(
                service.call(
                    "runtime.restart",
                    {"policy": "abort_generation", "mode": "experiment"},
                    caller_id="reconnected-agent",
                )
            )

        from threading import Thread

        thread = Thread(target=restart)
        thread.start()
        assert factory.entered.wait(2)
        factory.release.set()
        thread.join(2)
        assert not thread.is_alive()
        restarted = holder[0]
        assert restarted.ok is True
        assert factory.starts == 1
        abandoned = service.call(
            "operation.status",
            {"operation_id": operation_id},
            caller_id="reconnected-agent",
        )
        assert abandoned.value.state is AgentOperationState.FAILED
    finally:
        factory.release.set()
        service.close()


def test_failed_startup_cleanup_stays_unknown_until_explicit_close_reconciles_it(
    tmp_path: Path,
) -> None:
    factory = _PendingCleanupFactory()
    service = AgentWorkspaceService(
        tmp_path,
        factory,
        maximum_mode=CapabilityMode.EXPERIMENT,
    )
    try:
        first = service.call(
            "runtime.ensure",
            {"profile": "zup", "mode": "experiment"},
            caller_id="agent",
        )
        assert first.ok and isinstance(first.value, OperationDescriptor)
        terminal = service.call(
            "operation.wait",
            {"operation_id": first.value.operation_id, "timeout_s": 2.0},
            caller_id="agent",
        )

        assert terminal.ok
        assert terminal.value.state is AgentOperationState.UNKNOWN
        joined = service.call(
            "runtime.ensure",
            {"profile": "zup", "mode": "observe"},
            caller_id="other",
        )
        assert joined.ok
        assert joined.value.operation_id == first.value.operation_id
        assert factory.starts == 1
        assert service.call(
            "runtime.start", {"mode": "experiment"}, caller_id="other"
        ).ok is False
        assert factory.starts == 1

        failed_close = service.call(
            "runtime.close",
            {"policy": "abort_generation"},
            caller_id="agent",
        )
        assert failed_close.ok is False
        assert failed_close.failure.category is FailureCategory.PLATFORM_FAILURE
        assert factory.cleanup_calls == 1
        assert factory.starts == 1

        factory.cleanup_succeeds = True
        closed = service.call(
            "runtime.close",
            {"policy": "abort_generation"},
            caller_id="agent",
        )
        assert closed.ok
        assert factory.cleanup_calls == 2

        started = service.call(
            "runtime.start", {"mode": "experiment"}, caller_id="agent"
        )
        assert started.ok
        assert factory.starts == 2
    finally:
        service.close()


def test_service_close_retries_unknown_startup_cleanup_before_closing(
    tmp_path: Path,
) -> None:
    factory = _PendingCleanupFactory()
    service = AgentWorkspaceService(
        tmp_path,
        factory,
        maximum_mode=CapabilityMode.EXPERIMENT,
    )
    startup = service.call("runtime.ensure", {}, caller_id="agent")
    assert startup.ok and isinstance(startup.value, OperationDescriptor)
    terminal = service.call(
        "operation.wait",
        {"operation_id": startup.value.operation_id, "timeout_s": 2.0},
        caller_id="agent",
    )
    assert terminal.value.state is AgentOperationState.UNKNOWN

    with pytest.raises(OwnershipUncertain):
        service.close()

    assert service._closed is False
    assert factory.cleanup_calls == 1
    factory.cleanup_succeeds = True
    service.close()
    assert service._closed is True
    assert factory.cleanup_calls == 2


def test_direct_start_clears_service_cleanup_marker_when_factory_auto_reconciles(
    tmp_path: Path,
) -> None:
    factory = _AutoReconcilingPendingCleanupFactory()
    service = AgentWorkspaceService(
        tmp_path,
        factory,
        maximum_mode=CapabilityMode.EXPERIMENT,
    )

    failed = service.call(
        "runtime.start", {"mode": "experiment"}, caller_id="agent"
    )
    assert failed.ok is False
    assert factory.starts == 1

    factory.cleanup_succeeds = True
    started = service.call(
        "runtime.start", {"mode": "experiment"}, caller_id="agent"
    )
    assert started.ok is True
    assert factory.cleanup_calls == 1
    assert factory.starts == 2

    closed = service.call(
        "runtime.close",
        {"policy": "abort_generation"},
        caller_id="agent",
    )
    assert closed.ok is True
    service.close()
    assert service._closed is True


def test_ensure_clears_service_cleanup_marker_when_factory_auto_reconciles(
    tmp_path: Path,
) -> None:
    factory = _AutoReconcilingPendingCleanupFactory()
    service = AgentWorkspaceService(
        tmp_path,
        factory,
        maximum_mode=CapabilityMode.EXPERIMENT,
    )

    failed = service.call(
        "runtime.start", {"mode": "experiment"}, caller_id="agent"
    )
    assert failed.ok is False

    factory.cleanup_succeeds = True
    startup = service.call(
        "runtime.ensure",
        {"profile": "zup", "mode": "experiment"},
        caller_id="agent",
    )
    assert startup.ok and isinstance(startup.value, OperationDescriptor)
    terminal = service.call(
        "operation.wait",
        {"operation_id": startup.value.operation_id, "timeout_s": 2.0},
        caller_id="agent",
    )
    assert terminal.ok
    assert terminal.value.state is AgentOperationState.COMPLETED
    assert factory.cleanup_calls == 1
    assert factory.starts == 2

    closed = service.call(
        "runtime.close",
        {"policy": "abort_generation"},
        caller_id="agent",
    )
    assert closed.ok is True
    service.close()
    assert service._closed is True


def test_ensure_reconcile_failure_stays_unknown_until_close_succeeds(
    tmp_path: Path,
) -> None:
    factory = _AutoReconcilingPendingCleanupFactory()
    service = AgentWorkspaceService(
        tmp_path,
        factory,
        maximum_mode=CapabilityMode.EXPERIMENT,
    )

    failed = service.call(
        "runtime.start", {"mode": "experiment"}, caller_id="agent"
    )
    assert failed.ok is False

    startup = service.call(
        "runtime.ensure",
        {"profile": "zup", "mode": "experiment"},
        caller_id="agent",
    )
    assert startup.ok and isinstance(startup.value, OperationDescriptor)
    terminal = service.call(
        "operation.wait",
        {"operation_id": startup.value.operation_id, "timeout_s": 2.0},
        caller_id="agent",
    )
    assert terminal.ok
    assert terminal.value.state is AgentOperationState.UNKNOWN
    assert factory.cleanup_calls == 1
    assert factory.starts == 1

    factory.cleanup_succeeds = True
    closed = service.call(
        "runtime.close",
        {"policy": "abort_generation"},
        caller_id="agent",
    )
    assert closed.ok is True
    assert factory.cleanup_calls == 2
    service.close()
    assert service._closed is True
