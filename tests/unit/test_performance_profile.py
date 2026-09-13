import pytest

from onec_runtime.performance_profile import (
    PARSER_CALL_COUNTERS,
    PhaseEvent,
    PhaseRecorder,
    summarize_phases,
)


def test_worker_parser_counter_does_not_change_persisted_legacy_schema() -> None:
    recorder = PhaseRecorder()

    recorder.record_parser_call("worker_profile_parses")

    assert PARSER_CALL_COUNTERS == (
        "full_module_parses",
        "delta_method_parses",
        "packaging_validation_parses",
    )
    assert recorder.parser_calls.worker_profile_parses == 1
    assert recorder.parser_calls.as_tuple() == (0, 0, 0)
    assert recorder.parser_calls.as_dict() == {
        "full_module_parses": 0,
        "delta_method_parses": 0,
        "packaging_validation_parses": 0,
    }


def test_record_duration_appends_privacy_safe_external_phase() -> None:
    recorder = PhaseRecorder()

    recorder.record_duration(
        "root_swap",
        wall_ns=2_000_000,
        cpu_ns=0,
        item_count=1,
    )

    assert recorder.events[-1].as_dict() == {
        "sequence": 1,
        "phase": "root_swap",
        "wall_ns": 2_000_000,
        "cpu_ns": 0,
        "input_bytes": 0,
        "output_bytes": 0,
        "item_count": 1,
        "page_start": None,
        "result_id": "",
        "error_present": False,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("wall_ns", -1),
        ("wall_ns", True),
        ("cpu_ns", 1.5),
        ("input_bytes", False),
        ("output_bytes", "1"),
        ("item_count", -1),
    ),
)
def test_record_duration_rejects_untrusted_external_counters(
    field: str,
    value: object,
) -> None:
    counters: dict[str, object] = {
        "wall_ns": 1,
        "cpu_ns": 0,
        "input_bytes": 0,
        "output_bytes": 0,
        "item_count": 0,
    }
    counters[field] = value

    with pytest.raises(
        ValueError,
        match="profile counters must be non-negative integers",
    ):
        recorder = PhaseRecorder()
        recorder.record_duration("target_phase", **counters)  # type: ignore[arg-type]


def test_record_duration_rejects_empty_phase() -> None:
    with pytest.raises(ValueError, match="phase must not be empty"):
        PhaseRecorder().record_duration("", wall_ns=0)


def test_phase_recorder_measures_wall_cpu_bytes_and_items_without_values() -> None:
    wall_ticks = iter((100, 175))
    cpu_ticks = iter((20, 45))
    recorder = PhaseRecorder(
        wall_clock_ns=lambda: next(wall_ticks),
        cpu_clock_ns=lambda: next(cpu_ticks),
    )

    result = recorder.measure(
        "rdbg.test",
        lambda: b"abc",
        input_bytes=11,
        page_start=2400,
        result_id="11111111-1111-1111-1111-111111111111",
        output_bytes=lambda value: len(value),
        item_count=lambda value: 7,
    )

    assert result == b"abc"
    assert recorder.events[0].as_dict() == {
        "sequence": 1,
        "phase": "rdbg.test",
        "wall_ns": 75,
        "cpu_ns": 25,
        "input_bytes": 11,
        "output_bytes": 3,
        "item_count": 7,
        "page_start": 2400,
        "result_id": "11111111-1111-1111-1111-111111111111",
        "error_present": False,
    }


def test_phase_recorder_keeps_failed_operation_timing_without_error_text() -> None:
    wall_ticks = iter((10, 30))
    cpu_ticks = iter((5, 12))
    recorder = PhaseRecorder(
        wall_clock_ns=lambda: next(wall_ticks),
        cpu_clock_ns=lambda: next(cpu_ticks),
    )

    try:
        recorder.measure("rdbg.failure", lambda: (_ for _ in ()).throw(ValueError("private")))
    except ValueError:
        pass

    event = recorder.events[0].as_dict()
    assert event["error_present"] is True
    assert event["wall_ns"] == 20
    assert event["cpu_ns"] == 7
    assert "private" not in str(event)
    assert set(event) == {
        "sequence",
        "phase",
        "wall_ns",
        "cpu_ns",
        "input_bytes",
        "output_bytes",
        "item_count",
        "page_start",
        "result_id",
        "error_present",
    }


def test_phase_summary_exposes_xml_parse_amplification_and_exact_totals() -> None:
    events = [
        PhaseEvent(1, "rdbg.ping.request", 100, 10, output_bytes=1_000),
        PhaseEvent(2, "rdbg.ping.parse_targets", 20, 20, input_bytes=1_000),
        PhaseEvent(3, "rdbg.ping.parse_stops", 30, 30, input_bytes=1_000),
        PhaseEvent(4, "rdbg.ping.parse_local_variables", 40, 40, input_bytes=1_000),
        PhaseEvent(
            5,
            "rdbg.ping.parse_evaluations",
            50,
            50,
            input_bytes=1_000,
            item_count=1,
        ),
        PhaseEvent(6, "dataframe.convert_page", 60, 60, item_count=36_000),
        PhaseEvent(7, "dataframe.build", 70, 70, item_count=2_400),
    ]

    summary = summarize_phases(events)

    assert summary["event_count"] == 7
    assert summary["by_phase"]["rdbg.ping.parse_evaluations"] == {
        "calls": 1,
        "wall_ns": 50,
        "cpu_ns": 50,
        "input_bytes": 1_000,
        "output_bytes": 0,
        "item_count": 1,
        "error_count": 0,
    }
    assert summary["derived"] == {
        "ping_response_bytes": 1_000,
        "ping_parser_input_bytes": 4_000,
        "ping_xml_parse_amplification": 4.0,
        "profiled_wall_ns": 370,
        "profiled_cpu_ns": 280,
    }
