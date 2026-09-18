"""Own Stop observation for one arbiter without reading the debugger stream."""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock, Thread
from typing import Callable
from weakref import WeakKeyDictionary

from onec_runtime.execution.arbiter import (
    ExecutionTicket, RdbgArbiter, StopRequestOutcome, TargetTerminated,
)
from onec_runtime.execution.capture.scope import CaptureScope
from onec_runtime.execution.main.operation import MainOperation
from onec_runtime.execution.termination import (
    FileTerminationConfirmed, ServerTerminationConfirmed,
)
from onec_runtime.rdbg.models import TargetId


@dataclass(frozen=True, slots=True)
class StopContext:
    operation: MainOperation | None
    scope: CaptureScope | None
    target: TargetId | None


TargetExitProof = FileTerminationConfirmed | ServerTerminationConfirmed


class StopCoordinator:
    """Fence a ticket and publish loss only after exact target-exit proof."""

    def __init__(
        self,
        arbiter: RdbgArbiter,
        *,
        snapshot: Callable[[], StopContext],
        cancelled_before_effect: Callable[[ExecutionTicket, StopContext], None],
        confirmed_exit: Callable[[StopContext, TargetExitProof], None],
    ) -> None:
        self._arbiter = arbiter
        self._snapshot = snapshot
        self._cancelled_before_effect = cancelled_before_effect
        self._confirmed_exit = confirmed_exit
        self._lock = RLock()
        self._observed: WeakKeyDictionary[ExecutionTicket, bool] = WeakKeyDictionary()

    def request_stop(self, ticket: ExecutionTicket) -> StopRequestOutcome:
        context = self._snapshot()
        outcome = self._arbiter.request_stop(ticket)
        if outcome is StopRequestOutcome.CANCELLED_BEFORE_EFFECT:
            self._cancelled_before_effect(ticket, context)
        elif outcome is StopRequestOutcome.REQUESTED:
            with self._lock:
                if ticket not in self._observed:
                    self._observed[ticket] = True
                    Thread(
                        target=self._observe,
                        args=(ticket, context),
                        name="onec-target-stop-observer",
                        daemon=True,
                    ).start()
        return outcome

    def _observe(self, ticket: ExecutionTicket, context: StopContext) -> None:
        try:
            ticket.wait_settled()
        except TargetTerminated as terminated:
            evidence = terminated.evidence
            if context.target is not None and evidence.expected_target != context.target:
                return
        except BaseException:
            # A cell failure or unconfirmed teardown gives no target-loss proof.
            return
        else:
            return
        self._confirmed_exit(context, evidence)
