"""Generic cell preparation and admission; route decisions remain in its ports."""

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
    Rejected,
    ReplyPresenter,
    SourceDiagnostic,
    StalePreparedDispatch,
    StalePreparation,
    SubmissionReceipt,
    Unavailable,
)


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

    def execute(self, source: str, source_unit: SourceUnitRef) -> object:
        common: CommonCell = self._parser.prepare(source, source_unit)
        if isinstance(common, SourceDiagnostic):
            return self._replies.diagnostic_reply(common)
        while True:
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
                return admission.ticket.wait_initiator()
            except KeyboardInterrupt:
                if receipt.ticket is not None:
                    self._controller.request_stop(receipt.ticket)
                raise
