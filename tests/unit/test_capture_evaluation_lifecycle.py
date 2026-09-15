from __future__ import annotations

import json
from queue import Empty, Queue
from threading import Event, current_thread
from time import monotonic, sleep
from uuid import uuid4

import pytest

from onec_runtime.capture_evaluation import (
    CaptureEvaluationCoordinator,
    CaptureEvaluationKind,
    CaptureEvaluationRequest,
    CaptureEvaluationState,
    CaptureEvaluationTicket,
    CaptureFence,
    CapturePhase,
)
from onec_runtime.errors import (
    CaptureBusyError,
    CaptureEvaluationPendingError,
    CaptureOutcomeUnknownError,
    CaptureRecoveryRequiredError,
    CommandTimeout,
    StaleCaptureError,
    TargetLost,
)
from onec_runtime.rdbg.models import (
    DebugTarget,
    EvaluationResult,
    PendingEvaluation,
    StackFrame,
    StopEvent,
)
from onec_runtime.rdbg.session import RdbgSession, SessionState
from onec_runtime.runtime_api import _PreparedCaptureExecution

from test_prototype_runtime import (
    CAPTURE_A,
    CAPTURE_B,
    SERVICE,
    TARGET,
    USER,
    WORKER_BREAKPOINT,
    ScriptedSession,
    captured_controller,
)


class ControlledCaptureSession(ScriptedSession):
    """Delay coordinator-owned evalExpr results without faking the controller."""

    def __init__(
        self,
        *,
        stops=(CAPTURE_A,),  # type: ignore[no-untyped-def]
        dispatch_error: BaseException | None = None,
        local_start_error: BaseException | None = None,
        poll_error: BaseException | None = None,
        fail_workspace_on_call: int | None = None,
        block_messages: bool = False,
        auto_helpers: bool = False,
        allow_legacy_pin_helpers: bool = False,
    ) -> None:
        super().__init__(tuple(stops), fail_workspace_on_call=fail_workspace_on_call)
        self.dispatch_error = dispatch_error
        self.local_start_error = local_start_error
        self.poll_error = poll_error
        self.block_messages = block_messages
        self.auto_helpers = auto_helpers
        self.allow_legacy_pin_helpers = allow_legacy_pin_helpers
        self.accepted = Event()
        self.polling = Event()
        self.message_step_started = Event()
        self.allow_message_step = Event()
        if not block_messages:
            self.allow_message_step.set()
        self.events: Queue[EvaluationResult | StopEvent | BaseException] = Queue()
        self.capture_start_count = 0
        self.primary_dispatch_count = 0
        self.capture_poll_count = 0
        self.capture_pending: PendingEvaluation | None = None
        self._controlled_owner = object()
        self._pending_role = ""
        self.start_threads: list[int] = []
        self.dispatch_threads: list[int] = []
        self.poll_threads: list[int] = []
        self.workspace_threads: list[int] = []
        self.polled_capabilities: list[PendingEvaluation] = []

    def set_breakpoints(self, locations):  # type: ignore[no-untyped-def]
        ident = current_thread().ident
        assert ident is not None
        self.workspace_threads.append(ident)
        return super().set_breakpoints(locations)

    def evaluate(self, expression: str, **kwargs: object) -> EvaluationResult:
        if self.allow_legacy_pin_helpers and any(
            name in expression
            for name in (
                "УстановитьПинПоколенияWorker",
                "ОчиститьПинПоколенияWorker",
            )
        ):
            stack_level = kwargs.get("stack_level", 0)
            call_value: object = (
                expression if stack_level == 0 else (expression, stack_level)
            )
            self.calls.append(("evaluate", call_value))
            return EvaluationResult(uuid4(), "Булево", "Истина", False)
        return super().evaluate(expression, **kwargs)

    def start_evaluation(
        self,
        expression: str,
        *,
        max_text_size: int = 307_200,
        stack_level: int = 0,
        on_transport_dispatch=None,  # type: ignore[no-untyped-def]
    ) -> PendingEvaluation:
        del max_text_size
        ident = current_thread().ident
        assert ident is not None
        self.start_threads.append(ident)
        self.calls.append(("start_evaluation", (expression, stack_level)))
        self.capture_start_count += 1
        if self.local_start_error is not None:
            raise self.local_start_error
        if on_transport_dispatch is None:
            raise AssertionError("controller did not bind dispatch evidence to evalExpr entry")
        on_transport_dispatch()
        self.dispatch_threads.append(ident)
        if self.dispatch_error is not None:
            raise self.dispatch_error
        if self.capture_pending is not None:
            raise AssertionError("controller redispatched while one capability was pending")
        pending = PendingEvaluation(TARGET, uuid4(), self._controlled_owner)
        self.capture_pending = pending
        self._pending_role = (
            "messages"
            if "ЗабратьСообщенияЯчейкиИзКонтекста" in expression
            else (
                "evaluation"
                if "ВыполнитьКод" in expression
                else "helper"
            )
        )
        if self._pending_role == "evaluation":
            self.primary_dispatch_count += 1
        self.accepted.set()
        return pending

    def wait_evaluation_event(
        self,
        pending: PendingEvaluation,
        *,
        timeout_s: float,
    ) -> EvaluationResult | StopEvent:
        assert pending is self.capture_pending
        ident = current_thread().ident
        assert ident is not None
        self.poll_threads.append(ident)
        self.polled_capabilities.append(pending)
        self.capture_poll_count += 1
        self.polling.set()
        if self.poll_error is not None:
            raise self.poll_error
        if self._pending_role == "messages":
            self.message_step_started.set()
            if not self.allow_message_step.is_set():
                raise CommandTimeout("synthetic message sealing interval timeout")
            payload = json.dumps(list(self.message_values), ensure_ascii=False)
            self.capture_pending = None
            self._pending_role = ""
            return EvaluationResult(
                pending.result_id,
                "Строка",
                '"' + payload.replace('"', '""') + '"',
                False,
            )
        if self._pending_role == "helper" and self.auto_helpers:
            self.capture_pending = None
            self._pending_role = ""
            return EvaluationResult(
                pending.result_id,
                "Булево",
                "Истина",
                False,
            )
        try:
            event = self.events.get(timeout=min(timeout_s, 0.005))
        except Empty:
            raise CommandTimeout("synthetic interval timeout") from None
        if isinstance(event, BaseException):
            raise event
        self.capture_pending = None
        self._pending_role = ""
        return event

    def complete(
        self,
        presentation: str = "901",
        *,
        type_name: str = "Число",
        error: str = "",
    ) -> None:
        pending = self.capture_pending
        assert pending is not None
        self.events.put(EvaluationResult(
            pending.result_id,
            type_name,
            presentation,
            bool(error),
            error_text=error,
        ))

    def complete_stop(self) -> None:
        self.events.put(StopEvent(
            TARGET,
            WORKER_BREAKPOINT,
            "callStackFormed",
            stop_by_breakpoint=True,
            stack=(WORKER_BREAKPOINT,),
            stack_frames=(StackFrame(TARGET, 0, WORKER_BREAKPOINT),),
        ))


def eventually(predicate) -> None:  # type: ignore[no-untyped-def]
    deadline = monotonic() + 2
    while not predicate():
        assert monotonic() < deadline, "controller-owned evaluation did not settle"
        sleep(0.002)


class ControllerCaptureProbe:
    """One test-only adapter; controller storage names are deliberately irrelevant."""

    def __init__(self, controller) -> None:  # type: ignore[no-untyped-def]
        owners = [
            value
            for value in vars(controller).values()
            if isinstance(value, CaptureEvaluationCoordinator)
        ]
        assert len(owners) == 1, "controller must own exactly one capture coordinator"
        self.owner = owners[0]

    def status(self):  # type: ignore[no-untyped-def]
        return self.owner.status(self.owner._fence)

    def wait(self, evaluation_id: str | None, timeout_s: float):  # type: ignore[no-untyped-def]
        return self.owner.wait(self.owner._fence, evaluation_id, timeout_s)

    def close(self, session: ControlledCaptureSession) -> None:
        self.owner.begin_close()
        session.poll_error = TargetLost("synthetic teardown")
        assert self.owner.join(2)


def capture_probe(controller) -> ControllerCaptureProbe:  # type: ignore[no-untyped-def]
    return ControllerCaptureProbe(controller)


def close_owner(controller, session: ControlledCaptureSession) -> None:  # type: ignore[no-untyped-def]
    owners = [
        value
        for value in vars(controller).values()
        if isinstance(value, CaptureEvaluationCoordinator)
    ]
    if not owners:
        return
    assert len(owners) == 1
    owners[0].begin_close()
    session.poll_error = TargetLost("synthetic teardown")
    assert owners[0].join(2)


class FailingEvalTransport:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.request_entered = False
        self.dispatch_marker_set = False

    def mark_evalexpr_dispatch(self) -> None:
        assert not self.request_entered, (
            "dispatch marker must precede transport.request entry"
        )
        assert not self.dispatch_marker_set
        self.dispatch_marker_set = True

    def request(self, method: str, payload: bytes) -> bytes:
        del payload
        self.request_entered = True
        self.calls.append(method)
        assert self.dispatch_marker_set, (
            "transport.request entered before the dispatch marker"
        )
        raise OSError("synthetic evalExpr transport failure")


def ready_rdbg(transport: FailingEvalTransport) -> RdbgSession:
    session = RdbgSession(transport, SERVICE)  # type: ignore[arg-type]
    session.state = SessionState.READY
    session.target = DebugTarget(TARGET, "CLIENT", "Stopped", 7)
    return session


def test_rdbg_dispatch_marker_excludes_local_start_validation() -> None:
    transport = FailingEvalTransport()
    session = ready_rdbg(transport)

    with pytest.raises(ValueError, match="non-empty"):
        session.start_evaluation(
            "",
            on_transport_dispatch=transport.mark_evalexpr_dispatch,
        )

    assert transport.dispatch_marker_set is False
    assert transport.request_entered is False
    assert transport.calls == []


def test_rdbg_dispatch_marker_runs_at_transport_entry_before_acceptance() -> None:
    transport = FailingEvalTransport()
    session = ready_rdbg(transport)

    with pytest.raises(OSError, match="transport failure"):
        session.start_evaluation(
            "Результат = 1",
            on_transport_dispatch=transport.mark_evalexpr_dispatch,
        )

    assert transport.dispatch_marker_set is True
    assert transport.request_entered is True
    assert transport.calls == ["evalExpr"]


@pytest.mark.parametrize(
    ("expression", "phase", "state", "code", "disposition", "transport_calls"),
    [
        (
            "",
            CapturePhase.PAUSED,
            CaptureEvaluationState.FAILED,
            "pre_dispatch_failed",
            "release",
            [],
        ),
        (
            "Результат = 1",
            CapturePhase.OUTCOME_UNKNOWN,
            CaptureEvaluationState.UNKNOWN,
            "dispatch_uncertain",
            "quarantine",
            ["evalExpr"],
        ),
    ],
)
def test_coordinator_classifies_exact_evalexpr_entry_boundary(
    expression: str,
    phase: CapturePhase,
    state: CaptureEvaluationState,
    code: str,
    disposition: str,
    transport_calls: list[str],
) -> None:
    transport = FailingEvalTransport()
    session = ready_rdbg(transport)
    capture_fence = CaptureFence(7, 1, 1)
    owner = CaptureEvaluationCoordinator(capture_fence, poll_interval_s=0.01)
    dispositions: list[str] = []
    try:
        def dispatch(entered):  # type: ignore[no-untyped-def]
            def exact_transport_entry() -> None:
                entered()
                transport.mark_evalexpr_dispatch()

            return session.start_evaluation(
                expression,
                on_transport_dispatch=exact_transport_entry,
            )

        ticket = owner.submit_evaluation(CaptureEvaluationRequest(
            capture_fence,
            CaptureEvaluationKind.USER_BSL,
            dispatch,
            lambda pending, timeout_s: session.wait_evaluation_event(
                pending, timeout_s=timeout_s,
            ),
            lambda result: result.presentation,
            pin_lease=dispositions.append,
        ))
        outcome = owner.wait(capture_fence, ticket.evaluation_id, timeout_s=1)

        assert outcome.state is state
        assert outcome.diagnostic is not None
        assert outcome.diagnostic.code == code
        assert owner.status(capture_fence).phase is phase
        assert dispositions == [disposition]
        assert transport.calls == transport_calls
        assert transport.request_entered is bool(transport_calls)
        assert transport.dispatch_marker_set is bool(transport_calls)
    finally:
        owner.begin_close()
        assert owner.join(2)


def controlled_notebook_runtime(tmp_path):  # type: ignore[no-untyped-def]
    from onec_runtime.prototype_runtime import PrototypeRuntimeController
    from onec_runtime.runtime_api import PrototypeRuntimeApi
    from onec_runtime.worker_universe import (
        WorkerModuleArtifactBuilder,
        WorkerModuleArtifactCache,
    )
    from test_runtime_api import _UniverseInstructionExecutor, _notebook_worker_builder

    session = ControlledCaptureSession(
        stops=(CAPTURE_A, CAPTURE_B, SERVICE),
        auto_helpers=True,
        allow_legacy_pin_helpers=True,
    )
    controller = PrototypeRuntimeController(session, SERVICE, command_timeout_s=0.02)
    builder = _notebook_worker_builder(tmp_path)
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=builder,
        worker_module_builder=WorkerModuleArtifactBuilder(
            builder,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile="server-test",
        ),
        worker_instruction_executor=_UniverseInstructionExecutor(),
        capture_points=(CAPTURE_A, CAPTURE_B),
        user_breakpoints=(USER,),
    )
    return api, controller, session


@pytest.mark.parametrize("entrypoint", ["execute_bsl", "prepared_hypothesis"])
def test_runtime_api_capture_path_transfers_real_task3_handoff_and_pin_ownership(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
) -> None:  # type: ignore[no-untyped-def]
    from test_notebook_method_runtime import UPDATE

    api, controller, session = controlled_notebook_runtime(tmp_path)
    assert api.execute_bsl(UPDATE).succeeded
    assert api.execute_bsl("Результат = Б();").kind.value == "captured"
    original_operation_pin = api._operation_generation_pin
    assert original_operation_pin is not None
    assert len(api._worker_universe._leases) == 1
    prepared = (
        api.prepare_capture_hypothesis("РезультатИнструкции = Б();")
        if entrypoint == "prepared_hypothesis"
        else None
    )
    # Preparation pins only while lowering, then releases. The second lease
    # must appear only when the owned execution record accepts the operation.
    assert len(api._worker_universe._leases) == 1

    owned_calls: list[_PreparedCaptureExecution] = []
    original_owned = _PreparedCaptureExecution.execute_owned

    def owned(handoff, submit):  # type: ignore[no-untyped-def]
        owned_calls.append(handoff)
        return original_owned(handoff, submit)

    monkeypatch.setattr(_PreparedCaptureExecution, "execute_owned", owned)
    releases: list[object] = []
    quarantines: list[object] = []
    release_pin = api._worker_universe.release_pin
    retain_unknown = api._worker_universe.retain_outcome_unknown

    def release(pin):  # type: ignore[no-untyped-def]
        releases.append(pin)
        return release_pin(pin)

    def quarantine(pin):  # type: ignore[no-untyped-def]
        quarantines.append(pin)
        return retain_unknown(pin)

    monkeypatch.setattr(api._worker_universe, "release_pin", release)
    monkeypatch.setattr(api._worker_universe, "retain_outcome_unknown", quarantine)
    try:
        with pytest.raises(CaptureEvaluationPendingError) as caught:
            if prepared is None:
                api.execute_bsl("РезультатИнструкции = Б();")
            else:
                api.execute_prepared_capture_hypothesis(prepared)

        assert len(owned_calls) == 1
        assert owned_calls[0].transferred is True
        assert owned_calls[0].submitted is True
        assert api._poisoned_error is None
        assert api._operation_generation_pin is original_operation_pin
        assert api._evaluation_generation_pin is None
        assert len(api._worker_universe._leases) == 2
        assert releases == []
        assert quarantines == []
        assert session.primary_dispatch_count == 1
        status = capture_probe(controller).status()
        assert status.pending_evaluation_id == caught.value.evaluation_id
        assert status.phase is CapturePhase.EVALUATING

        session.complete()
        outcome = capture_probe(controller).wait(caught.value.evaluation_id, 1)

        assert outcome.state is CaptureEvaluationState.COMPLETED
        assert api._poisoned_error is None
        assert len(api._worker_universe._leases) == 1
        assert len(releases) == 1
        assert quarantines == []
        assert session.primary_dispatch_count == 1
    finally:
        close_owner(controller, session)


def test_acknowledged_timeout_keeps_one_pending_capability_and_rejects_redispatch() -> None:
    session = ControlledCaptureSession()
    controller = captured_controller(session, command_timeout_s=0.02)
    try:
        with pytest.raises(CaptureEvaluationPendingError) as caught:
            controller.execute_capture("РезультатИнструкции = 901;")

        assert session.accepted.is_set()
        assert session.capture_start_count == 1
        assert session.capture_pending is not None
        assert controller.state.value == "evaluating_capture"
        shielded = controller.breakpoint_workspace_owner.confirmed_snapshot
        assert shielded.shielded is True
        assert shielded.effective_locations == (SERVICE, USER)
        status = capture_probe(controller).status()
        assert (
            status.phase,
            status.pending_evaluation_id,
            status.evaluation_kind,
        ) == (
            CapturePhase.EVALUATING,
            caught.value.evaluation_id,
            CaptureEvaluationKind.USER_BSL,
        )

        with pytest.raises(CaptureBusyError) as busy:
            controller.execute_capture("РезультатИнструкции = 902;")
        assert busy.value.evaluation_id == caught.value.evaluation_id
        assert session.capture_start_count == 1
        assert controller.breakpoint_workspace_owner.confirmed_snapshot.shielded is True
    finally:
        close_owner(controller, session)


def test_late_success_restores_exact_workspace_and_completed_output_without_redispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_prototype_runtime import runtime_module

    session = ControlledCaptureSession()
    controller = captured_controller(session, command_timeout_s=0.02)
    initiating_thread = current_thread().ident
    normalization_threads: list[int] = []
    runtime = runtime_module()
    decode = runtime.evaluation_to_python

    def tracked_decode(result):  # type: ignore[no-untyped-def]
        ident = current_thread().ident
        assert ident is not None
        normalization_threads.append(ident)
        return decode(result)

    monkeypatch.setattr(runtime, "evaluation_to_python", tracked_decode)
    try:
        with pytest.raises(CaptureEvaluationPendingError) as caught:
            controller.execute_capture("РезультатИнструкции = 901;")
        eventually(lambda: session.capture_poll_count >= 2)
        session.complete()

        probe = capture_probe(controller)
        outcome = probe.wait(caught.value.evaluation_id, timeout_s=1)

        assert outcome.state is CaptureEvaluationState.COMPLETED
        assert outcome.result == 901
        assert controller.state.value == "captured"
        assert probe.status().pending_evaluation_id is None
        assert session.capture_start_count == 1
        assert session.primary_dispatch_count == 1
        assert len({id(value) for value in session.polled_capabilities}) == 1
        restored = controller.breakpoint_workspace_owner.confirmed_snapshot
        assert restored.shielded is False
        assert restored.effective_locations == (SERVICE, CAPTURE_A, CAPTURE_B, USER)
        owner_thread = session.dispatch_threads[0]
        assert owner_thread != initiating_thread
        assert set(session.start_threads + session.dispatch_threads + session.poll_threads) == {
            owner_thread
        }
        assert session.workspace_threads[-2:] == [owner_thread, owner_thread]
        assert normalization_threads and set(normalization_threads) == {owner_thread}
    finally:
        close_owner(controller, session)


def test_keyboard_interrupt_detaches_only_waiter_and_late_result_still_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = ControlledCaptureSession()
    controller = captured_controller(session, command_timeout_s=1)
    caller = current_thread()
    original_ticket_wait = CaptureEvaluationTicket.wait_initiator

    def interrupting_ticket_wait(
        ticket: CaptureEvaluationTicket,
        timeout_s: float | None = None,
    ) -> object:
        assert session.accepted.wait(1)
        owner = ticket._coordinator
        original_condition_wait = owner._condition.wait

        def interrupt_after_acceptance(timeout: float | None = None) -> bool:
            if current_thread() is caller:
                raise KeyboardInterrupt
            return original_condition_wait(timeout)

        monkeypatch.setattr(owner._condition, "wait", interrupt_after_acceptance)
        try:
            return original_ticket_wait(ticket, timeout_s)
        finally:
            monkeypatch.setattr(owner._condition, "wait", original_condition_wait)

    try:
        monkeypatch.setattr(
            CaptureEvaluationTicket,
            "wait_initiator",
            interrupting_ticket_wait,
        )
        with pytest.raises(KeyboardInterrupt):
            controller.execute_capture("РезультатИнструкции = 901;")

        probe = capture_probe(controller)
        status = probe.status()
        assert status.phase is CapturePhase.EVALUATING
        assert status.evaluation_timing.initiating_waiter_detached_ms is not None
        assert session.capture_pending is not None
        assert controller.breakpoint_workspace_owner.confirmed_snapshot.shielded is True

        session.complete()
        outcome = probe.wait(status.pending_evaluation_id, timeout_s=1)
        assert outcome.state is CaptureEvaluationState.COMPLETED
        assert controller.state.value == "captured"
        assert session.capture_start_count == 1
    finally:
        close_owner(controller, session)


def test_late_bsl_error_is_retained_after_workspace_restore() -> None:
    session = ControlledCaptureSession()
    controller = captured_controller(session, command_timeout_s=0.02)
    try:
        with pytest.raises(CaptureEvaluationPendingError) as caught:
            controller.execute_capture('ВызватьИсключение "boom";')
        session.complete("boom", type_name="Ошибка", error="private platform text")

        outcome = capture_probe(controller).wait(
            caught.value.evaluation_id, timeout_s=1,
        )

        assert outcome.state is CaptureEvaluationState.FAILED
        assert outcome.diagnostic is not None
        assert outcome.diagnostic.code == "bsl_error"
        assert controller.state.value == "captured"
        assert controller.breakpoint_workspace_owner.confirmed_snapshot.shielded is False
        assert "private platform text" not in repr(outcome)
        assert session.capture_start_count == 1
    finally:
        close_owner(controller, session)


@pytest.mark.parametrize("bsl_error", [False, True])
def test_late_user_result_seals_messages_inline_before_paused_publication(
    bsl_error: bool,
) -> None:
    session = ControlledCaptureSession(block_messages=True)
    session.message_values = ["late sealed message"]
    controller = captured_controller(session, command_timeout_s=0.02)
    initiating_thread = current_thread().ident
    workspace_call_baseline = session.workspace_call_count
    workspace_thread_baseline = len(session.workspace_threads)
    try:
        source = 'Сообщить("late sealed message");'
        if bsl_error:
            source += ' ВызватьИсключение "boom";'
        else:
            source += " РезультатИнструкции = 901;"
        with pytest.raises(CaptureEvaluationPendingError) as caught:
            controller.execute_capture(source)

        session.complete(
            "boom" if bsl_error else "901",
            type_name="Ошибка" if bsl_error else "Число",
            error="private platform error" if bsl_error else "",
        )
        assert session.message_step_started.wait(1)
        probe = capture_probe(controller)
        during_sealing = probe.status()
        assert during_sealing.phase is CapturePhase.EVALUATING
        assert during_sealing.pending_evaluation_id == caught.value.evaluation_id
        assert during_sealing.evaluation_kind is CaptureEvaluationKind.USER_BSL
        assert during_sealing.evaluation_timing.remote_step_count == 2
        owner_thread = session.dispatch_threads[0]
        shielded = controller.breakpoint_workspace_owner.confirmed_snapshot
        assert shielded.shielded is True
        assert shielded.effective_locations == (SERVICE, USER)
        assert session.workspace_call_count == workspace_call_baseline + 1
        assert session.workspace_threads[workspace_thread_baseline:] == [owner_thread]

        session.allow_message_step.set()
        outcome = probe.wait(caught.value.evaluation_id, timeout_s=1)

        assert outcome.messages == ("late sealed message",)
        assert outcome.state is (
            CaptureEvaluationState.FAILED
            if bsl_error
            else CaptureEvaluationState.COMPLETED
        )
        if bsl_error:
            assert outcome.diagnostic is not None
            assert outcome.diagnostic.code == "bsl_error"
        assert probe.status().phase is CapturePhase.PAUSED
        restored = controller.breakpoint_workspace_owner.confirmed_snapshot
        assert restored.shielded is False
        assert restored.effective_locations == (SERVICE, CAPTURE_A, CAPTURE_B, USER)
        assert session.workspace_call_count == workspace_call_baseline + 2
        assert session.workspace_threads[workspace_thread_baseline:] == [
            owner_thread,
            owner_thread,
        ]
        assert session.capture_start_count == 2
        assert session.primary_dispatch_count == 1
        assert owner_thread != initiating_thread
        assert set(session.start_threads + session.dispatch_threads + session.poll_threads) == {
            owner_thread
        }
    finally:
        session.allow_message_step.set()
        close_owner(controller, session)


def test_workspace_restoration_failure_requires_recovery_after_confirmed_result() -> None:
    session = ControlledCaptureSession(fail_workspace_on_call=3)
    controller = captured_controller(session, command_timeout_s=1)
    try:
        session.events.put(EvaluationResult(uuid4(), "Число", "901", False))
        # The controlled session correlates the queued result to the capability below.
        original_wait = session.wait_evaluation_event

        def correlated_wait(pending: PendingEvaluation, *, timeout_s: float) -> EvaluationResult:
            result = original_wait(pending, timeout_s=timeout_s)
            return EvaluationResult(pending.result_id, result.type_name, result.presentation, False)

        session.wait_evaluation_event = correlated_wait  # type: ignore[method-assign]
        with pytest.raises(CaptureRecoveryRequiredError):
            controller.execute_capture("РезультатИнструкции = 901;")

        status = capture_probe(controller).status()
        assert status.phase is CapturePhase.RECOVERY_REQUIRED
        assert status.failure is not None
        assert status.failure.code == "workspace_restore_failed"
        assert session.capture_start_count == 1
    finally:
        close_owner(controller, session)


def test_target_loss_stales_capture_without_workspace_restore() -> None:
    session = ControlledCaptureSession(poll_error=TargetLost("private target"))
    controller = captured_controller(session, command_timeout_s=1)
    try:
        with pytest.raises(StaleCaptureError):
            controller.execute_capture("РезультатИнструкции = 901;")

        assert controller.state.value == "lost"
        assert capture_probe(controller).status().phase is CapturePhase.STALE
        assert controller.breakpoint_workspace_owner.confirmed_snapshot.shielded is True
        assert session.capture_start_count == 1
    finally:
        close_owner(controller, session)


def test_unexpected_stop_requires_recovery_and_is_not_exposed_as_nested_capture() -> None:
    session = ControlledCaptureSession()
    controller = captured_controller(session, command_timeout_s=0.02)
    try:
        with pytest.raises(CaptureEvaluationPendingError) as caught:
            controller.execute_capture("РезультатИнструкции = 901;")
        session.complete_stop()

        probe = capture_probe(controller)
        outcome = probe.wait(caught.value.evaluation_id, timeout_s=1)

        assert outcome.state is CaptureEvaluationState.FAILED
        assert outcome.diagnostic is not None
        assert outcome.diagnostic.code == "unexpected_stop"
        assert probe.status().phase is CapturePhase.RECOVERY_REQUIRED
        assert controller.state.value == "recovering"
        assert controller.breakpoint_workspace_owner.confirmed_snapshot.shielded is True
        with pytest.raises(CaptureRecoveryRequiredError):
            controller.execute_capture("РезультатИнструкции = 902;")
        continue_count = session.continue_count
        with pytest.raises(CaptureRecoveryRequiredError):
            controller.resume_debug_stop()
        assert session.continue_count == continue_count
        assert session.primary_dispatch_count == 1
    finally:
        close_owner(controller, session)


def test_transport_entry_without_known_acceptance_is_the_only_outcome_unknown_case() -> None:
    session = ControlledCaptureSession(dispatch_error=OSError("private uncertain dispatch"))
    controller = captured_controller(session, command_timeout_s=1)
    try:
        with pytest.raises(CaptureOutcomeUnknownError):
            controller.execute_capture("РезультатИнструкции = 901;")

        status = capture_probe(controller).status()
        assert status.phase is CapturePhase.OUTCOME_UNKNOWN
        assert status.failure is not None
        assert status.failure.code == "dispatch_uncertain"
        assert controller.state.value == "recovering"
        assert session.capture_start_count == 1
        assert session.capture_pending is None
        assert controller.breakpoint_workspace_owner.confirmed_snapshot.shielded is True
    finally:
        close_owner(controller, session)


def inspect_temporary_table(controller):  # type: ignore[no-untyped-def]
    controller._capture_manager_paths["manager"] = (
        "Контекст.КонтекстОтладки.Менеджер"
    )
    return controller.capture_temporary_tables(
        "manager",
        names=("Данные",),
        cursor=0,
        limit=1,
        selection={"offset": 0, "limit": 10, "columns": ()},
    )


@pytest.mark.parametrize(
    ("invoke", "expected_kind", "late_type", "late_value", "changes_workspace"),
    [
        (
            lambda controller: controller.execute_system_capture("Результат = 1;"),
            CaptureEvaluationKind.MATERIALIZATION_HELPER,
            "Число",
            "1",
            True,
        ),
        (
            lambda controller: controller.resolve_capture_manager_origin("Скаляр", ()),
            CaptureEvaluationKind.INSPECTION,
            "Булево",
            "Истина",
            False,
        ),
        (
            lambda controller: controller.take_context_string(
                "__onec_value_" + "a" * 32, max_text_size=4096,
            ),
            CaptureEvaluationKind.MATERIALIZATION_HELPER,
            "Строка",
            '"payload"',
            False,
        ),
        (
            lambda controller: controller.install_capture_worker_generation_pin(
                "a" * 64,
            ),
            CaptureEvaluationKind.MATERIALIZATION_HELPER,
            "Булево",
            "Истина",
            False,
        ),
        (
            lambda controller: controller.clear_capture_worker_generation_pin(),
            CaptureEvaluationKind.MATERIALIZATION_HELPER,
            "Булево",
            "Истина",
            False,
        ),
        (
            lambda controller: controller.drop_context_value(
                "__onec_value_" + "a" * 32,
            ),
            CaptureEvaluationKind.MATERIALIZATION_HELPER,
            "Неопределено",
            "Неопределено",
            False,
        ),
        (
            inspect_temporary_table,
            CaptureEvaluationKind.INSPECTION,
            "Булево",
            "Истина",
            False,
        ),
    ],
)
def test_controller_owned_internal_evaluations_use_explicit_kind_and_same_owner(
    invoke, expected_kind, late_type: str, late_value: str,  # type: ignore[no-untyped-def]
    changes_workspace: bool,
) -> None:
    session = ControlledCaptureSession()
    controller = captured_controller(session, command_timeout_s=0.02)
    initiating_thread = current_thread().ident
    workspace_call_baseline = session.workspace_call_count
    workspace_thread_baseline = len(session.workspace_threads)
    try:
        with pytest.raises(CaptureEvaluationPendingError) as caught:
            invoke(controller)
        assert caught.value.evaluation_kind is expected_kind
        status = capture_probe(controller).status()
        assert status.evaluation_kind is expected_kind
        assert status.phase is CapturePhase.EVALUATING
        assert session.capture_start_count == 1
        owner_thread = session.dispatch_threads[0]
        assert owner_thread != initiating_thread
        assert set(
            session.start_threads + session.dispatch_threads + session.poll_threads
        ) == {owner_thread}
        if changes_workspace:
            assert session.workspace_call_count == workspace_call_baseline + 1
            assert session.workspace_threads[workspace_thread_baseline:] == [
                owner_thread
            ]
        else:
            assert session.workspace_call_count == workspace_call_baseline

        session.complete(late_value, type_name=late_type)
        outcome = capture_probe(controller).wait(
            caught.value.evaluation_id, timeout_s=1,
        )
        assert outcome.state is CaptureEvaluationState.COMPLETED
        assert outcome.result is None
        assert controller.state.value == "captured"
        assert session.capture_start_count == 1
        assert set(
            session.start_threads + session.dispatch_threads + session.poll_threads
        ) == {owner_thread}
        if changes_workspace:
            assert session.workspace_call_count == workspace_call_baseline + 2
            assert session.workspace_threads[workspace_thread_baseline:] == [
                owner_thread,
                owner_thread,
            ]
        else:
            assert session.workspace_call_count == workspace_call_baseline
    finally:
        close_owner(controller, session)
