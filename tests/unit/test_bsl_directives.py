from __future__ import annotations

from collections.abc import Sequence

import pytest

from onec_runtime.bsl.lexer import Token, tokenize
from onec_runtime.bsl.parser_target import PythonParserTarget


def _directive_lines(
    source: str,
    tokens: Sequence[Token],
) -> tuple[tuple[tuple[Token, ...], tuple[int, int]], ...]:
    lines: list[tuple[tuple[Token, ...], tuple[int, int]]] = []
    start = 0
    for line in source.splitlines(keepends=True):
        newline_width = 2 if line.endswith("\r\n") else int(line.endswith(("\r", "\n")))
        content_end = start + len(line) - newline_width
        end = start + len(line)
        if line.startswith("#"):
            lines.append(
                (
                    tuple(token for token in tokens if start <= token.start < end),
                    (start + 1, content_end),
                )
            )
        start = end
    return tuple(lines)


def test_raw_russian_directives_parse_with_original_spans_and_decoration() -> None:
    source = (
        "#Область Внешняя\r\n"
        "#Область Внутренняя\r\n"
        "#Если Сервер Тогда\r\n"
        "#ИначеЕсли Клиент Тогда\r\n"
        "#Иначе\r\n"
        "#КонецЕсли\r\n"
        '#Использовать "ОбщаяБиблиотека"\r\n'
        "#Native\r\n"
        "#Stack\r\n"
        "#КонецОбласти\r\n"
        "#КонецОбласти\r\n"
        "&НаСервере\r\n"
        "Процедура Проверить()\r\n"
        "КонецПроцедуры\r\n"
    )
    tokens = tokenize(source)
    target = PythonParserTarget.from_generated()
    directive_lines = _directive_lines(source, tokens)

    parsed = tuple(
        target.parse_tokens_ast(line_tokens, "ДирективаПрепроцессора")
        for line_tokens, _span in directive_lines
    )

    assert tuple(type(node).__name__ for node in parsed) == (
        "RegionDirective",
        "RegionDirective",
        "PreprocessorIfDirective",
        "PreprocessorElseIfDirective",
        "PreprocessorElseDirective",
        "PreprocessorEndIfDirective",
        "UseDirective",
        "NativeDirective",
        "StackDirective",
        "EndRegionDirective",
        "EndRegionDirective",
    )
    assert tuple((node.span.start, node.span.end) for node in parsed) == tuple(
        span for _tokens, span in directive_lines
    )
    assert parsed[0].Name == "Внешняя"
    assert parsed[1].Name == "Внутренняя"
    assert parsed[6].Library.Value == '"ОбщаяБиблиотека"'

    decoration_start = source.index("&НаСервере")
    module = target.parse_tokens_ast(
        tuple(token for token in tokens if token.start >= decoration_start),
        "Модуль",
    )
    method = module.Elements.Item
    decoration = method.Decorations[0]
    assert decoration.Name == "НаСервере"
    assert (decoration.span.start, decoration.span.end) == (
        decoration_start,
        decoration_start + len("&НаСервере"),
    )
    assert method.span.start == decoration_start
    assert method.span.end == source.index("КонецПроцедуры") + len("КонецПроцедуры")


def test_grammar_declared_english_directive_spellings_parse() -> None:
    source = (
        "#Region Outer\n"
        "#If Server Then\n"
        "#ElsIf Client Then\n"
        "#Else\n"
        "#EndIf\n"
        "#Use SharedLibrary\n"
        "#EndRegion\n"
    )
    tokens = tokenize(source)
    target = PythonParserTarget.from_generated()
    directive_lines = _directive_lines(source, tokens)

    parsed = tuple(
        target.parse_tokens_ast(line_tokens, "ДирективаПрепроцессора")
        for line_tokens, _span in directive_lines
    )

    assert tuple(type(node).__name__ for node in parsed) == (
        "RegionDirective",
        "PreprocessorIfDirective",
        "PreprocessorElseIfDirective",
        "PreprocessorElseDirective",
        "PreprocessorEndIfDirective",
        "UseDirective",
        "EndRegionDirective",
    )
    assert parsed[0].Name == "Outer"
    assert parsed[5].Library.Value == "SharedLibrary"
    assert tuple((node.span.start, node.span.end) for node in parsed) == tuple(
        span for _tokens, span in directive_lines
    )


def test_use_is_contextual_directive_and_valid_parameter_name() -> None:
    target = PythonParserTarget.from_generated()
    directive = '#Использовать "ОбщаяБиблиотека"\n'
    module_source = (
        "Процедура Проверить(Использовать) Экспорт\n"
        "    Если Использовать Тогда\n"
        "        Возврат;\n"
        "    КонецЕсли;\n"
        "КонецПроцедуры\n"
    )

    directive_tokens = tokenize(directive)
    parsed_directive = target.parse_tokens_ast(
        directive_tokens,
        "ДирективаПрепроцессора",
    )
    module_tokens = tokenize(module_source)
    module = target.parse_tokens_ast(module_tokens, "Модуль")

    assert type(parsed_directive).__name__ == "UseDirective"
    assert parsed_directive.Library.Value == '"ОбщаяБиблиотека"'
    assert tuple(token.type for token in directive_tokens[:2]) == (
        "#",
        "ИСПОЛЬЗОВАТЬ",
    )
    assert tuple(
        token.type
        for token in module_tokens
        if token.text.casefold() == "использовать"
    ) == ("ID", "ID")
    assert module.Elements.Item.Declaration.Parameters.Items[0].Name == "Использовать"


@pytest.mark.parametrize(
    "identifier",
    (
        "Область",
        "Region",
        "КонецОбласти",
        "EndRegion",
        "Использовать",
        "Use",
        "Native",
        "Stack",
    ),
)
def test_preprocessor_only_keywords_are_identifiers_outside_hash(
    identifier: str,
) -> None:
    source = (
        f"Процедура Проверить({identifier}) Экспорт\n"
        "КонецПроцедуры\n"
    )

    tokens = tokenize(source)
    module = PythonParserTarget.from_generated().parse_tokens_ast(tokens, "Модуль")

    assert next(token for token in tokens if token.text == identifier).type == "ID"
    assert module.Elements.Item.Declaration.Parameters.Items[0].Name == identifier
