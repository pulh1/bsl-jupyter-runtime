from __future__ import annotations

import pytest

from onec_runtime.bsl.lexer import tokenize
from onec_runtime.bsl.worker_preprocessor import (
    WorkerPreprocessorError,
    select_server_effective_tokens,
)


def _selected_texts(source: str) -> tuple[str, ...]:
    tokens = tuple(tokenize(source))
    selected = select_server_effective_tokens(source, tokens)
    assert all(any(token is original for original in tokens) for token in selected)
    return tuple(token.text for token in selected)


def test_selects_nested_server_branch_and_retains_decorations() -> None:
    source = (
        "#Область API\n"
        "#Если Клиент Тогда\n"
        "Клиентская = 1;\n"
        "#ИначеЕсли НЕ Клиент И (Сервер ИЛИ МобильноеПриложениеКлиент) Тогда\n"
        "#Если ВнешнееСоединение Тогда\n"
        "Внешняя = 2;\n"
        "#Иначе\n"
        "&НаСервере\n"
        "Процедура Серверная() Экспорт\n"
        "КонецПроцедуры\n"
        "#КонецЕсли\n"
        "#Иначе\n"
        "Другая = 3;\n"
        "#КонецЕсли\n"
        '#Использовать "ОбщаяБиблиотека"\n'
        "#КонецОбласти\n"
    )

    selected = _selected_texts(source)

    assert selected == (
        "&",
        "НаСервере",
        "Процедура",
        "Серверная",
        "(",
        ")",
        "Экспорт",
        "КонецПроцедуры",
    )


@pytest.mark.parametrize(
    ("source", "code"),
    (
        ("#Если НеизвестнаяСреда Тогда\nX = 1;\n#КонецЕсли\n", "unknown_symbol"),
        ("#Если Сервер Тогда\nX = 1;\n", "unbalanced_directive"),
        ("#Иначе\nX = 1;\n", "unexpected_directive"),
        ("#Если (Сервер ИЛИ Клиент Тогда\nX = 1;\n#КонецЕсли\n", "invalid_expression"),
    ),
)
def test_unknown_or_unbalanced_preprocessor_input_fails_closed(
    source: str,
    code: str,
) -> None:
    with pytest.raises(WorkerPreprocessorError) as caught:
        select_server_effective_tokens(source, tuple(tokenize(source)))

    assert caught.value.code == code


def test_filters_region_use_native_and_stack_service_directives() -> None:
    source = (
        "#Область A\n"
        "#Native\n"
        "#Stack\n"
        "#Использовать Lib\n"
        "X = 1;\n"
        "#КонецОбласти\n"
    )

    assert _selected_texts(source) == ("X", "=", "1", ";")


def test_unbalanced_directive_points_to_the_unmatched_opening() -> None:
    source = "#Область A\n\n  #Если Сервер Тогда\nX = 1;\n"
    opening = source.index("#Если")

    with pytest.raises(WorkerPreprocessorError) as caught:
        select_server_effective_tokens(source, tuple(tokenize(source)))

    assert caught.value.code == "unbalanced_directive"
    assert caught.value.span.start == opening


@pytest.mark.parametrize(
    "directive",
    (
        "#Область",
        "#Область A extra",
        "#Использовать",
        "#Использовать Lib extra",
        "#КонецОбласти extra",
        "#Native extra",
        "#Stack extra",
    ),
)
def test_malformed_service_directive_fails_closed(directive: str) -> None:
    source = f"{directive}\nX = 1;\n"

    with pytest.raises(WorkerPreprocessorError) as caught:
        select_server_effective_tokens(source, tuple(tokenize(source)))

    assert caught.value.code == "invalid_directive"
