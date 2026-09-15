from __future__ import annotations

from base64 import b64decode
import binascii
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
import re
from uuid import uuid4

import pandas as pd

from onec_runtime.compact_table import decode_compact_table_payload
from onec_runtime.capture_evaluation import AdmissionEnvelopeV1, CaptureTransferPlan
from onec_runtime.errors import CaptureValueCheckError, ProtocolError
from onec_runtime.experiment import bsl_string_literal
from onec_runtime.performance_profile import PhaseRecorder
from onec_runtime.rdbg.models import CollectionRow
from onec_runtime.table_materialization import ReferenceMode, ReferencePolicy


_HANDLE = re.compile(
    r"Контекст\.[^\W\d]\w*(?:\.[^\W\d]\w*)*\Z",
    re.UNICODE,
)
_CONTEXT_KEY = re.compile(r"__onec_compact_table_[0-9a-f]{32}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_SCALAR_KINDS = frozenset(
    {"string", "nullable_string", "boolean", "integer", "number", "datetime", "uuid"}
)


@dataclass(frozen=True, slots=True)
class CompactColumn:
    name: str
    kind: str
    is_reference: bool


def _compact_cell_kind(type_name: str) -> tuple[str, bool] | None:
    if type_name in {"Неопределено", "Null"}:
        return None
    if type_name.startswith("ПеречислениеСсылка."):
        return "nullable_string", False
    if "Ссылка." in type_name:
        return "reference", True
    kinds = {
        "Строка": "nullable_string",
        "Булево": "boolean",
        "Число": "number",
        "Дата": "datetime",
        "УникальныйИдентификатор": "uuid",
    }
    kind = kinds.get(type_name)
    if kind is None:
        raise ProtocolError("unsupported compact table column type")
    return kind, False


def infer_compact_columns(
    rows: tuple[CollectionRow, ...],
) -> tuple[CompactColumn, ...] | None:
    if not rows:
        return None
    names = tuple(cell.name for cell in rows[0].cells)
    if not names or len(set(names)) != len(names):
        raise ProtocolError("compact table frame schema is invalid")
    observed: list[tuple[str, bool] | None] = [None] * len(names)
    for row in rows:
        if tuple(cell.name for cell in row.cells) != names:
            raise ProtocolError("compact table frame schema changed")
        for ordinal, cell in enumerate(row.cells):
            candidate = _compact_cell_kind(cell.type_name)
            if candidate is None:
                continue
            if observed[ordinal] is None:
                observed[ordinal] = candidate
            elif observed[ordinal] != candidate:
                raise ProtocolError("compact table frame column type changed")
    if any(item is None for item in observed):
        return None
    return tuple(
        CompactColumn(name, item[0], item[1])
        for name, item in zip(names, observed, strict=True)
        if item is not None
    )


def infer_declared_compact_columns(
    rows: tuple[CollectionRow, ...],
) -> tuple[CompactColumn, ...] | None:
    if not rows:
        return None
    columns: list[CompactColumn] = []
    for row in rows:
        cells = {cell.name: cell for cell in row.cells}
        if set(cells) != {"Имя", "Вид", "Ссылка"}:
            raise ProtocolError("declared compact table schema is invalid")
        name = cells["Имя"].value_string
        kind = cells["Вид"].value_string
        is_reference = cells["Ссылка"].value_boolean
        if not name or is_reference is None:
            raise ProtocolError("declared compact table column is invalid")
        if not kind:
            return None
        if kind not in _SCALAR_KINDS | {"reference"}:
            raise ProtocolError("declared compact table kind is invalid")
        if (kind == "reference") != is_reference:
            raise ProtocolError("declared compact table reference marker is invalid")
        columns.append(CompactColumn(name, kind, is_reference))
    if len({column.name for column in columns}) != len(columns):
        raise ProtocolError("declared compact table column names are invalid")
    return tuple(columns)


def _reference_mode(value: str | ReferenceMode) -> ReferenceMode:
    try:
        return ReferenceMode(value)
    except ValueError as error:
        raise ProtocolError("unknown table reference mode") from error


def build_compact_transfer_instruction(
    handle: str,
    policy: ReferencePolicy,
    context_key: str,
    *,
    runtime_generation: int,
    context_generation: int,
    max_rows: int | None = None,
    max_payload_bytes: int | None = None,
    worker_type_registrations: tuple[str, ...] = (),
) -> str:
    if not _HANDLE.fullmatch(handle):
        raise ProtocolError(
            "table handle must be one direct or dotted persistent Context path"
        )
    if not _CONTEXT_KEY.fullmatch(context_key):
        raise ProtocolError("compact table context key is invalid")
    if runtime_generation <= 0 or context_generation <= 0:
        raise ProtocolError("table transfer generations must be positive")
    bounded_rows = _optional_budget(max_rows, "table row budget")
    bounded_bytes = _optional_budget(max_payload_bytes, "table byte budget")
    default = _reference_mode(policy.refs)
    overrides = dict(policy.ref_columns or {})
    if any(not isinstance(registration, str) or not registration for registration in worker_type_registrations):
        raise ProtocolError("table Worker type registrations are invalid")
    lines = ["Попытка", "ТипыОбъектовWorker = Новый Массив;"]
    for index, registration in enumerate(worker_type_registrations):
        lines.extend(
            (
                f"ВременныйОбъектWorker{index} = ВнешниеОбработки.Создать("
                f"{bsl_string_literal(registration)}, Ложь);",
                f"ТипыОбъектовWorker.Добавить(ТипЗнч(ВременныйОбъектWorker{index}));",
            )
        )
    lines.append("РежимыСсылокМатериализации = Новый Соответствие;")
    for column in sorted(overrides):
        if not isinstance(column, str) or not column:
            raise ProtocolError("table reference column name is invalid")
        mode = _reference_mode(overrides[column])
        lines.append(
            "РежимыСсылокМатериализации.Вставить("
            f"{bsl_string_literal(column)}, {bsl_string_literal(mode.value)});"
        )
    lines.extend(
        [
            "Материализация = "
            "RuntimeTableTransferServer.СериализоватьКомпактнуюТаблицу("
            f"{handle}, {bsl_string_literal(default.value)}, "
            "РежимыСсылокМатериализации, "
            f"ТипыОбъектовWorker, {bounded_rows}, {bounded_bytes});",
            "Если Не Материализация.Доступ Тогда",
            '    Результат = "D|worker_generation_value";',
            "Иначе",
            f"    Контекст.Вставить({bsl_string_literal(context_key)}, "
            "Материализация.Base64);",
            "    Результат = \"R|\" + "
            f"Формат({runtime_generation}, \"ЧГ=0; ЧДЦ=0\") + \"|\" + "
            f"Формат({context_generation}, \"ЧГ=0; ЧДЦ=0\") + \"|\" + "
            "Формат(Материализация.Размер, \"ЧГ=0; ЧДЦ=0\") + \"|\" + "
            "Материализация.Хеш + \"|\" + "
            "Формат(СтрДлина(Материализация.Base64), "
            "\"ЧГ=0; ЧДЦ=0\");",
            "КонецЕсли;",
            "Исключение",
            '    Результат = "E|value_admission_failed";',
            "КонецПопытки;",
        ]
    )
    return "\n".join(lines)


def _optional_budget(value: int | None, label: str) -> int:
    if value is None:
        return 0
    if type(value) is not int or value <= 0:
        raise ProtocolError(f"{label} must be positive")
    return value


class CompactRuntimeTableTransfer:
    def __init__(
        self,
        instruction_executor: Callable[[str], object],
        context_reader: Callable[[str, int], str],
        *,
        runtime_generation: Callable[[], int],
        context_generation: int,
        context_cleaner: Callable[[str], None] | None = None,
        max_text_size: int = 100_000_000,
        max_payload_bytes: int = 75_000_000,
        max_rows: int | None = None,
        key_factory: Callable[[], str] | None = None,
        profiler: PhaseRecorder | None = None,
        worker_type_registrations: Callable[[], tuple[str, ...]] | None = None,
        capture_executor: Callable[[CaptureTransferPlan], bytes] | None = None,
    ) -> None:
        self._capture_execute = capture_executor
        self._execute = instruction_executor
        self._read = context_reader
        self._runtime_generation = runtime_generation
        self._context_generation = context_generation
        self._clean = context_cleaner
        self._expected_runtime_generation = runtime_generation()
        self._max_text_size = max_text_size
        if max_payload_bytes <= 0:
            raise ValueError("max payload bytes must be positive")
        self._max_payload_bytes = max_payload_bytes
        self._max_rows = _optional_budget(max_rows, "table row budget")
        self._key_factory = key_factory or (
            lambda: "__onec_compact_table_" + uuid4().hex
        )
        self._profiler = profiler
        self._worker_type_registrations = worker_type_registrations

    def _profile(self, phase: str, operation, **metadata):  # type: ignore[no-untyped-def]
        if self._profiler is None:
            return operation()
        return self._profiler.measure(phase, operation, **metadata)

    def to_df(self, handle: str, policy: ReferencePolicy) -> pd.DataFrame:
        payload = self.payload(handle, policy)
        return self._profile(
            "table.build_dataframe",
            lambda: decode_compact_table_payload(payload, policy),
            input_bytes=len(payload),
            item_count=lambda frame: len(frame.index),
        )

    def prepare_payload(self, handle: str, policy: ReferencePolicy) -> CaptureTransferPlan:
        generation = self._runtime_generation()
        if generation != self._expected_runtime_generation:
            raise ProtocolError("table materializer runtime generation is stale")
        key = self._key_factory()
        source = build_compact_transfer_instruction(
            handle,
            policy,
            key,
            runtime_generation=generation,
            context_generation=self._context_generation,
            max_rows=self._max_rows or None,
            max_payload_bytes=self._max_payload_bytes,
            worker_type_registrations=(
                ()
                if self._worker_type_registrations is None
                else self._worker_type_registrations()
            ),
        )

        def decode(metadata: object, content: str) -> bytes:
            byte_count, base64_count, payload_hash = self._validate_metadata(metadata, generation)
            if len(content) != base64_count:
                raise CaptureValueCheckError("CAPTURE value admission result is invalid")
            try:
                payload = self._profile(
                    "table.decode_base64",
                    lambda: b64decode("".join(content.split()), validate=True),
                    input_bytes=len(content.encode("ascii")),
                    output_bytes=len,
                )
            except (ValueError, binascii.Error) as error:
                raise ProtocolError("compact table Base64 payload is invalid") from error
            if len(payload) != byte_count or sha256(payload).hexdigest() != payload_hash:
                raise CaptureValueCheckError("CAPTURE value payload integrity check failed")
            return payload

        return CaptureTransferPlan(
            source, key, f"Контекст.Удалить({bsl_string_literal(key)});\nРезультат = Истина;",
            self._max_text_size, decode,
        )

    def _validate_metadata(self, metadata: object, generation: int) -> tuple[int, int, str]:
        observed = AdmissionEnvelopeV1.parse(
            metadata,
            max_payload_bytes=self._max_payload_bytes,
            max_base64_chars=self._max_text_size,
        )
        if (
            observed.runtime_generation != generation
            or observed.context_generation != self._context_generation
        ):
            raise CaptureValueCheckError("CAPTURE value admission result is invalid")
        return observed.payload_bytes, observed.base64_chars, observed.payload_sha256

    def payload(self, handle: str, policy: ReferencePolicy) -> bytes:
        plan = self.prepare_payload(handle, policy)
        if self._capture_execute is not None:
            return self._capture_execute(plan)
        metadata = self._profile(
            "table.prepare_jsonl", lambda: self._execute(plan.instruction),
            input_bytes=len(plan.instruction.encode("utf-8")),
        )
        try:
            self._validate_metadata(metadata, self._expected_runtime_generation)
        except ProtocolError:
            if self._clean is not None:
                self._clean(plan.private_key)
            raise
        content = self._profile(
            "table.transfer_base64", lambda: self._read(plan.private_key, self._max_text_size),
            output_bytes=lambda value: len(value.encode("ascii")),
        )
        return plan.decode(metadata, content)
