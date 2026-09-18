"""Component CAPTURE evaluation ledger keeps only bounded public evidence."""

from threading import Event, Thread

import pytest

from onec_runtime.capture_evaluation import (
    CaptureEvaluationKind,
    CaptureEvaluationState,
    CaptureFailureDiagnostic,
    CapturePhase,
)
from onec_runtime.errors import ProtocolError, StaleCaptureError
from onec_runtime.execution.capture.evaluation_ledger import CaptureEvaluationLedger

from test_capture_stack_inventory_adapter import ready_scope


def test_pending_record_settles_without_changing_its_public_receipt() -> None:
    scope = ready_scope()
    ledger = CaptureEvaluationLedger(scope, is_current=lambda: True)

    ledger.begin("eval-1", CaptureEvaluationKind.USER_BSL)
    ledger.note_dispatch("eval-1")
    ledger.note_acknowledged("eval-1")
    pending = ledger.wait(timeout_s=0)
    completed = ledger.complete("eval-1", 42, messages=("ok",))

    assert pending.evaluation_id == "eval-1"
    assert pending.state is CaptureEvaluationState.PENDING
    assert completed.evaluation_id == "eval-1"
    assert completed.state is CaptureEvaluationState.COMPLETED
    assert completed.result == 42
    status = ledger.status()
    assert status.phase is CapturePhase.PAUSED
    assert status.last_evaluation_id == "eval-1"
    assert status.last_user_evaluation_id == "eval-1"
    assert status.evaluation_timing is not None
    assert status.evaluation_timing.remote_step_count == 1


def test_wait_timeout_does_not_cancel_the_active_record() -> None:
    scope = ready_scope()
    ledger = CaptureEvaluationLedger(scope, is_current=lambda: True)
    ledger.begin("eval-2", CaptureEvaluationKind.USER_BSL)

    observed = ledger.wait(timeout_s=0)

    assert observed.state is CaptureEvaluationState.PENDING
    assert ledger.status().phase is CapturePhase.EVALUATING
    assert ledger.complete("eval-2", True).state is CaptureEvaluationState.COMPLETED


def test_waiter_observes_settlement_without_owning_or_cancelling_it() -> None:
    scope = ready_scope()
    ledger = CaptureEvaluationLedger(scope, is_current=lambda: True)
    ledger.begin("eval-3", CaptureEvaluationKind.USER_BSL)
    entered = Event()
    observed = []

    def waiter() -> None:
        entered.set()
        observed.append(ledger.wait(timeout_s=1))

    thread = Thread(target=waiter)
    thread.start()
    assert entered.wait(1)
    ledger.complete("eval-3", "done")
    thread.join(1)

    assert not thread.is_alive()
    assert observed[0].state is CaptureEvaluationState.COMPLETED
    assert observed[0].result == "done"


def test_stale_scope_returns_stale_status_and_rejects_wait() -> None:
    scope = ready_scope()
    current = [True]
    ledger = CaptureEvaluationLedger(scope, is_current=lambda: current[0])
    ledger.begin("eval-4", CaptureEvaluationKind.USER_BSL)
    current[0] = False

    assert ledger.status().phase is CapturePhase.STALE
    with pytest.raises(StaleCaptureError):
        ledger.wait(timeout_s=0)


def test_scope_change_wakes_a_waiter_as_stale_without_cancellation() -> None:
    scope = ready_scope()
    current = [True]
    ledger = CaptureEvaluationLedger(scope, is_current=lambda: current[0])
    ledger.begin("eval-stale", CaptureEvaluationKind.USER_BSL)
    entered = Event()
    errors = []

    def waiter() -> None:
        entered.set()
        try:
            ledger.wait(timeout_s=1)
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=waiter)
    thread.start()
    assert entered.wait(1)
    current[0] = False
    ledger.notify_scope_changed()
    thread.join(1)

    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], StaleCaptureError)


def test_history_is_bounded_and_public_errors_are_sanitized() -> None:
    scope = ready_scope()
    ledger = CaptureEvaluationLedger(scope, is_current=lambda: True, history_limit=2)
    for receipt in ("eval-a", "eval-b", "eval-c"):
        ledger.begin(receipt, CaptureEvaluationKind.MATERIALIZATION_HELPER)
        ledger.complete(receipt)

    with pytest.raises(ProtocolError, match="unavailable"):
        ledger.wait(timeout_s=0, evaluation_id="eval-a")
    diagnostic = CaptureFailureDiagnostic(
        "transport", "secret\nmessage", "retry safely",
    )
    ledger.begin("eval-d", CaptureEvaluationKind.USER_BSL)
    unknown = ledger.mark_unknown("eval-d", diagnostic)

    assert unknown.state is CaptureEvaluationState.UNKNOWN
    assert unknown.diagnostic is diagnostic
    assert "\n" not in unknown.diagnostic.message
    assert "secret" not in repr(unknown)
    assert ledger.status().phase is CapturePhase.OUTCOME_UNKNOWN
