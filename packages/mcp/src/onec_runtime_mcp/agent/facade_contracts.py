"""Wire-safe contracts for the compact agent-facing application facade."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
import re
from types import MappingProxyType

from onec_runtime_mcp.agent.contracts import (
    AgentOperationState,
    JSONValue,
)
from onec_runtime_mcp.agent.capture_contracts import CaptureView
from onec_runtime_mcp.agent.proxies import (
    MeasurementCost,
    ProxyConsistency,
    ProxyDescriptor,
    ProxyFence,
    ProxyLifetime,
    ProxyProvenance,
    ProxyRealm,
    SizeAccuracy,
    ValuePreview,
    ValueSize,
)
from onec_runtime.runtime_contracts import (
    MAX_DIAGNOSTIC_COORDINATE,
    OperationExecutionProvenance,
)


class AgentOperationKind(StrEnum):
    RUNTIME_ENSURE = "runtime_ensure"
    CODE_RUN = "code_run"
    CODE_RUN_INLINE = "code_run_inline"
    PYTHON_RUN = "python_run"
    CAPTURE_RUN_UNTIL = "capture_run_until"
    CAPTURE_HYPOTHESIS = "capture_hypothesis"
    CAPTURE_CONTINUE = "capture_continue"


class MutationConfidence(StrEnum):
    EXACT = "exact"
    DECLARED = "declared"
    INFERRED = "inferred"
    UNKNOWN = "unknown"


MAX_DIAGNOSTIC_EXCERPT_LENGTH = 512
_DIAGNOSTIC_ID_RE = re.compile(r"[0-9a-f]{64}\Z")
_DIAGNOSTIC_LABEL_RE = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")
_DIAGNOSTIC_STAGES = frozenset(
    {"parsing", "lowering", "compilation", "execution"}
)
_MAPPING_CONFIDENCES = frozenset(
    {"exact", "nearest", "synthetic", "unknown"}
)
_FAILURE_FIELDS = frozenset(
    {
        "stage",
        "partial_results",
        "continue_state",
        "state_changed",
        "diagnostic",
    }
)
_PARTIAL_RESULT_STATES = frozenset(
    {
        "unattempted",
        "failed",
        "sent",
        "succeeded",
        "outcome_unknown",
        "unknown",
        "unavailable",
        "captured",
        "completed",
        "stale",
    }
)
_CONTINUE_STATES = frozenset(
    {"unattempted", "planned", "sent", "acknowledged", "outcome_unknown"}
)
_STATE_CHANGED_VALUES = frozenset({"no", "yes", "partial", "unknown"})


def _text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _optional_positive(value: object, *, name: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _strings(value: object, *, name: str) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, (tuple, list)):
        raise TypeError(f"{name} must be a sequence")
    result = tuple(value)
    if any(not isinstance(item, str) or not item for item in result):
        raise ValueError(f"{name} must contain non-empty strings")
    return result


def _string_map(value: object, *, name: str) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key or not isinstance(item, str) or not item:
            raise ValueError(f"{name} must map non-empty strings")
        result[key] = item
    return MappingProxyType(result)


def _json_map(value: object, *, name: str) -> Mapping[str, JSONValue]:
    from onec_runtime_mcp.agent.contracts import to_wire

    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping")
    wire = to_wire(value)
    if not isinstance(wire, dict):
        raise TypeError(f"{name} must be a mapping")
    return _freeze_json_mapping(wire)


def _freeze_json_mapping(value: Mapping[str, object]) -> Mapping[str, JSONValue]:
    return MappingProxyType(
        {key: _freeze_json_value(item) for key, item in value.items()}
    )  # type: ignore[return-value]


def _freeze_json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return _freeze_json_mapping(value)
    if isinstance(value, (tuple, list)):
        return tuple(_freeze_json_value(item) for item in value)
    return value


def _diagnostic_span(value: object, *, name: str) -> Mapping[str, JSONValue]:
    if not isinstance(value, Mapping) or set(value) != {"start", "end"}:
        raise ValueError(f"{name} must be an exact span")
    start = value.get("start")
    end = value.get("end")
    if (
        type(start) is not int
        or type(end) is not int
        or not 0 <= start <= end <= MAX_DIAGNOSTIC_COORDINATE
    ):
        raise ValueError(f"{name} must be bounded")
    return MappingProxyType({"start": start, "end": end})


def _diagnostic_location(value: object) -> Mapping[str, JSONValue]:
    if not isinstance(value, Mapping) or set(value) != {
        "line",
        "column",
        "span",
    }:
        raise ValueError("visible_location must have an exact wire shape")
    line = value.get("line")
    column = value.get("column")
    if (
        type(line) is not int
        or type(column) is not int
        or not 1 <= line <= MAX_DIAGNOSTIC_COORDINATE
        or not 1 <= column <= MAX_DIAGNOSTIC_COORDINATE
    ):
        raise ValueError("visible_location coordinates must be bounded")
    return MappingProxyType(
        {
            "line": line,
            "column": column,
            "span": _diagnostic_span(value.get("span"), name="visible span"),
        }
    )


@dataclass(frozen=True, slots=True)
class AgentDiagnosticView:
    diagnostic_id: str
    stage: str
    mapping_confidence: str
    visible_location: Mapping[str, JSONValue] | None
    related_visible_span: Mapping[str, JSONValue] | None
    excerpt: str | None
    synthetic_region: str | None

    def __post_init__(self) -> None:
        if (
            type(self.diagnostic_id) is not str
            or _DIAGNOSTIC_ID_RE.fullmatch(self.diagnostic_id) is None
        ):
            raise ValueError("diagnostic_id must be a lowercase sha256")
        if type(self.stage) is not str or self.stage not in _DIAGNOSTIC_STAGES:
            raise ValueError("stage is not allowlisted")
        if (
            type(self.mapping_confidence) is not str
            or self.mapping_confidence not in _MAPPING_CONFIDENCES
        ):
            raise ValueError("mapping_confidence is not allowlisted")
        if self.visible_location is not None:
            object.__setattr__(
                self,
                "visible_location",
                _diagnostic_location(self.visible_location),
            )
        if self.related_visible_span is not None:
            object.__setattr__(
                self,
                "related_visible_span",
                _diagnostic_span(
                    self.related_visible_span,
                    name="related_visible_span",
                ),
            )
        if self.excerpt is not None and (
            type(self.excerpt) is not str
            or len(self.excerpt) > MAX_DIAGNOSTIC_EXCERPT_LENGTH
        ):
            raise ValueError("excerpt must be a bounded string")
        if self.synthetic_region is not None and (
            type(self.synthetic_region) is not str
            or _DIAGNOSTIC_LABEL_RE.fullmatch(self.synthetic_region) is None
        ):
            raise ValueError("synthetic_region is not allowlisted")

    @classmethod
    def from_wire(cls, value: object) -> "AgentDiagnosticView":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("diagnostic view must be a mapping")
        expected = {
            "diagnostic_id",
            "stage",
            "mapping_confidence",
            "visible_location",
            "related_visible_span",
            "excerpt",
            "synthetic_region",
        }
        if set(value) != expected:
            raise ValueError("diagnostic view has an invalid wire shape")
        return cls(
            diagnostic_id=value.get("diagnostic_id"),  # type: ignore[arg-type]
            stage=value.get("stage"),  # type: ignore[arg-type]
            mapping_confidence=value.get("mapping_confidence"),  # type: ignore[arg-type]
            visible_location=value.get("visible_location"),  # type: ignore[arg-type]
            related_visible_span=value.get("related_visible_span"),  # type: ignore[arg-type]
            excerpt=value.get("excerpt"),  # type: ignore[arg-type]
            synthetic_region=value.get("synthetic_region"),  # type: ignore[arg-type]
        )


def _failure_map(
    value: object,
    *,
    allow_excerpt: bool,
) -> Mapping[str, JSONValue]:
    result = dict(_json_map(value, name="failure"))
    if set(result) - _FAILURE_FIELDS:
        raise ValueError("failure contains private or unsupported fields")
    stage = result.get("stage")
    if (
        type(stage) is not str
        or _DIAGNOSTIC_LABEL_RE.fullmatch(stage) is None
    ):
        raise ValueError("failure stage must be a bounded identifier")
    partial = result.get("partial_results", {})
    if not isinstance(partial, Mapping) or len(partial) > 100:
        raise ValueError("partial_results must be a bounded state mapping")
    normalized_partial: dict[str, str] = {}
    normalized_keys: set[str] = set()
    for key, state in partial.items():
        if (
            type(key) is not str
            or not key.isidentifier()
            or len(key) > 256
            or type(state) is not str
            or state not in _PARTIAL_RESULT_STATES
        ):
            raise ValueError("partial_results must contain bounded state facts")
        normalized_key = key.casefold()
        if normalized_key in normalized_keys:
            raise ValueError("partial_results identifiers must be unique")
        normalized_partial[key] = state
        normalized_keys.add(normalized_key)
    result["partial_results"] = normalized_partial
    continue_state = result.get("continue_state")
    if continue_state is not None and (
        type(continue_state) is not str
        or continue_state not in _CONTINUE_STATES
    ):
        raise ValueError("continue_state is not allowlisted")
    state_changed = result.get("state_changed")
    if state_changed is not None and (
        type(state_changed) is not str
        or state_changed not in _STATE_CHANGED_VALUES
    ):
        raise ValueError("state_changed is not allowlisted")
    raw_diagnostic = result.get("diagnostic")
    if raw_diagnostic is not None:
        diagnostic = AgentDiagnosticView.from_wire(raw_diagnostic)
        if not allow_excerpt and diagnostic.excerpt is not None:
            raise ValueError("public operation facts cannot persist excerpts")
        from onec_runtime_mcp.agent.contracts import to_wire

        result["diagnostic"] = to_wire(diagnostic)
    return _freeze_json_mapping(result)


@dataclass(frozen=True, slots=True)
class RecoveryAction:
    method: str
    arguments: Mapping[str, JSONValue] = MappingProxyType({})

    def __post_init__(self) -> None:
        _text(self.method, name="method")
        object.__setattr__(self, "arguments", _json_map(self.arguments, name="arguments"))

    @classmethod
    def from_wire(cls, value: object) -> "RecoveryAction":
        if not isinstance(value, Mapping):
            raise TypeError("recovery action must be a mapping")
        return cls(
            method=_text(value.get("method"), name="method"),
            arguments=value.get("arguments", {}),
        )


@dataclass(frozen=True, slots=True)
class AgentOperationIdentity:
    operation_id: str
    kind: AgentOperationKind
    runtime_id: str = ""
    runtime_generation: int | None = None
    cell_id: str | None = None
    revision: int | None = None
    source_sha256: str | None = None

    def __post_init__(self) -> None:
        _text(self.operation_id, name="operation_id")
        if not isinstance(self.kind, AgentOperationKind):
            raise TypeError("kind must be an AgentOperationKind")
        if self.runtime_id:
            _text(self.runtime_id, name="runtime_id")
        _optional_positive(self.runtime_generation, name="runtime_generation")
        if self.cell_id is not None:
            _text(self.cell_id, name="cell_id")
        _optional_positive(self.revision, name="revision")
        if self.source_sha256 is not None:
            if len(_text(self.source_sha256, name="source_sha256")) != 64:
                raise ValueError("source_sha256 must contain 64 characters")


@dataclass(frozen=True, slots=True)
class OperationTruncation:
    messages: bool = False
    changed_variables: bool = False
    outputs: bool = False

    def __post_init__(self) -> None:
        if any(type(value) is not bool for value in (
            self.messages,
            self.changed_variables,
            self.outputs,
        )):
            raise TypeError("truncation fields must be bool")


@dataclass(frozen=True, slots=True)
class OperationViewFacts:
    changed_variables: tuple[ProxyDescriptor, ...] = ()
    change_confidence: MutationConfidence = MutationConfidence.UNKNOWN
    outputs: Mapping[str, ProxyDescriptor] = MappingProxyType({})
    capture: CaptureView | None = None
    failure: Mapping[str, JSONValue] | None = None
    recovery: tuple[RecoveryAction, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.change_confidence, MutationConfidence):
            raise TypeError("change_confidence must be a MutationConfidence")
        object.__setattr__(
            self,
            "changed_variables",
            tuple(self.changed_variables),
        )
        if any(not isinstance(item, ProxyDescriptor) for item in self.changed_variables):
            raise TypeError("changed_variables must contain ProxyDescriptor values")
        if len({item.proxy_id for item in self.changed_variables}) != len(self.changed_variables):
            raise ValueError("changed_variables must be unique")
        if not isinstance(self.outputs, Mapping) or any(
            not isinstance(alias, str)
            or not alias
            or not isinstance(item, ProxyDescriptor)
            for alias, item in self.outputs.items()
        ):
            raise TypeError("outputs must map aliases to ProxyDescriptor values")
        object.__setattr__(self, "outputs", MappingProxyType(dict(self.outputs)))
        if self.capture is not None and not isinstance(self.capture, CaptureView):
            raise TypeError("capture must be a CaptureView")
        if self.failure is not None:
            object.__setattr__(
                self,
                "failure",
                _failure_map(self.failure, allow_excerpt=False),
            )
        if isinstance(self.recovery, list):
            object.__setattr__(self, "recovery", tuple(self.recovery))
        if not isinstance(self.recovery, tuple) or any(
            not isinstance(action, RecoveryAction) for action in self.recovery
        ):
            raise TypeError("recovery must contain RecoveryAction values")

    @classmethod
    def from_wire(cls, value: object) -> "OperationViewFacts":
        if not isinstance(value, Mapping):
            raise TypeError("operation view facts must be a mapping")
        changed = value.get("changed_variables", ())
        outputs = value.get("outputs", {})
        if isinstance(changed, str) or not isinstance(changed, (tuple, list)):
            raise TypeError("changed_variables must be a sequence")
        if not isinstance(outputs, Mapping):
            raise TypeError("outputs must be a mapping")
        recovery = value.get("recovery", ())
        if isinstance(recovery, str) or not isinstance(recovery, (tuple, list)):
            raise TypeError("recovery must be a sequence")
        capture = value.get("capture")
        return cls(
            changed_variables=tuple(
                proxy_descriptor_from_wire(item)
                for item in changed
            ),
            change_confidence=MutationConfidence(
                value.get("change_confidence", "unknown")
            ),
            outputs={
                alias: proxy_descriptor_from_wire(item)  # type: ignore[misc]
                for alias, item in outputs.items()
            },
            capture=None
            if capture is None
            else capture
            if isinstance(capture, CaptureView)
            else CaptureView.from_wire(capture),
            failure=value.get("failure"),
            recovery=tuple(RecoveryAction.from_wire(item) for item in recovery),
        )


def proxy_descriptor_from_wire(value: object) -> ProxyDescriptor:
    if isinstance(value, ProxyDescriptor):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("proxy descriptor must be a mapping")
    fence = value.get("fence")
    provenance = value.get("provenance")
    if not isinstance(fence, Mapping) or not isinstance(provenance, Mapping):
        raise TypeError("proxy descriptor requires fence and provenance")
    known = value.get("known_size")
    preview = value.get("bounded_preview")
    return ProxyDescriptor(
        proxy_id=value.get("proxy_id"),  # type: ignore[arg-type]
        realm=ProxyRealm(value.get("realm")),  # type: ignore[arg-type]
        lifetime=ProxyLifetime(value.get("lifetime")),  # type: ignore[arg-type]
        qualified_name=value.get("qualified_name"),  # type: ignore[arg-type]
        type_name=value.get("type_name"),  # type: ignore[arg-type]
        version=value.get("version"),  # type: ignore[arg-type]
        consistency=ProxyConsistency(value.get("consistency")),  # type: ignore[arg-type]
        fence=ProxyFence.from_wire(fence),
        provenance=ProxyProvenance(
            cell_id=provenance.get("cell_id"),  # type: ignore[arg-type]
            revision=provenance.get("revision"),  # type: ignore[arg-type]
            source_sha256=provenance.get("source_sha256"),  # type: ignore[arg-type]
            operation_id=provenance.get("operation_id"),  # type: ignore[arg-type]
            parent_proxy_ids=tuple(provenance.get("parent_proxy_ids", ())),  # type: ignore[arg-type]
        ),
        capabilities=tuple(value.get("capabilities", ())),  # type: ignore[arg-type]
        known_size=None if known is None else _value_size_from_wire(known),
        bounded_preview=None if preview is None else _value_preview_from_wire(preview),
    )


def _value_size_from_wire(value: object) -> ValueSize:
    if not isinstance(value, Mapping):
        raise TypeError("value size must be a mapping")
    return ValueSize(
        items=value.get("items"),  # type: ignore[arg-type]
        rows=value.get("rows"),  # type: ignore[arg-type]
        bytes=value.get("bytes"),  # type: ignore[arg-type]
        accuracy=SizeAccuracy(value.get("accuracy", "unknown")),  # type: ignore[arg-type]
        cost=MeasurementCost(value.get("cost", "cheap")),  # type: ignore[arg-type]
    )


def _value_preview_from_wire(value: object) -> ValuePreview:
    if not isinstance(value, Mapping):
        raise TypeError("value preview must be a mapping")
    return ValuePreview(
        type_name=value.get("type_name"),  # type: ignore[arg-type]
        scalar=value.get("scalar"),  # type: ignore[arg-type]
        sample=tuple(value.get("sample", ())),  # type: ignore[arg-type]
        truncated=value.get("truncated", False),  # type: ignore[arg-type]
    )


@dataclass(frozen=True, slots=True)
class OperationViewSnapshot:
    operation: object
    kind: AgentOperationKind
    facts: OperationViewFacts
    execution_provenance: OperationExecutionProvenance | None = None


@dataclass(frozen=True, slots=True)
class AgentOperationView:
    operation: AgentOperationIdentity
    state: AgentOperationState
    messages: tuple[str, ...]
    next_message_cursor: int
    next_event_cursor: int
    changed_variables: tuple[ProxyDescriptor, ...]
    change_confidence: MutationConfidence
    outputs: Mapping[str, ProxyDescriptor]
    capture: CaptureView | None
    failure: Mapping[str, JSONValue] | None
    recovery: tuple[RecoveryAction, ...]
    truncation: OperationTruncation
    execution_provenance: OperationExecutionProvenance | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.operation, AgentOperationIdentity):
            raise TypeError("operation must be an AgentOperationIdentity")
        if not isinstance(self.state, AgentOperationState):
            raise TypeError("state must be an AgentOperationState")
        object.__setattr__(self, "messages", _strings(self.messages, name="messages"))
        if type(self.next_message_cursor) is not int or self.next_message_cursor < 0:
            raise ValueError("next_message_cursor must be non-negative")
        if type(self.next_event_cursor) is not int or self.next_event_cursor < 0:
            raise ValueError("next_event_cursor must be non-negative")
        if not isinstance(self.change_confidence, MutationConfidence):
            raise TypeError("change_confidence must be a MutationConfidence")
        if isinstance(self.changed_variables, list):
            object.__setattr__(self, "changed_variables", tuple(self.changed_variables))
        if not isinstance(self.changed_variables, tuple) or any(
            not isinstance(item, ProxyDescriptor) for item in self.changed_variables
        ):
            raise TypeError("changed_variables must contain ProxyDescriptor values")
        if not isinstance(self.outputs, Mapping) or any(
            not isinstance(key, str) or not key or not isinstance(item, ProxyDescriptor)
            for key, item in self.outputs.items()
        ):
            raise TypeError("outputs must map aliases to ProxyDescriptor values")
        object.__setattr__(self, "outputs", MappingProxyType(dict(self.outputs)))
        if self.capture is not None:
            if not isinstance(self.capture, CaptureView):
                raise TypeError("capture must be a CaptureView")
            if self.state is not AgentOperationState.CAPTURED:
                raise ValueError("capture view requires captured operation state")
            fence = self.capture.fence
            if (
                self.operation.operation_id != fence.operation_id
                and self.operation.kind is not AgentOperationKind.CAPTURE_HYPOTHESIS
            ):
                raise ValueError("capture fence operation does not match operation view")
        if self.failure is not None:
            object.__setattr__(
                self,
                "failure",
                _failure_map(self.failure, allow_excerpt=True),
            )
        if isinstance(self.recovery, list):
            object.__setattr__(self, "recovery", tuple(self.recovery))
        if not isinstance(self.recovery, tuple) or any(
            not isinstance(action, RecoveryAction) for action in self.recovery
        ):
            raise TypeError("recovery must contain RecoveryAction values")
        if not isinstance(self.truncation, OperationTruncation):
            raise TypeError("truncation must be an OperationTruncation")
        if self.execution_provenance is not None and not isinstance(
            self.execution_provenance,
            OperationExecutionProvenance,
        ):
            raise TypeError(
                "execution_provenance must be an OperationExecutionProvenance"
            )


FACADE_WIRE_DATACLASSES: tuple[type[object], ...] = (
    AgentDiagnosticView,
    RecoveryAction,
    AgentOperationIdentity,
    OperationTruncation,
    OperationViewFacts,
    AgentOperationView,
)


__all__ = [
    "AgentDiagnosticView",
    "AgentOperationIdentity",
    "AgentOperationKind",
    "AgentOperationView",
    "MutationConfidence",
    "FACADE_WIRE_DATACLASSES",
    "OperationTruncation",
    "OperationViewFacts",
    "OperationViewSnapshot",
    "RecoveryAction",
]
