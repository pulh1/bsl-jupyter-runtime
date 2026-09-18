"""Public runtime state and reply contracts shared by execution adapters."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from onec_runtime.bsl import NormalizedDiagnostic
from onec_runtime.errors import ProtocolError
from onec_runtime.execution.capture import CaptureSetupSnapshot
from onec_runtime.rdbg.models import ModuleLocation
from onec_runtime.worker_breakpoints import RuntimeDebugStop, WorkerSourceLocation
from onec_runtime.worker_universe import WorkerGenerationHandle


class OperationState(Enum):
    IDLE = "idle"
    MAIN_PENDING = "main_pending"
    CAPTURED = "captured"
    CAPTURE_SETUP_FAILED = "capture_setup_failed"
    EVALUATING_CAPTURE = "evaluating_capture"
    DEBUG_STOPPED = "debug_stopped"
    FLUSHING = "flushing"
    PARTIAL_WRITEBACK_FAILURE = "partial_writeback_failure"
    BREAKPOINT_RESTORE_FAILURE = "breakpoint_restore_failure"
    RESUMING = "resuming"
    RECOVERING = "recovering"
    LOST = "lost"
    COMPLETED = "completed"
    FAILED = "failed"


class PartialWritebackError(ProtocolError):
    """At least one staged capture root could not be written to the frame."""


class RuntimeReplyKind(Enum):
    SOURCE_FAILED = "source_failed"
    MAIN_COMPLETED = "main_completed"
    CAPTURED = "captured"
    CAPTURE_CELL = "capture_cell"
    DEBUG_STOPPED = "debug_stopped"
    WORKER_LOADED = "worker_loaded"


@dataclass(frozen=True, slots=True)
class RuntimeReply:
    kind: RuntimeReplyKind
    operation_id: int
    state: OperationState
    result: object = None
    error: str = ""
    succeeded: bool = True
    location: ModuleLocation | WorkerSourceLocation | None = None
    stop_sequence: int | None = None
    messages: tuple[str, ...] = ()
    changed_roots: tuple[str, ...] = ()
    capture_ticket: str | None = None
    observed_command_id: int | None = None
    capture_dirty_roots: tuple[str, ...] = ()
    diagnostic: NormalizedDiagnostic | None = None
    debug_stop: RuntimeDebugStop | None = None


@dataclass(frozen=True, slots=True)
class CaptureCorrelationTicket:
    """Opaque runtime-owned evidence for one armed next-MAIN capture."""

    ticket_id: str
    expected_operation_id: int
    expected_stop_sequence: int


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    state: OperationState
    runtime_generation: int
    operation_id: int
    worker_generation: WorkerGenerationHandle | None
    capture_setup: CaptureSetupSnapshot | None = None


@dataclass(frozen=True, slots=True)
class RuntimeNamespaceSnapshot:
    runtime_generation: int
    context_generation: int
    names: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.runtime_generation <= 0 or self.context_generation <= 0:
            raise ValueError("runtime namespace generations must be positive")
        if any(not name.strip() for name in self.names):
            raise ValueError("runtime namespace name must not be empty")
        normalized = tuple(name.casefold() for name in self.names)
        if len(set(normalized)) != len(normalized):
            raise ValueError("runtime namespace names must be case-insensitively unique")
