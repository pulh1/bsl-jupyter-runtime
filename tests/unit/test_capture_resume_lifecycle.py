from __future__ import annotations

from _thread import interrupt_main
from threading import Event, Thread, Timer
from time import monotonic, sleep

import pytest

from onec_runtime.capture_evaluation import CapturePhase
from onec_runtime.errors import CaptureBusyError
from onec_runtime.prototype_runtime import OperationState
from onec_runtime.runtime_api import PrototypeRuntimeApi, RuntimeReplyKind

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


def test_keyboard_interrupt_detaches_resume_waiter_and_controller_finishes_once() -> None:
    """An interrupt leaves the accepted resume and its event stream with the owner."""
    session = ResumeBarrierSession((CAPTURE_A, SERVICE, SERVICE))
    controller = captured_controller(session, command_timeout_s=1)
    api = PrototypeRuntimeApi(controller)
    old_view = api.current_capture()
    interrupt = Timer(0.05, interrupt_main)
    interrupt.start()
    try:
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
        interrupt.cancel()
        session.release_root_export.set()
        session.release_next_stop.set()
        controller.shutdown_capture_evaluation()
