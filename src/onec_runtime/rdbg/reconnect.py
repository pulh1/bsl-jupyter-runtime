from __future__ import annotations

from dataclasses import dataclass

from onec_runtime.errors import RecoveryIdentityMismatch
from onec_runtime.recovery import (
    RecoveryCheckpoint,
    RecoveryIdentityEvidence,
    is_paused_target_state,
)
from onec_runtime.rdbg.session import RdbgSession, SessionState
from onec_runtime.rdbg.transport import RdbgTransport


@dataclass(frozen=True, slots=True)
class ReconnectedSession:
    session: RdbgSession
    evidence: RecoveryIdentityEvidence | None


def reconnect_session(
    transport: RdbgTransport,
    previous: RdbgSession,
    checkpoint: RecoveryCheckpoint,
    *,
    observe_executing: bool,
    timeout_s: float = 5.0,
) -> ReconnectedSession:
    session = RdbgSession(
        transport,
        previous.expected_location,
        alias=previous.alias,
        ui_id=previous.ui_id,
        break_on_next=False,
    )
    session.initialize()
    matches = [
        target
        for target in session.list_targets()
        if target.target_id == checkpoint.target.target_id
        and target.target_type == checkpoint.target.target_type
    ]
    if len(matches) != 1:
        raise RecoveryIdentityMismatch(
            f"Expected one recovered target, found {len(matches)}"
        )
    target = matches[0]
    session.attach_target(target)
    session.set_breakpoints(checkpoint.breakpoint_workspace)
    if is_paused_target_state(target.state):
        stop = session.read_current_stack(timeout_s=timeout_s)
        return ReconnectedSession(
            session,
            RecoveryIdentityEvidence(target, stop),
        )
    if not observe_executing:
        raise RecoveryIdentityMismatch("Paused checkpoint target is not stopped")
    session.state = SessionState.EXECUTING
    return ReconnectedSession(session, None)
