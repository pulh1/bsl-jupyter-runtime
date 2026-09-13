from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from onec_runtime.rdbg.models import ModuleLocation, StopEvent


class StopReason(Enum):
    MAIN_SERVICE = "main_service"
    CAPTURE = "capture"
    USER_BREAKPOINT = "user_breakpoint"
    PAUSE = "pause"
    EXCEPTION = "exception"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class BreakpointRegistry:
    service: ModuleLocation
    captures: tuple[ModuleLocation, ...] = ()
    users: tuple[ModuleLocation, ...] = ()

    def __post_init__(self) -> None:
        locations = self.full_locations
        if len(set(locations)) != len(locations):
            raise ValueError("Breakpoint registry groups overlap")

    @property
    def full_locations(self) -> tuple[ModuleLocation, ...]:
        return (self.service, *self.captures, *self.users)

    @property
    def evaluation_locations(self) -> tuple[ModuleLocation, ...]:
        return (self.service, *self.users)


@dataclass(frozen=True, slots=True)
class ClassifiedStop:
    event: StopEvent
    reason: StopReason


def classify_stop(
    event: StopEvent,
    registry: BreakpointRegistry,
    *,
    worker_locations: tuple[ModuleLocation, ...] = (),
) -> ClassifiedStop:
    if event.runtime_error:
        reason = StopReason.EXCEPTION
    elif event.suspended_by_other is True:
        reason = StopReason.PAUSE
    elif event.stop_by_breakpoint is True:
        if event.location == registry.service:
            reason = StopReason.MAIN_SERVICE
        elif event.location in registry.captures:
            reason = StopReason.CAPTURE
        elif event.location in registry.users or event.location in worker_locations:
            reason = StopReason.USER_BREAKPOINT
        else:
            reason = StopReason.UNKNOWN
    else:
        reason = StopReason.UNKNOWN
    return ClassifiedStop(event, reason)
