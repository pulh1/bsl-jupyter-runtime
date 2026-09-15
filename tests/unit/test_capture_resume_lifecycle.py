from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from threading import Event, RLock, Thread, current_thread, get_ident
from types import SimpleNamespace
from time import monotonic, sleep

import pytest

from onec_runtime.capture_evaluation import CapturePhase, CaptureResumeTicket
from onec_runtime.errors import (
    CaptureBusyError,
    CaptureRecoveryRequiredError,
    ProtocolError,
    RdbgTransportError,
)
from onec_runtime.prototype_runtime import OperationState, PartialWritebackError
from onec_runtime.runtime_api import PrototypeRuntimeApi, RuntimeReplyKind
from onec_runtime.session import RuntimeSession
from onec_runtime.fault_injection import FaultPoint
from onec_runtime.worker_universe import (
    WorkerModuleArtifactBuilder,
    WorkerModuleArtifactCache,
)

from test_prototype_runtime import (
    CAPTURE_A,
    CAPTURE_B,
    SERVICE,
    USER,
    ScriptedSession,
    captured_controller,
    evaluation,
    runtime_module,
)
from test_runtime_api import (
    _UniverseInstructionExecutor,
    _common_module_catalog,
    _notebook_worker_builder,
    _worker_module_unit,
)


class ResumeBarrierSession(ScriptedSession):
    """Expose each caller-owned resume boundary without changing RDBG semantics."""

    def __init__(self, stops):  # type: ignore[no-untyped-def]
        super().__init__(tuple(stops))
        self.root_export_entered = Event()
        self.release_root_export = Event()
        self.next_stop_wait_entered = Event()
        self.release_next_stop = Event()

    def evaluate(self, expression: str, **kwargs: object):  # type: ignore[no-untyped-def]
        if "ПоместитьЗначениеКонтекстаОтладки" in expression:
            self.root_export_entered.set()
            assert self.release_root_export.wait(2), "root export was not released"
        return super().evaluate(expression, **kwargs)

    def wait_for_any_stop(self, *, timeout_s: float):  # type: ignore[no-untyped-def]
        # The first wait belongs to execute_main() and must remain synchronous.
        if self.continue_count >= 2:
            self.next_stop_wait_entered.set()
            assert self.release_next_stop.wait(2), "next stop was not released"
        return super().wait_for_any_stop(timeout_s=timeout_s)


class WorkerResumeBarrierSession(ResumeBarrierSession):
    """Model the two trusted CAPTURE pin helpers around a real Worker host."""

    def evaluate(self, expression: str, **kwargs: object):  # type: ignore[no-untyped-def]
        if "ПинПоколенияWorker" in expression:
            return evaluation("Булево", "Истина")
        return super().evaluate(expression, **kwargs)


class ResumeBoundarySession(ScriptedSession):
    """Hold exactly one controller-owned continuation boundary."""

    def __init__(self, stage: str) -> None:
        super().__init__((CAPTURE_A, SERVICE, SERVICE))
        self.stage = stage
        self.entered = Event()
        self.release = Event()

    def _block(self, stage: str) -> None:
        if self.stage != stage:
            return
        self.entered.set()
        assert self.release.wait(2), f"{stage} was not released"

    def evaluate(self, expression: str, **kwargs: object):  # type: ignore[no-untyped-def]
        if "ПоместитьЗначениеКонтекстаОтладки" in expression:
            self._block("root")
        if "ЗавершитьКонтекстОтладки" in expression:
            self._block("cleanup")
        return super().evaluate(expression, **kwargs)

    def modify(self, variable: str, value_expression: str):  # type: ignore[no-untyped-def]
        # Initial MAIN setup also writes the command identifier.  The resume
        # writeback begins only after that MAIN has already continued once.
        if self.continue_count >= 1:
            self._block("modify")
        return super().modify(variable, value_expression)

    def continue_(self) -> None:
        super().continue_()
        if self.continue_count >= 2:
            self._block("continue")

    def wait_for_any_stop(self, *, timeout_s: float):  # type: ignore[no-untyped-def]
        if self.continue_count >= 2:
            self._block("wait")
        return super().wait_for_any_stop(timeout_s=timeout_s)


def eventually(predicate) -> None:  # type: ignore[no-untyped-def]
    deadline = monotonic() + 2
    while not predicate():
        assert monotonic() < deadline, "controller-owned resume did not settle"
        sleep(0.002)


@pytest.mark.parametrize(
    ("next_stop", "expected_kind"),
    (
        (SERVICE, RuntimeReplyKind.MAIN_COMPLETED),
        (CAPTURE_B, RuntimeReplyKind.CAPTURED),
        (USER, RuntimeReplyKind.DEBUG_STOPPED),
    ),
    ids=("terminal-main", "next-capture", "user-breakpoint"),
)
def test_resume_admission_is_controller_owned_before_root_export_and_preserves_next_stop(
    next_stop,
    expected_kind: RuntimeReplyKind,
) -> None:  # type: ignore[no-untyped-def]
    """The controller, not the caller writer, owns writeback through next stop."""
    stops = (CAPTURE_A, next_stop)
    if next_stop is SERVICE:
        stops += (SERVICE,)
    session = ResumeBarrierSession(stops)
    controller = captured_controller(session, command_timeout_s=1)
    api = PrototypeRuntimeApi(controller)
    old_view = api.current_capture()
    initial_status = api.status()
    initial_namespace = api.namespace_snapshot()
    replies: list[object] = []
    failures: list[BaseException] = []

    def resume() -> None:
        try:
            replies.append(api.resume_capture(dirty_roots=("Скаляр",)))
        except BaseException as error:
            failures.append(error)

    thread = Thread(target=resume)
    thread.start()
    try:
        assert session.root_export_entered.wait(1)

        # Admission is linearized before the first target-side root export;
        # status and the saved view are control-plane reads, not writer waits.
        assert api.status().state is OperationState.RESUMING
        assert old_view.status().phase is CapturePhase.RESUMING
        with pytest.raises(CaptureBusyError):
            api.execute_bsl("НоваяКоманда = 1;")
        with pytest.raises(CaptureBusyError):
            api.resume_capture()

        session.release_root_export.set()
        assert session.next_stop_wait_entered.wait(1)

        # Continue acknowledgement invalidates the old frame before the next
        # RDBG event, while the prior MAIN is still live.
        assert old_view.status().phase is CapturePhase.STALE
        assert api.status().state is OperationState.RESUMING

        session.release_next_stop.set()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert failures == []
        assert len(replies) == 1
        assert replies[0].kind is expected_kind
        assert session.continue_count == 2
        assert api.status().runtime_generation == initial_status.runtime_generation
        assert api.namespace_snapshot() == initial_namespace

        if expected_kind is RuntimeReplyKind.MAIN_COMPLETED:
            next_main = api.execute_bsl("СледующаяКоманда = 1;")
            assert next_main.kind is RuntimeReplyKind.MAIN_COMPLETED
            assert next_main.operation_id == replies[0].operation_id + 1
        elif expected_kind is RuntimeReplyKind.CAPTURED:
            successor = api.current_capture()
            assert successor != old_view
            assert successor.operation_id == old_view.operation_id
            assert successor.capture_generation == old_view.capture_generation
            assert successor.stop_sequence > old_view.stop_sequence
            assert successor.status().phase is CapturePhase.PAUSED
        else:
            assert api.status().state is OperationState.DEBUG_STOPPED
            with pytest.raises(ProtocolError, match="current state is debug_stopped"):
                api.execute_bsl("НоваяКоманда = 1;")
    finally:
        session.release_root_export.set()
        session.release_next_stop.set()
        thread.join(timeout=2)
        controller.shutdown_capture_evaluation()


def test_detached_resume_preserves_real_worker_pin_and_context_until_terminal(
    tmp_path: Path,
) -> None:
    """The accepted owner retains one old MAIN identity through its next event."""

    session = WorkerResumeBarrierSession((CAPTURE_A, SERVICE, SERVICE))
    controller = runtime_module().PrototypeRuntimeController(session, SERVICE)
    catalog = _common_module_catalog("МодульА")
    builder = _notebook_worker_builder(tmp_path)
    target = _UniverseInstructionExecutor()
    api = PrototypeRuntimeApi(
        controller,
        capture_points=(CAPTURE_A, CAPTURE_B),
        user_breakpoints=(USER,),
        notebook_worker_builder=builder,
        worker_module_builder=WorkerModuleArtifactBuilder(
            builder,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=target,
    )
    handle = api.load_worker_modules(
        (_worker_module_unit("МодульА", 17, catalog),),
        common_modules=catalog,
    )
    host = api._worker_universe
    initial = api.execute_bsl("СохраненноеИмя = 1;")
    assert initial.kind is RuntimeReplyKind.CAPTURED
    assert api.operation_worker_generation is handle
    old_view = api.current_capture()
    before = api.status()
    session.release_root_export.set()

    try:
        with pytest.raises(TimeoutError, match="resume remains pending"):
            api.resume_capture(dirty_roots=("Скаляр",), timeout_s=0.01)
        assert session.next_stop_wait_entered.wait(1)

        # A detached caller has no writer ownership, but the old MAIN's exact
        # Worker generation, runtime/context identity, and pending namespace
        # remain live until the controller receives the next RDBG event.
        assert api.status().state is OperationState.RESUMING
        assert api.status().runtime_generation == before.runtime_generation
        assert api.namespace_snapshot().context_generation == 1
        assert api.operation_worker_generation is handle
        assert old_view.status().phase is CapturePhase.STALE
        assert host._leases
        with pytest.raises(CaptureBusyError):
            api.execute_bsl("ЗапрещеноВоВремяResume = 1;")

        session.release_next_stop.set()
        eventually(lambda: controller.state is OperationState.COMPLETED)
        assert api.operation_worker_generation is None
        assert host._leases == {}
        assert api.namespace_snapshot().names == ("СохраненноеИмя",)
        assert "Скаляр" not in api.namespace_snapshot().names

        # The next MAIN stays on the same runtime/session and observes the
        # committed namespace only after the old controller-owned operation
        # became terminal.
        next_main = api.execute_bsl("СледующееИмя = 2;")
        assert next_main.kind is RuntimeReplyKind.MAIN_COMPLETED
        assert next_main.operation_id == initial.operation_id + 1
        assert api.status().runtime_generation == before.runtime_generation
        assert controller.session is session
        assert api.namespace_snapshot().names == (
            "СохраненноеИмя",
            "СледующееИмя",
        )
        assert session.continue_count == 3
    finally:
        session.release_root_export.set()
        session.release_next_stop.set()
        controller.shutdown_capture_evaluation()


def test_keyboard_interrupt_detaches_resume_waiter_and_controller_finishes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interrupt leaves the accepted resume and its event stream with the owner."""
    session = ResumeBarrierSession((CAPTURE_A, SERVICE, SERVICE))
    controller = captured_controller(session, command_timeout_s=1)
    api = PrototypeRuntimeApi(controller)
    old_view = api.current_capture()
    session.release_root_export.set()
    caller = current_thread()
    original_ticket_wait = CaptureResumeTicket.wait_initiator

    def interrupting_ticket_wait(
        ticket: CaptureResumeTicket,
        timeout_s: float | None = None,
    ) -> object:
        owner = ticket._coordinator
        original_condition_wait = owner._condition.wait

        def interrupt_wait(timeout: float | None = None) -> bool:
            if current_thread() is caller:
                raise KeyboardInterrupt
            return original_condition_wait(timeout)

        monkeypatch.setattr(owner._condition, "wait", interrupt_wait)
        try:
            return original_ticket_wait(ticket, timeout_s)
        finally:
            monkeypatch.setattr(owner._condition, "wait", original_condition_wait)

    try:
        monkeypatch.setattr(
            CaptureResumeTicket,
            "wait_initiator",
            interrupting_ticket_wait,
        )
        with pytest.raises(KeyboardInterrupt):
            api.resume_capture(dirty_roots=("Скаляр",))
        assert session.next_stop_wait_entered.wait(1)

        # The initiating waiter is gone, but exactly the accepted request owns
        # Continue and the next event; retrying must not redispatch it.
        assert api.status().state is OperationState.RESUMING
        with pytest.raises(CaptureBusyError):
            api.resume_capture()
        session.release_next_stop.set()
        eventually(lambda: controller.state is OperationState.COMPLETED)
        assert old_view.status().phase is CapturePhase.STALE
        assert session.continue_count == 2

        next_main = api.execute_bsl("СледующаяКоманда = 1;")
        assert next_main.kind is RuntimeReplyKind.MAIN_COMPLETED
    finally:
        session.release_root_export.set()
        session.release_next_stop.set()
        controller.shutdown_capture_evaluation()


def test_interrupt_after_resume_submission_detaches_the_initiator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interrupting the caller before ticket.wait cannot strand its delivery."""
    rdbg = ResumeBarrierSession((CAPTURE_A, SERVICE))
    controller = captured_controller(rdbg, command_timeout_s=1)
    api = PrototypeRuntimeApi(controller)

    @contextmanager
    def interrupting_handoff():  # type: ignore[no-untyped-def]
        raise KeyboardInterrupt
        yield

    monkeypatch.setattr(api, "_capture_owner_handoff", interrupting_handoff)
    rdbg.release_root_export.set()
    try:
        with pytest.raises(KeyboardInterrupt):
            api.resume_capture(dirty_roots=("Скаляр",))
        assert rdbg.next_stop_wait_entered.wait(1)
        owner = controller._capture_evaluation_coordinator
        assert owner is not None
        assert owner._active_resume is not None
        assert owner._active_resume.initiator_attached is False

        rdbg.release_next_stop.set()
        eventually(lambda: controller.state is OperationState.COMPLETED)
        assert rdbg.continue_count == 2
    finally:
        rdbg.release_root_export.set()
        rdbg.release_next_stop.set()
        controller.shutdown_capture_evaluation()


@pytest.mark.parametrize(
    ("handoff_error", "expected_error"),
    (
        (lambda: KeyboardInterrupt(), KeyboardInterrupt),
        (lambda: TimeoutError("resume handoff timed out"), TimeoutError),
    ),
    ids=("keyboard-interrupt", "timeout"),
)
def test_submission_adoption_receipt_detaches_session_waiter_before_ticket_return(
    monkeypatch: pytest.MonkeyPatch,
    handoff_error,
    expected_error: type[BaseException],
) -> None:  # type: ignore[no-untyped-def]
    """A post-adoption exception cannot strand the Session/MCP completion fence."""
    rdbg = ResumeBarrierSession((CAPTURE_A, SERVICE))
    controller = captured_controller(rdbg, command_timeout_s=1)
    api = PrototypeRuntimeApi(controller)
    runtime = object.__new__(RuntimeSession)
    runtime._operation_lock = RLock()
    runtime._capture_resume_listeners = []
    active = SimpleNamespace(
        ticket_id="capture-ticket",
        capture_intent_id="intent",
        operation_id="operation",
        capture_generation=1,
        source_revision=1,
        source_sha256="a" * 64,
        stop_sequence=1,
    )
    runtime._active_capture_ticket = active
    runtime.runtime_api = api
    terminal_fences: list[object] = []
    runtime.add_capture_resume_listener(terminal_fences.append)
    original_submit_resume = controller.submit_resume
    adopted = Event()

    def interrupt_after_adoption(**kwargs: object) -> CaptureResumeTicket:
        ticket = original_submit_resume(**kwargs)
        adopted.set()
        raise handoff_error()

    monkeypatch.setattr(controller, "submit_resume", interrupt_after_adoption)
    try:
        with pytest.raises(expected_error):
            runtime.resume_capture(dirty_roots=("Скаляр",))

        assert adopted.is_set()
        assert rdbg.root_export_entered.wait(1)
        owner = controller._capture_evaluation_coordinator
        assert owner is not None
        assert owner._active_resume is not None
        # The only proof available to RuntimeApi is the submission receipt:
        # Python never assigned the returned ticket to its local variable.
        assert owner._active_resume.initiator_attached is False
        with pytest.raises(CaptureBusyError):
            api.resume_capture()

        rdbg.release_root_export.set()
        assert rdbg.next_stop_wait_entered.wait(1)
        rdbg.release_next_stop.set()
        eventually(lambda: runtime._active_capture_ticket is None)
        assert terminal_fences and len(terminal_fences) == 1
        assert rdbg.continue_count == 2
    finally:
        rdbg.release_root_export.set()
        rdbg.release_next_stop.set()
        controller.shutdown_capture_evaluation()


@pytest.mark.parametrize("stage", ("root", "modify", "cleanup", "continue", "wait"))
def test_interrupt_at_every_resume_boundary_keeps_the_same_worker_plan(
    stage: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No boundary gives an interrupted caller back the resumed RDBG stream."""
    rdbg = ResumeBoundarySession(stage)
    controller = captured_controller(rdbg, command_timeout_s=1)
    api = PrototypeRuntimeApi(controller)
    original_wait = CaptureResumeTicket.wait_initiator

    def interrupt_after_boundary(
        ticket: CaptureResumeTicket,
        timeout_s: float | None = None,
    ) -> object:
        assert rdbg.entered.wait(1), f"{stage} was never reached"
        del ticket, timeout_s
        raise KeyboardInterrupt

    monkeypatch.setattr(CaptureResumeTicket, "wait_initiator", interrupt_after_boundary)
    try:
        with pytest.raises(KeyboardInterrupt):
            api.resume_capture(dirty_roots=("Скаляр",))
        with pytest.raises(CaptureBusyError):
            api.execute_bsl("НоваяКоманда = 1;")
        assert api.status().state is OperationState.RESUMING

        rdbg.release.set()
        eventually(lambda: controller.state is OperationState.COMPLETED)
        assert rdbg.continue_count == 2
    finally:
        rdbg.release.set()
        monkeypatch.setattr(CaptureResumeTicket, "wait_initiator", original_wait)
        controller.shutdown_capture_evaluation()


def test_resume_timeout_detaches_waiter_without_redispatching_continue() -> None:
    """A bounded initiator wait never cancels the accepted controller request."""
    session = ResumeBarrierSession((CAPTURE_A, SERVICE))
    controller = captured_controller(session, command_timeout_s=1)
    api = PrototypeRuntimeApi(controller)
    session.release_root_export.set()
    try:
        with pytest.raises(TimeoutError, match="resume remains pending"):
            api.resume_capture(dirty_roots=("Скаляр",), timeout_s=0.01)
        assert session.next_stop_wait_entered.wait(1)
        with pytest.raises(CaptureBusyError):
            api.resume_capture()
        session.release_next_stop.set()
        eventually(lambda: controller.state is OperationState.COMPLETED)
        assert session.continue_count == 2
    finally:
        session.release_root_export.set()
        session.release_next_stop.set()
        controller.shutdown_capture_evaluation()


def test_failed_writeback_publishes_typed_recovery_instead_of_stale_capture() -> None:
    """A confirmed failed root does not make recovery look like a stale frame."""
    rdbg = ScriptedSession((CAPTURE_A,), failed_roots=("Скаляр",))
    controller = captured_controller(rdbg, command_timeout_s=1)
    api = PrototypeRuntimeApi(controller)
    old_view = api.current_capture()
    try:
        with pytest.raises(PartialWritebackError, match="Скаляр"):
            api.resume_capture(dirty_roots=("Скаляр",))

        assert controller.state is OperationState.PARTIAL_WRITEBACK_FAILURE
        assert old_view.status().phase is CapturePhase.RECOVERY_REQUIRED
        assert api.status().state is OperationState.RECOVERING
        with pytest.raises(CaptureRecoveryRequiredError):
            api.resume_capture()
    finally:
        controller.shutdown_capture_evaluation()


def test_confirmed_pre_mutation_writeback_failure_keeps_the_session_capture_paused() -> None:
    """A rejected root export cannot discard the still-live CAPTURE fence."""

    class RootExportFailureSession(ScriptedSession):
        fail_export = True

        def evaluate(self, expression: str, **kwargs: object):  # type: ignore[no-untyped-def]
            if self.fail_export and "ПоместитьЗначениеКонтекстаОтладки" in expression:
                return evaluation("Ошибка", "", error="planned root export failure")
            return super().evaluate(expression, **kwargs)

    rdbg = RootExportFailureSession((CAPTURE_A, SERVICE))
    controller = captured_controller(rdbg, command_timeout_s=1)
    api = PrototypeRuntimeApi(controller)
    runtime = object.__new__(RuntimeSession)
    runtime._operation_lock = RLock()
    runtime._capture_resume_listeners = []
    active = SimpleNamespace(
        ticket_id="capture-ticket",
        capture_intent_id="intent",
        operation_id="operation",
        capture_generation=1,
        source_revision=1,
        source_sha256="a" * 64,
        stop_sequence=1,
    )
    runtime._active_capture_ticket = active
    runtime.runtime_api = api
    delivered: list[object] = []
    runtime.add_capture_resume_listener(delivered.append)
    old_view = api.current_capture()
    try:
        with pytest.raises(PartialWritebackError, match="root export failure"):
            runtime.resume_capture(dirty_roots=("Скаляр",))

        assert controller.state is OperationState.CAPTURED
        assert old_view.status().phase is CapturePhase.PAUSED
        assert runtime._active_capture_ticket is active
        assert delivered == []
        assert rdbg.continue_count == 1

        rdbg.fail_export = False
        completed = runtime.resume_capture(dirty_roots=("Скаляр",))
        assert completed.kind is RuntimeReplyKind.MAIN_COMPLETED
        assert runtime._active_capture_ticket is None
        assert len(delivered) == 1
        assert rdbg.continue_count == 2
    finally:
        controller.shutdown_capture_evaluation()


@pytest.mark.parametrize(
    ("stage", "expected_error", "expected_continue_count"),
    (
        ("after-first-root", RdbgTransportError, 1),
        ("cleanup", PartialWritebackError, 1),
        ("continue", RdbgTransportError, 2),
    ),
)
def test_resume_failure_after_mutation_or_required_step_requires_recovery(
    stage: str,
    expected_error: type[BaseException],
    expected_continue_count: int,
) -> None:
    """Only a proven pre-mutation rejection may retain a paused capture."""

    class RequiredStepFailureSession(ScriptedSession):
        def evaluate(self, expression: str, **kwargs: object):  # type: ignore[no-untyped-def]
            if stage == "cleanup" and "ЗавершитьКонтекстОтладки" in expression:
                return evaluation("Ошибка", "", error="planned cleanup failure")
            return super().evaluate(expression, **kwargs)

        def continue_(self) -> None:
            super().continue_()
            if stage == "continue" and self.continue_count == 2:
                raise RdbgTransportError("planned uncertain Continue")

    def fault(point: FaultPoint) -> None:
        if stage == "after-first-root" and point is FaultPoint.AFTER_FIRST_ROOT_WRITE:
            raise RdbgTransportError("planned post-mutation transport loss")

    rdbg = RequiredStepFailureSession((CAPTURE_A, SERVICE))
    controller = captured_controller(rdbg, command_timeout_s=1, fault_hook=fault)
    api = PrototypeRuntimeApi(controller)
    old_view = api.current_capture()
    try:
        with pytest.raises(expected_error):
            api.resume_capture(dirty_roots=("Скаляр",))

        assert controller.state in {
            OperationState.PARTIAL_WRITEBACK_FAILURE,
            OperationState.RECOVERING,
        }
        assert old_view.status().phase is CapturePhase.RECOVERY_REQUIRED
        with pytest.raises(CaptureRecoveryRequiredError):
            api.resume_capture()
        assert rdbg.continue_count == expected_continue_count
    finally:
        controller.shutdown_capture_evaluation()


def test_unproven_close_keeps_a_live_resume_record_until_its_worker_exits() -> None:
    """Shutdown cannot call an in-flight resume finalized before it is joined."""
    rdbg = ResumeBoundarySession("wait")
    controller = captured_controller(rdbg, command_timeout_s=1)
    api = PrototypeRuntimeApi(controller)
    try:
        with pytest.raises(TimeoutError, match="resume remains pending"):
            api.resume_capture(dirty_roots=("Скаляр",), timeout_s=0.01)
        assert rdbg.entered.wait(1)
        owner = controller._capture_evaluation_coordinator
        assert owner is not None

        owner.begin_close()
        owner.finish_close(False)

        assert owner._active_resume is not None
        assert owner._shutdown_finalized is False
        rdbg.release.set()
        assert owner.join(1)
        owner.finish_close(True)
        assert owner._shutdown_finalized is True
    finally:
        rdbg.release.set()
        controller.shutdown_capture_evaluation()


def test_attached_resume_notifies_session_listener_on_initiating_thread() -> None:
    """A completion listener cannot run on the coordinator while its caller waits."""
    rdbg = ResumeBarrierSession((CAPTURE_A, SERVICE))
    controller = captured_controller(rdbg, command_timeout_s=1)
    api = PrototypeRuntimeApi(controller)
    runtime = object.__new__(RuntimeSession)
    runtime._operation_lock = RLock()
    runtime._capture_resume_listeners = []
    runtime._active_capture_ticket = SimpleNamespace(
        ticket_id="capture-ticket",
        capture_intent_id="intent",
        operation_id="operation",
        capture_generation=1,
        source_revision=1,
        source_sha256="a" * 64,
        stop_sequence=1,
    )
    runtime.runtime_api = api
    listener_threads: list[int] = []
    runtime.add_capture_resume_listener(lambda _fence: listener_threads.append(get_ident()))
    rdbg.release_root_export.set()
    rdbg.release_next_stop.set()
    try:
        completed = runtime.resume_capture(dirty_roots=("Скаляр",))

        assert completed.kind is RuntimeReplyKind.MAIN_COMPLETED
        # A real MCP capture admission lock is reentrant for this initiating
        # thread but blocks the coordinator worker.  Keeping this callback on
        # the caller side prevents the ticket/listener lock cycle.
        assert listener_threads == [get_ident()]
    finally:
        rdbg.release_root_export.set()
        rdbg.release_next_stop.set()
        controller.shutdown_capture_evaluation()


def test_detached_session_waiter_is_retired_after_worker_terminal_delivery() -> None:
    """Timeout detaches only the waiter; the worker later retires Session state."""
    rdbg = ResumeBarrierSession((CAPTURE_A, SERVICE))
    controller = captured_controller(rdbg, command_timeout_s=1)
    api = PrototypeRuntimeApi(controller)
    runtime = object.__new__(RuntimeSession)
    runtime._operation_lock = RLock()
    runtime._capture_resume_listeners = []
    active = SimpleNamespace(
        ticket_id="capture-ticket",
        capture_intent_id="intent",
        operation_id="operation",
        capture_generation=1,
        source_revision=1,
        source_sha256="a" * 64,
        stop_sequence=1,
    )
    runtime._active_capture_ticket = active
    runtime.runtime_api = api
    listener_threads: list[int] = []
    runtime.add_capture_resume_listener(lambda _fence: listener_threads.append(get_ident()))
    coordinator = controller._capture_evaluation_coordinator
    assert coordinator is not None
    coordinator_thread_id = coordinator._worker.ident
    rdbg.release_root_export.set()
    try:
        with pytest.raises(TimeoutError, match="resume remains pending"):
            runtime.resume_capture(dirty_roots=("Скаляр",), timeout_s=0.01)
        assert rdbg.next_stop_wait_entered.wait(1)
        assert runtime._active_capture_ticket is active
        rdbg.release_next_stop.set()

        eventually(lambda: runtime._active_capture_ticket is None)
        assert listener_threads and listener_threads[0] != get_ident()
        assert listener_threads[0] != coordinator_thread_id
        assert rdbg.continue_count == 2
    finally:
        rdbg.release_root_export.set()
        rdbg.release_next_stop.set()
        controller.shutdown_capture_evaluation()
