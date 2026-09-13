from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from onec_runtime.bsl.parser_artifact_identity import (
    ParserArtifactManifest,
    verify_parser_artifact_manifest,
)
from onec_runtime.bsl.lexer import Token, tokenize
from onec_runtime.bsl.source_maps import SourceSpan


@dataclass(frozen=True, slots=True)
class ParseNode:
    production: str
    alternative: int
    children: tuple["ParseNode | Token", ...]


class BslParseError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        span: SourceSpan = SourceSpan(0, 0),
        code: str = "parse_error",
    ) -> None:
        super().__init__(message)
        self.span = span
        self.code = code


@dataclass(frozen=True, slots=True)
class GeneratedParserMetadata:
    """Provenance retained by the installed generated parser artifact."""

    grammar_sha256: str
    parsergen_package_sha256: str | None
    manifest: ParserArtifactManifest | None = None

    def __post_init__(self) -> None:
        if (
            self.manifest is not None
            and self.grammar_sha256 != self.manifest.identity_sha256
        ):
            raise ValueError("generated parser metadata identity is inconsistent")

    @property
    def parser_identity_sha256(self) -> str:
        """Return the complete artifact identity retained for cache admission."""
        return self.grammar_sha256


@dataclass(frozen=True, slots=True)
class DevelopmentParserDetails:
    """Parsergen-only details available from ``from_files()`` validation."""

    resolved: Any
    analysis: Any
    parser_ir: Any
    validation_warnings: tuple[dict[str, str], ...]


class PythonParserTarget:
    """Runs the committed semantic parser, or a development validation build."""

    ENTRYPOINTS = {
        "module": "Модуль",
        "notebook": "БлокНоутбука",
        "notebook_cell": "ЯчейкаНоутбука",
        "expression": "ОтдельноеВыражение",
        "statement": "ОтдельнаяИнструкция",
        "preprocessor": "ДирективаПрепроцессора",
    }

    def __init__(
        self,
        generated_parser_type: type[Any],
        generated_error: type[Exception],
        metadata: GeneratedParserMetadata,
        *,
        generated_source: str | None = None,
        development: DevelopmentParserDetails | None = None,
    ) -> None:
        self._generated_parser_type = generated_parser_type
        self.generated_parser = generated_parser_type()
        self.generated_error = generated_error
        self.metadata = metadata
        self.generated_source = generated_source
        self.development = development

    @property
    def grammar_sha256(self) -> str:
        return self.metadata.grammar_sha256

    @classmethod
    def from_generated(cls) -> "PythonParserTarget":
        """Load packaged code and allocate one mutable parser cursor."""
        from onec_runtime.bsl import generated_semantic_parser

        manifest = verify_parser_artifact_manifest(
            generated_semantic_parser.PARSER_ARTIFACT_MANIFEST_JSON
        )
        metadata = GeneratedParserMetadata(
            manifest.identity_sha256,
            manifest.parsergen_package_sha256,
            manifest,
        )
        return cls(
            generated_semantic_parser.GeneratedParser,
            generated_semantic_parser.GeneratedParseError,
            metadata,
        )

    @classmethod
    def from_files(
        cls,
        grammar_path: Path,
        parsergen_src: Path,
        *,
        lookahead: int = 1,
    ) -> "PythonParserTarget":
        """Build through the optional build-time development adapter."""
        from importlib import import_module

        adapter = import_module("onec_runtime_build.parser_target_development")
        return adapter.build_combined_python_parser_target(
            cls,
            grammar_path,
            parsergen_src,
            lookahead=lookahead,
        )

    def parse(self, source: str, entrypoint: str) -> None:
        self.parse_ast(source, entrypoint)

    def new_instance(self) -> "PythonParserTarget":
        """Reuse generated code while keeping parser cursor state controller-local."""
        return PythonParserTarget(
            self._generated_parser_type,
            self.generated_error,
            self.metadata,
            generated_source=self.generated_source,
            development=self.development,
        )

    def parse_ast(self, source: str, entrypoint: str) -> Any:
        return self.parse_tokens_ast(tokenize(source), entrypoint)

    def parse_tokens(self, tokens: Sequence[Token], entrypoint: str) -> None:
        self.parse_tokens_ast(tokens, entrypoint)

    def parse_tokens_ast(
        self,
        tokens: Sequence[Token],
        entrypoint: str,
    ) -> Any:
        entrypoint_name = self._entrypoint_name(entrypoint)
        try:
            return self.generated_parser.parse(tokens, entrypoint_name)
        except self.generated_error as error:
            actual = error.actual
            start = (
                tokens[error.position].start
                if error.position < len(tokens)
                else tokens[-1].end if tokens else 0
            )
            raise BslParseError(
                f"Unexpected {actual!r} at {start}; expected {error.expected!r}",
                span=SourceSpan(start, start),
                code="unexpected_token",
            ) from error

    def _entrypoint_name(self, entrypoint: str) -> str:
        entrypoint_name = next(
            (
                name
                for name, production in self.ENTRYPOINTS.items()
                if production == entrypoint
            ),
            None,
        )
        if entrypoint_name is None:
            raise ValueError(f"Unknown parser entrypoint production {entrypoint!r}")
        return entrypoint_name

    def parse_interpreted(self, source: str, entrypoint: str) -> ParseNode:
        return self.parse_tokens_interpreted(tokenize(source), entrypoint)

    def parse_tokens_interpreted(
        self,
        tokens: Sequence[Token],
        entrypoint: str,
    ) -> ParseNode:
        self._require_development()
        self.tokens = tokens
        self.position = 0
        node = self._production(entrypoint)
        if self.position != len(self.tokens):
            token = self.tokens[self.position]
            raise BslParseError(
                f"Unexpected {token.text!r} at {token.start}",
                span=SourceSpan(token.start, token.start),
                code="unexpected_token",
            )
        return node

    def recognize_tokens_interpreted(
        self,
        tokens: Sequence[Token],
        entrypoint: str,
    ) -> None:
        self._require_development()
        self.tokens = tokens
        self.position = 0
        self._recognize_production(entrypoint)
        if self.position != len(self.tokens):
            token = self.tokens[self.position]
            raise BslParseError(
                f"Unexpected {token.text!r} at {token.start}",
                span=SourceSpan(token.start, token.start),
                code="unexpected_token",
            )

    def _lookahead_matches(self, word: tuple[str, ...]) -> bool:
        actual = tuple(token.type for token in self.tokens[self.position : self.position + len(word)])
        if len(actual) < len(word):
            actual += ("$",) * (len(word) - len(actual))
        return actual == word

    def _production(self, name: str) -> ParseNode:
        development = self._require_development()
        alternatives = development.resolved.productions[name]
        matches = [
            (alternative_number, alternative)
            for alternative_number, alternative in enumerate(alternatives, start=1)
            if any(
                self._lookahead_matches(word)
                for word in development.analysis.select[(name, alternative_number)]
            )
        ]
        if len(matches) != 1:
            lookahead = [
                token.type
                for token in self.tokens[
                    self.position : self.position + development.analysis.k
                ]
            ]
            offset = self._current_offset()
            raise BslParseError(
                f"No unique alternative for {name} at {lookahead}",
                span=SourceSpan(offset, offset),
                code="ambiguous_alternative",
            )
        alternative_number, alternative = matches[0]
        children: list[ParseNode | Token] = []
        for symbol in alternative.symbols:
            if hasattr(symbol, "token_types"):
                if self.position >= len(self.tokens):
                    offset = self._current_offset()
                    raise BslParseError(
                        f"Unexpected end while parsing {name}",
                        span=SourceSpan(offset, offset),
                        code="unexpected_end",
                    )
                token = self.tokens[self.position]
                if token.type not in symbol.token_types:
                    raise BslParseError(
                        f"Expected {sorted(symbol.token_types)}, got {token.type} at {token.start}",
                        span=SourceSpan(token.start, token.start),
                        code="unexpected_token",
                    )
                self.position += 1
                children.append(token)
            else:
                children.append(self._production(symbol.name))
        return ParseNode(name, alternative_number, tuple(children))

    def _recognize_production(self, name: str) -> None:
        development = self._require_development()
        alternatives = development.resolved.productions[name]
        matches = [
            alternative
            for alternative_number, alternative in enumerate(alternatives, start=1)
            if any(
                self._lookahead_matches(word)
                for word in development.analysis.select[(name, alternative_number)]
            )
        ]
        if len(matches) != 1:
            lookahead = [
                token.type
                for token in self.tokens[
                    self.position : self.position + development.analysis.k
                ]
            ]
            offset = self._current_offset()
            raise BslParseError(
                f"No unique alternative for {name} at {lookahead}",
                span=SourceSpan(offset, offset),
                code="ambiguous_alternative",
            )
        for symbol in matches[0].symbols:
            if hasattr(symbol, "token_types"):
                if self.position >= len(self.tokens):
                    offset = self._current_offset()
                    raise BslParseError(
                        f"Unexpected end while parsing {name}",
                        span=SourceSpan(offset, offset),
                        code="unexpected_end",
                    )
                token = self.tokens[self.position]
                if token.type not in symbol.token_types:
                    raise BslParseError(
                        f"Expected {sorted(symbol.token_types)}, got "
                        f"{token.type} at {token.start}",
                        span=SourceSpan(token.start, token.start),
                        code="unexpected_token",
                    )
                self.position += 1
            else:
                self._recognize_production(symbol.name)

    @property
    def lookahead(self) -> int:
        return self._require_development().analysis.k

    @property
    def parser_ir(self) -> Any:
        return self._require_development().parser_ir

    @property
    def validation_warnings(self) -> tuple[dict[str, str], ...]:
        return self._require_development().validation_warnings

    def _current_offset(self) -> int:
        if self.position < len(self.tokens):
            return self.tokens[self.position].start
        return self.tokens[-1].end if self.tokens else 0

    def _require_development(self) -> DevelopmentParserDetails:
        if self.development is None:
            raise RuntimeError(
                "Interpreted parser details are available only from from_files()"
            )
        return self.development


def parse_raw_module(
    source: str,
    parser_target: PythonParserTarget,
) -> tuple[Any, tuple[Token, ...]]:
    """Parse a raw BSL module while retaining directive-covered source code.

    Each leading preprocessor line is validated by the grammar's directive
    entrypoint. Only those line tokens are omitted from the module token view;
    all returned tokens and AST spans keep their offsets in ``source``.
    """
    tokens = tuple(tokenize(source))
    module_tokens: list[Token] = []
    position = 0
    while position < len(tokens):
        token = tokens[position]
        line_start = source.rfind("\n", 0, token.start) + 1
        indentation = source[line_start : token.start]
        if token.type != "#" or not all(
            character.isspace() for character in indentation
        ):
            module_tokens.append(token)
            position += 1
            continue

        line_end = source.find("\n", token.end)
        if line_end < 0:
            line_end = len(source)
        directive_start = position
        while position < len(tokens) and tokens[position].start < line_end:
            position += 1
        parser_target.parse_tokens_ast(
            tokens[directive_start:position],
            "ДирективаПрепроцессора",
        )

    root = parser_target.parse_tokens_ast(tuple(module_tokens), "Модуль")
    return root, tokens
