"""Public cell entry points backed by one controller and one RDBG arbiter.

This adapter deliberately exposes only operations that have a complete
single-owner path. It does not forward missing methods to the old RuntimeApi:
doing so would create a second reader or writer of the debugger session.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, contextmanager, nullcontext
from math import isfinite
from threading import Thread, local
from typing import Callable, Iterator, Mapping, Protocol
from uuid import UUID

import pandas as pd

from onec_runtime.bsl.source_maps import SourceUnitRef, source_sha256
from onec_runtime.capture_evaluation import CapturePhase
from onec_runtime.capture_inspection import CaptureView
from onec_runtime.errors import NoActiveCaptureError, ProtocolError
from onec_runtime.execution.arbiter import ExecutionTicket, RdbgArbiter
from onec_runtime.execution.contracts import PreparedCell
from onec_runtime.execution.capture.public_inspection import (
    CaptureInspection, CaptureInspectionBridge, CaptureSourceResolver,
)
from onec_runtime.execution.capture.session_inspection_adapter import (
    SessionCaptureInspectionAdapter,
)
from onec_runtime.execution.capture.writeback import (
    CaptureExportFailed, CaptureModifyFailed,
)
from onec_runtime.execution.completion_fields import CompletionFieldsService
from onec_runtime.execution.controller.controller import ExecutionController
from onec_runtime.execution.pipeline import CellExecutionPipeline
from onec_runtime.execution.session_value_adapter import SessionValueMaterializationAdapter
from onec_runtime.execution.source_identity import NotebookSourceIdentityFactory
from onec_runtime.execution.value_reference import validate_public_direct_handle
from onec_runtime.execution.worker_breakpoint_service import WorkerBreakpointService
from onec_runtime.execution.worker_activation import WorkerMaterializationSnapshot
from onec_runtime.prototype_runtime import PartialWritebackError
from onec_runtime.runtime_contracts import OperationExecutionProvenance
from onec_runtime.rdbg.models import ModuleLocation
from onec_runtime.table_materialization import ReferencePolicy
from onec_runtime.value_materialization import MaterializationOptions
from onec_runtime.worker_breakpoints import WorkerBreakpointStatus


class ValueTransferPort(Protocol):
    """Stopped-route value operations owned by one materialization router."""

    def materialize_value(
        self, handle: str, options: MaterializationOptions | None = None,
    ) -> object: ...

    def to_df(
        self, handle: str, policy: ReferencePolicy | None = None,
        *, max_rows: int, max_bytes: int,
    ) -> pd.DataFrame: ...

    def materialize(
        self, handle: str, options: MaterializationOptions | None = None,
        *, table_policy: ReferencePolicy | None = None,
    ) -> object: ...

    def head_to_df(
        self, handle: str, count: int,
        *, policy: ReferencePolicy | None = None, max_bytes: int,
    ) -> pd.DataFrame: ...


class PublicExecutionFacade:
    """RuntimeSession's narrow execution API over the new ownership model.

    The composition root provides the source identity, status projection and
    optional admitted-artifact provenance reader. The latter runs after a
    ticket is accepted; the controller may already have dispatched the ticket.
    No Session admission lock is released during preparation or submission.
    """

    def __init__(
        self,
        pipeline: CellExecutionPipeline,
        controller: ExecutionController,
        arbiter: RdbgArbiter,
        *,
        source_unit_factory: Callable[[str], SourceUnitRef],
        status_reader: Callable[[], object],
        source_identity: NotebookSourceIdentityFactory | None = None,
        namespace_reader: Callable[[], object] | None = None,
        worker_catalog_snapshot: (
            Callable[[], WorkerMaterializationSnapshot] | None
        ) = None,
        resolve_capture_sources: CaptureSourceResolver | None = None,
        value_router: ValueTransferPort | None = None,
        value_router_factory: (
            Callable[
                [Callable[[], AbstractContextManager[None]]], ValueTransferPort
            ] | None
        ) = None,
        provenance_reader: (
            Callable[[PreparedCell], OperationExecutionProvenance] | None
        ) = None,
    ) -> None:
        if not callable(source_unit_factory) or not callable(status_reader):
            raise TypeError("source unit and status readers must be callable")
        if provenance_reader is not None and not callable(provenance_reader):
            raise TypeError("provenance reader must be callable")
        if namespace_reader is not None and not callable(namespace_reader):
            raise TypeError("namespace reader must be callable")
        if worker_catalog_snapshot is not None and not callable(worker_catalog_snapshot):
            raise TypeError("completion Worker catalog reader must be callable")
        if worker_catalog_snapshot is not None and namespace_reader is None:
            raise TypeError("completion fields require a namespace reader")
        if resolve_capture_sources is not None and not callable(resolve_capture_sources):
            raise TypeError("CAPTURE source resolver must be callable")
        if source_identity is not None and not isinstance(
            source_identity, NotebookSourceIdentityFactory
        ):
            raise TypeError("notebook source identity owner is invalid")
        if value_router is not None and value_router_factory is not None:
            raise TypeError("value route must have one owner")
        if value_router_factory is not None and not callable(value_router_factory):
            raise TypeError("value route factory is invalid")
        self._pipeline = pipeline
        self._controller = controller
        self._arbiter = arbiter
        self._source_unit_factory = source_unit_factory
        self._source_identity = source_identity
        self._status_reader = status_reader
        self._namespace_reader = namespace_reader
        self._completion_fields = (
            CompletionFieldsService(
                controller,
                namespace_snapshot=namespace_reader,
                worker_catalog_snapshot=worker_catalog_snapshot,
                wait_handoff=self._wait_handoff,
            )
            if worker_catalog_snapshot is not None else None
        )
        self._provenance_reader = provenance_reader
        self._caller_handoff = local()
        self._value_router = (
            value_router_factory(self._wait_handoff)
            if value_router_factory is not None else value_router
        )
        self._session_value_adapter = (
            SessionValueMaterializationAdapter(self._value_router)
            if self._value_router is not None else None
        )
        self._capture_inspection = CaptureInspectionBridge(
            controller, wait_handoff=self._wait_handoff,
            resolve_sources=resolve_capture_sources,
        )
        self._session_capture_inspection = SessionCaptureInspectionAdapter(
            controller, self._capture_inspection, wait_handoff=self._wait_handoff,
        )
        self._worker_breakpoint_service: WorkerBreakpointService | None = None

    def bind_worker_breakpoint_service(self, service: WorkerBreakpointService) -> None:
        """Bind one shared breakpoint owner during fresh runtime composition."""

        if not isinstance(service, WorkerBreakpointService):
            raise TypeError("Worker breakpoint service is required")
        if service.arbiter is not self._arbiter:
            raise ProtocolError("Worker breakpoints require this runtime's RDBG owner")
        if self._worker_breakpoint_service is not None:
            raise ProtocolError("Worker breakpoint service is already bound")
        self._worker_breakpoint_service = service

    @contextmanager
    def execution_caller_handoff(
        self,
        factory: Callable[[], AbstractContextManager[None]],
    ) -> Iterator[None]:
        """Bind this caller's Session-lock release for blocking ticket waits."""

        if not callable(factory):
            raise TypeError("execution caller handoff factory must be callable")
        if getattr(self._caller_handoff, "factory", None) is not None:
            raise ProtocolError("execution caller handoff is already bound")
        self._caller_handoff.factory = factory
        try:
            yield
        finally:
            del self._caller_handoff.factory

    def _wait_handoff(self) -> AbstractContextManager[None]:
        factory = getattr(self._caller_handoff, "factory", None)
        return nullcontext() if factory is None else factory()

    def execute_bsl(
        self,
        source: str,
        *,
        source_unit: SourceUnitRef | None = None,
        on_execution_provenance: (
            Callable[[OperationExecutionProvenance], None] | None
        ) = None,
    ) -> object:
        """Execute one notebook BSL cell on the controller-selected route."""

        if not isinstance(source, str) or not source.strip():
            raise ProtocolError("BSL cell is empty")
        if on_execution_provenance is not None and not callable(on_execution_provenance):
            raise TypeError("execution provenance callback must be callable")
        if on_execution_provenance is not None and self._provenance_reader is None:
            raise ProtocolError("execution provenance publication is not configured")
        visible_unit = (
            self._source_identity.next_unit(source, explicit=source_unit)
            if self._source_identity is not None
            else source_unit or self._source_unit_factory(source)
        )
        if not isinstance(visible_unit, SourceUnitRef):
            raise TypeError("source unit factory must return SourceUnitRef")
        if visible_unit.source_sha256 != source_sha256(source):
            raise ProtocolError("notebook source identity does not match cell text")

        prepared_evidence: tuple[PreparedCell, OperationExecutionProvenance] | None = None

        def read_prepared(prepared: PreparedCell) -> None:
            nonlocal prepared_evidence
            reader = self._provenance_reader
            assert reader is not None
            provenance = reader(prepared)
            if not isinstance(provenance, OperationExecutionProvenance):
                raise TypeError("provenance reader returned an invalid record")
            prepared_evidence = prepared, provenance

        def publish_admitted(prepared: PreparedCell) -> None:
            assert on_execution_provenance is not None
            if prepared_evidence is None or prepared_evidence[0] is not prepared:
                raise ProtocolError("admitted provenance has another prepared cell")
            on_execution_provenance(prepared_evidence[1])

        return self._pipeline.execute(
            source, visible_unit,
            wait_handoff=self._wait_handoff,
            on_prepared=(
                read_prepared if on_execution_provenance is not None else None
            ),
            on_admitted=(
                publish_admitted if on_execution_provenance is not None else None
            ),
        )

    def resume_capture(
        self,
        *,
        dirty_roots: tuple[str, ...] = (),
        successor_locations: tuple[ModuleLocation, ...] | None = None,
        continuation_attempt_id: str | None = None,
        timeout_s: float | None = None,
        on_completion: Callable[[object | None, BaseException | None], None] | None = None,
        on_detached_completion: (
            Callable[[object | None, BaseException | None], None] | None
        ) = None,
    ) -> object:
        """Continue the current CAPTURE stop without losing the owning ticket.

        Dirty roots and successor points are admitted in the same controller
        ticket before writeback and Continue. A successor admission binds its
        attempt ID and next-stop correlation ticket; the caller commits it
        only after the confirmed resume outcome.
        ``timeout_s`` limits this caller's wait, never remote BSL execution.
        """

        _validate_wait_timeout(timeout_s)
        submission = {
            "dirty_roots": dirty_roots,
            "successor_locations": successor_locations,
        }
        if continuation_attempt_id is not None:
            submission["continuation_attempt_id"] = continuation_attempt_id
        ticket = self._controller.submit_resume(**submission)
        try:
            return self._wait_for_reply(
                ticket, timeout_s=timeout_s,
                on_completion=_public_resume_completion(on_completion),
                on_detached_completion=(
                    _public_resume_completion(on_detached_completion)
                ),
            )
        except (CaptureExportFailed, CaptureModifyFailed):
            # These results are confirmed root failures. Keep their raw RDBG
            # models private while preserving the MCP continuation contract.
            raise _public_resume_error() from None

    def resume_debug_stop(self, *, timeout_s: float | None = None) -> object:
        """Continue a user breakpoint in its existing MAIN operation."""

        _validate_wait_timeout(timeout_s)
        ticket = self._controller.submit_resume_debug_stop()
        return self._wait_for_reply(ticket, timeout_s=timeout_s)

    def status(self) -> object:
        """Read a local runtime projection without entering RDBG."""

        return self._status_reader()

    def namespace_snapshot(self) -> object:
        """Read confirmed names and the proxy generation fence locally."""

        reader = self._namespace_reader
        if reader is None:
            raise ProtocolError("runtime namespace reader is not configured")
        return reader()

    def completion_fields(
        self, handle: str, *, table_row: bool = False, timeout_s: float = 1.0,
    ) -> tuple[str, ...]:
        """Read bounded field names through the current controller route."""

        service = self._completion_fields
        if service is None:
            raise ProtocolError("completion fields route is not configured")
        return service.completion_fields(
            handle, table_row=table_row, timeout_s=timeout_s,
        )

    def materialize_value(
        self, handle: str, options: MaterializationOptions | None = None,
    ) -> object:
        """Decode a bounded direct Context value through the current route."""

        router = self._value_router
        if router is None:
            raise ProtocolError("value transfer route is not configured")
        return router.materialize_value(handle, options)

    def materialize_session_value(self, handle: str, **options: object) -> object:
        """Apply RuntimeSession's value options to the current value route."""

        adapter = self._session_value_adapter
        if adapter is None:
            raise ProtocolError("value transfer route is not configured")
        return adapter.materialize_value(handle, **options)

    def materialize_session_table(self, handle: str, **options: object) -> pd.DataFrame:
        """Apply RuntimeSession's table options to the current value route."""

        adapter = self._session_value_adapter
        if adapter is None:
            raise ProtocolError("value transfer route is not configured")
        return adapter.materialize_table(handle, **options)

    def validate_value_reference(self, handle: str) -> str:
        """Validate a direct public Context handle without reading target data."""

        return validate_public_direct_handle(handle)

    def to_df(
        self,
        handle: str,
        policy: ReferencePolicy | None = None,
        *,
        max_rows: int,
        max_bytes: int = 64 * 1024 * 1024,
    ) -> pd.DataFrame:
        """Decode a bounded direct Context table through the current route."""

        router = self._value_router
        if router is None:
            raise ProtocolError("value transfer route is not configured")
        return router.to_df(handle, policy, max_rows=max_rows, max_bytes=max_bytes)

    def capture_inspection(self) -> CaptureInspection:
        """Return stack, frame, and context handles for the current stop."""

        return self._capture_inspection.current()

    def capture_stack(
        self, *, cursor: int, limit: int, timeout_s: float | None = None,
    ) -> Mapping[str, object]:
        """Page safe native stack metadata for RuntimeSession's capture fence."""

        return self._session_capture_inspection.capture_stack(
            cursor=cursor, limit=limit, timeout_s=timeout_s,
        )

    def capture_frame_variables(
        self, *, filters: Mapping[str, object], cursor: int, limit: int,
        timeout_s: float | None = None,
    ) -> Mapping[str, object]:
        """Page saved root-frame names and types for a fenced Session caller."""

        return self._session_capture_inspection.capture_frame_variables(
            filters=filters, cursor=cursor, limit=limit, timeout_s=timeout_s,
        )

    def capture_frame(
        self, *, level: int, cursor: int, limit: int,
        name: str | None = None, timeout_s: float | None = None,
    ) -> Mapping[str, object]:
        """Page safe frame metadata for RuntimeSession's capture fence."""

        return self._session_capture_inspection.capture_frame(
            level=level, cursor=cursor, limit=limit,
            name=name, timeout_s=timeout_s,
        )

    def current_capture(self) -> CaptureView:
        """Return the established CaptureView contract over controller evidence."""

        if self._controller.capture_scope is None:
            raise NoActiveCaptureError()
        inspection = self._capture_inspection.current()
        ledger = self._controller.capture_evaluation_ledger()
        identity = ledger.identity
        return CaptureView(
            identity.main_command_id,
            identity.runtime_generation,
            identity.local_stop_sequence,
            lambda: ledger.status().phase is not CapturePhase.STALE,
            ledger.status,
            lambda timeout_s, evaluation_id: ledger.wait(timeout_s, evaluation_id),
            inspection.stack,
            inspection.context,
        )

    def invalidate_capture_inspection(self) -> None:
        """Revoke frame and value handles after uncertain local preparation."""

        self._controller.invalidate_capture_inspection()

    def materialize(
        self,
        handle: str,
        options: MaterializationOptions | None = None,
        *,
        table_policy: ReferencePolicy | None = None,
    ) -> object:
        """Decode a bounded value or table using the current stopped route."""

        router = self._value_router
        if router is None:
            raise ProtocolError("value transfer route is not configured")
        return router.materialize(handle, options, table_policy=table_policy)

    def head_to_df(
        self,
        handle: str,
        count: int,
        *,
        policy: ReferencePolicy | None = None,
        max_bytes: int = 64 * 1024 * 1024,
    ) -> pd.DataFrame:
        """Copy at most ``count`` leading rows through the current route."""

        router = self._value_router
        if router is None:
            raise ProtocolError("value transfer route is not configured")
        return router.head_to_df(handle, count, policy=policy, max_bytes=max_bytes)

    def configure_capture_points(
        self, locations: tuple[ModuleLocation, ...],
    ) -> None:
        """Configure idle capture locations through the controller owner."""

        self._controller.configure_capture_points(locations)

    def configure_continuation_capture_points(
        self, locations: tuple[ModuleLocation, ...],
    ) -> None:
        """Rearm the same stopped CAPTURE target through the arbiter."""

        self._controller.configure_continuation_capture_points(locations)

    def begin_continuation_admission(
        self, spec: object, locations: tuple[ModuleLocation, ...],
    ) -> object:
        """Plan one successor of the live MAIN command without RDBG I/O."""

        return self._controller.begin_continuation_admission(spec, locations)

    def continuation_attempt_evidence(self, attempt_id: str) -> object:
        """Read the controller's ordered writeback and Continue evidence."""

        return self._controller.continuation_attempt_evidence(attempt_id)

    @staticmethod
    def continuation_admission_is_uncertain() -> bool:
        """Local successor planning has no transport-ambiguous effect."""

        return False

    def prepare_capture_ticket(self):
        """Reserve opaque evidence for the next admitted MAIN capture stop."""

        return self._controller.prepare_capture_ticket()

    def add_worker_breakpoint(
        self,
        source_unit: SourceUnitRef,
        canonical_module: str,
        line: int,
        *, enabled: bool = True,
        column: int | None = None,
    ) -> WorkerBreakpointStatus:
        """Install one logical Worker breakpoint through the shared owner."""

        return self._require_worker_breakpoint_service().add_worker_breakpoint(
            source_unit, canonical_module, line, enabled=enabled, column=column,
        )

    def remove_worker_breakpoint(self, breakpoint_id: UUID) -> None:
        """Remove one logical Worker breakpoint at a stable route boundary."""

        self._require_worker_breakpoint_service().remove_worker_breakpoint(
            breakpoint_id,
        )

    def set_worker_breakpoint_enabled(
        self, breakpoint_id: UUID, enabled: bool,
    ) -> WorkerBreakpointStatus:
        """Set the enabled state and install the resulting shared workspace."""

        return self._require_worker_breakpoint_service().set_worker_breakpoint_enabled(
            breakpoint_id, enabled,
        )

    def worker_breakpoint_status(
        self, breakpoint_id: UUID,
    ) -> WorkerBreakpointStatus:
        """Read a confirmed logical Worker breakpoint without RDBG I/O."""

        return self._require_worker_breakpoint_service().worker_breakpoint_status(
            breakpoint_id,
        )

    def list_worker_breakpoints(self) -> tuple[WorkerBreakpointStatus, ...]:
        """Read the confirmed logical Worker breakpoint catalog locally."""

        return self._require_worker_breakpoint_service().list_worker_breakpoints()

    def _require_worker_breakpoint_service(self) -> WorkerBreakpointService:
        service = self._worker_breakpoint_service
        if service is None:
            raise ProtocolError("Worker breakpoint service is not configured")
        return service

    def try_heartbeat_ticket(self) -> ExecutionTicket | None:
        """Queue a keepalive only while the single RDBG owner is idle."""

        return self._arbiter.try_heartbeat()

    def owns_debug_ui_stream(self) -> bool:
        """Tell RuntimeSession that it must never issue a direct RDBG keepalive."""

        return True

    def close(self) -> None:
        """Close local RDBG ownership only after every remote ticket retires.

        ArbiterBusy is deliberately propagated. Closing the owner while its
        target may still be executing would orphan its event stream.
        """

        self._arbiter.close(timeout=3.0)

    def _wait_for_reply(
        self,
        ticket: ExecutionTicket,
        *,
        timeout_s: float | None,
        on_completion: Callable[[object | None, BaseException | None], None] | None = None,
        on_detached_completion: (
            Callable[[object | None, BaseException | None], None] | None
        ) = None,
    ) -> object:
        try:
            with self._wait_handoff():
                reply = ticket.wait_initiator(timeout=timeout_s)
        except TimeoutError:
            # A finalizer can itself fail with TimeoutError. Inspect the
            # ticket before treating that exception as a local wait interval.
            if ticket.status().settled:
                try:
                    reply = ticket.wait_settled(timeout=0)
                except BaseException as error:
                    if on_completion is not None:
                        on_completion(None, error)
                    raise
                if on_completion is not None:
                    on_completion(reply, None)
                return reply
            ticket.detach_waiter()
            if on_detached_completion is not None:
                Thread(
                    target=_publish_detached,
                    args=(ticket, on_detached_completion),
                    name="onec-runtime-detached-execution",
                    daemon=True,
                ).start()
            raise
        except KeyboardInterrupt:
            ticket.detach_waiter()
            if on_detached_completion is not None:
                Thread(
                    target=_publish_detached,
                    args=(ticket, on_detached_completion),
                    name="onec-runtime-detached-execution",
                    daemon=True,
                ).start()
            raise
        except BaseException as error:
            if on_completion is not None:
                on_completion(None, error)
            raise
        if on_completion is not None:
            on_completion(reply, None)
        return reply


def _publish_detached(
    ticket: ExecutionTicket,
    callback: Callable[[object | None, BaseException | None], None],
) -> None:
    try:
        reply = ticket.wait_settled()
    except BaseException as error:
        callback(None, error)
    else:
        callback(reply, None)


def _public_resume_error() -> PartialWritebackError:
    return PartialWritebackError("CAPTURE root writeback was rejected")


def _public_resume_completion(
    callback: Callable[[object | None, BaseException | None], None] | None,
) -> Callable[[object | None, BaseException | None], None] | None:
    if callback is None:
        return None

    def publish(reply: object | None, error: BaseException | None) -> None:
        if isinstance(error, (CaptureExportFailed, CaptureModifyFailed)):
            error = _public_resume_error()
        callback(reply, error)

    return publish


def _validate_wait_timeout(timeout_s: float | None) -> None:
    if timeout_s is None:
        return
    if (
        isinstance(timeout_s, bool)
        or not isinstance(timeout_s, (int, float))
        or not isfinite(float(timeout_s))
        or timeout_s < 0
    ):
        raise ProtocolError("execution wait timeout must be finite and non-negative")
