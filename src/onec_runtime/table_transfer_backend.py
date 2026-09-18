from __future__ import annotations

from collections.abc import Callable
import re
from uuid import UUID

from onec_runtime.errors import ProtocolError
from onec_runtime.experiment import bsl_string_literal
from onec_runtime.table_materialization import (
    TableTransferChunk,
    TableTransferManifest,
)


_HANDLE = re.compile(r"e1cRuntimeКонтекст\.[^\W\d]\w*\Z", re.UNICODE)


def _require_handle(value: str) -> str:
    if not _HANDLE.fullmatch(value):
        raise ProtocolError(
            "table handle must be one direct persistent Context symbol"
        )
    return value


def _token(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as error:
        raise ProtocolError("table transfer token is not a UUID") from error


def start_transfer_instruction(
    table_handle: str,
    chunk_size: int,
    runtime_generation: int,
    context_generation: int,
) -> str:
    handle = _require_handle(table_handle)
    if type(chunk_size) is not int or chunk_size <= 0:
        raise ProtocolError("table transfer chunk size must be positive")
    if runtime_generation <= 0 or context_generation <= 0:
        raise ProtocolError("table transfer generations must be positive")
    return (
        f"СсылкиМатериализации = RuntimeTableTransferServer.СобратьСсылки("
        f"{handle});\n"
        "ПредставленияМатериализации = "
        "RuntimeTableTransferServer.ПолучитьПредставленияСсылок("
        "СсылкиМатериализации);\n"
        f"МатериализацияТаблицы = RuntimeTableTransferServer.СериализоватьТаблицу("
        f"{handle}, {chunk_size}, ПредставленияМатериализации);\n"
        'Если Не e1cRuntimeКонтекст.Свойство("RuntimeTableTransfers") Тогда\n'
        '    e1cRuntimeКонтекст.Вставить("RuntimeTableTransfers", Новый Соответствие);\n'
        "КонецЕсли;\n"
        "ТокенМатериализации = Строка(Новый УникальныйИдентификатор());\n"
        "АдресМатериализации = ПоместитьВоВременноеХранилище("
        "МатериализацияТаблицы.Части);\n"
        "ОписаниеМатериализации = Новый Структура;\n"
        'ОписаниеМатериализации.Вставить("Адрес", АдресМатериализации);\n'
        'ОписаниеМатериализации.Вставить("Размер", МатериализацияТаблицы.Размер);\n'
        'ОписаниеМатериализации.Вставить("РазмерСхемы", '
        "МатериализацияТаблицы.РазмерСхемы);\n"
        'ОписаниеМатериализации.Вставить("КоличествоЧастей", '
        "МатериализацияТаблицы.КоличествоЧастей);\n"
        'ОписаниеМатериализации.Вставить("ХешДанных", '
        "МатериализацияТаблицы.ХешДанных);\n"
        'ОписаниеМатериализации.Вставить("ХешСхемы", '
        "МатериализацияТаблицы.ХешСхемы);\n"
        f'ОписаниеМатериализации.Вставить("RuntimeПоколение", '
        f"{runtime_generation});\n"
        f'ОписаниеМатериализации.Вставить("КонтекстПоколение", '
        f"{context_generation});\n"
        "e1cRuntimeКонтекст.RuntimeTableTransfers.Вставить(ТокенМатериализации, "
        "ОписаниеМатериализации);\n"
        "Результат = ТокенМатериализации;"
    )


def get_manifest_instruction(token: str) -> str:
    literal = bsl_string_literal(_token(token))
    return (
        f"ТокенМатериализации = {literal};\n"
        "ОписаниеМатериализации = e1cRuntimeКонтекст.RuntimeTableTransfers.Получить("
        "ТокенМатериализации);\n"
        "Если ОписаниеМатериализации = Неопределено Тогда\n"
        '    ВызватьИсключение "unknown table transfer token";\n'
        "КонецЕсли;\n"
        "Результат = ТокенМатериализации + \"|\" + "
        "Формат(ОписаниеМатериализации.RuntimeПоколение, \"ЧГ=0; ЧДЦ=0\") + \"|\" + "
        "Формат(ОписаниеМатериализации.КонтекстПоколение, \"ЧГ=0; ЧДЦ=0\") + \"|\" + "
        "Формат(ОписаниеМатериализации.Размер, \"ЧГ=0; ЧДЦ=0\") + \"|\" + "
        "Формат(ОписаниеМатериализации.РазмерСхемы, \"ЧГ=0; ЧДЦ=0\") + \"|\" + "
        "Формат(ОписаниеМатериализации.КоличествоЧастей, \"ЧГ=0; ЧДЦ=0\") + \"|\" + "
        "ОписаниеМатериализации.ХешДанных + \"|\" + "
        "ОписаниеМатериализации.ХешСхемы;"
    )


def get_chunk_instruction(token: str, sequence: int) -> str:
    if type(sequence) is not int or sequence < 0:
        raise ProtocolError("table chunk sequence must be non-negative")
    literal = bsl_string_literal(_token(token))
    return (
        f"ТокенМатериализации = {literal};\n"
        "ОписаниеМатериализации = e1cRuntimeКонтекст.RuntimeTableTransfers.Получить("
        "ТокенМатериализации);\n"
        "Если ОписаниеМатериализации = Неопределено Тогда\n"
        '    ВызватьИсключение "unknown table transfer token";\n'
        "КонецЕсли;\n"
        "ЧастиМатериализации = ПолучитьИзВременногоХранилища("
        "ОписаниеМатериализации.Адрес);\n"
        f"ЧастьМатериализации = RuntimeTableTransferServer.ПолучитьЧасть("
        f"ЧастиМатериализации, {sequence});\n"
        f'Результат = "{sequence}|" + ЧастьМатериализации;'
    )


def close_transfer_instruction(token: str) -> str:
    literal = bsl_string_literal(_token(token))
    return (
        f"ТокенМатериализации = {literal};\n"
        "Если e1cRuntimeКонтекст.Свойство(\"RuntimeTableTransfers\") Тогда\n"
        "    ОписаниеМатериализации = e1cRuntimeКонтекст.RuntimeTableTransfers.Получить("
        "ТокенМатериализации);\n"
        "    Если ОписаниеМатериализации <> Неопределено Тогда\n"
        "        УдалитьИзВременногоХранилища(ОписаниеМатериализации.Адрес);\n"
        "        e1cRuntimeКонтекст.RuntimeTableTransfers.Удалить(ТокенМатериализации);\n"
        "    КонецЕсли;\n"
        "КонецЕсли;\n"
        "Результат = Истина;"
    )


class RuntimeTableTransferBackend:
    def __init__(
        self,
        instruction_executor: Callable[[str], object],
        *,
        runtime_generation: Callable[[], int],
        context_generation: int,
    ) -> None:
        self._execute = instruction_executor
        self._runtime_generation = runtime_generation
        self._context_generation = context_generation
        self._expected_runtime_generation = runtime_generation()

    def _require_current_runtime(self) -> int:
        current = self._runtime_generation()
        if current != self._expected_runtime_generation:
            raise ProtocolError("table materializer runtime generation is stale")
        return current

    def start(self, handle: str, chunk_size: int) -> str:
        generation = self._require_current_runtime()
        result = self._execute(
            start_transfer_instruction(
                handle,
                chunk_size,
                generation,
                self._context_generation,
            )
        )
        if not isinstance(result, str):
            raise ProtocolError("table transfer start did not return a token")
        return _token(result)

    def manifest(self, token: str) -> TableTransferManifest:
        self._require_current_runtime()
        result = self._execute(get_manifest_instruction(token))
        if not isinstance(result, str):
            raise ProtocolError("table transfer manifest is not a string")
        fields = result.split("|")
        if len(fields) != 8:
            raise ProtocolError("table transfer manifest field count is invalid")
        try:
            return TableTransferManifest(
                _token(fields[0]),
                int(fields[1]),
                int(fields[2]),
                int(fields[3]),
                int(fields[4]),
                int(fields[5]),
                fields[6],
                fields[7],
            )
        except ValueError as error:
            raise ProtocolError("table transfer manifest number is invalid") from error

    def chunk(self, token: str, sequence: int) -> TableTransferChunk:
        self._require_current_runtime()
        result = self._execute(get_chunk_instruction(token, sequence))
        if not isinstance(result, str) or "|" not in result:
            raise ProtocolError("table transfer chunk response is invalid")
        sequence_text, content = result.split("|", 1)
        try:
            observed = int(sequence_text)
        except ValueError as error:
            raise ProtocolError("table transfer chunk sequence is invalid") from error
        return TableTransferChunk(observed, content)

    def close(self, token: str) -> None:
        if self._execute(close_transfer_instruction(token)) is not True:
            raise ProtocolError("table transfer close acknowledgement is invalid")
