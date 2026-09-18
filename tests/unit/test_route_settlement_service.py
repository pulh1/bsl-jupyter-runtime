"""Publication after route admission, including yields before MAIN completion."""

from dataclasses import replace
from uuid import uuid4

import pytest

from onec_runtime.bsl.semantic_lowering import SemanticLoweringResult
from onec_runtime.bsl.notebook_methods import NotebookMethodSet
from onec_runtime.bsl.source_maps import (
    SourceUnitKind, SourceUnitRef, mapped_visible_source, source_sha256,
)
from onec_runtime.execution.capture.policy import CapturePreparedPayload
from onec_runtime.execution.capture.scope import CaptureContextState, CaptureScope
from onec_runtime.execution.common import NotebookCommonParser
from onec_runtime.execution.controller.controller import MainYield, MainYieldKind
from onec_runtime.execution.main import MainOperation, MainPhase
from onec_runtime.execution.main.completion import MainRemoteCompletion
from onec_runtime.execution.main.policy import MainPreparedPayload
from onec_runtime.execution.preparation import RoutePreparedStatement, WorkerCandidateIntent
from onec_runtime.bsl.parser_target import PythonParserTarget
from onec_runtime.rdbg.models import EvaluationResult
from onec_runtime.runtime_api import (
    CaptureCorrelationTicket, OperationState, RuntimeReplyKind,
)

from test_execution_route_sequence import CAPTURE_STOP


class NamespaceStore:
    def __init__(self) -> None:
        self.names: tuple[str, ...] = ()

    def publish_additions(self, names: tuple[str, ...]) -> None:
        seen = {name.casefold() for name in self.names}
        additions = tuple(name for name in names if name.casefold() not in seen)
        self.names += additions


def _payload(source: str, *, capture: bool = False):
    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL, "settlement-cell", 1, source_sha256(source)
    )
    common = NotebookCommonParser(PythonParserTarget.from_generated()).prepare(source, unit)
    mapped = mapped_visible_source(source, unit)
    lowering = SemanticLoweringResult(
        mapped, ("Начисление",), ("Начисление",), ("Начисление",), (), 0, (),
    )
    statement = RoutePreparedStatement(lowering, "__messages")
    if capture:
        return CapturePreparedPayload(common, statement, ("Начисление",))
    return MainPreparedPayload(common, statement)


def _scope(
    operation: MainOperation, local_sequence: int, *, stop=CAPTURE_STOP,
) -> CaptureScope:
    operation.stopped(stop, MainPhase.SUSPENDED_CAPTURE)
    scope = CaptureScope.from_stop(1, operation.command_id, stop, local_sequence)
    scope.record_locals(())
    scope.record_transfer("opaque-context-address")
    scope.record_kernel_frame(2)
    assert scope.record_main_command(operation.command_id)
    scope.record_context_begun()
    scope.mark_ready()
    return scope


def test_main_registration_preserves_source_and_namespace_until_resume_completion():
    from onec_runtime.execution.settlement import RouteSettlementService

    namespace = NamespaceStore()
    service = RouteSettlementService(namespace)
    payload = _payload("Начисление = 1;")
    operation = MainOperation(18, None, message_collector_key="__messages")
    service.register_main(operation, payload, prior_capture_sequence=4)
    scope = _scope(operation, 5)

    stopped = service.settle_main(
        MainYield(MainYieldKind.CAPTURE, operation, scope=scope), payload,
    )
    assert stopped.kind is RuntimeReplyKind.CAPTURED
    assert stopped.stop_sequence == 1
    assert namespace.names == ()
    assert service.pending_main_names(operation) == ("Начисление",)

    operation.continue_requested()
    operation.continue_acknowledged()
    next_scope = _scope(operation, 6)
    stopped_again = service.settle_main(
        MainYield(MainYieldKind.CAPTURE, operation, scope=next_scope), payload,
    )
    assert stopped_again.stop_sequence == 2
    assert namespace.names == ()

    completion = MainRemoteCompletion(True, "", ("done",))
    operation.remote_completed()
    operation.complete(completion)
    completed = service.settle_main(
        MainYield(MainYieldKind.COMPLETED, operation, completion=completion), payload,
    )
    assert completed.kind is RuntimeReplyKind.MAIN_COMPLETED
    assert completed.messages == ("done",)
    assert completed.changed_roots == ("Начисление",)
    assert namespace.names == ("Начисление",)
    assert service.pending_main_names(operation) == ()


def test_next_capture_ticket_rebinds_retained_main_publication_after_stop():
    from onec_runtime.execution.settlement import RouteSettlementService

    namespace = NamespaceStore()
    service = RouteSettlementService(namespace)
    payload = _payload("Начисление = 1;")
    operation = MainOperation(26, None, message_collector_key="__messages")
    first_ticket = CaptureCorrelationTicket("capture_first", 26, 1)
    service.register_main(
        operation, payload, prior_capture_sequence=4, capture_ticket=first_ticket,
    )
    first_scope = _scope(operation, 5)
    first_reply = service.settle_main(
        MainYield(MainYieldKind.CAPTURE, operation, scope=first_scope), payload,
    )
    assert first_reply.capture_ticket == "capture_first"
    assert service.next_capture_stop_sequence(operation) == 2

    next_ticket = CaptureCorrelationTicket("capture_next", 26, 2)
    service.rebind_next_capture_ticket(operation, next_ticket)
    assert service.pending_main_names(operation) == ("Начисление",)
    assert namespace.names == ()

    operation.continue_requested()
    operation.continue_acknowledged()
    next_scope = _scope(operation, 6)
    next_reply = service.settle_main(
        MainYield(MainYieldKind.CAPTURE, operation, scope=next_scope), payload,
    )
    assert next_reply.stop_sequence == 2
    assert next_reply.capture_ticket == "capture_next"
    assert next_reply.changed_roots == ("Начисление",)


def test_next_capture_ticket_rejects_foreign_or_nonsequential_rebind():
    from onec_runtime.execution.settlement import RouteSettlementService

    service = RouteSettlementService(NamespaceStore())
    payload = _payload("Начисление = 1;")
    operation = MainOperation(27, None, message_collector_key="__messages")
    service.register_main(operation, payload, prior_capture_sequence=7)
    next_ticket = CaptureCorrelationTicket("capture_next", 27, 2)
    with pytest.raises(ValueError, match="published CAPTURE stop"):
        service.next_capture_stop_sequence(operation)
    with pytest.raises(ValueError, match="published CAPTURE stop"):
        service.rebind_next_capture_ticket(operation, next_ticket)

    scope = _scope(operation, 8)
    service.settle_main(MainYield(MainYieldKind.CAPTURE, operation, scope=scope), payload)
    impostor = MainOperation(27, None, message_collector_key="__messages")
    with pytest.raises(ValueError, match="MAIN operation"):
        service.next_capture_stop_sequence(impostor)
    with pytest.raises(ValueError, match="MAIN operation"):
        service.rebind_next_capture_ticket(impostor, next_ticket)
    with pytest.raises(ValueError, match="MAIN command"):
        service.rebind_next_capture_ticket(
            operation, CaptureCorrelationTicket("capture_wrong", 28, 2),
        )
    for wrong_stop in (1, 3):
        with pytest.raises(ValueError, match="next CAPTURE stop"):
            service.rebind_next_capture_ticket(
                operation, CaptureCorrelationTicket("capture_wrong", 27, wrong_stop),
            )
    with pytest.raises(TypeError, match="CaptureCorrelationTicket"):
        service.rebind_next_capture_ticket(operation, object())
    with pytest.raises(ValueError, match="ticket ID"):
        service.rebind_next_capture_ticket(
            operation, CaptureCorrelationTicket("", 27, 2),
        )

    operation.continue_requested()
    operation.continue_acknowledged()
    with pytest.raises(ValueError, match="published CAPTURE stop"):
        service.next_capture_stop_sequence(operation)
    with pytest.raises(ValueError, match="published CAPTURE stop"):
        service.rebind_next_capture_ticket(operation, next_ticket)


def test_next_capture_ticket_requires_the_current_stop_to_be_published():
    from onec_runtime.execution.settlement import RouteSettlementService

    service = RouteSettlementService(NamespaceStore())
    payload = _payload("Начисление = 1;")
    operation = MainOperation(29, None, message_collector_key="__messages")
    service.register_main(operation, payload, prior_capture_sequence=4)
    first_scope = _scope(operation, 5)
    service.settle_main(MainYield(MainYieldKind.CAPTURE, operation, scope=first_scope), payload)

    operation.continue_requested()
    operation.continue_acknowledged()
    _scope(operation, 6, stop=replace(CAPTURE_STOP))

    with pytest.raises(ValueError, match="published CAPTURE stop"):
        service.next_capture_stop_sequence(operation)
    with pytest.raises(ValueError, match="published CAPTURE stop"):
        service.rebind_next_capture_ticket(
            operation, CaptureCorrelationTicket("capture_late", 29, 2),
        )


def test_main_bsl_failure_does_not_publish_prepared_namespace():
    from onec_runtime.execution.settlement import RouteSettlementService

    namespace = NamespaceStore()
    service = RouteSettlementService(namespace)
    payload = _payload("Начисление = 1 / 0;")
    operation = MainOperation(19, None, message_collector_key="__messages")
    service.register_main(operation, payload, prior_capture_sequence=0)
    completion = MainRemoteCompletion(None, "division by zero", ())
    operation.remote_completed()
    operation.complete(completion)

    reply = service.settle_main(
        MainYield(MainYieldKind.COMPLETED, operation, completion=completion), payload,
    )
    assert reply.succeeded is False
    assert reply.state is OperationState.FAILED
    assert reply.changed_roots == ("Начисление",)
    assert namespace.names == ()


def test_main_decode_failure_after_matched_command_returns_failed_reply():
    from onec_runtime.execution.reply_publication import MainConfirmedDecodeFailure
    from onec_runtime.execution.settlement import RouteSettlementService

    namespace = NamespaceStore()
    service = RouteSettlementService(namespace)
    payload = _payload("Начисление = 1;")
    operation = MainOperation(23, None, message_collector_key="__messages")
    service.register_main(operation, payload, prior_capture_sequence=0)
    operation.remote_completed()  # Matching command ID is already confirmed.

    reply = service.settle_main(MainConfirmedDecodeFailure(operation), payload)

    assert reply.kind is RuntimeReplyKind.MAIN_COMPLETED
    assert reply.operation_id == 23
    assert reply.state is OperationState.FAILED
    assert reply.succeeded is False
    assert reply.error == "MAIN completion decode failed"
    assert reply.changed_roots == ("Начисление",)
    assert namespace.names == ()
    assert operation.phase is MainPhase.COMPLETED


def test_main_error_after_capture_uses_original_source_diagnostic():
    from onec_runtime.execution.reply_publication import MainConfirmedDecodeFailure
    from onec_runtime.execution.settlement import RouteSettlementService

    source = "Начисление = 1 / 0;"
    payload = _payload(source)
    service = RouteSettlementService(NamespaceStore())
    operation = MainOperation(24, None, message_collector_key="__messages")
    service.register_main(operation, payload, prior_capture_sequence=0)
    scope = _scope(operation, 1)
    service.settle_main(MainYield(MainYieldKind.CAPTURE, operation, scope=scope), payload)
    operation.remote_completed()
    error = f"{{<Неизвестный модуль>(1,{source.index('/') + 1})}}: division by zero"

    reply = service.settle_main(
        MainConfirmedDecodeFailure(operation, remote_error=error), payload,
    )

    assert reply.error == "BSL execution failed"
    assert reply.diagnostic is not None
    assert reply.diagnostic.source_unit == payload.common.source_unit
    assert reply.diagnostic.visible_location.column == source.index("/") + 1


def test_worker_candidate_retains_prior_cell_visible_source_for_diagnostic():
    from onec_runtime.execution.settlement import RouteSettlementService

    prior_source = "Результат = 1 / 0;"
    prior_unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL, "prior-worker-cell", 1,
        source_sha256(prior_source),
    )
    prior_mapped = mapped_visible_source(prior_source, prior_unit)
    current = _payload("Начисление = 2;")
    current_mapped = mapped_visible_source(
        current.common.parsed_units.visible.text, current.common.source_unit,
    )
    method_set = NotebookMethodSet(
        prior_mapped, (), False, (), (prior_mapped, current_mapped),
    )
    intent = WorkerCandidateIntent(
        current_mapped, current.common.parsed_units, (), (), (), object(),
        None, method_set,
    )
    statement = RoutePreparedStatement(
        replace(current.statement.lowering, mapped_source=prior_mapped),
        "__messages",
    )
    payload = MainPreparedPayload(current.common, statement, intent)
    operation = MainOperation(25, None, message_collector_key="__messages")
    service = RouteSettlementService(NamespaceStore())
    service.register_main(operation, payload, prior_capture_sequence=0)
    error = (
        f"{{<Неизвестный модуль>(1,{prior_source.index('/') + 1})}}: "
        "division by zero"
    )
    completion = MainRemoteCompletion(None, error, ())
    operation.remote_completed()
    operation.complete(completion)

    reply = service.settle_main(
        MainYield(MainYieldKind.COMPLETED, operation, completion=completion), payload,
    )

    assert reply.diagnostic is not None
    assert reply.diagnostic.source_unit == prior_unit
    assert reply.diagnostic.visible_location.column == prior_source.index("/") + 1


def test_capture_failure_does_not_publish_names_or_close_confirmed_scope():
    from onec_runtime.execution.reply_publication import CaptureRemoteOutcome
    from onec_runtime.execution.settlement import RouteSettlementService

    namespace = NamespaceStore()
    service = RouteSettlementService(namespace)
    payload = _payload("Начисление = 1 / 0;", capture=True)
    operation = MainOperation(20, None)
    scope = _scope(operation, 1)
    service.register_capture(scope, payload)

    failed = service.settle_capture(
        CaptureRemoteOutcome(
            EvaluationResult(uuid4(), "Ошибка", "", True, "division by zero"),
            ("notice",),
        ),
        payload,
    )
    assert failed.kind is RuntimeReplyKind.CAPTURE_CELL
    assert failed.succeeded is False
    assert failed.messages == ("notice",)
    assert namespace.names == ()
    assert scope.context_state is CaptureContextState.READY

    corrected = _payload("Начисление = 2;", capture=True)
    service.register_capture(scope, corrected)
    succeeded = service.settle_capture(
        CaptureRemoteOutcome(EvaluationResult(uuid4(), "Число", "2", False)),
        corrected,
    )
    assert succeeded.succeeded is True
    assert namespace.names == ("Начисление",)
    assert scope.context_state is CaptureContextState.READY


def test_capture_rejects_another_scope_and_missing_remote_messages():
    from onec_runtime.execution.settlement import RouteSettlementService

    service = RouteSettlementService(NamespaceStore())
    payload = _payload("Начисление = 2;", capture=True)
    operation = MainOperation(21, None)
    scope = _scope(operation, 1)
    service.register_capture(scope, payload)
    another = CaptureScope.from_stop(1, operation.command_id, CAPTURE_STOP, 2)
    with pytest.raises(ValueError, match="scope"):
        service.register_capture(another, payload)
    with pytest.raises(TypeError, match="CaptureRemoteOutcome"):
        service.settle_capture(EvaluationResult(uuid4(), "Число", "2", False), payload)


def test_worker_only_requires_confirmed_published_handle():
    from onec_runtime.execution.settlement import RouteSettlementService, WorkerPublished

    service = RouteSettlementService(NamespaceStore())
    payload = MainPreparedPayload(_payload("Начисление = 1;").common, None, object())
    with pytest.raises(TypeError, match="WorkerPublished"):
        service.settle_main(None, payload)
    handle = object()
    reply = service.settle_main(
        WorkerPublished(handle, operation_id=22, state=OperationState.IDLE), payload,
    )
    assert reply.kind is RuntimeReplyKind.WORKER_LOADED
    assert reply.result is handle
