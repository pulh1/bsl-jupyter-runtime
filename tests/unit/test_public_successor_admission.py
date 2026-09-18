"""Successor CAPTURE intent stays correlated to the same MAIN operation.

The post-bootstrap facade is injected manually; RuntimeSession.start() is not
part of this contract until the full public cutover.
"""

from dataclasses import replace
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from onec_runtime.config import RuntimeConfig
from onec_runtime.capture_evaluation import CapturePhase
from onec_runtime.errors import ProtocolError
from onec_runtime.execution.post_bootstrap import compose_fresh_post_bootstrap_execution
from onec_runtime.rdbg.models import EvaluationResult
from onec_runtime.execution.continuation_models import ContinuationAttemptSpec
from onec_runtime.runtime_models import RuntimeReplyKind
from onec_runtime.session import RuntimeSession, RuntimeSessionConfig
from onec_runtime_mcp.agent.runtime_backend import OnecRuntimeBackend
from onec_runtime_mcp.agent.runtime_session import AgentRuntimeSession

from test_execution_controller_routes import BUSINESS, KERNEL, CompleteSession


class ExportFailsOnce(CompleteSession):
    def __init__(self) -> None:
        super().__init__(capture_count=2)
        self.fail_export = True

    def wait_evaluation_event(self, pending, *, timeout_s, on_transport_dispatch):
        if (
            self.fail_export
            and self.expression.startswith(
                "RuntimeKernelServer.ПоместитьЗначениеКонтекстаОтладки("
            )
        ):
            self.fail_export = False
            on_transport_dispatch()
            self._record("wait_eval")
            self.pending = None
            return EvaluationResult(
                pending.result_id, "Ошибка", "", True, "planned export failure",
            )
        return super().wait_evaluation_event(
            pending, timeout_s=timeout_s,
            on_transport_dispatch=on_transport_dispatch,
        )


def test_paused_capture_can_rearm_points_and_ticket_same_main() -> None:
    rdbg = CompleteSession(capture_count=2)
    composed = compose_fresh_post_bootstrap_execution(
        rdbg, KERNEL, runtime_generation=7,
        stopped_target=rdbg.target, capture_locations=(BUSINESS,),
        notebook_builder=lambda *_args, **_kwargs: None,
    )
    api = composed.execution.facade
    try:
        api.configure_capture_points((BUSINESS,))
        first_ticket = api.prepare_capture_ticket()
        first = api.execute_bsl("Результат = 1;")
        assert first.kind is RuntimeReplyKind.CAPTURED
        assert first.capture_ticket == first_ticket.ticket_id

        other = replace(BUSINESS, line=51)
        api.configure_continuation_capture_points((other,))
        assert composed.breakpoint_workspace.confirmed_snapshot.captures == (other,)
        next_ticket = api.prepare_capture_ticket()
        assert next_ticket.expected_operation_id == first.operation_id
        assert next_ticket.expected_stop_sequence == 2
        api.configure_continuation_capture_points((BUSINESS,))
        second = api.resume_capture()
        assert second.kind is RuntimeReplyKind.CAPTURED
        assert second.operation_id == first.operation_id
        assert second.capture_ticket == next_ticket.ticket_id
    finally:
        api.close()


def test_session_rearms_existing_capture_intent_for_next_stop(tmp_path: Path) -> None:
    platform = tmp_path / "bin"
    platform.mkdir()
    for name in ("1cv8.exe", "1cv8c.exe", "dbgs.exe"):
        (platform / name).touch()
    infobase = tmp_path / "base"
    infobase.mkdir()
    (infobase / "1Cv8.1CD").write_bytes(b"synthetic")
    config = RuntimeSessionConfig(
        RuntimeConfig(
            workspace=tmp_path / "workspace", platform_bin=platform,
            connection_string=f'File="{infobase}";',
        ),
        evidence_root=tmp_path / "evidence",
    )
    rdbg = CompleteSession(capture_count=2)
    api = compose_fresh_post_bootstrap_execution(
        rdbg, KERNEL, runtime_generation=7,
        stopped_target=rdbg.target, capture_locations=(BUSINESS,),
        notebook_builder=lambda *_args, **_kwargs: None,
    ).execution.facade
    runtime = RuntimeSession(
        config, SimpleNamespace(ensure_running=lambda: None),
        SimpleNamespace(), rdbg, api,
        SimpleNamespace(append_jsonl=lambda *_args: None),
        heartbeat_interval_s=60.0,
    )
    point = SimpleNamespace(name="capture", line=50)
    runtime.verify_capture_points = lambda _points: (
        SimpleNamespace(location=BUSINESS),
    )

    def intent(generation: int):
        return SimpleNamespace(
            points=(point,), capture_intent_id=f"intent-{generation}",
            operation_id=f"request-{generation}",
            capture_generation=generation, source_revision=1,
            source_sha256="source-hash",
        )

    try:
        first_arming = runtime.arm_capture_intent(intent(1))
        first = runtime.execute_bsl("Результат = 1;")
        assert first.capture_ticket == first_arming.ticket_id

        second_arming = runtime.arm_capture_intent(intent(2))
        assert second_arming.expected_controller_operation_id == first.operation_id
        assert second_arming.expected_stop_sequence == 2
        second = runtime.resume_capture()
        assert second.kind is RuntimeReplyKind.CAPTURED
        assert second.capture_ticket == second_arming.ticket_id
    finally:
        runtime._heartbeat_stop.set()
        runtime._heartbeat_thread.join(timeout=2)
        api.close()


def test_successor_commit_waits_for_confirmed_next_stop() -> None:
    class HeldSecondStop(CompleteSession):
        def __init__(self) -> None:
            super().__init__(capture_count=2)
            self.stop_calls = 0
            self.waiting = Event()
            self.release = Event()

        def wait_for_any_stop(self, **kwargs):
            self.stop_calls += 1
            if self.stop_calls == 2:
                self.waiting.set()
                assert self.release.wait(5)
            return super().wait_for_any_stop(**kwargs)

    rdbg = HeldSecondStop()
    api = compose_fresh_post_bootstrap_execution(
        rdbg, KERNEL, runtime_generation=7,
        stopped_target=rdbg.target, capture_locations=(BUSINESS,),
        notebook_builder=lambda *_args, **_kwargs: None,
    ).execution.facade
    api.configure_capture_points((BUSINESS,))
    api.prepare_capture_ticket()
    assert api.execute_bsl("Результат = 1;").kind is RuntimeReplyKind.CAPTURED
    admission = api.begin_continuation_admission(
        ContinuationAttemptSpec("late-commit", 1, "request", ()),
        (BUSINESS,),
    )
    with pytest.raises(ProtocolError, match="idle CAPTURE"):
        api.configure_continuation_capture_points(())
    with pytest.raises(ProtocolError, match="continuation admission"):
        api._controller.worker_mutation_route()
    results: list[object] = []
    errors: list[BaseException] = []

    def resume() -> None:
        try:
            results.append(api.resume_capture(continuation_attempt_id="late-commit"))
        except BaseException as error:
            errors.append(error)

    caller = Thread(target=resume)
    caller.start()
    try:
        assert rdbg.waiting.wait(5)
        with pytest.raises(ProtocolError, match="confirmed result"):
            admission.commit()
        rdbg.release.set()
        caller.join(timeout=5)
        assert not caller.is_alive()
        assert errors == []
        assert results[0].kind is RuntimeReplyKind.CAPTURED
        admission.commit()
    finally:
        rdbg.release.set()
        caller.join(timeout=5)
        api.close()


def test_session_successor_ticket_correlates_second_stop_of_same_main(
    tmp_path: Path,
) -> None:
    platform = tmp_path / "bin"
    platform.mkdir()
    for name in ("1cv8.exe", "1cv8c.exe", "dbgs.exe"):
        (platform / name).touch()
    infobase = tmp_path / "base"
    infobase.mkdir()
    (infobase / "1Cv8.1CD").write_bytes(b"synthetic")
    config = RuntimeSessionConfig(
        RuntimeConfig(
            workspace=tmp_path / "workspace",
            platform_bin=platform,
            connection_string=f'File="{infobase}";',
        ),
        evidence_root=tmp_path / "evidence",
    )
    rdbg = ExportFailsOnce()
    composed = compose_fresh_post_bootstrap_execution(
        rdbg, KERNEL,
        runtime_generation=7,
        stopped_target=rdbg.target,
        capture_locations=(BUSINESS,),
        notebook_builder=lambda *_args, **_kwargs: None,
    )
    api = composed.execution.facade
    runtime = RuntimeSession(
        config,
        SimpleNamespace(ensure_running=lambda: None),
        SimpleNamespace(),
        rdbg,
        api,
        SimpleNamespace(append_jsonl=lambda *_args: None),
        heartbeat_interval_s=60.0,
    )
    point = SimpleNamespace(name="capture", line=50)
    runtime.verify_capture_points = lambda _points: (
        SimpleNamespace(location=BUSINESS),
    )
    first_intent = SimpleNamespace(
        points=(point,), capture_intent_id="intent-1", operation_id="request-1",
        capture_generation=1, source_revision=1, source_sha256="source-hash",
    )
    try:
        first_arming = runtime.arm_capture_intent(first_intent)
        first = runtime.execute_bsl("Результат = 1;")
        assert first.kind is RuntimeReplyKind.CAPTURED
        assert first.capture_ticket == first_arming.ticket_id

        runtime._capture_locations[("capture", 50)] = BUSINESS
        other = replace(BUSINESS, line=51)
        other_point = SimpleNamespace(name="other", line=51)
        runtime._capture_locations[("other", 51)] = other
        failed_attempt = SimpleNamespace(
            attempt_id="continue-failed", capture_generation=1,
            request_operation_id="request-failed", dirty_roots=("Amount",),
        )
        failed_intent = SimpleNamespace(
            points=(other_point,), capture_intent_id="intent-failed",
            operation_id="request-failed", capture_generation=2,
            source_revision=1, source_sha256="source-hash",
        )
        backend = OnecRuntimeBackend("synthetic", AgentRuntimeSession(runtime))
        failed_admission = backend.prepare_capture_successor(
            failed_intent, attempt=failed_attempt,
        )
        failed_outcome = backend.continue_capture(
            dirty_roots=("Amount",), attempt_id="continue-failed",
        )
        assert failed_outcome.execution.runtime_state == "partial_writeback_failure"
        failed_evidence = runtime.continuation_attempt_evidence("continue-failed")
        assert failed_evidence.root_statuses == (("Amount", "failed"),)
        assert failed_evidence.continue_state == "unattempted"
        failed_admission.rollback()
        assert runtime._active_capture_ticket.ticket_id == first_arming.ticket_id
        assert composed.breakpoint_workspace.confirmed_snapshot.captures == (BUSINESS,)
        assert runtime.continuation_attempt_evidence("continue-failed") == failed_evidence
        assert runtime.current_capture().status().phase is CapturePhase.PAUSED
        assert runtime.execute_bsl("Результат = 2;").kind is RuntimeReplyKind.CAPTURE_CELL

        attempt = SimpleNamespace(
            attempt_id="continue-1", capture_generation=1,
            request_operation_id="request-2", dirty_roots=(),
        )
        next_intent = SimpleNamespace(
            points=(point,), capture_intent_id="intent-2", operation_id="request-2",
            capture_generation=2, source_revision=1, source_sha256="source-hash",
        )
        admission = runtime.prepare_capture_successor(next_intent, attempt=attempt)
        assert admission.arming is not None
        assert admission.arming.expected_controller_operation_id == first.operation_id
        assert admission.arming.expected_stop_sequence == 2
        before = runtime.continuation_attempt_evidence("continue-1")
        assert before.root_statuses == ()
        assert before.continue_state == "unattempted"

        with pytest.raises(ProtocolError, match="continuation attempt"):
            runtime.resume_capture()

        second = runtime.resume_capture(continuation_attempt_id="continue-1")
        assert second.kind is RuntimeReplyKind.CAPTURED
        assert second.operation_id == first.operation_id
        assert second.stop_sequence == 2
        assert second.capture_ticket == admission.arming.ticket_id
        after = runtime.continuation_attempt_evidence("continue-1")
        assert after.root_statuses == ()
        assert after.continue_state == "acknowledged"
        admission.commit()

        completed = runtime.resume_capture()
        assert completed.kind is RuntimeReplyKind.MAIN_COMPLETED
        assert completed.operation_id == first.operation_id
    finally:
        runtime._heartbeat_stop.set()
        runtime._heartbeat_thread.join(timeout=2)
        api.close()
