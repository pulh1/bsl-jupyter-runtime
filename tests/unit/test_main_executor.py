from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest

from onec_runtime.errors import (
    ProtocolError,
    RdbgTransportTimeout,
    StopWaitIntervalElapsed,
)
from onec_runtime.execution.main.executor import MainExecutor
from onec_runtime.execution.main.operation import MainOperation, MainPhase
from onec_runtime.rdbg.models import ModuleLocation, StopEvent, TargetId


TARGET = TargetId(UUID(int=1), "runtime")
LOCATION = ModuleLocation("ExtensionModule", "", UUID(int=2), UUID(int=3), 7, "Runtime")
STOP = StopEvent(TARGET, LOCATION, "breakpoint")


class MainPort:
    def __init__(self, *, empty_intervals: int = 0) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.empty_intervals = empty_intervals

    def modify(self, variable: str, value_expression: str) -> object:
        self.calls.append(("modify", variable, value_expression))
        return SimpleNamespace(error_occurred=False, error_text="")

    def continue_(self) -> None:
        self.calls.append(("continue",))

    def wait_for_any_stop(self, *, timeout_s: float) -> StopEvent:
        self.calls.append(("wait", timeout_s))
        if self.empty_intervals:
            self.empty_intervals -= 1
            raise StopWaitIntervalElapsed("poll interval ended")
        return STOP


def test_main_executor_owns_command_order_and_keeps_waiting_across_intervals() -> None:
    port = MainPort(empty_intervals=3)
    operation = MainOperation(17, TARGET)
    stages: list[str] = []

    stop = MainExecutor(port, poll_interval_s=0.1).dispatch(
        operation,
        "Результат = 17;",
        install_workspace=lambda: stages.append("workspace"),
        before_command_write=lambda: stages.append("write"),
        before_continue=lambda: stages.append("continue"),
    )

    assert stop is STOP
    assert stages == ["workspace", "write", "continue"]
    assert port.calls[:3] == [
        ("modify", "ТекущаяИнструкция", '"Результат = 17;"'),
        ("modify", "ИдентификаторКоманды", "17"),
        ("continue",),
    ]
    assert port.calls[3:] == [("wait", 0.1)] * 4
    assert operation.phase is MainPhase.RUNNING
    assert not operation.terminal


def test_main_executor_resume_uses_same_operation_and_does_not_rewrite_command() -> None:
    port = MainPort()
    operation = MainOperation(17, TARGET)
    operation.continue_acknowledged()
    operation.stopped(STOP, MainPhase.SUSPENDED_CAPTURE)

    assert MainExecutor(port).resume(operation) is STOP

    assert port.calls == [("continue",), ("wait", 6.0)]
    assert operation.command_id == 17
    assert operation.phase is MainPhase.RUNNING


def test_main_executor_does_not_continue_after_rejected_command_write() -> None:
    class RejectedPort(MainPort):
        def modify(self, variable: str, value_expression: str) -> object:
            super().modify(variable, value_expression)
            return SimpleNamespace(error_occurred=True, error_text="rejected")

    port = RejectedPort()
    operation = MainOperation(17, TARGET)

    with pytest.raises(ProtocolError, match="ТекущаяИнструкция"):
        MainExecutor(port).dispatch(
            operation,
            "Результат = 17;",
            install_workspace=lambda: None,
            before_command_write=lambda: None,
            before_continue=lambda: None,
        )

    assert port.calls == [("modify", "ТекущаяИнструкция", '"Результат = 17;"')]
    assert operation.phase is MainPhase.ADMITTED


def test_main_executor_does_not_hide_transport_timeout_as_empty_interval() -> None:
    class NetworkTimeoutPort(MainPort):
        def wait_for_any_stop(self, *, timeout_s: float) -> StopEvent:
            self.calls.append(("wait", timeout_s))
            if len([call for call in self.calls if call[0] == "wait"]) == 1:
                raise RdbgTransportTimeout("ping request failed")
            return STOP

    port = NetworkTimeoutPort()
    operation = MainOperation(17, TARGET)

    with pytest.raises(RdbgTransportTimeout, match="ping request failed"):
        MainExecutor(port).resume(operation)

    assert port.calls == [("continue",), ("wait", 6.0)]
    assert operation.phase is MainPhase.RUNNING
