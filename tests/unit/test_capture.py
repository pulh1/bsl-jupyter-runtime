import pytest

from onec_runtime import capture
from onec_runtime.capture import (
    build_capture_structure_expression,
    build_extension_call,
)


def test_builds_capture_structure_from_cyrillic_identifiers() -> None:
    expression = build_capture_structure_expression(
        ["Документы", "Результат", "результат", "ШаблонЗапроса"]
    )

    assert expression == (
        'Новый Структура("Документы,Результат,ШаблонЗапроса", '
        "Документы, Результат, ШаблонЗапроса)"
    )


@pytest.mark.parametrize(
    "name",
    ["", "Результат.Поле", 'Имя"Поля', "Имя,Поля", "Два Слова", "Если"],
)
def test_rejects_unsafe_or_keyword_capture_names(name: str) -> None:
    with pytest.raises(ValueError, match="capture variable"):
        build_capture_structure_expression([name])


def test_builds_one_extension_call_with_multiline_instruction() -> None:
    instruction = (
        'e1cRuntimeКонтекстОтладки.Результат.Добавить("ИзИнструкции");\n'
        "ПроверкаРазмера = e1cRuntimeКонтекстОтладки.Результат.Количество();\n"
        "РезультатИнструкции = ПроверкаРазмера;"
    )

    expression = build_extension_call(["Документы", "Результат"], instruction)

    assert expression == (
        "RuntimeKernelServer.ВыполнитьВКонтекстеОтладки("
        'Новый Структура("Документы,Результат", Документы, Результат), '
        '"e1cRuntimeКонтекстОтладки.Результат.Добавить(""ИзИнструкции"");" + Символы.ПС + '
        '"ПроверкаРазмера = e1cRuntimeКонтекстОтладки.Результат.Количество();" + Символы.ПС + '
        '"РезультатИнструкции = ПроверкаРазмера;")'
    )


def test_builds_lowered_extension_call_with_notebook_context() -> None:
    lowered = (
        'e1cRuntimeКонтекстОтладки.Результат.Добавить("ИзИнструкции");\n'
        'e1cRuntimeКонтекст.Вставить("РезультатИнструкции", '
        "e1cRuntimeКонтекстОтладки.Результат.Количество());"
    )

    assert hasattr(capture, "build_lowered_extension_call")
    expression = capture.build_lowered_extension_call(["Результат"], lowered)

    assert expression == (
        "RuntimeKernelServer.ВыполнитьПониженныйКодВКонтекстеОтладки("
        'Новый Структура("Результат", Результат), '
        '"e1cRuntimeКонтекстОтладки.Результат.Добавить(""ИзИнструкции"");" + Символы.ПС + '
        '"e1cRuntimeКонтекст.Вставить(""РезультатИнструкции"", '
        'e1cRuntimeКонтекстОтладки.Результат.Количество());")'
    )


def test_builds_persistent_capture_lifecycle_calls() -> None:
    assert capture.build_capture_begin_call(["Результат"]) == (
        "RuntimeKernelServer.НачатьКонтекстОтладки("
        'Новый Структура("Результат", Результат))'
    )
    assert capture.build_current_capture_call(
        "РезультатИнструкции = 1;"
    ) == (
        "RuntimeKernelServer.ВыполнитьКодТекущегоКонтекстаОтладки("
        '"РезультатИнструкции = 1;")'
    )
    assert capture.build_capture_root_expression("ШаблонЗапроса") == (
        'RuntimeKernelServer.ПолучитьЗначениеКонтекстаОтладки("ШаблонЗапроса")'
    )
    assert capture.build_capture_end_call() == (
        "RuntimeKernelServer.ЗавершитьКонтекстОтладки()"
    )


def test_builds_live_kernel_frame_capture_transfer_calls() -> None:
    address = "e1cib/tempstorage/capture-1"

    assert capture.build_capture_transfer_call(["Результат"]) == (
        "ПоместитьВоВременноеХранилище("
        'Новый Структура("Результат", Результат))'
    )
    assert capture.build_live_capture_begin_call(address) == (
        "RuntimeKernelServer.НачатьКонтекстОтладкиВКонтексте("
        'e1cRuntimeКонтекст, "e1cib/tempstorage/capture-1")'
    )
    assert capture.build_live_current_capture_call(
        "РезультатИнструкции = 1;"
    ) == (
        "RuntimeKernelServer.ВыполнитьКодВКонтекстеОтладки("
        'e1cRuntimeКонтекст, "РезультатИнструкции = 1;")'
    )
    assert capture.build_live_capture_root_transfer_call("ШаблонЗапроса") == (
        "RuntimeKernelServer.ПоместитьЗначениеКонтекстаОтладки("
        'e1cRuntimeКонтекст, "ШаблонЗапроса")'
    )
    assert capture.build_temporary_storage_value_expression(address) == (
        'ПолучитьИзВременногоХранилища("e1cib/tempstorage/capture-1")'
    )
    assert capture.build_live_capture_end_call() == (
        "RuntimeKernelServer.ЗавершитьКонтекстОтладкиВКонтексте(e1cRuntimeКонтекст)"
    )
