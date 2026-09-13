from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import json
from uuid import UUID

import pandas as pd
import pytest

from onec_runtime.table_materialization import (
    ReferencePolicy,
    TableMaterializationError,
    decode_table_payload,
)


REFERENCE_ID = "01234567-89ab-cdef-0123-456789abcdef"


def payload(*, duplicate: bool = False) -> bytes:
    columns = [
        {"name": "Имя", "ordinal": 0, "kinds": ["string"], "reference": False},
        {"name": "Активен", "ordinal": 1, "kinds": ["boolean", "undefined"], "reference": False},
        {"name": "Количество", "ordinal": 2, "kinds": ["number"], "reference": False},
        {"name": "Сумма", "ordinal": 3, "kinds": ["number"], "reference": False},
        {"name": "Момент", "ordinal": 4, "kinds": ["datetime", "null"], "reference": False},
        {"name": "Сотрудник", "ordinal": 5, "kinds": ["reference"], "reference": True},
        {"name": "Идентификатор", "ordinal": 6, "kinds": ["uuid"], "reference": False},
    ]
    if duplicate:
        columns[1]["name"] = "Имя"
    schema = {"record": "schema", "version": 1, "columns": columns}
    rows = [
        {
            "record": "row",
            "values": [
                ["string", "А"],
                ["boolean", True],
                ["number", "10"],
                ["number", "12.50"],
                ["datetime", "2026-08-14T12:34:56"],
                [
                    "reference",
                    {
                        "type": "Справочник.Сотрудники",
                        "uuid": REFERENCE_ID,
                        "presentation": "Иванов И.И.",
                        "empty": False,
                    },
                ],
                ["uuid", REFERENCE_ID],
            ],
        },
        {
            "record": "row",
            "values": [
                ["string", "Б"],
                ["undefined", None],
                ["number", "20"],
                ["number", "0.125"],
                ["null", None],
                [
                    "reference",
                    {
                        "type": "Справочник.Сотрудники",
                        "uuid": None,
                        "presentation": "",
                        "empty": True,
                    },
                ],
                ["uuid", "fedcba98-7654-3210-fedc-ba9876543210"],
            ],
        },
    ]
    return "".join(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
        for value in [schema, *rows]
    ).encode("utf-8")


def test_decodes_scalar_types_and_uses_presentation_by_default() -> None:
    frame = decode_table_payload(payload(), ReferencePolicy())

    assert list(frame.columns) == [
        "Имя",
        "Активен",
        "Количество",
        "Сумма",
        "Момент",
        "Сотрудник",
        "Идентификатор",
    ]
    assert str(frame["Имя"].dtype) == "string"
    assert str(frame["Активен"].dtype) == "boolean"
    assert str(frame["Количество"].dtype) == "Int64"
    assert frame.loc[0, "Сумма"] == Decimal("12.50")
    assert frame.loc[1, "Сумма"] == Decimal("0.125")
    assert frame.loc[0, "Момент"] == pd.Timestamp(datetime(2026, 8, 14, 12, 34, 56))
    assert frame.loc[0, "Сотрудник"] == "Иванов И.И."
    assert frame.loc[0, "Идентификатор"] == UUID(REFERENCE_ID)
    assert pd.isna(frame.loc[1, "Активен"])
    assert pd.isna(frame.loc[1, "Момент"])
    assert pd.isna(frame.loc[1, "Сотрудник"])


@pytest.mark.parametrize(
    "value",
    [datetime(1, 1, 1), datetime(3999, 12, 31, 23, 59, 59)],
)
def test_out_of_nanosecond_range_datetime_preserves_value(value: datetime) -> None:
    documents = [json.loads(line) for line in payload().decode("utf-8").splitlines()]
    documents[1]["values"][4] = ["datetime", value.isoformat()]
    altered = "\n".join(
        json.dumps(document, ensure_ascii=False) for document in documents
    ).encode("utf-8")

    frame = decode_table_payload(altered, ReferencePolicy())

    assert str(frame["Момент"].dtype) == "datetime64[us]"
    assert frame.loc[0, "Момент"].to_pydatetime() == value
    assert pd.isna(frame.loc[1, "Момент"])


@pytest.mark.parametrize("mode", ["uuid", "both"])
def test_reference_uuid_modes_preserve_identity(mode: str) -> None:
    frame = decode_table_payload(payload(), ReferencePolicy(refs=mode))

    if mode == "uuid":
        assert frame.loc[0, "Сотрудник"] == UUID(REFERENCE_ID)
        assert "Сотрудник__uuid" not in frame
    else:
        assert frame.loc[0, "Сотрудник"] == "Иванов И.И."
        assert frame.loc[0, "Сотрудник__uuid"] == UUID(REFERENCE_ID)
        assert list(frame.columns)[5:7] == ["Сотрудник", "Сотрудник__uuid"]
        assert pd.isna(frame.loc[1, "Сотрудник__uuid"])


def test_per_column_policy_overrides_global_policy() -> None:
    frame = decode_table_payload(
        payload(),
        ReferencePolicy(refs="uuid", ref_columns={"Сотрудник": "presentation"}),
    )

    assert frame.loc[0, "Сотрудник"] == "Иванов И.И."


@pytest.mark.parametrize(
    ("policy", "message"),
    [
        (ReferencePolicy(refs="raw"), "reference mode"),
        (ReferencePolicy(ref_columns={"НетКолонки": "uuid"}), "unknown reference column"),
        (ReferencePolicy(uuid_suffix=""), "UUID suffix"),
    ],
)
def test_rejects_invalid_reference_policy(policy: ReferencePolicy, message: str) -> None:
    with pytest.raises(TableMaterializationError, match=message):
        decode_table_payload(payload(), policy)


def test_rejects_generated_uuid_column_collision() -> None:
    document = payload().decode("utf-8").splitlines()
    schema = json.loads(document[0])
    schema["columns"].append(
        {"name": "Сотрудник__uuid", "ordinal": 7, "kinds": ["string"], "reference": False}
    )
    for row in document[1:]:
        json.loads(row)["values"].append(["string", "collision"])
    altered = (json.dumps(schema, ensure_ascii=False) + "\n" + "\n".join(document[1:]) + "\n").encode()

    with pytest.raises(TableMaterializationError, match="collision"):
        decode_table_payload(altered, ReferencePolicy(refs="both"))


def test_rejects_duplicate_columns_row_width_and_unsupported_tag() -> None:
    with pytest.raises(TableMaterializationError, match="duplicate column"):
        decode_table_payload(payload(duplicate=True), ReferencePolicy())

    lines = payload().decode().splitlines()
    row = json.loads(lines[1])
    row["values"].pop()
    broken_width = (lines[0] + "\n" + json.dumps(row, ensure_ascii=False) + "\n").encode()
    with pytest.raises(TableMaterializationError, match="row width"):
        decode_table_payload(broken_width, ReferencePolicy())

    row = json.loads(lines[1])
    row["values"][0] = ["opaque", "secret"]
    broken_tag = (lines[0] + "\n" + json.dumps(row, ensure_ascii=False) + "\n").encode()
    with pytest.raises(TableMaterializationError, match="unsupported cell tag"):
        decode_table_payload(broken_tag, ReferencePolicy())
