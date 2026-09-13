"""Current ZUP source admission helpers for acceptance and benchmarks."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from onec_runtime.errors import ProtocolError


ZUP_MODULE_NAMES = ("КадровыйУчет", "КадровыйУчетРасширенный")


@dataclass(frozen=True, slots=True, repr=False)
class ZupModuleSource:
    name: str
    source: str
    source_sha256: str
    source_bytes: int
    source_lines: int


@dataclass(frozen=True, slots=True, repr=False)
class ZupSourceBundle:
    units: tuple[ZupModuleSource, ...]
    dump_duration_ms: float

    def private_inventory(self) -> dict[str, object]:
        return {
            "module_names": [unit.name for unit in self.units],
            "source_sha256": {unit.name: unit.source_sha256 for unit in self.units},
            "source_bytes": {unit.name: unit.source_bytes for unit in self.units},
            "source_lines": {unit.name: unit.source_lines for unit in self.units},
        }


def _admission_error(category: str, name: str) -> ProtocolError:
    return ProtocolError(f"ZUP source inventory {category}: {name}")


def _named_module_directory(common_modules: Path, name: str) -> Path:
    try:
        matches = [
            entry
            for entry in common_modules.iterdir()
            if entry.name.casefold() == name.casefold()
        ]
    except OSError as error:
        raise _admission_error("unsafe", name) from error
    if not matches:
        raise _admission_error("missing", name)
    if len(matches) != 1:
        raise _admission_error("ambiguous", name)
    module_directory = matches[0]
    if (
        module_directory.name != name
        or module_directory.is_symlink()
        or not module_directory.is_dir()
    ):
        raise _admission_error("unsafe", name)
    return module_directory


def _read_named_module(common_modules: Path, name: str) -> ZupModuleSource:
    module_directory = _named_module_directory(common_modules, name)
    extension_directory = module_directory / "Ext"
    module_path = extension_directory / "Module.bsl"
    if extension_directory.is_symlink() or module_path.is_symlink():
        raise _admission_error("unsafe", name)
    if extension_directory.exists() and not extension_directory.is_dir():
        raise _admission_error("unsafe", name)
    if not extension_directory.is_dir():
        raise _admission_error("missing", name)
    if module_path.exists() and not module_path.is_file():
        raise _admission_error("unsafe", name)
    if not module_path.is_file():
        raise _admission_error("missing", name)
    try:
        module_files = tuple(module_directory.rglob("Module.bsl"))
    except OSError as error:
        raise _admission_error("unsafe", name) from error
    if module_files != (module_path,):
        raise _admission_error("ambiguous", name)
    try:
        source = module_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as error:
        raise _admission_error("unsafe", name) from error
    source_bytes = source.encode("utf-8")
    return ZupModuleSource(
        name=name,
        source=source,
        source_sha256=sha256(source_bytes).hexdigest(),
        source_bytes=len(source_bytes),
        source_lines=len(source.splitlines()),
    )


def admit_zup_source_bundle(root: Path) -> ZupSourceBundle:
    supplied_root = Path(root)
    if supplied_root.is_symlink() or not supplied_root.is_dir():
        raise _admission_error("unsafe", "bundle")
    common_modules = supplied_root / "CommonModules"
    if common_modules.is_symlink() or not common_modules.is_dir():
        raise _admission_error("unsafe", "bundle")
    return ZupSourceBundle(
        units=tuple(_read_named_module(common_modules, name) for name in ZUP_MODULE_NAMES),
        dump_duration_ms=0.0,
    )
