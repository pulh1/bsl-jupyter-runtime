from __future__ import annotations

from base64 import b64decode
import binascii
from dataclasses import dataclass
from datetime import datetime
import json
import math
import re
from typing import Mapping, cast
from uuid import UUID

import pandas as pd

from .table_materialization import (
    ReferenceMode,
    ReferencePolicy,
    TableMaterializationError,
    _datetime_series,
)


_SCHEMA_KEYS = frozenset({"version", "columns", "kinds", "reference_modes"})
_SCALAR_KINDS = frozenset(
    {
        "string",
        "nullable_string",
        "boolean",
        "integer",
        "number",
        "datetime",
        "uuid",
    }
)
_REFERENCE_KINDS = frozenset(mode.value for mode in ReferenceMode)
_ASCII_WHITESPACE = re.compile(r"[ \t\r\n\f\v]")


@dataclass(frozen=True, slots=True)
class CompactTableSchema:
    version: int
    columns: tuple[str, ...]
    kinds: tuple[str, ...]
    reference_modes: Mapping[str, ReferenceMode]


def decode_compact_table_base64(
    content: str,
    policy: ReferencePolicy | None = None,
) -> pd.DataFrame:
    if not isinstance(content, str):
        raise TableMaterializationError("compact table Base64 content is invalid")
    compact = _ASCII_WHITESPACE.sub("", content)
    try:
        payload = b64decode(compact, validate=True)
    except (ValueError, binascii.Error) as error:
        raise TableMaterializationError(
            "compact table content is not valid Base64"
        ) from error
    return decode_compact_table_payload(payload, policy)


def decode_compact_table_payload(
    payload: bytes,
    policy: ReferencePolicy | None = None,
) -> pd.DataFrame:
    lines = _read_lines(payload)
    schema_value = _load_json(lines[0], line_number=1)
    schema = _read_schema(schema_value)
    selected_policy = policy or ReferencePolicy()
    effective_modes = _bind_policy(schema, selected_policy)
    values_by_column: list[list[object]] = [[] for _ in schema.columns]

    for row_number, line in enumerate(lines[1:], start=1):
        raw_row = _load_json(line, line_number=row_number + 1)
        if not isinstance(raw_row, list):
            raise TableMaterializationError(f"compact table row {row_number} is invalid")
        if len(raw_row) != len(schema.columns):
            raise TableMaterializationError(
                f"compact table row width mismatch at row {row_number}"
            )
        for ordinal, raw_value in enumerate(raw_row):
            column = schema.columns[ordinal]
            mode = schema.reference_modes.get(column)
            if mode is None:
                decoded = _decode_scalar(
                    raw_value,
                    kind=schema.kinds[ordinal],
                    row_number=row_number,
                    column=column,
                )
            else:
                decoded = _decode_reference(
                    raw_value,
                    mode=mode,
                    row_number=row_number,
                    column=column,
                )
            values_by_column[ordinal].append(decoded)

    result: dict[str, pd.Series] = {}
    for ordinal, column in enumerate(schema.columns):
        values = values_by_column[ordinal]
        mode = schema.reference_modes.get(column)
        if mode is ReferenceMode.BOTH:
            presentations = [
                pd.NA if value is None else cast(tuple[object, object], value)[0]
                for value in values
            ]
            identifiers = [
                pd.NA if value is None else cast(tuple[object, object], value)[1]
                for value in values
            ]
            result[column] = pd.Series(presentations, dtype="string")
            result[column + selected_policy.uuid_suffix] = pd.Series(
                identifiers,
                dtype="object",
            )
        elif mode is ReferenceMode.PRESENTATION:
            result[column] = pd.Series(
                [pd.NA if value is None else value for value in values],
                dtype="string",
            )
        elif mode is ReferenceMode.UUID:
            result[column] = pd.Series(
                [pd.NA if value is None else value for value in values],
                dtype="object",
            )
        else:
            result[column] = _scalar_series(values, schema.kinds[ordinal])

    frame = pd.DataFrame(result)
    frame.attrs["compact_table_schema"] = {
        "version": schema.version,
        "columns": list(schema.columns),
        "kinds": list(schema.kinds),
        "reference_modes": {
            name: mode.value for name, mode in schema.reference_modes.items()
        },
    }
    frame.attrs["reference_modes"] = {
        name: mode.value for name, mode in effective_modes.items()
    }
    return frame


def _read_lines(payload: bytes) -> list[str]:
    if not isinstance(payload, bytes) or not payload:
        raise TableMaterializationError("compact table payload is empty")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise TableMaterializationError(
            "compact table payload is not valid UTF-8"
        ) from error
    lines = text.splitlines()
    if not lines:
        raise TableMaterializationError("compact table payload is empty")
    if any(not line for line in lines):
        raise TableMaterializationError("compact table payload contains an empty line")
    return lines


def _load_json(line: str, *, line_number: int) -> object:
    try:
        return json.loads(
            line,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (json.JSONDecodeError, ValueError) as error:
        raise TableMaterializationError(
            f"compact table line {line_number} is not valid JSON"
        ) from error


def _read_schema(value: object) -> CompactTableSchema:
    if not isinstance(value, dict) or set(value) != _SCHEMA_KEYS:
        raise TableMaterializationError("compact table schema fields are invalid")
    version = value.get("version")
    raw_columns = value.get("columns")
    raw_kinds = value.get("kinds")
    raw_modes = value.get("reference_modes")
    if type(version) is not int or version != 1:
        raise TableMaterializationError("compact table schema version is unsupported")
    if (
        not isinstance(raw_columns, list)
        or not raw_columns
        or any(not isinstance(name, str) or not name for name in raw_columns)
    ):
        raise TableMaterializationError("compact table schema columns are invalid")
    columns = cast(list[str], raw_columns)
    if len(set(columns)) != len(columns):
        raise TableMaterializationError("compact table schema has a duplicate column")
    if (
        not isinstance(raw_kinds, list)
        or len(raw_kinds) != len(columns)
        or any(not isinstance(kind, str) for kind in raw_kinds)
    ):
        raise TableMaterializationError("compact table schema kinds are invalid")
    kinds = cast(list[str], raw_kinds)
    if not isinstance(raw_modes, dict):
        raise TableMaterializationError(
            "compact table schema reference modes are invalid"
        )
    modes: dict[str, ReferenceMode] = {}
    for name, raw_mode in raw_modes.items():
        if not isinstance(name, str) or name not in columns or not isinstance(raw_mode, str):
            raise TableMaterializationError(
                "compact table schema reference column is invalid"
            )
        try:
            mode = ReferenceMode(raw_mode)
        except ValueError as error:
            raise TableMaterializationError(
                f"compact table schema reference mode is invalid for column {name}"
            ) from error
        if kinds[columns.index(name)] != mode.value:
            raise TableMaterializationError(
                f"compact table schema reference kind is invalid for column {name}"
            )
        modes[name] = mode
    for ordinal, kind in enumerate(kinds):
        name = columns[ordinal]
        if name in modes:
            continue
        if kind not in _SCALAR_KINDS or kind in {
            ReferenceMode.PRESENTATION.value,
            ReferenceMode.BOTH.value,
        }:
            raise TableMaterializationError(
                f"compact table schema reference mode is missing for column {name}"
            )
    return CompactTableSchema(version, tuple(columns), tuple(kinds), modes)


def _bind_policy(
    schema: CompactTableSchema,
    policy: ReferencePolicy,
) -> dict[str, ReferenceMode]:
    if not policy.uuid_suffix:
        raise TableMaterializationError("UUID suffix must not be empty")
    try:
        default = ReferenceMode(policy.refs)
    except ValueError as error:
        raise TableMaterializationError("unknown reference mode") from error
    overrides = dict(policy.ref_columns or {})
    unknown = set(overrides) - set(schema.reference_modes)
    if unknown:
        raise TableMaterializationError(
            "unknown reference column: " + ", ".join(sorted(unknown))
        )
    effective: dict[str, ReferenceMode] = {}
    for name, encoded in schema.reference_modes.items():
        try:
            requested = ReferenceMode(overrides.get(name, default))
        except ValueError as error:
            raise TableMaterializationError(
                f"unknown reference mode for column {name}"
            ) from error
        if requested is not encoded:
            raise TableMaterializationError(
                f"reference mode mismatch for column {name}"
            )
        if requested is ReferenceMode.BOTH:
            generated = name + policy.uuid_suffix
            if generated in schema.columns:
                raise TableMaterializationError(
                    f"generated UUID column collision: {generated}"
                )
        effective[name] = requested
    return effective


def _decode_scalar(
    value: object,
    *,
    kind: str,
    row_number: int,
    column: str,
) -> object:
    invalid = TableMaterializationError(
        f"invalid {kind} at row {row_number}, column {column}"
    )
    # 1C infers a column kind from populated cells and serializes missing
    # Неопределено/NULL cells as JSON null, including an entirely empty column.
    if value is None:
        return None
    if kind in {"string", "nullable_string"}:
        if isinstance(value, str):
            return value
        raise invalid
    if kind == "boolean":
        if type(value) is bool:
            return value
        raise invalid
    if kind == "integer":
        if type(value) is int and -(2**63) <= cast(int, value) < 2**63:
            return value
        raise invalid
    if kind == "number":
        if type(value) not in {int, float}:
            raise invalid
        try:
            number = float(cast(float, value))
        except OverflowError as error:
            raise invalid from error
        if math.isfinite(number):
            return number
        raise invalid
    if kind == "datetime":
        if not isinstance(value, str):
            raise invalid
        try:
            return datetime.fromisoformat(value)
        except ValueError as error:
            raise invalid from error
    if kind == "uuid":
        if not isinstance(value, str):
            raise invalid
        try:
            return UUID(value)
        except ValueError as error:
            raise invalid from error
    raise invalid


def _decode_reference(
    value: object,
    *,
    mode: ReferenceMode,
    row_number: int,
    column: str,
) -> object:
    invalid = TableMaterializationError(
        f"invalid {mode.value} reference at row {row_number}, column {column}"
    )
    if value is None:
        return None
    if mode is ReferenceMode.PRESENTATION:
        if isinstance(value, str):
            return value
        raise invalid
    if mode is ReferenceMode.UUID:
        if not isinstance(value, str):
            raise invalid
        try:
            return UUID(value)
        except ValueError as error:
            raise invalid from error
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not isinstance(value[0], str)
        or not isinstance(value[1], str)
    ):
        raise invalid
    try:
        identifier = UUID(value[1])
    except ValueError as error:
        raise invalid from error
    return value[0], identifier


def _scalar_series(values: list[object], kind: str) -> pd.Series:
    converted = [pd.NA if value is None else value for value in values]
    if kind in {"string", "nullable_string"}:
        return pd.Series(converted, dtype="string")
    if kind == "boolean":
        return pd.Series(converted, dtype="boolean")
    if kind == "integer":
        return pd.Series(converted, dtype="Int64")
    if kind == "number":
        return pd.Series(converted, dtype="Float64")
    if kind == "datetime":
        return _datetime_series(values)
    return pd.Series(converted, dtype="object")
