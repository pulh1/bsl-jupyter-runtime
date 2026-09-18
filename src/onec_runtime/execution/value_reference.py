"""Validate direct public Context references before a transfer plan is built."""

from __future__ import annotations

from onec_runtime.errors import ProtocolError
from onec_runtime.value_transfer_backend import validate_value_handle


_PRIVATE_WORKER_ROOTS = (
    "Контекст.RuntimeWorkerActiveGeneration".casefold(),
    "Контекст.RuntimeWorkerPinnedOperationGeneration".casefold(),
)


def validate_public_direct_handle(handle: str) -> str:
    """Accept a direct Context path while hiding internal Worker generations."""

    if isinstance(handle, str) and handle.casefold().startswith(_PRIVATE_WORKER_ROOTS):
        raise ProtocolError("Worker generation objects are not public values")
    return validate_value_handle(handle)
