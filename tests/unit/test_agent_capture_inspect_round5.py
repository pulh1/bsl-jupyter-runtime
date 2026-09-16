from __future__ import annotations

from pathlib import Path


WORKSPACE = Path(__file__).resolve().parents[2]
KERNEL_MODULE = (
    WORKSPACE
    / "onec"
    / "OnecInteractiveRuntime"
    / "CommonModules"
    / "RuntimeKernelServer"
    / "Ext"
    / "Module.bsl"
)


def _function_body(source: str, name: str) -> str:
    marker = f"Функция {name}"
    assert marker in source, f"missing function {name}"
    return source.split(marker, 1)[1].split("КонецФункции", 1)[0]


def test_extension_uses_named_temporary_table_lookup_for_both_descriptor_paths() -> None:
    """Break caught: live temporary-table collections have Найти, not Получить."""
    source = KERNEL_MODULE.read_text(encoding="utf-8-sig")
    metadata = _function_body(source, "ПолучитьСхемуВременнойТаблицыОтладки")
    selection = _function_body(source, "ПолучитьВременнуюТаблицуОтладки")

    assert "Менеджер.Таблицы.Получить(" not in metadata + selection

    lookup = _function_body(source, "ПолучитьОписательВременнойТаблицы")
    assert "Менеджер.Таблицы.Найти(ИмяТаблицы)" in lookup
    assert ".Получить(" not in lookup
    assert 'ВызватьИсключение "Временная таблица не найдена"' in lookup
    assert "ПолучитьОписательВременнойТаблицы(Менеджер, ИмяТаблицы)" in metadata
    assert "ПолучитьОписательВременнойТаблицы(Менеджер, ИмяТаблицы)" in selection
