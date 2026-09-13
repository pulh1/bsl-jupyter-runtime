from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
from statistics import mean


def _json_lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path)
    parser.add_argument("--increments", type=int)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()

    records = _json_lines(args.run / "commands.jsonl")
    if not records:
        raise RuntimeError("commands.jsonl is empty")
    summary = json.loads((args.run / "summary.json").read_text(encoding="utf-8"))
    if summary["status"] != "PASS":
        raise RuntimeError(f"run status is {summary['status']}: {summary['error']}")

    expected_ids = list(range(1, len(records) + 1))
    command_ids = [int(record["command_id"]) for record in records]
    completed_ids = [int(record["completed_command_id"]) for record in records]
    counters = [int(record["observed_counter"]) for record in records]
    if command_ids != expected_ids or completed_ids != expected_ids:
        raise RuntimeError("command IDs are lost, duplicated, or out of order")
    if counters != list(range(len(records))):
        raise RuntimeError("counter sequence is lost, duplicated, or out of order")
    if args.increments is not None and len(records) != args.increments + 1:
        raise RuntimeError(
            f"expected {args.increments + 1} records, got {len(records)}"
        )
    if any(str(record["error"]) for record in records):
        raise RuntimeError("at least one command contains a BSL error")

    locations = {
        (
            record["module_type"],
            record["extension_name"],
            record["object_id"],
            record["property_id"],
            int(record["line"]),
        )
        for record in records
    }
    if len(locations) != 1:
        raise RuntimeError(f"more than one accepted stop location: {locations}")

    durations = sorted(float(record["duration_ms"]) for record in records)
    onec_rss = [int(record["memory"]["onec"]) for record in records]
    blocks = []
    increment_records = records[1:]
    for start in range(0, len(increment_records), 1_000):
        block = increment_records[start : start + 1_000]
        block_durations = sorted(float(record["duration_ms"]) for record in block)
        blocks.append(
            {
                "first_command_id": int(block[0]["command_id"]),
                "last_command_id": int(block[-1]["command_id"]),
                "records": len(block),
                "mean_latency_ms": round(mean(block_durations), 3),
                "p95_latency_ms": round(
                    block_durations[int((len(block_durations) - 1) * 0.95)], 3
                ),
                "onec_rss_last_bytes": int(block[-1]["memory"]["onec"]),
                "dbgs_rss_last_bytes": int(block[-1]["memory"]["dbgs"]),
            }
        )
    heartbeats_path = args.run / "health.jsonl"
    heartbeats = _json_lines(heartbeats_path) if heartbeats_path.is_file() else []
    heartbeat_times = [
        datetime.fromisoformat(str(item["timestamp"])) for item in heartbeats
    ]
    heartbeat_gaps = [
        (right - left).total_seconds()
        for left, right in zip(heartbeat_times, heartbeat_times[1:])
    ]

    location = next(iter(locations))
    result = {
        "status": "PASS",
        "records": len(records),
        "increments": len(records) - 1,
        "final_counter": counters[-1],
        "latency_ms": {
            "mean": round(mean(durations), 3),
            "p95": round(durations[int((len(durations) - 1) * 0.95)], 3),
            "max": round(durations[-1], 3),
        },
        "onec_rss_bytes": {
            "first": onec_rss[0],
            "last": onec_rss[-1],
            "max": max(onec_rss),
        },
        "blocks": blocks,
        "heartbeats": len(heartbeats),
        "max_heartbeat_gap_s": round(max(heartbeat_gaps), 3) if heartbeat_gaps else None,
        "location": {
            "module_type": location[0],
            "extension_name": location[1],
            "object_id": location[2],
            "property_id": location[3],
            "line": location[4],
        },
        "duration_s": summary["duration_s"],
    }
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.write:
        (args.run / "verification.json").write_text(
            rendered + "\n", encoding="utf-8"
        )
    print(rendered)


if __name__ == "__main__":
    main()
