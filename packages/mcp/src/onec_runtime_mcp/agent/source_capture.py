"""Compatibility re-exports for core-owned symbolic capture resolution."""

from onec_runtime.capture_source import (
    CaptureBinding,
    CapturePointResolver,
    CommonModuleCaptureResolver,
)

__all__ = [
    "CaptureBinding",
    "CapturePointResolver",
    "CommonModuleCaptureResolver",
]
