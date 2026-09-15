from __future__ import annotations

from base64 import b64decode
import binascii
from collections.abc import Callable
from hashlib import sha256
import re
from uuid import uuid4

from onec_runtime.capture_evaluation import CaptureTransferPlan
from onec_runtime.errors import ProtocolError
from onec_runtime.experiment import bsl_string_literal
from onec_runtime.performance_profile import PhaseRecorder
from onec_runtime.value_materialization import (
    MaterializationOptions,
    decode_value_payload,
)


VALUE_CONTEXT_KEY_PREFIX = "__onec_value_"
_HANDLE = re.compile(
    r"Контекст\.[^\W\d]\w*(?:\.[^\W\d]\w*)*\Z",
    re.UNICODE,
)
_CONTEXT_KEY = re.compile(r"__onec_value_[0-9a-f]{32}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_ERROR_ENVELOPE_MAX_BYTES = 4096


def build_value_transfer_instruction(
    handle: str,
    options: MaterializationOptions,
    context_key: str,
    *,
    runtime_generation: int,
    context_generation: int,
) -> str:
    validate_value_handle(handle)
    if not _CONTEXT_KEY.fullmatch(context_key):
        raise ProtocolError("value materialization context key is invalid")
    if runtime_generation <= 0 or context_generation <= 0:
        raise ProtocolError("value transfer generations must be positive")
    return "\n".join(
        (
            "МатериализацияЗначения = "
            "RuntimeValueTransferServer.СериализоватьЗначение("
            f"{handle}, {bsl_string_literal(options.refs)}, "
            f"{options.max_depth}, {options.max_items}, {options.max_bytes});",
            f"Контекст.Вставить({bsl_string_literal(context_key)}, "
            "МатериализацияЗначения.Base64);",
            "Результат = "
            f'Формат({runtime_generation}, "ЧГ=0; ЧДЦ=0") + "|" + '
            f'Формат({context_generation}, "ЧГ=0; ЧДЦ=0") + "|" + '
            'Формат(МатериализацияЗначения.Размер, "ЧГ=0; ЧДЦ=0") + "|" + '
            'МатериализацияЗначения.Хеш + "|" + '
            "Формат(СтрДлина(МатериализацияЗначения.Base64), "
            '"ЧГ=0; ЧДЦ=0");',
        )
    )


def validate_value_handle(handle: str) -> str:
    if not isinstance(handle, str) or not _HANDLE.fullmatch(handle):
        raise ProtocolError(
            "value handle must be one direct or dotted persistent Context path"
        )
    return handle


class RuntimeValueTransfer:
    def __init__(
        self,
        instruction_executor: Callable[[str], object],
        context_reader: Callable[[str, int], str],
        *,
        context_cleaner: Callable[[str], None],
        runtime_generation: Callable[[], int],
        context_generation: int,
        key_factory: Callable[[], str] | None = None,
        profiler: PhaseRecorder | None = None,
        capture_executor: Callable[[CaptureTransferPlan], bytes] | None = None,
    ) -> None:
        self._capture_execute = capture_executor
        self._execute = instruction_executor
        self._read = context_reader
        self._clean = context_cleaner
        self._runtime_generation = runtime_generation
        self._context_generation = context_generation
        self._expected_runtime_generation = runtime_generation()
        self._key_factory = key_factory or (lambda: VALUE_CONTEXT_KEY_PREFIX + uuid4().hex)
        self._profiler = profiler

    def materialize(self, handle: str, options: MaterializationOptions) -> object:
        payload = self.payload(handle, options)
        return self._profile(
            "value.decode_snapshot",
            lambda: decode_value_payload(payload, options),
            input_bytes=len(payload),
        )

    def prepare_payload(self, handle: str, options: MaterializationOptions) -> CaptureTransferPlan:
        generation = self._runtime_generation()
        if generation != self._expected_runtime_generation:
            raise ProtocolError("value materializer runtime generation is stale")
        key = self._key_factory()
        source = build_value_transfer_instruction(
            handle,
            options,
            key,
            runtime_generation=generation,
            context_generation=self._context_generation,
        )
        maximum_transfer_bytes = max(options.max_bytes, _ERROR_ENVELOPE_MAX_BYTES)
        maximum_text_size = _base64_length(maximum_transfer_bytes)

        def decode(metadata: object, content: str) -> bytes:
            observed = _parse_metadata(metadata)
            if (
                observed[0] != generation
                or observed[1] != self._context_generation
                or observed[2] <= 0
                or observed[2] > maximum_transfer_bytes
                or observed[4] <= 0
                or observed[4] > maximum_text_size
                or len(content) != observed[4]
            ):
                raise ProtocolError("value materialization metadata is invalid")
            try:
                payload = self._profile(
                    "value.decode_base64",
                    lambda: b64decode("".join(content.split()), validate=True),
                    input_bytes=len(content.encode("ascii")),
                    output_bytes=len,
                )
            except (ValueError, binascii.Error) as error:
                raise ProtocolError("value materialization Base64 payload is invalid") from error
            if len(payload) != observed[2] or sha256(payload).hexdigest() != observed[3]:
                raise ProtocolError("value materialization payload integrity check failed")
            return payload

        return CaptureTransferPlan(
            source, key, f"Контекст.Удалить({bsl_string_literal(key)});\nРезультат = Истина;",
            maximum_text_size, decode,
        )

    def payload(self, handle: str, options: MaterializationOptions) -> bytes:
        plan = self.prepare_payload(handle, options)
        if self._capture_execute is not None:
            return self._capture_execute(plan)
        consumed = False
        primary_error: BaseException | None = None
        try:
            metadata = self._profile("value.prepare", lambda: self._execute(plan.instruction),
                                     input_bytes=len(plan.instruction.encode("utf-8")))
            content = self._profile("value.transfer_base64",
                                    lambda: self._read(plan.private_key, plan.max_text_size),
                                    output_bytes=lambda value: len(value.encode("ascii")))
            consumed = True
            return plan.decode(metadata, content)
        except BaseException as error:
            primary_error = error
            raise
        finally:
            # Synchronous MAIN transport has no coordinator-owned request.
            if not consumed:
                try:
                    self._clean(plan.private_key)
                except BaseException as cleanup_error:
                    if primary_error is not None:
                        primary_error.add_note(
                            "value materialization cleanup failed: "
                            f"{type(cleanup_error).__name__}: {cleanup_error}"
                        )
                    else:
                        raise

    def _profile(self, phase: str, operation, **metadata):  # type: ignore[no-untyped-def]
        if self._profiler is None:
            return operation()
        return self._profiler.measure(phase, operation, **metadata)


def _parse_metadata(value: object) -> tuple[int, int, int, str, int]:
    if not isinstance(value, str):
        raise ProtocolError("value materialization metadata is not a string")
    fields = value.split("|")
    if len(fields) != 5:
        raise ProtocolError("value materialization metadata field count is invalid")
    try:
        runtime_generation = int(fields[0])
        context_generation = int(fields[1])
        byte_count = int(fields[2])
        base64_count = int(fields[4])
    except ValueError as error:
        raise ProtocolError("value materialization metadata number is invalid") from error
    payload_hash = fields[3]
    if not _HASH.fullmatch(payload_hash):
        raise ProtocolError("value materialization metadata hash is invalid")
    return (
        runtime_generation,
        context_generation,
        byte_count,
        payload_hash,
        base64_count,
    )


def _base64_length(byte_count: int) -> int:
    return ((byte_count + 2) // 3) * 4
