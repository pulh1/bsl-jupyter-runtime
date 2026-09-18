"""Session CAPTURE fence notifications and extension descriptor routing."""

from __future__ import annotations

from contextlib import nullcontext
from threading import RLock
from types import SimpleNamespace

from onec_runtime_mcp.agent.capture_contracts import CaptureFence
from onec_runtime_mcp.agent.runtime_session import AgentRuntimeSession
from onec_runtime.runtime_models import (
    OperationState, RuntimeReply, RuntimeReplyKind,
)
from onec_runtime.session import RuntimeSession


def fence() -> CaptureFence:
    return CaptureFence("intent", "operation", 3, "a" * 64, 1, 1)

def test_successful_session_resume_notifies_the_exact_fence_but_failure_keeps_it() -> None:
    session = object.__new__(RuntimeSession)
    session._operation_lock = RLock()
    session._capture_resume_listeners = []
    active = SimpleNamespace(
        capture_intent_id="intent", operation_id="operation", capture_generation=1,
        source_revision=3, source_sha256="a" * 64, stop_sequence=1,
        ticket_id="private-runtime-ticket",
    )
    session._active_capture_ticket = active
    seen: list[CaptureFence] = []
    agent_session = AgentRuntimeSession(session)
    agent_session.add_capture_resume_listener(seen.append)
    resumed = RuntimeReply(RuntimeReplyKind.MAIN_COMPLETED, 1, OperationState.COMPLETED)
    session.runtime_api = SimpleNamespace(
        resume_capture=lambda **_kwargs: resumed,
        execution_caller_handoff=lambda _release: nullcontext(),
    )

    assert agent_session.resume_capture() is resumed
    assert seen == [fence()]
    assert session._active_capture_ticket is None

    session._active_capture_ticket = active
    session.runtime_api = SimpleNamespace(
        resume_capture=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("pre-send")),
        status=lambda: SimpleNamespace(state=OperationState.CAPTURED),
        execution_caller_handoff=lambda _release: nullcontext(),
    )
    try:
        session.resume_capture()
    except RuntimeError:
        pass
    else:
        raise AssertionError("failed resume must propagate")
    assert session._active_capture_ticket is active

    session.runtime_api = SimpleNamespace(
        resume_capture=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("ambiguous")),
        status=lambda: SimpleNamespace(state=OperationState.RECOVERING),
        execution_caller_handoff=lambda _release: nullcontext(),
    )
    try:
        session.resume_capture()
    except RuntimeError:
        pass
    else:
        raise AssertionError("ambiguous resume must propagate")
    assert session._active_capture_ticket is None
    assert seen == [fence(), fence()]


def test_capture_extension_resolves_descriptor_through_its_data_result() -> None:
    source = (
        __import__("pathlib").Path(__file__).parents[2]
        / "onec" / "OnecInteractiveRuntime" / "CommonModules" / "RuntimeKernelServer" / "Ext" / "Module.bsl"
    ).read_text(encoding="utf-8")

    assert "ОписательТаблицы.ПолучитьДанные()" in source
    assert "RuntimeTableTransferServer.ПодготовитьТабличноеЗначение(ОписательТаблицы)" not in source
