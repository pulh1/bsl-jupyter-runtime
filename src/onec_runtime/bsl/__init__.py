"""Minimal Python lexer/parser target used by the BSL grammar spike."""

from onec_runtime.bsl.diagnostics import (
    DiagnosticCoordinateSpace,
    DiagnosticStage,
    LoweredSourceLocation,
    MappingConfidence,
    NormalizedDiagnostic,
    ParsedPlatformDiagnostic,
    PlatformCoordinateCodec,
    VisibleSourceContext,
    VisibleSourceLocation,
    normalize_source_error,
    parse_platform_diagnostic,
    remap_platform_diagnostic,
)
from onec_runtime.bsl.preprocessor import preprocess_server_source
from onec_runtime.bsl.semantic_lowering import (
    LoweringMode,
    MethodScope,
    ModuleBinding,
    NameBinding,
    SemanticLoweringError,
    SemanticLoweringResult,
    SemanticNotebookLowerer,
    SourceEdit,
    WorkerExport,
)
from onec_runtime.bsl.source_maps import (
    LineIndex,
    MappedSource,
    MappingRelation,
    SourceArtifactKind,
    SourceArtifactRef,
    SourceMap,
    SourceSpan,
    SourceTransformBuilder,
    SourceUnitKind,
    SourceUnitRef,
    mapped_visible_source,
    source_sha256,
)
from onec_runtime.bsl.worker_projection_model import (
    BareName,
    BareNameKind,
    ParsedMethodModel,
    ParsedModuleModel,
)

__all__ = [
    "DiagnosticCoordinateSpace",
    "DiagnosticStage",
    "LoweredSourceLocation",
    "LoweringMode",
    "MappingConfidence",
    "MethodScope",
    "ModuleUniverseAdmissionError",
    "ModuleBinding",
    "NameBinding",
    "NormalizedDiagnostic",
    "ParsedPlatformDiagnostic",
    "PlatformCoordinateCodec",
    "BareName",
    "BareNameKind",
    "ParsedMethodModel",
    "ParsedModuleModel",
    "SemanticLoweringError",
    "SemanticLoweringResult",
    "SemanticNotebookLowerer",
    "SourceEdit",
    "WorkerExport",
    "WorkerModuleUnit",
    "VisibleSourceContext",
    "VisibleSourceLocation",
    "normalize_source_error",
    "parse_platform_diagnostic",
    "remap_platform_diagnostic",
    "preprocess_server_source",
    "CommonModuleCatalogSnapshot",
    "CommonModuleDescriptor",
    "CommonModuleScope",
    "SessionCommonModuleCatalog",
    "LineIndex",
    "MappedSource",
    "MappingRelation",
    "SourceArtifactKind",
    "SourceArtifactRef",
    "SourceMap",
    "SourceSpan",
    "SourceTransformBuilder",
    "SourceUnitKind",
    "SourceUnitRef",
    "mapped_visible_source",
    "source_sha256",
    "MODULE_SCOPE_DEPENDENCY",
    "ResolvedMethodPlan",
    "ResolvedModulePlan",
    "resolve_worker_dependencies",
    "worker_model_candidate_names",
]


def __getattr__(name: str) -> object:
    if name not in {
        "CommonModuleCatalogSnapshot",
        "CommonModuleDescriptor",
        "CommonModuleScope",
        "ModuleUniverseAdmissionError",
        "SessionCommonModuleCatalog",
        "WorkerModuleUnit",
        "MODULE_SCOPE_DEPENDENCY",
        "ResolvedMethodPlan",
        "ResolvedModulePlan",
        "resolve_worker_dependencies",
        "worker_model_candidate_names",
    }:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    if name in {
        "CommonModuleCatalogSnapshot",
        "CommonModuleDescriptor",
        "CommonModuleScope",
        "SessionCommonModuleCatalog",
    }:
        from onec_runtime.bsl import module_catalog

        return getattr(module_catalog, name)
    if name in {
        "MODULE_SCOPE_DEPENDENCY",
        "ResolvedMethodPlan",
        "ResolvedModulePlan",
        "resolve_worker_dependencies",
        "worker_model_candidate_names",
    }:
        from onec_runtime.bsl import worker_dependency_resolver

        return getattr(worker_dependency_resolver, name)
    from onec_runtime.bsl import module_universe

    return getattr(module_universe, name)
