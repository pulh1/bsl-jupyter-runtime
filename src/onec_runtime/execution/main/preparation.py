"""MAIN notebook statement preparation policy."""

from __future__ import annotations

from onec_runtime.bsl import LoweringMode
from onec_runtime.bsl.semantic_lowering import MAIN_LOWERING_PROFILE
from onec_runtime.execution.preparation import (
    RoutePreparationInput,
    RoutePreparedStatement,
    prepare_statement,
)


class MainCellPreparer:
    def prepare(self, request: RoutePreparationInput) -> RoutePreparedStatement:
        return prepare_statement(
            request, mode=LoweringMode.MAIN, profile=MAIN_LOWERING_PROFILE
        )
