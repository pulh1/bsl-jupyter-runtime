"""Fenced CAPTURE manager origins and temporary-table metadata.

Only validated identifiers enter trusted helper expressions. The service keeps
opaque handles local to one recognized stop; the controller owns all RDBG I/O.
Selected tables retain a bounded descriptor for later controller tickets.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from threading import RLock
from time import monotonic
from typing import Protocol
from uuid import uuid4

from onec_runtime.errors import ProtocolError, StaleCaptureError
from onec_runtime.execution.arbiter import OutcomeUnknown, SessionPort
from onec_runtime.execution.capture.scope import (
    CaptureContextState, CaptureFrameIdentity, CaptureScope,
)
from onec_runtime.execution.evaluation import wait_for_pending_result
from onec_runtime.execution.local_wait import (
    validate_local_wait_timeout, wait_initiator_locally,
)
from onec_runtime.experiment import bsl_string_literal
from onec_runtime.observation import ManagerOrigin, SelectionKind, ValueSelection
from onec_runtime.rdbg.models import (
    CollectionCell, CollectionRow, EvaluationResult, FrameVariable,
)
from onec_runtime.table_value import evaluation_to_python


MAX_SCHEMA_COLUMNS = 100
MAX_TABLE_POSITION = 10_000_000


def _identifier(value: object, *, label: str) -> str:
    if type(value) is not str or not 0 < len(value) <= 256 or not value.isidentifier():
        raise ProtocolError(f"CAPTURE {label} is invalid")
    return value


@dataclass(frozen=True, slots=True)
class CaptureManagerProbePlan:
    scope: CaptureScope
    root: str
    fields: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.scope, CaptureScope):
            raise TypeError("CAPTURE manager scope is required")
        _identifier(self.root, label="manager root")
        if type(self.fields) is not tuple or len(self.fields) > 100:
            raise ProtocolError("CAPTURE manager fields are invalid")
        for field in self.fields:
            _identifier(field, label="manager field")

    @property
    def expression(self) -> str:
        path = ".".join((self.root, *self.fields))
        return f'ТипЗнч({path}) = Тип("МенеджерВременныхТаблиц")'

    @property
    def stack_level(self) -> int:
        level = self.scope.frame_stack_level
        if level is None:
            raise StaleCaptureError()
        return level

    def decode(self, result: EvaluationResult) -> bool:
        if not isinstance(result, EvaluationResult) or result.error_occurred or result.type_name != "Булево":
            raise ProtocolError("CAPTURE manager origin is unavailable")
        try:
            confirmed = evaluation_to_python(result) is True
        except Exception:
            confirmed = False
        if not confirmed:
            raise ProtocolError("CAPTURE manager origin is unavailable")
        return True


@dataclass(frozen=True, slots=True)
class CaptureTableSchemaPlan:
    scope: CaptureScope
    root: str
    fields: tuple[str, ...]
    table: str

    def __post_init__(self) -> None:
        CaptureManagerProbePlan(self.scope, self.root, self.fields)
        _identifier(self.table, label="table name")

    @property
    def expression(self) -> str:
        path = "e1cRuntimeКонтекст.e1cRuntimeКонтекстОтладки." + ".".join((self.root, *self.fields))
        return (
            "RuntimeKernelServer.ПолучитьСхемуВременнойТаблицыОтладки("
            + path + ", " + bsl_string_literal(self.table) + ")"
        )

    @property
    def stack_level(self) -> int:
        level = self.scope.kernel_stack_level
        if level is None:
            raise StaleCaptureError()
        return level

    def decode(self, result: EvaluationResult) -> tuple[str, ...]:
        if not isinstance(result, EvaluationResult) or result.error_occurred:
            raise ProtocolError("CAPTURE table schema is unavailable")
        size = result.collection_size
        rows = result.collection_rows
        if (
            type(size) is not int or not 0 <= size <= MAX_SCHEMA_COLUMNS
            or type(rows) is not tuple or len(rows) != size
        ):
            raise ProtocolError("CAPTURE table schema exceeds bounded metadata")
        names: list[str] = []
        for row in rows:
            if (
                not isinstance(row, CollectionRow)
                or type(row.cells) is not tuple or len(row.cells) != 1
                or not isinstance(row.cells[0], CollectionCell)
                or row.cells[0].name != "Имя"
            ):
                raise ProtocolError("CAPTURE table schema row is invalid")
            cell = row.cells[0]
            raw = cell.value_string or cell.presentation
            if not isinstance(raw, str):
                raise ProtocolError("CAPTURE table schema row is invalid")
            if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
                raw = raw[1:-1]
            names.append(_identifier(raw, label="table column"))
        if len({name.casefold() for name in names}) != len(names):
            raise ProtocolError("CAPTURE table schema is ambiguous")
        return tuple(names)


@dataclass(frozen=True, slots=True)
class CaptureSelectedTableDescriptor:
    """Validated CAPTURE table recipe; the controller owns its opaque key."""

    scope: CaptureScope
    root: str
    fields: tuple[str, ...]
    table: str
    offset: int
    limit: int
    columns: tuple[str, ...]

    def __post_init__(self) -> None:
        CaptureTableSchemaPlan(self.scope, self.root, self.fields, self.table)
        if (
            type(self.offset) is not int or self.offset < 0
            or type(self.limit) is not int or not 1 <= self.limit <= 100
            or self.offset + self.limit > MAX_TABLE_POSITION
            or type(self.columns) is not tuple or len(self.columns) > 100
        ):
            raise ProtocolError("CAPTURE table selection bounds are invalid")
        for column in self.columns:
            _identifier(column, label="table column")
        if len({column.casefold() for column in self.columns}) != len(self.columns):
            raise ProtocolError("CAPTURE table selection columns are ambiguous")

    @property
    def expression(self) -> str:
        path = "e1cRuntimeКонтекст.e1cRuntimeКонтекстОтладки." + ".".join((self.root, *self.fields))
        columns = (
            "Новый Массив" if not self.columns else
            "СтрРазделить(" + bsl_string_literal(",".join(self.columns)) + ', ",")'
        )
        return (
            "RuntimeKernelServer.ПолучитьВременнуюТаблицуОтладки("
            + path + ", " + bsl_string_literal(self.table)
            + f", {self.offset}, {self.limit}, " + columns + ")"
        )


@dataclass(frozen=True, slots=True)
class CaptureSelectedTableSchemaPlan:
    descriptor: CaptureSelectedTableDescriptor

    @property
    def scope(self) -> CaptureScope:
        return self.descriptor.scope

    @property
    def expression(self) -> str:
        return (
            "RuntimeTableTransferServer.ПолучитьКомпактнуюСхему("
            + self.descriptor.expression + ")"
        )

    @property
    def stack_level(self) -> int:
        return CaptureTableSchemaPlan(
            self.scope, self.descriptor.root, self.descriptor.fields,
            self.descriptor.table,
        ).stack_level

    def decode(self, result: EvaluationResult) -> tuple[str, ...]:
        return CaptureTableSchemaPlan(
            self.scope, self.descriptor.root, self.descriptor.fields,
            self.descriptor.table,
        ).decode(result)


CaptureManagerMetadataPlan = (
    CaptureManagerProbePlan | CaptureTableSchemaPlan | CaptureSelectedTableSchemaPlan
)


def evaluate_capture_manager_metadata(
    plan: CaptureManagerMetadataPlan, port: SessionPort,
) -> EvaluationResult:
    """Run one private helper on the owned arbiter port, retaining its pending ID."""

    if isinstance(plan, (CaptureTableSchemaPlan, CaptureSelectedTableSchemaPlan)):
        pending = port.start_collection_evaluation(
            plan.expression, start_index=0, page_size=MAX_SCHEMA_COLUMNS + 1,
            max_text_size=4096, stack_level=plan.stack_level, timeout_s=30.0,
        )
    elif isinstance(plan, CaptureManagerProbePlan):
        pending = port.start_evaluation(
            plan.expression, max_text_size=4096,
            stack_level=plan.stack_level, timeout_s=30.0,
        )
    else:
        raise TypeError("CAPTURE manager metadata plan is invalid")
    if pending.target_id != plan.scope.identity.target_id:
        raise OutcomeUnknown("CAPTURE manager helper belongs to another target")
    return wait_for_pending_result(port, pending)


class _MetadataTicket(Protocol):
    def wait_initiator(self, timeout: float | None = None) -> object: ...
    def detach_waiter(self) -> None: ...
    def status(self) -> object: ...


class _MetadataController(Protocol):
    capture_scope: CaptureScope | None

    def require_capture_manager_metadata_ready(self, scope: CaptureScope) -> None: ...
    def submit_capture_manager_metadata(
        self, plan: CaptureManagerMetadataPlan,
    ) -> _MetadataTicket: ...
    def register_capture_table_descriptor(
        self, descriptor: CaptureSelectedTableDescriptor,
    ) -> str: ...
    def require_capture_table_descriptor(
        self, handle: str, scope: CaptureScope,
    ) -> CaptureSelectedTableDescriptor: ...


class CaptureManagerMetadataService:
    """Map existing public metadata calls onto exact-stop controller tickets."""

    def __init__(
        self,
        controller: _MetadataController,
        *,
        command_timeout_s: float = 90.0,
        clock: Callable[[], float] = monotonic,
        wait_handoff: Callable[[], AbstractContextManager[None]] = nullcontext,
    ) -> None:
        selected = validate_local_wait_timeout(command_timeout_s)
        if selected is None or not callable(clock) or not callable(wait_handoff):
            raise TypeError("CAPTURE metadata wait configuration is invalid")
        self._controller = controller
        self._clock = clock
        self._wait_handoff = wait_handoff
        self._command_timeout_s = selected
        self._lock = RLock()
        self._scope: CaptureScope | None = None
        self._by_path: dict[str, str] = {}
        self._managers: dict[str, tuple[str, tuple[str, ...]]] = {}
        self._metadata_handles: set[str] = set()

    def resolve_manager_origin(
        self, origin: ManagerOrigin, *, timeout_s: float | None = None,
    ) -> Mapping[str, object]:
        if not isinstance(origin, ManagerOrigin) or origin.namespace != "frame":
            raise ProtocolError("CAPTURE manager origin must be frame-scoped")
        deadline = self._deadline(timeout_s)
        scope = self._current_scope()
        root = self._canonical_root(scope, origin.root)
        plan = CaptureManagerProbePlan(scope, root, origin.fields)
        path = ".".join((root, *origin.fields)).casefold()
        with self._lock:
            existing = self._by_path.get(path)
        if existing is not None:
            self._remaining(deadline)
            self._require_same_scope(scope)
            return self._manager_result(existing)
        confirmed = self._wait(
            self._controller.submit_capture_manager_metadata(plan), deadline,
        )
        self._require_same_scope(scope)
        if confirmed is not True:
            raise ProtocolError("CAPTURE manager proof is invalid")
        with self._lock:
            handle = self._by_path.get(path)
            if handle is None:
                handle = "capture_manager_" + uuid4().hex
                self._by_path[path] = handle
                self._managers[handle] = (root, origin.fields)
        return self._manager_result(handle)

    def temporary_tables(
        self,
        manager_handle: str,
        *,
        names: tuple[str, ...] | None,
        cursor: int,
        limit: int,
        selection: ValueSelection | None,
        timeout_s: float | None = None,
    ) -> Mapping[str, object]:
        deadline = self._deadline(timeout_s)
        scope = self._current_scope()
        if type(manager_handle) is not str:
            raise ProtocolError("CAPTURE manager handle is stale or invalid")
        with self._lock:
            manager = self._managers.get(manager_handle)
        if manager is None:
            raise ProtocolError("CAPTURE manager handle is stale or invalid")
        if (
            type(names) is not tuple or len(names) != 1
            or type(cursor) is not int or cursor != 0
            or type(limit) is not int or not 1 <= limit <= 100
        ):
            raise ProtocolError("CAPTURE table page is invalid")
        name = _identifier(names[0], label="table name")
        root, fields = manager
        descriptor = None
        if selection is None:
            plan = CaptureTableSchemaPlan(scope, root, fields, name)
        else:
            if (
                not isinstance(selection, ValueSelection)
                or selection.kind is not SelectionKind.TABLE_ROWS
                or selection.limit is None or selection.names
            ):
                raise ProtocolError("CAPTURE table selection is invalid")
            descriptor = CaptureSelectedTableDescriptor(
                scope, root, fields, name,
                selection.offset, selection.limit, selection.columns,
            )
            plan = CaptureSelectedTableSchemaPlan(descriptor)
        schema = self._wait(
            self._controller.submit_capture_manager_metadata(plan), deadline,
        )
        self._require_same_scope(scope)
        if type(schema) is not tuple or any(type(item) is not str for item in schema):
            raise ProtocolError("CAPTURE table schema is invalid")
        if descriptor is None:
            handle = "capture_table_metadata_" + uuid4().hex
            with self._lock:
                self._metadata_handles.add(handle)
        else:
            handle = self._controller.register_capture_table_descriptor(descriptor)
        return {
            "items": ({"name": name, "schema": schema, "handle": handle},),
            "total": 1,
            "next_cursor": None,
        }

    def validate_value_reference(self, handle: str) -> str:
        scope = self._current_scope()
        if (
            type(handle) is str
            and handle.startswith("capture_table_")
            and not handle.startswith("capture_table_metadata_")
        ):
            self._controller.require_capture_table_descriptor(handle, scope)
            return handle
        with self._lock:
            if handle in self._managers or handle in self._metadata_handles:
                return handle
        raise ProtocolError("CAPTURE value handle is stale or invalid")

    def _current_scope(self) -> CaptureScope:
        scope = self._controller.capture_scope
        if (
            not isinstance(scope, CaptureScope)
            or scope.context_state is not CaptureContextState.READY
            or scope.frame_identity is not CaptureFrameIdentity.CONFIRMED
            or not scope.published
            or scope.frame_stack_level is None
            or scope.kernel_stack_level is None
            or scope.inspection_target_id != scope.identity.target_id
        ):
            raise StaleCaptureError()
        self._controller.require_capture_manager_metadata_ready(scope)
        with self._lock:
            if self._scope is not scope:
                self._scope = scope
                self._by_path.clear()
                self._managers.clear()
                self._metadata_handles.clear()
        return scope

    def _require_same_scope(self, expected: CaptureScope) -> None:
        if self._current_scope() is not expected:
            raise StaleCaptureError()

    @staticmethod
    def _canonical_root(scope: CaptureScope, name: str) -> str:
        requested = _identifier(name, label="manager root").casefold()
        matches = tuple(
            item.name for item in scope.frame_variables
            if isinstance(item, FrameVariable)
            and item.name.casefold() == requested
        )
        if len(matches) != 1:
            raise ProtocolError("CAPTURE manager root is unavailable")
        return _identifier(matches[0], label="manager root")

    @staticmethod
    def _manager_result(handle: str) -> Mapping[str, object]:
        return {
            "key": handle, "handle": handle,
            "type_name": "МенеджерВременныхТаблиц",
        }

    def _deadline(self, timeout_s: float | None) -> float:
        requested = validate_local_wait_timeout(timeout_s)
        selected = self._command_timeout_s if requested is None else min(
            requested, self._command_timeout_s,
        )
        return self._clock() + selected

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise TimeoutError("Local CAPTURE metadata interval elapsed")
        return remaining

    def _wait(self, ticket: _MetadataTicket, deadline: float) -> object:
        try:
            remaining = self._remaining(deadline)
        except TimeoutError:
            if not ticket.status().settled:  # type: ignore[attr-defined]
                ticket.detach_waiter()
            raise
        return wait_initiator_locally(
            ticket, timeout_s=remaining, wait_handoff=self._wait_handoff,
        )


__all__ = [
    "CaptureManagerMetadataService", "CaptureManagerMetadataPlan",
    "CaptureManagerProbePlan", "CaptureTableSchemaPlan",
    "CaptureSelectedTableDescriptor", "CaptureSelectedTableSchemaPlan",
    "evaluate_capture_manager_metadata",
]
