"""Closed protocol-2 envelope for CAPTURE value inspection.

The public value descriptors carry only ``SafeValuePath`` objects.  This module
turns an already-checked descriptor request into a small BSL program which asks
the checked-in extension helper to admit and project values, then validates the
opaque payload before the controller creates immutable public records.
"""

from __future__ import annotations

from base64 import b64decode
import binascii
from collections.abc import Callable
from dataclasses import dataclass, field
from hashlib import sha256
import json
import re
from uuid import uuid4

from onec_runtime.capture_evaluation import AdmissionEnvelopeV1
from onec_runtime.capture_values import (
    CaptureValuePolicy,
    PrivateProjectedValue,
    PrivateValueProjection,
    SafePathSegment,
    SafeValuePath,
    ValueInspectionRequest,
    ValueMetadata,
    ValuePathSegmentKind,
    ValueRootKind,
    ValueShape,
    ValueViewKind,
    VariableRole,
)
from onec_runtime.errors import CaptureValueCheckError, ProtocolError
from onec_runtime.experiment import bsl_string_literal


VALUE_INSPECTION_CONTEXT_KEY_PREFIX = "__onec_value_"
MAX_CAPTURE_VALUE_WIRE_TOTAL = 10_000_000
MAX_CAPTURE_VALUE_INSPECTION_SOURCE_BYTES = 64 * 1024
MAX_CAPTURE_VALUE_NATIVE_CANDIDATES = 100
MAX_CAPTURE_VALUE_WORKER_REGISTRATIONS = 32
MAX_CAPTURE_VALUE_WORKER_REGISTRATION_CHARS = 4096
_CONTEXT_KEY = re.compile(r"__onec_value_[0-9a-f]{32}\Z")
_PUBLIC_SHAPES = frozenset(
    {
        ValueShape.SCALAR,
        ValueShape.STRUCTURE,
        ValueShape.FIXED_STRUCTURE,
        ValueShape.ARRAY,
        ValueShape.FIXED_ARRAY,
        ValueShape.VALUE_TABLE,
        ValueShape.VALUE_TABLE_ROW,
        ValueShape.COLUMN,
    }
)
_NAMED_VIEWS = frozenset(
    {
        ValueViewKind.VARIABLES,
        ValueViewKind.STRUCTURE_FIELDS,
        ValueViewKind.TABLE_COLUMNS,
        ValueViewKind.ROW_FIELDS,
    }
)


def _base64_length(byte_count: int) -> int:
    return ((byte_count + 2) // 3) * 4


@dataclass(frozen=True, slots=True, repr=False)
class CaptureValueInspectionEnvelope:
    """One pre-registered private payload lifecycle owned by INSPECTION."""

    source: str
    private_key: str
    cleanup_source: str
    max_text_size: int
    parse_metadata: Callable[[object], AdmissionEnvelopeV1] = field(
        repr=False, compare=False,
    )
    decode: Callable[[AdmissionEnvelopeV1, str], object] = field(
        repr=False, compare=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not self.source:
            raise ValueError("capture value inspection source is invalid")
        if not _CONTEXT_KEY.fullmatch(self.private_key):
            raise ValueError("capture value inspection private key is invalid")
        if not isinstance(self.cleanup_source, str) or not self.cleanup_source:
            raise ValueError("capture value inspection cleanup source is invalid")
        if type(self.max_text_size) is not int or self.max_text_size < 4:
            raise ValueError("capture value inspection maximum text size is invalid")
        if not callable(self.parse_metadata) or not callable(self.decode):
            raise TypeError("capture value inspection envelope callbacks are invalid")


def build_capture_value_inspection_envelope(
    *,
    action: str,
    path: SafeValuePath,
    request: ValueInspectionRequest | None,
    limit: int | None,
    runtime_generation: int,
    context_generation: int,
    worker_type_registrations: tuple[str, ...],
    native_candidates: tuple[str, ...] = (),
    policy: CaptureValuePolicy | None = None,
) -> CaptureValueInspectionEnvelope:
    """Build a target program with no values or target handles in its API."""
    selected_policy = policy or CaptureValuePolicy()
    if not isinstance(selected_policy, CaptureValuePolicy):
        raise TypeError("capture value inspection policy is invalid")
    if action not in {"resolve", "project", "columns"}:
        raise CaptureValueCheckError("capture value operation is invalid")
    if not isinstance(path, SafeValuePath):
        raise CaptureValueCheckError("capture value path is invalid")
    if type(runtime_generation) is not int or runtime_generation < 1:
        raise CaptureValueCheckError("capture value runtime generation is invalid")
    if type(context_generation) is not int or context_generation < 1:
        raise CaptureValueCheckError("capture value context generation is invalid")
    if (
        type(worker_type_registrations) is not tuple
        or any(
            not isinstance(item, str)
            or not item
            or len(item) > MAX_CAPTURE_VALUE_WORKER_REGISTRATION_CHARS
            for item in worker_type_registrations
        )
        or len(worker_type_registrations) > MAX_CAPTURE_VALUE_WORKER_REGISTRATIONS
    ):
        raise CaptureValueCheckError("capture value Worker registrations are invalid")
    if len(path.segments) > selected_policy.max_depth + 1:
        raise CaptureValueCheckError("capture value path exceeds the depth budget")
    _validate_builder_request(
        action,
        path=path,
        request=request,
        limit=limit,
        max_depth=selected_policy.max_depth + 1,
    )
    if path.root.kind is ValueRootKind.CONTEXT:
        if type(native_candidates) is not tuple or native_candidates:
            raise CaptureValueCheckError("capture context candidates are invalid")
    elif (
        path.root.kind is not ValueRootKind.FRAME
        or type(native_candidates) is not tuple
        or len(native_candidates) > MAX_CAPTURE_VALUE_NATIVE_CANDIDATES
    ):
        raise CaptureValueCheckError("capture native frame candidates are unavailable")
    else:
        _validate_candidate_names(native_candidates)

    key = VALUE_INSPECTION_CONTEXT_KEY_PREFIX + uuid4().hex
    maximum_text_size = _base64_length(selected_policy.max_bytes)
    source = _inspection_source(
        action=action,
        path=path,
        request=request,
        limit=limit,
        context_key=key,
        runtime_generation=runtime_generation,
        context_generation=context_generation,
        worker_type_registrations=worker_type_registrations,
        native_candidates=native_candidates,
        max_items=selected_policy.max_items,
        max_bytes=selected_policy.max_bytes,
    )
    if len(source.encode("utf-8")) > MAX_CAPTURE_VALUE_INSPECTION_SOURCE_BYTES:
        raise CaptureValueCheckError("capture value inspection source exceeds its budget")

    def parse_metadata(metadata: object) -> AdmissionEnvelopeV1:
        observed = AdmissionEnvelopeV1.parse(
            metadata,
            max_payload_bytes=selected_policy.max_bytes,
            max_base64_chars=maximum_text_size,
        )
        if (
            observed.runtime_generation != runtime_generation
            or observed.context_generation != context_generation
        ):
            raise CaptureValueCheckError("CAPTURE value admission result is invalid")
        return observed

    def decode(observed: AdmissionEnvelopeV1, content: str) -> object:
        if not isinstance(observed, AdmissionEnvelopeV1):
            raise CaptureValueCheckError("CAPTURE value admission result is invalid")
        if not isinstance(content, str) or len(content) != observed.base64_chars:
            raise CaptureValueCheckError("CAPTURE value admission result is invalid")
        try:
            payload = b64decode(content, validate=True)
        except (ValueError, binascii.Error):
            raise CaptureValueCheckError("CAPTURE value payload is invalid") from None
        if (
            len(payload) != observed.payload_bytes
            or sha256(payload).hexdigest() != observed.payload_sha256
        ):
            raise CaptureValueCheckError("CAPTURE value payload integrity check failed")
        return _decode_wire_payload(payload, action=action, path=path, request=request, limit=limit)

    return CaptureValueInspectionEnvelope(
        source,
        key,
        "RuntimeKernelServer.УдалитьМатериализациюИзКонтекста("
        "RuntimeContextStoreServer.ПолучитьКонтекст(), "
        + bsl_string_literal(key)
        + ");\nРезультат = Истина;",
        maximum_text_size,
        parse_metadata,
        decode,
    )


def _validate_candidate_names(names: tuple[str, ...]) -> None:
    checked = tuple(
        SafePathSegment(ValuePathSegmentKind.VARIABLE, item).key for item in names
    )
    if len({item.casefold() for item in checked}) != len(checked):
        raise CaptureValueCheckError("capture native frame candidates are ambiguous")


def _validate_builder_request(
    action: str,
    *,
    path: SafeValuePath,
    request: ValueInspectionRequest | None,
    limit: int | None,
    max_depth: int,
) -> None:
    """Keep this lower-level source builder closed when called directly."""
    _validate_wire_path(action, path, max_depth=max_depth)
    if action == "project":
        if not isinstance(request, ValueInspectionRequest) or limit is not None:
            raise CaptureValueCheckError("capture value projection request is invalid")
        if (
            type(request.start) is not int
            or type(request.stop) is not int
            or request.start < 0
            or request.stop < request.start
            or request.start > MAX_CAPTURE_VALUE_WIRE_TOTAL
            or request.stop > MAX_CAPTURE_VALUE_WIRE_TOTAL
                or request.stop - request.start > CaptureValuePolicy().max_items
        ):
            raise CaptureValueCheckError("capture value projection request is invalid")
        _validate_wire_projection(request)
        return
    if action == "resolve":
        if request is not None or limit is not None:
            raise CaptureValueCheckError("capture value resolve request is invalid")
        return
    if request is not None or type(limit) is not int or not 1 <= limit <= 101:
        raise CaptureValueCheckError("capture value schema request is invalid")


def _validate_wire_path(
    action: str,
    path: SafeValuePath,
    *,
    max_depth: int,
) -> None:
    """Accept only symbolic routes that public descriptors can create."""
    segments = path.segments
    if not segments:
        if action != "project":
            raise CaptureValueCheckError("capture value root request is invalid")
        return
    if (
        len(segments) > max_depth
        or segments[0].kind is not ValuePathSegmentKind.VARIABLE
        or any(
            segment.kind in {
                ValuePathSegmentKind.VARIABLE,
                ValuePathSegmentKind.COLUMN,
            }
            for segment in segments[1:]
        )
    ):
        raise CaptureValueCheckError("capture value path is invalid")
    previous = segments[0]
    for segment in segments[1:]:
        if (
            previous.kind is ValuePathSegmentKind.ROW
            and segment.kind is not ValuePathSegmentKind.FIELD
        ):
            raise CaptureValueCheckError("capture value path is invalid")
        previous = segment


def _validate_wire_projection(request: ValueInspectionRequest) -> None:
    """Close root/role/view/exact combinations before BSL source is emitted."""
    if (
        not isinstance(request.view, ValueViewKind)
        or not isinstance(request.role, VariableRole)
        or type(request.parameter_names) is not tuple
    ):
        raise CaptureValueCheckError("capture value projection request is invalid")
    try:
        parameters = tuple(
            SafePathSegment(ValuePathSegmentKind.VARIABLE, name).key
            for name in request.parameter_names
        )
    except Exception:
        raise CaptureValueCheckError("capture value projection request is invalid") from None
    if len({name.casefold() for name in parameters}) != len(parameters):
        raise CaptureValueCheckError("capture value parameter names are ambiguous")

    path = request.path
    if not path.segments:
        if request.view is not ValueViewKind.VARIABLES:
            raise CaptureValueCheckError("capture value root view is invalid")
        if request.exact is not None:
            try:
                SafePathSegment(ValuePathSegmentKind.VARIABLE, request.exact)
            except Exception:
                raise CaptureValueCheckError(
                    "capture value root exact selector is invalid"
                ) from None
        if request.role is VariableRole.VARIABLES and parameters:
            raise CaptureValueCheckError(
                "capture value variable role cannot have parameter names"
            )
        return

    if (
        request.view is ValueViewKind.VARIABLES
        or request.role is not VariableRole.VARIABLES
        or parameters
    ):
        raise CaptureValueCheckError("capture value descendant view is invalid")
    named_view = request.view in {
        ValueViewKind.STRUCTURE_FIELDS,
        ValueViewKind.TABLE_COLUMNS,
        ValueViewKind.ROW_FIELDS,
    }
    indexed_view = request.view in {
        ValueViewKind.ARRAY_ITEMS,
        ValueViewKind.TABLE_ROWS,
    }
    if not (named_view or indexed_view):
        raise CaptureValueCheckError("capture value descendant view is invalid")
    terminal = path.segments[-1]
    if terminal.kind is ValuePathSegmentKind.ROW:
        if request.view is not ValueViewKind.ROW_FIELDS:
            raise CaptureValueCheckError("capture value terminal view is invalid")
    elif request.view is ValueViewKind.ROW_FIELDS:
        raise CaptureValueCheckError("capture value terminal view is invalid")
    if request.exact is None:
        return
    try:
        if named_view:
            SafePathSegment(ValuePathSegmentKind.FIELD, request.exact)
        elif type(request.exact) is not int or request.exact < 0:
            raise ValueError("indexed selector is invalid")
    except Exception:
        raise CaptureValueCheckError(
            "capture value descendant exact selector is invalid"
        ) from None


def _inspection_source(
    *,
    action: str,
    path: SafeValuePath,
    request: ValueInspectionRequest | None,
    limit: int | None,
    context_key: str,
    runtime_generation: int,
    context_generation: int,
    worker_type_registrations: tuple[str, ...],
    native_candidates: tuple[str, ...],
    max_items: int,
    max_bytes: int,
) -> str:
    suffix = uuid4().hex
    roots = f"__onecInspectionRoots{suffix}"
    context = f"__onecInspectionContext{suffix}"
    segments = f"__onecInspectionPath{suffix}"
    parameters = f"__onecInspectionParameters{suffix}"
    registrations = f"__onecInspectionWorkerTypes{suffix}"
    exact = f"__onecInspectionExact{suffix}"
    result = f"__onecInspectionProjection{suffix}"
    lines = [
        "Попытка",
        f"    {context} = RuntimeContextStoreServer.ПолучитьКонтекст();",
    ]
    if path.root.kind is ValueRootKind.CONTEXT:
        lines.append(f"    {roots} = {context}.КонтекстОтладки;")
    else:
        lines.append(f"    {roots} = Новый Структура;")
        for name in native_candidates:
            lines.append(
                f"    {roots}.Вставить({bsl_string_literal(name)}, {name});"
            )
    lines.append(f"    {registrations} = Новый Массив;")
    for index, registration in enumerate(worker_type_registrations):
        temporary = f"__onecInspectionWorker{index}{suffix}"
        lines.extend(
            (
                f"    {temporary} = ВнешниеОбработки.Создать("
                f"{bsl_string_literal(registration)}, Ложь);",
                f"    {registrations}.Добавить(ТипЗнч({temporary}));",
            )
        )
    lines.append(f"    {segments} = Новый Массив;")
    for segment in path.segments:
        item = f"__onecInspectionSegment{len(lines)}{suffix}"
        key = (
            bsl_string_literal(segment.key)
            if isinstance(segment.key, str)
            else str(segment.key)
        )
        lines.extend(
            (
                f"    {item} = Новый Структура;",
                f"    {item}.Вставить(\"kind\", {bsl_string_literal(segment.kind.value)});",
                f"    {item}.Вставить(\"key\", {key});",
                f"    {segments}.Добавить({item});",
            )
        )
    lines.append(f"    {parameters} = Новый Массив;")
    if request is not None:
        for parameter in request.parameter_names:
            lines.append(f"    {parameters}.Добавить({bsl_string_literal(parameter)});")
        exact_value = request.exact
        view = request.view.value
        role = request.role.value
        start, stop = request.start, request.stop
    else:
        exact_value = None
        view = ""
        role = "variables"
        start = stop = 0
    lines.append(
        f"    {exact} = "
        + (
            "Неопределено;"
            if exact_value is None
            else bsl_string_literal(exact_value)
            if isinstance(exact_value, str)
            else f"{exact_value};"
        )
    )
    # Project routes still pass the schema sentinel.  The helper rejects an
    # over-wide value table before it reads a selected row, including for a
    # direct controller caller that bypasses LocalCaptureValueAdapter.
    selected_limit = 101 if limit is None else limit
    lines.extend(
        (
            "    " + result + " = RuntimeValueTransferServer."
            "СпроецироватьЗначенияИнспекции("
            + f"{roots}, {segments}, {bsl_string_literal(action)}, "
            + f"{bsl_string_literal(view)}, {start}, {stop}, "
            + f"{bsl_string_literal(role)}, {parameters}, {exact}, "
            + f"{registrations}, {selected_limit}, {max_items}, {max_bytes});",
            f"    Если Не {result}.Доступ Тогда",
            '        Результат = "D|worker_generation_value";',
            "    Иначе",
            f"        {context}.Вставить({bsl_string_literal(context_key)}, {result}.Base64);",
            "        Результат = \"R|\" + "
            + f'Формат({runtime_generation}, "ЧГ=0; ЧДЦ=0") + "|" + '
            + f'Формат({context_generation}, "ЧГ=0; ЧДЦ=0") + "|" + '
            + f'Формат({result}.Размер, "ЧГ=0; ЧДЦ=0") + "|" + '
            + f"{result}.Хеш + \"|\" + "
            + f'Формат(СтрДлина({result}.Base64), "ЧГ=0; ЧДЦ=0");',
            "    КонецЕсли;",
            "Исключение",
            '    Результат = "E|value_admission_failed";',
            "КонецПопытки;",
        )
    )
    return "\n".join(lines)


def _decode_wire_payload(
    payload: bytes,
    *,
    action: str,
    path: SafeValuePath,
    request: ValueInspectionRequest | None,
    limit: int | None,
) -> object:
    if not isinstance(payload, bytes) or not payload or len(payload) > CaptureValuePolicy().max_bytes:
        raise CaptureValueCheckError("CAPTURE value payload is invalid")
    try:
        decoded = payload.decode("utf-8")
        document = json.loads(decoded, object_pairs_hook=_closed_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise CaptureValueCheckError("CAPTURE value payload is invalid") from None
    if not isinstance(document, dict):
        raise CaptureValueCheckError("CAPTURE value payload is invalid")
    if action == "project":
        return _decode_project(document, request)
    if action == "resolve":
        return _decode_resolve(document, path)
    return _decode_columns(document, limit)


def _exact_object(value: object, keys: frozenset[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys or any(
        not isinstance(key, str) for key in value
    ):
        raise CaptureValueCheckError("CAPTURE value payload is invalid")
    return value


def _closed_json_object(pairs: list[tuple[object, object]]) -> dict[str, object]:
    """Reject duplicate/non-string keys before the shape-specific parser runs."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if not isinstance(key, str) or key in result:
            raise ValueError("wire object keys are invalid")
        result[key] = value
    return result


def _decode_project(
    document: dict[str, object], request: ValueInspectionRequest | None,
) -> PrivateValueProjection:
    if request is None:
        raise CaptureValueCheckError("CAPTURE value payload is invalid")
    raw = _exact_object(
        document, frozenset({"v", "action", "entries", "total", "next"}),
    )
    if type(raw["v"]) is not int or raw["v"] != 1 or raw["action"] != "project":
        raise CaptureValueCheckError("CAPTURE value payload is invalid")
    entries = raw["entries"]
    total = raw["total"]
    next_cursor = raw["next"]
    if (
        not isinstance(entries, list)
        or len(entries) > request.stop - request.start
        or type(total) is not int
        or total < len(entries)
        or total > MAX_CAPTURE_VALUE_WIRE_TOTAL
        or next_cursor is not None and (type(next_cursor) is not int or next_cursor < 0)
    ):
        raise CaptureValueCheckError("CAPTURE value payload is invalid")
    expected_entries = max(0, min(request.stop, total) - request.start)
    expected_next = request.stop if request.stop < total else None
    if len(entries) != expected_entries or next_cursor != expected_next:
        raise CaptureValueCheckError("CAPTURE value payload is invalid")
    named = request.view in _NAMED_VIEWS
    projected = tuple(_decode_entry(item, named=named) for item in entries)
    selectors = tuple(entry.name for entry in projected)
    if len({_selector_key(item) for item in selectors}) != len(selectors):
        raise CaptureValueCheckError("CAPTURE value payload is invalid")
    return PrivateValueProjection(projected, total, next_cursor)


def _decode_resolve(
    document: dict[str, object], path: SafeValuePath,
) -> PrivateProjectedValue:
    raw = _exact_object(document, frozenset({"v", "action", "entry"}))
    if (
        type(raw["v"]) is not int
        or raw["v"] != 1
        or raw["action"] != "resolve"
        or not path.segments
    ):
        raise CaptureValueCheckError("CAPTURE value payload is invalid")
    terminal = path.segments[-1]
    entry = _decode_entry(
        raw["entry"], named=isinstance(terminal.key, str),
    )
    if not _same_name(entry.name, terminal.key):
        raise CaptureValueCheckError("CAPTURE value payload is invalid")
    return entry


def _decode_columns(document: dict[str, object], limit: int | None) -> tuple[str, ...]:
    raw = _exact_object(document, frozenset({"v", "action", "columns"}))
    columns = raw["columns"]
    if (
        type(raw["v"]) is not int
        or raw["v"] != 1
        or raw["action"] != "columns"
        or type(limit) is not int
        or not isinstance(columns, list)
        or len(columns) > limit
    ):
        raise CaptureValueCheckError("CAPTURE value payload is invalid")
    checked = tuple(
        SafePathSegment(ValuePathSegmentKind.COLUMN, item).key for item in columns
    )
    if len({item.casefold() for item in checked}) != len(checked):
        raise CaptureValueCheckError("CAPTURE value payload is invalid")
    return checked


def _decode_entry(value: object, *, named: bool) -> PrivateProjectedValue:
    raw = _exact_object(value, frozenset({"name", "denied"})) if isinstance(value, dict) and value.get("denied") is True else _exact_object(
        value,
        frozenset({"name", "denied", "type_name", "preview", "size", "shape", "cycle"}),
    )
    name = raw["name"]
    if named:
        name = SafePathSegment(ValuePathSegmentKind.FIELD, name).key
    elif type(name) is not int or name < 0:
        raise CaptureValueCheckError("CAPTURE value payload is invalid")
    denied = raw["denied"]
    if type(denied) is not bool:
        raise CaptureValueCheckError("CAPTURE value payload is invalid")
    if denied:
        return PrivateProjectedValue(name, lambda: _denied_metadata(), denied=True)
    type_name = raw["type_name"]
    preview = raw["preview"]
    size = raw["size"]
    shape = raw["shape"]
    cycle = raw["cycle"]
    if (
        not isinstance(type_name, str)
        or not isinstance(preview, str)
        or len(preview) > 512
        or size is not None and (type(size) is not int or size < 0 or size > MAX_CAPTURE_VALUE_WIRE_TOTAL)
        or not isinstance(shape, str)
        or type(cycle) is not bool
    ):
        raise CaptureValueCheckError("CAPTURE value payload is invalid")
    try:
        selected_shape = ValueShape(shape)
    except ValueError:
        raise CaptureValueCheckError("CAPTURE value payload is invalid") from None
    if selected_shape not in _PUBLIC_SHAPES:
        raise CaptureValueCheckError("CAPTURE value payload is invalid")
    metadata = ValueMetadata(type_name, preview, size, selected_shape)
    return PrivateProjectedValue(name, lambda: metadata, cycle=cycle)


def _same_name(left: str | int, right: str | int) -> bool:
    return (
        left.casefold() == right.casefold()
        if isinstance(left, str) and isinstance(right, str)
        else left == right
    )


def _selector_key(value: str | int) -> str | int:
    return value.casefold() if isinstance(value, str) else value


def _denied_metadata() -> ValueMetadata:
    raise CaptureValueCheckError("denied capture value metadata is unavailable")
