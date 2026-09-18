"""Per-root CAPTURE resume writeback through one worker-owned debugger port.

The caller owns the stopped frame and decides when to clean up or Continue.
This module only exports dirty roots to temporary storage and writes each
value into the original frame root. A pending/unknown remote command must be
reconciled by the RDBG owner before the ledger can be advanced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Protocol
from uuid import UUID

from onec_runtime.capture import (
    build_live_capture_root_transfer_call,
    build_temporary_storage_value_expression,
)
from onec_runtime.execution.evaluation import EvaluationPort, evaluate_until_result
from onec_runtime.rdbg.models import EvaluationResult, ModifyResult
from onec_runtime.table_value import evaluation_to_python


class RootWritePhase(str, Enum):
    UNATTEMPTED = "unattempted"
    EXPORT_PENDING = "export_pending"
    EXPORTED = "exported"
    MODIFY_SENT = "modify_sent"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class WritebackDisposition(str, Enum):
    IN_PROGRESS = "in_progress"
    READY = "ready"
    PAUSED_EXPORT_FAILED = "paused_export_failed"
    PARTIAL_WRITE = "partial_write"
    OUTCOME_UNKNOWN = "outcome_unknown"


class WritebackBlocked(RuntimeError):
    """A failed or unknown root requires explicit reconciliation first."""


class CaptureExportFailed(RuntimeError):
    """Export returned a confirmed BSL error or an unusable address."""

    def __init__(self, message: str, result: EvaluationResult) -> None:
        super().__init__(message)
        self.result = result


class CaptureModifyFailed(RuntimeError):
    """The frame-root modify returned a confirmed error."""

    def __init__(self, result: ModifyResult) -> None:
        super().__init__("Capture root write failed")
        self.result = result


class WritebackPort(EvaluationPort, Protocol):
    """A port bound to the current arbiter worker and stopped frame.

    ``modify`` must invoke ``on_transport_dispatch`` immediately before
    entering the transport. An exception after that callback has unknown
    outcome even if the HTTP request did not return a result.
    """

    def modify(
        self,
        variable: str,
        value_expression: str,
        *,
        on_transport_dispatch: Callable[[], None],
    ) -> ModifyResult: ...


@dataclass(frozen=True, slots=True)
class RootWriteRecord:
    root: str
    phase: RootWritePhase
    result_id: UUID | None = None
    failed_stage: str | None = None


@dataclass(slots=True)
class _RootState:
    phase: RootWritePhase = RootWritePhase.UNATTEMPTED
    address: str | None = field(default=None, repr=False)
    result_id: UUID | None = None
    failed_stage: str | None = None


class RootWritebackLedger:
    """Ordered, in-process recovery evidence for one CAPTURE resume attempt."""

    def __init__(self, dirty_roots: tuple[str, ...]) -> None:
        roots = tuple(dirty_roots)
        if len(roots) != len(set(roots)):
            raise ValueError("Dirty roots must be unique")
        for root in roots:
            build_live_capture_root_transfer_call(root)
        self.roots = roots
        self._states = {root: _RootState() for root in roots}

    def record(self, root: str) -> RootWriteRecord:
        state = self._states[root]
        return RootWriteRecord(root, state.phase, state.result_id, state.failed_stage)

    def retry_confirmed_export(self, root: str) -> None:
        """Allow an explicit new attempt after a *confirmed* export failure.

        The caller must first revalidate the same stopped frame. Already
        successful root writes remain in the ledger and are never replayed.
        Unknown evaluation outcomes and modify failures cannot use this path.
        """

        state = self._states[root]
        if state.phase is not RootWritePhase.FAILED or state.failed_stage != "export":
            raise WritebackBlocked("Only a confirmed export failure can be retried")
        if any(other.phase is RootWritePhase.UNKNOWN for other in self._states.values()):
            raise WritebackBlocked("Unknown writeback outcome requires reconciliation")
        state.phase = RootWritePhase.UNATTEMPTED
        state.failed_stage = None

    @property
    def disposition(self) -> WritebackDisposition:
        states = tuple(self._states.values())
        if any(state.phase is RootWritePhase.UNKNOWN for state in states):
            return WritebackDisposition.OUTCOME_UNKNOWN
        if all(state.phase is RootWritePhase.SUCCEEDED for state in states):
            return WritebackDisposition.READY
        if any(state.phase is RootWritePhase.MODIFY_SENT for state in states):
            return WritebackDisposition.OUTCOME_UNKNOWN
        if any(state.phase is RootWritePhase.FAILED for state in states):
            if any(
                state.phase is RootWritePhase.SUCCEEDED
                or state.failed_stage == "modify"
                for state in states
            ):
                return WritebackDisposition.PARTIAL_WRITE
            return WritebackDisposition.PAUSED_EXPORT_FAILED
        return WritebackDisposition.IN_PROGRESS


class CaptureWritebackExecutor:
    """Flush each dirty root once; reuse confirmed exports on safe retry."""

    def flush(
        self,
        port: WritebackPort,
        ledger: RootWritebackLedger,
        *,
        stack_level: int,
        poll_interval_s: float = 6.0,
    ) -> None:
        if ledger.disposition in {
            WritebackDisposition.OUTCOME_UNKNOWN,
            WritebackDisposition.PARTIAL_WRITE,
            WritebackDisposition.PAUSED_EXPORT_FAILED,
        }:
            raise WritebackBlocked("Capture writeback requires reconciliation")
        for root in ledger.roots:
            state = ledger._states[root]
            if state.phase is RootWritePhase.SUCCEEDED:
                continue
            if state.phase is RootWritePhase.UNATTEMPTED:
                state.phase = RootWritePhase.EXPORT_PENDING
                try:
                    result = evaluate_until_result(
                        port,
                        build_live_capture_root_transfer_call(root),
                        stack_level=stack_level,
                        wait_interval_s=poll_interval_s,
                    )
                except BaseException:
                    state.phase = RootWritePhase.UNKNOWN
                    raise
                if result.error_occurred:
                    state.phase = RootWritePhase.FAILED
                    state.failed_stage = "export"
                    raise CaptureExportFailed("Capture root export failed", result)
                try:
                    address = evaluation_to_python(result)
                except Exception:
                    address = None
                if result.type_name != "Строка" or not isinstance(address, str) or not address:
                    state.phase = RootWritePhase.FAILED
                    state.failed_stage = "export"
                    raise CaptureExportFailed("Capture root export address is invalid", result)
                state.address = address
                state.phase = RootWritePhase.EXPORTED
            if state.phase is not RootWritePhase.EXPORTED or state.address is None:
                raise WritebackBlocked("Capture writeback has no confirmed export")

            def mark_dispatched() -> None:
                state.phase = RootWritePhase.MODIFY_SENT

            expression = build_temporary_storage_value_expression(state.address)
            try:
                modified = port.modify(
                    root,
                    expression,
                    on_transport_dispatch=mark_dispatched,
                )
            except BaseException:
                if state.phase is RootWritePhase.MODIFY_SENT:
                    state.phase = RootWritePhase.UNKNOWN
                raise
            state.result_id = modified.result_id
            if modified.error_occurred:
                state.phase = RootWritePhase.FAILED
                state.failed_stage = "modify"
                raise CaptureModifyFailed(modified)
            state.phase = RootWritePhase.SUCCEEDED
