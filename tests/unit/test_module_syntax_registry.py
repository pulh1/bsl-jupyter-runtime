from dataclasses import FrozenInstanceError, replace

import pytest

from onec_runtime.bsl.full_ast_worker_projection import parse_full_ast_module


@pytest.mark.parametrize(
    ("source", "expected_intervals", "ambiguous_line", "neighbor_names"),
    [
        (
            "Procedure P(PArg)\n"
            "X = 1; EndProcedure Procedure Q(QArg)\n"
            "EndProcedure",
            [("P", 1, 2), ("Q", 2, 3)],
            2,
            ("P", "Q"),
        ),
        (
            "Procedure P(PArg) EndProcedure Procedure Q(QArg) EndProcedure",
            [("P", 1, 1), ("Q", 1, 1)],
            1,
            (None, None),
        ),
        (
            "Procedure P(PArg)\n"
            "X = 1; EndProcedure Procedure Q(QArg) EndProcedure Procedure R(RArg)\n"
            "EndProcedure",
            [("P", 1, 2), ("Q", 2, 2), ("R", 2, 3)],
            2,
            ("P", "R"),
        ),
    ],
)
def test_method_lookup_does_not_claim_a_signature_for_an_ambiguous_line(
    source, expected_intervals, ambiguous_line, neighbor_names,
) -> None:
    """A physical line without a column must not select one of several methods."""
    index = parse_full_ast_module(source).syntax_index
    assert [(method.name, method.start_line, method.end_line)
            for method in index.methods] == expected_intervals
    assert index.method_at_line(ambiguous_line) is None
    before = index.method_at_line(ambiguous_line - 1) if ambiguous_line > 1 else None
    after = index.method_at_line(ambiguous_line + 1)
    assert (None if before is None else before.name,
            None if after is None else after.name) == neighbor_names


def _syntax_api():
    from onec_runtime.bsl import module_syntax

    return module_syntax


def test_registry_keeps_exact_module_hash_and_both_parser_identities() -> None:
    """A new candidate or another root/layer must not overwrite a pinned version."""
    syntax = _syntax_api()
    registry = syntax.ModuleSyntaxRegistry()
    module = syntax.ModuleIdentity("project-binding", "configuration", "CommonModule", "A", "Module")
    first = parse_full_ast_module("Процедура P()\nКонецПроцедуры").syntax_index
    second = parse_full_ast_module("Процедура Q()\nКонецПроцедуры").syntax_index
    registry.publish(module, first)
    registry.publish(module, second)
    for field, value in (("namespace", "other-binding"), ("source_kind", "worker"),
                         ("module_kind", "Document"), ("object_id", "B"),
                         ("property_id", "ObjectModule"), ("extension", "Ext")):
        other = replace(module, **{field: value})
        assert registry.get(other, first.source_sha256, first.parser_identity) is None
    assert registry.get(module, first.source_sha256, first.parser_identity) is first
    assert registry.get(module, second.source_sha256, second.parser_identity) is second
    for identity in (("a" * 64, first.parser_identity[1]),
                     (first.parser_identity[0], "b" * 64)):
        variant = replace(first, parser_identity=identity)
        assert registry.get(module, first.source_sha256, identity) is None
        registry.publish(module, variant)
        assert registry.get(module, first.source_sha256, identity) is variant
    assert registry.get(module, first.source_sha256, first.parser_identity) is first


def test_registry_lru_bounds_unique_versions_and_refreshes_recent_lookup() -> None:
    syntax = _syntax_api()
    registry = syntax.ModuleSyntaxRegistry(capacity=3)
    module = syntax.ModuleIdentity(
        "project-binding", "configuration", "CommonModule", "A", "Module"
    )
    indexes = tuple(
        parse_full_ast_module(
            f"Процедура P{revision}()\nКонецПроцедуры"
        ).syntax_index
        for revision in range(5)
    )

    for index in indexes[:3]:
        registry.publish(module, index)
    assert registry.get(
        module, indexes[0].source_sha256, indexes[0].parser_identity
    ) is indexes[0]

    registry.publish(module, indexes[3])
    assert registry.get(
        module, indexes[1].source_sha256, indexes[1].parser_identity
    ) is None
    assert registry.get(
        module, indexes[0].source_sha256, indexes[0].parser_identity
    ) is indexes[0]

    registry.publish(module, indexes[4])
    assert len(registry._entries) == 3
    assert registry.get(
        module, indexes[2].source_sha256, indexes[2].parser_identity
    ) is None
    assert registry.get(
        module, indexes[0].source_sha256, indexes[0].parser_identity
    ) is indexes[0]


def test_publication_is_immutable_and_rejects_conflicting_source_facts() -> None:
    """A same-key overwrite must not change an already published stack's facts."""
    syntax = _syntax_api()
    registry = syntax.ModuleSyntaxRegistry()
    module = syntax.ModuleIdentity("project-binding", "configuration", "CommonModule", "A", "Module")
    index = parse_full_ast_module("Процедура P()\nКонецПроцедуры").syntax_index
    registry.publish(module, index)
    registry.publish(module, replace(index))
    with pytest.raises(FrozenInstanceError):
        index.methods[0].name = "Q"
    with pytest.raises(TypeError, match="tuple"):
        replace(index, methods=list(index.methods))
    with pytest.raises(TypeError, match="tuple"):
        replace(index.methods[0], parameters=["X"])
    with pytest.raises(ValueError, match="conflict"):
        registry.publish(module, replace(index, methods=()))
    assert registry.get(module, index.source_sha256, index.parser_identity) is index


def test_index_rejects_unordered_overlapping_methods_and_mutable_provenance() -> None:
    _syntax_api()
    index = parse_full_ast_module(
        "Процедура P()\nКонецПроцедуры\nПроцедура Q()\nКонецПроцедуры"
    ).syntax_index
    with pytest.raises(ValueError, match="order|overlap"):
        replace(index, methods=tuple(reversed(index.methods)))
    with pytest.raises(ValueError, match="order|overlap"):
        replace(index, methods=(index.methods[0], index.methods[0]))
    with pytest.raises(ValueError, match="parser_identity"):
        replace(index, parser_identity=list(index.parser_identity))
