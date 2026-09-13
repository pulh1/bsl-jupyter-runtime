"""Catalog-independent public model produced by the Worker projection parser."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntFlag
import re

from onec_runtime.bsl.source_maps import SourceSpan


class BareNameKind(IntFlag):
    READ = 1
    BARE_WRITE = 2


def _require_name(value: str, field: str) -> None:
    if type(value) is not str or not value:
        raise ValueError(f"{field} must be a non-empty string")


def _require_normalized(value: str, field: str) -> None:
    _require_name(value, field)
    if value != value.casefold():
        raise ValueError(f"{field} must be casefold-normalized")


def _require_sorted_names(values: tuple[str, ...], field: str) -> None:
    if type(values) is not tuple:
        raise TypeError(f"{field} must be a tuple")
    for value in values:
        _require_normalized(value, field)
    if values != tuple(sorted(set(values))):
        raise ValueError(f"{field} must contain unique sorted names")


@dataclass(frozen=True, slots=True)
class BareName:
    name: str
    normalized_name: str
    kinds: BareNameKind

    def __post_init__(self) -> None:
        _require_name(self.name, "name")
        _require_normalized(self.normalized_name, "normalized_name")
        if self.normalized_name != self.name.casefold():
            raise ValueError("normalized_name must match name")
        if not isinstance(self.kinds, BareNameKind) or not self.kinds:
            raise ValueError("kinds must contain at least one BareNameKind")
        known_kinds = BareNameKind.READ | BareNameKind.BARE_WRITE
        if int(self.kinds) & ~int(known_kinds):
            raise ValueError("kinds contains unknown BareNameKind bits")


@dataclass(frozen=True, slots=True)
class ParsedMethodModel:
    name: str
    normalized_name: str
    exported: bool
    declaration_span: SourceSpan
    alias_declaration_offset: int
    alias_initializer_offset: int
    declared_names: tuple[str, ...]
    bare_names: tuple[BareName, ...]

    def __post_init__(self) -> None:
        _require_name(self.name, "name")
        _require_normalized(self.normalized_name, "normalized_name")
        if self.normalized_name != self.name.casefold():
            raise ValueError("normalized_name must match name")
        if type(self.exported) is not bool:
            raise TypeError("exported must be bool")
        if not isinstance(self.declaration_span, SourceSpan):
            raise TypeError("declaration_span must be SourceSpan")
        for field, offset in (
            ("alias_declaration_offset", self.alias_declaration_offset),
            ("alias_initializer_offset", self.alias_initializer_offset),
        ):
            if type(offset) is not int or offset < 0:
                raise ValueError(f"{field} must be a non-negative integer")
        if self.alias_initializer_offset < self.alias_declaration_offset:
            raise ValueError("initializer cannot precede declarations")
        _require_sorted_names(self.declared_names, "declared_names")
        if type(self.bare_names) is not tuple or not all(
            isinstance(item, BareName) for item in self.bare_names
        ):
            raise TypeError("bare_names must be a tuple of BareName")
        normalized = tuple(item.normalized_name for item in self.bare_names)
        if len(normalized) != len(set(normalized)):
            raise ValueError("bare_names must be unique by normalized_name")


@dataclass(frozen=True, slots=True)
class ParsedModuleModel:
    source_sha256: str
    module_variables: tuple[str, ...]
    module_bare_names: tuple[BareName, ...]
    methods: tuple[ParsedMethodModel, ...]
    parser_identity: tuple[str, str] = field(compare=False, repr=False)

    def __post_init__(self) -> None:
        if (
            type(self.source_sha256) is not str
            or len(self.source_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.source_sha256)
        ):
            raise ValueError("source_sha256 must be a lowercase hexadecimal SHA-256")
        _require_sorted_names(self.module_variables, "module_variables")
        if type(self.module_bare_names) is not tuple or not all(
            isinstance(item, BareName) for item in self.module_bare_names
        ):
            raise TypeError("module_bare_names must be a tuple of BareName")
        normalized = tuple(item.normalized_name for item in self.module_bare_names)
        if len(normalized) != len(set(normalized)):
            raise ValueError("module_bare_names must be unique by normalized_name")
        if type(self.methods) is not tuple or not all(
            isinstance(item, ParsedMethodModel) for item in self.methods
        ):
            raise TypeError("methods must be a tuple of ParsedMethodModel")
        if (
            type(self.parser_identity) is not tuple
            or len(self.parser_identity) != 2
            or any(
                type(value) is not str
                or re.fullmatch(r"[0-9a-f]{64}", value) is None
                for value in self.parser_identity
            )
        ):
            raise ValueError("parser_identity must contain two SHA-256 values")


__all__ = [
    "BareName",
    "BareNameKind",
    "ParsedMethodModel",
    "ParsedModuleModel",
]
