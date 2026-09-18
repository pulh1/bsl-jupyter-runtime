"""Session-retained semantic state for incremental Worker module reloads."""

from __future__ import annotations

from dataclasses import dataclass, fields
import re

from onec_runtime.bsl.module_catalog import CommonModuleCatalogSnapshot
from onec_runtime.bsl.module_universe import (
    AMBIGUOUS_BINDING,
    DEPENDENCY_ALIAS_DECLARATION_REGION,
    DEPENDENCY_ALIAS_INITIALIZER_REGION,
    DEPENDENCY_FIELD_REGION,
    AnalyzedWorkerModule,
    DependencyUse,
    LoweredWorkerModule,
    MethodInsertionPoints,
    WorkerMethodAnalysis,
    WorkerModuleAnalysisContext,
    WorkerModuleUnit,
    _AliasTransformPlan,
    _analysis_error,
    _canonical_dependency_bindings_sha256,
    _dependency_binding,
    _mint_worker_semantic_admission,
    _plan_alias_transform,
    _prepare_isolated_worker_method,
    dependency_export_name,
    analyze_worker_module,
    lower_worker_module,
)
from onec_runtime.bsl.lexer import tokenize
from onec_runtime.bsl.parser_target import PythonParserTarget
from onec_runtime.bsl.source_maps import (
    MappedSource,
    MappingRelation,
    SourceArtifactKind,
    SourceMap,
    SourceMapSegment,
    SourceSpan,
    SourceTransformBuilder,
    _DeferredMappedSource,
    _NORMALIZED_WORKER_AUTHORITY,
    _RetainedTextFragment,
    _validate_retained_source,
)
from onec_runtime.performance_profile import PhaseRecorder


_CURRENT_WORKER_TRANSFORM_VERSION = next(
    field.default
    for field in fields(LoweredWorkerModule)
    if field.name == "transform_version"
)


class _SegmentReuseUnsupported(Exception):
    """The retained map cannot prove one unambiguous exact splice."""


class _OriginSegmentIndex:
    """One immutable origin interval index shared by all delta lookups."""

    def __init__(self, source_map: SourceMap) -> None:
        grouped: dict[tuple[object, MappingRelation], list[SourceMapSegment]] = {}
        for segment in source_map.segments:
            if segment.origin_ref is not None and segment.origin is not None:
                grouped.setdefault((segment.origin_ref, segment.relation), []).append(
                    segment
                )
        self._segments: dict[
            tuple[object, MappingRelation], tuple[SourceMapSegment, ...]
        ] = {}
        self._cursor: dict[tuple[object, MappingRelation], int] = {}
        self._last_origin_start: dict[tuple[object, MappingRelation], int] = {}
        self._ambiguous: set[tuple[object, MappingRelation]] = set()
        for key, items in grouped.items():
            ordered = tuple(items)
            self._segments[key] = ordered
            if any(
                current.origin is None
                or following.origin is None
                or current.origin.start > following.origin.start
                or current.origin.end > following.origin.start
                for current, following in zip(ordered, ordered[1:], strict=False)
            ):
                self._ambiguous.add(key)

    def generated_span(
        self,
        origin_ref: object,
        origin: SourceSpan,
        relation: MappingRelation,
    ) -> SourceSpan:
        key = (origin_ref, relation)
        if key in self._ambiguous:
            raise _SegmentReuseUnsupported
        segments = self._segments.get(key, ())
        if origin.start < self._last_origin_start.get(key, -1):
            raise _SegmentReuseUnsupported
        self._last_origin_start[key] = origin.start
        index = self._cursor.get(key, 0)
        while (
            index < len(segments)
            and segments[index].origin is not None
            and segments[index].origin.end <= origin.start
        ):
            index += 1
        self._cursor[key] = index
        if index >= len(segments):
            raise _SegmentReuseUnsupported
        segment = segments[index]
        if (
            segment.origin is None
            or segment.origin.start > origin.start
            or origin.end > segment.origin.end
        ):
            raise _SegmentReuseUnsupported
        if relation is MappingRelation.EXACT:
            return SourceSpan(
                segment.generated.start + origin.start - segment.origin.start,
                segment.generated.start + origin.end - segment.origin.start,
            )
        if segment.origin != origin:
            raise _SegmentReuseUnsupported
        return segment.generated


class _GeneratedSegmentCursor:
    """Resolve monotonically increasing generated offsets in one pass."""

    def __init__(self, source_map: SourceMap) -> None:
        self._segments = tuple(
            segment
            for segment in source_map.segments
            if segment.generated.start < segment.generated.end
        )
        self._index = 0

    def at(self, offset: int) -> SourceMapSegment:
        while (
            self._index < len(self._segments)
            and self._segments[self._index].generated.end <= offset
        ):
            self._index += 1
        if self._index >= len(self._segments):
            raise _SegmentReuseUnsupported
        segment = self._segments[self._index]
        if not segment.generated.start <= offset < segment.generated.end:
            raise _SegmentReuseUnsupported
        return segment


@dataclass(frozen=True, slots=True, repr=False)
class WorkerSemanticSnapshot:
    """One full admitted semantic result eligible to become a session base."""

    analysis: AnalyzedWorkerModule
    lowered: LoweredWorkerModule
    catalog_identity: tuple[str, str, int, str]
    parser_identity: tuple[str, str]
    delta_evidence: WorkerSemanticDeltaEvidence | None = None


@dataclass(frozen=True, slots=True)
class WorkerSemanticDeltaEvidence:
    method_name: str
    old_span: SourceSpan
    new_span: SourceSpan
    length_delta: int


def build_full_worker_semantic_snapshot(
    unit: WorkerModuleUnit,
    catalog: CommonModuleCatalogSnapshot,
    parser_target: PythonParserTarget,
    *,
    profiler: PhaseRecorder | None = None,
) -> WorkerSemanticSnapshot:
    """Build the full semantic result and retain its process-private admission."""

    analysis = analyze_worker_module(
        unit,
        catalog,
        parser_target,
        profiler=profiler,
    )
    lowered = lower_worker_module(analysis, profiler=profiler)
    return WorkerSemanticSnapshot(
        analysis,
        lowered,
        analysis.catalog_identity,
        analysis.parser_identity,
    )


def select_worker_semantic_snapshot_base(
    previous: WorkerSemanticSnapshot | None,
    unit: WorkerModuleUnit,
    catalog: CommonModuleCatalogSnapshot,
    parser_target: PythonParserTarget,
) -> WorkerSemanticSnapshot | None:
    """Return an exact compatible base, or ``None`` for conservative full fallback."""

    try:
        if (
            not isinstance(previous, WorkerSemanticSnapshot)
            or not isinstance(unit, WorkerModuleUnit)
            or not isinstance(catalog, CommonModuleCatalogSnapshot)
            or not isinstance(parser_target, PythonParserTarget)
        ):
            return None
        analysis = previous.analysis
        lowered = previous.lowered
        prior_unit = analysis.unit
        catalog_identity = previous.catalog_identity
        parser_identity = previous.parser_identity
        candidate_parser_identity = (
            parser_target.grammar_sha256,
            parser_target.metadata.parsergen_package_sha256 or "",
        )
        return (
            previous
            if (
                isinstance(analysis, AnalyzedWorkerModule)
                and isinstance(lowered, LoweredWorkerModule)
                and lowered.analysis is analysis
                and previous.catalog_identity == analysis.catalog_identity
                and previous.parser_identity == analysis.parser_identity
                and prior_unit.logical_name.casefold() == unit.logical_name.casefold()
                and prior_unit.kind == unit.kind
                and (
                    unit.revision > prior_unit.revision
                    or (
                        unit.revision == prior_unit.revision
                        and unit.mapped_source.artifact.source_sha256
                        == prior_unit.mapped_source.artifact.source_sha256
                        and unit.mapped_source.source_map_sha256
                        == prior_unit.mapped_source.source_map_sha256
                    )
                )
                and len(catalog_identity) == 4
                and catalog.profile == catalog_identity[0]
                and catalog.preprocessor_profile == catalog_identity[1]
                and catalog.revision >= catalog_identity[2]
                and catalog.sha256 == catalog_identity[3]
                and candidate_parser_identity == parser_identity
                and lowered.transform_version == _CURRENT_WORKER_TRANSFORM_VERSION
            )
            else None
        )
    except (AttributeError, IndexError, TypeError, ValueError):
        return None


def try_build_worker_semantic_delta(
    previous: WorkerSemanticSnapshot,
    unit: WorkerModuleUnit,
    catalog: CommonModuleCatalogSnapshot,
    parser_target: PythonParserTarget,
    *,
    profiler: PhaseRecorder | None = None,
) -> WorkerSemanticSnapshot | None:
    """Build one admitted method-local candidate, or request full fallback."""

    if (
        select_worker_semantic_snapshot_base(
            previous,
            unit,
            catalog,
            parser_target,
        )
        is not previous
    ):
        return None
    old_source = previous.analysis.unit.mapped_source.text
    new_source = unit.mapped_source.text
    hunk = _single_unambiguous_hunk(old_source, new_source)
    if hunk is None:
        return None
    old_hunk, new_hunk = hunk
    containing = tuple(
        (index, method)
        for index, method in enumerate(previous.analysis.methods)
        if _span_contains_edit(method.executable_body, old_hunk)
    )
    if len(containing) != 1:
        return None
    method_index, old_method = containing[0]
    if _edit_touches_directive(old_source, old_hunk) or _edit_touches_directive(
        new_source,
        new_hunk,
    ):
        return None

    length_delta = len(new_source) - len(old_source)
    candidate_method_span = SourceSpan(
        old_method.method_declaration.start,
        old_method.method_declaration.end + length_delta,
    )
    if (
        candidate_method_span.end < candidate_method_span.start
        or candidate_method_span.end > len(new_source)
    ):
        return None
    prepared = _prepare_isolated_worker_method(
        unit,
        catalog,
        parser_target,
        candidate_method_span,
        previous.analysis.context,
        profiler=profiler,
    )

    def analyze_and_merge() -> AnalyzedWorkerModule | None:
        isolated = prepared.analyze()
        if isolated is None:
            return None
        new_method = isolated.method
        if (
            new_method.method_name != old_method.method_name
            or new_method.exported != old_method.exported
            or new_method.signature_sha256 != old_method.signature_sha256
            or not _span_contains_edit(new_method.executable_body, new_hunk)
        ):
            return None
        return _merge_analysis(
            previous.analysis,
            unit,
            catalog,
            parser_target,
            method_index,
            isolated.method,
            isolated.insertion_point,
            old_method.method_declaration.end,
            length_delta,
        )

    merged = (
        analyze_and_merge()
        if profiler is None
        else profiler.measure("dependency_analysis", analyze_and_merge)
    )
    if merged is None:
        return None
    evidence = WorkerSemanticDeltaEvidence(
        old_method.method_name,
        old_hunk,
        new_hunk,
        length_delta,
    )
    lowered = lower_worker_module_delta(
        previous,
        merged,
        evidence,
        profiler=profiler,
    )
    if lowered is None:
        lowered = lower_worker_module(merged, profiler=profiler)
    return WorkerSemanticSnapshot(
        merged,
        lowered,
        merged.catalog_identity,
        merged.parser_identity,
        evidence,
    )


def lower_worker_module_delta(
    previous: WorkerSemanticSnapshot,
    analysis: AnalyzedWorkerModule,
    evidence: WorkerSemanticDeltaEvidence,
    *,
    profiler: PhaseRecorder | None = None,
) -> LoweredWorkerModule | None:
    """Lower one merged method delta by importing unchanged mapped segments."""
    if not _valid_delta_lowering_inputs(previous, analysis, evidence):
        return None
    prior_plan = _plan_alias_transform(previous.analysis)
    plan_operation = lambda: _plan_alias_transform(analysis)
    plan = (
        plan_operation()
        if profiler is None
        else profiler.measure("alias_transform", plan_operation)
    )
    if not _previous_lowering_is_reusable(previous, prior_plan, evidence):
        return None
    compose = lambda: _compose_worker_module_delta_source(
        previous,
        analysis,
        plan,
        evidence,
    )
    try:
        mapped_source = (
            compose()
            if profiler is None
            else profiler.measure("source_map_composition", compose)
        )
    except (_SegmentReuseUnsupported, ValueError):
        return None
    lowered = LoweredWorkerModule(
        analysis,
        mapped_source,
        _canonical_dependency_bindings_sha256(analysis.dependencies),
    )
    _mint_worker_semantic_admission(lowered)
    return lowered


def _valid_delta_lowering_inputs(
    previous: WorkerSemanticSnapshot,
    analysis: AnalyzedWorkerModule,
    evidence: WorkerSemanticDeltaEvidence,
) -> bool:
    try:
        old_source = previous.analysis.unit.mapped_source.text
        new_source = analysis.unit.mapped_source.text
        old_method = next(
            method
            for method in previous.analysis.methods
            if method.method_name.casefold() == evidence.method_name.casefold()
        )
        new_method = next(
            method
            for method in analysis.methods
            if method.method_name.casefold() == evidence.method_name.casefold()
        )
        return bool(
            isinstance(previous, WorkerSemanticSnapshot)
            and isinstance(analysis, AnalyzedWorkerModule)
            and isinstance(evidence, WorkerSemanticDeltaEvidence)
            and previous.lowered.analysis is previous.analysis
            and analysis.unit.logical_name.casefold()
            == previous.analysis.unit.logical_name.casefold()
            and analysis.unit.kind == previous.analysis.unit.kind
            and evidence.length_delta == len(new_source) - len(old_source)
            and evidence.old_span.end - evidence.old_span.start
            + evidence.length_delta
            == evidence.new_span.end - evidence.new_span.start
            and _single_unambiguous_hunk(old_source, new_source)
            == (evidence.old_span, evidence.new_span)
            and _span_contains_edit(old_method.executable_body, evidence.old_span)
            and _span_contains_edit(new_method.executable_body, evidence.new_span)
        )
    except (AttributeError, StopIteration, TypeError, ValueError):
        return False


def _previous_lowering_is_reusable(
    previous: WorkerSemanticSnapshot,
    prior_plan: _AliasTransformPlan,
    evidence: WorkerSemanticDeltaEvidence,
) -> bool:
    try:
        raw = previous.analysis.unit.mapped_source
        mapped = previous.lowered.mapped_source
        projection_source = mapped.transform_parent
        _validate_retained_source(mapped)
        if isinstance(projection_source, (MappedSource, _DeferredMappedSource)):
            _validate_retained_source(projection_source)
        lineage_offset = len(raw.lineage)
        if (
            not isinstance(projection_source, (MappedSource, _DeferredMappedSource))
            or len(mapped.lineage) != lineage_offset + 2
            or mapped.lineage[:lineage_offset] != raw.lineage
        ):
            return False
        projection = mapped.lineage[lineage_offset]
        wrapper = mapped.lineage[lineage_offset + 1]
        if (
            projection.generated.kind is not SourceArtifactKind.WORKER_PROJECTION
            or wrapper.generated.kind is not SourceArtifactKind.WORKER_MODULE
            or wrapper is not mapped.local_source_map
            or wrapper.generated != mapped.artifact
            or projection_source.artifact != projection.generated
            or projection_source.local_source_map != projection
            or projection_source.source_map.generated != projection.generated
        ):
            return False
        if any(
            segment.origin_ref != projection.generated
            for segment in wrapper.segments
            if segment.relation is not MappingRelation.SYNTHETIC
        ):
            return False
        if any(
            segment.anchor_ref is not None
            and segment.anchor_ref != projection.generated
            for segment in wrapper.segments
        ):
            return False

        exact = tuple(
            segment
            for segment in projection.segments
            if segment.relation is MappingRelation.EXACT
        )
        cursor = 0
        for segment in exact:
            if segment.origin_ref != raw.artifact or segment.origin is None:
                return False
            if segment.origin.start != cursor:
                return False
            cursor = segment.origin.end
        if cursor != len(raw.text):
            return False
        hunk_segments = tuple(
            segment
            for segment in exact
            if segment.origin is not None
            and _span_contains_edit(segment.origin, evidence.old_span)
        )
        if len(hunk_segments) != 1:
            return False

        fields = prior_plan.fields
        field_segments: list[SourceMapSegment] = []
        for segment in projection.segments:
            if segment.synthetic_region != DEPENDENCY_FIELD_REGION:
                break
            field_segments.append(segment)
        if len(field_segments) != len(fields):
            return False
        generated_cursor = 0
        for segment, (text, anchor, region) in zip(
            field_segments,
            fields,
            strict=True,
        ):
            if (
                segment.generated.start != generated_cursor
                or segment.generated.end - segment.generated.start != len(text)
                or segment.relation is not MappingRelation.SYNTHETIC
                or segment.synthetic_region != region
                or segment.anchor_ref != raw.artifact
                or segment.anchor_span != anchor
            ):
                return False
            generated_cursor = segment.generated.end

        expected_aliases = _planned_alias_fragments(prior_plan)
        alias_segments = tuple(
            segment
            for segment in projection.segments
            if segment.relation is MappingRelation.DERIVED
        )
        if len(alias_segments) != len(expected_aliases):
            return False
        for segment, (text, use, region) in zip(
            alias_segments,
            expected_aliases,
            strict=True,
        ):
            if (
                segment.generated.end - segment.generated.start != len(text)
                or segment.origin_ref != raw.artifact
                or segment.origin != use.span
                or segment.synthetic_region != region
                or segment.anchor_ref != raw.artifact
                or segment.anchor_span != use.method_declaration
            ):
                return False
        return all(
            segment.relation is MappingRelation.EXACT
            or segment.synthetic_region
            in {
                DEPENDENCY_FIELD_REGION,
                DEPENDENCY_ALIAS_DECLARATION_REGION,
                DEPENDENCY_ALIAS_INITIALIZER_REGION,
            }
            for segment in projection.segments
        )
    except (AttributeError, IndexError, TypeError, ValueError):
        return False


def _planned_alias_fragments(
    plan: _AliasTransformPlan,
) -> tuple[tuple[str, DependencyUse, str], ...]:
    source = plan.source
    line_ending = plan.line_ending
    declarations = plan.declarations
    initializers = plan.initializers
    fragments: list[tuple[str, DependencyUse, str]] = []
    for offset in sorted({*declarations, *initializers}):
        for index, (text, use, region) in enumerate(declarations.get(offset, ())):
            if index == 0 and offset > 0 and source.text[offset - 1] not in "\r\n":
                text = f"{line_ending}{text}"
            fragments.append((text, use, region))
        fragments.extend(initializers.get(offset, ()))
    return tuple(fragments)


def _compose_worker_module_delta_source(
    previous: WorkerSemanticSnapshot,
    analysis: AnalyzedWorkerModule,
    plan: _AliasTransformPlan,
    evidence: WorkerSemanticDeltaEvidence,
) -> MappedSource:
    source = plan.source
    line_ending = plan.line_ending
    fields = plan.fields
    declarations = plan.declarations
    initializers = plan.initializers
    new_method = next(
        method
        for method in analysis.methods
        if method.method_name.casefold() == evidence.method_name.casefold()
    )
    changed_method = new_method.method_declaration
    previous_module = previous.lowered.mapped_source
    previous_projection = previous_module.transform_parent
    if not isinstance(previous_projection, (MappedSource, _DeferredMappedSource)):
        raise _SegmentReuseUnsupported
    prior_projection_map = previous_projection.local_source_map
    prior_projection_index = _OriginSegmentIndex(prior_projection_map)
    prior_plan = _plan_alias_transform(previous.analysis)

    prior_field_segments = tuple(
        segment
        for segment in prior_projection_map.segments
        if segment.synthetic_region == DEPENDENCY_FIELD_REGION
    )
    if len(prior_field_segments) != len(prior_plan.fields):
        raise _SegmentReuseUnsupported
    prior_fields = {
        (binding.target_module.casefold(), binding.export_variable.casefold()): (
            text,
            segment.generated,
        )
        for binding, (text, _anchor, _region), segment in zip(
            previous.analysis.dependencies,
            prior_plan.fields,
            prior_field_segments,
            strict=True,
        )
    }
    if len(prior_fields) != len(prior_field_segments):
        raise _SegmentReuseUnsupported
    prior_alias_segments = tuple(
        segment
        for segment in prior_projection_map.segments
        if segment.relation is MappingRelation.DERIVED
    )
    prior_alias_fragments = _planned_alias_fragments(prior_plan)
    if len(prior_alias_segments) != len(prior_alias_fragments):
        raise _SegmentReuseUnsupported
    prior_aliases = {
        (use.method_name.casefold(), region, text): segment.generated
        for (text, use, region), segment in zip(
            prior_alias_fragments,
            prior_alias_segments,
            strict=True,
        )
    }
    if len(prior_aliases) != len(prior_alias_segments):
        raise _SegmentReuseUnsupported

    builder = SourceTransformBuilder(source)

    def append_raw(start: int, end: int) -> None:
        boundaries = sorted(
            {
                start,
                end,
                *(
                    point
                    for point in (changed_method.start, changed_method.end)
                    if start < point < end
                ),
            }
        )
        for left, right in zip(boundaries, boundaries[1:], strict=False):
            span = SourceSpan(left, right)
            if changed_method.start <= left and right <= changed_method.end:
                builder.copy(span)
            else:
                old_span = (
                    span
                    if right <= changed_method.start
                    else SourceSpan(
                        left - evidence.length_delta,
                        right - evidence.length_delta,
                    )
                )
                retained = prior_projection_index.generated_span(
                    previous.analysis.unit.mapped_source.artifact,
                    old_span,
                    MappingRelation.EXACT,
                )
                builder.retained_exact(previous_projection, retained, span)

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
            append_raw(cursor, offset)
        if offset == 0:
            for binding, (text, anchor, region) in zip(
                analysis.dependencies,
                fields,
                strict=True,
            ):
                retained_field = prior_fields.get(
                    (
                        binding.target_module.casefold(),
                        binding.export_variable.casefold(),
                    )
                )
                if retained_field is not None and retained_field[0] == text:
                    builder.retained_synthetic(
                        previous_projection,
                        retained_field[1],
                        anchor,
                        region,
                    )
                else:
                    builder.synthetic(text, anchor, region)
        declaration_items = declarations.get(offset, ())
        for index, (text, use, region) in enumerate(declaration_items):
            if index == 0 and offset > 0 and source.text[offset - 1] not in "\r\n":
                text = f"{line_ending}{text}"
            retained_alias = prior_aliases.get(
                (use.method_name.casefold(), region, text)
            )
            if (
                retained_alias is not None
                and use.method_name.casefold() != evidence.method_name.casefold()
            ):
                builder.retained_derived(
                    previous_projection,
                    retained_alias,
                    use.span,
                    region,
                    anchor=use.method_declaration,
                )
            else:
                builder.derived(text, use.span, region, anchor=use.method_declaration)
        for text, use, region in initializers.get(offset, ()):
            retained_alias = prior_aliases.get(
                (use.method_name.casefold(), region, text)
            )
            if (
                retained_alias is not None
                and use.method_name.casefold() != evidence.method_name.casefold()
            ):
                builder.retained_derived(
                    previous_projection,
                    retained_alias,
                    use.span,
                    region,
                    anchor=use.method_declaration,
                )
            else:
                builder.derived(text, use.span, region, anchor=use.method_declaration)
        cursor = offset
    if cursor < len(source.text):
        append_raw(cursor, len(source.text))
    projection = builder.build_deferred(SourceArtifactKind.WORKER_PROJECTION)
    return _prepare_worker_module_source_delta(
        projection,
        previous_projection,
        previous_module,
    )


def _prepare_worker_module_source_delta(
    source: _DeferredMappedSource,
    previous_projection: MappedSource | _DeferredMappedSource,
    previous_module: MappedSource,
) -> MappedSource:
    builder = SourceTransformBuilder(source)
    prior_wrapper_map = previous_module.local_source_map
    prior_wrapper_index = _OriginSegmentIndex(prior_wrapper_map)
    parent_cursor = _GeneratedSegmentCursor(source.source_map)

    fragments = source.retained_fragments
    for fragment_index, fragment in enumerate(fragments):
        local_cursor = 0
        index = fragment.text.find("\r")
        while index >= 0:
            if local_cursor < index:
                _append_normalized_exact_fragment(
                    builder,
                    fragment,
                    SourceSpan(local_cursor, index),
                    previous_projection,
                    previous_module,
                    prior_wrapper_index,
                )
            next_is_lf = fragment.text.startswith("\r\n", index) or (
                index + 1 == len(fragment.text)
                and fragment_index + 1 < len(fragments)
                and fragments[fragment_index + 1].text.startswith("\n")
            )
            if next_is_lf:
                # The full normalizer drops CR and copies LF with its own
                # origin, even when the pair crosses retained fragments.
                local_cursor = index + 1
                index = fragment.text.find("\r", local_cursor)
                continue
            end = index + 1
            current_span = SourceSpan(
                fragment.generated.start + index,
                fragment.generated.start + end,
            )
            parent = parent_cursor.at(current_span.start)
            region = (
                parent.synthetic_region
                if parent.relation is MappingRelation.DERIVED
                and parent.synthetic_region is not None
                else "worker_module_line_ending"
            )
            if fragment.retained_source is previous_projection:
                assert fragment.retained_span is not None
                old_span = SourceSpan(
                    fragment.retained_span.start + index,
                    fragment.retained_span.start + end,
                )
                retained = prior_wrapper_index.generated_span(
                    previous_projection.artifact,
                    old_span,
                    MappingRelation.DERIVED,
                )
                builder.retained_derived(
                    previous_module,
                    retained,
                    current_span,
                    region,
                )
            else:
                builder.derived("\n", current_span, region)
            local_cursor = end
            index = fragment.text.find("\r", end)
        if local_cursor < len(fragment.text):
            _append_normalized_exact_fragment(
                builder,
                fragment,
                SourceSpan(local_cursor, len(fragment.text)),
                previous_projection,
                previous_module,
                prior_wrapper_index,
            )
    return builder.build_canonical_reused(
        SourceArtifactKind.WORKER_MODULE,
        _normalized_worker_authority=_NORMALIZED_WORKER_AUTHORITY,
    )


def _append_normalized_exact_fragment(
    builder: SourceTransformBuilder,
    fragment: _RetainedTextFragment,
    local_span: SourceSpan,
    previous_projection: MappedSource | _DeferredMappedSource,
    previous_module: MappedSource,
    prior_wrapper_index: _OriginSegmentIndex,
) -> None:
    current_span = SourceSpan(
        fragment.generated.start + local_span.start,
        fragment.generated.start + local_span.end,
    )
    if fragment.retained_source is previous_projection:
        assert fragment.retained_span is not None
        old_span = SourceSpan(
            fragment.retained_span.start + local_span.start,
            fragment.retained_span.start + local_span.end,
        )
        retained = prior_wrapper_index.generated_span(
            previous_projection.artifact,
            old_span,
            MappingRelation.EXACT,
        )
        builder.retained_exact(previous_module, retained, current_span)
    else:
        width = local_span.end - local_span.start
        if width > 100_000:
            raise _SegmentReuseUnsupported
        builder.exact(fragment.text[local_span.start : local_span.end], current_span)


def _single_unambiguous_hunk(
    old: str,
    new: str,
) -> tuple[SourceSpan, SourceSpan] | None:
    if old == new:
        return None
    prefix = 0
    limit = min(len(old), len(new))
    while prefix < limit and old[prefix] == new[prefix]:
        prefix += 1
    suffix = 0
    while (
        suffix < len(old) - prefix
        and suffix < len(new) - prefix
        and old[len(old) - suffix - 1] == new[len(new) - suffix - 1]
    ):
        suffix += 1
    old_end = len(old) - suffix
    new_end = len(new) - suffix
    old_changed = old[prefix:old_end]
    new_changed = new[prefix:new_end]

    # Repeated characters can describe the same insertion/deletion at multiple
    # offsets. Refuse that alignment instead of choosing one arbitrarily.
    if not old_changed and new_changed:
        if (
            prefix > 0
            and new_changed[-1] == old[prefix - 1]
            or prefix < len(old)
            and new_changed[0] == old[prefix]
        ):
            return None
    elif old_changed and not new_changed:
        if (
            prefix > 0
            and old_changed[-1] == old[prefix - 1]
            or old_end < len(old)
            and old_changed[0] == old[old_end]
        ):
            return None
    return SourceSpan(prefix, old_end), SourceSpan(prefix, new_end)


def _span_contains_edit(container: SourceSpan, edit: SourceSpan) -> bool:
    if edit.start == edit.end:
        return container.start <= edit.start <= container.end
    return container.start <= edit.start and edit.end <= container.end


def _edit_touches_directive(source: str, edit: SourceSpan) -> bool:
    start = source.rfind("\n", 0, edit.start) + 1
    end = source.find("\n", edit.end)
    if end < 0:
        end = len(source)
    for line in source[start:end].splitlines() or (source[start:end],):
        if re.match(r"[ \t]*#", line):
            return True
    return False


def _merge_analysis(
    previous: AnalyzedWorkerModule,
    unit: WorkerModuleUnit,
    catalog: CommonModuleCatalogSnapshot,
    parser_target: PythonParserTarget,
    method_index: int,
    new_method: WorkerMethodAnalysis,
    new_insertion: MethodInsertionPoints,
    shift_after: int,
    length_delta: int,
) -> AnalyzedWorkerModule:
    methods = tuple(
        new_method
        if index == method_index
        else _shift_method(method, shift_after, length_delta)
        for index, method in enumerate(previous.methods)
    )
    insertion_points = tuple(
        new_insertion
        if index == method_index
        else _shift_insertion(point, shift_after, length_delta)
        for index, point in enumerate(previous.method_insertion_points)
    )
    identifier_spans: dict[str, SourceSpan] = {}
    for token in tokenize(unit.mapped_source.text):
        if token.type == "ID":
            identifier_spans.setdefault(
                token.text.casefold(),
                SourceSpan(token.start, token.end),
            )
    context = WorkerModuleAnalysisContext(
        previous.context.module_variable_names,
        previous.context.method_names,
        frozenset(identifier_spans),
    )
    uses_by_module: dict[str, list[DependencyUse]] = {}
    for method in methods:
        for normalized, use in method.dependency_uses:
            uses_by_module.setdefault(normalized, []).append(use)
    catalog_by_name = {
        descriptor.canonical_name.casefold(): descriptor
        for descriptor in catalog.modules
    }
    generated_names: set[str] = set()
    for normalized in sorted(uses_by_module):
        descriptor = catalog_by_name.get(normalized)
        if descriptor is None:
            raise _analysis_error(
                "worker module analysis input is invalid",
                uses_by_module[normalized][0].span,
                AMBIGUOUS_BINDING,
            )
        generated_name = dependency_export_name(
            descriptor.canonical_name
        ).casefold()
        if generated_name in generated_names:
            raise _analysis_error(
                "generated dependency names collide",
                uses_by_module[normalized][0].span,
                AMBIGUOUS_BINDING,
            )
        generated_names.add(generated_name)
        if generated_name in identifier_spans:
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
        previous.exported_methods,
        insertion_points,
        context,
        methods,
        (
            catalog.profile,
            catalog.preprocessor_profile,
            catalog.revision,
            catalog.sha256,
        ),
        (
            parser_target.grammar_sha256,
            parser_target.metadata.parsergen_package_sha256 or "",
        ),
    )


def _shift_method(
    method: WorkerMethodAnalysis,
    shift_after: int,
    amount: int,
) -> WorkerMethodAnalysis:
    return WorkerMethodAnalysis(
        method.method_name,
        method.exported,
        method.signature_sha256,
        _shift_span(method.method_declaration, shift_after, amount),
        _shift_span(method.executable_body, shift_after, amount),
        _shift_offset(method.declarations_end, shift_after, amount),
        _shift_offset(method.first_statement_start, shift_after, amount),
        tuple(
            (
                normalized,
                DependencyUse(
                    use.method_name,
                    use.category,
                    _shift_span(use.span, shift_after, amount),
                    _shift_span(use.method_declaration, shift_after, amount),
                ),
            )
            for normalized, use in method.dependency_uses
        ),
    )


def _shift_insertion(
    point: MethodInsertionPoints,
    shift_after: int,
    amount: int,
) -> MethodInsertionPoints:
    return MethodInsertionPoints(
        point.method_name,
        _shift_span(point.method_declaration, shift_after, amount),
        _shift_offset(point.declarations_end, shift_after, amount),
        _shift_offset(point.first_statement_start, shift_after, amount),
    )


def _shift_span(span: SourceSpan, shift_after: int, amount: int) -> SourceSpan:
    return SourceSpan(
        _shift_offset(span.start, shift_after, amount),
        _shift_offset(span.end, shift_after, amount),
    )


def _shift_offset(value: int, shift_after: int, amount: int) -> int:
    return value + amount if value >= shift_after else value
