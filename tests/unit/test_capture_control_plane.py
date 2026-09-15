from __future__ import annotations

import ast
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import gc
import json
from inspect import getmembers, getsource, isfunction
import os
import re
import subprocess
import sys
from threading import Event, Lock, RLock, Thread, current_thread
from time import monotonic, sleep
from textwrap import dedent
from types import SimpleNamespace
from uuid import uuid4
import weakref

import pytest

from onec_runtime.capture_evaluation import (
    CaptureCleanupLease,
    CaptureEvaluationCoordinator,
    CaptureEvaluationKind,
    CaptureEvaluationOutcome,
    CaptureEvaluationRequest,
    CaptureEvaluationState,
    CaptureEvaluationTicket,
    CaptureFence,
    CapturePhase,
    CaptureRemoteStep,
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
from onec_runtime.observation import ManagerOrigin
from onec_runtime.prototype_runtime import ContinuationAttemptSpec, OperationState
from onec_runtime.rdbg.models import EvaluationResult, PendingEvaluation
from onec_runtime.recovery_journal import RecoveryJournal
from onec_runtime.runtime_api import PrototypeRuntimeApi, RuntimeStatus
from onec_runtime.session import RuntimeSession

from test_capture_evaluation_lifecycle import (
    ControlledCaptureSession,
    close_owner,
    controlled_notebook_runtime,
)
from test_notebook_method_runtime import UPDATE
from test_prototype_runtime import (
    CAPTURE_A,
    TARGET,
    captured_controller,
)
from test_runtime_api import (
    FakeController,
    _common_module_catalog,
    _worker_module_unit,
)


_JOIN_TIMEOUT_S = 2.0


_CAPTURE_DATA_PLANE_ROUTES = frozenset(
    {
        "activate_prepared_main_for_capture",
        "add_worker_breakpoint",
        "begin_continuation_admission",
        "capture_frame",
        "capture_frame_variables",
        "capture_stack",
        "capture_temporary_tables",
        "completion_fields",
        "configure_capture_points",
        "configure_continuation_capture_points",
        "discard_prepared_main_for_capture",
        "execute_bsl",
        "execute_prepared_capture_hypothesis",
        "execute_prepared_main_for_capture",
        "invalidate_capture_inspection",
        "load_worker_modules",
        "materialization_kind",
        "materialize_table",
        "materialize_table_payload",
        "materialize_value",
        "materialize_value_payload",
        "prepare_capture_hypothesis",
        "prepare_capture_ticket",
        "prepare_main_for_capture",
        "project_to_df",
        "project_value",
        "project_value_payload",
        "release_worker_generation",
        "remove_worker_breakpoint",
        "require_public_value_handle",
        "require_public_value_handles",
        "resolve_capture_manager_origin",
        "resume_capture",
        "resume_debug_stop",
        "set_worker_breakpoint_enabled",
    }
)

_CAPTURE_CONTROL_PLANE_ROUTES = frozenset({"current_capture", "status"})

_LOCAL_READ_ONLY_ROUTES = frozenset(
    {
        "activated_main_worker_generation",
        "confirmed_worker_module_units",
        "continuation_admission_is_uncertain",
        "continuation_attempt_evidence",
        "last_worker_breakpoint_reload_report",
        "list_worker_breakpoints",
        "namespace_snapshot",
        "prepared_capture_hypothesis_provenance",
        "prepared_main_execution_provenance",
        "prepared_main_worker_generation",
        "worker_breakpoint_status",
    }
)

_SPECIAL_PUBLIC_ROUTES = frozenset(
    {
        "capture_session_caller_handoff",
        "close",
    }
)


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

    @property
    def owned_by_current_thread(self) -> bool:
        with self._guard:
            return self._owner == current_thread().ident


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


class ShutdownBlockingCaptureSession(ControlledCaptureSession):
    """Hold one acknowledged poll until shutdown invalidates or a test rescues it."""

    def __init__(
        self,
        *,
        wake_on_invalidate: bool,
        result_on_release: bool = False,
        gate_invalidation: bool = False,
    ) -> None:
        super().__init__()
        self.wake_on_invalidate = wake_on_invalidate
        self.result_on_release = result_on_release
        self.gate_invalidation = gate_invalidation
        self.shutdown_poll_entered = Event()
        self.shutdown_poll_release = Event()
        self.shutdown_invalidate_entered = Event()
        self.shutdown_allow_invalidate = Event()
        if not gate_invalidation:
            self.shutdown_allow_invalidate.set()
        self.shutdown_timeline: list[str] = []
        self.shutdown_invalidated = False
        self.shutdown_cleanup_dispatches = 0
        self.shutdown_private_values = (
            'ВызватьИсключение "private BSL expression";',
            "Контекст.__onec_private_value_handle",
            "https://private-user:private-password@target.invalid/rdbg",
            "worker-manifest-" + "f" * 64,
            "private session and target identity",
            "private transport exception text",
        )

    def wait_evaluation_event(
        self,
        pending: PendingEvaluation,
        *,
        timeout_s: float,
    ) -> EvaluationResult:
        del timeout_s
        assert pending is self.capture_pending
        self.shutdown_poll_entered.set()
        if not self.shutdown_poll_release.wait(_JOIN_TIMEOUT_S):
            raise AssertionError("shutdown test did not release the synthetic poll")
        self.shutdown_timeline.append("poll_returned")
        if self.result_on_release:
            return EvaluationResult(pending.result_id, "Число", "901", False)
        raise TargetLost(self.shutdown_private_values[-1])

    def invalidate(self) -> None:
        if self.shutdown_invalidated:
            return
        self.shutdown_invalidated = True
        self.shutdown_timeline.append("transport_invalidated")
        self.shutdown_invalidate_entered.set()
        if not self.shutdown_allow_invalidate.wait(_JOIN_TIMEOUT_S):
            raise AssertionError("shutdown test did not release transport invalidation")
        super().invalidate()
        if self.wake_on_invalidate:
            self.shutdown_poll_release.set()


class _ShutdownCleanupOwnershipToken:
    """Observable state reachable strongly only through its cleanup lease."""

    __slots__ = ("_dispositions", "_timeline", "__weakref__")

    def __init__(
        self,
        dispositions: list[tuple[int, str]],
        timeline: list[str],
    ) -> None:
        self._dispositions = dispositions
        self._timeline = timeline

    def dispose(self, lease_identity: int, disposition: str) -> None:
        self._dispositions.append((lease_identity, disposition))
        self._timeline.append("cleanup_" + disposition)


class _ObservableShutdownCleanupLease(CaptureCleanupLease):
    """A real cleanup lease with a test-only semantic shutdown observer."""

    __slots__ = ("_shutdown_ownership", "__weakref__")

    def __init__(
        self,
        private_key: str,
        cleanup_step: CaptureRemoteStep,
        ownership: _ShutdownCleanupOwnershipToken,
    ) -> None:
        super().__init__(private_key, cleanup_step)
        object.__setattr__(self, "_shutdown_ownership", ownership)

    def dispose_shutdown(self, disposition: str) -> None:
        self._shutdown_ownership.dispose(id(self), disposition)


class _ShutdownCleanupOwnershipProbe:
    """Observe identity, disposition, and retention without owning the lease."""

    def __init__(
        self,
        lease: _ObservableShutdownCleanupLease,
        ownership: _ShutdownCleanupOwnershipToken,
        dispositions: list[tuple[int, str]],
    ) -> None:
        self.lease_identity = id(lease)
        self._lease = weakref.ref(lease)
        self._ownership = weakref.ref(ownership)
        self.dispositions = dispositions

    @property
    def is_product_owned(self) -> bool:
        gc.collect()
        lease = self._lease()
        ownership = self._ownership()
        return lease is not None and ownership is not None


def start_shutdown_evaluation(
    controller: object,
    transport: ShutdownBlockingCaptureSession,
):  # type: ignore[no-untyped-def]
    owner = _capture_owner(controller)

    def dispatch(entered):  # type: ignore[no-untyped-def]
        return transport.start_evaluation(
            transport.shutdown_private_values[0],
            timeout_s=0.05,
            on_transport_dispatch=entered,
        )

    def poll(pending: PendingEvaluation, timeout_s: float):
        return transport.wait_evaluation_event(pending, timeout_s=timeout_s)

    cleanup_pending = PendingEvaluation(TARGET, uuid4(), object())

    def cleanup_dispatch(entered):  # type: ignore[no-untyped-def]
        transport.shutdown_cleanup_dispatches += 1
        transport.shutdown_timeline.append("cleanup_remote_dispatched")
        entered()
        return cleanup_pending

    def cleanup_poll(pending: PendingEvaluation, _timeout_s: float) -> EvaluationResult:
        assert pending is cleanup_pending
        transport.shutdown_timeline.append("cleanup_remote_completed")
        return EvaluationResult(pending.result_id, "Булево", "Истина", False)

    cleanup_dispositions: list[tuple[int, str]] = []
    cleanup_ownership = _ShutdownCleanupOwnershipToken(
        cleanup_dispositions,
        transport.shutdown_timeline,
    )
    cleanup = _ObservableShutdownCleanupLease(
        transport.shutdown_private_values[1],
        CaptureRemoteStep(cleanup_dispatch, cleanup_poll),
        cleanup_ownership,
    )
    cleanup_probe = _ShutdownCleanupOwnershipProbe(
        cleanup,
        cleanup_ownership,
        cleanup_dispositions,
    )

    ticket = owner.submit_evaluation(CaptureEvaluationRequest(
        _owner_fence(owner),
        CaptureEvaluationKind.USER_BSL,
        dispatch,
        poll,
        lambda _result: 901,
        pin_lease=lambda disposition: transport.shutdown_timeline.append(
            "pin_" + disposition
        ),
        cleanup_leases=(cleanup,),
    ))
    assert transport.shutdown_poll_entered.wait(1), "evaluation poll did not start"
    return owner, ticket, cleanup_probe


def _run_close(
    invoke,
    timeline: list[str],
    *,
    daemon: bool = True,
):  # type: ignore[no-untyped-def]
    finished = Event()
    errors: list[BaseException] = []

    def run() -> None:
        try:
            invoke()
        except BaseException as error:
            errors.append(error)
        finally:
            timeline.append("close_returned")
            finished.set()

    thread = Thread(target=run, name="capture-shutdown-caller", daemon=daemon)
    thread.start()
    return thread, finished, errors


def observe_shutdown_control_plane(
    owner: CaptureEvaluationCoordinator,
    timeline: list[str],
) -> list[float]:
    """Record public close/join boundaries without depending on worker storage."""

    original_begin_close = owner.begin_close
    original = owner.join
    timeouts: list[float] = []

    def observed_begin_close() -> None:
        timeline.append("coordinator_closing_entered")
        original_begin_close()

    def observed(timeout_s: float) -> bool:
        timeouts.append(timeout_s)
        timeline.append("event_consumer_join_entered")
        stopped = original(timeout_s)
        timeline.append("event_consumer_join_returned_" + str(stopped).lower())
        return stopped

    owner.begin_close = observed_begin_close  # type: ignore[method-assign]
    owner.join = observed  # type: ignore[method-assign]
    return timeouts


def timeline_contains_order(timeline: tuple[str, ...], *events: str) -> bool:
    if any(event not in timeline for event in events):
        return False
    positions = tuple(timeline.index(event) for event in events)
    return positions == tuple(sorted(positions)) and len(set(positions)) == len(positions)


def attempt_shutdown_submission(
    owner: CaptureEvaluationCoordinator,
) -> BaseException | None:
    try:
        owner.submit_evaluation(CaptureEvaluationRequest(
            _owner_fence(owner),
            CaptureEvaluationKind.INSPECTION,
            lambda _entered: (_ for _ in ()).throw(
                AssertionError("closed coordinator dispatched new work")
            ),
            lambda _pending, _timeout: (_ for _ in ()).throw(
                AssertionError("closed coordinator polled new work")
            ),
            lambda _result: None,
        ))
    except BaseException as error:
        return error
    return None


_SHUTDOWN_EVIDENCE_FIELDS = {
    "evaluation_id",
    "evaluation_kind",
    "termination_proven",
    "elapsed_ms",
    "pin_disposition",
    "cleanup_disposition",
    "cleanup_lease_count",
}


def assert_safe_shutdown_evidence(
    event,
    *,
    event_name: str,
    evaluation_id: str,
    termination_proven: bool,
    pin_disposition: str,
    cleanup_disposition: str,
    started_at: datetime,
    finished_at: datetime,
    private_values: tuple[str, ...],
) -> None:  # type: ignore[no-untyped-def]
    assert event.stream == "capture-evaluation.jsonl"
    assert event.event == event_name
    evidence = event.fields
    assert set(evidence) == _SHUTDOWN_EVIDENCE_FIELDS
    assert evidence["evaluation_id"] == evaluation_id
    assert re.fullmatch(r"[0-9a-f]{32}", str(evidence["evaluation_id"]))
    assert evidence["evaluation_kind"] == CaptureEvaluationKind.USER_BSL.value
    assert evidence["evaluation_kind"] in {
        kind.value for kind in CaptureEvaluationKind
    }
    assert evidence["termination_proven"] is termination_proven
    assert type(evidence["elapsed_ms"]) is int
    assert 0 <= evidence["elapsed_ms"] <= 600_000
    assert evidence["pin_disposition"] == pin_disposition
    assert evidence["pin_disposition"] in {"release", "quarantine", "retained"}
    assert evidence["cleanup_disposition"] == cleanup_disposition
    assert evidence["cleanup_disposition"] in {
        "release",
        "quarantine",
        "retained",
    }
    assert type(evidence["cleanup_lease_count"]) is int
    assert evidence["cleanup_lease_count"] == 1
    recorded_at = datetime.fromisoformat(event.timestamp)
    assert recorded_at.utcoffset() == timedelta(0)
    assert started_at - timedelta(seconds=1) <= recorded_at
    assert recorded_at <= finished_at + timedelta(seconds=1)
    payload = event.as_json()
    assert set(payload) == {"sequence", "timestamp", "event", *evidence}
    assert type(event.sequence) is int and event.sequence > 0
    serialized = json.dumps(payload, ensure_ascii=False)
    for private in private_values:
        assert private not in serialized
        assert private not in repr(event)


def runtime_api_writer_shutdown_probe() -> bool:
    """Run in a watched child so a permanently blocking close cannot hang pytest."""

    transport = ShutdownBlockingCaptureSession(
        wake_on_invalidate=True,
        gate_invalidation=True,
    )
    journal = RecoveryJournal()
    controller = captured_controller(
        transport,
        command_timeout_s=0.05,
        journal=journal,
    )
    api = PrototypeRuntimeApi(controller, journal=journal)
    if os.environ.get("ONEC_TEST_PERMANENT_SHUTDOWN_BLOCK") == "1":
        api.close = lambda: Event().wait()  # type: ignore[method-assign]
    owner, ticket, cleanup_probe = start_shutdown_evaluation(
        controller,
        transport,
    )
    del ticket
    join_timeouts = observe_shutdown_control_plane(
        owner,
        transport.shutdown_timeline,
    )
    if os.environ.get("ONEC_TEST_SHUTDOWN_WITHOUT_JOIN") == "1":
        api.close = lambda: (  # type: ignore[method-assign]
            owner.begin_close(),
            transport.invalidate(),
        )
    holder, release_writer, holder_errors = _start_api_lock_holder(api)
    closer, finished, close_errors = _run_close(
        api.close,
        transport.shutdown_timeline,
        daemon=False,
    )
    caught: BaseException | None = None
    try:
        invalidation_while_writer_owned = transport.shutdown_invalidate_entered.wait(0.4)
        timeline_before_writer_release = tuple(transport.shutdown_timeline)
        caught = attempt_shutdown_submission(owner)
        transport.shutdown_allow_invalidate.set()
        release_writer.set()
        finished_in_deadline = finished.wait(0.6)
        production_timeline = tuple(transport.shutdown_timeline)
        production_join_timeouts = tuple(join_timeouts)
        production_cleanup_dispositions = tuple(cleanup_probe.dispositions)
        cleanup_owned_after_close = cleanup_probe.is_product_owned
        production_cleanup_dispatches = transport.shutdown_cleanup_dispatches
    finally:
        transport.shutdown_allow_invalidate.set()
        transport.shutdown_poll_release.set()
        release_writer.set()
        holder.join(_JOIN_TIMEOUT_S)
        closer.join(_JOIN_TIMEOUT_S)
        owner.begin_close()
        assert owner.join(_JOIN_TIMEOUT_S)

    shutdown_order = (
        "coordinator_closing_entered",
        "transport_invalidated",
        "event_consumer_join_entered",
        "event_consumer_join_returned_true",
        "pin_quarantine",
        "cleanup_quarantine",
        "close_returned",
    )
    return (
        invalidation_while_writer_owned
        and timeline_before_writer_release[:2] == (
            "coordinator_closing_entered",
            "transport_invalidated",
        )
        and isinstance(caught, StaleCaptureError)
        and finished_in_deadline
        and timeline_contains_order(production_timeline, *shutdown_order)
        and all(production_timeline.count(event) == 1 for event in shutdown_order)
        and bool(production_join_timeouts)
        and all(0 <= timeout <= 0.05 for timeout in production_join_timeouts)
        and production_cleanup_dispositions == (
            (cleanup_probe.lease_identity, "quarantine"),
        )
        and cleanup_owned_after_close
        and production_cleanup_dispatches == 0
        and not holder_errors
        and not close_errors
        and not holder.is_alive()
        and not closer.is_alive()
    )


def test_runtime_api_close_with_pending_poll_and_busy_writer_is_process_bounded() -> None:
    code = (
        "import sys; "
        "sys.path.insert(0, 'tests/unit'); "
        "from test_capture_control_plane import runtime_api_writer_shutdown_probe; "
        "raise SystemExit(0 if runtime_api_writer_shutdown_probe() else 1)"
    )
    child = subprocess.Popen([sys.executable, "-c", code])
    timed_out = False
    try:
        try:
            return_code = child.wait(timeout=4)
        except subprocess.TimeoutExpired:
            timed_out = True
            child.terminate()
            try:
                return_code = child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                return_code = child.wait(timeout=2)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=2)

    assert child.poll() is not None, "shutdown watchdog did not reap its child"
    assert not timed_out, "RuntimeApi.close remained permanently blocked"
    assert return_code == 0, "shutdown skipped control-plane work while writer was busy"


def test_shutdown_watchdog_terminates_and_reaps_permanently_blocked_child() -> None:
    code = (
        "import sys; "
        "sys.path.insert(0, 'tests/unit'); "
        "from test_capture_control_plane import runtime_api_writer_shutdown_probe; "
        "raise SystemExit(0 if runtime_api_writer_shutdown_probe() else 1)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code],
        env={**os.environ, "ONEC_TEST_PERMANENT_SHUTDOWN_BLOCK": "1"},
    )
    timed_out = False
    try:
        try:
            child.wait(timeout=0.8)
        except subprocess.TimeoutExpired:
            timed_out = True
            child.terminate()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=2)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=2)

    assert timed_out, "permanent-close mutant unexpectedly escaped the watchdog"
    assert child.poll() is not None, "shutdown watchdog did not reap blocked child"


def test_writer_shutdown_probe_rejects_close_without_join_or_disposition() -> None:
    code = (
        "import sys; "
        "sys.path.insert(0, 'tests/unit'); "
        "from test_capture_control_plane import runtime_api_writer_shutdown_probe; "
        "raise SystemExit(0 if runtime_api_writer_shutdown_probe() else 1)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code],
        env={**os.environ, "ONEC_TEST_SHUTDOWN_WITHOUT_JOIN": "1"},
    )
    timed_out = False
    try:
        try:
            return_code = child.wait(timeout=4)
        except subprocess.TimeoutExpired:
            timed_out = True
            child.terminate()
            try:
                return_code = child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                return_code = child.wait(timeout=2)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=2)

    assert child.poll() is not None, "shutdown mutant watchdog did not reap its child"
    assert not timed_out, "no-join shutdown mutant remained blocked"
    assert return_code == 1, "probe accepted shutdown without join and disposition"


@pytest.mark.parametrize(
    ("result_on_release", "disposition"),
    ((True, "release"), (False, "quarantine")),
)
def test_runtime_api_close_invalidates_and_joins_before_pin_disposition(
    result_on_release: bool,
    disposition: str,
) -> None:
    transport = ShutdownBlockingCaptureSession(
        wake_on_invalidate=True,
        result_on_release=result_on_release,
        gate_invalidation=True,
    )
    journal = RecoveryJournal()
    controller = captured_controller(
        transport,
        command_timeout_s=0.05,
        journal=journal,
    )
    api = PrototypeRuntimeApi(controller, journal=journal)
    owner, ticket, cleanup_probe = start_shutdown_evaluation(controller, transport)
    evaluation_id = ticket.evaluation_id
    del ticket
    join_timeouts = observe_shutdown_control_plane(owner, transport.shutdown_timeline)
    started_at = datetime.now(timezone.utc)
    closer, finished, close_errors = _run_close(
        lambda: (api.close(), api.close()),
        transport.shutdown_timeline,
    )
    caught: BaseException | None = None
    try:
        invalidation_entered = transport.shutdown_invalidate_entered.wait(0.4)
        caught = attempt_shutdown_submission(owner)
        transport.shutdown_allow_invalidate.set()
        finished_in_deadline = finished.wait(0.6)
        finished_at = datetime.now(timezone.utc)
        close_join_timeouts = tuple(join_timeouts)
        timeline_before_rescue = tuple(transport.shutdown_timeline)
        disposed = tuple(
            event
            for event in journal.events
            if event.event == "capture_evaluation_shutdown_disposed"
        )
    finally:
        transport.shutdown_allow_invalidate.set()
        transport.shutdown_poll_release.set()
        owner.begin_close()
        assert owner.join(_JOIN_TIMEOUT_S)
        closer.join(_JOIN_TIMEOUT_S)

    assert finished_in_deadline, "RuntimeApi.close exceeded its configured deadline"
    assert not close_errors
    assert invalidation_entered, "RuntimeApi.close did not invalidate its transport"
    assert close_join_timeouts
    assert all(0 <= timeout <= 0.05 for timeout in close_join_timeouts)
    assert isinstance(caught, StaleCaptureError)
    assert timeline_before_rescue.index("coordinator_closing_entered") < (
        timeline_before_rescue.index("transport_invalidated")
    )
    assert timeline_before_rescue.index("transport_invalidated") < (
        timeline_before_rescue.index("event_consumer_join_entered")
    )
    assert timeline_before_rescue.index("event_consumer_join_returned_true") < (
        timeline_before_rescue.index("pin_" + disposition)
    )
    assert timeline_before_rescue.index("event_consumer_join_returned_true") < (
        timeline_before_rescue.index("cleanup_" + disposition)
    )
    assert timeline_before_rescue.index("pin_" + disposition) < (
        timeline_before_rescue.index("close_returned")
    )
    assert timeline_before_rescue.index("cleanup_" + disposition) < (
        timeline_before_rescue.index("close_returned")
    )
    # Target teardown owns the registered temporary value during shutdown;
    # no cleanup evalExpr may be sent after transport invalidation.
    assert transport.shutdown_cleanup_dispatches == 0
    assert timeline_before_rescue.count("pin_" + disposition) == 1
    assert timeline_before_rescue.count("cleanup_" + disposition) == 1
    assert cleanup_probe.dispositions == [
        (cleanup_probe.lease_identity, disposition)
    ]
    assert cleanup_probe.is_product_owned is (disposition == "quarantine")
    assert len(disposed) == 1
    assert_safe_shutdown_evidence(
        disposed[0],
        event_name="capture_evaluation_shutdown_disposed",
        evaluation_id=evaluation_id,
        termination_proven=True,
        pin_disposition=disposition,
        cleanup_disposition=disposition,
        started_at=started_at,
        finished_at=finished_at,
        private_values=transport.shutdown_private_values,
    )
    assert evaluation_id
    assert not closer.is_alive()


def test_runtime_api_close_journals_unproven_poll_without_releasing_its_lease() -> None:
    transport = ShutdownBlockingCaptureSession(
        wake_on_invalidate=False,
        gate_invalidation=True,
    )
    journal = RecoveryJournal()
    controller = captured_controller(
        transport,
        command_timeout_s=0.05,
        journal=journal,
    )
    api = PrototypeRuntimeApi(controller, journal=journal)
    owner, ticket, cleanup_probe = start_shutdown_evaluation(controller, transport)
    evaluation_id = ticket.evaluation_id
    del ticket
    join_timeouts = observe_shutdown_control_plane(owner, transport.shutdown_timeline)
    started_at = datetime.now(timezone.utc)
    closer, finished, close_errors = _run_close(
        lambda: (api.close(), api.close()),
        transport.shutdown_timeline,
    )
    caught: BaseException | None = None
    try:
        invalidation_entered = transport.shutdown_invalidate_entered.wait(0.4)
        caught = attempt_shutdown_submission(owner)
        transport.shutdown_allow_invalidate.set()
        finished_in_deadline = finished.wait(0.6)
        finished_at = datetime.now(timezone.utc)
        close_join_timeouts = tuple(join_timeouts)
        timeline_before_rescue = tuple(transport.shutdown_timeline)
        status_before_rescue = owner.status(_owner_fence(owner))
        cleanup_owned_before_rescue = cleanup_probe.is_product_owned
        abandoned = tuple(
            event
            for event in journal.events
            if event.event == "capture_evaluation_shutdown_abandoned"
        )
    finally:
        # The product must return without this rescue. The test releases the
        # synthetic uninterruptible transport only to leave pytest thread-clean.
        transport.shutdown_poll_release.set()
        owner.begin_close()
        assert owner.join(_JOIN_TIMEOUT_S)
        closer.join(_JOIN_TIMEOUT_S)

    assert finished_in_deadline, "RuntimeApi.close exceeded its configured deadline"
    assert not close_errors
    assert invalidation_entered, "RuntimeApi.close did not attempt transport invalidation"
    assert isinstance(caught, StaleCaptureError)
    assert close_join_timeouts
    assert all(0 <= timeout <= 0.05 for timeout in close_join_timeouts)
    assert timeline_before_rescue.index("coordinator_closing_entered") < (
        timeline_before_rescue.index("transport_invalidated")
    )
    assert timeline_before_rescue.index("transport_invalidated") < (
        timeline_before_rescue.index("event_consumer_join_entered")
    )
    assert timeline_before_rescue.index("event_consumer_join_entered") < (
        timeline_before_rescue.index("event_consumer_join_returned_false")
    )
    assert timeline_before_rescue.index("event_consumer_join_returned_false") < (
        timeline_before_rescue.index("close_returned")
    )
    assert "pin_release" not in timeline_before_rescue
    assert "pin_quarantine" not in timeline_before_rescue
    assert "cleanup_release" not in timeline_before_rescue
    assert "cleanup_quarantine" not in timeline_before_rescue
    assert cleanup_probe.dispositions == []
    assert cleanup_owned_before_rescue
    assert cleanup_probe.is_product_owned
    assert transport.shutdown_cleanup_dispatches == 0
    assert status_before_rescue.phase is CapturePhase.STALE
    assert status_before_rescue.pending_evaluation_id is None
    for private in transport.shutdown_private_values:
        assert private not in repr(status_before_rescue)
    assert len(abandoned) == 1
    assert_safe_shutdown_evidence(
        abandoned[0],
        event_name="capture_evaluation_shutdown_abandoned",
        evaluation_id=evaluation_id,
        termination_proven=False,
        pin_disposition="retained",
        cleanup_disposition="retained",
        started_at=started_at,
        finished_at=finished_at,
        private_values=transport.shutdown_private_values,
    )
    assert not closer.is_alive()


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


def test_session_releases_both_admission_locks_before_initiator_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second data-plane call must reach coordinator busy admission promptly."""

    api, controller, transport = _capture_runtime()
    runtime = _runtime_session(api)
    owner = _capture_owner(controller)
    initiator: Thread | None = None
    contender: Thread | None = None
    contender_finished = Event()
    contender_errors: list[BaseException] = []
    wait_entered = Event()
    submission_lock_observations: list[bool] = []
    wait_session_lock_observations: list[bool] = []
    wait_api_lock_observations: list[bool] = []
    starts_before = 0
    contender_finished_while_pending = False

    original_submit = owner.submit_evaluation
    original_wait = owner._wait_initiator

    def observed_submit(
        request: CaptureEvaluationRequest,
    ) -> CaptureEvaluationTicket:
        submission_lock_observations.append(
            runtime._operation_lock.owned_by_current_thread
        )
        ticket = original_submit(request)
        submission_lock_observations.append(
            runtime._operation_lock.owned_by_current_thread
        )
        return ticket

    def observed_wait(
        record: object,
        timeout_s: float | None = None,
    ) -> object:
        wait_session_lock_observations.append(
            runtime._operation_lock.owned_by_current_thread
        )
        acquired_api_lock = api._lock.acquire(blocking=False)
        wait_api_lock_observations.append(acquired_api_lock)
        if acquired_api_lock:
            api._lock.release()
        wait_entered.set()
        return original_wait(record, timeout_s)  # type: ignore[arg-type]

    def contend() -> None:
        try:
            runtime.execute_bsl("ВтораяИнструкция = 2;")
        except BaseException as error:
            contender_errors.append(error)
        finally:
            contender_finished.set()

    try:
        with monkeypatch.context() as patch:
            patch.setattr(owner, "submit_evaluation", observed_submit)
            patch.setattr(owner, "_wait_initiator", observed_wait)
            initiator, initiator_finished, initiator_errors = _start_pending_capture(
                lambda: runtime.execute_bsl("РезультатИнструкции = 901;"),
                transport.accepted,
            )
            assert wait_entered.wait(1), "initiator did not enter wait_initiator"
        assert not initiator_finished.is_set()

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

    assert submission_lock_observations == [True, True], (
        "RuntimeSession did not hold its short admission lock across submission"
    )
    assert wait_session_lock_observations == [False], (
        "RuntimeSession held admission through wait_initiator"
    )
    assert wait_api_lock_observations == [True], (
        "RuntimeApi held the writer through wait_initiator"
    )
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
    (
        "runtime.status",
        "runtime.current_capture",
        "api.current_capture",
        "capture.status",
        "capture.wait",
    ),
)
def test_capture_control_plane_does_not_acquire_runtime_api_lock(
    endpoint: str,
) -> None:
    """A blocking api._lock mutant must fail promptly and still tear down."""

    api, controller, transport = _capture_runtime()
    runtime = _runtime_session(api)
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
            "runtime.status": runtime.status,
            "runtime.current_capture": runtime.current_capture,
            "api.current_capture": api.current_capture,
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
        elif endpoint == "runtime.status":
            assert isinstance(results[0], RuntimeStatus)
            assert results[0].state is OperationState.EVALUATING_CAPTURE
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


def test_capture_submission_keeps_runtime_writer_until_ticket_is_adopted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, controller, transport = _capture_runtime()
    owner = _capture_owner(controller)
    original_submit = owner.submit_evaluation
    observations: list[tuple[bool, bool]] = []
    initiator: Thread | None = None

    def observed_submit(
        request: CaptureEvaluationRequest,
    ) -> CaptureEvaluationTicket:
        acquired = api._lock.acquire(blocking=False)
        observations.append(
            (api._writer_owner == current_thread().ident, acquired)
        )
        if acquired:
            api._lock.release()
        return original_submit(request)

    try:
        monkeypatch.setattr(owner, "submit_evaluation", observed_submit)
        initiator, _finished, failures = _start_pending_capture(
            lambda: api.execute_bsl("РезультатИнструкции = 901;"),
            transport.accepted,
        )
        assert failures == []
    finally:
        _finish_pending(initiator, transport)
        close_owner(controller, transport)

    assert observations == [(True, False)]


def test_runtime_status_uses_paused_coordinator_during_real_presubmit_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, controller, transport = _capture_runtime()
    owner = _capture_owner(controller)
    original_submit = owner.submit_evaluation
    submit_entered = Event()
    allow_submit = Event()
    initiator: Thread | None = None

    def delayed_submit(
        request: CaptureEvaluationRequest,
    ) -> CaptureEvaluationTicket:
        submit_entered.set()
        assert allow_submit.wait(_JOIN_TIMEOUT_S)
        return original_submit(request)

    @contextmanager
    def forbidden_writer():
        raise AssertionError("capture status entered RuntimeApi single-writer")
        yield

    try:
        monkeypatch.setattr(owner, "submit_evaluation", delayed_submit)
        failures: list[BaseException] = []

        def initiate() -> None:
            try:
                api.execute_bsl("РезультатИнструкции = 901;")
            except BaseException as error:
                failures.append(error)

        initiator = Thread(target=initiate, name="capture-presubmit-initiator")
        initiator.start()
        assert submit_entered.wait(1), "coordinator submission was not reached"
        assert controller.state is OperationState.EVALUATING_CAPTURE
        assert owner.status(owner._fence).phase is CapturePhase.PAUSED

        with monkeypatch.context() as patch:
            patch.setattr(api, "_single_writer", forbidden_writer)
            patch.setattr(
                api,
                "_require_available",
                lambda: (_ for _ in ()).throw(
                    AssertionError("capture status called _require_available")
                ),
            )
            status = api.status()

        assert status.state is OperationState.CAPTURED
        assert status.runtime_generation == owner._fence.capture_generation
        assert status.operation_id == owner._fence.operation_id
        assert failures == []
    finally:
        allow_submit.set()
        if transport.accepted.wait(1) and transport.capture_pending is not None:
            transport.complete()
        if initiator is not None:
            initiator.join(_JOIN_TIMEOUT_S)
            assert not initiator.is_alive(), "pre-submit initiator leaked"
        close_owner(controller, transport)


def test_prepared_reentry_reports_the_active_capture_evaluation(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    api, controller, transport = controlled_notebook_runtime(tmp_path)
    initiator: Thread | None = None
    try:
        assert api.execute_bsl(UPDATE).succeeded
        assert api.execute_bsl("Результат = Б();").kind.value == "captured"
        prepared = api.prepare_capture_hypothesis(
            "РезультатИнструкции = Б();"
        )
        initiator, _finished, failures = _start_pending_capture(
            lambda: api.execute_bsl("РезультатИнструкции = Б();"),
            transport.accepted,
        )
        pending_id = api.current_capture().status().pending_evaluation_id
        starts_before = transport.capture_start_count

        with pytest.raises(CaptureBusyError) as caught:
            api.execute_prepared_capture_hypothesis(prepared)

        assert pending_id is not None
        assert caught.value.evaluation_id == pending_id
        assert transport.capture_start_count == starts_before
        assert failures == []
    finally:
        _finish_pending(initiator, transport)
        close_owner(controller, transport)


def test_resume_is_rejected_during_capture_completion_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, controller, transport = _capture_runtime()
    owner = _capture_owner(controller)
    original_finalize = api._finalize_namespace_reply
    original_resume = controller.resume
    completion_entered = Event()
    release_completion = Event()
    resume_calls: list[dict[str, object]] = []
    initiator: Thread | None = None

    def blocked_finalize(*args, **kwargs):  # type: ignore[no-untyped-def]
        completion_entered.set()
        assert release_completion.wait(_JOIN_TIMEOUT_S)
        return original_finalize(*args, **kwargs)

    def observed_resume(**kwargs):  # type: ignore[no-untyped-def]
        resume_calls.append(kwargs)
        raise AssertionError("resume entered during CAPTURE completion")

    try:
        monkeypatch.setattr(api, "_finalize_namespace_reply", blocked_finalize)
        monkeypatch.setattr(controller, "resume", observed_resume)
        initiator, _finished, failures = _start_pending_capture(
            lambda: api.execute_bsl("РезультатИнструкции = 901;"),
            transport.accepted,
        )
        pending_id = api.current_capture().status().pending_evaluation_id
        transport.complete()
        assert completion_entered.wait(1), "completion barrier was not reached"
        assert controller.state is OperationState.CAPTURED
        assert owner.status(owner._fence).phase is CapturePhase.EVALUATING

        with pytest.raises(CaptureBusyError) as caught:
            api.resume_capture()

        assert pending_id is not None
        assert caught.value.evaluation_id == pending_id
        assert resume_calls == []
        assert failures == []
    finally:
        release_completion.set()
        if initiator is not None:
            initiator.join(_JOIN_TIMEOUT_S)
            assert not initiator.is_alive(), "completion initiator leaked"
        monkeypatch.setattr(api, "_finalize_namespace_reply", original_finalize)
        monkeypatch.setattr(controller, "resume", original_resume)
        close_owner(controller, transport)


def test_session_prepared_capture_releases_admission_lock_while_waiting(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    api, controller, transport = controlled_notebook_runtime(tmp_path)
    initiator: Thread | None = None
    contender: Thread | None = None
    try:
        assert api.execute_bsl(UPDATE).succeeded
        assert api.execute_bsl("Результат = Б();").kind.value == "captured"
        controller.command_timeout_s = 2.0
        prepared = api.prepare_capture_hypothesis(
            "РезультатИнструкции = Б();"
        )
        fence = SimpleNamespace(
            capture_intent_id="intent",
            operation_id=controller.operation_id,
            capture_generation=controller.runtime_generation,
            source_revision=1,
            source_sha256="synthetic",
            stop_sequence=controller.stop_sequence,
        )
        runtime = object.__new__(RuntimeSession)
        runtime.runtime_api = api
        runtime._operation_lock = RLock()
        runtime._closed = False
        runtime._active_capture_ticket = fence
        runtime.config = SimpleNamespace(chunk_size=128)

        first_errors: list[BaseException] = []
        second_errors: list[BaseException] = []
        second_done = Event()

        def first() -> None:
            try:
                runtime.execute_prepared_capture_hypothesis(prepared, fence)
            except BaseException as error:
                first_errors.append(error)

        def second() -> None:
            try:
                runtime.execute_bsl("РезультатИнструкции = Б();")
            except BaseException as error:
                second_errors.append(error)
            finally:
                second_done.set()

        transport.accepted.clear()
        initiator = Thread(target=first, name="prepared-capture-initiator")
        initiator.start()
        assert transport.accepted.wait(1), "prepared evaluation was not accepted"
        pending_id = api.current_capture().status().pending_evaluation_id
        starts_before = transport.capture_start_count
        transport.accepted.clear()

        contender = Thread(target=second, name="prepared-capture-contender")
        contender.start()
        contender_finished_while_pending = second_done.wait(0.2)

        if not contender_finished_while_pending:
            transport.complete()
            initiator.join(_JOIN_TIMEOUT_S)
            if transport.accepted.wait(1) and transport.capture_pending is not None:
                transport.complete()
        elif transport.capture_pending is not None:
            transport.complete()
        initiator.join(_JOIN_TIMEOUT_S)
        contender.join(_JOIN_TIMEOUT_S)

        assert contender_finished_while_pending, (
            "prepared CAPTURE held the Session admission lock through wait"
        )
        assert pending_id is not None
        assert first_errors == []
        assert len(second_errors) == 1
        assert isinstance(second_errors[0], CaptureBusyError)
        assert second_errors[0].evaluation_id == pending_id
        assert transport.capture_start_count == starts_before
        assert not initiator.is_alive()
        assert not contender.is_alive()
    finally:
        if transport.capture_pending is not None:
            transport.complete()
        if initiator is not None:
            initiator.join(_JOIN_TIMEOUT_S)
        if contender is not None:
            contender.join(_JOIN_TIMEOUT_S)
        close_owner(controller, transport)


@pytest.mark.parametrize("route", ("hypothesis", "rearm", "ticket"))
def test_capture_stateful_preparation_respects_completion_reservation(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:  # type: ignore[no-untyped-def]
    api, controller, transport = controlled_notebook_runtime(tmp_path)
    original_finalize = api._finalize_namespace_reply
    completion_entered = Event()
    release_completion = Event()
    rearm_calls: list[tuple[object, ...]] = []
    initiator: Thread | None = None

    def blocked_finalize(*args, **kwargs):  # type: ignore[no-untyped-def]
        completion_entered.set()
        assert release_completion.wait(_JOIN_TIMEOUT_S)
        return original_finalize(*args, **kwargs)

    def observed_rearm(points):  # type: ignore[no-untyped-def]
        rearm_calls.append(tuple(points))
        raise AssertionError("rearm entered during CAPTURE completion")

    try:
        assert api.execute_bsl(UPDATE).succeeded
        assert api.execute_bsl("Результат = Б();").kind.value == "captured"
        owner = _capture_owner(controller)
        monkeypatch.setattr(api, "_finalize_namespace_reply", blocked_finalize)
        monkeypatch.setattr(controller, "rearm_capture_successor", observed_rearm)
        initiator, _finished, failures = _start_pending_capture(
            lambda: api.execute_bsl("РезультатИнструкции = Б();"),
            transport.accepted,
        )
        pending_id = api.current_capture().status().pending_evaluation_id
        prepared_before = tuple(api._prepared_source_units)
        ticket_before = api._capture_ticket
        transport.complete()
        assert completion_entered.wait(1), "completion barrier was not reached"
        assert controller.state is OperationState.CAPTURED
        assert owner.status(owner._fence).phase is CapturePhase.EVALUATING

        with pytest.raises(CaptureBusyError) as caught:
            if route == "hypothesis":
                api.prepare_capture_hypothesis("РезультатИнструкции = Б();")
            elif route == "rearm":
                api.configure_continuation_capture_points((CAPTURE_A,))
            else:
                api.prepare_capture_ticket()

        assert pending_id is not None
        assert caught.value.evaluation_id == pending_id
        assert tuple(api._prepared_source_units) == prepared_before
        assert api._capture_ticket is ticket_before
        assert rearm_calls == []
        assert failures == []
    finally:
        release_completion.set()
        if initiator is not None:
            initiator.join(_JOIN_TIMEOUT_S)
            assert not initiator.is_alive(), "stateful-route initiator leaked"
        close_owner(controller, transport)


def test_busy_session_resume_preserves_the_active_capture_ticket() -> None:
    api, controller, transport = _capture_runtime()
    runtime = _runtime_session(api)
    fence = SimpleNamespace(
        ticket_id="capture-ticket",
        capture_intent_id="intent",
        operation_id="operation",
        capture_generation=controller.runtime_generation,
        source_revision=1,
        source_sha256="synthetic",
        stop_sequence=controller.stop_sequence,
    )
    runtime._active_capture_ticket = fence
    ended: list[object] = []
    runtime._capture_resume_listeners = [ended.append]
    initiator: Thread | None = None
    try:
        initiator, _finished, failures = _start_pending_capture(
            lambda: api.execute_bsl("РезультатИнструкции = 901;"),
            transport.accepted,
        )
        pending_id = api.current_capture().status().pending_evaluation_id

        with pytest.raises(CaptureBusyError) as caught:
            runtime.resume_capture()

        assert pending_id is not None
        assert caught.value.evaluation_id == pending_id
        assert runtime._active_capture_ticket is fence
        assert ended == []
        runtime._require_capture_fence(fence)
        assert failures == []
    finally:
        _finish_pending(initiator, transport)
        close_owner(controller, transport)


def test_continuation_admission_respects_capture_completion_reservation() -> None:
    api, controller, transport = _capture_runtime()
    owner = _capture_owner(controller)
    original_finalize = api._finalize_namespace_reply
    completion_entered = Event()
    release_completion = Event()
    initiator: Thread | None = None

    def blocked_finalize(*args, **kwargs):  # type: ignore[no-untyped-def]
        completion_entered.set()
        assert release_completion.wait(_JOIN_TIMEOUT_S)
        return original_finalize(*args, **kwargs)

    try:
        api._finalize_namespace_reply = blocked_finalize  # type: ignore[method-assign]
        initiator, _finished, failures = _start_pending_capture(
            lambda: api.execute_bsl("РезультатИнструкции = 901;"),
            transport.accepted,
        )
        pending_id = api.current_capture().status().pending_evaluation_id
        transport.complete()
        assert completion_entered.wait(1), "completion barrier was not reached"
        assert controller.state is OperationState.CAPTURED
        assert owner.status(owner._fence).phase is CapturePhase.EVALUATING

        registry_before = controller.registry
        controller_points_before = controller.capture_points
        workspace_before = controller._breakpoint_workspace
        confirmed_before = controller.breakpoint_workspace_owner.confirmed_snapshot
        attempts_before = dict(controller._continuation_attempts)
        active_attempt_before = controller._active_continuation_attempt_id
        journal_before = controller.journal.events
        workspace_calls_before = transport.workspace_call_count
        api_points_before = api._capture_points
        ticket_before = api._capture_ticket
        attempt = ContinuationAttemptSpec(
            "task5-r3-completion",
            max(1, controller.stop_sequence),
            "task5-r3-request",
            (),
        )

        with pytest.raises(CaptureBusyError) as caught:
            api.begin_continuation_admission(attempt, (CAPTURE_A,))

        assert pending_id is not None
        assert caught.value.evaluation_id == pending_id
        assert controller.registry is registry_before
        assert controller.capture_points == controller_points_before
        assert controller._breakpoint_workspace == workspace_before
        assert controller.breakpoint_workspace_owner.confirmed_snapshot == confirmed_before
        assert controller._continuation_attempts == attempts_before
        assert controller._active_continuation_attempt_id == active_attempt_before
        assert controller.journal.events == journal_before
        assert transport.workspace_call_count == workspace_calls_before
        assert api._capture_points == api_points_before
        assert api._capture_ticket is ticket_before
        assert failures == []
    finally:
        release_completion.set()
        api._finalize_namespace_reply = original_finalize  # type: ignore[method-assign]
        if initiator is not None:
            initiator.join(_JOIN_TIMEOUT_S)
            assert not initiator.is_alive(), "continuation initiator leaked"
        close_owner(controller, transport)


@pytest.mark.parametrize("route", ("add", "remove", "disable"))
def test_worker_breakpoint_mutation_respects_capture_completion_reservation(
    tmp_path,
    route: str,
) -> None:  # type: ignore[no-untyped-def]
    api, controller, transport = controlled_notebook_runtime(tmp_path)
    original_finalize = api._finalize_namespace_reply
    completion_entered = Event()
    release_completion = Event()
    initiator: Thread | None = None

    def blocked_finalize(*args, **kwargs):  # type: ignore[no-untyped-def]
        completion_entered.set()
        assert release_completion.wait(_JOIN_TIMEOUT_S)
        return original_finalize(*args, **kwargs)

    try:
        assert api.execute_bsl(UPDATE).succeeded
        assert api.execute_bsl("Результат = Б();").kind.value == "captured"
        owner = _capture_owner(controller)
        view = api._worker_universe._retained_debug_views()[0]
        module = view.modules[0]
        existing_id = None
        if route != "add":
            existing_id = api.add_worker_breakpoint(
                module.source_unit,
                module.canonical_module,
                2,
            ).breakpoint.id

        api._finalize_namespace_reply = blocked_finalize  # type: ignore[method-assign]
        initiator, _finished, failures = _start_pending_capture(
            lambda: api.execute_bsl("РезультатИнструкции = Б();"),
            transport.accepted,
        )
        pending_id = api.current_capture().status().pending_evaluation_id
        transport.complete()
        assert completion_entered.wait(1), "completion barrier was not reached"
        assert controller.state is OperationState.CAPTURED
        assert owner.status(owner._fence).phase is CapturePhase.EVALUATING

        catalog_before = api._worker_breakpoints.snapshot()
        statuses_before = api.list_worker_breakpoints()
        workspace_before = controller.breakpoint_workspace_owner.confirmed_snapshot
        physical_calls_before = transport.workspace_call_count

        with pytest.raises(CaptureBusyError) as caught:
            if route == "add":
                api.add_worker_breakpoint(
                    module.source_unit,
                    module.canonical_module,
                    3,
                )
            elif route == "remove":
                assert existing_id is not None
                api.remove_worker_breakpoint(existing_id)
            else:
                assert existing_id is not None
                api.set_worker_breakpoint_enabled(existing_id, False)

        assert pending_id is not None
        assert caught.value.evaluation_id == pending_id
        assert api._worker_breakpoints.snapshot() == catalog_before
        assert api.list_worker_breakpoints() == statuses_before
        assert controller.breakpoint_workspace_owner.confirmed_snapshot == workspace_before
        assert transport.workspace_call_count == physical_calls_before
        assert failures == []
    finally:
        release_completion.set()
        api._finalize_namespace_reply = original_finalize  # type: ignore[method-assign]
        if initiator is not None:
            initiator.join(_JOIN_TIMEOUT_S)
            assert not initiator.is_alive(), "breakpoint initiator leaked"
        close_owner(controller, transport)


def _invoke_capture_data_plane_route(
    api: PrototypeRuntimeApi,
    route: str,
) -> None:
    if route == "activate_prepared_main_for_capture":
        api.activate_prepared_main_for_capture(object())
    elif route == "add_worker_breakpoint":
        api.add_worker_breakpoint(object(), "Модуль", 1)  # type: ignore[arg-type]
    elif route == "begin_continuation_admission":
        api.begin_continuation_admission(
            ContinuationAttemptSpec("inventory", 1, "request", ()),
            (CAPTURE_A,),
        )
    elif route == "capture_frame":
        api.capture_frame(level=0, cursor=0, limit=1)
    elif route == "capture_frame_variables":
        api.capture_frame_variables(filters={}, cursor=0, limit=1)
    elif route == "capture_stack":
        api.capture_stack(cursor=0, limit=1)
    elif route == "capture_temporary_tables":
        api.capture_temporary_tables(
            "capture_manager_inventory",
            names=None,
            cursor=0,
            limit=1,
            selection=None,
        )
    elif route == "completion_fields":
        api.completion_fields("Контекст.Результат")
    elif route == "configure_capture_points":
        api.configure_capture_points((CAPTURE_A,))
    elif route == "configure_continuation_capture_points":
        api.configure_continuation_capture_points((CAPTURE_A,))
    elif route == "discard_prepared_main_for_capture":
        api.discard_prepared_main_for_capture(object())
    elif route == "execute_bsl":
        api.execute_bsl("РезультатИнструкции = 902;")
    elif route == "execute_prepared_capture_hypothesis":
        api.execute_prepared_capture_hypothesis(object())
    elif route == "execute_prepared_main_for_capture":
        api.execute_prepared_main_for_capture(object())
    elif route == "invalidate_capture_inspection":
        api.invalidate_capture_inspection()
    elif route == "load_worker_modules":
        api.load_worker_modules((), common_modules=object())  # type: ignore[arg-type]
    elif route == "materialization_kind":
        api.materialization_kind("Контекст.Результат")
    elif route == "materialize_table":
        api.materialize_table("Контекст.Результат")
    elif route == "materialize_table_payload":
        api.materialize_table_payload("Контекст.Результат")
    elif route == "materialize_value":
        api.materialize_value("Контекст.Результат")
    elif route == "materialize_value_payload":
        api.materialize_value_payload(
            "Контекст.Результат",
            max_depth=1,
            max_items=1,
            max_bytes=1024,
        )
    elif route == "prepare_capture_hypothesis":
        api.prepare_capture_hypothesis("РезультатИнструкции = 902;")
    elif route == "prepare_capture_ticket":
        api.prepare_capture_ticket()
    elif route == "prepare_main_for_capture":
        api.prepare_main_for_capture("Результат = 902;")
    elif route == "project_to_df":
        api.project_to_df("Контекст.Результат", {"offset": 0, "limit": 1})
    elif route == "project_to_df_invalid_selection":
        api.project_to_df("Контекст.Результат", {"offset": 0, "limit": 0})
    elif route == "project_value":
        api.project_value("Контекст.Результат", {"offset": 0, "limit": 1})
    elif route == "project_value_invalid_selection":
        api.project_value("Контекст.Результат", {})
    elif route == "project_value_payload":
        api.project_value_payload(
            "Контекст.Результат",
            kind="slice",
            offset=0,
            limit=1,
            columns=(),
            names=(),
            max_depth=1,
            max_items=1,
            max_rows=1,
            max_bytes=1024,
        )
    elif route == "release_worker_generation":
        api.release_worker_generation(object())  # type: ignore[arg-type]
    elif route == "remove_worker_breakpoint":
        api.remove_worker_breakpoint(uuid4())
    elif route == "require_public_value_handle":
        api.require_public_value_handle("Контекст.Результат")
    elif route == "require_public_value_handles":
        api.require_public_value_handles(("Контекст.Результат",))
    elif route == "resolve_capture_manager_origin":
        api.resolve_capture_manager_origin(ManagerOrigin("frame", "Запрос", ()))
    elif route == "resume_capture":
        api.resume_capture()
    elif route == "resume_debug_stop":
        api.resume_debug_stop()
    elif route == "set_worker_breakpoint_enabled":
        api.set_worker_breakpoint_enabled(uuid4(), False)
    else:  # pragma: no cover - the exhaustive classification test owns this fence
        raise AssertionError(f"missing data-plane route invocation: {route}")


def test_public_runtime_routes_have_an_exhaustive_capture_classification() -> None:
    public_methods = {
        name
        for name, value in getmembers(PrototypeRuntimeApi, isfunction)
        if not name.startswith("_")
    }
    classified = (
        _CAPTURE_DATA_PLANE_ROUTES
        | _CAPTURE_CONTROL_PLANE_ROUTES
        | _LOCAL_READ_ONLY_ROUTES
        | _SPECIAL_PUBLIC_ROUTES
    )

    assert public_methods == classified
    assert not (
        (_CAPTURE_DATA_PLANE_ROUTES & _CAPTURE_CONTROL_PLANE_ROUTES)
        or (_CAPTURE_DATA_PLANE_ROUTES & _LOCAL_READ_ONLY_ROUTES)
        or (_CAPTURE_DATA_PLANE_ROUTES & _SPECIAL_PUBLIC_ROUTES)
        or (_CAPTURE_CONTROL_PLANE_ROUTES & _LOCAL_READ_ONLY_ROUTES)
        or (_CAPTURE_CONTROL_PLANE_ROUTES & _SPECIAL_PUBLIC_ROUTES)
        or (_LOCAL_READ_ONLY_ROUTES & _SPECIAL_PUBLIC_ROUTES)
    )


@pytest.mark.parametrize("route", sorted(_CAPTURE_DATA_PLANE_ROUTES))
def test_capture_data_plane_admission_is_the_first_effectful_step(route: str) -> None:
    method = getattr(PrototypeRuntimeApi, route)
    if route == "prepare_main_for_capture":
        method = PrototypeRuntimeApi._begin_prepared_operation_pin
    parsed = ast.parse(dedent(getsource(method)))
    function = parsed.body[0]
    assert isinstance(function, ast.FunctionDef)
    statements = list(function.body)
    if (
        statements
        and isinstance(statements[0], ast.Expr)
        and isinstance(statements[0].value, ast.Constant)
        and isinstance(statements[0].value.value, str)
    ):
        statements.pop(0)
    while statements and isinstance(statements[0], ast.Delete):
        statements.pop(0)

    assert statements, f"{route} has no admission boundary"
    boundary = statements[0]
    assert isinstance(boundary, ast.With), (
        f"{route} performs work before capture data-plane admission"
    )
    first_context = boundary.items[0].context_expr
    assert isinstance(first_context, ast.Call)
    assert isinstance(first_context.func, ast.Attribute)
    assert isinstance(first_context.func.value, ast.Name)
    assert first_context.func.value.id == "self"
    assert first_context.func.attr == "_capture_data_plane_writer"


@pytest.mark.parametrize(
    "route",
    (
        *sorted(_CAPTURE_DATA_PLANE_ROUTES),
        "project_to_df_invalid_selection",
        "project_value_invalid_selection",
    ),
)
def test_every_public_capture_data_plane_route_respects_completion_reservation(
    route: str,
) -> None:
    api, controller, transport = _capture_runtime()
    owner = _capture_owner(controller)
    original_finalize = api._finalize_namespace_reply
    original_require_available = api._require_available
    original_invalidate = controller.invalidate_capture_inspection
    completion_entered = Event()
    release_completion = Event()
    forbidden_calls: list[str] = []
    initiator: Thread | None = None

    def blocked_finalize(*args, **kwargs):  # type: ignore[no-untyped-def]
        completion_entered.set()
        assert release_completion.wait(_JOIN_TIMEOUT_S)
        return original_finalize(*args, **kwargs)

    def forbidden_available() -> None:
        forbidden_calls.append("require_available")
        raise AssertionError("data-plane route passed coordinator admission")

    def forbidden_invalidate() -> None:
        forbidden_calls.append("invalidate_capture_inspection")
        raise AssertionError("capture invalidation passed coordinator admission")

    try:
        api._finalize_namespace_reply = blocked_finalize  # type: ignore[method-assign]
        initiator, _finished, failures = _start_pending_capture(
            lambda: api.execute_bsl("РезультатИнструкции = 902;"),
            transport.accepted,
        )
        pending_status = api.current_capture().status()
        transport.complete()
        assert completion_entered.wait(1), "completion barrier was not reached"
        assert controller.state is OperationState.CAPTURED
        assert owner.status(owner._fence).phase is CapturePhase.EVALUATING
        dispatches_before = transport.capture_start_count
        workspace_calls_before = transport.workspace_call_count
        api._require_available = forbidden_available  # type: ignore[method-assign]
        controller.invalidate_capture_inspection = forbidden_invalidate  # type: ignore[method-assign]

        with pytest.raises(CaptureBusyError) as caught:
            _invoke_capture_data_plane_route(api, route)

        assert pending_status.pending_evaluation_id is not None
        assert caught.value.evaluation_id == pending_status.pending_evaluation_id
        assert caught.value.evaluation_kind is CaptureEvaluationKind.USER_BSL
        assert caught.value.phase is CapturePhase.EVALUATING
        assert forbidden_calls == []
        assert transport.capture_start_count == dispatches_before
        assert transport.workspace_call_count == workspace_calls_before
        assert failures == []
    finally:
        api._require_available = original_require_available  # type: ignore[method-assign]
        controller.invalidate_capture_inspection = original_invalidate  # type: ignore[method-assign]
        release_completion.set()
        api._finalize_namespace_reply = original_finalize  # type: ignore[method-assign]
        if initiator is not None:
            initiator.join(_JOIN_TIMEOUT_S)
            assert not initiator.is_alive(), f"{route} initiator leaked"
        close_owner(controller, transport)


@pytest.mark.parametrize("route", ("load", "release"))
def test_worker_lifecycle_respects_capture_completion_reservation(
    tmp_path,
    route: str,
) -> None:  # type: ignore[no-untyped-def]
    api, controller, transport = controlled_notebook_runtime(tmp_path)
    original_finalize = api._finalize_namespace_reply
    original_publish = api._publish_worker_artifacts_locked
    original_release = api._release_worker_lifecycle_locked
    completion_entered = Event()
    release_completion = Event()
    lifecycle_calls: list[str] = []
    initiator: Thread | None = None

    def blocked_finalize(*args, **kwargs):  # type: ignore[no-untyped-def]
        completion_entered.set()
        assert release_completion.wait(_JOIN_TIMEOUT_S)
        return original_finalize(*args, **kwargs)

    def forbidden_publish(*args, **kwargs):  # type: ignore[no-untyped-def]
        lifecycle_calls.append("publish")
        raise AssertionError("Worker publication passed coordinator admission")

    def forbidden_release(*args, **kwargs):  # type: ignore[no-untyped-def]
        lifecycle_calls.append("release")
        raise AssertionError("Worker release passed coordinator admission")

    try:
        assert api.execute_bsl(UPDATE).succeeded
        catalog = _common_module_catalog("МодульА", "МодульБ")
        module_a = _worker_module_unit("МодульА", 17, catalog)
        module_b = _worker_module_unit("МодульБ", 17, catalog)
        handle = api.load_worker_modules((module_a,), common_modules=catalog)
        assert api.execute_bsl("Результат = Б();").kind.value == "captured"
        owner = _capture_owner(controller)
        api._finalize_namespace_reply = blocked_finalize  # type: ignore[method-assign]
        initiator, _finished, failures = _start_pending_capture(
            lambda: api.execute_bsl("РезультатИнструкции = Б();"),
            transport.accepted,
        )
        pending_status = api.current_capture().status()
        transport.complete()
        assert completion_entered.wait(1), "completion barrier was not reached"
        assert controller.state is OperationState.CAPTURED
        assert owner.status(owner._fence).phase is CapturePhase.EVALUATING

        generation_before = api.worker_generation_handle
        api_owned_before = api._api_owned_worker_generation_handle
        active_modules_before = dict(api._worker_active_modules)
        artifacts_before = dict(api._worker_module_artifacts)
        catalog_before = api._worker_catalog_snapshot
        retained_before = api._worker_universe._retained_debug_views()
        breakpoints_before = api._worker_breakpoints.snapshot()
        workspace_before = controller.breakpoint_workspace_owner.confirmed_snapshot
        workspace_calls_before = transport.workspace_call_count
        api._publish_worker_artifacts_locked = forbidden_publish  # type: ignore[method-assign]
        api._release_worker_lifecycle_locked = forbidden_release  # type: ignore[method-assign]

        with pytest.raises(CaptureBusyError) as caught:
            if route == "load":
                api.load_worker_modules((module_b,), common_modules=catalog)
            else:
                api.release_worker_generation(handle)

        assert pending_status.pending_evaluation_id is not None
        assert caught.value.evaluation_id == pending_status.pending_evaluation_id
        assert caught.value.evaluation_kind is CaptureEvaluationKind.USER_BSL
        assert caught.value.phase is CapturePhase.EVALUATING
        assert lifecycle_calls == []
        assert api.worker_generation_handle is generation_before
        assert api._api_owned_worker_generation_handle is api_owned_before
        assert api._worker_active_modules == active_modules_before
        assert api._worker_module_artifacts == artifacts_before
        assert api._worker_catalog_snapshot is catalog_before
        assert api._worker_universe._retained_debug_views() == retained_before
        assert api._worker_breakpoints.snapshot() == breakpoints_before
        assert controller.breakpoint_workspace_owner.confirmed_snapshot == workspace_before
        assert transport.workspace_call_count == workspace_calls_before
        assert failures == []
    finally:
        api._publish_worker_artifacts_locked = original_publish  # type: ignore[method-assign]
        api._release_worker_lifecycle_locked = original_release  # type: ignore[method-assign]
        release_completion.set()
        api._finalize_namespace_reply = original_finalize  # type: ignore[method-assign]
        if initiator is not None:
            initiator.join(_JOIN_TIMEOUT_S)
            assert not initiator.is_alive(), f"Worker {route} initiator leaked"
        close_owner(controller, transport)
