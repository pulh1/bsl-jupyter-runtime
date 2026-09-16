from __future__ import annotations

import importlib.resources
from pathlib import Path
from urllib.parse import quote
from uuid import UUID

from onec_runtime.extension_bundle import EXTENSION_NAME, read_extension_manifest
from onec_runtime.rdbg.models import ModuleLocation

KERNEL_OBJECT_ID = "8fc91d24-20f5-4da4-8ff7-7a7c682f80f5"
OBJECT_MODULE_PROPERTY_ID = "a637f77f-3840-441d-a1c3-699c8c5cb7e0"
DOCUMENT_MANAGER_MODULE_PROPERTY_ID = "d1b64a2c-8078-4982-8190-8f81aefda192"
SERVICE_BREAKPOINT_MARKER = "@runtime-service-breakpoint"
with importlib.resources.as_file(
    importlib.resources.files("onec_runtime").joinpath(
        "resources", "extension", "extension-manifest.json"
    )
) as _manifest_path:
    _PRODUCT_BREAKPOINTS = read_extension_manifest(_manifest_path).breakpoints

EXTENSION_OBJECT_ID = str(_PRODUCT_BREAKPOINTS.managed.object_id)
MANAGED_APPLICATION_MODULE_PROPERTY_ID = "d22e852a-cf8a-4f77-8ccb-3548e7792bea"
EXTENSION_BREAKPOINT_MARKER = "@runtime-extension-service-breakpoint"
SESSION_MODULE_PROPERTY_ID = "9b7bbbae-9771-46f2-9e4d-2489e0ffc702"
SESSION_STARTUP_BREAKPOINT_MARKER = "@runtime-session-startup-breakpoint"
SERVER_COMMON_MODULE_OBJECT_ID = str(_PRODUCT_BREAKPOINTS.server_service.object_id)
COMMON_MODULE_PROPERTY_ID = "d5963243-262e-4398-b4d7-fb16d06484f6"
SERVER_EXTENSION_BREAKPOINT_MARKER = "@runtime-server-extension-service-breakpoint"
SERVER_EXTENSION_ENTRY_BREAKPOINT_MARKER = "@runtime-server-extension-entry-breakpoint"
SYNTHETIC_CAPTURE_A_MARKER = "@runtime-synthetic-capture-a"
SYNTHETIC_CAPTURE_B_MARKER = "@runtime-synthetic-capture-b"
SHIELDED_NESTED_CAPTURE_MARKER = "@runtime-shielded-nested-capture"
USER_BREAKPOINT_MARKER = "@runtime-user-breakpoint"
ZUP_GENERAL_PURPOSE_OBJECT_ID = "75e77f99-b9d1-4bc0-93aa-c77a93d760d3"
ZUP_SERVER_CAPTURE_LINE = 875
ZUP_SERVER_CAPTURE_B_LINE = 877


def service_breakpoint_line(module_path: Path) -> int:
    lines = module_path.read_text(encoding="utf-8").splitlines()
    matches = [
        index
        for index, line in enumerate(lines, start=1)
        if SERVICE_BREAKPOINT_MARKER in line
    ]
    if len(matches) != 1:
        raise ValueError(
            f"Expected exactly one {SERVICE_BREAKPOINT_MARKER} marker, found {len(matches)}"
        )
    marker_line = lines[matches[0] - 1]
    if marker_line.lstrip().startswith("//") or (
        ";" not in marker_line and " Тогда" not in marker_line
    ):
        raise ValueError(
            "The service breakpoint marker must be on an executable BSL line"
        )
    return matches[0]


def external_module_url(epf_path: Path) -> str:
    normalized = str(epf_path.resolve()).replace("\\", "/")
    return "file://" + quote(normalized, safe="/:")


def extension_breakpoint_line(module_path: Path) -> int:
    lines = module_path.read_text(encoding="utf-8-sig").splitlines()
    matches = [
        index
        for index, line in enumerate(lines, start=1)
        if EXTENSION_BREAKPOINT_MARKER in line
    ]
    if len(matches) != 1:
        raise ValueError(
            f"Expected exactly one {EXTENSION_BREAKPOINT_MARKER} marker, found {len(matches)}"
        )
    return matches[0]


def server_extension_breakpoint_line(module_path: Path) -> int:
    lines = module_path.read_text(encoding="utf-8-sig").splitlines()
    matches = [
        index
        for index, line in enumerate(lines, start=1)
        if SERVER_EXTENSION_BREAKPOINT_MARKER in line
    ]
    if len(matches) != 1:
        raise ValueError(
            f"Expected exactly one {SERVER_EXTENSION_BREAKPOINT_MARKER} marker, found {len(matches)}"
        )
    return matches[0]


def server_extension_entry_breakpoint_line(module_path: Path) -> int:
    lines = module_path.read_text(encoding="utf-8-sig").splitlines()
    matches = [
        index
        for index, line in enumerate(lines, start=1)
        if SERVER_EXTENSION_ENTRY_BREAKPOINT_MARKER in line
    ]
    if len(matches) != 1:
        raise ValueError(
            f"Expected exactly one {SERVER_EXTENSION_ENTRY_BREAKPOINT_MARKER} "
            f"marker, found {len(matches)}"
        )
    marker_line = lines[matches[0] - 1]
    if marker_line.lstrip().startswith("//") or ";" not in marker_line:
        raise ValueError("The server entry breakpoint marker must be executable")
    return matches[0]


def session_extension_breakpoint_line(module_path: Path) -> int:
    lines = module_path.read_text(encoding="utf-8-sig").splitlines()
    matches = [
        index
        for index, line in enumerate(lines, start=1)
        if SESSION_STARTUP_BREAKPOINT_MARKER in line
    ]
    if len(matches) != 1:
        raise ValueError(
            f"Expected exactly one {SESSION_STARTUP_BREAKPOINT_MARKER} marker, "
            f"found {len(matches)}"
        )
    marker_line = lines[matches[0] - 1]
    if marker_line.lstrip().startswith("//") or " Тогда" not in marker_line:
        raise ValueError("The session startup marker must be on an executable BSL line")
    return matches[0]


def _server_module_marker_location(
    module_path: Path,
    marker: str,
) -> ModuleLocation:
    lines = module_path.read_text(encoding="utf-8-sig").splitlines()
    matches = [index for index, line in enumerate(lines, start=1) if marker in line]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one {marker} marker, found {len(matches)}")
    marker_line = lines[matches[0] - 1]
    if marker_line.lstrip().startswith("//") or (
        ";" not in marker_line and " Тогда" not in marker_line
    ):
        raise ValueError(f"Runtime marker {marker} must be executable")
    return ModuleLocation(
        "ExtensionModule",
        "",
        UUID(SERVER_COMMON_MODULE_OBJECT_ID),
        UUID(COMMON_MODULE_PROPERTY_ID),
        matches[0],
        EXTENSION_NAME,
    )


def synthetic_capture_locations(
    module_path: Path,
) -> tuple[ModuleLocation, ModuleLocation]:
    return _server_module_marker_location(
        module_path,
        SYNTHETIC_CAPTURE_A_MARKER,
    ), _server_module_marker_location(module_path, SYNTHETIC_CAPTURE_B_MARKER)


def shielded_capture_location(module_path: Path) -> ModuleLocation:
    return _server_module_marker_location(module_path, SHIELDED_NESTED_CAPTURE_MARKER)


def user_breakpoint_location(module_path: Path) -> ModuleLocation:
    return _server_module_marker_location(module_path, USER_BREAKPOINT_MARKER)


def zup_server_capture_location(
    *,
    line: int = ZUP_SERVER_CAPTURE_LINE,
) -> ModuleLocation:
    return ModuleLocation(
        module_type="ConfigModule",
        url="",
        object_id=UUID(ZUP_GENERAL_PURPOSE_OBJECT_ID),
        property_id=UUID(COMMON_MODULE_PROPERTY_ID),
        line=line,
    )
