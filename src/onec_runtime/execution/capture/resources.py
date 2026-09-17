"""Per-scope temporary resources, separate from debugger frame identity."""

from dataclasses import dataclass, field
from enum import Enum


class TemporaryCleanupState(str, Enum):
    LIVE = "live"
    CONFIRMED_FAILURE = "confirmed_failure"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class TemporaryCleanupDebt:
    """A private key whose idempotent deletion has not been confirmed."""

    key: str = field(repr=False)
    state: TemporaryCleanupState

    @property
    def can_retry_delete(self) -> bool:
        """A known rejection can be retried; an unknown outcome needs proof."""

        return self.state is TemporaryCleanupState.CONFIRMED_FAILURE
