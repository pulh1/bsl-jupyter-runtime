"""Shared, shallow Designer/EDT layout and configuration-layer binding."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path
from collections.abc import Iterator
from xml.etree import ElementTree

from onec_runtime.errors import ProtocolError


class SourceTreeLayout(str, Enum):
    DESIGNER = "designer"
    EDT = "edt"


class SourceLayer(str, Enum):
    AUTO = "auto"
    BASE = "base"
    EXTENSION = "extension"


@dataclass(frozen=True, slots=True)
class SourceRootBinding:
    project: str
    configured_root: Path
    normalized_root: Path
    layout: SourceTreeLayout
    layer: SourceLayer
    extension_name: str | None


# Only metadata kinds with a supported physical module role are traversed.
METADATA_DIRECTORIES = ("CommonModules", "Documents")


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def reject_links(path: Path) -> None:
    """Check lexical ancestry before resolve can erase a link/junction."""
    if ".." in path.parts:
        raise ProtocolError("configuration source root is unsafe")
    for component in (path, *path.parents):
        if component.is_symlink() or component.is_junction():
            raise ProtocolError("configuration source root is unsafe")


class ConfigurationSourceLayout:
    def __init__(self, source_root: Path | str) -> None:
        supplied = Path(source_root).absolute()
        reject_links(supplied)
        try:
            configured = supplied.resolve(strict=True)
        except OSError as error:
            raise ProtocolError("configuration source root is unsafe") from error
        if not configured.is_dir():
            raise ProtocolError("configuration source root is unsafe")
        nested = configured / "src"
        reject_links(nested)
        for root in (configured, nested):
            for name in (*METADATA_DIRECTORIES, "Configuration", "Configuration.xml"):
                reject_links(root / name)
        direct_tree = self._has_tree(configured)
        nested_tree = self._has_tree(nested)
        if direct_tree and nested_tree:
            raise ProtocolError("configuration source root is ambiguous")
        if not direct_tree and not nested_tree:
            raise ProtocolError("configuration source root is missing")
        self.configured_root = configured
        self.normalized_root = nested if nested_tree else configured
        root = self.normalized_root
        designer = (root / "Configuration.xml").is_file()
        edt = (root / "Configuration" / "Configuration.mdo").is_file()
        self._has_native_metadata = designer or edt
        if designer and edt:
            raise ProtocolError("configuration source root is ambiguous")
        # Metadata-only legacy exports lack Configuration metadata. Detect their
        # layout from one shallow entry; never parse or index module payloads.
        if not designer and not edt:
            edt = nested_tree
            for directory in METADATA_DIRECTORIES:
                folder = root / directory
                if not folder.is_dir():
                    continue
                with os.scandir(folder) as entries:
                    for entry in entries:
                        if entry.name.endswith(".xml"):
                            designer = True
                            break
                        if entry.is_dir(follow_symlinks=False):
                            name = entry.name
                            if (folder / name / f"{name}.mdo").is_file():
                                edt = True
                                break
                            if next((folder / name).glob("*.mdo"), None) is not None:
                                edt = True
                                break
                if designer or edt:
                    break
        self.layout = SourceTreeLayout.EDT if edt else SourceTreeLayout.DESIGNER

    @staticmethod
    def _has_tree(root: Path) -> bool:
        return any((root / name).is_dir() for name in METADATA_DIRECTORIES) or (
            (root / "Configuration.xml").is_file()
            or (root / "Configuration" / "Configuration.mdo").is_file()
        )

    def safe_path(self, path: Path, *, must_exist: bool = True) -> Path:
        if not path.is_relative_to(self.normalized_root):
            raise ProtocolError("configuration source root is unsafe")
        reject_links(path)
        try:
            resolved = path.resolve(strict=must_exist)
        except OSError as error:
            raise ProtocolError("configuration source path is unavailable") from error
        if resolved != path or not resolved.is_relative_to(self.normalized_root):
            raise ProtocolError("configuration source root is unsafe")
        return resolved

    def bind(
        self,
        project: str,
        *,
        layer: SourceLayer | str = SourceLayer.AUTO,
        extension_name: str | None = None,
    ) -> SourceRootBinding:
        if not isinstance(project, str) or not project.isidentifier():
            raise ProtocolError("configuration source project is invalid")
        try:
            requested = SourceLayer(layer)
        except ValueError as error:
            raise ProtocolError("configuration source layer is invalid") from error
        if requested == SourceLayer.EXTENSION and not extension_name:
            raise ProtocolError("configuration extension requires an explicit name")
        root = self.normalized_root
        path = (
            root / "Configuration.xml"
            if self.layout == SourceTreeLayout.DESIGNER
            else root / "Configuration" / "Configuration.mdo"
        )
        path = self.safe_path(path)
        try:
            document = ElementTree.fromstring(path.read_bytes())
            owners = [
                node
                for node in document.iter()
                if local_name(node.tag) == "Configuration"
            ]
            if len(owners) != 1:
                raise ValueError("configuration identity is ambiguous")
            owner = owners[0]
            if self.layout == SourceTreeLayout.DESIGNER:
                properties = [
                    node for node in owner if local_name(node.tag) == "Properties"
                ]
                if len(properties) != 1:
                    raise ValueError("configuration properties are missing")
                owner = properties[0]
            values: dict[str, str] = {}
            for node in owner:
                key = local_name(node.tag).casefold()
                # Native EDT repeats collection children. Only these scalar
                # fields participate in configuration-layer identity.
                if key not in {"name", "configurationextensionpurpose"}:
                    continue
                if key in values:
                    raise ValueError("duplicate configuration property")
                values[key] = node.text or ""
            discovered = (
                SourceLayer.EXTENSION
                if "configurationextensionpurpose" in values
                else SourceLayer.BASE
            )
            name = values.get("name") if discovered == SourceLayer.EXTENSION else None
            if name is not None and not name.isidentifier():
                raise ValueError("invalid extension name")
            if discovered == SourceLayer.EXTENSION and not name:
                raise ValueError("missing extension name")
        except (OSError, ValueError, ElementTree.ParseError) as error:
            raise ProtocolError("configuration source metadata is invalid") from error
        if requested != SourceLayer.AUTO and requested != discovered:
            raise ProtocolError("configuration source layer does not match metadata")
        if extension_name is not None and extension_name != name:
            raise ProtocolError("configuration extension name does not match metadata")
        return SourceRootBinding(
            project, self.configured_root, root, self.layout, discovered, name
        )

    def metadata_candidates(
        self,
        directory: str,
        *,
        streaming: bool = False,
    ) -> Iterator[Path]:
        yield from self._metadata_candidates(
            directory, self.layout, streaming=streaming
        )

    def legacy_metadata_alternates(self, directory: str) -> Iterator[Path]:
        """Additional names for legacy collision checks, never for admission."""
        if not self._has_native_metadata:
            alternate = (
                SourceTreeLayout.EDT
                if self.layout == SourceTreeLayout.DESIGNER
                else SourceTreeLayout.DESIGNER
            )
            yield from self._metadata_candidates(directory, alternate)

    def _metadata_candidates(
        self,
        directory: str,
        tree_layout: SourceTreeLayout,
        *,
        streaming: bool = False,
    ) -> Iterator[Path]:
        """Enumerate selected-format names without opening metadata payloads.

        Callers must validate a yielded path before reading it. In particular,
        Designer indexing does not stat or resolve every XML candidate.
        """
        if directory not in METADATA_DIRECTORIES:
            raise ProtocolError("configuration metadata kind is unsupported")
        folder = self.normalized_root / directory
        if tree_layout == SourceTreeLayout.DESIGNER and not streaming:
            yield from folder.glob("*.xml")
            return
        with os.scandir(folder) as entries:
            for entry in entries:
                if tree_layout == SourceTreeLayout.DESIGNER:
                    if Path(entry.name).match("*.xml"):
                        yield folder / entry.name
                    continue
                if entry.is_symlink():
                    raise ProtocolError("configuration source root is unsafe")
                if not entry.is_dir(follow_symlinks=False):
                    continue
                module = folder / entry.name
                if module.is_junction():
                    raise ProtocolError("configuration source root is unsafe")
                for metadata in module.glob("*.mdo"):
                    if metadata.stem != module.name:
                        raise ProtocolError(
                            "common-module metadata identity is invalid"
                        )
                    yield metadata

    def metadata_paths(self, directory: str) -> Iterator[Path]:
        if directory not in METADATA_DIRECTORIES:
            raise ProtocolError("configuration metadata kind is unsupported")
        folder = self.safe_path(self.normalized_root / directory, must_exist=False)
        if not folder.is_dir():
            return
        for path in self.metadata_candidates(directory, streaming=True):
            yield self.safe_path(path)

    def metadata_path(self, directory: str, name: str) -> Path:
        if directory not in METADATA_DIRECTORIES or not name.isidentifier():
            raise ProtocolError("configuration module identity is invalid")
        folder = self.normalized_root / directory
        path = (
            folder / f"{name}.xml"
            if self.layout == SourceTreeLayout.DESIGNER
            else folder / name / f"{name}.mdo"
        )
        return self.safe_path(path)

    def module_path(self, directory: str, name: str, role: str) -> Path:
        if (
            directory not in METADATA_DIRECTORIES
            or not name.isidentifier()
            or role not in {"Module", "ObjectModule"}
        ):
            raise ProtocolError("configuration module identity is invalid")
        folder = self.normalized_root / directory / name
        if self.layout == SourceTreeLayout.DESIGNER:
            folder /= "Ext"
        return self.safe_path(folder / f"{role}.bsl")
