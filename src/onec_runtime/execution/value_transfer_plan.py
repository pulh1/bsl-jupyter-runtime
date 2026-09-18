"""Pure BSL instructions and private-payload plans for value materialization."""

from __future__ import annotations

from base64 import b64decode
import binascii
import json
from hashlib import sha256
from typing import Literal

from onec_runtime.capture_evaluation import AdmissionEnvelopeV1, CaptureTransferPlan
from onec_runtime.errors import CaptureValueCheckError, ProtocolError
from onec_runtime.experiment import bsl_string_literal
from onec_runtime.table_materialization import ReferenceMode
from onec_runtime.value_materialization import MaterializationOptions


MAX_PROJECTION_POSITION = 10_000_000
PayloadRoute = Literal["table", "value"]


def classify_materialization_payload(payload: bytes) -> PayloadRoute:
    """Select a decoder only from an already integrity-checked public payload."""

    try:
        header = json.loads(payload.split(b"\n", 1)[0].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProtocolError("materialization payload route is invalid") from error
    if not isinstance(header, dict) or header.get("version") != 1:
        raise ProtocolError("materialization payload route is invalid")
    if set(header) == {"version", "error"} or "root" in header:
        return "value"
    if {"columns", "kinds", "reference_modes"} <= header.keys():
        return "table"
    raise ProtocolError("materialization payload route is invalid")


def build_projection_transfer_plan(
    instruction: str, *, context_key: str, max_bytes: int,
    runtime_generation: int, context_generation: int,
) -> CaptureTransferPlan:
    """Build admission, integrity and cleanup steps without sending RDBG."""

    if type(max_bytes) is not int or max_bytes <= 0:
        raise ProtocolError("projection payload budget must be positive")
    if type(runtime_generation) is not int or runtime_generation <= 0:
        raise ProtocolError("runtime generation must be positive")
    if type(context_generation) is not int or context_generation <= 0:
        raise ProtocolError("context generation must be positive")
    max_base64_chars = ((max_bytes + 2) // 3) * 4

    def envelope(metadata: object) -> AdmissionEnvelopeV1:
        parsed = AdmissionEnvelopeV1.parse(
            metadata, max_payload_bytes=max_bytes, max_base64_chars=max_base64_chars,
        )
        if (
            parsed.runtime_generation != runtime_generation
            or parsed.context_generation != context_generation
        ):
            raise CaptureValueCheckError("CAPTURE value admission result is invalid")
        return parsed

    def admit(metadata: object) -> object:
        envelope(metadata)
        return metadata

    def decode(metadata: object, content: str) -> bytes:
        parsed = envelope(metadata)
        if len(content) != parsed.base64_chars:
            raise CaptureValueCheckError("CAPTURE value admission result is invalid")
        try:
            payload = b64decode("".join(content.split()), validate=True)
        except (ValueError, binascii.Error) as error:
            raise ProtocolError("projection Base64 payload is invalid") from error
        if (
            len(payload) != parsed.payload_bytes
            or sha256(payload).hexdigest() != parsed.payload_sha256
        ):
            raise CaptureValueCheckError("CAPTURE value payload integrity check failed")
        return payload

    return CaptureTransferPlan(
        instruction,
        context_key,
        f"Контекст.Удалить({bsl_string_literal(context_key)});\nРезультат = Истина;",
        max_base64_chars,
        decode,
        admit,
    )


def build_dynamic_materialization_instruction(
    handle: str,
    *,
    context_key: str,
    options: MaterializationOptions,
    refs: str | ReferenceMode,
    ref_columns: dict[str, str | ReferenceMode] | None,
    max_rows: int,
    runtime_generation: int,
    context_generation: int,
    worker_type_registrations: tuple[str, ...],
) -> str:
    """Build one admitted table-or-value serializer instruction."""

    _require_generations(runtime_generation, context_generation)
    if type(max_rows) is not int or max_rows <= 0:
        raise ProtocolError("table row budget must be positive")
    table_mode, overrides = _table_policy(refs, ref_columns)
    _require_registrations(worker_type_registrations, "materialization")
    worker_lines = _worker_type_lines(worker_type_registrations)
    lines = ["Попытка", *worker_lines,
        "Если Не RuntimeValueTransferServer.ДопуститьЗначение("
        f"{handle}, ТипыОбъектовWorker) Тогда",
        '    Результат = "D|worker_generation_value";', "Иначе",
        "    ВидМатериализации = RuntimeValueTransferServer."
        f"ПолучитьВидМатериализации({handle});",
        '    Если ВидМатериализации = "table" Тогда',
        "        РежимыСсылокМатериализации = Новый Соответствие;"]
    lines.extend(_override_lines(overrides, indent="        "))
    lines.extend((
        "        Материализация = RuntimeTableTransferServer.СериализоватьКомпактнуюТаблицу("
        f"{handle}, {bsl_string_literal(table_mode)}, РежимыСсылокМатериализации, "
        f"ТипыОбъектовWorker, {max_rows}, {options.max_bytes});",
        '    ИначеЕсли ВидМатериализации = "value" Тогда',
        "        Материализация = RuntimeValueTransferServer.СериализоватьЗначение("
        f"{handle}, {bsl_string_literal(options.refs)}, {options.max_depth}, "
        f"{options.max_items}, {options.max_bytes}, ТипыОбъектовWorker);",
        "    Иначе", '        Результат = "E|value_admission_failed";', "    КонецЕсли;",
        '    Если ВидМатериализации = "table" Или ВидМатериализации = "value" Тогда',
        *_store_result_lines(context_key, runtime_generation, context_generation, indent="        "),
        "    КонецЕсли;", "КонецЕсли;", "Исключение",
        '    Результат = "E|value_admission_failed";', "КонецПопытки;",
    ))
    return "\n".join(lines)


def build_bounded_projection_instruction(
    handle: str,
    *,
    context_key: str,
    kind: str,
    offset: int,
    limit: int | None,
    columns: tuple[str, ...],
    names: tuple[str, ...],
    refs: str | ReferenceMode = ReferenceMode.PRESENTATION,
    ref_columns: dict[str, str | ReferenceMode] | None = None,
    max_depth: int = 32,
    max_items: int = 100_000,
    max_rows: int = 100_000,
    max_bytes: int = 64 * 1024 * 1024,
    runtime_generation: int = 1,
    context_generation: int = 1,
    worker_type_registrations: tuple[str, ...] = (),
) -> str:
    """Build the bounded BSL selected by ``head(...).to_df()`` and projections."""

    _require_generations(runtime_generation, context_generation)
    _require_registrations(worker_type_registrations, "projection")
    if kind == "table_rows":
        reference_mode, overrides = _table_policy(refs, ref_columns)
    else:
        reference_mode, overrides = "presentation", {}
    end = offset + (limit or 0) - 1
    if kind == "slice":
        projection = ["ПроекцияЗначения = Новый Массив;", _loop(handle, offset, end, "ПроекцияЗначения.Добавить")]
    elif kind == "table_rows":
        selection = "" if not columns else ", " + bsl_string_literal(",".join(columns))
        projection = ["СтрокиПроекции = Новый Массив;", _loop(handle, offset, end, "СтрокиПроекции.Добавить"), f"ПроекцияЗначения = {handle}.Скопировать(СтрокиПроекции{selection});"]
    elif kind == "fields":
        projection = ["ПроекцияЗначения = Новый Структура;", *(
            f"ПроекцияЗначения.Вставить({bsl_string_literal(name)}, {handle}.{name});" for name in names)]
    elif kind == "keys":
        projection = ["ПроекцияЗначения = Новый Соответствие;", *(
            f"ПроекцияЗначения.Вставить({bsl_string_literal(name)}, {handle}.Получить({bsl_string_literal(name)}));" for name in names)]
    else:
        raise ProtocolError("projection kind is unsupported")
    lines = ["Попытка", *_worker_type_lines(worker_type_registrations),
        "Если Не RuntimeValueTransferServer.ДопуститьЗначение("
        f"{handle}, ТипыОбъектовWorker) Тогда", '    Результат = "D|worker_generation_value";',
        "Иначе", *(f"    {line}" for line in projection)]
    if kind == "table_rows":
        lines += ["    РежимыСсылокМатериализации = Новый Соответствие;", *_override_lines(overrides, indent="    "),
            "    Материализация = RuntimeTableTransferServer.СериализоватьКомпактнуюТаблицу("
            f"ПроекцияЗначения, {bsl_string_literal(reference_mode)}, РежимыСсылокМатериализации, "
            f"ТипыОбъектовWorker, {limit or 0}, {max_bytes});"]
    else:
        value_refs = refs.value if isinstance(refs, ReferenceMode) else refs
        lines.append("    Материализация = RuntimeValueTransferServer.СериализоватьЗначение("
            f"ПроекцияЗначения, {bsl_string_literal(value_refs)}, {max_depth}, {max_items}, {max_bytes}, ТипыОбъектовWorker);")
    lines += [*_store_result_lines(context_key, runtime_generation, context_generation, indent="    "), "КонецЕсли;", "Исключение", '    Результат = "E|value_admission_failed";', "КонецПопытки;"]
    return "\n".join(lines)


def bounded_slice(selection: dict[str, object], *, maximum: int) -> tuple[int, int]:
    """Validate the offset/limit shape used by bounded value proxies."""

    if not isinstance(selection, dict) or set(selection) != {"offset", "limit"}:
        raise ProtocolError("bounded projection requires offset and limit")
    offset, limit = selection["offset"], selection["limit"]
    if (type(offset) is not int or type(limit) is not int or offset < 0 or limit <= 0
            or limit > maximum or offset > MAX_PROJECTION_POSITION
            or offset + limit > MAX_PROJECTION_POSITION):
        raise ProtocolError("bounded projection exceeds its limit")
    return offset, limit


def _loop(handle: str, offset: int, end: int, append: str) -> str:
    return f"Для ИндексПроекции = {offset} По Мин({handle}.Количество() - 1, {end}) Цикл\n    {append}({handle}[ИндексПроекции]);\nКонецЦикла;"


def _require_generations(runtime: int, context: int) -> None:
    if type(runtime) is not int or runtime <= 0 or type(context) is not int or context <= 0:
        raise ProtocolError("projection generations must be positive")


def _require_registrations(registrations: tuple[str, ...], label: str) -> None:
    if type(registrations) is not tuple or any(not isinstance(item, str) or not item for item in registrations):
        raise ProtocolError(f"{label} Worker type registrations are invalid")


def _worker_type_lines(registrations: tuple[str, ...]) -> list[str]:
    lines = ["ТипыОбъектовWorker = Новый Массив;"]
    for index, registration in enumerate(registrations):
        lines += [f"ВременныйОбъектWorker{index} = ВнешниеОбработки.Создать({bsl_string_literal(registration)}, Ложь);", f"ТипыОбъектовWorker.Добавить(ТипЗнч(ВременныйОбъектWorker{index}));"]
    return lines


def _table_policy(refs: str | ReferenceMode, columns: dict[str, str | ReferenceMode] | None) -> tuple[str, dict[str, str | ReferenceMode]]:
    try:
        mode = ReferenceMode(refs).value
    except ValueError as error:
        raise ProtocolError("unknown table reference mode") from error
    overrides = dict(columns or {})
    for column, override in overrides.items():
        if not isinstance(column, str) or not column:
            raise ProtocolError("table reference column name is invalid")
        try:
            ReferenceMode(override)
        except ValueError as error:
            raise ProtocolError("unknown table reference mode") from error
    return mode, overrides


def _override_lines(overrides: dict[str, str | ReferenceMode], *, indent: str) -> list[str]:
    return [f"{indent}РежимыСсылокМатериализации.Вставить({bsl_string_literal(column)}, {bsl_string_literal(ReferenceMode(overrides[column]).value)});" for column in sorted(overrides)]


def _store_result_lines(context_key: str, runtime: int, context: int, *, indent: str) -> tuple[str, ...]:
    return (f"{indent}Если Не Материализация.Доступ Тогда", f'{indent}    Результат = "D|worker_generation_value";', f"{indent}Иначе", f"{indent}    Контекст.Вставить({bsl_string_literal(context_key)}, Материализация.Base64);", f'{indent}    Результат = "R|" + Формат({runtime}, "ЧГ=0; ЧДЦ=0") + "|" + Формат({context}, "ЧГ=0; ЧДЦ=0") + "|" + Формат(Материализация.Размер, "ЧГ=0; ЧДЦ=0") + "|" + Материализация.Хеш + "|" + Формат(СтрДлина(Материализация.Base64), "ЧГ=0; ЧДЦ=0");', f"{indent}КонецЕсли;")
