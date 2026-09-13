from __future__ import annotations

from tools.worker_breakpoint_acceptance import (
    AcceptanceOutcome,
    classify_pytest_run,
)


def test_acceptance_pass_requires_all_three_live_tests() -> None:
    assert (
        classify_pytest_run(0, "3 passed, 6 deselected in 72.66s")
        is AcceptanceOutcome.PASSED
    )


def test_acceptance_skip_is_blocked_not_passed() -> None:
    assert (
        classify_pytest_run(0, "3 skipped, 6 deselected in 0.12s")
        is AcceptanceOutcome.BLOCKED
    )


def test_acceptance_partial_or_failed_run_is_failed() -> None:
    assert classify_pytest_run(0, "2 passed in 1.00s") is AcceptanceOutcome.FAILED
    assert (
        classify_pytest_run(1, "2 passed, 1 failed in 1.00s")
        is AcceptanceOutcome.FAILED
    )
