from dataclasses import FrozenInstanceError, replace

import pytest

from onec_runtime.bsl.full_ast_worker_projection import parse_full_ast_module


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
