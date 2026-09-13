"""Single-owner, full-replacement RDBG breakpoint workspace."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from threading import RLock
from typing import Protocol

from onec_runtime.errors import ProtocolError
from onec_runtime.rdbg.models import ModuleLocation


class BreakpointWorkspaceOutcomeUnknown(ProtocolError):
    """The debugger may have applied a workspace whose receipt was not observed."""


class _BreakpointSession(Protocol):
    def set_breakpoints(self, locations: tuple[ModuleLocation, ...]) -> None: ...


def _location_key(location: ModuleLocation) -> tuple[object, ...]:
    return (
        location.module_type,
        location.url,
        location.object_id.hex,
        location.property_id.hex,
        location.line,
        location.extension_name,
        location.ext_id,
    )


def _location_payload(location: ModuleLocation) -> dict[str, object]:
    return {
        "module_type": location.module_type,
        "url": location.url,
        "object_id": str(location.object_id),
        "property_id": str(location.property_id),
        "line": location.line,
        "extension_name": location.extension_name,
        "ext_id": location.ext_id,
    }


def _validate_locations(
    value: tuple[ModuleLocation, ...],
    group: str,
) -> None:
    if (
        type(value) is not tuple
        or any(type(location) is not ModuleLocation for location in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError(f"{group} breakpoint locations are invalid")


@dataclass(frozen=True, slots=True, repr=False)
class WorkspaceSnapshot:
    version: int
    service: ModuleLocation
    captures: tuple[ModuleLocation, ...]
    ordinary_users: tuple[ModuleLocation, ...]
    worker_slots: tuple[ModuleLocation, ...]
    shielded: bool

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version < 0:
            raise ValueError("breakpoint workspace version is invalid")
        if type(self.service) is not ModuleLocation:
            raise ValueError("breakpoint workspace service location is invalid")
        _validate_locations(self.captures, "capture")
        _validate_locations(self.ordinary_users, "ordinary user")
        _validate_locations(self.worker_slots, "Worker")
        if type(self.shielded) is not bool:
            raise ValueError("breakpoint workspace shield is invalid")
        groups = (
            {self.service},
            set(self.captures),
            set(self.ordinary_users),
            set(self.worker_slots),
        )
        for left_index, left in enumerate(groups):
            for right in groups[left_index + 1 :]:
                if left & right:
                    raise ProtocolError("Breakpoint workspace groups overlap")

    @property
    def effective_locations(self) -> tuple[ModuleLocation, ...]:
        return (
            (self.service,)
            + (() if self.shielded else self.captures)
            + self.ordinary_users
            + self.worker_slots
        )

    @property
    def digest(self) -> str:
        payload = json.dumps(
            [_location_payload(item) for item in self.effective_locations],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return sha256(payload).hexdigest()

    def __repr__(self) -> str:
        return (
            "WorkspaceSnapshot("
            f"version={self.version}, service=<redacted>, "
            f"captures={len(self.captures)}, "
            f"ordinary_users={len(self.ordinary_users)}, "
            f"worker_slots={len(self.worker_slots)}, "
            f"shielded={self.shielded}, digest={self.digest!r})"
        )


@dataclass(frozen=True, slots=True)
class WorkspaceInstallReceipt:
    version: int
    requested_digest: str

    def __post_init__(self) -> None:
        if (
            type(self.version) is not int
            or self.version < 0
            or type(self.requested_digest) is not str
            or len(self.requested_digest) != 64
        ):
            raise ValueError("breakpoint workspace receipt is invalid")


class BreakpointWorkspaceController:
    """Serialize complete debugger workspace replacements and their evidence."""

    __slots__ = (
        "_confirmed",
        "_lock",
        "_prepared",
        "_session",
        "_unknown",
    )

    def __init__(
        self,
        session: _BreakpointSession,
        initial: WorkspaceSnapshot,
    ) -> None:
        if type(initial) is not WorkspaceSnapshot:
            raise TypeError("initial breakpoint workspace is required")
        self._session = session
        self._confirmed = initial
        self._unknown = False
        self._prepared: dict[int, WorkspaceSnapshot] = {}
        self._lock = RLock()

    @property
    def confirmed_snapshot(self) -> WorkspaceSnapshot:
        with self._lock:
            return self._confirmed

    def prepare(
        self,
        *,
        captures: tuple[ModuleLocation, ...],
        ordinary_users: tuple[ModuleLocation, ...],
        worker_slots: tuple[ModuleLocation, ...],
        shielded: bool,
    ) -> WorkspaceSnapshot:
        if (
            type(worker_slots) is not tuple
            or any(type(item) is not ModuleLocation for item in worker_slots)
        ):
            raise ValueError("Worker breakpoint locations are invalid")
        unique_worker_slots = tuple(sorted(set(worker_slots), key=_location_key))
        with self._lock:
            self.require_confirmed()
            snapshot = WorkspaceSnapshot(
                self._confirmed.version + 1,
                self._confirmed.service,
                captures,
                ordinary_users,
                unique_worker_slots,
                shielded,
            )
            self._prepared[id(snapshot)] = snapshot
            return snapshot

    def install(self, snapshot: WorkspaceSnapshot) -> WorkspaceInstallReceipt:
        if type(snapshot) is not WorkspaceSnapshot:
            raise TypeError("breakpoint workspace snapshot is required")
        with self._lock:
            self.require_confirmed()
            if (
                self._prepared.get(id(snapshot)) is not snapshot
                or snapshot.version != self._confirmed.version + 1
            ):
                raise ProtocolError("Breakpoint workspace proposal is stale or foreign")
            del self._prepared[id(snapshot)]
            if snapshot.effective_locations != self._confirmed.effective_locations:
                setter = getattr(self._session, "set_breakpoints", None)
                if not callable(setter):
                    raise ProtocolError("Breakpoint workspace session is unavailable")
                try:
                    setter(snapshot.effective_locations)
                except BaseException as error:
                    self._unknown = True
                    self._prepared.clear()
                    raise BreakpointWorkspaceOutcomeUnknown(
                        "Worker breakpoint workspace outcome is unknown"
                    ) from error
            self._confirmed = snapshot
            self._prepared = {
                key: value
                for key, value in self._prepared.items()
                if value.version > snapshot.version
            }
            return WorkspaceInstallReceipt(snapshot.version, snapshot.digest)

    def require_confirmed(self) -> None:
        with self._lock:
            if self._unknown:
                raise BreakpointWorkspaceOutcomeUnknown(
                    "Worker breakpoint workspace outcome is unknown"
                )

    def quarantine(self) -> None:
        with self._lock:
            self._unknown = True
            self._prepared.clear()

    def _adopt_confirmed_session(
        self,
        session: _BreakpointSession,
        effective_locations: tuple[ModuleLocation, ...],
    ) -> None:
        """Bind a recovered session after it replayed the exact confirmed workspace."""
        with self._lock:
            if not callable(getattr(session, "set_breakpoints", None)):
                raise TypeError("breakpoint workspace session is invalid")
            if (
                type(effective_locations) is not tuple
                or effective_locations != self._confirmed.effective_locations
            ):
                raise ProtocolError("Recovered breakpoint workspace does not match")
            self._session = session
            self._unknown = False
            self._prepared.clear()
