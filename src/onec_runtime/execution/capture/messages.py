"""Read one CAPTURE cell's messages through its arbiter-owned RDBG port.

The caller must run this after the primary eval has a matched result, within
the same ticket's worker plan. A pending message eval remains owned by that
ticket; this module never retries an ambiguous dispatch or transport failure.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from onec_runtime.capture_evaluation import (
    MAX_CAPTURE_MESSAGE_CODEPOINTS, MAX_CAPTURE_MESSAGES,
)
from onec_runtime.errors import BslExecutionError, ProtocolError
from onec_runtime.execution.evaluation import (
    EvaluationPort, evaluate_until_result, wait_for_pending_result,
)
from onec_runtime.experiment import bsl_string_literal
from onec_runtime.rdbg.models import EvaluationResult, PendingEvaluation
from onec_runtime.table_value import evaluation_to_python

if TYPE_CHECKING:
    from onec_runtime.execution.reply_publication import CaptureRemoteOutcome


class CaptureMessageDecodeFailure(ProtocolError):
    """A matched collector result was invalid; its remote eval is retired."""


class CaptureMessageBslFailure(BslExecutionError):
    """The collector helper returned a confirmed BSL error."""


class CaptureMessageCollector:
    """Collect bounded messages without changing the stopped CAPTURE frame.

    Confirmed collector BSL/decode failures are ordinary cell failures. A
    transport exception or debugger stop propagates unchanged so the arbiter
    retains the exact pending expression for reconciliation. The caller must
    not invoke this from a result-policy callback that swallows exceptions.
    """

    def __init__(
        self, *, request_timeout_s: float = 30.0,
        wait_interval_s: float = 6.0,
        max_text_size: int = 307_200,
    ) -> None:
        if type(max_text_size) is not int or max_text_size <= 0:
            raise ValueError("CAPTURE message text limit must be positive")
        self._request_timeout_s = request_timeout_s
        self._wait_interval_s = wait_interval_s
        self._max_text_size = max_text_size

    def collect(
        self,
        port: EvaluationPort,
        evaluation: EvaluationResult,
        *,
        message_collector_key: str,
        stack_level: int,
    ) -> CaptureRemoteOutcome:
        """Return the confirmed user eval plus messages sealed on the same frame.

        The primary result may contain a BSL error: its messages still belong
        in the published cell reply. A missing key means the source did not
        install a collector and causes no remote request.
        """

        if not isinstance(evaluation, EvaluationResult):
            raise TypeError("CAPTURE user evaluation must be confirmed")
        if type(message_collector_key) is not str:
            raise TypeError("CAPTURE message key must be text")
        if type(stack_level) is not int or stack_level < 0:
            raise ValueError("CAPTURE kernel stack level must be non-negative")
        if not message_collector_key:
            from onec_runtime.execution.reply_publication import CaptureRemoteOutcome

            return CaptureRemoteOutcome(evaluation, ())
        expression = (
            "RuntimeKernelServer.ЗабратьСообщенияЯчейкиИзКонтекста(Контекст, "
            + bsl_string_literal(message_collector_key)
            + ")"
        )
        message_result = evaluate_until_result(
            port,
            expression,
            stack_level=stack_level,
            max_text_size=self._max_text_size,
            request_timeout_s=self._request_timeout_s,
            wait_interval_s=self._wait_interval_s,
        )
        return self.complete(evaluation, message_result)

    def complete(
        self, evaluation: EvaluationResult, message_result: EvaluationResult,
    ) -> CaptureRemoteOutcome:
        """Decode a matched collector result after reconciling its pending eval.

        This method performs no RDBG work, so reconciliation never sends the
        collector expression a second time.
        """

        if not isinstance(evaluation, EvaluationResult):
            raise TypeError("CAPTURE user evaluation must be confirmed")
        if not isinstance(message_result, EvaluationResult):
            raise TypeError("CAPTURE message evaluation must be confirmed")
        # Publication imports the controller, which composes this collector.
        from onec_runtime.execution.reply_publication import CaptureRemoteOutcome

        return CaptureRemoteOutcome(evaluation, self._decode(message_result))

    def await_pending_result(
        self, port: EvaluationPort, pending: PendingEvaluation,
    ) -> EvaluationResult:
        """Wait for the same collector expression; never send another eval."""

        return wait_for_pending_result(
            port, pending, wait_interval_s=self._wait_interval_s,
        )

    def _decode(self, result: EvaluationResult) -> tuple[str, ...]:
        if result.error_occurred:
            raise CaptureMessageBslFailure(result.error_text)
        try:
            payload = evaluation_to_python(result)
        except Exception as error:
            raise CaptureMessageDecodeFailure(
                "Cell message scalar cannot be decoded"
            ) from error
        if not isinstance(payload, str):
            raise CaptureMessageDecodeFailure("Cell message payload is not JSON text")
        if len(payload) > self._max_text_size:
            raise CaptureMessageDecodeFailure("Cell message payload exceeds text limit")
        try:
            messages = json.loads(payload)
        except json.JSONDecodeError as error:
            raise CaptureMessageDecodeFailure("Cell message payload is invalid JSON") from error
        if not isinstance(messages, list) or any(
            not isinstance(message, str) for message in messages
        ):
            raise CaptureMessageDecodeFailure("Cell message payload is not a text array")
        if len(messages) > MAX_CAPTURE_MESSAGES or any(
            len(message) > MAX_CAPTURE_MESSAGE_CODEPOINTS
            for message in messages
        ):
            # CaptureRemoteOutcome has no truncation evidence yet. Reporting a
            # bounded operation failure is safer than silently dropping output.
            raise CaptureMessageDecodeFailure("Cell message payload exceeds public message limit")
        return tuple(
            " ".join(
                "".join(
                    character if ord(character) >= 0x20 else " "
                    for character in message
                ).split()
            )
            for message in messages
        )
