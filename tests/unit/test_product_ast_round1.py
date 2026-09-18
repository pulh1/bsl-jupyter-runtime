from __future__ import annotations

import pytest

from onec_runtime.bsl import LoweringMode, SemanticNotebookLowerer
from onec_runtime.bsl.parser_target import PythonParserTarget
from onec_runtime.prototype_runtime import PrototypeRuntimeController


@pytest.fixture(scope="module")
def parser_target() -> PythonParserTarget:
    return PythonParserTarget.from_generated()


def test_message_wrapper_parses_and_preserves_the_cell_result(
    parser_target: PythonParserTarget,
) -> None:
    source = 'РезультатИнструкции = 41; Сообщить("", СтатусСообщения.Важное);'
    lowered = SemanticNotebookLowerer(parser_target).lower(
        source,
        mode=LoweringMode.CAPTURE,
        message_collector_key="__onec_cell_messages_7_3",
    )
    wrapped = PrototypeRuntimeController._with_message_collector(
        lowered.source,
        lowered.messages_intercepted,
        "__onec_cell_messages_7_3",
    )

    parser_target.parse(wrapped, "БлокНоутбука")
    assert "Наконец" not in wrapped
    assert "Исключение" in wrapped
    assert "РезультатИнструкции = СтрСоединить" not in wrapped
    assert "СтатусСообщения.Важное" not in wrapped


def test_parser_target_instances_do_not_share_generated_parser_state(
    parser_target: PythonParserTarget,
) -> None:
    second = parser_target.new_instance()

    assert second.metadata == parser_target.metadata
    assert second.generated_parser is not parser_target.generated_parser


def test_status_enum_is_a_platform_global_not_persistent_context(
    parser_target: PythonParserTarget,
) -> None:
    result = SemanticNotebookLowerer(parser_target).lower(
        'Сообщить("готово", СтатусСообщения.Важное);',
        mode=LoweringMode.MAIN,
        message_collector_key="__onec_cell_messages_1_0",
    )

    assert "e1cRuntimeКонтекст.СтатусСообщения" not in result.source
    assert "СтатусСообщения.Важное" not in result.source
