"""Resolve a local Worker source graph against a lazy common-module catalog.

This step reads only the configured source metadata. It runs before artifact
preparation and before any Worker publication ticket enters the RDBG arbiter.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from onec_runtime.bsl.full_ast_worker_projection import (
    ParsedModuleModel, full_ast_parser_identity, parse_full_ast_module,
)
from onec_runtime.bsl.module_catalog import (
    CommonModuleCatalogSnapshot, SessionCommonModuleCatalog,
)
from onec_runtime.bsl.module_universe import WorkerModuleUnit
from onec_runtime.bsl.worker_dependency_resolver import (
    ResolvedModulePlan, resolve_worker_dependencies, worker_model_candidate_names,
)
from onec_runtime.errors import ProtocolError
from onec_runtime.performance_profile import PhaseRecorder


@dataclass(frozen=True, slots=True)
class ConfirmedWorkerModulePreparation:
    """Source, syntax, semantics and artifact of one confirmed generation."""

    unit: WorkerModuleUnit
    model: ParsedModuleModel
    plan: ResolvedModulePlan
    artifact: object


@dataclass(frozen=True, slots=True)
class WorkerCatalogResolution:
    """One catalog and the exact parsed source graph used to resolve it."""

    catalog: CommonModuleCatalogSnapshot
    units: tuple[WorkerModuleUnit, ...]
    models: tuple[ParsedModuleModel, ...]
    plans: tuple[ResolvedModulePlan, ...]
    retained: tuple[ConfirmedWorkerModulePreparation | None, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.catalog, CommonModuleCatalogSnapshot)
            or type(self.units) is not tuple
            or type(self.models) is not tuple
            or type(self.plans) is not tuple
            or type(self.retained) is not tuple
            or len(self.units) != len(self.models)
            or len(self.units) != len(self.plans)
            or len(self.units) != len(self.retained)
            or any(
                not isinstance(unit, WorkerModuleUnit)
                or not isinstance(model, ParsedModuleModel)
                or not isinstance(plan, ResolvedModulePlan)
                or plan.source is not model
                or model.source_sha256 != unit.mapped_source.artifact.source_sha256
                for unit, model, plan in zip(
                    self.units, self.models, self.plans, strict=True,
                )
            )
        ):
            raise ProtocolError("Worker catalog resolution does not match its sources")


def resolve_worker_module_catalog(
    source: CommonModuleCatalogSnapshot | SessionCommonModuleCatalog,
    units: tuple[WorkerModuleUnit, ...],
    *,
    profiler: PhaseRecorder | None = None,
    previous: Mapping[str, ConfirmedWorkerModulePreparation] | None = None,
    validate_catalog: Callable[[CommonModuleCatalogSnapshot], None] | None = None,
) -> WorkerCatalogResolution:
    """Return a catalog and parsed models for a complete proposed Worker graph.

    The caller must include retained as well as newly supplied units so later
    catalog growth can resolve names in sources already active on the target.
    Missing bare names are permissive; every Worker module identity is required.
    """

    if not isinstance(source, (CommonModuleCatalogSnapshot, SessionCommonModuleCatalog)):
        raise TypeError("Worker common-module source catalog is invalid")
    if type(units) is not tuple or any(
        not isinstance(unit, WorkerModuleUnit) for unit in units
    ):
        raise TypeError("Worker module units are invalid")
    if profiler is not None and not isinstance(profiler, PhaseRecorder):
        raise TypeError("Worker lifecycle profiler is invalid")
    if previous is not None and (
        not isinstance(previous, Mapping)
        or any(
            type(name) is not str
            or not isinstance(entry, ConfirmedWorkerModulePreparation)
            for name, entry in previous.items()
        )
    ):
        raise TypeError("Worker confirmed preparation inventory is invalid")
    if validate_catalog is not None and not callable(validate_catalog):
        raise TypeError("Worker catalog validator is invalid")

    retained = tuple(
        None if previous is None else previous.get(unit.logical_name.casefold())
        for unit in units
    )
    parser_identity = full_ast_parser_identity() if previous else None
    models = tuple(
        entry.model
        if entry is not None
        and entry.model.source_sha256 == unit.mapped_source.artifact.source_sha256
        and entry.model.parser_identity == parser_identity
        else parse_full_ast_module(unit.mapped_source.text, profiler=profiler)
        for unit, entry in zip(units, retained, strict=True)
    )
    if any(
        model.source_sha256 != unit.mapped_source.artifact.source_sha256
        for model, unit in zip(models, units, strict=True)
    ):
        raise ProtocolError("Worker projected source identity changed")
    if isinstance(source, CommonModuleCatalogSnapshot):
        catalog = source
    else:
        candidates = worker_model_candidate_names(models)

        def resolve() -> CommonModuleCatalogSnapshot:
            source.resolve_candidates(candidates)
            return source.ensure_modules(unit.logical_name for unit in units)

        catalog = (
            resolve()
            if profiler is None
            else profiler.measure(
                "catalog_validation", resolve,
                item_count=lambda result: len(result.modules),
            )
        )
    if validate_catalog is not None:
        validate_catalog(catalog)
    current_identity = (
        catalog.profile, catalog.preprocessor_profile,
        catalog.revision, catalog.sha256,
    )
    plans: list[ResolvedModulePlan] = []
    for model, entry in zip(models, retained, strict=True):
        old_plan = entry.plan if entry is not None and model is entry.model else None
        if old_plan is not None and old_plan.catalog_identity == current_identity:
            plans.append(old_plan)
            continue
        # Nonmonotonic catalogs are rejected by the lifecycle service. Do not
        # let a previous-plan validation error mask that public rejection.
        if old_plan is not None and (
            old_plan.catalog_identity[:2] != current_identity[:2]
            or old_plan.catalog_generation > catalog.revision
            or (
                old_plan.catalog_generation == catalog.revision
                and old_plan.catalog_identity != current_identity
            )
        ):
            old_plan = None
        analyze = lambda: resolve_worker_dependencies(
            model, catalog, previous=old_plan,
        )
        plans.append(
            analyze() if profiler is None
            else profiler.measure("dependency_analysis", analyze)
        )
    return WorkerCatalogResolution(
        catalog, units, models, tuple(plans), retained,
    )
