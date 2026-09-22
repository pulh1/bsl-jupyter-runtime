"""Bounded CAPTURE admission, fence, and transfer-plan data contracts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from onec_runtime.errors import (
    CaptureValueAccessDeniedError,
    CaptureValueCheckError,
    ValueMaterializationError,
)


MAX_ADMISSION_ENVELOPE_BYTES = 192
MAX_ADMISSION_GENERATION = 2**63 - 1


@dataclass(frozen=True, slots=True)
class AdmissionEnvelopeV1:
    """The bounded admission result carried by the current extension protocol."""

    runtime_generation: int
    context_generation: int
    payload_bytes: int
    payload_sha256: str
    base64_chars: int

    def __post_init__(self) -> None:
        for name in ("runtime_generation", "context_generation"):
            value = getattr(self, name)
            if type(value) is not int or value < 1 or value > MAX_ADMISSION_GENERATION:
                raise ValueError(f"{name} must be a positive signed-64-bit integer")
        for name in ("payload_bytes", "base64_chars"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if (
            not isinstance(self.payload_sha256, str)
            or len(self.payload_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.payload_sha256)
        ):
            raise ValueError("payload_sha256 must be 64 lowercase hexadecimal characters")

    def encode(self) -> str:
        encoded = (
            f"R|{self.runtime_generation}|{self.context_generation}|"
            f"{self.payload_bytes}|{self.payload_sha256}|{self.base64_chars}"
        )
        if len(encoded.encode("ascii")) > MAX_ADMISSION_ENVELOPE_BYTES:
            raise ValueError("admission envelope exceeds its byte budget")
        return encoded

    @staticmethod
    def denied() -> str:
        return "D|worker_generation_value"

    @staticmethod
    def failed() -> str:
        return "E|value_admission_failed"

    @classmethod
    def parse(
        cls,
        encoded: object,
        *,
        max_payload_bytes: int,
        max_base64_chars: int,
    ) -> AdmissionEnvelopeV1:
        for value, name in (
            (max_payload_bytes, "max_payload_bytes"),
            (max_base64_chars, "max_base64_chars"),
        ):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if (
            not isinstance(encoded, str)
            or len(encoded) > MAX_ADMISSION_ENVELOPE_BYTES
            or any(ord(character) > 0x7F for character in encoded)
        ):
            raise CaptureValueCheckError("CAPTURE value admission result is invalid")
        if encoded == cls.denied():
            raise CaptureValueAccessDeniedError(
                "Worker generation objects are not public values"
            )
        if encoded == cls.failed():
            raise ValueMaterializationError("1C value materialization failed")
        fields = encoded.split("|")
        if (
            len(fields) != 6
            or fields[0] != "R"
            or any(not field or any(character not in "0123456789" for character in field)
                   for field in (fields[1], fields[2], fields[3], fields[5]))
            or len(fields[4]) != 64
            or any(character not in "0123456789abcdef" for character in fields[4])
        ):
            raise CaptureValueCheckError("CAPTURE value admission result is invalid")
        runtime_generation = int(fields[1])
        context_generation = int(fields[2])
        payload_bytes = int(fields[3])
        base64_chars = int(fields[5])
        if (
            runtime_generation < 1
            or runtime_generation > MAX_ADMISSION_GENERATION
            or context_generation < 1
            or context_generation > MAX_ADMISSION_GENERATION
            or payload_bytes < 1
            or payload_bytes > max_payload_bytes
            or base64_chars < 1
            or base64_chars > max_base64_chars
        ):
            raise CaptureValueCheckError("CAPTURE value admission result is invalid")
        return cls(
            runtime_generation=runtime_generation,
            context_generation=context_generation,
            payload_bytes=payload_bytes,
            payload_sha256=fields[4],
            base64_chars=base64_chars,
        )


@dataclass(frozen=True, slots=True)
class CaptureFence:
    operation_id: int
    capture_generation: int
    stop_sequence: int
    # The owner may bind runtime/target identity without publishing it.
    identity: object = field(default=None, repr=False)

    def __post_init__(self) -> None:
        for name in ("operation_id", "capture_generation", "stop_sequence"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")


def _admit_transfer_metadata(metadata: object) -> object:
    return metadata


@dataclass(frozen=True, slots=True)
class CaptureTransferPlan:
    """All temporary ownership is known before the creating instruction runs."""

    instruction: str = field(repr=False)
    private_key: str = field(repr=False)
    cleanup_instruction: str = field(repr=False)
    max_text_size: int
    decode: Callable[[object, str], bytes] = field(repr=False)
    admit_metadata: Callable[[object], object] = field(
        default=_admit_transfer_metadata,
        repr=False,
    )
