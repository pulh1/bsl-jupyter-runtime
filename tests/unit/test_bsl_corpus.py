from pathlib import Path

import pytest

from tools.support import corpus as bsl


def test_selects_deterministic_ten_percent_without_regulated_reporting(
    tmp_path: Path,
) -> None:
    for index in range(10):
        module = tmp_path / "CommonModules" / f"Обычный{index}" / "Ext" / "Module.bsl"
        module.parent.mkdir(parents=True)
        module.write_text(f"Процедура Тест{index}()\nКонецПроцедуры", encoding="utf-8")
    regulated_path = (
        tmp_path
        / "Reports"
        / "РегламентированныйОтчетСтатистика"
        / "Ext"
        / "ObjectModule.bsl"
    )
    regulated_path.parent.mkdir(parents=True)
    regulated_path.write_text("Процедура НеБрать()\nКонецПроцедуры", encoding="utf-8")
    regulated_content = (
        tmp_path / "CommonModules" / "СкрытаяСвязь" / "Ext" / "Module.bsl"
    )
    regulated_content.parent.mkdir(parents=True)
    regulated_content.write_text(
        "// Поддержка подсистемы РегламентированнаяОтчетность\n"
        "Процедура НеБрать()\nКонецПроцедуры",
        encoding="utf-8",
    )

    assert hasattr(bsl, "select_module_sample")
    first = bsl.select_module_sample(tmp_path, fraction=1.0)
    second = bsl.select_module_sample(tmp_path, fraction=1.0)

    assert first == second
    assert first.total_modules == 12
    assert first.excluded_by_path == 1
    assert first.excluded_by_content == 1
    assert first.eligible_modules == 10
    assert first.target_modules == 10
    assert len(first.selected) == 10
    assert all("Регламент" not in relative for relative in first.selected)


def test_reads_content_only_for_ranked_sample_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index in range(100):
        module = tmp_path / f"Module{index:03}.bsl"
        module.write_text("Процедура Тест()\nКонецПроцедуры", encoding="utf-8")
    original_read_text = Path.read_text
    reads: list[Path] = []

    def tracked_read_text(path: Path, *args: object, **kwargs: object) -> str:
        reads.append(path)
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", tracked_read_text)

    selection = bsl.select_module_sample(tmp_path, fraction=0.10)

    assert selection.target_modules == 10
    assert len(selection.selected) == 10
    assert len(reads) == 10


def test_excludes_modules_referenced_by_regulated_reporting_subsystem(
    tmp_path: Path,
) -> None:
    subsystem = tmp_path / "Subsystems" / "Отчетность.xml"
    subsystem.parent.mkdir(parents=True)
    subsystem.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns:xr="http://v8.1c.ru/8.3/xcf/readable" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
        '  <xr:Item xsi:type="xr:MDObjectRef">'
        "DataProcessor.ДокументооборотСКонтролирующимиОрганами"
        "</xr:Item>\n"
        '  <xr:Item xsi:type="xr:MDObjectRef">'
        "DocumentJournal.РегламентированныеДокументы"
        "</xr:Item>\n"
        "</MetaDataObject>",
        encoding="utf-8",
    )
    excluded = (
        tmp_path
        / "DataProcessors"
        / "ДокументооборотСКонтролирующимиОрганами"
        / "Ext"
        / "ObjectModule.bsl"
    )
    excluded.parent.mkdir(parents=True)
    excluded.write_text("Процедура НеБрать()\nКонецПроцедуры", encoding="utf-8")
    excluded_journal = (
        tmp_path
        / "DocumentJournals"
        / "РегламентированныеДокументы"
        / "Ext"
        / "ManagerModule.bsl"
    )
    excluded_journal.parent.mkdir(parents=True)
    excluded_journal.write_text(
        "Процедура ТожеНеБрать()\nКонецПроцедуры", encoding="utf-8"
    )
    included = tmp_path / "CommonModules" / "РасчетЗарплаты" / "Ext" / "Module.bsl"
    included.parent.mkdir(parents=True)
    included.write_text("Процедура Брать()\nКонецПроцедуры", encoding="utf-8")

    selection = bsl.select_module_sample(tmp_path, fraction=1.0)

    assert selection.excluded_by_subsystem == 2
    assert selection.selected == ("CommonModules/РасчетЗарплаты/Ext/Module.bsl",)
