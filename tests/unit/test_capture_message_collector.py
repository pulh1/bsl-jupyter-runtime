"""CAPTURE messages are read through the ticket's owned evaluation port."""

from __future__ import annotations

import json
from uuid import UUID

import pytest

from onec_runtime.errors import BslExecutionError, CommandTimeout, ProtocolError, RdbgTransportTimeout
from onec_runtime.execution.capture.messages import CaptureMessageCollector
from onec_runtime.execution.reply_publication import CaptureRemoteOutcome
from onec_runtime.rdbg.models import EvaluationResult, PendingEvaluation, TargetId


TARGET = TargetId(UUID(int=1), "test")
PENDING = PendingEvaluation(TARGET, UUID(int=2), object())
PRIMARY = EvaluationResult(UUID(int=3), "Неопределено", "", True, "user BSL failed")


def encoded_messages(messages: object) -> EvaluationResult:
    return EvaluationResult(
        PENDING.result_id, "Строка", "", False, value_string=json.dumps(messages),
    )


class OwnedPort:
    def __init__(self, events: list[object]) -> None:
        self.events = events
        self.starts: list[tuple[str, int, int, float]] = []
        self.waits: list[tuple[PendingEvaluation, float]] = []

    def start_evaluation(
        self, expression: str, *, max_text_size: int, stack_level: int, timeout_s: float,
    ) -> PendingEvaluation:
        self.starts.append((expression, max_text_size, stack_level, timeout_s))
        return PENDING

    def wait_evaluation_event(
        self, pending: PendingEvaluation, *, timeout_s: float,
    ) -> EvaluationResult:
        assert pending is PENDING
        self.waits.append((pending, timeout_s))
        event = self.events.pop(0)
        if isinstance(event, BaseException):
            raise event
        assert isinstance(event, EvaluationResult)
        return event


def test_collects_messages_after_confirmed_user_error_on_the_kernel_frame() -> None:
    port = OwnedPort([CommandTimeout("empty interval"), encoded_messages(["first", "second"])])

    outcome = CaptureMessageCollector(
        request_timeout_s=3, wait_interval_s=0.25,
    ).collect(port, PRIMARY, message_collector_key='__cell_"1', stack_level=2)

    assert isinstance(outcome, CaptureRemoteOutcome)
    assert outcome.evaluation is PRIMARY
    assert outcome.messages == ("first", "second")
    assert port.starts == [
        (
            'RuntimeKernelServer.ЗабратьСообщенияЯчейкиИзКонтекста(e1cRuntimeКонтекст, "__cell_""1")',
            307_200, 2, 3,
        )
    ]
    assert port.waits == [(PENDING, 0.25), (PENDING, 0.25)]


def test_empty_key_returns_confirmed_result_without_remote_dispatch() -> None:
    port = OwnedPort([])

    outcome = CaptureMessageCollector().collect(
        port, PRIMARY, message_collector_key="", stack_level=2,
    )

    assert outcome.evaluation is PRIMARY
    assert outcome.messages == ()
    assert port.starts == []


def test_reconciled_collector_result_can_be_decoded_without_second_dispatch() -> None:
    port = OwnedPort([])

    outcome = CaptureMessageCollector().complete(
        PRIMARY, encoded_messages(["late sealed message"]),
    )

    assert outcome.evaluation is PRIMARY
    assert outcome.messages == ("late sealed message",)
    assert port.starts == []


@pytest.mark.parametrize(
    "event, error_type",
    [
        (EvaluationResult(PENDING.result_id, "Неопределено", "", True, "collector BSL failed"), BslExecutionError),
        (EvaluationResult(PENDING.result_id, "Число", "1", False), ProtocolError),
        (EvaluationResult(PENDING.result_id, "Строка", '"invalid json"', False), ProtocolError),
        (encoded_messages({"not": "an array"}), ProtocolError),
        (encoded_messages(["ok", 1]), ProtocolError),
    ],
)
def test_confirmed_collector_failure_is_an_operation_error(
    event: EvaluationResult, error_type: type[BaseException],
) -> None:
    port = OwnedPort([event])

    with pytest.raises(error_type):
        CaptureMessageCollector().collect(
            port, PRIMARY, message_collector_key="__messages", stack_level=2,
        )

    assert len(port.starts) == 1


def test_message_result_is_bounded_without_silently_dropping_messages() -> None:
    port = OwnedPort([encoded_messages(["m"] * 101)])

    with pytest.raises(ProtocolError, match="limit"):
        CaptureMessageCollector().collect(
            port, PRIMARY, message_collector_key="__messages", stack_level=2,
        )


def test_oversized_message_is_reported_as_a_confirmed_operation_error() -> None:
    port = OwnedPort([encoded_messages(["x" * 1025])])

    with pytest.raises(ProtocolError, match="limit"):
        CaptureMessageCollector().collect(
            port, PRIMARY, message_collector_key="__messages", stack_level=2,
        )


def test_confirmed_malformed_scalar_and_control_characters_are_bounded() -> None:
    malformed = EvaluationResult(PENDING.result_id, "Число", "not a number", False)
    collector = CaptureMessageCollector()
    with pytest.raises(ProtocolError, match="cannot be decoded"):
        collector.complete(PRIMARY, malformed)

    outcome = collector.complete(PRIMARY, encoded_messages([" first\u001b  line\nsecond "]))
    assert outcome.messages == ("first line second",)


def test_ambiguous_collector_transport_retains_the_single_remote_dispatch() -> None:
    error = RdbgTransportTimeout("unknown collector outcome")
    port = OwnedPort([error])

    with pytest.raises(RdbgTransportTimeout) as caught:
        CaptureMessageCollector().collect(
            port, PRIMARY, message_collector_key="__messages", stack_level=2,
        )

    assert caught.value is error
    assert len(port.starts) == 1
    assert port.waits == [(PENDING, 6.0)]
