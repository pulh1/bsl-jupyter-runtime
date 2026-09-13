from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import ContextManager, Protocol

from onec_runtime.config import RuntimeConfig
from onec_runtime.errors import (
    ExtensionIdentityConflict,
    ExtensionLifecycleError,
    ExtensionNotInstalled,
)
from onec_runtime.extension_bundle import (
    ExtensionBundle,
    ExtensionFingerprints,
    ExtensionHandshakeEvidence,
    fingerprint_extension_dump,
)
from onec_runtime.extension_state import (
    ExtensionStateStore,
    InfobaseExtensionLock,
    VerifiedExtensionState,
)
from onec_runtime.performance_profile import PhaseRecorder
from onec_runtime.toolchain import ToolResult

_PRODUCT_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")


def _product_version_key(value: str) -> tuple[tuple[int, str], ...]:
    match = _PRODUCT_VERSION.fullmatch(value)
    if match is None:
        raise ExtensionLifecycleError(
            "extension artifact versions must use canonical MAJOR.MINOR.PATCH decimal syntax"
        )
    return tuple((len(component), component) for component in match.groups())


class TargetExtensionState(StrEnum):
    ABSENT = "absent"
    CURRENT = "current"
    OLDER = "older"
    FOREIGN = "foreign"
    INSTALLED = "installed"
    UPDATED = "updated"
    USER_MANAGED = "user-managed"


class LifecycleMode(StrEnum):
    FAST = "fast"
    SLOW = "slow"
    MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class LifecycleDecision:
    mode: LifecycleMode
    target_state: TargetExtensionState
    bundle: ExtensionBundle
    retry_allowed: bool


class ExtensionToolOperations(Protocol):
    def mutation_session(self, log_dir: Path) -> ContextManager[None]: ...

    def dump_files(self, destination: Path, log_path: Path) -> ToolResult: ...

    def dump_cfe(self, destination: Path, log_path: Path) -> ToolResult: ...

    def load_cfe(self, source: Path, log_path: Path) -> ToolResult | None: ...

    def apply(self, log_path: Path) -> ToolResult | None: ...

    def disable_safe_mode(self, log_dir: Path) -> None: ...


@dataclass(frozen=True, slots=True)
class _OperationPaths:
    root: Path
    initial_dump: Path
    final_dump: Path
    rollback_dump: Path
    backup_cfe: Path

    def log(self, name: str) -> Path:
        return self.root / f"{name}.log"


class ExtensionLifecycle:
    def __init__(
        self,
        config: RuntimeConfig,
        bundle: ExtensionBundle,
        state_store: ExtensionStateStore,
        tools: ExtensionToolOperations,
        profiler: PhaseRecorder | None = None,
        *,
        lifecycle_lock: InfobaseExtensionLock | None = None,
        lock_timeout_s: float = 30.0,
    ) -> None:
        self.config = config
        self.bundle = bundle
        self.state_store = state_store
        self.tools = tools
        self.profiler = profiler if profiler is not None else PhaseRecorder()
        state_root = config.runtime_dir / "extension-state"
        self._lock = lifecycle_lock or InfobaseExtensionLock(
            state_root, config.infobase_identity
        )
        self._lock_timeout_s = lock_timeout_s

    def prepare(self, *, force_slow: bool = False) -> LifecycleDecision:
        manifest = self.bundle.manifest
        if not force_slow and self.state_store.matches(manifest):
            return self._fast_decision()

        with self._lock.acquire(timeout_s=self._lock_timeout_s):
            if not force_slow and self.state_store.matches(manifest):
                return self._fast_decision()
            self.state_store.invalidate()
            paths = self._new_operation_paths()
            state, installed = self._inspect(paths.initial_dump, paths.log("inspect"))
            if state not in (
                TargetExtensionState.ABSENT,
                TargetExtensionState.CURRENT,
                TargetExtensionState.OLDER,
            ):
                raise ExtensionIdentityConflict(
                    "installed extension has a foreign permanent identity"
                )

            with self.tools.mutation_session(paths.root / "agent"):
                if state is TargetExtensionState.ABSENT:
                    self._install(paths)
                    result_state = TargetExtensionState.INSTALLED
                elif state is TargetExtensionState.CURRENT:
                    result_state = TargetExtensionState.CURRENT
                else:
                    assert installed is not None
                    self._update(paths, installed)
                    result_state = TargetExtensionState.UPDATED

                if state is TargetExtensionState.ABSENT:
                    self._require_current(paths.final_dump, paths.log("verify"))
                self.profiler.measure(
                    "extension.safe_mode",
                    lambda: self.tools.disable_safe_mode(paths.root / "safe-mode"),
                )
            return LifecycleDecision(
                LifecycleMode.SLOW,
                result_state,
                self.bundle,
                retry_allowed=False,
            )

    def invalidate_marker(self) -> None:
        self.state_store.invalidate()

    def prepare_manual(self) -> LifecycleDecision:
        with self._lock.acquire(timeout_s=self._lock_timeout_s):
            self.state_store.invalidate()
        return LifecycleDecision(
            LifecycleMode.MANUAL,
            TargetExtensionState.USER_MANAGED,
            self.bundle,
            retry_allowed=False,
        )

    def commit_handshake(
        self,
        evidence: tuple[ExtensionHandshakeEvidence, ExtensionHandshakeEvidence],
    ) -> None:
        def commit() -> None:
            self._validated_handshake(evidence)
            manifest = self.bundle.manifest
            state = VerifiedExtensionState(
                infobase_path=self.config.infobase_identity,
                product_id=manifest.product_id,
                cfe_sha256=manifest.cfe_sha256,
                manifest_schema_version=manifest.schema_version,
                artifact_version=manifest.artifact_version,
                protocol_version=manifest.protocol_version,
                identity_sha256=manifest.fingerprints.identity_sha256,
            )
            with self._lock.acquire(timeout_s=self._lock_timeout_s):
                self.state_store.write(state)

        self.profiler.measure("extension.handshake", commit)

    def accept_manual_handshake(
        self,
        evidence: tuple[ExtensionHandshakeEvidence, ExtensionHandshakeEvidence],
    ) -> str:
        def accept() -> str:
            managed, _server = self._validated_handshake(
                evidence,
                allow_artifact_version_mismatch=True,
            )
            return managed.artifact_version

        return self.profiler.measure("extension.handshake", accept)

    def _fast_decision(self) -> LifecycleDecision:
        return LifecycleDecision(
            LifecycleMode.FAST,
            TargetExtensionState.CURRENT,
            self.bundle,
            retry_allowed=True,
        )

    def _new_operation_paths(self) -> _OperationPaths:
        parent = self.config.runtime_dir / "extension-lifecycle"
        parent.mkdir(parents=True, exist_ok=True)
        root = Path(tempfile.mkdtemp(prefix="attempt-", dir=parent))
        root.chmod(0o700)
        return _OperationPaths(
            root=root,
            initial_dump=root / "installed-before",
            final_dump=root / "installed-after",
            rollback_dump=root / "installed-rollback",
            backup_cfe=root / "installed-backup.cfe",
        )

    def _inspect(
        self, destination: Path, log_path: Path
    ) -> tuple[TargetExtensionState, ExtensionFingerprints | None]:
        def inspect() -> ExtensionFingerprints | None:
            try:
                self.tools.dump_files(destination, log_path)
            except ExtensionNotInstalled:
                return None
            return fingerprint_extension_dump(destination, expected_product_id=None)

        installed = self.profiler.measure("extension.inspect", inspect)
        if installed is None:
            return TargetExtensionState.ABSENT, None

        expected = self.bundle.manifest.fingerprints
        if installed == expected:
            return TargetExtensionState.CURRENT, installed
        if (
            installed.identity == expected.identity
            and installed.identity_sha256 == expected.identity_sha256
        ):
            installed_version = _product_version_key(
                installed.artifact.artifact_version
            )
            packaged_version = _product_version_key(expected.artifact.artifact_version)
            if installed_version < packaged_version:
                return TargetExtensionState.OLDER, installed
            raise ExtensionLifecycleError(
                "installed same-identity extension is not a strictly older artifact"
            )
        return TargetExtensionState.FOREIGN, installed

    def _install(self, paths: _OperationPaths) -> None:
        self.profiler.measure(
            "extension.install",
            lambda: self.tools.load_cfe(self.bundle.cfe_path, paths.log("install")),
        )
        self._apply(paths.log("apply"))

    def _update(
        self, paths: _OperationPaths, old_fingerprints: ExtensionFingerprints
    ) -> None:
        backup_ready = False

        def backup_and_load() -> ToolResult | None:
            nonlocal backup_ready
            self.tools.dump_cfe(paths.backup_cfe, paths.log("backup"))
            backup_ready = True
            return self.tools.load_cfe(self.bundle.cfe_path, paths.log("update"))

        try:
            self.profiler.measure("extension.update", backup_and_load)
            self._apply(paths.log("apply"))
            self._require_current(paths.final_dump, paths.log("verify"))
        except BaseException as primary_error:
            if backup_ready:
                try:
                    self._rollback(paths, old_fingerprints)
                except BaseException as rollback_error:  # noqa: BLE001
                    primary_error.add_note(
                        f"Extension rollback failed: {type(rollback_error).__name__}"
                    )
            raise

    def _rollback(
        self, paths: _OperationPaths, old_fingerprints: ExtensionFingerprints
    ) -> None:
        self.profiler.measure(
            "extension.update",
            lambda: self.tools.load_cfe(paths.backup_cfe, paths.log("rollback-load")),
        )
        self._apply(paths.log("rollback-apply"))
        restored = self.profiler.measure(
            "extension.inspect",
            lambda: self._dump_fingerprints(
                paths.rollback_dump, paths.log("rollback-verify")
            ),
        )
        if restored != old_fingerprints:
            raise ExtensionLifecycleError(
                "extension rollback did not restore the exact prior artifact"
            )

    def _apply(self, log_path: Path) -> None:
        self.profiler.measure("extension.apply", lambda: self.tools.apply(log_path))

    def _require_current(self, destination: Path, log_path: Path) -> None:
        state, _ = self._inspect(destination, log_path)
        if state is not TargetExtensionState.CURRENT:
            raise ExtensionLifecycleError(
                "extension mutation did not produce the exact packaged artifact"
            )

    def _dump_fingerprints(
        self, destination: Path, log_path: Path
    ) -> ExtensionFingerprints:
        self.tools.dump_files(destination, log_path)
        return fingerprint_extension_dump(destination)

    def _validated_handshake(
        self,
        evidence: tuple[ExtensionHandshakeEvidence, ExtensionHandshakeEvidence],
        *,
        allow_artifact_version_mismatch: bool = False,
    ) -> tuple[ExtensionHandshakeEvidence, ExtensionHandshakeEvidence]:
        if not isinstance(evidence, tuple) or len(evidence) != 2:
            raise ExtensionLifecycleError(
                "extension handshake requires exactly two evidence records"
            )
        if any(not isinstance(item, ExtensionHandshakeEvidence) for item in evidence):
            raise ExtensionLifecycleError("extension handshake evidence is malformed")
        by_target = {item.target_type: item for item in evidence}
        if len(by_target) != len(evidence):
            raise ExtensionLifecycleError(
                "extension handshake contains duplicate target records"
            )
        server_target_type = self.config.server_target_type
        if set(by_target) != {"ManagedClient", server_target_type}:
            raise ExtensionLifecycleError(
                "extension handshake requires one managed and one server record"
            )

        managed = by_target["ManagedClient"]
        server = by_target[server_target_type]
        manifest = self.bundle.manifest
        expected_locations = {
            "ManagedClient": manifest.breakpoints.managed,
            server_target_type: manifest.breakpoints.server_entry,
        }
        if any(
            item.location != expected_locations[item.target_type]
            for item in (managed, server)
        ):
            raise ExtensionLifecycleError(
                "extension handshake does not match required breakpoint locations"
            )
        if (
            managed.product_id != server.product_id
            or managed.artifact_version != server.artifact_version
            or managed.protocol_version != server.protocol_version
        ):
            raise ExtensionLifecycleError(
                "extension client/server handshake values disagree"
            )
        if (
            managed.product_id != manifest.product_id
            or managed.protocol_version != manifest.protocol_version
            or (
                not allow_artifact_version_mismatch
                and managed.artifact_version != manifest.artifact_version
            )
        ):
            raise ExtensionLifecycleError(
                "extension handshake does not match the packaged manifest"
            )
        return managed, server

__all__ = [
    "ExtensionLifecycle",
    "ExtensionToolOperations",
    "LifecycleDecision",
    "LifecycleMode",
    "TargetExtensionState",
]
