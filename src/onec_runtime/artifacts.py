from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from threading import Lock
from typing import Any

from onec_runtime.rdbg.transport import TranscriptEntry
from onec_runtime.privacy import public_artifact_value


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class ArtifactWriter:
    def __init__(self, root: Path, scenario: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        self.run_dir = root / f"{stamp}-{scenario}"
        self.run_dir.mkdir(parents=True, exist_ok=False)
        self._lock = Lock()

    def write_json(self, name: str, value: Any) -> None:
        (self.run_dir / name).write_text(
            json.dumps(public_artifact_value(value), ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )

    def append_jsonl(self, name: str, value: Any) -> None:
        line = json.dumps(public_artifact_value(value), ensure_ascii=False, default=str)
        with self._lock, (self.run_dir / name).open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
            stream.flush()

    def transcript(self, entry: TranscriptEntry) -> None:
        self.append_jsonl(
            "rdbg-transcript.jsonl",
            {
                "timestamp": utc_now(),
                "monotonic_ns": entry.monotonic_ns,
                "command": entry.command,
                "status_code": entry.status_code,
                "duration_ms": entry.duration_ms,
                "error": entry.error,
                "request_xml": entry.request.decode("utf-8", errors="replace"),
                "response_xml": entry.response.decode("utf-8", errors="replace"),
            },
        )


class ExistingArtifactSink(ArtifactWriter):
    """Append artifact streams directly to an existing run directory."""

    def __init__(self, run_dir: Path) -> None:
        if not run_dir.is_dir():
            raise ValueError(f"Artifact run directory does not exist: {run_dir}")
        self.run_dir = run_dir
        self._lock = Lock()
