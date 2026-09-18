"""Read one MAIN command's completion through its owned RDBG evaluation port.

This module reads remote scalars and records matching command completion.
The controller remains responsible for visible-source diagnostics, reply
construction, state publication, and journal records.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import json
from math import isfinite

from onec_runtime.errors import BslExecutionError, ProtocolError
from onec_runtime.execution.evaluation import EvaluationPort, evaluate_until_result
from onec_runtime.execution.main.operation import MainOperation
from onec_runtime.experiment import bsl_string_literal
from onec_runtime.rdbg.models import EvaluationResult
from onec_runtime.table_value import evaluation_to_python


class MainCommandMismatchError(ProtocolError):
    """The remote completion belongs to another MAIN command."""

    def __init__(self, expected_command_id: int) -> None:
        super().__init__("Completed command does not match active operation")
        self.expected_command_id = expected_command_id


class MainScalarDecodeError(ProtocolError):
    """One completion scalar could not be decoded without exposing its value."""

    def __init__(self, phase: str, evaluation: EvaluationResult) -> None:
        super().__init__(f"MAIN completion {phase} cannot be decoded")
        self.phase = phase
        self.evaluation_type = evaluation.type_name
        self.exact_decimal_present = bool(evaluation.value_decimal)


@dataclass(frozen=True, slots=True)
class MainRemoteCompletion:
    result: object = field(repr=False)
    error: str = field(repr=False)
    messages: tuple[str, ...] = field(repr=False)


def _read(port: EvaluationPort, expression: str, *, poll_interval_s: float) -> EvaluationResult:
    return evaluate_until_result(
        port, expression, stack_level=0, wait_interval_s=poll_interval_s
    )


def _require_success(result: EvaluationResult) -> None:
    if result.error_occurred:
        raise BslExecutionError(result.error_text)


def _decode_scalar(result: EvaluationResult, *, phase: str) -> object:
    try:
        return evaluation_to_python(result)
    except Exception:
        raise MainScalarDecodeError(phase, result) from None


def read_main_completion(
    port: EvaluationPort,
    operation: MainOperation,
    *,
    message_collector_key: str = "",
    poll_interval_s: float = 6.0,
    on_error_decoded: Callable[[str], None] | None = None,
) -> MainRemoteCompletion:
    """Read exact completion; matching ID closes MAIN before value decoding.

    ``on_error_decoded`` lets the caller publish status and source-mapped
    diagnostics before result decoding, matching the legacy lifecycle.
    """

    if (
        isinstance(poll_interval_s, bool)
        or not isinstance(poll_interval_s, (int, float))
        or not isfinite(float(poll_interval_s))
        or poll_interval_s <= 0
    ):
        raise ValueError("MAIN completion poll interval must be finite and positive")
    interval = float(poll_interval_s)
    completed_evaluation = _read(port, "ЗавершеннаяКоманда", poll_interval_s=interval)
    _require_success(completed_evaluation)
    completed_id = _decode_scalar(completed_evaluation, phase="completed_command")
    if completed_id != operation.command_id:
        raise MainCommandMismatchError(operation.command_id)
    operation.remote_completed()
    result_evaluation = _read(port, "Результат", poll_interval_s=interval)
    error_evaluation = _read(port, "Ошибка", poll_interval_s=interval)
    _require_success(result_evaluation)
    _require_success(error_evaluation)
    error_value = _decode_scalar(error_evaluation, phase="error")
    error = "" if error_value is None else str(error_value)
    if on_error_decoded is not None:
        on_error_decoded(error)
    result = _decode_scalar(result_evaluation, phase="result")
    messages: tuple[str, ...] = ()
    if message_collector_key:
        expression = (
            "RuntimeKernelServer.ЗабратьСообщенияЯчейкиИзКонтекста(e1cRuntimeКонтекст, "
            + bsl_string_literal(message_collector_key)
            + ")"
        )
        message_evaluation = _read(port, expression, poll_interval_s=interval)
        _require_success(message_evaluation)
        payload = evaluation_to_python(message_evaluation)
        if not isinstance(payload, str):
            raise ProtocolError("Cell message payload is not JSON text")
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError as error:
            raise ProtocolError("Cell message payload is invalid JSON") from error
        if not isinstance(decoded, list) or any(
            not isinstance(message, str) for message in decoded
        ):
            raise ProtocolError("Cell message payload is not a text array")
        messages = tuple(decoded)
    return MainRemoteCompletion(result, error, messages)
