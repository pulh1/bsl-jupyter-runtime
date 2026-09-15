"""Immutable, privacy-safe snapshots for the CAPTURE control plane.

The objects in this module are deliberately data-only.  They can be retained
by a caller after a capture has become stale and their representations never
include debugger requests, handles, source, or raw exceptions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


# These limits are part of the public safety boundary.  Timing is intentionally
# coarse and bounded; it is evidence about controller progress, not a trace.
MAX_CAPTURE_TIMING_MS = 86_400_000
MAX_CAPTURE_TIMING_COUNT = 1_000_000
MAX_CAPTURE_DIAGNOSTIC_CODEPOINTS = 256
MAX_CAPTURE_DIAGNOSTIC_MESSAGE_CODEPOINTS = 1_024
MAX_CAPTURE_MESSAGES = 100
MAX_CAPTURE_MESSAGE_CODEPOINTS = 1_024
MAX_CAPTURE_IDENTIFIER_CODEPOINTS = 256


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


def _enum(value: object, enum_type: type[StrEnum], *, name: str) -> Any:
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
            object.__setattr__(
                self,
                "evaluation_kind",
                _enum(
                    self.evaluation_kind,
                    CaptureEvaluationKind,
                    name="evaluation_kind",
                ),
            )
        if self.evaluation_timing is not None and not isinstance(
            self.evaluation_timing, CaptureEvaluationTiming
        ):
            raise ValueError("evaluation_timing is invalid")
        if self.failure is not None and not isinstance(
            self.failure, CaptureFailureDiagnostic
        ):
            raise ValueError("failure is invalid")

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
    result: Any = None
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
        if isinstance(self.messages, str) or not isinstance(self.messages, (tuple, list)):
            raise ValueError("messages must be a sequence of strings")
        messages = tuple(
            _safe_text(
                message,
                name="message",
                limit=MAX_CAPTURE_MESSAGE_CODEPOINTS,
            )
            for message in self.messages[:MAX_CAPTURE_MESSAGES]
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
        if self.timing is not None and not isinstance(self.timing, CaptureEvaluationTiming):
            raise ValueError("timing is invalid")

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
