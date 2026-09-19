"""Human-only notebook rendering of validated 1C error evidence."""

from __future__ import annotations

from pathlib import Path

from onec_runtime.bsl import SourceUnitRef, source_sha256
from onec_runtime.bsl.diagnostics import (
    DiagnosticTextSpan,
    ErrorTraceFrame,
    ErrorTraceFrameOrigin,
    MappingConfidence,
    NormalizedDiagnostic,
)
from onec_runtime.runtime_contracts import sanitize_normalized_diagnostic

from .diagnostic_sources import DiagnosticSourceFiles


def _span_text(text: str, span: DiagnosticTextSpan | None) -> str | None:
    if span is None or not 0 <= span.start <= span.end <= len(text):
        return None
    value = text[span.start:span.end].strip()
    return value or None


def _frame_heading(frame: ErrorTraceFrame) -> str:
    visible = frame.visible_location
    if visible is not None and frame.mapping_confidence is MappingConfidence.EXACT:
        label = (
            "Ячейка BSL"
            if frame.origin is ErrorTraceFrameOrigin.EXECUTED_ARTIFACT
            else frame.logical_name or visible.source_unit.unit_id
        )
        return f"{label} (строка {visible.line}, колонка {visible.column})"
    platform = frame.platform_location
    label = frame.logical_name or platform.module_name or "Исполняемый код 1С"
    if frame.origin is ErrorTraceFrameOrigin.EXECUTED_ARTIFACT:
        label = "Ячейка BSL — сгенерированный код"
    elif (
        frame.origin is ErrorTraceFrameOrigin.NATIVE_MODULE
        and platform.extension_name is not None
    ):
        label = f"{platform.extension_name}: {label}"
    coordinate = f"строка {platform.line}"
    if platform.column is not None:
        coordinate += f", колонка {platform.column}"
    return f"{label} ({coordinate})"


def _original_cell_line(
    frame: ErrorTraceFrame,
    visible_source: str | None,
    source_unit: SourceUnitRef | None,
    source_matches: bool,
) -> str | None:
    if (
        frame.origin is not ErrorTraceFrameOrigin.EXECUTED_ARTIFACT
        or frame.mapping_confidence is not MappingConfidence.EXACT
        or frame.source_unit != source_unit
        or not isinstance(source_unit, SourceUnitRef)
        or not source_matches
        or type(visible_source) is not str
    ):
        return None
    span = frame.visible_line_span
    if span is None or not 0 <= span.start <= span.end <= len(visible_source):
        return None
    return visible_source[span.start:span.end].strip() or None


def _related_cell_line(
    frame: ErrorTraceFrame,
    visible_source: str | None,
    source_unit: SourceUnitRef | None,
    source_matches: bool,
) -> int | None:
    if (
        frame.origin is not ErrorTraceFrameOrigin.EXECUTED_ARTIFACT
        or frame.mapping_confidence is not MappingConfidence.NEAREST
        or frame.source_unit != source_unit
        or not source_matches
        or type(visible_source) is not str
    ):
        return None
    span = frame.related_visible_span
    if span is None or not 0 <= span.start <= span.end <= len(visible_source):
        return None
    return visible_source.count("\n", 0, span.start) + 1


def render_error_trace(
    diagnostic: NormalizedDiagnostic | None,
    *,
    heading: str,
    visible_source: str | None,
    source_unit: SourceUnitRef | None,
    source_root: Path | str | None = None,
) -> str | None:
    """Render bounded postmortem evidence and optional verified file hints."""

    safe = sanitize_normalized_diagnostic(diagnostic)
    if safe is None or safe.platform_diagnostic is None:
        return None
    evidence = safe.platform_diagnostic
    source_matches = (
        isinstance(source_unit, SourceUnitRef)
        and type(visible_source) is str
        and source_sha256(visible_source) == source_unit.source_sha256
    )
    lines = [heading]
    source_files = None
    if source_root is not None:
        try:
            source_files = DiagnosticSourceFiles(source_root)
        except BaseException:
            pass
    reasons = tuple(
        reason
        for cause in safe.causes
        if (reason := _span_text(evidence, cause.summary_span)) is not None
    )
    if reasons:
        lines.append("Причины:")
        lines.extend(
            f"  {index}. {reason}"
            for index, reason in enumerate(reasons, 1)
        )
    if safe.causes_truncated:
        lines.append("  … цепочка причин обрезана")
    if safe.frames:
        lines.append("Стек (1С):")
        for index, frame in enumerate(safe.frames, 1):
            lines.append(f"  {index}. {_frame_heading(frame)}")
            detail = _span_text(evidence, frame.detail_span)
            hint = None
            local_line = None
            if source_files is not None:
                try:
                    hint = source_files.hint(frame)
                    local_line = source_files.line_excerpt(frame, detail)
                except BaseException:
                    hint = None
                    local_line = None
            if hint is not None:
                lines.append(f"     файл: {hint} (локальный экспорт)")
            related_line = _related_cell_line(
                frame, visible_source, source_unit, source_matches,
            )
            if related_line is not None:
                lines.append(
                    f"     ориентир: ячейка BSL, строка {related_line} "
                    "(приблизительно)"
                )
            original = _original_cell_line(
                frame, visible_source, source_unit, source_matches,
            )
            if original is not None:
                lines.append(f"     исходник: {original}")
            if local_line is not None:
                lines.append(f"     локальный экспорт: {local_line}")
            if detail is not None:
                lines.append(f"     фрагмент 1С: {detail}")
    if safe.frames_truncated:
        lines.append("  … стек обрезан")
    if safe.platform_diagnostic_truncated:
        lines.append("  … сообщение 1С обрезано; исходный стек может быть длиннее")
    if evidence.strip():
        lines.extend(("Исходное сообщение 1С:", evidence))
    return "\n".join(lines) if len(lines) > 1 else None
