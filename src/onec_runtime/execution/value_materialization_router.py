"""Choose a fenced value transfer route without exposing route branches to APIs."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from typing import Protocol

import pandas as pd

from onec_runtime.errors import ProtocolError
from onec_runtime.execution.arbiter import RdbgArbiter
from onec_runtime.execution.capture.scope import CaptureScope
from onec_runtime.execution.capture.ticket_materialization import (
    WorkerTransferCatalog, bind_capture_ticket_materialization,
)
from onec_runtime.execution.dynamic_value_materialization import (
    CaptureDynamicValueMaterialization,
)
from onec_runtime.execution.main.idle_materialization import (
    MainIdleMaterializationService, MainIdleTargetFence,
)
from onec_runtime.execution.worker_activation import WorkerMaterializationSnapshot
from onec_runtime.table_materialization import ReferencePolicy
from onec_runtime.value_materialization import MaterializationOptions


class ValueRouteController(Protocol):
    """Controller's local route snapshot plus CAPTURE materialization ticket."""

    capture_scope: CaptureScope | None

    def value_route_snapshot(self) -> CaptureScope | MainIdleTargetFence | None: ...
    def main_idle_fence(self) -> MainIdleTargetFence | None: ...
    def main_idle_fence_in_ticket(self) -> MainIdleTargetFence | None: ...
    def submit_capture_materialization(
        self, plan: object, *, _before_first_effect: Callable[[], None] | None = None,
    ) -> object: ...


class ValueMaterializationRouter:
    """Bind direct Context transfers to the controller's current stopped route.

    The router contains the only MAIN/CAPTURE value-route choice. Neither
    RuntimeSession nor PublicExecutionFacade needs a mode branch. Every remote
    operation still belongs to a mode-specific arbiter ticket and rechecks its
    stop/target and Worker catalog before transport.
    """

    def __init__(
        self,
        controller: ValueRouteController,
        arbiter: RdbgArbiter,
        *,
        runtime_generation: int,
        context_generation: int,
        worker_catalog_snapshot: Callable[[], WorkerMaterializationSnapshot],
        wait_handoff: Callable[[], AbstractContextManager[None]] = nullcontext,
    ) -> None:
        if not callable(worker_catalog_snapshot):
            raise TypeError("Worker catalog reader is required")
        if not callable(wait_handoff):
            raise TypeError("value transfer wait handoff is invalid")
        self._controller = controller
        self._worker_catalog_snapshot = worker_catalog_snapshot
        self._runtime_generation = runtime_generation
        self._context_generation = context_generation
        self._wait_handoff = wait_handoff
        self._main = MainIdleMaterializationService(
            arbiter,
            main_idle_fence=controller.main_idle_fence,
            main_idle_fence_in_ticket=controller.main_idle_fence_in_ticket,
            runtime_generation=runtime_generation,
            context_generation=context_generation,
            worker_catalog_snapshot=self._worker_snapshot,
            wait_handoff=wait_handoff,
        )

    @property
    def retryable_main_cleanup_keys(self) -> tuple[str, ...]:
        """Confirmed MAIN private-key deletion debts held by the arbiter."""

        return self._main.retryable_cleanup_keys

    def retry_main_cleanup(self, key: str) -> None:
        """Retry only the confirmed idempotent deletion for a prior transfer."""

        self._main.retry_cleanup(key)

    def materialize_value(
        self, handle: str, options: MaterializationOptions | None = None,
    ) -> object:
        """Materialize one direct Context value on the selected stopped route."""

        return self._select().materialize_value(handle, options)

    def to_df(
        self, handle: str, policy: ReferencePolicy | None = None,
        *, max_rows: int, max_bytes: int = 64 * 1024 * 1024,
    ) -> pd.DataFrame:
        """Materialize one direct Context table on the selected stopped route."""

        return self._select().to_df(
            handle, policy, max_rows=max_rows, max_bytes=max_bytes,
        )

    def materialize(
        self,
        handle: str,
        options: MaterializationOptions | None = None,
        *,
        table_policy: ReferencePolicy | None = None,
    ) -> object:
        """Dynamically materialize a value or table on the confirmed route."""

        route = self._controller.value_route_snapshot()
        if isinstance(route, CaptureScope):
            return self._capture_dynamic(route).materialize(
                handle, options, table_policy=table_policy,
            )
        if isinstance(route, MainIdleTargetFence):
            return self._main.materialize(handle, options, table_policy=table_policy)
        raise ProtocolError("No confirmed stopped value route is available")

    def head_to_df(
        self,
        handle: str,
        count: int,
        *,
        policy: ReferencePolicy | None = None,
        max_bytes: int = 64 * 1024 * 1024,
    ) -> pd.DataFrame:
        """Return the bounded leading table rows on the confirmed route."""

        route = self._controller.value_route_snapshot()
        if isinstance(route, CaptureScope):
            return self._capture_dynamic(route).head_to_df(
                handle, count, policy=policy, max_bytes=max_bytes,
            )
        if isinstance(route, MainIdleTargetFence):
            return self._main.head_to_df(
                handle, count, policy=policy, max_bytes=max_bytes,
            )
        raise ProtocolError("No confirmed stopped value route is available")

    def _select(self):
        route = self._controller.value_route_snapshot()
        if isinstance(route, CaptureScope):
            return bind_capture_ticket_materialization(
                self._controller, route,
                runtime_generation=self._runtime_generation,
                context_generation=self._context_generation,
                worker_catalog_snapshot=self._transfer_catalog,
                wait_handoff=self._wait_handoff,
            )
        if isinstance(route, MainIdleTargetFence):
            return self._main
        raise ProtocolError("No confirmed stopped value route is available")

    def _worker_snapshot(self) -> WorkerMaterializationSnapshot:
        snapshot = self._worker_catalog_snapshot()
        if not isinstance(snapshot, WorkerMaterializationSnapshot):
            raise TypeError("Worker materialization snapshot is invalid")
        return snapshot

    def _transfer_catalog(self) -> WorkerTransferCatalog:
        snapshot = self._worker_snapshot()
        return WorkerTransferCatalog(snapshot.revision, snapshot.registrations)

    def _capture_dynamic(self, scope: CaptureScope) -> CaptureDynamicValueMaterialization:
        return CaptureDynamicValueMaterialization(
            self._controller,
            scope,
            runtime_generation=self._runtime_generation,
            context_generation=self._context_generation,
            worker_catalog_snapshot=self._transfer_catalog,
            wait_handoff=self._wait_handoff,
        )
