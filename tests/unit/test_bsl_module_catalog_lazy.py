from __future__ import annotations

from pathlib import Path

import pytest

from onec_runtime.bsl.module_catalog import (
    CommonModuleScope,
    SessionCommonModuleCatalog,
)
from onec_runtime.errors import ProtocolError


def _add_metadata(
    source_root: Path,
    name: str,
    *,
    server: bool = True,
    client: bool = False,
    global_module: bool = False,
) -> Path:
    directory = source_root / "CommonModules"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.xml"
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


def _source_root(tmp_path: Path) -> Path:
    (tmp_path / "CommonModules").mkdir(parents=True)
    return tmp_path


def test_initialization_indexes_names_without_opening_xml_payloads(tmp_path, monkeypatch):
    source = _source_root(tmp_path)
    _add_metadata(source, "Серверный")
    _add_metadata(source, "Клиентский", server=False, client=True)
    reads: list[Path] = []
    original = Path.read_bytes

    def observed(path: Path) -> bytes:
        reads.append(path)
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", observed)
    catalog = SessionCommonModuleCatalog(source, profile="server")

    snapshot = catalog.ensure_initialized()

    assert snapshot.revision == 1
    assert snapshot.modules == ()
    assert reads == []


def test_initialization_does_not_resolve_or_stat_each_enumerated_entry(
    tmp_path, monkeypatch
):
    source = _source_root(tmp_path)
    _add_metadata(source, "Серверный")
    catalog = SessionCommonModuleCatalog(source, profile="server")

    def forbidden(*args, **kwargs):
        raise AssertionError("catalog initialization inspected an XML payload path")

    monkeypatch.setattr(Path, "resolve", forbidden)
    monkeypatch.setattr(Path, "is_symlink", forbidden)
    monkeypatch.setattr(Path, "is_file", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)

    snapshot = catalog.ensure_initialized()

    assert snapshot.modules == ()


def test_casefold_collision_fails_before_opening_xml_payload(tmp_path, monkeypatch):
    source = _source_root(tmp_path)
    first = _add_metadata(source, "Alpha")
    collision = source / "CommonModules" / "ALPHA.xml"
    if collision != first:
        collision.write_text("<broken", encoding="utf-8")
    reads: list[Path] = []
    original = Path.read_bytes
    original_glob = Path.glob

    def observed(path: Path) -> bytes:
        reads.append(path)
        return original(path)

    def colliding_glob(path: Path, pattern: str):
        if path == source / "CommonModules" and pattern == "*.xml":
            return iter((first, collision))
        return original_glob(path, pattern)

    monkeypatch.setattr(Path, "read_bytes", observed)
    monkeypatch.setattr(Path, "glob", colliding_glob)

    with pytest.raises(ProtocolError, match="duplicate common module identity"):
        SessionCommonModuleCatalog(source, profile="server").ensure_initialized()

    assert reads == []


def test_candidates_open_each_unique_matching_xml_once(tmp_path, monkeypatch):
    source = _source_root(tmp_path)
    _add_metadata(source, "Альфа")
    _add_metadata(source, "Бета", client=True)
    _add_metadata(source, "НеЗапрошен")
    reads: list[str] = []
    original = Path.read_bytes

    def observed(path: Path) -> bytes:
        reads.append(path.stem)
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", observed)
    catalog = SessionCommonModuleCatalog(source, profile="server")

    snapshot = catalog.resolve_candidates(("АЛЬФА", "Бета", "альфа", "Нет"))
    again = catalog.resolve_candidates(("Альфа", "БЕТА"))

    assert [(item.canonical_name, item.scope) for item in snapshot.modules] == [
        ("Альфа", CommonModuleScope.SERVER),
        ("Бета", CommonModuleScope.CLIENT_SERVER),
    ]
    assert again is snapshot
    assert reads == ["Альфа", "Бета"]


def test_existing_non_server_candidate_is_negative_cached(tmp_path, monkeypatch):
    source = _source_root(tmp_path)
    _add_metadata(source, "Клиентский", server=False, client=True)
    reads = 0
    original = Path.read_bytes

    def observed(path: Path) -> bytes:
        nonlocal reads
        reads += 1
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", observed)
    catalog = SessionCommonModuleCatalog(source, profile="server")

    first = catalog.resolve_candidates(("Клиентский",))
    second = catalog.resolve_candidates(("КЛИЕНТСКИЙ",))

    assert first is second
    assert first.modules == ()
    assert reads == 1


def test_missing_candidate_refreshes_index_once_per_batch_and_discovers_later_file(
    tmp_path, monkeypatch
):
    source = _source_root(tmp_path)
    catalog = SessionCommonModuleCatalog(source, profile="server")
    catalog.ensure_initialized()
    refreshes = 0
    original_glob = Path.glob

    def observed(path: Path, pattern: str):
        nonlocal refreshes
        if path == source / "CommonModules" and pattern == "*.xml":
            refreshes += 1
        return original_glob(path, pattern)

    monkeypatch.setattr(Path, "glob", observed)

    before = catalog.resolve_candidates(("Поздний", "ЕщеОдинОтсутствующий"))
    _add_metadata(source, "Поздний")
    after = catalog.resolve_candidates(("Поздний",))

    assert before.modules == ()
    assert [item.canonical_name for item in after.modules] == ["Поздний"]
    assert refreshes == 2


def test_refresh_rejects_new_casefold_collision_before_payload_read(
    tmp_path, monkeypatch
):
    source = _source_root(tmp_path)
    first = _add_metadata(source, "Alpha")
    catalog = SessionCommonModuleCatalog(source, profile="server")
    catalog.ensure_initialized()
    collision = source / "CommonModules" / "ALPHA.xml"
    if collision != first:
        collision.write_text("<broken", encoding="utf-8")
    original_glob = Path.glob
    reads: list[Path] = []

    def colliding_glob(path: Path, pattern: str):
        if path == source / "CommonModules" and pattern == "*.xml":
            return iter((first, collision))
        return original_glob(path, pattern)

    def observed_read(path: Path) -> bytes:
        reads.append(path)
        return b""

    monkeypatch.setattr(Path, "glob", colliding_glob)
    monkeypatch.setattr(Path, "read_bytes", observed_read)

    with pytest.raises(ProtocolError, match="duplicate common module identity"):
        catalog.resolve_candidates(("Отсутствующий", "Alpha"))

    assert reads == []


@pytest.mark.parametrize(
    ("server", "client"),
    [(False, True), (False, False)],
)
def test_non_server_candidate_is_ignored_but_required_identity_fails(
    tmp_path, server, client
):
    source = _source_root(tmp_path)
    _add_metadata(source, "Клиентский", server=server, client=client)
    catalog = SessionCommonModuleCatalog(source, profile="server")

    assert catalog.resolve_candidates(("Клиентский",)).modules == ()
    with pytest.raises(ProtocolError, match="missing common module is unsupported"):
        catalog.ensure_modules(("Клиентский",))


def test_missing_candidate_is_ignored_but_required_identity_fails(tmp_path):
    catalog = SessionCommonModuleCatalog(_source_root(tmp_path), profile="server")

    assert catalog.resolve_candidates(("Отсутствует",)).modules == ()
    with pytest.raises(ProtocolError, match="missing common module is unsupported"):
        catalog.ensure_modules(("Отсутствует",))


def test_required_late_server_module_is_discovered_without_restart(tmp_path):
    source = _source_root(tmp_path)
    catalog = SessionCommonModuleCatalog(source, profile="server")

    with pytest.raises(ProtocolError, match="missing common module is unsupported"):
        catalog.ensure_modules(("Поздний",))
    _add_metadata(source, "Поздний")

    snapshot = catalog.ensure_modules(("Поздний",))

    assert [item.canonical_name for item in snapshot.modules] == ["Поздний"]
