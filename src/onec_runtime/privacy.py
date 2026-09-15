from __future__ import annotations

from dataclasses import fields, is_dataclass
from hashlib import sha256
from typing import Any

from onec_runtime.runtime_contracts import (
    MAX_PRIVATE_DIAGNOSTIC_BYTES,
    sanitize_normalized_diagnostic,
)
from onec_runtime.bsl import NormalizedDiagnostic


def diagnostic_to_public_wire(value: NormalizedDiagnostic) -> dict[str, object]:
    """Render one canonical visible-only diagnostic or fail closed."""
    try:
        safe = sanitize_normalized_diagnostic(value)
        if safe is None:
            return {}
        visible_location = safe.visible_location
        related = safe.related_visible_span
        return {
            "diagnostic_id": safe.diagnostic_id,
            "runtime_summary": safe.runtime_summary,
            "stage": safe.stage.value,
            "mapping_confidence": safe.mapping_confidence.value,
            "visible_location": (
                None
                if visible_location is None
                else {
                    "line": visible_location.line,
                    "column": visible_location.column,
                    "span": _span_wire(visible_location.span),
                }
            ),
            "related_visible_span": (
                None if related is None else _span_wire(related)
            ),
            "excerpt": None,
            "synthetic_region": safe.synthetic_region,
        }
    except BaseException:
        return {}


def diagnostic_to_expert_wire(value: NormalizedDiagnostic) -> dict[str, object]:
    """Render the bounded expert allowlist without source or runtime identity."""
    try:
        safe = sanitize_normalized_diagnostic(value)
        public = diagnostic_to_public_wire(value)
        if safe is None or not public:
            return {}
        lowered = safe.lowered_location
        platform, platform_truncated, platform_redacted = (
            bounded_platform_diagnostic(
                safe.platform_diagnostic,
                truncated=safe.platform_diagnostic_truncated,
                redacted=safe.platform_diagnostic_redacted,
            )
        )
        return {
            **public,
            "lowered_location": (
                None
                if lowered is None
                else {
                    "line": lowered.line,
                    "column": lowered.column,
                    "offset": lowered.offset,
                    "span": _span_wire(lowered.span),
                }
            ),
            "platform_diagnostic": platform,
            "platform_diagnostic_sha256": safe.platform_diagnostic_sha256,
            "platform_diagnostic_truncated": platform_truncated,
            "platform_diagnostic_redacted": platform_redacted,
            "execution_artifact_sha256": safe.execution_artifact_sha256,
            "source_map_sha256": safe.source_map_sha256,
            # RuntimeApi can bind these exact nullable fields from its
            # pre-dispatch provenance; the diagnostic itself never invents it.
            "worker_generation": None,
            "worker_manifest_sha256": None,
        }
    except BaseException:
        return {}


def bounded_platform_diagnostic(
    value: str | None,
    *,
    truncated: bool,
    redacted: bool,
) -> tuple[str | None, bool, bool]:
    """Byte-bound expert prose while preserving caller redaction provenance."""
    if type(truncated) is not bool or type(redacted) is not bool:
        return None, True, True
    if value is None:
        return None, truncated, redacted
    if type(value) is not str:
        return None, True, True
    encoded = value.encode("utf-8")
    bounded = encoded[:MAX_PRIVATE_DIAGNOSTIC_BYTES].decode(
        "utf-8", errors="ignore"
    )
    return (
        bounded,
        truncated or len(encoded) > MAX_PRIVATE_DIAGNOSTIC_BYTES,
        redacted,
    )


def _span_wire(value: object) -> dict[str, object]:
    return {
        "start": getattr(value, "start"),
        "end": getattr(value, "end"),
    }


def public_artifact_value(value: Any) -> Any:
    """Recursively redact reversible BSL sources from automatic evidence paths."""
    kind = type(value).__name__
    if kind == "ModuleLocation":
        return {
            "module_type": value.module_type,
            "line": value.line,
            "extension_name": value.extension_name,
        }
    if kind == "WorkerGenerationDebugView":
        return {
            "handle": public_artifact_value(value.handle),
            "module_count": len(value.modules),
        }
    if kind == "WorkerModuleDebugView":
        return {
            "source_unit": public_artifact_value(value.source_unit),
            "canonical_module": value.canonical_module,
            "artifact_sha256": value.artifact_sha256,
            "source_map_sha256": value.source_map_sha256,
        }
    if kind == "WorkerBreakpointPlan":
        return {
            "proposal_id": value.proposal_id,
            "expected_catalog_version": value.expected_catalog_version,
            "result_id": value.result_id,
            "desired_slot_count": len(value.desired_slots),
            "removed_ids": public_artifact_value(value.removed_ids),
            "next_snapshot": public_artifact_value(value.next_snapshot),
        }
    if kind == "WorkerBreakpointBinding":
        return {
            "breakpoint_id": value.breakpoint_id,
            "generation": public_artifact_value(value.generation),
            "location": public_artifact_value(value.location),
        }
    if kind == "WorkerFrameProvenance":
        return "<redacted worker frame provenance>"
    if kind == "WorkerArtifact":
        return {
            "logical_name": value.logical_name,
            "source_sha256": value.source_sha256,
            "source_map_sha256": value.source_map_sha256,
            "artifact_sha256": value.artifact_sha256,
            "source_provenance": public_artifact_value(value.source_provenance),
            "exports": public_artifact_value(value.exports),
        }
    if kind == "OperationHandle":
        return {
            "operation_id": value.operation_id,
            "visible_source_sha256": _source_sha256(value.visible_source),
            "lowered_source_sha256": _source_sha256(value.lowered_source),
            "messages_intercepted": value.messages_intercepted,
        }
    if kind == "CaptureCellResult":
        return {
            "operation_id": value.operation_id,
            "visible_source_sha256": _source_sha256(value.visible_source),
            "lowered_source_sha256": _source_sha256(value.lowered_source),
            "result": public_artifact_value(value.result),
            "messages": public_artifact_value(value.messages),
        }
    if kind == "NotebookCell":
        return {
            "worker_source_sha256": _source_sha256(value.worker_source),
            "statement_source_sha256": _source_sha256(value.statement_source),
            "exports": public_artifact_value(value.exports),
        }
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: public_artifact_value(getattr(value, item.name))
            for item in fields(value)
        }
    if isinstance(value, dict):
        return _public_mapping(value)
    if isinstance(value, (tuple, list, set, frozenset)):
        return [public_artifact_value(item) for item in value]
    return value


def _source_sha256(source: str) -> str:
    return sha256(source.encode("utf-8")).hexdigest()


def _public_mapping(value: dict[Any, Any]) -> dict[str, Any]:
    result = {str(key): public_artifact_value(item) for key, item in value.items()}
    for field_name in (
        "visible_source",
        "lowered_source",
        "worker_source",
        "statement_source",
    ):
        source = result.pop(field_name, None)
        if isinstance(source, str):
            result[f"{field_name}_sha256"] = _source_sha256(source)
    return result
