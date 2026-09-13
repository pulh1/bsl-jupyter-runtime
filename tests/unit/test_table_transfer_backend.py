from __future__ import annotations

from collections import deque

import pytest

from onec_runtime.errors import ProtocolError
from onec_runtime.table_transfer_backend import (
    RuntimeTableTransferBackend,
    close_transfer_instruction,
    get_chunk_instruction,
    get_manifest_instruction,
    start_transfer_instruction,
)


def test_builds_privileged_transfer_commands_without_arbitrary_expression() -> None:
    start = start_transfer_instruction("Контекст.Таблица", 65_536, 3, 5)
    manifest = get_manifest_instruction("01234567-89ab-cdef-0123-456789abcdef")
    chunk = get_chunk_instruction("01234567-89ab-cdef-0123-456789abcdef", 2)
    close = close_transfer_instruction("01234567-89ab-cdef-0123-456789abcdef")

    assert "RuntimeTableTransferServer.СериализоватьТаблицу(Контекст.Таблица" in start
    assert "RuntimeTableTransferServer.СобратьСсылки(Контекст.Таблица)" in start
    assert "RuntimeTableTransferServer.ПолучитьПредставленияСсылок" in start
    assert 'Контекст.Вставить("RuntimeTableTransfers", Новый Соответствие)' in start
    assert "ПоместитьВоВременноеХранилище" in start
    assert "RuntimeWorker" not in start
    assert "МодульПоколение" not in start
    assert "ПолучитьИзВременногоХранилища" in chunk
    assert "RuntimeTableTransferServer.ПолучитьЧасть" in chunk
    assert "УдалитьИзВременногоХранилища" in close
    assert "RuntimeTableTransfers.Удалить" in close
    assert "|" in manifest


def test_manifest_formats_integer_fields_without_locale_grouping() -> None:
    manifest = get_manifest_instruction("01234567-89ab-cdef-0123-456789abcdef")

    assert manifest.count('Формат(') == 5
    assert manifest.count('"ЧГ=0; ЧДЦ=0"') == 5
    assert "Строка(ОписаниеМатериализации." not in manifest


def test_manifest_command_publishes_schema_byte_count() -> None:
    start = start_transfer_instruction("Контекст.Таблица", 65_536, 3, 5)
    manifest = get_manifest_instruction("01234567-89ab-cdef-0123-456789abcdef")

    assert 'ОписаниеМатериализации.Вставить("РазмерСхемы", ' in start
    assert "ОписаниеМатериализации.РазмерСхемы" in manifest


@pytest.mark.parametrize(
    "value",
    [
        "Контекст.Таблица; ВызватьИсключение",
        "Контекст[\"Таблица\"]",
        "Таблица",
        "Контекст.Таблица.Количество()",
    ],
)
def test_rejects_untrusted_table_handle(value: str) -> None:
    with pytest.raises(ProtocolError, match="table handle"):
        start_transfer_instruction(value, 1_024, 3, 5)


def test_backend_parses_manifest_and_chunks_and_closes() -> None:
    replies = deque(
        [
            "01234567-89ab-cdef-0123-456789abcdef",
            "01234567-89ab-cdef-0123-456789abcdef|3|5|10|5|2|"
            + "a" * 64
            + "|"
            + "b" * 64,
            "1|QUJD",
            True,
        ]
    )
    calls: list[str] = []

    def execute(source: str) -> object:
        calls.append(source)
        return replies.popleft()

    backend = RuntimeTableTransferBackend(
        execute,
        runtime_generation=lambda: 3,
        context_generation=5,
    )

    token = backend.start("Контекст.Таблица", 65_536)
    manifest = backend.manifest(token)
    chunk = backend.chunk(token, 1)
    backend.close(token)

    assert manifest.token == token
    assert manifest.runtime_generation == 3
    assert manifest.context_generation == 5
    assert manifest.byte_count == 10
    assert manifest.schema_byte_count == 5
    assert manifest.chunk_count == 2
    assert chunk.sequence == 1
    assert chunk.content_base64 == "QUJD"
    assert len(calls) == 4


def test_backend_rejects_stale_runtime_before_sending_instruction() -> None:
    calls: list[str] = []
    generation = 3
    backend = RuntimeTableTransferBackend(
        lambda source: calls.append(source),
        runtime_generation=lambda: generation,
        context_generation=5,
    )
    generation = 4

    with pytest.raises(ProtocolError, match="runtime generation is stale"):
        backend.start("Контекст.Таблица", 1_024)

    assert calls == []


def test_backend_requires_close_acknowledgement() -> None:
    backend = RuntimeTableTransferBackend(
        lambda _: False,
        runtime_generation=lambda: 3,
        context_generation=5,
    )

    with pytest.raises(ProtocolError, match="close acknowledgement"):
        backend.close("01234567-89ab-cdef-0123-456789abcdef")


def test_backend_closes_transfer_after_runtime_generation_changes() -> None:
    calls: list[str] = []
    generation = 3
    backend = RuntimeTableTransferBackend(
        lambda source: calls.append(source) or True,
        runtime_generation=lambda: generation,
        context_generation=5,
    )
    generation = 4

    backend.close("01234567-89ab-cdef-0123-456789abcdef")

    assert len(calls) == 1
    assert "УдалитьИзВременногоХранилища" in calls[0]
