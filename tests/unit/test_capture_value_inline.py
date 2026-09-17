"""Inline CAPTURE value transfer uses one expression and no cleanup key."""

from __future__ import annotations

from base64 import b64encode
from hashlib import sha256
import json

import pytest

from onec_runtime.capture_value_protocol import (
    MAX_CAPTURE_VALUE_INSPECTION_SOURCE_BYTES,
    build_capture_value_inline_expression,
    build_capture_value_inspection_envelope,
)
from onec_runtime.capture_values import (
    SafeValuePath, ValueInspectionRequest, ValueRoot, ValueRootKind,
    ValueViewKind,
)
from onec_runtime.errors import CaptureValueCheckError


def _inline(*, native: bool = False):
    path = SafeValuePath(ValueRoot(
        ValueRootKind.FRAME if native else ValueRootKind.CONTEXT,
        0 if native else None,
    ))
    return build_capture_value_inline_expression(
        action="project", path=path,
        request=ValueInspectionRequest(path, ValueViewKind.VARIABLES, 0, 1),
        limit=None, runtime_generation=7, context_generation=11,
        worker_type_registrations=(),
        native_candidates=("ЛокальныйСчетчик",) if native else (),
    )


def _wire(payload: bytes) -> str:
    encoded = b64encode(payload).decode("ascii")
    return "R|7|11|{}|{}|{}|{}".format(
        len(payload), sha256(payload).hexdigest(), len(encoded), encoded,
    )


def test_inline_context_and_native_sources_use_one_helper_call() -> None:
    context = _inline()
    native = _inline(native=True)

    assert context.source.startswith(
        "RuntimeValueTransferServer.СериализоватьИнспекциюДляОтладки("
        "Контекст.КонтекстОтладки, "
    )
    assert 'Новый Структура("ЛокальныйСчетчик", ЛокальныйСчетчик)' in native.source
    for built in (context, native):
        assert "RuntimeContextStoreServer" not in built.source
        assert "__onec_value_" not in built.source
        assert "\n" not in built.source
        assert built.max_text_size >= 90_000


def test_inline_payload_decodes_verified_page_and_rejects_tampering() -> None:
    built = _inline()
    document = {
        "v": 1, "action": "project", "entries": [], "total": 0,
        "next": None,
    }
    payload = json.dumps(document, separators=(",", ":")).encode()
    page = built.decode(_wire(payload))
    assert page.total == 0 and page.entries == ()

    with pytest.raises(CaptureValueCheckError):
        built.decode(_wire(payload)[:-1] + "A")
    with pytest.raises(CaptureValueCheckError):
        built.decode("E|value_admission_failed")


def test_inline_request_is_bounded_by_its_own_source_not_the_legacy_program() -> None:
    """A legal inline request must not inherit an unused legacy source limit."""
    path = SafeValuePath(ValueRoot(ValueRootKind.CONTEXT))
    request = ValueInspectionRequest(path, ValueViewKind.VARIABLES, 0, 1)
    registrations = tuple(
        f"r{index}" + "x" * (1_750 - len(f"r{index}"))
        for index in range(32)
    )
    arguments = dict(
        action="project", path=path, request=request, limit=None,
        runtime_generation=7, context_generation=11,
        worker_type_registrations=registrations,
    )

    with pytest.raises(CaptureValueCheckError, match="source exceeds its budget"):
        build_capture_value_inspection_envelope(**arguments)

    inline = build_capture_value_inline_expression(**arguments)
    assert len(inline.source.encode("utf-8")) <= MAX_CAPTURE_VALUE_INSPECTION_SOURCE_BYTES
