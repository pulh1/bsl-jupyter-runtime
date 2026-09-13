from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree

from onec_runtime.bsl import SemanticNotebookLowerer
from onec_runtime.bsl.parser_target import PythonParserTarget


WORKSPACE = Path(__file__).resolve().parents[2]
EXTENSION = WORKSPACE / "onec" / "OnecInteractiveRuntime"
METADATA = EXTENSION / "CommonModules" / "RuntimeValueTransferServer.xml"
MODULE = (
    EXTENSION
    / "CommonModules"
    / "RuntimeValueTransferServer"
    / "Ext"
    / "Module.bsl"
)
WORKER = WORKSPACE / "onec" / "Worker" / "Worker" / "Ext" / "ObjectModule.bsl"
MD_NS = "http://v8.1c.ru/8.3/MDClasses"


def test_value_transfer_is_a_dedicated_server_extension_module() -> None:
    root = ElementTree.parse(METADATA).getroot()
    properties = root.find(f"{{{MD_NS}}}CommonModule/{{{MD_NS}}}Properties")

    assert properties is not None
    assert properties.findtext(f"{{{MD_NS}}}Name") == "RuntimeValueTransferServer"
    assert properties.findtext(f"{{{MD_NS}}}Global") == "false"
    assert properties.findtext(f"{{{MD_NS}}}Server") == "true"
    assert properties.findtext(f"{{{MD_NS}}}ServerCall") == "false"
    assert properties.findtext(f"{{{MD_NS}}}ReturnValuesReuse") == "DontUse"

    configuration = ElementTree.parse(EXTENSION / "Configuration.xml").getroot()
    children = configuration.find(
        f"{{{MD_NS}}}Configuration/{{{MD_NS}}}ChildObjects"
    )
    assert children is not None
    names = [child.text for child in children if child.tag.endswith("CommonModule")]
    assert names.count("RuntimeValueTransferServer") == 1


def test_value_transfer_module_parses_and_exports_only_public_boundary() -> None:
    source = MODULE.read_text(encoding="utf-8-sig")
    binding = SemanticNotebookLowerer(PythonParserTarget.from_generated()).bind_module(
        source
    )

    assert set(binding.exported_method_names) == {
        "ПолучитьИменаСвойствДляПодсказки",
        "ПолучитьВидМатериализации",
        "СериализоватьЗначение",
    }


def test_value_encoder_has_explicit_recursive_adapters_and_limits() -> None:
    source = MODULE.read_text(encoding="utf-8-sig")

    for type_name in (
        "Массив",
        "ФиксированныйМассив",
        "Структура",
        "ФиксированнаяСтруктура",
        "Соответствие",
        "ДеревоЗначений",
        "ДвоичныеДанные",
        "ХранилищеЗначения",
    ):
        assert f'Тип("{type_name}")' in source
    assert "МаксимальнаяГлубина" in source
    assert "МаксимумЭлементов" in source
    assert "МаксимумБайт" in source
    assert 'СоздатьОшибку("cycle"' in source
    assert 'СоздатьОшибку("unsupported"' in source
    assert "ПолучитьОбъект()" not in source


def test_object_adapter_reads_metadata_attributes_but_not_section_rows() -> None:
    source = MODULE.read_text(encoding="utf-8-sig")

    assert "Метаданные.НайтиПоТипу(ТипЗначения)" in source
    assert "МетаданныеОбъекта.СтандартныеРеквизиты" in source
    assert "МетаданныеОбъекта.Реквизиты" in source
    assert "МетаданныеОбъекта.ТабличныеЧасти" in source
    assert "Значение[ИмяРеквизита]" in source
    assert "Для Каждого СтрокаТабличнойЧасти" not in source


def test_metadata_family_and_wire_type_do_not_depend_on_localized_type_text() -> None:
    source = MODULE.read_text(encoding="utf-8-sig")

    route = source.split("Функция ПолучитьВидМатериализации", 1)[1].split(
        "КонецФункции", 1
    )[0]
    assert "Метаданные.НайтиПоТипу(ТипЗначения)" in route
    assert "ЭтоТабличноеЗначение(Значение, ТипЗначения, МетаданныеЗначения)" in route
    assert "XMLТипЗнч(Значение).ИмяТипа" in source
    assert 'СтрНайти(ИмяXMLТипа, "RecordSet.") > 0' in source
    assert 'СтрНайти(ИмяТипа, "ТабличнаяЧасть.")' not in route
    assert 'СтрНайти(ПолноеИмя, ".ТабличнаяЧасть.") > 0' in source
    assert "ЭтоОбъектМетаданных(МетаданныеОбъекта.ПолноеИмя())" in source
    assert "ЭтоОбъектМетаданных(Строка(ТипЗначения))" not in source
    assert "ЧастиИмени.Количество() <> 2" in source
    assert "ТехническоеИмяТипаМетаданных(МетаданныеОбъекта, \"Объект\")" in source
    assert "ТехническоеИмяТипаМетаданных(МетаданныеОбъекта, \"Ссылка\")" in source


def test_reference_and_object_dispatch_use_only_metadata_type_adapters() -> None:
    source = MODULE.read_text(encoding="utf-8-sig")
    detector = source.split("Функция ЭтоСсылка", 1)[1].split("КонецФункции", 1)[0]

    assert "Значение.УникальныйИдентификатор()" not in detector
    assert "МетаданныеОбъекта.ТипСсылки()" not in source
    assert "МетаданныеОбъекта.ТипОбъекта()" not in source
    assert "XMLТипЗнч(Значение).ИмяТипа" in source
    assert 'СтрНайти(ИмяXMLТипа, "Ref.") > 0' in detector


def test_encoder_accounts_bytes_before_binary_encoding_and_rejects_unbounded_storage() -> None:
    source = MODULE.read_text(encoding="utf-8-sig")
    binary_branch = source.split('ТипЗначения = Тип("ДвоичныеДанные")', 1)[1].split(
        "ИначеЕсли", 1
    )[0]
    storage_branch = source.split('ТипЗначения = Тип("ХранилищеЗначения")', 1)[1].split(
        "КонецЕсли", 1
    )[0]

    assert "КоличествоБайтОценка" in source
    assert "УчестьБайты" in source
    assert binary_branch.index("Значение.Размер()") < binary_branch.index("Base64БезРазрывов")
    assert "Значение.Получить()" not in storage_branch
    assert 'СоздатьОшибку("unsupported"' in storage_branch


def test_encoder_uses_conservative_string_budget_and_counts_metadata_entries() -> None:
    source = MODULE.read_text(encoding="utf-8-sig")
    string_budget = source.split("Функция УчестьСтроку", 1)[1].split(
        "КонецФункции", 1
    )[0]
    tree = source.split("Функция КодироватьДерево", 1)[1].split(
        "Функция КодироватьСтрокиДерева", 1
    )[0]
    tree_rows = source.split("Функция КодироватьСтрокиДерева", 1)[1].split(
        "КонецФункции", 1
    )[0]
    object_encoder = source.split("Функция КодироватьОбъект", 1)[1].split(
        "Функция КодироватьДерево", 1
    )[0]

    assert "ПолучитьДвоичныеДанныеИзСтроки" not in string_budget
    assert "СтрДлина(Значение) * 6" in string_budget
    assert "УчестьЭлемент(Путь + \".columns\"" in tree
    assert "УчестьСтроку(Колонка.Имя" in tree
    assert "УчестьЭлемент(ПутьСтроки + \".values\"" in tree_rows
    assert "УчестьЭлемент(Путь + \".sections\"" in object_encoder
    assert "УчестьСтроку(ТабличнаяЧасть.Имя" in object_encoder


def test_value_transfer_infrastructure_does_not_leak_into_worker() -> None:
    worker_source = WORKER.read_text(encoding="utf-8-sig")

    assert "СериализоватьЗначение" not in worker_source
    assert "ПолучитьВидМатериализации" not in worker_source
