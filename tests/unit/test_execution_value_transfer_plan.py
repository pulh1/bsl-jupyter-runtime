"""Pure BSL builders for public value-proxy transfer routes."""

import json

from onec_runtime.execution.value_transfer_plan import (
    build_bounded_projection_instruction,
    classify_materialization_payload,
)


def test_payload_classifier_keeps_table_and_value_decoders_on_separate_routes() -> None:
    table = json.dumps(
        {"version": 1, "columns": [], "kinds": [], "reference_modes": []},
        separators=(",", ":"),
    ).encode()
    value = json.dumps(
        {"version": 1, "root": {"kind": "scalar"}}, separators=(",", ":"),
    ).encode()

    assert classify_materialization_payload(table) == "table"
    assert classify_materialization_payload(value) == "value"


def test_head_to_df_builds_only_the_selected_table_rows() -> None:
    source = build_bounded_projection_instruction(
        "e1cRuntimeКонтекст.Таблица",
        context_key="__onec_projection_" + "a" * 32,
        kind="table_rows",
        offset=0,
        limit=2,
        columns=(),
        names=(),
    )

    assert "Для ИндексПроекции = 0 По Мин(e1cRuntimeКонтекст.Таблица.Количество() - 1, 1)" in source
    assert "ПроекцияЗначения = e1cRuntimeКонтекст.Таблица.Скопировать(СтрокиПроекции);" in source
    assert "СериализоватьКомпактнуюТаблицу(" in source
    assert "СериализоватьЗначение(" not in source
