from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import json
from uuid import UUID

import pytest

from onec_runtime.errors import (
    MaterializationCycleError,
    MaterializationKeyError,
    MaterializationLimitError,
    UnsupportedOnecType,
    ValuePayloadError,
)
from onec_runtime.value_materialization import (
    ONEC_NULL,
    ONEC_UNDEFINED,
    MaterializationOptions,
    OnecObjectSnapshot,
    OnecReference,
    OnecTreeSnapshot,
    decode_value_payload,
)


def _payload(root: object) -> bytes:
    return json.dumps(
        {"version": 1, "root": root},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def test_decodes_scalar_and_recursive_container_snapshot() -> None:
    result = decode_value_payload(
        _payload(
            {
                "t": "array",
                "v": [
                    {"t": "undefined"},
                    {"t": "null"},
                    {
                        "t": "structure",
                        "v": [
                            ["Amount", {"t": "number", "v": "12.50"}],
                            [
                                "When",
                                {"t": "datetime", "v": "2026-08-15T12:30:00"},
                            ],
                            ["Enabled", {"t": "boolean", "v": True}],
                        ],
                    },
                ],
            }
        )
    )

    assert result[0] is ONEC_UNDEFINED
    assert result[1] is ONEC_NULL
    assert result[2] == {
        "Amount": Decimal("12.50"),
        "When": datetime(2026, 8, 15, 12, 30),
        "Enabled": True,
    }


def test_decodes_fixed_array_uuid_binary_and_string() -> None:
    result = decode_value_payload(
        _payload(
            {
                "t": "fixed_array",
                "v": [
                    {"t": "string", "v": "данные"},
                    {"t": "uuid", "v": "12345678-1234-5678-1234-567812345678"},
                    {"t": "binary", "v": "AAEC/w=="},
                ],
            }
        )
    )

    assert result == (
        "данные",
        UUID("12345678-1234-5678-1234-567812345678"),
        b"\x00\x01\x02\xff",
    )


def test_reference_is_immutable_hashable_and_usable_as_map_key() -> None:
    reference = {
        "t": "reference",
        "type": "СправочникСсылка.Сотрудники",
        "uuid": "12345678-1234-5678-1234-567812345678",
        "presentation": "Иванов И.И.",
        "empty": False,
    }
    result = decode_value_payload(
        _payload(
            {
                "t": "map",
                "v": [[reference, {"t": "number", "v": "42"}]],
            }
        )
    )

    key = OnecReference(
        type_name="СправочникСсылка.Сотрудники",
        uuid=UUID("12345678-1234-5678-1234-567812345678"),
        presentation="Иванов И.И.",
        empty=False,
    )
    assert result == {key: Decimal("42")}
    assert hash(key)


def test_object_snapshot_is_read_only_mapping_with_recursive_attributes() -> None:
    result = decode_value_payload(
        _payload(
            {
                "t": "object",
                "type": "ДокументОбъект.ВедомостьВКассу",
                "attributes": [
                    ["Номер", {"t": "string", "v": "000001"}],
                    ["Проведен", {"t": "boolean", "v": False}],
                ],
                "sections": ["Зарплата", "Выплаты"],
            }
        )
    )

    assert isinstance(result, OnecObjectSnapshot)
    assert dict(result) == {"Номер": "000001", "Проведен": False}
    assert result["Номер"] == "000001"
    assert result.type_name == "ДокументОбъект.ВедомостьВКассу"
    assert result.tabular_sections == ("Зарплата", "Выплаты")
    with pytest.raises(TypeError):
        result.attributes["Номер"] = "changed"  # type: ignore[index]


def test_tree_snapshot_preserves_nested_rows() -> None:
    result = decode_value_payload(
        _payload(
            {
                "t": "tree",
                "columns": ["Name"],
                "rows": [
                    {
                        "values": [{"t": "string", "v": "root"}],
                        "children": [
                            {
                                "values": [{"t": "string", "v": "child"}],
                                "children": [],
                            }
                        ],
                    }
                ],
            }
        )
    )

    assert isinstance(result, OnecTreeSnapshot)
    assert result.columns == ("Name",)
    assert result.rows[0].values == ("root",)
    assert result.rows[0].children[0].values == ("child",)


@pytest.mark.parametrize(
    ("root", "message"),
    [
        (
            {
                "t": "map",
                "v": [[{"t": "array", "v": []}, {"t": "string", "v": "x"}]],
            },
            "not hashable",
        ),
        (
            {
                "t": "map",
                "v": [
                    [{"t": "number", "v": "1"}, {"t": "string", "v": "a"}],
                    [{"t": "number", "v": "1.0"}, {"t": "string", "v": "b"}],
                ],
            },
            "collides",
        ),
    ],
)
def test_map_fails_closed_for_invalid_python_keys(root: object, message: str) -> None:
    with pytest.raises(MaterializationKeyError, match=message):
        decode_value_payload(_payload(root))


@pytest.mark.parametrize(
    ("error", "error_type"),
    [
        (
            {"kind": "cycle", "path": "$.A[0]", "first_path": "$"},
            MaterializationCycleError,
        ),
        (
            {"kind": "unsupported", "path": "$.Query", "type": "Запрос"},
            UnsupportedOnecType,
        ),
        (
            {"kind": "depth_limit", "path": "$.A[32]", "limit": 32},
            MaterializationLimitError,
        ),
        (
            {"kind": "item_limit", "path": "$.A", "limit": 100000},
            MaterializationLimitError,
        ),
        (
            {"kind": "byte_limit", "path": "$", "limit": 67108864},
            MaterializationLimitError,
        ),
    ],
)
def test_maps_encoder_error_envelopes_to_specific_errors(
    error: dict[str, object], error_type: type[Exception]
) -> None:
    payload = json.dumps({"version": 1, "error": error}).encode("utf-8")
    with pytest.raises(error_type):
        decode_value_payload(payload)


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"version": 2, "root": {"t": "null"}},
        {"version": 1, "root": {"t": "unknown"}},
        {"version": 1, "root": {"t": "number", "v": "not-a-number"}},
        {"version": 1, "root": {"t": "uuid", "v": "bad"}},
        {"version": 1, "root": {"t": "binary", "v": "***"}},
        {"version": 1, "root": {"t": "structure", "v": [["A"]]}},
    ],
)
def test_rejects_malformed_typed_payload(document: object) -> None:
    with pytest.raises(ValuePayloadError):
        decode_value_payload(json.dumps(document).encode("utf-8"))


def test_decoder_enforces_local_depth_item_and_payload_limits() -> None:
    nested = {"t": "array", "v": [{"t": "array", "v": [{"t": "null"}]}]}
    with pytest.raises(MaterializationLimitError, match="depth"):
        decode_value_payload(_payload(nested), MaterializationOptions(max_depth=1))
    with pytest.raises(MaterializationLimitError, match="items"):
        decode_value_payload(
            _payload({"t": "array", "v": [{"t": "null"}, {"t": "null"}]}),
            MaterializationOptions(max_items=1),
        )
    with pytest.raises(MaterializationLimitError, match="bytes"):
        decode_value_payload(
            _payload({"t": "string", "v": "large"}),
            MaterializationOptions(max_bytes=8),
        )
