from __future__ import annotations

from pathlib import Path
import re
from xml.etree import ElementTree

from onec_runtime.bsl import SemanticNotebookLowerer
from onec_runtime.bsl.parser_target import PythonParserTarget
from onec_runtime.compact_table_backend import build_compact_transfer_instruction
from onec_runtime.table_materialization import ReferencePolicy
from onec_runtime.value_materialization import MaterializationOptions
from onec_runtime.value_transfer_backend import build_value_transfer_instruction


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
        "ПолучитьДопущенныеИменаСвойствДляПодсказки",
        "СериализоватьДопущенныеИменаСвойствДляПодсказки",
        "ПолучитьВидМатериализации",
        "ДопуститьЗначение",
        "СериализоватьЗначение",
        "СпроецироватьЗначенияИнспекции",
        "СериализоватьИнспекциюДляОтладки",
    }


def test_value_inspection_projector_is_a_closed_inline_admission_protocol() -> None:
    """The checked-in helper is the production boundary used by context views."""
    source = MODULE.read_text(encoding="utf-8-sig")
    def function(name: str) -> str:
        start = source.index("Функция " + name)
        end = source.index("КонецФункции", start)
        return source[start:end]

    projector = function("СпроецироватьЗначенияИнспекции")
    resolver = function("РазрешитьПутьИнспекции")
    entry = function("ЗаписьЗначенияИнспекции")
    names = function("СтраницаИменПроекцииИнспекции")

    assert "ДопуститьЗначение" in projector
    assert "Доступ" in projector
    assert "ЗавершитьДокументИнспекции" in projector
    assert "МаксимумБайт" in projector
    assert projector.index("ДопуститьЗначение(Корни") < projector.index(
        "РазрешитьПутьИнспекции"
    )
    assert resolver.index("ДопуститьЗначение(ТекущееЗначение") < resolver.index(
        "ТипЗнч(ТекущееЗначение)"
    )
    assert entry.index("ДопуститьЗначение(Значение") < entry.index(
        "ОписаниеЗначенияИнспекции"
    )
    assert 'Новый Структура("name,denied", Имя, Истина)' in entry
    assert "ЭтоСтрокаТаблицыИнспекции" in projector
    assert "Значение.Колонки.Количество() >= ЛимитКолонок" in names


def test_inspection_entry_failure_keeps_the_rest_of_the_page_available() -> None:
    """An unsupported 1C object must not abort a page of frame locals."""
    source = MODULE.read_text(encoding="utf-8-sig")
    entry = source.split("Функция ЗаписьЗначенияИнспекции(", 1)[1].split(
        "КонецФункции", 1
    )[0]
    projector = source.split("Функция СпроецироватьЗначенияИнспекции(", 1)[1].split(
        "КонецФункции", 1
    )[0]

    assert "ДопуститьЗначение(Значение" in entry
    assert "ОписаниеЗначенияИнспекции(Значение)" in entry
    assert "Исключение" in entry
    assert 'Новый Структура("name,unavailable", Имя, Истина)' in entry
    assert projector.index("Для Каждого Имя Из СтраницаИмен.Имена") < projector.index(
        'Новый Структура("name,unavailable", Имя, Истина)'
    )


def test_debugger_inspection_helper_returns_one_bounded_inline_envelope() -> None:
    """One eval result contains the admitted projection, with no private key."""
    source = MODULE.read_text(encoding="utf-8-sig")
    helper = source.split("Функция СериализоватьИнспекциюДляОтладки(", 1)[1].split(
        "КонецФункции", 1
    )[0]

    assert helper.startswith("Корни, ЗапросJSON) Экспорт")
    assert "СтрДлина(ЗапросJSON) > 65536" in helper
    assert "ПрочитатьJSON(ЧтениеJSON, Ложь)" in helper
    assert "ПроверитьЗапросИнспекцииДляОтладки(Запрос)" in helper
    assert "СпроецироватьЗначенияИнспекции(" in helper
    assert helper.index("ПроверитьЗапросИнспекцииДляОтладки") < helper.index(
        "СпроецироватьЗначенияИнспекции("
    )
    assert 'Возврат "D|worker_generation_value"' in helper
    assert 'Возврат "E|value_admission_failed"' in helper
    assert 'Возврат "R|"' in helper
    assert "Проекция.Base64" in helper
    assert "Проекция.Размер" in helper
    assert "Проекция.Хеш" in helper
    assert "RuntimeContextStoreServer" not in helper
    assert "ВременноеХранилище" not in helper


def test_debugger_inspection_request_has_closed_schema_and_budgets() -> None:
    source = MODULE.read_text(encoding="utf-8-sig")
    validator = source.split("Процедура ПроверитьЗапросИнспекцииДляОтладки(", 1)[1].split(
        "КонецПроцедуры", 1
    )[0]
    fields = {
        "action", "path", "view", "start", "stop", "role", "parameters",
        "exact", "registrations", "column_limit", "max_items", "max_bytes",
        "runtime_generation", "context_generation",
    }
    assert all(f'Запрос.Свойство("{field}")' in validator for field in fields)
    assert "Запрос.Количество() <> 14" in validator
    assert "Запрос.max_items > 100" in validator
    assert "Запрос.max_bytes > 65536" in validator
    assert "Запрос.column_limit > 101" in validator
    assert "Запрос.path.Количество() > 17" in validator
    assert "Запрос.registrations.Количество() > 32" in validator
    assert "СтрДлина(РегистрацияWorker) > 4096" in validator


def test_value_inspection_projector_marks_cycles_only_after_admission() -> None:
    """A cyclic container is public metadata but cannot be expanded again."""
    source = MODULE.read_text(encoding="utf-8-sig")

    def function(name: str) -> str:
        start = source.index("Функция " + name)
        end = source.index("КонецФункции", start)
        return source[start:end]

    projector = function("СпроецироватьЗначенияИнспекции")
    resolver = function("РазрешитьПутьИнспекции")
    entry = function("ЗаписьЗначенияИнспекции")

    assert "Разрешение.Предки.Добавить(Значение)" in projector
    assert '"Доступ,Значение,Имя,Предки"' in resolver
    assert "ЭтоЦиклИнспекции" in entry
    assert entry.index("ДопуститьЗначение(Значение") < entry.index(
        "ЭтоЦиклИнспекции"
    )
    assert 'Результат.Вставить("cycle", ОбнаруженЦикл)' in entry


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


def test_value_serializer_admits_root_and_each_descendant_before_encoding() -> None:
    source = MODULE.read_text(encoding="utf-8-sig")
    serializer = source.split("Функция СериализоватьЗначение", 1)[1].split(
        "КонецФункции", 1
    )[0]
    encoder = source.split("Функция КодироватьЗначение", 1)[1].split(
        "КонецФункции", 1
    )[0]

    assert (
        "(Значение, РежимСсылок, МаксимальнаяГлубина, МаксимумЭлементов, "
        "МаксимумБайт, ТипыОбъектовWorker) Экспорт"
    ) in serializer
    assert 'КонтекстСериализации.Вставить("ОтказДоступа", Ложь)' in serializer
    assert serializer.index("КодироватьЗначение(") < serializer.index(
        "Base64БезРазрывов"
    )
    assert "Если КонтекстСериализации.ОтказДоступа Тогда" in serializer
    assert encoder.index("ЭтоПриватноеЗначениеWorker(") < encoder.index(
        "ТипЗнч(Значение)"
    )
    assert 'КонтекстСериализации.ОтказДоступа = Истина' in encoder
    assert "Функция ЭтоПриватноеЗначениеWorker" in source
    assert 'Свойство("ManifestSha256")' in source
    assert 'Свойство("Modules")' in source
    assert 'Свойство("Exports")' in source


def test_completion_schema_helper_admits_before_reading_target_field_names() -> None:
    """Completion's one target instruction cannot inspect a denied root."""
    source = MODULE.read_text(encoding="utf-8-sig")
    helper = source.split(
        "Функция ПолучитьДопущенныеИменаСвойствДляПодсказки", 1
    )[1].split("КонецФункции", 1)[0]

    admission = helper.index("Если Не ДопуститьЗначение(")
    denied = helper.index('Состояние = "D|worker_generation_value"')
    field_read = helper.index("ПолучитьИменаСвойствДляПодсказки(")
    assert admission < denied < field_read

    scalar = source.split(
        "Функция СериализоватьДопущенныеИменаСвойствДляПодсказки", 1
    )[1].split("КонецФункции", 1)[0]
    assert scalar.index("ПолучитьДопущенныеИменаСвойствДляПодсказки(") < scalar.index(
        "Для Каждого СтрокаПодсказки"
    )
    assert 'Результат = "C" + Символы.Таб' in scalar


def test_value_serializer_success_has_the_same_explicit_access_contract_as_denial() -> None:
    """A generated protocol-2 branch may read only an explicitly present field."""
    source = MODULE.read_text(encoding="utf-8-sig")
    serializer = source.split("Функция СериализоватьЗначение", 1)[1].split(
        "КонецФункции", 1
    )[0]

    success = serializer.split("Результат = Новый Структура;", 1)[1].split(
        "Возврат Результат;", 1
    )[0]

    assert 'Результат.Вставить("Доступ", Истина);' in success
    assert success.index('Результат.Вставить("Доступ", Истина);') < success.index(
        'Результат.Вставить("Base64",'
    )


def _interpret_bsl_success_structure(function_source: str) -> dict[str, object]:
    """Faithfully model the BSL ``Структура.Вставить`` success branch.

    Designer verifies syntax only.  This minimal target-side interpreter follows
    the actual BSL success construction, so a missing ``Доступ`` behaves exactly
    as it does on 1C: the generated instruction raises while reading it.
    """
    success = function_source.split("Результат = Новый Структура;", 1)[1].split(
        "Возврат Результат;", 1
    )[0]
    result: dict[str, object] = {}
    for name, expression in re.findall(
        r'Результат\.Вставить\("([^"]+)",\s*([^;]+)\);', success
    ):
        result[name] = expression == "Истина"
    return result


def _execute_generated_success_branch(
    instruction: str,
    materialization: dict[str, object],
    *,
    variable: str,
) -> str:
    """Execute the only contract-relevant generated BSL branch."""
    assert f"Если Не {variable}.Доступ Тогда" in instruction
    if materialization["Доступ"] is not True:
        return "D|worker_generation_value"
    for field in ("Base64", "Размер", "Хеш"):
        materialization[field]
    return "R|1|1|1|" + "a" * 64 + "|4"


def test_bsl_success_structures_execute_the_generated_protocol_two_r_branch() -> None:
    value_source = MODULE.read_text(encoding="utf-8-sig")
    value_serializer = value_source.split("Функция СериализоватьЗначение", 1)[1].split(
        "КонецФункции", 1
    )[0]
    table_source = (
        EXTENSION / "CommonModules" / "RuntimeTableTransferServer" / "Ext" / "Module.bsl"
    ).read_text(encoding="utf-8-sig")
    table_finalizer = table_source.split(
        "Функция ЗавершитьКомпактнуюМатериализацию", 1
    )[1].split("КонецФункции", 1)[0]

    value_success = _interpret_bsl_success_structure(value_serializer)
    table_success = _interpret_bsl_success_structure(table_finalizer)
    for materialization in (value_success, table_success):
        materialization.update({"Base64": "e30=", "Размер": 2, "Хеш": "a" * 64})

    value_instruction = build_value_transfer_instruction(
        "e1cRuntimeКонтекст.Данные", MaterializationOptions(), "__onec_value_" + "a" * 32,
        runtime_generation=1, context_generation=1,
    )
    table_instruction = build_compact_transfer_instruction(
        "e1cRuntimeКонтекст.Таблица", ReferencePolicy(), "__onec_compact_table_" + "b" * 32,
        runtime_generation=1, context_generation=1,
    )

    assert _execute_generated_success_branch(
        value_instruction, value_success, variable="МатериализацияЗначения"
    ).startswith("R|")
    assert _execute_generated_success_branch(
        table_instruction, table_success, variable="Материализация"
    ).startswith("R|")


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
