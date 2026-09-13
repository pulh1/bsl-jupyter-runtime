from __future__ import annotations

from collections import deque
from collections.abc import Callable
from uuid import UUID

import pytest

import onec_runtime.bootstrap as bootstrap_module
from onec_runtime.bootstrap import (
    ExtensionHandshakeContract,
    enable_server_kernel_loop,
    observe_extension_handshake,
    verify_extension_handshake,
    wait_for_managed_startup_stop,
    wait_for_server_entry_then_service,
)
from onec_runtime.errors import ExtensionHandshakeError, ProtocolError, TargetLost, UnexpectedStop
from onec_runtime.extension_bundle import ExtensionHandshakeEvidence
from onec_runtime.kernel import SESSION_MODULE_PROPERTY_ID
from onec_runtime.rdbg.models import (
    DebugTarget,
    EvaluationResult,
    ModifyResult,
    ModuleLocation,
    StopEvent,
    TargetId,
)

TARGET = TargetId(UUID("22222222-2222-2222-2222-222222222222"), "DefAlias")
STARTUP = ModuleLocation(
    "ExtensionModule",
    "",
    UUID("883af47f-bd19-491f-8dfa-3bd4ff6e0cfa"),
    UUID("d22e852a-cf8a-4f77-8ccb-3548e7792bea"),
    4,
    "OnecInteractiveRuntime",
)
TRANSIENT = ModuleLocation(
    "ConfigModule",
    "",
    UUID("695a9be0-db9e-4a48-be4d-cca698ac15ee"),
    UUID(SESSION_MODULE_PROPERTY_ID),
    2,
)
ZUP_TRANSIENT = ModuleLocation(
    "ConfigModule",
    "",
    UUID("6447867e-d03a-48f8-8936-21dc8c847fae"),
    UUID(SESSION_MODULE_PROPERTY_ID),
    16,
)
MANAGED_TRANSIENT = ModuleLocation(
    STARTUP.module_type,
    STARTUP.url,
    STARTUP.object_id,
    STARTUP.property_id,
    3,
    STARTUP.extension_name,
    STARTUP.ext_id,
)
UNKNOWN = ModuleLocation(
    "ConfigModule",
    "",
    UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
    UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
    9,
)
SERVER_ENTRY = ModuleLocation(
    "ExtensionModule",
    "",
    UUID("cb953767-f436-4a5b-9e09-13a67d6e0201"),
    UUID("d5963243-262e-4398-b4d7-fb16d06484f6"),
    132,
    "OnecInteractiveRuntime",
)
SERVER_SERVICE = ModuleLocation(
    "ExtensionModule",
    "",
    UUID("cb953767-f436-4a5b-9e09-13a67d6e0201"),
    UUID("d5963243-262e-4398-b4d7-fb16d06484f6"),
    134,
    "OnecInteractiveRuntime",
)

HANDSHAKE_VALUES = {
    "ИдентификаторПродуктаRuntime": "onec-interactive-runtime",
    "ВерсияАртефактаRuntime": "0.1.0",
    "ВерсияПротоколаRuntime": "1",
}


class EvaluatingSession:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.evaluated: list[str] = []

    def evaluate(self, expression: str) -> EvaluationResult:
        self.evaluated.append(expression)
        return EvaluationResult(UUID(int=1), "Строка", self.values[expression], False)

    def modify(self, variable: str, value_expression: str) -> ModifyResult:
        raise AssertionError("handshake verification must not modify frame variables")


def handshake_contract() -> ExtensionHandshakeContract:
    return ExtensionHandshakeContract("onec-interactive-runtime", "0.1.0", "1")


def matching_session() -> EvaluatingSession:
    return EvaluatingSession(dict(HANDSHAKE_VALUES))


def verify_matching_handshake(
    session: EvaluatingSession,
) -> ExtensionHandshakeEvidence:
    return verify_extension_handshake(
        session,
        handshake_contract(),
        target_type="ManagedClient",
        location=STARTUP,
    )


def test_observe_handshake_returns_actual_values_without_contract() -> None:
    values = {
        "ИдентификаторПродуктаRuntime": "user-product",
        "ВерсияАртефактаRuntime": "0.1.0-user.1",
        "ВерсияПротоколаRuntime": "2",
    }

    evidence = observe_extension_handshake(
        EvaluatingSession(values),
        target_type="ManagedClient",
        location=STARTUP,
    )

    assert evidence == ExtensionHandshakeEvidence(
        "ManagedClient", "user-product", "0.1.0-user.1", "2", STARTUP
    )


def test_observe_handshake_decodes_rdbg_quoted_strings() -> None:
    values = {
        "ИдентификаторПродуктаRuntime": '"onec-interactive-runtime"',
        "ВерсияАртефактаRuntime": '"user""build"',
        "ВерсияПротоколаRuntime": '"1"',
    }

    evidence = observe_extension_handshake(
        EvaluatingSession(values),
        target_type="ManagedClient",
        location=STARTUP,
    )

    assert evidence.artifact_version == 'user"build'


@pytest.mark.parametrize(
    "presentation",
    ("private\nvalue", '"unterminated', "x" * 129),
)
def test_observe_handshake_rejects_unsafe_presentation_without_echo(
    presentation: str,
) -> None:
    values = dict(HANDSHAKE_VALUES)
    values["ВерсияАртефактаRuntime"] = presentation

    with pytest.raises(ExtensionHandshakeError, match="ВерсияАртефактаRuntime") as raised:
        observe_extension_handshake(
            EvaluatingSession(values),
            target_type="ManagedClient",
            location=STARTUP,
        )

    assert presentation not in str(raised.value)


def test_handshake_requires_all_three_string_values() -> None:
    session = matching_session()

    evidence = verify_matching_handshake(session)

    assert evidence == ExtensionHandshakeEvidence(
        "ManagedClient",
        "onec-interactive-runtime",
        "0.1.0",
        "1",
        STARTUP,
    )
    assert session.evaluated == list(HANDSHAKE_VALUES)


def test_handshake_accepts_exact_rdbg_quoted_string_presentations() -> None:
    session = EvaluatingSession(
        {name: f'"{value}"' for name, value in HANDSHAKE_VALUES.items()}
    )

    evidence = verify_matching_handshake(session)

    assert evidence.product_id == "onec-interactive-runtime"


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    ("variable", "value"),
    (
        ("ИдентификаторПродуктаRuntime", "foreign-secret"),
        ("ВерсияАртефактаRuntime", "private-version"),
        ("ВерсияПротоколаRuntime", "private-protocol"),
    ),
)
def test_handshake_rejects_mismatch_without_returned_value(
    variable: str, value: str
) -> None:
    session = matching_session()
    session.values[variable] = value

    with pytest.raises(ExtensionHandshakeError, match=variable) as raised:
        verify_matching_handshake(session)

    assert value not in str(raised.value)


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    ("type_name", "error_occurred"),
    (("Число", False), ("Строка", True)),
)
def test_handshake_rejects_non_string_or_evaluation_error_without_raw_details(
    type_name: str,
    error_occurred: bool,
) -> None:
    private_presentation = "private-evaluated-value"
    private_error = "private-platform-error"

    class InvalidEvaluationSession(EvaluatingSession):
        def evaluate(self, expression: str) -> EvaluationResult:
            self.evaluated.append(expression)
            return EvaluationResult(
                UUID(int=1),
                type_name,
                private_presentation,
                error_occurred,
                private_error,
            )

    with pytest.raises(ExtensionHandshakeError) as raised:
        verify_matching_handshake(InvalidEvaluationSession(dict(HANDSHAKE_VALUES)))

    message = str(raised.value)
    assert "ИдентификаторПродуктаRuntime" in message
    assert private_presentation not in message
    assert private_error not in message


class ScriptedBootstrapSession:
    def __init__(self, locations: tuple[ModuleLocation, ...]) -> None:
        self.break_on_next = True
        self.stops = deque(
            StopEvent(TARGET, location, "callStackFormed", stack=(location,))
            for location in locations
        )
        self.continue_count = 0

    def wait_for_any_stop(self, *, timeout_s: float) -> StopEvent:
        assert timeout_s > 0
        return self.stops.popleft()

    def continue_(self) -> None:
        self.continue_count += 1


def test_known_break_on_next_transient_is_explicitly_resumed() -> None:
    session = ScriptedBootstrapSession((TRANSIENT, STARTUP))

    stop = wait_for_managed_startup_stop(session, STARTUP, timeout_s=1)

    assert stop.location == STARTUP
    assert session.continue_count == 1


def test_managed_startup_checks_owned_client_while_waiting() -> None:
    class HealthCheckedSession(ScriptedBootstrapSession):
        def wait_for_any_stop(
            self, *, timeout_s: float, on_poll: Callable[[], None] | None = None
        ) -> StopEvent:
            if on_poll is not None:
                on_poll()
            return super().wait_for_any_stop(timeout_s=timeout_s)

    def client_exited() -> None:
        raise TargetLost("owned client exited")

    with pytest.raises(TargetLost, match="owned client exited"):
        wait_for_managed_startup_stop(
            HealthCheckedSession((STARTUP,)), STARTUP, timeout_s=1,
            on_poll=client_exited,
        )


def test_zup_session_module_break_on_next_transient_is_explicitly_resumed() -> None:
    session = ScriptedBootstrapSession((ZUP_TRANSIENT, STARTUP))

    stop = wait_for_managed_startup_stop(session, STARTUP, timeout_s=1)

    assert stop.location == STARTUP
    assert session.continue_count == 1


def test_managed_extension_break_on_next_transient_is_explicitly_resumed() -> None:
    session = ScriptedBootstrapSession((MANAGED_TRANSIENT, STARTUP))

    stop = wait_for_managed_startup_stop(session, STARTUP, timeout_s=1)

    assert stop.location == STARTUP
    assert session.continue_count == 1


def test_unknown_bootstrap_stop_remains_paused() -> None:
    session = ScriptedBootstrapSession((UNKNOWN,))

    with pytest.raises(UnexpectedStop, match="bootstrap"):
        wait_for_managed_startup_stop(session, STARTUP, timeout_s=1)

    assert session.continue_count == 0


@pytest.mark.parametrize("server_target_type", ["ServerEmulation", "Server"])
def test_server_bootstrap_enters_module_before_arming_tight_loop_breakpoint(server_target_type: str) -> None:
    """Regression: the first direct stop inside the tight loop was not observed."""
    events: list[object] = []

    class ServerBootstrapSession:
        def __init__(self) -> None:
            self.expected_location = STARTUP
            self.target = DebugTarget(TARGET, "ManagedClient", "stopped")

        def set_service_breakpoint(self) -> None:
            events.append(("breakpoint", self.expected_location))

        def continue_(self) -> None:
            events.append(("continue", self.target.target_type))

        def wait_for_service_stop(self, *, timeout_s: float) -> StopEvent:
            assert timeout_s > 0
            events.append(("wait", self.expected_location))
            if self.expected_location == SERVER_ENTRY:
                self.target = DebugTarget(TARGET, server_target_type, "stopped")
            return StopEvent(TARGET, self.expected_location, "breakpoint")

    session = ServerBootstrapSession()

    entry_stop, service_stop = wait_for_server_entry_then_service(
        session,
        SERVER_ENTRY,
        SERVER_SERVICE,
        timeout_s=90.0,
        server_target_type=server_target_type,
        on_entry=lambda actual: events.append(
            ("entry-probe", actual.target.target_type)  # type: ignore[union-attr]
        ),
    )

    assert entry_stop.location == SERVER_ENTRY
    assert service_stop.location == SERVER_SERVICE
    assert events == [
        ("breakpoint", SERVER_ENTRY),
        ("continue", "ManagedClient"),
        ("wait", SERVER_ENTRY),
        ("entry-probe", server_target_type),
        ("breakpoint", SERVER_SERVICE),
        ("continue", server_target_type),
        ("wait", SERVER_SERVICE),
    ]


def test_server_bootstrap_shares_one_timeout_budget_between_both_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wait_timeouts: list[float] = []

    class ServerBootstrapSession:
        def __init__(self) -> None:
            self.expected_location = STARTUP
            self.target = DebugTarget(TARGET, "ManagedClient", "stopped")

        def set_service_breakpoint(self) -> None:
            pass

        def continue_(self) -> None:
            pass

        def wait_for_service_stop(self, *, timeout_s: float) -> StopEvent:
            wait_timeouts.append(timeout_s)
            self.target = DebugTarget(TARGET, "ServerEmulation", "stopped")
            return StopEvent(TARGET, self.expected_location, "breakpoint")

    clock = iter((10.0, 10.0, 40.0))
    monkeypatch.setattr(bootstrap_module, "monotonic", lambda: next(clock))

    wait_for_server_entry_then_service(
        ServerBootstrapSession(),
        SERVER_ENTRY,
        SERVER_SERVICE,
        timeout_s=90.0,
    )

    assert wait_timeouts == [90.0, 60.0]


def test_server_bootstrap_rejects_entry_stop_on_non_server_subject() -> None:
    class WrongSubjectSession:
        def __init__(self) -> None:
            self.expected_location = STARTUP
            self.target = DebugTarget(TARGET, "ManagedClient", "stopped")

        def set_service_breakpoint(self) -> None:
            pass

        def continue_(self) -> None:
            pass

        def wait_for_service_stop(self, *, timeout_s: float) -> StopEvent:
            return StopEvent(TARGET, self.expected_location, "breakpoint")

    with pytest.raises(ProtocolError, match="entry.*ServerEmulation"):
        wait_for_server_entry_then_service(
            WrongSubjectSession(),
            SERVER_ENTRY,
            SERVER_SERVICE,
            timeout_s=1.0,
        )


def test_server_loop_guard_requires_false_write_true_readback() -> None:
    events: list[tuple[str, str]] = []

    class GuardSession:
        def __init__(self) -> None:
            self.reads = iter(("Ложь", "Истина"))

        def evaluate(self, expression: str) -> EvaluationResult:
            presentation = next(self.reads)
            events.append(("evaluate", expression))
            return EvaluationResult(TARGET.id, "Булево", presentation, False)

        def modify(self, variable: str, expression: str) -> ModifyResult:
            events.append(("modify", f"{variable}={expression}"))
            return ModifyResult(TARGET.id, "Булево", "Истина", False)

    evidence = enable_server_kernel_loop(GuardSession())

    assert evidence.as_dict() == {
        "variable": "ПродолжатьЦикл",
        "before": {
            "type_name": "Булево",
            "presentation": "Ложь",
            "error_occurred": False,
        },
        "write": {
            "expression": "Истина",
            "type_name": "Булево",
            "presentation": "Истина",
            "error_occurred": False,
        },
        "after": {
            "type_name": "Булево",
            "presentation": "Истина",
            "error_occurred": False,
        },
    }
    assert events == [
        ("evaluate", "ПродолжатьЦикл"),
        ("modify", "ПродолжатьЦикл=Истина"),
        ("evaluate", "ПродолжатьЦикл"),
    ]


def test_server_loop_guard_does_not_write_when_default_is_not_false() -> None:
    class WrongDefaultSession:
        def evaluate(self, expression: str) -> EvaluationResult:
            return EvaluationResult(TARGET.id, "Булево", "Истина", False)

        def modify(self, variable: str, expression: str) -> ModifyResult:
            raise AssertionError("guard must not be written after a wrong default")

    with pytest.raises(ProtocolError, match="was not Ложь"):
        enable_server_kernel_loop(WrongDefaultSession())


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    ("write_presentation", "readback_presentation", "message"),
    (
        ("Ложь", "Истина", "Unable to enable"),
        ("Истина", "Ложь", "readback was not Истина"),
    ),
)
def test_server_loop_guard_rejects_inexact_write_or_readback(
    write_presentation: str,
    readback_presentation: str,
    message: str,
) -> None:
    class InexactGuardSession:
        def __init__(self) -> None:
            self.reads = iter(("Ложь", readback_presentation))

        def evaluate(self, expression: str) -> EvaluationResult:
            return EvaluationResult(TARGET.id, "Булево", next(self.reads), False)

        def modify(self, variable: str, expression: str) -> ModifyResult:
            return ModifyResult(
                TARGET.id,
                "Булево",
                write_presentation,
                False,
            )

    with pytest.raises(ProtocolError, match=message):
        enable_server_kernel_loop(InexactGuardSession())
