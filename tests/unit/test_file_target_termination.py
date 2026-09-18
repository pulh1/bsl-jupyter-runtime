"""Stop of a file infobase requires the owned debuggee to have exited."""

from uuid import UUID

import pytest

from onec_runtime.execution.termination import (
    FileTargetProcessLease,
    FileTerminationConfirmed,
    FileTerminationUnknown,
    terminate_file_target,
)
from onec_runtime.rdbg.models import TargetId


TARGET = TargetId(UUID(int=21), "file-test")


class FakeProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode


class FakeOwnedProcess:
    def __init__(self, *, exit_on_close: bool = True) -> None:
        self.pid = 1234
        self.process = FakeProcess()
        self.exit_on_close = exit_on_close
        self.calls: list[float] = []

    def close(self, timeout_s: float) -> None:
        self.calls.append(timeout_s)
        if self.exit_on_close:
            self.process.returncode = -15


def test_file_stop_confirms_exit_of_the_exact_owned_debuggee() -> None:
    process = FakeOwnedProcess()

    result = terminate_file_target(process, TARGET, grace_s=30)

    assert isinstance(result, FileTerminationConfirmed)
    assert result.expected_target == TARGET
    assert result.pid == process.pid
    assert result.returncode == -15
    assert process.calls == [30.0]


def test_file_stop_does_not_claim_success_when_close_returns_before_exit() -> None:
    process = FakeOwnedProcess(exit_on_close=False)

    result = terminate_file_target(process, TARGET, grace_s=2)

    assert isinstance(result, FileTerminationUnknown)
    assert result.expected_target == TARGET
    assert result.pid == process.pid
    assert result.error_type == "ExitUnverified"
    assert process.calls == [2.0]


def test_file_stop_preserves_owner_when_termination_raises() -> None:
    process = FakeOwnedProcess()

    def reject(timeout_s: float) -> None:
        raise RuntimeError("private process path")

    process.close = reject

    result = terminate_file_target(process, TARGET, grace_s=2)

    assert isinstance(result, FileTerminationUnknown)
    assert result.error_type == "RuntimeError"
    assert "private process path" not in repr(result)


def test_file_stop_can_confirm_exit_even_when_close_raises() -> None:
    process = FakeOwnedProcess()

    def exited_then_raised(timeout_s: float) -> None:
        process.calls.append(timeout_s)
        process.process.returncode = -15
        raise RuntimeError("stream close failed")

    process.close = exited_then_raised

    result = terminate_file_target(process, TARGET, grace_s=2)

    assert isinstance(result, FileTerminationConfirmed)
    assert result.pid == process.pid
    assert result.returncode == -15
    assert process.calls == [2.0]


def test_file_stop_lease_captures_exact_debuggee_pid() -> None:
    process = FakeOwnedProcess()

    lease = FileTargetProcessLease(TARGET, process)

    assert lease.expected_target == TARGET
    assert lease.process is process
    assert lease.pid == 1234


def test_file_stop_lease_rejects_missing_owned_debuggee() -> None:
    with pytest.raises(TypeError, match="owned file debuggee"):
        FileTargetProcessLease(TARGET, None)


def test_unknown_file_stop_retry_only_checks_process_exit() -> None:
    process = FakeOwnedProcess(exit_on_close=False)
    first = terminate_file_target(process, TARGET, grace_s=2)
    assert isinstance(first, FileTerminationUnknown)

    process.process.returncode = -15
    second = terminate_file_target(
        process, TARGET, grace_s=2, request_termination=False,
    )

    assert isinstance(second, FileTerminationConfirmed)
    assert process.calls == [2.0]


@pytest.mark.parametrize("grace_s", [-1.0, float("inf"), float("nan"), True])
def test_file_stop_rejects_invalid_confirmation_grace_before_process_call(
    grace_s: object,
) -> None:
    process = FakeOwnedProcess()

    with pytest.raises(ValueError, match="grace_s"):
        terminate_file_target(process, TARGET, grace_s=grace_s)

    assert process.calls == []
