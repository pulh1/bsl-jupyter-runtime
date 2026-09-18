"""Lifetime of one MAIN command, independent of any request waiter."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from onec_runtime.rdbg.models import StopEvent, TargetId


class MainPhase(str, Enum):
    ADMITTED = "admitted"
    RUNNING = "running"
    SUSPENDED_CAPTURE = "suspended_capture"
    SUSPENDED_USER = "suspended_user"
    COMPLETED = "completed"
    UNKNOWN = "unknown"
    LOST = "lost"
    FAILED_BEFORE_DISPATCH = "failed_before_dispatch"


@dataclass(slots=True)
class MainOperation:
    command_id: int
    target: TargetId | None
    settler: Callable[[object], object] | None = field(default=None, repr=False)
    message_collector_key: str = field(default="", repr=False)
    phase: MainPhase = field(default=MainPhase.ADMITTED, init=False)
    # Set only by the Continue transport-entry callback. Admission, Worker
    # activation and command writes do not prove user MAIN was dispatched.
    command_dispatch_attempted: bool = field(default=False, init=False)
    pending_stop: StopEvent | None = field(default=None, init=False, repr=False)
    completion: object | None = field(default=None, init=False, repr=False)

    @property
    def terminal(self) -> bool:
        return self.phase in {
            MainPhase.COMPLETED, MainPhase.LOST, MainPhase.FAILED_BEFORE_DISPATCH
        }

    def _require_live(self) -> None:
        if self.terminal:
            raise RuntimeError("MAIN operation is already terminal")

    def command_write_requested(self) -> None:
        """Retain an unresolved command-field write without claiming Continue."""
        self._require_live()
        self.phase = MainPhase.UNKNOWN

    def command_write_acknowledged(self) -> None:
        """A confirmed field result restores pre-Continue admission."""
        self._require_live()
        if not self.command_dispatch_attempted:
            self.phase = MainPhase.ADMITTED

    def continue_requested(self) -> None:
        self._require_live()
        # Until acknowledgement, the previous frame may already have gone.
        self.command_dispatch_attempted = True
        self.phase = MainPhase.UNKNOWN
        self.pending_stop = None

    def continue_acknowledged(self) -> None:
        self._require_live()
        self.phase = MainPhase.RUNNING
        self.pending_stop = None

    def stopped(self, stop: StopEvent, phase: MainPhase) -> None:
        self._require_live()
        if phase not in {
            MainPhase.SUSPENDED_CAPTURE, MainPhase.SUSPENDED_USER, MainPhase.UNKNOWN
        }:
            raise ValueError("Invalid MAIN stop phase")
        if self.target is not None and stop.target_id != self.target:
            self.mark_unknown()
            raise ValueError("MAIN stop belongs to another target")
        self.target = stop.target_id
        self.pending_stop = stop
        self.phase = phase

    def complete(self, completion: object) -> None:
        if self.phase is not MainPhase.COMPLETED:
            self._require_live()
        elif self.completion is not None:
            raise RuntimeError("MAIN completion is already published")
        self.completion = completion
        self.pending_stop = None
        self.phase = MainPhase.COMPLETED

    def remote_completed(self) -> None:
        """Matching remote command ID proves completion before value decoding."""
        self._require_live()
        self.pending_stop = None
        self.phase = MainPhase.COMPLETED

    def fail_before_dispatch(self) -> None:
        """Terminate only after local failure or confirmed writes before Continue."""
        if self.phase is not MainPhase.ADMITTED:
            raise RuntimeError("MAIN dispatch has already been attempted")
        self.phase = MainPhase.FAILED_BEFORE_DISPATCH

    def mark_unknown(self) -> None:
        self._require_live()
        self.phase = MainPhase.UNKNOWN

    def mark_lost(self) -> None:
        """Call only after confirmed target loss, never for a lost waiter/frame."""
        self._require_live()
        self.pending_stop = None
        self.phase = MainPhase.LOST
