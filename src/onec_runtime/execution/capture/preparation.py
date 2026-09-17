"""CAPTURE notebook statement preparation policy."""

from __future__ import annotations

from onec_runtime.bsl import LoweringMode
from onec_runtime.bsl.semantic_lowering import CAPTURE_LOWERING_PROFILE
from onec_runtime.execution.preparation import (
    RoutePreparationInput,
    RoutePreparedStatement,
    prepare_statement,
)


class CaptureCellPreparer:
    def prepare(self, request: RoutePreparationInput) -> RoutePreparedStatement:
        return prepare_statement(
            request, mode=LoweringMode.CAPTURE, profile=CAPTURE_LOWERING_PROFILE
        )
