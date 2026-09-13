from pathlib import Path
import sys

import pytest

from onec_runtime.bsl.module_catalog import (
    CommonModuleCatalogSnapshot,
    CommonModuleScope,
    SessionCommonModuleCatalog,
)
from onec_runtime.errors import ProtocolError


def add_metadata(
    source_root: Path,
    name: str,
    *,
    server: bool,
    client: bool,
    global_module: bool,
) -> Path:
    common_modules = source_root / "CommonModules"
    common_modules.mkdir(parents=True, exist_ok=True)
    path = common_modules / f"{name}.xml"
    path.write_text(
        "<MetaDataObject><CommonModule><Properties>"
        f"<Name>{name}</Name>"
        f"<Global>{str(global_module).lower()}</Global>"
        f"<Server>{str(server).lower()}</Server>"
        f"<ClientManagedApplication>{str(client).lower()}</ClientManagedApplication>"
        "<ClientOrdinaryApplication>false</ClientOrdinaryApplication>"
        "</Properties></CommonModule></MetaDataObject>",
        encoding="utf-8",
    )
    return path


def source_project(root: Path, *modules: tuple[str, bool, bool, bool]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name, server, client, global_module in modules:
        add_metadata(
            root,
            name,
            server=server,
            client=client,
            global_module=global_module,
        )
    return root


def test_catalog_initializes_name_index_then_lazily_reads_requested_xml(
    tmp_path, monkeypatch
):
    source = source_project(
        tmp_path,
        ("Серверный", True, False, False),
        ("КлиентСервер", True, True, False),
        ("Клиентский", False, True, False),
        ("Глобальный", True, False, True),
    )
    reads: list[Path] = []
    original = Path.read_bytes

    def observed(path: Path) -> bytes:
        reads.append(path)
        assert path.suffix.casefold() == ".xml"
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", observed)
    catalog = SessionCommonModuleCatalog(
        source, profile="server-zup-8.3.27.2170"
    )

    first = catalog.ensure_initialized()
    second = catalog.ensure_initialized()

    assert first is second
    assert first.revision == 1
    assert first.modules == ()
    assert reads == []

    resolved = catalog.resolve_candidates(
        ("Серверный", "КлиентСервер", "Клиентский", "Глобальный")
    )

    assert resolved.revision == 2
    assert [(item.canonical_name, item.scope) for item in resolved.modules] == [
        ("КлиентСервер", CommonModuleScope.CLIENT_SERVER),
        ("Серверный", CommonModuleScope.SERVER),
    ]
    assert len(reads) == 4


def test_catalog_rejects_symlink_or_escape_before_reading_metadata(tmp_path):
    outside = source_project(tmp_path / "outside", ("Модуль", True, False, False))
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as error:
        if (
            getattr(error, "winerror", None) in {1314}
            or getattr(error, "errno", None) in {1, 13}
        ):
            pytest.skip("symlink creation requires OS privileges")
        raise

    with pytest.raises(ProtocolError, match="source root is unsafe"):
        SessionCommonModuleCatalog(linked, profile="server")


def test_missing_batch_extension_is_atomic_and_does_not_reread_known_names(tmp_path):
    source = source_project(tmp_path, ("Существующий", True, False, False))
    catalog = SessionCommonModuleCatalog(source, profile="server")
    first = catalog.ensure_initialized()
    add_metadata(source, "НовыйА", server=True, client=False, global_module=False)
    add_metadata(source, "НовыйБ", server=True, client=True, global_module=False)

    second = catalog.ensure_modules(("Существующий", "НовыйА", "НовыйБ"))

    assert second.revision == first.revision + 1
    assert {item.canonical_name for item in second.modules} == {
        "Существующий", "НовыйА", "НовыйБ"
    }


def test_one_invalid_missing_entry_rolls_back_entire_batch(tmp_path):
    source = source_project(tmp_path, ("Существующий", True, False, False))
    catalog = SessionCommonModuleCatalog(source, profile="server")
    before = catalog.ensure_initialized()
    add_metadata(source, "НовыйА", server=True, client=False, global_module=False)
    add_metadata(source, "НовыйБ", server=False, client=True, global_module=False)

    with pytest.raises(ProtocolError, match="missing common module is unsupported"):
        catalog.ensure_modules(("НовыйА", "НовыйБ"))

    assert catalog.ensure_initialized() is before


def test_catalog_supports_namespaces_and_revision_independent_audit_hash(tmp_path):
    source = tmp_path
    common_modules = source / "CommonModules"
    common_modules.mkdir(parents=True)
    (common_modules / "Серверный.xml").write_text(
        '<m:MetaDataObject xmlns:m="urn:metadata"><m:CommonModule><m:Properties>'
        '<m:Name>Серверный</m:Name><m:Global>false</m:Global>'
        '<m:Server>true</m:Server><m:ClientManagedApplication>false</m:ClientManagedApplication>'
        '<m:ClientOrdinaryApplication>false</m:ClientOrdinaryApplication>'
        "</m:Properties></m:CommonModule></m:MetaDataObject>",
        encoding="utf-8",
    )
    first = SessionCommonModuleCatalog(source, profile="server").ensure_initialized()
    second = CommonModuleCatalogSnapshot.create(
        profile="server",
        preprocessor_profile="server",
        revision=99,
        modules=first.modules,
    )

    assert first.modules == second.modules
    assert first.sha256 == second.sha256


def test_all_known_or_empty_batch_returns_same_snapshot(tmp_path):
    source = source_project(tmp_path, ("Существующий", True, False, False))
    catalog = SessionCommonModuleCatalog(source, profile="server")
    first = catalog.ensure_initialized()

    assert catalog.ensure_modules(()) is first
    loaded = catalog.ensure_modules(("существующий",))
    assert loaded.revision == 2
    assert catalog.ensure_modules(("СУЩЕСТВУЮЩИЙ",)) is loaded


def test_unrequested_invalid_xml_does_not_block_init_and_failed_read_is_atomic(tmp_path):
    source = source_project(tmp_path, ("Существующий", True, False, False))
    (source / "CommonModules" / "Сломанный.xml").write_text(
        "<broken", encoding="utf-8"
    )
    catalog = SessionCommonModuleCatalog(source, profile="server")
    before = catalog.ensure_initialized()

    with pytest.raises(ProtocolError, match="metadata is invalid"):
        catalog.resolve_candidates(("Сломанный",))

    assert catalog.ensure_initialized() is before


def add_edt_metadata(root: Path, name: str, properties: str = "<server>true</server>") -> Path:
    folder = root / "CommonModules" / name
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.mdo"
    path.write_text(
        '<mdclass:CommonModule xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass">'
        f"<name>{name}</name>{properties}</mdclass:CommonModule>", encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("project_root", [False, True])
def test_edt_roots_resolve_metadata_lazily_and_default_omitted_flags_to_false(
    tmp_path, monkeypatch, project_root,
):
    source = tmp_path / "src" if project_root else tmp_path
    server = add_edt_metadata(source, "Серверный")
    client = add_edt_metadata(source, "КлиентСервер", "<server>true</server><clientManagedApplication>true</clientManagedApplication>")
    ordinary = add_edt_metadata(source, "ОбычныйКлиент", "<server>true</server><clientOrdinaryApplication>true</clientOrdinaryApplication>")
    add_edt_metadata(source, "Глобальный", "<server>true</server><global>true</global>")
    add_edt_metadata(source, "Клиентский", "<clientManagedApplication>true</clientManagedApplication>")
    add_edt_metadata(source, "НеЗапрошенный").write_text("<broken", encoding="utf-8")
    reads = []
    read_bytes = Path.read_bytes

    def observed(path):
        reads.append(path)
        assert path.suffix == ".mdo"  # No BSL/configuration scan.
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", observed)
    catalog = SessionCommonModuleCatalog(tmp_path, profile="server")
    assert catalog.ensure_initialized().modules == ()
    assert reads == []
    snapshot = catalog.resolve_candidates(("СЕРВЕРНЫЙ", "КлиентСервер", "ОбычныйКлиент", "Глобальный", "Клиентский"))
    assert [(item.canonical_name, item.scope) for item in snapshot.modules] == [
        ("КлиентСервер", CommonModuleScope.CLIENT_SERVER),
        ("ОбычныйКлиент", CommonModuleScope.CLIENT_SERVER),
        ("Серверный", CommonModuleScope.SERVER),
    ]
    assert len(reads) == 5
    assert {server, client, ordinary} <= set(reads)
    assert catalog.ensure_modules(("серверный",)) is snapshot
    assert len(reads) == 5


def test_edt_and_designer_produce_equivalent_catalog_snapshots(tmp_path):
    edt = tmp_path / "edt"
    designer = tmp_path / "designer"
    add_edt_metadata(edt, "Модуль")
    add_metadata(designer, "Модуль", server=True, client=False, global_module=False)
    snapshots = [SessionCommonModuleCatalog(root, profile="server").ensure_modules(("Модуль",))
                 for root in (edt, designer)]
    assert snapshots[0] == snapshots[1]


def test_edt_batch_failure_is_atomic_and_new_modules_are_discovered(tmp_path):
    known = add_edt_metadata(tmp_path, "Существующий")
    catalog = SessionCommonModuleCatalog(tmp_path, profile="server")
    before = catalog.ensure_modules(("Существующий",))
    known.write_text("<broken", encoding="utf-8")  # Already cached; never reread.
    add_edt_metadata(tmp_path, "НовыйА")
    broken = add_edt_metadata(tmp_path, "НовыйБ", "<server>invalid</server>")
    with pytest.raises(ProtocolError, match="metadata is invalid"):
        catalog.ensure_modules(("Существующий", "НовыйА", "НовыйБ"))
    assert catalog.ensure_initialized() is before
    broken.write_text(broken.read_text(encoding="utf-8").replace("invalid", "true"), encoding="utf-8")
    after = catalog.ensure_modules(("Существующий", "НовыйА", "НовыйБ"))
    assert after.revision == before.revision + 1
    assert {item.canonical_name for item in after.modules} == {"Существующий", "НовыйА", "НовыйБ"}


@pytest.mark.parametrize("replacement", [
    "<name>Другой</name><server>true</server>",
    "<name>Модуль</name><server/>",
    "<name>Модуль</name><server>invalid</server>",
    "<name>Модуль</name><server>true</server><global>invalid</global>",
    "<name>Модуль</name><server>true</server><server>false</server>",
    "<name>Модуль</name><name>Другой</name><server>true</server>",
])
def test_edt_invalid_identity_or_flags_fail_closed(tmp_path, replacement):
    path = add_edt_metadata(tmp_path, "Модуль")
    path.write_text(f"<CommonModule>{replacement}</CommonModule>", encoding="utf-8")
    catalog = SessionCommonModuleCatalog(tmp_path, profile="server")
    with pytest.raises(ProtocolError, match="metadata.*invalid"):
        catalog.ensure_modules(("Модуль",))


def test_duplicate_identity_across_source_formats_is_rejected(tmp_path):
    add_metadata(tmp_path, "Модуль", server=True, client=False, global_module=False)
    add_edt_metadata(tmp_path, "МОДУЛЬ")
    catalog = SessionCommonModuleCatalog(tmp_path, profile="server")
    with pytest.raises(ProtocolError, match="duplicate common module identity"):
        catalog.ensure_initialized()


def test_edt_metadata_filename_must_match_parent_module(tmp_path):
    path = add_edt_metadata(tmp_path, "Модуль")
    path.rename(path.with_name("Другой.mdo"))
    catalog = SessionCommonModuleCatalog(tmp_path, profile="server")
    with pytest.raises(ProtocolError, match="metadata identity is invalid"):
        catalog.ensure_initialized()


def test_ambiguous_direct_and_nested_source_roots_are_rejected(tmp_path):
    add_edt_metadata(tmp_path, "Модуль")
    add_edt_metadata(tmp_path / "src", "Другой")
    with pytest.raises(ProtocolError, match="ambiguous"):
        SessionCommonModuleCatalog(tmp_path, profile="server")


@pytest.mark.parametrize("level", ["src", "CommonModules", "module", "metadata"])
def test_edt_rejects_symlink_paths_before_reading_metadata(tmp_path, monkeypatch, level):
    outside = tmp_path / "outside"
    metadata = add_edt_metadata(outside, "Модуль")
    project = tmp_path / "project"
    project.mkdir()
    links = {
        "src": (project / "src", outside),
        "CommonModules": (project / "CommonModules", outside / "CommonModules"),
        "module": (project / "CommonModules/Модуль", metadata.parent),
        "metadata": (project / "CommonModules/Модуль/Модуль.mdo", metadata),
    }
    link, target = links[level]
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except OSError as error:
        if getattr(error, "winerror", None) == 1314 or getattr(error, "errno", None) in {1, 13}:
            pytest.skip("symlink creation requires OS privileges")
        raise

    def forbidden_read(_path):
        pytest.fail("unsafe metadata must not be read")

    monkeypatch.setattr(Path, "read_bytes", forbidden_read)
    with pytest.raises(ProtocolError, match="source root is unsafe"):
        catalog = SessionCommonModuleCatalog(project, profile="server")
        catalog.ensure_modules(("Модуль",))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junction check")
@pytest.mark.parametrize("level", ["src", "CommonModules", "module"])
def test_edt_rejects_windows_junctions_before_reading_metadata(tmp_path, monkeypatch, level):
    import _winapi

    outside = tmp_path / "outside"
    metadata = add_edt_metadata(outside, "Модуль")
    project = tmp_path / "project"
    project.mkdir()
    link, target = {
        "src": (project / "src", outside),
        "CommonModules": (project / "CommonModules", outside / "CommonModules"),
        "module": (project / "CommonModules/Модуль", metadata.parent),
    }[level]
    link.parent.mkdir(parents=True, exist_ok=True)
    _winapi.CreateJunction(str(target), str(link))

    def forbidden_read(_path):
        pytest.fail("junction metadata must not be read")

    monkeypatch.setattr(Path, "read_bytes", forbidden_read)
    with pytest.raises(ProtocolError, match="source root is unsafe"):
        catalog = SessionCommonModuleCatalog(project, profile="server")
        catalog.ensure_modules(("Модуль",))
