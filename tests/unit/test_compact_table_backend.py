from __future__ import annotations

from base64 import b64encode
from hashlib import sha256
from inspect import signature
import json

import pytest

from onec_runtime.compact_table_backend import (
    CompactColumn,
    CompactRuntimeTableTransfer,
    build_compact_transfer_instruction,
    infer_declared_compact_columns,
    infer_compact_columns,
)
from onec_runtime.errors import (
    CaptureValueAccessDeniedError,
    CaptureValueCheckError,
    ProtocolError,
)
from onec_runtime.performance_profile import PhaseRecorder
from onec_runtime.rdbg.models import CollectionCell, CollectionRow
from onec_runtime.table_materialization import ReferencePolicy, TableMaterializationError


KEY = "__onec_compact_table_0123456789abcdef0123456789abcdef"


def test_capture_compact_plan_exists_before_creation_and_caller_does_not_clean():
    seen = []
    expected = payload()
    encoded = b64encode(expected).decode("ascii")
    def execute_plan(plan, evaluation_kind):
        from onec_runtime.capture_evaluation import CaptureEvaluationKind

        assert evaluation_kind is CaptureEvaluationKind.MATERIALIZATION_HELPER
        seen.append(plan)
        assert plan.private_key == KEY
        assert KEY in plan.cleanup_instruction
        assert "Результат = Истина;" in plan.cleanup_instruction
        return plan.decode(f"R|3|5|{len(expected)}|{sha256(expected).hexdigest()}|{len(encoded)}", encoded)
    transfer = CompactRuntimeTableTransfer(
        lambda source: pytest.fail("caller dispatched CAPTURE"),
        lambda key, maximum: pytest.fail("caller read CAPTURE"),
        context_cleaner=lambda key: pytest.fail("caller cleaned CAPTURE"),
        runtime_generation=lambda: 3, context_generation=5, key_factory=lambda: KEY,
        capture_executor=execute_plan,
    )
    assert transfer.payload("e1cRuntimeКонтекст.Таблица", ReferencePolicy(refs="both")) == expected
    assert len(seen) == 1


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
        "e1cRuntimeКонтекст.Таблица",
        ReferencePolicy(refs="uuid", ref_columns={"Employee": "both"}),
        KEY,
        runtime_generation=3,
        context_generation=5,
    )

    assert source.count("СериализоватьКомпактнуюТаблицу") == 1
    assert 'Вставить("Employee", "both")' in source
    assert f'e1cRuntimeКонтекст.Вставить("{KEY}"' in source
    assert "ПолучитьЧасть" not in source
    assert "RuntimeWorker" not in source


def test_compact_instruction_builds_protocol_two_admission_before_publication() -> None:
    source = build_compact_transfer_instruction(
        "e1cRuntimeКонтекст.Таблица",
        ReferencePolicy(),
        KEY,
        runtime_generation=3,
        context_generation=5,
        worker_type_registrations=("Worker.Extension",),
    )

    assert 'ВнешниеОбработки.Создать("Worker.Extension", Ложь)' in source
    assert source.index("ТипыОбъектовWorker") < source.index(
        "RuntimeTableTransferServer.СериализоватьКомпактнуюТаблицу("
    )
    assert "ТипыОбъектовWorker" in source.split(
        "RuntimeTableTransferServer.СериализоватьКомпактнуюТаблицу(", 1
    )[1]
    assert source.index("Если Не Материализация.Доступ Тогда") < source.index(
        f'e1cRuntimeКонтекст.Вставить("{KEY}"'
    )
    assert 'Результат = "D|worker_generation_value"' in source
    assert 'Результат = "E|value_admission_failed"' in source
    assert '"R|" + Формат(3, "ЧГ=0; ЧДЦ=0")' in source


def test_compact_instruction_reports_only_allowlisted_server_failure_codes() -> None:
    source = build_compact_transfer_instruction(
        "e1cRuntimeКонтекст.Таблица", ReferencePolicy(), KEY,
        runtime_generation=3, context_generation=5,
    )

    assert 'Результат = "E|table_row_limit_exceeded"' in source
    assert 'Результат = "E|table_byte_limit_exceeded"' in source
    assert 'Результат = "E|table_column_type_mismatch"' in source
    assert 'Результат = "E|table_unsupported_value_type"' in source
    assert 'Результат = "E|table_row_limit_required"' in source
    assert "ИнформацияОбОшибке().Описание" in source
    assert "Результат = ОписаниеОшибки" not in source


@pytest.mark.parametrize(
    ("metadata", "error_type"),
    [
        ("D|worker_generation_value", CaptureValueAccessDeniedError),
        ("E|value_admission_failed", TableMaterializationError),
        ("3|5|33|" + "0" * 64 + "|44", TableMaterializationError),
    ],
)
def test_nonready_or_predecessor_table_metadata_never_fetches_payload(
    metadata: str, error_type: type[Exception]
) -> None:
    reads: list[str] = []
    transfer = CompactRuntimeTableTransfer(
        lambda _source: metadata,
        lambda key, _maximum: reads.append(key) or "private-payload",
        runtime_generation=lambda: 3,
        context_generation=5,
        key_factory=lambda: KEY,
        context_cleaner=lambda _key: None,
    )

    with pytest.raises(error_type):
        transfer.payload("e1cRuntimeКонтекст.Таблица", ReferencePolicy())

    assert reads == []


@pytest.mark.parametrize(
    ("metadata", "message"),
    [
        ("E|table_row_limit_exceeded", "row limit"),
        ("E|table_byte_limit_exceeded", "byte limit"),
        ("E|table_column_type_mismatch", "incompatible"),
        ("E|table_unsupported_value_type", "unsupported"),
        ("E|table_row_limit_required", "explicit row limit"),
        ("E|value_admission_failed", "1C table serialization failed"),
    ],
)
def test_main_table_failure_has_safe_specific_message_without_payload_fetch(
    metadata: str, message: str
) -> None:
    reads: list[str] = []
    cleaned: list[str] = []
    transfer = CompactRuntimeTableTransfer(
        lambda _source: metadata,
        lambda key, _maximum: reads.append(key) or "private-payload",
        runtime_generation=lambda: 3,
        context_generation=5,
        key_factory=lambda: KEY,
        context_cleaner=cleaned.append,
    )

    with pytest.raises(TableMaterializationError, match=message) as raised:
        transfer.payload("e1cRuntimeКонтекст.Таблица", ReferencePolicy())

    assert "CAPTURE" not in str(raised.value)
    assert reads == []
    assert cleaned == [KEY]


def test_capture_table_failure_keeps_admission_error_type_with_safe_reason() -> None:
    def capture_execute(plan, _kind):
        return plan.admit_metadata("E|table_byte_limit_exceeded")

    transfer = CompactRuntimeTableTransfer(
        lambda _source: pytest.fail("CAPTURE used MAIN executor"),
        lambda _key, _maximum: pytest.fail("CAPTURE fetched payload"),
        runtime_generation=lambda: 3,
        context_generation=5,
        key_factory=lambda: KEY,
        capture_executor=capture_execute,
    )

    with pytest.raises(CaptureValueCheckError, match="byte limit"):
        transfer.payload("e1cRuntimeКонтекст.Таблица", ReferencePolicy())


def test_table_failure_code_rejects_extra_target_text_without_leaking_it() -> None:
    transfer = CompactRuntimeTableTransfer(
        lambda _source: "E|table_row_limit_exceeded|private-value",
        lambda _key, _maximum: pytest.fail("invalid metadata fetched a payload"),
        runtime_generation=lambda: 3,
        context_generation=5,
        key_factory=lambda: KEY,
        context_cleaner=lambda _key: None,
    )

    with pytest.raises(TableMaterializationError) as raised:
        transfer.payload("e1cRuntimeКонтекст.Таблица", ReferencePolicy())

    assert "private-value" not in str(raised.value)


def test_builds_compact_transfer_for_validated_tabular_section_path() -> None:
    source = build_compact_transfer_instruction(
        "e1cRuntimeКонтекст.Документ.Товары",
        ReferencePolicy(),
        KEY,
        runtime_generation=3,
        context_generation=5,
    )

    assert "e1cRuntimeКонтекст.Документ.Товары" in source


@pytest.mark.parametrize(
    "handle",
    (
        "e1cRuntimeКонтекст.Документ[0]",
        "e1cRuntimeКонтекст.Документ.Товары()",
        "e1cRuntimeКонтекст.Документ; Сообщить(1)",
        "RuntimeKernelServer.ПолучитьВременнуюТаблицуОтладки("
        "e1cRuntimeКонтекст.КонтекстОтладки.Результат, \"Итоги\", 0, 10, Новый Массив)",
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
        or f"R|3|5|{len(content)}|{sha256(content).hexdigest()}|{len(encoded)}",
        lambda key, maximum: reads.append((key, maximum)) or encoded,
        runtime_generation=lambda: 3,
        context_generation=5,
        key_factory=lambda: KEY,
    )

    frame = transfer.to_df(
        "e1cRuntimeКонтекст.Таблица",
        ReferencePolicy(refs="uuid", ref_columns={"Employee": "both"}),
    )

    assert frame.shape == (1, 2)
    assert len(calls) == 1
    assert "СериализоватьКомпактнуюТаблицу" in calls[0]
    assert "СтрокаМатериализации.Employee.УникальныйИдентификатор()" not in calls[0]
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
        or f"R|3|5|{len(content)}|{sha256(content).hexdigest()}|{len(encoded)}",
        lambda _key, _maximum: encoded,
        runtime_generation=lambda: 3,
        context_generation=5,
        key_factory=lambda: KEY,
    )

    frame = transfer.to_df("e1cRuntimeКонтекст.ТЗ", ReferencePolicy())

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
        lambda _source: f"R|1|1|{len(content)}|{'0' * 64}|{len(encoded)}",
        lambda key, _maximum: reads.append(key) or encoded,
        runtime_generation=lambda: 1,
        context_generation=1,
        key_factory=lambda: KEY,
    )

    with pytest.raises(ProtocolError, match="integrity"):
        transfer.to_df("e1cRuntimeКонтекст.Таблица", ReferencePolicy())

    assert reads == [KEY]


def test_rejects_declared_payload_over_byte_budget_before_atomic_take() -> None:
    content = payload()
    encoded = b64encode(content).decode()
    reads: list[str] = []
    transfer = CompactRuntimeTableTransfer(
        lambda _source: f"R|1|1|{len(content)}|{sha256(content).hexdigest()}|{len(encoded)}",
        lambda key, _maximum: reads.append(key) or encoded,
        runtime_generation=lambda: 1,
        context_generation=1,
        context_cleaner=lambda _key: None,
        max_payload_bytes=len(content) - 1,
        key_factory=lambda: KEY,
    )

    with pytest.raises(TableMaterializationError, match="invalid"):
        transfer.payload("e1cRuntimeКонтекст.Таблица", ReferencePolicy())

    assert reads == []


def test_generic_table_transport_has_no_specialized_schema_compatibility_surface() -> None:
    assert "columns" not in signature(build_compact_transfer_instruction).parameters
    assert "schema_reader" not in signature(CompactRuntimeTableTransfer).parameters


def test_generic_instruction_passes_budgets_to_server_serializer() -> None:
    source = build_compact_transfer_instruction(
        "e1cRuntimeКонтекст.Таблица",
        ReferencePolicy(),
        KEY,
        runtime_generation=3,
        context_generation=5,
        max_rows=7,
        max_payload_bytes=1024,
    )

    assert 'РежимыСсылокМатериализации, ТипыОбъектовWorker, 7, 1024);' in source


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
            f"R|3|5|{len(content)}|{sha256(content).hexdigest()}|{len(encoded)}"
        ),
        lambda _key, _maximum: encoded,
        runtime_generation=lambda: 3,
        context_generation=5,
        key_factory=lambda: KEY,
        profiler=recorder,
    )

    transfer.to_df(
        "e1cRuntimeКонтекст.Таблица",
        ReferencePolicy(refs="uuid", ref_columns={"Employee": "both"}),
    )

    assert [event.phase for event in recorder.events] == [
        "table.prepare_jsonl",
        "table.transfer_base64",
        "table.decode_base64",
        "table.build_dataframe",
    ]
    assert all(not hasattr(event, "payload") for event in recorder.events)
