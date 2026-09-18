"""Read-only public state derived from one controller observation."""

import pytest

from onec_runtime.errors import ProtocolError
from onec_runtime.execution.capture.scope import (
    CaptureContextState, CaptureFrameIdentity, CaptureSetupSnapshot,
    CaptureSetupStage,
)
from onec_runtime.execution.main.operation import MainPhase
from onec_runtime.execution.namespace import RuntimeNamespaceOwner
from onec_runtime.execution.status_projection import (
    ControllerStatusFacts, ExecutionActivity, ExecutionStatusProjection,
)
from onec_runtime.execution.worker_activation import WorkerActivationSnapshot
from onec_runtime.runtime_models import OperationState
from onec_runtime.worker_universe import WorkerGenerationHandle


class FactsReader:
    def __init__(self, facts: ControllerStatusFacts) -> None:
        self.facts = facts

    def __call__(self) -> ControllerStatusFacts:
        return self.facts


class WorkerReader:
    def __init__(self, handle: WorkerGenerationHandle | None = None) -> None:
        self.handle = handle

    def __call__(self) -> WorkerActivationSnapshot:
        return WorkerActivationSnapshot(0, (), None, self.handle)


def projection(
    facts: ControllerStatusFacts,
    worker: WorkerReader | None = None,
) -> tuple[ExecutionStatusProjection, FactsReader, RuntimeNamespaceOwner]:
    facts_reader = FactsReader(facts)
    worker_reader = worker or WorkerReader()
    namespace = RuntimeNamespaceOwner(
        facts.runtime_generation, 7, worker_snapshot=worker_reader,
        initial_names=("Начисление",),
    )
    return (
        ExecutionStatusProjection(
            controller_facts=facts_reader,
            worker_snapshot=worker_reader,
            namespace=namespace,
        ),
        facts_reader,
        namespace,
    )


def test_idle_and_main_pending_preserve_command_and_worker_generation() -> None:
    facts = ControllerStatusFacts(4, 0, None)
    handle = WorkerGenerationHandle(4, 7, 3, "a" * 64)
    status_projection, reader, _ = projection(facts, WorkerReader(handle))
    idle = status_projection.status()
    assert idle.state is OperationState.IDLE
    assert idle.operation_id == 0
    assert idle.worker_generation is handle
    reader.facts = ControllerStatusFacts(
        4, 19, MainPhase.RUNNING,
        activity=ExecutionActivity.MAIN,
        ticket_phase="running",
    )
    pending = status_projection.status()
    assert pending.state is OperationState.MAIN_PENDING
    assert pending.operation_id == 19
    assert pending.worker_generation is handle


def test_confirmed_capture_remains_captured_after_failed_cell() -> None:
    facts = ControllerStatusFacts(
        4, 19, MainPhase.SUSPENDED_CAPTURE,
        CaptureContextState.READY, CaptureFrameIdentity.CONFIRMED,
    )
    status_projection, reader, _ = projection(facts)
    assert status_projection.status().state is OperationState.CAPTURED
    reader.facts = ControllerStatusFacts(
        4, 19, MainPhase.SUSPENDED_CAPTURE,
        CaptureContextState.READY, CaptureFrameIdentity.CONFIRMED,
        activity=ExecutionActivity.CAPTURE,
        ticket_phase="settled",
    )
    # A failed cell ticket has settled; the physical frame remains confirmed.
    assert status_projection.status().state is OperationState.CAPTURED


def test_active_and_unknown_capture_ticket_do_not_imply_frame_loss() -> None:
    reader_facts = ControllerStatusFacts(
        4, 19, MainPhase.SUSPENDED_CAPTURE,
        CaptureContextState.READY, CaptureFrameIdentity.CONFIRMED,
        activity=ExecutionActivity.CAPTURE,
        ticket_phase="running",
    )
    status_projection, reader, _ = projection(reader_facts)
    assert status_projection.status().state is OperationState.EVALUATING_CAPTURE
    reader.facts = ControllerStatusFacts(
        4, 19, MainPhase.SUSPENDED_CAPTURE,
        CaptureContextState.READY, CaptureFrameIdentity.CONFIRMED,
        activity=ExecutionActivity.CAPTURE,
        ticket_phase="reconciling",
    )
    assert status_projection.status().state is OperationState.RECOVERING
    reader.facts = ControllerStatusFacts(
        4, 19, MainPhase.SUSPENDED_CAPTURE,
        CaptureContextState.READY, CaptureFrameIdentity.CONFIRMED,
        activity=ExecutionActivity.CAPTURE,
        ticket_phase="unknown",
    )
    assert status_projection.status().state is OperationState.RECOVERING


def test_capture_resume_and_setup_failure_are_separate_facts() -> None:
    setup = CaptureSetupSnapshot(
        CaptureSetupStage.STOP_RECOGNIZED,
        CaptureContextState.SETUP_FAILED,
        CaptureFrameIdentity.UNVERIFIED,
        "RuntimeError",
    )
    status_projection, reader, _ = projection(ControllerStatusFacts(
        4, 19, MainPhase.SUSPENDED_CAPTURE,
        CaptureContextState.SETUP_FAILED, CaptureFrameIdentity.UNVERIFIED,
        capture_setup=setup,
    ))
    failed_setup = status_projection.status()
    assert failed_setup.state is OperationState.CAPTURE_SETUP_FAILED
    assert failed_setup.capture_setup is setup
    reader.facts = ControllerStatusFacts(
        4, 19, MainPhase.SUSPENDED_CAPTURE,
        CaptureContextState.CLOSING, CaptureFrameIdentity.CONFIRMED,
        activity=ExecutionActivity.CAPTURE_RESUME,
        ticket_phase="running",
    )
    assert status_projection.status().state is OperationState.RESUMING


def test_only_proven_loss_reports_lost() -> None:
    status_projection, reader, _ = projection(ControllerStatusFacts(
        4, 19, MainPhase.UNKNOWN,
        CaptureContextState.READY, CaptureFrameIdentity.UNVERIFIED,
        activity=ExecutionActivity.CAPTURE_RESUME,
        ticket_phase="unknown",
    ))
    assert status_projection.status().state is OperationState.RECOVERING
    reader.facts = ControllerStatusFacts(4, 19, MainPhase.LOST)
    assert status_projection.status().state is OperationState.LOST


def test_namespace_snapshot_contains_confirmed_names_and_generation_fence() -> None:
    status_projection, _, namespace = projection(ControllerStatusFacts(4, 0, None))
    namespace.publish_additions(("Результат",))
    observed = status_projection.namespace_snapshot()
    assert (observed.runtime_generation, observed.context_generation) == (4, 7)
    assert observed.names == ("Начисление", "Результат")


def test_stale_worker_generation_cannot_be_published_as_current_status() -> None:
    stale = WorkerGenerationHandle(3, 7, 2, "b" * 64)
    status_projection, _, _ = projection(
        ControllerStatusFacts(4, 0, None), WorkerReader(stale)
    )
    with pytest.raises(ProtocolError, match="Worker generation"):
        status_projection.status()
