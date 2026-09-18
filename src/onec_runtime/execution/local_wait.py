"""Limit an initiating caller's ticket wait without cancelling remote work."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from math import isfinite
from typing import Protocol, TypeVar


_T = TypeVar("_T")


class LocalWaitTicket(Protocol[_T]):
    def wait_initiator(self, timeout: float | None = None) -> _T: ...
    def detach_waiter(self) -> None: ...
    def status(self) -> LocalWaitStatus: ...


class LocalWaitStatus(Protocol):
    settled: bool


def validate_local_wait_timeout(timeout_s: float | None) -> float | None:
    """Accept a finite positive local wait duration, independent of RDBG work."""

    if timeout_s is None:
        return None
    if (
        isinstance(timeout_s, bool)
        or not isinstance(timeout_s, (int, float))
        or not isfinite(float(timeout_s))
        or timeout_s <= 0
    ):
        raise ValueError("timeout_s must be a finite positive number")
    return float(timeout_s)


def wait_initiator_locally(
    ticket: LocalWaitTicket[_T],
    *,
    timeout_s: float | None,
    wait_handoff: Callable[[], AbstractContextManager[None]],
) -> _T:
    """Detach a timed-out or interrupted waiter; leave its ticket owned."""

    selected = validate_local_wait_timeout(timeout_s)
    try:
        with wait_handoff():
            if selected is None:
                return ticket.wait_initiator()
            return ticket.wait_initiator(selected)
    except TimeoutError:
        # The ticket's settled error can itself be TimeoutError. Only an
        # unsettled ticket has outlived this caller's local wait interval.
        if not ticket.status().settled:
            ticket.detach_waiter()
        raise
    except KeyboardInterrupt:
        ticket.detach_waiter()
        raise
