"""Direct Context value/table materialization over a CAPTURE ticket.

The existing value and compact-table transfer builders create validated BSL,
admission envelopes, integrity checks, cleanup instructions and decoders.
Only their CAPTURE executor path is used here. The supplied data plane sends
the plan through ``ExecutionController.submit_capture_materialization``; this
module cannot issue an RDBG command or read temporary storage directly.

This policy supports direct/dotted ``e1cRuntimeКонтекст.<identifier>`` handles. Bind
through ``bind_capture_ticket_materialization`` to recheck the Worker catalog
inside the admitted ticket. The value router sends deferred ``capture_table_*``
handles through the controller's separate selected-table request.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from typing import Protocol

import pandas as pd

from onec_runtime.capture_evaluation import CaptureEvaluationKind, CaptureTransferPlan
from onec_runtime.compact_table_backend import CompactRuntimeTableTransfer
from onec_runtime.errors import ProtocolError
from onec_runtime.execution.capture.data_plane import (
    CaptureTicketController, CaptureTicketDataPlane,
)
from onec_runtime.execution.capture.scope import CaptureScope
from onec_runtime.execution.value_reference import validate_public_direct_handle
from onec_runtime.table_materialization import ReferencePolicy
from onec_runtime.value_materialization import MaterializationOptions
from onec_runtime.value_transfer_backend import (
    RuntimeValueTransfer,
)


class CaptureTransferTicketPort(Protocol):
    """A fence-bound controller ticket path returning integrity-checked bytes."""

    def materialize_private_payload(self, plan: CaptureTransferPlan) -> bytes: ...


@dataclass(frozen=True, slots=True)
class WorkerTransferCatalog:
    """One immutable Worker privacy catalog used to build a transfer plan."""

    revision: int
    registrations: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("Worker transfer revision is invalid")
        if type(self.registrations) is not tuple or any(
            not isinstance(item, str) or not item
            for item in self.registrations
        ):
            raise ValueError("Worker transfer registrations are invalid")


def bind_capture_ticket_materialization(
    controller: CaptureTicketController,
    scope: CaptureScope,
    *,
    runtime_generation: int,
    context_generation: int,
    worker_catalog_snapshot: Callable[[], WorkerTransferCatalog],
    wait_handoff: Callable[[], AbstractContextManager[None]] = nullcontext,
) -> CaptureTicketMaterializationPolicy:
    """Freeze Worker admission evidence for one stopped CAPTURE value route.

    The catalog is checked again inside the controller's arbiter plan before
    its first RDBG request. A changed generation refuses this transfer without
    losing the stopped frame; the caller may bind a fresh policy and retry.
    """

    if not callable(worker_catalog_snapshot):
        raise TypeError("Worker transfer snapshot reader is required")
    catalog = worker_catalog_snapshot()
    if not isinstance(catalog, WorkerTransferCatalog):
        raise TypeError("Worker transfer snapshot is invalid")

    def require_same_catalog() -> None:
        if worker_catalog_snapshot() != catalog:
            raise ProtocolError("Worker catalog changed before CAPTURE transfer")

    transfer = CaptureTicketDataPlane(
        controller, scope,
        wait_handoff=wait_handoff,
        before_materialization=require_same_catalog,
    )
    return CaptureTicketMaterializationPolicy(
        transfer,
        runtime_generation=runtime_generation,
        context_generation=context_generation,
        worker_type_registrations=lambda: catalog.registrations,
    )


def _no_direct_rdbg(*_args: object) -> object:
    raise ProtocolError("CAPTURE transfer requires its controller ticket")


def _public_direct_handle(handle: str) -> str:
    return validate_public_direct_handle(handle)


class CaptureTicketMaterializationPolicy:
    """Build qualified transfer plans and decode only confirmed ticket bytes.

    ``worker_type_registrations`` must describe the admitted Worker generation.
    Public callers construct this policy with the fenced binding function,
    which rechecks that catalog inside the controller ticket before RDBG.
    """

    def __init__(
        self,
        transfer: CaptureTransferTicketPort,
        *,
        runtime_generation: int,
        context_generation: int,
        worker_type_registrations: Callable[[], tuple[str, ...]],
    ) -> None:
        if (
            type(runtime_generation) is not int or runtime_generation <= 0
            or type(context_generation) is not int or context_generation <= 0
        ):
            raise ValueError("CAPTURE materialization generations must be positive")
        if not callable(worker_type_registrations):
            raise TypeError("Worker type registration reader is required")
        self._transfer = transfer
        self._runtime_generation = runtime_generation
        self._context_generation = context_generation
        self._worker_type_registrations = worker_type_registrations

    def materialize_value(
        self,
        handle: str,
        options: MaterializationOptions | None = None,
    ) -> object:
        """Decode one bounded direct Context value snapshot."""

        selected = options if options is not None else MaterializationOptions()
        if not isinstance(selected, MaterializationOptions):
            raise TypeError("value materialization options are invalid")
        safe_handle = _public_direct_handle(handle)
        backend = RuntimeValueTransfer(
            _no_direct_rdbg,
            _no_direct_rdbg,
            context_cleaner=_no_direct_rdbg,
            runtime_generation=lambda: self._runtime_generation,
            context_generation=self._context_generation,
            worker_type_registrations=self._worker_type_registrations,
            capture_executor=self._execute_ticket_plan,
        )
        return backend.materialize(safe_handle, selected)

    def to_df(
        self,
        handle: str,
        policy: ReferencePolicy | None = None,
        *,
        max_rows: int,
        max_bytes: int = 64 * 1024 * 1024,
    ) -> pd.DataFrame:
        """Decode one bounded direct Context table as a DataFrame."""

        if policy is not None and not isinstance(policy, ReferencePolicy):
            raise TypeError("table reference policy is invalid")
        if type(max_rows) is not int or max_rows <= 0:
            raise ValueError("table row budget must be positive")
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("table byte budget must be positive")
        safe_handle = _public_direct_handle(handle)
        backend = CompactRuntimeTableTransfer(
            _no_direct_rdbg,
            _no_direct_rdbg,
            runtime_generation=lambda: self._runtime_generation,
            context_generation=self._context_generation,
            context_cleaner=_no_direct_rdbg,
            max_text_size=((max_bytes + 2) // 3) * 4,
            max_payload_bytes=max_bytes,
            max_rows=max_rows,
            worker_type_registrations=self._worker_type_registrations,
            capture_executor=self._execute_ticket_plan,
        )
        return backend.to_df(safe_handle, policy or ReferencePolicy())

    def _execute_ticket_plan(
        self, plan: CaptureTransferPlan, kind: CaptureEvaluationKind,
    ) -> bytes:
        if kind is not CaptureEvaluationKind.MATERIALIZATION_HELPER:
            raise ProtocolError("CAPTURE materialization kind is invalid")
        return self._transfer.materialize_private_payload(plan)
