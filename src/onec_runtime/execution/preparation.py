"""Shared lowering mechanics for controller-selected notebook cell routes.

The route preparer owns the profile, message key and Worker catalog choice.
This module only adapts the existing lowerer and pin prelude contracts while
the controller route context is being migrated.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass

from onec_runtime.bsl import (
    LoweringMode,
    LoweringProfile,
    SemanticLoweringResult,
    SemanticNotebookLowerer,
    WorkerExport,
)
from onec_runtime.bsl.source_maps import MappedSource
from onec_runtime.worker_universe import OperationGenerationPin


@dataclass(frozen=True, slots=True)
class RoutePreparationInput:
    statement: MappedSource
    lowerer: object
    candidate_catalog: tuple[WorkerExport, ...] | None
    operation_pin: OperationGenerationPin | None
    message_key_factory: Callable[[LoweringMode], str] | None
    complete_catalog: Callable[[tuple[WorkerExport, ...]], tuple[WorkerExport, ...]]
    pinned_catalog: Callable[[OperationGenerationPin], tuple[WorkerExport, ...]]
    temporary_catalog: Callable[
        [object, tuple[WorkerExport, ...] | None], AbstractContextManager[None]
    ]
    with_pin_prelude: Callable[
        [SemanticLoweringResult, OperationGenerationPin | None, LoweringMode],
        SemanticLoweringResult,
    ]


@dataclass(frozen=True, slots=True)
class RoutePreparedStatement:
    lowering: SemanticLoweringResult
    message_collector_key: str


def prepare_statement(
    request: RoutePreparationInput,
    *,
    mode: LoweringMode,
    profile: LoweringProfile,
) -> RoutePreparedStatement:
    """Lower one mapped statement with the route's immutable policy."""
    key_factory = request.message_key_factory
    message_key = key_factory(mode) if key_factory is not None else ""
    catalog = (
        request.complete_catalog(request.candidate_catalog)
        if request.candidate_catalog is not None
        else (
            request.pinned_catalog(request.operation_pin)
            if request.operation_pin is not None
            else None
        )
    )
    lower_mapped = getattr(request.lowerer, "lower_mapped", None)
    if callable(lower_mapped):
        # The production lowerer accepts immutable profiles. Keep the legacy
        # mode argument only for injected lowerers without that contract.
        route_argument = (
            {"profile": profile}
            if isinstance(request.lowerer, SemanticNotebookLowerer)
            else {"mode": mode}
        )
        lowering = lower_mapped(
            request.statement,
            **route_argument,
            message_collector_key=message_key or "__onec_cell_messages",
            worker_exports=catalog,
        )
    else:
        with request.temporary_catalog(request.lowerer, catalog):
            lowering = request.lowerer.lower(
                request.statement.text,
                mode=mode,
                message_collector_key=message_key or "__onec_cell_messages",
            )
    lowering = request.with_pin_prelude(
        lowering,
        None if request.candidate_catalog is not None else request.operation_pin,
        mode,
    )
    return RoutePreparedStatement(lowering, message_key)
