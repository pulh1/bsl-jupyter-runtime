from __future__ import annotations

from dataclasses import replace
from dataclasses import fields, is_dataclass
from hashlib import sha256
import json
import random
from types import MemberDescriptorType

import pytest

from onec_runtime.bsl import (
    CommonModuleCatalogSnapshot,
    CommonModuleDescriptor,
    CommonModuleScope,
    SourceSpan,
    SourceUnitKind,
    SourceUnitRef,
    mapped_visible_source,
    source_sha256,
    VisibleSourceContext,
    WorkerExport,
    WorkerModuleUnit,
)
from onec_runtime.bsl.module_universe import (
    lower_resolved_worker_module,
    worker_semantic_admission,
)
from onec_runtime.bsl.worker_dependency_resolver import resolve_worker_dependencies
from onec_runtime.bsl.full_ast_worker_projection import parse_full_ast_module
from onec_runtime.bsl.worker_reload_source_map import (
    CompactReloadSourceMap,
    ReloadInsertion,
    materialize_worker_reload_source,
    materialize_worker_reload_source_oracle,
)
from onec_runtime.config import RuntimeConfig
import onec_runtime.server_worker as server_worker
import onec_runtime.bsl.worker_reload_source_map as reload_source_map
from onec_runtime.server_worker import NotebookWorkerArtifactBuilder
from onec_runtime.worker_universe import (
    WorkerModuleArtifactBuilder,
    WorkerModuleArtifactCache,
)


def _visible(source: str):
    return mapped_visible_source(
        source,
        SourceUnitRef(
            SourceUnitKind.MODULE,
            "МодульТеста",
            7,
            source_sha256(source),
        ),
    )


def _insertions(source: str) -> tuple[ReloadInsertion, ...]:
    declaration = source.index("Процедура")
    body = source.index("Исходная")
    method = SourceSpan(declaration, len(source))
    use = SourceSpan(body, body + len("Исходная"))
    return (
        ReloadInsertion.synthetic(
            source_offset=0,
            text="Перем __Dependency Экспорт;\n",
            method_name=None,
            synthetic_region="worker_dependency_field",
            anchor=SourceSpan(0, 0),
        ),
        ReloadInsertion.derived(
            source_offset=body,
            text="Перем КадровыйУчет;\n",
            method_name="Проверить",
            synthetic_region="worker_dependency_alias_declaration",
            origin=use,
            anchor=method,
        ),
        ReloadInsertion.derived(
            source_offset=body,
            text="КадровыйУчет = __Dependency;\n",
            method_name="Проверить",
            synthetic_region="worker_dependency_alias_initializer",
            origin=use,
            anchor=method,
        ),
    )


def test_compact_reload_map_matches_legacy_oracle_without_materializing_segments() -> None:
    """Break caught: the success path must not expand one segment per CRLF/source run."""
    source = "Процедура Проверить()\r\n    Исходная = 1;\r\nКонецПроцедуры\r\n"
    visible = _visible(source)
    insertions = _insertions(source)

    compact = materialize_worker_reload_source(visible, insertions)
    oracle = materialize_worker_reload_source_oracle(visible, insertions)

    assert compact.text.encode("utf-8") == oracle.text.encode("utf-8")
    assert isinstance(compact.source_map, CompactReloadSourceMap)
    assert compact.source_map.generic_materialized is False
    assert tuple(
        compact.source_map.map_offset(offset)
        for offset in range(len(compact.text) + 1)
    ) == tuple(
        oracle.source_map.map_offset(offset)
        for offset in range(len(oracle.text) + 1)
    )
    assert (
        compact.source_map.materialize_generic().to_manifest()
        == oracle.source_map.to_manifest()
    )
    local = compact.local_source_map
    assert isinstance(local, CompactReloadSourceMap)
    assert (
        local.materialize_generic().to_manifest()
        == oracle.local_source_map.to_manifest()
    )
    assert compact.source_map.generic_materialized is False


def test_compact_reload_map_preserves_original_boundaries_and_method_checkpoints() -> None:
    """Break caught: grouped insertion checkpoints must not lose reverse positions."""
    source = "Процедура Проверить()\r\n    Исходная = 1;\r\nКонецПроцедуры"
    insertions = _insertions(source)
    compact = materialize_worker_reload_source(
        _visible(source),
        insertions,
        method_boundaries=(
            reload_source_map.ReloadMethodBoundary(
                "Проверить",
                0,
                source.index("Исходная"),
                len(source),
            ),
        ),
    )
    source_map = compact.source_map
    assert isinstance(source_map, CompactReloadSourceMap)

    for original in range(len(source) + 1):
        generated = source_map.map_original_offset(original)
        if source[original : original + 2] == "\r\n":
            assert generated + 1 == source_map.map_original_offset(original + 1)
            assert source_map.map_original_offset(original + 1) == (
                source_map.map_original_offset(original + 2)
            )
            continue
        if original and source[original - 1 : original + 1] == "\r\n":
            continue
        mapped = source_map.map_offset(generated)
        if mapped.origin_span is not None:
            assert mapped.origin_span.start <= original <= mapped.origin_span.end

    assert len(source_map.checkpoints) == 2
    assert len(source_map.method_checkpoints) == 1
    checkpoint = source_map.method_checkpoints[0]
    assert checkpoint.method_name == "Проверить"
    assert checkpoint.declaration_shift < checkpoint.body_shift


def test_compact_reload_map_keeps_crlf_and_synthetic_semantics_exact() -> None:
    """Break caught: compact arithmetic must not report normalized CRLF as exact text."""
    source = "Процедура Пустая()\r\nКонецПроцедуры\r\n"
    insertion = ReloadInsertion.synthetic(
        source_offset=0,
        text="Перем Поле Экспорт;\n",
        method_name=None,
        synthetic_region="worker_dependency_field",
        anchor=SourceSpan(0, 0),
    )
    compact = materialize_worker_reload_source(_visible(source), (insertion,))
    oracle = materialize_worker_reload_source_oracle(_visible(source), (insertion,))

    interesting = {
        0,
        insertion.generated_length - 1,
        insertion.generated_length,
        compact.text.index("\n", insertion.generated_length),
        len(compact.text),
    }
    assert {
        offset: compact.source_map.map_offset(offset) for offset in interesting
    } == {offset: oracle.source_map.map_offset(offset) for offset in interesting}


def test_compact_manifest_is_private_and_identity_changes_with_mapping() -> None:
    """Break caught: admission identity must bind compact semantics without raw source."""
    source = "Процедура Проверить()\n    Исходная = 1;\nКонецПроцедуры"
    visible = _visible(source)
    insertions = _insertions(source)
    first = materialize_worker_reload_source(visible, insertions)
    changed = materialize_worker_reload_source(
        visible,
        (
            replace(
                insertions[0],
                synthetic_region="worker_dependency_field_changed",
            ),
            *insertions[1:],
        ),
    )

    payload = json.dumps(first.source_map.to_compact_manifest(), ensure_ascii=False)
    assert source not in payload
    assert "Исходная = 1" not in payload
    assert first.source_map.source_map_sha256 != changed.source_map.source_map_sha256
    assert first.artifact.source_sha256 == changed.artifact.source_sha256
    assert first.source_map.generic_materialized is False


def test_compact_reload_inputs_remain_fail_closed() -> None:
    source = "Процедура Пустая()\nКонецПроцедуры"
    visible = _visible(source)
    insertion = ReloadInsertion.synthetic(
        source_offset=len(source) + 1,
        text="X",
        method_name=None,
        synthetic_region="bad",
        anchor=SourceSpan(0, 0),
    )
    with pytest.raises(ValueError, match="outside source"):
        materialize_worker_reload_source(visible, (insertion,))


@pytest.mark.parametrize("seed", range(12))
def test_compact_reload_map_differential_property_against_legacy(seed: int) -> None:
    """Break caught: interval lookup must preserve every boundary ordering case."""
    generator = random.Random(seed)
    endings = ("\n", "\r\n", "\r")
    source = "".join(
        f"Строка{index} = {generator.randrange(100)};{generator.choice(endings)}"
        for index in range(12)
    )
    candidate_offsets = tuple(
        offset
        for offset in range(len(source) + 1)
        if not (
            offset > 0
            and offset < len(source)
            and source[offset - 1 : offset + 1] == "\r\n"
        )
    )
    offsets = sorted(generator.sample(candidate_offsets, 6))
    insertions: list[ReloadInsertion] = []
    for ordinal, offset in enumerate(offsets):
        anchor = SourceSpan(offset, min(offset + 1, len(source)))
        if ordinal % 2:
            insertions.append(
                ReloadInsertion.derived(
                    source_offset=offset,
                    text=f"D{ordinal}\n",
                    method_name=f"Метод{ordinal // 2}",
                    synthetic_region="derived_property",
                    origin=anchor,
                    anchor=anchor,
                )
            )
        else:
            insertions.append(
                ReloadInsertion.synthetic(
                    source_offset=offset,
                    text=f"S{ordinal}\n",
                    method_name=None,
                    synthetic_region="synthetic_property",
                    anchor=anchor,
                )
            )
    visible = _visible(source)
    compact = materialize_worker_reload_source(visible, tuple(insertions))
    oracle = materialize_worker_reload_source_oracle(visible, tuple(insertions))

    assert compact.text == oracle.text
    assert tuple(
        compact.source_map.map_offset(offset)
        for offset in range(len(compact.text) + 1)
    ) == tuple(
        oracle.source_map.map_offset(offset)
        for offset in range(len(oracle.text) + 1)
    )


def test_semantic_admission_and_packaging_do_not_materialize_generic_map(
    tmp_path,
) -> None:
    """Break caught: admission/packaging must hash compact records, not legacy segments."""
    source = (
        "Функция Проверить() Экспорт\r\n"
        "    Возврат КадровыйУчет.Рассчитать();\r\n"
        "КонецФункции\r\n"
    )
    visible = _visible(source)
    unit = WorkerModuleUnit("МодульТеста", "module", 7, visible)
    catalog = CommonModuleCatalogSnapshot.create(
        profile="server-test",
        preprocessor_profile="server",
        revision=1,
        modules=(
            CommonModuleDescriptor("КадровыйУчет", CommonModuleScope.SERVER),
        ),
    )
    plan = resolve_worker_dependencies(parse_full_ast_module(source), catalog)
    lowered = lower_resolved_worker_module(unit, plan)
    compact_map = lowered.mapped_source.source_map
    assert isinstance(compact_map, CompactReloadSourceMap)
    admission = worker_semantic_admission(
        lowered,
        packer_identity="worker-epf-v1",
    )
    assert compact_map.generic_materialized is False

    platform = tmp_path / "platform"
    platform.mkdir()
    for executable in ("1cv8.exe", "1cv8c.exe", "dbgs.exe"):
        (platform / executable).write_bytes(b"stub")
    builder = NotebookWorkerArtifactBuilder(RuntimeConfig(tmp_path, platform))
    builder(
        lowered.mapped_source,
        (
            WorkerExport(
                "МодульТеста.Проверить",
                "Проверить",
                receiver_module="МодульТеста",
            ),
        ),
        visible_source_context=VisibleSourceContext(
            {
                SourceUnitRef(
                    SourceUnitKind.MODULE,
                    "МодульТеста",
                    7,
                    source_sha256(source),
                ): source
            }
        ),
        semantic_admission=admission,
        semantic_packer_identity="worker-epf-v1",
    )

    assert compact_map.generic_materialized is False


def test_module_artifact_admission_keeps_compact_map_lazy(tmp_path) -> None:
    """Break caught: module-universe validation must not iterate legacy segments."""
    source = (
        "Функция Проверить() Экспорт\n"
        "    Возврат КадровыйУчет.Рассчитать();\n"
        "КонецФункции\n"
    )
    visible = _visible(source)
    unit = WorkerModuleUnit("МодульТеста", "module", 7, visible)
    catalog = CommonModuleCatalogSnapshot.create(
        profile="server-test",
        preprocessor_profile="server",
        revision=1,
        modules=(
            CommonModuleDescriptor("КадровыйУчет", CommonModuleScope.SERVER),
        ),
    )
    lowered = lower_resolved_worker_module(
        unit,
        resolve_worker_dependencies(parse_full_ast_module(source), catalog),
    )
    compact_map = lowered.mapped_source.source_map
    assert isinstance(compact_map, CompactReloadSourceMap)
    platform = tmp_path / "platform"
    platform.mkdir()
    for executable in ("1cv8.exe", "1cv8c.exe", "dbgs.exe"):
        (platform / executable).write_bytes(b"stub")
    packer = NotebookWorkerArtifactBuilder(RuntimeConfig(tmp_path, platform))
    builder = WorkerModuleArtifactBuilder(
        packer,
        cache=WorkerModuleArtifactCache(),
        packer_version="worker-epf-v1",
        target_profile="server-test",
    )

    artifact = builder.build(
        lowered,
        visible_source_context=VisibleSourceContext(
            {
                SourceUnitRef(
                    SourceUnitKind.MODULE,
                    "МодульТеста",
                    7,
                    source_sha256(source),
                ): source
            }
        ),
    )

    assert compact_map.generic_materialized is False
    snapshot = server_worker._validated_admitted_snapshot(artifact.worker_artifact)
    assert isinstance(snapshot.mapped_source.source_map, CompactReloadSourceMap)
    assert snapshot.mapped_source.source_map.generic_materialized is False


def test_compact_manifest_is_the_hashed_public_manifest() -> None:
    source = "Процедура Тест()\r\n    Исходная = 1;\r\nКонецПроцедуры\r\n"
    mapped = materialize_worker_reload_source(_visible(source), _insertions(source))
    compact = mapped.source_map
    assert isinstance(compact, CompactReloadSourceMap)

    manifest = compact.to_manifest()
    payload = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    assert manifest["schema"] == "onec-compact-worker-reload-map-v1"
    assert sha256(payload).hexdigest() == compact.source_map_sha256
    assert compact.generic_materialized is False


def test_compact_mapping_mutation_invalidates_semantic_admission() -> None:
    source = (
        "Функция Проверить() Экспорт\n"
        "    Возврат КадровыйУчет.Рассчитать();\n"
        "КонецФункции\n"
    )
    unit = WorkerModuleUnit("МодульТеста", "module", 7, _visible(source))
    catalog = CommonModuleCatalogSnapshot.create(
        profile="server-test",
        preprocessor_profile="server",
        revision=1,
        modules=(
            CommonModuleDescriptor("КадровыйУчет", CommonModuleScope.SERVER),
        ),
    )
    lowered = lower_resolved_worker_module(
        unit,
        resolve_worker_dependencies(parse_full_ast_module(source), catalog),
    )
    compact = lowered.mapped_source.source_map
    assert isinstance(compact, CompactReloadSourceMap)
    worker_semantic_admission(lowered, packer_identity="worker-epf-v1")

    object.__setattr__(
        compact._compact_basis.intervals[0],
        "synthetic_region",
        "tampered-region",
    )

    with pytest.raises(ValueError, match="admission is invalid"):
        worker_semantic_admission(lowered, packer_identity="other-packer")


def test_compact_derived_indices_reject_ordinary_mutation_after_admission() -> None:
    source = (
        "Функция Проверить() Экспорт\n"
        "    Возврат КадровыйУчет.Рассчитать();\n"
        "КонецФункции\n"
    )
    unit = WorkerModuleUnit("МодульТеста", "module", 7, _visible(source))
    catalog = CommonModuleCatalogSnapshot.create(
        profile="server-test",
        preprocessor_profile="server",
        revision=1,
        modules=(
            CommonModuleDescriptor("КадровыйУчет", CommonModuleScope.SERVER),
        ),
    )
    lowered = lower_resolved_worker_module(
        unit,
        resolve_worker_dependencies(parse_full_ast_module(source), catalog),
    )
    compact = lowered.mapped_source.source_map
    assert isinstance(compact, CompactReloadSourceMap)
    admission = worker_semantic_admission(lowered, packer_identity="worker-epf-v1")
    before_digest = compact.source_map_sha256
    before_mapping = compact.map_offset(0)

    with pytest.raises(AttributeError):
        compact._compact_interval_starts = ()
    with pytest.raises(AttributeError):
        del compact._compact_inserted_after

    assert compact.source_map_sha256 == before_digest
    assert compact.map_offset(0) == before_mapping
    assert (
        worker_semantic_admission(lowered, packer_identity="worker-epf-v1")
        is admission
    )


@pytest.mark.parametrize(
    "field,replacement",
    (
        ("interval_starts", ()),
        ("checkpoint_offsets", ()),
        ("inserted_after", ()),
        ("line_ending_starts", ()),
        ("line_ending_widths", b""),
        ("crlf_lf_offsets", ()),
    ),
)
def test_compact_derived_index_tampering_fails_admission(
    field: str,
    replacement: object,
) -> None:
    source = (
        "Функция Проверить() Экспорт\r\n"
        "    Возврат КадровыйУчет.Рассчитать();\r\n"
        "КонецФункции\r\n"
    )
    unit = WorkerModuleUnit("МодульТеста", "module", 7, _visible(source))
    catalog = CommonModuleCatalogSnapshot.create(
        profile="server-test",
        preprocessor_profile="server",
        revision=1,
        modules=(
            CommonModuleDescriptor("КадровыйУчет", CommonModuleScope.SERVER),
        ),
    )
    lowered = lower_resolved_worker_module(
        unit,
        resolve_worker_dependencies(parse_full_ast_module(source), catalog),
    )
    compact = lowered.mapped_source.source_map
    assert isinstance(compact, CompactReloadSourceMap)
    worker_semantic_admission(lowered, packer_identity="worker-epf-v1")

    object.__setattr__(compact._compact_basis, field, replacement)

    with pytest.raises(ValueError, match="admission is invalid"):
        worker_semantic_admission(lowered, packer_identity="other-packer")


def _retained_graph(root: object) -> tuple[object, ...]:
    seen: set[int] = set()
    retained: list[object] = []
    stack = [root]
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        retained.append(current)
        if isinstance(current, tuple):
            stack.extend(current)
            continue
        for kind in type(current).__mro__:
            slots = getattr(kind, "__slots__", ())
            if isinstance(slots, str):
                slots = (slots,)
            for slot in slots:
                descriptor = kind.__dict__.get(slot)
                if not isinstance(descriptor, MemberDescriptorType):
                    continue
                try:
                    stack.append(descriptor.__get__(current, type(current)))
                except AttributeError:
                    pass
        if is_dataclass(current):
            stack.extend(getattr(current, item.name) for item in fields(current))
    return tuple(retained)


def test_compact_success_graph_does_not_retain_raw_source_or_edit_payloads() -> None:
    body = "".join(f"    Локальная{index} = {index};\r\n" for index in range(200))
    source = f"Процедура Проверить()\r\n{body}КонецПроцедуры\r\n"
    insertions = _insertions(source.replace("Локальная0", "Исходная", 1))
    source = source.replace("Локальная0", "Исходная", 1)
    mapped = materialize_worker_reload_source(_visible(source), insertions)
    compact = mapped.source_map
    assert isinstance(compact, CompactReloadSourceMap)

    retained = _retained_graph(compact)
    retained_strings = tuple(item for item in retained if isinstance(item, str))

    assert all(item is not source for item in retained_strings)
    assert all(
        all(item is not insertion._text for item in retained_strings)
        for insertion in insertions
    )
    assert sum(len(item) for item in retained_strings) < len(source) // 2
    assert not any(isinstance(item, (list, dict, set, bytearray)) for item in retained)

    snapshot = reload_source_map.snapshot_compact_mapped_worker_source(mapped)
    snapshot_retained = _retained_graph(snapshot.source_map)
    assert all(item is not source for item in snapshot_retained)
    assert not any(
        isinstance(item, (list, dict, set, bytearray))
        for item in snapshot_retained
    )


def test_method_checkpoints_are_real_boundaries_for_every_method() -> None:
    source = (
        "Процедура Первый()\r\n"
        "    Перем Локальная;\r\n"
        "    КадровыйУчет.Рассчитать();\r\n"
        "КонецПроцедуры\r\n"
        "Процедура Второй()\r\n"
        "    Локальная = 1;\r\n"
        "КонецПроцедуры\r\n"
    )
    unit = WorkerModuleUnit("МодульТеста", "module", 7, _visible(source))
    catalog = CommonModuleCatalogSnapshot.create(
        profile="server-test",
        preprocessor_profile="server",
        revision=1,
        modules=(
            CommonModuleDescriptor("КадровыйУчет", CommonModuleScope.SERVER),
        ),
    )
    plan = resolve_worker_dependencies(parse_full_ast_module(source), catalog)
    lowered = lower_resolved_worker_module(unit, plan)
    compact = lowered.mapped_source.source_map
    assert isinstance(compact, CompactReloadSourceMap)

    records = {item.method_name: item for item in compact.method_checkpoints}
    assert set(records) == {"Первый", "Второй"}
    for method in plan.methods:
        record = records[method.source.name]
        assert record.declaration_source_offset == method.source.declaration_span.start
        assert record.body_source_offset == method.source.alias_initializer_offset
        assert record.end_source_offset == method.source.declaration_span.end
        assert record.declaration_generated_offset == compact.map_original_offset(
            record.declaration_source_offset
        )
        assert record.body_generated_offset == compact.map_original_offset(
            record.body_source_offset
        )
        assert record.end_generated_offset == compact.map_original_offset(
            record.end_source_offset
        )
