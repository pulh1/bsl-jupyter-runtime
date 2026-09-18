"""Immutable identity and evidence for one CAPTURE continuation attempt."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ContinuationAttemptSpec:
    """Opaque identity binding one continuation to its exact ordered roots."""

    attempt_id: str
    capture_generation: int
    request_operation_id: str
    dirty_roots: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.attempt_id, str) or not self.attempt_id or len(self.attempt_id) > 256:
            raise ValueError("continuation attempt_id is invalid")
        if type(self.capture_generation) is not int or self.capture_generation <= 0:
            raise ValueError("continuation capture_generation must be positive")
        if (
            not isinstance(self.request_operation_id, str)
            or not self.request_operation_id
            or len(self.request_operation_id) > 256
        ):
            raise ValueError("continuation request_operation_id is invalid")
        roots = tuple(self.dirty_roots)
        if len(roots) > 100:
            raise ValueError("continuation dirty_roots exceeds 100")
        if any(
            not isinstance(root, str)
            or not root
            or len(root) > 256
            or not root.isidentifier()
            for root in roots
        ):
            raise ValueError("continuation dirty_roots are invalid")
        if len({root.casefold() for root in roots}) != len(roots):
            raise ValueError("continuation dirty_roots must be unique")
        object.__setattr__(self, "dirty_roots", roots)


@dataclass(frozen=True, slots=True)
class ContinuationAttemptEvidence:
    root_statuses: tuple[tuple[str, str], ...]
    continue_state: str
