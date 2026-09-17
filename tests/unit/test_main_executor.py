from __future__ import annotations

from collections.abc import Callable
from threading import Event
from types import SimpleNamespace
from uuid import UUID

import pytest

from onec_runtime.errors import (
    ProtocolError,
    RdbgTransportTimeout,
    StopWaitIntervalElapsed,
)
from onec_runtime.execution.arbiter import RdbgArbiter, RouteToken, Settlement, WaiterDetached
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

    def continue_(self, *, on_transport_dispatch: Callable[[], None] | None = None) -> None:
        self.calls.append(("continue",))
        if on_transport_dispatch is not None:
            on_transport_dispatch()

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


def test_long_lived_main_executor_uses_the_port_of_each_operation() -> None:
    first_port = MainPort()
    second_port = MainPort()
    executor = MainExecutor()
    operation = MainOperation(17, TARGET)

    assert executor.dispatch(
        operation,
        "Результат = 17;",
        port=first_port,
        install_workspace=lambda: None,
        before_command_write=lambda: None,
        before_continue=lambda: None,
    ) is STOP
    operation.stopped(STOP, MainPhase.SUSPENDED_CAPTURE)

    assert executor.resume(operation, port=second_port) is STOP
    assert [call[0] for call in first_port.calls] == [
        "modify", "modify", "continue", "wait"
    ]
    assert second_port.calls == [("continue",), ("wait", 6.0)]


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


def test_main_resume_keeps_paused_frame_on_confirmed_predispatch_rejection() -> None:
    class RejectedPort(MainPort):
        def continue_(self, *, on_transport_dispatch: Callable[[], None] | None = None) -> None:
            self.calls.append(("continue",))
            raise ProtocolError("rejected before transport entry")

    port = RejectedPort()
    operation = MainOperation(17, TARGET)
    operation.continue_acknowledged()
    operation.stopped(STOP, MainPhase.SUSPENDED_CAPTURE)

    with pytest.raises(ProtocolError, match="before transport entry"):
        MainExecutor(port).resume(operation)

    assert operation.phase is MainPhase.SUSPENDED_CAPTURE
    assert operation.pending_stop is STOP


def test_main_resume_records_unknown_after_transport_entry_without_ack() -> None:
    class AmbiguousPort(MainPort):
        def continue_(self, *, on_transport_dispatch: Callable[[], None] | None = None) -> None:
            self.calls.append(("continue",))
            assert on_transport_dispatch is not None
            on_transport_dispatch()
            raise RdbgTransportTimeout("ack unknown")

    operation = MainOperation(17, TARGET)
    operation.continue_acknowledged()
    operation.stopped(STOP, MainPhase.SUSPENDED_CAPTURE)

    with pytest.raises(RdbgTransportTimeout, match="ack unknown"):
        MainExecutor(AmbiguousPort()).resume(operation)

    assert operation.phase is MainPhase.UNKNOWN
    assert operation.pending_stop is None


def test_detached_main_waiter_leaves_arbiter_owning_the_next_stop() -> None:
    entered_wait = Event()
    release_stop = Event()

    class Session:
        target = SimpleNamespace(target_id=TARGET)

        def modify(self, variable, value_expression, *, on_transport_dispatch):
            on_transport_dispatch()
            return SimpleNamespace(error_occurred=False)

        def continue_(self, *, on_transport_dispatch):
            on_transport_dispatch()

        def wait_for_any_stop(self, *, timeout_s, expected_target, on_transport_dispatch):
            assert expected_target == TARGET
            on_transport_dispatch()
            entered_wait.set()
            assert release_stop.wait(3)
            return STOP

    route = RouteToken("runtime", 1, 0, "main")
    arbiter = RdbgArbiter(Session(), route)
    operation = MainOperation(17, TARGET)
    try:
        ticket = arbiter.submit(
            route,
            lambda port: Settlement(
                MainExecutor(port, poll_interval_s=0.1).dispatch(
                    operation,
                    "Результат = 17;",
                    install_workspace=lambda: None,
                    before_command_write=lambda: None,
                    before_continue=lambda: None,
                )
            ),
        )
        arbiter.dispatch(ticket)
        assert entered_wait.wait(3)
        ticket.detach_waiter()
        with pytest.raises(WaiterDetached):
            ticket.wait_initiator(1)
        assert ticket.status().awaiting_stop
        release_stop.set()
        assert ticket.wait_settled(3) is STOP
        assert operation.phase is MainPhase.RUNNING
    finally:
        release_stop.set()
        arbiter.close(timeout=3)
