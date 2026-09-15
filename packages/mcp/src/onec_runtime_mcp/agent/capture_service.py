"""Correlated source-level MAIN-to-CAPTURE orchestration.

The service deliberately owns only capture intent correlation.  The caller
owns the operation registry and the runtime backend remains the only component
that touches debugger sequencing.
"""

from __future__ import annotations

from contextlib import suppress
from functools import wraps
import json
import os
from hashlib import sha256
from dataclasses import dataclass, replace
from math import isfinite
from pathlib import Path
from collections.abc import Mapping, Sequence
from time import monotonic
from threading import RLock
from typing import Protocol
from uuid import uuid4

from onec_runtime_mcp.agent.capture_contracts import (
    CaptureFence,
    CaptureInspection,
    CapturePointRequest,
    CaptureView,
    FrameVariableDescriptor,
    ResolvedCapturePoint,
    TemporaryTableDescriptor,
    TemporaryTableManagerDescriptor,
)
from onec_runtime_mcp.agent.contracts import AgentOperationState, BackendExecution
from onec_runtime_mcp.agent.facade_contracts import RecoveryAction
from onec_runtime_mcp.agent.observation import (
    ManagerOrigin,
    ObservationPlan,
    ObservationResult,
    ObservationSourceKind,
    ValueSelection,
)
from onec_runtime_mcp.agent.proxies import (
    _CapturePublicationCheckpoint,
    ProxyDescriptor,
    ProxyProvenance,
    ProxyRegistry,
    ReleasedProxy,
    StaleProxy,
    ValueSize,
)


def _capture_operation_recovery(
    operation_id: str,
    *,
    include_workspace_status: bool = False,
    include_restart: bool = False,
) -> tuple[RecoveryAction, ...]:
    """Return only actions executable by the compact CAPTURE MCP profile."""
    actions: list[RecoveryAction] = []
    if include_workspace_status:
        actions.append(RecoveryAction("workspace.status", {}))
    actions.extend(
        (
            RecoveryAction(
                "operation.wait",
                {
                    "operation_id": operation_id,
                    "timeout_s": 0,
                    "after_event_cursor": 0,
                    "after_message_cursor": 0,
                },
            ),
            RecoveryAction(
                "runtime.close", {"policy": "abort_generation"}
            ),
        )
    )
    if include_restart:
        actions.append(
            RecoveryAction(
                "runtime.restart", {"policy": "abort_generation"}
            )
        )
    return tuple(actions)


@dataclass(frozen=True, slots=True)
class CaptureIntent:
    capture_intent_id: str
    operation_id: str
    source_revision: int
    source_sha256: str
    capture_generation: int
    points: tuple[ResolvedCapturePoint, ...]


@dataclass(frozen=True, slots=True)
class CaptureArming:
    """Runtime-owned, opaque evidence returned when an intent is armed."""

    ticket_id: str
    expected_controller_operation_id: int
    expected_stop_sequence: int


@dataclass(frozen=True, slots=True)
class CaptureContinuationAttempt:
    """Private one-use identity carried through every continuation layer."""

    attempt_id: str
    capture_generation: int
    request_operation_id: str
    dirty_roots: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.attempt_id, str)
            or not self.attempt_id
            or len(self.attempt_id) > 256
        ):
            raise ValueError("continuation attempt_id is invalid")
        if type(self.capture_generation) is not int or self.capture_generation <= 0:
            raise ValueError("continuation capture_generation must be positive")
        if (
            not isinstance(self.request_operation_id, str)
            or not self.request_operation_id
            or len(self.request_operation_id) > 256
        ):
            raise ValueError("continuation request_operation_id is invalid")
        roots = tuple(self.dirty_roots)
        if len(roots) > 100 or any(
            not isinstance(root, str)
            or not root.isidentifier()
            or len(root) > 256
            for root in roots
        ):
            raise ValueError("continuation dirty_roots are invalid")
        if len({root.casefold() for root in roots}) != len(roots):
            raise ValueError("continuation dirty_roots must be unique")
        object.__setattr__(self, "dirty_roots", roots)


@dataclass(frozen=True, slots=True)
class CaptureContinuationEvidence:
    """Exact bounded evidence for only one continuation attempt."""

    root_statuses: tuple[tuple[str, str], ...]
    continue_state: str

    def __post_init__(self) -> None:
        statuses = tuple(self.root_statuses)
        allowed_roots = {
            "unattempted",
            "failed",
            "sent",
            "succeeded",
            "outcome_unknown",
        }
        if len(statuses) > 100 or any(
            not isinstance(item, tuple)
            or len(item) != 2
            or not isinstance(item[0], str)
            or not item[0].isidentifier()
            or len(item[0]) > 256
            or item[1] not in allowed_roots
            for item in statuses
        ):
            raise ValueError("continuation root evidence is invalid")
        if len({root.casefold() for root, _status in statuses}) != len(statuses):
            raise ValueError("continuation root evidence must be unique")
        if self.continue_state not in {
            "unattempted",
            "planned",
            "sent",
            "acknowledged",
            "outcome_unknown",
        }:
            raise ValueError("continuation Continue evidence is invalid")
        object.__setattr__(self, "root_statuses", statuses)


@dataclass(frozen=True, slots=True)
class CaptureStop:
    stop_sequence: int
    location: ResolvedCapturePoint
    controller_operation_id: int
    ticket_id: str | None
    observed_command_id: int | None


@dataclass(frozen=True, slots=True)
class CaptureRunOutcome:
    execution: BackendExecution
    stop: CaptureStop | None = None
    partial_results: Mapping[str, str] = ()  # controller-derived, public root statuses
    continuation: CaptureContinuationEvidence | None = None
    user_main_dispatched: bool = False


@dataclass(frozen=True, slots=True)
class CaptureRunResult:
    execution: BackendExecution
    capture: CaptureView | None = None
    failure: dict[str, object] | None = None
    recovery: tuple[RecoveryAction, ...] = ()
    quarantine_runtime: bool = False
    user_main_dispatched: bool = False


class CaptureSuccessorPreparationError(RuntimeError):
    """Classify whether failed successor preparation was exactly restored."""

    def __init__(self, *, uncertain: bool) -> None:
        self.uncertain = uncertain
        super().__init__(
            "capture successor preparation is uncertain"
            if uncertain
            else "capture successor preparation was restored"
        )


class CaptureSuccessorAdmission(Protocol):
    arming: CaptureArming | None

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def quarantine(self) -> None: ...


class CaptureRuntimeBackend(Protocol):
    def resolve_capture_points(
        self, points: tuple[CapturePointRequest, ...]
    ) -> tuple[ResolvedCapturePoint, ...]: ...

    def arm_capture(self, intent: CaptureIntent) -> CaptureArming: ...

    def run_prepared_main_until_capture(
        self, prepared: object, *, intent: CaptureIntent
    ) -> CaptureRunOutcome: ...

    def discard_prepared_main_for_capture(self, prepared: object) -> None: ...

    def prepare_capture_successor(
        self,
        intent: CaptureIntent | None,
        *,
        attempt: CaptureContinuationAttempt,
    ) -> CaptureSuccessorAdmission: ...

    def continue_capture(
        self,
        *,
        dirty_roots: tuple[str, ...],
        attempt_id: str,
    ) -> CaptureRunOutcome: ...

    def disarm_capture(self, *, policy: str) -> None: ...

    def quarantine_capture_inspection(self, capture: CaptureFence) -> None: ...


class CaptureFrameBackend(Protocol):
    """Safe debugger-frame primitives; values and object graphs stay opaque."""

    def validate_value_reference(self, handle: str) -> str: ...

    def capture_stack(
        self, capture: CaptureFence, *, cursor: int, limit: int,
        timeout_s: float | None = None,
    ) -> Mapping[str, object]: ...

    def capture_frame(
        self, capture: CaptureFence, *, level: int, cursor: int, limit: int,
        name: str | None = None, timeout_s: float | None = None,
    ) -> Mapping[str, object]: ...

    def frame_variables(
        self,
        capture: CaptureFence,
        *,
        filters: Mapping[str, object],
        cursor: int,
        limit: int,
        timeout_s: float | None = None,
    ) -> Mapping[str, object]: ...

    def resolve_manager_origin(
        self,
        capture: CaptureFence,
        origin: ManagerOrigin,
        *,
        timeout_s: float | None = None,
    ) -> Mapping[str, object]: ...

    def temporary_tables(
        self,
        capture: CaptureFence,
        manager_handle: str,
        *, names: tuple[str, ...] | None, cursor: int, limit: int,
        selection: ValueSelection | None, timeout_s: float | None = None,
    ) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class _ManagerRecord:
    manager_id: str
    origin: ManagerOrigin
    fence: CaptureFence
    handle: str
    key: str
    type_name: str


@dataclass(frozen=True, slots=True)
class _CaptureServiceSnapshot:
    active_fence: CaptureFence | None
    active_capture: CaptureView | None
    managers: dict[str, _ManagerRecord]
    manager_by_origin: dict[tuple[CaptureFence, ManagerOrigin], str]
    manager_by_key: dict[tuple[CaptureFence, str], str]
    frame_proxies: dict[tuple[CaptureFence, str], str]
    table_proxies: dict[tuple[CaptureFence, str, str, ValueSelection | None], str]
    table_descriptors: dict[
        tuple[CaptureFence, str, str, ValueSelection | None], TemporaryTableDescriptor
    ]
    table_handles: dict[tuple[CaptureFence, str, str, ValueSelection | None], str]
    proxy_registry: object


_MISSING_SERVICE_VALUE = object()


@dataclass(slots=True)
class _CaptureInspectionMutationJournal:
    """Undo only service keys assigned by one capture.inspect call."""

    entries: list[tuple[dict, object, object]]
    seen: set[tuple[int, object]]

    def __init__(self) -> None:
        self.entries = []
        self.seen = set()

    def set(self, mapping: dict, key: object, value: object) -> None:
        identity = (id(mapping), key)
        if identity not in self.seen:
            self.seen.add(identity)
            self.entries.append(
                (mapping, key, mapping.get(key, _MISSING_SERVICE_VALUE))
            )
        mapping[key] = value

    def rollback(self) -> None:
        for mapping, key, previous in reversed(self.entries):
            if previous is _MISSING_SERVICE_VALUE:
                mapping.pop(key, None)
            else:
                mapping[key] = previous


class _ContinuationAdmission:
    """Service and backend snapshots retained through the Continue boundary."""

    def __init__(
        self,
        service: "CaptureService",
        registry: ProxyRegistry,
        snapshot: _CaptureServiceSnapshot,
        backend: CaptureSuccessorAdmission,
    ) -> None:
        self._service = service
        self._registry = registry
        self._snapshot = snapshot
        self._backend = backend
        self.arming = backend.arming
        self._closed = False

    def commit(self) -> None:
        if not self._closed:
            self._backend.commit()
            self._closed = True

    def rollback(self) -> None:
        if self._closed:
            return
        try:
            # Restore the service/registry snapshot while the lower admission
            # is still open, so a local restore failure can quarantine every
            # owner instead of leaving the physical candidate merely rolled
            # back and inspectable.
            self._service._restore_continuation_snapshot(
                self._registry, self._snapshot
            )
            self._backend.rollback()
        except BaseException:
            try:
                self._backend.quarantine()
            finally:
                self._service.invalidate_capture(self._registry)
                self._closed = True
            raise
        self._closed = True

    def quarantine(self) -> None:
        if self._closed:
            return
        try:
            self._backend.quarantine()
        finally:
            self._service.invalidate_capture(self._registry)
            self._closed = True


_FRAME_CAPABILITIES = ("describe", "size", "preview", "select", "materialize", "to_df")
_TABLE_CAPABILITIES = ("describe", "size", "preview", "select", "materialize", "to_df")
_TABLE_METADATA_CAPABILITIES = ("describe", "size", "select")


def _serialized_capture(method):  # type: ignore[no-untyped-def]
    """Run one capture-scoped transition under the service admission gate."""

    @wraps(method)
    def locked(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        with self.capture_admission():
            return method(self, *args, **kwargs)

    return locked


class CaptureService:
    """Durably creates an intent before a backend may arm a breakpoint."""

    def __init__(self, state_root: str | Path) -> None:
        self._capture_lock = RLock()
        self._journal_path = Path(state_root) / "capture-intents.jsonl"
        self._generation = self._load_generation()
        self._requests = self._load_requests()
        self._active_fence: CaptureFence | None = None
        self._active_capture: CaptureView | None = None
        self._managers: dict[str, _ManagerRecord] = {}
        self._manager_by_origin: dict[tuple[CaptureFence, ManagerOrigin], str] = {}
        self._manager_by_key: dict[tuple[CaptureFence, str], str] = {}
        self._frame_proxies: dict[tuple[CaptureFence, str], str] = {}
        self._table_proxies: dict[tuple[CaptureFence, str, str, ValueSelection | None], str] = {}
        self._table_descriptors: dict[
            tuple[CaptureFence, str, str, ValueSelection | None], TemporaryTableDescriptor
        ] = {}
        self._table_handles: dict[
            tuple[CaptureFence, str, str, ValueSelection | None], str
        ] = {}

    def capture_admission(self) -> RLock:
        """Return the reentrant boundary for capture-only publications."""
        return self._capture_lock

    @_serialized_capture
    def activate_capture(self, fence: CaptureFence, registry: ProxyRegistry | None = None) -> None:
        """Publish a new paused capture and expire every old frame-owned handle."""
        if not isinstance(fence, CaptureFence):
            raise TypeError("fence must be a CaptureFence")
        previous = self._active_fence
        if previous is not None and previous != fence:
            if registry is not None:
                registry.invalidate_capture_fence(previous)
            self._managers = {
                manager_id: record
                for manager_id, record in self._managers.items()
                if record.fence != previous
            }
            self._manager_by_origin = {
                key: manager_id
                for key, manager_id in self._manager_by_origin.items()
                if key[0] != previous
            }
            self._manager_by_key = {
                key: manager_id
                for key, manager_id in self._manager_by_key.items()
                if key[0] != previous
            }
            self._frame_proxies = {
                key: proxy_id for key, proxy_id in self._frame_proxies.items() if key[0] != previous
            }
            self._table_proxies = {
                key: proxy_id for key, proxy_id in self._table_proxies.items() if key[0] != previous
            }
            self._table_descriptors = {
                key: descriptor
                for key, descriptor in self._table_descriptors.items()
                if key[0] != previous
            }
            self._table_handles = {
                key: handle for key, handle in self._table_handles.items() if key[0] != previous
            }
        self._active_fence = fence
        if self._active_capture is not None and self._active_capture.fence != fence:
            self._active_capture = None

    @_serialized_capture
    def activate_capture_view(
        self, capture: CaptureView, registry: ProxyRegistry | None = None
    ) -> None:
        """Publish the complete paused view retained by CAPTURE hypotheses."""
        self.activate_capture(capture.fence, registry)
        self._active_capture = capture

    @_serialized_capture
    def current_capture(self, fence: CaptureFence) -> CaptureView:
        self._require_active(fence)
        if self._active_capture is None:
            raise StaleProxy("capture view is unavailable")
        return self._active_capture

    @_serialized_capture
    def _continuation_snapshot(
        self, registry: ProxyRegistry
    ) -> _CaptureServiceSnapshot:
        return _CaptureServiceSnapshot(
            self._active_fence,
            self._active_capture,
            dict(self._managers),
            dict(self._manager_by_origin),
            dict(self._manager_by_key),
            dict(self._frame_proxies),
            dict(self._table_proxies),
            dict(self._table_descriptors),
            dict(self._table_handles),
            registry._capture_state_snapshot(self._active_fence),
        )

    @_serialized_capture
    def _restore_continuation_snapshot(
        self, registry: ProxyRegistry, snapshot: _CaptureServiceSnapshot
    ) -> None:
        registry._restore_capture_state(snapshot.proxy_registry)
        self._active_fence = snapshot.active_fence
        self._active_capture = snapshot.active_capture
        self._managers = dict(snapshot.managers)
        self._manager_by_origin = dict(snapshot.manager_by_origin)
        self._manager_by_key = dict(snapshot.manager_by_key)
        self._frame_proxies = dict(snapshot.frame_proxies)
        self._table_proxies = dict(snapshot.table_proxies)
        self._table_descriptors = dict(snapshot.table_descriptors)
        self._table_handles = dict(snapshot.table_handles)

    @_serialized_capture
    def stage_dirty_roots(
        self, fence: CaptureFence, dirty_roots: Sequence[str]
    ) -> CaptureView:
        """Record only compiler-proven frame roots without resuming CAPTURE."""
        current = self.current_capture(fence)
        roots = tuple((*current.dirty_roots, *dirty_roots))
        # CaptureView validates identifiers and preserves their declared order;
        # semantic lowering already de-duplicates roots in source order.
        deduplicated = tuple(dict.fromkeys(name.casefold() for name in roots))
        by_key: dict[str, str] = {}
        for name in roots:
            by_key.setdefault(name.casefold(), name)
        if len(deduplicated) > 100:
            raise ValueError("capture continuation dirty_roots exceeds 100")
        self._active_capture = replace(
            current, dirty_roots=tuple(by_key[key] for key in deduplicated)
        )
        return self._active_capture

    @_serialized_capture
    def invalidate_capture(self, registry: ProxyRegistry) -> None:
        if self._active_fence is not None:
            registry.invalidate_capture_fence(self._active_fence)
        self._active_fence = None
        self._active_capture = None
        self._managers.clear()
        self._manager_by_origin.clear()
        self._manager_by_key.clear()
        self._frame_proxies.clear()
        self._table_proxies.clear()
        self._table_descriptors.clear()
        self._table_handles.clear()

    @_serialized_capture
    def active_source_matches(self, *, revision: int, source_sha256: str) -> bool:
        fence = self._active_fence
        return bool(
            fence is not None
            and fence.source_revision == revision
            and fence.source_sha256 == source_sha256
        )

    @_serialized_capture
    def invalidate_if_active(self, fence: CaptureFence, registry: ProxyRegistry) -> None:
        if self._active_fence == fence:
            self.invalidate_capture(registry)

    @_serialized_capture
    def frame_proxy_id(self, name: str, fence: CaptureFence) -> str:
        self._require_active(fence)
        try:
            return self._frame_proxies[(fence, name.casefold())]
        except KeyError as error:
            raise ValueError("frame local is unknown") from error

    @_serialized_capture
    def stack(
        self, backend: CaptureFrameBackend, *, fence: CaptureFence,
        cursor: int, limit: int, timeout_s: float | None = None,
    ) -> Mapping[str, object]:
        self._require_active(fence)
        return backend.capture_stack(
            fence, cursor=cursor, limit=limit, timeout_s=timeout_s,
        )

    @_serialized_capture
    def frame(
        self, backend: CaptureFrameBackend, *, fence: CaptureFence,
        level: int, cursor: int, limit: int, name: str | None = None,
        timeout_s: float | None = None,
    ) -> Mapping[str, object]:
        self._require_active(fence)
        return backend.capture_frame(
            fence, level=level, cursor=cursor, limit=limit, name=name,
            timeout_s=timeout_s,
        )

    @_serialized_capture
    def manager_handle(self, manager_id: str, *, fence: CaptureFence) -> str:
        self._require_active(fence)
        record = self._managers.get(manager_id)
        if record is None or record.fence != fence:
            raise StaleProxy("temporary-table manager is stale")
        return record.handle

    @_serialized_capture
    def inspect(
        self,
        backend: CaptureFrameBackend,
        registry: ProxyRegistry,
        *,
        fence: CaptureFence,
        runtime_id: str,
        runtime_generation: int,
        context_generation: int,
        filters: Mapping[str, object],
        cursor: int,
        limit: int,
        observe: ObservationPlan | None = None,
        timeout_s: float | None = None,
    ) -> CaptureInspection:
        """Return a bounded frame index, resolving only explicit safe origins."""
        if not isinstance(registry, ProxyRegistry) or not isinstance(fence, CaptureFence):
            raise TypeError("capture inspection requires registry and fence")
        if not isinstance(filters, Mapping) or set(filters) - {"name", "role", "type"}:
            raise ValueError("unsupported capture inspection filter")
        if type(cursor) is not int or cursor < 0:
            raise ValueError("cursor must be non-negative")
        if type(limit) is not int or not 0 < limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if observe is not None and not isinstance(observe, ObservationPlan):
            raise TypeError("observe must be an ObservationPlan")
        deadline = self._inspection_deadline(timeout_s)
        self._require_active(fence)
        publication = registry._begin_capture_publication(fence)
        mutations = _CaptureInspectionMutationJournal()
        try:
            variables, total, next_cursor = self._variables(
                backend, registry, fence, runtime_id, runtime_generation,
                context_generation, filters, cursor, limit, deadline,
                publication, mutations,
            )
            managers: list[TemporaryTableManagerDescriptor] = []
            tables: list[TemporaryTableDescriptor] = []
            if observe is not None:
                for item in observe.items:
                    self._remaining_timeout(deadline)
                    if item.result is not ObservationResult.PROXY:
                        raise ValueError(
                            "capture.inspect only permits proxy-only bounded observations"
                        )
                    source = item.source
                    if source.kind is ObservationSourceKind.TEMPORARY_TABLE_MANAGER:
                        if item.select is not None:
                            raise ValueError("manager observation cannot select rows")
                        assert source.origin is not None
                        manager = self._resolve_manager(
                            backend, fence, source.origin, runtime_id,
                            runtime_generation, deadline, mutations,
                        )
                        if all(
                            item.manager_id != manager.manager_id
                            for item in managers
                        ):
                            managers.append(manager)
                    elif source.kind is ObservationSourceKind.TEMPORARY_TABLE:
                        assert (
                            source.manager_id is not None
                            and source.table is not None
                        )
                        record = self._manager_record(source.manager_id, fence)
                        manager = self._manager_descriptor(
                            record, runtime_id, runtime_generation
                        )
                        if all(
                            item.manager_id != manager.manager_id
                            for item in managers
                        ):
                            managers.append(manager)
                        resolved_tables = self._tables(
                            backend, registry, fence, record, source.table,
                            runtime_id, runtime_generation, context_generation,
                            limit, item.select, deadline, publication, mutations,
                        )
                        tables.extend(
                            table
                            for table in resolved_tables
                            if all(
                                existing.table_id != table.table_id
                                for existing in tables
                            )
                        )
                    else:
                        raise ValueError(
                            "capture.inspect observations support only managers "
                            "and temporary tables"
                        )
            result = CaptureInspection(
                fence=fence,
                variables=tuple(variables),
                temporary_table_managers=tuple(managers),
                temporary_tables=tuple(tables),
                cursor=cursor,
                limit=limit,
                total_variables=total,
                next_cursor=next_cursor,
                truncated=next_cursor is not None,
            )
        except BaseException:
            mutations.rollback()
            registry._rollback_capture_publication(publication)
            raise
        registry._commit_capture_publication(publication)
        return result

    def _variables(
        self,
        backend: CaptureFrameBackend,
        registry: ProxyRegistry,
        fence: CaptureFence,
        runtime_id: str,
        runtime_generation: int,
        context_generation: int,
        filters: Mapping[str, object],
        cursor: int,
        limit: int,
        deadline: float | None,
        publication: _CapturePublicationCheckpoint,
        mutations: _CaptureInspectionMutationJournal,
    ) -> tuple[list[FrameVariableDescriptor], int, int | None]:
        remaining = self._remaining_timeout(deadline)
        if remaining is None:
            raw = backend.frame_variables(
                fence, filters=filters, cursor=cursor, limit=limit
            )
        else:
            raw = backend.frame_variables(
                fence, filters=filters, cursor=cursor, limit=limit,
                timeout_s=remaining,
            )
        self._remaining_timeout(deadline)
        items, total, next_cursor = self._page(raw, cursor, limit, total_required=True)
        result: list[FrameVariableDescriptor] = []
        for item in items:
            self._remaining_timeout(deadline)
            if not isinstance(item, Mapping):
                raise TypeError("frame variable metadata must be a mapping")
            name = item.get("name")
            type_name = item.get("type_name")
            role = item.get("role", "local")
            if not isinstance(name, str) or not isinstance(type_name, str) or not isinstance(role, str):
                raise ValueError("frame variable metadata is invalid")
            if not self._matches(name, type_name, role, filters):
                continue
            known_size = item.get("known_size")
            if known_size is not None and not isinstance(known_size, ValueSize):
                raise TypeError("frame variable known_size must be a ValueSize")
            proxy_key = (fence, name.casefold())
            handle = item.get("handle")
            if not isinstance(handle, str) or not handle:
                raise ValueError("frame variable handle must be a non-empty opaque string")
            backend.validate_value_reference(handle)
            existing = self._frame_proxies.get(proxy_key)
            proxy = (
                registry.resolve(existing)
                if existing is not None
                else registry.register_frame(
                    qualified_name=f"capture.{fence.capture_intent_id}.{name}",
                    type_name=type_name,
                    runtime_id=runtime_id,
                    runtime_generation=runtime_generation,
                    context_generation=context_generation,
                    capture_fence=fence,
                    provenance=ProxyProvenance(
                        "capture", fence.source_revision, fence.source_sha256, fence.operation_id
                    ),
                    resolver_handle=handle,
                    capabilities=_FRAME_CAPABILITIES,
                    known_size=known_size,
                    _publication=publication,
                )
            )
            mutations.set(self._frame_proxies, proxy_key, proxy.proxy_id)
            result.append(FrameVariableDescriptor(name, type_name, fence, proxy.capabilities, known_size, proxy.proxy_id))
        self._remaining_timeout(deadline)
        return result, total, next_cursor

    def _resolve_manager(
        self,
        backend: CaptureFrameBackend,
        fence: CaptureFence,
        origin: ManagerOrigin,
        runtime_id: str,
        runtime_generation: int,
        deadline: float | None,
        mutations: _CaptureInspectionMutationJournal,
    ) -> TemporaryTableManagerDescriptor:
        self._remaining_timeout(deadline)
        key = (fence, origin)
        existing = self._manager_by_origin.get(key)
        if existing is not None:
            return self._manager_descriptor(
                self._manager_record(existing, fence), runtime_id, runtime_generation
            )
        remaining = self._remaining_timeout(deadline)
        if remaining is None:
            raw = backend.resolve_manager_origin(fence, origin)
        else:
            raw = backend.resolve_manager_origin(
                fence, origin, timeout_s=remaining
            )
        self._remaining_timeout(deadline)
        if not isinstance(raw, Mapping):
            raise TypeError("manager resolution must return metadata")
        manager_key = raw.get("key")
        handle = raw.get("handle")
        type_name = raw.get("type_name")
        if not isinstance(manager_key, str) or not manager_key or not isinstance(handle, str) or not handle or type_name != "МенеджерВременныхТаблиц":
            raise ValueError("manager resolution is incomplete")
        backend.validate_value_reference(handle)
        known = self._manager_by_key.get((fence, manager_key))
        if known is not None:
            mutations.set(self._manager_by_origin, key, known)
            return self._manager_descriptor(
                self._manager_record(known, fence), runtime_id, runtime_generation
            )
        manager_id = f"vtm_{uuid4().hex}"
        record = _ManagerRecord(manager_id, origin, fence, handle, manager_key, type_name)
        mutations.set(self._managers, manager_id, record)
        mutations.set(self._manager_by_origin, key, manager_id)
        mutations.set(self._manager_by_key, (fence, manager_key), manager_id)
        self._remaining_timeout(deadline)
        return self._manager_descriptor(record, runtime_id, runtime_generation)

    @staticmethod
    def _manager_descriptor(
        record: _ManagerRecord, runtime_id: str, runtime_generation: int
    ) -> TemporaryTableManagerDescriptor:
        return TemporaryTableManagerDescriptor(
            record.manager_id,
            record.origin,
            record.fence,
            ("list_tables", "inspect_table", "materialize_table"),
        )

    def _tables(
        self,
        backend: CaptureFrameBackend,
        registry: ProxyRegistry,
        fence: CaptureFence,
        manager: _ManagerRecord,
        name: str,
        runtime_id: str,
        runtime_generation: int,
        context_generation: int,
        limit: int,
        selection: ValueSelection | None,
        deadline: float | None,
        publication: _CapturePublicationCheckpoint,
        mutations: _CaptureInspectionMutationJournal,
    ) -> tuple[TemporaryTableDescriptor, ...]:
        self._remaining_timeout(deadline)
        identity = (fence, manager.manager_id, name.casefold(), selection)
        cached = self._table_descriptors.get(identity)
        if cached is not None:
            try:
                registry.resolve(cached.table_id)
            except ReleasedProxy:
                handle = self._table_handles.get(identity)
                if handle is None:
                    raise StaleProxy("released temporary-table selection cannot be recreated")
                backend.validate_value_reference(handle)
                selection_key = (
                    "full"
                    if selection is None
                    else sha256(repr(selection).encode("utf-8")).hexdigest()[:16]
                )
                proxy = registry.register_frame(
                    qualified_name=(
                        f"capture.{fence.capture_intent_id}."
                        f"{manager.manager_id}.{name}.{selection_key}"
                    ),
                    type_name=(
                        "ТаблицаЗначений"
                        if selection is not None
                        else "ОписаниеВременнойТаблицы"
                    ),
                    runtime_id=runtime_id,
                    runtime_generation=runtime_generation,
                    context_generation=context_generation,
                    capture_fence=fence,
                    provenance=ProxyProvenance(
                        "capture", fence.source_revision, fence.source_sha256, fence.operation_id
                    ),
                    resolver_handle=handle,
                    capabilities=cached.capabilities,
                    known_size=cached.known_size,
                    _publication=publication,
                )
                cached = TemporaryTableDescriptor(
                    proxy.proxy_id,
                    cached.manager_id,
                    cached.name,
                    cached.fence,
                    cached.schema,
                    cached.known_size,
                    proxy.capabilities,
                )
                mutations.set(self._table_proxies, identity, proxy.proxy_id)
                mutations.set(self._table_descriptors, identity, cached)
            return (cached,)
        remaining = self._remaining_timeout(deadline)
        if remaining is None:
            raw_tables = backend.temporary_tables(
                fence, manager.handle, names=(name,), cursor=0, limit=limit,
                selection=selection,
            )
        else:
            raw_tables = backend.temporary_tables(
                fence, manager.handle, names=(name,), cursor=0, limit=limit,
                selection=selection, timeout_s=remaining,
            )
        self._remaining_timeout(deadline)
        page, _total, _next = self._page(raw_tables, 0, limit, total_required=False)
        result: list[TemporaryTableDescriptor] = []
        seen: set[str] = set()
        for raw in page:
            self._remaining_timeout(deadline)
            if not isinstance(raw, Mapping) or raw.get("name") != name:
                continue
            schema = raw.get("schema", ())
            known_size = raw.get("known_size")
            handle = raw.get("handle")
            if not isinstance(handle, str) or not handle or not isinstance(schema, Sequence) or isinstance(schema, str):
                raise ValueError("temporary table metadata is invalid")
            backend.validate_value_reference(handle)
            if known_size is not None and not isinstance(known_size, ValueSize):
                raise TypeError("temporary table known_size must be a ValueSize")
            # A bounded table-row projection has a different native resolver
            # handle than the full table.  Keep it independently fenced and
            # stable for the exact selector instead of aliasing either order.
            proxy_key = identity
            selection_key = (
                "full"
                if selection is None
                else sha256(repr(selection).encode("utf-8")).hexdigest()[:16]
            )
            existing = self._table_proxies.get(proxy_key)
            proxy = (
                registry.resolve(existing)
                if existing is not None
                else registry.register_frame(
                    qualified_name=f"capture.{fence.capture_intent_id}.{manager.manager_id}.{name}.{selection_key}",
                    type_name=(
                        "ТаблицаЗначений"
                        if selection is not None
                        else "ОписаниеВременнойТаблицы"
                    ),
                    runtime_id=runtime_id,
                    runtime_generation=runtime_generation,
                    context_generation=context_generation,
                    capture_fence=fence,
                    provenance=ProxyProvenance("capture", fence.source_revision, fence.source_sha256, fence.operation_id),
                    resolver_handle=handle,
                    capabilities=(
                        _TABLE_CAPABILITIES
                        if selection is not None
                        else _TABLE_METADATA_CAPABILITIES
                    ),
                    known_size=known_size,
                    _publication=publication,
                )
            )
            mutations.set(self._table_proxies, proxy_key, proxy.proxy_id)
            mutations.set(self._table_handles, identity, handle)
            if proxy.proxy_id in seen:
                continue
            seen.add(proxy.proxy_id)
            descriptor = TemporaryTableDescriptor(
                proxy.proxy_id, manager.manager_id, name, fence, tuple(schema), known_size, proxy.capabilities
            )
            mutations.set(self._table_descriptors, identity, descriptor)
            result.append(descriptor)
        if not result:
            raise ValueError("temporary table is unknown")
        self._remaining_timeout(deadline)
        return tuple(result)

    @staticmethod
    def _inspection_deadline(timeout_s: float | None) -> float | None:
        if timeout_s is None:
            return None
        if (
            isinstance(timeout_s, bool)
            or not isinstance(timeout_s, (int, float))
            or not isfinite(float(timeout_s))
            or float(timeout_s) <= 0
        ):
            raise ValueError("capture inspection timeout must be finite and positive")
        return monotonic() + float(timeout_s)

    @staticmethod
    def _remaining_timeout(deadline: float | None) -> float | None:
        if deadline is None:
            return None
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("capture inspection deadline exceeded")
        return remaining

    @staticmethod
    def _page(
        raw: object, cursor: int, limit: int, *, total_required: bool
    ) -> tuple[tuple[Mapping[str, object], ...], int, int | None]:
        if not isinstance(raw, Mapping) or set(raw) - {"items", "total", "next_cursor"}:
            raise TypeError("capture metadata page is invalid")
        items = raw.get("items")
        if isinstance(items, str) or not isinstance(items, Sequence) or len(items) > limit:
            raise ValueError("capture metadata page exceeds limit")
        if any(not isinstance(item, Mapping) for item in items):
            raise TypeError("capture metadata item is invalid")
        total = raw.get("total", cursor + len(items))
        next_cursor = raw.get("next_cursor")
        if type(total) is not int or total < cursor + len(items):
            raise ValueError("capture metadata total is incoherent")
        if total_required and "total" not in raw:
            raise ValueError("frame metadata page requires total")
        if next_cursor is not None and (type(next_cursor) is not int or next_cursor != cursor + len(items) or next_cursor >= total):
            raise ValueError("capture metadata next cursor is incoherent")
        if next_cursor is None and total != cursor + len(items):
            raise ValueError("terminal capture metadata page is incoherent")
        return tuple(items), total, next_cursor

    def _manager_record(self, manager_id: str, fence: CaptureFence) -> _ManagerRecord:
        record = self._managers.get(manager_id)
        if record is None:
            raise ValueError("temporary-table manager is unknown")
        if record.fence != fence or self._active_fence != fence:
            raise StaleProxy("temporary-table manager is stale")
        return record

    def _require_active(self, fence: CaptureFence) -> None:
        if self._active_fence != fence:
            raise StaleProxy("capture fence is stale")

    @staticmethod
    def _matches(name: str, type_name: str, role: str, filters: Mapping[str, object]) -> bool:
        for key, value, candidate in (("name", filters.get("name"), name), ("type", filters.get("type"), type_name), ("role", filters.get("role"), role)):
            if value is not None and (not isinstance(value, str) or not value or value.casefold() not in candidate.casefold()):
                return False
        return True

    @_serialized_capture
    def request_operation(self, request_id: str, fingerprint: str) -> str | None:
        existing = self._requests.get(request_id)
        if existing is None:
            return None
        if existing[0] != fingerprint:
            raise ValueError("capture request_id names different inputs")
        return existing[1]

    @_serialized_capture
    def record_request(self, request_id: str, fingerprint: str, operation_id: str) -> None:
        if self.request_operation(request_id, fingerprint) is not None:
            return
        self._append({"request_id": request_id, "fingerprint": fingerprint, "operation_id": operation_id})
        self._requests[request_id] = (fingerprint, operation_id)

    @_serialized_capture
    def run_until(
        self,
        backend: CaptureRuntimeBackend,
        *,
        operation_id: str,
        prepared_main: object,
        source_revision: int,
        source_sha256: str,
        points: tuple[CapturePointRequest, ...],
    ) -> CaptureRunResult:
        resolved = backend.resolve_capture_points(points)
        if len(resolved) != len(points) or {item.name for item in resolved} != {
            item.name for item in points
        }:
            raise ValueError("capture resolution must return each requested named point")
        if len({item.name.casefold() for item in resolved}) != len(resolved):
            raise ValueError("capture resolution returned duplicate names")
        intent = CaptureIntent(
            capture_intent_id=f"capture_{uuid4().hex}",
            operation_id=operation_id,
            source_revision=source_revision,
            source_sha256=source_sha256,
            capture_generation=self._generation + 1,
            points=resolved,
        )
        self._journal_intent(intent)
        try:
            arming = backend.arm_capture(intent)
        except BaseException:
            return self._failed_arm_cleanup(backend, intent, "capture_arm_failed")
        if (
            not isinstance(arming, CaptureArming)
            or not arming.ticket_id
            or type(arming.expected_controller_operation_id) is not int
            or arming.expected_controller_operation_id <= 0
            or type(arming.expected_stop_sequence) is not int
            or arming.expected_stop_sequence <= 0
        ):
            return self._failed_arm_cleanup(backend, intent, "capture_arm_unknown")
        try:
            self._journal_arming(intent, arming)
        except BaseException:
            return self._failed_arm_cleanup(backend, intent, "capture_journal_failed")
        try:
            outcome = backend.run_prepared_main_until_capture(
                prepared_main, intent=intent
            )
        except BaseException:
            outcome = CaptureRunOutcome(
                BackendExecution(AgentOperationState.UNKNOWN, (), False, "capture_transport")
            )
        if outcome.stop is None:
            result = self._no_stop(backend, intent, outcome)
        else:
            result = self._correlate(intent, arming, outcome)
        return replace(
            result,
            user_main_dispatched=(
                outcome.user_main_dispatched is True or outcome.stop is not None
            ),
        )

    @_serialized_capture
    def continue_capture(
        self,
        backend: CaptureRuntimeBackend,
        registry: ProxyRegistry,
        *,
        fence: CaptureFence,
        operation_id: str,
        next_points: tuple[CapturePointRequest, ...],
    ) -> CaptureRunResult:
        """Flush one paused fence then send its single irreversible Continue.

        Preparing a next stop is deliberately complete before the durable
        ``continue_sent`` marker.  A failed preparation is therefore proven
        pre-send and leaves the original paused capture usable; every path
        after that marker revokes the old frame immediately.
        """
        current = self.current_capture(fence)
        if len(current.dirty_roots) > 100:
            raise ValueError("capture continuation dirty_roots exceeds 100")
        if not isinstance(registry, ProxyRegistry):
            raise TypeError("continuation requires a ProxyRegistry")
        if not isinstance(operation_id, str) or not operation_id:
            raise ValueError("continuation operation_id must be non-empty")
        snapshot = self._continuation_snapshot(registry)
        attempt = CaptureContinuationAttempt(
            f"continue_{uuid4().hex}",
            fence.capture_generation,
            operation_id,
            current.dirty_roots,
        )
        intent: CaptureIntent | None = None
        if next_points:
            resolved = backend.resolve_capture_points(next_points)
            if len(resolved) != len(next_points) or {item.name for item in resolved} != {
                item.name for item in next_points
            }:
                raise ValueError("continuation resolution must return each requested named point")
            if any(
                item.source_revision != fence.source_revision
                or item.source_sha256 != fence.source_sha256
                for item in resolved
            ):
                raise ValueError("continuation capture source changed before transport")
            intent = CaptureIntent(
                capture_intent_id=f"capture_{uuid4().hex}",
                operation_id=operation_id,
                source_revision=fence.source_revision,
                source_sha256=fence.source_sha256,
                capture_generation=max(self._generation, fence.capture_generation) + 1,
                points=resolved,
            )
        admission: _ContinuationAdmission | None = None
        try:
            if intent is not None:
                self._journal_intent(intent)
            backend_admission = backend.prepare_capture_successor(
                intent, attempt=attempt
            )
            admission = _ContinuationAdmission(
                self, registry, snapshot, backend_admission
            )
        except CaptureSuccessorPreparationError as error:
            if not error.uncertain:
                return CaptureRunResult(
                    BackendExecution(
                        AgentOperationState.FAILED,
                        (),
                        False,
                        "capture_arm_failed",
                    ),
                    failure={"stage": "capture_arming", "partial_results": {}},
                )
            with suppress(BaseException):
                backend.quarantine_capture_inspection(fence)
            self.invalidate_capture(registry)
            return self._quarantined_continuation_result(
                operation_id,
                current.dirty_roots,
                runtime_state="capture_arm_unknown",
            )
        except BaseException:
            # Preparation may have crossed a sink or transport boundary before
            # producing an admission token. Fail closed at every owner.
            with suppress(BaseException):
                backend.quarantine_capture_inspection(fence)
            self.invalidate_capture(registry)
            return self._quarantined_continuation_result(
                operation_id,
                current.dirty_roots,
                runtime_state="capture_arm_unknown",
            )

        arming = admission.arming
        evidence_valid = (
            arming is None
            if intent is None
            else isinstance(arming, CaptureArming)
            and bool(arming.ticket_id)
            and type(arming.expected_controller_operation_id) is int
            and arming.expected_controller_operation_id > 0
            and type(arming.expected_stop_sequence) is int
            and arming.expected_stop_sequence > fence.stop_sequence
        )
        if not evidence_valid:
            try:
                admission.rollback()
            except BaseException:
                with suppress(BaseException):
                    admission.quarantine()
                return self._quarantined_continuation_result(
                    operation_id,
                    current.dirty_roots,
                    runtime_state="capture_arm_restore_unknown",
                )
            return CaptureRunResult(
                BackendExecution(
                    AgentOperationState.FAILED, (), False, "capture_arm_failed"
                ),
                failure={"stage": "capture_arming", "partial_results": {}},
            )

        try:
            if intent is not None:
                assert arming is not None
                self._journal_arming(intent, arming)
            self._journal_continuation(fence, current.dirty_roots, "planned")
        except BaseException:
            # A failed fsync/write can have persisted an unknown prefix.  It is
            # not a normal failed arm even if physical rollback appears to work.
            with suppress(BaseException):
                admission.quarantine()
            return self._quarantined_continuation_result(
                operation_id,
                current.dirty_roots,
                runtime_state="capture_journal_unknown",
            )

        # The service-side fence is snapshotted and revoked before entering the
        # backend.  A proven pre-send failure may restore it; possible send may
        # only quarantine or commit the invalidation.
        self.invalidate_capture(registry)
        try:
            outcome = backend.continue_capture(
                dirty_roots=current.dirty_roots,
                attempt_id=attempt.attempt_id,
            )
        except BaseException:
            with suppress(BaseException):
                admission.quarantine()
            with suppress(BaseException):
                self._journal_continuation(fence, current.dirty_roots, "unknown")
            return self._quarantined_continuation_result(
                operation_id,
                current.dirty_roots,
                partial_results={
                    root: "outcome_unknown" for root in current.dirty_roots
                },
                continue_state="outcome_unknown",
                runtime_state="capture_transport",
            )

        allowed_statuses = {
            "unattempted",
            "failed",
            "sent",
            "succeeded",
            "outcome_unknown",
        }
        raw_continuation_evidence = (
            outcome.continuation
            if type(outcome.continuation) is CaptureContinuationEvidence
            else None
        )
        continuation_evidence: CaptureContinuationEvidence | None = None
        if raw_continuation_evidence is not None:
            try:
                # Revalidate the controller boundary even though the adapter
                # normally constructs this frozen type.  Corrupt persisted or
                # transport data must quarantine rather than escape or become
                # acknowledgement evidence.
                candidate_evidence = CaptureContinuationEvidence(
                    tuple(raw_continuation_evidence.root_statuses),
                    raw_continuation_evidence.continue_state,
                )
            except (AttributeError, TypeError, ValueError):
                pass
            else:
                reported_roots = tuple(
                    root.casefold()
                    for root, _status in candidate_evidence.root_statuses
                )
                staged_roots = tuple(root.casefold() for root in current.dirty_roots)
                if reported_roots == staged_roots:
                    continuation_evidence = candidate_evidence
        reported_statuses: dict[str, str] = {}
        coarse_evidence_valid = type(outcome.partial_results) is tuple and not (
            outcome.partial_results
        )
        if isinstance(outcome.partial_results, Mapping):
            try:
                coarse_items = tuple(outcome.partial_results.items())
                coarse_roots = tuple(
                    root.casefold()
                    for root, status in coarse_items
                    if (
                        isinstance(root, str)
                        and root.isidentifier()
                        and len(root) <= 256
                        and isinstance(status, str)
                        and status in allowed_statuses
                    )
                )
                staged_roots = tuple(root.casefold() for root in current.dirty_roots)
                coarse_evidence_valid = (
                    len(coarse_items) <= 100
                    and len(coarse_roots) == len(coarse_items)
                    and coarse_roots == staged_roots
                )
                if coarse_evidence_valid:
                    reported_statuses.update(
                        (root.casefold(), status)
                        for root, status in coarse_items
                    )
            except BaseException:
                # The active capture was invalidated before this untrusted
                # controller reply crossed the boundary. Any malformed mapping
                # must fall through to quarantine instead of escaping cleanup.
                coarse_evidence_valid = False
        if raw_continuation_evidence is not None:
            raw_statuses = getattr(raw_continuation_evidence, "root_statuses", ())
            if isinstance(raw_statuses, (tuple, list)):
                for item in raw_statuses:
                    if (
                        isinstance(item, tuple)
                        and len(item) == 2
                        and isinstance(item[0], str)
                        and isinstance(item[1], str)
                        and item[1] in allowed_statuses
                    ):
                        reported_statuses[item[0].casefold()] = item[1]
        evidence_complete = (
            continuation_evidence is not None and coarse_evidence_valid
        )
        missing_status = (
            "outcome_unknown"
            if not evidence_complete
            else "unavailable"
        )
        exact_statuses = {
            root: (
                reported_statuses[root.casefold()]
                if reported_statuses.get(root.casefold()) in allowed_statuses
                else missing_status
            )
            for root in current.dirty_roots
        }
        continue_state = (
            continuation_evidence.continue_state
            if evidence_complete and continuation_evidence is not None
            else "outcome_unknown"
        )
        if not evidence_complete:
            # A backend reply without exact attempt evidence proves neither a
            # failed pre-send nor an acknowledged Continue.  Missing roots and
            # extra roots are malformed attempt evidence too.  Fail closed even
            # if the coarse execution reply claims a terminal outcome, while
            # retaining any valid bounded root statuses that are available.
            with suppress(BaseException):
                admission.quarantine()
            return self._quarantined_continuation_result(
                operation_id,
                current.dirty_roots,
                partial_results=exact_statuses,
                continue_state="outcome_unknown",
                runtime_state=outcome.execution.runtime_state,
            )
        if outcome.execution.runtime_state == "partial_writeback_failure":
            safe_to_restore = continue_state == "unattempted" and not any(
                status in {"sent", "succeeded", "outcome_unknown"}
                for status in exact_statuses.values()
            )
            if safe_to_restore:
                try:
                    admission.rollback()
                except BaseException:
                    with suppress(BaseException):
                        admission.quarantine()
                    return self._quarantined_continuation_result(
                        operation_id,
                        current.dirty_roots,
                        partial_results=exact_statuses,
                        continue_state=continue_state,
                        runtime_state="capture_restore_unknown",
                    )
                return CaptureRunResult(
                    outcome.execution,
                    failure={
                        "stage": "capture_writeback",
                        "partial_results": exact_statuses,
                        "continue_state": continue_state,
                    },
                )
            with suppress(BaseException):
                admission.quarantine()
            return self._quarantined_continuation_result(
                operation_id,
                current.dirty_roots,
                partial_results=exact_statuses,
                continue_state=(
                    continue_state
                    if continue_state in {"acknowledged", "unattempted"}
                    else "outcome_unknown"
                ),
                runtime_state="capture_writeback_unknown",
            )

        if continue_state != "acknowledged":
            # ``planned`` and ``sent`` are durable transport markers, not an
            # acknowledgement from the controller.  No terminal backend label
            # may upgrade either one into proof that Continue completed.  An
            # exact ``unattempted`` marker remains useful proof that Continue
            # itself was not sent, even when earlier root writes force runtime
            # quarantine.
            with suppress(BaseException):
                admission.quarantine()
            return self._quarantined_continuation_result(
                operation_id,
                current.dirty_roots,
                partial_results=exact_statuses,
                continue_state=(
                    "unattempted"
                    if continue_state == "unattempted"
                    else "outcome_unknown"
                ),
                runtime_state=outcome.execution.runtime_state,
            )

        if outcome.execution.terminal_state is AgentOperationState.UNKNOWN:
            with suppress(BaseException):
                admission.quarantine()
            return self._quarantined_continuation_result(
                operation_id,
                current.dirty_roots,
                partial_results=exact_statuses,
                continue_state=continue_state,
                runtime_state=outcome.execution.runtime_state,
            )

        if intent is None or arming is None:
            result = self._no_stop_without_disarm(
                operation_id, current.dirty_roots, outcome
            )
        elif outcome.stop is None:
            result = self._no_stop(backend, intent, outcome)
        else:
            result = self._correlate(intent, arming, outcome)
        if result.execution.terminal_state is AgentOperationState.UNKNOWN:
            with suppress(BaseException):
                admission.quarantine()
            stage = (
                result.failure.get("stage", "capture_transport")
                if result.failure is not None
                else "capture_transport"
            )
            quarantined = self._quarantined_continuation_result(
                operation_id,
                current.dirty_roots,
                partial_results=exact_statuses,
                continue_state=continue_state,
                runtime_state=result.execution.runtime_state,
            )
            assert quarantined.failure is not None
            quarantined.failure["stage"] = stage
            return quarantined
        admission.commit()
        return result

    @staticmethod
    def _quarantined_continuation_result(
        operation_id: str,
        dirty_roots: tuple[str, ...],
        *,
        partial_results: Mapping[str, str] | None = None,
        continue_state: str = "unattempted",
        runtime_state: str,
    ) -> CaptureRunResult:
        statuses = (
            dict(partial_results)
            if partial_results is not None
            else {root: "unattempted" for root in dirty_roots}
        )
        return CaptureRunResult(
            BackendExecution(
                AgentOperationState.UNKNOWN, (), False, runtime_state
            ),
            failure={
                "stage": "capture_transport",
                "partial_results": statuses,
                "continue_state": continue_state,
            },
            recovery=_capture_operation_recovery(
                operation_id,
                include_workspace_status=True,
                include_restart=True,
            ),
            quarantine_runtime=True,
        )

    @staticmethod
    def _no_stop_without_disarm(
        operation_id: str,
        dirty_roots: tuple[str, ...],
        outcome: CaptureRunOutcome,
    ) -> CaptureRunResult:
        if outcome.execution.terminal_state is AgentOperationState.UNKNOWN:
            return CaptureRunResult(
                outcome.execution,
                failure={"stage": "capture_transport", "partial_results": {root: "unknown" for root in dirty_roots}},
                recovery=_capture_operation_recovery(operation_id),
            )
        if outcome.execution.terminal_state is AgentOperationState.CAPTURED:
            return CaptureRunResult(
                BackendExecution(AgentOperationState.UNKNOWN, (), False, "capture_correlation_unknown"),
                failure={"stage": "capture_correlation", "partial_results": {root: "unknown" for root in dirty_roots}},
                recovery=_capture_operation_recovery(operation_id),
            )
        return CaptureRunResult(outcome.execution)

    @staticmethod
    def _failed_arm_cleanup(
        backend: CaptureRuntimeBackend,
        intent: CaptureIntent,
        runtime_state: str,
    ) -> CaptureRunResult:
        try:
            backend.disarm_capture(policy="terminal_no_stop")
        except BaseException:
            return CaptureRunResult(
                BackendExecution(
                    AgentOperationState.UNKNOWN,
                    (),
                    False,
                    "capture_arm_cleanup_unknown",
                ),
                failure={"stage": "capture_arming", "partial_results": {}},
                recovery=_capture_operation_recovery(intent.operation_id),
            )
        return CaptureRunResult(
            BackendExecution(AgentOperationState.FAILED, (), False, runtime_state),
            failure={"stage": "capture_arming", "partial_results": {}},
        )

    @staticmethod
    def _location_identity(point: ResolvedCapturePoint) -> tuple[object, ...]:
        return (
            point.project,
            point.module,
            point.procedure,
            point.line,
            point.executable_line,
            point.source_revision,
            point.source_sha256,
        )

    def _no_stop(
        self,
        backend: CaptureRuntimeBackend,
        intent: CaptureIntent,
        outcome: CaptureRunOutcome,
    ) -> CaptureRunResult:
        if outcome.execution.terminal_state is AgentOperationState.CAPTURED:
            return CaptureRunResult(
                BackendExecution(
                    AgentOperationState.UNKNOWN,
                    (),
                    False,
                    "capture_correlation_unknown",
                ),
                failure={"stage": "capture_correlation", "partial_results": {}},
                recovery=_capture_operation_recovery(intent.operation_id),
            )
        try:
            backend.disarm_capture(policy="terminal_no_stop")
        except BaseException:
            if outcome.execution.runtime_state == "debug_stopped":
                return CaptureRunResult(
                    outcome.execution,
                    failure={"stage": "unexpected_breakpoint", "partial_results": {}},
                    recovery=_capture_operation_recovery(intent.operation_id),
                )
            return CaptureRunResult(
                BackendExecution(AgentOperationState.UNKNOWN, (), False, "capture_disarm_unknown"),
                failure={"stage": "capture_disarm", "partial_results": {}},
                recovery=_capture_operation_recovery(intent.operation_id),
            )
        if outcome.execution.runtime_state == "debug_stopped":
            return CaptureRunResult(
                outcome.execution,
                failure={"stage": "unexpected_breakpoint", "partial_results": {}},
                recovery=_capture_operation_recovery(
                    intent.operation_id,
                    include_workspace_status=True,
                ),
            )
        if outcome.execution.terminal_state is AgentOperationState.UNKNOWN:
            return CaptureRunResult(
                outcome.execution,
                failure={"stage": "capture_transport", "partial_results": {}},
                recovery=_capture_operation_recovery(intent.operation_id),
            )
        return CaptureRunResult(outcome.execution)

    def _correlate(
        self, intent: CaptureIntent, arming: CaptureArming, outcome: CaptureRunOutcome
    ) -> CaptureRunResult:
        stop = outcome.stop
        assert stop is not None
        if (
            outcome.execution.terminal_state is not AgentOperationState.CAPTURED
            or stop.controller_operation_id != arming.expected_controller_operation_id
            or stop.observed_command_id != arming.expected_controller_operation_id
            or stop.ticket_id != arming.ticket_id
            or stop.stop_sequence != arming.expected_stop_sequence
            or stop.location not in intent.points
            or not any(
                self._location_identity(stop.location) == self._location_identity(point)
                for point in intent.points
            )
        ):
            return CaptureRunResult(
                BackendExecution(
                    AgentOperationState.UNKNOWN,
                    (),
                    False,
                    "capture_correlation_unknown",
                ),
                failure={"stage": "capture_correlation", "partial_results": {}},
                recovery=_capture_operation_recovery(intent.operation_id),
            )
        self._generation = intent.capture_generation
        fence = CaptureFence(
            capture_intent_id=intent.capture_intent_id,
            operation_id=intent.operation_id,
            source_revision=stop.location.source_revision,
            source_sha256=stop.location.source_sha256,
            capture_generation=intent.capture_generation,
            stop_sequence=stop.stop_sequence,
        )
        return CaptureRunResult(
            outcome.execution,
            capture=CaptureView(fence=fence, location=stop.location, inspection=None),
        )

    def _journal_intent(self, intent: CaptureIntent) -> None:
        payload = {
            "capture_intent_id": intent.capture_intent_id,
            "operation_id": intent.operation_id,
            "source_revision": intent.source_revision,
            "source_sha256": intent.source_sha256,
            "capture_generation": intent.capture_generation,
            "points": [
                {
                    "name": point.name,
                    "project": point.project,
                    "module": point.module,
                    "procedure": point.procedure,
                    "line": point.line,
                    "executable_line": point.executable_line,
                    "source_revision": point.source_revision,
                    "source_sha256": point.source_sha256,
                }
                for point in intent.points
            ],
        }
        self._append(payload)

    def _journal_arming(self, intent: CaptureIntent, arming: CaptureArming) -> None:
        self._append(
            {
                "capture_intent_id": intent.capture_intent_id,
                "runtime_ticket": arming.ticket_id,
                "expected_controller_operation_id": arming.expected_controller_operation_id,
                "expected_stop_sequence": arming.expected_stop_sequence,
                "source_revision": intent.source_revision,
                "source_sha256": intent.source_sha256,
                "capture_generation": intent.capture_generation,
            }
        )

    def _journal_continuation(
        self, fence: CaptureFence, dirty_roots: tuple[str, ...], status: str
    ) -> None:
        if status not in {"planned", "sent", "acknowledged", "unknown"}:
            raise ValueError("continuation journal status is invalid")
        self._append(
            {
                "continuation": status,
                "capture_intent_id": fence.capture_intent_id,
                "capture_generation": fence.capture_generation,
                "operation_id": fence.operation_id,
                "dirty_roots": list(dirty_roots),
            }
        )

    def _append(self, payload: dict[str, object]) -> None:
        self._journal_path.parent.mkdir(parents=True, exist_ok=True)
        with self._journal_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _load_generation(self) -> int:
        try:
            lines = self._journal_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return 0
        generation = 0
        for line in lines:
            try:
                item = json.loads(line)
                value = item.get("capture_generation") if isinstance(item, dict) else None
                if type(value) is int and value > generation:
                    generation = value
            except json.JSONDecodeError:
                continue
        return generation

    def _load_requests(self) -> dict[str, tuple[str, str]]:
        try:
            lines = self._journal_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return {}
        result: dict[str, tuple[str, str]] = {}
        for line in lines:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            request_id = item.get("request_id")
            fingerprint = item.get("fingerprint")
            operation_id = item.get("operation_id")
            if all(isinstance(value, str) and value for value in (request_id, fingerprint, operation_id)):
                result[request_id] = (fingerprint, operation_id)
        return result


__all__ = [
    "CaptureIntent",
    "CaptureArming",
    "CaptureSuccessorPreparationError",
    "CaptureRunOutcome",
    "CaptureRunResult",
    "CaptureRuntimeBackend",
    "CaptureService",
    "CaptureStop",
]
