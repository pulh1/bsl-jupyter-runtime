"""Generic cell preparation and admission; route decisions remain in its ports."""

from contextlib import AbstractContextManager, nullcontext
from threading import Lock
from typing import Callable

from onec_runtime.bsl.source_maps import SourceUnitRef
from onec_runtime.execution.contracts import (
    Accepted,
    CommonCell,
    CommonCellParser,
    Current,
    ExecutionControllerPort,
    PreparationContext,
    PreparationSnapshotReader,
    PreparationSnapshots,
    PreparedCell,
    Rejected,
    ReplyPresenter,
    SourceDiagnostic,
    StalePreparedDispatch,
    StalePreparation,
    SubmissionReceipt,
    Unavailable,
)


class StalePreparedCell(RuntimeError):
    """A separately prepared cell lost its route or snapshot before dispatch."""


class PreparedCellHandle:
    """Opaque one-use candidate bound to one pipeline and exact owner guards.

    Preparation never admits a ticket. The handle cannot be submitted through
    another pipeline or reused after any execution attempt, including a stale
    one. The controller remains the authority for route and version checks.
    """

    __slots__ = ("_owner", "_context", "_prepared", "_guards", "_claimed", "_lock")

    def __init__(
        self, owner: object, context: PreparationContext,
        prepared: PreparedCell, guards: object,
    ) -> None:
        self._owner = owner
        self._context = context
        self._prepared = prepared
        self._guards = guards
        self._claimed = False
        self._lock = Lock()

    def _claim(self, owner: object) -> tuple[PreparationContext, PreparedCell, object]:
        if self._owner is not owner:
            raise TypeError("Prepared cell belongs to another pipeline")
        with self._lock:
            if self._claimed:
                raise RuntimeError("Prepared cell was already consumed")
            self._claimed = True
        return self._context, self._prepared, self._guards


class CellExecutionPipeline:
    def __init__(
        self,
        parser: CommonCellParser,
        controller: ExecutionControllerPort,
        snapshots: PreparationSnapshotReader,
        replies: ReplyPresenter,
    ) -> None:
        self._parser = parser
        self._controller = controller
        self._snapshots = snapshots
        self._replies = replies
        self._preparation_owner = object()

    def prepare(
        self,
        source: str,
        source_unit: SourceUnitRef,
        *,
        wait_handoff: Callable[[], AbstractContextManager[None]] | None = None,
        on_prepared: Callable[[PreparedCell], None] | None = None,
    ) -> PreparedCellHandle | SourceDiagnostic | Unavailable:
        """Lower locally and seal the exact context and snapshot for admission.

        Diagnostics and unavailable routes are typed local results. A stale
        policy diagnostic is discarded and prepared again on a fresh route.
        No ticket, Worker activation or RDBG request is made here.
        """

        release_wait = wait_handoff or nullcontext
        common = self._parser.prepare(source, source_unit)
        if isinstance(common, SourceDiagnostic):
            return common
        while True:
            with release_wait():
                context = self._controller.await_preparation_context()
            if isinstance(context, Unavailable):
                return context
            snapshots = self._snapshots.read_for(context.capabilities)
            prepared = context.policy.prepare(common, snapshots, context)
            if isinstance(prepared, SourceDiagnostic):
                validity = self._controller.validate_preparation(context, snapshots.guards)
                if isinstance(validity, StalePreparation):
                    continue
                if isinstance(validity, Current):
                    return prepared
                if isinstance(validity, Unavailable):
                    return validity
                raise TypeError("Invalid preparation validation result")
            if on_prepared is not None:
                on_prepared(prepared)
            return PreparedCellHandle(
                self._preparation_owner, context, prepared, snapshots.guards,
            )

    def execute_prepared(
        self,
        candidate: PreparedCellHandle,
        *,
        wait_handoff: Callable[[], AbstractContextManager[None]] | None = None,
        on_admitted: Callable[[PreparedCell], None] | None = None,
    ) -> object:
        """Consume one candidate and admit it only against its original guards.

        Staleness is reported to the caller for a fresh explicit preparation;
        this method never repeats an adopted or possibly dispatched operation.
        """

        if not isinstance(candidate, PreparedCellHandle):
            raise TypeError("A sealed prepared cell is required")
        context, prepared, guards = candidate._claim(self._preparation_owner)
        release_wait = wait_handoff or nullcontext
        receipt = SubmissionReceipt()
        try:
            validity = self._controller.validate_preparation(context, guards)
            if isinstance(validity, StalePreparation):
                raise StalePreparedCell(validity.reason)
            if isinstance(validity, Unavailable):
                return self._replies.unavailable_reply(validity)
            if not isinstance(validity, Current):
                raise TypeError("Invalid preparation validation result")
            admission = self._controller.submit_cell(context, prepared, guards, receipt)
            if isinstance(admission, Rejected):
                if receipt.ticket is not None:
                    raise RuntimeError("Rejected admission adopted a ticket")
                if isinstance(admission.reason, StalePreparation):
                    raise StalePreparedCell(admission.reason.reason)
                if isinstance(admission.reason, Unavailable):
                    return self._replies.unavailable_reply(admission.reason)
                raise TypeError("Invalid admission rejection")
            if not isinstance(admission, Accepted):
                raise TypeError("Invalid admission result")
            if admission.ticket is not receipt.ticket:
                raise RuntimeError("Accepted ticket was not adopted by submission receipt")
            if on_admitted is not None:
                on_admitted(prepared)
            with release_wait():
                return admission.ticket.wait_initiator()
        except KeyboardInterrupt:
            if receipt.ticket is not None:
                self._controller.request_stop(receipt.ticket)
            raise

    def execute(
        self,
        source: str,
        source_unit: SourceUnitRef,
        *,
        wait_handoff: Callable[[], AbstractContextManager[None]] | None = None,
        on_prepared: Callable[[PreparedCell], None] | None = None,
        on_admitted: Callable[[PreparedCell], None] | None = None,
    ) -> object:
        """Execute a cell, releasing a caller lock only at blocking waits.

        ``on_admitted`` runs after the controller accepts the ticket. The
        controller may have dispatched it by then; this is a publication
        callback, not a before-transport hook.
        """

        release_wait = wait_handoff or nullcontext
        common: CommonCell = self._parser.prepare(source, source_unit)
        if isinstance(common, SourceDiagnostic):
            return self._replies.diagnostic_reply(common)
        while True:
            with release_wait():
                context: PreparationContext = self._controller.await_preparation_context()
            if isinstance(context, Unavailable):
                return self._replies.unavailable_reply(context)
            snapshots: PreparationSnapshots = self._snapshots.read_for(context.capabilities)
            prepared = context.policy.prepare(common, snapshots, context)
            if isinstance(prepared, SourceDiagnostic):
                validity = self._controller.validate_preparation(context, snapshots.guards)
                if isinstance(validity, StalePreparation):
                    continue
                if isinstance(validity, Current):
                    return self._replies.diagnostic_reply(prepared)
                if isinstance(validity, Unavailable):
                    return self._replies.unavailable_reply(validity)
                raise TypeError("Invalid preparation validation result")
            if on_prepared is not None:
                on_prepared(prepared)
            receipt = SubmissionReceipt()
            try:
                try:
                    admission = self._controller.submit_cell(
                        context, prepared, snapshots.guards, receipt
                    )
                except StalePreparedDispatch:
                    # Only admission may report proven pre-effect staleness.
                    # A ticket waiter can raise the same type after remote work.
                    continue
                if isinstance(admission, Rejected):
                    if receipt.ticket is not None:
                        raise RuntimeError("Rejected admission adopted a ticket")
                    if isinstance(admission.reason, StalePreparation):
                        continue
                    if isinstance(admission.reason, Unavailable):
                        return self._replies.unavailable_reply(admission.reason)
                    raise TypeError("Invalid admission rejection")
                if not isinstance(admission, Accepted):
                    raise TypeError("Invalid admission result")
                if admission.ticket is not receipt.ticket:
                    raise RuntimeError("Accepted ticket was not adopted by submission receipt")
                if on_admitted is not None:
                    on_admitted(prepared)
                with release_wait():
                    return admission.ticket.wait_initiator()
            except KeyboardInterrupt:
                if receipt.ticket is not None:
                    self._controller.request_stop(receipt.ticket)
                raise
