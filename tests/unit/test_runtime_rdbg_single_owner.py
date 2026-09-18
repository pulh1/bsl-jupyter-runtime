"""One RDBG owner across routes with an injected post-bootstrap public facade.

This does not exercise RuntimeSession.start() or the default bootstrap path.
"""

from collections.abc import Callable
from pathlib import Path
from threading import Event, Thread, current_thread
from types import SimpleNamespace
from typing import TypeVar

import pytest

from onec_runtime.capture_evaluation import CapturePhase
from onec_runtime.config import RuntimeConfig
from onec_runtime.errors import NoActiveCaptureError, ProtocolError
from onec_runtime.execution.post_bootstrap import compose_fresh_post_bootstrap_execution
from onec_runtime.runtime_api import RuntimeReplyKind
from onec_runtime.session import RuntimeSession, RuntimeSessionConfig

from test_execution_controller_routes import BUSINESS, KERNEL, CompleteSession


_Result = TypeVar("_Result")


def _notebook_call(action: Callable[[], _Result]) -> tuple[_Result, Thread]:
    """Give each notebook call a distinct caller without hiding its failure."""
    result: list[_Result] = []
    failures: list[BaseException] = []

    def run() -> None:
        try:
            result.append(action())
        except BaseException as error:
            failures.append(error)

    caller = Thread(target=run, name="notebook-caller")
    caller.start()
    caller.join(timeout=5)
    assert not caller.is_alive(), "notebook call did not settle"
    if failures:
        raise failures[0]
    return result[0], caller


def test_runtime_session_routes_main_capture_resume_and_heartbeat_through_one_owner(
    tmp_path: Path,
) -> None:
    """Break: public Session routes or heartbeat bypass the single RDBG owner."""

    class GatedSession(CompleteSession):
        def __init__(self) -> None:
            super().__init__()
            self.eval_waiting = Event()
            self.release_eval = Event()
            self._last_expression = ""
            self.hold_variable_read = False
            self.variable_waiting = Event()
            self.release_variable = Event()

        def local_variables(self, stack_level=0, **kwargs):
            if self.hold_variable_read and stack_level == 0:
                self.variable_waiting.set()
                assert self.release_variable.wait(5), "CAPTURE variable read was not released"
            return super().local_variables(stack_level, **kwargs)

        def start_evaluation(self, expression: str, **kwargs: object):
            self._last_expression = expression
            return super().start_evaluation(expression, **kwargs)

        def wait_evaluation_event(self, pending, *, timeout_s: float,
                                  on_transport_dispatch):
            if "ВыполнитьКодВКонтекстеОтладки" in self._last_expression:
                self.eval_waiting.set()
                assert self.release_eval.wait(5), "CAPTURE evaluation was not released"
            return super().wait_evaluation_event(
                pending, timeout_s=timeout_s,
                on_transport_dispatch=on_transport_dispatch,
            )

        def heartbeat(self, *, on_transport_dispatch=None) -> dict[str, object]:
            if on_transport_dispatch is not None:
                on_transport_dispatch()
            self._record("heartbeat")
            return {"rtt_ms": 1.0}

    platform_bin = tmp_path / "bin"
    platform_bin.mkdir()
    for executable in ("1cv8.exe", "1cv8c.exe", "dbgs.exe"):
        (platform_bin / executable).touch()
    infobase = tmp_path / "base"
    infobase.mkdir()
    (infobase / "1Cv8.1CD").write_bytes(b"synthetic")
    config = RuntimeSessionConfig(
        RuntimeConfig(
            workspace=tmp_path / "workspace",
            platform_bin=platform_bin,
            connection_string=f'File="{infobase}";',
        ),
        evidence_root=tmp_path / "evidence",
    )
    session = GatedSession()
    composed = compose_fresh_post_bootstrap_execution(
        session, KERNEL,
        runtime_generation=7,
        stopped_target=session.target,
        capture_locations=(BUSINESS,),
        notebook_builder=lambda *_args, **_kwargs: None,
    )
    api = composed.execution.facade
    process_checks: list[str] = []
    runtime = RuntimeSession(
        config,
        SimpleNamespace(ensure_running=lambda: process_checks.append("checked")),
        SimpleNamespace(),
        session,
        api,
        SimpleNamespace(append_jsonl=lambda *_args: None),
        heartbeat_interval_s=60.0,
    )
    runtime.verify_capture_points = lambda points: (
        SimpleNamespace(location=BUSINESS),
    )
    intent = SimpleNamespace(
        points=(object(),),
        capture_intent_id="capture-intent",
        operation_id="mcp-operation",
        capture_generation=1,
        source_revision=1,
        source_sha256="source-hash",
    )
    armed = runtime.arm_capture_intent(intent)

    calls: list[tuple[str, Thread]] = []
    for name in (
        "set_breakpoints",
        "modify",
        "continue_",
        "wait_for_any_stop",
        "local_variables",
        "start_evaluation",
        "wait_evaluation_event",
        "heartbeat",
    ):
        original = getattr(session, name)

        def record(*args: object, _name=name, _original=original, **kwargs: object):
            calls.append((_name, current_thread()))
            return _original(*args, **kwargs)

        setattr(session, name, record)

    capture_reply: list[object] = []
    capture_errors: list[BaseException] = []

    def run_capture() -> None:
        try:
            capture_reply.append(runtime.execute_bsl("РезультатИнструкции = 2;"))
        except BaseException as error:
            capture_errors.append(error)

    capture_caller = Thread(target=run_capture, name="capture-notebook-caller")
    try:
        captured, main_caller = _notebook_call(
            lambda: runtime.execute_bsl("Результат = 1;")
        )
        assert captured.kind is RuntimeReplyKind.CAPTURED
        assert captured.capture_ticket == armed.ticket_id
        assert armed.expected_controller_operation_id == captured.operation_id
        assert armed.expected_stop_sequence == captured.stop_sequence
        capture_fence = SimpleNamespace(
            capture_intent_id=intent.capture_intent_id,
            operation_id=intent.operation_id,
            capture_generation=intent.capture_generation,
            source_revision=intent.source_revision,
            source_sha256=intent.source_sha256,
            stop_sequence=captured.stop_sequence,
        )
        stack_page = runtime.capture_stack(capture_fence, cursor=0, limit=10)
        assert stack_page["total"] >= 2
        frame_page = runtime.capture_frame(
            capture_fence, level=0, cursor=0, limit=10,
        )
        assert frame_page["frame"]["level"] == 0
        session.hold_variable_read = True
        variable_reply: list[object] = []
        variable_errors: list[BaseException] = []

        def read_named_variable() -> None:
            try:
                variable_reply.append(runtime.capture_frame(
                    capture_fence, level=0, cursor=0, limit=1, name="Amount",
                ))
            except BaseException as error:
                variable_errors.append(error)

        variable_caller = Thread(target=read_named_variable, name="variable-caller")
        variable_caller.start()
        assert session.variable_waiting.wait(5)
        assert runtime._operation_lock.acquire(blocking=False), (
            "Session admission lock was retained during an owned variable read"
        )
        runtime._operation_lock.release()
        session.release_variable.set()
        variable_caller.join(timeout=5)
        assert not variable_caller.is_alive()
        assert variable_errors == []
        assert variable_reply[0]["variables"][0]["name"] == "Amount"
        capture_view = runtime.current_capture()
        assert capture_view.status().phase is CapturePhase.PAUSED

        capture_caller.start()
        assert session.eval_waiting.wait(5), "CAPTURE eval was not dispatched"
        runtime._heartbeat_tick()
        assert not any(name == "heartbeat" for name, _ in calls)
        assert process_checks == ["checked"]

        session.release_eval.set()
        capture_caller.join(timeout=5)
        assert not capture_caller.is_alive(), "CAPTURE notebook call did not settle"
        assert capture_errors == []
        assert len(capture_reply) == 1
        assert capture_reply[0].kind is RuntimeReplyKind.CAPTURE_CELL

        completed, resume_caller = _notebook_call(runtime.resume_capture)
        assert completed.kind is RuntimeReplyKind.MAIN_COMPLETED
        assert capture_view.status().phase is CapturePhase.STALE
        with pytest.raises(ProtocolError, match="capture frame is stale"):
            runtime.capture_stack(capture_fence, cursor=0, limit=1)
        with pytest.raises(NoActiveCaptureError):
            runtime.current_capture()
        runtime._heartbeat_tick()
        assert process_checks == ["checked", "checked"]

        event_calls = [name for name, _ in calls if name.startswith("wait_")]
        assert "wait_for_any_stop" in event_calls
        assert "wait_evaluation_event" in event_calls
        owners = {thread for _, thread in calls}
        assert len(owners) == 1, [
            (name, thread.name, thread.ident) for name, thread in calls
        ]
        assert owners.isdisjoint({main_caller, capture_caller, resume_caller})
        assert any(name == "heartbeat" for name, _ in calls)
    finally:
        session.release_eval.set()
        session.release_variable.set()
        if capture_caller.ident is not None:
            capture_caller.join(timeout=5)
        runtime._heartbeat_stop.set()
        runtime._heartbeat_thread.join(timeout=2)
        api.close()
