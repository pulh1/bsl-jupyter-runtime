"""Immutable statement-preparation snapshots for route-selected policies.

This local binding has no target or Worker lease. The controller must validate
the guard again at admission, then acquire any required Worker pin before a
remote side effect. Worker method definitions need a separate artifact path.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field

from onec_runtime.bsl import (
    LoweringMode,
    SemanticLoweringResult,
    SemanticNotebookLowerer,
    WorkerExport,
)
from onec_runtime.bsl.notebook_cells import NotebookCellProjection
from onec_runtime.bsl.parser_target import PythonParserTarget
from onec_runtime.execution.contracts import (
    CommonCell,
    OperationSourceMapBundle,
    PreparationSnapshots,
)
from onec_runtime.execution.preparation import RoutePreparationInput
from onec_runtime.worker_universe import OperationGenerationPin


class RouteSnapshotMismatch(RuntimeError):
    """A local preparation mixed versions or owners before lowering."""


@dataclass(frozen=True, slots=True, repr=False)
class RouteSnapshotGuard:
    owner: object = field(repr=False)
    version: int
    namespace_names: tuple[str, ...] = field(repr=False)
    worker_exports: tuple[WorkerExport, ...] = field(repr=False)


@dataclass(frozen=True, slots=True, repr=False)
class RoutePreparationSnapshot:
    """One locally consistent namespace and Worker catalog observation."""

    owner: object = field(repr=False)
    version: int
    namespace_names: tuple[str, ...] = field(repr=False)
    worker_exports: tuple[WorkerExport, ...] = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version < 0:
            raise ValueError("snapshot version must be a nonnegative integer")
        if type(self.namespace_names) is not tuple or any(
            not isinstance(name, str) or not name for name in self.namespace_names
        ):
            raise ValueError("namespace names must be an immutable text tuple")
        if len({name.casefold() for name in self.namespace_names}) != len(
            self.namespace_names
        ):
            raise ValueError("namespace names must be unique")
        if type(self.worker_exports) is not tuple:
            raise ValueError("Worker exports must be an immutable tuple")
        SemanticNotebookLowerer.prepare_worker_exports(self.worker_exports)

    def for_pipeline(self) -> PreparationSnapshots:
        """Keep the generic pipeline's three snapshot fields mutually bound."""

        return PreparationSnapshots(
            self.namespace_names,
            self.worker_exports,
            RouteSnapshotGuard(
                self.owner, self.version, self.namespace_names, self.worker_exports
            ),
        )


class SnapshotRouteBinding:
    """Build a fresh lowerer for a statement-only route preparation.

    ``owner`` is compared by identity. ``version`` is the version selected by
    the route context, not a remote-state read. Admission must compare that
    version with the live owner before any Worker or RDBG side effect.
    """

    def __init__(
        self,
        parser_target: PythonParserTarget,
        *,
        owner: object,
        version: int,
    ) -> None:
        if type(version) is not int or version < 0:
            raise ValueError("binding version must be a nonnegative integer")
        self._parser_target = parser_target
        self._owner = owner
        self._version = version

    def bind(
        self, common: CommonCell, snapshots: PreparationSnapshots
    ) -> RoutePreparationInput:
        guard = snapshots.guards
        if not isinstance(guard, RouteSnapshotGuard):
            raise RouteSnapshotMismatch("route snapshot guard has another type")
        if guard.owner is not self._owner:
            raise RouteSnapshotMismatch("route snapshot owner changed")
        if guard.version != self._version:
            raise RouteSnapshotMismatch("route snapshot version changed")
        names = snapshots.namespace
        exports = snapshots.worker_catalog
        if names != guard.namespace_names or type(names) is not tuple:
            raise RouteSnapshotMismatch("route snapshot namespace changed")
        if exports != guard.worker_exports or type(exports) is not tuple:
            raise RouteSnapshotMismatch("route snapshot Worker catalog changed")
        maps = common.source_maps
        if not isinstance(maps, OperationSourceMapBundle):
            raise TypeError("statement binding requires notebook source maps")
        cell = common.parsed_units
        if not isinstance(cell, NotebookCellProjection):
            raise TypeError("statement binding requires a notebook projection")
        if cell.has_methods:
            raise ValueError("Worker methods require an artifact preparation binding")
        statement = maps.statement_execution
        if statement is None:
            raise ValueError("statement binding requires executable statements")
        lowerer = SemanticNotebookLowerer(
            self._parser_target,
            context_names=names,
            worker_exports=exports,
        )
        return RoutePreparationInput(
            statement=statement,
            lowerer=lowerer,
            candidate_catalog=exports,
            operation_pin=None,
            message_key_factory=None,
            complete_catalog=_identity_catalog,
            pinned_catalog=_no_pin_catalog,
            temporary_catalog=_no_temporary_catalog,
            with_pin_prelude=_no_pin_prelude,
        )


def _identity_catalog(catalog: tuple[WorkerExport, ...]) -> tuple[WorkerExport, ...]:
    return catalog


def _no_pin_catalog(pin: OperationGenerationPin) -> tuple[WorkerExport, ...]:
    del pin
    raise RuntimeError("statement snapshot has no Worker generation pin")


def _no_temporary_catalog(
    lowerer: object, catalog: tuple[WorkerExport, ...] | None
) -> AbstractContextManager[None]:
    del lowerer, catalog
    return nullcontext()


def _no_pin_prelude(
    lowering: SemanticLoweringResult,
    pin: OperationGenerationPin | None,
    mode: LoweringMode,
) -> SemanticLoweringResult:
    del mode
    if pin is not None:
        raise RuntimeError("statement snapshot has no Worker generation pin")
    return lowering
