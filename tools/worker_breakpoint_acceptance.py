from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from enum import StrEnum
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from time import perf_counter


SCHEMA = "onec-worker-breakpoint-acceptance-v1"
EXPECTED_TEST_COUNT = 3
_REPOSITORY = Path(__file__).resolve().parents[1]
_TESTS = (
    "tests/integration/test_worker_universe_1c.py::"
    "test_worker_breakpoint_workspace_replacement_live",
    "tests/integration/test_worker_universe_1c.py::"
    "test_worker_breakpoint_pinned_generations_live",
    "tests/integration/test_worker_universe_1c.py::"
    "test_worker_breakpoint_capture_evaluation_does_not_stop_live",
)


class AcceptanceOutcome(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class _IterationEvidence:
    iteration: int
    outcome: str
    elapsed_seconds: float
    passed: int
    failed: int
    skipped: int
    errors: int


def _summary_count(output: str, label: str) -> int:
    matches = re.findall(rf"(?<!\d)(\d+)\s+{re.escape(label)}\b", output)
    return 0 if not matches else int(matches[-1])


def classify_pytest_run(returncode: int, output: str) -> AcceptanceOutcome:
    if type(returncode) is not int or not isinstance(output, str):
        raise TypeError("pytest acceptance result is invalid")
    if returncode != 0:
        return AcceptanceOutcome.FAILED
    passed = _summary_count(output, "passed")
    failed = _summary_count(output, "failed")
    errors = _summary_count(output, "error") + _summary_count(output, "errors")
    skipped = _summary_count(output, "skipped")
    if (
        passed == EXPECTED_TEST_COUNT
        and failed == 0
        and errors == 0
        and skipped == 0
    ):
        return AcceptanceOutcome.PASSED
    if passed == 0 and failed == 0 and errors == 0 and skipped:
        return AcceptanceOutcome.BLOCKED
    return AcceptanceOutcome.FAILED


def _run_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["ONEC_RUN_WORKER_UNIVERSE_INTEGRATION"] = "1"
    additions = (
        str(_REPOSITORY),
        str(_REPOSITORY / "src"),
        str(_REPOSITORY / "packages" / "mcp" / "src"),
    )
    inherited = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = os.pathsep.join(
        additions + ((inherited,) if inherited else ())
    )
    return environment


def run_worker_breakpoint_acceptance(
    *,
    output: Path,
    iterations: int = 1,
) -> Path:
    output = Path(output)
    if not output.is_absolute():
        raise ValueError("acceptance output must be an absolute path")
    if type(iterations) is not int or iterations <= 0:
        raise ValueError("acceptance iterations must be positive")
    if output.exists():
        raise FileExistsError(f"acceptance output already exists: {output}")
    output.mkdir(parents=True)

    evidence: list[_IterationEvidence] = []
    for iteration in range(1, iterations + 1):
        started = perf_counter()
        completed = subprocess.run(
            (sys.executable, "-m", "pytest", *_TESTS, "-q"),
            cwd=_REPOSITORY,
            env=_run_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        transcript = completed.stdout
        outcome = classify_pytest_run(completed.returncode, transcript)
        evidence.append(
            _IterationEvidence(
                iteration=iteration,
                outcome=outcome.value,
                elapsed_seconds=round(perf_counter() - started, 6),
                passed=_summary_count(transcript, "passed"),
                failed=_summary_count(transcript, "failed"),
                skipped=_summary_count(transcript, "skipped"),
                errors=(
                    _summary_count(transcript, "error")
                    + _summary_count(transcript, "errors")
                ),
            )
        )
        if outcome is not AcceptanceOutcome.PASSED:
            break

    outcomes = {AcceptanceOutcome(item.outcome) for item in evidence}
    overall = (
        AcceptanceOutcome.FAILED
        if AcceptanceOutcome.FAILED in outcomes
        else AcceptanceOutcome.BLOCKED
        if AcceptanceOutcome.BLOCKED in outcomes
        else AcceptanceOutcome.PASSED
    )
    report = {
        "schema": SCHEMA,
        "outcome": overall.value,
        "platform_profile": "8.3.27.2170",
        "iterations_requested": iterations,
        "iterations_completed": len(evidence),
        "tests_per_iteration": EXPECTED_TEST_COUNT,
        "capture_semantics": "worker_breakpoint_ignored_without_error",
        "iterations": [asdict(item) for item in evidence],
    }
    report_path = output / "worker-breakpoint-acceptance.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return report_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the disposable 1C Worker breakpoint acceptance gates."
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--iterations", type=int, default=1)
    return parser


def main() -> int:
    args = _parser().parse_args()
    report_path = run_worker_breakpoint_acceptance(
        output=args.output,
        iterations=args.iterations,
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    print(f"{report['outcome']}: {report_path}")
    return {
        AcceptanceOutcome.PASSED.value: 0,
        AcceptanceOutcome.FAILED.value: 1,
        AcceptanceOutcome.BLOCKED.value: 2,
    }[report["outcome"]]


if __name__ == "__main__":
    raise SystemExit(main())
