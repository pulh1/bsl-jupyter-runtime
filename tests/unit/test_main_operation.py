import pytest

from onec_runtime.execution.main import MainOperation, MainPhase


def test_ambiguous_continue_retains_command_until_confirmed_target_loss() -> None:
    operation = MainOperation(17, None)
    assert operation.phase is MainPhase.ADMITTED
    operation.continue_requested()
    assert operation.phase is MainPhase.UNKNOWN
    assert not operation.terminal
    assert operation.command_id == 17
    operation.continue_acknowledged()
    assert operation.phase is MainPhase.RUNNING
    operation.mark_unknown()
    assert not operation.terminal
    operation.mark_lost()
    assert operation.terminal
    assert operation.phase is MainPhase.LOST
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
