"""Resolve a local Worker source graph against a lazy common-module catalog.

This step reads only the configured source metadata. It runs before artifact
preparation and before any Worker publication ticket enters the RDBG arbiter.
"""

from __future__ import annotations

from onec_runtime.bsl.full_ast_worker_projection import parse_full_ast_module
from onec_runtime.bsl.module_catalog import (
    CommonModuleCatalogSnapshot, SessionCommonModuleCatalog,
)
from onec_runtime.bsl.module_universe import WorkerModuleUnit
from onec_runtime.bsl.worker_dependency_resolver import worker_model_candidate_names
from onec_runtime.errors import ProtocolError
from onec_runtime.performance_profile import PhaseRecorder


def resolve_worker_module_catalog(
    source: SessionCommonModuleCatalog,
    units: tuple[WorkerModuleUnit, ...],
    *,
    profiler: PhaseRecorder | None = None,
) -> CommonModuleCatalogSnapshot:
    """Return one confirmed snapshot for a complete, proposed Worker graph.

    The caller must include retained as well as newly supplied units so later
    catalog growth can resolve names in sources already active on the target.
    Missing bare names are permissive; every Worker module identity is required.
    """

    if not isinstance(source, SessionCommonModuleCatalog):
        raise TypeError("Worker common-module source catalog is invalid")
    if type(units) is not tuple or any(
        not isinstance(unit, WorkerModuleUnit) for unit in units
    ):
        raise TypeError("Worker module units are invalid")
    if profiler is not None and not isinstance(profiler, PhaseRecorder):
        raise TypeError("Worker lifecycle profiler is invalid")

    models = tuple(
        parse_full_ast_module(unit.mapped_source.text, profiler=profiler)
        for unit in units
    )
    if any(
        model.source_sha256 != unit.mapped_source.artifact.source_sha256
        for model, unit in zip(models, units, strict=True)
    ):
        raise ProtocolError("Worker projected source identity changed")
    candidates = worker_model_candidate_names(models)

    def resolve() -> CommonModuleCatalogSnapshot:
        source.resolve_candidates(candidates)
        return source.ensure_modules(unit.logical_name for unit in units)

    return (
        resolve()
        if profiler is None
        else profiler.measure(
            "catalog_validation", resolve,
            item_count=lambda result: len(result.modules),
        )
    )
