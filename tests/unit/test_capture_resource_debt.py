"""Temporary cleanup debt is independent of the stopped CAPTURE frame."""

from uuid import UUID

import pytest

from onec_runtime.execution.capture.scope import (
    CaptureContextState,
    CaptureFrameIdentity,
    CaptureScope,
)
from onec_runtime.execution.capture.resources import TemporaryCleanupState
from onec_runtime.rdbg.models import ModuleLocation, StopEvent, TargetId


TARGET = TargetId(UUID(int=1), "test")
LOCATION = ModuleLocation("ExtensionModule", "", UUID(int=2), UUID(int=3), 10, "Runtime")
STOP = StopEvent(TARGET, LOCATION, "callStackFormed")


def ready_scope() -> CaptureScope:
    scope = CaptureScope.from_stop(7, 42, STOP, 1)
    scope.record_main_command(42)
    scope.record_context_begun()
    scope.mark_ready()
    return scope


def test_confirmed_temporary_cleanup_error_preserves_frame_and_allows_exact_retry() -> None:
    scope = ready_scope()
    key = "__onec_compact_table_" + "a" * 32

    scope.track_temporary_key(key)
    scope.note_temporary_cleanup_failure(key)

    debt = scope.temporary_cleanup_debts[0]
    assert debt.key == key
    assert debt.state is TemporaryCleanupState.CONFIRMED_FAILURE
    assert debt.can_retry_delete
    assert scope.context_state is CaptureContextState.READY
    assert scope.frame_identity is CaptureFrameIdentity.CONFIRMED
    assert key not in repr(scope)
    assert key not in repr(debt)

    scope.confirm_temporary_cleanup(key)
    assert scope.temporary_cleanup_debts == ()
    assert scope.context_state is CaptureContextState.READY


def test_unknown_delete_requires_reconciliation_before_retry() -> None:
    scope = ready_scope()
    key = "__onec_value_" + "b" * 32
    scope.track_temporary_key(key)

    scope.note_temporary_cleanup_unknown(key)

    debt = scope.temporary_cleanup_debts[0]
    assert debt.state is TemporaryCleanupState.UNKNOWN
    assert not debt.can_retry_delete
    assert scope.frame_identity is CaptureFrameIdentity.CONFIRMED


def test_cleanup_debt_cannot_be_claimed_for_an_untracked_key() -> None:
    scope = ready_scope()

    with pytest.raises(KeyError):
        scope.note_temporary_cleanup_failure("__onec_value_" + "c" * 32)
    assert scope.temporary_cleanup_debts == ()
