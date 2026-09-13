from __future__ import annotations

from uuid import UUID

import pytest

from onec_runtime.errors import ProtocolError, RdbgDebugUiNotRegistered
from onec_runtime.rdbg.models import ModuleLocation
import onec_runtime.session as session_module


LOCATION = ModuleLocation(
    "ExtensionModule", "", UUID(int=1), UUID(int=2), 7,
    "OnecInteractiveRuntime",
)


def scripted_session(monkeypatch, outcomes):
    created = []

    class FakeRdbgSession:
        def __init__(self, *_args, **_kwargs):
            self.index = len(created)
            self.calls = []
            created.append(self)

        def initialize(self):
            self.calls.append("initialize")

        def set_service_breakpoint(self):
            self.calls.append("breakpoint")

        def verify_registration(self):
            self.calls.append("verify")
            error = outcomes[self.index]
            if error is not None:
                raise error

        def detach(self):
            self.calls.append("detach")

    monkeypatch.setattr(session_module, "RdbgSession", FakeRdbgSession)
    return created


def start_with_script(progress=None, attempts=3):
    return session_module._start_rdbg_with_registration_retry(
        transport=object(),
        location=LOCATION,
        alias="test-base",
        server_target_type="Server",
        break_on_next=False,
        progress=progress,
        attempts=attempts,
        retry_interval_s=0,
    )


def test_registration_retries_new_ui_before_startup(monkeypatch):
    created = scripted_session(
        monkeypatch,
        [
            RdbgDebugUiNotRegistered("missing"),
            RdbgDebugUiNotRegistered("missing"),
            None,
        ],
    )
    progress = []

    ready = start_with_script(progress.append)

    assert ready is created[2]
    assert [item.calls for item in created] == [
        ["initialize", "breakpoint", "verify", "detach"],
        ["initialize", "breakpoint", "verify", "detach"],
        ["initialize", "breakpoint", "verify"],
    ]
    assert len(progress) == 2


def test_registration_exhaustion_is_bounded_and_cleans_own_ui(monkeypatch):
    created = scripted_session(
        monkeypatch,
        [RdbgDebugUiNotRegistered("missing")] * 2,
    )

    with pytest.raises(ProtocolError, match="2 attempts"):
        start_with_script(attempts=2)

    assert len(created) == 2
    assert all(item.calls[-1] == "detach" for item in created)


def test_unrelated_registration_error_is_not_retried(monkeypatch):
    created = scripted_session(monkeypatch, [ProtocolError("unrelated")])

    with pytest.raises(ProtocolError, match="unrelated"):
        start_with_script()

    assert len(created) == 1
    assert created[0].calls[-1] == "detach"
