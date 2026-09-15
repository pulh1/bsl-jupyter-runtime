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
    DiagnosticTextSpan,
    ErrorTraceFrameOrigin,
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
