from dataclasses import FrozenInstanceError

import pytest

from onec_runtime.errors import StaleRuntimeGeneration
from onec_runtime.supervisor_model import (
    GenerationLifecycle,
    GenerationRegistry,
    TerminationCause,
)


def test_generation_ids_increase_and_new_start_waits_for_termination() -> None:
    registry = GenerationRegistry()
    first = registry.begin_start()
    registry.activate(first.handle, controller_pid=10, dbgs_pid=11, onec_pid=12)

    with pytest.raises(RuntimeError, match="ACTIVE"):
        registry.begin_start()

    registry.begin_termination(first.handle, TerminationCause.CONTROLLER_EXIT)
    registry.finish_termination(first.handle)
    second = registry.begin_start()

    assert first.handle.generation_id == 1
    assert second.handle.generation_id == 2


def test_old_generation_handle_is_fenced() -> None:
    registry = GenerationRegistry()
    first = registry.begin_start()
    registry.activate(first.handle, controller_pid=10, dbgs_pid=11, onec_pid=12)
    registry.begin_termination(first.handle, TerminationCause.CONTROLLER_EXIT)
    registry.finish_termination(first.handle)
    second = registry.begin_start()
    registry.activate(second.handle, controller_pid=20, dbgs_pid=21, onec_pid=22)

    with pytest.raises(StaleRuntimeGeneration):
        registry.require_active(first.handle)
    assert registry.require_active(second.handle).lifecycle is GenerationLifecycle.ACTIVE


def test_generation_records_transition_immutably_in_lifecycle_order() -> None:
    registry = GenerationRegistry()
    starting = registry.begin_start()

    with pytest.raises(RuntimeError, match="STARTING"):
        registry.begin_termination(starting.handle, TerminationCause.STARTUP_FAILED)

    active = registry.activate(
        starting.handle, controller_pid=10, dbgs_pid=11, onec_pid=12
    )
    terminating = registry.begin_termination(
        active.handle, TerminationCause.CONTROLLER_HUNG
    )
    terminated = registry.finish_termination(terminating.handle)

    assert starting.lifecycle is GenerationLifecycle.STARTING
    assert active.lifecycle is GenerationLifecycle.ACTIVE
    assert terminating.lifecycle is GenerationLifecycle.TERMINATING
    assert terminated.lifecycle is GenerationLifecycle.TERMINATED
    assert active.controller_pid == terminating.controller_pid == 10
    assert terminated.termination_cause is TerminationCause.CONTROLLER_HUNG
    with pytest.raises(FrozenInstanceError):
        active.lifecycle = GenerationLifecycle.TERMINATING  # type: ignore[misc]
