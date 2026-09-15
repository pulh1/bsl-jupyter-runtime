from __future__ import annotations

from dataclasses import fields, is_dataclass
from hashlib import sha256
import re
from typing import Any

from onec_runtime.runtime_contracts import sanitize_normalized_diagnostic
from onec_runtime.bsl import NormalizedDiagnostic


_PLATFORM_DIAGNOSTIC_LIMIT = 4_096
_SENSITIVE_DIAGNOSTIC_KEY = (
    r"(?:"
    r"[0-9A-Za-z_.-]*"
    r"(?:password|passwd|passphrase|secret|token|authorization|credential(?:s)?)"
    r"(?:[_.-]?(?:id|key|value|header))?|"
    r"rdbg[_\-.]?(?:session|connection|client|server|process|subject|"
    r"target|object|property|seance)"
    r"(?:[_\-.]?(?:pid|id|key))?|"
    r"rdbg[0-9A-Za-z_.-]*(?:pid|id|key)|"
    r"(?:process|session|connection|client|server|subject|runtime|worker)"
    r"[_\-.]?(?:pid|id|token|key|secret|credential(?:s)?)|"
    r"(?:process|session|connection|client|server|subject|runtime|worker)"
    r"[ \t]+(?:pid|id|token|key|secret|credential(?:s)?)|"
    r"(?:access|refresh|auth|api)[_\-.]?(?:key|secret|credential|header)|"
    r"pid"
    r")"
)
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    rf"""
    (?P<head>
        (?:
            ["']{_SENSITIVE_DIAGNOSTIC_KEY}["']
            |
            (?<![0-9A-Za-z_]){_SENSITIVE_DIAGNOSTIC_KEY}(?![0-9A-Za-z_])
        )
        (?:[ \t]*(?:=|:)[ \t]*|[ \t]+)
    )
    (?P<value>
        "(?:\\.|[^"\\])*"
        |'(?:\\.|[^'\\])*'
        |[^\r\n,;}}\]]+
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)
_AUTHORIZATION_RE = re.compile(
    r"\b(?:bearer|basic)\s+[^\s,;}}\]]+",
    re.IGNORECASE,
)


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
    """Bound and redact credential/process identities in expert-only prose."""
    if type(truncated) is not bool or type(redacted) is not bool:
        return None, True, True
    if value is None:
        return None, truncated, redacted
    if type(value) is not str:
        return None, True, True
    bounded = value[:_PLATFORM_DIAGNOSTIC_LIMIT]
    input_truncated = len(value) > _PLATFORM_DIAGNOSTIC_LIMIT
    bounded, assignment_count = _SENSITIVE_ASSIGNMENT_RE.subn(
        lambda match: f"{match.group('head')}<redacted>",
        bounded,
    )
    bounded, authorization_count = _AUTHORIZATION_RE.subn(
        "<redacted credential>",
        bounded,
    )
    expansion_truncated = len(bounded) > _PLATFORM_DIAGNOSTIC_LIMIT
    if len(bounded) > _PLATFORM_DIAGNOSTIC_LIMIT:
        bounded = bounded[:_PLATFORM_DIAGNOSTIC_LIMIT]
    return (
        bounded,
        truncated or input_truncated or expansion_truncated,
        redacted or assignment_count > 0 or authorization_count > 0,
    )


def _span_wire(value: object) -> dict[str, object]:
    return {
        "start": getattr(value, "start"),
        "end": getattr(value, "end"),
    }


def public_artifact_value(value: Any) -> Any:
    """Recursively redact reversible BSL sources from automatic evidence paths."""
    kind = type(value).__name__
    if kind == "CaptureView":
        # A live control-plane capability is not a saved status snapshot.
        return {"type": "CaptureView"}
    if kind == "ValueNode":
        return {
            "name": value.name,
            "type_name": value.type_name,
            "preview": value.preview,
            "size": value.size,
            "expandable": value.expandable,
            "shape": public_artifact_value(value.shape),
            "path": public_artifact_value(value.path),
            "private": value.private,
            "cycle": value.cycle,
        }
    if kind == "ValuePage":
        return {
            "items": public_artifact_value(value.items),
            "total": value.total,
            "next_cursor": value.next_cursor,
            "path": public_artifact_value(value.path),
            "view": value.view,
            "start": value.start,
            "stop": value.stop,
        }
    if kind == "SafeValuePath":
        return {
            "root": public_artifact_value(value.root),
            "segments": public_artifact_value(value.segments),
        }
    if kind == "ValueRoot":
        return {"kind": value.kind, "native_level": value.native_level}
    if kind == "SafePathSegment":
        return {"kind": value.kind, "key": value.key}
    if kind == "PrivateProjectedValue":
        return "<private capture projection>"
    if kind == "DebugFrame":
        method = value.method
        return {
            "native_level": value.native_level,
            "visible_index": value.visible_index,
            "source": value.source,
            "line": value.line,
            "source_status": value.source_status,
            "detail": value.detail,
            "method": (
                None
                if method is None
                else {
                    "name": method.name,
                    "parameters": public_artifact_value(method.parameters),
                    "start_line": method.start_line,
                    "end_line": method.end_line,
                }
            ),
            "method_status": value.method_status,
            "method_reason": value.method_reason,
            "runtime_kernel": value.runtime_kernel,
            "source_sha256": value.source_sha256,
        }
    if kind == "RuntimeFrameMarker":
        return {"count": value.count}
    if kind == "StackPage":
        return {
            "frames": public_artifact_value(value.frames),
            "total": value.total,
            "next_cursor": value.next_cursor,
            "detail": value.detail,
            "native": value.native,
        }
    if kind == "CaptureContextView":
        return {"root": public_artifact_value(value._root)}
    if kind == "VariableDescriptor":
        return {
            "root": public_artifact_value(value._root),
            "role": value._role,
        }
    if kind == "ChildValueDescriptor":
        return {
            "path": public_artifact_value(value._node.path),
            "alias": value._alias,
        }
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
