"""Single-worker RDBG ownership primitive; not yet wired to the runtime.

Submission is deliberately two phase: publish the ticket, then dispatch it.
Plans own protocol sequencing, polling and mandatory cleanup. They must return
Settlement only after remote ownership can safely be released. Neither a local
wait timeout nor an exception after session entry establishes that fact.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from threading import Condition, Thread, get_ident
from typing import Any, Callable, Protocol
from uuid import uuid4

from onec_runtime.errors import EvaluationDispatchUnknown
from onec_runtime.rdbg.models import EvaluationResult, PendingEvaluation, StopEvent


class EvaluationSession(Protocol):
    """Session owns protocol validation, correlation and capability retirement."""

    def start_evaluation(self, expression: str, *, max_text_size: int,
                         stack_level: int, timeout_s: float,
                         on_transport_dispatch: Callable[[], None]) -> PendingEvaluation: ...

    def start_collection_evaluation(self, expression: str, *, start_index: int,
                                    page_size: int, max_text_size: int,
                                    stack_level: int, timeout_s: float,
                                    on_transport_dispatch: Callable[[], None]) -> PendingEvaluation: ...

    def wait_evaluation_event(self, pending: PendingEvaluation, *, timeout_s: float
                              ) -> EvaluationResult | StopEvent: ...

    def continue_evaluation(self, pending: PendingEvaluation, stop: StopEvent) -> None: ...



@dataclass(frozen=True)
class RouteToken:
    incarnation: str
    epoch: int
    revision: int
    context_id: str


@dataclass(frozen=True)
class Settlement:
    """A request outcome, never a statement that its parent MAIN is terminal."""

    value: Any
    next_route: RouteToken | None = None


class OutcomeUnknown(RuntimeError):
    """Remote effects may still be live; retain ownership and pending evidence."""


class StaleRoute(RuntimeError):
    pass


class CancelledBeforeEffect(RuntimeError):
    pass


class WaiterDetached(RuntimeError):
    pass


class ArbiterBusy(RuntimeError):
    pass


@dataclass(frozen=True)
class TicketStatus:
    phase: str
    settled: bool
    waiter_detached: bool
    pending_capability: PendingEvaluation | None


class ExecutionTicket:
    def __init__(self, owner: RdbgArbiter, route: RouteToken, plan: Plan):
        self.id = uuid4().hex
        self._owner = owner
        self._route = route
        self._plan = plan
        self._phase = 'queued'
        self._ready = False
        self._detached = False
        self._pending: PendingEvaluation | None = None
        self._entered = False
        self._value: Any = None
        self._error: BaseException | None = None

    def status(self) -> TicketStatus:
        with self._owner._mailbox:
            return TicketStatus(self._phase, self._phase == 'settled', self._detached, self._pending)

    def detach_waiter(self) -> None:
        with self._owner._mailbox:
            self._detached = True
            self._owner._mailbox.notify_all()

    def cancel_queued(self) -> bool:
        """Only pre-effect cancellation; remote Stop needs an executor stop plan."""
        with self._owner._mailbox:
            if self._phase != 'queued':
                return False
            self._owner._queue.remove(self)
            self._owner._settle(self, error=CancelledBeforeEffect())
            return True

    def wait(self, timeout: float | None = None) -> Any:
        return self._wait(timeout, respect_detach=True)

    def wait_settled(self, timeout: float | None = None) -> Any:
        """Observer wait, independent of the detached initiating waiter."""
        return self._wait(timeout, respect_detach=False)

    def _wait(self, timeout: float | None, *, respect_detach: bool) -> Any:
        with self._owner._mailbox:
            if not self._owner._mailbox.wait_for(
                lambda: self._phase == 'settled' or (respect_detach and self._detached), timeout
            ):
                raise TimeoutError('Local waiter interval elapsed; operation remains owned')
            if respect_detach and self._detached:
                raise WaiterDetached()
            if self._error is not None:
                raise self._error
            return self._value

    def wait_unknown(self, timeout: float | None = None) -> bool:
        with self._owner._mailbox:
            self._owner._mailbox.wait_for(lambda: self._phase in ('unknown', 'settled'), timeout)
            return self._phase == 'unknown'


class SessionPort:
    """Worker-confined, capability-preserving eval operations only.

    Synchronous eval wrappers and arbitrary session methods are intentionally
    absent: they hide pending ownership. Additional operations need explicit
    evidence contracts before this port can safely expose them.
    """

    def __init__(self, owner: RdbgArbiter, ticket: ExecutionTicket):
        self._owner = owner
        self._ticket = ticket
        self._live = True

    def _check(self) -> None:
        if not self._live or get_ident() != self._owner._worker.ident:
            raise RuntimeError('Session port is confined to its active worker plan')

    def _transport_entered(self) -> None:
        self._check()
        with self._owner._mailbox:
            self._ticket._entered = True

    def _start(self, operation: Callable[..., PendingEvaluation], expression: str,
               **options: Any) -> PendingEvaluation:
        self._check()
        if self._ticket._pending is not None:
            raise ArbiterBusy('An evaluation capability is already owned')
        try:
            pending = operation(expression, on_transport_dispatch=self._transport_entered, **options)
        except EvaluationDispatchUnknown as error:
            with self._owner._mailbox:
                self._ticket._pending = error.pending
            raise
        with self._owner._mailbox:
            self._ticket._pending = pending
        return pending

    def start_evaluation(self, expression: str, *, max_text_size: int = 307_200,
                         stack_level: int = 0, timeout_s: float = 30.0) -> PendingEvaluation:
        return self._start(self._owner._session.start_evaluation, expression,
                           max_text_size=max_text_size, stack_level=stack_level, timeout_s=timeout_s)

    def start_collection_evaluation(self, expression: str, *, start_index: int,
                                    page_size: int = 2400, max_text_size: int = 4096,
                                    stack_level: int = 0, timeout_s: float = 30.0) -> PendingEvaluation:
        return self._start(self._owner._session.start_collection_evaluation, expression,
                           start_index=start_index, page_size=page_size, max_text_size=max_text_size,
                           stack_level=stack_level, timeout_s=timeout_s)

    def _require_pending(self, pending: PendingEvaluation) -> None:
        self._check()
        if pending is not self._ticket._pending or type(pending) is not PendingEvaluation:
            raise ValueError('The exact owned pending capability is required')

    def wait_evaluation_event(self, pending: PendingEvaluation, *, timeout_s: float
                              ) -> EvaluationResult | StopEvent:
        self._require_pending(pending)
        event = self._owner._session.wait_evaluation_event(pending, timeout_s=timeout_s)
        if isinstance(event, EvaluationResult):
            if event.result_id != pending.result_id:
                raise OutcomeUnknown('Session returned a mismatched evaluation result')
            # The session contract retires this exact capability on matched result.
            with self._owner._mailbox:
                self._ticket._pending = None
                self._ticket._entered = False
        elif not isinstance(event, StopEvent):
            raise OutcomeUnknown('Session returned an unrecognized evaluation event')
        return event

    def continue_evaluation(self, pending: PendingEvaluation, stop: StopEvent) -> None:
        self._require_pending(pending)
        self._owner._session.continue_evaluation(pending, stop)


Plan = Callable[[SessionPort], Settlement]


class RdbgArbiter:
    """Own one session after bootstrap, with one mailbox and one worker.

    This first port exposes only capability-preserving evaluation operations.
    No background polling is started: active plans read events on this worker.
    Unknown plans keep the slot until a reconciliation/teardown plan supplies a
    confirmed settlement. This primitive cannot itself infer protocol evidence
    or terminate remote execution; those are executor responsibilities.
    """

    def __init__(self, session: EvaluationSession, route: RouteToken):
        self._session = session
        self._route = route
        self._mailbox = Condition()
        self._queue: deque[ExecutionTicket] = deque()
        self._active: ExecutionTicket | None = None
        self._reconciliation: Plan | None = None
        self._closed = False
        self._worker = Thread(target=self._run, name='rdbg-arbiter', daemon=True)
        self._worker.start()

    @property
    def current_route(self) -> RouteToken:
        with self._mailbox:
            return self._route

    @property
    def active_ticket(self) -> ExecutionTicket | None:
        with self._mailbox:
            return self._active

    def submit(self, route: RouteToken, plan: Plan) -> ExecutionTicket:
        with self._mailbox:
            if self._closed:
                raise RuntimeError('Arbiter is closed')
            if route != self._route:
                raise StaleRoute()
            ticket = ExecutionTicket(self, route, plan)
            self._queue.append(ticket)
            return ticket

    def dispatch(self, ticket: ExecutionTicket) -> None:
        """Called only after the submitting layer has published its ticket."""
        with self._mailbox:
            self._check_ticket(ticket)
            if ticket._phase != 'queued':
                raise RuntimeError('Ticket is no longer queued')
            ticket._ready = True
            self._mailbox.notify_all()

    def reconcile(self, ticket: ExecutionTicket, plan: Plan) -> None:
        """Schedule evidence collection or confirmed teardown, never blind retry."""
        with self._mailbox:
            self._check_ticket(ticket)
            if self._active is not ticket or ticket._phase != 'unknown' or self._reconciliation is not None:
                raise ArbiterBusy('Reconciliation requires the unknown owner')
            self._reconciliation = plan
            self._mailbox.notify_all()

    def close(self, timeout: float | None = None) -> None:
        """Close local ownership only when no remote operation may remain live."""
        with self._mailbox:
            if self._active is not None:
                raise ArbiterBusy('Reconcile or confirm target teardown before closing')
            self._closed = True
            while self._queue:
                self._settle(self._queue.popleft(), error=CancelledBeforeEffect())
            self._mailbox.notify_all()
        self._worker.join(timeout)
        if self._worker.is_alive():
            raise TimeoutError('Arbiter worker did not exit')

    def _check_ticket(self, ticket: ExecutionTicket) -> None:
        if ticket._owner is not self:
            raise ValueError('Ticket belongs to a different arbiter')

    def _settle(self, ticket: ExecutionTicket, value: Any = None, error: BaseException | None = None) -> None:
        # Called only under mailbox; no callbacks or transport work here.
        ticket._value = value
        ticket._error = error
        assert ticket._pending is None, 'Cannot settle an owned evaluation capability'
        ticket._phase = 'settled'
        self._mailbox.notify_all()

    def _run(self) -> None:
        while True:
            with self._mailbox:
                self._mailbox.wait_for(lambda: self._closed or self._reconciliation is not None or (
                    self._active is None and bool(self._queue) and self._queue[0]._ready
                ))
                if self._closed:
                    return
                reconciling = self._reconciliation is not None
                if reconciling:
                    ticket = self._active
                    assert ticket is not None
                    plan = self._reconciliation
                    self._reconciliation = None
                else:
                    ticket = self._queue.popleft()
                    if ticket._route != self._route:
                        self._settle(ticket, error=StaleRoute())
                        continue
                    self._active = ticket
                    plan = ticket._plan
                ticket._phase = 'running'
            port = SessionPort(self, ticket)
            try:
                outcome = plan(port)
                if not isinstance(outcome, Settlement):
                    raise TypeError('Plan must return a confirmed Settlement')
                if ticket._pending is not None:
                    raise OutcomeUnknown('Settlement cannot discard an unretired capability')
            except BaseException as error:
                with self._mailbox:
                    if reconciling or ticket._entered or ticket._pending is not None or isinstance(error, OutcomeUnknown):
                        ticket._phase = 'unknown'
                        ticket._error = error
                        self._mailbox.notify_all()
                    else:
                        self._settle(ticket, error=error)
                        self._active = None
            else:
                with self._mailbox:
                    if outcome.next_route is not None:
                        self._route = outcome.next_route
                    self._settle(ticket, value=outcome.value)
                    self._active = None
            finally:
                port._live = False
