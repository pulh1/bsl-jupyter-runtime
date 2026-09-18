"""Immutable admission records for server-side common-module lowering."""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Callable, Iterable
from dataclasses import dataclass, fields, is_dataclass, replace
from hashlib import sha256
from hmac import compare_digest, new as hmac_new
import json
import re
from secrets import token_bytes
from threading import RLock
from typing import Any, Literal
from weakref import ReferenceType, WeakKeyDictionary, ref

from onec_runtime.bsl.lexer import BslLexError, tokenize
from onec_runtime.bsl.module_catalog import (
    CommonModuleCatalogSnapshot,
    CommonModuleDescriptor,
    CommonModuleScope,
)
from onec_runtime.bsl.parser_target import (
    BslParseError,
    PythonParserTarget,
    parse_raw_module,
)
from onec_runtime.bsl.platform_globals import WORKER_PLATFORM_GLOBALS
from onec_runtime.bsl.source_maps import (
    MappedSource,
    SourceArtifactKind,
    SourceSpan,
    SourceTransformBuilder,
    SourceUnitKind,
    SourceUnitRef,
    _observe_text_work,
    _trusted_mapped_semantic_summary,
    _validated_mapped_semantic_summary,
)
from onec_runtime.bsl.worker_dependency_resolver import (
    ResolvedMethodPlan,
    ResolvedModulePlan,
)
from onec_runtime.bsl.worker_projection_model import BareNameKind, ParsedModuleModel
from onec_runtime.bsl.worker_reload_source_map import (
    ReloadInsertion,
    ReloadMethodBoundary,
    materialize_worker_reload_source,
)
from onec_runtime.errors import ModuleUniverseAdmissionError
from onec_runtime.performance_profile import PhaseRecorder


_BSL_IDENTIFIER_RE = re.compile(r"[A-Za-zА-Яа-яЁё_][0-9A-Za-zА-Яа-яЁё_]*\Z")

MODULE_SCOPE_DEPENDENCY = "module_scope_dependency"
DYNAMIC_EXECUTE = "dynamic_execute"
AMBIGUOUS_BINDING = "ambiguous_module_dependency"

DEPENDENCY_FIELD_REGION = "worker_dependency_field"
DEPENDENCY_ALIAS_DECLARATION_REGION = "worker_dependency_alias_declaration"
DEPENDENCY_ALIAS_INITIALIZER_REGION = "worker_dependency_alias_initializer"
IMPLICIT_LOCAL_DECLARATION_REGION = "worker_implicit_local_declaration"

_PLATFORM_GLOBALS = WORKER_PLATFORM_GLOBALS


@dataclass(frozen=True, slots=True, repr=False)
class WorkerModuleUnit:
    logical_name: str
    kind: Literal["module", "test-module"]
    revision: int
    mapped_source: MappedSource

    def __post_init__(self) -> None:
        if not _is_bsl_identifier(self.logical_name):
            raise ModuleUniverseAdmissionError("worker module source identity is invalid")
        if self.kind not in ("module", "test-module"):
            raise ModuleUniverseAdmissionError("worker module source identity is invalid")
        if type(self.revision) is not int or self.revision < 0:
            raise ModuleUniverseAdmissionError("worker module source identity is invalid")
        if not isinstance(self.mapped_source, MappedSource):
            raise ModuleUniverseAdmissionError("worker module source identity is invalid")
        unit = _single_visible_unit(self.mapped_source)
        expected_kind = (
            SourceUnitKind.MODULE if self.kind == "module" else SourceUnitKind.TEST_MODULE
        )
        if (
            unit is None
            or unit.kind is not expected_kind
            or unit.unit_id != self.logical_name
            or unit.revision != self.revision
            or unit.source_sha256 != self.mapped_source.artifact.source_sha256
        ):
            raise ModuleUniverseAdmissionError("worker module source identity is invalid")


@dataclass(frozen=True, slots=True)
class DependencyUse:
    method_name: str
    category: Literal["call", "access", "value"]
    span: SourceSpan
    method_declaration: SourceSpan


@dataclass(frozen=True, slots=True)
class ModuleDependencyBinding:
    target_module: str
    export_variable: str
    uses: tuple[DependencyUse, ...]
    sha256: str


@dataclass(frozen=True, slots=True)
class MethodInsertionPoints:
    method_name: str
    method_declaration: SourceSpan
    declarations_end: int
    first_statement_start: int


@dataclass(frozen=True, slots=True)
class WorkerModuleAnalysisContext:
    module_variable_names: frozenset[str]
    method_names: frozenset[str]
    source_identifier_names: frozenset[str]


@dataclass(frozen=True, slots=True)
class WorkerMethodAnalysis:
    method_name: str
    exported: bool
    signature_sha256: str
    method_declaration: SourceSpan
    executable_body: SourceSpan
    declarations_end: int
    first_statement_start: int
    dependency_uses: tuple[tuple[str, DependencyUse], ...]


@dataclass(frozen=True, slots=True)
class _IsolatedWorkerMethodAnalysis:
    method: WorkerMethodAnalysis
    insertion_point: MethodInsertionPoints


@dataclass(frozen=True, slots=True)
class _PreparedIsolatedWorkerMethodAnalysis:
    analyze: Callable[[], _IsolatedWorkerMethodAnalysis | None]


@dataclass(frozen=True, slots=True, repr=False)
class AnalyzedWorkerModule:
    unit: WorkerModuleUnit
    dependencies: tuple[ModuleDependencyBinding, ...]
    exported_methods: tuple[str, ...]
    method_insertion_points: tuple[MethodInsertionPoints, ...]
    context: WorkerModuleAnalysisContext
    methods: tuple[WorkerMethodAnalysis, ...]
    catalog_identity: tuple[str, str, int, str]
    parser_identity: tuple[str, str]


@dataclass(frozen=True, slots=True, repr=False, weakref_slot=True, eq=False)
class LoweredWorkerModule:
    analysis: AnalyzedWorkerModule
    mapped_source: MappedSource
    dependency_bindings_sha256: str
    transform_version: str = "module-universe-v1"


class WorkerSemanticAdmission:
    """Opaque process-local proof for one exact lowered semantic result."""

    __slots__ = ("__lowered", "__identity", "__proof")

    def __init__(
        self,
        lowered: ReferenceType[LoweredWorkerModule],
        identity: _WorkerSemanticIdentity,
        proof: bytes,
        *,
        _authority: object,
    ) -> None:
        if _authority is not _SEMANTIC_ADMISSION_AUTHORITY:
            raise TypeError("Worker semantic admissions are process-private")
        object.__setattr__(self, "_WorkerSemanticAdmission__lowered", lowered)
        object.__setattr__(self, "_WorkerSemanticAdmission__identity", identity)
        object.__setattr__(self, "_WorkerSemanticAdmission__proof", proof)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Worker semantic admissions are immutable")

    def __repr__(self) -> str:
        return "<redacted worker semantic admission>"

    def _contents(
        self,
    ) -> tuple[
        ReferenceType[LoweredWorkerModule],
        _WorkerSemanticIdentity,
        bytes,
    ]:
        return self.__lowered, self.__identity, self.__proof


@dataclass(frozen=True, slots=True)
class _WorkerSemanticIdentity:
    logical_name: str
    raw_source_object_id: int
    raw_text_object_id: int
    raw_source_sha256: str
    raw_text_sha256: str
    lowered_source_object_id: int
    lowered_text_object_id: int
    lowered_source_sha256: str
    lowered_text_sha256: str
    source_map_object_id: int
    lowered_source_map_sha256: str
    lineage_object_id: int
    lineage_sha256: str
    dependency_bindings_sha256: str
    canonical_exported_methods: tuple[str, ...]
    catalog_identity: tuple[str, str, int, str]
    parser_identity: tuple[str, str]
    transform_version: str
    packer_identity: str | None


_SEMANTIC_ADMISSION_AUTHORITY = object()
_SEMANTIC_ADMISSION_PROOF_KEY = token_bytes(32)
_SEMANTIC_ADMISSIONS: WeakKeyDictionary[
    LoweredWorkerModule,
    dict[str | None, WorkerSemanticAdmission],
] = WeakKeyDictionary()
_SEMANTIC_ADMISSIONS_LOCK = RLock()


def dependency_export_name(canonical_name: str) -> str:
    if not _is_bsl_identifier(canonical_name):
        raise ValueError("dependency canonical name is invalid")
    digest = sha256(canonical_name.casefold().encode("utf-8")).hexdigest()[:12]
    return f"__OnecDependency_{digest}"


def analyze_worker_module(
    unit: WorkerModuleUnit,
    catalog: CommonModuleCatalogSnapshot,
    parser_target: PythonParserTarget,
    *,
    profiler: PhaseRecorder | None = None,
) -> AnalyzedWorkerModule:
    if not isinstance(unit, WorkerModuleUnit):
        raise _analysis_error(
            "worker module analysis input is invalid",
            SourceSpan(0, 0),
            AMBIGUOUS_BINDING,
        )
    if not isinstance(catalog, CommonModuleCatalogSnapshot):
        raise _analysis_error(
            "worker module analysis input is invalid",
            SourceSpan(0, 0),
            AMBIGUOUS_BINDING,
        )
    if not isinstance(parser_target, PythonParserTarget):
        raise _analysis_error(
            "worker module analysis input is invalid",
            SourceSpan(0, 0),
            AMBIGUOUS_BINDING,
        )
    parse = lambda: parse_raw_module(unit.mapped_source.text, parser_target)
    root, tokens = (
        parse()
        if profiler is None
        else profiler.measure_parser("full_module_parses", parse)
    )
    analyze = lambda: _analyze_parsed_worker_module(
        unit,
        catalog,
        root,
        tokens,
        parser_identity=(
            parser_target.metadata.parser_identity_sha256,
            parser_target.metadata.parsergen_package_sha256 or "",
        ),
    )
    return (
        analyze()
        if profiler is None
        else profiler.measure("dependency_analysis", analyze)
    )


def analyze_resolved_worker_module(
    unit: WorkerModuleUnit,
    plan: ResolvedModulePlan,
) -> AnalyzedWorkerModule:
    """Convert a resolved minimal projection to the legacy admission shape."""

    if not isinstance(unit, WorkerModuleUnit) or not isinstance(
        plan, ResolvedModulePlan
    ):
        raise _analysis_error(
            "worker module analysis input is invalid",
            SourceSpan(0, 0),
            AMBIGUOUS_BINDING,
        )
    if not _resolved_plan_structure_is_valid(plan):
        raise _analysis_error(
            "worker module analysis input is invalid",
            SourceSpan(0, 0),
            AMBIGUOUS_BINDING,
        )
    if unit.mapped_source.artifact.source_sha256 != plan.source.source_sha256:
        raise _analysis_error(
            "worker module projection does not match its source",
            SourceSpan(0, 0),
            AMBIGUOUS_BINDING,
        )
    if (
        type(plan.catalog_identity) is not tuple
        or len(plan.catalog_identity) != 4
        or any(
            type(value) is not str or not value
            for value in plan.catalog_identity[:2]
        )
        or type(plan.catalog_generation) is not int
        or plan.catalog_generation < 0
        or type(plan.catalog_identity[2]) is not int
        or plan.catalog_identity[2] != plan.catalog_generation
        or not re.fullmatch(r"[0-9a-f]{64}", plan.catalog_identity[3])
    ):
        raise _analysis_error(
            "worker module analysis input is invalid",
            SourceSpan(0, 0),
            AMBIGUOUS_BINDING,
        )

    source = unit.mapped_source.text
    identifier_names = set(plan.source.module_variables)
    method_names = frozenset(method.normalized_name for method in plan.source.methods)
    identifier_names.update(method_names)
    for method in plan.source.methods:
        identifier_names.update(method.declared_names)
        identifier_names.update(item.normalized_name for item in method.bare_names)
    identifier_names.update(
        item.normalized_name for item in plan.source.module_bare_names
    )

    uses_by_dependency: dict[str, list[DependencyUse]] = {
        dependency.casefold(): [] for dependency in plan.dependencies
    }
    insertion_points: list[MethodInsertionPoints] = []
    method_analyses: list[WorkerMethodAnalysis] = []
    exported_methods: list[str] = []
    for method in plan.methods:
        declaration = method.source.declaration_span
        if method.source.exported:
            exported_methods.append(method.source.name)
        insertion_points.append(
            MethodInsertionPoints(
                method.source.name,
                declaration,
                method.source.alias_declaration_offset,
                method.source.alias_initializer_offset,
            )
        )
        dependency_uses: list[tuple[str, DependencyUse]] = []
        for dependency in method.dependencies:
            normalized = dependency.casefold()
            use = DependencyUse(
                method.source.name,
                "value",
                declaration,
                declaration,
            )
            dependency_uses.append((normalized, use))
            try:
                uses_by_dependency[normalized].append(use)
            except KeyError:
                raise _analysis_error(
                    "resolved worker dependency plan is inconsistent",
                    declaration,
                    AMBIGUOUS_BINDING,
                ) from None
        method_analyses.append(
            WorkerMethodAnalysis(
                method.source.name,
                method.source.exported,
                sha256(
                    source[
                        declaration.start : method.source.alias_initializer_offset
                    ].encode("utf-8")
                ).hexdigest(),
                declaration,
                SourceSpan(method.source.alias_initializer_offset, declaration.end),
                method.source.alias_declaration_offset,
                method.source.alias_initializer_offset,
                tuple(dependency_uses),
            )
        )

    bindings: list[ModuleDependencyBinding] = []
    generated_names: set[str] = set()
    for dependency in plan.dependencies:
        normalized = dependency.casefold()
        uses = tuple(uses_by_dependency[normalized])
        if not uses:
            raise _analysis_error(
                "resolved worker dependency plan is inconsistent",
                SourceSpan(0, 0),
                AMBIGUOUS_BINDING,
            )
        binding = _dependency_binding(
            CommonModuleDescriptor(dependency, CommonModuleScope.SERVER),
            uses,
        )
        generated = binding.export_variable.casefold()
        if generated in generated_names or generated in identifier_names:
            raise _analysis_error(
                "generated dependency name collides with a source identifier",
                uses[0].span,
                AMBIGUOUS_BINDING,
            )
        generated_names.add(generated)
        bindings.append(binding)

    parser_identity = plan.source.parser_identity
    if parser_identity is None:
        raise _analysis_error(
            "worker module parser provenance is unavailable",
            SourceSpan(0, 0),
            AMBIGUOUS_BINDING,
        )
    return AnalyzedWorkerModule(
        unit,
        tuple(bindings),
        tuple(exported_methods),
        tuple(insertion_points),
        WorkerModuleAnalysisContext(
            frozenset(plan.source.module_variables),
            method_names,
            frozenset(identifier_names),
        ),
        tuple(method_analyses),
        plan.catalog_identity,
        parser_identity,
    )


def _resolved_plan_structure_is_valid(plan: ResolvedModulePlan) -> bool:
    if (
        not isinstance(plan.source, ParsedModuleModel)
        or type(plan.methods) is not tuple
        or not all(isinstance(method, ResolvedMethodPlan) for method in plan.methods)
        or tuple(method.source for method in plan.methods) != plan.source.methods
        or not _normalized_name_tuple(plan.implicit_local_names)
        or not _casefold_unique_names(plan.forbidden_global_writes)
        or not _casefold_unique_names(plan.dependencies)
        or not _implicit_names_match_writes(
            plan.implicit_local_names,
            plan.source.module_bare_names,
        )
    ):
        return False

    dependencies = {name.casefold() for name in plan.dependencies}
    module_implicit = set(plan.implicit_local_names)
    method_names = {
        method.normalized_name for method in plan.source.methods
    }
    module_hard_locals = (
        set(plan.source.module_variables) | method_names | set(_PLATFORM_GLOBALS)
    )
    module_forbidden = {
        name.casefold() for name in plan.forbidden_global_writes
    }
    if module_implicit & (
        module_forbidden | dependencies | module_hard_locals
    ):
        return False
    resolved_dependencies: set[str] = set()
    for method in plan.methods:
        expected_locals = tuple(
            sorted(
                {
                    *plan.source.module_variables,
                    *plan.implicit_local_names,
                    *method.source.declared_names,
                    *method.implicit_local_names,
                }
            )
        )
        if (
            not _normalized_name_tuple(method.local_names)
            or not _normalized_name_tuple(method.implicit_local_names)
            or method.local_names != expected_locals
            or not _implicit_names_match_writes(
                method.implicit_local_names,
                method.source.bare_names,
            )
            or not _casefold_unique_names(method.forbidden_global_writes)
            or not _casefold_unique_names(method.dependencies)
        ):
            return False
        method_dependencies = {
            dependency.casefold() for dependency in method.dependencies
        }
        method_forbidden = {
            name.casefold() for name in method.forbidden_global_writes
        }
        method_hard_locals = (
            set(plan.source.module_variables)
            | module_implicit
            | set(method.source.declared_names)
            | method_names
            | set(_PLATFORM_GLOBALS)
        )
        if (
            not method_dependencies.issubset(dependencies)
            or method_dependencies & method_forbidden
            or set(method.implicit_local_names)
            & (method_dependencies | method_forbidden | method_hard_locals)
        ):
            return False
        resolved_dependencies.update(method_dependencies)
    return resolved_dependencies == dependencies


def _normalized_name_tuple(names: object) -> bool:
    return (
        type(names) is tuple
        and all(
            type(name) is str and name and name == name.casefold()
            for name in names
        )
        and names == tuple(sorted(set(names)))
    )


def _casefold_unique_names(names: object) -> bool:
    return (
        type(names) is tuple
        and all(type(name) is str and name for name in names)
        and len(names) == len({name.casefold() for name in names})
    )


def _implicit_names_match_writes(
    names: tuple[str, ...],
    bare_names: tuple[Any, ...],
) -> bool:
    candidates = {item.normalized_name: item for item in bare_names}
    return all(
        (candidate := candidates.get(name)) is not None
        and candidate.kinds & BareNameKind.BARE_WRITE
        and _is_bsl_identifier(candidate.name)
        for name in names
    )


def _analyze_parsed_worker_module(
    unit: WorkerModuleUnit,
    catalog: CommonModuleCatalogSnapshot,
    root: Any,
    tokens: tuple[Any, ...],
    *,
    parser_identity: tuple[str, str] = ("", ""),
) -> AnalyzedWorkerModule:
    module_variables = tuple(
        variable
        for declaration in root.Declarations
        for variable in declaration.Variables
    )
    module_names = {variable.Name.casefold() for variable in module_variables}
    methods = tuple(_module_methods(root))
    method_names: set[str] = set()
    for method in methods:
        normalized = method.Declaration.Name.casefold()
        if normalized in method_names:
            raise _analysis_error(
                "worker module contains an ambiguous method declaration",
                _source_span(method.span),
                AMBIGUOUS_BINDING,
            )
        method_names.add(normalized)
    catalog_by_name = {item.canonical_name.casefold(): item for item in catalog.modules}
    for variable in module_variables:
        if variable.Export is not None and variable.Name.casefold() in catalog_by_name:
            raise _analysis_error(
                "exported module variable conflicts with a common module",
                _source_span(variable.span),
                AMBIGUOUS_BINDING,
            )
    identifier_spans: dict[str, SourceSpan] = {}
    for_token_starts: list[int] = []
    for token in tokens:
        if token.type == "ID":
            identifier_spans.setdefault(
                token.text.casefold(), SourceSpan(token.start, token.end)
            )
        elif token.type == "ДЛЯ":
            for_token_starts.append(token.start)
    loop_token_starts = tuple(for_token_starts)
    context = WorkerModuleAnalysisContext(
        frozenset(module_names),
        frozenset(method_names),
        frozenset(identifier_spans),
    )
    uses_by_module: dict[str, list[DependencyUse]] = {}
    insertion_points: list[MethodInsertionPoints] = []
    exported_methods: list[str] = []
    method_analyses: list[WorkerMethodAnalysis] = []

    elements = root.Elements
    while elements.Item is not None:
        item = elements.Item
        if type(item).__name__ != "Method":
            for node in _walk_ast(item):
                kind = type(node).__name__
                if kind == "ExecuteStatement":
                    raise _analysis_error(
                        "dynamic execution is unsupported in worker modules",
                        _source_span(node.span),
                        DYNAMIC_EXECUTE,
                    )
                if kind != "AccessChain":
                    continue
                normalized = node.Root.casefold()
                if (
                    normalized in module_names
                    or normalized in _PLATFORM_GLOBALS
                ):
                    continue
                if normalized in method_names and node.Arguments is not None:
                    continue
                if normalized in catalog_by_name:
                    raise _analysis_error(
                        "common module dependency is unsupported in module scope",
                        _source_span(node.span),
                        MODULE_SCOPE_DEPENDENCY,
                    )
        elements = elements.Rest

    for method in methods:
        declaration = method.Declaration
        if declaration.Export is not None:
            exported_methods.append(declaration.Name)
        method_span = _source_span(method.span)
        first_statement_start = declaration.Body.Code.span.start
        declarations_end = (
            _declaration_end(declaration.Body.LocalDeclarations[-1], tokens)
            if declaration.Body.LocalDeclarations
            else declaration.Body.Code.span.start
        )
        insertion_points.append(
            MethodInsertionPoints(
                declaration.Name,
                method_span,
                declarations_end,
                first_statement_start,
            )
        )
        executable_body = _executable_body(
            declaration.Body.Code,
            loop_token_starts,
        )
        shadowed = set(module_names)
        if declaration.Parameters is not None:
            shadowed |= {item.Name.casefold() for item in declaration.Parameters.Items}
        shadowed |= {
            name.casefold()
            for local in declaration.Body.LocalDeclarations
            for name in local.Names
        }

        accesses: list[Any] = []
        for node in _walk_ast(declaration.Body.Code):
            kind = type(node).__name__
            if kind == "ExecuteStatement":
                raise _analysis_error(
                    "dynamic execution is unsupported in worker modules",
                    _source_span(node.span),
                    DYNAMIC_EXECUTE,
                )
            if kind in {"ForEachStatement", "ForRangeStatement"}:
                shadowed.add(node.Variable.casefold())
            if kind == "SimpleStatement" and node.Value is not None:
                target = node.Target
                if (
                    type(target).__name__ == "AccessChain"
                    and target.Arguments is None
                    and not target.Postfix
                ):
                    shadowed.add(target.Root.casefold())
            if kind == "AccessChain":
                accesses.append(node)

        dependency_uses: list[tuple[str, DependencyUse]] = []
        for node in accesses:
            normalized = node.Root.casefold()
            if normalized in shadowed or normalized in _PLATFORM_GLOBALS:
                continue
            if normalized in method_names and node.Arguments is not None:
                continue
            descriptor = catalog_by_name.get(normalized)
            if descriptor is None:
                continue

            category = _dependency_category(node)
            use = DependencyUse(
                declaration.Name,
                category,
                _source_span(node.span),
                method_span,
            )
            dependency_uses.append((normalized, use))
            uses_by_module.setdefault(normalized, []).append(use)

        method_analyses.append(
            WorkerMethodAnalysis(
                declaration.Name,
                declaration.Export is not None,
                sha256(
                    unit.mapped_source.text[
                        method_span.start : executable_body.start
                    ].encode("utf-8")
                ).hexdigest(),
                method_span,
                executable_body,
                declarations_end,
                first_statement_start,
                tuple(dependency_uses),
            )
        )

    generated_names: set[str] = set()
    for normalized in sorted(uses_by_module):
        generated_name = dependency_export_name(
            catalog_by_name[normalized].canonical_name
        ).casefold()
        if generated_name in generated_names:
            raise _analysis_error(
                "generated dependency names collide",
                uses_by_module[normalized][0].span,
                AMBIGUOUS_BINDING,
            )
        generated_names.add(generated_name)
        if generated_name in context.source_identifier_names:
            raise _analysis_error(
                "generated dependency name collides with a source identifier",
                identifier_spans[generated_name],
                AMBIGUOUS_BINDING,
            )

    bindings = tuple(
        _dependency_binding(catalog_by_name[name], tuple(uses_by_module[name]))
        for name in sorted(uses_by_module)
    )
    return AnalyzedWorkerModule(
        unit,
        bindings,
        tuple(exported_methods),
        tuple(insertion_points),
        context,
        tuple(method_analyses),
        (
            catalog.profile,
            catalog.preprocessor_profile,
            catalog.revision,
            catalog.sha256,
        ),
        parser_identity,
    )


def _prepare_isolated_worker_method(
    unit: WorkerModuleUnit,
    catalog: CommonModuleCatalogSnapshot,
    parser_target: PythonParserTarget,
    method_span: SourceSpan,
    context: WorkerModuleAnalysisContext,
    *,
    profiler: PhaseRecorder | None = None,
) -> _PreparedIsolatedWorkerMethodAnalysis:
    """Parse one complete method and defer its context-dependent analysis."""

    method_source = unit.mapped_source.text[method_span.start : method_span.end]

    def parse() -> tuple[Any, tuple[Any, ...]] | None:
        try:
            return parse_raw_module(method_source, parser_target)
        except BslLexError as error:
            if error.code in {
                "unterminated_string",
                "unterminated_date_literal",
            }:
                try:
                    tokenize(unit.mapped_source.text)
                except BslLexError as canonical_error:
                    raise canonical_error from error
                return None
            absolute_start = method_span.start + error.span.start
            raise BslLexError(
                _remap_isolated_error_message(
                    str(error),
                    error.span.start,
                    absolute_start,
                ),
                span=SourceSpan(
                    absolute_start,
                    method_span.start + error.span.end,
                ),
                code=error.code,
            ) from error
        except BslParseError as error:
            absolute_start = method_span.start + error.span.start
            raise BslParseError(
                _remap_isolated_error_message(
                    str(error),
                    error.span.start,
                    absolute_start,
                ),
                span=SourceSpan(
                    absolute_start,
                    method_span.start + error.span.end,
                ),
                code=error.code,
            ) from error

    parsed = (
        parse()
        if profiler is None
        else profiler.measure_parser("delta_method_parses", parse)
    )
    if parsed is None:
        return _PreparedIsolatedWorkerMethodAnalysis(lambda: None)
    root, tokens = parsed

    def analyze() -> _IsolatedWorkerMethodAnalysis | None:
        elements: list[Any] = []
        cursor = root.Elements
        while cursor.Item is not None:
            elements.append(cursor.Item)
            cursor = cursor.Rest
        if (
            root.Declarations
            or len(elements) != 1
            or type(elements[0]).__name__ != "Method"
        ):
            return None

        method = elements[0]
        declaration = method.Declaration
        local_method_span = _source_span(method.span)
        absolute_method_span = _offset_source_span(local_method_span, method_span.start)
        loop_token_starts = tuple(
            token.start for token in tokens if token.type == "ДЛЯ"
        )
        local_executable_body = _executable_body(
            declaration.Body.Code,
            loop_token_starts,
        )
        executable_body = _offset_source_span(
            local_executable_body,
            method_span.start,
        )
        declarations_end = (
            _declaration_end(declaration.Body.LocalDeclarations[-1], tokens)
            if declaration.Body.LocalDeclarations
            else declaration.Body.Code.span.start
        )
        first_statement_start = declaration.Body.Code.span.start
        shadowed = set(context.module_variable_names)
        if declaration.Parameters is not None:
            shadowed |= {
                item.Name.casefold() for item in declaration.Parameters.Items
            }
        shadowed |= {
            name.casefold()
            for local in declaration.Body.LocalDeclarations
            for name in local.Names
        }

        accesses: list[Any] = []
        for node in _walk_ast(declaration.Body.Code):
            kind = type(node).__name__
            if kind == "ExecuteStatement":
                raise _analysis_error(
                    "dynamic execution is unsupported in worker modules",
                    _offset_source_span(_source_span(node.span), method_span.start),
                    DYNAMIC_EXECUTE,
                )
            if kind in {"ForEachStatement", "ForRangeStatement"}:
                shadowed.add(node.Variable.casefold())
            if kind == "SimpleStatement" and node.Value is not None:
                target = node.Target
                if (
                    type(target).__name__ == "AccessChain"
                    and target.Arguments is None
                    and not target.Postfix
                ):
                    shadowed.add(target.Root.casefold())
            if kind == "AccessChain":
                accesses.append(node)

        catalog_by_name = {
            item.canonical_name.casefold(): item for item in catalog.modules
        }
        dependency_uses: list[tuple[str, DependencyUse]] = []
        for node in accesses:
            normalized = node.Root.casefold()
            if normalized in shadowed or normalized in _PLATFORM_GLOBALS:
                continue
            if normalized in context.method_names and node.Arguments is not None:
                continue
            descriptor = catalog_by_name.get(normalized)
            if descriptor is None:
                continue
            try:
                category = _dependency_category(node)
            except ModuleUniverseAdmissionError as error:
                raise _analysis_error(
                    str(error),
                    _offset_source_span(error.span, method_span.start),
                    error.code,
                ) from error
            use = DependencyUse(
                declaration.Name,
                category,
                _offset_source_span(_source_span(node.span), method_span.start),
                absolute_method_span,
            )
            dependency_uses.append((normalized, use))

        analysis = WorkerMethodAnalysis(
            declaration.Name,
            declaration.Export is not None,
            sha256(
                method_source[
                    local_method_span.start : local_executable_body.start
                ].encode("utf-8")
            ).hexdigest(),
            absolute_method_span,
            executable_body,
            method_span.start + declarations_end,
            method_span.start + first_statement_start,
            tuple(dependency_uses),
        )
        return _IsolatedWorkerMethodAnalysis(
            analysis,
            MethodInsertionPoints(
                declaration.Name,
                absolute_method_span,
                method_span.start + declarations_end,
                method_span.start + first_statement_start,
            ),
        )

    return _PreparedIsolatedWorkerMethodAnalysis(analyze)


@dataclass(frozen=True, slots=True)
class _AliasTransformPlan:
    source: MappedSource
    line_ending: str
    fields: tuple[tuple[str, SourceSpan, str], ...]
    declarations: dict[int, tuple[tuple[str, DependencyUse, str], ...]]
    initializers: dict[int, tuple[tuple[str, DependencyUse, str], ...]]
    reuse_boundaries: tuple[int, ...]


def lower_worker_module(
    analysis: AnalyzedWorkerModule,
    *,
    profiler: PhaseRecorder | None = None,
) -> LoweredWorkerModule:
    if not isinstance(analysis, AnalyzedWorkerModule):
        raise ValueError("worker module lowering input is invalid")

    plan_operation = lambda: _plan_alias_transform(analysis)
    plan = (
        plan_operation()
        if profiler is None
        else profiler.measure("alias_transform", plan_operation)
    )
    compose = lambda: _compose_worker_module_source(plan)
    mapped_source = (
        compose()
        if profiler is None
        else profiler.measure("source_map_composition", compose)
    )
    dependency_bindings_sha256 = sha256(
        json.dumps(
            [binding.sha256 for binding in analysis.dependencies],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    lowered = LoweredWorkerModule(
        analysis,
        mapped_source,
        dependency_bindings_sha256,
    )
    admit = lambda: _mint_worker_semantic_admission(lowered)
    if profiler is None:
        admit()
    else:
        profiler.measure("admission", admit)
    return lowered


def lower_resolved_worker_module(
    unit: WorkerModuleUnit,
    plan: ResolvedModulePlan,
    *,
    profiler: PhaseRecorder | None = None,
) -> LoweredWorkerModule:
    """Lower a resolved projection directly, without parsing or map composition."""

    analyze = lambda: analyze_resolved_worker_module(unit, plan)
    analysis = (
        analyze()
        if profiler is None
        else profiler.measure("resolved_analysis_adapter", analyze)
    )
    plan_insertions = lambda: _resolved_reload_insertions(analysis, plan)
    insertions = (
        plan_insertions()
        if profiler is None
        else profiler.measure("alias_transform", plan_insertions)
    )
    materialize = lambda: materialize_worker_reload_source(
        unit.mapped_source,
        insertions,
        method_boundaries=tuple(
            ReloadMethodBoundary(
                method.source.name,
                method.source.declaration_span.start,
                method.source.alias_initializer_offset,
                method.source.declaration_span.end,
            )
            for method in plan.methods
        ),
    )
    mapped_source = (
        materialize()
        if profiler is None
        else profiler.measure("source_map_composition", materialize)
    )
    lowered = LoweredWorkerModule(
        analysis,
        mapped_source,
        _canonical_dependency_bindings_sha256(analysis.dependencies),
        "module-universe-v2",
    )
    admit = lambda: _mint_worker_semantic_admission(lowered)
    if profiler is None:
        admit()
    else:
        profiler.measure("admission", admit)
    return lowered


def _resolved_reload_insertions(
    analysis: AnalyzedWorkerModule,
    plan: ResolvedModulePlan,
) -> tuple[ReloadInsertion, ...]:
    source = analysis.unit.mapped_source.text
    bindings = {
        binding.target_module.casefold(): binding
        for binding in analysis.dependencies
    }
    insertions: list[ReloadInsertion] = []
    for binding in analysis.dependencies:
        insertions.append(
            ReloadInsertion.synthetic(
                source_offset=0,
                text=f"Перем {binding.export_variable} Экспорт;\n",
                method_name=None,
                synthetic_region=DEPENDENCY_FIELD_REGION,
                anchor=SourceSpan(0, 0),
            )
        )
    module_implicit_names = _implicit_local_spellings(
        plan.implicit_local_names,
        plan.source.module_bare_names,
    )
    if module_implicit_names:
        module_span = SourceSpan(0, len(source))
        insertions.append(
            ReloadInsertion.derived(
                source_offset=0,
                text="".join(f"Перем {name};\n" for name in module_implicit_names),
                method_name=None,
                synthetic_region=IMPLICIT_LOCAL_DECLARATION_REGION,
                origin=module_span,
                anchor=module_span,
            )
        )

    dependent_offsets: list[int] = []
    for method in plan.methods:
        if method.dependencies or method.implicit_local_names:
            dependent_offsets.append(method.source.alias_declaration_offset)
        if method.dependencies:
            dependent_offsets.append(method.source.alias_initializer_offset)
    line_contexts = (
        _reload_line_contexts(source, tuple(dependent_offsets))
        if dependent_offsets
        else {}
    )
    for method in plan.methods:
        if not method.dependencies and not method.implicit_local_names:
            continue
        method_bindings = tuple(
            bindings[dependency.casefold()]
            for dependency in method.dependencies
        )
        declaration_offset = method.source.alias_declaration_offset
        implicit_names = _implicit_local_spellings(
            method.implicit_local_names,
            method.source.bare_names,
        )
        if implicit_names:
            insertions.append(
                ReloadInsertion.derived(
                    source_offset=declaration_offset,
                    text=_render_reload_block(
                        tuple(f"Перем {name};" for name in implicit_names),
                        line_contexts[declaration_offset],
                    ),
                    method_name=method.source.name,
                    synthetic_region=IMPLICIT_LOCAL_DECLARATION_REGION,
                    origin=method.source.declaration_span,
                    anchor=method.source.declaration_span,
                )
            )
        if method_bindings:
            initializer_offset = method.source.alias_initializer_offset
            insertions.append(
                ReloadInsertion.derived(
                    source_offset=declaration_offset,
                    text=_render_reload_block(
                        tuple(
                            f"Перем {binding.target_module};"
                            for binding in method_bindings
                        ),
                        line_contexts[declaration_offset],
                    ),
                    method_name=method.source.name,
                    synthetic_region=DEPENDENCY_ALIAS_DECLARATION_REGION,
                    origin=method.source.declaration_span,
                    anchor=method.source.declaration_span,
                )
            )
            insertions.append(
                ReloadInsertion.derived(
                    source_offset=initializer_offset,
                    text=_render_reload_block(
                        tuple(
                            f"{binding.target_module} = {binding.export_variable};"
                            for binding in method_bindings
                        ),
                        line_contexts[initializer_offset],
                    ),
                    method_name=method.source.name,
                    synthetic_region=DEPENDENCY_ALIAS_INITIALIZER_REGION,
                    origin=method.source.declaration_span,
                    anchor=method.source.declaration_span,
                )
            )
    return tuple(insertions)


def _implicit_local_spellings(
    names: tuple[str, ...],
    bare_names: tuple[Any, ...],
) -> tuple[str, ...]:
    by_name = {item.normalized_name: item.name for item in bare_names}
    return tuple(by_name[name] for name in names)


def _render_reload_block(
    lines: tuple[str, ...],
    context: tuple[bool, str, bool],
) -> str:
    prefix_has_code, indent, next_is_newline = context
    leading = f"\n{indent}" if prefix_has_code else ""
    trailing = "" if next_is_newline else f"\n{indent}"
    return leading + f"\n{indent}".join(lines) + trailing


def _reload_line_contexts(
    source: str,
    offsets: tuple[int, ...],
) -> dict[int, tuple[bool, str, bool]]:
    _observe_text_work("reload_plan_source_scan", len(source))
    result: dict[int, tuple[bool, str, bool]] = {}
    line_start = 0
    previous = 0
    for offset in sorted(set(offsets)):
        newline = max(
            source.rfind("\n", previous, offset),
            source.rfind("\r", previous, offset),
        )
        if newline >= 0:
            line_start = newline + 1
        prefix = source[line_start:offset]
        indent_end = 0
        while indent_end < len(prefix) and prefix[indent_end] in " \t":
            indent_end += 1
        result[offset] = (
            bool(prefix.strip()),
            prefix[:indent_end],
            offset < len(source) and source[offset] in "\r\n",
        )
        previous = offset
    return result


def _mint_worker_semantic_admission(
    lowered: LoweredWorkerModule,
    *,
    packer_identity: str | None = None,
    semantic_identity: _WorkerSemanticIdentity | None = None,
) -> WorkerSemanticAdmission:
    """Mint the one semantic capability shared by full and future delta lowering."""
    if not isinstance(lowered, LoweredWorkerModule):
        raise ValueError("worker semantic admission input is invalid")
    if packer_identity is not None and not _safe_packer_identity(packer_identity):
        raise ValueError("worker semantic admission input is invalid")
    identity = (
        _worker_semantic_admission_identity(
            lowered,
            packer_identity=packer_identity,
        )
        if semantic_identity is None
        else replace(semantic_identity, packer_identity=packer_identity)
    )
    proof = _worker_semantic_admission_proof(identity)
    admission = WorkerSemanticAdmission(
        ref(lowered),
        identity,
        proof,
        _authority=_SEMANTIC_ADMISSION_AUTHORITY,
    )
    with _SEMANTIC_ADMISSIONS_LOCK:
        admissions = _SEMANTIC_ADMISSIONS.setdefault(lowered, {})
        admissions[packer_identity] = admission
    return admission


def worker_semantic_admission(
    lowered: LoweredWorkerModule,
    *,
    packer_identity: str,
) -> WorkerSemanticAdmission:
    """Return the process-private semantic proof minted during lowering."""
    if not isinstance(lowered, LoweredWorkerModule):
        raise ValueError("worker semantic admission is invalid")
    if not _safe_packer_identity(packer_identity):
        raise ValueError("worker semantic admission is invalid")
    with _SEMANTIC_ADMISSIONS_LOCK:
        admissions = _SEMANTIC_ADMISSIONS.get(lowered)
        basis = None if admissions is None else admissions.get(None)
    _validate_worker_semantic_admission(
        basis,
        source=lowered.mapped_source,
        exported_methods=lowered.analysis.exported_methods,
        expected_packer_identity=None,
    )
    with _SEMANTIC_ADMISSIONS_LOCK:
        admissions = _SEMANTIC_ADMISSIONS.get(lowered)
        admission = None if admissions is None else admissions.get(packer_identity)
        if admission is None:
            assert basis is not None
            _, basis_identity, _ = basis._contents()
            admission = _mint_worker_semantic_admission(
                lowered,
                packer_identity=packer_identity,
                semantic_identity=basis_identity,
            )
    assert admission is not None
    return admission


def _validate_worker_semantic_admission(
    admission: WorkerSemanticAdmission | None,
    *,
    source: MappedSource,
    exported_methods: tuple[str, ...],
    expected_packer_identity: str | None,
) -> LoweredWorkerModule:
    """Validate an admission without exposing its semantic manifest."""
    try:
        if (
            not isinstance(admission, WorkerSemanticAdmission)
            or not isinstance(source, MappedSource)
            or (
                expected_packer_identity is not None
                and not _safe_packer_identity(expected_packer_identity)
            )
            or type(exported_methods) is not tuple
            or any(type(item) is not str for item in exported_methods)
        ):
            raise ValueError
        lowered_reference, identity, proof = admission._contents()
        lowered = lowered_reference()
        if (
            lowered is None
            or lowered.mapped_source is not source
            or _canonical_exported_methods(exported_methods)
            != _canonical_exported_methods(lowered.analysis.exported_methods)
            or identity.packer_identity != expected_packer_identity
            or not _worker_semantic_identity_matches(identity, lowered)
            or not compare_digest(
                proof,
                _worker_semantic_admission_proof(identity),
            )
        ):
            raise ValueError
        with _SEMANTIC_ADMISSIONS_LOCK:
            admissions = _SEMANTIC_ADMISSIONS.get(lowered)
            if (
                admissions is None
                or admissions.get(expected_packer_identity) is not admission
            ):
                raise ValueError
        return lowered
    except Exception:
        raise ValueError("worker semantic admission is invalid") from None


def _worker_semantic_admission_proof(identity: _WorkerSemanticIdentity) -> bytes:
    mac = hmac_new(_SEMANTIC_ADMISSION_PROOF_KEY, digestmod="sha256")

    def update(value: object) -> int:
        if value is None:
            mac.update(b"n")
            return 1
        if type(value) is int:
            mac.update(b"i")
            if value < 0:
                raise ValueError("worker semantic admission identity is invalid")
            payload = value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")
            length = len(payload).to_bytes(8, "big", signed=False)
            mac.update(length)
            mac.update(payload)
            return 1 + len(length) + len(payload)
        if type(value) is str:
            mac.update(b"s")
            payload = value.encode("utf-8")
            length = len(payload).to_bytes(8, "big", signed=False)
            mac.update(length)
            mac.update(payload)
            return 1 + len(length) + len(payload)
        if type(value) is tuple:
            mac.update(b"t")
            length = len(value).to_bytes(8, "big", signed=False)
            mac.update(length)
            return 1 + len(length) + sum(update(item) for item in value)
        raise ValueError("worker semantic admission identity is invalid")

    width = 0
    for identity_field in fields(identity):
        width += update(identity_field.name)
        width += update(getattr(identity, identity_field.name))
    _observe_text_work("semantic_admission_proof_bytes", width)
    return mac.digest()


def _worker_semantic_admission_identity(
    lowered: LoweredWorkerModule,
    *,
    packer_identity: str | None,
) -> _WorkerSemanticIdentity:
    analysis = lowered.analysis
    raw = analysis.unit.mapped_source
    mapped = lowered.mapped_source
    summary = _trusted_mapped_semantic_summary(mapped)
    return _WorkerSemanticIdentity(
        analysis.unit.logical_name,
        id(raw),
        id(raw.text),
        raw.artifact.source_sha256,
        raw.artifact.source_sha256,
        id(mapped),
        id(mapped.text),
        mapped.artifact.source_sha256,
        mapped.artifact.source_sha256,
        id(mapped.source_map),
        summary.source_map_sha256,
        id(mapped.lineage),
        summary.lineage_sha256,
        _canonical_dependency_bindings_sha256(analysis.dependencies),
        _canonical_exported_methods(analysis.exported_methods),
        analysis.catalog_identity,
        analysis.parser_identity,
        lowered.transform_version,
        packer_identity,
    )


def _worker_semantic_identity_matches(
    identity: _WorkerSemanticIdentity,
    lowered: LoweredWorkerModule,
) -> bool:
    analysis = lowered.analysis
    raw = analysis.unit.mapped_source
    mapped = lowered.mapped_source
    summary = _validated_mapped_semantic_summary(mapped)
    dependency_bindings_sha256 = _canonical_dependency_bindings_sha256(
        analysis.dependencies
    )
    return (
        identity.logical_name == analysis.unit.logical_name
        and identity.raw_source_object_id == id(raw)
        and identity.raw_text_object_id == id(raw.text)
        and identity.raw_source_sha256 == raw.artifact.source_sha256
        and identity.raw_text_sha256 == raw.artifact.source_sha256
        and identity.lowered_source_object_id == id(mapped)
        and identity.lowered_text_object_id == id(mapped.text)
        and identity.lowered_source_sha256 == mapped.artifact.source_sha256
        and identity.lowered_text_sha256 == mapped.artifact.source_sha256
        and identity.source_map_object_id == id(mapped.source_map)
        and identity.lowered_source_map_sha256 == summary.source_map_sha256
        and identity.lineage_object_id == id(mapped.lineage)
        and identity.lineage_sha256 == summary.lineage_sha256
        and identity.dependency_bindings_sha256
        == dependency_bindings_sha256
        and lowered.dependency_bindings_sha256 == dependency_bindings_sha256
        and identity.canonical_exported_methods
        == _canonical_exported_methods(analysis.exported_methods)
        and identity.catalog_identity == analysis.catalog_identity
        and identity.parser_identity == analysis.parser_identity
        and identity.transform_version == lowered.transform_version
    )


def _canonical_exported_methods(methods: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted(item.casefold() for item in methods))


def _safe_packer_identity(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and all(
            character not in value
            for character in ("\x00", "\r", "\n", "/", "\\", "|")
        )
    )


def _canonical_dependency_bindings_sha256(
    bindings: tuple[ModuleDependencyBinding, ...],
) -> str:
    canonical = tuple(_canonical_dependency_binding_sha256(item) for item in bindings)
    return sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _canonical_dependency_binding_sha256(
    binding: ModuleDependencyBinding,
) -> str:
    payload = json.dumps(
        {
            "target_module": binding.target_module,
            "export_variable": binding.export_variable,
            "uses": [
                {
                    "method_name": use.method_name,
                    "category": use.category,
                    "span": [use.span.start, use.span.end],
                    "method_declaration": [
                        use.method_declaration.start,
                        use.method_declaration.end,
                    ],
                }
                for use in binding.uses
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _plan_alias_transform(analysis: AnalyzedWorkerModule) -> _AliasTransformPlan:
    source = analysis.unit.mapped_source
    line_ending = _preferred_line_ending(source.text)
    insertion_points = {
        item.method_name.casefold(): item
        for item in analysis.method_insertion_points
    }
    conditional_intervals = _conditional_directive_intervals(source.text)
    fields: list[tuple[str, SourceSpan, str]] = []
    declarations: dict[int, list[tuple[str, DependencyUse, str]]] = {}
    initializers: dict[int, list[tuple[str, DependencyUse, str]]] = {}

    for binding in analysis.dependencies:
        first_use = min(binding.uses, key=lambda item: item.span.start)
        fields.append(
            (
                f"Перем {binding.export_variable} Экспорт;{line_ending}",
                first_use.span,
                DEPENDENCY_FIELD_REGION,
            )
        )
        method_names = {
            use.method_name.casefold()
            for use in binding.uses
        }
        for method_name in sorted(method_names):
            point = insertion_points[method_name]
            method_uses = tuple(
                use
                for use in binding.uses
                if use.method_name.casefold() == method_name
            )
            method_first_use = min(method_uses, key=lambda item: item.span.start)
            declaration_offset = _unconditional_method_offset(
                point.declarations_end,
                point.method_declaration,
                conditional_intervals,
            )
            initializer_offset = _unconditional_method_offset(
                point.first_statement_start,
                point.method_declaration,
                conditional_intervals,
            )
            declaration_text = f"Перем {binding.target_module};{line_ending}"
            declarations.setdefault(declaration_offset, []).append(
                (
                    declaration_text,
                    method_first_use,
                    DEPENDENCY_ALIAS_DECLARATION_REGION,
                )
            )
            initializers.setdefault(initializer_offset, []).append(
                (
                    f"{binding.target_module} = {binding.export_variable};{line_ending}",
                    method_first_use,
                    DEPENDENCY_ALIAS_INITIALIZER_REGION,
                )
            )

    return _AliasTransformPlan(
        source,
        line_ending,
        tuple(fields),
        {offset: tuple(items) for offset, items in declarations.items()},
        {offset: tuple(items) for offset, items in initializers.items()},
        tuple(
            sorted(
                {
                    boundary
                    for method in analysis.methods
                    for boundary in (
                        method.method_declaration.start,
                        method.method_declaration.end,
                    )
                }
            )
        ),
    )


def _compose_worker_module_source(plan: _AliasTransformPlan) -> MappedSource:
    source = plan.source
    line_ending = plan.line_ending
    fields = plan.fields
    declarations = plan.declarations
    initializers = plan.initializers

    builder = SourceTransformBuilder(source)
    offsets = sorted(
        {
            0,
            len(source.text),
            *declarations,
            *initializers,
            *plan.reuse_boundaries,
        }
    )
    cursor = 0
    for offset in offsets:
        if cursor < offset:
            builder.copy(SourceSpan(cursor, offset))
        if offset == 0:
            for text, anchor, region in fields:
                builder.synthetic(text, anchor, region)
        declaration_items = declarations.get(offset, ())
        for index, (text, use, region) in enumerate(declaration_items):
            if (
                index == 0
                and offset > 0
                and source.text[offset - 1] not in "\r\n"
            ):
                text = f"{line_ending}{text}"
            builder.derived(
                text,
                use.span,
                region,
                anchor=use.method_declaration,
            )
        for text, use, region in initializers.get(offset, ()):
            builder.derived(
                text,
                use.span,
                region,
                anchor=use.method_declaration,
            )
        cursor = offset
    if cursor < len(source.text):
        builder.copy(SourceSpan(cursor, len(source.text)))

    transformed = builder.build(SourceArtifactKind.WORKER_PROJECTION)
    from onec_runtime.worker_epf import prepare_worker_module_source

    return prepare_worker_module_source(transformed)


def _module_methods(root: Any) -> Iterable[Any]:
    elements = root.Elements
    while elements.Item is not None:
        if type(elements.Item).__name__ == "Method":
            yield elements.Item
        elements = elements.Rest


def _preferred_line_ending(source: str) -> str:
    without_crlf = source.replace("\r\n", "")
    if "\r\n" in source and "\n" not in without_crlf and "\r" not in without_crlf:
        return "\r\n"
    if "\r" in source and "\n" not in source:
        return "\r"
    return "\n"


def _conditional_directive_intervals(source: str) -> tuple[SourceSpan, ...]:
    stack: list[int] = []
    intervals: list[SourceSpan] = []
    line_start = 0
    for line in source.splitlines(keepends=True):
        match = re.match(
            r"[ \t]*#[ \t]*([A-Za-zА-Яа-яЁё]+)",
            line,
        )
        if match is not None:
            keyword = match.group(1).casefold()
            if keyword in {"если", "if"}:
                stack.append(line_start)
            elif keyword in {"конецесли", "endif"} and stack:
                intervals.append(SourceSpan(stack.pop(), line_start + len(line)))
        line_start += len(line)
    intervals.extend(SourceSpan(start, len(source)) for start in stack)
    return tuple(sorted(intervals))


def _unconditional_method_offset(
    offset: int,
    method: SourceSpan,
    conditional_intervals: tuple[SourceSpan, ...],
) -> int:
    containing = tuple(
        interval
        for interval in conditional_intervals
        if method.start < interval.start <= offset < interval.end <= method.end
    )
    return min((interval.start for interval in containing), default=offset)


def _walk_ast(root: Any) -> Iterable[Any]:
    stack = [root]
    while stack:
        node = stack.pop()
        if not is_dataclass(node):
            continue
        yield node
        children: list[Any] = []
        for field in fields(node):
            if field.name == "span":
                continue
            value = getattr(node, field.name)
            if is_dataclass(value):
                children.append(value)
            elif isinstance(value, tuple):
                children.extend(item for item in value if is_dataclass(item))
        stack.extend(reversed(children))


def _source_span(span: Any) -> SourceSpan:
    return SourceSpan(span.start, span.end)


def _offset_source_span(span: SourceSpan, offset: int) -> SourceSpan:
    return SourceSpan(span.start + offset, span.end + offset)


def _remap_isolated_error_message(
    message: str,
    local_start: int,
    absolute_start: int,
) -> str:
    return re.sub(
        rf" at {local_start}(?=;|$)",
        f" at {absolute_start}",
        message,
        count=1,
    )


def _declaration_end(declaration: Any, tokens: tuple[Any, ...]) -> int:
    delimiter = next(
        (
            token
            for token in tokens
            if token.start == declaration.span.end and token.type == ";"
        ),
        None,
    )
    return delimiter.end if delimiter is not None else declaration.span.end


def _executable_body(code: Any, for_token_starts: tuple[int, ...]) -> SourceSpan:
    if code.First is None:
        return SourceSpan(code.span.end, code.span.end)
    start = code.First.span.start
    if type(code.First).__name__ in {"ForEachStatement", "ForRangeStatement"}:
        # The generated loop-node spans omit their leading `Для` token.
        start = for_token_starts[bisect_left(for_token_starts, code.span.start)]
    return SourceSpan(start, code.span.end)


def _dependency_category(node: Any) -> Literal["call", "access", "value"]:
    if node.Arguments is not None:
        raise _analysis_error(
            "common module object cannot be called directly",
            _source_span(node.span),
            AMBIGUOUS_BINDING,
        )
    if not node.Postfix:
        return "value"
    first = node.Postfix[0]
    if type(first).__name__ != "MemberAccess":
        raise _analysis_error(
            "common module dependency access is ambiguous",
            _source_span(node.span),
            AMBIGUOUS_BINDING,
        )
    return "call" if first.Arguments is not None else "access"


def _analysis_error(
    message: str,
    span: SourceSpan,
    code: str,
) -> ModuleUniverseAdmissionError:
    error = ModuleUniverseAdmissionError(message)
    error.span = span
    error.code = code
    return error


def _dependency_binding(
    descriptor: CommonModuleDescriptor,
    uses: tuple[DependencyUse, ...],
) -> ModuleDependencyBinding:
    export_variable = dependency_export_name(descriptor.canonical_name)
    payload = json.dumps(
        {
            "target_module": descriptor.canonical_name,
            "export_variable": export_variable,
            "uses": [
                {
                    "method_name": use.method_name,
                    "category": use.category,
                    "span": [use.span.start, use.span.end],
                    "method_declaration": [
                        use.method_declaration.start,
                        use.method_declaration.end,
                    ],
                }
                for use in uses
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return ModuleDependencyBinding(
        descriptor.canonical_name,
        export_variable,
        uses,
        sha256(payload.encode("utf-8")).hexdigest(),
    )


def _is_bsl_identifier(value: object) -> bool:
    return isinstance(value, str) and _BSL_IDENTIFIER_RE.fullmatch(value) is not None


def _single_visible_unit(mapped_source: MappedSource) -> SourceUnitRef | None:
    units = {
        reference
        for segment in mapped_source.source_map.segments
        for reference in (segment.origin_ref, segment.anchor_ref)
        if isinstance(reference, SourceUnitRef)
    }
    if len(units) != 1:
        return None
    return next(iter(units))
