from __future__ import annotations

from dataclasses import asdict, replace
from base64 import b64encode
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from onec_runtime.errors import ProtocolError
from onec_runtime.artifacts import ArtifactWriter
import onec_runtime_jupyter.extension as jupyter
from onec_runtime.recovery_journal import RecoveryJournal
from onec_runtime.bsl import WorkerExport
from onec_runtime.server_worker import (
    WorkerArtifact,
    _bind_worker_artifact_capability,
    _worker_artifact_capability,
    build_worker_artifact,
    validate_production_worker_artifact,
)


WORKSPACE = Path(__file__).parents[2]


def _replace_worker_private_capability(
    artifact: WorkerArtifact,
    alternate: WorkerArtifact,
    field: str,
) -> WorkerArtifact:
    capability = _worker_artifact_capability(artifact)
    alternate_capability = _worker_artifact_capability(alternate)
    assert capability is not None and alternate_capability is not None
    clone = replace(artifact)
    _bind_worker_artifact_capability(
        clone,
        artifact_path=(
            alternate_capability.artifact_path
            if field == "artifact_path"
            else capability.artifact_path
        ),
        source_path=(
            alternate_capability.source_path
            if field == "source_path"
            else capability.source_path
        ),
        expected_version=capability.expected_version,
        expected_value=capability.expected_value,
        admission=(
            alternate_capability.admission
            if field == "admission"
            else capability.admission
        ),
    )
    return clone


def _byte_values(value: object):
    if isinstance(value, bytes):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _byte_values(item)
    elif isinstance(value, (tuple, list, set, frozenset)):
        for item in value:
            yield from _byte_values(item)


def _private_artifact(tmp_path: Path) -> tuple[WorkerArtifact, tuple[str, ...]]:
    source = tmp_path / "PrivateWorker.bsl"
    source_bytes = (
        'Функция Версия() Экспорт\n// raw-source-marker\nВозврат "v1";\n'
        "КонецФункции;"
    ).encode("utf-8")
    source.write_bytes(source_bytes)
    epf = tmp_path / "PrivateWorker.epf"
    artifact_bytes = b"\x00raw-artifact-marker\xff"
    epf.write_bytes(artifact_bytes)
    artifact = build_worker_artifact(
        logical_name="PrivateWorker",
        source_path=source,
        artifact_path=epf,
        expected_version="v1",
        expected_value=13,
        exports=(WorkerExport("Расчет.Ндфл.Версия", "Версия"),),
    )
    markers = (
        "raw-source-marker",
        "raw-artifact-marker",
        b64encode(source_bytes).decode("ascii"),
        b64encode(artifact_bytes).decode("ascii"),
    )
    return artifact, markers


def test_worker_artifact_repr_and_asdict_redact_admission_payload(
    tmp_path: Path,
) -> None:
    artifact, markers = _private_artifact(tmp_path)

    public_repr = repr(artifact)
    public_dict = asdict(artifact)

    assert list(_byte_values(public_dict)) == []
    serialized_dict = json.dumps(public_dict, ensure_ascii=False, default=str)
    for marker in markers:
        assert marker not in public_repr
        assert marker not in serialized_dict


def test_generic_artifact_and_jupyter_serialization_redact_admission_payload(
    tmp_path: Path,
) -> None:
    artifact, markers = _private_artifact(tmp_path)
    writer = ArtifactWriter(tmp_path / "artifacts", "privacy")

    writer.write_json("worker.json", artifact)
    writer.append_jsonl("workers.jsonl", artifact)
    jupyter_payload = json.dumps(
        jupyter._json_value(artifact),
        ensure_ascii=False,
        default=str,
    )
    evidence = "\n".join(
        (
            (writer.run_dir / "worker.json").read_text(encoding="utf-8"),
            (writer.run_dir / "workers.jsonl").read_text(encoding="utf-8"),
            jupyter_payload,
        )
    )
    for marker in markers:
        assert marker not in evidence


def test_journal_evidence_redacts_worker_admission_payload(tmp_path: Path) -> None:
    artifact, markers = _private_artifact(tmp_path)
    writer = ArtifactWriter(tmp_path / "artifacts", "journal-privacy")
    journal = RecoveryJournal(writer.append_jsonl)

    journal.record("worker-evidence.jsonl", "worker_prepared", artifact=artifact)
    journal.flush()
    evidence = (writer.run_dir / "worker-evidence.jsonl").read_text(encoding="utf-8")

    for marker in markers:
        assert marker not in evidence


def test_admitted_source_bytes_remain_authoritative_after_source_file_changes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Worker.bsl"
    source.write_text(
        "Функция Версия() Экспорт\nВозврат \"v1\";\nКонецФункции;",
        encoding="utf-8",
    )
    epf = tmp_path / "Worker.epf"
    epf.write_bytes(b"worker")
    proven = build_worker_artifact(
        logical_name="Worker",
        source_path=source,
        artifact_path=epf,
        expected_version="v1",
        expected_value=13,
        exports=(WorkerExport("Расчет.Ндфл.Версия", "Версия"),),
    )
    source.write_text(
        "Функция Версия() Экспорт\nВозврат \"changed\";\nКонецФункции;",
        encoding="utf-8",
    )
    assert validate_production_worker_artifact(proven) == (
        WorkerExport("Расчет.Ндфл.Версия", "Версия"),
    )
