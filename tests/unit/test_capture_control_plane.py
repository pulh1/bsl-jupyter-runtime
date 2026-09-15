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
    CaptureBusyError,
    CaptureOutcomeUnknownError,
    CaptureRecoveryRequiredError,
    NoActiveCaptureError,
    NoCaptureEvaluationError,
    StaleCaptureError,
    TargetLost,
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

    @property
    def is_locked(self) -> bool:
        with self._guard:
            return self._owner is not None


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


def _start_api_lock_holder(api: PrototypeRuntimeApi):  # type: ignore[no-untyped-def]
    acquired = Event()
    release = Event()
    errors: list[BaseException] = []

    def hold() -> None:
        try:
            with api._lock:
                acquired.set()
                assert release.wait(_JOIN_TIMEOUT_S), "API lock holder was not released"
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=hold, name="capture-api-lock-holder")
    thread.start()
    assert acquired.wait(1), "API lock holder did not start"
    return thread, release, errors


def _start_observer(invoke):  # type: ignore[no-untyped-def]
    finished = Event()
    results: list[object] = []
    errors: list[BaseException] = []

    def observe() -> None:
        try:
            results.append(invoke())
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()

    thread = Thread(target=observe, name="capture-control-plane-observer")
    thread.start()
    return thread, finished, results, errors


def _submit_pending(
    owner: CaptureEvaluationCoordinator,
    *,
    kind: CaptureEvaluationKind = CaptureEvaluationKind.USER_BSL,
    value: int = 901,
):  # type: ignore[no-untyped-def]
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
        kind,
        dispatch,
        poll,
        lambda result: value,
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


def test_session_releases_both_admission_locks_before_initiator_wait() -> None:
    """A second data-plane call must reach coordinator busy admission promptly."""

    api, controller, transport = _capture_runtime()
    runtime = _runtime_session(api)
    initiator: Thread | None = None
    contender: Thread | None = None
    contender_finished = Event()
    contender_errors: list[BaseException] = []
    starts_before = 0
    session_lock_free = False
    api_lock_free = False
    contender_finished_while_pending = False

    def contend() -> None:
        try:
            runtime.execute_bsl("ВтораяИнструкция = 2;")
        except BaseException as error:
            contender_errors.append(error)
        finally:
            contender_finished.set()

    try:
        initiator, initiator_finished, initiator_errors = _start_pending_capture(
            lambda: runtime.execute_bsl("РезультатИнструкции = 901;"),
            transport.accepted,
        )
        assert not initiator_finished.is_set()
        session_lock_free = not runtime._operation_lock.is_locked
        api_lock_free = api._lock.acquire(blocking=False)
        if api_lock_free:
            api._lock.release()

        starts_before = transport.capture_start_count
        contender = Thread(target=contend, name="capture-data-plane-contender")
        contender.start()
        contender_finished_while_pending = contender_finished.wait(0.2)
    finally:
        if not contender_finished_while_pending:
            # Let a contract-violating contender leave the old Session lock
            # after the primary call unwinds, without starting another wait.
            transport.poll_error = TargetLost("synthetic RED cleanup")
        elif transport.capture_pending is not None:
            transport.complete()
        if initiator is not None:
            initiator.join(_JOIN_TIMEOUT_S)
        if contender is not None:
            contender.join(_JOIN_TIMEOUT_S)
        close_owner(controller, transport)

    assert session_lock_free, "RuntimeSession held admission through wait_initiator"
    assert api_lock_free, "RuntimeApi held the writer through wait_initiator"
    assert contender_finished_while_pending, "second data-plane admission blocked"
    assert len(contender_errors) == 1
    assert isinstance(contender_errors[0], CaptureBusyError), repr(contender_errors[0])
    assert transport.capture_start_count == starts_before
    assert not initiator.is_alive()
    assert not contender.is_alive()
    assert initiator_errors == []


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


@pytest.mark.parametrize(
    "endpoint",
    ("current_capture", "capture.status", "capture.wait"),
)
def test_capture_control_plane_does_not_acquire_runtime_api_lock(
    endpoint: str,
) -> None:
    """A blocking api._lock mutant must fail promptly and still tear down."""

    api, controller, transport = _capture_runtime()
    owner = _capture_owner(controller)
    evaluation_release: Event | None = None
    holder: Thread | None = None
    observer: Thread | None = None
    holder_release: Event | None = None
    try:
        capture = api.current_capture()
        ticket, evaluation_release, dispatch_count = _submit_pending(owner)
        _eventually(lambda: dispatch_count() == 1)

        holder, holder_release, holder_errors = _start_api_lock_holder(api)
        invocation = {
            "current_capture": api.current_capture,
            "capture.status": capture.status,
            "capture.wait": lambda: capture.wait(
                timeout_s=0,
                evaluation_id=ticket.evaluation_id,
            ),
        }[endpoint]
        observer, observer_finished, results, observer_errors = _start_observer(
            invocation
        )
        finished_while_locked = observer_finished.wait(0.2)

        # Unblock any bad implementation before asserting, so the test process
        # cannot remain trapped behind a non-reentrant API lock.
        holder_release.set()
        holder.join(_JOIN_TIMEOUT_S)
        observer.join(_JOIN_TIMEOUT_S)

        assert finished_while_locked, f"{endpoint} waited behind api._lock"
        assert holder_errors == []
        assert observer_errors == []
        assert len(results) == 1
        if endpoint == "capture.wait":
            assert isinstance(results[0], CaptureEvaluationOutcome)
            assert results[0].state is CaptureEvaluationState.PENDING
        elif endpoint == "capture.status":
            assert isinstance(results[0], CaptureStatus)
        assert dispatch_count() == 1
    finally:
        if holder_release is not None:
            holder_release.set()
        if holder is not None:
            holder.join(_JOIN_TIMEOUT_S)
            assert not holder.is_alive(), "API lock holder leaked"
        if observer is not None:
            observer.join(_JOIN_TIMEOUT_S)
            assert not observer.is_alive(), "control-plane observer leaked"
        if evaluation_release is not None:
            evaluation_release.set()
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
    runtime = _runtime_session(api)
    try:
        saved = api.current_capture()
        owner = _capture_owner(controller)
        _force_phase(owner, CapturePhase.PAUSED, CapturePhase.RESUMING)

        saved_status = saved.status()
        api_status = api.current_capture().status()
        session_status = runtime.current_capture().status()

        assert saved_status == api_status == session_status
        assert saved_status.phase is CapturePhase.RESUMING
        assert saved_status.can_inspect is False
        assert saved_status.can_resume_capture is False
    finally:
        close_owner(controller, transport)


def test_current_capture_exposes_recovery_required_state() -> None:
    transport = ControlledCaptureSession(fail_workspace_on_call=3)
    controller = captured_controller(transport, command_timeout_s=1)
    api = PrototypeRuntimeApi(controller)
    runtime = _runtime_session(api)
    try:
        saved = api.current_capture()
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

        saved_status = saved.status()
        api_status = api.current_capture().status()
        session_status = runtime.current_capture().status()
        assert saved_status == api_status == session_status
        assert saved_status.phase is CapturePhase.RECOVERY_REQUIRED
        assert saved_status.failure is not None
        assert saved_status.failure.code == "workspace_restore_failed"
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


def test_capture_wait_routes_explicit_user_id_after_later_internal_outcome() -> None:
    api, controller, transport = _capture_runtime()
    owner = _capture_owner(controller)
    releases: list[Event] = []
    try:
        capture = api.current_capture()
        user_ticket, user_release, user_dispatches = _submit_pending(
            owner,
            value=101,
        )
        releases.append(user_release)
        _eventually(lambda: user_dispatches() == 1)
        user_release.set()
        user_outcome = capture.wait(
            timeout_s=1,
            evaluation_id=user_ticket.evaluation_id,
        )
        assert user_outcome.result == 101

        internal_ticket, internal_release, internal_dispatches = _submit_pending(
            owner,
            kind=CaptureEvaluationKind.INSPECTION,
            value=202,
        )
        releases.append(internal_release)
        _eventually(lambda: internal_dispatches() == 1)
        observed_pending = capture.wait(
            timeout_s=0,
            evaluation_id=internal_ticket.evaluation_id,
        )
        assert observed_pending.state is CaptureEvaluationState.PENDING
        internal_release.set()
        internal_outcome = capture.wait(
            timeout_s=1,
            evaluation_id=internal_ticket.evaluation_id,
        )

        default_outcome = capture.wait(timeout_s=0)
        selected_user = capture.wait(
            timeout_s=0,
            evaluation_id=user_ticket.evaluation_id,
        )

        assert default_outcome.evaluation_id == internal_outcome.evaluation_id
        assert default_outcome.evaluation_kind is CaptureEvaluationKind.INSPECTION
        assert selected_user == user_outcome
        assert selected_user.evaluation_id == user_ticket.evaluation_id
        assert selected_user.result == 101
        assert user_dispatches() == internal_dispatches() == 1
    finally:
        for release in releases:
            release.set()
        close_owner(controller, transport)


def test_capture_wait_rejects_unknown_and_evicted_ids_without_dispatch() -> None:
    api, controller, transport = _capture_runtime()
    owner = _capture_owner(controller)
    releases: list[Event] = []
    try:
        capture = api.current_capture()
        first_ticket, first_release, first_dispatches = _submit_pending(
            owner,
            value=101,
        )
        releases.append(first_release)
        _eventually(lambda: first_dispatches() == 1)
        first_release.set()
        capture.wait(timeout_s=1, evaluation_id=first_ticket.evaluation_id)

        with pytest.raises(NoCaptureEvaluationError) as unknown:
            capture.wait(timeout_s=0, evaluation_id="missing-evaluation-id")
        assert unknown.value.evaluation_id == "missing-evaluation-id"
        assert first_dispatches() == 1

        second_ticket, second_release, second_dispatches = _submit_pending(
            owner,
            value=202,
        )
        releases.append(second_release)
        _eventually(lambda: second_dispatches() == 1)
        second_release.set()
        second = capture.wait(
            timeout_s=1,
            evaluation_id=second_ticket.evaluation_id,
        )
        assert second.result == 202

        with pytest.raises(NoCaptureEvaluationError) as evicted:
            capture.wait(timeout_s=0, evaluation_id=first_ticket.evaluation_id)
        assert evicted.value.evaluation_id == first_ticket.evaluation_id
        assert first_dispatches() == second_dispatches() == 1
    finally:
        for release in releases:
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
    initiator: Thread | None = None
    holder: Thread | None = None
    observer: Thread | None = None
    holder_release: Event | None = None
    try:
        capture = api.current_capture()
        initiator, finished, failures = _start_pending_capture(
            lambda: api.execute_bsl("РезультатИнструкции = 901;"),
            transport.accepted,
        )
        assert not finished.is_set()

        holder, holder_release, holder_errors = _start_api_lock_holder(api)
        transport.complete()
        observer, observer_finished, outcomes, observer_errors = _start_observer(
            lambda: capture.wait(timeout_s=0.2)
        )
        finished_while_locked = observer_finished.wait(0.3)
        outcomes_while_locked = tuple(outcomes)
        errors_while_locked = tuple(observer_errors)

        # Always release before asserting: a CaptureView.wait implementation
        # that incorrectly blocks on api._lock cannot hang this test process.
        holder_release.set()
        holder.join(_JOIN_TIMEOUT_S)
        observer.join(_JOIN_TIMEOUT_S)
        initiator.join(_JOIN_TIMEOUT_S)

        assert finished_while_locked, "capture.wait blocked on api._lock"
        assert errors_while_locked == ()
        assert len(outcomes_while_locked) == 1
        assert outcomes_while_locked[0].state is CaptureEvaluationState.COMPLETED
        assert holder_errors == []
        assert observer_errors == []
        assert not initiator.is_alive()
        assert not failures
    finally:
        if holder_release is not None:
            holder_release.set()
        if holder is not None:
            holder.join(_JOIN_TIMEOUT_S)
            assert not holder.is_alive(), "API lock holder leaked"
        if observer is not None:
            observer.join(_JOIN_TIMEOUT_S)
            assert not observer.is_alive(), "late-result observer leaked"
        _finish_pending(initiator, transport)
        close_owner(controller, transport)
