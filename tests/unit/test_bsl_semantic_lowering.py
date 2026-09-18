from __future__ import annotations

import pytest

from onec_runtime.bsl.parser_target import PythonParserTarget
from onec_runtime.bsl.source_maps import (
    MappedSource,
    MappingRelation,
    SourceArtifactKind,
    SourceMap,
    SourceSpan,
    SourceUnitKind,
    SourceUnitRef,
    mapped_visible_source,
    source_sha256,
)


@pytest.fixture(scope="module")
def parser_target() -> PythonParserTarget:
    return PythonParserTarget.from_generated()


def _mapped(source: str, unit_id: str, revision: int) -> MappedSource:
    return mapped_visible_source(
        source,
        SourceUnitRef(
            SourceUnitKind.NOTEBOOK_CELL,
            unit_id,
            revision,
            source_sha256(source),
        ),
    )


def test_old_context_names_are_ordinary_notebook_variables(
    parser_target: PythonParserTarget,
) -> None:
    from onec_runtime.bsl import LoweringMode, SemanticNotebookLowerer

    result = SemanticNotebookLowerer(parser_target).lower(
        "Контекст = 1; КонтекстОтладки = 2; "
        "Результат = Контекст + КонтекстОтладки;",
        mode=LoweringMode.MAIN,
    )

    assert result.context_names == ("Контекст", "КонтекстОтладки")
    assert 'e1cRuntimeКонтекст.Вставить("Контекст", 1)' in result.source
    assert 'e1cRuntimeКонтекст.Вставить("КонтекстОтладки", 2)' in result.source
    assert 'e1cRuntimeКонтекст.Контекст + e1cRuntimeКонтекст.КонтекстОтладки' in result.source


def test_capture_namespace_uses_new_runtime_name(
    parser_target: PythonParserTarget,
) -> None:
    from onec_runtime.bsl import LoweringMode, SemanticNotebookLowerer

    result = SemanticNotebookLowerer(parser_target).lower(
        "e1cRuntimeКонтекстОтладки.Счетчик = 2;",
        mode=LoweringMode.CAPTURE,
    )

    assert result.dirty_roots == ("Счетчик",)
    assert result.source == "e1cRuntimeКонтекстОтладки.Счетчик = 2;"


def test_lowers_persistent_assignment_worker_export_and_message_sink(
    parser_target: PythonParserTarget,
) -> None:
    from onec_runtime.bsl import SemanticNotebookLowerer as PublicLowerer
    from onec_runtime.bsl.semantic_lowering import (
        LoweringMode,
        SemanticNotebookLowerer,
        WorkerExport,
    )

    lowerer = SemanticNotebookLowerer(
        parser_target,
        worker_exports=(
            WorkerExport("Расчет.Ндфл.Посчитать", "Посчитать"),
        ),
    )
    assert PublicLowerer is SemanticNotebookLowerer

    result = lowerer.lower(
        "ГДФЛ = Расчет.Ндфл.Посчитать(); Сообщить(ГДФЛ);",
        mode=LoweringMode.MAIN,
    )

    assert result.source == (
        'e1cRuntimeКонтекст.Вставить("ГДФЛ", e1cRuntimeКонтекст.RuntimeWorker.Посчитать()); '
        "e1cRuntimeКонтекст.__onec_cell_messages.Добавить(Строка(e1cRuntimeКонтекст.ГДФЛ));"
    )
    assert result.context_names == ("ГДФЛ",)
    assert result.dirty_roots == ()
    assert result.persistent_write_roots == ("ГДФЛ",)
    assert result.worker_dependencies == ("Расчет.Ндфл.Посчитать",)
    assert result.messages_intercepted == 1
    parser_target.parse(result.source, "БлокНоутбука")


def test_message_interception_preserves_optional_status_argument(
    parser_target: PythonParserTarget,
) -> None:
    from onec_runtime.bsl import LoweringMode, SemanticNotebookLowerer

    result = SemanticNotebookLowerer(parser_target).lower(
        'Сообщить("готово", СтатусСообщения.Важное);',
        mode=LoweringMode.MAIN,
    )

    assert result.source == (
        'e1cRuntimeКонтекст.__onec_cell_messages.Добавить(Строка("готово"));'
    )


def test_binds_persistent_and_captured_contexts_without_aliasing(
    parser_target: PythonParserTarget,
) -> None:
    from onec_runtime.bsl.semantic_lowering import (
        LoweringMode,
        SemanticNotebookLowerer,
    )

    source = (
        'e1cRuntimeКонтекстОтладки.Результат.Добавить("x"); '
        "Скаляр = e1cRuntimeКонтекстОтладки.Скаляр; "
        "e1cRuntimeКонтекстОтладки.Скаляр = Скаляр + 1;"
    )
    result = SemanticNotebookLowerer(parser_target).lower(
        source,
        mode=LoweringMode.CAPTURE,
    )

    assert result.source == (
        'e1cRuntimeКонтекстОтладки.Результат.Добавить("x"); '
        'e1cRuntimeКонтекст.Вставить("Скаляр", e1cRuntimeКонтекстОтладки.Скаляр); '
        "e1cRuntimeКонтекстОтладки.Скаляр = e1cRuntimeКонтекст.Скаляр + 1;"
    )
    assert result.context_names == ("Скаляр",)
    assert result.dirty_roots == ("Скаляр",)
    assert result.persistent_write_roots == ("Скаляр",)
    parser_target.parse(result.source, "БлокНоутбука")


def test_lowering_profiles_define_route_specific_result_capture_and_map_behavior(
    parser_target: PythonParserTarget,
) -> None:
    """Break caught: a route profile must control every route-specific lowering choice."""
    from onec_runtime.bsl.semantic_lowering import (
        CAPTURE_LOWERING_PROFILE,
        MAIN_LOWERING_PROFILE,
        CaptureNamespaceRule,
        LoweringProfile,
        SemanticNotebookLowerer,
    )

    main = SemanticNotebookLowerer(parser_target).lower(
        "Результат = 1;",
        profile=MAIN_LOWERING_PROFILE,
    )
    capture = SemanticNotebookLowerer(parser_target).lower(
        "РезультатИнструкции = e1cRuntimeКонтекстОтладки.Скаляр;",
        profile=CAPTURE_LOWERING_PROFILE,
    )
    preview_profile = LoweringProfile(
        result_channel="Итог",
        capture_namespace_rule=CaptureNamespaceRule.MEMBER_ROOT,
        source_map_tag="preview",
    )
    preview = SemanticNotebookLowerer(parser_target).lower(
        "Итог = e1cRuntimeКонтекстОтладки.Скаляр;",
        profile=preview_profile,
    )

    assert main.source == "Результат = 1;"
    assert main.mapped_source.artifact.mode == "main"
    assert capture.source == "РезультатИнструкции = e1cRuntimeКонтекстОтладки.Скаляр;"
    assert capture.dirty_roots == ()
    assert capture.mapped_source.artifact.mode == "capture"
    assert preview.source == "Итог = e1cRuntimeКонтекстОтладки.Скаляр;"
    assert preview.mapped_source.artifact.mode == "preview"
    assert preview_profile == LoweringProfile(
        result_channel="Итог",
        capture_namespace_rule=CaptureNamespaceRule.MEMBER_ROOT,
        source_map_tag="preview",
    )

    with pytest.raises(TypeError, match="LoweringMode"):
        SemanticNotebookLowerer(parser_target).lower(
            "Результат = 1;",
            mode="other",  # type: ignore[arg-type]
        )


def test_persistent_name_catalog_preserves_first_committed_bsl_spelling(
    parser_target: PythonParserTarget,
) -> None:
    from onec_runtime.bsl.semantic_lowering import LoweringMode, SemanticNotebookLowerer

    lowerer = SemanticNotebookLowerer(parser_target)
    lowerer.lower("КадровыеДанныеТЗ = 1;", mode=LoweringMode.MAIN)
    lowerer.lower("кадровыеданныетз = 2;", mode=LoweringMode.MAIN)

    assert lowerer.context_names == frozenset({"кадровыеданныетз"})
    assert lowerer.persistent_names == ("КадровыеДанныеТЗ",)


def test_persistent_name_catalog_uses_first_spelling_within_one_cell(
    parser_target: PythonParserTarget,
) -> None:
    from onec_runtime.bsl.semantic_lowering import LoweringMode, SemanticNotebookLowerer

    lowerer = SemanticNotebookLowerer(parser_target)
    lowerer.lower(
        "ПервоеИмя = 1; первоеимя = 2;",
        mode=LoweringMode.MAIN,
    )

    assert lowerer.persistent_names == ("ПервоеИмя",)


def test_capture_namespace_is_rejected_outside_capture_and_as_bare_alias(
    parser_target: PythonParserTarget,
) -> None:
    from onec_runtime.bsl.semantic_lowering import (
        LoweringMode,
        SemanticLoweringError,
        SemanticNotebookLowerer,
    )

    lowerer = SemanticNotebookLowerer(parser_target)
    with pytest.raises(SemanticLoweringError, match="only available in CAPTURE"):
        lowerer.lower(
            "Результат = e1cRuntimeКонтекстОтладки.Скаляр;",
            mode=LoweringMode.MAIN,
        )
    with pytest.raises(SemanticLoweringError, match="bare capture namespace"):
        lowerer.lower(
            "Результат = e1cRuntimeКонтекстОтладки;",
            mode=LoweringMode.CAPTURE,
        )


def test_span_edits_preserve_comments_and_member_access(
    parser_target: PythonParserTarget,
) -> None:
    from onec_runtime.bsl.semantic_lowering import (
        LoweringMode,
        SemanticNotebookLowerer,
    )

    source = "Результат // lhs\n= // rhs\nТаблица.Количество();"
    result = SemanticNotebookLowerer(
        parser_target,
        context_names=("Таблица",),
    ).lower(source, mode=LoweringMode.MAIN)

    assert result.source == (
        "Результат // lhs\n= // rhs\ne1cRuntimeКонтекст.Таблица.Количество();"
    )
    assert isinstance(result.source_map, SourceMap)
    assert result.source_map is result.mapped_source.source_map
    assert result.edits
    assert all(edit.start <= edit.end for edit in result.edits)
    parser_target.parse(result.source, "БлокНоутбука")


def test_platform_globals_and_nested_capture_mutation_are_not_rebound(
    parser_target: PythonParserTarget,
) -> None:
    from onec_runtime.bsl.semantic_lowering import (
        LoweringMode,
        SemanticNotebookLowerer,
    )

    source = (
        "Строка(Значение); Таблица.Добавить(Значение); "
        "e1cRuntimeКонтекстОтладки.Таблица[0] = Значение;"
    )
    result = SemanticNotebookLowerer(
        parser_target,
        context_names=("Таблица", "Значение"),
        platform_globals=("Строка",),
    ).lower(source, mode=LoweringMode.CAPTURE)

    assert result.source == (
        "Строка(e1cRuntimeКонтекст.Значение); "
        "e1cRuntimeКонтекст.Таблица.Добавить(e1cRuntimeКонтекст.Значение); "
        "e1cRuntimeКонтекстОтладки.Таблица[0] = e1cRuntimeКонтекст.Значение;"
    )
    assert result.dirty_roots == ()


def test_long_notebook_block_is_bound_without_python_recursion(
    parser_target: PythonParserTarget,
) -> None:
    from onec_runtime.bsl.semantic_lowering import (
        LoweringMode,
        SemanticNotebookLowerer,
    )

    source = "\n".join(f"Значение{index} = {index};" for index in range(2_000))

    result = SemanticNotebookLowerer(parser_target).lower(
        source,
        mode=LoweringMode.MAIN,
    )

    assert len(result.context_names) == 2_000
    assert result.source.count("e1cRuntimeКонтекст.Вставить(") == 2_000


def test_context_name_catalog_persists_across_cells(
    parser_target: PythonParserTarget,
) -> None:
    from onec_runtime.bsl.semantic_lowering import (
        LoweringMode,
        SemanticNotebookLowerer,
    )

    lowerer = SemanticNotebookLowerer(parser_target)
    lowerer.lower("Первый = 1;", mode=LoweringMode.MAIN)

    second = lowerer.lower("Второй = Первый;", mode=LoweringMode.MAIN)

    assert second.context_names == ("Первый", "Второй")
    assert second.source == (
        'e1cRuntimeКонтекст.Вставить("Второй", e1cRuntimeКонтекст.Первый);'
    )


def test_unknown_bare_read_remains_for_platform_resolution(
    parser_target: PythonParserTarget,
) -> None:
    from onec_runtime.bsl.semantic_lowering import LoweringMode, SemanticNotebookLowerer

    lowerer = SemanticNotebookLowerer(parser_target)
    first = lowerer.lower(
        "Результат = НеизвестноеПлатформенноеИмя.Свойство;",
        mode=LoweringMode.MAIN,
    )
    assert first.source == "Результат = НеизвестноеПлатформенноеИмя.Свойство;"
    assert first.context_names == ()

    lowerer.lower("Оклад = 100;", mode=LoweringMode.MAIN)
    second = lowerer.lower(
        "Результат = Оклад + НеизвестноеПлатформенноеИмя.Свойство;",
        mode=LoweringMode.MAIN,
    )
    assert second.source == (
        "Результат = e1cRuntimeКонтекст.Оклад + НеизвестноеПлатформенноеИмя.Свойство;"
    )
    assert second.context_names == ("Оклад",)


def test_main_persistent_assignments_publish_declared_write_candidates(
    parser_target: PythonParserTarget,
) -> None:
    from onec_runtime.bsl.semantic_lowering import (
        LoweringMode,
        SemanticNotebookLowerer,
    )

    result = SemanticNotebookLowerer(parser_target).lower(
        "Порог = 2; Порог = Порог + 1; НовыйИтог = Порог;",
        mode=LoweringMode.MAIN,
    )

    assert result.dirty_roots == ()
    assert result.persistent_write_roots == ("Порог", "НовыйИтог")


def test_module_binding_registers_scopes_without_lowering_worker_source(
    parser_target: PythonParserTarget,
) -> None:
    from onec_runtime.bsl.semantic_lowering import (
        LoweringMode,
        SemanticNotebookLowerer,
    )

    lowerer = SemanticNotebookLowerer(
        parser_target,
        context_names=("Значение",),
        platform_globals=("Сообщить",),
    )
    module_source = (
        "Перем МодульноеИмя;\n"
        "Процедура Сообщить(Знач Параметр) Экспорт\n"
        "Перем ЛокальноеИмя;\n"
        "ЛокальноеИмя = Параметр;\n"
        "МодульноеИмя = ЛокальноеИмя;\n"
        "КонецПроцедуры"
    )

    binding = lowerer.bind_module(module_source)
    cell = lowerer.lower(
        "МодульноеИмя = Значение; Сообщить(Значение);",
        mode=LoweringMode.MAIN,
    )

    assert binding.module_names == ("МодульноеИмя",)
    assert binding.method_names == ("Сообщить",)
    assert binding.method_scopes[0].parameters == ("Параметр",)
    assert binding.method_scopes[0].local_names == ("ЛокальноеИмя",)
    assert binding.source == module_source
    assert cell.source == (
        "МодульноеИмя = e1cRuntimeКонтекст.Значение; Сообщить(e1cRuntimeКонтекст.Значение);"
    )
    assert cell.messages_intercepted == 0


def test_worker_export_shadows_platform_message_interception(
    parser_target: PythonParserTarget,
) -> None:
    from onec_runtime.bsl.semantic_lowering import (
        LoweringMode,
        SemanticNotebookLowerer,
        WorkerExport,
    )

    result = SemanticNotebookLowerer(
        parser_target,
        context_names=("Значение",),
        platform_globals=("Сообщить",),
        worker_exports=(WorkerExport("Сообщить", "Показать"),),
    ).lower("Сообщить(Значение);", mode=LoweringMode.MAIN)

    assert result.source == "e1cRuntimeКонтекст.RuntimeWorker.Показать(e1cRuntimeКонтекст.Значение);"
    assert result.worker_dependencies == ("Сообщить",)
    assert result.messages_intercepted == 0


def test_module_binding_supports_method_without_parameters_or_locals(
    parser_target: PythonParserTarget,
) -> None:
    from onec_runtime.bsl.semantic_lowering import SemanticNotebookLowerer

    binding = SemanticNotebookLowerer(parser_target).bind_module(
        "Функция БезАргументов()\nВозврат Истина;\nКонецФункции"
    )

    assert binding.method_scopes[0].name == "БезАргументов"
    assert binding.method_scopes[0].parameters == ()
    assert binding.method_scopes[0].local_names == ()


def test_method_binding_marks_persistent_root_span_and_implicit_local_shadow(
    parser_target: PythonParserTarget,
) -> None:
    from onec_runtime.bsl.semantic_lowering import SemanticNotebookLowerer

    source = (
        "Функция ПолучитьА()\nВозврат А;\nКонецФункции\n"
        "Функция ЛокальнаяА()\nА = 1; Возврат А;\nКонецФункции"
    )
    binding = SemanticNotebookLowerer(
        parser_target, context_names=("А",)
    ).bind_module(source)

    persistent = binding.method_scopes[0].references[0]
    assert persistent.kind == "persistent"
    assert source[persistent.span.start:persistent.span.end] == "А"
    assert [reference.kind for reference in binding.method_scopes[1].references] == [
        "local", "local",
    ]


def test_module_binding_resolves_parameter_and_local_shadows_before_lower_scopes(
    parser_target: PythonParserTarget,
) -> None:
    from onec_runtime.bsl import NameBinding as PublicNameBinding
    from onec_runtime.bsl.semantic_lowering import (
        NameBinding,
        SemanticNotebookLowerer,
        WorkerExport,
    )

    source = (
        "Процедура Параметры(Сообщить, Рабочий, Сохраненное)\n"
        "Сообщить(); Рабочий(); Сохраненное;\n"
        "КонецПроцедуры\n"
        "Процедура Локальные()\n"
        "Перем Сообщить, Рабочий, Сохраненное;\n"
        "Сообщить(); Рабочий(); Сохраненное;\n"
        "КонецПроцедуры"
    )
    binding = SemanticNotebookLowerer(
        parser_target,
        context_names=("Сохраненное",),
        platform_globals=("Сообщить",),
        worker_exports=(WorkerExport("Рабочий", "Вызвать"),),
    ).bind_module(source)

    assert binding.source == source
    assert PublicNameBinding is NameBinding
    assert isinstance(binding.method_scopes[0].references[0], PublicNameBinding)
    assert [
        (item.name, item.kind) for item in binding.method_scopes[0].references
    ] == [
        ("Сообщить", "parameter"),
        ("Рабочий", "parameter"),
        ("Сохраненное", "parameter"),
    ]
    assert [
        (item.name, item.kind) for item in binding.method_scopes[1].references
    ] == [
        ("Сообщить", "local"),
        ("Рабочий", "local"),
        ("Сохраненное", "local"),
    ]


@pytest.mark.parametrize(
    "source",
    (
        "e1cRuntimeКонтекстОтладки[0];",
        "e1cRuntimeКонтекстОтладки[0] = Значение;",
        "e1cRuntimeКонтекстОтладки.Получить();",
        "e1cRuntimeКонтекстОтладки()[0];",
    ),
)
def test_capture_namespace_rejects_non_member_root_forms(
    parser_target: PythonParserTarget,
    source: str,
) -> None:
    from onec_runtime.bsl.semantic_lowering import (
        LoweringMode,
        SemanticLoweringError,
        SemanticNotebookLowerer,
    )

    with pytest.raises(SemanticLoweringError, match="capture root"):
        SemanticNotebookLowerer(parser_target).lower(
            source,
            mode=LoweringMode.CAPTURE,
        )


def test_bare_capture_assignment_is_rejected(
    parser_target: PythonParserTarget,
) -> None:
    from onec_runtime.bsl.semantic_lowering import (
        LoweringMode,
        SemanticLoweringError,
        SemanticNotebookLowerer,
    )

    with pytest.raises(SemanticLoweringError, match="reserved"):
        SemanticNotebookLowerer(parser_target).lower(
            "e1cRuntimeКонтекстОтладки = Значение;",
            mode=LoweringMode.CAPTURE,
        )


def test_platform_calls_stay_unqualified_but_known_context_call_chains_lower(
    parser_target: PythonParserTarget,
) -> None:
    from onec_runtime.bsl.semantic_lowering import (
        LoweringMode,
        SemanticNotebookLowerer,
    )

    source = (
        "Дата = ТекущаяДата(); Строковое = Строка(Значение); "
        "Элемент = Справочники.Номенклатура.СоздатьЭлемент(); "
        "Результат = Расчет.Ндфл.Посчитать();"
    )
    result = SemanticNotebookLowerer(
        parser_target,
        context_names=("Расчет", "Значение"),
    ).lower(source, mode=LoweringMode.MAIN)

    assert result.source == (
        'e1cRuntimeКонтекст.Вставить("Дата", ТекущаяДата()); '
        'e1cRuntimeКонтекст.Вставить("Строковое", Строка(e1cRuntimeКонтекст.Значение)); '
        'e1cRuntimeКонтекст.Вставить("Элемент", Справочники.Номенклатура.СоздатьЭлемент()); '
        "Результат = e1cRuntimeКонтекст.Расчет.Ндфл.Посчитать();"
    )
    assert result.context_names == (
        "Расчет",
        "Значение",
        "Дата",
        "Строковое",
        "Элемент",
    )


def test_supported_non_call_platform_roots_stay_unqualified(
    parser_target: PythonParserTarget,
) -> None:
    from onec_runtime.bsl.semantic_lowering import LoweringMode, SemanticNotebookLowerer

    result = SemanticNotebookLowerer(parser_target).lower(
        "Результат = СтатусСообщения.Важное + Символы.ПС;",
        mode=LoweringMode.MAIN,
    )

    assert result.source == "Результат = СтатусСообщения.Важное + Символы.ПС;"


def test_worker_export_overrides_a_platform_manager_root(
    parser_target: PythonParserTarget,
) -> None:
    from onec_runtime.bsl.semantic_lowering import (
        LoweringMode,
        SemanticNotebookLowerer,
        WorkerExport,
    )

    result = SemanticNotebookLowerer(
        parser_target,
        worker_exports=(
            WorkerExport("Справочники.Номенклатура.СоздатьЭлемент", "Создать"),
        ),
    ).lower(
        "Элемент = Справочники.Номенклатура.СоздатьЭлемент();",
        mode=LoweringMode.MAIN,
    )

    assert result.source == (
        'e1cRuntimeКонтекст.Вставить("Элемент", e1cRuntimeКонтекст.RuntimeWorker.Создать());'
    )
    assert result.worker_dependencies == ("Справочники.Номенклатура.СоздатьЭлемент",)


def test_loop_variables_are_cell_local_while_loop_inputs_remain_persistent(
    parser_target: PythonParserTarget,
) -> None:
    from onec_runtime.bsl.semantic_lowering import LoweringMode, SemanticNotebookLowerer

    source = (
        "Для Каждого Строка Из Таблица Цикл Строка.Добавить(); КонецЦикла; "
        "Для Счетчик = Начало По Конец Цикл Счетчик = Счетчик + 1; КонецЦикла; "
        "После = Строка + Счетчик;"
    )
    result = SemanticNotebookLowerer(
        parser_target,
        context_names=("Таблица", "Начало", "Конец"),
    ).lower(source, mode=LoweringMode.MAIN)

    assert result.source == (
        "Для Каждого Строка Из e1cRuntimeКонтекст.Таблица Цикл Строка.Добавить(); КонецЦикла; "
        "Для Счетчик = e1cRuntimeКонтекст.Начало По e1cRuntimeКонтекст.Конец Цикл Счетчик = Счетчик + 1; КонецЦикла; "
        'e1cRuntimeКонтекст.Вставить("После", Строка + Счетчик);'
    )


@pytest.mark.parametrize(
    ("source", "position"),
    (
        ("e1cRuntimeКонтекст = Значение;", 0),
        ("e1cRuntimeКонтекстОтладки = Значение;", 0),
        ("e1cRuntimeКонтекст.e1cRuntimeКонтекстОтладки.Получить();", 18),
    ),
)
def test_runtime_namespaces_cannot_be_rebound_or_used_as_capture_aliases(
    parser_target: PythonParserTarget,
    source: str,
    position: int,
) -> None:
    from onec_runtime.bsl.semantic_lowering import (
        LoweringMode,
        SemanticLoweringError,
        SemanticNotebookLowerer,
    )

    with pytest.raises(SemanticLoweringError, match=rf"at {position}"):
        SemanticNotebookLowerer(parser_target).lower(source, mode=LoweringMode.CAPTURE)


@pytest.mark.parametrize(
    "source",
    (
        "Значение;",
        "ВызватьИсключение(Первый, Второй) + Третий;",
    ),
)
def test_semantic_gates_reject_declared_grammar_supersets(
    parser_target: PythonParserTarget,
    source: str,
) -> None:
    from onec_runtime.bsl.semantic_lowering import (
        LoweringMode,
        SemanticLoweringError,
        SemanticNotebookLowerer,
    )

    with pytest.raises(SemanticLoweringError):
        SemanticNotebookLowerer(parser_target).lower(source, mode=LoweringMode.MAIN)


def test_source_edits_cannot_extend_past_the_original_source() -> None:
    from onec_runtime.bsl.semantic_lowering import (
        SemanticLoweringError,
        SemanticNotebookLowerer,
        SourceEdit,
    )

    with pytest.raises(SemanticLoweringError, match="invalid lowering source edit"):
        SemanticNotebookLowerer._validated_edits(
            (SourceEdit(0, 2, "", "test"),),
            source_length=1,
        )


def test_message_sink_constructor_option_is_not_a_second_interception_contract(
    parser_target: PythonParserTarget,
) -> None:
    from onec_runtime.bsl.semantic_lowering import SemanticNotebookLowerer

    with pytest.raises(TypeError, match="message_sink"):
        SemanticNotebookLowerer(parser_target, message_sink="e1cRuntimeКонтекст.СтарыйSink")


def test_bare_statement_call_after_index_and_member_chain_is_valid(
    parser_target: PythonParserTarget,
) -> None:
    from onec_runtime.bsl.semantic_lowering import LoweringMode, SemanticNotebookLowerer

    result = SemanticNotebookLowerer(parser_target).lower(
        "Объект[0].Метод();",
        mode=LoweringMode.MAIN,
    )

    assert result.source == "Объект[0].Метод();"


@pytest.mark.parametrize(
    "source",
    (
        "ВызватьИсключение(Первый, Второй).Представление();",
        "ВызватьИсключение(Первый, Второй)[0];",
        "ВызватьИсключение(Первый, Второй) + Третий;",
    ),
)
def test_multi_argument_raise_rejects_postfix_and_tail_continuations(
    parser_target: PythonParserTarget,
    source: str,
) -> None:
    from onec_runtime.bsl.semantic_lowering import (
        LoweringMode,
        SemanticLoweringError,
        SemanticNotebookLowerer,
    )

    with pytest.raises(SemanticLoweringError, match="multi-argument"):
        SemanticNotebookLowerer(parser_target).lower(source, mode=LoweringMode.MAIN)


def test_empty_parenthesized_raise_is_not_a_multi_argument_continuation(
    parser_target: PythonParserTarget,
) -> None:
    from onec_runtime.bsl.semantic_lowering import LoweringMode, SemanticNotebookLowerer

    result = SemanticNotebookLowerer(parser_target).lower(
        "ВызватьИсключение();",
        mode=LoweringMode.MAIN,
    )

    assert result.source == "ВызватьИсключение();"


def test_worker_export_overrides_a_known_persistent_root(
    parser_target: PythonParserTarget,
) -> None:
    from onec_runtime.bsl.semantic_lowering import (
        LoweringMode,
        SemanticNotebookLowerer,
        WorkerExport,
    )

    result = SemanticNotebookLowerer(
        parser_target,
        context_names=("Расчет",),
        worker_exports=(WorkerExport("Расчет.Ндфл.Посчитать", "Посчитать"),),
    ).lower("Результат = Расчет.Ндфл.Посчитать();", mode=LoweringMode.MAIN)

    assert result.source == "Результат = e1cRuntimeКонтекст.RuntimeWorker.Посчитать();"


def test_runtime_result_channels_stay_local_while_worker_calls_are_lowered(
    parser_target: PythonParserTarget,
) -> None:
    from onec_runtime.bsl.semantic_lowering import (
        LoweringMode,
        SemanticNotebookLowerer,
        WorkerExport,
    )

    lowerer = SemanticNotebookLowerer(
        parser_target,
        worker_exports=(WorkerExport("Расчет.Ндфл.Посчитать", "Посчитать"),),
    )

    main = lowerer.lower(
        "Результат = Расчет.Ндфл.Посчитать();", mode=LoweringMode.MAIN
    )
    capture = lowerer.lower(
        "РезультатИнструкции = Расчет.Ндфл.Посчитать();",
        mode=LoweringMode.CAPTURE,
    )

    assert main.source == "Результат = e1cRuntimeКонтекст.RuntimeWorker.Посчитать();"
    assert capture.source == "РезультатИнструкции = e1cRuntimeКонтекст.RuntimeWorker.Посчитать();"


def test_direct_assignment_roots_are_persistent_before_their_first_use(
    parser_target: PythonParserTarget,
) -> None:
    from onec_runtime.bsl.semantic_lowering import LoweringMode, SemanticNotebookLowerer

    result = SemanticNotebookLowerer(parser_target).lower(
        "До(); До = 1; До();",
        mode=LoweringMode.MAIN,
    )

    assert result.source == 'e1cRuntimeКонтекст.До(); e1cRuntimeКонтекст.Вставить("До", 1); e1cRuntimeКонтекст.До();'


@pytest.mark.parametrize(
    "root",
    (
        "ПланыОбмена",
        "БизнесПроцессы",
        "Задачи",
        "ЖурналыДокументов",
        "КритерииОтбора",
        "Последовательности",
        "ВнешниеОбработки",
        "ВнешниеОтчеты",
    ),
)
def test_standard_manager_roots_are_non_call_platform_globals(
    parser_target: PythonParserTarget,
    root: str,
) -> None:
    from onec_runtime.bsl.semantic_lowering import LoweringMode, SemanticNotebookLowerer

    result = SemanticNotebookLowerer(parser_target).lower(
        f"Результат = {root}.СлужебноеИмя;",
        mode=LoweringMode.MAIN,
    )

    assert f"e1cRuntimeКонтекст.{root}" not in result.source


def test_document_write_mode_is_a_platform_global(
    parser_target: PythonParserTarget,
) -> None:
    from onec_runtime.bsl import LoweringMode, SemanticNotebookLowerer

    result = SemanticNotebookLowerer(
        parser_target,
        context_names=("Прием",),
    ).lower(
        "Прием.Записать(РежимЗаписиДокумента.Проведение);",
        mode=LoweringMode.MAIN,
    )

    assert result.source == (
        "e1cRuntimeКонтекст.Прием.Записать(РежимЗаписиДокумента.Проведение);"
    )


def test_persistent_assignment_uses_derived_name_and_fragment_mappings(
    parser_target: PythonParserTarget,
) -> None:
    """Break caught: a whole assignment replacement must not share one relation."""
    from onec_runtime.bsl import LoweringMode, SemanticNotebookLowerer

    source = "Ответ =\nДелитель / Делитель;"
    visible = _mapped(source, "cell-main", 3)

    result = SemanticNotebookLowerer(
        parser_target, context_names=("Делитель",),
    ).lower_mapped(
        visible,
        mode=LoweringMode.MAIN,
    )

    assert result.mapped_source.artifact.kind is SourceArtifactKind.SEMANTIC_LOWERING
    assert result.source == (
        'e1cRuntimeКонтекст.Вставить("Ответ",\n'
        "e1cRuntimeКонтекст.Делитель / e1cRuntimeКонтекст.Делитель);"
    )
    prefix = result.source.index("e1cRuntimeКонтекст.Вставить")
    assert result.source_map.map_offset(prefix).relation is MappingRelation.SYNTHETIC
    key = result.source.index("Ответ")
    mapped_key = result.source_map.map_offset(key)
    assert mapped_key.relation is MappingRelation.DERIVED
    assert mapped_key.origin_span == SourceSpan(0, len("Ответ"))
    comma = result.source.index('",') + 1
    assert result.source_map.map_offset(comma).relation is MappingRelation.SYNTHETIC
    slash = result.source.index("/")
    mapped_slash = result.source_map.map_offset(slash)
    assert mapped_slash.relation is MappingRelation.EXACT
    assert mapped_slash.origin_span == SourceSpan(source.index("/"), source.index("/") + 1)
    close = result.source.rindex(")")
    assert result.source_map.map_offset(close).relation is MappingRelation.SYNTHETIC
    for generated, origin in zip(
        (
            result.source.index("Делитель"),
            result.source.rindex("Делитель"),
        ),
        (
            source.index("Делитель"),
            source.rindex("Делитель"),
        ),
        strict=True,
    ):
        mapped = result.source_map.map_offset(generated)
        assert mapped.relation is MappingRelation.EXACT
        assert mapped.origin_span == SourceSpan(origin, origin + 1)


def test_persistent_references_keep_repeated_identifiers_and_main_result_exact(
    parser_target: PythonParserTarget,
) -> None:
    """Break caught: inserted prefixes must not steal copied identifier coordinates."""
    from onec_runtime.bsl import LoweringMode, SemanticNotebookLowerer

    source = "Результат = Сохраненное + Сохраненное;"
    visible = _mapped(source, "cell-result", 4)
    result = SemanticNotebookLowerer(
        parser_target,
        context_names=("Сохраненное",),
    ).lower_mapped(visible, mode=LoweringMode.MAIN)

    result_name = result.source.index("Результат")
    mapped_result = result.source_map.map_offset(result_name)
    assert mapped_result.relation is MappingRelation.EXACT
    assert mapped_result.origin_span == SourceSpan(0, 1)
    plus = result.source.index("+")
    assert result.source_map.map_offset(plus).origin_span == SourceSpan(
        source.index("+"), source.index("+") + 1
    )
    generated_names = (
        result.source.index("Сохраненное"),
        result.source.rindex("Сохраненное"),
    )
    visible_names = (source.index("Сохраненное"), source.rindex("Сохраненное"))
    for generated, origin in zip(generated_names, visible_names, strict=True):
        mapped = result.source_map.map_offset(generated)
        assert mapped.relation is MappingRelation.EXACT
        assert mapped.origin_span == SourceSpan(origin, origin + 1)
        prefix = generated - len("e1cRuntimeКонтекст.")
        assert result.source_map.map_offset(prefix).relation is MappingRelation.SYNTHETIC


def test_worker_receiver_is_synthetic_while_call_and_arguments_keep_coordinates(
    parser_target: PythonParserTarget,
) -> None:
    """Break caught: Worker receiver lowering must not absorb the copied call."""
    from onec_runtime.bsl import LoweringMode, SemanticNotebookLowerer, WorkerExport

    source = "Результат = Удвоить(Исходное + Исходное);"
    visible = _mapped(source, "cell-worker", 5)
    result = SemanticNotebookLowerer(
        parser_target,
        worker_exports=(WorkerExport("Удвоить", "Выполнить"),),
    ).lower_mapped(visible, mode=LoweringMode.MAIN)

    receiver = result.source.index("e1cRuntimeКонтекст.RuntimeWorker")
    assert result.source_map.map_offset(receiver).relation is MappingRelation.SYNTHETIC
    method = result.source.index("Выполнить")
    mapped_method = result.source_map.map_offset(method)
    assert mapped_method.relation is MappingRelation.DERIVED
    visible_method = source.index("Удвоить")
    assert mapped_method.origin_span == SourceSpan(
        visible_method, visible_method + len("Удвоить")
    )
    for character in ("(", "+", ")"):
        generated = result.source.index(character)
        origin = source.index(character)
        assert result.source_map.map_offset(generated).origin_span == SourceSpan(
            origin, origin + 1
        )
    for generated, origin in zip(
        (result.source.index("Исходное"), result.source.rindex("Исходное")),
        (source.index("Исходное"), source.rindex("Исходное")),
        strict=True,
    ):
        mapped = result.source_map.map_offset(generated)
        assert mapped.relation is MappingRelation.EXACT
        assert mapped.origin_span == SourceSpan(origin, origin + 1)


def test_same_name_worker_export_callee_is_an_exact_visible_copy(
    parser_target: PythonParserTarget,
) -> None:
    """Break caught: an unchanged Worker callee is mislabeled as derived."""
    from onec_runtime.bsl import LoweringMode, SemanticNotebookLowerer, WorkerExport

    source = "Результат = Удвоить(1);"
    visible = _mapped(source, "cell-worker-same-name", 6)
    result = SemanticNotebookLowerer(
        parser_target,
        worker_exports=(WorkerExport("Удвоить", "Удвоить"),),
    ).lower_mapped(visible, mode=LoweringMode.MAIN)

    assert result.source == "Результат = e1cRuntimeКонтекст.RuntimeWorker.Удвоить(1);"
    receiver = result.source.index("e1cRuntimeКонтекст.RuntimeWorker")
    callee = result.source.index("Удвоить")
    assert result.source_map.map_offset(receiver).relation is MappingRelation.SYNTHETIC
    for generated_offset, visible_offset in (
        (callee, 12),
        (callee + 3, 15),
        (callee + 6, 18),
    ):
        mapped = result.source_map.map_offset(generated_offset)
        assert mapped.relation is MappingRelation.EXACT
        assert mapped.origin_span == SourceSpan(visible_offset, visible_offset + 1)


def test_message_and_nested_worker_preserve_argument_mapping(
    parser_target: PythonParserTarget,
) -> None:
    """Break caught: nested argument mapping must survive both enclosing rewrites."""
    from onec_runtime.bsl import LoweringMode, SemanticNotebookLowerer, WorkerExport

    source = "Сообщить(Удвоить(Исходное));"
    visible = _mapped(source, "cell-message", 6)
    result = SemanticNotebookLowerer(
        parser_target,
        worker_exports=(WorkerExport("Удвоить", "Выполнить"),),
    ).lower_mapped(visible, mode=LoweringMode.MAIN)

    message_open = result.source.index("e1cRuntimeКонтекст.__onec_cell_messages")
    assert result.source_map.map_offset(message_open).relation is MappingRelation.SYNTHETIC
    generated = result.source.rindex("Исходное")
    mapped = result.source_map.map_offset(generated)
    visible_argument = 17
    assert source[visible_argument:].startswith("Исходное")
    assert mapped.unit == visible.source_map.map_offset(visible_argument).unit
    assert mapped.origin_span is not None and mapped.origin_span.start == visible_argument
    assert mapped.relation is MappingRelation.EXACT
    message_close = result.source.rindex(")")
    assert result.source_map.map_offset(message_close).relation is MappingRelation.SYNTHETIC


def test_empty_message_is_synthetic_but_statement_separator_is_exact(
    parser_target: PythonParserTarget,
) -> None:
    """Break caught: empty Сообщить must not claim visible argument coordinates."""
    from onec_runtime.bsl import LoweringMode, SemanticNotebookLowerer

    source = "Сообщить();"
    visible = _mapped(source, "cell-empty-message", 7)
    result = SemanticNotebookLowerer(parser_target).lower_mapped(
        visible,
        mode=LoweringMode.MAIN,
    )

    assert result.source == (
        "e1cRuntimeКонтекст.__onec_cell_messages.Добавить(Строка(Неопределено));"
    )
    for generated in (
        result.source.index("e1cRuntimeКонтекст"),
        result.source.index("Неопределено"),
        result.source.rindex(")"),
    ):
        assert result.source_map.map_offset(generated).relation is MappingRelation.SYNTHETIC
    semicolon = result.source.index(";")
    mapped_semicolon = result.source_map.map_offset(semicolon)
    assert mapped_semicolon.relation is MappingRelation.EXACT
    assert mapped_semicolon.origin_span == SourceSpan(source.index(";"), len(source))


def test_capture_namespace_and_result_channel_remain_exact_unicode_multiline(
    parser_target: PythonParserTarget,
) -> None:
    """Break caught: CAPTURE-local namespaces must never be rebound as persistent."""
    from onec_runtime.bsl import LoweringMode, SemanticNotebookLowerer

    source = (
        "e1cRuntimeКонтекстОтладки.Счётчик = e1cRuntimeКонтекстОтладки.Счётчик + 1;\n"
        "РезультатИнструкции = e1cRuntimeКонтекстОтладки.Счётчик;"
    )
    visible = _mapped(source, "cell-capture-unicode", 8)
    result = SemanticNotebookLowerer(parser_target).lower_mapped(
        visible,
        mode=LoweringMode.CAPTURE,
    )

    assert result.source == source
    assert result.dirty_roots == ("Счётчик",)
    assert result.persistent_write_roots == ()
    for needle in ("e1cRuntimeКонтекстОтладки", "Счётчик", "РезультатИнструкции", "\n"):
        generated = result.source.index(needle)
        origin = source.index(needle)
        mapped = result.source_map.map_offset(generated)
        assert mapped.relation is MappingRelation.EXACT
        assert mapped.origin_span == SourceSpan(origin, origin + 1)


def test_plain_string_lowering_wraps_anonymous_mapped_source_without_repr_text(
    parser_target: PythonParserTarget,
) -> None:
    """Break caught: the compatibility path must remain mapped and repr-safe."""
    from onec_runtime.bsl import LoweringMode, SemanticNotebookLowerer

    source = "СовершенноСекретное = 1;"
    result = SemanticNotebookLowerer(parser_target).lower(
        source,
        mode=LoweringMode.MAIN,
    )

    assert isinstance(result.mapped_source, MappedSource)
    assert result.source == 'e1cRuntimeКонтекст.Вставить("СовершенноСекретное", 1);'
    assert source not in repr(result)
    mapped_one = result.source_map.map_offset(result.source.rindex("1"))
    assert mapped_one.unit is not None
    assert mapped_one.unit.kind is SourceUnitKind.NOTEBOOK_CELL
    assert mapped_one.unit.source_sha256 == source_sha256(source)


def test_package_exports_mapped_lowering_interfaces() -> None:
    """Break caught: mapped lowering consumers need one stable package surface."""
    from onec_runtime.bsl import (
        MappedSource as PublicMappedSource,
        MappingRelation as PublicMappingRelation,
        SourceMap as PublicSourceMap,
        SourceTransformBuilder as PublicSourceTransformBuilder,
        mapped_visible_source as public_mapped_visible_source,
    )
    from onec_runtime.bsl.source_maps import SourceTransformBuilder

    assert PublicMappedSource is MappedSource
    assert PublicMappingRelation is MappingRelation
    assert PublicSourceMap is SourceMap
    assert PublicSourceTransformBuilder is SourceTransformBuilder
    assert public_mapped_visible_source is mapped_visible_source


def test_worker_export_preserves_legacy_flat_and_qualified_receiver_identity() -> None:
    from onec_runtime.bsl.semantic_lowering import WorkerExport

    flat = WorkerExport("Рассчитать", "Рассчитать")
    qualified = WorkerExport(
        "МодульРасчета.Рассчитать",
        "Рассчитать",
        receiver_module="МодульРасчета",
    )

    assert flat.receiver_module is None
    assert qualified.receiver_module == "МодульРасчета"


def test_worker_export_rejects_mismatched_qualified_receiver() -> None:
    from onec_runtime.bsl.semantic_lowering import WorkerExport

    with pytest.raises(ValueError, match="receiver"):
        WorkerExport(
            "ДругойМодуль.Рассчитать",
            "Рассчитать",
            receiver_module="МодульРасчета",
        )


def test_worker_export_catalog_identity_binds_receiver_and_preserves_legacy_none(
    parser_target: PythonParserTarget,
) -> None:
    from onec_runtime.bsl.semantic_lowering import (
        SemanticNotebookLowerer,
        WorkerExport,
    )

    legacy = SemanticNotebookLowerer(
        parser_target,
        worker_exports=(
            WorkerExport("МодульРасчета.Рассчитать", "Рассчитать"),
        ),
    )
    qualified = SemanticNotebookLowerer(
        parser_target,
        worker_exports=(
            WorkerExport(
                "МодульРасчета.Рассчитать",
                "Рассчитать",
                receiver_module="МодульРасчета",
            ),
        ),
    )

    assert legacy.worker_export_identity == (
        ("модульрасчета.рассчитать", "рассчитать", None),
    )
    assert qualified.worker_export_identity == (
        ("модульрасчета.рассчитать", "рассчитать", "модульрасчета"),
    )
    assert legacy.worker_export_identity != qualified.worker_export_identity


def test_per_call_worker_catalog_routes_through_exact_pinned_module_receiver(
    parser_target: PythonParserTarget,
) -> None:
    """A pinned operation must never route a call through the mutable active root."""
    from onec_runtime.bsl.semantic_lowering import (
        LoweringMode,
        SemanticNotebookLowerer,
        WorkerExport,
    )

    lowerer = SemanticNotebookLowerer(
        parser_target,
        worker_exports=(WorkerExport("Старый", "Старый"),),
    )
    pinned = (
        WorkerExport(
            "МодульРасчета.Рассчитать",
            "Рассчитать",
            receiver_module="МодульРасчета",
        ),
    )

    result = lowerer.lower(
        "Результат = МодульРасчета.Рассчитать();",
        mode=LoweringMode.MAIN,
        worker_exports=pinned,
    )

    assert result.source == (
        'Результат = __OnecPinnedWorkerGeneration.Modules.Получить('
        '"МодульРасчета").Рассчитать();'
    )
    assert "RuntimeWorker" not in result.source
    assert lowerer.worker_export_identity == (("старый", "старый", None),)
    parser_target.parse(result.source, "БлокНоутбука")


def test_capture_worker_catalog_keeps_the_kernel_wrapper_local_receiver(
    parser_target: PythonParserTarget,
) -> None:
    """The trusted kernel wrapper, not user code, binds the pinned local."""
    from onec_runtime.bsl.semantic_lowering import (
        LoweringMode,
        SemanticNotebookLowerer,
        WorkerExport,
    )

    result = SemanticNotebookLowerer(parser_target).lower(
        "РезультатИнструкции = МодульРасчета.Рассчитать();",
        mode=LoweringMode.CAPTURE,
        worker_exports=(
            WorkerExport(
                "МодульРасчета.Рассчитать",
                "Рассчитать",
                receiver_module="МодульРасчета",
            ),
        ),
    )

    assert result.source == (
        "РезультатИнструкции = "
        "__OnecPinnedWorkerGeneration.Modules.Получить("
        '"МодульРасчета").Рассчитать();'
    )
    parser_target.parse(result.source, "БлокНоутбука")


def test_dynamic_context_slot_overwrite_is_rejected_before_worker_lowering(
    parser_target: PythonParserTarget,
) -> None:
    """Opaque dynamic code cannot bypass the static reserved-slot protocol."""
    from onec_runtime.bsl.semantic_lowering import (
        LoweringMode,
        SemanticLoweringError,
        SemanticNotebookLowerer,
        WorkerExport,
    )

    source = (
        'Выполнить("e1cRuntimeКонтекст.Вставить(""RuntimeWorkerPinnedOperationGeneration"", '
        'Неопределено);"); '
        "РезультатИнструкции = МодульРасчета.Рассчитать();"
    )
    with pytest.raises(SemanticLoweringError) as raised:
        SemanticNotebookLowerer(parser_target).lower(
            source,
            mode=LoweringMode.CAPTURE,
            worker_exports=(
                WorkerExport(
                    "МодульРасчета.Рассчитать",
                    "Рассчитать",
                    receiver_module="МодульРасчета",
                ),
            ),
        )

    assert raised.value.code == "context_protocol_dynamic_execution"


@pytest.mark.parametrize(
    "source",
    (
        "__OnecPinnedWorkerGeneration = Неопределено;",
        "__OnecPinnedWorkerGenerationOriginal = Неопределено;",
        "__OnecPinnedWorkerGenerationSlotExists = Ложь;",
    ),
)
def test_kernel_worker_handoff_locals_are_reserved_from_user_source(
    parser_target: PythonParserTarget,
    source: str,
) -> None:
    from onec_runtime.bsl.semantic_lowering import (
        LoweringMode,
        SemanticLoweringError,
        SemanticNotebookLowerer,
    )

    with pytest.raises(SemanticLoweringError) as raised:
        SemanticNotebookLowerer(parser_target).lower(
            source,
            mode=LoweringMode.CAPTURE,
        )

    assert raised.value.code == "reserved_worker_protocol_local"


@pytest.mark.parametrize(
    "source",
    (
        "e1cRuntimeКонтекст.RuntimeWorkerActiveGeneration = Неопределено;",
        "e1cRuntimeКонтекст.RuntimeWorkerPinnedOperationGeneration = Неопределено;",
        'e1cRuntimeКонтекст["RuntimeWorkerActiveGeneration"] = Неопределено;',
        'e1cRuntimeКонтекст.Вставить("RuntimeWorkerActiveGeneration", Неопределено);',
        'e1cRuntimeКонтекст.Insert("RuntimeWorkerActiveGeneration", Неопределено);',
        "e1cRuntimeКонтекст.Вставить(ИмяСвойства, Неопределено);",
        'e1cRuntimeКонтекст.Удалить("RuntimeWorkerActiveGeneration");',
        'e1cRuntimeКонтекст.Delete("RuntimeWorkerActiveGeneration");',
        "e1cRuntimeКонтекст.Удалить(ИмяСвойства);",
        "e1cRuntimeКонтекст.Delete(PropertyName);",
    ),
)
def test_worker_generation_context_protocol_member_is_reserved(
    parser_target: PythonParserTarget,
    source: str,
) -> None:
    """A notebook cell cannot replace the root later pinned by trusted code."""
    from onec_runtime.bsl.semantic_lowering import (
        LoweringMode,
        SemanticLoweringError,
        SemanticNotebookLowerer,
    )

    with pytest.raises(SemanticLoweringError) as raised:
        SemanticNotebookLowerer(parser_target).lower(
            source,
            mode=LoweringMode.MAIN,
        )

    assert raised.value.code == "reserved_worker_protocol_member"


@pytest.mark.parametrize(
    "source",
    (
        "Алиас = e1cRuntimeКонтекст;",
        'e1cRuntimeКонтекст.Вставить("Алиас", e1cRuntimeКонтекст);',
        "e1cRuntimeКонтекст.Алиас = e1cRuntimeКонтекст;",
        "Массив[0] = e1cRuntimeКонтекст;",
        "Обработать(e1cRuntimeКонтекст);",
        "Массив.Добавить(e1cRuntimeКонтекст);",
        "Возврат e1cRuntimeКонтекст;",
        "Return e1cRuntimeКонтекст;",
    ),
)
def test_bare_context_cannot_escape_to_persistent_or_opaque_state(
    parser_target: PythonParserTarget,
    source: str,
) -> None:
    from onec_runtime.bsl.semantic_lowering import (
        LoweringMode,
        SemanticLoweringError,
        SemanticNotebookLowerer,
    )

    with pytest.raises(SemanticLoweringError) as raised:
        SemanticNotebookLowerer(parser_target).lower(
            source,
            mode=LoweringMode.MAIN,
        )

    assert raised.value.code == "context_alias_escape"


@pytest.mark.parametrize(
    "source",
    (
        "Выполнить Строка;",
        "Execute Text;",
    ),
)
def test_dynamic_notebook_execution_is_fail_closed_for_context_protocol(
    parser_target: PythonParserTarget,
    source: str,
) -> None:
    from onec_runtime.bsl.semantic_lowering import (
        LoweringMode,
        SemanticLoweringError,
        SemanticNotebookLowerer,
    )

    with pytest.raises(SemanticLoweringError) as raised:
        SemanticNotebookLowerer(parser_target).lower(
            source,
            mode=LoweringMode.MAIN,
        )

    assert raised.value.code == "context_protocol_dynamic_execution"


@pytest.mark.parametrize(
    "mutation",
    (
        'Второй.Удалить("RuntimeWorkerActiveGeneration");',
        'Второй.Delete("RuntimeWorkerActiveGeneration");',
        "Второй.Вставить(ИмяСвойства, Неопределено);",
    ),
)
def test_chained_module_context_aliases_cannot_mutate_active_root(
    parser_target: PythonParserTarget,
    mutation: str,
) -> None:
    from onec_runtime.bsl.semantic_lowering import (
        LoweringMode,
        SemanticLoweringError,
        SemanticNotebookLowerer,
    )

    lowerer = SemanticNotebookLowerer(parser_target)
    lowerer.bind_module("Перем Алиас, Второй;")

    with pytest.raises(SemanticLoweringError) as raised:
        lowerer.lower(
            f"Алиас = e1cRuntimeКонтекст; Второй = Алиас; {mutation}",
            mode=LoweringMode.MAIN,
        )

    assert raised.value.code == "reserved_worker_protocol_member"


@pytest.mark.parametrize(
    "escape",
    (
        "Второй.Сохранить();",
        "Обработать(Второй);",
        "Массив.Добавить(Второй);",
        "Возврат Второй;",
    ),
)
def test_chained_context_alias_cannot_escape_through_unknown_sink(
    parser_target: PythonParserTarget,
    escape: str,
) -> None:
    from onec_runtime.bsl.semantic_lowering import (
        LoweringMode,
        SemanticLoweringError,
        SemanticNotebookLowerer,
    )

    lowerer = SemanticNotebookLowerer(parser_target)
    lowerer.bind_module("Перем Алиас, Второй;")

    with pytest.raises(SemanticLoweringError) as raised:
        lowerer.lower(
            f"Алиас = e1cRuntimeКонтекст; Второй = Алиас; {escape}",
            mode=LoweringMode.MAIN,
        )

    assert raised.value.code == "context_alias_escape"


def test_context_alias_taint_merges_across_conditional_control_flow(
    parser_target: PythonParserTarget,
) -> None:
    from onec_runtime.bsl.semantic_lowering import (
        LoweringMode,
        SemanticLoweringError,
        SemanticNotebookLowerer,
    )

    lowerer = SemanticNotebookLowerer(parser_target, context_names=("Флаг",))
    lowerer.bind_module("Перем Алиас;")
    source = (
        "Если Флаг Тогда Алиас = e1cRuntimeКонтекст; "
        "Иначе Алиас = Новый Структура; КонецЕсли; "
        'Алиас.Delete("RuntimeWorkerActiveGeneration");'
    )

    with pytest.raises(SemanticLoweringError) as raised:
        lowerer.lower(source, mode=LoweringMode.MAIN)

    assert raised.value.code == "reserved_worker_protocol_member"


def test_context_module_alias_taint_survives_cells_and_safe_reassignment_clears_it(
    parser_target: PythonParserTarget,
) -> None:
    from onec_runtime.bsl.semantic_lowering import (
        LoweringMode,
        SemanticLoweringError,
        SemanticNotebookLowerer,
    )

    lowerer = SemanticNotebookLowerer(parser_target)
    lowerer.bind_module("Перем Алиас;")
    lowerer.lower("Алиас = e1cRuntimeКонтекст;", mode=LoweringMode.MAIN)

    with pytest.raises(SemanticLoweringError) as raised:
        lowerer.lower(
            'Алиас.Delete("RuntimeWorkerActiveGeneration");',
            mode=LoweringMode.MAIN,
        )
    assert raised.value.code == "reserved_worker_protocol_member"

    result = lowerer.lower(
        'Алиас = Новый Структура; Алиас.Delete("RuntimeWorkerActiveGeneration");',
        mode=LoweringMode.MAIN,
    )
    assert result.source.endswith(
        'Алиас = Новый Структура; Алиас.Delete("RuntimeWorkerActiveGeneration");'
    )


def test_context_alias_cannot_install_g19_manifest_over_g17_modules_spoof(
    parser_target: PythonParserTarget,
) -> None:
    from onec_runtime.bsl.semantic_lowering import (
        LoweringMode,
        SemanticLoweringError,
        SemanticNotebookLowerer,
    )

    lowerer = SemanticNotebookLowerer(
        parser_target,
        context_names=("ManifestG19", "ModulesG17", "ExportsG19"),
    )
    lowerer.bind_module("Перем Алиас;")
    source = (
        "Алиас = e1cRuntimeКонтекст; "
        'Алиас.Insert("RuntimeWorkerActiveGeneration", '
        'Новый ФиксированнаяСтруктура("ManifestSha256,Modules,Exports", '
        "ManifestG19, ModulesG17, ExportsG19));"
    )

    with pytest.raises(SemanticLoweringError) as raised:
        lowerer.lower(source, mode=LoweringMode.MAIN)

    assert raised.value.code == "reserved_worker_protocol_member"


def test_loop_local_context_alias_is_checked_flow_sensitively(
    parser_target: PythonParserTarget,
) -> None:
    from onec_runtime.bsl.semantic_lowering import (
        LoweringMode,
        SemanticLoweringError,
        SemanticNotebookLowerer,
    )

    lowerer = SemanticNotebookLowerer(
        parser_target,
        context_names=("Коллекция",),
    )

    with pytest.raises(SemanticLoweringError) as raised:
        lowerer.lower(
            "Для Каждого Алиас Из Коллекция Цикл "
            "Алиас = e1cRuntimeКонтекст; "
            'Алиас.Delete("RuntimeWorkerActiveGeneration"); '
            "КонецЦикла;",
            mode=LoweringMode.MAIN,
        )

    assert raised.value.code == "reserved_worker_protocol_member"


@pytest.mark.parametrize(
    "source",
    (
        (
            "Алиас = e1cRuntimeКонтекст; Перейти ~После; "
            "Алиас = Новый Структура; ~После: "
            'Алиас.Delete("RuntimeWorkerActiveGeneration");'
        ),
        (
            "Попытка Алиас = e1cRuntimeКонтекст; "
            'ВызватьИсключение "stop"; '
            "Алиас = Новый Структура; Исключение "
            'Алиас.Delete("RuntimeWorkerActiveGeneration"); '
            "КонецПопытки;"
        ),
        (
            "Если Истина Тогда Алиас = e1cRuntimeКонтекст; Перейти ~После; "
            "КонецЕсли; Алиас = Новый Структура; ~После: "
            'Алиас.Delete("RuntimeWorkerActiveGeneration");'
        ),
        (
            "Пока Истина Цикл Если Истина Тогда Алиас = e1cRuntimeКонтекст; "
            "Прервать; КонецЕсли; Алиас = Новый Структура; "
            "КонецЦикла; "
            'Алиас.Delete("RuntimeWorkerActiveGeneration");'
        ),
    ),
)
def test_control_transfer_cannot_skip_context_alias_reassignment(
    parser_target: PythonParserTarget,
    source: str,
) -> None:
    from onec_runtime.bsl.semantic_lowering import (
        LoweringMode,
        SemanticLoweringError,
        SemanticNotebookLowerer,
    )

    lowerer = SemanticNotebookLowerer(parser_target)
    lowerer.bind_module("Перем Алиас;")

    with pytest.raises(SemanticLoweringError) as raised:
        lowerer.lower(source, mode=LoweringMode.MAIN)

    assert raised.value.code == "reserved_worker_protocol_member"


@pytest.mark.parametrize(
    "source",
    (
        (
            "Пока Флаг Цикл Если Номер = 1 Тогда Алиас = e1cRuntimeКонтекст; "
            "Иначе Алиас.Delete(\"RuntimeWorkerActiveGeneration\"); "
            "КонецЕсли; Номер = Номер + 1; КонецЦикла;"
        ),
        (
            "Для Каждого Элемент Из Коллекция Цикл "
            "Если Элемент = 1 Тогда Алиас = e1cRuntimeКонтекст; "
            "Иначе Алиас.Удалить(\"RuntimeWorkerActiveGeneration\"); "
            "КонецЕсли; КонецЦикла;"
        ),
        (
            "Для Номер = 1 По 2 Цикл "
            "Если Номер = 1 Тогда Алиас = e1cRuntimeКонтекст; "
            "Иначе Алиас.Delete(\"RuntimeWorkerActiveGeneration\"); "
            "КонецЕсли; КонецЦикла;"
        ),
        (
            "Для Внешний = 1 По 2 Цикл Пока Флаг Цикл "
            "Если Внешний = 1 Тогда Алиас = e1cRuntimeКонтекст; "
            "Иначе Алиас.Delete(\"RuntimeWorkerActiveGeneration\"); "
            "КонецЕсли; Флаг = Ложь; КонецЦикла; КонецЦикла;"
        ),
        (
            "Для Номер = 1 По 2 Цикл "
            "Если Номер = 2 Тогда "
            "Алиас.Delete(\"RuntimeWorkerActiveGeneration\"); Прервать; "
            "КонецЕсли; Алиас = e1cRuntimeКонтекст; Продолжить; КонецЦикла;"
        ),
    ),
)
def test_loop_fixed_point_rejects_context_alias_from_prior_iteration(
    parser_target: PythonParserTarget,
    source: str,
) -> None:
    from onec_runtime.bsl.semantic_lowering import (
        LoweringMode,
        SemanticLoweringError,
        SemanticNotebookLowerer,
    )

    lowerer = SemanticNotebookLowerer(
        parser_target,
        context_names=("Флаг", "Коллекция"),
    )
    lowerer.bind_module("Перем Алиас, Номер;")

    with pytest.raises(SemanticLoweringError) as raised:
        lowerer.lower(source, mode=LoweringMode.MAIN)

    assert raised.value.code == "reserved_worker_protocol_member"


def test_backward_goto_reaches_fixed_point_before_reserved_mutation(
    parser_target: PythonParserTarget,
) -> None:
    from onec_runtime.bsl.semantic_lowering import (
        LoweringMode,
        SemanticLoweringError,
        SemanticNotebookLowerer,
    )

    lowerer = SemanticNotebookLowerer(parser_target)
    lowerer.bind_module("Перем Алиас;")
    source = (
        "Алиас = Новый Структура; ~Повтор: "
        'Алиас.Delete("RuntimeWorkerActiveGeneration"); '
        "Алиас = e1cRuntimeКонтекст; Перейти ~Повтор;"
    )

    with pytest.raises(SemanticLoweringError) as raised:
        lowerer.lower(source, mode=LoweringMode.MAIN)

    assert raised.value.code == "reserved_worker_protocol_member"


@pytest.mark.parametrize(
    "source",
    (
        (
            "Пока Флаг Цикл Алиас = e1cRuntimeКонтекст; Алиас = Новый Структура; "
            'Алиас.Delete("RuntimeWorkerActiveGeneration"); '
            "Флаг = Ложь; КонецЦикла;"
        ),
        (
            "Для Номер = 1 По 2 Цикл Алиас = e1cRuntimeКонтекст; "
            "Алиас = Новый Структура; "
            'Алиас.Delete("RuntimeWorkerActiveGeneration"); КонецЦикла;'
        ),
        (
            "Для Каждого Элемент Из Коллекция Цикл "
            'Элемент.Delete("RuntimeWorkerActiveGeneration"); КонецЦикла;'
        ),
    ),
)
def test_loop_fixed_point_preserves_proven_safe_kills_and_iteration_reset(
    parser_target: PythonParserTarget,
    source: str,
) -> None:
    from onec_runtime.bsl.semantic_lowering import LoweringMode, SemanticNotebookLowerer

    lowerer = SemanticNotebookLowerer(
        parser_target,
        context_names=("Флаг", "Коллекция"),
    )
    lowerer.bind_module("Перем Алиас;")

    result = lowerer.lower(source, mode=LoweringMode.MAIN)

    assert "RuntimeWorkerActiveGeneration" in result.source


def test_ordinary_context_member_reads_writes_and_literal_mutations_remain_valid(
    parser_target: PythonParserTarget,
) -> None:
    from onec_runtime.bsl.semantic_lowering import (
        LoweringMode,
        SemanticNotebookLowerer,
    )

    source = (
        "e1cRuntimeКонтекст.Обычное = 1; Значение = e1cRuntimeКонтекст.Обычное; "
        'e1cRuntimeКонтекст.Insert("Другое", Значение); e1cRuntimeКонтекст.Delete("Другое");'
    )

    result = SemanticNotebookLowerer(parser_target).lower(
        source,
        mode=LoweringMode.MAIN,
    )

    assert 'e1cRuntimeКонтекст.Insert("Другое", e1cRuntimeКонтекст.Значение)' in result.source
    assert 'e1cRuntimeКонтекст.Delete("Другое")' in result.source


@pytest.mark.parametrize("mode", ("main", "capture"))
def test_same_method_names_route_to_distinct_immutable_module_receivers(
    parser_target: PythonParserTarget,
    mode: str,
) -> None:
    from onec_runtime.bsl.semantic_lowering import (
        LoweringMode,
        SemanticNotebookLowerer,
        WorkerExport,
    )

    exports = (
        WorkerExport("МодульА.Рассчитать", "Рассчитать", receiver_module="МодульА"),
        WorkerExport("МодульБ.Рассчитать", "Рассчитать", receiver_module="МодульБ"),
    )
    lowerer = SemanticNotebookLowerer(
        parser_target,
        worker_exports=(WorkerExport("Старый", "Старый"),),
    )

    result = lowerer.lower(
        "А = МодульА.Рассчитать(); Б = МодульБ.Рассчитать();",
        mode=LoweringMode(mode),
        worker_exports=exports,
    )

    assert 'Modules.Получить("МодульА").Рассчитать()' in result.source
    assert 'Modules.Получить("МодульБ").Рассчитать()' in result.source
    assert lowerer.worker_export_identity == (("старый", "старый", None),)
    parser_target.parse(result.source, "БлокНоутбука")


def test_per_call_worker_catalog_requires_an_immutable_tuple(
    parser_target: PythonParserTarget,
) -> None:
    from onec_runtime.bsl.semantic_lowering import (
        LoweringMode,
        SemanticNotebookLowerer,
        WorkerExport,
    )

    with pytest.raises(TypeError, match="immutable tuple"):
        SemanticNotebookLowerer(parser_target).lower(
            "Результат = МодульА.Рассчитать();",
            mode=LoweringMode.MAIN,
            worker_exports=[  # type: ignore[arg-type]
                WorkerExport(
                    "МодульА.Рассчитать",
                    "Рассчитать",
                    receiver_module="МодульА",
                )
            ],
        )


@pytest.mark.parametrize(
    "public_path,method,receiver",
    (
        (
            "МодульА.Рассчитать); ВызватьИсключение",
            "Рассчитать); ВызватьИсключение",
            "МодульА",
        ),
        ("Модуль А.Рассчитать", "Рассчитать", "Модуль А"),
    ),
)
def test_worker_export_catalog_rejects_non_identifier_components(
    public_path: str,
    method: str,
    receiver: str,
    parser_target: PythonParserTarget,
) -> None:
    from onec_runtime.bsl.semantic_lowering import (
        LoweringMode,
        SemanticNotebookLowerer,
        WorkerExport,
    )

    with pytest.raises(ValueError, match="identifier"):
        SemanticNotebookLowerer(parser_target).lower(
            "Результат = 1;",
            mode=LoweringMode.MAIN,
            worker_exports=(
                WorkerExport(public_path, method, receiver_module=receiver),
            ),
        )
