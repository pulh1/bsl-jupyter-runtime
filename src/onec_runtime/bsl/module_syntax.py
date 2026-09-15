"""Compact, immutable syntax facts shared by reload and capture enrichment."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
import re
from threading import Lock
from typing import Literal

from onec_runtime.bsl.source_maps import SourceSpan


@dataclass(frozen=True, slots=True, repr=False)
class ModuleIdentity:
    """Logical identity; no source paths, revisions, hashes or parser state.

    The caller supplies a stable namespace for its runtime session or configured
    project/source binding. Configuration callers supply metadata kind, object
    and property identities plus the extension discriminator. Worker callers
    supply the unit kind, casefolded logical name and ``Module`` property.
    Layout and filesystem normalization belong to the source resolver.
    """

    namespace: str
    source_kind: Literal["worker", "configuration"]
    module_kind: str
    object_id: str
    property_id: str
    extension: str | None = None

    def __post_init__(self) -> None:
        for value in (self.namespace, self.module_kind, self.object_id, self.property_id):
            if type(value) is not str or not value:
                raise ValueError("module identity fields must be non-empty strings")
        if self.source_kind not in ("worker", "configuration"):
            raise ValueError("source_kind must be worker or configuration")
        if self.extension is not None:
            if type(self.extension) is not str or not self.extension:
                raise ValueError("extension must be a non-empty string or None")
            if self.source_kind != "configuration":
                raise ValueError("extension is only valid for configuration modules")


@dataclass(frozen=True, slots=True)
class MethodSyntaxInfo:
    name: str
    span: SourceSpan
    parameters: tuple[str, ...]
    start_line: int
    end_line: int

    def __post_init__(self) -> None:
        if type(self.name) is not str or not self.name:
            raise ValueError("method name must be non-empty")
        if type(self.span) is not SourceSpan or self.span.start == self.span.end:
            raise ValueError("method span must be a non-empty SourceSpan")
        if type(self.parameters) is not tuple or any(
            type(name) is not str or not name for name in self.parameters
        ):
            raise TypeError("parameters must be a tuple of non-empty strings")
        if (
            type(self.start_line) is not int
            or type(self.end_line) is not int
            or self.start_line < 1
            or self.end_line < self.start_line
        ):
            raise ValueError("method lines must be a positive inclusive interval")


@dataclass(frozen=True, slots=True)
class ModuleSyntaxIndex:
    source_sha256: str
    parser_identity: tuple[str, str]
    methods: tuple[MethodSyntaxInfo, ...]

    def __post_init__(self) -> None:
        if type(self.source_sha256) is not str or re.fullmatch(
            r"[0-9a-f]{64}", self.source_sha256
        ) is None:
            raise ValueError("source_sha256 must be a lowercase hexadecimal SHA-256")
        if (
            type(self.parser_identity) is not tuple
            or len(self.parser_identity) != 2
            or any(
                type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None
                for value in self.parser_identity
            )
        ):
            raise ValueError("parser_identity must contain two SHA-256 values")
        if type(self.methods) is not tuple or any(
            type(method) is not MethodSyntaxInfo for method in self.methods
        ):
            raise TypeError("methods must be a tuple of MethodSyntaxInfo")
        for previous, current in zip(self.methods, self.methods[1:]):
            if (
                current.span.start < previous.span.end
                or current.start_line < previous.end_line
            ):
                raise ValueError("methods must be ordered and non-overlapping")

    def method_at_line(self, line: int) -> MethodSyntaxInfo | None:
        """Find a method at a one-based source line, including decorations/end."""
        if type(line) is not int or line < 1:
            raise ValueError("line must be a positive integer")
        position = bisect_right(self.methods, line, key=lambda method: method.start_line) - 1
        if position >= 0 and line <= self.methods[position].end_line:
            return self.methods[position]
        return None


class ModuleSyntaxRegistry:
    """Versioned facts, independent of which source generation is active.

    Publishing permits capture to reuse a candidate parse. It does not activate
    that source; RuntimeApi binds versions to confirmed Worker generations.
    Existing versions live for the registry's lifetime and are never retargeted.
    """

    def __init__(self) -> None:
        self._entries: dict[
            tuple[ModuleIdentity, str, tuple[str, str]], ModuleSyntaxIndex
        ] = {}
        self._lock = Lock()

    def get(
        self, module: ModuleIdentity, source_sha256: str, parser_identity: tuple[str, str],
    ) -> ModuleSyntaxIndex | None:
        with self._lock:
            return self._entries.get((module, source_sha256, parser_identity))

    def publish(self, module: ModuleIdentity, index: ModuleSyntaxIndex) -> None:
        if type(module) is not ModuleIdentity or type(index) is not ModuleSyntaxIndex:
            raise TypeError("publication requires ModuleIdentity and ModuleSyntaxIndex")
        key = (module, index.source_sha256, index.parser_identity)
        with self._lock:
            current = self._entries.get(key)
            if current is not None and current != index:
                raise ValueError("conflicting syntax facts for an existing source version")
            self._entries.setdefault(key, index)


__all__ = ["MethodSyntaxInfo", "ModuleIdentity", "ModuleSyntaxIndex", "ModuleSyntaxRegistry"]
