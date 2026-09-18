"""Bounded dynamic value and table projections over one CAPTURE ticket."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from typing import Protocol
from uuid import uuid4

import pandas as pd

from onec_runtime.compact_table import decode_compact_table_payload
from onec_runtime.errors import ProtocolError
from onec_runtime.execution.capture.data_plane import (
    CaptureTicketController,
    CaptureTicketDataPlane,
)
from onec_runtime.execution.capture.scope import CaptureScope
from onec_runtime.execution.capture.ticket_materialization import WorkerTransferCatalog
from onec_runtime.execution.local_wait import validate_local_wait_timeout
from onec_runtime.execution.value_transfer_plan import (
    build_bounded_projection_instruction,
    build_dynamic_materialization_instruction,
    build_projection_transfer_plan,
    classify_materialization_payload,
)
from onec_runtime.execution.value_reference import validate_public_direct_handle
from onec_runtime.table_materialization import ReferencePolicy
from onec_runtime.value_materialization import MaterializationOptions, decode_value_payload


class CaptureDynamicValueMaterialization:
    """Build bounded dynamic transfers and submit them through the stopped route.

    This service holds no debugger session. ``CaptureTicketDataPlane`` submits
    the plan to the controller, whose arbiter executor owns all RDBG work.
    """

    def __init__(
        self,
        controller: CaptureTicketController,
        scope: CaptureScope,
        *,
        runtime_generation: int,
        context_generation: int,
        worker_catalog_snapshot: Callable[[], WorkerTransferCatalog],
        wait_handoff: Callable[[], AbstractContextManager[None]] = nullcontext,
    ) -> None:
        if not callable(worker_catalog_snapshot):
            raise TypeError("Worker catalog reader is required")
        if not callable(wait_handoff):
            raise TypeError("CAPTURE wait handoff is invalid")
        if type(runtime_generation) is not int or runtime_generation <= 0:
            raise ValueError("runtime generation must be positive")
        if type(context_generation) is not int or context_generation <= 0:
            raise ValueError("context generation must be positive")
        self._controller = controller
        self._scope = scope
        self._runtime_generation = runtime_generation
        self._context_generation = context_generation
        self._catalog_reader = worker_catalog_snapshot
        self._wait_handoff = wait_handoff

    def materialize(
        self,
        handle: str,
        options: MaterializationOptions | None = None,
        *,
        table_policy: ReferencePolicy | None = None,
        timeout_s: float | None = None,
    ) -> object:
        """Materialize with an optional limit on the initiating ticket wait."""

        local_wait = validate_local_wait_timeout(timeout_s)
        selected = options or MaterializationOptions()
        if not isinstance(selected, MaterializationOptions):
            raise TypeError("value materialization options are invalid")
        if table_policy is not None and not isinstance(table_policy, ReferencePolicy):
            raise TypeError("table reference policy is invalid")
        safe_handle = validate_public_direct_handle(handle)
        catalog, transfer = self._transfer()
        key = f"__onec_materialization_{uuid4().hex}"
        instruction = build_dynamic_materialization_instruction(
            safe_handle,
            context_key=key,
            options=selected,
            refs=selected.refs if table_policy is None else table_policy.refs,
            ref_columns=None if table_policy is None else table_policy.ref_columns,
            max_rows=selected.max_items,
            runtime_generation=self._runtime_generation,
            context_generation=self._context_generation,
            worker_type_registrations=catalog.registrations,
        )
        payload = transfer.materialize_private_payload(
            build_projection_transfer_plan(
                instruction,
                context_key=key,
                max_bytes=selected.max_bytes,
                runtime_generation=self._runtime_generation,
                context_generation=self._context_generation,
            ),
            timeout_s=local_wait,
        )
        route = classify_materialization_payload(payload)
        if route == "table":
            policy = table_policy or ReferencePolicy(selected.refs)
            return decode_compact_table_payload(payload, policy)
        return decode_value_payload(payload, selected)

    def head_to_df(
        self,
        handle: str,
        count: int,
        *,
        policy: ReferencePolicy | None = None,
        max_bytes: int = 64 * 1024 * 1024,
    ) -> pd.DataFrame:
        """Transfer only the first ``count`` rows of a direct Context table."""

        if type(count) is not int or not 0 < count <= 10_000:
            raise ValueError("head row count must be between 1 and 10000")
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("table byte budget must be positive")
        if policy is not None and not isinstance(policy, ReferencePolicy):
            raise TypeError("table reference policy is invalid")
        safe_handle = validate_public_direct_handle(handle)
        catalog, transfer = self._transfer()
        selected_policy = policy or ReferencePolicy()
        key = f"__onec_projection_{uuid4().hex}"
        instruction = build_bounded_projection_instruction(
            safe_handle,
            context_key=key,
            kind="table_rows",
            offset=0,
            limit=count,
            columns=(),
            names=(),
            refs=selected_policy.refs,
            ref_columns=selected_policy.ref_columns,
            max_rows=count,
            max_bytes=max_bytes,
            runtime_generation=self._runtime_generation,
            context_generation=self._context_generation,
            worker_type_registrations=catalog.registrations,
        )
        payload = transfer.materialize_private_payload(build_projection_transfer_plan(
            instruction,
            context_key=key,
            max_bytes=max_bytes,
            runtime_generation=self._runtime_generation,
            context_generation=self._context_generation,
        ))
        if classify_materialization_payload(payload) != "table":
            raise ProtocolError("bounded table projection returned a value payload")
        return decode_compact_table_payload(payload, selected_policy)

    def _transfer(self) -> tuple[WorkerTransferCatalog, CaptureTicketDataPlane]:
        catalog = self._catalog_reader()
        if not isinstance(catalog, WorkerTransferCatalog):
            raise TypeError("Worker transfer snapshot is invalid")

        def require_same_catalog() -> None:
            if self._catalog_reader() != catalog:
                raise ProtocolError("Worker catalog changed before CAPTURE transfer")

        return catalog, CaptureTicketDataPlane(
            self._controller,
            self._scope,
            wait_handoff=self._wait_handoff,
            before_materialization=require_same_catalog,
        )
