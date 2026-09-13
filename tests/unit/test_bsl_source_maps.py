import hashlib
import json
from dataclasses import asdict, replace

import pytest

import onec_runtime.bsl.source_maps as source_maps
from onec_runtime.bsl.source_maps import (
    LineIndex,
    MappedSource,
    MappingRelation,
    SourceMap,
    SourceMapSegment,
    SourceArtifactKind,
    SourceArtifactRef,
    SourceSpan,
    SourceTransformBuilder,
    SourceUnitKind,
    SourceUnitRef,
    compose_source_maps,
    map_offset,
    mapped_visible_source,
    source_sha256,
)


def _unit(text: str, unit_id: str = "cell-main", revision: int = 7) -> SourceUnitRef:
    return SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL,
        unit_id,
        revision,
        source_sha256(text),
    )


def _artifact(text: str, kind: SourceArtifactKind) -> SourceArtifactRef:
    return SourceArtifactRef(kind, source_sha256(text), len(text), "none")


def _projection_with_exact_and_synthetic_join() -> MappedSource:
    source = "первая\nвторая"
    visible = mapped_visible_source(source, _unit(source))
    builder = SourceTransformBuilder(visible)
    builder.copy(SourceSpan(0, 6))
    builder.synthetic("// join\n", SourceSpan(6, 7), "notebook_projection_join")
    builder.copy(SourceSpan(7, len(source)))
    return builder.build(SourceArtifactKind.STATEMENT_PROJECTION)


def _wrapper_over(parent: MappedSource) -> MappedSource:
    builder = SourceTransformBuilder(parent)
    builder.copy(SourceSpan(0, len(parent.text)))
    return builder.build(SourceArtifactKind.EXECUTED_BSL)


def test_line_index_preserves_crlf_and_unicode_code_point_offsets() -> None:
    source = "Первая\r\n😀Вторая\n"
    index = LineIndex(source)

    assert index.offset_to_line_column(0) == (1, 1)
    assert index.offset_to_line_column(source.index("😀")) == (2, 1)
    assert index.line_column_to_offset(2, 2) == source.index("В")
    assert index.line_column_to_offset(3, 1) == len(source)


def test_source_identity_rejects_invalid_hash_and_span() -> None:
    with pytest.raises(ValueError, match="source_sha256"):
        SourceUnitRef(SourceUnitKind.NOTEBOOK_CELL, "cell", 1, "short")
    with pytest.raises(ValueError, match="span"):
        SourceSpan(4, 3)


def test_source_sha256_hashes_utf8_source() -> None:
    source = "Первая\r\n😀Вторая\n"
    assert source_sha256(source) == hashlib.sha256(source.encode("utf-8")).hexdigest()


def test_source_span_is_nonnegative_half_open_interval() -> None:
    assert SourceSpan(0, 0) <= SourceSpan(1, 2)
    with pytest.raises(ValueError, match="span"):
        SourceSpan(-1, 0)


def test_line_index_rejects_invalid_coordinates() -> None:
    index = LineIndex("a\nb")
    with pytest.raises(ValueError, match="offset"):
        index.offset_to_line_column(-1)
    with pytest.raises(ValueError, match="offset"):
        index.offset_to_line_column(4)
    with pytest.raises(ValueError, match="line"):
        index.line_column_to_offset(0, 1)
    with pytest.raises(ValueError, match="column"):
        index.line_column_to_offset(1, 0)
    with pytest.raises(ValueError, match="line"):
        index.line_column_to_offset(4, 1)


def test_source_artifact_ref_accepts_metadata_without_source_text() -> None:
    artifact = SourceArtifactRef(
        SourceArtifactKind.VISIBLE,
        "a" * 64,
        12,
        "crlf",
        mode="debug",
    )
    assert artifact.source_sha256 == "a" * 64
    assert artifact.mode == "debug"


@pytest.mark.parametrize(
    "start,end",
    [(0.5, 1.5), (False, 1), (0, True), (0, 1.0)],
)
def test_source_span_rejects_non_integer_coordinates(start: object, end: object) -> None:
    with pytest.raises(ValueError, match="span"):
        SourceSpan(start, end)


def test_source_unit_ref_rejects_invalid_kind_revision_and_string_fields() -> None:
    digest = "a" * 64
    with pytest.raises(ValueError, match="kind"):
        SourceUnitRef("unknown", "id", 1, digest)
    with pytest.raises(ValueError, match="revision"):
        SourceUnitRef(SourceUnitKind.MODULE, "id", 1.5, digest)
    with pytest.raises(ValueError, match="revision"):
        SourceUnitRef(SourceUnitKind.MODULE, "id", False, digest)
    with pytest.raises(ValueError, match="unit_id"):
        SourceUnitRef(SourceUnitKind.MODULE, 42, 1, digest)


def test_source_artifact_ref_rejects_invalid_types() -> None:
    digest = "a" * 64
    with pytest.raises(ValueError, match="kind"):
        SourceArtifactRef("unknown", digest, 1, "lf")
    with pytest.raises(ValueError, match="character_length"):
        SourceArtifactRef(SourceArtifactKind.VISIBLE, digest, 1.5, "lf")
    with pytest.raises(ValueError, match="character_length"):
        SourceArtifactRef(SourceArtifactKind.VISIBLE, digest, True, "lf")
    with pytest.raises(ValueError, match="line_ending_kind"):
        SourceArtifactRef(SourceArtifactKind.VISIBLE, digest, 1, None)
    with pytest.raises(ValueError, match="worker_generation"):
        SourceArtifactRef(SourceArtifactKind.VISIBLE, digest, 1, "lf", worker_generation=0.5)
    with pytest.raises(ValueError, match="mode"):
        SourceArtifactRef(SourceArtifactKind.VISIBLE, digest, 1, "lf", mode=object())


@pytest.mark.parametrize("value", [False, 1.5, "1"])
def test_line_index_rejects_non_integer_coordinates(value: object) -> None:
    index = LineIndex("a\nb")
    with pytest.raises(ValueError, match="offset"):
        index.offset_to_line_column(value)
    with pytest.raises(ValueError, match="line"):
        index.line_column_to_offset(value, 1)
    with pytest.raises(ValueError, match="column"):
        index.line_column_to_offset(1, value)


def test_transform_composes_exact_derived_and_synthetic_fragments() -> None:
    source = "Ответ = 1;"
    visible = mapped_visible_source(source, _unit(source))
    builder = SourceTransformBuilder(visible)
    builder.synthetic('Контекст.Вставить("', SourceSpan(0, 6), "persistent_assignment")
    builder.derived("Ответ", SourceSpan(0, 5), "persistent_name")
    builder.synthetic('", ', SourceSpan(0, 7), "persistent_assignment")
    builder.copy(SourceSpan(8, 9))
    builder.synthetic(")", SourceSpan(0, 10), "persistent_assignment")
    builder.copy(SourceSpan(9, 10))
    lowered = builder.build(SourceArtifactKind.SEMANTIC_LOWERING)

    one = lowered.text.index("1")
    assert lowered.source_map.map_offset(one).origin_span == SourceSpan(8, 9)
    assert lowered.source_map.map_offset(one).relation is MappingRelation.EXACT
    runtime_prefix = lowered.text.index("Контекст")
    synthetic = lowered.source_map.map_offset(runtime_prefix)
    assert synthetic.relation is MappingRelation.SYNTHETIC
    assert synthetic.unit is None
    assert synthetic.origin_span is None
    assert synthetic.anchor_unit == _unit(source)
    assert synthetic.anchor_span == SourceSpan(0, 6)

    persistent_name = lowered.source_map.map_offset(lowered.text.index("Ответ"))
    assert persistent_name.relation is MappingRelation.DERIVED
    assert persistent_name.origin_span == SourceSpan(0, 5)


def test_composition_splits_segments_and_never_promotes_confidence() -> None:
    projected = _projection_with_exact_and_synthetic_join()
    wrapped = _wrapper_over(projected)

    mapped = wrapped.source_map.map_offset(wrapped.text.index("вторая"))
    assert mapped.unit is not None
    assert mapped.unit.unit_id == "cell-main"
    assert mapped.relation is MappingRelation.EXACT
    join = wrapped.source_map.map_offset(wrapped.text.index("// join"))
    assert join.relation is MappingRelation.SYNTHETIC
    assert join.synthetic_region == "notebook_projection_join"
    assert len(wrapped.source_map.segments) == 3


def test_derived_relation_remains_derived_through_exact_wrapping() -> None:
    source = "Имя"
    visible = mapped_visible_source(source, _unit(source))
    derived_builder = SourceTransformBuilder(visible)
    derived_builder.derived("ИМЯ", SourceSpan(0, 3), "normalized_name")
    derived = derived_builder.build(SourceArtifactKind.SEMANTIC_LOWERING)

    wrapped = _wrapper_over(derived)
    mapped = wrapped.source_map.map_offset(1)
    assert mapped.relation is MappingRelation.DERIVED
    assert mapped.origin_span == SourceSpan(0, 3)


@pytest.mark.parametrize(
    ("generated_text", "origin"),
    (("ABC", SourceSpan(0, 3)), ("X", SourceSpan(0, 3))),
)
def test_derived_composition_preserves_local_anchor_for_equal_and_unequal_widths(
    generated_text: str,
    origin: SourceSpan,
) -> None:
    source = "abc"
    visible = mapped_visible_source(source, _unit(source))
    generated = _artifact(generated_text, SourceArtifactKind.SEMANTIC_LOWERING)
    local = SourceMap(
        generated,
        (
            SourceMapSegment(
                SourceSpan(0, len(generated_text)),
                visible.artifact,
                origin,
                MappingRelation.DERIVED,
                "worker_dependency_alias",
                visible.artifact,
                SourceSpan(0, 1),
            ),
        ),
    )

    composed = compose_source_maps(local, visible.source_map)

    assert composed.segments == (
        SourceMapSegment(
            SourceSpan(0, len(generated_text)),
            _unit(source),
            origin,
            MappingRelation.DERIVED,
            "worker_dependency_alias",
            _unit(source),
            SourceSpan(0, 1),
        ),
    )
    mapped = composed.map_offset(0)
    assert mapped.anchor_unit == _unit(source)
    assert mapped.anchor_span == SourceSpan(0, 1)


def test_exact_wrapping_preserves_parent_derived_region_and_anchor() -> None:
    source = "abc"
    visible = mapped_visible_source(source, _unit(source))
    derived_artifact = _artifact("ABC", SourceArtifactKind.SEMANTIC_LOWERING)
    derived_map = SourceMap(
        derived_artifact,
        (
            SourceMapSegment(
                SourceSpan(0, 3),
                visible.artifact,
                SourceSpan(0, 3),
                MappingRelation.DERIVED,
                "worker_dependency_alias",
                visible.artifact,
                SourceSpan(0, 1),
            ),
        ),
    )
    derived = MappedSource(
        "ABC",
        derived_artifact,
        compose_source_maps(derived_map, visible.source_map),
        (derived_map,),
        derived_map,
    )

    wrapped = _wrapper_over(derived)

    assert wrapped.source_map.segments[0].relation is MappingRelation.DERIVED
    assert wrapped.source_map.segments[0].synthetic_region == "worker_dependency_alias"
    assert wrapped.source_map.segments[0].anchor_ref == _unit(source)
    assert wrapped.source_map.segments[0].anchor_span == SourceSpan(0, 1)


def test_insertion_boundaries_map_to_the_explicit_fragment_on_each_side() -> None:
    source = "ab"
    visible = mapped_visible_source(source, _unit(source))
    builder = SourceTransformBuilder(visible)
    builder.copy(SourceSpan(0, 1))
    builder.synthetic("X", SourceSpan(1, 1), "insert")
    builder.copy(SourceSpan(1, 2))
    generated = builder.build(SourceArtifactKind.STATEMENT_PROJECTION)

    assert generated.text == "aXb"
    assert generated.source_map.map_offset(0).origin_span == SourceSpan(0, 1)
    assert generated.source_map.map_offset(1).relation is MappingRelation.SYNTHETIC
    assert generated.source_map.map_offset(2).origin_span == SourceSpan(1, 2)


def test_zero_width_deletion_is_retained_but_not_returned_by_lookup() -> None:
    source = "abc"
    visible = mapped_visible_source(source, _unit(source))
    builder = SourceTransformBuilder(visible)
    builder.copy(SourceSpan(0, 1))
    builder.derived("", SourceSpan(1, 2), "deleted_character")
    builder.copy(SourceSpan(2, 3))
    generated = builder.build(SourceArtifactKind.STATEMENT_PROJECTION)

    assert generated.text == "ac"
    deletion = generated.local_source_map.segments[1]
    assert deletion.generated == SourceSpan(1, 1)
    assert deletion.origin == SourceSpan(1, 2)
    assert generated.source_map.map_offset(1).origin_span == SourceSpan(2, 3)


def test_eof_lookup_projects_the_exact_end_boundary() -> None:
    source = "abc"
    visible = mapped_visible_source(source, _unit(source))
    copied = _wrapper_over(visible)

    assert visible.source_map.map_offset(3).origin_span == SourceSpan(3, 3)
    assert copied.source_map.map_offset(3).origin_span == SourceSpan(3, 3)
    assert copied.source_map.map_offset(3).relation is MappingRelation.EXACT


def test_empty_visible_source_maps_its_eof_without_inventing_text() -> None:
    visible = mapped_visible_source("", _unit(""))
    mapped = visible.source_map.map_offset(0)
    assert mapped.relation is MappingRelation.EXACT
    assert mapped.origin_span == SourceSpan(0, 0)


def test_repeated_fragments_are_mapped_only_by_explicit_copy_spans() -> None:
    source = "x x"
    visible = mapped_visible_source(source, _unit(source))
    builder = SourceTransformBuilder(visible)
    builder.copy(SourceSpan(2, 3))
    builder.synthetic(" ", SourceSpan(1, 2), "reorder_join")
    builder.copy(SourceSpan(0, 1))
    reordered = builder.build(SourceArtifactKind.STATEMENT_PROJECTION)

    assert reordered.text == source
    assert reordered.source_map.map_offset(0).origin_span == SourceSpan(2, 3)
    assert reordered.source_map.map_offset(2).origin_span == SourceSpan(0, 1)


def test_adjacent_compatible_segments_are_coalesced_deterministically() -> None:
    source = "abc"
    visible = mapped_visible_source(source, _unit(source))
    builder = SourceTransformBuilder(visible)
    builder.copy(SourceSpan(0, 1))
    builder.copy(SourceSpan(1, 3))
    copied = builder.build(SourceArtifactKind.STATEMENT_PROJECTION)

    expected = SourceMapSegment(
        SourceSpan(0, 3),
        _unit(source),
        SourceSpan(0, 3),
        MappingRelation.EXACT,
    )
    assert copied.source_map.segments == (expected,)
    assert copied.local_source_map.segments == (
        SourceMapSegment(
            SourceSpan(0, 3),
            visible.artifact,
            SourceSpan(0, 3),
            MappingRelation.EXACT,
        ),
    )


def test_composition_supports_multiple_visible_origin_units() -> None:
    text = "aabb"
    artifact = _artifact(text, SourceArtifactKind.WORKER_MODULE)
    first = _unit("aa", "module-a", 1)
    second = _unit("bb", "module-b", 4)
    parent_map = SourceMap(
        artifact,
        (
            SourceMapSegment(SourceSpan(0, 2), first, SourceSpan(0, 2), MappingRelation.EXACT),
            SourceMapSegment(SourceSpan(2, 4), second, SourceSpan(0, 2), MappingRelation.EXACT),
        ),
    )
    parent = MappedSource(text, artifact, parent_map)

    wrapped = _wrapper_over(parent)
    assert wrapped.source_map.map_offset(0).unit == first
    assert wrapped.source_map.map_offset(3).unit == second
    assert len(wrapped.source_map.segments) == 2


def test_composition_rejects_a_local_map_for_the_wrong_parent_hash() -> None:
    parent = mapped_visible_source("abc", _unit("abc"))
    generated = _artifact("abc", SourceArtifactKind.STATEMENT_PROJECTION)
    wrong_parent = replace(parent.artifact, source_sha256="f" * 64)
    local = SourceMap(
        generated,
        (
            SourceMapSegment(
                SourceSpan(0, 3),
                wrong_parent,
                SourceSpan(0, 3),
                MappingRelation.EXACT,
            ),
        ),
    )

    with pytest.raises(ValueError, match="parent artifact"):
        compose_source_maps(local, parent.source_map)


@pytest.mark.parametrize("offset", [-1, 4, 1.5, False])
def test_map_offset_rejects_out_of_range_or_non_integer_offsets(offset: object) -> None:
    source_map = mapped_visible_source("abc", _unit("abc")).source_map
    with pytest.raises(ValueError, match="offset"):
        map_offset(source_map, offset)


@pytest.mark.parametrize(
    "segments,error",
    (
        (
            (SourceMapSegment(SourceSpan(1, 3), None, None, MappingRelation.SYNTHETIC, "gap"),),
            "coverage",
        ),
        (
            (
                SourceMapSegment(SourceSpan(0, 2), None, None, MappingRelation.SYNTHETIC, "first"),
                SourceMapSegment(
                    SourceSpan(1, 3), None, None, MappingRelation.SYNTHETIC, "overlap"
                ),
            ),
            "ordered",
        ),
        (
            (SourceMapSegment(SourceSpan(0, 4), None, None, MappingRelation.SYNTHETIC, "bounds"),),
            "bounds",
        ),
    ),
)
def test_source_map_requires_ordered_non_overlapping_full_generated_coverage(
    segments: tuple[SourceMapSegment, ...], error: str
) -> None:
    with pytest.raises(ValueError, match=error):
        SourceMap(_artifact("abc", SourceArtifactKind.EXECUTED_BSL), segments)


def test_segments_validate_exact_lengths_and_synthetic_coordinate_honesty() -> None:
    unit = _unit("abc")
    with pytest.raises(ValueError, match="one-to-one"):
        SourceMapSegment(
            SourceSpan(0, 2), unit, SourceSpan(0, 3), MappingRelation.EXACT
        )
    with pytest.raises(ValueError, match="origin coordinate"):
        SourceMapSegment(
            SourceSpan(0, 1), unit, SourceSpan(0, 1), MappingRelation.SYNTHETIC, "runtime"
        )
    with pytest.raises(ValueError, match="region"):
        SourceMapSegment(SourceSpan(0, 1), None, None, MappingRelation.SYNTHETIC)


def test_builder_rejects_out_of_bounds_fragments_and_identity_metadata() -> None:
    visible = mapped_visible_source("abc", _unit("abc"))
    builder = SourceTransformBuilder(visible)
    with pytest.raises(ValueError, match="parent source"):
        builder.copy(SourceSpan(0, 4))
    with pytest.raises(ValueError, match="parent source"):
        builder.derived("x", SourceSpan(4, 4), "derived")
    with pytest.raises(ValueError, match="parent source"):
        builder.synthetic("x", SourceSpan(3, 4), "synthetic")

    builder.copy(SourceSpan(0, 3))
    with pytest.raises(ValueError, match="identity metadata"):
        builder.build(SourceArtifactKind.EXECUTED_BSL, source_text="secret")


@pytest.mark.parametrize("line_ending", ("\n", "\r\n"))
def test_segment_reuse_preserves_exact_derived_synthetic_and_anchor_maps(
    line_ending: str,
) -> None:
    """Break caught: imported segments must remain identical to a fresh transform."""
    source = f"Первый();{line_ending}Второй();{line_ending}"
    visible = mapped_visible_source(source, _unit(source))
    anchor = SourceSpan(source.index("Второй"), source.index("Второй") + 6)

    fresh = SourceTransformBuilder(visible)
    fresh.synthetic(f"Перем Сервис;{line_ending}", anchor, "field")
    fresh.copy(SourceSpan(0, source.index("Второй")))
    fresh.derived(
        f"Сервис = Значение;{line_ending}",
        anchor,
        "alias",
        anchor=anchor,
    )
    fresh.copy(SourceSpan(source.index("Второй"), len(source)))
    expected = fresh.build(SourceArtifactKind.WORKER_PROJECTION)

    reused = SourceTransformBuilder(visible)
    reused.reuse_synthetic(f"Перем Сервис;{line_ending}", anchor, "field")
    reused.reuse_exact(SourceSpan(0, source.index("Второй")))
    reused.reuse_derived(
        f"Сервис = Значение;{line_ending}",
        anchor,
        "alias",
        anchor=anchor,
    )
    reused.reuse_exact(SourceSpan(source.index("Второй"), len(source)))
    actual = reused.build_reused(SourceArtifactKind.WORKER_PROJECTION)

    assert actual.text == expected.text
    assert actual.source_map.to_manifest() == expected.source_map.to_manifest()
    assert actual.lineage_manifest() == expected.lineage_manifest()
    assert actual.source_map_sha256 == expected.source_map_sha256


def test_mapped_source_hash_fence_and_lineage_are_text_free() -> None:
    source = "business-secret"
    visible = mapped_visible_source(source, _unit(source))
    builder = SourceTransformBuilder(visible)
    builder.copy(SourceSpan(0, len(source)))
    mapped = builder.build(SourceArtifactKind.EXECUTED_BSL, mode="main")

    assert mapped.local_source_map.generated == mapped.artifact
    assert mapped.local_source_map.segments[0].origin_ref == visible.artifact
    assert mapped.lineage == (mapped.local_source_map,)
    serialized = json.dumps(mapped.lineage_manifest(), ensure_ascii=False, sort_keys=True)
    assert source not in serialized
    assert source not in repr(mapped)
    assert mapped.source_map_sha256 == mapped.source_map.source_map_sha256
    assert len(mapped.source_map_sha256) == 64
    with pytest.raises(TypeError):
        asdict(mapped)

    wrong_artifact = replace(mapped.artifact, source_sha256="0" * 64)
    with pytest.raises(ValueError, match="artifact hash"):
        MappedSource(mapped.text, wrong_artifact, mapped.source_map)


def test_source_map_manifest_contains_only_hashes_spans_enums_and_identity_metadata() -> None:
    source = "секрет\r\n😀"
    mapped = mapped_visible_source(source, _unit(source))
    manifest = mapped.source_map.to_manifest()
    encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True)

    assert source not in encoded
    assert "source_sha256" in encoded
    assert manifest["generated"]["line_ending_kind"] == "crlf"


def test_source_map_construction_proof_hmacs_exact_canonical_manifest_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = _artifact("abcd", SourceArtifactKind.VISIBLE)
    unit = _unit("abcd")
    updates: list[bytes] = []
    original_hmac_new = source_maps.hmac_new

    class ObservedMac:
        def __init__(self, delegate: object) -> None:
            self._delegate = delegate

        def update(self, payload: bytes) -> None:
            updates.append(payload)
            self._delegate.update(payload)

        def digest(self) -> bytes:
            return self._delegate.digest()

    def observed_hmac_new(*args: object, **kwargs: object) -> ObservedMac:
        return ObservedMac(original_hmac_new(*args, **kwargs))

    monkeypatch.setattr(source_maps, "hmac_new", observed_hmac_new)

    source_map = SourceMap(
        artifact,
        (
            SourceMapSegment(
                SourceSpan(0, 4),
                unit,
                SourceSpan(0, 4),
                MappingRelation.EXACT,
            ),
        ),
    )

    assert updates == [source_map._canonical_manifest_bytes]
    expected = json.dumps(
        source_map.to_manifest(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert source_map._canonical_manifest_bytes == expected
    assert source_map.source_map_sha256 == hashlib.sha256(expected).hexdigest()


def test_validated_source_map_proof_recomputes_live_canonical_manifest() -> None:
    mapped = mapped_visible_source("abcd", _unit("abcd"))
    source_maps._validated_mapped_semantic_summary(mapped)
    segment = mapped.source_map.segments[0]
    object.__setattr__(
        mapped.source_map,
        "segments",
        (replace(segment, synthetic_region="tampered"),),
    )

    with pytest.raises(ValueError, match="semantic authority"):
        source_maps._validated_mapped_semantic_summary(mapped)


def test_structural_proof_uses_one_hmac_update_for_many_segments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = "x" * 200
    artifact = _artifact(text, SourceArtifactKind.VISIBLE)
    unit = _unit(text)
    segments = tuple(
        SourceMapSegment(
            SourceSpan(offset, offset + 1),
            unit,
            SourceSpan(offset, offset + 1),
            MappingRelation.EXACT,
        )
        for offset in range(len(text))
    )
    work: list[tuple[str, int]] = []
    monkeypatch.setattr(
        source_maps,
        "_TEXT_WORK_OBSERVER",
        lambda event, width: work.append((event, width)),
    )

    source_map = SourceMap(artifact, segments)
    mapped = MappedSource(text, artifact, source_map)
    source_maps._validated_mapped_semantic_summary(mapped)

    assert [
        width
        for event, width in work
        if event == "semantic_structural_hmac_update"
    ] == [1, 1]
    assert [
        width
        for event, width in work
        if event == "semantic_structural_segment_visit"
    ] == [len(segments)]


def test_unequal_width_derived_composition_rejects_multiple_reordered_parent_segments() -> None:
    text = "abcd"
    artifact = _artifact(text, SourceArtifactKind.STATEMENT_PROJECTION)
    unit = _unit(text)
    parent_map = SourceMap(
        artifact,
        (
            SourceMapSegment(
                SourceSpan(0, 2), unit, SourceSpan(2, 4), MappingRelation.EXACT
            ),
            SourceMapSegment(
                SourceSpan(2, 4), unit, SourceSpan(0, 2), MappingRelation.EXACT
            ),
        ),
    )
    parent = MappedSource(text, artifact, parent_map)
    builder = SourceTransformBuilder(parent)
    builder.derived("x", SourceSpan(0, 4), "ambiguous_rewrite")

    with pytest.raises(ValueError, match="unequal-width derived"):
        builder.build(SourceArtifactKind.SEMANTIC_LOWERING)


def test_unequal_width_derived_composition_rejects_exact_and_synthetic_parents() -> None:
    parent = _projection_with_exact_and_synthetic_join()
    builder = SourceTransformBuilder(parent)
    builder.derived("x", SourceSpan(0, len(parent.text)), "ambiguous_rewrite")

    with pytest.raises(ValueError, match="unequal-width derived"):
        builder.build(SourceArtifactKind.SEMANTIC_LOWERING)


def test_zero_width_deletion_provenance_is_not_a_mappable_empty_artifact_position() -> None:
    artifact = _artifact("", SourceArtifactKind.STATEMENT_PROJECTION)
    deletion_map = SourceMap(
        artifact,
        (
            SourceMapSegment(
                SourceSpan(0, 0),
                _unit("a"),
                SourceSpan(0, 1),
                MappingRelation.DERIVED,
                "deleted_source",
            ),
        ),
    )

    with pytest.raises(ValueError, match="mappable position"):
        deletion_map.map_offset(0)


@pytest.mark.parametrize("relation", [MappingRelation.EXACT, MappingRelation.SYNTHETIC])
def test_mapped_source_rejects_intermediate_refs_in_its_flattened_map(
    relation: MappingRelation,
) -> None:
    text = "x"
    artifact = _artifact(text, SourceArtifactKind.EXECUTED_BSL)
    intermediate = _artifact(text, SourceArtifactKind.SEMANTIC_LOWERING)
    if relation is MappingRelation.EXACT:
        segment = SourceMapSegment(
            SourceSpan(0, 1), intermediate, SourceSpan(0, 1), relation
        )
    else:
        segment = SourceMapSegment(
            SourceSpan(0, 1),
            None,
            None,
            relation,
            "wrapper",
            intermediate,
            SourceSpan(0, 1),
        )

    with pytest.raises(ValueError, match="flattened source map"):
        MappedSource(text, artifact, SourceMap(artifact, (segment,)))


def test_composition_rejects_an_unflattened_parent_map() -> None:
    parent_text = "x"
    parent_artifact = _artifact(parent_text, SourceArtifactKind.SEMANTIC_LOWERING)
    intermediate = _artifact(parent_text, SourceArtifactKind.STATEMENT_PROJECTION)
    parent = SourceMap(
        parent_artifact,
        (
            SourceMapSegment(
                SourceSpan(0, 1),
                intermediate,
                SourceSpan(0, 1),
                MappingRelation.EXACT,
            ),
        ),
    )
    generated = _artifact(parent_text, SourceArtifactKind.EXECUTED_BSL)
    local = SourceMap(
        generated,
        (
            SourceMapSegment(
                SourceSpan(0, 1),
                parent_artifact,
                SourceSpan(0, 1),
                MappingRelation.EXACT,
            ),
        ),
    )

    with pytest.raises(ValueError, match="flattened parent map"):
        compose_source_maps(local, parent)
