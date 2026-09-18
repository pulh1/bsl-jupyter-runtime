"""Public cell entry points backed by one controller and one RDBG arbiter.

This adapter deliberately exposes only operations that have a complete
single-owner path. It does not forward missing methods to the old RuntimeApi:
doing so would create a second reader or writer of the debugger session.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, contextmanager, nullcontext
from math import isfinite
from threading import Thread, local
from typing import Callable, Iterator, Protocol

import pandas as pd

from onec_runtime.bsl.source_maps import SourceUnitRef, source_sha256
from onec_runtime.capture_evaluation import CapturePhase
from onec_runtime.capture_inspection import CaptureView
from onec_runtime.errors import ProtocolError
from onec_runtime.execution.arbiter import ExecutionTicket, RdbgArbiter
from onec_runtime.execution.contracts import PreparedCell
from onec_runtime.execution.capture.public_inspection import (
    CaptureInspection, CaptureInspectionBridge,
)
from onec_runtime.execution.controller.controller import ExecutionController
from onec_runtime.execution.pipeline import CellExecutionPipeline
from onec_runtime.execution.source_identity import NotebookSourceIdentityFactory
from onec_runtime.execution.value_reference import validate_public_direct_handle
from onec_runtime.runtime_contracts import OperationExecutionProvenance
from onec_runtime.rdbg.models import ModuleLocation
from onec_runtime.table_materialization import ReferencePolicy
from onec_runtime.value_materialization import MaterializationOptions


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
        self._provenance_reader = provenance_reader
        self._caller_handoff = local()
        self._value_router = (
            value_router_factory(self._wait_handoff)
            if value_router_factory is not None else value_router
        )
        self._capture_inspection = CaptureInspectionBridge(
            controller, wait_handoff=self._wait_handoff,
        )

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
        ticket before writeback and Continue. Continuation attempt IDs still
        require their public evidence binding before dispatch.
        ``timeout_s`` limits this caller's wait, never remote BSL execution.
        """

        if continuation_attempt_id is not None:
            raise ProtocolError("continuation attempt admission is not configured")
        _validate_wait_timeout(timeout_s)
        ticket = self._controller.submit_resume(
            dirty_roots=dirty_roots,
            successor_locations=successor_locations,
        )
        return self._wait_for_reply(
            ticket, timeout_s=timeout_s,
            on_completion=on_completion,
            on_detached_completion=on_detached_completion,
        )

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

    def materialize_value(
        self, handle: str, options: MaterializationOptions | None = None,
    ) -> object:
        """Decode a bounded direct Context value through the current route."""

        router = self._value_router
        if router is None:
            raise ProtocolError("value transfer route is not configured")
        return router.materialize_value(handle, options)

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

    def current_capture(self) -> CaptureView:
        """Return the established CaptureView contract over controller evidence."""

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
