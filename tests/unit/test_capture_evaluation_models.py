from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
from datetime import datetime, timezone

import pytest

from onec_runtime.capture_evaluation import (
    MAX_CAPTURE_TIMING_COUNT,
    MAX_CAPTURE_TIMING_MS,
    CaptureEvaluationKind,
    CaptureEvaluationOutcome,
    CaptureEvaluationState,
    CaptureEvaluationTiming,
    CaptureFailureDiagnostic,
    CapturePhase,
    CaptureStatus,
)
from onec_runtime.errors import (
    CaptureBusyError,
    CaptureEvaluationPendingError,
    CaptureOutcomeUnknownError,
    CaptureRecoveryRequiredError,
    NoActiveCaptureError,
    NoCaptureEvaluationError,
    StaleCaptureError,
)


def test_safe_enums_have_the_public_wire_values() -> None:
    assert [phase.value for phase in CapturePhase] == [
        "paused",
        "evaluating",
        "resuming",
        "recovery_required",
        "outcome_unknown",
        "stale",
    ]
    assert {kind.value for kind in CaptureEvaluationKind} == {
        "user_bsl",
        "public_value_guard",
        "inspection",
        "materialization_helper",
    }
    assert {state.value for state in CaptureEvaluationState} == {
        "pending",
        "completed",
        "failed",
        "unknown",
    }


def test_timing_is_bounded_rounded_and_does_not_retain_private_data() -> None:
    timing = CaptureEvaluationTiming(
        evaluation_id="eval-1",
        created_at_utc=datetime(2026, 9, 15, 10, 11, 12, 999999),
        elapsed_ms=MAX_CAPTURE_TIMING_MS + 1,
        dispatch_entered_ms=1,
        rdbg_acknowledged_ms=2,
        initiating_waiter_detached_ms=None,
        last_poll_ms=MAX_CAPTURE_TIMING_MS + 100,
        result_received_ms=3,
        workspace_restored_ms=4,
        outcome_published_ms=5,
        remote_step_count=MAX_CAPTURE_TIMING_COUNT + 1,
        poll_count=MAX_CAPTURE_TIMING_COUNT + 2,
    )

    assert timing.created_at_utc == datetime(2026, 9, 15, 10, 11, 12, tzinfo=timezone.utc)
    assert timing.elapsed_ms == MAX_CAPTURE_TIMING_MS
    assert timing.dispatch_entered_ms == 1
    assert timing.last_poll_ms == MAX_CAPTURE_TIMING_MS
    assert timing.remote_step_count == MAX_CAPTURE_TIMING_COUNT
    assert timing.poll_count == MAX_CAPTURE_TIMING_COUNT
    assert "private" not in repr(timing).casefold()
    assert "handle" not in repr(timing).casefold()
    assert all(not name.startswith("_") for name in (field.name for field in fields(timing)))


def test_timing_rejects_invalid_types() -> None:
    with pytest.raises(ValueError, match="evaluation_id"):
        CaptureEvaluationTiming(evaluation_id="", created_at_utc=datetime.now(timezone.utc))
    with pytest.raises(ValueError, match="created_at_utc"):
        CaptureEvaluationTiming(evaluation_id="eval-1", created_at_utc="secret")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="dispatch_entered_ms"):
        CaptureEvaluationTiming(
            evaluation_id="eval-1",
            created_at_utc=datetime.now(timezone.utc),
            dispatch_entered_ms=-1,
        )


def test_failure_diagnostic_is_frozen_and_sanitized() -> None:
    diagnostic = CaptureFailureDiagnostic(
        code="controller\nfailed",
        message="a\x00bounded\r\nmessage",
        recommended_action="inspect\tstatus",
    )

    assert diagnostic.code == "controller failed"
    assert diagnostic.message == "a bounded message"
    assert diagnostic.recommended_action == "inspect status"
    with pytest.raises(FrozenInstanceError):
        diagnostic.code = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("phase", "can_inspect", "can_resume", "can_wait"),
    [
        (CapturePhase.PAUSED, True, True, False),
        (CapturePhase.EVALUATING, False, False, True),
        (CapturePhase.RESUMING, False, False, False),
        (CapturePhase.OUTCOME_UNKNOWN, False, False, True),
        (CapturePhase.RECOVERY_REQUIRED, False, False, False),
        (CapturePhase.STALE, False, False, False),
    ],
)
def test_status_capabilities_follow_phase_and_retained_outcome(
    phase: CapturePhase,
    can_inspect: bool,
    can_resume: bool,
    can_wait: bool,
) -> None:
    status = CaptureStatus(
        operation_id=7,
        capture_generation=3,
        stop_sequence=11,
        phase=phase,
        pending_evaluation_id=("eval-pending" if phase is CapturePhase.EVALUATING else None),
        evaluation_kind=(
            CaptureEvaluationKind.INSPECTION
            if phase is CapturePhase.EVALUATING
            else None
        ),
    )

    assert status.can_inspect is can_inspect
    assert status.can_resume_capture is can_resume
    assert status.can_wait is can_wait

    retained = CaptureStatus(
        operation_id=7,
        capture_generation=3,
        stop_sequence=11,
        phase=phase,
        last_evaluation_id=("eval-last" if phase is not CapturePhase.STALE else None),
    )
    assert retained.can_wait is (
        phase is CapturePhase.EVALUATING
        or phase in {
            CapturePhase.PAUSED,
            CapturePhase.RESUMING,
            CapturePhase.OUTCOME_UNKNOWN,
            CapturePhase.RECOVERY_REQUIRED,
        }
    )


def test_outcome_normalizes_messages_and_is_immutable() -> None:
    outcome = CaptureEvaluationOutcome(
        evaluation_id="eval-1",
        evaluation_kind=CaptureEvaluationKind.USER_BSL,
        state=CaptureEvaluationState.FAILED,
        messages=["ok", "bad\x00message"],
        error="raw\r\nerror",
    )

    assert outcome.messages == ("ok", "bad message")
    assert outcome.error == "raw error"
    assert isinstance(outcome.messages, tuple)
    with pytest.raises(FrozenInstanceError):
        outcome.state = CaptureEvaluationState.COMPLETED  # type: ignore[misc]
    assert "raw" not in repr(outcome)


def test_pending_and_busy_errors_expose_only_safe_evaluation_facts() -> None:
    pending = CaptureEvaluationPendingError("eval-1", CaptureEvaluationKind.PUBLIC_VALUE_GUARD)
    busy = CaptureBusyError(
        "eval-1", CaptureEvaluationKind.PUBLIC_VALUE_GUARD, CapturePhase.EVALUATING
    )

    assert pending.evaluation_id == "eval-1"
    assert pending.evaluation_kind is CaptureEvaluationKind.PUBLIC_VALUE_GUARD
    assert busy.evaluation_id == "eval-1"
    assert busy.evaluation_kind is CaptureEvaluationKind.PUBLIC_VALUE_GUARD
    assert busy.phase is CapturePhase.EVALUATING
    for error in (pending, busy):
        rendered = repr(error) + str(error)
        assert "request" not in rendered.casefold()
        assert "handle" not in rendered.casefold()
        assert "source" not in rendered.casefold()


@pytest.mark.parametrize(
    "error_type",
    [
        CaptureOutcomeUnknownError,
        CaptureRecoveryRequiredError,
        NoActiveCaptureError,
        NoCaptureEvaluationError,
        StaleCaptureError,
    ],
)
def test_public_lifecycle_errors_are_typed_and_bounded(error_type: type[Exception]) -> None:
    error = error_type("diagnostic\x00message")
    assert isinstance(error, Exception)
    assert "\x00" not in str(error)
    assert len(str(error)) <= 1024
