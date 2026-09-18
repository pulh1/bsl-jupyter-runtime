"""Pure MAIN/CAPTURE outcome presentation for the component execution path.

The publication records keep cell identity and exact source mapping after an
initial MAIN ticket yields at CAPTURE. RDBG message collection, namespace
commit, Worker ownership and public session wiring remain separate ports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from onec_runtime.bsl.diagnostics import (
    DiagnosticStage, NormalizedDiagnostic, VisibleSourceContext,
    parse_platform_diagnostic, remap_platform_diagnostic,
)
from onec_runtime.bsl.source_maps import MappedSource
from onec_runtime.execution.capture.scope import (
    CaptureContextState, CaptureFrameIdentity, CaptureScope,
)
from onec_runtime.execution.controller.controller import MainYield, MainYieldKind
from onec_runtime.execution.main import MainOperation, MainPhase
from onec_runtime.execution.main.completion import MainRemoteCompletion
from onec_runtime.rdbg.models import EvaluationResult
from onec_runtime.table_value import evaluation_to_python

if TYPE_CHECKING:
    from onec_runtime.runtime_models import CaptureCorrelationTicket, RuntimeReply


_BSL_FAILURE = "BSL execution failed"
_CAPTURE_DECODE_FAILURE = "CAPTURE result decode failed"
_MAIN_DECODE_FAILURE = "MAIN completion decode failed"


def _execution_diagnostic(
    error: str,
    source: MappedSource | None,
    visible: VisibleSourceContext | None,
) -> NormalizedDiagnostic | None:
    if not error or source is None:
        return None
    try:
        return remap_platform_diagnostic(
            parse_platform_diagnostic(error), source,
            stage=DiagnosticStage.EXECUTION,
            visible_source_context=visible,
        )
    except Exception:
        # Presentation failure must not turn a confirmed BSL result into an
        # unknown remote operation or expose unparsed platform text.
        return None


@dataclass(frozen=True, slots=True)
class MainPublicationRecord:
    """One MAIN command's reply evidence, retained through its CAPTURE stops.

    ``prior_capture_sequence`` is the controller's local capture count at MAIN
    admission. The public stop count for this command starts at one, while the
    local sequence remains monotonic for stale-handle fencing. The message key
    must be supplied to the remote completion reader after a later resume;
    publication consumes only the already collected completion messages.
    """

    operation: MainOperation = field(repr=False)
    prior_capture_sequence: int
    executed_source: MappedSource | None = field(default=None, repr=False)
    visible_source_context: VisibleSourceContext | None = field(default=None, repr=False)
    changed_roots: tuple[str, ...] = ()
    capture_ticket: CaptureCorrelationTicket | None = None
    message_collector_key: str = field(default="", repr=False)


@dataclass(frozen=True, slots=True)
class MainConfirmedDecodeFailure:
    """MAIN ID matched, but a later completion scalar could not be decoded.

    Construct this only after ``MainOperation.remote_completed``. A decode
    failure before command-ID correlation has no such proof and must remain a
    protocol failure, not a terminal reply for this MAIN command.
    """

    operation: MainOperation = field(repr=False)
    remote_error: str = field(default="", repr=False)
    messages: tuple[str, ...] = ()


class MainReplyPolicy:
    """Map one confirmed MAIN yield to the existing public reply contract."""

    def publish(
        self, outcome: MainYield | MainConfirmedDecodeFailure,
        record: MainPublicationRecord,
    ) -> RuntimeReply:
        # RuntimeApi can import this policy before declaring its public reply
        # dataclasses; resolve the public contract only when publishing.
        from onec_runtime.runtime_models import OperationState, RuntimeReply, RuntimeReplyKind

        if (
            not isinstance(outcome, (MainYield, MainConfirmedDecodeFailure))
            or outcome.operation is not record.operation
        ):
            raise ValueError("MAIN yield belongs to another publication record")
        operation = record.operation
        if isinstance(outcome, MainConfirmedDecodeFailure):
            if operation.phase is not MainPhase.COMPLETED:
                raise ValueError("MAIN command completion is not confirmed")
            return RuntimeReply(
                RuntimeReplyKind.MAIN_COMPLETED,
                operation.command_id,
                OperationState.FAILED,
                error=_BSL_FAILURE if outcome.remote_error else _MAIN_DECODE_FAILURE,
                succeeded=False,
                messages=outcome.messages,
                changed_roots=record.changed_roots,
                diagnostic=_execution_diagnostic(
                    outcome.remote_error,
                    record.executed_source,
                    record.visible_source_context,
                ),
            )
        if outcome.kind is MainYieldKind.COMPLETED:
            completion = outcome.completion
            if not isinstance(completion, MainRemoteCompletion) or operation.phase is not MainPhase.COMPLETED:
                raise ValueError("MAIN completion is not confirmed")
            succeeded = not completion.error
            return RuntimeReply(
                RuntimeReplyKind.MAIN_COMPLETED,
                operation.command_id,
                OperationState.COMPLETED if succeeded else OperationState.FAILED,
                result=completion.result,
                error="" if succeeded else _BSL_FAILURE,
                succeeded=succeeded,
                messages=completion.messages,
                changed_roots=record.changed_roots,
                diagnostic=_execution_diagnostic(
                    completion.error, record.executed_source,
                    record.visible_source_context,
                ),
            )
        if outcome.kind is MainYieldKind.CAPTURE:
            scope = outcome.scope
            if (
                not isinstance(scope, CaptureScope)
                or operation.phase is not MainPhase.SUSPENDED_CAPTURE
                or scope.identity.main_command_id != operation.command_id
                or scope.observed_main_command_id != operation.command_id
                or scope.context_state is not CaptureContextState.READY
                or scope.frame_identity is not CaptureFrameIdentity.CONFIRMED
            ):
                raise ValueError("MAIN CAPTURE stop is not confirmed")
            stop_sequence = (
                scope.identity.local_stop_sequence - record.prior_capture_sequence
            )
            if stop_sequence <= 0:
                raise ValueError("CAPTURE stop predates MAIN admission")
            ticket = record.capture_ticket
            ticket_id = (
                ticket.ticket_id
                if ticket is not None
                and ticket.expected_operation_id == operation.command_id
                and ticket.expected_stop_sequence == stop_sequence
                else None
            )
            return RuntimeReply(
                RuntimeReplyKind.CAPTURED,
                operation.command_id,
                OperationState.CAPTURED,
                location=scope.identity.location,
                stop_sequence=stop_sequence,
                capture_ticket=ticket_id,
                observed_command_id=scope.observed_main_command_id,
                changed_roots=record.changed_roots,
            )
        if outcome.kind is MainYieldKind.DEBUG_STOP:
            if (
                outcome.stop is None
                or operation.phase is not MainPhase.SUSPENDED_USER
                or outcome.stop is not operation.pending_stop
            ):
                raise ValueError("MAIN user breakpoint is not confirmed")
            return RuntimeReply(
                RuntimeReplyKind.DEBUG_STOPPED,
                operation.command_id,
                OperationState.DEBUG_STOPPED,
                location=outcome.stop.location,
                changed_roots=record.changed_roots,
            )
        raise ValueError("Unrecognized MAIN yield")


@dataclass(frozen=True, slots=True)
class CapturePublicationRecord:
    """Source and root metadata for one cell within a confirmed CAPTURE stop."""

    scope: CaptureScope = field(repr=False)
    executed_source: MappedSource | None = field(default=None, repr=False)
    visible_source_context: VisibleSourceContext | None = field(default=None, repr=False)
    changed_roots: tuple[str, ...] = ()
    capture_dirty_roots: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CaptureRemoteOutcome:
    """Confirmed eval plus messages already collected by the RDBG owner."""

    evaluation: EvaluationResult = field(repr=False)
    messages: tuple[str, ...] = ()


class CaptureReplyPolicy:
    """Map a confirmed CAPTURE eval without changing the stopped frame."""

    def publish(
        self, outcome: CaptureRemoteOutcome, record: CapturePublicationRecord,
    ) -> RuntimeReply:
        from onec_runtime.runtime_models import OperationState, RuntimeReply, RuntimeReplyKind

        if not isinstance(outcome, CaptureRemoteOutcome):
            raise TypeError("CAPTURE publication requires a confirmed remote outcome")
        scope = record.scope
        if (
            scope.context_state is not CaptureContextState.READY
            or scope.frame_identity is not CaptureFrameIdentity.CONFIRMED
        ):
            raise ValueError("CAPTURE publication requires the confirmed stop")
        result = outcome.evaluation
        if not isinstance(result, EvaluationResult):
            raise TypeError("CAPTURE evaluation result is invalid")
        diagnostic = None
        if result.error_occurred:
            value = None
            error = _BSL_FAILURE
            diagnostic = _execution_diagnostic(
                result.error_text, record.executed_source,
                record.visible_source_context,
            )
        else:
            try:
                value = evaluation_to_python(result)
                error = ""
            except Exception:
                # A malformed confirmed scalar is an ordinary cell failure.
                # It cannot turn the stopped CAPTURE scope into remote unknown.
                value = None
                error = _CAPTURE_DECODE_FAILURE
        return RuntimeReply(
            RuntimeReplyKind.CAPTURE_CELL,
            scope.identity.main_command_id,
            OperationState.CAPTURED,
            result=value,
            error=error,
            succeeded=not error,
            messages=outcome.messages,
            changed_roots=record.changed_roots,
            capture_dirty_roots=record.capture_dirty_roots,
            diagnostic=diagnostic,
        )
