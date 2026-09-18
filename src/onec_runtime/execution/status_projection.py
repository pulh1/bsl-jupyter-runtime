"""Public state from one immutable controller observation and local owners.

``ControllerStatusFacts`` is copied under ExecutionController's lock. Its
``ticket_phase`` must be read from the ticket owned by that controller route;
reading ``arbiter.active_ticket`` independently here would mix two instants.
No local wait interval can establish target loss.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, TYPE_CHECKING

from onec_runtime.errors import ProtocolError
from onec_runtime.execution.capture.scope import (
    CaptureContextState, CaptureFrameIdentity, CaptureSetupSnapshot,
)
from onec_runtime.execution.main.operation import MainPhase
from onec_runtime.execution.namespace import RuntimeNamespaceOwner
from onec_runtime.execution.worker_activation import WorkerActivationSnapshot

if TYPE_CHECKING:
    from onec_runtime.runtime_models import RuntimeNamespaceSnapshot, RuntimeStatus


class ExecutionActivity(str, Enum):
    """Which admitted route owns a ticket in a controller status snapshot."""

    NONE = "none"
    MAIN = "main"
    CAPTURE = "capture"
    CAPTURE_RESUME = "capture_resume"
    DEBUG_RESUME = "debug_resume"
    MAINTENANCE = "maintenance"


@dataclass(frozen=True, slots=True)
class ControllerStatusFacts:
    """Atomic local facts; never proof of remote target termination by itself.

    ``command_id`` is the current or most recent MAIN command ID. The
    controller captures the phase and scope fields together under its lock.
    ``ticket_phase`` is a copy of its owned arbiter ticket's status phase.
    """

    runtime_generation: int
    command_id: int
    main_phase: MainPhase | None
    capture_context_state: CaptureContextState | None = None
    capture_frame_identity: CaptureFrameIdentity | None = None
    capture_setup: CaptureSetupSnapshot | None = None
    activity: ExecutionActivity = ExecutionActivity.NONE
    ticket_phase: str | None = None
    main_succeeded: bool | None = None

    def __post_init__(self) -> None:
        if type(self.runtime_generation) is not int or self.runtime_generation <= 0:
            raise ValueError("runtime generation must be positive")
        if type(self.command_id) is not int or self.command_id < 0:
            raise ValueError("MAIN command ID must be nonnegative")
        if self.main_phase is not None and not isinstance(self.main_phase, MainPhase):
            raise TypeError("MAIN phase is invalid")
        if (self.capture_context_state is None) != (
            self.capture_frame_identity is None
        ):
            raise ValueError("capture context and frame identity must be observed together")
        if self.capture_context_state is not None and not isinstance(
            self.capture_context_state, CaptureContextState
        ):
            raise TypeError("capture context state is invalid")
        if self.capture_frame_identity is not None and not isinstance(
            self.capture_frame_identity, CaptureFrameIdentity
        ):
            raise TypeError("capture frame identity is invalid")
        if self.capture_setup is not None and not isinstance(
            self.capture_setup, CaptureSetupSnapshot
        ):
            raise TypeError("capture setup snapshot is invalid")
        if not isinstance(self.activity, ExecutionActivity):
            raise TypeError("execution activity is invalid")
        if self.ticket_phase not in (
            None, "queued", "running", "reconciling", "unknown", "settled",
        ):
            raise ValueError("owned ticket phase is invalid")
        if self.main_succeeded is not None and type(self.main_succeeded) is not bool:
            raise TypeError("MAIN result flag is invalid")


class ExecutionStatusProjection:
    """Build existing RuntimeStatus and proxy generation snapshots locally.

    The injected controller reader must return one immutable snapshot. The
    Worker reader returns one published generation snapshot; the namespace
    owner supplies confirmed names and the runtime/context generation fence.
    This layer performs no RDBG command and never waits for a remote event.
    """

    def __init__(
        self,
        *,
        controller_facts: Callable[[], ControllerStatusFacts],
        worker_snapshot: Callable[[], WorkerActivationSnapshot],
        namespace: RuntimeNamespaceOwner,
    ) -> None:
        if not callable(controller_facts) or not callable(worker_snapshot):
            raise TypeError("controller and Worker readers must be callable")
        if not isinstance(namespace, RuntimeNamespaceOwner):
            raise TypeError("runtime namespace owner is required")
        self._controller_facts = controller_facts
        self._worker_snapshot = worker_snapshot
        self._namespace = namespace

    def status(self) -> RuntimeStatus:
        """Return a local observation without inferring loss from a timeout."""

        from onec_runtime.runtime_models import RuntimeStatus

        facts = self._read_facts()
        worker = self._worker_snapshot()
        if not isinstance(worker, WorkerActivationSnapshot):
            raise TypeError("Worker snapshot reader returned an invalid value")
        namespace = self._read_namespace(facts)
        handle = worker.active_handle
        if handle is not None and (
            handle.runtime_generation != facts.runtime_generation
            or handle.context_generation != namespace.context_generation
        ):
            raise ProtocolError("Worker generation belongs to another runtime context")
        setup = facts.capture_setup
        if facts.capture_context_state is CaptureContextState.READY:
            setup = None
        return RuntimeStatus(
            _state(facts), facts.runtime_generation, facts.command_id,
            handle, setup,
        )

    def namespace_snapshot(self) -> RuntimeNamespaceSnapshot:
        """Expose only confirmed names and the exact proxy generation fence."""

        return self._read_namespace(self._read_facts())

    def _read_facts(self) -> ControllerStatusFacts:
        facts = self._controller_facts()
        if not isinstance(facts, ControllerStatusFacts):
            raise TypeError("controller status reader returned invalid facts")
        return facts

    def _read_namespace(self, facts: ControllerStatusFacts) -> RuntimeNamespaceSnapshot:
        from onec_runtime.runtime_models import RuntimeNamespaceSnapshot

        namespace = self._namespace.namespace_snapshot()
        if not isinstance(namespace, RuntimeNamespaceSnapshot):
            raise TypeError("namespace owner returned invalid snapshot")
        if namespace.runtime_generation != facts.runtime_generation:
            raise ProtocolError("namespace belongs to another runtime generation")
        return namespace


def _state(facts: ControllerStatusFacts):
    from onec_runtime.runtime_models import OperationState

    if (
        facts.main_phase is MainPhase.LOST
        or facts.capture_frame_identity is CaptureFrameIdentity.LOST
    ):
        return OperationState.LOST
    if facts.ticket_phase in {"unknown", "reconciling"} or facts.main_phase is MainPhase.UNKNOWN:
        return OperationState.RECOVERING
    if facts.capture_context_state is CaptureContextState.SETUP_FAILED:
        return OperationState.CAPTURE_SETUP_FAILED
    active = facts.ticket_phase in {"queued", "running"}
    if active and facts.activity is ExecutionActivity.CAPTURE_RESUME:
        return OperationState.RESUMING
    if active and facts.activity is ExecutionActivity.CAPTURE:
        return OperationState.EVALUATING_CAPTURE
    if facts.main_phase is MainPhase.SUSPENDED_CAPTURE:
        if (
            facts.capture_context_state is CaptureContextState.READY
            and facts.capture_frame_identity is CaptureFrameIdentity.CONFIRMED
        ):
            return OperationState.CAPTURED
        return OperationState.RECOVERING
    if facts.main_phase is MainPhase.SUSPENDED_USER:
        return OperationState.MAIN_PENDING if active else OperationState.DEBUG_STOPPED
    if facts.main_phase in {MainPhase.ADMITTED, MainPhase.RUNNING}:
        return OperationState.MAIN_PENDING
    if facts.main_phase is MainPhase.COMPLETED:
        return (
            OperationState.FAILED if facts.main_succeeded is False
            else OperationState.COMPLETED
        )
    if facts.main_phase is MainPhase.FAILED_BEFORE_DISPATCH:
        return OperationState.FAILED
    return OperationState.IDLE
