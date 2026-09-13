from __future__ import annotations

from dataclasses import dataclass
import re

from onec_runtime.bsl.source_maps import SourceSpan


KEYWORDS = {
    "ПЕРЕМ": "ПЕРЕМ", "VAR": "ПЕРЕМ",
    "ЭКСПОРТ": "ЭКСПОРТ", "EXPORT": "ЭКСПОРТ",
    "АСИНХ": "АСИНХ", "ASYNC": "АСИНХ",
    "ПРОЦЕДУРА": "ПРОЦЕДУРА", "PROCEDURE": "ПРОЦЕДУРА",
    "КОНЕЦПРОЦЕДУРЫ": "КОНЕЦПРОЦЕДУРЫ", "ENDPROCEDURE": "КОНЕЦПРОЦЕДУРЫ",
    "ФУНКЦИЯ": "ФУНКЦИЯ", "FUNCTION": "ФУНКЦИЯ",
    "КОНЕЦФУНКЦИИ": "КОНЕЦФУНКЦИИ", "ENDFUNCTION": "КОНЕЦФУНКЦИИ",
    "ЗНАЧ": "ЗНАЧ", "VAL": "ЗНАЧ",
    "ЕСЛИ": "ЕСЛИ", "IF": "ЕСЛИ", "ТОГДА": "ТОГДА", "THEN": "ТОГДА",
    "ИНАЧЕЕСЛИ": "ИНАЧЕЕСЛИ", "ELSIF": "ИНАЧЕЕСЛИ",
    "ИНАЧЕ": "ИНАЧЕ", "ELSE": "ИНАЧЕ", "КОНЕЦЕСЛИ": "КОНЕЦЕСЛИ", "ENDIF": "КОНЕЦЕСЛИ",
    "ПОКА": "ПОКА", "WHILE": "ПОКА", "ЦИКЛ": "ЦИКЛ", "DO": "ЦИКЛ",
    "КОНЕЦЦИКЛА": "КОНЕЦЦИКЛА", "ENDDO": "КОНЕЦЦИКЛА",
    "ДЛЯ": "ДЛЯ", "FOR": "ДЛЯ", "КАЖДОГО": "КАЖДОГО", "EACH": "КАЖДОГО",
    "ИЗ": "ИЗ", "IN": "ИЗ", "ПО": "ПО", "TO": "ПО",
    "ПОПЫТКА": "ПОПЫТКА", "TRY": "ПОПЫТКА", "ИСКЛЮЧЕНИЕ": "ИСКЛЮЧЕНИЕ", "EXCEPT": "ИСКЛЮЧЕНИЕ",
    "КОНЕЦПОПЫТКИ": "КОНЕЦПОПЫТКИ", "ENDTRY": "КОНЕЦПОПЫТКИ",
    "ВОЗВРАТ": "ВОЗВРАТ", "RETURN": "ВОЗВРАТ", "ПРОДОЛЖИТЬ": "ПРОДОЛЖИТЬ", "CONTINUE": "ПРОДОЛЖИТЬ",
    "ПРЕРВАТЬ": "ПРЕРВАТЬ", "BREAK": "ПРЕРВАТЬ", "ВЫЗВАТЬИСКЛЮЧЕНИЕ": "ВЫЗВАТЬИСКЛЮЧЕНИЕ", "RAISE": "ВЫЗВАТЬИСКЛЮЧЕНИЕ",
    "ВЫПОЛНИТЬ": "ВЫПОЛНИТЬ", "EXECUTE": "ВЫПОЛНИТЬ", "ПЕРЕЙТИ": "ПЕРЕЙТИ", "GOTO": "ПЕРЕЙТИ",
    "ДОБАВИТЬОБРАБОТЧИК": "ДОБАВИТЬОБРАБОТЧИК", "ADDHANDLER": "ДОБАВИТЬОБРАБОТЧИК",
    "УДАЛИТЬОБРАБОТЧИК": "УДАЛИТЬОБРАБОТЧИК", "REMOVEHANDLER": "УДАЛИТЬОБРАБОТЧИК",
    "ЖДАТЬ": "ЖДАТЬ", "AWAIT": "ЖДАТЬ", "И": "И", "AND": "И", "ИЛИ": "ИЛИ", "OR": "ИЛИ", "НЕ": "НЕ", "NOT": "НЕ",
    "НОВЫЙ": "НОВЫЙ", "NEW": "НОВЫЙ", "ИСТИНА": "ИСТИНА", "TRUE": "ИСТИНА", "ЛОЖЬ": "ЛОЖЬ", "FALSE": "ЛОЖЬ",
    "НЕОПРЕДЕЛЕНО": "НЕОПРЕДЕЛЕНО", "UNDEFINED": "НЕОПРЕДЕЛЕНО", "NULL": "NULL",
    "ОБЛАСТЬ": "ОБЛАСТЬ", "REGION": "ОБЛАСТЬ",
    "КОНЕЦОБЛАСТИ": "КОНЕЦОБЛАСТИ", "ENDREGION": "КОНЕЦОБЛАСТИ",
    "ИСПОЛЬЗОВАТЬ": "ИСПОЛЬЗОВАТЬ", "USE": "ИСПОЛЬЗОВАТЬ",
    "NATIVE": "NATIVE", "STACK": "STACK",
}

_PREPROCESSOR_ONLY_KEYWORDS = frozenset(
    {
        "ОБЛАСТЬ",
        "REGION",
        "КОНЕЦОБЛАСТИ",
        "ENDREGION",
        "ИСПОЛЬЗОВАТЬ",
        "USE",
        "NATIVE",
        "STACK",
    }
)

_ID = re.compile(r"[A-Za-zА-Яа-яЁё_][0-9A-Za-zА-Яа-яЁё_]*")
_NUMBER = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")


@dataclass(frozen=True, slots=True)
class Token:
    type: str
    text: str
    start: int
    end: int


class BslLexError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        span: SourceSpan = SourceSpan(0, 0),
        code: str = "lexical_error",
    ) -> None:
        super().__init__(message)
        self.span = span
        self.code = code


def tokenize(source: str) -> list[Token]:
    result: list[Token] = []
    position = 0
    force_identifier = False
    while position < len(source):
        if source[position].isspace():
            position += 1
            continue
        if source.startswith("//", position):
            newline = source.find("\n", position)
            position = len(source) if newline < 0 else newline + 1
            continue
        start = position
        character = source[position]
        if character == '"':
            position += 1
            while position < len(source):
                if source[position] == '"':
                    if position + 1 < len(source) and source[position + 1] == '"':
                        position += 2
                        continue
                    position += 1
                    break
                position += 1
            else:
                raise BslLexError(
                    f"Unterminated string at {start}",
                    span=SourceSpan(start, len(source)),
                    code="unterminated_string",
                )
            result.append(Token("STRING", source[start:position], start, position))
            force_identifier = False
            continue
        if character == "'":
            position = source.find("'", position + 1)
            if position < 0:
                raise BslLexError(
                    f"Unterminated date literal at {start}",
                    span=SourceSpan(start, len(source)),
                    code="unterminated_date_literal",
                )
            position += 1
            result.append(Token("DATETIME", source[start:position], start, position))
            force_identifier = False
            continue
        match = _NUMBER.match(source, position)
        if match:
            position = match.end()
            result.append(Token("NUMBER", match.group(), start, position))
            force_identifier = False
            continue
        match = _ID.match(source, position)
        if match:
            position = match.end()
            text = match.group()
            keyword = text.upper()
            preprocessor_only = keyword in _PREPROCESSOR_ONLY_KEYWORDS
            follows_hash = bool(result and result[-1].type == "#")
            token_type = (
                "ID"
                if force_identifier or (preprocessor_only and not follows_hash)
                else KEYWORDS.get(keyword, "ID")
            )
            result.append(Token(token_type, text, start, position))
            force_identifier = False
            continue
        operator = next(
            (value for value in ("<=", ">=", "<>") if source.startswith(value, position)),
            None,
        )
        if operator:
            position += len(operator)
            result.append(Token(operator, operator, start, position))
            force_identifier = False
            continue
        if character in ";,().[]+-*/%=<>?:~#&":
            position += 1
            result.append(Token(character, character, start, position))
            force_identifier = character in ".&"
            continue
        raise BslLexError(
            f"Unexpected character {character!r} at {position}",
            span=SourceSpan(position, position + 1),
            code="unexpected_character",
        )
    return result
