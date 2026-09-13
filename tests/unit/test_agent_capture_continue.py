"""One-use continuation contract tests.

The remaining scenarios are deliberately expressed through the public facade:
no debugger identity or runtime ticket may cross this boundary.
"""

from __future__ import annotations

from threading import Event, Thread

from onec_runtime_mcp.agent.contracts import ServiceResponse
from onec_runtime_mcp.agent.capture_contracts import (
    CaptureFence,
    CapturePointRequest,
    CaptureView,
    ResolvedCapturePoint,
)
from onec_runtime_mcp.agent.capture_service import (
    CaptureArming,
    CaptureContinuationEvidence,
    CaptureRunOutcome,
    CaptureService,
    CaptureStop,
)
from onec_runtime_mcp.agent.contracts import AgentOperationState, BackendExecution
from onec_runtime_mcp.agent.facade import AgentFacade
from onec_runtime_mcp.agent.proxies import (
    ProxyProvenance,
    ProxyRegistry,
    ReleasedProxy,
    StaleProxy,
)
from onec_runtime_mcp.agent.contracts import CapabilityMode
from onec_runtime_mcp.agent.service import AgentWorkspaceService, _AdmittedRuntime
import pytest


def _request() -> dict[str, object]:
    return {
        "fence": {
            "capture_intent_id": "capture-intent",
            "operation_id": "op-capture",
            "source_revision": 7,
            "source_sha256": "a" * 64,
            "capture_generation": 1,
            "stop_sequence": 1,
        },
        "next_points": [],
        "observe": {"items": []},
        "request_id": "continue-request-1",
        "wait_s": 2.0,
    }


def test_facade_routes_one_fenced_continuation_without_private_runtime_identity() -> None:
    # Break caught: routing continuation through a generic execution API would
    # lose its public capture fence or leak debugger-only correlation evidence.
    class Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def call(self, method: str, arguments: dict[str, object]) -> ServiceResponse:
            self.calls.append((method, arguments))
            return ServiceResponse.success(
                {
                    "operation": {
                        "operation_id": "op-continue",
                        "kind": "capture_continue",
                        "runtime_id": "runtime-1",
                        "runtime_generation": 1,
                        "cell_id": None,
                        "revision": None,
                        "source_sha256": None,
                    },
                    "state": "completed",
                    "messages": [],
                    "next_message_cursor": 0,
                    "next_event_cursor": 1,
                    "changed_variables": [],
                    "change_confidence": "unknown",
                    "outputs": {},
                    "capture": None,
                    "failure": None,
                    "recovery": [],
                    "truncation": {
                        "messages": False,
                        "changed_variables": False,
                        "outputs": False,
                    },
                }
            )

    client = Client()
    view = AgentFacade(client).capture_continue(_request())

    assert view.operation.kind.value == "capture_continue"
    assert client.calls[0][0] == "capture.continue"
    assert client.calls[0][1]["fence"] == _request()["fence"]
    assert set(client.calls[0][1]) == {
        "fence", "next_points", "observe", "request_id", "wait_s"
    }


class _ContinuationRuntime:
    def __init__(self) -> None:
        self.writeback_roots: tuple[str, ...] = ()
        self.continue_calls = 0
        self.armed: list[object] = []

    def require_public_value_handle(self, handle: str) -> None:
        del handle

    def resolve_capture_points(
        self, points: tuple[CapturePointRequest, ...]
    ) -> tuple[ResolvedCapturePoint, ...]:
        return tuple(
            ResolvedCapturePoint(
                point.name, "zup", "Payroll", "Run", 23, 7, "a" * 64, 23, "Выполнить();"
            )
            for point in points
        )

    def arm_capture(self, intent: object) -> CaptureArming:
        self.armed.append(intent)
        return CaptureArming("private-ticket", 42, 2)

    def prepare_capture_successor(self, intent, *, attempt):  # type: ignore[no-untyped-def]
        runtime = self

        class Admission:
            arming = None if intent is None else runtime.arm_capture(intent)

            def commit(self) -> None:
                return None

            def rollback(self) -> None:
                return None

            def quarantine(self) -> None:
                return None

        if intent is None:
            self.rearm_capture_successor(())
        return Admission()

    def frame_variables(self, capture, *, filters, cursor, limit, timeout_s=None):  # type: ignore[no-untyped-def]
        assert capture == _fence()
        return {
            "items": ({"name": "Сумма", "type_name": "Число", "role": "local", "handle": "frame-sum"},),
            "total": 1,
            "next_cursor": None,
        }

    def continue_capture(
        self, *, dirty_roots: tuple[str, ...], attempt_id: str | None = None
    ) -> CaptureRunOutcome:
        self.writeback_roots = dirty_roots
        self.continue_calls += 1
        location = ResolvedCapturePoint(
            "after", "zup", "Payroll", "Run", 23, 7, "a" * 64, 23, "Выполнить();"
        )
        return CaptureRunOutcome(
            BackendExecution(AgentOperationState.CAPTURED, (), False, "captured"),
            CaptureStop(2, location, 42, "private-ticket", 42),
            continuation=CaptureContinuationEvidence(
                tuple((root, "succeeded") for root in dirty_roots),
                "acknowledged",
            ),
        )

    def rearm_capture_successor(self, points: tuple[CapturePointRequest, ...]) -> None:
        assert points == ()

    def disarm_capture(self, *, policy: str) -> None:
        return None


class _Factory:
    def start(self, *, mode):  # type: ignore[no-untyped-def]
        raise AssertionError("continuation must use the already selected runtime")


def _fence() -> CaptureFence:
    return CaptureFence("capture-intent", "op-capture", 7, "a" * 64, 1, 1)


def _capture() -> CaptureView:
    return CaptureView(
        _fence(),
        ResolvedCapturePoint("before", "zup", "Payroll", "Run", 17, 7, "a" * 64, 17, "Выполнить();"),
        None,
        dirty_roots=("Сумма",),
    )


def test_continue_flushes_staged_roots_once_and_invalidates_old_frame_handles(tmp_path) -> None:
    # Break caught: retaining the original capture after Continue would make a
    # stale frame proxy look live, or a retry could send the same write twice.
    runtime = _ContinuationRuntime()
    registry = ProxyRegistry()
    service = CaptureService(tmp_path)
    service.activate_capture_view(_capture(), registry)
    old_proxy = service.inspect(
        runtime, registry, fence=_fence(), runtime_id="runtime-1", runtime_generation=1,
        context_generation=1, filters={}, cursor=0, limit=20,
    ).variables[0].proxy_id

    result = service.continue_capture(
        runtime, registry, fence=_fence(), operation_id="op-continue",
        next_points=(CapturePointRequest("after", "zup", "Payroll", "Run", 23),),
    )

    assert runtime.writeback_roots == ("Сумма",)
    assert runtime.continue_calls == 1
    assert result.capture is not None
    assert result.capture.fence.capture_generation == 2
    with __import__("pytest").raises(StaleProxy):
        registry.resolve(old_proxy)


def test_ambiguous_continue_is_unknown_and_the_durable_request_is_never_resent(tmp_path) -> None:
    # Break caught: a frontend retry after an ambiguous transport result must
    # return the original UNKNOWN operation rather than send Continue again.
    runtime = _ContinuationRuntime()

    def lose_transport(*, dirty_roots, attempt_id=None):  # type: ignore[no-untyped-def]
        runtime.continue_calls += 1
        raise OSError("frontend disconnected after send")

    runtime.continue_capture = lose_transport  # type: ignore[method-assign]
    service = AgentWorkspaceService(tmp_path, _Factory(), maximum_mode=CapabilityMode.EXPERIMENT)
    service._runtime = _AdmittedRuntime(runtime, "runtime-1", 1, CapabilityMode.EXPERIMENT)  # type: ignore[arg-type]
    service._selected["default"] = "runtime-1"
    service._capture.activate_capture_view(_capture(), service._proxy_registry)
    request = {**_request(), "next_points": [], "wait_s": 2.0}
    try:
        first = service.call("capture.continue", request)
        second = service.call("capture.continue", request)

        assert first.ok and second.ok, (first.failure, second.failure)
        assert first.value.state is second.value.state is AgentOperationState.UNKNOWN
        assert first.value.failure == {
            "stage": "capture_transport",
            "partial_results": {"Сумма": "outcome_unknown"},
            "continue_state": "outcome_unknown",
        }
        assert runtime.continue_calls == 1
    finally:
        service.close()


def test_continue_request_journal_failure_terminates_submission_and_releases_lane(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _ContinuationRuntime()
    service = AgentWorkspaceService(
        tmp_path, _Factory(), maximum_mode=CapabilityMode.EXPERIMENT
    )
    service._runtime = _AdmittedRuntime(  # type: ignore[assignment]
        runtime, "runtime-1", 1, CapabilityMode.EXPERIMENT
    )
    service._selected["default"] = "runtime-1"
    service._capture.activate_capture_view(_capture(), service._proxy_registry)
    gates: list[object] = []
    original_startup = service._startup_operation_id

    def traced_startup(gate, holder):  # type: ignore[no-untyped-def]
        gates.append(gate)
        return original_startup(gate, holder)

    original_record = service._capture.record_request
    failures = [OSError("request journal write failed after submit")]

    def fail_once(request_id: str, fingerprint: str, operation_id: str) -> None:
        if failures:
            raise failures.pop()
        original_record(request_id, fingerprint, operation_id)

    monkeypatch.setattr(service, "_startup_operation_id", traced_startup)
    monkeypatch.setattr(service._capture, "record_request", fail_once)
    request = {**_request(), "request_id": "journal-failure-continue", "wait_s": 0.05}
    try:
        failed = service.call("capture.continue", request)
        follow_up = service._operations.submit(
            {
                "operation_kind": "code_run",
                "runtime_id": "runtime-1",
                "runtime_generation": 1,
                "code_id": "after-journal-failure",
                "revision": 1,
                "source_sha256": "f" * 64,
                "inputs_sha256": "follow-up-continue",
            },
            lambda: BackendExecution(
                AgentOperationState.COMPLETED, (), False, "ready"
            ),
        )
        follow_up = service._operations.wait(follow_up.operation_id, 0.05)

        assert (failed.ok, follow_up.state) == (
            True,
            AgentOperationState.COMPLETED,
        )
        assert failed.value.state is AgentOperationState.FAILED
        assert failed.value.failure == {
            "stage": "capture_request_journal",
            "partial_results": {},
        }
        assert runtime.continue_calls == 0
        replay = service.call("capture.continue", request)
        assert replay.ok
        assert replay.value.operation.operation_id == failed.value.operation.operation_id
        assert replay.value.state is AgentOperationState.FAILED
        assert runtime.continue_calls == 0
    finally:
        for gate in gates:
            gate.set()  # type: ignore[attr-defined]
        service.close()


def _root_names(count: int) -> tuple[str, ...]:
    return tuple(f"Корень{index}" for index in range(count))


def test_dirty_root_staging_deduplicates_before_enforcing_the_100_root_bound(
    tmp_path,
) -> None:
    service = CaptureService(tmp_path)
    roots = _root_names(100)
    service.activate_capture_view(
        CaptureView(_fence(), _capture().location, None, dirty_roots=roots)
    )

    duplicate = service.stage_dirty_roots(_fence(), (roots[0].swapcase(),))

    assert duplicate.dirty_roots == roots


def test_dirty_root_staging_rejects_oversized_first_or_merged_batch_atomically(
    tmp_path,
) -> None:
    service = CaptureService(tmp_path)
    service.activate_capture_view(CaptureView(_fence(), _capture().location, None))

    with pytest.raises(ValueError, match="exceeds 100"):
        service.stage_dirty_roots(_fence(), _root_names(101))
    assert service.current_capture(_fence()).dirty_roots == ()

    staged = service.stage_dirty_roots(_fence(), _root_names(100))
    assert len(staged.dirty_roots) == 100
    with pytest.raises(ValueError, match="exceeds 100"):
        service.stage_dirty_roots(_fence(), ("ЕщеОдин",))
    assert service.current_capture(_fence()).dirty_roots == _root_names(100)


def test_continuation_admission_rejects_preexisting_oversized_dirty_state_before_arm(
    tmp_path,
) -> None:
    runtime = _ContinuationRuntime()
    service = CaptureService(tmp_path)
    service.activate_capture_view(
        CaptureView(_fence(), _capture().location, None, dirty_roots=_root_names(101))
    )

    with pytest.raises(ValueError, match="exceeds 100"):
        service.continue_capture(
            runtime,
            ProxyRegistry(),
            fence=_fence(),
            operation_id="op-continue",
            next_points=(),
        )

    assert runtime.armed == []
    assert runtime.continue_calls == 0


class _SuccessorAdmission:
    def __init__(
        self,
        events: list[str],
        *,
        arming: CaptureArming | None,
        rollback_fails: bool = False,
    ) -> None:
        self.events = events
        self.arming = arming
        self.rollback_fails = rollback_fails

    def commit(self) -> None:
        self.events.append("commit")

    def rollback(self) -> None:
        self.events.append("rollback")
        if self.rollback_fails:
            raise OSError("rollback transport is uncertain")

    def quarantine(self) -> None:
        self.events.append("quarantine")


class _TransactionalContinuationRuntime(_ContinuationRuntime):
    def __init__(
        self,
        *,
        arming: CaptureArming | None,
        rollback_fails: bool = False,
    ) -> None:
        super().__init__()
        self.events: list[str] = []
        self.admission = _SuccessorAdmission(
            self.events, arming=arming, rollback_fails=rollback_fails
        )

    def arm_capture(self, intent: object) -> CaptureArming:
        raise AssertionError("continuation must use one transactional admission")

    def prepare_capture_successor(self, intent, *, attempt):  # type: ignore[no-untyped-def]
        assert attempt.capture_generation == _fence().capture_generation
        assert attempt.request_operation_id
        assert attempt.dirty_roots == ("Сумма",)
        self.events.append("prepare")
        return self.admission

    def continue_capture(self, *, dirty_roots, attempt_id=None):  # type: ignore[no-untyped-def]
        self.events.append("continue")
        if self.admission.arming is None:
            self.writeback_roots = dirty_roots
            self.continue_calls += 1
            return CaptureRunOutcome(
                BackendExecution(
                    AgentOperationState.COMPLETED, (), False, "completed"
                ),
                continuation=CaptureContinuationEvidence(
                    tuple((root, "succeeded") for root in dirty_roots),
                    "acknowledged",
                ),
            )
        return super().continue_capture(dirty_roots=dirty_roots)


def test_successor_arm_and_service_journal_are_one_admission_transaction(
    tmp_path,
) -> None:
    runtime = _TransactionalContinuationRuntime(
        arming=CaptureArming("private-ticket", 42, 2)
    )
    service = CaptureService(tmp_path)
    registry = ProxyRegistry()
    service.activate_capture_view(_capture(), registry)

    result = service.continue_capture(
        runtime,
        registry,
        fence=_fence(),
        operation_id="op-continue",
        next_points=(CapturePointRequest("after", "zup", "Payroll", "Run", 23),),
    )

    assert result.capture is not None
    assert runtime.events == ["prepare", "continue", "commit"]


def test_wrong_successor_ticket_after_continue_quarantines_instead_of_committing(
    tmp_path,
) -> None:
    runtime = _TransactionalContinuationRuntime(
        arming=CaptureArming("private-ticket", 42, 2)
    )

    def wrong_ticket(*, dirty_roots, attempt_id=None):  # type: ignore[no-untyped-def]
        runtime.events.append("continue")
        location = ResolvedCapturePoint(
            "after", "zup", "Payroll", "Run", 23, 7, "a" * 64, 23, "Выполнить();"
        )
        return CaptureRunOutcome(
            BackendExecution(AgentOperationState.CAPTURED, (), False, "captured"),
            CaptureStop(2, location, 42, "wrong-ticket", 42),
            continuation=CaptureContinuationEvidence(
                tuple((root, "succeeded") for root in dirty_roots),
                "acknowledged",
            ),
        )

    runtime.continue_capture = wrong_ticket  # type: ignore[method-assign]
    service = CaptureService(tmp_path)
    service.activate_capture_view(_capture())

    result = service.continue_capture(
        runtime,
        ProxyRegistry(),
        fence=_fence(),
        operation_id="op-continue",
        next_points=(CapturePointRequest("after", "zup", "Payroll", "Run", 23),),
    )

    assert result.execution.terminal_state is AgentOperationState.UNKNOWN
    assert result.quarantine_runtime is True
    assert result.failure is not None
    assert result.failure["stage"] == "capture_correlation"
    assert result.failure["continue_state"] == "acknowledged"
    assert runtime.events == ["prepare", "continue", "quarantine"]


def test_invalid_successor_evidence_rolls_back_every_layer_and_keeps_handles_live(
    tmp_path,
) -> None:
    runtime = _TransactionalContinuationRuntime(
        arming=CaptureArming("", 42, 2)
    )
    service = CaptureService(tmp_path)
    registry = ProxyRegistry()
    service.activate_capture_view(_capture(), registry)
    proxy_id = service.inspect(
        runtime,
        registry,
        fence=_fence(),
        runtime_id="runtime-1",
        runtime_generation=1,
        context_generation=1,
        filters={},
        cursor=0,
        limit=20,
    ).variables[0].proxy_id

    result = service.continue_capture(
        runtime,
        registry,
        fence=_fence(),
        operation_id="op-continue",
        next_points=(CapturePointRequest("after", "zup", "Payroll", "Run", 23),),
    )

    assert result.execution.terminal_state is AgentOperationState.FAILED
    assert runtime.events == ["prepare", "rollback"]
    assert service.current_capture(_fence()).fence == _fence()
    assert registry.resolve(proxy_id).proxy_id == proxy_id


def test_inspection_racing_pre_send_rollback_never_returns_an_erased_proxy(
    tmp_path,
) -> None:
    """A returned frame proxy and unrelated publication must survive rollback."""
    prepare_entered = Event()
    release_prepare = Event()
    inspect_started = Event()
    inspect_finished = Event()
    runtime = _TransactionalContinuationRuntime(
        arming=CaptureArming("", 42, 2)
    )
    original_prepare = runtime.prepare_capture_successor

    def blocked_prepare(intent, *, attempt):  # type: ignore[no-untyped-def]
        admission = original_prepare(intent, attempt=attempt)
        prepare_entered.set()
        assert release_prepare.wait(2), "test did not release successor admission"
        return admission

    runtime.prepare_capture_successor = blocked_prepare  # type: ignore[method-assign]
    service = CaptureService(tmp_path)
    registry = ProxyRegistry()
    service.activate_capture_view(_capture(), registry)
    release_candidate = registry.register_frame(
        qualified_name="capture.release-candidate",
        type_name="Число",
        runtime_id="runtime-1",
        runtime_generation=1,
        context_generation=1,
        capture_fence=_fence(),
        provenance=ProxyProvenance(
            "capture", 7, "a" * 64, "op-capture"
        ),
        resolver_handle="release-handle",
    )
    continuation_results: list[object] = []
    continuation_errors: list[BaseException] = []
    inspected_proxy_ids: list[str] = []
    inspection_errors: list[BaseException] = []

    def continue_worker() -> None:
        try:
            continuation_results.append(
                service.continue_capture(
                    runtime,
                    registry,
                    fence=_fence(),
                    operation_id="op-continue",
                    next_points=(
                        CapturePointRequest(
                            "after", "zup", "Payroll", "Run", 23
                        ),
                    ),
                )
            )
        except BaseException as error:
            continuation_errors.append(error)

    def inspect_worker() -> None:
        inspect_started.set()
        try:
            inspected_proxy_ids.append(
                service.inspect(
                    runtime,
                    registry,
                    fence=_fence(),
                    runtime_id="runtime-1",
                    runtime_generation=1,
                    context_generation=1,
                    filters={},
                    cursor=0,
                    limit=20,
                ).variables[0].proxy_id
            )
        except BaseException as error:
            inspection_errors.append(error)
        finally:
            inspect_finished.set()

    continuation = Thread(target=continue_worker)
    continuation.start()
    assert prepare_entered.wait(2), "continuation did not reach successor admission"

    unrelated = registry.register_python(
        qualified_name="python.concurrent",
        type_name="int",
        python_generation=1,
        provenance=ProxyProvenance(
            "python-cell", 1, "b" * 64, "python-operation"
        ),
        resolver_handle="python-handle",
    )
    assert registry.release(release_candidate.proxy_id) is True
    inspection = Thread(target=inspect_worker)
    inspection.start()
    assert inspect_started.wait(2)
    # Without a capture admission boundary this completes before rollback and
    # the wholesale registry restore erases the descriptor already returned.
    assert not inspect_finished.wait(0.25), "inspection bypassed capture admission"
    release_prepare.set()
    continuation.join(timeout=2)
    inspection.join(timeout=2)

    assert not continuation.is_alive(), "continuation admission deadlocked"
    assert not inspection.is_alive(), "capture inspection deadlocked"
    assert continuation_errors == []
    assert inspection_errors == []
    assert len(continuation_results) == len(inspected_proxy_ids) == 1
    assert continuation_results[0].execution.terminal_state is AgentOperationState.FAILED
    assert registry.resolve(inspected_proxy_ids[0]).proxy_id == inspected_proxy_ids[0]
    assert registry.resolve(unrelated.proxy_id).proxy_id == unrelated.proxy_id
    with pytest.raises(ReleasedProxy):
        registry.resolve(release_candidate.proxy_id)


def test_inspection_racing_arming_journal_failure_never_publishes_erased_proxy(
    tmp_path,
) -> None:
    journal_entered = Event()
    release_journal = Event()
    inspect_started = Event()
    inspect_finished = Event()
    runtime = _TransactionalContinuationRuntime(
        arming=CaptureArming("private-ticket", 42, 2)
    )
    service = CaptureService(tmp_path)
    registry = ProxyRegistry()
    service.activate_capture_view(_capture(), registry)

    def fail_arming_journal(intent, arming):  # type: ignore[no-untyped-def]
        journal_entered.set()
        assert release_journal.wait(2), "test did not release arming journal"
        raise OSError("planned arming journal failure")

    service._journal_arming = fail_arming_journal  # type: ignore[method-assign]
    continuation_results: list[object] = []
    continuation_errors: list[BaseException] = []
    inspected_proxy_ids: list[str] = []
    inspection_errors: list[BaseException] = []

    def continue_worker() -> None:
        try:
            continuation_results.append(
                service.continue_capture(
                    runtime,
                    registry,
                    fence=_fence(),
                    operation_id="op-continue",
                    next_points=(
                        CapturePointRequest(
                            "after", "zup", "Payroll", "Run", 23
                        ),
                    ),
                )
            )
        except BaseException as error:
            continuation_errors.append(error)

    def inspect_worker() -> None:
        inspect_started.set()
        try:
            inspected_proxy_ids.append(
                service.inspect(
                    runtime,
                    registry,
                    fence=_fence(),
                    runtime_id="runtime-1",
                    runtime_generation=1,
                    context_generation=1,
                    filters={},
                    cursor=0,
                    limit=20,
                ).variables[0].proxy_id
            )
        except BaseException as error:
            inspection_errors.append(error)
        finally:
            inspect_finished.set()

    continuation = Thread(target=continue_worker)
    continuation.start()
    assert journal_entered.wait(2), "continuation did not reach arming journal"
    unrelated = registry.register_python(
        qualified_name="python.during-journal",
        type_name="int",
        python_generation=1,
        provenance=ProxyProvenance(
            "python-cell", 1, "c" * 64, "python-operation"
        ),
        resolver_handle="python-journal-handle",
    )
    inspection = Thread(target=inspect_worker)
    inspection.start()
    assert inspect_started.wait(2)
    assert not inspect_finished.wait(0.25), "inspection bypassed capture admission"
    release_journal.set()
    continuation.join(timeout=2)
    inspection.join(timeout=2)

    assert not continuation.is_alive(), "continuation journal path deadlocked"
    assert not inspection.is_alive(), "capture inspection deadlocked"
    assert continuation_errors == []
    assert len(continuation_results) == 1
    assert continuation_results[0].execution.terminal_state is AgentOperationState.UNKNOWN
    assert continuation_results[0].quarantine_runtime is True
    assert inspected_proxy_ids == []
    assert len(inspection_errors) == 1
    assert isinstance(inspection_errors[0], StaleProxy)
    assert registry.resolve(unrelated.proxy_id).proxy_id == unrelated.proxy_id
    assert runtime.events == ["prepare", "quarantine"]


def test_missing_continuation_evidence_is_unknown_and_preserves_known_root_statuses(
    tmp_path,
) -> None:
    runtime = _TransactionalContinuationRuntime(
        arming=CaptureArming("private-ticket", 42, 2)
    )

    def missing_evidence(*, dirty_roots, attempt_id=None):  # type: ignore[no-untyped-def]
        runtime.events.append("continue")
        return CaptureRunOutcome(
            BackendExecution(AgentOperationState.UNKNOWN, (), False, "unknown"),
            partial_results={"Сумма": "succeeded"},
            continuation=None,
        )

    runtime.continue_capture = missing_evidence  # type: ignore[method-assign]
    service = CaptureService(tmp_path)
    registry = ProxyRegistry()
    service.activate_capture_view(_capture(), registry)

    result = service.continue_capture(
        runtime,
        registry,
        fence=_fence(),
        operation_id="op-continue",
        next_points=(CapturePointRequest("after", "zup", "Payroll", "Run", 23),),
    )

    assert result.execution.terminal_state is AgentOperationState.UNKNOWN
    assert result.quarantine_runtime is True
    assert result.failure == {
        "stage": "capture_transport",
        "partial_results": {"Сумма": "succeeded"},
        "continue_state": "outcome_unknown",
    }
    assert [action.method for action in result.recovery] == [
        "workspace.status",
        "operation.wait",
        "runtime.close",
        "runtime.restart",
    ]
    assert runtime.events == ["prepare", "continue", "quarantine"]


def test_incomplete_acknowledgement_evidence_is_malformed_and_never_publishes_ack(
    tmp_path,
) -> None:
    runtime = _TransactionalContinuationRuntime(
        arming=CaptureArming("private-ticket", 42, 2)
    )

    def incomplete_ack(*, dirty_roots, attempt_id=None):  # type: ignore[no-untyped-def]
        runtime.events.append("continue")
        return CaptureRunOutcome(
            BackendExecution(AgentOperationState.COMPLETED, (), False, "completed"),
            partial_results={"Сумма": "succeeded"},
            continuation=CaptureContinuationEvidence((), "acknowledged"),
        )

    runtime.continue_capture = incomplete_ack  # type: ignore[method-assign]
    service = CaptureService(tmp_path)
    registry = ProxyRegistry()
    service.activate_capture_view(_capture(), registry)

    result = service.continue_capture(
        runtime,
        registry,
        fence=_fence(),
        operation_id="op-continue",
        next_points=(CapturePointRequest("after", "zup", "Payroll", "Run", 23),),
    )

    assert result.execution.terminal_state is AgentOperationState.UNKNOWN
    assert result.quarantine_runtime is True
    assert result.failure == {
        "stage": "capture_transport",
        "partial_results": {"Сумма": "succeeded"},
        "continue_state": "outcome_unknown",
    }
    assert runtime.events == ["prepare", "continue", "quarantine"]


def _unchecked_continuation_evidence(
    root_statuses: tuple[tuple[object, object], ...],
) -> CaptureContinuationEvidence:
    """Model corrupt controller evidence that crossed the typed adapter boundary."""
    evidence = object.__new__(CaptureContinuationEvidence)
    object.__setattr__(evidence, "root_statuses", root_statuses)
    object.__setattr__(evidence, "continue_state", "acknowledged")
    return evidence


@pytest.mark.parametrize(
    ("case", "root_statuses"),
    [
        (
            "reversed",
            (("Порог", "succeeded"), ("Сумма", "succeeded")),
        ),
        (
            "duplicate",
            (
                ("Сумма", "succeeded"),
                ("Порог", "succeeded"),
                ("сумма", "succeeded"),
            ),
        ),
        ("missing", (("Сумма", "succeeded"),)),
        (
            "extra",
            (
                ("Сумма", "succeeded"),
                ("Порог", "succeeded"),
                ("Лишний", "succeeded"),
            ),
        ),
        (
            "malformed",
            (("Сумма", "succeeded"), (42, "succeeded")),
        ),
        (
            "malformed_status",
            (("Сумма", ["succeeded"]), ("Порог", "succeeded")),
        ),
    ],
)
def test_continuation_ack_requires_exact_casefolded_ordered_root_evidence(
    tmp_path,
    case: str,
    root_statuses: tuple[tuple[object, object], ...],
) -> None:
    # Break caught: set equality used to accept reordered/duplicated roots and
    # malformed controller evidence could escape as an exception instead of
    # quarantining the ambiguous continuation attempt.
    runtime = _ContinuationRuntime()
    runtime.quarantined = False  # type: ignore[attr-defined]

    def prepare_capture_successor(intent, *, attempt):  # type: ignore[no-untyped-def]
        assert intent is None
        assert attempt.dirty_roots == ("Сумма", "Порог")

        class Admission:
            arming = None

            def commit(self) -> None:
                raise AssertionError(f"{case} evidence must not commit")

            def rollback(self) -> None:
                raise AssertionError(f"{case} evidence must not roll back")

            def quarantine(self) -> None:
                runtime.quarantined = True  # type: ignore[attr-defined]

        return Admission()

    def malformed_ack(*, dirty_roots, attempt_id=None):  # type: ignore[no-untyped-def]
        runtime.continue_calls += 1
        return CaptureRunOutcome(
            BackendExecution(AgentOperationState.COMPLETED, (), False, "completed"),
            continuation=_unchecked_continuation_evidence(root_statuses),
        )

    runtime.prepare_capture_successor = prepare_capture_successor  # type: ignore[method-assign]
    runtime.continue_capture = malformed_ack  # type: ignore[method-assign]
    service = CaptureService(tmp_path)
    service.activate_capture_view(
        CaptureView(
            _fence(),
            _capture().location,
            None,
            dirty_roots=("Сумма", "Порог"),
        )
    )

    result = service.continue_capture(
        runtime,
        ProxyRegistry(),
        fence=_fence(),
        operation_id="op-continue",
        next_points=(),
    )

    assert result.execution.terminal_state is AgentOperationState.UNKNOWN
    assert result.quarantine_runtime is True
    assert result.failure is not None
    assert result.failure["continue_state"] == "outcome_unknown"
    assert runtime.quarantined is True  # type: ignore[attr-defined]
    assert runtime.continue_calls == 1


def test_continuation_ack_accepts_casefolded_roots_only_in_staged_order(
    tmp_path,
) -> None:
    runtime = _ContinuationRuntime()

    def exact_ack(*, dirty_roots, attempt_id=None):  # type: ignore[no-untyped-def]
        return CaptureRunOutcome(
            BackendExecution(AgentOperationState.COMPLETED, (), False, "completed"),
            continuation=CaptureContinuationEvidence(
                (("сумма", "succeeded"), ("ПОРОГ", "succeeded")),
                "acknowledged",
            ),
        )

    runtime.continue_capture = exact_ack  # type: ignore[method-assign]
    service = CaptureService(tmp_path)
    service.activate_capture_view(
        CaptureView(
            _fence(),
            _capture().location,
            None,
            dirty_roots=("Сумма", "Порог"),
        )
    )

    result = service.continue_capture(
        runtime,
        ProxyRegistry(),
        fence=_fence(),
        operation_id="op-continue",
        next_points=(),
    )

    assert result.execution.terminal_state is AgentOperationState.COMPLETED
    assert runtime.continue_calls == 0


class _ExplodingPartialResults(dict[object, object]):
    def items(self):  # type: ignore[override]
        raise OSError("corrupt coarse continuation evidence")


@pytest.mark.parametrize(
    "partial_results",
    [
        {"Порог": "succeeded", "Сумма": "succeeded"},
        {
            "Сумма": "succeeded",
            "Порог": "succeeded",
            "Лишний": "succeeded",
        },
        {42: "succeeded", "Порог": "succeeded"},
        {"Сумма": ["succeeded"], "Порог": "succeeded"},
        {"Сумма": "invented", "Порог": "succeeded"},
        _ExplodingPartialResults(),
    ],
    ids=(
        "reordered",
        "extra",
        "malformed-key",
        "unhashable-status",
        "unknown-status",
        "iteration-error",
    ),
)
def test_malformed_coarse_partial_results_after_continue_always_quarantine(
    tmp_path,
    partial_results: object,
) -> None:
    # The active capture is invalidated before backend continuation. Corrupt
    # coarse evidence must therefore return UNKNOWN, never escape as an
    # exception that bypasses admission quarantine.
    runtime = _ContinuationRuntime()
    runtime.quarantined = False  # type: ignore[attr-defined]

    def prepare_capture_successor(intent, *, attempt):  # type: ignore[no-untyped-def]
        assert intent is None

        class Admission:
            arming = None

            def commit(self) -> None:
                raise AssertionError("malformed coarse evidence must not commit")

            def rollback(self) -> None:
                raise AssertionError("malformed coarse evidence must not roll back")

            def quarantine(self) -> None:
                runtime.quarantined = True  # type: ignore[attr-defined]

        return Admission()

    def corrupt(*, dirty_roots, attempt_id=None):  # type: ignore[no-untyped-def]
        return CaptureRunOutcome(
            BackendExecution(AgentOperationState.COMPLETED, (), False, "completed"),
            partial_results=partial_results,  # type: ignore[arg-type]
            continuation=CaptureContinuationEvidence(
                (("Сумма", "succeeded"), ("Порог", "succeeded")),
                "acknowledged",
            ),
        )

    runtime.prepare_capture_successor = prepare_capture_successor  # type: ignore[method-assign]
    runtime.continue_capture = corrupt  # type: ignore[method-assign]
    service = CaptureService(tmp_path)
    service.activate_capture_view(
        CaptureView(
            _fence(),
            _capture().location,
            None,
            dirty_roots=("Сумма", "Порог"),
        )
    )

    result = service.continue_capture(
        runtime,
        ProxyRegistry(),
        fence=_fence(),
        operation_id="op-continue",
        next_points=(),
    )

    assert result.execution.terminal_state is AgentOperationState.UNKNOWN
    assert result.quarantine_runtime is True
    assert result.failure is not None
    assert result.failure["continue_state"] == "outcome_unknown"
    assert runtime.quarantined is True  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "terminal_state",
    [AgentOperationState.FAILED, AgentOperationState.UNKNOWN],
)
def test_sent_continuation_evidence_is_normalized_to_unknown(
    tmp_path,
    terminal_state: AgentOperationState,
) -> None:
    runtime = _TransactionalContinuationRuntime(
        arming=CaptureArming("private-ticket", 42, 2)
    )

    def unacknowledged(*, dirty_roots, attempt_id=None):  # type: ignore[no-untyped-def]
        runtime.events.append("continue")
        return CaptureRunOutcome(
            BackendExecution(terminal_state, (), False, "transport_failed"),
            continuation=CaptureContinuationEvidence(
                (("Сумма", "succeeded"),),
                "sent",
            ),
        )

    runtime.continue_capture = unacknowledged  # type: ignore[method-assign]
    service = CaptureService(tmp_path)
    registry = ProxyRegistry()
    service.activate_capture_view(_capture(), registry)

    result = service.continue_capture(
        runtime,
        registry,
        fence=_fence(),
        operation_id="op-continue",
        next_points=(CapturePointRequest("after", "zup", "Payroll", "Run", 23),),
    )

    assert result.execution.terminal_state is AgentOperationState.UNKNOWN
    assert result.quarantine_runtime is True
    assert result.failure == {
        "stage": "capture_transport",
        "partial_results": {"Сумма": "succeeded"},
        "continue_state": "outcome_unknown",
    }
    assert runtime.events == ["prepare", "continue", "quarantine"]


def test_rollback_uncertainty_quarantines_all_capture_owners_and_requires_restart(
    tmp_path,
) -> None:
    runtime = _TransactionalContinuationRuntime(
        arming=CaptureArming("", 42, 2), rollback_fails=True
    )
    service = CaptureService(tmp_path)
    registry = ProxyRegistry()
    service.activate_capture_view(_capture(), registry)
    proxy_id = service.inspect(
        runtime,
        registry,
        fence=_fence(),
        runtime_id="runtime-1",
        runtime_generation=1,
        context_generation=1,
        filters={},
        cursor=0,
        limit=20,
    ).variables[0].proxy_id

    result = service.continue_capture(
        runtime,
        registry,
        fence=_fence(),
        operation_id="op-continue",
        next_points=(CapturePointRequest("after", "zup", "Payroll", "Run", 23),),
    )

    assert result.execution.terminal_state is AgentOperationState.UNKNOWN
    assert result.quarantine_runtime is True
    assert runtime.events == ["prepare", "rollback", "quarantine"]
    with pytest.raises(StaleProxy):
        registry.resolve(proxy_id)
    with pytest.raises(StaleProxy):
        service.current_capture(_fence())
    assert [action.method for action in result.recovery] == [
        "workspace.status",
        "operation.wait",
        "runtime.close",
        "runtime.restart",
    ]


def test_service_snapshot_restore_failure_quarantines_lower_admission(
    tmp_path, monkeypatch
) -> None:
    runtime = _TransactionalContinuationRuntime(
        arming=CaptureArming("", 42, 2)
    )
    service = CaptureService(tmp_path)
    registry = ProxyRegistry()
    service.activate_capture_view(_capture(), registry)
    monkeypatch.setattr(
        registry,
        "_restore_capture_state",
        lambda _snapshot: (_ for _ in ()).throw(
            OSError("planned registry restore failure")
        ),
    )

    result = service.continue_capture(
        runtime,
        registry,
        fence=_fence(),
        operation_id="op-continue",
        next_points=(CapturePointRequest("after", "zup", "Payroll", "Run", 23),),
    )

    assert result.execution.terminal_state is AgentOperationState.UNKNOWN
    assert result.quarantine_runtime is True
    assert runtime.events == ["prepare", "quarantine"]
    with pytest.raises(StaleProxy):
        service.current_capture(_fence())


def test_empty_successor_uses_the_same_admission_transaction(tmp_path) -> None:
    runtime = _TransactionalContinuationRuntime(arming=None)
    service = CaptureService(tmp_path)
    service.activate_capture_view(_capture())

    result = service.continue_capture(
        runtime,
        ProxyRegistry(),
        fence=_fence(),
        operation_id="op-continue",
        next_points=(),
    )

    assert result.execution.terminal_state is AgentOperationState.COMPLETED
    assert runtime.events == ["prepare", "continue", "commit"]


def test_service_arming_fsync_uncertainty_quarantines_instead_of_failed_pause(
    tmp_path, monkeypatch
) -> None:
    runtime = _TransactionalContinuationRuntime(
        arming=CaptureArming("private-ticket", 42, 2)
    )
    service = CaptureService(tmp_path)
    registry = ProxyRegistry()
    service.activate_capture_view(_capture(), registry)
    monkeypatch.setattr(
        service,
        "_journal_arming",
        lambda *_args: (_ for _ in ()).throw(OSError("fsync failed")),
    )

    result = service.continue_capture(
        runtime,
        registry,
        fence=_fence(),
        operation_id="op-continue",
        next_points=(CapturePointRequest("after", "zup", "Payroll", "Run", 23),),
    )

    assert result.execution.terminal_state is AgentOperationState.UNKNOWN
    assert result.quarantine_runtime is True
    assert runtime.events == ["prepare", "quarantine"]
    with pytest.raises(StaleProxy):
        service.current_capture(_fence())


def test_workspace_marks_generation_closing_and_replays_unknown_after_rollback_uncertainty(
    tmp_path,
) -> None:
    runtime = _TransactionalContinuationRuntime(
        arming=CaptureArming("", 42, 2), rollback_fails=True
    )
    workspace = AgentWorkspaceService(
        tmp_path, _Factory(), maximum_mode=CapabilityMode.EXPERIMENT
    )
    admitted = _AdmittedRuntime(
        runtime, "runtime-1", 1, CapabilityMode.EXPERIMENT
    )
    workspace._runtime = admitted  # type: ignore[assignment]
    workspace._selected["default"] = "runtime-1"
    workspace._capture.activate_capture_view(
        _capture(), workspace._proxy_registry
    )
    try:
        first = workspace.call("capture.continue", _request())
        second = workspace.call("capture.continue", _request())

        assert first.ok and second.ok
        assert first.value.state is AgentOperationState.UNKNOWN
        assert second.value.operation.operation_id == first.value.operation.operation_id
        assert admitted.closing is True
        assert runtime.events == ["prepare", "rollback", "quarantine"]
    finally:
        workspace.close()


def test_service_restart_replays_the_same_unknown_continuation_without_backend(
    tmp_path,
) -> None:
    runtime = _TransactionalContinuationRuntime(
        arming=CaptureArming("", 42, 2), rollback_fails=True
    )
    first_service = AgentWorkspaceService(
        tmp_path, _Factory(), maximum_mode=CapabilityMode.EXPERIMENT
    )
    first_service._runtime = _AdmittedRuntime(  # type: ignore[assignment]
        runtime, "runtime-1", 1, CapabilityMode.EXPERIMENT
    )
    first_service._selected["default"] = "runtime-1"
    first_service._capture.activate_capture_view(
        _capture(), first_service._proxy_registry
    )
    first = first_service.call("capture.continue", _request())
    first_service.close()

    restarted = AgentWorkspaceService(
        tmp_path, _Factory(), maximum_mode=CapabilityMode.EXPERIMENT
    )
    try:
        replay = restarted.call("capture.continue", _request())

        assert first.ok and replay.ok
        assert replay.value.operation.operation_id == first.value.operation.operation_id
        assert replay.value.state is AgentOperationState.UNKNOWN
        assert runtime.events == ["prepare", "rollback", "quarantine"]
    finally:
        restarted.close()
