from __future__ import annotations

from pathlib import Path
import json

import pytest

from onec_runtime.epf_container import EpfContainerError
import onec_runtime.worker_epf as worker_epf
import onec_runtime.bsl.source_maps as source_maps
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
    compose_source_maps,
    mapped_visible_source,
    source_sha256,
)
from onec_runtime.worker_epf import (
    build_worker_epf,
    read_worker_source,
)


SOURCE_LF = "Функция Посчитать() Экспорт\n\tВозврат 29;\nКонецФункции\n"
SOURCE_CRLF = SOURCE_LF.replace("\n", "\r\n")


def test_builds_deterministic_worker_and_recovers_normalized_source(
    tmp_path: Path,
) -> None:
    output = tmp_path / "Worker.epf"

    first = build_worker_epf(SOURCE_LF, output).read_bytes()
    second = build_worker_epf(SOURCE_CRLF, output).read_bytes()

    assert first == second
    assert read_worker_source(output) == SOURCE_LF


def test_changed_source_atomically_replaces_stale_artifact(tmp_path: Path) -> None:
    output = tmp_path / "Worker.epf"
    first = build_worker_epf(SOURCE_LF, output).read_bytes()
    changed = SOURCE_LF.replace("29", "31")

    second = build_worker_epf(changed, output).read_bytes()

    assert second != first
    assert read_worker_source(output) == changed
    assert tuple(output.parent.glob("*.tmp")) == ()


def test_supports_five_thousand_line_object_module(tmp_path: Path) -> None:
    body = "".join(f"\tЗначение = {index};\n" for index in range(5_000))
    source = f"Функция Большая() Экспорт\n{body}\tВозврат Значение;\nКонецФункции\n"

    output = build_worker_epf(source, tmp_path / "Worker-large.epf")

    assert read_worker_source(output) == source
    assert output.stat().st_size > 0


def test_mapped_worker_packaging_reuses_admitted_hash_and_encodes_source_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: native packaging must not rehash admitted Worker text."""
    source = "//" + "x" * 1_999_900 + "\nПроцедура X()\nКонецПроцедуры\n"
    unit = SourceUnitRef(
        SourceUnitKind.MODULE,
        "large-worker",
        1,
        source_sha256(source),
    )
    prepared = worker_epf.prepare_worker_module_source(
        mapped_visible_source(source, unit)
    )
    work: list[tuple[str, int]] = []
    monkeypatch.setattr(
        source_maps,
        "_TEXT_WORK_OBSERVER",
        lambda event, width: work.append((event, width)),
        raising=False,
    )

    build_worker_epf(prepared, tmp_path / "Worker-large-mapped.epf")

    assert [width for event, width in work if event == "packaging_encode"] == [
        len(prepared.text)
    ]
    assert not [width for event, width in work if event == "packaging_hash"]
    assert [width for event, width in work if event == "packaging_verify"] == [
        len(prepared.text)
    ]


def test_mapped_packaging_rejects_worker_kind_without_normalization_authority(
    tmp_path: Path,
) -> None:
    """Break caught: an arbitrary transform cannot certify normalized Worker text."""
    source = "Процедура X()\r\nКонецПроцедуры\r\n"
    unit = SourceUnitRef(
        SourceUnitKind.MODULE,
        "uncertified-worker",
        1,
        source_sha256(source),
    )
    visible = mapped_visible_source(source, unit)
    transform = SourceTransformBuilder(visible)
    transform.copy(SourceSpan(0, len(source)))
    uncertified = transform.build(SourceArtifactKind.WORKER_MODULE)

    with pytest.raises(ValueError, match="authority"):
        build_worker_epf(uncertified, tmp_path / "Worker-uncertified.epf")


@pytest.mark.parametrize("source", ("", " \r\n\t", "Функция X()\n\x00\nКонецФункции"))
def test_rejects_empty_or_nul_source(source: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        build_worker_epf(source, tmp_path / "Worker.epf")


def test_reader_rejects_non_worker_container() -> None:
    with pytest.raises(EpfContainerError):
        read_worker_source(b"not-a-worker")


def test_prepared_worker_module_maps_line_ending_normalization_to_exact_input() -> None:
    """Break caught: packaged coordinates must use the final normalized module map."""
    source = "Процедура Первая() Экспорт\r\nКонецПроцедуры;"
    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL,
        "cell-crlf",
        4,
        source_sha256(source),
    )

    prepared = worker_epf.prepare_worker_module_source(
        mapped_visible_source(source, unit)
    )

    assert prepared.text == source.replace("\r\n", "\n")
    assert prepared.artifact.kind is SourceArtifactKind.WORKER_MODULE
    mapped = prepared.source_map.map_offset(prepared.text.index("Конец"))
    assert mapped.relation is MappingRelation.EXACT
    assert mapped.unit == unit
    assert mapped.origin_span is not None
    assert mapped.origin_span.start == source.index("Конец")


def test_prepared_worker_module_drops_generation_identity_while_composing_map() -> None:
    """Break caught: reusable module artifacts must never inherit generation identity."""
    source = "Процедура Первая() Экспорт\r\nКонецПроцедуры;"
    unit = SourceUnitRef(
        SourceUnitKind.MODULE,
        "МодульРасчета",
        11,
        source_sha256(source),
    )
    visible = mapped_visible_source(source, unit)
    from onec_runtime.bsl.source_maps import SourceSpan, SourceTransformBuilder

    builder = SourceTransformBuilder(visible)
    builder.copy(SourceSpan(0, len(source)))
    generation_bound = builder.build(
        SourceArtifactKind.WORKER_PROJECTION,
        worker_generation=19,
        worker_manifest_sha256="1" * 64,
    )

    prepared = worker_epf.prepare_worker_module_source(generation_bound)

    assert prepared.artifact.kind is SourceArtifactKind.WORKER_MODULE
    assert prepared.artifact.worker_generation is None
    assert prepared.artifact.worker_manifest_sha256 is None
    mapped = prepared.source_map.map_offset(prepared.text.index("Конец"))
    assert mapped.relation is MappingRelation.EXACT
    assert mapped.unit == unit
    assert mapped.origin_span is not None
    assert mapped.origin_span.start == source.index("Конец")
    lineage_manifest = prepared.lineage_manifest()

    def assert_generation_independent(value: object) -> None:
        if isinstance(value, dict):
            if "worker_generation" in value:
                assert value["worker_generation"] is None
            if "worker_manifest_sha256" in value:
                assert value["worker_manifest_sha256"] is None
            for item in value.values():
                assert_generation_independent(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                assert_generation_independent(item)

    assert_generation_independent(lineage_manifest)
    assert '"worker_generation": 19' not in json.dumps(
        lineage_manifest,
        ensure_ascii=False,
        sort_keys=True,
    )
    recomposed = visible.source_map
    for local in prepared.lineage:
        recomposed = compose_source_maps(local, recomposed)
    assert recomposed == prepared.source_map


def test_prepared_worker_module_normalizes_crlf_split_between_exact_segments() -> None:
    """Break caught: a CRLF seam must not become one multi-parent derived edit."""
    source = "A\r\nB"
    unit = SourceUnitRef(
        SourceUnitKind.MODULE,
        "ExactSeam",
        1,
        source_sha256(source),
    )
    artifact = SourceArtifactRef(
        SourceArtifactKind.WORKER_PROJECTION,
        source_sha256(source),
        len(source),
        "crlf",
    )
    source_map = SourceMap(
        artifact,
        (
            SourceMapSegment(
                SourceSpan(0, 2),
                unit,
                SourceSpan(0, 2),
                MappingRelation.EXACT,
            ),
            SourceMapSegment(
                SourceSpan(2, 4),
                unit,
                SourceSpan(2, 4),
                MappingRelation.EXACT,
            ),
        ),
    )

    prepared = worker_epf.prepare_worker_module_source(
        MappedSource(source, artifact, source_map)
    )

    assert prepared.text == "A\nB"
    newline = prepared.source_map.map_offset(1)
    assert newline.relation is MappingRelation.EXACT
    assert newline.unit == unit
    assert newline.origin_span == SourceSpan(2, 3)


def test_prepared_worker_module_preserves_derived_metadata_across_crlf_seam() -> None:
    """Break caught: seam handling must retain adjacent derived/exact provenance."""
    source = "A\r\nB"
    visible = "A\nB"
    unit = SourceUnitRef(
        SourceUnitKind.MODULE,
        "DerivedSeam",
        1,
        source_sha256(visible),
    )
    artifact = SourceArtifactRef(
        SourceArtifactKind.WORKER_PROJECTION,
        source_sha256(source),
        len(source),
        "crlf",
    )
    source_map = SourceMap(
        artifact,
        (
            SourceMapSegment(
                SourceSpan(0, 2),
                unit,
                SourceSpan(0, 1),
                MappingRelation.DERIVED,
                "dependency_alias_line",
                unit,
                SourceSpan(0, 1),
            ),
            SourceMapSegment(
                SourceSpan(2, 4),
                unit,
                SourceSpan(1, 3),
                MappingRelation.EXACT,
            ),
        ),
    )

    prepared = worker_epf.prepare_worker_module_source(
        MappedSource(source, artifact, source_map)
    )

    assert prepared.text == visible
    assert prepared.source_map.segments == (
        SourceMapSegment(
            SourceSpan(0, 1),
            unit,
            SourceSpan(0, 1),
            MappingRelation.DERIVED,
            "dependency_alias_line",
            unit,
            SourceSpan(0, 1),
        ),
        SourceMapSegment(
            SourceSpan(1, 3),
            unit,
            SourceSpan(1, 3),
            MappingRelation.EXACT,
        ),
    )
