from __future__ import annotations

import os
from dataclasses import fields
from pathlib import Path

import pytest


WORKSPACE = Path(__file__).parents[2]
COMBINED_GRAMMAR = WORKSPACE / "grammar" / "bsl-server-strict.grammar"
PARSERGEN_SRC = Path(os.environ.get(
    "ONEC_PARSERGEN_SRC", str(WORKSPACE / "tests" / "fixtures" / "parsergen" / "src")
))
ENTRYPOINTS = {
    "module": "Модуль",
    "notebook": "БлокНоутбука",
    "notebook_cell": "ЯчейкаНоутбука",
    "expression": "ОтдельноеВыражение",
    "statement": "ОтдельнаяИнструкция",
    "preprocessor": "ДирективаПрепроцессора",
}

pytestmark = pytest.mark.skipif(
    not PARSERGEN_SRC.is_dir(),
    reason="ONEC_PARSERGEN_SRC is required for parsergen validation tests",
)


def _generate_semantic_parser():
    from tools.generate_bsl_semantic_parser import build_combined_generated_target

    target = build_combined_generated_target(
        COMBINED_GRAMMAR,
        ENTRYPOINTS,
        artifact_label="bsl-semantic-ast-test",
        runtime_source_span=True,
    )
    namespace: dict[str, object] = {}
    exec(compile(target.rendered, str(COMBINED_GRAMMAR), "exec"), namespace)
    return target.generated, namespace


def test_strict_bsl_grammar_generates_semantic_python_ast() -> None:
    generated, _ = _generate_semantic_parser()

    assert {"Module", "CodeBlock", "AccessChain"} <= {
        node.name for node in generated.ast_schema
    }


def test_semantic_ast_preserves_notebook_names_calls_and_exact_spans() -> None:
    from onec_runtime.bsl.lexer import tokenize

    _, namespace = _generate_semantic_parser()
    source = "НДФЛ = Расчет.Ндфл.Посчитать(); Сообщить(НДФЛ);"

    block = namespace["GeneratedParser"]().parse(tokenize(source), "notebook")

    assignment = block.First
    assert type(assignment) is namespace["SimpleStatement"]
    assert assignment.Target.Root == "НДФЛ"
    call = (
        assignment.Value.First.First.First.First.First.Value
    )
    assert type(call) is namespace["AccessChain"]
    assert call.Root == "Расчет"
    assert tuple(item.Name for item in call.Postfix) == ("Ндфл", "Посчитать")
    assert call.span == namespace["SourceSpan"](7, 30)

    message = block.Rest.First.Target
    assert message.Root == "Сообщить"
    assert message.Arguments is not None
    assert message.span == namespace["SourceSpan"](32, 46)


def test_semantic_ast_exposes_method_parameters_locals_and_body() -> None:
    from onec_runtime.bsl.lexer import tokenize

    _, namespace = _generate_semantic_parser()
    source = (
        "Процедура Проверить(Знач Параметр)\n"
        "Перем Локальная;\n"
        "Локальная = Параметр;\n"
        "КонецПроцедуры"
    )

    module = namespace["GeneratedParser"]().parse(tokenize(source), "module")

    declaration = module.Elements.Item.Declaration
    assert type(declaration) is namespace["ProcedureDeclaration"]
    assert declaration.Name == "Проверить"
    assert declaration.Parameters.Items[0].Name == "Параметр"
    assert declaration.Body.LocalDeclarations[0].Names == ("Локальная",)
    assert declaration.Body.Code.First.Target.Root == "Локальная"
    assert module.span == namespace["SourceSpan"](0, len(source))


def test_combined_grammar_preserves_representative_shapes_and_spans() -> None:
    from onec_runtime.bsl.lexer import tokenize

    _, namespace = _generate_semantic_parser()
    source = (
        "Перем Глобальная Экспорт;\n"
        "Процедура Проверить(Знач Параметр) Экспорт\n"
        "Локальная = Расчет.Ндфл.Посчитать(Параметр);\n"
        "КонецПроцедуры"
    )

    module = namespace["GeneratedParser"]().parse(tokenize(source), "module")
    method = module.Elements.Item
    declaration = method.Declaration
    code = declaration.Body.Code
    statement = code.First
    access = statement.Value.First.First.First.First.First.Value

    expected_fields = {
        "Module": ("Declarations", "Elements", "span"),
        "Method": ("Decorations", "Async", "Declaration", "span"),
        "ProcedureDeclaration": ("Name", "Parameters", "Export", "Body", "span"),
        "CodeBlock": ("First", "Rest", "span"),
        "SimpleStatement": ("Target", "Value", "span"),
        "AccessChain": ("Root", "Arguments", "Postfix", "span"),
    }
    for name, names in expected_fields.items():
        assert tuple(field.name for field in fields(namespace[name])) == names

    assert type(module) is namespace["Module"]
    assert type(method) is namespace["Method"]
    assert type(declaration) is namespace["ProcedureDeclaration"]
    assert type(code) is namespace["CodeBlock"]
    assert type(statement) is namespace["SimpleStatement"]
    assert type(access) is namespace["AccessChain"]
    span = namespace["SourceSpan"]
    assert module.span == span(0, 128)
    assert method.span == span(26, 128)
    assert declaration.span == span(26, 128)
    assert code.span == span(69, 113)
    assert statement.span == span(69, 112)
    assert access.span == span(81, 112)


def test_combined_grammar_preserves_legacy_parse_error_tuple() -> None:
    from onec_runtime.bsl.lexer import tokenize

    _, namespace = _generate_semantic_parser()
    tokens = tokenize("Результат = ;")

    with pytest.raises(namespace["GeneratedParseError"]) as new_caught:
        namespace["GeneratedParser"]().parse(tokens, "notebook")

    assert (
        new_caught.value.position,
        new_caught.value.actual,
        new_caught.value.expected,
    ) == (
        2,
        ";",
        (
            "(",
            "+",
            "-",
            "?",
            "DATETIME",
            "ID",
            "NULL",
            "NUMBER",
            "STRING",
            "ЖДАТЬ",
            "ИСТИНА",
            "ЛОЖЬ",
            "НЕ",
            "НЕОПРЕДЕЛЕНО",
            "НОВЫЙ",
        ),
    )


def test_runtime_parser_adapter_returns_generated_semantic_ast() -> None:
    from onec_runtime.bsl.parser_artifact_identity import verify_parser_artifact_manifest
    from onec_runtime.bsl.parser_target import (
        BslParseError,
        GeneratedParserMetadata,
        PythonParserTarget,
    )

    _, namespace = _generate_semantic_parser()
    manifest = verify_parser_artifact_manifest(
        str(namespace["PARSER_ARTIFACT_MANIFEST_JSON"])
    )
    target = PythonParserTarget(
        namespace["GeneratedParser"],  # type: ignore[arg-type]
        namespace["GeneratedParseError"],  # type: ignore[arg-type]
        GeneratedParserMetadata(
            manifest.identity_sha256,
            manifest.parsergen_package_sha256,
            manifest,
        ),
    )

    block = target.parse_ast("Результат = 1;", "БлокНоутбука")

    assert type(block).__name__ == "CodeBlock"
    assert type(block.First).__name__ == "SimpleStatement"
    assert block.First.Target.Root == "Результат"
    assert target.development is None

    with pytest.raises(BslParseError, match="Unexpected") as caught:
        target.parse_ast("Результат = ;", "БлокНоутбука")
    assert "expected" in str(caught.value)
