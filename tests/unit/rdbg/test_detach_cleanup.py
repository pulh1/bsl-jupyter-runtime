from uuid import UUID

import pytest

from onec_runtime.errors import ProtocolError, RdbgDebugUiNotRegistered
from onec_runtime.rdbg.models import DebugTarget, ModuleLocation, TargetId
from onec_runtime.rdbg.session import RdbgSession, SessionState


LOCATION = ModuleLocation(
    "ExtensionModule", "", UUID(int=1), UUID(int=2), 7, "OnecInteractiveRuntime"
)
TARGET_ID = TargetId(UUID(int=10), "runtime_test", UUID(int=100))


class DetachTransport:
    def __init__(
        self, *, fail_target_always: bool = False, ui_failures: int = 0,
        ui_missing: bool = False,
    ) -> None:
        self.fail_target_always = fail_target_always
        self.ui_failures = ui_failures
        self.ui_missing = ui_missing
        self.calls: list[str] = []
        self.target_calls = 0
        self.ui_calls = 0

    def request(self, command: str, *_args: object, **_kwargs: object) -> bytes:
        self.calls.append(command)
        if command == "attachDetachDbgTargets":
            self.target_calls += 1
            if self.fail_target_always or self.target_calls > 1:
                raise ProtocolError("target is already gone")
        elif command == "detachDebugUI":
            self.ui_calls += 1
            if self.ui_missing:
                raise RdbgDebugUiNotRegistered("debug UI is not registered")
            if self.ui_calls <= self.ui_failures:
                raise ProtocolError("debug UI deregistration failed")
        return b""


def attached_session(transport: DetachTransport) -> RdbgSession:
    session = RdbgSession(transport, LOCATION)  # type: ignore[arg-type]
    target = DebugTarget(TARGET_ID, "ManagedClient", "Stopped")
    session.state = SessionState.READY
    session.target = target
    session.attached_targets[target.target_id.id] = target
    return session


def test_detach_deregisters_ui_when_attached_target_is_already_gone() -> None:
    transport = DetachTransport(fail_target_always=True)
    session = attached_session(transport)

    session.detach()

    assert transport.calls == ["attachDetachDbgTargets", "detachDebugUI"]
    assert session.state is SessionState.DETACHED
    assert session.target is None
    assert session.attached_targets == {}


def test_detach_keeps_ui_state_retryable_until_deregistration_succeeds() -> None:
    transport = DetachTransport(fail_target_always=True, ui_failures=1)
    session = attached_session(transport)
    target = session.target

    with pytest.raises(
        ProtocolError, match="RDBG detach failed: ProtocolError, ProtocolError"
    ):
        session.detach()

    assert session.state is SessionState.READY
    assert session.target is target
    assert set(session.attached_targets) == {TARGET_ID.id}

    session.detach()

    assert transport.ui_calls == 2
    assert transport.target_calls == 2
    assert session.state is SessionState.DETACHED
    assert session.target is None
    assert session.attached_targets == {}


def test_detach_finishes_when_our_debug_ui_has_already_disappeared() -> None:
    transport = DetachTransport(ui_missing=True)
    session = attached_session(transport)

    session.detach()

    assert session.state is SessionState.DETACHED
    assert transport.ui_calls == 1
