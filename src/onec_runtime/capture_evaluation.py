"""Immutable snapshots and single-owner evaluation for the CAPTURE control plane.

Public snapshots are data-only and can be retained after a capture becomes
stale. Their representations never include debugger requests, handles, source,
or raw exceptions. Private coordinator requests live below the snapshot types.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import StrEnum
from math import isfinite
from threading import Condition, Lock, Thread, current_thread
from time import monotonic
from typing import TypeVar
from uuid import uuid4

from onec_runtime.breakpoint_workspace import BreakpointWorkspaceOutcomeUnknown
from onec_runtime.errors import (
    BslExecutionError,
    CaptureBusyError,
    CaptureEvaluationDeliveryError,
    CaptureEvaluationPendingError,
    CaptureOutcomeUnknownError,
    CaptureRecoveryRequiredError,
    CommandTimeout,
    NoCaptureEvaluationError,
    ProtocolError,
    StaleCaptureError,
    TargetLost,
    UnexpectedStop,
)
from onec_runtime.rdbg.models import EvaluationResult, PendingEvaluation, StopEvent
from onec_runtime.recovery_journal import RecoveryJournal


# These limits are part of the public safety boundary.  Timing is intentionally
# coarse and bounded; it is evidence about controller progress, not a trace.
MAX_CAPTURE_TIMING_MS = 86_400_000
MAX_CAPTURE_TIMING_COUNT = 1_000_000
MAX_CAPTURE_DIAGNOSTIC_CODEPOINTS = 256
MAX_CAPTURE_DIAGNOSTIC_MESSAGE_CODEPOINTS = 1_024
MAX_CAPTURE_MESSAGES = 100
MAX_CAPTURE_MESSAGE_CODEPOINTS = 1_024
MAX_CAPTURE_IDENTIFIER_CODEPOINTS = 256
_MESSAGE_TRUNCATION_NOTE = "Messages truncated to the first 100 entries."


class CapturePhase(StrEnum):
    PAUSED = "paused"
    EVALUATING = "evaluating"
    RESUMING = "resuming"
    RECOVERY_REQUIRED = "recovery_required"
    OUTCOME_UNKNOWN = "outcome_unknown"
    STALE = "stale"


class CaptureEvaluationKind(StrEnum):
    USER_BSL = "user_bsl"
    PUBLIC_VALUE_GUARD = "public_value_guard"
    INSPECTION = "inspection"
    MATERIALIZATION_HELPER = "materialization_helper"


class CaptureEvaluationState(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    UNKNOWN = "unknown"


_INTERNAL_EVALUATION_KINDS = frozenset(
    {
        CaptureEvaluationKind.PUBLIC_VALUE_GUARD,
        CaptureEvaluationKind.INSPECTION,
        CaptureEvaluationKind.MATERIALIZATION_HELPER,
    }
)
_EnumT = TypeVar("_EnumT", bound=StrEnum)


def _safe_text(value: object, *, name: str, limit: int, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    # Replace all control characters with spaces.  This keeps messages useful
    # in terminals and logs without allowing embedded control sequences.
    cleaned = "".join(character if ord(character) >= 0x20 else " " for character in value)
    cleaned = " ".join(cleaned.split())
    if not allow_empty and not cleaned:
        raise ValueError(f"{name} must not be empty")
    return cleaned[:limit]


def _identifier(value: object, *, name: str = "evaluation_id") -> str:
    return _safe_text(
        value,
        name=name,
        limit=MAX_CAPTURE_IDENTIFIER_CODEPOINTS,
        allow_empty=False,
    )


def _enum(value: object, enum_type: type[_EnumT], *, name: str) -> _EnumT:
    if isinstance(value, enum_type):
        return value
    if isinstance(value, str):
        try:
            return enum_type(value)
        except ValueError as error:
            raise ValueError(f"{name} is invalid") from error
    raise ValueError(f"{name} is invalid")


def _bounded_counter(value: object, *, name: str, maximum: int) -> int:
    if type(value) is not int:
        raise ValueError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return min(maximum, value)


def _bounded_offset(value: object, *, name: str) -> int | None:
    if value is None:
        return None
    return _bounded_counter(value, name=name, maximum=MAX_CAPTURE_TIMING_MS)


@dataclass(frozen=True, slots=True)
class CaptureEvaluationTiming:
    evaluation_id: str
    created_at_utc: datetime
    elapsed_ms: int | None = None
    dispatch_entered_ms: int | None = None
    rdbg_acknowledged_ms: int | None = None
    initiating_waiter_detached_ms: int | None = None
    last_poll_ms: int | None = None
    result_received_ms: int | None = None
    workspace_restored_ms: int | None = None
    outcome_published_ms: int | None = None
    remote_step_count: int = 0
    poll_count: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "evaluation_id", _identifier(self.evaluation_id))
        if not isinstance(self.created_at_utc, datetime):
            raise ValueError("created_at_utc must be a datetime")
        created = self.created_at_utc
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        else:
            created = created.astimezone(timezone.utc)
        object.__setattr__(self, "created_at_utc", created.replace(microsecond=0))
        for name in (
            "elapsed_ms",
            "dispatch_entered_ms",
            "rdbg_acknowledged_ms",
            "initiating_waiter_detached_ms",
            "last_poll_ms",
            "result_received_ms",
            "workspace_restored_ms",
            "outcome_published_ms",
        ):
            object.__setattr__(self, name, _bounded_offset(getattr(self, name), name=name))
        object.__setattr__(
            self,
            "remote_step_count",
            _bounded_counter(
                self.remote_step_count,
                name="remote_step_count",
                maximum=MAX_CAPTURE_TIMING_COUNT,
            ),
        )
        object.__setattr__(
            self,
            "poll_count",
            _bounded_counter(
                self.poll_count,
                name="poll_count",
                maximum=MAX_CAPTURE_TIMING_COUNT,
            ),
        )


@dataclass(frozen=True, slots=True)
class CaptureFailureDiagnostic:
    code: str
    message: str
    recommended_action: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "code",
            _safe_text(
                self.code,
                name="code",
                limit=MAX_CAPTURE_DIAGNOSTIC_CODEPOINTS,
                allow_empty=False,
            ),
        )
        object.__setattr__(
            self,
            "message",
            _safe_text(
                self.message,
                name="message",
                limit=MAX_CAPTURE_DIAGNOSTIC_MESSAGE_CODEPOINTS,
            ),
        )
        object.__setattr__(
            self,
            "recommended_action",
            _safe_text(
                self.recommended_action,
                name="recommended_action",
                limit=MAX_CAPTURE_DIAGNOSTIC_MESSAGE_CODEPOINTS,
            ),
        )


@dataclass(frozen=True, slots=True)
class CaptureStatus:
    operation_id: int
    capture_generation: int
    stop_sequence: int
    phase: CapturePhase
    pending_evaluation_id: str | None = None
    evaluation_kind: CaptureEvaluationKind | None = None
    last_evaluation_id: str | None = None
    last_user_evaluation_id: str | None = None
    evaluation_timing: CaptureEvaluationTiming | None = None
    failure: CaptureFailureDiagnostic | None = None

    def __post_init__(self) -> None:
        for name in ("operation_id", "capture_generation", "stop_sequence"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        object.__setattr__(self, "phase", _enum(self.phase, CapturePhase, name="phase"))
        for name in (
            "pending_evaluation_id",
            "last_evaluation_id",
            "last_user_evaluation_id",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _identifier(value, name=name))
        if self.pending_evaluation_id is None:
            if self.evaluation_kind is not None:
                raise ValueError("evaluation_kind requires pending_evaluation_id")
        else:
            if self.phase is not CapturePhase.EVALUATING:
                raise ValueError("pending_evaluation_id requires evaluating phase")
            object.__setattr__(
                self,
                "evaluation_kind",
                _enum(
                    self.evaluation_kind,
                    CaptureEvaluationKind,
                    name="evaluation_kind",
                ),
            )
        if self.phase is CapturePhase.EVALUATING and self.pending_evaluation_id is None:
            raise ValueError("evaluating phase requires pending_evaluation_id")
        if self.evaluation_timing is not None and not isinstance(
            self.evaluation_timing, CaptureEvaluationTiming
        ):
            raise ValueError("evaluation_timing is invalid")
        if self.failure is not None and not isinstance(
            self.failure, CaptureFailureDiagnostic
        ):
            raise ValueError("failure is invalid")
        if self.failure is not None and self.phase not in {
            CapturePhase.OUTCOME_UNKNOWN,
            CapturePhase.RECOVERY_REQUIRED,
        }:
            raise ValueError("failure requires a terminal failure phase")
        if self.evaluation_timing is not None:
            selected_id = self.pending_evaluation_id or self.last_evaluation_id
            if selected_id is None or self.evaluation_timing.evaluation_id != selected_id:
                raise ValueError("evaluation_timing does not match selected evaluation")
        if self.phase is CapturePhase.OUTCOME_UNKNOWN and self.last_evaluation_id is None:
            raise ValueError("outcome_unknown phase requires last_evaluation_id")

    @property
    def can_inspect(self) -> bool:
        return self.phase is CapturePhase.PAUSED

    @property
    def can_resume_capture(self) -> bool:
        return self.phase is CapturePhase.PAUSED

    @property
    def can_wait(self) -> bool:
        if self.phase is CapturePhase.STALE:
            return False
        if self.phase is CapturePhase.EVALUATING:
            return True
        if self.phase is CapturePhase.OUTCOME_UNKNOWN:
            return True
        return self.last_evaluation_id is not None and self.phase in {
            CapturePhase.PAUSED,
            CapturePhase.RESUMING,
            CapturePhase.RECOVERY_REQUIRED,
        }


@dataclass(frozen=True, slots=True, repr=False)
class CaptureEvaluationOutcome:
    evaluation_id: str
    evaluation_kind: CaptureEvaluationKind
    state: CaptureEvaluationState
    result: object = None
    messages: tuple[str, ...] = ()
    error: str | None = None
    diagnostic: CaptureFailureDiagnostic | None = None
    timing: CaptureEvaluationTiming | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "evaluation_id", _identifier(self.evaluation_id))
        object.__setattr__(
            self,
            "evaluation_kind",
            _enum(
                self.evaluation_kind,
                CaptureEvaluationKind,
                name="evaluation_kind",
            ),
        )
        object.__setattr__(
            self,
            "state",
            _enum(self.state, CaptureEvaluationState, name="state"),
        )
        if self.result is not None:
            if self.evaluation_kind in _INTERNAL_EVALUATION_KINDS:
                raise ValueError("internal evaluation outcomes cannot expose a result")
            object.__setattr__(self, "result", _public_result(self.result))
        if isinstance(self.messages, str) or not isinstance(self.messages, (tuple, list)):
            raise ValueError("messages must be a sequence of strings")
        raw_messages = tuple(self.messages)
        messages_truncated = len(raw_messages) > MAX_CAPTURE_MESSAGES
        messages = tuple(
            _safe_text(
                message,
                name="message",
                limit=MAX_CAPTURE_MESSAGE_CODEPOINTS,
            )
            for message in raw_messages[:MAX_CAPTURE_MESSAGES]
        )
        messages_truncated = messages_truncated or any(
            len(message) > MAX_CAPTURE_MESSAGE_CODEPOINTS
            for message in raw_messages[:MAX_CAPTURE_MESSAGES]
            if isinstance(message, str)
        )
        object.__setattr__(self, "messages", messages)
        if self.error is not None:
            object.__setattr__(
                self,
                "error",
                _safe_text(
                    self.error,
                    name="error",
                    limit=MAX_CAPTURE_DIAGNOSTIC_MESSAGE_CODEPOINTS,
                ),
            )
        if self.diagnostic is not None and not isinstance(
            self.diagnostic, CaptureFailureDiagnostic
        ):
            raise ValueError("diagnostic is invalid")
        if messages_truncated:
            object.__setattr__(
                self,
                "diagnostic",
                _message_truncation_diagnostic(self.diagnostic),
            )
        if self.timing is not None and not isinstance(self.timing, CaptureEvaluationTiming):
            raise ValueError("timing is invalid")
        if self.timing is not None and self.timing.evaluation_id != self.evaluation_id:
            raise ValueError("timing does not match evaluation_id")
        if self.state is CaptureEvaluationState.PENDING:
            if (
                self.result is not None
                or self.messages
                or self.error is not None
                or self.diagnostic is not None
            ):
                raise ValueError("pending outcome has incompatible state payload")
        elif self.state is CaptureEvaluationState.COMPLETED:
            if self.error is not None or (
                self.diagnostic is not None and not messages_truncated
            ):
                raise ValueError("completed outcome has incompatible state payload")
        elif self.state is CaptureEvaluationState.FAILED:
            if self.result is not None:
                raise ValueError("failed outcome has incompatible state payload")
        elif self.state is CaptureEvaluationState.UNKNOWN:
            if self.result is not None or self.error is not None:
                raise ValueError("unknown outcome has incompatible state payload")

    def __repr__(self) -> str:
        return (
            "CaptureEvaluationOutcome("
            f"evaluation_id={self.evaluation_id!r}, "
            f"evaluation_kind={self.evaluation_kind.value!r}, "
            f"state={self.state.value!r}, "
            f"result={'<present>' if self.result is not None else None!r}, "
            f"messages={len(self.messages)}, "
            f"error={'<present>' if self.error else None!r}, "
            f"diagnostic={'<present>' if self.diagnostic else None!r}, "
            f"timing={self.timing!r})"
        )


def _public_result(value: object) -> object:
    """Copy the narrow immutable result shape produced by evaluation_to_python."""

    if value is None or type(value) in {bool, int}:
        return value
    if type(value) is float:
        if not isfinite(value):
            raise ValueError("public result must be finite")
        return value
    if type(value) is str:
        # Result values have already passed the public-value admission guard.
        # Preserve that admitted scalar byte-for-byte, including controls and
        # length, so a late outcome is observationally identical to a direct
        # evaluation result.
        return value
    if type(value) is tuple:
        return tuple(_public_result(item) for item in value)
    raise ValueError("public result must be an immutable scalar or tuple")


def _message_truncation_diagnostic(
    existing: CaptureFailureDiagnostic | None,
) -> CaptureFailureDiagnostic:
    if existing is None:
        return CaptureFailureDiagnostic(
            code="messages_truncated",
            message=_MESSAGE_TRUNCATION_NOTE,
            recommended_action="inspect capture.wait()",
        )
    # Keep the existing stable code and action, while reserving enough room
    # for an explicit marker even when the original message used its bound.
    separator = " "
    available = max(
        0,
        MAX_CAPTURE_DIAGNOSTIC_MESSAGE_CODEPOINTS
        - len(separator)
        - len(_MESSAGE_TRUNCATION_NOTE),
    )
    message = existing.message[:available] + separator + _MESSAGE_TRUNCATION_NOTE
    return CaptureFailureDiagnostic(
        code=existing.code,
        message=message,
        recommended_action=existing.recommended_action,
    )


# Frontends import these immutable snapshots. The fence, request, ticket and
# coordinator below are controller-internal contracts, intentionally excluded.
__all__ = [
    "MAX_CAPTURE_DIAGNOSTIC_CODEPOINTS",
    "MAX_CAPTURE_DIAGNOSTIC_MESSAGE_CODEPOINTS",
    "MAX_CAPTURE_IDENTIFIER_CODEPOINTS",
    "MAX_CAPTURE_MESSAGE_CODEPOINTS",
    "MAX_CAPTURE_MESSAGES",
    "MAX_CAPTURE_TIMING_COUNT",
    "MAX_CAPTURE_TIMING_MS",
    "CaptureEvaluationKind",
    "CaptureEvaluationOutcome",
    "CaptureEvaluationState",
    "CaptureEvaluationTiming",
    "CaptureFailureDiagnostic",
    "CapturePhase",
    "CaptureStatus",
]


# Internal ownership types. None of these objects are exported by frontends.
# Remote-step plans will compose these callbacks on this same worker.
_MAX_POLL_PROGRESS_EVENTS = 16


def _nothing() -> None:
    pass


def _no_messages() -> tuple[str, ...]:
    return ()


def _no_pin(disposition: str) -> None:
    pass


@dataclass(frozen=True, slots=True)
class CaptureFence:
    operation_id: int
    capture_generation: int
    stop_sequence: int
    # The owner may bind runtime/target identity without publishing it.
    identity: object = field(default=None, repr=False)

    def __post_init__(self) -> None:
        for name in ("operation_id", "capture_generation", "stop_sequence"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class CaptureRemoteStep:
    """One private transport capability, consumed on the active record."""

    dispatch: Callable[[Callable[[], None]], PendingEvaluation] = field(repr=False)
    poll: Callable[[PendingEvaluation, float], EvaluationResult | StopEvent] = field(repr=False)
    restore: Callable[[], None] = field(default=_nothing, repr=False)


class _CapturePinDispositionState(StrEnum):
    UNCLAIMED = "unclaimed"
    IN_FLIGHT = "in_flight"
    SUCCEEDED = "succeeded"
    FAILED_OR_UNKNOWN = "failed_or_unknown"
    RETAINED = "retained"


class _CapturePinDispositionLease:
    """One physical pin outcome shared by worker and shutdown supervisor."""

    __slots__ = ("_callback", "_disposition", "_lock", "_state", "__weakref__")

    def __init__(self, callback: Callable[[str], None]) -> None:
        if not callable(callback):
            raise ValueError("pin disposition callback must be callable")
        self._callback = callback
        self._disposition: str | None = None
        self._lock = Lock()
        self._state = _CapturePinDispositionState.UNCLAIMED

    def retain(self) -> _CapturePinDispositionState:
        """Atomically transfer an unclaimed lease to supervised retention."""
        with self._lock:
            if self._state is _CapturePinDispositionState.UNCLAIMED:
                self._state = _CapturePinDispositionState.RETAINED
            return self._state

    def dispose(
        self,
        disposition: str,
        *,
        allow_retained: bool = False,
    ) -> _CapturePinDispositionState:
        if disposition not in {"release", "quarantine"}:
            raise ValueError("invalid CAPTURE pin disposition")
        with self._lock:
            if (
                self._disposition is not None
                and self._disposition != disposition
            ):
                return _CapturePinDispositionState.FAILED_OR_UNKNOWN
            if self._state in {
                _CapturePinDispositionState.SUCCEEDED,
                _CapturePinDispositionState.FAILED_OR_UNKNOWN,
                _CapturePinDispositionState.IN_FLIGHT,
            }:
                return self._state
            if (
                self._state is _CapturePinDispositionState.RETAINED
                and not allow_retained
            ):
                return self._state
            self._disposition = disposition
            self._state = _CapturePinDispositionState.IN_FLIGHT
        try:
            self._callback(disposition)
        except BaseException:
            with self._lock:
                self._state = _CapturePinDispositionState.FAILED_OR_UNKNOWN
            return _CapturePinDispositionState.FAILED_OR_UNKNOWN
        with self._lock:
            self._state = _CapturePinDispositionState.SUCCEEDED
            return self._state

    def __call__(self, disposition: str) -> None:
        outcome = self.dispose(disposition, allow_retained=True)
        if outcome is not _CapturePinDispositionState.SUCCEEDED:
            raise ProtocolError(
                "CAPTURE pin disposition could not be proven"
            ) from None


@dataclass(frozen=True, slots=True)
class CaptureCleanupLease:
    private_key: str = field(repr=False)
    cleanup_step: CaptureRemoteStep = field(repr=False)


@dataclass(frozen=True, slots=True)
class CaptureTransferPlan:
    """All temporary ownership is known before the creating instruction runs."""

    instruction: str = field(repr=False)
    private_key: str = field(repr=False)
    cleanup_instruction: str = field(repr=False)
    max_text_size: int
    decode: Callable[[object, str], bytes] = field(repr=False)

    def capture_request(
        self, fence: CaptureFence, *,
        step_factory: Callable[[str], CaptureRemoteStep],
        read: Callable[[CaptureStepContext, str, int], str],
        pin_lease: Callable[[str], None] = _no_pin,
    ) -> CaptureEvaluationRequest:
        first = step_factory(self.instruction)
        cleanup = CaptureCleanupLease(self.private_key, step_factory(self.cleanup_instruction))

        def continuation(context: CaptureStepContext, metadata: object) -> bytes:
            return self.decode(metadata, read(context, self.private_key, self.max_text_size))

        return CaptureEvaluationRequest(
            fence, CaptureEvaluationKind.MATERIALIZATION_HELPER,
            first.dispatch, first.poll, lambda result: result.presentation,
            restore=first.restore, pin_lease=pin_lease, cleanup_leases=(cleanup,),
            step_continuation=continuation,
        )


class _RemoteStepFailure(Exception):
    """Transport failure already classified by the capability owner."""

    def __init__(self, phase: CapturePhase, code: str, *, uncertain: bool = False):
        super().__init__(code)
        self.phase = phase
        self.code = code
        self.uncertain = uncertain


class _ShutdownStepSettled(Exception):
    """A remote step settled after close began; teardown owns disposition."""

    def __init__(self, disposition: str):
        super().__init__(disposition)
        self.disposition = disposition


@dataclass(frozen=True, slots=True, repr=False)
class CaptureStepContext:
    _coordinator: CaptureEvaluationCoordinator
    _record: _CaptureEvaluationRecord

    def execute_inline(self, step: CaptureRemoteStep) -> EvaluationResult:
        owner = self._coordinator
        if current_thread() is not owner._worker:
            raise ProtocolError("CAPTURE inline steps require the coordinator worker")
        with owner._condition:
            if owner._active is not self._record or self._record.outcome is not None:
                raise ProtocolError("CAPTURE inline steps require the active record")
            if self._record.step_in_progress or self._record.capability is not None:
                raise ProtocolError("CAPTURE record already owns a remote step")
            # Reserve the whole callback lifetime, including the gap before
            # dispatch returns its capability and after polling restores state.
            self._record.step_in_progress = True
        try:
            return owner._execute_remote_step(self._record, step)
        finally:
            with owner._condition:
                self._record.step_in_progress = False


@dataclass(frozen=True, slots=True)
class CaptureEvaluationRequest:
    """Transfer all callbacks and leases before any remote work can start.

    dispatch must call its argument immediately before entering transport. An
    exception before that marker proves there was no dispatch; after it, only
    a returned PendingEvaluation proves acceptance. Normal callbacks run
    outside the condition, exclusively on the worker. pin_lease receives
    'release' or 'quarantine'; cleanup callbacks remain owned on abnormal
    termination. After a proven shutdown join, the close caller owns pin
    disposition and any cleanup lease ``dispose_shutdown`` hook.

    result_policy admits/normalizes the private RDBG result. For user_bsl its
    returned value must also fit the public immutable scalar/tuple boundary.
    Internal results are delivered only through the initiating ticket.
    continuation is optional downstream work and is abandoned on detachment.
    initiator_error_policy may restore controller-owned diagnostic detail for
    that ticket only; public outcomes and journal evidence remain normalized.
    """

    fence: CaptureFence = field(repr=False)
    evaluation_kind: CaptureEvaluationKind
    dispatch: Callable[[Callable[[], None]], PendingEvaluation] = field(repr=False)
    poll: Callable[[PendingEvaluation, float], EvaluationResult | StopEvent] = field(repr=False)
    result_policy: Callable[[EvaluationResult], object] = field(repr=False)
    restore: Callable[[], None] = field(default=_nothing, repr=False)
    continuation: Callable[[object], object] | None = field(default=None, repr=False)
    seal_messages: Callable[[], tuple[str, ...]] = field(default=_no_messages, repr=False)
    pin_lease: _CapturePinDispositionLease | Callable[[str], None] = field(
        default=_no_pin,
        repr=False,
    )
    cleanup_leases: tuple[CaptureCleanupLease | Callable[[], None], ...] = field(default=(), repr=False)
    step_policy: Callable[[CaptureStepContext, EvaluationResult], object] | None = field(default=None, repr=False)
    step_continuation: Callable[[CaptureStepContext, object], object] | None = field(default=None, repr=False)
    completion: Callable[[object, BaseException | None], object] | None = field(default=None, repr=False)
    initiator_error_policy: Callable[[BaseException], BaseException] | None = field(
        default=None,
        repr=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.fence, CaptureFence):
            raise ValueError("capture fence is required")
        object.__setattr__(self, "evaluation_kind", _enum(
            self.evaluation_kind, CaptureEvaluationKind, name="evaluation_kind",
        ))
        object.__setattr__(self, "cleanup_leases", tuple(self.cleanup_leases))
        if not isinstance(self.pin_lease, _CapturePinDispositionLease):
            object.__setattr__(
                self,
                "pin_lease",
                _CapturePinDispositionLease(self.pin_lease),
            )
        callbacks = (
            self.dispatch, self.poll, self.result_policy, self.restore,
            self.seal_messages, self.pin_lease,
        )
        if not all(callable(callback) for callback in callbacks):
            raise ValueError("evaluation callbacks must be callable")
        if self.continuation is not None and not callable(self.continuation):
            raise ValueError("continuation must be callable")
        if any(not isinstance(lease, CaptureCleanupLease) and not callable(lease)
               for lease in self.cleanup_leases):
            raise ValueError("cleanup must be a lease or callable")
        for callback in (
            self.step_policy,
            self.step_continuation,
            self.completion,
            self.initiator_error_policy,
        ):
            if callback is not None and not callable(callback):
                raise ValueError("optional evaluation policies must be callable")


@dataclass(slots=True, repr=False)
class _CaptureEvaluationRecord:
    request: CaptureEvaluationRequest
    evaluation_id: str = field(default_factory=lambda: uuid4().hex)
    created: float = field(default_factory=monotonic)
    created_at_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    offsets: dict[str, int] = field(default_factory=dict)
    remote_step_count: int = 0
    poll_count: int = 0
    poll_events: int = 0
    capability: PendingEvaluation | None = None
    step_in_progress: bool = False
    dispatch_entered: bool = False
    acknowledged: bool = False
    initiator_attached: bool = True
    observed: bool = False
    continuation_started: bool = False
    cleanup_status: str = "not_started"
    settlement_order: int = 0
    outcome: CaptureEvaluationOutcome | None = None
    shutdown_pin_disposed: bool = False
    shutdown_cleanup_disposed: set[int] = field(default_factory=set)
    shutdown_evidence_published: bool = False
    # These never enter a public snapshot, repr, journal, or observer outcome.
    private_result: object = None
    initiating_error: BaseException | None = None


@dataclass(frozen=True, slots=True)
class CaptureEvaluationTicket:
    evaluation_id: str
    evaluation_kind: CaptureEvaluationKind
    _coordinator: CaptureEvaluationCoordinator = field(repr=False)
    _record: _CaptureEvaluationRecord = field(repr=False)

    def wait_initiator(self, timeout_s: float | None = None) -> object:
        """Deliver a private result/error; only this wait owns continuation.

        An acknowledged pending timeout raises CaptureEvaluationPendingError.
        A deadline during dispatch raises plain TimeoutError, since acceptance
        is not yet known. Both detach the initiator without cancelling work.
        KeyboardInterrupt propagates only on this caller's stack.
        """
        return self._coordinator._wait_initiator(self._record, timeout_s)


@dataclass(slots=True, repr=False)
class _CaptureSubmission:
    """Private receipt for one synchronous adapter-to-coordinator handoff.

    The receipt survives an interrupted ticket return. Its dynamic scope is
    only the local submission call, so adapters may wrap pin/completion
    callbacks without hiding acceptance. It never crosses the remote boundary.
    """

    ticket: CaptureEvaluationTicket | None = None

    def submit(self, adapter: Callable[..., CaptureEvaluationTicket], **kwargs: object) -> CaptureEvaluationTicket:
        if _capture_submission.get() is not None or self.ticket is not None:
            raise ProtocolError("CAPTURE submission handoff was already claimed")
        token = _capture_submission.set(self)
        try:
            return adapter(**kwargs)
        finally:
            _capture_submission.reset(token)

    def detach_initiator(self) -> None:
        ticket = self.ticket
        if ticket is not None:
            with ticket._coordinator._condition:
                ticket._coordinator._detach_locked(ticket._record, "submission_interrupted")


_capture_submission: ContextVar[_CaptureSubmission | None] = ContextVar(
    "capture_submission", default=None,
)


class CaptureEvaluationCoordinator:
    """One daemon event consumer and one active logical evaluation.

    The condition protects local records only. Dispatch, polling, restoration,
    policy, cleanup, pin disposition and journal callbacks run without it.
    Shutdown disposition runs on the close caller only after a proven join.
    begin_close wakes callers and rejects admission; the controller must then
    stop/invalidate its transport before bounded join and resource teardown.
    """

    def __init__(
        self,
        fence: CaptureFence,
        *,
        poll_interval_s: float = 0.1,
        journal: RecoveryJournal | None = None,
    ) -> None:
        if not isinstance(fence, CaptureFence):
            raise ValueError("capture fence is required")
        _timeout(poll_interval_s)
        if poll_interval_s <= 0 or poll_interval_s > 6:
            raise ValueError("poll interval must be positive and at most six seconds")
        self._fence = fence
        self._poll_interval_s = poll_interval_s
        self._journal = journal if journal is not None else RecoveryJournal()
        self._condition = Condition(Lock())
        self._disposition_lock = Lock()
        self._phase = CapturePhase.PAUSED
        self._failure: CaptureFailureDiagnostic | None = None
        self._active: _CaptureEvaluationRecord | None = None
        self._last_user: CaptureEvaluationOutcome | None = None
        self._last_internal: CaptureEvaluationOutcome | None = None
        self._last: CaptureEvaluationOutcome | None = None
        self._settlement_order = 0
        self._last_user_order = 0
        self._last_internal_order = 0
        self._last_order = 0
        self._quarantined: _CaptureEvaluationRecord | None = None
        self._closing = False
        self._shutdown_record: _CaptureEvaluationRecord | None = None
        self._shutdown_disposition: str | None = None
        self._shutdown_finalized = False
        self._shutdown_abandoned = False
        self._shutdown_publication: str | None = None
        self._events: deque[tuple[str, dict[str, object]]] = deque()
        self._worker = Thread(target=self._run, name="capture-evaluation-owner", daemon=True)
        self._worker.start()

    def submit_evaluation(self, request: CaptureEvaluationRequest) -> CaptureEvaluationTicket:
        if current_thread() is self._worker:
            raise ProtocolError("Recursive CAPTURE submission is prohibited")
        with self._condition:
            self._require_fence_locked(request.fence)
            if self._phase is CapturePhase.OUTCOME_UNKNOWN:
                raise CaptureOutcomeUnknownError(
                    self._last.evaluation_id if self._last is not None else None, self._failure,
                )
            if self._phase is CapturePhase.RECOVERY_REQUIRED:
                raise CaptureRecoveryRequiredError(self._failure)
            if self._active is not None:
                raise CaptureBusyError(
                    self._active.evaluation_id,
                    self._active.request.evaluation_kind,
                    self._phase,
                )
            record = _CaptureEvaluationRecord(request)
            ticket = CaptureEvaluationTicket(
                record.evaluation_id, request.evaluation_kind, self, record,
            )
            submission = _capture_submission.get()
            if submission is not None and submission.ticket is not None:
                raise ProtocolError("CAPTURE submission handoff was already claimed")
            try:
                self._active = record
                if submission is not None:
                    submission.ticket = ticket
                self._phase = CapturePhase.EVALUATING
                self._evidence_locked(record, "record_created")
                self._condition.notify_all()
                return ticket
            except BaseException:
                if self._active is record:
                    # Adoption, not a successful return, transfers ownership.
                    # Repair the receipt if interruption fell between these
                    # short assignments, before releasing the worker mutex.
                    if submission is not None:
                        submission.ticket = ticket
                    self._phase = CapturePhase.EVALUATING
                    record.initiator_attached = False
                    self._condition.notify_all()
                raise

    def status(self, fence: CaptureFence) -> CaptureStatus:
        with self._condition:
            if fence != self._fence or self._closing or self._phase is CapturePhase.STALE:
                return CaptureStatus(
                    fence.operation_id, fence.capture_generation, fence.stop_sequence,
                    CapturePhase.STALE,
                )
            timing = (
                self._timing_locked(self._active) if self._active is not None
                else self._last.timing if self._last is not None else None
            )
            return CaptureStatus(
                fence.operation_id, fence.capture_generation, fence.stop_sequence,
                self._phase,
                pending_evaluation_id=self._active.evaluation_id if self._active else None,
                evaluation_kind=self._active.request.evaluation_kind if self._active else None,
                last_evaluation_id=self._last.evaluation_id if self._last else None,
                last_user_evaluation_id=self._last_user.evaluation_id if self._last_user else None,
                evaluation_timing=timing,
                failure=self._failure,
            )

    def wait(
        self,
        fence: CaptureFence,
        evaluation_id: str | None = None,
        timeout_s: float | None = None,
    ) -> CaptureEvaluationOutcome:
        if current_thread() is self._worker:
            raise ProtocolError("CAPTURE worker cannot wait on its own outcome")
        deadline = _deadline(timeout_s)
        with self._condition:
            self._require_fence_locked(fence)
            record = self._select_locked(evaluation_id)
            if isinstance(record, CaptureEvaluationOutcome):
                return record
            record.observed = True
            # An interrupted observer simply exits this condition; it never
            # changes initiating waiter or continuation state.
            while record.outcome is None:
                self._require_fence_locked(fence)
                remaining = _remaining(deadline)
                if remaining is not None and remaining <= 0:
                    return CaptureEvaluationOutcome(
                        record.evaluation_id, record.request.evaluation_kind,
                        CaptureEvaluationState.PENDING, timing=self._timing_locked(record),
                    )
                self._condition.wait(remaining)
            self._require_fence_locked(fence)
            return record.outcome

    def begin_close(self) -> None:
        with self._condition:
            self._closing = True
            if self._active is not None:
                self._active.initiator_attached = False
            self._condition.notify_all()

    def join(self, timeout_s: float) -> bool:
        _timeout(timeout_s)
        if timeout_s is None:
            raise ValueError("join requires a finite deadline")
        if current_thread() is self._worker:
            raise ProtocolError("CAPTURE worker cannot join itself")
        self._worker.join(timeout_s)
        return not self._worker.is_alive()

    def finish_close(self, termination_proven: bool) -> None:
        """Dispose shutdown ownership once, after termination has been classified."""
        if type(termination_proven) is not bool:
            raise TypeError("termination_proven must be a Boolean")
        if termination_proven:
            with self._disposition_lock:
                self._finish_close_exclusive(True)
        else:
            # An unresponsive worker may own the disposition lock. Recording
            # supervised retention must remain bounded and never wait for it.
            self._finish_close_exclusive(False)

    def _finish_close_exclusive(self, termination_proven: bool) -> None:
        with self._condition:
            if self._shutdown_finalized:
                return
            if self._shutdown_publication is not None:
                return
            if termination_proven and self._worker.is_alive():
                raise ProtocolError(
                    "CAPTURE shutdown termination has not been proven"
                )
            record = self._shutdown_record or self._active
            if record is None:
                self._shutdown_finalized = True
                return
            elapsed_ms = self._offset(record)
            cleanup_count = len(record.request.cleanup_leases)
            if not termination_proven:
                self._shutdown_abandoned = True
                self._shutdown_record = record
                self._quarantined = record
                self._phase = CapturePhase.STALE
                self._failure = None
                pin_state = record.request.pin_lease.retain()
                if pin_state is not _CapturePinDispositionState.RETAINED:
                    self._condition.notify_all()
                    raise ProtocolError(
                        "CAPTURE shutdown disposition remains unresolved"
                    ) from None
                if self._active is record:
                    self._active = None
                self._condition.notify_all()
                event = "capture_evaluation_shutdown_abandoned"
                disposition = "retained"
            else:
                disposition = self._shutdown_disposition
                if disposition not in {"release", "quarantine"}:
                    raise ProtocolError(
                        "CAPTURE shutdown record has no terminal disposition"
                    )
                event = "capture_evaluation_shutdown_disposed"
            self._shutdown_publication = event

        if termination_proven:
            failed_resources = 0
            if not record.shutdown_pin_disposed:
                pin_state = record.request.pin_lease.dispose(
                    disposition,
                    allow_retained=True,
                )
                if pin_state is not _CapturePinDispositionState.SUCCEEDED:
                    failed_resources += 1
                else:
                    record.shutdown_pin_disposed = True
            for index, cleanup in enumerate(record.request.cleanup_leases):
                if index in record.shutdown_cleanup_disposed:
                    continue
                dispose = getattr(cleanup, "dispose_shutdown", None)
                try:
                    if callable(dispose):
                        dispose(disposition)
                except BaseException:
                    failed_resources += 1
                else:
                    record.shutdown_cleanup_disposed.add(index)
            if failed_resources:
                with self._condition:
                    if self._shutdown_publication == event:
                        self._shutdown_publication = None
                raise ProtocolError(
                    "CAPTURE shutdown disposition failed "
                    f"(failed_resources={failed_resources})"
                ) from None
        if not record.shutdown_evidence_published:
            try:
                self._journal.record(
                    "capture-evaluation.jsonl",
                    event,
                    evaluation_id=record.evaluation_id,
                    evaluation_kind=record.request.evaluation_kind.value,
                    termination_proven=termination_proven,
                    elapsed_ms=elapsed_ms,
                    pin_disposition=disposition,
                    cleanup_disposition=disposition,
                    cleanup_lease_count=cleanup_count,
                )
            except BaseException:
                with self._condition:
                    if self._shutdown_publication == event:
                        self._shutdown_publication = None
                raise ProtocolError(
                    "CAPTURE shutdown evidence could not be recorded"
                ) from None
            record.shutdown_evidence_published = True
        with self._condition:
            self._shutdown_finalized = True
            self._shutdown_publication = None
            if termination_proven:
                self._shutdown_record = None
                if disposition == "quarantine":
                    self._quarantined = record

    def _require_fence_locked(self, fence: CaptureFence) -> None:
        if fence != self._fence or self._closing or self._phase is CapturePhase.STALE:
            raise StaleCaptureError()

    def _select_locked(
        self, evaluation_id: str | None,
    ) -> _CaptureEvaluationRecord | CaptureEvaluationOutcome:
        if evaluation_id is None:
            selected = self._active or self._last
            if selected is not None:
                return selected
        else:
            for selected in (self._active, self._last_user, self._last_internal):
                if selected is not None and selected.evaluation_id == evaluation_id:
                    return selected
        raise NoCaptureEvaluationError(evaluation_id)

    def _wait_initiator(self, record: _CaptureEvaluationRecord, timeout_s: float | None) -> object:
        try:
            if current_thread() is self._worker:
                raise ProtocolError("CAPTURE worker cannot wait on its own outcome")
            deadline = _deadline(timeout_s)
            with self._condition:
                try:
                    while record.outcome is None:
                        self._require_fence_locked(record.request.fence)
                        remaining = _remaining(deadline)
                        if remaining is not None and remaining <= 0:
                            self._detach_locked(record, "timeout")
                            if record.acknowledged:
                                raise CaptureEvaluationPendingError(
                                    record.evaluation_id, record.request.evaluation_kind,
                                )
                            raise TimeoutError("Waiting for CAPTURE dispatch timed out; use capture.wait()")
                        self._condition.wait(remaining)
                    self._require_fence_locked(record.request.fence)
                    if record.initiating_error is not None:
                        raise record.initiating_error
                    return record.private_result
                except KeyboardInterrupt:
                    # Condition.wait() reacquires this mutex before raising.
                    # Abandon pending continuation before releasing it, so
                    # the owner cannot admit optional work in an unwind gap.
                    self._detach_locked(record, "interrupt")
                    raise
        except KeyboardInterrupt:
            # Setup and condition acquisition can both be interrupted. The
            # condition may never have been acquired, or publication may have
            # won the race before this handler reacquires it. Rehandling an
            # interrupt already detached inside the condition is idempotent.
            with self._condition:
                self._detach_locked(record, "interrupt")
            raise

    def _detach_locked(self, record: _CaptureEvaluationRecord, reason: str) -> None:
        if record.outcome is None and record.initiator_attached:
            record.initiator_attached = False
            self._evidence_locked(record, "initiating_waiter_detached", reason=reason)
            self._condition.notify_all()

    def _run(self) -> None:
        while True:
            with self._condition:
                while self._active is None and not self._closing:
                    self._condition.wait()
                if self._closing and self._active is None:
                    return
                record = self._active
            assert record is not None
            self._flush_evidence()
            self._execute(record)
            self._flush_evidence()
            # Public retention contains only immutable outcomes. Do not keep
            # the last request/temporary result alive in an idle worker frame.
            del record

    def _execute(self, record: _CaptureEvaluationRecord) -> None:
        request = record.request
        with self._condition:
            closing = self._closing
        if closing:
            self._defer_shutdown(record, "release")
            return

        context = CaptureStepContext(self, record)
        try:
            event = context.execute_inline(CaptureRemoteStep(
                request.dispatch, request.poll, request.restore,
            ))
        except _ShutdownStepSettled as settled:
            self._defer_shutdown(record, settled.disposition)
            return
        except _RemoteStepFailure as error:
            self._finish_remote_failure(record, error)
            return

        with self._condition:
            closing = self._closing
        if closing:
            self._defer_shutdown(record, "release")
            return

        value = None
        private_result = None
        diagnostic = None
        messages: tuple[str, ...] = ()
        state = CaptureEvaluationState.COMPLETED
        failure_category = "result_policy_failed"
        try:
            value = (request.step_policy(context, event) if request.step_policy is not None
                     else request.result_policy(event))
            if event.error_occurred:
                raise BslExecutionError("CAPTURE BSL evaluation failed")
            if request.evaluation_kind is CaptureEvaluationKind.USER_BSL:
                value = _public_result(value)
            private_result = value
            with self._condition:
                # This is the linearization point for starting optional work.
                run_continuation = record.initiator_attached and not self._closing and (request.continuation is not None or request.step_continuation is not None)
                record.continuation_started = run_continuation
            if run_continuation:
                failure_category = "continuation_failed"
                if request.step_continuation is not None:
                    private_result = request.step_continuation(context, value)
                else:
                    assert request.continuation is not None
                    private_result = request.continuation(value)
        except _ShutdownStepSettled as settled:
            self._defer_shutdown(record, settled.disposition)
            return
        except _RemoteStepFailure as error:
            if error.phase is not CapturePhase.PAUSED:
                self._finish_remote_failure(record, error, followup=True)
                return
            # The earlier creating step settled. A later local rejection has
            # no pending capability and must still drain its cleanup leases.
            state = CaptureEvaluationState.FAILED
            diagnostic = _diagnostic(error.code)
            value = None
            private_result = None
        except TargetLost:
            self._finish(record, CapturePhase.STALE, CaptureEvaluationState.FAILED,
                         diagnostic=_diagnostic("target_lost"), quarantine=True)
            return
        except BaseException:
            state = CaptureEvaluationState.FAILED
            code = "bsl_error" if event.error_occurred else failure_category
            diagnostic = _diagnostic(code)
            value = None
            private_result = None

        with self._condition:
            closing = self._closing
        if closing:
            self._defer_shutdown(record, "release")
            return
        try:
            for cleanup in request.cleanup_leases:
                if isinstance(cleanup, CaptureCleanupLease):
                    result = context.execute_inline(cleanup.cleanup_step)
                    if result.error_occurred:
                        raise BslExecutionError("CAPTURE required cleanup failed")
                else:
                    cleanup()
            record.cleanup_status = "completed"
        except _ShutdownStepSettled as settled:
            self._defer_shutdown(record, settled.disposition)
            return
        except _RemoteStepFailure as error:
            record.cleanup_status = "unknown" if error.uncertain else "failed"
            self._finish(record,
                         CapturePhase.STALE if error.phase is CapturePhase.STALE else CapturePhase.RECOVERY_REQUIRED,
                         CaptureEvaluationState.FAILED,
                         diagnostic=_diagnostic("target_lost" if error.phase is CapturePhase.STALE else
                                                "cleanup_uncertain" if error.uncertain else "cleanup_failed"),
                         quarantine=True)
            return
        except TargetLost:
            record.cleanup_status = "unknown"
            self._finish(record, CapturePhase.STALE, CaptureEvaluationState.FAILED,
                         diagnostic=_diagnostic("target_lost"), quarantine=True)
            return
        except BaseException as error:
            uncertain = isinstance(error, CommandTimeout)
            record.cleanup_status = "unknown" if uncertain else "failed"
            self._finish(record, CapturePhase.RECOVERY_REQUIRED, CaptureEvaluationState.FAILED,
                         diagnostic=_diagnostic("cleanup_uncertain" if uncertain else "cleanup_failed"),
                         quarantine=True)
            return
        try:
            messages = request.seal_messages()
            if isinstance(messages, (tuple, list)):
                messages = tuple(messages)
            # Validate/copy before publishing or releasing the ownership pin.
            candidate = CaptureEvaluationOutcome(
                record.evaluation_id, request.evaluation_kind, state,
                result=value if request.evaluation_kind is CaptureEvaluationKind.USER_BSL else None,
                messages=messages, error=diagnostic.message if diagnostic else None,
                diagnostic=diagnostic,
            )
        except BaseException:
            diagnostic = _diagnostic("result_delivery_failed")
            messages = ()
            candidate = CaptureEvaluationOutcome(
                record.evaluation_id, request.evaluation_kind, CaptureEvaluationState.FAILED,
                error=diagnostic.message, diagnostic=diagnostic,
            )
            private_result = None
        self._finish(record, CapturePhase.PAUSED, candidate.state, candidate=candidate,
                     private_result=private_result, diagnostic=diagnostic, messages=messages)

    def _execute_remote_step(
        self, record: _CaptureEvaluationRecord, step: CaptureRemoteStep,
    ) -> EvaluationResult:
        entered = False
        with self._condition:
            if self._closing:
                raise _ShutdownStepSettled("release")
            step_index = record.remote_step_count + 1

        def dispatch_entered() -> None:
            nonlocal entered
            if current_thread() is not self._worker:
                raise ProtocolError("Dispatch evidence must come from the CAPTURE worker")
            with self._condition:
                self._require_fence_locked(record.request.fence)
                if entered:
                    raise ProtocolError("CAPTURE dispatch was already entered")
                entered = True
                record.dispatch_entered = True
                record.remote_step_count = step_index
                self._evidence_locked(record, "dispatch_entered", step_index=step_index)
            self._flush_evidence()

        try:
            capability = step.dispatch(dispatch_entered)
            if not isinstance(capability, PendingEvaluation):
                raise ProtocolError("CAPTURE dispatch did not return a pending capability")
            with self._condition:
                record.capability = capability
                record.acknowledged = True
                self._evidence_locked(record, "rdbg_acknowledged", step_index=step_index)
            self._flush_evidence()
        except _RemoteStepFailure:
            raise
        except BreakpointWorkspaceOutcomeUnknown:
            raise _RemoteStepFailure(
                CapturePhase.OUTCOME_UNKNOWN,
                "workspace_shield_unknown",
                uncertain=True,
            ) from None
        except TargetLost:
            raise _RemoteStepFailure(CapturePhase.STALE, "target_lost", uncertain=True) from None
        except BaseException:
            raise _RemoteStepFailure(
                CapturePhase.OUTCOME_UNKNOWN if entered else CapturePhase.PAUSED,
                "dispatch_uncertain" if entered else "pre_dispatch_failed", uncertain=entered,
            ) from None

        while True:
            with self._condition:
                if self._closing:
                    raise _ShutdownStepSettled("quarantine")
            try:
                event = step.poll(capability, self._poll_interval_s)
            except CommandTimeout:
                self._poll_evidence(record)
                continue
            except _RemoteStepFailure:
                raise
            except TargetLost:
                raise _RemoteStepFailure(CapturePhase.STALE, "target_lost", uncertain=True) from None
            except BaseException as error:
                code = "unexpected_stop" if isinstance(error, UnexpectedStop) else "evaluation_stream_failed"
                raise _RemoteStepFailure(CapturePhase.RECOVERY_REQUIRED, code, uncertain=True) from None
            self._poll_evidence(record)
            if isinstance(event, StopEvent):
                raise _RemoteStepFailure(CapturePhase.RECOVERY_REQUIRED, "unexpected_stop", uncertain=True)
            if not isinstance(event, EvaluationResult) or event.result_id != capability.result_id:
                raise _RemoteStepFailure(CapturePhase.RECOVERY_REQUIRED, "evaluation_stream_failed", uncertain=True)
            with self._condition:
                record.capability = None
                self._evidence_locked(record, "result_received", step_index=step_index)
            self._flush_evidence()
            with self._condition:
                closing = self._closing
            if closing:
                raise _ShutdownStepSettled("release")
            break
        try:
            step.restore()
        except _RemoteStepFailure:
            raise
        except TargetLost:
            raise _RemoteStepFailure(CapturePhase.STALE, "target_lost", uncertain=True) from None
        except BaseException:
            raise _RemoteStepFailure(CapturePhase.RECOVERY_REQUIRED, "workspace_restore_failed") from None
        with self._condition:
            self._evidence_locked(record, "workspace_restored", step_index=step_index)
        self._flush_evidence()
        return event

    def _finish_remote_failure(
        self, record: _CaptureEvaluationRecord, error: _RemoteStepFailure,
        *, followup: bool = False,
    ) -> None:
        phase = error.phase
        if followup and phase is CapturePhase.OUTCOME_UNKNOWN:
            phase = CapturePhase.RECOVERY_REQUIRED
        self._finish(record, phase,
                     CaptureEvaluationState.UNKNOWN if phase is CapturePhase.OUTCOME_UNKNOWN else CaptureEvaluationState.FAILED,
                     diagnostic=_diagnostic(error.code), quarantine=phase is not CapturePhase.PAUSED)

    def _finish(
        self,
        record: _CaptureEvaluationRecord,
        phase: CapturePhase,
        state: CaptureEvaluationState,
        *,
        diagnostic: CaptureFailureDiagnostic | None = None,
        quarantine: bool = False,
        private_result: object = None,
        candidate: CaptureEvaluationOutcome | None = None,
        messages: tuple[str, ...] = (),
    ) -> None:
        with self._disposition_lock:
            self._finish_exclusive(
                record,
                phase,
                state,
                diagnostic=diagnostic,
                quarantine=quarantine,
                private_result=private_result,
                candidate=candidate,
                messages=messages,
            )
        self._flush_evidence()

    def _finish_exclusive(
        self,
        record: _CaptureEvaluationRecord,
        phase: CapturePhase,
        state: CaptureEvaluationState,
        *,
        diagnostic: CaptureFailureDiagnostic | None = None,
        quarantine: bool = False,
        private_result: object = None,
        candidate: CaptureEvaluationOutcome | None = None,
        messages: tuple[str, ...] = (),
    ) -> None:
        with self._condition:
            closing = self._closing
        if closing:
            self._defer_shutdown(
                record,
                "quarantine" if quarantine else "release",
            )
            return
        # All leases are disposed outside the condition, before outcome visibility.
        if record.request.completion is not None:
            provisional = candidate or CaptureEvaluationOutcome(
                record.evaluation_id, record.request.evaluation_kind, state,
                diagnostic=diagnostic,
            )
            try:
                private_result = record.request.completion(
                    private_result,
                    _initiating_failure(record.evaluation_id, phase, provisional),
                )
            except BaseException:
                private_result = None
                if phase is CapturePhase.PAUSED:
                    state = CaptureEvaluationState.FAILED
                    diagnostic = _diagnostic("result_delivery_failed")
                    candidate = None
                # Local delivery cannot resolve an unknown remote outcome or
                # replace the primary recovery/stale lifecycle diagnostic.
        with self._condition:
            shutdown_claimed = (
                self._closing
                or self._shutdown_abandoned
                or self._active is not record
            )
        if shutdown_claimed:
            self._defer_shutdown(
                record,
                "quarantine" if quarantine else "release",
            )
            return
        disposition = "quarantine" if quarantine else "release"
        pin_state = record.request.pin_lease.dispose(disposition)
        if pin_state is _CapturePinDispositionState.FAILED_OR_UNKNOWN:
            quarantine = True
            phase = CapturePhase.RECOVERY_REQUIRED
            state = CaptureEvaluationState.FAILED
            diagnostic = _diagnostic("pin_disposition_failed")
            candidate = None
        with self._condition:
            shutdown_claimed = (
                self._closing
                or self._shutdown_abandoned
                or self._active is not record
                or pin_state in {
                    _CapturePinDispositionState.IN_FLIGHT,
                    _CapturePinDispositionState.RETAINED,
                }
            )
        if shutdown_claimed:
            self._defer_shutdown(
                record,
                "quarantine" if quarantine else disposition,
            )
            return
        if candidate is None:
            candidate = CaptureEvaluationOutcome(
                record.evaluation_id, record.request.evaluation_kind, state,
                diagnostic=diagnostic,
                error=diagnostic.message if diagnostic and state is CaptureEvaluationState.FAILED else None,
            )
        initiating_error = self._initiator_failure(record, phase, candidate)
        with self._condition:
            record.private_result = private_result
            record.initiating_error = initiating_error
            if quarantine:
                self._quarantined = record
            self._evidence_locked(record, "outcome_published", state=state.value,
                                  cleanup_status=record.cleanup_status,
                                  error_category=diagnostic.code if diagnostic else (
                                      candidate.diagnostic.code if candidate.diagnostic else None))
            timing = self._timing_locked(record)
            # Terminal timing, waiter state, outcome, retention and admission
            # become visible at one condition boundary. A delivery-time
            # interrupt after this point cannot retroactively detach a waiter.
            # Reapply the original sealed message snapshot, not an already
            # truncated payload with a completed-state diagnostic. This keeps
            # the immutable model's strict state validation intact.
            outcome = replace(candidate, timing=timing, messages=messages, diagnostic=diagnostic)
            record.outcome = outcome
            self._settlement_order += 1
            record.settlement_order = self._settlement_order
            self._retain_locked(record)
            self._phase = CapturePhase.STALE if self._closing else phase
            self._failure = diagnostic if self._phase in {
                CapturePhase.OUTCOME_UNKNOWN, CapturePhase.RECOVERY_REQUIRED,
            } else None
            self._active = None
            self._condition.notify_all()
        # Evidence was queued in the same critical section as publication.
        # The wrapper drains it after releasing disposition admission, so
        # begin_close never waits behind journal I/O.

    def _defer_shutdown(
        self,
        record: _CaptureEvaluationRecord,
        disposition: str,
    ) -> None:
        if disposition not in {"release", "quarantine"}:
            raise ValueError("invalid CAPTURE shutdown disposition")
        with self._condition:
            if self._active is not record:
                return
            record.initiator_attached = False
            self._shutdown_record = record
            self._shutdown_disposition = disposition
            if self._shutdown_abandoned:
                self._quarantined = record
            self._phase = CapturePhase.STALE
            self._failure = None
            self._active = None
            self._condition.notify_all()

    @staticmethod
    def _initiator_failure(
        record: _CaptureEvaluationRecord,
        phase: CapturePhase,
        outcome: CaptureEvaluationOutcome,
    ) -> BaseException | None:
        error = _initiating_failure(record.evaluation_id, phase, outcome)
        policy = record.request.initiator_error_policy
        if error is None or policy is None:
            return error
        try:
            replacement = policy(error)
        except BaseException:
            return error
        return replacement if isinstance(replacement, BaseException) else error

    def _retain_locked(self, record: _CaptureEvaluationRecord) -> None:
        assert record.outcome is not None
        assert record.settlement_order > 0
        if record.request.evaluation_kind is CaptureEvaluationKind.USER_BSL:
            if record.settlement_order <= self._last_user_order:
                return
            self._last_user = record.outcome
            self._last_user_order = record.settlement_order
        elif record.observed or not record.initiator_attached or record.outcome.state is not CaptureEvaluationState.COMPLETED:
            if record.settlement_order <= self._last_internal_order:
                return
            self._last_internal = record.outcome
            self._last_internal_order = record.settlement_order
        else:
            return
        if record.settlement_order > self._last_order:
            self._last = record.outcome
            self._last_order = record.settlement_order

    def _timing_locked(self, record: _CaptureEvaluationRecord) -> CaptureEvaluationTiming:
        elapsed = record.offsets.get("outcome_published_ms", self._offset(record))
        return CaptureEvaluationTiming(
            record.evaluation_id, record.created_at_utc, elapsed_ms=elapsed,
            remote_step_count=record.remote_step_count, poll_count=record.poll_count,
            **record.offsets,
        )

    @staticmethod
    def _offset(record: _CaptureEvaluationRecord) -> int:
        return min(MAX_CAPTURE_TIMING_MS, max(0, int((monotonic() - record.created) * 1000)))

    def _evidence_locked(self, record: _CaptureEvaluationRecord, event: str, **extra: object) -> None:
        offset = self._offset(record)
        if event != "record_created" and event != "poll_progress":
            record.offsets.setdefault(f"{event}_ms", offset)
        fields: dict[str, object] = {
            "evaluation_id": record.evaluation_id,
            "evaluation_kind": record.request.evaluation_kind.value,
            "operation_id": record.request.fence.operation_id,
            "capture_generation": record.request.fence.capture_generation,
            "stop_sequence": record.request.fence.stop_sequence,
            "elapsed_ms": offset,
            "poll_count": record.poll_count,
            "remote_step_count": record.remote_step_count,
            "state": record.outcome.state.value if record.outcome else "pending",
            **record.offsets,
            **extra,
        }
        self._events.append((f"capture_evaluation_{event}", fields))

    def _poll_evidence(self, record: _CaptureEvaluationRecord) -> None:
        with self._condition:
            record.poll_count = min(MAX_CAPTURE_TIMING_COUNT, record.poll_count + 1)
            record.offsets["last_poll_ms"] = self._offset(record)
            if record.poll_events < _MAX_POLL_PROGRESS_EVENTS:
                record.poll_events += 1
                self._evidence_locked(record, "poll_progress")
        self._flush_evidence()

    def _flush_evidence(self) -> None:
        # The worker alone drains this bounded per-record event queue. No file
        # sink is flushed here: the owning controller may flush its journal.
        with self._condition:
            events = tuple(self._events)
            self._events.clear()
        for event, fields in events:
            self._journal.record("capture-evaluation.jsonl", event, **fields)


def _timeout(timeout_s: float | None) -> None:
    if timeout_s is not None and (
        type(timeout_s) not in {int, float} or not isfinite(timeout_s) or timeout_s < 0
    ):
        raise ValueError("timeout must be a finite non-negative number or None")


def _deadline(timeout_s: float | None) -> float | None:
    _timeout(timeout_s)
    return None if timeout_s is None else monotonic() + timeout_s


def _remaining(deadline: float | None) -> float | None:
    return None if deadline is None else deadline - monotonic()


def _diagnostic(code: str) -> CaptureFailureDiagnostic:
    # Never derive text or category from a callback's exception or RDBG payload.
    messages = {
        "pre_dispatch_failed": "CAPTURE evaluation failed before transport dispatch.",
        "dispatch_uncertain": "CAPTURE dispatch acceptance could not be established.",
        "bsl_error": "CAPTURE BSL evaluation failed.",
        "result_policy_failed": "CAPTURE result policy failed.",
        "result_delivery_failed": "CAPTURE result delivery failed.",
        "continuation_failed": "CAPTURE downstream delivery failed.",
        "workspace_restore_failed": "CAPTURE workspace restoration failed.",
        "workspace_shield_unknown": "CAPTURE workspace shielding could not be confirmed.",
        "cleanup_failed": "CAPTURE required cleanup failed.",
        "cleanup_uncertain": "CAPTURE required cleanup could not be confirmed.",
        "unexpected_stop": "An unexpected debugger stop interrupted CAPTURE evaluation.",
        "evaluation_stream_failed": "CAPTURE evaluation event consumption failed.",
        "target_lost": "The CAPTURE debugger target is no longer available.",
        "coordinator_closed": "The CAPTURE coordinator is closing.",
        "pin_disposition_failed": "CAPTURE generation pin disposition failed.",
    }
    normal_failure = code in {
        "pre_dispatch_failed", "bsl_error", "result_policy_failed",
        "result_delivery_failed", "continuation_failed",
    }
    return CaptureFailureDiagnostic(
        code, messages[code], "inspect capture.wait()" if normal_failure else "close and restart the runtime",
    )


def _initiating_failure(
    evaluation_id: str, phase: CapturePhase, outcome: CaptureEvaluationOutcome,
) -> BaseException | None:
    diagnostic = outcome.diagnostic
    if phase is CapturePhase.STALE:
        return StaleCaptureError()
    if phase is CapturePhase.OUTCOME_UNKNOWN:
        return CaptureOutcomeUnknownError(evaluation_id, diagnostic)
    if phase is CapturePhase.RECOVERY_REQUIRED:
        return CaptureRecoveryRequiredError(diagnostic)
    if outcome.state is not CaptureEvaluationState.FAILED:
        return None
    assert diagnostic is not None
    if diagnostic.code == "bsl_error":
        return BslExecutionError(diagnostic.message, messages=outcome.messages)
    if diagnostic.code == "pre_dispatch_failed":
        return ProtocolError(diagnostic.message)
    return CaptureEvaluationDeliveryError(diagnostic)
