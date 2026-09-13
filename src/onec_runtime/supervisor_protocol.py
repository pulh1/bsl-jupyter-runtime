from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from onec_runtime.errors import InvalidMessageSequence


PROTOCOL_VERSION = 1


class MessageKind(str, Enum):
    DEBUG_READY = "debug_ready"
    DEBUGGEE_STARTED = "debuggee_started"
    READY = "ready"
    HEARTBEAT = "heartbeat"
    GRANT_LEASE = "grant_lease"
    RENEW_LEASE = "renew_lease"
    LEASE_GRANTED = "lease_granted"
    LEASE_CONFLICT = "lease_conflict"
    LEASE_EXPIRED = "lease_expired"
    EXECUTE_PHASE = "execute_phase"
    PHASE_REACHED = "phase_reached"
    OPERATION_RESULT = "operation_result"
    STATUS = "status"
    STATUS_RESULT = "status_result"
    SHUTDOWN = "shutdown"
    FATAL = "fatal"


@dataclass(frozen=True, slots=True)
class ControlMessage:
    protocol_version: int
    generation_id: int
    message_sequence: int
    kind: MessageKind
    payload: dict[str, object]


class MessageSender:
    def __init__(self, generation_id: int) -> None:
        self._generation_id = generation_id
        self._message_sequence = 0

    def create(self, kind: MessageKind, **payload: object) -> ControlMessage:
        self._message_sequence += 1
        return ControlMessage(
            protocol_version=PROTOCOL_VERSION,
            generation_id=self._generation_id,
            message_sequence=self._message_sequence,
            kind=kind,
            payload=payload,
        )


class MessageReceiver:
    def __init__(self, generation_id: int) -> None:
        self._generation_id = generation_id
        self._expected_sequence = 1

    def accept(self, message: ControlMessage) -> None:
        if message.protocol_version != PROTOCOL_VERSION:
            raise InvalidMessageSequence(
                "Unsupported protocol version "
                f"{message.protocol_version}; expected {PROTOCOL_VERSION}"
            )
        if message.generation_id != self._generation_id:
            raise InvalidMessageSequence(
                f"Message generation {message.generation_id} does not match "
                f"receiver generation {self._generation_id}"
            )
        if message.message_sequence != self._expected_sequence:
            raise InvalidMessageSequence(
                f"Message sequence {message.message_sequence} is not the "
                f"expected sequence {self._expected_sequence}"
            )
        self._expected_sequence += 1
