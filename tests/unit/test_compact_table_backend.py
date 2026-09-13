from __future__ import annotations

from base64 import b64encode
from hashlib import sha256
import json

import pytest

from onec_runtime.compact_table_backend import (
    CompactColumn,
    CompactRuntimeTableTransfer,
    build_compact_transfer_instruction,
    infer_declared_compact_columns,
    infer_compact_columns,
)
from onec_runtime.errors import ProtocolError
from onec_runtime.performance_profile import PhaseRecorder
from onec_runtime.rdbg.models import CollectionCell, CollectionRow
from onec_runtime.table_materialization import ReferencePolicy


KEY = "__onec_compact_table_0123456789abcdef0123456789abcdef"


def payload() -> bytes:
    return (
        json.dumps(
            {
                "version": 1,
                "columns": ["Employee"],
                "kinds": ["both"],
                "reference_modes": {"Employee": "both"},
            },
            separators=(",", ":"),
        )
        + "\n"
        + json.dumps(
            [["Alice", "01234567-89ab-cdef-0123-456789abcdef"]],
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def test_builds_one_compact_preparation_with_safe_reference_overrides() -> None:
    source = build_compact_transfer_instruction(
        "Контекст.Таблица",
        ReferencePolicy(refs="uuid", ref_columns={"Employee": "both"}),
        KEY,
        runtime_generation=3,
        context_generation=5,
    )

    assert source.count("СериализоватьКомпактнуюТаблицу") == 1
    assert 'Вставить("Employee", "both")' in source
    assert f'Контекст.Вставить("{KEY}"' in source
    assert "ПолучитьЧасть" not in source
    assert "RuntimeWorker" not in source


def test_builds_compact_transfer_for_validated_tabular_section_path() -> None:
    source = build_compact_transfer_instruction(
        "Контекст.Документ.Товары",
        ReferencePolicy(),
        KEY,
        runtime_generation=3,
        context_generation=5,
    )

    assert "Контекст.Документ.Товары" in source


@pytest.mark.parametrize(
    "handle",
    (
        "Контекст.Документ[0]",
        "Контекст.Документ.Товары()",
        "Контекст.Документ; Сообщить(1)",
    ),
)
def test_rejects_executable_compact_table_path(handle: str) -> None:
    with pytest.raises(ProtocolError, match="handle"):
        build_compact_transfer_instruction(
            handle,
            ReferencePolicy(),
            KEY,
            runtime_generation=3,
            context_generation=5,
        )


def test_reads_one_scalar_and_verifies_compact_payload() -> None:
    content = payload()
    encoded = b64encode(content).decode()
    calls: list[str] = []
    reads: list[tuple[str, int]] = []
    transfer = CompactRuntimeTableTransfer(
        lambda source: calls.append(source)
        or f"3|5|{len(content)}|{sha256(content).hexdigest()}|{len(encoded)}",
        lambda key, maximum: reads.append((key, maximum)) or encoded,
        runtime_generation=lambda: 3,
        context_generation=5,
        schema_reader=lambda handle: (
            CompactColumn("Employee", "reference", is_reference=True),
        ),
        key_factory=lambda: KEY,
    )

    frame = transfer.to_df(
        "Контекст.Таблица",
        ReferencePolicy(refs="uuid", ref_columns={"Employee": "both"}),
    )

    assert frame.shape == (1, 2)
    assert len(calls) == 1
    assert "СериализоватьКомпактнуюТаблицу" not in calls[0]
    assert "СтрокаМатериализации.Employee.УникальныйИдентификатор()" in calls[0]
    assert reads == [(KEY, 100_000_000)]


def test_to_df_keeps_unfilled_table_column_as_missing_values() -> None:
    schema = {
        "version": 1,
        "columns": ["Поле1", "Поле2", "Поле3", "Поле4", "Поле5"],
        "kinds": ["number", "number", "string", "string", "string"],
        "reference_modes": {},
    }
    rows = [
        [index, index + 1, "Сотрудник" + str(index), "Подразделение", None]
        for index in range(1, 21)
    ]
    content = "".join(
        json.dumps(value, ensure_ascii=False) + "\n" for value in [schema, *rows]
    ).encode("utf-8")
    encoded = b64encode(content).decode("ascii")
    calls: list[str] = []
    transfer = CompactRuntimeTableTransfer(
        lambda source: calls.append(source)
        or f"3|5|{len(content)}|{sha256(content).hexdigest()}|{len(encoded)}",
        lambda _key, _maximum: encoded,
        runtime_generation=lambda: 3,
        context_generation=5,
        key_factory=lambda: KEY,
    )

    frame = transfer.to_df("Контекст.ТЗ", ReferencePolicy())

    assert frame.shape == (20, 5)
    assert frame.loc[0, "Поле1"] == 1
    assert frame.loc[19, "Поле3"] == "Сотрудник20"
    assert frame["Поле5"].isna().all()
    assert "СериализоватьКомпактнуюТаблицу" in calls[0]


def test_rejects_payload_integrity_mismatch_after_atomic_take() -> None:
    content = payload()
    encoded = b64encode(content).decode()
    reads: list[str] = []
    transfer = CompactRuntimeTableTransfer(
        lambda _source: f"1|1|{len(content)}|{'0' * 64}|{len(encoded)}",
        lambda key, _maximum: reads.append(key) or encoded,
        runtime_generation=lambda: 1,
        context_generation=1,
        schema_reader=lambda handle: (
            CompactColumn("Employee", "reference", is_reference=True),
        ),
        key_factory=lambda: KEY,
    )

    with pytest.raises(ProtocolError, match="integrity"):
        transfer.to_df("Контекст.Таблица", ReferencePolicy())

    assert reads == [KEY]


def test_rejects_declared_payload_over_byte_budget_before_atomic_take() -> None:
    content = payload()
    encoded = b64encode(content).decode()
    reads: list[str] = []
    transfer = CompactRuntimeTableTransfer(
        lambda _source: f"1|1|{len(content)}|{sha256(content).hexdigest()}|{len(encoded)}",
        lambda key, _maximum: reads.append(key) or encoded,
        runtime_generation=lambda: 1,
        context_generation=1,
        context_cleaner=lambda _key: None,
        schema_reader=lambda _handle: (
            CompactColumn("Employee", "reference", is_reference=True),
        ),
        max_payload_bytes=len(content) - 1,
        key_factory=lambda: KEY,
    )

    with pytest.raises(ProtocolError, match="metadata"):
        transfer.payload("Контекст.Таблица", ReferencePolicy())

    assert reads == []


def test_specialized_instruction_keeps_scalar_conversion_out_of_row_dispatch() -> None:
    source = build_compact_transfer_instruction(
        "Контекст.Таблица",
        ReferencePolicy(refs="uuid"),
        KEY,
        runtime_generation=3,
        context_generation=5,
        columns=(
            CompactColumn("Employee", "reference", is_reference=True),
            CompactColumn("Amount", "number", is_reference=False),
            CompactColumn("Moment", "datetime", is_reference=False),
        ),
    )

    assert "СериализоватьКомпактнуюТаблицу" not in source
    assert "КомпактноеЗначение" not in source
    assert "СтрокаМатериализации.Employee.УникальныйИдентификатор()" in source
    assert "ЗначенияСтроки.Добавить(СтрокаМатериализации.Amount);" in source
    assert "ЗначенияСтроки.Добавить(XMLСтрока(СтрокаМатериализации.Moment));" in source
    assert (
        "ТаблицаМатериализации = RuntimeTableTransferServer."
        "ПодготовитьТабличноеЗначение(Контекст.Таблица, "
        "МаксимумСтрокМатериализации);"
    ) in source
    assert source.count("Для Каждого СтрокаМатериализации Из ТаблицаМатериализации Цикл") == 1


def test_specialized_instruction_enforces_row_and_byte_budgets_before_jsonl_append() -> None:
    source = build_compact_transfer_instruction(
        "Контекст.Таблица",
        ReferencePolicy(refs="presentation"),
        KEY,
        runtime_generation=3,
        context_generation=5,
        columns=(CompactColumn("Amount", "number", is_reference=False),),
        max_rows=12,
        max_payload_bytes=4096,
    )

    assert "МаксимумСтрокМатериализации = 12;" in source
    assert "МаксимумБайтМатериализации = 4096;" in source
    assert (
        "ПодготовитьТабличноеЗначение(Контекст.Таблица, "
        "МаксимумСтрокМатериализации)"
    ) in source
    assert "КоличествоСтрокJSONL >= МаксимумСтрокМатериализации" in source
    assert "РазмерJSONL + РазмерСтрокиJSONL > МаксимумБайтМатериализации" in source
    assert source.index("КоличествоСтрокJSONL >= МаксимумСтрокМатериализации") < source.index(
        "СтрокиJSONL.Добавить(СтрокаJSONL);"
    )
    assert source.index("РазмерJSONL + РазмерСтрокиJSONL > МаксимумБайтМатериализации") < source.index(
        "СтрокиJSONL.Добавить(СтрокаJSONL);"
    )


def test_generic_instruction_passes_budgets_to_server_serializer() -> None:
    source = build_compact_transfer_instruction(
        "Контекст.Таблица",
        ReferencePolicy(),
        KEY,
        runtime_generation=3,
        context_generation=5,
        max_rows=7,
        max_payload_bytes=1024,
    )

    assert 'РежимыСсылокМатериализации, 7, 1024);' in source


def test_infers_one_stable_schema_from_a_small_frame_sample() -> None:
    rows = (
        CollectionRow(
            0,
            (
                CollectionCell(
                    "Employee",
                    "СправочникСсылка.Сотрудники",
                    "Employee A",
                    value_string="01234567-89ab-cdef-0123-456789abcdef",
                ),
                CollectionCell("Amount", "Число", "1", value_decimal="1"),
                CollectionCell("OptionalText", "Неопределено", "Неопределено"),
            ),
        ),
        CollectionRow(
            1,
            (
                CollectionCell(
                    "Employee",
                    "СправочникСсылка.Сотрудники",
                    "Employee B",
                    value_string="11234567-89ab-cdef-0123-456789abcdef",
                ),
                CollectionCell("Amount", "Число", "1.5", value_decimal="1.5"),
                CollectionCell(
                    "OptionalText", "Строка", "value", value_string="value"
                ),
            ),
        ),
    )

    assert infer_compact_columns(rows) == (
        CompactColumn("Employee", "reference", is_reference=True),
        CompactColumn("Amount", "number", is_reference=False),
        CompactColumn("OptionalText", "nullable_string", is_reference=False),
    )


def test_infers_enumeration_as_presentation_without_a_fake_uuid_projection() -> None:
    rows = (
        CollectionRow(
            0,
            (
                CollectionCell(
                    "IncomeKind",
                    "ПеречислениеСсылка.ВидыДоходов",
                    "Заработная плата",
                ),
            ),
        ),
    )

    assert infer_compact_columns(rows) == (
        CompactColumn("IncomeKind", "nullable_string", is_reference=False),
    )


def test_specialized_instruction_converts_enumeration_presentation_to_string() -> None:
    source = build_compact_transfer_instruction(
        "Контекст.Таблица",
        ReferencePolicy(refs="both"),
        KEY,
        runtime_generation=3,
        context_generation=5,
        columns=(
            CompactColumn("IncomeKind", "nullable_string", is_reference=False),
        ),
    )

    assert (
        "?(СтрокаМатериализации.IncomeKind = Неопределено Или "
        "СтрокаМатериализации.IncomeKind = NULL, Неопределено, "
        "Строка(СтрокаМатериализации.IncomeKind))"
    ) in source
    assert "ЗначенияСтроки.Добавить(СтрокаМатериализации.IncomeKind);" not in source


def test_reads_declared_table_schema_without_sampling_values() -> None:
    rows = (
        CollectionRow(
            0,
            (
                CollectionCell("Имя", "Строка", '"Employee"', value_string="Employee"),
                CollectionCell("Вид", "Строка", '"reference"', value_string="reference"),
                CollectionCell("Ссылка", "Булево", "Истина", value_boolean=True),
            ),
        ),
        CollectionRow(
            1,
            (
                CollectionCell("Имя", "Строка", '"Amount"', value_string="Amount"),
                CollectionCell("Вид", "Строка", '"number"', value_string="number"),
                CollectionCell("Ссылка", "Булево", "Ложь", value_boolean=False),
            ),
        ),
    )

    assert infer_declared_compact_columns(rows) == (
        CompactColumn("Employee", "reference", is_reference=True),
        CompactColumn("Amount", "number", is_reference=False),
    )


def test_falls_back_when_declared_table_schema_is_untyped_or_composite() -> None:
    rows = (
        CollectionRow(
            0,
            (
                CollectionCell("Имя", "Строка", '"Value"', value_string="Value"),
                CollectionCell("Вид", "Строка", '""', value_string=""),
                CollectionCell("Ссылка", "Булево", "Ложь", value_boolean=False),
            ),
        ),
    )

    assert infer_declared_compact_columns(rows) is None


def test_records_each_materialization_boundary_without_payload_values() -> None:
    content = payload()
    encoded = b64encode(content).decode()
    recorder = PhaseRecorder()
    transfer = CompactRuntimeTableTransfer(
        lambda _source: (
            f"3|5|{len(content)}|{sha256(content).hexdigest()}|{len(encoded)}"
        ),
        lambda _key, _maximum: encoded,
        runtime_generation=lambda: 3,
        context_generation=5,
        schema_reader=lambda _handle: (
            CompactColumn("Employee", "reference", is_reference=True),
        ),
        key_factory=lambda: KEY,
        profiler=recorder,
    )

    transfer.to_df(
        "Контекст.Таблица",
        ReferencePolicy(refs="uuid", ref_columns={"Employee": "both"}),
    )

    assert [event.phase for event in recorder.events] == [
        "table.schema_read",
        "table.prepare_jsonl",
        "table.transfer_base64",
        "table.decode_base64",
        "table.build_dataframe",
    ]
    assert all(not hasattr(event, "payload") for event in recorder.events)
