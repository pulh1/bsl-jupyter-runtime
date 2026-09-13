from __future__ import annotations

from base64 import b64encode
from datetime import datetime
import json
from uuid import UUID

import pandas as pd
import pytest

from onec_runtime.compact_table import (
    decode_compact_table_base64,
    decode_compact_table_payload,
)
from onec_runtime.table_materialization import (
    ReferencePolicy,
    TableMaterializationError,
)


REFERENCE_ID = "01234567-89ab-cdef-0123-456789abcdef"
OTHER_ID = "fedcba98-7654-3210-fedc-ba9876543210"


def compact_payload(
    *,
    modes: dict[str, str] | None = None,
    rows: list[list[object]] | None = None,
) -> bytes:
    selected_modes = modes or {"Employee": "both", "Department": "uuid"}
    columns = [
        "Name",
        "Active",
        "Count",
        "Amount",
        "Moment",
        "Identifier",
        "Optional",
        "Employee",
        "Department",
    ]
    kinds = [
        "string",
        "boolean",
        "integer",
        "number",
        "datetime",
        "uuid",
        "nullable_string",
        selected_modes["Employee"],
        selected_modes["Department"],
    ]
    schema = {
        "version": 1,
        "columns": columns,
        "kinds": kinds,
        "reference_modes": selected_modes,
    }
    selected_rows = rows or [
        [
            "Alice",
            True,
            10,
            12.5,
            "2026-08-14T12:34:56",
            OTHER_ID,
            None,
            ["Alice Employee", REFERENCE_ID]
            if selected_modes["Employee"] == "both"
            else REFERENCE_ID
            if selected_modes["Employee"] == "uuid"
            else "Alice Employee",
            OTHER_ID
            if selected_modes["Department"] == "uuid"
            else ["Main", OTHER_ID]
            if selected_modes["Department"] == "both"
            else "Main",
        ],
        [
            "Bob",
            False,
            20,
            0.125,
            "2026-08-15T00:00:00",
            REFERENCE_ID,
            "note",
            None,
            None,
        ],
    ]
    return "".join(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
        for value in [schema, *selected_rows]
    ).encode("utf-8")


def test_decodes_typed_columns_and_both_reference_mode() -> None:
    frame = decode_compact_table_payload(
        compact_payload(),
        ReferencePolicy(
            refs="uuid",
            ref_columns={"Employee": "both"},
            uuid_suffix="_id",
        ),
    )

    assert list(frame.columns) == [
        "Name",
        "Active",
        "Count",
        "Amount",
        "Moment",
        "Identifier",
        "Optional",
        "Employee",
        "Employee_id",
        "Department",
    ]
    assert str(frame["Name"].dtype) == "string"
    assert str(frame["Active"].dtype) == "boolean"
    assert str(frame["Count"].dtype) == "Int64"
    assert str(frame["Amount"].dtype) == "Float64"
    assert str(frame["Moment"].dtype) == "datetime64[ns]"
    assert str(frame["Optional"].dtype) == "string"
    assert frame.loc[0, "Moment"] == pd.Timestamp(datetime(2026, 8, 14, 12, 34, 56))
    assert frame.loc[0, "Identifier"] == UUID(OTHER_ID)
    assert frame.loc[0, "Employee"] == "Alice Employee"
    assert frame.loc[0, "Employee_id"] == UUID(REFERENCE_ID)
    assert frame.loc[0, "Department"] == UUID(OTHER_ID)
    assert pd.isna(frame.loc[1, "Employee"])
    assert pd.isna(frame.loc[1, "Employee_id"])
    assert frame.attrs["compact_table_schema"]["version"] == 1
    assert frame.attrs["reference_modes"] == {
        "Employee": "both",
        "Department": "uuid",
    }


@pytest.mark.parametrize(
    ("kind", "expected_dtype"),
    [
        ("string", "string"),
        ("nullable_string", "string"),
        ("boolean", "boolean"),
        ("integer", "Int64"),
        ("number", "Float64"),
        ("datetime", "datetime64[ns]"),
        ("uuid", "object"),
    ],
)
def test_missing_scalar_cell_materializes_as_missing_value(
    kind: str, expected_dtype: str,
) -> None:
    schema = {
        "version": 1,
        "columns": ["Поле5"],
        "kinds": [kind],
        "reference_modes": {},
    }
    payload = (
        json.dumps(schema, ensure_ascii=False) + "\n" + "[null]\n[null]\n"
    ).encode("utf-8")

    frame = decode_compact_table_payload(payload)

    assert frame.shape == (2, 1)
    assert str(frame["Поле5"].dtype) == expected_dtype
    assert frame["Поле5"].isna().all()


@pytest.mark.parametrize(
    "value",
    [datetime(1, 1, 1), datetime(3999, 12, 31, 23, 59, 59)],
)
def test_out_of_nanosecond_range_datetime_preserves_value(value: datetime) -> None:
    schema = {
        "version": 1,
        "columns": ["Момент"],
        "kinds": ["datetime"],
        "reference_modes": {},
    }
    payload = "\n".join(
        json.dumps(item, ensure_ascii=False)
        for item in [schema, [value.isoformat()], [None], ["2026-08-14T12:34:56"]]
    ).encode("utf-8")

    frame = decode_compact_table_payload(payload)

    assert str(frame["Момент"].dtype) == "datetime64[us]"
    assert frame.loc[0, "Момент"].to_pydatetime() == value
    assert pd.isna(frame.loc[1, "Момент"])
    assert frame.loc[2, "Момент"].to_pydatetime() == datetime(2026, 8, 14, 12, 34, 56)


@pytest.mark.parametrize(
    ("mode", "wire_value", "expected"),
    [
        ("presentation", "Alice Employee", "Alice Employee"),
        ("uuid", REFERENCE_ID, UUID(REFERENCE_ID)),
    ],
)
def test_decodes_single_reference_modes(
    mode: str,
    wire_value: object,
    expected: object,
) -> None:
    payload = compact_payload(
        modes={"Employee": mode, "Department": mode},
        rows=[
            [
                "Alice",
                True,
                1,
                1.0,
                "2026-08-14T00:00:00",
                OTHER_ID,
                None,
                wire_value,
                wire_value,
            ]
        ],
    )

    frame = decode_compact_table_payload(payload, ReferencePolicy(refs=mode))

    assert frame.loc[0, "Employee"] == expected


def test_decodes_platform_wrapped_base64() -> None:
    encoded = b64encode(compact_payload()).decode("ascii")
    wrapped = " \r\n".join(encoded[index : index + 80] for index in range(0, len(encoded), 80))

    frame = decode_compact_table_base64(
        wrapped,
        ReferencePolicy(refs="uuid", ref_columns={"Employee": "both"}),
    )

    assert frame.shape == (2, 10)


def mutate_payload(mutator: object) -> bytes:
    documents = [json.loads(line) for line in compact_payload().decode().splitlines()]
    mutator(documents)  # type: ignore[operator]
    return "".join(
        json.dumps(value, separators=(",", ":")) + "\n" for value in documents
    ).encode()


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda docs: docs[0].update(version=2), "schema"),
        (lambda docs: docs[0].update(extra=True), "schema"),
        (lambda docs: docs[0]["columns"].__setitem__(1, "Name"), "duplicate"),
        (lambda docs: docs[0]["reference_modes"].pop("Employee"), "reference"),
        (lambda docs: docs[0]["reference_modes"].update(Name="uuid"), "reference"),
        (lambda docs: docs[1].pop(), "row width"),
        (lambda docs: docs[1].__setitem__(0, 42), "Name"),
        (lambda docs: docs[1].__setitem__(1, 1), "Active"),
        (lambda docs: docs[1].__setitem__(2, True), "Count"),
        (lambda docs: docs[1].__setitem__(2, 2**80), "Count"),
        (lambda docs: docs[1].__setitem__(3, 10**1000), "Amount"),
        (lambda docs: docs[1].__setitem__(4, "secret-not-a-date"), "Moment"),
        (lambda docs: docs[1].__setitem__(5, "secret-not-a-uuid"), "Identifier"),
        (lambda docs: docs[1].__setitem__(6, 42), "Optional"),
        (lambda docs: docs[1].__setitem__(7, ["Employee", "secret-not-a-uuid"]), "Employee"),
    ],
)
def test_rejects_malformed_payload_without_leaking_values(
    mutator: object,
    message: str,
) -> None:
    with pytest.raises(TableMaterializationError, match=message) as captured:
        decode_compact_table_payload(
            mutate_payload(mutator),
            ReferencePolicy(refs="uuid", ref_columns={"Employee": "both"}),
        )

    assert "secret" not in str(captured.value)


def test_rejects_reference_policy_mismatch_and_generated_collision() -> None:
    with pytest.raises(TableMaterializationError, match="reference mode"):
        decode_compact_table_payload(compact_payload(), ReferencePolicy(refs="both"))

    collision = mutate_payload(
        lambda docs: (
            docs[0]["columns"].append("Employee__uuid"),
            docs[0]["kinds"].append("string"),
            [row.append("collision") for row in docs[1:]],
        )
    )
    with pytest.raises(TableMaterializationError, match="collision"):
        decode_compact_table_payload(
            collision,
            ReferencePolicy(refs="uuid", ref_columns={"Employee": "both"}),
        )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"", "empty"),
        (b"\xff", "UTF-8"),
        (b"{}\n\n[]\n", "empty line"),
        (b"not-json\n", "JSON"),
    ],
)
def test_rejects_invalid_jsonl(payload: bytes, message: str) -> None:
    with pytest.raises(TableMaterializationError, match=message):
        decode_compact_table_payload(payload)


def test_rejects_invalid_base64_without_echoing_content() -> None:
    with pytest.raises(TableMaterializationError, match="Base64") as captured:
        decode_compact_table_base64("secret!not-base64")

    assert "secret" not in str(captured.value)
