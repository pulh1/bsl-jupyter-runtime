"""Single-worker RDBG ownership primitive; not yet wired to the runtime.

Submission is deliberately two phase: publish the ticket, then dispatch it.
Plans own protocol sequencing, polling and mandatory cleanup. Raw plans return
Settlement; policy-bound plans return ReadyForPolicy only after cleanup. Neither
a local wait timeout nor an exception after session entry establishes release.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum, auto
from math import isfinite
from threading import Condition, Thread, get_ident
from typing import Any, Callable, Protocol
from uuid import uuid4

from onec_runtime.errors import CommandTimeout, EvaluationDispatchUnknown, StopWaitIntervalElapsed
from onec_runtime.execution.termination import (
    FileTerminationConfirmed, ServerTerminationConfirmed, TerminationUnknown,
    terminate_server_target,
)
from onec_runtime.rdbg.models import DebugTarget, EvaluationResult, LocalVariablesResult, ModuleLocation, ModifyResult, PendingEvaluation, StopEvent, TargetId
from onec_runtime.rdbg.session import BoundServerTargetAbsence


class EvaluationSession(Protocol):
    """Session owns protocol validation, correlation and capability retirement."""

    target: DebugTarget | None

    def set_breakpoints(self, locations: tuple[ModuleLocation, ...], *,
                        on_transport_dispatch: Callable[[], None]) -> None: ...

    def local_variables(self, stack_level: int = 0, *, timeout_s: float,
                        on_transport_dispatch: Callable[[], None]) -> LocalVariablesResult: ...

    def heartbeat(self, *, on_transport_dispatch: Callable[[], None]) -> dict[str, object]: ...

    def modify(self, variable: str, value_expression: str, *,
               on_transport_dispatch: Callable[[], None]) -> ModifyResult: ...

    def continue_(self, *, on_transport_dispatch: Callable[[], None]) -> None: ...

    def wait_for_any_stop(self, *, timeout_s: float, expected_target: TargetId,
                          on_transport_dispatch: Callable[[], None]) -> StopEvent: ...

    def start_evaluation(self, expression: str, *, max_text_size: int,
                         stack_level: int, timeout_s: float,
                         on_transport_dispatch: Callable[[], None]) -> PendingEvaluation: ...

    def start_collection_evaluation(self, expression: str, *, start_index: int,
                                    page_size: int, max_text_size: int,
                                    stack_level: int, timeout_s: float,
                                    on_transport_dispatch: Callable[[], None]) -> PendingEvaluation: ...

    def wait_evaluation_event(self, pending: PendingEvaluation, *, timeout_s: float,
                              on_transport_dispatch: Callable[[], None]
                              ) -> EvaluationResult | StopEvent: ...

    def continue_evaluation(self, pending: PendingEvaluation, stop: StopEvent, *,
                            on_transport_dispatch: Callable[[], None]) -> None: ...

    def terminate_bound_server_session(self) -> bool: ...

    def wait_for_bound_server_targets_absent(
        self, expected_target: TargetId, *, timeout_s: float,
    ) -> BoundServerTargetAbsence: ...



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


@dataclass(frozen=True)
class ReadyForPolicy:
    """Confirmed raw result after all mandatory remote cleanup has completed.

    Only a ticket with a finalizer may return this from its initial or
    reconciliation plan. Evidence-only reconciliation must leave that ticket
    unknown until a later plan can supply the raw outcome safely.
    """

    raw_outcome: Any
    next_route: RouteToken | None = None


@dataclass(frozen=True)
class ConfirmedFailure:
    """Terminal cell error after all mandatory remote work has completed."""

    error: BaseException


class OutcomeUnknown(RuntimeError):
    """Remote effects may still be live; retain ownership and pending evidence."""


class StopPendingTeardown(OutcomeUnknown):
    """Stop blocked a later effect after earlier remote work had started."""


class TargetTerminated(RuntimeError):
    """The exact old target was confirmed absent; its ticket cannot resume."""

    def __init__(self, evidence: FileTerminationConfirmed | ServerTerminationConfirmed):
        self.evidence = evidence
        super().__init__('Target termination confirmed')


class StaleRoute(RuntimeError):
    pass


class CancelledBeforeEffect(RuntimeError):
    pass


class WaiterDetached(RuntimeError):
    pass


class ArbiterBusy(RuntimeError):
    pass


class StopRequestOutcome(Enum):
    """Admission result, not evidence that remote execution has stopped."""

    CANCELLED_BEFORE_EFFECT = auto()
    REQUESTED = auto()
    ALREADY_SETTLED = auto()


@dataclass(frozen=True)
class TicketStatus:
    phase: str
    settled: bool
    waiter_detached: bool
    pending_capability: PendingEvaluation | None
    awaiting_stop: bool
    stop_requested: bool


class ExecutionTicket:
    def __init__(self, owner: RdbgArbiter, route: RouteToken, plan: Plan,
                 finalizer: Callable[[Any], Any] | None = None):
        self.id = uuid4().hex
        self._owner = owner
        self._route = route
        self._plan = plan
        self._finalizer = finalizer
        self._finalizer_started = False
        self._phase = 'queued'
        self._ready = False
        self._detached = False
        self._pending: PendingEvaluation | None = None
        self._pending_stop: StopEvent | None = None
        self._entered = False
        self._ever_entered = False
        self._stop_requested = False
        self._stop_blocked_after_effect = False
        self._server_termination_attempted = False
        self._stop_teardown: ServerTeardownAttempt | None = None
        self._stop_target: TargetId | None = None
        self._effect_target: TargetId | None = None
        self._value: Any = None
        self._error: BaseException | None = None
        self._post_settlement_cleanup_plan: Plan | None = None
        self._post_settlement_cleanup: ExecutionTicket | None = None
        self._post_settlement_parent: ExecutionTicket | None = None

    @property
    def post_settlement_cleanup(self) -> ExecutionTicket | None:
        """The cleanup ticket queued after this ticket's confirmed settlement."""

        with self._owner._mailbox:
            return self._post_settlement_cleanup

    @property
    def stop_teardown(self) -> ServerTeardownAttempt | None:
        """Worker-owned Stop proof, if exact server-target evidence allowed it."""

        with self._owner._mailbox:
            return self._stop_teardown

    def status(self) -> TicketStatus:
        with self._owner._mailbox:
            return TicketStatus(
                self._phase, self._phase == 'settled', self._detached, self._pending,
                self._stop_target is not None, self._stop_requested,
            )

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

    def wait_initiator(self, timeout: float | None = None) -> Any:
        """Wait for the owning notebook caller; other observers use wait_settled."""

        return self.wait(timeout)

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
    """Worker-confined operations preserving eval and Continue ownership.

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

    def handoff_route(self, next_route: RouteToken) -> None:
        """Change route inside the active plan before its next RDBG operation.

        The executor must first establish the stop or Continue evidence for the
        transition. A handoff does not retire this ticket or any remote capability.
        """
        self._check()
        with self._owner._mailbox:
            if self._owner._active is not self._ticket or self._ticket._phase != 'running':
                raise RuntimeError('Route handoff requires the active worker plan')
            current = self._owner._route
            if next_route.incarnation != current.incarnation or next_route.epoch <= current.epoch:
                raise StaleRoute('Route handoff requires a newer epoch of this incarnation')
            self._owner._route = next_route
            self._owner._mailbox.notify_all()

    def register_post_settlement_cleanup(self, plan: Plan) -> None:
        """Queue one cleanup after this confirmed ticket settles.

        The cleanup gets a fresh port, so ambiguous cleanup transport belongs
        to its own ticket rather than replacing the user-visible result.
        """

        self._check()
        if not callable(plan):
            raise TypeError('post-settlement cleanup plan must be callable')
        with self._owner._mailbox:
            if (self._owner._active is not self._ticket
                    or self._ticket._phase != 'running'):
                raise RuntimeError('Cleanup registration requires the active plan')
            if self._ticket._post_settlement_cleanup_plan is not None:
                raise RuntimeError('Only one post-settlement cleanup may be registered')
            self._ticket._post_settlement_cleanup_plan = plan

    def _transport_entered(self) -> None:
        self._check()
        with self._owner._mailbox:
            if self._ticket._stop_requested:
                if self._ticket._ever_entered:
                    self._ticket._stop_blocked_after_effect = True
                    raise StopPendingTeardown('Remote work preceded Stop; target evidence is required')
                raise CancelledBeforeEffect()
            self._ticket._entered = True
            self._ticket._ever_entered = True
            target = self._owner._session.target
            if target is not None:
                self._ticket._effect_target = target.target_id

    def _stop_checkpoint(self) -> None:
        self._check()
        with self._owner._mailbox:
            if self._ticket._stop_requested:
                self._ticket._stop_blocked_after_effect = True
                raise StopPendingTeardown('Stop requested while remote work remains outstanding')

    def _require_idle(self) -> None:
        self._check()
        if self._ticket._pending is not None or self._ticket._stop_target is not None or self._ticket._entered:
            raise ArbiterBusy('A remote operation is still outstanding')

    def set_breakpoints(self, locations: tuple[ModuleLocation, ...]) -> None:
        self._require_idle()
        self._owner._session.set_breakpoints(locations, on_transport_dispatch=self._transport_entered)
        with self._owner._mailbox:
            self._ticket._entered = False

    def local_variables(self, stack_level: int = 0, *, timeout_s: float = 30.0) -> LocalVariablesResult:
        """Read a stopped frame while retaining ownership of ambiguous dispatch."""
        self._require_idle()
        result = self._owner._session.local_variables(
            stack_level=stack_level, timeout_s=timeout_s,
            on_transport_dispatch=self._transport_entered,
        )
        with self._owner._mailbox:
            self._ticket._entered = False
        return result

    def heartbeat(self) -> dict[str, object]:
        """Renew the Debug UI lease without creating a second event reader."""
        self._require_idle()
        result = self._owner._session.heartbeat(on_transport_dispatch=self._transport_entered)
        with self._owner._mailbox:
            self._ticket._entered = False
        return result

    def modify(self, variable: str, value_expression: str, *,
               on_transport_dispatch: Callable[[], None] | None = None) -> ModifyResult:
        self._require_idle()
        previously_entered = self._ticket._ever_entered

        def entered() -> None:
            self._transport_entered()
            if on_transport_dispatch is not None:
                try:
                    on_transport_dispatch()
                except BaseException:
                    # The caller's bookkeeping rejected this command before
                    # transport entry; preserve prior ownership evidence.
                    with self._owner._mailbox:
                        self._ticket._entered = False
                        self._ticket._ever_entered = previously_entered
                    raise

        result = self._owner._session.modify(
            variable, value_expression, on_transport_dispatch=entered
        )
        with self._owner._mailbox:
            self._ticket._entered = False
        return result

    def continue_(self, *, on_transport_dispatch: Callable[[], None] | None = None) -> None:
        self._require_idle()
        target = self._owner._session.target
        if target is None:
            raise ValueError('Continue requires a selected target')
        def entered() -> None:
            previously_entered = self._ticket._ever_entered
            self._transport_entered()
            with self._owner._mailbox:
                self._ticket._stop_target = target.target_id
            if on_transport_dispatch is not None:
                try:
                    on_transport_dispatch()
                except BaseException:
                    # RdbgSession invokes this callback before transport.request.
                    # A local rejection cannot leave remote stop ownership live.
                    with self._owner._mailbox:
                        self._ticket._stop_target = None
                        self._ticket._entered = False
                        self._ticket._ever_entered = previously_entered
                    raise
        self._owner._session.continue_(on_transport_dispatch=entered)
        with self._owner._mailbox:
            self._ticket._entered = False

    def wait_for_any_stop(self, *, timeout_s: float = 60.0) -> StopEvent:
        self._check()
        expected = self._ticket._stop_target
        if expected is None:
            raise ValueError('No Continue stop is outstanding')
        try:
            stop = self._owner._session.wait_for_any_stop(
                timeout_s=timeout_s, expected_target=expected, on_transport_dispatch=self._transport_entered)
        except StopWaitIntervalElapsed:
            self._stop_checkpoint()
            raise
        if stop.target_id != expected:
            raise OutcomeUnknown('Stop belongs to a different target')
        with self._owner._mailbox:
            self._ticket._stop_target = None
            self._ticket._entered = False
        return stop

    def _start(self, operation: Callable[..., PendingEvaluation], expression: str,
               **options: Any) -> PendingEvaluation:
        self._require_idle()
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
        try:
            event = self._owner._session.wait_evaluation_event(
                pending, timeout_s=timeout_s,
                on_transport_dispatch=self._transport_entered,
            )
        except CommandTimeout as error:
            if type(error) is not CommandTimeout:
                raise OutcomeUnknown('Evaluation wait failed before its remote outcome was proven') from error
            self._stop_checkpoint()
            raise
        if isinstance(event, EvaluationResult):
            if event.result_id != pending.result_id:
                raise OutcomeUnknown('Session returned a mismatched evaluation result')
            # The session contract retires this exact capability on matched result.
            with self._owner._mailbox:
                self._ticket._pending = None
                self._ticket._pending_stop = None
                self._ticket._entered = False
        elif not isinstance(event, StopEvent):
            raise OutcomeUnknown('Session returned an unrecognized evaluation event')
        else:
            if event.target_id != pending.target_id:
                raise OutcomeUnknown('Evaluation stop belongs to a different target')
            with self._owner._mailbox:
                self._ticket._pending_stop = event
        return event

    def continue_evaluation(self, pending: PendingEvaluation, stop: StopEvent) -> None:
        self._require_pending(pending)
        if stop is not self._ticket._pending_stop:
            raise ValueError('The exact owned stop is required to continue evaluation')
        self._owner._session.continue_evaluation(
            pending, stop, on_transport_dispatch=self._transport_entered,
        )
        with self._owner._mailbox:
            self._ticket._pending_stop = None
            self._ticket._entered = False


Plan = Callable[[SessionPort], Settlement | ReadyForPolicy | ConfirmedFailure]


class ServerTeardownAttempt:
    """One worker-owned termination attempt; waiter expiry changes no ownership."""

    def __init__(self, owner: RdbgArbiter, ticket: ExecutionTicket,
                 expected_target: TargetId, grace_s: float,
                 request_termination: bool) -> None:
        self._owner = owner
        self.ticket = ticket
        self.expected_target = expected_target
        self.grace_s = grace_s
        self.request_termination = request_termination
        self._result: ServerTerminationConfirmed | TerminationUnknown | None = None
        self._done = False

    def wait(self, timeout: float | None = None) -> ServerTerminationConfirmed | TerminationUnknown:
        with self._owner._mailbox:
            if not self._owner._mailbox.wait_for(lambda: self._done, timeout):
                raise TimeoutError('Local teardown waiter interval elapsed; target remains owned')
            assert self._result is not None
            return self._result


class RdbgArbiter:
    """Own one session after bootstrap, with one mailbox and one worker.

    The port exposes evaluation and MAIN command operations with ownership evidence.
    No background polling is started: active plans read events on this worker.
    Unknown plans keep the slot until a reconciliation or exact target absence
    proof settles them. A user Stop schedules server termination on this same
    worker only after the active plan relinquishes it; unmatched target evidence
    keeps the old owner fenced.
    """

    def __init__(self, session: EvaluationSession, route: RouteToken):
        self._session = session
        self._route = route
        self._mailbox = Condition()
        self._queue: deque[ExecutionTicket] = deque()
        self._active: ExecutionTicket | None = None
        self._reconciliation: Plan | None = None
        self._server_teardown: ServerTeardownAttempt | None = None
        self._cleanup_debt: ExecutionTicket | None = None
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

    @property
    def has_pending_operations(self) -> bool:
        """True while a queued or running ticket owns the admission boundary."""
        with self._mailbox:
            return self._active is not None or bool(self._queue)

    def submit(self, route: RouteToken, plan: Plan, *,
               finalizer: Callable[[Any], Any] | None = None) -> ExecutionTicket:
        with self._mailbox:
            if self._closed:
                raise RuntimeError('Arbiter is closed')
            if self._active is not None and self._active._stop_requested:
                raise ArbiterBusy('Stop is requested for the active operation')
            if route != self._route:
                raise StaleRoute()
            ticket = ExecutionTicket(self, route, plan, finalizer)
            self._queue.append(ticket)
            return ticket

    def try_heartbeat(self) -> ExecutionTicket | None:
        """Queue one best-effort heartbeat only while the RDBG owner is idle."""

        with self._mailbox:
            if (self._closed or self._active is not None or self._queue
                    or self._reconciliation is not None or self._server_teardown is not None):
                return None
            ticket = ExecutionTicket(
                self, self._route, lambda port: Settlement(port.heartbeat())
            )
            self._queue.append(ticket)
            ticket._ready = True
            self._mailbox.notify_all()
            return ticket

    def dispatch(self, ticket: ExecutionTicket) -> None:
        """Called only after the submitting layer has published its ticket."""
        with self._mailbox:
            self._check_ticket(ticket)
            if ticket._phase != 'queued':
                raise RuntimeError('Ticket is no longer queued')
            ticket._ready = True
            self._mailbox.notify_all()

    def retry_post_settlement_cleanup(self, ticket: ExecutionTicket) -> ExecutionTicket:
        """Retry a confirmed failed cleanup without replaying its parent plan."""

        with self._mailbox:
            self._check_ticket(ticket)
            plan = ticket._post_settlement_cleanup_plan
            previous = ticket._post_settlement_cleanup
            if (plan is None or previous is None or previous._phase != 'settled'
                    or previous._error is None or self._cleanup_debt is not ticket):
                raise ArbiterBusy('Cleanup is not a confirmed retryable failure')
            if self._closed or self._active is not None:
                raise ArbiterBusy('Cleanup retry requires an idle arbiter')
            cleanup = ExecutionTicket(self, self._route, plan)
            cleanup._post_settlement_parent = ticket
            cleanup._ready = True
            ticket._post_settlement_cleanup = cleanup
            self._queue.appendleft(cleanup)
            self._mailbox.notify_all()
            return cleanup

    def request_stop(self, ticket: ExecutionTicket) -> StopRequestOutcome:
        """Fence dispatch and schedule exact server teardown if still unknown.

        A queued ticket has no remote effects and can be cancelled locally.
        An active ticket keeps ownership until its plan settles or reconciles.
        """
        with self._mailbox:
            self._check_ticket(ticket)
            if ticket._phase == 'settled':
                return StopRequestOutcome.ALREADY_SETTLED
            if ticket._phase == 'queued':
                self._queue.remove(ticket)
                self._settle(ticket, error=CancelledBeforeEffect())
                return StopRequestOutcome.CANCELLED_BEFORE_EFFECT
            if self._active is not ticket:
                raise ArbiterBusy('Stop requires the active operation')
            if not ticket._stop_requested:
                ticket._stop_requested = True
                while self._queue:
                    self._settle(self._queue.popleft(), error=CancelledBeforeEffect())
                self._mailbox.notify_all()
            self._schedule_auto_server_stop_locked(ticket)
            return StopRequestOutcome.REQUESTED

    def _schedule_auto_server_stop_locked(self, ticket: ExecutionTicket) -> None:
        """Run Stop only after the active plan relinquishes the one RDBG worker."""

        if (
            not ticket._stop_requested
            or ticket._phase != 'unknown'
            or self._active is not ticket
            or self._reconciliation is not None
            or self._server_teardown is not None
        ):
            return
        expected = self._exact_server_stop_target_locked(ticket)
        if expected is None:
            # File targets require their exact process owner; an uncertain
            # server target must remain fenced instead of killing a guess.
            return
        attempt = ServerTeardownAttempt(
            self, ticket, expected, 30.0,
            request_termination=not ticket._server_termination_attempted,
        )
        ticket._server_termination_attempted = True
        ticket._stop_teardown = attempt
        self._server_teardown = attempt
        self._mailbox.notify_all()

    def _exact_server_stop_target_locked(
        self, ticket: ExecutionTicket,
    ) -> TargetId | None:
        expected = (
            ticket._pending.target_id if ticket._pending is not None else
            ticket._stop_target or ticket._effect_target
        )
        selected = self._session.target
        if (
            expected is None
            or expected.seance_id is None
            or selected is None
            or selected.target_id != expected
            or selected.target_type != 'Server'
        ):
            return None
        return expected

    def reconcile(self, ticket: ExecutionTicket, plan: Plan) -> None:
        """Schedule evidence collection or confirmed teardown, never blind retry."""
        with self._mailbox:
            self._check_ticket(ticket)
            if (self._active is not ticket or ticket._phase != 'unknown'
                    or self._reconciliation is not None or self._server_teardown is not None):
                raise ArbiterBusy('Reconciliation requires the unknown owner')
            self._reconciliation = plan
            # Do not let wait_unknown() observe the previous unknown phase as
            # the outcome of this newly scheduled reconciliation attempt.
            ticket._phase = 'reconciling'
            self._mailbox.notify_all()

    def teardown_fenced_server_target(
        self, ticket: ExecutionTicket, route: RouteToken, *, grace_s: float = 30.0,
    ) -> ServerTeardownAttempt:
        """Run exact server teardown and absence proof on the RDBG worker.

        Only an unknown, stopped ticket may enter. An unknown proof retains
        the owner; a later explicit attempt probes absence without resending
        the termination command. The returned wait is local and detachable.
        """
        if (isinstance(grace_s, bool) or not isinstance(grace_s, (int, float))
                or not isfinite(float(grace_s)) or grace_s < 0):
            raise ValueError('grace_s must be finite and non-negative')
        with self._mailbox:
            self._check_ticket(ticket)
            if route != self._route:
                raise StaleRoute('Server teardown belongs to another route')
            if (self._active is not ticket or ticket._phase != 'unknown'
                    or not ticket._stop_requested or self._reconciliation is not None
                    or self._server_teardown is not None):
                raise ArbiterBusy('Server teardown requires the fenced unknown owner')
            expected = self._exact_server_stop_target_locked(ticket)
            if expected is None:
                raise ValueError('Server teardown requires the exact selected server target')
            attempt = ServerTeardownAttempt(
                self, ticket, expected, float(grace_s),
                request_termination=not ticket._server_termination_attempted,
            )
            ticket._server_termination_attempted = True
            ticket._stop_teardown = attempt
            self._server_teardown = attempt
            self._mailbox.notify_all()
            return attempt

    def retire_terminated_target(
        self,
        ticket: ExecutionTicket,
        route: RouteToken,
        evidence: FileTerminationConfirmed | ServerTerminationConfirmed,
    ) -> None:
        """Retire an unknown owner only after exact target termination proof.

        The old arbiter closes to user dispatch; a replacement runtime needs a
        new arbiter and incarnation. This method does not terminate a target.
        """
        with self._mailbox:
            self._check_ticket(ticket)
            if (self._active is not ticket or ticket._phase != 'unknown'
                    or self._reconciliation is not None or self._server_teardown is not None):
                raise ArbiterBusy('Target retirement requires the unknown owner')
            if route != self._route:
                raise StaleRoute('Termination evidence belongs to another route')
            if type(evidence) not in (FileTerminationConfirmed, ServerTerminationConfirmed):
                raise ValueError('confirmed target termination evidence is required')
            expected = (ticket._pending.target_id if ticket._pending is not None else
                        ticket._stop_target or ticket._effect_target)
            if expected is None or evidence.expected_target != expected:
                raise ValueError('Termination evidence belongs to another target')
            if isinstance(evidence, ServerTerminationConfirmed):
                absence = evidence.absence
                if (
                    not isinstance(absence, BoundServerTargetAbsence)
                    or absence.expected_target != expected
                    or absence.bound_client.infobase_alias.casefold() != expected.infobase_alias.casefold()
                    or absence.bound_client.seance_id != expected.seance_id
                    or (absence.bound_client.infobase_instance_id is not None
                        and expected.infobase_instance_id is not None
                        and absence.bound_client.infobase_instance_id != expected.infobase_instance_id)
                ):
                    raise ValueError('server absence evidence belongs to another target')

            self._closed = True
            while self._queue:
                self._settle(self._queue.popleft(), error=CancelledBeforeEffect())
            ticket._pending = None
            ticket._pending_stop = None
            ticket._stop_target = None
            ticket._entered = False
            self._settle(ticket, error=TargetTerminated(evidence))
            self._active = None
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
        assert (ticket._pending is None and ticket._pending_stop is None
                and ticket._stop_target is None and not ticket._entered), 'Cannot settle an owned evaluation capability'
        ticket._phase = 'settled'
        self._mailbox.notify_all()

    def _run(self) -> None:
        while True:
            with self._mailbox:
                self._mailbox.wait_for(lambda: self._closed or self._server_teardown is not None or
                    self._reconciliation is not None or (
                    self._active is None and bool(self._queue) and self._queue[0]._ready
                    and (self._cleanup_debt is None
                         or self._queue[0]._post_settlement_parent is self._cleanup_debt)
                ))
                if self._closed:
                    return
                teardown = self._server_teardown
                if teardown is None:
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
            if teardown is not None:
                self._run_server_teardown(teardown)
                continue
            port = SessionPort(self, ticket)
            try:
                outcome = plan(port)
                if isinstance(outcome, ConfirmedFailure):
                    if not isinstance(outcome.error, BaseException):
                        raise TypeError('Confirmed failure requires an exception')
                    if ticket._finalizer_started:
                        raise OutcomeUnknown('Policy finalizer already started; its effects cannot be replaced')
                elif ticket._finalizer is None:
                    if not isinstance(outcome, Settlement):
                        raise TypeError('Raw plan must return a confirmed Settlement')
                elif ticket._finalizer_started:
                    raise OutcomeUnknown('Policy finalizer already started; its effects cannot be repeated')
                elif not isinstance(outcome, ReadyForPolicy):
                    raise OutcomeUnknown('Policy ticket requires a confirmed raw outcome after cleanup')
                if ticket._stop_blocked_after_effect and not reconciling:
                    raise StopPendingTeardown('A stopped plan cannot settle after blocking remote work')
                if ticket._pending is not None or ticket._stop_target is not None or ticket._entered:
                    raise OutcomeUnknown('Settlement cannot discard an unretired capability')
                if not isinstance(outcome, ConfirmedFailure) and outcome.next_route is not None:
                    with self._mailbox:
                        current = self._route
                        next_route = outcome.next_route
                        newer_epoch = next_route.epoch > current.epoch
                        newer_revision = (next_route.epoch == current.epoch
                                          and next_route.context_id == current.context_id
                                          and next_route.revision > current.revision)
                        if next_route.incarnation != current.incarnation or not (newer_epoch or newer_revision):
                            raise StaleRoute('Settlement cannot reverse or repeat the current route')
                if isinstance(outcome, ReadyForPolicy):
                    # The callback may publish namespace/Worker state. It runs
                    # outside the mailbox and cannot be retried after entry.
                    ticket._finalizer_started = True
                    assert ticket._finalizer is not None
                    outcome = Settlement(
                        ticket._finalizer(outcome.raw_outcome), outcome.next_route
                    )
            except BaseException as error:
                with self._mailbox:
                    if (reconciling or ticket._entered or ticket._pending is not None
                            or ticket._stop_target is not None or ticket._finalizer_started
                            or isinstance(error, OutcomeUnknown)):
                        ticket._phase = 'unknown'
                        ticket._error = error
                        self._schedule_auto_server_stop_locked(ticket)
                        self._mailbox.notify_all()
                    else:
                        cleanup_plan = ticket._post_settlement_cleanup_plan
                        if cleanup_plan is not None:
                            self._queue_post_settlement_cleanup(ticket, cleanup_plan)
                        self._settle(ticket, error=error)
                        if ticket._post_settlement_parent is not None:
                            self._cleanup_debt = ticket._post_settlement_parent
                        self._active = None
            else:
                with self._mailbox:
                    cleanup_plan = ticket._post_settlement_cleanup_plan
                    if isinstance(outcome, ConfirmedFailure):
                        self._settle(ticket, error=outcome.error)
                    else:
                        if outcome.next_route is not None:
                            self._route = outcome.next_route
                        self._settle(ticket, value=outcome.value)
                    if cleanup_plan is not None:
                        self._queue_post_settlement_cleanup(ticket, cleanup_plan)
                    if ticket._post_settlement_parent is self._cleanup_debt:
                        self._cleanup_debt = None
                    self._active = None
                    self._mailbox.notify_all()
            finally:
                port._live = False

    def _queue_post_settlement_cleanup(
        self, parent: ExecutionTicket, plan: Plan,
    ) -> None:
        """Queue cleanup before already-waiting work after parent settlement."""

        cleanup = ExecutionTicket(self, self._route, plan)
        cleanup._post_settlement_parent = parent
        cleanup._ready = True
        parent._post_settlement_cleanup = cleanup
        self._queue.appendleft(cleanup)

    def _run_server_teardown(self, attempt: ServerTeardownAttempt) -> None:
        assert get_ident() == self._worker.ident
        try:
            result = terminate_server_target(
                self._session, attempt.expected_target, grace_s=attempt.grace_s,
                request_termination=attempt.request_termination,
            )
        except BaseException as error:
            result = TerminationUnknown(
                attempt.expected_target, 'confirmation', type(error).__name__, None,
            )
        with self._mailbox:
            assert self._server_teardown is attempt
            self._server_teardown = None
            if isinstance(result, ServerTerminationConfirmed):
                try:
                    self.retire_terminated_target(attempt.ticket, self._route, result)
                except ValueError:
                    result = TerminationUnknown(
                        attempt.expected_target, 'confirmation', 'EvidenceMismatch', None,
                    )
            attempt._result = result
            attempt._done = True
            self._mailbox.notify_all()
