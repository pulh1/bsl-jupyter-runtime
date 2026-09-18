"""An interrupted initiating wait requests Stop; a local timeout does not."""

from contextlib import contextmanager, nullcontext
from threading import Event
from types import SimpleNamespace

import pytest

from onec_runtime.execution.arbiter import (
    CancelledBeforeEffect, RdbgArbiter, RouteToken, Settlement,
)
from onec_runtime.execution.local_wait import wait_initiator_locally
from onec_runtime.execution.public_facade import PublicExecutionFacade
from onec_runtime.execution.capture.value_projection import CaptureTicketValueProjection
from onec_runtime.execution.main.idle_materialization import (
    MainIdleMaterializationService, MainIdleTargetFence,
)
from onec_runtime.execution.worker_module_lifecycle import WorkerModuleLifecycleService

from test_main_idle_materialization import Session, TARGET
from test_capture_materialization_executor import transfer_plan
from onec_runtime.execution.worker_activation import WorkerMaterializationSnapshot


@contextmanager
def _interrupted_wait():
    raise KeyboardInterrupt
    yield


def _active_ticket():
    route = RouteToken("runtime", 1, 0, "main")
    arbiter = RdbgArbiter(Session([]), route)
    entered = Event()
    release = Event()

    def plan(_port):
        entered.set()
        assert release.wait(3)
        return Settlement(None)

    ticket = arbiter.submit(route, plan)
    arbiter.dispatch(ticket)
    assert entered.wait(1)
    return arbiter, ticket, release


def test_local_direct_ticket_interrupt_requests_stop_without_losing_owner() -> None:
    arbiter, ticket, release = _active_ticket()
    try:
        with pytest.raises(KeyboardInterrupt):
            wait_initiator_locally(
                ticket, timeout_s=None, wait_handoff=_interrupted_wait,
                request_stop=lambda: arbiter.request_stop(ticket),
            )
        assert ticket.status().stop_requested is True
        assert ticket.status().waiter_detached is True
    finally:
        release.set()
        ticket.wait_settled(timeout=3)
        arbiter.close(timeout=3)


def test_facade_resume_wait_interrupt_requests_stop_for_active_ticket() -> None:
    arbiter, ticket, release = _active_ticket()
    try:
        controller = SimpleNamespace(request_stop=arbiter.request_stop)
        facade = PublicExecutionFacade(
            object(), controller, arbiter,
            source_unit_factory=lambda _source: None,
            status_reader=lambda: None,
        )
        facade._wait_handoff = _interrupted_wait

        with pytest.raises(KeyboardInterrupt):
            facade._wait_for_reply(ticket, timeout_s=None)
        assert ticket.status().stop_requested is True
        assert ticket.status().waiter_detached is True
    finally:
        release.set()
        ticket.wait_settled(timeout=3)
        arbiter.close(timeout=3)


def test_local_direct_ticket_timeout_only_detaches_waiter() -> None:
    arbiter, ticket, release = _active_ticket()
    try:
        with pytest.raises(TimeoutError):
            wait_initiator_locally(
                ticket, timeout_s=0.01, wait_handoff=nullcontext,
                request_stop=lambda: arbiter.request_stop(ticket),
            )
        assert ticket.status().stop_requested is False
        assert ticket.status().waiter_detached is True
    finally:
        release.set()
        ticket.wait_settled(timeout=3)
        arbiter.close(timeout=3)


def test_capture_value_projection_interrupt_requests_stop() -> None:
    arbiter, ticket, release = _active_ticket()
    try:
        projection = CaptureTicketValueProjection.__new__(CaptureTicketValueProjection)
        projection._controller = SimpleNamespace(request_stop=arbiter.request_stop)
        projection._wait_handoff = _interrupted_wait
        with pytest.raises(KeyboardInterrupt):
            projection._wait(ticket)
        assert ticket.status().stop_requested is True
        assert ticket.status().waiter_detached is True
    finally:
        release.set()
        ticket.wait_settled(timeout=3)
        arbiter.close(timeout=3)


def test_main_cleanup_retry_interrupt_requests_stop() -> None:
    arbiter, ticket, release = _active_ticket()
    stop_calls = []

    def request_stop(selected):
        stop_calls.append(selected)
        return arbiter.request_stop(selected)

    try:
        service = MainIdleMaterializationService(
            arbiter,
            main_idle_fence=lambda: MainIdleTargetFence(arbiter.current_route, TARGET),
            runtime_generation=1,
            context_generation=1,
            wait_handoff=_interrupted_wait,
            request_stop=request_stop,
        )
        parent = object()
        service._cleanup_debts["key"] = parent
        arbiter.retry_post_settlement_cleanup = lambda selected: ticket if selected is parent else None
        with pytest.raises(KeyboardInterrupt):
            service.retry_cleanup("key")
        assert ticket.status().stop_requested is True
        assert ticket.status().waiter_detached is True
        assert stop_calls == [ticket]
    finally:
        release.set()
        ticket.wait_settled(timeout=3)
        arbiter.close(timeout=3)


def test_worker_module_lifecycle_interrupt_requests_stop() -> None:
    route = RouteToken("runtime", 1, 0, "main")
    arbiter = RdbgArbiter(Session([]), route)
    entered = Event()
    release = Event()
    tickets = []
    stop_calls = []
    submit = arbiter.submit

    def record_submit(token, plan, **kwargs):
        ticket = submit(token, plan, **kwargs)
        tickets.append(ticket)
        return ticket

    @contextmanager
    def interrupted_wait():
        assert entered.wait(3)
        raise KeyboardInterrupt
        yield

    def plan(_port):
        entered.set()
        assert release.wait(3)
        return Settlement(None)

    try:
        service = WorkerModuleLifecycleService.__new__(WorkerModuleLifecycleService)
        service._arbiter = arbiter
        service._wait_handoff = interrupted_wait
        def request_stop(ticket):
            stop_calls.append(ticket)
            return arbiter.request_stop(ticket)
        service._request_stop = request_stop
        arbiter.submit = record_submit
        with pytest.raises(KeyboardInterrupt):
            service._submit_and_wait(route, plan)
        assert len(tickets) == 1
        assert tickets[0].status().stop_requested is True
        assert tickets[0].status().waiter_detached is True
        assert stop_calls == tickets
    finally:
        release.set()
        if tickets:
            tickets[0].wait_settled(timeout=3)
        arbiter.close(timeout=3)


def test_main_idle_submit_interrupt_rolls_back_receipted_ticket() -> None:
    route = RouteToken("runtime", 1, 0, "main")
    session = Session([])
    arbiter = RdbgArbiter(session, route)
    submitted = []
    submit = arbiter.submit

    def interrupt_after_adoption(*args, **kwargs):
        ticket = submit(*args, **kwargs)
        submitted.append(ticket)
        raise KeyboardInterrupt

    arbiter.submit = interrupt_after_adoption
    service = MainIdleMaterializationService(
        arbiter,
        main_idle_fence=lambda: MainIdleTargetFence(route, TARGET),
        runtime_generation=1,
        context_generation=1,
    )
    try:
        with pytest.raises(KeyboardInterrupt):
            service.transfer_private_plan(
                transfer_plan("__onec_value_" + "a" * 32),
                catalog=WorkerMaterializationSnapshot(0, ()),
            )
        assert len(submitted) == 1
        assert submitted[0].status().settled is True
        with pytest.raises(CancelledBeforeEffect):
            submitted[0].wait_settled(0)
        assert session.calls == []
    finally:
        for ticket in submitted:
            ticket.cancel_queued()
        arbiter.close(timeout=3)


def test_worker_module_submit_interrupt_rolls_back_receipted_ticket() -> None:
    route = RouteToken("runtime", 1, 0, "main")
    arbiter = RdbgArbiter(Session([]), route)
    submitted = []
    submit = arbiter.submit

    def interrupt_after_adoption(*args, **kwargs):
        ticket = submit(*args, **kwargs)
        submitted.append(ticket)
        raise KeyboardInterrupt

    arbiter.submit = interrupt_after_adoption
    service = WorkerModuleLifecycleService.__new__(WorkerModuleLifecycleService)
    service._arbiter = arbiter
    try:
        with pytest.raises(KeyboardInterrupt):
            service._submit_and_wait(route, lambda _port: Settlement(None))
        assert len(submitted) == 1
        assert submitted[0].status().settled is True
        with pytest.raises(CancelledBeforeEffect):
            submitted[0].wait_settled(0)
    finally:
        for ticket in submitted:
            ticket.cancel_queued()
        arbiter.close(timeout=3)
