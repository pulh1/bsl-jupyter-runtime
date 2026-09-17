"""Synchronous server-target teardown for the arbiter's single RDBG worker.

The caller must have fenced ordinary dispatch before invoking this helper.
It does not read debugger events or create another RDBG writer.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Literal, Protocol

from onec_runtime.rdbg.models import TargetId
from onec_runtime.rdbg.session import BoundServerTargetAbsence


class ServerTerminationPort(Protocol):
    def terminate_bound_server_session(self) -> bool: ...

    def wait_for_bound_server_targets_absent(
        self, expected_target: TargetId, *, timeout_s: float
    ) -> BoundServerTargetAbsence: ...


@dataclass(frozen=True, slots=True)
class ServerTerminationConfirmed:
    expected_target: TargetId
    absence: BoundServerTargetAbsence


@dataclass(frozen=True, slots=True)
class TerminationUnknown:
    """The old target may still run; callers must retain its ownership fence."""

    expected_target: TargetId
    stage: Literal["request", "confirmation"]
    error_type: str
    client_termination_requested: bool | None


def terminate_server_target(
    port: ServerTerminationPort,
    expected_target: TargetId,
    *,
    grace_s: float = 30.0,
) -> ServerTerminationConfirmed | TerminationUnknown:
    """Request teardown and require absence within a confirmation interval.

    ``grace_s`` limits proof gathering, never execution of the BSL command.
    An expired interval preserves unknown target ownership.
    """

    if (
        isinstance(grace_s, bool)
        or not isinstance(grace_s, (int, float))
        or not isfinite(float(grace_s))
        or grace_s < 0
    ):
        raise ValueError("grace_s must be finite and non-negative")

    try:
        requested = port.terminate_bound_server_session()
    except Exception as error:
        return TerminationUnknown(
            expected_target, "request", type(error).__name__, None
        )
    try:
        absence = port.wait_for_bound_server_targets_absent(
            expected_target, timeout_s=float(grace_s)
        )
    except Exception as error:
        return TerminationUnknown(
            expected_target, "confirmation", type(error).__name__, requested
        )
    if (
        not isinstance(absence, BoundServerTargetAbsence)
        or absence.expected_target != expected_target
        or absence.bound_client.infobase_alias.casefold()
        != expected_target.infobase_alias.casefold()
        or absence.bound_client.seance_id != expected_target.seance_id
        or (
            absence.bound_client.infobase_instance_id is not None
            and expected_target.infobase_instance_id is not None
            and absence.bound_client.infobase_instance_id
            != expected_target.infobase_instance_id
        )
    ):
        return TerminationUnknown(
            expected_target, "confirmation", "EvidenceMismatch", requested
        )
    return ServerTerminationConfirmed(expected_target, absence)
