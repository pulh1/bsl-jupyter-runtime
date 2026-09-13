"""Select the server-effective token view without rewriting Worker source."""

from __future__ import annotations

from dataclasses import dataclass

from onec_runtime.bsl.lexer import Token
from onec_runtime.bsl.parser_target import BslParseError
from onec_runtime.bsl.source_maps import SourceSpan


_TRUE_SYMBOLS = frozenset({"сервер", "server"})
_FALSE_SYMBOLS = frozenset(
    {
        "клиент",
        "client",
        "вебклиент",
        "webclient",
        "тонкийклиент",
        "thinclient",
        "толстыйклиентобычноеприложение",
        "thickclientordinaryapplication",
        "толстыйклиентуправляемоеприложение",
        "thickclientmanagedapplication",
        "внешнеесоединение",
        "externalconnection",
        "мобильныйклиент",
        "mobileclient",
        "мобильныйавтономныйсервер",
        "mobilestandaloneserver",
        "мобильноеприложениеклиент",
        "mobileapplicationclient",
        "мобильноеприложениесервер",
        "mobileapplicationserver",
        "сервермобильногоприложения",
        "mobileapplicationserver",
    }
)
_SERVICE_DIRECTIVES = frozenset(
    {"ОБЛАСТЬ", "КОНЕЦОБЛАСТИ", "ИСПОЛЬЗОВАТЬ", "NATIVE", "STACK"}
)


class WorkerPreprocessorError(BslParseError):
    pass


@dataclass(slots=True)
class _ConditionalFrame:
    opening: Token
    parent_active: bool
    any_taken: bool
    current_active: bool
    else_seen: bool = False


class _ExpressionParser:
    def __init__(self, tokens: tuple[Token, ...]) -> None:
        self._tokens = tokens
        self._index = 0

    def parse(self) -> bool:
        if not self._tokens:
            self._fail("empty preprocessor expression")
        value = self._parse_or()
        if self._index != len(self._tokens):
            self._fail("unexpected token in preprocessor expression")
        return value

    def _parse_or(self) -> bool:
        value = self._parse_and()
        while self._accept("ИЛИ"):
            operand = self._parse_and()
            value = value or operand
        return value

    def _parse_and(self) -> bool:
        value = self._parse_unary()
        while self._accept("И"):
            operand = self._parse_unary()
            value = value and operand
        return value

    def _parse_unary(self) -> bool:
        if self._accept("НЕ"):
            return not self._parse_unary()
        if self._accept("("):
            value = self._parse_or()
            if not self._accept(")"):
                self._fail("missing closing parenthesis")
            return value
        if self._index >= len(self._tokens) or self._tokens[self._index].type != "ID":
            self._fail("expected a preprocessor symbol")
        token = self._tokens[self._index]
        self._index += 1
        normalized = token.text.casefold()
        if normalized in _TRUE_SYMBOLS:
            return True
        if normalized in _FALSE_SYMBOLS:
            return False
        raise WorkerPreprocessorError(
            f"Unknown server preprocessor symbol {token.text!r} at {token.start}",
            span=SourceSpan(token.start, token.end),
            code="unknown_symbol",
        )

    def _accept(self, token_type: str) -> bool:
        if (
            self._index < len(self._tokens)
            and self._tokens[self._index].type == token_type
        ):
            self._index += 1
            return True
        return False

    def _fail(self, message: str) -> None:
        token = self._tokens[min(self._index, len(self._tokens) - 1)]
        raise WorkerPreprocessorError(
            f"{message} at {token.start}",
            span=SourceSpan(token.start, token.end),
            code="invalid_expression",
        )


def _directive_end(source: str, token: Token) -> int:
    newline = source.find("\n", token.end)
    return len(source) if newline < 0 else newline + 1


def _error(token: Token, message: str, code: str) -> WorkerPreprocessorError:
    return WorkerPreprocessorError(
        f"{message} at {token.start}",
        span=SourceSpan(token.start, token.end),
        code=code,
    )


def _validate_service_directive(directive: tuple[Token, ...]) -> None:
    kind = directive[1].type
    valid = (
        kind == "ОБЛАСТЬ"
        and len(directive) == 3
        and directive[2].type == "ID"
    ) or (
        kind == "ИСПОЛЬЗОВАТЬ"
        and len(directive) == 3
        and directive[2].type in {"ID", "STRING"}
    ) or (
        kind in {"КОНЕЦОБЛАСТИ", "NATIVE", "STACK"}
        and len(directive) == 2
    )
    if not valid:
        raise _error(
            directive[-1],
            f"invalid #{directive[1].text} directive",
            "invalid_directive",
        )


def select_server_effective_tokens(
    source: str,
    tokens: tuple[Token, ...],
) -> tuple[Token, ...]:
    """Return original tokens belonging to the server-effective source branch."""
    selected: list[Token] = []
    stack: list[_ConditionalFrame] = []
    active = True
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.type != "#":
            if active:
                selected.append(token)
            index += 1
            continue

        line_start = max(
            source.rfind("\n", 0, token.start),
            source.rfind("\r", 0, token.start),
        ) + 1
        if source[line_start : token.start].strip():
            raise _error(token, "preprocessor directive must start its line", "invalid_directive")
        end = _directive_end(source, token)
        next_index = index + 1
        while next_index < len(tokens) and tokens[next_index].start < end:
            next_index += 1
        directive = tokens[index:next_index]
        index = next_index
        if len(directive) < 2:
            raise _error(token, "empty preprocessor directive", "invalid_directive")
        kind = directive[1].type

        if kind in _SERVICE_DIRECTIVES:
            _validate_service_directive(directive)
            continue
        if kind == "ЕСЛИ":
            if len(directive) < 4 or directive[-1].type != "ТОГДА":
                raise _error(token, "invalid #If directive", "invalid_directive")
            condition = _ExpressionParser(directive[2:-1]).parse()
            frame = _ConditionalFrame(token, active, condition, active and condition)
            stack.append(frame)
            active = frame.current_active
            continue
        if kind == "ИНАЧЕЕСЛИ":
            if not stack:
                raise _error(token, "#ElsIf without #If", "unexpected_directive")
            frame = stack[-1]
            if frame.else_seen:
                raise _error(token, "#ElsIf after #Else", "unexpected_directive")
            if len(directive) < 4 or directive[-1].type != "ТОГДА":
                raise _error(token, "invalid #ElsIf directive", "invalid_directive")
            condition = _ExpressionParser(directive[2:-1]).parse()
            frame.current_active = frame.parent_active and not frame.any_taken and condition
            frame.any_taken = frame.any_taken or condition
            active = frame.current_active
            continue
        if kind == "ИНАЧЕ":
            if not stack:
                raise _error(token, "#Else without #If", "unexpected_directive")
            frame = stack[-1]
            if frame.else_seen or len(directive) != 2:
                raise _error(token, "invalid or repeated #Else", "unexpected_directive")
            frame.else_seen = True
            frame.current_active = frame.parent_active and not frame.any_taken
            frame.any_taken = True
            active = frame.current_active
            continue
        if kind == "КОНЕЦЕСЛИ":
            if not stack:
                raise _error(token, "#EndIf without #If", "unexpected_directive")
            if len(directive) != 2:
                raise _error(token, "invalid #EndIf directive", "invalid_directive")
            frame = stack.pop()
            active = frame.parent_active
            continue
        raise _error(directive[1], "unsupported preprocessor directive", "invalid_directive")

    if stack:
        raise _error(
            stack[-1].opening,
            "unterminated #If directive",
            "unbalanced_directive",
        )
    return tuple(selected)


__all__ = ["WorkerPreprocessorError", "select_server_effective_tokens"]
