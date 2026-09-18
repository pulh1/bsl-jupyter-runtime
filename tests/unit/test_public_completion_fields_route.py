"""Completion fields reach the new stopped-route owner through the facade."""

from contextlib import contextmanager
from types import SimpleNamespace
from uuid import UUID

import pytest

from onec_runtime.errors import ProtocolError
from onec_runtime.execution.arbiter import OutcomeUnknown
from onec_runtime.execution.capture.writeback import (
    CaptureExportFailed, CaptureModifyFailed,
)
from onec_runtime.execution.public_facade import PublicExecutionFacade
from onec_runtime.execution.worker_activation import WorkerMaterializationSnapshot
from onec_runtime.prototype_runtime import PartialWritebackError
from onec_runtime.rdbg.models import EvaluationResult, ModifyResult
from onec_runtime.runtime_api import RuntimeNamespaceSnapshot

from test_public_execution_facade import _Arbiter, _Controller, _Pipeline, unit


def test_public_facade_binds_completion_service_and_caller_wait_handoff() -> None:
    in_handoff = [False]

    class Ticket:
        def wait_initiator(self, timeout=None):
            assert in_handoff[0]
            assert timeout == 0.25
            return ("Номер", "Название")

    class Controller:
        def submit_completion_helper(self, plan):
            assert "e1cRuntimeКонтекст.Данные" in plan.expression
            return Ticket()

    api = PublicExecutionFacade(
        _Pipeline(), Controller(), _Arbiter(),
        source_unit_factory=unit,
        status_reader=lambda: SimpleNamespace(),
        namespace_reader=lambda: RuntimeNamespaceSnapshot(1, 2, ("Данные",)),
        worker_catalog_snapshot=lambda: WorkerMaterializationSnapshot(0, ()),
    )

    @contextmanager
    def handoff():
        in_handoff[0] = True
        try:
            yield
        finally:
            in_handoff[0] = False

    with api.execution_caller_handoff(handoff):
        assert api.completion_fields("e1cRuntimeКонтекст.Данные", timeout_s=0.25) == (
            "Номер", "Название",
        )
    assert in_handoff == [False]


def test_public_facade_rejects_completion_when_catalog_binding_is_absent() -> None:
    api = PublicExecutionFacade(
        _Pipeline(), object(), _Arbiter(),
        source_unit_factory=unit,
        status_reader=lambda: SimpleNamespace(),
        namespace_reader=lambda: RuntimeNamespaceSnapshot(1, 2, ("Данные",)),
    )

    with pytest.raises(ProtocolError, match="completion fields route"):
        api.completion_fields("e1cRuntimeКонтекст.Данные")


@pytest.mark.parametrize("failure", [
    CaptureExportFailed(
        "private export error",
        EvaluationResult(UUID(int=1), "Строка", "private value", True),
    ),
    CaptureModifyFailed(ModifyResult(UUID(int=2), "Строка", "private value", True)),
])
def test_resume_translates_confirmed_root_write_failure_for_mcp(failure) -> None:
    class FailedTicket:
        def wait_initiator(self, timeout=None):
            raise failure

    controller = _Controller()
    controller.resume = FailedTicket()
    api = PublicExecutionFacade(
        _Pipeline(), controller, _Arbiter(),
        source_unit_factory=unit, status_reader=lambda: SimpleNamespace(),
    )
    callbacks = []

    with pytest.raises(PartialWritebackError) as caught:
        api.resume_capture(on_completion=lambda reply, error: callbacks.append(error))
    assert "private" not in str(caught.value)
    assert len(callbacks) == 1
    assert isinstance(callbacks[0], PartialWritebackError)
    assert "private" not in str(callbacks[0])


def test_resume_keeps_unknown_writeback_outcome_owned() -> None:
    failure = OutcomeUnknown("remote outcome unproven")

    class UnknownTicket:
        def wait_initiator(self, timeout=None):
            raise failure

    controller = _Controller()
    controller.resume = UnknownTicket()
    api = PublicExecutionFacade(
        _Pipeline(), controller, _Arbiter(),
        source_unit_factory=unit, status_reader=lambda: SimpleNamespace(),
    )

    with pytest.raises(OutcomeUnknown) as caught:
        api.resume_capture()
    assert caught.value is failure
