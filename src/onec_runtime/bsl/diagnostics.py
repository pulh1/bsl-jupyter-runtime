"""Bounded, typed normalization for deterministic and 1C BSL diagnostics."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field as dataclass_field, replace
from enum import StrEnum
from hashlib import sha256
import re

from onec_runtime.bsl.source_maps import (
    LineIndex,
    MappedSource,
    MappingRelation,
    SourceMapSegment,
    SourceSpan,
    SourceUnitRef,
    source_sha256,
)


_PLATFORM_DIAGNOSTIC_LIMIT_BYTES = 64 * 1024
_PLATFORM_FRAME_LIMIT = 128
_PLATFORM_CAUSE_LIMIT = 32
_PLATFORM_CATEGORY_NAME_LIMIT = 16
_PLATFORM_CATEGORY_LABEL_LIMIT = 128
_PLATFORM_COORDINATE_LIMIT = 10_000_000
_MODULE_LOCATOR_LIMIT = 512
_MODULE_COMPONENT_LIMIT = 32
_MODULE_IDENTIFIER = r"[A-Za-zА-Яа-яЁё_][0-9A-Za-zА-Яа-яЁё_]*"
_UNKNOWN_MODULE_PATTERN = (
    r"<Неизвестный модуль>|Неизвестный модуль|<Unknown module>|Unknown module"
)
_LOCATION_RE = re.compile(
    rf"^\{{(?:(?P<unknown>{_UNKNOWN_MODULE_PATTERN})|"
    rf"(?:(?P<extension>{_MODULE_IDENTIFIER})[ \t]+)?"
    rf"(?P<module>{_MODULE_IDENTIFIER}(?:\.{_MODULE_IDENTIFIER})*))?"
    r"\((?P<line>[0-9]{1,10})"
    r"(?:\s*,\s*(?P<column>[0-9]{1,10}))?\)\}",
    re.MULTILINE,
)
_WORKER_REGISTRATION_RE = re.compile(
    r"OnecRuntime_[0-9a-f]{8}_[0-9a-f]{16}\Z",
    re.IGNORECASE,
)
_COMPILATION_MARKER_RE = re.compile(
    r"(?:[ \t]+|\r?\n[ \t]*)"
    r"\[ОшибкаКомпиляцииВстроенногоЯзыка\]"
    r"[ \t]*(?:\r?\n)?\Z",
)
_NESTED_COMPILE_CAUSE_PREFIX_RE = re.compile(
    r"(?:^|\r?\n)[ \t]*(?:по причине:|Reason:)[ \t]*\r?\n\Z",
    re.IGNORECASE,
)
_CAUSE_BOUNDARY_RE = re.compile(
    r"^[ \t]*(?:по причине:|Reason:)[ \t]*(?:\r?\n|\Z)",
    re.IGNORECASE | re.MULTILINE,
)
_CATEGORY_BLOCK_RE = re.compile(
    rf"^[ \t]*\[(?P<names>{_MODULE_IDENTIFIER}"
    rf"(?:[ \t]*,[ \t]*{_MODULE_IDENTIFIER})*)\][ \t]*(?:\r?\n|\Z)",
    re.MULTILINE,
)
_DIAGNOSTIC_BLOCK_START_RE = re.compile(r"^\{", re.MULTILINE)
_UNKNOWN_MODULES = frozenset(
    {
        "<Неизвестный модуль>",
        "Неизвестный модуль",
        "<Unknown module>",
        "Unknown module",
    }
)


class DiagnosticStage(StrEnum):
    PARSING = "parsing"
    LOWERING = "lowering"
    COMPILATION = "compilation"
    EXECUTION = "execution"


class DiagnosticCoordinateSpace(StrEnum):
    UNKNOWN = "unknown"
    EXECUTED_BSL = "executed_bsl"
    HOST_MODULE = "host_module"


class MappingConfidence(StrEnum):
    EXACT = "exact"
    NEAREST = "nearest"
    SYNTHETIC = "synthetic"
    UNKNOWN = "unknown"


class _PrivatePlatformEvidence:
    """Bounded expert prose that generic serializers cannot traverse."""

    __slots__ = ("_text",)

    def __init__(self, text: str) -> None:
        self._text = text

    @property
    def text(self) -> str:
        return self._text

    def __repr__(self) -> str:
        return "<redacted platform diagnostic>"

    def __deepcopy__(self, memo: dict[int, object]) -> object:
        return _REDACTED_PLATFORM_EVIDENCE


class _RedactedPlatformEvidence:
    __slots__ = ()

    def __repr__(self) -> str:
        return "<redacted platform diagnostic>"


_REDACTED_PLATFORM_EVIDENCE = _RedactedPlatformEvidence()


@dataclass(frozen=True, slots=True)
class DiagnosticTextSpan:
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class DiagnosticCategoryBlock:
    span: DiagnosticTextSpan
    names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ParsedDiagnosticCause:
    ordinal: int
    summary_span: DiagnosticTextSpan
    block_span: DiagnosticTextSpan
    frame_ordinals: tuple[int, ...]
    category: DiagnosticCategoryBlock | None = None


@dataclass(frozen=True, slots=True)
class ParsedDiagnosticFrame:
    ordinal: int
    cause_ordinal: int | None
    location: PlatformDiagnosticLocation
    block_span: DiagnosticTextSpan
    detail_span: DiagnosticTextSpan | None


@dataclass(frozen=True, slots=True)
class ParsedPlatformDiagnostic:
    _platform_evidence: _PrivatePlatformEvidence = dataclass_field(repr=False)
    platform_diagnostic_sha256: str
    platform_diagnostic_truncated: bool
    platform_diagnostic_redacted: bool
    line: int | None
    column: int | None
    coordinate_space: DiagnosticCoordinateSpace
    module_name: str | None
    additional_locations: tuple[tuple[int, int], ...]
    has_compilation_marker: bool
    locations: tuple[PlatformDiagnosticLocation, ...]
    causes: tuple[ParsedDiagnosticCause, ...] = ()
    frames: tuple[ParsedDiagnosticFrame, ...] = ()
    opaque_spans: tuple[DiagnosticTextSpan, ...] = ()
    frames_truncated: bool = False
    causes_truncated: bool = False

    @property
    def platform_diagnostic(self) -> str:
        return self._platform_evidence.text


@dataclass(frozen=True, slots=True)
class VisibleSourceLocation:
    source_unit: SourceUnitRef
    line: int
    column: int
    span: SourceSpan


class VisibleSourceContext:
    """Hash-fenced visible line indexes that retain no source text."""

    __slots__ = ("_indices",)

    def __init__(self, sources: Mapping[SourceUnitRef, str]) -> None:
        if not isinstance(sources, Mapping):
            raise ValueError("visible sources must be a mapping")
        indices: dict[
            tuple[object, str, int],
            tuple[str, LineIndex],
        ] = {}
        for unit, source in sources.items():
            if not isinstance(unit, SourceUnitRef) or type(source) is not str:
                raise ValueError("visible sources require SourceUnitRef and string pairs")
            indices[_visible_unit_key(unit)] = (
                source_sha256(source),
                LineIndex(source),
            )
        self._indices = indices

    def line_column(
        self,
        unit: SourceUnitRef,
        offset: int,
    ) -> tuple[int, int] | None:
        entry = self._indices.get(_visible_unit_key(unit))
        if entry is None or entry[0] != unit.source_sha256:
            return None
        try:
            return entry[1].offset_to_line_column(offset)
        except ValueError:
            return None

    def line_range(
        self,
        unit: SourceUnitRef,
        line: int,
    ) -> SourceSpan | None:
        entry = self._indices.get(_visible_unit_key(unit))
        if entry is None or entry[0] != unit.source_sha256:
            return None
        try:
            return entry[1].line_range(line)
        except ValueError:
            return None

    def __repr__(self) -> str:
        return f"VisibleSourceContext(entries={len(self._indices)}, source=<redacted>)"


@dataclass(frozen=True, slots=True)
class WorkerArtifactPlatformLocation:
    registration_name: str


@dataclass(frozen=True, slots=True)
class PlatformDiagnosticLocation:
    module_name: str | None
    module_components: tuple[str, ...]
    worker_artifact_location: WorkerArtifactPlatformLocation | None
    line: int
    column: int | None
    coordinate_space: DiagnosticCoordinateSpace
    extension_name: str | None = None


@dataclass(frozen=True, slots=True)
class _AcceptedPlatformLocation:
    start: int
    end: int
    location: PlatformDiagnosticLocation


@dataclass(frozen=True, slots=True, repr=False)
class WorkerDiagnosticArtifact:
    """Private, manifest-fenced source-map input for Worker diagnostics."""

    logical_name: str
    revision: int
    artifact_sha256: str
    registration_name: str
    manifest_sha256: str
    source_map_sha256: str
    mapped_source: MappedSource = dataclass_field(repr=False)
    visible_source_context: VisibleSourceContext | None = dataclass_field(
        default=None,
        repr=False,
    )

    def __post_init__(self) -> None:
        if (
            not isinstance(self.logical_name, str)
            or not self.logical_name
            or type(self.revision) is not int
            or self.revision < 0
            or re.fullmatch(r"[0-9a-f]{64}", self.artifact_sha256) is None
            or not isinstance(self.registration_name, str)
            or not self.registration_name
            or re.fullmatch(r"[0-9a-f]{64}", self.manifest_sha256) is None
            or re.fullmatch(r"[0-9a-f]{64}", self.source_map_sha256) is None
            or not isinstance(self.mapped_source, MappedSource)
            or self.source_map_sha256 != self.mapped_source.source_map_sha256
            or (
                self.visible_source_context is not None
                and not isinstance(self.visible_source_context, VisibleSourceContext)
            )
        ):
            raise ValueError("worker diagnostic artifact source map identity is invalid")

    def __repr__(self) -> str:
        return (
            "WorkerDiagnosticArtifact("
            f"logical_name={self.logical_name!r}, revision={self.revision}, "
            f"artifact_sha256={self.artifact_sha256!r}, source=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class WorkerRuntimeFrameDiagnostic:
    registration_name: str
    logical_name: str | None
    revision: int | None
    artifact_sha256: str | None
    mapping_confidence: MappingConfidence
    source_unit: SourceUnitRef | None = None
    visible_location: VisibleSourceLocation | None = None
    related_visible_span: SourceSpan | None = None
    lowered_location: LoweredSourceLocation | None = None
    synthetic_region: str | None = None
    dependency_anchor: SourceSpan | None = None
    method_anchor: SourceSpan | None = None


@dataclass(frozen=True, slots=True)
class LoweredSourceLocation:
    line: int
    column: int
    offset: int
    span: SourceSpan


class ErrorTraceFrameOrigin(StrEnum):
    EXECUTED_ARTIFACT = "executed_artifact"
    WORKER_ARTIFACT = "worker_artifact"
    NATIVE_MODULE = "native_module"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ErrorTraceCause:
    ordinal: int
    summary_span: DiagnosticTextSpan
    block_span: DiagnosticTextSpan
    frame_ordinals: tuple[int, ...]
    category: DiagnosticCategoryBlock | None = None


@dataclass(frozen=True, slots=True)
class ErrorTraceFrame:
    ordinal: int
    cause_ordinal: int | None
    origin: ErrorTraceFrameOrigin
    platform_location: PlatformDiagnosticLocation
    block_span: DiagnosticTextSpan
    detail_span: DiagnosticTextSpan | None
    mapping_confidence: MappingConfidence
    registration_name: str | None = None
    logical_name: str | None = None
    revision: int | None = None
    artifact_sha256: str | None = None
    source_unit: SourceUnitRef | None = None
    visible_location: VisibleSourceLocation | None = None
    visible_line_span: SourceSpan | None = None
    related_visible_span: SourceSpan | None = None
    lowered_location: LoweredSourceLocation | None = None
    synthetic_region: str | None = None
    dependency_anchor: SourceSpan | None = None
    method_anchor: SourceSpan | None = None


@dataclass(frozen=True, slots=True)
class NormalizedDiagnostic:
    diagnostic_id: str
    runtime_summary: str
    stage: DiagnosticStage
    mapping_confidence: MappingConfidence
    code: str | None = None
    source_unit: SourceUnitRef | None = None
    visible_location: VisibleSourceLocation | None = None
    related_visible_span: SourceSpan | None = None
    lowered_location: LoweredSourceLocation | None = None
    synthetic_region: str | None = None
    _platform_evidence: _PrivatePlatformEvidence | None = dataclass_field(
        default=None,
        repr=False,
    )
    platform_diagnostic_sha256: str | None = None
    platform_diagnostic_truncated: bool = False
    platform_diagnostic_redacted: bool = False
    execution_artifact_sha256: str | None = None
    source_map_sha256: str | None = None
    dependency_anchor: SourceSpan | None = None
    method_anchor: SourceSpan | None = None
    worker_frames: tuple[WorkerRuntimeFrameDiagnostic, ...] = ()
    causes: tuple[ErrorTraceCause, ...] = ()
    frames: tuple[ErrorTraceFrame, ...] = ()
    frames_truncated: bool = False
    causes_truncated: bool = False

    @property
    def platform_diagnostic(self) -> str | None:
        return (
            self._platform_evidence.text
            if self._platform_evidence is not None
            else None
        )


class PlatformCoordinateCodec:
    """Decode one-based 1C coordinates against one exact artifact string."""

    def __init__(self, executed_source: str) -> None:
        self._index = LineIndex(executed_source)

    def to_offset(self, line: int, column: int) -> int | None:
        try:
            return self._index.line_column_to_offset(line, column)
        except ValueError:
            return None


def _bound_platform_diagnostic(message: str) -> tuple[str, bool]:
    encoded = message.encode("utf-8")
    if len(encoded) <= _PLATFORM_DIAGNOSTIC_LIMIT_BYTES:
        return message, False
    return (
        encoded[:_PLATFORM_DIAGNOSTIC_LIMIT_BYTES].decode("utf-8", errors="ignore"),
        True,
    )


def _accepted_platform_locations(text: str) -> tuple[_AcceptedPlatformLocation, ...]:
    accepted: list[_AcceptedPlatformLocation] = []
    for match in _LOCATION_RE.finditer(text):
        unknown = match.group("unknown")
        module = unknown or match.group("module")
        extension_name = match.group("extension")
        components = (
            ()
            if module is None
            else ((module,) if module in _UNKNOWN_MODULES else tuple(module.split(".")))
        )
        line = int(match.group("line"))
        column_text = match.group("column")
        column = None if column_text is None else int(column_text)
        if (
            (module is not None and len(module) > _MODULE_LOCATOR_LIMIT)
            or (
                extension_name is not None
                and len(extension_name) > _MODULE_LOCATOR_LIMIT
            )
            or len(components) > _MODULE_COMPONENT_LIMIT
            or (column is None and line <= 0)
        ):
            continue
        worker_location = (
            None
            if extension_name is not None
            else _parse_worker_artifact_location(components)
        )
        accepted.append(
            _AcceptedPlatformLocation(
                match.start(),
                match.end(),
                PlatformDiagnosticLocation(
                    module,
                    components,
                    worker_location,
                    line,
                    column,
                    (
                        DiagnosticCoordinateSpace.EXECUTED_BSL
                        if module in _UNKNOWN_MODULES
                        else (
                            DiagnosticCoordinateSpace.UNKNOWN
                            if module is None
                            else DiagnosticCoordinateSpace.HOST_MODULE
                        )
                    ),
                    extension_name,
                ),
            )
        )
    return tuple(accepted)


def _trim_diagnostic_span(text: str, start: int, end: int) -> DiagnosticTextSpan:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return DiagnosticTextSpan(start, end)


def _frame_detail_span(
    text: str,
    locator_end: int,
    block_end: int,
) -> DiagnosticTextSpan | None:
    start = locator_end
    if start < block_end and text[start] == ":":
        start += 1
    span = _trim_diagnostic_span(text, start, block_end)
    return None if span.start == span.end else span


def _complement_spans(
    text_length: int,
    consumed: tuple[DiagnosticTextSpan, ...],
) -> tuple[DiagnosticTextSpan, ...]:
    merged: list[DiagnosticTextSpan] = []
    for span in sorted(consumed, key=lambda item: (item.start, item.end)):
        if span.start == span.end:
            continue
        if merged and span.start <= merged[-1].end:
            merged[-1] = DiagnosticTextSpan(
                merged[-1].start,
                max(merged[-1].end, span.end),
            )
        else:
            merged.append(span)
    opaque: list[DiagnosticTextSpan] = []
    cursor = 0
    for span in merged:
        if cursor < span.start:
            opaque.append(DiagnosticTextSpan(cursor, span.start))
        cursor = max(cursor, span.end)
    if cursor < text_length:
        opaque.append(DiagnosticTextSpan(cursor, text_length))
    return tuple(opaque)


def _trace_location_is_bounded(location: PlatformDiagnosticLocation) -> bool:
    return (
        0 <= location.line <= _PLATFORM_COORDINATE_LIMIT
        and (
            location.column is None
            or 0 <= location.column <= _PLATFORM_COORDINATE_LIMIT
        )
    )


def _parse_diagnostic_structure(
    text: str,
    accepted: tuple[_AcceptedPlatformLocation, ...],
) -> tuple[
    tuple[ParsedDiagnosticCause, ...],
    tuple[ParsedDiagnosticFrame, ...],
    tuple[DiagnosticTextSpan, ...],
    bool,
    bool,
]:
    markers = tuple(_CAUSE_BOUNDARY_RE.finditer(text))
    category_matches = tuple(_CATEGORY_BLOCK_RE.finditer(text))
    if not text:
        cause_blocks: tuple[DiagnosticTextSpan, ...] = ()
    else:
        starts = (0, *(match.end() for match in markers))
        ends = (*(match.start() for match in markers), len(text))
        cause_blocks = tuple(
            DiagnosticTextSpan(start, end)
            for start, end in zip(starts, ends, strict=True)
        )
    retained_cause_blocks = cause_blocks[:_PLATFORM_CAUSE_LIMIT]
    candidates = tuple(
        item for item in accepted if _trace_location_is_bounded(item.location)
    )
    retained_candidates = candidates[:_PLATFORM_FRAME_LIMIT]
    diagnostic_block_starts = tuple(
        sorted(
            (
                *(match.start() for match in _DIAGNOSTIC_BLOCK_START_RE.finditer(text)),
                *(match.start() for match in category_matches),
            )
        )
    )
    frames: list[ParsedDiagnosticFrame] = []
    for ordinal, item in enumerate(retained_candidates):
        cause_index = next(
            (
                index
                for index, block in enumerate(cause_blocks)
                if block.start <= item.start < block.end
            ),
            None,
        )
        cause_end = len(text) if cause_index is None else cause_blocks[cause_index].end
        next_block_start = next(
            (start for start in diagnostic_block_starts if start > item.start),
            cause_end,
        )
        block_end = min(next_block_start, cause_end)
        frames.append(
            ParsedDiagnosticFrame(
                ordinal,
                (
                    cause_index
                    if cause_index is not None
                    and cause_index < len(retained_cause_blocks)
                    else None
                ),
                item.location,
                DiagnosticTextSpan(item.start, block_end),
                _frame_detail_span(text, item.end, block_end),
            )
        )
    causes: list[ParsedDiagnosticCause] = []
    for ordinal, block in enumerate(retained_cause_blocks):
        first_locator = next(
            (item.start for item in accepted if block.start <= item.start < block.end),
            block.end,
        )
        cause_category_matches = tuple(
            match
            for match in category_matches
            if block.start <= match.start() < block.end
        )
        category = None
        if len(cause_category_matches) == 1:
            category_match = cause_category_matches[0]
            category_names = tuple(
                item.strip() for item in category_match.group("names").split(",")
            )
            if (
                len(category_names) <= _PLATFORM_CATEGORY_NAME_LIMIT
                and all(
                    len(item) <= _PLATFORM_CATEGORY_LABEL_LIMIT
                    for item in category_names
                )
            ):
                category = DiagnosticCategoryBlock(
                    DiagnosticTextSpan(category_match.start(), category_match.end()),
                    category_names,
                )
        summary_boundaries = (
            first_locator,
            *(match.start() for match in cause_category_matches),
        )
        summary_end = min(summary_boundaries)
        causes.append(
            ParsedDiagnosticCause(
                ordinal,
                _trim_diagnostic_span(text, block.start, summary_end),
                block,
                tuple(
                    frame.ordinal for frame in frames if frame.cause_ordinal == ordinal
                ),
                category,
            )
        )
    consumed = (
        *(DiagnosticTextSpan(match.start(), match.end()) for match in markers),
        *(cause.summary_span for cause in causes),
        *(frame.block_span for frame in frames),
        *(cause.category.span for cause in causes if cause.category is not None),
    )
    return (
        tuple(causes),
        tuple(frames),
        _complement_spans(len(text), tuple(consumed)),
        len(candidates) > _PLATFORM_FRAME_LIMIT,
        len(cause_blocks) > _PLATFORM_CAUSE_LIMIT,
    )


def parse_platform_diagnostic(message: str) -> ParsedPlatformDiagnostic:
    """Structurally parse allowlisted 1C locations from bounded diagnostic text."""
    if type(message) is not str:
        raise ValueError("platform diagnostic must be a string")
    bounded, text_truncated = _bound_platform_diagnostic(message)
    digest = sha256(message.encode("utf-8")).hexdigest()
    accepted = _accepted_platform_locations(bounded)
    (
        causes,
        frames,
        opaque_spans,
        frames_truncated,
        causes_truncated,
    ) = _parse_diagnostic_structure(bounded, accepted)
    column_locations = tuple(
        item for item in accepted if item.location.column is not None
    )
    terminal_compilation = _COMPILATION_MARKER_RE.search(bounded) is not None
    primary = (
        column_locations[0]
        if column_locations and column_locations[0].start == 0
        else None
    )
    if (
        primary is None
        and terminal_compilation
        and len(column_locations) == 1
        and (
            column_locations[0].location.coordinate_space
            is DiagnosticCoordinateSpace.EXECUTED_BSL
            or column_locations[0].location.worker_artifact_location is not None
        )
        and _NESTED_COMPILE_CAUSE_PREFIX_RE.search(
            bounded[: column_locations[0].start]
        )
        is not None
    ):
        primary = column_locations[0]
    if primary is None:
        module_name = None
        line = None
        column = None
        space = DiagnosticCoordinateSpace.UNKNOWN
        additional: tuple[tuple[int, int], ...] = ()
    else:
        module_name = primary.location.module_name
        line = primary.location.line
        column = primary.location.column
        space = primary.location.coordinate_space
        additional = tuple(
            (item.location.line, item.location.column)
            for item in column_locations[1:]
        )
    return ParsedPlatformDiagnostic(
        _PrivatePlatformEvidence(bounded),
        digest,
        text_truncated,
        False,
        line,
        column,
        space,
        module_name,
        additional,
        primary is not None and terminal_compilation,
        tuple(item.location for item in accepted[:_PLATFORM_FRAME_LIMIT]),
        causes=causes,
        frames=frames,
        opaque_spans=opaque_spans,
        frames_truncated=frames_truncated,
        causes_truncated=causes_truncated,
    )


def remap_worker_stage_diagnostic(
    parsed: ParsedPlatformDiagnostic,
    *,
    artifact_sha256: str,
    phase: str,
    candidate_manifest_sha256: str,
    candidate_artifacts: tuple[WorkerDiagnosticArtifact, ...],
) -> NormalizedDiagnostic:
    """Map a stage failure only through one exact candidate artifact."""
    _validate_worker_diagnostic_request(
        parsed,
        candidate_manifest_sha256,
        candidate_artifacts,
    )
    if (
        re.fullmatch(r"[0-9a-f]{64}", artifact_sha256) is None
        or phase not in {"decode", "upload", "connect", "registration-result", "create"}
    ):
        raise ValueError("worker artifact stage identity is invalid")
    stage = (
        DiagnosticStage.EXECUTION
        if phase == "create" and not parsed.has_compilation_marker
        else DiagnosticStage.COMPILATION
    )
    matches = tuple(
        artifact
        for artifact in candidate_artifacts
        if artifact.manifest_sha256 == candidate_manifest_sha256
        and artifact.artifact_sha256 == artifact_sha256
    )
    if len(matches) != 1:
        return _unmapped_worker_diagnostic(
            parsed,
            stage=stage,
            code="worker_artifact_identity_unmapped",
            identity=(candidate_manifest_sha256, artifact_sha256, phase),
        )
    artifact = matches[0]
    locations = _worker_stage_locations(parsed, artifact)
    if not locations:
        return _unmapped_worker_diagnostic(
            parsed,
            stage=stage,
            code="worker_artifact_location_unmapped",
            identity=(candidate_manifest_sha256, artifact_sha256, phase),
        )
    if len(locations) != 1:
        return _unmapped_worker_diagnostic(
            parsed,
            stage=stage,
            code="worker_artifact_location_ambiguous",
            identity=(candidate_manifest_sha256, artifact_sha256, phase),
        )
    location = locations[0]
    diagnostic = remap_platform_diagnostic(
        replace(
            parsed,
            line=location.line,
            column=location.column,
            coordinate_space=DiagnosticCoordinateSpace.EXECUTED_BSL,
            module_name=location.module_name,
        ),
        artifact.mapped_source,
        stage=stage,
        visible_source_context=artifact.visible_source_context,
    )
    frames, worker_frames = _normalize_trace_frames(
        parsed,
        executed=artifact.mapped_source,
        visible_source_context=artifact.visible_source_context,
        pinned_manifest_sha256=candidate_manifest_sha256,
        pinned_artifacts=candidate_artifacts,
    )
    return replace(
        _with_dependency_binding(diagnostic, artifact.mapped_source),
        frames=frames,
        worker_frames=worker_frames,
    )


def _remap_worker_runtime_primary(
    parsed: ParsedPlatformDiagnostic,
    *,
    stage: DiagnosticStage,
    pinned_manifest_sha256: str,
    pinned_artifacts: tuple[WorkerDiagnosticArtifact, ...],
) -> NormalizedDiagnostic:
    """Resolve every Worker stack frame through one immutable operation pin."""
    _validate_worker_diagnostic_request(
        parsed,
        pinned_manifest_sha256,
        pinned_artifacts,
    )
    frames: list[WorkerRuntimeFrameDiagnostic] = []
    for location in parsed.locations:
        worker_location = location.worker_artifact_location
        if worker_location is None:
            module_name = location.module_name
            if module_name is not None and module_name in _UNKNOWN_MODULES:
                frames.append(_unknown_worker_projection(module_name))
            continue
        artifact = _pinned_worker_artifact(
            worker_location.registration_name,
            pinned_manifest_sha256,
            pinned_artifacts,
        )
        if artifact is None:
            frames.append(_unknown_worker_projection(worker_location.registration_name))
            continue
        try:
            frames.append(
                _worker_runtime_frame(
                    location,
                    artifact,
                    worker_location.registration_name,
                )
            )
        except BaseException:
            frames.append(
                _unknown_worker_projection(worker_location.registration_name)
            )
    if not frames:
        return _unmapped_worker_diagnostic(
            parsed,
            stage=stage,
            code="worker_runtime_frame_unmapped",
            identity=(pinned_manifest_sha256,),
        )
    primary = frames[0]
    source_map_sha256 = None
    execution_artifact_sha256 = None
    if primary.artifact_sha256 is not None:
        artifact = next(
            (
                item
                for item in pinned_artifacts
                if item.manifest_sha256 == pinned_manifest_sha256
                and item.artifact_sha256 == primary.artifact_sha256
                and item.registration_name.casefold()
                == primary.registration_name.casefold()
            ),
            None,
        )
        if artifact is not None:
            source_map_sha256 = artifact.mapped_source.source_map_sha256
            execution_artifact_sha256 = artifact.mapped_source.artifact.source_sha256
    return NormalizedDiagnostic(
        diagnostic_id=_worker_diagnostic_id(
            stage,
            "worker_runtime_frames",
            parsed.platform_diagnostic_sha256,
            (pinned_manifest_sha256, *(frame.registration_name for frame in frames)),
        ),
        runtime_summary=_summary(stage),
        stage=stage,
        mapping_confidence=primary.mapping_confidence,
        code=(
            "dependency_binding"
            if primary.synthetic_region == "dependency_binding"
            else (
                "worker_runtime_frame_unmapped"
                if primary.mapping_confidence is MappingConfidence.UNKNOWN
                else None
            )
        ),
        source_unit=primary.source_unit,
        visible_location=primary.visible_location,
        related_visible_span=primary.related_visible_span,
        lowered_location=primary.lowered_location,
        synthetic_region=primary.synthetic_region,
        _platform_evidence=parsed._platform_evidence,
        platform_diagnostic_sha256=parsed.platform_diagnostic_sha256,
        platform_diagnostic_truncated=parsed.platform_diagnostic_truncated,
        platform_diagnostic_redacted=parsed.platform_diagnostic_redacted,
        execution_artifact_sha256=execution_artifact_sha256,
        source_map_sha256=source_map_sha256,
        dependency_anchor=primary.dependency_anchor,
        method_anchor=primary.method_anchor,
        worker_frames=tuple(frames),
    )


def _remap_platform_primary(
    parsed: ParsedPlatformDiagnostic,
    executed: MappedSource,
    *,
    stage: DiagnosticStage,
    visible_source_context: VisibleSourceContext | None = None,
) -> NormalizedDiagnostic:
    if not isinstance(parsed, ParsedPlatformDiagnostic):
        raise ValueError("parsed must be a ParsedPlatformDiagnostic")
    if not isinstance(executed, MappedSource):
        raise ValueError("executed must be a MappedSource")
    if type(stage) is not DiagnosticStage:
        raise ValueError("stage must be a DiagnosticStage")
    if visible_source_context is not None and not isinstance(
        visible_source_context,
        VisibleSourceContext,
    ):
        raise ValueError("visible_source_context must be a VisibleSourceContext")
    effective_stage = (
        DiagnosticStage.COMPILATION if parsed.has_compilation_marker else stage
    )
    offset = None
    if (
        parsed.coordinate_space is DiagnosticCoordinateSpace.EXECUTED_BSL
        and parsed.line is not None
        and parsed.column is not None
    ):
        offset = PlatformCoordinateCodec(executed.text).to_offset(
            parsed.line,
            parsed.column,
        )
    mapping = _map_executed_offset(executed, offset, visible_source_context)
    lowered = None
    if offset is not None and parsed.line is not None and parsed.column is not None:
        width = 0 if offset == len(executed.text) else 1
        lowered = LoweredSourceLocation(
            parsed.line,
            parsed.column,
            offset,
            SourceSpan(offset, offset + width),
        )
    diagnostic_id = _diagnostic_id(
        effective_stage,
        None,
        parsed.platform_diagnostic_sha256,
        executed,
        offset,
    )
    return NormalizedDiagnostic(
        diagnostic_id=diagnostic_id,
        runtime_summary=_summary(effective_stage),
        stage=effective_stage,
        mapping_confidence=mapping.confidence,
        source_unit=mapping.source_unit,
        visible_location=mapping.visible,
        related_visible_span=mapping.related,
        lowered_location=lowered,
        synthetic_region=mapping.synthetic_region,
        _platform_evidence=parsed._platform_evidence,
        platform_diagnostic_sha256=parsed.platform_diagnostic_sha256,
        platform_diagnostic_truncated=parsed.platform_diagnostic_truncated,
        platform_diagnostic_redacted=parsed.platform_diagnostic_redacted,
        execution_artifact_sha256=executed.artifact.source_sha256,
        source_map_sha256=executed.source_map_sha256,
    )


def remap_platform_diagnostic(
    parsed: ParsedPlatformDiagnostic,
    executed: MappedSource,
    *,
    stage: DiagnosticStage,
    visible_source_context: VisibleSourceContext | None = None,
) -> NormalizedDiagnostic:
    return normalize_platform_diagnostic_trace(
        parsed,
        stage=stage,
        executed=executed,
        visible_source_context=visible_source_context,
    )


def _visible_line_span(
    mapping: _MappedDiagnosticOffset,
    context: VisibleSourceContext | None,
) -> SourceSpan | None:
    if mapping.visible is None or context is None:
        return None
    return context.line_range(mapping.visible.source_unit, mapping.visible.line)


def _main_trace_frame(
    frame: ParsedDiagnosticFrame,
    executed: MappedSource,
    context: VisibleSourceContext | None,
) -> ErrorTraceFrame:
    location = frame.location
    if location.column is None:
        position = _line_only_mapped_position(executed, location.line)
        column = None if position is None else position[0]
        offset = None if position is None else position[1]
    else:
        column = location.column
        offset = PlatformCoordinateCodec(executed.text).to_offset(
            location.line,
            location.column,
        )
    mapping = _map_executed_offset(executed, offset, context)
    lowered = None
    if offset is not None and column is not None:
        width = 0 if offset == len(executed.text) else 1
        lowered = LoweredSourceLocation(
            location.line,
            column,
            offset,
            SourceSpan(offset, offset + width),
        )
    return ErrorTraceFrame(
        ordinal=frame.ordinal,
        cause_ordinal=frame.cause_ordinal,
        origin=ErrorTraceFrameOrigin.EXECUTED_ARTIFACT,
        platform_location=location,
        block_span=frame.block_span,
        detail_span=frame.detail_span,
        mapping_confidence=mapping.confidence,
        source_unit=mapping.source_unit,
        visible_location=mapping.visible,
        visible_line_span=_visible_line_span(mapping, context),
        related_visible_span=mapping.related,
        lowered_location=lowered,
        synthetic_region=mapping.synthetic_region,
    )


def _native_trace_frame(frame: ParsedDiagnosticFrame) -> ErrorTraceFrame:
    return ErrorTraceFrame(
        ordinal=frame.ordinal,
        cause_ordinal=frame.cause_ordinal,
        origin=ErrorTraceFrameOrigin.NATIVE_MODULE,
        platform_location=frame.location,
        block_span=frame.block_span,
        detail_span=frame.detail_span,
        mapping_confidence=MappingConfidence.UNKNOWN,
    )


def _unknown_trace_frame(
    frame: ParsedDiagnosticFrame,
    *,
    registration_name: str | None = None,
) -> ErrorTraceFrame:
    return ErrorTraceFrame(
        ordinal=frame.ordinal,
        cause_ordinal=frame.cause_ordinal,
        origin=ErrorTraceFrameOrigin.UNKNOWN,
        platform_location=frame.location,
        block_span=frame.block_span,
        detail_span=frame.detail_span,
        mapping_confidence=MappingConfidence.UNKNOWN,
        registration_name=registration_name,
    )


def _unknown_worker_projection(
    observed_registration: str,
) -> WorkerRuntimeFrameDiagnostic:
    return WorkerRuntimeFrameDiagnostic(
        observed_registration,
        None,
        None,
        None,
        MappingConfidence.UNKNOWN,
    )


def _pinned_worker_artifact(
    registration_name: str,
    manifest_sha256: str,
    artifacts: tuple[WorkerDiagnosticArtifact, ...],
) -> WorkerDiagnosticArtifact | None:
    matches = tuple(
        artifact
        for artifact in artifacts
        if artifact.manifest_sha256 == manifest_sha256
        and artifact.registration_name.casefold() == registration_name.casefold()
    )
    return matches[0] if len(matches) == 1 else None


def _is_main_trace_frame(frame: ParsedDiagnosticFrame) -> bool:
    location = frame.location
    if location.module_name in _UNKNOWN_MODULES:
        return True
    return (
        location.module_name is None
        and location.extension_name is None
        and location.column is None
        and frame.cause_ordinal == 0
    )


def _worker_trace_frame(
    frame: ParsedDiagnosticFrame,
    artifact: WorkerDiagnosticArtifact,
    observed_registration: str,
) -> tuple[ErrorTraceFrame, WorkerRuntimeFrameDiagnostic]:
    legacy = _worker_runtime_frame(
        frame.location,
        artifact,
        observed_registration,
    )
    visible_line = (
        None
        if legacy.visible_location is None
        or artifact.visible_source_context is None
        else artifact.visible_source_context.line_range(
            legacy.visible_location.source_unit,
            legacy.visible_location.line,
        )
    )
    return (
        ErrorTraceFrame(
            ordinal=frame.ordinal,
            cause_ordinal=frame.cause_ordinal,
            origin=ErrorTraceFrameOrigin.WORKER_ARTIFACT,
            platform_location=frame.location,
            block_span=frame.block_span,
            detail_span=frame.detail_span,
            mapping_confidence=legacy.mapping_confidence,
            registration_name=observed_registration,
            logical_name=legacy.logical_name,
            revision=legacy.revision,
            artifact_sha256=legacy.artifact_sha256,
            source_unit=legacy.source_unit,
            visible_location=legacy.visible_location,
            visible_line_span=visible_line,
            related_visible_span=legacy.related_visible_span,
            lowered_location=legacy.lowered_location,
            synthetic_region=legacy.synthetic_region,
            dependency_anchor=legacy.dependency_anchor,
            method_anchor=legacy.method_anchor,
        ),
        legacy,
    )


def _normalize_trace_frames(
    parsed: ParsedPlatformDiagnostic,
    *,
    executed: MappedSource | None,
    visible_source_context: VisibleSourceContext | None,
    pinned_manifest_sha256: str | None,
    pinned_artifacts: tuple[WorkerDiagnosticArtifact, ...],
) -> tuple[
    tuple[ErrorTraceFrame, ...],
    tuple[WorkerRuntimeFrameDiagnostic, ...],
]:
    frames: list[ErrorTraceFrame] = []
    worker_frames: list[WorkerRuntimeFrameDiagnostic] = []
    include_worker_projection = pinned_manifest_sha256 is not None
    for item in parsed.frames:
        worker_location = item.location.worker_artifact_location
        module_name = item.location.module_name
        observed_registration = (
            None if worker_location is None else worker_location.registration_name
        )
        try:
            if worker_location is not None and pinned_manifest_sha256 is not None:
                artifact = _pinned_worker_artifact(
                    worker_location.registration_name,
                    pinned_manifest_sha256,
                    pinned_artifacts,
                )
                if artifact is None:
                    normalized = _unknown_trace_frame(
                        item,
                        registration_name=worker_location.registration_name,
                    )
                    legacy = _unknown_worker_projection(
                        worker_location.registration_name
                    )
                else:
                    normalized, legacy = _worker_trace_frame(
                        item,
                        artifact,
                        worker_location.registration_name,
                    )
                frames.append(normalized)
                worker_frames.append(legacy)
                continue
            if worker_location is not None:
                frames.append(
                    _unknown_trace_frame(
                        item,
                        registration_name=worker_location.registration_name,
                    )
                )
                continue
            if _is_main_trace_frame(item):
                frames.append(
                    _main_trace_frame(item, executed, visible_source_context)
                    if executed is not None
                    else _unknown_trace_frame(item)
                )
                if (
                    include_worker_projection
                    and module_name is not None
                    and module_name in _UNKNOWN_MODULES
                ):
                    worker_frames.append(_unknown_worker_projection(module_name))
                continue
            frames.append(
                _unknown_trace_frame(item)
                if module_name is None
                else _native_trace_frame(item)
            )
        except BaseException:
            frames.append(
                _unknown_trace_frame(
                    item,
                    registration_name=observed_registration,
                )
            )
            if include_worker_projection and (
                worker_location is not None
                or (module_name is not None and module_name in _UNKNOWN_MODULES)
            ):
                projection_name = observed_registration or module_name
                if projection_name is None:
                    continue
                worker_frames.append(
                    _unknown_worker_projection(projection_name)
                )
    return tuple(frames), tuple(worker_frames)


def _generic_trace_base(
    parsed: ParsedPlatformDiagnostic,
    stage: DiagnosticStage,
) -> NormalizedDiagnostic:
    effective_stage = (
        DiagnosticStage.COMPILATION if parsed.has_compilation_marker else stage
    )
    diagnostic_id = sha256(
        "|".join(
            (
                effective_stage.value,
                "platform_trace",
                parsed.platform_diagnostic_sha256,
            )
        ).encode("utf-8")
    ).hexdigest()
    return NormalizedDiagnostic(
        diagnostic_id=diagnostic_id,
        runtime_summary=_summary(effective_stage),
        stage=effective_stage,
        mapping_confidence=MappingConfidence.UNKNOWN,
        _platform_evidence=parsed._platform_evidence,
        platform_diagnostic_sha256=parsed.platform_diagnostic_sha256,
        platform_diagnostic_truncated=parsed.platform_diagnostic_truncated,
        platform_diagnostic_redacted=parsed.platform_diagnostic_redacted,
    )


def normalize_platform_diagnostic_trace(
    parsed: ParsedPlatformDiagnostic,
    *,
    stage: DiagnosticStage,
    executed: MappedSource | None = None,
    visible_source_context: VisibleSourceContext | None = None,
    pinned_manifest_sha256: str | None = None,
    pinned_artifacts: tuple[WorkerDiagnosticArtifact, ...] = (),
) -> NormalizedDiagnostic:
    if not isinstance(parsed, ParsedPlatformDiagnostic):
        raise ValueError("parsed must be a ParsedPlatformDiagnostic")
    if type(stage) is not DiagnosticStage:
        raise ValueError("stage must be a DiagnosticStage")
    if executed is not None and not isinstance(executed, MappedSource):
        raise ValueError("executed must be a MappedSource or None")
    if visible_source_context is not None and not isinstance(
        visible_source_context,
        VisibleSourceContext,
    ):
        raise ValueError("visible_source_context must be a VisibleSourceContext")
    if executed is None and visible_source_context is not None:
        raise ValueError("visible source context requires an executed artifact")
    if pinned_manifest_sha256 is not None and type(pinned_manifest_sha256) is not str:
        raise ValueError("pinned manifest identity must be a string or None")
    if type(pinned_artifacts) is not tuple:
        raise ValueError("pinned artifacts must be an immutable tuple")
    if pinned_manifest_sha256 is None:
        if pinned_artifacts:
            raise ValueError("pinned artifacts require a manifest identity")
    else:
        _validate_worker_diagnostic_request(
            parsed,
            pinned_manifest_sha256,
            pinned_artifacts,
        )
    if pinned_manifest_sha256 is not None and (
        executed is None
        or (
            bool(parsed.locations)
            and parsed.locations[0].worker_artifact_location is not None
        )
    ):
        base = _remap_worker_runtime_primary(
            parsed,
            stage=stage,
            pinned_manifest_sha256=pinned_manifest_sha256,
            pinned_artifacts=pinned_artifacts,
        )
    elif executed is not None:
        base = _remap_platform_primary(
            parsed,
            executed,
            stage=stage,
            visible_source_context=visible_source_context,
        )
    else:
        base = _generic_trace_base(parsed, stage)
    causes = tuple(
        ErrorTraceCause(
            item.ordinal,
            item.summary_span,
            item.block_span,
            item.frame_ordinals,
            item.category,
        )
        for item in parsed.causes
    )
    normalized_frames, normalized_worker_frames = _normalize_trace_frames(
        parsed,
        executed=executed,
        visible_source_context=visible_source_context,
        pinned_manifest_sha256=pinned_manifest_sha256,
        pinned_artifacts=pinned_artifacts,
    )
    compatibility_frames = (
        base.worker_frames
        if executed is None and pinned_manifest_sha256 is not None
        else normalized_worker_frames
    )
    return replace(
        base,
        causes=causes,
        frames=normalized_frames,
        frames_truncated=parsed.frames_truncated,
        causes_truncated=parsed.causes_truncated,
        worker_frames=compatibility_frames,
    )


def remap_worker_runtime_diagnostic(
    parsed: ParsedPlatformDiagnostic,
    *,
    pinned_manifest_sha256: str,
    pinned_artifacts: tuple[WorkerDiagnosticArtifact, ...],
) -> NormalizedDiagnostic:
    """Resolve every Worker stack frame through one immutable operation pin."""
    return normalize_platform_diagnostic_trace(
        parsed,
        stage=DiagnosticStage.EXECUTION,
        pinned_manifest_sha256=pinned_manifest_sha256,
        pinned_artifacts=pinned_artifacts,
    )


def normalize_source_error(
    error: Exception,
    source: MappedSource,
    *,
    stage: DiagnosticStage,
    visible_source_context: VisibleSourceContext | None = None,
) -> NormalizedDiagnostic:
    """Normalize a deterministic source error without a platform round trip."""
    if not isinstance(source, MappedSource):
        raise ValueError("source must be a MappedSource")
    if type(stage) is not DiagnosticStage:
        raise ValueError("stage must be a DiagnosticStage")
    if visible_source_context is not None and not isinstance(
        visible_source_context,
        VisibleSourceContext,
    ):
        raise ValueError("visible_source_context must be a VisibleSourceContext")
    span = getattr(error, "span", None)
    code = getattr(error, "code", None)
    if not isinstance(span, SourceSpan) or type(code) is not str or not code:
        raise ValueError("source error must provide a SourceSpan and stable code")
    offset = span.start if span.end <= len(source.text) else None
    mapping = _map_executed_offset(source, offset, visible_source_context)
    lowered = None
    if offset is not None:
        line, column = LineIndex(source.text).offset_to_line_column(offset)
        lowered = LoweredSourceLocation(line, column, offset, span)
    message_sha = sha256(str(error).encode("utf-8")).hexdigest()
    return NormalizedDiagnostic(
        diagnostic_id=_diagnostic_id(stage, code, message_sha, source, offset),
        runtime_summary=_summary(stage),
        stage=stage,
        mapping_confidence=mapping.confidence,
        code=code,
        source_unit=mapping.source_unit,
        visible_location=mapping.visible,
        related_visible_span=mapping.related,
        lowered_location=lowered,
        synthetic_region=mapping.synthetic_region,
        execution_artifact_sha256=source.artifact.source_sha256,
        source_map_sha256=source.source_map_sha256,
    )


@dataclass(frozen=True, slots=True)
class _MappedDiagnosticOffset:
    confidence: MappingConfidence
    source_unit: SourceUnitRef | None = None
    visible: VisibleSourceLocation | None = None
    related: SourceSpan | None = None
    synthetic_region: str | None = None


def _map_executed_offset(
    source: MappedSource,
    offset: int | None,
    visible_source_context: VisibleSourceContext | None,
) -> _MappedDiagnosticOffset:
    if offset is None:
        return _MappedDiagnosticOffset(MappingConfidence.UNKNOWN)
    try:
        mapped = source.source_map.map_offset(offset)
    except ValueError:
        return _MappedDiagnosticOffset(MappingConfidence.UNKNOWN)
    if (
        mapped.relation is MappingRelation.EXACT
        and mapped.unit is not None
        and mapped.origin_span is not None
    ):
        coordinates = (
            visible_source_context.line_column(
                mapped.unit,
                mapped.origin_span.start,
            )
            if visible_source_context is not None
            else None
        )
        if coordinates is None:
            return _MappedDiagnosticOffset(
                MappingConfidence.NEAREST,
                mapped.unit,
                related=mapped.origin_span,
            )
        return _MappedDiagnosticOffset(
            MappingConfidence.EXACT,
            mapped.unit,
            VisibleSourceLocation(
                mapped.unit,
                coordinates[0],
                coordinates[1],
                mapped.origin_span,
            ),
        )
    if (
        mapped.relation is MappingRelation.DERIVED
        and mapped.unit is not None
        and mapped.origin_span is not None
    ):
        return _MappedDiagnosticOffset(
            MappingConfidence.NEAREST,
            mapped.unit,
            related=mapped.origin_span,
        )
    if mapped.relation is MappingRelation.SYNTHETIC:
        return _MappedDiagnosticOffset(
            MappingConfidence.SYNTHETIC,
            mapped.anchor_unit,
            related=mapped.anchor_span,
            synthetic_region=mapped.synthetic_region,
        )
    return _MappedDiagnosticOffset(MappingConfidence.UNKNOWN)


def _validate_worker_diagnostic_request(
    parsed: ParsedPlatformDiagnostic,
    manifest_sha256: str,
    artifacts: tuple[WorkerDiagnosticArtifact, ...],
) -> None:
    if not isinstance(parsed, ParsedPlatformDiagnostic):
        raise ValueError("parsed must be a ParsedPlatformDiagnostic")
    if re.fullmatch(r"[0-9a-f]{64}", manifest_sha256) is None:
        raise ValueError("worker diagnostic manifest identity is invalid")
    if type(artifacts) is not tuple or any(
        not isinstance(item, WorkerDiagnosticArtifact) for item in artifacts
    ):
        raise ValueError("worker diagnostic artifacts must be an immutable tuple")


def _unmapped_worker_diagnostic(
    parsed: ParsedPlatformDiagnostic,
    *,
    stage: DiagnosticStage,
    code: str,
    identity: tuple[str, ...],
) -> NormalizedDiagnostic:
    return NormalizedDiagnostic(
        diagnostic_id=_worker_diagnostic_id(
            stage,
            code,
            parsed.platform_diagnostic_sha256,
            identity,
        ),
        runtime_summary=_summary(stage),
        stage=stage,
        mapping_confidence=MappingConfidence.UNKNOWN,
        code=code,
        _platform_evidence=parsed._platform_evidence,
        platform_diagnostic_sha256=parsed.platform_diagnostic_sha256,
        platform_diagnostic_truncated=parsed.platform_diagnostic_truncated,
        platform_diagnostic_redacted=parsed.platform_diagnostic_redacted,
    )


def _with_dependency_binding(
    diagnostic: NormalizedDiagnostic,
    source: MappedSource,
) -> NormalizedDiagnostic:
    lowered = diagnostic.lowered_location
    if lowered is None:
        return diagnostic
    try:
        mapped = source.source_map.map_offset(lowered.offset)
    except ValueError:
        return diagnostic
    segment = _source_map_segment_at(source, lowered.offset)
    region = None if segment is None else segment.synthetic_region
    if region not in {
        "worker_dependency_field",
        "worker_dependency_alias_declaration",
        "worker_dependency_alias_initializer",
    }:
        return diagnostic
    dependency_anchor = (
        mapped.origin_span
        if mapped.relation is MappingRelation.DERIVED
        else mapped.anchor_span
    )
    method_anchor = (
        mapped.anchor_span
        if mapped.relation is MappingRelation.DERIVED
        else None
    )
    return replace(
        diagnostic,
        code="dependency_binding",
        synthetic_region="dependency_binding",
        dependency_anchor=dependency_anchor,
        method_anchor=method_anchor,
    )


def _line_only_mapped_position(
    source: MappedSource,
    line: int,
) -> tuple[int, int] | None:
    """Resolve a line-only frame only through one exact code span."""
    if type(line) is not int or line <= 0:
        return None
    index = LineIndex(source.text)
    try:
        line_start = index.line_column_to_offset(line, 1)
    except ValueError:
        return None
    newline = source.text.find("\n", line_start)
    line_end = len(source.text) if newline < 0 else newline
    code_offsets = tuple(
        offset
        for offset in range(line_start, line_end)
        if not source.text[offset].isspace()
    )
    if not code_offsets:
        return None
    first = code_offsets[0]
    last = code_offsets[-1] + 1
    spans = tuple(
        segment
        for segment in source.source_map.segments
        if segment.generated.start < last and first < segment.generated.end
    )
    if (
        len(spans) != 1
        or spans[0].relation is not MappingRelation.EXACT
        or not isinstance(spans[0].origin_ref, SourceUnitRef)
        or spans[0].origin is None
    ):
        return None
    try:
        mapped = source.source_map.map_offset(first)
    except ValueError:
        return None
    if (
        mapped.relation is not MappingRelation.EXACT
        or mapped.unit is None
        or mapped.origin_span is None
    ):
        return None
    return first - line_start + 1, first


def _worker_runtime_frame(
    location: PlatformDiagnosticLocation,
    artifact: WorkerDiagnosticArtifact,
    observed_registration: str,
) -> WorkerRuntimeFrameDiagnostic:
    if location.column is None:
        normalized = _line_only_mapped_position(
            artifact.mapped_source,
            location.line,
        )
        column = None if normalized is None else normalized[0]
        offset = None if normalized is None else normalized[1]
    else:
        column = location.column
        offset = PlatformCoordinateCodec(artifact.mapped_source.text).to_offset(
            location.line,
            location.column,
        )
    mapping = _map_executed_offset(
        artifact.mapped_source,
        offset,
        artifact.visible_source_context,
    )
    lowered = None
    if offset is not None and column is not None:
        width = 0 if offset == len(artifact.mapped_source.text) else 1
        lowered = LoweredSourceLocation(
            location.line,
            column,
            offset,
            SourceSpan(offset, offset + width),
        )
    dependency_anchor = None
    method_anchor = None
    synthetic_region = mapping.synthetic_region
    if offset is not None:
        try:
            mapped = artifact.mapped_source.source_map.map_offset(offset)
        except ValueError:
            mapped = None
        segment = _source_map_segment_at(artifact.mapped_source, offset)
        if mapped is not None and segment is not None and segment.synthetic_region in {
            "worker_dependency_field",
            "worker_dependency_alias_declaration",
            "worker_dependency_alias_initializer",
        }:
            synthetic_region = "dependency_binding"
            dependency_anchor = (
                mapped.origin_span
                if mapped.relation is MappingRelation.DERIVED
                else mapped.anchor_span
            )
            method_anchor = (
                mapped.anchor_span
                if mapped.relation is MappingRelation.DERIVED
                else None
            )
    return WorkerRuntimeFrameDiagnostic(
        observed_registration,
        artifact.logical_name,
        artifact.revision,
        artifact.artifact_sha256,
        mapping.confidence,
        mapping.source_unit,
        mapping.visible,
        mapping.related,
        lowered,
        synthetic_region,
        dependency_anchor,
        method_anchor,
    )


def _worker_stage_locations(
    parsed: ParsedPlatformDiagnostic,
    artifact: WorkerDiagnosticArtifact,
) -> tuple[PlatformDiagnosticLocation, ...]:
    return tuple(
        location
        for location in parsed.locations
        if location.worker_artifact_location is not None
        and location.worker_artifact_location.registration_name.casefold()
        == artifact.registration_name.casefold()
    )


def _parse_worker_artifact_location(
    components: tuple[str, ...],
) -> WorkerArtifactPlatformLocation | None:
    if (
        len(components) == 3
        and components[0].casefold() == "внешняяобработка"
        and _WORKER_REGISTRATION_RE.fullmatch(components[1]) is not None
        and components[2].casefold() == "модульобъекта"
    ):
        return WorkerArtifactPlatformLocation(components[1])
    return None


def _source_map_segment_at(
    source: MappedSource,
    offset: int,
) -> SourceMapSegment | None:
    for segment in source.source_map.segments:
        if segment.generated.start <= offset < segment.generated.end:
            return segment
    return None


def _worker_diagnostic_id(
    stage: DiagnosticStage,
    code: str,
    message_sha256: str,
    identity: tuple[str, ...],
) -> str:
    return sha256(
        "|".join((stage.value, code, message_sha256, *identity)).encode("utf-8")
    ).hexdigest()


def _summary(stage: DiagnosticStage) -> str:
    return f"BSL {stage.value} failed"


def _visible_unit_key(unit: SourceUnitRef) -> tuple[object, str, int]:
    return unit.kind, unit.unit_id, unit.revision


def _diagnostic_id(
    stage: DiagnosticStage,
    code: str | None,
    message_sha256: str,
    source: MappedSource,
    offset: int | None,
) -> str:
    identity = "|".join(
        (
            stage.value,
            code or "platform",
            message_sha256,
            source.artifact.source_sha256,
            source.source_map_sha256,
            "unknown" if offset is None else str(offset),
        )
    )
    return sha256(identity.encode("utf-8")).hexdigest()
