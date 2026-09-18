"""Bounded transfer request for a controller-owned CAPTURE table descriptor.

Only the controller resolves the opaque handle. It calls ``prepare`` inside
the admitted arbiter ticket, after checking the exact stopped scope. The
existing compact transfer builder then supplies admission, integrity, and
private-key cleanup policies without accepting an expression from the caller.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from re import fullmatch

from onec_runtime.capture_evaluation import CaptureTransferPlan
from onec_runtime.compact_table_backend import CompactRuntimeTableTransfer
from onec_runtime.errors import ProtocolError
from onec_runtime.execution.capture.manager_metadata import (
    CaptureSelectedTableDescriptor,
)
from onec_runtime.execution.capture.scope import CaptureScope
from onec_runtime.table_materialization import ReferencePolicy


def _no_direct_rdbg(*_args: object) -> object:
    raise ProtocolError("CAPTURE selected table requires its controller ticket")


@dataclass(frozen=True, slots=True)
class CaptureSelectedTableTransferRequest:
    handle: str
    scope: CaptureScope
    policy: ReferencePolicy
    max_rows: int
    max_bytes: int
    runtime_generation: int
    context_generation: int
    worker_registrations: tuple[str, ...]
    relative_offset: int = 0
    relative_limit: int | None = None

    def __post_init__(self) -> None:
        if type(self.handle) is not str or fullmatch(r"capture_table_[0-9a-f]{32}", self.handle) is None:
            raise ProtocolError("CAPTURE selected table handle is invalid")
        if not isinstance(self.scope, CaptureScope):
            raise TypeError("CAPTURE selected table scope is required")
        if not isinstance(self.policy, ReferencePolicy):
            raise TypeError("CAPTURE selected table reference policy is invalid")
        if type(self.max_rows) is not int or not 1 <= self.max_rows <= 100_000:
            raise ProtocolError("CAPTURE selected table row budget is invalid")
        if type(self.max_bytes) is not int or not 1 <= self.max_bytes <= 64 * 1024 * 1024:
            raise ProtocolError("CAPTURE selected table byte budget is invalid")
        if (
            type(self.runtime_generation) is not int or self.runtime_generation <= 0
            or type(self.context_generation) is not int or self.context_generation <= 0
        ):
            raise ProtocolError("CAPTURE selected table generation is invalid")
        if type(self.worker_registrations) is not tuple or any(
            type(item) is not str or not item for item in self.worker_registrations
        ):
            raise ProtocolError("CAPTURE selected table Worker catalog is invalid")
        if (
            type(self.relative_offset) is not int or self.relative_offset < 0
            or self.relative_limit is not None
            and (type(self.relative_limit) is not int or self.relative_limit <= 0)
        ):
            raise ProtocolError("CAPTURE selected table sub-selection is invalid")

    def prepare(self, descriptor: CaptureSelectedTableDescriptor) -> CaptureTransferPlan:
        """Build the transfer only after the controller resolved this handle."""

        if not isinstance(descriptor, CaptureSelectedTableDescriptor) or descriptor.scope is not self.scope:
            raise ProtocolError("CAPTURE selected table descriptor is stale")
        if self.relative_limit is not None:
            if self.relative_offset >= descriptor.limit:
                raise ProtocolError("CAPTURE selected table sub-selection exceeds selection")
            descriptor = replace(
                descriptor,
                offset=descriptor.offset + self.relative_offset,
                limit=min(self.relative_limit, descriptor.limit - self.relative_offset),
            )
        elif self.relative_offset:
            raise ProtocolError("CAPTURE selected table sub-selection is invalid")
        backend = CompactRuntimeTableTransfer(
            _no_direct_rdbg, _no_direct_rdbg,
            runtime_generation=lambda: self.runtime_generation,
            context_generation=self.context_generation,
            context_cleaner=_no_direct_rdbg,
            worker_type_registrations=lambda: self.worker_registrations,
            max_text_size=((self.max_bytes + 2) // 3) * 4,
            max_payload_bytes=self.max_bytes,
            max_rows=self.max_rows,
        )
        return backend._prepare_trusted_expression_payload(
            descriptor.expression, self.policy,
        )


__all__ = ["CaptureSelectedTableTransferRequest"]
