"""Choose a fenced value transfer route without exposing route branches to APIs."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from re import fullmatch
from typing import Protocol
from uuid import uuid4

import pandas as pd

from onec_runtime.capture_evaluation import CaptureTransferPlan
from onec_runtime.compact_table import decode_compact_table_payload
from onec_runtime.errors import ProtocolError
from onec_runtime.execution.arbiter import RdbgArbiter
from onec_runtime.execution.capture.data_plane import CaptureTicketDataPlane
from onec_runtime.execution.capture.scope import CaptureScope
from onec_runtime.execution.capture.selected_table_materialization import (
    CaptureSelectedTableTransferRequest,
)
from onec_runtime.execution.capture.ticket_materialization import (
    WorkerTransferCatalog, bind_capture_ticket_materialization,
)
from onec_runtime.execution.dynamic_value_materialization import (
    CaptureDynamicValueMaterialization,
)
from onec_runtime.execution.local_wait import validate_local_wait_timeout
from onec_runtime.execution.main.idle_materialization import (
    MainIdleMaterializationService, MainIdleTargetFence,
)
from onec_runtime.execution.worker_activation import WorkerMaterializationSnapshot
from onec_runtime.execution.value_reference import validate_public_direct_handle
from onec_runtime.execution.value_transfer_plan import (
    build_projection_transfer_plan, classify_materialization_payload,
)
from onec_runtime.experiment import bsl_string_literal
from onec_runtime.table_materialization import ReferencePolicy
from onec_runtime.value_materialization import MaterializationOptions, decode_value_payload


_KIND_PAYLOAD_BYTES = 2048
_KIND_OPTIONS = MaterializationOptions(max_depth=1, max_items=1, max_bytes=_KIND_PAYLOAD_BYTES)


class ValueRouteController(Protocol):
    """Controller's local route snapshot plus CAPTURE materialization ticket."""

    capture_scope: CaptureScope | None

    def value_route_snapshot(self) -> CaptureScope | MainIdleTargetFence | None: ...
    def main_idle_fence(self) -> MainIdleTargetFence | None: ...
    def main_idle_fence_in_ticket(self) -> MainIdleTargetFence | None: ...
    def submit_capture_materialization(
        self, plan: object, *, _before_first_effect: Callable[[], None] | None = None,
    ) -> object: ...
    def require_capture_table_descriptor(
        self, handle: str, scope: CaptureScope,
    ) -> object: ...


class ValueMaterializationRouter:
    """Bind value transfers to the controller's current stopped route.

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
        self._arbiter = arbiter
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
        """Materialize one direct or deferred table on the stopped route."""

        if _is_selected_table_handle(handle):
            selected_policy = policy or ReferencePolicy()
            payload = self.transfer_selected_table(
                handle, selected_policy, max_rows=max_rows,
                max_bytes=max_bytes, catalog=self._transfer_catalog(),
                timeout_s=None,
            )
            return _decode_selected_table(payload, selected_policy)
        return self._select().to_df(
            handle, policy, max_rows=max_rows, max_bytes=max_bytes,
        )

    def materialize(
        self,
        handle: str,
        options: MaterializationOptions | None = None,
        *,
        table_policy: ReferencePolicy | None = None,
        timeout_s: float | None = None,
    ) -> object:
        """Materialize on the confirmed route with an optional local wait limit."""

        local_wait = validate_local_wait_timeout(timeout_s)
        if _is_selected_table_handle(handle):
            selected_options = options or MaterializationOptions()
            if not isinstance(selected_options, MaterializationOptions):
                raise TypeError("value materialization options are invalid")
            policy = table_policy or ReferencePolicy()
            payload = self.transfer_selected_table(
                handle, policy, max_rows=selected_options.max_items,
                max_bytes=selected_options.max_bytes,
                catalog=self._transfer_catalog(), timeout_s=local_wait,
            )
            return _decode_selected_table(payload, policy)
        route = self._route_snapshot()
        if isinstance(route, CaptureScope):
            return self._capture_dynamic(route).materialize(
                handle, options, table_policy=table_policy, timeout_s=local_wait,
            )
        if isinstance(route, MainIdleTargetFence):
            return self._main.materialize(
                handle, options, table_policy=table_policy, timeout_s=local_wait,
            )
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

        if _is_selected_table_handle(handle):
            selected_policy = policy or ReferencePolicy()
            payload = self.transfer_selected_table(
                handle, selected_policy, max_rows=count,
                max_bytes=max_bytes, catalog=self._transfer_catalog(),
                timeout_s=None, relative_limit=count,
            )
            return _decode_selected_table(payload, selected_policy)
        route = self._route_snapshot()
        if isinstance(route, CaptureScope):
            return self._capture_dynamic(route).head_to_df(
                handle, count, policy=policy, max_bytes=max_bytes,
            )
        if isinstance(route, MainIdleTargetFence):
            return self._main.head_to_df(
                handle, count, policy=policy, max_bytes=max_bytes,
            )
        raise ProtocolError("No confirmed stopped value route is available")

    def transfer(
        self, plan: CaptureTransferPlan, *, catalog: WorkerTransferCatalog,
        timeout_s: float | None,
    ) -> bytes:
        """Execute a private projection plan through the current route ticket.

        The route owner decodes the admission envelope, keeps unknown outcomes
        pending, and performs private-key cleanup. A changed Worker catalog or
        stopped route is rejected before a remote side effect.
        """

        if not isinstance(plan, CaptureTransferPlan):
            raise TypeError("projection transfer plan is invalid")
        if not isinstance(catalog, WorkerTransferCatalog):
            raise TypeError("projection Worker catalog is invalid")
        wait = validate_local_wait_timeout(timeout_s)
        self._require_catalog(catalog)
        route = self._route_snapshot()
        if isinstance(route, CaptureScope):
            def require_same_catalog() -> None:
                self._require_catalog(catalog)

            data = CaptureTicketDataPlane(
                self._controller, route,
                wait_handoff=self._wait_handoff,
                before_materialization=require_same_catalog,
            )
            return data.materialize_private_payload(plan, timeout_s=wait)
        if isinstance(route, MainIdleTargetFence):
            return self._main.transfer_private_plan(
                plan,
                catalog=WorkerMaterializationSnapshot(
                    catalog.revision, catalog.registrations,
                ),
                timeout_s=wait,
            )
        raise ProtocolError("No confirmed stopped value route is available")

    def validate_selected_table_handle(self, handle: str) -> None:
        """Check a controller-owned selected table key without debugger I/O."""

        route = self._route_snapshot()
        if not isinstance(route, CaptureScope):
            raise ProtocolError("CAPTURE selected table route is unavailable")
        self._controller.require_capture_table_descriptor(handle, route)

    def transfer_selected_table(
        self,
        handle: str,
        policy: ReferencePolicy,
        *,
        max_rows: int,
        max_bytes: int,
        catalog: WorkerTransferCatalog,
        timeout_s: float | None,
        relative_offset: int = 0,
        relative_limit: int | None = None,
    ) -> bytes:
        """Resolve a selected descriptor and build its plan inside the ticket."""

        if not isinstance(catalog, WorkerTransferCatalog):
            raise TypeError("projection Worker catalog is invalid")
        wait = validate_local_wait_timeout(timeout_s)
        self._require_catalog(catalog)
        route = self._route_snapshot()
        if not isinstance(route, CaptureScope):
            raise ProtocolError("CAPTURE selected table route is unavailable")
        request = CaptureSelectedTableTransferRequest(
            handle, route, policy, max_rows, max_bytes,
            self._runtime_generation, self._context_generation,
            catalog.registrations, relative_offset, relative_limit,
        )

        def require_same_catalog() -> None:
            self._require_catalog(catalog)

        data = CaptureTicketDataPlane(
            self._controller, route,
            wait_handoff=self._wait_handoff,
            before_materialization=require_same_catalog,
        )
        return data.materialize_private_payload(request, timeout_s=wait)

    def inspect_kind(
        self, handle: str, *, catalog: WorkerTransferCatalog,
        timeout_s: float | None,
    ) -> str:
        """Read a bounded serializer kind without exposing debugger text."""

        safe_handle = validate_public_direct_handle(handle)
        if not isinstance(catalog, WorkerTransferCatalog):
            raise TypeError("projection Worker catalog is invalid")
        self._require_catalog(catalog)
        key = f"__onec_projection_{uuid4().hex}"
        instruction = _kind_instruction(
            safe_handle, context_key=key,
            catalog=catalog,
            runtime_generation=self._runtime_generation,
            context_generation=self._context_generation,
        )
        plan = build_projection_transfer_plan(
            instruction, context_key=key, max_bytes=_KIND_PAYLOAD_BYTES,
            runtime_generation=self._runtime_generation,
            context_generation=self._context_generation,
        )
        payload = self.transfer(plan, catalog=catalog, timeout_s=timeout_s)
        if classify_materialization_payload(payload) != "value":
            raise ProtocolError("materialization kind payload is invalid")
        kind = decode_value_payload(payload, _KIND_OPTIONS)
        if kind not in {"value", "table"}:
            raise ProtocolError("materialization kind is invalid")
        return kind

    def _select(self):
        route = self._route_snapshot()
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

    def _route_snapshot(self) -> CaptureScope | MainIdleTargetFence | None:
        # A parent value reply can settle before its mandatory private-key
        # cleanup. Wait for that exact dependent ticket, then inspect the
        # controller again; another user operation may already own the route.
        with self._wait_handoff():
            self._arbiter.wait_for_dependent_cleanup()
        return self._controller.value_route_snapshot()

    def _worker_snapshot(self) -> WorkerMaterializationSnapshot:
        snapshot = self._worker_catalog_snapshot()
        if not isinstance(snapshot, WorkerMaterializationSnapshot):
            raise TypeError("Worker materialization snapshot is invalid")
        return snapshot

    def _transfer_catalog(self) -> WorkerTransferCatalog:
        snapshot = self._worker_snapshot()
        return WorkerTransferCatalog(snapshot.revision, snapshot.registrations)

    def _require_catalog(self, expected: WorkerTransferCatalog) -> None:
        if self._transfer_catalog() != expected:
            raise ProtocolError("Worker catalog changed before value projection")

    def _capture_dynamic(self, scope: CaptureScope) -> CaptureDynamicValueMaterialization:
        return CaptureDynamicValueMaterialization(
            self._controller,
            scope,
            runtime_generation=self._runtime_generation,
            context_generation=self._context_generation,
            worker_catalog_snapshot=self._transfer_catalog,
            wait_handoff=self._wait_handoff,
        )


def _kind_instruction(
    handle: str, *, context_key: str, catalog: WorkerTransferCatalog,
    runtime_generation: int, context_generation: int,
) -> str:
    """Serialize only ``value`` or ``table`` after Worker privacy admission."""

    lines = ["Попытка", "ТипыОбъектовWorker = Новый Массив;"]
    for index, registration in enumerate(catalog.registrations):
        lines.extend((
            f"ВременныйОбъектWorker{index} = ВнешниеОбработки.Создать("
            f"{bsl_string_literal(registration)}, Ложь);",
            f"ТипыОбъектовWorker.Добавить(ТипЗнч(ВременныйОбъектWorker{index}));",
        ))
    lines.extend((
        "Если Не RuntimeValueTransferServer.ДопуститьЗначение("
        f"{handle}, ТипыОбъектовWorker) Тогда",
        '    Результат = "D|worker_generation_value";',
        "Иначе",
        "    ВидМатериализации = RuntimeValueTransferServer."
        f"ПолучитьВидМатериализации({handle});",
        '    Если ВидМатериализации = "table" Или ВидМатериализации = "value" Тогда',
        "        Материализация = RuntimeValueTransferServer."
        "СериализоватьЗначение(ВидМатериализации, "
        f'"presentation", 1, 1, {_KIND_PAYLOAD_BYTES}, ТипыОбъектовWorker);',
        "        Если Не Материализация.Доступ Тогда",
        '            Результат = "D|worker_generation_value";',
        "        Иначе",
        f"            e1cRuntimeКонтекст.Вставить({bsl_string_literal(context_key)}, Материализация.Base64);",
        '            Результат = "R|" + '
        f'Формат({runtime_generation}, "ЧГ=0; ЧДЦ=0") + "|" + '
        f'Формат({context_generation}, "ЧГ=0; ЧДЦ=0") + "|" + '
        'Формат(Материализация.Размер, "ЧГ=0; ЧДЦ=0") + "|" + '
        'Материализация.Хеш + "|" + '
        'Формат(СтрДлина(Материализация.Base64), "ЧГ=0; ЧДЦ=0");',
        "        КонецЕсли;",
        "    Иначе",
        '        Результат = "E|value_admission_failed";',
        "    КонецЕсли;",
        "КонецЕсли;",
        "Исключение",
        '    Результат = "E|value_admission_failed";',
        "КонецПопытки;",
    ))
    return "\n".join(lines)


def _is_selected_table_handle(handle: object) -> bool:
    return type(handle) is str and fullmatch(r"capture_table_[0-9a-f]{32}", handle) is not None


def _decode_selected_table(payload: bytes, policy: ReferencePolicy) -> pd.DataFrame:
    if classify_materialization_payload(payload) != "table":
        raise ProtocolError("selected table returned an invalid payload kind")
    return decode_compact_table_payload(payload, policy)
