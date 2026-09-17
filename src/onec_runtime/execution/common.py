"""Visible BSL cell parsing and source maps shared by execution routes."""

from __future__ import annotations

from onec_runtime.bsl import DiagnosticStage, VisibleSourceContext
from onec_runtime.bsl.lexer import BslLexError
from onec_runtime.bsl.notebook_cells import split_notebook_cell
from onec_runtime.bsl.parser_target import BslParseError, PythonParserTarget
from onec_runtime.bsl.source_maps import (
    SourceUnitRef,
    mapped_visible_source,
)
from onec_runtime.errors import ProtocolError
from onec_runtime.execution.contracts import (
    CommonCell,
    OperationSourceMapBundle,
    SourceDiagnostic,
)


class NotebookCommonParser:
    """Split a visible notebook cell without selecting an execution route."""

    def __init__(self, parser_target: PythonParserTarget | None = None) -> None:
        self._parser_target = parser_target or PythonParserTarget.from_generated()

    def prepare(
        self, source: str, source_unit: SourceUnitRef
    ) -> CommonCell | SourceDiagnostic:
        visible_source = mapped_visible_source(source, source_unit)
        try:
            cell = split_notebook_cell(
                self._parser_target, source, source_unit=source_unit
            )
        except (BslLexError, BslParseError) as error:
            return SourceDiagnostic(
                "BSL parsing failed",
                error=error,
                mapped_source=visible_source,
                visible_source_context=VisibleSourceContext({source_unit: source}),
                stage=DiagnosticStage.PARSING,
            )
        mapped_visible = cell.visible.source_map.map_offset(0)
        if mapped_visible.unit is None:
            raise ProtocolError("Notebook cell has no visible source identity")
        return CommonCell(
            source_unit,
            cell,
            OperationSourceMapBundle(mapped_visible.unit, cell.worker, cell.statements),
            source_unit.source_sha256,
        )
