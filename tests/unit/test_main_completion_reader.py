"""MAIN completion scalars retain one RDBG owner through every read interval."""

from __future__ import annotations

from collections import deque
from importlib import import_module
from uuid import UUID

import pytest

from onec_runtime.errors import (
    BslExecutionError,
    CommandTimeout,
    RdbgTransportTimeout,
)
from onec_runtime.execution.main.operation import MainOperation, MainPhase
from onec_runtime.execution.evaluation import EvaluationSuspended
from onec_runtime.rdbg.models import (
    EvaluationResult,
    ModuleLocation,
    PendingEvaluation,
    StopEvent,
    TargetId,
)


TARGET = TargetId(UUID(int=41), "runtime_test")
_EMPTY = object()


def _contract():
    try:
        return import_module("onec_runtime.execution.main.completion")
    except ModuleNotFoundError as error:
        pytest.fail(f"MAIN completion reader is missing: {error}")


class _OwnedEvaluationPort:
    """Replace the remote boundary while retaining real pending capabilities."""

    def __init__(self, script: dict[str, list[object]]) -> None:
        self.script = {name: deque(items) for name, items in script.items()}
        self.starts: list[str] = []
        self.waits: list[tuple[PendingEvaluation, float]] = []
        self._expressions: dict[PendingEvaluation, str] = {}

    def start_evaluation(
        self,
        expression: str,
        *,
        max_text_size: int,
        stack_level: int,
        timeout_s: float,
    ) -> PendingEvaluation:
        assert stack_level == 0
        assert max_text_size == 307_200
        assert timeout_s == 30.0
        self.starts.append(expression)
        pending = PendingEvaluation(TARGET, UUID(int=len(self.starts)), self)
        self._expressions[pending] = expression
        return pending

    def wait_evaluation_event(
        self, pending: PendingEvaluation, *, timeout_s: float
    ) -> EvaluationResult | StopEvent:
        self.waits.append((pending, timeout_s))
        item = self.script[self._expressions[pending]].popleft()
        if item is _EMPTY:
            raise CommandTimeout("empty expression-result interval")
        if isinstance(item, Exception):
            raise item
        if isinstance(item, StopEvent):
            return item
        type_name, presentation, error_occurred, error_text, value_string = item
        return EvaluationResult(
            pending.result_id,
            type_name,
            presentation,
            error_occurred,
            error_text,
            value_string=value_string,
        )


def _normal_script(*, completed: str = "17") -> dict[str, list[object]]:
    return {
        "ЗавершеннаяКоманда": [("Число", completed, False, "", "")],
        "Результат": [("Число", "42", False, "", "")],
        "Ошибка": [("Строка", '""', False, "", "")],
        'RuntimeKernelServer.ЗабратьСообщенияЯчейкиИзКонтекста(Контекст, "msg")': [
            ("Строка", '"[\\"готово\\"]"', False, "", '["готово"]')
        ],
    }


def _running_operation() -> MainOperation:
    operation = MainOperation(17, TARGET)
    operation.continue_acknowledged()
    return operation


def test_matching_command_reads_result_error_and_messages() -> None:
    contract = _contract()
    port = _OwnedEvaluationPort(_normal_script())
    operation = _running_operation()

    result = contract.read_main_completion(
        port, operation, message_collector_key="msg"
    )

    assert result.result == 42
    assert result.error == ""
    assert result.messages == ("готово",)
    assert operation.phase is MainPhase.COMPLETED
    assert port.starts == [
        "ЗавершеннаяКоманда",
        "Результат",
        "Ошибка",
        'RuntimeKernelServer.ЗабратьСообщенияЯчейкиИзКонтекста(Контекст, "msg")',
    ]


def test_mismatched_command_id_cannot_close_or_read_another_operation() -> None:
    contract = _contract()
    port = _OwnedEvaluationPort(_normal_script(completed="18"))
    operation = _running_operation()

    with pytest.raises(contract.MainCommandMismatchError) as caught:
        contract.read_main_completion(port, operation)

    assert caught.value.expected_command_id == 17
    assert operation.phase is MainPhase.RUNNING
    assert port.starts == ["ЗавершеннаяКоманда"]


def test_mismatched_command_error_does_not_expose_remote_scalar() -> None:
    contract = _contract()
    private_marker = "private-payment-token-139"
    script = _normal_script()
    script["ЗавершеннаяКоманда"] = [
        ("Строка", f'"{private_marker}"', False, "", private_marker)
    ]
    port = _OwnedEvaluationPort(script)

    with pytest.raises(contract.MainCommandMismatchError) as caught:
        contract.read_main_completion(port, _running_operation())

    assert private_marker not in str(caught.value)
    assert private_marker not in repr(caught.value)


def test_completion_record_repr_does_not_expose_result_or_messages() -> None:
    contract = _contract()
    private_marker = "private-payment-token-139"
    script = _normal_script()
    script["Результат"] = [
        ("Строка", f'"{private_marker}"', False, "", private_marker)
    ]
    script[
        'RuntimeKernelServer.ЗабратьСообщенияЯчейкиИзКонтекста(Контекст, "msg")'
    ] = [("Строка", "", False, "", f'["{private_marker}"]')]

    result = contract.read_main_completion(
        _OwnedEvaluationPort(script), _running_operation(), message_collector_key="msg"
    )

    assert result.result == private_marker
    assert result.messages == (private_marker,)
    assert private_marker not in repr(result)


def test_bsl_error_in_result_read_keeps_proven_main_completion() -> None:
    contract = _contract()
    script = _normal_script()
    script["Результат"] = [("Неопределено", "", True, "BSL failed", "")]
    port = _OwnedEvaluationPort(script)
    operation = _running_operation()

    with pytest.raises(BslExecutionError, match="BSL failed"):
        contract.read_main_completion(port, operation)

    assert operation.phase is MainPhase.COMPLETED
    assert port.starts == ["ЗавершеннаяКоманда", "Результат", "Ошибка"]


def test_decode_error_after_matching_id_does_not_reopen_main() -> None:
    contract = _contract()
    script = _normal_script()
    script["Результат"] = [("Число", "private malformed scalar", False, "", "")]
    port = _OwnedEvaluationPort(script)
    operation = _running_operation()

    with pytest.raises(contract.MainScalarDecodeError) as caught:
        contract.read_main_completion(port, operation)

    assert caught.value.phase == "result"
    assert caught.value.evaluation_type == "Число"
    assert "private malformed scalar" not in str(caught.value)
    assert operation.phase is MainPhase.COMPLETED
    assert port.starts == ["ЗавершеннаяКоманда", "Результат", "Ошибка"]


def test_empty_poll_intervals_keep_one_pending_eval_without_redispatch() -> None:
    contract = _contract()
    script = _normal_script()
    script["ЗавершеннаяКоманда"] = [
        _EMPTY,
        _EMPTY,
        *script["ЗавершеннаяКоманда"],
    ]
    port = _OwnedEvaluationPort(script)
    operation = _running_operation()

    result = contract.read_main_completion(port, operation, poll_interval_s=0.25)

    assert result.result == 42
    assert port.starts.count("ЗавершеннаяКоманда") == 1
    assert len(port.waits) == 5
    assert port.waits[0][0] is port.waits[1][0] is port.waits[2][0]
    assert all(timeout_s == 0.25 for _, timeout_s in port.waits)


def test_transport_unknown_keeps_pending_capability_without_redispatch() -> None:
    contract = _contract()
    script = _normal_script()
    script["ЗавершеннаяКоманда"] = [
        RdbgTransportTimeout("remote request outcome is unknown")
    ]
    port = _OwnedEvaluationPort(script)
    operation = _running_operation()

    with pytest.raises(RdbgTransportTimeout):
        contract.read_main_completion(port, operation)

    assert operation.phase is MainPhase.RUNNING
    assert port.starts == ["ЗавершеннаяКоманда"]
    assert len(port.waits) == 1
    assert port.waits[0][0] in port._expressions


def test_eval_stop_preserves_pending_capability_and_stop_for_owner() -> None:
    contract = _contract()
    location = ModuleLocation(
        "CommonModule", "", UUID(int=51), UUID(int=52), 12
    )
    stop = StopEvent(TARGET, location, "breakpoint")
    script = _normal_script()
    script["ЗавершеннаяКоманда"] = [stop]
    port = _OwnedEvaluationPort(script)
    operation = _running_operation()

    with pytest.raises(EvaluationSuspended) as caught:
        contract.read_main_completion(port, operation)

    assert caught.value.stop is stop
    assert caught.value.pending is port.waits[0][0]
    assert operation.phase is MainPhase.RUNNING
    assert port.starts == ["ЗавершеннаяКоманда"]


def test_error_is_published_before_result_decode_failure() -> None:
    contract = _contract()
    script = _normal_script()
    script["Результат"] = [("Число", "private malformed scalar", False, "", "")]
    script["Ошибка"] = [
        ("Строка", '"Ошибка строки 4"', False, "", "Ошибка строки 4")
    ]
    port = _OwnedEvaluationPort(script)
    operation = _running_operation()
    published_errors: list[str] = []

    with pytest.raises(contract.MainScalarDecodeError):
        contract.read_main_completion(
            port, operation, on_error_decoded=published_errors.append
        )

    assert published_errors == ["Ошибка строки 4"]
    assert operation.phase is MainPhase.COMPLETED
