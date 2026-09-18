from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
from datetime import datetime, timezone
from typing import get_type_hints

import pytest

import onec_runtime.capture_evaluation as capture_evaluation
from onec_runtime.capture_evaluation import (
    AdmissionEnvelopeV1,
    MAX_CAPTURE_TIMING_COUNT,
    MAX_CAPTURE_TIMING_MS,
    CaptureEvaluationKind,
    CaptureEvaluationOutcome,
    CaptureEvaluationState,
    CaptureEvaluationTiming,
    CaptureFailureDiagnostic,
    CapturePhase,
    CaptureStatus,
    _enum,
    is_public_capture_evaluation_id,
)
from onec_runtime.errors import (
    CaptureBusyError,
    CaptureEvaluationDeliveryError,
    CaptureEvaluationPendingError,
    CaptureOutcomeUnknownError,
    CaptureRecoveryRequiredError,
    CaptureValueAccessDeniedError,
    CaptureValueCheckError,
    NoActiveCaptureError,
    NoCaptureEvaluationError,
    ProtocolError,
    StaleCaptureError,
)


def test_admission_envelope_v1_round_trips_the_only_ready_shape() -> None:
    envelope = AdmissionEnvelopeV1(
        runtime_generation=3,
        context_generation=5,
        payload_bytes=33,
        payload_sha256="a" * 64,
        base64_chars=44,
    )

    encoded = envelope.encode()

    assert encoded == f"R|3|5|33|{'a' * 64}|44"
    assert AdmissionEnvelopeV1.parse(
        encoded,
        max_payload_bytes=33,
        max_base64_chars=44,
    ) == envelope


def test_public_capture_models_keep_supported_import_identity_and_redaction() -> None:
    from onec_runtime.capture_evaluation_models import (
        CaptureEvaluationOutcome as OutcomeModel,
        CaptureStatus as StatusModel,
    )
    from onec_runtime.capture_transfer_models import (
        AdmissionEnvelopeV1 as EnvelopeModel,
        CaptureFence,
        CaptureTransferPlan,
    )

    assert OutcomeModel is CaptureEvaluationOutcome
    assert StatusModel is CaptureStatus
    assert EnvelopeModel is AdmissionEnvelopeV1
    fence = CaptureFence(1, 2, 3, identity="private target")
    plan = CaptureTransferPlan(
        "private instruction", "private key", "private cleanup", 128,
        lambda metadata, payload: b"ok",
    )
    assert "private target" not in repr(fence)
    assert "private instruction" not in repr(plan)
    assert "private key" not in repr(plan)


def test_delivery_error_bounds_and_sanitizes_its_diagnostic() -> None:
    error = CaptureEvaluationDeliveryError("delivery\n" + "x" * 3000)
    assert isinstance(error, ProtocolError)
    assert len(str(error)) <= 1024
    assert "\n" not in str(error)
    assert error.diagnostic.code == "result_delivery_failed"
    assert len(error.diagnostic.message) <= 1024


@pytest.mark.parametrize(
    ("encoded", "error_type"),
    [
        ("D|worker_generation_value", CaptureValueAccessDeniedError),
        ("E|value_admission_failed", CaptureValueCheckError),
    ],
)
def test_admission_envelope_v1_maps_closed_nonready_tags(
    encoded: str, error_type: type[Exception]
) -> None:
    with pytest.raises(error_type) as caught:
        AdmissionEnvelopeV1.parse(
            encoded,
            max_payload_bytes=1,
            max_base64_chars=1,
        )

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    "encoded",
    [
        None,
        "",
        "R|3|5|33|" + "a" * 64,
        "R|3|5|33|" + "a" * 64 + "|44|extra",
        "R|+3|5|33|" + "a" * 64 + "|44",
        "R|0|5|33|" + "a" * 64 + "|44",
        f"R|{2**63}|5|33|" + "a" * 64 + "|44",
        "R|3|5|34|" + "a" * 64 + "|44",
        "R|3|5|33|" + "A" * 64 + "|44",
        "R|3|5|33|" + "a" * 64 + "|45",
        "D|private target detail",
        "E|value_admission_failed|private target detail",
        "X|private target detail",
        "Е|value_admission_failed",
        "E|" + "x" * 191,
    ],
)
def test_admission_envelope_v1_rejects_malformed_or_over_budget_results(
    encoded: object,
) -> None:
    with pytest.raises(CaptureValueCheckError) as caught:
        AdmissionEnvelopeV1.parse(
            encoded,
            max_payload_bytes=33,
            max_base64_chars=44,
        )

    assert "private target detail" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_admission_envelope_v1_builds_only_literal_denied_and_error_results() -> None:
    assert AdmissionEnvelopeV1.denied() == "D|worker_generation_value"
    assert AdmissionEnvelopeV1.failed() == "E|value_admission_failed"


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
        "inspection",
        "materialization_helper",
    }
    assert {state.value for state in CaptureEvaluationState} == {
        "pending",
        "completed",
        "failed",
        "unknown",
    }


def test_enum_helper_annotations_are_resolvable() -> None:
    hints = get_type_hints(_enum)
    assert "return" in hints


def test_public_evaluation_receipt_grammar_is_exported_and_disjoint_from_private_ids() -> None:
    public_receipt = "capture-eval-v1-a4f1d86e1e1d4d45b0948e021f669d1f"

    assert "is_public_capture_evaluation_id" in capture_evaluation.__all__
    assert is_public_capture_evaluation_id(public_receipt)
    for private_identifier in (
        "a4f1d86e1e1d4d45b0948e021f669d1f",
        "a4f1d86e-1e1d-4d45-b094-8e021f669d1f",
        "capture_manager_" + "a" * 32,
        "capture_table_metadata_" + "a" * 32,
        "capture_table_" + "a" * 32,
        "__onec_capture_table_" + "a" * 32,
        "__onec_value_" + "a" * 32,
        "__onec_materialization_" + "a" * 32,
        "__onec_projection_" + "a" * 32,
        "__onec_compact_table_" + "a" * 32,
        "e1cRuntimeКонтекст.Секрет",
        "worker://private-handle",
    ):
        assert not is_public_capture_evaluation_id(private_identifier)
    assert not is_public_capture_evaluation_id(
        "capture-eval-v1-" + "A" * 32
    )
    assert not is_public_capture_evaluation_id(
        "capture-eval-v1-" + "a" * 31
    )


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
        last_evaluation_id=(
            "eval-unknown" if phase is CapturePhase.OUTCOME_UNKNOWN else None
        ),
    )

    assert status.can_inspect is can_inspect
    assert status.can_resume_capture is can_resume
    assert status.can_wait is can_wait

    if phase is not CapturePhase.EVALUATING:
        retained = CaptureStatus(
            operation_id=7,
            capture_generation=3,
            stop_sequence=11,
            phase=phase,
            last_evaluation_id=("eval-last" if phase is not CapturePhase.STALE else None),
        )
        assert retained.can_wait is (
            phase in {
                CapturePhase.PAUSED,
                CapturePhase.RESUMING,
                CapturePhase.OUTCOME_UNKNOWN,
                CapturePhase.RECOVERY_REQUIRED,
            }
        )


def test_status_rejects_pending_records_outside_evaluating() -> None:
    for phase in CapturePhase:
        if phase is CapturePhase.EVALUATING:
            continue
        with pytest.raises(ValueError, match="pending_evaluation_id"):
            CaptureStatus(
                operation_id=7,
                capture_generation=3,
                stop_sequence=11,
                phase=phase,
                pending_evaluation_id="eval-pending",
                evaluation_kind=CaptureEvaluationKind.INSPECTION,
            )


def test_status_requires_pending_identity_while_evaluating() -> None:
    with pytest.raises(ValueError, match="evaluating"):
        CaptureStatus(
            operation_id=7,
            capture_generation=3,
            stop_sequence=11,
            phase=CapturePhase.EVALUATING,
        )


def test_status_requires_timing_to_describe_selected_evaluation() -> None:
    timing = CaptureEvaluationTiming(
        evaluation_id="other-eval",
        created_at_utc=datetime.now(timezone.utc),
    )
    with pytest.raises(ValueError, match="evaluation_timing"):
        CaptureStatus(
            operation_id=7,
            capture_generation=3,
            stop_sequence=11,
            phase=CapturePhase.EVALUATING,
            pending_evaluation_id="eval-pending",
            evaluation_kind=CaptureEvaluationKind.INSPECTION,
            evaluation_timing=timing,
        )
    with pytest.raises(ValueError, match="evaluation_timing"):
        CaptureStatus(
            operation_id=7,
            capture_generation=3,
            stop_sequence=11,
            phase=CapturePhase.PAUSED,
            last_evaluation_id="eval-last",
            evaluation_timing=timing,
        )


def test_outcome_unknown_requires_a_retained_evaluation() -> None:
    with pytest.raises(ValueError, match="last_evaluation_id"):
        CaptureStatus(
            operation_id=7,
            capture_generation=3,
            stop_sequence=11,
            phase=CapturePhase.OUTCOME_UNKNOWN,
        )

    recovery = CaptureStatus(
        operation_id=7,
        capture_generation=3,
        stop_sequence=11,
        phase=CapturePhase.RECOVERY_REQUIRED,
    )
    assert recovery.can_wait is False


@pytest.mark.parametrize(
    "phase",
    [
        CapturePhase.PAUSED,
        CapturePhase.EVALUATING,
        CapturePhase.RESUMING,
        CapturePhase.STALE,
    ],
)
def test_status_rejects_failure_outside_terminal_failure_phases(phase: CapturePhase) -> None:
    with pytest.raises(ValueError, match="failure"):
        CaptureStatus(
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
            failure=CaptureFailureDiagnostic("failed", "message", "recover"),
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


@pytest.mark.parametrize(
    ("state", "kwargs"),
    [
        (CaptureEvaluationState.PENDING, {"result": 1}),
        (CaptureEvaluationState.PENDING, {"messages": ("still waiting",)}),
        (CaptureEvaluationState.PENDING, {"error": "failed"}),
        (CaptureEvaluationState.PENDING, {"diagnostic": CaptureFailureDiagnostic("x", "y", "z")}),
        (CaptureEvaluationState.COMPLETED, {"error": "failed"}),
        (CaptureEvaluationState.COMPLETED, {"diagnostic": CaptureFailureDiagnostic("x", "y", "z")}),
        (CaptureEvaluationState.FAILED, {"result": "private"}),
        (CaptureEvaluationState.UNKNOWN, {"result": "private"}),
        (CaptureEvaluationState.UNKNOWN, {"error": "private"}),
    ],
)
def test_outcome_rejects_state_incompatible_payloads(
    state: CaptureEvaluationState, kwargs: dict[str, object]
) -> None:
    with pytest.raises(ValueError, match="state"):
        CaptureEvaluationOutcome(
            evaluation_id="eval-1",
            evaluation_kind=CaptureEvaluationKind.USER_BSL,
            state=state,
            **kwargs,
        )


@pytest.mark.parametrize(
    ("state", "kwargs"),
    [
        (CaptureEvaluationState.PENDING, {}),
        (CaptureEvaluationState.COMPLETED, {}),
        (CaptureEvaluationState.FAILED, {"error": "failed safely"}),
        (
            CaptureEvaluationState.UNKNOWN,
            {"diagnostic": CaptureFailureDiagnostic("unknown", "uncertain", "recover")},
        ),
    ],
)
def test_outcome_accepts_state_appropriate_payloads(
    state: CaptureEvaluationState, kwargs: dict[str, object]
) -> None:
    outcome = CaptureEvaluationOutcome(
        evaluation_id="eval-1",
        evaluation_kind=CaptureEvaluationKind.USER_BSL,
        state=state,
        **kwargs,
    )
    assert outcome.state is state


def test_outcome_preserves_an_admitted_user_string_exactly() -> None:
    value = "line 1\n" + ("x" * 2_000) + "\r\nline 3\x00"
    outcome = CaptureEvaluationOutcome(
        evaluation_id="eval-1",
        evaluation_kind=CaptureEvaluationKind.USER_BSL,
        state=CaptureEvaluationState.COMPLETED,
        result=value,
    )
    assert outcome.result == value


def test_outcome_reports_message_count_truncation() -> None:
    outcome = CaptureEvaluationOutcome(
        evaluation_id="eval-1",
        evaluation_kind=CaptureEvaluationKind.USER_BSL,
        state=CaptureEvaluationState.COMPLETED,
        messages=tuple(f"message-{index}" for index in range(101)),
    )

    assert len(outcome.messages) == 100
    assert outcome.diagnostic is not None
    assert outcome.diagnostic.code == "messages_truncated"
    assert "truncated" in outcome.diagnostic.message.casefold()


def test_outcome_preserves_failure_diagnostic_while_reporting_message_truncation() -> None:
    existing = CaptureFailureDiagnostic(
        "remote_failure",
        "remote operation failed",
        "retry after recovery",
    )
    outcome = CaptureEvaluationOutcome(
        evaluation_id="eval-1",
        evaluation_kind=CaptureEvaluationKind.USER_BSL,
        state=CaptureEvaluationState.FAILED,
        messages=tuple(f"message-{index}" for index in range(101)),
        error="failed",
        diagnostic=existing,
    )

    assert outcome.diagnostic is not None
    assert outcome.diagnostic.code == existing.code
    assert outcome.diagnostic.recommended_action == existing.recommended_action
    assert existing.message in outcome.diagnostic.message
    assert "truncated" in outcome.diagnostic.message.casefold()


@pytest.mark.parametrize(
    "value",
    [
        Exception("SELECT * FROM secret"),
        {"request": "opaque"},
        ["mutable"],
        ("safe", ["mutable nested"]),
    ],
)
def test_outcome_rejects_opaque_and_mutable_results(value: object) -> None:
    with pytest.raises(ValueError, match="public result"):
        CaptureEvaluationOutcome(
            evaluation_id="eval-1",
            evaluation_kind=CaptureEvaluationKind.USER_BSL,
            state=CaptureEvaluationState.COMPLETED,
            result=value,
        )


def test_outcome_accepts_only_immutable_public_scalars_and_hides_internal_results() -> None:
    outcome = CaptureEvaluationOutcome(
        evaluation_id="eval-1",
        evaluation_kind=CaptureEvaluationKind.USER_BSL,
        state=CaptureEvaluationState.COMPLETED,
        result=(None, True, 4, 1.5, "value"),
    )
    assert outcome.result == (None, True, 4, 1.5, "value")

    for kind in (
        CaptureEvaluationKind.MATERIALIZATION_HELPER,
        CaptureEvaluationKind.INSPECTION,
        CaptureEvaluationKind.MATERIALIZATION_HELPER,
    ):
        with pytest.raises(ValueError, match="internal"):
            CaptureEvaluationOutcome(
                evaluation_id="eval-1",
                evaluation_kind=kind,
                state=CaptureEvaluationState.COMPLETED,
                result=True,
            )


def test_pending_and_busy_errors_expose_only_safe_evaluation_facts() -> None:
    pending = CaptureEvaluationPendingError("eval-1", CaptureEvaluationKind.MATERIALIZATION_HELPER)
    busy = CaptureBusyError(
        "eval-1", CaptureEvaluationKind.MATERIALIZATION_HELPER, CapturePhase.EVALUATING
    )

    assert pending.evaluation_id == "eval-1"
    assert pending.evaluation_kind is CaptureEvaluationKind.MATERIALIZATION_HELPER
    assert busy.evaluation_id == "eval-1"
    assert busy.evaluation_kind is CaptureEvaluationKind.MATERIALIZATION_HELPER
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
