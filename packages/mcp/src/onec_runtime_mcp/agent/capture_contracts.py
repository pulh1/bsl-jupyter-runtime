"""Immutable, redacted contracts for frame-scoped CAPTURE operations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import isfinite
from typing import TYPE_CHECKING

from onec_runtime.capture_source import CapturePointRequest, ResolvedCapturePoint
from onec_runtime_mcp.agent.observation import ManagerOrigin, ObservationPlan
from onec_runtime_mcp.agent.proxies import (
    MeasurementCost,
    SizeAccuracy,
    ValueBudget,
    ValueSize,
)

if TYPE_CHECKING:
    from onec_runtime_mcp.agent.facade_contracts import RecoveryAction


MAX_CAPTURE_PAGE_SIZE = 100
CAPTURE_MUTABLE_OBJECT_CAVEAT = (
    "live mutable-object mutations are immediate and non-transactional"
)
MAX_CAPTURE_POINTS = 32
MAX_CAPTURE_CAPABILITIES = 32
MAX_CAPTURE_CAPABILITY_LENGTH = 128
MAX_TEMPORARY_TABLE_SCHEMA_FIELDS = 100
MAX_TYPE_NAME = 256


def _opaque_id(value: object, *, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or any(
            not (character.isascii() and (character.isalnum() or character in "-_"))
            for character in value
        )
    ):
        raise ValueError(f"{name} must be an opaque identifier")
    return value


def _identifier(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError(f"{name} must be a bounded identifier")
    if not value.isidentifier():
        raise ValueError(f"{name} must be an identifier without expressions")
    return value


def _qualified_identifier(value: object, *, name: str) -> str:
    if not isinstance(value, str) or len(value) > 512:
        raise ValueError(f"{name} must be bounded")
    parts = value.split(".")
    if not parts:
        raise ValueError(f"{name} must be a qualified identifier")
    for part in parts:
        _identifier(part, name=name)
    return value


def _positive(value: object, *, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _sha256(value: object, *, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")
    return value


def _identifiers(value: object, *, name: str) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise TypeError(f"{name} must be a sequence")
    result = tuple(_identifier(item, name=name) for item in value)
    if len(set(item.casefold() for item in result)) != len(result):
        raise ValueError(f"{name} must be unique")
    return result


def _capabilities(value: object) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise TypeError("capabilities must be a sequence")
    result = tuple(value)
    if len(result) > MAX_CAPTURE_CAPABILITIES:
        raise ValueError("capabilities must be bounded")
    if any(
        not isinstance(item, str)
        or not item
        or len(item) > MAX_CAPTURE_CAPABILITY_LENGTH
        for item in result
    ):
        raise ValueError("capabilities must contain bounded non-empty strings")
    if len(set(result)) != len(result):
        raise ValueError("capabilities must be unique")
    return result


def _require_capture_fence(value: object) -> "CaptureFence":
    if not isinstance(value, CaptureFence):
        raise TypeError("fence must be a CaptureFence")
    return value


def _mapping(value: object, *, name: str, allowed: set[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) - allowed:
        raise ValueError(f"{name} has unsupported fields")
    return value


def _fence_from_wire(value: object) -> "CaptureFence":
    return CaptureFence.from_wire(value)


def _size_from_wire(value: object) -> ValueSize:
    raw = _mapping(
        value,
        name="value size",
        allowed={"items", "rows", "bytes", "accuracy", "cost"},
    )
    return ValueSize(
        items=raw.get("items"),  # type: ignore[arg-type]
        rows=raw.get("rows"),  # type: ignore[arg-type]
        bytes=raw.get("bytes"),  # type: ignore[arg-type]
        accuracy=SizeAccuracy(raw.get("accuracy", "unknown")),  # type: ignore[arg-type]
        cost=MeasurementCost(raw.get("cost", "cheap")),  # type: ignore[arg-type]
    )


@dataclass(frozen=True, slots=True)
class CaptureFence:
    capture_intent_id: str
    operation_id: str
    source_revision: int
    source_sha256: str
    capture_generation: int
    stop_sequence: int

    def __post_init__(self) -> None:
        _opaque_id(self.capture_intent_id, name="capture_intent_id")
        _opaque_id(self.operation_id, name="operation_id")
        _positive(self.source_revision, name="source_revision")
        _sha256(self.source_sha256, name="source_sha256")
        _positive(self.capture_generation, name="capture_generation")
        _positive(self.stop_sequence, name="stop_sequence")

    @classmethod
    def from_wire(cls, value: object) -> "CaptureFence":
        raw = _mapping(
            value,
            name="capture fence",
            allowed={
                "capture_intent_id",
                "operation_id",
                "source_revision",
                "source_sha256",
                "capture_generation",
                "stop_sequence",
            },
        )
        if set(raw) != {
            "capture_intent_id",
            "operation_id",
            "source_revision",
            "source_sha256",
            "capture_generation",
            "stop_sequence",
        }:
            raise ValueError("capture fence requires complete correlation identity")
        return cls(**dict(raw))  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class FrameVariableDescriptor:
    name: str
    type_name: str
    fence: CaptureFence
    capabilities: tuple[str, ...] = ()
    known_size: ValueSize | None = None
    proxy_id: str = ""

    def __post_init__(self) -> None:
        _identifier(self.name, name="frame variable name")
        if not isinstance(self.type_name, str) or not self.type_name or len(self.type_name) > MAX_TYPE_NAME:
            raise ValueError("type_name must be a bounded non-empty string")
        _require_capture_fence(self.fence)
        object.__setattr__(self, "capabilities", _capabilities(self.capabilities))
        if self.known_size is not None and not isinstance(self.known_size, ValueSize):
            raise TypeError("known_size must be a ValueSize")
        if self.proxy_id and not isinstance(self.proxy_id, str):
            raise TypeError("proxy_id must be a string")

    @classmethod
    def from_wire(cls, value: object) -> "FrameVariableDescriptor":
        raw = _mapping(
            value,
            name="frame variable descriptor",
            allowed={"name", "type_name", "fence", "capabilities", "known_size", "proxy_id"},
        )
        size = raw.get("known_size")
        return cls(
            name=raw.get("name"),  # type: ignore[arg-type]
            type_name=raw.get("type_name"),  # type: ignore[arg-type]
            fence=_fence_from_wire(raw.get("fence")),
            capabilities=raw.get("capabilities", ()),  # type: ignore[arg-type]
            known_size=None if size is None else _size_from_wire(size),
            proxy_id=raw.get("proxy_id", ""),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class TemporaryTableManagerDescriptor:
    manager_id: str
    origin: ManagerOrigin
    fence: CaptureFence
    capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _opaque_id(self.manager_id, name="manager_id")
        if not isinstance(self.origin, ManagerOrigin) or self.origin.namespace != "frame":
            raise ValueError("temporary-table manager origin must be frame-scoped")
        _require_capture_fence(self.fence)
        object.__setattr__(self, "capabilities", _capabilities(self.capabilities))

    @classmethod
    def from_wire(cls, value: object) -> "TemporaryTableManagerDescriptor":
        raw = _mapping(
            value,
            name="temporary table manager descriptor",
            allowed={"manager_id", "origin", "fence", "capabilities"},
        )
        return cls(
            manager_id=raw.get("manager_id"),  # type: ignore[arg-type]
            origin=ManagerOrigin.from_wire(raw.get("origin")),
            fence=_fence_from_wire(raw.get("fence")),
            capabilities=raw.get("capabilities", ()),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class TemporaryTableDescriptor:
    table_id: str
    manager_id: str
    name: str
    fence: CaptureFence
    schema: tuple[str, ...] = ()
    known_size: ValueSize | None = None
    capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _opaque_id(self.table_id, name="table_id")
        _opaque_id(self.manager_id, name="manager_id")
        _identifier(self.name, name="temporary table name")
        _require_capture_fence(self.fence)
        schema = _identifiers(self.schema, name="schema")
        if len(schema) > MAX_TEMPORARY_TABLE_SCHEMA_FIELDS:
            raise ValueError("schema must be bounded")
        object.__setattr__(self, "schema", schema)
        object.__setattr__(self, "capabilities", _capabilities(self.capabilities))
        if self.known_size is not None and not isinstance(self.known_size, ValueSize):
            raise TypeError("known_size must be a ValueSize")

    @classmethod
    def from_wire(cls, value: object) -> "TemporaryTableDescriptor":
        raw = _mapping(
            value,
            name="temporary table descriptor",
            allowed={
                "table_id",
                "manager_id",
                "name",
                "fence",
                "schema",
                "known_size",
                "capabilities",
            },
        )
        size = raw.get("known_size")
        return cls(
            table_id=raw.get("table_id"),  # type: ignore[arg-type]
            manager_id=raw.get("manager_id"),  # type: ignore[arg-type]
            name=raw.get("name"),  # type: ignore[arg-type]
            fence=_fence_from_wire(raw.get("fence")),
            schema=raw.get("schema", ()),  # type: ignore[arg-type]
            known_size=None if size is None else _size_from_wire(size),
            capabilities=raw.get("capabilities", ()),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class CaptureInspection:
    fence: CaptureFence
    variables: tuple[FrameVariableDescriptor, ...]
    temporary_table_managers: tuple[TemporaryTableManagerDescriptor, ...]
    temporary_tables: tuple[TemporaryTableDescriptor, ...]
    cursor: int
    limit: int
    total_variables: int
    next_cursor: int | None
    truncated: bool

    def __post_init__(self) -> None:
        if not isinstance(self.fence, CaptureFence):
            raise TypeError("fence must be a CaptureFence")
        for name in ("variables", "temporary_table_managers", "temporary_tables"):
            value = getattr(self, name)
            if isinstance(value, str) or not isinstance(value, Sequence):
                raise TypeError(f"{name} must be a sequence")
            object.__setattr__(self, name, tuple(value))
        if any(not isinstance(item, FrameVariableDescriptor) for item in self.variables):
            raise TypeError("variables must contain FrameVariableDescriptor values")
        if any(
            not isinstance(item, TemporaryTableManagerDescriptor)
            for item in self.temporary_table_managers
        ):
            raise TypeError("temporary_table_managers must contain manager descriptors")
        if any(not isinstance(item, TemporaryTableDescriptor) for item in self.temporary_tables):
            raise TypeError("temporary_tables must contain table descriptors")
        for item in (*self.variables, *self.temporary_table_managers, *self.temporary_tables):
            if item.fence != self.fence:
                raise ValueError("descriptor fence does not match the active capture fence")
        manager_ids = tuple(item.manager_id for item in self.temporary_table_managers)
        table_ids = tuple(item.table_id for item in self.temporary_tables)
        if len(set(manager_ids)) != len(manager_ids):
            raise ValueError("temporary table manager IDs must be unique")
        if len(set(table_ids)) != len(table_ids):
            raise ValueError("temporary table IDs must be unique")
        if any(item.manager_id not in manager_ids for item in self.temporary_tables):
            raise ValueError("temporary tables must reference an inspected manager")
        if type(self.cursor) is not int or self.cursor < 0:
            raise ValueError("cursor must be non-negative")
        if type(self.limit) is not int or not 0 < self.limit <= MAX_CAPTURE_PAGE_SIZE:
            raise ValueError("limit must be positive and bounded")
        if any(
            len(items) > self.limit
            for items in (
                self.variables,
                self.temporary_table_managers,
                self.temporary_tables,
            )
        ):
            raise ValueError("inspection collections must not exceed the page limit")
        if type(self.total_variables) is not int or self.total_variables < len(self.variables):
            raise ValueError("total_variables must cover the returned variables")
        if self.cursor > self.total_variables:
            raise ValueError("cursor must not exceed total_variables")
        returned_until = self.cursor + len(self.variables)
        if returned_until > self.total_variables:
            raise ValueError("returned variables exceed total_variables")
        if self.next_cursor is not None:
            if (
                type(self.next_cursor) is not int
                or self.next_cursor != returned_until
                or not self.cursor < self.next_cursor < self.total_variables
            ):
                raise ValueError("next_cursor must advance within total_variables")
        elif returned_until != self.total_variables:
            raise ValueError("terminal page must include all remaining variables")
        if type(self.truncated) is not bool:
            raise TypeError("truncated must be a bool")
        if self.truncated != (self.next_cursor is not None):
            raise ValueError("truncation must match next_cursor")

    @classmethod
    def from_wire(cls, value: object) -> "CaptureInspection":
        raw = _mapping(
            value,
            name="capture inspection",
            allowed={
                "fence",
                "variables",
                "temporary_table_managers",
                "temporary_tables",
                "cursor",
                "limit",
                "total_variables",
                "next_cursor",
                "truncated",
            },
        )
        return cls(
            fence=CaptureFence.from_wire(raw.get("fence")),
            variables=tuple(
                FrameVariableDescriptor.from_wire(item)
                for item in raw.get("variables", ())  # type: ignore[union-attr]
            ),
            temporary_table_managers=tuple(
                TemporaryTableManagerDescriptor.from_wire(item)
                for item in raw.get("temporary_table_managers", ())  # type: ignore[union-attr]
            ),
            temporary_tables=tuple(
                TemporaryTableDescriptor.from_wire(item)
                for item in raw.get("temporary_tables", ())  # type: ignore[union-attr]
            ),
            cursor=raw.get("cursor"),  # type: ignore[arg-type]
            limit=raw.get("limit"),  # type: ignore[arg-type]
            total_variables=raw.get("total_variables"),  # type: ignore[arg-type]
            next_cursor=raw.get("next_cursor"),  # type: ignore[arg-type]
            truncated=raw.get("truncated"),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class CaptureView:
    fence: CaptureFence
    location: ResolvedCapturePoint
    inspection: CaptureInspection | None
    dirty_roots: tuple[str, ...] = ()
    paused: bool = True
    mutable_object_caveat: str = CAPTURE_MUTABLE_OBJECT_CAVEAT
    recovery: tuple["RecoveryAction", ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.fence, CaptureFence):
            raise TypeError("fence must be a CaptureFence")
        if not isinstance(self.location, ResolvedCapturePoint):
            raise TypeError("location must be a ResolvedCapturePoint")
        if self.location.source_revision != self.fence.source_revision or self.location.source_sha256 != self.fence.source_sha256:
            raise ValueError("capture location must match the capture source identity")
        if self.inspection is not None:
            if not isinstance(self.inspection, CaptureInspection):
                raise TypeError("inspection must be a CaptureInspection")
            if self.inspection.fence != self.fence:
                raise ValueError("inspection must match the capture fence")
        object.__setattr__(self, "dirty_roots", _identifiers(self.dirty_roots, name="dirty_roots"))
        if type(self.paused) is not bool:
            raise TypeError("paused must be a bool")
        if not self.paused:
            raise ValueError("capture view must remain paused")
        if self.mutable_object_caveat != CAPTURE_MUTABLE_OBJECT_CAVEAT:
            raise ValueError("capture view must disclose mutable-object semantics")
        if isinstance(self.recovery, list):
            object.__setattr__(self, "recovery", tuple(self.recovery))
        if isinstance(self.recovery, str) or not isinstance(self.recovery, tuple):
            raise TypeError("recovery must be a sequence")
        from onec_runtime_mcp.agent.facade_contracts import RecoveryAction

        if any(not isinstance(action, RecoveryAction) for action in self.recovery):
            raise TypeError("recovery must contain RecoveryAction values")

    @classmethod
    def from_wire(cls, value: object) -> "CaptureView":
        raw = _mapping(
            value,
            name="capture view",
            allowed={
                "fence",
                "location",
                "inspection",
                "dirty_roots",
                "paused",
                "mutable_object_caveat",
                "recovery",
            },
        )
        if set(raw) != {
            "fence",
            "location",
            "inspection",
            "dirty_roots",
            "paused",
            "mutable_object_caveat",
            "recovery",
        }:
            raise ValueError("capture view requires complete wire content")
        inspection = raw.get("inspection")
        from onec_runtime_mcp.agent.facade_contracts import RecoveryAction

        return cls(
            fence=CaptureFence.from_wire(raw.get("fence")),
            location=ResolvedCapturePoint.from_wire(raw.get("location")),
            inspection=None if inspection is None else CaptureInspection.from_wire(inspection),
            dirty_roots=raw.get("dirty_roots", ()),  # type: ignore[arg-type]
            paused=raw.get("paused", True),  # type: ignore[arg-type]
            mutable_object_caveat=raw.get("mutable_object_caveat"),  # type: ignore[arg-type]
            recovery=tuple(
                RecoveryAction.from_wire(item)
                for item in raw.get("recovery", ())  # type: ignore[union-attr]
            ),
        )


@dataclass(frozen=True, slots=True)
class CaptureContinuationRequest:
    fence: CaptureFence
    next_points: tuple[CapturePointRequest, ...]
    observe: ObservationPlan | None = None
    budget: ValueBudget | None = None
    wait_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not isinstance(self.fence, CaptureFence):
            raise TypeError("fence must be a CaptureFence")
        if isinstance(self.next_points, str) or not isinstance(self.next_points, Sequence):
            raise TypeError("next_points must be a sequence")
        points = tuple(self.next_points)
        if len(points) > MAX_CAPTURE_POINTS or any(
            not isinstance(point, CapturePointRequest) for point in points
        ):
            raise ValueError("next_points must contain bounded capture point requests")
        if len({point.name.casefold() for point in points}) != len(points):
            raise ValueError("next point names must be unique")
        object.__setattr__(self, "next_points", points)
        if self.observe is not None and not isinstance(self.observe, ObservationPlan):
            raise TypeError("observe must be an ObservationPlan")
        if self.budget is not None and not isinstance(self.budget, ValueBudget):
            raise TypeError("budget must be a ValueBudget")
        if (
            type(self.wait_seconds) not in {int, float}
            or not isfinite(self.wait_seconds)
            or self.wait_seconds <= 0
        ):
            raise ValueError("wait_seconds must be finite and positive")

    @classmethod
    def from_wire(cls, value: object) -> "CaptureContinuationRequest":
        raw = _mapping(
            value,
            name="capture continuation request",
            allowed={"fence", "next_points", "observe", "budget", "wait_seconds"},
        )
        observe = raw.get("observe")
        budget = raw.get("budget")
        if budget is not None:
            budget = ValueBudget(**dict(_mapping(
                budget,
                name="value budget",
                allowed={"max_depth", "max_items", "max_rows", "max_bytes", "timeout_seconds"},
            )))
        return cls(
            fence=CaptureFence.from_wire(raw.get("fence")),
            next_points=tuple(
                CapturePointRequest.from_wire(item)
                for item in raw.get("next_points", ())  # type: ignore[union-attr]
            ),
            observe=None if observe is None else ObservationPlan.from_wire(observe),
            budget=budget,  # type: ignore[arg-type]
            wait_seconds=raw.get("wait_seconds", 30.0),  # type: ignore[arg-type]
        )


CAPTURE_WIRE_DATACLASSES: tuple[type[object], ...] = (
    CaptureFence,
    CapturePointRequest,
    ResolvedCapturePoint,
    FrameVariableDescriptor,
    TemporaryTableManagerDescriptor,
    TemporaryTableDescriptor,
    CaptureInspection,
    CaptureView,
    CaptureContinuationRequest,
)


__all__ = [
    "CAPTURE_WIRE_DATACLASSES",
    "CAPTURE_MUTABLE_OBJECT_CAVEAT",
    "CaptureContinuationRequest",
    "CaptureFence",
    "CaptureInspection",
    "CapturePointRequest",
    "CaptureView",
    "FrameVariableDescriptor",
    "MAX_CAPTURE_PAGE_SIZE",
    "ResolvedCapturePoint",
    "TemporaryTableDescriptor",
    "TemporaryTableManagerDescriptor",
]
