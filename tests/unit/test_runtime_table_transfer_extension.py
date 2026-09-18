from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree

from onec_runtime.bsl import SemanticNotebookLowerer
from onec_runtime.bsl.parser_target import PythonParserTarget


WORKSPACE = Path(__file__).resolve().parents[2]
EXTENSION = WORKSPACE / "onec" / "OnecInteractiveRuntime"
SERVICE_MODULE = EXTENSION / "CommonModules" / "RuntimeTableTransferServer" / "Ext" / "Module.bsl"
WORKER_MODULE = WORKSPACE / "onec" / "Worker" / "Worker" / "Ext" / "ObjectModule.bsl"
MD_NS = "http://v8.1c.ru/8.3/MDClasses"


def test_table_transfer_service_is_a_separate_server_extension_module() -> None:
    metadata = ElementTree.parse(
        EXTENSION / "CommonModules" / "RuntimeTableTransferServer.xml"
    ).getroot()
    properties = metadata.find(f"{{{MD_NS}}}CommonModule/{{{MD_NS}}}Properties")
    assert properties is not None
    assert properties.findtext(f"{{{MD_NS}}}Name") == "RuntimeTableTransferServer"
    assert properties.findtext(f"{{{MD_NS}}}Server") == "true"
    assert properties.findtext(f"{{{MD_NS}}}ServerCall") == "false"
    assert properties.findtext(f"{{{MD_NS}}}ReturnValuesReuse") == "DontUse"


def test_table_transfer_service_exports_complete_infrastructure_catalog() -> None:
    source = SERVICE_MODULE.read_text(encoding="utf-8-sig")
    binding = SemanticNotebookLowerer(PythonParserTarget.from_generated()).bind_module(source)

    assert set(binding.exported_method_names) == {
        "СобратьСсылки",
        "ПолучитьПредставленияСсылок",
        "СериализоватьТаблицу",
        "ПолучитьЧасть",
        "СериализоватьКомпактнуюТаблицу",
        "ЗавершитьКомпактнуюМатериализацию",
            "ПолучитьКомпактнуюСхему",
            "ПодготовитьТабличноеЗначение",
        }


def test_compact_schema_uses_declared_column_types_before_row_values() -> None:
    source = SERVICE_MODULE.read_text(encoding="utf-8-sig")

    schema = source.split("Функция ПолучитьКомпактнуюСхему", 1)[1].split(
        "КонецФункции", 1
    )[0]

    assert "Колонка.ТипЗначения" in schema
    assert "Для Каждого СтрокаТаблицы Из Таблица" not in schema


def test_query_result_schema_uses_declared_columns_without_materializing_rows() -> None:
    source = SERVICE_MODULE.read_text(encoding="utf-8-sig")

    assert "Функция ПолучитьКолонкиТабличногоЗначения" in source
    schema = source.split("Функция ПолучитьКомпактнуюСхему", 1)[1].split(
        "КонецФункции", 1
    )[0]
    columns = source.split("Функция ПолучитьКолонкиТабличногоЗначения", 1)[1].split(
        "КонецФункции", 1
    )[0]

    assert "ПолучитьКолонкиТабличногоЗначения(Таблица)" in schema
    assert "ПодготовитьТабличноеЗначение" not in schema
    assert ".Выгрузить(" not in schema
    assert 'Тип("ТаблицаЗначений")' in columns
    assert 'Тип("РезультатЗапроса")' in columns
    assert "Возврат ТабличноеЗначение.Колонки;" in columns
    assert ".Выгрузить(" not in columns


def test_query_result_normalization_rejects_n_plus_one_before_cell_read() -> None:
    """Break caught: a query result must not return a truncated successful page."""
    source = SERVICE_MODULE.read_text(encoding="utf-8-sig")
    helper = source.split(
        "Функция ПодготовитьТабличноеЗначение", 1
    )[1].split("КонецФункции", 1)[0]

    assert "(ТабличноеЗначение, МаксимумСтрок = 0) Экспорт" in helper
    assert 'Тип("РезультатЗапроса")' in helper
    assert ".Выгрузить(" not in helper
    assert "МаксимумСтрок <= 0" in helper
    assert "Колонка.Имя, Колонка.ТипЗначения" in helper
    assert "ВыборкаДанных = ТабличноеЗначение.Выбрать();" in helper
    assert "Пока ВыборкаДанных.Следующий() Цикл" in helper

    next_row = helper.index("Пока ВыборкаДанных.Следующий() Цикл")
    row_guard = helper.index("Результат.Количество() >= МаксимумСтрок")
    overflow = helper.index(
        'ВызватьИсключение "Превышен лимит строк компактной таблицы";', row_guard
    )
    allocate = helper.index("Результат.Добавить()")
    copy = helper.index(
        "ВыборкаДанных[КолонкаРезультата.Имя]", allocate
    )
    assert next_row < row_guard < overflow < allocate < copy


def test_value_table_stays_zero_copy_and_other_table_like_values_fail_closed() -> None:
    source = SERVICE_MODULE.read_text(encoding="utf-8-sig")
    helper = source.split(
        "Функция ПодготовитьТабличноеЗначение", 1
    )[1].split("КонецФункции", 1)[0]

    value_table = helper.index('Тип("ТаблицаЗначений")')
    unchanged = helper.index("Возврат ТабличноеЗначение;", value_table)
    assert 'Тип("РезультатЗапроса")' in helper
    query_result = helper.index('Тип("РезультатЗапроса")')
    unsupported = helper.index("Неподдерживаемое табличное значение")
    assert value_table < unchanged < query_result < unsupported
    assert "Попытка" not in helper
    assert ".Выгрузить(" not in helper
    assert ".Выгрузить(" not in source


def test_compact_serializer_passes_row_budget_into_query_normalization() -> None:
    source = SERVICE_MODULE.read_text(encoding="utf-8-sig")
    serializer = source.split(
        "Функция СериализоватьКомпактнуюТаблицу", 1
    )[1].split("КонецФункции", 1)[0]

    assert (
        "Таблица = ПодготовитьТабличноеЗначение(Таблица, МаксимумСтрок);"
        in serializer
    )


def test_compact_serializer_admits_root_and_cells_before_type_or_payload() -> None:
    source = SERVICE_MODULE.read_text(encoding="utf-8-sig")
    serializer = source.split(
        "Функция СериализоватьКомпактнуюТаблицу", 1
    )[1].split("КонецФункции", 1)[0]
    classifier = source.split(
        "Функция ОпределитьКомпактнуюСхемуКолонок", 1
    )[1].split("КонецФункции", 1)[0]

    assert (
        "(Знач Таблица, РежимСсылок, РежимыСсылокКолонок, "
        "ТипыОбъектовWorker, МаксимумСтрок = 0, МаксимумБайт = 0) Экспорт"
    ) in serializer
    assert serializer.index("ЭтоПриватноеЗначениеWorker(Таблица") < serializer.index(
        "ПодготовитьТабличноеЗначение(Таблица"
    )
    assert (
        "ОпределитьКомпактнуюСхемуКолонок(Таблица, ТипыОбъектовWorker, "
        "МаксимумСтрок)" in serializer
    )
    assert classifier.index("ЭтоПриватноеЗначениеWorker(") < classifier.index(
        "КомпактныйВидЗначения("
    )
    assert "Если СхемаКолонок = Неопределено Тогда" in serializer
    assert serializer.index("ЭтоПриватноеЗначениеWorker(ЗначениеЯчейки") < serializer.index(
        "КомпактноеЗначение(ЗначениеЯчейки"
    )
    assert serializer.index("Если ОтказДоступа Тогда") < serializer.index(
        "ЗавершитьКомпактнуюМатериализацию"
    )


def test_compact_serializer_success_has_the_same_explicit_access_contract_as_denial() -> None:
    source = SERVICE_MODULE.read_text(encoding="utf-8-sig")
    finalizer = source.split("Функция ЗавершитьКомпактнуюМатериализацию", 1)[1].split(
        "КонецФункции", 1
    )[0]

    assert 'Результат.Вставить("Доступ", Истина);' in finalizer
    assert finalizer.index('Результат.Вставить("Доступ", Истина);') < finalizer.index(
        'Результат.Вставить("Base64",'
    )


def test_generic_compact_serializer_uses_declared_schema_before_observed_values() -> None:
    source = SERVICE_MODULE.read_text(encoding="utf-8-sig")
    classifier = source.split("Функция ОпределитьКомпактнуюСхемуКолонок", 1)[1].split(
        "КонецФункции", 1
    )[0]

    assert "ОпределитьОбъявленныйКомпактныйВид(Колонка.ТипЗначения)" in classifier
    assert classifier.index("ОпределитьОбъявленныйКомпактныйВид(") < classifier.index(
        "Для Каждого СтрокаТаблицы Из Таблица"
    )


def test_compact_schema_classifier_never_reads_past_the_bounded_row_page() -> None:
    """A sentinel after the page must be unreachable for ValueTable and query input."""
    source = SERVICE_MODULE.read_text(encoding="utf-8-sig")
    serializer = source.split("Функция СериализоватьКомпактнуюТаблицу", 1)[1].split(
        "КонецФункции", 1
    )[0]
    classifier = source.split("Функция ОпределитьКомпактнуюСхемуКолонок", 1)[1].split(
        "КонецФункции", 1
    )[0]

    assert "ОпределитьКомпактнуюСхемуКолонок(Таблица, ТипыОбъектовWorker, МаксимумСтрок)" in serializer
    assert "(Таблица, ТипыОбъектовWorker, Знач МаксимумСтрок)" in classifier
    row_guard = classifier.index("КоличествоПроверенныхСтрок >= МаксимумСтрок")
    cell_read = classifier.index("ЗначениеЯчейки = СтрокаТаблицы[Колонка.Имя]")
    assert row_guard < cell_read

    class SentinelRow:
        def __getitem__(self, column: str) -> str:
            raise AssertionError(f"classifier touched row outside its page: {column}")

    def bounded_classifier_reads(rows: list[object], maximum_rows: int) -> list[object]:
        """The BSL loop's guard must run before its cell access."""
        inspected = 0
        observed: list[object] = []
        for row in rows:
            if inspected >= maximum_rows:
                break
            observed.append(row["Колонка"])  # type: ignore[index]
            inspected += 1
        return observed

    page = [{"Колонка": "first"}, {"Колонка": "second"}, SentinelRow()]
    assert bounded_classifier_reads(page, 2) == ["first", "second"]


def test_unbounded_schema_probe_keeps_the_serializer_row_budget_unbounded() -> None:
    """A schema sample must not turn max_rows=0 into a one-row transfer."""
    source = SERVICE_MODULE.read_text(encoding="utf-8-sig")
    classifier = source.split("Функция ОпределитьКомпактнуюСхемуКолонок", 1)[1].split(
        "КонецФункции", 1
    )[0]

    assert "(Таблица, ТипыОбъектовWorker, Знач МаксимумСтрок)" in classifier


def test_compact_serializer_checks_budgets_before_base64_construction() -> None:
    source = SERVICE_MODULE.read_text(encoding="utf-8-sig")
    serializer = source.split(
        "Функция СериализоватьКомпактнуюТаблицу", 1
    )[1].split("КонецФункции", 1)[0]

    assert "КоличествоСтрокJSONL >= МаксимумСтрок" in serializer
    assert "РазмерJSONL + РазмерСтрокиJSONL > МаксимумБайт" in serializer
    assert serializer.index("КоличествоСтрокJSONL >= МаксимумСтрок") < serializer.index(
        "СтрокиJSONL.Добавить(JSONСтроки)"
    )
    assert serializer.index("РазмерJSONL + РазмерСтрокиJSONL > МаксимумБайт") < serializer.index(
        "СтрокиJSONL.Добавить(JSONСтроки)"
    )


def test_kernel_exports_atomic_compact_payload_take() -> None:
    kernel = (
        EXTENSION / "CommonModules" / "RuntimeKernelServer" / "Ext" / "Module.bsl"
    ).read_text(encoding="utf-8-sig")

    binding = SemanticNotebookLowerer(PythonParserTarget.from_generated()).bind_module(
        kernel
    )

    assert "ЗабратьКомпактнуюМатериализациюИзКонтекста" in set(
        binding.exported_method_names
    )
    assert "e1cRuntimeКонтекст.Удалить(Ключ)" in kernel


def test_compact_serializer_classifies_columns_once_before_full_row_loop() -> None:
    source = SERVICE_MODULE.read_text(encoding="utf-8-sig")

    serializer = source.split(
        "Функция СериализоватьКомпактнуюТаблицу", 1
    )[1].split("КонецФункции", 1)[0]
    value_encoder = source.split("Функция КомпактноеЗначение", 1)[1].split(
        "КонецФункции", 1
    )[0]

    assert (
        "ОпределитьКомпактнуюСхемуКолонок(Таблица, ТипыОбъектовWorker, "
        "МаксимумСтрок)" in serializer
    )
    assert "КомпактныйВидЗначения(СтрокаТаблицы" not in serializer
    assert "КомпактноеЗначение(" in serializer
    assert "СсылочныеКолонки[ИндексКолонки]" in serializer
    assert "ЭтоСсылка(" not in value_encoder


def test_worker_module_contains_only_user_worker_methods() -> None:
    source = WORKER_MODULE.read_text(encoding="utf-8-sig")

    assert "СобратьСсылки" not in source
    assert "ПолучитьПредставленияСсылок" not in source
    assert "СериализоватьТаблицу" not in source
    assert "ПолучитьЧасть" not in source
