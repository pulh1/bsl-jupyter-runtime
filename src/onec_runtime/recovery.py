from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from uuid import UUID

from onec_runtime.errors import RecoveryIdentityMismatch
from onec_runtime.rdbg.models import DebugTarget, ModuleLocation, StopEvent


class RecoveryPhase(str, Enum):
    CAPTURED = "captured"
    FLUSHING = "flushing"
    RESUMING = "resuming"


class RecoveryOutcome(str, Enum):
    RECOVERED = "recovered"
    LOST = "lost"


class SideEffectStatus(str, Enum):
    PLANNED = "planned"
    SENT = "sent"
    ACKNOWLEDGED = "acknowledged"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


@dataclass(frozen=True, slots=True)
class RootWriteRecord:
    sequence: int
    root: str
    expression_sha256: str
    status: SideEffectStatus
    result_id: UUID | None
    error: str = ""

    @property
    def replay_forbidden(self) -> bool:
        return self.status in {
            SideEffectStatus.ACKNOWLEDGED,
            SideEffectStatus.SUCCEEDED,
            SideEffectStatus.FAILED,
            SideEffectStatus.OUTCOME_UNKNOWN,
        }

    @property
    def succeeded(self) -> bool:
        return self.status is SideEffectStatus.SUCCEEDED


@dataclass(frozen=True, slots=True)
class RecoveryCheckpoint:
    sequence: int
    runtime_generation: int
    operation_id: int
    phase: RecoveryPhase
    target: DebugTarget
    frame_location: ModuleLocation | None
    stop_sequence: int
    breakpoint_workspace: tuple[ModuleLocation, ...]
    write_journal: tuple[RootWriteRecord, ...]
    continue_sent: bool


@dataclass(frozen=True, slots=True)
class RecoveryIdentityEvidence:
    target: DebugTarget
    stop: StopEvent


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    outcome: RecoveryOutcome
    phase: RecoveryPhase
    checkpoint: RecoveryCheckpoint
    reason: str
    continuation: object | None = None


def is_paused_target_state(state: str) -> bool:
    return state.casefold() in {"stopped", "stoponnextline"}


def validate_paused_identity(
    checkpoint: RecoveryCheckpoint,
    evidence: RecoveryIdentityEvidence,
) -> None:
    if evidence.target.target_id != checkpoint.target.target_id:
        raise RecoveryIdentityMismatch("Recovered target identity changed")
    if evidence.target.target_type != checkpoint.target.target_type:
        raise RecoveryIdentityMismatch("Recovered target type changed")
    if not is_paused_target_state(evidence.target.state):
        raise RecoveryIdentityMismatch("Recovered target is not stopped")
    if (
        checkpoint.target.state_number is not None
        and evidence.target.state_number is not None
        and evidence.target.state_number != checkpoint.target.state_number
    ):
        raise RecoveryIdentityMismatch("Recovered target state number changed")
    if checkpoint.frame_location is None:
        raise RecoveryIdentityMismatch("Checkpoint has no frame-zero location")
    if evidence.stop.location != checkpoint.frame_location:
        raise RecoveryIdentityMismatch("Recovered frame-zero location changed")
