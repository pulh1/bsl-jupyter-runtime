from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from enum import StrEnum
from math import isfinite
import keyword
import re
from secrets import token_urlsafe
from types import MappingProxyType

from onec_runtime.bsl import NormalizedDiagnostic
from onec_runtime.errors import ProtocolError, RuntimeProbeError
from onec_runtime.runtime_contracts import (
    OperationExecutionProvenance,
    sanitize_normalized_diagnostic,
)
from onec_runtime_mcp.agent.proxies import (
    ProxyDescriptor,
    ProxyFence,
    ProxyProvenance,
    ValueBudget,
    ValuePreview,
    ValueSize,
)
from onec_runtime_mcp.agent.python_protocol import (
    PythonImportDescriptor,
    PythonInspection,
    PythonRunResult,
    PythonWorkspaceLimits,
    PythonWorkspaceStatus,
)


type JSONScalar = None | bool | int | float | str
type JSONValue = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]


MAX_OPERATION_MESSAGES = 100
MAX_MESSAGE_LENGTH = 4_096
class CapabilityMode(StrEnum):
    OBSERVE = "observe"
    EXPERIMENT = "experiment"
    COMMIT = "commit"
    ADMIN = "admin"


class FailureCategory(StrEnum):
    INVALID_REQUEST = "invalid_request"
    CONFLICT = "conflict"
    STALE = "stale"
    DENIED = "denied"
    LIMIT = "limit"
    UNSUPPORTED = "unsupported"
    PLATFORM_FAILURE = "platform_failure"
    LOST = "lost"
    UNKNOWN = "unknown"


class StateChanged(StrEnum):
    NO = "no"
    YES = "yes"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class RetrySafety(StrEnum):
    YES = "yes"
    NO = "no"
    AFTER_STATUS_CHECK = "after_status_check"


class AgentOperationState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    CAPTURED = "captured"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class BackendExecution:
    """Redacted terminal outcome supplied by a runtime backend."""

    terminal_state: AgentOperationState
    messages: tuple[str, ...]
    result_present: bool
    runtime_state: str
    changed_roots: tuple[str, ...] = ()
    failure_stage: str | None = None
    capture_dirty_roots: tuple[str, ...] = ()
    diagnostic: NormalizedDiagnostic | None = None
    state_changed: StateChanged = StateChanged.UNKNOWN

    def __post_init__(self) -> None:
        if self.terminal_state not in {
            AgentOperationState.COMPLETED,
            AgentOperationState.CAPTURED,
            AgentOperationState.FAILED,
            AgentOperationState.UNKNOWN,
        }:
            raise ValueError("backend outcome must be terminal")
        if isinstance(self.messages, str) or not isinstance(self.messages, tuple):
            raise TypeError("messages must be a tuple")
        if len(self.messages) > MAX_OPERATION_MESSAGES or any(
            not isinstance(message, str) or len(message) > MAX_MESSAGE_LENGTH
            for message in self.messages
        ):
            raise ValueError("messages must be bounded strings")
        if type(self.result_present) is not bool:
            raise TypeError("result_present must be a bool")
        _require_non_empty_string(self.runtime_state, field_name="runtime_state")
        if isinstance(self.changed_roots, str) or not isinstance(
            self.changed_roots, tuple
        ):
            raise TypeError("changed_roots must be a tuple")
        if any(not isinstance(root, str) or not root for root in self.changed_roots):
            raise ValueError("changed_roots must contain non-empty strings")
        if len({root.casefold() for root in self.changed_roots}) != len(
            self.changed_roots
        ):
            raise ValueError("changed_roots must be case-insensitively unique")
        if isinstance(self.capture_dirty_roots, str) or not isinstance(
            self.capture_dirty_roots, tuple
        ):
            raise TypeError("capture_dirty_roots must be a tuple")
        if any(
            not isinstance(root, str) or not root
            for root in self.capture_dirty_roots
        ):
            raise ValueError("capture_dirty_roots must contain non-empty strings")
        if len({root.casefold() for root in self.capture_dirty_roots}) != len(
            self.capture_dirty_roots
        ):
            raise ValueError(
                "capture_dirty_roots must be case-insensitively unique"
            )
        if self.failure_stage is not None and self.failure_stage not in {
            "parsing",
            "lowering",
            "execution",
        }:
            raise ValueError("failure_stage is unsupported")
        if self.diagnostic is not None and not isinstance(
            self.diagnostic, NormalizedDiagnostic
        ):
            raise TypeError("diagnostic must be a NormalizedDiagnostic")
        if not isinstance(self.state_changed, StateChanged):
            raise TypeError("state_changed must be a StateChanged")

    @classmethod
    def completed(
        cls,
        *,
        messages: tuple[str, ...],
        result_present: bool,
        runtime_state: str = "ready",
        changed_roots: tuple[str, ...] = (),
    ) -> "BackendExecution":
        return cls(
            AgentOperationState.COMPLETED,
            messages,
            result_present,
            runtime_state,
            changed_roots,
        )


class CodeLanguage(StrEnum):
    BSL = "bsl"
    PYTHON = "python"
    MARKDOWN = "markdown"


class CodeMode(StrEnum):
    MAIN = "main"
    CAPTURE = "capture"
    WORKER = "worker"


def _require_non_empty_string(value: str, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


def _require_positive(value: int, *, field_name: str) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field_name} must be positive")


def _require_non_negative(value: int | None, *, field_name: str) -> None:
    if value is not None and (type(value) is not int or value < 0):
        raise ValueError(f"{field_name} must be a non-negative integer")


def _frozen_string_sequence(value: object, *, field_name: str) -> tuple[object, ...]:
    if isinstance(value, str) or not isinstance(value, (tuple, list)):
        raise TypeError(f"{field_name} must be a sequence")
    return tuple(value)


def _frozen_wire_value(value: object) -> object:
    """Validate a wire value and detach all mutable containers from callers."""
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not isfinite(value):
            raise TypeError("value is not wire-safe")
        return value
    if isinstance(value, StrEnum):
        return value
    if isinstance(value, _WIRE_DATACLASSES) or _is_facade_wire_dataclass(value):
        return value
    if isinstance(value, (tuple, list)):
        return tuple(_frozen_wire_value(item) for item in value)
    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError("mapping key is not wire-safe")
            frozen[key] = _frozen_wire_value(item)
        return MappingProxyType(frozen)
    raise TypeError("value is not wire-safe")


@dataclass(frozen=True, slots=True)
class MethodFailure:
    category: FailureCategory
    state_changed: StateChanged
    safe_to_retry: RetrySafety
    current_state: Mapping[str, JSONValue]
    operation_id: str | None = None
    affected_proxies: tuple[str, ...] = ()
    partial_results: Mapping[str, JSONValue] = MappingProxyType({})
    event_cursor: int | None = None
    recommended_actions: tuple[str, ...] = ()
    diagnostic_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.category, FailureCategory):
            raise TypeError("category must be a FailureCategory")
        if not isinstance(self.state_changed, StateChanged):
            raise TypeError("state_changed must be a StateChanged")
        if not isinstance(self.safe_to_retry, RetrySafety):
            raise TypeError("safe_to_retry must be a RetrySafety")
        if not isinstance(self.current_state, Mapping):
            raise TypeError("current_state must be a mapping")
        if not isinstance(self.partial_results, Mapping):
            raise TypeError("partial_results must be a mapping")
        object.__setattr__(self, "current_state", _frozen_wire_value(self.current_state))
        object.__setattr__(self, "partial_results", _frozen_wire_value(self.partial_results))
        object.__setattr__(
            self,
            "affected_proxies",
            _frozen_string_sequence(
                self.affected_proxies,
                field_name="affected_proxies",
            ),
        )
        object.__setattr__(
            self,
            "recommended_actions",
            _frozen_string_sequence(
                self.recommended_actions,
                field_name="recommended_actions",
            ),
        )
        if self.operation_id is not None:
            _require_non_empty_string(self.operation_id, field_name="operation_id")
        _require_non_negative(self.event_cursor, field_name="event_cursor")
        if any(not isinstance(proxy, str) or not proxy.strip() for proxy in self.affected_proxies):
            raise ValueError("affected_proxies must contain non-empty strings")
        if any(
            not isinstance(action, str) or not action.strip()
            for action in self.recommended_actions
        ):
            raise ValueError("recommended_actions must contain non-empty strings")
        _require_non_empty_string(self.diagnostic_id, field_name="diagnostic_id")


@dataclass(frozen=True, slots=True)
class ServiceResponse:
    ok: bool
    value: object | None = None
    failure: MethodFailure | None = None

    def __post_init__(self) -> None:
        if type(self.ok) is not bool:
            raise TypeError("ok must be a bool")
        if self.ok:
            if self.failure is not None:
                raise ValueError("successful response cannot include failure")
            object.__setattr__(self, "value", _frozen_wire_value(self.value))
            return
        if self.failure is None or self.value is not None:
            raise ValueError("failed response requires failure and no value")
        if not isinstance(self.failure, MethodFailure):
            raise TypeError("failed response requires a MethodFailure")

    @classmethod
    def success(cls, value: object) -> ServiceResponse:
        return cls(ok=True, value=value)

    @classmethod
    def fail(cls, failure: MethodFailure) -> ServiceResponse:
        return cls(ok=False, failure=failure)


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    name: str
    available: bool
    reason: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty_string(self.name, field_name="name")
        if type(self.available) is not bool:
            raise TypeError("available must be a bool")
        if self.available and self.reason is not None:
            raise ValueError("available capability cannot include reason")
        if not self.available:
            _require_non_empty_string(self.reason or "", field_name="reason")


@dataclass(frozen=True, slots=True)
class WorkspaceDescriptor:
    workspace_id: str
    project_name: str
    project_root: str = ""
    current_runtime_id: str | None = None
    capabilities: tuple[CapabilityDescriptor, ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty_string(self.workspace_id, field_name="workspace_id")
        _require_non_empty_string(self.project_name, field_name="project_name")
        if self.project_root:
            _require_non_empty_string(self.project_root, field_name="project_root")
        if self.current_runtime_id is not None:
            _require_non_empty_string(self.current_runtime_id, field_name="current_runtime_id")
        object.__setattr__(self, "capabilities", tuple(self.capabilities))
        if any(
            not isinstance(capability, CapabilityDescriptor)
            for capability in self.capabilities
        ):
            raise TypeError("capabilities must contain CapabilityDescriptor values")
        names = tuple(capability.name for capability in self.capabilities)
        if len(set(names)) != len(names):
            raise ValueError("duplicate capability name")


@dataclass(frozen=True, slots=True)
class RuntimeDescriptor:
    runtime_id: str
    generation: int
    state: str
    mode: CapabilityMode
    active_operation_id: str | None = None
    health: str = ""

    def __post_init__(self) -> None:
        _require_non_empty_string(self.runtime_id, field_name="runtime_id")
        _require_positive(self.generation, field_name="generation")
        _require_non_empty_string(self.state, field_name="state")
        if not isinstance(self.mode, CapabilityMode):
            raise TypeError("mode must be a CapabilityMode")
        if self.active_operation_id is not None:
            _require_non_empty_string(self.active_operation_id, field_name="active_operation_id")
        if self.health:
            _require_non_empty_string(self.health, field_name="health")


@dataclass(frozen=True, slots=True)
class CodeDescriptor:
    cell_id: str
    revision: int
    language: CodeLanguage
    mode: CodeMode
    source_sha256: str
    outputs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty_string(self.cell_id, field_name="cell_id")
        _require_positive(self.revision, field_name="revision")
        if not isinstance(self.language, CodeLanguage):
            raise TypeError("language must be a CodeLanguage")
        if not isinstance(self.mode, CodeMode):
            raise TypeError("mode must be a CodeMode")
        _require_non_empty_string(self.source_sha256, field_name="source_sha256")
        object.__setattr__(
            self, "outputs", _frozen_string_sequence(self.outputs, field_name="outputs")
        )
        if any(not isinstance(item, str) or not item for item in self.outputs):
            raise ValueError("outputs must contain non-empty strings")
        self._validate_outputs()

    def _validate_outputs(self) -> None:
        if self.language is not CodeLanguage.PYTHON and self.outputs:
            raise ValueError("only Python code can declare outputs")
        if (
            len(set(self.outputs)) != len(self.outputs)
            or any(
                not item.isidentifier()
                or keyword.iskeyword(item)
                or item.startswith("_")
                for item in self.outputs
            )
        ):
            raise ValueError("Python outputs must be unique public identifiers")


@dataclass(frozen=True, slots=True)
class CodeRevision:
    cell_id: str
    revision: int
    source: str
    source_sha256: str
    document_sha256: str
    language: CodeLanguage
    mode: CodeMode
    outputs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty_string(self.cell_id, field_name="cell_id")
        _require_positive(self.revision, field_name="revision")
        if not isinstance(self.language, CodeLanguage):
            raise TypeError("language must be a CodeLanguage")
        if not isinstance(self.mode, CodeMode):
            raise TypeError("mode must be a CodeMode")
        if not isinstance(self.source, str):
            raise TypeError("source must be a string")
        _require_non_empty_string(self.source_sha256, field_name="source_sha256")
        _require_non_empty_string(self.document_sha256, field_name="document_sha256")
        object.__setattr__(
            self, "outputs", _frozen_string_sequence(self.outputs, field_name="outputs")
        )
        if any(not isinstance(item, str) or not item for item in self.outputs):
            raise ValueError("outputs must contain non-empty strings")
        if self.language is not CodeLanguage.PYTHON and self.outputs:
            raise ValueError("only Python code can declare outputs")
        if (
            len(set(self.outputs)) != len(self.outputs)
            or any(
                not item.isidentifier()
                or keyword.iskeyword(item)
                or item.startswith("_")
                for item in self.outputs
            )
        ):
            raise ValueError("Python outputs must be unique public identifiers")


@dataclass(frozen=True, slots=True)
class OperationDescriptor:
    operation_id: str
    state: AgentOperationState
    runtime_id: str = ""
    runtime_generation: int | None = None
    cell_id: str | None = None
    revision: int | None = None
    source_sha256: str | None = None
    messages_cursor: int = 0
    event_cursor: int = 0
    result_present: bool = False
    result_access: str = "unavailable_until_value_proxies"
    safe_to_retry: RetrySafety = RetrySafety.AFTER_STATUS_CHECK

    def __post_init__(self) -> None:
        _require_non_empty_string(self.operation_id, field_name="operation_id")
        if not isinstance(self.state, AgentOperationState):
            raise TypeError("state must be an AgentOperationState")
        if self.runtime_id:
            _require_non_empty_string(self.runtime_id, field_name="runtime_id")
        if self.runtime_generation is not None:
            _require_positive(self.runtime_generation, field_name="runtime_generation")
        if self.cell_id is not None:
            _require_non_empty_string(self.cell_id, field_name="cell_id")
        if self.revision is not None:
            _require_positive(self.revision, field_name="revision")
        if self.source_sha256 is not None:
            _require_non_empty_string(self.source_sha256, field_name="source_sha256")
        _require_non_negative(self.messages_cursor, field_name="messages_cursor")
        _require_non_negative(self.event_cursor, field_name="event_cursor")
        if type(self.result_present) is not bool:
            raise TypeError("result_present must be a bool")
        _require_non_empty_string(self.result_access, field_name="result_access")
        if not isinstance(self.safe_to_retry, RetrySafety):
            raise TypeError("safe_to_retry must be a RetrySafety")


@dataclass(frozen=True, slots=True)
class OperationOutput:
    operation_id: str
    messages: tuple[str, ...]
    next_cursor: int
    has_more: bool

    def __post_init__(self) -> None:
        _require_non_empty_string(self.operation_id, field_name="operation_id")
        _require_non_negative(self.next_cursor, field_name="next_cursor")
        if type(self.has_more) is not bool:
            raise TypeError("has_more must be a bool")
        object.__setattr__(
            self,
            "messages",
            _frozen_string_sequence(self.messages, field_name="messages"),
        )
        if len(self.messages) > MAX_OPERATION_MESSAGES or any(
            not isinstance(message, str) or len(message) > MAX_MESSAGE_LENGTH
            for message in self.messages
        ):
            raise ValueError("messages must be bounded strings")


_WIRE_DATACLASSES: tuple[type[object], ...] = (
    MethodFailure,
    ServiceResponse,
    WorkspaceDescriptor,
    RuntimeDescriptor,
    CodeDescriptor,
    CodeRevision,
    OperationDescriptor,
    OperationOutput,
    CapabilityDescriptor,
    BackendExecution,
    OperationExecutionProvenance,
    ProxyFence,
    ProxyProvenance,
    ProxyDescriptor,
    ValueBudget,
    ValueSize,
    ValuePreview,
    PythonWorkspaceLimits,
    PythonWorkspaceStatus,
    PythonRunResult,
    PythonInspection,
    PythonImportDescriptor,
)


def to_wire(value: object) -> JSONValue:
    """Return an allowlisted JSON value without stringifying arbitrary objects."""
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if isfinite(value):
            return value
        raise TypeError("value is not wire-safe")
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, ProxyFence):
        result: dict[str, JSONValue] = {
            "runtime_id": to_wire(value.runtime_id),
            "runtime_generation": to_wire(value.runtime_generation),
            "context_generation": to_wire(value.context_generation),
            "python_generation": to_wire(value.python_generation),
        }
        if value.capture_fence is not None:
            result["capture_fence"] = to_wire(value.capture_fence)
        return result
    if isinstance(value, _WIRE_DATACLASSES) or _is_facade_wire_dataclass(value):
        return {field.name: to_wire(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, (tuple, list)):
        return [to_wire(item) for item in value]
    if isinstance(value, Mapping):
        result: dict[str, JSONValue] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError("mapping key is not wire-safe")
            result[key] = to_wire(item)
        return result
    raise TypeError("value is not wire-safe")


def _is_facade_wire_dataclass(value: object) -> bool:
    try:
        from onec_runtime_mcp.agent.capture_contracts import CAPTURE_WIRE_DATACLASSES
        from onec_runtime_mcp.agent.facade_contracts import FACADE_WIRE_DATACLASSES
        from onec_runtime_mcp.agent.observation import OBSERVATION_WIRE_DATACLASSES
        from onec_runtime_mcp.agent.value_policy import VALUE_POLICY_WIRE_DATACLASSES
    except ImportError:
        return False
    return isinstance(
        value,
        CAPTURE_WIRE_DATACLASSES
        + FACADE_WIRE_DATACLASSES
        + OBSERVATION_WIRE_DATACLASSES
        + VALUE_POLICY_WIRE_DATACLASSES,
    )


def failure_from_exception(
    error: BaseException,
    current_state: Mapping[str, JSONValue],
) -> MethodFailure:
    """Normalize failures without allowing exception detail onto the wire."""
    if isinstance(error, (ValueError, TypeError)):
        category = FailureCategory.INVALID_REQUEST
        state_changed = StateChanged.NO
        safe_to_retry = RetrySafety.NO
    elif isinstance(error, (ProtocolError, RuntimeProbeError)):
        category = FailureCategory.PLATFORM_FAILURE
        state_changed = StateChanged.UNKNOWN
        safe_to_retry = RetrySafety.AFTER_STATUS_CHECK
    else:
        category = FailureCategory.UNKNOWN
        state_changed = StateChanged.UNKNOWN
        safe_to_retry = RetrySafety.AFTER_STATUS_CHECK
    return MethodFailure(
        category=category,
        state_changed=state_changed,
        safe_to_retry=safe_to_retry,
        current_state=current_state,
        diagnostic_id=f"diag_{token_urlsafe(18)}",
    )
