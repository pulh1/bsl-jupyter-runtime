from __future__ import annotations

import json
from base64 import b64encode
from dataclasses import dataclass
from hashlib import sha256
from re import fullmatch
from typing import Literal
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID

from onec_runtime.errors import ProtocolError
from onec_runtime.experiment import bsl_string_literal


MAX_WORKER_STAGE_BATCH_SIZE = 10
MAX_WORKER_STAGE_OUTCOME_BYTES = 128 * 1024
MAX_WORKER_STAGE_URL_CODEPOINTS = 4096
MAX_WORKER_STAGE_DIAGNOSTIC_CODEPOINTS = 4096
WORKER_STAGE_SCHEMA = "onec-worker-stage-batch-receipt"
WORKER_STAGE_SCHEMA_VERSION = 2


def _safe_identifier(value: object) -> bool:
    return (
        isinstance(value, str)
        and fullmatch(r"[A-Za-z_\u0400-\u04ff][A-Za-z0-9_\u0400-\u04ff]*", value)
        is not None
    )


def _ascii_registration_name(value: object) -> bool:
    return (
        type(value) is str
        and fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) is not None
    )


def _sha256(value: object) -> bool:
    return type(value) is str and fullmatch(r"[0-9a-f]{64}", value) is not None


def _temp_storage_session_id(value: object) -> str | None:
    if (
        type(value) is not str
        or not value
        or len(value) > MAX_WORKER_STAGE_URL_CODEPOINTS
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        return None
    split = urlsplit(value)
    if (
        split.scheme
        or split.netloc
        or split.fragment
        or not split.path.startswith("e1cib/tempstorage/")
        or split.path == "e1cib/tempstorage/"
    ):
        return None
    try:
        query = parse_qsl(split.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return None
    seance_ids = [item for name, item in query if name == "seanceId"]
    return seance_ids[0] if len(seance_ids) == 1 and seance_ids[0] else None


def _qualified_temp_storage_url(value: object) -> bool:
    return _temp_storage_session_id(value) is not None


@dataclass(frozen=True, slots=True, repr=False)
class WorkerStageEntry:
    logical_name: str
    artifact_sha256: str
    registration_name: str
    artifact_bytes: bytes

    def __post_init__(self) -> None:
        if (
            type(self.logical_name) is not str
            or not _safe_identifier(self.logical_name)
            or not _sha256(self.artifact_sha256)
            or not _ascii_registration_name(self.registration_name)
            or type(self.artifact_bytes) is not bytes
            or not self.artifact_bytes
        ):
            raise ValueError("worker stage entry is invalid")

    def __repr__(self) -> str:
        return (
            "WorkerStageEntry("
            f"logical_name={self.logical_name!r}, "
            f"artifact_sha256={self.artifact_sha256!r}, payload=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class WorkerStageBatch:
    batch_index: int
    entries: tuple[WorkerStageEntry, ...]

    def __post_init__(self) -> None:
        if (
            type(self.batch_index) is not int
            or self.batch_index < 0
            or type(self.entries) is not tuple
            or not self.entries
            or len(self.entries) > MAX_WORKER_STAGE_BATCH_SIZE
            or not all(type(entry) is WorkerStageEntry for entry in self.entries)
        ):
            raise ValueError("worker stage batch is invalid")
        registrations = tuple(entry.registration_name.casefold() for entry in self.entries)
        if len(set(registrations)) != len(registrations):
            raise ValueError("worker stage batch registrations are not unique")


def build_worker_stage_batches(
    entries: tuple[WorkerStageEntry, ...],
) -> tuple[WorkerStageBatch, ...]:
    if type(entries) is not tuple or not all(
        type(entry) is WorkerStageEntry for entry in entries
    ):
        raise ValueError("worker stage entries are invalid")
    registrations = tuple(entry.registration_name.casefold() for entry in entries)
    if len(set(registrations)) != len(registrations):
        raise ValueError("worker stage registrations are not unique")
    return tuple(
        WorkerStageBatch(
            index // MAX_WORKER_STAGE_BATCH_SIZE,
            entries[index : index + MAX_WORKER_STAGE_BATCH_SIZE],
        )
        for index in range(0, len(entries), MAX_WORKER_STAGE_BATCH_SIZE)
    )


def _positive_index(value: object) -> bool:
    return type(value) is int and value > 0


def _batch_count_is_valid(batch: WorkerStageBatch, batch_count: object) -> bool:
    return _positive_index(batch_count) and batch.batch_index < batch_count


def worker_stage_batch_digest(batch: WorkerStageBatch, batch_count: int) -> str:
    if type(batch) is not WorkerStageBatch or not _batch_count_is_valid(
        batch, batch_count
    ):
        raise ValueError("worker stage batch coordinates are invalid")
    identity = {
        "batch_index": batch.batch_index,
        "batch_count": batch_count,
        "entries": [
            {
                "logical_name": entry.logical_name,
                "registration_name": entry.registration_name,
                "artifact_sha256": entry.artifact_sha256,
            }
            for entry in batch.entries
        ],
    }
    payload = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True, repr=False)
class WorkerStageRegistrationReceipt:
    registration_name: str
    artifact_sha256: str
    temp_storage_url: str

    def __post_init__(self) -> None:
        if (
            not _ascii_registration_name(self.registration_name)
            or not _sha256(self.artifact_sha256)
            or not _qualified_temp_storage_url(self.temp_storage_url)
        ):
            raise ValueError("worker stage registration receipt is invalid")

    def __repr__(self) -> str:
        return (
            "WorkerStageRegistrationReceipt("
            f"registration_name={self.registration_name!r}, "
            f"artifact_sha256={self.artifact_sha256!r}, "
            "temp_storage_url=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class WorkerStageFailure:
    item_index: int
    phase: Literal["decode", "upload", "connect", "registration-result"]
    outcome: Literal["known_pre_swap", "registration_outcome_unknown"]
    diagnostic: str
    orphan_url: str | None

    def __post_init__(self) -> None:
        if (
            type(self.item_index) is not int
            or self.item_index < 0
            or type(self.phase) is not str
            or self.phase not in {"decode", "upload", "connect", "registration-result"}
            or type(self.outcome) is not str
            or self.outcome not in {
                "known_pre_swap",
                "registration_outcome_unknown",
            }
            or type(self.diagnostic) is not str
            or len(self.diagnostic) > MAX_WORKER_STAGE_DIAGNOSTIC_CODEPOINTS
            or (
                self.orphan_url is not None
                and not _qualified_temp_storage_url(self.orphan_url)
            )
        ):
            raise ValueError("worker stage failure is invalid")
        expected_outcome = (
            "known_pre_swap"
            if self.phase in {"decode", "upload"}
            else "registration_outcome_unknown"
        )
        if self.outcome != expected_outcome:
            raise ValueError("worker stage failure outcome is invalid")
        if self.phase in {"connect", "registration-result"} and self.orphan_url is None:
            raise ValueError("worker stage failure orphan URL is missing")

    def __repr__(self) -> str:
        return (
            "WorkerStageFailure("
            f"item_index={self.item_index!r}, phase={self.phase!r}, "
            f"outcome={self.outcome!r}, diagnostic=<redacted>, "
            "orphan_url=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class WorkerStageBatchOutcome:
    transaction_id: UUID
    batch_index: int
    batch_count: int
    batch_digest: str
    status: Literal["succeeded", "failed"]
    connected: tuple[WorkerStageRegistrationReceipt, ...]
    failure: WorkerStageFailure | None

    def __post_init__(self) -> None:
        if (
            type(self.transaction_id) is not UUID
            or type(self.batch_index) is not int
            or self.batch_index < 0
            or not _positive_index(self.batch_count)
            or self.batch_index >= self.batch_count
            or not _sha256(self.batch_digest)
            or type(self.status) is not str
            or self.status not in {"succeeded", "failed"}
            or type(self.connected) is not tuple
            or len(self.connected) > MAX_WORKER_STAGE_BATCH_SIZE
            or not all(
                type(item) is WorkerStageRegistrationReceipt
                for item in self.connected
            )
            or (
                self.failure is not None
                and type(self.failure) is not WorkerStageFailure
            )
            or (self.status == "succeeded") != (self.failure is None)
        ):
            raise ValueError("worker stage batch outcome is invalid")

    def __repr__(self) -> str:
        return (
            "WorkerStageBatchOutcome("
            f"transaction_id={self.transaction_id!r}, "
            f"batch_index={self.batch_index!r}, batch_count={self.batch_count!r}, "
            f"batch_digest={self.batch_digest!r}, status={self.status!r}, "
            f"connected={self.connected!r}, failure={self.failure!r})"
        )


def stage_worker_batch_instruction(
    batch: WorkerStageBatch,
    *,
    batch_count: int,
    transaction_id: UUID,
) -> str:
    """Upload and connect a sealed batch and return one bounded JSON outcome."""
    if (
        type(batch) is not WorkerStageBatch
        or not _batch_count_is_valid(batch, batch_count)
        or type(transaction_id) is not UUID
    ):
        raise ValueError("worker stage batch coordinates are invalid")

    digest = worker_stage_batch_digest(batch, batch_count)
    lines = [
        "ПодключенныеWorker = Новый Массив;",
        "ОтказWorker = Неопределено;",
    ]
    for item_index, entry in enumerate(batch.entries):
        suffix = str(item_index)
        encoded = bsl_string_literal(b64encode(entry.artifact_bytes).decode("ascii"))
        registration = bsl_string_literal(entry.registration_name)
        artifact_digest = bsl_string_literal(entry.artifact_sha256)
        lines.extend(
            (
                "Если ОтказWorker = Неопределено Тогда",
                f'    ФазаWorker{suffix} = "decode";',
                f"    АдресАртефактаWorker{suffix} = Неопределено;",
                "    Попытка",
                f"        ДанныеАртефактаWorker{suffix} = Base64Значение({encoded});",
                f'        ФазаWorker{suffix} = "upload";',
                f"        АдресАртефактаWorker{suffix} = Строка("
                f"ПоместитьВоВременноеХранилище(ДанныеАртефактаWorker{suffix}));",
                f'        ФазаWorker{suffix} = "connect";',
                f"        ИмяАртефактаWorker{suffix} = ВнешниеОбработки.Подключить("
                f"АдресАртефактаWorker{suffix}, {registration}, Ложь);",
                f"        Если ИмяАртефактаWorker{suffix} <> {registration} Тогда",
                f'            ФазаWorker{suffix} = "registration-result";',
                f"            ОтказWorker = Новый Структура("
                '"item_index,phase,outcome,diagnostic,orphan_url", '
                f'{item_index}, ФазаWorker{suffix}, "registration_outcome_unknown", '
                '"registration identity mismatch", '
                f"АдресАртефактаWorker{suffix});",
                "        Иначе",
                "            ПодключениеWorker = Новый Структура("
                '"registration_name,artifact_sha256,temp_storage_url", '
                f"{registration}, {artifact_digest}, АдресАртефактаWorker{suffix});",
                "            ПодключенныеWorker.Добавить(ПодключениеWorker);",
                "        КонецЕсли;",
                "    Исключение",
                f"        ИсходWorker{suffix} = ?(ФазаWorker{suffix} = \"decode\" "
                f"ИЛИ ФазаWorker{suffix} = \"upload\", \"known_pre_swap\", "
                '"registration_outcome_unknown");',
                f"        СиротскийАдресWorker{suffix} = ?("
                f"АдресАртефактаWorker{suffix} = Неопределено, Ложь, "
                f"АдресАртефактаWorker{suffix});",
                "        ОтказWorker = Новый Структура("
                '"item_index,phase,outcome,diagnostic,orphan_url", '
                f"{item_index}, ФазаWorker{suffix}, ИсходWorker{suffix}, "
                "Лев(ОписаниеОшибки(), 4096), "
                f"СиротскийАдресWorker{suffix});",
                "    КонецПопытки;",
                "КонецЕсли;",
            )
        )

    lines.extend(
        (
            'СтатусWorker = ?(ОтказWorker = Неопределено, "succeeded", "failed");',
            "ДокументWorker = Новый Структура("
            '"schema,schema_version,transaction_id,batch_index,batch_count,'
            'batch_digest,status,connected,failure", '
            f'{bsl_string_literal(WORKER_STAGE_SCHEMA)}, {WORKER_STAGE_SCHEMA_VERSION}, '
            f'{bsl_string_literal(str(transaction_id))}, {batch.batch_index}, '
            f'{batch_count}, {bsl_string_literal(digest)}, СтатусWorker, '
            "ПодключенныеWorker, ?(ОтказWorker = Неопределено, Ложь, ОтказWorker));",
            "ЗаписьWorker = Новый ЗаписьJSON;",
            "ЗаписьWorker.УстановитьСтроку("
            "Новый ПараметрыЗаписиJSON(ПереносСтрокJSON.Нет));",
            "ЗаписатьJSON(ЗаписьWorker, ДокументWorker);",
            "Результат = ЗаписьWorker.Закрыть();",
        )
    )
    return "\n".join(lines)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("Worker stage outcome is invalid")
        result[key] = value
    return result


def _invalid_constant(_value: str) -> object:
    raise ProtocolError("Worker stage outcome is invalid")


def _exact_keys(value: object, expected: set[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != expected:
        raise ProtocolError("Worker stage outcome is invalid")
    return value


def parse_worker_stage_batch_outcome(
    payload: str,
    batch: WorkerStageBatch,
    *,
    batch_count: int,
    transaction_id: UUID,
) -> WorkerStageBatchOutcome:
    """Parse only the current JSON outcome correlated to one sealed batch."""
    if (
        type(batch) is not WorkerStageBatch
        or not _batch_count_is_valid(batch, batch_count)
        or type(transaction_id) is not UUID
    ):
        raise ValueError("worker stage batch coordinates are invalid")
    if type(payload) is not str:
        raise ProtocolError("Worker stage outcome is invalid")
    try:
        encoded = payload.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ProtocolError("Worker stage outcome is invalid") from error
    if len(encoded) > MAX_WORKER_STAGE_OUTCOME_BYTES:
        raise ProtocolError("Worker stage outcome is invalid")
    try:
        document = json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
        )
    except (json.JSONDecodeError, ProtocolError, RecursionError) as error:
        raise ProtocolError("Worker stage outcome is invalid") from error
    document = _exact_keys(
        document,
        {
            "schema",
            "schema_version",
            "transaction_id",
            "batch_index",
            "batch_count",
            "batch_digest",
            "status",
            "connected",
            "failure",
        },
    )
    digest = worker_stage_batch_digest(batch, batch_count)
    if (
        document["schema"] != WORKER_STAGE_SCHEMA
        or type(document["schema_version"]) is not int
        or document["schema_version"] != WORKER_STAGE_SCHEMA_VERSION
        or document["transaction_id"] != str(transaction_id)
        or type(document["batch_index"]) is not int
        or document["batch_index"] != batch.batch_index
        or type(document["batch_count"]) is not int
        or document["batch_count"] != batch_count
        or document["batch_digest"] != digest
        or document["status"] not in {"succeeded", "failed"}
        or type(document["connected"]) is not list
    ):
        raise ProtocolError("Worker stage outcome is invalid")

    connected: list[WorkerStageRegistrationReceipt] = []
    for index, raw in enumerate(document["connected"]):
        if index >= len(batch.entries):
            raise ProtocolError("Worker stage outcome is invalid")
        row = _exact_keys(
            raw,
            {"registration_name", "artifact_sha256", "temp_storage_url"},
        )
        entry = batch.entries[index]
        if (
            row["registration_name"] != entry.registration_name
            or row["artifact_sha256"] != entry.artifact_sha256
        ):
            raise ProtocolError("Worker stage outcome is invalid")
        try:
            connected.append(
                WorkerStageRegistrationReceipt(
                    entry.registration_name,
                    entry.artifact_sha256,
                    row["temp_storage_url"],  # type: ignore[arg-type]
                )
            )
        except ValueError as error:
            raise ProtocolError("Worker stage outcome is invalid") from error
    if len({item.temp_storage_url for item in connected}) != len(connected):
        raise ProtocolError("Worker stage outcome is invalid")

    status = document["status"]
    failure: WorkerStageFailure | None
    if status == "succeeded":
        if document["failure"] is not False or len(connected) != len(batch.entries):
            raise ProtocolError("Worker stage outcome is invalid")
        failure = None
    else:
        raw_failure = _exact_keys(
            document["failure"],
            {"item_index", "phase", "outcome", "diagnostic", "orphan_url"},
        )
        if (
            type(raw_failure["item_index"]) is not int
            or raw_failure["item_index"] != len(connected)
            or raw_failure["item_index"] >= len(batch.entries)
        ):
            raise ProtocolError("Worker stage outcome is invalid")
        try:
            orphan_url = raw_failure["orphan_url"]
            if orphan_url is False:
                orphan_url = None
            failure = WorkerStageFailure(
                raw_failure["item_index"],
                raw_failure["phase"],  # type: ignore[arg-type]
                raw_failure["outcome"],  # type: ignore[arg-type]
                raw_failure["diagnostic"],  # type: ignore[arg-type]
                orphan_url,  # type: ignore[arg-type]
            )
        except ValueError as error:
            raise ProtocolError("Worker stage outcome is invalid") from error

    return WorkerStageBatchOutcome(
        transaction_id,
        batch.batch_index,
        batch_count,
        digest,
        status,  # type: ignore[arg-type]
        tuple(connected),
        failure,
    )
