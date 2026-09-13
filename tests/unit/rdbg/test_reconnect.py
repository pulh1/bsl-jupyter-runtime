from collections import defaultdict, deque
from uuid import UUID

import pytest

from onec_runtime.errors import RecoveryIdentityMismatch
from onec_runtime.rdbg.models import DebugTarget, ModuleLocation, TargetId
from onec_runtime.rdbg.reconnect import reconnect_session
from onec_runtime.rdbg.session import RdbgSession, SessionState
from onec_runtime.rdbg.xml_codec import BASE_NS, RDBG_NS
from onec_runtime.recovery import RecoveryCheckpoint, RecoveryPhase


TARGET_UUID = UUID("22222222-2222-2222-2222-222222222222")
OTHER_TARGET_UUID = UUID("33333333-3333-3333-3333-333333333333")
OBJECT_ID = UUID("cb953767-f436-4a5b-9e09-13a67d6e0201")
PROPERTY_ID = UUID("d5963243-262e-4398-b4d7-fb16d06484f6")
LOCATION = ModuleLocation(
    "ExtensionModule",
    "",
    OBJECT_ID,
    PROPERTY_ID,
    69,
    "OnecInteractiveRuntime",
)
TARGET_ID = TargetId(TARGET_UUID, "DefAlias")
TARGET = DebugTarget(TARGET_ID, "ServerEmulation", "stopped", 7)


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.responses: dict[str, deque[bytes]] = defaultdict(deque)

    def request(self, command: str, payload: bytes = b"", **_: object) -> bytes:
        self.calls.append(command)
        return self.responses[command].popleft() if self.responses[command] else b""


def previous_session() -> RdbgSession:
    previous = RdbgSession(FakeTransport(), LOCATION)  # type: ignore[arg-type]
    previous.target = TARGET
    previous.attached_targets[TARGET_UUID] = TARGET
    previous.state = SessionState.READY
    return previous


def checkpoint() -> RecoveryCheckpoint:
    return RecoveryCheckpoint(
        sequence=1,
        runtime_generation=1,
        operation_id=2,
        phase=RecoveryPhase.CAPTURED,
        target=TARGET,
        frame_location=LOCATION,
        stop_sequence=1,
        breakpoint_workspace=(LOCATION,),
        write_journal=(),
        continue_sent=False,
    )


def target_payload(
    *,
    target_id: UUID = TARGET_UUID,
    state: str = "stopped",
) -> bytes:
    return f"""<response xmlns="{RDBG_NS}"><result>success</result><item>
      <targetID xmlns="{BASE_NS}"><id>{target_id}</id>
      <infoBaseAlias>DefAlias</infoBaseAlias>
      <targetType>ServerEmulation</targetType></targetID>
      <stateNum>7</stateNum><state>{state}</state></item></response>""".encode()


def stack_payload() -> bytes:
    return f"""<response xmlns="{RDBG_NS}"><result>success</result><callStack>
      <moduleID xmlns="{BASE_NS}"><type>{LOCATION.module_type}</type>
      <extensionName>{LOCATION.extension_name}</extensionName>
      <objectID>{OBJECT_ID}</objectID><propertyID>{PROPERTY_ID}</propertyID></moduleID>
      <lineNo>{LOCATION.line}</lineNo></callStack></response>""".encode()


def test_reconnect_paused_reuses_ui_id_and_returns_frame_evidence() -> None:
    transport = FakeTransport()
    transport.responses["getDbgAllTargetStates"].append(target_payload())
    transport.responses["getCallStack"].append(stack_payload())
    previous = previous_session()

    reconnected = reconnect_session(
        transport,  # type: ignore[arg-type]
        previous,
        checkpoint(),
        observe_executing=False,
        timeout_s=1,
    )

    assert reconnected.session.ui_id == previous.ui_id
    assert reconnected.evidence is not None
    assert reconnected.evidence.stop.location == LOCATION
    assert transport.calls[:4] == [
        "attachDebugUI",
        "initSettings",
        "setAutoAttachSettings",
        "getDbgAllTargetStates",
    ]
    assert "step" not in transport.calls


def test_reconnect_stop_on_next_line_requests_exact_frame_evidence() -> None:
    transport = FakeTransport()
    transport.responses["getDbgAllTargetStates"].append(
        target_payload(state="StopOnNextLine")
    )
    transport.responses["getCallStack"].append(stack_payload())

    reconnected = reconnect_session(
        transport,  # type: ignore[arg-type]
        previous_session(),
        checkpoint(),
        observe_executing=False,
        timeout_s=1,
    )

    assert reconnected.evidence is not None
    assert reconnected.evidence.target.state == "StopOnNextLine"
    assert reconnected.evidence.stop.location == LOCATION
    assert "getCallStack" in transport.calls


def test_reconnect_rejects_changed_target() -> None:
    transport = FakeTransport()
    transport.responses["getDbgAllTargetStates"].append(
        target_payload(target_id=OTHER_TARGET_UUID)
    )

    with pytest.raises(RecoveryIdentityMismatch, match="found 0"):
        reconnect_session(
            transport,  # type: ignore[arg-type]
            previous_session(),
            checkpoint(),
            observe_executing=False,
            timeout_s=1,
        )


def test_reconnect_executing_observes_without_continue() -> None:
    transport = FakeTransport()
    transport.responses["getDbgAllTargetStates"].append(
        target_payload(state="running")
    )

    reconnected = reconnect_session(
        transport,  # type: ignore[arg-type]
        previous_session(),
        checkpoint(),
        observe_executing=True,
        timeout_s=1,
    )

    assert reconnected.evidence is None
    assert reconnected.session.state is SessionState.EXECUTING
    assert "getCallStack" not in transport.calls
    assert "step" not in transport.calls
