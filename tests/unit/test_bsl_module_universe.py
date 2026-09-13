from dataclasses import replace
from hashlib import sha256
import subprocess
import sys

import pytest

import onec_runtime.bsl.module_universe as module_universe
from onec_runtime.bsl import (
    CommonModuleCatalogSnapshot,
    CommonModuleDescriptor,
    CommonModuleScope,
    ModuleUniverseAdmissionError,
    SourceSpan,
    SourceUnitKind,
    SourceUnitRef,
    WorkerModuleUnit,
    mapped_visible_source,
    source_sha256,
)
from onec_runtime.bsl.source_maps import (
    MappingRelation,
    SourceArtifactKind,
    compose_source_maps,
)
from onec_runtime.bsl.module_universe import (
    AMBIGUOUS_BINDING,
    DEPENDENCY_ALIAS_DECLARATION_REGION,
    DEPENDENCY_ALIAS_INITIALIZER_REGION,
    DEPENDENCY_FIELD_REGION,
    DYNAMIC_EXECUTE,
    LoweredWorkerModule,
    MODULE_SCOPE_DEPENDENCY,
    analyze_worker_module,
    dependency_export_name,
    lower_worker_module,
)
from onec_runtime.bsl.parser_target import PythonParserTarget, parse_raw_module
from onec_runtime.performance_profile import PhaseRecorder


def _catalog(*names: str) -> CommonModuleCatalogSnapshot:
    if not names:
        names = ("КадровыйУчет", "ОбщегоНазначения")
    return CommonModuleCatalogSnapshot.create(
        profile="server-zup-8.3.27",
        preprocessor_profile="server",
        revision=1,
        modules=tuple(
            CommonModuleDescriptor(name, CommonModuleScope.SERVER)
            for name in names
        ),
    )


def _mapped_module(
    source: str = "секрет-модуля",
    *,
    name: str = "КадровыйУчет",
    kind: SourceUnitKind = SourceUnitKind.MODULE,
    revision: int = 7,
):
    return mapped_visible_source(
        source,
        SourceUnitRef(kind, name, revision, source_sha256(source)),
    )


def _unit_for_catalog(
    source: str,
    _catalog: CommonModuleCatalogSnapshot,
    *,
    name: str = "МодульРасчета",
) -> WorkerModuleUnit:
    mapped = _mapped_module(source, name=name)
    return WorkerModuleUnit(name, "module", 7, mapped)


@pytest.fixture
def catalog() -> CommonModuleCatalogSnapshot:
    return _catalog()


@pytest.fixture
def parser() -> PythonParserTarget:
    return PythonParserTarget.from_generated()


@pytest.fixture
def unit_factory(catalog: CommonModuleCatalogSnapshot):
    def create(source: str, *, name: str = "МодульРасчета") -> WorkerModuleUnit:
        mapped = _mapped_module(source, name=name)
        return WorkerModuleUnit(name, "module", 7, mapped)

    return create


@pytest.fixture
def unit(unit_factory):
    return unit_factory(
        "Функция Проверить() Экспорт\n"
        "Результат = КадровыйУчет.Рассчитать();\n"
        "Ссылка = ОбщегоНазначения.Значение;\n"
        "Возврат КадровыйУчет;\n"
        "КонецФункции"
    )


def test_analysis_discovers_dependencies_independent_of_override_membership(
    unit,
    catalog,
    parser,
) -> None:
    analyzed = analyze_worker_module(unit, catalog, parser)

    assert [
        (item.target_module, item.export_variable) for item in analyzed.dependencies
    ] == [
        ("КадровыйУчет", dependency_export_name("КадровыйУчет")),
        ("ОбщегоНазначения", dependency_export_name("ОбщегоНазначения")),
    ]
    assert {use.category for binding in analyzed.dependencies for use in binding.uses} == {
        "call",
        "access",
        "value",
    }


def test_analysis_and_lowering_profile_one_parse_and_distinct_phases(
    unit,
    catalog,
    parser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: telemetry must not reparse transformed source diagnostically."""
    parse_calls: list[str] = []
    real_parse = module_universe.parse_raw_module

    def counted_parse(source: str, target: PythonParserTarget):
        parse_calls.append(source)
        return real_parse(source, target)

    monkeypatch.setattr(module_universe, "parse_raw_module", counted_parse)
    profiler = PhaseRecorder()

    analyzed = analyze_worker_module(unit, catalog, parser, profiler=profiler)
    lowered = lower_worker_module(analyzed, profiler=profiler)

    assert parse_calls == [unit.mapped_source.text]
    assert [event.phase for event in profiler.events] == [
        "semantic_parse",
        "dependency_analysis",
        "alias_transform",
        "source_map_composition",
        "admission",
    ]
    assert lowered.analysis is analyzed


class _CountingIdentifier(str):
    def __new__(cls, value: str):
        item = super().__new__(cls, value)
        item.casefold_calls = 0
        return item

    def casefold(self) -> str:
        self.casefold_calls += 1
        return super().casefold()


def _large_dependency_source(
    *, module_count: int, identifier_count: int
) -> tuple[str, tuple[_CountingIdentifier, ...]]:
    assert identifier_count >= 1 + module_count * 2
    module_names = tuple(f"Модуль{number:03}" for number in range(module_count))
    filler_count = identifier_count - 1 - module_count * 2
    source = "\n".join(
        (
            "Процедура Проверить()",
            *(f"{name}.Вызвать();" for name in module_names),
            *(f"Локальная{number:05} = {number};" for number in range(filler_count)),
            "КонецПроцедуры",
        )
    )
    _, tokens = parse_raw_module(source, PythonParserTarget.from_generated())
    counting_ids = tuple(
        _CountingIdentifier(token.text) for token in tokens if token.type == "ID"
    )
    assert len(counting_ids) == identifier_count
    return source, counting_ids


def test_dependency_collision_index_casefolds_each_identifier_once() -> None:
    """Break caught: rescanning identifiers per dependency is quadratic."""
    source, counting_ids = _large_dependency_source(
        module_count=100,
        identifier_count=10_000,
    )
    root, tokens = parse_raw_module(source, PythonParserTarget.from_generated())
    counting_iter = iter(counting_ids)
    counted_tokens = tuple(
        replace(token, text=next(counting_iter)) if token.type == "ID" else token
        for token in tokens
    )

    analyzed = module_universe._analyze_parsed_worker_module(
        _unit_for_catalog(source, _catalog(), name="ТестовыйМодуль"),
        _catalog(*(f"Модуль{number:03}" for number in range(100))),
        root,
        counted_tokens,
    )

    assert tuple(binding.target_module for binding in analyzed.dependencies) == tuple(
        f"Модуль{number:03}" for number in range(100)
    )
    assert all(len(binding.uses) == 1 for binding in analyzed.dependencies)
    assert sum(item.casefold_calls for item in counting_ids) <= 10_100


@pytest.mark.parametrize(
    ("source", "executable"),
    (
        ("Функция Пусто()\nКонецФункции", ""),
        ("Функция Пусто();\nКонецФункции", ""),
        (
            "Функция СТочкойСЗапятой();\n"
            "Возврат КадровыйУчет.Рассчитать();\n"
            "КонецФункции",
            "Возврат КадровыйУчет.Рассчитать();",
        ),
        (
            "Функция СТочкойСЗапятойCRLF();\r\n"
            "Возврат КадровыйУчет.Рассчитать();\r\n"
            "КонецФункции",
            "Возврат КадровыйУчет.Рассчитать();",
        ),
        (
            "Функция СТочкойПеребор();\n"
            "Для Каждого Элемент Из Коллекция Цикл\n"
            "КонецЦикла;\n"
            "КонецФункции",
            "Для Каждого Элемент Из Коллекция Цикл\nКонецЦикла;",
        ),
        (
            "Функция СТочкойПереборCRLF();\r\n"
            "Для Каждого Элемент Из Коллекция Цикл\r\n"
            "КонецЦикла;\r\n"
            "КонецФункции",
            "Для Каждого Элемент Из Коллекция Цикл\r\nКонецЦикла;",
        ),
        (
            "Функция СТочкойДиапазон();\n"
            "Для Номер = 1 По 2 Цикл\n"
            "КонецЦикла;\n"
            "КонецФункции",
            "Для Номер = 1 По 2 Цикл\nКонецЦикла;",
        ),
        (
            "Функция Обход()\n"
            "Для Каждого Элемент Из Коллекция Цикл\n"
            "КонецЦикла;\n"
            "КонецФункции",
            "Для Каждого Элемент Из Коллекция Цикл\nКонецЦикла;",
        ),
        (
            "Функция СЛокальной()\n"
            "Перем Результат;\n"
            "Возврат КадровыйУчет.Рассчитать();\n"
            "КонецФункции",
            "Возврат КадровыйУчет.Рассчитать();",
        ),
        (
            "Функция СДирективой()\n"
            "#Если Сервер Тогда\n"
            "Возврат КадровыйУчет.Рассчитать();\n"
            "#КонецЕсли\n"
            "КонецФункции",
            "Возврат КадровыйУчет.Рассчитать();",
        ),
        (
            "Функция CRLF()\r\n"
            "Перем Результат;\r\n"
            "Возврат КадровыйУчет.Рассчитать();\r\n"
            "КонецФункции",
            "Возврат КадровыйУчет.Рассчитать();",
        ),
    ),
)
def test_method_analysis_tracks_only_executable_statement_boundaries(
    source: str,
    executable: str,
    unit_factory,
    catalog,
    parser,
) -> None:
    """Break caught: delta admission would accept edits outside executable code."""
    analyzed = analyze_worker_module(unit_factory(source), catalog, parser)

    assert type(analyzed.context).__name__ == "WorkerModuleAnalysisContext"
    assert len(analyzed.methods) == 1
    method = analyzed.methods[0]
    assert type(method).__name__ == "WorkerMethodAnalysis"
    assert source[method.executable_body.start : method.executable_body.end] == executable
    assert method.executable_body.start >= method.declarations_end
    assert "КонецФункции" in source[method.executable_body.end :]
    assert method.signature_sha256 == sha256(
        source[method.method_declaration.start : method.executable_body.start].encode(
            "utf-8"
        )
    ).hexdigest()


def test_parameter_and_local_shadowing_are_not_dependencies(
    unit_factory,
    catalog,
    parser,
) -> None:
    unit = unit_factory(
        "Функция А(КадровыйУчет)\n"
        "Перем ОбщегоНазначения;\n"
        "Возврат КадровыйУчет.Метод();\n"
        "КонецФункции"
    )

    assert analyze_worker_module(unit, catalog, parser).dependencies == ()


def test_analysis_rejects_an_unknown_qualified_target_with_private_diagnostics(
    unit_factory,
    catalog,
    parser,
) -> None:
    source = (
        "Функция Проверить()\n"
        "// sentinel-secret-source-fragment\n"
        "Возврат Неизвестный.Метод();\n"
        "КонецФункции"
    )

    with pytest.raises(ModuleUniverseAdmissionError) as caught:
        analyze_worker_module(unit_factory(source), catalog, parser)

    target = "Неизвестный.Метод()"
    assert caught.value.code == AMBIGUOUS_BINDING
    assert caught.value.span == SourceSpan(
        source.index(target),
        source.index(target) + len(target),
    )
    assert "sentinel-secret-source-fragment" not in str(caught.value)


def test_known_module_member_is_admitted_without_method_catalog(
    unit_factory,
    catalog,
    parser,
) -> None:
    source = (
        "Функция Проверить()\n"
        "Возврат КадровыйУчет.Скрытый();\n"
        "КонецФункции"
    )

    analysis = analyze_worker_module(unit_factory(source), catalog, parser)

    assert analysis.dependencies[0].target_module == "КадровыйУчет"


def test_analysis_rejects_dependency_use_in_executable_module_scope(
    unit_factory,
    catalog,
    parser,
) -> None:
    source = "КадровыйУчет.Рассчитать();"

    with pytest.raises(ModuleUniverseAdmissionError) as caught:
        analyze_worker_module(unit_factory(source), catalog, parser)

    assert caught.value.code == MODULE_SCOPE_DEPENDENCY
    assert caught.value.span == SourceSpan(0, len(source) - 1)


def test_analysis_rejects_module_scope_unknown_qualified_root(
    unit_factory,
    catalog,
    parser,
) -> None:
    source = "Неизвестный.Метод();"

    with pytest.raises(ModuleUniverseAdmissionError) as caught:
        analyze_worker_module(unit_factory(source), catalog, parser)

    assert caught.value.code == AMBIGUOUS_BINDING
    assert caught.value.span == SourceSpan(0, len(source) - 1)


@pytest.mark.parametrize(
    "root",
    (
        "ЧастиДаты",
        "ОбходРезультатаЗапроса",
        "ВидСравненияКомпоновкиДанных",
        "ТипГруппыЭлементовОтбораКомпоновкиДанных",
        "ЦветаСтиля",
    ),
)
def test_confirmed_platform_namespace_is_not_a_dependency(root, parser) -> None:
    source = f"Функция Проверить()\nВозврат {root}.Значение;\nКонецФункции"

    analysis = analyze_worker_module(
        _unit_for_catalog(source, _catalog()), _catalog(), parser
    )

    assert analysis.dependencies == ()


@pytest.mark.parametrize(
    "statement",
    ('Выполнить("Сообщить(1)");', "Выполнить(Текст);"),
)
def test_analysis_rejects_every_dynamic_execute(
    statement,
    unit_factory,
    catalog,
    parser,
) -> None:
    source = f"Процедура Проверить(Текст)\n{statement}\nКонецПроцедуры"

    with pytest.raises(ModuleUniverseAdmissionError) as caught:
        analyze_worker_module(unit_factory(source), catalog, parser)

    assert caught.value.code == DYNAMIC_EXECUTE
    assert caught.value.span == SourceSpan(
        source.index("Выполнить"),
        source.index(";"),
    )


def test_analysis_rejects_generated_dependency_name_collision(
    unit_factory,
    catalog,
    parser,
) -> None:
    generated = dependency_export_name("КадровыйУчет")
    source = (
        f"Перем {generated};\n"
        "Функция Проверить()\n"
        "Возврат КадровыйУчет.Рассчитать();\n"
        "КонецФункции"
    )

    with pytest.raises(ModuleUniverseAdmissionError) as caught:
        analyze_worker_module(unit_factory(source), catalog, parser)

    assert caught.value.code == AMBIGUOUS_BINDING
    assert caught.value.span == SourceSpan(
        source.index(generated),
        source.index(generated) + len(generated),
    )


def test_analysis_rejects_generated_name_collision_between_dependencies(
    monkeypatch,
    unit_factory,
    catalog,
    parser,
) -> None:
    import onec_runtime.bsl.module_universe as module_universe

    monkeypatch.setattr(
        module_universe,
        "dependency_export_name",
        lambda _canonical_name: "__OnecDependency_forced",
    )
    source = (
        "Функция Проверить()\n"
        "КадровыйУчет.Рассчитать();\n"
        "ОбщегоНазначения.Значение();\n"
        "КонецФункции"
    )

    with pytest.raises(ModuleUniverseAdmissionError) as caught:
        analyze_worker_module(unit_factory(source), catalog, parser)

    second = "ОбщегоНазначения.Значение()"
    assert caught.value.code == AMBIGUOUS_BINDING
    assert caught.value.span == SourceSpan(
        source.index(second),
        source.index(second) + len(second),
    )


def test_assignments_and_loop_variables_shadow_catalog_names(
    unit_factory,
    catalog,
    parser,
) -> None:
    source = (
        "Функция Проверить(Коллекция)\n"
        "КадровыйУчет = Новый Структура;\n"
        "Для Каждого ОбщегоНазначения Из Коллекция Цикл\n"
        "КадровыйУчет.Рассчитать();\n"
        "ОбщегоНазначения.Значение();\n"
        "КонецЦикла;\n"
        "КонецФункции"
    )

    assert analyze_worker_module(unit_factory(source), catalog, parser).dependencies == ()


def test_self_qualified_calls_are_static_dependencies(catalog, parser) -> None:
    source = (
        "Функция Проверить()\n"
        "Возврат кадровыйучет.Рассчитать();\n"
        "КонецФункции"
    )
    unit = _unit_for_catalog(source, catalog, name="КадровыйУчет")

    analyzed = analyze_worker_module(unit, catalog, parser)

    assert tuple(binding.target_module for binding in analyzed.dependencies) == (
        "КадровыйУчет",
    )


def test_qualified_catalog_call_is_not_shadowed_by_same_named_internal_method(
    unit_factory,
    catalog,
    parser,
) -> None:
    source = (
        "Процедура КадровыйУчет()\n"
        "КонецПроцедуры\n"
        "Функция Проверить()\n"
        "Возврат КадровыйУчет.Рассчитать();\n"
        "КонецФункции"
    )

    analyzed = analyze_worker_module(unit_factory(source), catalog, parser)

    assert tuple(binding.target_module for binding in analyzed.dependencies) == (
        "КадровыйУчет",
    )


def test_internal_method_result_chain_is_not_a_module_dependency(
    unit_factory,
    catalog,
    parser,
) -> None:
    source = (
        "Функция Получить()\n"
        "Возврат Новый Структура;\n"
        "КонецФункции\n"
        "Функция Проверить()\n"
        "Возврат Получить().Свойство;\n"
        "КонецФункции"
    )

    analyzed = analyze_worker_module(unit_factory(source), catalog, parser)

    assert analyzed.dependencies == ()


def test_same_named_internal_method_result_chain_is_not_catalog_dependency(
    unit_factory,
    catalog,
    parser,
) -> None:
    source = (
        "Функция КадровыйУчет()\n"
        "Возврат Новый Структура;\n"
        "КонецФункции\n"
        "Функция Проверить()\n"
        "Возврат КадровыйУчет().Свойство;\n"
        "КонецФункции"
    )

    analyzed = analyze_worker_module(unit_factory(source), catalog, parser)

    assert analyzed.dependencies == ()


def test_analysis_rejects_exported_module_variable_colliding_with_catalog_module(
    unit_factory,
    catalog,
    parser,
) -> None:
    source = (
        "Перем КадровыйУчет Экспорт;\n"
        "Функция Проверить()\n"
        "Возврат КадровыйУчет.Рассчитать();\n"
        "КонецФункции"
    )

    with pytest.raises(ModuleUniverseAdmissionError) as caught:
        analyze_worker_module(unit_factory(source), catalog, parser)

    assert caught.value.code == AMBIGUOUS_BINDING
    assert caught.value.span == SourceSpan(len("Перем "), source.index(";"))


def test_analysis_rejects_case_insensitive_duplicate_methods(
    unit_factory,
    catalog,
    parser,
) -> None:
    source = (
        "Процедура Проверить()\nКонецПроцедуры\n"
        "Процедура проверить()\nКонецПроцедуры"
    )

    with pytest.raises(ModuleUniverseAdmissionError) as caught:
        analyze_worker_module(unit_factory(source), catalog, parser)

    assert caught.value.code == AMBIGUOUS_BINDING
    assert caught.value.span == SourceSpan(
        source.index("Процедура проверить"),
        len(source),
    )


def test_platform_managers_are_not_module_dependencies(
    unit_factory,
    catalog,
    parser,
) -> None:
    source = (
        "Функция Проверить()\n"
        "Возврат Справочники.Сотрудники.НайтиПоКоду(1);\n"
        "КонецФункции"
    )

    assert analyze_worker_module(unit_factory(source), catalog, parser).dependencies == ()


def test_analysis_records_exports_and_distinct_structural_insertion_points(
    unit_factory,
    catalog,
    parser,
) -> None:
    source = (
        "Функция СПеременной() Экспорт\n"
        "Перем Результат;\n"
        "Возврат КадровыйУчет.Рассчитать();\n"
        "КонецФункции\n"
        "Процедура БезПеременной()\n"
        "ОбщегоНазначения.Значение();\n"
        "КонецПроцедуры"
    )

    analyzed = analyze_worker_module(unit_factory(source), catalog, parser)

    assert analyzed.exported_methods == ("СПеременной",)
    first, second = analyzed.method_insertion_points
    assert first.declarations_end == source.index(";", source.index("Перем Результат")) + 1
    assert first.first_statement_start == source.index("Возврат")
    assert second.declarations_end == second.first_statement_start == source.index(
        "ОбщегоНазначения"
    )
    assert "КадровыйУчет.Рассчитать" not in repr(analyzed)


def test_alias_declarations_precede_all_initializers(
    unit_factory,
    catalog,
    parser,
) -> None:
    source = (
        "Функция А()\n"
        "Перем Первый, Второй;\n"
        "Возврат КадровыйУчет.Рассчитать();\n"
        "КонецФункции"
    )

    lowered = lower_worker_module(
        analyze_worker_module(unit_factory(source), catalog, parser)
    )

    declaration = lowered.mapped_source.text.index("Перем КадровыйУчет;")
    original_declaration = lowered.mapped_source.text.index("Перем Первый, Второй;")
    initializer = lowered.mapped_source.text.index(
        "КадровыйУчет = __OnecDependency_"
    )
    original_statement = lowered.mapped_source.text.index(
        "Возврат КадровыйУчет.Рассчитать()"
    )
    assert original_declaration < declaration < initializer < original_statement


def test_coincident_alias_edits_have_explicit_canonical_order(
    unit_factory,
    catalog,
    parser,
) -> None:
    source = (
        "Функция А()\n"
        "Возврат ОбщегоНазначения.Значение() + "
        "КадровыйУчет.Рассчитать();\n"
        "КонецФункции"
    )

    lowered = lower_worker_module(
        analyze_worker_module(unit_factory(source), catalog, parser)
    )
    text = lowered.mapped_source.text

    kadry_field = text.index(
        f"Перем {dependency_export_name('КадровыйУчет')} Экспорт;"
    )
    common_field = text.index(
        f"Перем {dependency_export_name('ОбщегоНазначения')} Экспорт;"
    )
    kadry_declaration = text.index("Перем КадровыйУчет;")
    common_declaration = text.index("Перем ОбщегоНазначения;")
    kadry_initializer = text.index("КадровыйУчет = __OnecDependency_")
    common_initializer = text.index("ОбщегоНазначения = __OnecDependency_")
    original_statement = text.index("Возврат ")

    assert kadry_field < common_field
    assert kadry_declaration < common_declaration < kadry_initializer
    assert kadry_initializer < common_initializer < original_statement


@pytest.mark.parametrize(
    "source",
    (
        (
            "Функция БезПерем()\n"
            "Возврат КадровыйУчет.Рассчитать();\n"
            "КонецФункции"
        ),
        (
            "Функция ДваОбъявления()\n"
            "Перем Первый;\n"
            "Перем Второй;\n"
            "Возврат КадровыйУчет.Рассчитать();\n"
            "КонецФункции"
        ),
        (
            "Функция СДирективой()\n"
            "#Если Сервер Тогда\n"
            "Перем Первый;\n"
            "#КонецЕсли\n"
            "Возврат КадровыйУчет.Рассчитать();\n"
            "КонецФункции"
        ),
        (
            "Функция CRLF()\r\n"
            "Перем Первый;\r\n"
            "Возврат КадровыйУчет.Рассчитать();\r\n"
            "КонецФункции"
        ),
        (
            "Функция ДвеЗависимости()\n"
            "Возврат КадровыйУчет.Рассчитать() + "
            "ОбщегоНазначения.Значение();\n"
            "КонецФункции"
        ),
    ),
)
def test_lowered_supported_shapes_parse_with_committed_generated_parser(
    source,
    unit_factory,
    catalog,
    parser,
) -> None:
    lowered = lower_worker_module(
        analyze_worker_module(unit_factory(source), catalog, parser)
    )

    parse_raw_module(lowered.mapped_source.text, PythonParserTarget.from_generated())
    assert lowered.mapped_source.artifact.kind is SourceArtifactKind.WORKER_MODULE
    assert "\r" not in lowered.mapped_source.text


def test_alias_declaration_precedes_a_trailing_method_header_semicolon(
    unit_factory,
    catalog,
    parser,
) -> None:
    source = (
        "Процедура Проверить(Значение);\n"
        "КадровыйУчет.Рассчитать(Значение);\n"
        "КонецПроцедуры"
    )

    lowered = lower_worker_module(
        analyze_worker_module(unit_factory(source), catalog, parser)
    )

    parse_raw_module(lowered.mapped_source.text, PythonParserTarget.from_generated())
    text = lowered.mapped_source.text
    declaration = text.index("Перем КадровыйУчет;")
    initializer = text.index("КадровыйУчет = __OnecDependency_")
    original_statement = text.index("КадровыйУчет.Рассчитать(Значение)")
    header_semicolon = text.rfind(";", 0, original_statement)
    assert declaration < initializer < header_semicolon < original_statement


def test_alias_initializer_precedes_the_complete_for_each_prefix(
    unit_factory,
    catalog,
    parser,
) -> None:
    source = (
        "Процедура Проверить()\n"
        "Для Каждого Элемент Из КадровыйУчет.Получить() Цикл\n"
        "КонецЦикла;\n"
        "КонецПроцедуры"
    )

    lowered = lower_worker_module(
        analyze_worker_module(unit_factory(source), catalog, parser)
    )

    parse_raw_module(lowered.mapped_source.text, PythonParserTarget.from_generated())
    text = lowered.mapped_source.text
    initializer = text.index("КадровыйУчет = __OnecDependency_")
    loop = text.index("Для Каждого")
    assert initializer < loop


def test_self_dependency_lowers_to_an_ordinary_alias(catalog, parser) -> None:
    source = (
        "Функция Проверить()\n"
        "Возврат кадровыйучет.Рассчитать();\n"
        "КонецФункции"
    )
    unit = _unit_for_catalog(source, catalog, name="КадровыйУчет")

    lowered = lower_worker_module(analyze_worker_module(unit, catalog, parser))

    assert "Перем КадровыйУчет;" in lowered.mapped_source.text
    assert (
        f"КадровыйУчет = {dependency_export_name('КадровыйУчет')};"
        in lowered.mapped_source.text
    )
    parse_raw_module(lowered.mapped_source.text, PythonParserTarget.from_generated())


def test_dependency_lowering_emits_stable_exact_generated_and_synthetic_maps(
    unit_factory,
    catalog,
    parser,
) -> None:
    source = (
        "Функция Проверить()\r\n"
        "Возврат КадровыйУчет.Рассчитать() + "
        "ОбщегоНазначения.Значение();\r\n"
        "КонецФункции"
    )
    analysis = analyze_worker_module(unit_factory(source), catalog, parser)

    lowered = lower_worker_module(analysis)

    assert isinstance(lowered, LoweredWorkerModule)
    assert len(lowered.dependency_bindings_sha256) == 64
    assert lowered.transform_version == "module-universe-v1"
    assert lowered.mapped_source.artifact.worker_generation is None
    assert lowered.mapped_source.artifact.worker_manifest_sha256 is None
    assert repr(lowered).find("КадровыйУчет.Рассчитать") == -1

    regions = {
        segment.synthetic_region
        for segment in lowered.mapped_source.source_map.segments
        if segment.synthetic_region is not None
    }
    assert {
        DEPENDENCY_FIELD_REGION,
        DEPENDENCY_ALIAS_DECLARATION_REGION,
        DEPENDENCY_ALIAS_INITIALIZER_REGION,
    } <= regions

    first_binding = analysis.dependencies[0]
    field_offset = lowered.mapped_source.text.index(first_binding.export_variable)
    field_mapping = lowered.mapped_source.source_map.map_offset(field_offset)
    assert field_mapping.relation is MappingRelation.SYNTHETIC
    assert field_mapping.synthetic_region == DEPENDENCY_FIELD_REGION
    assert field_mapping.anchor_span == first_binding.uses[0].span

    declaration_offset = lowered.mapped_source.text.index(
        f"Перем {first_binding.target_module};"
    )
    initializer_offset = lowered.mapped_source.text.index(
        f"{first_binding.target_module} = {first_binding.export_variable};"
    )
    for offset, region in (
        (declaration_offset, DEPENDENCY_ALIAS_DECLARATION_REGION),
        (initializer_offset, DEPENDENCY_ALIAS_INITIALIZER_REGION),
    ):
        mapped = lowered.mapped_source.source_map.map_offset(offset)
        assert mapped.relation is MappingRelation.DERIVED
        assert mapped.origin_span == first_binding.uses[0].span
        segment = next(
            item
            for item in lowered.mapped_source.source_map.segments
            if item.generated.start <= offset < item.generated.end
        )
        assert segment.synthetic_region == region
        assert segment.anchor_span == first_binding.uses[0].method_declaration

    for call in ("КадровыйУчет.Рассчитать()", "ОбщегоНазначения.Значение()"):
        generated_start = lowered.mapped_source.text.index(call)
        original_start = source.index(call)
        for delta in (0, len(call) - 1):
            mapped = lowered.mapped_source.source_map.map_offset(generated_start + delta)
            assert mapped.relation is MappingRelation.EXACT
            assert mapped.origin_span == SourceSpan(
                original_start + delta,
                original_start + delta + 1,
            )


def test_aliases_for_alternate_and_nested_directive_branches_are_unconditional(
    unit_factory,
    catalog,
    parser,
) -> None:
    source = (
        "Функция А()\n"
        "#Если Клиент Тогда\n"
        "    Сообщить(\"client\");\n"
        "#Иначе\n"
        "    #Если Сервер Тогда\n"
        "        Возврат КадровыйУчет.Рассчитать();\n"
        "    #КонецЕсли\n"
        "#КонецЕсли\n"
        "КонецФункции"
    )
    analysis = analyze_worker_module(unit_factory(source), catalog, parser)

    lowered = lower_worker_module(analysis)

    text = lowered.mapped_source.text
    outer_directive = text.index("#Если Клиент")
    method = text.index("Функция А()")
    field = text.index(dependency_export_name("КадровыйУчет"))
    declaration = text.index("Перем КадровыйУчет;")
    initializer = text.index("КадровыйУчет = __OnecDependency_")
    assert field < method < declaration < initializer < outer_directive
    parse_raw_module(text, PythonParserTarget.from_generated())


def test_published_worker_module_map_is_exact_recomposition_of_lineage(
    unit_factory,
    catalog,
    parser,
) -> None:
    source = (
        "Функция А()\r\n"
        "Возврат КадровыйУчет.Рассчитать();\r\n"
        "КонецФункции"
    )
    analysis = analyze_worker_module(unit_factory(source), catalog, parser)

    lowered = lower_worker_module(analysis)

    recomposed = analysis.unit.mapped_source.source_map
    for local in lowered.mapped_source.lineage:
        recomposed = compose_source_maps(local, recomposed)
    assert recomposed == lowered.mapped_source.source_map
    alias = next(
        segment
        for segment in recomposed.segments
        if segment.synthetic_region == DEPENDENCY_ALIAS_INITIALIZER_REGION
    )
    assert alias.origin == analysis.dependencies[0].uses[0].span
    assert alias.anchor_span == analysis.dependencies[0].uses[0].method_declaration


def test_analysis_method_span_includes_decorations_and_async(
    unit_factory,
    catalog,
    parser,
) -> None:
    source = (
        "&Запуск\n"
        "Асинх Функция Проверить() Экспорт\n"
        "Возврат КадровыйУчет.Рассчитать();\n"
        "КонецФункции"
    )

    analyzed = analyze_worker_module(unit_factory(source), catalog, parser)

    assert analyzed.method_insertion_points[0].method_declaration == SourceSpan(
        0,
        len(source),
    )
    assert analyzed.dependencies[0].uses[0].method_declaration == SourceSpan(
        0,
        len(source),
    )


def test_catalog_digest_is_order_and_case_stable() -> None:
    first = _catalog()
    second = CommonModuleCatalogSnapshot.create(
        profile="server-zup-8.3.27",
        preprocessor_profile="server",
        revision=1,
        modules=tuple(reversed(first.modules)),
    )

    assert first.sha256 == second.sha256
    assert first.require("кадровыйучет").canonical_name == "КадровыйУчет"


def test_errors_import_without_initializing_the_catalog_module() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from onec_runtime.errors import RecoveryIdentityMismatch",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_worker_module_unit_requires_matching_visible_module_identity() -> None:
    source = "секрет-модуля"
    mapped = _mapped_module(source)

    unit = WorkerModuleUnit("КадровыйУчет", "module", 7, mapped)

    assert unit.logical_name == "КадровыйУчет"
    with pytest.raises(ModuleUniverseAdmissionError, match="worker module source identity"):
        WorkerModuleUnit("ДругоеИмя", "module", 7, mapped)


def test_worker_module_unit_and_admission_errors_do_not_reveal_source_text() -> None:
    source = "sentinel-secret-worker-source"
    mapped = _mapped_module(source)
    unit = WorkerModuleUnit("КадровыйУчет", "module", 7, mapped)

    assert source not in repr(unit)
    with pytest.raises(ModuleUniverseAdmissionError) as error:
        WorkerModuleUnit("ДругоеИмя", "module", 7, mapped)
    assert source not in str(error.value)
