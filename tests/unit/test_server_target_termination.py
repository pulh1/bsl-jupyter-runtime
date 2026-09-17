"""A Stop request cannot infer target death from RDBG command acknowledgement."""

from __future__ import annotations

from importlib import import_module
from uuid import UUID

import pytest

from onec_runtime.errors import CommandTimeout, ProtocolError, RdbgDebugUiNotRegistered
from onec_runtime.rdbg.models import TargetId
from onec_runtime.rdbg.session import BoundServerTargetAbsence


CLIENT = TargetId(UUID(int=11), "runtime_test", UUID(int=21))
TARGET = TargetId(UUID(int=12), "runtime_test", UUID(int=21))


def _termination_contract():
    try:
        return import_module("onec_runtime.execution.termination")
    except ModuleNotFoundError as error:
        pytest.fail(f"server target termination contract is missing: {error}")


class _ServerPort:
    def __init__(self, evidence: BoundServerTargetAbsence) -> None:
        self.evidence = evidence
        self.calls: list[object] = []

    def terminate_bound_server_session(self) -> bool:
        self.calls.append("terminate")
        return True

    def wait_for_bound_server_targets_absent(
        self, expected_target: TargetId, *, timeout_s: float
    ) -> BoundServerTargetAbsence:
        self.calls.append(("confirm", expected_target, timeout_s))
        return self.evidence


def test_stop_confirms_server_target_only_after_registry_absence() -> None:
    contract = _termination_contract()
    evidence = BoundServerTargetAbsence(CLIENT, TARGET, 1.0, 2)
    port = _ServerPort(evidence)

    result = contract.terminate_server_target(port, TARGET)

    assert isinstance(result, contract.ServerTerminationConfirmed)
    assert result.expected_target == TARGET
    assert result.absence is evidence
    assert port.calls == ["terminate", ("confirm", TARGET, 30.0)]


def test_termination_ack_without_absence_keeps_exact_target_unknown() -> None:
    contract = _termination_contract()

    class NoAbsencePort(_ServerPort):
        def wait_for_bound_server_targets_absent(
            self, expected_target: TargetId, *, timeout_s: float
        ) -> BoundServerTargetAbsence:
            self.calls.append(("confirm", expected_target, timeout_s))
            raise CommandTimeout("private target detail")

    port = NoAbsencePort(BoundServerTargetAbsence(CLIENT, TARGET, 1.0, 1))

    result = contract.terminate_server_target(port, TARGET)

    assert isinstance(result, contract.TerminationUnknown)
    assert result.expected_target == TARGET
    assert result.stage == "confirmation"
    assert result.error_type == "CommandTimeout"
    assert result.client_termination_requested is True
    assert "private target detail" not in repr(result)
    assert port.calls == ["terminate", ("confirm", TARGET, 30.0)]


@pytest.mark.parametrize("failure", [RdbgDebugUiNotRegistered, ProtocolError])
def test_failed_termination_request_never_claims_target_absence(
    failure: type[Exception],
) -> None:
    contract = _termination_contract()

    class FailedRequestPort(_ServerPort):
        def terminate_bound_server_session(self) -> bool:
            self.calls.append("terminate")
            raise failure("private target detail")

    port = FailedRequestPort(BoundServerTargetAbsence(CLIENT, TARGET, 1.0, 1))

    result = contract.terminate_server_target(port, TARGET)

    assert isinstance(result, contract.TerminationUnknown)
    assert result.expected_target == TARGET
    assert result.stage == "request"
    assert result.error_type == failure.__name__
    assert result.client_termination_requested is None
    assert "private target detail" not in repr(result)
    assert port.calls == ["terminate"]


def test_mismatched_registry_evidence_does_not_confirm_original_target() -> None:
    contract = _termination_contract()
    other = TargetId(UUID(int=13), TARGET.infobase_alias, TARGET.seance_id)
    port = _ServerPort(BoundServerTargetAbsence(CLIENT, other, 1.0, 1))

    result = contract.terminate_server_target(port, TARGET)

    assert isinstance(result, contract.TerminationUnknown)
    assert result.expected_target == TARGET
    assert result.stage == "confirmation"
    assert result.error_type == "EvidenceMismatch"


def test_absence_evidence_for_another_client_session_cannot_confirm_stop() -> None:
    contract = _termination_contract()
    other_client = TargetId(UUID(int=14), CLIENT.infobase_alias, UUID(int=22))
    port = _ServerPort(BoundServerTargetAbsence(other_client, TARGET, 1.0, 1))

    result = contract.terminate_server_target(port, TARGET)

    assert isinstance(result, contract.TerminationUnknown)
    assert result.error_type == "EvidenceMismatch"


@pytest.mark.parametrize("grace_s", [-1.0, float("inf"), float("nan"), True])
def test_invalid_confirmation_grace_is_rejected_before_termination(
    grace_s: object,
) -> None:
    contract = _termination_contract()
    port = _ServerPort(BoundServerTargetAbsence(CLIENT, TARGET, 1.0, 1))

    with pytest.raises(ValueError, match="grace_s"):
        contract.terminate_server_target(port, TARGET, grace_s=grace_s)

    assert port.calls == []
