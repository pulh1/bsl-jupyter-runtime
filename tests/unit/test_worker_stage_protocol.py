from __future__ import annotations

import json
from dataclasses import replace
from uuid import UUID

import pytest

from onec_runtime.errors import ProtocolError
from onec_runtime.worker_stage_protocol import (
    MAX_WORKER_STAGE_BATCH_SIZE,
    MAX_WORKER_STAGE_OUTCOME_BYTES,
    WorkerStageBatch,
    WorkerStageEntry,
    WorkerStageFailure,
    WorkerStageRegistrationReceipt,
    build_worker_stage_batches,
    parse_worker_stage_batch_outcome,
    stage_worker_batch_instruction,
    worker_stage_batch_digest,
)


TRANSACTION = UUID("11111111-2222-3333-4444-555555555555")


def _entry(index: int) -> WorkerStageEntry:
    return WorkerStageEntry(
        logical_name=f"Модуль{index}",
        artifact_sha256=f"{index + 1:064x}",
        registration_name=f"OnecRuntime_{index:08x}_{index:016x}",
        artifact_bytes=f"epf-{index}".encode("ascii"),
    )


def _url(index: int) -> str:
    return (
        f"e1cib/tempstorage/00000000-0000-0000-0000-{index + 1:012d}"
        f"?seanceId=opaque-{index}%2Bvalue"
    )


def _document(
    batch: WorkerStageBatch,
    *,
    batch_count: int = 1,
    connected_count: int | None = None,
    failure: dict[str, object] | None = None,
) -> dict[str, object]:
    count = len(batch.entries) if connected_count is None else connected_count
    rendered_failure = (
        False
        if failure is None
        else {
            **failure,
            "orphan_url": (
                False if failure.get("orphan_url") is None else failure["orphan_url"]
            ),
        }
    )
    return {
        "schema": "onec-worker-stage-batch-receipt",
        "schema_version": 2,
        "transaction_id": str(TRANSACTION),
        "batch_index": batch.batch_index,
        "batch_count": batch_count,
        "batch_digest": worker_stage_batch_digest(batch, batch_count),
        "status": "succeeded" if failure is None else "failed",
        "connected": [
            {
                "registration_name": entry.registration_name,
                "artifact_sha256": entry.artifact_sha256,
                "temp_storage_url": _url(index),
            }
            for index, entry in enumerate(batch.entries[:count])
        ],
        "failure": rendered_failure,
    }


def _payload(document: dict[str, object]) -> str:
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"))


@pytest.mark.parametrize(
    ("count", "sizes"),
    ((0, ()), (1, (1,)), (10, (10,)), (11, (10, 1)), (21, (10, 10, 1))),
)
def test_batches_are_manifest_ordered_and_limited(
    count: int, sizes: tuple[int, ...]
) -> None:
    batches = build_worker_stage_batches(tuple(_entry(i) for i in range(count)))

    assert tuple(len(batch.entries) for batch in batches) == sizes
    assert tuple(
        entry.logical_name for batch in batches for entry in batch.entries
    ) == tuple(f"Модуль{i}" for i in range(count))


def test_batch_rejects_empty_duplicate_and_oversized_entries() -> None:
    with pytest.raises(ValueError):
        WorkerStageBatch(0, ())
    duplicate = replace(_entry(1), registration_name=_entry(0).registration_name.lower())
    with pytest.raises(ValueError):
        WorkerStageBatch(0, (_entry(0), duplicate))
    with pytest.raises(ValueError):
        WorkerStageBatch(
            0,
            tuple(_entry(index) for index in range(MAX_WORKER_STAGE_BATCH_SIZE + 1)),
        )


def test_instruction_stages_each_entry_and_returns_current_json_identity() -> None:
    batch = WorkerStageBatch(0, (_entry(0), _entry(1)))

    source = stage_worker_batch_instruction(
        batch,
        batch_count=1,
        transaction_id=TRANSACTION,
    )

    assert source.count("ВнешниеОбработки.Подключить") == 2
    assert source.count("ПоместитьВоВременноеХранилище") == 2
    assert source.count("Строка(ПоместитьВоВременноеХранилище") == 2
    assert "Null" not in source
    assert "Новый ЗаписьJSON" in source
    assert '"onec-worker-stage-batch-receipt"' in source
    assert str(TRANSACTION) in source
    assert worker_stage_batch_digest(batch, 1) in source
    assert "onec-worker-stage-batch-receipt-v1" not in source
    assert "onec-worker-stage-batch-v1" not in source
    assert "RuntimeWorkerActiveGeneration" not in source


def test_success_outcome_preserves_exact_ordered_urls() -> None:
    batch = WorkerStageBatch(0, (_entry(0), _entry(1)))
    document = _document(batch)
    document["connected"][1]["temp_storage_url"] += "%2Fкириллица"  # type: ignore[index]

    outcome = parse_worker_stage_batch_outcome(
        _payload(document),
        batch,
        batch_count=1,
        transaction_id=TRANSACTION,
    )

    assert outcome.status == "succeeded"
    assert tuple(item.registration_name for item in outcome.connected) == tuple(
        item.registration_name for item in batch.entries
    )
    assert outcome.connected[1].temp_storage_url.endswith("%2Fкириллица")
    assert outcome.failure is None


def test_old_receipt_is_rejected_without_fallback() -> None:
    batch = WorkerStageBatch(0, (_entry(0),))

    with pytest.raises(ProtocolError):
        parse_worker_stage_batch_outcome(
            "onec-worker-stage-batch-receipt-v1|0|1|1|old",
            batch,
            batch_count=1,
            transaction_id=TRANSACTION,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("schema", "old"),
        ("schema_version", 1),
        ("schema_version", True),
        ("transaction_id", str(UUID(int=2))),
        ("batch_index", 1),
        ("batch_count", 2),
        ("batch_digest", "f" * 64),
        ("status", "unknown"),
    ),
)
def test_outcome_rejects_wrong_envelope_identity(field: str, value: object) -> None:
    batch = WorkerStageBatch(0, (_entry(0),))
    document = _document(batch)
    document[field] = value

    with pytest.raises(ProtocolError):
        parse_worker_stage_batch_outcome(
            _payload(document), batch, batch_count=1, transaction_id=TRANSACTION
        )


def test_outcome_rejects_duplicate_unknown_and_trailing_json() -> None:
    batch = WorkerStageBatch(0, (_entry(0),))
    payload = _payload(_document(batch))
    duplicate = payload.replace(
        '"schema_version":2',
        '"schema_version":2,"schema_version":2',
    )
    unknown = payload.replace('"failure":false', '"failure":false,"extra":1')

    for candidate in (duplicate, unknown, payload + " trailing"):
        with pytest.raises(ProtocolError):
            parse_worker_stage_batch_outcome(
                candidate, batch, batch_count=1, transaction_id=TRANSACTION
            )


def test_outcome_rejects_reordered_missing_duplicate_or_bad_urls() -> None:
    batch = WorkerStageBatch(0, (_entry(0), _entry(1)))
    variants: list[dict[str, object]] = []
    reordered = _document(batch)
    reordered["connected"].reverse()  # type: ignore[union-attr]
    variants.append(reordered)
    missing = _document(batch)
    missing["connected"].pop()  # type: ignore[union-attr]
    variants.append(missing)
    duplicate = _document(batch)
    duplicate["connected"][1]["temp_storage_url"] = _url(0)  # type: ignore[index]
    variants.append(duplicate)
    malformed = _document(batch)
    malformed["connected"][0]["temp_storage_url"] = "not-a-temp-url"  # type: ignore[index]
    variants.append(malformed)

    for document in variants:
        with pytest.raises(ProtocolError):
            parse_worker_stage_batch_outcome(
                _payload(document), batch, batch_count=1, transaction_id=TRANSACTION
            )


@pytest.mark.parametrize(
    ("phase", "outcome", "orphan_url"),
    (
        ("decode", "known_pre_swap", None),
        ("upload", "known_pre_swap", None),
        ("connect", "registration_outcome_unknown", _url(1)),
        ("registration-result", "registration_outcome_unknown", _url(1)),
    ),
)
def test_failed_outcome_accepts_only_exact_connected_prefix(
    phase: str,
    outcome: str,
    orphan_url: str | None,
) -> None:
    batch = WorkerStageBatch(0, (_entry(0), _entry(1)))
    failure = {
        "item_index": 1,
        "phase": phase,
        "outcome": outcome,
        "diagnostic": "private platform diagnostic",
        "orphan_url": orphan_url,
    }

    parsed = parse_worker_stage_batch_outcome(
        _payload(_document(batch, connected_count=1, failure=failure)),
        batch,
        batch_count=1,
        transaction_id=TRANSACTION,
    )

    assert parsed.status == "failed"
    assert len(parsed.connected) == 1
    assert parsed.failure is not None
    assert parsed.failure.item_index == 1
    assert parsed.failure.outcome == outcome


def test_failed_outcome_rejects_prefix_or_phase_outcome_mismatch() -> None:
    batch = WorkerStageBatch(0, (_entry(0), _entry(1)))
    failure = {
        "item_index": 0,
        "phase": "connect",
        "outcome": "known_pre_swap",
        "diagnostic": "private",
        "orphan_url": _url(0),
    }

    with pytest.raises(ProtocolError):
        parse_worker_stage_batch_outcome(
            _payload(_document(batch, connected_count=1, failure=failure)),
            batch,
            batch_count=1,
            transaction_id=TRANSACTION,
        )


def test_private_url_and_diagnostic_are_redacted_from_repr() -> None:
    receipt = WorkerStageRegistrationReceipt(
        _entry(0).registration_name,
        _entry(0).artifact_sha256,
        _url(0),
    )
    failure = WorkerStageFailure(
        0,
        "connect",
        "registration_outcome_unknown",
        "secret diagnostic",
        _url(0),
    )

    assert _url(0) not in repr(receipt)
    assert _url(0) not in repr(failure)
    assert "secret diagnostic" not in repr(failure)


def test_outcome_rejects_oversized_utf8_before_json_decode() -> None:
    batch = WorkerStageBatch(0, (_entry(0),))

    with pytest.raises(ProtocolError):
        parse_worker_stage_batch_outcome(
            "я" * (MAX_WORKER_STAGE_OUTCOME_BYTES // 2 + 1),
            batch,
            batch_count=1,
            transaction_id=TRANSACTION,
        )
