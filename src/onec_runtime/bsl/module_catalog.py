"""Lazy Designer/EDT common-module metadata catalog for one runtime session."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from threading import RLock
import xml.etree.ElementTree as ElementTree

from onec_runtime.configuration_source import ConfigurationSourceLayout
from onec_runtime.errors import ModuleUniverseAdmissionError, ProtocolError


_BSL_IDENTIFIER_RE = re.compile(r"[A-Za-zА-Яа-яЁё_][0-9A-Za-zА-Яа-яЁё_]*\Z")


class CommonModuleScope(str, Enum):
    SERVER = "server"
    CLIENT_SERVER = "client_server"


@dataclass(frozen=True, slots=True)
class CommonModuleDescriptor:
    canonical_name: str
    scope: CommonModuleScope


@dataclass(frozen=True, slots=True)
class CommonModuleCatalogSnapshot:
    profile: str
    preprocessor_profile: str
    revision: int
    modules: tuple[CommonModuleDescriptor, ...]
    sha256: str

    @classmethod
    def create(
        cls,
        *,
        profile: str,
        preprocessor_profile: str,
        revision: int,
        modules: Iterable[CommonModuleDescriptor],
    ) -> "CommonModuleCatalogSnapshot":
        ordered = tuple(sorted(modules, key=lambda item: item.canonical_name.casefold()))
        payload = json.dumps(
            {
                "profile": profile,
                "preprocessor_profile": preprocessor_profile,
                "modules": [
                    [item.canonical_name.casefold(), item.scope.value]
                    for item in ordered
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return cls(
            profile,
            preprocessor_profile,
            revision,
            ordered,
            sha256(payload).hexdigest(),
        )

    def require(self, name: str) -> CommonModuleDescriptor:
        by_name = {item.canonical_name.casefold(): item for item in self.modules}
        try:
            return by_name[name.casefold()]
        except (AttributeError, KeyError):
            raise ModuleUniverseAdmissionError(
                "common module is absent from catalog"
            ) from None


class SessionCommonModuleCatalog:
    def __init__(
        self,
        source_root: Path,
        *,
        profile: str,
        preprocessor_profile: str = "server",
    ) -> None:
        self._layout = ConfigurationSourceLayout(source_root)
        self._source_root = self._layout.normalized_root
        self._configured_root = self._layout.configured_root
        self._common_modules = self._layout.safe_path(
            self._source_root / "CommonModules"
        )
        if not self._common_modules.is_dir():
            raise ProtocolError("common-module source root is unsafe")
        self._profile = profile
        self._preprocessor_profile = preprocessor_profile
        self._snapshot: CommonModuleCatalogSnapshot | None = None
        self._path_index: dict[str, Path] | None = None
        self._resolved: dict[str, CommonModuleDescriptor | None] = {}
        self._lock = RLock()

    @property
    def initialized(self) -> bool:
        with self._lock:
            return self._snapshot is not None

    @property
    def source_root(self) -> Path:
        """Normalized Designer root or EDT src directory."""
        return self._source_root

    def read_worker_module_source(self, path: Path | str) -> tuple[str, str]:
        """Read one canonical Designer or EDT module under the bound root.

        A relative path may start at the configured EDT project root or its src.
        """
        if not isinstance(path, (Path, str)):
            raise ProtocolError("Worker module source path is invalid")
        supplied = Path(path)
        if supplied.is_absolute():
            candidate = supplied
        elif (
            self._configured_root != self._source_root
            and supplied.parts
            and supplied.parts[0].casefold() == "src"
        ):
            candidate = self._configured_root / supplied
        else:
            candidate = self._source_root / supplied
        try:
            relative = candidate.relative_to(self._source_root)
        except ValueError:
            raise ProtocolError("Worker module source path is outside source root") from None
        parts = relative.parts
        if (
            len(parts) == 4
            and parts[0].casefold() == "commonmodules"
            and parts[2].casefold() == "ext"
            and parts[3].casefold() == "module.bsl"
        ):
            name = parts[1]
            metadata = self._common_modules / f"{name}.xml"
        elif (
            len(parts) == 3
            and parts[0].casefold() == "commonmodules"
            and parts[2].casefold() == "module.bsl"
        ):
            name = parts[1]
            metadata = self._common_modules / name / f"{name}.mdo"
        else:
            raise ProtocolError("Worker module source path is invalid")
        if not _is_bsl_identifier(name):
            raise ProtocolError("Worker module source path is invalid")
        expected = self._layout.module_path("CommonModules", name, "Module")
        if candidate != expected:
            raise ProtocolError("Worker module source path does not match configured layout")
        metadata = self._layout.metadata_path("CommonModules", name)
        try:
            resolved = candidate.resolve(strict=True)
            if (
                resolved != candidate
                or not resolved.is_file()
                or not metadata.is_file()
                or _is_link(metadata)
            ):
                raise ProtocolError("Worker module source path is invalid")
            return name, resolved.read_bytes().decode("utf-8-sig")
        except (OSError, UnicodeError) as error:
            raise ProtocolError("Worker module source path is unreadable") from error

    def ensure_initialized(self) -> CommonModuleCatalogSnapshot:
        with self._lock:
            if self._snapshot is None:
                self._path_index = self._enumerate_path_index()
                self._snapshot = CommonModuleCatalogSnapshot.create(
                    profile=self._profile,
                    preprocessor_profile=self._preprocessor_profile,
                    revision=1,
                    modules=(),
                )
            return self._snapshot

    def resolve_candidates(self, names: Iterable[str]) -> CommonModuleCatalogSnapshot:
        """Resolve permissive bare roots, ignoring missing/non-server modules."""

        return self._resolve_names(names, required=False)

    def ensure_modules(self, names: Iterable[str]) -> CommonModuleCatalogSnapshot:
        """Resolve required loaded-module identities, failing closed."""

        return self._resolve_names(names, required=True)

    def _resolve_names(
        self,
        names: Iterable[str],
        *,
        required: bool,
    ) -> CommonModuleCatalogSnapshot:
        with self._lock:
            current = self.ensure_initialized()
            requested: dict[str, str] = {}
            for name in names:
                if not _is_bsl_identifier(name):
                    raise ProtocolError("common-module batch identity is invalid")
                requested.setdefault(name.casefold(), name)
            if not requested:
                return current

            staged_resolved = dict(self._resolved)
            staged_index = self._path_index or {}
            if any(
                normalized not in staged_index and normalized not in staged_resolved
                for normalized in requested
            ):
                staged_index = self._enumerate_path_index()
            staged_modules = {
                item.canonical_name.casefold(): item for item in current.modules
            }
            changed = False
            for normalized in requested:
                if normalized in staged_resolved:
                    descriptor = staged_resolved[normalized]
                else:
                    metadata_path = staged_index.get(normalized)
                    if metadata_path is None:
                        if required:
                            raise ProtocolError("missing common module is unsupported")
                        continue
                    descriptor = self._read_descriptor(metadata_path)
                    staged_resolved[normalized] = descriptor

                if descriptor is None:
                    if required:
                        raise ProtocolError("missing common module is unsupported")
                    continue
                if normalized not in staged_modules:
                    staged_modules[normalized] = descriptor
                    changed = True

            self._resolved = staged_resolved
            self._path_index = staged_index
            if not changed:
                return current
            candidate = CommonModuleCatalogSnapshot.create(
                profile=current.profile,
                preprocessor_profile=current.preprocessor_profile,
                revision=current.revision + 1,
                modules=staged_modules.values(),
            )
            self._snapshot = candidate
            return candidate

    def _enumerate_path_index(self) -> dict[str, Path]:
        index: dict[str, Path] = {}
        paths = list(self._common_modules.glob("*.xml"))
        with os.scandir(self._common_modules) as entries:
            for entry in entries:
                if entry.name.casefold().endswith(".xml"):
                    continue
                if entry.is_symlink():
                    raise ProtocolError("common-module source root is unsafe")
                if not entry.is_dir(follow_symlinks=False):
                    continue
                folder = Path(entry.path)
                if folder.is_junction():
                    raise ProtocolError("common-module source root is unsafe")
                # One level only: EDT metadata is Name/Name.mdo. Do not scan
                # module source files or descend into Designer's Ext directory.
                for path in folder.glob("*.mdo"):
                    if path.stem != folder.name:
                        raise ProtocolError("common-module metadata identity is invalid")
                    paths.append(path)
        for path in sorted(paths, key=lambda item: item.name.casefold()):
            if not _is_bsl_identifier(path.stem):
                raise ProtocolError("common-module metadata identity is invalid")
            normalized = path.stem.casefold()
            if normalized in index:
                raise ProtocolError("duplicate common module identity")
            index[normalized] = path
        return index

    def _read_descriptor(self, metadata_path: Path) -> CommonModuleDescriptor | None:
        return self._read_metadata(metadata_path)[1]

    def _read_metadata(
        self, metadata_path: Path
    ) -> tuple[str, CommonModuleDescriptor | None]:
        resolved = self._safe_metadata_path(metadata_path)
        try:
            document = ElementTree.fromstring(resolved.read_bytes())
        except (OSError, ElementTree.ParseError) as error:
            raise ProtocolError("common-module metadata is invalid") from error
        edt = resolved.suffix.casefold() == ".mdo"
        properties = (
            document if _local_name(document.tag) == "CommonModule" else None
        ) if edt else _properties_element(document)
        if properties is None:
            raise ProtocolError("common-module metadata is invalid")
        name = _text_property(properties, "name" if edt else "Name")
        if (
            name is None
            or not _is_bsl_identifier(name)
            or resolved.stem != name
        ):
            raise ProtocolError("common-module metadata identity is invalid")
        if edt:
            # EDT omits boolean properties at their default false value.
            server = _bool_property(properties, "server", default=False)
            global_module = _bool_property(properties, "global", default=False)
            managed = _bool_property(properties, "clientManagedApplication", default=False)
            ordinary = _bool_property(properties, "clientOrdinaryApplication", default=False)
            client = managed or ordinary
        else:
            server = _bool_property(properties, "Server")
            global_module = _bool_property(properties, "Global")
            client = _bool_property(properties, "ClientManagedApplication") or _bool_property(
                properties, "ClientOrdinaryApplication"
            )
        if not server or global_module:
            return name, None
        return name, CommonModuleDescriptor(
            canonical_name=name,
            scope=(
                CommonModuleScope.CLIENT_SERVER
                if client
                else CommonModuleScope.SERVER
            ),
        )

    def _safe_metadata_path(self, metadata_path: Path) -> Path:
        path = Path(metadata_path)
        designer_path = path.suffix.casefold() == ".xml" and path.parent == self._common_modules
        edt_path = (
            path.suffix.casefold() == ".mdo"
            and path.parent.parent == self._common_modules
            and path.stem == path.parent.name
        )
        if _is_link(path) or _is_link(path.parent) or not (designer_path or edt_path):
            raise ProtocolError("common-module source root is unsafe")
        try:
            resolved = path.resolve(strict=True)
        except (FileNotFoundError, OSError) as error:
            raise ProtocolError("common-module source root is unsafe") from error
        if resolved.parent != path.parent or not resolved.is_file():
            raise ProtocolError("common-module source root is unsafe")
        return resolved


def _local_name(tag: object) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _properties_element(document: ElementTree.Element) -> ElementTree.Element | None:
    for common_module in document.iter():
        if _local_name(common_module.tag) != "CommonModule":
            continue
        for child in common_module:
            if _local_name(child.tag) == "Properties":
                return child
    return None


def _text_property(properties: ElementTree.Element, name: str) -> str | None:
    values = [child.text or "" for child in properties if _local_name(child.tag) == name]
    if len(values) > 1:
        raise ProtocolError("common-module metadata is invalid")
    return values[0] if values else None


def _bool_property(
    properties: ElementTree.Element, name: str, *, default: bool | None = None,
) -> bool:
    value = _text_property(properties, name)
    if value is None:
        if default is not None:
            return default
        raise ProtocolError("common-module metadata is invalid")
    if value.strip().casefold() == "true":
        return True
    if value.strip().casefold() == "false":
        return False
    raise ProtocolError("common-module metadata is invalid")


def _is_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _is_bsl_identifier(value: object) -> bool:
    return isinstance(value, str) and bool(_BSL_IDENTIFIER_RE.fullmatch(value))
