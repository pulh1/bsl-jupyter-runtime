from __future__ import annotations

from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from threading import Event, Lock, RLock, Thread, current_thread
from time import monotonic, sleep
from types import SimpleNamespace
from uuid import uuid4

import pytest

from onec_runtime.capture_evaluation import (
    CaptureEvaluationCoordinator,
    CaptureEvaluationKind,
    CaptureEvaluationOutcome,
    CaptureEvaluationRequest,
    CaptureEvaluationState,
    CaptureEvaluationTicket,
    CaptureFence,
    CapturePhase,
    CaptureStatus,
)
from onec_runtime.errors import (
    CaptureOutcomeUnknownError,
    CaptureRecoveryRequiredError,
    NoActiveCaptureError,
    StaleCaptureError,
    WorkerPromotionOutcomeUnknown,
)
from onec_runtime.prototype_runtime import OperationState
from onec_runtime.rdbg.models import EvaluationResult, PendingEvaluation
from onec_runtime.runtime_api import PrototypeRuntimeApi, RuntimeStatus
from onec_runtime.session import RuntimeSession

from test_capture_evaluation_lifecycle import (
    ControlledCaptureSession,
    close_owner,
    controlled_notebook_runtime,
)
from test_notebook_method_runtime import UPDATE
from test_prototype_runtime import (
    TARGET,
    captured_controller,
)
from test_runtime_api import FakeController


_JOIN_TIMEOUT_S = 2.0


class _CrossThreadRejectingLock:
    """Let the initiating operation own the lock; reject observer entry."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._guard = Lock()
        self._owner: int | None = None
        self._depth = 0
        self.cross_thread_attempts = 0

    def acquire(self, blocking: bool = True) -> bool:
        ident = current_thread().ident
        assert ident is not None
        with self._guard:
            if self._owner is not None and self._owner != ident:
                self.cross_thread_attempts += 1
                raise AssertionError("control plane entered the session operation lock")
        acquired = self._lock.acquire(blocking=blocking)
        if acquired:
            with self._guard:
                self._owner = ident
                self._depth += 1
        return acquired

    def release(self) -> None:
        ident = current_thread().ident
        assert ident is not None
        with self._guard:
            assert self._owner == ident
            self._depth -= 1
            if self._depth == 0:
                self._owner = None
        self._lock.release()

    def __enter__(self) -> _CrossThreadRejectingLock:
        assert self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def _capture_runtime(*, timeout_s: float = 2.0):  # type: ignore[no-untyped-def]
    transport = ControlledCaptureSession()
    controller = captured_controller(transport, command_timeout_s=timeout_s)
    return PrototypeRuntimeApi(controller), controller, transport


def _runtime_session(api: PrototypeRuntimeApi) -> RuntimeSession:
    runtime = object.__new__(RuntimeSession)
    runtime.runtime_api = api
    runtime._operation_lock = _CrossThreadRejectingLock()
    runtime.config = SimpleNamespace(chunk_size=128)
    return runtime


def _start_pending_capture(invoke, accepted: Event):  # type: ignore[no-untyped-def]
    failures: list[BaseException] = []
    finished = Event()
    accepted.clear()

    def run() -> None:
        try:
            invoke()
        except BaseException as error:
            failures.append(error)
        finally:
            finished.set()

    thread = Thread(target=run, name="capture-control-plane-initiator")
    thread.start()
    assert accepted.wait(1), "synthetic RDBG evaluation was not acknowledged"
    return thread, finished, failures


def _finish_pending(
    thread: Thread | None,
    transport: ControlledCaptureSession,
) -> None:
    if transport.capture_pending is not None:
        transport.complete()
    if thread is not None:
        thread.join(_JOIN_TIMEOUT_S)
        assert not thread.is_alive(), "initiating caller leaked"


def _capture_owner(controller: object) -> CaptureEvaluationCoordinator:
    owners = [
        value
        for value in vars(controller).values()
        if isinstance(value, CaptureEvaluationCoordinator)
    ]
    assert len(owners) == 1
    return owners[0]


def _owner_fence(owner: CaptureEvaluationCoordinator) -> CaptureFence:
    fences = [value for value in vars(owner).values() if isinstance(value, CaptureFence)]
    assert len(fences) == 1
    return fences[0]


def _force_phase(
    owner: CaptureEvaluationCoordinator,
    expected: CapturePhase,
    replacement: CapturePhase,
) -> None:
    """Inject an otherwise Task-7-owned phase without naming coordinator storage."""

    matches = [name for name, value in vars(owner).items() if value is expected]
    assert len(matches) == 1
    setattr(owner, matches[0], replacement)


def _eventually(predicate, *, timeout_s: float = 1.0) -> None:  # type: ignore[no-untyped-def]
    deadline = monotonic() + timeout_s
    while not predicate():
        assert monotonic() < deadline, "control-plane state did not settle"
        sleep(0.002)


def _submit_pending(owner: CaptureEvaluationCoordinator):  # type: ignore[no-untyped-def]
    pending = PendingEvaluation(TARGET, uuid4(), object())
    release = Event()
    dispatch_count = 0

    def dispatch(entered):  # type: ignore[no-untyped-def]
        nonlocal dispatch_count
        dispatch_count += 1
        entered()
        return pending

    def poll(capability: PendingEvaluation, timeout_s: float):
        assert capability is pending
        if not release.wait(min(timeout_s, 0.005)):
            from onec_runtime.errors import CommandTimeout

            raise CommandTimeout("synthetic interval timeout")
        return EvaluationResult(pending.result_id, "Число", "901", False)

    ticket = owner.submit_evaluation(CaptureEvaluationRequest(
        _owner_fence(owner),
        CaptureEvaluationKind.USER_BSL,
        dispatch,
        poll,
        lambda result: 901,
    ))
    return ticket, release, lambda: dispatch_count


def test_current_capture_rejects_runtime_without_a_capture() -> None:
    api = PrototypeRuntimeApi(FakeController())

    with pytest.raises(NoActiveCaptureError):
        api.current_capture()


def test_current_capture_returns_a_typed_paused_view() -> None:
    api, controller, transport = _capture_runtime()
    try:
        capture = api.current_capture()
        status = capture.status()

        from onec_runtime.capture_inspection import CaptureView

        assert isinstance(capture, CaptureView)
        assert isinstance(status, CaptureStatus)
        assert (
            status.operation_id,
            status.capture_generation,
            status.stop_sequence,
            status.phase,
        ) == (1, 1, 1, CapturePhase.PAUSED)
    finally:
        close_owner(controller, transport)


@pytest.mark.parametrize(
    "endpoint",
    ("runtime.status", "runtime.current_capture", "capture.status", "capture.wait"),
)
def test_session_control_plane_bypasses_operation_lock_while_initiator_is_blocked(
    endpoint: str,
) -> None:
    api, controller, transport = _capture_runtime()
    runtime = _runtime_session(api)
    capture = None
    thread: Thread | None = None
    try:
        if endpoint.startswith("capture."):
            capture = runtime.current_capture()
        thread, finished, failures = _start_pending_capture(
            lambda: runtime.execute_bsl("РезультатИнструкции = 901;"),
            transport.accepted,
        )
        assert not finished.is_set()

        if endpoint == "runtime.status":
            result = runtime.status()
            assert isinstance(result, RuntimeStatus)
            assert result.state is OperationState.EVALUATING_CAPTURE
        elif endpoint == "runtime.current_capture":
            result = runtime.current_capture()
            assert result.status().phase is CapturePhase.EVALUATING
        elif endpoint == "capture.status":
            assert capture is not None
            result = capture.status()
            assert result.phase is CapturePhase.EVALUATING
        else:
            assert capture is not None
            result = capture.wait(timeout_s=0)
            assert result.state is CaptureEvaluationState.PENDING

        assert runtime._operation_lock.cross_thread_attempts == 0
        assert not failures
    finally:
        _finish_pending(thread, transport)
        close_owner(controller, transport)


def test_capture_control_plane_bypasses_api_writer_availability_and_worker_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, controller, transport = _capture_runtime()
    thread: Thread | None = None
    try:
        capture = api.current_capture()
        thread, finished, failures = _start_pending_capture(
            lambda: api.execute_bsl("РезультатИнструкции = 901;"),
            transport.accepted,
        )
        assert not finished.is_set()

        @contextmanager
        def forbidden_writer():
            raise AssertionError("control plane entered RuntimeApi single-writer")
            yield

        def forbidden_available() -> None:
            raise AssertionError("control plane called RuntimeApi._require_available")

        def forbidden_guard(_handle: object) -> None:
            raise AssertionError("control plane called a Worker public-value guard")

        with monkeypatch.context() as patch:
            patch.setattr(api, "_single_writer", forbidden_writer)
            patch.setattr(api, "_require_available", forbidden_available)
            patch.setattr(api, "require_public_value_handle", forbidden_guard)
            runtime_status = api.status()
            current = api.current_capture()
            capture_status = capture.status()
            pending = capture.wait(timeout_s=0)

        assert runtime_status.state is OperationState.EVALUATING_CAPTURE
        assert current.status().pending_evaluation_id == capture_status.pending_evaluation_id
        assert capture_status.phase is CapturePhase.EVALUATING
        assert pending.state is CaptureEvaluationState.PENDING
        assert not failures
    finally:
        _finish_pending(thread, transport)
        close_owner(controller, transport)


def test_runtime_status_uses_coordinator_phase_when_controller_state_drifted() -> None:
    api, controller, transport = _capture_runtime()
    thread: Thread | None = None
    try:
        thread, finished, failures = _start_pending_capture(
            lambda: api.execute_bsl("РезультатИнструкции = 901;"),
            transport.accepted,
        )
        assert not finished.is_set()
        controller.state = OperationState.CAPTURED

        status = api.status()
        capture_status = api.current_capture().status()

        assert status.state is OperationState.EVALUATING_CAPTURE
        assert capture_status.phase is CapturePhase.EVALUATING
        assert capture_status.pending_evaluation_id is not None
        assert not failures
    finally:
        _finish_pending(thread, transport)
        close_owner(controller, transport)


def test_interrupted_initiator_leaves_control_plane_and_pending_wait_reachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, controller, transport = _capture_runtime()

    def interrupt_after_acknowledgement(
        ticket: CaptureEvaluationTicket,
        timeout_s: float | None = None,
    ) -> object:
        assert transport.accepted.wait(1)
        raise KeyboardInterrupt

    try:
        monkeypatch.setattr(
            CaptureEvaluationTicket,
            "wait_initiator",
            interrupt_after_acknowledgement,
        )
        with pytest.raises(KeyboardInterrupt):
            api.execute_bsl("РезультатИнструкции = 901;")

        capture = api.current_capture()
        runtime_status = api.status()
        status = capture.status()
        pending = capture.wait(timeout_s=0)

        assert runtime_status.state is OperationState.EVALUATING_CAPTURE
        assert status.phase is CapturePhase.EVALUATING
        assert status.pending_evaluation_id == pending.evaluation_id
        assert pending.state is CaptureEvaluationState.PENDING
        assert api._poisoned_error is None
        assert transport.primary_dispatch_count == 1
    finally:
        if transport.capture_pending is not None:
            transport.complete()
        close_owner(controller, transport)


def test_acknowledged_pending_keeps_generation_pin_without_poison(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    api, controller, transport = controlled_notebook_runtime(tmp_path)
    thread: Thread | None = None
    try:
        assert api.execute_bsl(UPDATE).succeeded
        assert api.execute_bsl("Результат = Б();").kind.value == "captured"
        controller.command_timeout_s = 2.0
        original_operation_pin = api._operation_generation_pin
        assert original_operation_pin is not None
        assert len(api._worker_universe._leases) == 1

        thread, finished, failures = _start_pending_capture(
            lambda: api.execute_bsl("РезультатИнструкции = Б();"),
            transport.accepted,
        )
        assert not finished.is_set()

        capture_status = api.current_capture().status()
        assert capture_status.phase is CapturePhase.EVALUATING
        assert capture_status.pending_evaluation_id is not None
        assert api._operation_generation_pin is original_operation_pin
        assert api._evaluation_generation_pin is None
        assert len(api._worker_universe._leases) == 2
        assert api._poisoned_error is None
        assert not failures
    finally:
        _finish_pending(thread, transport)
        close_owner(controller, transport)


def test_uncertain_preacceptance_quarantines_pin_but_keeps_control_plane_reachable(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    api, controller, transport = controlled_notebook_runtime(tmp_path)
    quarantined: list[object] = []
    retain = api._worker_universe.retain_outcome_unknown

    def tracked_retain(pin: object) -> None:
        quarantined.append(pin)
        retain(pin)  # type: ignore[arg-type]

    monkeypatch.setattr(api._worker_universe, "retain_outcome_unknown", tracked_retain)
    try:
        assert api.execute_bsl(UPDATE).succeeded
        assert api.execute_bsl("Результат = Б();").kind.value == "captured"
        capture_starts_before = transport.capture_start_count
        transport.dispatch_error = OSError("private synthetic uncertain dispatch")

        with pytest.raises(CaptureOutcomeUnknownError):
            api.execute_bsl("РезультатИнструкции = Б();")

        assert len(quarantined) == 1
        assert isinstance(api._poisoned_error, WorkerPromotionOutcomeUnknown)
        status = api.status()
        capture = api.current_capture()
        outcome = capture.wait(timeout_s=0)

        assert status.state is OperationState.RECOVERING
        assert capture.status().phase is CapturePhase.OUTCOME_UNKNOWN
        assert outcome.state is CaptureEvaluationState.UNKNOWN
        assert transport.capture_start_count == capture_starts_before + 1
    finally:
        close_owner(controller, transport)


@pytest.mark.parametrize("field", ("operation_id", "runtime_generation", "stop_sequence"))
def test_capture_view_uses_the_exact_three_part_fence(field: str) -> None:
    api, controller, transport = _capture_runtime()
    try:
        capture = api.current_capture()
        setattr(controller, field, getattr(controller, field) + 1)

        assert capture.status().phase is CapturePhase.STALE
        with pytest.raises(StaleCaptureError):
            capture.wait(timeout_s=0)
    finally:
        close_owner(controller, transport)


def test_capture_view_reports_resuming_from_the_control_snapshot() -> None:
    api, controller, transport = _capture_runtime()
    try:
        capture = api.current_capture()
        owner = _capture_owner(controller)
        _force_phase(owner, CapturePhase.PAUSED, CapturePhase.RESUMING)

        status = capture.status()

        assert status.phase is CapturePhase.RESUMING
        assert status.can_inspect is False
        assert status.can_resume_capture is False
    finally:
        close_owner(controller, transport)


def test_current_capture_exposes_recovery_required_state() -> None:
    transport = ControlledCaptureSession(fail_workspace_on_call=3)
    controller = captured_controller(transport, command_timeout_s=1)
    api = PrototypeRuntimeApi(controller)
    try:
        capture = api.current_capture()
        transport.events.put(EvaluationResult(uuid4(), "Число", "901", False))
        original_wait = transport.wait_evaluation_event

        def correlated_wait(
            pending: PendingEvaluation,
            *,
            timeout_s: float,
        ) -> EvaluationResult:
            result = original_wait(pending, timeout_s=timeout_s)
            assert isinstance(result, EvaluationResult)
            return EvaluationResult(
                pending.result_id,
                result.type_name,
                result.presentation,
                result.error_occurred,
                error_text=result.error_text,
            )

        transport.wait_evaluation_event = correlated_wait  # type: ignore[method-assign]
        with pytest.raises(CaptureRecoveryRequiredError):
            controller.execute_capture("РезультатИнструкции = 901;")

        status = capture.status()
        assert status.phase is CapturePhase.RECOVERY_REQUIRED
        assert status.failure is not None
        assert status.failure.code == "workspace_restore_failed"
    finally:
        close_owner(controller, transport)


def test_old_capture_view_becomes_stale_after_target_loss() -> None:
    from onec_runtime.errors import TargetLost

    transport = ControlledCaptureSession(poll_error=TargetLost("private target"))
    controller = captured_controller(transport, command_timeout_s=1)
    api = PrototypeRuntimeApi(controller)
    try:
        capture = api.current_capture()
        with pytest.raises(StaleCaptureError):
            controller.execute_capture("РезультатИнструкции = 901;")

        assert capture.status().phase is CapturePhase.STALE
        with pytest.raises(StaleCaptureError):
            capture.wait(timeout_s=0)
    finally:
        close_owner(controller, transport)


def test_capture_wait_timeout_and_repeated_observation_never_redispatch() -> None:
    api, controller, transport = _capture_runtime()
    owner = _capture_owner(controller)
    release: Event | None = None
    try:
        capture = api.current_capture()
        ticket, release, dispatch_count = _submit_pending(owner)
        _eventually(lambda: dispatch_count() == 1)

        first = capture.wait(timeout_s=0, evaluation_id=ticket.evaluation_id)
        second = capture.wait(timeout_s=0, evaluation_id=ticket.evaluation_id)

        assert first.state is CaptureEvaluationState.PENDING
        assert second.state is CaptureEvaluationState.PENDING
        assert first.evaluation_id == second.evaluation_id == ticket.evaluation_id
        assert dispatch_count() == 1

        release.set()
        settled = capture.wait(timeout_s=1, evaluation_id=ticket.evaluation_id)
        repeated = capture.wait(timeout_s=0, evaluation_id=ticket.evaluation_id)
        assert settled == repeated
        assert settled.state is CaptureEvaluationState.COMPLETED
        assert dispatch_count() == 1
    finally:
        if release is not None:
            release.set()
        close_owner(controller, transport)


def test_capture_view_and_snapshots_are_typed_immutable_and_redacted() -> None:
    api, controller, transport = _capture_runtime()
    try:
        capture = api.current_capture()
        owner = _capture_owner(controller)
        ticket, release, _dispatch_count = _submit_pending(owner)
        try:
            status = capture.status()
            outcome = capture.wait(timeout_s=0, evaluation_id=ticket.evaluation_id)

            assert isinstance(status, CaptureStatus)
            assert isinstance(outcome, CaptureEvaluationOutcome)
            with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
                status.phase = CapturePhase.PAUSED  # type: ignore[misc]
            with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
                outcome.state = CaptureEvaluationState.COMPLETED  # type: ignore[misc]
            with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
                capture.status = lambda: None  # type: ignore[method-assign]

            public_text = repr((capture, status, outcome))
            assert controller.active_operation is not None
            assert controller.active_operation.visible_source not in public_text
            assert repr(_owner_fence(owner).identity) not in public_text
            assert "CaptureEvaluationCoordinator" not in public_text
            assert "_CaptureEvaluationRecord" not in public_text
            for forbidden in (
                "source",
                "handle",
                "record",
                "coordinator",
                "identity",
                "target_id",
            ):
                assert not hasattr(capture, forbidden)
        finally:
            release.set()
            capture.wait(timeout_s=1, evaluation_id=ticket.evaluation_id)
    finally:
        close_owner(controller, transport)


def test_late_completion_does_not_need_runtime_api_lock() -> None:
    api, controller, transport = _capture_runtime()
    thread: Thread | None = None
    capture = None
    try:
        capture = api.current_capture()
        thread, finished, failures = _start_pending_capture(
            lambda: api.execute_bsl("РезультатИнструкции = 901;"),
            transport.accepted,
        )
        assert not finished.is_set()

        assert api._lock.acquire(blocking=False)
        try:
            transport.complete()
            outcome = capture.wait(timeout_s=0.2)
        finally:
            api._lock.release()

        assert outcome.state is CaptureEvaluationState.COMPLETED
        thread.join(_JOIN_TIMEOUT_S)
        assert not thread.is_alive()
        assert not failures
    finally:
        _finish_pending(thread, transport)
        close_owner(controller, transport)
