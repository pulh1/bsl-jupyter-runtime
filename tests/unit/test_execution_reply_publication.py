"""Public replies from the new route outcomes, without debugger I/O."""

import subprocess
import sys
from uuid import uuid4

import pytest

from onec_runtime.bsl.diagnostics import VisibleSourceContext
from onec_runtime.bsl.source_maps import (
    SourceUnitKind, SourceUnitRef, mapped_visible_source, source_sha256,
)
from onec_runtime.execution.capture.scope import (
    CaptureContextState, CaptureFrameIdentity, CaptureScope,
)
from onec_runtime.execution.controller.controller import MainYield, MainYieldKind
from onec_runtime.execution.main import MainOperation, MainPhase
from onec_runtime.execution.main.completion import MainRemoteCompletion
from onec_runtime.rdbg.models import EvaluationResult
from onec_runtime.runtime_api import (
    CaptureCorrelationTicket, OperationState, RuntimeReplyKind,
)

from test_execution_route_sequence import CAPTURE_STOP


def _publication_module():
    from onec_runtime.execution import reply_publication

    return reply_publication


def _source(source: str):
    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL, "publication-cell", 1,
        source_sha256(source),
    )
    return mapped_visible_source(source, unit), VisibleSourceContext({unit: source}), unit


def test_publication_import_does_not_require_completed_runtime_api_contracts():
    """RuntimeApi may eventually import the policy before declaring RuntimeReply."""
    script = (
        "import sys, types\n"
        "sys.modules['onec_runtime.runtime_api'] = types.ModuleType('onec_runtime.runtime_api')\n"
        "from onec_runtime.execution import reply_publication\n"
        "assert reply_publication.MainReplyPolicy\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def _ready_scope(operation: MainOperation, *, local_sequence: int) -> CaptureScope:
    operation.stopped(CAPTURE_STOP, MainPhase.SUSPENDED_CAPTURE)
    scope = CaptureScope.from_stop(1, operation.command_id, CAPTURE_STOP, local_sequence)
    scope.record_locals(())
    scope.record_transfer("opaque-context-address")
    scope.record_kernel_frame(2)
    assert scope.record_main_command(operation.command_id)
    scope.record_context_begun()
    scope.mark_ready()
    return scope


@pytest.mark.parametrize(
    ("remote_error", "want_state", "want_succeeded", "want_error"),
    [
        ("", OperationState.COMPLETED, True, ""),
        ("planned BSL failure", OperationState.FAILED, False, "BSL execution failed"),
    ],
)
def test_main_terminal_reply_uses_remote_error_without_reopening_command(
    remote_error, want_state, want_succeeded, want_error,
):
    publication = _publication_module()
    operation = MainOperation(7, None)
    record = publication.MainPublicationRecord(operation, prior_capture_sequence=0)
    completion = MainRemoteCompletion(42, remote_error, ("notice",))
    operation.remote_completed()
    operation.complete(completion)

    reply = publication.MainReplyPolicy().publish(
        MainYield(MainYieldKind.COMPLETED, operation, completion=completion), record,
    )

    assert reply.kind is RuntimeReplyKind.MAIN_COMPLETED
    assert reply.operation_id == 7
    assert reply.state is want_state
    assert reply.succeeded is want_succeeded
    assert reply.result == 42
    assert reply.error == want_error
    assert reply.messages == ("notice",)
    assert operation.phase is MainPhase.COMPLETED


def test_main_publication_record_survives_capture_stop_until_later_completion():
    publication = _publication_module()
    operation = MainOperation(9, None)
    scope = _ready_scope(operation, local_sequence=3)
    ticket = CaptureCorrelationTicket("capture_expected", 9, 1)
    record = publication.MainPublicationRecord(
        operation, prior_capture_sequence=2, capture_ticket=ticket,
        message_collector_key="__cell_messages",
    )
    policy = publication.MainReplyPolicy()

    stopped = policy.publish(MainYield(MainYieldKind.CAPTURE, operation, scope=scope), record)
    assert stopped.kind is RuntimeReplyKind.CAPTURED
    assert stopped.state is OperationState.CAPTURED
    assert stopped.location == CAPTURE_STOP.location
    assert stopped.stop_sequence == 1
    assert stopped.observed_command_id == 9
    assert stopped.capture_ticket == "capture_expected"
    assert operation.phase is MainPhase.SUSPENDED_CAPTURE

    completion = MainRemoteCompletion(True, "", ("message after resume",))
    operation.remote_completed()
    operation.complete(completion)
    completed = policy.publish(
        MainYield(MainYieldKind.COMPLETED, operation, completion=completion), record,
    )
    assert completed.kind is RuntimeReplyKind.MAIN_COMPLETED
    assert completed.operation_id == stopped.operation_id == 9
    assert completed.result is True
    assert completed.messages == ("message after resume",)
    assert record.message_collector_key == "__cell_messages"


def test_capture_ticket_is_not_attached_to_another_stop_sequence():
    publication = _publication_module()
    operation = MainOperation(9, None)
    scope = _ready_scope(operation, local_sequence=3)
    record = publication.MainPublicationRecord(
        operation, prior_capture_sequence=2,
        capture_ticket=CaptureCorrelationTicket("capture_later", 9, 2),
    )

    reply = publication.MainReplyPolicy().publish(
        MainYield(MainYieldKind.CAPTURE, operation, scope=scope), record,
    )

    assert reply.stop_sequence == 1
    assert reply.capture_ticket is None


def test_main_user_breakpoint_reply_uses_confirmed_stop_location():
    publication = _publication_module()
    operation = MainOperation(13, None)
    operation.stopped(CAPTURE_STOP, MainPhase.SUSPENDED_USER)
    record = publication.MainPublicationRecord(operation, prior_capture_sequence=0)

    reply = publication.MainReplyPolicy().publish(
        MainYield(MainYieldKind.DEBUG_STOP, operation, stop=CAPTURE_STOP), record,
    )

    assert reply.kind is RuntimeReplyKind.DEBUG_STOPPED
    assert reply.operation_id == 13
    assert reply.state is OperationState.DEBUG_STOPPED
    assert reply.location == CAPTURE_STOP.location
    assert reply.debug_stop is None  # Worker source binding belongs to another port.
    assert operation.phase is MainPhase.SUSPENDED_USER


def test_main_error_diagnostic_maps_to_its_saved_visible_source():
    publication = _publication_module()
    source = "Результат = 1 / 0;"
    mapped, visible, unit = _source(source)
    operation = MainOperation(11, None)
    record = publication.MainPublicationRecord(
        operation, prior_capture_sequence=0,
        executed_source=mapped, visible_source_context=visible,
    )
    error = f"{{<Неизвестный модуль>(1,{source.index('/') + 1})}}: division by zero"
    completion = MainRemoteCompletion(None, error, ())
    operation.remote_completed()
    operation.complete(completion)

    reply = publication.MainReplyPolicy().publish(
        MainYield(MainYieldKind.COMPLETED, operation, completion=completion), record,
    )

    assert reply.error == "BSL execution failed"
    assert reply.diagnostic is not None
    assert reply.diagnostic.source_unit == unit
    assert reply.diagnostic.visible_location is not None
    assert reply.diagnostic.visible_location.column == source.index("/") + 1


@pytest.mark.parametrize(
    ("error_occurred", "want_succeeded", "want_result", "want_error"),
    [
        (False, True, True, ""),
        (True, False, None, "BSL execution failed"),
    ],
)
def test_capture_eval_reply_preserves_ready_scope_on_bsl_error(
    error_occurred, want_succeeded, want_result, want_error,
):
    publication = _publication_module()
    operation = MainOperation(5, None)
    scope = _ready_scope(operation, local_sequence=1)
    scope.admit_cell_dirty_roots(("Сумма",))
    record = publication.CapturePublicationRecord(
        scope, changed_roots=("Сумма",), capture_dirty_roots=("Сумма",),
    )
    remote = publication.CaptureRemoteOutcome(
        EvaluationResult(
            uuid4(), "Булево", "Истина", error_occurred,
            "planned BSL failure" if error_occurred else "",
        ),
        messages=("notice",),
    )

    reply = publication.CaptureReplyPolicy().publish(remote, record)

    assert reply.kind is RuntimeReplyKind.CAPTURE_CELL
    assert reply.operation_id == 5
    assert reply.state is OperationState.CAPTURED
    assert reply.succeeded is want_succeeded
    assert reply.result is want_result
    assert reply.error == want_error
    assert reply.messages == ("notice",)
    assert reply.changed_roots == ("Сумма",)
    assert reply.capture_dirty_roots == ("Сумма",)
    assert scope.context_state is CaptureContextState.READY
    assert scope.frame_identity is CaptureFrameIdentity.CONFIRMED
    assert scope.dirty_roots == ("Сумма",)


def test_capture_bsl_error_diagnostic_uses_its_own_cell_source():
    publication = _publication_module()
    source = "Сумма = 1 / 0;"
    mapped, visible, unit = _source(source)
    operation = MainOperation(5, None)
    scope = _ready_scope(operation, local_sequence=1)
    record = publication.CapturePublicationRecord(
        scope, executed_source=mapped, visible_source_context=visible,
    )
    error = f"{{<Неизвестный модуль>(1,{source.index('/') + 1})}}: division by zero"
    remote = publication.CaptureRemoteOutcome(
        EvaluationResult(uuid4(), "Ошибка", "", True, error),
    )

    reply = publication.CaptureReplyPolicy().publish(remote, record)

    assert reply.succeeded is False
    assert reply.diagnostic is not None
    assert reply.diagnostic.source_unit == unit
    assert reply.diagnostic.visible_location is not None
    assert reply.diagnostic.visible_location.column == source.index("/") + 1
    assert scope.context_state is CaptureContextState.READY


def test_capture_result_decode_error_is_a_failed_cell_not_a_lost_scope():
    publication = _publication_module()
    operation = MainOperation(5, None)
    scope = _ready_scope(operation, local_sequence=1)
    record = publication.CapturePublicationRecord(scope)
    remote = publication.CaptureRemoteOutcome(
        EvaluationResult(uuid4(), "Число", "private-invalid-number", False),
    )

    reply = publication.CaptureReplyPolicy().publish(remote, record)

    assert reply.kind is RuntimeReplyKind.CAPTURE_CELL
    assert reply.succeeded is False
    assert reply.result is None
    assert reply.error == "CAPTURE result decode failed"
    assert "private-invalid-number" not in repr(reply)
    assert scope.context_state is CaptureContextState.READY
    assert scope.frame_identity is CaptureFrameIdentity.CONFIRMED
