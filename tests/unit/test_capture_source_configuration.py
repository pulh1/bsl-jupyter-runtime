from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
import shutil
from threading import RLock
from typing import cast
from uuid import UUID

import pytest

from onec_runtime.capture_source import CapturePointRequest, CaptureSourceConfig
from onec_runtime.config import RuntimeConfig
from onec_runtime.errors import (
    CaptureSourceNotConfigured,
    ProtocolError,
)
from onec_runtime.execution.public_facade import PublicExecutionFacade
from onec_runtime.kernel import OBJECT_MODULE_PROPERTY_ID
from onec_runtime.rdbg.models import ModuleLocation, StackFrame, TargetId
from onec_runtime.runtime_api import PrototypeRuntimeApi
from onec_runtime.session import RuntimeSession, RuntimeSessionConfig
from tests.unit.test_extension_session import (
    FakeLifecycle,
    fast_decision,
    patch_successful_runtime_attempt,
    session_config,
)
from tests.unit.test_configuration_source_layout import FIXTURES

MODULE_UUID = "11111111-2222-3333-4444-555555555555"
SESSION_STATUS_FIXTURE = object()
ACTIVE_TICKET_FIXTURE = object()


@dataclass
class RecordingCaptureApi:
    calls: list[tuple[ModuleLocation, ...]] = field(default_factory=list)
    failure: BaseException | None = None

    def configure_capture_points(self, locations: tuple[ModuleLocation, ...]) -> None:
        if self.failure is not None:
            raise self.failure
        self.calls.append(locations)


def bare_capture_session(api: RecordingCaptureApi) -> RuntimeSession:
    session = object.__new__(RuntimeSession)
    session._operation_lock = RLock()
    session.runtime_api = cast(PrototypeRuntimeApi, api)
    session._active_capture_ticket = None
    session._active_capture_points = ()
    session._active_capture_locations = ()
    session._capture_source_resolver = None
    session._capture_source_bindings = {}
    object.__setattr__(session, "status", lambda: SESSION_STATUS_FIXTURE)
    return session


def write_common_module_source(root: Path) -> Path:
    module = root / "CommonModules" / "Продажи"
    module.mkdir(parents=True)
    (module / "Продажи.mdo").write_text(
        f'<mdclass:CommonModule xmlns:mdclass="urn:test" uuid="{MODULE_UUID}"/>',
        encoding="utf-8",
    )
    (module / "Module.bsl").write_text(
        "Процедура Провести() Экспорт\n"
        "    ПодготовитьДанные();\n"
        "    ЗаписатьДвижения();\n"
        "КонецПроцедуры\n",
        encoding="utf-8",
    )
    return root


def request() -> CapturePointRequest:
    return CapturePointRequest("point", "ut", "Продажи", "Провести", 3)


def test_runtime_config_does_not_require_capture_source(tmp_path: Path) -> None:
    platform_bin = tmp_path / "bin"
    platform_bin.mkdir()
    for executable in ("1cv8.exe", "1cv8c.exe", "dbgs.exe"):
        (platform_bin / executable).touch()
    runtime_config = RuntimeConfig(tmp_path, platform_bin)
    config = RuntimeSessionConfig(runtime_config, tmp_path / "evidence")

    assert config.capture_source is None


def test_capture_source_config_accepts_string_path(tmp_path: Path) -> None:
    config = CaptureSourceConfig("ut", str(tmp_path / "export"))

    assert config.source_root == (tmp_path / "export").resolve()


def test_symbolic_capture_without_source_fails_lazily_and_keeps_runtime_alive() -> None:
    api = RecordingCaptureApi()
    session = bare_capture_session(api)

    with pytest.raises(
        CaptureSourceNotConfigured,
        match="configure_capture_source",
    ):
        session.resolve_capture_points((request(),))

    assert api.calls == []
    assert session.status() is SESSION_STATUS_FIXTURE


def test_invalid_source_path_fails_only_when_resolution_is_requested(
    tmp_path: Path,
) -> None:
    api = RecordingCaptureApi()
    session = bare_capture_session(api)

    session.configure_capture_source("ut", tmp_path / "missing")

    assert api.calls == [()]
    with pytest.raises(ProtocolError, match="source root"):
        session.resolve_capture_points((request(),))
    assert session.status() is SESSION_STATUS_FIXTURE


def test_live_source_replacement_disarms_and_invalidates_old_bindings(
    tmp_path: Path,
) -> None:
    api = RecordingCaptureApi()
    session = bare_capture_session(api)
    first = write_common_module_source(tmp_path / "first")
    second = write_common_module_source(tmp_path / "second")

    session.configure_capture_source("ut", first)
    old = session.resolve_capture_points((request(),))
    session.configure_capture_source("ut", second)

    assert api.calls == [(), ()]
    with pytest.raises(ValueError, match="not resolved by this capture source"):
        session.verify_capture_points(old)


def test_clear_capture_source_disarms_and_invalidates_bindings(
    tmp_path: Path,
) -> None:
    api = RecordingCaptureApi()
    session = bare_capture_session(api)
    session.configure_capture_source("ut", write_common_module_source(tmp_path))
    old = session.resolve_capture_points((request(),))

    session.clear_capture_source()

    assert api.calls == [(), ()]
    with pytest.raises(CaptureSourceNotConfigured):
        session.resolve_capture_points((request(),))
    with pytest.raises(ValueError, match="not resolved by this capture source"):
        session.verify_capture_points(old)


def test_public_facade_receives_rebound_frame_source_resolver(tmp_path: Path) -> None:
    calls: list[tuple[str, object]] = []
    facade = object.__new__(PublicExecutionFacade)
    facade.configure_capture_points = (
        lambda locations: calls.append(("points", locations))
    )
    facade.configure_capture_source_resolver = (
        lambda resolver: calls.append(("resolver", resolver))
    )
    session = bare_capture_session(cast(RecordingCaptureApi, facade))

    session.configure_capture_source("ut", FIXTURES / "designer_base")
    configured = session._capture_stack_source_resolver
    assert configured is not None
    assert calls == [("points", ()), ("resolver", configured)]

    session.clear_capture_source()
    assert calls[-2:] == [("points", ()), ("resolver", None)]


def test_disarm_failure_preserves_existing_source_and_bindings(
    tmp_path: Path,
) -> None:
    api = RecordingCaptureApi()
    session = bare_capture_session(api)
    first = write_common_module_source(tmp_path / "first")
    second = write_common_module_source(tmp_path / "second")
    session.configure_capture_source("ut", first)
    old = session.resolve_capture_points((request(),))
    failure = RuntimeError("disarm failed")
    api.failure = failure

    with pytest.raises(RuntimeError) as raised:
        session.configure_capture_source("ut", second)

    assert raised.value is failure
    api.failure = None
    assert session.verify_capture_points(old)[0].point == old[0]
    assert api.calls == [()]


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "operation", ["configure", "clear"]
)
def test_active_capture_rejects_source_change_without_disarming(
    tmp_path: Path,
    operation: str,
) -> None:
    api = RecordingCaptureApi()
    session = bare_capture_session(api)
    object.__setattr__(session, "_active_capture_ticket", ACTIVE_TICKET_FIXTURE)

    with pytest.raises(ProtocolError, match="active capture"):
        if operation == "configure":
            session.configure_capture_source("ut", tmp_path)
        else:
            session.clear_capture_source()

    assert api.calls == []


def test_verification_requires_exact_complete_cached_point(tmp_path: Path) -> None:
    session = bare_capture_session(RecordingCaptureApi())
    session.configure_capture_source("ut", write_common_module_source(tmp_path))
    point = session.resolve_capture_points((request(),))[0]

    with pytest.raises(ValueError, match="not resolved by this capture source"):
        session.verify_capture_points((replace(point, excerpt="different"),))


def test_bootstrap_source_is_configured_after_both_handshakes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    lifecycle = FakeLifecycle(decisions=[fast_decision()])
    patch_successful_runtime_attempt(monkeypatch, lifecycle)
    source_root = tmp_path / "source"
    config = replace(
        session_config(tmp_path),
        capture_source=CaptureSourceConfig("ut", source_root),
    )

    def configure(
        self: RuntimeSession,
        project: str,
        configured_root: Path,
    ) -> None:
        assert lifecycle.events[-1] == "commit"
        assert (project, configured_root) == ("ut", source_root)
        lifecycle.events.append("capture-source")

    monkeypatch.setattr(RuntimeSession, "configure_capture_source", configure)

    session = RuntimeSession.start(config)

    assert lifecycle.events[-3:] == [
        "server-handshake",
        "commit",
        "capture-source",
    ]
    session.close()


def test_bootstrap_source_root_configures_default_capture_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    lifecycle = FakeLifecycle(decisions=[fast_decision()])
    patch_successful_runtime_attempt(monkeypatch, lifecycle)
    source_root = tmp_path / "source"
    shutil.copytree(FIXTURES / "designer_base", source_root)
    config = replace(session_config(tmp_path), source_root=source_root)

    configured: list[tuple[str, Path]] = []

    def configure(
        self: RuntimeSession,
        project: str,
        configured_root: Path,
    ) -> None:
        assert lifecycle.events[-1] == "commit"
        configured.append((project, configured_root))

    monkeypatch.setattr(RuntimeSession, "configure_capture_source", configure)

    session = RuntimeSession.start(config)

    assert configured == [("Notebook", source_root.resolve())]
    session.close()


def test_bootstrap_source_root_binds_capture_stack_source_resolution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    lifecycle = FakeLifecycle(decisions=[fast_decision()])
    patch_successful_runtime_attempt(monkeypatch, lifecycle)

    class RuntimeApi:
        def configure_capture_points(
            self, _locations: tuple[ModuleLocation, ...]
        ) -> None:
            pass

    import onec_runtime.session as session_module
    monkeypatch.setattr(
        session_module, "PrototypeRuntimeApi", lambda *_args, **_kwargs: RuntimeApi()
    )
    source_root = tmp_path / "source"
    shutil.copytree(FIXTURES / "designer_base", source_root)

    session = RuntimeSession.start(
        replace(session_config(tmp_path), source_root=source_root)
    )
    try:
        resolver = session._capture_stack_source_resolver
        assert resolver is not None
        frame = StackFrame(
            TargetId(UUID(int=1), "test"),
            0,
            ModuleLocation(
                "ConfigModule",
                "",
                UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
                UUID(OBJECT_MODULE_PROPERTY_ID),
                2,
            ),
        )

        resolved, = resolver((frame,))

        assert resolved is not None
        assert (resolved.source, resolved.line) == (
            "Документ.ПриемНаРаботу.МодульОбъекта", 2
        )
    finally:
        session.close()


def test_bootstrap_explicit_capture_source_overrides_source_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    lifecycle = FakeLifecycle(decisions=[fast_decision()])
    patch_successful_runtime_attempt(monkeypatch, lifecycle)
    source_root = tmp_path / "source"
    capture_root = tmp_path / "capture"
    shutil.copytree(FIXTURES / "designer_base", source_root)
    capture_root.mkdir()
    config = replace(
        session_config(tmp_path),
        source_root=source_root,
        capture_source=CaptureSourceConfig("ut", capture_root),
    )

    configured: list[tuple[str, Path]] = []

    def configure(
        self: RuntimeSession,
        project: str,
        configured_root: Path,
    ) -> None:
        configured.append((project, configured_root))

    monkeypatch.setattr(RuntimeSession, "configure_capture_source", configure)

    session = RuntimeSession.start(config)

    assert configured == [("ut", capture_root)]
    session.close()


def test_bootstrap_source_failure_closes_admitted_session_without_repair(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    lifecycle = FakeLifecycle(decisions=[fast_decision()])
    started = patch_successful_runtime_attempt(monkeypatch, lifecycle)
    failure = ProtocolError("capture source rejected")
    config = replace(
        session_config(tmp_path),
        capture_source=CaptureSourceConfig("ut", tmp_path / "source"),
    )

    def reject(*_args: object, **_kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(RuntimeSession, "configure_capture_source", reject)

    with pytest.raises(ProtocolError) as raised:
        RuntimeSession.start(config)

    assert raised.value is failure
    assert lifecycle.prepare_count == 1
    assert "invalidate" not in lifecycle.events
    assert started.closed_attempts == [1]
    assert started.closed_transports == [1]


def test_refresh_capture_sources_invalidates_negative_results_and_old_points(tmp_path):
    from tests.unit.test_configuration_source_layout import FIXTURES
    from tests.unit.test_capture_source_resolver import location, DOCUMENT
    import shutil
    from onec_runtime.kernel import OBJECT_MODULE_PROPERTY_ID

    project = tmp_path / "project"
    shutil.copytree(FIXTURES / "designer_base", project)
    session = bare_capture_session(RecordingCaptureApi())
    session.configure_capture_source("demo", project)
    assert hasattr(session, "refresh_capture_sources"), (
        "session refresh hook is missing"
    )
    source = session._capture_source_catalog
    source.resolve_modules((location(),))
    before = source.generation
    session.refresh_capture_sources()
    assert source.generation == before + 1
    assert (
        source.resolve_modules((location(DOCUMENT, OBJECT_MODULE_PROPERTY_ID),))[
            0
        ].canonical_name
        == "Документ.ПриемНаРаботу.МодульОбъекта"
    )


def test_explicit_extension_configuration_rejects_mismatch_before_disarm():
    from tests.unit.test_configuration_source_layout import FIXTURES

    api = RecordingCaptureApi()
    session = bare_capture_session(api)
    with pytest.raises(ProtocolError, match="match"):
        session.configure_capture_source(
            "demo", FIXTURES / "designer_extension", layer="base"
        )
    assert api.calls == []


def test_active_capture_rejects_refresh():
    session = bare_capture_session(RecordingCaptureApi())
    session._active_capture_ticket = ACTIVE_TICKET_FIXTURE
    assert hasattr(session, "refresh_capture_sources"), (
        "session refresh hook is missing"
    )
    with pytest.raises(ProtocolError, match="active capture"):
        session.refresh_capture_sources()


@pytest.mark.parametrize("root_suffix", ["", "src"])
def test_symbolic_capture_uses_shared_edt_root_and_extension_layer(root_suffix):
    from tests.unit.test_configuration_source_layout import FIXTURES

    session = bare_capture_session(RecordingCaptureApi())
    session.configure_capture_source("demo", FIXTURES / "edt_extension" / root_suffix)
    point = CapturePointRequest("point", "demo", "Общий", "Выполнить", 2)
    resolved = session.resolve_capture_points((point,))
    assert (
        session.verify_capture_points(resolved)[0].location.extension_name
        == "Дополнение"
    )


@pytest.mark.parametrize("configured", [True, False])
def test_successful_reload_publishes_owning_source_version_failure_keeps_old(
    tmp_path, configured
):
    from tests.unit.test_configuration_source_layout import FIXTURES
    from onec_runtime.bsl import (
        SourceUnitKind,
        SourceUnitRef,
        mapped_visible_source,
        source_sha256,
    )
    from onec_runtime.bsl.module_universe import WorkerModuleUnit
    from onec_runtime.worker_universe import WorkerGenerationHandle
    from onec_runtime.bsl.module_catalog import SessionCommonModuleCatalog

    @dataclass
    class ReloadApi(RecordingCaptureApi):
        generation: int = 0
        active_units: dict = field(default_factory=dict)

        def confirmed_worker_module_units(self, handle):
            assert handle.generation == self.generation
            return tuple(self.active_units.values())

        def load_worker_modules(self, units, **kwargs):
            if self.failure:
                raise self.failure
            self.generation += 1
            self.active_units.update(
                (unit.logical_name.casefold(), unit) for unit in units
            )
            return WorkerGenerationHandle(1, 1, self.generation, "a" * 64)

    api = ReloadApi()
    session = bare_capture_session(api)
    session._active_worker_file_units = {}
    session._common_module_catalog = SessionCommonModuleCatalog(
        FIXTURES / "designer_base", profile="server"
    )
    if configured:
        session.configure_capture_source("demo", FIXTURES / "designer_base")

    def unit(text, revision):
        ref = SourceUnitRef(
            SourceUnitKind.MODULE, "Общий", revision, source_sha256(text)
        )
        return WorkerModuleUnit(
            "Общий", "module", revision, mapped_visible_source(text, ref)
        )

    first = unit("Процедура Выполнить()\nКонецПроцедуры", 1)
    session.load_worker_modules((first,))
    assert hasattr(session, "_capture_worker_sources"), (
        "successful reload publication hook is missing"
    )
    old = session._capture_worker_sources["общий"]
    old_generation = session._capture_source_catalog.generation if configured else None
    second = unit("Процедура Выполнить()\n    Значение = 2;\nКонецПроцедуры", 2)
    session.load_worker_modules((second,))
    assert old.read_text() == first.mapped_source.text
    assert old.generation == 1
    assert (
        session._capture_worker_sources["общий"].read_text()
        == second.mapped_source.text
    )
    assert session._capture_worker_sources["общий"].generation == 2
    if configured:
        assert session._capture_source_catalog.generation == old_generation
    api.failure = ProtocolError("promotion outcome unknown")
    with pytest.raises(ProtocolError):
        session.load_worker_modules((first,))
    assert session._capture_worker_sources["общий"].generation == 2


@pytest.mark.parametrize("loader", ["batch", "file"])
@pytest.mark.parametrize("failure", ["create", "unknown"])
def test_confirmed_worker_upserts_publish_complete_source_set(
    tmp_path, loader, failure
):
    import shutil
    from onec_runtime.bsl.module_catalog import SessionCommonModuleCatalog
    from onec_runtime.errors import BslExecutionError, WorkerPromotionOutcomeUnknown
    from tests.unit.test_bsl_module_catalog import add_metadata
    from tests.unit.test_configuration_source_layout import FIXTURES
    from tests.unit.test_runtime_api import (
        _common_module_catalog,
        _semantic_snapshot_runtime,
        _SemanticSnapshotFailureTarget,
        _worker_module_unit,
    )

    root = tmp_path / "project"
    shutil.copytree(FIXTURES / "designer_base", root)
    names = ("ModuleA", "ModuleB", "ModuleC")
    metadata_catalog = _common_module_catalog(*names)
    target = _SemanticSnapshotFailureTarget()
    api = _semantic_snapshot_runtime(tmp_path, metadata_catalog, target=target)
    session = bare_capture_session(api)
    session._common_module_catalog = SessionCommonModuleCatalog(
        root, profile="server-test"
    )
    session._active_worker_file_units = {}
    session._worker_file_revisions = {}
    session.configure_capture_source("demo", root)

    def save(name, revision):
        add_metadata(root, name, server=True, client=False, global_module=False)
        unit = _worker_module_unit(name, revision, metadata_catalog)
        path = root / "CommonModules" / name / "Ext/Module.bsl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(unit.mapped_source.text.encode("utf-8"))
        return unit, path

    a1, path_a = save("ModuleA", 1)
    b1, path_b = save("ModuleB", 1)
    if loader == "batch":
        first = session.load_worker_modules((a1, b1))
    else:
        session.load_worker_module(path_a)
        before_add = session._capture_source_catalog.generation
        first = session.load_worker_module(path_b)
        assert session._capture_source_catalog.generation == before_add + 1
    assert set(session._capture_worker_sources) == {"modulea", "moduleb"}
    old_b = session._capture_worker_sources["moduleb"]
    before_update = session._capture_source_catalog.generation
    a2, _ = save("ModuleA", 2)
    second = (
        session.load_worker_modules((a2,))
        if loader == "batch"
        else session.load_worker_module(path_a)
    )
    assert set(session._capture_worker_sources) == {"modulea", "moduleb"}
    assert {pin.generation for pin in session._capture_worker_sources.values()} == {
        second.generation
    }
    assert (
        session._capture_worker_sources["moduleb"].read_text() == b1.mapped_source.text
    )
    assert (
        session._capture_worker_sources["modulea"].read_text() == a2.mapped_source.text
    )
    assert old_b.generation == first.generation
    assert old_b.read_text() == b1.mapped_source.text
    assert session._capture_source_catalog.generation == before_update
    with pytest.raises(ProtocolError, match="generation"):
        api.confirmed_worker_module_units(first)

    c1, path_c = save("ModuleC", 1)
    third = (
        session.load_worker_modules((c1,))
        if loader == "batch"
        else session.load_worker_module(path_c)
    )
    assert set(session._capture_worker_sources) == {"modulea", "moduleb", "modulec"}
    assert {pin.generation for pin in session._capture_worker_sources.values()} == {
        third.generation
    }
    assert session._capture_source_catalog.generation == before_update + 1
    confirmed = session._capture_worker_sources
    failed_revision, _ = save("ModuleA", 3)
    target.failure = failure
    with pytest.raises((BslExecutionError, WorkerPromotionOutcomeUnknown)):
        if loader == "batch":
            session.load_worker_modules((failed_revision,))
        else:
            session.load_worker_module(path_a)
    assert session._capture_worker_sources is confirmed
    assert (
        session._capture_worker_sources["modulea"].read_text() == a2.mapped_source.text
    )
    assert session._capture_source_catalog.generation == before_update + 1


def test_confirmed_worker_source_inventory_rejects_unconfirmed_handle(tmp_path):
    from tests.unit.test_runtime_api import (
        _common_module_catalog,
        _semantic_snapshot_runtime,
    )

    api = _semantic_snapshot_runtime(tmp_path, _common_module_catalog("ModuleA"))
    with pytest.raises(ProtocolError, match="generation"):
        api.confirmed_worker_module_units(None)
