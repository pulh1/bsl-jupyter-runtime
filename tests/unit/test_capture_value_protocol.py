from __future__ import annotations

from base64 import b64encode
from hashlib import sha256
import json

import pytest

from onec_runtime.capture_value_protocol import (
    MAX_CAPTURE_VALUE_WIRE_TOTAL,
    NativeCandidatePage,
    VALUE_INSPECTION_CONTEXT_KEY_PREFIX,
    build_capture_value_inspection_envelope,
)
from onec_runtime.capture_values import (
    SafeValuePath,
    ValueInspectionRequest,
    ValuePathSegmentKind,
    ValueRoot,
    ValueRootKind,
    ValueViewKind,
    VariableRole,
)
from onec_runtime.errors import CaptureValueCheckError


def _project_envelope():
    path = SafeValuePath(ValueRoot(ValueRootKind.CONTEXT))
    return build_capture_value_inspection_envelope(
        action="project",
        path=path,
        request=ValueInspectionRequest(path, ValueViewKind.VARIABLES, 0, 1),
        limit=None,
        runtime_generation=7,
        context_generation=11,
        worker_type_registrations=(),
    )


def _admission(envelope, payload: bytes):  # type: ignore[no-untyped-def]
    encoded = b64encode(payload).decode("ascii")
    return envelope.parse_metadata(
        "R|7|11|{}|{}|{}".format(
            len(payload), sha256(payload).hexdigest(), len(encoded),
        )
    ), encoded


def test_production_value_envelope_uses_only_checked_in_helpers_and_a_private_key():
    envelope = _project_envelope()

    assert envelope.private_key.startswith(VALUE_INSPECTION_CONTEXT_KEY_PREFIX)
    assert "СпроецироватьЗначенияИнспекции" in envelope.source
    assert "RuntimeContextStoreServer.ПолучитьКонтекст()" in envelope.source
    assert "RuntimeKernelServer.УдалитьМатериализациюИзКонтекста" in envelope.cleanup_source
    assert "RuntimeContextStoreServer.ПолучитьКонтекст()" in envelope.cleanup_source
    assert "Результат = \"E|value_admission_failed\"" in envelope.source


def test_production_value_builder_rejects_fabricated_routes_before_emitting_bsl():
    root = SafeValuePath(ValueRoot(ValueRootKind.CONTEXT))
    variable = root.child(ValuePathSegmentKind.VARIABLE, "Value")
    row = variable.child(ValuePathSegmentKind.ROW, 0)
    invalid = (
        (root, ValueInspectionRequest(root, ValueViewKind.ARRAY_ITEMS, 0, 1)),
        (row, ValueInspectionRequest(row, ValueViewKind.TABLE_ROWS, 0, 1)),
        (
            root,
            ValueInspectionRequest(
                root,
                ValueViewKind.VARIABLES,
                MAX_CAPTURE_VALUE_WIRE_TOTAL + 1,
                MAX_CAPTURE_VALUE_WIRE_TOTAL + 2,
            ),
        ),
    )

    for path, request in invalid:
        with pytest.raises(CaptureValueCheckError):
            build_capture_value_inspection_envelope(
                action="project",
                path=path,
                request=request,
                limit=None,
                runtime_generation=7,
                context_generation=11,
                worker_type_registrations=(),
            )


def test_production_value_wire_parser_rejects_unknown_or_duplicate_layout_without_leaking_payload():
    envelope = _project_envelope()
    document = {
        "v": 1,
        "action": "project",
        "entries": [{
            "name": "Оклад",
            "denied": False,
            "type_name": "Число",
            "preview": "PRIVATE_PAYROLL_VALUE",
            "size": None,
            "shape": "scalar",
            "cycle": False,
            "unexpected": "PRIVATE_PAYROLL_VALUE",
        }],
        "total": 1,
        "next": None,
    }
    payload = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode()
    admission, content = _admission(envelope, payload)

    with pytest.raises(CaptureValueCheckError) as rejected:
        envelope.decode(admission, content)

    assert "PRIVATE_PAYROLL_VALUE" not in str(rejected.value)
    assert rejected.value.__cause__ is None and rejected.value.__context__ is None

    duplicate = (
        b'{"v":1,"v":1,"action":"project","entries":[],"total":0,"next":null}'
    )
    admission, content = _admission(envelope, duplicate)
    with pytest.raises(CaptureValueCheckError):
        envelope.decode(admission, content)


@pytest.mark.parametrize(
    "shape",
    (
        "scalar",
        "structure",
        "fixed_structure",
        "array",
        "fixed_array",
        "value_table",
        "value_table_row",
        "column",
    ),
)
def test_production_value_wire_parser_accepts_only_each_advertised_shape(shape: str):
    envelope = _project_envelope()
    document = {
        "v": 1,
        "action": "project",
        "entries": [{
            "name": "Значение",
            "denied": False,
            "type_name": "ПроверочныйТип",
            "preview": "",
            "size": 0 if shape != "scalar" else None,
            "shape": shape,
            "cycle": False,
        }],
        "total": 1,
        "next": None,
    }
    payload = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode()
    admission, content = _admission(envelope, payload)

    projection = envelope.decode(admission, content)

    assert projection.entries[0].describe().shape.value == shape


def test_native_candidate_page_compacts_the_wire_request_and_restores_public_cursor():
    path = SafeValuePath(ValueRoot(ValueRootKind.FRAME, 3))
    request = ValueInspectionRequest(path, ValueViewKind.VARIABLES, 50, 51)
    compact = NativeCandidatePage(
        ValueInspectionRequest(path, ValueViewKind.VARIABLES, 0, 1),
        ("V50",),
        101,
        51,
    )
    envelope = build_capture_value_inspection_envelope(
        action="project",
        path=path,
        request=request,
        limit=None,
        runtime_generation=7,
        context_generation=11,
        worker_type_registrations=(),
        native_candidates=("V50",),
        native_page=compact,
    )
    document = {
        "v": 1,
        "action": "project",
        "entries": [{
            "name": "V50",
            "denied": False,
            "type_name": "Число",
            "preview": "50",
            "size": None,
            "shape": "scalar",
            "cycle": False,
        }],
        "total": 1,
        "next": None,
    }
    payload = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode()
    admission, content = _admission(envelope, payload)

    projection = envelope.decode(admission, content)

    assert 'Вставить("V50", V50);' in envelope.source
    assert 'Вставить("V0", V0);' not in envelope.source
    assert projection.total == 101 and projection.next_cursor == 51
    assert [entry.name for entry in projection.entries] == ["V50"]


def test_native_candidate_page_is_closed_over_the_compact_request_and_names():
    path = SafeValuePath(ValueRoot(ValueRootKind.FRAME, 3))
    request = ValueInspectionRequest(
        path,
        ValueViewKind.VARIABLES,
        0,
        1,
        VariableRole.VARIABLES,
    )
    invalid = (
        NativeCandidatePage(
            ValueInspectionRequest(path, ValueViewKind.VARIABLES, 1, 2),
            ("V0",),
            1,
            None,
        ),
        NativeCandidatePage(
            ValueInspectionRequest(path, ValueViewKind.VARIABLES, 0, 1),
            ("V1",),
            1,
            None,
        ),
    )

    for compact in invalid:
        with pytest.raises(CaptureValueCheckError):
            build_capture_value_inspection_envelope(
                action="project",
                path=path,
                request=request,
                limit=None,
                runtime_generation=7,
                context_generation=11,
                worker_type_registrations=(),
                native_candidates=("V0",),
                native_page=compact,
            )
