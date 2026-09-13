"""Durable, single-lane execution records for the agent runtime service."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from dataclasses import dataclass, field, fields, replace
from hashlib import sha256
import json
from math import isfinite
import os
from pathlib import Path
from threading import Condition, Lock, RLock
from time import monotonic
from typing import cast
from uuid import uuid4

from onec_runtime_mcp.agent.contracts import (
    AgentOperationState,
    BackendExecution,
    MAX_MESSAGE_LENGTH,
    MAX_OPERATION_MESSAGES,
    OperationDescriptor,
    OperationOutput,
    RetrySafety,
    to_wire,
)
from onec_runtime_mcp.agent.facade_contracts import (
    AgentDiagnosticView,
    AgentOperationKind,
    MAX_DIAGNOSTIC_EXCERPT_LENGTH,
    OperationViewFacts,
    OperationViewSnapshot,
)
from onec_runtime_mcp.agent.observation import ObservationPlan
from onec_runtime.bsl import NormalizedDiagnostic
from onec_runtime.bsl.source_maps import SourceUnitKind
from onec_runtime.privacy import bounded_platform_diagnostic
from onec_runtime.runtime_contracts import (
    MAX_DIAGNOSTIC_COORDINATE,
    MAX_DIAGNOSTIC_LABEL_LENGTH,
    MAX_PRIVATE_DIAGNOSTIC_LENGTH,
    OperationExecutionProvenance,
    sanitize_normalized_diagnostic,
)


MAX_WAIT_SECONDS = 60.0
_DIAGNOSTIC_SUMMARIES = {
    "parsing": "BSL parsing failed",
    "lowering": "BSL lowering failed",
    "compilation": "BSL compilation failed",
    "execution": "BSL execution failed",
}


@dataclass(frozen=True, slots=True)
class _Command:
    operation_kind: AgentOperationKind
    runtime_id: str
    runtime_generation: int | None
    code_id: str | None
    revision: int | None
    source_sha256: str | None
    inputs_sha256: str
    request_id: str | None = None
    observation_plan: ObservationPlan | None = None
    startup_profile: str | None = None
    startup_mode: str | None = None

    @property
    def idempotency_key(
        self,
    ) -> tuple[
        str,
        str,
        int | None,
        str | None,
        int | None,
        str | None,
        str,
        str | None,
    ]:
        return (
            self.operation_kind.value,
            self.runtime_id,
            self.runtime_generation,
            self.code_id,
            self.revision,
            self.source_sha256,
            self.inputs_sha256,
            self.request_id,
        )


@dataclass(slots=True)
class _Operation:
    operation_id: str
    command: _Command
    state: AgentOperationState
    messages: list[tuple[int, str]] = field(default_factory=list)
    result_present: bool = False
    last_event_cursor: int = 0
    future: Future[None] | None = None
    view_facts: OperationViewFacts = field(default_factory=OperationViewFacts)
    execution_provenance: OperationExecutionProvenance | None = None
    private_diagnostic_ref: "_PrivateDiagnosticRef | None" = None


@dataclass(frozen=True, slots=True)
class _PrivateDiagnosticRef:
    """Hash-only public-journal anchor; never part of compact projections."""

    diagnostic_id: str
    content_integrity_sha256: str

    def __post_init__(self) -> None:
        _require_sha256(self.diagnostic_id, name="diagnostic_id")
        _require_sha256(
            self.content_integrity_sha256,
            name="content_integrity_sha256",
        )

    @classmethod
    def from_wire(cls, value: object) -> "_PrivateDiagnosticRef":
        if not isinstance(value, Mapping) or set(value) != {
            "diagnostic_id",
            "content_integrity_sha256",
        }:
            raise ValueError("private diagnostic reference has an invalid shape")
        return cls(
            diagnostic_id=value.get("diagnostic_id"),  # type: ignore[arg-type]
            content_integrity_sha256=value.get(  # type: ignore[arg-type]
                "content_integrity_sha256"
            ),
        )


def _private_diagnostic_content_integrity(
    value: Mapping[str, object],
) -> str:
    encoded = json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class _PrivateDiagnosticRecord:
    """Bounded expert fields, isolated from the public operation journal."""

    diagnostic_id: str
    code: str | None
    source_unit_kind: str | None
    source_unit_id: str | None
    source_revision: int | None
    visible_source_sha256: str | None
    lowered_line: int | None
    lowered_column: int | None
    lowered_offset: int | None
    lowered_span_start: int | None
    lowered_span_end: int | None
    platform_diagnostic: str | None
    platform_diagnostic_sha256: str | None
    platform_diagnostic_truncated: bool
    platform_diagnostic_redacted: bool
    execution_artifact_sha256: str | None
    source_map_sha256: str | None
    excerpt: str | None
    content_integrity_sha256: str

    def __post_init__(self) -> None:
        _require_sha256(self.diagnostic_id, name="diagnostic_id")
        if self.code is not None and (
            type(self.code) is not str
            or not self.code
            or len(self.code) > MAX_DIAGNOSTIC_LABEL_LENGTH
        ):
            raise ValueError("private diagnostic code is invalid")
        source_values = (
            self.source_unit_kind,
            self.source_unit_id,
            self.source_revision,
            self.visible_source_sha256,
        )
        if any(value is not None for value in source_values):
            if (
                self.source_unit_kind not in {item.value for item in SourceUnitKind}
                or type(self.source_unit_id) is not str
                or not self.source_unit_id
                or len(self.source_unit_id) > 256
                or type(self.source_revision) is not int
                or not 0 <= self.source_revision <= MAX_DIAGNOSTIC_COORDINATE
                or self.visible_source_sha256 is None
            ):
                raise ValueError("private diagnostic source identity is invalid")
            _require_sha256(
                self.visible_source_sha256,
                name="visible_source_sha256",
            )
        lowered_values = (
            self.lowered_line,
            self.lowered_column,
            self.lowered_offset,
            self.lowered_span_start,
            self.lowered_span_end,
        )
        if any(value is not None for value in lowered_values):
            if (
                any(type(value) is not int for value in lowered_values)
                or not 1 <= self.lowered_line <= MAX_DIAGNOSTIC_COORDINATE  # type: ignore[operator]
                or not 1 <= self.lowered_column <= MAX_DIAGNOSTIC_COORDINATE  # type: ignore[operator]
                or not 0 <= self.lowered_offset <= MAX_DIAGNOSTIC_COORDINATE  # type: ignore[operator]
                or not 0 <= self.lowered_span_start <= self.lowered_span_end <= MAX_DIAGNOSTIC_COORDINATE  # type: ignore[operator]
            ):
                raise ValueError("private lowered location is invalid")
        if self.platform_diagnostic is not None and (
            type(self.platform_diagnostic) is not str
            or len(self.platform_diagnostic) > MAX_PRIVATE_DIAGNOSTIC_LENGTH
            or self.platform_diagnostic_sha256 is None
            or (
                self.platform_diagnostic_truncated
                and len(self.platform_diagnostic) != MAX_PRIVATE_DIAGNOSTIC_LENGTH
            )
            or (
                not self.platform_diagnostic_truncated
                and sha256(self.platform_diagnostic.encode("utf-8")).hexdigest()
                != self.platform_diagnostic_sha256
            )
        ):
            raise ValueError("private platform diagnostic is invalid")
        for name in (
            "platform_diagnostic_sha256",
            "execution_artifact_sha256",
            "source_map_sha256",
        ):
            value = getattr(self, name)
            if value is not None:
                _require_sha256(value, name=name)
        if type(self.platform_diagnostic_truncated) is not bool or type(
            self.platform_diagnostic_redacted
        ) is not bool:
            raise TypeError("private diagnostic flags must be exact booleans")
        if self.excerpt is not None and (
            type(self.excerpt) is not str
            or len(self.excerpt) > MAX_DIAGNOSTIC_EXCERPT_LENGTH
        ):
            raise ValueError("private diagnostic excerpt is invalid")
        _require_sha256(
            self.content_integrity_sha256,
            name="content_integrity_sha256",
        )
        content = {
            item.name: getattr(self, item.name)
            for item in fields(self)
            if item.name != "content_integrity_sha256"
        }
        if (
            _private_diagnostic_content_integrity(content)
            != self.content_integrity_sha256
        ):
            raise ValueError("private diagnostic content integrity is invalid")

    @classmethod
    def from_diagnostic(
        cls,
        diagnostic: NormalizedDiagnostic,
        *,
        excerpt: str | None,
    ) -> "_PrivateDiagnosticRecord":
        safe = sanitize_normalized_diagnostic(diagnostic)
        if safe is None:
            raise ValueError("diagnostic is malformed")
        unit = safe.source_unit
        lowered = safe.lowered_location
        content: dict[str, object] = {
            "diagnostic_id": safe.diagnostic_id,
            "code": safe.code,
            "source_unit_kind": None if unit is None else unit.kind.value,
            "source_unit_id": None if unit is None else unit.unit_id,
            "source_revision": None if unit is None else unit.revision,
            "visible_source_sha256": (
                None if unit is None else unit.source_sha256
            ),
            "lowered_line": None if lowered is None else lowered.line,
            "lowered_column": None if lowered is None else lowered.column,
            "lowered_offset": None if lowered is None else lowered.offset,
            "lowered_span_start": (
                None if lowered is None else lowered.span.start
            ),
            "lowered_span_end": None if lowered is None else lowered.span.end,
            "platform_diagnostic": safe.platform_diagnostic,
            "platform_diagnostic_sha256": safe.platform_diagnostic_sha256,
            "platform_diagnostic_truncated": (
                safe.platform_diagnostic_truncated
            ),
            "platform_diagnostic_redacted": safe.platform_diagnostic_redacted,
            "execution_artifact_sha256": safe.execution_artifact_sha256,
            "source_map_sha256": safe.source_map_sha256,
            "excerpt": excerpt,
        }
        return cls(
            **content,  # type: ignore[arg-type]
            content_integrity_sha256=_private_diagnostic_content_integrity(
                content
            ),
        )

    @classmethod
    def from_wire(cls, value: object) -> "_PrivateDiagnosticRecord":
        if not isinstance(value, Mapping):
            raise TypeError("private diagnostic record must be a mapping")
        expected = {item.name for item in fields(cls)}
        if set(value) != expected:
            raise ValueError("private diagnostic record has an invalid shape")
        return cls(**{name: value.get(name) for name in expected})  # type: ignore[arg-type]

    def to_wire(self) -> dict[str, object]:
        return {item.name: getattr(self, item.name) for item in fields(self)}

    def matches_operation(
        self,
        command: _Command,
        provenance: OperationExecutionProvenance | None,
    ) -> bool:
        source_values = (
            self.source_unit_kind,
            self.source_unit_id,
            self.source_revision,
            self.visible_source_sha256,
        )
        if any(value is None for value in source_values) or (
            self.source_unit_kind != SourceUnitKind.NOTEBOOK_CELL.value
            or self.source_unit_id != command.code_id
            or self.source_revision != command.revision
            or self.visible_source_sha256 != command.source_sha256
        ):
            return False
        if provenance is None:
            return (
                self.execution_artifact_sha256 is None
                and self.source_map_sha256 is None
            )
        return (
            self.execution_artifact_sha256
            == provenance.executed_source_sha256
            and self.source_map_sha256 == provenance.source_map_sha256
            and self.visible_source_sha256
            == provenance.visible_source_sha256
        )

    def matches_canonical_diagnostic_id(
        self,
        public: AgentDiagnosticView,
    ) -> bool:
        """Recompute platform-backed diagnostic identity where possible."""
        if self.platform_diagnostic_sha256 is None:
            return True
        if (
            self.execution_artifact_sha256 is None
            or self.source_map_sha256 is None
        ):
            return False
        identity = "|".join(
            (
                public.stage,
                self.code or "platform",
                self.platform_diagnostic_sha256,
                self.execution_artifact_sha256,
                self.source_map_sha256,
                (
                    "unknown"
                    if self.lowered_offset is None
                    else str(self.lowered_offset)
                ),
            )
        )
        return (
            sha256(identity.encode("utf-8")).hexdigest()
            == self.diagnostic_id
        )

    def to_expert_wire(
        self,
        public: AgentDiagnosticView,
        provenance: OperationExecutionProvenance | None,
    ) -> dict[str, object]:
        platform, truncated, redacted = bounded_platform_diagnostic(
            self.platform_diagnostic,
            truncated=self.platform_diagnostic_truncated,
            redacted=self.platform_diagnostic_redacted,
        )
        visible = public.visible_location
        related = public.related_visible_span
        return {
            "diagnostic_id": public.diagnostic_id,
            "runtime_summary": _DIAGNOSTIC_SUMMARIES[public.stage],
            "stage": public.stage,
            "mapping_confidence": public.mapping_confidence,
            "visible_location": (
                None
                if visible is None
                else {
                    "line": visible["line"],
                    "column": visible["column"],
                    "span": dict(cast(Mapping[str, object], visible["span"])),
                }
            ),
            "related_visible_span": (
                None if related is None else dict(related)
            ),
            "excerpt": self.excerpt,
            "synthetic_region": public.synthetic_region,
            "lowered_location": (
                None
                if self.lowered_line is None
                else {
                    "line": self.lowered_line,
                    "column": self.lowered_column,
                    "offset": self.lowered_offset,
                    "span": {
                        "start": self.lowered_span_start,
                        "end": self.lowered_span_end,
                    },
                }
            ),
            "platform_diagnostic": platform,
            "platform_diagnostic_sha256": self.platform_diagnostic_sha256,
            "platform_diagnostic_truncated": truncated,
            "platform_diagnostic_redacted": redacted,
            "execution_artifact_sha256": self.execution_artifact_sha256,
            "source_map_sha256": self.source_map_sha256,
            "worker_generation": (
                None if provenance is None else provenance.worker_generation
            ),
            "worker_manifest_sha256": (
                None
                if provenance is None
                else provenance.worker_manifest_sha256
            ),
        }


def _require_sha256(value: object, *, name: str) -> None:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase sha256")


class OperationRegistry:
    """Persist operations before exposing their transitions to waiters.

    Backend calls pass through an independent one-wide lane even when an
    injected executor has several workers. Waiting only observes an operation;
    neither a timeout nor ``stop_waiting`` makes a second runtime call or
    attempts an ambiguous cancellation.
    """

    def __init__(
        self,
        state_root: str | Path,
        executor_factory: Callable[[], Executor] | None = None,
    ) -> None:
        self._state_root = Path(state_root)
        self._journal_path = self._state_root / ".runtime" / "agent-service" / "operations.jsonl"
        self._private_diagnostic_path = (
            self._state_root
            / ".runtime"
            / "agent-service"
            / "diagnostics.private.jsonl"
        )
        self._lock = RLock()
        self._changed = Condition(self._lock)
        self._execution_lane = Lock()
        self._operations: dict[str, _Operation] = {}
        self._private_diagnostics: dict[str, _PrivateDiagnosticRecord] | None = None
        self._private_diagnostic_conflicts: set[str] | None = None
        self._waiters: dict[str, set[str]] = {}
        self._stopped_waiters: set[tuple[str, str]] = set()
        self._next_cursor = 1
        self._executor_factory = executor_factory
        self._executor: Executor | None = None
        self._rehydrate()

    def submit(
        self,
        command: Mapping[str, object],
        execute: Callable[[], BackendExecution],
    ) -> OperationDescriptor:
        normalized = self._normalize_command(command)
        if not callable(execute):
            raise TypeError("execute must be callable")
        with self._changed:
            for operation in self._operations.values():
                if (
                    operation.command.idempotency_key == normalized.idempotency_key
                    and operation.state in {AgentOperationState.QUEUED, AgentOperationState.RUNNING}
                ):
                    return self._descriptor(operation)
            operation = _Operation(str(uuid4()), normalized, AgentOperationState.QUEUED)
            self._operations[operation.operation_id] = operation
            self._record_locked(operation, "submitted")
            try:
                executor = self._executor_locked()
                operation.future = cast(Future[None], executor.submit(self._run, operation.operation_id, execute))
            except BaseException:
                self._transition_locked(operation, AgentOperationState.FAILED)
            return self._descriptor(operation)

    def submit_startup(
        self,
        command: Mapping[str, object],
        execute: Callable[[], BackendExecution],
    ) -> OperationDescriptor:
        """Publish one runtime-start command through the existing durable lane."""
        if not isinstance(command, Mapping):
            raise TypeError("command must be a mapping")
        supplied_kind = command.get("operation_kind")
        if supplied_kind not in {None, AgentOperationKind.RUNTIME_ENSURE.value}:
            raise ValueError("startup command has an incompatible operation kind")
        normalized = dict(command)
        normalized["operation_kind"] = AgentOperationKind.RUNTIME_ENSURE.value
        return self.submit(normalized, execute)

    def shutdown(self) -> None:
        """Release the service-owned executor without altering durable operations."""
        with self._changed:
            executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=False)

    def list(self, filters: Mapping[str, object] | None = None) -> tuple[OperationDescriptor, ...]:
        if filters is not None and not isinstance(filters, Mapping):
            raise TypeError("filters must be a mapping")
        filters = filters or {}
        allowed = {"runtime_id", "runtime_generation", "state"}
        if set(filters) - allowed:
            raise ValueError("unsupported operation filter")
        with self._changed:
            return tuple(
                self._descriptor(operation)
                for operation in self._operations.values()
                if self._matches(operation, filters)
            )

    def status(self, operation_id: str) -> OperationDescriptor:
        with self._changed:
            return self._descriptor(self._operation_locked(operation_id))

    def recoverable_startup(
        self,
    ) -> tuple[OperationDescriptor, str, str] | None:
        """Return unresolved startup ownership restored from the durable journal."""
        with self._changed:
            for operation in reversed(tuple(self._operations.values())):
                if (
                    operation.command.operation_kind
                    is AgentOperationKind.RUNTIME_ENSURE
                    and operation.state
                    in {
                        AgentOperationState.QUEUED,
                        AgentOperationState.RUNNING,
                        AgentOperationState.UNKNOWN,
                    }
                    and operation.command.startup_profile is not None
                    and operation.command.startup_mode is not None
                ):
                    return (
                        self._descriptor(operation),
                        operation.command.startup_profile,
                        operation.command.startup_mode,
                    )
            return None

    def capture_request_operation(
        self, request_id: str, fingerprint: str
    ) -> str | None:
        """Resolve a CAPTURE request from the durable operation journal.

        The operation submission is flushed before the capture request journal
        is attempted.  Keeping the public request identity on that submission
        therefore gives retries a truthful binding even when the secondary
        request journal fails after submit.
        """
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("request_id must be a non-empty string")
        if not isinstance(fingerprint, str) or not fingerprint:
            raise ValueError("fingerprint must be a non-empty string")
        with self._changed:
            matching = tuple(
                operation
                for operation in self._operations.values()
                if operation.command.request_id == request_id
            )
            if not matching:
                return None
            if any(
                operation.command.inputs_sha256 != fingerprint
                for operation in matching
            ):
                raise ValueError("capture request_id names different inputs")
            operation_ids = {operation.operation_id for operation in matching}
            if len(operation_ids) != 1:
                raise ValueError("capture request_id has ambiguous durable ownership")
            return matching[-1].operation_id

    def abandon_unknown_startup(self, operation_id: str) -> OperationDescriptor:
        """Durably abandon recovered startup ownership after explicit restart."""
        with self._changed:
            operation = self._operation_locked(operation_id)
            if (
                operation.command.operation_kind
                is not AgentOperationKind.RUNTIME_ENSURE
                or operation.state is not AgentOperationState.UNKNOWN
            ):
                raise ValueError("only an unknown startup can be abandoned")
            self._transition_locked(
                operation,
                AgentOperationState.FAILED,
                reason="explicit_restart_abandoned_unknown_startup",
            )
            return self._descriptor(operation)

    def view_snapshot(self, operation_id: str) -> OperationViewSnapshot:
        with self._changed:
            operation = self._operation_locked(operation_id)
            return OperationViewSnapshot(
                operation=self._descriptor(operation),
                kind=operation.command.operation_kind,
                facts=operation.view_facts,
                execution_provenance=operation.execution_provenance,
            )

    def set_execution_provenance(
        self,
        operation_id: str,
        provenance: OperationExecutionProvenance,
    ) -> OperationViewSnapshot:
        """Flush one immutable exact execution identity before target dispatch."""
        if not isinstance(provenance, OperationExecutionProvenance):
            raise TypeError(
                "provenance must be an OperationExecutionProvenance"
            )
        with self._changed:
            operation = self._operation_locked(operation_id)
            if (
                operation.command.source_sha256 is not None
                and provenance.visible_source_sha256
                != operation.command.source_sha256
            ):
                raise ValueError(
                    "execution provenance does not match the admitted source"
                )
            current = operation.execution_provenance
            if current is not None:
                if current != provenance:
                    raise ValueError("execution provenance is immutable")
                return self._view_snapshot_locked(operation)
            if operation.state is not AgentOperationState.RUNNING:
                raise ValueError(
                    "execution provenance must be recorded before dispatch"
                )
            payload = self._record_locked(
                operation,
                "execution_provenance",
                provenance=to_wire(provenance),
            )
            operation.execution_provenance = provenance
            self._publish_transition(payload)
            return self._view_snapshot_locked(operation)

    def record_diagnostic(
        self,
        diagnostic: NormalizedDiagnostic,
        *,
        excerpt: str | None = None,
        operation_id: str | None = None,
    ) -> str:
        """Write private detail, then its hash-only operation-journal anchor."""
        record = _PrivateDiagnosticRecord.from_diagnostic(
            diagnostic,
            excerpt=excerpt,
        )
        with self._changed:
            operation = (
                None
                if operation_id is None
                else self._operation_locked(operation_id)
            )
            if (
                operation is not None
                and operation.state is not AgentOperationState.RUNNING
            ):
                raise ValueError(
                    "private diagnostic reference requires a running operation"
                )
            self._ensure_private_diagnostics_loaded_locked()
            assert self._private_diagnostics is not None
            assert self._private_diagnostic_conflicts is not None
            if record.diagnostic_id in self._private_diagnostic_conflicts:
                raise ValueError("private diagnostic record is conflicted")
            current = self._private_diagnostics.get(record.diagnostic_id)
            if current is not None:
                if current != record:
                    raise ValueError("private diagnostic record is immutable")
            else:
                # The private record is flushed before the public hash anchor.
                # A crash between them leaves an inert orphan, never details.
                self._append_private_diagnostic(record)
                self._private_diagnostics[record.diagnostic_id] = record
            if operation is not None:
                reference = _PrivateDiagnosticRef(
                    record.diagnostic_id,
                    record.content_integrity_sha256,
                )
                current_reference = operation.private_diagnostic_ref
                if current_reference is not None:
                    if current_reference != reference:
                        raise ValueError(
                            "private diagnostic reference is immutable"
                        )
                else:
                    payload = self._record_locked(
                        operation,
                        "private_diagnostic_ref",
                        reference={
                            "diagnostic_id": reference.diagnostic_id,
                            "content_integrity_sha256": (
                                reference.content_integrity_sha256
                            ),
                        },
                    )
                    operation.private_diagnostic_ref = reference
                    self._publish_transition(payload)
            return record.diagnostic_id

    def diagnostic_excerpt(self, diagnostic_id: str) -> str | None:
        """Resolve only the bounded response excerpt for an explicit view."""
        try:
            _require_sha256(diagnostic_id, name="diagnostic_id")
        except ValueError:
            return None
        with self._changed:
            self._ensure_private_diagnostics_loaded_locked()
            assert self._private_diagnostics is not None
            record = self._private_diagnostics.get(diagnostic_id)
            return None if record is None else record.excerpt

    def expert_diagnostic(
        self,
        operation_id: str,
        diagnostic_id: str,
    ) -> dict[str, object] | None:
        """Resolve one exact operation-bound private diagnostic explicitly."""
        try:
            _require_sha256(diagnostic_id, name="diagnostic_id")
        except ValueError:
            return None
        with self._changed:
            operation = self._operation_locked(operation_id)
            failure = operation.view_facts.failure
            if failure is None:
                return None
            raw_public = failure.get("diagnostic")
            try:
                public = AgentDiagnosticView.from_wire(raw_public)
            except (TypeError, ValueError):
                return None
            if public.diagnostic_id != diagnostic_id:
                return None
            reference = operation.private_diagnostic_ref
            if reference is None or reference.diagnostic_id != diagnostic_id:
                return None
            self._ensure_private_diagnostics_loaded_locked()
            assert self._private_diagnostics is not None
            assert self._private_diagnostic_conflicts is not None
            if diagnostic_id in self._private_diagnostic_conflicts:
                return None
            record = self._private_diagnostics.get(diagnostic_id)
            if (
                record is None
                or record.diagnostic_id != diagnostic_id
                or record.content_integrity_sha256
                != reference.content_integrity_sha256
                or not record.matches_canonical_diagnostic_id(public)
                or not record.matches_operation(
                    operation.command,
                    operation.execution_provenance,
                )
            ):
                return None
            try:
                return record.to_expert_wire(
                    public,
                    operation.execution_provenance,
                )
            except (KeyError, TypeError, ValueError):
                return None

    def set_view_facts(
        self,
        operation_id: str,
        facts: OperationViewFacts,
    ) -> OperationViewSnapshot:
        if not isinstance(facts, OperationViewFacts):
            raise TypeError("facts must be OperationViewFacts")
        with self._changed:
            operation = self._operation_locked(operation_id)
            payload = self._record_locked(
                operation,
                "view_facts",
                facts=to_wire(facts),
            )
            operation.view_facts = facts
            self._publish_transition(payload)
            return OperationViewSnapshot(
                operation=self._descriptor(operation),
                kind=operation.command.operation_kind,
                facts=operation.view_facts,
                execution_provenance=operation.execution_provenance,
            )

    def _view_snapshot_locked(
        self,
        operation: _Operation,
    ) -> OperationViewSnapshot:
        return OperationViewSnapshot(
            operation=self._descriptor(operation),
            kind=operation.command.operation_kind,
            facts=operation.view_facts,
            execution_provenance=operation.execution_provenance,
        )

    def wait(
        self,
        operation_id: str,
        timeout_s: float,
        after_cursor: int | None = None,
        waiter_id: str | None = None,
    ) -> OperationDescriptor:
        timeout = self._validate_timeout(timeout_s)
        self._validate_cursor(after_cursor, field_name="after_cursor")
        if waiter_id is not None and (not isinstance(waiter_id, str) or not waiter_id):
            raise ValueError("waiter_id must be a non-empty string")
        with self._changed:
            operation = self._operation_locked(operation_id)
            if waiter_id is not None:
                self._register_waiter_locked(operation_id, waiter_id)
            try:
                deadline = monotonic() + timeout
                while True:
                    if (
                        waiter_id is not None
                        and (operation_id, waiter_id) in self._stopped_waiters
                    ):
                        return self._descriptor(operation)
                    if self._is_terminal(operation.state) or (
                        after_cursor is not None and operation.last_event_cursor > after_cursor
                    ):
                        return self._descriptor(operation)
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        return self._descriptor(operation)
                    self._changed.wait(remaining)
            finally:
                if waiter_id is not None:
                    self._unregister_waiter_locked(operation_id, waiter_id)

    def output(
        self,
        operation_id: str,
        after_cursor: int = 0,
        limits: Mapping[str, object] | None = None,
    ) -> OperationOutput:
        self._validate_cursor(after_cursor, field_name="after_cursor")
        limit = self._message_limit(limits)
        with self._changed:
            operation = self._operation_locked(operation_id)
            available = [item for item in operation.messages if item[0] > after_cursor]
            page = available[:limit]
            next_cursor = page[-1][0] if page else after_cursor
            return OperationOutput(
                operation_id=operation.operation_id,
                messages=tuple(message for _, message in page),
                next_cursor=next_cursor,
                has_more=len(available) > len(page),
            )

    def result(self, operation_id: str) -> OperationDescriptor:
        """Expose result availability without exposing a backend result value."""
        return self.status(operation_id)

    def stop_waiting(self, operation_id: str, waiter_id: str) -> OperationDescriptor:
        if not isinstance(waiter_id, str) or not waiter_id:
            raise ValueError("waiter_id must be a non-empty string")
        with self._changed:
            operation = self._operation_locked(operation_id)
            if waiter_id in self._waiters.get(operation_id, set()):
                self._stopped_waiters.add((operation_id, waiter_id))
                self._changed.notify_all()
            return self._descriptor(operation)

    def mark_generation_aborted(
        self, runtime_id: str, generation: int
    ) -> tuple[OperationDescriptor, ...]:
        if not isinstance(runtime_id, str) or not runtime_id:
            raise ValueError("runtime_id must be a non-empty string")
        if type(generation) is not int or generation <= 0:
            raise ValueError("generation must be positive")
        with self._changed:
            changed: list[OperationDescriptor] = []
            for operation in self._operations.values():
                if (
                    operation.command.runtime_id == runtime_id
                    and operation.command.runtime_generation == generation
                    and not self._is_terminal(operation.state)
                ):
                    self._transition_locked(
                        operation,
                        AgentOperationState.UNKNOWN,
                        reason="generation_aborted",
                    )
                    changed.append(self._descriptor(operation))
            return tuple(changed)

    def _run(self, operation_id: str, execute: Callable[[], BackendExecution]) -> None:
        with self._execution_lane:
            with self._changed:
                operation = self._operations.get(operation_id)
                if operation is None or operation.state is not AgentOperationState.QUEUED:
                    return
                self._transition_locked(operation, AgentOperationState.RUNNING)
            try:
                outcome = execute()
                if not isinstance(outcome, BackendExecution):
                    raise TypeError("backend execution must return BackendExecution")
            except BaseException:
                outcome = BackendExecution(
                    terminal_state=AgentOperationState.FAILED,
                    messages=(),
                    result_present=False,
                    runtime_state="failed",
                )
        safe_diagnostic = (
            None
            if outcome.diagnostic is None
            else sanitize_normalized_diagnostic(outcome.diagnostic)
        )
        if safe_diagnostic is not None:
            try:
                self.record_diagnostic(
                    safe_diagnostic,
                    operation_id=operation_id,
                )
            except BaseException:
                # The compact public reference is independently durable and
                # remains useful when the private expert record is unavailable.
                pass
        with self._changed:
            operation = self._operations.get(operation_id)
            if operation is None or self._is_terminal(operation.state):
                return
            if safe_diagnostic is not None:
                diagnostic = self._diagnostic_view(safe_diagnostic)
                failure = dict(operation.view_facts.failure or {})
                failure.setdefault(
                    "stage",
                    outcome.failure_stage or "execution",
                )
                failure.setdefault("partial_results", {})
                failure.setdefault("state_changed", outcome.state_changed.value)
                failure["diagnostic"] = to_wire(diagnostic)
                facts = replace(operation.view_facts, failure=failure)
                if facts != operation.view_facts:
                    payload = self._record_locked(
                        operation,
                        "view_facts",
                        facts=to_wire(facts),
                    )
                    operation.view_facts = facts
                    self._publish_transition(payload)
            for message in outcome.messages:
                self._publish_transition(self._record_locked(operation, "message", message=message))
            self._transition_locked(
                operation,
                outcome.terminal_state,
                result_present=outcome.result_present,
            )

    def _rehydrate(self) -> None:
        with self._changed:
            if self._journal_path.exists():
                for line in self._journal_path.read_text(encoding="utf-8").splitlines():
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(payload, dict):
                        self._fold_event(payload)
            self._next_cursor = max(
                (operation.last_event_cursor for operation in self._operations.values()),
                default=0,
            ) + 1
            for operation in tuple(self._operations.values()):
                if operation.state in {AgentOperationState.QUEUED, AgentOperationState.RUNNING}:
                    self._transition_locked(
                        operation,
                        AgentOperationState.UNKNOWN,
                        reason="service_restart_during_execution",
                    )

    def _fold_event(self, payload: Mapping[str, object]) -> None:
        cursor = payload.get("cursor")
        event = payload.get("event")
        operation_id = payload.get("operation_id")
        if type(cursor) is not int or cursor < 1 or not isinstance(event, str) or not isinstance(operation_id, str):
            return
        if event == "submitted":
            try:
                command = self._normalize_command(payload)
            except (TypeError, ValueError):
                return
            self._operations[operation_id] = _Operation(
                operation_id, command, AgentOperationState.QUEUED, last_event_cursor=cursor
            )
            return
        operation = self._operations.get(operation_id)
        if operation is None or cursor <= operation.last_event_cursor:
            return
        if event == "started" and operation.state is AgentOperationState.QUEUED:
            operation.state = AgentOperationState.RUNNING
        elif event == "message" and isinstance(payload.get("message"), str):
            operation.messages.append((cursor, cast(str, payload["message"])))
        elif event == "view_facts":
            try:
                operation.view_facts = OperationViewFacts.from_wire(payload.get("facts"))
            except (TypeError, ValueError):
                return
        elif event == "execution_provenance":
            if operation.state is not AgentOperationState.RUNNING:
                return
            try:
                provenance = OperationExecutionProvenance.from_wire(
                    payload.get("provenance")
                )
            except (TypeError, ValueError):
                return
            if (
                operation.command.source_sha256 is not None
                and provenance.visible_source_sha256
                != operation.command.source_sha256
            ):
                return
            if operation.execution_provenance is not None:
                if operation.execution_provenance != provenance:
                    return
            else:
                operation.execution_provenance = provenance
        elif event == "private_diagnostic_ref":
            if operation.state is not AgentOperationState.RUNNING:
                return
            try:
                reference = _PrivateDiagnosticRef.from_wire(
                    payload.get("reference")
                )
            except (TypeError, ValueError):
                return
            if operation.private_diagnostic_ref is not None:
                if operation.private_diagnostic_ref != reference:
                    return
            else:
                operation.private_diagnostic_ref = reference
        elif event in {"completed", "captured", "failed", "unknown"}:
            operation.state = AgentOperationState(event)
            operation.result_present = payload.get("result_present") is True
        else:
            return
        operation.last_event_cursor = cursor

    def _record_locked(self, operation: _Operation, event: str, **extra: object) -> dict[str, object]:
        cursor = self._next_cursor
        payload: dict[str, object] = {"cursor": cursor, "event": event, "operation_id": operation.operation_id}
        if event == "submitted":
            payload.update(
                {
                    "operation_kind": operation.command.operation_kind.value,
                    "runtime_id": operation.command.runtime_id,
                    "runtime_generation": operation.command.runtime_generation,
                    "code_id": operation.command.code_id,
                    "revision": operation.command.revision,
                    "source_sha256": operation.command.source_sha256,
                    "inputs_sha256": operation.command.inputs_sha256,
                    "request_id": operation.command.request_id,
                    "startup_profile": operation.command.startup_profile,
                    "startup_mode": operation.command.startup_mode,
                    "observation": (
                        None
                        if operation.command.observation_plan is None
                        else to_wire(operation.command.observation_plan)
                    ),
                }
            )
        elif event == "message":
            payload["message"] = cast(str, extra["message"])
        elif event == "view_facts":
            payload["facts"] = extra["facts"]
        elif event == "execution_provenance":
            payload["provenance"] = extra["provenance"]
        elif event == "private_diagnostic_ref":
            payload["reference"] = extra["reference"]
        elif event in {"completed", "captured", "failed", "unknown"}:
            payload["result_present"] = extra.get("result_present") is True
            if "reason" in extra:
                payload["reason"] = cast(str, extra["reason"])
        self._append_event(payload)
        self._next_cursor += 1
        operation.last_event_cursor = cursor
        if event == "message":
            operation.messages.append((cursor, cast(str, extra["message"])))
        return payload

    def _transition_locked(
        self,
        operation: _Operation,
        state: AgentOperationState,
        *,
        result_present: bool = False,
        reason: str | None = None,
    ) -> None:
        if not self._is_terminal(state) and state is not AgentOperationState.RUNNING:
            raise ValueError("invalid operation transition")
        event_extra: dict[str, object] = {"result_present": result_present}
        if reason is not None:
            event_extra["reason"] = reason
        payload = self._record_locked(
            operation,
            state.value if state is not AgentOperationState.RUNNING else "started",
            **event_extra,
        )
        operation.state = state
        operation.result_present = result_present
        self._publish_transition(payload)

    def _append_event(self, payload: Mapping[str, object]) -> None:
        self._journal_path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._journal_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())

    def _append_private_diagnostic(
        self,
        record: _PrivateDiagnosticRecord,
    ) -> None:
        self._private_diagnostic_path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(
            record.to_wire(),
            ensure_ascii=False,
            separators=(",", ":"),
        ) + "\n"
        with self._private_diagnostic_path.open(
            "a",
            encoding="utf-8",
            newline="\n",
        ) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())

    def _ensure_private_diagnostics_loaded_locked(self) -> None:
        if self._private_diagnostics is not None:
            return
        records: dict[str, _PrivateDiagnosticRecord] = {}
        conflicted: set[str] = set()
        if self._private_diagnostic_path.exists():
            try:
                lines = self._private_diagnostic_path.read_text(
                    encoding="utf-8"
                ).splitlines()
            except (OSError, UnicodeError):
                lines = []
            for line in lines:
                try:
                    decoded = json.loads(line)
                except json.JSONDecodeError:
                    continue
                candidate_id = (
                    decoded.get("diagnostic_id")
                    if isinstance(decoded, Mapping)
                    else None
                )
                if candidate_id in conflicted:
                    continue
                try:
                    record = _PrivateDiagnosticRecord.from_wire(decoded)
                except (TypeError, ValueError):
                    try:
                        _require_sha256(candidate_id, name="diagnostic_id")
                    except ValueError:
                        continue
                    records.pop(cast(str, candidate_id), None)
                    conflicted.add(cast(str, candidate_id))
                    continue
                if record.diagnostic_id in conflicted:
                    continue
                current = records.get(record.diagnostic_id)
                if current is None:
                    records[record.diagnostic_id] = record
                elif current != record:
                    # Conflicting private evidence is omitted rather than
                    # choosing an arbitrary detail record.
                    records.pop(record.diagnostic_id, None)
                    conflicted.add(record.diagnostic_id)
        self._private_diagnostics = records
        self._private_diagnostic_conflicts = conflicted

    @staticmethod
    def _diagnostic_view(diagnostic: NormalizedDiagnostic) -> AgentDiagnosticView:
        location = diagnostic.visible_location
        related = diagnostic.related_visible_span
        return AgentDiagnosticView(
            diagnostic_id=diagnostic.diagnostic_id,
            stage=diagnostic.stage.value,
            mapping_confidence=diagnostic.mapping_confidence.value,
            visible_location=(
                None
                if location is None
                else {
                    "line": location.line,
                    "column": location.column,
                    "span": {
                        "start": location.span.start,
                        "end": location.span.end,
                    },
                }
            ),
            related_visible_span=(
                None
                if related is None
                else {"start": related.start, "end": related.end}
            ),
            excerpt=None,
            synthetic_region=diagnostic.synthetic_region,
        )

    def _publish_transition(self, payload: dict[str, object]) -> None:
        """Notify only after this exact payload has been flushed to disk."""
        self._changed.notify_all()

    def _register_waiter_locked(self, operation_id: str, waiter_id: str) -> None:
        self._waiters.setdefault(operation_id, set()).add(waiter_id)
        self._stopped_waiters.discard((operation_id, waiter_id))

    def _unregister_waiter_locked(self, operation_id: str, waiter_id: str) -> None:
        waiters = self._waiters.get(operation_id)
        if waiters is not None:
            waiters.discard(waiter_id)
            if not waiters:
                del self._waiters[operation_id]
        self._stopped_waiters.discard((operation_id, waiter_id))

    def _executor_locked(self) -> Executor:
        if self._executor is None:
            self._executor = (
                self._executor_factory()
                if self._executor_factory is not None
                else ThreadPoolExecutor(max_workers=1, thread_name_prefix="onec-agent-runtime")
            )
        return self._executor

    def _operation_locked(self, operation_id: str) -> _Operation:
        if not isinstance(operation_id, str) or not operation_id:
            raise ValueError("operation_id must be a non-empty string")
        try:
            return self._operations[operation_id]
        except KeyError as error:
            raise KeyError("operation not found") from error

    @staticmethod
    def _descriptor(operation: _Operation) -> OperationDescriptor:
        messages_cursor = operation.messages[-1][0] if operation.messages else 0
        return OperationDescriptor(
            operation_id=operation.operation_id,
            state=operation.state,
            runtime_id=operation.command.runtime_id,
            runtime_generation=operation.command.runtime_generation,
            cell_id=operation.command.code_id,
            revision=operation.command.revision,
            source_sha256=operation.command.source_sha256,
            messages_cursor=messages_cursor,
            event_cursor=operation.last_event_cursor,
            result_present=operation.result_present,
            safe_to_retry=OperationRegistry._retry_safety(operation.state),
        )

    @staticmethod
    def _retry_safety(state: AgentOperationState) -> RetrySafety:
        if state in {AgentOperationState.COMPLETED, AgentOperationState.CAPTURED}:
            return RetrySafety.YES
        if state is AgentOperationState.FAILED:
            return RetrySafety.NO
        return RetrySafety.AFTER_STATUS_CHECK

    @staticmethod
    def _is_terminal(state: AgentOperationState) -> bool:
        return state in {
            AgentOperationState.COMPLETED,
            AgentOperationState.CAPTURED,
            AgentOperationState.FAILED,
            AgentOperationState.UNKNOWN,
        }

    @staticmethod
    def _normalize_command(command: Mapping[str, object]) -> _Command:
        if not isinstance(command, Mapping):
            raise TypeError("command must be a mapping")
        kind = AgentOperationKind(
            command.get("operation_kind", AgentOperationKind.CODE_RUN.value)
        )
        runtime_id = (
            ""
            if kind is AgentOperationKind.RUNTIME_ENSURE
            and command.get("runtime_id") in {None, ""}
            else OperationRegistry._optional_string(command, "runtime_id") or ""
        )
        runtime_generation = OperationRegistry._optional_positive(
            command, "runtime_generation"
        )
        code_id = OperationRegistry._optional_string(command, "code_id")
        revision = OperationRegistry._optional_positive(command, "revision")
        source_sha256 = OperationRegistry._optional_string(command, "source_sha256")
        raw_observation = command.get("observation")
        observation_plan = (
            None
            if raw_observation is None
            else ObservationPlan.from_wire(raw_observation)
        )
        if kind is not AgentOperationKind.RUNTIME_ENSURE and (
            not runtime_id
            or runtime_generation is None
            or code_id is None
            or revision is None
            or source_sha256 is None
        ):
            raise ValueError("code operations require complete runtime and source identity")
        startup_profile = OperationRegistry._optional_string(
            command, "startup_profile"
        )
        startup_mode = OperationRegistry._optional_string(command, "startup_mode")
        if kind is AgentOperationKind.RUNTIME_ENSURE and (
            startup_profile is None or startup_mode is None
        ):
            raise ValueError("startup operations require profile and mode")
        return _Command(
            operation_kind=kind,
            runtime_id=runtime_id,
            runtime_generation=runtime_generation,
            code_id=code_id,
            revision=revision,
            source_sha256=source_sha256,
            inputs_sha256=OperationRegistry._required_string(command, "inputs_sha256"),
            request_id=OperationRegistry._optional_string(command, "request_id"),
            observation_plan=observation_plan,
            startup_profile=startup_profile,
            startup_mode=startup_mode,
        )

    @staticmethod
    def _required_string(mapping: Mapping[str, object], name: str) -> str:
        value = mapping.get(name)
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name} must be a non-empty string")
        return value

    @staticmethod
    def _positive(mapping: Mapping[str, object], name: str) -> int:
        value = mapping.get(name)
        if type(value) is not int or value <= 0:
            raise ValueError(f"{name} must be positive")
        return cast(int, value)

    @staticmethod
    def _optional_string(mapping: Mapping[str, object], name: str) -> str | None:
        value = mapping.get(name)
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name} must be a non-empty string")
        return value

    @staticmethod
    def _optional_positive(mapping: Mapping[str, object], name: str) -> int | None:
        value = mapping.get(name)
        if value is None:
            return None
        if type(value) is not int or value <= 0:
            raise ValueError(f"{name} must be positive")
        return cast(int, value)

    @staticmethod
    def _validate_timeout(value: object) -> float:
        if type(value) not in {int, float} or not isfinite(cast(float, value)):
            raise ValueError("timeout_s must be finite")
        timeout = float(value)
        if timeout < 0 or timeout > MAX_WAIT_SECONDS:
            raise ValueError("timeout_s is outside allowed bounds")
        return timeout

    @staticmethod
    def _validate_cursor(value: object, *, field_name: str) -> None:
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError(f"{field_name} must be a non-negative integer")

    @staticmethod
    def _message_limit(limits: Mapping[str, object] | None) -> int:
        if limits is None:
            return MAX_OPERATION_MESSAGES
        if not isinstance(limits, Mapping) or set(limits) - {"messages"}:
            raise ValueError("limits must contain only messages")
        value = limits.get("messages", MAX_OPERATION_MESSAGES)
        if type(value) is not int or not 1 <= cast(int, value) <= MAX_OPERATION_MESSAGES:
            raise ValueError("messages limit is outside allowed bounds")
        return cast(int, value)

    @staticmethod
    def _matches(operation: _Operation, filters: Mapping[str, object]) -> bool:
        state = filters.get("state")
        if state is not None and state != operation.state and state != operation.state.value:
            return False
        return (
            filters.get("runtime_id", operation.command.runtime_id) == operation.command.runtime_id
            and filters.get("runtime_generation", operation.command.runtime_generation)
            == operation.command.runtime_generation
        )
