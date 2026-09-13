from dataclasses import FrozenInstanceError
from uuid import UUID

import pytest

from onec_runtime.errors import RecoveryIdentityMismatch
from onec_runtime.rdbg.models import DebugTarget, ModuleLocation, StopEvent, TargetId
from onec_runtime.recovery import (
    RecoveryCheckpoint,
    RecoveryIdentityEvidence,
    RecoveryOutcome,
    RecoveryPhase,
    RootWriteRecord,
    SideEffectStatus,
    validate_paused_identity,
)


TARGET_ID = TargetId(UUID("22222222-2222-2222-2222-222222222222"), "DefAlias")
TARGET = DebugTarget(TARGET_ID, "ServerEmulation", "stopped", 7)
LOCATION = ModuleLocation(
    "ExtensionModule",
    "",
    UUID("cb953767-f436-4a5b-9e09-13a67d6e0201"),
    UUID("d5963243-262e-4398-b4d7-fb16d06484f6"),
    69,
    "OnecInteractiveRuntime",
)


def checkpoint(*, phase: RecoveryPhase = RecoveryPhase.CAPTURED) -> RecoveryCheckpoint:
    return RecoveryCheckpoint(
        sequence=1,
        runtime_generation=4,
        operation_id=9,
        phase=phase,
        target=TARGET,
        frame_location=LOCATION,
        stop_sequence=2,
        breakpoint_workspace=(LOCATION,),
        write_journal=(),
        continue_sent=False,
    )


def test_checkpoint_is_immutable_and_records_replay_guards() -> None:
    value = checkpoint()

    assert value.runtime_generation == 4
    assert value.operation_id == 9
    assert value.stop_sequence == 2
    assert value.continue_sent is False
    with pytest.raises(FrozenInstanceError):
        value.operation_id = 10  # type: ignore[misc]


def test_root_write_unknown_outcome_forbids_replay() -> None:
    record = RootWriteRecord(
        sequence=3,
        root="Скаляр",
        expression_sha256="a" * 64,
        status=SideEffectStatus.OUTCOME_UNKNOWN,
        result_id=None,
        error="transport closed",
    )

    assert record.replay_forbidden is True
    assert record.succeeded is False


def test_paused_identity_accepts_exact_target_and_frame() -> None:
    evidence = RecoveryIdentityEvidence(
        target=TARGET,
        stop=StopEvent(TARGET_ID, LOCATION, "recoveredCallStack", stack=(LOCATION,)),
    )

    validate_paused_identity(checkpoint(), evidence)


def test_paused_identity_accepts_stop_on_next_line_with_exact_frame() -> None:
    live_target = DebugTarget(
        TARGET_ID,
        "ServerEmulation",
        "StopOnNextLine",
        7,
    )
    evidence = RecoveryIdentityEvidence(
        target=live_target,
        stop=StopEvent(TARGET_ID, LOCATION, "recoveredCallStack", stack=(LOCATION,)),
    )

    validate_paused_identity(checkpoint(), evidence)


def test_paused_identity_rejects_changed_frame() -> None:
    changed = ModuleLocation(
        LOCATION.module_type,
        LOCATION.url,
        LOCATION.object_id,
        LOCATION.property_id,
        LOCATION.line + 1,
        LOCATION.extension_name,
    )
    evidence = RecoveryIdentityEvidence(
        target=TARGET,
        stop=StopEvent(TARGET_ID, changed, "recoveredCallStack", stack=(changed,)),
    )

    with pytest.raises(RecoveryIdentityMismatch, match="frame-zero"):
        validate_paused_identity(checkpoint(), evidence)


def test_paused_identity_rejects_changed_target_state_number() -> None:
    changed = DebugTarget(TARGET_ID, "ServerEmulation", "stopped", 8)
    evidence = RecoveryIdentityEvidence(
        target=changed,
        stop=StopEvent(TARGET_ID, LOCATION, "recoveredCallStack", stack=(LOCATION,)),
    )

    with pytest.raises(RecoveryIdentityMismatch, match="state number"):
        validate_paused_identity(checkpoint(), evidence)


def test_recovery_outcomes_are_explicit() -> None:
    assert RecoveryOutcome.RECOVERED.value == "recovered"
    assert RecoveryOutcome.LOST.value == "lost"
