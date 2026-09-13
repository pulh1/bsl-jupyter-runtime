"""Target-neutral symbolic capture resolution for exported 1C source."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Protocol
from uuid import UUID
from xml.etree import ElementTree

from onec_runtime.errors import ProtocolError
from onec_runtime.kernel import COMMON_MODULE_PROPERTY_ID
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

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_root", Path(self.source_root).resolve())


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
        self.source_root = Path(source_root).resolve()
        self.extension_name = self._designer_extension_name()

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
                            "ExtensionModule"
                            if self.extension_name
                            else "ConfigModule"
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

    def _designer_extension_name(self) -> str:
        configuration_path = self.source_root / "Configuration.xml"
        if not configuration_path.is_file():
            return ""
        try:
            root = ElementTree.parse(configuration_path).getroot()
            configuration = next(
                element
                for element in root.iter()
                if element.tag.rsplit("}", 1)[-1] == "Configuration"
            )
            properties = next(
                element
                for element in configuration
                if element.tag.rsplit("}", 1)[-1] == "Properties"
            )
            values = {
                element.tag.rsplit("}", 1)[-1]: element.text or ""
                for element in properties
            }
        except (OSError, ElementTree.ParseError, StopIteration) as error:
            raise ProtocolError(
                f"configuration source is invalid: {configuration_path}"
            ) from error
        if "ConfigurationExtensionPurpose" not in values:
            return ""
        name = values.get("Name", "")
        try:
            return _identifier(name, name="extension name")
        except ValueError as error:
            raise ProtocolError(
                f"configuration extension name is invalid: {configuration_path}"
            ) from error

    def _common_modules(self) -> Path:
        path = self.source_root / "CommonModules"
        if not path.is_dir():
            raise ProtocolError("common-module source root is missing")
        return path

    def _load_module(self, module_name: str) -> _ModuleSource:
        leaf_name = module_name.rsplit(".", 1)[-1]
        module_root = self._common_modules() / leaf_name
        edt_metadata = module_root / f"{leaf_name}.mdo"
        edt_source = module_root / "Module.bsl"
        designer_metadata = self._common_modules() / f"{leaf_name}.xml"
        designer_source = module_root / "Ext" / "Module.bsl"
        if edt_metadata.is_file() and edt_source.is_file():
            metadata_path, source_path = edt_metadata, edt_source
        elif designer_metadata.is_file() and designer_source.is_file():
            metadata_path, source_path = designer_metadata, designer_source
        else:
            raise ProtocolError(f"common module source is missing: {module_name}")
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
    "CommonModuleCaptureResolver",
    "ResolvedCapturePoint",
]
