from __future__ import annotations

from enum import Enum
from typing import Callable

from onec_runtime.errors import RdbgTransportError


class FaultPoint(str, Enum):
    AFTER_CAPTURE_CHECKPOINT = "after_capture_checkpoint"
    AFTER_FIRST_ROOT_WRITE = "after_first_root_write"
    AFTER_CONTINUE_ACK = "after_continue_ack"


class InjectedTransportFailure(RdbgTransportError):
    """A deterministic transport loss at a tested lifecycle boundary."""


class CloseTransportAt:
    def __init__(self, point: FaultPoint, close: Callable[[], None]) -> None:
        self.point = point
        self.close = close
        self.fired = False

    def __call__(self, point: FaultPoint) -> None:
        if self.fired or point is not self.point:
            return
        self.fired = True
        self.close()
        raise InjectedTransportFailure(f"Injected fault at {point.value}")
