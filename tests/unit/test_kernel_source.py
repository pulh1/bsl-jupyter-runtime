from pathlib import Path
from xml.etree import ElementTree

from onec_runtime import kernel
from onec_runtime.extension_bundle import read_extension_manifest
from onec_runtime.kernel import (
    KERNEL_OBJECT_ID,
    OBJECT_MODULE_PROPERTY_ID,
    ZUP_GENERAL_PURPOSE_OBJECT_ID,
    ZUP_SERVER_CAPTURE_B_LINE,
    ZUP_SERVER_CAPTURE_LINE,
    external_module_url,
    extension_breakpoint_line,
    service_breakpoint_line,
    zup_server_capture_location,
)


WORKSPACE = Path(__file__).parents[2]
WORKER_MODULE = WORKSPACE / "onec" / "Worker" / "Worker" / "Ext" / "ObjectModule.bsl"
KERNEL_XML = WORKSPACE / "onec" / "Kernel" / "Kernel.xml"
KERNEL_MODULE = WORKSPACE / "onec" / "Kernel" / "Kernel" / "Ext" / "ObjectModule.bsl"
SERVER_EXTENSION_MODULE = (
    WORKSPACE
    / "onec"
    / "OnecInteractiveRuntime"
    / "CommonModules"
    / "RuntimeKernelServer"
    / "Ext"
    / "Module.bsl"
)
TABLE_TRANSFER_EXTENSION_MODULE = (
    WORKSPACE
    / "onec"
    / "OnecInteractiveRuntime"
    / "CommonModules"
    / "RuntimeTableTransferServer"
    / "Ext"
    / "Module.bsl"
)
SERVER_EXTENSION_METADATA = (
    WORKSPACE
    / "onec"
    / "OnecInteractiveRuntime"
    / "CommonModules"
    / "RuntimeKernelServer.xml"
)
CONTEXT_EXTENSION_MODULE = (
    WORKSPACE
    / "onec"
    / "OnecInteractiveRuntime"
    / "CommonModules"
    / "RuntimeContextStoreServer"
    / "Ext"
    / "Module.bsl"
)
CONTEXT_EXTENSION_METADATA = (
    WORKSPACE
    / "onec"
    / "OnecInteractiveRuntime"
    / "CommonModules"
    / "RuntimeContextStoreServer.xml"
)
SERVER_EXTENSION_CONFIGURATION = WORKSPACE / "onec" / "OnecInteractiveRuntime" / "Configuration.xml"
MANAGED_EXTENSION_MODULE = (
    WORKSPACE / "onec" / "OnecInteractiveRuntime" / "Ext" / "ManagedApplicationModule.bsl"
)
MD_NS = "http://v8.1c.ru/8.3/MDClasses"


def test_kernel_metadata_has_stable_external_processor_identity() -> None:
    root = ElementTree.parse(KERNEL_XML).getroot()
    processor = root.find(f"{{{MD_NS}}}ExternalDataProcessor")

    assert processor is not None
    assert processor.attrib["uuid"] == "8fc91d24-20f5-4da4-8ff7-7a7c682f80f5"
    assert processor.findtext(f"{{{MD_NS}}}Properties/{{{MD_NS}}}Name") == "Kernel"
    child_objects = processor.find(f"{{{MD_NS}}}ChildObjects")
    assert child_objects is not None
    assert [child.text for child in child_objects] == ["Runtime"]
    assert KERNEL_OBJECT_ID == processor.attrib["uuid"]
    assert OBJECT_MODULE_PROPERTY_ID == "a637f77f-3840-441d-a1c3-699c8c5cb7e0"


def test_service_breakpoint_resolves_to_the_only_marker_line() -> None:
    lines = KERNEL_MODULE.read_text(encoding="utf-8").splitlines()

    line = service_breakpoint_line(KERNEL_MODULE)

    assert lines[line - 1].strip() == (
        "Если ИдентификаторКоманды > ЗавершеннаяКоманда Тогда // @runtime-service-breakpoint"
    )
    assert sum("@runtime-service-breakpoint" in item for item in lines) == 1


def test_external_module_url_is_an_absolute_percent_encoded_file_uri() -> None:
    epf = Path(r"C:\runtime build\Kernel.epf")

    assert external_module_url(epf) == "file://C:/runtime%20build/Kernel.epf"


def test_server_extension_exposes_debug_context_execution_method() -> None:
    source = SERVER_EXTENSION_MODULE.read_text(encoding="utf-8-sig")

    assert (
        "Функция ВыполнитьВКонтекстеОтладки(e1cRuntimeКонтекстОтладки, Код) Экспорт" in source
    )
    assert "Выполнить(Код);" in source
    assert "Возврат РезультатИнструкции;" in source
    assert not any(
        line.strip().startswith("Перем ") for line in source.splitlines()
    )


def test_capture_execution_wrappers_snapshot_bind_and_restore_worker_pin() -> None:
    source = SERVER_EXTENSION_MODULE.read_text(encoding="utf-8-sig")
    slot = '"RuntimeWorkerPinnedOperationGeneration"'

    for signature in (
        "Функция ВыполнитьКодТекущегоКонтекстаОтладки(Код) Экспорт",
        "Функция ВыполнитьКодВКонтекстеОтладки(e1cRuntimeКонтекст, Код) Экспорт",
    ):
        start = source.index(signature)
        end = source.index("КонецФункции", start)
        wrapper = source[start:end]
        snapshot = (
            f"e1cRuntimeКонтекст.Свойство({slot}, "
            "__OnecPinnedWorkerGenerationOriginal)"
        )
        bind = (
            "__OnecPinnedWorkerGeneration = "
            "__OnecPinnedWorkerGenerationOriginal;"
        )
        execute = "Выполнить(Код);"
        restore = (
            "ВосстановитьПинПоколенияWorker(e1cRuntimeКонтекст, "
            "__OnecPinnedWorkerGenerationSlotExists, "
            "__OnecPinnedWorkerGenerationOriginal);"
        )

        assert snapshot in wrapper
        assert bind in wrapper
        assert wrapper.index(snapshot) < wrapper.index(bind) < wrapper.index(execute)
        assert wrapper.count(restore) == 2
        assert wrapper.index("Исключение") < wrapper.index(restore)
        assert wrapper.rindex(restore) > wrapper.index("КонецПопытки")


def test_capture_worker_pin_restore_reinstates_or_removes_the_original_slot() -> None:
    source = SERVER_EXTENSION_MODULE.read_text(encoding="utf-8-sig")
    start = source.index(
        "Процедура ВосстановитьПинПоколенияWorker(e1cRuntimeКонтекст, "
        "СлотСуществовал, ИсходноеЗначение)"
    )
    end = source.index("КонецПроцедуры", start)
    restore = source[start:end]

    assert (
        'e1cRuntimeКонтекст.Вставить("RuntimeWorkerPinnedOperationGeneration", '
        "ИсходноеЗначение);"
    ) in restore
    assert (
        'e1cRuntimeКонтекст.Удалить("RuntimeWorkerPinnedOperationGeneration");'
        in restore
    )
    assert "Если СлотСуществовал Тогда" in restore


def test_extension_exposes_manifest_checked_worker_pin_install_and_clear() -> None:
    source = SERVER_EXTENSION_MODULE.read_text(encoding="utf-8-sig")

    assert (
        "Функция УстановитьПинПоколенияWorker(e1cRuntimeКонтекст, "
        "ОжидаемыйManifestSha256) Экспорт"
    ) in source
    assert (
        'e1cRuntimeКонтекст.Свойство("RuntimeWorkerActiveGeneration", '
        "АктивноеПоколениеWorker)"
    ) in source
    assert (
        "АктивноеПоколениеWorker.ManifestSha256 <> "
        "ОжидаемыйManifestSha256"
    ) in source
    assert (
        'e1cRuntimeКонтекст.Вставить("RuntimeWorkerPinnedOperationGeneration", '
        "АктивноеПоколениеWorker);"
    ) in source
    assert "Функция ОчиститьПинПоколенияWorker(e1cRuntimeКонтекст) Экспорт" in source
    assert 'e1cRuntimeКонтекст.Удалить("RuntimeWorkerPinnedOperationGeneration");' in source


def test_server_extension_batches_reference_presentations() -> None:
    source = TABLE_TRANSFER_EXTENSION_MODULE.read_text(encoding="utf-8-sig")

    assert "Функция ПолучитьПредставленияСсылок(МассивСсылок) Экспорт" in source
    assert "Для Каждого Ссылка Из МассивСсылок Цикл" in source
    assert ".УникальныйИдентификатор()" in source
    assert "Строка(Ссылка)" in source
    assert "Возврат Результат;" in source


def test_extension_serializes_table_and_keeps_transfer_state_outside_module() -> None:
    source = TABLE_TRANSFER_EXTENSION_MODULE.read_text(encoding="utf-8-sig")

    assert (
        "Функция СериализоватьТаблицу(Знач Таблица, РазмерЧасти, ПредставленияСсылок) Экспорт"
        in source
    )
    assert "Функция СобратьСсылки(Знач Таблица) Экспорт" in source
    assert "Функция ПолучитьЧасть(Части, НомерЧасти) Экспорт" in source
    assert "УникальныйИдентификатор()" in source
    assert "ЗаписьJSON" in source
    assert "Base64Строка" in source
    assert "SHA256" in source
    assert "RuntimeTableTransfers" not in source


def test_table_transfer_extension_hashes_binary_data_with_compatible_platform_api() -> None:
    source = TABLE_TRANSFER_EXTENSION_MODULE.read_text(encoding="utf-8-sig")

    assert "ПолучитьHexСтрокуИзДвоичныхДанных(Хеширование.ХешСумма)" in source
    assert "ХешСумма.ПолучитьБуферДвоичныхДанных()" not in source


def test_extension_emits_canonical_base64_chunks_without_line_breaks() -> None:
    source = TABLE_TRANSFER_EXTENSION_MODULE.read_text(encoding="utf-8-sig")

    assert "Функция Base64БезРазрывов(Данные)" in source
    assert 'СтрЗаменить(Результат, Символы.ВК, "")' in source
    assert 'СтрЗаменить(Результат, Символы.ПС, "")' in source
    assert "Части.Добавить(Base64БезРазрывов(" in source


def test_extension_uses_bom_free_utf8_for_payload_schema_and_chunks() -> None:
    source = TABLE_TRANSFER_EXTENSION_MODULE.read_text(encoding="utf-8-sig")

    assert source.count("КодировкаТекста.UTF8") == source.count(
        "КодировкаТекста.UTF8, Ложь"
    )
    assert "КодировкаТекста.UTF8);" not in source


def test_extension_hashes_schema_record_without_jsonl_delimiter() -> None:
    source = TABLE_TRANSFER_EXTENSION_MODULE.read_text(encoding="utf-8-sig")

    assert "JSONСхемы = ВJSON(Схема);" in source
    assert "СтрокаСхемы = JSONСхемы + Символы.ПС;" in source
    assert (
        "ПолучитьДвоичныеДанныеИзСтроки(JSONСхемы, "
        "КодировкаТекста.UTF8, Ложь)"
    ) in source


def test_extension_writes_each_jsonl_record_without_internal_line_breaks() -> None:
    source = TABLE_TRANSFER_EXTENSION_MODULE.read_text(encoding="utf-8-sig")

    assert (
        "ЗаписьJSON.УстановитьСтроку(Новый "
        "ПараметрыЗаписиJSON(ПереносСтрокJSON.Нет));"
    ) in source
    assert "ЗаписьJSON.УстановитьСтроку();" not in source


def test_extension_bounds_utf8_chunks_by_worst_case_code_point_width() -> None:
    source = TABLE_TRANSFER_EXTENSION_MODULE.read_text(encoding="utf-8-sig")

    assert "РазмерЧастиВСимволах = Макс(1, Цел(РазмерЧасти / 4));" in source
    assert "Сред(ТекстДанных, Позиция, РазмерЧастиВСимволах)" in source
    assert "Позиция = Позиция + РазмерЧастиВСимволах;" in source


def test_server_runtime_is_bootstrapped_from_managed_application() -> None:
    managed_source = MANAGED_EXTENSION_MODULE.read_text(encoding="utf-8-sig")

    assert "Перем ПродолжатьЦикл;" not in managed_source
    assert '&После("ПриНачалеРаботыСистемы")' in managed_source
    assert "Процедура OnecInteractiveRuntime_ПриНачалеРаботыСистемы()" in managed_source
    assert "ПродолжатьЦикл = Ложь;" in managed_source
    assert "С = 1; // @runtime-extension-service-breakpoint" in managed_source
    assert "Если ПродолжатьЦикл Тогда" in managed_source
    assert "RuntimeKernelServer.Запустить();" in managed_source
    assert "Пока Истина Цикл" not in managed_source
    startup_line = extension_breakpoint_line(MANAGED_EXTENSION_MODULE)
    assert "@runtime-extension-service-breakpoint" in managed_source.splitlines()[
        startup_line - 1
    ]


def test_server_kernel_does_not_cache_return_values() -> None:
    root = ElementTree.parse(SERVER_EXTENSION_METADATA).getroot()
    common_module = root.find(f"{{{MD_NS}}}CommonModule")

    assert common_module is not None
    properties = common_module.find(f"{{{MD_NS}}}Properties")
    assert properties is not None
    assert properties.findtext(f"{{{MD_NS}}}ReturnValuesReuse") == "DontUse"

    source = SERVER_EXTENSION_MODULE.read_text(encoding="utf-8-sig")
    assert "Функция ПолучитьКонтекст() Экспорт" not in source
    assert source.count("RuntimeContextStoreServer.ПолучитьКонтекст()") >= 3
    assert "Пока Истина Цикл" in source


def test_server_kernel_has_executable_entry_before_tight_loop() -> None:
    from onec_runtime.kernel import server_extension_entry_breakpoint_line

    source = SERVER_EXTENSION_MODULE.read_text(encoding="utf-8-sig")
    lines = source.splitlines()
    entry_line = server_extension_entry_breakpoint_line(SERVER_EXTENSION_MODULE)

    assert (
        "e1cRuntimeКонтекст = Новый Структура; "
        "// @runtime-server-extension-entry-breakpoint"
    ) in lines[
        entry_line - 1
    ]
    loop_method = source.index("Процедура Запустить() Экспорт")
    entry_marker = source.index("@runtime-server-extension-entry-breakpoint")
    service_line = kernel.server_extension_breakpoint_line(SERVER_EXTENSION_MODULE)
    assert loop_method < entry_marker
    assert entry_line < service_line
    assert lines[service_line - 1].strip() == (
        "С = 1; // @runtime-server-extension-service-breakpoint"
    )


def test_server_kernel_rebinds_evicted_context_from_live_kernel_frame() -> None:
    source = SERVER_EXTENSION_MODULE.read_text(encoding="utf-8-sig")

    assert "Функция НачатьКонтекстОтладки(e1cRuntimeКонтекстОтладки) Экспорт" in source
    assert (
        "Функция ВосстановитьКонтекстВыполнения(КонтекстВыполнения) Экспорт"
    ) in source
    assert 'КонтекстВыполнения.Вставить("__onec_runtime_context_id",' in source
    assert "СнимокКонтекста = Новый Структура;" in source
    assert "Для Каждого ЭлементКонтекста Из КонтекстВыполнения Цикл" in source
    assert "СнимокКонтекста.Вставить(" in source
    assert "e1cRuntimeКонтекст.Очистить();" in source
    assert "Для Каждого ЭлементКонтекста Из СнимокКонтекста Цикл" in source
    assert 'Если Не e1cRuntimeКонтекст.Свойство("__onec_runtime_context_id"' not in source
    assert "e1cRuntimeКонтекст = СинхронизироватьКонтекст(e1cRuntimeКонтекст);" not in source


def test_server_kernel_owns_context_and_capture_uses_temporary_transfer() -> None:
    source = SERVER_EXTENSION_MODULE.read_text(encoding="utf-8-sig")

    assert (
        "e1cRuntimeКонтекст = Новый Структура; "
        "// @runtime-server-extension-entry-breakpoint"
    ) in source
    assert "Функция НачатьКонтекстОтладкиВКонтексте(" in source
    assert "ПолучитьИзВременногоХранилища(АдресКонтекстаОтладки)" in source
    assert "Функция ВыполнитьКодВКонтекстеОтладки(e1cRuntimeКонтекст, Код) Экспорт" in source
    assert "Функция ПоместитьЗначениеКонтекстаОтладки(e1cRuntimeКонтекст, Имя) Экспорт" in source
    assert "ПоместитьВоВременноеХранилище(Значение)" in source
    assert (
        "Функция ЗавершитьКонтекстОтладкиВКонтексте(e1cRuntimeКонтекст) Экспорт"
    ) in source


def test_capture_rebind_helper_does_not_shift_proven_rdbg_coordinates() -> None:
    source = SERVER_EXTENSION_MODULE.read_text(encoding="utf-8-sig")
    capture_a, capture_b = kernel.synthetic_capture_locations(SERVER_EXTENSION_MODULE)
    entry_line = kernel.server_extension_entry_breakpoint_line(
        SERVER_EXTENSION_MODULE
    )
    service_line = kernel.server_extension_breakpoint_line(
        SERVER_EXTENSION_MODULE
    )

    manifest = read_extension_manifest(
        WORKSPACE
        / "src"
        / "onec_runtime"
        / "resources"
        / "extension"
        / "extension-manifest.json"
    )
    assert (entry_line, service_line) == (
        manifest.breakpoints.server_entry.line,
        manifest.breakpoints.server_service.line,
    )
    assert (capture_a.line, capture_b.line) == (96, 99)
    assert source.index("Функция ВосстановитьКонтекстВыполнения(") > source.index(
        "@runtime-server-extension-service-breakpoint"
    )
    assert source.index("Процедура ПерепривязатьКонтекстВыполнения(") > source.index(
        "@runtime-server-extension-service-breakpoint"
    )


def test_message_helpers_follow_platform_compatible_bootstrap_prefix() -> None:
    source = SERVER_EXTENSION_MODULE.read_text(encoding="utf-8-sig")
    service_line = kernel.server_extension_breakpoint_line(SERVER_EXTENSION_MODULE)

    assert service_line <= 127
    assert source.index("@runtime-server-extension-service-breakpoint") < source.index(
        "Процедура ДобавитьСообщение("
    )
    assert 'e1cRuntimeКонтекст.Вставить("__onec_cell_messages_result_key", Ключ);' in source
    assert 'e1cRuntimeКонтекст.Вставить("__onec_cell_messages_result", Сообщения);' in source


def test_context_store_uses_session_cache_for_cross_request_debug_evaluation() -> None:
    root = ElementTree.parse(CONTEXT_EXTENSION_METADATA).getroot()
    common_module = root.find(f"{{{MD_NS}}}CommonModule")

    assert common_module is not None
    properties = common_module.find(f"{{{MD_NS}}}Properties")
    assert properties is not None
    assert properties.findtext(f"{{{MD_NS}}}Server") == "true"
    assert properties.findtext(f"{{{MD_NS}}}ServerCall") == "false"
    assert properties.findtext(f"{{{MD_NS}}}ExternalConnection") == "true"
    assert properties.findtext(f"{{{MD_NS}}}ClientOrdinaryApplication") == "true"
    assert properties.findtext(f"{{{MD_NS}}}ReturnValuesReuse") == "DuringRequest"

    source = CONTEXT_EXTENSION_MODULE.read_text(encoding="utf-8-sig")
    declarations = [
        line.strip()
        for line in source.splitlines()
        if line.strip().startswith(("Функция ", "Процедура "))
    ]
    assert declarations == ["Функция ПолучитьКонтекст() Экспорт"]
    assert "Возврат Новый Структура;" in source


def test_server_extension_persists_and_releases_capture_structure() -> None:
    source = SERVER_EXTENSION_MODULE.read_text(encoding="utf-8-sig")

    assert "Функция НачатьКонтекстОтладки(e1cRuntimeКонтекстОтладки) Экспорт" in source
    assert (
        "Функция ВосстановитьКонтекстВыполнения(КонтекстВыполнения) Экспорт"
    ) in source
    assert "Функция ВыполнитьКодТекущегоКонтекстаОтладки(Код) Экспорт" in source
    assert "Функция ПолучитьЗначениеКонтекстаОтладки(Имя) Экспорт" in source
    assert "Функция ЗавершитьКонтекстОтладки() Экспорт" in source
    assert "\tВозврат Истина;\nКонецФункции" in source
    assert source.count("e1cRuntimeКонтекстОтладки = Неопределено;") >= 2
    assert "РезультатКонтекста = Неопределено;" in source
    assert "Значение = Неопределено;" in source


def test_runtime_sources_do_not_ship_the_disproven_extension_installer() -> None:
    installer_root = WORKSPACE / "onec" / "ExtensionInstaller"
    assert not any(path.is_file() for path in installer_root.rglob("*"))


def test_synthetic_capture_markers_resolve_to_executable_lines() -> None:
    first, second = kernel.synthetic_capture_locations(SERVER_EXTENSION_MODULE)
    lines = SERVER_EXTENSION_MODULE.read_text(encoding="utf-8-sig").splitlines()

    assert first.line < second.line
    assert first.object_id == second.object_id
    assert "Если Результат.Количество() = 0 Тогда" in lines[first.line - 1]
    assert "Результат.Добавить(Скаляр);" in lines[first.line]
    assert "Если Результат.Количество() = 1 Тогда" in lines[second.line - 1]
    assert "Скаляр = Скаляр + 1;" in lines[second.line]


def test_reentrancy_fixture_markers_are_unique_and_executable() -> None:
    shielded = kernel.shielded_capture_location(SERVER_EXTENSION_MODULE)
    user = kernel.user_breakpoint_location(SERVER_EXTENSION_MODULE)
    lines = SERVER_EXTENSION_MODULE.read_text(encoding="utf-8-sig").splitlines()
    source = "\n".join(lines)

    assert shielded.line != user.line
    assert "@runtime-shielded-nested-capture" in lines[shielded.line - 1]
    assert "@runtime-user-breakpoint" in lines[user.line - 1]
    assert "Функция ВызовПодCaptureShield(Значение) Экспорт" in source
    assert "Функция ВызовСОбычнойТочкой(Значение) Экспорт" in source


def test_server_extension_uses_current_extension_mode_without_host_properties() -> None:
    root = ElementTree.parse(SERVER_EXTENSION_CONFIGURATION).getroot()
    properties = root.find(f"{{{MD_NS}}}Configuration/{{{MD_NS}}}Properties")

    assert properties is not None
    assert properties.findtext(
        f"{{{MD_NS}}}ConfigurationExtensionCompatibilityMode"
    ) == "Version8_3_27"
    assert properties.find(f"{{{MD_NS}}}InterfaceCompatibilityMode") is None
    assert properties.find(f"{{{MD_NS}}}DefaultLanguage") is None


def test_zup_server_capture_location_matches_source_dump_identity() -> None:
    location = zup_server_capture_location()

    assert ZUP_GENERAL_PURPOSE_OBJECT_ID == "75e77f99-b9d1-4bc0-93aa-c77a93d760d3"
    assert ZUP_SERVER_CAPTURE_LINE == 875
    assert location.module_type == "ConfigModule"
    assert location.url == ""
    assert str(location.object_id) == ZUP_GENERAL_PURPOSE_OBJECT_ID
    assert str(location.property_id) == "d5963243-262e-4398-b4d7-fb16d06484f6"
    assert location.line == ZUP_SERVER_CAPTURE_LINE
    assert location.extension_name == ""


def test_zup_server_capture_location_supports_second_capture_line() -> None:
    location = zup_server_capture_location(line=ZUP_SERVER_CAPTURE_B_LINE)

    assert ZUP_SERVER_CAPTURE_B_LINE == 877
    assert location.line == 877
    assert location.object_id == zup_server_capture_location().object_id
