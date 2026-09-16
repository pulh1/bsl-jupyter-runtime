"""Bounded, expression-free local models for CAPTURE value inspection.

``LocalCaptureValueAdapter`` is an internal integration seam.  Its backend owns
the exact stop-fence/lifecycle check and all target-side coordinator operations;
this module deliberately does not guess RDBG entry points.  Every descriptor
request is fresh, while returned pages contain immutable presentation data.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum
import json
import re
from typing import TYPE_CHECKING, Protocol, cast, overload

from onec_runtime.bsl.lexer import KEYWORDS
from onec_runtime.errors import (
    CaptureBusyError,
    CaptureEvaluationPendingError,
    CaptureLookupError,
    CaptureOutcomeUnknownError,
    CapturePathError,
    CaptureRecoveryRequiredError,
    CaptureShapeUnsupportedError,
    CaptureSourceUnavailableError,
    CaptureValueAccessDeniedError,
    CaptureValueCheckError,
    NoActiveCaptureError,
    StaleCaptureError,
)
from onec_runtime.privacy import public_artifact_value

if TYPE_CHECKING:
    from onec_runtime.capture_inspection import DebugFrame


MAX_PAGE_ITEMS = 100
MAX_NAME_CHARS = 256
MAX_TYPE_CHARS = 256
MAX_PREVIEW_CHARS = 512
_BSL_IDENTIFIER = re.compile(r"[A-Za-zА-Яа-яЁё_][0-9A-Za-zА-Яа-яЁё_]*\Z")


class ValueRootKind(StrEnum):
    CONTEXT = "context"
    FRAME = "frame"


class ValuePathSegmentKind(StrEnum):
    VARIABLE = "variable"
    FIELD = "field"
    INDEX = "index"
    COLUMN = "column"
    ROW = "row"


class ValueShape(StrEnum):
    SCALAR = "scalar"
    STRUCTURE = "structure"
    FIXED_STRUCTURE = "fixed_structure"
    ARRAY = "array"
    FIXED_ARRAY = "fixed_array"
    VALUE_TABLE = "value_table"
    VALUE_TABLE_ROW = "value_table_row"
    COLUMN = "column"
    MAP = "map"
    VALUE_TREE = "value_tree"
    APPLICATION_OBJECT = "application_object"
    UNDOCUMENTED = "undocumented"


class ValueViewKind(StrEnum):
    VARIABLES = "variables"
    STRUCTURE_FIELDS = "structure_fields"
    ARRAY_ITEMS = "array_items"
    TABLE_COLUMNS = "table_columns"
    TABLE_ROWS = "table_rows"
    ROW_FIELDS = "row_fields"


class VariableRole(StrEnum):
    VARIABLES = "variables"
    PARAMETERS = "parameters"
    LOCALS = "locals"


def _identifier(value: object, *, what: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > MAX_NAME_CHARS
            or _BSL_IDENTIFIER.fullmatch(value) is None or value.upper() in KEYWORDS):
        raise CapturePathError(f"{what} must be a bounded identifier")
    return value


@dataclass(frozen=True, slots=True, repr=False)
class ValueRoot:
    kind: ValueRootKind
    native_level: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ValueRootKind):
            raise CapturePathError("value root kind is invalid")
        if self.kind is ValueRootKind.CONTEXT:
            if self.native_level is not None:
                raise CapturePathError("context root cannot have a native level")
        elif type(self.native_level) is not int or self.native_level < 0:
            raise CapturePathError("frame root needs a nonnegative native level")

    def __repr__(self) -> str:
        return (
            "КонтекстОтладки"
            if self.kind is ValueRootKind.CONTEXT
            else f"frame[{self.native_level}]"
        )


@dataclass(frozen=True, slots=True, repr=False)
class SafePathSegment:
    kind: ValuePathSegmentKind
    key: str | int

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ValuePathSegmentKind):
            raise CapturePathError("safe-path segment kind is invalid")
        if self.kind in {
            ValuePathSegmentKind.VARIABLE,
            ValuePathSegmentKind.FIELD,
            ValuePathSegmentKind.COLUMN,
        }:
            _identifier(self.key, what="safe-path name")
        elif type(self.key) is not int or self.key < 0:
            raise CapturePathError("safe-path index must be nonnegative")

    @property
    def display_name(self) -> str | int:
        return self.key

    def __repr__(self) -> str:
        return f"{self.kind.value}({self.key!r})"


@dataclass(frozen=True, slots=True, repr=False)
class SafeValuePath:
    root: ValueRoot
    segments: tuple[SafePathSegment, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.root, ValueRoot):
            raise CapturePathError("safe path root is invalid")
        if type(self.segments) is not tuple or any(
            not isinstance(item, SafePathSegment) for item in self.segments
        ):
            raise CapturePathError("safe path must contain frozen segments")
        if len(self.segments) > 64:
            raise CapturePathError("safe path is too deep")

    def child(self, kind: ValuePathSegmentKind, key: str | int) -> SafeValuePath:
        return SafeValuePath(self.root, self.segments + (SafePathSegment(kind, key),))

    def __repr__(self) -> str:
        suffix = "".join(
            f".{part.key}" if isinstance(part.key, str) else f"[{part.key}]"
            for part in self.segments
        )
        return repr(self.root) + suffix


@dataclass(frozen=True, slots=True)
class ValueMetadata:
    type_name: str
    preview: str
    size: int | None
    shape: ValueShape

    def __post_init__(self) -> None:
        if (not isinstance(self.type_name, str) or not self.type_name
                or len(self.type_name) > MAX_TYPE_CHARS):
            raise CaptureValueCheckError("capture value type is invalid")
        if not isinstance(self.preview, str):
            raise CaptureValueCheckError("capture value preview is invalid")
        if self.size is not None and (type(self.size) is not int or self.size < 0):
            raise CaptureValueCheckError("capture value size is invalid")
        if not isinstance(self.shape, ValueShape):
            raise CaptureValueCheckError("capture value shape is invalid")


@dataclass(frozen=True, slots=True, repr=False)
class PrivateProjectedValue:
    """One backend-admitted result; metadata is absent for a denied value."""

    name: str | int
    describe: Callable[[], ValueMetadata] = field(repr=False, compare=False)
    denied: bool = False
    cycle: bool = False

    def __post_init__(self) -> None:
        if type(self.name) is int:
            if self.name < 0:
                raise CaptureValueCheckError("projected index is invalid")
        else:
            _identifier(self.name, what="projected name")
        if not callable(self.describe):
            raise CaptureValueCheckError("projected metadata reader is invalid")
        if type(self.denied) is not bool:
            raise CaptureValueCheckError("projected admission state is invalid")
        if type(self.cycle) is not bool:
            raise CaptureValueCheckError("projected cycle marker is invalid")


@dataclass(frozen=True, slots=True)
class PrivateValueProjection:
    entries: tuple[PrivateProjectedValue, ...]
    total: int
    next_cursor: int | None

    def __post_init__(self) -> None:
        if type(self.entries) is not tuple or any(
            not isinstance(item, PrivateProjectedValue) for item in self.entries
        ):
            raise CaptureValueCheckError("projected values must be an immutable tuple")
        if type(self.total) is not int or self.total < len(self.entries):
            raise CaptureValueCheckError("projected total is invalid")
        if self.next_cursor is not None and (
            type(self.next_cursor) is not int or self.next_cursor < 0
        ):
            raise CaptureValueCheckError("projected cursor is invalid")


@dataclass(frozen=True, slots=True, repr=False)
class ValueInspectionRequest:
    path: SafeValuePath
    view: ValueViewKind
    start: int
    stop: int
    role: VariableRole = VariableRole.VARIABLES
    parameter_names: tuple[str, ...] = ()
    exact: str | int | None = None


class CaptureValueBackend(Protocol):
    """Target-side seam awaiting RuntimeApi/Session/coordinator attachment.

    ``validate_inspection`` must validate the exact fence and lifecycle without
    privacy or source work.  Each later call must validate the same fence again
    while it runs. A projection owns the target-side admission decision for
    its root and selected children before it returns. Temporary-handle cleanup
    is a materialization-helper step.
    Variable projections apply the requested role before paging.  Parameter
    pages must preserve absolute source order and publish totals/cursors for the
    classified parameter inventory.  The adapter verifies this contract; only
    a terminal full inventory can be classified locally without ambiguity.
    """

    def validate_inspection(self, fence: object) -> None: ...
    def resolve_value(self, fence: object, path: SafeValuePath) -> PrivateProjectedValue: ...
    def project_values(
        self, fence: object, request: ValueInspectionRequest,
    ) -> PrivateValueProjection: ...
    def discover_table_columns(
        self, fence: object, path: SafeValuePath, limit: int,
    ) -> tuple[str, ...]: ...


@dataclass(frozen=True, slots=True, repr=False)
class CaptureValuePolicy:
    max_depth: int = 16
    max_items: int = MAX_PAGE_ITEMS
    max_bytes: int = 64 * 1024

    def __post_init__(self) -> None:
        if type(self.max_depth) is not int or self.max_depth < 0 or self.max_depth > 64:
            raise ValueError("capture value depth budget is invalid")
        if (type(self.max_items) is not int or self.max_items < 1
                or self.max_items > MAX_PAGE_ITEMS):
            raise ValueError("capture value item budget is invalid")
        if type(self.max_bytes) is not int or self.max_bytes < 1:
            raise ValueError("capture value byte budget is invalid")

@dataclass(frozen=True, slots=True, repr=False)
class ValueNode:
    name: str | int
    type_name: str | None
    preview: str
    size: int | None
    expandable: bool
    shape: ValueShape | None
    path: SafeValuePath
    cycle: bool = False
    _owner: LocalCaptureValueAdapter | None = field(
        repr=False, compare=False, default=None,
    )

    @property
    def children(self) -> ChildValueDescriptor:
        return ChildValueDescriptor(self._require_owner(), self, None)

    @property
    def fields(self) -> ChildValueDescriptor:
        return ChildValueDescriptor(self._require_owner(), self, "fields")

    @property
    def items(self) -> ChildValueDescriptor:
        return ChildValueDescriptor(self._require_owner(), self, "items")

    @property
    def columns(self) -> ChildValueDescriptor:
        return ChildValueDescriptor(self._require_owner(), self, "columns")

    @property
    def rows(self) -> ChildValueDescriptor:
        return ChildValueDescriptor(self._require_owner(), self, "rows")

    def _require_owner(self) -> LocalCaptureValueAdapter:
        if self._owner is None:
            raise CaptureSourceUnavailableError("value inspection is not attached")
        return self._owner

    def __str__(self) -> str:
        marker = " ▸" if self.expandable else ""
        return f"{self.name}: {self.type_name} = {self.preview}{marker}"

    __repr__ = __str__


@dataclass(frozen=True, slots=True, repr=False)
class DeniedValueNode:
    """The only public representation of a denied selected child."""

    name: str | int
    access: str = field(init=False, default="denied")
    expandable: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        if type(self.name) is int:
            if self.name < 0:
                raise CaptureValueCheckError("projected index is invalid")
        else:
            _identifier(self.name, what="projected name")

    def __str__(self) -> str:
        return f"{self.name}: <private runtime value>"

    __repr__ = __str__


@dataclass(frozen=True, slots=True, repr=False)
class ValuePage:
    items: tuple[ValueNode | DeniedValueNode, ...]
    total: int
    next_cursor: int | None
    path: SafeValuePath
    view: str
    start: int
    stop: int

    def __post_init__(self) -> None:
        if type(self.items) is not tuple or any(
            not isinstance(item, (ValueNode, DeniedValueNode)) for item in self.items
        ):
            raise TypeError("value page items must be an immutable tuple")

    def __str__(self) -> str:
        header = f"{self.path}.{self.view} [{self.start}:{self.stop}]"
        body = "\n".join(f"├─ {item}" for item in self.items)
        return header + ("\n" + body if body else "")

    __repr__ = __str__


@dataclass(frozen=True, slots=True, repr=False)
class VariableDescriptor:
    _owner: LocalCaptureValueAdapter
    _root: ValueRoot
    _role: VariableRole

    @overload
    def __getitem__(self, key: str) -> ValueNode: ...
    @overload
    def __getitem__(self, key: int) -> ValueNode: ...
    @overload
    def __getitem__(self, key: slice) -> ValuePage: ...

    def __getitem__(self, key: str | int | slice) -> ValueNode | ValuePage:
        return self._owner._read_variables(self._root, self._role, key)

    def __iter__(self):
        raise CapturePathError("variable iteration requires a bounded page")


@dataclass(frozen=True, slots=True, repr=False)
class CaptureContextView:
    _owner: LocalCaptureValueAdapter
    _root: ValueRoot

    @property
    def variables(self) -> VariableDescriptor:
        return VariableDescriptor(self._owner, self._root, VariableRole.VARIABLES)

    @property
    def parameters(self) -> VariableDescriptor:
        return VariableDescriptor(self._owner, self._root, VariableRole.PARAMETERS)

    @property
    def locals(self) -> VariableDescriptor:
        return VariableDescriptor(self._owner, self._root, VariableRole.LOCALS)

    def __repr__(self) -> str:
        return repr(self._root)


@dataclass(frozen=True, slots=True, repr=False)
class ChildValueDescriptor:
    _owner: LocalCaptureValueAdapter
    _node: ValueNode
    _alias: str | None

    @overload
    def __getitem__(self, key: str) -> ValueNode: ...
    @overload
    def __getitem__(self, key: int) -> ValueNode: ...
    @overload
    def __getitem__(self, key: slice) -> ValuePage: ...

    def __getitem__(self, key: str | int | slice) -> ValueNode | ValuePage:
        return self._owner._read_children(self._node, self._alias, key)

    def __iter__(self):
        raise CapturePathError("child iteration requires a bounded page")


class LocalCaptureValueAdapter:
    """Local descriptor engine; lifecycle and remote execution stay injected."""

    def __init__(
        self,
        backend: CaptureValueBackend,
        fence: object,
        *,
        policy: CaptureValuePolicy,
        resolve_parameters: Callable[[ValueRoot], tuple[str, ...]],
    ) -> None:
        if not isinstance(policy, CaptureValuePolicy):
            raise TypeError("capture value policy is invalid")
        if not callable(resolve_parameters):
            raise TypeError("parameter resolver must be callable")
        self._backend = backend
        self._fence = fence
        self._policy = policy
        self._resolve_parameters = resolve_parameters

    @property
    def context(self) -> CaptureContextView:
        return CaptureContextView(self, ValueRoot(ValueRootKind.CONTEXT))

    def frame(self, native_level: int) -> CaptureContextView:
        return CaptureContextView(self, ValueRoot(ValueRootKind.FRAME, native_level))

    def bind_frame(self, frame: DebugFrame) -> DebugFrame:
        """Return a saved stack frame carrying this adapter's live value scope."""
        from onec_runtime.capture_inspection import DebugFrame

        if not isinstance(frame, DebugFrame):
            raise TypeError("capture value scope requires a DebugFrame")
        return replace(frame, _value_scope=self.frame(frame.native_level))

    def _validate_first(self) -> None:
        self._backend.validate_inspection(self._fence)

    def _read_variables(
        self, root: ValueRoot, role: VariableRole, key: str | int | slice,
    ) -> ValueNode | ValuePage:
        self._validate_first()
        exact: str | None = None
        if isinstance(key, str):
            exact = _identifier(key, what="variable name")
            start, stop = 0, 2  # One overflow sentinel detects folded-name ambiguity.
        elif type(key) is int:
            if key < 0:
                raise CapturePathError("variable index must be nonnegative")
            start, stop = key, key + 1
        elif isinstance(key, slice):
            start, stop = self._page_bounds(key)
        else:
            raise TypeError("variables require an identifier, index or bounded slice")
        if isinstance(key, slice) and start == stop:
            return self._empty_page(
                SafeValuePath(root), role.value, start=start, stop=stop,
            )
        parameters = self._parameters(root) if role is not VariableRole.VARIABLES else ()
        request = ValueInspectionRequest(
            SafeValuePath(root), ValueViewKind.VARIABLES, start, stop,
            role, parameters, exact,
        )
        projection = self._project(request)
        projection = self._classify_role_projection(projection, request)
        page = self._normalize_page(
            projection, request, exact_access=not isinstance(key, slice),
        )
        if isinstance(key, slice):
            return page
        return self._one(page, exact=exact)

    @staticmethod
    def _classify_role_projection(
        projection: PrivateValueProjection,
        request: ValueInspectionRequest,
    ) -> PrivateValueProjection:
        if request.role is VariableRole.VARIABLES:
            return projection
        parameter_order = {
            name.casefold(): index for index, name in enumerate(request.parameter_names)
        }
        original = projection.entries
        if request.role is VariableRole.PARAMETERS:
            return LocalCaptureValueAdapter._classify_parameter_projection(
                projection, request, parameter_order,
            )
        entries = tuple(
            entry for entry in original
            if not isinstance(entry.name, str)
            or entry.name.casefold() not in parameter_order
        )
        changed = entries != original
        if changed and projection.next_cursor is not None:
            raise CaptureValueCheckError(
                "backend must apply variable roles before nonterminal paging"
            )
        total = (
            request.start + len(entries)
            if changed and projection.next_cursor is None
            else projection.total
        )
        return PrivateValueProjection(entries, total, projection.next_cursor)

    @staticmethod
    def _classify_parameter_projection(
        projection: PrivateValueProjection,
        request: ValueInspectionRequest,
        parameter_order: dict[str, int],
    ) -> PrivateValueProjection:
        if request.exact is not None:
            entries = tuple(
                entry for entry in projection.entries
                if isinstance(entry.name, str)
                and entry.name.casefold() in parameter_order
            )
            folded = tuple(cast(str, entry.name).casefold() for entry in entries)
            if len(set(folded)) != len(folded):
                raise CaptureValueCheckError("parameter projection is ambiguous")
            return PrivateValueProjection(entries, len(entries), None)

        expected_all = tuple(name.casefold() for name in request.parameter_names)
        full_inventory = (
            request.start == 0
            and projection.next_cursor is None
            and request.stop >= projection.total
        )
        if full_inventory:
            entries = tuple(
                entry for entry in projection.entries
                if isinstance(entry.name, str)
                and entry.name.casefold() in parameter_order
            )
            entries = tuple(sorted(
                entries,
                key=lambda entry: parameter_order[cast(str, entry.name).casefold()],
            ))
            actual = tuple(cast(str, entry.name).casefold() for entry in entries)
            if actual != expected_all:
                raise CaptureValueCheckError(
                    "backend parameter page does not match source order"
                )
            return PrivateValueProjection(entries, len(expected_all), None)

        actual = tuple(
            entry.name.casefold() if isinstance(entry.name, str) else None
            for entry in projection.entries
        )
        expected = expected_all[request.start:request.stop]
        total = len(expected_all)
        next_cursor = request.stop if request.stop < total else None
        if (
            actual != expected
            or projection.total != total
            or projection.next_cursor != next_cursor
        ):
            raise CaptureValueCheckError(
                "backend parameter page does not match source order"
            )
        return projection

    def _parameters(self, root: ValueRoot) -> tuple[str, ...]:
        source_unavailable = False
        try:
            names = self._resolve_parameters(root)
        except (
            CaptureBusyError,
            CaptureEvaluationPendingError,
            CaptureOutcomeUnknownError,
            CaptureRecoveryRequiredError,
            NoActiveCaptureError,
            StaleCaptureError,
        ):
            raise
        except Exception:
            source_unavailable = True
            names = ()
        if source_unavailable:
            raise CaptureSourceUnavailableError(
                "method source unavailable; use variables for unclassified values"
            ) from None
        if type(names) is not tuple:
            raise CaptureSourceUnavailableError(
                "method parameter classification unavailable; use variables"
            )
        try:
            checked = tuple(_identifier(name, what="method parameter") for name in names)
        except CapturePathError as error:
            raise CaptureSourceUnavailableError(
                "method parameter classification unavailable; use variables"
            ) from error
        if len({name.casefold() for name in checked}) != len(checked):
            raise CaptureSourceUnavailableError(
                "method parameter classification is ambiguous; use variables"
            )
        return checked

    def _read_children(
        self, node: ValueNode, alias: str | None, key: str | int | slice,
    ) -> ValueNode | ValuePage:
        self._validate_first()
        if node.cycle:
            raise CapturePathError("capture value cycle cannot be expanded")
        depth = max(0, len(node.path.segments) - 1)
        if depth >= self._policy.max_depth:
            raise CapturePathError("capture value depth budget exceeded")
        view, segment_kind = self._view_for(node.shape, alias)
        exact: str | int | None = None
        if isinstance(key, str):
            exact = _identifier(key, what="child name")
            if segment_kind not in {ValuePathSegmentKind.FIELD, ValuePathSegmentKind.COLUMN}:
                raise CapturePathError("this child view requires a nonnegative index")
            start, stop = 0, 2  # One overflow sentinel detects folded-name ambiguity.
        elif type(key) is int:
            if key < 0:
                raise CapturePathError("child index must be nonnegative")
            if segment_kind not in {ValuePathSegmentKind.INDEX, ValuePathSegmentKind.ROW}:
                raise CapturePathError("this child view requires an identifier")
            exact, start, stop = key, 0, 1
        elif isinstance(key, slice):
            start, stop = self._page_bounds(key)
        else:
            raise TypeError("children require a safe name, index or bounded slice")
        if isinstance(key, slice) and start == stop:
            return self._empty_page(node.path, view.value, start=start, stop=stop)

        root_record = self._backend.resolve_value(self._fence, node.path)
        if root_record.denied:
            raise CaptureValueAccessDeniedError("capture value is private")
        root_metadata = self._describe(root_record)
        if not isinstance(root_metadata, ValueMetadata) or root_metadata.shape is not node.shape:
            raise CaptureValueCheckError("capture value shape changed during inspection")
        schema = None
        if view in {
            ValueViewKind.TABLE_ROWS,
            ValueViewKind.TABLE_COLUMNS,
            ValueViewKind.ROW_FIELDS,
        }:
            schema = self._discover_schema(node.path)
        request = ValueInspectionRequest(node.path, view, start, stop, exact=exact)
        projection = self._project(request)
        if schema is not None and view in {
            ValueViewKind.TABLE_COLUMNS, ValueViewKind.ROW_FIELDS,
        }:
            self._validate_schema_projection(projection, request, schema)
        page = self._normalize_page(
            projection, request, exact_access=exact is not None,
            segment_kind=segment_kind,
        )
        if isinstance(key, slice):
            return page
        return self._one(page, exact=exact)

    def _discover_schema(self, path: SafeValuePath) -> tuple[str, ...]:
        columns = self._backend.discover_table_columns(
            self._fence, path, MAX_PAGE_ITEMS + 1,
        )
        if type(columns) is not tuple or any(not isinstance(name, str) for name in columns):
            raise CaptureValueCheckError("value-table schema is invalid")
        if len(columns) > MAX_PAGE_ITEMS:
            raise CaptureShapeUnsupportedError("value table exceeds 100 columns")
        checked = tuple(_identifier(name, what="value-table column") for name in columns)
        if len({name.casefold() for name in checked}) != len(checked):
            raise CaptureShapeUnsupportedError("value-table column names must be unique")
        return checked

    @staticmethod
    def _validate_schema_projection(
        projection: PrivateValueProjection,
        request: ValueInspectionRequest,
        schema: tuple[str, ...],
    ) -> None:
        actual = tuple(entry.name for entry in projection.entries)
        if any(not isinstance(name, str) for name in actual):
            raise CaptureValueCheckError("value projection does not match its schema")
        if isinstance(request.exact, str):
            expected = tuple(
                name for name in schema if name.casefold() == request.exact.casefold()
            )
        else:
            expected = schema[request.start:request.stop]
            if projection.total != len(schema):
                raise CaptureValueCheckError("value projection schema count does not match")
        if actual != expected:
            raise CaptureValueCheckError("value projection does not match its schema")

    def _view_for(
        self, shape: ValueShape | None, alias: str | None,
    ) -> tuple[ValueViewKind, ValuePathSegmentKind]:
        supported = {
            ValueShape.STRUCTURE: (ValueViewKind.STRUCTURE_FIELDS, ValuePathSegmentKind.FIELD),
            ValueShape.FIXED_STRUCTURE: (
                ValueViewKind.STRUCTURE_FIELDS, ValuePathSegmentKind.FIELD,
            ),
            ValueShape.ARRAY: (ValueViewKind.ARRAY_ITEMS, ValuePathSegmentKind.INDEX),
            ValueShape.FIXED_ARRAY: (ValueViewKind.ARRAY_ITEMS, ValuePathSegmentKind.INDEX),
            ValueShape.VALUE_TABLE_ROW: (ValueViewKind.ROW_FIELDS, ValuePathSegmentKind.FIELD),
        }
        if shape is ValueShape.VALUE_TABLE:
            if alias == "columns":
                return ValueViewKind.TABLE_COLUMNS, ValuePathSegmentKind.COLUMN
            if alias in (None, "rows"):
                return ValueViewKind.TABLE_ROWS, ValuePathSegmentKind.ROW
        expected_alias = {
            ValueShape.STRUCTURE: "fields",
            ValueShape.FIXED_STRUCTURE: "fields",
            ValueShape.ARRAY: "items",
            ValueShape.FIXED_ARRAY: "items",
            ValueShape.VALUE_TABLE_ROW: "fields",
        }.get(shape)
        if shape in supported and alias in (None, expected_alias):
            return supported[cast(ValueShape, shape)]
        raise CaptureShapeUnsupportedError("capture value shape or child view is unsupported")

    def _page_bounds(self, key: slice) -> tuple[int, int]:
        if key.step is not None:
            raise CapturePathError("capture pages do not accept a slice step")
        start, stop = 0 if key.start is None else key.start, key.stop
        if (type(start) is not int or type(stop) is not int or start < 0
                or stop < start or stop - start > MAX_PAGE_ITEMS):
            raise CapturePathError("capture pages require finite nonnegative bounds of at most 100")
        if stop - start > self._policy.max_items:
            raise CapturePathError("capture page exceeds the item budget")
        return start, stop

    def _project(self, request: ValueInspectionRequest) -> PrivateValueProjection:
        result = self._backend.project_values(self._fence, request)
        if not isinstance(result, PrivateValueProjection):
            raise CaptureValueCheckError("capture projection result is invalid")
        if len(result.entries) > request.stop - request.start:
            raise CaptureValueCheckError("capture projection exceeded the requested page")
        if result.next_cursor is not None and (
            not result.entries
            or result.next_cursor != request.start + len(result.entries)
            or result.next_cursor >= result.total
        ):
            raise CaptureValueCheckError("capture projection cursor is invalid")
        return result

    def _normalize_page(
        self,
        projection: PrivateValueProjection,
        request: ValueInspectionRequest,
        *,
        exact_access: bool,
        segment_kind: ValuePathSegmentKind = ValuePathSegmentKind.VARIABLE,
    ) -> ValuePage:
        nodes: list[ValueNode | DeniedValueNode] = []
        for entry in projection.entries:
            name = entry.name
            if segment_kind in {
                ValuePathSegmentKind.VARIABLE,
                ValuePathSegmentKind.FIELD,
                ValuePathSegmentKind.COLUMN,
            }:
                checked_name: str | int = _identifier(name, what="projected name")
            elif type(name) is int and name >= 0:
                checked_name = name
            else:
                raise CaptureValueCheckError("projected child index is invalid")
            if entry.denied:
                if exact_access:
                    raise CaptureValueAccessDeniedError("capture value is private")
                node = DeniedValueNode(checked_name)
            else:
                path = request.path.child(segment_kind, checked_name)
                metadata = self._describe(entry)
                if not isinstance(metadata, ValueMetadata):
                    raise CaptureValueCheckError("capture value metadata is invalid")
                preview = self._preview(metadata)
                cycle = entry.cycle
                expandable = (
                    not cycle
                    and metadata.shape in {
                        ValueShape.STRUCTURE, ValueShape.FIXED_STRUCTURE,
                        ValueShape.ARRAY, ValueShape.FIXED_ARRAY,
                        ValueShape.VALUE_TABLE, ValueShape.VALUE_TABLE_ROW,
                    }
                    and metadata.size != 0
                )
                node = ValueNode(
                    checked_name, metadata.type_name, preview, metadata.size,
                    expandable, metadata.shape, path, cycle=cycle,
                    _owner=self,
                )
            nodes.append(node)
        page = ValuePage(
            tuple(nodes), projection.total, projection.next_cursor,
            request.path,
            request.role.value
            if request.view is ValueViewKind.VARIABLES
            else request.view.value,
            request.start, request.stop,
        )
        return self._bounded_page(page)

    def _empty_page(
        self,
        path: SafeValuePath,
        view: str,
        *,
        start: int,
        stop: int,
    ) -> ValuePage:
        return self._bounded_page(ValuePage((), 0, None, path, view, start, stop))

    def _bounded_page(self, page: ValuePage) -> ValuePage:
        public_bytes = json.dumps(
            public_artifact_value(page),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(public_bytes) > self._policy.max_bytes:
            raise CaptureValueCheckError("capture value page byte budget exceeded")
        return page

    @staticmethod
    def _preview(metadata: ValueMetadata) -> str:
        preview = metadata.preview
        if metadata.shape is not ValueShape.SCALAR and metadata.size is not None:
            preview = f"{metadata.size} elements"
        if len(preview) > MAX_PREVIEW_CHARS:
            preview = preview[: MAX_PREVIEW_CHARS - 1] + "…"
        return preview

    @staticmethod
    def _describe(entry: PrivateProjectedValue) -> ValueMetadata:
        check_failed = False
        try:
            metadata = entry.describe()
        except (
            CaptureBusyError,
            CaptureEvaluationPendingError,
            CaptureOutcomeUnknownError,
            CaptureRecoveryRequiredError,
            CaptureValueAccessDeniedError,
            CaptureValueCheckError,
            NoActiveCaptureError,
            StaleCaptureError,
        ):
            raise
        except Exception:
            check_failed = True
            metadata = None
        if check_failed:
            raise CaptureValueCheckError("capture value metadata check failed") from None
        if not isinstance(metadata, ValueMetadata):
            raise CaptureValueCheckError("capture value metadata is invalid")
        return metadata

    @staticmethod
    def _one(page: ValuePage, *, exact: str | int | None) -> ValueNode:
        if not page.items:
            raise CaptureLookupError("capture value not found")
        if len(page.items) != 1:
            raise CaptureLookupError("capture value lookup is ambiguous")
        if exact is not None:
            matches = [item for item in page.items if (
                item.name.casefold() == exact.casefold()
                if isinstance(item.name, str) and isinstance(exact, str)
                else item.name == exact
            )]
            if len(matches) != 1:
                raise CaptureLookupError("capture value lookup is ambiguous")
        item = page.items[0]
        if isinstance(item, DeniedValueNode):
            raise CaptureValueAccessDeniedError("capture value is private")
        return item
