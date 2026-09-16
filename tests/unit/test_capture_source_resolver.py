from pathlib import Path
from uuid import UUID
import os
import shutil
import pytest
import onec_runtime.capture_source as capture_source
from onec_runtime.kernel import (
    COMMON_MODULE_PROPERTY_ID,
    DOCUMENT_MANAGER_MODULE_PROPERTY_ID,
    OBJECT_MODULE_PROPERTY_ID,
)
from onec_runtime.rdbg.models import ModuleLocation
from tests.unit.test_configuration_source_layout import FIXTURES

COMMON = UUID("11111111-2222-3333-4444-555555555555")
DOCUMENT = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")


def catalog(*configs, **kwargs):
    assert hasattr(capture_source, "CaptureSourceCatalog"), (
        "batch source catalog is missing"
    )
    return capture_source.CaptureSourceCatalog(configs, **kwargs)


def location(object_id=COMMON, role=COMMON_MODULE_PROPERTY_ID, extension="", line=2):
    return ModuleLocation(
        "ExtensionModule" if extension else "ConfigModule",
        "",
        object_id,
        UUID(role),
        line,
        extension_name=extension,
    )


@pytest.mark.parametrize("layout", ["designer", "edt"])
def test_same_physical_ids_resolve_by_layer_with_canonical_names_and_lines(layout):
    source = catalog(
        *(
            capture_source.CaptureSourceConfig("demo", FIXTURES / f"{layout}_{layer}")
            for layer in ("base", "extension")
        )
    )
    requests = tuple(
        location(obj, prop, ext, 2)
        for ext in ("", "Дополнение")
        for obj, prop in [
            (COMMON, COMMON_MODULE_PROPERTY_ID),
            (DOCUMENT, OBJECT_MODULE_PROPERTY_ID),
        ]
    )
    result = source.resolve_modules(requests)
    assert [item.canonical_name for item in result] == [
        "ОбщийМодуль.Общий.Модуль",
        "Документ.ПриемНаРаботу.МодульОбъекта",
    ] * 2
    assert [item.module_role for item in result] == ["Module", "ObjectModule"] * 2
    assert [item.line for item in result] == [2] * 4
    assert [item.binding.extension_name for item in result] == [
        None,
        None,
        "Дополнение",
        "Дополнение",
    ]
    for item in result:
        assert item.source_version.source_status == "trusted_export"
        assert item.source_version.read_text().splitlines()[1] == "    Значение = 1;"


def test_edt_src_root_resolves_common_and_document_object_frames():
    source = catalog(
        capture_source.CaptureSourceConfig("demo", FIXTURES / "edt_base" / "src")
    )

    result = source.resolve_modules(
        (
            location(COMMON, COMMON_MODULE_PROPERTY_ID),
            location(DOCUMENT, OBJECT_MODULE_PROPERTY_ID),
        )
    )

    assert [item.canonical_name for item in result] == [
        "ОбщийМодуль.Общий.Модуль",
        "Документ.ПриемНаРаботу.МодульОбъекта",
    ]
    assert [item.module_role for item in result] == ["Module", "ObjectModule"]
    assert [item.source_version.source_status for item in result] == [
        "trusted_export",
        "trusted_export",
    ]


@pytest.mark.parametrize("layout", ["designer", "edt"])
@pytest.mark.parametrize("layer", ["base", "extension"])
def test_document_manager_module_resolves_by_layer_with_verified_property(
    layout, layer
):
    extension = "Дополнение" if layer == "extension" else ""
    source = catalog(
        capture_source.CaptureSourceConfig("demo", FIXTURES / f"{layout}_{layer}")
    )

    result, = source.resolve_modules(
        (location(DOCUMENT, DOCUMENT_MANAGER_MODULE_PROPERTY_ID, extension),)
    )

    assert result.canonical_name == "Документ.ПриемНаРаботу.МодульМенеджера"
    assert result.module_role == "ManagerModule"
    assert result.line == 2
    assert result.binding.extension_name == (extension or None)
    root = FIXTURES / f"{layout}_{layer}" / ("src" if layout == "edt" else "")
    relative = (
        "Documents/ПриемНаРаботу/Ext/ManagerModule.bsl"
        if layout == "designer"
        else "Documents/ПриемНаРаботу/ManagerModule.bsl"
    )
    assert result.source_path == (root / relative).resolve()
    assert result.source_version.source_status == "trusted_export"
    assert result.source_version.read_text().splitlines()[1] == "    Значение = 1;"


def test_missing_extension_and_unknown_property_are_unavailable_without_fallback(
    monkeypatch,
):
    source = catalog(
        capture_source.CaptureSourceConfig("demo", FIXTURES / "designer_base")
    )
    passes = []
    original = os.scandir

    def observed(path):
        passes.append(Path(path))
        return original(path)

    monkeypatch.setattr(os, "scandir", observed)
    result = source.resolve_modules(
        (location(extension="Дополнение"), location(role=str(UUID(int=1))))
    )
    assert [item.reason for item in result] == [
        "source_unavailable",
        "source_unavailable",
    ]
    assert passes == []


@pytest.mark.parametrize("layout", ["designer", "edt"])
def test_batch_is_lazy_caches_encountered_descriptions_and_stops_early(
    tmp_path, monkeypatch, layout
):
    project = tmp_path / "project"
    shutil.copytree(FIXTURES / f"{layout}_base", project)
    root = project / ("src" if layout == "edt" else "")
    passes, reads = [], []
    original_scan, original_read = os.scandir, Path.read_bytes

    def scan(path):
        passes.append(Path(path))
        return original_scan(path)

    def read(path):
        reads.append(path)
        return original_read(path)

    monkeypatch.setattr(os, "scandir", scan)
    monkeypatch.setattr(Path, "read_bytes", read)
    source = catalog(capture_source.CaptureSourceConfig("demo", project))
    assert not any(p.parent.name in {"CommonModules", "Documents"} for p in reads)
    passes.clear()
    reads.clear()
    result = source.resolve_modules(
        (location(), location(DOCUMENT, OBJECT_MODULE_PROPERTY_ID))
    )
    assert all(item.source_version for item in result)
    assert passes.count(root / "CommonModules") == 1
    assert passes.count(root / "Documents") == 1
    passes.clear()
    reads.clear()
    assert source.resolve_modules((location(line=3),))[0].line == 3
    assert passes == [] and reads == []
    # A trailing invalid description must never be opened after a match.
    if layout == "designer":
        (root / "CommonModules" / "ЯПоследний.xml").write_text("invalid")
    else:
        tail = root / "CommonModules" / "ЯПоследний"
        tail.mkdir()
        (tail / "ЯПоследний.mdo").write_text("invalid")
    fresh = catalog(capture_source.CaptureSourceConfig("demo", project))
    assert (
        fresh.resolve_modules((location(),))[0].canonical_name
        == "ОбщийМодуль.Общий.Модуль"
    )


def test_negative_cache_is_bounded_and_refresh_discovers_new_module(tmp_path):
    project = tmp_path / "project"
    shutil.copytree(FIXTURES / "designer_base", project)
    source = catalog(capture_source.CaptureSourceConfig("demo", project), cache_limit=2)
    missing = location(UUID(int=42))
    assert source.resolve_modules((missing,))[0].reason == "source_unavailable"
    path = project / "CommonModules" / "Новый.xml"
    path.write_text(
        f'<MetaDataObject><CommonModule uuid="{UUID(int=42)}"><Properties><Name>Новый</Name></Properties></CommonModule></MetaDataObject>',
        encoding="utf-8",
    )
    module = project / "CommonModules" / "Новый" / "Ext"
    module.mkdir(parents=True)
    (module / "Module.bsl").write_text("Значение = 2;", encoding="utf-8")
    assert source.resolve_modules((missing,))[0].reason == "source_unavailable"
    source.refresh()
    assert (
        source.resolve_modules((missing,))[0].canonical_name
        == "ОбщийМодуль.Новый.Модуль"
    )
    source.resolve_modules(tuple(location(UUID(int=n)) for n in range(100, 110)))
    assert source.cache_size <= 2


def test_pin_rejects_file_change_before_read(tmp_path):
    project = tmp_path / "project"
    shutil.copytree(FIXTURES / "designer_base", project)
    source = catalog(capture_source.CaptureSourceConfig("demo", project))
    pin = source.resolve_modules((location(),))[0].source_version
    pin.path.write_text("changed", encoding="utf-8")
    with pytest.raises(
        capture_source.CaptureSourceChangedError, match="source_changed"
    ):
        pin.read_text()


def test_pin_rejects_change_during_same_open_file_read(tmp_path, monkeypatch):
    project = tmp_path / "project"
    shutil.copytree(FIXTURES / "designer_base", project)
    pin = (
        catalog(capture_source.CaptureSourceConfig("demo", project))
        .resolve_modules((location(),))[0]
        .source_version
    )
    original = os.fstat
    count = 0

    def observed(fd):
        nonlocal count
        count += 1
        if count == 2:
            os.utime(
                pin.path, ns=(pin.mtime_ns + 1000000000, pin.mtime_ns + 1000000000)
            )
        return original(fd)

    monkeypatch.setattr(os, "fstat", observed)
    with pytest.raises(
        capture_source.CaptureSourceChangedError, match="source_changed"
    ):
        pin.read_text()


def test_worker_source_pin_is_immutable_and_keeps_owning_generation():
    assert hasattr(capture_source, "SourceVersionRef"), "source version pin is missing"
    old = capture_source.SourceVersionRef.worker(
        artifact_id="artifact-old", generation=1, source_text="old"
    )
    new = capture_source.SourceVersionRef.worker(
        artifact_id="artifact-new", generation=2, source_text="new"
    )
    assert old.read_text() == "old" and new.read_text() == "new"
    assert old.artifact_id == "artifact-old" and old.generation == 1
    assert old.source_status == "runtime_verified"
    with pytest.raises(AttributeError):
        old.generation = 2


def test_encountered_metadata_is_reused_without_second_directory_pass(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    shutil.copytree(FIXTURES / "designer_base", project)
    late_id = UUID(int=42)
    metadata = project / "CommonModules" / "ЯПоздний.xml"
    metadata.write_text(
        f'<MetaDataObject><CommonModule uuid="{late_id}"><Properties><Name>ЯПоздний</Name></Properties></CommonModule></MetaDataObject>',
        encoding="utf-8",
    )
    folder = project / "CommonModules" / "ЯПоздний" / "Ext"
    folder.mkdir(parents=True)
    (folder / "Module.bsl").write_text("Значение = 2;", encoding="utf-8")
    source = catalog(capture_source.CaptureSourceConfig("demo", project))
    assert (
        source.resolve_modules((location(late_id),))[0].canonical_name
        == "ОбщийМодуль.ЯПоздний.Модуль"
    )

    def forbidden(path):
        raise AssertionError("encountered description triggered another directory pass")

    monkeypatch.setattr(os, "scandir", forbidden)
    assert (
        source.resolve_modules((location(),))[0].canonical_name
        == "ОбщийМодуль.Общий.Модуль"
    )


def test_refresh_replaces_changed_positive_metadata_without_rereading_unchanged(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    shutil.copytree(FIXTURES / "designer_base", project)
    source = catalog(capture_source.CaptureSourceConfig("demo", project))
    source.resolve_modules((location(), location(DOCUMENT, OBJECT_MODULE_PROPERTY_ID)))
    changed = project / "CommonModules" / "Общий.xml"
    changed.write_text(
        changed.read_text(encoding="utf-8").replace(str(COMMON), str(UUID(int=42))),
        encoding="utf-8",
    )
    source.refresh()
    reads = []
    original = Path.read_bytes

    def observed(path):
        reads.append(path)
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", observed)
    result = source.resolve_modules(
        (
            location(),
            location(UUID(int=42)),
            location(DOCUMENT, OBJECT_MODULE_PROPERTY_ID),
        )
    )
    assert result[0].reason == "source_unavailable"
    assert result[1].canonical_name == "ОбщийМодуль.Общий.Модуль"
    assert result[2].canonical_name == "Документ.ПриемНаРаботу.МодульОбъекта"
    assert reads == [changed]


def test_configuration_mismatch_cannot_fall_back_to_valid_base():
    from onec_runtime.errors import ProtocolError

    configs = (
        capture_source.CaptureSourceConfig("demo", FIXTURES / "designer_base"),
        capture_source.CaptureSourceConfig(
            "demo",
            FIXTURES / "designer_extension",
            layer="extension",
            extension_name="Другое",
        ),
    )
    with pytest.raises(ProtocolError, match="match"):
        catalog(*configs)
