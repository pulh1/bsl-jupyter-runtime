from collections import defaultdict, deque
from uuid import UUID
from xml.etree import ElementTree

import pytest

from onec_runtime.errors import ProtocolError
from onec_runtime.rdbg.models import DebugTarget, EvaluationResult, ModuleLocation, StopEvent, TargetId
from onec_runtime.rdbg.session import RdbgSession, SessionState
from onec_runtime.rdbg.xml_codec import BASE_NS, RDBG_NS, build_terminate_request


LOCATION = ModuleLocation("ExtensionModule", "", UUID(int=1), UUID(int=2), 7, "OnecInteractiveRuntime")
CLIENT = TargetId(UUID(int=10), "runtime_test", UUID(int=100))
SERVER = TargetId(UUID(int=11), "runtime_test", UUID(int=100))
FOREIGN = TargetId(UUID(int=12), "runtime_test", UUID(int=200))


class Transport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes]] = []
        self.responses: dict[str, deque[bytes]] = defaultdict(deque)
        self.responses["getDbgAllTargetStates"].append(b"<response/>")

    def request(self, command: str, payload: bytes = b"", **kwargs: object) -> bytes:
        self.calls.append((command, payload))
        if self.responses[command]:
            return self.responses[command].popleft()
        return b"<response/>" if command == "getDbgAllTargetStates" else b""


def target_xml(target: TargetId, kind: str) -> str:
    seance = f"<seanceId>{target.seance_id}</seanceId>" if target.seance_id else ""
    return f'<targetID xmlns="{BASE_NS}"><id>{target.id}</id><infoBaseAlias>{target.infobase_alias}</infoBaseAlias>{seance}<targetType>{kind}</targetType></targetID>'


def started(*targets: tuple[TargetId, str]) -> bytes:
    items = "".join(f"<result><cmdID>targetStarted</cmdID>{target_xml(target, kind)}</result>" for target, kind in targets)
    return f'<response xmlns="{RDBG_NS}">{items}</response>'.encode()


def create_session(transport: Transport) -> RdbgSession:
    return RdbgSession(transport, LOCATION, alias="runtime_test", server_target_type="Server", break_on_next=True)


def bind_owned_client(session: RdbgSession) -> None:
    session.evaluate = lambda expression: EvaluationResult(UUID(int=20), "Строка", '"onec-runtime:owned"', False)
    session.bind_server_session(launch_token="onec-runtime:owned")


def test_server_debugger_initializes_only_managed_client_auto_attach() -> None:
    transport = Transport()
    session = create_session(transport)
    session.initialize()
    settings = next(payload for command, payload in transport.calls if command == "setAutoAttachSettings")
    root = ElementTree.fromstring(settings)
    assert [node.text for node in root.iter() if node.tag.endswith("}targetType")] == ["ManagedClient"]
    assert [node.text for node in root.iter() if node.tag.endswith("}infoBaseAlias")] == ["runtime_test"]


def test_server_debugger_never_attaches_preexisting_clients() -> None:
    transport = Transport()
    transport.responses["getDbgAllTargetStates"] = deque([
        f'<response><item>{target_xml(FOREIGN, "ManagedClient")}<state>Working</state><stateNum>1</stateNum></item></response>'.encode()
    ])
    session = create_session(transport)
    session.initialize()
    transport.responses["pingDebugUIParams"].append(started((FOREIGN, "ManagedClient"), (CLIENT, "ManagedClient")))
    session._poll(0.1)
    assert set(session.attached_targets) == {CLIENT.id}
    attachments = [payload for command, payload in transport.calls if command == "attachDetachDbgTargets"]
    assert len(attachments) == 1
    assert str(CLIENT.id).encode() in attachments[0]


def test_server_attachment_is_bound_to_managed_client_session_before_any_server_attach() -> None:
    transport = Transport()
    session = create_session(transport)
    session.initialize()
    transport.responses["pingDebugUIParams"].append(started((CLIENT, "ManagedClient")))
    session._poll(0.1)
    session._admit_stop(StopEvent(CLIENT, LOCATION, "breakpoint"))
    bind_owned_client(session)
    foreign_alias = TargetId(UUID(int=14), "another_database", CLIENT.seance_id)
    transport.responses["pingDebugUIParams"].append(started((FOREIGN, "Server"), (foreign_alias, "Server"), (SERVER, "Server")))
    session._poll(0.1)
    assert set(session.attached_targets) == {CLIENT.id, SERVER.id}
    attachments = [payload for command, payload in transport.calls if command == "attachDetachDbgTargets"]
    assert len(attachments) == 2
    assert str(SERVER.id).encode() in attachments[-1]


def test_server_binding_requires_a_managed_client_with_session_identity() -> None:
    transport = Transport()
    session = create_session(transport)
    session.initialize()
    session.state = SessionState.READY
    session.target = DebugTarget(TargetId(CLIENT.id, "runtime_test"), "ManagedClient", "Stopped")
    with pytest.raises(ProtocolError, match="session"):
        session.bind_server_session(launch_token="onec-runtime:owned")


def test_foreign_first_client_is_rejected_before_binding_or_server_attachment() -> None:
    transport = Transport()
    session = create_session(transport)
    session.initialize()
    transport.responses["pingDebugUIParams"].append(started((FOREIGN, "ManagedClient")))
    session._poll(0.1)
    session._admit_stop(StopEvent(FOREIGN, LOCATION, "breakpoint"))
    expressions = []
    def foreign_launch(expression):
        expressions.append(expression)
        return EvaluationResult(UUID(int=20), "Строка", '"different-launch"', False)
    session.evaluate = foreign_launch
    with pytest.raises(ProtocolError, match="launch identity"):
        session.bind_server_session(launch_token="onec-runtime:owned")
    assert expressions == ["ПараметрЗапуска"]
    assert session._bound_client_target is None
    assert set(session.attached_targets) == {FOREIGN.id}


def test_binding_discovers_own_server_registered_before_the_managed_client() -> None:
    transport = Transport()
    session = create_session(transport)
    session.initialize()
    transport.responses["pingDebugUIParams"].append(started((SERVER, "Server"), (CLIENT, "ManagedClient")))
    session._poll(0.1)
    assert set(session.attached_targets) == {CLIENT.id}
    session._admit_stop(StopEvent(CLIENT, LOCATION, "breakpoint"))
    transport.responses["getDbgAllTargetStates"].append(
        f'<response><item>{target_xml(SERVER, "Server")}<state>Working</state><stateNum>1</stateNum></item></response>'.encode()
    )
    bind_owned_client(session)
    assert set(session.attached_targets) == {CLIENT.id, SERVER.id}


def test_ambiguous_new_clients_fail_before_any_attachment() -> None:
    transport = Transport()
    session = create_session(transport)
    session.initialize()
    transport.responses["pingDebugUIParams"].append(started((CLIENT, "ManagedClient"), (FOREIGN, "ManagedClient")))
    with pytest.raises(ProtocolError, match="managed client"):
        session._poll(0.1)
    assert not any(command == "attachDetachDbgTargets" for command, _ in transport.calls)


def test_initialize_failure_after_registration_can_still_detach_debug_ui() -> None:
    class FailingTransport(Transport):
        def request(self, command: str, payload: bytes = b"", **kwargs: object) -> bytes:
            if command == "initSettings":
                raise ProtocolError("settings failed")
            return super().request(command, payload, **kwargs)
    transport = FailingTransport()
    session = create_session(transport)
    with pytest.raises(ProtocolError):
        session.initialize()
    session.detach()
    assert any(command == "detachDebugUI" for command, _ in transport.calls)


def test_termination_uses_full_identity_and_only_the_authenticated_server_session() -> None:
    transport = Transport()
    session = create_session(transport)
    session.initialize()
    session.state = SessionState.READY
    session.target = DebugTarget(CLIENT, "ManagedClient", "Stopped")
    bind_owned_client(session)
    own_xml = target_xml(SERVER, "Server").replace('</targetID>', '<userName>Test User</userName><isServerInfoBase>undefined</isServerInfoBase></targetID>')
    foreign_xml = target_xml(FOREIGN, "Server")
    transport.responses["getDbgAllTargetStates"].append(
        f'<response><item>{own_xml}<state>Stopped</state><stateNum>1</stateNum></item><item>{foreign_xml}<state>Working</state><stateNum>1</stateNum></item></response>'.encode()
    )
    session.terminate_bound_server_session()
    payload = next(payload for command, payload in transport.calls if command == "terminateDbgTarget")
    assert str(SERVER.id).encode() in payload
    assert str(FOREIGN.id).encode() not in payload
    assert b'Test User' in payload
    assert str(CLIENT.seance_id).encode() in payload
    assert b'DebugTargetIdLight' not in payload


def test_termination_does_not_touch_an_unverified_client_session() -> None:
    transport = Transport()
    session = create_session(transport)
    session.initialize()
    session.state = SessionState.READY
    session.target = DebugTarget(FOREIGN, "ManagedClient", "Stopped")
    session.terminate_bound_server_session()
    assert not any(command == "terminateDbgTarget" for command, _ in transport.calls)


def states(*targets: tuple[TargetId, str]) -> bytes:
    items = "".join(f"<item>{target_xml(target, kind)}<state>Working</state><stateNum>1</stateNum></item>" for target, kind in targets)
    return f"<response>{items}</response>".encode()


def bound_session(transport: Transport) -> RdbgSession:
    session = create_session(transport)
    session.initialize()
    session.state = SessionState.READY
    session.target = DebugTarget(CLIENT, "ManagedClient", "Stopped")
    bind_owned_client(session)
    transport.calls.clear()
    return session


def test_termination_stops_server_before_freshly_identifying_only_owned_client() -> None:
    transport = Transport()
    session = bound_session(transport)
    sibling = TargetId(UUID(int=13), CLIENT.infobase_alias, CLIENT.seance_id)
    transport.responses["getDbgAllTargetStates"].extend([
        states((SERVER, "Server"), (CLIENT, "ManagedClient")),
        states((CLIENT, "ManagedClient"), (FOREIGN, "ManagedClient"), (sibling, "ManagedClient")),
    ])
    assert session.terminate_bound_server_session() is True
    assert [command for command, _ in transport.calls] == [
        "getDbgAllTargetStates", "terminateDbgTarget", "getDbgAllTargetStates", "terminateDbgTarget",
    ]
    server_request, client_request = [payload for command, payload in transport.calls if command == "terminateDbgTarget"]
    assert str(SERVER.id).encode() in server_request
    assert str(CLIENT.id).encode() not in server_request
    assert str(CLIENT.id).encode() in client_request
    assert str(FOREIGN.id).encode() not in client_request
    assert str(sibling.id).encode() not in client_request
    assert b"ManagedClient" in client_request
    assert b"DebugTargetIdLight" not in client_request


@pytest.mark.parametrize("changed,kind", [
    (TargetId(CLIENT.id, CLIENT.infobase_alias), "ManagedClient"),
    (TargetId(CLIENT.id, CLIENT.infobase_alias, FOREIGN.seance_id), "ManagedClient"),
    (CLIENT, "Server"),
])
def test_client_termination_rejects_changed_native_identity(changed: TargetId, kind: str) -> None:
    transport = Transport()
    session = bound_session(transport)
    transport.responses["getDbgAllTargetStates"].extend([states(), states((changed, kind))])
    with pytest.raises(ProtocolError, match="exact target identity"):
        session.terminate_bound_server_session()
    assert not any(command == "terminateDbgTarget" for command, _ in transport.calls)


def test_termination_retries_after_client_ack_failure_without_touching_foreign_targets() -> None:
    transport = Transport()
    session = bound_session(transport)
    transport.responses["getDbgAllTargetStates"].extend([
        states((SERVER, "Server")), states((CLIENT, "ManagedClient")),
        states((FOREIGN, "Server")), states((CLIENT, "ManagedClient")),
    ])
    transport.responses["terminateDbgTarget"].extend([b"", b"<response><result>failure</result></response>", b""])
    with pytest.raises(ProtocolError, match="acknowledgement"):
        session.terminate_bound_server_session()
    assert session.terminate_bound_server_session() is True
    payloads = [payload for command, payload in transport.calls if command == "terminateDbgTarget"]
    assert len(payloads) == 3
    assert all(str(FOREIGN.id).encode() not in payload for payload in payloads)


def test_server_termination_failure_keeps_client_alive_for_retry() -> None:
    transport = Transport()
    session = bound_session(transport)
    transport.responses["getDbgAllTargetStates"].append(states((SERVER, "Server"), (CLIENT, "ManagedClient")))
    transport.responses["terminateDbgTarget"].append(b"<response><result>failure</result></response>")
    with pytest.raises(ProtocolError, match="acknowledgement"):
        session.terminate_bound_server_session()
    assert [command for command, _ in transport.calls] == ["getDbgAllTargetStates", "terminateDbgTarget"]


def test_absent_bound_client_requires_no_native_termination() -> None:
    transport = Transport()
    session = bound_session(transport)
    assert session.terminate_bound_server_session() is False
    assert not any(command == "terminateDbgTarget" for command, _ in transport.calls)


@pytest.mark.parametrize("payload", [
    b"<response/>",
    states((CLIENT, "ManagedClient"), (CLIENT, "ManagedClient")),
    states((CLIENT, "Server")),
    states((TargetId(CLIENT.id, "other_alias", CLIENT.seance_id), "ManagedClient")),
])
def test_client_termination_builder_requires_one_exact_full_identity(payload: bytes) -> None:
    with pytest.raises(ProtocolError, match="exact target identity"):
        build_terminate_request("runtime_test", UUID(int=1), (CLIENT,), payload, target_type="ManagedClient")


def test_client_termination_builder_preserves_native_identity_fields() -> None:
    payload = states((CLIENT, "ManagedClient")).replace(
        b"</targetID>", b"<userName>Owned User</userName><isServerInfoBase>undefined</isServerInfoBase></targetID>",
    )
    request = build_terminate_request("runtime_test", UUID(int=1), (CLIENT,), payload, target_type="ManagedClient")
    assert b"Owned User" in request
    assert b"undefined" in request
    assert str(CLIENT.seance_id).encode() in request
    with pytest.raises(ValueError, match="Unsupported"):
        build_terminate_request("runtime_test", UUID(int=1), (CLIENT,), payload, target_type="ServerEmulation")
