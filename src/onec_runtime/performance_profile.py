from __future__ import annotations

from dataclasses import asdict, dataclass
from time import perf_counter_ns, process_time_ns
from typing import Callable, TypeVar


T = TypeVar("T")

PARSER_CALL_COUNTERS = (
    "full_module_parses",
    "delta_method_parses",
    "packaging_validation_parses",
)
_INTERNAL_PARSER_CALL_COUNTERS = (*PARSER_CALL_COUNTERS, "worker_profile_parses")

_PING_PARSE_PHASES = frozenset(
    {
        "rdbg.ping.parse_xml",
        "rdbg.ping.parse_targets",
        "rdbg.ping.parse_stops",
        "rdbg.ping.parse_local_variables",
        "rdbg.ping.parse_evaluations",
    }
)


@dataclass(frozen=True, slots=True)
class PhaseEvent:
    sequence: int
    phase: str
    wall_ns: int
    cpu_ns: int
    input_bytes: int = 0
    output_bytes: int = 0
    item_count: int = 0
    page_start: int | None = None
    result_id: str = ""
    error_present: bool = False

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ParserCallCounters:
    full_module_parses: int = 0
    delta_method_parses: int = 0
    packaging_validation_parses: int = 0
    worker_profile_parses: int = 0

    def as_tuple(self) -> tuple[int, int, int]:
        return (
            self.full_module_parses,
            self.delta_method_parses,
            self.packaging_validation_parses,
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "full_module_parses": self.full_module_parses,
            "delta_method_parses": self.delta_method_parses,
            "packaging_validation_parses": self.packaging_validation_parses,
        }


class PhaseRecorder:
    """Records aggregate timings without retaining runtime values or payloads."""

    def __init__(
        self,
        *,
        wall_clock_ns: Callable[[], int] = perf_counter_ns,
        cpu_clock_ns: Callable[[], int] = process_time_ns,
    ) -> None:
        self.events: list[PhaseEvent] = []
        self._wall_clock_ns = wall_clock_ns
        self._cpu_clock_ns = cpu_clock_ns
        self._parser_calls = {name: 0 for name in _INTERNAL_PARSER_CALL_COUNTERS}

    @property
    def parser_calls(self) -> ParserCallCounters:
        return ParserCallCounters(**self._parser_calls)

    def measure_parser(
        self,
        counter: str,
        operation: Callable[[], T],
    ) -> T:
        self.record_parser_call(counter)
        return self.measure("semantic_parse", operation)

    def record_parser_call(self, counter: str) -> None:
        if counter not in self._parser_calls:
            raise ValueError("parser call counter is invalid")
        self._parser_calls[counter] += 1

    def record_duration(
        self,
        phase: str,
        *,
        wall_ns: int,
        cpu_ns: int = 0,
        input_bytes: int = 0,
        output_bytes: int = 0,
        item_count: int = 0,
        result_id: str = "",
    ) -> None:
        if not isinstance(phase, str) or not phase:
            raise ValueError("phase must not be empty")
        counters = (wall_ns, cpu_ns, input_bytes, output_bytes, item_count)
        if any(type(value) is not int or value < 0 for value in counters):
            raise ValueError("profile counters must be non-negative integers")
        self.events.append(
            PhaseEvent(
                sequence=len(self.events) + 1,
                phase=phase,
                wall_ns=wall_ns,
                cpu_ns=cpu_ns,
                input_bytes=input_bytes,
                output_bytes=output_bytes,
                item_count=item_count,
                result_id=result_id,
            )
        )

    def measure(
        self,
        phase: str,
        operation: Callable[[], T],
        *,
        input_bytes: int = 0,
        page_start: int | None = None,
        result_id: str = "",
        output_bytes: Callable[[T], int] | None = None,
        item_count: Callable[[T], int] | None = None,
    ) -> T:
        if not phase:
            raise ValueError("phase must not be empty")
        if input_bytes < 0 or (page_start is not None and page_start < 0):
            raise ValueError("profile counters must be non-negative")
        wall_started = self._wall_clock_ns()
        cpu_started = self._cpu_clock_ns()
        try:
            result = operation()
        except BaseException:
            self._append(
                phase,
                wall_started,
                cpu_started,
                input_bytes=input_bytes,
                page_start=page_start,
                result_id=result_id,
                error_present=True,
            )
            raise
        measured_output = output_bytes(result) if output_bytes is not None else 0
        measured_items = item_count(result) if item_count is not None else 0
        if measured_output < 0 or measured_items < 0:
            raise ValueError("profile counters must be non-negative")
        self._append(
            phase,
            wall_started,
            cpu_started,
            input_bytes=input_bytes,
            output_bytes=measured_output,
            item_count=measured_items,
            page_start=page_start,
            result_id=result_id,
        )
        return result

    def _append(
        self,
        phase: str,
        wall_started: int,
        cpu_started: int,
        *,
        input_bytes: int = 0,
        output_bytes: int = 0,
        item_count: int = 0,
        page_start: int | None = None,
        result_id: str = "",
        error_present: bool = False,
    ) -> None:
        self.events.append(
            PhaseEvent(
                sequence=len(self.events) + 1,
                phase=phase,
                wall_ns=self._wall_clock_ns() - wall_started,
                cpu_ns=self._cpu_clock_ns() - cpu_started,
                input_bytes=input_bytes,
                output_bytes=output_bytes,
                item_count=item_count,
                page_start=page_start,
                result_id=result_id,
                error_present=error_present,
            )
        )


def summarize_phases(events: list[PhaseEvent]) -> dict[str, object]:
    by_phase: dict[str, dict[str, int]] = {}
    for event in events:
        totals = by_phase.setdefault(
            event.phase,
            {
                "calls": 0,
                "wall_ns": 0,
                "cpu_ns": 0,
                "input_bytes": 0,
                "output_bytes": 0,
                "item_count": 0,
                "error_count": 0,
            },
        )
        totals["calls"] += 1
        totals["wall_ns"] += event.wall_ns
        totals["cpu_ns"] += event.cpu_ns
        totals["input_bytes"] += event.input_bytes
        totals["output_bytes"] += event.output_bytes
        totals["item_count"] += event.item_count
        totals["error_count"] += int(event.error_present)
    ping_response_bytes = by_phase.get("rdbg.ping.request", {}).get(
        "output_bytes", 0
    )
    ping_parser_input_bytes = sum(
        by_phase.get(phase, {}).get("input_bytes", 0)
        for phase in _PING_PARSE_PHASES
    )
    return {
        "event_count": len(events),
        "by_phase": by_phase,
        "derived": {
            "ping_response_bytes": ping_response_bytes,
            "ping_parser_input_bytes": ping_parser_input_bytes,
            "ping_xml_parse_amplification": (
                ping_parser_input_bytes / ping_response_bytes
                if ping_response_bytes
                else 0.0
            ),
            "profiled_wall_ns": sum(event.wall_ns for event in events),
            "profiled_cpu_ns": sum(event.cpu_ns for event in events),
        },
    }
