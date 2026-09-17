from collections import defaultdict, deque
from threading import Event, Thread
from uuid import UUID
from xml.etree import ElementTree

import pytest

import onec_runtime.rdbg.session as session_module
from onec_runtime.errors import (
    CommandTimeout,
    EvaluationDispatchUnknown,
    ProtocolError,
    TargetLost,
    UnexpectedStop,
)
from onec_runtime.performance_profile import PhaseRecorder
from onec_runtime.rdbg.models import (
    DebugTarget,
    EvaluationResult,
    ModuleLocation,
    PendingEvaluation,
    StopEvent,
    TargetId,
)
from onec_runtime.rdbg.session import RdbgSession, SessionState
from onec_runtime.rdbg.xml_codec import BASE_NS, CALC_NS, RDBG_NS


TARGET_ID = UUID("22222222-2222-2222-2222-222222222222")
OBJECT_ID = UUID("883af47f-bd19-491f-8dfa-3bd4ff6e0cfa")
PROPERTY_ID = UUID("d22e852a-cf8a-4f77-8ccb-3548e7792bea")
LOCATION = ModuleLocation(
    "ExtensionModule", "", OBJECT_ID, PROPERTY_ID, 16, "OnecInteractiveRuntime"
)
CAPTURE_A = ModuleLocation(
    "ExtensionModule", "", OBJECT_ID, PROPERTY_ID, 73, "OnecInteractiveRuntime"
)
UNREGISTERED = ModuleLocation(
    "ExtensionModule", "", OBJECT_ID, PROPERTY_ID, 99, "OnecInteractiveRuntime"
)


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.payloads: list[tuple[str, bytes]] = []
        self.responses: dict[str, deque[bytes]] = defaultdict(deque)

    def request(self, command: str, payload: bytes = b"", **_: object) -> bytes:
        self.calls.append(command)
        self.payloads.append((command, payload))
        return self.responses[command].popleft() if self.responses[command] else b""

    def test_server(self) -> float:
        return 1.0


def test_attaches_started_managed_client_and_accepts_extension_stop() -> None:
    target_started = f"""<response xmlns="{RDBG_NS}"><result><cmdID>targetStarted</cmdID>
      <targetID xmlns="{BASE_NS}"><id>{TARGET_ID}</id><infoBaseAlias>DefAlias</infoBaseAlias>
      <targetType>ManagedClient</targetType></targetID></result></response>""".encode()
    stopped = f"""<response xmlns="{RDBG_NS}"><result><cmdID>callStackFormed</cmdID>
      <targetID xmlns="{BASE_NS}"><id>{TARGET_ID}</id><infoBaseAlias>DefAlias</infoBaseAlias></targetID>
      <callStack><moduleID><type>ExtensionModule</type><extensionName>OnecInteractiveRuntime</extensionName>
      <objectID>{OBJECT_ID}</objectID><propertyID>{PROPERTY_ID}</propertyID></moduleID>
      <lineNo>16</lineNo></callStack></result></response>""".encode()
    transport = FakeTransport()
    transport.responses["pingDebugUIParams"].extend((target_started, stopped))
    session = RdbgSession(transport, LOCATION)  # type: ignore[arg-type]

    session.initialize()
    session.set_service_breakpoint()
    event = session.wait_for_service_stop(timeout_s=1)

    assert event.location == LOCATION
    assert session.state is SessionState.READY
    assert session.target is not None
    assert session.target.target_type == "ManagedClient"
    assert "attachDetachDbgTargets" in transport.calls
    assert transport.calls.count("setBreakpoints") == 2
    assert "getCallStack" not in transport.calls


def ready_session(transport: FakeTransport) -> RdbgSession:
    session = RdbgSession(transport, LOCATION)  # type: ignore[arg-type]
    target_id = TargetId(TARGET_ID, "DefAlias")
    session.target = DebugTarget(target_id, "ServerEmulation", "stopped")
    session.attached_targets[TARGET_ID] = session.target
    session.state = SessionState.READY
    return session


def test_set_breakpoints_accepts_empty_http_success_acknowledgement() -> None:
    transport = FakeTransport()
    transport.responses["setBreakpoints"].append(b"")
    session = ready_session(transport)

    session.set_breakpoints((LOCATION, CAPTURE_A))

    assert session._breakpoint_installed is True
    assert session._breakpoint_locations == (LOCATION, CAPTURE_A)


def test_set_breakpoints_accepts_explicit_success_acknowledgement() -> None:
    transport = FakeTransport()
    transport.responses["setBreakpoints"].append(
        f'<response xmlns="{RDBG_NS}"><result>success</result></response>'.encode()
    )
    session = ready_session(transport)

    session.set_breakpoints((LOCATION, CAPTURE_A))

    assert session._breakpoint_locations == (LOCATION, CAPTURE_A)


def test_invalidate_fences_breakpoint_request_after_validation_and_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = FakeTransport()
    session = ready_session(transport)
    build_entered = Event()
    release_build = Event()
    errors = []
    original_build = session_module.build_breakpoints_request

    def blocked_build(*args, **kwargs):  # type: ignore[no-untyped-def]
        build_entered.set()
        assert release_build.wait(2)
        return original_build(*args, **kwargs)

    monkeypatch.setattr(session_module, "build_breakpoints_request", blocked_build)

    def set_breakpoints() -> None:
        try:
            session.set_breakpoints((LOCATION, CAPTURE_A))
        except BaseException as error:
            errors.append(error)

    worker = Thread(target=set_breakpoints)
    worker.start()
    assert build_entered.wait(1), "breakpoint request did not pass validation"
    session.invalidate()
    assert session.state is SessionState.FAILED
    assert transport.calls == []
    release_build.set()
    worker.join(1)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], ProtocolError)
    assert transport.calls == []
    assert session._breakpoint_installed is False


def test_invalidate_after_eval_admission_allows_only_the_admitted_request() -> None:
    transport = FakeTransport()
    session = ready_session(transport)
    dispatch_entered = Event()
    release_dispatch = Event()
    errors = []

    def dispatch_marker() -> None:
        dispatch_entered.set()
        assert release_dispatch.wait(2)

    def evaluate() -> None:
        try:
            session.start_evaluation(
                "Результат = 1;",
                timeout_s=0.05,
                on_transport_dispatch=dispatch_marker,
            )
        except BaseException as error:
            errors.append(error)

    worker = Thread(target=evaluate)
    worker.start()
    assert dispatch_entered.wait(1), "evalExpr did not reach its dispatch marker"
    session.invalidate()
    assert transport.calls == []
    release_dispatch.set()
    worker.join(1)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], ProtocolError)
    assert transport.calls == ["evalExpr"]
    assert session._pending_evaluation_states == {}


@pytest.mark.parametrize(
    "response",
    (
        b"<broken",
        f'<response xmlns="{RDBG_NS}"><result>false</result></response>'.encode(),
        f'<response xmlns="{RDBG_NS}"></response>'.encode(),
    ),
)
def test_set_breakpoints_rejects_unconfirmed_acknowledgement(response: bytes) -> None:
    transport = FakeTransport()
    transport.responses["setBreakpoints"].append(response)
    session = ready_session(transport)

    with pytest.raises(ProtocolError, match="setBreakpoints acknowledgement"):
        session.set_breakpoints((LOCATION, CAPTURE_A))

    assert session._breakpoint_installed is False
    assert session._breakpoint_locations == ()


def target_states_payload() -> bytes:
    return f'''<response xmlns="{RDBG_NS}"><result>success</result><item>
        <targetIDStr>target</targetIDStr><targetID xmlns="{BASE_NS}">
        <id>{TARGET_ID}</id><infoBaseAlias>DefAlias</infoBaseAlias>
        <targetType>ServerEmulation</targetType></targetID>
        <stateNum>1</stateNum><state>stopped</state>
        </item></response>'''.encode()


def test_heartbeat_renews_the_registered_debug_ui_lease() -> None:
    transport = FakeTransport()
    transport.responses["pingDebugUIParams"].append(b"")
    transport.responses["getDbgAllTargetStates"].append(target_states_payload())
    session = ready_session(transport)

    result = session.heartbeat()

    assert result == {"rtt_ms": 1.0, "target_state": "stopped"}
    assert transport.calls == ["pingDebugUIParams", "getDbgAllTargetStates"]


def test_heartbeat_marks_each_transport_entry_before_request() -> None:
    events: list[str] = []

    class OrderedTransport(FakeTransport):
        def test_server(self) -> float:
            events.append("test_server")
            return super().test_server()

        def request(self, command: str, payload: bytes = b"", **options: object) -> bytes:
            events.append(command)
            return super().request(command, payload, **options)

    transport = OrderedTransport()
    transport.responses["pingDebugUIParams"].append(b"")
    transport.responses["getDbgAllTargetStates"].append(target_states_payload())
    session = ready_session(transport)

    result = session.heartbeat(on_transport_dispatch=lambda: events.append("dispatch"))

    assert result == {"rtt_ms": 1.0, "target_state": "stopped"}
    assert events == [
        "dispatch", "test_server", "dispatch", "pingDebugUIParams",
        "dispatch", "getDbgAllTargetStates",
    ]


def test_heartbeat_callback_rejection_blocks_first_and_later_transport_entries() -> None:
    class CountingTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.server_checks = 0

        def test_server(self) -> float:
            self.server_checks += 1
            return super().test_server()

    first = CountingTransport()
    first_session = ready_session(first)

    def reject_first() -> None:
        raise ValueError("before first entry")

    with pytest.raises(ValueError, match="before first entry"):
        first_session.heartbeat(on_transport_dispatch=reject_first)
    assert first.server_checks == 0
    assert first.calls == []

    later = CountingTransport()
    later_session = ready_session(later)
    attempts = 0

    def reject_second() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise ValueError("before second entry")

    with pytest.raises(ValueError, match="before second entry"):
        later_session.heartbeat(on_transport_dispatch=reject_second)
    assert later.server_checks == 1
    assert later.calls == []


def test_heartbeat_reports_lost_selected_target_after_renewing_lease() -> None:
    """Break: treating a missing selected target as a healthy heartbeat."""
    transport = FakeTransport()
    transport.responses["getDbgAllTargetStates"].append(
        f'<response xmlns="{RDBG_NS}"><result>success</result></response>'.encode()
    )
    session = ready_session(transport)

    with pytest.raises(TargetLost, match="Selected target disappeared"):
        session.heartbeat()

    assert transport.calls == ["pingDebugUIParams", "getDbgAllTargetStates"]


def stopped_payload(location: ModuleLocation) -> bytes:
    return f"""<response xmlns="{RDBG_NS}"><result><cmdID>callStackFormed</cmdID>
      <targetID xmlns="{BASE_NS}"><id>{TARGET_ID}</id><infoBaseAlias>DefAlias</infoBaseAlias></targetID>
      <callStack><moduleID><type>{location.module_type}</type>
      <extensionName>{location.extension_name}</extensionName>
      <objectID>{location.object_id}</objectID><propertyID>{location.property_id}</propertyID>
      </moduleID><lineNo>{location.line}</lineNo></callStack></result></response>""".encode()


def started_payload(target_id: UUID) -> bytes:
    return f"""<response xmlns="{RDBG_NS}"><result><cmdID>targetStarted</cmdID>
      <targetID xmlns="{BASE_NS}"><id>{target_id}</id><infoBaseAlias>DefAlias</infoBaseAlias>
      <targetType>ManagedClient</targetType></targetID></result></response>""".encode()


@pytest.mark.parametrize(
    ("blocked_entry", "expected_calls"),
    [
        (3, ["pingDebugUIParams"]),
        (4, ["pingDebugUIParams", "clearBreakOnNextStatement"]),
        (5, ["pingDebugUIParams", "clearBreakOnNextStatement", "attachDetachDbgTargets"]),
    ],
)
def test_heartbeat_fences_each_autoattach_effect_after_ping(
    blocked_entry: int, expected_calls: list[str],
) -> None:
    transport = FakeTransport()
    transport.responses["pingDebugUIParams"].append(
        started_payload(UUID("33333333-3333-3333-3333-333333333333"))
    )
    transport.responses["getDbgAllTargetStates"].append(target_states_payload())
    session = ready_session(transport)
    session._breakpoint_installed = True
    session._breakpoint_locations = (LOCATION,)
    entries = 0

    def reject_at_entry() -> None:
        nonlocal entries
        entries += 1
        if entries == blocked_entry:
            raise ValueError("Stop fenced autoattach")

    with pytest.raises(ValueError, match="Stop fenced autoattach"):
        session.heartbeat(on_transport_dispatch=reject_at_entry)

    assert entries == blocked_entry
    assert transport.calls == expected_calls


def test_eval_wait_fences_autoattach_discovered_during_poll() -> None:
    transport = FakeTransport()
    transport.responses["pingDebugUIParams"].append(
        started_payload(UUID("33333333-3333-3333-3333-333333333333"))
    )
    session = ready_session(transport)
    pending = session.start_evaluation("1")
    entries = 0

    def reject_autoattach() -> None:
        nonlocal entries
        entries += 1
        if entries == 2:
            raise ValueError("Stop fenced eval poll autoattach")

    with pytest.raises(ValueError, match="Stop fenced eval poll autoattach"):
        session.wait_evaluation_event(
            pending, timeout_s=1, on_transport_dispatch=reject_autoattach,
        )

    assert transport.calls == ["evalExpr", "pingDebugUIParams"]
    assert id(pending) in session._pending_evaluation_states


def test_eval_result_survives_fenced_autoattach_in_same_ping() -> None:
    transport = FakeTransport()
    session = ready_session(transport)
    pending = session.start_evaluation("1")
    discovered_id = UUID("33333333-3333-3333-3333-333333333333")
    transport.responses["pingDebugUIParams"].append(f'''<response xmlns="{RDBG_NS}">
      <result><cmdID>targetStarted</cmdID>
        <targetID xmlns="{BASE_NS}"><id>{discovered_id}</id>
          <infoBaseAlias>DefAlias</infoBaseAlias><targetType>ManagedClient</targetType>
        </targetID></result>
      <result><cmdID>exprEvaluated</cmdID><evalExprResBaseData>
        <expressionResultID xmlns="{CALC_NS}">{pending.result_id}</expressionResultID>
        <resultValueInfo xmlns="{CALC_NS}"><typeName>Число</typeName><pres>MQ==</pres></resultValueInfo>
        <errorOccurred xmlns="{CALC_NS}">false</errorOccurred>
      </evalExprResBaseData></result></response>'''.encode())
    entries = 0

    def stop_before_autoattach() -> None:
        nonlocal entries
        entries += 1
        if entries == 2:
            raise ValueError("Stop fenced autoattach")

    with pytest.raises(ValueError, match="Stop fenced autoattach"):
        session.wait_evaluation_event(
            pending, timeout_s=1, on_transport_dispatch=stop_before_autoattach,
        )

    assert transport.calls == ["evalExpr", "pingDebugUIParams"]
    assert id(pending) in session._pending_evaluation_states
    result = session.wait_evaluation_event(pending, timeout_s=0.01)
    assert (result.result_id, result.presentation) == (pending.result_id, "1")
    assert transport.calls == ["evalExpr", "pingDebugUIParams"]


def test_main_stop_wait_fences_autoattach_discovered_during_poll() -> None:
    transport = FakeTransport()
    transport.responses["pingDebugUIParams"].append(
        started_payload(UUID("33333333-3333-3333-3333-333333333333"))
    )
    session = ready_session(transport)
    session.state = SessionState.EXECUTING
    entries = 0

    def reject_autoattach() -> None:
        nonlocal entries
        entries += 1
        if entries == 2:
            raise ValueError("Stop fenced MAIN poll autoattach")

    with pytest.raises(ValueError, match="Stop fenced MAIN poll autoattach"):
        session.wait_for_any_stop(timeout_s=1, on_transport_dispatch=reject_autoattach)

    assert transport.calls == ["pingDebugUIParams"]


def test_local_variables_wait_fences_autoattach_discovered_during_poll() -> None:
    transport = FakeTransport()
    transport.responses["evalLocalVariables"].append(b"")
    transport.responses["pingDebugUIParams"].append(
        started_payload(UUID("33333333-3333-3333-3333-333333333333"))
    )
    session = ready_session(transport)
    entries = 0

    def reject_autoattach() -> None:
        nonlocal entries
        entries += 1
        if entries == 3:
            raise ValueError("Stop fenced locals poll autoattach")

    with pytest.raises(ValueError, match="Stop fenced locals poll autoattach"):
        session.local_variables(timeout_s=1, on_transport_dispatch=reject_autoattach)

    assert transport.calls == ["evalLocalVariables", "pingDebugUIParams"]


def test_heartbeat_preserves_stop_evaluation_and_locals_for_real_consumers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break: dropping/duplicating heartbeat events or losing deferred result caches."""
    result_id = UUID("aaaaaaaa-9999-9999-9999-999999999999")
    locals_id = UUID("bbbbbbbb-9999-9999-9999-999999999999")
    next_id = UUID("cccccccc-9999-9999-9999-999999999999")
    transport = FakeTransport()
    session = ready_session(transport)
    ids = iter((result_id, locals_id, next_id))
    monkeypatch.setattr("onec_runtime.rdbg.session.uuid4", lambda: next(ids))
    pending = session.start_evaluation("1")
    payload = ElementTree.fromstring(stopped_payload(CAPTURE_A))
    data = ElementTree.fromstring(f'''<response xmlns="{RDBG_NS}">
      <result><cmdID>exprEvaluated</cmdID><evalExprResBaseData>
        <expressionResultID xmlns="{CALC_NS}">{result_id}</expressionResultID>
        <resultValueInfo xmlns="{CALC_NS}"><typeName>Число</typeName><pres>MQ==</pres></resultValueInfo>
        <errorOccurred xmlns="{CALC_NS}">false</errorOccurred>
      </evalExprResBaseData></result>
      <result><cmdID>exprEvaluated</cmdID><evalExprResBaseData>
        <expressionResultID xmlns="{CALC_NS}">{locals_id}</expressionResultID>
        <calculationResult xmlns="{CALC_NS}"><valueOfContextPropInfo>
          <propInfo><propName>Результат</propName></propInfo>
          <valueInfo><typeName>Массив</typeName><pres>W10=</pres></valueInfo>
        </valueOfContextPropInfo></calculationResult>
      </evalExprResBaseData></result></response>''')
    payload.extend(data)
    transport.responses["pingDebugUIParams"].append(ElementTree.tostring(payload))
    transport.responses["getDbgAllTargetStates"].append(target_states_payload())

    session.heartbeat()

    stop = session.wait_evaluation_event(pending, timeout_s=0.05)
    assert isinstance(stop, StopEvent) and stop.location == CAPTURE_A
    session.continue_evaluation(pending, stop)
    result = session.wait_evaluation_event(pending, timeout_s=0.05)
    assert isinstance(result, EvaluationResult)
    assert (result.result_id, result.type_name, result.presentation) == (result_id, "Число", "1")
    local = session.local_variables(timeout_s=0.05)
    assert local.result_id == locals_id
    assert [(item.name, item.type_name, item.presentation) for item in local.variables] == [
        ("Результат", "Массив", "[]")
    ]
    assert transport.calls.count("pingDebugUIParams") == 1
    assert transport.calls.count("step") == 1
    # A duplicate stop/result from the heartbeat would precede this next result.
    transport.responses["evalExpr"].append(f'''<response xmlns="{RDBG_NS}"><result>
      <expressionResultID xmlns="{CALC_NS}">{next_id}</expressionResultID>
      <resultValueInfo xmlns="{CALC_NS}"><typeName>Число</typeName><pres>Mg==</pres></resultValueInfo>
      <errorOccurred xmlns="{CALC_NS}">false</errorOccurred></result></response>'''.encode())
    assert session.evaluate("2", timeout_s=0.05).presentation == "2"
    assert transport.calls.count("pingDebugUIParams") == 1


def test_wait_for_stop_accepts_registered_capture_location() -> None:
    transport = FakeTransport()
    transport.responses["pingDebugUIParams"].append(stopped_payload(CAPTURE_A))
    session = ready_session(transport)
    session.state = SessionState.EXECUTING

    stop = session.wait_for_stop((LOCATION, CAPTURE_A), timeout_s=1)

    assert stop.location == CAPTURE_A
    assert session.state is SessionState.READY


def test_wait_for_stop_rejects_unregistered_location() -> None:
    transport = FakeTransport()
    transport.responses["pingDebugUIParams"].append(stopped_payload(UNREGISTERED))
    session = ready_session(transport)
    session.state = SessionState.EXECUTING

    with pytest.raises(UnexpectedStop, match="does not match any allowed"):
        session.wait_for_stop((LOCATION, CAPTURE_A), timeout_s=1)


def test_wait_for_any_stop_returns_unregistered_location_paused() -> None:
    transport = FakeTransport()
    transport.responses["pingDebugUIParams"].append(stopped_payload(UNREGISTERED))
    session = ready_session(transport)
    session.state = SessionState.EXECUTING

    event = session.wait_for_any_stop(timeout_s=1)

    assert event.location == UNREGISTERED
    assert session.state is SessionState.READY
    assert transport.calls.count("step") == 0


def test_wait_for_any_stop_interval_expiry_preserves_running_target() -> None:
    transport = FakeTransport()
    session = ready_session(transport)
    original_target = session.target
    session.state = SessionState.EXECUTING

    with pytest.raises(CommandTimeout, match="waiting for a runtime stop"):
        session.wait_for_any_stop(timeout_s=0.001)

    assert session.state is SessionState.EXECUTING
    assert session.target is original_target

    transport.responses["pingDebugUIParams"].append(stopped_payload(CAPTURE_A))
    stop = session.wait_for_any_stop(timeout_s=1)

    assert stop.location == CAPTURE_A
    assert session.state is SessionState.READY
    assert session.target is original_target


def test_wait_for_any_stop_aborts_when_owned_client_exits_between_polls() -> None:
    transport = FakeTransport()
    session = ready_session(transport)
    session.state = SessionState.EXECUTING
    checks = 0

    def check_client() -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise TargetLost("owned client exited")

    with pytest.raises(TargetLost, match="owned client exited"):
        session.wait_for_any_stop(timeout_s=30, on_poll=check_client)

    assert transport.calls.count("pingDebugUIParams") == 1


def test_read_current_stack_returns_frame_zero_without_continue() -> None:
    transport = FakeTransport()
    transport.responses["getCallStack"].append(
        f"""<response xmlns="{RDBG_NS}"><result>success</result><callStack>
        <moduleID xmlns="{BASE_NS}"><type>{CAPTURE_A.module_type}</type>
        <extensionName>{CAPTURE_A.extension_name}</extensionName>
        <objectID>{CAPTURE_A.object_id}</objectID>
        <propertyID>{CAPTURE_A.property_id}</propertyID></moduleID>
        <lineNo>{CAPTURE_A.line}</lineNo></callStack></response>""".encode()
    )
    session = ready_session(transport)

    event = session.read_current_stack(timeout_s=1)

    assert event.location == CAPTURE_A
    assert event.reason == "recoveredCallStack"
    assert event.stack == (CAPTURE_A,)
    assert [frame.level for frame in event.stack_frames] == [0]
    assert transport.calls.count("step") == 0


def test_invalidated_session_rejects_all_runtime_calls_without_transport() -> None:
    transport = FakeTransport()
    session = ready_session(transport)
    session.invalidate()
    initial_calls = tuple(transport.calls)

    operations = (
        lambda: session.evaluate("1"),
        lambda: session.modify("Счетчик", "1"),
        session.continue_,
        lambda: session.wait_for_any_stop(timeout_s=1),
    )
    for operation in operations:
        with pytest.raises(ProtocolError, match="current state is failed"):
            operation()

    assert session.state is SessionState.FAILED
    assert session.target is None
    assert session.attached_targets == {}
    assert tuple(transport.calls) == initial_calls


def test_strict_wait_rejects_unknown_without_break_on_next_continue() -> None:
    transport = FakeTransport()
    transport.responses["pingDebugUIParams"].append(stopped_payload(UNREGISTERED))
    session = ready_session(transport)
    session.break_on_next = True
    session.state = SessionState.EXECUTING

    with pytest.raises(UnexpectedStop, match="does not match any allowed"):
        session.wait_for_stop((LOCATION, CAPTURE_A), timeout_s=1)

    assert session.state is SessionState.READY
    assert transport.calls.count("step") == 0


def test_new_target_receives_full_registered_breakpoint_workspace() -> None:
    target_started = started_payload(TARGET_ID)
    transport = FakeTransport()
    transport.responses["pingDebugUIParams"].extend(
        (target_started, stopped_payload(CAPTURE_A))
    )
    session = RdbgSession(transport, LOCATION)  # type: ignore[arg-type]
    session.initialize()

    session.set_breakpoints((LOCATION, CAPTURE_A))
    event = session.wait_for_stop((LOCATION, CAPTURE_A), timeout_s=1)

    breakpoint_payloads = [
        payload for command, payload in transport.payloads if command == "setBreakpoints"
    ]
    assert event.location == CAPTURE_A
    assert len(breakpoint_payloads) == 2
    for payload in breakpoint_payloads:
        root = ElementTree.fromstring(payload)
        assert sum(
            node.tag.rsplit("}", 1)[-1] == "moduleBPInfo" for node in root.iter()
        ) == 1
        assert sum(
            node.tag.rsplit("}", 1)[-1] == "bpInfo" for node in root.iter()
        ) == 2


def stack_level(payload: bytes) -> str:
    root = ElementTree.fromstring(payload)
    return next(
        node.text or "" for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "stackLevel"
    )


def test_local_variables_returns_direct_result_for_selected_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result_id = UUID("99999999-9999-9999-9999-999999999999")
    response = f"""<response xmlns="{RDBG_NS}"><result>
      <expressionResultID xmlns="{CALC_NS}">{result_id}</expressionResultID>
      <calculationResult xmlns="{CALC_NS}"><valueOfContextPropInfo>
        <propInfo><propName>Документы</propName></propInfo>
        <valueInfo><typeName>Массив</typeName><pres>0JzQsNGB0YHQuNCy</pres></valueInfo>
      </valueOfContextPropInfo></calculationResult>
    </result></response>""".encode()
    transport = FakeTransport()
    transport.responses["evalLocalVariables"].append(response)
    session = ready_session(transport)
    monkeypatch.setattr("onec_runtime.rdbg.session.uuid4", lambda: result_id)

    result = session.local_variables(stack_level=2)

    assert [item.name for item in result.variables] == ["Документы"]
    request = next(payload for command, payload in transport.payloads if command == "evalLocalVariables")
    assert stack_level(request) == "2"
    assert session.state is SessionState.READY
    assert "step" not in transport.calls


def test_local_variables_bounds_the_http_request_by_its_deadline() -> None:
    class TimedOutTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.request_timeout: float | None = None

        def request(
            self, command: str, payload: bytes = b"", *, timeout_s: float = 60.0,
            **kwargs: object,
        ) -> bytes:
            assert command == "evalLocalVariables"
            self.request_timeout = timeout_s
            raise CommandTimeout("RDBG request timed out")

    transport = TimedOutTransport()
    session = ready_session(transport)

    with pytest.raises(CommandTimeout):
        session.local_variables(stack_level=1, timeout_s=0.25)

    assert transport.request_timeout is not None
    assert 0 < transport.request_timeout <= 0.25


def test_local_variables_callback_rejection_prevents_transport_and_pending_state() -> None:
    transport = FakeTransport()
    session = ready_session(transport)

    def reject() -> None:
        raise ValueError("local owner rejected dispatch")

    with pytest.raises(ValueError, match="local owner rejected dispatch"):
        session.local_variables(timeout_s=1, on_transport_dispatch=reject)

    assert transport.calls == []
    assert session._pending_local_variables == {}
    assert session.state is SessionState.READY


def test_local_variables_marks_transport_entry_before_each_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_id = UUID("11111111-9999-9999-9999-999999999999")
    second_id = UUID("22222222-9999-9999-9999-999999999999")
    events: list[str] = []

    class OrderedTransport(FakeTransport):
        def request(self, command: str, payload: bytes = b"", **options: object) -> bytes:
            events.append("request")
            return super().request(command, payload, **options)

    transport = OrderedTransport()
    for result_id in (first_id, second_id):
        transport.responses["evalLocalVariables"].append(
            f"""<response xmlns="{RDBG_NS}"><result>
              <expressionResultID xmlns="{CALC_NS}">{result_id}</expressionResultID>
              <calculationResult xmlns="{CALC_NS}"/><errorOccurred>false</errorOccurred>
            </result></response>""".encode()
        )
    session = ready_session(transport)
    result_ids = iter((first_id, second_id))
    monkeypatch.setattr("onec_runtime.rdbg.session.uuid4", lambda: next(result_ids))

    result = session.local_variables(
        timeout_s=1,
        retry_delays_s=(0,),
        on_transport_dispatch=lambda: events.append("dispatch"),
    )

    assert result.result_id == second_id
    assert events == ["dispatch", "request", "dispatch", "request"]
    assert transport.calls == ["evalLocalVariables", "evalLocalVariables"]


def test_evaluate_targets_selected_stack_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result_id = UUID("88888888-8888-8888-8888-888888888888")
    response = f"""<response xmlns="{RDBG_NS}"><result>
      <expressionResultID xmlns="{CALC_NS}">{result_id}</expressionResultID>
      <resultValueInfo xmlns="{CALC_NS}"><typeName>Булево</typeName>
      <pres>0JjRgdGC0LjQvdCw</pres></resultValueInfo>
      <errorOccurred xmlns="{CALC_NS}">false</errorOccurred>
      </result></response>""".encode()
    transport = FakeTransport()
    transport.responses["evalExpr"].append(response)
    session = ready_session(transport)
    monkeypatch.setattr("onec_runtime.rdbg.session.uuid4", lambda: result_id)

    session.evaluate(
        "RuntimeKernelServer.НачатьКонтекстОтладки(Неопределено, Контекст)",
        stack_level=2,
    )

    request = next(payload for command, payload in transport.payloads if command == "evalExpr")
    assert stack_level(request) == "2"


def test_evaluation_stop_is_returned_before_matching_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = FakeTransport()
    session = ready_session(transport)
    pending = session.start_evaluation("1", stack_level=0)
    stop = StopEvent(
        session.target.target_id,
        UNREGISTERED,
        "callStackFormed",
        stop_by_breakpoint=True,
    )
    result = EvaluationResult(pending.result_id, "Число", "1", False)
    batches = iter((([stop], []), ([], [result])))
    monkeypatch.setattr(session, "_poll", lambda _timeout: next(batches))

    observed = session.wait_evaluation_event(pending, timeout_s=1)
    assert observed is stop
    session.continue_evaluation(pending, stop)
    assert session.wait_evaluation_event(pending, timeout_s=1) is result
    assert transport.calls.count("step") == 1


def test_continue_evaluation_callback_rejection_keeps_exact_pending_stop() -> None:
    transport = FakeTransport()
    session = ready_session(transport)
    pending = session.start_evaluation("1")
    stop = StopEvent(pending.target_id, UNREGISTERED, "callStackFormed")
    session._event_queue.append(stop)
    assert session.wait_evaluation_event(pending, timeout_s=1) is stop

    def reject() -> None:
        raise ValueError("Stop fenced Continue")

    with pytest.raises(ValueError, match="Stop fenced Continue"):
        session.continue_evaluation(pending, stop, on_transport_dispatch=reject)

    assert "step" not in transport.calls
    assert session._pending_evaluation_states[id(pending)].suspended_stop is stop
    assert session.state is SessionState.READY


def test_continue_evaluation_marks_exact_stop_before_step_request() -> None:
    events: list[str] = []

    class OrderedTransport(FakeTransport):
        def request(self, command: str, payload: bytes = b"", **options: object) -> bytes:
            if command == "step":
                events.append("step")
            return super().request(command, payload, **options)

    transport = OrderedTransport()
    session = ready_session(transport)
    pending = session.start_evaluation("1")
    stop = StopEvent(pending.target_id, UNREGISTERED, "callStackFormed")
    session._event_queue.append(stop)
    assert session.wait_evaluation_event(pending, timeout_s=1) is stop

    session.continue_evaluation(
        pending, stop, on_transport_dispatch=lambda: events.append("dispatch"),
    )

    assert events == ["dispatch", "step"]
    assert session._pending_evaluation_states[id(pending)].suspended_stop is None
    assert transport.calls.count("step") == 1


def test_continue_evaluation_rejects_foreign_stop_before_callback() -> None:
    transport = FakeTransport()
    session = ready_session(transport)
    pending = session.start_evaluation("1")
    owned_stop = StopEvent(pending.target_id, UNREGISTERED, "callStackFormed")
    session._event_queue.append(owned_stop)
    assert session.wait_evaluation_event(pending, timeout_s=1) is owned_stop
    foreign_stop = StopEvent(pending.target_id, UNREGISTERED, "callStackFormed")
    entered: list[str] = []

    with pytest.raises(ProtocolError, match="stale or foreign"):
        session.continue_evaluation(
            pending, foreign_stop, on_transport_dispatch=lambda: entered.append("step"),
        )

    assert entered == []
    assert "step" not in transport.calls
    assert session._pending_evaluation_states[id(pending)].suspended_stop is owned_stop


def test_start_evaluation_accepts_empty_xml_acknowledgement() -> None:
    transport = FakeTransport()
    transport.responses["evalExpr"].append(
        f'<response xmlns="{RDBG_NS}"></response>'.encode()
    )
    session = ready_session(transport)

    pending = session.start_evaluation("1")

    assert pending.target_id == session.target.target_id
    assert session.state is SessionState.READY


def test_ambiguous_eval_transport_keeps_one_capability_for_late_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TimedOutEvalTransport(FakeTransport):
        def request(self, command: str, payload: bytes = b"", **options: object) -> bytes:
            if command == "evalExpr":
                self.calls.append(command)
                raise CommandTimeout("evalExpr HTTP response was lost")
            return super().request(command, payload, **options)

    transport = TimedOutEvalTransport()
    session = ready_session(transport)
    entered: list[str] = []

    with pytest.raises(EvaluationDispatchUnknown) as raised:
        session.start_evaluation("1", on_transport_dispatch=lambda: entered.append("evalExpr"))

    pending = raised.value.pending
    assert entered == ["evalExpr"]
    assert len(session._pending_evaluation_states) == 1
    with pytest.raises(ProtocolError, match="already pending"):
        session.start_evaluation("2")

    result = EvaluationResult(pending.result_id, "Число", "1", False)
    monkeypatch.setattr(session, "_poll", lambda _timeout: ([], [result]))
    assert session.wait_evaluation_event(pending, timeout_s=1) is result
    assert transport.calls.count("evalExpr") == 1
    assert session._pending_evaluation_states == {}


def test_pretransport_eval_admission_failure_releases_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = FakeTransport()
    session = ready_session(transport)
    original_request = session._request
    entered: list[str] = []

    def invalidate_before_request(command: str, payload: bytes = b"", **options: object) -> bytes:
        session.invalidate()
        return original_request(command, payload, **options)

    monkeypatch.setattr(session, "_request", invalidate_before_request)
    with pytest.raises(ProtocolError, match="invalidated"):
        session.start_evaluation("1", on_transport_dispatch=lambda: entered.append("evalExpr"))

    assert entered == []
    assert transport.calls == []
    assert session._pending_evaluation_states == {}


def test_invalidation_after_eval_dispatch_reports_unknown_outcome() -> None:
    class InvalidatingTransport(FakeTransport):
        def request(self, command: str, payload: bytes = b"", **options: object) -> bytes:
            response = super().request(command, payload, **options)
            if command == "evalExpr":
                session.invalidate()
            return response

    transport = InvalidatingTransport()
    session = ready_session(transport)
    entered: list[str] = []

    with pytest.raises(EvaluationDispatchUnknown) as raised:
        session.start_evaluation("1", on_transport_dispatch=lambda: entered.append("evalExpr"))

    assert entered == ["evalExpr"]
    assert raised.value.pending.target_id is not None
    assert transport.calls.count("evalExpr") == 1
    assert session.state is SessionState.FAILED


def test_malformed_eval_ack_retains_capability_until_correlated_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = FakeTransport()
    transport.responses["evalExpr"].append(b"<broken")
    session = ready_session(transport)

    with pytest.raises(EvaluationDispatchUnknown) as raised:
        session.start_evaluation("1")

    pending = raised.value.pending
    assert isinstance(raised.value.__cause__, ProtocolError)
    result = EvaluationResult(pending.result_id, "Число", "1", False)
    monkeypatch.setattr(session, "_poll", lambda _timeout: ([], [result]))
    assert session.wait_evaluation_event(pending, timeout_s=1) is result
    assert transport.calls.count("evalExpr") == 1


def test_start_evaluation_treats_nonempty_xml_without_result_as_unknown() -> None:
    transport = FakeTransport()
    transport.responses["evalExpr"].append(
        f'<response xmlns="{RDBG_NS}"><unexpected/></response>'.encode()
    )
    session = ready_session(transport)

    with pytest.raises(EvaluationDispatchUnknown) as raised:
        session.start_evaluation("1")

    assert isinstance(raised.value.__cause__, ProtocolError)
    assert "missing required result" in str(raised.value.__cause__)
    assert len(session._pending_evaluation_states) == 1


def test_pending_evaluation_rejects_copied_capability() -> None:
    transport = FakeTransport()
    session = ready_session(transport)
    pending = session.start_evaluation("1")
    copied = PendingEvaluation(
        pending.target_id,
        pending.result_id,
        pending.owner,
    )

    with pytest.raises(ProtocolError, match="stale or foreign"):
        session.wait_evaluation_event(copied, timeout_s=1)


def test_local_variables_correlates_deferred_ping_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result_id = UUID("aaaaaaaa-9999-9999-9999-999999999999")
    other_id = UUID("bbbbbbbb-9999-9999-9999-999999999999")
    unrelated = f"""<response xmlns="{RDBG_NS}"><result><cmdID>exprEvaluated</cmdID>
      <evalExprResBaseData><expressionResultID xmlns="{CALC_NS}">{other_id}</expressionResultID>
        <calculationResult xmlns="{CALC_NS}"><valueOfContextPropInfo>
          <propInfo><propName>Чужая</propName></propInfo>
          <valueInfo><typeName>Число</typeName><pres>MQ==</pres></valueInfo>
        </valueOfContextPropInfo></calculationResult>
      </evalExprResBaseData></result></response>""".encode()
    matching = f"""<response xmlns="{RDBG_NS}"><result><cmdID>exprEvaluated</cmdID>
      <evalExprResBaseData><expressionResultID xmlns="{CALC_NS}">{result_id}</expressionResultID>
        <calculationResult xmlns="{CALC_NS}"><valueOfContextPropInfo>
          <propInfo><propName>Результат</propName></propInfo>
          <valueInfo><typeName>Массив</typeName><pres>W10=</pres></valueInfo>
        </valueOfContextPropInfo></calculationResult>
      </evalExprResBaseData></result></response>""".encode()
    transport = FakeTransport()
    transport.responses["evalLocalVariables"].append(b"")
    transport.responses["pingDebugUIParams"].extend((unrelated, matching))
    session = ready_session(transport)
    monkeypatch.setattr("onec_runtime.rdbg.session.uuid4", lambda: result_id)

    result = session.local_variables(timeout_s=1)

    assert [item.name for item in result.variables] == ["Результат"]
    assert session.state is SessionState.READY
    assert "step" not in transport.calls


def test_local_variables_retries_bounded_empty_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result_ids = [
        UUID(f"{number:08d}-9999-9999-9999-999999999999")
        for number in range(1, 5)
    ]
    transport = FakeTransport()
    for result_id in result_ids:
        transport.responses["evalLocalVariables"].append(
            f"""<response xmlns="{RDBG_NS}"><result>
              <expressionResultID xmlns="{CALC_NS}">{result_id}</expressionResultID>
              <calculationResult xmlns="{CALC_NS}"/><errorOccurred>false</errorOccurred>
            </result></response>""".encode()
        )
    session = ready_session(transport)
    generated_ids = iter(result_ids)
    monkeypatch.setattr("onec_runtime.rdbg.session.uuid4", lambda: next(generated_ids))

    result = session.local_variables(timeout_s=1, retry_delays_s=(0, 0, 0))

    assert result.variables == ()
    assert result.result_id == result_ids[-1]
    assert transport.calls.count("evalLocalVariables") == 4
    assert session.state is SessionState.READY
    assert "step" not in transport.calls


def test_local_variables_rejects_negative_stack_level() -> None:
    transport = FakeTransport()
    session = ready_session(transport)

    with pytest.raises(ValueError, match="non-negative"):
        session.local_variables(stack_level=-1)

    assert "evalLocalVariables" not in transport.calls


def test_evaluate_collection_returns_absolute_row_indices_and_page_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result_id = UUID("88888888-8888-8888-8888-888888888888")
    response = f"""<response xmlns="{RDBG_NS}"><result>
      <expressionResultID xmlns="{CALC_NS}">{result_id}</expressionResultID>
      <resultValueInfo xmlns="{CALC_NS}"><typeName>ТаблицаЗначений</typeName>
        <collectionSize>10000</collectionSize></resultValueInfo>
      <calculationResult xmlns="{CALC_NS}"><viewInterface>collection</viewInterface>
        <valueOfCollectionInfo><valueOfContextPropInfo>
          <propInfo><propName>Номер</propName></propInfo>
          <valueInfo><typeName>Число</typeName><valueDecimal>4801</valueDecimal><pres>NDgwMQ==</pres></valueInfo>
        </valueOfContextPropInfo></valueOfCollectionInfo>
      </calculationResult><errorOccurred>false</errorOccurred>
    </result></response>""".encode()
    transport = FakeTransport()
    transport.responses["evalExpr"].append(response)
    session = ready_session(transport)
    monkeypatch.setattr("onec_runtime.rdbg.session.uuid4", lambda: result_id)

    result = session.evaluate_collection(
        "Контекст.ZupMaterializationTable",
        start_index=4800,
        page_size=2400,
        stack_level=2,
    )

    assert result.collection_size == 10000
    assert [row.index for row in result.collection_rows] == [4800]
    request = next(payload for command, payload in transport.payloads if command == "evalExpr")
    root = ElementTree.fromstring(request)
    fields = {
        node.tag.rsplit("}", 1)[-1]: node.text
        for node in root.iter()
        if node.tag.rsplit("}", 1)[-1]
        in {"interfaces", "startIndex", "pageSize", "stackLevel"}
    }
    assert fields == {
        "interfaces": "collection",
        "startIndex": "4800",
        "pageSize": "2400",
        "stackLevel": "2",
    }
    assert session.state is SessionState.READY


def test_evaluate_collection_correlates_deferred_ping_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result_id = UUID("77777777-7777-7777-7777-777777777777")
    deferred = f"""<response xmlns="{RDBG_NS}"><result><cmdID>exprEvaluated</cmdID>
      <evalExprResBaseData><expressionResultID xmlns="{CALC_NS}">{result_id}</expressionResultID>
      <resultValueInfo xmlns="{CALC_NS}"><typeName>ТаблицаЗначений</typeName>
        <collectionSize>1</collectionSize></resultValueInfo>
      <calculationResult xmlns="{CALC_NS}"><viewInterface>collection</viewInterface>
        <valueOfCollectionInfo><valueOfContextPropInfo>
          <propInfo><propName>Номер</propName></propInfo>
          <valueInfo><typeName>Число</typeName><valueDecimal>1</valueDecimal><pres>MQ==</pres></valueInfo>
        </valueOfContextPropInfo></valueOfCollectionInfo>
      </calculationResult><errorOccurred>false</errorOccurred>
      </evalExprResBaseData></result></response>""".encode()
    transport = FakeTransport()
    transport.responses["evalExpr"].append(b"")
    transport.responses["pingDebugUIParams"].append(deferred)
    recorder = PhaseRecorder()
    session = ready_session(transport)
    session.profiler = recorder
    monkeypatch.setattr("onec_runtime.rdbg.session.uuid4", lambda: result_id)

    result = session.evaluate_collection("Контекст.Таблица", start_index=0, timeout_s=1)

    assert result.collection_size == 1
    assert result.collection_rows[0].cells[0].value_decimal == "1"
    assert transport.calls.count("evalExpr") == 1
    assert session.state is SessionState.READY
    assert [event.phase for event in recorder.events] == [
        "rdbg.collection.eval_request",
        "rdbg.collection.direct_parse",
        "rdbg.collection.pending_lookup",
        "rdbg.ping.request",
        "rdbg.ping.parse_xml",
        "rdbg.ping.extract_targets",
        "rdbg.ping.extract_stops",
        "rdbg.ping.extract_local_variables",
        "rdbg.ping.extract_evaluations",
        "rdbg.collection.pending_lookup",
        "rdbg.collection.reindex_rows",
    ]
    assert recorder.events[0].output_bytes == 0
    assert recorder.events[3].output_bytes == len(deferred)
    assert recorder.events[8].item_count == 1
    assert all(event.page_start == 0 for event in recorder.events)


def test_started_collection_evaluation_retains_one_capability_until_late_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result_id = UUID("67676767-6767-6767-6767-676767676767")
    deferred = f"""<response xmlns="{RDBG_NS}"><result><cmdID>exprEvaluated</cmdID>
      <evalExprResBaseData><expressionResultID xmlns="{CALC_NS}">{result_id}</expressionResultID>
      <resultValueInfo xmlns="{CALC_NS}"><typeName>ТаблицаЗначений</typeName>
        <collectionSize>1</collectionSize></resultValueInfo>
      <calculationResult xmlns="{CALC_NS}"><viewInterface>collection</viewInterface>
        <valueOfCollectionInfo><valueInfo><typeName>Строка</typeName>
          <valueString>Колонка</valueString><pres>0JrQvtC70L7QvdC60LA=</pres>
        </valueInfo></valueOfCollectionInfo>
      </calculationResult><errorOccurred>false</errorOccurred>
      </evalExprResBaseData></result></response>""".encode()
    transport = FakeTransport()
    transport.responses["evalExpr"].append(b"")
    transport.responses["pingDebugUIParams"].append(deferred)
    session = ready_session(transport)
    monkeypatch.setattr("onec_runtime.rdbg.session.uuid4", lambda: result_id)
    dispatches: list[str] = []

    pending = session.start_collection_evaluation(
        "Контекст.Таблица",
        start_index=100,
        page_size=101,
        stack_level=2,
        on_transport_dispatch=lambda: dispatches.append("entered"),
    )

    assert isinstance(pending, PendingEvaluation)
    assert dispatches == ["entered"]
    assert transport.calls.count("evalExpr") == 1

    result = session.wait_evaluation_event(pending, timeout_s=1)

    assert isinstance(result, EvaluationResult)
    assert [row.index for row in result.collection_rows] == [100]
    assert transport.calls.count("evalExpr") == 1
    assert session.state is SessionState.READY


def test_collection_uses_one_deadline_for_http_dispatch_and_result_polling(monkeypatch):
    now = [100.0]
    requests = []

    class SlowTransport(FakeTransport):
        def request(self, command, payload=b"", **kwargs):
            requests.append((command, kwargs.get("timeout_s")))
            if command == "evalExpr":
                now[0] += 0.98
            else:
                now[0] += kwargs["timeout_s"]
            return b""

    monkeypatch.setattr("onec_runtime.rdbg.session.monotonic", lambda: now[0])
    session = ready_session(SlowTransport())
    with pytest.raises(CommandTimeout):
        session.evaluate_collection("Контекст.Данные", start_index=0, timeout_s=1.0)
    assert requests[0] == ("evalExpr", pytest.approx(1.0))
    assert requests[1] == ("pingDebugUIParams", pytest.approx(0.02))
    assert len(requests) == 2
    assert now[0] == pytest.approx(101.0)


def test_collection_rejects_direct_result_received_after_deadline(monkeypatch):
    result_id = UUID("88888888-8888-8888-8888-888888888888")
    now = [100.0]
    response = f'''<response xmlns="{RDBG_NS}"><result>
      <expressionResultID xmlns="{CALC_NS}">{result_id}</expressionResultID>
      <resultValueInfo xmlns="{CALC_NS}"><typeName>ТаблицаЗначений</typeName>
        <collectionSize>0</collectionSize></resultValueInfo>
      <errorOccurred>false</errorOccurred>
    </result></response>'''.encode()

    class LateTransport(FakeTransport):
        def request(self, command, payload=b"", **kwargs):
            now[0] += 1.01
            return response

    monkeypatch.setattr("onec_runtime.rdbg.session.uuid4", lambda: result_id)
    monkeypatch.setattr("onec_runtime.rdbg.session.monotonic", lambda: now[0])
    session = ready_session(LateTransport())
    with pytest.raises(CommandTimeout):
        session.evaluate_collection("Контекст.Данные", start_index=0, timeout_s=1.0)


def test_modify_returns_correlated_error_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result_id = UUID("55555555-5555-5555-5555-555555555555")
    transport = FakeTransport()
    transport.responses["modifyValue"].append(
        f"""<response xmlns="{BASE_NS}" xmlns:rdbg="{RDBG_NS}"
          xmlns:calc="{CALC_NS}"><rdbg:newValueState>
          <calc:evalResultState>withErrors</calc:evalResultState>
          <calc:expressionResultID>{result_id}</calc:expressionResultID>
          <calc:resultValueInfo>
          <calc:presProcessedCorrectly>false</calc:presProcessedCorrectly>
          </calc:resultValueInfo>
          <calc:errorOccurred>true</calc:errorOccurred>
          <calc:exceptionStr>0J7RiNC40LHQutCw</calc:exceptionStr>
          </rdbg:newValueState></response>""".encode()
    )
    session = ready_session(transport)
    monkeypatch.setattr("onec_runtime.rdbg.session.uuid4", lambda: result_id)

    result = session.modify("НетТакойПеременной", "1")

    assert result.result_id == result_id
    assert result.error_occurred is True
    assert result.error_text == "Ошибка"
    assert session.state is SessionState.READY


def test_modify_rejects_mismatched_result_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_id = UUID("66666666-6666-6666-6666-666666666666")
    actual_id = UUID("77777777-7777-7777-7777-777777777777")
    transport = FakeTransport()
    transport.responses["modifyValue"].append(
        f"""<response xmlns="{BASE_NS}" xmlns:rdbg="{RDBG_NS}"
          xmlns:calc="{CALC_NS}"><rdbg:newValueState>
          <calc:evalResultState>correctly</calc:evalResultState>
          <calc:expressionResultID>{actual_id}</calc:expressionResultID>
          <calc:resultValueInfo><calc:typeName>Число</calc:typeName>
          <calc:pres>MQ==</calc:pres></calc:resultValueInfo>
          </rdbg:newValueState></response>""".encode()
    )
    session = ready_session(transport)
    monkeypatch.setattr("onec_runtime.rdbg.session.uuid4", lambda: expected_id)

    with pytest.raises(ProtocolError, match="modifyValue result"):
        session.modify("Счетчик", "1")


def test_pending_evaluation_survives_interval_timeout_and_consumes_one_late_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interval timeout must preserve the original capability without redispatch."""
    transport = FakeTransport()
    session = ready_session(transport)
    pending = session.start_evaluation("1")
    result = EvaluationResult(pending.result_id, "Число", "1", False)
    intervals = []

    def timed_out(timeout_s):
        intervals.append(timeout_s)
        raise CommandTimeout("interval elapsed")

    monkeypatch.setattr(session, "_poll", timed_out)
    for _ in range(3):
        with pytest.raises(CommandTimeout):
            session.wait_evaluation_event(pending, timeout_s=0.025)
    assert len(session._pending_evaluation_states) == 1
    assert all(0 < interval <= 0.025 for interval in intervals)
    monkeypatch.setattr(session, "_poll", lambda timeout_s: ([], [result]))
    assert session.wait_evaluation_event(pending, timeout_s=0.025) is result
    with pytest.raises(ProtocolError, match="stale or foreign"):
        session.wait_evaluation_event(pending, timeout_s=0.025)
    assert transport.calls.count("evalExpr") == 1
    assert not session._pending_evaluation_states

@pytest.mark.parametrize('command', ['set_breakpoints', 'modify', 'continue_'])
def test_main_commands_report_transport_entry_after_validation(command):
    transport = FakeTransport()
    session = ready_session(transport)
    entered = []
    def invoke():
        if command == 'set_breakpoints':
            return session.set_breakpoints((LOCATION,), on_transport_dispatch=lambda: entered.append(True))
        if command == 'modify':
            return session.modify('x', '1', on_transport_dispatch=lambda: entered.append(True))
        return session.continue_(on_transport_dispatch=lambda: entered.append(True))
    session.state = SessionState.DETACHED
    with pytest.raises(ProtocolError):
        invoke()
    assert entered == []
    assert transport.calls == []
    session.state = SessionState.READY
    if command == 'modify':
        with pytest.raises(ProtocolError):  # Empty response is ambiguous after entry.
            invoke()
    else:
        invoke()
    assert entered == [True]


def test_continue_validates_remote_acknowledgement():
    transport = FakeTransport()
    session = ready_session(transport)
    transport.responses['step'].append(b'<response><result>failure</result></response>')
    with pytest.raises(ProtocolError):
        session.continue_()
    assert session.state is SessionState.EXECUTING


def test_wait_expected_target_preserves_foreign_stop_before_admission():
    transport = FakeTransport()
    session = ready_session(transport)
    expected = session.target
    foreign = DebugTarget(TargetId(UUID('33333333-3333-3333-3333-333333333333'), 'DefAlias'), 'ServerEmulation', 'stopped')
    session.attached_targets[foreign.target_id.id] = foreign
    foreign_stop = StopEvent(foreign.target_id, LOCATION, 'breakpoint')
    expected_stop = StopEvent(expected.target_id, LOCATION, 'breakpoint')
    session._event_queue.extend((foreign_stop, expected_stop))
    session.state = SessionState.EXECUTING
    assert session.wait_for_any_stop(expected_target=expected.target_id, timeout_s=1) is expected_stop
    assert session.target is expected
    assert session.state is SessionState.READY
    # Another owner can later consume the original foreign event.
    session.state = SessionState.ATTACHED
    assert session.wait_for_any_stop(expected_target=foreign.target_id, timeout_s=1) is foreign_stop
    assert session.target is foreign
    assert transport.calls == []


def test_foreign_only_stop_does_not_prevent_later_expected_poll():
    transport = FakeTransport()
    session = ready_session(transport)
    expected = session.target
    foreign = DebugTarget(TargetId(UUID('33333333-3333-3333-3333-333333333333'), 'DefAlias'), 'ServerEmulation', 'stopped')
    session.attached_targets[foreign.target_id.id] = foreign
    foreign_stop = StopEvent(foreign.target_id, LOCATION, 'breakpoint')
    session._event_queue.append(foreign_stop)
    session.state = SessionState.EXECUTING
    with pytest.raises(CommandTimeout):
        session.wait_for_any_stop(expected_target=expected.target_id, timeout_s=0.001)
    assert session.target is expected
    assert session.state is SessionState.EXECUTING
    expected_stop = StopEvent(expected.target_id, LOCATION, 'breakpoint')
    session._event_queue.append(expected_stop)
    assert session.wait_for_any_stop(expected_target=expected.target_id, timeout_s=1) is expected_stop
    session.state = SessionState.ATTACHED
    assert session.wait_for_any_stop(expected_target=foreign.target_id, timeout_s=1) is foreign_stop


def test_continue_accepts_step_target_state_response_for_selected_target():
    transport = FakeTransport()
    session = ready_session(transport)
    transport.responses["step"].append(
        f'''<response xmlns="{RDBG_NS}"><item>
        <targetID xmlns="{BASE_NS}"><id>{TARGET_ID}</id>
        <infoBaseAlias>DefAlias</infoBaseAlias><targetType>ServerEmulation</targetType>
        </targetID><stateNum>16</stateNum><state>Worked</state>
        </item></response>'''.encode()
    )

    session.continue_()

    assert session.state is SessionState.EXECUTING
    assert transport.calls == ["step"]


def test_continue_accepts_selected_target_among_step_state_items():
    transport = FakeTransport()
    session = ready_session(transport)
    transport.responses["step"].append(
        f'''<response xmlns="{RDBG_NS}">
        <item><targetID xmlns="{BASE_NS}"><id>{UUID(int=91)}</id>
        <infoBaseAlias>DefAlias</infoBaseAlias><targetType>ManagedClient</targetType>
        </targetID><stateNum>16</stateNum><state>Worked</state></item>
        <item><targetID xmlns="{BASE_NS}"><id>{TARGET_ID}</id>
        <infoBaseAlias>DefAlias</infoBaseAlias><targetType>ServerEmulation</targetType>
        </targetID><stateNum>16</stateNum><state>Worked</state></item>
        </response>'''.encode()
    )

    session.continue_()

    assert session.state is SessionState.EXECUTING


def test_continue_rejects_step_state_response_for_another_target():
    transport = FakeTransport()
    session = ready_session(transport)
    transport.responses["step"].append(
        f'''<response xmlns="{RDBG_NS}"><item>
        <targetID xmlns="{BASE_NS}"><id>{UUID(int=91)}</id>
        <infoBaseAlias>DefAlias</infoBaseAlias><targetType>ServerEmulation</targetType>
        </targetID><stateNum>16</stateNum><state>Worked</state>
        </item></response>'''.encode()
    )

    with pytest.raises(ProtocolError, match="step acknowledgement"):
        session.continue_()
    assert session.state is SessionState.EXECUTING


def test_empty_stop_interval_has_distinct_retryable_exception():
    from onec_runtime.errors import StopWaitIntervalElapsed
    session = ready_session(FakeTransport())
    session.state = SessionState.EXECUTING
    with pytest.raises(StopWaitIntervalElapsed):
        session.wait_for_any_stop(timeout_s=0)
    assert session.state is SessionState.EXECUTING


def test_stop_transport_timeout_is_not_an_empty_interval():
    from onec_runtime.errors import RdbgTransportTimeout, StopWaitIntervalElapsed
    class FailedTransport(FakeTransport):
        def request(self, *args, **kwargs):
            raise RdbgTransportTimeout('network timeout')
    session = ready_session(FailedTransport())
    session.state = SessionState.EXECUTING
    with pytest.raises(RdbgTransportTimeout) as raised:
        session.wait_for_any_stop(timeout_s=1)
    assert not isinstance(raised.value, StopWaitIntervalElapsed)
    assert session.state is SessionState.EXECUTING
