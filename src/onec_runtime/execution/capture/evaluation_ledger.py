"""Bounded public CAPTURE evaluation evidence, independent of RDBG transport."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from math import isfinite
from threading import Condition
from time import monotonic

from onec_runtime.capture_evaluation import (
    CaptureEvaluationKind,
    CaptureEvaluationOutcome,
    CaptureEvaluationState,
    CaptureEvaluationTiming,
    CaptureFailureDiagnostic,
    CapturePhase,
    CaptureStatus,
)
from onec_runtime.errors import ProtocolError, StaleCaptureError
from onec_runtime.execution.capture.scope import CaptureScope, CaptureStopIdentity


@dataclass(slots=True)
class _Record:
    evaluation_id: str
    kind: CaptureEvaluationKind
    created: float
    created_at: datetime
    dispatch_entered_ms: int | None = None
    rdbg_acknowledged_ms: int | None = None
    initiating_waiter_detached_ms: int | None = None
    last_poll_ms: int | None = None
    result_received_ms: int | None = None
    outcome_published_ms: int | None = None
    remote_step_count: int = 0
    poll_count: int = 0
    outcome: CaptureEvaluationOutcome | None = None
    unknown: bool = False
    failure: CaptureFailureDiagnostic | None = None


class CaptureEvaluationLedger:
    """Retain public evaluation snapshots for one exact CAPTURE stop.

    The owner calls lifecycle markers after its arbiter ticket has established
    each fact. This ledger never receives a session, ticket, pending RDBG
    capability, or expression. Local waits observe only the already-owned
    record and do not cancel or redispatch it.
    """

    def __init__(
        self,
        scope: CaptureScope,
        *,
        is_current: Callable[[], bool],
        history_limit: int = 32,
    ) -> None:
        if not isinstance(scope, CaptureScope):
            raise TypeError("CAPTURE scope is required")
        if not callable(is_current):
            raise TypeError("CAPTURE scope reader must be callable")
        if type(history_limit) is not int or not 1 <= history_limit <= 64:
            raise ValueError("CAPTURE evaluation history limit must be 1..64")
        self._scope = scope
        self._identity = scope.identity
        self._is_current = is_current
        self._history_limit = history_limit
        self._condition = Condition()
        self._records: OrderedDict[str, _Record] = OrderedDict()
        self._active_id: str | None = None
        self._last_id: str | None = None
        self._last_user_id: str | None = None
        self._resuming = False

    @property
    def identity(self) -> CaptureStopIdentity:
        """The exact stop fenced by this ledger."""

        return self._identity

    def notify_scope_changed(self) -> None:
        """Wake local observers after the controller replaces or closes the scope.

        The caller changes its own ``is_current`` predicate first. This method
        has no transport meaning and never attempts to terminate an evaluation.
        """

        with self._condition:
            self._condition.notify_all()

    def mark_resuming(self) -> None:
        """Record controller-confirmed resume admission without remote I/O."""

        with self._condition:
            self._require_current_locked()
            if self._active_id is not None:
                raise ProtocolError("CAPTURE evaluation prevents resume")
            self._resuming = True
            self._condition.notify_all()

    def discard_resuming(self) -> None:
        """Roll back a resume admission that never reached arbiter dispatch."""

        with self._condition:
            self._require_current_locked()
            if not self._resuming:
                raise ProtocolError("CAPTURE resume is not reserved")
            self._resuming = False
            self._condition.notify_all()

    def begin(self, receipt_id: str, kind: CaptureEvaluationKind) -> None:
        """Publish an admitted evaluation before its first remote effect."""

        if not isinstance(kind, CaptureEvaluationKind):
            raise TypeError("CAPTURE evaluation kind is invalid")
        with self._condition:
            self._require_current_locked()
            if not isinstance(receipt_id, str) or not receipt_id:
                raise ValueError("CAPTURE evaluation receipt ID is invalid")
            # The public models enforce the same bounded identifier grammar.
            CaptureEvaluationTiming(receipt_id, datetime.now(timezone.utc))
            if receipt_id in self._records:
                raise ProtocolError("CAPTURE evaluation receipt was already recorded")
            if self._active_id is not None:
                raise ProtocolError("CAPTURE evaluation is already active")
            now = monotonic()
            self._records[receipt_id] = _Record(
                receipt_id, kind, now, datetime.now(timezone.utc),
            )
            self._active_id = receipt_id
            self._last_id = receipt_id
            if kind is CaptureEvaluationKind.USER_BSL:
                self._last_user_id = receipt_id
            self._trim_locked()
            self._condition.notify_all()

    def discard_unstarted(self, receipt_id: str) -> None:
        """Roll back a local admission that never reached arbiter dispatch."""

        with self._condition:
            record = self._active_record_locked(receipt_id)
            if self._has_timing_evidence(record) or record.outcome is not None:
                raise ProtocolError("CAPTURE evaluation admission already has evidence")
            del self._records[receipt_id]
            self._active_id = None
            self._last_id = next(reversed(self._records), None)
            self._last_user_id = next(
                (candidate for candidate in reversed(self._records)
                 if self._records[candidate].kind is CaptureEvaluationKind.USER_BSL),
                None,
            )
            self._condition.notify_all()

    def note_dispatch(self, receipt_id: str) -> None:
        self._mark(receipt_id, "dispatch")

    def note_acknowledged(self, receipt_id: str) -> None:
        self._mark(receipt_id, "acknowledged")

    def note_poll(self, receipt_id: str) -> None:
        self._mark(receipt_id, "poll")

    def note_waiter_detached(self, receipt_id: str) -> None:
        self._mark(receipt_id, "detached")

    def complete(
        self, receipt_id: str, result: object = None, *, messages: tuple[str, ...] = (),
    ) -> CaptureEvaluationOutcome:
        """Record a confirmed successful outcome supplied by the ticket owner."""

        return self._settle(
            receipt_id,
            CaptureEvaluationState.COMPLETED,
            result=result,
            messages=messages,
        )

    def fail(
        self,
        receipt_id: str,
        error: str,
        *,
        messages: tuple[str, ...] = (),
        diagnostic: CaptureFailureDiagnostic | None = None,
    ) -> CaptureEvaluationOutcome:
        """Record a confirmed failed outcome using only public-safe fields."""

        if diagnostic is not None and not isinstance(diagnostic, CaptureFailureDiagnostic):
            raise TypeError("CAPTURE failure diagnostic is invalid")
        return self._settle(
            receipt_id,
            CaptureEvaluationState.FAILED,
            messages=messages,
            error=error,
            diagnostic=diagnostic,
        )

    def mark_unknown(
        self, receipt_id: str, diagnostic: CaptureFailureDiagnostic,
    ) -> CaptureEvaluationOutcome:
        """Keep an ambiguous remote evaluation selected until reconciliation."""

        if not isinstance(diagnostic, CaptureFailureDiagnostic):
            raise TypeError("CAPTURE failure diagnostic is invalid")
        with self._condition:
            record = self._active_record_locked(receipt_id)
            record.unknown = True
            record.failure = diagnostic
            outcome = self._outcome_locked(
                record, CaptureEvaluationState.UNKNOWN, diagnostic=diagnostic,
            )
            record.outcome = outcome
            self._condition.notify_all()
            return outcome

    def status(self) -> CaptureStatus:
        """Return a local snapshot; an old stop is explicitly stale."""

        with self._condition:
            if not self._current_locked():
                return CaptureStatus(
                    self._identity.main_command_id,
                    self._identity.runtime_generation,
                    self._identity.local_stop_sequence,
                    CapturePhase.STALE,
                )
            active = self._active_record_or_none_locked()
            if active is not None:
                if active.unknown:
                    return CaptureStatus(
                        self._identity.main_command_id,
                        self._identity.runtime_generation,
                        self._identity.local_stop_sequence,
                        CapturePhase.OUTCOME_UNKNOWN,
                        pending_evaluation_id=active.evaluation_id,
                        evaluation_kind=active.kind,
                        last_evaluation_id=self._last_id,
                        last_user_evaluation_id=self._last_user_id,
                        evaluation_timing=self._timing_locked(active),
                        failure=active.failure,
                    )
                return CaptureStatus(
                    self._identity.main_command_id,
                    self._identity.runtime_generation,
                    self._identity.local_stop_sequence,
                    CapturePhase.EVALUATING,
                    pending_evaluation_id=active.evaluation_id,
                    evaluation_kind=active.kind,
                    last_evaluation_id=self._last_id,
                    last_user_evaluation_id=self._last_user_id,
                    evaluation_timing=self._timing_locked(active),
                )
            last = None if self._last_id is None else self._records.get(self._last_id)
            return CaptureStatus(
                self._identity.main_command_id,
                self._identity.runtime_generation,
                self._identity.local_stop_sequence,
                CapturePhase.RESUMING if self._resuming else CapturePhase.PAUSED,
                last_evaluation_id=self._last_id,
                last_user_evaluation_id=self._last_user_id,
                evaluation_timing=None if last is None else self._timing_locked(last),
            )

    def wait(
        self, timeout_s: float | None = None, evaluation_id: str | None = None,
    ) -> CaptureEvaluationOutcome:
        """Observe the selected record without cancelling its arbiter ticket."""

        _validate_timeout(timeout_s)
        deadline = None if timeout_s is None else monotonic() + float(timeout_s)
        with self._condition:
            self._require_current_locked()
            record = self._select_locked(evaluation_id)
            while record.outcome is None:
                remaining = None if deadline is None else deadline - monotonic()
                if remaining is not None and remaining <= 0:
                    return self._outcome_locked(record, CaptureEvaluationState.PENDING)
                self._condition.wait(remaining)
                self._require_current_locked()
            return record.outcome

    def _mark(self, receipt_id: str, event: str) -> None:
        with self._condition:
            record = self._active_record_locked(receipt_id)
            elapsed = _elapsed_ms(record.created)
            if event == "dispatch":
                if record.dispatch_entered_ms is None:
                    record.dispatch_entered_ms = elapsed
                    record.remote_step_count += 1
            elif event == "acknowledged":
                if record.dispatch_entered_ms is None:
                    raise ProtocolError("CAPTURE evaluation was not dispatched")
                record.rdbg_acknowledged_ms = elapsed
            elif event == "poll":
                record.last_poll_ms = elapsed
                record.poll_count += 1
            elif event == "detached":
                record.initiating_waiter_detached_ms = elapsed
            else:
                raise RuntimeError("unknown CAPTURE evaluation ledger event")
            self._condition.notify_all()

    def _settle(
        self, receipt_id: str, state: CaptureEvaluationState, **payload: object,
    ) -> CaptureEvaluationOutcome:
        with self._condition:
            record = self._active_record_locked(receipt_id)
            has_timing_evidence = self._has_timing_evidence(record)
            if has_timing_evidence:
                record.result_received_ms = _elapsed_ms(record.created)
            outcome = self._outcome_locked(record, state, **payload)
            if has_timing_evidence:
                record.outcome_published_ms = _elapsed_ms(record.created)
            record.outcome = replace(outcome, timing=self._timing_locked(record))
            self._active_id = None
            self._trim_locked()
            self._condition.notify_all()
            return record.outcome

    def _outcome_locked(
        self, record: _Record, state: CaptureEvaluationState, **payload: object,
    ) -> CaptureEvaluationOutcome:
        return CaptureEvaluationOutcome(
            record.evaluation_id,
            record.kind,
            state,
            timing=self._timing_locked(record),
            **payload,
        )

    def _timing_locked(self, record: _Record) -> CaptureEvaluationTiming | None:
        if not self._has_timing_evidence(record):
            return None
        return CaptureEvaluationTiming(
            record.evaluation_id,
            record.created_at,
            elapsed_ms=_elapsed_ms(record.created),
            dispatch_entered_ms=record.dispatch_entered_ms,
            rdbg_acknowledged_ms=record.rdbg_acknowledged_ms,
            initiating_waiter_detached_ms=record.initiating_waiter_detached_ms,
            last_poll_ms=record.last_poll_ms,
            result_received_ms=record.result_received_ms,
            outcome_published_ms=record.outcome_published_ms,
            remote_step_count=record.remote_step_count,
            poll_count=record.poll_count,
        )

    @staticmethod
    def _has_timing_evidence(record: _Record) -> bool:
        return (
            record.dispatch_entered_ms is not None
            or record.rdbg_acknowledged_ms is not None
            or record.initiating_waiter_detached_ms is not None
            or record.last_poll_ms is not None
            or record.remote_step_count > 0
            or record.poll_count > 0
        )

    def _active_record_locked(self, receipt_id: str) -> _Record:
        self._require_current_locked()
        if receipt_id != self._active_id:
            raise ProtocolError("CAPTURE evaluation receipt is not active")
        record = self._records.get(receipt_id)
        if record is None:
            raise ProtocolError("CAPTURE evaluation receipt is unavailable")
        return record

    def _active_record_or_none_locked(self) -> _Record | None:
        return None if self._active_id is None else self._records.get(self._active_id)

    def _select_locked(self, evaluation_id: str | None) -> _Record:
        selected = evaluation_id or self._active_id or self._last_id
        if not isinstance(selected, str):
            raise ProtocolError("CAPTURE evaluation is unavailable")
        record = self._records.get(selected)
        if record is None:
            raise ProtocolError("CAPTURE evaluation is unavailable")
        return record

    def _trim_locked(self) -> None:
        while len(self._records) > self._history_limit:
            key, record = next(iter(self._records.items()))
            if key == self._active_id:
                raise ProtocolError("CAPTURE evaluation history is full")
            del self._records[key]
            if self._last_id == key:
                self._last_id = next(reversed(self._records), None)
            if self._last_user_id == key:
                self._last_user_id = next(
                    (candidate for candidate in reversed(self._records)
                     if self._records[candidate].kind is CaptureEvaluationKind.USER_BSL),
                    None,
                )

    def _current_locked(self) -> bool:
        scope = self._scope
        return self._is_current() and scope.identity == self._identity

    def _require_current_locked(self) -> None:
        if not self._current_locked():
            raise StaleCaptureError()


def _elapsed_ms(start: float) -> int:
    return max(0, int((monotonic() - start) * 1000))


def _validate_timeout(timeout_s: float | None) -> None:
    if timeout_s is None:
        return
    if (
        isinstance(timeout_s, bool)
        or not isinstance(timeout_s, (int, float))
        or not isfinite(float(timeout_s))
        or timeout_s < 0
    ):
        raise ValueError("CAPTURE wait timeout must be finite and non-negative")
