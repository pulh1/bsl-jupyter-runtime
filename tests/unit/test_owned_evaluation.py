"""A typed pending eval remains owned across request intervals and stops."""

from uuid import UUID

import pytest

from onec_runtime.errors import CommandTimeout, RdbgTransportTimeout
from onec_runtime.execution.evaluation import EvaluationSuspended, evaluate_until_result
from onec_runtime.rdbg.models import (
    EvaluationResult,
    ModuleLocation,
    PendingEvaluation,
    StopEvent,
    TargetId,
)


TARGET = TargetId(UUID(int=1), "test")
LOCATION = ModuleLocation("ExtensionModule", "", UUID(int=2), UUID(int=3), 7)
PENDING = PendingEvaluation(TARGET, UUID(int=4), object())
RESULT = EvaluationResult(PENDING.result_id, "Число", "42", False, "")
STOP = StopEvent(TARGET, LOCATION, "breakpoint")


class EvaluationPort:
    def __init__(self, events: list[object]) -> None:
        self.events = events
        self.starts: list[tuple[object, ...]] = []
        self.waits: list[tuple[PendingEvaluation, float]] = []

    def start_evaluation(
        self,
        expression: str,
        *,
        max_text_size: int,
        stack_level: int,
        timeout_s: float,
    ) -> PendingEvaluation:
        self.starts.append((expression, max_text_size, stack_level, timeout_s))
        return PENDING

    def wait_evaluation_event(
        self, pending: PendingEvaluation, *, timeout_s: float
    ) -> EvaluationResult | StopEvent:
        self.waits.append((pending, timeout_s))
        event = self.events.pop(0)
        if isinstance(event, BaseException):
            raise event
        assert isinstance(event, (EvaluationResult, StopEvent))
        return event


def test_owned_eval_waits_multiple_intervals_without_resending_expression() -> None:
    port = EvaluationPort([CommandTimeout("interval") for _ in range(3)] + [RESULT])

    result = evaluate_until_result(
        port,
        "2 + 40",
        stack_level=2,
        request_timeout_s=10,
        wait_interval_s=0.25,
    )

    assert result is RESULT
    assert port.starts == [("2 + 40", 307_200, 2, 10)]
    assert port.waits == [(PENDING, 0.25)] * 4


def test_owned_eval_stop_exposes_the_same_pending_capability() -> None:
    port = EvaluationPort([STOP])

    with pytest.raises(EvaluationSuspended) as caught:
        evaluate_until_result(port, "Считать()", wait_interval_s=0.25)

    assert caught.value.pending is PENDING
    assert caught.value.stop is STOP
    assert len(port.starts) == 1
    assert port.waits == [(PENDING, 0.25)]


def test_owned_eval_does_not_treat_transport_timeout_as_empty_interval() -> None:
    port = EvaluationPort([RdbgTransportTimeout("network request failed")])

    with pytest.raises(RdbgTransportTimeout, match="network request failed"):
        evaluate_until_result(port, "Считать()", wait_interval_s=0.25)

    assert len(port.starts) == 1
    assert port.waits == [(PENDING, 0.25)]
