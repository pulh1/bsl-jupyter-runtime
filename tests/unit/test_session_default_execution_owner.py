"""The public bootstrap hands its stopped RDBG target to one execution owner."""

from onec_runtime.execution.public_facade import PublicExecutionFacade
from onec_runtime.session import RuntimeSession

from test_extension_session import (
    FakeLifecycle, fast_decision, patch_successful_runtime_attempt, session_config,
)


def test_default_start_uses_one_public_arbiter_after_bootstrap(
    monkeypatch, tmp_path,
) -> None:
    probe = patch_successful_runtime_attempt(
        monkeypatch, FakeLifecycle(decisions=[fast_decision()]),
    )

    session = RuntimeSession.start(session_config(tmp_path))
    try:
        assert isinstance(session.runtime_api, PublicExecutionFacade)
        assert session.runtime_api._arbiter._session is session._rdbg
        assert probe.attempt_count == 1
    finally:
        session.close()
