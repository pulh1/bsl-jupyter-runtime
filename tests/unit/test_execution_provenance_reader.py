"""Exact prepared statement provenance before remote admission."""

from dataclasses import replace

import pytest

from onec_runtime.bsl.semantic_lowering import SemanticLoweringResult
from onec_runtime.bsl.source_maps import (
    SourceArtifactKind,
    SourceSpan,
    SourceTransformBuilder,
    SourceUnitKind,
    SourceUnitRef,
    mapped_visible_source,
    source_sha256,
)
from onec_runtime.errors import ProtocolError
from onec_runtime.execution.capture.policy import CapturePreparedPayload
from onec_runtime.execution.contracts import CommonCell, PreparedCell
from onec_runtime.execution.main.policy import MainPreparedPayload
from onec_runtime.execution.preparation import RoutePreparedStatement
from onec_runtime.execution.provenance import PreparedExecutionProvenanceReader


def prepared(*, capture: bool, worker: bool = False, statement: bool = True):
    visible = "Результат = 1;"
    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL, "provenance-cell", 1, source_sha256(visible)
    )
    common = CommonCell(unit, None, None, unit.source_sha256)
    mapped = mapped_visible_source(visible, unit)
    builder = SourceTransformBuilder(mapped)
    builder.synthetic("// prepared\n", SourceSpan(0, 0), "prepared_prefix")
    builder.copy(SourceSpan(0, len(visible)))
    executed = builder.build(SourceArtifactKind.EXECUTED_BSL, mode="capture" if capture else "main")
    lowering = SemanticLoweringResult(executed, (), (), (), (), 0, ())
    lowered = RoutePreparedStatement(lowering, "__messages") if statement else None
    intent = object() if worker else None
    payload = (
        CapturePreparedPayload(
            common, None if worker else lowered, (), intent,
            lowered if worker else None,
        )
        if capture else MainPreparedPayload(
            common, None if worker else lowered, intent,
            lowered if worker else None,
        )
    )
    return PreparedCell(object(), object(), payload), executed, unit


@pytest.mark.parametrize("capture", [False, True])
def test_reader_uses_exact_lowered_statement_and_mode(capture: bool) -> None:
    cell, executed, unit = prepared(capture=capture)
    provenance = PreparedExecutionProvenanceReader()(cell)
    assert provenance.mode == ("capture" if capture else "main")
    assert provenance.visible_source_sha256 == unit.source_sha256
    assert provenance.executed_source_sha256 == executed.artifact.source_sha256
    assert provenance.source_map_sha256 == executed.source_map_sha256
    assert provenance.executed_source_sha256 != unit.source_sha256


def test_reader_uses_deferred_mixed_worker_statement_artifact() -> None:
    cell, executed, _ = prepared(capture=False, worker=True)
    provenance = PreparedExecutionProvenanceReader()(cell)
    assert provenance.executed_source_sha256 == executed.artifact.source_sha256
    assert provenance.worker_generation is None


def test_worker_only_provenance_fails_before_worker_artifact_exists() -> None:
    cell, _, _ = prepared(capture=True, worker=True, statement=False)
    with pytest.raises(ProtocolError, match="Worker artifact"):
        PreparedExecutionProvenanceReader()(cell)


def test_reader_rejects_mismatched_visible_identity() -> None:
    cell, _, _ = prepared(capture=False)
    payload = cell.payload
    payload = replace(
        payload,
        common=CommonCell(
            payload.common.source_unit, None, None, source_sha256("wrong")
        ),
    )
    cell = replace(cell, payload=payload)
    with pytest.raises(ProtocolError, match="visible source"):
        PreparedExecutionProvenanceReader()(cell)
