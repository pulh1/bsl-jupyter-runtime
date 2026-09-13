from __future__ import annotations

import json
from pathlib import Path

import pytest

from onec_runtime.errors import ProtocolError
from integration.support.main_cell_waterfall import (
    MAIN_WATERFALL_PHASES,
    MainCellWaterfall,
    WaterfallLowerer,
    WaterfallSession,
    verify_main_cell_waterfall_artifact,
)


CANONICAL_RUNS = (
    Path(__file__).parents[2]
    / "tests"
    / "fixtures"
    / "waterfall"
    / "run-a",
    Path(__file__).parents[2]
    / "tests"
    / "fixtures"
    / "waterfall"
    / "run-b",
)


class _Clock:
    def __init__(self) -> None:
        self.value = 100

    def __call__(self) -> int:
        self.value += 10
        return self.value


class _Lowerer:
    def lower(self, source: str, **kwargs: object) -> str:
        del source, kwargs
        return "lowered"

    def lower_mapped(self, source: object, **kwargs: object) -> str:
        del source, kwargs
        return "mapped-lowered"


class _Session:
    state = "ready"
    target = object()

    def set_breakpoints(self, locations: tuple[object, ...]) -> None:
        del locations

    def modify(self, variable: str, value: str) -> object:
        del variable, value
        return object()

    def continue_(self) -> None:
        return None

    def wait_for_any_stop(self, *, timeout_s: float) -> object:
        del timeout_s
        return object()

    def evaluate(self, expression: str) -> object:
        del expression
        return object()


def _complete_trace() -> MainCellWaterfall:
    trace = MainCellWaterfall(clock_ns=_Clock())
    session = WaterfallSession(_Session(), trace)
    lowerer = WaterfallLowerer(_Lowerer(), trace)
    trace.begin_cell("main", "a" * 64)
    assert lowerer.lower("visible") == "lowered"
    session.set_breakpoints((object(),))
    session.modify("ТекущаяИнструкция", "x")
    session.modify("ИдентификаторКоманды", "1")
    session.continue_()
    session.wait_for_any_stop(timeout_s=1.0)
    session.evaluate("ЗавершеннаяКоманда")
    session.evaluate("Результат")
    session.evaluate("Ошибка")
    session.evaluate("message-probe")
    trace.reply_returned(operation_id=1)
    trace.mime_created()
    return trace


def test_recorder_emits_exact_value_free_t0_t10_order() -> None:
    trace = _complete_trace()
    events = trace.events
    assert tuple(event.phase for event in events) == MAIN_WATERFALL_PHASES
    assert tuple(event.sequence for event in events) == tuple(range(11))
    assert all(event.segment_ns >= 0 for event in events)
    assert all(event.elapsed_ns >= event.segment_ns for event in events)
    assert events[-1].elapsed_ns == sum(event.segment_ns for event in events)
    encoded = json.dumps([event.as_dict() for event in events], ensure_ascii=False)
    assert "visible" not in encoded
    assert "lowered" not in encoded
    assert "message-probe" not in encoded


def test_session_and_lowerer_are_transparent_outside_profiled_cell() -> None:
    trace = MainCellWaterfall(clock_ns=_Clock())
    session = WaterfallSession(_Session(), trace)
    lowerer = WaterfallLowerer(_Lowerer(), trace)
    assert lowerer.lower("visible") == "lowered"
    assert session.state == "ready"
    assert session.target is not None
    assert session.evaluate("private") is not None
    assert trace.events == ()


def test_mapped_lowering_records_the_same_t1_waterfall_boundary() -> None:
    trace = MainCellWaterfall(clock_ns=_Clock())
    lowerer = WaterfallLowerer(_Lowerer(), trace)
    trace.begin_cell("main", "a" * 64)

    assert lowerer.lower_mapped(object()) == "mapped-lowered"

    assert tuple(event.phase for event in trace.events) == ("T0", "T1")


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _artifact(tmp_path: Path) -> Path:
    trace = _complete_trace()
    events = [event.as_dict() for event in trace.events]
    summary = trace.completed[0].as_dict()
    _write_jsonl(tmp_path / "waterfall-events.jsonl", events)
    _write_json(tmp_path / "waterfall-summary.json", {"status": "PASS", "cells": [summary]})
    _write_jsonl(
        tmp_path / "frontend-events.jsonl",
        [
            {
                "phase": "main",
                "source_sha256": "a" * 64,
                "payload": {"kind": "main_completed", "operation_id": 1},
            }
        ],
    )
    _write_json(tmp_path / "summary.json", {"status": "PASS", "cleanup": {"owned_process_count": 0}})
    return tmp_path


def test_verifier_binds_events_to_frontend_operation_and_cleanup(tmp_path: Path) -> None:
    result = verify_main_cell_waterfall_artifact(_artifact(tmp_path))
    assert result == {"status": "PASS", "cell_count": 1, "phase_count": 11}


@pytest.mark.parametrize("run_dir", CANONICAL_RUNS)
def test_canonical_zup_waterfall_artifacts_pass(run_dir: Path) -> None:
    assert verify_main_cell_waterfall_artifact(run_dir) == {
        "status": "PASS",
        "cell_count": 2,
        "phase_count": 22,
    }


@pytest.mark.parametrize(
    "mutation",
    ("phase", "negative", "source", "operation", "cleanup", "total"),
)
def test_verifier_rejects_mutated_claims(tmp_path: Path, mutation: str) -> None:
    run_dir = _artifact(tmp_path)
    events = [json.loads(line) for line in (run_dir / "waterfall-events.jsonl").read_text(encoding="utf-8").splitlines()]
    summary = json.loads((run_dir / "waterfall-summary.json").read_text(encoding="utf-8"))
    frontend = [json.loads(line) for line in (run_dir / "frontend-events.jsonl").read_text(encoding="utf-8").splitlines()]
    top = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    if mutation == "phase":
        events[4]["phase"] = "T4_wrong"
    elif mutation == "negative":
        events[5]["segment_ns"] = -1
    elif mutation == "source":
        frontend[0]["source_sha256"] = "b" * 64
    elif mutation == "operation":
        frontend[0]["payload"]["operation_id"] = 2
    elif mutation == "cleanup":
        top["cleanup"]["owned_process_count"] = 1
    elif mutation == "total":
        summary["cells"][0]["total_ns"] += 1
    _write_jsonl(run_dir / "waterfall-events.jsonl", events)
    _write_json(run_dir / "waterfall-summary.json", summary)
    _write_jsonl(run_dir / "frontend-events.jsonl", frontend)
    _write_json(run_dir / "summary.json", top)
    with pytest.raises(ProtocolError):
        verify_main_cell_waterfall_artifact(run_dir)
