from __future__ import annotations

from contextlib import AbstractContextManager, contextmanager, nullcontext
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from hashlib import sha256
from inspect import Parameter, signature
from math import isfinite
from pathlib import Path
from threading import Lock, get_ident, local
from time import monotonic
from types import MappingProxyType
from typing import Callable, Iterator, Protocol
import re
from uuid import UUID, uuid4
from weakref import WeakKeyDictionary

import pandas as pd

from onec_runtime.capture_evaluation import (
    CaptureEvaluationCoordinator,
    CaptureEvaluationTicket,
    CapturePhase,
    _CaptureSubmission,
)
from onec_runtime.capture_inspection import CaptureView

from onec_runtime.bsl import (
    DiagnosticStage,
    LoweringMode,
    NormalizedDiagnostic,
    ParsedModuleModel,
    ResolvedModulePlan,
    SemanticLoweringError,
    SemanticLoweringResult,
    SemanticNotebookLowerer,
    VisibleSourceContext,
    WorkerExport,
    normalize_source_error,
    parse_platform_diagnostic,
    resolve_worker_dependencies,
    worker_model_candidate_names,
)
from onec_runtime.bsl.full_ast_worker_projection import (
    full_ast_parser_identity,
    parse_full_ast_module,
)
from onec_runtime.bsl.diagnostics import (
    WorkerDiagnosticArtifact,
    remap_worker_runtime_diagnostic,
)
from onec_runtime.bsl.lexer import BslLexError
from onec_runtime.bsl.module_universe import (
    CommonModuleCatalogSnapshot,
    WorkerModuleUnit,
    lower_resolved_worker_module,
)
from onec_runtime.bsl.module_catalog import SessionCommonModuleCatalog
from onec_runtime.bsl.module_syntax import (
    ModuleIdentity,
    ModuleSyntaxIndex,
    ModuleSyntaxRegistry,
)
from onec_runtime.bsl.parser_target import BslParseError
from onec_runtime.bsl.notebook_cells import NotebookCellProjection
from onec_runtime.bsl.notebook_method_globals import bind_notebook_method_globals
from onec_runtime.bsl.notebook_methods import (
    NotebookMethodSet,
    instrument_notebook_worker_messages,
    merge_notebook_methods,
)
from onec_runtime.bsl.source_maps import (
    MappedSource,
    SourceArtifactKind,
    SourceSpan,
    SourceTransformBuilder,
    SourceUnitKind,
    SourceUnitRef,
    mapped_visible_source,
    source_sha256,
)
from onec_runtime.observation import ManagerOrigin, SelectionKind, ValueSelection
from onec_runtime.runtime_contracts import OperationExecutionProvenance
from onec_runtime.compact_table import decode_compact_table_payload
from onec_runtime.compact_table_backend import (
    CompactRuntimeTableTransfer,
    infer_compact_columns,
    infer_declared_compact_columns,
)
from onec_runtime.errors import (
    BslExecutionError,
    CaptureBusyError,
    CaptureOutcomeUnknownError,
    CaptureRecoveryRequiredError,
    NoActiveCaptureError,
    PoisonedRuntimeError,
    ProtocolError,
    StaleCaptureError,
    StaleWorkerGeneration,
    WorkerPromotionOutcomeUnknown,
)
from onec_runtime.breakpoint_workspace import (
    BreakpointWorkspaceController,
    BreakpointWorkspaceOutcomeUnknown,
    WorkspaceSnapshot,
)
from onec_runtime.experiment import bsl_string_literal
from onec_runtime.prototype_runtime import (
    CaptureCellResult,
    CapturedStop,
    DebugStop,
    MainCompletion,
    OperationState,
    ContinuationAttemptEvidence,
    ContinuationAttemptSpec,
)
from onec_runtime.performance_profile import PhaseRecorder
from onec_runtime.rdbg.models import ModuleLocation
from onec_runtime.recovery_journal import RecoveryJournal
from onec_runtime.server_worker import (
    NotebookWorkerArtifactBuilder,
    WorkerArtifact,
    remap_worker_artifact_stage_error,
    validate_production_worker_artifact,
)
from onec_runtime.table_materialization import ReferenceMode, ReferencePolicy
from onec_runtime.worker_universe import (
    OperationGenerationPin,
    ServerWorkerUniverseRegistry,
    WorkerGenerationHandle,
    WorkerModuleArtifact,
    WorkerModuleArtifactBuilder,
    WorkerUniverseCandidate,
    WorkerUniverseRegistry,
    worker_module_artifact_from_notebook,
)
from onec_runtime.worker_breakpoints import (
    RuntimeDebugStop,
    WorkerBreakpointConflict,
    WorkerBreakpointCoordinator,
    WorkerBreakpointPlan,
    WorkerBreakpointReloadOutcome,
    WorkerBreakpointReloadPolicy,
    WorkerBreakpointReloadReport,
    WorkerBreakpointStatus,
    WorkerSourceLocation,
    map_worker_stop,
)
from onec_runtime.value_materialization import (
    MaterializationOptions,
    decode_value_payload,
)
from onec_runtime.value_transfer_backend import (
    RuntimeValueTransfer,
    validate_value_handle,
)


MAX_PROJECTION_POSITION = 10_000_000
_BSL_EXECUTION_FAILURE_SUMMARY = "BSL execution failed"
_RESERVED_WORKER_ROOT_CONTEXT_SLOT = "RuntimeWorkerPinnedOperationGeneration"


def _no_capture_primary_execution() -> None:
    return None


def _identity_capture_error(error: BslExecutionError) -> BslExecutionError:
    return error


def _accepts_capture_execution_callbacks(callback: Callable[..., object]) -> bool:
    try:
        parameters = signature(callback).parameters.values()
    except (TypeError, ValueError):
        return True
    names = {parameter.name for parameter in parameters}
    return (
        any(parameter.kind is Parameter.VAR_KEYWORD for parameter in parameters)
        or {"primary_execution", "normalize_error"} <= names
    )


@dataclass(frozen=True, slots=True)
class _ActiveWorkerModule:
    unit: WorkerModuleUnit
    model: ParsedModuleModel
    plan: ResolvedModulePlan
    semantic_plan_key: tuple[object, ...]
    artifact: WorkerModuleArtifact


class RuntimeReplyKind(Enum):
    SOURCE_FAILED = "source_failed"
    MAIN_COMPLETED = "main_completed"
    CAPTURED = "captured"
    CAPTURE_CELL = "capture_cell"
    DEBUG_STOPPED = "debug_stopped"
    WORKER_LOADED = "worker_loaded"


@dataclass(frozen=True, slots=True)
class RuntimeReply:
    kind: RuntimeReplyKind
    operation_id: int
    state: OperationState
    result: object = None
    error: str = ""
    succeeded: bool = True
    location: ModuleLocation | WorkerSourceLocation | None = None
    stop_sequence: int | None = None
    messages: tuple[str, ...] = ()
    changed_roots: tuple[str, ...] = ()
    capture_ticket: str | None = None
    observed_command_id: int | None = None
    capture_dirty_roots: tuple[str, ...] = ()
    diagnostic: NormalizedDiagnostic | None = None
    debug_stop: RuntimeDebugStop | None = None


class _PreparedMainExecutionAttempt:
    """Redacted RuntimeApi evidence for one phase-two execution attempt."""

    __slots__ = ("__reply", "__error", "__user_main_dispatched")

    def __init__(
        self,
        *,
        reply: RuntimeReply | None = None,
        error: BaseException | None = None,
        user_main_dispatched: bool,
    ) -> None:
        if type(user_main_dispatched) is not bool:
            raise TypeError("user MAIN dispatch evidence must be an exact boolean")
        if (reply is None) == (error is None):
            raise TypeError("prepared MAIN attempt requires exactly one outcome")
        if reply is not None and not isinstance(reply, RuntimeReply):
            raise TypeError("prepared MAIN attempt reply is invalid")
        if error is not None and not isinstance(error, BaseException):
            raise TypeError("prepared MAIN attempt error is invalid")
        object.__setattr__(self, "_PreparedMainExecutionAttempt__reply", reply)
        object.__setattr__(self, "_PreparedMainExecutionAttempt__error", error)
        object.__setattr__(
            self,
            "_PreparedMainExecutionAttempt__user_main_dispatched",
            user_main_dispatched,
        )

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("prepared MAIN execution attempts are immutable")

    def __repr__(self) -> str:
        return "<redacted prepared main execution attempt>"

    def __deepcopy__(self, memo: dict[int, object]) -> str:
        del memo
        return "<redacted prepared main execution attempt>"

    @property
    def user_main_dispatched(self) -> bool:
        return self.__user_main_dispatched

    def reply(self) -> RuntimeReply:
        if self.__error is not None:
            raise self.__error
        assert self.__reply is not None
        return self.__reply


@dataclass(frozen=True, slots=True)
class OperationSourceMapBundle:
    """Private mapped branches bound to one visible operation identity."""

    visible: SourceUnitRef
    worker_candidate: MappedSource | None = field(repr=False)
    statement_execution: MappedSource | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class CaptureCorrelationTicket:
    """Opaque runtime-owned evidence for one armed next-MAIN capture."""

    ticket_id: str
    expected_operation_id: int
    expected_stop_sequence: int


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    state: OperationState
    runtime_generation: int
    operation_id: int
    worker_generation: WorkerGenerationHandle | None


@dataclass(frozen=True, slots=True)
class RuntimeNamespaceSnapshot:
    runtime_generation: int
    context_generation: int
    names: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.runtime_generation <= 0 or self.context_generation <= 0:
            raise ValueError("runtime namespace generations must be positive")
        if any(not name.strip() for name in self.names):
            raise ValueError("runtime namespace name must not be empty")
        normalized = tuple(name.casefold() for name in self.names)
        if len(set(normalized)) != len(normalized):
            raise ValueError("runtime namespace names must be case-insensitively unique")


class _PreparedCaptureHypothesis:
    """Sealed exact-lowering admission owned by one RuntimeApi instance."""

    __slots__ = (
        "__weakref__",
        "__owner",
        "__token",
        "__source",
        "__source_sha256",
        "__lowered_sha256",
        "__lowering",
        "__lowerer",
        "__catalog_identity",
        "__worker_identity",
        "__operation_pin",
        "__controller_fence",
        "__context_before",
        "__message_collector_key",
        "__execution_provenance",
    )

    def __init__(
        self,
        *,
        owner: object,
        token: str,
        source: str,
        lowering: SemanticLoweringResult,
        lowerer: SemanticNotebookLowerer,
        catalog_identity: tuple[tuple[str, str, str | None], ...],
        worker_identity: WorkerGenerationHandle | None,
        operation_pin: OperationGenerationPin | None,
        controller_fence: tuple[int, int, int, int],
        context_before: tuple[str, ...],
        message_collector_key: str,
        execution_provenance: OperationExecutionProvenance,
    ) -> None:
        object.__setattr__(self, "_PreparedCaptureHypothesis__owner", owner)
        object.__setattr__(self, "_PreparedCaptureHypothesis__token", token)
        object.__setattr__(self, "_PreparedCaptureHypothesis__source", source)
        object.__setattr__(
            self,
            "_PreparedCaptureHypothesis__source_sha256",
            sha256(source.encode("utf-8")).hexdigest(),
        )
        object.__setattr__(
            self,
            "_PreparedCaptureHypothesis__lowered_sha256",
            sha256(lowering.source.encode("utf-8")).hexdigest(),
        )
        object.__setattr__(self, "_PreparedCaptureHypothesis__lowering", lowering)
        object.__setattr__(self, "_PreparedCaptureHypothesis__lowerer", lowerer)
        object.__setattr__(
            self,
            "_PreparedCaptureHypothesis__catalog_identity",
            catalog_identity,
        )
        object.__setattr__(
            self,
            "_PreparedCaptureHypothesis__worker_identity",
            worker_identity,
        )
        object.__setattr__(
            self,
            "_PreparedCaptureHypothesis__operation_pin",
            operation_pin,
        )
        object.__setattr__(
            self,
            "_PreparedCaptureHypothesis__controller_fence",
            controller_fence,
        )
        object.__setattr__(
            self,
            "_PreparedCaptureHypothesis__context_before",
            context_before,
        )
        object.__setattr__(
            self,
            "_PreparedCaptureHypothesis__message_collector_key",
            message_collector_key,
        )
        object.__setattr__(
            self,
            "_PreparedCaptureHypothesis__execution_provenance",
            execution_provenance,
        )

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("prepared capture hypotheses are immutable")

    def __repr__(self) -> str:
        return "<redacted prepared capture hypothesis>"

    def __deepcopy__(self, memo: dict[int, object]) -> str:
        del memo
        return "<redacted prepared capture hypothesis>"

    def contents(
        self, owner: object
    ) -> tuple[
        str,
        str,
        str,
        str,
        SemanticLoweringResult,
        SemanticNotebookLowerer,
        tuple[tuple[str, str, str | None], ...],
        WorkerGenerationHandle | None,
        OperationGenerationPin | None,
        tuple[int, int, int, int],
        tuple[str, ...],
        str,
    ]:
        if self.__owner is not owner:
            raise ProtocolError("Runtime API requires an owned prepared capture")
        return (
            self.__token,
            self.__source,
            self.__source_sha256,
            self.__lowered_sha256,
            self.__lowering,
            self.__lowerer,
            self.__catalog_identity,
            self.__worker_identity,
            self.__operation_pin,
            self.__controller_fence,
            self.__context_before,
            self.__message_collector_key,
        )

    def execution_provenance(
        self,
        owner: object,
    ) -> OperationExecutionProvenance:
        if self.__owner is not owner:
            raise ProtocolError("Runtime API requires an owned prepared capture")
        return self.__execution_provenance


@dataclass(frozen=True, slots=True, repr=False)
class _PreparedMainPayload:
    token: str
    source: str
    source_sha256: str
    source_unit: SourceUnitRef
    statement_source: MappedSource | None
    statement_identity: tuple[str, str] | None
    worker_artifact: WorkerArtifact | None
    worker_identity: tuple[str, str, str | None] | None
    candidate_catalog: tuple[WorkerExport, ...] | None
    prepared_worker_catalog: tuple[WorkerExport, ...] | None
    lowering: SemanticLoweringResult | None
    lowering_identity: tuple[str, str] | None
    lowerer: SemanticNotebookLowerer
    catalog_identity: tuple[tuple[str, str, str | None], ...]
    active_worker: WorkerGenerationHandle | None
    controller_fence: tuple[int, int, OperationState]
    context_before: tuple[str, ...]
    namespace_before: tuple[str, ...]
    message_collector_key: str
    visible_source_context: VisibleSourceContext
    execution_provenance: OperationExecutionProvenance
    operation_pin: OperationGenerationPin | None
    preview_worker: WorkerGenerationHandle | None
    method_set_before: NotebookMethodSet | None
    method_set_candidate: NotebookMethodSet | None


class _PreparedMainExecution:
    """Private one-use MAIN preparation owned by one RuntimeApi instance."""

    __slots__ = ("__owner", "__payload", "__weakref__")

    def __init__(self, owner: object, payload: _PreparedMainPayload) -> None:
        object.__setattr__(self, "_PreparedMainExecution__owner", owner)
        object.__setattr__(self, "_PreparedMainExecution__payload", payload)

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("prepared MAIN executions are immutable")

    def __repr__(self) -> str:
        return "<redacted prepared main execution>"

    def __deepcopy__(self, memo: dict[int, object]) -> str:
        del memo
        return "<redacted prepared main execution>"

    def contents(self, owner: object) -> _PreparedMainPayload:
        if self.__owner is not owner:
            raise ProtocolError("Runtime API requires an owned prepared main")
        return self.__payload


@dataclass(frozen=True, slots=True, repr=False)
class _ActivatedMainPayload:
    token: str
    prepared: _PreparedMainPayload
    controller_fence: tuple[int, int, OperationState]
    catalog_identity: tuple[tuple[str, str, str | None], ...]
    active_worker: WorkerGenerationHandle | None
    worker_exports: tuple[WorkerExport, ...]
    context_before: tuple[str, ...]
    namespace_before: tuple[str, ...]
    operation_pin: OperationGenerationPin | None
    lowering: SemanticLoweringResult | None
    lowering_identity: tuple[str, str] | None


class _ActivatedPreparedMainExecution:
    """Private one-use MAIN execution resealed after Worker activation."""

    __slots__ = ("__owner", "__payload", "__weakref__")

    def __init__(self, owner: object, payload: _ActivatedMainPayload) -> None:
        object.__setattr__(
            self, "_ActivatedPreparedMainExecution__owner", owner
        )
        object.__setattr__(
            self, "_ActivatedPreparedMainExecution__payload", payload
        )

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("activated MAIN executions are immutable")

    def __repr__(self) -> str:
        return "<redacted activated main execution>"

    def __deepcopy__(self, memo: dict[int, object]) -> str:
        del memo
        return "<redacted activated main execution>"

    def contents(self, owner: object) -> _ActivatedMainPayload:
        if self.__owner is not owner:
            raise ProtocolError("Runtime API requires an owned activated main")
        return self.__payload


class RuntimeController(Protocol):
    runtime_generation: int
    operation_id: int
    state: OperationState

    def inspect_completion_fields(
        self, handle: str, *, table_row: bool
    ) -> EvaluationResult: ...

    def execute_main(
        self,
        source: str,
        *,
        capture_points: tuple[ModuleLocation, ...] = (),
        user_breakpoints: tuple[ModuleLocation, ...] = (),
        on_transport_dispatch: Callable[[], None] | None = None,
    ) -> MainCompletion | CapturedStop | DebugStop: ...

    def execute_capture(
        self,
        source: str,
        *,
        on_transport_dispatch: Callable[[], None] | None = None,
    ) -> CaptureCellResult | DebugStop: ...

    def execute_mapped_main(
        self,
        visible_source: str,
        lowered_source: MappedSource,
        *,
        visible_source_context: VisibleSourceContext,
        messages_intercepted: int = 0,
        message_collector_key: str = "",
        worker_messages: bool = False,
        capture_points: tuple[ModuleLocation, ...] = (),
        user_breakpoints: tuple[ModuleLocation, ...] = (),
        on_transport_dispatch: Callable[[], None] | None = None,
    ) -> MainCompletion | CapturedStop | DebugStop: ...

    def execute_mapped_capture(
        self,
        visible_source: str,
        lowered_source: MappedSource,
        *,
        visible_source_context: VisibleSourceContext,
        messages_intercepted: int = 0,
        message_collector_key: str = "",
        worker_messages: bool = False,
        dirty_roots: tuple[str, ...] = (),
        on_transport_dispatch: Callable[[], None] | None = None,
    ) -> CaptureCellResult | DebugStop: ...

    def execute_system_main(self, source: str) -> MainCompletion: ...

    def execute_system_capture(
        self, source: str
    ) -> CaptureCellResult | DebugStop: ...

    def install_capture_worker_generation_pin(
        self,
        manifest_sha256: str,
    ) -> None: ...

    def clear_capture_worker_generation_pin(self) -> None: ...

    def take_context_string(self, key: str, *, max_text_size: int) -> str: ...

    def drop_context_value(self, key: str) -> None: ...

    def resume(
        self,
        *,
        dirty_roots: tuple[str, ...] = (),
        continuation_attempt_id: str | None = None,
        on_transport_dispatch: Callable[[], None] | None = None,
    ) -> MainCompletion | CapturedStop | DebugStop: ...

    def resume_debug_stop(
        self,
        *,
        on_transport_dispatch: Callable[[], None] | None = None,
    ) -> MainCompletion | CapturedStop | CaptureCellResult | DebugStop: ...

    def rearm_capture_successor(
        self, capture_points: tuple[ModuleLocation, ...]
    ) -> None: ...

    def begin_continuation_admission(
        self,
        attempt: ContinuationAttemptSpec,
        capture_points: tuple[ModuleLocation, ...],
    ) -> object: ...

    def continuation_attempt_evidence(
        self, attempt_id: str
    ) -> ContinuationAttemptEvidence: ...

    def capture_frame_variables(
        self, *, filters: Mapping[str, object], cursor: int, limit: int,
        timeout_s: float | None = None,
    ) -> Mapping[str, object]: ...

    def capture_stack(
        self, *, cursor: int, limit: int, timeout_s: float | None = None,
    ) -> Mapping[str, object]: ...

    def capture_frame(
        self, *, level: int, cursor: int, limit: int,
        name: str | None = None, timeout_s: float | None = None,
    ) -> Mapping[str, object]: ...

    def resolve_capture_manager_origin(
        self, root: str, fields: tuple[str, ...], *,
        timeout_s: float | None = None,
    ) -> Mapping[str, object]: ...

    def capture_temporary_tables(
        self,
        manager_handle: str,
        *,
        names: tuple[str, ...] | None,
        cursor: int,
        limit: int,
        selection: Mapping[str, object] | None,
        timeout_s: float | None = None,
    ) -> Mapping[str, object]: ...

    def capture_value_handle(self, handle: str) -> str: ...

    def is_capture_metadata_handle(self, handle: str) -> bool: ...

    def invalidate_capture_inspection(self) -> None: ...


class _SyntheticOriginStringWorkerBuilderAdapter:
    """Keep injected source-only builders off the mapped production path."""

    def __init__(
        self,
        builder: Callable[[str, tuple[WorkerExport, ...]], WorkerArtifact],
    ) -> None:
        self._builder = builder

    def __call__(
        self,
        source: MappedSource,
        exports: tuple[WorkerExport, ...],
        *,
        visible_source_context: VisibleSourceContext | None = None,
    ) -> WorkerArtifact:
        del visible_source_context
        return self._builder(source.text, exports)


class _RuntimeContinuationAdmission:
    def __init__(
        self,
        api: "PrototypeRuntimeApi",
        controller: object,
        *,
        capture_points: tuple[ModuleLocation, ...],
        capture_ticket: CaptureCorrelationTicket | None,
        inspection_quarantined: bool,
        ticket: CaptureCorrelationTicket | None,
    ) -> None:
        self._api = api
        self._controller = controller
        self._capture_points = capture_points
        self._capture_ticket = capture_ticket
        self._inspection_quarantined = inspection_quarantined
        self.ticket = ticket
        self._closed = False

    def commit(self) -> None:
        if not self._closed:
            commit = getattr(self._controller, "commit")
            commit()
            self._closed = True

    def rollback(self) -> None:
        if self._closed:
            return
        rollback = getattr(self._controller, "rollback")
        try:
            rollback()
        except BaseException:
            with self._api._single_writer():
                self._api._capture_ticket = None
                self._api._capture_inspection_quarantined = True
            self._closed = True
            raise
        with self._api._single_writer():
            self._api._capture_points = self._capture_points
            self._api._capture_ticket = self._capture_ticket
            self._api._capture_inspection_quarantined = (
                self._inspection_quarantined
            )
        self._closed = True

    def quarantine(self) -> None:
        if self._closed:
            return
        try:
            quarantine = getattr(self._controller, "quarantine")
            quarantine()
        finally:
            with self._api._single_writer():
                self._api._capture_ticket = None
                self._api._capture_inspection_quarantined = True
            self._closed = True


@dataclass(slots=True, repr=False)
class _PreparedCaptureExecution:
    """Prepared controller call with an explicit, one-way ownership handoff.

    The synchronous controller retains its existing behavior. A coordinator
    adapter consumes execute_owned and then owns pin disposition and mandatory
    completion independently of the waiting caller.
    """

    execute: Callable[[], object]
    detach_pin: Callable[[], Callable[[str], None]]
    completion: Callable[[object, BaseException | None], object]
    release_writer: Callable[[], AbstractContextManager[None]]
    release_waiter: Callable[[], AbstractContextManager[None]] = nullcontext
    primary_execution: Callable[[], None] = _no_capture_primary_execution
    normalize_error: Callable[[BslExecutionError], BslExecutionError] = (
        _identity_capture_error
    )
    transferred: bool = False
    rejection: Callable[[BaseException], object] | None = None
    submitted: bool = False

    def execute_sync(self) -> object:
        if self.transferred:
            raise ProtocolError("Prepared CAPTURE execution was already transferred")
        return self.execute()

    def execute_owned(self, submit: Callable[..., CaptureEvaluationTicket]) -> object:
        if self.transferred:
            raise ProtocolError("Prepared CAPTURE execution was already transferred")
        submission = _CaptureSubmission()
        lease = self.detach_pin()
        self.transferred = True
        with self.release_writer():
            try:
                ownership: dict[str, object] = {
                    "pin_lease": lease,
                    "completion": self.completion,
                }
                if _accepts_capture_execution_callbacks(submit):
                    ownership.update(
                        primary_execution=self.primary_execution,
                        normalize_error=self.normalize_error,
                    )
                ticket = submission.submit(submit, **ownership)
                self.submitted = True
                with self.release_waiter():
                    return ticket.wait_initiator()
            except BaseException as error:
                if submission.ticket is not None:
                    self.submitted = True
                    submission.detach_initiator()
                    raise
                try:
                    if self.rejection is not None:
                        self.rejection(error)
                    else:
                        self.completion(None, error)
                finally:
                    lease("release")
                raise


class PrototypeRuntimeApi:
    """Single-writer frontend boundary over the proven prototype Controller."""

    _WORKER_STATES = frozenset(
        {
            OperationState.IDLE,
            OperationState.COMPLETED,
            OperationState.FAILED,
            OperationState.CAPTURED,
        }
    )
    _MAIN_READY_STATES = frozenset(
        {
            OperationState.IDLE,
            OperationState.COMPLETED,
            OperationState.FAILED,
        }
    )

    def __init__(
        self,
        controller: RuntimeController,
        *,
        capture_points: tuple[ModuleLocation, ...] = (),
        user_breakpoints: tuple[ModuleLocation, ...] = (),
        context_generation: int = 1,
        journal: RecoveryJournal | None = None,
        worker_instruction_executor: Callable[[str], object] | None = None,
        notebook_worker_builder: Callable[[str, tuple[WorkerExport, ...]], WorkerArtifact]
        | NotebookWorkerArtifactBuilder
        | None = None,
        worker_module_builder: WorkerModuleArtifactBuilder | object | None = None,
    ) -> None:
        self._controller = controller
        self._capture_points = tuple(capture_points)
        self._capture_ticket: CaptureCorrelationTicket | None = None
        self._capture_inspection_quarantined = False
        self._user_breakpoints = tuple(user_breakpoints)
        self._lock = Lock()
        self._writer_owner: int | None = None
        self._worker_exports: tuple[WorkerExport, ...] = ()
        self._poisoned_error: ProtocolError | None = None
        self._closed = False
        self._context_generation = context_generation
        self._pending_dirty_roots: dict[str, str] = {}
        self._active_command_deadline: float | None = None
        self._prepared_capture_owner = object()
        self._consumed_capture_preparations: set[str] = set()
        self._prepared_main_owner = object()
        self._consumed_main_preparations: set[str] = set()
        self._prepared_source_units: WeakKeyDictionary[object, SourceUnitRef] = WeakKeyDictionary()
        lowerer = getattr(controller, "lowerer", None)
        initial_names = getattr(lowerer, "persistent_names", ())
        self._namespace_names = (
            initial_names
            if isinstance(initial_names, tuple)
            and all(isinstance(name, str) for name in initial_names)
            else ()
        )
        self._pending_namespace_names: tuple[str, ...] | None = None
        self._notebook_worker_builder = (
            notebook_worker_builder
            if notebook_worker_builder is None
            or isinstance(notebook_worker_builder, NotebookWorkerArtifactBuilder)
            else _SyntheticOriginStringWorkerBuilderAdapter(notebook_worker_builder)
        )
        self._worker_module_builder = worker_module_builder
        self._worker_module_artifacts: dict[
            tuple[str, str, int, str, str], WorkerModuleArtifact
        ] = {}
        self._worker_active_modules: dict[str, _ActiveWorkerModule] = {}
        self._module_syntax_registry = ModuleSyntaxRegistry()
        self._worker_syntax_generations: dict[
            WorkerGenerationHandle, Mapping[str, ModuleSyntaxIndex]
        ] = {}
        self._worker_catalog_snapshot: CommonModuleCatalogSnapshot | None = None
        self._notebook_worker_revision = 0
        self._notebook_method_set: NotebookMethodSet | None = None
        self._notebook_worker_descriptor: WorkerModuleArtifact | None = None
        self._anonymous_notebook_id = f"anonymous-notebook-{uuid4().hex}"
        self._anonymous_notebook_revision = 0
        self._worker_generation_handle: WorkerGenerationHandle | None = None
        # The registry gives each confirmed publication one explicit retention.
        # This marker identifies the one owned by this public API, rather than
        # treating every historical return value as an unbounded owner.
        self._api_owned_worker_generation_handle: WorkerGenerationHandle | None = None
        self._worker_breakpoints = WorkerBreakpointCoordinator(session_id=uuid4())
        self._last_worker_breakpoint_reload_report: (
            WorkerBreakpointReloadReport | None
        ) = None
        self._worker_generation_diagnostics: dict[
            str,
            tuple[WorkerDiagnosticArtifact, ...],
        ] = {}
        self._operation_generation_pin: OperationGenerationPin | None = None
        self._preparing_generation_pin: OperationGenerationPin | None = None
        self._evaluation_generation_pin: OperationGenerationPin | None = None
        self._generation_lock = Lock()
        self._evaluation_pin_lock = self._generation_lock
        self._session_waiter_handoffs = local()
        self._worker_instruction_executor = (
            worker_instruction_executor or self._execute_worker_instruction
        )
        self._worker_journal = journal or RecoveryJournal()
        self._worker_universe = WorkerUniverseRegistry(
            runtime_generation=lambda: self._controller.runtime_generation,
            context_generation=lambda: self._context_generation,
        )
        self._worker_universe_target = ServerWorkerUniverseRegistry(
            self._worker_universe,
            self._worker_instruction_executor,
        )

    @property
    def worker_generation_handle(self) -> WorkerGenerationHandle | None:
        return self._worker_generation_handle

    @property
    def module_syntax_registry(self) -> ModuleSyntaxRegistry:
        """Shared exact-version source facts; publication alone is not activation."""
        return self._module_syntax_registry

    def _worker_module_identity(self, unit: WorkerModuleUnit) -> ModuleIdentity:
        return ModuleIdentity(
            namespace=self._anonymous_notebook_id,
            source_kind="worker",
            module_kind=unit.kind,
            object_id=unit.logical_name.casefold(),
            property_id="Module",
        )

    def _worker_module_syntax(
        self, logical_name: str, *, generation: WorkerGenerationHandle | None = None,
    ) -> ModuleSyntaxIndex | None:
        """Select syntax through the physical frame's generation or MAIN pin.

        This internal lookup does no parsing or transport. Capture callers must
        still own the runtime single-writer scope, validate their stop fence
        and apply strict Worker source mapping.
        """
        handle = (
            generation or self.operation_worker_generation or self._worker_generation_handle
        )
        return self._worker_syntax_generations.get(handle, {}).get(logical_name.casefold())

    @property
    def operation_worker_generation(self) -> WorkerGenerationHandle | None:
        pin = self._operation_generation_pin or self._preparing_generation_pin
        return None if pin is None else pin.handle

    def status(self) -> RuntimeStatus:
        owner = self._capture_control_owner()
        capture_status = None if owner is None else owner.status(owner._fence)
        capture_controls_state = (
            capture_status is not None
            and (
                capture_status.phase is not CapturePhase.PAUSED
                or self._controller.state is OperationState.CAPTURED
            )
        )
        if not capture_controls_state or self._closed:
            with self._single_writer():
                self._require_available()
                return RuntimeStatus(
                    self._controller.state,
                    self._controller.runtime_generation,
                    self._controller.operation_id,
                    self._worker_generation_handle,
                )
        state = self._controller.state
        assert capture_status is not None
        state = {
            CapturePhase.PAUSED: OperationState.CAPTURED,
            CapturePhase.EVALUATING: OperationState.EVALUATING_CAPTURE,
            CapturePhase.RESUMING: OperationState.RESUMING,
            CapturePhase.OUTCOME_UNKNOWN: OperationState.RECOVERING,
            CapturePhase.RECOVERY_REQUIRED: OperationState.RECOVERING,
        }.get(capture_status.phase, state)
        with self._generation_lock:
            worker_generation = self._worker_generation_handle
        return RuntimeStatus(
            state,
            capture_status.capture_generation,
            capture_status.operation_id,
            worker_generation,
        )

    def current_capture(self) -> CaptureView:
        owner = self._capture_control_owner()
        if owner is None:
            state = getattr(self._controller, "state", None)
            raise NoActiveCaptureError(
                state.value if isinstance(state, OperationState) else None
            )
        fence = owner._fence

        def is_current() -> bool:
            return (
                self._capture_control_owner() is owner
                and self._controller.operation_id == fence.operation_id
                and self._controller.runtime_generation == fence.capture_generation
                and getattr(self._controller, "stop_sequence", None)
                == fence.stop_sequence
            )

        return CaptureView(
            fence.operation_id,
            fence.capture_generation,
            fence.stop_sequence,
            is_current,
            lambda: owner.status(fence),
            lambda timeout_s, evaluation_id: owner.wait(
                fence,
                evaluation_id,
                timeout_s,
            ),
        )

    def _capture_control_owner(self) -> CaptureEvaluationCoordinator | None:
        owner = getattr(self._controller, "_capture_evaluation_coordinator", None)
        return owner if isinstance(owner, CaptureEvaluationCoordinator) else None

    def _require_capture_data_plane_admission(self) -> None:
        owner = self._capture_control_owner()
        if owner is None:
            return
        status = owner.status(owner._fence)
        if status.phase is CapturePhase.PAUSED:
            return
        if status.phase is CapturePhase.EVALUATING:
            assert status.pending_evaluation_id is not None
            assert status.evaluation_kind is not None
            raise CaptureBusyError(
                status.pending_evaluation_id,
                status.evaluation_kind,
                status.phase,
            )
        if status.phase is CapturePhase.OUTCOME_UNKNOWN:
            raise CaptureOutcomeUnknownError(
                status.last_evaluation_id,
                status.failure,
            )
        if status.phase is CapturePhase.RECOVERY_REQUIRED:
            raise CaptureRecoveryRequiredError(status.failure)
        if status.phase is CapturePhase.STALE:
            raise StaleCaptureError()
        raise ProtocolError(f"CAPTURE data plane is unavailable ({status.phase.value})")

    def namespace_snapshot(self) -> RuntimeNamespaceSnapshot:
        with self._single_writer():
            self._require_available()
            return RuntimeNamespaceSnapshot(
                self._controller.runtime_generation,
                self._context_generation,
                self._namespace_names,
            )

    def completion_fields(
        self, handle: str, *, table_row: bool = False, timeout_s: float = 1.0
    ) -> tuple[str, ...]:
        """Read current field names only; never evaluate caller-provided expressions."""
        with self._single_writer(), self._bounded_command_timeout(timeout_s):
            self._require_available()
            if self._controller.state not in self._WORKER_STATES:
                raise ProtocolError("Completion requires an idle or captured runtime")
            self._require_capture_inspection_available()
            if (
                not isinstance(handle, str)
                or len(handle) > 512
                or not re.fullmatch(r"Контекст\.[^\W\d]\w*(?:\.[^\W\d]\w*){0,7}", handle)
                or type(table_row) is not bool
            ):
                raise ProtocolError("Completion requires a direct or dotted Context path")
            if handle.split(".")[1].casefold() not in {
                name.casefold() for name in self._namespace_names
            }:
                raise ProtocolError("Completion root is not in the current namespace")
            # The schema helper admits only table/structure types and excludes
            # private Worker container shapes. Do not run the general MAIN
            # privacy instruction here: completion must preserve operation state.
            self._public_value_guard_handle_locked(handle)
            with self._remaining_command_timeout():
                result = self._controller.inspect_completion_fields(handle, table_row=table_row)
            if result.error_occurred or len(result.collection_rows) > 128:
                raise ProtocolError("Invalid completion field schema")
            names: list[str] = []
            seen: set[str] = set()
            for row in result.collection_rows:
                if len(row.cells) != 1 or row.cells[0].name != "Имя":
                    raise ProtocolError("Invalid completion field schema")
                name = row.cells[0].value_string
                if (not isinstance(name, str) or len(name) > 128
                        or not re.fullmatch(r"[^\W\d]\w*", name)
                        or name.casefold() in seen):
                    raise ProtocolError("Invalid completion field name")
                seen.add(name.casefold())
                names.append(name)
            return tuple(names)

    def continuation_admission_is_uncertain(self) -> bool:
        """Report only whether a failed paused admission is safe to restore."""
        with self._single_writer():
            return (
                self._controller.state is OperationState.RECOVERING
                or self._capture_inspection_quarantined
            )

    def configure_capture_points(
        self,
        locations: tuple[ModuleLocation, ...],
    ) -> None:
        with self._single_writer():
            self._require_available()
            if self._controller.state not in self._MAIN_READY_STATES:
                raise ProtocolError(
                    "Capture points can only be configured while runtime is main-ready; "
                    f"current state is {self._controller.state.value}"
                )
            points = tuple(locations)
            if any(not isinstance(point, ModuleLocation) for point in points):
                raise ProtocolError("capture point must be a ModuleLocation")
            if any(point.line <= 0 for point in points):
                raise ProtocolError("capture point line must be positive")
            if len(set(points)) != len(points):
                raise ProtocolError("duplicate capture point")
            self._capture_points = points
            self._capture_ticket = None

    def configure_continuation_capture_points(
        self, locations: tuple[ModuleLocation, ...]
    ) -> None:
        """Atomically replace only the successor capture points while paused."""
        with self._single_writer():
            self._require_available()
            if self._controller.state is not OperationState.CAPTURED:
                raise ProtocolError("Continuation capture points require a captured runtime")
            points = tuple(locations)
            if any(not isinstance(point, ModuleLocation) for point in points):
                raise ProtocolError("capture point must be a ModuleLocation")
            if any(point.line <= 0 for point in points) or len(set(points)) != len(points):
                raise ProtocolError("continuation capture points are invalid")
            self._controller.rearm_capture_successor(points)
            self._capture_points = points
            self._capture_ticket = None

    def add_worker_breakpoint(
        self,
        source_unit: SourceUnitRef,
        canonical_module: str,
        line: int,
        *,
        enabled: bool = True,
        column: int | None = None,
    ) -> WorkerBreakpointStatus:
        with self._single_writer():
            self._require_worker_breakpoint_mutation_boundary_locked()
            plan = self._worker_breakpoints.prepare_add(
                source_unit,
                canonical_module,
                line,
                enabled=enabled,
                column=column,
            )
            self._apply_worker_breakpoint_plan_locked(plan)
            return self._worker_breakpoints.status(plan.result_id)

    def remove_worker_breakpoint(self, breakpoint_id: UUID) -> None:
        with self._single_writer():
            self._require_worker_breakpoint_mutation_boundary_locked()
            plan = self._worker_breakpoints.prepare_remove(breakpoint_id)
            self._apply_worker_breakpoint_plan_locked(plan)

    def set_worker_breakpoint_enabled(
        self,
        breakpoint_id: UUID,
        enabled: bool,
    ) -> WorkerBreakpointStatus:
        with self._single_writer():
            self._require_worker_breakpoint_mutation_boundary_locked()
            plan = self._worker_breakpoints.prepare_enabled(
                breakpoint_id,
                enabled,
            )
            self._apply_worker_breakpoint_plan_locked(plan)
            return self._worker_breakpoints.status(breakpoint_id)

    def worker_breakpoint_status(
        self,
        breakpoint_id: UUID,
    ) -> WorkerBreakpointStatus:
        with self._single_writer():
            return self._worker_breakpoints.status(breakpoint_id)

    def list_worker_breakpoints(self) -> tuple[WorkerBreakpointStatus, ...]:
        with self._single_writer():
            return self._worker_breakpoints.list_statuses()

    def _require_worker_breakpoint_mutation_boundary_locked(self) -> None:
        self._require_available()
        if self._controller.state not in {
            OperationState.IDLE,
            OperationState.COMPLETED,
            OperationState.FAILED,
            OperationState.CAPTURED,
            OperationState.DEBUG_STOPPED,
        }:
            raise ProtocolError(
                "Worker breakpoint mutation requires a stable runtime boundary; "
                f"current state is {self._controller.state.value}"
            )

    def _apply_worker_breakpoint_plan_locked(
        self,
        plan: WorkerBreakpointPlan,
    ) -> None:
        owner = self._worker_breakpoint_workspace_owner()
        current = owner.confirmed_snapshot
        if current.worker_slots != plan.desired_slots:
            desired = owner.prepare(
                captures=current.captures,
                ordinary_users=current.ordinary_users,
                worker_slots=plan.desired_slots,
                shielded=current.shielded,
            )
            try:
                self._install_worker_breakpoint_workspace(desired)
            except BreakpointWorkspaceOutcomeUnknown as error:
                self._worker_breakpoints.quarantine(plan)
                self._poisoned_error = error
                raise
        self._worker_breakpoints.commit(plan, workspace_confirmed=True)

    def begin_continuation_admission(
        self,
        attempt: ContinuationAttemptSpec,
        locations: tuple[ModuleLocation, ...],
    ) -> _RuntimeContinuationAdmission:
        """Snapshot API metadata around the controller's physical admission."""
        with self._single_writer():
            self._require_available()
            if self._controller.state is not OperationState.CAPTURED:
                raise ProtocolError(
                    "Continuation admission requires a captured runtime"
                )
            points = tuple(locations)
            if any(not isinstance(point, ModuleLocation) for point in points):
                raise ProtocolError("capture point must be a ModuleLocation")
            if any(point.line <= 0 for point in points) or len(set(points)) != len(points):
                raise ProtocolError("continuation capture points are invalid")
            previous_points = self._capture_points
            previous_ticket = self._capture_ticket
            previous_quarantine = self._capture_inspection_quarantined
            begin = getattr(self._controller, "begin_continuation_admission", None)
            if not callable(begin):
                raise ProtocolError(
                    "Runtime controller cannot transact continuation admission"
                )
            try:
                controller_admission = begin(attempt, points)
            except BaseException:
                if self._controller.state is OperationState.RECOVERING:
                    self._capture_ticket = None
                    self._capture_inspection_quarantined = True
                raise
            try:
                self._capture_points = points
                self._capture_ticket = (
                    self._prepare_capture_ticket_locked() if points else None
                )
            except BaseException:
                rollback = getattr(controller_admission, "rollback", None)
                try:
                    if not callable(rollback):
                        raise ProtocolError(
                            "Controller admission cannot restore capture state"
                        )
                    rollback()
                except BaseException:
                    self._capture_ticket = None
                    self._capture_inspection_quarantined = True
                    raise
                self._capture_points = previous_points
                self._capture_ticket = previous_ticket
                self._capture_inspection_quarantined = previous_quarantine
                raise
            return _RuntimeContinuationAdmission(
                self,
                controller_admission,
                capture_points=previous_points,
                capture_ticket=previous_ticket,
                inspection_quarantined=previous_quarantine,
                ticket=self._capture_ticket,
            )

    def prepare_capture_ticket(self) -> CaptureCorrelationTicket:
        """Reserve opaque evidence for the next controller MAIN operation.

        The returned controller number remains internal to the runtime/session
        boundary; agent-facing capture views never serialize it.
        """
        with self._single_writer():
            self._require_available()
            if self._controller.state not in (*self._MAIN_READY_STATES, OperationState.CAPTURED):
                raise ProtocolError("Capture ticket requires a main-ready or captured runtime")
            if not self._capture_points:
                raise ProtocolError("Capture ticket requires armed capture points")
            return self._prepare_capture_ticket_locked()

    def _prepare_capture_ticket_locked(self) -> CaptureCorrelationTicket:
        ticket = CaptureCorrelationTicket(
            ticket_id=f"capture_{uuid4().hex}",
            expected_operation_id=(
                self._controller.operation_id
                if self._controller.state is OperationState.CAPTURED
                else self._controller.operation_id + 1
            ),
            expected_stop_sequence=(
                self._controller.stop_sequence + 1
                if self._controller.state is OperationState.CAPTURED
                else 1
            ),
        )
        self._capture_ticket = ticket
        return ticket

    def _begin_prepared_operation_pin(self) -> OperationGenerationPin | None:
        with self._single_writer():
            self._require_available()
            if self._controller.state not in self._MAIN_READY_STATES:
                raise ProtocolError("MAIN preparation requires a main-ready runtime")
            if self._worker_generation_handle is None:
                return None
            if (
                self._operation_generation_pin is not None
                or self._preparing_generation_pin is not None
            ):
                raise ProtocolError(
                    "Runtime operation already owns a Worker generation pin"
                )
            pin = self._worker_universe.pin_active()
            if pin.handle is not self._worker_generation_handle:
                self._release_generation_pin_locked(pin)
                raise ProtocolError(
                    "Active Worker generation changed while preparing operation"
                )
            self._preparing_generation_pin = pin
            return pin

    def _release_prepared_operation_pin(
        self,
        pin: OperationGenerationPin | None,
    ) -> None:
        if pin is None:
            return
        with self._single_writer():
            self._release_generation_pin_locked(pin)

    def prepare_main_for_capture(
        self,
        source: str,
        *,
        source_unit: SourceUnitRef | None = None,
    ) -> object:
        pin = self._begin_prepared_operation_pin()
        try:
            result = self._prepare_main_for_capture_with_generation_pin(
                source,
                source_unit=source_unit,
                operation_pin=pin,
            )
        except BaseException:
            self._release_prepared_operation_pin(pin)
            raise
        finally:
            with self._single_writer():
                if self._preparing_generation_pin is pin:
                    self._preparing_generation_pin = None
        if not isinstance(result, _PreparedMainExecution):
            self._release_prepared_operation_pin(pin)
        return result

    def _prepare_main_for_capture_with_generation_pin(
        self,
        source: str,
        *,
        source_unit: SourceUnitRef | None = None,
        operation_pin: OperationGenerationPin | None,
    ) -> object:
        """Prepare exact Worker and statement branches without target mutation."""
        with self._single_writer():
            self._require_available()
            if self._controller.state not in self._MAIN_READY_STATES:
                raise ProtocolError("MAIN preparation requires a main-ready runtime")
            if not isinstance(source, str) or not source.strip():
                raise ProtocolError("MAIN source must be non-empty")
            lowerer = getattr(self._controller, "lowerer", None)
            if not isinstance(lowerer, SemanticNotebookLowerer):
                raise ProtocolError("MAIN preparation requires the active lowerer")

            from onec_runtime.bsl.notebook_cells import split_notebook_cell

            unit = self._notebook_source_unit(source, source_unit)
            visible_source = mapped_visible_source(source, unit)
            visible_source_context = VisibleSourceContext({unit: source})
            try:
                cell = split_notebook_cell(
                    lowerer.parser_target,
                    source,
                    source_unit=unit,
                )
            except (BslLexError, BslParseError) as error:
                return self._source_failure_reply(
                    error,
                    visible_source,
                    stage=DiagnosticStage.PARSING,
                    visible_source_context=visible_source_context,
                )
            mapped_visible = cell.visible.source_map.map_offset(0)
            if mapped_visible.unit != unit:
                raise ProtocolError("Notebook cell visible identity changed")
            source_maps = OperationSourceMapBundle(unit, cell.worker, cell.statements)
            fence = self._main_controller_fence()
            context_before = lowerer.persistent_names
            namespace_before = self._namespace_names
            catalog_identity = lowerer.worker_export_identity
            active_worker = self._worker_generation_handle

            method_set_candidate, worker_artifact, candidate_catalog = (
                self._prepare_notebook_methods(cell)
            )
            if method_set_candidate is not None:
                visible_source_context = method_set_candidate.visible_source_context
            prepared_worker_catalog = (
                None if candidate_catalog is None else lowerer.prepare_worker_exports(
                    self._complete_notebook_catalog(candidate_catalog)
                )
            )

            preview_worker = None
            if worker_artifact is not None:
                preview_worker = self._worker_universe.preview_handle(
                    self._notebook_publication_artifacts(worker_artifact),
                    export_catalog=self._notebook_effective_catalog(
                        self._complete_notebook_catalog(candidate_catalog)
                    ),
                )
            lowering: SemanticLoweringResult | None = None
            key_factory = getattr(self._controller, "message_collector_key", None)
            message_collector_key = (
                key_factory(LoweringMode.MAIN)
                if callable(key_factory)
                else "__onec_cell_messages"
            )
            try:
                if source_maps.statement_execution is not None:
                    try:
                        lowering_catalog = (
                            self._complete_notebook_catalog(candidate_catalog)
                            if candidate_catalog is not None
                            else (
                                None
                                if operation_pin is None
                                else self._operation_pin_catalog(operation_pin)
                            )
                        )
                        lowering = lowerer.lower_mapped(
                            source_maps.statement_execution,
                            mode=LoweringMode.MAIN,
                            message_collector_key=message_collector_key,
                            worker_exports=lowering_catalog,
                        )
                        lowering = self._with_worker_generation_prelude(
                            lowering,
                            preview_worker if preview_worker is not None else (
                                None if operation_pin is None else operation_pin.handle
                            ),
                            mode=LoweringMode.MAIN,
                        )
                    except (BslLexError, BslParseError) as error:
                        return self._source_failure_reply(
                            error,
                            source_maps.statement_execution,
                            stage=DiagnosticStage.PARSING,
                            visible_source_context=visible_source_context,
                        )
                    except SemanticLoweringError as error:
                        return self._source_failure_reply(
                            error,
                            source_maps.statement_execution,
                            stage=DiagnosticStage.LOWERING,
                            visible_source_context=visible_source_context,
                        )
            finally:
                self._restore_namespace_context(lowerer, context_before)

            if (
                self._main_controller_fence() != fence
                or lowerer.worker_export_identity != catalog_identity
                or self._worker_generation_handle != active_worker
                or self._namespace_names != namespace_before
                or lowerer.persistent_names != context_before
            ):
                raise ProtocolError("Runtime changed during MAIN preparation")
            statement_identity = (
                None
                if source_maps.statement_execution is None
                else self._mapped_source_identity(source_maps.statement_execution)
            )
            worker_identity = (
                None
                if worker_artifact is None
                else self._worker_artifact_identity(worker_artifact)
            )
            lowering_identity = (
                None
                if lowering is None
                else self._mapped_source_identity(lowering.mapped_source)
            )
            execution_provenance = self._build_execution_provenance(
                visible_source_sha256=unit.source_sha256,
                mode="main",
                visible_source=visible_source,
                statement_source=source_maps.statement_execution,
                lowering=lowering,
                worker_artifact=worker_artifact,
            )
            prepared = _PreparedMainExecution(
                self._prepared_main_owner,
                _PreparedMainPayload(
                    token=f"main_prepared_{uuid4().hex}",
                    source=source,
                    source_sha256=sha256(source.encode("utf-8")).hexdigest(),
                    source_unit=unit,
                    statement_source=source_maps.statement_execution,
                    statement_identity=statement_identity,
                    worker_artifact=worker_artifact,
                    worker_identity=worker_identity,
                    candidate_catalog=candidate_catalog,
                    prepared_worker_catalog=prepared_worker_catalog,
                    lowering=lowering,
                    lowering_identity=lowering_identity,
                    lowerer=lowerer,
                    catalog_identity=catalog_identity,
                    active_worker=active_worker,
                    controller_fence=fence,
                    context_before=context_before,
                    namespace_before=namespace_before,
                    message_collector_key=message_collector_key,
                    visible_source_context=visible_source_context,
                    execution_provenance=execution_provenance,
                    operation_pin=operation_pin,
                    preview_worker=preview_worker,
                    method_set_before=self._notebook_method_set,
                    method_set_candidate=method_set_candidate,
                ),
            )
            self._prepared_source_units[prepared] = unit
            return prepared

    def prepared_main_execution_provenance(
        self,
        prepared: object,
    ) -> OperationExecutionProvenance:
        """Return only hash-only metadata from an owned sealed preparation."""
        with self._single_writer():
            self._require_available()
            if not isinstance(prepared, _PreparedMainExecution):
                raise ProtocolError("Runtime API requires a prepared main")
            payload = prepared.contents(self._prepared_main_owner)
            self._require_current_main_preparation(payload)
            return payload.execution_provenance

    def prepared_main_worker_generation(
        self,
        prepared: object,
    ) -> WorkerGenerationHandle | None:
        """Return only the immutable generation handle sealed in preparation."""
        with self._single_writer():
            self._require_available()
            if not isinstance(prepared, _PreparedMainExecution):
                raise ProtocolError("Runtime API requires a prepared main")
            payload = prepared.contents(self._prepared_main_owner)
            if payload.token in self._consumed_main_preparations:
                raise ProtocolError("Prepared main was already consumed")
            pin = payload.operation_pin
            return None if pin is None else pin.handle

    def activated_main_worker_generation(
        self,
        prepared: object,
    ) -> WorkerGenerationHandle | None:
        """Return only the generation handle carried across MAIN resealing."""
        with self._single_writer():
            self._require_available()
            if not isinstance(prepared, _ActivatedPreparedMainExecution):
                raise ProtocolError("Runtime API requires an activated prepared main")
            activated = prepared.contents(self._prepared_main_owner)
            if activated.token in self._consumed_main_preparations:
                raise ProtocolError("Activated prepared main was already consumed")
            pin = activated.operation_pin
            return None if pin is None else pin.handle

    def activate_prepared_main_for_capture(self, prepared: object) -> object:
        """Activate an already-built Worker and reseal the prepared user MAIN."""
        with self._single_writer():
            self._require_available()
            if not isinstance(prepared, _PreparedMainExecution):
                raise ProtocolError("Runtime API requires a prepared main")
            payload = prepared.contents(self._prepared_main_owner)
            if payload.token in self._consumed_main_preparations:
                raise ProtocolError("Prepared main was already consumed")
            self._consumed_main_preparations.add(payload.token)
            self._prepared_source_units.pop(prepared, None)
            try:
                self._require_current_main_preparation(payload)
            except BaseException:
                self._release_failed_activation_pin(payload.operation_pin)
                raise
            key_factory = getattr(self._controller, "message_collector_key", None)
            current_key = (
                key_factory(LoweringMode.MAIN)
                if callable(key_factory)
                else "__onec_cell_messages"
            )
            if current_key != payload.message_collector_key:
                self._release_failed_activation_pin(payload.operation_pin)
                raise ProtocolError("Prepared main message fence is stale")

            operation_pin = payload.operation_pin
            lowering = payload.lowering
            if payload.worker_artifact is not None:
                try:
                    assert payload.candidate_catalog is not None
                    assert payload.prepared_worker_catalog is not None
                    def require_preview(handle: WorkerGenerationHandle) -> None:
                        preview = payload.preview_worker
                        if preview is None or (
                            handle.runtime_generation,
                            handle.context_generation,
                            handle.generation,
                            handle.manifest_sha256,
                        ) != (
                            preview.runtime_generation,
                            preview.context_generation,
                            preview.generation,
                            preview.manifest_sha256,
                        ):
                            raise ProtocolError("Prepared main Worker preview is stale")

                    self._publish_notebook_worker_artifact_locked(
                        payload.worker_artifact,
                        validated_catalog=payload.candidate_catalog,
                        prepared_catalog=payload.prepared_worker_catalog,
                        method_set_candidate=payload.method_set_candidate,
                        on_prepared_generation=require_preview,
                    )
                except BaseException:
                    self._restore_namespace_context(
                        payload.lowerer, payload.context_before
                    )
                    self._release_failed_activation_pin(payload.operation_pin)
                    raise
                if operation_pin is not None:
                    self._release_generation_pin_locked(operation_pin)
                    operation_pin = None
                try:
                    operation_pin = self._worker_universe.pin_active()
                    if operation_pin.handle is not self._worker_generation_handle:
                        self._release_generation_pin_locked(operation_pin)
                        operation_pin = None
                        raise ProtocolError(
                            "Activated Worker generation changed while pinning MAIN"
                        )
                except BaseException:
                    self._release_failed_activation_pin(operation_pin)
                    raise

            try:
                controller_fence = self._main_controller_fence()
            except BaseException:
                self._release_failed_activation_pin(operation_pin)
                raise
            catalog_identity = payload.lowerer.worker_export_identity
            active_worker = self._worker_generation_handle
            worker_exports = self._worker_exports
            if (
                getattr(self._controller, "lowerer", None) is not payload.lowerer
                or payload.lowerer.persistent_names != payload.context_before
                or self._namespace_names != payload.namespace_before
                or self._worker_export_identity(worker_exports) != catalog_identity
                or (
                    payload.worker_artifact is None
                    and (
                        catalog_identity != payload.catalog_identity
                        or active_worker != payload.active_worker
                    )
                )
                or (
                    payload.worker_artifact is not None
                    and (
                        active_worker is None
                        or self._complete_notebook_catalog(
                            payload.candidate_catalog
                        )
                        != worker_exports
                        or self._worker_export_identity(
                            self._complete_notebook_catalog(
                                payload.candidate_catalog
                            )
                        )
                        != catalog_identity
                    )
                )
            ):
                self._release_failed_activation_pin(operation_pin)
                raise ProtocolError("Runtime changed during prepared MAIN activation")
            prepared = _ActivatedPreparedMainExecution(
                self._prepared_main_owner,
                _ActivatedMainPayload(
                    token=f"main_activated_{uuid4().hex}",
                    prepared=payload,
                    controller_fence=controller_fence,
                    catalog_identity=catalog_identity,
                    active_worker=active_worker,
                    worker_exports=worker_exports,
                    context_before=payload.context_before,
                    namespace_before=payload.namespace_before,
                    operation_pin=operation_pin,
                    lowering=lowering,
                    lowering_identity=(
                        None
                        if lowering is None
                        else self._mapped_source_identity(lowering.mapped_source)
                    ),
                ),
            )
            self._prepared_source_units[prepared] = payload.source_unit
            return prepared

    def _release_failed_activation_pin(
        self,
        pin: OperationGenerationPin | None,
    ) -> None:
        if pin is None or self._poisoned_error is not None:
            return
        self._release_generation_pin_locked(pin)

    def _attempt_prepared_main_for_capture(
        self, prepared: object
    ) -> _PreparedMainExecutionAttempt:
        """Capture exact dispatch evidence without exposing execution internals."""
        user_main_dispatched = False

        def mark_user_main_dispatched() -> None:
            nonlocal user_main_dispatched
            user_main_dispatched = True

        try:
            reply = self.execute_prepared_main_for_capture(
                prepared,
                _dispatch_evidence=mark_user_main_dispatched,
            )
        except BaseException as error:
            return _PreparedMainExecutionAttempt(
                error=error,
                user_main_dispatched=user_main_dispatched,
            )
        return _PreparedMainExecutionAttempt(
            reply=reply,
            user_main_dispatched=user_main_dispatched,
        )

    def execute_prepared_main_for_capture(
        self,
        prepared: object,
        *,
        _dispatch_evidence: Callable[[], None] | None = None,
    ) -> RuntimeReply:
        """Consume one post-activation MAIN without parsing, building, or lowering."""
        with self._single_writer():
            self._require_available()
            if not isinstance(prepared, _ActivatedPreparedMainExecution):
                raise ProtocolError("Runtime API requires an activated prepared main")
            activated = prepared.contents(self._prepared_main_owner)
            if activated.token in self._consumed_main_preparations:
                raise ProtocolError("Activated prepared main was already consumed")
            self._consumed_main_preparations.add(activated.token)
            self._prepared_source_units.pop(prepared, None)
            payload = activated.prepared
            if (
                self._controller.state not in self._MAIN_READY_STATES
                or self._main_controller_fence() != activated.controller_fence
                or getattr(self._controller, "lowerer", None) is not payload.lowerer
                or payload.lowerer.worker_export_identity
                != activated.catalog_identity
                or self._worker_export_identity(self._worker_exports)
                != activated.catalog_identity
                or self._worker_exports != activated.worker_exports
                or self._worker_generation_handle != activated.active_worker
                or (
                    activated.operation_pin is not None
                    and activated.operation_pin.handle
                    is not self._worker_generation_handle
                )
                or payload.lowerer.persistent_names != activated.context_before
                or self._namespace_names != activated.namespace_before
                or sha256(payload.source.encode("utf-8")).hexdigest()
                != payload.source_sha256
                or payload.source_unit.source_sha256 != payload.source_sha256
                or (
                    payload.statement_source is not None
                    and self._mapped_source_identity(payload.statement_source)
                    != payload.statement_identity
                )
                or (
                    payload.worker_artifact is not None
                    and self._worker_artifact_identity(payload.worker_artifact)
                    != payload.worker_identity
                )
                or (
                    activated.lowering is not None
                    and self._mapped_source_identity(
                        activated.lowering.mapped_source
                    )
                    != activated.lowering_identity
                )
            ):
                if activated.operation_pin is not None:
                    self._release_generation_pin_locked(activated.operation_pin)
                raise ProtocolError("Activated prepared main is stale")

            if payload.worker_artifact is not None and payload.statement_source is None:
                assert self._worker_generation_handle is not None
                if activated.operation_pin is not None:
                    self._release_generation_pin_locked(activated.operation_pin)
                return RuntimeReply(
                    RuntimeReplyKind.WORKER_LOADED,
                    self._controller.operation_id,
                    self._controller.state,
                    result=self._worker_generation_handle,
                )

            if activated.operation_pin is not None:
                if self._operation_generation_pin is not None:
                    self._release_generation_pin_locked(activated.operation_pin)
                    raise ProtocolError(
                        "Runtime operation already owns a Worker generation pin"
                    )
                self._operation_generation_pin = activated.operation_pin

            lowering = activated.lowering
            user_main_dispatched = False
            namespace_switched = False

            def mark_user_main_dispatched() -> None:
                nonlocal user_main_dispatched
                user_main_dispatched = True
                if _dispatch_evidence is not None:
                    _dispatch_evidence()

            try:
                self._restore_namespace_context(
                    payload.lowerer,
                    activated.context_before
                    if lowering is None
                    else lowering.context_names,
                )
                namespace_switched = True
                execute_mapped = getattr(
                    self._controller, "execute_mapped_main", None
                )
                self._require_operation_pin_dispatch_fence_locked(
                    activated.operation_pin,
                    mode=LoweringMode.MAIN,
                )
                if callable(execute_mapped):
                    mapped_execution = (
                        payload.statement_source
                        if lowering is None
                        else lowering.mapped_source
                    )
                    reply = self._reply(
                        execute_mapped(
                            payload.source,
                            mapped_execution,
                            visible_source_context=payload.visible_source_context,
                            messages_intercepted=(
                                0 if lowering is None else lowering.messages_intercepted
                            ),
                            message_collector_key=payload.message_collector_key,
                            worker_messages=self._notebook_worker_messages_enabled(),
                            worker_globals=self._notebook_worker_globals(),
                            capture_points=self._capture_points,
                            user_breakpoints=self._user_breakpoints,
                            on_transport_dispatch=mark_user_main_dispatched,
                        )
                    )
                else:
                    raise ProtocolError(
                        "Runtime controller requires mapped MAIN execution"
                    )
            except BaseException:
                try:
                    if namespace_switched:
                        self._restore_namespace_context(
                            payload.lowerer, activated.context_before
                        )
                finally:
                    self._finalize_active_operation_pin_locked(
                        reply=None,
                        outcome_unknown=user_main_dispatched,
                    )
                raise
            try:
                return self._finalize_namespace_reply(
                    reply,
                    lowering=lowering,
                    context_before=activated.context_before,
                    lowerer=payload.lowerer,
                )
            finally:
                self._finalize_active_operation_pin_locked(reply=reply)

    def discard_prepared_main_for_capture(self, prepared: object) -> None:
        """Consume one unused MAIN capability and release its operation pin."""
        with self._single_writer():
            if isinstance(prepared, _PreparedMainExecution):
                payload = prepared.contents(self._prepared_main_owner)
                token = payload.token
            elif isinstance(prepared, _ActivatedPreparedMainExecution):
                activated = prepared.contents(self._prepared_main_owner)
                payload = activated.prepared
                token = activated.token
            else:
                raise ProtocolError("Runtime API requires a prepared main")
            if token in self._consumed_main_preparations:
                raise ProtocolError("Prepared main was already consumed")
            self._consumed_main_preparations.add(token)
            self._prepared_source_units.pop(prepared, None)
            pin = (
                activated.operation_pin
                if isinstance(prepared, _ActivatedPreparedMainExecution)
                else payload.operation_pin
            )
            if pin is not None:
                self._release_generation_pin_locked(pin)

    def _require_current_main_preparation(self, payload: _PreparedMainPayload) -> None:
        worker_parts_valid = (
            payload.worker_artifact is None
            and payload.worker_identity is None
            and payload.candidate_catalog is None
            and payload.prepared_worker_catalog is None
        ) or (
            payload.worker_artifact is not None
            and payload.worker_identity is not None
            and payload.candidate_catalog is not None
            and payload.prepared_worker_catalog
            == self._complete_notebook_catalog(payload.candidate_catalog)
        )
        if (
            not worker_parts_valid
            or self._notebook_method_set is not payload.method_set_before
            or (
                payload.operation_pin is not None
                and payload.operation_pin.handle is not self._worker_generation_handle
            )
            or self._controller.state not in self._MAIN_READY_STATES
            or self._main_controller_fence() != payload.controller_fence
            or getattr(self._controller, "lowerer", None) is not payload.lowerer
            or payload.lowerer.worker_export_identity != payload.catalog_identity
            or self._worker_export_identity(self._worker_exports)
            != payload.catalog_identity
            or self._worker_generation_handle != payload.active_worker
            or payload.lowerer.persistent_names != payload.context_before
            or self._namespace_names != payload.namespace_before
            or sha256(payload.source.encode("utf-8")).hexdigest()
            != payload.source_sha256
            or payload.source_unit.source_sha256 != payload.source_sha256
            or (
                payload.statement_source is not None
                and self._mapped_source_identity(payload.statement_source)
                != payload.statement_identity
            )
            or (
                payload.worker_artifact is not None
                and self._worker_artifact_identity(payload.worker_artifact)
                != payload.worker_identity
            )
            or (
                payload.lowering is not None
                and self._mapped_source_identity(payload.lowering.mapped_source)
                != payload.lowering_identity
            )
        ):
            raise ProtocolError("Prepared main is stale")

    def _main_controller_fence(self) -> tuple[int, int, OperationState]:
        generation = self._controller.runtime_generation
        operation_id = self._controller.operation_id
        state = self._controller.state
        if (
            type(generation) is not int
            or generation <= 0
            or type(operation_id) is not int
            or operation_id < 0
            or state not in self._MAIN_READY_STATES
        ):
            raise ProtocolError("MAIN controller identity is incomplete")
        return generation, operation_id, state

    @staticmethod
    def _mapped_source_identity(source: MappedSource) -> tuple[str, str]:
        return source.artifact.source_sha256, source.source_map_sha256

    @staticmethod
    def _build_execution_provenance(
        *,
        visible_source_sha256: str,
        mode: str,
        visible_source: MappedSource,
        statement_source: MappedSource | None,
        lowering: SemanticLoweringResult | None,
        worker_artifact: WorkerArtifact | None,
    ) -> OperationExecutionProvenance:
        """Select exact prepared artifacts without returning their source text."""
        executed = (
            lowering.mapped_source
            if lowering is not None
            else statement_source
            if statement_source is not None
            else None
        )
        worker = None if worker_artifact is None else worker_artifact.source_provenance
        if executed is not None:
            executed_sha256 = executed.artifact.source_sha256
            source_map_sha256 = executed.source_map_sha256
        elif worker_artifact is not None:
            if worker is None:
                raise ProtocolError("Worker execution provenance is incomplete")
            executed_sha256 = worker.source_sha256
            source_map_sha256 = worker.source_map_sha256
        else:
            executed_sha256 = visible_source.artifact.source_sha256
            source_map_sha256 = visible_source.source_map_sha256
        return OperationExecutionProvenance(
            visible_source_sha256=visible_source_sha256,
            executed_source_sha256=executed_sha256,
            source_map_sha256=source_map_sha256,
            mode=mode,
            worker_generation=(
                None if worker is None else worker.worker_generation
            ),
            worker_manifest_sha256=(
                None if worker is None else worker.worker_manifest_sha256
            ),
        )

    @staticmethod
    def _worker_artifact_identity(
        artifact: WorkerArtifact,
    ) -> tuple[str, str, str | None]:
        return (
            artifact.source_sha256,
            artifact.artifact_sha256,
            artifact.source_map_sha256,
        )

    @staticmethod
    def _worker_export_identity(
        catalog: tuple[WorkerExport, ...],
    ) -> tuple[tuple[str, str, str | None], ...]:
        return tuple(
            sorted(
                (
                    item.public_path.casefold(),
                    item.method.casefold(),
                    (
                        None
                        if item.receiver_module is None
                        else item.receiver_module.casefold()
                    ),
                )
                for item in catalog
            )
        )

    @staticmethod
    def _notebook_effective_catalog(
        catalog: tuple[WorkerExport, ...],
    ) -> tuple[WorkerExport, ...]:
        return tuple(
            export
            if export.receiver_module is not None
            else WorkerExport(
                export.public_path,
                export.method,
                receiver_module="Worker",
            )
            for export in catalog
        )

    def _notebook_source_unit(
        self, source: str, explicit: SourceUnitRef | None = None
    ) -> SourceUnitRef:
        if explicit is not None:
            retained = list(self._prepared_source_units.values())
            for view in self._worker_universe._retained_debug_views():
                for module in view.modules:
                    retained.extend(module.source_units)
            identity = explicit.kind, explicit.unit_id, explicit.revision
            if any(
                (unit.kind, unit.unit_id, unit.revision) == identity
                and unit.source_sha256 != explicit.source_sha256
                for unit in retained
            ):
                raise ProtocolError("Notebook source identity conflicts with a retained source")
            return explicit
        self._anonymous_notebook_revision += 1
        return SourceUnitRef(
            SourceUnitKind.NOTEBOOK_CELL, self._anonymous_notebook_id,
            self._anonymous_notebook_revision, source_sha256(source),
        )

    def _prepare_notebook_methods(
        self, cell: NotebookCellProjection
    ) -> tuple[
        NotebookMethodSet | None, WorkerArtifact | None,
        tuple[WorkerExport, ...] | None,
    ]:
        """Build one immutable upsert candidate without changing active methods."""
        if not cell.has_methods:
            return None, None, None
        candidate = merge_notebook_methods(self._notebook_method_set, cell)
        builder = self._notebook_worker_builder
        if builder is None:
            raise ProtocolError(
                "Notebook methods require a configured worker artifact builder"
            )
        bound_source, bound_globals = bind_notebook_method_globals(
            candidate.mapped_source,
            context_names=self._namespace_names,
            exports=candidate.exports,
        )
        candidate = replace(candidate, bound_globals=bound_globals)
        artifact = builder(
            instrument_notebook_worker_messages(bound_source),
            candidate.exports,
            visible_source_context=candidate.visible_source_context,
        )
        return candidate, artifact, validate_production_worker_artifact(artifact)

    def _notebook_worker_messages_enabled(self) -> bool:
        return (
            self._notebook_method_set is not None
            and self._notebook_method_set.intercepts_messages
        )

    def _notebook_worker_globals(self) -> tuple[str, ...]:
        return (
            () if self._notebook_method_set is None
            else self._notebook_method_set.bound_globals
        )

    def _complete_notebook_catalog(
        self, notebook: tuple[WorkerExport, ...]
    ) -> tuple[WorkerExport, ...]:
        return self._descriptor_catalog(
            tuple(active.artifact for active in self._worker_active_modules.values()),
            notebook=notebook,
        )

    @classmethod
    def _descriptor_catalog(
        cls, descriptors: tuple[WorkerModuleArtifact, ...],
        *, notebook: tuple[WorkerExport, ...] | None = None,
    ) -> tuple[WorkerExport, ...]:
        result: list[WorkerExport] = []
        for descriptor in sorted(descriptors, key=lambda item: item.logical_name.casefold()):
            for export in descriptor.exports:
                result.append(WorkerExport(
                    export.method if descriptor.logical_name.casefold() == "worker"
                    else f"{descriptor.logical_name}.{export.method}",
                    export.method, receiver_module=descriptor.logical_name,
                ))
        if notebook is not None:
            result.extend(cls._notebook_effective_catalog(notebook))
        return tuple(sorted(result, key=lambda item: item.public_path.casefold()))

    def prepare_capture_hypothesis(self, source: str) -> object:
        """Lower one CAPTURE cell against the exact active runtime catalog.

        Preparation is target-read-only.  The stateful lowerer's persistent
        namespace is restored before the single-writer fence is released; the
        sealed result is committed only by its one execution consumer.
        """
        with self._single_writer():
            self._require_available()
            self._require_capture_inspection_available()
            if self._controller.state is not OperationState.CAPTURED:
                raise ProtocolError("CAPTURE preparation requires a captured runtime")
            if not isinstance(source, str) or not source.strip():
                raise ProtocolError("CAPTURE source must be non-empty")
            lowerer = getattr(self._controller, "lowerer", None)
            if not isinstance(lowerer, SemanticNotebookLowerer):
                raise ProtocolError("CAPTURE preparation requires the active lowerer")

            from onec_runtime.bsl.notebook_cells import split_notebook_cell

            visible_unit = self._notebook_source_unit(source)
            visible_source = mapped_visible_source(source, visible_unit)
            visible_source_context = VisibleSourceContext({visible_unit: source})
            try:
                cell = split_notebook_cell(
                    lowerer.parser_target,
                    source,
                    source_unit=visible_unit,
                )
            except (BslLexError, BslParseError) as error:
                return self._source_failure_reply(
                    error,
                    visible_source,
                    stage=DiagnosticStage.PARSING,
                    visible_source_context=visible_source_context,
                )
            if cell.has_methods:
                error = SemanticLoweringError(
                    "CAPTURE hypotheses cannot define or replace Worker methods"
                )
                return self._source_failure_reply(
                    error,
                    visible_source,
                    stage=DiagnosticStage.LOWERING,
                    visible_source_context=visible_source_context,
                )
            statement_source = cell.statements
            if statement_source is None or not statement_source.text.strip():
                error = SemanticLoweringError(
                    "CAPTURE hypotheses require executable statements"
                )
                return self._source_failure_reply(
                    error,
                    visible_source,
                    stage=DiagnosticStage.LOWERING,
                    visible_source_context=visible_source_context,
                )

            fence = self._capture_controller_fence()
            context_before = lowerer.persistent_names
            operation_pin = self._operation_generation_pin
            pinned_catalog = self._worker_exports or None
            catalog_identity = (
                lowerer.worker_export_identity
                if pinned_catalog is None
                else self._worker_export_identity(pinned_catalog)
            )
            worker_identity = self._worker_generation_handle
            key_factory = getattr(self._controller, "message_collector_key", None)
            if not callable(key_factory):
                raise ProtocolError("CAPTURE controller cannot fence message collection")
            message_collector_key = key_factory(LoweringMode.CAPTURE)
            evaluation_pin = self._pin_capture_evaluation_locked()
            try:
                try:
                    lowering = lowerer.lower_mapped(
                        statement_source,
                        mode=LoweringMode.CAPTURE,
                        message_collector_key=message_collector_key,
                        worker_exports=pinned_catalog,
                    )
                    lowering = self._with_generation_pin_prelude(
                        lowering, evaluation_pin, mode=LoweringMode.CAPTURE
                    )
                except (BslLexError, BslParseError) as error:
                    return self._source_failure_reply(
                        error,
                        statement_source,
                        stage=DiagnosticStage.PARSING,
                        visible_source_context=visible_source_context,
                    )
                except SemanticLoweringError as error:
                    return self._source_failure_reply(
                        error,
                        statement_source,
                        stage=DiagnosticStage.LOWERING,
                        visible_source_context=visible_source_context,
                    )
            finally:
                lowerer.restore_persistent_names(context_before)
                self._finish_capture_evaluation_pin_locked()
            if (
                self._capture_controller_fence() != fence
                or self._operation_generation_pin is not operation_pin
                or self._worker_generation_handle is not worker_identity
                or lowerer.worker_export_identity != catalog_identity
            ):
                raise ProtocolError("Runtime changed during CAPTURE preparation")
            prepared = _PreparedCaptureHypothesis(
                owner=self._prepared_capture_owner,
                token=f"capture_prepared_{uuid4().hex}",
                source=source,
                lowering=lowering,
                lowerer=lowerer,
                catalog_identity=catalog_identity,
                worker_identity=worker_identity,
                operation_pin=operation_pin,
                controller_fence=fence,
                context_before=context_before,
                message_collector_key=message_collector_key,
                execution_provenance=self._build_execution_provenance(
                    visible_source_sha256=visible_unit.source_sha256,
                    mode="capture",
                    visible_source=visible_source,
                    statement_source=statement_source,
                    lowering=lowering,
                    worker_artifact=None,
                ),
            )
            self._prepared_source_units[prepared] = visible_unit
            return prepared

    def prepared_capture_hypothesis_provenance(
        self,
        prepared: object,
    ) -> OperationExecutionProvenance:
        """Return safe metadata without exposing the prepared capability."""
        with self._single_writer():
            self._require_available()
            self._require_capture_inspection_available()
            if not isinstance(prepared, _PreparedCaptureHypothesis):
                raise ProtocolError("Runtime API requires a prepared capture")
            return prepared.execution_provenance(self._prepared_capture_owner)

    def execute_prepared_capture_hypothesis(self, prepared: object) -> RuntimeReply:
        """Consume one exact prepared CAPTURE lowering without lowering again."""
        with self._single_writer():
            self._require_available()
            self._require_capture_inspection_available()
            if not isinstance(prepared, _PreparedCaptureHypothesis):
                raise ProtocolError("Runtime API requires a prepared capture")
            (
                token,
                source,
                source_sha256,
                lowered_sha256,
                lowering,
                lowerer,
                catalog_identity,
                worker_identity,
                operation_pin,
                controller_fence,
                context_before,
                message_collector_key,
            ) = prepared.contents(self._prepared_capture_owner)
            if token in self._consumed_capture_preparations:
                raise ProtocolError("Prepared capture was already consumed")
            if (
                self._controller.state is not OperationState.CAPTURED
                or self._capture_controller_fence() != controller_fence
                or getattr(self._controller, "lowerer", None) is not lowerer
                or self._operation_generation_pin is not operation_pin
                or lowerer.worker_export_identity != catalog_identity
                or self._worker_generation_handle is not worker_identity
                or lowerer.persistent_names != context_before
                or sha256(source.encode("utf-8")).hexdigest() != source_sha256
                or sha256(lowering.source.encode("utf-8")).hexdigest()
                != lowered_sha256
            ):
                raise ProtocolError("Prepared capture is stale")
            key_factory = getattr(self._controller, "message_collector_key", None)
            if (
                not callable(key_factory)
                or key_factory(LoweringMode.CAPTURE) != message_collector_key
            ):
                raise ProtocolError("Prepared capture message fence is stale")

            execute_mapped = getattr(
                self._controller, "execute_mapped_capture", None
            )
            if not callable(execute_mapped):
                raise ProtocolError(
                    "Runtime controller requires mapped CAPTURE execution"
                )
            self._consumed_capture_preparations.add(token)
            self._prepared_source_units.pop(prepared, None)
            visible_source_context = self._visible_context_for_mapped(
                source,
                lowering.mapped_source,
            )
            capture_dispatched = False

            def mark_capture_dispatched() -> None:
                nonlocal capture_dispatched
                capture_dispatched = True

            evaluation_pin = self._pin_capture_evaluation_locked()
            handoff = None
            try:
                lowerer.restore_persistent_names(lowering.context_names)
                self._require_operation_pin_dispatch_fence_locked(
                    evaluation_pin,
                    mode=LoweringMode.CAPTURE,
                    capture_evaluation=True,
                )
                primary_execution, normalize_capture_error = (
                    self._capture_execution_callbacks_locked(
                        lowering.dirty_roots,
                    )
                )

                def completion(result: object, error: BaseException | None) -> object:
                    # Mandatory local completion is owned by the record,
                    # including after its initiating waiter detaches.
                    # A confirmed execution may write a captured root before
                    # BSL fails. This ledger belongs to coordinator completion,
                    # even when no initiating caller remains to see it.
                    if error is None or isinstance(error, BslExecutionError):
                        for root in lowering.dirty_roots:
                            self._pending_dirty_roots.setdefault(root.casefold(), root)
                    if error is not None:
                        lowerer.restore_persistent_names(context_before)
                        return None
                    reply = self._reply(result)
                    return self._finalize_namespace_reply(
                        reply,
                        lowering=lowering,
                        context_before=context_before,
                        lowerer=lowerer,
                    )

                def rejection(error: BaseException) -> None:
                    # Submission did not create an owned record. Its error
                    # type is not proof that captured BSL actually executed.
                    lowerer.restore_persistent_names(context_before)

                try:
                    if callable(execute_mapped):
                        handoff = _PreparedCaptureExecution(
                            execute=lambda: execute_mapped(
                                source, lowering.mapped_source,
                                visible_source_context=visible_source_context,
                                messages_intercepted=lowering.messages_intercepted,
                                message_collector_key=message_collector_key,
                                worker_messages=self._notebook_worker_messages_enabled(),
                                worker_globals=self._notebook_worker_globals(),
                                dirty_roots=lowering.dirty_roots,
                                on_transport_dispatch=mark_capture_dispatched,
                            ),
                            detach_pin=self._detach_capture_evaluation_pin_locked,
                            completion=completion,
                            release_writer=self._capture_owner_handoff,
                            release_waiter=self._capture_session_waiter_handoff,
                            primary_execution=primary_execution,
                            normalize_error=normalize_capture_error,
                            rejection=rejection,
                        )
                        result = self._execute_prepared_capture_handoff(handoff)
                        if handoff.transferred:
                            return result
                        reply = self._reply(result)
                    else:
                        raise ProtocolError(
                            "Runtime controller requires mapped CAPTURE execution"
                        )
                except BslExecutionError as error:
                    diagnostic = (
                        error.diagnostic
                        if handoff is not None and handoff.transferred
                        else self._worker_runtime_diagnostic(
                            str(error),
                            error.diagnostic,
                        )
                    )
                    reply = RuntimeReply(
                        RuntimeReplyKind.CAPTURE_CELL,
                        self._controller.operation_id,
                        self._controller.state,
                        error=_BSL_EXECUTION_FAILURE_SUMMARY,
                        succeeded=False,
                        messages=error.messages,
                        diagnostic=diagnostic,
                    )
                    if handoff is not None and handoff.transferred:
                        # Mandatory completion/rejection already updated
                        # namespace and dirty roots on the owning path.
                        if handoff.submitted:
                            reply = replace(
                                reply, changed_roots=lowering.persistent_write_roots,
                                capture_dirty_roots=lowering.dirty_roots,
                            )
                        return reply
            except BaseException:
                if handoff is not None and handoff.transferred:
                    raise
                try:
                    lowerer.restore_persistent_names(context_before)
                finally:
                    self._finish_capture_evaluation_pin_locked(outcome_unknown=capture_dispatched)
                raise
            try:
                for root in lowering.dirty_roots:
                    self._pending_dirty_roots.setdefault(root.casefold(), root)
                return self._finalize_namespace_reply(
                    reply, lowering=lowering, context_before=context_before, lowerer=lowerer,
                )
            finally:
                self._finish_capture_evaluation_pin_locked(reply=reply)

    def _execute_prepared_capture_handoff(self, handoff: _PreparedCaptureExecution) -> object:
        submit_owned = getattr(self._controller, "submit_capture_execution", None)
        if not callable(submit_owned):
            return handoff.execute_sync()
        return handoff.execute_owned(
            lambda **ownership: submit_owned(
                handoff.execute,
                **ownership,
            )
        )

    def _capture_controller_fence(self) -> tuple[int, int, int, int]:
        values = (
            self._controller.runtime_generation,
            self._controller.operation_id,
            getattr(self._controller, "stop_sequence", None),
            getattr(self._controller, "cell_sequence", None),
        )
        if any(type(value) is not int or value < 0 for value in values):
            raise ProtocolError("CAPTURE controller identity is incomplete")
        return values  # type: ignore[return-value]

    def _capture_operation_fence(self) -> tuple[int, int, int]:
        if self._controller.state is not OperationState.CAPTURED:
            raise ProtocolError("CAPTURE operation fence requires a captured runtime")
        values = (
            self._controller.runtime_generation,
            self._controller.operation_id,
            getattr(self._controller, "stop_sequence", None),
        )
        if any(type(value) is not int or value < 0 for value in values):
            raise ProtocolError("CAPTURE operation identity is incomplete")
        return values  # type: ignore[return-value]

    def _begin_user_operation_pin_locked(
        self,
    ) -> tuple[OperationGenerationPin | None, bool]:
        """Return the exact pin and whether it belongs to a paused CAPTURE."""
        with self._confirmed_single_writer():
            self._require_available()
            if self._controller.state is OperationState.CAPTURED:
                pin = self._pin_capture_evaluation_locked()
                return pin, True
            if self._controller.state not in self._MAIN_READY_STATES:
                return None, False
            if self._worker_generation_handle is None:
                return None, False
            pin = self._worker_universe.pin_active()
            if pin.handle is not self._worker_generation_handle:
                try:
                    self._release_generation_pin_locked(pin)
                finally:
                    raise ProtocolError(
                        "Active Worker generation changed while pinning operation"
                    )
            if self._operation_generation_pin is not None:
                self._release_generation_pin_locked(pin)
                raise ProtocolError(
                    "Runtime operation already owns a Worker generation pin"
                )
            self._operation_generation_pin = pin
            return pin, False

    def _operation_pin_catalog(
        self,
        pin: OperationGenerationPin,
    ) -> tuple[WorkerExport, ...]:
        return pin.export_catalog

    def _pin_capture_evaluation_locked(self) -> OperationGenerationPin | None:
        with self._generation_lock:
            if self._evaluation_generation_pin is not None:
                raise ProtocolError("CAPTURE evaluation already owns a generation pin")
            expected = self._worker_generation_handle
        if expected is None:
            return None
        pin = self._worker_universe.pin_active()
        failure = "Active Worker generation changed while pinning CAPTURE"
        with self._generation_lock:
            if self._evaluation_generation_pin is not None:
                failure = "CAPTURE evaluation already owns a generation pin"
            elif (
                self._worker_generation_handle is expected
                and pin.handle is expected
            ):
                self._evaluation_generation_pin = pin
                return pin
        self._release_generation_pin_locked(pin)
        raise ProtocolError(failure)

    def _detach_capture_evaluation_pin_locked(self) -> Callable[[str], None]:
        with self._generation_lock:
            pin = self._evaluation_generation_pin
            self._evaluation_generation_pin = None
        lease_lock = Lock()
        disposed = False

        def dispose(disposition: str) -> None:
            nonlocal disposed
            if disposition not in {"release", "quarantine"}:
                raise ValueError("invalid CAPTURE pin disposition")
            with lease_lock:
                if disposed:
                    return
                disposed = True
            # Neither the pin slot nor the one-shot lease lock is held while
            # disposing Worker ownership or updating breakpoint workspaces.
            if pin is None:
                return
            if disposition == "quarantine":
                try:
                    self._worker_universe.retain_outcome_unknown(pin)
                finally:
                    self._poisoned_error = WorkerPromotionOutcomeUnknown(
                        pin.handle.generation, pin.handle.manifest_sha256,
                    )
            else:
                self._release_generation_pin_locked(pin)

        return dispose

    def _capture_execution_callbacks_locked(
        self,
        dirty_roots: tuple[str, ...],
    ) -> tuple[
        Callable[[], None],
        Callable[[BslExecutionError], BslExecutionError],
    ]:
        """Bind post-dispatch evidence to this exact CAPTURE generation.

        The callbacks outlive the caller-side writer handoff.  Capture the
        immutable diagnostic artifacts now, while the evaluation pin still
        identifies the generation that will execute the request.
        """
        with self._confirmed_single_writer():
            pin = self._evaluation_generation_pin
            manifest_sha256 = (
                None if pin is None else pin.handle.manifest_sha256
            )
            artifacts = (
                ()
                if manifest_sha256 is None
                else self._worker_generation_diagnostics.get(
                    manifest_sha256,
                    (),
                )
            )

        def primary_execution() -> None:
            for root in dirty_roots:
                self._pending_dirty_roots.setdefault(root.casefold(), root)

        def normalize_error(error: BslExecutionError) -> BslExecutionError:
            if manifest_sha256 is None or not artifacts:
                return error
            diagnostic = self._worker_runtime_diagnostic_from_artifacts(
                str(error),
                error.diagnostic,
                manifest_sha256=manifest_sha256,
                artifacts=artifacts,
            )
            if diagnostic is error.diagnostic:
                return error
            return BslExecutionError(
                str(error),
                messages=error.messages,
                diagnostic=diagnostic,
            )

        return primary_execution, normalize_error

    def _finish_capture_evaluation_pin_locked(
        self, *, reply: RuntimeReply | None = None, outcome_unknown: bool = False
    ) -> None:
        if self._poisoned_error is not None:
            # Publication may already have quarantined the universe. Its
            # leases remain owned until teardown; release cannot be trusted.
            return
        if (
            not outcome_unknown
            and reply is not None
            and reply.kind is RuntimeReplyKind.DEBUG_STOPPED
            and self._controller.state is OperationState.CAPTURE_DEBUG_STOPPED
        ):
            return
        dispose = self._detach_capture_evaluation_pin_locked()
        dispose("quarantine" if outcome_unknown else "release")

    def _require_operation_pin_dispatch_fence_locked(
        self,
        pin: OperationGenerationPin | None,
        *,
        mode: LoweringMode,
        capture_evaluation: bool = False,
        require_active: bool = True,
    ) -> None:
        with self._confirmed_single_writer():
            expected_pin = (self._evaluation_generation_pin
                            if capture_evaluation else self._operation_generation_pin)
            if expected_pin is not pin:
                raise ProtocolError(
                    "Runtime Worker generation pin changed before dispatch"
                )
            if require_active and (mode is LoweringMode.MAIN or capture_evaluation):
                active = self._worker_generation_handle
                if (active is None) != (pin is None) or (
                    active is not None
                    and pin is not None
                    and pin.handle is not active
                ):
                    raise ProtocolError(
                        "Active Worker generation changed before MAIN dispatch"
                    )

    def _finish_user_operation_pin_locked(
        self,
        pin: OperationGenerationPin | None,
        *,
        reply: RuntimeReply | None,
        outcome_unknown: bool = False,
    ) -> None:
        if pin is None:
            return
        if self._poisoned_error is not None:
            return
        with self._confirmed_single_writer():
            keep = self._reply_keeps_operation_pin(reply)
            if keep:
                if self._operation_generation_pin is not pin:
                    raise ProtocolError(
                        "Runtime Worker generation pin changed during operation"
                    )
                if reply.kind is RuntimeReplyKind.CAPTURED:
                    self._install_capture_worker_generation_pin_locked(pin)
                return
            if self._operation_generation_pin is not pin:
                raise ProtocolError(
                    "Runtime Worker generation pin changed during operation"
                )
            if reply is None and outcome_unknown:
                self._operation_generation_pin = None
                try:
                    self._worker_universe.retain_outcome_unknown(pin)
                finally:
                    self._poisoned_error = WorkerPromotionOutcomeUnknown(
                        pin.handle.generation,
                        pin.handle.manifest_sha256,
                    )
                return
            self._operation_generation_pin = None
            self._release_generation_pin_locked(pin)

    def _finalize_active_operation_pin_locked(
        self,
        *,
        reply: RuntimeReply | None,
        outcome_unknown: bool = False,
    ) -> None:
        pin = self._operation_generation_pin
        if pin is None:
            return
        if self._reply_keeps_operation_pin(reply):
            assert reply is not None
            if reply.kind is RuntimeReplyKind.CAPTURED:
                self._install_capture_worker_generation_pin_locked(pin)
            return
        if reply is None and outcome_unknown:
            self._operation_generation_pin = None
            try:
                self._worker_universe.retain_outcome_unknown(pin)
            finally:
                self._poisoned_error = WorkerPromotionOutcomeUnknown(
                    pin.handle.generation,
                    pin.handle.manifest_sha256,
                )
            return
        self._operation_generation_pin = None
        self._release_generation_pin_locked(pin)

    def _reply_keeps_operation_pin(
        self,
        reply: RuntimeReply | None,
    ) -> bool:
        if reply is None:
            return False
        if reply.kind in {
            RuntimeReplyKind.CAPTURED,
            RuntimeReplyKind.DEBUG_STOPPED,
        }:
            return True
        return (
            reply.kind is RuntimeReplyKind.CAPTURE_CELL
            and self._controller.state is OperationState.CAPTURED
        )

    def _release_generation_pin_locked(
        self,
        pin: OperationGenerationPin,
    ) -> None:
        try:
            self._release_worker_lifecycle_locked(pin)
            self._prune_worker_caches_locked()
        except WorkerPromotionOutcomeUnknown as error:
            self._poisoned_error = error
            raise

    def _install_capture_worker_generation_pin_locked(
        self,
        pin: OperationGenerationPin,
    ) -> None:
        install = getattr(
            self._controller,
            "install_capture_worker_generation_pin",
            None,
        )
        if not callable(install):
            raise ProtocolError(
                "Runtime controller cannot install the CAPTURE Worker pin"
            )
        # The kernel slot is local input to a future evaluation, not the
        # generation local retained by the suspended MAIN stack frame.
        active = self._worker_generation_handle
        if active is None:
            raise ProtocolError("CAPTURE evaluation generation is unavailable")
        with self._capture_helper_writer_handoff():
            install(active.manifest_sha256)

    def _clear_capture_worker_generation_pin_locked(self) -> None:
        clear = getattr(
            self._controller,
            "clear_capture_worker_generation_pin",
            None,
        )
        if not callable(clear):
            raise ProtocolError(
                "Runtime controller cannot clear the CAPTURE Worker pin"
            )
        with self._capture_helper_writer_handoff():
            clear()

    @staticmethod
    def _with_generation_pin_prelude(
        lowering: SemanticLoweringResult,
        pin: OperationGenerationPin | None,
        *,
        mode: LoweringMode,
    ) -> SemanticLoweringResult:
        return PrototypeRuntimeApi._with_worker_generation_prelude(
            lowering, None if pin is None else pin.handle, mode=mode
        )

    @staticmethod
    def _with_worker_generation_prelude(
        lowering: SemanticLoweringResult,
        handle: WorkerGenerationHandle | None,
        *,
        mode: LoweringMode,
    ) -> SemanticLoweringResult:
        if handle is None:
            return lowering
        manifest = bsl_string_literal(handle.manifest_sha256)
        local_assignment = (
            "__OnecPinnedWorkerGeneration = "
            "Контекст.RuntimeWorkerActiveGeneration;\n"
        )
        prelude = local_assignment + (
            "Если __OnecPinnedWorkerGeneration.ManifestSha256 <> "
            f"{manifest} Тогда\n"
            '    ВызватьИсключение "Worker generation pin mismatch";\n'
            "КонецЕсли;\n"
        )
        builder = SourceTransformBuilder(lowering.mapped_source)
        builder.synthetic(
            prelude,
            SourceSpan(0, 0),
            "worker_generation_pin_prelude",
        )
        builder.copy(SourceSpan(0, len(lowering.source)))
        mapped = builder.build(
            SourceArtifactKind.EXECUTED_BSL,
            mode=mode.value,
            worker_generation=handle.generation,
            worker_manifest_sha256=handle.manifest_sha256,
        )
        return replace(lowering, mapped_source=mapped)

    def execute_bsl(
        self,
        source: str,
        *,
        source_unit: SourceUnitRef | None = None,
        on_execution_provenance: (
            Callable[[OperationExecutionProvenance], None] | None
        ) = None,
    ) -> RuntimeReply:
        with self._single_writer():
            self._require_capture_data_plane_admission()
            self._require_available()
            pin, shared_capture_pin = self._begin_user_operation_pin_locked()
            user_bsl_dispatched = False
            known_reply: RuntimeReply | None = None

            def mark_user_bsl_dispatched() -> None:
                nonlocal user_bsl_dispatched
                user_bsl_dispatched = True

            def mark_known_reply(reply: RuntimeReply) -> None:
                nonlocal known_reply
                known_reply = reply

            try:
                reply = self._execute_bsl_with_generation_pin_locked(
                    source,
                    source_unit=source_unit,
                    on_execution_provenance=on_execution_provenance,
                    operation_pin=pin,
                    _dispatch_evidence=mark_user_bsl_dispatched,
                    _reply_evidence=mark_known_reply,
                )
            except BaseException:
                current_pin = self._operation_generation_pin
                if shared_capture_pin:
                    self._finish_capture_evaluation_pin_locked(
                        reply=known_reply,
                        outcome_unknown=user_bsl_dispatched and known_reply is None
                    )
                elif current_pin is not None:
                    self._finish_user_operation_pin_locked(
                        current_pin, reply=known_reply,
                        outcome_unknown=user_bsl_dispatched and known_reply is None,
                    )
                raise
            current_pin = self._operation_generation_pin
            if shared_capture_pin:
                self._finish_capture_evaluation_pin_locked(reply=reply)
            elif current_pin is not None:
                self._finish_user_operation_pin_locked(current_pin, reply=reply)
            return reply

    def _execute_bsl_with_generation_pin_locked(
        self,
        source: str,
        *,
        source_unit: SourceUnitRef | None = None,
        on_execution_provenance: (
            Callable[[OperationExecutionProvenance], None] | None
        ) = None,
        operation_pin: OperationGenerationPin | None,
        _dispatch_evidence: Callable[[], None] | None = None,
        _reply_evidence: Callable[[RuntimeReply], None] | None = None,
    ) -> RuntimeReply:
        with self._confirmed_single_writer():
            self._require_available()
            if on_execution_provenance is not None and not callable(
                on_execution_provenance
            ):
                raise TypeError("on_execution_provenance must be callable")
            if not source.strip():
                raise ProtocolError("BSL cell is empty")
            lowerer = getattr(self._controller, "lowerer", None)
            cell_source = source
            from onec_runtime.bsl.notebook_cells import split_notebook_cell
            from onec_runtime.bsl.parser_target import PythonParserTarget

            parser_target = getattr(lowerer, "parser_target", None)
            visible_unit = self._notebook_source_unit(source, source_unit)
            visible_source = mapped_visible_source(source, visible_unit)
            visible_source_context = VisibleSourceContext({visible_unit: source})
            try:
                cell = split_notebook_cell(
                    parser_target
                    if isinstance(parser_target, PythonParserTarget)
                    else PythonParserTarget.from_generated(),
                    source,
                    source_unit=visible_unit,
                )
            except (BslLexError, BslParseError) as error:
                return self._source_failure_reply(
                    error,
                    visible_source,
                    stage=DiagnosticStage.PARSING,
                    visible_source_context=visible_source_context,
                )
            mapped_visible = cell.visible.source_map.map_offset(0)
            if mapped_visible.unit is None:
                raise ProtocolError("Notebook cell has no visible source identity")
            source_maps = OperationSourceMapBundle(
                mapped_visible.unit,
                cell.worker,
                cell.statements,
            )
            if source_maps.statement_execution is not None:
                cell_source = source_maps.statement_execution.text
            method_set_candidate, worker_artifact, candidate_catalog = (
                self._prepare_notebook_methods(cell)
            )
            if method_set_candidate is not None:
                visible_source_context = method_set_candidate.visible_source_context

            mode = (
                "capture" if self._controller.state is OperationState.CAPTURED else "main"
            )
            lowered = cell_source
            lowering = None
            context_before: tuple[str, ...] = self._namespace_names
            message_collector_key = ""
            if lowerer is not None and source_maps.statement_execution is not None:
                lowering_mode = LoweringMode(mode)
                observed_before = getattr(lowerer, "persistent_names", ())
                if isinstance(observed_before, tuple) and all(
                    isinstance(name, str) for name in observed_before
                ):
                    context_before = observed_before
                key_factory = getattr(self._controller, "message_collector_key", None)
                if callable(key_factory):
                    message_collector_key = key_factory(lowering_mode)
                assert source_maps.statement_execution is not None
                lowering_catalog = (
                    self._complete_notebook_catalog(candidate_catalog)
                    if candidate_catalog is not None
                    else (
                        None
                        if operation_pin is None
                        else self._operation_pin_catalog(operation_pin)
                    )
                )
                try:
                    lower_mapped = getattr(lowerer, "lower_mapped", None)
                    if callable(lower_mapped):
                        lowering = lower_mapped(
                            source_maps.statement_execution,
                            mode=lowering_mode,
                            message_collector_key=(
                                message_collector_key or "__onec_cell_messages"
                            ),
                            worker_exports=lowering_catalog,
                        )
                    else:
                        with self._temporary_worker_catalog(
                            lowerer,
                            lowering_catalog,
                        ):
                            lowering = lowerer.lower(
                                cell_source,
                                mode=lowering_mode,
                                message_collector_key=(
                                    message_collector_key or "__onec_cell_messages"
                                ),
                            )
                    lowering = self._with_generation_pin_prelude(
                        lowering,
                        None if candidate_catalog is not None else operation_pin,
                        mode=lowering_mode,
                    )
                except (BslLexError, BslParseError) as error:
                    self._restore_namespace_context(lowerer, context_before)
                    return self._source_failure_reply(
                        error,
                        source_maps.statement_execution,
                        stage=DiagnosticStage.PARSING,
                        visible_source_context=visible_source_context,
                    )
                except SemanticLoweringError as error:
                    self._restore_namespace_context(lowerer, context_before)
                    return self._source_failure_reply(
                        error,
                        source_maps.statement_execution,
                        stage=DiagnosticStage.LOWERING,
                        visible_source_context=visible_source_context,
                    )
                lowered = lowering.source
            def record_execution_provenance(
                handle: WorkerGenerationHandle | None = None,
            ) -> None:
                nonlocal lowering, lowered
                if handle is not None and lowering is not None:
                    lowering = self._with_worker_generation_prelude(
                        lowering, handle, mode=LoweringMode(mode)
                    )
                    lowered = lowering.source
                execution_provenance = self._build_execution_provenance(
                    visible_source_sha256=visible_unit.source_sha256,
                    mode=mode,
                    visible_source=visible_source,
                    statement_source=source_maps.statement_execution,
                    lowering=lowering,
                    worker_artifact=worker_artifact,
                )
                if on_execution_provenance is not None:
                    on_execution_provenance(execution_provenance)

            if worker_artifact is None:
                try:
                    record_execution_provenance()
                except BaseException:
                    self._restore_namespace_context(lowerer, context_before)
                    raise
            worker_only = False
            if worker_artifact is not None:
                capture_fence = (
                    self._capture_operation_fence()
                    if mode == "capture"
                    and source_maps.statement_execution is not None
                    else None
                )
                try:
                    self._publish_notebook_worker_artifact_locked(
                        worker_artifact, method_set_candidate=method_set_candidate,
                        on_prepared_generation=record_execution_provenance,
                    )
                except BslExecutionError as error:
                    self._restore_namespace_context(lowerer, context_before)
                    diagnostic = error.diagnostic
                    if diagnostic is None or self._poisoned_error is not None:
                        raise
                    reply = RuntimeReply(
                        RuntimeReplyKind.SOURCE_FAILED,
                        self._controller.operation_id,
                        self._controller.state,
                        error=diagnostic.runtime_summary,
                        succeeded=False,
                        messages=error.messages,
                        diagnostic=diagnostic,
                    )
                    if _reply_evidence is not None:
                        _reply_evidence(reply)
                    return reply
                except BaseException:
                    self._restore_namespace_context(lowerer, context_before)
                    raise
                if capture_fence is not None:
                    try:
                        capture_fence_changed = (
                            self._capture_operation_fence() != capture_fence
                        )
                    except BaseException:
                        capture_fence_changed = True
                    if capture_fence_changed:
                        self._poisoned_error = PoisonedRuntimeError(
                            "Runtime API is poisoned: CAPTURE fence changed during "
                            "Worker activation; controlled recovery is required"
                        )
                        raise self._poisoned_error
                if source_maps.statement_execution is None:
                    worker_only = True
                else:
                    if operation_pin is not None:
                        if mode == "main":
                            self._operation_generation_pin = None
                        else:
                            self._evaluation_generation_pin = None
                        self._release_generation_pin_locked(operation_pin)
                    operation_pin = self._worker_universe.pin_active()
                    if operation_pin.handle is not self._worker_generation_handle:
                        self._release_generation_pin_locked(operation_pin)
                        operation_pin = None
                        raise ProtocolError(
                            "Activated Worker generation changed while pinning MAIN"
                        )
                    if mode == "main":
                        self._operation_generation_pin = operation_pin
                    else:
                        self._evaluation_generation_pin = operation_pin
            if worker_only:
                assert self._worker_generation_handle is not None
                return RuntimeReply(
                    RuntimeReplyKind.WORKER_LOADED,
                    self._controller.operation_id,
                    self._controller.state,
                    result=self._worker_generation_handle,
                )
            self._require_operation_pin_dispatch_fence_locked(
                operation_pin,
                mode=LoweringMode(mode),
                capture_evaluation=mode == "capture",
            )
            if self._controller.state is OperationState.CAPTURED:
                execute_mapped = getattr(self._controller, "execute_mapped_capture", None)
                handoff: _PreparedCaptureExecution | None = None
                try:
                    if callable(execute_mapped):
                        assert source_maps.statement_execution is not None
                        mapped_execution = (
                            source_maps.statement_execution
                            if lowering is None
                            else lowering.mapped_source
                        )
                        dirty_roots = (
                            () if lowering is None else lowering.dirty_roots
                        )
                        primary_execution, normalize_capture_error = (
                            self._capture_execution_callbacks_locked(
                                dirty_roots,
                            )
                        )

                        def completion(
                            result: object,
                            error: BaseException | None,
                        ) -> object:
                            if error is None or isinstance(error, BslExecutionError):
                                for root in dirty_roots:
                                    self._pending_dirty_roots.setdefault(
                                        root.casefold(), root,
                                    )
                            if error is not None:
                                self._restore_namespace_context(
                                    lowerer, context_before,
                                )
                                return None
                            completed_reply = self._reply(result)
                            if _reply_evidence is not None:
                                _reply_evidence(completed_reply)
                            return self._finalize_namespace_reply(
                                completed_reply,
                                lowering=lowering,
                                context_before=context_before,
                                lowerer=lowerer,
                            )

                        def rejection(error: BaseException) -> None:
                            del error
                            self._restore_namespace_context(
                                lowerer, context_before,
                            )

                        handoff = _PreparedCaptureExecution(
                            execute=lambda: execute_mapped(
                                source,
                                mapped_execution,
                                visible_source_context=visible_source_context,
                                messages_intercepted=(
                                    0
                                    if lowering is None
                                    else lowering.messages_intercepted
                                ),
                                message_collector_key=message_collector_key,
                                worker_messages=self._notebook_worker_messages_enabled(),
                                worker_globals=self._notebook_worker_globals(),
                                dirty_roots=dirty_roots,
                                on_transport_dispatch=_dispatch_evidence,
                            ),
                            detach_pin=self._detach_capture_evaluation_pin_locked,
                            completion=completion,
                            release_writer=self._capture_owner_handoff,
                            release_waiter=self._capture_session_waiter_handoff,
                            primary_execution=primary_execution,
                            normalize_error=normalize_capture_error,
                            rejection=rejection,
                        )
                        result = self._execute_prepared_capture_handoff(handoff)
                        if handoff.transferred:
                            return result  # type: ignore[return-value]
                        reply = self._reply(result)
                    else:
                        raise ProtocolError(
                            "Runtime controller requires mapped CAPTURE execution"
                        )
                except BslExecutionError as error:
                    diagnostic = (
                        error.diagnostic
                        if handoff is not None and handoff.transferred
                        else self._worker_runtime_diagnostic(
                            str(error),
                            error.diagnostic,
                        )
                    )
                    reply = RuntimeReply(
                        RuntimeReplyKind.CAPTURE_CELL,
                        self._controller.operation_id,
                        self._controller.state,
                        error=_BSL_EXECUTION_FAILURE_SUMMARY,
                        succeeded=False,
                        messages=error.messages,
                        diagnostic=diagnostic,
                    )
                    if handoff is not None and handoff.transferred:
                        if _reply_evidence is not None:
                            _reply_evidence(reply)
                        if handoff.submitted and lowering is not None:
                            reply = replace(
                                reply,
                                changed_roots=lowering.persistent_write_roots,
                                capture_dirty_roots=lowering.dirty_roots,
                            )
                        return reply
                except BaseException:
                    if handoff is not None and handoff.transferred:
                        raise
                    self._restore_namespace_context(lowerer, context_before)
                    raise
                if _reply_evidence is not None:
                    _reply_evidence(reply)
                if lowering is not None:
                    for root in lowering.dirty_roots:
                        self._pending_dirty_roots.setdefault(root.casefold(), root)
                return self._finalize_namespace_reply(
                    reply,
                    lowering=lowering,
                    context_before=context_before,
                    lowerer=lowerer,
                )
            execute_mapped = getattr(self._controller, "execute_mapped_main", None)
            try:
                if callable(execute_mapped):
                    assert source_maps.statement_execution is not None
                    mapped_execution = (
                        source_maps.statement_execution
                        if lowering is None
                        else lowering.mapped_source
                    )
                    outcome = execute_mapped(
                        source,
                        mapped_execution,
                        visible_source_context=visible_source_context,
                        messages_intercepted=(
                            0 if lowering is None else lowering.messages_intercepted
                        ),
                        message_collector_key=message_collector_key,
                        worker_messages=self._notebook_worker_messages_enabled(),
                        worker_globals=self._notebook_worker_globals(),
                        capture_points=self._capture_points,
                        user_breakpoints=self._user_breakpoints,
                        on_transport_dispatch=_dispatch_evidence,
                    )
                    reply = self._reply(outcome)
                    if isinstance(outcome, MainCompletion):
                        reply = self._bind_unlocated_main_origin(
                            reply, outcome, visible_unit
                        )
                else:
                    raise ProtocolError(
                        "Runtime controller requires mapped MAIN execution"
                    )
            except BaseException:
                self._restore_namespace_context(lowerer, context_before)
                raise
            if _reply_evidence is not None:
                _reply_evidence(reply)
            return self._finalize_namespace_reply(
                reply,
                lowering=lowering,
                context_before=context_before,
                lowerer=lowerer,
            )

    def resume_debug_stop(
        self,
        *,
        timeout_s: float | None = None,
    ) -> RuntimeReply:
        with self._single_writer(), self._bounded_command_timeout(timeout_s):
            self._require_available()
            return self._resume_debug_stop_locked()

    def _resume_debug_stop_locked(self) -> RuntimeReply:
        if self._controller.state not in {
            OperationState.DEBUG_STOPPED,
            OperationState.CAPTURE_DEBUG_STOPPED,
        }:
            raise ProtocolError(
                "Resume requires a debug_stopped or capture_debug_stopped runtime"
            )
        capture_evaluation = self._controller.state is OperationState.CAPTURE_DEBUG_STOPPED
        reply: RuntimeReply | None = None
        resume_dispatched = False

        def mark_debug_resume_dispatched() -> None:
            nonlocal resume_dispatched
            resume_dispatched = True

        self._require_operation_pin_dispatch_fence_locked(
            self._evaluation_generation_pin if capture_evaluation else self._operation_generation_pin,
            mode=LoweringMode.CAPTURE,
            capture_evaluation=capture_evaluation,
            require_active=False,
        )
        try:
            try:
                reply = self._reply(
                    self._controller.resume_debug_stop(
                        on_transport_dispatch=mark_debug_resume_dispatched,
                    )
                )
            except BslExecutionError as error:
                if not (
                    capture_evaluation
                    and self._controller.state is OperationState.CAPTURED
                ):
                    raise
                diagnostic = self._worker_runtime_diagnostic(
                    str(error),
                    error.diagnostic,
                )
                reply = RuntimeReply(
                    RuntimeReplyKind.CAPTURE_CELL,
                    self._controller.operation_id,
                    self._controller.state,
                    error=_BSL_EXECUTION_FAILURE_SUMMARY,
                    succeeded=False,
                    messages=error.messages,
                    diagnostic=diagnostic,
                )
            self._finalize_pending_namespace(reply)
            return reply
        finally:
            # A continuation owns an already-paused operation.  Before the
            # controller observes transport dispatch, a local failure says
            # nothing about whether that operation completed; retain its exact
            # pin until a known reply or an outcome-unknown dispatch result.
            if reply is not None or resume_dispatched:
                if capture_evaluation:
                    self._finish_capture_evaluation_pin_locked(
                        reply=reply,
                        outcome_unknown=resume_dispatched and reply is None,
                    )
                else:
                    self._finalize_active_operation_pin_locked(
                        reply=reply,
                        outcome_unknown=resume_dispatched and reply is None,
                    )

    def resume_capture(
        self,
        *,
        dirty_roots: tuple[str, ...] = (),
        continuation_attempt_id: str | None = None,
    ) -> RuntimeReply:
        with self._single_writer():
            self._require_available()
            if self._controller.state is OperationState.CAPTURED:
                combined = dict(self._pending_dirty_roots)
                for root in dirty_roots:
                    combined.setdefault(root.casefold(), root)
                resume_arguments: dict[str, object] = {
                    "dirty_roots": tuple(combined.values())
                }
                if continuation_attempt_id is not None:
                    resume_arguments["continuation_attempt_id"] = (
                        continuation_attempt_id
                    )
                reply: RuntimeReply | None = None
                resume_dispatched = False

                def mark_resume_dispatched() -> None:
                    nonlocal resume_dispatched
                    resume_dispatched = True

                self._require_operation_pin_dispatch_fence_locked(
                    self._operation_generation_pin,
                    mode=LoweringMode.CAPTURE,
                )
                if self._operation_generation_pin is not None:
                    self._clear_capture_worker_generation_pin_locked()
                try:
                    resume_arguments["on_transport_dispatch"] = (
                        mark_resume_dispatched
                    )
                    reply = self._reply(
                        self._controller.resume(**resume_arguments)  # type: ignore[arg-type]
                    )
                    self._pending_dirty_roots.clear()
                    self._finalize_pending_namespace(reply)
                    return reply
                finally:
                    # See _resume_debug_stop_locked: this is a continuation
                    # of an existing MAIN, not a newly-created operation.
                    if reply is not None or resume_dispatched:
                        self._finalize_active_operation_pin_locked(
                            reply=reply,
                            outcome_unknown=resume_dispatched and reply is None,
                        )
            if self._controller.state in {
                OperationState.DEBUG_STOPPED,
            }:
                if dirty_roots:
                    raise ProtocolError(
                        "Dirty capture roots cannot be used at a user breakpoint"
                    )
                return self._resume_debug_stop_locked()
            if self._controller.state is OperationState.CAPTURE_DEBUG_STOPPED:
                raise ProtocolError(
                    "Pending CAPTURE evaluation requires resume_debug_stop"
                )
            raise ProtocolError(
                "Resume requires captured or debug_stopped runtime; current state "
                f"is {self._controller.state.value}"
            )

    def continuation_attempt_evidence(
        self, attempt_id: str
    ) -> ContinuationAttemptEvidence:
        with self._single_writer():
            evidence = getattr(
                self._controller, "continuation_attempt_evidence", None
            )
            if not callable(evidence):
                raise ProtocolError(
                    "Runtime controller has no continuation attempt evidence"
                )
            return evidence(attempt_id)

    def load_worker_modules(
        self,
        units: tuple[WorkerModuleUnit, ...],
        *,
        common_modules: CommonModuleCatalogSnapshot | SessionCommonModuleCatalog,
        breakpoint_policy: WorkerBreakpointReloadPolicy = (
            WorkerBreakpointReloadPolicy.STRICT
        ),
        profiler: PhaseRecorder | None = None,
    ) -> WorkerGenerationHandle:
        """Upsert modules and atomically publish the complete active graph."""
        with (
            self._single_writer(),
            self._prune_worker_caches_after_failure_locked(),
        ):
            self._require_available()
            if type(units) is not tuple or not units:
                raise ProtocolError("Worker module units must be a non-empty tuple")
            if not isinstance(
                common_modules,
                (CommonModuleCatalogSnapshot, SessionCommonModuleCatalog),
            ):
                raise ProtocolError("Worker common-module catalog is invalid")
            if any(
                not isinstance(unit, WorkerModuleUnit)
                for unit in units
            ):
                raise ProtocolError("Worker module catalog binding does not match")
            names = tuple(unit.logical_name.casefold() for unit in units)
            if len(names) != len(set(names)):
                raise ProtocolError("Worker module names must be unique")
            if "worker" in names:
                raise ProtocolError("Worker is reserved for notebook methods")

            active_units = self._worker_units_with_updates(units)
            ordered_names = tuple(sorted(active_units))
            models = self._worker_models_for_units(
                active_units,
                ordered_names,
                profiler=profiler,
            )
            catalog = self._resolve_worker_catalog(
                common_modules,
                active_units,
                ordered_names,
                models,
                profiler=profiler,
            )
            plans = self._worker_plans_for_models(
                models,
                ordered_names,
                catalog,
                profiler=profiler,
            )
            artifacts, staged_active, cache_additions = (
                self._build_worker_candidate(
                    active_units,
                    ordered_names,
                    models,
                    plans,
                    profiler=profiler,
                )
            )

            descriptors = tuple(artifacts)
            if self._notebook_worker_descriptor is not None:
                descriptors += (self._notebook_worker_descriptor,)
            handle = self._publish_worker_artifacts_locked(
                descriptors,
                lowering_catalog=self._descriptor_catalog(descriptors),
                profiler=profiler,
                breakpoint_policy=breakpoint_policy,
                module_syntax={name: models[name].syntax_index for name in ordered_names},
            )
            self._worker_active_modules = staged_active
            self._worker_module_artifacts.update(cache_additions)
            self._worker_catalog_snapshot = catalog
            self._prune_worker_caches_locked()
            return handle

    def confirmed_worker_module_units(
        self, handle: WorkerGenerationHandle,
    ) -> tuple[WorkerModuleUnit, ...]:
        """Read the complete source set of the current confirmed generation.

        This is local inventory access, not a target evaluation. An older or
        unconfirmed handle must never be relabeled as the current source set.
        """
        with self._single_writer():
            self._require_available()
            if (
                not isinstance(handle, WorkerGenerationHandle)
                or handle is not self._worker_generation_handle
            ):
                raise ProtocolError("Worker source generation is not current")
            return tuple(
                self._worker_active_modules[name].unit
                for name in sorted(self._worker_active_modules)
            )

    def _worker_units_with_updates(
        self,
        units: tuple[WorkerModuleUnit, ...],
    ) -> dict[str, WorkerModuleUnit]:
        active_units = {
            name: active.unit
            for name, active in self._worker_active_modules.items()
        }
        active_units.update(
            (unit.logical_name.casefold(), unit) for unit in units
        )
        return active_units

    def _worker_models_for_units(
        self,
        units: Mapping[str, WorkerModuleUnit],
        ordered_names: tuple[str, ...],
        *,
        profiler: PhaseRecorder | None,
    ) -> dict[str, ParsedModuleModel]:
        models: dict[str, ParsedModuleModel] = {}
        parser_identity = full_ast_parser_identity()
        for normalized in ordered_names:
            unit = units[normalized]
            current = self._worker_active_modules.get(normalized)
            source_hash = unit.mapped_source.artifact.source_sha256
            if (
                current is not None
                and current.model.source_sha256 == source_hash
                and current.model.parser_identity == parser_identity
            ):
                model = current.model
            else:
                model = parse_full_ast_module(
                    unit.mapped_source.text,
                    profiler=profiler,
                )
            if model.source_sha256 != source_hash:
                raise ProtocolError("Worker projected source identity changed")
            if model.parser_identity != parser_identity:
                raise ProtocolError("Worker parser provenance changed during parse")
            if model.syntax_index is None:
                raise ProtocolError("Worker projected syntax index is unavailable")
            self._module_syntax_registry.publish(
                self._worker_module_identity(unit), model.syntax_index
            )
            models[normalized] = model
        return models

    def _resolve_worker_catalog(
        self,
        source: CommonModuleCatalogSnapshot | SessionCommonModuleCatalog,
        units: Mapping[str, WorkerModuleUnit],
        ordered_names: tuple[str, ...],
        models: Mapping[str, ParsedModuleModel],
        *,
        profiler: PhaseRecorder | None,
    ) -> CommonModuleCatalogSnapshot:
        if isinstance(source, CommonModuleCatalogSnapshot):
            catalog = source
            self._require_monotonic_worker_catalog(catalog)
            for normalized in ordered_names:
                catalog.require(units[normalized].logical_name)
            return catalog

        candidate_names = worker_model_candidate_names(models.values())

        def resolve() -> CommonModuleCatalogSnapshot:
            source.resolve_candidates(candidate_names)
            return source.ensure_modules(
                units[name].logical_name for name in ordered_names
            )

        catalog = (
            resolve()
            if profiler is None
            else profiler.measure(
                "catalog_validation",
                resolve,
                item_count=lambda result: len(result.modules),
            )
        )
        self._require_monotonic_worker_catalog(catalog)
        return catalog

    def _worker_plans_for_models(
        self,
        models: Mapping[str, ParsedModuleModel],
        ordered_names: tuple[str, ...],
        catalog: CommonModuleCatalogSnapshot,
        *,
        profiler: PhaseRecorder | None,
    ) -> dict[str, tuple[ResolvedModulePlan, tuple[object, ...]]]:
        catalog_changed = catalog != self._worker_catalog_snapshot
        plans: dict[str, tuple[ResolvedModulePlan, tuple[object, ...]]] = {}
        for normalized in ordered_names:
            model = models[normalized]
            current = self._worker_active_modules.get(normalized)
            same_source = (
                current is not None
                and current.model.source_sha256 == model.source_sha256
                and current.model.parser_identity == model.parser_identity
            )
            if same_source and not catalog_changed:
                plan = current.plan
            else:
                def resolve() -> ResolvedModulePlan:
                    return resolve_worker_dependencies(
                        model,
                        catalog,
                        previous=current.plan if same_source else None,
                    )

                plan = (
                    resolve()
                    if profiler is None
                    else profiler.measure("dependency_analysis", resolve)
                )
            plans[normalized] = (plan, self._worker_semantic_plan_key(plan))
        return plans

    def _build_worker_candidate(
        self,
        units: Mapping[str, WorkerModuleUnit],
        ordered_names: tuple[str, ...],
        models: Mapping[str, ParsedModuleModel],
        plans: Mapping[str, tuple[ResolvedModulePlan, tuple[object, ...]]],
        *,
        profiler: PhaseRecorder | None,
    ) -> tuple[
        tuple[WorkerModuleArtifact, ...],
        dict[str, _ActiveWorkerModule],
        dict[tuple[str, str, int, str, str], WorkerModuleArtifact],
    ]:
        builder = self._worker_module_builder
        build = getattr(builder, "build", None)
        if not callable(build):
            raise ProtocolError("Worker module artifact builder is unavailable")
        artifacts: list[WorkerModuleArtifact] = []
        staged_active: dict[str, _ActiveWorkerModule] = {}
        cache_additions: dict[
            tuple[str, str, int, str, str], WorkerModuleArtifact
        ] = {}
        for normalized in ordered_names:
            unit = units[normalized]
            plan, plan_key = plans[normalized]
            current = self._worker_active_modules.get(normalized)
            descriptor_key = self._worker_unit_descriptor_key(unit)
            artifact = (
                current.artifact
                if (
                    current is not None
                    and self._worker_unit_descriptor_key(current.unit)
                    == descriptor_key
                    and current.semantic_plan_key == plan_key
                )
                else None
            )
            if artifact is None:
                lowered = lower_resolved_worker_module(
                    unit,
                    plan,
                    profiler=profiler,
                )
                visible_context = self._worker_visible_source_context(unit)
                artifact = (
                    build(lowered, visible_source_context=visible_context)
                    if profiler is None
                    else build(
                        lowered,
                        visible_source_context=visible_context,
                        profiler=profiler,
                    )
                )
                if not isinstance(artifact, WorkerModuleArtifact):
                    raise ProtocolError(
                        "Worker module artifact builder returned an invalid value"
                    )
                cache_additions[descriptor_key] = artifact
            artifacts.append(artifact)
            staged_active[normalized] = _ActiveWorkerModule(
                unit,
                models[normalized],
                plan,
                plan_key,
                artifact,
            )
        return tuple(artifacts), staged_active, cache_additions

    @staticmethod
    def _worker_visible_source_context(unit: WorkerModuleUnit) -> VisibleSourceContext:
        references = {
            reference
            for segment in unit.mapped_source.source_map.segments
            for reference in (segment.origin_ref, segment.anchor_ref)
            if isinstance(reference, SourceUnitRef)
        }
        return VisibleSourceContext(
            {reference: unit.mapped_source.text for reference in references}
        )

    @staticmethod
    def _worker_unit_descriptor_key(
        unit: WorkerModuleUnit,
    ) -> tuple[str, str, int, str, str]:
        return (
            unit.logical_name.casefold(),
            unit.kind,
            unit.revision,
            unit.mapped_source.artifact.source_sha256,
            unit.mapped_source.source_map_sha256,
        )

    @staticmethod
    def _worker_semantic_plan_key(
        plan: ResolvedModulePlan,
    ) -> tuple[object, ...]:
        return (
            plan.source.source_sha256,
            plan.source.parser_identity,
            plan.implicit_local_names,
            plan.forbidden_global_writes,
            plan.dependencies,
            tuple(
                (
                    method.source.normalized_name,
                    method.local_names,
                    method.implicit_local_names,
                    method.dependencies,
                    method.forbidden_global_writes,
                )
                for method in plan.methods
            ),
        )

    def _require_monotonic_worker_catalog(
        self,
        candidate: CommonModuleCatalogSnapshot,
    ) -> None:
        if not self._is_monotonic_worker_catalog(candidate):
            raise ProtocolError(
                "Worker common-module catalog is not a monotonic extension"
            )

    def _is_monotonic_worker_catalog(
        self,
        candidate: CommonModuleCatalogSnapshot,
    ) -> bool:
        current = self._worker_catalog_snapshot
        if current is None:
            return True
        if (
            candidate.profile != current.profile
            or candidate.preprocessor_profile != current.preprocessor_profile
            or candidate.revision < current.revision
        ):
            return False
        if candidate.revision == current.revision:
            return (
                candidate.modules == current.modules
                and candidate.sha256 == current.sha256
            )
        candidate_by_name = {
            item.canonical_name.casefold(): item for item in candidate.modules
        }
        if len(candidate_by_name) != len(candidate.modules):
            return False
        return all(
            candidate_by_name.get(item.canonical_name.casefold()) == item
            for item in current.modules
        )

    def _publish_worker_artifacts_locked(
        self,
        artifacts: tuple[WorkerModuleArtifact, ...],
        *,
        lowering_catalog: tuple[WorkerExport, ...] | None = None,
        prepared_catalog: tuple[object, ...] | None = None,
        before_promote: Callable[[], None] | None = None,
        on_prepared_generation: Callable[[WorkerGenerationHandle], None] | None = None,
        module_syntax: Mapping[str, ModuleSyntaxIndex] | None = None,
        profiler: PhaseRecorder | None = None,
        breakpoint_policy: WorkerBreakpointReloadPolicy = (
            WorkerBreakpointReloadPolicy.STRICT
        ),
    ) -> WorkerGenerationHandle:
        """Publish through the sole host/target universe and commit after swap."""
        if type(breakpoint_policy) is not WorkerBreakpointReloadPolicy:
            raise TypeError("worker breakpoint reload policy is required")
        # Snapshot candidates before remote work. Notebook-only publications
        # inherit the confirmed module versions without consulting MAIN's pin.
        candidate_syntax = MappingProxyType(dict(
            self._worker_syntax_generations.get(self._worker_generation_handle, {})
            if module_syntax is None else module_syntax
        ))
        effective_catalog = (
            None
            if lowering_catalog is None
            else self._notebook_effective_catalog(lowering_catalog)
        )
        candidate = self._worker_universe.prepare(
            artifacts,
            export_catalog=effective_catalog,
        )
        try:
            candidate_diagnostics = self._worker_candidate_diagnostics(candidate)
        except BaseException:
            self._worker_universe.discard(candidate)
            raise
        catalog = candidate.export_catalog
        lowerer = getattr(self._controller, "lowerer", None)
        catalog_admission = prepared_catalog
        if lowerer is not None:
            prepare_catalog = getattr(lowerer, "prepare_worker_exports", None)
            if not callable(prepare_catalog):
                self._worker_universe.discard(candidate)
                raise ProtocolError(
                    "Worker lowerer cannot prepare a generation export catalog"
                )
            if catalog_admission is None:
                try:
                    catalog_admission = prepare_catalog(catalog)
                except BaseException:
                    self._worker_universe.discard(candidate)
                    raise
        if on_prepared_generation is not None:
            try:
                on_prepared_generation(candidate.handle)
            except BaseException:
                self._worker_universe.discard(candidate)
                raise
        if before_promote is not None:
            try:
                before_promote()
            except BaseException:
                self._worker_universe.discard(candidate)
                raise
        if self._worker_breakpoints.list_statuses():
            (
                handle,
                breakpoint_plan,
                breakpoint_transaction_id,
            ) = self._publish_breakpoint_generation_locked(
                candidate,
                breakpoint_policy=breakpoint_policy,
                candidate_diagnostics=candidate_diagnostics,
                profiler=profiler,
            )
        else:
            breakpoint_plan = None
            try:
                handle = self._worker_universe_target.promote(
                    candidate,
                    profiler=profiler,
                )
            except BslExecutionError as error:
                raise remap_worker_artifact_stage_error(
                    error,
                    candidate_manifest_sha256=candidate.manifest.sha256,
                    candidate_artifacts=candidate_diagnostics,
                ) from None
            except WorkerPromotionOutcomeUnknown as error:
                self._worker_active_modules.clear()
                self._notebook_method_set = None
                self._notebook_worker_descriptor = None
                self._poisoned_error = error
                raise

        if lowerer is not None and catalog_admission is not None:
            commit_catalog = getattr(lowerer, "commit_worker_exports", None)
            if not callable(commit_catalog):
                self._worker_active_modules.clear()
                self._notebook_method_set = None
                self._notebook_worker_descriptor = None
                self._poisoned_error = PoisonedRuntimeError(
                    "Runtime API is poisoned: Worker generation was activated "
                    "but its export catalog could not be committed"
                )
                raise self._poisoned_error
            try:
                commit_catalog(catalog_admission)
            except BaseException as error:
                self._worker_active_modules.clear()
                self._notebook_method_set = None
                self._notebook_worker_descriptor = None
                self._poisoned_error = PoisonedRuntimeError(
                    "Runtime API is poisoned: Worker generation was activated "
                    "but its export catalog could not be committed"
                )
                raise self._poisoned_error from error
        if breakpoint_plan is not None:
            try:
                self._worker_breakpoints.commit(
                    breakpoint_plan,
                    workspace_confirmed=True,
                )
            except BaseException as error:
                self._worker_breakpoints.quarantine(breakpoint_plan)
                self._worker_active_modules.clear()
                self._notebook_method_set = None
                self._notebook_worker_descriptor = None
                self._poisoned_error = PoisonedRuntimeError(
                    "Runtime API is poisoned: Worker generation was activated "
                    "but its breakpoint catalog could not be committed"
                )
                self._record_worker_breakpoint_reload(
                    breakpoint_transaction_id,
                    candidate.handle,
                    breakpoint_policy,
                    WorkerBreakpointReloadOutcome.QUARANTINED,
                    breakpoint_plan,
                )
                raise self._poisoned_error from error
            self._record_worker_breakpoint_reload(
                breakpoint_transaction_id,
                candidate.handle,
                breakpoint_policy,
                WorkerBreakpointReloadOutcome.COMMITTED,
                breakpoint_plan,
            )
        else:
            try:
                self._worker_breakpoints.set_views(
                    self._worker_universe._retained_debug_views()
                )
            except BaseException as error:
                self._worker_active_modules.clear()
                self._notebook_method_set = None
                self._notebook_worker_descriptor = None
                self._poisoned_error = PoisonedRuntimeError(
                    "Runtime API is poisoned: Worker generation was activated "
                    "but its debug views could not be committed"
                )
                raise self._poisoned_error from error
        previous_api_owned_handle = self._api_owned_worker_generation_handle
        self._worker_generation_handle = handle
        self._worker_syntax_generations[handle] = candidate_syntax
        self._worker_generation_diagnostics[handle.manifest_sha256] = (
            candidate_diagnostics
        )
        self._worker_exports = tuple(catalog)
        self._api_owned_worker_generation_handle = handle
        if previous_api_owned_handle is not None:
            self._retire_superseded_api_generation_locked(previous_api_owned_handle)
        elif breakpoint_plan is not None:
            self._reconcile_confirmed_breakpoint_views_after_publication_locked()
        return handle

    def _reconcile_confirmed_breakpoint_views_after_publication_locked(
        self,
    ) -> None:
        """Make breakpoint views match host-confirmed roots without releasing."""
        breakpoint_plan: WorkerBreakpointPlan | None = None
        try:
            retained_views = self._worker_universe._retained_debug_views()
            breakpoint_plan = self._worker_breakpoints.prepare_release(
                retained_views
            )
            owner = self._worker_breakpoint_workspace_owner()
            previous = owner.confirmed_snapshot
            desired = owner.prepare(
                captures=previous.captures,
                ordinary_users=previous.ordinary_users,
                worker_slots=breakpoint_plan.desired_slots,
                shielded=previous.shielded,
            )
            self._install_worker_breakpoint_workspace(desired)
            self._worker_breakpoints.commit(
                breakpoint_plan,
                workspace_confirmed=True,
            )
        except BaseException as error:
            if breakpoint_plan is not None:
                self._worker_breakpoints.quarantine(breakpoint_plan)
            self._worker_active_modules.clear()
            self._notebook_method_set = None
            self._notebook_worker_descriptor = None
            if self._poisoned_error is None:
                self._poisoned_error = (
                    error
                    if isinstance(error, BreakpointWorkspaceOutcomeUnknown)
                    else PoisonedRuntimeError(
                        "Runtime API is poisoned: confirmed publication "
                        "followed by post-publication breakpoint "
                        "reconciliation failure"
                    )
                )
            if self._poisoned_error is error:
                raise
            raise self._poisoned_error from error

    def _retire_superseded_api_generation_locked(
        self,
        previous_handle: WorkerGenerationHandle,
    ) -> None:
        """Release one confirmed prior API publication after the new commit."""
        try:
            self._release_worker_lifecycle_locked(previous_handle)
        except BaseException as error:
            # The target root and public generation metadata already name the
            # new publication.  Do not claim rollback or prune speculative
            # caches; invalidate publication metadata and leave teardown as
            # the owner of every registry reference still retained.
            self._worker_active_modules.clear()
            self._notebook_method_set = None
            self._notebook_worker_descriptor = None
            if self._poisoned_error is None:
                self._poisoned_error = PoisonedRuntimeError(
                    "Runtime API is poisoned: confirmed publication followed "
                    "by retirement failure"
                )
            if self._poisoned_error is error:
                raise
            raise self._poisoned_error from error

    def _publish_breakpoint_generation_locked(
        self,
        candidate: WorkerUniverseCandidate,
        *,
        breakpoint_policy: WorkerBreakpointReloadPolicy,
        candidate_diagnostics: tuple[WorkerDiagnosticArtifact, ...],
        profiler: PhaseRecorder | None,
    ) -> tuple[WorkerGenerationHandle, WorkerBreakpointPlan, UUID]:
        transaction_id = uuid4()
        workspace_owner = self._worker_breakpoint_workspace_owner()
        previous_workspace = workspace_owner.confirmed_snapshot
        prepared = None
        plan: WorkerBreakpointPlan | None = None
        workspace_installed = False
        try:
            prepared = self._worker_universe_target.prepare_root(
                candidate,
                transaction_id=transaction_id,
                profiler=profiler,
            )
            candidate_view = self._worker_universe._candidate_debug_view(candidate)

            def prepare_breakpoint_map() -> WorkerBreakpointPlan:
                return self._worker_breakpoints.prepare_generation(
                    candidate_view,
                    breakpoint_policy,
                )

            plan = (
                prepare_breakpoint_map()
                if profiler is None
                else profiler.measure(
                    "worker_breakpoint_promotion_map",
                    prepare_breakpoint_map,
                    item_count=lambda result: len(result.desired_slots),
                )
            )
            desired_workspace = workspace_owner.prepare(
                captures=previous_workspace.captures,
                ordinary_users=previous_workspace.ordinary_users,
                worker_slots=plan.desired_slots,
                shielded=previous_workspace.shielded,
            )
            if profiler is None:
                self._install_worker_breakpoint_workspace(desired_workspace)
            else:
                profiler.measure(
                    "worker_breakpoint_workspace_install",
                    lambda: self._install_worker_breakpoint_workspace(
                        desired_workspace
                    ),
                    item_count=lambda _result: len(plan.desired_slots),
                )
            workspace_installed = True
            handle = self._worker_universe_target.swap_root(
                prepared,
                profiler=profiler,
            )
            return handle, plan, transaction_id
        except WorkerBreakpointConflict:
            if prepared is not None:
                try:
                    self._worker_universe_target.discard_root(prepared)
                except WorkerPromotionOutcomeUnknown as discard_error:
                    self._quarantine_breakpoint_publication_after_discard_loss(
                        discard_error,
                        transaction_id=transaction_id,
                        candidate_handle=candidate.handle,
                        policy=breakpoint_policy,
                        plan=plan,
                    )
                    raise
            self._record_worker_breakpoint_reload(
                transaction_id,
                candidate.handle,
                breakpoint_policy,
                WorkerBreakpointReloadOutcome.ABORTED,
                None,
            )
            raise
        except (BreakpointWorkspaceOutcomeUnknown, WorkerPromotionOutcomeUnknown) as error:
            if plan is not None:
                self._worker_breakpoints.quarantine(plan)
            workspace_owner.quarantine()
            if prepared is not None:
                self._worker_universe_target.quarantine_root(prepared)
            self._worker_active_modules.clear()
            self._notebook_method_set = None
            self._notebook_worker_descriptor = None
            self._poisoned_error = error
            self._record_worker_breakpoint_reload(
                transaction_id,
                candidate.handle,
                breakpoint_policy,
                WorkerBreakpointReloadOutcome.QUARANTINED,
                plan,
            )
            raise
        except BaseException as error:
            if workspace_installed:
                try:
                    restored = workspace_owner.prepare(
                        captures=previous_workspace.captures,
                        ordinary_users=previous_workspace.ordinary_users,
                        worker_slots=previous_workspace.worker_slots,
                        shielded=previous_workspace.shielded,
                    )
                    self._install_worker_breakpoint_workspace(restored)
                    if prepared is not None:
                        self._worker_universe_target.discard_root(prepared)
                except BaseException as restoration_error:
                    if plan is not None:
                        self._worker_breakpoints.quarantine(plan)
                    workspace_owner.quarantine()
                    self._worker_active_modules.clear()
                    self._notebook_method_set = None
                    self._notebook_worker_descriptor = None
                    self._poisoned_error = PoisonedRuntimeError(
                        "Runtime API is poisoned: Worker breakpoint publication "
                        "could not restore its previous state"
                    )
                    self._record_worker_breakpoint_reload(
                        transaction_id,
                        candidate.handle,
                        breakpoint_policy,
                        WorkerBreakpointReloadOutcome.QUARANTINED,
                        plan,
                    )
                    raise self._poisoned_error from restoration_error
            elif prepared is not None:
                try:
                    self._worker_universe_target.discard_root(prepared)
                except WorkerPromotionOutcomeUnknown as discard_error:
                    self._quarantine_breakpoint_publication_after_discard_loss(
                        discard_error,
                        transaction_id=transaction_id,
                        candidate_handle=candidate.handle,
                        policy=breakpoint_policy,
                        plan=plan,
                    )
                    raise
            self._record_worker_breakpoint_reload(
                transaction_id,
                candidate.handle,
                breakpoint_policy,
                WorkerBreakpointReloadOutcome.ABORTED,
                plan,
            )
            if isinstance(error, BslExecutionError):
                raise remap_worker_artifact_stage_error(
                    error,
                    candidate_manifest_sha256=candidate.manifest.sha256,
                    candidate_artifacts=candidate_diagnostics,
                ) from None
            raise

    def _quarantine_breakpoint_publication_after_discard_loss(
        self,
        error: WorkerPromotionOutcomeUnknown,
        *,
        transaction_id: UUID,
        candidate_handle: WorkerGenerationHandle,
        policy: WorkerBreakpointReloadPolicy,
        plan: WorkerBreakpointPlan | None,
    ) -> None:
        if plan is not None:
            self._worker_breakpoints.quarantine(plan)
        self._worker_active_modules.clear()
        self._notebook_method_set = None
        self._notebook_worker_descriptor = None
        self._poisoned_error = error
        self._record_worker_breakpoint_reload(
            transaction_id,
            candidate_handle,
            policy,
            WorkerBreakpointReloadOutcome.QUARANTINED,
            plan,
        )

    def _worker_breakpoint_workspace_owner(self) -> BreakpointWorkspaceController:
        owner = getattr(self._controller, "breakpoint_workspace_owner", None)
        if not isinstance(owner, BreakpointWorkspaceController):
            raise ProtocolError("Worker breakpoint workspace owner is unavailable")
        owner.require_confirmed()
        return owner

    def _install_worker_breakpoint_workspace(
        self,
        snapshot: WorkspaceSnapshot,
    ) -> None:
        install = getattr(self._controller, "install_worker_workspace", None)
        if not callable(install):
            raise ProtocolError("Worker breakpoint workspace installer is unavailable")
        install(snapshot)

    def _record_worker_breakpoint_reload(
        self,
        transaction_id: UUID,
        candidate_handle: WorkerGenerationHandle,
        policy: WorkerBreakpointReloadPolicy,
        outcome: WorkerBreakpointReloadOutcome,
        plan: WorkerBreakpointPlan | None,
    ) -> None:
        snapshot = self._worker_breakpoints.snapshot()
        self._last_worker_breakpoint_reload_report = WorkerBreakpointReloadReport(
            transaction_id,
            candidate_handle,
            policy,
            outcome,
            () if plan is None else plan.removal_details,
            (
                plan.removed_ids
                if plan is not None
                and outcome is WorkerBreakpointReloadOutcome.COMMITTED
                else ()
            ),
            (
                plan.next_snapshot.catalog_version
                if plan is not None
                and outcome is WorkerBreakpointReloadOutcome.COMMITTED
                else snapshot.catalog_version
            ),
        )

    def last_worker_breakpoint_reload_report(
        self,
    ) -> WorkerBreakpointReloadReport | None:
        with self._single_writer():
            return self._last_worker_breakpoint_reload_report

    def _worker_candidate_diagnostics(
        self,
        candidate: WorkerUniverseCandidate,
    ) -> tuple[WorkerDiagnosticArtifact, ...]:
        return self._worker_universe._candidate_diagnostics(candidate)

    def _prune_worker_caches_locked(self) -> None:
        """Retain cache payloads reachable from confirmed lifecycle roots only."""
        inventory = self._worker_universe._confirmed_live_inventory()
        if inventory is None:
            return
        retained_artifacts = {
            key: artifact
            for key, artifact in self._worker_module_artifacts.items()
            if key in inventory.descriptor_cache_identities
        }
        prune_binary_cache = getattr(
            self._worker_module_builder,
            "_prune_cache",
            None,
        )
        if callable(prune_binary_cache):
            prune_binary_cache(inventory.binary_keys)
        self._worker_module_artifacts = retained_artifacts
        self._worker_generation_diagnostics = {
            manifest_sha256: diagnostics
            for manifest_sha256, diagnostics in self._worker_generation_diagnostics.items()
            if manifest_sha256 in inventory.manifest_sha256s
        }

    @contextmanager
    def _prune_worker_caches_after_failure_locked(self) -> Iterator[None]:
        try:
            yield
        except BaseException:
            if self._poisoned_error is None:
                try:
                    self._prune_worker_caches_locked()
                except BaseException:
                    pass
            raise

    def release_worker_generation(self, handle: WorkerGenerationHandle) -> None:
        """Release the current API-owned generation; superseded handles are stale."""
        with self._single_writer():
            self._require_available()
            try:
                self._release_worker_lifecycle_locked(handle)
                if self._api_owned_worker_generation_handle is handle:
                    self._api_owned_worker_generation_handle = None
                self._prune_worker_caches_locked()
            except ProtocolError as error:
                if "stale" in str(error).casefold() or "released" in str(error).casefold():
                    raise StaleWorkerGeneration(
                        "Worker generation handle is stale or released"
                    ) from None
                raise

    def _release_worker_lifecycle_locked(
        self,
        release: WorkerGenerationHandle | OperationGenerationPin,
    ) -> None:
        if not self._worker_breakpoints.list_statuses():
            if type(release) is WorkerGenerationHandle:
                self._worker_universe_target.release(release)
            else:
                self._worker_universe_target.release_pin(release)
            self._worker_breakpoints.set_views(
                self._worker_universe._retained_debug_views()
            )
            return
        lifecycle_plan = self._worker_universe_target.preview_release(release)
        breakpoint_plan = self._worker_breakpoints.prepare_release(
            lifecycle_plan.remaining_views
        )
        owner = self._worker_breakpoint_workspace_owner()
        previous = owner.confirmed_snapshot
        desired = owner.prepare(
            captures=previous.captures,
            ordinary_users=previous.ordinary_users,
            worker_slots=breakpoint_plan.desired_slots,
            shielded=previous.shielded,
        )
        try:
            self._install_worker_breakpoint_workspace(desired)
        except BreakpointWorkspaceOutcomeUnknown as error:
            self._worker_breakpoints.quarantine(breakpoint_plan)
            self._poisoned_error = error
            raise
        try:
            self._worker_universe_target.commit_release(lifecycle_plan)
        except BaseException as error:
            try:
                restored = owner.prepare(
                    captures=previous.captures,
                    ordinary_users=previous.ordinary_users,
                    worker_slots=previous.worker_slots,
                    shielded=previous.shielded,
                )
                self._install_worker_breakpoint_workspace(restored)
            except BaseException as restoration_error:
                owner.quarantine()
                self._worker_breakpoints.quarantine(breakpoint_plan)
                self._poisoned_error = PoisonedRuntimeError(
                    "Runtime API is poisoned: Worker release workspace "
                    "could not be restored"
                )
                raise self._poisoned_error from restoration_error
            raise error
        try:
            self._worker_breakpoints.commit(
                breakpoint_plan,
                workspace_confirmed=True,
            )
        except BaseException as error:
            self._worker_breakpoints.quarantine(breakpoint_plan)
            self._poisoned_error = PoisonedRuntimeError(
                "Runtime API is poisoned: Worker lifecycle changed but its "
                "breakpoint catalog could not be committed"
            )
            raise self._poisoned_error from error

    def close(self) -> None:
        """Release all generation roots, pins and target registrations once."""
        with self._single_writer():
            if self._closed:
                return
            if (
                self._controller.state is OperationState.CAPTURED
                and self._operation_generation_pin is not None
            ):
                self._clear_capture_worker_generation_pin_locked()
            try:
                if self._controller.state is OperationState.RECOVERING:
                    self._worker_universe_target.abandon_target()
                else:
                    self._worker_universe_target.teardown()
            except WorkerPromotionOutcomeUnknown:
                raise
            except BaseException as error:
                self._poisoned_error = PoisonedRuntimeError(
                    "Runtime API cleanup could not release Worker generations"
                )
                raise self._poisoned_error from error
            self._operation_generation_pin = None
            self._preparing_generation_pin = None
            self._evaluation_generation_pin = None
            self._worker_generation_handle = None
            self._api_owned_worker_generation_handle = None
            prune_binary_cache = getattr(
                self._worker_module_builder,
                "_prune_cache",
                None,
            )
            if callable(prune_binary_cache):
                prune_binary_cache(frozenset())
            self._worker_module_artifacts.clear()
            self._worker_generation_diagnostics.clear()
            self._worker_syntax_generations.clear()
            self._module_syntax_registry = ModuleSyntaxRegistry()
            self._worker_active_modules.clear()
            self._notebook_method_set = None
            self._notebook_worker_descriptor = None
            self._worker_exports = ()
            self._prepared_source_units.clear()
            self._closed = True

    def _notebook_publication_artifacts(
        self, artifact: WorkerArtifact,
    ) -> tuple[WorkerModuleArtifact, ...]:
        descriptor = worker_module_artifact_from_notebook(
            artifact,
            revision=self._notebook_worker_revision + 1,
            target_profile=(
                "notebook-worker" if self._worker_catalog_snapshot is None
                else self._worker_catalog_snapshot.profile
            ),
        )
        return tuple(
            active.artifact for active in self._worker_active_modules.values()
        ) + (descriptor,)

    def _publish_notebook_worker_artifact_locked(
        self,
        artifact: WorkerArtifact,
        *,
        validated_catalog: tuple[WorkerExport, ...] | None = None,
        prepared_catalog: tuple[WorkerExport, ...] | None = None,
        method_set_candidate: NotebookMethodSet | None = None,
        on_prepared_generation: Callable[[WorkerGenerationHandle], None] | None = None,
    ) -> WorkerGenerationHandle:
        catalog = validate_production_worker_artifact(artifact)
        if validated_catalog is not None and catalog != validated_catalog:
            raise ProtocolError("Prepared Worker catalog identity changed")
        revision = self._notebook_worker_revision + 1
        artifacts = self._notebook_publication_artifacts(artifact)
        descriptor = artifacts[-1]
        def record_upload_planned() -> None:
            self._worker_journal.record(
                "reload-events.jsonl",
                "artifact_upload_planned",
                logical_name="Worker",
                runtime_generation=self._controller.runtime_generation,
                context_generation=self._context_generation,
                module_generation=revision,
            )
            self._worker_journal.flush()

        generation = self._publish_worker_artifacts_locked(
            artifacts,
            lowering_catalog=self._complete_notebook_catalog(tuple(catalog)),
            prepared_catalog=prepared_catalog,
            before_promote=record_upload_planned,
            on_prepared_generation=on_prepared_generation,
        )
        self._notebook_worker_descriptor = descriptor
        self._notebook_worker_revision = revision
        try:
            self._prune_worker_caches_locked()
        except BaseException as error:
            self._poisoned_error = PoisonedRuntimeError(
                "Runtime API is poisoned: notebook publication cleanup failed"
            )
            self._notebook_method_set = None
            self._notebook_worker_descriptor = None
            raise self._poisoned_error from error
        self._notebook_method_set = method_set_candidate
        return generation

    def materialize_table(
        self,
        handle: str,
        *,
        refs: str | ReferenceMode = ReferenceMode.PRESENTATION,
        ref_columns: dict[str, str | ReferenceMode] | None = None,
        uuid_suffix: str = "__uuid",
        chunk_size: int = 65_536,
        profiler: PhaseRecorder | None = None,
    ) -> pd.DataFrame:
        with self._single_writer():
            self._require_available()
            safe_handle = self._resolve_value_handle_locked(handle)
            return self._materialize_table_locked(
                safe_handle,
                refs=refs,
                ref_columns=ref_columns,
                uuid_suffix=uuid_suffix,
                profiler=profiler,
            )

    def materialize_value(
        self,
        handle: str,
        *,
        refs: str | ReferenceMode = ReferenceMode.PRESENTATION,
        ref_columns: dict[str, str | ReferenceMode] | None = None,
        uuid_suffix: str = "__uuid",
        chunk_size: int = 65_536,
        max_depth: int = 32,
        max_items: int = 100_000,
        max_bytes: int = 64 * 1024 * 1024,
        timeout_s: float | None = None,
        profiler: PhaseRecorder | None = None,
    ) -> object:
        del chunk_size
        with self._single_writer(), self._bounded_command_timeout(timeout_s):
            self._require_available()
            safe_handle = self._resolve_value_handle_locked(handle)
            route = self._materialization_kind_locked(safe_handle)
            if route == "table":
                return self._materialize_table_locked(
                    safe_handle,
                    refs=refs,
                    ref_columns=ref_columns,
                    uuid_suffix=uuid_suffix,
                    max_rows=max_items,
                    max_bytes=max_bytes,
                    profiler=profiler,
                )
            if route != "value":
                raise ProtocolError("1C value materialization route is invalid")
            mode = refs.value if isinstance(refs, ReferenceMode) else refs
            options = MaterializationOptions(
                refs=mode,
                max_depth=max_depth,
                max_items=max_items,
                max_bytes=max_bytes,
            )
            transfer = RuntimeValueTransfer(
                self._execute_worker_instruction,
                self._take_context_string,
                context_cleaner=self._drop_context_value,
                runtime_generation=lambda: self._controller.runtime_generation,
                context_generation=self._context_generation,
                profiler=profiler,
            )
            return transfer.materialize(safe_handle, options)

    def materialization_kind(
        self, handle: str, *, timeout_s: float | None = None
    ) -> str:
        with self._single_writer(), self._bounded_command_timeout(timeout_s):
            self._require_available()
            return self._materialization_kind_locked(self._resolve_value_handle_locked(handle))

    def materialize_value_payload(
        self,
        handle: str,
        *,
        refs: str = "presentation",
        max_depth: int,
        max_items: int,
        max_bytes: int,
        timeout_s: float | None = None,
        profiler: PhaseRecorder | None = None,
    ) -> bytes:
        with self._single_writer(), self._bounded_command_timeout(timeout_s):
            self._require_available()
            safe_handle = self._resolve_value_handle_locked(handle)
            if self._materialization_kind_locked(safe_handle) != "value":
                raise ProtocolError("1C value is not a recursive value payload")
            transfer = RuntimeValueTransfer(
                self._execute_worker_instruction,
                self._take_context_string,
                context_cleaner=self._drop_context_value,
                runtime_generation=lambda: self._controller.runtime_generation,
                context_generation=self._context_generation,
                profiler=profiler,
            )
            return transfer.payload(
                safe_handle,
                MaterializationOptions(refs, max_depth, max_items, max_bytes),
            )

    def materialize_table_payload(
        self,
        handle: str,
        *,
        refs: str | ReferenceMode = ReferenceMode.PRESENTATION,
        ref_columns: dict[str, str | ReferenceMode] | None = None,
        uuid_suffix: str = "__uuid",
        max_rows: int | None = None,
        max_bytes: int = 64 * 1024 * 1024,
        timeout_s: float | None = None,
        profiler: PhaseRecorder | None = None,
    ) -> bytes:
        with self._single_writer(), self._bounded_command_timeout(timeout_s):
            self._require_available()
            safe_handle = self._resolve_value_handle_locked(handle)
            if self._materialization_kind_locked(safe_handle) != "table":
                raise ProtocolError("1C value is not a tabular payload")
            transfer = CompactRuntimeTableTransfer(
                self._execute_worker_instruction,
                self._take_context_string,
                runtime_generation=lambda: self._controller.runtime_generation,
                context_generation=self._context_generation,
                context_cleaner=self._drop_context_value,
                schema_reader=self._inspect_compact_columns,
                max_text_size=((max_bytes + 2) // 3) * 4,
                max_payload_bytes=max_bytes,
                max_rows=max_rows,
                profiler=profiler,
            )
            return transfer.payload(
                safe_handle,
                ReferencePolicy(refs, ref_columns, uuid_suffix),
            )

    def project_value_payload(
        self,
        handle: str,
        *,
        kind: str,
        offset: int,
        limit: int | None,
        columns: tuple[str, ...],
        names: tuple[str, ...],
        max_depth: int,
        max_items: int,
        max_rows: int,
        max_bytes: int,
        timeout_s: float | None = None,
        refs: str | ReferenceMode = ReferenceMode.PRESENTATION,
        ref_columns: dict[str, str | ReferenceMode] | None = None,
        uuid_suffix: str = "__uuid",
    ) -> tuple[str, bytes]:
        """Build and serialize a bounded projection without scanning past its limit."""
        with self._single_writer(), self._bounded_command_timeout(timeout_s):
            self._require_available()
            safe_handle = self._resolve_value_handle_locked(handle)
            return self._project_value_payload_locked(
                safe_handle,
                kind=kind,
                offset=offset,
                limit=limit,
                columns=columns,
                names=names,
                max_depth=max_depth,
                max_items=max_items,
                max_rows=max_rows,
                max_bytes=max_bytes,
                refs=refs,
                ref_columns=ref_columns,
                uuid_suffix=uuid_suffix,
            )

    def _project_value_payload_locked(
        self,
        safe_handle: str,
        *,
        kind: str,
        offset: int,
        limit: int | None,
        columns: tuple[str, ...],
        names: tuple[str, ...],
        max_depth: int,
        max_items: int,
        max_rows: int,
        max_bytes: int,
        refs: str | ReferenceMode,
        ref_columns: dict[str, str | ReferenceMode] | None,
        uuid_suffix: str,
    ) -> tuple[str, bytes]:
        if (
            type(offset) is not int
            or offset < 0
            or offset > MAX_PROJECTION_POSITION
        ):
            raise ProtocolError("projection offset is invalid")
        if limit is not None and (type(limit) is not int or limit <= 0):
            raise ProtocolError("projection limit is invalid")
        if any(
            not isinstance(name, str)
            or re.fullmatch(r"[^\W\d]\w*", name, re.UNICODE) is None
            for name in (*columns, *names)
        ):
            raise ProtocolError("projection names must be BSL identifiers")
        if kind in {"slice", "table_rows"}:
            bound = max_rows if kind == "table_rows" else max_items
            if (
                limit is None
                or limit > bound
                or offset + limit > MAX_PROJECTION_POSITION
            ):
                raise ProtocolError("projection exceeds its server-owned budget")
        elif kind in {"fields", "keys"}:
            selected = names
            if not selected or len(selected) > max_items:
                raise ProtocolError("projection exceeds its server-owned budget")
        else:
            raise ProtocolError("projection kind is unsupported")

        context_key = f"__onec_projection_{uuid4().hex}"
        projection_handle = f"Контекст.{context_key}"
        instruction = self._projection_instruction(
            safe_handle,
            context_key=context_key,
            kind=kind,
            offset=offset,
            limit=limit,
            columns=columns,
            names=names,
        )
        try:
            self._execute_worker_instruction(instruction)
            if kind == "table_rows":
                transfer = CompactRuntimeTableTransfer(
                    self._execute_worker_instruction,
                    self._take_context_string,
                    runtime_generation=lambda: self._controller.runtime_generation,
                    context_generation=self._context_generation,
                    context_cleaner=self._drop_context_value,
                    schema_reader=self._inspect_compact_columns,
                    max_text_size=((max_bytes + 2) // 3) * 4,
                    max_payload_bytes=max_bytes,
                    max_rows=limit,
                )
                return (
                    "compact_table",
                    transfer.payload(
                        projection_handle,
                        ReferencePolicy(refs, ref_columns, uuid_suffix),
                    ),
                )
            transfer = RuntimeValueTransfer(
                self._execute_worker_instruction,
                self._take_context_string,
                context_cleaner=self._drop_context_value,
                runtime_generation=lambda: self._controller.runtime_generation,
                context_generation=self._context_generation,
            )
            return (
                "value",
                transfer.payload(
                    projection_handle,
                    MaterializationOptions(
                        refs.value if isinstance(refs, ReferenceMode) else refs,
                        max_depth,
                        max_items,
                        max_bytes,
                    ),
                ),
            )
        finally:
            self._drop_context_value(context_key)

    def project_to_df(
        self,
        handle: str,
        selection: dict[str, object],
        *,
        refs: str | ReferenceMode = ReferenceMode.PRESENTATION,
        ref_columns: dict[str, str | ReferenceMode] | None = None,
        uuid_suffix: str = "__uuid",
        chunk_size: int | None = None,
        max_rows: int = 10_000,
        max_bytes: int = 64 * 1024 * 1024,
        timeout_s: float | None = None,
    ) -> pd.DataFrame:
        del chunk_size
        offset, limit = self._bounded_slice(selection, maximum=max_rows)
        with self._single_writer(), self._bounded_command_timeout(timeout_s):
            self._require_available()
            safe_handle = self._resolve_value_handle_locked(handle)
            kind, payload = self._project_value_payload_locked(
                safe_handle,
                kind="table_rows",
                offset=offset,
                limit=limit,
                columns=(),
                names=(),
                max_depth=1,
                max_items=limit,
                max_rows=max_rows,
                max_bytes=max_bytes,
                refs=refs,
                ref_columns=ref_columns,
                uuid_suffix=uuid_suffix,
            )
            if kind != "compact_table":
                raise ProtocolError("table projection returned an invalid payload kind")
            return decode_compact_table_payload(
                payload, ReferencePolicy(refs, ref_columns, uuid_suffix)
            )

    def project_value(
        self,
        handle: str,
        selection: dict[str, object],
        *,
        refs: str = "presentation",
        ref_columns: dict[str, str] | None = None,
        uuid_suffix: str = "__uuid",
        chunk_size: int | None = None,
        max_depth: int = 32,
        max_items: int = 100_000,
        max_bytes: int = 64 * 1024 * 1024,
        timeout_s: float | None = None,
    ) -> object:
        del chunk_size
        offset, limit = self._bounded_slice(selection, maximum=max_items)
        with self._single_writer(), self._bounded_command_timeout(timeout_s):
            self._require_available()
            safe_handle = self._resolve_value_handle_locked(handle)
            route = self._materialization_kind_locked(safe_handle)
            projection_kind = "table_rows" if route == "table" else "slice"
            kind, payload = self._project_value_payload_locked(
                safe_handle,
                kind=projection_kind,
                offset=offset,
                limit=limit,
                columns=(),
                names=(),
                max_depth=1 if route == "table" else max_depth,
                max_items=limit if route == "table" else max_items,
                max_rows=max_items if route == "table" else limit,
                max_bytes=max_bytes,
                refs=refs,
                ref_columns=ref_columns,
                uuid_suffix=uuid_suffix,
            )
            if route == "table":
                if kind != "compact_table":
                    raise ProtocolError(
                        "table projection returned an invalid payload kind"
                    )
                return decode_compact_table_payload(
                    payload, ReferencePolicy(refs, ref_columns, uuid_suffix)
                )
            if kind != "value":
                raise ProtocolError("value projection returned an invalid payload kind")
            return decode_value_payload(
                payload,
                MaterializationOptions(refs, max_depth, max_items, max_bytes),
            )

    @staticmethod
    def _bounded_slice(
        selection: dict[str, object], *, maximum: int
    ) -> tuple[int, int]:
        if not isinstance(selection, dict) or set(selection) != {"offset", "limit"}:
            raise ProtocolError("bounded projection requires offset and limit")
        offset = selection["offset"]
        limit = selection["limit"]
        if (
            type(offset) is not int
            or type(limit) is not int
            or offset < 0
            or limit <= 0
            or limit > maximum
            or offset > MAX_PROJECTION_POSITION
            or offset + limit > MAX_PROJECTION_POSITION
        ):
            raise ProtocolError("bounded projection exceeds its limit")
        return offset, limit

    @staticmethod
    def _projection_instruction(
        handle: str,
        *,
        context_key: str,
        kind: str,
        offset: int,
        limit: int | None,
        columns: tuple[str, ...],
        names: tuple[str, ...],
    ) -> str:
        lines: list[str]
        if kind == "slice":
            lines = [
                "ПроекцияЗначения = Новый Массив;",
                f"Для ИндексПроекции = {offset} По Мин({handle}.Количество() - 1, {offset + (limit or 0) - 1}) Цикл",
                f"    ПроекцияЗначения.Добавить({handle}[ИндексПроекции]);",
                "КонецЦикла;",
            ]
        elif kind == "table_rows":
            lines = [
                "СтрокиПроекции = Новый Массив;",
                f"Для ИндексПроекции = {offset} По Мин({handle}.Количество() - 1, {offset + (limit or 0) - 1}) Цикл",
                f"    СтрокиПроекции.Добавить({handle}[ИндексПроекции]);",
                "КонецЦикла;",
            ]
            column_argument = (
                ""
                if not columns
                else ", " + bsl_string_literal(",".join(columns))
            )
            lines.append(
                f"ПроекцияЗначения = {handle}.Скопировать(СтрокиПроекции{column_argument});"
            )
        elif kind == "fields":
            lines = ["ПроекцияЗначения = Новый Структура;"]
            lines.extend(
                "ПроекцияЗначения.Вставить("
                f"{bsl_string_literal(name)}, {handle}.{name});"
                for name in names
            )
        else:
            lines = ["ПроекцияЗначения = Новый Соответствие;"]
            lines.extend(
                "ПроекцияЗначения.Вставить("
                f"{bsl_string_literal(name)}, {handle}.Получить({bsl_string_literal(name)}));"
                for name in names
            )
        lines.extend(
            (
                f"Контекст.Вставить({bsl_string_literal(context_key)}, ПроекцияЗначения);",
                "Результат = Истина;",
            )
        )
        return "\n".join(lines)

    def _materialization_kind_locked(self, safe_handle: str) -> str:
        route = self._execute_worker_instruction(
            "Результат = RuntimeValueTransferServer."
            f"ПолучитьВидМатериализации({safe_handle});"
        )
        if route not in {"value", "table"}:
            raise ProtocolError("1C value materialization route is invalid")
        return route

    def _resolve_value_handle_locked(self, handle: str) -> str:
        self._require_public_value_handle_locked(handle)
        if isinstance(handle, str) and handle.startswith("capture_table_"):
            self._require_capture_inspection_available()
            return validate_value_handle(self._controller.capture_value_handle(handle))
        return validate_value_handle(handle)

    def require_public_value_handle(self, handle: str) -> None:
        """Reject target Worker roots/modules before any public proxy can escape."""
        with self._single_writer():
            self._require_available()
            self._require_public_value_handle_locked(handle)

    def require_public_value_handles(self, handles: tuple[str, ...]) -> None:
        """Prove every handle public with at most one target privacy instruction."""
        with self._single_writer():
            self._require_available()
            if not isinstance(handles, tuple) or any(
                not isinstance(handle, str) for handle in handles
            ):
                raise ProtocolError("value handles must be a tuple of strings")
            self._require_public_value_handles_locked(handles)

    def _require_public_value_handle_locked(self, handle: object) -> None:
        self._require_public_value_handles_locked((handle,))

    def _public_value_guard_handle_locked(self, handle: object) -> str | None:
        if not isinstance(handle, str):
            return None
        normalized = handle.casefold()
        if normalized.startswith(
            "Контекст.RuntimeWorkerActiveGeneration".casefold()
        ) or normalized.startswith(
            f"Контекст.{_RESERVED_WORKER_ROOT_CONTEXT_SLOT}".casefold()
        ) or normalized.startswith("__OnecPinnedWorkerGeneration".casefold()):
            raise ProtocolError("Worker generation objects are not public values")
        if handle.startswith("capture_table_metadata_"):
            is_metadata = getattr(
                self._controller, "is_capture_metadata_handle", None
            )
            if not callable(is_metadata) or is_metadata(handle) is not True:
                raise ProtocolError(
                    "Worker generation objects are not public values"
                )
            # An admitted inventory handle identifies metadata, not a target
            # value.  Its controller-owned identity is the complete privacy
            # proof; materialization still rejects it via capture_value_handle.
            return None
        if handle.startswith(("capture_table_", "capture_manager_")):
            self._require_capture_inspection_available()
            safe_handle = validate_value_handle(
                self._controller.capture_value_handle(handle)
            )
        else:
            safe_handle = validate_value_handle(handle)
        return safe_handle

    def _require_public_value_handles_locked(self, handles: tuple[object, ...]) -> None:
        # Resolve and admit the entire batch before any target privacy work.
        safe_handles = tuple(
            safe_handle
            for handle in handles
            if (safe_handle := self._public_value_guard_handle_locked(handle))
            is not None
        )
        if not safe_handles or self._worker_generation_handle is None:
            return
        try:
            privacy_registrations = (
                self._worker_universe_target.privacy_registration_snapshot()
            )
        except BaseException:
            raise ProtocolError(
                "Worker generation objects are not public values"
            ) from None
        type_probes = tuple(
            line
            for index, registration in enumerate(privacy_registrations)
            for line in (
                f"ВременныйОбъектWorker{index} = "
                "ВнешниеОбработки.Создать("
                f"{bsl_string_literal(registration)}, Ложь);",
                "ТипыОбъектовWorker.Добавить(ТипЗнч("
                f"ВременныйОбъектWorker{index}));",
            )
        )
        instruction = (
            "// onec-worker-public-value-guard\n"
            "ТипыОбъектовWorker = Новый Массив;\n"
            + "\n".join(type_probes)
            + "\n"
            "ПроверяемыеЗначенияWorker = Новый Массив;\n"
            + "\n".join(
                f"ПроверяемыеЗначенияWorker.Добавить({safe_handle});"
                for safe_handle in safe_handles
            )
            + "\n"
            "ЕстьОбъектыWorker = Ложь;\n"
            "Для Каждого ПроверяемоеЗначениеWorker "
            "Из ПроверяемыеЗначенияWorker Цикл\n"
            "ТипПроверяемогоWorker = ТипЗнч(ПроверяемоеЗначениеWorker);\n"
            "ЭтоОбъектWorker = "
            "ТипыОбъектовWorker.Найти(ТипПроверяемогоWorker) "
            "<> Неопределено;\n"
            "Если Не ЭтоОбъектWorker "
            'И ТипПроверяемогоWorker = Тип("ФиксированнаяСтруктура") '
            "Тогда\n"
            "    ЭтоКореньWorker = "
            'ПроверяемоеЗначениеWorker.Свойство("ManifestSha256") '
            'И ПроверяемоеЗначениеWorker.Свойство("Modules") '
            'И ПроверяемоеЗначениеWorker.Свойство("Exports");\n'
            "    ВидКонтейнераWorker = Неопределено;\n"
            "    ЭтоЭкспортыWorker = "
            'ПроверяемоеЗначениеWorker.Свойство("Kind", ВидКонтейнераWorker) '
            'И ВидКонтейнераWorker = "OnecWorkerExportsV1" '
            'И ПроверяемоеЗначениеWorker.Свойство("Items");\n'
            "    ЭтоОбъектWorker = ЭтоКореньWorker Или ЭтоЭкспортыWorker;\n"
            "КонецЕсли;\n"
            "Если Не ЭтоОбъектWorker "
            'И ТипПроверяемогоWorker = Тип("ФиксированноеСоответствие") '
            "Тогда\n"
            "    Для Каждого ЭлементМодулейWorker "
            "Из ПроверяемоеЗначениеWorker Цикл\n"
            "        Если ТипыОбъектовWorker.Найти("
            "ТипЗнч(ЭлементМодулейWorker.Значение)) "
            "<> Неопределено Тогда\n"
            "            ЭтоОбъектWorker = Истина;\n"
            "            Прервать;\n"
            "        КонецЕсли;\n"
            "    КонецЦикла;\n"
            "КонецЕсли;\n"
            "ЕстьОбъектыWorker = ЕстьОбъектыWorker Или ЭтоОбъектWorker;\n"
            "КонецЦикла;\n"
            "Результат = ЕстьОбъектыWorker;"
        )
        try:
            forbidden = self._worker_instruction_executor(instruction)
        except BaseException:
            raise ProtocolError(
                "Worker generation objects are not public values"
            ) from None
        if forbidden is not False:
            raise ProtocolError("Worker generation objects are not public values")

    def _materialize_table_locked(
        self,
        handle: str,
        *,
        refs: str | ReferenceMode,
        ref_columns: dict[str, str | ReferenceMode] | None,
        uuid_suffix: str,
        max_rows: int | None = None,
        max_bytes: int = 75_000_000,
        profiler: PhaseRecorder | None = None,
    ) -> pd.DataFrame:
        transfer = CompactRuntimeTableTransfer(
            self._execute_worker_instruction,
            self._take_context_string,
            runtime_generation=lambda: self._controller.runtime_generation,
            context_generation=self._context_generation,
            context_cleaner=self._drop_context_value,
            schema_reader=self._inspect_compact_columns,
            max_text_size=((max_bytes + 2) // 3) * 4,
            max_payload_bytes=max_bytes,
            max_rows=max_rows,
            profiler=profiler,
        )
        return transfer.to_df(
            handle,
            ReferencePolicy(refs, ref_columns, uuid_suffix),
        )

    def _inspect_compact_columns(self, table_handle: str):  # type: ignore[no-untyped-def]
        with self._remaining_command_timeout():
            declared_rows = self._controller.inspect_declared_table_schema(
                table_handle
            ).collection_rows
        declared = infer_declared_compact_columns(declared_rows)
        if declared is not None:
            return declared
        with self._remaining_command_timeout():
            sample_rows = self._controller.inspect_table_sample(
                table_handle
            ).collection_rows
        return infer_compact_columns(sample_rows)

    def _take_context_string(self, key: str, max_text_size: int) -> str:
        with self._remaining_command_timeout():
            with self._capture_helper_writer_handoff():
                return self._controller.take_context_string(
                    key,
                    max_text_size=max_text_size,
                )

    def _drop_context_value(self, key: str) -> None:
        with self._capture_helper_writer_handoff():
            self._controller.drop_context_value(key)

    def _execute_worker_instruction(self, source: str) -> object:
        with self._remaining_command_timeout():
            if self._controller.state is OperationState.CAPTURED:
                with self._capture_helper_writer_handoff():
                    cell = self._controller.execute_system_capture(
                        source + "\nРезультатИнструкции = Результат;"
                    )
                return cell.result
            completion = self._controller.execute_system_main(source)
            if not completion.succeeded:
                raise BslExecutionError(
                    completion.error,
                    messages=completion.messages,
                    diagnostic=completion.diagnostic,
                )
            return completion.result

    def _finalize_namespace_reply(
        self,
        reply: RuntimeReply,
        *,
        lowering: object | None,
        context_before: tuple[str, ...],
        lowerer: object | None,
    ) -> RuntimeReply:
        if lowering is None:
            return reply
        names = getattr(lowering, "context_names", ())
        if not isinstance(names, tuple) or any(not isinstance(name, str) for name in names):
            raise ProtocolError("Notebook lowering produced an invalid namespace catalog")
        persistent_writes = getattr(lowering, "persistent_write_roots", ())
        if not isinstance(persistent_writes, tuple) or any(
            not isinstance(root, str) for root in persistent_writes
        ):
            raise ProtocolError("Notebook lowering produced invalid persistent writes")
        dirty_roots = getattr(lowering, "dirty_roots", ())
        if not isinstance(dirty_roots, tuple) or any(
            not isinstance(root, str) for root in dirty_roots
        ):
            raise ProtocolError("Notebook lowering produced invalid capture roots")
        if not reply.succeeded:
            self._restore_namespace_context(lowerer, context_before)
            return replace(
                reply,
                changed_roots=persistent_writes,
                capture_dirty_roots=dirty_roots,
            )
        if reply.kind is RuntimeReplyKind.MAIN_COMPLETED:
            self._namespace_names = names
            self._pending_namespace_names = None
        elif reply.kind is RuntimeReplyKind.CAPTURE_CELL:
            before = {name.casefold() for name in context_before}
            additions = tuple(name for name in names if name.casefold() not in before)
            self._namespace_names = self._merge_namespace_names(
                self._namespace_names,
                additions,
            )
        elif reply.kind in {
            RuntimeReplyKind.CAPTURED,
            RuntimeReplyKind.DEBUG_STOPPED,
        }:
            self._pending_namespace_names = names
        return replace(
            reply,
            changed_roots=persistent_writes,
            capture_dirty_roots=dirty_roots,
        )

    def _finalize_pending_namespace(self, reply: RuntimeReply) -> None:
        pending = self._pending_namespace_names
        if pending is None:
            return
        if reply.kind is RuntimeReplyKind.MAIN_COMPLETED and reply.succeeded:
            self._namespace_names = self._merge_namespace_names(
                pending,
                self._namespace_names,
            )
            self._pending_namespace_names = None
        elif reply.kind is RuntimeReplyKind.CAPTURE_CELL and reply.succeeded:
            self._namespace_names = self._merge_namespace_names(
                self._namespace_names,
                pending,
            )
            self._pending_namespace_names = None
        elif not reply.succeeded:
            self._pending_namespace_names = None
            self._restore_namespace_context(
                getattr(self._controller, "lowerer", None),
                self._namespace_names,
            )

    @staticmethod
    def _restore_namespace_context(
        lowerer: object | None,
        names: tuple[str, ...],
    ) -> None:
        restore = getattr(lowerer, "restore_persistent_names", None)
        if callable(restore):
            restore(names)

    @contextmanager
    def _temporary_worker_catalog(
        self,
        lowerer: object,
        candidate_catalog: tuple[WorkerExport, ...] | None,
    ) -> Iterator[None]:
        """Lower against a prepared candidate without activating or retaining it."""
        if candidate_catalog is None:
            yield
            return
        prepare = getattr(lowerer, "prepare_worker_exports", None)
        commit = getattr(lowerer, "commit_worker_exports", None)
        force_commit = getattr(lowerer, "force_commit_worker_exports", None)
        if (
            not callable(prepare)
            or not callable(commit)
            or not callable(force_commit)
        ):
            raise ProtocolError(
                "Worker lowerer cannot prepare an isolated candidate catalog"
            )
        previous = prepare(self._worker_exports)
        candidate = prepare(candidate_catalog)
        try:
            commit(candidate)
            yield
        finally:
            try:
                force_commit(previous)
            except BaseException as error:
                self._poisoned_error = PoisonedRuntimeError(
                    "Runtime API is poisoned: the active worker export catalog "
                    "could not be restored after candidate preparation; controlled "
                    "recovery is required"
                )
                raise self._poisoned_error from error

    def _source_failure_reply(
        self,
        error: Exception,
        source: MappedSource,
        *,
        stage: DiagnosticStage,
        visible_source_context: VisibleSourceContext,
    ) -> RuntimeReply:
        diagnostic = normalize_source_error(
            error,
            source,
            stage=stage,
            visible_source_context=visible_source_context,
        )
        return RuntimeReply(
            RuntimeReplyKind.SOURCE_FAILED,
            self._controller.operation_id,
            self._controller.state,
            error=diagnostic.runtime_summary,
            succeeded=False,
            diagnostic=diagnostic,
        )

    @staticmethod
    def _visible_context_for_mapped(
        visible_source: str,
        mapped: MappedSource,
    ) -> VisibleSourceContext:
        digest = source_sha256(visible_source)
        units = {
            reference
            for segment in mapped.source_map.segments
            for reference in (segment.origin_ref, segment.anchor_ref)
            if isinstance(reference, SourceUnitRef)
            and reference.source_sha256 == digest
        }
        if not units:
            raise ProtocolError("Mapped execution has no matching visible source unit")
        return VisibleSourceContext({unit: visible_source for unit in units})

    @staticmethod
    def _merge_namespace_names(
        first: tuple[str, ...],
        second: tuple[str, ...],
    ) -> tuple[str, ...]:
        result: dict[str, str] = {}
        for name in (*first, *second):
            result.setdefault(name.casefold(), name)
        return tuple(result.values())

    def _require_available(self) -> None:
        if self._closed:
            raise ProtocolError("Runtime API is closed")
        if self._poisoned_error is not None:
            raise self._poisoned_error

    def _require_capture_inspection_available(self) -> None:
        if self._capture_inspection_quarantined:
            raise ProtocolError(
                "Capture inspection is quarantined; recover or close the runtime"
            )

    @contextmanager
    def _bounded_command_timeout(self, timeout_s: float | None) -> Iterator[None]:
        if timeout_s is None:
            yield
            return
        if (
            isinstance(timeout_s, bool)
            or not isinstance(timeout_s, (int, float))
            or not isfinite(float(timeout_s))
            or float(timeout_s) <= 0
        ):
            raise ProtocolError("command timeout must be a finite positive number")
        previous = self._active_command_deadline
        candidate = monotonic() + float(timeout_s)
        self._active_command_deadline = (
            candidate if previous is None else min(previous, candidate)
        )
        try:
            yield
        finally:
            self._active_command_deadline = previous

    @contextmanager
    def _remaining_command_timeout(self) -> Iterator[float | None]:
        deadline = self._active_command_deadline
        if deadline is None:
            yield None
            return
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise ProtocolError("materialization deadline exceeded")
        if not hasattr(self._controller, "command_timeout_s"):
            yield remaining
            if monotonic() > deadline:
                raise ProtocolError("materialization deadline exceeded")
            return
        original = getattr(self._controller, "command_timeout_s")
        if (
            isinstance(original, bool)
            or not isinstance(original, (int, float))
            or not isfinite(float(original))
            or float(original) <= 0
        ):
            raise ProtocolError("controller command timeout is invalid")
        setattr(self._controller, "command_timeout_s", min(float(original), remaining))
        try:
            yield min(float(original), remaining)
            if monotonic() > deadline:
                raise ProtocolError("materialization deadline exceeded")
        finally:
            setattr(self._controller, "command_timeout_s", original)

    @contextmanager
    def _capture_owner_handoff(self) -> Iterator[None]:
        """Drop the caller's writer boundary while the coordinator owns work."""
        owns_writer = self._writer_owner == get_ident()
        if owns_writer:
            self._writer_owner = None
            self._lock.release()
        try:
            yield
        finally:
            if owns_writer:
                self._lock.acquire()
                self._writer_owner = get_ident()

    @contextmanager
    def capture_session_caller_handoff(
        self,
        factory: Callable[[], AbstractContextManager[None]],
    ) -> Iterator[None]:
        """Bind one Session admission lock to this caller's CAPTURE wait."""
        if not callable(factory):
            raise TypeError("CAPTURE Session handoff factory must be callable")
        if getattr(self._session_waiter_handoffs, "factory", None) is not None:
            raise ProtocolError("CAPTURE Session handoff is already bound")
        self._session_waiter_handoffs.factory = factory
        try:
            yield
        finally:
            del self._session_waiter_handoffs.factory

    @contextmanager
    def _capture_session_waiter_handoff(self) -> Iterator[None]:
        factory = getattr(self._session_waiter_handoffs, "factory", None)
        if not callable(factory):
            yield
            return
        with factory():
            yield

    @contextmanager
    def _capture_helper_writer_handoff(self) -> Iterator[None]:
        """Keep helper admission local, but release the writer for remote work."""
        bind = getattr(
            self._controller,
            "capture_helper_caller_handoff",
            None,
        )
        if not callable(bind):
            yield
            return
        with bind(self._capture_owner_handoff):
            yield

    @contextmanager
    def _single_writer(self) -> Iterator[None]:
        if not self._lock.acquire(blocking=False):
            raise ProtocolError("Runtime is already executing another request")
        self._writer_owner = get_ident()
        try:
            yield
        finally:
            self._writer_owner = None
            self._lock.release()

    @contextmanager
    def _confirmed_single_writer(self) -> Iterator[None]:
        """Assert a locked-helper call without acquiring a second boundary."""
        if self._writer_owner != get_ident():
            raise ProtocolError("Runtime locked helper requires the single writer")
        yield

    def _worker_runtime_diagnostic(
        self,
        message: str,
        current: NormalizedDiagnostic | None,
    ) -> NormalizedDiagnostic | None:
        if not message:
            return current
        pin = (self._evaluation_generation_pin or self._operation_generation_pin
               or self._preparing_generation_pin)
        if pin is None:
            return current
        artifacts = self._worker_generation_diagnostics.get(
            pin.handle.manifest_sha256
        )
        if artifacts is None:
            return current
        return self._worker_runtime_diagnostic_from_artifacts(
            message,
            current,
            manifest_sha256=pin.handle.manifest_sha256,
            artifacts=artifacts,
        )

    @staticmethod
    def _worker_runtime_diagnostic_from_artifacts(
        message: str,
        current: NormalizedDiagnostic | None,
        *,
        manifest_sha256: str,
        artifacts: tuple[WorkerDiagnosticArtifact, ...],
    ) -> NormalizedDiagnostic | None:
        try:
            parsed = parse_platform_diagnostic(message)
            if not any(
                location.worker_artifact_location is not None
                for location in parsed.locations
            ):
                return current
            return remap_worker_runtime_diagnostic(
                parsed,
                pinned_manifest_sha256=manifest_sha256,
                pinned_artifacts=artifacts,
            )
        except (TypeError, ValueError):
            return current

    @staticmethod
    def _bind_unlocated_main_origin(
        reply: RuntimeReply,
        completion: MainCompletion,
        visible_unit: SourceUnitRef,
    ) -> RuntimeReply:
        """Bind an unlocated 1C cause to the exact completed notebook cell."""
        diagnostic = reply.diagnostic
        operation = completion.operation
        executed = operation.executed_source
        if (
            reply.succeeded
            or reply.kind is not RuntimeReplyKind.MAIN_COMPLETED
            or reply.operation_id != operation.operation_id
            or diagnostic is None
            or diagnostic.stage is not DiagnosticStage.EXECUTION
            or diagnostic.source_unit is not None
            or diagnostic.visible_location is not None
            or diagnostic.worker_frames
            or not isinstance(executed, MappedSource)
            or source_sha256(operation.visible_source) != visible_unit.source_sha256
            or diagnostic.execution_artifact_sha256
            != executed.artifact.source_sha256
            or diagnostic.source_map_sha256 != executed.source_map_sha256
        ):
            return reply
        return replace(reply, diagnostic=replace(diagnostic, source_unit=visible_unit))

    def _reply(
        self,
        result: MainCompletion | CapturedStop | CaptureCellResult | DebugStop,
    ) -> RuntimeReply:
        if isinstance(result, (MainCompletion, CaptureCellResult)) and isinstance(
            result.result, OperationGenerationPin
        ):
            raise ProtocolError("Worker generation objects are not public values")
        if isinstance(result, MainCompletion):
            diagnostic = self._worker_runtime_diagnostic(
                result.error,
                result.diagnostic,
            )
            return RuntimeReply(
                RuntimeReplyKind.MAIN_COMPLETED,
                result.operation.operation_id,
                self._controller.state,
                result.result,
                (
                    ""
                    if result.succeeded
                    else _BSL_EXECUTION_FAILURE_SUMMARY
                ),
                result.succeeded,
                messages=result.messages,
                diagnostic=diagnostic,
            )
        if isinstance(result, CapturedStop):
            self._capture_inspection_quarantined = False
            ticket = self._capture_ticket
            return RuntimeReply(
                RuntimeReplyKind.CAPTURED,
                result.operation.operation_id,
                self._controller.state,
                location=result.location,
                stop_sequence=result.stop_sequence,
                capture_ticket=(
                    ticket.ticket_id
                    if ticket is not None
                    and result.operation.operation_id == ticket.expected_operation_id
                    and result.observed_command_id == ticket.expected_operation_id
                    else None
                ),
                observed_command_id=result.observed_command_id,
            )
        if isinstance(result, CaptureCellResult):
            return RuntimeReply(
                RuntimeReplyKind.CAPTURE_CELL,
                result.operation_id,
                self._controller.state,
                result.result,
                messages=result.messages,
            )
        mapped_stop: RuntimeDebugStop | None = None
        pin = (
            self._evaluation_generation_pin
            if self._controller.state is OperationState.CAPTURE_DEBUG_STOPPED
            else self._operation_generation_pin
        )
        if pin is not None:
            view = self._worker_universe._operation_debug_view(pin)
            mapped_stop = map_worker_stop(
                result.stop,
                operation_id=result.operation.operation_id,
                view=view,
                origin=(
                    "capture_evaluation"
                    if self._controller.state
                    is OperationState.CAPTURE_DEBUG_STOPPED
                    else "main"
                ),
                bindings=self._worker_breakpoints.bindings_for_view(view),
                reason=result.reason,
            )
        return RuntimeReply(
            RuntimeReplyKind.DEBUG_STOPPED,
            result.operation.operation_id,
            self._controller.state,
            location=(
                result.stop.location
                if mapped_stop is None
                else mapped_stop.location
            ),
            debug_stop=mapped_stop,
        )

    def invalidate_capture_inspection(self) -> None:
        """Fail closed after uncertain preparation without resuming execution."""
        with self._single_writer():
            self._capture_inspection_quarantined = True
            self._capture_ticket = None
            invalidate = getattr(
                self._controller, "invalidate_capture_inspection", None
            )
            if not callable(invalidate):
                raise ProtocolError(
                    "Runtime controller cannot quarantine capture inspection"
                )
            invalidate()

    def capture_frame_variables(self, *, filters: Mapping[str, object], cursor: int, limit: int, timeout_s: float | None = None) -> Mapping[str, object]:
        with self._single_writer():
            self._require_available()
            self._require_capture_inspection_available()
            with self._bounded_command_timeout(timeout_s):
                with self._remaining_command_timeout() as remaining:
                    return self._controller.capture_frame_variables(
                        filters=filters, cursor=cursor, limit=limit,
                        timeout_s=remaining,
                    )

    def capture_stack(self, *, cursor: int, limit: int, timeout_s: float | None = None) -> Mapping[str, object]:
        with self._single_writer():
            self._require_available()
            self._require_capture_inspection_available()
            with self._bounded_command_timeout(timeout_s):
                with self._remaining_command_timeout() as remaining:
                    return self._controller.capture_stack(
                        cursor=cursor, limit=limit, timeout_s=remaining,
                    )

    def capture_frame(self, *, level: int, cursor: int, limit: int, name: str | None = None, timeout_s: float | None = None) -> Mapping[str, object]:
        with self._single_writer():
            self._require_available()
            self._require_capture_inspection_available()
            with self._bounded_command_timeout(timeout_s):
                with self._remaining_command_timeout() as remaining:
                    return self._controller.capture_frame(
                        level=level, cursor=cursor, limit=limit, name=name,
                        timeout_s=remaining,
                    )

    def resolve_capture_manager_origin(self, origin: ManagerOrigin, *, timeout_s: float | None = None) -> Mapping[str, object]:
        with self._single_writer():
            self._require_available()
            self._require_capture_inspection_available()
            if not isinstance(origin, ManagerOrigin) or origin.namespace != "frame":
                raise ProtocolError("capture manager origin must be frame-scoped")
            with self._bounded_command_timeout(timeout_s):
                with self._remaining_command_timeout() as remaining:
                    with self._capture_helper_writer_handoff():
                        return self._controller.resolve_capture_manager_origin(
                            origin.root, origin.fields, timeout_s=remaining
                        )

    def capture_temporary_tables(self, manager_handle: str, *, names: tuple[str, ...] | None, cursor: int, limit: int, selection: ValueSelection | None, timeout_s: float | None = None) -> Mapping[str, object]:
        with self._single_writer():
            self._require_available()
            self._require_capture_inspection_available()
            if selection is not None and (
                not isinstance(selection, ValueSelection)
                or selection.kind is not SelectionKind.TABLE_ROWS
            ):
                raise ProtocolError("capture table selection must be bounded table rows")
            wire_selection = (
                None
                if selection is None
                else {"offset": selection.offset, "limit": selection.limit, "columns": selection.columns}
            )
            with self._bounded_command_timeout(timeout_s):
                with self._remaining_command_timeout() as remaining:
                    with self._capture_helper_writer_handoff():
                        return self._controller.capture_temporary_tables(
                            manager_handle, names=names, cursor=cursor, limit=limit,
                            selection=wire_selection, timeout_s=remaining,
                        )
