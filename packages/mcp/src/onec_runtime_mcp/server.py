"""Thin, optional MCP v2 transport for a running agent runtime service."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Annotated, Literal, cast
from uuid import uuid4

from mcp.server import MCPServer
from mcp_types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, RootModel, StrictInt, model_validator

from onec_runtime_mcp.agent.contracts import (
    AgentOperationState,
    CodeLanguage,
    FailureCategory,
    MethodFailure,
    RetrySafety,
    ServiceResponse,
    StateChanged,
    to_wire,
)
from onec_runtime_mcp.agent.facade_contracts import AgentOperationKind
from onec_runtime_mcp.agent.proxies import (
    MeasurementCost,
    ProxyConsistency,
    ProxyLifetime,
    ProxyRealm,
    SizeAccuracy,
)
from onec_runtime_mcp.agent.service_client import ServiceClient
from onec_runtime_mcp.agent.facade import AgentFacade, AgentFacadeError
from onec_runtime_mcp.agent.mcp_profiles import McpProfile, tool_names
from onec_runtime.privacy import bounded_platform_diagnostic


_TOOL_NAMES = (
    "workspace.open", "workspace.status", "workspace.capabilities", "workspace.close",
    "workspace.variables", "workspace.variable", "workspace.variable_history",
    "workspace.snapshot_variables", "workspace.delete_variables",
    "runtime.start", "runtime.ensure", "runtime.list", "runtime.select", "runtime.status",
    "runtime.request_mode", "runtime.restart", "runtime.close",
    "code.list", "code.get", "code.put", "code.diff", "code.history", "code.run",
    "code.run_inline", "code.promote", "code.delete",
    "operation.list", "operation.status", "operation.wait", "operation.output",
    "operation.result", "operation.stop_waiting", "operation.abort_generation",
    "operation.explain_failure",
    "value.inspect", "value.describe", "value.size", "value.preview", "value.get", "value.select",
    "value.snapshot", "value.materialize", "value.to_df", "value.compare", "value.release",
    "python.variables", "python.run", "python.inspect", "python.imports", "python.reset", "python.status",
)

_TEXT = Annotated[str, Field(min_length=1, max_length=4096)]
_SOURCE = Annotated[str, Field(min_length=1)]
_HASH = Annotated[str, Field(min_length=64, max_length=64)]
_LOWER_SHA256 = Annotated[
    str,
    Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"),
]
_POSITIVE = Annotated[int, Field(gt=0)]
_CURSOR = Annotated[int, Field(ge=0)]
_CAPTURE_PROJECTION_OFFSET = Annotated[int, Field(ge=0, le=10_000_000)]
_WAIT_SECONDS = Annotated[float, Field(ge=0, le=30)]
_MESSAGE_LIMIT = Annotated[int, Field(ge=1, le=100)]
_MODE = Literal["observe", "experiment", "commit", "admin"]
_CODE_MODE = Literal["main", "capture", "worker"]
_WIRE_ID = Annotated[str, Field(min_length=1, max_length=256)]
_WIRE_KEY = Annotated[str, Field(min_length=1, max_length=256)]
_WIRE_TEXT = Annotated[str, Field(max_length=4096)]
_WIRE_MESSAGE = Annotated[str, Field(max_length=4096)]
_WIRE_STATUS = Annotated[str, Field(min_length=1, max_length=128)]
_PARTIAL_STATUS = Literal[
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
]
_CONTINUE_STATUS = Literal[
    "unattempted", "planned", "sent", "acknowledged", "outcome_unknown"
]
_STATE_CHANGED = Literal["no", "yes", "partial", "unknown"]
_DIAGNOSTIC_STAGE = Literal["parsing", "lowering", "compilation", "execution"]
_MAPPING_CONFIDENCE = Literal["exact", "nearest", "synthetic", "unknown"]
_DIAGNOSTIC_COORDINATE = Annotated[StrictInt, Field(ge=0, le=10_000_000)]
_DIAGNOSTIC_POSITIVE_COORDINATE = Annotated[
    StrictInt,
    Field(ge=1, le=10_000_000),
]
_DIAGNOSTIC_LABEL = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_]{0,127}$"),
]
_DIAGNOSTIC_EXCERPT = Annotated[str, Field(max_length=512)]


class _BoundedJsonArray(RootModel[list["_BoundedJsonValue"]]):
    root: list["_BoundedJsonValue"] = Field(max_length=100)


class _BoundedJsonObject(RootModel[dict[_WIRE_KEY, "_BoundedJsonValue"]]):
    root: dict[_WIRE_KEY, "_BoundedJsonValue"] = Field(max_length=100)


_BoundedJsonValue = (
    _WIRE_TEXT
    | int
    | float
    | bool
    | None
    | _BoundedJsonArray
    | _BoundedJsonObject
)


class _DiagnosticSpanWire(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    start: _DIAGNOSTIC_COORDINATE
    end: _DIAGNOSTIC_COORDINATE

    @model_validator(mode="after")
    def require_ordered_span(self) -> "_DiagnosticSpanWire":
        if self.start > self.end:
            raise ValueError("diagnostic span must be ordered")
        return self


class _DiagnosticLocationWire(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    line: _DIAGNOSTIC_POSITIVE_COORDINATE
    column: _DIAGNOSTIC_POSITIVE_COORDINATE
    span: _DiagnosticSpanWire


class _AgentDiagnosticViewWire(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    diagnostic_id: _LOWER_SHA256
    stage: _DIAGNOSTIC_STAGE
    mapping_confidence: _MAPPING_CONFIDENCE
    visible_location: _DiagnosticLocationWire | None
    related_visible_span: _DiagnosticSpanWire | None
    excerpt: _DIAGNOSTIC_EXCERPT | None
    synthetic_region: _DIAGNOSTIC_LABEL | None


class _LoweredDiagnosticLocationWire(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    line: _DIAGNOSTIC_POSITIVE_COORDINATE
    column: _DIAGNOSTIC_POSITIVE_COORDINATE
    offset: _DIAGNOSTIC_COORDINATE
    span: _DiagnosticSpanWire


class _ExpertDiagnosticWire(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    diagnostic_id: _LOWER_SHA256
    runtime_summary: Literal[
        "BSL parsing failed",
        "BSL lowering failed",
        "BSL compilation failed",
        "BSL execution failed",
    ]
    stage: _DIAGNOSTIC_STAGE
    mapping_confidence: _MAPPING_CONFIDENCE
    visible_location: _DiagnosticLocationWire | None
    related_visible_span: _DiagnosticSpanWire | None
    excerpt: _DIAGNOSTIC_EXCERPT | None
    synthetic_region: _DIAGNOSTIC_LABEL | None
    lowered_location: _LoweredDiagnosticLocationWire | None
    platform_diagnostic: _WIRE_TEXT | None
    platform_diagnostic_sha256: _LOWER_SHA256 | None
    platform_diagnostic_truncated: bool
    platform_diagnostic_redacted: bool
    execution_artifact_sha256: _LOWER_SHA256 | None
    source_map_sha256: _LOWER_SHA256 | None
    worker_generation: Annotated[StrictInt, Field(ge=0, le=10_000_000)] | None
    worker_manifest_sha256: _LOWER_SHA256 | None

    @model_validator(mode="after")
    def require_canonical_summary(self) -> "_ExpertDiagnosticWire":
        summaries = {
            "parsing": "BSL parsing failed",
            "lowering": "BSL lowering failed",
            "compilation": "BSL compilation failed",
            "execution": "BSL execution failed",
        }
        if self.runtime_summary != summaries[self.stage]:
            raise ValueError("diagnostic summary does not match its stage")
        if self.platform_diagnostic is not None and self.platform_diagnostic_sha256 is None:
            raise ValueError("platform diagnostic requires its original hash")
        bounded, truncated, redacted = bounded_platform_diagnostic(
            self.platform_diagnostic,
            truncated=self.platform_diagnostic_truncated,
            redacted=self.platform_diagnostic_redacted,
        )
        if (
            bounded != self.platform_diagnostic
            or truncated != self.platform_diagnostic_truncated
            or redacted != self.platform_diagnostic_redacted
        ):
            raise ValueError("platform diagnostic is not privacy-canonical")
        return self


class _ExpertFailureExplanationWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: _WIRE_ID
    category: FailureCategory
    state: AgentOperationState
    state_changed: StateChanged
    safe_to_retry: RetrySafety
    cell_id: _WIRE_ID | None
    revision: Annotated[StrictInt, Field(ge=0, le=10_000_000)] | None
    source_sha256: _LOWER_SHA256 | None
    recommended_actions: list[_WIRE_TEXT] = Field(max_length=100)
    diagnostic: _AgentDiagnosticViewWire | None
    diagnostic_details: _ExpertDiagnosticWire | None

    @model_validator(mode="after")
    def require_exact_diagnostic_binding(self) -> "_ExpertFailureExplanationWire":
        public = self.diagnostic
        expert = self.diagnostic_details
        if expert is None:
            return self
        if public is None or (
            expert.diagnostic_id != public.diagnostic_id
            or expert.stage != public.stage
            or expert.mapping_confidence != public.mapping_confidence
            or expert.visible_location != public.visible_location
            or expert.related_visible_span != public.related_visible_span
            or expert.synthetic_region != public.synthetic_region
        ):
            raise ValueError("expert diagnostic does not match the public record")
        if (
            self.cell_id is None
            or self.revision is None
            or self.source_sha256 is None
        ):
            raise ValueError("expert diagnostic requires exact operation source")
        return self


class _ExpertSafeFailureWire(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    category: Literal["platform_failure"]
    state_changed: Literal["unknown"]
    safe_to_retry: Literal["after_status_check"]
    current_state: dict[str, object] = Field(max_length=0)
    operation_id: None
    affected_proxies: list[_WIRE_ID] = Field(max_length=0)
    partial_results: dict[str, object] = Field(max_length=0)
    event_cursor: None
    recommended_actions: list[_WIRE_TEXT] = Field(max_length=0)
    diagnostic_id: _WIRE_ID


class _ExpertFailureResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    value: _ExpertFailureExplanationWire | None
    failure: _ExpertSafeFailureWire | None

    @model_validator(mode="after")
    def require_one_outcome(self) -> "_ExpertFailureResponse":
        if self.ok != (self.value is not None and self.failure is None):
            raise ValueError("expert failure response has an invalid outcome")
        return self


class _OperationFailureWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: _WIRE_STATUS
    partial_results: dict[_WIRE_KEY, _PARTIAL_STATUS] = Field(
        default_factory=dict, max_length=100
    )
    continue_state: _CONTINUE_STATUS | None = None
    state_changed: _STATE_CHANGED | None = None
    diagnostic: _AgentDiagnosticViewWire | None = None


class _AgentOperationIdentityWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: _WIRE_ID
    kind: AgentOperationKind
    runtime_id: Annotated[str, Field(max_length=256)]
    runtime_generation: int | None
    cell_id: _WIRE_ID | None
    revision: int | None
    source_sha256: _HASH | None


class _OperationExecutionProvenanceWire(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    visible_source_sha256: _LOWER_SHA256
    executed_source_sha256: _LOWER_SHA256
    source_map_sha256: _LOWER_SHA256
    mode: _CODE_MODE
    worker_generation: Annotated[StrictInt, Field(ge=0, le=10_000_000)] | None
    worker_manifest_sha256: _LOWER_SHA256 | None


class _AgentOperationViewWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: _AgentOperationIdentityWire
    state: AgentOperationState
    messages: list[_WIRE_MESSAGE] = Field(max_length=100)
    next_message_cursor: int
    next_event_cursor: int
    changed_variables: list["_ProxyDescriptorWire"] = Field(max_length=100)
    change_confidence: Literal["exact", "declared", "inferred", "unknown"]
    outputs: dict[_WIRE_KEY, "_ProxyDescriptorWire"] = Field(max_length=100)
    capture: "_CaptureViewWire | None"
    failure: _OperationFailureWire | None
    recovery: list["_RecoveryActionWire"] = Field(max_length=100)
    truncation: "_OperationTruncationWire"
    execution_provenance: _OperationExecutionProvenanceWire | None


class _ProxyFenceWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runtime_id: _WIRE_ID | None = None
    runtime_generation: int | None = None
    context_generation: int | None = None
    python_generation: int | None = None
    capture_fence: "_CaptureFenceWire | None" = None


class _ProxyProvenanceWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cell_id: _WIRE_ID
    revision: int
    source_sha256: _HASH
    operation_id: _WIRE_ID
    parent_proxy_ids: list[_WIRE_ID] = Field(default_factory=list, max_length=100)


class _ValueSizeWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: int | None = None
    rows: int | None = None
    bytes: int | None = None
    accuracy: SizeAccuracy
    cost: MeasurementCost


class _ValuePreviewWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type_name: _WIRE_TEXT
    scalar: _WIRE_TEXT | int | float | bool | None = None
    sample: list[_BoundedJsonValue] = Field(default_factory=list, max_length=100)
    truncated: bool = False


class _ProxyDescriptorWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proxy_id: _WIRE_ID
    realm: ProxyRealm
    lifetime: ProxyLifetime
    qualified_name: Annotated[str, Field(min_length=1, max_length=512)]
    type_name: _WIRE_TEXT
    version: int
    consistency: ProxyConsistency
    fence: _ProxyFenceWire
    provenance: _ProxyProvenanceWire
    capabilities: list[_WIRE_ID] = Field(default_factory=list, max_length=100)
    known_size: _ValueSizeWire | None = None
    bounded_preview: _ValuePreviewWire | None = None


class _EmptyRecoveryArgumentsWire(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _OperationWaitRecoveryArgumentsWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: _WIRE_ID
    timeout_s: _WAIT_SECONDS
    after_event_cursor: _CURSOR
    after_message_cursor: _CURSOR


class _RuntimeCloseRecoveryArgumentsWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy: Literal["abort_generation"]


class _RuntimeEnsureRecoveryArgumentsWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: _MODE
    profile: _WIRE_ID


class _RecoveryActionWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: Literal[
        "workspace.status",
        "runtime.close",
        "runtime.restart",
        "runtime.ensure",
        "operation.wait",
    ]
    arguments: (
        _EmptyRecoveryArgumentsWire
        | _OperationWaitRecoveryArgumentsWire
        | _RuntimeCloseRecoveryArgumentsWire
        | _RuntimeEnsureRecoveryArgumentsWire
    ) = Field(default_factory=_EmptyRecoveryArgumentsWire)


class _OperationTruncationWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: bool = False
    changed_variables: bool = False
    outputs: bool = False


class _ManagerOriginWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    namespace: Literal["frame", "context"]
    root: Annotated[str, Field(min_length=1, max_length=256)]
    fields: list[Annotated[str, Field(min_length=1, max_length=256)]] = Field(default_factory=list, max_length=100)


class _ContextBindingSourceWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["context_binding"]
    name: Annotated[str, Field(min_length=5, max_length=512)]


class _FrameLocalSourceWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["frame_local"]
    name: Annotated[str, Field(min_length=1, max_length=256)]


class _TemporaryTableSourceWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["temporary_table"]
    manager_id: Annotated[str, Field(min_length=1, max_length=128)]
    table: Annotated[str, Field(min_length=1, max_length=256)]


class _TemporaryTableManagerSourceWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["temporary_table_manager"]
    origin: _ManagerOriginWire


_ObservationSourceWire = (
    _ContextBindingSourceWire
    | _FrameLocalSourceWire
    | _TemporaryTableSourceWire
    | _TemporaryTableManagerSourceWire
)


class _SliceSelectionWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["slice"]
    offset: Annotated[int, Field(ge=0)] = 0
    limit: Annotated[int, Field(gt=0, le=10_000)]


class _TableRowsSelectionWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["table_rows"]
    offset: Annotated[int, Field(ge=0)] = 0
    limit: Annotated[int, Field(gt=0, le=10_000)]
    columns: list[Annotated[str, Field(min_length=1, max_length=256)]] = Field(default_factory=list, max_length=100)


class _NamesSelectionWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["fields", "keys"]
    names: list[Annotated[str, Field(min_length=1, max_length=256)]] = Field(max_length=100)


_ValueSelectionWire = _SliceSelectionWire | _TableRowsSelectionWire | _NamesSelectionWire


class _ObservationItemWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alias: Annotated[str, Field(min_length=1, max_length=256)]
    source: _ObservationSourceWire
    result: Literal["proxy", "preview", "python", "dataframe"] = Field(
        default="proxy",
        description=(
            "For an unselected captured temporary table, preview creates one "
            "server-owned bounded head selection; python/dataframe require an "
            "explicit table_rows selection."
        ),
    )
    select: _ValueSelectionWire | None = Field(
        default=None,
        description=(
            "Finite projection. Captured table metadata remains proxy-only "
            "unless a bounded row selection is supplied or synthesized for preview."
        ),
    )


class _ObservationPlanWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[_ObservationItemWire] = Field(default_factory=list, max_length=100)
    budget_profile: Literal[
        "agent_metadata", "agent_preview", "agent_dataframe"
    ] = "agent_metadata"


class _CaptureTableRowsSelectionWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["table_rows"]
    offset: Annotated[int, Field(ge=0)] = 0
    limit: Annotated[int, Field(gt=0, le=100)]
    columns: list[
        Annotated[str, Field(min_length=1, max_length=256)]
    ] = Field(default_factory=list, max_length=100)


_CaptureValueSelectionWire = (
    _SliceSelectionWire | _CaptureTableRowsSelectionWire | _NamesSelectionWire
)


class _CaptureObservationItemWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alias: Annotated[str, Field(min_length=1, max_length=256)]
    source: _ObservationSourceWire
    result: Literal["proxy", "preview", "python", "dataframe"] = "proxy"
    select: _CaptureValueSelectionWire | None = None


class _CaptureHypothesisObservationPlanWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[_CaptureObservationItemWire] = Field(
        default_factory=list, max_length=100
    )
    budget_profile: Literal[
        "agent_metadata", "agent_preview", "agent_dataframe"
    ] = "agent_metadata"


class _RuntimeDescriptorWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runtime_id: _WIRE_ID
    generation: int
    state: _WIRE_STATUS
    mode: _WIRE_STATUS
    active_operation_id: _WIRE_ID | None = None
    health: _WIRE_TEXT = ""


class _AgentOperationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    value: _AgentOperationViewWire | None
    failure: dict[str, object] | None


class _AgentEnsureResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    value: _AgentOperationViewWire | _RuntimeDescriptorWire | None
    failure: dict[str, object] | None


class _CaptureFenceWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capture_intent_id: _TEXT
    operation_id: _TEXT
    source_revision: _POSITIVE
    source_sha256: _HASH
    capture_generation: _POSITIVE
    stop_sequence: _POSITIVE


class _CaptureCodeRefWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cell_id: _TEXT
    revision: _POSITIVE
    source_sha256: _HASH


class _CaptureInspectFiltersWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: _TEXT | None = None
    role: _TEXT | None = None
    type: _TEXT | None = None


class _CaptureManagerOriginWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    namespace: Literal["frame"]
    root: _TEXT
    fields: list[_TEXT] = Field(default_factory=list, max_length=100)


class _CaptureManagerSourceWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["temporary_table_manager"]
    origin: _CaptureManagerOriginWire


class _CaptureTableSourceWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["temporary_table"]
    manager_id: _TEXT
    table: _TEXT


class _CaptureTableSelectionWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["table_rows"]
    offset: _CAPTURE_PROJECTION_OFFSET = 0
    limit: Annotated[int, Field(ge=1, le=100)]
    columns: list[_TEXT] = Field(default_factory=list, max_length=100)


class _CaptureManagerObservationWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alias: _TEXT
    source: _CaptureManagerSourceWire
    result: Literal["proxy"] = "proxy"


class _CaptureTableObservationWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alias: _TEXT
    source: _CaptureTableSourceWire
    result: Literal["proxy"] = "proxy"
    select: _CaptureTableSelectionWire | None = None


class _CaptureInspectObservationPlanWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[_CaptureManagerObservationWire | _CaptureTableObservationWire] = Field(default_factory=list, max_length=100)
    budget_profile: Literal["agent_metadata"] = "agent_metadata"


class _CapturePointWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=256)]
    project: Annotated[str, Field(max_length=256)] = ""
    module: Annotated[str, Field(max_length=512)] = ""
    procedure: Annotated[str, Field(max_length=256)] = ""
    line: Annotated[int, Field(gt=0)] | None = None
    source_fragment: Annotated[str, Field(min_length=1, max_length=4096)] | None = None


class _ResolvedCapturePointWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=256)]
    project: Annotated[str, Field(min_length=1, max_length=256)]
    module: Annotated[str, Field(min_length=1, max_length=512)]
    procedure: Annotated[str, Field(min_length=1, max_length=256)]
    line: _POSITIVE
    source_revision: _POSITIVE
    source_sha256: _HASH
    executable_line: _POSITIVE
    excerpt: Annotated[str, Field(min_length=1, max_length=4096)]


class _FrameVariableDescriptorWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=256)]
    type_name: _TEXT
    fence: _CaptureFenceWire
    capabilities: list[_TEXT] = Field(default_factory=list, max_length=100)
    known_size: _ValueSizeWire | None = None
    proxy_id: Annotated[str, Field(max_length=256)] = ""


class _TemporaryTableManagerDescriptorWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manager_id: _TEXT
    origin: _CaptureManagerOriginWire
    fence: _CaptureFenceWire
    capabilities: list[_TEXT] = Field(default_factory=list, max_length=100)


class _TemporaryTableDescriptorWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table_id: _TEXT
    manager_id: _TEXT
    name: Annotated[str, Field(min_length=1, max_length=256)]
    fence: _CaptureFenceWire
    schema_: list[Annotated[str, Field(min_length=1, max_length=256)]] = Field(
        default_factory=list,
        alias="schema",
        serialization_alias="schema",
        max_length=100,
    )
    known_size: _ValueSizeWire | None = None
    capabilities: list[_TEXT] = Field(default_factory=list, max_length=100)


class _CaptureInspectionWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fence: _CaptureFenceWire
    variables: list[_FrameVariableDescriptorWire] = Field(max_length=100)
    temporary_table_managers: list[_TemporaryTableManagerDescriptorWire] = Field(
        max_length=100
    )
    temporary_tables: list[_TemporaryTableDescriptorWire] = Field(max_length=100)
    cursor: _CURSOR
    limit: Annotated[int, Field(ge=1, le=100)]
    total_variables: _CURSOR
    next_cursor: _CURSOR | None
    truncated: bool


class _CaptureViewWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fence: _CaptureFenceWire
    location: _ResolvedCapturePointWire
    inspection: _CaptureInspectionWire | None
    dirty_roots: list[Annotated[str, Field(min_length=1, max_length=256)]] = Field(
        max_length=100
    )
    paused: Literal[True]
    mutable_object_caveat: Literal[
        "live mutable-object mutations are immediate and non-transactional"
    ]
    recovery: list[_RecoveryActionWire] = Field(max_length=100)


class _FailureDetailWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: _WIRE_KEY
    value: _WIRE_TEXT


class _CaptureFailureWire(BaseModel):
    """Finite, sanitized MCP failure contract for every capture tool."""

    model_config = ConfigDict(extra="forbid")

    category: FailureCategory
    code: _WIRE_ID
    retryable: RetrySafety
    state_changed: StateChanged
    stage: _WIRE_STATUS
    message: _WIRE_MESSAGE
    details: list[_FailureDetailWire] = Field(default_factory=list, max_length=100)
    partial_results: dict[_WIRE_KEY, _WIRE_STATUS] = Field(
        default_factory=dict, max_length=100
    )
    continue_state: _CONTINUE_STATUS | None = None
    recovery: list[_RecoveryActionWire] = Field(
        default_factory=list, max_length=100
    )


class _CaptureOperationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    value: _AgentOperationViewWire | None
    failure: _CaptureFailureWire | None


class _CaptureInspectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    value: _CaptureInspectionWire | None
    failure: _CaptureFailureWire | None


class _CaptureStackFrameWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: _CURSOR
    runtime_kernel: bool
    module_type: _TEXT | None
    object_id: _TEXT | None
    property_id: _TEXT | None
    line: _CURSOR | None
    extension_name: Annotated[str, Field(max_length=256)] | None


class _CaptureStackWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    frames: list[_CaptureStackFrameWire] = Field(max_length=100)
    total: _CURSOR
    next_cursor: _CURSOR | None


class _CaptureFrameVariableWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=256)]
    type_name: _TEXT
    presentation: Annotated[str, Field(max_length=512)] | None = None
    collection_size: _CURSOR | None = None


class _CaptureFrameWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    frame: _CaptureStackFrameWire
    variables: list[_CaptureFrameVariableWire] = Field(max_length=100)
    total: _CURSOR
    next_cursor: _CURSOR | None


class _CaptureStackResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    value: _CaptureStackWire | None
    failure: _CaptureFailureWire | None


class _CaptureFrameResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    value: _CaptureFrameWire | None
    failure: _CaptureFailureWire | None


_TOOL_ANNOTATIONS = {
    "workspace.open": ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True),
    "workspace.status": ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True),
    "workspace.capabilities": ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True),
    "workspace.close": ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False),
    "workspace.variables": ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
    "workspace.variable": ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
    "workspace.variable_history": ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
    "workspace.snapshot_variables": ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False),
    "workspace.delete_variables": ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False),
    "runtime.start": ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False),
    "runtime.ensure": ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
    "runtime.list": ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True),
    "runtime.select": ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
    "runtime.status": ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True),
    "runtime.request_mode": ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True),
    "runtime.restart": ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False),
    "runtime.close": ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False),
    "code.list": ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
    "code.get": ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True),
    "code.put": ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False),
    "code.diff": ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True),
    "code.history": ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True),
    "code.run": ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False),
    "code.run_inline": ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False),
    "code.promote": ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False),
    "code.delete": ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False),
    "operation.list": ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True),
    "operation.status": ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True),
    "operation.wait": ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True),
    "operation.output": ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True),
    "operation.result": ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True),
    "operation.stop_waiting": ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
    "operation.abort_generation": ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False),
    "operation.explain_failure": ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True),
    "value.describe": ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True),
    "value.inspect": ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True),
    "value.size": ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True),
    "value.preview": ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True),
    "value.get": ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False),
    "value.select": ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False),
    "value.snapshot": ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False),
    "value.materialize": ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False),
    "value.to_df": ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False),
    "value.compare": ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True),
    "value.release": ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True),
    "python.variables": ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True),
    "python.run": ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False),
    "python.inspect": ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True),
    "python.imports": ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
    "python.reset": ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False),
    "python.status": ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
    "capture.run_until": ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True),
    "capture.inspect": ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True),
    "capture.stack": ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True),
    "capture.frame": ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True),
    "capture.hypothesis": ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True),
    "capture.continue": ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True),
}

_TOOL_DESCRIPTIONS = {
    "workspace.open": "Open and select a project workspace and its notebook code store.",
    "workspace.status": "Return the current workspace selection, runtime summary, and service state.",
    "workspace.capabilities": "Return the API version and capabilities actually available from this service.",
    "workspace.close": "Detach this caller or abort the current runtime generation according to policy.",
    "workspace.variables": "List current user BSL and/or Python variables as generation-fenced proxies with origin-cell provenance.",
    "workspace.variable": "Resolve one exact qualified bsl.* or python.* variable without silently resolving name collisions.",
    "workspace.variable_history": "List version and derivation history for one qualified variable binding.",
    "workspace.snapshot_variables": "Create bounded immutable Python snapshots for selected BSL variables.",
    "workspace.delete_variables": "Delete selected Python bindings only when all expected versions match.",
    "runtime.start": "Start a new supervised 1C runtime; fail if one already exists.",
    "runtime.ensure": "Select a suitable existing 1C runtime or start one; this is the normal agent entrypoint.",
    "runtime.list": "List runtimes available in the current workspace.",
    "runtime.select": "Select an existing runtime for this authenticated caller.",
    "runtime.status": "Return runtime lifecycle, generation, active operation, mode, and health.",
    "runtime.request_mode": "Check whether a capability mode is allowed without exceeding the server maximum.",
    "runtime.restart": "Abort the current generation and start a new one, invalidating its 1C values.",
    "runtime.close": "Explicitly abort and close the selected runtime generation.",
    "code.list": "Select a notebook container, list its cells, and record immutable revision history.",
    "code.get": "Read one exact cell revision with source, metadata, hashes, and provenance.",
    "code.put": "Save a new cell revision using required revision and document-hash conflict fences.",
    "code.diff": "Return a bounded diff between two immutable revisions of a cell.",
    "code.history": "List immutable cell revisions and their recorded executions.",
    "code.run": "Execute one exact saved revision fenced by revision number and source hash.",
    "code.run_inline": "Execute unsaved code as an immutable inline operation without changing the notebook.",
    "code.promote": "Persist the exact visible source of an inline operation as a notebook cell revision.",
    "code.delete": "Delete a saved cell using required revision and document-hash conflict fences.",
    "operation.list": "List active and recent durable operations, optionally filtered by runtime or state.",
    "operation.status": "Return the authoritative current state and event cursor of an operation.",
    "operation.wait": "Wait for operation or event progress without starting another execution.",
    "operation.output": "Read ordered messages after a cursor with an explicit bounded message limit.",
    "operation.result": "Return result metadata when supported; raw 1C values are never returned.",
    "operation.stop_waiting": "Stop only this caller's wait; this does not stop code running in 1C.",
    "operation.abort_generation": "Explicitly terminate the entire runtime generation that owns an operation.",
    "operation.explain_failure": "Return a normalized failure cause and safe recovery actions for an operation.",
    "value.describe": "Return proxy type, lifetime, generation fences, provenance, version and supported actions.",
    "value.inspect": "Inspect proxy metadata and list explicit next actions without hidden scans; costly details require a server budget profile.",
    "value.size": "Measure bounded value dimensions and report accuracy and measurement cost.",
    "value.preview": "Return a bounded wire-safe preview without transferring the complete value.",
    "value.get": "Create a proxy for one named or indexed child without materializing the parent.",
    "value.select": "Create a bounded projected proxy for selected fields or columns.",
    "value.snapshot": "Create a bounded Python snapshot proxy while retaining source provenance.",
    "value.materialize": "Materialize a bounded 1C value into a native child-owned Python object proxy.",
    "value.to_df": "Materialize a bounded tabular 1C value into a child-owned pandas DataFrame proxy.",
    "value.compare": "Compare two same-realm proxies under an explicit bounded policy.",
    "value.release": "Release a proxy handle without deleting its named notebook binding.",
    "python.variables": "List all user-visible Python outputs; imports and intermediate locals remain private.",
    "python.run": "Run Python with proxy inputs and publish only explicitly declared output names.",
    "python.inspect": "Return bounded Python type, shape, dtype, memory and scalar-preview metadata.",
    "python.imports": "List allowlisted Python packages and their installed versions.",
    "python.reset": "Clear the managed Python namespace and invalidate its current proxy generation.",
    "python.status": "Return managed Python generation, health, variable count and resource-limit guarantees.",
}

_AGENT_TOOL_DESCRIPTIONS = {
    "workspace.open": "Open and select the notebook workspace the agent will inspect and execute.",
    "workspace.status": "Get the selected workspace, runtime lifecycle, and safe next actions.",
    "workspace.variables": "List current BSL and Python variable proxies with provenance; values are not copied.",
    "runtime.ensure": "Join the existing durable startup or ensure one compatible runtime; returns a ready descriptor or AgentOperationView.",
    "runtime.restart": "Abort the current generation and start a replacement, invalidating generation-bound proxies.",
    "runtime.close": "Explicitly abort and close the selected runtime generation.",
    "code.list": "Select a notebook and list immutable cells so the agent can choose an exact revision.",
    "code.get": "Read one exact immutable cell revision, source hash, metadata, and provenance.",
    "code.put": "Save a new cell revision using revision and document-hash conflict fences.",
    "code.run": "Run one exact saved BSL MAIN revision with optional observations; returns AgentOperationView.",
    "code.run_inline": "Run unsaved BSL MAIN code with optional observations; returns AgentOperationView without editing the notebook.",
    "operation.wait": "Wait for durable progress and return the same AgentOperationView shape with messages and outputs.",
    "value.inspect": "Inspect proxy metadata cheaply; explicit detail uses a named server-owned cost profile.",
    "value.materialize": "Materialize one proxy into managed Python under the fixed dataframe-sized server budget.",
    "value.to_df": "Convert one tabular proxy to a managed pandas DataFrame under the fixed server budget.",
    "python.run": "Run Python over proxy inputs and publish only explicitly named output proxies.",
    "capture.run_until": "Run one exact MAIN revision until a source-level capture point or terminal result; mismatched stops never continue automatically.",
    "capture.inspect": "Read a filtered, paged capture-frame metadata index; unselected temporary-table handles expose schema and size only, never rows.",
    "capture.stack": "On request, page the paused native RDBG call stack; runtime kernel locations and raw URLs are hidden.",
    "capture.frame": "On request, page names and types in one native paused RDBG frame. Naming one local refreshes its bounded RDBG presentation and collection size when available; staged CAPTURE changes are available through capture.inspect.",
    "capture.hypothesis": "Run one exact CAPTURE revision and remain paused; an unselected table preview uses one bounded server-owned head selection, while Python/DataFrame requests require explicit bounded table_rows.",
    "capture.continue": "Flush staged frame roots exactly once, continue one active capture fence, and optionally observe only a newly correlated next stop.",
}

_AGENT_SERVER_TOOLS: dict[int, frozenset[str]] = {}


def _validate_tool_names() -> None:
    if len(_TOOL_NAMES) != len(set(_TOOL_NAMES)):
        raise ValueError("duplicate MCP tool name")
    if not set(_TOOL_NAMES) <= set(_TOOL_ANNOTATIONS) or not set(_TOOL_NAMES) <= set(
        _TOOL_DESCRIPTIONS
    ):
        raise ValueError("MCP tool catalogs do not match")
    if not set(_AGENT_TOOL_DESCRIPTIONS) >= set(tool_names(McpProfile.AGENT)):
        raise ValueError("agent MCP description catalog does not match")
    if set(_AGENT_TOOL_DESCRIPTIONS) != set(tool_names(McpProfile.CAPTURE)):
        raise ValueError("capture MCP description catalog does not match")


def _mcp_tool(server: MCPServer, name: str):  # type: ignore[no-untyped-def]
    return server.tool(
        name=name,
        description=_TOOL_DESCRIPTIONS[name],
        annotations=_TOOL_ANNOTATIONS[name],
        structured_output=True,
    )


async def _call(client: ServiceClient, method: str, arguments: dict[str, object]) -> dict[str, object]:
    try:
        response = await asyncio.to_thread(client.call, method, arguments)
    except Exception:
        response = ServiceResponse.fail(
            MethodFailure(
                category=FailureCategory.PLATFORM_FAILURE,
                state_changed=StateChanged.UNKNOWN,
                safe_to_retry=RetrySafety.AFTER_STATUS_CHECK,
                current_state={},
                diagnostic_id=f"diag_{uuid4().hex}",
            )
        )
    return cast(dict[str, object], to_wire(response))


async def _call_facade(
    function: Callable[[dict[str, object]], object],
    arguments: dict[str, object],
) -> dict[str, object]:
    try:
        value = await asyncio.to_thread(function, arguments)
        response = ServiceResponse.success(value)
    except AgentFacadeError as error:
        response = (
            ServiceResponse.fail(error.failure)
            if error.failure is not None
            else _platform_failure()
        )
    except (TypeError, ValueError):
        response = ServiceResponse.fail(
            MethodFailure(
                category=FailureCategory.INVALID_REQUEST,
                state_changed=StateChanged.NO,
                safe_to_retry=RetrySafety.NO,
                current_state={},
                diagnostic_id=f"diag_{uuid4().hex}",
            )
        )
    except Exception:
        response = _platform_failure()
    return cast(dict[str, object], to_wire(response))


_CAPTURE_FAILURE_STAGES = frozenset(
    {
        "request",
        "capture_preparation",
        "capture_arming",
        "capture_correlation",
        "unexpected_breakpoint",
        "capture_disarm",
        "capture_transport",
        "capture_writeback",
        "capture_staging",
        "namespace_publication",
        "observation",
        "execution",
        "capture_hypothesis_transport",
    }
)
_PUBLIC_PARTIAL_STATUSES = frozenset(
    {
        "unattempted",
        "failed",
        "sent",
        "succeeded",
        "outcome_unknown",
        "unavailable",
        "unknown",
        "captured",
        "completed",
        "stale",
    }
)
_PUBLIC_RUNTIME_STATES = frozenset(
    {"unknown", "captured", "completed", "failed", "absent", "closing"}
)
_PRIVATE_MARKERS = (
    "private",
    "secret",
    "token",
    "rdbg",
    "prepared",
    "catalog",
    "pid",
)


def _contains_private_marker(value: str) -> bool:
    folded = value.casefold()
    return any(marker in folded for marker in _PRIVATE_MARKERS)


def _capture_recovery(value: object) -> list[dict[str, object]]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[dict[str, object]] = []
    for raw in value[:100]:
        if not isinstance(raw, dict):
            continue
        method = raw.get("method")
        arguments = raw.get("arguments")
        if not isinstance(arguments, dict):
            continue
        if method == "workspace.status" and not arguments:
            result.append({"method": method, "arguments": {}})
        elif method == "runtime.close" and arguments == {
            "policy": "abort_generation"
        }:
            result.append({"method": method, "arguments": dict(arguments)})
        elif method == "runtime.restart" and arguments == {
            "policy": "abort_generation"
        }:
            result.append({"method": method, "arguments": dict(arguments)})
        elif method == "runtime.ensure" and (
            arguments.get("mode") in {"observe", "experiment", "commit", "admin"}
            and arguments.get("profile") == "default"
            and set(arguments) == {"mode", "profile"}
        ):
            result.append({"method": method, "arguments": dict(arguments)})
        elif method == "operation.wait":
            operation_id = arguments.get("operation_id")
            timeout_s = arguments.get("timeout_s")
            after_event_cursor = arguments.get("after_event_cursor")
            after_message_cursor = arguments.get("after_message_cursor")
            if (
                set(arguments)
                == {
                    "operation_id",
                    "timeout_s",
                    "after_event_cursor",
                    "after_message_cursor",
                }
                and isinstance(operation_id, str)
                and 0 < len(operation_id) <= 256
                and not _contains_private_marker(operation_id)
                and all(
                    character.isascii()
                    and (character.isalnum() or character in "-_.")
                    for character in operation_id
                )
                and type(timeout_s) in {int, float}
                and 0 <= timeout_s <= 30
                and type(after_event_cursor) is int
                and after_event_cursor >= 0
                and type(after_message_cursor) is int
                and after_message_cursor >= 0
            ):
                result.append(
                    {
                        "method": method,
                        "arguments": dict(arguments),
                    }
                )
    return result


def _capture_failure(value: object) -> dict[str, object]:
    raw = value if isinstance(value, dict) else {}
    category_value = raw.get("category")
    try:
        category = FailureCategory(category_value)
    except (TypeError, ValueError):
        category = FailureCategory.UNKNOWN
    retry_value = raw.get("safe_to_retry")
    try:
        retryable = RetrySafety(retry_value)
    except (TypeError, ValueError):
        retryable = RetrySafety.AFTER_STATUS_CHECK
    state_value = raw.get("state_changed")
    try:
        state_changed = StateChanged(state_value)
    except (TypeError, ValueError):
        state_changed = StateChanged.UNKNOWN
    current = raw.get("current_state")
    current_state = current if isinstance(current, dict) else {}
    raw_stage = current_state.get("stage")
    stage = (
        raw_stage
        if isinstance(raw_stage, str) and raw_stage in _CAPTURE_FAILURE_STAGES
        else "request"
    )
    raw_code = raw.get("diagnostic_id")
    code = f"capture-{category.value}"
    if isinstance(raw_code, str) and raw_code.startswith("diag_"):
        suffix = raw_code.removeprefix("diag_")
        if len(suffix) == 32 and all(
            character in "0123456789abcdef" for character in suffix
        ):
            code = raw_code
    partial: dict[str, str] = {}
    raw_partial = raw.get("partial_results")
    if isinstance(raw_partial, dict):
        for key, item in tuple(raw_partial.items())[:100]:
            if (
                not isinstance(key, str)
                or not key.isidentifier()
                or len(key) > 256
                or _contains_private_marker(key)
            ):
                continue
            partial[key] = (
                item
                if isinstance(item, str) and item in _PUBLIC_PARTIAL_STATUSES
                else "unavailable"
            )
    details: list[dict[str, str]] = []
    runtime_state = current_state.get("runtime_state")
    if isinstance(runtime_state, str) and runtime_state in _PUBLIC_RUNTIME_STATES:
        details.append({"name": "runtime_state", "value": runtime_state})
    raw_continue_state = current_state.get("continue_state")
    continue_state = (
        raw_continue_state
        if isinstance(raw_continue_state, str)
        and raw_continue_state
        in {"unattempted", "planned", "sent", "acknowledged", "outcome_unknown"}
        else None
    )
    return {
        "category": category.value,
        "code": code,
        "retryable": retryable.value,
        "state_changed": state_changed.value,
        "stage": stage,
        "message": "Capture request failed; follow the listed recovery actions.",
        "details": details,
        "partial_results": partial,
        "continue_state": continue_state,
        "recovery": _capture_recovery(current_state.get("recovery")),
    }


async def _call_capture_facade(
    function: Callable[[dict[str, object]], object],
    arguments: dict[str, object],
) -> dict[str, object]:
    response = await _call_facade(function, arguments)
    if response.get("ok") is True:
        return {"ok": True, "value": response.get("value"), "failure": None}
    return {
        "ok": False,
        "value": None,
        "failure": _capture_failure(response.get("failure")),
    }


def _platform_failure() -> ServiceResponse:
    return ServiceResponse.fail(
        MethodFailure(
            category=FailureCategory.PLATFORM_FAILURE,
            state_changed=StateChanged.UNKNOWN,
            safe_to_retry=RetrySafety.AFTER_STATUS_CHECK,
            current_state={},
            diagnostic_id=f"diag_{uuid4().hex}",
        )
    )


def _expert_platform_failure() -> _ExpertFailureResponse:
    """Construct a fresh fixed failure without reflecting rejected input."""
    return _ExpertFailureResponse.model_validate(to_wire(_platform_failure()))


def _agent_tool(server: MCPServer, name: str):  # type: ignore[no-untyped-def]
    if name not in _AGENT_SERVER_TOOLS[id(server)]:
        return lambda function: function
    return server.tool(
        name=name,
        description=_AGENT_TOOL_DESCRIPTIONS[name],
        annotations=_TOOL_ANNOTATIONS[name],
        structured_output=True,
    )


def _create_agent_mcp_server(client: ServiceClient, selected_tools: frozenset[str]) -> MCPServer:
    server = MCPServer("onec-interactive-runtime")
    _AGENT_SERVER_TOOLS[id(server)] = selected_tools
    facade = AgentFacade(client)

    @_agent_tool(server, "workspace.open")
    async def workspace_open(project: _TEXT | None = None) -> dict[str, object]:
        return await _call_facade(
            facade.workspace_open,
            {} if project is None else {"project": project},
        )

    @_agent_tool(server, "workspace.status")
    async def workspace_status() -> dict[str, object]:
        return await _call_facade(facade.workspace_status, {})

    @_agent_tool(server, "workspace.variables")
    async def workspace_variables(
        namespace: Literal["all", "bsl", "python"] = "all",
    ) -> dict[str, object]:
        return await _call_facade(
            facade.workspace_variables, {"namespace": namespace}
        )

    @_agent_tool(server, "runtime.ensure")
    async def runtime_ensure(
        mode: _MODE | None = None,
        profile: _TEXT | None = None,
    ) -> _AgentEnsureResponse:
        arguments: dict[str, object] = {}
        if mode is not None:
            arguments["mode"] = mode
        if profile is not None:
            arguments["profile"] = profile
        return _AgentEnsureResponse.model_validate(
            await _call_facade(facade.runtime_ensure, arguments)
        )

    @_agent_tool(server, "runtime.restart")
    async def runtime_restart(
        policy: Literal["abort_generation"],
        mode: _MODE | None = None,
    ) -> dict[str, object]:
        arguments: dict[str, object] = {"policy": policy}
        if mode is not None:
            arguments["mode"] = mode
        return await _call_facade(facade.runtime_restart, arguments)

    @_agent_tool(server, "runtime.close")
    async def runtime_close(
        policy: Literal["abort_generation"],
    ) -> dict[str, object]:
        return await _call_facade(facade.runtime_close, {"policy": policy})

    @_agent_tool(server, "code.list")
    async def code_list(
        container: _TEXT,
        language: CodeLanguage | None = None,
        mode: _CODE_MODE | None = None,
    ) -> dict[str, object]:
        filters: dict[str, object] = {}
        if language is not None:
            filters["language"] = language
        if mode is not None:
            filters["mode"] = mode
        arguments: dict[str, object] = {"container": container}
        if filters:
            arguments["filters"] = filters
        return await _call_facade(facade.code_list, arguments)

    @_agent_tool(server, "code.get")
    async def code_get(
        cell_id: _TEXT,
        revision: _POSITIVE | None = None,
    ) -> dict[str, object]:
        arguments: dict[str, object] = {"cell_id": cell_id}
        if revision is not None:
            arguments["revision"] = revision
        return await _call_facade(facade.code_get, arguments)

    @_agent_tool(server, "code.put")
    async def code_put(
        cell_id: _TEXT,
        source: _SOURCE,
        language: CodeLanguage,
        mode: _CODE_MODE,
        expected_revision: _POSITIVE,
        expected_document_sha256: _HASH,
        outputs: list[_TEXT] | None = None,
    ) -> dict[str, object]:
        arguments: dict[str, object] = {
            "cell_id": cell_id,
            "source": source,
            "language": language,
            "mode": mode,
            "expected_revision": expected_revision,
            "expected_document_sha256": expected_document_sha256,
        }
        if outputs is not None:
            arguments["outputs"] = outputs
        return await _call_facade(facade.code_put, arguments)

    @_agent_tool(server, "code.run")
    async def code_run(
        cell_id: _TEXT,
        revision: _POSITIVE,
        source_sha256: _HASH,
        inputs: dict[str, str] | None = None,
        outputs: list[_TEXT] | None = None,
        observe: _ObservationPlanWire | None = None,
        wait_s: _WAIT_SECONDS = 0,
    ) -> _AgentOperationResponse:
        arguments: dict[str, object] = {
            "cell_id": cell_id,
            "revision": revision,
            "source_sha256": source_sha256,
            "inputs": inputs or {},
            "wait_s": wait_s,
        }
        if outputs is not None:
            arguments["outputs"] = outputs
        if observe is not None:
            arguments["observe"] = observe.model_dump(exclude_none=True)
        return _AgentOperationResponse.model_validate(
            await _call_facade(facade.code_run, arguments)
        )

    @_agent_tool(server, "code.run_inline")
    async def code_run_inline(
        language: Literal["bsl"],
        mode: Literal["main"],
        source: _SOURCE,
        inputs: dict[str, str] | None = None,
        outputs: list[_TEXT] | None = None,
        observe: _ObservationPlanWire | None = None,
        wait_s: _WAIT_SECONDS = 0,
    ) -> _AgentOperationResponse:
        arguments: dict[str, object] = {
            "language": language,
            "mode": mode,
            "source": source,
            "inputs": inputs or {},
            "wait_s": wait_s,
        }
        if outputs is not None:
            arguments["outputs"] = outputs
        if observe is not None:
            arguments["observe"] = observe.model_dump(exclude_none=True)
        return _AgentOperationResponse.model_validate(
            await _call_facade(facade.code_run_inline, arguments)
        )

    @_agent_tool(server, "operation.wait")
    async def operation_wait(
        operation_id: _TEXT,
        timeout_s: _WAIT_SECONDS = 0,
        after_event_cursor: _CURSOR = 0,
        after_message_cursor: _CURSOR = 0,
    ) -> _AgentOperationResponse:
        return _AgentOperationResponse.model_validate(
            await _call_facade(
                facade.operation_wait,
                {
                    "operation_id": operation_id,
                    "timeout_s": timeout_s,
                    "after_event_cursor": after_event_cursor,
                    "after_message_cursor": after_message_cursor,
                },
            )
        )

    @_agent_tool(server, "value.inspect")
    async def value_inspect(
        proxy_id: _TEXT,
        detail: Literal["auto", "metadata", "size", "preview"] = "auto",
        budget_profile: Literal[
            "agent_metadata", "agent_preview", "agent_dataframe"
        ] = "agent_metadata",
    ) -> dict[str, object]:
        return await _call_facade(
            facade.value_inspect,
            {
                "proxy_id": proxy_id,
                "detail": detail,
                "budget_profile": budget_profile,
            },
        )

    @_agent_tool(server, "value.materialize")
    async def value_materialize(
        proxy_id: _TEXT,
        budget_profile: Literal["agent_dataframe"] = "agent_dataframe",
        refs: Literal["presentation", "uuid", "both"] = "presentation",
    ) -> dict[str, object]:
        return await _call_facade(
            facade.value_materialize,
            {"proxy_id": proxy_id, "budget_profile": budget_profile, "refs": refs},
        )

    @_agent_tool(server, "value.to_df")
    async def value_to_df(
        proxy_id: _TEXT,
        budget_profile: Literal["agent_dataframe"] = "agent_dataframe",
        columns: list[_TEXT] | None = None,
        refs: Literal["presentation", "uuid", "both"] = "presentation",
    ) -> dict[str, object]:
        arguments: dict[str, object] = {
            "proxy_id": proxy_id,
            "budget_profile": budget_profile,
            "refs": refs,
        }
        if columns is not None:
            arguments["columns"] = columns
        return await _call_facade(facade.value_to_df, arguments)

    @_agent_tool(server, "python.run")
    async def python_run(
        code: _SOURCE,
        inputs: dict[str, str],
        outputs: list[_TEXT],
    ) -> dict[str, object]:
        return await _call_facade(
            facade.python_run,
            {"code": code, "inputs": inputs, "outputs": outputs},
        )

    @_agent_tool(server, "capture.run_until")
    async def capture_run_until(
        cell_id: _TEXT,
        revision: _POSITIVE,
        source_sha256: _HASH,
        points: Annotated[
            list[_CapturePointWire], Field(min_length=1, max_length=32)
        ],
        request_id: _TEXT,
        wait_s: _WAIT_SECONDS = 30,
    ) -> _CaptureOperationResponse:
        return _CaptureOperationResponse.model_validate(
            await _call_capture_facade(
                facade.capture_run_until,
                {
                    "cell_id": cell_id,
                    "revision": revision,
                    "source_sha256": source_sha256,
                    "points": [item.model_dump(exclude_none=True) for item in points],
                    "wait_s": wait_s,
                    "request_id": request_id,
                },
            )
        )

    @_agent_tool(server, "capture.inspect")
    async def capture_inspect(
        fence: _CaptureFenceWire,
        filters: _CaptureInspectFiltersWire | None = None,
        cursor: _CURSOR = 0,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        observe: _CaptureInspectObservationPlanWire | None = None,
    ) -> _CaptureInspectionResponse:
        arguments: dict[str, object] = {
            "fence": fence.model_dump(),
            "filters": {} if filters is None else filters.model_dump(exclude_none=True),
            "cursor": cursor,
            "limit": limit,
        }
        if observe is not None:
            arguments["observe"] = observe.model_dump(exclude_none=True)
        return _CaptureInspectionResponse.model_validate(
            await _call_capture_facade(facade.capture_inspect, arguments)
        )

    @_agent_tool(server, "capture.stack")
    async def capture_stack(
        fence: _CaptureFenceWire,
        cursor: _CURSOR = 0,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> _CaptureStackResponse:
        return _CaptureStackResponse.model_validate(
            await _call_capture_facade(
                facade.capture_stack,
                {"fence": fence.model_dump(), "cursor": cursor, "limit": limit},
            )
        )

    @_agent_tool(server, "capture.frame")
    async def capture_frame(
        fence: _CaptureFenceWire,
        level: _CURSOR,
        cursor: _CURSOR = 0,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        name: Annotated[str, Field(min_length=1, max_length=256)] | None = None,
    ) -> _CaptureFrameResponse:
        arguments: dict[str, object] = {
            "fence": fence.model_dump(), "level": level,
            "cursor": cursor, "limit": limit,
        }
        if name is not None:
            arguments["name"] = name
        return _CaptureFrameResponse.model_validate(
            await _call_capture_facade(facade.capture_frame, arguments)
        )

    @_agent_tool(server, "capture.hypothesis")
    async def capture_hypothesis(
        fence: _CaptureFenceWire,
        code_ref: _CaptureCodeRefWire,
        request_id: _TEXT,
        observe: _CaptureHypothesisObservationPlanWire | None = None,
        wait_s: _WAIT_SECONDS = 30,
    ) -> _CaptureOperationResponse:
        arguments: dict[str, object] = {
            "fence": fence.model_dump(),
            "code_ref": code_ref.model_dump(),
            "request_id": request_id,
            "wait_s": wait_s,
        }
        if observe is not None:
            arguments["observe"] = observe.model_dump(exclude_none=True)
        return _CaptureOperationResponse.model_validate(
            await _call_capture_facade(facade.capture_hypothesis, arguments)
        )

    @_agent_tool(server, "capture.continue")
    async def capture_continue(
        fence: _CaptureFenceWire,
        request_id: _TEXT,
        next_points: list[_CapturePointWire] = Field(default_factory=list, max_length=32),
        observe: _CaptureHypothesisObservationPlanWire | None = None,
        wait_s: _WAIT_SECONDS = 30,
    ) -> _CaptureOperationResponse:
        arguments: dict[str, object] = {
            "fence": fence.model_dump(),
            "next_points": [item.model_dump(exclude_none=True) for item in next_points],
            "request_id": request_id,
            "wait_s": wait_s,
        }
        if observe is not None:
            arguments["observe"] = observe.model_dump(exclude_none=True)
        return _CaptureOperationResponse.model_validate(
            await _call_capture_facade(facade.capture_continue, arguments)
        )

    return server


def create_mcp_server(
    client: ServiceClient,
    *,
    profile: McpProfile = McpProfile.AGENT,
) -> MCPServer:
    """Create an explicit MCP tool palette over an already-running service."""
    _validate_tool_names()
    try:
        normalized_profile = (
            profile if isinstance(profile, McpProfile) else McpProfile(profile)
        )
    except (TypeError, ValueError) as error:
        raise ValueError("unknown MCP profile") from error
    if normalized_profile in {McpProfile.AGENT, McpProfile.CAPTURE}:
        return _create_agent_mcp_server(client, tool_names(normalized_profile))
    server = MCPServer("onec-interactive-runtime")
    selected_tools = tool_names(normalized_profile)
    if selected_tools != frozenset(_TOOL_NAMES):
        raise ValueError("expert MCP profile must expose the complete Domain catalog")

    @_mcp_tool(server, "workspace.open")
    async def workspace_open(project: _TEXT | None = None) -> dict[str, object]:
        return await _call(client, "workspace.open", {} if project is None else {"project": project})

    @_mcp_tool(server, "workspace.status")
    async def workspace_status() -> dict[str, object]:
        return await _call(client, "workspace.status", {})

    @_mcp_tool(server, "workspace.capabilities")
    async def workspace_capabilities() -> dict[str, object]:
        return await _call(client, "workspace.capabilities", {})

    @_mcp_tool(server, "workspace.close")
    async def workspace_close(policy: Literal["detach", "abort_generation"]) -> dict[str, object]:
        return await _call(client, "workspace.close", {"policy": policy})

    @_mcp_tool(server, "workspace.variables")
    async def workspace_variables(namespace: Literal["all", "bsl", "python"] = "all") -> dict[str, object]:
        return await _call(client, "workspace.variables", {"namespace": namespace})

    @_mcp_tool(server, "workspace.variable")
    async def workspace_variable(qualified_name: _TEXT) -> dict[str, object]:
        return await _call(client, "workspace.variable", {"qualified_name": qualified_name})

    @_mcp_tool(server, "workspace.variable_history")
    async def workspace_variable_history(qualified_name: _TEXT) -> dict[str, object]:
        return await _call(client, "workspace.variable_history", {"qualified_name": qualified_name})

    @_mcp_tool(server, "workspace.snapshot_variables")
    async def workspace_snapshot_variables(names: list[_TEXT], budget: dict[str, int | float]) -> dict[str, object]:
        return await _call(client, "workspace.snapshot_variables", {"names": names, "budget": budget})

    @_mcp_tool(server, "workspace.delete_variables")
    async def workspace_delete_variables(names: list[_TEXT], expected_versions: dict[str, int]) -> dict[str, object]:
        return await _call(client, "workspace.delete_variables", {"names": names, "expected_versions": expected_versions})

    @_mcp_tool(server, "runtime.start")
    async def runtime_start(mode: _MODE | None = None, profile: _TEXT | None = None) -> dict[str, object]:
        arguments: dict[str, object] = {}
        if mode is not None:
            arguments["mode"] = mode
        if profile is not None:
            arguments["profile"] = profile
        return await _call(client, "runtime.start", arguments)

    @_mcp_tool(server, "runtime.ensure")
    async def runtime_ensure(mode: _MODE | None = None, profile: _TEXT | None = None) -> dict[str, object]:
        arguments: dict[str, object] = {}
        if mode is not None:
            arguments["mode"] = mode
        if profile is not None:
            arguments["profile"] = profile
        return await _call(client, "runtime.ensure", arguments)

    @_mcp_tool(server, "runtime.list")
    async def runtime_list() -> dict[str, object]:
        return await _call(client, "runtime.list", {})

    @_mcp_tool(server, "runtime.select")
    async def runtime_select(runtime_id: _TEXT) -> dict[str, object]:
        return await _call(client, "runtime.select", {"runtime_id": runtime_id})

    @_mcp_tool(server, "runtime.status")
    async def runtime_status(runtime_id: _TEXT | None = None) -> dict[str, object]:
        return await _call(client, "runtime.status", {} if runtime_id is None else {"runtime_id": runtime_id})

    @_mcp_tool(server, "runtime.request_mode")
    async def runtime_request_mode(mode: _MODE) -> dict[str, object]:
        return await _call(client, "runtime.request_mode", {"mode": mode})

    @_mcp_tool(server, "runtime.restart")
    async def runtime_restart(policy: Literal["abort_generation"], mode: _MODE | None = None) -> dict[str, object]:
        arguments: dict[str, object] = {"policy": policy}
        if mode is not None:
            arguments["mode"] = mode
        return await _call(client, "runtime.restart", arguments)

    @_mcp_tool(server, "runtime.close")
    async def runtime_close(policy: Literal["abort_generation"]) -> dict[str, object]:
        return await _call(client, "runtime.close", {"policy": policy})

    @_mcp_tool(server, "code.list")
    async def code_list(container: _TEXT, language: CodeLanguage | None = None, mode: _CODE_MODE | None = None) -> dict[str, object]:
        filters: dict[str, object] = {}
        if language is not None:
            filters["language"] = language
        if mode is not None:
            filters["mode"] = mode
        arguments: dict[str, object] = {"container": container}
        if filters:
            arguments["filters"] = filters
        return await _call(client, "code.list", arguments)

    @_mcp_tool(server, "code.get")
    async def code_get(cell_id: _TEXT, revision: _POSITIVE | None = None) -> dict[str, object]:
        arguments: dict[str, object] = {"cell_id": cell_id}
        if revision is not None:
            arguments["revision"] = revision
        return await _call(client, "code.get", arguments)

    @_mcp_tool(server, "code.put")
    async def code_put(cell_id: _TEXT, source: _SOURCE, language: CodeLanguage, mode: _CODE_MODE, expected_revision: _POSITIVE, expected_document_sha256: _HASH, outputs: list[_TEXT] | None = None) -> dict[str, object]:
        arguments: dict[str, object] = {"cell_id": cell_id, "source": source, "language": language, "mode": mode, "expected_revision": expected_revision, "expected_document_sha256": expected_document_sha256}
        if outputs is not None:
            arguments["outputs"] = outputs
        return await _call(client, "code.put", arguments)

    @_mcp_tool(server, "code.diff")
    async def code_diff(cell_id: _TEXT, left: _POSITIVE, right: _POSITIVE) -> dict[str, object]:
        return await _call(client, "code.diff", {"cell_id": cell_id, "left": left, "right": right})

    @_mcp_tool(server, "code.history")
    async def code_history(cell_id: _TEXT) -> dict[str, object]:
        return await _call(client, "code.history", {"cell_id": cell_id})

    @_mcp_tool(server, "code.run")
    async def code_run(cell_id: _TEXT, revision: _POSITIVE, source_sha256: _HASH, inputs: dict[str, str] | None = None, outputs: list[_TEXT] | None = None, wait_s: _WAIT_SECONDS = 0) -> dict[str, object]:
        arguments: dict[str, object] = {"cell_id": cell_id, "revision": revision, "source_sha256": source_sha256, "inputs": inputs or {}, "wait_s": wait_s}
        if outputs is not None:
            arguments["outputs"] = outputs
        return await _call(client, "code.run", arguments)

    @_mcp_tool(server, "code.run_inline")
    async def code_run_inline(language: CodeLanguage, mode: _CODE_MODE, source: _SOURCE, inputs: dict[str, str] | None = None, outputs: list[_TEXT] | None = None, wait_s: _WAIT_SECONDS = 0) -> dict[str, object]:
        arguments: dict[str, object] = {"language": language, "mode": mode, "source": source, "inputs": inputs or {}, "wait_s": wait_s}
        if outputs is not None:
            arguments["outputs"] = outputs
        return await _call(client, "code.run_inline", arguments)

    @_mcp_tool(server, "code.promote")
    async def code_promote(operation_id: _TEXT, cell_id: _TEXT, expected_revision: _POSITIVE, expected_document_sha256: _HASH) -> dict[str, object]:
        return await _call(client, "code.promote", {"operation_id": operation_id, "cell_id": cell_id, "expected_revision": expected_revision, "expected_document_sha256": expected_document_sha256})

    @_mcp_tool(server, "code.delete")
    async def code_delete(cell_id: _TEXT, expected_revision: _POSITIVE, expected_document_sha256: _HASH) -> dict[str, object]:
        return await _call(client, "code.delete", {"cell_id": cell_id, "expected_revision": expected_revision, "expected_document_sha256": expected_document_sha256})

    @_mcp_tool(server, "operation.list")
    async def operation_list(runtime_id: _TEXT | None = None, runtime_generation: _POSITIVE | None = None, state: AgentOperationState | None = None) -> dict[str, object]:
        filters: dict[str, object] = {}
        if runtime_id is not None:
            filters["runtime_id"] = runtime_id
        if runtime_generation is not None:
            filters["runtime_generation"] = runtime_generation
        if state is not None:
            filters["state"] = state
        return await _call(client, "operation.list", {} if not filters else {"filters": filters})

    @_mcp_tool(server, "operation.status")
    async def operation_status(operation_id: _TEXT) -> dict[str, object]:
        return await _call(client, "operation.status", {"operation_id": operation_id})

    @_mcp_tool(server, "operation.wait")
    async def operation_wait(operation_id: _TEXT, timeout_s: _WAIT_SECONDS = 0, after_cursor: _CURSOR | None = None) -> dict[str, object]:
        arguments: dict[str, object] = {"operation_id": operation_id, "timeout_s": timeout_s}
        if after_cursor is not None:
            arguments["after_cursor"] = after_cursor
        return await _call(client, "operation.wait", arguments)

    @_mcp_tool(server, "operation.output")
    async def operation_output(operation_id: _TEXT, after_cursor: _CURSOR = 0, messages: _MESSAGE_LIMIT | None = None) -> dict[str, object]:
        arguments: dict[str, object] = {"operation_id": operation_id, "after_cursor": after_cursor}
        if messages is not None:
            arguments["limits"] = {"messages": messages}
        return await _call(client, "operation.output", arguments)

    @_mcp_tool(server, "operation.result")
    async def operation_result(operation_id: _TEXT) -> dict[str, object]:
        return await _call(client, "operation.result", {"operation_id": operation_id})

    @_mcp_tool(server, "operation.stop_waiting")
    async def operation_stop_waiting(operation_id: _TEXT) -> dict[str, object]:
        return await _call(client, "operation.stop_waiting", {"operation_id": operation_id})

    @_mcp_tool(server, "operation.abort_generation")
    async def operation_abort_generation(operation_id: _TEXT) -> dict[str, object]:
        return await _call(client, "operation.abort_generation", {"operation_id": operation_id})

    @_mcp_tool(server, "operation.explain_failure")
    async def operation_explain_failure(
        operation_id: _TEXT,
    ) -> _ExpertFailureResponse:
        response = await _call(
            client,
            "operation.explain_failure",
            {"operation_id": operation_id},
        )
        if not isinstance(response, dict) or response.get("ok") is not True:
            return _expert_platform_failure()
        try:
            return _ExpertFailureResponse.model_validate(response)
        except Exception:
            # Validation exceptions may embed rejected values. Return a fresh
            # compact failure rather than reflecting private input in MCP text.
            return _expert_platform_failure()

    @_mcp_tool(server, "value.describe")
    async def value_describe(proxy_id: _TEXT) -> dict[str, object]:
        return await _call(client, "value.describe", {"proxy_id": proxy_id})

    @_mcp_tool(server, "value.inspect")
    async def value_inspect(
        proxy_id: _TEXT,
        detail: Literal["auto", "metadata", "size", "preview"] = "auto",
        budget_profile: Literal["agent_metadata", "agent_preview", "agent_dataframe"] = "agent_metadata",
    ) -> dict[str, object]:
        return await _call(
            client,
            "value.inspect",
            {"proxy_id": proxy_id, "detail": detail, "budget_profile": budget_profile},
        )

    @_mcp_tool(server, "value.size")
    async def value_size(proxy_id: _TEXT, budget: dict[str, int | float] | None = None) -> dict[str, object]:
        arguments: dict[str, object] = {"proxy_id": proxy_id}
        if budget is not None:
            arguments["budget"] = budget
        return await _call(client, "value.size", arguments)

    @_mcp_tool(server, "value.preview")
    async def value_preview(proxy_id: _TEXT, items: Annotated[int, Field(ge=1, le=100)] = 20, bytes: Annotated[int, Field(ge=1, le=16384)] = 4096) -> dict[str, object]:
        return await _call(client, "value.preview", {"proxy_id": proxy_id, "limits": {"items": items, "bytes": bytes}})

    @_mcp_tool(server, "value.get")
    async def value_get(proxy_id: _TEXT, selector: str | int) -> dict[str, object]:
        return await _call(client, "value.get", {"proxy_id": proxy_id, "selector": selector})

    @_mcp_tool(server, "value.select")
    async def value_select(proxy_id: _TEXT, fields: list[_TEXT]) -> dict[str, object]:
        return await _call(client, "value.select", {"proxy_id": proxy_id, "fields": fields})

    @_mcp_tool(server, "value.snapshot")
    async def value_snapshot(proxy_id: _TEXT, budget: dict[str, int | float]) -> dict[str, object]:
        return await _call(client, "value.snapshot", {"proxy_id": proxy_id, "budget": budget})

    @_mcp_tool(server, "value.materialize")
    async def value_materialize(proxy_id: _TEXT, budget: dict[str, int | float], refs: Literal["presentation", "uuid", "both"] = "presentation") -> dict[str, object]:
        return await _call(client, "value.materialize", {"proxy_id": proxy_id, "target": "python", "policy": {"refs": refs}, "budget": budget})

    @_mcp_tool(server, "value.to_df")
    async def value_to_df(proxy_id: _TEXT, budget: dict[str, int | float], columns: list[_TEXT] | None = None, refs: Literal["presentation", "uuid", "both"] = "presentation") -> dict[str, object]:
        return await _call(client, "value.to_df", {"proxy_id": proxy_id, "columns": columns, "refs": refs, "budget": budget})

    @_mcp_tool(server, "value.compare")
    async def value_compare(left_proxy_id: _TEXT, right_proxy_id: _TEXT, budget: dict[str, int | float], policy: dict[str, str] | None = None) -> dict[str, object]:
        return await _call(client, "value.compare", {"left_proxy_id": left_proxy_id, "right_proxy_id": right_proxy_id, "policy": policy or {}, "budget": budget})

    @_mcp_tool(server, "value.release")
    async def value_release(proxy_id: _TEXT) -> dict[str, object]:
        return await _call(client, "value.release", {"proxy_id": proxy_id})

    @_mcp_tool(server, "python.variables")
    async def python_variables() -> dict[str, object]:
        return await _call(client, "python.variables", {})

    @_mcp_tool(server, "python.run")
    async def python_run(code: _SOURCE, inputs: dict[str, str], outputs: list[_TEXT]) -> dict[str, object]:
        return await _call(client, "python.run", {"code": code, "inputs": inputs, "outputs": outputs})

    @_mcp_tool(server, "python.inspect")
    async def python_inspect(proxy_id: _TEXT) -> dict[str, object]:
        return await _call(client, "python.inspect", {"proxy_id": proxy_id})

    @_mcp_tool(server, "python.imports")
    async def python_imports() -> dict[str, object]:
        return await _call(client, "python.imports", {})

    @_mcp_tool(server, "python.reset")
    async def python_reset(policy: Literal["clear"]) -> dict[str, object]:
        return await _call(client, "python.reset", {"policy": policy})

    @_mcp_tool(server, "python.status")
    async def python_status() -> dict[str, object]:
        return await _call(client, "python.status", {})

    return server
