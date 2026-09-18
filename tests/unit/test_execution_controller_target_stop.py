"""Confirmed Stop retires the exact controller-owned MAIN/CAPTURE lifetime."""

from threading import Event
from time import monotonic, sleep

import pytest

from onec_runtime.errors import ProtocolError
from onec_runtime.capture_evaluation import CapturePhase
from onec_runtime.execution.arbiter import (
    OutcomeUnknown, RdbgArbiter, RouteToken, Settlement, StopRequestOutcome,
    TargetTerminated,
)
from onec_runtime.execution.capture.cell_evaluator import CaptureCellEvaluator
from onec_runtime.execution.capture.executor import CaptureExecutor
from onec_runtime.execution.capture.scope import CaptureFrameIdentity
from onec_runtime.execution.controller.controller import ExecutionController
from onec_runtime.execution.main import MainExecutor, MainPhase
from onec_runtime.execution.termination import (
    FileTargetProcessLease, FileTerminationConfirmed, FileTerminationUnknown,
)
from onec_runtime.rdbg.models import DebugTarget, EvaluationResult
from onec_runtime.stop_routing import BreakpointRegistry
from onec_runtime.table_value import evaluation_to_python

from test_execution_controller_routes import CompleteSession
from test_execution_route_sequence import BUSINESS, KERNEL, TARGET


class _Process:
    def __init__(self, *, exits: bool = True) -> None:
        self.pid = 1379
        self.process = self
        self.exits = exits
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def close(self, timeout_s: float) -> None:
        if self.exits:
            self.returncode = -15


class _StopSession(CompleteSession):
    def __init__(self) -> None:
        super().__init__()
        self.target = DebugTarget(TARGET, "ServerEmulation", "stopped", 1)
        self.hang_main = False
        self.hang_capture = False
        self.command_id = 1

    def modify(self, variable, value_expression, *, on_transport_dispatch):
        if variable == "ИдентификаторКоманды":
            self.command_id = int(value_expression)
        return super().modify(
            variable, value_expression,
            on_transport_dispatch=on_transport_dispatch,
        )

    def wait_for_any_stop(self, *, timeout_s, expected_target, on_transport_dispatch):
        if self.hang_main:
            raise OutcomeUnknown("MAIN stop still pending")
        return super().wait_for_any_stop(
            timeout_s=timeout_s,
            expected_target=expected_target,
            on_transport_dispatch=on_transport_dispatch,
        )

    def wait_evaluation_event(self, pending, *, timeout_s, on_transport_dispatch):
        if self.hang_capture:
            raise OutcomeUnknown("CAPTURE eval still pending")
        if self.expression == "ИдентификаторКоманды":
            on_transport_dispatch()
            self._record("wait_eval")
            self.pending = None
            return EvaluationResult(
                pending.result_id, "Число", str(self.command_id), False,
            )
        return super().wait_evaluation_event(
            pending, timeout_s=timeout_s,
            on_transport_dispatch=on_transport_dispatch,
        )


def _controller(session: _StopSession, process: _Process):
    arbiter = RdbgArbiter(
        session,
        RouteToken("runtime-1", 1, 0, "main"),
        file_target_lease=FileTargetProcessLease(TARGET, process),
    )
    controller = ExecutionController(
        arbiter,
        MainExecutor(poll_interval_s=0.1),
        CaptureExecutor(None, KERNEL, decode_command_id=evaluation_to_python),
        CaptureCellEvaluator(),
        BreakpointRegistry(KERNEL, (BUSINESS,)),
        runtime_generation=1,
        initial_target_id=TARGET,
    )
    return arbiter, controller


def _wait_for_loss(controller: ExecutionController) -> None:
    deadline = monotonic() + 3
    while controller.status_facts().main_phase is not MainPhase.LOST:
        if monotonic() > deadline:
            pytest.fail("confirmed termination was not observed by controller")
        sleep(0.01)


def test_detached_main_stop_marks_exact_main_lost_after_target_exit() -> None:
    session = _StopSession()
    session.hang_main = True
    arbiter, controller = _controller(session, _Process())
    try:
        ticket = controller.submit_main("Результат = 1;")
        assert ticket.wait_unknown(3)
        operation = controller.main_operation
        assert operation is not None
        ticket.detach_waiter()

        controller.request_stop(ticket)
        with pytest.raises(TargetTerminated) as stopped:
            ticket.wait_settled(3)
        _wait_for_loss(controller)

        assert operation.phase is MainPhase.LOST
        assert isinstance(stopped.value.evidence, FileTerminationConfirmed)
        assert controller.confirmed_target_termination is stopped.value.evidence
        assert controller.main_idle_fence() is None
        assert controller.value_route_snapshot() is None
        with pytest.raises(ProtocolError, match="terminated"):
            controller.worker_mutation_route()
        with pytest.raises(ProtocolError, match="terminated"):
            controller.submit_main("Результат = 2;")
    finally:
        arbiter.close(timeout=3)


def test_capture_eval_stop_marks_existing_scope_lost_and_rejects_inspection() -> None:
    session = _StopSession()
    arbiter, controller = _controller(session, _Process())
    try:
        controller.submit_main("Результат = 1;").wait_settled(3)
        operation = controller.main_operation
        scope = controller.capture_scope
        assert operation is not None and scope is not None
        ledger = controller.capture_evaluation_ledger()
        session.hang_capture = True
        ticket = controller.submit_capture_cell("Результат = 2;")
        assert ticket.wait_unknown(3)
        ticket.detach_waiter()

        controller.request_stop(ticket)
        with pytest.raises(TargetTerminated):
            ticket.wait_settled(3)
        _wait_for_loss(controller)

        assert controller.main_operation is operation
        assert controller.capture_scope is scope
        assert scope.frame_identity is CaptureFrameIdentity.LOST
        assert scope.frame_stack_level is None
        assert controller.status_facts().capture_frame_identity is CaptureFrameIdentity.LOST
        assert ledger.status().phase is CapturePhase.STALE
        with pytest.raises(ProtocolError):
            controller.capture_evaluation_ledger()
        with pytest.raises(ProtocolError):
            controller.submit_capture_variable_page(stack_level=0, start=0, stop=1)
    finally:
        arbiter.close(timeout=3)


def test_unconfirmed_stop_keeps_main_and_capture_frame_live() -> None:
    session = _StopSession()
    process = _Process(exits=False)
    arbiter, controller = _controller(session, process)
    try:
        controller.submit_main("Результат = 1;").wait_settled(3)
        scope = controller.capture_scope
        assert scope is not None
        session.hang_capture = True
        ticket = controller.submit_capture_cell("Результат = 2;")
        assert ticket.wait_unknown(3)

        controller.request_stop(ticket)
        attempt = ticket.stop_teardown
        assert attempt is not None
        assert isinstance(attempt.wait(3), FileTerminationUnknown)
        assert controller.main_operation is not None
        assert controller.main_operation.phase is MainPhase.SUSPENDED_CAPTURE
        assert scope.frame_identity is CaptureFrameIdentity.CONFIRMED
        assert controller.confirmed_target_termination is None

        process.returncode = -15
        arbiter.teardown_fenced_file_target(
            ticket, arbiter.current_route, grace_s=0.1,
        ).wait(3)
        with pytest.raises(TargetTerminated):
            ticket.wait_settled(3)
        _wait_for_loss(controller)
    finally:
        arbiter.close(timeout=3)


def test_queued_main_stop_retire_pre_dispatch_operation_without_target_loss() -> None:
    session = _StopSession()
    arbiter, controller = _controller(session, _Process())
    entered = Event()
    release = Event()
    try:
        route = arbiter.current_route

        def hold(_port):
            entered.set()
            assert release.wait(3)
            return Settlement(None)

        holding = arbiter.submit(route, hold)
        arbiter.dispatch(holding)
        assert entered.wait(3)
        queued = controller.submit_main("Результат = 1;")
        operation = controller.main_operation
        assert operation is not None

        assert controller.request_stop(queued) is StopRequestOutcome.CANCELLED_BEFORE_EFFECT
        assert operation.phase is MainPhase.FAILED_BEFORE_DISPATCH
        assert controller.confirmed_target_termination is None
        release.set()
        holding.wait_settled(3)
        controller.submit_main("Результат = 2;").wait_settled(3)
    finally:
        release.set()
        holding.wait_settled(3)
        arbiter.close(timeout=3)
