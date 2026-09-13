from __future__ import annotations

from base64 import b64decode
import binascii
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from hashlib import sha256
import json
import re
from typing import Mapping, Protocol, cast
from uuid import UUID

import pandas as pd


class TableMaterializationError(ValueError):
    """The server table snapshot is malformed or violates the public contract."""


class TableCleanupError(TableMaterializationError):
    def __init__(
        self,
        primary: BaseException | None,
        cleanup: BaseException,
    ) -> None:
        self.primary = primary
        self.cleanup = cleanup
        message = f"table transfer cleanup failed: {cleanup}"
        if primary is not None:
            message = f"{primary}; additionally, {message}"
        super().__init__(message)


class ReferenceMode(str, Enum):
    PRESENTATION = "presentation"
    UUID = "uuid"
    BOTH = "both"


@dataclass(frozen=True, slots=True)
class ReferencePolicy:
    refs: str | ReferenceMode = ReferenceMode.PRESENTATION
    ref_columns: Mapping[str, str | ReferenceMode] | None = None
    uuid_suffix: str = "__uuid"


@dataclass(frozen=True, slots=True)
class TableColumn:
    name: str
    ordinal: int
    kinds: tuple[str, ...]
    reference: bool


@dataclass(frozen=True, slots=True)
class TableSchema:
    version: int
    columns: tuple[TableColumn, ...]


@dataclass(frozen=True, slots=True)
class TableTransferManifest:
    token: str
    runtime_generation: int
    context_generation: int
    byte_count: int
    schema_byte_count: int
    chunk_count: int
    payload_sha256: str
    schema_sha256: str


@dataclass(frozen=True, slots=True)
class TableTransferChunk:
    sequence: int
    content_base64: str


class TableTransferBackend(Protocol):
    def start(self, handle: str, chunk_size: int) -> str: ...

    def manifest(self, token: str) -> TableTransferManifest: ...

    def chunk(self, token: str, sequence: int) -> TableTransferChunk: ...

    def close(self, token: str) -> None: ...


class TableMaterializer:
    def __init__(
        self,
        backend: TableTransferBackend,
        *,
        runtime_generation: int,
        context_generation: int,
        chunk_size: int = 65_536,
    ) -> None:
        if runtime_generation <= 0 or context_generation <= 0:
            raise ValueError("materializer generations must be positive")
        if chunk_size <= 0:
            raise ValueError("chunk size must be positive")
        self.backend = backend
        self.runtime_generation = runtime_generation
        self.context_generation = context_generation
        self.chunk_size = chunk_size

    def to_df(self, handle: str, policy: ReferencePolicy) -> pd.DataFrame:
        if not handle:
            raise TableMaterializationError("table handle must not be empty")
        token: str | None = None
        primary: BaseException | None = None
        try:
            token = self.backend.start(handle, self.chunk_size)
            if not token:
                raise TableMaterializationError("table transfer token is empty")
            manifest = self.backend.manifest(token)
            _validate_manifest(
                manifest,
                token=token,
                runtime_generation=self.runtime_generation,
                context_generation=self.context_generation,
            )
            parts: list[bytes] = []
            for sequence in range(manifest.chunk_count):
                chunk = self.backend.chunk(token, sequence)
                if chunk.sequence != sequence:
                    raise TableMaterializationError(
                        f"table chunk sequence mismatch: expected {sequence}, "
                        f"got {chunk.sequence}"
                    )
                try:
                    content = b64decode(chunk.content_base64, validate=True)
                except (ValueError, binascii.Error) as error:
                    raise TableMaterializationError(
                        f"table chunk {sequence} is not valid Base64"
                    ) from error
                parts.append(content)
            payload = b"".join(parts)
            if len(payload) != manifest.byte_count:
                raise TableMaterializationError(
                    "table payload byte count does not match manifest"
                )
            if sha256(payload).hexdigest() != manifest.payload_sha256:
                raise TableMaterializationError(
                    "table payload SHA-256 does not match manifest"
                )
            if (
                type(manifest.schema_byte_count) is not int
                or manifest.schema_byte_count <= 0
                or manifest.schema_byte_count >= len(payload)
            ):
                raise TableMaterializationError(
                    "table manifest schema byte count is invalid"
                )
            schema_record = payload[: manifest.schema_byte_count]
            delimiter = payload[manifest.schema_byte_count :]
            if not delimiter.startswith((b"\n", b"\r\n")):
                raise TableMaterializationError(
                    "table schema record is not followed by a JSONL delimiter"
                )
            observed_schema_sha256 = sha256(schema_record).hexdigest()
            if observed_schema_sha256 != manifest.schema_sha256:
                raise TableMaterializationError(
                    "table schema SHA-256 does not match manifest: "
                    f"expected={manifest.schema_sha256}, "
                    f"observed={observed_schema_sha256}"
                )
            return decode_table_payload(payload, policy)
        except BaseException as error:
            primary = error
            raise
        finally:
            if token is not None:
                try:
                    self.backend.close(token)
                except BaseException as cleanup:
                    raise TableCleanupError(primary, cleanup) from cleanup


_SCALAR_TAGS = frozenset(
    {"string", "boolean", "number", "datetime", "uuid", "undefined", "null"}
)


def _validate_manifest(
    manifest: TableTransferManifest,
    *,
    token: str,
    runtime_generation: int,
    context_generation: int,
) -> None:
    if manifest.token != token:
        raise TableMaterializationError("table manifest token mismatch")
    if manifest.runtime_generation != runtime_generation:
        raise TableMaterializationError("table manifest runtime generation mismatch")
    if manifest.context_generation != context_generation:
        raise TableMaterializationError("table manifest context generation mismatch")
    if type(manifest.byte_count) is not int or manifest.byte_count <= 0:
        raise TableMaterializationError("table manifest byte count is invalid")
    if type(manifest.chunk_count) is not int or manifest.chunk_count <= 0:
        raise TableMaterializationError("table manifest chunk count is invalid")
    for label, value in (
        ("payload", manifest.payload_sha256),
        ("schema", manifest.schema_sha256),
    ):
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise TableMaterializationError(
                f"table manifest {label} SHA-256 is invalid"
            )


def decode_table_payload(
    payload: bytes,
    policy: ReferencePolicy | None = None,
) -> pd.DataFrame:
    selected_policy = policy or ReferencePolicy()
    records = _read_records(payload)
    schema = _read_schema(records[0])
    modes = _validate_policy(schema, selected_policy)
    decoded_rows = [_decode_row(value, schema, index) for index, value in enumerate(records[1:])]

    result: dict[str, pd.Series] = {}
    for column in schema.columns:
        values = [row[column.ordinal] for row in decoded_rows]
        if column.reference:
            mode = modes[column.name]
            presentations = [
                pd.NA if value is None else cast(dict[str, object], value)["presentation"]
                for value in values
            ]
            identifiers = [
                pd.NA if value is None else cast(dict[str, object], value)["uuid"]
                for value in values
            ]
            if mode is ReferenceMode.UUID:
                result[column.name] = pd.Series(identifiers, dtype="object")
            else:
                result[column.name] = pd.Series(presentations, dtype="string")
                if mode is ReferenceMode.BOTH:
                    result[column.name + selected_policy.uuid_suffix] = pd.Series(
                        identifiers, dtype="object"
                    )
            continue
        result[column.name] = _scalar_series(values, column)

    frame = pd.DataFrame(result)
    frame.attrs["onec_schema"] = schema
    frame.attrs["reference_modes"] = {
        name: mode.value for name, mode in modes.items()
    }
    return frame


def _read_records(payload: bytes) -> list[dict[str, object]]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise TableMaterializationError("table payload is not valid UTF-8") from error
    lines = text.splitlines()
    if not lines:
        raise TableMaterializationError("table payload is empty")
    records: list[dict[str, object]] = []
    for index, line in enumerate(lines, start=1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise TableMaterializationError(
                f"table payload line {index} is not valid JSON"
            ) from error
        if not isinstance(value, dict):
            raise TableMaterializationError(
                f"table payload line {index} is not an object"
            )
        records.append(cast(dict[str, object], value))
    return records


def _read_schema(value: dict[str, object]) -> TableSchema:
    if value.get("record") != "schema" or value.get("version") != 1:
        raise TableMaterializationError("unsupported table schema record")
    raw_columns = value.get("columns")
    if not isinstance(raw_columns, list):
        raise TableMaterializationError("table schema columns are invalid")
    columns: list[TableColumn] = []
    names: set[str] = set()
    for ordinal, raw in enumerate(raw_columns):
        if not isinstance(raw, dict):
            raise TableMaterializationError("table schema column is invalid")
        name = raw.get("name")
        kinds = raw.get("kinds")
        reference = raw.get("reference")
        if (
            not isinstance(name, str)
            or not name
            or raw.get("ordinal") != ordinal
            or not isinstance(kinds, list)
            or not kinds
            or any(not isinstance(kind, str) or not kind for kind in kinds)
            or type(reference) is not bool
        ):
            raise TableMaterializationError(
                f"table schema column {ordinal} is invalid"
            )
        if name in names:
            raise TableMaterializationError(f"duplicate column name: {name}")
        names.add(name)
        columns.append(
            TableColumn(name, ordinal, tuple(cast(list[str], kinds)), cast(bool, reference))
        )
    return TableSchema(1, tuple(columns))


def _mode(value: str | ReferenceMode) -> ReferenceMode:
    try:
        return ReferenceMode(value)
    except ValueError as error:
        raise TableMaterializationError(f"unknown reference mode: {value}") from error


def _validate_policy(
    schema: TableSchema,
    policy: ReferencePolicy,
) -> dict[str, ReferenceMode]:
    if not policy.uuid_suffix:
        raise TableMaterializationError("UUID suffix must not be empty")
    default = _mode(policy.refs)
    reference_names = {column.name for column in schema.columns if column.reference}
    overrides = dict(policy.ref_columns or {})
    unknown = set(overrides) - reference_names
    if unknown:
        raise TableMaterializationError(
            "unknown reference column: " + ", ".join(sorted(unknown))
        )
    modes = {
        name: _mode(overrides.get(name, default)) for name in reference_names
    }
    names = {column.name for column in schema.columns}
    for name, mode in modes.items():
        generated = name + policy.uuid_suffix
        if mode is ReferenceMode.BOTH and generated in names:
            raise TableMaterializationError(
                f"generated UUID column collision: {generated}"
            )
    return modes


def _decode_row(
    value: dict[str, object],
    schema: TableSchema,
    row_index: int,
) -> list[object]:
    raw_values = value.get("values")
    if value.get("record") != "row" or not isinstance(raw_values, list):
        raise TableMaterializationError(f"row {row_index} is invalid")
    if len(raw_values) != len(schema.columns):
        raise TableMaterializationError(f"row width mismatch at row {row_index}")
    return [
        _decode_cell(cell, schema.columns[index], row_index)
        for index, cell in enumerate(raw_values)
    ]


def _decode_cell(value: object, column: TableColumn, row_index: int) -> object:
    if not isinstance(value, list) or len(value) != 2 or not isinstance(value[0], str):
        raise TableMaterializationError(
            f"invalid cell at row {row_index}, column {column.name}"
        )
    tag, raw = value
    if tag not in _SCALAR_TAGS and tag != "reference":
        raise TableMaterializationError(
            f"unsupported cell tag {tag} at row {row_index}, column {column.name}"
        )
    if tag not in column.kinds:
        raise TableMaterializationError(
            f"cell tag {tag} is absent from schema at row {row_index}, column {column.name}"
        )
    if tag in {"undefined", "null"}:
        if raw is not None:
            raise TableMaterializationError(f"{tag} cell must contain null")
        return None
    if tag == "string":
        if not isinstance(raw, str):
            raise TableMaterializationError("string cell value is invalid")
        return raw
    if tag == "boolean":
        if type(raw) is not bool:
            raise TableMaterializationError("boolean cell value is invalid")
        return raw
    if tag == "number":
        if not isinstance(raw, str):
            raise TableMaterializationError("number cell value is invalid")
        try:
            return Decimal(raw)
        except InvalidOperation as error:
            raise TableMaterializationError("number cell is not canonical decimal") from error
    if tag == "datetime":
        if not isinstance(raw, str):
            raise TableMaterializationError("datetime cell value is invalid")
        try:
            return datetime.fromisoformat(raw)
        except ValueError as error:
            raise TableMaterializationError("datetime cell is not ISO-8601") from error
    if tag == "uuid":
        if not isinstance(raw, str):
            raise TableMaterializationError("UUID cell value is invalid")
        try:
            return UUID(raw)
        except ValueError as error:
            raise TableMaterializationError("UUID cell is invalid") from error
    if not column.reference or not isinstance(raw, dict):
        raise TableMaterializationError("reference cell is invalid")
    required = {"type", "uuid", "presentation", "empty"}
    if set(raw) != required:
        raise TableMaterializationError("reference cell fields are invalid")
    type_name = raw.get("type")
    identifier = raw.get("uuid")
    presentation = raw.get("presentation")
    empty = raw.get("empty")
    if (
        not isinstance(type_name, str)
        or not type_name
        or type(presentation) is not str
        or type(empty) is not bool
    ):
        raise TableMaterializationError("reference cell value is invalid")
    if empty:
        if identifier is not None or presentation != "":
            raise TableMaterializationError("empty reference cell is inconsistent")
        return None
    if not isinstance(identifier, str):
        raise TableMaterializationError("reference UUID is missing")
    try:
        parsed = UUID(identifier)
    except ValueError as error:
        raise TableMaterializationError("reference UUID is invalid") from error
    return {"type": type_name, "uuid": parsed, "presentation": presentation}


def _datetime_series(values: list[object]) -> pd.Series:
    nullable = [None if value is None else value for value in values]
    try:
        return pd.Series(pd.to_datetime(nullable), dtype="datetime64[ns]")
    except (pd.errors.OutOfBoundsDatetime, OverflowError):
        # 1C dates can lie outside pandas' nanosecond range (for example,
        # 0001-01-01 and 3999-12-31). Microseconds cover the full 1C range.
        return pd.Series(nullable, dtype="datetime64[us]")


def _scalar_series(values: list[object], column: TableColumn) -> pd.Series:
    non_missing = [value for value in values if value is not None]
    tags = set(column.kinds) - {"undefined", "null"}
    converted = [pd.NA if value is None else value for value in values]
    if tags == {"string"}:
        return pd.Series(converted, dtype="string")
    if tags == {"boolean"}:
        return pd.Series(converted, dtype="boolean")
    if tags == {"number"} and all(
        isinstance(value, Decimal) and value == value.to_integral()
        for value in non_missing
    ):
        integers = [pd.NA if value is None else int(cast(Decimal, value)) for value in values]
        if all(
            value is pd.NA or -(2**63) <= cast(int, value) < 2**63
            for value in integers
        ):
            return pd.Series(integers, dtype="Int64")
    if tags == {"datetime"}:
        return _datetime_series(values)
    return pd.Series(converted, dtype="object")
