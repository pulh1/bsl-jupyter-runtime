from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from time import perf_counter_ns
from typing import Any, Callable

from onec_runtime.errors import ProtocolError


MAIN_WATERFALL_PHASES = tuple(f"T{index}" for index in range(11))


@dataclass(frozen=True, slots=True)
class WaterfallEvent:
    cell: str
    source_sha256: str
    sequence: int
    phase: str
    segment_ns: int
    elapsed_ns: int

    def as_dict(self) -> dict[str, object]:
        return {
            "cell": self.cell,
            "source_sha256": self.source_sha256,
            "sequence": self.sequence,
            "phase": self.phase,
            "segment_ns": self.segment_ns,
            "elapsed_ns": self.elapsed_ns,
        }


@dataclass(frozen=True, slots=True)
class WaterfallCellSummary:
    cell: str
    source_sha256: str
    operation_id: int
    total_ns: int
    phase_ns: tuple[tuple[str, int], ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "cell": self.cell,
            "source_sha256": self.source_sha256,
            "operation_id": self.operation_id,
            "total_ns": self.total_ns,
            "phase_ns": dict(self.phase_ns),
        }


class MainCellWaterfall:
    """Value-free timing marks for one real warm MAIN cell at a time."""

    def __init__(self, *, clock_ns: Callable[[], int] = perf_counter_ns) -> None:
        self._clock_ns = clock_ns
        self._events: list[WaterfallEvent] = []
        self._completed: list[WaterfallCellSummary] = []
        self._active: list[WaterfallEvent] | None = None
        self._cell = ""
        self._source_sha256 = ""
        self._started_ns = 0
        self._last_ns = 0
        self._evaluation_count = 0
        self._operation_id: int | None = None

    @property
    def active(self) -> bool:
        return self._active is not None

    @property
    def events(self) -> tuple[WaterfallEvent, ...]:
        return tuple(self._events)

    @property
    def completed(self) -> tuple[WaterfallCellSummary, ...]:
        return tuple(self._completed)

    def begin_cell(self, cell: str, source_sha256: str) -> None:
        if self.active:
            raise ProtocolError("A MAIN waterfall cell is already active")
        if not cell or len(source_sha256) != 64:
            raise ValueError("Waterfall cell identity is invalid")
        now = self._clock_ns()
        self._active = []
        self._cell = cell
        self._source_sha256 = source_sha256
        self._started_ns = now
        self._last_ns = now
        self._evaluation_count = 0
        self._operation_id = None
        self._append("T0", now)

    def lowerer_returned(self) -> None:
        self._mark_if_active("T1")

    def breakpoints_returned(self) -> None:
        self._mark_if_active("T2")

    def modify_returned(self) -> None:
        if not self.active:
            return
        phase = self._next_phase()
        if phase not in {"T3", "T4"}:
            raise ProtocolError(f"Unexpected modifyValue at waterfall {phase}")
        self._mark(phase)

    def continue_returned(self) -> None:
        self._mark_if_active("T5")

    def stop_returned(self) -> None:
        self._mark_if_active("T6")

    def evaluation_returned(self) -> None:
        if not self.active:
            return
        self._evaluation_count += 1
        if self._evaluation_count == 3:
            self._mark("T7")

    def reply_returned(self, *, operation_id: int) -> None:
        if not self.active:
            return
        if self._next_phase() != "T8":
            raise ProtocolError("MAIN reply arrived before the three completion reads")
        self._operation_id = operation_id
        self._mark("T8")
        self._mark("T9")

    def mime_created(self) -> None:
        if not self.active:
            return
        self._mark("T10")
        assert self._active is not None
        assert self._operation_id is not None
        total_ns = self._active[-1].elapsed_ns
        self._completed.append(
            WaterfallCellSummary(
                self._cell,
                self._source_sha256,
                self._operation_id,
                total_ns,
                tuple((event.phase, event.segment_ns) for event in self._active),
            )
        )
        self._active = None

    def _mark_if_active(self, phase: str) -> None:
        if self.active:
            self._mark(phase)

    def _next_phase(self) -> str:
        assert self._active is not None
        return MAIN_WATERFALL_PHASES[len(self._active)]

    def _mark(self, phase: str) -> None:
        if self._next_phase() != phase:
            raise ProtocolError(
                f"MAIN waterfall expected {self._next_phase()}, received {phase}"
            )
        self._append(phase, self._clock_ns())

    def _append(self, phase: str, now: int) -> None:
        assert self._active is not None
        event = WaterfallEvent(
            self._cell,
            self._source_sha256,
            len(self._active),
            phase,
            now - self._last_ns,
            now - self._started_ns,
        )
        self._active.append(event)
        self._events.append(event)
        self._last_ns = now


class WaterfallLowerer:
    def __init__(self, lowerer: object, trace: MainCellWaterfall) -> None:
        self._lowerer = lowerer
        self._trace = trace

    def __getattr__(self, name: str) -> Any:
        return getattr(self._lowerer, name)

    def lower(self, *args: object, **kwargs: object) -> object:
        result = self._lowerer.lower(*args, **kwargs)
        self._trace.lowerer_returned()
        return result

    def lower_mapped(self, *args: object, **kwargs: object) -> object:
        result = self._lowerer.lower_mapped(*args, **kwargs)
        self._trace.lowerer_returned()
        return result


class WaterfallSession:
    def __init__(self, session: object, trace: MainCellWaterfall) -> None:
        object.__setattr__(self, "_session", session)
        object.__setattr__(self, "_trace", trace)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    def __setattr__(self, name: str, value: object) -> None:
        if name in {"_session", "_trace"}:
            object.__setattr__(self, name, value)
        else:
            setattr(self._session, name, value)

    def set_breakpoints(self, *args: object, **kwargs: object) -> object:
        result = self._session.set_breakpoints(*args, **kwargs)
        self._trace.breakpoints_returned()
        return result

    def modify(self, *args: object, **kwargs: object) -> object:
        result = self._session.modify(*args, **kwargs)
        self._trace.modify_returned()
        return result

    def continue_(self, *args: object, **kwargs: object) -> object:
        result = self._session.continue_(*args, **kwargs)
        self._trace.continue_returned()
        return result

    def wait_for_any_stop(self, *args: object, **kwargs: object) -> object:
        result = self._session.wait_for_any_stop(*args, **kwargs)
        self._trace.stop_returned()
        return result

    def evaluate(self, *args: object, **kwargs: object) -> object:
        result = self._session.evaluate(*args, **kwargs)
        self._trace.evaluation_returned()
        return result


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ProtocolError(f"Invalid waterfall JSON: {path.name}") from error
    if not isinstance(value, dict):
        raise ProtocolError(f"Waterfall JSON object required: {path.name}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    try:
        values = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ProtocolError(f"Invalid waterfall JSONL: {path.name}") from error
    if not values or any(not isinstance(value, dict) for value in values):
        raise ProtocolError(f"Waterfall JSONL objects required: {path.name}")
    return values


def verify_main_cell_waterfall_artifact(run_dir: Path) -> dict[str, object]:
    run_dir = run_dir.resolve()
    events = _read_jsonl(run_dir / "waterfall-events.jsonl")
    declared = _read_json(run_dir / "waterfall-summary.json")
    frontend = _read_jsonl(run_dir / "frontend-events.jsonl")
    top = _read_json(run_dir / "summary.json")
    cleanup = top.get("cleanup")
    if top.get("status") != "PASS" or cleanup != {"owned_process_count": 0}:
        raise ProtocolError("Waterfall requires a cleaned-up PASS run")
    cells = declared.get("cells")
    if declared.get("status") != "PASS" or not isinstance(cells, list) or not cells:
        raise ProtocolError("Waterfall summary is invalid")
    if len(events) != len(cells) * len(MAIN_WATERFALL_PHASES):
        raise ProtocolError("Waterfall phase count is invalid")
    frontend_by_phase = {
        row.get("phase"): row for row in frontend if isinstance(row.get("phase"), str)
    }
    for cell_index, cell in enumerate(cells):
        if not isinstance(cell, dict):
            raise ProtocolError("Waterfall cell summary is invalid")
        group = events[
            cell_index * len(MAIN_WATERFALL_PHASES) :
            (cell_index + 1) * len(MAIN_WATERFALL_PHASES)
        ]
        phases = tuple(row.get("phase") for row in group)
        sequences = tuple(row.get("sequence") for row in group)
        if phases != MAIN_WATERFALL_PHASES or sequences != tuple(range(11)):
            raise ProtocolError("Waterfall T0-T10 ordering is invalid")
        label = cell.get("cell")
        source_sha256 = cell.get("source_sha256")
        operation_id = cell.get("operation_id")
        if any(
            row.get("cell") != label or row.get("source_sha256") != source_sha256
            for row in group
        ):
            raise ProtocolError("Waterfall cell identity is inconsistent")
        segments = [row.get("segment_ns") for row in group]
        elapsed = [row.get("elapsed_ns") for row in group]
        if any(type(value) is not int or value < 0 for value in segments + elapsed):
            raise ProtocolError("Waterfall durations are invalid")
        assert all(type(value) is int for value in segments + elapsed)
        if elapsed[-1] != sum(segments) or cell.get("total_ns") != elapsed[-1]:
            raise ProtocolError("Waterfall total is inconsistent")
        phase_ns = cell.get("phase_ns")
        if phase_ns != dict(zip(MAIN_WATERFALL_PHASES, segments, strict=True)):
            raise ProtocolError("Waterfall phase summary is inconsistent")
        front = frontend_by_phase.get(label)
        payload = front.get("payload") if isinstance(front, dict) else None
        if (
            not isinstance(front, dict)
            or front.get("source_sha256") != source_sha256
            or not isinstance(payload, dict)
            or payload.get("kind") != "main_completed"
            or payload.get("operation_id") != operation_id
        ):
            raise ProtocolError("Waterfall cell is not bound to its MAIN frontend reply")
    return {
        "status": "PASS",
        "cell_count": len(cells),
        "phase_count": len(events),
    }
