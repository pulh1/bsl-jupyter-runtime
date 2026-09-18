"""Shared lowering mechanics for controller-selected notebook cell routes.

The route preparer owns the profile, message key and Worker catalog choice.
This module only adapts the existing lowerer and pin prelude contracts while
the controller route context is being migrated.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace

from onec_runtime.bsl import (
    LoweringMode,
    LoweringProfile,
    SemanticLoweringResult,
    SemanticNotebookLowerer,
    WorkerExport,
)
from onec_runtime.bsl.source_maps import MappedSource
from onec_runtime.bsl.notebook_cells import NotebookCellProjection
from onec_runtime.bsl.notebook_methods import NotebookMethodSet
from onec_runtime.execution.message_collector import with_message_collector
from onec_runtime.execution.worker_globals import with_notebook_worker_globals
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
    worker_globals: tuple[str, ...] = ()
    worker_messages: bool = False


@dataclass(frozen=True, slots=True)
class RoutePreparedStatement:
    lowering: SemanticLoweringResult
    message_collector_key: str


@dataclass(frozen=True, slots=True)
class WorkerCandidateIntent:
    """Local method projection and catalog expected by later Worker activation.

    This is neither a built artifact nor a published Worker generation.
    ``cell`` and ``method_set_candidate`` keep retained method bodies and
    visible source origins. ``guard`` binds them to the snapshot checked again
    at admission.
    """

    projection: MappedSource = field(repr=False)
    cell: NotebookCellProjection = field(repr=False)
    exports: tuple[WorkerExport, ...] = field(repr=False)
    candidate_catalog: tuple[WorkerExport, ...] = field(repr=False)
    namespace_names: tuple[str, ...] = field(repr=False)
    guard: object = field(repr=False)
    previous_methods: NotebookMethodSet | None = field(repr=False)
    method_set_candidate: NotebookMethodSet = field(repr=False)


def prepare_statement(
    request: RoutePreparationInput,
    *,
    mode: LoweringMode,
    profile: LoweringProfile,
) -> RoutePreparedStatement:
    """Lower one mapped statement with the route's immutable policy."""
    key_factory = request.message_key_factory
    message_key = (key_factory(mode) if key_factory is not None else "") or "__onec_cell_messages"
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
            message_collector_key=message_key,
            worker_exports=catalog,
        )
    else:
        with request.temporary_catalog(request.lowerer, catalog):
            lowering = request.lowerer.lower(
                request.statement.text,
                mode=mode,
                message_collector_key=message_key,
            )
    lowering = replace(
        lowering,
        mapped_source=with_message_collector(
            with_notebook_worker_globals(lowering.mapped_source, request.worker_globals),
            max(lowering.messages_intercepted, int(request.worker_messages)),
            message_key,
            worker_messages=request.worker_messages,
        ),
    )
    # The generation check must precede message/global wrappers: both may
    # access the active Worker before the user's lowered statement begins.
    lowering = request.with_pin_prelude(
        lowering,
        None if request.candidate_catalog is not None else request.operation_pin,
        mode,
    )
    return RoutePreparedStatement(lowering, message_key)
