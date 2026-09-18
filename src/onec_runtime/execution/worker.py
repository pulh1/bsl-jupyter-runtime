"""Worker activation contracts owned by an arbiter execution plan."""

from __future__ import annotations

from typing import Protocol

from onec_runtime.execution.arbiter import SessionPort
from onec_runtime.execution.arbiter import OutcomeUnknown
from onec_runtime.execution.preparation import WorkerCandidateIntent


class WorkerActivationLease(Protocol):
    """One activated Worker generation held by the submitted operation."""

    def release(self, *, port: SessionPort) -> None: ...

    def retain_outcome_unknown(self, *, port: SessionPort) -> None: ...


class WorkerActivationUnknown(OutcomeUnknown):
    """Activation lost its reply after creating a lease that must be retained."""

    __slots__ = ("lease",)

    def __init__(self, lease: WorkerActivationLease, message: str) -> None:
        super().__init__(message)
        self.lease = lease


class WorkerActivationPort(Protocol):
    """Build, publish, and pin a prepared Worker on the arbiter worker only."""

    def pin_active(self, *, port: SessionPort) -> WorkerActivationLease | None:
        """Pin the current generation for one admitted ordinary operation."""
        ...

    def activate(
        self, intent: WorkerCandidateIntent, *, port: SessionPort
    ) -> WorkerActivationLease: ...
