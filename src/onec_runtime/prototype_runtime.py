from __future__ import annotations

from dataclasses import dataclass, field
from contextlib import AbstractContextManager, contextmanager, nullcontext
from enum import Enum
from hashlib import sha256
from math import isfinite
import json
import re
from collections.abc import Mapping, Sequence
from time import monotonic
from typing import Callable, cast
from functools import lru_cache
from threading import local
from uuid import UUID, uuid4

from onec_runtime.bsl import (
    DiagnosticStage,
    LoweringMode,
    MappedSource,
    NormalizedDiagnostic,
    SemanticNotebookLowerer,
    SourceArtifactKind,
    SourceSpan,
    SourceTransformBuilder,
    SourceUnitKind,
    SourceUnitRef,
    VisibleSourceContext,
    mapped_visible_source,
    parse_platform_diagnostic,
    remap_platform_diagnostic,
    source_sha256,
)
from onec_runtime.bsl.parser_target import PythonParserTarget
from onec_runtime.capture import (
    build_capture_transfer_call,
    build_live_capture_begin_call,
    build_live_capture_end_call,
    build_live_capture_root_transfer_call,
    build_live_current_capture_call,
    build_temporary_storage_value_expression,
)
from onec_runtime.capture_evaluation import (
    CaptureCleanupLease,
    CaptureEvaluationCoordinator,
    CaptureEvaluationKind,
    CaptureEvaluationRequest,
    CaptureEvaluationTicket,
    CaptureFence,
    CapturePhase,
    CaptureRemoteStep,
    CaptureStepContext,
)
from onec_runtime.capture_value_protocol import (
    CaptureValueInspectionEnvelope,
    MAX_CAPTURE_VALUE_NATIVE_CANDIDATES,
    NativeCandidatePage,
    build_capture_value_inspection_envelope,
)
from onec_runtime.capture_values import (
    CaptureValuePolicy,
    PrivateProjectedValue,
    PrivateValueProjection,
    SafePathSegment,
    SafeValuePath,
    ValueInspectionRequest,
    ValueMetadata,
    ValuePathSegmentKind,
    ValueRootKind,
    ValueViewKind,
    VariableRole,
)
from onec_runtime.breakpoint_workspace import (
    BreakpointWorkspaceController,
    BreakpointWorkspaceOutcomeUnknown,
    WorkspaceInstallReceipt,
    WorkspaceSnapshot,
)
from onec_runtime.errors import (
    BslExecutionError,
    CaptureBusyError,
    CaptureEvaluationDeliveryError,
    CaptureEvaluationPendingError,
    CaptureOutcomeUnknownError,
    CaptureRecoveryRequiredError,
    CaptureSourceUnavailableError,
    CaptureValueAccessDeniedError,
    CaptureValueCheckError,
    ProtocolError,
    RdbgTransportError,
    StaleCaptureError,
)
from onec_runtime.experiment import bsl_string_literal
from onec_runtime.fault_injection import FaultPoint
from onec_runtime.rdbg.models import (
    EvaluationResult,
    FrameVariable,
    ModuleLocation,
    PendingEvaluation,
    StackFrame,
    StopEvent,
    TargetId,
)
from onec_runtime.rdbg.reconnect import ReconnectedSession
from onec_runtime.rdbg.session import RdbgSession
from onec_runtime.recovery import (
    RecoveryCheckpoint,
    RecoveryOutcome,
    RecoveryPhase,
    RecoveryResult,
    RootWriteRecord,
    SideEffectStatus,
    validate_paused_identity,
)
from onec_runtime.recovery_journal import RecoveryJournal
from onec_runtime.stop_routing import (
    BreakpointRegistry,
    ClassifiedStop,
    StopReason,
    classify_stop,
)
from onec_runtime.table_value import evaluation_to_python


MAX_CAPTURE_PROJECTION_POSITION = 10_000_000
MAX_CAPTURE_SCHEMA_COLUMNS = 100
MAX_CAPTURE_VALUE_INSPECTION_SOURCE_BYTES = 64 * 1024
MAX_CAPTURE_VALUE_PATH_SEGMENTS = CaptureValuePolicy().max_depth + 1
# RDBG local-variable inventory is private input, not a public result.  Keep
# its pre-projection read finite even when the target reports an unexpected
# frame shape; the selected BSL roots remain capped at the protocol's 100.
MAX_CAPTURE_VALUE_NATIVE_INVENTORY = 10_000


@dataclass(frozen=True, slots=True, repr=False)
class CaptureValueInspectionPlan:
    """One prequalified target expression plus its closed result decoder.

    The plan builder is an internal target-protocol adapter.  Building it is
    local only; the controller submits ``source`` through its existing capture
    coordinator before ``decode`` can observe a target result.  This keeps
    descriptor construction and candidate routing free of target I/O.
    """

    source: str = field(repr=False)
    decode: Callable[[EvaluationResult], object] = field(
        repr=False, compare=False,
    )
    envelope: CaptureValueInspectionEnvelope | None = field(
        default=None, repr=False, compare=False,
    )

    def __post_init__(self) -> None:
        if (
            not isinstance(self.source, str)
            or not self.source
            or (
                len(self.source.encode("utf-8"))
                > MAX_CAPTURE_VALUE_INSPECTION_SOURCE_BYTES
            )
        ):
            raise ValueError("capture value inspection source is invalid")
        if not callable(self.decode):
            raise TypeError("capture value inspection decoder must be callable")
        if self.envelope is not None:
            if not isinstance(self.envelope, CaptureValueInspectionEnvelope):
                raise TypeError("capture value inspection envelope is invalid")
            if self.envelope.source != self.source:
                raise ValueError("capture value inspection plan source disagrees with envelope")


CaptureValueInspectionBuilder = Callable[..., CaptureValueInspectionPlan]


@dataclass(frozen=True, slots=True, repr=False)
class _NativeCaptureCandidateSelection:
    """The only private inventory data allowed into a native envelope."""

    candidates: tuple[str, ...]
    page: NativeCandidatePage | None = None


def _denied_capture_value_metadata() -> ValueMetadata:
    """A sealed denied record never has target-side metadata to expose."""
    raise CaptureValueAccessDeniedError("capture value is private")


def _seal_capture_projected_value(
    value: PrivateProjectedValue,
) -> PrivateProjectedValue:
    """Detach a qualified target result from its decoder before publication.

    ``PrivateProjectedValue.describe`` is deliberately lazy in the local
    adapter protocol.  A target plan must consume it while the coordinator
    owns the inspection, then replace it with a pure metadata closure.  That
    keeps page rendering, repr, and descendants from retaining a callback
    which could perform target I/O after the coordinator handoff.
    """
    if not isinstance(value, PrivateProjectedValue):
        raise CaptureValueCheckError("capture value target projection is invalid")
    if value.denied:
        return PrivateProjectedValue(
            value.name,
            _denied_capture_value_metadata,
            denied=True,
            cycle=value.cycle,
        )
    metadata = value.describe()
    if not isinstance(metadata, ValueMetadata):
        raise CaptureValueCheckError("capture value metadata is invalid")
    return PrivateProjectedValue(
        value.name,
        lambda: metadata,
        cycle=value.cycle,
    )


def _safe_platform_diagnostic(
    message: str,
    executed_source: MappedSource,
    *,
    visible_source_context: VisibleSourceContext | None,
) -> NormalizedDiagnostic | None:
    try:
        return remap_platform_diagnostic(
            parse_platform_diagnostic(message),
            executed_source,
            stage=DiagnosticStage.EXECUTION,
            visible_source_context=visible_source_context,
        )
    except Exception:
        return None


class OperationState(Enum):
    IDLE = "idle"
    MAIN_PENDING = "main_pending"
    CAPTURED = "captured"
    EVALUATING_CAPTURE = "evaluating_capture"
    CAPTURE_DEBUG_STOPPED = "capture_debug_stopped"
    DEBUG_STOPPED = "debug_stopped"
    FLUSHING = "flushing"
    PARTIAL_WRITEBACK_FAILURE = "partial_writeback_failure"
    BREAKPOINT_RESTORE_FAILURE = "breakpoint_restore_failure"
    RESUMING = "resuming"
    RECOVERING = "recovering"
    LOST = "lost"
    COMPLETED = "completed"
    FAILED = "failed"


class PartialWritebackError(ProtocolError):
    """At least one staged capture root could not be written to the frame."""


class BreakpointRestoreError(ProtocolError):
    """The full breakpoint workspace could not be restored while paused."""


class CompletionDecodeError(ProtocolError):
    """A MAIN completion scalar could not be decoded without exposing its value."""

    def __init__(self, phase: str, evaluation: EvaluationResult) -> None:
        super().__init__(f"MAIN completion {phase} cannot be decoded")
        self.phase = phase
        self.evaluation_type = evaluation.type_name
        self.exact_decimal_present = bool(evaluation.value_decimal)


@dataclass(frozen=True, slots=True)
class OperationHandle:
    operation_id: int
    visible_source: str = field(repr=False)
    lowered_source: str = field(repr=False)
    messages_intercepted: int = 0
    message_collector_key: str = ""
    executed_source: MappedSource | None = field(default=None, repr=False)
    visible_source_context: VisibleSourceContext | None = field(
        default=None,
        repr=False,
    )


@dataclass(frozen=True, slots=True)
class CapturedStop:
    operation: OperationHandle
    location: ModuleLocation
    stop_sequence: int
    variables: tuple[FrameVariable, ...]
    observed_command_id: int | None = None


@dataclass(frozen=True, slots=True)
class DebugStop:
    operation: OperationHandle
    stop: StopEvent
    reason: StopReason


@dataclass(frozen=True, slots=True)
class _PendingCaptureEvaluation:
    pending: PendingEvaluation
    operation: OperationHandle
    visible_source: str = field(repr=False)
    lowered_source: str = field(repr=False)
    executed_source: MappedSource = field(repr=False)
    visible_source_context: VisibleSourceContext | None = field(repr=False)
    messages_intercepted: int
    message_collector_key: str
    cell_fields: Mapping[str, object] = field(repr=False)


@dataclass(frozen=True, slots=True)
class _CaptureInitiatingWaiter:
    ticket: CaptureEvaluationTicket = field(repr=False)
    timeout_s: float

    def wait_initiator(self) -> object:
        return self.ticket.wait_initiator(self.timeout_s)


@dataclass(frozen=True, slots=True)
class _CaptureOwnedSubmission:
    pin_lease: Callable[[str], None] = field(repr=False)
    completion: Callable[[object, BaseException | None], object] = field(repr=False)
    primary_execution: Callable[[], None] = field(repr=False)
    normalize_error: Callable[[BslExecutionError], BslExecutionError] = field(
        repr=False,
    )


@dataclass(frozen=True, slots=True)
class BreakpointWorkspaceEvent:
    phase: str
    locations: tuple[ModuleLocation, ...]


@dataclass(frozen=True, slots=True)
class ContinuationAttemptSpec:
    """Opaque identity binding one continuation to its exact ordered roots."""

    attempt_id: str
    capture_generation: int
    request_operation_id: str
    dirty_roots: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.attempt_id, str) or not self.attempt_id or len(self.attempt_id) > 256:
            raise ValueError("continuation attempt_id is invalid")
        if type(self.capture_generation) is not int or self.capture_generation <= 0:
            raise ValueError("continuation capture_generation must be positive")
        if (
            not isinstance(self.request_operation_id, str)
            or not self.request_operation_id
            or len(self.request_operation_id) > 256
        ):
            raise ValueError("continuation request_operation_id is invalid")
        roots = tuple(self.dirty_roots)
        if len(roots) > 100:
            raise ValueError("continuation dirty_roots exceeds 100")
        if any(
            not isinstance(root, str)
            or not root
            or len(root) > 256
            or not root.isidentifier()
            for root in roots
        ):
            raise ValueError("continuation dirty_roots are invalid")
        if len({root.casefold() for root in roots}) != len(roots):
            raise ValueError("continuation dirty_roots must be unique")
        object.__setattr__(self, "dirty_roots", roots)


@dataclass(frozen=True, slots=True)
class ContinuationAttemptEvidence:
    root_statuses: tuple[tuple[str, str], ...]
    continue_state: str


@dataclass(slots=True)
class _ContinuationAttemptState:
    spec: ContinuationAttemptSpec
    root_statuses: dict[str, str]
    continue_state: str = "unattempted"
    irreversible_roots: set[str] = field(default_factory=set)


class _ControllerContinuationAdmission:
    """Exact rollback fence for one paused successor preparation."""

    def __init__(
        self,
        controller: "PrototypeRuntimeController",
        *,
        registry: BreakpointRegistry,
        capture_points: tuple[ModuleLocation, ...],
        workspace: tuple[ModuleLocation, ...],
        state: OperationState,
        attempt_id: str,
    ) -> None:
        self._controller = controller
        self._registry = registry
        self._capture_points = capture_points
        self._workspace = workspace
        self._state = state
        self.attempt_id = attempt_id
        self._closed = False

    def commit(self) -> None:
        self._closed = True

    def rollback(self) -> None:
        if self._closed:
            return
        attempt = self._controller._continuation_attempts[self.attempt_id]
        unsafe = (
            bool(attempt.irreversible_roots)
            or attempt.continue_state != "unattempted"
            or self._controller.state is OperationState.RECOVERING
        )
        if unsafe:
            self.quarantine()
            raise ProtocolError("continuation admission has irreversible evidence")
        try:
            self._controller._set_workspace(
                "capture_successor_restore", self._workspace
            )
        except BaseException as error:
            self.quarantine()
            raise ProtocolError(
                "capture successor breakpoint rollback is uncertain"
            ) from error
        self._controller.registry = self._registry
        self._controller.capture_points = self._capture_points
        self._controller.state = self._state
        self._closed = True

    def quarantine(self) -> None:
        self._controller._clear_capture_inspection()
        self._controller.state = OperationState.RECOVERING
        self._closed = True


@dataclass(frozen=True, slots=True)
class CaptureCellResult:
    operation_id: int
    visible_source: str = field(repr=False)
    lowered_source: str = field(repr=False)
    result: object
    messages: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MainCompletion:
    operation: OperationHandle
    result: object
    error: str
    succeeded: bool
    messages: tuple[str, ...] = ()
    diagnostic: NormalizedDiagnostic | None = None


@lru_cache(maxsize=1)
def _semantic_parser_target() -> PythonParserTarget:
    return PythonParserTarget.from_generated()


class PrototypeRuntimeController:
    """Single-writer in-process facade for the PrototypeProfile."""

    def __init__(
        self,
        session: RdbgSession,
        service_location: ModuleLocation,
        *,
        kernel_location: ModuleLocation | None = None,
        command_timeout_s: float = 60.0,
        runtime_generation: int = 1,
        journal: RecoveryJournal | None = None,
        fault_hook: Callable[[FaultPoint], None] | None = None,
        on_generation_lost: Callable[[RecoveryCheckpoint], None] | None = None,
        capture_value_inspection_builder: CaptureValueInspectionBuilder | None = None,
    ) -> None:
        if (
            capture_value_inspection_builder is not None
            and not callable(capture_value_inspection_builder)
        ):
            raise TypeError("capture value inspection builder must be callable")
        self.session = session
        self.service_location = service_location
        self.kernel_location = kernel_location or service_location
        self.command_timeout_s = command_timeout_s
        self.runtime_generation = runtime_generation
        self.journal = journal or RecoveryJournal()
        self.fault_hook = fault_hook
        self.on_generation_lost = on_generation_lost
        self.lowerer = SemanticNotebookLowerer(_semantic_parser_target().new_instance())
        self.state = OperationState.IDLE
        self.operation_id = 0
        self.active_operation: OperationHandle | None = None
        self.capture_points: tuple[ModuleLocation, ...] = ()
        self.registry = BreakpointRegistry(service_location)
        self.stop_sequence = 0
        self.last_capture_location: ModuleLocation | None = None
        self.capture_kernel_stack_level: int | None = None
        self.capture_frame_stack_level: int | None = None
        self._capture_frame_variables: tuple[FrameVariable, ...] = ()
        self._capture_stack_frames: tuple[StackFrame, ...] = ()
        self._capture_target_id: TargetId | None = None
        self._capture_manager_paths: dict[str, str] = {}
        self._capture_value_paths: dict[str, str] = {}
        self._capture_metadata_handles: set[str] = set()
        self._capture_value_inspection_builder = capture_value_inspection_builder
        self.checkpoint_sequence = 0
        self.recovery_checkpoint: RecoveryCheckpoint | None = None
        self.continue_sent = False
        self._generation_lost_notified = False
        self.cell_sequence = 0
        self.side_effect_sequence = 0
        self.write_journal: list[RootWriteRecord] = []
        self.stop_history: list[ClassifiedStop] = []
        self.last_debug_stop: DebugStop | None = None
        self.pending_capture_evaluation: _PendingCaptureEvaluation | None = None
        self._capture_evaluation_coordinator: CaptureEvaluationCoordinator | None = None
        self._capture_shutdown_transport_invalidated = False
        self._capture_owned_submission: _CaptureOwnedSubmission | None = None
        self._capture_helper_handoffs = local()
        self.breakpoint_workspaces: list[BreakpointWorkspaceEvent] = []
        self._breakpoint_workspace = self.registry.full_locations
        self.breakpoint_workspace_owner = BreakpointWorkspaceController(
            session,
            WorkspaceSnapshot(0, service_location, (), (), (), False),
        )
        self._continuation_attempts: dict[str, _ContinuationAttemptState] = {}
        self._active_continuation_attempt_id: str | None = None

    @staticmethod
    def _capture_identifier(value: object, *, name: str) -> str:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 256
            or re.fullmatch(r"[^\W\d]\w*", value, re.UNICODE) is None
        ):
            raise ProtocolError(f"capture {name} is invalid")
        return value

    def _require_capture_frame(self) -> int:
        self._require_state(OperationState.CAPTURED)
        if self.capture_frame_stack_level is None:
            raise ProtocolError("capture frame is not initialized")
        return self.capture_frame_stack_level

    def _clear_capture_inspection(self) -> None:
        self.capture_frame_stack_level = None
        self._capture_frame_variables = ()
        self._capture_stack_frames = ()
        self._capture_target_id = None
        self._capture_manager_paths.clear()
        self._capture_value_paths.clear()
        self._capture_metadata_handles.clear()

    def _replace_capture_evaluation_coordinator(self) -> None:
        previous = self._capture_evaluation_coordinator
        if previous is not None:
            previous.begin_close()
            if not previous.join(min(1.0, self.command_timeout_s)):
                raise ProtocolError("Previous CAPTURE coordinator did not stop")
        if self.active_operation is None or self._capture_target_id is None:
            raise ProtocolError("CAPTURE coordinator identity is incomplete")
        fence = CaptureFence(
            self.active_operation.operation_id,
            self.runtime_generation,
            self.stop_sequence,
            self._capture_target_id,
        )
        self._capture_evaluation_coordinator = CaptureEvaluationCoordinator(
            fence,
            poll_interval_s=min(0.1, self.command_timeout_s),
            journal=self.journal,
        )
        self._capture_shutdown_transport_invalidated = False

    def _capture_evaluation_owner(self) -> CaptureEvaluationCoordinator:
        owner = self._capture_evaluation_coordinator
        if owner is None:
            raise ProtocolError("CAPTURE evaluation coordinator is unavailable")
        return owner

    def _require_capture_evaluation_admission(self) -> None:
        if self.state is OperationState.CAPTURED:
            return
        owner = self._capture_evaluation_coordinator
        if owner is not None:
            status = owner.status(owner._fence)
            if status.phase is CapturePhase.EVALUATING and status.pending_evaluation_id:
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
        self._require_state(OperationState.CAPTURED)

    def _capture_remote_step(
        self,
        expression: str,
        *,
        stack_level: int,
        max_text_size: int = 307_200,
        timeout_s: float | None = None,
        before_dispatch: Callable[[], None] | None = None,
        pre_dispatch_cleanup: Callable[[], None] | None = None,
        on_transport_dispatch: Callable[[], None] | None = None,
        restore: Callable[[], None] | None = None,
    ) -> CaptureRemoteStep:
        def dispatch(dispatch_entered: Callable[[], None]) -> PendingEvaluation:
            prepared = False
            entered = False
            if before_dispatch is not None:
                before_dispatch()
            prepared = True

            def mark_transport_entry() -> None:
                nonlocal entered
                if on_transport_dispatch is not None:
                    on_transport_dispatch()
                dispatch_entered()
                entered = True

            try:
                return self.session.start_evaluation(
                    expression,
                    max_text_size=max_text_size,
                    stack_level=stack_level,
                    timeout_s=(
                        self.command_timeout_s
                        if timeout_s is None
                        else timeout_s
                    ),
                    on_transport_dispatch=mark_transport_entry,
                )
            except BaseException:
                cleanup = pre_dispatch_cleanup or restore
                if prepared and not entered and cleanup is not None:
                    cleanup()
                raise

        def poll(
            pending: PendingEvaluation,
            timeout_s: float,
        ) -> EvaluationResult | StopEvent:
            return self.session.wait_evaluation_event(
                pending,
                timeout_s=timeout_s,
            )

        return CaptureRemoteStep(
            dispatch,
            poll,
            restore if restore is not None else lambda: None,
        )

    def _complete_capture_lifecycle(
        self,
        value: object,
        error: BaseException | None,
    ) -> object:
        self.pending_capture_evaluation = None
        self.last_debug_stop = None
        if isinstance(error, StaleCaptureError):
            self.state = OperationState.LOST
        elif isinstance(
            error,
            (CaptureOutcomeUnknownError, CaptureRecoveryRequiredError),
        ):
            self.state = OperationState.RECOVERING
        else:
            self.state = OperationState.CAPTURED
        return value

    def _submit_capture_request(
        self,
        request: CaptureEvaluationRequest,
        *,
        return_ticket: bool = False,
        timeout_s: float | None = None,
        helper_handoff: bool = False,
    ) -> object:
        self.state = OperationState.EVALUATING_CAPTURE
        handoff = (
            getattr(self._capture_helper_handoffs, "factory")()
            if helper_handoff
            and callable(getattr(self._capture_helper_handoffs, "factory", None))
            else nullcontext()
        )
        with handoff:
            try:
                ticket = self._capture_evaluation_owner().submit_evaluation(request)
            except BaseException:
                if self.state is OperationState.EVALUATING_CAPTURE:
                    owner = self._capture_evaluation_owner()
                    status = owner.status(request.fence)
                    adopted = (
                        status.phase is CapturePhase.EVALUATING
                        and status.pending_evaluation_id is not None
                    )
                    if not adopted:
                        self.state = OperationState.CAPTURED
                raise
            if return_ticket:
                return ticket
            return ticket.wait_initiator(
                self.command_timeout_s if timeout_s is None else timeout_s
            )

    @contextmanager
    def capture_helper_caller_handoff(
        self,
        factory: Callable[[], AbstractContextManager[None]],
    ):  # type: ignore[no-untyped-def]
        """Bind one caller thread's writer release to helper submit/wait only."""
        if not callable(factory):
            raise TypeError("CAPTURE helper handoff factory must be callable")
        if getattr(self._capture_helper_handoffs, "factory", None) is not None:
            raise ProtocolError("CAPTURE helper handoff is already bound")
        self._capture_helper_handoffs.factory = factory
        try:
            yield
        finally:
            del self._capture_helper_handoffs.factory

    def submit_capture_execution(
        self,
        execute: Callable[[], object],
        *,
        pin_lease: Callable[[str], None],
        completion: Callable[[object, BaseException | None], object],
        primary_execution: Callable[[], None],
        normalize_error: Callable[[BslExecutionError], BslExecutionError],
    ) -> _CaptureInitiatingWaiter:
        """Bind one RuntimeApi ownership handoff to its controller submission."""
        if self._capture_owned_submission is not None:
            raise ProtocolError("CAPTURE ownership submission is already active")
        if not all(callable(callback) for callback in (
            execute,
            pin_lease,
            completion,
            primary_execution,
            normalize_error,
        )):
            raise TypeError("CAPTURE ownership callbacks must be callable")
        self._capture_owned_submission = _CaptureOwnedSubmission(
            pin_lease,
            completion,
            primary_execution,
            normalize_error,
        )
        try:
            ticket = execute()
        finally:
            self._capture_owned_submission = None
        if not isinstance(ticket, CaptureEvaluationTicket):
            raise ProtocolError("CAPTURE ownership submission did not return a ticket")
        return _CaptureInitiatingWaiter(ticket, self.command_timeout_s)

    def _evaluate_capture_helper(
        self,
        expression: str,
        *,
        evaluation_kind: CaptureEvaluationKind,
        stack_level: int,
        max_text_size: int = 307_200,
        result_policy: Callable[[EvaluationResult], object],
        timeout_s: float | None = None,
    ) -> object:
        self._require_capture_evaluation_admission()
        owner = self._capture_evaluation_owner()
        selected_timeout = (
            self.command_timeout_s if timeout_s is None else timeout_s
        )
        policy_failure: BaseException | None = None

        def apply_policy(result: EvaluationResult) -> object:
            nonlocal policy_failure
            try:
                return result_policy(result)
            except (BslExecutionError, ProtocolError) as error:
                policy_failure = error
                raise

        step = self._capture_remote_step(
            expression,
            stack_level=stack_level,
            max_text_size=max_text_size,
            timeout_s=selected_timeout,
        )
        try:
            return self._submit_capture_request(CaptureEvaluationRequest(
                owner._fence,
                evaluation_kind,
                step.dispatch,
                step.poll,
                apply_policy,
                restore=step.restore,
                completion=self._complete_capture_lifecycle,
            ), timeout_s=selected_timeout, helper_handoff=True)
        except (BslExecutionError, CaptureEvaluationDeliveryError):
            if policy_failure is not None:
                raise policy_failure
            raise

    def invalidate_capture_inspection(self) -> None:
        """Revoke captured-frame handles without resuming the suspended target."""
        self._clear_capture_inspection()

    def shutdown_capture_evaluation(self) -> bool:
        """Stop and classify the CAPTURE owner within the command deadline."""
        owner = self._capture_evaluation_coordinator
        if owner is None:
            return True
        owner.begin_close()
        invalidation_error: BaseException | None = None
        if not self._capture_shutdown_transport_invalidated:
            self._capture_shutdown_transport_invalidated = True
            try:
                self.session.invalidate()
            except BaseException as error:
                invalidation_error = error
        stopped = owner.join(min(1.0, self.command_timeout_s))
        owner.finish_close(stopped)
        if invalidation_error is not None:
            raise invalidation_error
        return stopped

    def _capture_command_deadline(self, timeout_s: float | None) -> float:
        selected = self.command_timeout_s if timeout_s is None else timeout_s
        for value in (selected, self.command_timeout_s):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(float(value))
                or float(value) <= 0
            ):
                raise ProtocolError(
                    "capture inspection timeout must be finite and positive"
                )
        return monotonic() + min(float(selected), float(self.command_timeout_s))

    @staticmethod
    def _capture_remaining_timeout(deadline: float) -> float:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise ProtocolError("capture inspection deadline exceeded")
        return remaining

    def _record(self, stream: str, event: str, **fields: object) -> None:
        self.journal.record(stream, event, **fields)

    def _flush_journal(self) -> None:
        self.journal.flush()

    def _checkpoint(self, phase: RecoveryPhase) -> RecoveryCheckpoint:
        if self.active_operation is None:
            raise ProtocolError("Recovery checkpoint has no active operation")
        target = self.session.target
        if target is None:
            raise ProtocolError("Recovery checkpoint has no selected target")
        self.checkpoint_sequence += 1
        checkpoint = RecoveryCheckpoint(
            sequence=self.checkpoint_sequence,
            runtime_generation=self.runtime_generation,
            operation_id=self.active_operation.operation_id,
            phase=phase,
            target=target,
            frame_location=self.last_capture_location,
            stop_sequence=self.stop_sequence,
            breakpoint_workspace=(
                self.breakpoint_workspace_owner.confirmed_snapshot.effective_locations
            ),
            write_journal=tuple(self.write_journal),
            continue_sent=self.continue_sent,
        )
        self.recovery_checkpoint = checkpoint
        self._record(
            "recovery-checkpoints.jsonl",
            "checkpoint",
            checkpoint=checkpoint,
        )
        self._flush_journal()
        return checkpoint

    def _inject(self, point: FaultPoint, phase: RecoveryPhase) -> None:
        checkpoint = self._checkpoint(phase)
        if self.fault_hook is None:
            return
        try:
            self.fault_hook(point)
        except RdbgTransportError as error:
            self.state = OperationState.RECOVERING
            self._record(
                "recovery-transitions.jsonl",
                "transport_lost",
                checkpoint_sequence=checkpoint.sequence,
                phase=phase.value,
                fault_point=point.value,
                error=str(error),
            )
            self._flush_journal()
            raise

    def _set_root_status(
        self,
        root: str,
        expression_sha256: str,
        status: SideEffectStatus,
        *,
        result_id: UUID | None = None,
        error: str = "",
    ) -> RootWriteRecord:
        existing_index = next(
            (
                index
                for index, entry in enumerate(self.write_journal)
                if entry.root == root
            ),
            None,
        )
        if existing_index is None:
            self.side_effect_sequence += 1
            sequence = self.side_effect_sequence
        else:
            sequence = self.write_journal[existing_index].sequence
        record = RootWriteRecord(
            sequence,
            root,
            expression_sha256,
            status,
            result_id,
            error,
        )
        if existing_index is None:
            self.write_journal.append(record)
        else:
            self.write_journal[existing_index] = record
        return record

    def _require_state(self, *states: OperationState) -> None:
        if self.state not in states:
            expected = ", ".join(state.value for state in states)
            raise ProtocolError(
                f"Runtime operation requires state {expected}; current state is "
                f"{self.state.value}"
            )

    @property
    def _allowed_locations(self) -> tuple[ModuleLocation, ...]:
        return self.registry.full_locations

    @staticmethod
    def _checked_modify(result: object, variable: str) -> None:
        if getattr(result, "error_occurred", False):
            raise ProtocolError(
                f"Failed to modify {variable}: {getattr(result, 'error_text', '')}"
            )

    def _set_workspace(
        self,
        phase: str,
        locations: tuple[ModuleLocation, ...],
    ) -> None:
        if not locations or locations[0] != self.service_location:
            raise ProtocolError("Breakpoint workspace must retain the service point")
        if phase == "capture-evaluation":
            captures = self.registry.captures
            ordinary_users = self.registry.users
            shielded = True
        else:
            ordinary_users = tuple(
                location
                for location in locations[1:]
                if location in self.registry.users
            )
            captures = tuple(
                location
                for location in locations[1:]
                if location not in ordinary_users
            )
            shielded = False
        try:
            confirmed = self.breakpoint_workspace_owner.confirmed_snapshot
            desired = self.breakpoint_workspace_owner.prepare(
                captures=captures,
                ordinary_users=ordinary_users,
                worker_slots=confirmed.worker_slots,
                shielded=shielded,
            )
            self.breakpoint_workspace_owner.install(desired)
        except BreakpointWorkspaceOutcomeUnknown:
            self.state = OperationState.RECOVERING
            raise
        self._breakpoint_workspace = desired.effective_locations
        self.breakpoint_workspaces.append(
            BreakpointWorkspaceEvent(phase, desired.effective_locations)
        )

    def install_worker_workspace(
        self,
        snapshot: WorkspaceSnapshot,
    ) -> WorkspaceInstallReceipt:
        try:
            receipt = self.breakpoint_workspace_owner.install(snapshot)
        except BreakpointWorkspaceOutcomeUnknown:
            self.state = OperationState.RECOVERING
            raise
        self._breakpoint_workspace = snapshot.effective_locations
        self.breakpoint_workspaces.append(
            BreakpointWorkspaceEvent("worker", snapshot.effective_locations)
        )
        return receipt

    def require_debug_workspace_ready(self) -> None:
        try:
            self.breakpoint_workspace_owner.require_confirmed()
        except BreakpointWorkspaceOutcomeUnknown:
            self.state = OperationState.RECOVERING
            raise

    def rearm_capture_successor(
        self, capture_points: tuple[ModuleLocation, ...]
    ) -> None:
        """Replace only capture breakpoints while the business frame is paused.

        The debugger registry and physical workspace are one transaction: a
        failed install restores the exact prior registry/workspace before the
        caller may decide that no Continue has been sent.
        """
        self._require_state(OperationState.CAPTURED)
        points = tuple(capture_points)
        previous_registry = self.registry
        previous_points = self.capture_points
        candidate = BreakpointRegistry(
            self.service_location, points, previous_registry.users
        )
        try:
            self._set_workspace("capture_successor", candidate.full_locations)
        except BaseException:
            try:
                self._set_workspace("capture_successor_restore", previous_registry.full_locations)
            except BaseException as rollback_error:
                self.state = OperationState.RECOVERING
                raise ProtocolError("capture successor breakpoint rollback is uncertain") from rollback_error
            raise
        self.registry = candidate
        self.capture_points = points

    def begin_continuation_admission(
        self,
        attempt: ContinuationAttemptSpec,
        capture_points: tuple[ModuleLocation, ...],
    ) -> _ControllerContinuationAdmission:
        """Begin one durable successor attempt before any root resolver runs."""
        self._require_state(OperationState.CAPTURED)
        if not isinstance(attempt, ContinuationAttemptSpec):
            raise TypeError("attempt must be a ContinuationAttemptSpec")
        if attempt.attempt_id in self._continuation_attempts:
            raise ProtocolError("continuation attempt_id was already used")
        snapshot = _ControllerContinuationAdmission(
            self,
            registry=self.registry,
            capture_points=self.capture_points,
            workspace=self._breakpoint_workspace,
            state=self.state,
            attempt_id=attempt.attempt_id,
        )
        state = _ContinuationAttemptState(
            attempt,
            {root: "unattempted" for root in attempt.dirty_roots},
        )
        self._continuation_attempts[attempt.attempt_id] = state
        self._active_continuation_attempt_id = attempt.attempt_id
        self._record(
            "write-journal.jsonl",
            "continuation_attempt_started",
            attempt_id=attempt.attempt_id,
            runtime_generation=self.runtime_generation,
            operation_id=self.active_operation.operation_id if self.active_operation else 0,
            capture_generation=attempt.capture_generation,
            request_operation_id=attempt.request_operation_id,
            dirty_roots=list(attempt.dirty_roots),
        )
        for root in attempt.dirty_roots:
            self._record(
                "write-journal.jsonl",
                "root_write_planned",
                attempt_id=attempt.attempt_id,
                runtime_generation=self.runtime_generation,
                operation_id=self.active_operation.operation_id if self.active_operation else 0,
                root=root,
            )
        try:
            self._flush_journal()
        except BaseException:
            snapshot.quarantine()
            raise
        points = tuple(capture_points)
        candidate = BreakpointRegistry(
            self.service_location, points, self.registry.users
        )
        try:
            self._set_workspace(
                "capture_successor", candidate.full_locations
            )
            self.registry = candidate
            self.capture_points = points
        except BaseException as error:
            if isinstance(error, RdbgTransportError):
                snapshot.quarantine()
                raise
            try:
                snapshot.rollback()
            except BaseException:
                pass
            raise
        return snapshot

    def continuation_attempt_evidence(
        self, attempt_id: str
    ) -> ContinuationAttemptEvidence:
        try:
            attempt = self._continuation_attempts[attempt_id]
        except KeyError as error:
            raise ProtocolError("continuation attempt is unknown") from error
        return ContinuationAttemptEvidence(
            tuple(
                (root, attempt.root_statuses[root])
                for root in attempt.spec.dirty_roots
            ),
            attempt.continue_state,
        )

    def _continuation_attempt(
        self,
        dirty_roots: tuple[str, ...],
        attempt_id: str | None,
    ) -> _ContinuationAttemptState:
        if attempt_id is None:
            spec = ContinuationAttemptSpec(
                f"internal-{uuid4().hex}",
                max(1, self.stop_sequence),
                f"controller-{self.operation_id}",
                dirty_roots,
            )
            state = _ContinuationAttemptState(
                spec, {root: "unattempted" for root in dirty_roots}
            )
            self._continuation_attempts[spec.attempt_id] = state
            self._active_continuation_attempt_id = spec.attempt_id
            self._record(
                "write-journal.jsonl",
                "continuation_attempt_started",
                attempt_id=spec.attempt_id,
                runtime_generation=self.runtime_generation,
                operation_id=self.operation_id,
                capture_generation=spec.capture_generation,
                request_operation_id=spec.request_operation_id,
                dirty_roots=list(dirty_roots),
            )
            for root in dirty_roots:
                self._record(
                    "write-journal.jsonl",
                    "root_write_planned",
                    attempt_id=spec.attempt_id,
                    runtime_generation=self.runtime_generation,
                    operation_id=self.operation_id,
                    root=root,
                )
            self._flush_journal()
            return state
        try:
            state = self._continuation_attempts[attempt_id]
        except KeyError as error:
            raise ProtocolError("continuation attempt is unknown") from error
        if state.spec.dirty_roots != dirty_roots:
            raise ProtocolError("continuation attempt dirty_roots changed")
        return state

    def _mark_continuation_root(
        self,
        attempt: _ContinuationAttemptState,
        root: str,
        status: str,
        *,
        error: str = "",
    ) -> None:
        attempt.root_statuses[root] = status
        if status in {"sent", "succeeded", "outcome_unknown"}:
            attempt.irreversible_roots.add(root)
        self._record(
            "write-journal.jsonl",
            f"root_write_{status}",
            attempt_id=attempt.spec.attempt_id,
            runtime_generation=self.runtime_generation,
            operation_id=self.operation_id,
            root=root,
            error=error,
        )
        self._flush_journal()

    def _mark_later_roots_unattempted(
        self, attempt: _ContinuationAttemptState, failed_root: str
    ) -> None:
        start = attempt.spec.dirty_roots.index(failed_root) + 1
        for root in attempt.spec.dirty_roots[start:]:
            if attempt.root_statuses[root] == "unattempted":
                self._record(
                    "write-journal.jsonl",
                    "root_write_unattempted",
                    attempt_id=attempt.spec.attempt_id,
                    runtime_generation=self.runtime_generation,
                    operation_id=self.operation_id,
                    root=root,
                )
        self._flush_journal()

    def execute_main(
        self,
        source: str,
        *,
        capture_points: tuple[ModuleLocation, ...] = (),
        user_breakpoints: tuple[ModuleLocation, ...] = (),
        on_transport_dispatch: Callable[[], None] | None = None,
    ) -> MainCompletion | CapturedStop | DebugStop:
        return self._execute_main(
            source,
            None,
            capture_points=capture_points,
            user_breakpoints=user_breakpoints,
            on_transport_dispatch=on_transport_dispatch,
        )

    def execute_lowered_main(
        self,
        visible_source: str,
        lowered_source: str,
        *,
        messages_intercepted: int = 0,
        message_collector_key: str = "",
        capture_points: tuple[ModuleLocation, ...] = (),
        user_breakpoints: tuple[ModuleLocation, ...] = (),
        on_transport_dispatch: Callable[[], None] | None = None,
    ) -> MainCompletion | CapturedStop | DebugStop:
        return self._execute_main(
            visible_source,
            lowered_source,
            messages_intercepted=messages_intercepted,
            message_collector_key=message_collector_key,
            capture_points=capture_points,
            user_breakpoints=user_breakpoints,
            on_transport_dispatch=on_transport_dispatch,
        )

    def execute_mapped_main(
        self,
        visible_source: str,
        lowered_source: MappedSource,
        *,
        visible_source_context: VisibleSourceContext,
        messages_intercepted: int = 0,
        message_collector_key: str = "",
        worker_messages: bool = False,
        worker_globals: tuple[str, ...] = (),
        capture_points: tuple[ModuleLocation, ...] = (),
        user_breakpoints: tuple[ModuleLocation, ...] = (),
        on_transport_dispatch: Callable[[], None] | None = None,
    ) -> MainCompletion | CapturedStop | DebugStop:
        return self._execute_main(
            visible_source,
            lowered_source,
            visible_source_context=visible_source_context,
            messages_intercepted=messages_intercepted,
            message_collector_key=message_collector_key,
            worker_messages=worker_messages,
            worker_globals=worker_globals,
            capture_points=capture_points,
            user_breakpoints=user_breakpoints,
            on_transport_dispatch=on_transport_dispatch,
        )

    def execute_system_main(self, source: str) -> MainCompletion:
        mapped = self._compatibility_mapped_source(source, "system-main")
        result = self._execute_main(
            source,
            mapped,
            visible_source_context=self._visible_context(mapped, source),
        )
        if not isinstance(result, MainCompletion):
            raise ProtocolError("System MAIN stopped outside the service boundary")
        return result

    def _execute_main(
        self,
        visible_source: str,
        lowered_source: str | MappedSource | None,
        *,
        visible_source_context: VisibleSourceContext | None = None,
        messages_intercepted: int | None = None,
        message_collector_key: str = "",
        worker_messages: bool = False,
        worker_globals: tuple[str, ...] = (),
        capture_points: tuple[ModuleLocation, ...] = (),
        user_breakpoints: tuple[ModuleLocation, ...] = (),
        on_transport_dispatch: Callable[[], None] | None = None,
    ) -> MainCompletion | CapturedStop | DebugStop:
        self._require_state(
            OperationState.IDLE,
            OperationState.COMPLETED,
            OperationState.FAILED,
        )
        if lowered_source is None:
            message_collector_key = self.message_collector_key(LoweringMode.MAIN)
            lowering = self.lowerer.lower(
                visible_source,
                mode=LoweringMode.MAIN,
                message_collector_key=message_collector_key,
            )
            mapped_lowered = lowering.mapped_source
            messages_intercepted = lowering.messages_intercepted
        elif isinstance(lowered_source, MappedSource):
            mapped_lowered = lowered_source
            if messages_intercepted is None:
                messages_intercepted = 0
        else:
            mapped_lowered = self._compatibility_mapped_source(
                lowered_source,
                "lowered-main",
            )
            if messages_intercepted is None:
                messages_intercepted = 0
        messages_intercepted = max(messages_intercepted, int(worker_messages))
        bound_source = self._with_worker_globals_mapped(
            mapped_lowered, worker_globals
        )
        wrapped_source = self._with_message_collector_mapped(
            bound_source,
            messages_intercepted,
            message_collector_key,
            worker_messages=worker_messages,
        )
        executed_source = self._as_executed_source(
            wrapped_source,
            mode=LoweringMode.MAIN,
        )
        lowered_text = executed_source.text
        if visible_source_context is None:
            visible_source_context = self._visible_context(
                executed_source,
                visible_source,
            )
        self.operation_id += 1
        operation = OperationHandle(
            self.operation_id,
            visible_source,
            lowered_text,
            messages_intercepted,
            message_collector_key,
            executed_source,
            visible_source_context,
        )
        self.active_operation = operation
        self._record(
            "write-journal.jsonl",
            "main_started",
            runtime_generation=self.runtime_generation,
            operation_id=operation.operation_id,
            state_before=self.state.value,
            visible_sha256=sha256(visible_source.encode("utf-8")).hexdigest(),
            lowered_sha256=executed_source.artifact.source_sha256,
        )
        self._flush_journal()
        try:
            self.capture_points = tuple(capture_points)
            self.registry = BreakpointRegistry(
                self.service_location,
                self.capture_points,
                tuple(user_breakpoints),
            )
            self.stop_sequence = 0
            self.last_capture_location = None
            self.recovery_checkpoint = None
            self.continue_sent = False
            self._generation_lost_notified = False
            self.write_journal.clear()
            self.stop_history.clear()
            self.last_debug_stop = None
            self.breakpoint_workspaces.clear()
            self._set_workspace("full", self._allowed_locations)
            instruction = self.session.modify(
                "ТекущаяИнструкция", bsl_string_literal(lowered_text)
            )
            self._checked_modify(instruction, "ТекущаяИнструкция")
            command = self.session.modify(
                "ИдентификаторКоманды", str(operation.operation_id)
            )
            self._checked_modify(command, "ИдентификаторКоманды")
            self.state = OperationState.MAIN_PENDING
            if on_transport_dispatch is not None:
                on_transport_dispatch()
            self.require_debug_workspace_ready()
            self.session.continue_()
            stop = self.session.wait_for_any_stop(timeout_s=self.command_timeout_s)
            return self._route_stop(stop)
        except BaseException as error:
            if self.state not in {
                OperationState.CAPTURED,
                OperationState.DEBUG_STOPPED,
                OperationState.PARTIAL_WRITEBACK_FAILURE,
                OperationState.BREAKPOINT_RESTORE_FAILURE,
                OperationState.RECOVERING,
            }:
                self.state = OperationState.FAILED
            failure_fields: dict[str, object] = {}
            if isinstance(error, CompletionDecodeError):
                failure_fields = {
                    "failure_phase": error.phase,
                    "evaluation_type": error.evaluation_type,
                    "exact_decimal_present": error.exact_decimal_present,
                }
            self._record(
                "write-journal.jsonl",
                "main_failed",
                runtime_generation=self.runtime_generation,
                operation_id=operation.operation_id,
                state_after=self.state.value,
                visible_sha256=sha256(visible_source.encode("utf-8")).hexdigest(),
                lowered_sha256=executed_source.artifact.source_sha256,
                error_type=type(error).__name__,
                **failure_fields,
            )
            self._flush_journal()
            raise

    def _route_stop(
        self,
        stop: StopEvent,
    ) -> MainCompletion | CapturedStop | DebugStop:
        classified = classify_stop(
            stop,
            self.registry,
            worker_locations=(
                self.breakpoint_workspace_owner.confirmed_snapshot.worker_slots
            ),
        )
        self.stop_history.append(classified)
        if classified.reason is StopReason.MAIN_SERVICE:
            return self._complete_main()
        if classified.reason is StopReason.CAPTURE:
            return self._begin_capture(stop)
        if self.active_operation is None:
            self.state = OperationState.FAILED
            raise ProtocolError("Debug stop has no active MAIN operation")
        debug_stop = DebugStop(self.active_operation, stop, classified.reason)
        self.last_debug_stop = debug_stop
        self.state = OperationState.DEBUG_STOPPED
        return debug_stop

    def resume_debug_stop(
        self,
        *,
        on_transport_dispatch: Callable[[], None] | None = None,
    ) -> MainCompletion | CapturedStop | CaptureCellResult | DebugStop:
        if self.state is OperationState.RECOVERING:
            owner = self._capture_evaluation_coordinator
            if owner is not None:
                status = owner.status(owner._fence)
                if status.phase is CapturePhase.RECOVERY_REQUIRED:
                    raise CaptureRecoveryRequiredError(status.failure)
        self._require_state(
            OperationState.DEBUG_STOPPED,
            OperationState.CAPTURE_DEBUG_STOPPED,
        )
        if (
            self.last_debug_stop is None
            or self.last_debug_stop.reason is not StopReason.USER_BREAKPOINT
        ):
            raise ProtocolError("Only an explicit user breakpoint can be resumed")
        if self.state is OperationState.CAPTURE_DEBUG_STOPPED:
            capture = self.pending_capture_evaluation
            if capture is None:
                raise ProtocolError("CAPTURE debug stop has no pending evaluation")
            if on_transport_dispatch is not None:
                on_transport_dispatch()
            self.session.continue_evaluation(
                capture.pending,
                self.last_debug_stop.stop,
            )
            self.state = OperationState.EVALUATING_CAPTURE
            event = self.session.wait_evaluation_event(
                capture.pending,
                timeout_s=self.command_timeout_s,
            )
            return self._handle_capture_evaluation_event(capture, event)
        self.state = OperationState.MAIN_PENDING
        if on_transport_dispatch is not None:
            on_transport_dispatch()
        self.require_debug_workspace_ready()
        self.session.continue_()
        stop = self.session.wait_for_any_stop(timeout_s=self.command_timeout_s)
        return self._route_stop(stop)

    def _begin_capture(self, stop: StopEvent) -> CapturedStop:
        if self.active_operation is None:
            self.state = OperationState.FAILED
            raise ProtocolError("Capture stop has no active MAIN operation")
        local_result = self.session.local_variables(stack_level=0)
        if local_result.error_occurred:
            self.state = OperationState.FAILED
            raise ProtocolError(local_result.error_text)
        transfer = self.session.evaluate(
            build_capture_transfer_call(
                variable.name for variable in local_result.variables
            )
        )
        if transfer.error_occurred:
            self.state = OperationState.FAILED
            raise BslExecutionError(transfer.error_text)
        address = evaluation_to_python(transfer)
        if not isinstance(address, str) or not address:
            self.state = OperationState.FAILED
            raise ProtocolError("Capture temporary-storage address is invalid")
        stack_level = self._locate_kernel_context_frame(stop)
        command_evidence = self.session.evaluate(
            "ИдентификаторКоманды", stack_level=stack_level
        )
        if command_evidence.error_occurred:
            self.state = OperationState.FAILED
            raise BslExecutionError(command_evidence.error_text)
        observed_command_id = self._decode_main_completion_value(
            command_evidence,
            phase="capture_command",
        )
        if observed_command_id != self.active_operation.operation_id:
            self.state = OperationState.FAILED
            raise ProtocolError(
                f"Captured command {observed_command_id!r} does not match active "
                f"operation {self.active_operation.operation_id}"
            )
        begin = self.session.evaluate(
            build_live_capture_begin_call(address),
            stack_level=stack_level,
        )
        if begin.error_occurred:
            self.state = OperationState.FAILED
            raise BslExecutionError(begin.error_text)
        self.capture_kernel_stack_level = stack_level
        # RDBG level zero is the exact suspended business frame used to build
        # the capture context. The default metadata index reuses these locals;
        # other frames and named value details are read only on explicit request.
        self.capture_frame_stack_level = 0
        self._capture_frame_variables = tuple(local_result.variables)
        self._capture_stack_frames = tuple(stop.stack_frames)
        self._capture_target_id = stop.target_id
        self._capture_manager_paths.clear()
        self._capture_value_paths.clear()
        self._capture_metadata_handles.clear()
        self.stop_sequence += 1
        self.last_capture_location = stop.location
        self.state = OperationState.CAPTURED
        self._replace_capture_evaluation_coordinator()
        captured = CapturedStop(
            self.active_operation,
            stop.location,
            self.stop_sequence,
            local_result.variables,
            observed_command_id,
        )
        self._inject(
            FaultPoint.AFTER_CAPTURE_CHECKPOINT,
            RecoveryPhase.CAPTURED,
        )
        return captured

    def _locate_kernel_context_frame(self, stop: StopEvent) -> int:
        required = {
            "контекст",
            "текущаяинструкция",
            "идентификаторкоманды",
        }
        stack = stop.stack
        frames = stop.stack_frames
        if not stack or not frames:
            self.state = OperationState.FAILED
            raise ProtocolError("Capture stop has no exact stack mapping for runtime kernel")
        if (
            len(frames) != len(stack)
            or tuple(frame.location for frame in frames) != stack
            or any(frame.level < 0 for frame in frames)
            or len({frame.level for frame in frames}) != len(frames)
        ):
            self.state = OperationState.FAILED
            raise ProtocolError("Capture stop stack mapping is incoherent")
        for frame in frames:
            stack_level = frame.level
            if stack_level <= 0 or stack_level >= 8:
                continue
            frame_location = frame.location
            if not self._same_kernel_module(frame_location):
                continue
            local_result = self.session.local_variables(stack_level=stack_level)
            if local_result.error_occurred:
                continue
            names = {variable.name.casefold() for variable in local_result.variables}
            if not required.issubset(names):
                continue
            return stack_level
        self.state = OperationState.FAILED
        raise ProtocolError("Runtime kernel context frame was not found")

    def _same_kernel_module(self, location: ModuleLocation) -> bool:
        kernel = self.kernel_location
        return (
            location.module_type == kernel.module_type
            and location.url == kernel.url
            and location.object_id == kernel.object_id
            and location.property_id == kernel.property_id
            and location.extension_name == kernel.extension_name
            and location.ext_id == kernel.ext_id
        )

    def _required_capture_kernel_stack_level(self) -> int:
        if self.capture_kernel_stack_level is None:
            raise ProtocolError("Capture kernel context frame is not initialized")
        return self.capture_kernel_stack_level

    def execute_capture(
        self,
        source: str,
        *,
        on_transport_dispatch: Callable[[], None] | None = None,
    ) -> CaptureCellResult | DebugStop:
        self._require_capture_evaluation_admission()
        message_collector_key = self.message_collector_key(LoweringMode.CAPTURE)
        lowering = self.lowerer.lower(
            source,
            mode=LoweringMode.CAPTURE,
            message_collector_key=message_collector_key,
        )
        return self._execute_capture(
            source,
            lowering.mapped_source,
            visible_source_context=self._visible_context(
                lowering.mapped_source,
                source,
            ),
            messages_intercepted=lowering.messages_intercepted,
            message_collector_key=message_collector_key,
            dirty_roots=lowering.dirty_roots,
            on_transport_dispatch=on_transport_dispatch,
        )

    def execute_lowered_capture(
        self,
        visible_source: str,
        lowered_source: str,
        *,
        messages_intercepted: int = 0,
        message_collector_key: str = "",
        dirty_roots: tuple[str, ...] = (),
        on_transport_dispatch: Callable[[], None] | None = None,
    ) -> CaptureCellResult | DebugStop:
        return self._execute_capture(
            visible_source,
            self._compatibility_mapped_source(
                lowered_source,
                "lowered-capture",
            ),
            messages_intercepted=messages_intercepted,
            message_collector_key=message_collector_key,
            dirty_roots=dirty_roots,
            on_transport_dispatch=on_transport_dispatch,
        )

    def execute_mapped_capture(
        self,
        visible_source: str,
        lowered_source: MappedSource,
        *,
        visible_source_context: VisibleSourceContext,
        messages_intercepted: int = 0,
        message_collector_key: str = "",
        worker_messages: bool = False,
        worker_globals: tuple[str, ...] = (),
        dirty_roots: tuple[str, ...] = (),
        on_transport_dispatch: Callable[[], None] | None = None,
    ) -> CaptureCellResult | DebugStop:
        return self._execute_capture(
            visible_source,
            lowered_source,
            visible_source_context=visible_source_context,
            messages_intercepted=messages_intercepted,
            message_collector_key=message_collector_key,
            worker_messages=worker_messages,
            worker_globals=worker_globals,
            dirty_roots=dirty_roots,
            on_transport_dispatch=on_transport_dispatch,
        )

    def capture_frame_variables(
        self, *, filters: Mapping[str, object], cursor: int, limit: int,
        timeout_s: float | None = None,
    ) -> Mapping[str, object]:
        """Return a page of captured-frame metadata without reading values."""
        deadline = self._capture_command_deadline(timeout_s)
        self._require_capture_frame()
        if not isinstance(filters, Mapping) or set(filters) - {"name", "role", "type"}:
            raise ProtocolError("capture frame filters are invalid")
        if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ProtocolError("capture frame page is invalid")
        expected = {
            key: value.casefold()
            for key, value in filters.items()
            if isinstance(value, str) and value
        }
        if len(expected) != len(filters):
            raise ProtocolError("capture frame filters must be non-empty strings")
        items = tuple(
            variable
            for variable in self._capture_frame_variables
            if (
                ("name" not in expected or expected["name"] in variable.name.casefold())
                and ("type" not in expected or expected["type"] in variable.type_name.casefold())
                and ("role" not in expected or expected["role"] == "local")
            )
        )
        if cursor > len(items):
            raise ProtocolError("capture frame cursor exceeds metadata")
        self._capture_remaining_timeout(deadline)
        page = items[cursor : cursor + limit]
        next_cursor = cursor + len(page)
        return {
            "items": tuple(
                {
                    "name": variable.name,
                    "type_name": variable.type_name,
                    "role": "local",
                    # Registry-private path into the captured Context structure.
                    "handle": "Контекст.КонтекстОтладки." + variable.name,
                }
                for variable in page
            ),
            "total": len(items),
            "next_cursor": next_cursor if next_cursor < len(items) else None,
        }

    def _capture_stack_frame_wire(self, frame: StackFrame) -> Mapping[str, object]:
        location = frame.location
        if self._same_kernel_module(location):
            # The caller needs the stack level, not the extension's private
            # RDBG URL, source line, or module identity.
            return {
                "level": frame.level,
                "runtime_kernel": True,
                "module_type": None,
                "object_id": None,
                "property_id": None,
                "line": None,
                "extension_name": None,
            }
        return {
            "level": frame.level,
            "runtime_kernel": False,
            "module_type": location.module_type,
            "object_id": str(location.object_id) if location.object_id is not None else None,
            "property_id": str(location.property_id) if location.property_id is not None else None,
            "line": location.line,
            "extension_name": location.extension_name,
        }

    def _require_capture_stack(self) -> tuple[StackFrame, ...]:
        self._require_capture_frame()
        if not self._capture_stack_frames or self._capture_target_id is None:
            raise ProtocolError("capture stack is unavailable")
        target = self.session.target
        if target is None or target.target_id != self._capture_target_id:
            raise ProtocolError("capture target has changed")
        return self._capture_stack_frames

    def capture_stack_inventory(
        self, *, timeout_s: float | None = None,
    ) -> tuple[StackFrame, ...]:
        """Read the debugger's current native stack for the still-fenced stop."""
        deadline = self._capture_command_deadline(timeout_s)
        saved = self._require_capture_stack()
        expected_target = self._capture_target_id
        read = getattr(self.session, "read_current_stack", None)
        if not callable(read):
            raise ProtocolError("RDBG session cannot read the current stack")
        stop = read(timeout_s=self._capture_remaining_timeout(deadline))
        if not isinstance(stop, StopEvent) or stop.target_id != expected_target:
            raise ProtocolError("fresh capture stack target has changed")
        frames = tuple(stop.stack_frames)
        levels = tuple(frame.level for frame in frames)
        if (
            not frames
            or any(type(frame) is not StackFrame for frame in frames)
            or any(frame.target_id != expected_target for frame in frames)
            or len(set(levels)) != len(levels)
            or levels != tuple(sorted(levels))
            or levels[0] != 0
            or tuple(frame.location for frame in frames) != stop.stack
        ):
            raise ProtocolError("fresh capture stack mapping is incoherent")
        if frames[0].location != saved[0].location:
            raise ProtocolError("fresh capture stack no longer names the captured stop")
        self._capture_remaining_timeout(deadline)
        return frames

    def capture_stack(
        self, *, cursor: int, limit: int, timeout_s: float | None = None,
    ) -> Mapping[str, object]:
        """Page already-observed stack locations only when requested."""
        deadline = self._capture_command_deadline(timeout_s)
        if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ProtocolError("capture stack page is invalid")
        frames = self._require_capture_stack()
        if cursor > len(frames):
            raise ProtocolError("capture stack cursor exceeds frames")
        self._capture_remaining_timeout(deadline)
        page = frames[cursor : cursor + limit]
        next_cursor = cursor + len(page)
        return {
            "frames": tuple(self._capture_stack_frame_wire(frame) for frame in page),
            "total": len(frames),
            "next_cursor": next_cursor if next_cursor < len(frames) else None,
        }

    def capture_frame(
        self, *, level: int, cursor: int, limit: int,
        name: str | None = None, timeout_s: float | None = None,
    ) -> Mapping[str, object]:
        """Read one frame on demand; evaluate only an explicitly named local."""
        deadline = self._capture_command_deadline(timeout_s)
        if type(level) is not int or level < 0:
            raise ProtocolError("capture frame level is invalid")
        if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ProtocolError("capture frame page is invalid")
        if name is not None:
            self._capture_identifier(name, name="variable name")
            if cursor != 0:
                raise ProtocolError("named capture variable cursor must be zero")
        frames = self._require_capture_stack()
        frame = next((item for item in frames if item.level == level), None)
        if frame is None:
            raise ProtocolError("capture frame level is unavailable")
        if self._same_kernel_module(frame.location):
            raise ProtocolError("runtime kernel frame variables are unavailable")
        if level == self.capture_frame_stack_level and name is None:
            variables = self._capture_frame_variables
        else:
            result = self.session.local_variables(
                stack_level=level,
                timeout_s=self._capture_remaining_timeout(deadline),
                max_text_size=512,
            )
            if result.error_occurred:
                raise ProtocolError("capture frame variables are unavailable")
            variables = result.variables
        if name is not None:
            variables = tuple(item for item in variables if item.name.casefold() == name.casefold())
            if not variables:
                raise ProtocolError("capture variable is unavailable")
        if cursor > len(variables):
            raise ProtocolError("capture frame cursor exceeds variables")
        self._capture_remaining_timeout(deadline)
        page = variables[cursor : cursor + limit]
        if name is None:
            items: tuple[Mapping[str, object], ...] = tuple(
                {"name": item.name, "type_name": item.type_name} for item in page
            )
        else:
            item = page[0]
            items = ({
                "name": item.name,
                "type_name": item.type_name,
                "presentation": item.presentation[:512],
                "collection_size": item.collection_size,
            },)
        next_cursor = cursor + len(page)
        return {
            "frame": self._capture_stack_frame_wire(frame),
            "variables": items,
            "total": len(variables),
            "next_cursor": next_cursor if next_cursor < len(variables) else None,
        }

    def capture_value_inspection(
        self,
        action: str,
        *,
        path: SafeValuePath | None,
        request: ValueInspectionRequest | None,
        limit: int | None,
        worker_type_registrations: tuple[str, ...],
        context_generation: int = 1,
        timeout_s: float | None = None,
    ) -> object:
        """Submit one qualified capture-value projection through the coordinator.

        A value descriptor carries only a frozen symbolic path.  The normal
        path builds a protocol-2 envelope backed by the checked-in extension
        helper.  Test-only injected builders remain a narrow decoder seam;
        there is no inventory-to-public-value fallback.
        """
        deadline = self._capture_command_deadline(timeout_s)
        # This deliberately precedes request/path validation.  A busy, stale
        # or recovery state wins over malformed user input and no target-side
        # privacy/admission work is reached in that state.
        self._require_capture_frame()
        self._require_capture_value_stop_fence()
        try:
            selected_path, stack_level = self._capture_value_request_target(
                action,
                path=path,
                request=request,
                limit=limit,
            )
            if (
                type(worker_type_registrations) is not tuple
                or any(
                    not isinstance(registration, str)
                    or not registration
                    or len(registration) > 4096
                    for registration in worker_type_registrations
                )
            ):
                raise CaptureValueCheckError(
                    "capture value Worker registrations are invalid"
                )
            if type(context_generation) is not int or context_generation < 1:
                raise CaptureValueCheckError("capture value context generation is invalid")
        except (
            CaptureBusyError,
            CaptureEvaluationPendingError,
            CaptureOutcomeUnknownError,
            CaptureRecoveryRequiredError,
            CaptureSourceUnavailableError,
            CaptureValueAccessDeniedError,
            CaptureValueCheckError,
            StaleCaptureError,
        ):
            raise
        except Exception:
            raise CaptureValueCheckError(
                "capture value inspection request is invalid"
            ) from None

        try:
            builder = self._capture_value_inspection_builder
            if builder is None:
                plan = self._build_capture_value_inspection_plan(
                    action=action,
                    path=selected_path,
                    request=request,
                    limit=limit,
                    worker_type_registrations=worker_type_registrations,
                    context_generation=context_generation,
                    deadline=deadline,
                )
            else:
                plan = builder(
                    action=action,
                    path=selected_path,
                    request=request,
                    limit=limit,
                    worker_type_registrations=worker_type_registrations,
                )
        except (
            CaptureBusyError,
            CaptureEvaluationPendingError,
            CaptureOutcomeUnknownError,
            CaptureRecoveryRequiredError,
        ):
            # A builder runs before it has submitted a coordinator record.
            # Its fabricated lifecycle result cannot identify a public pending
            # evaluation, so never leak it to descriptor callers.
            self._require_capture_value_stop_fence()
            raise CaptureValueCheckError(
                "capture value target projection is unavailable"
            ) from None
        except (
            CaptureSourceUnavailableError,
            CaptureValueAccessDeniedError,
            CaptureValueCheckError,
            StaleCaptureError,
        ):
            raise
        except Exception:
            raise CaptureValueCheckError(
                "capture value target projection is unavailable"
            ) from None
        if not isinstance(plan, CaptureValueInspectionPlan):
            raise CaptureValueCheckError("capture value target plan is invalid")
        # A builder may have needed a native candidate-name read.  Do not send
        # its source after that pre-submit work has lost this saved stop.
        self._require_capture_value_stop_fence()

        def decode_value(value: object) -> object:
            try:
                # A target response can race a new stop.  Detect that fence
                # change before touching even the private lazy metadata
                # readers returned by the trusted decoder.
                self._require_capture_value_stop_fence()
            except (
                CaptureBusyError,
                CaptureEvaluationPendingError,
                CaptureOutcomeUnknownError,
                CaptureRecoveryRequiredError,
                CaptureSourceUnavailableError,
                CaptureValueAccessDeniedError,
                CaptureValueCheckError,
                StaleCaptureError,
            ):
                raise
            except Exception:
                raise CaptureValueCheckError(
                    "capture value target projection is invalid"
                ) from None
            if action == "resolve":
                return _seal_capture_projected_value(value)
            if action == "project":
                if not isinstance(value, PrivateValueProjection):
                    raise CaptureValueCheckError(
                        "capture value target projection is invalid"
                    )
                return PrivateValueProjection(
                    tuple(_seal_capture_projected_value(item) for item in value.entries),
                    value.total,
                    value.next_cursor,
                )
            if type(value) is not tuple or any(
                not isinstance(name, str) for name in value
            ):
                raise CaptureValueCheckError("capture value target schema is invalid")
            return value

        def decode(result: EvaluationResult) -> object:
            if result.error_occurred:
                raise CaptureValueCheckError(
                    "capture value target projection failed"
                )
            return decode_value(plan.decode(result))

        try:
            if plan.envelope is None:
                result = self._evaluate_capture_helper(
                    plan.source,
                    evaluation_kind=CaptureEvaluationKind.INSPECTION,
                    stack_level=stack_level,
                    result_policy=decode,
                    timeout_s=self._capture_remaining_timeout(deadline),
                )
            else:
                result = self._evaluate_capture_value_envelope(
                    plan.envelope,
                    stack_level=stack_level,
                    result_decoder=decode_value,
                    timeout_s=self._capture_remaining_timeout(deadline),
                )
            self._capture_remaining_timeout(deadline)
            return result
        except (
            CaptureBusyError,
            CaptureEvaluationPendingError,
            CaptureOutcomeUnknownError,
            CaptureRecoveryRequiredError,
            CaptureSourceUnavailableError,
            CaptureValueAccessDeniedError,
            CaptureValueCheckError,
            StaleCaptureError,
        ):
            raise
        except Exception:
            # Evaluation source and RDBG result diagnostics may contain target
            # paths or value presentations.  The RuntimeApi sees one bounded
            # typed failure, then rechecks the lifecycle fence before exposing
            # it to the descriptor caller.
            raise CaptureValueCheckError(
                "capture value target projection failed"
            ) from None

    def _build_capture_value_inspection_plan(
        self,
        *,
        action: str,
        path: SafeValuePath,
        request: ValueInspectionRequest | None,
        limit: int | None,
        worker_type_registrations: tuple[str, ...],
        context_generation: int,
        deadline: float,
    ) -> CaptureValueInspectionPlan:
        """Create the only production target plan after complete local grammar."""
        native_selection = (
            _NativeCaptureCandidateSelection(())
            if path.root.kind is ValueRootKind.CONTEXT
            else self._capture_value_native_candidates(
                action=action,
                path=path,
                request=request,
                deadline=deadline,
            )
        )
        envelope = build_capture_value_inspection_envelope(
            action=action,
            path=path,
            request=request,
            limit=limit,
            runtime_generation=self.runtime_generation,
            context_generation=context_generation,
            worker_type_registrations=worker_type_registrations,
            native_candidates=native_selection.candidates,
            native_page=native_selection.page,
            policy=CaptureValuePolicy(),
        )
        return CaptureValueInspectionPlan(envelope.source, lambda _result: None, envelope)

    def _capture_value_native_candidates(
        self,
        *,
        action: str,
        path: SafeValuePath,
        request: ValueInspectionRequest | None,
        deadline: float,
    ) -> _NativeCaptureCandidateSelection:
        """Compact one private RDBG inventory before it can form BSL source."""
        names = self._capture_value_native_inventory(path, deadline=deadline)
        if action == "project" and not path.segments:
            if not isinstance(request, ValueInspectionRequest):
                raise CaptureValueCheckError("capture native frame page is invalid")
            return self._capture_value_native_root_page(names, request)
        selector = path.segments[0] if path.segments else None
        canonical = (
            None
            if (
                selector is None
                or selector.kind is not ValuePathSegmentKind.VARIABLE
                or not isinstance(selector.key, str)
            )
            else next(
                (
                    name for name in names
                    if name.casefold() == selector.key.casefold()
                ),
                None,
            )
        )
        if canonical is None:
            raise CaptureValueCheckError(
                "capture native frame root is unavailable"
            )
        return _NativeCaptureCandidateSelection((canonical,))

    def _capture_value_native_inventory(
        self,
        path: SafeValuePath,
        *,
        deadline: float,
    ) -> tuple[str, ...]:
        """Read only bounded safe candidate names from one private RDBG reply."""
        root = path.root
        if root.kind is not ValueRootKind.FRAME or type(root.native_level) is not int:
            raise CaptureValueCheckError("capture native frame candidates are invalid")
        if root.native_level == self.capture_frame_stack_level:
            variables = self._capture_frame_variables
        else:
            result = self.session.local_variables(stack_level=root.native_level)
            if result.error_occurred:
                raise CaptureSourceUnavailableError(
                    "native frame value candidates are unavailable"
                )
            variables = result.variables
        invalid_inventory = (
            type(variables) is not tuple
            or len(variables) > MAX_CAPTURE_VALUE_NATIVE_INVENTORY
            or any(not isinstance(item, FrameVariable) for item in variables)
        )
        names: tuple[str, ...] = ()
        if not invalid_inventory:
            try:
                names = tuple(
                    SafePathSegment(ValuePathSegmentKind.VARIABLE, item.name).key
                    for item in variables
                )
            except Exception:
                invalid_inventory = True
        if invalid_inventory:
            raise CaptureSourceUnavailableError(
                "native frame value candidates are unavailable"
            )
        if len({name.casefold() for name in names}) != len(names):
            raise CaptureSourceUnavailableError(
                "native frame value candidates are unavailable"
            )
        self._capture_remaining_timeout(deadline)
        return names

    @staticmethod
    def _capture_value_native_root_page(
        names: tuple[str, ...],
        request: ValueInspectionRequest,
    ) -> _NativeCaptureCandidateSelection:
        """Apply root role, exact and slice semantics before target admission."""
        by_folded_name = {name.casefold(): name for name in names}
        if request.role is VariableRole.PARAMETERS:
            candidates, total = (
                PrototypeRuntimeController._capture_value_native_parameter_page(
                    by_folded_name, request,
                )
            )
        else:
            inventory = names
            if request.role is VariableRole.LOCALS:
                parameters = {
                    name.casefold() for name in request.parameter_names
                }
                inventory = tuple(
                    name for name in names if name.casefold() not in parameters
                )
            if request.exact is not None:
                candidates = tuple(
                    name for name in inventory
                    if name.casefold() == cast(str, request.exact).casefold()
                )
                total = len(candidates)
            else:
                candidates = inventory[request.start:request.stop]
                total = len(inventory)
        if len(candidates) > MAX_CAPTURE_VALUE_NATIVE_CANDIDATES:
            raise CaptureValueCheckError("capture native frame page is invalid")
        next_cursor = request.stop if request.exact is None and request.stop < total else None
        source_request = ValueInspectionRequest(
            request.path,
            request.view,
            0,
            len(candidates),
            request.role,
            candidates if request.role is VariableRole.PARAMETERS else (),
            request.exact,
        )
        return _NativeCaptureCandidateSelection(
            candidates,
            NativeCandidatePage(source_request, candidates, total, next_cursor),
        )

    @staticmethod
    def _capture_value_native_parameter_page(
        by_folded_name: Mapping[str, str],
        request: ValueInspectionRequest,
    ) -> tuple[tuple[str, ...], int]:
        """Retain parameters in source order without exposing the inventory."""
        if request.exact is not None:
            candidate = by_folded_name.get(cast(str, request.exact).casefold())
            if candidate is None or all(
                candidate.casefold() != name.casefold()
                for name in request.parameter_names
            ):
                return (), 0
            return (candidate,), 1
        selected = request.parameter_names[request.start:request.stop]
        return (
            tuple(
                by_folded_name[name.casefold()]
                for name in selected
                if name.casefold() in by_folded_name
            ),
            len(request.parameter_names),
        )

    def _evaluate_capture_value_envelope(
        self,
        envelope: CaptureValueInspectionEnvelope,
        *,
        stack_level: int,
        result_decoder: Callable[[object], object],
        timeout_s: float,
    ) -> object:
        """Run admission, private read and cleanup in one INSPECTION record."""
        self._require_capture_evaluation_admission()
        owner = self._capture_evaluation_owner()
        policy_failure: BaseException | None = None

        def note_policy_failure(error: BaseException) -> None:
            nonlocal policy_failure
            if isinstance(
                error,
                (
                    BslExecutionError,
                    ProtocolError,
                    CaptureSourceUnavailableError,
                    CaptureValueAccessDeniedError,
                    CaptureValueCheckError,
                    StaleCaptureError,
                ),
            ):
                policy_failure = error

        def parse_initial(result: EvaluationResult) -> AdmissionEnvelopeV1:
            if result.error_occurred:
                error = CaptureValueCheckError(
                    "capture value target projection failed"
                )
                note_policy_failure(error)
                raise error
            try:
                return envelope.parse_metadata(evaluation_to_python(result))
            except BaseException as error:
                note_policy_failure(error)
                raise

        def read_payload(context: CaptureStepContext) -> str:
            source = (
                "RuntimeKernelServer.ЗабратьКомпактнуюМатериализациюИзКонтекста("
                "RuntimeContextStoreServer.ПолучитьКонтекст(), "
                + bsl_string_literal(envelope.private_key)
                + ")"
            )
            response = context.execute_inline(self._capture_remote_step(
                source,
                stack_level=stack_level,
                max_text_size=envelope.max_text_size,
                timeout_s=timeout_s,
            ))
            if response.error_occurred:
                raise CaptureValueCheckError("capture value payload is unavailable")
            try:
                content = evaluation_to_python(response)
            except Exception:
                raise CaptureValueCheckError("capture value payload is invalid") from None
            if not isinstance(content, str) or len(content) > envelope.max_text_size:
                raise CaptureValueCheckError("capture value payload is invalid")
            return content

        def continue_envelope(
            context: CaptureStepContext,
            metadata: AdmissionEnvelopeV1,
        ) -> object:
            try:
                value = envelope.decode(metadata, read_payload(context))
                self._require_capture_value_stop_fence()
                return result_decoder(value)
            except BaseException as error:
                note_policy_failure(error)
                raise

        first = self._capture_remote_step(
            envelope.source,
            stack_level=stack_level,
            max_text_size=4096,
            timeout_s=timeout_s,
        )
        cleanup = CaptureCleanupLease(
            envelope.private_key,
            self._capture_remote_step(
                envelope.cleanup_source,
                stack_level=stack_level,
                max_text_size=4096,
                timeout_s=timeout_s,
            ),
        )
        request = CaptureEvaluationRequest(
            owner._fence,
            CaptureEvaluationKind.INSPECTION,
            first.dispatch,
            first.poll,
            parse_initial,
            restore=first.restore,
            cleanup_leases=(cleanup,),
            step_continuation=continue_envelope,
            completion=self._complete_capture_lifecycle,
        )
        try:
            return self._submit_capture_request(
                request,
                timeout_s=timeout_s,
                helper_handoff=True,
            )
        except (BslExecutionError, CaptureEvaluationDeliveryError):
            if policy_failure is not None:
                # A policy failure is private coordinator work.  Before it
                # becomes a descriptor error, let a changed stop fence win.
                self._require_capture_value_stop_fence()
                raise policy_failure from None
            raise

    def _require_capture_value_stop_fence(self) -> None:
        """Validate the controller-side portion of the saved CAPTURE fence.

        RuntimeApi checks the complete public fence before and after every
        operation.  The result policy runs inside the coordinator, however,
        so it also needs this small check before it materializes a private
        result record.  It deliberately does not inspect coordinator phase:
        the policy itself executes while that phase is ``evaluating``.
        """
        fence = self._capture_evaluation_owner()._fence
        if (
            fence.operation_id != self.operation_id
            or fence.capture_generation != self.runtime_generation
            or fence.stop_sequence != self.stop_sequence
        ):
            raise StaleCaptureError()

    def _capture_value_request_target(
        self,
        action: object,
        *,
        path: SafeValuePath | None,
        request: ValueInspectionRequest | None,
        limit: int | None,
    ) -> tuple[SafeValuePath, int]:
        """Validate the closed runtime request before its builder sees it."""
        if action not in {"resolve", "project", "columns"}:
            raise CaptureValueCheckError("capture value operation is invalid")
        if action == "project":
            if (
                path is not None
                or limit is not None
                or not isinstance(request, ValueInspectionRequest)
            ):
                raise CaptureValueCheckError("capture value projection request is invalid")
            selected_path = request.path
            if (
                not isinstance(request.view, ValueViewKind)
                or not isinstance(request.role, VariableRole)
                or type(request.start) is not int
                or type(request.stop) is not int
                or request.start < 0
                or request.stop < request.start
                or request.start > MAX_CAPTURE_PROJECTION_POSITION
                or request.stop > MAX_CAPTURE_PROJECTION_POSITION
                or request.stop - request.start > 100
                or type(request.parameter_names) is not tuple
                or any(not isinstance(name, str) for name in request.parameter_names)
            ):
                raise CaptureValueCheckError("capture value projection request is invalid")
            self._validate_capture_value_projection_grammar(request)
        elif action == "resolve":
            if (
                request is not None
                or limit is not None
                or not isinstance(path, SafeValuePath)
            ):
                raise CaptureValueCheckError("capture value root request is invalid")
            selected_path = path
        else:
            if request is not None or not isinstance(path, SafeValuePath):
                raise CaptureValueCheckError("capture value schema request is invalid")
            if type(limit) is not int or not 1 <= limit <= MAX_CAPTURE_SCHEMA_COLUMNS + 1:
                raise CaptureValueCheckError("capture value schema request is invalid")
            selected_path = path

        if not isinstance(selected_path, SafeValuePath):
            raise CaptureValueCheckError("capture value path is invalid")
        self._validate_capture_value_path(action, selected_path)
        root = selected_path.root
        if root.kind is ValueRootKind.CONTEXT:
            return selected_path, self._required_capture_kernel_stack_level()
        if root.kind is not ValueRootKind.FRAME or type(root.native_level) is not int:
            raise CaptureValueCheckError("capture value path root is invalid")
        frames = self._require_capture_stack()
        frame = next((item for item in frames if item.level == root.native_level), None)
        if frame is None or self._same_kernel_module(frame.location):
            raise CaptureSourceUnavailableError(
                "native frame value inspection is unavailable"
            )
        return selected_path, root.native_level

    @staticmethod
    def _validate_capture_value_path(
        action: str,
        path: SafeValuePath,
    ) -> None:
        """Reject paths that no public value descriptor can construct.

        The frozen path types prevent expression injection.  This second gate
        prevents a direct controller caller from using their general-purpose
        constructors to skip the root variable or the public depth budget
        before a qualified target builder sees the request.
        """
        segments = path.segments
        if not segments:
            if action != "project":
                raise CaptureValueCheckError("capture value root request is invalid")
            return
        if (
            len(segments) > MAX_CAPTURE_VALUE_PATH_SEGMENTS
            or segments[0].kind is not ValuePathSegmentKind.VARIABLE
            or any(
                segment.kind in {
                    ValuePathSegmentKind.VARIABLE,
                    ValuePathSegmentKind.COLUMN,
                }
                for segment in segments[1:]
            )
        ):
            raise CaptureValueCheckError("capture value path is invalid")
        previous = segments[0]
        for segment in segments[1:]:
            # A public row descriptor may expose only named fields.  Column
            # nodes are metadata leaves and cannot form a target value path.
            if (
                previous.kind is ValuePathSegmentKind.ROW
                and segment.kind is not ValuePathSegmentKind.FIELD
            ):
                raise CaptureValueCheckError("capture value path is invalid")
            previous = segment

    @classmethod
    def _validate_capture_value_projection_grammar(
        cls,
        request: ValueInspectionRequest,
    ) -> None:
        """Validate every locally constructible project route before planning.

        ``SafeValuePath`` proves that individual components are expression
        safe.  It intentionally permits more combinations than public value
        descriptors can create, so this gate also closes root/view/role/exact
        combinations for direct controller users.
        """
        path = request.path
        if not isinstance(path, SafeValuePath):
            raise CaptureValueCheckError("capture value projection path is invalid")
        if not isinstance(request.view, ValueViewKind) or not isinstance(
            request.role, VariableRole,
        ):
            raise CaptureValueCheckError("capture value projection request is invalid")
        if type(request.parameter_names) is not tuple:
            raise CaptureValueCheckError("capture value projection request is invalid")
        try:
            parameters = tuple(
                SafePathSegment(ValuePathSegmentKind.VARIABLE, name).key
                for name in request.parameter_names
            )
        except Exception:
            raise CaptureValueCheckError("capture value projection request is invalid") from None
        if len({name.casefold() for name in parameters}) != len(parameters):
            raise CaptureValueCheckError("capture value parameter names are ambiguous")

        if not path.segments:
            if request.view is not ValueViewKind.VARIABLES:
                raise CaptureValueCheckError("capture value root view is invalid")
            if request.exact is not None:
                try:
                    SafePathSegment(ValuePathSegmentKind.VARIABLE, request.exact)
                except Exception:
                    raise CaptureValueCheckError(
                        "capture value root exact selector is invalid"
                    ) from None
            if request.role is VariableRole.VARIABLES and parameters:
                raise CaptureValueCheckError(
                    "capture value variable role cannot have parameter names"
                )
            return

        if (
            request.view is ValueViewKind.VARIABLES
            or request.role is not VariableRole.VARIABLES
            or parameters
        ):
            raise CaptureValueCheckError("capture value descendant view is invalid")
        named_view = request.view in {
            ValueViewKind.STRUCTURE_FIELDS,
            ValueViewKind.TABLE_COLUMNS,
            ValueViewKind.ROW_FIELDS,
        }
        indexed_view = request.view in {
            ValueViewKind.ARRAY_ITEMS,
            ValueViewKind.TABLE_ROWS,
        }
        if not (named_view or indexed_view):
            raise CaptureValueCheckError("capture value descendant view is invalid")
        terminal = path.segments[-1]
        if terminal.kind is ValuePathSegmentKind.ROW:
            if request.view is not ValueViewKind.ROW_FIELDS:
                raise CaptureValueCheckError("capture value terminal view is invalid")
        elif request.view is ValueViewKind.ROW_FIELDS:
            raise CaptureValueCheckError("capture value terminal view is invalid")
        if request.exact is None:
            return
        try:
            if named_view:
                SafePathSegment(ValuePathSegmentKind.FIELD, request.exact)
            elif type(request.exact) is not int or request.exact < 0:
                raise ValueError("indexed selector is invalid")
        except Exception:
            raise CaptureValueCheckError(
                "capture value descendant exact selector is invalid"
            ) from None

    def resolve_capture_manager_origin(
        self, root: str, fields: tuple[str, ...], *,
        timeout_s: float | None = None,
    ) -> Mapping[str, object]:
        deadline = self._capture_command_deadline(timeout_s)
        frame_stack_level = self._require_capture_frame()
        root = self._capture_identifier(root, name="manager root")
        if isinstance(fields, str) or not isinstance(fields, tuple):
            raise ProtocolError("capture manager fields are invalid")
        checked_fields = tuple(
            self._capture_identifier(field, name="manager field") for field in fields
        )
        frame_roots = {
            item.name.casefold(): item.name
            for item in self._capture_frame_variables
        }
        canonical_root = frame_roots.get(root.casefold())
        if canonical_root is None:
            raise ProtocolError("capture manager root is unavailable")
        physical_path = ".".join((canonical_root, *checked_fields))
        native_path = "Контекст.КонтекстОтладки." + physical_path
        existing = next(
            (
                key
                for key, value in self._capture_manager_paths.items()
                if value.casefold() == native_path.casefold()
            ),
            None,
        )
        if existing is not None:
            self._capture_remaining_timeout(deadline)
            return {
                "key": existing,
                "handle": existing,
                "type_name": "МенеджерВременныхТаблиц",
            }
        expression = (
            f"ТипЗнч({physical_path}) = "
            'Тип("МенеджерВременныхТаблиц")'
        )

        def admit_manager(proof: EvaluationResult) -> bool:
            if (
                proof.error_occurred
                or proof.type_name != "Булево"
                or evaluation_to_python(proof) is not True
            ):
                raise ProtocolError("capture manager origin is unavailable")
            return True

        self._evaluate_capture_helper(
            expression,
            evaluation_kind=CaptureEvaluationKind.INSPECTION,
            stack_level=frame_stack_level,
            result_policy=admit_manager,
            timeout_s=self._capture_remaining_timeout(deadline),
        )
        self._capture_remaining_timeout(deadline)
        handle = existing or "capture_manager_" + uuid4().hex
        self._capture_manager_paths[handle] = native_path
        return {"key": handle, "handle": handle, "type_name": "МенеджерВременныхТаблиц"}

    def capture_temporary_tables(
        self,
        manager_handle: str,
        *,
        names: tuple[str, ...] | None,
        cursor: int,
        limit: int,
        selection: Mapping[str, object] | None,
        timeout_s: float | None = None,
    ) -> Mapping[str, object]:
        deadline = self._capture_command_deadline(timeout_s)
        self._require_capture_frame()
        if not isinstance(manager_handle, str) or manager_handle not in self._capture_manager_paths:
            raise ProtocolError("capture manager handle is stale or invalid")
        if names is None or isinstance(names, str) or len(names) != 1:
            raise ProtocolError("capture table inspection requires one named table")
        if type(cursor) is not int or cursor != 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ProtocolError("capture table page is invalid")
        name = self._capture_identifier(names[0], name="table name")
        offset, row_limit, columns = self._capture_table_selection(selection)
        manager_path = self._capture_manager_paths[manager_handle]
        if selection is None:
            schema = self._capture_temporary_table_schema(
                manager_path, name, deadline=deadline
            )
            handle = "capture_table_metadata_" + uuid4().hex
            self._capture_metadata_handles.add(handle)
            return {
                "items": ({"name": name, "schema": schema, "handle": handle},),
                "total": 1,
                "next_cursor": None,
            }
        context_key = "__onec_capture_table_" + uuid4().hex
        columns_expression = (
            "Новый Массив"
            if not columns
            else "СтрРазделить(" + bsl_string_literal(",".join(columns)) + ", \",\")"
        )
        expression = (
            "RuntimeKernelServer.СохранитьВременнуюТаблицуОтладки(Контекст, "
            + manager_path
            + ", " + bsl_string_literal(name)
            + ", " + bsl_string_literal(context_key)
            + f", {offset}, {row_limit}, " + columns_expression + ")"
        )
        def accept_projection(result: EvaluationResult) -> None:
            if result.error_occurred:
                raise BslExecutionError(result.error_text)
            return None

        self._evaluate_capture_helper(
            expression,
            evaluation_kind=CaptureEvaluationKind.INSPECTION,
            stack_level=self._required_capture_kernel_stack_level(),
            result_policy=accept_projection,
            timeout_s=self._capture_remaining_timeout(deadline),
        )
        self._capture_remaining_timeout(deadline)
        native_path = "Контекст." + context_key
        schema_result = self.inspect_declared_table_schema(
            native_path,
            timeout_s=self._capture_remaining_timeout(deadline),
        )
        self._capture_remaining_timeout(deadline)
        schema = self._capture_schema_names(schema_result.collection_rows)
        handle = "capture_table_" + uuid4().hex
        self._capture_value_paths[handle] = native_path
        return {
            "items": ({"name": name, "schema": schema, "handle": handle},),
            "total": 1,
            "next_cursor": None,
        }

    def _capture_temporary_table_schema(
        self, manager_path: str, name: str, *, deadline: float
    ) -> tuple[str, ...]:
        expression = (
            "RuntimeKernelServer.ПолучитьСхемуВременнойТаблицыОтладки("
            + manager_path
            + ", "
            + bsl_string_literal(name)
            + ")"
        )
        rows: list[object] = []
        start_index = 0
        while len(rows) <= MAX_CAPTURE_SCHEMA_COLUMNS:
            page_size = min(64, MAX_CAPTURE_SCHEMA_COLUMNS + 1 - len(rows))
            result = self.session.evaluate_collection(
                expression,
                start_index=start_index,
                page_size=page_size,
                timeout_s=self._capture_remaining_timeout(deadline),
                max_text_size=4096,
                stack_level=self._required_capture_kernel_stack_level(),
            )
            self._capture_remaining_timeout(deadline)
            if result.error_occurred:
                raise BslExecutionError(result.error_text)
            page = result.collection_rows
            rows.extend(page)
            if len(rows) > MAX_CAPTURE_SCHEMA_COLUMNS:
                raise ProtocolError("capture table schema exceeds bounded metadata")
            if result.collection_size is not None:
                if result.collection_size > MAX_CAPTURE_SCHEMA_COLUMNS:
                    raise ProtocolError("capture table schema exceeds bounded metadata")
                if len(rows) >= result.collection_size:
                    break
            if len(page) < page_size:
                break
            start_index += len(page)
        return self._capture_schema_names(rows)

    @classmethod
    def _capture_table_selection(
        cls, selection: Mapping[str, object] | None
    ) -> tuple[int, int, tuple[str, ...]]:
        if selection is None:
            return 0, 0, ()
        if not isinstance(selection, Mapping) or set(selection) != {"offset", "limit", "columns"}:
            raise ProtocolError("capture table selection is invalid")
        offset, limit, columns = selection["offset"], selection["limit"], selection["columns"]
        if (
            type(offset) is not int
            or offset < 0
            or offset > MAX_CAPTURE_PROJECTION_POSITION
            or type(limit) is not int
            or not 1 <= limit <= 100
            or offset + limit > MAX_CAPTURE_PROJECTION_POSITION
        ):
            raise ProtocolError("capture table selection bounds are invalid")
        if isinstance(columns, str) or not isinstance(columns, Sequence) or len(columns) > 100:
            raise ProtocolError("capture table selection columns are invalid")
        return offset, limit, tuple(cls._capture_identifier(column, name="table column") for column in columns)

    @staticmethod
    def _capture_schema_names(rows: Sequence[object]) -> tuple[str, ...]:
        names: list[str] = []
        for row in rows:
            cells = getattr(row, "cells", ())
            for cell in cells:
                if getattr(cell, "name", "") == "Имя":
                    name = getattr(cell, "value_string", "") or getattr(cell, "presentation", "")
                    if isinstance(name, str) and name:
                        names.append(name.strip('"'))
        return tuple(names)

    def capture_value_handle(self, handle: str) -> str:
        self._require_capture_frame()
        if handle in self._capture_manager_paths:
            return self._capture_manager_paths[handle]
        if handle in self._capture_metadata_handles:
            raise ProtocolError(
                "capture table inventory is metadata-only; request a bounded row selection"
            )
        try:
            return self._capture_value_paths[handle]
        except KeyError as error:
            raise ProtocolError("capture value handle is stale or invalid") from error

    def is_capture_metadata_handle(self, handle: str) -> bool:
        """Prove an opaque inventory handle belongs to the active frame."""
        self._require_capture_frame()
        return isinstance(handle, str) and handle in self._capture_metadata_handles

    @classmethod
    def _with_message_collector(
        cls,
        source: str,
        messages_intercepted: int,
        message_collector_key: str,
    ) -> str:
        mapped = cls._compatibility_mapped_source(source, "collector-compatibility")
        return cls._with_message_collector_mapped(
            mapped,
            messages_intercepted,
            message_collector_key,
        ).text

    @staticmethod
    def _with_worker_globals_mapped(
        source: MappedSource,
        names: tuple[str, ...],
    ) -> MappedSource:
        """Expose only referenced notebook values for one Worker invocation."""
        if not names:
            return source
        worker = "__OnecNotebookGlobalWorker"
        previous = "__OnecNotebookPreviousGlobals"
        globals_name = "__OnecNotebookBoundGlobals"
        initialization = (
            f'{worker} = Контекст.RuntimeWorkerActiveGeneration.Modules.Получить("Worker");\n'
            f"{previous} = {worker}.__OnecNotebookGlobals;\n"
            f"{globals_name} = Новый Структура;\n"
        )
        for name in names:
            initialization += (
                f"{globals_name}.Вставить({bsl_string_literal(name)}, "
                f"Контекст.{name});\n"
            )
        initialization += f"{worker}.__OnecNotebookGlobals = {globals_name};\n"
        restore = f"{worker}.__OnecNotebookGlobals = {previous};\n"
        start = SourceSpan(0, 0)
        end = SourceSpan(len(source.text), len(source.text))
        builder = SourceTransformBuilder(source)
        builder.synthetic(initialization, start, "worker_globals_initialize")
        builder.synthetic("Попытка\n", start, "worker_globals_try")
        builder.copy(SourceSpan(0, len(source.text)))
        builder.synthetic("\n" + restore, end, "worker_globals_finalize")
        builder.synthetic("Исключение\n", end, "worker_globals_exception")
        builder.synthetic(restore, end, "worker_globals_finalize")
        builder.synthetic("ВызватьИсключение;\n", end, "worker_globals_rethrow")
        builder.synthetic("КонецПопытки;", end, "worker_globals_end")
        return builder.build(
            SourceArtifactKind.COLLECTOR_WRAPPER,
            wrapper_semantic_version="notebook-globals-v1",
            mode=source.artifact.mode,
        )

    @staticmethod
    def _with_message_collector_mapped(
        source: MappedSource,
        messages_intercepted: int,
        message_collector_key: str,
        *,
        worker_messages: bool = False,
    ) -> MappedSource:
        if not isinstance(source, MappedSource):
            raise ValueError("message collector source must be mapped")
        if not messages_intercepted:
            return source
        worker_object = "__OnecPinnedWorkerGenerationMessageObject"
        previous_sink = "__OnecPinnedWorkerGenerationPreviousMessageSink"
        finalize = (
            (
                f"{worker_object}.__OnecWorkerMessageSink = "
                f"{previous_sink};\n"
                if worker_messages else ""
            )
            + 'Контекст.Вставить("__onec_cell_messages_result_key", '
            + bsl_string_literal(message_collector_key)
            + ");\n"
            + 'Контекст.Вставить("__onec_cell_messages_result", Контекст.'
            + message_collector_key
            + ");\n"
            + "Контекст.Удалить("
            + bsl_string_literal(message_collector_key)
            + ");"
        )
        start = SourceSpan(0, 0)
        end = SourceSpan(len(source.text), len(source.text))
        builder = SourceTransformBuilder(source)
        initialize = f'Контекст.Вставить("{message_collector_key}", Новый Массив);\n'
        if worker_messages:
            initialize += (
                f"{worker_object} = "
                'Контекст.RuntimeWorkerActiveGeneration.Modules.Получить("Worker");\n'
                f"{previous_sink} = "
                f"{worker_object}.__OnecWorkerMessageSink;\n"
                f"{worker_object}.__OnecWorkerMessageSink = "
                f"Контекст.{message_collector_key};\n"
            )
        builder.synthetic(
            initialize,
            start,
            "message_collector_initialize",
        )
        builder.synthetic("Попытка\n", start, "message_collector_try")
        builder.copy(SourceSpan(0, len(source.text)))
        builder.synthetic(
            "\n" + finalize + "\n",
            end,
            "message_collector_finalize",
        )
        builder.synthetic(
            "Исключение\n",
            end,
            "message_collector_exception",
        )
        builder.synthetic(
            finalize + "\n",
            end,
            "message_collector_finalize",
        )
        builder.synthetic(
            "ВызватьИсключение;\n",
            end,
            "message_collector_rethrow",
        )
        builder.synthetic("КонецПопытки;", end, "message_collector_end")
        return builder.build(
            SourceArtifactKind.COLLECTOR_WRAPPER,
            wrapper_semantic_version="message-collector-v1",
            mode=source.artifact.mode,
        )

    @staticmethod
    def _as_executed_source(
        source: MappedSource,
        *,
        mode: LoweringMode,
    ) -> MappedSource:
        builder = SourceTransformBuilder(source)
        builder.copy(SourceSpan(0, len(source.text)))
        return builder.build(
            SourceArtifactKind.EXECUTED_BSL,
            wrapper_semantic_version="runtime-execution-v1",
            mode=mode.value,
        )

    @staticmethod
    def _compatibility_mapped_source(source: str, purpose: str) -> MappedSource:
        digest = source_sha256(source)
        return mapped_visible_source(
            source,
            SourceUnitRef(
                SourceUnitKind.NOTEBOOK_CELL,
                f"runtime-{purpose}:{digest}",
                0,
                digest,
            ),
        )

    @staticmethod
    def _visible_context(
        mapped: MappedSource,
        visible_source: str,
    ) -> VisibleSourceContext | None:
        digest = source_sha256(visible_source)
        units = {
            reference
            for segment in mapped.source_map.segments
            for reference in (segment.origin_ref, segment.anchor_ref)
            if isinstance(reference, SourceUnitRef)
            and reference.source_sha256 == digest
        }
        if not units:
            return None
        return VisibleSourceContext({unit: visible_source for unit in units})

    def message_collector_key(self, mode: LoweringMode) -> str:
        operation_id = (
            self.operation_id + 1
            if mode is LoweringMode.MAIN
            else self.active_operation.operation_id if self.active_operation else 0
        )
        cell_sequence = 0 if mode is LoweringMode.MAIN else self.cell_sequence + 1
        return (
            f"__onec_cell_messages_{self.runtime_generation}_{operation_id}_{cell_sequence}"
        )

    def execute_system_capture(self, source: str) -> CaptureCellResult | DebugStop:
        """Execute trusted runtime BSL verbatim in the current capture frame."""
        mapped = self._compatibility_mapped_source(source, "system-capture")
        return self._execute_capture(
            source,
            mapped,
            visible_source_context=self._visible_context(mapped, source),
            evaluation_kind=CaptureEvaluationKind.MATERIALIZATION_HELPER,
        )

    def install_capture_worker_generation_pin(
        self,
        manifest_sha256: str,
    ) -> None:
        """Bind the host-fenced Worker root into the ephemeral CAPTURE slot."""
        self._require_capture_evaluation_admission()
        if (
            not isinstance(manifest_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", manifest_sha256) is None
        ):
            raise ProtocolError("CAPTURE Worker manifest identity is invalid")
        def accept_pin(result: EvaluationResult) -> None:
            if result.error_occurred or evaluation_to_python(result) is not True:
                raise ProtocolError("CAPTURE Worker pin installation failed")
            return None

        self._evaluate_capture_helper(
            "RuntimeKernelServer.УстановитьПинПоколенияWorker(Контекст, "
            f"{bsl_string_literal(manifest_sha256)})",
            evaluation_kind=CaptureEvaluationKind.MATERIALIZATION_HELPER,
            stack_level=self._required_capture_kernel_stack_level(),
            result_policy=accept_pin,
        )

    def clear_capture_worker_generation_pin(self) -> None:
        """Remove only the reserved ephemeral CAPTURE Worker slot."""
        self._require_capture_evaluation_admission()

        def accept_cleanup(result: EvaluationResult) -> None:
            if result.error_occurred or evaluation_to_python(result) is not True:
                raise ProtocolError("CAPTURE Worker pin cleanup failed")
            return None

        self._evaluate_capture_helper(
            "RuntimeKernelServer.ОчиститьПинПоколенияWorker(Контекст)",
            evaluation_kind=CaptureEvaluationKind.MATERIALIZATION_HELPER,
            stack_level=self._required_capture_kernel_stack_level(),
            result_policy=accept_cleanup,
        )

    def _execute_capture(
        self,
        visible_source: str,
        lowered_source: str | MappedSource,
        *,
        visible_source_context: VisibleSourceContext | None = None,
        messages_intercepted: int = 0,
        message_collector_key: str = "",
        worker_messages: bool = False,
        worker_globals: tuple[str, ...] = (),
        dirty_roots: tuple[str, ...] = (),
        on_transport_dispatch: Callable[[], None] | None = None,
        evaluation_kind: CaptureEvaluationKind = CaptureEvaluationKind.USER_BSL,
    ) -> CaptureCellResult | DebugStop:
        self._require_capture_evaluation_admission()
        if self.active_operation is None:
            raise ProtocolError("Capture cell has no active MAIN operation")
        selected_timeout = self.command_timeout_s
        mapped_lowered = (
            lowered_source
            if isinstance(lowered_source, MappedSource)
            else self._compatibility_mapped_source(lowered_source, "capture-cell")
        )
        messages_intercepted = max(messages_intercepted, int(worker_messages))
        bound_source = self._with_worker_globals_mapped(
            mapped_lowered, worker_globals
        )
        wrapped_source = self._with_message_collector_mapped(
            bound_source,
            messages_intercepted,
            message_collector_key,
            worker_messages=worker_messages,
        )
        executed_source = self._as_executed_source(
            wrapped_source,
            mode=LoweringMode.CAPTURE,
        )
        lowered_text = executed_source.text
        if visible_source_context is None:
            visible_source_context = self._visible_context(
                executed_source,
                visible_source,
            )
        self.cell_sequence += 1
        cell_fields = {
            "runtime_generation": self.runtime_generation,
            "operation_id": self.active_operation.operation_id,
            "capture_stop_sequence": self.stop_sequence,
            "cell_sequence": self.cell_sequence,
        }
        self._record(
            "write-journal.jsonl",
            "cell_started",
            **cell_fields,
            visible_sha256=sha256(visible_source.encode("utf-8")).hexdigest(),
            lowered_sha256=executed_source.artifact.source_sha256,
            state_before=OperationState.CAPTURED.value,
            dirty_roots=list(dirty_roots),
            immediate_object_mutation_possible=True,
        )
        stack_level = self._required_capture_kernel_stack_level()
        operation = self.active_operation
        owner = self._capture_evaluation_owner()
        owned = self._capture_owned_submission
        sealed_messages: tuple[str, ...] = ()
        platform_error: BslExecutionError | None = None

        def shield_workspace() -> None:
            self._set_workspace(
                "capture-evaluation",
                self.registry.evaluation_locations,
            )

        def restore_workspace() -> None:
            self._set_workspace("full-restore", self.registry.full_locations)

        primary = self._capture_remote_step(
            build_live_current_capture_call(lowered_text),
            stack_level=stack_level,
            timeout_s=selected_timeout,
            before_dispatch=shield_workspace,
            pre_dispatch_cleanup=restore_workspace,
            on_transport_dispatch=on_transport_dispatch,
            restore=None if messages_intercepted else restore_workspace,
        )

        def apply_result(
            context: CaptureStepContext,
            event: EvaluationResult,
        ) -> object:
            nonlocal sealed_messages, platform_error
            if owned is not None:
                owned.primary_execution()
            if messages_intercepted:
                message_step = self._capture_remote_step(
                    "RuntimeKernelServer.ЗабратьСообщенияЯчейкиИзКонтекста(Контекст, "
                    + bsl_string_literal(message_collector_key)
                    + ")",
                    stack_level=stack_level,
                    timeout_s=selected_timeout,
                    restore=restore_workspace,
                )
                sealed_messages = self._decode_cell_messages(
                    context.execute_inline(message_step)
                )
            if event.error_occurred:
                platform_error = BslExecutionError(
                    event.error_text,
                    messages=sealed_messages,
                    diagnostic=_safe_platform_diagnostic(
                        event.error_text,
                        executed_source,
                        visible_source_context=visible_source_context,
                    ),
                )
                if owned is not None:
                    platform_error = owned.normalize_error(platform_error)
                return None
            return evaluation_to_python(event)

        def preserve_platform_error(error: BaseException) -> BaseException:
            if isinstance(error, BslExecutionError) and platform_error is not None:
                return platform_error
            return error

        external_completion = None if owned is None else owned.completion

        def complete_cell(
            value: object,
            error: BaseException | None,
        ) -> object:
            self._complete_capture_lifecycle(value, error)
            cell = (
                CaptureCellResult(
                    operation.operation_id,
                    visible_source,
                    lowered_text,
                    value,
                    sealed_messages,
                )
                if error is None
                else None
            )
            self._record(
                "write-journal.jsonl",
                "cell_completed" if error is None else "cell_failed",
                **cell_fields,
                state_after=self.state.value,
                **({} if error is None else {"error_type": type(error).__name__}),
            )
            self._flush_journal()
            if external_completion is not None:
                return external_completion(cell, error)
            return cell

        request = CaptureEvaluationRequest(
            owner._fence,
            evaluation_kind,
            primary.dispatch,
            primary.poll,
            evaluation_to_python,
            restore=primary.restore,
            seal_messages=lambda: sealed_messages,
            pin_lease=(
                (lambda disposition: None)
                if owned is None
                else owned.pin_lease
            ),
            step_policy=apply_result,
            completion=complete_cell,
            initiator_error_policy=preserve_platform_error,
        )
        return self._submit_capture_request(
            request,
            return_ticket=owned is not None,
            timeout_s=selected_timeout,
            helper_handoff=(
                owned is None
                and evaluation_kind is not CaptureEvaluationKind.USER_BSL
            ),
        )  # type: ignore[return-value]

    def _handle_capture_evaluation_event(
        self,
        capture: _PendingCaptureEvaluation,
        event: EvaluationResult | StopEvent,
    ) -> CaptureCellResult | DebugStop:
        if self.pending_capture_evaluation is not capture:
            raise ProtocolError("Pending CAPTURE evaluation identity changed")
        if isinstance(event, StopEvent):
            classified = classify_stop(
                event,
                self.registry,
                worker_locations=(
                    self.breakpoint_workspace_owner.confirmed_snapshot.worker_slots
                ),
            )
            self.stop_history.append(classified)
            debug_stop = DebugStop(capture.operation, event, classified.reason)
            self.last_debug_stop = debug_stop
            self.state = OperationState.CAPTURE_DEBUG_STOPPED
            return debug_stop
        try:
            try:
                self._set_workspace("full-restore", self.registry.full_locations)
            except BaseException as restore_error:
                self.state = OperationState.BREAKPOINT_RESTORE_FAILURE
                raise BreakpointRestoreError(
                    "Failed to restore full breakpoint workspace"
                ) from restore_error
            self.state = OperationState.CAPTURED
            self.pending_capture_evaluation = None
            self.last_debug_stop = None
            if event.error_occurred:
                messages = self._take_capture_cell_messages(
                    capture.message_collector_key
                    if capture.messages_intercepted
                    else ""
                )
                diagnostic = _safe_platform_diagnostic(
                    event.error_text,
                    capture.executed_source,
                    visible_source_context=capture.visible_source_context,
                )
                raise BslExecutionError(
                    event.error_text,
                    messages=messages,
                    diagnostic=diagnostic,
                )
            value = evaluation_to_python(event)
            messages = self._take_capture_cell_messages(
                capture.message_collector_key
                if capture.messages_intercepted
                else ""
            )
            cell = CaptureCellResult(
                capture.operation.operation_id,
                capture.visible_source,
                capture.lowered_source,
                value,
                messages,
            )
        except BaseException as error:
            self._record(
                "write-journal.jsonl",
                "cell_failed",
                **capture.cell_fields,
                state_after=self.state.value,
                error_type=type(error).__name__,
            )
            self._flush_journal()
            raise
        self._record(
            "write-journal.jsonl",
            "cell_completed",
            **capture.cell_fields,
            state_after=self.state.value,
        )
        self._flush_journal()
        return cell

    def resume(
        self,
        *,
        dirty_roots: tuple[str, ...] = (),
        continuation_attempt_id: str | None = None,
        on_transport_dispatch: Callable[[], None] | None = None,
    ) -> MainCompletion | CapturedStop | DebugStop:
        self._require_state(OperationState.CAPTURED)
        dirty_roots = tuple(dirty_roots)
        attempt = self._continuation_attempt(
            dirty_roots, continuation_attempt_id
        )
        self.state = OperationState.FLUSHING
        for root in dirty_roots:
            try:
                transfer = self.session.evaluate(
                    build_live_capture_root_transfer_call(root),
                    stack_level=self._required_capture_kernel_stack_level(),
                )
            except BaseException as error:
                self._mark_continuation_root(
                    attempt, root, "failed", error=type(error).__name__
                )
                self._mark_later_roots_unattempted(attempt, root)
                self.state = (
                    OperationState.RECOVERING
                    if isinstance(error, RdbgTransportError)
                    else OperationState.PARTIAL_WRITEBACK_FAILURE
                )
                raise
            if transfer.error_occurred:
                self._mark_continuation_root(
                    attempt, root, "failed", error=transfer.error_text
                )
                self._mark_later_roots_unattempted(attempt, root)
                self.state = OperationState.PARTIAL_WRITEBACK_FAILURE
                raise PartialWritebackError(
                    f"Capture root {root} transfer failed: {transfer.error_text}"
                )
            address = evaluation_to_python(transfer)
            if not isinstance(address, str) or not address:
                self._mark_continuation_root(
                    attempt,
                    root,
                    "failed",
                    error="invalid temporary-storage address",
                )
                self._mark_later_roots_unattempted(attempt, root)
                self.state = OperationState.PARTIAL_WRITEBACK_FAILURE
                raise PartialWritebackError(
                    f"Capture root {root} temporary-storage address is invalid"
                )
            expression = build_temporary_storage_value_expression(address)
            expression_sha256 = sha256(expression.encode("utf-8")).hexdigest()
            self._set_root_status(root, expression_sha256, SideEffectStatus.PLANNED)
            self._set_root_status(root, expression_sha256, SideEffectStatus.SENT)
            self._mark_continuation_root(attempt, root, "sent")
            try:
                result = self.session.modify(root, expression)
            except RdbgTransportError as error:
                self._set_root_status(
                    root,
                    expression_sha256,
                    SideEffectStatus.OUTCOME_UNKNOWN,
                    error=str(error),
                )
                self._mark_continuation_root(
                    attempt, root, "outcome_unknown", error=str(error)
                )
                self._mark_later_roots_unattempted(attempt, root)
                raise
            self._set_root_status(
                root,
                expression_sha256,
                SideEffectStatus.ACKNOWLEDGED,
                result_id=result.result_id,
            )
            self._record(
                "write-journal.jsonl",
                "root_write_acknowledged",
                runtime_generation=self.runtime_generation,
                operation_id=self.active_operation.operation_id,
                root=root,
                result_id=str(result.result_id),
            )
            if result.error_occurred:
                self._set_root_status(
                    root,
                    expression_sha256,
                    SideEffectStatus.FAILED,
                    result_id=result.result_id,
                    error=result.error_text,
                )
                self._mark_continuation_root(
                    attempt, root, "failed", error=result.error_text
                )
                self._mark_later_roots_unattempted(attempt, root)
                self.state = OperationState.PARTIAL_WRITEBACK_FAILURE
                raise PartialWritebackError(
                    f"Capture root {root} failed: {result.error_text}"
                )
            self._set_root_status(
                root,
                expression_sha256,
                SideEffectStatus.SUCCEEDED,
                result_id=result.result_id,
            )
            self._mark_continuation_root(attempt, root, "succeeded")
            if sum(entry.succeeded for entry in self.write_journal) == 1:
                self._inject(
                    FaultPoint.AFTER_FIRST_ROOT_WRITE,
                    RecoveryPhase.FLUSHING,
                )
        self._record(
            "write-journal.jsonl",
            "capture_cleanup_planned",
            runtime_generation=self.runtime_generation,
            operation_id=self.active_operation.operation_id,
        )
        self._flush_journal()
        try:
            close = self.session.evaluate(
                build_live_capture_end_call(),
                stack_level=self._required_capture_kernel_stack_level(),
            )
        except RdbgTransportError as error:
            self._record(
                "write-journal.jsonl",
                "capture_cleanup_outcome_unknown",
                runtime_generation=self.runtime_generation,
                operation_id=self.active_operation.operation_id,
                error=str(error),
            )
            self._flush_journal()
            raise
        if close.error_occurred:
            self._record(
                "write-journal.jsonl",
                "capture_cleanup_failed",
                runtime_generation=self.runtime_generation,
                operation_id=self.active_operation.operation_id,
                error=close.error_text,
            )
            self._flush_journal()
            self.state = OperationState.PARTIAL_WRITEBACK_FAILURE
            raise PartialWritebackError(
                f"Capture cleanup failed after write-back: {close.error_text}"
            )
        self._record(
            "write-journal.jsonl",
            "capture_cleanup_completed",
            runtime_generation=self.runtime_generation,
            operation_id=self.active_operation.operation_id,
        )
        self._flush_journal()
        self.state = OperationState.RESUMING
        attempt.continue_state = "planned"
        self._record(
            "write-journal.jsonl",
            "continue_planned",
            attempt_id=attempt.spec.attempt_id,
            runtime_generation=self.runtime_generation,
            operation_id=self.active_operation.operation_id,
        )
        self._flush_journal()
        attempt.continue_state = "sent"
        self._record(
            "write-journal.jsonl",
            "continue_sent",
            attempt_id=attempt.spec.attempt_id,
            runtime_generation=self.runtime_generation,
            operation_id=self.active_operation.operation_id,
        )
        self._flush_journal()
        try:
            if on_transport_dispatch is not None:
                on_transport_dispatch()
            self.require_debug_workspace_ready()
            self.session.continue_()
        except RdbgTransportError as error:
            # The transport gives no pre-send acknowledgement. Treat this as
            # an ambiguous continuation: the paused frame may already be gone.
            self._clear_capture_inspection()
            self.state = OperationState.RECOVERING
            attempt.continue_state = "outcome_unknown"
            self._record(
                "write-journal.jsonl",
                "continue_outcome_unknown",
                attempt_id=attempt.spec.attempt_id,
                runtime_generation=self.runtime_generation,
                operation_id=self.active_operation.operation_id,
                error=str(error),
            )
            self._flush_journal()
            raise
        # The debugger has accepted Continue; no frame-backed resolver may
        # survive into the next stop, even if the subsequent wait is unknown.
        self._clear_capture_inspection()
        attempt.continue_state = "acknowledged"
        self._record(
            "write-journal.jsonl",
            "continue_acknowledged",
            attempt_id=attempt.spec.attempt_id,
            runtime_generation=self.runtime_generation,
            operation_id=self.active_operation.operation_id,
        )
        self.continue_sent = True
        self._flush_journal()
        self._inject(
            FaultPoint.AFTER_CONTINUE_ACK,
            RecoveryPhase.RESUMING,
        )
        stop = self.session.wait_for_any_stop(timeout_s=self.command_timeout_s)
        return self._route_stop(stop)

    def _lose_generation(
        self,
        checkpoint: RecoveryCheckpoint,
        reason: str,
    ) -> RecoveryResult:
        self.state = OperationState.LOST
        self._record(
            "recovery-transitions.jsonl",
            "lost",
            checkpoint_sequence=checkpoint.sequence,
            phase=checkpoint.phase.value,
            reason=reason,
        )
        self._flush_journal()
        self.session.invalidate()
        if not self._generation_lost_notified:
            self._generation_lost_notified = True
            if self.on_generation_lost is not None:
                self.on_generation_lost(checkpoint)
        return RecoveryResult(
            RecoveryOutcome.LOST,
            checkpoint.phase,
            checkpoint,
            reason,
        )

    @staticmethod
    def _validate_target_identity(
        checkpoint: RecoveryCheckpoint,
        reconnected: ReconnectedSession,
    ) -> None:
        target = reconnected.session.target
        if target is None:
            raise ProtocolError("Recovered session has no selected target")
        if target.target_id != checkpoint.target.target_id:
            raise ProtocolError("Recovered target identity changed")
        if target.target_type != checkpoint.target.target_type:
            raise ProtocolError("Recovered target type changed")

    def recover_transport(
        self,
        reconnector: Callable[[RecoveryCheckpoint, bool], ReconnectedSession],
    ) -> RecoveryResult:
        self._require_state(OperationState.RECOVERING)
        checkpoint = self.recovery_checkpoint
        if checkpoint is None:
            raise ProtocolError("RECOVERING state has no checkpoint")
        self._record(
            "recovery-transitions.jsonl",
            "recovering",
            checkpoint_sequence=checkpoint.sequence,
            phase=checkpoint.phase.value,
        )
        self._flush_journal()
        if checkpoint.phase is RecoveryPhase.FLUSHING:
            return self._lose_generation(
                checkpoint,
                "partial write-back cannot be replayed",
            )

        reconnected: ReconnectedSession | None = None
        try:
            reconnected = reconnector(
                checkpoint,
                checkpoint.phase is RecoveryPhase.RESUMING,
            )
            self._validate_target_identity(checkpoint, reconnected)
            if checkpoint.phase is RecoveryPhase.CAPTURED:
                if reconnected.evidence is None:
                    raise ProtocolError("Paused recovery has no frame evidence")
                validate_paused_identity(checkpoint, reconnected.evidence)
                old_session = self.session
                self.session = reconnected.session
                self.breakpoint_workspace_owner._adopt_confirmed_session(
                    reconnected.session,
                    checkpoint.breakpoint_workspace,
                )
                old_session.invalidate()
                self.state = OperationState.CAPTURED
                self._record(
                    "recovery-transitions.jsonl",
                    "recovered",
                    checkpoint_sequence=checkpoint.sequence,
                    phase=checkpoint.phase.value,
                )
                self._flush_journal()
                return RecoveryResult(
                    RecoveryOutcome.RECOVERED,
                    checkpoint.phase,
                    checkpoint,
                    "exact paused identity recovered",
                )

            stop = (
                reconnected.evidence.stop
                if reconnected.evidence is not None
                else reconnected.session.wait_for_any_stop(
                    timeout_s=self.command_timeout_s
                )
            )
            if stop.target_id != checkpoint.target.target_id:
                raise ProtocolError("Recovered stop belongs to another target")
            old_session = self.session
            self.session = reconnected.session
            self.breakpoint_workspace_owner._adopt_confirmed_session(
                reconnected.session,
                checkpoint.breakpoint_workspace,
            )
            old_session.invalidate()
            self.state = OperationState.RESUMING
            continuation = self._route_stop(stop)
            self._record(
                "recovery-transitions.jsonl",
                "recovered",
                checkpoint_sequence=checkpoint.sequence,
                phase=checkpoint.phase.value,
            )
            self._flush_journal()
            return RecoveryResult(
                RecoveryOutcome.RECOVERED,
                checkpoint.phase,
                checkpoint,
                "continued operation observed without replay",
                continuation,
            )
        except Exception as error:
            if reconnected is not None and reconnected.session is not self.session:
                reconnected.session.invalidate()
            return self._lose_generation(checkpoint, str(error))

    def _complete_main(self) -> MainCompletion:
        if self.active_operation is None:
            self.state = OperationState.FAILED
            raise ProtocolError("MAIN completion has no active operation")
        completed_result = self.session.evaluate("ЗавершеннаяКоманда")
        if completed_result.error_occurred:
            self.state = OperationState.FAILED
            raise BslExecutionError(completed_result.error_text)
        completed_id = self._decode_main_completion_value(
            completed_result,
            phase="completed_command",
        )
        if completed_id != self.active_operation.operation_id:
            self.state = OperationState.FAILED
            raise ProtocolError(
                f"Completed command {completed_id!r} does not match active "
                f"operation {self.active_operation.operation_id}"
            )
        result_evaluation = self.session.evaluate("Результат")
        error_evaluation = self.session.evaluate("Ошибка")
        if result_evaluation.error_occurred:
            self.state = OperationState.FAILED
            raise BslExecutionError(result_evaluation.error_text)
        if error_evaluation.error_occurred:
            self.state = OperationState.FAILED
            raise BslExecutionError(error_evaluation.error_text)
        error_value = self._decode_main_completion_value(
            error_evaluation,
            phase="error",
        )
        error = "" if error_value is None else str(error_value)
        diagnostic = None
        if error and self.active_operation.executed_source is not None:
            diagnostic = _safe_platform_diagnostic(
                error,
                self.active_operation.executed_source,
                visible_source_context=self.active_operation.visible_source_context,
            )
        self.state = OperationState.FAILED if error else OperationState.COMPLETED
        result_value = self._decode_main_completion_value(
            result_evaluation,
            phase="result",
        )
        messages = self._take_kernel_cell_messages(
            self.active_operation.message_collector_key
            if self.active_operation.messages_intercepted
            else ""
        )
        completion = MainCompletion(
            self.active_operation,
            result_value,
            error,
            not error,
            messages,
            diagnostic,
        )
        self._record(
            "write-journal.jsonl",
            "main_completed" if completion.succeeded else "main_failed",
            runtime_generation=self.runtime_generation,
            operation_id=self.active_operation.operation_id,
            state_after=self.state.value,
            visible_sha256=sha256(
                self.active_operation.visible_source.encode("utf-8")
            ).hexdigest(),
            lowered_sha256=sha256(
                self.active_operation.lowered_source.encode("utf-8")
            ).hexdigest(),
        )
        self._flush_journal()
        return completion

    @staticmethod
    def _decode_main_completion_value(
        evaluation: EvaluationResult,
        *,
        phase: str,
    ) -> object:
        try:
            return evaluation_to_python(evaluation)
        except Exception as error:
            raise CompletionDecodeError(phase, evaluation) from error

    def _take_capture_cell_messages(
        self, message_collector_key: str
    ) -> tuple[str, ...]:
        return self._take_context_cell_messages(
            message_collector_key,
            stack_level=self._required_capture_kernel_stack_level(),
        )

    def _take_kernel_cell_messages(
        self, message_collector_key: str
    ) -> tuple[str, ...]:
        return self._take_context_cell_messages(message_collector_key, stack_level=0)

    def _take_context_cell_messages(
        self, message_collector_key: str, *, stack_level: int
    ) -> tuple[str, ...]:
        if not message_collector_key:
            return ()
        result = self.session.evaluate(
            "RuntimeKernelServer.ЗабратьСообщенияЯчейкиИзКонтекста(Контекст, "
            + bsl_string_literal(message_collector_key)
            + ")",
            stack_level=stack_level,
        )
        if result.error_occurred:
            raise BslExecutionError(result.error_text)
        return self._decode_cell_messages(result)

    @staticmethod
    def _decode_cell_messages(result: EvaluationResult) -> tuple[str, ...]:
        if result.error_occurred:
            raise BslExecutionError(result.error_text)
        payload = evaluation_to_python(result)
        if not isinstance(payload, str):
            raise ProtocolError("Cell message payload is not JSON text")
        try:
            messages = json.loads(payload)
        except json.JSONDecodeError as error:
            raise ProtocolError("Cell message payload is invalid JSON") from error
        if not isinstance(messages, list) or any(
            not isinstance(message, str) for message in messages
        ):
            raise ProtocolError("Cell message payload is not a text array")
        return tuple(messages)

    def take_context_string(self, key: str, *, max_text_size: int) -> str:
        if not re.fullmatch(
            r"__(?:onec_compact_table|onec_value)_[0-9a-f]{32}", key
        ):
            raise ProtocolError("materialization context key is invalid")
        if type(max_text_size) is not int or max_text_size <= 0:
            raise ProtocolError("compact table maximum text size is invalid")
        expression = (
            "RuntimeKernelServer."
            "ЗабратьКомпактнуюМатериализациюИзКонтекста(Контекст, "
            + bsl_string_literal(key)
            + ")"
        )

        def decode_payload(result: EvaluationResult) -> str:
            if result.error_occurred:
                raise BslExecutionError(result.error_text)
            value = evaluation_to_python(result)
            if not isinstance(value, str):
                raise ProtocolError("compact table payload is not a string")
            return value

        if self.state in {
            OperationState.CAPTURED,
            OperationState.EVALUATING_CAPTURE,
            OperationState.RECOVERING,
        }:
            return self._evaluate_capture_helper(
                expression,
                evaluation_kind=CaptureEvaluationKind.MATERIALIZATION_HELPER,
                stack_level=self._required_capture_kernel_stack_level(),
                max_text_size=max_text_size,
                result_policy=decode_payload,
            )  # type: ignore[return-value]
        return decode_payload(self.session.evaluate(
            expression,
            timeout_s=self.command_timeout_s,
            max_text_size=max_text_size,
            stack_level=0,
        ))

    def drop_context_value(self, key: str) -> None:
        if not re.fullmatch(
            r"__(?:onec_compact_table|onec_value|onec_projection)_[0-9a-f]{32}",
            key,
        ):
            raise ProtocolError("materialization context key is invalid")
        expression = (
            "RuntimeKernelServer."
            "УдалитьМатериализациюИзКонтекста(Контекст, "
            + bsl_string_literal(key)
            + ")"
        )

        def accept_drop(result: EvaluationResult) -> None:
            if result.error_occurred:
                raise BslExecutionError(result.error_text)

        if self.state in {
            OperationState.CAPTURED,
            OperationState.EVALUATING_CAPTURE,
            OperationState.RECOVERING,
        }:
            self._evaluate_capture_helper(
                expression,
                evaluation_kind=CaptureEvaluationKind.MATERIALIZATION_HELPER,
                stack_level=self._required_capture_kernel_stack_level(),
                result_policy=accept_drop,
            )
            return
        accept_drop(self.session.evaluate(
            expression,
            timeout_s=self.command_timeout_s,
            stack_level=0,
        ))

    def inspect_table_sample(
        self,
        handle: str,
        *,
        page_size: int = 16,
    ) -> EvaluationResult:
        if not re.fullmatch(
            r"Контекст\.[^\W\d]\w*(?:\.[^\W\d]\w*)*", handle, re.UNICODE
        ):
            raise ProtocolError(
                "table handle must be one direct or dotted persistent Context path"
            )
        if type(page_size) is not int or not 1 <= page_size <= 64:
            raise ProtocolError("table schema sample size is invalid")
        stack_level = (
            self._required_capture_kernel_stack_level()
            if self.state is OperationState.CAPTURED
            else 0
        )
        result = self.session.evaluate_collection(
            handle,
            start_index=0,
            page_size=page_size,
            timeout_s=self.command_timeout_s,
            max_text_size=4096,
            stack_level=stack_level,
        )
        if result.error_occurred:
            raise BslExecutionError(result.error_text)
        return result

    def inspect_completion_fields(
        self,
        handle: str,
        *,
        table_row: bool,
        worker_type_registrations: tuple[str, ...],
    ) -> EvaluationResult:
        """Admit and inspect bounded field names in one target operation."""
        if (not isinstance(handle, str) or len(handle) > 512
                or not re.fullmatch(r"Контекст\.[^\W\d]\w*(?:\.[^\W\d]\w*){0,7}", handle)
                or type(table_row) is not bool):
            raise ProtocolError("Completion requires a direct or dotted Context path")
        if (
            type(worker_type_registrations) is not tuple
            or any(
                not isinstance(registration, str)
                or not registration
                or "\n" in registration
                or "\r" in registration
                for registration in worker_type_registrations
            )
        ):
            raise ProtocolError("Completion Worker type registrations are invalid")
        stack_level = (
            self._required_capture_kernel_stack_level()
            if self.state is OperationState.CAPTURED else 0
        )
        result = self.session.evaluate_collection(
            "RuntimeValueTransferServer.ПолучитьДопущенныеИменаСвойствДляПодсказки("
            + handle
            + (", Истина, " if table_row else ", Ложь, ")
            + bsl_string_literal("\n".join(worker_type_registrations))
            + ")",
            # One admission-marker row plus the helper's bounded 128 names.
            start_index=0, page_size=129, max_text_size=512,
            timeout_s=self.command_timeout_s, stack_level=stack_level,
        )
        if result.error_occurred:
            raise BslExecutionError(result.error_text)
        return result

    def inspect_declared_table_schema(
        self, handle: str, *, timeout_s: float | None = None
    ) -> EvaluationResult:
        deadline = self._capture_command_deadline(timeout_s)
        if not re.fullmatch(
            r"Контекст\.[^\W\d]\w*(?:\.[^\W\d]\w*)*", handle, re.UNICODE
        ):
            raise ProtocolError(
                "table handle must be one direct or dotted persistent Context path"
            )
        stack_level = (
            self._required_capture_kernel_stack_level()
            if self.state is OperationState.CAPTURED
            else 0
        )
        result = self.session.evaluate_collection(
            "RuntimeTableTransferServer.ПолучитьКомпактнуюСхему(" + handle + ")",
            start_index=0,
            page_size=64,
            timeout_s=self._capture_remaining_timeout(deadline),
            max_text_size=4096,
            stack_level=stack_level,
        )
        self._capture_remaining_timeout(deadline)
        if result.error_occurred:
            raise BslExecutionError(result.error_text)
        return result
