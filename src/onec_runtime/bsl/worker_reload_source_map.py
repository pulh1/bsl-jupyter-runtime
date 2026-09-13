"""Single-pass Worker reload insertion materialization and source mapping."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field, replace
from enum import Enum
from hashlib import sha256
from hmac import compare_digest
import json
from typing import Callable, Iterable

from onec_runtime.bsl.source_maps import (
    MappedOffset,
    MappedSource,
    MappingRelation,
    SourceArtifactKind,
    SourceArtifactRef,
    SourceMap,
    SourceMapSegment,
    SourceSpan,
    SourceUnitRef,
    _NORMALIZED_WORKER_AUTHORITY,
    _source_map_structural_proof,
)


WORKER_LINE_ENDING_REGION = "worker_module_line_ending"
_WORK_OBSERVER: Callable[[str, int], None] | None = None


def _observe_work(event: str, amount: int = 1) -> None:
    if _WORK_OBSERVER is not None:
        _WORK_OBSERVER(event, amount)


class _InsertionRelation(Enum):
    DERIVED = "derived"
    SYNTHETIC = "synthetic"


@dataclass(frozen=True, slots=True)
class ReloadInsertion:
    source_offset: int
    generated_length: int
    method_name: str | None
    synthetic_region: str
    _text: str = field(repr=False)
    _relation: _InsertionRelation = field(repr=False)
    _origin: SourceSpan | None = field(repr=False)
    _anchor: SourceSpan = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.source_offset) is not int or self.source_offset < 0:
            raise ValueError("insertion source offset must be non-negative")
        if (
            type(self.generated_length) is not int
            or self.generated_length != len(self._text)
            or not self._text
        ):
            raise ValueError("insertion generated length is invalid")
        if self.method_name is not None and (
            type(self.method_name) is not str or not self.method_name
        ):
            raise ValueError("insertion method name is invalid")
        if type(self.synthetic_region) is not str or not self.synthetic_region:
            raise ValueError("insertion synthetic region is invalid")
        if not isinstance(self._relation, _InsertionRelation):
            raise ValueError("insertion relation is invalid")
        if not isinstance(self._anchor, SourceSpan):
            raise ValueError("insertion anchor is invalid")
        if self._relation is _InsertionRelation.DERIVED:
            if not isinstance(self._origin, SourceSpan):
                raise ValueError("derived insertion origin is invalid")
        elif self._origin is not None:
            raise ValueError("synthetic insertion cannot claim an origin")
        if "\r" in self._text or "\x00" in self._text:
            raise ValueError("insertion payload is not normalized")

    @classmethod
    def derived(
        cls,
        *,
        source_offset: int,
        text: str,
        method_name: str | None,
        synthetic_region: str,
        origin: SourceSpan,
        anchor: SourceSpan,
    ) -> "ReloadInsertion":
        return cls(
            source_offset,
            len(text),
            method_name,
            synthetic_region,
            text,
            _InsertionRelation.DERIVED,
            origin,
            anchor,
        )

    @classmethod
    def synthetic(
        cls,
        *,
        source_offset: int,
        text: str,
        method_name: str | None,
        synthetic_region: str,
        anchor: SourceSpan,
    ) -> "ReloadInsertion":
        return cls(
            source_offset,
            len(text),
            method_name,
            synthetic_region,
            text,
            _InsertionRelation.SYNTHETIC,
            None,
            anchor,
        )


@dataclass(frozen=True, slots=True)
class ReloadMapCheckpoint:
    source_offset: int
    generated_offset: int
    inserted_length: int


@dataclass(frozen=True, slots=True)
class ReloadMethodBoundary:
    method_name: str
    declaration_start: int
    body_start: int
    method_end: int


@dataclass(frozen=True, slots=True)
class ReloadMethodCheckpoint:
    method_name: str
    declaration_source_offset: int
    declaration_generated_offset: int
    body_source_offset: int
    body_generated_offset: int
    end_source_offset: int
    end_generated_offset: int

    @property
    def declaration_shift(self) -> int:
        return self.declaration_generated_offset - self.declaration_source_offset

    @property
    def body_shift(self) -> int:
        return self.body_generated_offset - self.body_source_offset

    @property
    def end_shift(self) -> int:
        return self.end_generated_offset - self.end_source_offset


@dataclass(frozen=True, slots=True)
class _CompactInsertionInterval:
    generated: SourceSpan
    source_offset: int
    method_name: str | None
    relation: MappingRelation
    synthetic_region: str
    origin_ref: SourceUnitRef | None
    origin: SourceSpan | None
    anchor_ref: SourceUnitRef | None
    anchor_span: SourceSpan | None
    source_origin: SourceSpan | None
    source_anchor: SourceSpan


@dataclass(frozen=True, slots=True)
class _CompactReloadBasis:
    parent_artifact: SourceArtifactRef
    parent_source_map: SourceMap
    source_length: int
    intervals: tuple[_CompactInsertionInterval, ...]
    interval_starts: tuple[int, ...]
    checkpoints: tuple[ReloadMapCheckpoint, ...]
    checkpoint_offsets: tuple[int, ...]
    inserted_after: tuple[int, ...]
    line_ending_starts: tuple[int, ...]
    line_ending_widths: bytes
    crlf_lf_offsets: tuple[int, ...]
    method_checkpoints: tuple[ReloadMethodCheckpoint, ...]


class CompactReloadSourceMap(SourceMap):
    """Immutable reload map whose unchanged source ranges remain implicit."""

    __slots__ = (
        "_compact_generated",
        "_compact_basis",
        "_compact_flattened",
        "_canonical_manifest_bytes",
        "_canonical_sha256",
        "_structural_proof",
    )
    _compact_reload_map = True

    def __init__(
        self,
        generated: SourceArtifactRef,
        basis: _CompactReloadBasis,
        *,
        flattened: bool,
    ) -> None:
        if not isinstance(basis, _CompactReloadBasis) or type(flattened) is not bool:
            raise ValueError("compact reload map basis is invalid")
        _validate_compact_basis(basis, generated)
        object.__setattr__(self, "_compact_generated", generated)
        object.__setattr__(self, "_compact_basis", basis)
        object.__setattr__(self, "_compact_flattened", flattened)
        payload = json.dumps(
            self.to_compact_manifest(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        object.__setattr__(self, "_canonical_manifest_bytes", payload)
        object.__setattr__(self, "_canonical_sha256", sha256(payload).hexdigest())
        object.__setattr__(
            self,
            "_structural_proof",
            sha256(b"onec-compact-reload-map-v1\x00" + payload).digest(),
        )

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("CompactReloadSourceMap is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("CompactReloadSourceMap is immutable")

    @property
    def generated(self) -> SourceArtifactRef:
        return self._compact_generated

    @property
    def segments(self) -> tuple[SourceMapSegment, ...]:
        """Materialize the legacy representation only for an explicit consumer."""
        return self.materialize_generic().segments

    @property
    def checkpoints(self) -> tuple[ReloadMapCheckpoint, ...]:
        return self._compact_basis.checkpoints

    @property
    def compact_interval_count(self) -> int:
        return len(self._compact_basis.intervals)

    @property
    def method_checkpoints(self) -> tuple[ReloadMethodCheckpoint, ...]:
        return self._compact_basis.method_checkpoints

    @property
    def source_map_sha256(self) -> str:
        return self._canonical_sha256

    @property
    def generic_materialized(self) -> bool:
        return False

    def map_offset(self, offset: int) -> MappedOffset:
        if not self._compact_flattened:
            return self.materialize_generic().map_offset(offset)
        if type(offset) is not int or not 0 <= offset <= self.generated.character_length:
            raise ValueError("offset must be within the generated source")
        interval = self._interval_at(offset)
        if interval is not None:
            return _mapped_compact_interval(interval)
        if offset == self.generated.character_length:
            if offset:
                previous_interval = self._interval_at(offset - 1)
                if previous_interval is not None:
                    return _mapped_compact_interval(previous_interval)
                original = self._original_offset_at_generated(offset - 1)
                ending_width = self._line_ending_width(original)
                if ending_width is not None:
                    projected = _ParentProjector(
                        self._compact_basis.parent_source_map
                    ).derived(
                        SourceSpan(original, original + ending_width),
                        SourceSpan(0, 1),
                        WORKER_LINE_ENDING_REGION,
                    )
                    return _mapped_compact_segment(projected)
            return self._compact_basis.parent_source_map.map_offset(
                self._compact_basis.source_length
            )
        original = self._original_offset_at_generated(offset)
        ending_width = self._line_ending_width(original)
        if ending_width is not None:
            projected = _ParentProjector(
                self._compact_basis.parent_source_map
            ).derived(
                SourceSpan(original, original + ending_width),
                SourceSpan(0, 1),
                WORKER_LINE_ENDING_REGION,
            )
            return _mapped_compact_segment(projected)
        return self._compact_basis.parent_source_map.map_offset(original)

    def map_original_offset(self, offset: int) -> int:
        basis = self._compact_basis
        if type(offset) is not int or not 0 <= offset <= basis.source_length:
            raise ValueError("offset must be within the original source")
        checkpoint_index = bisect_right(basis.checkpoint_offsets, offset) - 1
        inserted = (
            0
            if checkpoint_index < 0
            else basis.inserted_after[checkpoint_index]
        )
        removed_crlf = bisect_right(basis.crlf_lf_offsets, offset - 1)
        return offset - removed_crlf + inserted

    def materialize_generic(self) -> SourceMap:
        return _materialize_compact_generic(self)

    def to_manifest(self) -> dict[str, object]:
        return self.to_compact_manifest()

    def to_compact_manifest(self) -> dict[str, object]:
        basis = self._compact_basis
        unit_values = {
            unit
            for interval in basis.intervals
            for unit in (interval.origin_ref, interval.anchor_ref)
            if unit is not None
        }
        for _role, unit, _span in self.compact_visible_spans():
            unit_values.add(unit)
        units = tuple(
            sorted(
                unit_values,
                key=lambda item: (
                    item.kind.value,
                    item.unit_id,
                    item.revision,
                    item.source_sha256,
                ),
            )
        )
        unit_indices = {unit: index for index, unit in enumerate(units)}
        methods = tuple(
            sorted(
                {
                    item.method_name
                    for item in basis.intervals
                    if item.method_name is not None
                },
                key=str.casefold,
            )
        )
        method_names = tuple(
            sorted(
                {item.method_name for item in basis.method_checkpoints} | set(methods),
                key=str.casefold,
            )
        )
        method_indices = {name: index for index, name in enumerate(method_names)}
        regions = tuple(
            sorted({item.synthetic_region for item in basis.intervals})
        )
        region_indices = {name: index for index, name in enumerate(regions)}
        return {
            "schema": "onec-compact-worker-reload-map-v1",
            "flattened": self._compact_flattened,
            "generated": _artifact_manifest(self.generated),
            "parent_artifact": _artifact_manifest(basis.parent_artifact),
            "parent_map_sha256": basis.parent_source_map.source_map_sha256,
            "source_length": basis.source_length,
            "units": [
                [unit.kind.value, unit.unit_id, unit.revision, unit.source_sha256]
                for unit in units
            ],
            "methods": list(method_names),
            "regions": list(regions),
            "line_endings": _encoded_line_endings(
                basis.line_ending_starts,
                basis.line_ending_widths,
            ),
            "checkpoints": [
                [item.source_offset, item.generated_offset, item.inserted_length]
                for item in basis.checkpoints
            ],
            "intervals": [
                [
                    item.generated.start,
                    item.generated.end,
                    item.source_offset,
                    (
                        None
                        if item.method_name is None
                        else method_indices[item.method_name]
                    ),
                    item.relation.value,
                    region_indices[item.synthetic_region],
                    *_indexed_span(item.origin_ref, item.origin, unit_indices),
                    *_indexed_span(item.anchor_ref, item.anchor_span, unit_indices),
                    *_plain_span(item.source_origin),
                    *_plain_span(item.source_anchor),
                ]
                for item in basis.intervals
            ],
            "method_checkpoints": [
                [
                    method_indices[item.method_name],
                    item.declaration_source_offset,
                    item.declaration_generated_offset,
                    item.body_source_offset,
                    item.body_generated_offset,
                    item.end_source_offset,
                    item.end_generated_offset,
                ]
                for item in basis.method_checkpoints
            ],
        }

    def compact_visible_spans(
        self,
    ) -> tuple[tuple[str, SourceUnitRef, SourceSpan], ...]:
        records: list[tuple[str, SourceUnitRef, SourceSpan]] = []
        for segment in self._compact_basis.parent_source_map.segments:
            for role, reference, span in (
                ("origin", segment.origin_ref, segment.origin),
                ("anchor", segment.anchor_ref, segment.anchor_span),
            ):
                if isinstance(reference, SourceUnitRef) and span is not None:
                    records.append((role, reference, span))
        for interval in self._compact_basis.intervals:
            for role, reference, span in (
                ("origin", interval.origin_ref, interval.origin),
                ("anchor", interval.anchor_ref, interval.anchor_span),
            ):
                if reference is not None and span is not None:
                    records.append((role, reference, span))
        return tuple(records)

    def mapped_spans_for_visible_span(
        self,
        reference: SourceUnitRef,
        requested: SourceSpan,
    ) -> tuple[tuple[MappingRelation, SourceSpan], ...]:
        """Return only generated ranges relevant to one visible source interval."""
        if not isinstance(reference, SourceUnitRef) or not isinstance(
            requested, SourceSpan
        ):
            raise TypeError("visible source reference and span are required")
        if not self._compact_flattened:
            raise ValueError("visible span lookup requires a flattened reload map")

        records: list[tuple[MappingRelation, SourceSpan]] = []
        exact_offsets: set[int] = set()
        for segment in self._compact_basis.parent_source_map.segments:
            if (
                segment.relation is not MappingRelation.EXACT
                or segment.origin_ref != reference
                or segment.origin is None
            ):
                continue
            start = max(segment.origin.start, requested.start)
            end = min(segment.origin.end, requested.end)
            if start >= end:
                continue
            for origin_offset in range(start, end):
                parent_offset = (
                    segment.generated.start + origin_offset - segment.origin.start
                )
                exact_offsets.add(self.map_original_offset(parent_offset))

        for generated_offset in sorted(exact_offsets):
            mapped = self.map_offset(generated_offset)
            if (
                mapped.relation is MappingRelation.EXACT
                and mapped.unit == reference
                and mapped.origin_span is not None
                and mapped.origin_span.start < requested.end
                and requested.start < mapped.origin_span.end
            ):
                records.append(
                    (
                        MappingRelation.EXACT,
                        SourceSpan(generated_offset, generated_offset + 1),
                    )
                )

        for interval in self._compact_basis.intervals:
            visible_span = (
                interval.origin
                if interval.origin_ref == reference
                else interval.anchor_span
                if interval.anchor_ref == reference
                else None
            )
            if (
                visible_span is not None
                and visible_span.start < requested.end
                and requested.start < visible_span.end
            ):
                records.append((interval.relation, interval.generated))
        return tuple(records)

    def compact_guard(self) -> tuple[int, ...]:
        basis = self._compact_basis
        return (
            id(self),
            id(basis),
            id(basis.intervals),
            id(basis.interval_starts),
            id(basis.checkpoints),
            id(basis.checkpoint_offsets),
            id(basis.inserted_after),
            id(basis.line_ending_starts),
            id(basis.line_ending_widths),
            id(basis.crlf_lf_offsets),
            id(basis.method_checkpoints),
            id(self._canonical_manifest_bytes),
            id(basis.parent_artifact),
            id(basis.parent_source_map),
        )

    def _validated_compact_structural_proof(self) -> bytes:
        basis = self._compact_basis
        _validate_compact_basis(basis, self.generated)
        actual_parent_proof = _source_map_structural_proof(
            basis.parent_source_map,
            observe=True,
        )
        if not compare_digest(
            basis.parent_source_map._structural_proof,
            actual_parent_proof,
        ):
            raise ValueError("compact reload parent map is invalid")
        payload = json.dumps(
            self.to_compact_manifest(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        proof = sha256(b"onec-compact-reload-map-v1\x00" + payload).digest()
        return proof

    def _interval_at(self, offset: int) -> _CompactInsertionInterval | None:
        basis = self._compact_basis
        if not basis.intervals:
            return None
        index = bisect_right(basis.interval_starts, offset) - 1
        if index < 0:
            return None
        interval = basis.intervals[index]
        if interval.generated.start <= offset < interval.generated.end:
            return interval
        if offset == self.generated.character_length and interval.generated.end == offset:
            return interval
        return None

    def _original_offset_at_generated(self, generated: int) -> int:
        low = 0
        high = self._compact_basis.source_length
        while low < high:
            middle = (low + high + 1) // 2
            if self.map_original_offset(middle) <= generated:
                low = middle
            else:
                high = middle - 1
        return low

    def _line_ending_width(self, source_offset: int) -> int | None:
        basis = self._compact_basis
        index = bisect_right(basis.line_ending_starts, source_offset) - 1
        if index < 0 or basis.line_ending_starts[index] != source_offset:
            return None
        return basis.line_ending_widths[index]

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, CompactReloadSourceMap)
            and self._canonical_manifest_bytes == other._canonical_manifest_bytes
        )

    def __hash__(self) -> int:
        return hash(self._canonical_sha256)

    def __repr__(self) -> str:
        return (
            "CompactReloadSourceMap(artifact="
            f"{self.generated.kind.value}:{self.generated.source_sha256}, "
            f"intervals={len(self._compact_basis.intervals)})"
        )


class CompactMappedWorkerSource(MappedSource):
    """Mapped Worker source backed by a compact reload map."""

    __slots__ = ()


def snapshot_compact_mapped_worker_source(
    source: CompactMappedWorkerSource,
) -> CompactMappedWorkerSource:
    """Detach the compact map graph without materializing generic segments."""

    if not isinstance(source, CompactMappedWorkerSource) or not isinstance(
        source.source_map,
        CompactReloadSourceMap,
    ):
        raise ValueError("compact Worker source is invalid")
    current = source.source_map
    basis = current._compact_basis
    map_cache: dict[int, SourceMap] = {}
    parent_map = _clone_source_map(basis.parent_source_map, map_cache)
    generated = replace(source.artifact)
    intervals = tuple(
        replace(
            interval,
            generated=replace(interval.generated),
            origin_ref=(
                None if interval.origin_ref is None else replace(interval.origin_ref)
            ),
            origin=None if interval.origin is None else replace(interval.origin),
            anchor_ref=(
                None if interval.anchor_ref is None else replace(interval.anchor_ref)
            ),
            anchor_span=(
                None if interval.anchor_span is None else replace(interval.anchor_span)
            ),
            source_origin=(
                None
                if interval.source_origin is None
                else replace(interval.source_origin)
            ),
            source_anchor=replace(interval.source_anchor),
        )
        for interval in basis.intervals
    )
    cloned_basis = _compact_basis(
        replace(basis.parent_artifact),
        parent_map,
        basis.source_length,
        intervals,
        tuple(replace(item) for item in basis.checkpoints),
        basis.line_ending_starts,
        basis.line_ending_widths,
        tuple(replace(item) for item in basis.method_checkpoints),
    )
    flattened = CompactReloadSourceMap(
        generated,
        cloned_basis,
        flattened=True,
    )
    local = CompactReloadSourceMap(
        generated,
        cloned_basis,
        flattened=False,
    )
    lineage_prefix = tuple(
        _clone_source_map(item, map_cache)
        for item in source.lineage[:-1]
    )
    return CompactMappedWorkerSource(
        source.text,
        generated,
        flattened,
        (*lineage_prefix, local),
        local,
        _normalized_worker_authority=_NORMALIZED_WORKER_AUTHORITY,
    )


def _clone_source_map(
    source_map: SourceMap,
    cache: dict[int, SourceMap],
) -> SourceMap:
    existing = cache.get(id(source_map))
    if existing is not None:
        return existing
    cloned = SourceMap(
        replace(source_map.generated),
        tuple(
            replace(
                segment,
                generated=replace(segment.generated),
                origin_ref=(
                    None if segment.origin_ref is None else replace(segment.origin_ref)
                ),
                origin=None if segment.origin is None else replace(segment.origin),
                anchor_ref=(
                    None if segment.anchor_ref is None else replace(segment.anchor_ref)
                ),
                anchor_span=(
                    None if segment.anchor_span is None else replace(segment.anchor_span)
                ),
            )
            for segment in source_map.segments
        ),
    )
    cache[id(source_map)] = cloned
    return cloned


class _ParentProjector:
    def __init__(self, source_map: SourceMap) -> None:
        self._segments = tuple(
            segment
            for segment in source_map.segments
            if segment.generated.start < segment.generated.end
        )
        self._starts = tuple(segment.generated.start for segment in self._segments)
        self._cursor = 0

    def exact(self, span: SourceSpan, generated_start: int) -> list[SourceMapSegment]:
        overlapping = self._overlapping_monotonic(span)
        result: list[SourceMapSegment] = []
        for parent in overlapping:
            start = max(parent.generated.start, span.start)
            end = min(parent.generated.end, span.end)
            generated = SourceSpan(
                generated_start + start - span.start,
                generated_start + end - span.start,
            )
            result.append(
                _project_exact_piece(parent, SourceSpan(start, end), generated)
            )
        if not result:
            raise ValueError("source fragment has no visible mapping")
        return result

    def derived(
        self,
        span: SourceSpan,
        generated: SourceSpan,
        region: str,
    ) -> SourceMapSegment:
        overlapping = self._overlapping_monotonic(span)
        synthetic = next(
            (
                segment
                for segment in overlapping
                if segment.relation is MappingRelation.SYNTHETIC
            ),
            None,
        )
        if synthetic is not None:
            return SourceMapSegment(
                generated,
                None,
                None,
                MappingRelation.SYNTHETIC,
                synthetic.synthetic_region,
                synthetic.anchor_ref,
                synthetic.anchor_span,
            )
        unit, origin = _anchor_from_segments(overlapping, span)
        if unit is None or origin is None:
            raise ValueError("derived source fragment crosses visible source units")
        return SourceMapSegment(
            generated,
            unit,
            origin,
            MappingRelation.DERIVED,
            region,
        )

    def anchor(
        self,
        span: SourceSpan,
    ) -> tuple[SourceUnitRef | None, SourceSpan | None]:
        if span.start == span.end:
            if not self._segments:
                return None, None
            index = bisect_right(self._starts, span.start) - 1
            if index < 0:
                index = 0
            if index >= len(self._segments):
                index = len(self._segments) - 1
            segment = self._segments[index]
            _observe_work("parent_segment_visit")
            if segment.relation is MappingRelation.SYNTHETIC:
                return (
                    segment.anchor_ref
                    if isinstance(segment.anchor_ref, SourceUnitRef)
                    else None,
                    segment.anchor_span,
                )
            if (
                not isinstance(segment.origin_ref, SourceUnitRef)
                or segment.origin is None
            ):
                return None, None
            point = (
                segment.origin.start
                + span.start
                - segment.generated.start
                if segment.relation is MappingRelation.EXACT
                else segment.origin.start
            )
            return segment.origin_ref, SourceSpan(point, point)
        index = max(0, bisect_right(self._starts, span.start) - 1)
        overlapping: list[SourceMapSegment] = []
        while index < len(self._segments):
            segment = self._segments[index]
            _observe_work("parent_segment_visit")
            if segment.generated.start >= span.end:
                break
            if span.start < segment.generated.end:
                overlapping.append(segment)
            index += 1
        return _anchor_from_segments(tuple(overlapping), span)

    def _overlapping_monotonic(
        self,
        span: SourceSpan,
    ) -> tuple[SourceMapSegment, ...]:
        while (
            self._cursor < len(self._segments)
            and self._segments[self._cursor].generated.end <= span.start
        ):
            _observe_work("parent_segment_visit")
            self._cursor += 1
        result: list[SourceMapSegment] = []
        scan = self._cursor
        while scan < len(self._segments):
            parent = self._segments[scan]
            _observe_work("parent_segment_visit")
            if parent.generated.start >= span.end:
                break
            if span.start < parent.generated.end:
                result.append(parent)
            scan += 1
        if not result and span.start < span.end:
            raise ValueError("source fragment has no visible mapping")
        return tuple(result)


def materialize_worker_reload_source(
    source: MappedSource,
    insertions: Iterable[ReloadInsertion],
    *,
    method_boundaries: Iterable[ReloadMethodBoundary] = (),
) -> CompactMappedWorkerSource:
    """Apply reload insertions without expanding unchanged source into segments."""

    ordered = _validated_reload_insertions(source, insertions)
    boundaries = _validated_method_boundaries(
        source.artifact.character_length,
        method_boundaries,
    )
    line_ending_starts, line_ending_widths = _line_ending_index(source.text)
    fragments: list[str] = []
    intervals: list[_CompactInsertionInterval] = []
    checkpoints: list[ReloadMapCheckpoint] = []
    projector = _ParentProjector(source.source_map)
    generated_cursor = 0
    source_cursor = 0
    index = 0
    while index < len(ordered):
        insertion = ordered[index]
        offset = insertion.source_offset
        if source_cursor < offset:
            normalized = _normalize_reload_fragment(source.text[source_cursor:offset])
            fragments.append(normalized)
            generated_cursor += len(normalized)
        group_start = generated_cursor
        inserted_length = 0
        while index < len(ordered) and ordered[index].source_offset == offset:
            insertion = ordered[index]
            generated = SourceSpan(
                generated_cursor,
                generated_cursor + insertion.generated_length,
            )
            fragments.append(insertion._text)
            if insertion._relation is _InsertionRelation.DERIVED:
                assert insertion._origin is not None
                origin_ref, origin = projector.anchor(insertion._origin)
                if origin_ref is None or origin is None:
                    raise ValueError("derived insertion has no visible origin")
                relation = MappingRelation.DERIVED
            else:
                origin_ref = None
                origin = None
                relation = MappingRelation.SYNTHETIC
            anchor_ref, anchor = projector.anchor(insertion._anchor)
            intervals.append(
                _CompactInsertionInterval(
                    generated,
                    offset,
                    insertion.method_name,
                    relation,
                    insertion.synthetic_region,
                    origin_ref,
                    origin,
                    anchor_ref,
                    anchor,
                    insertion._origin,
                    insertion._anchor,
                )
            )
            generated_cursor = generated.end
            inserted_length += insertion.generated_length
            index += 1
        checkpoints.append(
            ReloadMapCheckpoint(offset, group_start, inserted_length)
        )
        source_cursor = offset
    if source_cursor < len(source.text):
        normalized = _normalize_reload_fragment(source.text[source_cursor:])
        fragments.append(normalized)
        generated_cursor += len(normalized)

    text = "".join(fragments)
    if not text.strip():
        raise ValueError("Worker source is empty")
    digest = sha256(text.encode("utf-8")).hexdigest()
    artifact = SourceArtifactRef(
        SourceArtifactKind.WORKER_MODULE,
        digest,
        len(text),
        "lf" if "\n" in text else "none",
    )
    checkpoint_tuple = tuple(checkpoints)
    interval_tuple = tuple(intervals)
    basis = _compact_basis(
        source.artifact,
        source.source_map,
        len(source.text),
        interval_tuple,
        checkpoint_tuple,
        line_ending_starts,
        line_ending_widths,
        _reload_method_checkpoints(
            boundaries,
            checkpoint_tuple,
            line_ending_starts,
            line_ending_widths,
        ),
    )
    flattened = CompactReloadSourceMap(
        artifact,
        basis,
        flattened=True,
    )
    local = CompactReloadSourceMap(
        artifact,
        basis,
        flattened=False,
    )
    return CompactMappedWorkerSource(
        text,
        artifact,
        flattened,
        (*source.lineage, local),
        local,
        _normalized_worker_authority=_NORMALIZED_WORKER_AUTHORITY,
    )


def materialize_worker_reload_source_oracle(
    source: MappedSource,
    insertions: Iterable[ReloadInsertion],
) -> MappedSource:
    """Apply insertions and newline normalization directly to a WORKER_MODULE."""

    ordered = _validated_reload_insertions(source, insertions)

    fragments: list[str] = []
    local_segments: list[SourceMapSegment] = []
    flattened_segments: list[SourceMapSegment] = []
    projector = _ParentProjector(source.source_map)
    anchor_cache: dict[SourceSpan, tuple[SourceUnitRef | None, SourceSpan | None]] = {}
    generated_cursor = 0

    def mapped_anchor(
        span: SourceSpan,
    ) -> tuple[SourceUnitRef | None, SourceSpan | None]:
        mapped = anchor_cache.get(span)
        if mapped is None:
            mapped = projector.anchor(span)
            anchor_cache[span] = mapped
        return mapped

    def append_original(span: SourceSpan) -> None:
        nonlocal generated_cursor
        cursor = span.start
        while cursor < span.end:
            carriage = source.text.find("\r", cursor, span.end)
            exact_end = span.end if carriage < 0 else carriage
            if cursor < exact_end:
                text = source.text[cursor:exact_end]
                generated = SourceSpan(generated_cursor, generated_cursor + len(text))
                fragments.append(text)
                local_segments.append(
                    SourceMapSegment(
                        generated,
                        source.artifact,
                        SourceSpan(cursor, exact_end),
                        MappingRelation.EXACT,
                    )
                )
                flattened_segments.extend(
                    projector.exact(SourceSpan(cursor, exact_end), generated_cursor)
                )
                generated_cursor += len(text)
            if carriage < 0:
                break
            ending = (
                carriage + 2
                if carriage + 1 < span.end and source.text[carriage + 1] == "\n"
                else carriage + 1
            )
            generated = SourceSpan(generated_cursor, generated_cursor + 1)
            origin = SourceSpan(carriage, ending)
            fragments.append("\n")
            local_segments.append(
                SourceMapSegment(
                    generated,
                    source.artifact,
                    origin,
                    MappingRelation.DERIVED,
                    WORKER_LINE_ENDING_REGION,
                )
            )
            flattened_segments.append(
                projector.derived(
                    origin,
                    generated,
                    WORKER_LINE_ENDING_REGION,
                )
            )
            generated_cursor += 1
            cursor = ending

    def append_insertion(insertion: ReloadInsertion) -> None:
        nonlocal generated_cursor
        generated = SourceSpan(
            generated_cursor,
            generated_cursor + insertion.generated_length,
        )
        fragments.append(insertion._text)
        anchor_unit, anchor_span = mapped_anchor(insertion._anchor)
        if insertion._relation is _InsertionRelation.SYNTHETIC:
            local_segments.append(
                SourceMapSegment(
                    generated,
                    None,
                    None,
                    MappingRelation.SYNTHETIC,
                    insertion.synthetic_region,
                    source.artifact,
                    insertion._anchor,
                )
            )
            flattened_segments.append(
                SourceMapSegment(
                    generated,
                    None,
                    None,
                    MappingRelation.SYNTHETIC,
                    insertion.synthetic_region,
                    anchor_unit,
                    anchor_span,
                )
            )
        else:
            assert insertion._origin is not None
            origin_unit, origin_span = mapped_anchor(insertion._origin)
            if origin_unit is None or origin_span is None:
                raise ValueError("derived insertion has no visible origin")
            local_segments.append(
                SourceMapSegment(
                    generated,
                    source.artifact,
                    insertion._origin,
                    MappingRelation.DERIVED,
                    insertion.synthetic_region,
                    source.artifact,
                    insertion._anchor,
                )
            )
            flattened_segments.append(
                SourceMapSegment(
                    generated,
                    origin_unit,
                    origin_span,
                    MappingRelation.DERIVED,
                    insertion.synthetic_region,
                    anchor_unit,
                    anchor_span,
                )
            )
        generated_cursor = generated.end

    source_cursor = 0
    for insertion in ordered:
        if source_cursor < insertion.source_offset:
            append_original(SourceSpan(source_cursor, insertion.source_offset))
        append_insertion(insertion)
        source_cursor = insertion.source_offset
    if source_cursor < len(source.text):
        append_original(SourceSpan(source_cursor, len(source.text)))

    text = "".join(fragments)
    if not text.strip():
        raise ValueError("Worker source is empty")
    digest = sha256(text.encode("utf-8")).hexdigest()
    artifact = SourceArtifactRef(
        SourceArtifactKind.WORKER_MODULE,
        digest,
        len(text),
        "lf" if "\n" in text else "none",
    )
    local = SourceMap(artifact, tuple(local_segments))
    flattened = SourceMap(artifact, tuple(flattened_segments))
    return MappedSource(
        text,
        artifact,
        flattened,
        (*source.lineage, local),
        local,
        _normalized_worker_authority=_NORMALIZED_WORKER_AUTHORITY,
    )


def _validated_reload_insertions(
    source: MappedSource,
    insertions: Iterable[ReloadInsertion],
) -> tuple[ReloadInsertion, ...]:
    if not isinstance(source, MappedSource):
        raise ValueError("Worker reload source must be a MappedSource")
    try:
        supplied = tuple(insertions)
    except TypeError as error:
        raise ValueError("Worker reload insertions are invalid") from error
    if any(not isinstance(item, ReloadInsertion) for item in supplied):
        raise ValueError("Worker reload insertions are invalid")
    ordered = tuple(
        item
        for _, item in sorted(
            enumerate(supplied),
            key=lambda pair: (pair[1].source_offset, pair[0]),
        )
    )
    if any(item.source_offset > len(source.text) for item in ordered):
        raise ValueError("Worker reload insertion is outside source")
    if any(
        item.source_offset > 0
        and item.source_offset < len(source.text)
        and source.text[item.source_offset - 1 : item.source_offset + 1] == "\r\n"
        for item in ordered
    ):
        raise ValueError("Worker reload insertion splits a CRLF sequence")
    if "\x00" in source.text:
        raise ValueError("Worker source contains a NUL character")
    return ordered


def _normalize_reload_fragment(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _validated_method_boundaries(
    source_length: int,
    boundaries: Iterable[ReloadMethodBoundary],
) -> tuple[ReloadMethodBoundary, ...]:
    try:
        supplied = tuple(boundaries)
    except TypeError as error:
        raise ValueError("Worker reload method boundaries are invalid") from error
    if any(not isinstance(item, ReloadMethodBoundary) for item in supplied):
        raise ValueError("Worker reload method boundaries are invalid")
    ordered = tuple(
        sorted(supplied, key=lambda item: (item.declaration_start, item.method_name.casefold()))
    )
    names: set[str] = set()
    previous_end = 0
    for item in ordered:
        normalized = item.method_name.casefold()
        if (
            not item.method_name
            or normalized in names
            or not (
                0 <= item.declaration_start <= item.body_start <= item.method_end <= source_length
            )
            or item.declaration_start < previous_end
        ):
            raise ValueError("Worker reload method boundaries are invalid")
        names.add(normalized)
        previous_end = item.method_end
    return ordered


def _line_ending_index(source: str) -> tuple[tuple[int, ...], bytes]:
    starts: list[int] = []
    widths = bytearray()
    scan = 0
    while True:
        carriage = source.find("\r", scan)
        if carriage < 0:
            break
        width = 2 if source.startswith("\r\n", carriage) else 1
        starts.append(carriage)
        widths.append(width)
        scan = carriage + width
    return tuple(starts), bytes(widths)


def _compact_basis(
    parent_artifact: SourceArtifactRef,
    parent_source_map: SourceMap,
    source_length: int,
    intervals: tuple[_CompactInsertionInterval, ...],
    checkpoints: tuple[ReloadMapCheckpoint, ...],
    line_ending_starts: tuple[int, ...],
    line_ending_widths: bytes,
    method_checkpoints: tuple[ReloadMethodCheckpoint, ...],
) -> _CompactReloadBasis:
    cumulative = 0
    inserted_after: list[int] = []
    for item in checkpoints:
        cumulative += item.inserted_length
        inserted_after.append(cumulative)
    inserted_after_tuple = tuple(inserted_after)
    return _CompactReloadBasis(
        parent_artifact,
        parent_source_map,
        source_length,
        intervals,
        tuple(item.generated.start for item in intervals),
        checkpoints,
        tuple(item.source_offset for item in checkpoints),
        inserted_after_tuple,
        line_ending_starts,
        line_ending_widths,
        tuple(
            start + 1
            for start, width in zip(
                line_ending_starts,
                line_ending_widths,
                strict=True,
            )
            if width == 2
        ),
        method_checkpoints,
    )


def _reload_method_checkpoints(
    boundaries: tuple[ReloadMethodBoundary, ...],
    checkpoints: tuple[ReloadMapCheckpoint, ...],
    line_ending_starts: tuple[int, ...],
    line_ending_widths: bytes,
) -> tuple[ReloadMethodCheckpoint, ...]:
    checkpoint_offsets = tuple(item.source_offset for item in checkpoints)
    cumulative = 0
    inserted_after: list[int] = []
    for item in checkpoints:
        cumulative += item.inserted_length
        inserted_after.append(cumulative)
    inserted_after_tuple = tuple(inserted_after)
    crlf_lf_offsets = tuple(
        start + 1
        for start, width in zip(
            line_ending_starts,
            line_ending_widths,
            strict=True,
        )
        if width == 2
    )
    return tuple(
        ReloadMethodCheckpoint(
            item.method_name,
            item.declaration_start,
            _original_to_generated(
                item.declaration_start,
                checkpoint_offsets,
                inserted_after_tuple,
                crlf_lf_offsets,
            ),
            item.body_start,
            _original_to_generated(
                item.body_start,
                checkpoint_offsets,
                inserted_after_tuple,
                crlf_lf_offsets,
            ),
            item.method_end,
            _original_to_generated(
                item.method_end,
                checkpoint_offsets,
                inserted_after_tuple,
                crlf_lf_offsets,
            ),
        )
        for item in boundaries
    )


def _original_to_generated(
    source_offset: int,
    checkpoint_offsets: tuple[int, ...],
    inserted_after: tuple[int, ...],
    crlf_lf_offsets: tuple[int, ...],
) -> int:
    checkpoint_index = bisect_right(checkpoint_offsets, source_offset) - 1
    inserted = 0 if checkpoint_index < 0 else inserted_after[checkpoint_index]
    removed = bisect_right(crlf_lf_offsets, source_offset - 1)
    return source_offset - removed + inserted


def _validate_compact_basis(
    basis: _CompactReloadBasis,
    generated: SourceArtifactRef,
) -> None:
    if (
        not isinstance(basis.parent_artifact, SourceArtifactRef)
        or not isinstance(basis.parent_source_map, SourceMap)
        or basis.parent_source_map.generated != basis.parent_artifact
        or type(basis.source_length) is not int
        or basis.source_length != basis.parent_artifact.character_length
        or basis.interval_starts
        != tuple(item.generated.start for item in basis.intervals)
        or basis.checkpoint_offsets
        != tuple(item.source_offset for item in basis.checkpoints)
        or len(basis.line_ending_starts) != len(basis.line_ending_widths)
    ):
        raise ValueError("compact reload map basis is invalid")
    cumulative = 0
    expected_inserted_after: list[int] = []
    for item in basis.checkpoints:
        cumulative += item.inserted_length
        expected_inserted_after.append(cumulative)
    if basis.inserted_after != tuple(expected_inserted_after):
        raise ValueError("compact reload map basis is invalid")
    previous_end = -1
    for start, width in zip(
        basis.line_ending_starts,
        basis.line_ending_widths,
        strict=True,
    ):
        if width not in (1, 2) or start <= previous_end or start + width > basis.source_length:
            raise ValueError("compact reload line endings are invalid")
        previous_end = start + width - 1
    expected_crlf = tuple(
        start + 1
        for start, width in zip(
            basis.line_ending_starts,
            basis.line_ending_widths,
            strict=True,
        )
        if width == 2
    )
    if basis.crlf_lf_offsets != expected_crlf:
        raise ValueError("compact reload line endings are invalid")
    expected_length = basis.source_length - len(expected_crlf) + cumulative
    if expected_length != generated.character_length:
        raise ValueError("compact reload generated length is invalid")
    if any(
        item.generated.end > generated.character_length
        or item.source_offset > basis.source_length
        or item.source_anchor.end > basis.source_length
        or (
            item.source_origin is not None
            and item.source_origin.end > basis.source_length
        )
        for item in basis.intervals
    ):
        raise ValueError("compact reload interval is invalid")
    names: set[str] = set()
    for item in basis.method_checkpoints:
        normalized = item.method_name.casefold()
        if (
            not item.method_name
            or normalized in names
            or not (
                0
                <= item.declaration_source_offset
                <= item.body_source_offset
                <= item.end_source_offset
                <= basis.source_length
            )
            or item.declaration_generated_offset
            != _original_to_generated(
                item.declaration_source_offset,
                basis.checkpoint_offsets,
                basis.inserted_after,
                basis.crlf_lf_offsets,
            )
            or item.body_generated_offset
            != _original_to_generated(
                item.body_source_offset,
                basis.checkpoint_offsets,
                basis.inserted_after,
                basis.crlf_lf_offsets,
            )
            or item.end_generated_offset
            != _original_to_generated(
                item.end_source_offset,
                basis.checkpoint_offsets,
                basis.inserted_after,
                basis.crlf_lf_offsets,
            )
        ):
            raise ValueError("compact reload method checkpoints are invalid")
        names.add(normalized)


def _project_exact_piece(
    parent: SourceMapSegment,
    parent_piece: SourceSpan,
    generated: SourceSpan,
) -> SourceMapSegment:
    if parent.relation is MappingRelation.SYNTHETIC:
        return SourceMapSegment(
            generated,
            None,
            None,
            MappingRelation.SYNTHETIC,
            parent.synthetic_region,
            parent.anchor_ref,
            parent.anchor_span,
        )
    assert parent.origin_ref is not None and parent.origin is not None
    if parent.relation is MappingRelation.EXACT:
        origin = SourceSpan(
            parent.origin.start + parent_piece.start - parent.generated.start,
            parent.origin.start + parent_piece.end - parent.generated.start,
        )
    else:
        origin = parent.origin
    return SourceMapSegment(
        generated,
        parent.origin_ref,
        origin,
        parent.relation,
        parent.synthetic_region,
        parent.anchor_ref,
        parent.anchor_span,
    )


def _anchor_from_segments(
    segments: tuple[SourceMapSegment, ...],
    span: SourceSpan,
) -> tuple[SourceUnitRef | None, SourceSpan | None]:
    candidates: list[tuple[SourceUnitRef, SourceSpan]] = []
    for segment in segments:
        start = max(segment.generated.start, span.start)
        end = min(segment.generated.end, span.end)
        if start < end:
            if (
                segment.relation is MappingRelation.EXACT
                and isinstance(segment.origin_ref, SourceUnitRef)
                and segment.origin is not None
            ):
                candidates.append(
                    (
                        segment.origin_ref,
                        SourceSpan(
                            segment.origin.start + start - segment.generated.start,
                            segment.origin.start + end - segment.generated.start,
                        ),
                    )
                )
            elif (
                segment.relation is MappingRelation.DERIVED
                and isinstance(segment.origin_ref, SourceUnitRef)
                and segment.origin is not None
            ):
                candidates.append((segment.origin_ref, segment.origin))
            elif (
                isinstance(segment.anchor_ref, SourceUnitRef)
                and segment.anchor_span is not None
            ):
                candidates.append((segment.anchor_ref, segment.anchor_span))
    if not candidates or any(unit != candidates[0][0] for unit, _ in candidates[1:]):
        return None, None
    return candidates[0][0], SourceSpan(
        min(candidate.start for _, candidate in candidates),
        max(candidate.end for _, candidate in candidates),
    )


def _mapped_compact_interval(interval: _CompactInsertionInterval) -> MappedOffset:
    if interval.relation is MappingRelation.SYNTHETIC:
        return MappedOffset(
            MappingRelation.SYNTHETIC,
            synthetic_region=interval.synthetic_region,
            anchor_unit=interval.anchor_ref,
            anchor_span=interval.anchor_span,
        )
    return MappedOffset(
        MappingRelation.DERIVED,
        interval.origin_ref,
        interval.origin,
        anchor_unit=interval.anchor_ref,
        anchor_span=interval.anchor_span,
    )


def _mapped_compact_segment(segment: SourceMapSegment) -> MappedOffset:
    if segment.relation is MappingRelation.SYNTHETIC:
        return MappedOffset(
            MappingRelation.SYNTHETIC,
            synthetic_region=segment.synthetic_region,
            anchor_unit=(
                segment.anchor_ref
                if isinstance(segment.anchor_ref, SourceUnitRef)
                else None
            ),
            anchor_span=segment.anchor_span,
        )
    return MappedOffset(
        segment.relation,
        segment.origin_ref if isinstance(segment.origin_ref, SourceUnitRef) else None,
        segment.origin,
        anchor_unit=(
            segment.anchor_ref if isinstance(segment.anchor_ref, SourceUnitRef) else None
        ),
        anchor_span=segment.anchor_span,
    )


def _materialize_compact_generic(source_map: CompactReloadSourceMap) -> SourceMap:
    basis = source_map._compact_basis
    local_segments: list[SourceMapSegment] = []
    flattened_segments: list[SourceMapSegment] = []
    projector = _ParentProjector(basis.parent_source_map)
    generated_cursor = 0
    source_cursor = 0

    def append_original(end: int) -> None:
        nonlocal generated_cursor, source_cursor
        ending_index = bisect_right(basis.line_ending_starts, source_cursor - 1)
        while (
            ending_index < len(basis.line_ending_starts)
            and basis.line_ending_starts[ending_index] < end
        ):
            ending_start = basis.line_ending_starts[ending_index]
            if source_cursor < ending_start:
                generated = SourceSpan(
                    generated_cursor,
                    generated_cursor + ending_start - source_cursor,
                )
                original = SourceSpan(source_cursor, ending_start)
                local_segments.append(
                    SourceMapSegment(
                        generated,
                        basis.parent_artifact,
                        original,
                        MappingRelation.EXACT,
                    )
                )
                flattened_segments.extend(projector.exact(original, generated_cursor))
                generated_cursor = generated.end
            width = basis.line_ending_widths[ending_index]
            ending_end = ending_start + width
            generated = SourceSpan(generated_cursor, generated_cursor + 1)
            original = SourceSpan(ending_start, ending_end)
            local_segments.append(
                SourceMapSegment(
                    generated,
                    basis.parent_artifact,
                    original,
                    MappingRelation.DERIVED,
                    WORKER_LINE_ENDING_REGION,
                )
            )
            flattened_segments.append(
                projector.derived(original, generated, WORKER_LINE_ENDING_REGION)
            )
            generated_cursor = generated.end
            source_cursor = ending_end
            ending_index += 1
        if source_cursor < end:
            generated = SourceSpan(
                generated_cursor,
                generated_cursor + end - source_cursor,
            )
            original = SourceSpan(source_cursor, end)
            local_segments.append(
                SourceMapSegment(
                    generated,
                    basis.parent_artifact,
                    original,
                    MappingRelation.EXACT,
                )
            )
            flattened_segments.extend(projector.exact(original, generated_cursor))
            generated_cursor = generated.end
            source_cursor = end

    for interval in basis.intervals:
        if source_cursor < interval.source_offset:
            append_original(interval.source_offset)
        if generated_cursor != interval.generated.start:
            raise ValueError("compact reload interval ordering is invalid")
        if interval.relation is MappingRelation.SYNTHETIC:
            local_segments.append(
                SourceMapSegment(
                    interval.generated,
                    None,
                    None,
                    MappingRelation.SYNTHETIC,
                    interval.synthetic_region,
                    basis.parent_artifact,
                    interval.source_anchor,
                )
            )
        else:
            if interval.source_origin is None:
                raise ValueError("compact reload derived interval is invalid")
            local_segments.append(
                SourceMapSegment(
                    interval.generated,
                    basis.parent_artifact,
                    interval.source_origin,
                    MappingRelation.DERIVED,
                    interval.synthetic_region,
                    basis.parent_artifact,
                    interval.source_anchor,
                )
            )
        flattened_segments.append(
            SourceMapSegment(
                interval.generated,
                interval.origin_ref,
                interval.origin,
                interval.relation,
                interval.synthetic_region,
                interval.anchor_ref,
                interval.anchor_span,
            )
        )
        generated_cursor = interval.generated.end
        source_cursor = interval.source_offset
    if source_cursor < basis.source_length:
        append_original(basis.source_length)
    segments = flattened_segments if source_map._compact_flattened else local_segments
    return SourceMap(source_map.generated, tuple(segments))


def _artifact_manifest(artifact: SourceArtifactRef) -> list[object]:
    return [
        artifact.kind.value,
        artifact.source_sha256,
        artifact.character_length,
        artifact.line_ending_kind,
        artifact.lowering_semantic_version,
        artifact.wrapper_semantic_version,
        artifact.mode,
        artifact.worker_generation,
        artifact.worker_manifest_sha256,
        artifact.export_catalog_sha256,
    ]


def _indexed_span(
    unit: SourceUnitRef | None,
    span: SourceSpan | None,
    unit_indices: dict[SourceUnitRef, int],
) -> tuple[int | None, int | None, int | None]:
    if unit is None or span is None:
        return None, None, None
    return unit_indices[unit], span.start, span.end


def _plain_span(span: SourceSpan | None) -> tuple[int | None, int | None]:
    if span is None:
        return None, None
    return span.start, span.end


def _encoded_line_endings(starts: tuple[int, ...], widths: bytes) -> list[int]:
    previous = 0
    result: list[int] = []
    for start, width in zip(starts, widths, strict=True):
        result.append(((start - previous) << 1) | (width - 1))
        previous = start
    return result


__all__ = [
    "CompactMappedWorkerSource",
    "CompactReloadSourceMap",
    "ReloadInsertion",
    "ReloadMapCheckpoint",
    "ReloadMethodBoundary",
    "ReloadMethodCheckpoint",
    "WORKER_LINE_ENDING_REGION",
    "materialize_worker_reload_source",
    "materialize_worker_reload_source_oracle",
    "snapshot_compact_mapped_worker_source",
]
