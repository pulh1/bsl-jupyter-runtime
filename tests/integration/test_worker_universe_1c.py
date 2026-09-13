from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from time import perf_counter, time
from unittest.mock import patch
from uuid import uuid4

import psutil
import pytest

import onec_runtime.bsl.module_universe as module_universe
import onec_runtime.worker_universe as worker_universe
from integration.jupyter_bsl_fixture.extension import (
    FIXTURE_EXTENSION_NAME,
    build_fixture_extension,
    install_fixture_extension,
    install_minimal_host_configuration,
    install_product_extension,
    validate_fixture_extension_source,
)
from integration.support.configurator_agent import configure_extensions_unsafe
from onec_runtime.bsl import (
    CommonModuleCatalogSnapshot,
    CommonModuleDescriptor,
    CommonModuleScope,
    SourceUnitKind,
    SourceUnitRef,
    WorkerModuleUnit,
    mapped_visible_source,
    source_sha256,
)
from onec_runtime.bsl.diagnostics import (
    DiagnosticStage,
    MappingConfidence,
    parse_platform_diagnostic,
)
from onec_runtime.bsl.module_universe import DEPENDENCY_ALIAS_INITIALIZER_REGION
from onec_runtime.bsl.source_maps import (
    LineIndex,
    SourceArtifactKind,
    SourceSpan,
    SourceTransformBuilder,
)
from onec_runtime.config import RuntimeConfig
from onec_runtime.errors import (
    BslExecutionError,
    CommandTimeout,
    ProtocolError,
    WorkerPromotionOutcomeUnknown,
)
from onec_runtime.extension_bundle import EXTENSION_NAME, packaged_extension_bundle
from onec_runtime.kernel import SYNTHETIC_CAPTURE_A_MARKER
from onec_runtime.session import RuntimeSession, RuntimeSessionConfig
from onec_runtime.table_value import evaluation_to_python
from onec_runtime.runtime_api import RuntimeReply, RuntimeReplyKind
from onec_runtime.toolchain import create_empty_infobase, run_tool_command
from onec_runtime.worker_breakpoints import WorkerMappedFrame
from onec_runtime.worker_epf import prepare_worker_module_source
from onec_runtime.worker_universe import (
    WorkerGenerationHandle,
    WorkerUniverseManifest,
    registration_name as worker_registration_name,
)


MODULE_A = "JupyterBslFixtureCallerServer"
MODULE_B = "JupyterBslFixtureCalleeServer"
_LIVE_FLAG = "ONEC_RUN_WORKER_UNIVERSE_INTEGRATION"
_EXPECTED_PLATFORM = Path(r"C:\Program Files\1cv8\8.3.27.2170\bin")
_REPOSITORY = Path(__file__).resolve().parents[2]
_FIXTURE_SOURCE = _REPOSITORY / "tests" / "fixtures" / "onec" / "JupyterBslTestFixture"
_HOST_SOURCE = _REPOSITORY / "tests" / "fixtures" / "onec" / "MinimalHostConfiguration"
_OWNED_PROCESS_NAMES = {"1cv8.exe", "1cv8c.exe", "dbgs.exe"}
_EXPECTED_PRODUCT_VERSION = "8.3.27.2170"
_SETTINGS_OBJECT_KEY = "onec-interactive-runtime-task9"

_MODULE_A_SOURCE = f"""#\u041e\u0431\u043b\u0430\u0441\u0442\u044c \u041f\u0443\u0431\u043b\u0438\u0447\u043d\u044b\u0439\u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442
\u041f\u0435\u0440\u0435\u043c \u041f\u043e\u0441\u043b\u0435\u0434\u043d\u0435\u0435\u0417\u043d\u0430\u0447\u0435\u043d\u0438\u0435;

\u041f\u0440\u043e\u0446\u0435\u0434\u0443\u0440\u0430 \u0417\u0430\u043f\u043e\u043c\u043d\u0438\u0442\u044c(\u0417\u043d\u0430\u0447\u0435\u043d\u0438\u0435) \u042d\u043a\u0441\u043f\u043e\u0440\u0442
    \u041f\u0435\u0440\u0435\u043c \u041f\u0440\u0435\u0434\u044b\u0434\u0443\u0449\u0435\u0435\u0417\u043d\u0430\u0447\u0435\u043d\u0438\u0435;
    \u041f\u0440\u0435\u0434\u044b\u0434\u0443\u0449\u0435\u0435\u0417\u043d\u0430\u0447\u0435\u043d\u0438\u0435 = \u041f\u043e\u0441\u043b\u0435\u0434\u043d\u0435\u0435\u0417\u043d\u0430\u0447\u0435\u043d\u0438\u0435;
    \u041f\u043e\u0441\u043b\u0435\u0434\u043d\u0435\u0435\u0417\u043d\u0430\u0447\u0435\u043d\u0438\u0435 = \u0417\u043d\u0430\u0447\u0435\u043d\u0438\u0435;
\u041a\u043e\u043d\u0435\u0446\u041f\u0440\u043e\u0446\u0435\u0434\u0443\u0440\u044b

\u0424\u0443\u043d\u043a\u0446\u0438\u044f \u0421\u0440\u0430\u0432\u043d\u0438\u0442\u044c\u041f\u043e\u0432\u0442\u043e\u0440\u043d\u044b\u0435\u0421\u0441\u044b\u043b\u043a\u0438(\u041b\u0435\u0432\u0430\u044f, \u041f\u0440\u0430\u0432\u0430\u044f)
    \u0412\u043e\u0437\u0432\u0440\u0430\u0442 \u041b\u0435\u0432\u0430\u044f = \u041f\u0440\u0430\u0432\u0430\u044f;
\u041a\u043e\u043d\u0435\u0446\u0424\u0443\u043d\u043a\u0446\u0438\u0438

#\u0415\u0441\u043b\u0438 \u0421\u0435\u0440\u0432\u0435\u0440 \u0422\u043e\u0433\u0434\u0430
\u0424\u0443\u043d\u043a\u0446\u0438\u044f \u0414\u0430\u043d\u043d\u044b\u0435\u0417\u0430\u0432\u0438\u0441\u0438\u043c\u043e\u0441\u0442\u0438() \u042d\u043a\u0441\u043f\u043e\u0440\u0442
    \u041f\u0435\u0440\u0435\u043c \u041f\u0435\u0440\u0432\u044b\u0439\u0422\u0438\u043f, \u0412\u0442\u043e\u0440\u043e\u0439\u0422\u0438\u043f;
    \u041f\u0435\u0440\u0432\u044b\u0439\u0422\u0438\u043f = \u0422\u0438\u043f\u0417\u043d\u0447({MODULE_B});
    \u0412\u0442\u043e\u0440\u043e\u0439\u0422\u0438\u043f = \u0422\u0438\u043f\u0417\u043d\u0447({MODULE_B});
    \u0420\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442\u0412\u044b\u0437\u043e\u0432\u0430 = {MODULE_B}.\u0412\u044b\u043f\u043e\u043b\u043d\u0438\u0442\u044c\u0428\u0430\u0433(10);
    \u0412\u043e\u0437\u0432\u0440\u0430\u0442 \u0421\u0442\u0440\u043e\u043a\u0430({MODULE_B})
        + "|" + \u0421\u0442\u0440\u043e\u043a\u0430(\u041f\u0435\u0440\u0432\u044b\u0439\u0422\u0438\u043f)
        + "|" + \u0421\u0442\u0440\u043e\u043a\u0430(\u041f\u0435\u0440\u0432\u044b\u0439\u0422\u0438\u043f = \u0412\u0442\u043e\u0440\u043e\u0439\u0422\u0438\u043f)
        + "|" + \u0421\u0442\u0440\u043e\u043a\u0430(\u0421\u0440\u0430\u0432\u043d\u0438\u0442\u044c\u041f\u043e\u0432\u0442\u043e\u0440\u043d\u044b\u0435\u0421\u0441\u044b\u043b\u043a\u0438({MODULE_B}, {MODULE_B}))
        + "|" + \u0421\u0442\u0440\u043e\u043a\u0430(\u0420\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442\u0412\u044b\u0437\u043e\u0432\u0430.\u0421\u0447\u0435\u0442\u0447\u0438\u043a);
\u041a\u043e\u043d\u0435\u0446\u0424\u0443\u043d\u043a\u0446\u0438\u0438
#\u041a\u043e\u043d\u0435\u0446\u0415\u0441\u043b\u0438

\u0424\u0443\u043d\u043a\u0446\u0438\u044f \u041c\u0435\u0442\u043a\u0430A() \u042d\u043a\u0441\u043f\u043e\u0440\u0442
    \u0412\u043e\u0437\u0432\u0440\u0430\u0442 "A17";
\u041a\u043e\u043d\u0435\u0446\u0424\u0443\u043d\u043a\u0446\u0438\u0438

\u0424\u0443\u043d\u043a\u0446\u0438\u044f \u041f\u0440\u043e\u0432\u0435\u0440\u0438\u0442\u044c\u0426\u0438\u043a\u043b() \u042d\u043a\u0441\u043f\u043e\u0440\u0442
    \u0412\u043e\u0437\u0432\u0440\u0430\u0442 {MODULE_B}.\u0412\u044b\u0437\u0432\u0430\u0442\u044cA();
\u041a\u043e\u043d\u0435\u0446\u0424\u0443\u043d\u043a\u0446\u0438\u0438

\u0424\u0443\u043d\u043a\u0446\u0438\u044f \u041f\u0440\u043e\u0432\u0435\u0440\u0438\u0442\u044cSelf() \u042d\u043a\u0441\u043f\u043e\u0440\u0442
    \u0412\u043e\u0437\u0432\u0440\u0430\u0442 {MODULE_A}.\u041c\u0435\u0442\u043a\u0430A();
\u041a\u043e\u043d\u0435\u0446\u0424\u0443\u043d\u043a\u0446\u0438\u0438

\u0424\u0443\u043d\u043a\u0446\u0438\u044f \u041f\u043e\u043b\u0443\u0447\u0438\u0442\u044cB() \u042d\u043a\u0441\u043f\u043e\u0440\u0442
    \u0412\u043e\u0437\u0432\u0440\u0430\u0442 {MODULE_B};
\u041a\u043e\u043d\u0435\u0446\u0424\u0443\u043d\u043a\u0446\u0438\u0438

\u0424\u0443\u043d\u043a\u0446\u0438\u044f \u0410\u0432\u0430\u0440\u0438\u044fA() \u042d\u043a\u0441\u043f\u043e\u0440\u0442
    \u0412\u043e\u0437\u0432\u0440\u0430\u0442 {MODULE_B}.\u0410\u0432\u0430\u0440\u0438\u044f();
\u041a\u043e\u043d\u0435\u0446\u0424\u0443\u043d\u043a\u0446\u0438\u0438
#\u041a\u043e\u043d\u0435\u0446\u041e\u0431\u043b\u0430\u0441\u0442\u0438
"""


def _module_b_source(revision: int) -> str:
    return f"""#\u041e\u0431\u043b\u0430\u0441\u0442\u044c \u041f\u0443\u0431\u043b\u0438\u0447\u043d\u044b\u0439\u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442
\u0424\u0443\u043d\u043a\u0446\u0438\u044f \u0412\u044b\u043f\u043e\u043b\u043d\u0438\u0442\u044c\u0428\u0430\u0433(\u041d\u0430\u0447\u0430\u043b\u044c\u043d\u043e\u0435\u0417\u043d\u0430\u0447\u0435\u043d\u0438\u0435) \u042d\u043a\u0441\u043f\u043e\u0440\u0442
    \u041f\u0435\u0440\u0435\u043c \u0421\u0447\u0435\u0442\u0447\u0438\u043a;
    \u0421\u0447\u0435\u0442\u0447\u0438\u043a = \u041d\u0430\u0447\u0430\u043b\u044c\u043d\u043e\u0435\u0417\u043d\u0430\u0447\u0435\u043d\u0438\u0435 + {revision};
    \u0412\u043e\u0437\u0432\u0440\u0430\u0442 \u041d\u043e\u0432\u044b\u0439 \u0421\u0442\u0440\u0443\u043a\u0442\u0443\u0440\u0430("\u0421\u0447\u0435\u0442\u0447\u0438\u043a", \u0421\u0447\u0435\u0442\u0447\u0438\u043a);
\u041a\u043e\u043d\u0435\u0446\u0424\u0443\u043d\u043a\u0446\u0438\u0438

\u0424\u0443\u043d\u043a\u0446\u0438\u044f \u0412\u0435\u0440\u0441\u0438\u044f() \u042d\u043a\u0441\u043f\u043e\u0440\u0442
    \u0412\u043e\u0437\u0432\u0440\u0430\u0442 "B{revision}";
\u041a\u043e\u043d\u0435\u0446\u0424\u0443\u043d\u043a\u0446\u0438\u0438

\u0424\u0443\u043d\u043a\u0446\u0438\u044f \u0412\u044b\u0437\u0432\u0430\u0442\u044cA() \u042d\u043a\u0441\u043f\u043e\u0440\u0442
    \u0412\u043e\u0437\u0432\u0440\u0430\u0442 {MODULE_A}.\u041c\u0435\u0442\u043a\u0430A() + "->B{revision}";
\u041a\u043e\u043d\u0435\u0446\u0424\u0443\u043d\u043a\u0446\u0438\u0438

\u0424\u0443\u043d\u043a\u0446\u0438\u044f \u041f\u0440\u043e\u0432\u0435\u0440\u0438\u0442\u044cSelf() \u042d\u043a\u0441\u043f\u043e\u0440\u0442
    \u0412\u043e\u0437\u0432\u0440\u0430\u0442 {MODULE_B}.\u0412\u0435\u0440\u0441\u0438\u044f();
\u041a\u043e\u043d\u0435\u0446\u0424\u0443\u043d\u043a\u0446\u0438\u0438

\u0424\u0443\u043d\u043a\u0446\u0438\u044f \u0410\u0432\u0430\u0440\u0438\u044f() \u042d\u043a\u0441\u043f\u043e\u0440\u0442
    \u0412\u044b\u0437\u0432\u0430\u0442\u044c\u0418\u0441\u043a\u043b\u044e\u0447\u0435\u043d\u0438\u0435 "task9-b-runtime-B{revision}";
\u041a\u043e\u043d\u0435\u0446\u0424\u0443\u043d\u043a\u0446\u0438\u0438
#\u041a\u043e\u043d\u0435\u0446\u041e\u0431\u043b\u0430\u0441\u0442\u0438
"""


@dataclass(frozen=True, slots=True)
class WorkerUniverseSourceContract:
    catalog: CommonModuleCatalogSnapshot
    module_a: WorkerModuleUnit
    module_b_g17: WorkerModuleUnit
    module_b_g18: WorkerModuleUnit
    module_b_g19: WorkerModuleUnit

    @property
    def original_b_units(self) -> tuple[WorkerModuleUnit, ...]:
        return (self.module_a,)

    @property
    def g17_units(self) -> tuple[WorkerModuleUnit, ...]:
        return (self.module_a, self.module_b_g17)

    @property
    def g18_units(self) -> tuple[WorkerModuleUnit, ...]:
        return (self.module_a, self.module_b_g18)

    @property
    def g19_units(self) -> tuple[WorkerModuleUnit, ...]:
        return (self.module_a, self.module_b_g19)


def _unit(
    logical_name: str,
    revision: int,
    source: str,
    catalog: CommonModuleCatalogSnapshot,
) -> WorkerModuleUnit:
    reference = SourceUnitRef(
        SourceUnitKind.MODULE,
        logical_name,
        revision,
        source_sha256(source),
    )
    return WorkerModuleUnit(
        logical_name,
        "module",
        revision,
        mapped_visible_source(source, reference),
    )


def worker_universe_source_contract() -> WorkerUniverseSourceContract:
    catalog = CommonModuleCatalogSnapshot.create(
        profile="server-jupyter-bsl-fixture-8.3.27.2170",
        preprocessor_profile="server",
        revision=1,
        modules=(
            CommonModuleDescriptor(
                MODULE_A,
                CommonModuleScope.SERVER,
            ),
            CommonModuleDescriptor(
                MODULE_B,
                CommonModuleScope.SERVER,
            ),
        ),
    )
    return WorkerUniverseSourceContract(
        catalog,
        _unit(MODULE_A, 17, _MODULE_A_SOURCE, catalog),
        _unit(MODULE_B, 17, _module_b_source(17), catalog),
        _unit(MODULE_B, 18, _module_b_source(18), catalog),
        _unit(MODULE_B, 19, _module_b_source(19), catalog),
    )


@dataclass(frozen=True, slots=True)
class PlatformExecutableIdentity:
    filename: str
    product_version: str
    sha256: str


class _VsFixedFileInfo(ctypes.Structure):
    _fields_ = (
        ("signature", ctypes.c_uint),
        ("structure_version", ctypes.c_uint),
        ("file_version_ms", ctypes.c_uint),
        ("file_version_ls", ctypes.c_uint),
        ("product_version_ms", ctypes.c_uint),
        ("product_version_ls", ctypes.c_uint),
        ("file_flags_mask", ctypes.c_uint),
        ("file_flags", ctypes.c_uint),
        ("file_os", ctypes.c_uint),
        ("file_type", ctypes.c_uint),
        ("file_subtype", ctypes.c_uint),
        ("file_date_ms", ctypes.c_uint),
        ("file_date_ls", ctypes.c_uint),
    )


def _windows_product_version(path: Path) -> str:
    version = getattr(ctypes, "windll", None)
    if version is None:
        raise ValueError("Windows ProductVersion API is unavailable")
    size = version.version.GetFileVersionInfoSizeW(str(path), None)
    if size <= 0:
        raise ValueError(f"missing ProductVersion resource: {path.name}")
    buffer = ctypes.create_string_buffer(size)
    if not version.version.GetFileVersionInfoW(str(path), 0, size, buffer):
        raise ValueError(f"unreadable ProductVersion resource: {path.name}")
    pointer = ctypes.c_void_p()
    length = ctypes.c_uint()
    if not version.version.VerQueryValueW(
        buffer,
        "\\",
        ctypes.byref(pointer),
        ctypes.byref(length),
    ):
        raise ValueError(f"missing fixed ProductVersion: {path.name}")
    fixed = ctypes.cast(pointer, ctypes.POINTER(_VsFixedFileInfo)).contents
    if (
        length.value < ctypes.sizeof(_VsFixedFileInfo)
        or fixed.signature != 0xFEEF04BD
    ):
        raise ValueError(f"invalid fixed ProductVersion: {path.name}")
    values = (
        fixed.product_version_ms >> 16,
        fixed.product_version_ms & 0xFFFF,
        fixed.product_version_ls >> 16,
        fixed.product_version_ls & 0xFFFF,
    )
    return ".".join(str(value) for value in values)


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _platform_executable_identities(
    platform: Path,
    *,
    version_reader=_windows_product_version,
    expected_version: str = _EXPECTED_PRODUCT_VERSION,
) -> tuple[PlatformExecutableIdentity, ...]:
    paths = (
        platform / "1cv8.exe",
        platform / "1cv8c.exe",
        platform / "dbgs.exe",
    )
    identities: list[PlatformExecutableIdentity] = []
    for path in paths:
        resolved = path.resolve()
        if not resolved.is_file() or resolved.parent != platform:
            raise ValueError(f"platform executable parent mismatch: {path.name}")
        product_version = version_reader(resolved)
        if product_version != expected_version:
            raise ValueError(
                f"platform executable ProductVersion mismatch: {path.name}="
                f"{product_version}"
            )
        identities.append(
            PlatformExecutableIdentity(
                resolved.name.casefold(),
                product_version,
                _file_sha256(resolved),
            )
        )
    return tuple(identities)


def _platform_prerequisites(
) -> tuple[Path, tuple[PlatformExecutableIdentity, ...]]:
    if os.environ.get(_LIVE_FLAG) != "1":
        pytest.skip(
            f"set {_LIVE_FLAG}=1 for the fresh temporary-infobase Worker universe gate"
        )
    configured = Path(
        os.environ.get("ONEC_PLATFORM_BIN", str(_EXPECTED_PLATFORM))
    ).resolve()
    if configured.parent.name not in {"8.3.27.2170", "8.5.1.1529"}:
        pytest.fail("Worker universe live gate requires 1C 8.3.27.2170 or 8.5.1.1529")
    missing = tuple(
        executable
        for executable in ("1cv8.exe", "1cv8c.exe", "dbgs.exe")
        if not (configured / executable).is_file()
    )
    if missing:
        pytest.skip(
            "Worker universe live gate prerequisites are missing: "
            + ", ".join(missing)
        )
    try:
        identities = _platform_executable_identities(
            configured, expected_version=configured.parent.name,
        )
    except ValueError as error:
        pytest.fail(str(error))
    return configured, identities


def _matching_owned_processes(infobase: Path) -> tuple[dict[str, object], ...]:
    marker = str(infobase.resolve()).casefold()
    matches: list[dict[str, object]] = []
    for process in psutil.process_iter(("pid", "name", "cmdline", "create_time")):
        try:
            name = (process.info["name"] or "").casefold()
            command = " ".join(process.info["cmdline"] or ()).casefold()
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            continue
        if name in _OWNED_PROCESS_NAMES and marker in command:
            matches.append(
                {
                    "pid": process.pid,
                    "name": name,
                    "create_time": process.info["create_time"],
                }
            )
    return tuple(matches)


def _normalized_source(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").strip()


def _verify_exact_fixture_install(config: RuntimeConfig, root: Path) -> str:
    dump = root / "installed-fixture-source"
    log = root / "installed-fixture-source.log"
    run_tool_command(
        [
            str(config.designer_exe),
            "DESIGNER",
            "/F",
            str(config.infobase_dir),
            "/N",
            config.username,
            "/P",
            "",
            "/DumpConfigToFiles",
            str(dump),
            "-Extension",
            FIXTURE_EXTENSION_NAME,
            "-Format",
            "Hierarchical",
            "/DisableStartupDialogs",
            "/DisableStartupMessages",
            "/Out",
            str(log),
        ],
        log,
    )
    installed = validate_fixture_extension_source(dump)
    expected = validate_fixture_extension_source(_FIXTURE_SOURCE)
    assert installed.extension_name == expected.extension_name
    assert installed.module_names == expected.module_names
    assert installed.fragment_counts == expected.fragment_counts
    for logical_name in (MODULE_A, MODULE_B):
        relative = Path("CommonModules") / logical_name / "Ext" / "Module.bsl"
        assert _normalized_source(dump / relative) == _normalized_source(
            _FIXTURE_SOURCE / relative
        )
    return installed.source_sha256


@dataclass(slots=True)
class LiveWorkerUniverseHarness:
    config: RuntimeConfig
    session: RuntimeSession
    platform: Path
    platform_executables: tuple[PlatformExecutableIdentity, ...]
    fixture_source_sha256: str
    started_at: float
    phase_seconds: dict[str, float]

    def measure(self, phase: str, operation):
        started = perf_counter()
        try:
            return operation()
        finally:
            self.phase_seconds[phase] = perf_counter() - started

    @property
    def host_registry(self):
        return self.session.runtime_api._worker_universe

    @property
    def target_registry(self):
        return self.session.runtime_api._worker_universe_target


@contextmanager
def _fresh_live_harness(
    tmp_path: Path,
) -> Iterator[LiveWorkerUniverseHarness]:
    platform, executable_identities = _platform_prerequisites()
    owned_root = tmp_path / "task9-live-owned"
    assert owned_root.resolve().parent == tmp_path.resolve()
    assert not owned_root.exists()
    target_root = owned_root / "target"
    config = RuntimeConfig(
        workspace=target_root,
        platform_bin=platform,
    )
    owned_root.mkdir(parents=True)
    phase_seconds: dict[str, float] = {}

    def measured(phase: str, operation):
        phase_started = perf_counter()
        try:
            return operation()
        finally:
            phase_seconds[phase] = perf_counter() - phase_started

    session: RuntimeSession | None = None
    harness: LiveWorkerUniverseHarness | None = None
    owned_processes: tuple[dict[str, object], ...] = ()
    fixture_sha256 = ""
    started_at = 0.0
    process_evidence: list[dict[str, object]] = []
    target_evidence: dict[str, object] = {}
    cleanup = {
        "host_registry_empty": False,
        "target_registry_empty": False,
        "owned_processes_gone": False,
        "owned_root_removed": False,
    }
    try:
        assert _matching_owned_processes(config.infobase_dir) == ()
        artifact = measured(
            "setup.fixture_build",
            lambda: build_fixture_extension(
                platform,
                _FIXTURE_SOURCE,
                owned_root / "fixture-extension-build",
            ),
        )
        measured("setup.infobase_create", lambda: create_empty_infobase(config))
        measured(
            "setup.host_configuration_install",
            lambda: install_minimal_host_configuration(
                config,
                _HOST_SOURCE,
                owned_root / "host-configuration-install",
            ),
        )
        measured(
            "setup.product_extension_install",
            lambda: install_product_extension(
                config,
                owned_root / "product-extension-install",
            ),
        )
        measured(
            "setup.fixture_extension_install",
            lambda: install_fixture_extension(
                config,
                artifact,
                owned_root / "fixture-extension-install",
            ),
        )
        measured(
            "setup.extensions_configure",
            lambda: configure_extensions_unsafe(
                config,
                (EXTENSION_NAME, FIXTURE_EXTENSION_NAME),
                owned_root / "configurator-agent",
            ),
        )
        fixture_sha256 = measured(
            "setup.fixture_identity_dump",
            lambda: _verify_exact_fixture_install(config, owned_root),
        )
        started_at = time()
        session = measured(
            "setup.runtime_start",
            lambda: RuntimeSession.start(
                RuntimeSessionConfig(
                    config,
                    owned_root / "runtime-evidence",
                    source_root=_FIXTURE_SOURCE,
                )
            ),
        )
        owned_processes = session.owned_process_snapshot()
        marker = str(config.infobase_dir.resolve()).casefold()
        assert {item["role"] for item in owned_processes} == {"dbgs", "onec"}
        executable_by_name = {
            item.filename: item for item in executable_identities
        }
        rdbg_root = session._transport._base_url.removesuffix("/e1crdbg/rdbg")
        for item in owned_processes:
            process = psutil.Process(int(item["pid"]))
            executable = Path(process.exe()).resolve()
            command = " ".join(process.cmdline()).casefold()
            assert process.create_time() >= started_at
            assert item["create_time"] >= started_at
            assert process.create_time() == item["create_time"]
            assert Path(str(item["executable"])).resolve() == executable
            assert executable.parent == platform
            expected_executable = (
                "dbgs.exe" if item["role"] == "dbgs" else "1cv8c.exe"
            )
            assert executable.name.casefold() == expected_executable
            assert _file_sha256(executable) == (
                executable_by_name[expected_executable].sha256
            )
            if item["role"] == "dbgs":
                assert f"--ownerpid={os.getpid()}" in command
                assert f"--addr={config.debug_host}".casefold() in command
                assert "--notify=" in command
                assert str(config.logs_dir.resolve()).casefold() in command
                command_identity = {
                    "debug_address": config.debug_host,
                    "logs_dir": str(config.logs_dir.resolve()),
                    "owner_pid": os.getpid(),
                }
            else:
                assert marker in command
                assert "/debuggerurl" in command
                assert rdbg_root.casefold() in command
                assert "/debug -http -attach" in command
                command_identity = {
                    "infobase": str(config.infobase_dir.resolve()),
                    "rdbg_root": rdbg_root,
                }
            process_evidence.append(
                {
                    "command_identity": command_identity,
                    "create_time": process.create_time(),
                    "executable": str(executable),
                    "pid": process.pid,
                    "role": item["role"],
                    "sha256": executable_by_name[expected_executable].sha256,
                }
            )
        target = session._rdbg.target
        assert target is not None
        assert target.target_type == "ServerEmulation"
        assert target.target_id.infobase_alias == "DefAlias"
        assert target.target_id.seance_id is not None
        assert target.target_id.infobase_instance_id is not None
        target_evidence = {
            "infobase_alias": target.target_id.infobase_alias,
            "infobase_instance_id": str(target.target_id.infobase_instance_id),
            "seance_id": str(target.target_id.seance_id),
            "target_type": target.target_type,
        }
        bootstrap = json.loads(
            next((owned_root / "runtime-evidence").iterdir())
            .joinpath("bootstrap.json")
            .read_text(encoding="utf-8")
        )
        packaged = packaged_extension_bundle(config.runtime_dir).manifest
        assert bootstrap["status"] == "PASS"
        assert bootstrap["same_session"] is True
        assert bootstrap["bundle"]["identity_sha256"] == (
            packaged.fingerprints.identity_sha256
        )
        harness = LiveWorkerUniverseHarness(
            config,
            session,
            platform,
            executable_identities,
            fixture_sha256,
            started_at,
            phase_seconds,
        )
        yield harness
    finally:
        active_error = sys.exception()
        cleanup_errors: list[BaseException] = []
        if session is not None:
            try:
                measured("cleanup.session_close", session.close)
            except BaseException as error:
                cleanup_errors.append(error)
            try:
                host = session.runtime_api._worker_universe
                target = session.runtime_api._worker_universe_target
                assert host._state.value == "closed"
                assert host._registration_refcounts == {}
                assert host._registration_artifacts == {}
                assert host._quarantine_holds == set()
                assert target._registrations == {}
                assert target._candidate_registrations == {}
                cleanup["host_registry_empty"] = True
                cleanup["target_registry_empty"] = True
            except BaseException as error:
                cleanup_errors.append(error)
        try:
            assert _matching_owned_processes(config.infobase_dir) == ()
            for identity in owned_processes:
                try:
                    process = psutil.Process(int(identity["pid"]))
                    assert process.create_time() != identity["create_time"]
                except psutil.NoSuchProcess:
                    pass
            cleanup["owned_processes_gone"] = True
        except BaseException as error:
            cleanup_errors.append(error)
        cleanup_started = perf_counter()
        try:
            shutil.rmtree(owned_root)
            assert not owned_root.exists()
            cleanup["owned_root_removed"] = True
        except BaseException as error:
            cleanup_errors.append(error)
        finally:
            phase_seconds["cleanup.owned_root_remove"] = (
                perf_counter() - cleanup_started
            )
        evidence = {
            "cleanup": cleanup,
            "fixture": {
                "extension_name": FIXTURE_EXTENSION_NAME,
                "source_sha256": fixture_sha256,
            },
            "outcome": (
                "PASS"
                if active_error is None and not cleanup_errors
                else "FAIL"
            ),
            "phase_seconds": dict(sorted(phase_seconds.items())),
            "processes": sorted(process_evidence, key=lambda item: str(item["role"])),
            "platform": [
                {
                    "filename": item.filename,
                    "product_version": item.product_version,
                    "sha256": item.sha256,
                }
                for item in executable_identities
            ],
            "schema": "onec-task9-live-evidence-v1",
            "started_at": started_at,
            "target": target_evidence,
        }
        print(
            "TASK9_LIVE_EVIDENCE="
            + json.dumps(
                evidence,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            flush=True,
        )
        if cleanup_errors:
            failures: list[BaseException] = []
            if active_error is not None:
                failures.append(active_error)
            failures.extend(cleanup_errors)
            raise ExceptionGroup("Task 9 live cleanup failed", failures)


class _CountingRealArtifactBuilder:
    """Observe cache misses while delegating every build to the production packer."""

    def __init__(self, wrapped: object) -> None:
        self.wrapped = wrapped
        self.calls_by_module: dict[str, int] = {}

    def build(self, lowered: object, **kwargs: object):
        logical_name = lowered.analysis.unit.logical_name  # type: ignore[attr-defined]
        self.calls_by_module[logical_name] = (
            self.calls_by_module.get(logical_name, 0) + 1
        )
        return self.wrapped.build(lowered, **kwargs)  # type: ignore[attr-defined]


class _InvalidGeneratedAliasBuilder:
    """Corrupt one generated alias, then invoke the real production EPF packer."""

    def __init__(self, wrapped: object, *, revision: int) -> None:
        self.wrapped = wrapped
        self.revision = revision
        self.mutated = False
        self.mutated_source = None
        self.expected_lowered_offset: int | None = None
        self.artifact = None

    def build(self, lowered: object, **kwargs: object):
        unit = lowered.analysis.unit  # type: ignore[attr-defined]
        if unit.logical_name == MODULE_B and unit.revision == self.revision:
            mapped = lowered.mapped_source  # type: ignore[attr-defined]
            segment = next(
                item
                for item in mapped.source_map.segments
                if item.synthetic_region == DEPENDENCY_ALIAS_INITIALIZER_REGION
                and item.generated.start < item.generated.end
            )
            target = segment.generated
            original = mapped.text[target.start : target.end]
            left, separator, _right = original.partition("=")
            assert separator == "="
            line_ending = "\r\n" if original.endswith("\r\n") else "\n"
            builder = SourceTransformBuilder(mapped)
            if target.start:
                builder.copy(SourceSpan(0, target.start))
            builder.derived(
                f"{left}= Task9GeneratedAliasIsUndefined;{line_ending}",
                target,
                DEPENDENCY_ALIAS_INITIALIZER_REGION,
            )
            if target.end < len(mapped.text):
                builder.copy(SourceSpan(target.end, len(mapped.text)))
            mutated_source = prepare_worker_module_source(
                builder.build(mapped.artifact.kind)
            )
            # Inject a lowering defect before semantic admission is minted. The real
            # admission, packer, target compiler and diagnostic mapper still run.
            with patch.object(
                module_universe, "_compose_worker_module_source",
                return_value=mutated_source,
            ):
                lowered = module_universe.lower_worker_module(lowered.analysis)
            self.mutated = True
            self.mutated_source = mutated_source
            self.expected_lowered_offset = mutated_source.text.index(
                "Task9GeneratedAliasIsUndefined"
            )
        artifact = self.wrapped.build(lowered, **kwargs)  # type: ignore[attr-defined]
        if unit.logical_name == MODULE_B and unit.revision == self.revision:
            self.artifact = artifact
        return artifact


class _RealPhaseFaultExecutor:
    """Insert one target-side throw after a real instruction phase boundary."""

    def __init__(self, wrapped: object, phase: str) -> None:
        self.wrapped = wrapped
        self.phase = phase
        self.injected_source: str | None = None
        self.real_calls = 0

    def __call__(self, source: str) -> object:
        is_stage = (
            "onec-worker-artifact-stage=" in source
            or "onec-worker-stage-batch-receipt" in source
        )
        is_promotion = "onec-worker-root-prepare-stage=" in source
        applies = (
            self.phase in {"upload", "connect"} and is_stage
            or self.phase in {"create", "wire", "probe"} and is_promotion
        )
        if applies and self.injected_source is None:
            source = self._inject(source)
            self.injected_source = source
        assert "\u0412\u043d\u0435\u0448\u043d\u0438\u0435\u041e\u0431\u0440\u0430\u0431\u043e\u0442\u043a\u0438.\u041e\u0442\u043a\u043b\u044e\u0447\u0438\u0442\u044c" not in source
        self.real_calls += 1
        return self.wrapped(source)  # type: ignore[operator]

    def _inject(self, source: str) -> str:
        failure = f'    \u0412\u044b\u0437\u0432\u0430\u0442\u044c\u0418\u0441\u043a\u043b\u044e\u0447\u0435\u043d\u0438\u0435 "task9-{self.phase}-fault";'
        is_batch = "onec-worker-stage-batch-receipt" in source
        if is_batch and self.phase == "upload":
            start = source.index("АдресАртефактаWorker0 = Строка(")
            insertion = source.index("));", start) + len("));")
        elif is_batch and self.phase == "connect":
            insertion = source.index("ИмяАртефактаWorker0 = ВнешниеОбработки.Подключить(")
        elif self.phase == "upload":
            start = source.index(
                "\u0410\u0434\u0440\u0435\u0441\u0410\u0440\u0442\u0435\u0444\u0430\u043a\u0442\u0430Worker = \u041f\u043e\u043c\u0435\u0441\u0442\u0438\u0442\u044c\u0412\u043e\u0412\u0440\u0435\u043c\u0435\u043d\u043d\u043e\u0435\u0425\u0440\u0430\u043d\u0438\u043b\u0438\u0449\u0435("
            )
            insertion = source.index("));", start) + len("));")
        elif self.phase == "connect":
            insertion = source.index(
                "\u0418\u043c\u044f\u0410\u0440\u0442\u0435\u0444\u0430\u043a\u0442\u0430Worker = \u0412\u043d\u0435\u0448\u043d\u0438\u0435\u041e\u0431\u0440\u0430\u0431\u043e\u0442\u043a\u0438.\u041f\u043e\u0434\u043a\u043b\u044e\u0447\u0438\u0442\u044c("
            )
        elif self.phase == "create":
            start = source.index("\u0412\u043d\u0435\u0448\u043d\u0438\u0435\u041e\u0431\u0440\u0430\u0431\u043e\u0442\u043a\u0438.\u0421\u043e\u0437\u0434\u0430\u0442\u044c(")
            insertion = source.index("\n", start)
        elif self.phase == "wire":
            match = re.search(
                r"(?m)^\s*[A-Za-z_\u0400-\u04ff][A-Za-z0-9_\u0400-\u04ff]*"
                r"\.__OnecDependency_[0-9a-f]+ = "
                r"[A-Za-z_\u0400-\u04ff][A-Za-z0-9_\u0400-\u04ff]*;$",
                source,
            )
            assert match is not None
            insertion = match.end()
        else:
            phase = source.index('= "probe";')
            check = source.index("\u0415\u0441\u043b\u0438 \u0422\u0438\u043f\u0417\u043d\u0447(", phase)
            insertion = source.index("\u041a\u043e\u043d\u0435\u0446\u0415\u0441\u043b\u0438;", check) + len(
                "\u041a\u043e\u043d\u0435\u0446\u0415\u0441\u043b\u0438;"
            )
        return source[:insertion] + "\n" + failure + source[insertion:]


class _DropRealPromotionAcknowledgement:
    """Let the target swap succeed, then make its acknowledgement unknowable."""

    def __init__(self, wrapped: object) -> None:
        self.wrapped = wrapped
        self.receipt: object | None = None
        self.dropped = False
        self.real_calls = 0
        self.sources: list[str] = []

    def __call__(self, source: str) -> object:
        self.real_calls += 1
        self.sources.append(source)
        result = self.wrapped(source)  # type: ignore[operator]
        if (
            not self.dropped
            and "onec-worker-root-swap-receipt-v1" in source
        ):
            self.receipt = result
            self.dropped = True
            raise CommandTimeout("task9 dropped real promotion acknowledgement")
        return result


def _require_scalar(reply: RuntimeReply, expected: object) -> None:
    assert reply.kind is RuntimeReplyKind.MAIN_COMPLETED
    assert reply.succeeded is True, reply.error
    assert reply.result == expected
    assert reply.diagnostic is None


def _active_manifest(harness: LiveWorkerUniverseHarness) -> WorkerUniverseManifest:
    manifest = harness.host_registry.active_manifest
    assert isinstance(manifest, WorkerUniverseManifest)
    return manifest


def _module_registration(
    manifest: WorkerUniverseManifest,
    logical_name: str,
) -> str:
    matches = tuple(
        item.registration_name
        for item in manifest.modules
        if item.logical_name == logical_name
    )
    assert len(matches) == 1
    return matches[0]


def _worker_artifact_key(unit: WorkerModuleUnit) -> tuple[str, str, int, str, str]:
    return (
        unit.logical_name.casefold(),
        unit.kind,
        unit.revision,
        unit.mapped_source.artifact.source_sha256,
        unit.mapped_source.source_map_sha256,
    )


def _source_line(source: str, fragment: str) -> int:
    matches = tuple(
        index
        for index, line in enumerate(source.splitlines(), start=1)
        if fragment in line
    )
    assert len(matches) == 1
    return matches[0]


def _worker_source_ref(unit: WorkerModuleUnit) -> SourceUnitRef:
    kind = (
        SourceUnitKind.MODULE
        if unit.kind == "module"
        else SourceUnitKind.TEST_MODULE
    )
    return SourceUnitRef(
        kind,
        unit.logical_name,
        unit.revision,
        unit.mapped_source.artifact.source_sha256,
    )


def run_breakpoint_workspace_gate(
    harness: LiveWorkerUniverseHarness,
) -> dict[str, object]:
    contract = worker_universe_source_contract()
    handle = harness.session.load_worker_modules(contract.g17_units)
    line_a = _source_line(
        _MODULE_A_SOURCE,
        f"РезультатВызова = {MODULE_B}.ВыполнитьШаг(10);",
    )
    source_b = _module_b_source(17)
    line_b = _source_line(
        source_b,
        "Счетчик = НачальноеЗначение + 17;",
    )
    breakpoint_a = harness.session.runtime_api.add_worker_breakpoint(
        _worker_source_ref(contract.module_a),
        MODULE_A.casefold(),
        line_a,
    ).breakpoint.id
    harness.session.runtime_api.add_worker_breakpoint(
        _worker_source_ref(contract.module_b_g17),
        MODULE_B.casefold(),
        line_b,
    )

    def execute_and_collect() -> tuple[list[str], RuntimeReply]:
        reply = harness.session.execute_bsl(
            f"Результат = {MODULE_A}.ДанныеЗависимости();"
        )
        observed: list[str] = []
        while reply.kind is RuntimeReplyKind.DEBUG_STOPPED:
            assert reply.debug_stop is not None
            assert reply.debug_stop.origin == "main"
            observed.append(reply.debug_stop.location.canonical_module)  # type: ignore[union-attr]
            reply = harness.session.resume_debug_stop()
        return observed, reply

    ab_hits, first = execute_and_collect()
    assert first.kind is RuntimeReplyKind.MAIN_COMPLETED
    assert first.succeeded is True
    harness.session.remove_worker_breakpoint(breakpoint_a)
    b_only_hits, second = execute_and_collect()
    assert second.kind is RuntimeReplyKind.MAIN_COMPLETED
    assert second.succeeded is True
    harness.session.release_worker_generation(handle)
    return {
        "ab_hit_modules": ab_hits,
        "b_only_hit_modules": b_only_hits,
        "source_roundtrip_matches": ab_hits == [
            MODULE_A.casefold(),
            MODULE_B.casefold(),
        ],
        "discovery_step_calls": 0,
    }


def _enter_synthetic_capture(harness: LiveWorkerUniverseHarness) -> RuntimeReply:
    harness.session.configure_capture_points((_capture_location(harness),))
    captured = harness.session.execute_bsl(
        "СинтетическийРезультат = "
        "RuntimeKernelServer.СинтетическийCapture(100);\n"
        "Результат = СинтетическийРезультат;"
    )
    assert captured.kind is RuntimeReplyKind.CAPTURED
    assert captured.location == _capture_location(harness)
    return captured


def _execute_capture_worker_call(harness: LiveWorkerUniverseHarness) -> RuntimeReply:
    prepared = harness.session.runtime_api.prepare_capture_hypothesis(
        f"РезультатИнструкции = {MODULE_A}.ДанныеЗависимости();"
    )
    return harness.session.runtime_api.execute_prepared_capture_hypothesis(prepared)


def run_breakpoint_pinned_generation_gate(
    harness: LiveWorkerUniverseHarness,
) -> dict[str, object]:
    contract = worker_universe_source_contract()
    g17 = harness.session.load_worker_modules(contract.g17_units)
    _enter_synthetic_capture(harness)
    assert harness.session.runtime_api.operation_worker_generation is g17

    g18 = harness.session.load_worker_modules(contract.g18_units)
    line_b17 = _source_line(
        _module_b_source(17),
        "Счетчик = НачальноеЗначение + 17;",
    )
    line_b18 = _source_line(
        _module_b_source(18),
        "Счетчик = НачальноеЗначение + 18;",
    )
    pinned_status = harness.session.runtime_api.add_worker_breakpoint(
        _worker_source_ref(contract.module_b_g17),
        MODULE_B.casefold(),
        line_b17,
    )
    active_status = harness.session.runtime_api.add_worker_breakpoint(
        _worker_source_ref(contract.module_b_g18),
        MODULE_B.casefold(),
        line_b18,
    )
    assert pinned_status.installed_binding_count == 1
    assert active_status.installed_binding_count == 1

    capture_cell = _execute_capture_worker_call(harness)
    assert capture_cell.kind is RuntimeReplyKind.CAPTURE_CELL
    assert harness.session.runtime_api.operation_worker_generation is g17
    outer = harness.session.resume_capture()
    assert outer.kind is RuntimeReplyKind.MAIN_COMPLETED
    assert outer.succeeded is True

    active_stop = harness.session.execute_bsl(
        f"Результат = {MODULE_A}.ДанныеЗависимости();"
    )
    assert active_stop.kind is RuntimeReplyKind.DEBUG_STOPPED
    assert active_stop.debug_stop is not None
    active_frames = tuple(
        frame
        for frame in active_stop.debug_stop.frames
        if isinstance(frame, WorkerMappedFrame)
    )
    assert active_frames
    assert active_frames[0].generation is g18
    assert active_stop.debug_stop.breakpoint_ids == (active_status.breakpoint.id,)
    completed = harness.session.resume_debug_stop()
    assert completed.kind is RuntimeReplyKind.MAIN_COMPLETED
    harness.session.release_worker_generation(g18)
    return {
        "capture_result": str(capture_cell.result).split("|")[-1],
        "main_result": str(completed.result).split("|")[-1],
        "capture_breakpoint_ignored": capture_cell.kind
        is RuntimeReplyKind.CAPTURE_CELL,
        "pinned_generation_retained": any(
            item.generation is g17 for item in pinned_status.generations
        ),
        "active_generation_advanced": active_frames[0].generation is g18,
    }


def run_breakpoint_capture_evaluation_boundary_gate(
    harness: LiveWorkerUniverseHarness,
) -> dict[str, object]:
    contract = worker_universe_source_contract()
    g17 = harness.session.load_worker_modules(contract.g17_units)
    source_b = _module_b_source(17)
    line_b = _source_line(
        source_b,
        "Счетчик = НачальноеЗначение + 17;",
    )
    breakpoint_status = harness.session.runtime_api.add_worker_breakpoint(
        _worker_source_ref(contract.module_b_g17),
        MODULE_B.casefold(),
        line_b,
    )
    captured = _enter_synthetic_capture(harness)
    assert harness.session.runtime_api.operation_worker_generation is g17

    capture_cell = _execute_capture_worker_call(harness)
    assert capture_cell.kind is RuntimeReplyKind.CAPTURE_CELL
    assert capture_cell.debug_stop is None
    assert breakpoint_status.installed_binding_count == 1
    outer = harness.session.resume_capture()
    assert outer.kind is RuntimeReplyKind.MAIN_COMPLETED
    assert outer.succeeded is True
    main_stop = harness.session.execute_bsl(
        f"Результат = {MODULE_A}.ДанныеЗависимости();"
    )
    assert main_stop.kind is RuntimeReplyKind.DEBUG_STOPPED
    assert main_stop.debug_stop is not None
    assert main_stop.debug_stop.origin == "main"
    assert main_stop.debug_stop.breakpoint_ids == (breakpoint_status.breakpoint.id,)
    main_completed = harness.session.resume_debug_stop()
    assert main_completed.kind is RuntimeReplyKind.MAIN_COMPLETED
    assert main_completed.succeeded is True
    harness.session.release_worker_generation(g17)
    return {
        "result": str(capture_cell.result).split("|")[-1],
        "operation_id_stable": captured.operation_id == capture_cell.operation_id,
        "capture_breakpoint_ignored": capture_cell.debug_stop is None,
        "same_breakpoint_stops_in_main": main_stop.debug_stop.breakpoint_ids
        == (breakpoint_status.breakpoint.id,),
    }


@pytest.mark.integration
@pytest.mark.live_1c
@pytest.mark.timeout(600)
def test_worker_breakpoint_workspace_replacement_live(tmp_path: Path) -> None:
    with _fresh_live_harness(tmp_path) as harness:
        result = run_breakpoint_workspace_gate(harness)
        assert result["ab_hit_modules"] == [MODULE_A.casefold(), MODULE_B.casefold()]
        assert result["b_only_hit_modules"] == [MODULE_B.casefold()]
        assert result["discovery_step_calls"] == 0
        assert result["source_roundtrip_matches"] is True


@pytest.mark.integration
@pytest.mark.live_1c
@pytest.mark.timeout(600)
def test_worker_breakpoint_pinned_generations_live(tmp_path: Path) -> None:
    with _fresh_live_harness(tmp_path) as harness:
        result = run_breakpoint_pinned_generation_gate(harness)
        # New CAPTURE evaluations use the active catalog; the paused MAIN keeps G17.
        assert result["capture_result"] == "28"
        assert result["main_result"] == "28"
        assert result["capture_breakpoint_ignored"] is True
        assert result["pinned_generation_retained"] is True
        assert result["active_generation_advanced"] is True


@pytest.mark.integration
@pytest.mark.live_1c
@pytest.mark.timeout(600)
def test_worker_breakpoint_capture_evaluation_does_not_stop_live(
    tmp_path: Path,
) -> None:
    with _fresh_live_harness(tmp_path) as harness:
        result = run_breakpoint_capture_evaluation_boundary_gate(harness)
        assert result["result"] == "27"
        assert result["operation_id_stable"] is True
        assert result["capture_breakpoint_ignored"] is True
        assert result["same_breakpoint_stops_in_main"] is True


def _cached_worker_artifact(api: object, unit: WorkerModuleUnit):
    return api._worker_module_artifacts[_worker_artifact_key(unit)]  # type: ignore[attr-defined]


@pytest.mark.integration
@pytest.mark.live_1c
@pytest.mark.timeout(600)
def test_original_overloaded_cycle_and_registration_coexistence_live(
    tmp_path: Path,
) -> None:
    contract = worker_universe_source_contract()
    with _fresh_live_harness(tmp_path) as harness:
        api = harness.session.runtime_api
        counter = _CountingRealArtifactBuilder(api._worker_module_builder)
        api._worker_module_builder = counter

        original = harness.measure(
            "load.original_b",
            lambda: harness.session.load_worker_modules(
                contract.original_b_units,
            ),
        )
        assert isinstance(original, WorkerGenerationHandle)
        original_observation = harness.session.execute_bsl(
            f"\u0420\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442 = {MODULE_A}.\u0414\u0430\u043d\u043d\u044b\u0435\u0417\u0430\u0432\u0438\u0441\u0438\u043c\u043e\u0441\u0442\u0438();"
        )
        assert original_observation.succeeded is True
        original_parts = str(original_observation.result).split("|")
        assert original_parts[0]
        assert re.sub(r"\s+", "", original_parts[1].casefold()) == "\u043e\u0431\u0449\u0438\u0439\u043c\u043e\u0434\u0443\u043b\u044c"
        assert all(
            part.casefold() in {"\u0438\u0441\u0442\u0438\u043d\u0430", "\u0434\u0430"}
            for part in original_parts[2:4]
        )
        assert original_parts[4] == "12"

        g17 = harness.measure(
            "load.g17",
            lambda: harness.session.load_worker_modules(
                contract.g17_units,
            ),
        )
        g17_manifest = _active_manifest(harness)
        b17_registration = _module_registration(g17_manifest, MODULE_B)
        assert b17_registration in harness.target_registry._registrations
        assert b17_registration not in repr(g17)

        overloaded_observation = harness.session.execute_bsl(
            f"\u0420\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442 = {MODULE_A}.\u0414\u0430\u043d\u043d\u044b\u0435\u0417\u0430\u0432\u0438\u0441\u0438\u043c\u043e\u0441\u0442\u0438();"
        )
        assert overloaded_observation.succeeded is True
        overloaded_parts = str(overloaded_observation.result).split("|")
        assert overloaded_parts[0]
        assert re.sub(r"\s+", "", overloaded_parts[1].casefold()).startswith(
            "\u0432\u043d\u0435\u0448\u043d\u044f\u044f\u043e\u0431\u0440\u0430\u0431\u043e\u0442\u043a\u0430\u043e\u0431\u044a\u0435\u043a\u0442"
        )
        assert all(
            part.casefold() in {"\u0438\u0441\u0442\u0438\u043d\u0430", "\u0434\u0430"}
            for part in overloaded_parts[2:4]
        )
        assert overloaded_parts[4] == "27"
        assert original_parts[0] != overloaded_parts[0]
        assert original_parts[1] != overloaded_parts[1]

        _require_scalar(
            harness.session.execute_bsl(
                f"\u0420\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442 = {MODULE_A}.\u041f\u0440\u043e\u0432\u0435\u0440\u0438\u0442\u044c\u0426\u0438\u043a\u043b();"
            ),
            "A17->B17",
        )
        _require_scalar(
            harness.session.execute_bsl(
                f"\u0420\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442 = {MODULE_B}.\u041f\u0440\u043e\u0432\u0435\u0440\u0438\u0442\u044cSelf();"
            ),
            "B17",
        )

        _require_scalar(
            harness.session.execute_bsl(
                f'\u041a\u043e\u043d\u0442\u0435\u043a\u0441\u0442.\u0412\u0441\u0442\u0430\u0432\u0438\u0442\u044c("Task9HeldB17", '
                f'{MODULE_A}.\u041f\u043e\u043b\u0443\u0447\u0438\u0442\u044cB());\n'
                "\u0420\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442 = \u041a\u043e\u043d\u0442\u0435\u043a\u0441\u0442.Task9HeldB17.\u0412\u0435\u0440\u0441\u0438\u044f();"
            ),
            "B17",
        )
        g18 = harness.measure(
            "load.g18",
            lambda: harness.session.load_worker_modules(
                contract.g18_units,
            ),
        )
        g18_manifest = _active_manifest(harness)
        b18_registration = _module_registration(g18_manifest, MODULE_B)
        assert b18_registration != b17_registration
        assert {b17_registration, b18_registration} <= set(
            harness.target_registry._registrations
        )

        assert b17_registration in harness.target_registry._registrations
        assert harness.host_registry.registration_refcount(b17_registration) == 0
        assert b18_registration in harness.target_registry._registrations
        _require_scalar(
            harness.session.execute_bsl(
                "\u0420\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442 = \u041a\u043e\u043d\u0442\u0435\u043a\u0441\u0442.Task9HeldB17.\u0412\u0435\u0440\u0441\u0438\u044f();"
            ),
            "B17",
        )
        _require_scalar(
            harness.session.execute_bsl(
                f"\u0420\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442 = {MODULE_B}.\u0412\u0435\u0440\u0441\u0438\u044f();"
            ),
            "B18",
        )
        _require_scalar(
            harness.session.execute_bsl(
                '\u041a\u043e\u043d\u0442\u0435\u043a\u0441\u0442.\u0423\u0434\u0430\u043b\u0438\u0442\u044c("Task9HeldB17");\n'
                "\u0420\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442 = \u0418\u0441\u0442\u0438\u043d\u0430;"
            ),
            True,
        )
        assert counter.calls_by_module == {MODULE_A: 1, MODULE_B: 2}
        assert g18.generation == g17.generation + 1


def _capture_location(harness: LiveWorkerUniverseHarness):
    manifest = packaged_extension_bundle(
        harness.config.runtime_dir
    ).manifest
    source = (
        _REPOSITORY
        / "onec"
        / "OnecInteractiveRuntime"
        / "CommonModules"
        / "RuntimeKernelServer"
        / "Ext"
        / "Module.bsl"
    ).read_text(encoding="utf-8-sig")
    lines = tuple(
        number
        for number, text in enumerate(source.splitlines(), start=1)
        if SYNTHETIC_CAPTURE_A_MARKER in text
    )
    assert len(lines) == 1
    return replace(manifest.breakpoints.server_entry, line=lines[0])


def _settings_delete_source(item_key: str) -> str:
    return (
        "\u0425\u0440\u0430\u043d\u0438\u043b\u0438\u0449\u0435\u041e\u0431\u0449\u0438\u0445\u041d\u0430\u0441\u0442\u0440\u043e\u0435\u043a.\u0423\u0434\u0430\u043b\u0438\u0442\u044c("
        f'"{_SETTINGS_OBJECT_KEY}", "{item_key}", \u0418\u043c\u044f\u041f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u044f());\n'
        "\u0420\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442 = \u0425\u0440\u0430\u043d\u0438\u043b\u0438\u0449\u0435\u041e\u0431\u0449\u0438\u0445\u041d\u0430\u0441\u0442\u0440\u043e\u0435\u043a.\u0417\u0430\u0433\u0440\u0443\u0437\u0438\u0442\u044c("
        f'"{_SETTINGS_OBJECT_KEY}", "{item_key}") = \u041d\u0435\u043e\u043f\u0440\u0435\u0434\u0435\u043b\u0435\u043d\u043e;'
    )


def _settings_missing_source(item_key: str) -> str:
    return (
        "\u0420\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442 = \u041d\u0435 \u0422\u0440\u0430\u043d\u0437\u0430\u043a\u0446\u0438\u044f\u0410\u043a\u0442\u0438\u0432\u043d\u0430() \u0418 \u0425\u0440\u0430\u043d\u0438\u043b\u0438\u0449\u0435\u041e\u0431\u0449\u0438\u0445\u041d\u0430\u0441\u0442\u0440\u043e\u0435\u043a.\u0417\u0430\u0433\u0440\u0443\u0437\u0438\u0442\u044c("
        f'"{_SETTINGS_OBJECT_KEY}", "{item_key}") = \u041d\u0435\u043e\u043f\u0440\u0435\u0434\u0435\u043b\u0435\u043d\u043e;'
    )


def _transactional_capture_source(
    body: str,
    *,
    item_key: str,
    marker: str,
) -> str:
    indented_body = "\n".join("    " + line for line in body.splitlines())
    return (
        "\u041d\u0430\u0447\u0430\u0442\u044c\u0422\u0440\u0430\u043d\u0437\u0430\u043a\u0446\u0438\u044e();\n"
        "\u041f\u043e\u043f\u044b\u0442\u043a\u0430\n"
        "    \u0425\u0440\u0430\u043d\u0438\u043b\u0438\u0449\u0435\u041e\u0431\u0449\u0438\u0445\u041d\u0430\u0441\u0442\u0440\u043e\u0435\u043a.\u0421\u043e\u0445\u0440\u0430\u043d\u0438\u0442\u044c("
        f'"{_SETTINGS_OBJECT_KEY}", "{item_key}", "{marker}");\n'
        "    \u0415\u0441\u043b\u0438 \u0425\u0440\u0430\u043d\u0438\u043b\u0438\u0449\u0435\u041e\u0431\u0449\u0438\u0445\u041d\u0430\u0441\u0442\u0440\u043e\u0435\u043a.\u0417\u0430\u0433\u0440\u0443\u0437\u0438\u0442\u044c("
        f'"{_SETTINGS_OBJECT_KEY}", "{item_key}") <> "{marker}" \u0422\u043e\u0433\u0434\u0430\n'
        '        \u0412\u044b\u0437\u0432\u0430\u0442\u044c\u0418\u0441\u043a\u043b\u044e\u0447\u0435\u043d\u0438\u0435 "task9 transaction marker is not visible";\n'
        "    \u041a\u043e\u043d\u0435\u0446\u0415\u0441\u043b\u0438;\n"
        f"{indented_body}\n"
        "    \u041e\u0442\u043c\u0435\u043d\u0438\u0442\u044c\u0422\u0440\u0430\u043d\u0437\u0430\u043a\u0446\u0438\u044e();\n"
        "\u0418\u0441\u043a\u043b\u044e\u0447\u0435\u043d\u0438\u0435\n"
        "    \u0415\u0441\u043b\u0438 \u0422\u0440\u0430\u043d\u0437\u0430\u043a\u0446\u0438\u044f\u0410\u043a\u0442\u0438\u0432\u043d\u0430() \u0422\u043e\u0433\u0434\u0430\n"
        "        \u041e\u0442\u043c\u0435\u043d\u0438\u0442\u044c\u0422\u0440\u0430\u043d\u0437\u0430\u043a\u0446\u0438\u044e();\n"
        "    \u041a\u043e\u043d\u0435\u0446\u0415\u0441\u043b\u0438;\n"
        "    \u0412\u044b\u0437\u0432\u0430\u0442\u044c\u0418\u0441\u043a\u043b\u044e\u0447\u0435\u043d\u0438\u0435;\n"
        "\u041a\u043e\u043d\u0435\u0446\u041f\u043e\u043f\u044b\u0442\u043a\u0438;"
    )


def _run_two_promotion_capture_gate(
    harness: LiveWorkerUniverseHarness,
    *,
    transactional: bool,
) -> None:
    contract = worker_universe_source_contract()
    g17 = harness.session.load_worker_modules(
        contract.g17_units,
    )
    g17_manifest = _active_manifest(harness)
    b17_registration = _module_registration(g17_manifest, MODULE_B)
    harness.session.configure_capture_points((_capture_location(harness),))
    settings_item = f"generation-capture-{uuid4().hex}"
    settings_marker = f"transaction-marker-{uuid4().hex}"
    if transactional:
        _require_scalar(
            harness.session.execute_bsl(_settings_delete_source(settings_item)),
            True,
        )

    body = (
        f'\u0414\u043e\u041f\u0430\u0443\u0437\u044b = {MODULE_A}.\u041f\u0440\u043e\u0432\u0435\u0440\u0438\u0442\u044c\u0426\u0438\u043a\u043b();\n'
        "\u0421\u0438\u043d\u0442\u0435\u0442\u0438\u0447\u0435\u0441\u043a\u0438\u0439\u0420\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442 = RuntimeKernelServer.\u0421\u0438\u043d\u0442\u0435\u0442\u0438\u0447\u0435\u0441\u043a\u0438\u0439Capture(100);\n"
        f'\u041f\u043e\u0441\u043b\u0435\u041f\u0430\u0443\u0437\u044b = {MODULE_A}.\u041f\u0440\u043e\u0432\u0435\u0440\u0438\u0442\u044c\u0426\u0438\u043a\u043b();\n'
        "\u0420\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442 = \u0414\u043e\u041f\u0430\u0443\u0437\u044b + \"|\" + \u041f\u043e\u0441\u043b\u0435\u041f\u0430\u0443\u0437\u044b;"
    )
    if transactional:
        source = _transactional_capture_source(
            body,
            item_key=settings_item,
            marker=settings_marker,
        )
    else:
        source = body

    captured = harness.measure(
        "capture.enter.transaction" if transactional else "capture.enter.main",
        lambda: harness.session.execute_bsl(source),
    )
    assert captured.kind is RuntimeReplyKind.CAPTURED
    assert captured.location == _capture_location(harness)
    assert harness.session.runtime_api.operation_worker_generation is g17
    if transactional:
        transaction_active = harness.session._rdbg.evaluate(
            "\u0422\u0440\u0430\u043d\u0437\u0430\u043a\u0446\u0438\u044f\u0410\u043a\u0442\u0438\u0432\u043d\u0430()"
        )
        assert evaluation_to_python(transaction_active) is True
        stored_marker = harness.session._rdbg.evaluate(
            "\u0425\u0440\u0430\u043d\u0438\u043b\u0438\u0449\u0435\u041e\u0431\u0449\u0438\u0445\u041d\u0430\u0441\u0442\u0440\u043e\u0435\u043a.\u0417\u0430\u0433\u0440\u0443\u0437\u0438\u0442\u044c("
            f'"{_SETTINGS_OBJECT_KEY}", "{settings_item}")'
        )
        assert evaluation_to_python(stored_marker) == settings_marker

    g18 = harness.session.load_worker_modules(
        contract.g18_units,
    )
    g19 = harness.session.load_worker_modules(
        contract.g19_units,
    )
    assert g18.generation == g17.generation + 1
    assert g19.generation == g18.generation + 1
    g19_manifest = _active_manifest(harness)
    live_while_stopped = harness.host_registry._confirmed_live_inventory()
    assert live_while_stopped is not None
    assert live_while_stopped.manifest_sha256s == frozenset(
        (g17_manifest.sha256, g19_manifest.sha256)
    )
    assert {
        view.handle for view in harness.host_registry._retained_debug_views()
    } == {g17, g19}
    assert b17_registration in harness.target_registry._registrations
    assert harness.host_registry.registration_refcount(b17_registration) > 0

    for _ in range(2):
        prepared = harness.session.runtime_api.prepare_capture_hypothesis(
            f"РезультатИнструкции = {MODULE_A}.\u041f\u0440\u043e\u0432\u0435\u0440\u0438\u0442\u044c\u0426\u0438\u043a\u043b();"
        )
        hypothesis = (
            harness.session.runtime_api.execute_prepared_capture_hypothesis(
                prepared
            )
        )
        assert hypothesis.kind is RuntimeReplyKind.CAPTURE_CELL
        assert hypothesis.result == "A17->B19"
        assert harness.session.runtime_api.operation_worker_generation is g17

    resumed = harness.measure(
        "capture.resume.transaction" if transactional else "capture.resume.main",
        harness.session.runtime_api.resume_capture,
    )
    _require_scalar(resumed, "A17->B17|A17->B17")
    assert b17_registration in harness.target_registry._registrations
    assert harness.host_registry.registration_refcount(b17_registration) == 0
    live_after_terminal = harness.host_registry._confirmed_live_inventory()
    assert live_after_terminal is not None
    assert live_after_terminal.manifest_sha256s == frozenset((g19_manifest.sha256,))
    assert {
        view.handle for view in harness.host_registry._retained_debug_views()
    } == {g19}
    _require_scalar(
        harness.session.execute_bsl(
            f"\u0420\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442 = {MODULE_A}.\u041f\u0440\u043e\u0432\u0435\u0440\u0438\u0442\u044c\u0426\u0438\u043a\u043b();"
        ),
        "A17->B19",
    )
    if transactional:
        _require_scalar(
            harness.session.execute_bsl(_settings_missing_source(settings_item)),
            True,
        )
        _require_scalar(
            harness.session.execute_bsl(_settings_delete_source(settings_item)),
            True,
        )


@pytest.mark.integration
@pytest.mark.live_1c
@pytest.mark.timeout(600)
@pytest.mark.parametrize("transactional", (False, True), ids=("main", "transaction"))
def test_g17_capture_pin_survives_g18_g19_promotions_live(
    tmp_path: Path,
    transactional: bool,
) -> None:
    with _fresh_live_harness(tmp_path) as harness:
        _run_two_promotion_capture_gate(
            harness,
            transactional=transactional,
        )


def _assert_g17_remains_active(
    harness: LiveWorkerUniverseHarness,
    handle: WorkerGenerationHandle,
    manifest: WorkerUniverseManifest,
    registrations: dict[str, tuple[str, str]],
) -> None:
    assert harness.host_registry.active_handle is handle
    assert _active_manifest(harness) == manifest
    assert registrations.items() <= harness.target_registry._registrations.items()
    _require_scalar(
        harness.session.execute_bsl(
            f"\u0420\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442 = {MODULE_A}.\u041f\u0440\u043e\u0432\u0435\u0440\u0438\u0442\u044c\u0426\u0438\u043a\u043b();"
        ),
        "A17->B17",
    )


def _target_registration_is_unavailable(
    production_executor: object,
    registration: str,
) -> bool:
    result = production_executor(  # type: ignore[operator]
        "\u041f\u043e\u043f\u044b\u0442\u043a\u0430\n"
        "    \u041e\u0431\u044a\u0435\u043a\u0442\u041f\u0440\u043e\u0432\u0435\u0440\u043a\u0438Worker = \u0412\u043d\u0435\u0448\u043d\u0438\u0435\u041e\u0431\u0440\u0430\u0431\u043e\u0442\u043a\u0438.\u0421\u043e\u0437\u0434\u0430\u0442\u044c("
        f'"{registration}", \u041b\u043e\u0436\u044c);\n'
        "    \u0420\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442 = \u041b\u043e\u0436\u044c;\n"
        "\u0418\u0441\u043a\u043b\u044e\u0447\u0435\u043d\u0438\u0435\n"
        "    \u0420\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442 = \u0418\u0441\u0442\u0438\u043d\u0430;\n"
        "\u041a\u043e\u043d\u0435\u0446\u041f\u043e\u043f\u044b\u0442\u043a\u0438;"
    )
    return result is True


@pytest.mark.integration
@pytest.mark.live_1c
@pytest.mark.timeout(600)
def test_compile_and_runtime_diagnostics_are_mapped_from_real_1c(
    tmp_path: Path,
) -> None:
    contract = worker_universe_source_contract()
    with _fresh_live_harness(tmp_path) as harness:
        api = harness.session.runtime_api
        g17 = harness.session.load_worker_modules(
            contract.g17_units,
        )
        manifest = _active_manifest(harness)
        registrations = dict(harness.target_registry._registrations)

        production_builder = api._worker_module_builder
        generated_failure = _InvalidGeneratedAliasBuilder(
            production_builder,
            revision=28,
        )
        api._worker_module_builder = generated_failure
        b28 = _unit(MODULE_B, 28, _module_b_source(28), contract.catalog)
        try:
            with pytest.raises(BslExecutionError) as generated_raised:
                harness.session.load_worker_modules(
                    (contract.module_a, b28),
                )
        finally:
            api._worker_module_builder = production_builder
        assert generated_failure.mutated is True
        generated = generated_raised.value.diagnostic
        assert generated is not None
        assert generated.stage is DiagnosticStage.COMPILATION, generated.platform_diagnostic
        assert generated.mapping_confidence is MappingConfidence.NEAREST
        assert generated.code == "dependency_binding"
        assert generated.synthetic_region == "dependency_binding"
        assert generated.source_unit is not None
        assert (generated.source_unit.unit_id, generated.source_unit.revision) == (
            MODULE_B,
            28,
        )
        assert generated.source_unit.source_sha256 == source_sha256(
            _module_b_source(28)
        )
        assert generated.visible_location is None
        # Compact reload maps anchor generated alias initialization to its method.
        assert generated.related_visible_span == SourceSpan(253, 354)
        assert LineIndex(_module_b_source(28)).offset_to_line_column(292) == (
            13,
            13,
        )
        assert generated.dependency_anchor == SourceSpan(253, 354)
        assert generated.method_anchor == SourceSpan(253, 354)
        assert generated_failure.expected_lowered_offset is not None
        assert generated_failure.mutated_source is not None
        expected_generated_offset = generated_failure.expected_lowered_offset
        assert expected_generated_offset == 447
        expected_generated_line, expected_generated_column = LineIndex(
            generated_failure.mutated_source.text
        ).offset_to_line_column(expected_generated_offset)
        assert (expected_generated_line, expected_generated_column) == (16, 37)
        assert generated.lowered_location is not None
        assert (
            generated.lowered_location.line,
            generated.lowered_location.column,
            generated.lowered_location.offset,
            generated.lowered_location.span,
        ) == (
            expected_generated_line,
            expected_generated_column,
            expected_generated_offset,
            SourceSpan(expected_generated_offset, expected_generated_offset + 1),
        )
        assert generated_failure.artifact is not None
        generated_registration = worker_registration_name(
            MODULE_B,
            generated_failure.artifact.artifact_sha256,
        )
        assert generated.execution_artifact_sha256 == (
            generated_failure.artifact.source_sha256
        )
        assert generated.source_map_sha256 == (
            generated_failure.artifact.source_map_sha256
        )
        assert generated.platform_diagnostic is not None
        parsed_generated = parse_platform_diagnostic(
            generated.platform_diagnostic
        )
        assert parsed_generated.has_compilation_marker is True
        generated_locations = tuple(
            location
            for location in parsed_generated.locations
            if location.worker_artifact_location is not None
        )
        assert tuple(
            location.worker_artifact_location.registration_name
            for location in generated_locations
            if location.worker_artifact_location is not None
        ) == (generated_registration,)
        assert tuple(
            (location.line, location.column) for location in generated_locations
        ) == (
            (expected_generated_line, expected_generated_column),
        )
        _assert_g17_remains_active(harness, g17, manifest, registrations)

        bad_exact_source = (
            "\u0424\u0443\u043d\u043a\u0446\u0438\u044f \u041c\u0435\u0442\u043a\u0430A() \u042d\u043a\u0441\u043f\u043e\u0440\u0442\n"
            "    \u0412\u043e\u0437\u0432\u0440\u0430\u0442 Task9\u041d\u0435\u043e\u043f\u0440\u0435\u0434\u0435\u043b\u0435\u043d\u043d\u0430\u044f\u041f\u0435\u0440\u0435\u043c\u0435\u043d\u043d\u0430\u044f;\n"
            "\u041a\u043e\u043d\u0435\u0446\u0424\u0443\u043d\u043a\u0446\u0438\u0438\n"
        )
        bad_exact = _unit(MODULE_A, 29, bad_exact_source, contract.catalog)
        with pytest.raises(BslExecutionError) as exact_raised:
            harness.session.load_worker_modules(
                (bad_exact,),
            )
        exact = exact_raised.value.diagnostic
        assert exact is not None
        assert exact.stage is DiagnosticStage.COMPILATION
        assert exact.mapping_confidence is MappingConfidence.EXACT
        assert exact.source_unit is not None
        assert (exact.source_unit.unit_id, exact.source_unit.revision) == (
            MODULE_A,
            29,
        )
        assert exact.source_unit.source_sha256 == source_sha256(bad_exact_source)
        exact_offset = bad_exact_source.index(
            "Task9\u041d\u0435\u043e\u043f\u0440\u0435\u0434\u0435\u043b\u0435\u043d\u043d\u0430\u044f\u041f\u0435\u0440\u0435\u043c\u0435\u043d\u043d\u0430\u044f"
        )
        assert LineIndex(bad_exact_source).offset_to_line_column(exact_offset) == (
            2,
            13,
        )
        assert exact.visible_location is not None
        assert (
            exact.visible_location.line,
            exact.visible_location.column,
            exact.visible_location.span,
        ) == (2, 13, SourceSpan(exact_offset, exact_offset + 1))
        assert exact.related_visible_span is None
        assert exact.dependency_anchor is None
        assert exact.method_anchor is None
        assert exact.lowered_location is not None
        assert (
            exact.lowered_location.line,
            exact.lowered_location.column,
            exact.lowered_location.offset,
            exact.lowered_location.span,
        ) == (2, 13, exact_offset, SourceSpan(exact_offset, exact_offset + 1))
        assert _worker_artifact_key(bad_exact) not in api._worker_module_artifacts
        assert exact.execution_artifact_sha256 is not None
        assert len(exact.execution_artifact_sha256) == 64
        assert exact.source_map_sha256 is not None
        assert len(exact.source_map_sha256) == 64
        assert exact.platform_diagnostic is not None
        parsed_exact = parse_platform_diagnostic(exact.platform_diagnostic)
        assert parsed_exact.has_compilation_marker is True
        exact_locations = tuple(
            location
            for location in parsed_exact.locations
            if location.worker_artifact_location is not None
        )
        assert len(exact_locations) == 1
        exact_registration = exact_locations[0].worker_artifact_location
        assert exact_registration is not None
        assert exact_registration.registration_name.startswith("OnecRuntime_")
        assert tuple(
            (location.line, location.column) for location in exact_locations
        ) == (
            (2, 13),
        )
        _assert_g17_remains_active(harness, g17, manifest, registrations)

        runtime = harness.session.execute_bsl(
            f"\u0420\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442 = {MODULE_A}.\u0410\u0432\u0430\u0440\u0438\u044fA();"
        )
        assert runtime.kind is RuntimeReplyKind.MAIN_COMPLETED
        assert runtime.succeeded is False
        assert runtime.diagnostic is not None
        assert runtime.diagnostic.stage is DiagnosticStage.EXECUTION
        assert [
            frame.logical_name for frame in runtime.diagnostic.worker_frames
        ] == [MODULE_B, MODULE_A]
        assert [
            frame.revision for frame in runtime.diagnostic.worker_frames
        ] == [17, 17]
        b_frame, a_frame = runtime.diagnostic.worker_frames
        assert b_frame.mapping_confidence is MappingConfidence.EXACT
        assert a_frame.mapping_confidence is MappingConfidence.EXACT
        assert b_frame.visible_location is not None
        assert a_frame.visible_location is not None
        assert (
            b_frame.visible_location.line,
            b_frame.visible_location.column,
            b_frame.visible_location.span,
        ) == (21, 5, SourceSpan(483, 484))
        assert (
            a_frame.visible_location.line,
            a_frame.visible_location.column,
            a_frame.visible_location.span,
        ) == (45, 5, SourceSpan(1256, 1257))
        assert b_frame.source_unit == b_frame.visible_location.source_unit
        assert a_frame.source_unit == a_frame.visible_location.source_unit
        assert (
            b_frame.source_unit.unit_id,
            b_frame.source_unit.revision,
            b_frame.source_unit.source_sha256,
            a_frame.source_unit.unit_id,
            a_frame.source_unit.revision,
            a_frame.source_unit.source_sha256,
        ) == (
            MODULE_B,
            17,
            contract.module_b_g17.mapped_source.artifact.source_sha256,
            MODULE_A,
            17,
            contract.module_a.mapped_source.artifact.source_sha256,
        )
        assert b_frame.related_visible_span is None
        assert a_frame.related_visible_span is None
        assert b_frame.dependency_anchor is None
        assert a_frame.dependency_anchor is None
        assert b_frame.method_anchor is None
        assert a_frame.method_anchor is None
        assert b_frame.lowered_location is not None
        assert a_frame.lowered_location is not None
        assert runtime.diagnostic.platform_diagnostic is not None
        admitted_diagnostics = {
            item.logical_name: item
            for item in api._worker_generation_diagnostics[manifest.sha256]
        }
        manifest_modules = {item.logical_name: item for item in manifest.modules}
        expected_runtime_registrations = (
            manifest_modules[MODULE_B].registration_name,
            manifest_modules[MODULE_A].registration_name,
        )
        assert (b_frame.registration_name, a_frame.registration_name) == (
            expected_runtime_registrations
        )
        assert (b_frame.artifact_sha256, a_frame.artifact_sha256) == (
            manifest_modules[MODULE_B].artifact_sha256,
            manifest_modules[MODULE_A].artifact_sha256,
        )
        assert (b_frame.artifact_sha256, a_frame.artifact_sha256) == (
            admitted_diagnostics[MODULE_B].artifact_sha256,
            admitted_diagnostics[MODULE_A].artifact_sha256,
        )
        assert admitted_diagnostics[MODULE_B].source_map_sha256 == (
            _cached_worker_artifact(api, contract.module_b_g17).source_map_sha256
        )
        assert admitted_diagnostics[MODULE_A].source_map_sha256 == (
            _cached_worker_artifact(api, contract.module_a).source_map_sha256
        )
        b_lowered_offset = admitted_diagnostics[MODULE_B].mapped_source.text.index(
            "\u0412\u044b\u0437\u0432\u0430\u0442\u044c\u0418\u0441\u043a\u043b\u044e\u0447\u0435\u043d\u0438\u0435"
        )
        a_lowered_offset = admitted_diagnostics[MODULE_A].mapped_source.text.index(
            f"Возврат {MODULE_B}.\u0410\u0432\u0430\u0440\u0438\u044f()"
        )
        b_lowered_coordinates = LineIndex(
            admitted_diagnostics[MODULE_B].mapped_source.text
        ).offset_to_line_column(b_lowered_offset)
        a_lowered_coordinates = LineIndex(
            admitted_diagnostics[MODULE_A].mapped_source.text
        ).offset_to_line_column(a_lowered_offset)
        assert (b_lowered_offset, *b_lowered_coordinates) == (789, 27, 5)
        assert (a_lowered_offset, *a_lowered_coordinates) == (1913, 58, 5)
        assert (
            b_frame.lowered_location.line,
            b_frame.lowered_location.column,
            b_frame.lowered_location.offset,
            b_frame.lowered_location.span,
        ) == (
            *b_lowered_coordinates,
            b_lowered_offset,
            SourceSpan(b_lowered_offset, b_lowered_offset + 1),
        )
        assert (
            a_frame.lowered_location.line,
            a_frame.lowered_location.column,
            a_frame.lowered_location.offset,
            a_frame.lowered_location.span,
        ) == (
            *a_lowered_coordinates,
            a_lowered_offset,
            SourceSpan(a_lowered_offset, a_lowered_offset + 1),
        )
        assert runtime.diagnostic.source_map_sha256 == (
            admitted_diagnostics[MODULE_B].source_map_sha256
        )
        assert runtime.diagnostic.execution_artifact_sha256 == (
            admitted_diagnostics[MODULE_B].mapped_source.artifact.source_sha256
        )
        parsed_runtime = parse_platform_diagnostic(
            runtime.diagnostic.platform_diagnostic
        )
        runtime_registrations = tuple(
            location.worker_artifact_location.registration_name
            for location in parsed_runtime.locations
            if location.worker_artifact_location is not None
        )
        assert runtime_registrations == expected_runtime_registrations
        runtime_locations = tuple(
            location
            for location in parsed_runtime.locations
            if location.worker_artifact_location is not None
        )
        assert tuple(
            (location.line, location.column) for location in runtime_locations
        ) == (
            # The native runtime stack reports lines; mapping resolves statement columns.
            (b_frame.lowered_location.line, None),
            (a_frame.lowered_location.line, None),
        )


@pytest.mark.integration
@pytest.mark.live_1c
@pytest.mark.timeout(600)
def test_real_phase_failures_are_atomic_and_retain_connected_candidates(
    tmp_path: Path,
) -> None:
    contract = worker_universe_source_contract()
    with _fresh_live_harness(tmp_path) as harness:
        g17 = harness.session.load_worker_modules(
            contract.g17_units,
        )
        manifest = _active_manifest(harness)
        registrations = dict(harness.target_registry._registrations)
        target = harness.target_registry
        production_executor = target._instruction_executor
        host = harness.host_registry
        refcounts = dict(host._registration_refcounts)
        registration_artifacts = dict(host._registration_artifacts)
        quarantine_holds = set(host._quarantine_holds)
        candidate_registrations = dict(target._candidate_registrations)
        for revision, phase in enumerate(
            ("upload", "create", "wire", "probe"),
            start=30,
        ):
            fault = _RealPhaseFaultExecutor(production_executor, phase)
            target._instruction_executor = fault
            candidate_b = _unit(
                MODULE_B,
                revision,
                _module_b_source(revision),
                contract.catalog,
            )
            try:
                with pytest.raises(BslExecutionError) as raised:
                    harness.session.load_worker_modules(
                        (contract.module_a, candidate_b),
                    )
            finally:
                target._instruction_executor = production_executor
            assert fault.injected_source is not None
            assert f"task9-{phase}-fault" in fault.injected_source
            assert fault.real_calls == {
                "upload": 1,
                "create": 2,
                "wire": 2,
                "probe": 2,
            }[phase]
            if phase in {"create", "wire", "probe"}:
                assert ".\u041f\u043e\u043b\u0443\u0447\u0438\u0442\u044c(" in fault.injected_source
            if phase in {"upload", "connect"}:
                assert f"task9-{phase}-fault" in str(raised.value)
            else:
                assert (
                    f"onec-worker-root-prepare-stage={phase}"
                    in str(raised.value)
                )
            assert (
                _worker_artifact_key(candidate_b)
                not in harness.session.runtime_api._worker_module_artifacts
            )
            injected_registrations = set(
                re.findall(
                    r"OnecRuntime_[0-9a-f]{8}_[0-9a-f]{16}",
                    fault.injected_source,
                    re.IGNORECASE,
                )
            )
            candidate_only = injected_registrations - set(registrations)
            assert len(candidate_only) == 1
            candidate_registration = candidate_only.pop()
            assert target._candidate_registrations == candidate_registrations
            if phase in {"upload", "connect"}:
                assert candidate_registration not in target._registrations
                assert _target_registration_is_unavailable(
                    production_executor,
                    candidate_registration,
                )
            else:
                assert candidate_registration in target._registrations
                assert not _target_registration_is_unavailable(
                    production_executor,
                    candidate_registration,
                )
            assert host._state.value == "ready"
            assert host._pending is None
            assert host._registration_refcounts == refcounts
            assert host._registration_artifacts == registration_artifacts
            assert host._quarantine_holds == quarantine_holds
            _assert_g17_remains_active(
                harness,
                g17,
                manifest,
                registrations,
            )


@pytest.mark.integration
@pytest.mark.live_1c
@pytest.mark.timeout(600)
def test_real_connect_failure_blocks_runtime_and_retains_cleanup_ownership(
    tmp_path: Path,
) -> None:
    contract = worker_universe_source_contract()
    with _fresh_live_harness(tmp_path) as harness:
        g17 = harness.session.load_worker_modules(contract.g17_units)
        target = harness.target_registry
        production_executor = target._instruction_executor
        fault = _RealPhaseFaultExecutor(production_executor, "connect")
        target._instruction_executor = fault
        try:
            with pytest.raises(WorkerPromotionOutcomeUnknown):
                harness.session.load_worker_modules(contract.g18_units)
        finally:
            target._instruction_executor = production_executor
        assert fault.injected_source is not None
        assert fault.real_calls == 1
        assert target._broken is True
        assert harness.host_registry._state.value == "broken"
        with pytest.raises(ProtocolError, match="unavailable in broken state"):
            _ = harness.host_registry.active_handle
        assert target._orphan_urls
        assert target._candidate_registrations
        with pytest.raises(WorkerPromotionOutcomeUnknown):
            harness.session.load_worker_modules(contract.g19_units)


@pytest.mark.integration
@pytest.mark.live_1c
@pytest.mark.timeout(600)
def test_lost_real_swap_acknowledgement_poisons_runtime_and_cleans_on_close(
    tmp_path: Path,
) -> None:
    contract = worker_universe_source_contract()
    with _fresh_live_harness(tmp_path) as harness:
        g17 = harness.session.load_worker_modules(
            contract.g17_units,
        )
        g17_manifest = _active_manifest(harness)
        b17 = _module_registration(g17_manifest, MODULE_B)
        target = harness.target_registry
        production_executor = target._instruction_executor
        dropped = _DropRealPromotionAcknowledgement(production_executor)
        target._instruction_executor = dropped
        with pytest.raises(WorkerPromotionOutcomeUnknown) as raised:
            harness.session.load_worker_modules(
                contract.g18_units,
            )

        assert dropped.dropped is True
        assert raised.value.generation == g17.generation + 1
        receipt = worker_universe._worker_promotion_receipt(dropped.receipt)
        assert receipt.generation == raised.value.generation
        assert receipt.manifest_sha256 == raised.value.manifest_sha256
        assert receipt.root_key == f"generation-{raised.value.generation}"
        assert receipt.acknowledged is True
        assert type(receipt.generation_create_wire_probe_ms) is int
        assert receipt.generation_create_wire_probe_ms >= 0
        assert type(receipt.root_swap_ms) is int
        assert receipt.root_swap_ms >= 0
        assert harness.host_registry._state.value == "broken"
        assert target._broken is True
        assert (
            _worker_artifact_key(contract.module_b_g18)
            not in harness.session.runtime_api._worker_module_artifacts
        )
        candidate_only = set(target._registrations) - {
            item.registration_name for item in g17_manifest.modules
        }
        assert len(candidate_only) == 1
        b18 = candidate_only.pop()
        assert set(target._registrations) == {
            item.registration_name for item in g17_manifest.modules
        } | {b18}
        assert b17 in target._registrations
        assert harness.host_registry._quarantine_holds == {b18}
        assert harness.host_registry.registration_refcount(b18) == 1
        target_calls_after_lost_ack = dropped.real_calls
        with pytest.raises(WorkerPromotionOutcomeUnknown) as rejected_call:
            harness.session.execute_bsl(
                f"\u0420\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442 = {MODULE_B}.\u0412\u0435\u0440\u0441\u0438\u044f();"
            )
        assert rejected_call.value is raised.value
        assert dropped.real_calls == target_calls_after_lost_ack
        with pytest.raises(WorkerPromotionOutcomeUnknown) as poisoned:
            harness.session.status()
        assert poisoned.value is raised.value
        assert dropped.real_calls == target_calls_after_lost_ack
        target._instruction_executor = production_executor

    with _fresh_live_harness(tmp_path / "restart") as restarted:
        restarted.session.load_worker_modules(
            contract.g19_units,
        )
        _require_scalar(
            restarted.session.execute_bsl(
                f"\u0420\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442 = {MODULE_B}.\u0412\u0435\u0440\u0441\u0438\u044f();"
            ),
            "B19",
        )
