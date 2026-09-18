from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from onec_runtime.bsl import LoweringMode, SemanticNotebookLowerer, WorkerExport
from onec_runtime.bsl.parser_target import PythonParserTarget
from onec_runtime.errors import ProtocolError
from onec_runtime_jupyter.extension import MACHINE_MIME_TYPE, _display_reply
from onec_runtime.runtime_models import (
    OperationState, RuntimeReply, RuntimeReplyKind,
)
import onec_runtime.server_worker as server_worker


WORKSPACE = Path(__file__).parents[2]


def test_production_worker_builder_binds_exports_from_packaged_parser(
    tmp_path: Path,
) -> None:
    source = (
        WORKSPACE
        / "tests"
        / "fixtures"
        / "onec"
        / "HotReloadWorker"
        / "v1"
        / "Worker"
        / "Ext"
        / "ObjectModule.bsl"
    )
    artifact_path = tmp_path / "Worker-v1.epf"
    artifact_path.write_bytes(b"worker-v1")
    exports = (
        WorkerExport("Расчет.Ндфл.Посчитать", "Посчитать"),
        WorkerExport("Расчет.Ндфл.ИзменитьРезультат", "ИзменитьРезультат"),
        WorkerExport("Расчет.Ндфл.Версия", "Версия"),
    )

    assert hasattr(server_worker, "build_worker_artifact")
    artifact = server_worker.build_worker_artifact(
        logical_name="Worker",
        source_path=source,
        artifact_path=artifact_path,
        expected_version="v1",
        expected_value=13,
        exports=exports,
    )

    assert artifact.source_sha256 == sha256(source.read_bytes()).hexdigest()
    assert artifact.artifact_sha256 == sha256(artifact_path.read_bytes()).hexdigest()
    assert artifact.exports == exports


def test_production_worker_builder_rejects_non_exported_catalog_method(
    tmp_path: Path,
) -> None:
    source = (
        WORKSPACE
        / "tests"
        / "fixtures"
        / "onec"
        / "HotReloadWorker"
        / "v1"
        / "Worker"
        / "Ext"
        / "ObjectModule.bsl"
    )
    artifact_path = tmp_path / "Worker-v1.epf"
    artifact_path.write_bytes(b"worker-v1")

    with pytest.raises(ProtocolError, match="not exported"):
        server_worker.build_worker_artifact(
            logical_name="Worker",
            source_path=source,
            artifact_path=artifact_path,
            expected_version="v1",
            expected_value=13,
            exports=(WorkerExport("Расчет.Ндфл.Удалить", "Удалить"),),
        )


def test_real_lowerer_replaces_and_removes_worker_catalog_entries() -> None:
    lowerer = SemanticNotebookLowerer(PythonParserTarget.from_generated())
    v1 = (WorkerExport("Расчет.Ндфл.Посчитать", "Посчитать"),)
    v2 = (WorkerExport("Расчет.Ндфл.Версия", "Версия"),)

    lowerer.set_worker_exports(v1)
    first = lowerer.lower(
        "ГДФЛ = Расчет.Ндфл.Посчитать();", mode=LoweringMode.MAIN
    )
    lowerer.set_worker_exports(v2)
    second = lowerer.lower(
        "Версия = Расчет.Ндфл.Версия();", mode=LoweringMode.MAIN
    )
    stale = lowerer.lower(
        "ГДФЛ = Расчет.Ндфл.Посчитать();", mode=LoweringMode.MAIN
    )

    assert "e1cRuntimeКонтекст.RuntimeWorker.Посчитать()" in first.source
    assert "e1cRuntimeКонтекст.RuntimeWorker.Версия()" in second.source
    assert "e1cRuntimeКонтекст.RuntimeWorker.Посчитать()" not in stale.source


def test_collector_key_must_match_the_bsl_identifier_lexer_rule() -> None:
    lowerer = SemanticNotebookLowerer(PythonParserTarget.from_generated())

    with pytest.raises(ValueError, match="BSL identifier"):
        lowerer.lower("Значение = 1;", mode=LoweringMode.MAIN, message_collector_key="1bad")


def test_capture_error_reply_preserves_lossless_messages_for_jupyter() -> None:
    reply = RuntimeReply(
        RuntimeReplyKind.CAPTURE_CELL, 7, OperationState.CAPTURED,
        error="BSL execution failed", succeeded=False,
        messages=("first\nline", "", "last"),
    )
    bundle = _display_reply(reply)._repr_mimebundle_()

    assert reply.kind is RuntimeReplyKind.CAPTURE_CELL
    assert reply.succeeded is False
    assert reply.error == "BSL execution failed"
    assert reply.messages == ("first\nline", "", "last")
    assert reply.state is OperationState.CAPTURED
    assert bundle[MACHINE_MIME_TYPE]["messages"] == ["first\nline", "", "last"]
    assert "first\nline\n\nlast" not in bundle["text/plain"]
