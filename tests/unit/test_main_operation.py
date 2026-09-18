import pytest

from onec_runtime.execution.main import MainOperation, MainPhase


def test_ambiguous_continue_retains_command_until_confirmed_target_loss() -> None:
    operation = MainOperation(17, None)
    assert operation.phase is MainPhase.ADMITTED
    assert operation.command_dispatch_attempted is False
    operation.continue_requested()
    assert operation.phase is MainPhase.UNKNOWN
    assert operation.command_dispatch_attempted is True
    assert not operation.terminal
    assert operation.command_id == 17
    operation.continue_acknowledged()
    assert operation.phase is MainPhase.RUNNING
    operation.mark_unknown()
    assert not operation.terminal
    operation.mark_lost()
    assert operation.terminal
    assert operation.phase is MainPhase.LOST
    assert operation.command_dispatch_attempted is True
    with pytest.raises(RuntimeError, match="terminal"):
        operation.continue_acknowledged()


def test_settled_completion_cannot_be_replaced_by_late_continue() -> None:
    operation = MainOperation(18, None)
    result = object()
    operation.complete(result)
    with pytest.raises(RuntimeError, match="terminal"):
        operation.continue_requested()
    assert operation.completion is result
    assert operation.phase is MainPhase.COMPLETED
    assert operation.command_dispatch_attempted is False


def test_failure_before_continue_keeps_main_dispatch_proof_false() -> None:
    operation = MainOperation(19, None)
    operation.fail_before_dispatch()
    assert operation.command_dispatch_attempted is False
