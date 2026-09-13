"""Resolve minimal Worker name projections against a session catalog snapshot."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from onec_runtime.bsl.module_catalog import (
    CommonModuleCatalogSnapshot,
    CommonModuleDescriptor,
)
from onec_runtime.bsl.source_maps import SourceSpan
from onec_runtime.bsl.worker_projection_model import (
    BareName,
    BareNameKind,
    ParsedMethodModel,
    ParsedModuleModel,
)
from onec_runtime.errors import ModuleUniverseAdmissionError


MODULE_SCOPE_DEPENDENCY = "module_scope_dependency"
AMBIGUOUS_BINDING = "ambiguous_module_dependency"

_PLATFORM_GLOBALS = frozenset(
    {
        "статуссообщения",
        "символы",
        "кодировкатекста",
        "справочники",
        "документы",
        "журналыдокументов",
        "регистрысведений",
        "регистрынакопления",
        "регистрыбухгалтерии",
        "регистрырасчета",
        "планывидовхарактеристик",
        "планысчетов",
        "планывидоврасчета",
        "планыобмена",
        "бизнеспроцессы",
        "задачи",
        "критерииотбора",
        "последовательности",
        "константы",
        "перечисления",
        "внешниеобработки",
        "внешниеотчеты",
        "обработки",
        "отчеты",
        "метаданные",
        "параметрысеанса",
        "частидаты",
        "обходрезультатазапроса",
        "видсравнениякомпоновкиданных",
        "типгруппыэлементовотборакомпоновкиданных",
        "цветастиля",
    }
)


@dataclass(frozen=True, slots=True)
class ResolvedMethodPlan:
    source: ParsedMethodModel
    local_names: tuple[str, ...]
    implicit_local_names: tuple[str, ...]
    dependencies: tuple[str, ...]
    forbidden_global_writes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResolvedModulePlan:
    source: ParsedModuleModel
    catalog_generation: int
    catalog_identity: tuple[str, str, int, str]
    implicit_local_names: tuple[str, ...]
    forbidden_global_writes: tuple[str, ...]
    methods: tuple[ResolvedMethodPlan, ...]
    dependencies: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ResolvedNames:
    implicit_locals: tuple[str, ...]
    forbidden_writes: tuple[str, ...]
    dependencies: tuple[str, ...]


def worker_model_candidate_names(
    models: Iterable[ParsedModuleModel],
) -> tuple[str, ...]:
    """Return the canonical catalog lookup set for parsed module models."""
    candidates: set[str] = set()
    for model in models:
        if not isinstance(model, ParsedModuleModel):
            raise TypeError("models must contain ParsedModuleModel values")
        candidates.update(item.normalized_name for item in model.module_bare_names)
        candidates.update(
            item.normalized_name
            for method in model.methods
            for item in method.bare_names
        )
    return tuple(sorted(candidates))


def resolve_worker_dependencies(
    model: ParsedModuleModel,
    catalog: CommonModuleCatalogSnapshot,
    *,
    previous: ResolvedModulePlan | None = None,
) -> ResolvedModulePlan:
    """Resolve one immutable syntax model without revisiting source or tokens."""

    if not isinstance(model, ParsedModuleModel):
        raise TypeError("model must be ParsedModuleModel")
    if not isinstance(catalog, CommonModuleCatalogSnapshot):
        raise TypeError("catalog must be CommonModuleCatalogSnapshot")
    if previous is not None:
        if not isinstance(previous, ResolvedModulePlan):
            raise TypeError("previous must be ResolvedModulePlan")
        if (
            previous.source.source_sha256 != model.source_sha256
            or previous.source.parser_identity != model.parser_identity
        ):
            raise ValueError("previous plan must describe the same source version")
        current_identity = _catalog_identity(catalog)
        if previous.catalog_identity[:2] != current_identity[:2]:
            raise ValueError("catalog profile identity cannot change")
        if previous.catalog_generation > catalog.revision:
            raise ValueError("catalog generation cannot move backwards")
        if (
            previous.catalog_generation == catalog.revision
            and previous.catalog_identity != current_identity
        ):
            raise ValueError("catalog identity changed within one generation")

    method_names: set[str] = set()
    for method in model.methods:
        if method.normalized_name in method_names:
            error = ModuleUniverseAdmissionError(
                "worker module contains an ambiguous method declaration"
            )
            error.code = AMBIGUOUS_BINDING
            error.span = method.declaration_span
            raise error
        method_names.add(method.normalized_name)

    catalog_by_name = {
        descriptor.canonical_name.casefold(): descriptor
        for descriptor in catalog.modules
    }
    catalog_order = tuple(catalog_by_name)
    internal_methods = frozenset(method_names)
    module_hard_locals = frozenset(model.module_variables)
    previous_module_implicit = (
        frozenset(previous.implicit_local_names) if previous is not None else frozenset()
    )
    module_names = _resolve_names(
        model.module_bare_names,
        hard_locals=module_hard_locals,
        frozen_implicit=previous_module_implicit,
        internal_methods=internal_methods,
        catalog_by_name=catalog_by_name,
        catalog_order=catalog_order,
    )
    if module_names.dependencies:
        error = ModuleUniverseAdmissionError(
            "common module dependency is unsupported in module scope"
        )
        error.code = MODULE_SCOPE_DEPENDENCY
        error.span = SourceSpan(0, 0)
        raise error

    previous_methods = (
        {method.source.normalized_name: method for method in previous.methods}
        if previous is not None
        else {}
    )
    methods: list[ResolvedMethodPlan] = []
    all_dependencies: set[str] = set()
    for method in model.methods:
        prior = previous_methods.get(method.normalized_name)
        hard_locals = (
            module_hard_locals
            | frozenset(module_names.implicit_locals)
            | frozenset(method.declared_names)
        )
        resolved = _resolve_names(
            method.bare_names,
            hard_locals=hard_locals,
            frozen_implicit=(
                frozenset(prior.implicit_local_names)
                if prior is not None
                else frozenset()
            ),
            internal_methods=internal_methods,
            catalog_by_name=catalog_by_name,
            catalog_order=catalog_order,
        )
        all_dependencies.update(name.casefold() for name in resolved.dependencies)
        methods.append(
            ResolvedMethodPlan(
                source=method,
                local_names=tuple(
                    sorted(hard_locals | frozenset(resolved.implicit_locals))
                ),
                implicit_local_names=resolved.implicit_locals,
                dependencies=resolved.dependencies,
                forbidden_global_writes=resolved.forbidden_writes,
            )
        )

    return ResolvedModulePlan(
        source=model,
        catalog_generation=catalog.revision,
        catalog_identity=_catalog_identity(catalog),
        implicit_local_names=module_names.implicit_locals,
        forbidden_global_writes=module_names.forbidden_writes,
        methods=tuple(methods),
        dependencies=tuple(
            catalog_by_name[name].canonical_name
            for name in catalog_order
            if name in all_dependencies
        ),
    )


def _catalog_identity(
    catalog: CommonModuleCatalogSnapshot,
) -> tuple[str, str, int, str]:
    return (
        catalog.profile,
        catalog.preprocessor_profile,
        catalog.revision,
        catalog.sha256,
    )


def _resolve_names(
    bare_names: tuple[BareName, ...],
    *,
    hard_locals: frozenset[str],
    frozen_implicit: frozenset[str],
    internal_methods: frozenset[str],
    catalog_by_name: dict[str, CommonModuleDescriptor],
    catalog_order: tuple[str, ...],
) -> _ResolvedNames:
    by_name = {item.normalized_name: item for item in bare_names}
    implicit: set[str] = set(frozen_implicit)
    forbidden: set[str] = set()
    dependencies: set[str] = set()
    shadowed = hard_locals | frozen_implicit | internal_methods | _PLATFORM_GLOBALS

    for normalized, item in by_name.items():
        if normalized in shadowed:
            continue
        descriptor = catalog_by_name.get(normalized)
        if item.kinds & BareNameKind.BARE_WRITE:
            if descriptor is None:
                implicit.add(normalized)
            else:
                forbidden.add(normalized)
            continue
        if item.kinds & BareNameKind.READ and descriptor is not None:
            dependencies.add(normalized)

    return _ResolvedNames(
        implicit_locals=tuple(sorted(implicit)),
        forbidden_writes=tuple(
            catalog_by_name[name].canonical_name
            for name in catalog_order
            if name in forbidden
        ),
        dependencies=tuple(
            catalog_by_name[name].canonical_name
            for name in catalog_order
            if name in dependencies
        ),
    )


__all__ = [
    "AMBIGUOUS_BINDING",
    "MODULE_SCOPE_DEPENDENCY",
    "ResolvedMethodPlan",
    "ResolvedModulePlan",
    "resolve_worker_dependencies",
    "worker_model_candidate_names",
]
