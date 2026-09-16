"""Target-neutral symbolic capture resolution for exported 1C source."""

from __future__ import annotations

import re
import os
from collections import OrderedDict
from threading import RLock
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Protocol
from uuid import UUID
from xml.etree import ElementTree

from onec_runtime.errors import ProtocolError
from onec_runtime.kernel import COMMON_MODULE_PROPERTY_ID, OBJECT_MODULE_PROPERTY_ID
from onec_runtime.configuration_source import (
    ConfigurationSourceLayout,
    SourceLayer,
    SourceRootBinding,
    SourceTreeLayout,
    local_name,
    reject_links,
)
from onec_runtime.rdbg.models import ModuleLocation

MAX_SOURCE_EXCERPT = 4_096
_DECLARATION = re.compile(
    r"^\s*(Процедура|Функция)\s+([\w\u0400-\u04ff]+)\s*\(",
    re.IGNORECASE,
)


def _identifier(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError(f"{name} must be a bounded identifier")
    if not value.isidentifier():
        raise ValueError(f"{name} must be an identifier without expressions")
    return value


def _qualified_identifier(value: object, *, name: str) -> str:
    if not isinstance(value, str) or len(value) > 512:
        raise ValueError(f"{name} must be bounded")
    parts = value.split(".")
    if not parts:
        raise ValueError(f"{name} must be a qualified identifier")
    for part in parts:
        _identifier(part, name=name)
    return value


def _positive(value: object, *, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _sha256(value: object, *, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")
    return value


def _mapping(value: object, *, name: str, allowed: set[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) - allowed:
        raise ValueError(f"{name} has unsupported fields")
    return value


@dataclass(frozen=True, slots=True)
class CaptureSourceConfig:
    project: str
    source_root: Path | str
    layer: SourceLayer | str = SourceLayer.AUTO
    extension_name: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_root", Path(self.source_root).absolute())
        try:
            layer = SourceLayer(self.layer)
        except ValueError as error:
            raise ProtocolError("configuration source layer is invalid") from error
        if layer == SourceLayer.EXTENSION and not self.extension_name:
            raise ProtocolError("configuration extension requires an explicit name")
        object.__setattr__(self, "layer", layer)


@dataclass(frozen=True, slots=True)
class CapturePointRequest:
    name: str
    project: str = ""
    module: str = ""
    procedure: str = ""
    line: int | None = None
    source_fragment: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.name, name="capture point name")
        has_location = any(
            value not in {"", None}
            for value in (self.project, self.module, self.procedure, self.line)
        )
        if has_location:
            _identifier(self.project, name="project")
            _qualified_identifier(self.module, name="module")
            _identifier(self.procedure, name="procedure")
            _positive(self.line, name="line")
        if self.source_fragment is not None and (
            not isinstance(self.source_fragment, str)
            or not self.source_fragment.strip()
            or len(self.source_fragment) > MAX_SOURCE_EXCERPT
        ):
            raise ValueError("source_fragment must be a bounded non-empty string")
        if not has_location and self.source_fragment is None:
            raise ValueError(
                "capture point requires a source location or stable fragment"
            )

    @classmethod
    def from_wire(cls, value: object) -> CapturePointRequest:
        raw = _mapping(
            value,
            name="capture point request",
            allowed={
                "name",
                "project",
                "module",
                "procedure",
                "line",
                "source_fragment",
            },
        )
        return cls(**dict(raw))  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class ResolvedCapturePoint:
    name: str
    project: str
    module: str
    procedure: str
    line: int
    source_revision: int
    source_sha256: str
    executable_line: int
    excerpt: str

    def __post_init__(self) -> None:
        CapturePointRequest(
            name=self.name,
            project=self.project,
            module=self.module,
            procedure=self.procedure,
            line=self.line,
        )
        _positive(self.source_revision, name="source_revision")
        _sha256(self.source_sha256, name="source_sha256")
        _positive(self.executable_line, name="executable_line")
        if (
            not isinstance(self.excerpt, str)
            or not self.excerpt.strip()
            or len(self.excerpt) > MAX_SOURCE_EXCERPT
        ):
            raise ValueError("excerpt must be a bounded non-empty string")

    @classmethod
    def from_wire(cls, value: object) -> ResolvedCapturePoint:
        raw = _mapping(
            value,
            name="resolved capture point",
            allowed={
                "name",
                "project",
                "module",
                "procedure",
                "line",
                "source_revision",
                "source_sha256",
                "executable_line",
                "excerpt",
            },
        )
        return cls(**dict(raw))  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class CaptureBinding:
    point: ResolvedCapturePoint
    location: ModuleLocation


class CapturePointResolver(Protocol):
    def resolve(
        self, points: Sequence[CapturePointRequest]
    ) -> tuple[CaptureBinding, ...]: ...

    def verify(self, bindings: Sequence[CaptureBinding]) -> None: ...


@dataclass(frozen=True, slots=True)
class _ModuleSource:
    name: str
    path: Path
    object_id: UUID
    content: bytes
    text: str

    @property
    def digest(self) -> str:
        return sha256(self.content).hexdigest()


class CommonModuleCaptureResolver:
    """Resolve stable source coordinates in an exported 1C source tree."""

    def __init__(self, project: str, source_root: Path) -> None:
        if not isinstance(project, str) or not project.isidentifier():
            raise ValueError("project must be an identifier")
        self.project = project
        self.source_root = Path(source_root).absolute()
        reject_links(self.source_root)
        self._layout: ConfigurationSourceLayout | None = None
        self.extension_name = ""
        if self.source_root.exists():
            self._layout = ConfigurationSourceLayout(self.source_root)
            self.source_root = self._layout.normalized_root
            if any(
                path.is_file()
                for path in (
                    self.source_root / "Configuration.xml",
                    self.source_root / "Configuration" / "Configuration.mdo",
                )
            ):
                self.extension_name = self._layout.bind(project).extension_name or ""

    def resolve(
        self, points: Sequence[CapturePointRequest]
    ) -> tuple[CaptureBinding, ...]:
        result: list[CaptureBinding] = []
        for request in points:
            if (
                request.project
                and request.project.casefold() != self.project.casefold()
            ):
                raise ValueError(
                    "capture point project does not match resolver project"
                )
            if request.module:
                source = self._load_module(request.module)
                line, procedure = self._resolve_in_module(source, request)
            else:
                source, line, procedure = self._resolve_fragment_globally(request)
            excerpt = source.text.splitlines()[line - 1].strip()
            resolved = ResolvedCapturePoint(
                name=request.name,
                project=self.project,
                module=source.name,
                procedure=procedure,
                line=line,
                source_revision=1,
                source_sha256=source.digest,
                executable_line=line,
                excerpt=excerpt,
            )
            result.append(
                CaptureBinding(
                    resolved,
                    ModuleLocation(
                        module_type=(
                            "ExtensionModule" if self.extension_name else "ConfigModule"
                        ),
                        url="",
                        object_id=source.object_id,
                        property_id=UUID(COMMON_MODULE_PROPERTY_ID),
                        line=line,
                        extension_name=self.extension_name,
                    ),
                )
            )
        return tuple(result)

    def resolve_module_line(self, module_name: str, line: int) -> ModuleLocation:
        """Resolve a common-module source line to its debugger location."""
        if type(line) is not int or line < 1:
            raise ValueError("capture point line must be positive")
        source = self._load_module(module_name)
        lines = source.text.splitlines()
        if line > len(lines):
            raise ValueError("capture point line is outside module source")
        excerpt = lines[line - 1].strip()
        if (
            not excerpt
            or excerpt.startswith("//")
            or _DECLARATION.match(excerpt)
            or excerpt.casefold() in {"конецпроцедуры", "конецфункции"}
        ):
            raise ValueError("capture point line is not executable source")
        return ModuleLocation(
            module_type="ExtensionModule" if self.extension_name else "ConfigModule",
            url="",
            object_id=source.object_id,
            property_id=UUID(COMMON_MODULE_PROPERTY_ID),
            line=line,
            extension_name=self.extension_name,
        )

    def verify(self, bindings: Sequence[CaptureBinding]) -> None:
        for binding in bindings:
            source = self._load_module(binding.point.module)
            if source.digest != binding.point.source_sha256:
                raise ProtocolError("capture source changed after resolution")

    def _resolve_in_module(
        self,
        source: _ModuleSource,
        request: CapturePointRequest,
    ) -> tuple[int, str]:
        regions = self._procedure_regions(source.text)
        expected = request.procedure.casefold()
        matching = [region for name, *region in regions if name.casefold() == expected]
        if len(matching) != 1:
            raise ValueError("capture point procedure was not found exactly once")
        start, end = matching[0]
        if request.source_fragment is not None:
            matches = self._fragment_lines(
                source.text, request.source_fragment, start, end
            )
            if len(matches) != 1:
                raise ValueError("capture source fragment must occur exactly one time")
            line = matches[0]
        else:
            assert request.line is not None
            line = request.line
        if not start <= line <= end:
            raise ValueError("capture point line is outside the named procedure")
        excerpt = source.text.splitlines()[line - 1].strip()
        if not excerpt or excerpt.startswith("//"):
            raise ValueError("capture point line is not executable source")
        return line, request.procedure

    def _resolve_fragment_globally(
        self,
        request: CapturePointRequest,
    ) -> tuple[_ModuleSource, int, str]:
        assert request.source_fragment is not None
        matches: list[tuple[_ModuleSource, int, str]] = []
        for module_dir in self._common_modules().iterdir():
            if not module_dir.is_dir():
                continue
            try:
                source = self._load_module(module_dir.name)
            except ProtocolError:
                continue
            for procedure, start, end in self._procedure_regions(source.text):
                for line in self._fragment_lines(
                    source.text, request.source_fragment, start, end
                ):
                    matches.append((source, line, procedure))
        if len(matches) != 1:
            raise ValueError("capture source fragment must occur exactly one time")
        return matches[0]

    @staticmethod
    def _fragment_lines(text: str, fragment: str, start: int, end: int) -> list[int]:
        return [
            number
            for number, line in enumerate(text.splitlines(), start=1)
            if start <= number <= end and fragment.strip() in line
        ]

    @staticmethod
    def _procedure_regions(text: str) -> tuple[tuple[str, int, int], ...]:
        lines = text.splitlines()
        regions: list[tuple[str, int, int]] = []
        active: tuple[str, int, str] | None = None
        for number, line in enumerate(lines, start=1):
            match = _DECLARATION.match(line)
            if match is not None:
                if active is not None:
                    raise ProtocolError("nested common-module procedure declaration")
                kind = match.group(1).casefold()
                terminator = "конецпроцедуры" if kind == "процедура" else "конецфункции"
                active = (match.group(2), number, terminator)
                continue
            if active is not None and line.strip().casefold() == active[2]:
                regions.append((active[0], active[1], number))
                active = None
        if active is not None:
            raise ProtocolError("common-module procedure has no terminator")
        return tuple(regions)

    def _common_modules(self) -> Path:
        path = self.source_root / "CommonModules"
        if not path.is_dir():
            raise ProtocolError("common-module source root is missing")
        return path

    def _load_module(self, module_name: str) -> _ModuleSource:
        _qualified_identifier(module_name, name="module")
        leaf_name = module_name.rsplit(".", 1)[-1]
        if self._layout is None:
            self._layout = ConfigurationSourceLayout(self.source_root)
            self.source_root = self._layout.normalized_root
        metadata_path = self._layout.metadata_path("CommonModules", leaf_name)
        source_path = self._layout.module_path("CommonModules", leaf_name, "Module")
        try:
            root = ElementTree.parse(metadata_path).getroot()
            candidates = [
                element
                for element in root.iter()
                if element.tag.rsplit("}", 1)[-1] == "CommonModule"
            ]
            owner = root if "uuid" in root.attrib else candidates[0]
            object_id = UUID(owner.attrib["uuid"])
            content = source_path.read_bytes()
            text = content.decode("utf-8-sig")
        except (
            OSError,
            UnicodeDecodeError,
            ElementTree.ParseError,
            IndexError,
            KeyError,
            ValueError,
        ) as error:
            raise ProtocolError(
                f"common module source is invalid: {module_name}"
            ) from error
        return _ModuleSource(module_name, source_path, object_id, content, text)


__all__ = [
    "CaptureBinding",
    "CapturePointRequest",
    "CapturePointResolver",
    "CaptureSourceConfig",
    "CaptureModuleResolver",
    "CaptureModuleSource",
    "CaptureSourceCatalog",
    "CaptureSourceChangedError",
    "CaptureSourceUnavailable",
    "SourceVersionRef",
    "CommonModuleCaptureResolver",
    "ResolvedCapturePoint",
]


class CaptureSourceChangedError(ProtocolError):
    """The trusted export no longer has its pinned file signature."""


@dataclass(frozen=True, slots=True)
class SourceVersionRef:
    """Immutable Worker text or a trusted export's observable file version.

    File signatures are deliberately not runtime verification. Equal-sized
    replacement content preserving modification time is outside this guarantee.
    """

    source_status: str
    path: Path | None = None
    size: int | None = None
    mtime_ns: int | None = None
    artifact_id: str | None = None
    generation: int | None = None
    source_sha256: str | None = None
    _source_text: str | None = None

    @classmethod
    def trusted_export(cls, path: Path) -> SourceVersionRef:
        reject_links(path)
        signature = path.stat()
        if not path.is_file():
            raise ProtocolError("configuration source path is unavailable")
        return cls("trusted_export", path, signature.st_size, signature.st_mtime_ns)

    @classmethod
    def worker(
        cls, *, artifact_id: str, generation: int, source_text: str
    ) -> SourceVersionRef:
        if not artifact_id or type(generation) is not int or generation < 1:
            raise ValueError("Worker source pin requires artifact and generation")
        return cls(
            "runtime_verified",
            artifact_id=artifact_id,
            generation=generation,
            source_sha256=sha256(source_text.encode("utf-8")).hexdigest(),
            _source_text=source_text,
        )

    def read_text(self) -> str:
        if self.source_status == "runtime_verified":
            if self._source_text is None:
                raise ProtocolError("Worker source pin is unavailable")
            return self._source_text
        if self.path is None:
            raise ProtocolError("configuration source pin is unavailable")
        expected = (self.size, self.mtime_ns)
        try:
            reject_links(self.path)
            with self.path.open("rb") as stream:
                before = os.fstat(stream.fileno())
                if (before.st_size, before.st_mtime_ns) != expected:
                    raise CaptureSourceChangedError("source_changed")
                content = stream.read()
                after = os.fstat(stream.fileno())
                # Also detect replacement of the path while the old file was open.
                current = self.path.stat()
                reject_links(self.path)
                if (
                    (after.st_size, after.st_mtime_ns) != expected
                    or (current.st_size, current.st_mtime_ns) != expected
                    or len(content) != self.size
                ):
                    raise CaptureSourceChangedError("source_changed")
            return content.decode("utf-8-sig")
        except (OSError, UnicodeError, ProtocolError) as error:
            raise CaptureSourceChangedError("source_changed") from error


@dataclass(frozen=True, slots=True)
class CaptureModuleSource:
    binding: SourceRootBinding
    canonical_name: str
    module_kind: str
    module_role: str
    object_id: UUID
    property_id: UUID
    source_path: Path
    line: int
    source_version: SourceVersionRef


@dataclass(frozen=True, slots=True)
class CaptureSourceUnavailable:
    location: ModuleLocation
    reason: str = "source_unavailable"


@dataclass(frozen=True, slots=True)
class _ModuleDescription:
    name: str
    object_id: UUID
    kind: str
    metadata_path: Path
    signature: tuple[int, int]


# An explicit kind/property table prevents a UUID in a different module role
# from accidentally selecting a common-module source.
_MODULE_ROLES = {
    UUID(COMMON_MODULE_PROPERTY_ID): (
        "CommonModules",
        "CommonModule",
        "Module",
        "ОбщийМодуль",
        "Модуль",
    ),
    UUID(OBJECT_MODULE_PROPERTY_ID): (
        "Documents",
        "Document",
        "ObjectModule",
        "Документ",
        "МодульОбъекта",
    ),
}


class CaptureModuleResolver:
    """One bound configuration tree, scanned only for unresolved module kinds."""

    def __init__(self, config: CaptureSourceConfig, *, cache_limit: int = 1024) -> None:
        self.layout = ConfigurationSourceLayout(config.source_root)
        self.binding = self.layout.bind(
            config.project, layer=config.layer, extension_name=config.extension_name
        )
        self._descriptions: OrderedDict[Path, _ModuleDescription] = OrderedDict()
        self._cache_limit = cache_limit

    def _description(self, path: Path, kind: str) -> _ModuleDescription:
        path = self.layout.safe_path(path)
        stat = path.stat()
        signature = (stat.st_size, stat.st_mtime_ns)
        cached = self._descriptions.get(path)
        if cached is not None and cached.signature == signature:
            self._descriptions.move_to_end(path)
            return cached
        try:
            document = ElementTree.fromstring(path.read_bytes())
            owners = [node for node in document.iter() if local_name(node.tag) == kind]
            if len(owners) != 1:
                raise ValueError("metadata kind mismatch")
            owner = owners[0]
            object_id = UUID(owner.attrib["uuid"])
            if self.binding.layout == SourceTreeLayout.DESIGNER:
                properties = [
                    node for node in owner if local_name(node.tag) == "Properties"
                ]
                if len(properties) != 1:
                    raise ValueError("missing metadata properties")
                owner = properties[0]
            names = [
                node.text or ""
                for node in owner
                if local_name(node.tag).casefold() == "name"
            ]
            if len(names) != 1 or not names[0].isidentifier() or names[0] != path.stem:
                raise ValueError("metadata name mismatch")
            name = names[0]
        except (OSError, ValueError, KeyError, ElementTree.ParseError) as error:
            raise ProtocolError("configuration module metadata is invalid") from error
        result = _ModuleDescription(name, object_id, kind, path, signature)
        self._descriptions[path] = result
        self._descriptions.move_to_end(path)
        while len(self._descriptions) > self._cache_limit:
            self._descriptions.popitem(last=False)
        return result

    def resolve_batch(
        self, requests: Sequence[ModuleLocation]
    ) -> dict[tuple[UUID, UUID], _ModuleDescription]:
        missing = {(item.object_id, item.property_id) for item in requests}
        result: dict[tuple[UUID, UUID], _ModuleDescription] = {}
        for description in tuple(self._descriptions.values()):
            for property_id, (_directory, kind, *_role) in _MODULE_ROLES.items():
                key = (description.object_id, property_id)
                if description.kind == kind and key in missing:
                    result[key] = description
                    missing.remove(key)
                    self._descriptions.move_to_end(description.metadata_path)
        for property_id, (
            directory,
            kind,
            _role,
            _prefix,
            _suffix,
        ) in _MODULE_ROLES.items():
            wanted = {key for key in missing if key[1] == property_id}
            if not wanted:
                continue
            for path in self.layout.metadata_paths(directory):
                description = self._description(path, kind)
                key = (description.object_id, property_id)
                if key in wanted:
                    result[key] = description
                    wanted.remove(key)
                    if not wanted:
                        break
        return result

    def refresh(self) -> None:
        for path, description in tuple(self._descriptions.items()):
            try:
                self.layout.safe_path(path)
                stat = path.stat()
                unchanged = (stat.st_size, stat.st_mtime_ns) == description.signature
            except (OSError, ProtocolError):
                unchanged = False
            if not unchanged:
                del self._descriptions[path]

    def materialize(
        self, description: _ModuleDescription, location: ModuleLocation
    ) -> CaptureModuleSource:
        directory, kind, role, prefix, suffix = _MODULE_ROLES[location.property_id]
        path = self.layout.module_path(directory, description.name, role)
        return CaptureModuleSource(
            self.binding,
            f"{prefix}.{description.name}.{suffix}",
            kind,
            role,
            location.object_id,
            location.property_id,
            path,
            location.line,
            SourceVersionRef.trusted_export(path),
        )


class CaptureSourceCatalog:
    """Bounded session-local positive/negative mappings, with explicit refresh."""

    def __init__(
        self, configs: Sequence[CaptureSourceConfig], *, cache_limit: int = 1024
    ) -> None:
        if type(cache_limit) is not int or cache_limit < 1:
            raise ValueError("capture source cache limit must be positive")
        self._resolvers = tuple(
            CaptureModuleResolver(config, cache_limit=cache_limit) for config in configs
        )
        self._cache_limit = cache_limit
        self.generation = 1
        self._cache: OrderedDict[
            tuple[SourceRootBinding, int, str, UUID, UUID], _ModuleDescription | None
        ] = OrderedDict()
        self._lock = RLock()

    @property
    def cache_size(self) -> int:
        with self._lock:
            return len(self._cache)

    @property
    def bindings(self) -> tuple[SourceRootBinding, ...]:
        return tuple(resolver.binding for resolver in self._resolvers)

    def _key(
        self, resolver: CaptureModuleResolver, location: ModuleLocation
    ) -> tuple[SourceRootBinding, int, str, UUID, UUID]:
        return (
            resolver.binding,
            self.generation,
            location.module_type,
            location.object_id,
            location.property_id,
        )

    def _put(
        self,
        key: tuple[SourceRootBinding, int, str, UUID, UUID],
        value: _ModuleDescription | None,
    ) -> None:
        self._cache[key] = value
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_limit:
            self._cache.popitem(last=False)

    def refresh(self) -> None:
        with self._lock:
            self.generation += 1
            for resolver in self._resolvers:
                resolver.refresh()
            previous = tuple(self._cache.items())
            self._cache.clear()
            for (
                binding,
                _generation,
                module_type,
                object_id,
                property_id,
            ), description in previous:
                if description is None:
                    continue
                try:
                    reject_links(description.metadata_path)
                    stat = description.metadata_path.stat()
                except (OSError, ProtocolError):
                    continue
                if (stat.st_size, stat.st_mtime_ns) == description.signature:
                    self._put(
                        (binding, self.generation, module_type, object_id, property_id),
                        description,
                    )

    def resolve_modules(
        self, locations: Sequence[ModuleLocation]
    ) -> tuple[CaptureModuleSource | CaptureSourceUnavailable, ...]:
        with self._lock:
            chosen: list[CaptureModuleResolver | None] = []
            batches: dict[CaptureModuleResolver, list[ModuleLocation]] = {}
            descriptions: dict[
                tuple[SourceRootBinding, int, str, UUID, UUID],
                _ModuleDescription | None,
            ] = {}
            for location in locations:
                matches = (
                    [
                        resolver
                        for resolver in self._resolvers
                        if (
                            (resolver.binding.extension_name or "")
                            == location.extension_name
                            and location.module_type
                            == (
                                "ExtensionModule"
                                if resolver.binding.layer == SourceLayer.EXTENSION
                                else "ConfigModule"
                            )
                        )
                    ]
                    if location.property_id in _MODULE_ROLES
                    else []
                )
                # More than one configured project with this layer is ambiguous;
                # physical debugger coordinates do not carry project identity.
                resolver = matches[0] if len(matches) == 1 else None
                chosen.append(resolver)
                if resolver is None:
                    continue
                key = self._key(resolver, location)
                if key in self._cache:
                    descriptions[key] = self._cache[key]
                    self._cache.move_to_end(key)
                else:
                    batches.setdefault(resolver, []).append(location)
            for resolver, batch in batches.items():
                found = resolver.resolve_batch(batch)
                for location in batch:
                    key = self._key(resolver, location)
                    value = found.get((location.object_id, location.property_id))
                    descriptions[key] = value
                    self._put(key, value)
            result: list[CaptureModuleSource | CaptureSourceUnavailable] = []
            for resolver, location in zip(chosen, locations, strict=True):
                description = (
                    descriptions.get(self._key(resolver, location))
                    if resolver
                    else None
                )
                if resolver is None or description is None:
                    result.append(CaptureSourceUnavailable(location))
                else:
                    try:
                        result.append(resolver.materialize(description, location))
                    except (OSError, ProtocolError):
                        result.append(CaptureSourceUnavailable(location))
            return tuple(result)
