"""Bounded public value projections over a route-owned transfer ticket.

This service prepares serialization plans and decodes their verified results.
The injected ticket port owns the selected MAIN/CAPTURE route, its RDBG
capability, Worker-catalog recheck, and mandatory private-key cleanup. The
service cannot issue a debugger request directly.
"""

from __future__ import annotations

from collections.abc import Callable
from re import fullmatch
from typing import Protocol
from uuid import uuid4

import pandas as pd

from onec_runtime.capture_evaluation import CaptureTransferPlan
from onec_runtime.compact_table import decode_compact_table_payload
from onec_runtime.compact_table_backend import CompactRuntimeTableTransfer
from onec_runtime.errors import ProtocolError
from onec_runtime.execution.capture.ticket_materialization import WorkerTransferCatalog
from onec_runtime.execution.local_wait import validate_local_wait_timeout
from onec_runtime.execution.value_reference import validate_public_direct_handle
from onec_runtime.execution.value_transfer_plan import (
    MAX_PROJECTION_POSITION,
    bounded_slice,
    build_bounded_projection_instruction,
    build_projection_transfer_plan,
    classify_materialization_payload,
)
from onec_runtime.experiment import bsl_string_literal
from onec_runtime.performance_profile import PhaseRecorder
from onec_runtime.table_materialization import ReferenceMode, ReferencePolicy
from onec_runtime.value_materialization import MaterializationOptions, decode_value_payload
from onec_runtime.value_transfer_backend import RuntimeValueTransfer


class ProjectionTicketPort(Protocol):
    """One stopped route's admitted, integrity-checking ticket operations."""

    def transfer(
        self, plan: CaptureTransferPlan, *, catalog: WorkerTransferCatalog,
        timeout_s: float | None,
    ) -> bytes: ...

    def inspect_kind(
        self, handle: str, *, catalog: WorkerTransferCatalog,
        timeout_s: float | None,
    ) -> str: ...


class ValueProjectionService:
    """Plan and decode bounded projections without knowing MAIN or CAPTURE.

    A public caller must bind ``ProjectionTicketPort`` to the current route.
    ``transfer`` must use the plan's admission and decode callbacks inside its
    ticket and recheck ``catalog`` before the first remote effect. It must not
    return raw RDBG response text or bypass the private-key cleanup stage.
    """

    def __init__(
        self,
        ticket: ProjectionTicketPort,
        *,
        runtime_generation: int,
        context_generation: int,
        worker_catalog_snapshot: Callable[[], WorkerTransferCatalog],
    ) -> None:
        if not callable(getattr(ticket, "transfer", None)) or not callable(
            getattr(ticket, "inspect_kind", None)
        ):
            raise TypeError("projection requires a route-owned ticket port")
        if type(runtime_generation) is not int or runtime_generation <= 0:
            raise ValueError("runtime generation must be positive")
        if type(context_generation) is not int or context_generation <= 0:
            raise ValueError("context generation must be positive")
        if not callable(worker_catalog_snapshot):
            raise TypeError("Worker catalog reader is required")
        self._ticket = ticket
        self._runtime_generation = runtime_generation
        self._context_generation = context_generation
        self._worker_catalog_snapshot = worker_catalog_snapshot

    def materialization_kind(
        self, handle: str, *, timeout_s: float | None = None,
    ) -> str:
        """Read only the supported serializer kind from an admitted ticket."""

        safe_handle = validate_public_direct_handle(handle)
        catalog = self._catalog()
        kind = self._ticket.inspect_kind(
            safe_handle, catalog=catalog,
            timeout_s=validate_local_wait_timeout(timeout_s),
        )
        if kind not in {"value", "table"}:
            raise ProtocolError("materialization kind is invalid")
        return kind

    def materialize_value_payload(
        self,
        handle: str,
        *,
        refs: str = "presentation",
        max_depth: int,
        max_items: int,
        max_bytes: int,
        timeout_s: float | None = None,
        profiler: PhaseRecorder | None = None,
    ) -> bytes:
        """Return only an admitted, integrity-checked typed-value payload."""

        safe_handle = validate_public_direct_handle(handle)
        options = MaterializationOptions(refs, max_depth, max_items, max_bytes)
        catalog = self._catalog()
        wait = validate_local_wait_timeout(timeout_s)
        backend = RuntimeValueTransfer(
            _no_direct_debugger, _no_direct_debugger,
            context_cleaner=_no_direct_debugger,
            runtime_generation=lambda: self._runtime_generation,
            context_generation=self._context_generation,
            worker_type_registrations=lambda: catalog.registrations,
            profiler=profiler,
            capture_executor=lambda plan, _kind: self._transfer(
                plan, catalog=catalog, timeout_s=wait,
            ),
        )
        payload = backend.payload(safe_handle, options)
        if classify_materialization_payload(payload) != "value":
            raise ProtocolError("value materialization returned an invalid payload kind")
        return payload

    def materialize_table_payload(
        self,
        handle: str,
        *,
        refs: str | ReferenceMode = ReferenceMode.PRESENTATION,
        ref_columns: dict[str, str | ReferenceMode] | None = None,
        uuid_suffix: str = "__uuid",
        max_rows: int | None = None,
        max_bytes: int = 64 * 1024 * 1024,
        timeout_s: float | None = None,
        profiler: PhaseRecorder | None = None,
    ) -> bytes:
        """Return only an admitted, integrity-checked compact table payload."""

        safe_handle = validate_public_direct_handle(handle)
        if max_rows is not None and (type(max_rows) is not int or max_rows <= 0):
            raise ProtocolError("table row budget must be positive")
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ProtocolError("table byte budget must be positive")
        policy = ReferencePolicy(refs, ref_columns, uuid_suffix)
        catalog = self._catalog()
        wait = validate_local_wait_timeout(timeout_s)
        backend = CompactRuntimeTableTransfer(
            _no_direct_debugger, _no_direct_debugger,
            runtime_generation=lambda: self._runtime_generation,
            context_generation=self._context_generation,
            context_cleaner=_no_direct_debugger,
            worker_type_registrations=lambda: catalog.registrations,
            max_text_size=((max_bytes + 2) // 3) * 4,
            max_payload_bytes=max_bytes,
            max_rows=max_rows,
            profiler=profiler,
            capture_executor=lambda plan, _kind: self._transfer(
                plan, catalog=catalog, timeout_s=wait,
            ),
        )
        payload = backend.payload(safe_handle, policy)
        if classify_materialization_payload(payload) != "table":
            raise ProtocolError("table materialization returned an invalid payload kind")
        return payload

    def project_value_payload(
        self,
        handle: str,
        *,
        kind: str,
        offset: int,
        limit: int | None,
        columns: tuple[str, ...],
        names: tuple[str, ...],
        max_depth: int,
        max_items: int,
        max_rows: int,
        max_bytes: int,
        timeout_s: float | None = None,
        refs: str | ReferenceMode = ReferenceMode.PRESENTATION,
        ref_columns: dict[str, str | ReferenceMode] | None = None,
        uuid_suffix: str = "__uuid",
    ) -> tuple[str, bytes]:
        """Serialize only a validated slice, row set, or named projection."""

        safe_handle = validate_public_direct_handle(handle)
        _validate_projection(
            kind, offset, limit, columns, names,
            max_depth=max_depth, max_items=max_items,
            max_rows=max_rows, max_bytes=max_bytes,
        )
        policy = ReferencePolicy(refs, ref_columns, uuid_suffix)
        if kind != "table_rows":
            MaterializationOptions(_reference_mode(refs), max_depth, max_items, max_bytes)
        catalog = self._catalog()
        key = f"__onec_projection_{uuid4().hex}"
        instruction = build_bounded_projection_instruction(
            safe_handle,
            context_key=key,
            kind=kind,
            offset=offset,
            limit=limit,
            columns=columns,
            names=names,
            refs=refs,
            ref_columns=ref_columns,
            max_depth=max_depth,
            max_items=max_items,
            max_rows=max_rows,
            max_bytes=max_bytes,
            runtime_generation=self._runtime_generation,
            context_generation=self._context_generation,
            worker_type_registrations=catalog.registrations,
        )
        plan = build_projection_transfer_plan(
            instruction,
            context_key=key,
            max_bytes=max_bytes,
            runtime_generation=self._runtime_generation,
            context_generation=self._context_generation,
        )
        payload = self._transfer(
            plan, catalog=catalog,
            timeout_s=validate_local_wait_timeout(timeout_s),
        )
        route = classify_materialization_payload(payload)
        expected = "table" if kind == "table_rows" else "value"
        if route != expected:
            raise ProtocolError("projection returned an invalid payload kind")
        return ("compact_table" if kind == "table_rows" else "value", payload)

    def project_to_df(
        self,
        handle: str,
        selection: dict[str, object],
        *,
        refs: str | ReferenceMode = ReferenceMode.PRESENTATION,
        ref_columns: dict[str, str | ReferenceMode] | None = None,
        uuid_suffix: str = "__uuid",
        chunk_size: int | None = None,
        max_rows: int = 10_000,
        max_bytes: int = 64 * 1024 * 1024,
        timeout_s: float | None = None,
    ) -> pd.DataFrame:
        """Decode a bounded table row selection without scanning earlier rows."""

        del chunk_size
        if type(max_rows) is not int or max_rows <= 0:
            raise ProtocolError("table row budget must be positive")
        offset, limit = bounded_slice(selection, maximum=max_rows)
        _, payload = self.project_value_payload(
            handle, kind="table_rows", offset=offset, limit=limit,
            columns=(), names=(), max_depth=1, max_items=limit,
            max_rows=max_rows, max_bytes=max_bytes, timeout_s=timeout_s,
            refs=refs, ref_columns=ref_columns, uuid_suffix=uuid_suffix,
        )
        return decode_compact_table_payload(
            payload, ReferencePolicy(refs, ref_columns, uuid_suffix),
        )

    def project_value(
        self,
        handle: str,
        selection: dict[str, object],
        *,
        refs: str = "presentation",
        ref_columns: dict[str, str] | None = None,
        uuid_suffix: str = "__uuid",
        chunk_size: int | None = None,
        max_depth: int = 32,
        max_items: int = 100_000,
        max_bytes: int = 64 * 1024 * 1024,
        timeout_s: float | None = None,
    ) -> object:
        """Decode a bounded slice with its current table or value shape."""

        del chunk_size
        if type(max_items) is not int or max_items <= 0:
            raise ProtocolError("value item budget must be positive")
        offset, limit = bounded_slice(selection, maximum=max_items)
        safe_handle = validate_public_direct_handle(handle)
        options = MaterializationOptions(refs, max_depth, max_items, max_bytes)
        catalog = self._catalog()
        key = f"__onec_projection_{uuid4().hex}"
        instruction = _build_dynamic_slice_instruction(
            safe_handle, context_key=key, offset=offset, limit=limit,
            options=options, ref_columns=ref_columns,
            catalog=catalog, runtime_generation=self._runtime_generation,
            context_generation=self._context_generation,
        )
        plan = build_projection_transfer_plan(
            instruction, context_key=key, max_bytes=max_bytes,
            runtime_generation=self._runtime_generation,
            context_generation=self._context_generation,
        )
        payload = self._transfer(
            plan, catalog=catalog,
            timeout_s=validate_local_wait_timeout(timeout_s),
        )
        if classify_materialization_payload(payload) == "table":
            return decode_compact_table_payload(
                payload, ReferencePolicy(refs, ref_columns, uuid_suffix),
            )
        return decode_value_payload(payload, options)

    def _catalog(self) -> WorkerTransferCatalog:
        catalog = self._worker_catalog_snapshot()
        if not isinstance(catalog, WorkerTransferCatalog):
            raise TypeError("Worker transfer snapshot is invalid")
        return catalog

    def _transfer(
        self, plan: CaptureTransferPlan, *, catalog: WorkerTransferCatalog,
        timeout_s: float | None,
    ) -> bytes:
        payload = self._ticket.transfer(plan, catalog=catalog, timeout_s=timeout_s)
        if type(payload) is not bytes:
            raise ProtocolError("materialization ticket payload is invalid")
        return payload


def _reference_mode(refs: str | ReferenceMode) -> str:
    return refs.value if isinstance(refs, ReferenceMode) else refs


def _build_dynamic_slice_instruction(
    handle: str, *, context_key: str, offset: int, limit: int,
    options: MaterializationOptions,
    ref_columns: dict[str, str] | None,
    catalog: WorkerTransferCatalog,
    runtime_generation: int, context_generation: int,
) -> str:
    """Choose table/value and slice within one remote helper operation."""

    try:
        table_mode = ReferenceMode(options.refs).value
    except ValueError as error:
        raise ProtocolError("unknown table reference mode") from error
    overrides = dict(ref_columns or {})
    for name, mode in overrides.items():
        if not isinstance(name, str) or not name:
            raise ProtocolError("table reference column name is invalid")
        try:
            ReferenceMode(mode)
        except ValueError as error:
            raise ProtocolError("unknown table reference mode") from error
    worker_lines = ["ТипыОбъектовWorker = Новый Массив;"]
    for index, registration in enumerate(catalog.registrations):
        worker_lines.extend((
            f"ВременныйОбъектWorker{index} = ВнешниеОбработки.Создать("
            f"{bsl_string_literal(registration)}, Ложь);",
            f"ТипыОбъектовWorker.Добавить(ТипЗнч(ВременныйОбъектWorker{index}));",
        ))
    end = offset + limit - 1
    lines = [
        "Попытка", *worker_lines,
        "Если Не RuntimeValueTransferServer.ДопуститьЗначение("
        f"{handle}, ТипыОбъектовWorker) Тогда",
        '    Результат = "D|worker_generation_value";',
        "Иначе",
        "    ВидМатериализации = RuntimeValueTransferServer."
        f"ПолучитьВидМатериализации({handle});",
        '    Если ВидМатериализации = "table" Тогда',
        "        СтрокиПроекции = Новый Массив;",
        f"        Для ИндексПроекции = {offset} По Мин({handle}.Количество() - 1, {end}) Цикл",
        f"            СтрокиПроекции.Добавить({handle}[ИндексПроекции]);",
        "        КонецЦикла;",
        f"        ПроекцияЗначения = {handle}.Скопировать(СтрокиПроекции);",
        "        РежимыСсылокМатериализации = Новый Соответствие;",
    ]
    for name in sorted(overrides):
        lines.append(
            "        РежимыСсылокМатериализации.Вставить("
            f"{bsl_string_literal(name)}, "
            f"{bsl_string_literal(ReferenceMode(overrides[name]).value)});"
        )
    lines.extend((
        "        Материализация = RuntimeTableTransferServer."
        "СериализоватьКомпактнуюТаблицу("
        f"ПроекцияЗначения, {bsl_string_literal(table_mode)}, "
        "РежимыСсылокМатериализации, ТипыОбъектовWorker, "
        f"{limit}, {options.max_bytes});",
        '    ИначеЕсли ВидМатериализации = "value" Тогда',
        "        ПроекцияЗначения = Новый Массив;",
        f"        Для ИндексПроекции = {offset} По Мин({handle}.Количество() - 1, {end}) Цикл",
        f"            ПроекцияЗначения.Добавить({handle}[ИндексПроекции]);",
        "        КонецЦикла;",
        "        Материализация = RuntimeValueTransferServer."
        "СериализоватьЗначение("
        f"ПроекцияЗначения, {bsl_string_literal(options.refs)}, "
        f"{options.max_depth}, {options.max_items}, {options.max_bytes}, "
        "ТипыОбъектовWorker);",
        "    Иначе",
        '        Результат = "E|value_admission_failed";',
        "    КонецЕсли;",
        '    Если ВидМатериализации = "table" Или ВидМатериализации = "value" Тогда',
        "        Если Не Материализация.Доступ Тогда",
        '            Результат = "D|worker_generation_value";',
        "        Иначе",
        f"            Контекст.Вставить({bsl_string_literal(context_key)}, Материализация.Base64);",
        '            Результат = "R|" + '
        f'Формат({runtime_generation}, "ЧГ=0; ЧДЦ=0") + "|" + '
        f'Формат({context_generation}, "ЧГ=0; ЧДЦ=0") + "|" + '
        'Формат(Материализация.Размер, "ЧГ=0; ЧДЦ=0") + "|" + '
        'Материализация.Хеш + "|" + '
        'Формат(СтрДлина(Материализация.Base64), "ЧГ=0; ЧДЦ=0");',
        "        КонецЕсли;",
        "    КонецЕсли;",
        "КонецЕсли;",
        "Исключение",
        '    Результат = "E|value_admission_failed";',
        "КонецПопытки;",
    ))
    return "\n".join(lines)


def _validate_projection(
    kind: str, offset: int, limit: int | None,
    columns: tuple[str, ...], names: tuple[str, ...],
    *, max_depth: int, max_items: int, max_rows: int, max_bytes: int,
) -> None:
    if any(type(value) is not int or value <= 0 for value in (
        max_depth, max_items, max_rows, max_bytes,
    )):
        raise ProtocolError("projection materialization budgets must be positive")
    if type(offset) is not int or not 0 <= offset <= MAX_PROJECTION_POSITION:
        raise ProtocolError("projection offset is invalid")
    if limit is not None and (type(limit) is not int or limit <= 0):
        raise ProtocolError("projection limit is invalid")
    if type(columns) is not tuple or type(names) is not tuple or any(
        not isinstance(name, str)
        or fullmatch(r"[^\W\d]\w*", name) is None
        for name in (*columns, *names)
    ):
        raise ProtocolError("projection names must be BSL identifiers")
    if kind in {"slice", "table_rows"}:
        bound = max_rows if kind == "table_rows" else max_items
        if (
            limit is None or limit > bound
            or offset + limit > MAX_PROJECTION_POSITION
            or names or (kind == "slice" and columns)
        ):
            raise ProtocolError("projection exceeds its server-owned budget")
    elif kind in {"fields", "keys"}:
        if offset != 0 or limit is not None or columns or not names or len(names) > max_items:
            raise ProtocolError("projection exceeds its server-owned budget")
    else:
        raise ProtocolError("projection kind is unsupported")


def _no_direct_debugger(*_args: object) -> object:
    raise ProtocolError("value projection requires its route-owned ticket")


__all__ = ["ProjectionTicketPort", "ValueProjectionService"]
