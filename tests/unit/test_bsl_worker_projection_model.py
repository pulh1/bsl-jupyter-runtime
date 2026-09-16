from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from enum import Enum
import gc
from types import FunctionType, ModuleType

import pytest

from onec_runtime.bsl import (
    BareName,
    BareNameKind,
    ParsedMethodModel,
    ParsedModuleModel,
    SourceSpan,
)
from onec_runtime.bsl import generated_semantic_parser as generated
from onec_runtime.bsl.full_ast_worker_projection import (
    full_ast_parser_identity,
    parse_full_ast_module,
)
from onec_runtime.bsl.lexer import Token
from onec_runtime.bsl.parser_target import BslParseError
from onec_runtime.bsl.source_maps import source_sha256


def _reachable_values(root: object) -> tuple[object, ...]:
    atomic = (str, bytes, int, float, bool, type(None), Enum)
    boundary = (type, ModuleType, FunctionType)
    pending = [root]
    seen: set[int] = set()
    values: list[object] = []
    while pending:
        value = pending.pop()
        if isinstance(value, atomic + boundary):
            continue
        identity = id(value)
        if identity in seen:
            continue
        seen.add(identity)
        values.append(value)
        pending.extend(gc.get_referents(value))
    return tuple(values)


def test_builds_exact_immutable_catalog_independent_model() -> None:
    source = (
        "Перем Модульная;\n"
        "&НаСервере\n"
        "Асинх Функция Тест(Параметр) Экспорт\n"
        "    Перем Локальная;\n"
        "    Неявная = КадровыйУчет.Получить();\n"
        "    Объект.Свойство = Неявная;\n"
        "    Возврат кадровыйучет.Другое();\n"
        "КонецФункции"
    )

    model = parse_full_ast_module(source)

    assert model == ParsedModuleModel(
        source_sha256=source_sha256(source),
        module_variables=("модульная",),
        module_bare_names=(),
        methods=(
            ParsedMethodModel(
                name="Тест",
                normalized_name="тест",
                exported=True,
                declaration_span=SourceSpan(source.index("&"), len(source)),
                alias_declaration_offset=source.index("Перем Локальная"),
                alias_initializer_offset=source.index(";", source.index("    Перем"))
                + 1,
                declared_names=("локальная", "параметр"),
                bare_names=(
                    BareName(
                        "Неявная",
                        "неявная",
                        BareNameKind.READ | BareNameKind.BARE_WRITE,
                    ),
                    BareName("КадровыйУчет", "кадровыйучет", BareNameKind.READ),
                    BareName("Объект", "объект", BareNameKind.READ),
                ),
            ),
        ),
        parser_identity=model.parser_identity,
    )
    assert not hasattr(model, "__dict__")
    with pytest.raises(FrozenInstanceError):
        model.source_sha256 = "changed"  # type: ignore[misc]


def test_worker_model_retains_full_parser_provenance() -> None:
    model = parse_full_ast_module("Процедура P()\nКонецПроцедуры")

    assert model.parser_identity == full_ast_parser_identity()


def test_worker_model_rejects_syntax_for_a_different_source_or_parser() -> None:
    """A cached Worker model must not carry capture facts for another parse."""
    model = parse_full_ast_module("Процедура P()\nКонецПроцедуры")
    index = model.syntax_index
    for other in (replace(index, source_sha256="a" * 64),
                  replace(index, parser_identity=("a" * 64, "b" * 64))):
        with pytest.raises(ValueError, match="syntax"):
            replace(model, syntax_index=other)
    reachable = _reachable_values(index)
    assert not any(isinstance(value, Token) for value in reachable)
    assert not any(isinstance(value, tuple(generated.AST_CLASSES.values())) for value in reachable)


def test_normalizes_interleaved_declarations_from_comma_locals_and_both_loops() -> None:
    source = (
        "Перем Zed, альфа, ZED;\n"
        "Процедура P(Знач Beta, бета)\n"
        "    Перем Gamma, АЛЬФА;\n"
        "    Для Каждого Элемент Из Коллекция Цикл\n"
        "    КонецЦикла;\n"
        "    Для Счетчик = 1 По Предел Цикл\n"
        "    КонецЦикла;\n"
        "КонецПроцедуры"
    )

    model = parse_full_ast_module(source)

    assert model.module_variables == ("zed", "альфа")
    assert model.methods[0].declared_names == (
        "beta",
        "gamma",
        "альфа",
        "бета",
        "счетчик",
        "элемент",
    )


@pytest.mark.parametrize(
    ("statement", "expected"),
    (
        ("X = 1;", (("X", BareNameKind.BARE_WRITE),)),
        (
            "X = X + 1;",
            (("X", BareNameKind.READ | BareNameKind.BARE_WRITE),),
        ),
        (
            "X.Свойство = Y;",
            (("X", BareNameKind.READ), ("Y", BareNameKind.READ)),
        ),
        (
            "X[Индекс] = Y;",
            (
                ("X", BareNameKind.READ),
                ("Индекс", BareNameKind.READ),
                ("Y", BareNameKind.READ),
            ),
        ),
    ),
)
def test_classifies_assignment_roots_without_rewriting_member_or_index_targets(
    statement: str,
    expected: tuple[tuple[str, BareNameKind], ...],
) -> None:
    source = f"Процедура P()\n{statement}\nКонецПроцедуры"

    method = parse_full_ast_module(source).methods[0]

    assert tuple((item.name, item.kinds) for item in method.bare_names) == expected


def test_rejects_bare_statement_but_accepts_call_statement() -> None:
    with pytest.raises(BslParseError) as caught:
        parse_full_ast_module("Процедура P()\nX.Свойство;\nКонецПроцедуры")

    assert caught.value.code == "bare_access_chain_statement"
    assert parse_full_ast_module(
        "Процедура P()\nX.Свойство();\nКонецПроцедуры"
    ).methods[0].bare_names == (BareName("X", "x", BareNameKind.READ),)


@pytest.mark.parametrize(
    ("body", "declaration_fragment", "initializer_fragment"),
    (
        ("", "КонецПроцедуры", "КонецПроцедуры"),
        ("    X = 1;\n", "X = 1", "X = 1"),
        (
            "    Перем A;\n    Перем B, C;\n    X = 1;\n",
            "Перем A",
            ";\n    X = 1",
        ),
    ),
)
def test_derives_one_declaration_and_initializer_boundary(
    body: str,
    declaration_fragment: str,
    initializer_fragment: str,
) -> None:
    source = f"Процедура P()\n{body}КонецПроцедуры"

    method = parse_full_ast_module(source).methods[0]

    assert source[method.alias_declaration_offset :].startswith(declaration_fragment)
    if body.startswith("    Перем"):
        assert source[method.alias_initializer_offset - 1 :].startswith(
            initializer_fragment
        )
    else:
        assert source[method.alias_initializer_offset :].startswith(initializer_fragment)


def test_public_model_does_not_retain_tokens_raw_nodes_or_transient_targets() -> None:
    model = parse_full_ast_module(
        "Процедура P(A)\nПерем B;\nX = A.Y();\nКонецПроцедуры"
    )

    reachable = _reachable_values(model)
    generated_types = tuple(generated.AST_CLASSES.values())

    assert not any(isinstance(value, Token) for value in reachable)
    assert not any(isinstance(value, generated_types) for value in reachable)


def test_rejects_unknown_bare_name_kind_bits() -> None:
    with pytest.raises(ValueError, match="unknown"):
        BareName("X", "x", BareNameKind(4))


def test_rejects_duplicate_module_bare_names_by_normalized_name() -> None:
    duplicate = BareName("X", "x", BareNameKind.READ)

    with pytest.raises(ValueError, match="unique"):
        ParsedModuleModel(
            source_sha256="0" * 64,
            module_variables=(),
            module_bare_names=(duplicate, duplicate),
            methods=(),
            parser_identity=("1" * 64, "2" * 64),
        )
