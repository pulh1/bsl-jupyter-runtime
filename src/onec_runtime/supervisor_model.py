from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from uuid import UUID

from onec_runtime.errors import StaleRuntimeGeneration


class GenerationLifecycle(str, Enum):
    STARTING = "starting"
    ACTIVE = "active"
    TERMINATING = "terminating"
    TERMINATED = "terminated"


class TerminationCause(str, Enum):
    CONTROLLER_EXIT = "controller_exit"
    CONTROLLER_HUNG = "controller_hung"
    LEASE_EXPIRED = "lease_expired"
    STARTUP_FAILED = "startup_failed"
    REQUESTED = "requested"


@dataclass(frozen=True, slots=True)
class GenerationHandle:
    generation_id: int


@dataclass(frozen=True, slots=True)
class LeaseHandle:
    generation_id: int
    owner_id: UUID
    lease_epoch: int


@dataclass(frozen=True, slots=True)
class SupervisedOperationHandle:
    generation_id: int
    operation_id: int
    owner_id: UUID
    lease_epoch: int


@dataclass(frozen=True, slots=True)
class GenerationRecord:
    handle: GenerationHandle
    lifecycle: GenerationLifecycle
    controller_pid: int | None = None
    dbgs_pid: int | None = None
    onec_pid: int | None = None
    termination_cause: TerminationCause | None = None


class GenerationRegistry:
    """Owns the in-memory lifecycle of one supervised runtime generation."""

    def __init__(self) -> None:
        self._last_generation_id = 0
        self._current: GenerationRecord | None = None

    def begin_start(self) -> GenerationRecord:
        if self._current is not None and (
            self._current.lifecycle is not GenerationLifecycle.TERMINATED
        ):
            raise RuntimeError(
                "Cannot start generation while "
                f"{self._current.lifecycle.name} generation exists"
            )
        self._last_generation_id += 1
        self._current = GenerationRecord(
            handle=GenerationHandle(self._last_generation_id),
            lifecycle=GenerationLifecycle.STARTING,
        )
        return self._current

    def activate(
        self,
        handle: GenerationHandle,
        *,
        controller_pid: int,
        dbgs_pid: int,
        onec_pid: int,
    ) -> GenerationRecord:
        record = self._require_current(handle)
        self._require_lifecycle(record, GenerationLifecycle.STARTING, "activate")
        self._current = replace(
            record,
            lifecycle=GenerationLifecycle.ACTIVE,
            controller_pid=controller_pid,
            dbgs_pid=dbgs_pid,
            onec_pid=onec_pid,
        )
        return self._current

    def begin_termination(
        self,
        handle: GenerationHandle,
        cause: TerminationCause,
    ) -> GenerationRecord:
        record = self._require_current(handle)
        self._require_lifecycle(
            record,
            GenerationLifecycle.ACTIVE,
            "begin termination",
        )
        self._current = replace(
            record,
            lifecycle=GenerationLifecycle.TERMINATING,
            termination_cause=cause,
        )
        return self._current

    def finish_termination(self, handle: GenerationHandle) -> GenerationRecord:
        record = self._require_current(handle)
        self._require_lifecycle(
            record,
            GenerationLifecycle.TERMINATING,
            "finish termination",
        )
        self._current = replace(record, lifecycle=GenerationLifecycle.TERMINATED)
        return self._current

    def require_active(self, handle: GenerationHandle) -> GenerationRecord:
        record = self._require_current(handle)
        if record.lifecycle is not GenerationLifecycle.ACTIVE:
            raise StaleRuntimeGeneration(
                f"Generation {handle.generation_id} is not active"
            )
        return record

    def _require_current(self, handle: GenerationHandle) -> GenerationRecord:
        if self._current is None or self._current.handle != handle:
            raise StaleRuntimeGeneration(
                f"Generation {handle.generation_id} is no longer current"
            )
        return self._current

    @staticmethod
    def _require_lifecycle(
        record: GenerationRecord,
        expected: GenerationLifecycle,
        action: str,
    ) -> None:
        if record.lifecycle is not expected:
            raise RuntimeError(
                f"Cannot {action} generation in {record.lifecycle.name} lifecycle"
            )
