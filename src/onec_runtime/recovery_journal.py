from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Callable

from onec_runtime.privacy import public_artifact_value


AppendJsonl = Callable[[str, object], None]


@dataclass(frozen=True, slots=True)
class JournalEvent:
    sequence: int
    timestamp: str
    stream: str
    event: str
    fields: dict[str, object]

    def as_json(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "event": self.event,
            **self.fields,
        }


class RecoveryJournal:
    def __init__(self, sink: AppendJsonl | None = None) -> None:
        self._sink = sink
        self._lock = Lock()
        # Serialize the complete snapshot -> sink -> retire loop without
        # holding the state lock across user I/O.  ``record`` may therefore be
        # called by another thread (or by the sink itself), while recursive
        # ``flush`` from the sink is deliberately unsupported.
        self._flush_lock = Lock()
        self._events: list[JournalEvent] = []
        self._pending: list[JournalEvent] = []

    @property
    def events(self) -> tuple[JournalEvent, ...]:
        return tuple(self._events)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def record(self, stream: str, event: str, **fields: object) -> JournalEvent:
        normalized = {key: public_artifact_value(value) for key, value in fields.items()}
        with self._lock:
            item = JournalEvent(
                sequence=len(self._events) + 1,
                timestamp=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                stream=stream,
                event=event,
                fields=normalized,
            )
            self._events.append(item)
            self._pending.append(item)
            return item

    def flush(self) -> None:
        with self._flush_lock:
            with self._lock:
                pending = tuple(self._pending)
            if self._sink is None:
                with self._lock:
                    del self._pending[: len(pending)]
                return
            for item in pending:
                self._sink(item.stream, item.as_json())
                with self._lock:
                    # Concurrent records append after this snapshot; retire
                    # only the committed prefix and never a later append.
                    if self._pending and self._pending[0] is item:
                        del self._pending[0]
