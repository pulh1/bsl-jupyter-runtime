from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest
from extension_bundle_support import write_manifest_fixture

from onec_runtime.errors import ExtensionLockTimeout
from onec_runtime.extension_bundle import ExtensionManifest, read_extension_manifest
from onec_runtime.extension_state import (
    ExtensionStateStore,
    InfobaseExtensionLock,
    VerifiedExtensionState,
)


def _manifest(tmp_path: Path, *, cfe_sha256: str = "a" * 64) -> ExtensionManifest:
    return read_extension_manifest(
        write_manifest_fixture(tmp_path, cfe_sha256=cfe_sha256)
    )


def _state(infobase: Path, manifest: ExtensionManifest) -> VerifiedExtensionState:
    return VerifiedExtensionState(
        infobase_path=str(infobase.resolve()),
        product_id=manifest.product_id,
        cfe_sha256=manifest.cfe_sha256,
        manifest_schema_version=manifest.schema_version,
        artifact_version=manifest.artifact_version,
        protocol_version=manifest.protocol_version,
        identity_sha256=manifest.fingerprints.identity_sha256,
    )


def test_marker_is_keyed_by_canonical_infobase_and_written_atomically(
    tmp_path: Path,
) -> None:
    infobase = tmp_path / "parent" / ".." / "base"
    manifest = _manifest(tmp_path)
    store = ExtensionStateStore(tmp_path / ".runtime", infobase)
    state = _state(infobase, manifest)

    store.write(state)

    assert store.read() == state
    assert store.path.name == (
        sha256(str(infobase.resolve()).encode()).hexdigest() + ".json"
    )
    assert not tuple(store.path.parent.glob("*.tmp"))


def test_corrupt_marker_is_unverified(tmp_path: Path) -> None:
    store = ExtensionStateStore(tmp_path / ".runtime", tmp_path / "base")
    store.path.parent.mkdir(parents=True)
    store.path.write_text("{not-json", encoding="utf-8")

    assert store.read() is None


def test_marker_with_unknown_property_is_unverified(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    store = ExtensionStateStore(tmp_path / ".runtime", tmp_path / "base")
    store.write(_state(tmp_path / "base", manifest))
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    payload["username"] = "operator"
    store.path.write_text(json.dumps(payload), encoding="utf-8")

    assert store.read() is None


def test_matches_requires_exact_manifest_binding(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    store = ExtensionStateStore(tmp_path / ".runtime", tmp_path / "base")
    store.write(_state(tmp_path / "base", manifest))

    assert store.matches(manifest)
    assert not store.matches(replace(manifest, cfe_sha256="d" * 64))
    assert not store.matches(replace(manifest, artifact_version="0.2.0"))
    assert not store.matches(replace(manifest, protocol_version="1"))
    assert not store.matches(replace(manifest, product_id="another-product"))
    assert not store.matches(
        replace(
            manifest,
            fingerprints=replace(manifest.fingerprints, identity_sha256="e" * 64),
        )
    )


def test_invalidate_removes_verified_marker(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    store = ExtensionStateStore(tmp_path / ".runtime", tmp_path / "base")
    store.write(_state(tmp_path / "base", manifest))

    store.invalidate()
    store.invalidate()

    assert store.read() is None
    assert not store.path.exists()


def test_different_infobases_use_different_marker_and_lock_keys(
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path / ".runtime"
    first_store = ExtensionStateStore(runtime_dir, tmp_path / "first")
    second_store = ExtensionStateStore(runtime_dir, tmp_path / "second")
    first_lock = InfobaseExtensionLock(runtime_dir, tmp_path / "first")
    second_lock = InfobaseExtensionLock(runtime_dir, tmp_path / "second")

    assert first_store.path != second_store.path
    assert first_lock.path != second_lock.path
    with first_lock.acquire(timeout_s=0.1), second_lock.acquire(timeout_s=0.1):
        pass


def test_replace_failure_preserves_previous_marker_and_removes_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest(tmp_path)
    store = ExtensionStateStore(tmp_path / ".runtime", tmp_path / "base")
    original = _state(tmp_path / "base", manifest)
    store.write(original)
    original_bytes = store.path.read_bytes()

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError(f"cannot replace {source} with {destination}")

    monkeypatch.setattr("onec_runtime.extension_state.os.replace", fail_replace)

    with pytest.raises(OSError, match="cannot replace"):
        store.write(replace(original, protocol_version="1"))

    assert store.path.read_bytes() == original_bytes
    assert not tuple(store.path.parent.glob("*.tmp"))


def test_same_infobase_lock_contends_then_releases_for_reacquire(
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path / ".runtime"
    first_lock = InfobaseExtensionLock(runtime_dir, tmp_path / "base")
    second_lock = InfobaseExtensionLock(runtime_dir, tmp_path / "base")

    with (
        first_lock.acquire(timeout_s=1.0),
        pytest.raises(ExtensionLockTimeout),
        second_lock.acquire(timeout_s=0.01),
    ):
        pass

    with second_lock.acquire(timeout_s=0.1):
        pass


def test_marker_json_contains_only_fast_path_binding_fields(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    store = ExtensionStateStore(tmp_path / ".runtime", tmp_path / "base")

    store.write(_state(tmp_path / "base", manifest))

    payload = json.loads(store.path.read_text(encoding="utf-8"))
    assert set(payload) == {
        "artifact_version",
        "cfe_sha256",
        "identity_sha256",
        "infobase_path",
        "manifest_schema_version",
        "product_id",
        "protocol_version",
        "safe_mode",
    }
    assert "username" not in payload
    assert "password" not in payload
