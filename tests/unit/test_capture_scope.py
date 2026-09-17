from uuid import UUID

import pytest

from onec_runtime.execution.capture.scope import (
    CaptureContextState,
    CaptureFrameIdentity,
    CaptureScope,
    CaptureSetupStage,
    CaptureStopIdentity,
)
from onec_runtime.rdbg.models import FrameVariable, ModuleLocation, StopEvent, TargetId


TARGET = TargetId(UUID("22222222-2222-2222-2222-222222222222"), "Test")
LOCATION = ModuleLocation(
    "ExtensionModule", "", UUID(int=1), UUID(int=2), 50, "Runtime"
)
STOP = StopEvent(TARGET, LOCATION, "callStackFormed", stop_by_breakpoint=True)


def test_scope_keeps_stop_identity_when_local_setup_fails() -> None:
    scope = CaptureScope.from_stop(7, 42, STOP, 3)

    scope.fail_setup(ValueError("local values are unavailable"))

    assert scope.identity.runtime_generation == 7
    assert scope.identity.main_command_id == 42
    assert scope.identity.target_id == TARGET
    assert scope.identity.location == LOCATION
    assert scope.identity.local_stop_sequence == 3
    assert scope.setup_stage is CaptureSetupStage.STOP_RECOGNIZED
    assert scope.context_state is CaptureContextState.SETUP_FAILED
    assert scope.frame_identity is CaptureFrameIdentity.UNVERIFIED
    assert scope.setup_error_code == "ValueError"


def test_scope_records_setup_evidence_and_ready_frame() -> None:
    scope = CaptureScope.from_stop(7, 42, STOP, 3)
    variables = (FrameVariable("Value", "Number", "7"),)

    scope.record_locals(variables)
    scope.record_transfer("temporary-address")
    scope.record_kernel_frame(2)
    assert scope.record_main_command(42)
    scope.record_context_begun()
    scope.mark_ready()

    assert scope.setup_stage is CaptureSetupStage.CONTEXT_OPENED
    assert scope.context_state is CaptureContextState.READY
    assert scope.frame_identity is CaptureFrameIdentity.CONFIRMED
    assert scope.frame_variables == variables
    assert scope.kernel_stack_level == 2
    assert scope.frame_stack_level == 0
    assert scope.inspection_target_id == TARGET

    scope.invalidate_inspection()

    assert scope.frame_variables == ()
    assert scope.inspection_target_id is None
    assert scope.identity.target_id == TARGET

    scope.mark_closed()
    assert scope.context_state is CaptureContextState.CLOSED
    assert scope.frame_identity is CaptureFrameIdentity.RELEASED


def test_wrong_main_id_never_confirms_frame() -> None:
    scope = CaptureScope.from_stop(7, 42, STOP, 3)
    scope.record_locals(())
    scope.record_transfer("temporary-address")
    scope.record_kernel_frame(2)

    assert not scope.record_main_command(41)
    assert scope.observed_main_command_id == 41
    assert scope.frame_identity is CaptureFrameIdentity.UNVERIFIED
    with pytest.raises(RuntimeError, match="confirmed MAIN command"):
        scope.mark_ready()


def test_ambiguous_continue_keeps_old_stop_evidence_but_blocks_frame_access() -> None:
    scope = CaptureScope.from_stop(7, 42, STOP, 3)
    scope.record_locals(())
    scope.record_transfer("temporary-address")
    scope.record_kernel_frame(2)
    assert scope.record_main_command(42)
    scope.record_context_begun()
    scope.mark_ready()

    scope.mark_unverified()

    assert scope.context_state is CaptureContextState.CLOSING
    assert scope.frame_identity is CaptureFrameIdentity.UNVERIFIED
    assert scope.identity.target_id == TARGET
    assert scope.inspection_target_id is None


def test_scope_rejects_identity_that_does_not_match_stop() -> None:
    other = ModuleLocation(
        "ExtensionModule", "", UUID(int=1), UUID(int=2), 51, "Runtime"
    )
    with pytest.raises(ValueError, match="does not match"):
        CaptureScope(CaptureStopIdentity(7, 42, TARGET, other, 3), STOP)


def test_transport_uncertainty_does_not_claim_confirmed_setup_failure() -> None:
    scope = CaptureScope.from_stop(7, 42, STOP, 3)
    scope.note_setup_uncertain(ConnectionError("response lost"))

    assert scope.context_state is CaptureContextState.OPENING
    assert scope.frame_identity is CaptureFrameIdentity.UNVERIFIED
    assert scope.setup_error_code == "ConnectionError"
