from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from math import isfinite
from uuid import UUID

from onec_runtime.errors import (
    LeaseConflict,
    LeaseExpired,
    StaleLeaseEpoch,
    StaleRuntimeGeneration,
)
from onec_runtime.supervisor_model import LeaseHandle


# Server-owned policy cap; permits the supervised live phase lease of 95 seconds.
MAX_LEASE_TTL_S = 120.0


def validate_lease_ttl(ttl_s: object) -> float:
    if type(ttl_s) is int:
        if ttl_s <= 0 or ttl_s > MAX_LEASE_TTL_S:
            raise ValueError(
                f"ttl_s must be greater than 0 and at most {MAX_LEASE_TTL_S}"
            )
        return float(ttl_s)
    if type(ttl_s) is not float or not isfinite(ttl_s):
        raise ValueError("ttl_s must be an exact finite built-in number")
    if ttl_s <= 0 or ttl_s > MAX_LEASE_TTL_S:
        raise ValueError(
            f"ttl_s must be greater than 0 and at most {MAX_LEASE_TTL_S}"
        )
    return ttl_s


@dataclass(frozen=True, slots=True)
class LeaseStatus:
    owner_id: UUID | None
    lease_epoch: int | None
    expires_in_s: float | None
    expired: bool


class LeaseAuthority:
    """Issues the single mutating-owner lease for one runtime generation."""

    def __init__(self, generation_id: int, *, clock: Callable[[], float]) -> None:
        self._generation_id = generation_id
        self._clock = clock
        self._active: LeaseHandle | None = None
        self._deadline: float | None = None
        self._expired = False
        self._next_epoch = 1
        self._expiry_reported = False

    def grant(self, owner_id: UUID, *, ttl_s: object) -> LeaseHandle:
        validated_ttl_s = validate_lease_ttl(ttl_s)
        if self._active is not None:
            self._raise_if_expired()
            if self._active.owner_id == owner_id:
                return self._active
            raise LeaseConflict("Another owner holds this generation lease")
        if self._expired:
            raise LeaseExpired("The generation lease has expired")

        self._active = LeaseHandle(self._generation_id, owner_id, self._next_epoch)
        self._next_epoch += 1
        self._deadline = self._clock() + validated_ttl_s
        return self._active

    def renew(self, handle: LeaseHandle, *, ttl_s: object) -> LeaseHandle:
        active = self._require_valid_handle(handle)
        validated_ttl_s = validate_lease_ttl(ttl_s)
        self._deadline = self._clock() + validated_ttl_s
        return active

    def validate(self, handle: LeaseHandle) -> LeaseHandle:
        return self._require_valid_handle(handle)

    def expire_if_due(self) -> LeaseHandle | None:
        if self._active is None or self._expiry_reported:
            return None
        if not self._expired:
            deadline = self._deadline
            if deadline is None or self._clock() < deadline:
                return None
            self._expired = True
        self._expiry_reported = True
        return self._active

    def status(self) -> LeaseStatus:
        if self._active is None:
            return LeaseStatus(None, None, None, self._expired)
        deadline = self._deadline
        if deadline is None:
            return LeaseStatus(self._active.owner_id, self._active.lease_epoch, None, True)
        remaining = max(0.0, deadline - self._clock())
        return LeaseStatus(
            self._active.owner_id,
            self._active.lease_epoch,
            remaining,
            self._expired or remaining == 0.0,
        )

    def _require_valid_handle(self, handle: LeaseHandle) -> LeaseHandle:
        if handle.generation_id != self._generation_id:
            raise StaleRuntimeGeneration(
                f"Generation {handle.generation_id} is no longer current"
            )
        active = self._active
        if active is None or handle.owner_id != active.owner_id:
            raise LeaseConflict("The supplied owner does not hold this lease")
        if handle.lease_epoch != active.lease_epoch:
            raise StaleLeaseEpoch("The supplied lease epoch is not current")
        self._raise_if_expired()
        return active

    def _raise_if_expired(self) -> None:
        deadline = self._deadline
        if self._expired or deadline is None or self._clock() >= deadline:
            self._expired = True
            raise LeaseExpired("The generation lease has expired")
