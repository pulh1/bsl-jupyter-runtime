"""Bounded, expression-free observation requests for agent operations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum


MAX_OBSERVATIONS = 100
MAX_SELECTION_LIMIT = 10_000
MAX_COLUMNS = 100


class ObservationSourceKind(StrEnum):
    CONTEXT_BINDING = "context_binding"
    FRAME_LOCAL = "frame_local"
    TEMPORARY_TABLE = "temporary_table"
    TEMPORARY_TABLE_MANAGER = "temporary_table_manager"


class ObservationResult(StrEnum):
    PROXY = "proxy"
    PREVIEW = "preview"
    PYTHON = "python"
    DATAFRAME = "dataframe"


class SelectionKind(StrEnum):
    SLICE = "slice"
    TABLE_ROWS = "table_rows"
    FIELDS = "fields"
    KEYS = "keys"


def _identifier(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError(f"{name} must be a bounded identifier")
    if not value.isidentifier():
        raise ValueError(f"{name} must be an identifier without expressions")
    return value


def _qualified_binding(value: object) -> str:
    if not isinstance(value, str) or len(value) > 512:
        raise ValueError("context binding must be bounded")
    parts = value.split(".")
    if len(parts) != 2 or parts[0].casefold() not in {"bsl", "python"}:
        raise ValueError("context binding requires bsl. or python. namespace")
    _identifier(parts[1], name="context binding")
    return value


def _opaque_id(value: object, *, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or any(not (character.isascii() and (character.isalnum() or character in "-_")) for character in value)
    ):
        raise ValueError(f"{name} must be an opaque identifier")
    return value


def _sequence(value: object, *, name: str) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise TypeError(f"{name} must be a sequence")
    result = tuple(_identifier(item, name=name) for item in value)
    if len(result) > MAX_COLUMNS or len(set(item.casefold() for item in result)) != len(result):
        raise ValueError(f"{name} must be unique and bounded")
    return result


@dataclass(frozen=True, slots=True)
class ManagerOrigin:
    namespace: str
    root: str
    fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.namespace not in {"frame", "context"}:
            raise ValueError("manager namespace must be frame or context")
        _identifier(self.root, name="manager root")
        fields = _sequence(self.fields, name="manager fields")
        object.__setattr__(self, "fields", fields)

    @classmethod
    def from_wire(cls, value: object) -> "ManagerOrigin":
        if not isinstance(value, Mapping) or set(value) != {"namespace", "root", "fields"}:
            raise ValueError("manager origin requires namespace, root and fields")
        return cls(
            namespace=value["namespace"],  # type: ignore[arg-type]
            root=value["root"],  # type: ignore[arg-type]
            fields=value["fields"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class ObservationSource:
    kind: ObservationSourceKind
    name: str | None = None
    table: str | None = None
    manager_id: str | None = None
    origin: ManagerOrigin | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ObservationSourceKind):
            raise TypeError("source kind is invalid")
        if self.kind is ObservationSourceKind.CONTEXT_BINDING:
            object.__setattr__(self, "name", _qualified_binding(self.name))
            if self.table is not None or self.manager_id is not None or self.origin is not None:
                raise ValueError("context binding cannot include manager fields")
        elif self.kind is ObservationSourceKind.FRAME_LOCAL:
            object.__setattr__(self, "name", _identifier(self.name, name="frame local"))
            if self.table is not None or self.manager_id is not None or self.origin is not None:
                raise ValueError("frame local cannot include manager fields")
        elif self.kind is ObservationSourceKind.TEMPORARY_TABLE:
            if self.name is not None:
                raise ValueError("temporary table uses table, not name")
            object.__setattr__(self, "table", _identifier(self.table, name="temporary table"))
            object.__setattr__(self, "manager_id", _opaque_id(self.manager_id, name="manager_id"))
            if self.origin is not None:
                raise ValueError("temporary table uses manager_id, not origin")
        else:
            if self.name is not None or self.table is not None or self.manager_id is not None or not isinstance(self.origin, ManagerOrigin):
                raise ValueError("manager source requires only structured origin")

    @classmethod
    def from_wire(cls, value: object) -> "ObservationSource":
        if not isinstance(value, Mapping) or "kind" not in value:
            raise TypeError("observation source must be a mapping")
        allowed = {"kind", "name", "table", "manager_id", "origin"}
        if set(value) - allowed:
            raise ValueError("unsupported observation source field")
        try:
            kind = ObservationSourceKind(value["kind"])
        except (TypeError, ValueError) as error:
            raise ValueError("unknown observation source kind") from error
        origin_value = value.get("origin")
        return cls(
            kind=kind,
            name=value.get("name"),  # type: ignore[arg-type]
            table=value.get("table"),  # type: ignore[arg-type]
            manager_id=value.get("manager_id"),  # type: ignore[arg-type]
            origin=None if origin_value is None else ManagerOrigin.from_wire(origin_value),
        )


@dataclass(frozen=True, slots=True)
class ValueSelection:
    kind: SelectionKind
    offset: int = 0
    limit: int | None = None
    columns: tuple[str, ...] = ()
    names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SelectionKind):
            raise TypeError("selection kind is invalid")
        if type(self.offset) is not int or self.offset < 0:
            raise ValueError("selection offset must be non-negative")
        if self.limit is not None and (
            type(self.limit) is not int
            or self.limit <= 0
            or self.limit > MAX_SELECTION_LIMIT
        ):
            raise ValueError("selection limit must be positive and bounded")
        columns = _sequence(self.columns, name="columns")
        names = _sequence(self.names, name="names")
        object.__setattr__(self, "columns", columns)
        object.__setattr__(self, "names", names)
        if self.kind in {SelectionKind.SLICE, SelectionKind.TABLE_ROWS} and self.limit is None:
            raise ValueError("row and slice selections require a limit")
        if self.kind is SelectionKind.SLICE and (columns or names):
            raise ValueError("slice does not accept fields")
        if self.kind is SelectionKind.TABLE_ROWS and names:
            raise ValueError("table rows use columns")
        if self.kind in {SelectionKind.FIELDS, SelectionKind.KEYS} and (
            self.limit is not None or self.offset != 0 or columns or not names
        ):
            raise ValueError("field/key selection requires names only")

    @classmethod
    def from_wire(cls, value: object) -> "ValueSelection":
        if not isinstance(value, Mapping) or "kind" not in value:
            raise TypeError("selection must be a mapping")
        if set(value) - {"kind", "offset", "limit", "columns", "names"}:
            raise ValueError("unsupported selection field")
        try:
            kind = SelectionKind(value["kind"])
        except (TypeError, ValueError) as error:
            raise ValueError("unknown selection kind") from error
        return cls(
            kind,
            offset=value.get("offset", 0),  # type: ignore[arg-type]
            limit=value.get("limit"),  # type: ignore[arg-type]
            columns=value.get("columns", ()),  # type: ignore[arg-type]
            names=value.get("names", ()),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class ObservationItem:
    alias: str
    source: ObservationSource
    result: ObservationResult = ObservationResult.PROXY
    select: ValueSelection | None = None

    def __post_init__(self) -> None:
        _identifier(self.alias, name="observation alias")
        if not isinstance(self.source, ObservationSource):
            raise TypeError("observation source is invalid")
        if not isinstance(self.result, ObservationResult):
            raise TypeError("observation result is invalid")
        if self.select is not None and not isinstance(self.select, ValueSelection):
            raise TypeError("observation selection is invalid")

    @classmethod
    def from_wire(cls, value: object) -> "ObservationItem":
        if not isinstance(value, Mapping) or set(value) - {"alias", "source", "result", "select"}:
            raise ValueError("observation item has unsupported fields")
        try:
            result = ObservationResult(value.get("result", ObservationResult.PROXY.value))
        except (TypeError, ValueError) as error:
            raise ValueError("unknown observation result") from error
        selection = value.get("select")
        return cls(
            alias=value.get("alias"),  # type: ignore[arg-type]
            source=ObservationSource.from_wire(value.get("source")),
            result=result,
            select=None if selection is None else ValueSelection.from_wire(selection),
        )


@dataclass(frozen=True, slots=True)
class ObservationPlan:
    items: tuple[ObservationItem, ...]
    budget_profile: str = "agent_metadata"

    def __post_init__(self) -> None:
        if isinstance(self.items, str) or not isinstance(self.items, Sequence):
            raise TypeError("observation items must be a sequence")
        items = tuple(self.items)
        if len(items) > MAX_OBSERVATIONS or any(not isinstance(item, ObservationItem) for item in items):
            raise ValueError("observation items must be bounded")
        aliases = tuple(item.alias.casefold() for item in items)
        if len(set(aliases)) != len(aliases):
            raise ValueError("observation aliases must be unique")
        if self.budget_profile not in {"agent_metadata", "agent_preview", "agent_dataframe"}:
            raise ValueError("unknown observation budget profile")
        object.__setattr__(self, "items", items)

    @classmethod
    def from_wire(cls, value: object) -> "ObservationPlan":
        if not isinstance(value, Mapping) or set(value) - {"items", "budget_profile"}:
            raise ValueError("observation plan has unsupported fields")
        raw_items = value.get("items", ())
        if isinstance(raw_items, str) or not isinstance(raw_items, Sequence):
            raise TypeError("observation items must be a sequence")
        return cls(
            tuple(ObservationItem.from_wire(item) for item in raw_items),
            budget_profile=value.get("budget_profile", "agent_metadata"),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class ObservationFailure:
    alias: str
    stage: str
    reason: str


@dataclass(frozen=True, slots=True)
class ObservationOutcome:
    outputs: tuple[tuple[str, str], ...] = ()
    failures: tuple[ObservationFailure, ...] = ()


OBSERVATION_WIRE_DATACLASSES: tuple[type[object], ...] = (
    ManagerOrigin,
    ObservationSource,
    ValueSelection,
    ObservationItem,
    ObservationPlan,
    ObservationFailure,
    ObservationOutcome,
)
