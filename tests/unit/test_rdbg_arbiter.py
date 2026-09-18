from threading import Event, Lock, Thread, get_ident
from types import SimpleNamespace

from uuid import uuid4

import pytest

import onec_runtime.execution.arbiter as arbiter_module
from arbiter_test_cleanup import confirm_test_server_terminated
from onec_runtime.errors import (
    CommandTimeout, EvaluationDispatchUnknown, LocalVariablesResultTimeout,
    RdbgTransportTimeout,
)
from onec_runtime.rdbg.models import FrameVariable, LocalVariablesResult, PendingEvaluation, TargetId, EvaluationResult


from onec_runtime.execution.arbiter import (
    ConfirmedFailure, RdbgArbiter, RouteToken, Settlement, OutcomeUnknown, StaleRoute,
    CancelledBeforeEffect, WaiterDetached, ArbiterBusy,
)
from onec_runtime.execution.contracts import SubmissionReceipt


class Session:
    def __init__(self):
        self.target = None
        self.entered = Event()
        self.release = Event()
        self.calls = []
        self.lock = Lock()
        self.active = 0
        self.maximum = 0

    def start_evaluation(self, expression, *, on_transport_dispatch, **kwargs):
        if not expression:
            raise ValueError('empty expression')
        self.pending = PendingEvaluation(TargetId(uuid4(), 'test', uuid4()), uuid4(), self)
        self.expression = expression
        on_transport_dispatch()
        self.record(expression, expression == 'blocked')
        if expression == 'ambiguous':
            raise EvaluationDispatchUnknown(self.pending)
        return self.pending

    def start_collection_evaluation(self, expression, **kwargs):
        return self.start_evaluation(expression, **kwargs)

    def wait_evaluation_event(self, pending, *, timeout_s, on_transport_dispatch=None):
        assert pending is self.pending
        self.record('event')
        return EvaluationResult(pending.result_id, 'String', self.expression, False)

    def local_variables(self, stack_level=0, *, timeout_s, on_transport_dispatch):
        on_transport_dispatch()
        self.record('locals')
        return LocalVariablesResult(
            uuid4(), (FrameVariable(f'level{stack_level}', 'Number', '1'),)
        )

    def set_breakpoints(self, locations, *, on_transport_dispatch):
        on_transport_dispatch()
        self.record('set_breakpoints')

    def heartbeat(self, *, on_transport_dispatch):
        for command in ('test-server', 'ping', 'targets'):
            on_transport_dispatch()
            self.record(command)
        return {'rtt_ms': 1.0, 'target_state': 'stopped'}

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


class StoppedEvaluationSession(Session):
    def __init__(self):
        super().__init__()
        self.stop = None
        self.resumed = False
        self.continue_callbacks = 0

    def wait_evaluation_event(self, pending, *, timeout_s, on_transport_dispatch=None):
        assert pending is self.pending
        if not self.resumed:
            if self.stop is None:
                from onec_runtime.rdbg.models import ModuleLocation, StopEvent

                location = ModuleLocation('ExtensionModule', '', uuid4(), uuid4(), 1, 'Test')
                self.stop = StopEvent(pending.target_id, location, 'breakpoint')
            self.record('stop')
            return self.stop
        self.record('result')
        return EvaluationResult(pending.result_id, 'String', self.expression, False)

    def continue_evaluation(self, pending, stop, *, on_transport_dispatch=None):
        assert pending is self.pending
        if on_transport_dispatch is not None:
            self.continue_callbacks += 1
            on_transport_dispatch()
        self.record('step')
        self.resumed = True


def close_stopped_eval_test_arbiter(arbiter, route, ticket, session):
    """Use explicit test target proof only when a RED failure leaves ownership unknown."""
    if ticket.wait_unknown(3):
        confirm_test_server_terminated(
            arbiter, ticket, route, session, session.pending.target_id,
        )
        with pytest.raises(arbiter_module.TargetTerminated):
            ticket.wait_settled(3)
    arbiter.close(timeout=3)


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


def test_local_variables_result_retires_transport_entry_before_next_ticket(runtime):
    session, route, arbiter = runtime
    ticket = arbiter.submit(route, lambda port: Settlement(port.local_variables(stack_level=2, timeout_s=1)))
    arbiter.dispatch(ticket)

    result = ticket.wait(3)
    assert [variable.name for variable in result.variables] == ['level2']
    assert ticket.status().settled
    later = arbiter.submit(route, lambda port: Settlement(evaluate(port, 'later')))
    arbiter.dispatch(later)
    assert later.wait(3) == 'later'
    assert [name for name, _ in session.calls] == ['locals', 'later', 'event']


def test_local_variables_result_timeout_releases_read_only_ticket(runtime):
    session, route, arbiter = runtime

    def missing_result(*, stack_level, timeout_s, on_transport_dispatch):
        on_transport_dispatch()
        session.record('locals-request')
        raise LocalVariablesResultTimeout('No local variables result arrived')

    session.local_variables = missing_result
    ticket = arbiter.submit(route, lambda port: Settlement(port.local_variables(timeout_s=0.1)))
    arbiter.dispatch(ticket)
    with pytest.raises(LocalVariablesResultTimeout):
        ticket.wait_settled(3)
    assert arbiter.active_ticket is None

    successor = arbiter.submit(route, lambda port: Settlement('capture still paused'))
    arbiter.dispatch(successor)
    assert successor.wait(3) == 'capture still paused'


def test_heartbeat_waits_behind_active_eval_and_runs_on_same_worker(runtime):
    session, route, arbiter = runtime
    active = arbiter.submit(route, lambda port: Settlement(evaluate(port, 'blocked')))
    arbiter.dispatch(active)
    assert session.entered.wait(3)
    heartbeat = arbiter.submit(route, lambda port: Settlement(port.heartbeat()))
    arbiter.dispatch(heartbeat)
    assert [name for name, _ in session.calls] == ['blocked']

    session.release.set()
    assert active.wait(3) == 'blocked'
    assert heartbeat.wait(3) == {'rtt_ms': 1.0, 'target_state': 'stopped'}
    assert [name for name, _ in session.calls] == [
        'blocked', 'event', 'test-server', 'ping', 'targets',
    ]
    assert len({thread for _, thread in session.calls}) == 1


def test_best_effort_heartbeat_skips_busy_owner_and_runs_when_idle(runtime):
    session, route, arbiter = runtime
    active = arbiter.submit(route, lambda port: Settlement(evaluate(port, 'blocked')))
    arbiter.dispatch(active)
    try:
        assert session.entered.wait(3)
        assert arbiter.try_heartbeat() is None
        assert arbiter.has_pending_operations
    finally:
        session.release.set()
    assert active.wait_settled(3) == 'blocked'
    heartbeat = arbiter.try_heartbeat()
    assert heartbeat is not None
    assert heartbeat.wait_settled(3) == {'rtt_ms': 1.0, 'target_state': 'stopped'}
    assert len({thread for _, thread in session.calls}) == 1


def test_post_settlement_cleanup_runs_before_later_ticket_and_preserves_parent_reply(runtime):
    session, route, arbiter = runtime
    entered = Event()
    release = Event()

    def user_plan(port):
        port.register_post_settlement_cleanup(
            lambda cleanup_port: Settlement(evaluate(cleanup_port, 'cleanup'))
        )
        entered.set()
        assert release.wait(3)
        return Settlement('user-reply')

    user = arbiter.submit(route, user_plan)
    arbiter.dispatch(user)
    assert entered.wait(3)
    later = arbiter.submit(route, lambda port: Settlement(evaluate(port, 'later')))
    arbiter.dispatch(later)
    release.set()

    assert user.wait(3) == 'user-reply'
    cleanup = user.post_settlement_cleanup
    assert cleanup is not None
    assert cleanup.wait(3) == 'cleanup'
    assert later.wait(3) == 'later'
    assert [name for name, _ in session.calls] == ['cleanup', 'event', 'later', 'event']


def test_close_waits_for_running_post_settlement_cleanup(runtime):
    _session, route, arbiter = runtime
    cleanup_entered = Event()
    release_cleanup = Event()
    close_done = Event()
    close_errors = []

    def plan(port):
        def cleanup(_cleanup_port):
            cleanup_entered.set()
            assert release_cleanup.wait(3)
            return Settlement('released')

        port.register_post_settlement_cleanup(cleanup)
        return Settlement('reply')

    parent = arbiter.submit(route, plan)
    arbiter.dispatch(parent)
    assert parent.wait_settled(3) == 'reply'
    cleanup_ticket = parent.post_settlement_cleanup
    assert cleanup_ticket is not None and cleanup_entered.wait(3)

    def close_owner():
        try:
            arbiter.close(timeout=3)
        except BaseException as error:
            close_errors.append(error)
        finally:
            close_done.set()

    closer = Thread(target=close_owner)
    closer.start()
    try:
        assert not close_done.wait(0.05)
    finally:
        release_cleanup.set()
        closer.join(3)
    assert close_done.is_set()
    assert close_errors == []
    assert cleanup_ticket.wait_settled(0) == 'released'


def test_close_drains_queued_post_settlement_cleanup(runtime, monkeypatch):
    _session, route, arbiter = runtime
    parent_finished = Event()
    release_worker = Event()
    cleanup_ran = Event()
    close_done = Event()
    close_errors = []
    parent_holder = {}
    original_port = arbiter_module.SessionPort

    class PausingPort(original_port):
        def __setattr__(self, name, value):
            if (
                name == '_live' and value is False
                and getattr(self, '_ticket', None) is parent_holder.get('ticket')
            ):
                parent_finished.set()
                assert release_worker.wait(3)
            super().__setattr__(name, value)

    monkeypatch.setattr(arbiter_module, 'SessionPort', PausingPort)

    def plan(port):
        def cleanup(_cleanup_port):
            cleanup_ran.set()
            return Settlement('released')

        port.register_post_settlement_cleanup(cleanup)
        return Settlement('reply')

    parent = arbiter.submit(route, plan)
    parent_holder['ticket'] = parent
    arbiter.dispatch(parent)
    assert parent.wait_settled(3) == 'reply'
    cleanup_ticket = parent.post_settlement_cleanup
    assert cleanup_ticket is not None and parent_finished.wait(3)
    assert cleanup_ticket.status().phase == 'queued'

    def close_owner():
        try:
            arbiter.close(timeout=3)
        except BaseException as error:
            close_errors.append(error)
        finally:
            close_done.set()

    closer = Thread(target=close_owner)
    closer.start()
    try:
        assert not close_done.wait(0.05)
    finally:
        release_worker.set()
        closer.join(3)
    assert close_done.is_set()
    assert close_errors == []
    assert cleanup_ran.is_set()
    assert cleanup_ticket.wait_settled(0) == 'released'


def test_dependent_cleanup_observer_waits_when_cleanup_is_still_queued(
    runtime, monkeypatch,
):
    _session, route, arbiter = runtime
    parent_finished = Event()
    release_worker = Event()
    observer_finished = Event()
    observed = []
    parent_holder = {}
    original_port = arbiter_module.SessionPort

    class PausingPort(original_port):
        def __setattr__(self, name, value):
            if (
                name == '_live' and value is False
                and getattr(self, '_ticket', None) is parent_holder.get('ticket')
            ):
                parent_finished.set()
                assert release_worker.wait(3)
            super().__setattr__(name, value)

    monkeypatch.setattr(arbiter_module, 'SessionPort', PausingPort)

    def parent_plan(port):
        port.register_post_settlement_cleanup(lambda _port: Settlement('released'))
        return Settlement('reply')

    parent = arbiter.submit(route, parent_plan)
    parent_holder['ticket'] = parent
    arbiter.dispatch(parent)
    assert parent.wait_settled(3) == 'reply'
    cleanup = parent.post_settlement_cleanup
    assert cleanup is not None and parent_finished.wait(3)
    assert cleanup.status().phase == 'queued'

    def observe() -> None:
        try:
            observed.append(arbiter.wait_for_dependent_cleanup())
        finally:
            observer_finished.set()

    observer = Thread(target=observe)
    try:
        observer.start()
        assert not observer_finished.wait(0.05)
        release_worker.set()
        observer.join(3)
        assert not observer.is_alive()
        assert observed == [True]
        assert cleanup.wait_settled(0) == 'released'
    finally:
        release_worker.set()
        observer.join(3) if observer.ident is not None else None


def test_close_retains_confirmed_post_settlement_cleanup_debt(runtime):
    _session, route, arbiter = runtime
    attempts = 0

    def cleanup(_port):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ValueError('cleanup rejected')
        return Settlement('released')

    def plan(port):
        port.register_post_settlement_cleanup(cleanup)
        return Settlement('reply')

    parent = arbiter.submit(route, plan)
    arbiter.dispatch(parent)
    assert parent.wait_settled(3) == 'reply'
    first = parent.post_settlement_cleanup
    assert first is not None
    with pytest.raises(ValueError, match='cleanup rejected'):
        first.wait_settled(3)
    with pytest.raises(ArbiterBusy, match='cleanup'):
        arbiter.close(timeout=3)
    assert arbiter.retry_post_settlement_cleanup(parent).wait_settled(3) == 'released'


def test_confirmed_post_settlement_cleanup_failure_is_observable_and_retryable(runtime):
    _session, route, arbiter = runtime
    attempts = []

    def cleanup(_port):
        attempts.append('cleanup')
        if len(attempts) == 1:
            raise ValueError('cleanup rejected')
        return Settlement('released')

    def user_plan(port):
        port.register_post_settlement_cleanup(cleanup)
        return Settlement('user-reply')

    user = arbiter.submit(route, user_plan)
    arbiter.dispatch(user)
    assert user.wait(3) == 'user-reply'
    first = user.post_settlement_cleanup
    assert first is not None
    with pytest.raises(ValueError, match='cleanup rejected'):
        first.wait(3)

    retry = arbiter.retry_post_settlement_cleanup(user)
    assert retry.wait(3) == 'released'
    assert user.post_settlement_cleanup is retry


def test_confirmed_cleanup_debt_holds_already_queued_later_work_until_retry(runtime):
    session, route, arbiter = runtime
    cleanup_entered = Event()
    release_cleanup = Event()
    attempts = []

    def cleanup(_port):
        attempts.append('cleanup')
        if len(attempts) == 1:
            cleanup_entered.set()
            assert release_cleanup.wait(3)
            raise ValueError('cleanup rejected')
        return Settlement('released')

    def user_plan(port):
        port.register_post_settlement_cleanup(cleanup)
        return Settlement('user-reply')

    user = arbiter.submit(route, user_plan)
    arbiter.dispatch(user)
    assert user.wait(3) == 'user-reply'
    first = user.post_settlement_cleanup
    assert first is not None and cleanup_entered.wait(3)
    later = arbiter.submit(route, lambda port: Settlement(evaluate(port, 'later')))
    arbiter.dispatch(later)
    release_cleanup.set()
    with pytest.raises(ValueError, match='cleanup rejected'):
        first.wait(3)
    with pytest.raises(TimeoutError):
        later.wait(0.05)
    assert session.calls == []

    assert arbiter.retry_post_settlement_cleanup(user).wait(3) == 'released'
    assert later.wait(3) == 'later'


def test_post_settlement_cleanup_uses_route_installed_by_parent_settlement(runtime):
    _session, route, arbiter = runtime
    next_route = RouteToken(route.incarnation, route.epoch + 1, 0, 'next')

    def user_plan(port):
        def cleanup(_cleanup_port):
            assert arbiter.current_route == next_route
            return Settlement('released')

        port.register_post_settlement_cleanup(cleanup)
        return Settlement('user-reply', next_route=next_route)

    user = arbiter.submit(route, user_plan)
    arbiter.dispatch(user)
    assert user.wait(3) == 'user-reply'
    cleanup = user.post_settlement_cleanup
    assert cleanup is not None and cleanup.wait(3) == 'released'
    later = arbiter.submit(next_route, lambda _port: Settlement('later'))
    arbiter.dispatch(later)
    assert later.wait(3) == 'later'


def test_post_settlement_cleanup_runs_after_confirmed_parent_exception(runtime):
    _session, route, arbiter = runtime
    cleaned = []

    def user_plan(port):
        port.register_post_settlement_cleanup(
            lambda _cleanup_port: cleaned.append('released') or Settlement('released')
        )
        raise ValueError('instruction rejected')

    user = arbiter.submit(route, user_plan)
    arbiter.dispatch(user)
    with pytest.raises(ValueError, match='instruction rejected'):
        user.wait(3)
    cleanup = user.post_settlement_cleanup
    assert cleanup is not None and cleanup.wait(3) == 'released'
    assert cleaned == ['released']


def test_ambiguous_post_settlement_cleanup_owns_arbiter_after_parent_reply(runtime):
    session, route, arbiter = runtime

    def user_plan(port):
        port.register_post_settlement_cleanup(
            lambda cleanup_port: Settlement(evaluate(cleanup_port, 'ambiguous'))
        )
        return Settlement('user-reply')

    user = arbiter.submit(route, user_plan)
    arbiter.dispatch(user)
    assert user.wait(3) == 'user-reply'
    cleanup = user.post_settlement_cleanup
    assert cleanup is not None and cleanup.wait_unknown(3)
    assert arbiter.active_ticket is cleanup
    assert arbiter.try_heartbeat() is None
    with pytest.raises(ArbiterBusy, match='cleanup outcome unknown'):
        arbiter.close(timeout=0)
    assert arbiter.active_ticket is cleanup
    confirm_test_server_terminated(
        arbiter, cleanup, route, session, session.pending.target_id,
    )


def test_heartbeat_rejects_pending_eval_before_transport(runtime):
    session, route, arbiter = runtime

    def plan(port):
        pending = port.start_evaluation('first')
        try:
            with pytest.raises(ArbiterBusy):
                port.heartbeat()
        finally:
            result = port.wait_evaluation_event(pending, timeout_s=1)
        return Settlement(result.presentation)

    ticket = arbiter.submit(route, plan)
    arbiter.dispatch(ticket)
    assert ticket.wait(3) == 'first'
    assert [name for name, _ in session.calls] == ['first', 'event']


def test_continue_evaluation_dispatches_exact_pending_stop_with_callback():
    session = StoppedEvaluationSession()
    route = RouteToken('eval-stop', 1, 0, 'capture-scope')
    arbiter = RdbgArbiter(session, route)

    def plan(port):
        pending = port.start_evaluation('pending')
        stop = port.wait_evaluation_event(pending, timeout_s=1)
        port.continue_evaluation(pending, stop)
        return Settlement(port.wait_evaluation_event(pending, timeout_s=1).presentation)

    ticket = arbiter.submit(route, plan)
    arbiter.dispatch(ticket)
    try:
        assert ticket.wait(3) == 'pending'
        assert session.continue_callbacks == 1
        assert [name for name, _ in session.calls] == ['pending', 'stop', 'step', 'result']
    finally:
        close_stopped_eval_test_arbiter(arbiter, route, ticket, session)


def test_stop_before_continue_evaluation_prevents_step_for_pending_eval():
    session = StoppedEvaluationSession()
    route = RouteToken('eval-stop-race', 1, 0, 'capture-scope')
    arbiter = RdbgArbiter(session, route)
    stop_received = Event()
    release_plan = Event()

    def plan(port):
        pending = port.start_evaluation('pending')
        stop = port.wait_evaluation_event(pending, timeout_s=1)
        stop_received.set()
        assert release_plan.wait(3)
        port.continue_evaluation(pending, stop)
        return Settlement('unsafe')

    ticket = arbiter.submit(route, plan)
    arbiter.dispatch(ticket)
    assert stop_received.wait(3)
    try:
        assert arbiter.request_stop(ticket).name == 'REQUESTED'
        release_plan.set()
        assert ticket.wait_unknown(3)
        assert ticket.status().pending_capability is session.pending
        assert session.continue_callbacks == 1
        assert [name for name, _ in session.calls] == ['pending', 'stop']
    finally:
        release_plan.set()
        close_stopped_eval_test_arbiter(arbiter, route, ticket, session)


def test_continue_evaluation_refuses_foreign_stop_before_dispatch():
    from onec_runtime.rdbg.models import StopEvent

    session = StoppedEvaluationSession()
    route = RouteToken('eval-foreign-stop', 1, 0, 'capture-scope')
    arbiter = RdbgArbiter(session, route)

    def plan(port):
        pending = port.start_evaluation('pending')
        owned_stop = port.wait_evaluation_event(pending, timeout_s=1)
        foreign_stop = StopEvent(owned_stop.target_id, owned_stop.location, owned_stop.reason)
        with pytest.raises(ValueError, match='owned stop'):
            port.continue_evaluation(pending, foreign_stop)
        port.continue_evaluation(pending, owned_stop)
        return Settlement(port.wait_evaluation_event(pending, timeout_s=1).presentation)

    ticket = arbiter.submit(route, plan)
    arbiter.dispatch(ticket)
    try:
        assert not ticket.wait_unknown(3), 'foreign stop must be rejected inside the plan'
        assert ticket.wait(3) == 'pending'
        assert session.continue_callbacks == 1
        assert [name for name, _ in session.calls] == ['pending', 'stop', 'step', 'result']
    finally:
        close_stopped_eval_test_arbiter(arbiter, route, ticket, session)


def test_interrupted_receipt_adoption_retires_unready_ticket(runtime) -> None:
    session, route, arbiter = runtime

    def interrupt(_ticket) -> None:
        raise KeyboardInterrupt

    receipt = SubmissionReceipt(interrupt)
    with pytest.raises(KeyboardInterrupt):
        arbiter.submit(route, lambda _port: Settlement("orphan"), receipt=receipt)

    assert receipt.ticket is not None
    assert receipt.ticket.status().settled
    assert session.calls == []
    successor = arbiter.submit(route, lambda _port: Settlement("successor"))
    arbiter.dispatch(successor)
    assert successor.wait_settled(3) == "successor"


@pytest.mark.parametrize(
    'outcome', [Settlement('premature'), ConfirmedFailure(ValueError('premature'))],
)
def test_settlement_cannot_discard_pending_evaluation_stop(outcome):
    session = StoppedEvaluationSession()
    route = RouteToken('eval-suspended-stop', 1, 0, 'capture-scope')
    arbiter = RdbgArbiter(session, route)

    def plan(port):
        pending = port.start_evaluation('pending')
        stop = port.wait_evaluation_event(pending, timeout_s=1)
        assert stop is session.stop
        return outcome

    ticket = arbiter.submit(route, plan)
    arbiter.dispatch(ticket)
    try:
        assert ticket.wait_unknown(3)
        assert ticket.status().pending_capability is session.pending
        assert arbiter.active_ticket is ticket
        with pytest.raises(ArbiterBusy):
            arbiter.close(timeout=0)
    finally:
        close_stopped_eval_test_arbiter(arbiter, route, ticket, session)


def test_heartbeat_rejects_pending_continue_stop_before_transport(runtime):
    from onec_runtime.rdbg.models import ModuleLocation, StopEvent

    session, route, arbiter = runtime
    target_id = TargetId(uuid4(), 'main')
    session.target = SimpleNamespace(target_id=target_id)
    stop = StopEvent(target_id, ModuleLocation('ExtensionModule', '', uuid4(), uuid4(), 1, 'Test'), 'breakpoint')

    def continue_(*, on_transport_dispatch):
        on_transport_dispatch()
        session.record('continue')

    def wait_for_any_stop(*, expected_target, on_transport_dispatch, **kwargs):
        assert expected_target == target_id
        on_transport_dispatch()
        session.record('wait-stop')
        return stop

    session.continue_ = continue_
    session.wait_for_any_stop = wait_for_any_stop

    def plan(port):
        port.continue_()
        try:
            with pytest.raises(ArbiterBusy):
                port.heartbeat()
        finally:
            observed_stop = port.wait_for_any_stop(timeout_s=1)
        return Settlement(observed_stop)

    ticket = arbiter.submit(route, plan)
    arbiter.dispatch(ticket)
    assert ticket.wait(3) is stop
    assert [name for name, _ in session.calls] == ['continue', 'wait-stop']


def test_heartbeat_stop_before_first_transport_entry_sends_nothing(runtime):
    session, route, arbiter = runtime
    entered_plan = Event()
    release_plan = Event()

    def plan(port):
        entered_plan.set()
        assert release_plan.wait(3)
        return Settlement(port.heartbeat())

    ticket = arbiter.submit(route, plan)
    arbiter.dispatch(ticket)
    assert entered_plan.wait(3)
    try:
        assert arbiter.request_stop(ticket).name == 'REQUESTED'
    finally:
        release_plan.set()
    with pytest.raises(CancelledBeforeEffect):
        ticket.wait(3)
    assert session.calls == []


def test_local_variables_rejects_outstanding_eval_before_transport(runtime):
    session, route, arbiter = runtime

    def plan(port):
        pending = port.start_evaluation('first')
        try:
            with pytest.raises(ArbiterBusy):
                port.local_variables(timeout_s=1)
        finally:
            result = port.wait_evaluation_event(pending, timeout_s=1)
        return Settlement(result.presentation)

    ticket = arbiter.submit(route, plan)
    arbiter.dispatch(ticket)
    assert ticket.wait(3) == 'first'
    assert [name for name, _ in session.calls] == ['first', 'event']


def test_local_variables_rejects_outstanding_continue_stop_before_transport(runtime):
    from onec_runtime.rdbg.models import ModuleLocation, StopEvent

    session, route, arbiter = runtime
    target_id = TargetId(uuid4(), 'main')
    session.target = SimpleNamespace(target_id=target_id)
    stop = StopEvent(target_id, ModuleLocation('ExtensionModule', '', uuid4(), uuid4(), 1, 'Test'), 'breakpoint')

    def continue_(*, on_transport_dispatch):
        on_transport_dispatch()
        session.record('continue')

    def wait_for_any_stop(*, expected_target, on_transport_dispatch, **kwargs):
        assert expected_target == target_id
        on_transport_dispatch()
        session.record('wait-stop')
        return stop

    session.continue_ = continue_
    session.wait_for_any_stop = wait_for_any_stop

    def plan(port):
        port.continue_()
        try:
            with pytest.raises(ArbiterBusy):
                port.local_variables(timeout_s=1)
        finally:
            observed_stop = port.wait_for_any_stop(timeout_s=1)
        return Settlement(observed_stop)

    ticket = arbiter.submit(route, plan)
    arbiter.dispatch(ticket)
    assert ticket.wait(3) is stop
    assert [name for name, _ in session.calls] == ['continue', 'wait-stop']


def test_local_variables_stop_before_first_transport_entry_sends_nothing(runtime):
    session, route, arbiter = runtime
    entered_plan = Event()
    release_plan = Event()

    def plan(port):
        entered_plan.set()
        assert release_plan.wait(3)
        return Settlement(port.local_variables(timeout_s=1))

    ticket = arbiter.submit(route, plan)
    arbiter.dispatch(ticket)
    assert entered_plan.wait(3)
    try:
        assert arbiter.request_stop(ticket).name == 'REQUESTED'
    finally:
        release_plan.set()
    with pytest.raises(CancelledBeforeEffect):
        ticket.wait(3)
    assert session.calls == []


def test_local_variables_timeout_after_dispatch_keeps_arbiter_owner():
    session = Session()
    target = TargetId(uuid4(), 'capture', uuid4())
    session.target = SimpleNamespace(target_id=target)
    route = RouteToken('locals-timeout', 1, 0, 'capture-scope')
    arbiter = RdbgArbiter(session, route)

    def timed_out(*, stack_level, timeout_s, on_transport_dispatch):
        on_transport_dispatch()
        session.record('locals-request')
        raise RdbgTransportTimeout('HTTP response missing after request entry')

    session.local_variables = timed_out
    ticket = arbiter.submit(route, lambda port: Settlement(port.local_variables(timeout_s=0.1)))
    arbiter.dispatch(ticket)
    assert ticket.wait_unknown(3)
    assert not ticket.status().settled
    assert arbiter.active_ticket is ticket
    later = arbiter.submit(route, lambda port: Settlement(evaluate(port, 'unsafe')))
    arbiter.dispatch(later)
    with pytest.raises(TimeoutError):
        later.wait(0)
    assert [name for name, _ in session.calls] == ['locals-request']

    proof = confirm_test_server_terminated(arbiter, ticket, route, session, target)
    with pytest.raises(arbiter_module.TargetTerminated) as raised:
        ticket.wait_settled(3)
    assert raised.value.evidence is proof
    with pytest.raises(CancelledBeforeEffect):
        later.wait(3)
    with pytest.raises(RuntimeError, match='closed'):
        arbiter.submit(route, lambda port: Settlement('stale'))
    arbiter.close(timeout=3)


def test_heartbeat_timeout_after_transport_entry_keeps_owner_until_target_proof():
    session = Session()
    target = TargetId(uuid4(), 'heartbeat', uuid4())
    session.target = SimpleNamespace(target_id=target)
    route = RouteToken('heartbeat-timeout', 1, 0, 'capture-scope')
    arbiter = RdbgArbiter(session, route)

    def timed_out(*, on_transport_dispatch):
        on_transport_dispatch()
        session.record('test-server')
        raise CommandTimeout('heartbeat transport response missing')

    session.heartbeat = timed_out
    ticket = arbiter.submit(route, lambda port: Settlement(port.heartbeat()))
    arbiter.dispatch(ticket)
    assert ticket.wait_unknown(3)
    assert arbiter.active_ticket is ticket
    later = arbiter.submit(route, lambda port: Settlement(evaluate(port, 'unsafe')))
    arbiter.dispatch(later)
    with pytest.raises(TimeoutError):
        later.wait(0)
    assert [name for name, _ in session.calls] == ['test-server']

    confirm_test_server_terminated(arbiter, ticket, route, session, target)
    with pytest.raises(arbiter_module.TargetTerminated):
        ticket.wait_settled(3)
    with pytest.raises(CancelledBeforeEffect):
        later.wait(3)
    arbiter.close(timeout=3)


def test_stop_between_heartbeat_requests_blocks_later_transport_entry():
    from onec_runtime.rdbg.models import DebugTarget

    session = Session()
    target = TargetId(uuid4(), 'heartbeat', uuid4())
    session.target = DebugTarget(target, 'CLIENT', 'stopped')
    route = RouteToken('heartbeat-stop', 1, 0, 'capture-scope')
    arbiter = RdbgArbiter(session, route)
    first_request_done = Event()
    release_heartbeat = Event()

    def heartbeat(*, on_transport_dispatch):
        on_transport_dispatch()
        session.record('test-server')
        first_request_done.set()
        assert release_heartbeat.wait(3)
        on_transport_dispatch()
        session.record('ping-must-not-run')
        return {'rtt_ms': 1.0}

    session.heartbeat = heartbeat
    ticket = arbiter.submit(route, lambda port: Settlement(port.heartbeat()))
    arbiter.dispatch(ticket)
    assert first_request_done.wait(3)
    try:
        assert arbiter.request_stop(ticket).name == 'REQUESTED'
    finally:
        release_heartbeat.set()
    assert ticket.wait_unknown(3)
    assert [name for name, _ in session.calls] == ['test-server']
    assert arbiter.active_ticket is ticket

    confirm_test_server_terminated(arbiter, ticket, route, session, target)
    with pytest.raises(arbiter_module.TargetTerminated):
        ticket.wait_settled(3)
    arbiter.close(timeout=3)


def test_termination_retirement_requires_exact_route_target_and_confirmed_evidence():
    from onec_runtime.execution.termination import (
        FileTerminationConfirmed, FileTerminationUnknown, ServerTerminationConfirmed,
    )
    from onec_runtime.rdbg.models import DebugTarget
    from onec_runtime.rdbg.session import BoundServerTargetAbsence

    session = Session()
    route = RouteToken('pending-eval', 1, 0, 'capture-scope')
    arbiter = RdbgArbiter(session, route)

    def ambiguous(port):
        port.start_evaluation('ambiguous')
        return Settlement('unreachable')

    ticket = arbiter.submit(route, ambiguous)
    arbiter.dispatch(ticket)
    assert ticket.wait_unknown(3)
    pending = session.pending
    assert ticket.status().pending_capability is pending
    assert not ticket.status().awaiting_stop
    stale_route = RouteToken(route.incarnation, 2, 0, 'another-scope')
    other_target = TargetId(uuid4(), pending.target_id.infobase_alias)

    with pytest.raises(StaleRoute):
        arbiter.retire_terminated_target(
            ticket, stale_route, FileTerminationConfirmed(pending.target_id, 1234, -15)
        )
    with pytest.raises(ValueError, match='target'):
        arbiter.retire_terminated_target(
            ticket, route, FileTerminationConfirmed(other_target, 1234, -15)
        )
    with pytest.raises(ValueError, match='confirmed'):
        arbiter.retire_terminated_target(
            ticket, route, FileTerminationUnknown(pending.target_id, 1234, 'ExitUnverified')
        )
    assert arbiter.active_ticket is ticket
    assert ticket.status().pending_capability is pending
    assert ticket.wait_unknown(0)

    session.target = DebugTarget(pending.target_id, 'Server', 'stopped')
    wrong_client = TargetId(uuid4(), 'another-infobase')
    mismatched_absence = BoundServerTargetAbsence(wrong_client, pending.target_id, 1.0, 2)
    with pytest.raises(ValueError, match='server'):
        arbiter.retire_terminated_target(
            ticket, route, ServerTerminationConfirmed(pending.target_id, mismatched_absence)
        )

    absence = BoundServerTargetAbsence(
        TargetId(uuid4(), pending.target_id.infobase_alias, pending.target_id.seance_id),
        pending.target_id, 1.0, 2,
    )
    proof = ServerTerminationConfirmed(pending.target_id, absence)
    arbiter.retire_terminated_target(ticket, route, proof)
    with pytest.raises(arbiter_module.TargetTerminated) as raised:
        ticket.wait_settled(3)
    assert raised.value.evidence is proof
    assert ticket.status().pending_capability is None
    with pytest.raises(arbiter_module.TargetTerminated) as route_error:
        arbiter.wait_for_dependent_cleanup()
    assert route_error.value.evidence is proof
    arbiter.close(timeout=3)


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


def test_active_worker_handoff_precedes_capture_setup_and_survives_setup_failure(runtime):
    session, main_route, arbiter = runtime
    capture_route = RouteToken('incarnation', 2, 0, 'capture-stop')
    observed = []
    stale_tickets = []
    original_start = session.start_evaluation

    def capture_setup(expression, **kwargs):
        observed.append((arbiter.current_route, arbiter.active_ticket, ticket.status().settled))
        return original_start(expression, **kwargs)

    session.start_evaluation = capture_setup

    def main_to_capture(port):
        stale = arbiter.submit(main_route, lambda p: Settlement(evaluate(p, 'stale')))
        stale_tickets.append(stale)
        arbiter.dispatch(stale)
        port.handoff_route(capture_route)
        port.start_evaluation('')  # Confirmed local setup error, before transport.
        return Settlement('unreachable')

    ticket = arbiter.submit(main_route, main_to_capture)
    arbiter.dispatch(ticket)
    with pytest.raises(ValueError, match='empty expression'):
        ticket.wait(3)
    with pytest.raises(StaleRoute):
        stale_tickets[0].wait(3)
    assert observed == [(capture_route, ticket, False)]
    assert arbiter.current_route == capture_route
    assert session.calls == []


def test_worker_handoff_keeps_pending_eval_capability_on_active_ticket(runtime):
    session, main_route, arbiter = runtime
    capture_route = RouteToken('incarnation', 2, 0, 'capture-stop')
    observed = []

    def main_to_capture(port):
        pending = port.start_evaluation('setup')
        port.handoff_route(capture_route)
        observed.append((arbiter.active_ticket, ticket.status().pending_capability,
                         ticket.status().settled, arbiter.current_route))
        return Settlement(port.wait_evaluation_event(pending, timeout_s=1).presentation)

    ticket = arbiter.submit(main_route, main_to_capture)
    arbiter.dispatch(ticket)
    assert ticket.wait(3) == 'setup'
    owner, pending, settled, route = observed[0]
    assert owner is ticket
    assert pending is session.pending
    assert not settled
    assert route == capture_route
    assert arbiter.current_route == capture_route


def test_stale_settlement_cannot_reverse_worker_route_handoff(runtime):
    _, main_route, arbiter = runtime
    capture_route = RouteToken('incarnation', 2, 0, 'capture-stop')

    def main_to_capture(port):
        port.handoff_route(capture_route)
        return Settlement('wrong-route', next_route=main_route)

    ticket = arbiter.submit(main_route, main_to_capture)
    arbiter.dispatch(ticket)
    with pytest.raises(StaleRoute):
        ticket.wait(3)
    assert arbiter.current_route == capture_route


def test_confirmed_capture_continue_handoffs_to_main_before_waiting_for_stop():
    from onec_runtime.rdbg.models import ModuleLocation, StopEvent

    capture_route = RouteToken('incarnation', 2, 0, 'capture-stop')
    main_route = RouteToken('incarnation', 3, 0, 'main-command')
    target_id = TargetId(uuid4(), 'test')
    location = ModuleLocation('ExtensionModule', '', uuid4(), uuid4(), 1, 'Test')
    stop = StopEvent(target_id, location, 'breakpoint')

    class ContinueSession:
        target = SimpleNamespace(target_id=target_id)

        def continue_(self, *, on_transport_dispatch):
            on_transport_dispatch()

        def wait_for_any_stop(self, *, expected_target, on_transport_dispatch, **kwargs):
            assert expected_target == target_id
            observed.append((arbiter.current_route, arbiter.active_ticket,
                             ticket.status().awaiting_stop, ticket.status().settled))
            on_transport_dispatch()
            return stop

    observed = []
    arbiter = RdbgArbiter(ContinueSession(), capture_route)
    try:
        def resume_capture(port):
            port.continue_()
            port.handoff_route(main_route)
            return Settlement(port.wait_for_any_stop(timeout_s=1))

        ticket = arbiter.submit(capture_route, resume_capture)
        arbiter.dispatch(ticket)
        assert ticket.wait(3) is stop
        assert observed == [(main_route, ticket, True, False)]
        assert arbiter.current_route == main_route
    finally:
        arbiter.close(timeout=3)


def test_route_handoff_rejects_caller_thread_and_expired_port(runtime):
    _, route, arbiter = runtime
    next_route = RouteToken('incarnation', 2, 0, 'capture-stop')
    stored_ports = []
    entered = Event()
    release = Event()

    def plan(port):
        stored_ports.append(port)
        entered.set()
        assert release.wait(3)
        return Settlement('done')

    ticket = arbiter.submit(route, plan)
    arbiter.dispatch(ticket)
    assert entered.wait(3)
    with pytest.raises(RuntimeError, match='confined'):
        stored_ports[0].handoff_route(next_route)
    assert arbiter.current_route == route
    release.set()
    assert ticket.wait(3) == 'done'

    def second_plan(port):
        with pytest.raises(RuntimeError, match='confined'):
            stored_ports[0].handoff_route(next_route)
        return Settlement('still-main')

    second = arbiter.submit(route, second_plan)
    arbiter.dispatch(second)
    assert second.wait(3) == 'still-main'
    assert arbiter.current_route == route


def test_queued_cancellation_has_no_effect(runtime):
    session, route, arbiter = runtime
    ticket = arbiter.submit(route, lambda port: Settlement(evaluate(port, 'cancelled')))
    assert ticket.cancel_queued()
    with pytest.raises(CancelledBeforeEffect):
        ticket.wait(3)
    assert session.calls == []


def test_stop_cancels_queued_ticket_without_touching_active_owner(runtime):
    session, route, arbiter = runtime
    active = arbiter.submit(route, lambda port: Settlement(evaluate(port, 'blocked')))
    arbiter.dispatch(active)
    assert session.entered.wait(3)
    queued = arbiter.submit(route, lambda port: Settlement(evaluate(port, 'must-not-run')))
    arbiter.dispatch(queued)
    try:
        assert arbiter.request_stop(queued).name == 'CANCELLED_BEFORE_EFFECT'
        with pytest.raises(CancelledBeforeEffect):
            queued.wait(3)
        assert arbiter.active_ticket is active
        assert [name for name, _ in session.calls] == ['blocked']
    finally:
        session.release.set()
        active.wait_settled(3)
    assert active.wait(3) == 'blocked'


def test_active_stop_fences_queued_and_new_user_dispatch_until_settlement(runtime):
    session, route, arbiter = runtime
    active = arbiter.submit(route, lambda port: Settlement(evaluate(port, 'blocked')))
    arbiter.dispatch(active)
    assert session.entered.wait(3)
    queued = arbiter.submit(route, lambda port: Settlement(evaluate(port, 'must-not-run')))
    arbiter.dispatch(queued)
    try:
        assert arbiter.request_stop(active).name == 'REQUESTED'
        assert active.status().stop_requested
        assert not active.status().settled
        with pytest.raises(CancelledBeforeEffect):
            queued.wait(3)
        with pytest.raises(ArbiterBusy):
            arbiter.submit(route, lambda port: Settlement(evaluate(port, 'too-late')))
        assert [name for name, _ in session.calls] == ['blocked']
    finally:
        session.release.set()
        active.wait_settled(3)
    assert active.wait(3) == 'blocked'
    assert active.status().stop_requested
    later = arbiter.submit(route, lambda port: Settlement(evaluate(port, 'after-completion')))
    arbiter.dispatch(later)
    assert later.wait(3) == 'after-completion'
    assert [name for name, _ in session.calls] == ['blocked', 'event', 'after-completion', 'event']


def test_completed_ticket_wins_stop_race_without_fencing_route(runtime):
    session, route, arbiter = runtime
    ticket = arbiter.submit(route, lambda port: Settlement(evaluate(port, 'finished')))
    arbiter.dispatch(ticket)
    assert ticket.wait(3) == 'finished'

    assert arbiter.request_stop(ticket).name == 'ALREADY_SETTLED'
    assert not ticket.status().stop_requested
    later = arbiter.submit(route, lambda port: Settlement(evaluate(port, 'later')))
    arbiter.dispatch(later)
    assert later.wait(3) == 'later'


def test_active_stop_before_first_transport_effect_cancels_dispatch(runtime):
    session, route, arbiter = runtime
    entered_plan = Event()
    release_plan = Event()

    def plan(port):
        entered_plan.set()
        assert release_plan.wait(3)
        return Settlement(evaluate(port, 'must-not-run'))

    ticket = arbiter.submit(route, plan)
    arbiter.dispatch(ticket)
    assert entered_plan.wait(3)
    try:
        assert arbiter.request_stop(ticket).name == 'REQUESTED'
    finally:
        release_plan.set()
    with pytest.raises(CancelledBeforeEffect):
        ticket.wait(3)
    assert session.calls == []


def test_stop_fences_dispatch_after_local_continue_callback_rejects_transport(runtime):
    session, route, arbiter = runtime
    session.target = SimpleNamespace(target_id=TargetId(uuid4(), 'main'))
    callback_entered = Event()
    release_callback = Event()

    def continue_(*, on_transport_dispatch):
        on_transport_dispatch()
        session.record('continue-transport')

    def reject_continue():
        callback_entered.set()
        assert release_callback.wait(3)
        raise ValueError('local rejection before transport')

    def plan(port):
        try:
            port.continue_(on_transport_dispatch=reject_continue)
        except ValueError:
            return Settlement(evaluate(port, 'must-not-run'))
        raise AssertionError('Continue callback should reject before transport')

    session.continue_ = continue_
    ticket = arbiter.submit(route, plan)
    arbiter.dispatch(ticket)
    assert callback_entered.wait(3)
    try:
        assert arbiter.request_stop(ticket).name == 'REQUESTED'
    finally:
        release_callback.set()
    with pytest.raises(CancelledBeforeEffect):
        ticket.wait(3)
    assert session.calls == []


def test_stop_after_confirmed_modify_blocks_continue_and_retains_owner():
    session = Session()
    session.target = SimpleNamespace(target_id=TargetId(uuid4(), 'main'))
    route = RouteToken('stop-after-modify', 1, 0, 'main')
    arbiter = RdbgArbiter(session, route)
    modified = Event()
    release_plan = Event()

    def modify(variable, expression, *, on_transport_dispatch):
        on_transport_dispatch()
        session.record('modify')

    def continue_(*, on_transport_dispatch):
        on_transport_dispatch()
        session.record('continue')

    def plan(port):
        port.modify('ТекущаяИнструкция', '1')
        modified.set()
        assert release_plan.wait(3)
        try:
            port.continue_()
        except OutcomeUnknown:
            return Settlement('caught-error-is-not-stop-evidence')
        return Settlement('should-not-complete')

    session.modify = modify
    session.continue_ = continue_
    ticket = arbiter.submit(route, plan)
    arbiter.dispatch(ticket)
    assert modified.wait(3)
    assert arbiter.request_stop(ticket).name == 'REQUESTED'
    release_plan.set()

    assert ticket.wait_unknown(3)
    assert ticket.status().stop_requested
    assert not ticket.status().awaiting_stop
    assert arbiter.active_ticket is ticket
    assert [name for name, _ in session.calls] == ['modify']
    with pytest.raises(ArbiterBusy):
        arbiter.submit(route, lambda port: Settlement('unsafe'))
    # The fake supplies teardown evidence only after the quarantine assertions;
    # production reconciliation must obtain this evidence from the target.
    arbiter.reconcile(ticket, lambda port: Settlement('target-gone'))
    assert ticket.wait_settled(3) == 'target-gone'
    arbiter.close(timeout=3)


def test_modify_callback_rejection_does_not_claim_remote_dispatch(runtime):
    session, route, arbiter = runtime
    entries = []

    def modify(variable, value_expression, *, on_transport_dispatch):
        on_transport_dispatch()
        session.record('modify-must-not-run')

    def reject():
        entries.append('callback')
        raise ValueError('local writeback bookkeeping rejected')

    def plan(port):
        with pytest.raises(ValueError, match='bookkeeping'):
            port.modify('root', 'value', on_transport_dispatch=reject)
        return Settlement(evaluate(port, 'next-safe-operation'))

    session.modify = modify
    ticket = arbiter.submit(route, plan)
    arbiter.dispatch(ticket)
    assert ticket.wait(3) == 'next-safe-operation'
    assert entries == ['callback']
    assert [name for name, _ in session.calls] == [
        'next-safe-operation', 'event',
    ]


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


def test_policy_finalizer_waits_for_reconciled_eval_and_workspace_restore(runtime):
    session, route, arbiter = runtime
    observed = []
    session.target = SimpleNamespace(target_id=TargetId(uuid4(), 'test', uuid4()))

    def initial(port):
        port.start_evaluation('ambiguous')
        return Settlement('unreachable')

    def finish(raw):
        observed.append((raw, get_ident(), tuple(name for name, _ in session.calls)))
        return f'published:{raw}'

    ticket = arbiter.submit(route, initial, finalizer=finish)
    arbiter.dispatch(ticket)
    try:
        assert ticket.wait_unknown(3)
        pending = ticket.status().pending_capability
        assert pending is session.pending
        ticket.detach_waiter()
        with pytest.raises(WaiterDetached):
            ticket.wait_initiator(3)

        def reconcile(port):
            result = port.wait_evaluation_event(pending, timeout_s=1)
            port.set_breakpoints(())  # mandatory workspace restore before publication
            return arbiter_module.ReadyForPolicy(result.presentation)

        arbiter.reconcile(ticket, reconcile)
        assert ticket.wait_settled(3) == 'published:ambiguous'
        assert observed == [('ambiguous', session.calls[0][1], ('ambiguous', 'event', 'set_breakpoints'))]
        assert ticket.status().settled
    finally:
        if ticket.status().phase == 'unknown':
            confirm_test_server_terminated(
                arbiter, ticket, route, session,
                (ticket.status().pending_capability or session.target).target_id,
            )


def test_policy_ticket_reconcile_plain_settlement_cannot_publish_raw_outcome(runtime):
    session, route, arbiter = runtime
    session.target = SimpleNamespace(target_id=TargetId(uuid4(), 'test', uuid4()))
    published = []

    def initial(port):
        port.start_evaluation('ambiguous')
        return Settlement('unreachable')

    ticket = arbiter.submit(route, initial, finalizer=lambda raw: published.append(raw) or 'policy')
    arbiter.dispatch(ticket)
    try:
        assert ticket.wait_unknown(3)
        pending = ticket.status().pending_capability
        arbiter.reconcile(ticket, lambda port: Settlement(
            port.wait_evaluation_event(pending, timeout_s=1).presentation
        ))
        assert ticket.wait_unknown(3)
        assert not ticket.status().settled
        assert arbiter.active_ticket is ticket
        assert published == []
        arbiter.reconcile(ticket, lambda _port: arbiter_module.ReadyForPolicy('confirmed'))
        assert ticket.wait_settled(3) == 'policy'
        assert published == ['confirmed']
    finally:
        if ticket.status().phase == 'unknown':
            confirm_test_server_terminated(
                arbiter, ticket, route, session,
                (ticket.status().pending_capability or session.target).target_id,
            )


def test_policy_finalizer_failure_is_not_retried_by_reconciliation(runtime):
    session, route, arbiter = runtime
    session.target = SimpleNamespace(target_id=TargetId(uuid4(), 'test', uuid4()))
    calls = []

    def finish(raw):
        calls.append(raw)
        raise RuntimeError('publication partially failed')

    ticket = arbiter.submit(
        route,
        lambda port: arbiter_module.ReadyForPolicy(
            port.local_variables(stack_level=0, timeout_s=1).variables[0].name
        ),
        finalizer=finish,
    )
    arbiter.dispatch(ticket)
    try:
        assert ticket.wait_unknown(3)
        assert calls == ['level0']
        arbiter.reconcile(ticket, lambda _port: arbiter_module.ReadyForPolicy('level0'))
        assert ticket.wait_unknown(3)
        assert calls == ['level0']
        assert arbiter.active_ticket is ticket
        arbiter.reconcile(ticket, lambda _port: ConfirmedFailure(ValueError('skip policy')))
        assert ticket.wait_unknown(3)
        assert calls == ['level0']
        assert arbiter.active_ticket is ticket
    finally:
        if ticket.status().phase == 'unknown':
            confirm_test_server_terminated(
                arbiter, ticket, route, session,
                (ticket.status().pending_capability or session.target).target_id,
            )


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
    with pytest.raises(RuntimeError, match='confined'):
        ports[0].local_variables(timeout_s=1)
    with pytest.raises(RuntimeError, match='confined'):
        ports[0].heartbeat()
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


def test_real_rdbg_local_variables_callback_flows_through_arbiter(monkeypatch):
    from onec_runtime.rdbg.models import DebugTarget, ModuleLocation
    from onec_runtime.rdbg.session import RdbgSession, SessionState
    from onec_runtime.rdbg.xml_codec import CALC_NS, RDBG_NS

    result_id = uuid4()

    class Transport:
        def __init__(self):
            self.calls = []

        def request(self, command, payload=b'', **kwargs):
            self.calls.append(command)
            assert command == 'evalLocalVariables'
            return f'''<response xmlns="{RDBG_NS}"><result>
              <expressionResultID xmlns="{CALC_NS}">{result_id}</expressionResultID>
              <calculationResult xmlns="{CALC_NS}"><valueOfContextPropInfo>
                <propInfo><propName>Переменная</propName></propInfo>
                <valueInfo><typeName>Число</typeName><pres>MQ==</pres></valueInfo>
              </valueOfContextPropInfo></calculationResult>
            </result></response>'''.encode()

    transport = Transport()
    location = ModuleLocation('ExtensionModule', '', uuid4(), uuid4(), 1, 'Test')
    session = RdbgSession(transport, location)
    session.state = SessionState.READY
    session.target = DebugTarget(TargetId(uuid4(), 'test'), 'Server', 'stopped')
    monkeypatch.setattr('onec_runtime.rdbg.session.uuid4', lambda: result_id)
    route = RouteToken('real-locals', 1, 0, 'capture-scope')
    arbiter = RdbgArbiter(session, route)
    try:
        ticket = arbiter.submit(route, lambda port: Settlement(port.local_variables(stack_level=2, timeout_s=1)))
        arbiter.dispatch(ticket)
        result = ticket.wait(3)
        assert result.result_id == result_id
        assert [variable.name for variable in result.variables] == ['Переменная']
        assert ticket.status().settled
        assert transport.calls == ['evalLocalVariables']
    finally:
        arbiter.close(timeout=3)


def test_real_rdbg_heartbeat_runs_all_requests_on_arbiter_worker():
    from onec_runtime.rdbg.models import DebugTarget, ModuleLocation
    from onec_runtime.rdbg.session import RdbgSession, SessionState
    from onec_runtime.rdbg.xml_codec import BASE_NS, RDBG_NS

    target = TargetId(uuid4(), 'DefAlias')

    class Transport:
        def __init__(self):
            self.calls = []

        def test_server(self):
            self.calls.append(('test-server', get_ident()))
            return 1.0

        def request(self, command, payload=b'', **kwargs):
            self.calls.append((command, get_ident()))
            if command == 'pingDebugUIParams':
                return b''
            assert command == 'getDbgAllTargetStates'
            return f'''<response xmlns="{RDBG_NS}"><result>success</result><item>
              <targetIDStr>target</targetIDStr><targetID xmlns="{BASE_NS}">
              <id>{target.id}</id><infoBaseAlias>DefAlias</infoBaseAlias>
              <targetType>ServerEmulation</targetType></targetID>
              <stateNum>1</stateNum><state>stopped</state>
              </item></response>'''.encode()

    transport = Transport()
    location = ModuleLocation('ExtensionModule', '', uuid4(), uuid4(), 1, 'Test')
    session = RdbgSession(transport, location)
    session.state = SessionState.READY
    session.target = DebugTarget(target, 'ServerEmulation', 'stopped')
    route = RouteToken('real-heartbeat', 1, 0, 'capture-scope')
    arbiter = RdbgArbiter(session, route)
    try:
        ticket = arbiter.submit(route, lambda port: Settlement(port.heartbeat()))
        arbiter.dispatch(ticket)
        assert ticket.wait(3) == {'rtt_ms': 1.0, 'target_state': 'stopped'}
        assert [command for command, _ in transport.calls] == [
            'test-server', 'pingDebugUIParams', 'getDbgAllTargetStates',
        ]
        assert len({thread for _, thread in transport.calls}) == 1
        assert ticket.status().settled
    finally:
        arbiter.close(timeout=3)


def test_stop_between_eval_ping_and_autoattach_fences_hidden_rdbg_effect():
    from onec_runtime.rdbg.models import DebugTarget, ModuleLocation
    from onec_runtime.rdbg.session import RdbgSession, SessionState
    from onec_runtime.rdbg.xml_codec import BASE_NS, RDBG_NS

    target = TargetId(uuid4(), 'DefAlias', uuid4())
    discovered_id = uuid4()
    ping_entered = Event()
    release_ping = Event()

    class Transport:
        def __init__(self):
            self.calls = []

        def request(self, command, payload=b'', **kwargs):
            self.calls.append(command)
            if command == 'pingDebugUIParams':
                ping_entered.set()
                assert release_ping.wait(3)
                return f'''<response xmlns="{RDBG_NS}"><result><cmdID>targetStarted</cmdID>
                  <targetID xmlns="{BASE_NS}"><id>{discovered_id}</id>
                  <infoBaseAlias>DefAlias</infoBaseAlias>
                  <targetType>ManagedClient</targetType></targetID></result></response>'''.encode()
            return b''

    transport = Transport()
    location = ModuleLocation('ExtensionModule', '', uuid4(), uuid4(), 1, 'Test')
    session = RdbgSession(transport, location)
    session.state = SessionState.READY
    session.target = DebugTarget(target, 'ServerEmulation', 'stopped')
    session.attached_targets[target.id] = session.target
    route = RouteToken('eval-ping-stop', 1, 0, 'capture-scope')
    arbiter = RdbgArbiter(session, route)

    def plan(port):
        pending = port.start_evaluation('1')
        return Settlement(port.wait_evaluation_event(pending, timeout_s=1))

    ticket = arbiter.submit(route, plan)
    arbiter.dispatch(ticket)
    assert ping_entered.wait(3)
    try:
        assert arbiter.request_stop(ticket).name == 'REQUESTED'
        release_ping.set()
        assert ticket.wait_unknown(3)
        assert transport.calls == ['evalExpr', 'pingDebugUIParams']
        assert arbiter.active_ticket is ticket
    finally:
        release_ping.set()
        if ticket.wait_unknown(3):
            confirm_test_server_terminated(arbiter, ticket, route, session, target)
            with pytest.raises(arbiter_module.TargetTerminated):
                ticket.wait_settled(3)
        arbiter.close(timeout=3)


def test_stop_after_ping_reconciles_result_despite_fenced_autoattach():
    from onec_runtime.rdbg.models import DebugTarget, ModuleLocation
    from onec_runtime.rdbg.session import RdbgSession, SessionState
    from onec_runtime.rdbg.xml_codec import BASE_NS, CALC_NS, RDBG_NS

    target = TargetId(uuid4(), 'DefAlias', uuid4())
    discovered_id = uuid4()
    ping_entered = Event()
    release_ping = Event()

    class Transport:
        def __init__(self):
            self.calls = []

        def request(self, command, payload=b'', **kwargs):
            self.calls.append(command)
            if command == 'pingDebugUIParams':
                ping_entered.set()
                assert release_ping.wait(3)
                pending = next(iter(session._pending_evaluation_states.values())).capability
                return f'''<response xmlns="{RDBG_NS}">
                  <result><cmdID>targetStarted</cmdID>
                    <targetID xmlns="{BASE_NS}"><id>{discovered_id}</id>
                    <infoBaseAlias>DefAlias</infoBaseAlias>
                    <targetType>ManagedClient</targetType></targetID></result>
                  <result><cmdID>exprEvaluated</cmdID><evalExprResBaseData>
                    <expressionResultID xmlns="{CALC_NS}">{pending.result_id}</expressionResultID>
                    <resultValueInfo xmlns="{CALC_NS}"><typeName>Число</typeName><pres>MQ==</pres></resultValueInfo>
                    <errorOccurred xmlns="{CALC_NS}">false</errorOccurred>
                  </evalExprResBaseData></result></response>'''.encode()
            return b''

    transport = Transport()
    location = ModuleLocation('ExtensionModule', '', uuid4(), uuid4(), 1, 'Test')
    session = RdbgSession(transport, location)
    session.state = SessionState.READY
    session.target = DebugTarget(target, 'ServerEmulation', 'stopped')
    session.attached_targets[target.id] = session.target
    route = RouteToken('eval-ping-result-stop', 1, 0, 'capture-scope')
    arbiter = RdbgArbiter(session, route)

    def plan(port):
        pending = port.start_evaluation('1')
        return Settlement(port.wait_evaluation_event(pending, timeout_s=1).presentation)

    ticket = arbiter.submit(route, plan)
    arbiter.dispatch(ticket)
    assert ping_entered.wait(3)
    try:
        assert arbiter.request_stop(ticket).name == 'REQUESTED'
        release_ping.set()
        assert ticket.wait_unknown(3)
        assert transport.calls == ['evalExpr', 'pingDebugUIParams']
        pending = ticket.status().pending_capability
        assert pending is not None
        arbiter.reconcile(ticket, lambda port: Settlement(
            port.wait_evaluation_event(pending, timeout_s=0.01).presentation,
        ))
        assert ticket.wait_settled(3) == '1'
        assert transport.calls == ['evalExpr', 'pingDebugUIParams']
    finally:
        release_ping.set()
        if ticket.wait_unknown(3):
            confirm_test_server_terminated(arbiter, ticket, route, session, target)
            with pytest.raises(arbiter_module.TargetTerminated):
                ticket.wait_settled(3)
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


def test_stop_after_empty_main_wait_interval_yields_owner_to_reconciliation(runtime):
    from onec_runtime.errors import StopWaitIntervalElapsed
    from onec_runtime.rdbg.models import DebugTarget, ModuleLocation, StopEvent

    session, route, arbiter = runtime
    target = TargetId(uuid4(), 'main')
    session.target = DebugTarget(target, 'Server', 'stopped')
    stop = StopEvent(target, ModuleLocation('ExtensionModule', '', uuid4(), uuid4(), 1, 'Test'), 'breakpoint')
    entered_wait = Event()
    release_wait = Event()
    wait_threads = []

    def continue_(*, on_transport_dispatch):
        on_transport_dispatch()
        session.record('continue')

    def wait_for_any_stop(*, expected_target, on_transport_dispatch, **kwargs):
        assert expected_target == target
        wait_threads.append(get_ident())
        if len(wait_threads) == 1:
            on_transport_dispatch()
            entered_wait.set()
            assert release_wait.wait(3)
            raise StopWaitIntervalElapsed('empty interval')
        return stop  # Already queued: no new transport dispatch.

    session.continue_ = continue_
    session.wait_for_any_stop = wait_for_any_stop

    def main(port):
        port.continue_()
        while True:
            try:
                return Settlement(port.wait_for_any_stop(timeout_s=0.01))
            except StopWaitIntervalElapsed:
                continue

    ticket = arbiter.submit(route, main)
    arbiter.dispatch(ticket)
    assert entered_wait.wait(3)
    queued = arbiter.submit(route, lambda port: Settlement(evaluate(port, 'unsafe')))
    arbiter.dispatch(queued)
    try:
        assert arbiter.request_stop(ticket).name == 'REQUESTED'
        with pytest.raises(CancelledBeforeEffect):
            queued.wait(3)
        with pytest.raises(ArbiterBusy):
            arbiter.submit(route, lambda port: Settlement('unsafe'))
    finally:
        release_wait.set()

    assert ticket.wait_unknown(3)
    assert len(wait_threads) == 1
    assert ticket.status().awaiting_stop
    assert arbiter.active_ticket is ticket
    arbiter.reconcile(ticket, lambda port: Settlement(port.wait_for_any_stop(timeout_s=0.01)))
    assert ticket.wait_settled(3) is stop
    assert len(wait_threads) == 2
    assert wait_threads[0] == wait_threads[1] == session.calls[0][1]
    assert [name for name, _ in session.calls] == ['continue']


def test_stop_after_empty_capture_eval_interval_retains_capability_for_reconciliation(runtime):
    from onec_runtime.errors import CommandTimeout

    session, route, arbiter = runtime
    entered_wait = Event()
    release_wait = Event()
    wait_threads = []

    def wait_evaluation_event(pending, *, timeout_s, on_transport_dispatch=None):
        assert pending is session.pending
        wait_threads.append(get_ident())
        if len(wait_threads) == 1:
            entered_wait.set()
            assert release_wait.wait(3)
            raise CommandTimeout('empty interval')
        return EvaluationResult(pending.result_id, 'String', 'late-result', False)

    session.wait_evaluation_event = wait_evaluation_event

    def capture(port):
        pending = port.start_evaluation('long-running')
        while True:
            try:
                return Settlement(port.wait_evaluation_event(pending, timeout_s=0.01).presentation)
            except CommandTimeout:
                continue

    ticket = arbiter.submit(route, capture)
    arbiter.dispatch(ticket)
    assert entered_wait.wait(3)
    queued = arbiter.submit(route, lambda port: Settlement(evaluate(port, 'unsafe')))
    arbiter.dispatch(queued)
    try:
        assert arbiter.request_stop(ticket).name == 'REQUESTED'
        with pytest.raises(CancelledBeforeEffect):
            queued.wait(3)
        with pytest.raises(ArbiterBusy):
            arbiter.submit(route, lambda port: Settlement('unsafe'))
    finally:
        release_wait.set()

    assert ticket.wait_unknown(3)
    assert len(wait_threads) == 1
    assert ticket.status().pending_capability is session.pending
    assert arbiter.active_ticket is ticket
    arbiter.reconcile(ticket, lambda port: Settlement(
        port.wait_evaluation_event(session.pending, timeout_s=0.01).presentation))
    assert ticket.wait_settled(3) == 'late-result'
    assert len(wait_threads) == 2
    assert wait_threads[0] == wait_threads[1] == session.calls[0][1]
    assert [name for name, _ in session.calls] == ['long-running']


def test_eval_transport_timeout_escapes_broad_interval_retry_with_pending_owner(runtime):
    from onec_runtime.errors import CommandTimeout, RdbgTransportTimeout

    session, route, arbiter = runtime
    waits = []

    def wait_evaluation_event(pending, *, timeout_s, on_transport_dispatch=None):
        assert pending is session.pending
        waits.append(get_ident())
        if len(waits) == 1:
            raise RdbgTransportTimeout('RDBG poll outcome is unknown')
        return EvaluationResult(pending.result_id, 'String', 'confirmed-result', False)

    session.wait_evaluation_event = wait_evaluation_event

    def capture(port):
        pending = port.start_evaluation('long-running')
        while True:
            try:
                return Settlement(port.wait_evaluation_event(pending, timeout_s=0.01).presentation)
            except CommandTimeout:
                continue  # Existing broad executor loops must not absorb transport failure.

    ticket = arbiter.submit(route, capture)
    arbiter.dispatch(ticket)
    assert ticket.wait_unknown(3)
    assert len(waits) == 1
    assert ticket.status().pending_capability is session.pending
    assert arbiter.active_ticket is ticket
    arbiter.reconcile(ticket, lambda port: Settlement(
        port.wait_evaluation_event(session.pending, timeout_s=0.01).presentation))
    assert ticket.wait_settled(3) == 'confirmed-result'
    assert len(waits) == 2
    assert waits[0] == waits[1] == session.calls[0][1]


def test_matching_main_stop_wins_stop_request_during_wait(runtime):
    from onec_runtime.rdbg.models import DebugTarget, ModuleLocation, StopEvent

    session, route, arbiter = runtime
    target = TargetId(uuid4(), 'main')
    session.target = DebugTarget(target, 'Server', 'stopped')
    stop = StopEvent(target, ModuleLocation('ExtensionModule', '', uuid4(), uuid4(), 1, 'Test'), 'breakpoint')
    entered_wait = Event()
    release_wait = Event()
    session.continue_ = lambda *, on_transport_dispatch: on_transport_dispatch()

    def wait_for_any_stop(*, expected_target, on_transport_dispatch, **kwargs):
        assert expected_target == target
        on_transport_dispatch()
        entered_wait.set()
        assert release_wait.wait(3)
        return stop

    session.wait_for_any_stop = wait_for_any_stop

    def main(port):
        port.continue_()
        return Settlement(port.wait_for_any_stop(timeout_s=0.01))

    ticket = arbiter.submit(route, main)
    arbiter.dispatch(ticket)
    assert entered_wait.wait(3)
    try:
        assert arbiter.request_stop(ticket).name == 'REQUESTED'
    finally:
        release_wait.set()
    assert ticket.wait_settled(3) is stop
    assert ticket.status().settled
    assert not ticket.status().awaiting_stop
    assert arbiter.active_ticket is None


def test_matching_capture_result_wins_stop_request_during_wait(runtime):
    session, route, arbiter = runtime
    entered_wait = Event()
    release_wait = Event()

    def wait_evaluation_event(pending, *, timeout_s, on_transport_dispatch=None):
        assert pending is session.pending
        entered_wait.set()
        assert release_wait.wait(3)
        return EvaluationResult(pending.result_id, 'String', 'completed', False)

    session.wait_evaluation_event = wait_evaluation_event

    def capture(port):
        pending = port.start_evaluation('long-running')
        return Settlement(port.wait_evaluation_event(pending, timeout_s=0.01).presentation)

    ticket = arbiter.submit(route, capture)
    arbiter.dispatch(ticket)
    assert entered_wait.wait(3)
    try:
        assert arbiter.request_stop(ticket).name == 'REQUESTED'
    finally:
        release_wait.set()
    assert ticket.wait_settled(3) == 'completed'
    assert ticket.status().settled
    assert ticket.status().pending_capability is None
    assert arbiter.active_ticket is None


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


def _unknown_server_stop(*, absence_results, block_confirmation=None,
                         handoff_to_capture=False):
    from onec_runtime.rdbg.models import DebugTarget
    from onec_runtime.rdbg.session import BoundServerTargetAbsence

    target = TargetId(uuid4(), 'DefAlias', uuid4())
    client = TargetId(uuid4(), 'DefAlias', target.seance_id)
    evidence = BoundServerTargetAbsence(client, target, 1.0, 1)

    class ServerSession:
        def __init__(self):
            self.target = DebugTarget(target, 'Server', 'stopped')
            self.calls = []
            self.absence_results = list(absence_results)
            self.confirmation_entered = Event()

        def continue_(self, *, on_transport_dispatch):
            on_transport_dispatch()
            self.calls.append(('continue', get_ident()))

        def terminate_bound_server_session(self):
            self.calls.append(('terminate', get_ident()))
            return True

        def wait_for_bound_server_targets_absent(self, expected_target, *, timeout_s):
            self.calls.append(('confirm', get_ident(), expected_target, timeout_s))
            self.confirmation_entered.set()
            if block_confirmation is not None:
                assert block_confirmation.wait(3)
            outcome = self.absence_results.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return evidence if outcome is None else outcome

    session = ServerSession()
    route = RouteToken('server-stop', 1, 0, 'main')
    capture_route = RouteToken(route.incarnation, 2, 0, 'capture-scope')
    arbiter = RdbgArbiter(session, route)

    def unknown_main(port):
        port.continue_()
        if handoff_to_capture:
            port.handoff_route(capture_route)
        raise OutcomeUnknown('stop is not correlated')

    ticket = arbiter.submit(route, unknown_main)
    arbiter.dispatch(ticket)
    assert ticket.wait_unknown(3)
    return session, capture_route if handoff_to_capture else route, arbiter, ticket, target, evidence


def test_server_owner_rejects_file_exit_proof_without_file_process_lease():
    from onec_runtime.execution.termination import (
        FileTerminationConfirmed, ServerTerminationConfirmed,
    )

    _session, route, arbiter, ticket, target, absence = _unknown_server_stop(
        absence_results=[None],
    )
    try:
        with pytest.raises(ValueError, match='file.*backend|file.*lease'):
            arbiter.retire_terminated_target(
                ticket, route, FileTerminationConfirmed(target, 1234, -15),
            )
        assert arbiter.active_ticket is ticket
        assert ticket.wait_unknown(0)
    finally:
        if arbiter.active_ticket is ticket:
            arbiter.retire_terminated_target(
                ticket, route, ServerTerminationConfirmed(target, absence),
            )
        arbiter.close(timeout=3)


def test_server_exit_proof_requires_current_bound_server_target():
    from onec_runtime.execution.termination import ServerTerminationConfirmed
    from onec_runtime.rdbg.models import DebugTarget

    session, route, arbiter, ticket, target, absence = _unknown_server_stop(
        absence_results=[None],
    )
    selected = session.target
    session.target = DebugTarget(target, 'ServerEmulation', 'stopped')
    try:
        with pytest.raises(ValueError, match='server.*backend|selected server target'):
            arbiter.retire_terminated_target(
                ticket, route, ServerTerminationConfirmed(target, absence),
            )
        assert arbiter.active_ticket is ticket
        assert ticket.wait_unknown(0)
    finally:
        session.target = selected
        if arbiter.active_ticket is ticket:
            arbiter.retire_terminated_target(
                ticket, route, ServerTerminationConfirmed(target, absence),
            )
        arbiter.close(timeout=3)


def test_stop_automatically_terminates_fenced_server_target_on_same_worker():
    from onec_runtime.execution.termination import ServerTerminationConfirmed

    session, route, arbiter, ticket, target, evidence = _unknown_server_stop(
        absence_results=[None],
    )

    assert arbiter.request_stop(ticket).name == 'REQUESTED'
    attempt = ticket.stop_teardown
    assert attempt is not None
    result = attempt.wait(3)
    assert isinstance(result, ServerTerminationConfirmed)
    assert result.expected_target == target and result.absence is evidence
    with pytest.raises(arbiter_module.TargetTerminated):
        ticket.wait_settled(3)
    assert [call[0] for call in session.calls] == ['continue', 'terminate', 'confirm']
    assert len({call[1] for call in session.calls}) == 1
    arbiter.close(timeout=3)


def test_stop_during_main_wait_automatically_terminates_after_worker_checkpoint():
    from onec_runtime.errors import StopWaitIntervalElapsed
    from onec_runtime.execution.termination import ServerTerminationConfirmed
    from onec_runtime.rdbg.models import DebugTarget
    from onec_runtime.rdbg.session import BoundServerTargetAbsence

    target = TargetId(uuid4(), 'DefAlias', uuid4())
    client = TargetId(uuid4(), 'DefAlias', target.seance_id)
    evidence = BoundServerTargetAbsence(client, target, 1.0, 1)
    entered_wait = Event()
    release_wait = Event()

    class ServerSession:
        def __init__(self):
            self.target = DebugTarget(target, 'Server', 'running')
            self.calls = []

        def continue_(self, *, on_transport_dispatch):
            on_transport_dispatch()
            self.calls.append(('continue', get_ident()))

        def wait_for_any_stop(self, *, timeout_s, expected_target, on_transport_dispatch):
            assert expected_target == target
            on_transport_dispatch()
            self.calls.append(('wait', get_ident()))
            entered_wait.set()
            assert release_wait.wait(3)
            raise StopWaitIntervalElapsed('poll interval')

        def terminate_bound_server_session(self):
            self.calls.append(('terminate', get_ident()))
            return True

        def wait_for_bound_server_targets_absent(self, expected_target, *, timeout_s):
            self.calls.append(('confirm', get_ident()))
            assert expected_target == target
            return evidence

    session = ServerSession()
    route = RouteToken('main-auto-stop', 1, 0, 'main')
    arbiter = RdbgArbiter(session, route)

    def main(port):
        port.continue_()
        while True:
            try:
                return Settlement(port.wait_for_any_stop(timeout_s=0.01))
            except StopWaitIntervalElapsed:
                continue

    ticket = arbiter.submit(route, main)
    arbiter.dispatch(ticket)
    assert entered_wait.wait(3)
    assert arbiter.request_stop(ticket).name == 'REQUESTED'
    assert ticket.stop_teardown is None  # One worker still owns the wait.
    release_wait.set()
    with pytest.raises(arbiter_module.TargetTerminated):
        ticket.wait_settled(3)
    attempt = ticket.stop_teardown
    assert attempt is not None
    assert isinstance(attempt.wait(3), ServerTerminationConfirmed)
    assert [name for name, _ in session.calls] == ['continue', 'wait', 'terminate', 'confirm']
    assert len({thread for _, thread in session.calls}) == 1
    arbiter.close(timeout=3)


def test_stop_during_capture_eval_retains_pending_until_target_absence():
    from onec_runtime.execution.termination import ServerTerminationConfirmed
    from onec_runtime.rdbg.models import DebugTarget
    from onec_runtime.rdbg.session import BoundServerTargetAbsence

    target = TargetId(uuid4(), 'DefAlias', uuid4())
    client = TargetId(uuid4(), 'DefAlias', target.seance_id)
    evidence = BoundServerTargetAbsence(client, target, 1.0, 1)
    entered_wait = Event()
    release_wait = Event()
    entered_confirmation = Event()
    release_confirmation = Event()

    class ServerSession:
        def __init__(self):
            self.target = DebugTarget(target, 'Server', 'stopped')
            self.pending = PendingEvaluation(target, uuid4(), self)
            self.calls = []

        def start_evaluation(self, expression, *, on_transport_dispatch, **kwargs):
            on_transport_dispatch()
            self.calls.append(('eval', get_ident()))
            return self.pending

        def wait_evaluation_event(self, pending, *, timeout_s, on_transport_dispatch):
            assert pending is self.pending
            on_transport_dispatch()
            self.calls.append(('wait', get_ident()))
            entered_wait.set()
            assert release_wait.wait(3)
            raise CommandTimeout('empty poll interval')

        def terminate_bound_server_session(self):
            self.calls.append(('terminate', get_ident()))
            return True

        def wait_for_bound_server_targets_absent(self, expected_target, *, timeout_s):
            assert expected_target == target
            self.calls.append(('confirm', get_ident()))
            entered_confirmation.set()
            assert release_confirmation.wait(3)
            return evidence

    session = ServerSession()
    route = RouteToken('capture-auto-stop', 1, 0, 'capture-scope')
    arbiter = RdbgArbiter(session, route)

    def capture(port):
        pending = port.start_evaluation('long-running')
        while True:
            try:
                return Settlement(port.wait_evaluation_event(pending, timeout_s=0.01))
            except CommandTimeout:
                continue

    ticket = arbiter.submit(route, capture)
    arbiter.dispatch(ticket)
    assert entered_wait.wait(3)
    assert arbiter.request_stop(ticket).name == 'REQUESTED'
    release_wait.set()
    assert entered_confirmation.wait(3)
    try:
        assert ticket.status().pending_capability is session.pending
        assert arbiter.active_ticket is ticket
        with pytest.raises(ArbiterBusy):
            arbiter.submit(route, lambda port: Settlement('unsafe'))
    finally:
        release_confirmation.set()
    with pytest.raises(arbiter_module.TargetTerminated):
        ticket.wait_settled(3)
    attempt = ticket.stop_teardown
    assert attempt is not None
    assert isinstance(attempt.wait(3), ServerTerminationConfirmed)
    assert [name for name, _ in session.calls] == ['eval', 'wait', 'terminate', 'confirm']
    assert len({thread for _, thread in session.calls}) == 1
    arbiter.close(timeout=3)


def test_stop_does_not_terminate_server_after_matching_main_stop_wins_race():
    from onec_runtime.rdbg.models import DebugTarget, ModuleLocation, StopEvent

    target = TargetId(uuid4(), 'DefAlias', uuid4())
    stop = StopEvent(
        target, ModuleLocation('ExtensionModule', '', uuid4(), uuid4(), 1, 'Test'),
        'breakpoint',
    )
    entered_wait = Event()
    release_wait = Event()

    class ServerSession:
        def __init__(self):
            self.target = DebugTarget(target, 'Server', 'running')
            self.terminations = 0

        def continue_(self, *, on_transport_dispatch):
            on_transport_dispatch()

        def wait_for_any_stop(self, *, timeout_s, expected_target, on_transport_dispatch):
            on_transport_dispatch()
            entered_wait.set()
            assert release_wait.wait(3)
            return stop

        def terminate_bound_server_session(self):
            self.terminations += 1
            return True

    session = ServerSession()
    route = RouteToken('main-result-wins-stop', 1, 0, 'main')
    arbiter = RdbgArbiter(session, route)

    def main(port):
        port.continue_()
        return Settlement(port.wait_for_any_stop(timeout_s=0.01))

    ticket = arbiter.submit(route, main)
    arbiter.dispatch(ticket)
    assert entered_wait.wait(3)
    assert arbiter.request_stop(ticket).name == 'REQUESTED'
    release_wait.set()
    assert ticket.wait_settled(3) is stop
    assert ticket.stop_teardown is None
    assert session.terminations == 0
    arbiter.close(timeout=3)


@pytest.mark.parametrize('selected_kind', ['foreign_target', 'file_mode'])
def test_auto_stop_requires_exact_owned_server_target(selected_kind):
    from onec_runtime.rdbg.models import DebugTarget

    from onec_runtime.execution.termination import ServerTerminationConfirmed

    session, route, arbiter, ticket, target, evidence = _unknown_server_stop(
        absence_results=[None],
    )
    session.target = (
        DebugTarget(TargetId(uuid4(), 'DefAlias', target.seance_id), 'Server', 'stopped')
        if selected_kind == 'foreign_target'
        else DebugTarget(target, 'ServerEmulation', 'stopped')
    )

    assert arbiter.request_stop(ticket).name == 'REQUESTED'
    assert ticket.stop_teardown is None
    assert ticket.wait_unknown(0)
    assert arbiter.active_ticket is ticket
    assert [call[0] for call in session.calls] == ['continue']
    with pytest.raises(ValueError, match='exact selected server target'):
        arbiter.teardown_fenced_server_target(ticket, route, grace_s=0.01)
    with pytest.raises(ArbiterBusy):
        arbiter.close(timeout=0)
    session.target = DebugTarget(target, 'Server', 'stopped')
    arbiter.retire_terminated_target(
        ticket, route, ServerTerminationConfirmed(target, evidence),
    )
    arbiter.close(timeout=3)


def test_server_stop_teardown_uses_same_worker_and_exact_proof():
    session, route, arbiter, ticket, target, evidence = _unknown_server_stop(
        absence_results=[None],
    )
    queued = arbiter.submit(route, lambda port: Settlement('must not run'))
    arbiter.dispatch(queued)
    with pytest.raises(ArbiterBusy, match='fenced unknown'):
        arbiter.teardown_fenced_server_target(ticket, route, grace_s=0.1)
    assert arbiter.request_stop(ticket).name == 'REQUESTED'
    with pytest.raises(CancelledBeforeEffect):
        queued.wait_settled(3)

    attempt = ticket.stop_teardown
    assert attempt is not None
    result = attempt.wait(3)

    from onec_runtime.execution.termination import ServerTerminationConfirmed
    assert isinstance(result, ServerTerminationConfirmed)
    assert result.expected_target == target and result.absence is evidence
    with pytest.raises(arbiter_module.TargetTerminated) as raised:
        ticket.wait_settled(3)
    assert raised.value.evidence is result
    assert [call[0] for call in session.calls] == ['continue', 'terminate', 'confirm']
    assert len({call[1] for call in session.calls}) == 1
    with pytest.raises(RuntimeError, match='closed'):
        arbiter.submit(route, lambda port: Settlement(None))
    arbiter.close(timeout=3)


def test_server_stop_after_main_to_capture_handoff_uses_current_route():
    from onec_runtime.execution.termination import ServerTerminationConfirmed

    session, capture_route, arbiter, ticket, target, evidence = _unknown_server_stop(
        absence_results=[None], handoff_to_capture=True,
    )
    assert arbiter.current_route == capture_route
    assert arbiter.request_stop(ticket).name == 'REQUESTED'
    attempt = ticket.stop_teardown
    assert attempt is not None

    result = attempt.wait(3)
    assert isinstance(result, ServerTerminationConfirmed)
    assert result.expected_target == target and result.absence is evidence
    with pytest.raises(arbiter_module.TargetTerminated):
        ticket.wait_settled(3)
    arbiter.close(timeout=3)


def test_server_stop_rejects_foreign_absence_without_repeating_termination():
    from onec_runtime.execution.termination import TerminationUnknown, ServerTerminationConfirmed
    from onec_runtime.rdbg.session import BoundServerTargetAbsence

    wrong_target = TargetId(uuid4(), 'DefAlias', uuid4())
    wrong_client = TargetId(uuid4(), 'DefAlias', wrong_target.seance_id)
    foreign = BoundServerTargetAbsence(wrong_client, wrong_target, 1.0, 1)
    session, route, arbiter, ticket, target, evidence = _unknown_server_stop(
        absence_results=[foreign, None],
    )
    assert arbiter.request_stop(ticket).name == 'REQUESTED'
    first = ticket.stop_teardown
    assert first is not None
    result = first.wait(3)
    assert isinstance(result, TerminationUnknown)
    assert result.error_type == 'EvidenceMismatch'
    assert result.expected_target == target
    assert ticket.wait_unknown(0)
    assert arbiter.active_ticket is ticket

    second = arbiter.teardown_fenced_server_target(ticket, route, grace_s=0.01)
    assert ticket.stop_teardown is second
    confirmed = second.wait(3)
    assert isinstance(confirmed, ServerTerminationConfirmed)
    assert confirmed.absence is evidence
    assert [call[0] for call in session.calls] == ['continue', 'terminate', 'confirm', 'confirm']
    with pytest.raises(arbiter_module.TargetTerminated):
        ticket.wait_settled(3)
    arbiter.close(timeout=3)


def test_server_stop_unknown_keeps_owner_and_retries_only_absence_probe():
    from onec_runtime.execution.termination import TerminationUnknown, ServerTerminationConfirmed

    session, route, arbiter, ticket, target, evidence = _unknown_server_stop(
        absence_results=[CommandTimeout('registry interval'), None],
    )
    assert arbiter.request_stop(ticket).name == 'REQUESTED'
    first = ticket.stop_teardown
    assert first is not None
    unknown = first.wait(3)
    assert isinstance(unknown, TerminationUnknown)
    assert unknown.expected_target == target
    assert ticket.wait_unknown(0)
    assert arbiter.active_ticket is ticket
    with pytest.raises(ArbiterBusy):
        arbiter.close(timeout=0)
    with pytest.raises(ArbiterBusy):
        arbiter.submit(route, lambda port: Settlement('unsafe'))

    second = arbiter.teardown_fenced_server_target(ticket, route, grace_s=0.01)
    confirmed = second.wait(3)
    assert isinstance(confirmed, ServerTerminationConfirmed)
    assert confirmed.absence is evidence
    assert [call[0] for call in session.calls] == ['continue', 'terminate', 'confirm', 'confirm']
    with pytest.raises(arbiter_module.TargetTerminated):
        ticket.wait_settled(3)
    arbiter.close(timeout=3)


def test_server_stop_teardown_waiter_timeout_retains_worker_and_route_fence():
    from onec_runtime.execution.termination import ServerTerminationConfirmed

    release_confirmation = Event()
    session, route, arbiter, ticket, target, evidence = _unknown_server_stop(
        absence_results=[None], block_confirmation=release_confirmation,
    )
    assert arbiter.request_stop(ticket).name == 'REQUESTED'
    stale = RouteToken(route.incarnation, route.epoch + 1, 0, route.context_id)
    with pytest.raises(StaleRoute):
        arbiter.teardown_fenced_server_target(ticket, stale, grace_s=0.01)
    attempt = ticket.stop_teardown
    assert attempt is not None
    assert session.confirmation_entered.wait(3)
    try:
        with pytest.raises(TimeoutError):
            attempt.wait(0.01)
        with pytest.raises(ArbiterBusy):
            arbiter.teardown_fenced_server_target(ticket, route, grace_s=0.01)
        assert arbiter.active_ticket is ticket
        assert ticket.wait_unknown(0)
    finally:
        release_confirmation.set()
    assert isinstance(attempt.wait(3), ServerTerminationConfirmed)
    with pytest.raises(arbiter_module.TargetTerminated):
        ticket.wait_settled(3)
    arbiter.close(timeout=3)


def test_real_rdbg_server_teardown_and_absence_share_arbiter_worker():
    from onec_runtime.execution.termination import ServerTerminationConfirmed
    from onec_runtime.rdbg.models import DebugTarget, ModuleLocation
    from onec_runtime.rdbg.session import RdbgSession, SessionState
    from onec_runtime.rdbg.xml_codec import BASE_NS, RDBG_NS

    seance_id = uuid4()
    target = TargetId(uuid4(), 'DefAlias', seance_id)
    client = TargetId(uuid4(), 'DefAlias', seance_id)

    def registry(*subjects):
        items = ''.join(
            f'''<item><targetIDStr>target</targetIDStr><targetID xmlns="{BASE_NS}">
              <id>{identity.id}</id><infoBaseAlias>DefAlias</infoBaseAlias>
              <seanceId>{seance_id}</seanceId><targetType>{kind}</targetType>
              </targetID><stateNum>1</stateNum><state>stopped</state></item>'''
            for identity, kind in subjects
        )
        return f'<response xmlns="{RDBG_NS}"><result>success</result>{items}</response>'.encode()

    class Transport:
        def __init__(self):
            self.calls = []
            self.registries = [
                registry((target, 'Server'), (client, 'ManagedClient')),
                registry((client, 'ManagedClient')),
                registry(),
            ]

        def request(self, command, payload=b'', **kwargs):
            self.calls.append((command, get_ident()))
            if command == 'getDbgAllTargetStates':
                return self.registries.pop(0)
            return b''

    transport = Transport()
    location = ModuleLocation('ExtensionModule', '', uuid4(), uuid4(), 1, 'Test')
    session = RdbgSession(transport, location, server_target_type='Server')
    session.state = SessionState.READY
    session.target = DebugTarget(target, 'Server', 'stopped')
    session.attached_targets[target.id] = session.target
    session._bound_client_target = client
    route = RouteToken('real-server-stop', 1, 0, 'main')
    arbiter = RdbgArbiter(session, route)

    def unknown_main(port):
        port.continue_()
        raise OutcomeUnknown('waiting for stop')

    ticket = arbiter.submit(route, unknown_main)
    arbiter.dispatch(ticket)
    assert ticket.wait_unknown(3)
    assert arbiter.request_stop(ticket).name == 'REQUESTED'
    attempt = ticket.stop_teardown
    assert attempt is not None
    result = attempt.wait(3)

    assert isinstance(result, ServerTerminationConfirmed)
    assert result.expected_target == target
    assert [command for command, _ in transport.calls] == [
        'step', 'getDbgAllTargetStates', 'terminateDbgTarget',
        'getDbgAllTargetStates', 'terminateDbgTarget', 'getDbgAllTargetStates',
    ]
    assert len({thread for _, thread in transport.calls}) == 1
    with pytest.raises(arbiter_module.TargetTerminated):
        ticket.wait_settled(3)
    arbiter.close(timeout=3)
