from __future__ import annotations

import cProfile
import pstats
from dataclasses import fields, is_dataclass, make_dataclass
from enum import Enum
from types import FunctionType, ModuleType

import pytest

from onec_runtime.bsl import generated_semantic_parser as generated
from onec_runtime.bsl.full_ast_worker_projection import (
    full_ast_parser_identity,
    parse_full_ast_module,
    project_full_ast_module,
)
from onec_runtime.bsl.lexer import Token, tokenize
from onec_runtime.bsl.parser_target import BslParseError, PythonParserTarget
from onec_runtime.bsl.source_maps import SourceSpan, source_sha256
from onec_runtime.bsl.worker_preprocessor import select_server_effective_tokens
from onec_runtime.bsl.worker_projection_model import (
    BareName,
    BareNameKind,
    ParsedMethodModel,
    ParsedModuleModel,
)
from onec_runtime.performance_profile import PhaseRecorder


def test_neutral_full_ast_module_api_preserves_extracted_model() -> None:
    """Break caught: runtime cutover must retain the proven extractor contract."""
    source = "Процедура P()\nX = Y;\nКонецПроцедуры"

    assert parse_full_ast_module(source).methods[0].bare_names == (
        BareName("X", "x", BareNameKind.BARE_WRITE),
        BareName("Y", "y", BareNameKind.READ),
    )


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
        if is_dataclass(value) and not isinstance(value, type):
            pending.extend(getattr(value, field.name) for field in fields(value))
        elif isinstance(value, (tuple, list)):
            pending.extend(value)
    return tuple(values)


def test_full_ast_extractor_builds_existing_catalog_independent_model() -> None:
    source = (
        "Перем Модульная Экспорт;\n"
        "МодульнаяЗапись = МодульноеЧтение;\n"
        "&НаСервере\n"
        "Асинх Функция Проверка(Знач Параметр) Экспорт\n"
        "    Перем Локальная;\n"
        "    Для Каждого Элемент Из КадровыйУчет.Получить() Цикл\n"
        "        Локальная = Элемент;\n"
        "    КонецЦикла;\n"
        "    Для Счетчик = 1 По Предел Цикл\n"
        "    КонецЦикла;\n"
        "    Возврат Локальная;\n"
        "КонецФункции\n\n"
        "Процедура Пустая()\n"
        "КонецПроцедуры"
    )

    model = parse_full_ast_module(source)

    method_start = source.index("&НаСервере")
    method_end = source.index("КонецФункции") + len("КонецФункции")
    local_start = source.index("Перем Локальная")
    assert model == ParsedModuleModel(
        source_sha256=source_sha256(source),
        module_variables=("модульная",),
        module_bare_names=(
            BareName("МодульнаяЗапись", "модульнаязапись", BareNameKind.BARE_WRITE),
            BareName("МодульноеЧтение", "модульноечтение", BareNameKind.READ),
        ),
        methods=(
            ParsedMethodModel(
                name="Проверка",
                normalized_name="проверка",
                exported=True,
                declaration_span=SourceSpan(method_start, method_end),
                alias_declaration_offset=local_start,
                alias_initializer_offset=source.index(";", local_start) + 1,
                declared_names=(
                    "локальная",
                    "параметр",
                    "счетчик",
                    "элемент",
                ),
                bare_names=(
                    BareName("КадровыйУчет", "кадровыйучет", BareNameKind.READ),
                    BareName(
                        "Локальная",
                        "локальная",
                        BareNameKind.READ | BareNameKind.BARE_WRITE,
                    ),
                    BareName("Элемент", "элемент", BareNameKind.READ),
                    BareName("Предел", "предел", BareNameKind.READ),
                ),
            ),
            ParsedMethodModel(
                name="Пустая",
                normalized_name="пустая",
                exported=False,
                declaration_span=SourceSpan(
                    source.index("Процедура Пустая"), len(source)
                ),
                alias_declaration_offset=source.index("КонецПроцедуры"),
                alias_initializer_offset=source.index("КонецПроцедуры"),
                declared_names=(),
                bare_names=(),
            ),
        ),
        parser_identity=model.parser_identity,
    )


@pytest.mark.parametrize(
    ("statement", "expected"),
    (
        ("Имя = 1;", (("Имя", BareNameKind.BARE_WRITE),)),
        (
            "Имя = Имя + 1;",
            (("Имя", BareNameKind.READ | BareNameKind.BARE_WRITE),),
        ),
        (
            "Имя.Свойство = Значение;",
            (("Имя", BareNameKind.READ), ("Значение", BareNameKind.READ)),
        ),
        (
            "Имя[Индекс] = Значение;",
            (
                ("Имя", BareNameKind.READ),
                ("Индекс", BareNameKind.READ),
                ("Значение", BareNameKind.READ),
            ),
        ),
        (
            "Имя().Свойство = Значение;",
            (("Имя", BareNameKind.READ), ("Значение", BareNameKind.READ)),
        ),
    ),
)
def test_full_ast_classifies_assignment_roots(
    statement: str,
    expected: tuple[tuple[str, BareNameKind], ...],
) -> None:
    source = f"Процедура P()\n{statement}\nКонецПроцедуры"

    full = parse_full_ast_module(source)

    assert tuple(
        (bare.name, bare.kinds) for bare in full.methods[0].bare_names
    ) == expected


def test_full_ast_rejects_bare_access_chain() -> None:
    source = "Процедура P()\r\nИмя.Свойство;\r\nКонецПроцедуры"
    with pytest.raises(BslParseError) as caught:
        parse_full_ast_module(source)

    assert caught.value.span == SourceSpan(
        source.index("Имя.Свойство"),
        source.index("Имя.Свойство") + len("Имя.Свойство"),
    )


def test_call_inside_index_does_not_turn_bare_target_into_call_statement() -> None:
    source = "Процедура P()\nX[Y()];\nКонецПроцедуры"

    with pytest.raises(BslParseError) as caught:
        parse_full_ast_module(source)
    assert caught.value.code == "bare_access_chain_statement"
    assert caught.value.span == SourceSpan(
        source.index("X[Y()]"),
        source.index(";"),
    )


@pytest.mark.parametrize(
    "source",
    (
        (
            "Перем Имя, ИМЯ;\r\n"
            "#Если Сервер Тогда\r\n"
            "СерверноеИмя = 1;\r\n"
            "#Иначе\r\n"
            "КлиентскоеИмя = 1;\r\n"
            "#КонецЕсли\r\n"
            "Процедура P(Параметр, ПАРАМЕТР)\r\n"
            "Перем Локальная, ЛОКАЛЬНАЯ;\r\n"
            "Результат = СерверноеИмя;\r\n"
            "КонецПроцедуры"
        ),
        (
            "Процедура P()\n"
            "Результат = A.B[Индекс].C(Вход);\n"
            "Выполнить(\"СкрытоеИмя.Метод()\");\n"
            "КонецПроцедуры"
        ),
        (
            "Процедура P(A)\n"
            "Если A Тогда\n"
            "    Пока B Цикл\n"
            "        X = C.D();\n"
            "    КонецЦикла;\n"
            "Иначе\n"
            "    Попытка\n"
            "        Y[Z] = Q;\n"
            "    Исключение\n"
            "        R = S;\n"
            "    КонецПопытки;\n"
            "КонецЕсли;\n"
            "КонецПроцедуры"
        ),
    ),
)
def test_full_ast_model_preserves_directives_crlf_and_collisions(
    source: str,
) -> None:
    model = parse_full_ast_module(source)
    assert model.source_sha256 == source_sha256(source)
    assert all(method.normalized_name == method.name.casefold() for method in model.methods)


def test_module_scope_loop_variables_match_worker_hard_locals() -> None:
    source = (
        "Для Каждого Элемент Из Коллекция Цикл\n"
        "КонецЦикла;\n"
        "Для Счетчик = 1 По Предел Цикл\n"
        "КонецЦикла;"
    )

    full = parse_full_ast_module(source)

    assert full.module_variables == ("счетчик", "элемент")


def test_server_preprocessor_keeps_original_method_and_body_offsets() -> None:
    source = (
        "#Если Сервер Тогда\r\n"
        "&НаСервере\r\n"
        "Процедура Серверная()\r\n"
        "    X = Y;\r\n"
        "КонецПроцедуры\r\n"
        "#Иначе\r\n"
        "Процедура Клиентская()\r\n"
        "КонецПроцедуры\r\n"
        "#КонецЕсли"
    )

    model = parse_full_ast_module(source)

    assert tuple(method.name for method in model.methods) == ("Серверная",)
    method = model.methods[0]
    assert method.declaration_span == SourceSpan(
        source.index("&НаСервере"),
        source.index("КонецПроцедуры") + len("КонецПроцедуры"),
    )
    assert method.alias_declaration_offset == source.index("X = Y")
    assert method.alias_initializer_offset == source.index("X = Y")


def test_facade_tokenizes_and_parses_exactly_once_and_profiles_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from onec_runtime.bsl import full_ast_worker_projection

    source = "Процедура P()\nX = Y;\nКонецПроцедуры"
    tokenize_calls = 0
    parse_calls = 0
    real_tokenize = full_ast_worker_projection.tokenize
    real_parse = generated.GeneratedParser.parse

    def counting_tokenize(value: str) -> list[Token]:
        nonlocal tokenize_calls
        tokenize_calls += 1
        return real_tokenize(value)

    def counting_parse(
        self: generated.GeneratedParser,
        tokens: tuple[Token, ...],
        entrypoint: str,
    ) -> object:
        nonlocal parse_calls
        parse_calls += 1
        return real_parse(self, tokens, entrypoint)

    monkeypatch.setattr(full_ast_worker_projection, "tokenize", counting_tokenize)
    monkeypatch.setattr(generated.GeneratedParser, "parse", counting_parse)
    profiler = PhaseRecorder()

    model = parse_full_ast_module(source, profiler=profiler)

    assert model.methods[0].bare_names == (
        BareName("X", "x", BareNameKind.BARE_WRITE),
        BareName("Y", "y", BareNameKind.READ),
    )
    assert tokenize_calls == 1
    assert parse_calls == 1
    assert profiler.parser_calls.full_module_parses == 1
    assert [event.phase for event in profiler.events] == [
        "semantic_parse",
        "ast_model_extract",
    ]


def test_typed_scope_dispatch_classifies_every_generated_ast_field() -> None:
    from onec_runtime.bsl import full_ast_worker_projection

    full_ast_worker_projection._validate_node_field_schema(generated.AST_CLASSES)


def test_typed_scope_dispatch_rejects_new_field_on_existing_node() -> None:
    from onec_runtime.bsl import full_ast_worker_projection

    changed_return = make_dataclass(
        "ReturnStatement",
        (("Value", object), ("Diagnostics", object), ("span", SourceSpan)),
        frozen=True,
        slots=True,
    )
    changed_classes = dict(generated.AST_CLASSES)
    changed_classes["ReturnStatement"] = changed_return

    with pytest.raises(ValueError, match="ReturnStatement.*Diagnostics"):
        full_ast_worker_projection._validate_node_field_schema(changed_classes)


def test_scope_extractor_does_not_dispatch_expression_wrappers_as_calls() -> None:
    source = (
        "Процедура P()\n"
        + "".join(
            f"Результат{index} = A + B * C - D / E;\n" for index in range(50)
        )
        + "КонецПроцедуры"
    )
    tokens = tuple(tokenize(source))
    effective = select_server_effective_tokens(source, tokens)
    root = PythonParserTarget.from_generated().parse_tokens_ast(effective, "Модуль")
    profile = cProfile.Profile()

    profile.runcall(
        project_full_ast_module,
        source,
        root,
        effective,
        parser_identity=full_ast_parser_identity(),
    )

    traversal_calls = sum(
        total_calls
        for (filename, _line, function_name), (_primitive, total_calls, *_rest) in (
            pstats.Stats(profile).stats.items()
        )
        if filename.endswith("full_ast_worker_projection.py")
        and function_name in {"_collect_scope_facts", "_walk", "_push_children"}
    )
    assert traversal_calls == 2


def test_full_ast_generated_error_has_product_contract() -> None:
    source = "Процедура P( КонецПроцедуры"
    with pytest.raises(BslParseError) as caught:
        parse_full_ast_module(source)

    assert caught.value.code == "unexpected_token"


def test_projector_handles_5000_linked_statements_without_retaining_ast() -> None:
    span = SourceSpan(0, 0)
    code: object = generated.CodeBlock(None, None, span)
    for _ in range(5_000):
        code = generated.CodeBlock(generated.ContinueStatement(span), code, span)
    body = generated.MethodBody((), code, span)
    declaration = generated.ProcedureDeclaration("P", None, None, body, span)
    method = generated.Method((), None, declaration, span)
    elements = generated.ModuleElements(
        method,
        generated.ModuleElements(None, None, span),
        span,
    )
    root = generated.Module((), elements, span)

    model = project_full_ast_module(
        "",
        root,
        (),
        parser_identity=full_ast_parser_identity(),
    )
    reachable = _reachable_values(model)
    generated_types = tuple(generated.AST_CLASSES.values())

    assert model.methods[0].name == "P"
    assert not any(isinstance(value, Token) for value in reachable)
    assert not any(isinstance(value, generated_types) for value in reachable)
