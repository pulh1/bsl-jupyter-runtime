"""Successor CAPTURE intent stays correlated to the same MAIN operation.

The post-bootstrap facade is injected manually; RuntimeSession.start() is not
part of this contract until the full public cutover.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from onec_runtime.config import RuntimeConfig
from onec_runtime.errors import ProtocolError
from onec_runtime.execution.post_bootstrap import compose_fresh_post_bootstrap_execution
from onec_runtime.runtime_api import RuntimeReplyKind
from onec_runtime.session import RuntimeSession, RuntimeSessionConfig

from test_execution_controller_routes import BUSINESS, KERNEL, CompleteSession


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
    rdbg = CompleteSession(capture_count=2)
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
        admission.commit()

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

        completed = runtime.resume_capture()
        assert completed.kind is RuntimeReplyKind.MAIN_COMPLETED
        assert completed.operation_id == first.operation_id
    finally:
        runtime._heartbeat_stop.set()
        runtime._heartbeat_thread.join(timeout=2)
        api.close()
