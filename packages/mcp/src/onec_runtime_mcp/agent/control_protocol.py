"""Strict JSON-only data contracts for the agent's loopback control channel."""

from __future__ import annotations

import json
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID


MAX_PAYLOAD_BYTES = 1_048_576
SCHEMA_VERSION = 1
MAX_CALLER_ID_LENGTH = 128


class ControlProtocolError(ValueError):
    """The local control envelope or endpoint descriptor is invalid."""


@dataclass(frozen=True, slots=True)
class ControlEndpoint:
    host: str
    port: int
    service_instance_id: str
    descriptor_path: Path
    token_path: Path

    def __post_init__(self) -> None:
        if self.host != "127.0.0.1":
            raise ValueError("control server must bind to 127.0.0.1")
        if type(self.port) is not int or not 0 < self.port < 65536:
            raise ValueError("port must be valid")
        if not self.service_instance_id:
            raise ValueError("service_instance_id is required")

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @classmethod
    def read(cls, descriptor_path: str | Path) -> "ControlEndpoint":
        path = Path(descriptor_path)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ControlProtocolError("invalid control descriptor") from error
        if not isinstance(payload, dict) or set(payload) != {"schema_version", "host", "port", "service_instance_id"}:
            raise ControlProtocolError("invalid control descriptor")
        if payload["schema_version"] != SCHEMA_VERSION:
            raise ControlProtocolError("unsupported control schema")
        return cls(payload["host"], payload["port"], payload["service_instance_id"], path, path.with_name("token"))

    def write_descriptor(self) -> None:
        _write_private_json(self.descriptor_path, {"schema_version": SCHEMA_VERSION, "host": self.host, "port": self.port, "service_instance_id": self.service_instance_id})


@dataclass(frozen=True, slots=True)
class ControlCall:
    request_id: str
    caller_id: str
    method: str
    arguments: dict[str, object]


def parse_call(payload: bytes) -> ControlCall:
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ControlProtocolError("payload too large")
    try:
        value = json.loads(payload.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ControlProtocolError("malformed JSON") from error
    if not isinstance(value, dict) or set(value) != {"request_id", "caller_id", "method", "arguments"}:
        raise ControlProtocolError("invalid call envelope")
    request_id = value["request_id"]
    caller_id = value["caller_id"]
    method = value["method"]
    arguments = value["arguments"]
    try:
        UUID(request_id)
    except (TypeError, ValueError) as error:
        raise ControlProtocolError("invalid request_id") from error
    if not isinstance(caller_id, str) or not caller_id or len(caller_id) > MAX_CALLER_ID_LENGTH or not caller_id.isascii() or caller_id.strip() != caller_id:
        raise ControlProtocolError("invalid caller_id")
    if not isinstance(method, str) or not method or len(method) > 128:
        raise ControlProtocolError("invalid method")
    if not isinstance(arguments, dict):
        raise ControlProtocolError("arguments must be object")
    return ControlCall(request_id, caller_id, method, arguments)


def _write_private_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    with suppress(OSError):
        path.chmod(0o600)


def _reject_constant(_constant: str) -> object:
    raise ValueError("non-standard JSON constant")
