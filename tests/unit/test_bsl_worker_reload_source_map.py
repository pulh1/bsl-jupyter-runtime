from __future__ import annotations

from dataclasses import replace

import pytest

import onec_runtime.bsl.module_universe as module_universe
import onec_runtime.bsl.source_maps as source_maps
import onec_runtime.bsl.worker_reload_source_map as reload_source_map
import onec_runtime.worker_epf as worker_epf
from onec_runtime.bsl.module_catalog import (
    CommonModuleCatalogSnapshot,
    CommonModuleDescriptor,
    CommonModuleScope,
)
from onec_runtime.bsl.module_universe import (
    DEPENDENCY_ALIAS_DECLARATION_REGION,
    DEPENDENCY_ALIAS_INITIALIZER_REGION,
    DEPENDENCY_FIELD_REGION,
    IMPLICIT_LOCAL_DECLARATION_REGION,
    WorkerModuleUnit,
    analyze_resolved_worker_module,
    lower_resolved_worker_module,
    worker_semantic_admission,
)
from onec_runtime.bsl.source_maps import (
    MappingRelation,
    MappedSource,
    SourceArtifactKind,
    SourceArtifactRef,
    SourceMap,
    SourceMapSegment,
    SourceSpan,
    SourceUnitKind,
    SourceUnitRef,
    mapped_visible_source,
    source_sha256,
)
from onec_runtime.bsl.worker_dependency_resolver import resolve_worker_dependencies
from onec_runtime.bsl.full_ast_worker_projection import (
    full_ast_parser_identity,
    parse_full_ast_module,
)
from onec_runtime.bsl.worker_reload_source_map import ReloadInsertion
from onec_runtime.errors import ModuleUniverseAdmissionError


SOURCE = (
    "Перем Модульная;\n"
    "Функция Первый() Экспорт\n"
    "    Перем Локальная;\n"
    "    Альфа.Получить();\n"
    "    Тест.Получить();\n"
    "    Возврат 1;\n"
    "КонецФункции\n"
    "Процедура Пустой()\n"
    "КонецПроцедуры"
)

EXPECTED = (
    "Перем __OnecDependency_fe7d03c91452 Экспорт;\n"
    "Перем __OnecDependency_e34f6dec12c4 Экспорт;\n"
    "Перем Модульная;\n"
    "Функция Первый() Экспорт\n"
    "    Перем Альфа;\n"
    "    Перем Тест;\n"
    "    Перем Локальная;\n"
    "    Альфа = __OnecDependency_fe7d03c91452;\n"
    "    Тест = __OnecDependency_e34f6dec12c4;\n"
    "    Альфа.Получить();\n"
    "    Тест.Получить();\n"
    "    Возврат 1;\n"
    "КонецФункции\n"
    "Процедура Пустой()\n"
    "КонецПроцедуры"
)


def _catalog(*names: str) -> CommonModuleCatalogSnapshot:
    return CommonModuleCatalogSnapshot.create(
        profile="server-test",
        preprocessor_profile="server",
        revision=3,
        modules=(
            CommonModuleDescriptor(name, CommonModuleScope.SERVER) for name in names
        ),
    )


def _unit_and_plan(source: str = SOURCE):
    unit_ref = SourceUnitRef(
        SourceUnitKind.MODULE,
        "Тест",
        7,
        source_sha256(source),
    )
    unit = WorkerModuleUnit(
        "Тест",
        "module",
        7,
        mapped_visible_source(source, unit_ref),
    )
    plan = resolve_worker_dependencies(
        parse_full_ast_module(source),
        _catalog("Альфа", "Тест"),
    )
    return unit, plan, unit_ref


def test_resolved_analysis_preserves_legacy_shape_without_per_use_positions():
    unit, plan, _ = _unit_and_plan()

    analysis = analyze_resolved_worker_module(unit, plan)

    assert analysis.catalog_identity == plan.catalog_identity
    assert analysis.parser_identity == full_ast_parser_identity()
    assert analysis.exported_methods == ("Первый",)
    assert [binding.target_module for binding in analysis.dependencies] == [
        "Альфа",
        "Тест",
    ]
    assert [binding.export_variable for binding in analysis.dependencies] == [
        "__OnecDependency_fe7d03c91452",
        "__OnecDependency_e34f6dec12c4",
    ]
    first_span = plan.methods[0].source.declaration_span
    assert [binding.uses for binding in analysis.dependencies] == [
        (
            module_universe.DependencyUse(
                "Первый", "value", first_span, first_span
            ),
        ),
        (
            module_universe.DependencyUse(
                "Первый", "value", first_span, first_span
            ),
        ),
    ]


def test_lowering_emits_exact_final_worker_module_for_self_and_empty_method():
    unit, plan, _ = _unit_and_plan()

    lowered = lower_resolved_worker_module(unit, plan)

    assert lowered.mapped_source.text == EXPECTED
    assert lowered.mapped_source.artifact.kind is SourceArtifactKind.WORKER_MODULE
    assert lowered.mapped_source.artifact.line_ending_kind == "lf"
    assert lowered.transform_version == "module-universe-v2"
    assert [
        point.method_name for point in lowered.analysis.method_insertion_points
    ] == ["Первый", "Пустой"]


def test_compact_map_preserves_original_and_anchors_all_injected_regions():
    unit, plan, unit_ref = _unit_and_plan()

    mapped = lower_resolved_worker_module(unit, plan).mapped_source

    original = mapped.source_map.map_offset(mapped.text.index("Возврат 1"))
    assert original.relation is MappingRelation.EXACT
    assert original.unit == unit_ref
    assert original.origin_span == SourceSpan(
        SOURCE.index("Возврат 1"), SOURCE.index("Возврат 1") + 1
    )

    field = mapped.source_map.map_offset(mapped.text.index("__OnecDependency"))
    assert field.relation is MappingRelation.SYNTHETIC
    assert field.synthetic_region == DEPENDENCY_FIELD_REGION
    assert field.anchor_unit == unit_ref
    assert field.anchor_span == SourceSpan(0, 0)

    method_span = plan.methods[0].source.declaration_span
    declaration = mapped.source_map.map_offset(mapped.text.index("Перем Альфа"))
    assert declaration.relation is MappingRelation.DERIVED
    assert declaration.origin_span == method_span
    assert declaration.anchor_unit == unit_ref
    assert declaration.anchor_span == method_span

    initializer = mapped.source_map.map_offset(
        mapped.text.index("Альфа = __OnecDependency")
    )
    assert initializer.relation is MappingRelation.DERIVED
    assert initializer.origin_span == method_span
    assert initializer.anchor_span == method_span

    regions = {
        segment.synthetic_region for segment in mapped.source_map.segments
    }
    assert {
        DEPENDENCY_FIELD_REGION,
        DEPENDENCY_ALIAS_DECLARATION_REGION,
        DEPENDENCY_ALIAS_INITIALIZER_REGION,
    } <= regions
    assert mapped.lineage[-1] is mapped.local_source_map


def test_crlf_is_normalized_directly_and_original_location_remains_exact():
    source = SOURCE.replace("\n", "\r\n")
    unit, plan, unit_ref = _unit_and_plan(source)

    mapped = lower_resolved_worker_module(unit, plan).mapped_source

    assert "\r" not in mapped.text
    assert mapped.text == EXPECTED
    mapped_end = mapped.source_map.map_offset(mapped.text.rindex("КонецПроцедуры"))
    assert mapped_end.relation is MappingRelation.EXACT
    assert mapped_end.unit == unit_ref
    assert mapped_end.origin_span == SourceSpan(
        source.rindex("КонецПроцедуры"),
        source.rindex("КонецПроцедуры") + 1,
    )


def test_resolved_path_does_not_parse_prepare_or_use_general_map_composition(
    monkeypatch,
):
    unit, plan, _ = _unit_and_plan()

    def forbidden(*args, **kwargs):
        raise AssertionError("resolved lowering entered the legacy path")

    monkeypatch.setattr(module_universe, "parse_raw_module", forbidden)
    monkeypatch.setattr(worker_epf, "prepare_worker_module_source", forbidden)
    monkeypatch.setattr(source_maps, "compose_source_maps", forbidden)

    lowered = lower_resolved_worker_module(unit, plan)

    assert lowered.mapped_source.text == EXPECTED
    insertion_regions = sum(
        segment.synthetic_region is not None
        for segment in lowered.mapped_source.source_map.segments
    )
    assert insertion_regions <= 8


def test_resolved_lowering_mints_admission_bound_to_exact_analysis_identity():
    unit, plan, _ = _unit_and_plan()
    lowered = lower_resolved_worker_module(unit, plan)

    assert worker_semantic_admission(lowered, packer_identity="test-packer")

    object.__setattr__(
        lowered.analysis,
        "catalog_identity",
        (
            plan.catalog_identity[0],
            plan.catalog_identity[1],
            plan.catalog_identity[2] + 1,
            "f" * 64,
        ),
    )
    with pytest.raises(ValueError, match="admission is invalid"):
        worker_semantic_admission(lowered, packer_identity="other-packer")


def test_resolved_lowering_admission_rejects_changed_full_parser_identity():
    unit, plan, _ = _unit_and_plan()
    lowered = lower_resolved_worker_module(unit, plan)

    assert worker_semantic_admission(lowered, packer_identity="test-packer")

    parsergen_package_sha256 = lowered.analysis.parser_identity[1]
    # A profile/backend/role change can alter this identity without changing syntax.
    object.__setattr__(
        lowered.analysis,
        "parser_identity",
        ("f" * 64, parsergen_package_sha256),
    )
    with pytest.raises(ValueError, match="admission is invalid"):
        worker_semantic_admission(lowered, packer_identity="other-packer")


def test_analysis_rejects_internally_inconsistent_catalog_identity():
    unit, plan, _ = _unit_and_plan()
    tampered = replace(
        plan,
        catalog_identity=(
            plan.catalog_identity[0],
            plan.catalog_identity[1],
            plan.catalog_identity[2] + 1,
            plan.catalog_identity[3],
        ),
    )

    with pytest.raises(ModuleUniverseAdmissionError, match="analysis input is invalid"):
        analyze_resolved_worker_module(unit, tampered)


@pytest.mark.parametrize(
    "tamper",
    (
        lambda plan: replace(plan, methods=plan.methods[:1]),
        lambda plan: replace(plan, methods=tuple(reversed(plan.methods))),
        lambda plan: replace(plan, dependencies=plan.dependencies[:1]),
        lambda plan: replace(
            plan,
            implicit_local_names=("локальная", "ЛОКАЛЬНАЯ"),
        ),
        lambda plan: replace(
            plan,
            catalog_generation=-1,
            catalog_identity=(
                plan.catalog_identity[0],
                plan.catalog_identity[1],
                -1,
                plan.catalog_identity[3],
            ),
        ),
    ),
)
def test_analysis_rejects_forged_resolved_plan_structure(tamper):
    unit, plan, _ = _unit_and_plan()

    with pytest.raises(
        ModuleUniverseAdmissionError,
        match="analysis input is invalid",
    ):
        analyze_resolved_worker_module(unit, tamper(plan))


def test_reload_insertion_hides_ephemeral_payload_from_repr():
    insertion = ReloadInsertion.derived(
        source_offset=4,
        text="СекретныйИсходник",
        method_name="Метод",
        synthetic_region=DEPENDENCY_ALIAS_DECLARATION_REGION,
        origin=SourceSpan(1, 3),
        anchor=SourceSpan(1, 3),
    )

    assert insertion.generated_length == len("СекретныйИсходник")
    assert "СекретныйИсходник" not in repr(insertion)


def test_materializer_rejects_invalid_insertion_without_attribute_leak():
    unit, _, _ = _unit_and_plan()

    with pytest.raises(ValueError, match="insertions are invalid"):
        reload_source_map.materialize_worker_reload_source(
            unit.mapped_source,
            (object(),),
        )


def test_forbidden_global_write_does_not_generate_field_or_alias():
    source = "Процедура P()\n    Альфа = 1;\nКонецПроцедуры"
    unit, _, _ = _unit_and_plan(source)
    plan = resolve_worker_dependencies(
        parse_full_ast_module(source),
        _catalog("Альфа"),
    )

    lowered = lower_resolved_worker_module(unit, plan)

    assert plan.methods[0].forbidden_global_writes == ("Альфа",)
    assert plan.dependencies == ()
    assert lowered.analysis.dependencies == ()
    assert lowered.mapped_source.text == source
    assert "__OnecDependency" not in lowered.mapped_source.text


def test_frozen_implicit_locals_are_declared_before_and_after_catalog_growth():
    source = (
        "мОдУлЬнАя = 1;\n"
        "Процедура P()\n"
        "    лОкАлЬнАя = 1;\n"
        "    Альфа.X();\n"
        "    Возврат мОдУлЬнАя;\n"
        "КонецПроцедуры"
    )
    unit, _, unit_ref = _unit_and_plan(source)
    initial_catalog = _catalog("Альфа")
    initial = resolve_worker_dependencies(
        parse_full_ast_module(source),
        initial_catalog,
    )
    grown_catalog = CommonModuleCatalogSnapshot.create(
        profile=initial_catalog.profile,
        preprocessor_profile=initial_catalog.preprocessor_profile,
        revision=initial_catalog.revision + 1,
        modules=(
            *initial_catalog.modules,
            CommonModuleDescriptor("Модульная", CommonModuleScope.SERVER),
            CommonModuleDescriptor("Локальная", CommonModuleScope.SERVER),
        ),
    )
    grown = resolve_worker_dependencies(
        initial.source,
        grown_catalog,
        previous=initial,
    )

    initial_lowered = lower_resolved_worker_module(unit, initial)
    grown_lowered = lower_resolved_worker_module(unit, grown)

    assert initial.implicit_local_names == grown.implicit_local_names == (
        "модульная",
    )
    assert initial.methods[0].implicit_local_names == (
        "локальная",
    )
    assert grown.methods[0].implicit_local_names == ("локальная",)
    assert initial.dependencies == grown.dependencies == ("Альфа",)
    assert initial.methods[0].dependencies == grown.methods[0].dependencies == (
        "Альфа",
    )
    assert initial_lowered.mapped_source.text == grown_lowered.mapped_source.text
    generated = grown_lowered.mapped_source.text
    assert "Перем мОдУлЬнАя;\nмОдУлЬнАя = 1;" in generated
    assert generated.index("Перем лОкАлЬнАя;") < generated.index("Перем Альфа;")
    assert generated.index("Перем Альфа;") < generated.index(
        "Альфа = __OnecDependency"
    )

    implicit_segments = tuple(
        segment
        for segment in grown_lowered.mapped_source.source_map.segments
        if segment.synthetic_region == IMPLICIT_LOCAL_DECLARATION_REGION
    )
    assert len(implicit_segments) == 2
    assert implicit_segments[0].anchor_ref == unit_ref
    assert implicit_segments[0].anchor_span == SourceSpan(0, len(source))
    assert implicit_segments[1].anchor_ref == unit_ref
    assert (
        implicit_segments[1].anchor_span
        == initial.methods[0].source.declaration_span
    )


def test_initial_known_global_writes_stay_forbidden_at_both_scopes():
    source = (
        "Модульная = 1;\n"
        "Процедура P()\n"
        "    Локальная = 1;\n"
        "КонецПроцедуры"
    )
    unit, _, _ = _unit_and_plan(source)
    plan = resolve_worker_dependencies(
        parse_full_ast_module(source),
        _catalog("Модульная", "Локальная"),
    )

    lowered = lower_resolved_worker_module(unit, plan)

    assert plan.implicit_local_names == ()
    assert plan.forbidden_global_writes == ("Модульная",)
    assert plan.methods[0].implicit_local_names == ()
    assert plan.methods[0].forbidden_global_writes == ("Локальная",)
    assert lowered.mapped_source.text == source
    assert "Перем Модульная" not in lowered.mapped_source.text
    assert "Перем Локальная" not in lowered.mapped_source.text


@pytest.mark.parametrize("scope", ("module", "method"))
def test_analysis_rejects_forged_implicit_local_without_matching_write(scope):
    source = "Процедура P()\n    Неизвестное.X();\nКонецПроцедуры"
    unit, _, _ = _unit_and_plan(source)
    plan = resolve_worker_dependencies(parse_full_ast_module(source), _catalog())
    if scope == "module":
        forged = replace(plan, implicit_local_names=("инъекция",))
    else:
        method = plan.methods[0]
        forged_method = replace(
            method,
            local_names=tuple(sorted((*method.local_names, "неизвестное"))),
            implicit_local_names=("неизвестное",),
        )
        forged = replace(plan, methods=(forged_method,))

    with pytest.raises(
        ModuleUniverseAdmissionError,
        match="analysis input is invalid",
    ):
        analyze_resolved_worker_module(unit, forged)


def test_analysis_rejects_non_identifier_implicit_local_spelling():
    source = "Локальная = 1;\nПроцедура P()\nКонецПроцедуры"
    unit, _, _ = _unit_and_plan(source)
    plan = resolve_worker_dependencies(parse_full_ast_module(source), _catalog())
    bare = replace(
        plan.source.module_bare_names[0],
        name="bad-name",
        normalized_name="bad-name",
    )
    forged_source = replace(plan.source, module_bare_names=(bare,))
    forged = replace(
        plan,
        source=forged_source,
        implicit_local_names=("bad-name",),
    )

    with pytest.raises(
        ModuleUniverseAdmissionError,
        match="analysis input is invalid",
    ):
        analyze_resolved_worker_module(unit, forged)


def test_analysis_rejects_implicit_local_that_is_also_forbidden():
    source = "Модульная = 1;\nПроцедура P()\nКонецПроцедуры"
    unit, _, _ = _unit_and_plan(source)
    plan = resolve_worker_dependencies(
        parse_full_ast_module(source),
        _catalog("Модульная"),
    )
    forged = replace(plan, implicit_local_names=("модульная",))

    with pytest.raises(
        ModuleUniverseAdmissionError,
        match="analysis input is invalid",
    ):
        analyze_resolved_worker_module(unit, forged)


@pytest.mark.parametrize(
    ("source", "scope", "name"),
    (
        (
            "Перем Явная;\nЯвная = 1;\nПроцедура P()\nКонецПроцедуры",
            "module",
            "явная",
        ),
        (
            "Процедура P()\nПерем Явная;\nЯвная = 1;\nКонецПроцедуры",
            "method",
            "явная",
        ),
        (
            "P = 1;\nПроцедура P()\nКонецПроцедуры",
            "module",
            "p",
        ),
        (
            "Документы = 1;\nПроцедура P()\nКонецПроцедуры",
            "module",
            "документы",
        ),
        (
            "Процедура P()\nP = 1;\nКонецПроцедуры",
            "method",
            "p",
        ),
        (
            "Процедура P()\nДокументы = 1;\nКонецПроцедуры",
            "method",
            "документы",
        ),
    ),
)
def test_analysis_rejects_forged_implicit_local_that_is_hard_local(
    source,
    scope,
    name,
):
    unit, _, _ = _unit_and_plan(source)
    plan = resolve_worker_dependencies(parse_full_ast_module(source), _catalog())
    if scope == "module":
        forged = replace(plan, implicit_local_names=(name,))
    else:
        method = plan.methods[0]
        forged_method = replace(
            method,
            local_names=tuple(sorted({*method.local_names, name})),
            implicit_local_names=(name,),
        )
        forged = replace(plan, methods=(forged_method,))

    with pytest.raises(
        ModuleUniverseAdmissionError,
        match="analysis input is invalid",
    ):
        analyze_resolved_worker_module(unit, forged)


def test_dependency_used_only_in_second_method_is_inserted_only_there():
    source = (
        "Процедура Первый()\n"
        "КонецПроцедуры\n"
        "Процедура Второй()\n"
        "    Альфа.X();\n"
        "КонецПроцедуры"
    )
    unit, _, _ = _unit_and_plan(source)
    plan = resolve_worker_dependencies(
        parse_full_ast_module(source),
        _catalog("Альфа"),
    )

    lowered = lower_resolved_worker_module(unit, plan)

    first = lowered.mapped_source.text.index("Процедура Первый")
    second = lowered.mapped_source.text.index("Процедура Второй")
    alias = lowered.mapped_source.text.index("Перем Альфа")
    assert not first < alias < second
    assert alias > second
    assert lowered.analysis.dependencies[0].uses[0].method_name == "Второй"


def test_materializer_visits_segmented_parent_linearly(monkeypatch):
    source = "A\r\n" * 20 + "Z"
    unit_ref = SourceUnitRef(
        SourceUnitKind.MODULE,
        "Сегменты",
        1,
        source_sha256(source),
    )
    artifact = SourceArtifactRef(
        SourceArtifactKind.VISIBLE,
        unit_ref.source_sha256,
        len(source),
        "crlf",
    )
    parent_segments = tuple(
        SourceMapSegment(
            SourceSpan(offset, offset + 1),
            unit_ref,
            SourceSpan(offset, offset + 1),
            MappingRelation.EXACT,
        )
        for offset in range(len(source))
    )
    parent = MappedSource(source, artifact, SourceMap(artifact, parent_segments))
    insertions = tuple(
        ReloadInsertion.synthetic(
            source_offset=offset,
            text="X",
            method_name=None,
            synthetic_region=DEPENDENCY_FIELD_REGION,
            anchor=SourceSpan(offset, offset),
        )
        for offset in range(0, len(source), 9)
    )
    visits = 0

    def observe(event: str, amount: int) -> None:
        nonlocal visits
        if event == "parent_segment_visit":
            visits += amount

    monkeypatch.setattr(reload_source_map, "_WORK_OBSERVER", observe)

    mapped = reload_source_map.materialize_worker_reload_source(parent, insertions)

    assert mapped.artifact.line_ending_kind == "lf"
    assert visits <= len(parent_segments) * 3 + len(insertions) * 3


def test_reload_plan_scans_source_for_line_context_once(monkeypatch):
    source = "".join(
        f"Процедура P{index}()\n    Альфа.X();\nКонецПроцедуры\n"
        for index in range(20)
    )
    unit, _, _ = _unit_and_plan(source)
    plan = resolve_worker_dependencies(
        parse_full_ast_module(source),
        _catalog("Альфа"),
    )
    observed: list[int] = []

    def observe(event: str, width: int) -> None:
        if event == "reload_plan_source_scan":
            observed.append(width)

    monkeypatch.setattr(source_maps, "_TEXT_WORK_OBSERVER", observe)

    lower_resolved_worker_module(unit, plan)

    assert observed == [len(source)]
