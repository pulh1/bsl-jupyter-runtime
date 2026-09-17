"""Wait for one owned RDBG evaluation without a BSL execution deadline.

The caller's port retains the pending capability. An empty wait interval may
repeat, but a transport failure or debugger stop is returned to the operation
owner with the same capability for reconciliation.
"""

from __future__ import annotations

from math import isfinite
from typing import Protocol

from onec_runtime.errors import CommandTimeout
from onec_runtime.rdbg.models import EvaluationResult, PendingEvaluation, StopEvent


class EvaluationPort(Protocol):
    def start_evaluation(
        self,
        expression: str,
        *,
        max_text_size: int,
        stack_level: int,
        timeout_s: float,
    ) -> PendingEvaluation: ...

    def wait_evaluation_event(
        self, pending: PendingEvaluation, *, timeout_s: float
    ) -> EvaluationResult | StopEvent: ...


class EvaluationSuspended(RuntimeError):
    """The same pending expression is stopped at a debugger frame."""

    def __init__(self, pending: PendingEvaluation, stop: StopEvent) -> None:
        super().__init__("Expression evaluation stopped at a debugger frame")
        self.pending = pending
        self.stop = stop


def evaluate_until_result(
    port: EvaluationPort,
    expression: str,
    *,
    stack_level: int = 0,
    max_text_size: int = 307_200,
    request_timeout_s: float = 30.0,
    wait_interval_s: float = 6.0,
) -> EvaluationResult:
    """Dispatch once; repeat bounded event waits for the *same* capability.

    ``request_timeout_s`` bounds the initial HTTP request and
    ``wait_interval_s`` bounds each event poll. Neither is a deadline for the
    BSL expression. A transport exception propagates for reconciliation.
    """

    if (
        isinstance(wait_interval_s, bool)
        or not isinstance(wait_interval_s, (int, float))
        or not isfinite(float(wait_interval_s))
        or wait_interval_s <= 0
    ):
        raise ValueError("evaluation wait interval must be finite and positive")
    pending = port.start_evaluation(
        expression,
        max_text_size=max_text_size,
        stack_level=stack_level,
        timeout_s=request_timeout_s,
    )
    while True:
        try:
            event = port.wait_evaluation_event(pending, timeout_s=wait_interval_s)
        except CommandTimeout as error:
            if type(error) is CommandTimeout:
                continue
            raise
        if isinstance(event, StopEvent):
            raise EvaluationSuspended(pending, event)
        return event
