from __future__ import annotations

import pytest

from tools.support.benchmark import nearest_rank, summarize_durations


def test_summarize_durations_reports_latency_and_throughput() -> None:
    summary = summarize_durations(
        [0.001, 0.003, 0.002, 0.005],
        elapsed_s=0.012,
        byte_count=12_000,
    )

    assert summary["operations"] == 4
    assert summary["median_ms"] == 2.5
    assert summary["p95_ms"] == 5.0
    assert summary["operations_s"] == 4 / 0.012
    assert summary["mib_s"] == 12_000 / 1024 / 1024 / 0.012


def test_nearest_rank_uses_observed_samples_and_validates_fraction() -> None:
    samples = [9.0, 1.0, 4.0, 2.0, 7.0]

    assert nearest_rank(samples, 0.50) == 4.0
    assert nearest_rank(samples, 0.95) == 9.0

    with pytest.raises(ValueError, match="percentile"):
        nearest_rank(samples, 0.0)
    with pytest.raises(ValueError, match="samples"):
        nearest_rank([], 0.95)
