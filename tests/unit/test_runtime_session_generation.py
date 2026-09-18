"""A replacement bootstrap gives runtime bound references a fresh epoch."""

from pathlib import Path

import pytest

from onec_runtime.session import RuntimeSession

from test_extension_session import (
    FakeLifecycle, fast_decision, patch_successful_runtime_attempt, session_config,
)


def test_successive_public_bootstraps_advance_runtime_generation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    patch_successful_runtime_attempt(
        monkeypatch,
        FakeLifecycle(decisions=[fast_decision(), fast_decision()]),
    )
    config = session_config(tmp_path)

    first = RuntimeSession.start(config)
    try:
        first_generation = first.namespace_snapshot().runtime_generation
    finally:
        first.close()

    second = RuntimeSession.start(config)
    try:
        second_generation = second.namespace_snapshot().runtime_generation
    finally:
        second.close()

    assert type(first_generation) is int
    assert second_generation > first_generation
