from types import SimpleNamespace

import pytest
from IPython.core.completer import provisionalcompleter
from IPython.core.interactiveshell import InteractiveShell

from onec_runtime.errors import ProtocolError
from onec_runtime_jupyter.extension import load_ipython_extension, unload_ipython_extension


class Runtime:
    def __init__(self):
        self.fields = ("Сотрудник", "Организация", "ФОТ")
        self.calls = []
        self.closed = False

    def completion_fields(self, handle, *, table_row=False, timeout_s=1.0):
        self.calls.append((handle, table_row, timeout_s))
        if self.closed:
            raise ProtocolError("private error must not escape")
        return self.fields


@pytest.fixture
def shell():
    value = InteractiveShell()
    value.Completer.use_jedi = False
    value.user_ns["_onec_runtime"] = Runtime()
    load_ipython_extension(value)
    yield value
    unload_ipython_extension(value)


def complete(shell, source):
    with provisionalcompleter():
        return list(shell.Completer.completions(source, len(source)))


def test_live_table_fields_use_kernel_completion_with_correct_replacement(shell):
    source = "%%bsl\nКадровыеДанные[0]."
    items = complete(shell, source)
    assert {item.text for item in items} >= {"Сотрудник", "Организация", "ФОТ"}
    fields = [item for item in items if item.text == "ФОТ"]
    assert len(fields) == 1 and fields[0].type == "property"
    assert (fields[0].start, fields[0].end) == (len(source), len(source))
    assert shell.user_ns["_onec_runtime"].calls == [("Контекст.КадровыеДанные", True, 1.0)]


def test_structures_nested_paths_and_case_insensitive_prefix(shell):
    runtime = shell.user_ns["_onec_runtime"]
    runtime.fields = ("Номер", "Название")
    source = "%%bsl\nЗначение = Данные.Вложенные.на"
    items = complete(shell, source)
    assert [item.text for item in items] == ["Название"]
    assert (items[0].start, items[0].end) == (len(source)-2, len(source))
    assert runtime.calls == [("Контекст.Данные.Вложенные", False, 1.0)]


def test_fields_are_read_again_after_value_replacement_and_runtime_reinstall(shell):
    runtime = shell.user_ns["_onec_runtime"]
    source = "%%bsl\nРезультатЗапроса[0]."
    assert "ФОТ" in {item.text for item in complete(shell, source)}
    runtime.fields = ("НовыйСтолбец",)
    assert {item.text for item in complete(shell, source)} == {"НовыйСтолбец"}
    replacement = Runtime()
    replacement.fields = ("ДругаяСтруктура",)
    shell.user_ns["_onec_runtime"] = replacement
    assert {item.text for item in complete(shell, source)} == {"ДругаяСтруктура"}
    assert len(runtime.calls) == 2 and len(replacement.calls) == 1


@pytest.mark.parametrize("source", [
    "КадровыеДанные[0].", "%%python\nКадровыеДанные[0].",
    '%%bsl\nТекст = "Данные.', '%%bsl\n// Данные.',
    "%%bsl\nДанные.\n// продолжение", "%%bsl\nПолучитьДанные().",
    "%%bsl\nПолучитьДанные().Поле.", "%%bsl\nДанные[Вызвать()].",
    "%%bsl\nДанные[-1].", "%%bsl\nДанные[0.5].", "%%bsl\nДанные[0];Удалить().",
    "%%bsl\nДанные[0].Вложенные.", "%%bsl\nДанные[0][1].",
    "%%bsl\nДанные[1e2].", "%%bsl\n(Данные).", "%%bsl\nНовый Структура.",
    '%%bsl\nТекст = "Первая строка\n|Данные.',
    "%%bsl\nДанные. // комментарий",
])
def test_no_live_queries_for_python_strings_comments_or_executable_receivers(shell, source):
    complete(shell, source)
    assert not shell.user_ns["_onec_runtime"].calls


def test_unavailable_runtime_does_not_break_completion(shell, capsys):
    shell.user_ns["_onec_runtime"].closed = True
    complete(shell, "%%bsl\nДанные.")
    assert "private error" not in capsys.readouterr().err


def test_load_is_idempotent_and_unload_removes_owned_matcher(shell):
    load_ipython_extension(shell)
    complete(shell, "%%bsl\nДанные.")
    assert len(shell.user_ns["_onec_runtime"].calls) == 1
    unload_ipython_extension(shell)
    complete(shell, "%%bsl\nДанные.")
    assert len(shell.user_ns["_onec_runtime"].calls) == 1


def test_cursor_in_middle_of_cell_uses_only_code_before_cursor(shell):
    source = '%%bsl\n// Данные из предыдущего вызова\nРезультат = Данные.фо;\nТекст = "строка";'
    position = source.index("фо") + 2
    with provisionalcompleter():
        items = list(shell.Completer.completions(source, position))
    assert [item.text for item in items] == ["ФОТ"]
    assert (items[0].start, items[0].end) == (position - 2, position)
    assert shell.user_ns["_onec_runtime"].calls == [("Контекст.Данные", False, 1.0)]


def test_bsl_fields_coexist_with_default_jedi_and_python_completion(shell):
    shell.Completer.use_jedi = True
    # Real runtime variables also have Python proxies. Their attributes must
    # not leak into the BSL field list through Jedi's actual matcher identifier.
    shell.user_ns["Данные"] = SimpleNamespace(to_df=lambda: None, materialize=lambda: None)
    assert {item.text for item in complete(shell, "%%bsl\nДанные.")} == {
        "Сотрудник", "Организация", "ФОТ",
    }
    shell.Completer.use_jedi = False
    shell.user_ns["python_object"] = SimpleNamespace(python_field=42)
    source = "python_object.py"
    assert "python_object.python_field" in {
        source[:item.start] + item.text + source[item.end:]
        for item in complete(shell, source)
    }
    assert len(shell.user_ns["_onec_runtime"].calls) == 1


def test_runtime_can_be_installed_after_extension_load(shell):
    del shell.user_ns["_onec_runtime"]
    assert complete(shell, "%%bsl\nДанные.") == []
    runtime = Runtime()
    shell.user_ns["_onec_runtime"] = runtime
    assert "ФОТ" in {item.text for item in complete(shell, "%%bsl\nДанные.")}
    assert len(runtime.calls) == 1


def test_load_and_unload_support_shell_without_completer():
    class FakeShell:
        user_ns = {}

        def register_magics(self, magics):
            self.magics = magics

    shell = FakeShell()
    load_ipython_extension(shell)
    unload_ipython_extension(shell)


@pytest.mark.parametrize("padding", ["x" * 65536, "я" * 32768], ids=["ascii", "utf8"])
def test_large_cell_skips_runtime_completion(shell, padding):
    complete(shell, "%%bsl\n// " + padding + "\nДанные.")
    assert not shell.user_ns["_onec_runtime"].calls
