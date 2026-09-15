from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import subprocess
import sys

import pytest

from onec_runtime.bsl import (
    DiagnosticStage,
    MappingConfidence,
    NormalizedDiagnostic,
    SourceArtifactKind,
    SourceSpan,
    SourceTransformBuilder,
    SourceUnitKind,
    SourceUnitRef,
    mapped_visible_source,
    source_sha256,
)
from onec_runtime.bsl.source_maps import MappedSource
from onec_runtime.bsl.diagnostics import (
    DiagnosticCoordinateSpace,
    DiagnosticTextSpan,
    ErrorTraceFrameOrigin,
    LoweredSourceLocation,
    PlatformDiagnosticLocation,
    VisibleSourceLocation,
    WorkerArtifactPlatformLocation,
    WorkerRuntimeFrameDiagnostic,
    _PrivatePlatformEvidence,
    parse_platform_diagnostic,
    remap_platform_diagnostic,
)
from onec_runtime.runtime_contracts import sanitize_normalized_diagnostic


def _one_line_mapped_source(source: str) -> MappedSource:
    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL,
        "contract-diagnostic",
        1,
        source_sha256(source),
    )
    visible = mapped_visible_source(source, unit)
    builder = SourceTransformBuilder(visible)
    builder.copy(SourceSpan(0, len(source)))
    return builder.build(SourceArtifactKind.EXECUTED_BSL)


def _valid_trace_diagnostic() -> NormalizedDiagnostic:
    source = "Результат = 1;"
    return remap_platform_diagnostic(
        parse_platform_diagnostic("{<Неизвестный модуль>(1,1)}: failure"),
        _one_line_mapped_source(source),
        stage=DiagnosticStage.EXECUTION,
    )


def _two_cause_trace_diagnostic() -> NormalizedDiagnostic:
    source = "Результат = 1;"
    return remap_platform_diagnostic(
        parse_platform_diagnostic(
            "outer\n"
            "{<Неизвестный модуль>(1,1)}: first\n"
            "по причине:\n"
            "inner\n"
            "{<Неизвестный модуль>(1,1)}: second"
        ),
        _one_line_mapped_source(source),
        stage=DiagnosticStage.EXECUTION,
    )


def _mutate_trace_for_contract_case(
    diagnostic: NormalizedDiagnostic,
    mutation: str,
) -> NormalizedDiagnostic:
    frame = diagnostic.frames[0]
    cause = diagnostic.causes[0]
    text = diagnostic.platform_diagnostic
    assert text is not None
    if mutation == "bad_frame_ordinal":
        return replace(diagnostic, frames=(replace(frame, ordinal=9),))
    if mutation == "bad_cause_reference":
        return replace(diagnostic, frames=(replace(frame, cause_ordinal=31),))
    if mutation == "oversized_frames":
        return replace(
            diagnostic,
            causes=(),
            frames=tuple(
                replace(frame, ordinal=index, cause_ordinal=None)
                for index in range(129)
            ),
        )
    if mutation == "oversized_causes":
        return replace(
            diagnostic,
            frames=(replace(frame, cause_ordinal=None),),
            causes=tuple(
                replace(cause, ordinal=index, frame_ordinals=())
                for index in range(33)
            ),
        )
    if mutation == "out_of_range_platform_coordinate":
        return replace(
            diagnostic,
            frames=(
                replace(
                    frame,
                    platform_location=replace(
                        frame.platform_location,
                        line=10_000_001,
                    ),
                ),
            ),
        )
    if mutation == "diagnostic_span_outside_text":
        return replace(
            diagnostic,
            frames=(
                replace(
                    frame,
                    block_span=DiagnosticTextSpan(0, len(text) + 1),
                ),
            ),
        )
    if mutation == "wrong_origin_enum":
        return replace(diagnostic, frames=(replace(frame, origin="native_module"),))
    if mutation == "invalid_worker_digest":
        return replace(diagnostic, frames=(replace(frame, artifact_sha256="bad"),))
    raise AssertionError(f"unknown contract mutation: {mutation}")


def test_sanitizer_uses_utf8_byte_bound_for_private_platform_evidence() -> None:
    accepted_text = "😀" * (64 * 1024 // len("😀".encode("utf-8")))
    rejected_text = accepted_text + "😀"

    def diagnostic(text: str) -> NormalizedDiagnostic:
        return NormalizedDiagnostic(
            "a" * 64,
            "untrusted summary",
            DiagnosticStage.EXECUTION,
            MappingConfidence.UNKNOWN,
            _platform_evidence=_PrivatePlatformEvidence(text),
            platform_diagnostic_sha256=sha256(text.encode("utf-8")).hexdigest(),
        )

    assert sanitize_normalized_diagnostic(diagnostic(accepted_text)) is not None
    assert sanitize_normalized_diagnostic(diagnostic(rejected_text)) is None


def test_sanitizer_accepts_maximum_bounded_error_trace() -> None:
    raw = "{<Неизвестный модуль>(1,1)}: " + "x" * (64 * 1024)
    parsed = parse_platform_diagnostic(raw)
    diagnostic = remap_platform_diagnostic(
        parsed,
        _one_line_mapped_source("Результат = 1;"),
        stage=DiagnosticStage.EXECUTION,
    )

    safe = sanitize_normalized_diagnostic(diagnostic)

    assert safe is not None
    assert safe.platform_diagnostic is not None
    assert len(safe.platform_diagnostic.encode("utf-8")) <= 64 * 1024
    assert safe.frames


@pytest.mark.parametrize(
    "mutation",
    (
        "bad_frame_ordinal",
        "bad_cause_reference",
        "oversized_frames",
        "oversized_causes",
        "out_of_range_platform_coordinate",
        "diagnostic_span_outside_text",
        "wrong_origin_enum",
        "invalid_worker_digest",
    ),
)
def test_sanitizer_rejects_malformed_nested_trace_without_raising(
    mutation: str,
) -> None:
    """Break caught: unvalidated trace fields cross a public contract boundary."""
    diagnostic = _valid_trace_diagnostic()
    malformed = _mutate_trace_for_contract_case(diagnostic, mutation)

    assert sanitize_normalized_diagnostic(malformed) is None


@pytest.mark.parametrize(
    "mutation",
    (
        "unknown_host_coordinate_space",
        "bogus_worker_locator_shape",
        "worker_origin_on_main_locator",
        "native_origin_on_main_locator",
        "line_only_zero",
    ),
)
def test_sanitizer_rejects_impossible_trace_locator_and_origin_pairs(
    mutation: str,
) -> None:
    """Break caught: parser-impossible locations are treated as normalized trace data."""
    diagnostic = _valid_trace_diagnostic()
    frame = diagnostic.frames[0]
    if mutation == "unknown_host_coordinate_space":
        malformed = replace(
            diagnostic,
            frames=(
                replace(
                    frame,
                    platform_location=replace(
                        frame.platform_location,
                        coordinate_space=DiagnosticCoordinateSpace.HOST_MODULE,
                    ),
                ),
            ),
        )
    elif mutation == "bogus_worker_locator_shape":
        malformed = replace(
            diagnostic,
            frames=(
                replace(
                    frame,
                    origin=ErrorTraceFrameOrigin.WORKER_ARTIFACT,
                    platform_location=PlatformDiagnosticLocation(
                        "Bad.bogus.Thing",
                        ("Bad", "bogus", "Thing"),
                        WorkerArtifactPlatformLocation("bogus"),
                        1,
                        1,
                        DiagnosticCoordinateSpace.HOST_MODULE,
                    ),
                ),
            ),
        )
    elif mutation == "worker_origin_on_main_locator":
        malformed = replace(
            diagnostic,
            frames=(
                replace(frame, origin=ErrorTraceFrameOrigin.WORKER_ARTIFACT),
            ),
        )
    elif mutation == "native_origin_on_main_locator":
        malformed = replace(
            diagnostic,
            frames=(replace(frame, origin=ErrorTraceFrameOrigin.NATIVE_MODULE),),
        )
    elif mutation == "line_only_zero":
        malformed = replace(
            diagnostic,
            frames=(
                replace(
                    frame,
                    platform_location=replace(
                        frame.platform_location,
                        line=0,
                        column=None,
                    ),
                    lowered_location=None,
                ),
            ),
        )
    else:
        raise AssertionError(f"unknown locator mutation: {mutation}")

    assert sanitize_normalized_diagnostic(malformed) is None


def test_sanitizer_preserves_two_coordinate_zero_trace_evidence() -> None:
    """Break caught: a legacy two-coordinate platform locator is over-rejected."""
    diagnostic = _valid_trace_diagnostic()
    frame = diagnostic.frames[0]

    safe = sanitize_normalized_diagnostic(
        replace(
            diagnostic,
            frames=(
                replace(
                    frame,
                    platform_location=replace(
                        frame.platform_location,
                        line=0,
                        column=0,
                    ),
                    lowered_location=None,
                ),
            ),
        )
    )

    assert safe is not None


@pytest.mark.parametrize(
    "mutation",
    ("missing_source", "line_span_outside_visible", "lowered_coordinate_mismatch"),
)
def test_sanitizer_rejects_unlinked_trace_mapping_fields(mutation: str) -> None:
    """Break caught: a trace mapping field can be detached from its locator."""
    diagnostic = _valid_trace_diagnostic()
    frame = diagnostic.frames[0]
    assert frame.source_unit is not None
    visible = VisibleSourceLocation(frame.source_unit, 1, 1, SourceSpan(0, 1))
    if mutation == "missing_source":
        malformed_frame = replace(
            frame,
            source_unit=None,
            visible_location=visible,
        )
    elif mutation == "line_span_outside_visible":
        malformed_frame = replace(
            frame,
            visible_location=visible,
            visible_line_span=SourceSpan(1, 2),
        )
    elif mutation == "lowered_coordinate_mismatch":
        malformed_frame = replace(
            frame,
            lowered_location=LoweredSourceLocation(
                2,
                1,
                0,
                SourceSpan(0, 1),
            ),
        )
    else:
        raise AssertionError(f"unknown mapping mutation: {mutation}")
    malformed = replace(diagnostic, frames=(malformed_frame,))

    assert sanitize_normalized_diagnostic(malformed) is None


def test_sanitizer_rejects_unlinked_legacy_worker_mapping_fields() -> None:
    """Break caught: Worker compatibility frames can carry detached mappings."""
    diagnostic = _valid_trace_diagnostic()
    frame = diagnostic.frames[0]
    assert frame.source_unit is not None
    visible = VisibleSourceLocation(frame.source_unit, 1, 1, SourceSpan(0, 1))
    worker = WorkerRuntimeFrameDiagnostic(
        "OnecRuntime_deadbeef_deadbeefdeadbeef",
        None,
        None,
        None,
        MappingConfidence.EXACT,
        None,
        visible,
        None,
        LoweredSourceLocation(1, 1, 0, SourceSpan(0, 1)),
    )

    assert sanitize_normalized_diagnostic(
        replace(diagnostic, worker_frames=(worker,))
    ) is None


@pytest.mark.parametrize("mutation", ("reordered", "overlapping", "frame_outside"))
def test_sanitizer_rejects_nonmonotonic_trace_text_topology(
    mutation: str,
) -> None:
    """Break caught: cause and frame ordinals no longer match retained text order."""
    diagnostic = _two_cause_trace_diagnostic()
    first_cause, second_cause = diagnostic.causes
    first_frame, second_frame = diagnostic.frames
    if mutation == "reordered":
        malformed = replace(
            diagnostic,
            causes=(
                replace(
                    first_cause,
                    block_span=second_cause.block_span,
                    summary_span=second_cause.summary_span,
                ),
                replace(
                    second_cause,
                    block_span=first_cause.block_span,
                    summary_span=first_cause.summary_span,
                ),
            ),
        )
    elif mutation == "overlapping":
        malformed = replace(
            diagnostic,
            causes=(
                first_cause,
                replace(
                    second_cause,
                    block_span=DiagnosticTextSpan(
                        first_cause.block_span.start + 1,
                        first_cause.block_span.end,
                    ),
                    summary_span=DiagnosticTextSpan(
                        first_cause.block_span.start + 1,
                        first_cause.block_span.end,
                    ),
                ),
            ),
        )
    elif mutation == "frame_outside":
        malformed = replace(
            diagnostic,
            frames=(
                first_frame,
                replace(
                    second_frame,
                    block_span=DiagnosticTextSpan(
                        first_frame.block_span.end + 1,
                        second_frame.block_span.start - 1,
                    ),
                    detail_span=None,
                ),
            ),
        )
    else:
        raise AssertionError(f"unknown topology mutation: {mutation}")

    assert sanitize_normalized_diagnostic(malformed) is None


def test_core_runtime_imports_without_agent_package() -> None:
    script = """
import builtins

real_import = builtins.__import__

def guarded(name, *args, **kwargs):
    if name == "onec_runtime_mcp.agent" or name.startswith("onec_runtime_mcp.agent."):
        raise AssertionError(name)
    return real_import(name, *args, **kwargs)

builtins.__import__ = guarded

import onec_runtime.runtime_api
import onec_runtime.privacy
import onec_runtime.session
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_runtime_session_imports_without_ipython() -> None:
    script = """
import builtins

real_import = builtins.__import__

def guarded(name, *args, **kwargs):
    if name == "IPython" or name.startswith("IPython."):
        raise AssertionError(name)
    return real_import(name, *args, **kwargs)

builtins.__import__ = guarded

import onec_runtime.session
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
