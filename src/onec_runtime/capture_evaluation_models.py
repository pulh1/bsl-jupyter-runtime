"""Public immutable CAPTURE snapshots, validation, and redaction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from math import isfinite
import re
from typing import TypeVar


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
_PUBLIC_EVALUATION_ID_PATTERN = re.compile(
    r"\Acapture-eval-v1-[0-9a-f]{32}\Z"
)


def is_public_capture_evaluation_id(value: object) -> bool:
    """Whether ``value`` has the public evaluation receipt grammar.

    RDBG expression-result IDs are UUIDs, while Worker and value-transfer
    handles use private grammars. The public namespace is deliberately tagged,
    so a frontend can reject those private identifiers by validating this
    exact grammar.
    """

    return (
        type(value) is str
        and _PUBLIC_EVALUATION_ID_PATTERN.fullmatch(value) is not None
    )


class CapturePhase(StrEnum):
    PAUSED = "paused"
    EVALUATING = "evaluating"
    RESUMING = "resuming"
    RECOVERY_REQUIRED = "recovery_required"
    OUTCOME_UNKNOWN = "outcome_unknown"
    STALE = "stale"


class CaptureEvaluationKind(StrEnum):
    USER_BSL = "user_bsl"
    INSPECTION = "inspection"
    MATERIALIZATION_HELPER = "materialization_helper"


class CaptureEvaluationState(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    UNKNOWN = "unknown"


_INTERNAL_EVALUATION_KINDS = frozenset(
    {
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
    inspection_available: bool = True

    def __post_init__(self) -> None:
        for name in ("operation_id", "capture_generation", "stop_sequence"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        object.__setattr__(self, "phase", _enum(self.phase, CapturePhase, name="phase"))
        if type(self.inspection_available) is not bool:
            raise ValueError("inspection_available must be a boolean")
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
            if self.phase not in {
                CapturePhase.EVALUATING,
                CapturePhase.OUTCOME_UNKNOWN,
            }:
                raise ValueError("pending_evaluation_id requires an active evaluation phase")
            if self.phase is CapturePhase.OUTCOME_UNKNOWN and self.failure is None:
                raise ValueError(
                    "pending_evaluation_id requires failure during outcome_unknown"
                )
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
        if (
            self.phase is CapturePhase.OUTCOME_UNKNOWN
            and self.pending_evaluation_id is None
            and self.last_evaluation_id is None
            and self.failure is None
        ):
            raise ValueError("outcome_unknown phase requires last_evaluation_id or failure")

    @property
    def can_inspect(self) -> bool:
        return self.phase is CapturePhase.PAUSED and self.inspection_available

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


__all__ = [
    "MAX_CAPTURE_DIAGNOSTIC_CODEPOINTS",
    "MAX_CAPTURE_DIAGNOSTIC_MESSAGE_CODEPOINTS",
    "MAX_CAPTURE_IDENTIFIER_CODEPOINTS",
    "MAX_CAPTURE_MESSAGE_CODEPOINTS",
    "MAX_CAPTURE_MESSAGES",
    "MAX_CAPTURE_TIMING_COUNT",
    "MAX_CAPTURE_TIMING_MS",
    "is_public_capture_evaluation_id",
    "CaptureEvaluationKind",
    "CaptureEvaluationOutcome",
    "CaptureEvaluationState",
    "CaptureEvaluationTiming",
    "CaptureFailureDiagnostic",
    "CapturePhase",
    "CaptureStatus",
]
