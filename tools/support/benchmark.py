from __future__ import annotations

from math import ceil
from statistics import median
from typing import TypedDict


class TimingSummary(TypedDict):
    operations: int
    elapsed_s: float
    median_ms: float
    p95_ms: float
    operations_s: float
    mib_s: float


def nearest_rank(samples: list[float] | tuple[float, ...], percentile: float) -> float:
    """Return an observed percentile using the nearest-rank definition."""
    if not 0 < percentile <= 1:
        raise ValueError("percentile must be in (0, 1]")
    if not samples:
        raise ValueError("samples must not be empty")
    ordered = sorted(float(value) for value in samples)
    return ordered[ceil(len(ordered) * percentile) - 1]


def summarize_durations(
    durations_s: list[float],
    *,
    elapsed_s: float,
    byte_count: int,
) -> TimingSummary:
    if not durations_s:
        raise ValueError("at least one duration is required")
    ordered = sorted(durations_s)
    p95 = nearest_rank(ordered, 0.95)
    return {
        "operations": len(durations_s),
        "elapsed_s": elapsed_s,
        "median_ms": median(ordered) * 1_000,
        "p95_ms": p95 * 1_000,
        "operations_s": len(durations_s) / elapsed_s,
        "mib_s": byte_count / 1024 / 1024 / elapsed_s,
    }
