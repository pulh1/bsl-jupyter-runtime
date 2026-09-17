"""Evidence and setup progress for one stopped CAPTURE frame.

The scope records what was proved about the stop independently of a cell's
result or the debugger protocol's pending evaluation capability.
"""

from dataclasses import dataclass, field
from enum import Enum

from onec_runtime.rdbg.models import (
    FrameVariable,
    ModuleLocation,
    StackFrame,
    StopEvent,
    TargetId,
)


class CaptureFrameIdentity(str, Enum):
    UNVERIFIED = "unverified"
    CONFIRMED = "confirmed"
    RELEASED = "released"
    LOST = "lost"


class CaptureContextState(str, Enum):
    OPENING = "opening"
    READY = "ready"
    SETUP_FAILED = "setup_failed"
    CLOSING = "closing"
    CLOSED = "closed"


class CaptureSetupStage(str, Enum):
    STOP_RECOGNIZED = "stop_recognized"
    LOCALS_READ = "locals_read"
    CONTEXT_TRANSFERRED = "context_transferred"
    KERNEL_FRAME_FOUND = "kernel_frame_found"
    MAIN_ID_CONFIRMED = "main_id_confirmed"
    CONTEXT_BEGUN = "context_begun"
    CONTEXT_OPENED = "context_opened"


@dataclass(frozen=True, slots=True)
class CaptureStopIdentity:
    runtime_generation: int
    main_command_id: int
    target_id: TargetId
    location: ModuleLocation
    local_stop_sequence: int

    def __post_init__(self) -> None:
        if (
            self.runtime_generation <= 0
            or self.main_command_id <= 0
            or self.local_stop_sequence <= 0
        ):
            raise ValueError("capture stop identity has invalid sequence")


@dataclass(frozen=True, slots=True)
class CaptureSetupSnapshot:
    """Bounded public evidence for an unfinished CAPTURE stop."""

    setup_stage: CaptureSetupStage
    context_state: CaptureContextState
    frame_identity: CaptureFrameIdentity
    error_code: str | None


@dataclass(slots=True)
class CaptureScope:
    """One recognized stop, including partial setup when opening fails."""

    identity: CaptureStopIdentity
    stop: StopEvent = field(repr=False)
    setup_stage: CaptureSetupStage = field(
        default=CaptureSetupStage.STOP_RECOGNIZED, init=False
    )
    context_state: CaptureContextState = field(
        default=CaptureContextState.OPENING, init=False
    )
    frame_identity: CaptureFrameIdentity = field(
        default=CaptureFrameIdentity.UNVERIFIED, init=False
    )
    setup_error_code: str | None = field(default=None, init=False)
    frame_variables: tuple[FrameVariable, ...] = field(default=(), init=False, repr=False)
    transfer_address: str | None = field(default=None, init=False, repr=False)
    kernel_stack_level: int | None = field(default=None, init=False)
    observed_main_command_id: int | None = field(default=None, init=False)
    frame_stack_level: int | None = field(default=None, init=False)
    stack_frames: tuple[StackFrame, ...] = field(default=(), init=False, repr=False)
    inspection_target_id: TargetId | None = field(default=None, init=False)
    published: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if (
            self.identity.target_id != self.stop.target_id
            or self.identity.location != self.stop.location
        ):
            raise ValueError("capture identity does not match the debugger stop")

    @classmethod
    def from_stop(
        cls,
        runtime_generation: int,
        main_command_id: int,
        stop: StopEvent,
        local_stop_sequence: int,
    ) -> "CaptureScope":
        return cls(
            CaptureStopIdentity(
                runtime_generation,
                main_command_id,
                stop.target_id,
                stop.location,
                local_stop_sequence,
            ),
            stop,
        )

    def record_locals(self, variables: tuple[FrameVariable, ...]) -> None:
        self.frame_variables = tuple(variables)
        self.setup_stage = CaptureSetupStage.LOCALS_READ

    def record_transfer(self, address: str) -> None:
        if not address:
            raise ValueError("capture transfer address is empty")
        self.transfer_address = address
        self.setup_stage = CaptureSetupStage.CONTEXT_TRANSFERRED

    def record_kernel_frame(self, stack_level: int) -> None:
        if stack_level <= 0:
            raise ValueError("capture kernel frame level must be positive")
        self.kernel_stack_level = stack_level
        self.setup_stage = CaptureSetupStage.KERNEL_FRAME_FOUND

    def record_main_command(self, observed_command_id: int) -> bool:
        self.observed_main_command_id = observed_command_id
        if observed_command_id != self.identity.main_command_id:
            return False
        self.frame_identity = CaptureFrameIdentity.CONFIRMED
        self.setup_stage = CaptureSetupStage.MAIN_ID_CONFIRMED
        return True

    def record_context_begun(self) -> None:
        if self.setup_stage is not CaptureSetupStage.MAIN_ID_CONFIRMED:
            raise RuntimeError("capture frame has no confirmed MAIN command")
        self.setup_stage = CaptureSetupStage.CONTEXT_BEGUN

    def mark_ready(self) -> None:
        if self.setup_stage is not CaptureSetupStage.CONTEXT_BEGUN:
            if self.setup_stage is not CaptureSetupStage.MAIN_ID_CONFIRMED:
                raise RuntimeError("capture frame has no confirmed MAIN command")
            raise RuntimeError("capture context has not begun")
        self.frame_stack_level = 0
        self.stack_frames = tuple(self.stop.stack_frames)
        self.inspection_target_id = self.identity.target_id
        self.context_state = CaptureContextState.READY
        self.setup_stage = CaptureSetupStage.CONTEXT_OPENED
        self.published = True

    def fail_setup(self, error: BaseException) -> None:
        self.context_state = CaptureContextState.SETUP_FAILED
        self.setup_error_code = type(error).__name__

    def setup_snapshot(self) -> CaptureSetupSnapshot:
        return CaptureSetupSnapshot(
            self.setup_stage,
            self.context_state,
            self.frame_identity,
            self.setup_error_code,
        )

    def note_setup_uncertain(self, error: BaseException) -> None:
        """A transport exception does not prove the remote setup step failed."""
        self.setup_error_code = type(error).__name__

    def invalidate_inspection(self) -> None:
        self.frame_stack_level = None
        self.frame_variables = ()
        self.stack_frames = ()
        self.inspection_target_id = None

    def mark_closed(self) -> None:
        self.context_state = CaptureContextState.CLOSED
        # Acknowledged Continue releases this frame; the target may live and
        # stop again. Its recognized-stop evidence remains in identity/stop.
        self.frame_identity = CaptureFrameIdentity.RELEASED
        self.invalidate_inspection()

    def mark_unverified(self) -> None:
        self.frame_identity = CaptureFrameIdentity.UNVERIFIED
        self.context_state = CaptureContextState.CLOSING
        self.invalidate_inspection()
