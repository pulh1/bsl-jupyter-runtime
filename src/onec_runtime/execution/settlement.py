"""Publish confirmed route outcomes without accessing RDBG or the legacy API.

The controller registers admission evidence before dispatch and calls the
route policy's ``settle`` method after the arbiter has correlated an outcome.
This service keeps one MAIN record through every stop of that command and a
separate record for each CAPTURE cell. Rejected preparation never reaches a
registration or namespace publication call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock
from typing import TYPE_CHECKING, Protocol

from onec_runtime.bsl.diagnostics import VisibleSourceContext
from onec_runtime.execution.capture.policy import CapturePreparedPayload
from onec_runtime.execution.capture.scope import CaptureScope
from onec_runtime.execution.controller.controller import MainYield, MainYieldKind
from onec_runtime.execution.main import MainOperation, MainPhase
from onec_runtime.execution.main.policy import MainPreparedPayload
from onec_runtime.execution.preparation import RoutePreparedStatement
from onec_runtime.execution.reply_publication import (
    CapturePublicationRecord, CaptureRemoteOutcome, CaptureReplyPolicy,
    MainConfirmedDecodeFailure, MainPublicationRecord, MainReplyPolicy,
)

if TYPE_CHECKING:
    from onec_runtime.runtime_api import CaptureCorrelationTicket, OperationState, RuntimeReply


class NamespacePublicationPort(Protocol):
    """Merge a confirmed full catalog into the owner-managed namespace.

    The implementation merges names case-insensitively and increments its
    snapshot version before a subsequent cell may prepare against the result.
    It performs no debugger calls.
    """

    def publish_additions(self, names: tuple[str, ...]) -> None: ...


@dataclass(frozen=True, slots=True)
class WorkerPublished:
    """Confirmed Worker-only activation from the owning arbiter plan."""

    handle: object = field(repr=False)
    operation_id: int
    state: OperationState


@dataclass(frozen=True, slots=True)
class _MainRegistration:
    payload: MainPreparedPayload = field(repr=False)
    record: MainPublicationRecord = field(repr=False)
    namespace_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _CaptureRegistration:
    payload: CapturePreparedPayload = field(repr=False)
    record: CapturePublicationRecord = field(repr=False)
    namespace_names: tuple[str, ...]


def _statement(payload: MainPreparedPayload | CapturePreparedPayload) -> RoutePreparedStatement | None:
    return payload.statement or payload.deferred_statement


def _visible(payload: MainPreparedPayload | CapturePreparedPayload) -> VisibleSourceContext:
    if payload.worker_intent is not None:
        return payload.worker_intent.method_set_candidate.visible_source_context
    return VisibleSourceContext({
        payload.common.source_unit: payload.common.parsed_units.visible.text,
    })


def _names(statement: RoutePreparedStatement) -> tuple[str, ...]:
    names = statement.lowering.context_names
    if not isinstance(names, tuple) or any(not isinstance(name, str) or not name for name in names):
        raise TypeError("Prepared namespace names are invalid")
    return names


class RouteSettlementService:
    """The route policies' settlement port for existing ``RuntimeReply`` values.

    ``register_main`` is called after creating the command operation and before
    its command write. The record remains until terminal completion
    or explicit discard after proven loss. ``register_capture`` is called for
    each admitted CAPTURE cell and is consumed by its confirmed eval outcome.
    The service never reads RDBG; CAPTURE messages arrive in
    ``CaptureRemoteOutcome`` from the arbiter's session port.
    """

    def __init__(self, namespace: NamespacePublicationPort) -> None:
        self._namespace = namespace
        self._main: dict[int, _MainRegistration] = {}
        self._capture: dict[int, _CaptureRegistration] = {}
        self._main_policy = MainReplyPolicy()
        self._capture_policy = CaptureReplyPolicy()
        self._lock = RLock()

    def register_main(
        self,
        operation: MainOperation,
        payload: MainPreparedPayload,
        *,
        prior_capture_sequence: int,
        capture_ticket: CaptureCorrelationTicket | None = None,
    ) -> None:
        """Retain source, message key and stop baseline for one MAIN command."""

        statement = _statement(payload)
        if statement is None:
            raise ValueError("A MAIN command requires a prepared statement")
        if prior_capture_sequence < 0:
            raise ValueError("Prior CAPTURE sequence must be non-negative")
        if operation.message_collector_key != statement.message_collector_key:
            raise ValueError("MAIN message key changed before publication")
        record = MainPublicationRecord(
            operation,
            prior_capture_sequence=prior_capture_sequence,
            executed_source=statement.lowering.mapped_source,
            visible_source_context=_visible(payload),
            changed_roots=statement.lowering.persistent_write_roots,
            capture_ticket=capture_ticket,
            message_collector_key=statement.message_collector_key,
        )
        registration = _MainRegistration(payload, record, _names(statement))
        with self._lock:
            if operation.command_id in self._main:
                raise ValueError("MAIN command already has a publication record")
            self._main[operation.command_id] = registration

    def register_capture(
        self, scope: CaptureScope, payload: CapturePreparedPayload,
        *, base_namespace_names: tuple[str, ...] = (),
    ) -> None:
        """Retain per-cell source evidence within the confirmed stop."""

        statement = _statement(payload)
        if statement is None:
            raise ValueError("A CAPTURE eval requires a prepared statement")
        if type(base_namespace_names) is not tuple or any(
            not isinstance(name, str) or not name
            for name in base_namespace_names
        ):
            raise TypeError("CAPTURE base namespace is invalid")
        record = CapturePublicationRecord(
            scope,
            executed_source=statement.lowering.mapped_source,
            visible_source_context=_visible(payload),
            changed_roots=statement.lowering.persistent_write_roots,
            capture_dirty_roots=payload.dirty_roots,
        )
        base = {name.casefold() for name in base_namespace_names}
        additions = tuple(
            name for name in _names(statement) if name.casefold() not in base
        )
        registration = _CaptureRegistration(payload, record, additions)
        with self._lock:
            prior = self._capture.get(id(payload))
            if prior is not None:
                if prior.payload is payload and prior.record.scope is not scope:
                    raise ValueError("CAPTURE payload belongs to another scope")
                raise ValueError("CAPTURE cell already has a publication record")
            self._capture[id(payload)] = registration

    def pending_main_names(self, operation: MainOperation) -> tuple[str, ...]:
        """Return speculative names for CAPTURE preparation at this MAIN stop.

        These names are never published by this read. They may include names
        from source after the current stop, so callers use them only as an
        isolated lowering catalog while this exact MAIN command is suspended.
        """

        if operation.phase not in {
            MainPhase.SUSPENDED_CAPTURE, MainPhase.SUSPENDED_USER,
        }:
            return ()
        with self._lock:
            registration = self._main.get(operation.command_id)
            if (
                registration is None
                or registration.record.operation is not operation
            ):
                return ()
            return registration.namespace_names

    def settle_main(self, outcome: object, payload: MainPreparedPayload) -> RuntimeReply:
        """Publish a MAIN yield; only terminal success publishes namespace names."""

        if _statement(payload) is None:
            return self._worker_only_reply(outcome, payload.worker_intent is not None)
        if not isinstance(outcome, (MainYield, MainConfirmedDecodeFailure)):
            raise TypeError("MAIN settlement requires a confirmed MAIN outcome")
        with self._lock:
            registration = self._main.get(outcome.operation.command_id)
            if registration is None or registration.payload is not payload:
                raise ValueError("MAIN yield has no matching publication record")
        reply = self._main_policy.publish(outcome, registration.record)
        if (
            isinstance(outcome, MainConfirmedDecodeFailure)
            or outcome.kind is MainYieldKind.COMPLETED
        ):
            if reply.succeeded:
                self._namespace.publish_additions(registration.namespace_names)
            with self._lock:
                if self._main.get(outcome.operation.command_id) is registration:
                    del self._main[outcome.operation.command_id]
        return reply

    def settle_capture(
        self, outcome: object, payload: CapturePreparedPayload,
    ) -> RuntimeReply:
        """Publish one confirmed eval; a failed cell leaves its scope ready."""

        if _statement(payload) is None:
            return self._worker_only_reply(outcome, payload.worker_intent is not None)
        if not isinstance(outcome, CaptureRemoteOutcome):
            raise TypeError("CAPTURE settlement requires CaptureRemoteOutcome")
        with self._lock:
            registration = self._capture.get(id(payload))
            if registration is None or registration.payload is not payload:
                raise ValueError("CAPTURE cell has no matching publication record")
        reply = self._capture_policy.publish(outcome, registration.record)
        if reply.succeeded:
            self._namespace.publish_additions(registration.namespace_names)
        with self._lock:
            if self._capture.get(id(payload)) is registration:
                del self._capture[id(payload)]
        return reply

    def discard_main(self, operation: MainOperation) -> None:
        """Release an unpublished command only after confirmed retirement."""

        with self._lock:
            registration = self._main.get(operation.command_id)
            if registration is not None and registration.record.operation is operation:
                del self._main[operation.command_id]

    def discard_capture(self, payload: CapturePreparedPayload) -> None:
        """Retire a cell after proven pre-dispatch rejection or session loss.

        Never discard an evaluation merely because its remote outcome is
        unknown; its record still owns source and result correlation.
        """

        with self._lock:
            registration = self._capture.get(id(payload))
            if registration is not None and registration.payload is payload:
                del self._capture[id(payload)]

    @staticmethod
    def _worker_only_reply(outcome: object, has_intent: bool) -> RuntimeReply:
        from onec_runtime.runtime_api import OperationState, RuntimeReply, RuntimeReplyKind

        if not has_intent or not isinstance(outcome, WorkerPublished):
            raise TypeError("Worker-only settlement requires WorkerPublished")
        if (
            outcome.handle is None
            or isinstance(outcome.operation_id, bool)
            or not isinstance(outcome.operation_id, int)
            or outcome.operation_id < 0
            or not isinstance(outcome.state, OperationState)
        ):
            raise ValueError("Worker publication evidence is invalid")
        return RuntimeReply(
            RuntimeReplyKind.WORKER_LOADED,
            outcome.operation_id,
            outcome.state,
            result=outcome.handle,
        )
