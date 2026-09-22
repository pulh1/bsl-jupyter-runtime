import os
from hashlib import sha256
from pathlib import Path
import subprocess
import sys

import pytest

from onec_runtime.bsl.lexer import tokenize
from onec_runtime.bsl.notebook_cells import split_notebook_cell
from onec_runtime.bsl.parser_artifact_identity import verify_parser_artifact_manifest
from onec_runtime.bsl.parser_target import GeneratedParserMetadata, PythonParserTarget
from onec_runtime.bsl.source_maps import (
    LineIndex,
    MappingRelation,
    SourceArtifactKind,
    SourceSpan,
    SourceUnitKind,
    SourceUnitRef,
    source_sha256,
)
from onec_runtime.errors import ProtocolError
from tools.grammar_corpus_spike import build_combined_development_target


WORKSPACE = Path(__file__).parents[2]
COMBINED_GRAMMAR = WORKSPACE / "grammar" / "bsl-server-strict.grammar"
PARSERGEN_SRC = Path(os.environ.get(
    "ONEC_PARSERGEN_SRC", str(WORKSPACE / "tests" / "fixtures" / "parsergen" / "src")
))
VISIBLE_CAPTURE_CELL = (
    'КонтекстОтладки.Результат.Добавить("ИзИнструкции");\n'
    "КоличествоПосле = КонтекстОтладки.Результат.Количество();\n"
    "РезультатИнструкции = КоличествоПосле;"
)


@pytest.fixture
def target() -> PythonParserTarget:
    from tools.generate_bsl_semantic_parser import render_generated_module

    namespace: dict[str, object] = {}
    exec(render_generated_module(COMBINED_GRAMMAR).decode("utf-8"), namespace)
    manifest = verify_parser_artifact_manifest(
        str(namespace["PARSER_ARTIFACT_MANIFEST_JSON"])
    )
    return PythonParserTarget(
        namespace["GeneratedParser"],  # type: ignore[arg-type]
        namespace["GeneratedParseError"],  # type: ignore[arg-type]
        GeneratedParserMetadata(
            manifest.identity_sha256,
            manifest.parsergen_package_sha256,
            manifest,
        ),
    )


def _development_target() -> PythonParserTarget:
    return build_combined_development_target(COMBINED_GRAMMAR, PARSERGEN_SRC)


def _unit(unit_id: str, revision: int, source: str) -> SourceUnitRef:
    return SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL,
        unit_id,
        revision,
        source_sha256(source),
    )


def test_mixed_cell_projects_worker_and_statements_to_one_visible_unit(
    target: PythonParserTarget,
) -> None:
    """Break caught: mixed-cell branches must not lose visible coordinates."""
    source = (
        "Процедура Удвоить(Значение)\n"
        "    Возврат Значение * 2;\n"
        "КонецПроцедуры\n\n"
        "Исходное = 21;\n"
        "Результат = Удвоить(Исходное);"
    )
    unit = _unit("cell-main", 7, source)

    cell = split_notebook_cell(target, source, source_unit=unit)

    assert cell.visible.text == source
    assert cell.worker is not None and " Экспорт" in cell.worker.text
    assert cell.worker.artifact.kind is SourceArtifactKind.WORKER_PROJECTION
    export_offset = cell.worker.text.index(" Экспорт")
    inserted = cell.worker.source_map.map_offset(export_offset)
    assert inserted.relation is MappingRelation.SYNTHETIC
    assert inserted.synthetic_region == "notebook_method_export"
    assert inserted.anchor_unit == unit
    assert cell.statements is not None
    assert cell.statements.artifact.kind is SourceArtifactKind.STATEMENT_PROJECTION
    result_offset = cell.statements.text.index("Результат")
    mapped = cell.statements.source_map.map_offset(result_offset)
    assert mapped.unit == unit
    assert mapped.origin_span is not None
    assert LineIndex(source).offset_to_line_column(mapped.origin_span.start) == (6, 1)


def test_method_only_cell_has_only_mapped_worker_projection(
    target: PythonParserTarget,
) -> None:
    """Break caught: a method-only cell must not invent a statement branch."""
    source = "Функция Ответ()\nВозврат 42;\nКонецФункции;"
    unit = _unit("method-only", 1, source)

    cell = split_notebook_cell(target, source, source_unit=unit)

    assert cell.worker is not None
    assert cell.worker_source == "Функция Ответ() Экспорт\nВозврат 42;\nКонецФункции;"
    assert cell.statements is None
    assert cell.statement_source == ""
    assert cell.has_methods is True
    assert cell.has_statements is False


def test_statement_only_cell_has_only_exact_statement_projection(
    target: PythonParserTarget,
) -> None:
    """Break caught: a statement-only cell must not invent a Worker branch."""
    source = "Первое = 1;\nВторое = 2;"
    unit = _unit("statement-only", 2, source)

    cell = split_notebook_cell(target, source, source_unit=unit)

    assert cell.worker is None
    assert cell.worker_source == ""
    assert cell.statements is not None
    assert cell.statement_source == source
    second = cell.statements.source_map.map_offset(source.index("Второе"))
    assert second.relation is MappingRelation.EXACT
    assert second.origin_span == SourceSpan(source.index("Второе"), source.index("Второе") + 1)


@pytest.mark.parametrize(
    "source",
    (
        pytest.param(
            "Для Каждого Элемент Из Коллекция Цикл\n"
            "    Сообщить(Элемент);\n"
            "КонецЦикла;",
            id="for-each-first",
        ),
        pytest.param(
            "Для Номер = 1 По 2 Цикл\n"
            "    Сообщить(Номер);\n"
            "КонецЦикла;",
            id="for-range-first",
        ),
        pytest.param(
            "Начало = 1;\n"
            "Для Каждого Элемент Из Коллекция Цикл\n"
            "    Сообщить(Элемент);\n"
            "КонецЦикла;",
            id="for-each-after-statement",
        ),
    ),
)
def test_top_level_for_loop_keeps_keyword_through_notebook_projection(
    target: PythonParserTarget, source: str,
) -> None:
    """Break caught: a projected top-level loop must still be executable BSL."""
    from onec_runtime.bsl.semantic_lowering import LoweringMode, SemanticNotebookLowerer

    unit = _unit("top-level-for", 1, source)
    cell = split_notebook_cell(target, source, source_unit=unit)

    assert cell.statements is not None
    assert cell.statement_source == source
    keyword_offset = source.index("Для")
    mapped = cell.statements.source_map.map_offset(keyword_offset)
    assert mapped.relation is MappingRelation.EXACT
    assert mapped.origin_span == SourceSpan(keyword_offset, keyword_offset + 1)

    lowered = SemanticNotebookLowerer(target).lower(
        cell.statement_source, mode=LoweringMode.CAPTURE,
    )
    target.parse(lowered.source, "БлокНоутбука")


def test_adjacent_statements_copy_comment_separator_exactly(
    target: PythonParserTarget,
) -> None:
    """Break caught: copied comments must retain exact visible provenance."""
    source = "Первое = 1;\n// между\nВторое = 2;"
    unit = _unit("comment-gap", 3, source)

    cell = split_notebook_cell(target, source, source_unit=unit)

    assert cell.statements is not None
    assert cell.statements.text == source
    comment = cell.statements.source_map.map_offset(source.index("//"))
    assert comment.relation is MappingRelation.EXACT
    assert comment.unit == unit
    assert comment.origin_span == SourceSpan(source.index("//"), source.index("//") + 1)


def test_alternating_cell_uses_independent_synthetic_branch_joins(
    target: PythonParserTarget,
) -> None:
    """Break caught: removed opposite-branch text must never share coordinates."""
    source = (
        "Повтор = 1;\n"
        "Процедура Первая()\nКонецПроцедуры;\n"
        "Повтор = 1;\n"
        "Процедура Вторая()\nКонецПроцедуры;"
    )
    unit = _unit("alternating", 4, source)

    cell = split_notebook_cell(target, source, source_unit=unit)

    assert cell.worker is not None and cell.statements is not None
    assert "Повтор = 1" not in cell.worker.text
    assert "Процедура" not in cell.statements.text
    statement_join = len("Повтор = 1;")
    joined = cell.statements.source_map.map_offset(statement_join)
    assert joined.relation is MappingRelation.SYNTHETIC
    assert joined.synthetic_region == "notebook_projection_join"
    first_visible = source.index("Повтор")
    second_visible = source.rindex("Повтор")
    first_projected = cell.statements.text.index("Повтор")
    second_projected = cell.statements.text.rindex("Повтор")
    assert cell.statements.source_map.map_offset(first_projected).origin_span == SourceSpan(
        first_visible, first_visible + 1
    )
    assert cell.statements.source_map.map_offset(second_projected).origin_span == SourceSpan(
        second_visible, second_visible + 1
    )


def test_crlf_and_non_bmp_offsets_are_not_normalized(
    target: PythonParserTarget,
) -> None:
    """Break caught: CRLF and non-BMP text must remain Python code-point exact."""
    source = (
        "Процедура Метод()\r\n"
        "КонецПроцедуры\r\n\r\n"
        'Маркер = "😀";\r\n'
        "Итог = Маркер;"
    )
    unit = _unit("crlf-unicode", 5, source)

    cell = split_notebook_cell(target, source, source_unit=unit)

    assert cell.statements is not None
    assert cell.statements.text == 'Маркер = "😀";\r\nИтог = Маркер;'
    assert cell.statements.artifact.line_ending_kind == "crlf"
    emoji_projected = cell.statements.text.index("😀")
    emoji_visible = source.index("😀")
    assert cell.statements.source_map.map_offset(emoji_projected).origin_span == SourceSpan(
        emoji_visible, emoji_visible + 1
    )
    result_projected = cell.statements.text.index("Итог")
    mapped = cell.statements.source_map.map_offset(result_projected)
    assert mapped.origin_span is not None
    assert LineIndex(source).offset_to_line_column(mapped.origin_span.start) == (5, 1)


def test_duplicate_method_names_fail_before_projection(
    target: PythonParserTarget,
) -> None:
    """Break caught: case-insensitive duplicate exports must remain deterministic."""
    source = (
        "Процедура Дубль()\nКонецПроцедуры;\n"
        "Процедура дУБЛЬ()\nКонецПроцедуры;"
    )

    with pytest.raises(ProtocolError, match="duplicate method"):
        split_notebook_cell(target, source, source_unit=_unit("duplicate", 6, source))


def test_lexer_normalizes_keywords_but_not_members() -> None:
    tokens = tokenize("Если Расчет.Если Тогда НДФЛ = 1; КонецЕсли;")

    assert [token.type for token in tokens] == [
        "ЕСЛИ", "ID", ".", "ID", "ТОГДА", "ID", "=", "NUMBER", ";", "КОНЕЦЕСЛИ", ";"
    ]


def test_loads_strict_grammar_and_parses_capture_cell_and_procedure_module() -> None:
    if not COMBINED_GRAMMAR.is_file() or not PARSERGEN_SRC.is_dir():
        pytest.skip("local grammar and QueryConsole1C parsergen are required")

    target = _development_target()

    target.parse(VISIBLE_CAPTURE_CELL, "БлокНоутбука")
    target.parse(
        "Процедура ПроверитьКонтекст() Экспорт\n"
        "Результат = КонтекстОтладки.Результат.Количество();\n"
        "КонецПроцедуры",
        "Модуль",
    )
    assert target.metadata.manifest is not None
    assert target.metadata.manifest.grammar_source_sha256 == sha256(
        COMBINED_GRAMMAR.read_bytes()
    ).hexdigest()
    assert target.metadata.parsergen_package_sha256 == (
        "bf959e9d30d54960585189106faa8bb14b23740b009e09d2f7675c678849caed"
    )
    assert target.generated_source is not None
    assert target.lookahead == 1
    assert target.validation_warnings == ()
    assert 'PARSERGEN_BACKEND_ID = "python-semantic-direct-v1"' in target.generated_source
    assert "PRODUCTIONS =" not in target.generated_source
    assert "class _Frame" not in target.generated_source
    assert target.parser_ir is not None


def test_strict_grammar_is_ll1_and_accepts_real_zup_compatibility_cases() -> None:
    if not PARSERGEN_SRC.is_dir():
        pytest.skip("local QueryConsole1C parsergen is required")

    target = _development_target()

    real_zup_cases = (
        # A final semicolon before a block boundary is optional in real modules.
        "Если Истина Тогда\nРезультат = 1\nКонецЕсли;",
        "Функция ПолучитьРезультат()\nВозврат 1\nКонецФункции",
        # A method header may carry a semicolon.
        "Процедура Обработать();\nРезультат = 1;\nКонецПроцедуры",
        # Empty statements occur in platform-accepted source.
        "Результат = 1;;Результат = 2;",
        # The extended raise form accepts multiple actual parameters.
        'ВызватьИсключение("Ошибка", КатегорияОшибки, 1);',
    )
    for source in real_zup_cases:
        target.parse(source, "Модуль")

    assert target.lookahead == 1


def test_python_target_parses_large_realistic_module_without_leaking_recursion_limit() -> None:
    if not PARSERGEN_SRC.is_dir():
        pytest.skip("local QueryConsole1C parsergen is required")

    target = _development_target()
    source = "\n".join(
        f"Результат{number} = {number};" for number in range(1_200)
    )
    original_limit = sys.getrecursionlimit()

    target.parse(source, "Модуль")

    assert sys.getrecursionlimit() == original_limit


def test_generated_and_interpreted_targets_accept_the_same_token_stream() -> None:
    if not PARSERGEN_SRC.is_dir():
        pytest.skip("local QueryConsole1C parsergen is required")

    target = _development_target()
    tokens = tokenize("Результат = 1;")

    target.parse_tokens(tokens, "Модуль")
    target.recognize_tokens_interpreted(tokens, "Модуль")
    target.parse_tokens_interpreted(tokens, "Модуль")


def test_grammar_spike_writes_utf8_when_console_encoding_is_cp1252(tmp_path: Path) -> None:
    if not COMBINED_GRAMMAR.is_file() or not PARSERGEN_SRC.is_dir():
        pytest.skip("local grammar and QueryConsole1C parsergen are required")
    output = tmp_path / "grammar-result.json"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(WORKSPACE / "src")
    environment["PYTHONIOENCODING"] = "cp1252"

    completed = subprocess.run(
        [
            sys.executable,
            str(WORKSPACE / "tools" / "grammar_spike.py"),
            "--grammar",
            str(WORKSPACE / "grammar" / "bsl-server-strict.grammar"),
            "--parsergen-src",
            str(PARSERGEN_SRC),
            "--output",
            str(output),
        ],
        cwd=WORKSPACE,
        env=environment,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    assert '"status": "PASS"' in output.read_text(encoding="utf-8")
    assert "БлокНоутбука" in completed.stdout.decode("utf-8")


@pytest.mark.parametrize(
    "tool",
    (
        "grammar_spike.py",
        "grammar_corpus_spike.py",
        "grammar_parser_benchmark.py",
    ),
)
def test_combined_development_tool_clis_accept_only_combined_grammar(
    tool: str,
) -> None:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(WORKSPACE / "src")

    completed = subprocess.run(
        [sys.executable, str(WORKSPACE / "tools" / tool), "--help"],
        cwd=WORKSPACE,
        env=environment,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr.decode(
        "utf-8",
        errors="replace",
    )
    help_text = completed.stdout.decode("utf-8")
    assert "--grammar" in help_text
    assert "--syntax" not in help_text
    assert "--full-profile" not in help_text
