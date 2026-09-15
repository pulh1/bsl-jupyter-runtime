from __future__ import annotations

import shutil
import threading
import time
from contextlib import contextmanager
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from hashlib import sha256
from pathlib import Path
from typing import cast

import pytest
from extension_bundle_support import ROOT_ID, write_dump_fixture, write_manifest_fixture

from onec_runtime.config import RuntimeConfig
from onec_runtime.errors import (
    ExtensionBundleError,
    ExtensionIdentityConflict,
    ExtensionLifecycleError,
    ExtensionNotInstalled,
    ProcessStartError,
)
from onec_runtime.extension_bundle import (
    ExtensionBundle,
    ExtensionHandshakeEvidence,
    ExtensionManifest,
    read_extension_manifest,
)
from onec_runtime.extension_lifecycle import (
    ExtensionLifecycle,
    LifecycleDecision,
    LifecycleMode,
    TargetExtensionState,
)
from onec_runtime.extension_state import (
    ExtensionStateStore,
    InfobaseExtensionLock,
    VerifiedExtensionState,
)
from onec_runtime.performance_profile import PhaseRecorder
from onec_runtime.toolchain import ToolResult


@dataclass
class FakeExtensionTools:
    dumps: list[Path | BaseException] = field(default_factory=list)
    fail_on_any_call: bool = False
    failures: dict[int, BaseException] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list, init=False)
    destinations: list[Path] = field(default_factory=list, init=False)
    logs: list[Path] = field(default_factory=list, init=False)
    mutation_sessions: int = field(default=0, init=False)
    mutation_active: bool = field(default=False, init=False)

    def _call(self, label: str, log_path: Path) -> ToolResult:
        if label in {"load-cfe", "apply", "disable-safe-mode"}:
            assert self.mutation_active, f"{label} ran outside the agent session"
        self.calls.append(label)
        self.logs.append(log_path)
        if self.fail_on_any_call:
            raise AssertionError(f"unexpected tool call: {label}")
        failure = self.failures.get(len(self.calls))
        if failure is not None:
            raise failure
        return ToolResult((label,), 0, log_path)

    @contextmanager
    def mutation_session(self, _log_dir: Path):
        assert not self.mutation_active
        self.mutation_sessions += 1
        self.mutation_active = True
        try:
            yield
        finally:
            self.mutation_active = False

    def dump_files(self, destination: Path, log_path: Path) -> ToolResult:
        result = self._call("dump-files", log_path)
        self.destinations.append(destination)
        if not self.dumps:
            raise AssertionError("dump fixture queue is empty")
        source = self.dumps.pop(0)
        if isinstance(source, BaseException):
            raise source
        shutil.copytree(source, destination)
        return result

    def dump_cfe(self, destination: Path, log_path: Path) -> ToolResult:
        result = self._call("dump-cfe", log_path)
        self.destinations.append(destination)
        destination.write_bytes(b"recognized-older-cfe")
        return result

    def load_cfe(self, source: Path, log_path: Path) -> ToolResult:
        self.destinations.append(source)
        return self._call("load-cfe", log_path)

    def apply(self, log_path: Path) -> ToolResult:
        return self._call("apply", log_path)

    def disable_safe_mode(self, log_dir: Path) -> None:
        self._call("disable-safe-mode", log_dir)


@dataclass(frozen=True)
class LifecycleFixture:
    lifecycle: ExtensionLifecycle
    bundle: ExtensionBundle
    manifest: ExtensionManifest
    state_store: ExtensionStateStore
    lock: InfobaseExtensionLock
    profiler: PhaseRecorder
    runtime: RuntimeConfig


def _verified_state(
    infobase: Path, manifest: ExtensionManifest
) -> VerifiedExtensionState:
    return VerifiedExtensionState(
        infobase_path=str(infobase.resolve()),
        product_id=manifest.product_id,
        cfe_sha256=manifest.cfe_sha256,
        manifest_schema_version=manifest.schema_version,
        artifact_version=manifest.artifact_version,
        protocol_version=manifest.protocol_version,
        identity_sha256=manifest.fingerprints.identity_sha256,
    )


def make_lifecycle(
    root: Path,
    *,
    tools: FakeExtensionTools,
    marker_matches: bool,
) -> LifecycleFixture:
    platform_bin = root / "bin"
    platform_bin.mkdir(parents=True)
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

    cfe = root / "bundle" / "OnecInteractiveRuntime.cfe"
    cfe.parent.mkdir()
    cfe.write_bytes(b"packaged-cfe")
    manifest = read_extension_manifest(
        write_manifest_fixture(
            cfe.parent,
            cfe_sha256=sha256(cfe.read_bytes()).hexdigest(),
            cfe_size=cfe.stat().st_size,
        )
    )
    bundle = ExtensionBundle(cfe, manifest, cfe.parent)
    state_store = ExtensionStateStore(runtime.runtime_dir / "extension-state", infobase)
    lock = InfobaseExtensionLock(runtime.runtime_dir / "extension-state", infobase)
    profiler = PhaseRecorder()
    lifecycle = ExtensionLifecycle(
        runtime,
        bundle,
        state_store,
        tools,
        profiler,
        lifecycle_lock=lock,
        lock_timeout_s=2.0,
    )
    if marker_matches:
        state_store.write(_verified_state(infobase, manifest))
    return LifecycleFixture(
        lifecycle, bundle, manifest, state_store, lock, profiler, runtime
    )


def test_server_handshake_uses_connection_identity_and_rejects_emulation(tmp_path: Path) -> None:
    fixture = make_lifecycle(tmp_path, tools=FakeExtensionTools(), marker_matches=False)
    runtime = replace(fixture.runtime, connection_string='Srvr="localhost";Ref="runtime_test";')
    store = ExtensionStateStore(runtime.runtime_dir / "extension-state", runtime.infobase_identity)
    lifecycle = ExtensionLifecycle(runtime, fixture.bundle, store, FakeExtensionTools())
    managed, emulated = _handshakes(fixture.manifest)
    server = replace(emulated, target_type="Server")

    lifecycle.commit_handshake((managed, server))
    assert store.read() is not None
    assert store.read().infobase_path == runtime.infobase_identity
    with pytest.raises(ExtensionLifecycleError, match="managed and one server"):
        lifecycle.commit_handshake((managed, emulated))


def _current_dump(root: Path) -> Path:
    return cast(Path, write_dump_fixture(root))


def _older_dump(root: Path) -> Path:
    return cast(Path, write_dump_fixture(root, artifact_version="0.0.9"))


def _version_dump(
    root: Path, artifact_version: str, *, protocol_version: str = "1"
) -> Path:
    return write_dump_fixture(
        root,
        artifact_version=artifact_version,
        protocol_version=protocol_version,
    )


def _same_version_source_mismatch(root: Path) -> Path:
    dump = _current_dump(root)
    module = dump / "CommonModules" / "RuntimeKernelServer" / "Ext" / "Module.bsl"
    module.write_text(
        module.read_text(encoding="utf-8-sig") + "// exact-artifact mismatch\n",
        encoding="utf-8-sig",
        newline="",
    )
    return dump


def _foreign_dump(root: Path) -> Path:
    dump = cast(Path, write_dump_fixture(root))
    foreign_root = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    for path in (dump / "Configuration.xml", dump / "ConfigDumpInfo.xml"):
        path.write_text(
            path.read_text(encoding="utf-8").replace(ROOT_ID, foreign_root),
            encoding="utf-8",
            newline="",
        )
    configuration = dump / "Configuration.xml"
    configuration.write_text(
        configuration.read_text(encoding="utf-8").replace(
            "<Vendor>onec-interactive-runtime</Vendor>",
            "<Vendor>foreign-runtime-product</Vendor>",
        ),
        encoding="utf-8",
        newline="",
    )
    declaration = 'ИдентификаторПродуктаRuntime = "onec-interactive-runtime";'
    replacement = 'ИдентификаторПродуктаRuntime = "foreign-runtime-product";'
    for relative in (
        Path("Ext/ManagedApplicationModule.bsl"),
        Path("CommonModules/RuntimeKernelServer/Ext/Module.bsl"),
    ):
        module = dump / relative
        module.write_text(
            module.read_text(encoding="utf-8-sig").replace(declaration, replacement),
            encoding="utf-8-sig",
            newline="",
        )
    return dump


def _handshakes(
    manifest: ExtensionManifest,
) -> tuple[ExtensionHandshakeEvidence, ExtensionHandshakeEvidence]:
    return (
        ExtensionHandshakeEvidence(
            "ManagedClient",
            manifest.product_id,
            manifest.artifact_version,
            manifest.protocol_version,
            manifest.breakpoints.managed,
        ),
        ExtensionHandshakeEvidence(
            "ServerEmulation",
            manifest.product_id,
            manifest.artifact_version,
            manifest.protocol_version,
            manifest.breakpoints.server_entry,
        ),
    )


def _handshake_pair(
    fixture: LifecycleFixture,
    *,
    product_id: str | None = None,
    artifact_version: str | None = None,
    protocol_version: str | None = None,
) -> tuple[ExtensionHandshakeEvidence, ExtensionHandshakeEvidence]:
    manifest = fixture.manifest
    values = (
        product_id or manifest.product_id,
        artifact_version or manifest.artifact_version,
        protocol_version or manifest.protocol_version,
    )
    return (
        ExtensionHandshakeEvidence(
            "ManagedClient", *values, manifest.breakpoints.managed
        ),
        ExtensionHandshakeEvidence(
            "ServerEmulation", *values, manifest.breakpoints.server_entry
        ),
    )


def test_matching_marker_selects_fast_path_without_designer(tmp_path: Path) -> None:
    tools = FakeExtensionTools(fail_on_any_call=True)
    fixture = make_lifecycle(tmp_path, tools=tools, marker_matches=True)

    decision = fixture.lifecycle.prepare()

    assert decision.mode is LifecycleMode.FAST
    assert decision.target_state is TargetExtensionState.CURRENT
    assert decision.retry_allowed is True
    assert tools.calls == []
    assert tools.mutation_sessions == 0
    assert fixture.profiler.events == []


def test_force_slow_ignores_matching_marker_and_inspects(tmp_path: Path) -> None:
    tools = FakeExtensionTools(
        dumps=[_current_dump(tmp_path / "current-before")]
    )
    fixture = make_lifecycle(tmp_path, tools=tools, marker_matches=True)

    decision = fixture.lifecycle.prepare(force_slow=True)

    assert decision.mode is LifecycleMode.SLOW
    assert decision.target_state is TargetExtensionState.CURRENT
    assert decision.retry_allowed is False
    assert tools.calls == ["dump-files", "disable-safe-mode"]


def test_absent_extension_installs_applies_and_redumps_without_check(tmp_path: Path) -> None:
    tools = FakeExtensionTools(
        dumps=[ExtensionNotInstalled("not installed"), _current_dump(tmp_path / "post")]
    )
    fixture = make_lifecycle(tmp_path, tools=tools, marker_matches=False)

    decision = fixture.lifecycle.prepare()

    assert decision.mode is LifecycleMode.SLOW
    assert decision.target_state is TargetExtensionState.INSTALLED
    assert tools.calls == ["dump-files", "load-cfe", "apply", "dump-files", "disable-safe-mode"]
    assert tools.mutation_sessions == 1
    assert fixture.state_store.read() is None
    assert [event.phase for event in fixture.profiler.events] == [
        "extension.inspect",
        "extension.install",
        "extension.apply",
        "extension.inspect",
        "extension.safe_mode",
    ]
    assert fixture.profiler.events[0].error_present is False


def test_exact_current_without_marker_tries_handshake_before_agent(tmp_path: Path) -> None:
    tools = FakeExtensionTools(
        dumps=[_current_dump(tmp_path / "first")]
    )
    fixture = make_lifecycle(tmp_path, tools=tools, marker_matches=False)

    decision = fixture.lifecycle.prepare()

    assert decision.mode is LifecycleMode.PROBED
    assert decision.target_state is TargetExtensionState.CURRENT
    assert decision.retry_allowed is True
    assert tools.calls == ["dump-files"]
    assert tools.mutation_sessions == 0
    assert fixture.state_store.read() is None


def test_recognized_0_0_9_updates_to_packaged_0_1_0(tmp_path: Path) -> None:
    tools = FakeExtensionTools(
        dumps=[_older_dump(tmp_path / "old"), _current_dump(tmp_path / "new")]
    )
    fixture = make_lifecycle(tmp_path, tools=tools, marker_matches=False)

    decision = fixture.lifecycle.prepare()

    assert decision.target_state is TargetExtensionState.UPDATED
    assert tools.calls == [
        "dump-files",
        "dump-cfe",
        "load-cfe",
        "apply",
        "dump-files",
        "disable-safe-mode",
    ]
    assert [event.phase for event in fixture.profiler.events] == [
        "extension.inspect",
        "extension.update",
        "extension.apply",
        "extension.inspect",
        "extension.safe_mode",
    ]


def test_newer_0_2_0_fails_closed_before_any_mutation(
    tmp_path: Path,
) -> None:
    tools = FakeExtensionTools(dumps=[_version_dump(tmp_path / "newer", "0.2.0")])
    fixture = make_lifecycle(tmp_path, tools=tools, marker_matches=False)

    with pytest.raises(ExtensionLifecycleError, match="strictly older"):
        fixture.lifecycle.prepare()

    assert tools.calls == ["dump-files"]
    assert fixture.state_store.read() is None


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "mismatch", ["protocol", "source"]
)
def test_same_0_1_0_exact_mismatch_fails_closed_without_mutation(
    tmp_path: Path,
    mismatch: str,
) -> None:
    installed = (
        _version_dump(tmp_path / mismatch, "0.1.0", protocol_version="999")
        if mismatch == "protocol"
        else _same_version_source_mismatch(tmp_path / mismatch)
    )
    tools = FakeExtensionTools(dumps=[installed])
    fixture = make_lifecycle(tmp_path, tools=tools, marker_matches=False)

    with pytest.raises(ExtensionLifecycleError, match="strictly older"):
        fixture.lifecycle.prepare()

    assert tools.calls == ["dump-files"]
    assert fixture.state_store.read() is None


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "installed_version",
    [
        "0.1",
        "01.0.0",
        "0.1.0-alpha",
        "0.1.0+build",
        "0.1.0.0",
    ],
)
def test_ambiguous_product_version_fails_closed_without_mutation(
    tmp_path: Path,
    installed_version: str,
) -> None:
    tools = FakeExtensionTools(
        dumps=[_version_dump(tmp_path / "ambiguous", installed_version)]
    )
    fixture = make_lifecycle(tmp_path, tools=tools, marker_matches=False)

    with pytest.raises(ExtensionLifecycleError, match="canonical MAJOR.MINOR.PATCH"):
        fixture.lifecycle.prepare()

    assert tools.calls == ["dump-files"]
    assert fixture.state_store.read() is None


def test_foreign_identity_fails_closed_after_only_initial_dump(tmp_path: Path) -> None:
    tools = FakeExtensionTools(dumps=[_foreign_dump(tmp_path / "foreign")])
    fixture = make_lifecycle(tmp_path, tools=tools, marker_matches=False)

    with pytest.raises(ExtensionIdentityConflict, match="identity"):
        fixture.lifecycle.prepare()

    assert tools.calls == ["dump-files"]
    assert fixture.state_store.read() is None


def test_malformed_dump_is_not_classified_as_absent(tmp_path: Path) -> None:
    malformed = _current_dump(tmp_path / "malformed")
    (malformed / "ConfigDumpInfo.xml").write_text("<broken", encoding="utf-8")
    tools = FakeExtensionTools(dumps=[malformed])
    fixture = make_lifecycle(tmp_path, tools=tools, marker_matches=False)

    with pytest.raises(ExtensionBundleError, match="malformed"):
        fixture.lifecycle.prepare()

    assert tools.calls == ["dump-files"]
    assert fixture.profiler.events[-1].phase == "extension.inspect"
    assert fixture.profiler.events[-1].error_present is True


def test_authentication_failure_is_not_classified_as_absent(tmp_path: Path) -> None:
    authentication_error = ProcessStartError("authentication failed")
    tools = FakeExtensionTools(dumps=[authentication_error])
    fixture = make_lifecycle(tmp_path, tools=tools, marker_matches=False)

    with pytest.raises(ProcessStartError) as raised:
        fixture.lifecycle.prepare()

    assert raised.value is authentication_error
    assert tools.calls == ["dump-files"]
    assert fixture.profiler.events[-1].error_present is True


def test_update_failure_rolls_back_and_proves_exact_old_fingerprint(
    tmp_path: Path,
) -> None:
    primary_error = ProcessStartError("new extension apply failed")
    tools = FakeExtensionTools(
        dumps=[_older_dump(tmp_path / "before"), _older_dump(tmp_path / "restored")],
        failures={4: primary_error},
    )
    fixture = make_lifecycle(tmp_path, tools=tools, marker_matches=False)

    with pytest.raises(ProcessStartError) as raised:
        fixture.lifecycle.prepare()

    assert raised.value is primary_error
    assert tools.calls == [
        "dump-files",
        "dump-cfe",
        "load-cfe",
        "apply",
        "load-cfe",
        "apply",
        "dump-files",
    ]
    assert not getattr(primary_error, "__notes__", [])
    assert fixture.state_store.read() is None


def test_update_rollback_failure_is_a_typed_note_on_the_primary_error(
    tmp_path: Path,
) -> None:
    primary_error = ProcessStartError("new extension apply failed")
    rollback_error = ProcessStartError("backup load failed with private diagnostic")
    tools = FakeExtensionTools(
        dumps=[_older_dump(tmp_path / "before")],
        failures={4: primary_error, 5: rollback_error},
    )
    fixture = make_lifecycle(tmp_path, tools=tools, marker_matches=False)

    with pytest.raises(ProcessStartError) as raised:
        fixture.lifecycle.prepare()

    assert raised.value is primary_error
    assert tools.calls == [
        "dump-files",
        "dump-cfe",
        "load-cfe",
        "apply",
        "load-cfe",
    ]
    assert raised.value.__notes__ == ["Extension rollback failed: ProcessStartError"]
    assert "private diagnostic" not in raised.value.__notes__[0]


def test_rollback_fingerprint_mismatch_is_not_silently_accepted(tmp_path: Path) -> None:
    primary_error = ProcessStartError("new extension apply failed")
    tools = FakeExtensionTools(
        dumps=[_older_dump(tmp_path / "before"), _current_dump(tmp_path / "wrong")],
        failures={4: primary_error},
    )
    fixture = make_lifecycle(tmp_path, tools=tools, marker_matches=False)

    with pytest.raises(ProcessStartError) as raised:
        fixture.lifecycle.prepare()

    assert raised.value is primary_error
    assert raised.value.__notes__ == [
        "Extension rollback failed: ExtensionLifecycleError"
    ]


def test_post_update_inspection_failure_restores_and_proves_old_artifact(
    tmp_path: Path,
) -> None:
    tools = FakeExtensionTools(
        dumps=[
            _older_dump(tmp_path / "before"),
            _older_dump(tmp_path / "failed-post-update"),
            _older_dump(tmp_path / "restored"),
        ]
    )
    fixture = make_lifecycle(tmp_path, tools=tools, marker_matches=False)

    with pytest.raises(ExtensionLifecycleError) as raised:
        fixture.lifecycle.prepare()

    assert "exact packaged artifact" in str(raised.value)
    assert tools.calls == [
        "dump-files",
        "dump-cfe",
        "load-cfe",
        "apply",
        "dump-files",
        "load-cfe",
        "apply",
        "dump-files",
    ]
    assert not getattr(raised.value, "__notes__", [])


def test_slow_path_rechecks_marker_after_acquiring_real_infobase_lock(
    tmp_path: Path,
) -> None:
    tools = FakeExtensionTools(fail_on_any_call=True)
    fixture = make_lifecycle(tmp_path, tools=tools, marker_matches=False)
    holder_ready = threading.Event()
    permit_marker = threading.Event()
    result: list[LifecycleDecision] = []

    def hold_then_commit() -> None:
        with fixture.lock.acquire(timeout_s=1.0):
            holder_ready.set()
            assert permit_marker.wait(timeout=1.0)
            fixture.state_store.write(
                _verified_state(fixture.runtime.infobase_dir, fixture.manifest)
            )

    def prepare() -> None:
        result.append(fixture.lifecycle.prepare())

    holder = threading.Thread(target=hold_then_commit)
    holder.start()
    assert holder_ready.wait(timeout=1.0)
    contender = threading.Thread(target=prepare)
    contender.start()
    time.sleep(0.03)
    permit_marker.set()
    holder.join(timeout=1.0)
    contender.join(timeout=1.0)

    assert not holder.is_alive()
    assert not contender.is_alive()
    decision = result[0]
    assert decision.mode is LifecycleMode.FAST
    assert tools.calls == []


def test_each_slow_prepare_uses_fresh_private_operation_paths(tmp_path: Path) -> None:
    tools = FakeExtensionTools(
        dumps=[
            _current_dump(tmp_path / "first-before"),
            _current_dump(tmp_path / "second-before"),
        ]
    )
    fixture = make_lifecycle(tmp_path, tools=tools, marker_matches=False)

    fixture.lifecycle.prepare(force_slow=True)
    first_destination = tools.destinations[0]
    fixture.lifecycle.prepare(force_slow=True)
    second_destination = tools.destinations[1]

    assert first_destination != second_destination
    assert first_destination.is_relative_to(fixture.runtime.runtime_dir)
    assert second_destination.is_relative_to(fixture.runtime.runtime_dir)
    assert all(fixture.runtime.username not in str(path) for path in tools.logs)


def test_prepare_never_writes_marker_before_live_handshake(tmp_path: Path) -> None:
    tools = FakeExtensionTools(
        dumps=[ExtensionNotInstalled("not installed"), _current_dump(tmp_path / "post")]
    )
    fixture = make_lifecycle(tmp_path, tools=tools, marker_matches=False)

    fixture.lifecycle.prepare()

    assert fixture.state_store.read() is None


def test_two_sided_handshake_commits_exact_manifest_bound_marker(
    tmp_path: Path,
) -> None:
    fixture = make_lifecycle(
        tmp_path, tools=FakeExtensionTools(fail_on_any_call=True), marker_matches=False
    )
    evidence = _handshakes(fixture.manifest)

    fixture.lifecycle.commit_handshake(evidence)

    state = fixture.state_store.read()
    assert state is not None
    assert state == _verified_state(fixture.runtime.infobase_dir, fixture.manifest)
    assert fixture.profiler.events[-1].phase == "extension.handshake"
    assert fixture.profiler.events[-1].error_present is False


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "mutate",
    [
        lambda client, server: (client,),
        lambda client, server: (client, client),
        lambda client, server: (server, server),
        lambda client, server: (replace(client, product_id="foreign"), server),
        lambda client, server: (
            replace(client, artifact_version="0.0.9"),
            server,
        ),
        lambda client, server: (replace(client, protocol_version="2"), server),
        lambda client, server: (
            replace(client, location=server.location),
            server,
        ),
    ],
)
def test_handshake_rejects_omissions_duplicates_and_mismatches_without_marker(
    tmp_path: Path,
    mutate: Callable[
        [ExtensionHandshakeEvidence, ExtensionHandshakeEvidence],
        tuple[ExtensionHandshakeEvidence, ...],
    ],
) -> None:
    fixture = make_lifecycle(
        tmp_path, tools=FakeExtensionTools(fail_on_any_call=True), marker_matches=False
    )
    client, server = _handshakes(fixture.manifest)
    invalid = mutate(client, server)

    with pytest.raises(ExtensionLifecycleError, match="handshake"):
        fixture.lifecycle.commit_handshake(
            cast(
                tuple[ExtensionHandshakeEvidence, ExtensionHandshakeEvidence],
                invalid,
            )
        )

    assert fixture.state_store.read() is None
    assert fixture.profiler.events[-1].phase == "extension.handshake"
    assert fixture.profiler.events[-1].error_present is True


def test_auto_handshake_rejects_common_nonpackaged_artifact_version_without_marker(
    tmp_path: Path,
) -> None:
    tools = FakeExtensionTools(fail_on_any_call=True)
    fixture = make_lifecycle(tmp_path, tools=tools, marker_matches=False)

    with pytest.raises(ExtensionLifecycleError, match="packaged manifest"):
        fixture.lifecycle.commit_handshake(
            _handshake_pair(fixture, artifact_version="0.1.0-user.1")
        )

    assert fixture.state_store.read() is None
    assert tools.calls == []
    assert fixture.profiler.events[-1].error_present is True


def test_invalidate_marker_removes_prior_verified_state(tmp_path: Path) -> None:
    fixture = make_lifecycle(
        tmp_path, tools=FakeExtensionTools(fail_on_any_call=True), marker_matches=True
    )

    fixture.lifecycle.invalidate_marker()

    assert fixture.state_store.read() is None


def test_manual_prepare_invalidates_auto_marker_without_designer_calls(
    tmp_path: Path,
) -> None:
    tools = FakeExtensionTools(fail_on_any_call=True)
    fixture = make_lifecycle(tmp_path, tools=tools, marker_matches=True)

    decision = fixture.lifecycle.prepare_manual()

    assert decision == LifecycleDecision(
        LifecycleMode.MANUAL,
        TargetExtensionState.USER_MANAGED,
        fixture.bundle,
        retry_allowed=False,
    )
    assert tools.calls == []
    assert not fixture.state_store.matches(fixture.manifest)
    assert [event.phase for event in fixture.profiler.events] == []


def test_manual_handshake_accepts_common_custom_artifact_version_without_marker(
    tmp_path: Path,
) -> None:
    fixture = make_lifecycle(
        tmp_path,
        tools=FakeExtensionTools(fail_on_any_call=True),
        marker_matches=False,
    )

    observed = fixture.lifecycle.accept_manual_handshake(
        _handshake_pair(fixture, artifact_version="0.1.0-user.1")
    )

    assert observed == "0.1.0-user.1"
    assert not fixture.state_store.matches(fixture.manifest)
    assert [event.phase for event in fixture.profiler.events] == [
        "extension.handshake"
    ]


def test_manual_handshake_rejects_predecessor_protocol_before_target_work(
    tmp_path: Path,
) -> None:
    tools = FakeExtensionTools(fail_on_any_call=True)
    fixture = make_lifecycle(
        tmp_path,
        tools=tools,
        marker_matches=False,
    )
    assert fixture.manifest.protocol_version == "2"

    with pytest.raises(ExtensionLifecycleError, match="packaged manifest"):
        fixture.lifecycle.accept_manual_handshake(
            _handshake_pair(
                fixture,
                artifact_version="0.1.2-user-managed",
                protocol_version="1",
            )
        )

    assert tools.calls == []
    assert fixture.state_store.read() is None


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    ("mutate", "message"),
    [
        (
            lambda client, server: (
                replace(client, product_id="private-product"),
                replace(server, product_id="private-product"),
            ),
            "packaged manifest",
        ),
        (
            lambda client, server: (
                replace(client, protocol_version="private-protocol"),
                replace(server, protocol_version="private-protocol"),
            ),
            "packaged manifest",
        ),
        (
            lambda client, server: (replace(client, location="private-location"), server),
            "breakpoint locations",
        ),
        (lambda client, server: (client, client), "duplicate target"),
        (
            lambda client, server: (
                client,
                replace(server, artifact_version="private-server-version"),
            ),
            "client/server",
        ),
    ],
)
def test_manual_handshake_rejects_invalid_evidence_without_private_values_or_marker(
    tmp_path: Path,
    mutate: Callable[
        [ExtensionHandshakeEvidence, ExtensionHandshakeEvidence],
        tuple[ExtensionHandshakeEvidence, ExtensionHandshakeEvidence],
    ],
    message: str,
) -> None:
    fixture = make_lifecycle(
        tmp_path,
        tools=FakeExtensionTools(fail_on_any_call=True),
        marker_matches=False,
    )
    invalid = mutate(*_handshake_pair(fixture, artifact_version="private-client-version"))

    with pytest.raises(ExtensionLifecycleError, match=message) as raised:
        fixture.lifecycle.accept_manual_handshake(invalid)

    assert "private-" not in str(raised.value)
    assert fixture.state_store.read() is None
    assert fixture.profiler.events[-1].phase == "extension.handshake"
    assert fixture.profiler.events[-1].error_present is True
