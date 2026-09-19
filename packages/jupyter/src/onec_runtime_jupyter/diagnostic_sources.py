"""Optional local file hints for postmortem 1C module locations."""

from __future__ import annotations

from pathlib import Path

from onec_runtime.bsl import source_sha256
from onec_runtime.bsl.diagnostics import ErrorTraceFrame, ErrorTraceFrameOrigin
from onec_runtime.configuration_source import (
    ConfigurationSourceLayout,
    SourceLayer,
    SourceTreeLayout,
)


_DIRECTORIES = {
    "ОбщийМодуль": "CommonModules",
    "CommonModule": "CommonModules",
    "Справочник": "Catalogs",
    "Catalog": "Catalogs",
    "Документ": "Documents",
    "Document": "Documents",
}
_ROLES = {
    "Модуль": "Module",
    "Module": "Module",
    "МодульМенеджера": "ManagerModule",
    "ManagerModule": "ManagerModule",
    "МодульОбъекта": "ObjectModule",
    "ObjectModule": "ObjectModule",
}
_FORM_LABELS = {"Форма", "Form"}


def _native_parts(
    module_name: str | None,
) -> tuple[str, str, str, str | None] | None:
    if module_name is None:
        return None
    parts = module_name.split(".")
    if len(parts) == 3 and parts[0] in _DIRECTORIES:
        role = _ROLES.get(parts[2])
        if role is None or (
            _DIRECTORIES[parts[0]] == "CommonModules" and role != "Module"
        ):
            return None
        return _DIRECTORIES[parts[0]], parts[1], role, None
    if (
        len(parts) == 5
        and parts[0] in _DIRECTORIES
        and _DIRECTORIES[parts[0]] in {"Catalogs", "Documents"}
        and parts[2] in _FORM_LABELS
        and parts[4] in _FORM_LABELS
    ):
        return _DIRECTORIES[parts[0]], parts[1], "Form", parts[3]
    return None


class DiagnosticSourceFiles:
    """Resolve only exact named files beneath one configured 1C source tree."""

    def __init__(self, source_root: Path | str) -> None:
        self.layout = ConfigurationSourceLayout(source_root)
        root = self.layout.normalized_root
        identity = (
            root / "Configuration.xml"
            if self.layout.layout is SourceTreeLayout.DESIGNER
            else root / "Configuration" / "Configuration.mdo"
        )
        self.binding = (
            self.layout.bind("NotebookDiagnostics") if identity.is_file() else None
        )

    def _paths(
        self,
        directory: str,
        name: str,
        role: str,
        form: str | None,
    ) -> tuple[Path, Path] | None:
        if not name.isidentifier() or (form is not None and not form.isidentifier()):
            return None
        root = self.layout.normalized_root
        base = root / directory / name
        if form is None:
            metadata = (
                root / directory / f"{name}.xml"
                if self.layout.layout is SourceTreeLayout.DESIGNER
                else base / f"{name}.mdo"
            )
            module = (
                base / "Ext" / f"{role}.bsl"
                if self.layout.layout is SourceTreeLayout.DESIGNER
                else base / f"{role}.bsl"
            )
        elif self.layout.layout is SourceTreeLayout.DESIGNER:
            metadata = base / "Forms" / f"{form}.xml"
            module = base / "Forms" / form / "Ext" / "Form" / "Module.bsl"
        else:
            return None
        return metadata, module

    def hint(self, frame: ErrorTraceFrame) -> str | None:
        if frame.origin is ErrorTraceFrameOrigin.WORKER_ARTIFACT:
            if frame.logical_name is None or frame.source_unit is None:
                return None
            parts = ("CommonModules", frame.logical_name, "Module", None)
        elif frame.origin is ErrorTraceFrameOrigin.NATIVE_MODULE:
            if self.binding is None:
                return None
            extension = frame.platform_location.extension_name
            if extension is None and self.binding.layer is not SourceLayer.BASE:
                return None
            if extension is not None and (
                self.binding.layer is not SourceLayer.EXTENSION
                or self.binding.extension_name != extension
            ):
                return None
            parts = _native_parts(frame.platform_location.module_name)
            if parts is None:
                return None
        else:
            return None
        directory, name, role, form = parts
        candidates = self._paths(directory, name, role, form)
        if candidates is None:
            return None
        metadata, module = candidates
        metadata = self.layout.safe_path(metadata)
        module = self.layout.safe_path(module)
        if not module.is_file():
            return None
        if frame.origin is ErrorTraceFrameOrigin.NATIVE_MODULE:
            if not metadata.is_file():
                return None
        else:
            assert frame.source_unit is not None
            source = module.read_bytes().decode("utf-8-sig")
            if source_sha256(source) != frame.source_unit.source_sha256:
                return None
        return module.relative_to(self.layout.normalized_root).as_posix()

    def line_excerpt(
        self,
        frame: ErrorTraceFrame,
        platform_detail: str | None,
    ) -> str | None:
        """Return a local line only when it confirms the 1C-provided fragment."""

        relative = self.hint(frame)
        if relative is None or platform_detail is None:
            return None
        path = self.layout.safe_path(self.layout.normalized_root / relative)
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        line = frame.platform_location.line
        if not 1 <= line <= len(lines):
            return None
        local = lines[line - 1].strip()
        return local if local and local == platform_detail.strip() else None
