from __future__ import annotations

import json
import traceback
import warnings
from contextlib import contextmanager
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

import onec_runtime.errors as errors_module
import onec_runtime.session as session_module
from onec_runtime.bsl import (
    SourceUnitKind,
    SourceUnitRef,
    WorkerModuleUnit,
    mapped_visible_source,
    source_sha256,
)
from onec_runtime.config import RuntimeConfig
from onec_runtime.errors import (
    CommandTimeout,
    ExtensionHandshakeError,
    ExtensionIdentityConflict,
    ExtensionLifecycleError,
    ProcessStartError,
    ProtocolError,
    RdbgDebugUiNotRegistered,
    TargetLost,
    UnexpectedStop,
)
from onec_runtime.extension_bundle import (
    ExtensionBundle,
    ExtensionHandshakeEvidence,
    read_extension_manifest,
)
from onec_runtime.extension_lifecycle import (
    LifecycleDecision,
    LifecycleMode,
    TargetExtensionState,
)
from onec_runtime.performance_profile import PhaseRecorder
from onec_runtime.rdbg.models import (
    DebugTarget,
    EvaluationResult,
    ModuleLocation,
    StopEvent,
    TargetId,
)
from onec_runtime.session import (
    ExtensionMode,
    RuntimeSession,
    RuntimeSessionConfig,
    runtime_bootstrap_locations,
)

from onec_runtime.worker_universe import WorkerGenerationHandle

WORKSPACE = Path(__file__).parents[2]
RESOURCE_ROOT = WORKSPACE / "src" / "onec_runtime" / "resources" / "extension"
MANIFEST = read_extension_manifest(RESOURCE_ROOT / "extension-manifest.json")
BUNDLE = ExtensionBundle(
    RESOURCE_ROOT / MANIFEST.cfe_filename,
    MANIFEST,
    RESOURCE_ROOT,
)
TARGET_ID = TargetId(
    UUID("11111111-1111-1111-1111-111111111111"),
    "DefAlias",
    UUID("22222222-2222-2222-2222-222222222222"),
)
HANDSHAKE_VALUES = {
    "ИдентификаторПродуктаRuntime": MANIFEST.product_id,
    "ВерсияАртефактаRuntime": MANIFEST.artifact_version,
    "ВерсияПротоколаRuntime": MANIFEST.protocol_version,
}


@dataclass
class AttemptProbe:
    rdbg_options: list[dict[str, object]] = field(default_factory=list)
    managed_startup_timeouts: list[float] = field(default_factory=list)
    server_startup_timeouts: list[float] = field(default_factory=list)
    bound_server_sessions: list[int] = field(default_factory=list)
    attempt_count: int = 0
    managed_errors: dict[int, BaseException] = field(default_factory=dict)
    registration_errors: dict[int, BaseException] = field(default_factory=dict)
    handshake_values: dict[tuple[int, str, str], str] = field(default_factory=dict)
    safe_mode_presentations: dict[int, str] = field(default_factory=dict)
    safe_mode_checks: list[int] = field(default_factory=list)
    server_entry_errors: dict[int, BaseException] = field(default_factory=dict)
    server_target_id: TargetId = TARGET_ID
    service_target_id: TargetId | None = None
    handshake_errors: dict[tuple[int, str], ExtensionHandshakeError] = field(
        default_factory=dict
    )
    server_mismatch_attempts: set[int] = field(default_factory=set)
    service_errors: dict[int, BaseException] = field(default_factory=dict)
    process_errors: dict[int, BaseException] = field(default_factory=dict)
    process_health_errors: dict[int, BaseException] = field(default_factory=dict)
    cleanup_errors: dict[int, BaseException] = field(default_factory=dict)
    termination_failures: dict[int, int] = field(default_factory=dict)
    detach_failures: dict[int, int] = field(default_factory=dict)
    termination_calls: list[int] = field(default_factory=list)
    detach_calls: list[int] = field(default_factory=list)
    closed_transports: list[int] = field(default_factory=list)
    closed_attempts: list[int] = field(default_factory=list)
    runtime_api_kwargs: list[dict[str, object]] = field(default_factory=list)
    module_builder_args: list[tuple[object, dict[str, object]]] = field(
        default_factory=list
    )


class FakeLifecycle:
    def __init__(
        self,
        *,
        decisions: list[LifecycleDecision | BaseException],
        first_handshake_error: ExtensionHandshakeError | None = None,
        manual_handshake_error: ExtensionLifecycleError | None = None,
        server_target_type: str = "ServerEmulation",
    ) -> None:
        self.server_target_type = server_target_type
        self.decisions = list(decisions)
        self.first_handshake_error = first_handshake_error
        self.manual_handshake_error = manual_handshake_error
        self.events: list[str] = []
        self.tool_calls: list[str] = []
        self.prepare_count = 0
        self.profiler: PhaseRecorder | None = None
        self.attempt_probe: AttemptProbe | None = None

    def prepare(self, *, force_slow: bool = False) -> LifecycleDecision:
        self.prepare_count += 1
        self.events.append("prepare-force-slow" if force_slow else "prepare")
        decision = self.decisions.pop(0)
        if isinstance(decision, BaseException):
            raise decision
        return decision

    def invalidate_marker(self) -> None:
        assert self.attempt_probe is not None
        assert self.attempt_probe.closed_transports == [1]
        assert self.attempt_probe.closed_attempts == [1]
        self.events.append("invalidate")

    def prepare_manual(self) -> LifecycleDecision:
        self.prepare_count += 1
        self.events.append("prepare-manual")
        decision = self.decisions.pop(0)
        if isinstance(decision, BaseException):
            raise decision
        return decision

    def accept_manual_handshake(
        self,
        evidence: tuple[ExtensionHandshakeEvidence, ExtensionHandshakeEvidence],
    ) -> str:
        assert self.profiler is not None
        self.profiler.measure("extension.handshake", lambda: None)
        self.events.append("accept-manual")
        if self.manual_handshake_error is not None:
            raise self.manual_handshake_error
        assert evidence[0].artifact_version == evidence[1].artifact_version
        return evidence[0].artifact_version

    def commit_handshake(
        self,
        evidence: tuple[ExtensionHandshakeEvidence, ExtensionHandshakeEvidence],
    ) -> None:
        assert [item.target_type for item in evidence] == [
            "ManagedClient",
            self.server_target_type,
        ]
        assert self.profiler is not None
        self.profiler.measure("extension.handshake", lambda: None)
        self.events.append("commit")


def fast_decision() -> LifecycleDecision:
    return LifecycleDecision(
        LifecycleMode.FAST,
        TargetExtensionState.CURRENT,
        BUNDLE,
        retry_allowed=True,
    )


def slow_decision() -> LifecycleDecision:
    return LifecycleDecision(
        LifecycleMode.SLOW,
        TargetExtensionState.CURRENT,
        BUNDLE,
        retry_allowed=False,
    )


def manual_decision() -> LifecycleDecision:
    return LifecycleDecision(
        LifecycleMode.MANUAL,
        TargetExtensionState.USER_MANAGED,
        BUNDLE,
        retry_allowed=False,
    )


def session_config(root: Path) -> RuntimeSessionConfig:
    platform_bin = root / "bin"
    platform_bin.mkdir()
    for executable in ("1cv8.exe", "1cv8c.exe", "dbgs.exe"):
        (platform_bin / executable).touch()
    infobase = root / "base"
    infobase.mkdir()
    (infobase / "1Cv8.1CD").write_bytes(b"test-infobase")
    runtime = RuntimeConfig(
        root,
        platform_bin,
        connection_string=f'File="{infobase}";',
        username="private-operator",
    )
    return RuntimeSessionConfig(runtime, root / "evidence")


def test_runtime_session_config_defaults_to_automatic_extension_mode(
    tmp_path: Path,
) -> None:
    assert session_config(tmp_path).extension_mode is ExtensionMode.AUTO


def test_runtime_session_config_accepts_manual_extension_mode(tmp_path: Path) -> None:
    config = replace(
        session_config(tmp_path),
        extension_mode=ExtensionMode.MANUAL,
    )

    assert config.extension_mode is ExtensionMode.MANUAL


def test_runtime_session_config_rejects_untyped_extension_mode(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="extension_mode must be an ExtensionMode"):
        replace(session_config(tmp_path), extension_mode="manual")  # type: ignore[arg-type]


def _common_module_source(root: Path, *names: str) -> Path:
    common_modules = root / "CommonModules"
    common_modules.mkdir(parents=True)
    for name in names:
        (common_modules / f"{name}.xml").write_text(
            "<MetaDataObject><CommonModule><Properties>"
            f"<Name>{name}</Name>"
            "<Global>false</Global>"
            "<Server>true</Server>"
            "<ClientManagedApplication>false</ClientManagedApplication>"
            "<ClientOrdinaryApplication>false</ClientOrdinaryApplication>"
            "</Properties></CommonModule></MetaDataObject>",
            encoding="utf-8",
        )
    return root


def _worker_unit(name: str) -> WorkerModuleUnit:
    source = "Функция Версия() Экспорт\n    Возврат 1;\nКонецФункции\n"
    reference = SourceUnitRef(
        SourceUnitKind.MODULE,
        name,
        1,
        source_sha256(source),
    )
    return WorkerModuleUnit(
        name,
        "module",
        1,
        mapped_visible_source(source, reference),
    )


SESSION_WORKER_GENERATION = WorkerGenerationHandle(1, 1, 1, "a" * 64)


class _SessionModuleRuntimeApi:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[WorkerModuleUnit, ...], object, object]] = []
        self.breakpoint_calls: list[tuple[SourceUnitRef, str, int]] = []
        self.released: list[object] = []
        self.active_units: dict[str, WorkerModuleUnit] = {}

    def confirmed_worker_module_units(self, handle: WorkerGenerationHandle) -> tuple[WorkerModuleUnit, ...]:
        assert handle is SESSION_WORKER_GENERATION
        return tuple(self.active_units.values())

    def load_worker_modules(
        self,
        units: tuple[WorkerModuleUnit, ...],
        *,
        common_modules: object,
        breakpoint_policy: object = None,
        profiler: object = None,
    ) -> WorkerGenerationHandle:
        del breakpoint_policy
        self.calls.append((units, common_modules, profiler))
        self.active_units.update((unit.logical_name.casefold(), unit) for unit in units)
        return SESSION_WORKER_GENERATION

    def add_worker_breakpoint(
        self, source_unit: SourceUnitRef, canonical_module: str, line: int,
    ) -> str:
        self.breakpoint_calls.append((source_unit, canonical_module, line))
        return "breakpoint"

    def release_worker_generation(self, handle: object) -> None:
        self.released.append(handle)

    def owns_debug_ui_stream(self) -> bool:
        return False


class _IdleRdbg:
    def heartbeat(self) -> dict[str, object]:
        return {}


class _Closeable:
    def close(self) -> None:
        pass


def _module_session(config: RuntimeSessionConfig) -> tuple[RuntimeSession, _SessionModuleRuntimeApi]:
    api = _SessionModuleRuntimeApi()
    session = RuntimeSession(
        config,
        _Closeable(),  # type: ignore[arg-type]
        _Closeable(),  # type: ignore[arg-type]
        _IdleRdbg(),  # type: ignore[arg-type]
        api,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        heartbeat_interval_s=60.0,
    )
    return session, api


def test_runtime_session_forwards_the_cold_catalog_manager_to_the_runtime_api(
    tmp_path: Path,
) -> None:
    """Break caught: constructing a session must not eagerly read module XML."""
    source = _common_module_source(
        tmp_path / "source",
        "НовыйА",
        "НовыйБ",
    )
    config = replace(session_config(tmp_path), source_root=source)
    session, api = _module_session(config)
    try:
        catalog = session._common_module_catalog
        assert config.source_root == source.resolve()
        assert catalog is not None
        assert catalog.initialized is False

        units = (_worker_unit("НовыйА"), _worker_unit("НовыйБ"))
        assert session.load_worker_modules(units) == SESSION_WORKER_GENERATION

        assert catalog.initialized is False
        assert len(api.calls) == 1
        assert api.calls[0][0] == units
        assert api.calls[0][1] is catalog
    finally:
        session.close()


def test_load_worker_module_from_designer_path_reloads_saved_source(
    tmp_path: Path,
) -> None:
    source_root = _common_module_source(tmp_path / "source", "МодульА")
    module = source_root / "CommonModules" / "МодульА" / "Ext" / "Module.bsl"
    module.parent.mkdir(parents=True)
    module.write_text('Функция Версия() Экспорт\n    Возврат "первая";\nКонецФункции\n', encoding="utf-8")
    session, api = _module_session(
        replace(session_config(tmp_path), source_root=source_root)
    )
    try:
        assert session.load_worker_module(str(module)) == SESSION_WORKER_GENERATION
        first = api.calls[0][0][0]
        assert first.logical_name == "МодульА"
        assert first.mapped_source.text == module.read_bytes().decode("utf-8-sig")

        module.write_text('Функция Версия() Экспорт\n    Возврат "вторая";\nКонецФункции\n', encoding="utf-8")
        assert session.load_worker_module(module.relative_to(source_root)) == SESSION_WORKER_GENERATION
        second = api.calls[1][0][0]
        assert second.revision > first.revision
        assert second.mapped_source.text == module.read_bytes().decode("utf-8-sig")
        assert second.mapped_source.artifact.source_sha256 != first.mapped_source.artifact.source_sha256
    finally:
        session.close()


def test_worker_breakpoint_uses_only_absolute_or_relative_source_path_and_line(
    tmp_path: Path,
) -> None:
    source_root = _common_module_source(tmp_path / "source", "МодульА")
    module = source_root / "CommonModules" / "МодульА" / "Ext" / "Module.bsl"
    module.parent.mkdir(parents=True)
    source = "Функция Версия() Экспорт\n    Возврат 1;\nКонецФункции\n"
    module.write_bytes(source.encode("utf-8"))
    session, api = _module_session(
        replace(session_config(tmp_path), source_root=source_root)
    )
    try:
        session.load_worker_module(str(module))
        relative = str(module.relative_to(source_root))

        assert session.add_worker_breakpoint(str(module), 2) == "breakpoint"
        assert session.add_worker_breakpoint(relative, 2) == "breakpoint"

        expected = SourceUnitRef(
            SourceUnitKind.MODULE, "МодульА", 1, source_sha256(source)
        )
        assert api.breakpoint_calls == [
            (expected, "модульа", 2),
            (expected, "модульа", 2),
        ]
    finally:
        session.close()


def test_worker_breakpoint_accepts_edt_path_relative_to_configured_project_root(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    module = project / "src" / "CommonModules" / "МодульА" / "Module.bsl"
    module.parent.mkdir(parents=True)
    (module.parent / "МодульА.mdo").write_text(
        "<CommonModule><name>МодульА</name><server>true</server></CommonModule>",
        encoding="utf-8",
    )
    module.write_bytes("Функция Версия()\n    Возврат 1;\nКонецФункции\n".encode("utf-8"))
    session, api = _module_session(
        replace(session_config(tmp_path), source_root=project)
    )
    try:
        session.load_worker_module(str(module))

        assert session.add_worker_breakpoint(
            "src/CommonModules/МодульА/Module.bsl", 2
        ) == "breakpoint"
        assert api.breakpoint_calls[0][1:] == ("модульа", 2)
    finally:
        session.close()


def test_worker_breakpoint_rejects_unloaded_and_changed_source(
    tmp_path: Path,
) -> None:
    source_root = _common_module_source(tmp_path / "source", "МодульА")
    module = source_root / "CommonModules" / "МодульА" / "Ext" / "Module.bsl"
    module.parent.mkdir(parents=True)
    module.write_text("Функция Версия()\n    Возврат 1;\nКонецФункции\n", encoding="utf-8")
    session, api = _module_session(
        replace(session_config(tmp_path), source_root=source_root)
    )
    try:
        with pytest.raises(ProtocolError, match="loaded"):
            session.add_worker_breakpoint(str(module), 2)

        session.load_worker_module(str(module))
        module.write_text("Функция Версия()\n    Возврат 2;\nКонецФункции\n", encoding="utf-8")
        with pytest.raises(ProtocolError, match="reload"):
            session.add_worker_breakpoint(str(module), 2)
        assert api.breakpoint_calls == []
    finally:
        session.close()


def test_worker_breakpoint_rejects_invalid_line_before_dispatch(
    tmp_path: Path,
) -> None:
    source_root = _common_module_source(tmp_path / "source", "МодульА")
    module = source_root / "CommonModules" / "МодульА" / "Ext" / "Module.bsl"
    module.parent.mkdir(parents=True)
    module.write_text("Функция Версия()\n    Возврат 1;\nКонецФункции\n", encoding="utf-8")
    session, api = _module_session(
        replace(session_config(tmp_path), source_root=source_root)
    )
    try:
        session.load_worker_module(str(module))
        for invalid in (0, True, "2"):
            with pytest.raises(ValueError, match="line"):
                session.add_worker_breakpoint(str(module), invalid)
        assert api.breakpoint_calls == []
    finally:
        session.close()


def test_worker_breakpoint_path_identity_is_cleared_by_manual_module_replacement(
    tmp_path: Path,
) -> None:
    source_root = _common_module_source(tmp_path / "source", "МодульА")
    module = source_root / "CommonModules" / "МодульА" / "Ext" / "Module.bsl"
    module.parent.mkdir(parents=True)
    module.write_text("Функция Версия()\n    Возврат 1;\nКонецФункции\n", encoding="utf-8")
    session, api = _module_session(
        replace(session_config(tmp_path), source_root=source_root)
    )
    try:
        session.load_worker_module(str(module))
        session.load_worker_modules((_worker_unit("МодульА"),))

        with pytest.raises(ProtocolError, match="loaded"):
            session.add_worker_breakpoint(str(module), 2)
        assert api.breakpoint_calls == []
    finally:
        session.close()


def test_worker_breakpoint_uses_reloaded_file_revision_and_forgets_released_generation(
    tmp_path: Path,
) -> None:
    source_root = _common_module_source(tmp_path / "source", "МодульА")
    module = source_root / "CommonModules" / "МодульА" / "Ext" / "Module.bsl"
    module.parent.mkdir(parents=True)
    module.write_text("Функция Версия()\n    Возврат 1;\nКонецФункции\n", encoding="utf-8")
    session, api = _module_session(
        replace(session_config(tmp_path), source_root=source_root)
    )
    try:
        session.load_worker_module(str(module))
        second = "Функция Версия()\n    Возврат 2;\nКонецФункции\n"
        module.write_bytes(second.encode("utf-8"))
        handle = session.load_worker_module(str(module))

        session.add_worker_breakpoint(str(module), 2)
        assert api.breakpoint_calls[0][0].revision == 2
        assert api.breakpoint_calls[0][0].source_sha256 == source_sha256(second)

        session.release_worker_generation(handle)
        with pytest.raises(ProtocolError, match="loaded"):
            session.add_worker_breakpoint(str(module), 2)
        assert len(api.breakpoint_calls) == 1
    finally:
        session.close()


@pytest.mark.parametrize("root_is_project", [False, True])
def test_load_worker_module_from_edt_path(
    tmp_path: Path, root_is_project: bool,
) -> None:
    project = tmp_path / "project"
    source_root = project / "src"
    module = source_root / "CommonModules" / "МодульА" / "Module.bsl"
    module.parent.mkdir(parents=True)
    (module.parent / "МодульА.mdo").write_text(
        "<CommonModule><name>МодульА</name><server>true</server></CommonModule>",
        encoding="utf-8",
    )
    module.write_text('Функция Версия() Экспорт\n    Возврат 1;\nКонецФункции\n', encoding="utf-8")
    configured_root = project if root_is_project else source_root
    session, api = _module_session(
        replace(session_config(tmp_path), source_root=configured_root)
    )
    try:
        session.load_worker_module(module)
        assert api.calls[0][0][0].logical_name == "МодульА"
        assert api.calls[0][0][0].mapped_source.text == module.read_bytes().decode("utf-8-sig")
    finally:
        session.close()


def test_load_worker_module_rejects_source_outside_bound_root(tmp_path: Path) -> None:
    source_root = _common_module_source(tmp_path / "source", "МодульА")
    outside = tmp_path / "other" / "CommonModules" / "МодульА" / "Ext" / "Module.bsl"
    outside.parent.mkdir(parents=True)
    outside.write_text("", encoding="utf-8")
    session, api = _module_session(
        replace(session_config(tmp_path), source_root=source_root)
    )
    try:
        with pytest.raises(ProtocolError, match="Worker module source path"):
            session.load_worker_module(outside)
        assert api.calls == []
    finally:
        session.close()


def test_load_worker_module_preserves_crlf_source_offsets(tmp_path: Path) -> None:
    source_root = _common_module_source(tmp_path / "source", "МодульА")
    module = source_root / "CommonModules" / "МодульА" / "Ext" / "Module.bsl"
    module.parent.mkdir(parents=True)
    source = 'Функция Версия() Экспорт\r\n    Возврат "😀";\r\nКонецФункции\r\n'
    module.write_bytes(source.encode("utf-8-sig"))
    session, api = _module_session(
        replace(session_config(tmp_path), source_root=source_root)
    )
    try:
        session.load_worker_module(module)
        assert api.calls[0][0][0].mapped_source.text == source
    finally:
        session.close()


def test_load_worker_module_rejects_noncanonical_file(tmp_path: Path) -> None:
    source_root = _common_module_source(tmp_path / "source", "МодульА")
    wrong = source_root / "CommonModules" / "МодульА" / "Wrong.bsl"
    wrong.parent.mkdir(parents=True)
    wrong.write_text("", encoding="utf-8")
    session, api = _module_session(
        replace(session_config(tmp_path), source_root=source_root)
    )
    try:
        with pytest.raises(ProtocolError, match="Worker module source path"):
            session.load_worker_module(wrong)
        assert api.calls == []
    finally:
        session.close()


def test_load_worker_module_requires_source_root(tmp_path: Path) -> None:
    session, api = _module_session(session_config(tmp_path))
    try:
        with pytest.raises(ProtocolError, match="source root is not configured"):
            session.load_worker_module("CommonModules/МодульА/Ext/Module.bsl")
        assert api.calls == []
    finally:
        session.close()


def test_runtime_session_config_rejects_missing_source_root_privately(
    tmp_path: Path,
) -> None:
    """Break caught: strict resolution must not disclose a missing source path."""
    config = session_config(tmp_path)
    missing = tmp_path / "private-missing-source"

    with pytest.raises(
        ProtocolError,
        match="^common-module source root is unsafe$",
    ) as raised:
        replace(config, source_root=missing)

    assert str(missing) not in str(raised.value)


def test_runtime_session_config_rejects_source_root_symlink(
    tmp_path: Path,
) -> None:
    """Break caught: config normalization cannot erase a supplied symlink."""
    config = session_config(tmp_path)
    source = _common_module_source(tmp_path / "source", "МодульА")
    linked = tmp_path / "linked-source"
    try:
        linked.symlink_to(source, target_is_directory=True)
    except NotImplementedError:
        pytest.skip("directory symlinks are unavailable on this platform")
    except OSError as error:
        if getattr(error, "winerror", None) != 1314:
            raise
        pytest.skip("directory symlinks require Windows developer privileges")

    with pytest.raises(
        ProtocolError,
        match="^common-module source root is unsafe$",
    ) as raised:
        replace(config, source_root=linked)

    assert str(linked) not in str(raised.value)


def test_module_load_without_bound_source_root_fails_before_runtime_api(
    tmp_path: Path,
) -> None:
    """Break caught: callers cannot inject a catalog into an unbound session."""
    session, api = _module_session(session_config(tmp_path))
    try:
        with pytest.raises(ProtocolError, match="source root is not configured"):
            session.load_worker_modules((_worker_unit("МодульА"),))
        assert api.calls == []
    finally:
        session.close()


@pytest.mark.parametrize("units", ([], (), (object(),)))
def test_module_load_rejects_malformed_batch_before_catalog_initialization(
    tmp_path: Path,
    units: object,
) -> None:
    """Break caught: invalid batches must not trigger metadata reads or runtime calls."""
    source = _common_module_source(tmp_path / "source", "МодульА")
    session, api = _module_session(
        replace(session_config(tmp_path), source_root=source)
    )
    try:
        catalog = session._common_module_catalog
        assert catalog is not None
        with pytest.raises(ProtocolError, match="non-empty tuple|binding does not match"):
            session.load_worker_modules(units)  # type: ignore[arg-type]
        assert catalog.initialized is False
        assert api.calls == []
    finally:
        session.close()


def test_runtime_extension_tools_reuse_agent_across_cli_fingerprint_dumps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    events: list[str] = []

    class Editor:
        def load_cfe(self, _source: Path) -> None:
            events.append("agent-load")

        def apply(self) -> None:
            events.append("agent-apply")

        def disable_safe_mode(self) -> None:
            events.append("agent-safe-mode")

        @contextmanager
        def designer_access(self):
            events.append("agent-disconnect")
            try:
                yield
            finally:
                events.append("agent-connect")

    @contextmanager
    def edit_extension(_config: RuntimeConfig, _logs: Path):
        events.append("agent-open")
        try:
            yield Editor()
        finally:
            events.append("agent-close")

    def cli_dump_files(_config: RuntimeConfig, _dest: Path, _log: Path) -> None:
        events.append("cli-dump-files")

    def cli_dump_cfe(_config: RuntimeConfig, _dest: Path, _log: Path) -> None:
        events.append("cli-dump-cfe")

    monkeypatch.setattr(session_module, "edit_extension", edit_extension, raising=False)
    monkeypatch.setattr(session_module, "dump_target_extension_files", cli_dump_files)
    monkeypatch.setattr(session_module, "dump_target_extension_cfe", cli_dump_cfe)
    tools = session_module._OnecInteractiveRuntimeTools(session_config(tmp_path).runtime)

    with tools.mutation_session(tmp_path / "agent"):
        tools.load_cfe(tmp_path / "runtime.cfe", tmp_path / "load.log")
        tools.apply(tmp_path / "apply.log")
        tools.dump_files(tmp_path / "dump", tmp_path / "dump.log")
        tools.dump_cfe(tmp_path / "backup.cfe", tmp_path / "backup.log")
        tools.disable_safe_mode(tmp_path / "safe-mode")

    assert events == [
        "agent-open", "agent-load", "agent-apply",
        "agent-disconnect", "cli-dump-files", "agent-connect",
        "agent-disconnect", "cli-dump-cfe", "agent-connect",
        "agent-safe-mode", "agent-close",
    ]


def patch_successful_runtime_attempt(
    monkeypatch: pytest.MonkeyPatch,
    lifecycle: FakeLifecycle,
) -> AttemptProbe:
    probe = AttemptProbe()
    lifecycle.attempt_probe = probe

    class FakeProcesses:
        def __init__(self, _config: RuntimeConfig) -> None:
            probe.attempt_count += 1
            self.attempt = probe.attempt_count
            self.debug_server: object | None = None
            self.debuggee: object | None = None

        def start_debug_server(self) -> int:
            error = probe.process_errors.get(self.attempt)
            if error is not None:
                raise error
            self.debug_server = SimpleNamespace(pid=100 + self.attempt)
            return 1550 + self.attempt

        def start_debuggee(self, _port: int, **_kwargs: object) -> object:
            self.debuggee = SimpleNamespace(pid=200 + self.attempt)
            return self.debuggee

        def ensure_running(self) -> None:
            error = probe.process_health_errors.get(self.attempt)
            if error is not None:
                raise error

        def close(self) -> None:
            probe.closed_attempts.append(self.attempt)
            lifecycle.events.append("attempt-close")
            error = probe.cleanup_errors.get(self.attempt)
            if error is not None:
                raise error

    class FakeTransport:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.attempt = probe.attempt_count
            self.closed = False

        def close(self) -> None:
            self.closed = True
            probe.closed_transports.append(self.attempt)

    class FakeRdbgSession:
        def __init__(
            self,
            _transport: object,
            expected_location: ModuleLocation,
            **_kwargs: object,
        ) -> None:
            probe.rdbg_options.append(dict(_kwargs))
            self.attempt = probe.attempt_count
            self.expected_location = expected_location
            self.target: DebugTarget = DebugTarget(
                TARGET_ID, "ManagedClient", "stopped"
            )
            self._recorded_targets: set[str] = set()

        def initialize(self) -> None:
            pass

        def bind_server_session(self, *, launch_token: str) -> None:
            assert launch_token.startswith("onec-runtime:")
            probe.bound_server_sessions.append(self.attempt)

        def detach(self) -> None:
            probe.detach_calls.append(self.attempt)
            lifecycle.events.append("debugger-detach")
            remaining = probe.detach_failures.get(self.attempt, 0)
            if remaining:
                probe.detach_failures[self.attempt] = remaining - 1
                raise ProtocolError("private detach failure")

        def terminate_bound_server_session(self) -> None:
            probe.termination_calls.append(self.attempt)
            remaining = probe.termination_failures.get(self.attempt, 0)
            if remaining:
                probe.termination_failures[self.attempt] = remaining - 1
                raise ProtocolError("private termination failure")

        def set_service_breakpoint(self) -> None:
            pass

        def verify_registration(self) -> None:
            error = probe.registration_errors.get(self.attempt)
            if error is not None:
                raise error

        def evaluate(self, expression: str) -> EvaluationResult:
            if expression.startswith("РасширенияКонфигурации.Получить("):
                probe.safe_mode_checks.append(self.attempt)
                return EvaluationResult(
                    UUID(int=1),
                    "Булево",
                    probe.safe_mode_presentations.get(self.attempt, "Ложь"),
                    False,
                )
            target_type = self.target.target_type
            if target_type not in self._recorded_targets:
                lifecycle.events.append(
                    "managed-handshake"
                    if target_type == "ManagedClient"
                    else "server-handshake"
                )
                self._recorded_targets.add(target_type)
            configured = probe.handshake_errors.get((self.attempt, target_type))
            if configured is not None:
                raise configured
            if (
                self.attempt == 1
                and target_type == "ManagedClient"
                and lifecycle.first_handshake_error is not None
            ):
                raise lifecycle.first_handshake_error
            value = probe.handshake_values.get(
                (self.attempt, target_type, expression), HANDSHAKE_VALUES[expression]
            )
            if (
                target_type == "ServerEmulation"
                and self.attempt in probe.server_mismatch_attempts
                and expression == "ВерсияПротоколаRuntime"
            ):
                value = "private-mismatched-protocol"
            return EvaluationResult(UUID(int=1), "Строка", value, False)

        def heartbeat(self) -> dict[str, object]:
            return {}

    def lifecycle_factory(
        *_args: object, profiler: PhaseRecorder, **_kwargs: object
    ) -> FakeLifecycle:
        lifecycle.profiler = profiler
        return lifecycle

    def managed_stop(
        rdbg: FakeRdbgSession,
        location: ModuleLocation,
        *,
        timeout_s: float,
        on_poll: Callable[[], None] | None = None,
    ) -> StopEvent:
        assert timeout_s > 0
        probe.managed_startup_timeouts.append(timeout_s)
        if on_poll is not None:
            on_poll()
        error = probe.managed_errors.get(rdbg.attempt)
        if error is not None:
            raise error
        rdbg.target = DebugTarget(TARGET_ID, "ManagedClient", "stopped")
        return StopEvent(TARGET_ID, location, "breakpoint", True)

    def server_stops(
        rdbg: FakeRdbgSession,
        entry: ModuleLocation,
        service: ModuleLocation,
        *,
        timeout_s: float,
        on_entry: Callable[[FakeRdbgSession], None],
        server_target_type: str = "ServerEmulation",
    ) -> tuple[StopEvent, StopEvent]:
        assert timeout_s > 0
        probe.server_startup_timeouts.append(timeout_s)
        error = probe.server_entry_errors.get(rdbg.attempt)
        if error is not None:
            raise error
        rdbg.target = DebugTarget(probe.server_target_id, server_target_type, "stopped")
        on_entry(rdbg)
        service_target_id = probe.service_target_id or probe.server_target_id
        rdbg.target = DebugTarget(service_target_id, server_target_type, "stopped")
        error = probe.service_errors.get(rdbg.attempt)
        if error is not None:
            raise error
        return (
            StopEvent(probe.server_target_id, entry, "breakpoint", True),
            StopEvent(service_target_id, service, "breakpoint", True),
        )

    monkeypatch.setattr(
        session_module, "packaged_extension_bundle", lambda _root: BUNDLE
    )
    monkeypatch.setattr(session_module, "ExtensionLifecycle", lifecycle_factory)
    monkeypatch.setattr(session_module, "FileModeProcesses", FakeProcesses)
    monkeypatch.setattr(session_module, "RdbgTransport", FakeTransport)
    monkeypatch.setattr(session_module, "RdbgSession", FakeRdbgSession)
    monkeypatch.setattr(session_module, "wait_for_managed_startup_stop", managed_stop)
    monkeypatch.setattr(
        session_module, "wait_for_server_entry_then_service", server_stops
    )
    monkeypatch.setattr(
        session_module,
        "enable_server_kernel_loop",
        lambda _rdbg: SimpleNamespace(as_dict=lambda: {"guard": "sanitized"}),
    )
    monkeypatch.setattr(
        session_module,
        "PrototypeRuntimeController",
        lambda *_args, **_kwargs: object(),
    )
    def runtime_api(*_args: object, **kwargs: object) -> object:
        probe.runtime_api_kwargs.append(dict(kwargs))
        return SimpleNamespace()

    monkeypatch.setattr(session_module, "PrototypeRuntimeApi", runtime_api)
    monkeypatch.setattr(
        session_module, "NotebookWorkerArtifactBuilder", lambda _runtime: object()
    )
    def module_builder(packer: object, **kwargs: object) -> object:
        probe.module_builder_args.append((packer, dict(kwargs)))
        return SimpleNamespace(kind="worker-module-builder")

    monkeypatch.setattr(
        session_module,
        "WorkerModuleArtifactBuilder",
        module_builder,
        raising=False,
    )
    return probe


def test_runtime_bootstrap_locations_are_exactly_manifest_derived() -> None:
    assert runtime_bootstrap_locations(MANIFEST) == (
        MANIFEST.breakpoints.managed,
        MANIFEST.breakpoints.server_entry,
        MANIFEST.breakpoints.server_service,
    )


def test_client_server_session_bootstrap_uses_server_alias_and_binds_its_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    lifecycle = FakeLifecycle(decisions=[fast_decision()], server_target_type="Server")
    probe = patch_successful_runtime_attempt(monkeypatch, lifecycle)
    config = session_config(tmp_path)
    config = replace(config, runtime=replace(config.runtime, connection_string='Srvr="localhost";Ref="runtime_test";'))
    session = RuntimeSession.start(config)
    try:
        assert probe.rdbg_options[0]["alias"] == "runtime_test"
        assert probe.rdbg_options[0]["server_target_type"] == "Server"
        assert probe.rdbg_options[0]["break_on_next"] is False
        assert probe.bound_server_sessions == [1]
        evidence = json.loads((session.artifacts.run_dir / "bootstrap.json").read_text(encoding="utf-8"))
        assert [record["target_type"] for record in evidence["handshakes"]] == ["ManagedClient", "Server"]
        assert evidence["same_session"] is True
    finally:
        session.close()
    assert lifecycle.events[-2:] == ["attempt-close", "debugger-detach"]


def test_runtime_start_waits_up_to_150_seconds_for_each_bootstrap_stop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    lifecycle = FakeLifecycle(decisions=[fast_decision()])
    probe = patch_successful_runtime_attempt(monkeypatch, lifecycle)

    session = RuntimeSession.start(session_config(tmp_path))
    try:
        assert probe.managed_startup_timeouts == [150.0]
        assert probe.server_startup_timeouts == [150.0]
    finally:
        session.close()


def test_fast_session_starts_without_designer_and_commits_after_two_handshakes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lifecycle = FakeLifecycle(decisions=[fast_decision()])
    started = patch_successful_runtime_attempt(monkeypatch, lifecycle)

    session = RuntimeSession.start(session_config(tmp_path))

    assert lifecycle.events == [
        "prepare",
        "managed-handshake",
        "server-handshake",
        "commit",
    ]
    assert lifecycle.tool_calls == []
    assert started.attempt_count == 1
    assert len(started.module_builder_args) == 1
    packer, module_builder_kwargs = started.module_builder_args[0]
    assert packer is started.runtime_api_kwargs[0]["notebook_worker_builder"]
    assert module_builder_kwargs["packer_version"] == "worker-epf-v1"
    assert module_builder_kwargs["target_profile"] == "runtime-session-server-v1"
    assert started.runtime_api_kwargs[0]["worker_module_builder"].kind == (
        "worker-module-builder"
    )
    bootstrap_text = (session.artifacts.run_dir / "bootstrap.json").read_text(
        encoding="utf-8"
    )
    evidence = json.loads(bootstrap_text)
    assert evidence["lifecycle"] == {"mode": "fast", "target_state": "current"}
    assert evidence["bundle"] == {
        "source": "packaged-release",
        "artifact_sha256": MANIFEST.fingerprints.artifact_sha256,
        "cfe_sha256": MANIFEST.cfe_sha256,
        "identity_sha256": MANIFEST.fingerprints.identity_sha256,
    }
    assert [item["target_type"] for item in evidence["handshakes"]] == [
        "ManagedClient",
        "ServerEmulation",
    ]
    assert "extension_name" not in evidence
    assert "validation_returncode" not in evidence
    assert "manual_extension" not in evidence
    assert "private-operator" not in bootstrap_text
    phases = json.loads(
        (session.artifacts.run_dir / "bootstrap-phases.json").read_text(
            encoding="utf-8"
        )
    )
    assert [event["phase"] for event in phases] == [
        "extension.bundle",
        "extension.handshake",
    ]
    session.close()


def test_start_reports_visible_stages_in_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lifecycle = FakeLifecycle(decisions=[fast_decision()])
    patch_successful_runtime_attempt(monkeypatch, lifecycle)
    stages: list[str] = []

    session = RuntimeSession.start(session_config(tmp_path), progress=stages.append)
    try:
        assert stages == [
            "Загрузка расширения 1С",
            "Проверка состояния расширения 1С",
            "Запуск сервера отладки 1С",
            "Запуск 1С:Предприятия",
            "Ожидание клиентского сеанса 1С",
            "Проверка расширения в клиентском сеансе",
            "Подключение серверного сеанса 1С",
            "Проверка безопасного режима расширения 1С",
            "Сеанс 1С готов",
        ]
    finally:
        session.close()


def test_start_closes_session_when_final_progress_callback_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lifecycle = FakeLifecycle(decisions=[fast_decision()])
    probe = patch_successful_runtime_attempt(monkeypatch, lifecycle)

    def progress(stage: str) -> None:
        if stage == "Сеанс 1С готов":
            raise RuntimeError("progress stream closed")

    with pytest.raises(RuntimeError, match="progress stream closed"):
        RuntimeSession.start(session_config(tmp_path), progress=progress)

    assert probe.closed_attempts == [1]
    assert probe.closed_transports == [1]


def test_enterprise_authentication_failure_explains_startup_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lifecycle = FakeLifecycle(decisions=[fast_decision()])
    started = patch_successful_runtime_attempt(monkeypatch, lifecycle)
    started.managed_errors[1] = CommandTimeout("private timeout detail")
    config = session_config(tmp_path)
    log_path = config.runtime.logs_dir / "1c-messages.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("The infobase user is not authenticated\nprivate login", encoding="utf-8")

    with pytest.raises(errors_module.ProcessStartError) as raised:
        RuntimeSession.start(config)

    message = str(raised.value)
    assert "1С:Предприятие" in message
    assert "проверьте username и password" in message
    assert str(log_path) in message
    assert "private" not in message


def test_exited_client_reports_licensing_failure_without_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lifecycle = FakeLifecycle(decisions=[fast_decision()])
    started = patch_successful_runtime_attempt(monkeypatch, lifecycle)
    started.process_health_errors[1] = TargetLost("owned client exited")
    config = session_config(tmp_path)
    log_path = config.runtime.logs_dir / "1c-messages.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "Ошибка программного лицензирования. Файл программной лицензии "
        "не предусматривает возможность запуска сервера 1С:Предприятия: "
        "file://private-license-path.lic",
        encoding="utf-8",
    )

    with pytest.raises(ProcessStartError) as raised:
        RuntimeSession.start(config)

    message = str(raised.value)
    assert "лицензи" in message
    assert "сервера 1С:Предприятия" in message
    assert "private-license-path" not in message
    assert started.attempt_count == 1


def test_manual_session_starts_once_without_auto_prepare_or_marker_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lifecycle = FakeLifecycle(decisions=[manual_decision()])
    started = patch_successful_runtime_attempt(monkeypatch, lifecycle)
    config = replace(session_config(tmp_path), extension_mode=ExtensionMode.MANUAL)

    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        session = RuntimeSession.start(config)
    try:
        assert list(recorded) == []
        assert lifecycle.events == [
            "prepare-manual", "managed-handshake", "server-handshake", "accept-manual"
        ]
        assert lifecycle.tool_calls == []
        assert started.attempt_count == 1
        evidence = json.loads(
            (session.artifacts.run_dir / "bootstrap.json").read_text(encoding="utf-8")
        )
        assert evidence["lifecycle"] == {
            "mode": "manual", "target_state": "user-managed"
        }
        assert evidence["bundle"]["source"] == "packaged-release"
        assert evidence["manual_extension"] == {
            "packaged_artifact_version": MANIFEST.artifact_version,
            "observed_artifact_version": MANIFEST.artifact_version,
            "version_matches": True,
            "protocol_version": MANIFEST.protocol_version,
        }
        phases = json.loads(
            (session.artifacts.run_dir / "bootstrap-phases.json").read_text(
                encoding="utf-8"
            )
        )
        assert [event["phase"] for event in phases] == [
            "extension.bundle", "extension.handshake"
        ]
    finally:
        session.close()


def test_manual_artifact_version_mismatch_warns_once_and_still_starts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lifecycle = FakeLifecycle(decisions=[manual_decision()])
    started = patch_successful_runtime_attempt(monkeypatch, lifecycle)
    for target_type in ("ManagedClient", "ServerEmulation"):
        started.handshake_values[(1, target_type, "ВерсияАртефактаRuntime")] = (
            "0.1.0-user.1"
        )
    config = replace(session_config(tmp_path), extension_mode=ExtensionMode.MANUAL)

    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        session = RuntimeSession.start(config)
    try:
        assert len(recorded) == 1
        assert recorded[0].category is errors_module.ManualExtensionVersionWarning
        assert "0.1.0-user.1" in str(recorded[0].message)
        assert MANIFEST.artifact_version in str(recorded[0].message)
        evidence = json.loads(
            (session.artifacts.run_dir / "bootstrap.json").read_text(encoding="utf-8")
        )
        assert evidence["manual_extension"]["observed_artifact_version"] == (
            "0.1.0-user.1"
        )
        assert evidence["manual_extension"]["version_matches"] is False
        assert [item["artifact_version"] for item in evidence["handshakes"]] == [
            "0.1.0-user.1", "0.1.0-user.1"
        ]
        assert started.attempt_count == 1
    finally:
        session.close()


@pytest.mark.parametrize("reason", ("foreign product", "incompatible protocol"))
def test_manual_handshake_rejection_is_final_without_auto_repair(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reason: str
) -> None:
    lifecycle = FakeLifecycle(
        decisions=[manual_decision()],
        manual_handshake_error=ExtensionLifecycleError(reason),
    )
    started = patch_successful_runtime_attempt(monkeypatch, lifecycle)
    for target_type in ("ManagedClient", "ServerEmulation"):
        started.handshake_values[(1, target_type, "ВерсияАртефактаRuntime")] = (
            "0.1.0-user.1"
        )
    config = replace(session_config(tmp_path), extension_mode=ExtensionMode.MANUAL)

    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        with pytest.raises(ExtensionLifecycleError, match=reason):
            RuntimeSession.start(config)

    assert list(recorded) == []
    assert started.attempt_count == 1
    assert started.closed_attempts == [1]
    assert started.closed_transports == [1]
    assert lifecycle.events == [
        "prepare-manual", "managed-handshake", "server-handshake",
        "accept-manual", "attempt-close",
    ]
    assert lifecycle.tool_calls == []


@pytest.mark.parametrize("mode", (ExtensionMode.AUTO, ExtensionMode.MANUAL))
@pytest.mark.parametrize(
    "identity_change", ("foreign-entry", "missing-entry-session", "new-service-target")
)
def test_session_binds_server_entry_identity_through_service_admission(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: ExtensionMode,
    identity_change: str,
) -> None:
    manual = mode is ExtensionMode.MANUAL
    lifecycle = FakeLifecycle(
        decisions=[manual_decision() if manual else fast_decision()]
    )
    started = patch_successful_runtime_attempt(monkeypatch, lifecycle)
    started.service_target_id = TARGET_ID
    if identity_change == "foreign-entry":
        started.server_target_id = replace(TARGET_ID, seance_id=UUID(int=3))
    elif identity_change == "missing-entry-session":
        started.server_target_id = replace(TARGET_ID, seance_id=None)
    else:
        started.service_target_id = replace(TARGET_ID, id=UUID(int=4))
    config = replace(session_config(tmp_path), extension_mode=mode)

    with pytest.raises(
        ProtocolError, match="different sessions|different server targets"
    ):
        session = RuntimeSession.start(config)
        session.close()

    assert started.attempt_count == 1
    assert started.closed_attempts == [1]
    assert started.closed_transports == [1]
    assert lifecycle.events == [
        "prepare-manual" if manual else "prepare",
        "managed-handshake",
        "server-handshake",
        "attempt-close",
    ]
    assert lifecycle.tool_calls == []
    run_dir = next((tmp_path / "evidence").iterdir())
    attempts = json.loads(
        (run_dir / "bootstrap-attempts.json").read_text(encoding="utf-8")
    )
    assert len(attempts) == 1
    assert attempts[0]["repairable"] is False
    assert not (run_dir / "bootstrap.json").exists()


def test_manual_session_rejects_different_client_server_session_without_repair(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lifecycle = FakeLifecycle(decisions=[manual_decision()])
    started = patch_successful_runtime_attempt(monkeypatch, lifecycle)
    started.server_target_id = replace(
        TARGET_ID,
        seance_id=UUID("33333333-3333-3333-3333-333333333333"),
    )
    config = replace(session_config(tmp_path), extension_mode=ExtensionMode.MANUAL)

    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        with pytest.raises(
            ProtocolError,
            match="^Runtime client/server targets belong to different sessions$",
        ) as raised:
            RuntimeSession.start(config)

    assert type(raised.value) is ProtocolError
    assert list(recorded) == []
    assert started.attempt_count == 1
    assert started.closed_attempts == [1]
    assert started.closed_transports == [1]
    assert lifecycle.events == [
        "prepare-manual",
        "managed-handshake",
        "server-handshake",
        "attempt-close",
    ]
    assert lifecycle.tool_calls == []
    run_dir = next((tmp_path / "evidence").iterdir())
    attempts = json.loads(
        (run_dir / "bootstrap-attempts.json").read_text(encoding="utf-8")
    )
    assert attempts == [
        {
            "admitted": False,
            "attempt": 1,
            "cleanup_succeeded": True,
            "decision_mode": "manual",
            "error_type": "ProtocolError",
            "repairable": False,
            "stage": "server-service",
        }
    ]
    assert not (run_dir / "bootstrap.json").exists()


@pytest.mark.parametrize("failure_type", (CommandTimeout, UnexpectedStop))
@pytest.mark.parametrize("stage", ("managed-bootstrap", "server-entry-wait"))
def test_manual_missing_entrypoint_cleans_up_without_repair_or_database_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    stage: str, failure_type: type[BaseException],
) -> None:
    lifecycle = FakeLifecycle(decisions=[manual_decision()])
    started = patch_successful_runtime_attempt(monkeypatch, lifecycle)
    failures = (
        started.managed_errors if stage == "managed-bootstrap"
        else started.server_entry_errors
    )
    failures[1] = failure_type("private startup diagnostic")
    config = replace(session_config(tmp_path), extension_mode=ExtensionMode.MANUAL)

    with pytest.raises(ProtocolError, match="install.*current release") as raised:
        RuntimeSession.start(config)

    assert type(raised.value) is errors_module.ManualExtensionUnavailable
    assert "private" not in str(raised.value)
    assert raised.value.__suppress_context__ is True
    assert started.attempt_count == 1
    assert started.closed_transports == [1]
    assert started.closed_attempts == [1]
    assert lifecycle.events == (
        ["prepare-manual", "attempt-close"] if stage == "managed-bootstrap"
        else ["prepare-manual", "managed-handshake", "attempt-close"]
    )
    assert lifecycle.tool_calls == []
    run_dir = next((tmp_path / "evidence").iterdir())
    attempts = json.loads(
        (run_dir / "bootstrap-attempts.json").read_text(encoding="utf-8")
    )
    assert len(attempts) == 1
    assert attempts[0]["stage"] == stage
    assert attempts[0]["cleanup_succeeded"] is True
    assert attempts[0]["decision_mode"] == "manual"
    assert not (run_dir / "bootstrap.json").exists()


@pytest.mark.parametrize("stage", ("managed-bootstrap", "server-entry-wait"))
@pytest.mark.parametrize("failure_type", (CommandTimeout, UnexpectedStop))
def test_manual_unavailable_preserves_only_sanitized_cleanup_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stage: str,
    failure_type: type[BaseException],
) -> None:
    lifecycle = FakeLifecycle(decisions=[manual_decision()])
    started = patch_successful_runtime_attempt(monkeypatch, lifecycle)
    primary = failure_type("private startup diagnostic")
    primary.add_note("Runtime startup cleanup failed: private injected note")
    primary.add_note("private unrelated note")
    failures = (
        started.managed_errors if stage == "managed-bootstrap"
        else started.server_entry_errors
    )
    failures[1] = primary
    started.cleanup_errors[1] = RuntimeError("private cleanup diagnostic")
    config = replace(session_config(tmp_path), extension_mode=ExtensionMode.MANUAL)

    with pytest.raises(errors_module.ManualExtensionUnavailable) as raised:
        RuntimeSession.start(config)

    assert getattr(raised.value, "__notes__", []) == [
        "Runtime startup cleanup failed: RuntimeError",
        "Runtime startup cleanup is incomplete; retry with exception.retry_cleanup()",
    ]
    assert callable(getattr(raised.value, "retry_cleanup", None))
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True
    assert "private" not in "".join(traceback.format_exception(raised.value))
    assert started.attempt_count == 1
    assert started.closed_transports == [1]
    assert started.closed_attempts == [1]
    assert lifecycle.events == (
        ["prepare-manual", "attempt-close"] if stage == "managed-bootstrap"
        else ["prepare-manual", "managed-handshake", "attempt-close"]
    )
    assert lifecycle.tool_calls == []
    run_dir = next((tmp_path / "evidence").iterdir())
    attempts = json.loads(
        (run_dir / "bootstrap-attempts.json").read_text(encoding="utf-8")
    )
    assert len(attempts) == 1
    assert attempts[0]["cleanup_succeeded"] is False
    assert not (run_dir / "bootstrap.json").exists()


@pytest.mark.parametrize("mode", (LifecycleMode.FAST, LifecycleMode.PROBED))
def test_existing_extension_checks_live_safe_mode_before_marking_it_verified(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: LifecycleMode,
) -> None:
    lifecycle = FakeLifecycle(decisions=[
        LifecycleDecision(
            mode, TargetExtensionState.CURRENT, BUNDLE,
            retry_allowed=True,
        )
    ])
    started = patch_successful_runtime_attempt(monkeypatch, lifecycle)

    session = RuntimeSession.start(session_config(tmp_path))
    try:
        assert started.safe_mode_checks == [1]
        assert lifecycle.events[-1] == "commit"
        evidence = json.loads(
            (session.artifacts.run_dir / "bootstrap.json").read_text(encoding="utf-8")
        )
        assert evidence["lifecycle"]["mode"] == mode.value
    finally:
        session.close()


@pytest.mark.parametrize("mode", (LifecycleMode.FAST, LifecycleMode.PROBED))
def test_existing_extension_with_safe_mode_enabled_repairs_through_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: LifecycleMode,
) -> None:
    lifecycle = FakeLifecycle(decisions=[
        LifecycleDecision(
            mode, TargetExtensionState.CURRENT, BUNDLE,
            retry_allowed=True,
        ),
        slow_decision(),
    ])
    started = patch_successful_runtime_attempt(monkeypatch, lifecycle)
    started.safe_mode_presentations[1] = "Истина"

    session = RuntimeSession.start(session_config(tmp_path))
    try:
        assert started.safe_mode_checks == [1]
        assert lifecycle.events.count("commit") == 1
        assert lifecycle.events.index("commit") > lifecycle.events.index("prepare-force-slow")
        evidence = json.loads(
            (session.artifacts.run_dir / "bootstrap.json").read_text(encoding="utf-8")
        )
        assert evidence["lifecycle"]["mode"] == "slow"
        assert evidence["attempt_failures"][0]["stage"] == "safe-mode-check"
        assert evidence["attempt_failures"][0]["repairable"] is True
    finally:
        session.close()


@pytest.mark.parametrize("initial_mode", (LifecycleMode.FAST, LifecycleMode.PROBED))
def test_existing_extension_handshake_failure_cleans_up_then_retries_slow_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, initial_mode: LifecycleMode
) -> None:
    lifecycle = FakeLifecycle(
        decisions=[
            LifecycleDecision(
                initial_mode,
                TargetExtensionState.CURRENT,
                BUNDLE,
                retry_allowed=True,
            ),
            slow_decision(),
        ],
        first_handshake_error=ExtensionHandshakeError("mismatch"),
    )
    started = patch_successful_runtime_attempt(monkeypatch, lifecycle)

    session = RuntimeSession.start(session_config(tmp_path))

    assert lifecycle.events == [
        "prepare",
        "managed-handshake",
        "attempt-close",
        "invalidate",
        "prepare-force-slow",
        "managed-handshake",
        "server-handshake",
        "commit",
    ]
    assert lifecycle.prepare_count == 2
    assert started.attempt_count == 2
    assert started.closed_transports == [1]
    assert started.closed_attempts == [1]
    evidence = json.loads(
        (session.artifacts.run_dir / "bootstrap.json").read_text(encoding="utf-8")
    )
    assert evidence["lifecycle"] == {"mode": "slow", "target_state": "current"}
    assert evidence["attempt_failures"] == [
        {
            "admitted": False,
            "attempt": 1,
            "cleanup_succeeded": True,
            "decision_mode": initial_mode.value,
            "error_type": "ExtensionHandshakeError",
            "repairable": True,
            "stage": "managed-handshake",
        }
    ]
    phases = json.loads(
        (session.artifacts.run_dir / "bootstrap-phases.json").read_text(
            encoding="utf-8"
        )
    )
    assert [event["phase"] for event in phases].count("extension.retry") == 1
    session.close()


def test_failed_slow_repair_keeps_retry_for_unfinished_configurator_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    primary = ExtensionHandshakeError("live protocol mismatch")
    repair_error = ExtensionLifecycleError("Configurator Agent resource cleanup failed")
    pending = ["owned-agent"]

    def retry_cleanup() -> None:
        pending.clear()

    repair_error.retry_cleanup = retry_cleanup
    repair_error.add_note("private agent diagnostic must not escape")
    lifecycle = FakeLifecycle(
        decisions=[fast_decision(), repair_error], first_handshake_error=primary,
    )
    started = patch_successful_runtime_attempt(monkeypatch, lifecycle)

    with pytest.raises(ExtensionHandshakeError) as raised:
        RuntimeSession.start(session_config(tmp_path))

    assert raised.value is primary
    retry = getattr(raised.value, "retry_cleanup", None)
    assert callable(retry)
    retry()
    retry()
    assert pending == []
    assert any("cleanup is incomplete" in note for note in raised.value.__notes__)
    assert not any("private agent diagnostic" in note for note in raised.value.__notes__)
    assert started.attempt_count == 1
    assert started.closed_attempts == [1]


def test_failed_slow_repair_preserves_handshake_error_and_never_admits_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    primary = ExtensionHandshakeError("live protocol mismatch")
    repair_error = ExtensionLifecycleError("private same-version diagnostic")
    lifecycle = FakeLifecycle(
        decisions=[fast_decision(), repair_error],
        first_handshake_error=primary,
    )
    started = patch_successful_runtime_attempt(monkeypatch, lifecycle)

    with pytest.raises(ExtensionHandshakeError) as raised:
        RuntimeSession.start(session_config(tmp_path))

    assert raised.value is primary
    assert raised.value.__notes__ == [
        "Runtime startup repair failed: ExtensionLifecycleError"
    ]
    assert "private same-version diagnostic" not in raised.value.__notes__[0]
    assert lifecycle.events == [
        "prepare",
        "managed-handshake",
        "attempt-close",
        "invalidate",
        "prepare-force-slow",
    ]
    assert lifecycle.prepare_count == 2
    assert started.attempt_count == 1
    assert started.closed_transports == [1]
    assert started.closed_attempts == [1]
    assert "commit" not in lifecycle.events
    run_dir = next((tmp_path / "evidence").iterdir())
    attempts = json.loads(
        (run_dir / "bootstrap-attempts.json").read_text(encoding="utf-8")
    )
    assert attempts == [
        {
            "admitted": False,
            "attempt": 1,
            "cleanup_succeeded": True,
            "decision_mode": "fast",
            "error_type": "ExtensionHandshakeError",
            "repairable": True,
            "stage": "managed-handshake",
        }
    ]
    phases = json.loads((run_dir / "bootstrap-phases.json").read_text(encoding="utf-8"))
    assert [(event["phase"], event["error_present"]) for event in phases] == [
        ("extension.bundle", False),
        ("extension.retry", True),
    ]
    assert not (run_dir / "bootstrap.json").exists()


def test_prepare_rejection_still_writes_failed_phase_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rejection = ExtensionIdentityConflict("private identity diagnostic")
    lifecycle = FakeLifecycle(decisions=[])
    patch_successful_runtime_attempt(monkeypatch, lifecycle)

    def reject_prepare(*, force_slow: bool = False) -> LifecycleDecision:
        assert force_slow is False
        assert lifecycle.profiler is not None

        def reject() -> LifecycleDecision:
            raise rejection

        return lifecycle.profiler.measure("extension.inspect", reject)

    monkeypatch.setattr(lifecycle, "prepare", reject_prepare)

    with pytest.raises(ExtensionIdentityConflict) as raised:
        RuntimeSession.start(session_config(tmp_path))

    assert raised.value is rejection
    run_dir = next((tmp_path / "evidence").iterdir())
    phases = json.loads((run_dir / "bootstrap-phases.json").read_text(encoding="utf-8"))
    assert [(event["phase"], event["error_present"]) for event in phases] == [
        ("extension.bundle", False),
        ("extension.inspect", True),
    ]


def test_phase_evidence_write_failure_preserves_primary_with_sanitized_note(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rejection = ExtensionIdentityConflict("primary identity rejection")
    lifecycle = FakeLifecycle(decisions=[])
    patch_successful_runtime_attempt(monkeypatch, lifecycle)

    def reject_prepare(*, force_slow: bool = False) -> LifecycleDecision:
        assert force_slow is False
        assert lifecycle.profiler is not None

        def reject() -> LifecycleDecision:
            raise rejection

        return lifecycle.profiler.measure("extension.inspect", reject)

    original_write_json = session_module.ArtifactWriter.write_json

    def fail_phase_write(self: object, name: str, value: object) -> None:
        if name == "bootstrap-phases.json":
            raise OSError("private phase write diagnostic")
        original_write_json(self, name, value)  # type: ignore[arg-type]

    monkeypatch.setattr(lifecycle, "prepare", reject_prepare)
    monkeypatch.setattr(session_module.ArtifactWriter, "write_json", fail_phase_write)

    with pytest.raises(ExtensionIdentityConflict) as raised:
        RuntimeSession.start(session_config(tmp_path))

    assert raised.value is rejection
    assert raised.value.__notes__ == [
        "Runtime startup phase evidence write failed: OSError"
    ]
    assert "private phase write diagnostic" not in raised.value.__notes__[0]


def test_phase_evidence_failure_closes_successful_attempt_with_sanitized_note(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lifecycle = FakeLifecycle(decisions=[fast_decision()])
    started = patch_successful_runtime_attempt(monkeypatch, lifecycle)
    started.cleanup_errors[1] = RuntimeError("private cleanup diagnostic")
    original_write_json = session_module.ArtifactWriter.write_json

    def fail_phase_write(self: object, name: str, value: object) -> None:
        if name == "bootstrap-phases.json":
            raise OSError("private phase write diagnostic")
        original_write_json(self, name, value)  # type: ignore[arg-type]

    monkeypatch.setattr(session_module.ArtifactWriter, "write_json", fail_phase_write)

    with pytest.raises(ProtocolError) as raised:
        RuntimeSession.start(session_config(tmp_path))

    assert str(raised.value) == "Runtime startup phase evidence could not be written"
    assert raised.value.__notes__ == [
        "Runtime startup cleanup failed: ProtocolError",
        "Runtime startup cleanup is incomplete; retry with exception.retry_cleanup()",
    ]
    assert callable(getattr(raised.value, "retry_cleanup", None))
    assert "private" not in str(raised.value)
    assert "private cleanup diagnostic" not in raised.value.__notes__[0]
    assert started.closed_attempts == [1]


def test_second_handshake_failure_is_final_without_a_third_attempt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first = ExtensionHandshakeError("first mismatch")
    second = ExtensionHandshakeError("second mismatch")
    lifecycle = FakeLifecycle(decisions=[fast_decision(), slow_decision()])
    started = patch_successful_runtime_attempt(monkeypatch, lifecycle)
    started.handshake_errors = {
        (1, "ManagedClient"): first,
        (2, "ManagedClient"): second,
    }

    with pytest.raises(ExtensionHandshakeError) as raised:
        RuntimeSession.start(session_config(tmp_path))

    assert raised.value is second
    assert started.attempt_count == 2
    assert started.closed_transports == [1, 2]
    assert started.closed_attempts == [1, 2]
    assert lifecycle.prepare_count == 2
    assert "commit" not in lifecycle.events


def test_unrelated_process_start_failure_is_not_a_repair_candidate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    failure = ProcessStartError("unrelated startup failure")
    lifecycle = FakeLifecycle(decisions=[fast_decision()])
    started = patch_successful_runtime_attempt(monkeypatch, lifecycle)
    started.process_errors[1] = failure

    with pytest.raises(ProcessStartError) as raised:
        RuntimeSession.start(session_config(tmp_path))

    assert raised.value is failure
    assert lifecycle.events == ["prepare", "attempt-close"]
    assert lifecycle.prepare_count == 1
    assert started.attempt_count == 1


@pytest.mark.parametrize("mode", (ExtensionMode.AUTO, ExtensionMode.MANUAL))
def test_lost_debug_ui_after_client_launch_retries_without_extension_repair(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: ExtensionMode,
) -> None:
    lifecycle = FakeLifecycle(
        decisions=[manual_decision() if mode is ExtensionMode.MANUAL else fast_decision()]
    )
    started = patch_successful_runtime_attempt(monkeypatch, lifecycle)
    started.managed_errors[1] = RdbgDebugUiNotRegistered("owned UI lost")
    config = replace(session_config(tmp_path), extension_mode=mode)

    session = RuntimeSession.start(config)
    try:
        assert started.attempt_count == 2
        assert started.closed_attempts == [1]
        assert started.closed_transports == [1]
        assert lifecycle.prepare_count == 1
        assert "prepare-force-slow" not in lifecycle.events
        evidence = json.loads(
            (session.artifacts.run_dir / "bootstrap-attempts.json").read_text(
                encoding="utf-8"
            )
        )
        assert evidence[0]["error_type"] == "RdbgDebugUiNotRegistered"
        assert evidence[0]["cleanup_succeeded"] is True
    finally:
        session.close()


@pytest.mark.parametrize("cleanup_incomplete", (False, True))
def test_lost_debug_ui_launch_retry_is_bounded_and_requires_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cleanup_incomplete: bool,
) -> None:
    lifecycle = FakeLifecycle(decisions=[fast_decision()])
    started = patch_successful_runtime_attempt(monkeypatch, lifecycle)
    started.managed_errors[1] = RdbgDebugUiNotRegistered("first UI lost")
    started.managed_errors[2] = RdbgDebugUiNotRegistered("second UI lost")
    if cleanup_incomplete:
        started.cleanup_errors[1] = ProtocolError("owned client cleanup failed")

    with pytest.raises(RdbgDebugUiNotRegistered):
        RuntimeSession.start(session_config(tmp_path))

    assert started.attempt_count == (1 if cleanup_incomplete else 2)
    assert lifecycle.prepare_count == 1


def test_failed_registration_detach_is_retried_by_startup_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    lifecycle = FakeLifecycle(decisions=[fast_decision()])
    started = patch_successful_runtime_attempt(monkeypatch, lifecycle)
    started.registration_errors[1] = RdbgDebugUiNotRegistered("owned UI lost")
    started.detach_failures[1] = 1
    config = session_config(tmp_path)
    config = replace(
        config,
        runtime=replace(
            config.runtime,
            connection_string='Srvr="localhost";Ref="runtime_test";',
        ),
    )

    with pytest.raises(ProtocolError, match="private detach failure"):
        RuntimeSession.start(config)

    assert started.detach_calls == [1, 1]
    assert started.closed_attempts == [1]
    assert started.closed_transports == [1]


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "failure",
    (
        CommandTimeout("managed timeout"),
        UnexpectedStop("unexpected managed bootstrap stop"),
    ),
)
def test_managed_bootstrap_failure_repairs_fast_path_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: BaseException,
) -> None:
    lifecycle = FakeLifecycle(decisions=[fast_decision(), slow_decision()])
    started = patch_successful_runtime_attempt(monkeypatch, lifecycle)
    started.managed_errors[1] = failure

    session = RuntimeSession.start(session_config(tmp_path))

    assert lifecycle.events[:4] == [
        "prepare",
        "attempt-close",
        "invalidate",
        "prepare-force-slow",
    ]
    assert started.attempt_count == 2
    session.close()


def test_server_entry_handshake_mismatch_repairs_fast_path_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lifecycle = FakeLifecycle(decisions=[fast_decision(), slow_decision()])
    started = patch_successful_runtime_attempt(monkeypatch, lifecycle)
    started.server_mismatch_attempts.add(1)

    session = RuntimeSession.start(session_config(tmp_path))

    assert lifecycle.events[:6] == [
        "prepare",
        "managed-handshake",
        "server-handshake",
        "attempt-close",
        "invalidate",
        "prepare-force-slow",
    ]
    assert started.attempt_count == 2
    evidence = json.loads(
        (session.artifacts.run_dir / "bootstrap.json").read_text(encoding="utf-8")
    )
    assert evidence["attempt_failures"] == [
        {
            "admitted": False,
            "attempt": 1,
            "cleanup_succeeded": True,
            "decision_mode": "fast",
            "error_type": "ExtensionHandshakeError",
            "repairable": True,
            "stage": "server-entry-handshake",
        }
    ]
    session.close()


def test_cleanup_failure_preserves_primary_and_adds_only_sanitized_note(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    primary = ExtensionHandshakeError("primary mismatch")
    cleanup = RuntimeError("private cleanup diagnostic")
    lifecycle = FakeLifecycle(
        decisions=[fast_decision(), slow_decision()],
        first_handshake_error=primary,
    )
    started = patch_successful_runtime_attempt(monkeypatch, lifecycle)
    started.cleanup_errors[1] = cleanup

    with pytest.raises(ExtensionHandshakeError) as raised:
        RuntimeSession.start(session_config(tmp_path))

    assert raised.value is primary
    assert raised.value.__notes__ == [
        "Runtime startup cleanup failed: RuntimeError",
        "Runtime startup cleanup is incomplete; retry with exception.retry_cleanup()",
    ]
    assert callable(getattr(raised.value, "retry_cleanup", None))
    assert "private cleanup diagnostic" not in raised.value.__notes__[0]
    assert lifecycle.prepare_count == 1
    assert "invalidate" not in lifecycle.events


def test_server_startup_cleanup_failure_exposes_ordered_retry_capability(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    primary = CommandTimeout("private service startup failure")
    lifecycle = FakeLifecycle(
        decisions=[fast_decision()], server_target_type="Server"
    )
    started = patch_successful_runtime_attempt(monkeypatch, lifecycle)
    started.service_errors[1] = primary
    started.termination_failures[1] = 1
    started.detach_failures[1] = 1
    config = session_config(tmp_path)
    config = replace(
        config,
        runtime=replace(
            config.runtime,
            connection_string='Srvr="localhost";Ref="runtime_test";',
        ),
    )

    with pytest.raises(CommandTimeout) as raised:
        RuntimeSession.start(config)

    assert raised.value is primary
    retry_cleanup = getattr(raised.value, "retry_cleanup", None)
    assert callable(retry_cleanup)
    assert getattr(raised.value, "__notes__", []) == [
        "Runtime startup cleanup failed: ProtocolError",
        "Runtime startup cleanup is incomplete; retry with exception.retry_cleanup()",
    ]
    assert started.termination_calls == [1]
    assert started.closed_attempts == []
    assert started.detach_calls == []
    assert started.closed_transports == []

    with pytest.raises(ProtocolError, match="Runtime startup cleanup failed"):
        retry_cleanup()

    assert started.termination_calls == [1, 1]
    assert started.closed_attempts == [1]
    assert started.detach_calls == [1]
    assert started.closed_transports == []

    retry_cleanup()
    retry_cleanup()

    assert started.termination_calls == [1, 1]
    assert started.closed_attempts == [1]
    assert started.detach_calls == [1, 1]
    assert started.closed_transports == [1]


def test_server_handshake_failure_never_commits_marker_early(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lifecycle = FakeLifecycle(decisions=[slow_decision()])
    started = patch_successful_runtime_attempt(monkeypatch, lifecycle)
    started.server_mismatch_attempts.add(1)

    with pytest.raises(ExtensionHandshakeError):
        RuntimeSession.start(session_config(tmp_path))

    assert lifecycle.events == [
        "prepare",
        "managed-handshake",
        "server-handshake",
        "attempt-close",
    ]
    assert started.closed_transports == [1]
    assert started.closed_attempts == [1]
    assert "commit" not in lifecycle.events


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "failure",
    (
        CommandTimeout("service stop timeout"),
        ExtensionHandshakeError("service stop protocol failure"),
    ),
)
def test_service_stop_failure_is_not_a_repair_candidate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: BaseException,
) -> None:
    lifecycle = FakeLifecycle(decisions=[fast_decision(), slow_decision()])
    started = patch_successful_runtime_attempt(monkeypatch, lifecycle)
    started.service_errors[1] = failure

    with pytest.raises((CommandTimeout, ExtensionHandshakeError)) as raised:
        RuntimeSession.start(session_config(tmp_path))

    assert raised.value is failure
    assert lifecycle.prepare_count == 1
    assert started.attempt_count == 1
    assert started.closed_transports == [1]
    assert started.closed_attempts == [1]
    assert "invalidate" not in lifecycle.events
