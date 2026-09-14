from dataclasses import FrozenInstanceError
import gc
import weakref

import pytest

from onec_runtime.bsl.notebook_cells import split_notebook_cell
from onec_runtime.bsl.parser_target import PythonParserTarget
from onec_runtime.bsl.source_maps import MappingRelation, SourceUnitKind, SourceUnitRef, source_sha256
from onec_runtime.errors import ProtocolError


def parse_cell(source: str, revision: int = 1, unit_id: str = "cell"):
    unit = SourceUnitRef(SourceUnitKind.NOTEBOOK_CELL, unit_id, revision, source_sha256(source))
    return split_notebook_cell(PythonParserTarget.from_generated(), source, source_unit=unit), unit


def test_single_message_statement_with_leading_newline_projects_without_crashing():
    cell, _ = parse_cell('\nСообщить("Привет мир!")\n')
    assert cell.statement_source == 'Сообщить("Привет мир!")'
    assert cell.worker_source == ""


def test_multiple_statements_with_leading_newline_and_final_missing_semicolon():
    source = (
        '\nЗапрос = Новый Запрос;\n'
        'Запрос.Текст = "Выбрать 1 как поле";\n'
        'Рез = Запрос.Выполнить()\n'
    )
    cell, _ = parse_cell(source)
    assert cell.statement_source == source.strip()


def merge(previous, cell):
    from onec_runtime.bsl.notebook_methods import merge_notebook_methods

    return merge_notebook_methods(previous, cell)


def test_worker_message_instrumentation_preserves_calls_and_visible_origin():
    from onec_runtime.bsl.notebook_methods import instrument_notebook_worker_messages

    source = (
        'Процедура Показать()\n'
        '    // Сообщить("комментарий");\n'
        '    Текст = "Сообщить(строка)";\n'
        '    Объект.Сообщить("метод");\n'
        '    Сообщить("Успех");\n'
        'КонецПроцедуры'
    )
    cell, unit = parse_cell(source)
    candidate = merge(None, cell)

    instrumented = instrument_notebook_worker_messages(candidate.mapped_source)

    assert candidate.intercepts_messages
    assert 'Объект.Сообщить("метод")' in instrumented.text
    assert '// Сообщить("комментарий")' in instrumented.text
    assert '"Сообщить(строка)"' in instrumented.text
    assert '__OnecWorkerMessage("Успех")' in instrumented.text
    assert instrumented.text.count('__OnecWorkerMessage("Успех")') == 1
    mapped_call = instrumented.source_map.map_offset(
        instrumented.text.index('__OnecWorkerMessage("Успех")')
    )
    assert mapped_call.relation is MappingRelation.DERIVED
    assert mapped_call.unit == unit
    assert mapped_call.origin_span.start == source.index('Сообщить("Успех")')
    assert instrumented.source_map.map_offset(0).relation is MappingRelation.SYNTHETIC
    PythonParserTarget.from_generated().parse_ast(instrumented.text, "Модуль")


def test_worker_messages_instrument_only_bare_russian_and_english_calls():
    from onec_runtime.bsl.notebook_methods import instrument_notebook_worker_messages

    source = (
        'Процедура Показать()\n'
        '    Объект.Message("метод");\n'
        '    Message("one");\n'
        '    Сообщить("два");\n'
        'КонецПроцедуры'
    )
    cell, _ = parse_cell(source)
    candidate = merge(None, cell)
    instrumented = instrument_notebook_worker_messages(candidate.mapped_source)

    assert 'Объект.Message("метод")' in instrumented.text
    assert '__OnecWorkerMessage("one")' in instrumented.text
    assert '__OnecWorkerMessage("два")' in instrumented.text
    assert candidate.mapped_source.text.count('__OnecWorkerMessage') == 0


def test_worker_without_messages_keeps_original_module_source():
    from onec_runtime.bsl.notebook_methods import instrument_notebook_worker_messages

    cell, _ = parse_cell('Процедура Показать()\nОбъект.Сообщить("метод");\nКонецПроцедуры')
    candidate = merge(None, cell)

    assert not candidate.intercepts_messages
    assert instrument_notebook_worker_messages(candidate.mapped_source) is candidate.mapped_source


def test_persistent_method_reads_preserve_shadows_literals_and_origins():
    from onec_runtime.bsl.notebook_method_globals import bind_notebook_method_globals

    source = (
        'Функция ПолучитьА(Параметр)\n'
        '    Перем Локальное;\n'
        '    // __OnecNotebookGlobals и А в комментарии\n'
        '    Локальное = "__OnecNotebookGlobals: А в строке";\n'
        '    Для Сч = 1 По 2 Цикл\n'
        '        Локальное = Локальное + А.Поле;\n'
        '    КонецЦикла;\n'
        '    Возврат А + Параметр;\n'
        'КонецФункции\n'
        'Функция ПараметрА(А)\n'
        '    Возврат А;\n'
        'КонецФункции'
    )
    cell, unit = parse_cell(source)
    candidate = merge(None, cell)
    bound, names = bind_notebook_method_globals(
        candidate.mapped_source,
        context_names=('А', 'Сч', 'Локальное', 'Параметр'),
        exports=candidate.exports,
    )

    prefix = '__OnecNotebookGlobals.'
    assert names == ('А',)
    assert bound.text.startswith('Перем __OnecNotebookGlobals Экспорт;\n')
    assert bound.text.count(prefix + 'А') == 2
    assert 'Возврат А;\nКонецФункции' in bound.text
    assert '// __OnecNotebookGlobals и А в комментарии' in bound.text
    assert '"__OnecNotebookGlobals: А в строке"' in bound.text
    assert 'Для Сч = 1 По 2 Цикл' in bound.text
    assert 'Локальное = Локальное + ' + prefix + 'А.Поле;' in bound.text
    root_offset = bound.text.index(prefix + 'А.Поле') + len(prefix)
    root = bound.source_map.map_offset(root_offset)
    assert root.relation is MappingRelation.EXACT
    assert root.unit == unit
    assert root.origin_span.start == source.index('А.Поле')
    PythonParserTarget.from_generated().parse_ast(bound.text, 'Модуль')


def test_notebook_method_binds_known_name_and_leaves_platform_name_native():
    from onec_runtime.bsl.notebook_method_globals import bind_notebook_method_globals

    source = (
        'Функция Проверка()\n'
        '    Если ВидДвиженияНакопления.Приход = ВидДвиженияНакопления.Приход Тогда\n'
        '        Возврат ПроцентПовышения;\n'
        '    КонецЕсли;\n'
        'КонецФункции'
    )
    cell, _ = parse_cell(source)
    candidate = merge(None, cell)

    bound, names = bind_notebook_method_globals(
        candidate.mapped_source,
        context_names=('ПроцентПовышения',),
        exports=candidate.exports,
    )

    assert names == ('ПроцентПовышения',)
    assert 'Возврат __OnecNotebookGlobals.ПроцентПовышения;' in bound.text
    assert bound.text.count('ВидДвиженияНакопления.Приход') == 2
    assert '__OnecNotebookGlobals.ВидДвиженияНакопления' not in bound.text


def test_replacing_helper_retains_caller_order_and_exact_origins():
    first, unit1 = parse_cell("Функция А()\nВозврат Б();\nКонецФункции\nФункция Б()\nВозврат 1;\nКонецФункции")
    second, unit2 = parse_cell("Функция Б()\nВозврат 2;\nКонецФункции", 2)
    result = merge(merge(None, first), second)
    assert [item.method for item in result.exports] == ["А", "Б"]
    for token, unit, source in (("Возврат Б", unit1, first.visible.text), ("Возврат 2", unit2, second.visible.text)):
        mapped = result.mapped_source.source_map.map_offset(result.mapped_source.text.index(token))
        assert mapped.unit == unit
        assert mapped.origin_span.start == source.index(token)
        assert result.visible_source_context.line_column(unit, mapped.origin_span.start) == (2, 1)
    assert "Возврат 1" not in result.mapped_source.text


def test_case_insensitive_replacement_and_new_name_append():
    first, _ = parse_cell("Процедура Альфа()\nКонецПроцедуры\nПроцедура Бета()\nКонецПроцедуры")
    second, _ = parse_cell("Процедура альфа(Аргумент)\nКонецПроцедуры\nПроцедура Гамма()\nКонецПроцедуры", 2)
    result = merge(merge(None, first), second)
    assert [item.method for item in result.exports] == ["альфа", "Бета", "Гамма"]
    assert "Альфа()" not in result.mapped_source.text


def test_mixed_and_statement_only_cells_do_not_retain_statements():
    cell, _ = parse_cell("Процедура А()\nКонецПроцедуры\nСекретнаяПеременная = 99;")
    statement, current = parse_cell("ДругаяПеременная = 17;", 2)
    result = merge(merge(None, cell), statement)
    assert [item.method for item in result.exports] == ["А"]
    assert "Переменная" not in result.mapped_source.text
    assert result.visible_source_context.line_column(current, 0) == (1, 1)


def test_duplicate_declarations_in_one_cell_are_rejected():
    with pytest.raises(ProtocolError, match="duplicate"):
        parse_cell("Процедура А()\nКонецПроцедуры\nПроцедура а()\nКонецПроцедуры")


@pytest.mark.parametrize("declaration, terminator", [("Функция", "КонецФункции"), ("Процедура", "КонецПроцедуры")])
@pytest.mark.parametrize("export", ["", " Экспорт"])
def test_notebook_separator_after_method_is_not_retained_as_module_body(declaration, terminator, export):
    source = f'{declaration} А(){export}\r\nСообщить("body;");\r\n{terminator};\r\nЗначение = 1;'
    first, unit1 = parse_cell(source)
    second, unit2 = parse_cell("Процедура Б()\nСообщить(2);\nКонецПроцедуры;", 2)
    result = merge(merge(None, first), second)
    assert result.mapped_source.text == (
        f'{declaration} А() Экспорт\r\nСообщить("body;");\r\n{terminator}\n'
        'Процедура Б() Экспорт\nСообщить(2);\nКонецПроцедуры'
    )
    assert first.statement_source == "Значение = 1;"
    assert first.visible.text == source
    for token, unit, original in (("body;", unit1, source), ("Сообщить(2)", unit2, second.visible.text)):
        mapped = result.mapped_source.source_map.map_offset(result.mapped_source.text.index(token))
        assert mapped.unit == unit
        assert mapped.origin_span.start == original.index(token)


def test_unicode_crlf_annotations_and_synthetic_export_and_join():
    first, unit1 = parse_cell('&НаСервере\r\nФункция А()\r\n// ёж 😀\r\nВозврат "ёж";\r\nКонецФункции')
    second, unit2 = parse_cell("Процедура Б() Экспорт\r\nКонецПроцедуры", 2)
    result = merge(merge(None, first), second)
    text = result.mapped_source.text
    assert text.startswith("&НаСервере\r\nФункция А() Экспорт\r\n")
    assert '// ёж 😀\r\nВозврат "ёж";' in text
    export = result.mapped_source.source_map.map_offset(text.index(" Экспорт"))
    assert export.relation is MappingRelation.SYNTHETIC
    assert export.synthetic_region == "notebook_method_export"
    assert export.anchor_unit == unit1
    join = result.mapped_source.source_map.map_offset(text.index("\nПроцедура Б"))
    assert join.relation is MappingRelation.SYNTHETIC
    assert join.anchor_unit == unit2
    assert result.mapped_source.source_map.map_offset(text.index("&НаСервере")).unit == unit1
    PythonParserTarget.from_generated().parse_ast(text, "ЯчейкаНоутбука")


def test_conflicting_explicit_identity_is_rejected_even_if_all_methods_replaced():
    first, _ = parse_cell("Процедура А()\nКонецПроцедуры")
    conflicting, _ = parse_cell("Процедура А(НовыйАргумент)\nКонецПроцедуры")
    previous = merge(None, first)
    with pytest.raises(ProtocolError, match="conflicting source identit"):
        merge(previous, conflicting)
    assert previous.mapped_source.text == "Процедура А() Экспорт\nКонецПроцедуры"
    assert merge(previous, first).exports == previous.exports


def test_retained_source_is_pruned_only_after_last_method_is_replaced():
    first, unit1 = parse_cell("Процедура А()\nКонецПроцедуры\nПроцедура Б()\nКонецПроцедуры")
    second, unit2 = parse_cell("Процедура А(НовыйАргумент)\nКонецПроцедуры", 2)
    third, unit3 = parse_cell("Процедура Б(НовыйАргумент)\nКонецПроцедуры", 3)
    result = merge(merge(None, first), second)
    assert result.visible_source_context.line_column(unit1, 0) == (1, 1)
    result = merge(result, third)
    assert result.visible_source_context.line_column(unit1, 0) is None
    assert result.visible_source_context.line_column(unit2, 0) == (1, 1)
    assert result.visible_source_context.line_column(unit3, 0) == (1, 1)


def test_result_and_context_ownership_are_immutable_and_nonrevealing():
    cell, unit = parse_cell('Функция А()\nВозврат "RAW_SOURCE_SECRET";\nКонецФункции')
    result = merge(None, cell)
    assert "RAW_SOURCE_SECRET" not in repr(result)
    with pytest.raises((FrozenInstanceError, AttributeError)):
        result.exports = ()
    context = result.visible_source_context
    context._indices.clear()
    assert result.visible_source_context.line_column(unit, 0) == (1, 1)


def test_empty_method_set_has_valid_empty_map():
    cell, _ = parse_cell("Значение = 1;")
    result = merge(None, cell)
    assert result.exports == ()
    assert result.mapped_source.text == ""
    assert result.mapped_source.source_map.segments[0].relation is MappingRelation.SYNTHETIC
    with pytest.raises(ValueError, match="no mappable position"):
        result.mapped_source.source_map.map_offset(0)


def test_replaced_source_and_previous_assembled_artifact_are_released():
    cell, _ = parse_cell("Процедура А()\nКонецПроцедуры")
    source_ref = weakref.ref(cell.visible)
    result = merge(None, cell)
    assembled_ref = weakref.ref(result.mapped_source)
    del cell
    for revision in range(2, 12):
        cell, _ = parse_cell(f"Процедура А()\nСообщить({revision});\nКонецПроцедуры", revision)
        result = merge(result, cell)
    gc.collect()
    assert source_ref() is None
    assert assembled_ref() is None
    assert len(result.visible_source_context._indices) == 1


def test_multi_origin_methods_pass_production_worker_admission(tmp_path):
    from onec_runtime.config import RuntimeConfig
    from onec_runtime.server_worker import NotebookWorkerArtifactBuilder, validate_production_worker_artifact
    from onec_runtime.worker_epf import read_worker_source

    caller, unit1 = parse_cell("Функция А()\nВозврат Б();\nКонецФункции")
    helper, unit2 = parse_cell("Функция Б()\nВозврат 2;\nКонецФункции", 2)
    result = merge(merge(None, caller), helper)
    platform = tmp_path / "platform"
    platform.mkdir()
    for executable in ("1cv8.exe", "1cv8c.exe", "dbgs.exe"):
        (platform / executable).write_bytes(b"stub")
    config = RuntimeConfig(tmp_path, platform)
    worker = NotebookWorkerArtifactBuilder(config)(
        result.mapped_source, result.exports, visible_source_context=result.visible_source_context
    )
    validate_production_worker_artifact(worker)
    artifact_path = next((config.build_dir / "notebook-workers").glob("*.epf"))
    assert read_worker_source(artifact_path) == result.mapped_source.text
    assert worker.exports == result.exports
    assert result.visible_source_context.line_column(unit1, 12) == (2, 1)
    assert result.visible_source_context.line_column(unit2, 12) == (2, 1)


@pytest.mark.parametrize("defect", ["bounds", "text", "hash", "missing_origin", "visible_map"])
def test_composition_rejects_unproved_origin_or_text(defect):
    from onec_runtime.bsl.source_maps import (
        MappedSource, SourceArtifactKind, SourceArtifactRef, SourceMap, SourceMapSegment,
        SourceSpan, compose_mapped_sources, mapped_visible_source,
    )

    unit = SourceUnitRef(SourceUnitKind.NOTEBOOK_CELL, "origin", 1, source_sha256("abc"))
    visible = mapped_visible_source("abc", unit)
    text = "axc" if defect == "text" else "abc"
    artifact = SourceArtifactRef(SourceArtifactKind.WORKER_PROJECTION, source_sha256(text), len(text), "none")
    start = 1 if defect == "bounds" else 0
    source_map = SourceMap(artifact, (SourceMapSegment(SourceSpan(0, 3), unit, SourceSpan(start, start + 3), MappingRelation.EXACT),))
    fragment = MappedSource(text, artifact, source_map)
    originals = (visible,)
    if defect == "hash":
        object.__setattr__(fragment, "_text", "bad")
    elif defect == "missing_origin":
        originals = ()
    elif defect == "visible_map":
        shifted = SourceMap(visible.artifact, (SourceMapSegment(SourceSpan(0, 3), unit, SourceSpan(1, 4), MappingRelation.EXACT),))
        originals = (MappedSource("abc", visible.artifact, shifted),)
    with pytest.raises(ValueError):
        compose_mapped_sources((fragment,), visible_sources=originals, kind=SourceArtifactKind.WORKER_PROJECTION)
