"""Explicit synthetic server-exit proof for unit tests of unrelated unknown paths."""

from uuid import uuid4

from onec_runtime.execution.arbiter import ExecutionTicket, RdbgArbiter, RouteToken
from onec_runtime.execution.termination import ServerTerminationConfirmed
from onec_runtime.rdbg.models import DebugTarget, TargetId
from onec_runtime.rdbg.session import BoundServerTargetAbsence


def confirm_test_server_terminated(
    arbiter: RdbgArbiter,
    ticket: ExecutionTicket,
    route: RouteToken,
    session: object,
    target: TargetId,
) -> ServerTerminationConfirmed:
    """Model a bound server disappearing after an unrelated test fences it."""

    assert target.seance_id is not None
    session.target = DebugTarget(target, "Server", "stopped")
    client = TargetId(uuid4(), target.infobase_alias, target.seance_id)
    proof = ServerTerminationConfirmed(
        target, BoundServerTargetAbsence(client, target, 1.0, 1),
    )
    arbiter.retire_terminated_target(ticket, route, proof)
    return proof
