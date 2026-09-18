"""File-mode Stop may retire only the exact owned 1C debuggee process."""

from threading import Event, get_ident
from uuid import uuid4

import pytest

from onec_runtime.errors import StopWaitIntervalElapsed
from onec_runtime.execution.arbiter import (
    ArbiterBusy, OutcomeUnknown, RdbgArbiter, RouteToken, Settlement,
    TargetTerminated,
)
from onec_runtime.execution.termination import (
    FileTargetProcessLease, FileTerminationConfirmed, FileTerminationUnknown,
)
from onec_runtime.rdbg.models import DebugTarget, PendingEvaluation, TargetId


class _PollableProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode


class _OwnedDebuggee:
    def __init__(self, *, exit_on_close: bool = True) -> None:
        self.pid = 1234
        self.process = _PollableProcess()
        self.exit_on_close = exit_on_close
        self.close_calls: list[tuple[float, int]] = []

    def close(self, timeout_s: float) -> None:
        self.close_calls.append((timeout_s, get_ident()))
        if self.exit_on_close:
            self.process.returncode = -15


class _FileSession:
    def __init__(self, target: TargetId) -> None:
        self.target = DebugTarget(target, "ServerEmulation", "stopped")
        self.calls: list[tuple[str, int]] = []

    def continue_(self, *, on_transport_dispatch) -> None:
        on_transport_dispatch()
        self.calls.append(("continue", get_ident()))


def _unknown_file_ticket(*, exit_on_close: bool = True):
    target = TargetId(uuid4(), "file-test", uuid4())
    process = _OwnedDebuggee(exit_on_close=exit_on_close)
    session = _FileSession(target)
    route = RouteToken("file-stop", 1, 0, "main")
    arbiter = RdbgArbiter(
        session, route, file_target_lease=FileTargetProcessLease(target, process),
    )

    def main(port):
        port.continue_()
        raise OutcomeUnknown("matching stop is not known")

    ticket = arbiter.submit(route, main)
    arbiter.dispatch(ticket)
    assert ticket.wait_unknown(3)
    return session, route, arbiter, ticket, target, process


def test_file_stop_automatically_terminates_exact_owned_debuggee_on_worker() -> None:
    session, route, arbiter, ticket, target, process = _unknown_file_ticket()

    assert arbiter.request_stop(ticket).name == "REQUESTED"
    attempt = ticket.stop_teardown
    assert attempt is not None
    result = attempt.wait(3)
    assert isinstance(result, FileTerminationConfirmed)
    assert result.expected_target == target
    assert result.pid == process.pid and result.returncode == -15
    with pytest.raises(TargetTerminated) as stopped:
        ticket.wait_settled(3)
    assert stopped.value.evidence is result
    assert [name for name, _ in session.calls] == ["continue"]
    assert len(process.close_calls) == 1
    assert process.close_calls[0][1] == session.calls[0][1]
    with pytest.raises(RuntimeError, match="closed"):
        arbiter.submit(route, lambda port: Settlement("unsafe"))
    arbiter.close(timeout=3)


def test_unknown_file_exit_keeps_owner_and_retry_only_polls_original_process() -> None:
    _session, route, arbiter, ticket, target, process = _unknown_file_ticket(
        exit_on_close=False,
    )

    assert arbiter.request_stop(ticket).name == "REQUESTED"
    first = ticket.stop_teardown
    assert first is not None
    unknown = first.wait(3)
    assert isinstance(unknown, FileTerminationUnknown)
    assert unknown.expected_target == target
    assert arbiter.active_ticket is ticket
    with pytest.raises(ArbiterBusy):
        arbiter.submit(route, lambda port: Settlement("unsafe"))

    process.process.returncode = -15
    second = arbiter.teardown_fenced_file_target(ticket, route, grace_s=0.1)
    assert ticket.stop_teardown is second
    result = second.wait(3)
    assert isinstance(result, FileTerminationConfirmed)
    assert len(process.close_calls) == 1
    assert process.close_calls[0][0] == 30.0
    with pytest.raises(TargetTerminated):
        ticket.wait_settled(3)
    arbiter.close(timeout=3)


def test_file_capture_eval_keeps_exact_pending_until_debuggee_exit_proof() -> None:
    target = TargetId(uuid4(), "file-test")
    process = _OwnedDebuggee(exit_on_close=False)

    class CaptureSession(_FileSession):
        def __init__(self):
            super().__init__(target)
            self.pending = PendingEvaluation(target, uuid4(), self)

        def start_evaluation(self, expression, *, on_transport_dispatch, **kwargs):
            on_transport_dispatch()
            self.calls.append(("eval", get_ident()))
            return self.pending

    session = CaptureSession()
    route = RouteToken("file-capture-stop", 1, 0, "capture")
    arbiter = RdbgArbiter(
        session, route, file_target_lease=FileTargetProcessLease(target, process),
    )

    def capture(port):
        port.start_evaluation("long-running")
        raise OutcomeUnknown("eval result is still pending")

    ticket = arbiter.submit(route, capture)
    arbiter.dispatch(ticket)
    assert ticket.wait_unknown(3)
    assert arbiter.request_stop(ticket).name == "REQUESTED"
    first = ticket.stop_teardown
    assert first is not None
    assert isinstance(first.wait(3), FileTerminationUnknown)
    assert ticket.status().pending_capability is session.pending
    assert arbiter.active_ticket is ticket

    process.process.returncode = -15
    second = arbiter.teardown_fenced_file_target(ticket, route)
    assert isinstance(second.wait(3), FileTerminationConfirmed)
    with pytest.raises(TargetTerminated):
        ticket.wait_settled(3)
    assert len(process.close_calls) == 1
    arbiter.close(timeout=3)


def test_file_main_stop_yields_worker_after_next_bounded_poll() -> None:
    target = TargetId(uuid4(), "file-test")
    process = _OwnedDebuggee()
    entered_wait = Event()
    release_wait = Event()

    class WaitingSession(_FileSession):
        def wait_for_any_stop(self, *, timeout_s, expected_target, on_transport_dispatch):
            assert expected_target == target
            on_transport_dispatch()
            self.calls.append(("wait", get_ident()))
            entered_wait.set()
            assert release_wait.wait(3)
            raise StopWaitIntervalElapsed("bounded poll")

    session = WaitingSession(target)
    route = RouteToken("file-main-poll-stop", 1, 0, "main")
    arbiter = RdbgArbiter(
        session, route, file_target_lease=FileTargetProcessLease(target, process),
    )

    def main(port):
        port.continue_()
        while True:
            try:
                return Settlement(port.wait_for_any_stop(timeout_s=0.01))
            except StopWaitIntervalElapsed:
                continue

    ticket = arbiter.submit(route, main)
    arbiter.dispatch(ticket)
    assert entered_wait.wait(3)
    assert arbiter.request_stop(ticket).name == "REQUESTED"
    assert ticket.stop_teardown is None
    release_wait.set()
    with pytest.raises(TargetTerminated):
        ticket.wait_settled(3)
    assert [name for name, _ in session.calls] == ["continue", "wait"]
    assert len(process.close_calls) == 1
    assert process.close_calls[0][1] == session.calls[0][1]
    arbiter.close(timeout=3)


def test_file_stop_rejects_lease_for_another_stopped_target() -> None:
    target = TargetId(uuid4(), "file-test")
    session = _FileSession(target)
    process = _OwnedDebuggee()

    with pytest.raises(ValueError, match="exact selected file target"):
        RdbgArbiter(
            session, RouteToken("file-stop", 1, 0, "main"),
            file_target_lease=FileTargetProcessLease(
                TargetId(uuid4(), "file-test"), process,
            ),
        )

    assert process.close_calls == []


def test_file_exit_proof_requires_exact_leased_process() -> None:
    from onec_runtime.execution.termination import ServerTerminationConfirmed
    from onec_runtime.rdbg.session import BoundServerTargetAbsence

    _session, route, arbiter, ticket, target, process = _unknown_file_ticket()
    server_proof = ServerTerminationConfirmed(
        target,
        BoundServerTargetAbsence(TargetId(uuid4(), target.infobase_alias), target, 1.0, 1),
    )
    try:
        with pytest.raises(ValueError, match='server.*backend|selected server target'):
            arbiter.retire_terminated_target(ticket, route, server_proof)
        with pytest.raises(ValueError, match='file.*process|file.*lease'):
            arbiter.retire_terminated_target(
                ticket, route, FileTerminationConfirmed(target, process.pid + 1, -15),
            )
        assert arbiter.active_ticket is ticket
        assert ticket.wait_unknown(0)
    finally:
        if arbiter.active_ticket is ticket:
            arbiter.retire_terminated_target(
                ticket, route, FileTerminationConfirmed(target, process.pid, -15),
            )
        arbiter.close(timeout=3)


def test_file_owner_rejects_server_proof_after_selected_target_kind_changes() -> None:
    from onec_runtime.execution.termination import ServerTerminationConfirmed
    from onec_runtime.rdbg.session import BoundServerTargetAbsence

    session, route, arbiter, ticket, target, process = _unknown_file_ticket()
    selected = session.target
    session.target = DebugTarget(target, 'Server', 'stopped')
    proof = ServerTerminationConfirmed(
        target,
        BoundServerTargetAbsence(
            TargetId(uuid4(), target.infobase_alias, target.seance_id), target, 1.0, 1,
        ),
    )
    try:
        with pytest.raises(ValueError, match='server.*backend|file.*backend'):
            arbiter.retire_terminated_target(ticket, route, proof)
        assert arbiter.active_ticket is ticket
    finally:
        session.target = selected
        if arbiter.active_ticket is ticket:
            arbiter.retire_terminated_target(
                ticket, route, FileTerminationConfirmed(target, process.pid, -15),
            )
        arbiter.close(timeout=3)
