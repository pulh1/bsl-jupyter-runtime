"""Mode-independent parsing shared by future execution policies."""

from onec_runtime.bsl import DiagnosticStage
from onec_runtime.bsl.lexer import BslLexError
from onec_runtime.bsl.source_maps import SourceUnitKind, SourceUnitRef, source_sha256
from onec_runtime.execution.common import NotebookCommonParser
from onec_runtime.execution.contracts import CommonCell, SourceDiagnostic


def _unit(source: str) -> SourceUnitRef:
    return SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL,
        "common-parser-cell",
        3,
        source_sha256(source),
    )


def test_common_parser_keeps_worker_and_statement_maps_on_visible_cell() -> None:
    """A mixed cell must retain two distinct branches with original positions."""
    source = "Процедура Метод()\nКонецПроцедуры;\nРезультат = 1;"
    unit = _unit(source)

    result = NotebookCommonParser().prepare(source, unit)

    assert isinstance(result, CommonCell)
    assert result.source_unit == unit
    assert result.source_hash == unit.source_sha256
    assert result.parsed_units.has_methods
    assert result.parsed_units.has_statements
    worker = result.source_maps.worker_candidate
    statement = result.source_maps.statement_execution
    assert worker is not None and statement is not None
    assert worker.artifact != statement.artifact
    assert worker.source_map.map_offset(0).unit == unit
    assert statement.source_map.map_offset(0).unit == unit
    assert statement.source_map.map_offset(0).origin_span.start == source.index(
        "Результат"
    )


def test_common_parser_preserves_original_lexical_evidence() -> None:
    """A parse failure must carry the original span into reply normalization."""
    source = "Результат = $;"
    unit = _unit(source)

    result = NotebookCommonParser().prepare(source, unit)

    assert isinstance(result, SourceDiagnostic)
    assert isinstance(result.error, BslLexError)
    assert result.error.span.start == source.index("$")
    assert result.stage is DiagnosticStage.PARSING
    assert result.mapped_source is not None
    assert result.mapped_source.source_map.map_offset(0).unit == unit
    assert result.visible_source_context is not None
    assert result.visible_source_context.line_column(unit, source.index("$")) == (1, 13)
    assert source not in repr(result)
