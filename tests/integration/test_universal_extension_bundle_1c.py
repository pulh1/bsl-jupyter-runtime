from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterator
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import psutil
import pytest

import onec_runtime.session as session_module
from onec_runtime.config import RuntimeConfig
from onec_runtime.configurator_agent import prepare_extension
from onec_runtime.errors import ExtensionHandshakeError, ExtensionIdentityConflict
from onec_runtime.extension_bundle import (
    EXTENSION_NAME,
    ExtensionBundle,
    fingerprint_extension_dump,
    packaged_extension_bundle,
)
from onec_runtime.extension_state import ExtensionStateStore, VerifiedExtensionState
from onec_runtime.performance_profile import PhaseRecorder
from onec_runtime.processes import FileModeProcesses, OwnedProcess
from onec_runtime.runtime_models import RuntimeReplyKind
from onec_runtime.session import ExtensionMode, RuntimeSession, RuntimeSessionConfig
from onec_runtime.toolchain import (
    apply_product_extension,
    create_empty_infobase,
    create_file_infobase_at,
    dump_product_extension_cfe,
    dump_target_extension_cfe,
    dump_target_extension_files,
    load_product_extension_source,
    load_target_extension_cfe,
)
from tools.build_runtime_extension_bundle import build_runtime_extension_bundle

pytestmark = pytest.mark.integration

_EXPECTED_PLATFORM = Path(r"C:\Program Files\1cv8\8.3.27.2170\bin")
_REPOSITORY = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class _OwnedProcessIdentity:
    pid: int
    create_time: float
    role: str


class _OwnedProcessTracker:
    def __init__(self) -> None:
        self.identities: list[_OwnedProcessIdentity] = []

    def record(self, pid: int, *, role: str) -> None:
        process = psutil.Process(pid)
        self.identities.append(_OwnedProcessIdentity(pid, process.create_time(), role))

    def assert_all_gone(self) -> None:
        survivors: list[_OwnedProcessIdentity] = []
        for identity in self.identities:
            try:
                process = psutil.Process(identity.pid)
                if process.create_time() == identity.create_time:
                    survivors.append(identity)
            except psutil.NoSuchProcess:
                continue
        assert survivors == [], "test-owned 1C processes remain alive: " + ", ".join(
            f"{identity.role}:{identity.pid}" for identity in survivors
        )

    def assert_runtime_pair_captured_and_gone(self) -> None:
        assert {identity.role for identity in self.identities} >= {"dbgs", "1cv8c"}
        self.assert_all_gone()


@pytest.fixture(autouse=True)
def _track_owned_runtime_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[_OwnedProcessTracker]:
    tracker = _OwnedProcessTracker()

    class TrackedFileModeProcesses(FileModeProcesses):
        def start_debug_server(self, timeout_s: float = 30.0) -> int:
            port = super().start_debug_server(timeout_s)
            assert self.debug_server is not None
            tracker.record(self.debug_server.pid, role="dbgs")
            return port

        def start_debuggee(
            self,
            debug_port: int,
            *,
            execute_external: bool = True,
            thick_client: bool = False,
            startup_parameter: str | None = None,
        ) -> OwnedProcess:
            process = super().start_debuggee(
                debug_port,
                execute_external=execute_external,
                thick_client=thick_client,
                startup_parameter=startup_parameter,
            )
            tracker.record(process.pid, role="1cv8c")
            return process

    monkeypatch.setattr(session_module, "FileModeProcesses", TrackedFileModeProcesses)
    yield tracker
    tracker.assert_all_gone()


def _platform_bin() -> Path:
    if os.environ.get("ONEC_RUN_EXTENSION_BUNDLE_INTEGRATION") != "1":
        pytest.skip(
            "set ONEC_RUN_EXTENSION_BUNDLE_INTEGRATION=1 for live 1C acceptance"
        )
    configured = os.environ.get("ONEC_PLATFORM_BIN", "").strip()
    platform = (
        Path(configured).resolve() if configured else _EXPECTED_PLATFORM.resolve()
    )
    if platform.parent.name not in {"8.3.27.2170", "8.5.1.1529"}:
        pytest.fail("live bundle acceptance requires 1C 8.3.27.2170 or 8.5.1.1529")
    for executable in ("1cv8.exe", "1cv8c.exe", "dbgs.exe"):
        if not (platform / executable).is_file():
            pytest.fail(f"required platform executable is missing: {executable}")
    return platform


def _config(
    root: Path, platform: Path, *, infobase: Path | None = None, username: str = ""
) -> RuntimeConfig:
    return RuntimeConfig(
        root,
        platform,
        connection_string=f'File="{infobase}";' if infobase is not None else None,
        username=username,
    )


def _phase_names(profiler: PhaseRecorder) -> list[str]:
    return [event.phase for event in profiler.events]


def _assert_no_owned_1c_process(
    config: RuntimeConfig,
    tracker: _OwnedProcessTracker | None = None,
) -> None:
    target = str(config.infobase_dir.resolve()).casefold()
    for process in psutil.process_iter(("name", "cmdline")):
        try:
            name = (process.info["name"] or "").casefold()
            command = " ".join(process.info["cmdline"] or ()).casefold()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
        if name in {"1cv8.exe", "1cv8c.exe", "dbgs.exe"} and target in command:
            pytest.fail(f"owned 1C process remains alive: {process.pid}")
    if tracker is not None:
        tracker.assert_runtime_pair_captured_and_gone()


def _marker(config: RuntimeConfig) -> ExtensionStateStore:
    return ExtensionStateStore(
        config.runtime_dir / "extension-state",
        config.infobase_dir,
    )


def _write_matching_marker(config: RuntimeConfig, bundle: ExtensionBundle) -> None:
    manifest = bundle.manifest
    _marker(config).write(
        VerifiedExtensionState(
            infobase_path=str(config.infobase_dir.resolve()),
            product_id=manifest.product_id,
            cfe_sha256=manifest.cfe_sha256,
            manifest_schema_version=manifest.schema_version,
            artifact_version=manifest.artifact_version,
            protocol_version=manifest.protocol_version,
            identity_sha256=manifest.fingerprints.identity_sha256,
        )
    )


def _start(
    config: RuntimeConfig, evidence: Path
) -> tuple[RuntimeSession, PhaseRecorder]:
    profiler = PhaseRecorder()
    session = RuntimeSession.start(
        RuntimeSessionConfig(config, evidence, startup_profiler=profiler)
    )
    return session, profiler


def _assert_exact_installed(
    config: RuntimeConfig, bundle: ExtensionBundle, root: Path
) -> None:
    dump = root / "installed-exact"
    dump_target_extension_files(config, dump, root / "installed-exact.log")
    assert fingerprint_extension_dump(dump) == bundle.manifest.fingerprints


def _exercise_first_and_fast(
    config: RuntimeConfig,
    root: Path,
    tracker: _OwnedProcessTracker,
) -> None:
    bundle = packaged_extension_bundle(config.runtime_dir)
    assert _marker(config).read() is None
    first, first_profile = _start(config, root / "evidence-first")
    try:
        properties = first.execute_bsl('''
РасширениеRuntime = РасширенияКонфигурации.Получить(
    Новый Структура("Имя", "OnecInteractiveRuntime"))[0];
Результат = РасширениеRuntime.БезопасныйРежим;''')
        assert properties.succeeded and properties.result is False
        method = first.execute_bsl('''
Функция ПроверкаУстановки(Значение) Экспорт
    Возврат Значение + 1;
КонецФункции
Результат = ПроверкаУстановки(41);''')
        assert method.succeeded and method.result == 42
    finally:
        first.close()
    assert _marker(config).matches(bundle.manifest)
    assert _phase_names(first_profile) == [
        "extension.bundle",
        "extension.inspect",
        "extension.install",
        "extension.apply",
        "extension.inspect",
        "extension.safe_mode",
        "extension.handshake",
    ]

    second, second_profile = _start(config, root / "evidence-second")
    second.close()
    assert _phase_names(second_profile) == ["extension.bundle", "extension.handshake"]
    _assert_exact_installed(config, bundle, root)
    _assert_no_owned_1c_process(config, tracker)


def _copy_source(root: Path) -> Path:
    destination = root / "onec" / EXTENSION_NAME
    destination.parent.mkdir(parents=True)
    shutil.copytree(_REPOSITORY / "onec" / EXTENSION_NAME, destination)
    return destination


def _replace_exact(path: Path, old: str, new: str, *, count: int) -> None:
    text = path.read_text(encoding="utf-8-sig")
    assert text.count(old) == count
    path.write_text(text.replace(old, new), encoding="utf-8", newline="\n")


def _replace_exact_in_xml_tree(root: Path, old: str, new: str, *, count: int) -> None:
    matches: list[tuple[Path, int]] = []
    for path in sorted(root.rglob("*.xml")):
        occurrences = path.read_text(encoding="utf-8-sig").count(old)
        if occurrences:
            matches.append((path, occurrences))
    assert sum(occurrences for _, occurrences in matches) == count
    for path, occurrences in matches:
        _replace_exact(path, old, new, count=occurrences)


def _build_variant(
    root: Path,
    platform: Path,
    *,
    artifact_version: str | None = None,
    protocol_version: str | None = None,
) -> ExtensionBundle:
    packaged = packaged_extension_bundle(root / "packaged").manifest
    artifact_version = artifact_version or packaged.artifact_version
    protocol_version = protocol_version or packaged.protocol_version
    source = _copy_source(root)
    if artifact_version != packaged.artifact_version:
        _replace_exact(
            source / "Configuration.xml",
            f"<Version>{packaged.artifact_version}</Version>",
            f"<Version>{artifact_version}</Version>",
            count=1,
        )
        for relative in (
            Path("Ext/ManagedApplicationModule.bsl"),
            Path("CommonModules/RuntimeKernelServer/Ext/Module.bsl"),
        ):
            _replace_exact(
                source / relative,
                f'ВерсияАртефактаRuntime = "{packaged.artifact_version}";',
                f'ВерсияАртефактаRuntime = "{artifact_version}";',
                count=1,
            )
    if protocol_version != packaged.protocol_version:
        for relative in (
            Path("Ext/ManagedApplicationModule.bsl"),
            Path("CommonModules/RuntimeKernelServer/Ext/Module.bsl"),
        ):
            _replace_exact(
                source / relative,
                f'ВерсияПротоколаRuntime = "{packaged.protocol_version}";',
                f'ВерсияПротоколаRuntime = "{protocol_version}";',
                count=1,
            )
    return build_runtime_extension_bundle(
        source_root=source,
        output_root=root / "bundle",
        platform_bin=platform,
        artifact_version=artifact_version,
        protocol_version=protocol_version,
    )


def _build_table_bound_instrumented_bundle(
    root: Path,
    platform: Path,
    *,
    move_serializer_guard_after_read: bool,
):  # type: ignore[no-untyped-def]
    """Build an exact-source CFE with probes after each bounded BSL cell read."""
    source = _copy_source(root)
    module = source / "CommonModules" / "RuntimeTableTransferServer" / "Ext" / "Module.bsl"
    text = module.read_text(encoding="utf-8-sig")
    classifier_start = text.index("Функция ОпределитьКомпактнуюСхемуКолонок")
    before_classifier, classifier = text[:classifier_start], text[classifier_start:]
    serializer_read = "\t\t\tЗначениеЯчейки = СтрокаТаблицы[Колонка.Имя];"
    serializer_probe = serializer_read + (
        "\n\t\t\tЕсли ЗначениеЯчейки = \"__table_bound_sentinel__\" Тогда"
        "\n\t\t\t\tВызватьИсключение \"out_of_page_serializer_cell_read\";"
        "\n\t\t\tКонецЕсли;"
    )
    assert before_classifier.count(serializer_read) == 1
    before_classifier = before_classifier.replace(serializer_read, serializer_probe, 1)
    classifier_read = "\t\t\tЗначениеЯчейки = СтрокаТаблицы[Колонка.Имя];"
    classifier_probe = classifier_read + (
        "\n\t\t\tЕсли ЗначениеЯчейки = \"__table_bound_sentinel__\" Тогда"
        "\n\t\t\t\tВызватьИсключение \"out_of_page_classifier_cell_read\";"
        "\n\t\t\tКонецЕсли;"
    )
    assert classifier.count(classifier_read) == 1
    classifier = classifier.replace(classifier_read, classifier_probe, 1)
    query_read = (
        "\t\t\tСтрокаРезультата[КолонкаРезультата.Имя]"
        "\n\t\t\t\t= ВыборкаДанных[КолонкаРезультата.Имя];"
    )
    query_probe = (
        "\t\t\tЕсли ВыборкаДанных[КолонкаРезультата.Имя]"
        " = \"__table_bound_sentinel__\" Тогда"
        "\n\t\t\t\tВызватьИсключение \"out_of_page_query_cell_read\";"
        "\n\t\t\tКонецЕсли;\n"
        + query_read
    )
    instrumented = before_classifier + classifier
    if move_serializer_guard_after_read:
        serializer_guard = (
            "\t\tЕсли МаксимумСтрок > 0 И КоличествоСтрокJSONL >= МаксимумСтрок Тогда\n"
            "\t\t\tВызватьИсключение \"Превышен лимит строк компактной таблицы\";\n"
            "\t\tКонецЕсли;\n"
        )
        moved_guard = (
            "\n\t\t\tЕсли МаксимумСтрок > 0 И КоличествоСтрокJSONL >= МаксимумСтрок Тогда\n"
            "\t\t\t\tВызватьИсключение \"Превышен лимит строк компактной таблицы\";\n"
            "\t\t\tКонецЕсли;"
        )
        assert instrumented.count(serializer_guard) == 1
        instrumented = instrumented.replace(serializer_guard, "", 1)
        assert instrumented.count(serializer_probe) == 1
        instrumented = instrumented.replace(
            serializer_probe, serializer_probe + moved_guard, 1
        )
    assert instrumented.count(query_read) == 1
    module.write_text(
        instrumented.replace(query_read, query_probe, 1),
        encoding="utf-8",
        newline="\n",
    )
    packaged = packaged_extension_bundle(root / "packaged").manifest
    return build_runtime_extension_bundle(
        source_root=source,
        output_root=root / "bundle",
        platform_bin=platform,
        artifact_version=packaged.artifact_version,
        protocol_version=packaged.protocol_version,
    )


def _install_cfe(config: RuntimeConfig, cfe: Path, root: Path) -> None:
    load_target_extension_cfe(config, cfe, root / "load.log")
    apply_product_extension(config, root / "apply.log")
    prepare_extension(config, root / "safe-mode")


@pytest.mark.live_1c
@pytest.mark.parametrize(
    ("move_serializer_guard_after_read", "expected"),
    ((False, "bounded|exact|bounded"), (True, "serializer_probe|exact|bounded")),
    ids=("bounded", "guard_after_read_is_detected"),
)
def test_compact_table_bound_is_executed_before_value_table_and_query_sentinel_cells(
    tmp_path: Path,
    _track_owned_runtime_processes: _OwnedProcessTracker,
    move_serializer_guard_after_read: bool,
    expected: str,
) -> None:
    """Opt-in live qualification of the instrumented product BSL CFE, not a unit model."""
    platform = _platform_bin()
    bundle = _build_table_bound_instrumented_bundle(
        tmp_path / "instrumented",
        platform,
        move_serializer_guard_after_read=move_serializer_guard_after_read,
    )
    config = _config(tmp_path / "target", platform)
    create_empty_infobase(config)
    _install_cfe(config, bundle.cfe_path, tmp_path / "install")
    session = RuntimeSession.start(RuntimeSessionConfig(
        config, tmp_path / "evidence", extension_mode=ExtensionMode.MANUAL
    ))
    try:
        reply = session.execute_bsl('''
Таблица = Новый ТаблицаЗначений;
Таблица.Колонки.Добавить("Значение", Новый ОписаниеТипов("Строка"));
Для Номер = 0 По 9999 Цикл
    СтрокаТаблицы = Таблица.Добавить();
    СтрокаТаблицы.Значение = ?(Номер = 3, "__table_bound_sentinel__", "safe");
КонецЦикла;

ПроверкаТаблицыЗначений = "";
Попытка
    Материализация = RuntimeTableTransferServer.СериализоватьКомпактнуюТаблицу(
        Таблица, "presentation", Новый Соответствие, Новый Массив, 3, 1000000);
    ПроверкаТаблицыЗначений = ?(Материализация.Доступ, "unexpected_success", "denied");
Исключение
    ОписаниеОшибки = ИнформацияОбОшибке().Описание;
    Если ОписаниеОшибки = "Превышен лимит строк компактной таблицы" Тогда
        ПроверкаТаблицыЗначений = "bounded";
    ИначеЕсли ОписаниеОшибки = "out_of_page_serializer_cell_read" Тогда
        ПроверкаТаблицыЗначений = "serializer_probe";
    Иначе
        ПроверкаТаблицыЗначений = "failed";
    КонецЕсли;
КонецПопытки;

Запрос = Новый Запрос;
Запрос.Текст = "ВЫБРАТЬ
|    \"\"safe\"\" КАК Значение
|ОБЪЕДИНИТЬ ВСЕ
|ВЫБРАТЬ
|    \"\"safe\"\" КАК Значение
|ОБЪЕДИНИТЬ ВСЕ
|ВЫБРАТЬ
|    \"\"safe\"\" КАК Значение";
ПроверкаТочногоЛимита = "";
Попытка
    ТочныйРезультат = RuntimeTableTransferServer.СериализоватьКомпактнуюТаблицу(
        Запрос.Выполнить(), "presentation", Новый Соответствие, Новый Массив, 3, 1000000);
    ПроверкаТочногоЛимита = ?(ТочныйРезультат.Доступ, "exact", "denied");
Исключение
    ПроверкаТочногоЛимита = "failed";
КонецПопытки;

Запрос.Текст = "ВЫБРАТЬ
|    1 КАК НомерСтроки,
|    \"\"safe\"\" КАК Значение
|ОБЪЕДИНИТЬ ВСЕ
|ВЫБРАТЬ
|    2 КАК НомерСтроки,
|    \"\"safe\"\" КАК Значение
|ОБЪЕДИНИТЬ ВСЕ
|ВЫБРАТЬ
|    3 КАК НомерСтроки,
|    \"\"safe\"\" КАК Значение
|ОБЪЕДИНИТЬ ВСЕ
|ВЫБРАТЬ
|    4 КАК НомерСтроки,
|    \"\"__table_bound_sentinel__\"\" КАК Значение
|УПОРЯДОЧИТЬ ПО НомерСтроки";
ПроверкаЗапроса = "";
Попытка
    МатериализацияЗапроса = RuntimeTableTransferServer.СериализоватьКомпактнуюТаблицу(
        Запрос.Выполнить(), "presentation", Новый Соответствие, Новый Массив, 3, 1000000);
    ПроверкаЗапроса = ?(МатериализацияЗапроса.Доступ, "unexpected_success", "denied");
Исключение
    ОписаниеОшибки = ИнформацияОбОшибке().Описание;
    Если ОписаниеОшибки = "Превышен лимит строк компактной таблицы" Тогда
        ПроверкаЗапроса = "bounded";
    ИначеЕсли ОписаниеОшибки = "out_of_page_query_cell_read" Тогда
        ПроверкаЗапроса = "query_probe";
    ИначеЕсли ОписаниеОшибки = "out_of_page_classifier_cell_read" Тогда
        ПроверкаЗапроса = "classifier_probe";
    ИначеЕсли ОписаниеОшибки = "out_of_page_serializer_cell_read" Тогда
        ПроверкаЗапроса = "serializer_probe";
    Иначе
        ПроверкаЗапроса = "failed";
    КонецЕсли;
КонецПопытки;
Результат = ПроверкаТаблицыЗначений + "|" + ПроверкаТочногоЛимита + "|" + ПроверкаЗапроса;''')
    finally:
        session.close()

    assert reply.succeeded
    assert reply.result == expected
    _assert_no_owned_1c_process(config, _track_owned_runtime_processes)


@pytest.mark.live_1c
def test_unbounded_to_df_keeps_all_rows_after_schema_probe(
    tmp_path: Path,
    _track_owned_runtime_processes: _OwnedProcessTracker,
) -> None:
    """A two-row ValueTable must not inherit the schema probe's one-row limit."""
    platform = _platform_bin()
    packaged = packaged_extension_bundle(tmp_path / "packaged").manifest
    bundle = build_runtime_extension_bundle(
        source_root=_REPOSITORY / "onec" / "OnecInteractiveRuntime",
        output_root=tmp_path / "bundle",
        platform_bin=platform,
        artifact_version=packaged.artifact_version,
        protocol_version=packaged.protocol_version,
    )
    config = _config(tmp_path / "target", platform)
    create_empty_infobase(config)
    _install_cfe(config, bundle.cfe_path, tmp_path / "install")
    session = RuntimeSession.start(RuntimeSessionConfig(
        config, tmp_path / "evidence", extension_mode=ExtensionMode.MANUAL
    ))
    try:
        reply = session.execute_bsl('''
ТаблицаДляPython = Новый ТаблицаЗначений;
ТаблицаДляPython.Колонки.Добавить("Значение", Новый ОписаниеТипов("Строка"));
ТаблицаДляPython.Добавить().Значение = "first";
ТаблицаДляPython.Добавить().Значение = "second";
''')
        assert reply.succeeded
        frame = session.to_df("e1cRuntimeКонтекст.ТаблицаДляPython")
        assert frame["Значение"].tolist() == ["first", "second"]
    finally:
        session.close()

    _assert_no_owned_1c_process(config, _track_owned_runtime_processes)


@pytest.mark.live_1c
def test_bounded_to_df_initializes_value_transfer_module(
    tmp_path: Path,
    _track_owned_runtime_processes: _OwnedProcessTracker,
) -> None:
    """A bounded table page must compile and run the real value-transfer module."""
    platform = _platform_bin()
    packaged = packaged_extension_bundle(tmp_path / "packaged").manifest
    bundle = build_runtime_extension_bundle(
        source_root=_REPOSITORY / "onec" / "OnecInteractiveRuntime",
        output_root=tmp_path / "bundle",
        platform_bin=platform,
        artifact_version=packaged.artifact_version,
        protocol_version=packaged.protocol_version,
    )
    config = _config(tmp_path / "target", platform)
    create_empty_infobase(config)
    _install_cfe(config, bundle.cfe_path, tmp_path / "install")
    session = RuntimeSession.start(RuntimeSessionConfig(
        config, tmp_path / "evidence", extension_mode=ExtensionMode.MANUAL
    ))
    try:
        reply = session.execute_bsl('''
ТаблицаДляСреза = Новый ТаблицаЗначений;
ТаблицаДляСреза.Колонки.Добавить("Значение", Новый ОписаниеТипов("Строка"));
Для Номер = 0 По 9 Цикл
    ТаблицаДляСреза.Добавить().Значение = "row_" + Формат(Номер, "ЧГ=0; ЧДЦ=0");
КонецЦикла;
''')
        assert reply.succeeded
        frame = session.project_to_df(
            "e1cRuntimeКонтекст.ТаблицаДляСреза",
            {"offset": 5, "limit": 5},
            timeout_s=20,
        )
        assert frame["Значение"].tolist() == [
            "row_5", "row_6", "row_7", "row_8", "row_9"
        ]
    finally:
        session.close()

    _assert_no_owned_1c_process(config, _track_owned_runtime_processes)


@pytest.mark.live_1c
def test_capture_cell_table_to_df_before_resume(
    tmp_path: Path,
    _track_owned_runtime_processes: _OwnedProcessTracker,
) -> None:
    """A table copied in a CAPTURE cell remains materializable while paused."""
    platform = _platform_bin()
    bundle = packaged_extension_bundle(tmp_path / "bundle")
    config = _config(tmp_path / "target", platform)
    create_empty_infobase(config)
    _install_cfe(config, bundle.cfe_path, tmp_path / "install")
    source = (
        _REPOSITORY / "onec" / "OnecInteractiveRuntime" / "CommonModules"
        / "RuntimeKernelServer" / "Ext" / "Module.bsl"
    ).read_text(encoding="utf-8-sig")
    marker_lines = [
        number for number, line in enumerate(source.splitlines(), start=1)
        if "@runtime-synthetic-capture-a" in line
    ]
    assert len(marker_lines) == 1
    location = replace(bundle.manifest.breakpoints.server_entry, line=marker_lines[0])
    session = RuntimeSession.start(RuntimeSessionConfig(
        config, tmp_path / "evidence", extension_mode=ExtensionMode.MANUAL
    ))
    try:
        session.configure_capture_points((location,))
        captured = session.execute_bsl(
            "Результат = RuntimeKernelServer.СинтетическийCapture(100);"
        )
        assert captured.kind is RuntimeReplyKind.CAPTURED
        cell = session.execute_bsl('''
ТаблицаДляPython = Новый ТаблицаЗначений;
ТаблицаДляPython.Колонки.Добавить(
    "Значение", Новый ОписаниеТипов("Строка"));
ТаблицаДляPython.Добавить().Значение = "first";
ТаблицаДляPython.Добавить().Значение = "second";
СнимокПосле = ТаблицаДляPython.Скопировать();
''')
        assert cell.kind is RuntimeReplyKind.CAPTURE_CELL
        assert cell.succeeded, cell.error
        frame = session.to_df("e1cRuntimeКонтекст.СнимокПосле")
        assert frame["Значение"].tolist() == ["first", "second"]
        resumed = session.resume_capture()
        assert resumed.kind is RuntimeReplyKind.MAIN_COMPLETED
        assert resumed.succeeded
        session.clear_capture_points()
    finally:
        session.close()

    _assert_no_owned_1c_process(config, _track_owned_runtime_processes)


@pytest.mark.live_1c
def test_capture_table_materialization_after_failed_then_fixed_cell(
    tmp_path: Path,
    _track_owned_runtime_processes: _OwnedProcessTracker,
) -> None:
    """A failed CAPTURE cell followed by a corrected cell must not poison to_df."""
    platform = _platform_bin()
    bundle = packaged_extension_bundle(tmp_path / "bundle")
    config = _config(tmp_path / "target", platform)
    create_empty_infobase(config)
    _install_cfe(config, bundle.cfe_path, tmp_path / "install")
    source = (
        _REPOSITORY / "onec" / "OnecInteractiveRuntime" / "CommonModules"
        / "RuntimeKernelServer" / "Ext" / "Module.bsl"
    ).read_text(encoding="utf-8-sig")
    marker_lines = [
        number for number, line in enumerate(source.splitlines(), start=1)
        if "@runtime-synthetic-capture-a" in line
    ]
    assert len(marker_lines) == 1
    location = replace(bundle.manifest.breakpoints.server_entry, line=marker_lines[0])
    session = RuntimeSession.start(RuntimeSessionConfig(
        config, tmp_path / "evidence", extension_mode=ExtensionMode.MANUAL
    ))
    try:
        session.configure_capture_points((location,))
        captured = session.execute_bsl(
            "Результат = RuntimeKernelServer.СинтетическийCapture(100);"
        )
        assert captured.kind is RuntimeReplyKind.CAPTURED
        cell = session.execute_bsl('''
СнимокПоказателей = Новый ТаблицаЗначений;
СнимокПоказателей.Колонки.Добавить("Значение", Новый ОписаниеТипов("Строка"));
СнимокПоказателей.Добавить().Значение = "after failure";
''')
        assert cell.kind is RuntimeReplyKind.CAPTURE_CELL
        assert cell.succeeded, cell.error
        page = session.project_to_df(
            "e1cRuntimeКонтекст.СнимокПоказателей", {"offset": 0, "limit": 1},
        )
        assert page["Значение"].tolist() == ["after failure"]
        failed = session.execute_bsl('''
Процедура ПоказатьОклад()
    ВызватьИсключение "deliberate procedure failure";
КонецПроцедуры

ПоказатьОклад();
''')
        assert failed.kind is RuntimeReplyKind.CAPTURE_CELL
        assert failed.succeeded is False
        fixed = session.execute_bsl('''
Процедура ПоказатьОклад()
    Сообщить("fixed");
КонецПроцедуры

ПоказатьОклад();
''')
        assert fixed.kind is RuntimeReplyKind.CAPTURE_CELL
        assert fixed.succeeded, fixed.error
        frame = session.to_df("e1cRuntimeКонтекст.СнимокПоказателей")
        assert frame["Значение"].tolist() == ["after failure"]
        resumed = session.resume_capture()
        assert resumed.kind is RuntimeReplyKind.MAIN_COMPLETED
        assert resumed.succeeded
        session.clear_capture_points()
    finally:
        session.close()

    _assert_no_owned_1c_process(config, _track_owned_runtime_processes)


def test_minimal_first_install_then_structural_fast_path(
    tmp_path: Path,
    _track_owned_runtime_processes: _OwnedProcessTracker,
) -> None:
    config = _config(tmp_path, _platform_bin())
    create_empty_infobase(config)

    _exercise_first_and_fast(config, tmp_path, _track_owned_runtime_processes)


@pytest.mark.parametrize("predecessor_version", ["0.0.9", "0.1.3", "0.1.4"])
def test_predecessor_is_updated_to_packaged_artifact(
    tmp_path: Path,
    _track_owned_runtime_processes: _OwnedProcessTracker,
    predecessor_version: str,
) -> None:
    platform = _platform_bin()
    predecessor = _build_variant(
        tmp_path / "predecessor", platform, artifact_version=predecessor_version
    )
    config = _config(tmp_path / "target", platform)
    create_empty_infobase(config)
    _install_cfe(config, predecessor.cfe_path, tmp_path / "preinstall")

    session, profiler = _start(config, tmp_path / "evidence")
    session.close()

    assert "extension.update" in _phase_names(profiler)
    _assert_exact_installed(
        config, packaged_extension_bundle(config.runtime_dir), tmp_path
    )
    _assert_no_owned_1c_process(config, _track_owned_runtime_processes)


def test_foreign_same_name_is_rejected_without_mutation(tmp_path: Path) -> None:
    platform = _platform_bin()
    source = _copy_source(tmp_path / "foreign")
    packaged = packaged_extension_bundle(tmp_path / "foreign-packaged-cache")
    identity = packaged.manifest.fingerprints.identity
    _replace_exact_in_xml_tree(
        source,
        str(identity.root_id),
        str(uuid4()),
        count=2,
    )
    for module_id in identity.runtime_module_ids:
        _replace_exact_in_xml_tree(
            source,
            str(module_id),
            str(uuid4()),
            count=3,
        )
    foreign_product_id = "foreign-runtime-product"
    _replace_exact(
        source / "Configuration.xml",
        f"<Vendor>{identity.product_id}</Vendor>",
        f"<Vendor>{foreign_product_id}</Vendor>",
        count=1,
    )
    declaration = f'ИдентификаторПродуктаRuntime = "{identity.product_id}";'
    replacement = f'ИдентификаторПродуктаRuntime = "{foreign_product_id}";'
    for relative in (
        Path("Ext/ManagedApplicationModule.bsl"),
        Path("CommonModules/RuntimeKernelServer/Ext/Module.bsl"),
    ):
        _replace_exact(source / relative, declaration, replacement, count=1)
    build_base = tmp_path / "foreign-build-base"
    create_file_infobase_at(
        platform / "1cv8.exe", build_base, tmp_path / "foreign-create.log"
    )
    load_product_extension_source(
        platform / "1cv8.exe", build_base, source, tmp_path / "foreign-source.log"
    )
    foreign_cfe = tmp_path / f"foreign-{EXTENSION_NAME}.cfe"
    dump_product_extension_cfe(
        platform / "1cv8.exe", build_base, foreign_cfe, tmp_path / "foreign-cfe.log"
    )
    foreign_build_config = _config(
        tmp_path / "foreign-inspection", platform, infobase=build_base
    )
    foreign_dump = tmp_path / "foreign-dump"
    dump_target_extension_files(
        foreign_build_config,
        foreign_dump,
        tmp_path / "foreign-dump.log",
    )
    foreign_fingerprint = fingerprint_extension_dump(
        foreign_dump, expected_product_id=None
    )
    assert foreign_fingerprint.identity.product_id == foreign_product_id
    assert foreign_fingerprint.identity.root_id != identity.root_id
    assert set(foreign_fingerprint.identity.runtime_module_ids).isdisjoint(
        identity.runtime_module_ids
    )
    assert (
        foreign_fingerprint.identity_sha256
        != packaged.manifest.fingerprints.identity_sha256
    )
    config = _config(tmp_path / "target", platform)
    create_empty_infobase(config)
    _install_cfe(config, foreign_cfe, tmp_path / "preinstall")
    before = tmp_path / "before.cfe"
    dump_target_extension_cfe(config, before, tmp_path / "before.log")

    evidence_root = tmp_path / "evidence"
    with pytest.raises(ExtensionIdentityConflict):
        RuntimeSession.start(RuntimeSessionConfig(config, tmp_path / "evidence"))

    run_dir = next(evidence_root.iterdir())
    phase_evidence = json.loads(
        (run_dir / "bootstrap-phases.json").read_text(encoding="utf-8")
    )
    assert [event["phase"] for event in phase_evidence] == [
        "extension.bundle",
        "extension.inspect",
    ]

    after = tmp_path / "after.cfe"
    dump_target_extension_cfe(config, after, tmp_path / "after.log")
    assert sha256(before.read_bytes()).digest() == sha256(after.read_bytes()).digest()
    _assert_no_owned_1c_process(config)


def test_stale_marker_with_absent_extension_repairs_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _track_owned_runtime_processes: _OwnedProcessTracker,
) -> None:
    config = _config(tmp_path, _platform_bin())
    create_empty_infobase(config)
    bundle = packaged_extension_bundle(config.runtime_dir)
    _write_matching_marker(config, bundle)
    original = session_module.wait_for_managed_startup_stop
    monkeypatch.setattr(
        session_module,
        "wait_for_managed_startup_stop",
        lambda session, expected, *, timeout_s, on_poll=None: original(
            session, expected, timeout_s=min(timeout_s, 5.0), on_poll=on_poll
        ),
    )

    session, profiler = _start(config, tmp_path / "evidence")
    session.close()

    phases = _phase_names(profiler)
    assert phases.count("extension.retry") == 1
    assert phases.count("extension.install") == 1
    _assert_exact_installed(config, bundle, tmp_path)
    _assert_no_owned_1c_process(config, _track_owned_runtime_processes)


def test_same_version_incompatible_handshake_is_never_admitted(
    tmp_path: Path,
    _track_owned_runtime_processes: _OwnedProcessTracker,
) -> None:
    platform = _platform_bin()
    incompatible = _build_variant(
        tmp_path / "incompatible", platform, protocol_version="999"
    )
    config = _config(tmp_path / "target", platform)
    create_empty_infobase(config)
    _install_cfe(config, incompatible.cfe_path, tmp_path / "preinstall")
    packaged = packaged_extension_bundle(config.runtime_dir)
    assert (
        incompatible.manifest.fingerprints.artifact_sha256
        != packaged.manifest.fingerprints.artifact_sha256
    )
    _write_matching_marker(config, packaged)

    evidence_root = tmp_path / "evidence"
    profiler = PhaseRecorder()
    with pytest.raises(ExtensionHandshakeError) as raised:
        RuntimeSession.start(
            RuntimeSessionConfig(config, evidence_root, startup_profiler=profiler)
        )

    assert raised.value.__notes__ == [
        "Runtime startup repair failed: ExtensionLifecycleError"
    ]
    run_dir = next(evidence_root.iterdir())
    attempts = json.loads(
        (run_dir / "bootstrap-attempts.json").read_text(encoding="utf-8")
    )
    phase_evidence = json.loads(
        (run_dir / "bootstrap-phases.json").read_text(encoding="utf-8")
    )

    phases = _phase_names(profiler)
    assert phases.count("extension.retry") == 1
    assert phases.count("extension.update") == 0
    assert attempts == [
        {
            "admitted": False,
            "attempt": 1,
            "cleanup_succeeded": True,
            "decision_mode": "fast",
            "error_type": "ExtensionHandshakeError",
            "repairable": True,
            "stage": "managed-handshake",
        }
    ]
    assert [(event["phase"], event["error_present"]) for event in phase_evidence] == [
        ("extension.bundle", False),
        ("extension.inspect", False),
        ("extension.retry", True),
    ]
    assert not (run_dir / "bootstrap.json").exists()
    assert _marker(config).read() is None
    _assert_exact_installed(config, incompatible, tmp_path)
    _assert_no_owned_1c_process(config, _track_owned_runtime_processes)


def _zup_target() -> tuple[Path, str]:
    configured = os.environ.get("ONEC_RUNTIME_ZUP_CLEAN_INFOBASE", "").strip()
    username = os.environ.get("ONEC_RUNTIME_USERNAME", "").strip()
    if not configured or not username:
        pytest.skip(
            "dedicated ZUP subset blocked: set ONEC_RUNTIME_ZUP_CLEAN_INFOBASE and ONEC_RUNTIME_USERNAME"
        )
    target = Path(configured).resolve()
    forbidden = {
        Path(target.anchor).resolve(),
        _REPOSITORY.resolve(),
        _REPOSITORY.parent.resolve(),
    }
    if target in forbidden or _REPOSITORY.resolve() in target.parents:
        pytest.fail("refusing broad, workspace, or source ZUP target")
    if not (target / "1Cv8.1CD").is_file():
        pytest.fail("dedicated ZUP target must contain 1Cv8.1CD")
    needle = str(target).casefold()
    for process in psutil.process_iter(("name", "cmdline")):
        try:
            if (process.info["name"] or "").casefold() not in {"1cv8.exe", "1cv8c.exe", "dbgs.exe"}:
                continue
            command = " ".join(process.info["cmdline"] or ()).casefold()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
        if needle in command:
            pytest.fail(f"dedicated ZUP target is open in process {process.pid}")
    return target, username


def test_dedicated_clean_zup_first_install_then_fast_path(
    tmp_path: Path,
    _track_owned_runtime_processes: _OwnedProcessTracker,
) -> None:
    target, username = _zup_target()
    config = _config(
        tmp_path,
        _platform_bin(),
        infobase=target,
        username=username,
    )

    _exercise_first_and_fast(config, tmp_path, _track_owned_runtime_processes)
