"""A confirmed Stop replaces the owned notebook runtime and revokes old proxies."""

from uuid import UUID

import pytest

from onec_runtime.errors import ProtocolError
from onec_runtime.execution.termination import FileTerminationConfirmed
from onec_runtime.rdbg.models import TargetId
from onec_runtime.runtime_models import RuntimeNamespaceSnapshot
from onec_runtime_jupyter import InteractiveRuntimeSession, install_runtime
from onec_runtime_jupyter import session as session_module
from onec_runtime_jupyter.extension import OnecRuntimeMagics


class _Shell:
    def __init__(self) -> None:
        self.user_ns: dict[str, object] = {}


class _Core:
    def __init__(self, generation: int, *, proof: object | None = None) -> None:
        self.config = object()
        self.confirmed_target_termination = proof
        self.generation = generation
        self.is_closed = False
        self.closes = 0
        self.capture_sources: list[tuple[str, object]] = []

    def namespace_snapshot(self) -> RuntimeNamespaceSnapshot:
        return RuntimeNamespaceSnapshot(self.generation, 1, ("Table",))

    def validate_value_reference(self, handle: str) -> str:
        return handle

    def close(self) -> None:
        self.closes += 1
        self.is_closed = True

    def configure_capture_source(self, project: str, source_root: object) -> None:
        self.capture_sources.append((project, source_root))

    def execute_bsl(self, source: str, **kwargs: object) -> object:
        return source

    def resume_capture(self, **kwargs: object) -> object:
        return kwargs

    def status(self) -> object:
        return self.generation


def test_confirmed_stop_replaces_runtime_and_invalidates_saved_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = TargetId(
        UUID(int=1), "Acceptance", UUID(int=2),
    )
    old = _Core(1, proof=FileTerminationConfirmed(target, 123, 0))
    new = _Core(2)
    shell = _Shell()
    install_runtime(shell, old)  # type: ignore[arg-type]
    saved_proxy = shell.user_ns["Table"]
    owner = InteractiveRuntimeSession(old)  # type: ignore[arg-type]
    owner._installed_shell = shell
    calls: list[object] = []
    monkeypatch.setattr(
        session_module.RuntimeSession, "start",
        classmethod(lambda cls, config, **kwargs: calls.append(config) or new),
    )
    monkeypatch.setattr(session_module, "start_guardian", lambda runtime: None)

    owner._recover_confirmed_stop()

    assert owner.runtime is new
    assert old.closes == 1
    assert calls == [old.config]
    assert shell.user_ns["_onec_runtime"] is new
    with pytest.raises(ProtocolError, match="stale"):
        saved_proxy.materialize()


def test_unknown_stop_keeps_existing_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = _Core(1)
    shell = _Shell()
    install_runtime(shell, old)  # type: ignore[arg-type]
    owner = InteractiveRuntimeSession(old)  # type: ignore[arg-type]
    owner._installed_shell = shell
    monkeypatch.setattr(
        session_module.RuntimeSession, "start",
        classmethod(lambda cls, config, **kwargs: pytest.fail("unknown stop restarted")),
    )

    owner._recover_confirmed_stop()

    assert owner.runtime is old
    assert old.closes == 0


def test_magic_entry_replaces_confirmed_stop_and_rebinds_capture_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    target = TargetId(UUID(int=1), "Acceptance", UUID(int=2))
    old = _Core(1)
    new = _Core(2)
    shell = _Shell()
    install_runtime(shell, old)  # type: ignore[arg-type]
    owner = InteractiveRuntimeSession(old)  # type: ignore[arg-type]
    owner._installed_shell = shell
    setattr(shell, "_onec_interactive_runtime_owner", owner)
    owner.configure_capture_source("demo", tmp_path)
    old.confirmed_target_termination = FileTerminationConfirmed(target, 123, 0)
    monkeypatch.setattr(
        session_module.RuntimeSession, "start",
        classmethod(lambda cls, config, **kwargs: new),
    )
    monkeypatch.setattr(session_module, "start_guardian", lambda runtime: None)

    runtime = OnecRuntimeMagics(shell)._runtime()  # type: ignore[arg-type]

    assert runtime is new
    assert owner.runtime is new
    assert new.capture_sources == [("demo", tmp_path.resolve())]


def test_incomplete_old_cleanup_blocks_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = TargetId(UUID(int=1), "Acceptance", UUID(int=2))
    old = _Core(1, proof=FileTerminationConfirmed(target, 123, 0))
    shell = _Shell()
    install_runtime(shell, old)  # type: ignore[arg-type]
    saved_proxy = shell.user_ns["Table"]
    owner = InteractiveRuntimeSession(old)  # type: ignore[arg-type]
    owner._installed_shell = shell
    old.close = lambda: None  # type: ignore[method-assign]
    monkeypatch.setattr(
        session_module.RuntimeSession, "start",
        classmethod(lambda cls, config, **kwargs: pytest.fail("unsafe replacement")),
    )

    with pytest.raises(ProtocolError, match="cleanup is incomplete"):
        owner._recover_confirmed_stop()

    assert owner.runtime is old
    with pytest.raises(ProtocolError, match="stale"):
        saved_proxy.materialize()
