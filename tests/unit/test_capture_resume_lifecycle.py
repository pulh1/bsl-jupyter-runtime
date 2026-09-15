from __future__ import annotations

from threading import Event, RLock, Thread, current_thread, get_ident
from types import SimpleNamespace
from time import monotonic, sleep

import pytest

from onec_runtime.capture_evaluation import CapturePhase, CaptureResumeTicket
from onec_runtime.errors import CaptureBusyError
from onec_runtime.prototype_runtime import OperationState
from onec_runtime.runtime_api import PrototypeRuntimeApi, RuntimeReplyKind
from onec_runtime.session import RuntimeSession

from test_prototype_runtime import CAPTURE_A, CAPTURE_B, SERVICE, USER, ScriptedSession, captured_controller


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

        if expected_kind is RuntimeReplyKind.MAIN_COMPLETED:
            next_main = api.execute_bsl("СледующаяКоманда = 1;")
            assert next_main.kind is RuntimeReplyKind.MAIN_COMPLETED
            assert next_main.operation_id == replies[0].operation_id + 1
    finally:
        session.release_root_export.set()
        session.release_next_stop.set()
        thread.join(timeout=2)
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
