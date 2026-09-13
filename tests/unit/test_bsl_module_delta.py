from dataclasses import replace
import gc
import random

import pytest

import onec_runtime.bsl.module_universe as module_universe
import onec_runtime.bsl.module_delta as module_delta
import onec_runtime.bsl.source_maps as source_maps
import onec_runtime.worker_epf as worker_epf
from onec_runtime.errors import ModuleUniverseAdmissionError
from onec_runtime.performance_profile import PhaseRecorder
from onec_runtime.bsl.module_catalog import (
    CommonModuleCatalogSnapshot,
    CommonModuleDescriptor,
    CommonModuleScope,
)
from onec_runtime.bsl.module_delta import (
    WorkerSemanticDeltaEvidence,
    WorkerSemanticSnapshot,
    build_full_worker_semantic_snapshot,
    select_worker_semantic_snapshot_base,
    try_build_worker_semantic_delta,
)
from onec_runtime.bsl.lexer import BslLexError
from onec_runtime.bsl.module_universe import (
    LoweredWorkerModule,
    WorkerModuleUnit,
    dependency_export_name,
    worker_semantic_admission,
)
from onec_runtime.bsl.parser_target import (
    BslParseError,
    GeneratedParserMetadata,
    PythonParserTarget,
)
from onec_runtime.bsl.source_maps import (
    MappedSource,
    MappingRelation,
    SourceArtifactKind,
    SourceArtifactRef,
    SourceMap,
    SourceMapSegment,
    SourceSpan,
    SourceTransformBuilder,
    SourceUnitKind,
    SourceUnitRef,
    mapped_visible_source,
    source_sha256,
)


def _catalog(
    revision: int = 5,
    *names: str,
    profile: str = "server-zup",
    preprocessor_profile: str = "server",
) -> CommonModuleCatalogSnapshot:
    return CommonModuleCatalogSnapshot.create(
        profile=profile,
        preprocessor_profile=preprocessor_profile,
        revision=revision,
        modules=tuple(
            CommonModuleDescriptor(name, CommonModuleScope.SERVER)
            for name in names
        ),
    )


def _unit(
    logical_name: str,
    revision: int,
    *,
    kind: str = "module",
    value: str = "one",
) -> WorkerModuleUnit:
    source = (
        "Функция Версия() Экспорт\n"
        f'    Возврат "{value}";\n'
        "КонецФункции\n"
    )
    source_kind = (
        SourceUnitKind.MODULE
        if kind == "module"
        else SourceUnitKind.TEST_MODULE
    )
    reference = SourceUnitRef(
        source_kind,
        logical_name,
        revision,
        source_sha256(source),
    )
    return WorkerModuleUnit(
        logical_name,
        kind,  # type: ignore[arg-type]
        revision,
        mapped_visible_source(source, reference),
    )


def _unit_from_source(
    source: str,
    revision: int,
    *,
    logical_name: str = "МодульА",
) -> WorkerModuleUnit:
    reference = SourceUnitRef(
        SourceUnitKind.MODULE,
        logical_name,
        revision,
        source_sha256(source),
    )
    return WorkerModuleUnit(
        logical_name,
        "module",
        revision,
        mapped_visible_source(source, reference),
    )


def _assert_semantically_equal(
    actual: WorkerSemanticSnapshot,
    expected: WorkerSemanticSnapshot,
) -> None:
    assert actual.analysis.dependencies == expected.analysis.dependencies
    assert actual.analysis.exported_methods == expected.analysis.exported_methods
    assert actual.analysis.method_insertion_points == expected.analysis.method_insertion_points
    assert actual.analysis.context == expected.analysis.context
    assert actual.analysis.methods == expected.analysis.methods
    assert actual.analysis.catalog_identity == expected.analysis.catalog_identity
    assert actual.analysis.parser_identity == expected.analysis.parser_identity
    assert actual.lowered.mapped_source.text == expected.lowered.mapped_source.text
    assert (
        actual.lowered.mapped_source.source_map.to_manifest()
        == expected.lowered.mapped_source.source_map.to_manifest()
    )
    assert (
        actual.lowered.mapped_source.lineage_manifest()
        == expected.lowered.mapped_source.lineage_manifest()
    )
    assert (
        actual.lowered.mapped_source.source_map_sha256
        == expected.lowered.mapped_source.source_map_sha256
    )
    assert (
        actual.lowered.dependency_bindings_sha256
        == expected.lowered.dependency_bindings_sha256
    )
    actual_admission = worker_semantic_admission(
        actual.lowered,
        packer_identity="worker-epf-v1",
    )
    expected_admission = worker_semantic_admission(
        expected.lowered,
        packer_identity="worker-epf-v1",
    )
    actual_identity = actual_admission._contents()[1]
    expected_identity = expected_admission._contents()[1]
    for field_name in (
        "logical_name",
        "raw_source_sha256",
        "raw_text_sha256",
        "lowered_source_sha256",
        "lowered_text_sha256",
        "lowered_source_map_sha256",
        "lineage_sha256",
        "dependency_bindings_sha256",
        "canonical_exported_methods",
        "catalog_identity",
        "parser_identity",
        "transform_version",
        "packer_identity",
    ):
        assert getattr(actual_identity, field_name) == getattr(
            expected_identity,
            field_name,
        )


def _snapshot() -> tuple[
    WorkerSemanticSnapshot,
    CommonModuleCatalogSnapshot,
    PythonParserTarget,
]:
    catalog = _catalog(5, "МодульА")
    parser_target = PythonParserTarget.from_generated()
    snapshot = build_full_worker_semantic_snapshot(
        _unit("МодульА", 10),
        catalog,
        parser_target,
    )
    return snapshot, catalog, parser_target


def test_isolated_literal_edit_matches_independent_full_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: a local edit must parse one method, not the full module."""
    catalog = _catalog(5, "МодульА")
    parser_target = PythonParserTarget.from_generated()
    previous = build_full_worker_semantic_snapshot(
        _unit("МодульА", 10, value="one"),
        catalog,
        parser_target,
    )
    candidate = _unit("МодульА", 11, value="два")
    parse_sources: list[str] = []
    original_parse = module_universe.parse_raw_module

    def record_parse(source: str, target: PythonParserTarget):
        parse_sources.append(source)
        return original_parse(source, target)

    monkeypatch.setattr(module_universe, "parse_raw_module", record_parse)

    result = try_build_worker_semantic_delta(
        previous,
        candidate,
        catalog,
        parser_target,
    )
    monkeypatch.setattr(module_universe, "parse_raw_module", original_parse)
    independent = build_full_worker_semantic_snapshot(
        candidate,
        catalog,
        PythonParserTarget.from_generated(),
    )

    assert result is not None
    assert isinstance(result.delta_evidence, WorkerSemanticDeltaEvidence)
    assert parse_sources == [candidate.mapped_source.text.rstrip("\n")]
    _assert_semantically_equal(result, independent)


@pytest.mark.parametrize(
    ("line_ending", "old_first", "new_first", "old_second", "new_second"),
    (
        ("\n", 'Возврат "old";', 'Возврат "новое🙂";', "Возврат 2;", "Возврат 2;"),
        ("\r\n", "Возврат 1;", "Возврат 100000;", "Возврат 2;", "Возврат 2;"),
        ("\n", "Возврат 1;", "Результат = 2;\n    Возврат Результат;", "Возврат 2;", "Возврат 2;"),
        ("\n", "Возврат 1;", "Возврат МодульБ.Получить();", "Возврат 2;", "Возврат 2;"),
        ("\n", "Возврат МодульБ.Получить();", "Возврат 1;", "Возврат 2;", "Возврат 2;"),
        ("\n", "Возврат 1;", "Возврат 1;", "Возврат 2;", "Сообщить(2);\n    Возврат 3;"),
    ),
)
def test_supported_method_edits_match_full_and_shift_following_spans(
    line_ending: str,
    old_first: str,
    new_first: str,
    old_second: str,
    new_second: str,
) -> None:
    """Break caught: supported statements/dependencies must merge byte-exactly."""
    def source(first: str, second: str) -> str:
        text = (
            "Функция Первый() Экспорт\n"
            f"    {first}\n"
            "КонецФункции\n"
            "Функция Второй()\n"
            f"    {second}\n"
            "КонецФункции\n"
        )
        return text.replace("\n", line_ending)

    catalog = _catalog(5, "МодульА", "МодульБ")
    target = PythonParserTarget.from_generated()
    old_source = source(old_first, old_second)
    new_source = source(new_first, new_second)
    previous = build_full_worker_semantic_snapshot(
        _unit_from_source(old_source, 10),
        catalog,
        target,
    )
    candidate = _unit_from_source(new_source, 11)

    result = try_build_worker_semantic_delta(
        previous,
        candidate,
        catalog,
        target,
    )
    expected = build_full_worker_semantic_snapshot(
        candidate,
        catalog,
        PythonParserTarget.from_generated(),
    )

    assert result is not None
    _assert_semantically_equal(result, expected)
    assert result.delta_evidence is not None
    assert result.delta_evidence.length_delta == len(new_source) - len(old_source)


@pytest.mark.parametrize("line_ending", ("\n", "\r\n"))
def test_delta_lowering_segment_reuse_materializes_only_one_final_two_megabyte_text(
    monkeypatch: pytest.MonkeyPatch,
    line_ending: str,
) -> None:
    """Break caught: retained fragments must avoid duplicate near-full text copies."""
    prefix = "//" + "p" * 999_700 + "\n"
    suffix = "//" + "s" * 999_700 + "\n"
    old_source = (
        prefix
        + "Функция Первый()\n"
        + "    Возврат МодульБ.Получить();\n"
        + "КонецФункции\n"
        + "Функция Средний() Экспорт\n"
        + '    Возврат "old";\n'
        + "КонецФункции\n"
        + suffix
        + "Функция Последний()\n"
        + "    Возврат МодульВ.Получить();\n"
        + "КонецФункции\n"
    ).replace("\n", line_ending)
    new_source = old_source.replace('"old"', '"new-value"')
    catalog = _catalog(5, "МодульА", "МодульБ", "МодульВ")
    target = PythonParserTarget.from_generated()
    previous = build_full_worker_semantic_snapshot(
        _unit_from_source(old_source, 10),
        catalog,
        target,
    )
    candidate = _unit_from_source(new_source, 11)
    merged = try_build_worker_semantic_delta(previous, candidate, catalog, target)
    assert merged is not None
    assert merged.delta_evidence is not None
    work: list[tuple[str, int]] = []
    monkeypatch.setattr(
        source_maps,
        "_TEXT_WORK_OBSERVER",
        lambda event, width: work.append((event, width)),
        raising=False,
    )
    lowered = module_delta.lower_worker_module_delta(
        previous,
        merged.analysis,
        merged.delta_evidence,
    )

    assert lowered is not None
    actual_slices = [width for event, width in work if event == "slice"]
    assert actual_slices
    assert max(actual_slices) < 100_000
    assert sum(actual_slices) < 200_000
    materializations = [width for event, width in work if event == "materialize"]
    projection_hashes = [width for event, width in work if event == "projection_hash"]
    final_hashes = [width for event, width in work if event == "final_hash"]
    projection = lowered.mapped_source.transform_parent
    assert projection is not None
    assert materializations == [len(lowered.mapped_source.text)]
    assert projection_hashes == [projection.artifact.character_length]
    assert final_hashes == [len(lowered.mapped_source.text)]
    assert not [width for event, width in work if event == "lineage_hash"]
    assert any(event == "retained_fragment" for event, _width in work)
    assert sum(width for event, width in work if event == "segment_allocate") < 100
    assert 0 < sum(
        width for event, width in work if event == "retained_metadata"
    ) < 10_000
    assert sum(width for event, width in work if event == "fragment_allocate") < 250
    full_text_work = [
        (event, width)
        for event, width in work
        if width >= len(lowered.mapped_source.text) // 2
        and event not in {"retained_fragment"}
    ]
    assert full_text_work == [
        ("projection_hash", projection.artifact.character_length),
        ("materialize", len(lowered.mapped_source.text)),
        ("final_hash", len(lowered.mapped_source.text)),
    ]


def test_delta_admission_and_mapped_packaging_have_one_accounted_authority_pass(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: admission and packaging must not rebuild 2 MB authorities."""
    prefix = "//" + "p" * 999_700 + "\n"
    suffix = "//" + "s" * 999_700 + "\n"
    old_source = (
        prefix
        + "Функция Первый()\n"
        + "    Возврат МодульБ.Получить();\n"
        + "КонецФункции\n"
        + "Функция Средний() Экспорт\n"
        + '    Возврат "old";\n'
        + "КонецФункции\n"
        + suffix
        + "Функция Последний()\n"
        + "    Возврат МодульВ.Получить();\n"
        + "КонецФункции\n"
    )
    new_source = old_source.replace('"old"', '"new-value"')
    catalog = _catalog(5, "МодульА", "МодульБ", "МодульВ")
    target = PythonParserTarget.from_generated()
    previous = build_full_worker_semantic_snapshot(
        _unit_from_source(old_source, 10),
        catalog,
        target,
    )
    work: list[tuple[str, int]] = []
    monkeypatch.setattr(
        source_maps,
        "_TEXT_WORK_OBSERVER",
        lambda event, width: work.append((event, width)),
        raising=False,
    )
    candidate = _unit_from_source(new_source, 11)
    manifest_calls: dict[int, int] = {}
    retained_repr_calls = 0
    normalization_calls = 0
    original_manifest = SourceMap.to_manifest
    original_repr = repr
    original_normalize = worker_epf._normalize_source

    def track_manifest(source_map: SourceMap) -> dict[str, object]:
        manifest_calls[id(source_map)] = manifest_calls.get(id(source_map), 0) + 1
        return original_manifest(source_map)

    def track_repr(value: object) -> str:
        nonlocal retained_repr_calls
        retained_repr_calls += 1
        return original_repr(value)

    def track_normalize(value: str) -> str:
        nonlocal normalization_calls
        normalization_calls += 1
        return original_normalize(value)

    monkeypatch.setattr(SourceMap, "to_manifest", track_manifest)
    monkeypatch.setattr(source_maps, "repr", track_repr, raising=False)
    monkeypatch.setattr(worker_epf, "_normalize_source", track_normalize)
    result = try_build_worker_semantic_delta(
        previous,
        candidate,
        catalog,
        target,
    )
    assert result is not None
    assert result.delta_evidence is not None
    projection = result.lowered.mapped_source.transform_parent
    assert projection is not None
    worker_semantic_admission(
        result.lowered,
        packer_identity="worker-epf-v1",
    )
    worker_epf.build_worker_epf(
        result.lowered.mapped_source,
        tmp_path / "Worker-delta.epf",
    )

    assert manifest_calls
    assert max(manifest_calls.values()) == 1
    map_segment_visits = [
        width for event, width in work if event == "map_manifest_segment_visit"
    ]
    map_allocations = [
        width for event, width in work if event == "map_manifest_allocation"
    ]
    map_serializations = [
        width for event, width in work if event == "map_manifest_serialized_bytes"
    ]
    map_hashes = [width for event, width in work if event == "map_manifest_hash"]
    assert map_segment_visits and sum(map_segment_visits) < 1_000
    assert map_allocations and sum(map_allocations) > sum(map_segment_visits)
    assert map_serializations == map_hashes
    # One canonical map is constructed with the raw candidate before delta
    # admission; the remaining serializations correspond to observed manifests.
    assert len(map_serializations) == len(manifest_calls) + 1
    lineage_serializations = [
        width for event, width in work if event == "semantic_lineage_serialized_bytes"
    ]
    lineage_hashes = [
        width for event, width in work if event == "semantic_lineage_hash"
    ]
    assert lineage_serializations == lineage_hashes
    assert retained_repr_calls == 0
    assert normalization_calls == 0
    assert [width for event, width in work if event == "semantic_summary_reuse"] == [
        1,
        1,
    ]
    structural_visits = [
        width for event, width in work if event == "semantic_structural_segment_visit"
    ]
    assert structural_visits and sum(structural_visits) < 1_000
    retained_proof_bytes = [
        width for event, width in work if event == "retained_proof_bytes"
    ]
    assert retained_proof_bytes and max(retained_proof_bytes) < 100_000
    semantic_proof_bytes = [
        width for event, width in work if event == "semantic_admission_proof_bytes"
    ]
    assert semantic_proof_bytes and max(semantic_proof_bytes) < 100_000
    assert [width for event, width in work if event == "packaging_normalized_authority"] == [
        len(result.lowered.mapped_source.text)
    ]
    assert not [width for event, width in work if event == "packaging_normalize"]
    assert not [width for event, width in work if event == "packaging_hash"]
    policy_visits = [
        width for event, width in work if event == "worker_policy_fragment_visit"
    ]
    assert policy_visits and sum(policy_visits) < 250
    nul_scans = [width for event, width in work if event == "fragment_nul_scan"]
    assert nul_scans == [63, 63]
    assert [width for event, width in work if event == "normalized_worker_authority"] == [
        1
    ]
    full_size = len(result.lowered.mapped_source.text) // 2
    assert [
        (event, width)
        for event, width in work
        if width >= full_size
        and event
        not in {
            "retained_fragment",
            "packaging_normalized_authority",
            "packaging_verify",
        }
    ] == [
        ("projection_hash", projection.artifact.character_length),
        ("materialize", len(result.lowered.mapped_source.text)),
        ("final_hash", len(result.lowered.mapped_source.text)),
        ("packaging_encode", len(result.lowered.mapped_source.text)),
    ]


def test_delta_nul_error_matches_full_before_admission_or_packaging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: delta must not certify NUL text rejected by full lowering."""
    old_source = (
        "Функция Версия() Экспорт\n"
        '    Возврат "old";\n'
        "КонецФункции\n"
    )
    new_source = old_source.replace('"old"', '"new\x00value"')
    catalog = _catalog(5, "МодульА")
    target = PythonParserTarget.from_generated()
    previous = build_full_worker_semantic_snapshot(
        _unit_from_source(old_source, 10),
        catalog,
        target,
    )
    candidate = _unit_from_source(new_source, 11)
    with pytest.raises(ValueError) as full_failure:
        build_full_worker_semantic_snapshot(
            candidate,
            catalog,
            PythonParserTarget.from_generated(),
        )
    unexpected_calls: list[str] = []

    def unexpected_admission(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        unexpected_calls.append("admission")
        raise AssertionError("NUL delta reached admission")

    def unexpected_packaging(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        unexpected_calls.append("packaging")
        raise AssertionError("NUL delta reached packaging")

    work: list[tuple[str, int]] = []
    monkeypatch.setattr(
        module_delta,
        "_mint_worker_semantic_admission",
        unexpected_admission,
    )
    monkeypatch.setattr(worker_epf, "build_worker_epf", unexpected_packaging)
    monkeypatch.setattr(
        source_maps,
        "_TEXT_WORK_OBSERVER",
        lambda event, width: work.append((event, width)),
        raising=False,
    )

    with pytest.raises(type(full_failure.value)) as delta_failure:
        try_build_worker_semantic_delta(
            previous,
            candidate,
            catalog,
            target,
        )

    assert str(delta_failure.value) == str(full_failure.value)
    assert str(delta_failure.value) == "Worker source contains a NUL character"
    assert unexpected_calls == []
    assert not [event for event, _width in work if event == "normalized_worker_authority"]


def test_semantic_authority_proofs_encode_unbounded_nonnegative_revisions_uniquely() -> None:
    """Break caught: valid revisions above uint64 must not overflow or alias."""
    values = (0, 2**64 - 1, 2**64, 2**130 + 17)
    structural_proofs: list[bytes] = []
    generation_proofs: list[bytes] = []
    admission_proofs: list[bytes] = []
    for value in values:
        artifact = SourceArtifactRef(
            SourceArtifactKind.WORKER_MODULE,
            "a" * 64,
            0,
            "none",
            worker_generation=value,
        )
        generated_map = SourceMap(
            artifact,
            (
                SourceMapSegment(
                    SourceSpan(0, 0),
                    None,
                    None,
                    MappingRelation.SYNTHETIC,
                    "empty-worker",
                ),
            ),
        )
        generation_proofs.append(generated_map._structural_proof)
        catalog = _catalog(value, "МодульА")
        snapshot = build_full_worker_semantic_snapshot(
            _unit("МодульА", value),
            catalog,
            PythonParserTarget.from_generated(),
        )
        structural_proofs.append(snapshot.analysis.unit.mapped_source.source_map._structural_proof)
        admission = worker_semantic_admission(
            snapshot.lowered,
            packer_identity="worker-epf-v1",
        )
        identity = admission._contents()[1]
        admission_proofs.append(
            module_universe._worker_semantic_admission_proof(identity)
        )

    assert len(set(structural_proofs)) == len(values)
    assert len(set(generation_proofs)) == len(values)
    assert len(set(admission_proofs)) == len(values)


def test_delta_lowering_crlf_map_lookup_scales_linearly_and_stays_exact() -> None:
    """Break caught: per-line full segment scans make CRLF lowering quadratic."""
    inspections: list[int] = []

    class CountingSegments(tuple):
        def __iter__(self):  # type: ignore[no-untyped-def]
            for segment in super().__iter__():
                inspections[-1] += 1
                yield segment

    for method_count in (30, 60, 120):
        def source(changed: bool) -> str:
            methods = []
            for index in range(method_count):
                value = "changed-value" if changed and index == method_count // 2 else "old"
                methods.append(
                    f"Функция Метод{index:03d}()\n"
                    f'    Значение = "{value}";\n'
                    "    Возврат МодульБ.Получить(Значение);\n"
                    "КонецФункции\n"
                )
            return "".join(methods).replace("\n", "\r\n")

        catalog = _catalog(5, "МодульА", "МодульБ")
        target = PythonParserTarget.from_generated()
        previous = build_full_worker_semantic_snapshot(
            _unit_from_source(source(False), 10),
            catalog,
            target,
        )
        candidate = _unit_from_source(source(True), 11)
        merged = try_build_worker_semantic_delta(previous, candidate, catalog, target)
        assert merged is not None
        assert merged.delta_evidence is not None
        projection = previous.lowered.mapped_source.transform_parent
        assert projection is not None
        inspections.append(0)
        object.__setattr__(
            projection.local_source_map,
            "segments",
            CountingSegments(projection.local_source_map.segments),
        )
        object.__setattr__(
            previous.lowered.mapped_source.local_source_map,
            "segments",
            CountingSegments(previous.lowered.mapped_source.local_source_map.segments),
        )

        lowered = module_delta.lower_worker_module_delta(
            previous,
            merged.analysis,
            merged.delta_evidence,
        )
        independent = build_full_worker_semantic_snapshot(
            candidate,
            catalog,
            PythonParserTarget.from_generated(),
        )

        assert lowered is not None
        _assert_semantically_equal(replace(merged, lowered=lowered), independent)

    assert inspections[1] <= inspections[0] * 2.5
    assert inspections[2] <= inspections[1] * 2.5


def test_one_hundred_sequential_deltas_retain_only_current_generation_after_gc() -> None:
    """Break caught: retained fragments must not form an unbounded revision chain."""
    def source(revision: int) -> str:
        return (
            "Функция Первый()\n"
            "    Возврат МодульБ.Получить();\n"
            "КонецФункции\n"
            "Функция Средний() Экспорт\n"
            f'    Возврат "revision-{revision:03d}";\n'
            "КонецФункции\n"
            "Функция Последний()\n"
            "    Возврат МодульВ.Получить();\n"
            "КонецФункции\n"
        )

    catalog = _catalog(5, "МодульА", "МодульБ", "МодульВ")
    target = PythonParserTarget.from_generated()
    snapshot = build_full_worker_semantic_snapshot(
        _unit_from_source(source(0), 10),
        catalog,
        target,
    )
    for revision in range(1, 101):
        result = try_build_worker_semantic_delta(
            snapshot,
            _unit_from_source(source(revision), 10 + revision),
            catalog,
            target,
        )
        assert result is not None
        snapshot = result

    gc.collect()
    pending = [snapshot.lowered.mapped_source, snapshot.analysis.unit.mapped_source]
    retained_objects: set[int] = set()
    full_text_objects: set[int] = set()
    full_text_threshold = len(snapshot.analysis.unit.mapped_source.text) // 2
    while pending:
        current = pending.pop()
        if id(current) in retained_objects:
            continue
        retained_objects.add(id(current))
        current_text = getattr(current, "text", None)
        if isinstance(current_text, str) and len(current_text) >= full_text_threshold:
            full_text_objects.add(id(current_text))
        parent = getattr(current, "transform_parent", None)
        if parent is not None:
            pending.append(parent)
        fragments = getattr(
            current,
            "_retained_fragments",
            getattr(current, "retained_fragments", ()),
        )
        pending.extend(
            fragment.retained_source
            for fragment in fragments
            if fragment.retained_source is not None
        )
        full_text_objects.update(
            id(fragment.text)
            for fragment in fragments
            if len(fragment.text) >= full_text_threshold
        )

    assert len(retained_objects) <= 4
    assert len(full_text_objects) <= 3


@pytest.mark.parametrize(
    "corruption",
    ("stale-fragment", "source-swap", "source-and-proof-swap"),
)
def test_delta_lowering_rejects_untrusted_retained_fragment_before_admission(
    corruption: str,
) -> None:
    """Break caught: same-length retained text corruption must fail closed."""
    def source(value: str) -> str:
        return (
            "Функция Первый()\n"
            "    Возврат МодульБ.Получить();\n"
            "КонецФункции\n"
            "Функция Средний() Экспорт\n"
            f'    Возврат "{value}";\n'
            "КонецФункции\n"
        )

    catalog = _catalog(5, "МодульА", "МодульБ")
    target = PythonParserTarget.from_generated()
    previous = build_full_worker_semantic_snapshot(
        _unit_from_source(source("old-value"), 10),
        catalog,
        target,
    )
    candidate = _unit_from_source(source("new-value-longer"), 11)
    merged = try_build_worker_semantic_delta(previous, candidate, catalog, target)
    assert merged is not None
    assert merged.delta_evidence is not None
    projection = previous.lowered.mapped_source.transform_parent
    assert projection is not None
    other_projection = None

    if corruption == "stale-fragment":
        fragments = list(projection._retained_fragments)
        index = max(range(len(fragments)), key=lambda item: len(fragments[item].text))
        stale = fragments[index]
        replacement = ("X" if stale.text[:1] != "X" else "Y") + stale.text[1:]
        fragments[index] = replace(stale, text=replacement)
    else:
        other = build_full_worker_semantic_snapshot(
            _unit_from_source(source("bad-value"), 12),
            catalog,
            PythonParserTarget.from_generated(),
        )
        other_projection = other.lowered.mapped_source.transform_parent
        assert other_projection is not None
        fragments = list(other_projection._retained_fragments)
    object.__setattr__(projection, "_retained_fragments", tuple(fragments))
    if corruption == "source-and-proof-swap":
        assert other_projection is not None
        object.__setattr__(projection, "_retained_proof", other_projection._retained_proof)

    assert module_delta.lower_worker_module_delta(
        previous,
        merged.analysis,
        merged.delta_evidence,
    ) is None


def test_mapped_source_rejects_forged_trusted_digest_for_changed_same_length_text() -> None:
    """Break caught: callers cannot bypass content validation with a digest scalar."""
    snapshot, _catalog_value, _target = _snapshot()
    mapped = snapshot.lowered.mapped_source
    forged_text = ("X" if mapped.text[:1] != "X" else "Y") + mapped.text[1:]

    with pytest.raises(ValueError, match="hash|proof|authority"):
        MappedSource(
            forged_text,
            mapped.artifact,
            mapped.source_map,
            mapped.lineage,
            mapped.local_source_map,
            _trusted_source_sha256=mapped.artifact.source_sha256,
        )


@pytest.mark.parametrize(
    ("line_ending", "old_statement", "new_statement"),
    (
        ("\n", "Возврат 1;", "Возврат 100000;"),
        ("\r\n", "Возврат 1;", "Возврат 100000;"),
        ("\n", "Возврат 1;", "Возврат МодульБ.Получить();"),
        ("\n", "Возврат МодульБ.Получить();", "Возврат 1;"),
    ),
)
def test_delta_lowering_is_exactly_identical_to_independent_full_lowering(
    line_ending: str,
    old_statement: str,
    new_statement: str,
) -> None:
    """Break caught: segment reuse must preserve every lowering/map identity."""
    def source(statement: str) -> str:
        return (
            "Функция Первый() Экспорт\n"
            f"    {statement}\n"
            "КонецФункции\n"
            "Функция Второй()\n"
            "    Возврат МодульВ.Получить();\n"
            "КонецФункции\n"
        ).replace("\n", line_ending)

    catalog = _catalog(5, "МодульА", "МодульБ", "МодульВ")
    target = PythonParserTarget.from_generated()
    previous = build_full_worker_semantic_snapshot(
        _unit_from_source(source(old_statement), 10),
        catalog,
        target,
    )
    candidate = _unit_from_source(source(new_statement), 11)
    merged = try_build_worker_semantic_delta(previous, candidate, catalog, target)
    assert merged is not None
    assert merged.delta_evidence is not None

    delta_lowered = module_delta.lower_worker_module_delta(
        previous,
        merged.analysis,
        merged.delta_evidence,
    )
    independent = build_full_worker_semantic_snapshot(
        candidate,
        catalog,
        PythonParserTarget.from_generated(),
    )

    assert delta_lowered is not None
    actual = replace(merged, lowered=delta_lowered)
    _assert_semantically_equal(actual, independent)


@pytest.mark.parametrize("corruption", ("lineage", "exact-hunk", "generated-prefix"))
def test_delta_lowering_ambiguous_previous_segments_request_full_lowering(
    corruption: str,
) -> None:
    """Break caught: uncertain previous map structure must fail closed to full lowering."""
    source = (
        "Функция Версия() Экспорт\n"
        "    Возврат МодульБ.Получить();\n"
        "КонецФункции\n"
    )
    catalog = _catalog(5, "МодульА", "МодульБ")
    target = PythonParserTarget.from_generated()
    previous = build_full_worker_semantic_snapshot(
        _unit_from_source(source, 10),
        catalog,
        target,
    )
    candidate = _unit_from_source(source.replace("Получить", "Рассчитать"), 11)
    merged = try_build_worker_semantic_delta(previous, candidate, catalog, target)
    assert merged is not None
    assert merged.delta_evidence is not None

    mapped = previous.lowered.mapped_source
    if corruption == "lineage":
        corrupted_mapped = MappedSource(
            mapped.text,
            mapped.artifact,
            mapped.source_map,
            (mapped.local_source_map,),
            mapped.local_source_map,
        )
    else:
        projection = mapped.lineage[-2]
        segments = list(projection.segments)
        if corruption == "exact-hunk":
            index = next(
                index
                for index, segment in enumerate(segments)
                if segment.relation is MappingRelation.EXACT
                and segment.origin is not None
                and segment.origin.start <= merged.delta_evidence.old_span.start
                and merged.delta_evidence.old_span.end <= segment.origin.end
            )
            segment = segments[index]
            segments[index] = SourceMapSegment(
                segment.generated,
                segment.origin_ref,
                segment.origin,
                MappingRelation.DERIVED,
                "ambiguous_previous_exact_segment",
                segment.anchor_ref,
                segment.anchor_span,
            )
        else:
            index = next(
                index
                for index, segment in enumerate(segments)
                if segment.synthetic_region == "worker_dependency_field"
            )
            segment = segments[index]
            segments[index] = SourceMapSegment(
                segment.generated,
                segment.origin_ref,
                segment.origin,
                segment.relation,
                "ambiguous_generated_prefix",
                segment.anchor_ref,
                segment.anchor_span,
            )
        corrupted_projection = SourceMap(projection.generated, tuple(segments))
        corrupted_mapped = MappedSource(
            mapped.text,
            mapped.artifact,
            mapped.source_map,
            (*mapped.lineage[:-2], corrupted_projection, mapped.local_source_map),
            mapped.local_source_map,
        )
    corrupted_previous = replace(
        previous,
        lowered=LoweredWorkerModule(
            previous.analysis,
            corrupted_mapped,
            previous.lowered.dependency_bindings_sha256,
        ),
    )

    assert (
        module_delta.lower_worker_module_delta(
            corrupted_previous,
            merged.analysis,
            merged.delta_evidence,
        )
        is None
    )


@pytest.mark.parametrize(
    "mutation",
    (
        "signature",
        "export",
        "parameters",
        "locals",
        "module_variables",
        "module_statement",
        "two_methods",
        "directive",
        "method_add",
        "method_delete",
        "catalog",
        "parser",
        "transform",
        "repeated_diff",
    ),
)
def test_unsupported_or_ambiguous_edits_request_one_full_fallback(
    mutation: str,
) -> None:
    """Break caught: structural uncertainty must return None before admission."""
    old_source = (
        "Перем Модульная;\n"
        "Функция Первый(Значение) Экспорт\n"
        "    Перем Локальная;\n"
        '    Локальная = "aaa";\n'
        "    Возврат Локальная;\n"
        "КонецФункции\n"
        "Функция Второй()\n"
        "    Возврат 2;\n"
        "КонецФункции\n"
    )
    new_source = old_source.replace('"aaa"', '"bbb"')
    catalog = _catalog(5, "МодульА")
    candidate_catalog = catalog
    parser = PythonParserTarget.from_generated()
    candidate_parser = parser
    previous = build_full_worker_semantic_snapshot(
        _unit_from_source(old_source, 10),
        catalog,
        parser,
    )

    if mutation == "signature":
        new_source = old_source.replace("Функция Первый", "Процедура Первый").replace(
            "КонецФункции\nФункция Второй", "КонецПроцедуры\nФункция Второй", 1
        )
    elif mutation == "export":
        new_source = old_source.replace(") Экспорт", ")", 1)
    elif mutation == "parameters":
        new_source = old_source.replace("(Значение)", "(Значение, Еще)", 1)
    elif mutation == "locals":
        new_source = old_source.replace("Перем Локальная;", "Перем Локальная, Еще;")
    elif mutation == "module_variables":
        new_source = old_source.replace("Перем Модульная;", "Перем Модульная, Еще;")
    elif mutation == "module_statement":
        new_source = "Сообщить(1);\n" + old_source
    elif mutation == "two_methods":
        new_source = old_source.replace('"aaa"', '"bbb"').replace(
            "Возврат 2;", "Возврат 3;"
        )
    elif mutation == "directive":
        old_source = old_source.replace(
            '    Локальная = "aaa";\n',
            '#Если Сервер Тогда\n    Локальная = "aaa";\n#КонецЕсли\n',
        )
        new_source = old_source.replace("#Если Сервер Тогда", "#Если Клиент Тогда")
        previous = build_full_worker_semantic_snapshot(
            _unit_from_source(old_source, 10),
            catalog,
            parser,
        )
    elif mutation == "method_add":
        new_source = old_source + "Функция Третий()\nВозврат 3;\nКонецФункции\n"
    elif mutation == "method_delete":
        new_source = old_source[: old_source.index("Функция Второй")]
    elif mutation == "catalog":
        candidate_catalog = _catalog(6, "МодульА", "МодульБ")
    elif mutation == "parser":
        candidate_parser = PythonParserTarget(
            type(parser.generated_parser),
            parser.generated_error,
            GeneratedParserMetadata("f" * 64, "e" * 64),
        )
    elif mutation == "transform":
        previous = WorkerSemanticSnapshot(
            previous.analysis,
            replace(previous.lowered, transform_version="module-universe-v2"),
            previous.catalog_identity,
            previous.parser_identity,
        )
    elif mutation == "repeated_diff":
        new_source = old_source.replace('"aaa"', '"aaaa"')

    assert try_build_worker_semantic_delta(
        previous,
        _unit_from_source(new_source, 11),
        candidate_catalog,
        candidate_parser,
    ) is None


@pytest.mark.parametrize(
    "replacement",
    (
        "Возврат (",
        'Выполнить("ВнутреннийМетод()");',
        "Неизвестный.Получить();",
        "МодульБ();",
    ),
)
def test_admitted_method_errors_are_exact_and_never_masked_by_fallback(
    replacement: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: a proven local error must escape after one method parse."""
    old_source = (
        "Перем Модульная;\n"
        "Функция Первый() Экспорт\n"
        "    Возврат 1;\n"
        "КонецФункции\n"
    )
    new_source = old_source.replace("Возврат 1;", replacement)
    catalog = _catalog(5, "МодульА", "МодульБ")
    parser = PythonParserTarget.from_generated()
    previous = build_full_worker_semantic_snapshot(
        _unit_from_source(old_source, 10),
        catalog,
        parser,
    )
    calls: list[str] = []
    original = module_universe.parse_raw_module

    def record(source: str, target: PythonParserTarget):
        calls.append(source)
        return original(source, target)

    monkeypatch.setattr(module_universe, "parse_raw_module", record)

    with pytest.raises((BslParseError, ModuleUniverseAdmissionError)) as caught:
        try_build_worker_semantic_delta(
            previous,
            _unit_from_source(new_source, 11),
            catalog,
            parser,
        )
    monkeypatch.setattr(module_universe, "parse_raw_module", original)
    with pytest.raises(type(caught.value)) as full_caught:
        build_full_worker_semantic_snapshot(
            _unit_from_source(new_source, 11),
            catalog,
            PythonParserTarget.from_generated(),
        )

    assert len(calls) == 1
    assert calls[0] != new_source
    assert str(caught.value) == str(full_caught.value)
    assert caught.value.code == full_caught.value.code
    assert caught.value.span == full_caught.value.span
    if replacement.startswith("Выполнить"):
        assert caught.value.code == "dynamic_execute"
    elif replacement.startswith(("Неизвестный", "МодульБ")):
        assert caught.value.code == "ambiguous_module_dependency"
    else:
        assert caught.value.code == "unexpected_token"


@pytest.mark.parametrize(
    "replacement",
    (
        "Возврат @;",
        'Возврат "незавершенная строка',
    ),
)
def test_admitted_lexical_errors_match_full_module_coordinates(
    replacement: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: isolated lexer diagnostics must use module coordinates."""
    old_source = (
        "Перем Модульная;\n"
        "Функция Первый() Экспорт\n"
        "    Возврат 1;\n"
        "КонецФункции\n"
        "Функция Второй()\n"
        "    Возврат 2;\n"
        "КонецФункции\n"
    )
    new_source = old_source.replace("Возврат 1;", replacement)
    catalog = _catalog(5, "МодульА")
    parser = PythonParserTarget.from_generated()
    previous = build_full_worker_semantic_snapshot(
        _unit_from_source(old_source, 10),
        catalog,
        parser,
    )
    calls: list[str] = []
    original = module_universe.parse_raw_module

    def record(source: str, target: PythonParserTarget):
        calls.append(source)
        return original(source, target)

    monkeypatch.setattr(module_universe, "parse_raw_module", record)
    with pytest.raises(BslLexError) as caught:
        try_build_worker_semantic_delta(
            previous,
            _unit_from_source(new_source, 11),
            catalog,
            parser,
        )
    monkeypatch.setattr(module_universe, "parse_raw_module", original)
    with pytest.raises(BslLexError) as full_caught:
        build_full_worker_semantic_snapshot(
            _unit_from_source(new_source, 11),
            catalog,
            PythonParserTarget.from_generated(),
        )

    assert len(calls) == 1
    assert calls[0] != new_source
    assert str(caught.value) == str(full_caught.value)
    assert caught.value.code == full_caught.value.code
    assert caught.value.span == full_caught.value.span


@pytest.mark.parametrize("line_ending", ("\n", "\r\n"))
@pytest.mark.parametrize(
    ("replacement", "later_statement"),
    (
        ('Возврат "незавершенная строка', 'Возврат "later";'),
        ("Возврат '20260901", "Возврат '20260101';"),
    ),
)
def test_cross_method_lexical_state_uses_canonical_full_candidate_error(
    line_ending: str,
    replacement: str,
    later_statement: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: later delimiters can move the canonical lexical error."""
    old_source = line_ending.join(
        (
            "Функция Первый() Экспорт",
            "    Возврат 1;",
            "КонецФункции",
            "Функция Второй()",
            f"    {later_statement}",
            "КонецФункции",
            "",
        )
    )
    new_source = old_source.replace("Возврат 1;", replacement)
    catalog = _catalog(5, "МодульА")
    parser = PythonParserTarget.from_generated()
    previous = build_full_worker_semantic_snapshot(
        _unit_from_source(old_source, 10),
        catalog,
        parser,
    )
    parse_sources: list[str] = []
    original_parse = module_universe.parse_raw_module

    def record_parse(source: str, target: PythonParserTarget):
        parse_sources.append(source)
        return original_parse(source, target)

    monkeypatch.setattr(module_universe, "parse_raw_module", record_parse)
    with pytest.raises(BslLexError) as caught:
        try_build_worker_semantic_delta(
            previous,
            _unit_from_source(new_source, 11),
            catalog,
            parser,
        )
    monkeypatch.setattr(module_universe, "parse_raw_module", original_parse)
    with pytest.raises(BslLexError) as full_caught:
        build_full_worker_semantic_snapshot(
            _unit_from_source(new_source, 11),
            catalog,
            PythonParserTarget.from_generated(),
        )

    assert parse_sources == [
        new_source[
            previous.analysis.methods[0].method_declaration.start :
            previous.analysis.methods[0].method_declaration.end
            + len(new_source)
            - len(old_source)
        ]
    ]
    assert str(caught.value) == str(full_caught.value)
    assert caught.value.code == full_caught.value.code
    assert caught.value.span == full_caught.value.span


def test_cross_method_lexical_recovery_is_attributed_to_semantic_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: full lexical recovery must remain visible in telemetry."""
    old_source = (
        "Функция Первый() Экспорт\n"
        "    Возврат 1;\n"
        "КонецФункции\n"
        "Функция Второй()\n"
        '    Возврат "later";\n'
        "КонецФункции\n"
    )
    new_source = old_source.replace("Возврат 1;", 'Возврат "незавершенная')
    catalog = _catalog(5, "МодульА")
    parser = PythonParserTarget.from_generated()
    previous = build_full_worker_semantic_snapshot(
        _unit_from_source(old_source, 10),
        catalog,
        parser,
    )
    clock = [0]
    recorder = PhaseRecorder(
        wall_clock_ns=lambda: clock[0],
        cpu_clock_ns=lambda: clock[0],
    )
    from onec_runtime.bsl.lexer import tokenize as real_tokenize

    def timed_full_tokenize(source: str):
        assert source == new_source
        clock[0] += 100
        return real_tokenize(source)

    monkeypatch.setattr(
        module_universe,
        "tokenize",
        timed_full_tokenize,
        raising=False,
    )

    with pytest.raises(BslLexError):
        try_build_worker_semantic_delta(
            previous,
            _unit_from_source(new_source, 11),
            catalog,
            parser,
            profiler=recorder,
        )

    assert [(event.phase, event.wall_ns, event.error_present) for event in recorder.events] == [
        ("semantic_parse", 100, True)
    ]


def test_valid_delta_does_not_enter_full_lexical_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: canonical full lexing is restricted to lexical errors."""
    catalog = _catalog(5, "МодульА")
    parser = PythonParserTarget.from_generated()
    previous = build_full_worker_semantic_snapshot(
        _unit("МодульА", 10, value="one"),
        catalog,
        parser,
    )

    def reject_recovery(_source: str) -> object:
        raise AssertionError("valid delta entered lexical recovery")

    monkeypatch.setattr(module_universe, "tokenize", reject_recovery)

    result = try_build_worker_semantic_delta(
        previous,
        _unit("МодульА", 11, value="two"),
        catalog,
        parser,
    )

    assert result is not None


@pytest.mark.parametrize("line_ending", ("\n", "\r\n"))
@pytest.mark.parametrize("delimiter", ('"', "'"))
def test_cross_boundary_lexical_state_requests_full_fallback_when_full_lexes(
    line_ending: str,
    delimiter: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: an isolated lex error is not admitted if full lexing passes."""
    old_source = line_ending.join(
        (
            "Функция Первый() Экспорт",
            "    Возврат 1;",
            "КонецФункции",
            "Функция Второй()",
            f"    // delimiter in an unchanged comment: {delimiter}",
            "    Возврат 2;",
            "КонецФункции",
            "",
        )
    )
    replacement = (
        'Возврат "cross-boundary'
        if delimiter == '"'
        else "Возврат '20260901"
    )
    new_source = old_source.replace("Возврат 1;", replacement)
    catalog = _catalog(5, "МодульА")
    parser = PythonParserTarget.from_generated()
    previous = build_full_worker_semantic_snapshot(
        _unit_from_source(old_source, 10),
        catalog,
        parser,
    )
    parse_sources: list[str] = []
    original_parse = module_universe.parse_raw_module
    recorder = PhaseRecorder()

    def record_parse(source: str, target: PythonParserTarget):
        parse_sources.append(source)
        return original_parse(source, target)

    monkeypatch.setattr(module_universe, "parse_raw_module", record_parse)
    result = try_build_worker_semantic_delta(
        previous,
        _unit_from_source(new_source, 11),
        catalog,
        parser,
        profiler=recorder,
    )
    monkeypatch.setattr(module_universe, "parse_raw_module", original_parse)

    assert result is None
    assert len(parse_sources) == 1
    assert parse_sources[0] != new_source
    assert [(event.phase, event.error_present) for event in recorder.events] == [
        ("semantic_parse", False),
        ("dependency_analysis", False),
    ]
    with pytest.raises(BslParseError):
        build_full_worker_semantic_snapshot(
            _unit_from_source(new_source, 11),
            catalog,
            PythonParserTarget.from_generated(),
        )


def test_generated_dependency_name_collision_is_exact_without_full_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: delta admission must retain generated-name collision fences."""
    generated = dependency_export_name("МодульБ")
    old_source = (
        "Функция Первый() Экспорт\n"
        "    Возврат 1;\n"
        "КонецФункции\n"
    )
    replacement = (
        f"{generated} = 1;\n"
        "    Возврат МодульБ.Получить();"
    )
    new_source = old_source.replace("Возврат 1;", replacement)
    catalog = _catalog(5, "МодульА", "МодульБ")
    parser = PythonParserTarget.from_generated()
    previous = build_full_worker_semantic_snapshot(
        _unit_from_source(old_source, 10),
        catalog,
        parser,
    )
    calls = 0
    original = module_universe.parse_raw_module

    def record(source: str, target: PythonParserTarget):
        nonlocal calls
        calls += 1
        return original(source, target)

    monkeypatch.setattr(module_universe, "parse_raw_module", record)

    with pytest.raises(ModuleUniverseAdmissionError) as caught:
        try_build_worker_semantic_delta(
            previous,
            _unit_from_source(new_source, 11),
            catalog,
            parser,
        )

    assert calls == 1
    assert caught.value.code == "ambiguous_module_dependency"
    assert caught.value.span == SourceSpan(
        new_source.index(generated),
        new_source.index(generated) + len(generated),
    )


def test_delta_merge_and_candidate_retokenization_are_attributed_to_dependency_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: full-candidate merge work must not escape phase telemetry."""
    old_source = (
        "Функция Первый() Экспорт\n"
        "    Возврат 1;\n"
        "КонецФункции\n"
    )
    new_source = old_source.replace("Возврат 1;", "Возврат МодульБ.Получить();")
    catalog = _catalog(5, "МодульА", "МодульБ")
    target = PythonParserTarget.from_generated()
    previous = build_full_worker_semantic_snapshot(
        _unit_from_source(old_source, 10),
        catalog,
        target,
    )
    clock = [0]
    recorder = PhaseRecorder(
        wall_clock_ns=lambda: clock[0],
        cpu_clock_ns=lambda: clock[0],
    )
    original_tokenize = module_delta.tokenize

    def timed_tokenize(source: str):
        clock[0] += 100
        return original_tokenize(source)

    monkeypatch.setattr(module_delta, "tokenize", timed_tokenize)

    result = try_build_worker_semantic_delta(
        previous,
        _unit_from_source(new_source, 11),
        catalog,
        target,
        profiler=recorder,
    )

    assert result is not None
    dependency_events = [
        event for event in recorder.events if event.phase == "dependency_analysis"
    ]
    assert len(dependency_events) == 1
    assert dependency_events[0].wall_ns >= 100
    assert [event.phase for event in recorder.events] == [
        "semantic_parse",
        "dependency_analysis",
        "alias_transform",
        "source_map_composition",
    ]


def test_deterministic_diverse_edit_matrix_classifies_delta_and_fallback_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: a literal-only corpus must not hide unsupported edit paths."""
    rng = random.Random(20260901)
    catalog = _catalog(5, "МодульА", "МодульБ")
    parser = PythonParserTarget.from_generated()
    original_parse = module_universe.parse_raw_module
    source_hashes: set[str] = set()
    supported_coverage: set[tuple[str, str, int]] = set()
    fallback_coverage: set[str] = set()
    supported_kinds = ("literal", "statement", "dependency_add", "dependency_remove")

    for case_number in range(120):
        line_ending = "\r\n" if case_number % 2 else "\n"
        method_index = rng.randrange(3)
        kind = supported_kinds[case_number % len(supported_kinds)]
        old_source = _matrix_module_source(
            case_number,
            line_ending,
            dependency_method=(method_index if kind == "dependency_remove" else None),
        )
        marker = (
            "Возврат МодульБ.Получить();"
            if kind == "dependency_remove"
            else f'Возврат "base-{method_index}";'
        )
        if kind == "literal":
            replacement = f'Возврат "значение-{case_number}-🙂";'
        elif kind == "statement":
            replacement = (
                f"Результат = {rng.randrange(10**6, 10**12)};"
                f"{line_ending}    Возврат Результат;"
            )
        elif kind == "dependency_add":
            replacement = "Возврат МодульБ.Получить();"
        else:
            replacement = f"Возврат {rng.randrange(10**6, 10**12)};"
        candidate_source = old_source.replace(marker, replacement, 1)
        previous = build_full_worker_semantic_snapshot(
            _unit_from_source(old_source, 1),
            catalog,
            parser,
        )
        candidate = _unit_from_source(candidate_source, case_number + 2)
        calls: list[str] = []

        def record(source: str, target: PythonParserTarget):
            calls.append(source)
            return original_parse(source, target)

        monkeypatch.setattr(module_universe, "parse_raw_module", record)
        actual = try_build_worker_semantic_delta(
            previous,
            candidate,
            catalog,
            parser,
        )
        monkeypatch.setattr(module_universe, "parse_raw_module", original_parse)
        expected = build_full_worker_semantic_snapshot(
            candidate,
            catalog,
            PythonParserTarget.from_generated(),
        )

        assert actual is not None, (case_number, kind, method_index)
        assert len(calls) == 1
        assert calls[0] != candidate_source
        _assert_semantically_equal(actual, expected)
        source_hashes.add(candidate.mapped_source.artifact.source_sha256)
        supported_coverage.add((kind, line_ending, method_index))

    fallback_kinds = (
        "signature",
        "local_declaration",
        "directive",
        "two_methods",
        "repeated_ambiguity",
        "method_add",
        "method_delete",
        "module_variable",
        "export",
        "body_local_declaration",
    )
    for offset in range(110):
        case_number = 120 + offset
        line_ending = "\r\n" if offset % 2 else "\n"
        method_index = rng.randrange(3)
        kind = fallback_kinds[offset % len(fallback_kinds)]
        old_source = _matrix_module_source(
            case_number,
            line_ending,
            directive_method=(method_index if kind == "directive" else None),
            repeated_method=(
                method_index if kind == "repeated_ambiguity" else None
            ),
        )
        method_name = ("Первый", "Второй", "Третий")[method_index]
        header = f"Функция {method_name}()"
        marker = f'Возврат "base-{method_index}";'
        candidate_source = old_source
        expected_delta_parses = 0

        if kind == "signature":
            candidate_source = old_source.replace(
                header,
                f"Функция {method_name}(Параметр{case_number})",
                1,
            )
        elif kind == "local_declaration":
            candidate_source = old_source.replace(
                f"{header}{' Экспорт' if method_index == 0 else ''}{line_ending}",
                (
                    f"{header}{' Экспорт' if method_index == 0 else ''}"
                    f"{line_ending}    Перем Локальная{case_number};{line_ending}"
                ),
                1,
            )
        elif kind == "directive":
            candidate_source = old_source.replace(
                "#Если Сервер Тогда",
                f"#Если Сервер Тогда{' ' * (case_number + 1)}",
                1,
            )
        elif kind == "two_methods":
            candidate_source = old_source.replace(
                'Возврат "base-0";',
                f"Возврат {case_number};",
                1,
            ).replace(
                'Возврат "base-2";',
                f"Возврат {case_number + 1};",
                1,
            )
        elif kind == "repeated_ambiguity":
            candidate_source = old_source.replace('Возврат "aaa";', 'Возврат "aaaa";', 1)
        elif kind == "method_add":
            candidate_source = old_source + (
                f"Функция Добавленная{case_number}(){line_ending}"
                f"    Возврат {case_number};{line_ending}"
                f"КонецФункции{line_ending}"
            )
        elif kind == "method_delete":
            method_start = old_source.index(header)
            method_end = old_source.index("КонецФункции", method_start) + len(
                "КонецФункции"
            )
            if old_source[method_end :].startswith(line_ending):
                method_end += len(line_ending)
            candidate_source = old_source[:method_start] + old_source[method_end:]
        elif kind == "module_variable":
            candidate_source = (
                f"Перем Модульная{case_number};{line_ending}" + old_source
            )
        elif kind == "export":
            candidate_source = old_source.replace("() Экспорт", "()", 1)
        else:
            candidate_source = old_source.replace(
                marker,
                (
                    f"Перем Локальная{case_number};{line_ending}"
                    f"    Возврат {case_number};"
                ),
                1,
            )
            expected_delta_parses = 1

        previous = build_full_worker_semantic_snapshot(
            _unit_from_source(old_source, 1),
            catalog,
            parser,
        )
        candidate = _unit_from_source(candidate_source, case_number + 2)
        calls = []

        def record_fallback(source: str, target: PythonParserTarget):
            calls.append(source)
            return original_parse(source, target)

        monkeypatch.setattr(module_universe, "parse_raw_module", record_fallback)
        actual = try_build_worker_semantic_delta(
            previous,
            candidate,
            catalog,
            parser,
        )
        monkeypatch.setattr(module_universe, "parse_raw_module", original_parse)
        expected = build_full_worker_semantic_snapshot(
            candidate,
            catalog,
            PythonParserTarget.from_generated(),
        )

        assert actual is None, (case_number, kind, method_index)
        assert len(calls) == expected_delta_parses, (
            case_number,
            kind,
            method_index,
        )
        assert expected.analysis.unit is candidate
        source_hashes.add(candidate.mapped_source.artifact.source_sha256)
        fallback_coverage.add(kind)

    assert len(source_hashes) == 230
    assert {item[0] for item in supported_coverage} == set(supported_kinds)
    assert {item[1] for item in supported_coverage} == {"\n", "\r\n"}
    assert {item[2] for item in supported_coverage} == {0, 1, 2}
    assert fallback_coverage == set(fallback_kinds)


def _matrix_module_source(
    case_number: int,
    line_ending: str,
    *,
    dependency_method: int | None = None,
    directive_method: int | None = None,
    repeated_method: int | None = None,
) -> str:
    lines = [f"// matrix case {case_number}"]
    for index, method_name in enumerate(("Первый", "Второй", "Третий")):
        export = " Экспорт" if index == 0 else ""
        lines.append(f"Функция {method_name}(){export}")
        if directive_method == index:
            lines.append("#Если Сервер Тогда")
        if dependency_method == index:
            lines.append("    Возврат МодульБ.Получить();")
        elif repeated_method == index:
            lines.append('    Возврат "aaa";')
        else:
            lines.append(f'    Возврат "base-{index}";')
        if directive_method == index:
            lines.append("#КонецЕсли")
        lines.append("КонецФункции")
    return line_ending.join(lines) + line_ending


def test_full_semantic_snapshot_retains_analyzed_lowered_admission() -> None:
    """Break caught: a full reload must leave one reusable admitted result."""
    snapshot, catalog, parser_target = _snapshot()

    assert snapshot.analysis is snapshot.lowered.analysis
    assert snapshot.catalog_identity == (
        catalog.profile,
        catalog.preprocessor_profile,
        catalog.revision,
        catalog.sha256,
    )
    assert snapshot.parser_identity == (
        parser_target.metadata.parser_identity_sha256,
        parser_target.metadata.parsergen_package_sha256 or "",
    )
    assert worker_semantic_admission(
        snapshot.lowered,
        packer_identity="worker-epf-v1",
    ) is not None
    assert "Возврат" not in repr(snapshot)


def test_semantic_snapshot_base_accepts_monotonic_same_identity() -> None:
    """Break caught: a supported next revision must reuse its confirmed base."""
    snapshot, catalog, parser_target = _snapshot()
    same_catalog_new_revision = _catalog(6, "МодульА")
    assert same_catalog_new_revision.sha256 == catalog.sha256

    selected = select_worker_semantic_snapshot_base(
        snapshot,
        _unit("модульа", 11, value="two"),
        same_catalog_new_revision,
        parser_target,
    )

    assert selected is snapshot


@pytest.mark.parametrize(
    "mismatch",
    (
        "logical_name",
        "kind",
        "module_revision",
        "same_revision_changed_source",
        "catalog_profile",
        "preprocessor_profile",
        "catalog_revision",
        "catalog_digest",
        "parser_identity",
        "parser_package",
        "transform",
    ),
)
def test_semantic_snapshot_identity_mismatch_selects_full_fallback(
    mismatch: str,
) -> None:
    """Break caught: an uncertain base must yield full fallback, never admission."""
    snapshot, catalog, parser_target = _snapshot()
    unit = _unit("МодульА", 11, value="two")
    candidate_catalog = _catalog(6, "МодульА")
    candidate_parser = parser_target
    previous = snapshot

    if mismatch == "logical_name":
        unit = _unit("МодульБ", 11, value="two")
    elif mismatch == "kind":
        unit = _unit("МодульА", 11, kind="test-module", value="two")
    elif mismatch == "module_revision":
        unit = _unit("МодульА", 9, value="two")
    elif mismatch == "same_revision_changed_source":
        unit = _unit("МодульА", 10, value="two")
    elif mismatch == "catalog_profile":
        candidate_catalog = _catalog(6, "МодульА", profile="other-profile")
    elif mismatch == "preprocessor_profile":
        candidate_catalog = _catalog(
            6,
            "МодульА",
            preprocessor_profile="client",
        )
    elif mismatch == "catalog_revision":
        candidate_catalog = _catalog(4, "МодульА")
    elif mismatch == "catalog_digest":
        candidate_catalog = _catalog(6, "МодульА", "МодульБ")
    elif mismatch in {"parser_identity", "parser_package"}:
        metadata = GeneratedParserMetadata(
            (
                "f" * 64
                if mismatch == "parser_identity"
                else parser_target.metadata.grammar_sha256
            ),
            (
                "e" * 64
                if mismatch == "parser_package"
                else parser_target.metadata.parsergen_package_sha256
            ),
        )
        candidate_parser = PythonParserTarget(
            type(parser_target.generated_parser),
            parser_target.generated_error,
            metadata,
        )
    else:
        changed_lowered = replace(
            snapshot.lowered,
            transform_version="module-universe-v2",
        )
        previous = WorkerSemanticSnapshot(
            snapshot.analysis,
            changed_lowered,
            snapshot.catalog_identity,
            snapshot.parser_identity,
        )

    assert select_worker_semantic_snapshot_base(
        previous,
        unit,
        candidate_catalog,
        candidate_parser,
    ) is None
