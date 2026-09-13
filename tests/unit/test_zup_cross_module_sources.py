from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from integration.support import zup_sources as sources
from onec_runtime.errors import ProtocolError


def write_module(root: Path, name: str, payload: bytes) -> None:
    module = root / "CommonModules" / name / "Ext" / "Module.bsl"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_bytes(payload)


def write_named_modules(root: Path) -> None:
    write_module(root, "КадровыйУчет", b"private-unit-one\n")
    write_module(root, "КадровыйУчетРасширенный", b"private-unit-two\n")


def test_admit_zup_source_bundle_reads_only_named_modules(tmp_path: Path) -> None:
    dump = tmp_path / "dump"
    write_named_modules(dump)
    write_module(dump, "Посторонний", b"unrelated-module\n")

    bundle = sources.admit_zup_source_bundle(dump)

    assert tuple(unit.name for unit in bundle.units) == sources.ZUP_MODULE_NAMES
    assert all(
        unit.source_sha256 == sha256(unit.source.encode()).hexdigest()
        for unit in bundle.units
    )
    assert "Посторонний" not in str(bundle.private_inventory())


def test_admission_tolerates_utf8_bom_and_counts_source_inventory(tmp_path: Path) -> None:
    dump = tmp_path / "dump"
    write_module(dump, "КадровыйУчет", b"\xef\xbb\xbfprivate-one\n")
    write_module(dump, "КадровыйУчетРасширенный", b"private-two")

    bundle = sources.admit_zup_source_bundle(dump)

    assert bundle.units[0].source == "private-one\n"
    assert bundle.units[0].source_bytes == len(b"private-one\n")
    assert bundle.units[0].source_lines == 1
    assert bundle.units[1].source_lines == 1


def test_admission_rejects_missing_named_module_without_source_disclosure(
    tmp_path: Path,
) -> None:
    dump = tmp_path / "dump"
    write_module(dump, "КадровыйУчет", b"sensitive-marker")

    with pytest.raises(ProtocolError, match="missing.*КадровыйУчетРасширенный") as error:
        sources.admit_zup_source_bundle(dump)

    assert "sensitive-marker" not in str(error.value)


def test_admission_rejects_ambiguous_named_module_inventory(tmp_path: Path) -> None:
    dump = tmp_path / "dump"
    write_named_modules(dump)
    duplicate = dump / "CommonModules" / "КадровыйУчет" / "Duplicate" / "Module.bsl"
    duplicate.parent.mkdir(parents=True)
    duplicate.write_bytes(b"duplicate")

    with pytest.raises(ProtocolError, match="ambiguous.*КадровыйУчет"):
        sources.admit_zup_source_bundle(dump)


def test_admission_rejects_malformed_named_module_as_unsafe(tmp_path: Path) -> None:
    dump = tmp_path / "dump"
    write_named_modules(dump)
    extension_directory = dump / "CommonModules" / "КадровыйУчет" / "Ext"
    (extension_directory / "Module.bsl").unlink()
    extension_directory.rmdir()
    extension_directory.write_bytes(b"not-a-directory")

    with pytest.raises(ProtocolError, match="unsafe.*КадровыйУчет"):
        sources.admit_zup_source_bundle(dump)
