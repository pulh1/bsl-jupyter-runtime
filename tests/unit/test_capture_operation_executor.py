"""One CAPTURE cell owns its debugger work through one arbiter plan."""

from threading import get_ident
from uuid import UUID

import pytest

from onec_runtime.errors import BslExecutionError, RdbgTransportTimeout
from onec_runtime.execution.arbiter import OutcomeUnknown, RdbgArbiter, RouteToken, Settlement
from onec_runtime.execution.capture.cell_evaluator import CaptureCellEvaluator
from onec_runtime.execution.capture import operation_executor as operation_module
from onec_runtime.execution.capture.resources import TemporaryCleanupState
from onec_runtime.execution.capture.scope import (
    CaptureContextState,
    CaptureFrameIdentity,
    CaptureScope,
)
from onec_runtime.rdbg.models import (
    DebugTarget,
    EvaluationResult,
    ModuleLocation,
    PendingEvaluation,
    StackFrame,
    StopEvent,
    TargetId,
)


TARGET = TargetId(UUID(int=1), "test")
BUSINESS = ModuleLocation("ExtensionModule", "", UUID(int=2), UUID(int=3), 50, "Runtime")
KERNEL = ModuleLocation("ExtensionModule", "", UUID(int=2), UUID(int=3), 60, "Runtime")
FULL = (KERNEL, BUSINESS)
SHIELDED = (KERNEL,)
STOP = StopEvent(
    TARGET,
    BUSINESS,
    "callStackFormed",
    stack=(BUSINESS, BUSINESS, KERNEL),
    stack_frames=tuple(
        StackFrame(TARGET, level, location)
        for level, location in enumerate((BUSINESS, BUSINESS, KERNEL))
    ),
)


def ready_scope() -> CaptureScope:
    scope = CaptureScope.from_stop(7, 42, STOP, 3)
    scope.record_locals(())
    scope.record_transfer("temporary-address")
    scope.record_kernel_frame(2)
    assert scope.record_main_command(42)
    scope.record_context_begun()
    scope.mark_ready()
    return scope


class Session:
    target = DebugTarget(TARGET, "Server", "stopped")

    def __init__(self, events: list[object]) -> None:
        self.events = events
        self.calls: list[tuple[str, object, int]] = []
        self.pending = PendingEvaluation(TARGET, UUID(int=4), self)
        self._starts = 0

    def set_breakpoints(self, locations, *, on_transport_dispatch):
        on_transport_dispatch()
        self.calls.append(("breakpoints", locations, get_ident()))

    def start_evaluation(self, expression, *, on_transport_dispatch, **kwargs):
        on_transport_dispatch()
        self.calls.append(("start", (expression, kwargs["stack_level"]), get_ident()))
        self._starts += 1
        self.pending = PendingEvaluation(TARGET, UUID(int=3 + self._starts), self)
        return self.pending

    def wait_evaluation_event(self, pending, *, timeout_s, on_transport_dispatch):
        assert pending is self.pending
        on_transport_dispatch()
        self.calls.append(("wait", pending, get_ident()))
        event = self.events.pop(0)
        if isinstance(event, BaseException):
            raise event
        return event

    def continue_evaluation(self, pending, stop, *, on_transport_dispatch):
        assert pending is self.pending and stop is STOP
        on_transport_dispatch()
        self.calls.append(("continue-eval", pending, get_ident()))


def operation_plan(scope, calls, *, cleanup_error=None, cleanup_action=None):
    from onec_runtime.execution.capture.operation_executor import CaptureCellOperationExecutor

    executor = CaptureCellOperationExecutor(CaptureCellEvaluator(wait_interval_s=0.01))

    def shield(port):
        port.set_breakpoints(SHIELDED)

    def restore(port):
        port.set_breakpoints(FULL)

    def cleanup(port):
        calls.append(("cleanup", None, get_ident()))
        if cleanup_action is not None:
            cleanup_action(port)
        if cleanup_error is not None:
            raise cleanup_error

    def result_policy(result):
        calls.append(("policy", result, get_ident()))
        if result.error_occurred:
            raise BslExecutionError(result.error_text)
        return result.presentation

    return lambda port: executor.execute(
        scope,
        "Результат = 1;",
        port=port,
        shield_workspace=shield,
        restore_workspace=restore,
        cleanup=cleanup,
        result_policy=result_policy,
    )


def test_confirmed_bsl_error_restores_workspace_and_preserves_capture_frame() -> None:
    result = EvaluationResult(UUID(int=4), "Неопределено", "", True, "planned BSL error")
    session = Session([result])
    route = RouteToken("runtime", 1, 0, "capture")
    arbiter = RdbgArbiter(session, route)
    scope = ready_scope()
    try:
        ticket = arbiter.submit(route, operation_plan(scope, session.calls))
        arbiter.dispatch(ticket)
        with pytest.raises(BslExecutionError, match="planned BSL error"):
            ticket.wait(3)

        assert [kind for kind, _, _ in session.calls] == [
            "breakpoints", "start", "wait", "breakpoints", "policy", "cleanup"
        ]
        assert session.calls[0][1] == SHIELDED
        assert session.calls[1][1] == (
            'RuntimeKernelServer.ВыполнитьКодВКонтекстеОтладки(Контекст, '
            '"Результат = 1;")', 2
        )
        assert session.calls[3][1] == FULL
        assert len({thread for _, _, thread in session.calls}) == 1
        assert session.calls[0][2] != get_ident()
        assert scope.context_state is CaptureContextState.READY
        assert scope.frame_identity is CaptureFrameIdentity.CONFIRMED
        assert arbiter.active_ticket is None
        next_ticket = arbiter.submit(route, lambda port: Settlement("next cell admitted"))
        arbiter.dispatch(next_ticket)
        assert next_ticket.wait(3) == "next cell admitted"
    finally:
        arbiter.close(timeout=3)


def test_unknown_eval_keeps_pending_owner_and_skips_restore_cleanup_and_policy() -> None:
    result = EvaluationResult(UUID(int=4), "Число", "1", False)
    session = Session([RdbgTransportTimeout("network interval failed"), result])
    route = RouteToken("runtime", 1, 0, "capture")
    arbiter = RdbgArbiter(session, route)
    scope = ready_scope()
    ticket = arbiter.submit(route, operation_plan(scope, session.calls))
    arbiter.dispatch(ticket)

    assert ticket.wait_unknown(3)
    assert isinstance(ticket._error, OutcomeUnknown)
    assert ticket.status().pending_capability is session.pending
    assert arbiter.active_ticket is ticket
    assert [kind for kind, _, _ in session.calls] == ["breakpoints", "start", "wait"]
    assert scope.frame_identity is CaptureFrameIdentity.CONFIRMED

    def reconcile(port):
        observed = port.wait_evaluation_event(session.pending, timeout_s=0.01)
        port.set_breakpoints(FULL)
        return Settlement(observed.presentation)

    arbiter.reconcile(ticket, reconcile)
    assert ticket.wait_settled(3) == "1"
    assert [kind for kind, _, _ in session.calls].count("start") == 1
    arbiter.close(timeout=3)


def test_eval_stop_retains_same_pending_and_does_not_restore_early() -> None:
    result = EvaluationResult(UUID(int=4), "Число", "1", False)
    session = Session([STOP, result])
    route = RouteToken("runtime", 1, 0, "capture")
    arbiter = RdbgArbiter(session, route)
    ticket = arbiter.submit(route, operation_plan(ready_scope(), session.calls))
    arbiter.dispatch(ticket)

    assert ticket.wait_unknown(3)
    assert ticket.status().pending_capability is session.pending
    assert [kind for kind, _, _ in session.calls] == ["breakpoints", "start", "wait"]

    def reconcile(port):
        port.continue_evaluation(session.pending, STOP)
        observed = port.wait_evaluation_event(session.pending, timeout_s=0.01)
        port.set_breakpoints(FULL)
        return Settlement(observed.presentation)

    arbiter.reconcile(ticket, reconcile)
    assert ticket.wait_settled(3) == "1"
    assert [kind for kind, _, _ in session.calls].count("start") == 1
    arbiter.close(timeout=3)


def test_confirmed_restore_rejection_keeps_operation_owned_until_repair() -> None:
    result = EvaluationResult(UUID(int=4), "Число", "1", False)
    session = Session([result])
    original_set = session.set_breakpoints
    rejected = False

    def set_breakpoints(locations, *, on_transport_dispatch):
        nonlocal rejected
        if locations == FULL and not rejected:
            rejected = True
            raise ValueError("restore rejected before transport")
        original_set(locations, on_transport_dispatch=on_transport_dispatch)

    session.set_breakpoints = set_breakpoints
    route = RouteToken("runtime", 1, 0, "capture")
    arbiter = RdbgArbiter(session, route)
    scope = ready_scope()
    ticket = arbiter.submit(route, operation_plan(scope, session.calls))
    arbiter.dispatch(ticket)

    assert ticket.wait_unknown(3)
    assert arbiter.active_ticket is ticket
    assert scope.frame_identity is CaptureFrameIdentity.CONFIRMED
    assert [kind for kind, _, _ in session.calls] == ["breakpoints", "start", "wait"]
    later = arbiter.submit(route, lambda port: Settlement("later"))
    arbiter.dispatch(later)
    with pytest.raises(TimeoutError):
        later.wait(0)

    def reconcile(port):
        port.set_breakpoints(FULL)
        return Settlement("repaired")

    arbiter.reconcile(ticket, reconcile)
    assert ticket.wait_settled(3) == "repaired"
    assert later.wait(3) == "later"
    arbiter.close(timeout=3)


def test_restore_repair_uses_confirmed_result_without_second_eval() -> None:
    result = EvaluationResult(UUID(int=4), "Число", "17", False)
    session = Session([result])
    original_set = session.set_breakpoints
    rejected = False

    def set_breakpoints(locations, *, on_transport_dispatch):
        nonlocal rejected
        if locations == FULL and not rejected:
            rejected = True
            raise ValueError("restore rejected before transport")
        original_set(locations, on_transport_dispatch=on_transport_dispatch)

    session.set_breakpoints = set_breakpoints
    route = RouteToken("runtime", 1, 0, "capture")
    arbiter = RdbgArbiter(session, route)
    scope = ready_scope()
    executor = operation_module.CaptureCellOperationExecutor(
        CaptureCellEvaluator(wait_interval_s=0.01)
    )
    operation = operation_module.CaptureCellOperation(scope.identity)
    policy_calls = []
    cleanup_calls = []

    def restore(port):
        port.set_breakpoints(FULL)

    def policy(confirmed):
        policy_calls.append(confirmed)
        return confirmed.presentation

    def cleanup(_port):
        cleanup_calls.append("done")

    try:
        ticket = arbiter.submit(
            route,
            lambda port: executor.execute(
                scope, "Результат = 17;", operation=operation, port=port,
                shield_workspace=lambda worker: worker.set_breakpoints(SHIELDED),
                restore_workspace=restore, cleanup=cleanup, result_policy=policy,
            ),
        )
        arbiter.dispatch(ticket)
        assert ticket.wait_unknown(3)
        assert operation.confirmed_result is result
        assert [kind for kind, _, _ in session.calls].count("start") == 1
        assert policy_calls == []
        assert cleanup_calls == []

        arbiter.reconcile(
            ticket,
            lambda port: executor.repair_confirmed_result(
                operation, scope, port=port, restore_workspace=restore,
                cleanup=cleanup, result_policy=policy,
            ),
        )
        assert ticket.wait_settled(3) == "17"
        assert [kind for kind, _, _ in session.calls].count("start") == 1
        assert policy_calls == [result]
        assert cleanup_calls == ["done"]
        assert scope.frame_identity is CaptureFrameIdentity.CONFIRMED
    finally:
        if arbiter.active_ticket is None:
            arbiter.close(timeout=3)


def test_restore_repair_settles_confirmed_bsl_error_and_releases_next_cell() -> None:
    result = EvaluationResult(UUID(int=4), "Ошибка", "", True, "planned BSL error")
    session = Session([result])
    original_set = session.set_breakpoints
    rejected = False

    def set_breakpoints(locations, *, on_transport_dispatch):
        nonlocal rejected
        if locations == FULL and not rejected:
            rejected = True
            raise ValueError("restore rejected before transport")
        original_set(locations, on_transport_dispatch=on_transport_dispatch)

    session.set_breakpoints = set_breakpoints
    route = RouteToken("runtime", 1, 0, "capture")
    arbiter = RdbgArbiter(session, route)
    scope = ready_scope()
    executor = operation_module.CaptureCellOperationExecutor(
        CaptureCellEvaluator(wait_interval_s=0.01)
    )
    operation = operation_module.CaptureCellOperation(scope.identity)

    def restore(port):
        port.set_breakpoints(FULL)

    def policy(confirmed):
        if confirmed.error_occurred:
            raise BslExecutionError(confirmed.error_text)
        return confirmed.presentation

    try:
        ticket = arbiter.submit(
            route,
            lambda port: executor.execute(
                scope, "ВызватьОшибку;", operation=operation, port=port,
                shield_workspace=lambda worker: worker.set_breakpoints(SHIELDED),
                restore_workspace=restore, cleanup=lambda worker: None,
                result_policy=policy,
            ),
        )
        arbiter.dispatch(ticket)
        assert ticket.wait_unknown(3)
        later = arbiter.submit(route, lambda port: Settlement("next cell"))
        arbiter.dispatch(later)

        arbiter.reconcile(
            ticket,
            lambda port: executor.repair_confirmed_result(
                operation, scope, port=port, restore_workspace=restore,
                cleanup=lambda worker: None, result_policy=policy,
            ),
        )
        with pytest.raises(BslExecutionError, match="planned BSL error"):
            ticket.wait_settled(3)
        assert later.wait_settled(3) == "next cell"
        assert [kind for kind, _, _ in session.calls].count("start") == 1
        assert scope.frame_identity is CaptureFrameIdentity.CONFIRMED
    finally:
        if arbiter.active_ticket is None:
            arbiter.close(timeout=3)


def test_mandatory_cleanup_failure_keeps_operation_owned_until_repair() -> None:
    result = EvaluationResult(UUID(int=4), "Число", "1", False)
    session = Session([result])
    route = RouteToken("runtime", 1, 0, "capture")
    arbiter = RdbgArbiter(session, route)
    ticket = arbiter.submit(
        route,
        operation_plan(
            ready_scope(), session.calls,
            cleanup_error=ValueError("cleanup rejected"),
        ),
    )
    arbiter.dispatch(ticket)

    assert ticket.wait_unknown(3)
    assert arbiter.active_ticket is ticket
    assert [kind for kind, _, _ in session.calls] == [
        "breakpoints", "start", "wait", "breakpoints", "policy", "cleanup"
    ]
    arbiter.reconcile(ticket, lambda port: Settlement("cleanup repaired"))
    assert ticket.wait_settled(3) == "cleanup repaired"
    arbiter.close(timeout=3)


def test_confirmed_temporary_delete_rejection_is_isolated_debt_and_next_cell_runs() -> None:
    primary = EvaluationResult(UUID(int=4), "Число", "1", False)
    rejected_delete = EvaluationResult(UUID(int=5), "Неопределено", "", True, "delete rejected")
    next_primary = EvaluationResult(UUID(int=6), "Число", "2", False)
    session = Session([primary, rejected_delete, next_primary])
    route = RouteToken("runtime", 1, 0, "capture")
    arbiter = RdbgArbiter(session, route)
    scope = ready_scope()
    key = "__onec_value_" + "a" * 32
    scope.track_temporary_key(key)

    def delete_key(port):
        pending = port.start_evaluation("УдалитьИзВременногоХранилища(...)", stack_level=2)
        outcome = port.wait_evaluation_event(pending, timeout_s=0.01)
        assert outcome.error_occurred
        raise operation_module.ConfirmedTemporaryKeyCleanupFailure(key)

    ticket = arbiter.submit(
        route,
        operation_plan(scope, session.calls, cleanup_action=delete_key),
    )
    arbiter.dispatch(ticket)

    with pytest.raises(operation_module.ConfirmedTemporaryKeyCleanupFailure):
        ticket.wait(3)
    assert ticket.status().settled
    assert arbiter.active_ticket is None
    assert scope.context_state is CaptureContextState.READY
    assert scope.frame_identity is CaptureFrameIdentity.CONFIRMED
    assert [(debt.key, debt.state) for debt in scope.temporary_cleanup_debts] == [
        (key, TemporaryCleanupState.CONFIRMED_FAILURE)
    ]

    next_ticket = arbiter.submit(route, operation_plan(scope, session.calls))
    arbiter.dispatch(next_ticket)
    assert next_ticket.wait(3) == "2"
    assert [kind for kind, _, _ in session.calls].count("start") == 3
    arbiter.close(timeout=3)


def test_unknown_temporary_delete_keeps_pending_owner_without_repeat_dispatch() -> None:
    primary = EvaluationResult(UUID(int=4), "Число", "1", False)
    delete_result = EvaluationResult(UUID(int=5), "Неопределено", "", False)
    session = Session([primary, RdbgTransportTimeout("delete wait failed"), delete_result])
    route = RouteToken("runtime", 1, 0, "capture")
    arbiter = RdbgArbiter(session, route)
    scope = ready_scope()
    key = "__onec_value_" + "b" * 32
    scope.track_temporary_key(key)

    def delete_key(port):
        pending = port.start_evaluation("УдалитьИзВременногоХранилища(...)", stack_level=2)
        try:
            port.wait_evaluation_event(pending, timeout_s=0.01)
        except OutcomeUnknown as error:
            raise operation_module.TemporaryKeyCleanupOutcomeUnknown(key) from error

    ticket = arbiter.submit(
        route,
        operation_plan(scope, session.calls, cleanup_action=delete_key),
    )
    arbiter.dispatch(ticket)

    assert ticket.wait_unknown(3)
    assert isinstance(ticket._error, operation_module.TemporaryKeyCleanupOutcomeUnknown)
    assert ticket.status().pending_capability is session.pending
    assert arbiter.active_ticket is ticket
    assert [(debt.key, debt.state) for debt in scope.temporary_cleanup_debts] == [
        (key, TemporaryCleanupState.UNKNOWN)
    ]
    next_ticket = arbiter.submit(route, lambda port: Settlement("next cell"))
    arbiter.dispatch(next_ticket)
    with pytest.raises(TimeoutError):
        next_ticket.wait(0)

    def reconcile(port):
        outcome = port.wait_evaluation_event(session.pending, timeout_s=0.01)
        assert not outcome.error_occurred
        scope.confirm_temporary_cleanup(key)
        return Settlement("delete confirmed")

    arbiter.reconcile(ticket, reconcile)
    assert ticket.wait_settled(3) == "delete confirmed"
    assert next_ticket.wait(3) == "next cell"
    assert [kind for kind, _, _ in session.calls].count("start") == 2
    assert scope.temporary_cleanup_debts == ()
    arbiter.close(timeout=3)


def test_confirmed_key_debt_keeps_cell_error_as_separate_evidence() -> None:
    result = EvaluationResult(UUID(int=4), "Неопределено", "", True, "planned BSL error")
    session = Session([result])
    route = RouteToken("runtime", 1, 0, "capture")
    arbiter = RdbgArbiter(session, route)
    scope = ready_scope()
    key = "__onec_value_" + "c" * 32
    scope.track_temporary_key(key)
    ticket = arbiter.submit(
        route,
        operation_plan(
            scope,
            session.calls,
            cleanup_error=operation_module.ConfirmedTemporaryKeyCleanupFailure(key),
        ),
    )
    arbiter.dispatch(ticket)

    with pytest.raises(operation_module.ConfirmedTemporaryKeyCleanupFailure) as failure:
        ticket.wait(3)
    assert isinstance(failure.value.policy_error, BslExecutionError)
    assert str(failure.value.policy_error) == "planned BSL error"
    assert ticket.status().settled
    assert arbiter.active_ticket is None
    assert scope.temporary_cleanup_debts[0].state is TemporaryCleanupState.CONFIRMED_FAILURE
    arbiter.close(timeout=3)


def test_cleanup_debt_keeps_confirmed_bsl_error_as_separate_evidence() -> None:
    result = EvaluationResult(UUID(int=4), "Неопределено", "", True, "planned BSL error")
    session = Session([result])
    route = RouteToken("runtime", 1, 0, "capture")
    arbiter = RdbgArbiter(session, route)
    scope = ready_scope()
    ticket = arbiter.submit(
        route,
        operation_plan(
            scope, session.calls,
            cleanup_error=ValueError("cleanup rejected"),
        ),
    )
    arbiter.dispatch(ticket)

    assert ticket.wait_unknown(3)
    assert isinstance(ticket._error.policy_error, BslExecutionError)
    assert str(ticket._error.policy_error) == "planned BSL error"
    assert scope.frame_identity is CaptureFrameIdentity.CONFIRMED
    arbiter.reconcile(ticket, lambda port: Settlement("cleanup repaired"))
    assert ticket.wait_settled(3) == "cleanup repaired"
    arbiter.close(timeout=3)


def test_local_eval_rejection_after_shield_keeps_workspace_repair_owned() -> None:
    session = Session([])

    def reject_eval(*args, **kwargs):
        raise ValueError("evaluation rejected before transport")

    session.start_evaluation = reject_eval
    route = RouteToken("runtime", 1, 0, "capture")
    arbiter = RdbgArbiter(session, route)
    ticket = arbiter.submit(route, operation_plan(ready_scope(), session.calls))
    arbiter.dispatch(ticket)

    assert ticket.wait_unknown(3)
    assert arbiter.active_ticket is ticket
    assert [kind for kind, _, _ in session.calls] == ["breakpoints"]
    assert ticket.status().pending_capability is None

    def reconcile(port):
        port.set_breakpoints(FULL)
        return Settlement("workspace repaired")

    arbiter.reconcile(ticket, reconcile)
    assert ticket.wait_settled(3) == "workspace repaired"
    arbiter.close(timeout=3)


def test_unready_capture_scope_rejects_before_workspace_side_effect() -> None:
    session = Session([])
    route = RouteToken("runtime", 1, 0, "capture")
    arbiter = RdbgArbiter(session, route)
    unready = CaptureScope.from_stop(7, 42, STOP, 3)
    ticket = arbiter.submit(route, operation_plan(unready, session.calls))
    arbiter.dispatch(ticket)

    with pytest.raises(RuntimeError, match="ready"):
        ticket.wait(3)
    assert session.calls == []
    arbiter.close(timeout=3)
