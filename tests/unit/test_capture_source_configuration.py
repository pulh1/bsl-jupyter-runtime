from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import RLock
from typing import cast

import pytest

from onec_runtime.capture_source import CapturePointRequest, CaptureSourceConfig
from onec_runtime.config import RuntimeConfig
from onec_runtime.errors import (
    CaptureSourceNotConfigured,
    ProtocolError,
)
from onec_runtime.rdbg.models import ModuleLocation
from onec_runtime.runtime_api import PrototypeRuntimeApi
from onec_runtime.session import RuntimeSession, RuntimeSessionConfig
from tests.unit.test_extension_session import (
    FakeLifecycle,
    fast_decision,
    patch_successful_runtime_attempt,
    session_config,
)

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
