from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
from threading import RLock
from uuid import UUID, uuid4, uuid5

from onec_runtime.bsl.source_maps import (
    MappingRelation,
    SourceSpan,
    SourceUnitRef,
)
from onec_runtime.bsl.worker_reload_source_map import CompactReloadSourceMap
from onec_runtime.errors import ProtocolError
from onec_runtime.rdbg.models import ModuleLocation, StackFrame, StopEvent, TargetId
from onec_runtime.stop_routing import StopReason
from onec_runtime.worker_universe import (
    WorkerGenerationDebugView,
    WorkerGenerationHandle,
    WorkerModuleDebugView,
)


class WorkerBreakpointRejection(StrEnum):
    MODULE_NOT_IN_CANDIDATE = "module_not_in_candidate"
    SOURCE_IDENTITY_MISMATCH = "source_identity_mismatch"
    SOURCE_REVISION_NOT_ADMITTED = "source_revision_not_admitted"
    NO_RETAINED_COMPATIBLE_ARTIFACT = "no_retained_compatible_artifact"
    LINE_OUT_OF_RANGE = "line_out_of_range"
    UNMAPPED_SOURCE_LINE = "unmapped_source_line"
    SYNTHETIC_TARGET = "synthetic_target"
    AMBIGUOUS_LINE_MAPPING = "ambiguous_line_mapping"


class WorkerBreakpointReloadPolicy(StrEnum):
    STRICT = "strict"
    RESET_INCOMPATIBLE = "reset_incompatible"


class WorkerBreakpointResolution(StrEnum):
    PENDING = "pending"
    RESOLVED = "resolved"
    REJECTED = "rejected"


class WorkerBreakpointReloadOutcome(StrEnum):
    COMMITTED = "committed"
    ABORTED = "aborted"
    QUARANTINED = "quarantined"


@dataclass(frozen=True, slots=True)
class WorkerBreakpoint:
    id: UUID
    source_unit: SourceUnitRef
    canonical_module: str
    line: int
    column: int | None = None

    def __post_init__(self) -> None:
        if (
            type(self.id) is not UUID
            or not isinstance(self.source_unit, SourceUnitRef)
            or type(self.canonical_module) is not str
            or not self.canonical_module
            or self.canonical_module != self.canonical_module.casefold()
            or type(self.line) is not int
            or self.line < 1
            or self.column is not None
        ):
            raise ValueError("worker breakpoint is invalid")


@dataclass(frozen=True, slots=True)
class WorkerBreakpointGenerationStatus:
    generation: WorkerGenerationHandle
    resolution: WorkerBreakpointResolution
    reason: WorkerBreakpointRejection | None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.generation, WorkerGenerationHandle)
            or type(self.resolution) is not WorkerBreakpointResolution
            or not _valid_resolution_reason(self.resolution, self.reason)
        ):
            raise ValueError("worker breakpoint generation status is invalid")


@dataclass(frozen=True, slots=True)
class WorkerBreakpointStatus:
    breakpoint: WorkerBreakpoint
    enabled: bool
    resolution: WorkerBreakpointResolution
    reason: WorkerBreakpointRejection | None
    generations: tuple[WorkerBreakpointGenerationStatus, ...]
    installed_binding_count: int
    catalog_version: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.breakpoint, WorkerBreakpoint)
            or type(self.enabled) is not bool
            or type(self.resolution) is not WorkerBreakpointResolution
            or not _valid_resolution_reason(self.resolution, self.reason)
            or type(self.generations) is not tuple
            or any(
                not isinstance(item, WorkerBreakpointGenerationStatus)
                for item in self.generations
            )
            or type(self.installed_binding_count) is not int
            or self.installed_binding_count < 0
            or type(self.catalog_version) is not int
            or self.catalog_version < 0
        ):
            raise ValueError("worker breakpoint status is invalid")


@dataclass(frozen=True, slots=True)
class WorkerBreakpointReloadRemoval:
    breakpoint_id: UUID
    reason: WorkerBreakpointRejection

    def __post_init__(self) -> None:
        if (
            type(self.breakpoint_id) is not UUID
            or type(self.reason) is not WorkerBreakpointRejection
        ):
            raise ValueError("worker breakpoint reload removal is invalid")


@dataclass(frozen=True, slots=True)
class WorkerBreakpointReloadReport:
    transaction_id: UUID
    candidate_handle: WorkerGenerationHandle
    policy: WorkerBreakpointReloadPolicy
    outcome: WorkerBreakpointReloadOutcome
    planned_removals: tuple[WorkerBreakpointReloadRemoval, ...]
    committed_removals: tuple[UUID, ...]
    catalog_version: int

    def __post_init__(self) -> None:
        if (
            type(self.transaction_id) is not UUID
            or not isinstance(self.candidate_handle, WorkerGenerationHandle)
            or type(self.policy) is not WorkerBreakpointReloadPolicy
            or type(self.outcome) is not WorkerBreakpointReloadOutcome
            or type(self.planned_removals) is not tuple
            or any(
                type(item) is not WorkerBreakpointReloadRemoval
                for item in self.planned_removals
            )
            or type(self.committed_removals) is not tuple
            or any(type(item) is not UUID for item in self.committed_removals)
            or type(self.catalog_version) is not int
            or self.catalog_version < 0
            or (
                self.outcome is not WorkerBreakpointReloadOutcome.COMMITTED
                and self.committed_removals
            )
        ):
            raise ValueError("worker breakpoint reload report is invalid")


class WorkerBreakpointConflict(ProtocolError):
    """STRICT publication rejected by candidate-proven incompatibilities."""

    def __init__(
        self,
        rejections: tuple[WorkerBreakpointReloadRemoval, ...],
    ) -> None:
        if (
            type(rejections) is not tuple
            or not rejections
            or any(type(item) is not WorkerBreakpointReloadRemoval for item in rejections)
        ):
            raise ValueError("worker breakpoint conflict details are invalid")
        self.rejections = rejections
        super().__init__("Worker generation conflicts with enabled logical breakpoints")


@dataclass(frozen=True, slots=True, repr=False)
class WorkerBreakpointCatalogSnapshot:
    catalog_version: int
    statuses: tuple[WorkerBreakpointStatus, ...]
    tombstone_ids: frozenset[UUID]

    def __post_init__(self) -> None:
        if (
            type(self.catalog_version) is not int
            or self.catalog_version < 0
            or type(self.statuses) is not tuple
            or any(not isinstance(item, WorkerBreakpointStatus) for item in self.statuses)
            or any(item.catalog_version != self.catalog_version for item in self.statuses)
            or len({item.breakpoint.id for item in self.statuses}) != len(self.statuses)
            or type(self.tombstone_ids) is not frozenset
            or any(type(item) is not UUID for item in self.tombstone_ids)
            or any(item.breakpoint.id in self.tombstone_ids for item in self.statuses)
        ):
            raise ValueError("worker breakpoint catalog snapshot is invalid")

    def __repr__(self) -> str:
        return (
            "WorkerBreakpointCatalogSnapshot("
            f"catalog_version={self.catalog_version}, "
            f"statuses={self.statuses!r}, tombstones={len(self.tombstone_ids)})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class WorkerBreakpointPlan:
    owner: object
    proposal_id: UUID
    expected_catalog_version: int
    result_id: UUID
    desired_slots: tuple[ModuleLocation, ...]
    removed_ids: tuple[UUID, ...]
    removal_details: tuple[WorkerBreakpointReloadRemoval, ...]
    next_views: tuple[WorkerGenerationDebugView, ...]
    next_snapshot: WorkerBreakpointCatalogSnapshot

    def __post_init__(self) -> None:
        if (
            type(self.proposal_id) is not UUID
            or type(self.expected_catalog_version) is not int
            or self.expected_catalog_version < 0
            or type(self.result_id) is not UUID
            or type(self.desired_slots) is not tuple
            or any(type(item) is not ModuleLocation for item in self.desired_slots)
            or type(self.removed_ids) is not tuple
            or any(type(item) is not UUID for item in self.removed_ids)
            or type(self.removal_details) is not tuple
            or any(
                type(item) is not WorkerBreakpointReloadRemoval
                for item in self.removal_details
            )
            or (
                self.removal_details
                and tuple(item.breakpoint_id for item in self.removal_details)
                != self.removed_ids
            )
            or type(self.next_views) is not tuple
            or any(type(item) is not WorkerGenerationDebugView for item in self.next_views)
            or not isinstance(self.next_snapshot, WorkerBreakpointCatalogSnapshot)
            or self.next_snapshot.catalog_version
            not in (self.expected_catalog_version, self.expected_catalog_version + 1)
        ):
            raise ValueError("worker breakpoint plan is invalid")

    def __repr__(self) -> str:
        return (
            "WorkerBreakpointPlan("
            "owner=<redacted>, "
            f"proposal_id={self.proposal_id!r}, "
            f"expected_catalog_version={self.expected_catalog_version}, "
            f"result_id={self.result_id!r}, "
            f"desired_slots={len(self.desired_slots)}, "
            f"removed_ids={self.removed_ids!r}, "
            f"next_snapshot={self.next_snapshot!r})"
        )


_PENDING_REASONS = frozenset(
    {
        WorkerBreakpointRejection.SOURCE_REVISION_NOT_ADMITTED,
        WorkerBreakpointRejection.NO_RETAINED_COMPATIBLE_ARTIFACT,
    }
)
_MAP_REJECTION_REASONS = frozenset(
    {
        WorkerBreakpointRejection.LINE_OUT_OF_RANGE,
        WorkerBreakpointRejection.UNMAPPED_SOURCE_LINE,
        WorkerBreakpointRejection.SYNTHETIC_TARGET,
        WorkerBreakpointRejection.AMBIGUOUS_LINE_MAPPING,
    }
)


def _valid_resolution_reason(
    resolution: WorkerBreakpointResolution,
    reason: WorkerBreakpointRejection | None,
) -> bool:
    if resolution is WorkerBreakpointResolution.RESOLVED:
        return reason is None
    if type(reason) is not WorkerBreakpointRejection:
        return False
    if resolution is WorkerBreakpointResolution.PENDING:
        return reason in _PENDING_REASONS
    return reason in _MAP_REJECTION_REASONS


@dataclass(frozen=True, slots=True)
class WorkerLineMapping:
    generated_line: int | None
    reason: WorkerBreakpointRejection | None

    def __post_init__(self) -> None:
        if (
            (self.generated_line is None) == (self.reason is None)
            or (
                self.generated_line is not None
                and (
                    type(self.generated_line) is not int
                    or self.generated_line < 1
                )
            )
            or (
                self.reason is not None
                and type(self.reason) is not WorkerBreakpointRejection
            )
        ):
            raise ValueError("worker line mapping is invalid")


@dataclass(frozen=True, slots=True)
class WorkerSourceLocation:
    source_unit: SourceUnitRef
    canonical_module: str
    line: int
    column: int | None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.source_unit, SourceUnitRef)
            or type(self.canonical_module) is not str
            or not self.canonical_module
            or self.canonical_module != self.canonical_module.casefold()
            or type(self.line) is not int
            or self.line < 1
            or self.column is not None
        ):
            raise ValueError("worker source location is invalid")


class WorkerFrameProvenance:
    """Opaque proof tying a mapped frame to one target-bound physical frame."""

    __slots__ = ("__owner", "__raw_frame", "__target_incarnation_id")

    def __init__(
        self,
        owner: object,
        raw_frame: StackFrame,
        target_incarnation_id: UUID,
    ) -> None:
        if (
            owner is not _WORKER_FRAME_PROVENANCE_OWNER
            or type(raw_frame) is not StackFrame
            or type(target_incarnation_id) is not UUID
        ):
            raise TypeError("Worker frame provenance is private")
        self.__owner = owner
        self.__raw_frame = raw_frame
        self.__target_incarnation_id = target_incarnation_id

    def __repr__(self) -> str:
        return "WorkerFrameProvenance(<redacted>)"


_WORKER_FRAME_PROVENANCE_OWNER = object()


@dataclass(frozen=True, slots=True, repr=False)
class WorkerMappedFrame:
    level: int
    source: WorkerSourceLocation
    generation: WorkerGenerationHandle
    artifact_sha256: str
    source_map_sha256: str
    provenance: WorkerFrameProvenance

    def __post_init__(self) -> None:
        if (
            type(self.level) is not int
            or self.level < 0
            or type(self.source) is not WorkerSourceLocation
            or not isinstance(self.generation, WorkerGenerationHandle)
            or type(self.artifact_sha256) is not str
            or len(self.artifact_sha256) != 64
            or type(self.source_map_sha256) is not str
            or len(self.source_map_sha256) != 64
            or type(self.provenance) is not WorkerFrameProvenance
        ):
            raise ValueError("mapped Worker frame is invalid")

    def __repr__(self) -> str:
        return (
            "WorkerMappedFrame("
            f"level={self.level}, source={self.source!r}, "
            f"generation={self.generation!r}, "
            f"artifact_sha256={self.artifact_sha256!r}, "
            f"source_map_sha256={self.source_map_sha256!r}, "
            "provenance=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class NativeDebugFrame:
    level: int
    location: ModuleLocation

    def __post_init__(self) -> None:
        if (
            type(self.level) is not int
            or self.level < 0
            or type(self.location) is not ModuleLocation
        ):
            raise ValueError("native debug frame is invalid")


@dataclass(frozen=True, slots=True, repr=False)
class WorkerBreakpointBinding:
    breakpoint_id: UUID
    generation: WorkerGenerationHandle
    location: ModuleLocation

    def __post_init__(self) -> None:
        if (
            type(self.breakpoint_id) is not UUID
            or not isinstance(self.generation, WorkerGenerationHandle)
            or type(self.location) is not ModuleLocation
        ):
            raise ValueError("Worker breakpoint binding is invalid")

    def __repr__(self) -> str:
        return (
            "WorkerBreakpointBinding("
            f"breakpoint_id={self.breakpoint_id!r}, "
            f"generation={self.generation!r}, location=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class RuntimeDebugStop:
    reason: StopReason
    origin: str
    operation_id: int
    location: WorkerSourceLocation | ModuleLocation
    frames: tuple[WorkerMappedFrame | NativeDebugFrame, ...]
    breakpoint_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        if (
            type(self.reason) is not StopReason
            or self.origin not in {"main", "capture_evaluation"}
            or type(self.operation_id) is not int
            or self.operation_id < 0
            or type(self.location) not in {WorkerSourceLocation, ModuleLocation}
            or type(self.frames) is not tuple
            or any(
                type(frame) not in {WorkerMappedFrame, NativeDebugFrame}
                for frame in self.frames
            )
            or type(self.breakpoint_ids) is not tuple
            or any(type(item) is not UUID for item in self.breakpoint_ids)
            or len(set(self.breakpoint_ids)) != len(self.breakpoint_ids)
        ):
            raise ValueError("runtime debug stop is invalid")

    def __repr__(self) -> str:
        return (
            "RuntimeDebugStop("
            f"reason={self.reason!r}, origin={self.origin!r}, "
            f"operation_id={self.operation_id}, location={self.location!r}, "
            f"frames={self.frames!r}, breakpoint_ids={self.breakpoint_ids!r})"
        )


def _overlaps(left: SourceSpan, right: SourceSpan) -> bool:
    if left.start == left.end:
        return right.start <= left.start <= right.end
    if right.start == right.end:
        return left.start <= right.start <= left.end
    return left.start < right.end and right.start < left.end


def _generated_line_offsets(view: WorkerModuleDebugView, line: int) -> range:
    try:
        span = view.generated_line_index.line_range(line)
    except ValueError as error:
        raise ProtocolError("Worker generated line is outside the artifact") from error
    if span.start == span.end:
        return range(span.start, span.start + 1)
    return range(span.start, span.end)


def map_generated_line(
    view: WorkerModuleDebugView,
    line: int,
) -> WorkerSourceLocation | None:
    if type(view) is not WorkerModuleDebugView:
        raise TypeError("worker module debug view is required")
    if type(line) is not int or line < 1:
        raise ValueError("generated line must be positive")
    if view.visible_context is None:
        raise ProtocolError("Worker visible source context is unavailable")

    exact: set[tuple[SourceUnitRef, int, int]] = set()
    saw_synthetic = False
    saw_unproven = False
    for offset in _generated_line_offsets(view, line):
        try:
            mapped = view.mapped_source.source_map.map_offset(offset)
        except ValueError:
            continue
        if mapped.relation is MappingRelation.EXACT:
            if mapped.unit is None or mapped.origin_span is None:
                raise ProtocolError("Worker exact mapping has no source identity")
            coordinates = view.visible_context.line_column(
                mapped.unit,
                mapped.origin_span.start,
            )
            if coordinates is None:
                raise ProtocolError("Worker exact mapping provenance is unavailable")
            exact.add((mapped.unit, coordinates[0], coordinates[1]))
        elif mapped.relation is MappingRelation.SYNTHETIC:
            saw_synthetic = True
        else:
            saw_unproven = True

    source_lines = {(unit, source_line) for unit, source_line, _column in exact}
    if len(source_lines) > 1:
        raise ProtocolError("Worker generated line mapping is ambiguous")
    if source_lines:
        unit, source_line = next(iter(source_lines))
        return WorkerSourceLocation(
            unit,
            view.canonical_module,
            source_line,
            None,
        )
    if saw_synthetic and not saw_unproven:
        return None
    raise ProtocolError("Worker generated line mapping is not exact")


def resolve_source_line(
    view: WorkerModuleDebugView,
    source_unit: SourceUnitRef,
    line: int,
) -> WorkerLineMapping:
    if type(view) is not WorkerModuleDebugView:
        raise TypeError("worker module debug view is required")
    if not isinstance(source_unit, SourceUnitRef):
        raise TypeError("worker source unit is required")
    if source_unit not in view.source_units:
        raise ProtocolError("Worker source identity does not match the debug view")
    if type(line) is not int or line < 1:
        return WorkerLineMapping(None, WorkerBreakpointRejection.LINE_OUT_OF_RANGE)
    if view.visible_context is None:
        raise ProtocolError("Worker visible source context is unavailable")
    requested = view.visible_context.line_range(source_unit, line)
    if requested is None:
        return WorkerLineMapping(None, WorkerBreakpointRejection.LINE_OUT_OF_RANGE)

    exact_lines: set[int] = set()
    synthetic_lines: set[int] = set()
    source_map = view.mapped_source.source_map
    if isinstance(source_map, CompactReloadSourceMap):
        spans = source_map.mapped_spans_for_visible_span(source_unit, requested)
        for relation, span in spans:
            if span.start == span.end:
                lines = (view.generated_line_index.offset_to_line_column(span.start)[0],)
            else:
                first = view.generated_line_index.offset_to_line_column(span.start)[0]
                last = view.generated_line_index.offset_to_line_column(span.end - 1)[0]
                lines = range(first, last + 1)
            if relation is MappingRelation.EXACT:
                exact_lines.update(lines)
            elif relation is MappingRelation.SYNTHETIC:
                synthetic_lines.update(lines)
    else:
        for offset in range(len(view.mapped_source.text) + 1):
            try:
                mapped = source_map.map_offset(offset)
            except ValueError:
                continue
            generated_line = view.generated_line_index.offset_to_line_column(offset)[0]
            if (
                mapped.relation is MappingRelation.EXACT
                and mapped.unit == source_unit
                and mapped.origin_span is not None
                and _overlaps(mapped.origin_span, requested)
            ):
                exact_lines.add(generated_line)
            elif (
                mapped.relation is MappingRelation.SYNTHETIC
                and mapped.anchor_unit == source_unit
                and mapped.anchor_span is not None
                and _overlaps(mapped.anchor_span, requested)
            ):
                synthetic_lines.add(generated_line)

    if not exact_lines:
        return WorkerLineMapping(
            None,
            (
                WorkerBreakpointRejection.SYNTHETIC_TARGET
                if synthetic_lines
                else WorkerBreakpointRejection.UNMAPPED_SOURCE_LINE
            ),
        )
    if len(exact_lines) != 1 or exact_lines & synthetic_lines:
        return WorkerLineMapping(
            None,
            WorkerBreakpointRejection.AMBIGUOUS_LINE_MAPPING,
        )
    generated_line = next(iter(exact_lines))
    roundtrip = map_generated_line(view, generated_line)
    if roundtrip is None or (
        roundtrip.source_unit != source_unit or roundtrip.line != line
    ):
        return WorkerLineMapping(
            None,
            WorkerBreakpointRejection.AMBIGUOUS_LINE_MAPPING,
        )
    return WorkerLineMapping(generated_line, None)


def _breakpoint_identity(
    source_unit: SourceUnitRef,
    canonical_module: str,
    line: int,
    column: int | None,
) -> str:
    return json.dumps(
        {
            "canonical_module": canonical_module,
            "column": column,
            "line": line,
            "source_kind": source_unit.kind.value,
            "source_revision": source_unit.revision,
            "source_sha256": source_unit.source_sha256.lower(),
            "source_unit_id": source_unit.unit_id,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _module_location_key(location: ModuleLocation) -> tuple[object, ...]:
    return (
        location.module_type,
        location.url,
        location.object_id.hex,
        location.property_id.hex,
        location.line,
        location.extension_name,
        location.ext_id,
    )


class WorkerBreakpointCoordinator:
    """Own immutable logical Worker breakpoint catalog proposals.

    This layer resolves admitted source provenance only.  It intentionally does
    not contact RDBG and therefore cannot claim any installed physical binding.
    """

    __slots__ = (
        "_committed_proposals",
        "_installed_slots",
        "_lock",
        "_owner",
        "_quarantined_plans",
        "_session_id",
        "_snapshot",
        "_views",
    )

    def __init__(self, *, session_id: UUID) -> None:
        if type(session_id) is not UUID:
            raise TypeError("worker breakpoint session id must be a UUID")
        self._session_id = session_id
        self._owner = object()
        self._lock = RLock()
        self._views: tuple[WorkerGenerationDebugView, ...] = ()
        self._installed_slots: frozenset[ModuleLocation] = frozenset()
        self._committed_proposals: set[UUID] = set()
        self._quarantined_plans: list[WorkerBreakpointPlan] = []
        self._snapshot = WorkerBreakpointCatalogSnapshot(0, (), frozenset())

    def set_views(self, views: tuple[WorkerGenerationDebugView, ...]) -> None:
        if (
            type(views) is not tuple
            or any(type(view) is not WorkerGenerationDebugView for view in views)
            or len({id(view.handle) for view in views}) != len(views)
        ):
            raise TypeError("worker generation debug views are invalid")
        with self._lock:
            if views == self._views:
                return
            self._views = views
            definitions = self._definitions(self._snapshot)
            version = self._snapshot.catalog_version + 1
            self._snapshot, _slots = self._build_snapshot(
                definitions,
                self._snapshot.tombstone_ids,
                version,
            )

    def prepare_add(
        self,
        source_unit: SourceUnitRef,
        canonical_module: str,
        line: int,
        *,
        enabled: bool,
        column: int | None,
    ) -> WorkerBreakpointPlan:
        self._validate_request(
            source_unit,
            canonical_module,
            line,
            enabled=enabled,
            column=column,
        )
        canonical_module = canonical_module.casefold()
        with self._lock:
            identity = _breakpoint_identity(
                source_unit,
                canonical_module,
                line,
                column,
            )
            breakpoint_id = uuid5(self._session_id, identity)
            for status in self._snapshot.statuses:
                if status.breakpoint.id == breakpoint_id:
                    if (
                        status.breakpoint.source_unit != source_unit
                        or status.breakpoint.canonical_module != canonical_module
                        or status.breakpoint.line != line
                        or status.breakpoint.column != column
                    ):
                        raise ProtocolError("Worker breakpoint identity collision")
                    return self._plan(
                        result_id=breakpoint_id,
                        snapshot=self._snapshot,
                        removed_ids=(),
                    )
            if breakpoint_id in self._snapshot.tombstone_ids:
                raise ProtocolError("Removed Worker breakpoint identity cannot be reused")
            breakpoint = WorkerBreakpoint(
                breakpoint_id,
                source_unit,
                canonical_module,
                line,
                column,
            )
            definitions = self._definitions(self._snapshot) + ((breakpoint, enabled),)
            next_snapshot, slots = self._build_snapshot(
                definitions,
                self._snapshot.tombstone_ids,
                self._snapshot.catalog_version + 1,
            )
            return self._plan(
                result_id=breakpoint_id,
                snapshot=next_snapshot,
                removed_ids=(),
                desired_slots=slots,
            )

    def prepare_remove(self, breakpoint_id: UUID) -> WorkerBreakpointPlan:
        self._validate_breakpoint_id(breakpoint_id)
        with self._lock:
            if breakpoint_id in self._snapshot.tombstone_ids:
                return self._plan(
                    result_id=breakpoint_id,
                    snapshot=self._snapshot,
                    removed_ids=(),
                )
            definitions = tuple(
                item
                for item in self._definitions(self._snapshot)
                if item[0].id != breakpoint_id
            )
            if len(definitions) == len(self._snapshot.statuses):
                raise ProtocolError("Worker breakpoint id is unknown to this session")
            tombstones = self._snapshot.tombstone_ids | {breakpoint_id}
            next_snapshot, slots = self._build_snapshot(
                definitions,
                frozenset(tombstones),
                self._snapshot.catalog_version + 1,
            )
            return self._plan(
                result_id=breakpoint_id,
                snapshot=next_snapshot,
                removed_ids=(breakpoint_id,),
                desired_slots=slots,
            )

    def prepare_enabled(
        self,
        breakpoint_id: UUID,
        enabled: bool,
    ) -> WorkerBreakpointPlan:
        self._validate_breakpoint_id(breakpoint_id)
        if type(enabled) is not bool:
            raise TypeError("worker breakpoint enabled must be bool")
        with self._lock:
            if breakpoint_id in self._snapshot.tombstone_ids:
                raise ProtocolError("Worker breakpoint has been removed")
            definitions = list(self._definitions(self._snapshot))
            for index, (breakpoint, current_enabled) in enumerate(definitions):
                if breakpoint.id != breakpoint_id:
                    continue
                if current_enabled is enabled:
                    return self._plan(
                        result_id=breakpoint_id,
                        snapshot=self._snapshot,
                        removed_ids=(),
                    )
                definitions[index] = (breakpoint, enabled)
                next_snapshot, slots = self._build_snapshot(
                    tuple(definitions),
                    self._snapshot.tombstone_ids,
                    self._snapshot.catalog_version + 1,
                )
                return self._plan(
                    result_id=breakpoint_id,
                    snapshot=next_snapshot,
                    removed_ids=(),
                    desired_slots=slots,
                )
            raise ProtocolError("Worker breakpoint id is unknown to this session")

    def prepare_generation(
        self,
        candidate_view: WorkerGenerationDebugView,
        policy: WorkerBreakpointReloadPolicy,
    ) -> WorkerBreakpointPlan:
        if type(candidate_view) is not WorkerGenerationDebugView:
            raise TypeError("worker candidate debug view is required")
        if type(policy) is not WorkerBreakpointReloadPolicy:
            raise TypeError("worker breakpoint reload policy is required")
        with self._lock:
            if any(view.handle is candidate_view.handle for view in self._views):
                raise ProtocolError("Worker candidate debug view is already retained")
            incompatibilities = tuple(
                detail
                for status in self._snapshot.statuses
                if (
                    detail := self._candidate_incompatibility(
                        status.breakpoint,
                        candidate_view,
                    )
                )
                is not None
            )
            if policy is WorkerBreakpointReloadPolicy.STRICT:
                enabled_ids = {
                    status.breakpoint.id
                    for status in self._snapshot.statuses
                    if status.enabled
                }
                conflicts = tuple(
                    detail
                    for detail in incompatibilities
                    if detail.breakpoint_id in enabled_ids
                )
                if conflicts:
                    raise WorkerBreakpointConflict(conflicts)
                removals: tuple[WorkerBreakpointReloadRemoval, ...] = ()
            else:
                removals = incompatibilities
            removed_ids = tuple(item.breakpoint_id for item in removals)
            removed = frozenset(removed_ids)
            definitions = tuple(
                item
                for item in self._definitions(self._snapshot)
                if item[0].id not in removed
            )
            next_views = self._views + (candidate_view,)
            next_tombstones = self._snapshot.tombstone_ids | removed
            next_snapshot, desired_slots = self._build_snapshot(
                definitions,
                frozenset(next_tombstones),
                self._snapshot.catalog_version + 1,
                views=next_views,
                assume_desired_installed=True,
            )
            return self._plan(
                result_id=uuid4(),
                snapshot=next_snapshot,
                removed_ids=removed_ids,
                removal_details=removals,
                desired_slots=desired_slots,
                next_views=next_views,
            )

    def prepare_release(
        self,
        remaining_views: tuple[WorkerGenerationDebugView, ...],
    ) -> WorkerBreakpointPlan:
        if (
            type(remaining_views) is not tuple
            or any(
                type(view) is not WorkerGenerationDebugView
                for view in remaining_views
            )
            or any(
                all(view is not retained for retained in self._views)
                for view in remaining_views
            )
        ):
            raise TypeError("remaining Worker debug views are invalid")
        with self._lock:
            next_snapshot, desired_slots = self._build_snapshot(
                self._definitions(self._snapshot),
                self._snapshot.tombstone_ids,
                self._snapshot.catalog_version + 1,
                views=remaining_views,
                assume_desired_installed=True,
            )
            return self._plan(
                result_id=uuid4(),
                snapshot=next_snapshot,
                removed_ids=(),
                desired_slots=desired_slots,
                next_views=remaining_views,
            )

    def quarantine(self, plan: WorkerBreakpointPlan) -> None:
        if type(plan) is not WorkerBreakpointPlan:
            raise TypeError("worker breakpoint plan is required")
        with self._lock:
            if (
                plan.owner is not self._owner
                or plan.expected_catalog_version != self._snapshot.catalog_version
                or plan.proposal_id in self._committed_proposals
            ):
                raise ProtocolError("Worker breakpoint proposal is stale")
            self._quarantined_plans.append(plan)

    def commit(
        self,
        plan: WorkerBreakpointPlan,
        *,
        workspace_confirmed: bool = False,
    ) -> None:
        if type(plan) is not WorkerBreakpointPlan:
            raise TypeError("worker breakpoint plan is required")
        if type(workspace_confirmed) is not bool:
            raise TypeError("workspace confirmation must be bool")
        with self._lock:
            if (
                plan.owner is not self._owner
                or plan.expected_catalog_version != self._snapshot.catalog_version
                or plan.proposal_id in self._committed_proposals
            ):
                raise ProtocolError("Worker breakpoint proposal is stale")
            snapshot = plan.next_snapshot
            if workspace_confirmed:
                snapshot, desired_slots = self._build_snapshot(
                    self._definitions(plan.next_snapshot),
                    plan.next_snapshot.tombstone_ids,
                    plan.next_snapshot.catalog_version,
                    views=plan.next_views,
                    installed_slots=frozenset(plan.desired_slots),
                )
                if desired_slots != plan.desired_slots:
                    raise ProtocolError(
                        "Worker breakpoint proposal changed during commit"
                    )
            self._snapshot = snapshot
            self._views = plan.next_views
            if workspace_confirmed:
                self._installed_slots = frozenset(plan.desired_slots)
            self._committed_proposals.add(plan.proposal_id)

    def status(self, breakpoint_id: UUID) -> WorkerBreakpointStatus:
        self._validate_breakpoint_id(breakpoint_id)
        with self._lock:
            for status in self._snapshot.statuses:
                if status.breakpoint.id == breakpoint_id:
                    return status
        raise ProtocolError("Worker breakpoint id is unknown to this session")

    def list_statuses(self) -> tuple[WorkerBreakpointStatus, ...]:
        with self._lock:
            return self._snapshot.statuses

    def snapshot(self) -> WorkerBreakpointCatalogSnapshot:
        with self._lock:
            return self._snapshot

    def bindings_for_view(
        self,
        view: WorkerGenerationDebugView,
    ) -> tuple[WorkerBreakpointBinding, ...]:
        if type(view) is not WorkerGenerationDebugView:
            raise TypeError("worker generation debug view is required")
        with self._lock:
            if all(view is not retained for retained in self._views):
                raise ProtocolError("Worker generation debug view is not retained")
            bindings: list[WorkerBreakpointBinding] = []
            for status in self._snapshot.statuses:
                if not status.enabled:
                    continue
                breakpoint = status.breakpoint
                modules = tuple(
                    module
                    for module in view.modules
                    if module.canonical_module == breakpoint.canonical_module
                    and breakpoint.source_unit in module.source_units
                )
                if not modules:
                    continue
                if len(modules) != 1:
                    raise ProtocolError(
                        "Worker generation contains duplicate module views"
                    )
                mapping = resolve_source_line(
                    modules[0],
                    breakpoint.source_unit,
                    breakpoint.line,
                )
                if mapping.generated_line is None:
                    continue
                bindings.append(
                    WorkerBreakpointBinding(
                        breakpoint.id,
                        view.handle,
                        modules[0].registration.module_location(
                            mapping.generated_line
                        ),
                    )
                )
            return tuple(
                sorted(
                    bindings,
                    key=lambda item: (
                        _module_location_key(item.location),
                        item.breakpoint_id.hex,
                    ),
                )
            )

    @staticmethod
    def _validate_request(
        source_unit: SourceUnitRef,
        canonical_module: str,
        line: int,
        *,
        enabled: bool,
        column: int | None,
    ) -> None:
        if not isinstance(source_unit, SourceUnitRef):
            raise TypeError("worker breakpoint source unit is required")
        if type(canonical_module) is not str or not canonical_module:
            raise ValueError("worker breakpoint module is required")
        if type(line) is not int or line < 1:
            raise ValueError("worker breakpoint line must be positive")
        if type(enabled) is not bool:
            raise TypeError("worker breakpoint enabled must be bool")
        if column is not None:
            raise ValueError("worker breakpoint columns are not supported")

    @staticmethod
    def _validate_breakpoint_id(breakpoint_id: UUID) -> None:
        if type(breakpoint_id) is not UUID:
            raise TypeError("worker breakpoint id must be a UUID")

    @staticmethod
    def _definitions(
        snapshot: WorkerBreakpointCatalogSnapshot,
    ) -> tuple[tuple[WorkerBreakpoint, bool], ...]:
        return tuple((status.breakpoint, status.enabled) for status in snapshot.statuses)

    def _plan(
        self,
        *,
        result_id: UUID,
        snapshot: WorkerBreakpointCatalogSnapshot,
        removed_ids: tuple[UUID, ...],
        removal_details: tuple[WorkerBreakpointReloadRemoval, ...] = (),
        desired_slots: tuple[ModuleLocation, ...] | None = None,
        next_views: tuple[WorkerGenerationDebugView, ...] | None = None,
    ) -> WorkerBreakpointPlan:
        if next_views is None:
            next_views = self._views
        if desired_slots is None:
            _ignored, desired_slots = self._build_snapshot(
                self._definitions(snapshot),
                snapshot.tombstone_ids,
                snapshot.catalog_version,
            )
        return WorkerBreakpointPlan(
            self._owner,
            uuid4(),
            self._snapshot.catalog_version,
            result_id,
            desired_slots,
            removed_ids,
            removal_details,
            next_views,
            snapshot,
        )

    def _build_snapshot(
        self,
        definitions: tuple[tuple[WorkerBreakpoint, bool], ...],
        tombstones: frozenset[UUID],
        version: int,
        *,
        views: tuple[WorkerGenerationDebugView, ...] | None = None,
        installed_slots: frozenset[ModuleLocation] | None = None,
        assume_desired_installed: bool = False,
    ) -> tuple[WorkerBreakpointCatalogSnapshot, tuple[ModuleLocation, ...]]:
        statuses: list[WorkerBreakpointStatus] = []
        desired: set[ModuleLocation] = set()
        active_views = self._views if views is None else views
        active_installed_slots = (
            self._installed_slots if installed_slots is None else installed_slots
        )
        for breakpoint, enabled in definitions:
            status, slots = self._resolve_breakpoint(
                breakpoint,
                enabled=enabled,
                version=version,
                views=active_views,
                installed_slots=active_installed_slots,
                assume_desired_installed=assume_desired_installed,
            )
            statuses.append(status)
            if enabled:
                desired.update(slots)
        desired_slots = tuple(sorted(desired, key=_module_location_key))
        return (
            WorkerBreakpointCatalogSnapshot(version, tuple(statuses), tombstones),
            desired_slots,
        )

    def _resolve_breakpoint(
        self,
        breakpoint: WorkerBreakpoint,
        *,
        enabled: bool,
        version: int,
        views: tuple[WorkerGenerationDebugView, ...],
        installed_slots: frozenset[ModuleLocation],
        assume_desired_installed: bool,
    ) -> tuple[WorkerBreakpointStatus, tuple[ModuleLocation, ...]]:
        generation_statuses: list[WorkerBreakpointGenerationStatus] = []
        resolved_slots: set[ModuleLocation] = set()
        exact_rejections: list[WorkerBreakpointRejection] = []
        saw_module = False
        for generation_view in views:
            module_views = tuple(
                module
                for module in generation_view.modules
                if module.canonical_module == breakpoint.canonical_module
            )
            if len(module_views) > 1:
                raise ProtocolError("Worker generation contains duplicate module views")
            if not module_views:
                generation_statuses.append(
                    WorkerBreakpointGenerationStatus(
                        generation_view.handle,
                        WorkerBreakpointResolution.PENDING,
                        WorkerBreakpointRejection.NO_RETAINED_COMPATIBLE_ARTIFACT,
                    )
                )
                continue
            saw_module = True
            module = module_views[0]
            if breakpoint.source_unit not in module.source_units:
                generation_statuses.append(
                    WorkerBreakpointGenerationStatus(
                        generation_view.handle,
                        WorkerBreakpointResolution.PENDING,
                        WorkerBreakpointRejection.SOURCE_REVISION_NOT_ADMITTED,
                    )
                )
                continue
            mapping = resolve_source_line(
                module,
                breakpoint.source_unit,
                breakpoint.line,
            )
            if mapping.reason is not None:
                exact_rejections.append(mapping.reason)
                generation_statuses.append(
                    WorkerBreakpointGenerationStatus(
                        generation_view.handle,
                        WorkerBreakpointResolution.REJECTED,
                        mapping.reason,
                    )
                )
                continue
            if mapping.generated_line is None:
                raise ProtocolError("Worker breakpoint resolution lost generated line")
            slot = module.registration.module_location(mapping.generated_line)
            resolved_slots.add(slot)
            generation_statuses.append(
                WorkerBreakpointGenerationStatus(
                    generation_view.handle,
                    WorkerBreakpointResolution.RESOLVED,
                    None,
                )
            )

        if resolved_slots:
            resolution = WorkerBreakpointResolution.RESOLVED
            reason = None
        elif exact_rejections:
            resolution = WorkerBreakpointResolution.REJECTED
            reason = (
                exact_rejections[0]
                if len(set(exact_rejections)) == 1
                else WorkerBreakpointRejection.AMBIGUOUS_LINE_MAPPING
            )
        else:
            resolution = WorkerBreakpointResolution.PENDING
            reason = (
                WorkerBreakpointRejection.SOURCE_REVISION_NOT_ADMITTED
                if saw_module
                else WorkerBreakpointRejection.NO_RETAINED_COMPATIBLE_ARTIFACT
            )
        installed_count = (
            (
                len(resolved_slots)
                if assume_desired_installed
                else len(resolved_slots & installed_slots)
            )
            if enabled and resolution is WorkerBreakpointResolution.RESOLVED
            else 0
        )
        status = WorkerBreakpointStatus(
            breakpoint,
            enabled,
            resolution,
            reason,
            tuple(generation_statuses),
            installed_count,
            version,
        )
        return status, tuple(sorted(resolved_slots, key=_module_location_key))

    @staticmethod
    def _candidate_incompatibility(
        breakpoint: WorkerBreakpoint,
        candidate_view: WorkerGenerationDebugView,
    ) -> WorkerBreakpointReloadRemoval | None:
        modules = tuple(
            module
            for module in candidate_view.modules
            if module.canonical_module == breakpoint.canonical_module
        )
        if len(modules) > 1:
            raise ProtocolError("Worker generation contains duplicate module views")
        if not modules:
            reason = WorkerBreakpointRejection.MODULE_NOT_IN_CANDIDATE
        elif breakpoint.source_unit not in modules[0].source_units:
            reason = WorkerBreakpointRejection.SOURCE_IDENTITY_MISMATCH
        else:
            mapping = resolve_source_line(
                modules[0],
                breakpoint.source_unit,
                breakpoint.line,
            )
            reason = mapping.reason
        return (
            None
            if reason is None
            else WorkerBreakpointReloadRemoval(breakpoint.id, reason)
        )


def require_exact_worker_module_or_native(
    location: ModuleLocation,
    view: WorkerGenerationDebugView,
) -> WorkerModuleDebugView | None:
    if type(location) is not ModuleLocation:
        raise TypeError("debug module location is required")
    if type(view) is not WorkerGenerationDebugView:
        raise TypeError("worker generation debug view is required")
    if type(location.line) is not int or location.line < 1:
        raise ProtocolError("Debug frame line is invalid")
    expected_locations = tuple(
        (module, module.registration.module_location(location.line))
        for module in view.modules
    )
    exact = tuple(
        module
        for module, expected in expected_locations
        if location == expected
    )
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise ProtocolError("Worker physical module identity is ambiguous")
    for _module, expected in expected_locations:
        same_worker_shape = (
            location.module_type == expected.module_type
            and location.object_id == expected.object_id
            and location.property_id == expected.property_id
            and location.extension_name == expected.extension_name
            and location.ext_id == expected.ext_id
        )
        if location.url == expected.url or same_worker_shape:
            raise ProtocolError("Worker physical module identity is stale or forged")
    return None


def mint_worker_frame(
    level: int,
    source: WorkerSourceLocation,
    view: WorkerGenerationDebugView,
    module: WorkerModuleDebugView,
    raw_frame: StackFrame,
) -> WorkerMappedFrame:
    if (
        type(level) is not int
        or level < 0
        or type(source) is not WorkerSourceLocation
        or type(view) is not WorkerGenerationDebugView
        or type(module) is not WorkerModuleDebugView
        or type(raw_frame) is not StackFrame
        or all(module is not candidate for candidate in view.modules)
        or raw_frame.level != level
    ):
        raise ProtocolError("Worker frame provenance is inconsistent")
    return WorkerMappedFrame(
        level,
        source,
        view.handle,
        module.artifact_sha256,
        module.source_map_sha256,
        WorkerFrameProvenance(
            _WORKER_FRAME_PROVENANCE_OWNER,
            raw_frame,
            module.registration.target_incarnation_id,
        ),
    )


def _stop_stack_frames(event: StopEvent) -> tuple[StackFrame, ...]:
    if type(event) is not StopEvent or type(event.target_id) is not TargetId:
        raise TypeError("RDBG stop event is required")
    if event.stack_frames:
        frames = event.stack_frames
        levels = tuple(frame.level for frame in frames)
        if (
            any(type(frame) is not StackFrame for frame in frames)
            or any(frame.target_id != event.target_id for frame in frames)
            or any(type(level) is not int or level < 0 for level in levels)
            or len(set(levels)) != len(levels)
        ):
            raise ProtocolError("Worker debug stack is incoherent")
        frames = tuple(sorted(frames, key=lambda frame: frame.level))
        if frames[0].level != 0 or frames[0].location != event.location:
            raise ProtocolError("Worker debug stack top does not match the stop")
        if event.stack and tuple(frame.location for frame in frames) != event.stack:
            raise ProtocolError("Worker debug stack representations disagree")
        return frames
    locations = event.stack or (event.location,)
    if locations[0] != event.location:
        raise ProtocolError("Worker debug stack top does not match the stop")
    return tuple(
        StackFrame(event.target_id, level, location)
        for level, location in enumerate(locations)
    )


def map_worker_stop(
    event: StopEvent,
    *,
    operation_id: int,
    view: WorkerGenerationDebugView,
    origin: str,
    bindings: tuple[WorkerBreakpointBinding, ...],
    reason: StopReason = StopReason.USER_BREAKPOINT,
) -> RuntimeDebugStop:
    if type(operation_id) is not int or operation_id < 0:
        raise ValueError("debug stop operation id is invalid")
    if type(view) is not WorkerGenerationDebugView:
        raise TypeError("worker generation debug view is required")
    if origin not in {"main", "capture_evaluation"}:
        raise ValueError("debug stop origin is invalid")
    if (
        type(bindings) is not tuple
        or any(type(binding) is not WorkerBreakpointBinding for binding in bindings)
        or any(binding.generation is not view.handle for binding in bindings)
    ):
        raise ProtocolError("Worker breakpoint bindings do not match generation")
    if type(reason) is not StopReason:
        raise TypeError("debug stop reason is required")

    top_module = require_exact_worker_module_or_native(event.location, view)
    if top_module is None:
        public_location: WorkerSourceLocation | ModuleLocation = event.location
    else:
        mapped_top = map_generated_line(top_module, event.location.line)
        if mapped_top is None:
            raise ProtocolError("Worker stopped in synthetic generated source")
        public_location = mapped_top

    visible_frames: list[WorkerMappedFrame | NativeDebugFrame] = []
    for raw_frame in _stop_stack_frames(event):
        module = require_exact_worker_module_or_native(raw_frame.location, view)
        if module is None:
            visible_frames.append(
                NativeDebugFrame(raw_frame.level, raw_frame.location)
            )
            continue
        source = map_generated_line(module, raw_frame.location.line)
        if source is None:
            continue
        visible_frames.append(
            mint_worker_frame(raw_frame.level, source, view, module, raw_frame)
        )
    breakpoint_ids = tuple(
        sorted(
            {
                binding.breakpoint_id
                for binding in bindings
                if binding.location == event.location
            },
            key=lambda item: item.hex,
        )
    )
    return RuntimeDebugStop(
        reason,
        origin,
        operation_id,
        public_location,
        tuple(visible_frames),
        breakpoint_ids,
    )
