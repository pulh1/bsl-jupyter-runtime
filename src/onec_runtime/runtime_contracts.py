"""Immutable contracts shared by core runtime adapters."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
import re

from onec_runtime.bsl import (
    DiagnosticStage,
    MappingConfidence,
    NormalizedDiagnostic,
    SourceSpan,
    SourceUnitKind,
    SourceUnitRef,
    VisibleSourceLocation,
)
from onec_runtime.bsl.diagnostics import (
    DiagnosticCoordinateSpace,
    DiagnosticTextSpan,
    ErrorTraceCause,
    ErrorTraceFrame,
    ErrorTraceFrameOrigin,
    LoweredSourceLocation,
    PlatformDiagnosticLocation,
    WorkerArtifactPlatformLocation,
    WorkerRuntimeFrameDiagnostic,
)


MAX_DIAGNOSTIC_COORDINATE = 10_000_000
MAX_DIAGNOSTIC_LABEL_LENGTH = 128
MAX_PRIVATE_DIAGNOSTIC_BYTES = 64 * 1024
MAX_DIAGNOSTIC_FRAMES = 128
MAX_DIAGNOSTIC_CAUSES = 32

_DIAGNOSTIC_ID_RE = re.compile(r"[0-9a-f]{64}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_DIAGNOSTIC_LABEL_RE = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")
_MODULE_COMPONENT_RE = re.compile(r"[A-Za-zА-Яа-яЁё_][0-9A-Za-zА-Яа-яЁё_]*\Z")
_WORKER_REGISTRATION_RE = re.compile(
    r"OnecRuntime_[0-9a-f]{8}_[0-9a-f]{16}\Z",
    re.IGNORECASE,
)
_UNKNOWN_MODULES = frozenset({"<Неизвестный модуль>", "Неизвестный модуль"})
_DIAGNOSTIC_SUMMARIES = {
    DiagnosticStage.PARSING: "BSL parsing failed",
    DiagnosticStage.LOWERING: "BSL lowering failed",
    DiagnosticStage.COMPILATION: "BSL compilation failed",
    DiagnosticStage.EXECUTION: "BSL execution failed",
}
_EXECUTION_MODES = frozenset({"main", "capture", "worker"})


def sanitize_normalized_diagnostic(
    value: object,
) -> NormalizedDiagnostic | None:
    """Return a canonical bounded diagnostic or fail closed without raising."""
    try:
        if not isinstance(value, NormalizedDiagnostic):
            return None
        if type(value.stage) is not DiagnosticStage:
            return None
        if type(value.mapping_confidence) is not MappingConfidence:
            return None
        if (
            type(value.diagnostic_id) is not str
            or _DIAGNOSTIC_ID_RE.fullmatch(value.diagnostic_id) is None
        ):
            return None
        if value.code is not None and (
            type(value.code) is not str
            or len(value.code) > MAX_DIAGNOSTIC_LABEL_LENGTH
            or _DIAGNOSTIC_LABEL_RE.fullmatch(value.code) is None
        ):
            return None
        if value.synthetic_region is not None and (
            type(value.synthetic_region) is not str
            or len(value.synthetic_region) > MAX_DIAGNOSTIC_LABEL_LENGTH
            or _DIAGNOSTIC_LABEL_RE.fullmatch(value.synthetic_region) is None
        ):
            return None
        if value.source_unit is not None and (
            not _bounded_source_unit(value.source_unit)
        ):
            return None
        if value.visible_location is not None:
            location = value.visible_location
            if (
                not isinstance(location, VisibleSourceLocation)
                or not _bounded_source_unit(location.source_unit)
                or not _bounded_positive_coordinate(location.line)
                or not _bounded_positive_coordinate(location.column)
                or not _bounded_span(location.span)
                or (
                    value.source_unit is not None
                    and location.source_unit != value.source_unit
                )
            ):
                return None
        if value.related_visible_span is not None and not _bounded_span(
            value.related_visible_span
        ):
            return None
        if any(
            not _bounded_optional_span(anchor)
            for anchor in (value.dependency_anchor, value.method_anchor)
        ):
            return None
        if value.lowered_location is not None:
            location = value.lowered_location
            if (
                not isinstance(location, LoweredSourceLocation)
                or not _bounded_positive_coordinate(location.line)
                or not _bounded_positive_coordinate(location.column)
                or type(location.offset) is not int
                or not 0 <= location.offset <= MAX_DIAGNOSTIC_COORDINATE
                or not _bounded_span(location.span)
            ):
                return None
        for name in (
            "platform_diagnostic_sha256",
            "execution_artifact_sha256",
            "source_map_sha256",
        ):
            digest = getattr(value, name)
            if digest is not None and (
                type(digest) is not str or _SHA256_RE.fullmatch(digest) is None
            ):
                return None
        if type(value.platform_diagnostic_truncated) is not bool or type(
            value.platform_diagnostic_redacted
        ) is not bool:
            return None
        platform_diagnostic = value.platform_diagnostic
        if platform_diagnostic is not None and (
            type(platform_diagnostic) is not str
            or len(platform_diagnostic.encode("utf-8")) > MAX_PRIVATE_DIAGNOSTIC_BYTES
            or value.platform_diagnostic_sha256 is None
        ):
            return None
        if not _bounded_trace(value, platform_diagnostic):
            return None
        return replace(
            value,
            runtime_summary=_DIAGNOSTIC_SUMMARIES[value.stage],
        )
    except BaseException:
        return None


def _bounded_positive_coordinate(value: object) -> bool:
    return type(value) is int and 1 <= value <= MAX_DIAGNOSTIC_COORDINATE


def _bounded_span(value: object) -> bool:
    return (
        isinstance(value, SourceSpan)
        and type(value.start) is int
        and type(value.end) is int
        and 0 <= value.start <= value.end <= MAX_DIAGNOSTIC_COORDINATE
    )


def _bounded_source_unit(value: object) -> bool:
    return (
        isinstance(value, SourceUnitRef)
        and type(value.kind) is SourceUnitKind
        and type(value.unit_id) is str
        and 0 < len(value.unit_id) <= 256
        and type(value.revision) is int
        and 0 <= value.revision <= MAX_DIAGNOSTIC_COORDINATE
        and type(value.source_sha256) is str
        and _SHA256_RE.fullmatch(value.source_sha256) is not None
    )


def _bounded_diagnostic_span(value: object, text_length: int) -> bool:
    return (
        isinstance(value, DiagnosticTextSpan)
        and type(value.start) is int
        and type(value.end) is int
        and 0 <= value.start <= value.end <= text_length
    )


def _bounded_platform_location(value: object) -> bool:
    if (
        not isinstance(value, PlatformDiagnosticLocation)
        or type(value.module_name) is not str
        or not 0 < len(value.module_name) <= 512
        or type(value.module_components) is not tuple
        or not 0 < len(value.module_components) <= 32
        or type(value.line) is not int
        or not 0 <= value.line <= MAX_DIAGNOSTIC_COORDINATE
        or (value.column is None and value.line == 0)
        or (
            value.column is not None
            and (
                type(value.column) is not int
                or not 0 <= value.column <= MAX_DIAGNOSTIC_COORDINATE
            )
        )
        or type(value.coordinate_space) is not DiagnosticCoordinateSpace
    ):
        return False
    if value.module_name in _UNKNOWN_MODULES:
        return (
            value.module_components == (value.module_name,)
            and value.worker_artifact_location is None
            and value.coordinate_space is DiagnosticCoordinateSpace.EXECUTED_BSL
        )
    if (
        value.coordinate_space is not DiagnosticCoordinateSpace.HOST_MODULE
        or value.module_name != ".".join(value.module_components)
        or any(
            type(item) is not str or _MODULE_COMPONENT_RE.fullmatch(item) is None
            for item in value.module_components
        )
    ):
        return False
    canonical_worker = (
        len(value.module_components) == 3
        and value.module_components[0].casefold() == "внешняяобработка"
        and _WORKER_REGISTRATION_RE.fullmatch(value.module_components[1]) is not None
        and value.module_components[2].casefold() == "модульобъекта"
    )
    if not canonical_worker:
        return value.worker_artifact_location is None
    return (
        isinstance(value.worker_artifact_location, WorkerArtifactPlatformLocation)
        and value.worker_artifact_location.registration_name
        == value.module_components[1]
    )


def _is_unknown_platform_location(location: PlatformDiagnosticLocation) -> bool:
    return location.module_name in _UNKNOWN_MODULES


def _is_worker_platform_location(location: PlatformDiagnosticLocation) -> bool:
    return location.worker_artifact_location is not None


def _bounded_trace_origin(frame: ErrorTraceFrame) -> bool:
    location = frame.platform_location
    unknown = _is_unknown_platform_location(location)
    worker = _is_worker_platform_location(location)
    if frame.origin is ErrorTraceFrameOrigin.EXECUTED_ARTIFACT:
        return unknown and not worker
    if frame.origin is ErrorTraceFrameOrigin.WORKER_ARTIFACT:
        return worker
    if frame.origin is ErrorTraceFrameOrigin.NATIVE_MODULE:
        return not unknown and not worker
    return frame.origin is ErrorTraceFrameOrigin.UNKNOWN


def _bounded_optional_label(value: object, *, maximum: int = 256) -> bool:
    return value is None or (type(value) is str and 0 < len(value) <= maximum)


def _bounded_optional_span(value: object) -> bool:
    return value is None or _bounded_span(value)


def _bounded_visible_mapping(
    source_unit: SourceUnitRef | None,
    visible_location: VisibleSourceLocation | None,
    visible_line_span: SourceSpan | None = None,
) -> bool:
    if visible_location is None:
        return visible_line_span is None
    if (
        source_unit is None
        or visible_location.source_unit != source_unit
        or not _bounded_span(visible_location.span)
    ):
        return False
    return visible_line_span is None or (
        visible_line_span.start <= visible_location.span.start
        and visible_location.span.end <= visible_line_span.end
    )


def _bounded_mapping_confidence(
    confidence: MappingConfidence,
    source_unit: SourceUnitRef | None,
    visible_location: VisibleSourceLocation | None,
    related_visible_span: SourceSpan | None,
    synthetic_region: str | None,
    dependency_anchor: SourceSpan | None,
    method_anchor: SourceSpan | None,
) -> bool:
    if confidence is MappingConfidence.EXACT:
        return source_unit is not None and visible_location is not None
    if confidence is MappingConfidence.NEAREST:
        return (
            source_unit is not None
            and visible_location is None
            and related_visible_span is not None
        )
    if confidence is MappingConfidence.SYNTHETIC:
        return (
            visible_location is None
            and synthetic_region is not None
            and (source_unit is None) == (related_visible_span is None)
        )
    if confidence is MappingConfidence.UNKNOWN:
        return all(
            value is None
            for value in (
                source_unit,
                visible_location,
                related_visible_span,
                synthetic_region,
                dependency_anchor,
                method_anchor,
            )
        )
    return False


def _bounded_trace_frame_fields(frame: ErrorTraceFrame) -> bool:
    if type(frame.mapping_confidence) is not MappingConfidence:
        return False
    if not _bounded_optional_label(frame.registration_name, maximum=512):
        return False
    if not _bounded_optional_label(frame.logical_name):
        return False
    if frame.revision is not None and (
        type(frame.revision) is not int
        or not 0 <= frame.revision <= MAX_DIAGNOSTIC_COORDINATE
    ):
        return False
    if frame.artifact_sha256 is not None and (
        type(frame.artifact_sha256) is not str
        or _SHA256_RE.fullmatch(frame.artifact_sha256) is None
    ):
        return False
    if frame.source_unit is not None and not _bounded_source_unit(frame.source_unit):
        return False
    if frame.visible_location is not None:
        visible = frame.visible_location
        if (
            not isinstance(visible, VisibleSourceLocation)
            or not _bounded_source_unit(visible.source_unit)
            or not _bounded_positive_coordinate(visible.line)
            or not _bounded_positive_coordinate(visible.column)
            or not _bounded_span(visible.span)
            or (
                frame.source_unit is not None
                and visible.source_unit != frame.source_unit
            )
        ):
            return False
    if not _bounded_visible_mapping(
        frame.source_unit,
        frame.visible_location,
        frame.visible_line_span,
    ):
        return False
    if frame.lowered_location is not None:
        lowered = frame.lowered_location
        if (
            not isinstance(lowered, LoweredSourceLocation)
            or not _bounded_positive_coordinate(lowered.line)
            or not _bounded_positive_coordinate(lowered.column)
            or type(lowered.offset) is not int
            or not 0 <= lowered.offset <= MAX_DIAGNOSTIC_COORDINATE
            or not _bounded_span(lowered.span)
        ):
            return False
        if (
            frame.origin
            not in {
                ErrorTraceFrameOrigin.EXECUTED_ARTIFACT,
                ErrorTraceFrameOrigin.WORKER_ARTIFACT,
            }
            or lowered.line != frame.platform_location.line
            or (
                frame.platform_location.column is not None
                and lowered.column != frame.platform_location.column
            )
        ):
            return False
    if any(
        not _bounded_optional_span(value)
        for value in (
            frame.visible_line_span,
            frame.related_visible_span,
            frame.dependency_anchor,
            frame.method_anchor,
        )
    ):
        return False
    if frame.synthetic_region is not None and (
        type(frame.synthetic_region) is not str
        or len(frame.synthetic_region) > MAX_DIAGNOSTIC_LABEL_LENGTH
        or _DIAGNOSTIC_LABEL_RE.fullmatch(frame.synthetic_region) is None
    ):
        return False
    return _bounded_mapping_confidence(
        frame.mapping_confidence,
        frame.source_unit,
        frame.visible_location,
        frame.related_visible_span,
        frame.synthetic_region,
        frame.dependency_anchor,
        frame.method_anchor,
    )


def _frame_has_no_generated_mapping(frame: ErrorTraceFrame) -> bool:
    return all(
        value is None
        for value in (
            frame.source_unit,
            frame.visible_location,
            frame.visible_line_span,
            frame.related_visible_span,
            frame.lowered_location,
            frame.synthetic_region,
            frame.dependency_anchor,
            frame.method_anchor,
        )
    )


def _frame_has_no_worker_provenance(frame: ErrorTraceFrame) -> bool:
    return all(
        value is None
        for value in (
            frame.registration_name,
            frame.logical_name,
            frame.revision,
            frame.artifact_sha256,
        )
    )


def _bounded_origin_specific_trace_fields(
    frame: ErrorTraceFrame,
) -> bool:
    location = frame.platform_location
    if frame.origin is ErrorTraceFrameOrigin.NATIVE_MODULE:
        return (
            frame.mapping_confidence is MappingConfidence.UNKNOWN
            and _frame_has_no_generated_mapping(frame)
            and _frame_has_no_worker_provenance(frame)
        )
    if frame.origin is ErrorTraceFrameOrigin.EXECUTED_ARTIFACT:
        return _frame_has_no_worker_provenance(frame)
    if frame.origin is ErrorTraceFrameOrigin.WORKER_ARTIFACT:
        worker_location = location.worker_artifact_location
        if (
            worker_location is None
            or frame.registration_name is None
            or frame.logical_name is None
            or frame.revision is None
            or frame.artifact_sha256 is None
            or worker_location.registration_name.casefold()
            != frame.registration_name.casefold()
        ):
            return False
        return True
    if frame.origin is ErrorTraceFrameOrigin.UNKNOWN:
        if (
            frame.mapping_confidence is not MappingConfidence.UNKNOWN
            or not _frame_has_no_generated_mapping(frame)
            or any(
                value is not None
                for value in (
                    frame.logical_name,
                    frame.revision,
                    frame.artifact_sha256,
                )
            )
        ):
            return False
        worker_location = location.worker_artifact_location
        if worker_location is None:
            return frame.registration_name is None
        return (
            frame.registration_name is not None
            and frame.registration_name.casefold()
            == worker_location.registration_name.casefold()
        )
    return False


def _bounded_worker_frame(value: object) -> bool:
    if not isinstance(value, WorkerRuntimeFrameDiagnostic):
        return False
    if not (
        type(value.registration_name) is str
        and 0 < len(value.registration_name) <= 512
    ):
        return False
    if not _bounded_optional_label(value.logical_name):
        return False
    if type(value.mapping_confidence) is not MappingConfidence:
        return False
    if value.revision is not None and (
        type(value.revision) is not int
        or not 0 <= value.revision <= MAX_DIAGNOSTIC_COORDINATE
    ):
        return False
    if value.artifact_sha256 is not None and (
        type(value.artifact_sha256) is not str
        or _SHA256_RE.fullmatch(value.artifact_sha256) is None
    ):
        return False
    identity_values = (
        value.logical_name,
        value.revision,
        value.artifact_sha256,
    )
    if any(item is None for item in identity_values) and any(
        item is not None for item in identity_values
    ):
        return False
    if all(item is None for item in identity_values) and (
        value.mapping_confidence is not MappingConfidence.UNKNOWN
        or any(
            item is not None
            for item in (
                value.source_unit,
                value.visible_location,
                value.related_visible_span,
                value.lowered_location,
                value.synthetic_region,
                value.dependency_anchor,
                value.method_anchor,
            )
        )
    ):
        return False
    if value.source_unit is not None and not _bounded_source_unit(value.source_unit):
        return False
    if value.visible_location is not None:
        visible = value.visible_location
        if (
            not isinstance(visible, VisibleSourceLocation)
            or not _bounded_source_unit(visible.source_unit)
            or not _bounded_positive_coordinate(visible.line)
            or not _bounded_positive_coordinate(visible.column)
            or not _bounded_span(visible.span)
            or (
                value.source_unit is not None
                and visible.source_unit != value.source_unit
            )
        ):
            return False
    if not _bounded_visible_mapping(value.source_unit, value.visible_location):
        return False
    if value.lowered_location is not None:
        lowered = value.lowered_location
        if (
            not isinstance(lowered, LoweredSourceLocation)
            or not _bounded_positive_coordinate(lowered.line)
            or not _bounded_positive_coordinate(lowered.column)
            or type(lowered.offset) is not int
            or not 0 <= lowered.offset <= MAX_DIAGNOSTIC_COORDINATE
            or not _bounded_span(lowered.span)
        ):
            return False
    if any(
        not _bounded_optional_span(item)
        for item in (
            value.related_visible_span,
            value.dependency_anchor,
            value.method_anchor,
        )
    ):
        return False
    if value.synthetic_region is not None and (
        type(value.synthetic_region) is not str
        or len(value.synthetic_region) > MAX_DIAGNOSTIC_LABEL_LENGTH
        or _DIAGNOSTIC_LABEL_RE.fullmatch(value.synthetic_region) is None
    ):
        return False
    return _bounded_mapping_confidence(
        value.mapping_confidence,
        value.source_unit,
        value.visible_location,
        value.related_visible_span,
        value.synthetic_region,
        value.dependency_anchor,
        value.method_anchor,
    )


def _bounded_trace(
    value: NormalizedDiagnostic,
    platform_text: str | None,
) -> bool:
    if (
        type(value.worker_frames) is not tuple
        or len(value.worker_frames) > MAX_DIAGNOSTIC_FRAMES
    ):
        return False
    if any(not _bounded_worker_frame(frame) for frame in value.worker_frames):
        return False
    if (
        type(value.frames) is not tuple
        or len(value.frames) > MAX_DIAGNOSTIC_FRAMES
    ):
        return False
    if type(value.causes) is not tuple or len(value.causes) > MAX_DIAGNOSTIC_CAUSES:
        return False
    if type(value.frames_truncated) is not bool or type(value.causes_truncated) is not bool:
        return False
    if (value.frames or value.causes) and platform_text is None:
        return False
    text_length = 0 if platform_text is None else len(platform_text)
    for index, cause in enumerate(value.causes):
        if (
            not isinstance(cause, ErrorTraceCause)
            or type(cause.ordinal) is not int
            or cause.ordinal != index
            or not _bounded_diagnostic_span(cause.summary_span, text_length)
            or not _bounded_diagnostic_span(cause.block_span, text_length)
            or not (
                cause.block_span.start
                <= cause.summary_span.start
                <= cause.summary_span.end
                <= cause.block_span.end
            )
            or type(cause.frame_ordinals) is not tuple
            or any(type(item) is not int for item in cause.frame_ordinals)
            or tuple(sorted(set(cause.frame_ordinals))) != cause.frame_ordinals
            or any(not 0 <= item < len(value.frames) for item in cause.frame_ordinals)
        ):
            return False
        if index and value.causes[index - 1].block_span.end > cause.block_span.start:
            return False
    for index, frame in enumerate(value.frames):
        if (
            not isinstance(frame, ErrorTraceFrame)
            or type(frame.ordinal) is not int
            or frame.ordinal != index
            or type(frame.origin) is not ErrorTraceFrameOrigin
            or not _bounded_platform_location(frame.platform_location)
            or not _bounded_trace_origin(frame)
            or not _bounded_trace_frame_fields(frame)
            or not _bounded_origin_specific_trace_fields(frame)
            or not _bounded_diagnostic_span(frame.block_span, text_length)
            or (
                frame.detail_span is not None
                and not _bounded_diagnostic_span(frame.detail_span, text_length)
            )
            or (
                frame.detail_span is not None
                and not (
                    frame.block_span.start
                    <= frame.detail_span.start
                    <= frame.detail_span.end
                    <= frame.block_span.end
                )
            )
            or (
                frame.cause_ordinal is not None
                and (
                    type(frame.cause_ordinal) is not int
                    or not 0 <= frame.cause_ordinal < len(value.causes)
                )
            )
        ):
            return False
        if frame.cause_ordinal is not None and index not in value.causes[
            frame.cause_ordinal
        ].frame_ordinals:
            return False
        if index and value.frames[index - 1].block_span.end > frame.block_span.start:
            return False
        if frame.cause_ordinal is not None:
            cause_block = value.causes[frame.cause_ordinal].block_span
            if not (
                cause_block.start <= frame.block_span.start
                and frame.block_span.end <= cause_block.end
            ):
                return False
    for cause in value.causes:
        if cause.frame_ordinals != tuple(
            frame.ordinal
            for frame in value.frames
            if frame.cause_ordinal == cause.ordinal
        ):
            return False
    return True


@dataclass(frozen=True, slots=True)
class OperationExecutionProvenance:
    """Hash-only identity of the exact artifact admitted for one operation."""

    visible_source_sha256: str
    executed_source_sha256: str
    source_map_sha256: str
    mode: str
    worker_generation: int | None = None
    worker_manifest_sha256: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "visible_source_sha256",
            "executed_source_sha256",
            "source_map_sha256",
        ):
            value = getattr(self, name)
            if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
                raise ValueError(f"{name} must be a lowercase sha256")
        if type(self.mode) is not str or self.mode not in _EXECUTION_MODES:
            raise ValueError("mode must be an allowlisted code mode")
        if self.worker_generation is not None and (
            type(self.worker_generation) is not int
            or not 0 <= self.worker_generation <= MAX_DIAGNOSTIC_COORDINATE
        ):
            raise ValueError(
                "worker_generation must be a bounded non-negative integer"
            )
        if self.worker_manifest_sha256 is not None and (
            type(self.worker_manifest_sha256) is not str
            or _SHA256_RE.fullmatch(self.worker_manifest_sha256) is None
        ):
            raise ValueError("worker_manifest_sha256 must be a lowercase sha256")

    @classmethod
    def from_wire(cls, value: object) -> "OperationExecutionProvenance":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("execution provenance must be a mapping")
        expected = {
            "visible_source_sha256",
            "executed_source_sha256",
            "source_map_sha256",
            "mode",
            "worker_generation",
            "worker_manifest_sha256",
        }
        if set(value) != expected:
            raise ValueError("execution provenance has an invalid wire shape")
        return cls(
            visible_source_sha256=value.get("visible_source_sha256"),  # type: ignore[arg-type]
            executed_source_sha256=value.get("executed_source_sha256"),  # type: ignore[arg-type]
            source_map_sha256=value.get("source_map_sha256"),  # type: ignore[arg-type]
            mode=value.get("mode"),  # type: ignore[arg-type]
            worker_generation=value.get("worker_generation"),  # type: ignore[arg-type]
            worker_manifest_sha256=value.get("worker_manifest_sha256"),  # type: ignore[arg-type]
        )
