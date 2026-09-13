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
    SourceUnitRef,
    VisibleSourceLocation,
)


MAX_DIAGNOSTIC_COORDINATE = 10_000_000
MAX_DIAGNOSTIC_LABEL_LENGTH = 128
MAX_PRIVATE_DIAGNOSTIC_LENGTH = 4_096

_DIAGNOSTIC_ID_RE = re.compile(r"[0-9a-f]{64}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_DIAGNOSTIC_LABEL_RE = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")
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
            not isinstance(value.source_unit, SourceUnitRef)
            or len(value.source_unit.unit_id) > 256
        ):
            return None
        if value.visible_location is not None:
            location = value.visible_location
            if (
                not isinstance(location, VisibleSourceLocation)
                or not _bounded_positive_coordinate(location.line)
                or not _bounded_positive_coordinate(location.column)
                or not _bounded_span(location.span)
                or len(location.source_unit.unit_id) > 256
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
        if value.lowered_location is not None:
            location = value.lowered_location
            if (
                not _bounded_positive_coordinate(location.line)
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
            or len(platform_diagnostic) > MAX_PRIVATE_DIAGNOSTIC_LENGTH
            or value.platform_diagnostic_sha256 is None
        ):
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
