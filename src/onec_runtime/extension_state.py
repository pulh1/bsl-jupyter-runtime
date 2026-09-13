from __future__ import annotations

import errno
import json
import math
import os
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from typing import BinaryIO

from onec_runtime.errors import ExtensionLockTimeout
from onec_runtime.extension_bundle import ExtensionManifest

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

_STATE_FIELDS = {
    "infobase_path",
    "product_id",
    "cfe_sha256",
    "manifest_schema_version",
    "artifact_version",
    "protocol_version",
    "identity_sha256",
    "safe_mode",
}


@dataclass(frozen=True, slots=True)
class VerifiedExtensionState:
    infobase_path: str
    product_id: str
    cfe_sha256: str
    manifest_schema_version: int
    artifact_version: str
    protocol_version: str
    identity_sha256: str
    safe_mode: bool = False


def _canonical_infobase(infobase_dir: Path | str) -> str:
    # Strings are already canonical connection identities from RuntimeConfig.
    # Keep Path callers and existing file cache keys backward compatible.
    if isinstance(infobase_dir, str):
        return infobase_dir
    return str(infobase_dir.resolve())


def _infobase_key(canonical_infobase: str) -> str:
    return sha256(canonical_infobase.encode()).hexdigest()


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_hash(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _decode_state(
    payload: object, *, canonical_infobase: str
) -> VerifiedExtensionState | None:
    if not isinstance(payload, dict) or set(payload) != _STATE_FIELDS:
        return None
    if payload["safe_mode"] is not False:
        return None
    if not _is_int(payload["manifest_schema_version"]):
        return None
    if payload["infobase_path"] != canonical_infobase:
        return None
    for field in ("product_id", "artifact_version", "protocol_version"):
        value = payload[field]
        if not isinstance(value, str) or not value:
            return None
    for field in ("cfe_sha256", "identity_sha256"):
        if not _is_hash(payload[field]):
            return None
    return VerifiedExtensionState(
        infobase_path=payload["infobase_path"],
        product_id=payload["product_id"],
        cfe_sha256=payload["cfe_sha256"],
        manifest_schema_version=payload["manifest_schema_version"],
        artifact_version=payload["artifact_version"],
        protocol_version=payload["protocol_version"],
        identity_sha256=payload["identity_sha256"],
        safe_mode=False,
    )


class ExtensionStateStore:
    def __init__(self, runtime_dir: Path, infobase_dir: Path | str) -> None:
        self._canonical_infobase = _canonical_infobase(infobase_dir)
        self.path = runtime_dir / f"{_infobase_key(self._canonical_infobase)}.json"

    def read(self) -> VerifiedExtensionState | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return _decode_state(payload, canonical_infobase=self._canonical_infobase)

    def matches(self, manifest: ExtensionManifest) -> bool:
        state = self.read()
        if state is None:
            return False
        return bool(
            state.product_id == manifest.product_id
            and state.cfe_sha256 == manifest.cfe_sha256
            and state.manifest_schema_version == manifest.schema_version
            and state.artifact_version == manifest.artifact_version
            and state.protocol_version == manifest.protocol_version
            and state.identity_sha256 == manifest.fingerprints.identity_sha256
        )

    def write(self, state: VerifiedExtensionState) -> None:
        payload = asdict(state)
        if _decode_state(payload, canonical_infobase=self._canonical_infobase) != state:
            raise ValueError("extension state is invalid for this infobase")

        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.stem}-",
            suffix=".tmp",
            dir=self.path.parent,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
        finally:
            temporary_path.unlink(missing_ok=True)

    def invalidate(self) -> None:
        self.path.unlink(missing_ok=True)


class InfobaseExtensionLock:
    def __init__(self, runtime_dir: Path, infobase_dir: Path | str) -> None:
        canonical_infobase = _canonical_infobase(infobase_dir)
        self.path = runtime_dir / f"{_infobase_key(canonical_infobase)}.lock"

    def acquire(self, *, timeout_s: float) -> AbstractContextManager[None]:
        if not math.isfinite(timeout_s) or timeout_s < 0:
            raise ValueError("timeout_s must be finite and non-negative")
        return self._acquire(timeout_s=timeout_s)

    @contextmanager
    def _acquire(self, *, timeout_s: float) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+b") as handle:
            self._ensure_lock_byte(handle)
            deadline = time.monotonic() + timeout_s
            while not self._try_lock(handle):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ExtensionLockTimeout(
                        f"timed out acquiring extension lifecycle lock: {self.path}"
                    )
                time.sleep(min(0.01, remaining))
            try:
                yield None
            finally:
                self._unlock(handle)

    @staticmethod
    def _ensure_lock_byte(handle: BinaryIO) -> None:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _try_lock(handle: BinaryIO) -> bool:
        handle.seek(0)
        if sys.platform == "win32":
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    return False
                raise
        else:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno in {errno.EACCES, errno.EAGAIN}:
                    return False
                raise
        return True

    @staticmethod
    def _unlock(handle: BinaryIO) -> None:
        handle.seek(0)
        if sys.platform == "win32":
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


__all__ = [
    "ExtensionStateStore",
    "InfobaseExtensionLock",
    "VerifiedExtensionState",
]
