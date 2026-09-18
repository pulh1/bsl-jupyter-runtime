"""Immutable statement-preparation snapshots for route-selected policies.

This local binding has no target or Worker lease. The controller must validate
the guard again at admission, then acquire any required Worker pin before a
remote side effect. Method projections remain deferred until that admission.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field, replace

from onec_runtime.bsl.notebook_method_globals import bind_notebook_method_globals

from onec_runtime.bsl import (
    LoweringMode,
    SemanticLoweringResult,
    SemanticNotebookLowerer,
    WorkerExport,
)
from onec_runtime.bsl.notebook_cells import NotebookCellProjection
from onec_runtime.bsl.notebook_methods import (
    NotebookMethodSet, merge_notebook_methods,
)
from onec_runtime.bsl.parser_target import PythonParserTarget
from onec_runtime.bsl.source_maps import (
    SourceArtifactKind, SourceSpan, SourceTransformBuilder,
)
from onec_runtime.experiment import bsl_string_literal
from onec_runtime.execution.contracts import (
    CommonCell,
    OperationSourceMapBundle,
    PreparationSnapshots,
)
from onec_runtime.execution.preparation import (
    RoutePreparationInput, WorkerCandidateIntent,
)
from onec_runtime.worker_universe import OperationGenerationPin, WorkerGenerationHandle


class RouteSnapshotMismatch(RuntimeError):
    """A local preparation mixed versions or owners before lowering."""


@dataclass(frozen=True, slots=True, repr=False)
class RouteSnapshotGuard:
    owner: object = field(repr=False)
    version: int
    namespace_names: tuple[str, ...] = field(repr=False)
    worker_exports: tuple[WorkerExport, ...] = field(repr=False)
    previous_methods: NotebookMethodSet | None = field(default=None, repr=False)
    active_worker_handle: WorkerGenerationHandle | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True, repr=False)
class RoutePreparationSnapshot:
    """One locally consistent namespace, Worker catalog and method observation."""

    owner: object = field(repr=False)
    version: int
    namespace_names: tuple[str, ...] = field(repr=False)
    worker_exports: tuple[WorkerExport, ...] = field(repr=False)
    previous_methods: NotebookMethodSet | None = field(default=None, repr=False)
    active_worker_handle: WorkerGenerationHandle | None = field(default=None, repr=False)

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
        if self.previous_methods is not None and not isinstance(
            self.previous_methods, NotebookMethodSet
        ):
            raise ValueError("previous notebook methods snapshot is invalid")
        if self.previous_methods is not None and not set(
            self.previous_methods.exports
        ).issubset(self.worker_exports):
            raise ValueError("previous notebook method catalog is inconsistent")
        if self.active_worker_handle is not None and not isinstance(
            self.active_worker_handle, WorkerGenerationHandle
        ):
            raise ValueError("active Worker generation handle is invalid")

    def for_pipeline(self) -> PreparationSnapshots:
        """Keep the generic pipeline's three snapshot fields mutually bound."""

        return PreparationSnapshots(
            self.namespace_names,
            self.worker_exports,
            RouteSnapshotGuard(
                self.owner, self.version, self.namespace_names,
                self.worker_exports, self.previous_methods,
                self.active_worker_handle,
            ),
        )


class SnapshotRouteBinding:
    """Build a fresh lowerer and optional method intent from one snapshot.

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

    def _checked_cell(
        self, common: CommonCell, snapshots: PreparationSnapshots
    ) -> tuple[OperationSourceMapBundle, NotebookCellProjection, RouteSnapshotGuard]:
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
        return maps, cell, guard

    def worker_intent(
        self, common: CommonCell, snapshots: PreparationSnapshots
    ) -> WorkerCandidateIntent | None:
        """Describe a local Worker candidate without building or publishing it."""

        maps, cell, guard = self._checked_cell(common, snapshots)
        projection = maps.worker_candidate
        if projection is None:
            if cell.has_methods:
                raise ValueError("Worker projection is missing")
            return None
        if projection is not cell.worker or not cell.exports:
            raise ValueError("Worker projection and exports do not match")
        catalog = {export.public_path.casefold(): export for export in guard.worker_exports}
        for export in cell.exports:
            catalog[export.public_path.casefold()] = export
        candidate_catalog = SemanticNotebookLowerer.prepare_worker_exports(
            tuple(catalog.values())
        )
        method_set_candidate = merge_notebook_methods(
            guard.previous_methods, cell,
        )
        return WorkerCandidateIntent(
            projection, cell, cell.exports, candidate_catalog,
            guard.namespace_names, guard,
            guard.previous_methods, method_set_candidate,
        )

    def bind(
        self, common: CommonCell, snapshots: PreparationSnapshots
    ) -> RoutePreparationInput:
        maps, _cell, guard = self._checked_cell(common, snapshots)
        statement = maps.statement_execution
        if statement is None:
            raise ValueError("statement binding requires executable statements")
        intent = self.worker_intent(common, snapshots)
        catalog = guard.worker_exports if intent is None else intent.candidate_catalog
        method_set = guard.previous_methods if intent is None else intent.method_set_candidate
        notebook_exports = (
            set() if method_set is None else
            {item.public_path.casefold() for item in method_set.exports}
        )
        catalog = tuple(
            WorkerExport(item.public_path, item.method, receiver_module="Worker")
            if item.receiver_module is None
            and item.public_path.casefold() in notebook_exports
            else item
            for item in catalog
        )
        worker_globals = () if method_set is None else method_set.bound_globals
        worker_messages = False if method_set is None else method_set.intercepts_messages
        if intent is not None:
            _, worker_globals = bind_notebook_method_globals(
                method_set.mapped_source,
                context_names=guard.namespace_names,
                exports=method_set.exports,
            )
        lowerer = SemanticNotebookLowerer(
            self._parser_target,
            context_names=guard.namespace_names,
            worker_exports=catalog,
        )
        return RoutePreparationInput(
            statement=statement,
            lowerer=lowerer,
            candidate_catalog=catalog,
            operation_pin=None,
            message_key_factory=None,
            complete_catalog=_identity_catalog,
            pinned_catalog=_no_pin_catalog,
            temporary_catalog=_no_temporary_catalog,
            with_pin_prelude=lambda lowering, pin, mode: _with_snapshot_worker_prelude(
                lowering, pin, mode, catalog,
                None if intent is not None else guard.active_worker_handle,
                bool(worker_globals or worker_messages),
            ),
            worker_globals=worker_globals,
            worker_messages=worker_messages,
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


def _with_snapshot_worker_prelude(
    lowering: SemanticLoweringResult,
    pin: OperationGenerationPin | None,
    mode: LoweringMode,
    catalog: tuple[WorkerExport, ...],
    handle: WorkerGenerationHandle | None,
    worker_wrapper: bool,
) -> SemanticLoweringResult:
    if pin is not None:
        raise RuntimeError("statement snapshot has no Worker generation pin")
    dependencies = {name.casefold() for name in lowering.worker_dependencies}
    if not worker_wrapper and not any(
        item.receiver_module is not None
        and item.public_path.casefold() in dependencies
        for item in catalog
    ):
        return lowering
    builder = SourceTransformBuilder(lowering.mapped_source)
    prelude = (
        "__OnecPinnedWorkerGeneration = "
        "e1cRuntimeКонтекст.RuntimeWorkerActiveGeneration;\n"
    )
    if handle is not None:
        prelude += (
            "Если __OnecPinnedWorkerGeneration.ManifestSha256 <> "
            f"{bsl_string_literal(handle.manifest_sha256)} Тогда\n"
            '    ВызватьИсключение "Worker generation pin mismatch";\n'
            "КонецЕсли;\n"
        )
    builder.synthetic(
        prelude,
        SourceSpan(0, 0),
        "worker_generation_pin_prelude",
    )
    builder.copy(SourceSpan(0, len(lowering.source)))
    return replace(
        lowering,
        mapped_source=builder.build(
            SourceArtifactKind.EXECUTED_BSL, mode=mode.value,
        ),
    )
