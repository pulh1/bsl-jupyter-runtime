from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path


def add_two_stage_bootstrap_evidence(run_dir: Path) -> None:
    environment_path = run_dir / "environment.json"
    environment = json.loads(environment_path.read_text(encoding="utf-8"))
    server = environment["target"]
    client = {
        "target_id": {
            **server["target_id"],
            "id": "44444444-4444-4444-4444-444444444444",
        },
        "target_type": "ManagedClient",
        "state": "stopped",
        "state_number": None,
    }
    entry = {
        **environment["server_service"],
        "line": environment["server_service"]["line"] - 2,
    }
    environment["client_target"] = client
    environment["server_entry"] = entry
    environment_path.write_text(
        json.dumps(environment, ensure_ascii=False), encoding="utf-8"
    )

    def stop(target: dict[str, object], location: dict[str, object]) -> dict[str, object]:
        return {
            "target_id": target["target_id"],
            "location": location,
            "reason": "breakpoint",
            "stop_by_breakpoint": True,
            "suspended_by_other": False,
            "stack": [],
            "runtime_error": "",
        }

    for name, value in (
        ("managed-startup-stop.json", stop(client, environment["managed_startup"])),
        ("server-entry-stop.json", stop(server, entry)),
        ("server-start-stop.json", stop(server, environment["server_service"])),
    ):
        (run_dir / name).write_text(json.dumps(value), encoding="utf-8")
    (run_dir / "guard-bootstrap.json").write_text(
        json.dumps(
            {
                "variable": "ПродолжатьЦикл",
                "before": {
                    "type_name": "Булево",
                    "presentation": "Ложь",
                    "error_occurred": False,
                },
                "write": {
                    "expression": "Истина",
                    "type_name": "Булево",
                    "presentation": "Истина",
                    "error_occurred": False,
                },
                "after": {
                    "type_name": "Булево",
                    "presentation": "Истина",
                    "error_occurred": False,
                },
                "transcript_interval": {"after_sequence": 0, "end_sequence": 9},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    empty_hash = sha256(b"").hexdigest()
    commands = (
        "evalExpr",
        "modifyValue",
        "evalExpr",
        "setBreakpoints",
        "step",
        "pingDebugUIParams",
        "setBreakpoints",
        "step",
        "pingDebugUIParams",
    )
    (run_dir / "rdbg-transcript.jsonl").write_text(
        "".join(
            json.dumps(
                {
                    "sequence": sequence,
                    "timestamp": "2026-08-13T00:00:00Z",
                    "monotonic_ns": sequence,
                    "event": "rdbg_exchange",
                    "command": command,
                    "status_code": 200,
                    "duration_ms": 1.0,
                    "error_present": False,
                    "request_bytes": 0,
                    "request_sha256": empty_hash,
                    "response_bytes": 0,
                    "response_sha256": empty_hash,
                }
            )
            + "\n"
            for sequence, command in enumerate(commands, start=1)
        ),
        encoding="utf-8",
    )
