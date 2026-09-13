from __future__ import annotations

from base64 import b64decode
import binascii
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
from types import MappingProxyType
from typing import Any
from uuid import UUID

from onec_runtime.errors import (
    MaterializationCycleError,
    MaterializationKeyError,
    MaterializationLimitError,
    UnsupportedOnecType,
    ValuePayloadError,
)


class _OnecSentinel:
    __slots__ = ("_name",)

    def __init__(self, name: str) -> None:
        self._name = name

    def __repr__(self) -> str:
        return self._name

    def __copy__(self) -> _OnecSentinel:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> _OnecSentinel:
        del memo
        return self


ONEC_UNDEFINED = _OnecSentinel("ONEC_UNDEFINED")
ONEC_NULL = _OnecSentinel("ONEC_NULL")


@dataclass(frozen=True, slots=True)
class MaterializationOptions:
    refs: str = "presentation"
    max_depth: int = 32
    max_items: int = 100_000
    max_bytes: int = 64 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.refs not in {"presentation", "uuid", "both"}:
            raise ValueError("reference mode must be presentation, uuid, or both")
        for name in ("max_depth", "max_items", "max_bytes"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class OnecReference:
    type_name: str
    uuid: UUID | None
    presentation: str
    empty: bool


@dataclass(frozen=True, slots=True)
class OnecEnumValue:
    type_name: str
    name: str
    presentation: str


@dataclass(frozen=True, slots=True)
class OnecObjectSnapshot(Mapping[str, object]):
    type_name: str
    attributes: Mapping[str, object]
    tabular_sections: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.type_name:
            raise ValueError("1C object type name must not be empty")
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))
        object.__setattr__(self, "tabular_sections", tuple(self.tabular_sections))

    def __getitem__(self, key: str) -> object:
        return self.attributes[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.attributes)

    def __len__(self) -> int:
        return len(self.attributes)


@dataclass(frozen=True, slots=True)
class OnecTreeRow:
    values: tuple[object, ...]
    children: tuple[OnecTreeRow, ...] = ()


@dataclass(frozen=True, slots=True)
class OnecTreeSnapshot:
    columns: tuple[str, ...]
    rows: tuple[OnecTreeRow, ...]


def decode_value_payload(
    payload: bytes,
    options: MaterializationOptions | None = None,
) -> object:
    selected = options or MaterializationOptions()
    if not isinstance(payload, bytes):
        raise TypeError("typed value payload must be bytes")
    if len(payload) > selected.max_bytes:
        raise MaterializationLimitError(
            f"materialization bytes limit {selected.max_bytes} exceeded at $"
        )
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValuePayloadError("typed value payload is not valid UTF-8 JSON") from error
    if not isinstance(document, dict) or document.get("version") != 1:
        raise ValuePayloadError("typed value payload version is invalid")
    keys = set(document)
    if keys == {"version", "error"}:
        _raise_wire_error(document["error"])
    if keys != {"version", "root"}:
        raise ValuePayloadError("typed value payload envelope is invalid")
    decoder = _ValueDecoder(selected)
    return decoder.decode(document["root"], path="$", depth=0)


class _ValueDecoder:
    __slots__ = ("options", "items")

    def __init__(self, options: MaterializationOptions) -> None:
        self.options = options
        self.items = 0

    def decode(self, node: object, *, path: str, depth: int) -> object:
        if depth > self.options.max_depth:
            raise MaterializationLimitError(
                f"materialization depth limit {self.options.max_depth} exceeded at {path}"
            )
        if not isinstance(node, dict) or not isinstance(node.get("t"), str):
            raise ValuePayloadError(f"typed value node is invalid at {path}")
        tag = node["t"]
        if tag == "undefined":
            self._exact(node, {"t"}, path)
            return ONEC_UNDEFINED
        if tag == "null":
            self._exact(node, {"t"}, path)
            return ONEC_NULL
        if tag == "string":
            return self._scalar(node, str, path)
        if tag == "boolean":
            return self._scalar(node, bool, path)
        if tag == "number":
            raw = self._scalar(node, str, path)
            try:
                return Decimal(raw)
            except InvalidOperation as error:
                raise ValuePayloadError(f"number is invalid at {path}") from error
        if tag == "datetime":
            raw = self._scalar(node, str, path)
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError as error:
                raise ValuePayloadError(f"datetime is invalid at {path}") from error
        if tag == "uuid":
            raw = self._scalar(node, str, path)
            try:
                return UUID(raw)
            except ValueError as error:
                raise ValuePayloadError(f"UUID is invalid at {path}") from error
        if tag == "binary":
            raw = self._scalar(node, str, path)
            try:
                return b64decode(raw, validate=True)
            except (ValueError, binascii.Error) as error:
                raise ValuePayloadError(f"binary Base64 is invalid at {path}") from error
        if tag in {"array", "fixed_array"}:
            values = self._sequence(node, path)
            decoded = [
                self.decode(value, path=f"{path}[{index}]", depth=depth + 1)
                for index, value in enumerate(values)
            ]
            return tuple(decoded) if tag == "fixed_array" else decoded
        if tag == "structure":
            return self._decode_structure(node, path=path, depth=depth)
        if tag == "map":
            return self._decode_map(node, path=path, depth=depth)
        if tag == "reference":
            return self._decode_reference(node, path)
        if tag == "enum":
            return self._decode_enum(node, path)
        if tag == "object":
            return self._decode_object(node, path=path, depth=depth)
        if tag == "tree":
            return self._decode_tree(node, path=path, depth=depth)
        raise ValuePayloadError(f"unknown typed value tag {tag!r} at {path}")

    def _scalar(self, node: dict[str, object], expected: type, path: str) -> Any:
        self._exact(node, {"t", "v"}, path)
        value = node["v"]
        if type(value) is not expected:
            raise ValuePayloadError(f"typed scalar value is invalid at {path}")
        return value

    def _sequence(self, node: dict[str, object], path: str) -> list[object]:
        self._exact(node, {"t", "v"}, path)
        values = node["v"]
        if not isinstance(values, list):
            raise ValuePayloadError(f"typed collection is invalid at {path}")
        self._add_items(len(values), path)
        return values

    def _decode_structure(
        self, node: dict[str, object], *, path: str, depth: int
    ) -> dict[str, object]:
        pairs = self._sequence(node, path)
        result: dict[str, object] = {}
        normalized: set[str] = set()
        for index, pair in enumerate(pairs):
            if not isinstance(pair, list) or len(pair) != 2 or not isinstance(pair[0], str):
                raise ValuePayloadError(f"structure entry is invalid at {path}[{index}]")
            key = pair[0]
            if key.casefold() in normalized:
                raise ValuePayloadError(f"duplicate structure key at {path}.{key}")
            normalized.add(key.casefold())
            result[key] = self.decode(pair[1], path=f"{path}.{key}", depth=depth + 1)
        return result

    def _decode_map(
        self, node: dict[str, object], *, path: str, depth: int
    ) -> dict[object, object]:
        pairs = self._sequence(node, path)
        result: dict[object, object] = {}
        for index, pair in enumerate(pairs):
            entry_path = f"{path}[{index}]"
            if not isinstance(pair, list) or len(pair) != 2:
                raise ValuePayloadError(f"Map entry is invalid at {entry_path}")
            key = self.decode(pair[0], path=f"{entry_path}.key", depth=depth + 1)
            try:
                hash(key)
            except TypeError as error:
                raise MaterializationKeyError(
                    f"materialized Map key is not hashable at {entry_path}.key"
                ) from error
            if key in result:
                raise MaterializationKeyError(
                    f"materialized Map key collides at {entry_path}.key"
                )
            result[key] = self.decode(
                pair[1], path=f"{entry_path}.value", depth=depth + 1
            )
        return result

    def _decode_reference(self, node: dict[str, object], path: str) -> OnecReference:
        self._exact(node, {"t", "type", "uuid", "presentation", "empty"}, path)
        type_name = node["type"]
        raw_uuid = node["uuid"]
        presentation = node["presentation"]
        empty = node["empty"]
        if (
            not isinstance(type_name, str)
            or not type_name
            or raw_uuid is not None
            and not isinstance(raw_uuid, str)
            or not isinstance(presentation, str)
            or type(empty) is not bool
        ):
            raise ValuePayloadError(f"reference node is invalid at {path}")
        try:
            value_uuid = None if raw_uuid is None else UUID(raw_uuid)
        except ValueError as error:
            raise ValuePayloadError(f"reference UUID is invalid at {path}") from error
        if empty and value_uuid is not None:
            raise ValuePayloadError(f"empty reference has UUID at {path}")
        return OnecReference(type_name, value_uuid, presentation, empty)

    def _decode_enum(self, node: dict[str, object], path: str) -> OnecEnumValue:
        self._exact(node, {"t", "type", "name", "presentation"}, path)
        values = (node["type"], node["name"], node["presentation"])
        if any(not isinstance(value, str) or not value for value in values[:2]) or not isinstance(
            values[2], str
        ):
            raise ValuePayloadError(f"enumeration node is invalid at {path}")
        return OnecEnumValue(values[0], values[1], values[2])

    def _decode_object(
        self, node: dict[str, object], *, path: str, depth: int
    ) -> OnecObjectSnapshot:
        self._exact(node, {"t", "type", "attributes", "sections"}, path)
        type_name = node["type"]
        attributes = node["attributes"]
        sections = node["sections"]
        if not isinstance(type_name, str) or not type_name:
            raise ValuePayloadError(f"object type is invalid at {path}")
        if not isinstance(attributes, list) or not isinstance(sections, list):
            raise ValuePayloadError(f"object node is invalid at {path}")
        self._add_items(len(attributes), path)
        decoded: dict[str, object] = {}
        normalized: set[str] = set()
        for index, pair in enumerate(attributes):
            if not isinstance(pair, list) or len(pair) != 2 or not isinstance(pair[0], str):
                raise ValuePayloadError(f"object attribute is invalid at {path}[{index}]")
            name = pair[0]
            if name.casefold() in normalized:
                raise ValuePayloadError(f"duplicate object attribute at {path}.{name}")
            normalized.add(name.casefold())
            decoded[name] = self.decode(
                pair[1], path=f"{path}.{name}", depth=depth + 1
            )
        if any(not isinstance(section, str) or not section for section in sections):
            raise ValuePayloadError(f"object tabular sections are invalid at {path}")
        if len({section.casefold() for section in sections}) != len(sections):
            raise ValuePayloadError(f"duplicate object tabular section at {path}")
        return OnecObjectSnapshot(type_name, decoded, tuple(sections))

    def _decode_tree(
        self, node: dict[str, object], *, path: str, depth: int
    ) -> OnecTreeSnapshot:
        self._exact(node, {"t", "columns", "rows"}, path)
        columns = node["columns"]
        rows = node["rows"]
        if (
            not isinstance(columns, list)
            or any(not isinstance(column, str) or not column for column in columns)
            or len(set(columns)) != len(columns)
            or not isinstance(rows, list)
        ):
            raise ValuePayloadError(f"tree node is invalid at {path}")
        return OnecTreeSnapshot(
            tuple(columns),
            tuple(
                self._decode_tree_row(
                    row,
                    columns=len(columns),
                    path=f"{path}.rows[{index}]",
                    depth=depth + 1,
                )
                for index, row in enumerate(rows)
            ),
        )

    def _decode_tree_row(
        self,
        row: object,
        *,
        columns: int,
        path: str,
        depth: int,
    ) -> OnecTreeRow:
        if depth > self.options.max_depth:
            raise MaterializationLimitError(
                f"materialization depth limit {self.options.max_depth} exceeded at {path}"
            )
        if not isinstance(row, dict) or set(row) != {"values", "children"}:
            raise ValuePayloadError(f"tree row is invalid at {path}")
        values = row["values"]
        children = row["children"]
        if not isinstance(values, list) or len(values) != columns or not isinstance(
            children, list
        ):
            raise ValuePayloadError(f"tree row shape is invalid at {path}")
        self._add_items(1, path)
        return OnecTreeRow(
            tuple(
                self.decode(value, path=f"{path}.values[{index}]", depth=depth)
                for index, value in enumerate(values)
            ),
            tuple(
                self._decode_tree_row(
                    child,
                    columns=columns,
                    path=f"{path}.children[{index}]",
                    depth=depth + 1,
                )
                for index, child in enumerate(children)
            ),
        )

    def _add_items(self, count: int, path: str) -> None:
        self.items += count
        if self.items > self.options.max_items:
            raise MaterializationLimitError(
                f"materialization items limit {self.options.max_items} exceeded at {path}"
            )

    @staticmethod
    def _exact(node: dict[str, object], expected: set[str], path: str) -> None:
        if set(node) != expected:
            raise ValuePayloadError(f"typed value node fields are invalid at {path}")


def _raise_wire_error(value: object) -> None:
    if not isinstance(value, dict) or not isinstance(value.get("kind"), str):
        raise ValuePayloadError("typed value error envelope is invalid")
    kind = value["kind"]
    path = value.get("path")
    if not isinstance(path, str) or not path:
        raise ValuePayloadError("typed value error path is invalid")
    if kind == "cycle":
        if set(value) != {"kind", "path", "first_path"} or not isinstance(
            value.get("first_path"), str
        ):
            raise ValuePayloadError("typed value cycle envelope is invalid")
        raise MaterializationCycleError(
            f"materialization cycle at {path}; first seen at {value['first_path']}"
        )
    if kind == "unsupported":
        if set(value) != {"kind", "path", "type"} or not isinstance(
            value.get("type"), str
        ):
            raise ValuePayloadError("typed value unsupported envelope is invalid")
        raise UnsupportedOnecType(
            f"unsupported 1C type {value['type']} at {path}"
        )
    if kind in {"depth_limit", "item_limit", "byte_limit"}:
        if set(value) != {"kind", "path", "limit"} or type(value.get("limit")) is not int:
            raise ValuePayloadError("typed value limit envelope is invalid")
        label = kind.removesuffix("_limit").replace("item", "items")
        raise MaterializationLimitError(
            f"materialization {label} limit {value['limit']} exceeded at {path}"
        )
    raise ValuePayloadError(f"unknown typed value error kind {kind!r}")
