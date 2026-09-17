from threading import Event, Lock, Thread, get_ident
from types import SimpleNamespace

from uuid import uuid4

import pytest

from onec_runtime.errors import EvaluationDispatchUnknown
from onec_runtime.rdbg.models import PendingEvaluation, TargetId, EvaluationResult


from onec_runtime.execution.arbiter import (
    RdbgArbiter, RouteToken, Settlement, OutcomeUnknown, StaleRoute,
    CancelledBeforeEffect, WaiterDetached, ArbiterBusy,
)


class Session:
    def __init__(self):
        self.entered = Event()
        self.release = Event()
        self.calls = []
        self.lock = Lock()
        self.active = 0
        self.maximum = 0

    def start_evaluation(self, expression, *, on_transport_dispatch, **kwargs):
        if not expression:
            raise ValueError('empty expression')
        self.pending = PendingEvaluation(TargetId(uuid4(), 'test'), uuid4(), self)
        self.expression = expression
        on_transport_dispatch()
        self.record(expression, expression == 'blocked')
        if expression == 'ambiguous':
            raise EvaluationDispatchUnknown(self.pending)
        return self.pending

    def start_collection_evaluation(self, expression, **kwargs):
        return self.start_evaluation(expression, **kwargs)

    def wait_evaluation_event(self, pending, *, timeout_s):
        assert pending is self.pending
        self.record('event')
        return EvaluationResult(pending.result_id, 'String', self.expression, False)

    def record(self, name, block=False):
        with self.lock:
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            self.calls.append((name, get_ident()))
        try:
            if block:
                self.entered.set()
                assert self.release.wait(3)
            return name
        finally:
            with self.lock:
                self.active -= 1


def evaluate(port, expression):
    pending = port.start_evaluation(expression)
    return port.wait_evaluation_event(pending, timeout_s=1).presentation


@pytest.fixture
def runtime():
    session = Session()
    route = RouteToken('incarnation', 1, 0, 'scope')
    arbiter = RdbgArbiter(session, route)
    yield session, route, arbiter
    session.release.set()
    arbiter.close(timeout=3)


def test_ticket_precedes_dispatch_and_plans_share_one_reader(runtime):
    session, route, arbiter = runtime
    first = arbiter.submit(route, lambda port: Settlement(evaluate(port, 'blocked')))
    assert session.calls == []
    arbiter.dispatch(first)
    assert session.entered.wait(3)
    second = arbiter.submit(route, lambda port: Settlement(evaluate(port, 'events')))
    arbiter.dispatch(second)
    assert [name for name, _ in session.calls] == ['blocked']
    session.release.set()
    assert first.wait(3) == 'blocked'
    assert second.wait(3) == 'events'
    assert session.maximum == 1
    assert len({thread for _, thread in session.calls}) == 1


def test_local_continue_callback_rejection_releases_pretransport_owner() -> None:
    class PredispatchSession:
        target = SimpleNamespace(target_id=TargetId(uuid4(), 'test'))

        def __init__(self) -> None:
            self.transport_calls = 0

        def continue_(self, *, on_transport_dispatch):
            on_transport_dispatch()
            self.transport_calls += 1

    session = PredispatchSession()
    route = RouteToken('incarnation', 1, 0, 'main')
    arbiter = RdbgArbiter(session, route)
    try:
        def reject() -> None:
            raise ValueError('local state rejected Continue')

        ticket = arbiter.submit(
            route, lambda port: Settlement(port.continue_(on_transport_dispatch=reject))
        )
        arbiter.dispatch(ticket)
        with pytest.raises(ValueError, match='local state rejected Continue'):
            ticket.wait(3)
        assert ticket.status().settled
        assert session.transport_calls == 0
    finally:
        arbiter.close(timeout=3)


def test_handoff_rejects_queued_old_epoch_before_effect(runtime):
    session, route, arbiter = runtime
    new_route = RouteToken('incarnation', 2, 0, 'next-scope')
    def handoff(port):
        evaluate(port, 'blocked')
        return Settlement('capture-stopped', next_route=new_route)
    first = arbiter.submit(route, handoff)
    arbiter.dispatch(first)
    assert session.entered.wait(3)
    stale = arbiter.submit(route, lambda port: Settlement(evaluate(port, 'stale')))
    arbiter.dispatch(stale)
    session.release.set()
    assert first.wait(3) == 'capture-stopped'
    with pytest.raises(StaleRoute):
        stale.wait(3)
    assert arbiter.current_route == new_route
    with pytest.raises(StaleRoute):
        arbiter.submit(route, lambda port: Settlement(None))
    assert [name for name, _ in session.calls] == ['blocked', 'event']


def test_queued_cancellation_has_no_effect(runtime):
    session, route, arbiter = runtime
    ticket = arbiter.submit(route, lambda port: Settlement(evaluate(port, 'cancelled')))
    assert ticket.cancel_queued()
    with pytest.raises(CancelledBeforeEffect):
        ticket.wait(3)
    assert session.calls == []


def test_detach_does_not_cancel_late_settlement(runtime):
    session, route, arbiter = runtime
    ticket = arbiter.submit(route, lambda port: Settlement(evaluate(port, 'blocked')))
    arbiter.dispatch(ticket)
    assert session.entered.wait(3)
    ticket.detach_waiter()
    with pytest.raises(WaiterDetached):
        ticket.wait(3)
    session.release.set()
    assert ticket.wait_settled(3) == 'blocked'
    assert ticket.status().settled


def test_unknown_keeps_pending_owner_until_worker_reconciliation(runtime):
    session, route, arbiter = runtime
    def ambiguous(port):
        port.start_evaluation('ambiguous')
        return Settlement('unreachable')
    ticket = arbiter.submit(route, ambiguous)
    arbiter.dispatch(ticket)
    assert ticket.wait_unknown(3)
    capability = session.pending
    assert ticket.status().pending_capability is capability
    later = arbiter.submit(route, lambda port: Settlement(evaluate(port, 'later')))
    arbiter.dispatch(later)
    assert arbiter.active_ticket is ticket
    with pytest.raises(TimeoutError):
        later.wait(0)
    with pytest.raises(ArbiterBusy):
        arbiter.close(timeout=0)
    arbiter.reconcile(ticket, lambda port: Settlement(port.wait_evaluation_event(capability, timeout_s=1).presentation))
    assert ticket.wait(3) == 'ambiguous'
    assert later.wait(3) == 'later'
    assert [name for name, _ in session.calls] == ['ambiguous', 'event', 'later', 'event']
    assert len({thread for _, thread in session.calls}) == 1


def test_unclassified_transport_exception_is_conservatively_unknown(runtime):
    session, route, arbiter = runtime
    def plan(port):
        port.start_evaluation('transport-entry')
        raise OSError('connection lost')
    ticket = arbiter.submit(route, plan)
    arbiter.dispatch(ticket)
    assert ticket.wait_unknown(3)
    arbiter.reconcile(ticket, lambda port: Settlement(port.wait_evaluation_event(session.pending, timeout_s=1).presentation))
    assert ticket.wait(3) == 'transport-entry'


def test_callbacks_can_observe_status_without_mailbox_deadlock(runtime):
    session, route, arbiter = runtime
    def plan(port):
        observed = Event()
        def observer():
            assert arbiter.current_route == route
            assert arbiter.active_ticket is ticket
            observed.set()
        thread = Thread(target=observer, daemon=True)
        thread.start()
        assert observed.wait(3)
        thread.join(3)
        return Settlement('ok')
    ticket = arbiter.submit(route, plan)
    arbiter.dispatch(ticket)
    assert ticket.wait(3) == 'ok'


def test_failed_reconciliation_cannot_release_unknown_owner_without_new_io(runtime):
    session, route, arbiter = runtime
    def unknown(port):
        raise OutcomeUnknown('external activation outcome unknown')
    ticket = arbiter.submit(route, unknown)
    arbiter.dispatch(ticket)
    assert ticket.wait_unknown(3)
    attempted = Event()
    def failed_reconciliation(port):
        attempted.set()
        raise ValueError('local evidence unavailable')
    arbiter.reconcile(ticket, failed_reconciliation)
    assert attempted.wait(3)
    assert ticket.wait_unknown(3)
    assert arbiter.active_ticket is ticket
    arbiter.reconcile(ticket, lambda port: Settlement('confirmed'))
    assert ticket.wait(3) == 'confirmed'


def test_session_port_cannot_escape_worker_plan(runtime):
    session, route, arbiter = runtime
    ports = []
    def plan(port):
        ports.append(port)
        return Settlement('ok')
    ticket = arbiter.submit(route, plan)
    arbiter.dispatch(ticket)
    assert ticket.wait(3) == 'ok'
    with pytest.raises(RuntimeError, match='confined'):
        ports[0].start_evaluation('escaped')
    assert session.calls == []


def test_settled_capture_stop_ticket_preserves_live_main_operation(runtime):
    from onec_runtime.execution.main.operation import MainOperation, MainPhase

    session, route, arbiter = runtime
    parent = MainOperation(command_id=17, target=None)
    parent.continue_acknowledged()
    def plan(port):
        evaluate(port, 'capture-stop')
        parent.phase = MainPhase.SUSPENDED_CAPTURE
        return Settlement(parent)
    ticket = arbiter.submit(route, plan)
    arbiter.dispatch(ticket)
    assert ticket.wait(3) is parent
    assert ticket.status().settled
    assert not parent.terminal
    assert parent.phase is MainPhase.SUSPENDED_CAPTURE


@pytest.mark.parametrize('collection', [False, True])
def test_start_validation_error_is_confirmed_before_dispatch(runtime, collection):
    session, route, arbiter = runtime
    def plan(port):
        if collection:
            port.start_collection_evaluation('', start_index=0)
        else:
            port.start_evaluation('')
        return Settlement(None)
    ticket = arbiter.submit(route, plan)
    arbiter.dispatch(ticket)
    with pytest.raises(ValueError):
        ticket.wait(3)
    assert ticket.status().settled
    assert session.calls == []
    later = arbiter.submit(route, lambda port: Settlement(evaluate(port, 'ok')))
    arbiter.dispatch(later)
    assert later.wait(3) == 'ok'


def test_settlement_cannot_discard_pending_capability(runtime):
    session, route, arbiter = runtime
    def plan(port):
        port.start_evaluation('pending')
        return Settlement('premature')
    ticket = arbiter.submit(route, plan)
    arbiter.dispatch(ticket)
    assert ticket.wait_unknown(3)
    assert ticket.status().pending_capability is session.pending
    arbiter.reconcile(ticket, lambda port: Settlement(port.wait_evaluation_event(session.pending, timeout_s=1).presentation))
    assert ticket.wait(3) == 'pending'


def test_arbitrary_sync_session_methods_are_not_exposed(runtime):
    session, route, arbiter = runtime
    def plan(port):
        for method in ('call', 'evaluate', 'evaluate_collection', 'retain_pending'):
            assert not hasattr(port, method)
        return Settlement('ok')
    ticket = arbiter.submit(route, plan)
    arbiter.dispatch(ticket)
    assert ticket.wait(3) == 'ok'
    assert session.calls == []


def test_foreign_capability_cannot_retire_pending_owner(runtime):
    session, route, arbiter = runtime
    def plan(port):
        pending = port.start_evaluation('pending')
        impostor = PendingEvaluation(pending.target_id, pending.result_id, pending.owner)
        port.wait_evaluation_event(impostor, timeout_s=1)
        return Settlement('invalid')
    ticket = arbiter.submit(route, plan)
    arbiter.dispatch(ticket)
    assert ticket.wait_unknown(3)
    assert ticket.status().pending_capability is session.pending
    assert [name for name, _ in session.calls] == ['pending']
    arbiter.reconcile(ticket, lambda port: Settlement(port.wait_evaluation_event(session.pending, timeout_s=1).presentation))
    assert ticket.wait(3) == 'pending'

@pytest.mark.parametrize('collection', [False, True])
def test_real_session_validation_releases_queue_without_transport(collection):
    from onec_runtime.rdbg.models import DebugTarget, ModuleLocation
    from onec_runtime.rdbg.session import RdbgSession, SessionState

    class NoTransport:
        def request(self, *args, **kwargs):
            raise AssertionError('Validation must precede transport')

    location = ModuleLocation('ExtensionModule', '', uuid4(), uuid4(), 1, 'Test')
    session = RdbgSession(NoTransport(), location)
    session.state = SessionState.READY
    session.target = DebugTarget(TargetId(uuid4(), 'test'), 'ManagedClient', 'stopped')
    route = RouteToken('validation', 1, 0, 'scope')
    arbiter = RdbgArbiter(session, route)
    try:
        def plan(port):
            if collection:
                port.start_collection_evaluation('1', start_index=-1)
            else:
                port.start_evaluation('1', stack_level=-1)
            return Settlement(None)
        ticket = arbiter.submit(route, plan)
        arbiter.dispatch(ticket)
        with pytest.raises(ValueError):
            ticket.wait(3)
        assert ticket.status().settled
        later = arbiter.submit(route, lambda port: Settlement('ready'))
        arbiter.dispatch(later)
        assert later.wait(3) == 'ready'
    finally:
        arbiter.close(timeout=3)


def test_wrong_result_id_does_not_retire_capability(runtime):
    session, route, arbiter = runtime
    original_wait = session.wait_evaluation_event
    session.wait_evaluation_event = lambda pending, **kwargs: EvaluationResult(uuid4(), 'String', 'wrong', False)
    ticket = arbiter.submit(route, lambda port: Settlement(evaluate(port, 'pending')))
    arbiter.dispatch(ticket)
    assert ticket.wait_unknown(3)
    assert ticket.status().pending_capability is session.pending
    session.wait_evaluation_event = original_wait
    arbiter.reconcile(ticket, lambda port: Settlement(port.wait_evaluation_event(session.pending, timeout_s=1).presentation))
    assert ticket.wait(3) == 'pending'


def test_second_start_cannot_overwrite_live_capability(runtime):
    session, route, arbiter = runtime
    def plan(port):
        port.start_evaluation('first')
        port.start_collection_evaluation('second', start_index=0)
        return Settlement(None)
    ticket = arbiter.submit(route, plan)
    arbiter.dispatch(ticket)
    assert ticket.wait_unknown(3)
    assert [name for name, _ in session.calls] == ['first']
    assert ticket.status().pending_capability is session.pending
    arbiter.reconcile(ticket, lambda port: Settlement(port.wait_evaluation_event(session.pending, timeout_s=1).presentation))
    assert ticket.wait(3) == 'first'


def test_sequential_capabilities_keep_logical_slot_until_final_settlement(runtime):
    session, route, arbiter = runtime
    def multi_step(port):
        first = port.start_evaluation('first')
        assert port.wait_evaluation_event(first, timeout_s=1).presentation == 'first'
        second = port.start_collection_evaluation('blocked', start_index=0)
        assert second is not first
        assert port.wait_evaluation_event(second, timeout_s=1).presentation == 'blocked'
        return Settlement('multi-step-complete')
    ticket = arbiter.submit(route, multi_step)
    arbiter.dispatch(ticket)
    assert session.entered.wait(3)
    later = arbiter.submit(route, lambda port: Settlement(evaluate(port, 'later')))
    arbiter.dispatch(later)
    assert arbiter.active_ticket is ticket
    assert not ticket.status().settled
    assert [name for name, _ in session.calls] == ['first', 'event', 'blocked']
    session.release.set()
    assert ticket.wait(3) == 'multi-step-complete'
    assert later.wait(3) == 'later'
    assert [name for name, _ in session.calls] == ['first', 'event', 'blocked', 'event', 'later', 'event']


def test_main_stop_intervals_preserve_one_logical_owner(runtime):
    from onec_runtime.errors import StopWaitIntervalElapsed
    from onec_runtime.rdbg.models import DebugTarget, ModuleLocation, StopEvent
    session, route, arbiter = runtime
    target = TargetId(uuid4(), 'main')
    session.target = DebugTarget(target, 'Server', 'stopped')
    stop = StopEvent(target, ModuleLocation('ExtensionModule', '', uuid4(), uuid4(), 1, 'Test'), 'breakpoint')
    polls = []
    def continue_(*, on_transport_dispatch):
        on_transport_dispatch()
        session.record('continue')
    def wait_for_any_stop(*, timeout_s, expected_target, on_transport_dispatch):
        on_transport_dispatch()
        polls.append(timeout_s)
        if len(polls) < 3:
            raise StopWaitIntervalElapsed('interval')
        session.record('stop', True)
        return stop
    session.continue_ = continue_
    session.wait_for_any_stop = wait_for_any_stop
    def main(port):
        port.continue_()
        while True:
            try:
                result = port.wait_for_any_stop(timeout_s=0.01)
                return Settlement(result)
            except StopWaitIntervalElapsed:
                pass
    ticket = arbiter.submit(route, main)
    arbiter.dispatch(ticket)
    assert session.entered.wait(3)
    later = arbiter.submit(route, lambda port: Settlement(evaluate(port, 'later')))
    arbiter.dispatch(later)
    assert arbiter.active_ticket is ticket
    assert ticket.status().awaiting_stop
    assert [name for name, _ in session.calls] == ['continue', 'stop']
    session.release.set()
    assert ticket.wait(3) is stop
    assert later.wait(3) == 'later'
    assert len(polls) == 3


def test_continue_settlement_without_stop_is_quarantined(runtime):
    from onec_runtime.rdbg.models import DebugTarget, ModuleLocation, StopEvent
    session, route, arbiter = runtime
    target = TargetId(uuid4(), 'main')
    session.target = DebugTarget(target, 'Server', 'stopped')
    session.continue_ = lambda *, on_transport_dispatch: on_transport_dispatch()
    stop = StopEvent(target, ModuleLocation('ExtensionModule', '', uuid4(), uuid4(), 1, 'Test'), 'breakpoint')
    session.wait_for_any_stop = lambda **kwargs: stop
    def main(port):
        port.continue_()
        return Settlement('premature')
    ticket = arbiter.submit(route, main)
    arbiter.dispatch(ticket)
    assert ticket.wait_unknown(3)
    assert ticket.status().awaiting_stop
    arbiter.reconcile(ticket, lambda port: Settlement(port.wait_for_any_stop(timeout_s=1)))
    assert ticket.wait(3) is stop

@pytest.mark.parametrize('failure', ['ambiguous_continue', 'expired_wait'])
def test_main_unknown_can_reconcile_without_repeating_continue(runtime, failure):
    from onec_runtime.errors import StopWaitIntervalElapsed
    from onec_runtime.rdbg.models import DebugTarget, ModuleLocation, StopEvent
    session, route, arbiter = runtime
    target = TargetId(uuid4(), 'main')
    session.target = DebugTarget(target, 'Server', 'stopped')
    stop = StopEvent(target, ModuleLocation('ExtensionModule', '', uuid4(), uuid4(), 1, 'Test'), 'breakpoint')
    def continue_(*, on_transport_dispatch):
        on_transport_dispatch()
        session.record('continue')
        if failure == 'ambiguous_continue':
            raise OSError('ack lost')
    def expired(**kwargs):
        raise StopWaitIntervalElapsed('interval')
    session.continue_ = continue_
    session.wait_for_any_stop = expired
    def main(port):
        port.continue_()
        return Settlement(port.wait_for_any_stop(timeout_s=0.01))
    ticket = arbiter.submit(route, main)
    arbiter.dispatch(ticket)
    assert ticket.wait_unknown(3)
    assert ticket.status().awaiting_stop
    session.wait_for_any_stop = lambda **kwargs: stop
    arbiter.reconcile(ticket, lambda port: Settlement(port.wait_for_any_stop(timeout_s=1)))
    assert ticket.wait(3) is stop
    assert [name for name, _ in session.calls] == ['continue']


def test_ambiguous_modify_cannot_be_swallowed_or_followed_by_new_effects():
    session = Session()
    route = RouteToken('quarantine', 1, 0, 'main')
    arbiter = RdbgArbiter(session, route)
    def modify(variable, value_expression, *, on_transport_dispatch):
        on_transport_dispatch()
        session.record('modify')
        raise OSError('write acknowledgement lost')
    session.modify = modify
    def plan(port):
        try:
            port.modify('command', '1')
        except OSError:
            pass
        return Settlement('not evidence')
    ticket = arbiter.submit(route, plan)
    arbiter.dispatch(ticket)
    assert ticket.wait_unknown(3)
    later = arbiter.submit(route, lambda port: Settlement(evaluate(port, 'unsafe')))
    arbiter.dispatch(later)
    with pytest.raises(TimeoutError):
        later.wait(0)
    with pytest.raises(ArbiterBusy):
        arbiter.close(timeout=0)
    assert [name for name, _ in session.calls] == ['modify']
    # The daemon intentionally retains ownership: no remote evidence permits
    # retirement. The test must not manufacture an acknowledgement for cleanup.


@pytest.mark.parametrize('command', ['set_breakpoints', 'modify', 'continue_'])
def test_main_port_predispatch_error_releases_queue(runtime, command):
    from onec_runtime.rdbg.models import DebugTarget
    session, route, arbiter = runtime
    session.target = DebugTarget(TargetId(uuid4(), 'main'), 'Server', 'stopped')
    def reject(*args, **kwargs):
        raise ValueError('local validation')
    setattr(session, command, reject)
    def plan(port):
        if command == 'set_breakpoints':
            port.set_breakpoints(())
        elif command == 'modify':
            port.modify('', '')
        else:
            port.continue_()
        return Settlement(None)
    ticket = arbiter.submit(route, plan)
    arbiter.dispatch(ticket)
    with pytest.raises(ValueError):
        ticket.wait(3)
    later = arbiter.submit(route, lambda port: Settlement(evaluate(port, 'safe')))
    arbiter.dispatch(later)
    assert later.wait(3) == 'safe'


def test_real_session_foreign_stop_cannot_poison_main_owner():
    from onec_runtime.rdbg.models import DebugTarget, ModuleLocation, StopEvent
    from onec_runtime.rdbg.session import RdbgSession, SessionState

    class Transport:
        def request(self, command, *args, **kwargs):
            assert command == 'step'
            return b''

    location = ModuleLocation('ExtensionModule', '', uuid4(), uuid4(), 1, 'Test')
    session = RdbgSession(Transport(), location)
    expected = DebugTarget(TargetId(uuid4(), 'expected'), 'Server', 'stopped')
    foreign = DebugTarget(TargetId(uuid4(), 'foreign'), 'Server', 'stopped')
    session.target = expected
    session.attached_targets.update({expected.target_id.id: expected, foreign.target_id.id: foreign})
    session.state = SessionState.READY
    expected_stop = StopEvent(expected.target_id, location, 'breakpoint')
    foreign_stop = StopEvent(foreign.target_id, location, 'breakpoint')
    session._event_queue.extend((foreign_stop, expected_stop))
    route = RouteToken('routing', 1, 0, 'main')
    arbiter = RdbgArbiter(session, route)
    def main(port):
        port.continue_()
        return Settlement(port.wait_for_any_stop(timeout_s=1))
    ticket = arbiter.submit(route, main)
    arbiter.dispatch(ticket)
    assert ticket.wait(3) is expected_stop
    assert session.target is expected
    assert not ticket.status().awaiting_stop
    arbiter.close(timeout=3)
    session.state = SessionState.ATTACHED
    assert session.wait_for_any_stop(expected_target=foreign.target_id, timeout_s=1) is foreign_stop
