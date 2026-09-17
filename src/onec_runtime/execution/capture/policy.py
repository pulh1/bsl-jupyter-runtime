"""CAPTURE cell policy at the generic preparation and settlement boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, cast

from onec_runtime.bsl.diagnostics import DiagnosticStage, VisibleSourceContext
from onec_runtime.bsl.lexer import BslLexError
from onec_runtime.bsl.parser_target import BslParseError
from onec_runtime.bsl.semantic_lowering import SemanticLoweringError
from onec_runtime.execution.capture.preparation import CaptureCellPreparer
from onec_runtime.execution.contracts import (
    CommonCell,
    OperationSourceMapBundle,
    PreparationContext,
    PreparationSnapshots,
    PreparedCell,
    SourceDiagnostic,
)
from onec_runtime.execution.preparation import RoutePreparationInput, RoutePreparedStatement


class CapturePreparationBinding(Protocol):
    """Bind one CAPTURE statement to isolated namespace and Worker snapshots."""

    def bind(
        self, common: CommonCell, snapshots: PreparationSnapshots
    ) -> RoutePreparationInput: ...


@dataclass(frozen=True, slots=True)
class CapturePreparedPayload:
    common: CommonCell = field(repr=False)
    statement: RoutePreparedStatement | None = field(repr=False)
    dirty_roots: tuple[str, ...] = ()


class CaptureSettlementPort(Protocol):
    """Own CAPTURE namespace, messages, Worker binding and result policy."""

    def settle_capture(
        self, outcome: object, payload: CapturePreparedPayload
    ) -> object: ...


class CaptureCellPolicy:
    def __init__(
        self,
        binding: CapturePreparationBinding,
        preparer: CaptureCellPreparer | None = None,
    ) -> None:
        self._binding = binding
        self._preparer = preparer or CaptureCellPreparer()

    def prepare(
        self,
        common: CommonCell,
        snapshots: PreparationSnapshots,
        context: PreparationContext,
    ) -> PreparedCell | SourceDiagnostic:
        maps = common.source_maps
        if not isinstance(maps, OperationSourceMapBundle):
            raise TypeError("CAPTURE requires notebook source maps")
        statement = maps.statement_execution
        lowered: RoutePreparedStatement | None = None
        dirty_roots: tuple[str, ...] = ()
        if statement is not None:
            request = self._binding.bind(common, snapshots)
            if request.statement is not statement:
                raise ValueError("CAPTURE preparation request uses another statement")
            try:
                lowered = self._preparer.prepare(request)
            except (BslLexError, BslParseError, SemanticLoweringError) as error:
                stage = (
                    DiagnosticStage.LOWERING
                    if isinstance(error, SemanticLoweringError)
                    else DiagnosticStage.PARSING
                )
                return SourceDiagnostic(
                    "CAPTURE statement preparation failed",
                    error=error,
                    mapped_source=statement,
                    visible_source_context=VisibleSourceContext(
                        {common.source_unit: common.parsed_units.visible.text}
                    ),
                    stage=stage,
                )
            dirty_roots = lowered.lowering.dirty_roots
        return PreparedCell(
            context.route_token,
            context.preparation_nonce,
            CapturePreparedPayload(common, lowered, dirty_roots),
        )

    def settle(self, outcome: object, prepared: PreparedCell, services: object) -> object:
        payload = prepared.payload
        if not isinstance(payload, CapturePreparedPayload):
            raise TypeError("CAPTURE settlement requires CAPTURE prepared payload")
        return cast(CaptureSettlementPort, services).settle_capture(outcome, payload)
