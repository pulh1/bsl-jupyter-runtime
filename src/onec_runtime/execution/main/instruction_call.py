"""Encode a trusted BSL instruction for evaluation on a stopped MAIN frame."""

from __future__ import annotations

from onec_runtime.experiment import bsl_string_literal


def build_main_instruction_call(instruction: str) -> str:
    """Call the extension helper without advancing the MAIN command loop."""

    if not isinstance(instruction, str) or not instruction.strip():
        raise ValueError("MAIN helper instruction must be nonempty text")
    return (
        "RuntimeKernelServer.ВыполнитьКодВКонтекстеMain(e1cRuntimeКонтекст, "
        + bsl_string_literal(instruction + "\nРезультатИнструкции = Результат;")
        + ")"
    )
