"""Kernel completions for fields of existing BSL context values."""
from __future__ import annotations

from IPython.core.completer import (
    CompletionContext,
    SimpleCompletion,
    SimpleMatcherResult,
    context_matcher,
)

from onec_runtime.bsl.lexer import BslLexError, tokenize


_MATCHER_ATTRIBUTE = "_onec_runtime_completion_matcher"
_MAX_CELL_BYTES = 64 * 1024
_PYTHON_MATCHERS = {
    "IPCompleter.python_matcher",
    "IPCompleter.jedi_matcher",
    "IPCompleter.dict_key_matcher",
    "IPCompleter.python_func_kw_matcher",
    "IPCompleter.file_matcher",
}


def _receiver(context: CompletionContext) -> tuple[str, bool, str] | None:
    if len(context.full_text) > _MAX_CELL_BYTES:
        return None
    try:
        if len(context.full_text.encode("utf-8")) > _MAX_CELL_BYTES:
            return None
    except UnicodeError:
        return None
    lines = context.full_text.split("\n")
    if not lines or lines[0].strip() != "%%bsl" or context.cursor_line < 1:
        return None
    source = "\n".join([
        *lines[1:context.cursor_line],
        lines[context.cursor_line][:context.cursor_position],
    ])
    try:
        tokens = tokenize(source)
    except BslLexError:
        return None
    # The lexer skips comments. Requiring a token at the cursor also rejects
    # receivers in comments, including a comment following an earlier dot.
    if not tokens or tokens[-1].end != len(source):
        return None
    prefix = tokens.pop().text if tokens[-1].type == "ID" else ""
    if not tokens or tokens.pop().type != ".":
        return None
    table_row = bool(tokens and tokens[-1].type == "]")
    if table_row:
        if (len(tokens) < 3 or tokens[-3].type != "["
                or tokens[-2].type != "NUMBER"
                or not tokens[-2].text.isascii()
                or not tokens[-2].text.isdecimal()):
            return None
        del tokens[-3:]
    if not tokens or tokens[-1].type != "ID":
        return None
    path = [tokens.pop().text]
    while len(tokens) >= 2 and tokens[-1].type == "." and tokens[-2].type == "ID":
        tokens.pop()
        path.append(tokens.pop().text)
    # Do not mistake a suffix of a call, index or literal for a context root.
    if tokens and tokens[-1].type in {
        ".", "]", ")", "ID", "NUMBER", "STRING", "DATETIME", "НОВЫЙ",
    }:
        return None
    return "e1cRuntimeКонтекст." + ".".join(reversed(path)), table_row, prefix


def install_completion_matcher(shell: object) -> None:
    completer = getattr(shell, "Completer", None)
    if completer is None or not hasattr(completer, "custom_matchers"):
        return
    existing = getattr(shell, _MATCHER_ATTRIBUTE, None)
    if existing is not None:
        if existing not in completer.custom_matchers:
            completer.custom_matchers.append(existing)
        return

    @context_matcher(priority=100, identifier="onec_runtime.bsl_fields")
    def matcher(context: CompletionContext) -> SimpleMatcherResult:
        receiver = _receiver(context)
        if receiver is None:
            return {"completions": [], "suppress": False}
        handle, table_row, prefix = receiver
        completions = []
        try:
            runtime = getattr(shell, "user_ns", {}).get("_onec_runtime")
            fields = runtime.completion_fields(handle, table_row=table_row, timeout_s=1.0)
            completions = [
                SimpleCompletion(name, type="property")
                for name in dict.fromkeys(fields)
                if isinstance(name, str) and name.casefold().startswith(prefix.casefold())
            ]
        except Exception:
            # Completion is optional and must not publish platform diagnostics.
            pass
        return {
            "completions": completions,
            "matched_fragment": prefix,
            "suppress": _PYTHON_MATCHERS,
        }

    setattr(shell, _MATCHER_ATTRIBUTE, matcher)
    completer.custom_matchers.append(matcher)


def remove_completion_matcher(shell: object) -> None:
    matcher = getattr(shell, _MATCHER_ATTRIBUTE, None)
    if matcher is None:
        return
    completer = getattr(shell, "Completer", None)
    if completer is not None:
        matchers = getattr(completer, "custom_matchers", [])
        while matcher in matchers:
            matchers.remove(matcher)
    delattr(shell, _MATCHER_ATTRIBUTE)
