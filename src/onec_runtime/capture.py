from __future__ import annotations

from collections.abc import Iterable

from onec_runtime.bsl.lexer import KEYWORDS
from onec_runtime.experiment import bsl_string_literal


def _validate_capture_variable(name: str) -> None:
    valid = bool(name) and (name[0].isalpha() or name[0] == "_")
    valid = valid and all(character.isalnum() or character == "_" for character in name)
    if not valid or name.upper() in KEYWORDS:
        raise ValueError(f"Unsafe capture variable name: {name!r}")


def build_capture_structure_expression(variable_names: Iterable[str]) -> str:
    names: list[str] = []
    seen: set[str] = set()
    for name in variable_names:
        _validate_capture_variable(name)
        folded = name.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        names.append(name)
    if not names:
        raise ValueError("At least one capture variable is required")
    fields = ",".join(names)
    values = ", ".join(names)
    return f'Новый Структура("{fields}", {values})'


def build_extension_call(variable_names: Iterable[str], instruction: str) -> str:
    structure = build_capture_structure_expression(variable_names)
    code = bsl_string_literal(instruction)
    return (
        "RuntimeKernelServer.ВыполнитьВКонтекстеОтладки("
        f"{structure}, {code})"
    )


def build_lowered_extension_call(
    variable_names: Iterable[str], instruction: str
) -> str:
    structure = build_capture_structure_expression(variable_names)
    code = bsl_string_literal(instruction)
    return (
        "RuntimeKernelServer.ВыполнитьПониженныйКодВКонтекстеОтладки("
        f"{structure}, {code})"
    )


def build_capture_begin_call(variable_names: Iterable[str]) -> str:
    structure = build_capture_structure_expression(variable_names)
    return f"RuntimeKernelServer.НачатьКонтекстОтладки({structure})"


def build_current_capture_call(instruction: str) -> str:
    code = bsl_string_literal(instruction)
    return (
        "RuntimeKernelServer.ВыполнитьКодТекущегоКонтекстаОтладки("
        f"{code})"
    )


def build_capture_root_expression(name: str) -> str:
    _validate_capture_variable(name)
    return (
        "RuntimeKernelServer.ПолучитьЗначениеКонтекстаОтладки("
        f'"{name}")'
    )


def build_capture_end_call() -> str:
    return "RuntimeKernelServer.ЗавершитьКонтекстОтладки()"


def build_capture_transfer_call(variable_names: Iterable[str]) -> str:
    structure = build_capture_structure_expression(variable_names)
    return f"ПоместитьВоВременноеХранилище({structure})"


def build_live_capture_begin_call(address: str) -> str:
    return (
        "RuntimeKernelServer.НачатьКонтекстОтладкиВКонтексте(e1cRuntimeКонтекст, "
        + bsl_string_literal(address)
        + ")"
    )


def build_live_current_capture_call(instruction: str) -> str:
    return (
        "RuntimeKernelServer.ВыполнитьКодВКонтекстеОтладки(e1cRuntimeКонтекст, "
        + bsl_string_literal(instruction)
        + ")"
    )


def build_live_capture_root_transfer_call(name: str) -> str:
    _validate_capture_variable(name)
    return (
        "RuntimeKernelServer.ПоместитьЗначениеКонтекстаОтладки(e1cRuntimeКонтекст, "
        + bsl_string_literal(name)
        + ")"
    )


def build_temporary_storage_value_expression(address: str) -> str:
    return "ПолучитьИзВременногоХранилища(" + bsl_string_literal(address) + ")"


def build_live_capture_end_call() -> str:
    return "RuntimeKernelServer.ЗавершитьКонтекстОтладкиВКонтексте(e1cRuntimeКонтекст)"
