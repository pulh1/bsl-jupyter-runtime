from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from uuid import UUID

import pytest

from integration.support.zup_capture_fixture import locate_payroll_capture_points
from onec_runtime.errors import ProtocolError
from onec_runtime.kernel import COMMON_MODULE_PROPERTY_ID


MODULE_UUID = UUID("241b925b-fa18-4451-b695-0c9a22574341")

MODULE_SOURCE = dedent(
    """\
    Функция ЗарплатаКВыплате(ОписаниеОперации, ОтборСотрудников) Экспорт
        МенеджерВременныхТаблиц = Новый МенеджерВременныхТаблиц;
        СоздатьВТСотрудникиДляВедомостиПоШапке(МенеджерВременныхТаблиц, ОписаниеОперации, ОтборСотрудников);
        СоздатьВТЗарплатаКВыплате(
            МенеджерВременныхТаблиц,
            ОписаниеОперации);

        ОписательВременныхТаблиц =
            КадровыйУчет.ОписательВременныхТаблицДляСоздатьВТКадровыеДанныеСотрудников(МенеджерВременныхТаблиц);
        Возврат Новый ТаблицаЗначений;
    КонецФункции

    Процедура СоздатьВТЗарплатаКВыплате(МенеджерВременныхТаблиц, ОписаниеОперации) Экспорт
        Параметры = Новый Структура;
        ВзаиморасчетыССотрудниками.СоздатьВТЗарплатаКВыплате(
            МенеджерВременныхТаблиц,
            Параметры);

        ВзаиморасчетыССотрудниками.СоздатьВТЗарплатаКВыплатеОграниченнаяСальдоФизлиц(
            МенеджерВременныхТаблиц,
            Параметры);
    КонецПроцедуры
    """
)


def _source_root(tmp_path: Path, source: str = MODULE_SOURCE, *, uuid: str | None = None) -> Path:
    root = tmp_path / "src"
    module_root = root / "CommonModules" / "ВедомостьНаВыплатуЗарплаты"
    module_root.mkdir(parents=True)
    identifier = uuid or str(MODULE_UUID)
    (module_root / "ВедомостьНаВыплатуЗарплаты.mdo").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<mdclass:CommonModule xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass" '
        f'uuid="{identifier}"><name>ВедомостьНаВыплатуЗарплаты</name>'
        "<server>true</server></mdclass:CommonModule>",
        encoding="utf-8",
    )
    (module_root / "Module.bsl").write_text(source, encoding="utf-8", newline="\n")
    return root


def _designer_source_root(tmp_path: Path) -> Path:
    root = tmp_path / "designer"
    common_modules = root / "CommonModules"
    module_root = common_modules / "ВедомостьНаВыплатуЗарплаты" / "Ext"
    module_root.mkdir(parents=True)
    (common_modules / "ВедомостьНаВыплатуЗарплаты.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">'
        f'<CommonModule uuid="{MODULE_UUID}"><Properties>'
        '<Name>ВедомостьНаВыплатуЗарплаты</Name>'
        '</Properties></CommonModule></MetaDataObject>',
        encoding="utf-8",
    )
    (module_root / "Module.bsl").write_text(
        MODULE_SOURCE, encoding="utf-8", newline="\n"
    )
    return root


def test_locator_finds_two_execution_ordered_capture_boundaries(tmp_path: Path) -> None:
    points = locate_payroll_capture_points(_source_root(tmp_path))

    assert points.module_name == "ВедомостьНаВыплатуЗарплаты"
    assert points.module_uuid == MODULE_UUID
    assert points.before_limit.module_type == "ConfigModule"
    assert points.before_limit.object_id == MODULE_UUID
    assert points.before_limit.property_id == UUID(COMMON_MODULE_PROPERTY_ID)
    assert points.before_limit.line == 19
    assert points.after_cascade.line == 8
    assert points.before_limit != points.after_cascade
    assert len(points.source_sha256) == 64


def test_locator_accepts_designer_xml_export_layout(tmp_path: Path) -> None:
    root = _designer_source_root(tmp_path)
    points = locate_payroll_capture_points(root)

    assert points.module_uuid == MODULE_UUID
    assert points.module_path == (
        root
        / "CommonModules"
        / "ВедомостьНаВыплатуЗарплаты"
        / "Ext"
        / "Module.bsl"
    )


@pytest.mark.parametrize(
    ("source", "message"),
    (
        (
            MODULE_SOURCE.replace(
                "ВзаиморасчетыССотрудниками.СоздатьВТЗарплатаКВыплате(\n",
                "ВзаиморасчетыССотрудниками.ПропуститьПервыйЭтап(\n",
            ),
            "primary payroll temporary-table call",
        ),
        (
            MODULE_SOURCE.replace(
                "ВзаиморасчетыССотрудниками.СоздатьВТЗарплатаКВыплатеОграниченнаяСальдоФизлиц(\n",
                "ВзаиморасчетыССотрудниками.ПропуститьОграничение(\n",
            ),
            "payroll debt-limit call",
        ),
        (
            MODULE_SOURCE.replace(
                "    ОписательВременныхТаблиц =\n",
                "    // итоговая стадия удалена\n",
            ),
            "post-cascade assignment",
        ),
    ),
)
def test_locator_fails_closed_when_required_boundary_drifts(
    tmp_path: Path,
    source: str,
    message: str,
) -> None:
    with pytest.raises(ProtocolError, match=message):
        locate_payroll_capture_points(_source_root(tmp_path, source))


def test_locator_rejects_malformed_metadata_uuid(tmp_path: Path) -> None:
    with pytest.raises(ProtocolError, match="UUID"):
        locate_payroll_capture_points(_source_root(tmp_path, uuid="not-a-uuid"))


def test_locator_ignores_commented_call_lookalikes(tmp_path: Path) -> None:
    source = MODULE_SOURCE.replace(
        "Процедура СоздатьВТЗарплатаКВыплате",
        "// ВзаиморасчетыССотрудниками.СоздатьВТЗарплатаКВыплате(ложный вызов);\n"
        "Процедура СоздатьВТЗарплатаКВыплате",
    )

    points = locate_payroll_capture_points(_source_root(tmp_path, source))

    assert points.before_limit.line == 20
    assert points.after_cascade.line == 8
