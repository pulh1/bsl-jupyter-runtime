"""MAIN cell policy at the generic preparation and settlement boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol, cast

from onec_runtime.bsl.diagnostics import DiagnosticStage, VisibleSourceContext
from onec_runtime.bsl.lexer import BslLexError
from onec_runtime.bsl.parser_target import BslParseError
from onec_runtime.bsl.semantic_lowering import SemanticLoweringError
from onec_runtime.execution.contracts import (
    CommonCell,
    OperationSourceMapBundle,
    PreparationContext,
    PreparationSnapshots,
    PreparedCell,
    SourceDiagnostic,
)
from onec_runtime.execution.main.preparation import MainCellPreparer
from onec_runtime.execution.preparation import (
    RoutePreparationInput, RoutePreparedStatement, WorkerCandidateIntent,
)
from onec_runtime.execution.worker_activation import PrebuiltWorkerIntent


class MainPreparationBinding(Protocol):
    """Build a local request from immutable owner snapshots.

    The binding supplies an isolated lowerer and Worker catalog view. It must
    not publish a Worker generation or mutate the debugger target.
    """

    def bind(
        self, common: CommonCell, snapshots: PreparationSnapshots
    ) -> RoutePreparationInput: ...

    def worker_intent(
        self, common: CommonCell, snapshots: PreparationSnapshots
    ) -> WorkerCandidateIntent | None: ...


@dataclass(frozen=True, slots=True)
class MainPreparedPayload:
    common: CommonCell = field(repr=False)
    statement: RoutePreparedStatement | None = field(repr=False)
    worker_intent: WorkerCandidateIntent | PrebuiltWorkerIntent | None = field(default=None, repr=False)
    deferred_statement: RoutePreparedStatement | None = field(default=None, repr=False)


class MainSettlementPort(Protocol):
    """Own MAIN namespace, Worker and reply publication after ticket outcome."""

    def settle_main(self, outcome: object, payload: MainPreparedPayload) -> object: ...


class MainCellPolicy:
    def __init__(
        self,
        binding: MainPreparationBinding,
        preparer: MainCellPreparer | None = None,
        *,
        prebuild_worker: (
            Callable[[WorkerCandidateIntent], PrebuiltWorkerIntent] | None
        ) = None,
    ) -> None:
        self._binding = binding
        self._preparer = preparer or MainCellPreparer()
        self._prebuild_worker = prebuild_worker

    def prepare(
        self,
        common: CommonCell,
        snapshots: PreparationSnapshots,
        context: PreparationContext,
    ) -> PreparedCell | SourceDiagnostic:
        maps = common.source_maps
        if not isinstance(maps, OperationSourceMapBundle):
            raise TypeError("MAIN requires notebook source maps")
        statement = maps.statement_execution
        worker_intent = (
            self._binding.worker_intent(common, snapshots)
            if maps.worker_candidate is not None else None
        )
        lowered: RoutePreparedStatement | None = None
        if statement is not None:
            request = self._binding.bind(common, snapshots)
            if request.statement is not statement:
                raise ValueError("MAIN preparation request uses another statement")
            try:
                lowered = self._preparer.prepare(request)
            except (BslLexError, BslParseError, SemanticLoweringError) as error:
                stage = (
                    DiagnosticStage.LOWERING
                    if isinstance(error, SemanticLoweringError)
                    else DiagnosticStage.PARSING
                )
                return SourceDiagnostic(
                    "MAIN statement preparation failed",
                    error=error,
                    mapped_source=statement,
                    visible_source_context=VisibleSourceContext(
                        {common.source_unit: common.parsed_units.visible.text}
                    ),
                    stage=stage,
                )
        if worker_intent is not None and self._prebuild_worker is not None:
            worker_intent = self._prebuild_worker(worker_intent)
        return PreparedCell(
            context.route_token,
            context.preparation_nonce,
            MainPreparedPayload(
                common,
                None if worker_intent is not None else lowered,
                worker_intent,
                lowered if worker_intent is not None else None,
            ),
        )

    def settle(self, outcome: object, prepared: PreparedCell, services: object) -> object:
        payload = prepared.payload
        if not isinstance(payload, MainPreparedPayload):
            raise TypeError("MAIN settlement requires MAIN prepared payload")
        return cast(MainSettlementPort, services).settle_main(outcome, payload)
