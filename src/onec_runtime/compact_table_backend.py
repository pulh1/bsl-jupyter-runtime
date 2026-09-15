from __future__ import annotations

from base64 import b64decode
import binascii
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
import re
from uuid import uuid4

import pandas as pd

from onec_runtime.compact_table import decode_compact_table_payload
from onec_runtime.capture_evaluation import CaptureTransferPlan
from onec_runtime.errors import ProtocolError
from onec_runtime.experiment import bsl_string_literal
from onec_runtime.performance_profile import PhaseRecorder
from onec_runtime.rdbg.models import CollectionRow
from onec_runtime.table_materialization import ReferenceMode, ReferencePolicy


_HANDLE = re.compile(
    r"Контекст\.[^\W\d]\w*(?:\.[^\W\d]\w*)*\Z",
    re.UNICODE,
)
_CONTEXT_KEY = re.compile(r"__onec_compact_table_[0-9a-f]{32}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[^\W\d]\w*\Z", re.UNICODE)
_SCALAR_KINDS = frozenset(
    {"string", "nullable_string", "boolean", "integer", "number", "datetime", "uuid"}
)


@dataclass(frozen=True, slots=True)
class CompactColumn:
    name: str
    kind: str
    is_reference: bool


def _compact_cell_kind(type_name: str) -> tuple[str, bool] | None:
    if type_name in {"Неопределено", "Null"}:
        return None
    if type_name.startswith("ПеречислениеСсылка."):
        return "nullable_string", False
    if "Ссылка." in type_name:
        return "reference", True
    kinds = {
        "Строка": "nullable_string",
        "Булево": "boolean",
        "Число": "number",
        "Дата": "datetime",
        "УникальныйИдентификатор": "uuid",
    }
    kind = kinds.get(type_name)
    if kind is None:
        raise ProtocolError("unsupported compact table column type")
    return kind, False


def infer_compact_columns(
    rows: tuple[CollectionRow, ...],
) -> tuple[CompactColumn, ...] | None:
    if not rows:
        return None
    names = tuple(cell.name for cell in rows[0].cells)
    if not names or len(set(names)) != len(names):
        raise ProtocolError("compact table frame schema is invalid")
    observed: list[tuple[str, bool] | None] = [None] * len(names)
    for row in rows:
        if tuple(cell.name for cell in row.cells) != names:
            raise ProtocolError("compact table frame schema changed")
        for ordinal, cell in enumerate(row.cells):
            candidate = _compact_cell_kind(cell.type_name)
            if candidate is None:
                continue
            if observed[ordinal] is None:
                observed[ordinal] = candidate
            elif observed[ordinal] != candidate:
                raise ProtocolError("compact table frame column type changed")
    if any(item is None for item in observed):
        return None
    return tuple(
        CompactColumn(name, item[0], item[1])
        for name, item in zip(names, observed, strict=True)
        if item is not None
    )


def infer_declared_compact_columns(
    rows: tuple[CollectionRow, ...],
) -> tuple[CompactColumn, ...] | None:
    if not rows:
        return None
    columns: list[CompactColumn] = []
    for row in rows:
        cells = {cell.name: cell for cell in row.cells}
        if set(cells) != {"Имя", "Вид", "Ссылка"}:
            raise ProtocolError("declared compact table schema is invalid")
        name = cells["Имя"].value_string
        kind = cells["Вид"].value_string
        is_reference = cells["Ссылка"].value_boolean
        if not name or is_reference is None:
            raise ProtocolError("declared compact table column is invalid")
        if not kind:
            return None
        if kind not in _SCALAR_KINDS | {"reference"}:
            raise ProtocolError("declared compact table kind is invalid")
        if (kind == "reference") != is_reference:
            raise ProtocolError("declared compact table reference marker is invalid")
        columns.append(CompactColumn(name, kind, is_reference))
    if len({column.name for column in columns}) != len(columns):
        raise ProtocolError("declared compact table column names are invalid")
    return tuple(columns)


def _reference_mode(value: str | ReferenceMode) -> ReferenceMode:
    try:
        return ReferenceMode(value)
    except ValueError as error:
        raise ProtocolError("unknown table reference mode") from error


def build_compact_transfer_instruction(
    handle: str,
    policy: ReferencePolicy,
    context_key: str,
    *,
    runtime_generation: int,
    context_generation: int,
    columns: tuple[CompactColumn, ...] | None = None,
    max_rows: int | None = None,
    max_payload_bytes: int | None = None,
) -> str:
    if not _HANDLE.fullmatch(handle):
        raise ProtocolError(
            "table handle must be one direct or dotted persistent Context path"
        )
    if not _CONTEXT_KEY.fullmatch(context_key):
        raise ProtocolError("compact table context key is invalid")
    if runtime_generation <= 0 or context_generation <= 0:
        raise ProtocolError("table transfer generations must be positive")
    bounded_rows = _optional_budget(max_rows, "table row budget")
    bounded_bytes = _optional_budget(max_payload_bytes, "table byte budget")
    default = _reference_mode(policy.refs)
    overrides = dict(policy.ref_columns or {})
    if columns is not None:
        return _build_specialized_compact_transfer_instruction(
            handle,
            policy,
            context_key,
            runtime_generation=runtime_generation,
            context_generation=context_generation,
            columns=columns,
            max_rows=bounded_rows,
            max_payload_bytes=bounded_bytes,
        )
    lines = ["РежимыСсылокМатериализации = Новый Соответствие;"]
    for column in sorted(overrides):
        if not isinstance(column, str) or not column:
            raise ProtocolError("table reference column name is invalid")
        mode = _reference_mode(overrides[column])
        lines.append(
            "РежимыСсылокМатериализации.Вставить("
            f"{bsl_string_literal(column)}, {bsl_string_literal(mode.value)});"
        )
    lines.extend(
        [
            "КомпактнаяМатериализация = "
            "RuntimeTableTransferServer.СериализоватьКомпактнуюТаблицу("
            f"{handle}, {bsl_string_literal(default.value)}, "
            "РежимыСсылокМатериализации, "
            f"{bounded_rows}, {bounded_bytes});",
            f"Контекст.Вставить({bsl_string_literal(context_key)}, "
            "КомпактнаяМатериализация.Base64);",
            "Результат = "
            f"Формат({runtime_generation}, \"ЧГ=0; ЧДЦ=0\") + \"|\" + "
            f"Формат({context_generation}, \"ЧГ=0; ЧДЦ=0\") + \"|\" + "
            "Формат(КомпактнаяМатериализация.Размер, \"ЧГ=0; ЧДЦ=0\") + \"|\" + "
            "КомпактнаяМатериализация.Хеш + \"|\" + "
            "Формат(СтрДлина(КомпактнаяМатериализация.Base64), "
            "\"ЧГ=0; ЧДЦ=0\");",
        ]
    )
    return "\n".join(lines)


def _optional_budget(value: int | None, label: str) -> int:
    if value is None:
        return 0
    if type(value) is not int or value <= 0:
        raise ProtocolError(f"{label} must be positive")
    return value


def _table_field(row_name: str, column_name: str) -> str:
    if _IDENTIFIER.fullmatch(column_name):
        return f"{row_name}.{column_name}"
    return f"{row_name}[{bsl_string_literal(column_name)}]"


def _metadata_lines(
    context_key: str,
    *,
    runtime_generation: int,
    context_generation: int,
) -> list[str]:
    return [
        f"Контекст.Вставить({bsl_string_literal(context_key)}, "
        "КомпактнаяМатериализация.Base64);",
        "Результат = "
        f"Формат({runtime_generation}, \"ЧГ=0; ЧДЦ=0\") + \"|\" + "
        f"Формат({context_generation}, \"ЧГ=0; ЧДЦ=0\") + \"|\" + "
        "Формат(КомпактнаяМатериализация.Размер, \"ЧГ=0; ЧДЦ=0\") + \"|\" + "
        "КомпактнаяМатериализация.Хеш + \"|\" + "
        "Формат(СтрДлина(КомпактнаяМатериализация.Base64), "
        "\"ЧГ=0; ЧДЦ=0\");",
    ]


def _build_specialized_compact_transfer_instruction(
    handle: str,
    policy: ReferencePolicy,
    context_key: str,
    *,
    runtime_generation: int,
    context_generation: int,
    columns: tuple[CompactColumn, ...],
    max_rows: int,
    max_payload_bytes: int,
) -> str:
    if not columns:
        raise ProtocolError("compact table schema has no columns")
    names = [column.name for column in columns]
    if any(not name for name in names) or len(set(names)) != len(names):
        raise ProtocolError("compact table column names are invalid")
    default = _reference_mode(policy.refs)
    overrides = dict(policy.ref_columns or {})
    references = {column.name for column in columns if column.is_reference}
    unknown = set(overrides) - references
    if unknown:
        raise ProtocolError("unknown reference column: " + ", ".join(sorted(unknown)))

    effective_kinds: list[str] = []
    reference_modes: dict[str, ReferenceMode] = {}
    for column in columns:
        if column.is_reference:
            mode = _reference_mode(overrides.get(column.name, default))
            effective_kinds.append(mode.value)
            reference_modes[column.name] = mode
        else:
            if column.kind not in _SCALAR_KINDS:
                raise ProtocolError(
                    f"unsupported compact scalar kind for column {column.name}"
                )
            effective_kinds.append(column.kind)

    lines = [
        "СтрокиJSONL = Новый Массив;",
        "КолонкиJSONL = Новый Массив;",
        f"МаксимумСтрокМатериализации = {max_rows};",
        f"МаксимумБайтМатериализации = {max_payload_bytes};",
    ]
    lines.extend(
        f"КолонкиJSONL.Добавить({bsl_string_literal(name)});" for name in names
    )
    lines.append("ВидыJSONL = Новый Массив;")
    lines.extend(
        f"ВидыJSONL.Добавить({bsl_string_literal(kind)});"
        for kind in effective_kinds
    )
    lines.append("РежимыСсылокJSONL = Новый Структура;")
    lines.extend(
        "РежимыСсылокJSONL.Вставить("
        f"{bsl_string_literal(name)}, {bsl_string_literal(mode.value)});"
        for name, mode in reference_modes.items()
    )
    lines.extend(
        [
            "СхемаJSONL = Новый Структура;",
            'СхемаJSONL.Вставить("version", 1);',
            'СхемаJSONL.Вставить("columns", КолонкиJSONL);',
            'СхемаJSONL.Вставить("kinds", ВидыJSONL);',
            'СхемаJSONL.Вставить("reference_modes", РежимыСсылокJSONL);',
            "ЗаписьСхемыJSONL = Новый ЗаписьJSON;",
            "ЗаписьСхемыJSONL.УстановитьСтроку("
            "Новый ПараметрыЗаписиJSON(ПереносСтрокJSON.Нет));",
            "ЗаписатьJSON(ЗаписьСхемыJSONL, СхемаJSONL);",
            "СтрокиJSONL.Добавить(ЗаписьСхемыJSONL.Закрыть());",
            "РазмерJSONL = ПолучитьДвоичныеДанныеИзСтроки(СтрокиJSONL[0] + "
            "Символы.ПС, КодировкаТекста.UTF8, Ложь).Размер();",
            "КоличествоСтрокJSONL = 0;",
            "Если МаксимумБайтМатериализации > 0 И "
            "РазмерJSONL > МаксимумБайтМатериализации Тогда",
            '    ВызватьИсключение "Превышен лимит байтов компактной таблицы";',
            "КонецЕсли;",
            "ТаблицаМатериализации = RuntimeTableTransferServer."
            f"ПодготовитьТабличноеЗначение({handle}, "
            "МаксимумСтрокМатериализации);",
            "Для Каждого СтрокаМатериализации Из ТаблицаМатериализации Цикл",
            "    Если МаксимумСтрокМатериализации > 0 И "
            "КоличествоСтрокJSONL >= МаксимумСтрокМатериализации Тогда",
            '        ВызватьИсключение "Превышен лимит строк компактной таблицы";',
            "    КонецЕсли;",
            "    ЗначенияСтроки = Новый Массив;",
        ]
    )
    for ordinal, column in enumerate(columns):
        value = _table_field("СтрокаМатериализации", column.name)
        if not column.is_reference:
            encoded = (
                f"XMLСтрока({value})"
                if column.kind == "datetime"
                else (
                    f"?({value} = Неопределено Или {value} = NULL, "
                    f"Неопределено, Строка({value}))"
                )
                if column.kind == "nullable_string"
                else f"Строка({value})"
                if column.kind == "uuid"
                else value
            )
            lines.append(f"    ЗначенияСтроки.Добавить({encoded});")
            continue
        mode = reference_modes[column.name]
        if mode is ReferenceMode.PRESENTATION:
            encoded = f"?(ЗначениеЗаполнено({value}), Строка({value}), Неопределено)"
            lines.append(f"    ЗначенияСтроки.Добавить({encoded});")
        elif mode is ReferenceMode.UUID:
            encoded = (
                f"?(ЗначениеЗаполнено({value}), "
                f"Строка({value}.УникальныйИдентификатор()), Неопределено)"
            )
            lines.append(f"    ЗначенияСтроки.Добавить({encoded});")
        else:
            temporary = f"ОбеФормыСсылки{ordinal}"
            lines.extend(
                [
                    f"    Если ЗначениеЗаполнено({value}) Тогда",
                    f"        {temporary} = Новый Массив;",
                    f"        {temporary}.Добавить(Строка({value}));",
                    f"        {temporary}.Добавить(Строка("
                    f"{value}.УникальныйИдентификатор()));",
                    "    Иначе",
                    f"        {temporary} = Неопределено;",
                    "    КонецЕсли;",
                    f"    ЗначенияСтроки.Добавить({temporary});",
                ]
            )
    lines.extend(
        [
            "    ЗаписьСтрокиJSONL = Новый ЗаписьJSON;",
            "    ЗаписьСтрокиJSONL.УстановитьСтроку("
            "Новый ПараметрыЗаписиJSON(ПереносСтрокJSON.Нет));",
            "    ЗаписатьJSON(ЗаписьСтрокиJSONL, ЗначенияСтроки);",
            "    СтрокаJSONL = ЗаписьСтрокиJSONL.Закрыть();",
            "    РазмерСтрокиJSONL = ПолучитьДвоичныеДанныеИзСтроки("
            "СтрокаJSONL + Символы.ПС, КодировкаТекста.UTF8, Ложь).Размер();",
            "    Если МаксимумБайтМатериализации > 0 И "
            "РазмерJSONL + РазмерСтрокиJSONL > МаксимумБайтМатериализации Тогда",
            '        ВызватьИсключение "Превышен лимит байтов компактной таблицы";',
            "    КонецЕсли;",
            "    СтрокиJSONL.Добавить(СтрокаJSONL);",
            "    РазмерJSONL = РазмерJSONL + РазмерСтрокиJSONL;",
            "    КоличествоСтрокJSONL = КоличествоСтрокJSONL + 1;",
            "КонецЦикла;",
            "КомпактнаяМатериализация = RuntimeTableTransferServer."
            "ЗавершитьКомпактнуюМатериализацию(СтрокиJSONL);",
        ]
    )
    lines.extend(
        _metadata_lines(
            context_key,
            runtime_generation=runtime_generation,
            context_generation=context_generation,
        )
    )
    return "\n".join(lines)


class CompactRuntimeTableTransfer:
    def __init__(
        self,
        instruction_executor: Callable[[str], object],
        context_reader: Callable[[str, int], str],
        *,
        runtime_generation: Callable[[], int],
        context_generation: int,
        context_cleaner: Callable[[str], None] | None = None,
        schema_reader: Callable[[str], tuple[CompactColumn, ...] | None] | None = None,
        max_text_size: int = 100_000_000,
        max_payload_bytes: int = 75_000_000,
        max_rows: int | None = None,
        key_factory: Callable[[], str] | None = None,
        profiler: PhaseRecorder | None = None,
        capture_executor: Callable[[CaptureTransferPlan], bytes] | None = None,
    ) -> None:
        self._capture_execute = capture_executor
        self._execute = instruction_executor
        self._read = context_reader
        self._runtime_generation = runtime_generation
        self._context_generation = context_generation
        self._clean = context_cleaner
        self._schema_reader = schema_reader
        self._expected_runtime_generation = runtime_generation()
        self._max_text_size = max_text_size
        if max_payload_bytes <= 0:
            raise ValueError("max payload bytes must be positive")
        self._max_payload_bytes = max_payload_bytes
        self._max_rows = _optional_budget(max_rows, "table row budget")
        self._key_factory = key_factory or (
            lambda: "__onec_compact_table_" + uuid4().hex
        )
        self._profiler = profiler

    def _profile(self, phase: str, operation, **metadata):  # type: ignore[no-untyped-def]
        if self._profiler is None:
            return operation()
        return self._profiler.measure(phase, operation, **metadata)

    def to_df(self, handle: str, policy: ReferencePolicy) -> pd.DataFrame:
        payload = self.payload(handle, policy)
        return self._profile(
            "table.build_dataframe",
            lambda: decode_compact_table_payload(payload, policy),
            input_bytes=len(payload),
            item_count=lambda frame: len(frame.index),
        )

    def prepare_payload(self, handle: str, policy: ReferencePolicy) -> CaptureTransferPlan:
        generation = self._runtime_generation()
        if generation != self._expected_runtime_generation:
            raise ProtocolError("table materializer runtime generation is stale")
        key = self._key_factory()
        columns = self._profile(
            "table.schema_read",
            lambda: self._schema_reader(handle) if self._schema_reader else None,
        )
        source = build_compact_transfer_instruction(
            handle,
            policy,
            key,
            runtime_generation=generation,
            context_generation=self._context_generation,
            columns=columns,
            max_rows=self._max_rows or None,
            max_payload_bytes=self._max_payload_bytes,
        )

        def decode(metadata: object, content: str) -> bytes:
            byte_count, base64_count, payload_hash = self._validate_metadata(metadata, generation)
            if len(content) != base64_count:
                raise ProtocolError("compact table metadata is invalid")
            try:
                payload = self._profile(
                    "table.decode_base64",
                    lambda: b64decode("".join(content.split()), validate=True),
                    input_bytes=len(content.encode("ascii")),
                    output_bytes=len,
                )
            except (ValueError, binascii.Error) as error:
                raise ProtocolError("compact table Base64 payload is invalid") from error
            if len(payload) != byte_count or sha256(payload).hexdigest() != payload_hash:
                raise ProtocolError("compact table payload integrity check failed")
            return payload

        return CaptureTransferPlan(
            source, key, f"Контекст.Удалить({bsl_string_literal(key)});\nРезультат = Истина;",
            self._max_text_size, decode,
        )

    def _validate_metadata(self, metadata: object, generation: int) -> tuple[int, int, str]:
        if not isinstance(metadata, str):
            raise ProtocolError("compact table metadata is not a string")
        fields = metadata.split("|")
        if len(fields) != 5:
            raise ProtocolError("compact table metadata field count is invalid")
        try:
            observed_runtime = int(fields[0])
            observed_context = int(fields[1])
            byte_count = int(fields[2])
            base64_count = int(fields[4])
        except ValueError as error:
            raise ProtocolError("compact table metadata number is invalid") from error
        payload_hash = fields[3]
        if (
            observed_runtime != generation
            or observed_context != self._context_generation
            or byte_count <= 0
            or byte_count > self._max_payload_bytes
            or base64_count <= 0
            or base64_count > self._max_text_size
            or not _HASH.fullmatch(payload_hash)
        ):
            raise ProtocolError("compact table metadata is invalid")
        return byte_count, base64_count, payload_hash

    def payload(self, handle: str, policy: ReferencePolicy) -> bytes:
        plan = self.prepare_payload(handle, policy)
        if self._capture_execute is not None:
            return self._capture_execute(plan)
        metadata = self._profile(
            "table.prepare_jsonl", lambda: self._execute(plan.instruction),
            input_bytes=len(plan.instruction.encode("utf-8")),
        )
        try:
            self._validate_metadata(metadata, self._expected_runtime_generation)
        except ProtocolError:
            if self._clean is not None:
                self._clean(plan.private_key)
            raise
        content = self._profile(
            "table.transfer_base64", lambda: self._read(plan.private_key, self._max_text_size),
            output_bytes=lambda value: len(value.encode("ascii")),
        )
        return plan.decode(metadata, content)
