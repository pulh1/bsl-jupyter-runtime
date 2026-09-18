"""Controller-selected statement preparation has one fenced admission path."""

from dataclasses import replace
from threading import Event, Thread, get_ident
from uuid import UUID

import pytest
from arbiter_test_cleanup import confirm_test_server_terminated

from onec_runtime.bsl.parser_target import PythonParserTarget
from onec_runtime.bsl.source_maps import SourceUnitKind, SourceUnitRef, source_sha256
from onec_runtime.execution.arbiter import (
    ArbiterBusy, CancelledBeforeEffect, OutcomeUnknown, RdbgArbiter, ReadyForPolicy,
    RouteToken, Settlement, WaiterDetached,
)
from onec_runtime.errors import EvaluationDispatchUnknown
from onec_runtime.execution.capture.cell_evaluator import CaptureCellEvaluator
from onec_runtime.execution.capture.executor import CaptureExecutor
from onec_runtime.execution.capture.policy import CaptureCellPolicy
from onec_runtime.execution.capture.scope import CaptureContextState
from onec_runtime.execution.common import NotebookCommonParser
from onec_runtime.execution.contracts import (
    Accepted, Current, PreparedCell, Rejected, SourceDiagnostic,
    StalePreparation, SubmissionReceipt, Unavailable,
)
from onec_runtime.execution.main import MainExecutor, MainPhase
from onec_runtime.execution.main.policy import MainCellPolicy
from onec_runtime.execution.snapshot_binding import RoutePreparationSnapshot
from onec_runtime.execution.worker import WorkerActivationUnknown
from onec_runtime.rdbg.models import EvaluationResult, ModifyResult
from onec_runtime.stop_routing import BreakpointRegistry
from onec_runtime.table_value import evaluation_to_python

from test_execution_controller_routes import BUSINESS, CompleteSession, KERNEL, TARGET


def _common(source: str, parser: PythonParserTarget):
    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL, "controller-route", 1, source_sha256(source)
    )
    result = NotebookCommonParser(parser).prepare(source, unit)
    assert not isinstance(result, SourceDiagnostic)
    return result


def _runtime(snapshot_provider, *, session=None, settlement_services=None, worker_activation=None,
             capture_snapshot_provider=None):
    from onec_runtime.execution.controller.controller import ExecutionController

    session = session or CompleteSession()
    arbiter = RdbgArbiter(session, RouteToken("runtime-1", 1, 0, "main"))
    parser = PythonParserTarget.from_generated()
    kwargs = {} if worker_activation is None else {"worker_activation": worker_activation}
    if capture_snapshot_provider is not None:
        kwargs["capture_snapshot_provider"] = capture_snapshot_provider
    controller = ExecutionController(
        arbiter,
        MainExecutor(poll_interval_s=0.1),
        CaptureExecutor(None, KERNEL, decode_command_id=evaluation_to_python),
        CaptureCellEvaluator(),
        BreakpointRegistry(KERNEL, (BUSINESS,)),
        runtime_generation=1,
        parser_target=parser,
        snapshot_provider=snapshot_provider,
        settlement_services=settlement_services,
        **kwargs,
    )
    return controller, arbiter, session, parser


def test_capture_preparation_uses_suspended_main_speculative_namespace() -> None:
    owner = object()
    captured_operations = []

    def capture_snapshot(operation):
        captured_operations.append(operation)
        return RoutePreparationSnapshot(owner, 2, ("ИзMain",), ())

    controller, arbiter, _session, _parser = _runtime(
        lambda: RoutePreparationSnapshot(owner, 1, (), ()),
        capture_snapshot_provider=capture_snapshot,
    )
    try:
        controller.submit_main("Результат = 1;").wait_settled(3)
        context = controller.await_preparation_context()
        assert context.capabilities.namespace_names == ("ИзMain",)
        assert captured_operations == [controller.main_operation]
    finally:
        arbiter.close(timeout=3)


def test_capture_preparation_rejects_changed_speculative_namespace() -> None:
    owner = object()
    pending = [("ИзMain",)]
    controller, arbiter, _session, parser = _runtime(
        lambda: RoutePreparationSnapshot(owner, 1, (), ()),
        capture_snapshot_provider=lambda _operation: RoutePreparationSnapshot(
            owner, 2 if pending[0] == ("ИзMain",) else 3, pending[0], (),
        ),
    )
    try:
        controller.submit_main("Результат = 1;").wait_settled(3)
        context, _prepared_cell = _prepared(controller, parser, "Результат = 2;")
        pending[0] = ("ДругоеИмя",)

        result = controller.validate_preparation(
            context, context.capabilities.for_pipeline().guards,
        )

        assert isinstance(result, StalePreparation)
    finally:
        arbiter.close(timeout=3)


def test_capture_points_can_be_configured_before_first_main_dispatch() -> None:
    owner = object()
    controller, arbiter, _session, _parser = _runtime(
        lambda: RoutePreparationSnapshot(owner, 1, (), ()),
    )
    try:
        controller.configure_capture_points((BUSINESS,))
        assert controller._registry.captures == (BUSINESS,)
    finally:
        arbiter.close(timeout=3)


def test_controller_status_facts_keep_confirmed_capture_after_cell_failure() -> None:
    from onec_runtime.execution.status_projection import ExecutionActivity
    from onec_runtime.execution.capture.scope import CaptureFrameIdentity

    class BslErrorSession(CompleteSession):
        def wait_evaluation_event(self, pending, *, timeout_s, on_transport_dispatch):
            if self.expression.startswith(
                "RuntimeKernelServer.ВыполнитьКодВКонтекстеОтладки("
            ):
                on_transport_dispatch()
                self._record("wait_eval")
                self.pending = None
                return EvaluationResult(
                    pending.result_id, "Ошибка", "", True, "planned BSL error"
                )
            return super().wait_evaluation_event(
                pending, timeout_s=timeout_s,
                on_transport_dispatch=on_transport_dispatch,
            )

    owner = object()
    controller, arbiter, _session, _parser = _runtime(
        lambda: RoutePreparationSnapshot(owner, 1, (), ()),
        session=BslErrorSession(),
    )
    try:
        initial = controller.status_facts()
        assert initial.command_id == 0
        assert initial.main_phase is None

        controller.submit_main("Результат = 1;").wait_settled(3)
        stopped = controller.status_facts()
        assert stopped.command_id == 1
        assert stopped.main_phase is MainPhase.SUSPENDED_CAPTURE
        assert stopped.capture_frame_identity is CaptureFrameIdentity.CONFIRMED
        assert stopped.activity is ExecutionActivity.NONE

        result = controller.submit_capture_cell("РезультатИнструкции = 2;").wait_settled(3)
        assert result.error_occurred
        after_error = controller.status_facts()
        assert after_error.main_phase is MainPhase.SUSPENDED_CAPTURE
        assert after_error.capture_frame_identity is CaptureFrameIdentity.CONFIRMED
        assert after_error.activity is ExecutionActivity.NONE
    finally:
        arbiter.close(timeout=3)


def _prepared(controller, parser, source: str):
    context = controller.await_preparation_context()
    common = _common(source, parser)
    prepared = context.policy.prepare(common, context.capabilities.for_pipeline(), context)
    assert isinstance(prepared, PreparedCell)
    return context, prepared


def _previous_methods(parser, result: int):
    from onec_runtime.bsl.notebook_cells import split_notebook_cell
    from onec_runtime.bsl.notebook_methods import merge_notebook_methods

    source = f"Функция Старый() Экспорт\nВозврат {result};\nКонецФункции"
    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL, "previous-method", 1,
        source_sha256(source),
    )
    return merge_notebook_methods(
        None, split_notebook_cell(parser, source, source_unit=unit)
    )


def test_main_policy_settles_once_on_arbiter_after_capture_setup() -> None:
    owner = object()
    entered = Event()
    release = Event()
    observed = []
    policy_calls = []

    class Services:
        def settle_main(self, outcome, payload):
            observed.append((outcome, payload, get_ident()))
            assert outcome.kind.value == "capture"
            assert outcome.scope.published
            entered.set()
            assert release.wait(3)
            return "main-policy-reply"

    services = Services()
    controller, arbiter, session, parser = _runtime(
        lambda: RoutePreparationSnapshot(owner, 1, (), ()),
        settlement_services=services,
    )
    try:
        context, prepared = _prepared(controller, parser, "Результат = 1;")
        original_settle = context.policy.settle

        def settle(outcome, submitted, services):
            policy_calls.append((outcome, submitted, services, get_ident()))
            return original_settle(outcome, submitted, services)

        context.policy.settle = settle
        ticket = controller.submit_cell(
            context, prepared, context.capabilities.for_pipeline().guards,
            SubmissionReceipt(),
        ).ticket
        assert entered.wait(3)
        assert not ticket.status().settled
        assert [name for name, _ in session.calls].count("locals") >= 1
        ticket.detach_waiter()
        with pytest.raises(WaiterDetached):
            ticket.wait_initiator(3)
        release.set()
        assert ticket.wait_settled(3) == "main-policy-reply"
        assert len(observed) == 1
        assert len(policy_calls) == 1
        assert policy_calls[0][1] is prepared
        assert policy_calls[0][2] is services
        assert policy_calls[0][3] == session.calls[0][1].ident
        assert observed[0][1] is prepared.payload
        assert observed[0][2] == session.calls[0][1].ident
    finally:
        release.set()
        arbiter.close(timeout=3)


def test_capture_policy_settles_confirmed_bsl_error_after_restore_when_waiter_detaches() -> None:
    class BslErrorSession(CompleteSession):
        def wait_evaluation_event(self, pending, *, timeout_s, on_transport_dispatch):
            if self.expression.startswith("RuntimeKernelServer.ВыполнитьКодВКонтекстеОтладки("):
                on_transport_dispatch()
                self._record("wait_eval")
                self.pending = None
                return EvaluationResult(pending.result_id, "Ошибка", "", True, "planned BSL error")
            return super().wait_evaluation_event(
                pending, timeout_s=timeout_s, on_transport_dispatch=on_transport_dispatch,
            )

    owner = object()
    observed = []
    policy_calls = []
    entered = Event()
    release = Event()
    session = BslErrorSession()

    class Services:
        def settle_capture(self, outcome, payload):
            observed.append((outcome, payload, get_ident(), len(session.calls)))
            assert outcome.error_occurred
            assert [name for name, _ in session.calls][-1] == "set_breakpoints"
            entered.set()
            assert release.wait(3)
            return "bsl-error-reply"

    services = Services()
    controller, arbiter, _session, parser = _runtime(
        lambda: RoutePreparationSnapshot(owner, 1, (), ()),
        session=session,
        settlement_services=services,
    )
    try:
        assert controller.submit_main("Результат = 1;").wait_settled(3).kind.value == "capture"
        scope = controller.capture_scope
        context, prepared = _prepared(controller, parser, "Результат = 2;")
        original_settle = context.policy.settle

        def settle(outcome, submitted, services):
            policy_calls.append((outcome, submitted, services, get_ident()))
            return original_settle(outcome, submitted, services)

        context.policy.settle = settle
        ticket = controller.submit_cell(
            context, prepared, context.capabilities.for_pipeline().guards,
            SubmissionReceipt(),
        ).ticket
        assert entered.wait(3)
        assert not ticket.status().settled
        ticket.detach_waiter()
        with pytest.raises(WaiterDetached):
            ticket.wait_initiator(3)
        release.set()
        assert ticket.wait_settled(3) == "bsl-error-reply"
        assert len(observed) == 1
        assert len(policy_calls) == 1
        assert policy_calls[0][1] is prepared
        assert policy_calls[0][2] is services
        assert policy_calls[0][3] == session.calls[0][1].ident
        assert observed[0][1] is prepared.payload
        assert observed[0][2] == session.calls[0][1].ident
        assert controller.capture_scope is scope
        assert scope.published
        assert controller.submit_capture_cell("Результат = 3;").wait_settled(3).error_occurred
        assert len(observed) == 1
    finally:
        release.set()
        arbiter.close(timeout=3)


def test_capture_bsl_error_survives_confirmed_restore_repair_on_same_ticket() -> None:
    class RestoreRejectedOnce(CompleteSession):
        def __init__(self) -> None:
            super().__init__()
            self.restore_rejected = False
            self.user_evals = 0

        def start_evaluation(self, expression, **kwargs):
            if expression.startswith("RuntimeKernelServer.ВыполнитьКодВКонтекстеОтладки("):
                self.user_evals += 1
            return super().start_evaluation(expression, **kwargs)

        def wait_evaluation_event(self, pending, *, timeout_s, on_transport_dispatch):
            if self.expression.startswith("RuntimeKernelServer.ВыполнитьКодВКонтекстеОтладки("):
                on_transport_dispatch()
                self._record("wait_eval")
                self.pending = None
                return EvaluationResult(
                    pending.result_id, "Ошибка", "", True, "planned BSL error"
                )
            return super().wait_evaluation_event(
                pending, timeout_s=timeout_s, on_transport_dispatch=on_transport_dispatch,
            )

        def set_breakpoints(self, locations, *, on_transport_dispatch):
            if (
                tuple(locations) == (KERNEL, BUSINESS)
                and self.expression.startswith("RuntimeKernelServer.ВыполнитьКодВКонтекстеОтладки(")
                and not self.restore_rejected
            ):
                self.restore_rejected = True
                raise ValueError("restore rejected before transport")
            return super().set_breakpoints(
                locations, on_transport_dispatch=on_transport_dispatch
            )

    owner = object()
    session = RestoreRejectedOnce()
    replies = []

    class Services:
        def settle_capture(self, outcome, _payload):
            replies.append(outcome)
            return "bsl-error-reply"

    controller, arbiter, _session, parser = _runtime(
        lambda: RoutePreparationSnapshot(owner, 1, (), ()),
        session=session, settlement_services=Services(),
    )
    try:
        controller.submit_main("Результат = 1;").wait_settled(3)
        scope = controller.capture_scope
        context, prepared = _prepared(controller, parser, "Результат = 2;")
        ticket = controller.submit_cell(
            context, prepared, context.capabilities.for_pipeline().guards,
            SubmissionReceipt(),
        ).ticket
        assert ticket.wait_unknown(3)
        assert session.user_evals == 1
        assert replies == []

        controller.repair_capture_cell_after_restore(ticket)
        assert ticket.wait_settled(3) == "bsl-error-reply"
        assert len(replies) == 1 and replies[0].error_occurred
        assert session.user_evals == 1
        assert controller.capture_scope is scope
        assert scope.published
    finally:
        if arbiter.active_ticket is None:
            arbiter.close(timeout=3)


def test_capture_policy_reconciles_pending_eval_then_restores_workspace_once() -> None:
    class AmbiguousCaptureSession(CompleteSession):
        def __init__(self):
            super().__init__()
            self.ambiguous_once = True

        def start_evaluation(self, expression, **kwargs):
            pending = super().start_evaluation(expression, **kwargs)
            if (
                self.ambiguous_once
                and expression.startswith("RuntimeKernelServer.ВыполнитьКодВКонтекстеОтладки(")
            ):
                self.ambiguous_once = False
                raise EvaluationDispatchUnknown(pending)
            return pending

    session = AmbiguousCaptureSession()
    object_owner = object()
    settlements = []

    class Services:
        def settle_capture(self, outcome, payload):
            settlements.append((outcome, payload, tuple(name for name, _ in session.calls)))
            return "capture-policy-reply"

    controller, arbiter, _session, parser = _runtime(
        lambda: RoutePreparationSnapshot(object_owner, 1, (), ()),
        session=session,
        settlement_services=Services(),
    )
    try:
        controller.submit_main("Результат = 1;").wait_settled(3)
        scope = controller.capture_scope
        context, prepared = _prepared(controller, parser, "Результат = 2;")
        ticket = controller.submit_cell(
            context, prepared, context.capabilities.for_pipeline().guards,
            SubmissionReceipt(),
        ).ticket
        assert ticket.wait_unknown(3)
        assert settlements == []
        pending = ticket.status().pending_capability
        assert pending is session.pending
        starts_before_reconcile = sum(name == "start_eval" for name, _ in session.calls)
        ticket.detach_waiter()
        with pytest.raises(WaiterDetached):
            ticket.wait_initiator(3)

        controller.reconcile_capture_pending_eval(ticket)
        assert ticket.wait_settled(3) == "capture-policy-reply"
        assert len(settlements) == 1
        assert settlements[0][0].error_occurred is False
        assert settlements[0][1] is prepared.payload
        assert settlements[0][2][-1] == "set_breakpoints"
        assert sum(name == "start_eval" for name, _ in session.calls) == starts_before_reconcile
        assert controller.capture_scope is scope
        assert scope.published
    finally:
        if 'ticket' in locals() and ticket.status().phase == 'unknown':
            confirm_test_server_terminated(
                arbiter, ticket, arbiter.current_route, session,
                (ticket.status().pending_capability or session.target).target_id,
            )
        arbiter.close(timeout=3)


def test_policy_does_not_settle_rejected_or_cancelled_before_effect_cell() -> None:
    owner = object()
    version = [1]
    observed = []

    class Services:
        def settle_main(self, outcome, payload):
            observed.append((outcome, payload))
            return outcome

    controller, arbiter, session, parser = _runtime(
        lambda: RoutePreparationSnapshot(owner, version[0], (), ()),
        settlement_services=Services(),
    )
    try:
        stale_context, stale_prepared = _prepared(controller, parser, "Результат = 1;")
        version[0] = 2
        rejected = controller.submit_cell(
            stale_context, stale_prepared,
            stale_context.capabilities.for_pipeline().guards, SubmissionReceipt(),
        )
        assert isinstance(rejected, Rejected)
        assert observed == []

        context, prepared = _prepared(controller, parser, "Результат = 1;")
        original_dispatch = arbiter.dispatch
        arbiter.dispatch = lambda _ticket: None
        receipt = SubmissionReceipt()
        try:
            accepted = controller.submit_cell(
                context, prepared, context.capabilities.for_pipeline().guards, receipt,
            )
        finally:
            arbiter.dispatch = original_dispatch
        assert accepted.ticket.status().phase == "queued"
        assert accepted.ticket.cancel_queued()
        with pytest.raises(CancelledBeforeEffect):
            accepted.ticket.wait_settled(3)
        assert observed == []
        assert session.calls == []
    finally:
        arbiter.close(timeout=3)


@pytest.mark.parametrize("capture_count", [1, 2])
def test_main_policy_and_message_key_survive_capture_until_resumed_completion(
    capture_count: int,
) -> None:
    from onec_runtime.execution.reply_publication import (
        MainPublicationRecord, MainReplyPolicy,
    )
    from onec_runtime.runtime_api import RuntimeReplyKind

    class MessageSession(CompleteSession):
        def __init__(self) -> None:
            super().__init__(capture_count=capture_count)
            self.message_reads: list[str] = []

        def wait_evaluation_event(self, pending, *, timeout_s, on_transport_dispatch):
            if self.expression.startswith(
                "RuntimeKernelServer.ЗабратьСообщенияЯчейкиИзКонтекста("
            ):
                on_transport_dispatch()
                self._record("wait_eval")
                self.pending = None
                self.message_reads.append(self.expression)
                return EvaluationResult(
                    pending.result_id, "Строка", '"[\\"after resume\\"]"', False,
                    value_string='["after resume"]',
                )
            return super().wait_evaluation_event(
                pending, timeout_s=timeout_s,
                on_transport_dispatch=on_transport_dispatch,
            )

    class Services:
        def __init__(self) -> None:
            self.record = None
            self.policy = MainReplyPolicy()
            self.publications = []

        def settle_main(self, outcome, payload):
            if self.record is None:
                self.record = MainPublicationRecord(
                    outcome.operation, prior_capture_sequence=0,
                    message_collector_key=payload.statement.message_collector_key,
                )
            self.publications.append((self.record, payload))
            return self.policy.publish(outcome, self.record)

    services = Services()
    session = MessageSession()
    owner = object()
    controller, arbiter, _session, parser = _runtime(
        lambda: RoutePreparationSnapshot(owner, 1, (), ()),
        session=session, settlement_services=services,
    )
    try:
        context, prepared = _prepared(controller, parser, "Результат = 1;")
        payload = prepared.payload
        statement = replace(payload.statement, message_collector_key="__main_messages_1")
        prepared = replace(prepared, payload=replace(payload, statement=statement))
        accepted = controller.submit_cell(
            context, prepared, context.capabilities.for_pipeline().guards,
            SubmissionReceipt(),
        )
        assert isinstance(accepted, Accepted)
        first = accepted.ticket.wait_settled(3)
        assert first.kind is RuntimeReplyKind.CAPTURED
        operation = controller.main_operation
        assert services.record.operation is operation

        if capture_count == 2:
            later_capture = controller.submit_resume().wait_settled(3)
            assert later_capture.kind is RuntimeReplyKind.CAPTURED
            assert later_capture.operation_id == first.operation_id
            assert later_capture.stop_sequence == 2
        second = controller.submit_resume().wait_settled(3)
        assert second.kind is RuntimeReplyKind.MAIN_COMPLETED
        assert second.operation_id == first.operation_id == operation.command_id
        assert second.result == 3
        assert second.messages == ("after resume",)
        assert len(services.publications) == capture_count + 1
        assert all(record is services.record for record, _payload in services.publications)
        assert all(payload is prepared.payload for _record, payload in services.publications)
        assert session.message_reads == [
            'RuntimeKernelServer.ЗабратьСообщенияЯчейкиИзКонтекста(e1cRuntimeКонтекст, "__main_messages_1")'
        ]
    finally:
        arbiter.close(timeout=3)


def test_main_completion_reads_prepared_message_key_without_capture() -> None:
    class MessageSession(CompleteSession):
        def __init__(self) -> None:
            super().__init__(capture_count=0)
            self.message_reads: list[str] = []

        def wait_evaluation_event(self, pending, *, timeout_s, on_transport_dispatch):
            if self.expression.startswith(
                "RuntimeKernelServer.ЗабратьСообщенияЯчейкиИзКонтекста("
            ):
                on_transport_dispatch()
                self._record("wait_eval")
                self.pending = None
                self.message_reads.append(self.expression)
                return EvaluationResult(
                    pending.result_id, "Строка", '"[\\"initial completion\\"]"', False,
                    value_string='["initial completion"]',
                )
            return super().wait_evaluation_event(
                pending, timeout_s=timeout_s,
                on_transport_dispatch=on_transport_dispatch,
            )

    owner = object()
    session = MessageSession()
    controller, arbiter, _session, parser = _runtime(
        lambda: RoutePreparationSnapshot(owner, 1, (), ()), session=session,
    )
    try:
        context, prepared = _prepared(controller, parser, "Результат = 1;")
        statement = replace(
            prepared.payload.statement, message_collector_key="__initial_messages"
        )
        prepared = replace(
            prepared, payload=replace(prepared.payload, statement=statement)
        )
        accepted = controller.submit_cell(
            context, prepared, context.capabilities.for_pipeline().guards,
            SubmissionReceipt(),
        )
        assert isinstance(accepted, Accepted)
        completed = accepted.ticket.wait_settled(3)
        assert completed.completion.messages == ("initial completion",)
        assert session.message_reads == [
            'RuntimeKernelServer.ЗабратьСообщенияЯчейкиИзКонтекста(e1cRuntimeКонтекст, "__initial_messages")'
        ]
    finally:
        arbiter.close(timeout=3)


def test_main_snapshot_stale_rejects_before_rdbg_and_adopts_before_dispatch() -> None:
    owner = object()
    version = [1]
    provider = lambda: RoutePreparationSnapshot(owner, version[0], (), ())
    controller, arbiter, session, parser = _runtime(provider)
    try:
        context, prepared = _prepared(controller, parser, "Результат = 1;")
        assert isinstance(context.policy, MainCellPolicy)
        guards = context.capabilities.for_pipeline().guards
        assert isinstance(controller.validate_preparation(context, guards), Current)

        version[0] = 2
        assert isinstance(controller.validate_preparation(context, guards), StalePreparation)
        rejected_receipt = SubmissionReceipt()
        assert isinstance(
            controller.submit_cell(context, prepared, guards, rejected_receipt), Rejected
        )
        assert rejected_receipt.ticket is None
        assert session.calls == []

        current, current_prepared = _prepared(controller, parser, "Результат = 1;")
        current_guards = current.capabilities.for_pipeline().guards
        receipt = SubmissionReceipt()
        accepted = controller.submit_cell(
            current, current_prepared, current_guards, receipt
        )
        assert isinstance(accepted, Accepted)
        assert accepted.ticket is receipt.ticket
        assert accepted.ticket.wait_initiator().kind.value == "capture"
        assert isinstance(controller.validate_preparation(current, current_guards), StalePreparation)
    finally:
        arbiter.close(timeout=3)


def test_previous_methods_change_rejects_preparation_at_same_version() -> None:
    parser = PythonParserTarget.from_generated()
    owner = object()
    old = _previous_methods(parser, 1)
    new = _previous_methods(parser, 2)
    assert old.exports == new.exports
    snapshots = [RoutePreparationSnapshot(owner, 1, (), old.exports, old)]
    controller, arbiter, session, _parser = _runtime(lambda: snapshots[0])
    try:
        context, prepared = _prepared(controller, parser, "Результат = 1;")
        guards = context.capabilities.for_pipeline().guards
        snapshots[0] = RoutePreparationSnapshot(owner, 1, (), new.exports, new)
        assert isinstance(controller.validate_preparation(context, guards), StalePreparation)
        receipt = SubmissionReceipt()
        assert isinstance(controller.submit_cell(context, prepared, guards, receipt), Rejected)
        assert receipt.ticket is None
        assert session.calls == []
    finally:
        arbiter.close(timeout=3)


def test_previous_methods_change_after_admission_rejects_before_remote_effects() -> None:
    from onec_runtime.execution.controller.controller import StalePreparedDispatch

    parser = PythonParserTarget.from_generated()
    owner = object()
    old = _previous_methods(parser, 1)
    new = _previous_methods(parser, 2)
    assert old.exports == new.exports
    entered = Event()
    release = Event()
    caller_thread = get_ident()
    snapshots = [RoutePreparationSnapshot(owner, 1, (), old.exports, old)]

    def provider():
        if get_ident() != caller_thread:
            entered.set()
            assert release.wait(3)
        return snapshots[0]

    controller, arbiter, session, _parser = _runtime(provider)
    try:
        context, prepared = _prepared(controller, parser, "Результат = 1;")
        accepted = controller.submit_cell(
            context, prepared, context.capabilities.for_pipeline().guards,
            SubmissionReceipt(),
        )
        assert isinstance(accepted, Accepted)
        assert entered.wait(3)
        snapshots[0] = RoutePreparationSnapshot(owner, 1, (), new.exports, new)
        release.set()
        with pytest.raises(StalePreparedDispatch):
            accepted.ticket.wait_settled(3)
        assert session.calls == []
        assert controller.main_operation.phase is MainPhase.FAILED_BEFORE_DISPATCH
    finally:
        release.set()
        arbiter.close(timeout=3)


def test_capture_policy_reuses_scope_and_consumes_one_nonce() -> None:
    owner = object()
    provider = lambda: RoutePreparationSnapshot(owner, 1, (), ())
    controller, arbiter, session, parser = _runtime(provider)
    try:
        main_context, main_prepared = _prepared(controller, parser, "Результат = 1;")
        main_receipt = SubmissionReceipt()
        controller.submit_cell(
            main_context, main_prepared,
            main_context.capabilities.for_pipeline().guards, main_receipt,
        ).ticket.wait_initiator()
        scope = controller.capture_scope
        main = controller.main_operation

        context, prepared = _prepared(controller, parser, "Результат = 2;")
        assert isinstance(context.policy, CaptureCellPolicy)
        receipt = SubmissionReceipt()
        accepted = controller.submit_cell(
            context, prepared, context.capabilities.for_pipeline().guards, receipt
        )
        assert isinstance(accepted, Accepted)
        assert accepted.ticket.wait_initiator().error_occurred is False
        assert controller.capture_scope is scope
        assert controller.main_operation is main
        assert main.phase is MainPhase.SUSPENDED_CAPTURE
        replay = SubmissionReceipt()
        assert isinstance(
            controller.submit_cell(
                context, prepared, context.capabilities.for_pipeline().guards, replay
            ), Rejected
        )
        assert replay.ticket is None
    finally:
        arbiter.close(timeout=3)


def test_foreign_snapshot_owner_rejects_without_adopting_or_dispatching() -> None:
    snapshots = [RoutePreparationSnapshot(object(), 1, (), ())]
    controller, arbiter, session, parser = _runtime(lambda: snapshots[0])
    try:
        context, prepared = _prepared(controller, parser, "Результат = 1;")
        snapshots[0] = RoutePreparationSnapshot(object(), 1, (), ())
        guards = context.capabilities.for_pipeline().guards
        assert isinstance(controller.validate_preparation(context, guards), StalePreparation)
        receipt = SubmissionReceipt()
        assert isinstance(controller.submit_cell(context, prepared, guards, receipt), Rejected)
        assert receipt.ticket is None
        assert session.calls == []
    finally:
        arbiter.close(timeout=3)


def test_queued_ticket_prevents_new_context_and_invalidates_old_context() -> None:
    owner = object()
    controller, arbiter, session, parser = _runtime(
        lambda: RoutePreparationSnapshot(owner, 1, (), ())
    )
    try:
        context, _prepared_cell = _prepared(controller, parser, "Результат = 1;")
        queued = arbiter.submit(
            arbiter.current_route, lambda _port: Settlement(None)
        )
        assert arbiter.active_ticket is None
        assert isinstance(controller.await_preparation_context(), Unavailable)
        guards = context.capabilities.for_pipeline().guards
        assert isinstance(
            controller.validate_preparation(context, guards), StalePreparation
        )
        assert queued.cancel_queued()
        assert session.calls == []
    finally:
        arbiter.close(timeout=3)


def test_cancelled_adopted_main_ticket_leaves_terminal_operation() -> None:
    class SecondCommandSession(CompleteSession):
        def wait_evaluation_event(self, pending, *, timeout_s, on_transport_dispatch):
            if self.expression == "ИдентификаторКоманды":
                on_transport_dispatch()
                self._record("wait_eval")
                self.pending = None
                return EvaluationResult(pending.result_id, "Число", "2", False)
            return super().wait_evaluation_event(
                pending, timeout_s=timeout_s,
                on_transport_dispatch=on_transport_dispatch,
            )

    owner = object()
    controller, arbiter, session, parser = _runtime(
        lambda: RoutePreparationSnapshot(owner, 1, (), ()),
        session=SecondCommandSession(),
    )
    original_dispatch = arbiter.dispatch

    def cancel_before_dispatch(ticket):
        assert ticket.cancel_queued()
        original_dispatch(ticket)

    try:
        context, prepared = _prepared(controller, parser, "Результат = 1;")
        receipt = SubmissionReceipt()
        arbiter.dispatch = cancel_before_dispatch
        with pytest.raises(RuntimeError, match="no longer queued"):
            controller.submit_cell(
                context, prepared, context.capabilities.for_pipeline().guards,
                receipt,
            )
        assert receipt.ticket is not None
        with pytest.raises(CancelledBeforeEffect):
            receipt.ticket.wait_initiator()
        assert controller.main_operation is not None
        assert controller.main_operation.phase is MainPhase.FAILED_BEFORE_DISPATCH
        assert session.calls == []

        arbiter.dispatch = original_dispatch
        assert controller.submit_main("Результат = 2;").wait_initiator().kind.value == "capture"
    finally:
        arbiter.dispatch = original_dispatch
        arbiter.close(timeout=3)


def test_worker_revalidates_snapshot_before_first_remote_side_effect() -> None:
    from onec_runtime.execution.controller.controller import StalePreparedDispatch

    owner = object()
    version = [1]
    entered = Event()
    release = Event()
    caller_thread = get_ident()
    settlements = []

    class Services:
        def settle_main(self, outcome, payload):
            settlements.append((outcome, payload))
            return outcome

    def provider():
        if get_ident() != caller_thread:
            entered.set()
            assert release.wait(3)
        return RoutePreparationSnapshot(owner, version[0], (), ())

    controller, arbiter, session, parser = _runtime(
        provider, settlement_services=Services()
    )
    try:
        context, prepared = _prepared(controller, parser, "Результат = 1;")
        receipt = SubmissionReceipt()
        accepted = controller.submit_cell(
            context, prepared, context.capabilities.for_pipeline().guards, receipt
        )
        assert isinstance(accepted, Accepted)
        assert entered.wait(3)
        version[0] = 2
        release.set()
        with pytest.raises(StalePreparedDispatch):
            accepted.ticket.wait_initiator()
        assert session.calls == []
        assert settlements == []
        assert controller.main_operation.phase is MainPhase.FAILED_BEFORE_DISPATCH
        assert isinstance(controller.await_preparation_context().policy, MainCellPolicy)
    finally:
        release.set()
        arbiter.close(timeout=3)


def test_later_capture_inspection_does_not_stale_an_already_adopted_cell() -> None:
    owner = object()
    entered = Event()
    release = Event()
    caller_thread = get_ident()
    block_worker = [False]

    def provider():
        if block_worker[0] and get_ident() != caller_thread:
            entered.set()
            assert release.wait(3)
        return RoutePreparationSnapshot(owner, 1, (), ())

    controller, arbiter, _session, parser = _runtime(provider)
    try:
        controller.submit_main("Результат = 1;").wait(3)
        context, prepared = _prepared(controller, parser, "Результат = 2;")
        block_worker[0] = True
        receipt = SubmissionReceipt()
        accepted = controller.submit_cell(
            context, prepared, context.capabilities.for_pipeline().guards, receipt
        )
        assert isinstance(accepted, Accepted)
        assert entered.wait(3)
        inspection = controller.submit_capture_variable("Amount")
        release.set()
        assert accepted.ticket.wait_initiator().error_occurred is False
        assert inspection.wait(3).name == "Amount"
    finally:
        release.set()
        arbiter.close(timeout=3)


def test_submission_receipt_is_adopted_before_first_transport_entry() -> None:
    receipt = SubmissionReceipt()

    class ReceiptSession(CompleteSession):
        def set_breakpoints(self, locations, *, on_transport_dispatch):
            assert receipt.ticket is not None
            return super().set_breakpoints(
                locations, on_transport_dispatch=on_transport_dispatch
            )

    owner = object()
    provider = lambda: RoutePreparationSnapshot(owner, 1, (), ())
    controller, arbiter, session, parser = _runtime(
        provider, session=ReceiptSession()
    )
    try:
        context, prepared = _prepared(controller, parser, "Результат = 1;")
        accepted = controller.submit_cell(
            context, prepared, context.capabilities.for_pipeline().guards, receipt
        )
        assert isinstance(accepted, Accepted)
        assert accepted.ticket.wait_initiator().kind.value == "capture"
        assert session.calls
    finally:
        arbiter.close(timeout=3)


def test_preparation_waits_for_main_stop_without_holding_controller_lock() -> None:
    entered = Event()
    release = Event()

    class WaitingSession(CompleteSession):
        def wait_for_any_stop(self, *, timeout_s, expected_target, on_transport_dispatch):
            entered.set()
            assert release.wait(3)
            return super().wait_for_any_stop(
                timeout_s=timeout_s,
                expected_target=expected_target,
                on_transport_dispatch=on_transport_dispatch,
            )

    owner = object()
    controller, arbiter, _session, _parser = _runtime(
        lambda: RoutePreparationSnapshot(owner, 1, (), ()),
        session=WaitingSession(),
    )
    result = []
    finished = Event()

    def await_route():
        try:
            result.append(controller.await_preparation_context())
        finally:
            finished.set()

    waiter = Thread(target=await_route)
    try:
        old_context = controller.await_preparation_context()
        ticket = controller.submit_main("Результат = 1;")
        assert entered.wait(3)
        ticket.detach_waiter()
        waiter.start()
        assert not finished.wait(0.1)
        assert isinstance(
            controller.validate_preparation(
                old_context, old_context.capabilities.for_pipeline().guards
            ), StalePreparation
        )
        release.set()
        assert ticket.wait_settled().kind.value == "capture"
        assert finished.wait(3)
        assert isinstance(result[0].policy, CaptureCellPolicy)
    finally:
        release.set()
        waiter.join(3)
        ticket.wait_settled(3)
        arbiter.close(timeout=3)


def test_preparation_waits_for_queued_main_before_choosing_capture_route() -> None:
    blocker_entered = Event()
    release_blocker = Event()
    finished = Event()
    result = []

    owner = object()
    controller, arbiter, _session, _parser = _runtime(
        lambda: RoutePreparationSnapshot(owner, 1, (), ())
    )

    def blocker_plan(_port):
        blocker_entered.set()
        assert release_blocker.wait(3)
        return Settlement(None)

    def await_route():
        try:
            result.append(controller.await_preparation_context())
        finally:
            finished.set()

    blocker = arbiter.submit(arbiter.current_route, blocker_plan)
    arbiter.dispatch(blocker)
    waiter = Thread(target=await_route)
    try:
        assert blocker_entered.wait(3)
        main = controller.submit_main("Результат = 1;")
        assert main.status().phase == "queued"
        waiter.start()
        assert not finished.wait(0.1)
        release_blocker.set()
        blocker.wait_settled(3)
        assert main.wait_settled(3).kind.value == "capture"
        assert finished.wait(3)
        assert isinstance(result[0].policy, CaptureCellPolicy)
    finally:
        release_blocker.set()
        waiter.join(3)
        arbiter.close(timeout=3)


def test_preparation_waits_for_dependent_value_cleanup_after_reply() -> None:
    cleanup_entered = Event()
    release_cleanup = Event()
    finished = Event()
    observed = []
    owner = object()
    controller, arbiter, _session, _parser = _runtime(
        lambda: RoutePreparationSnapshot(owner, 1, (), ())
    )

    def cleanup_plan(_port):
        cleanup_entered.set()
        assert release_cleanup.wait(3)
        return Settlement(None)

    def parent_plan(port):
        port.register_post_settlement_cleanup(cleanup_plan)
        return Settlement(None)

    def await_route():
        try:
            observed.append(controller.await_preparation_context())
        finally:
            finished.set()

    parent = arbiter.submit(arbiter.current_route, parent_plan)
    arbiter.dispatch(parent)
    waiter = Thread(target=await_route)
    try:
        parent.wait_settled(3)
        assert cleanup_entered.wait(3)
        waiter.start()
        assert not finished.wait(0.1)
        release_cleanup.set()
        assert finished.wait(3)
        assert isinstance(observed[0].policy, MainCellPolicy)
    finally:
        release_cleanup.set()
        waiter.join(3) if waiter.ident is not None else None
        arbiter.close(timeout=3)


def test_preparation_waits_for_resumed_main_completion() -> None:
    entered = Event()
    release = Event()
    finished = Event()
    result = []

    class WaitingResumeSession(CompleteSession):
        def __init__(self):
            super().__init__()
            self.stop_count = 0

        def wait_for_any_stop(self, *, timeout_s, expected_target, on_transport_dispatch):
            self.stop_count += 1
            if self.stop_count == 2:
                entered.set()
                assert release.wait(3)
            return super().wait_for_any_stop(
                timeout_s=timeout_s,
                expected_target=expected_target,
                on_transport_dispatch=on_transport_dispatch,
            )

    owner = object()
    controller, arbiter, _session, _parser = _runtime(
        lambda: RoutePreparationSnapshot(owner, 1, (), ()),
        session=WaitingResumeSession(),
    )

    def await_route():
        try:
            result.append(controller.await_preparation_context())
        finally:
            finished.set()

    waiter = Thread(target=await_route)
    try:
        assert controller.submit_main("Результат = 1;").wait_settled(3).kind.value == "capture"
        resumed = controller.submit_resume()
        assert entered.wait(3)
        waiter.start()
        assert not finished.wait(0.1)
        release.set()
        assert resumed.wait_settled(3).kind.value == "completed"
        assert finished.wait(3)
        assert isinstance(result[0].policy, MainCellPolicy)
    finally:
        release.set()
        waiter.join(3)
        arbiter.close(timeout=3)


@pytest.mark.parametrize("with_statement", [False, True])
def test_worker_method_intent_is_local_until_controller_can_activate_artifact(
    with_statement: bool,
) -> None:
    owner = object()
    controller, arbiter, session, parser = _runtime(
        lambda: RoutePreparationSnapshot(owner, 1, (), ())
    )
    try:
        context = controller.await_preparation_context()
        source = "Функция Посчитать() Экспорт\nВозврат 1;\nКонецФункции\n"
        if with_statement:
            source += "Итог = Посчитать();"
        common = _common(source, parser)
        prepared = context.policy.prepare(common, context.capabilities.for_pipeline(), context)
        assert isinstance(prepared, PreparedCell)
        assert prepared.payload.worker_intent.projection is common.source_maps.worker_candidate
        assert prepared.payload.statement is None
        assert (prepared.payload.deferred_statement is not None) is with_statement
        receipt = SubmissionReceipt()
        rejected = controller.submit_cell(
            context, prepared, context.capabilities.for_pipeline().guards, receipt
        )
        assert isinstance(rejected, Rejected)
        assert isinstance(rejected.reason, Unavailable)
        assert receipt.ticket is None
        assert session.calls == []
    finally:
        arbiter.close(timeout=3)


@pytest.mark.parametrize("capture", [False, True])
@pytest.mark.parametrize("with_statement", [False, True])
def test_worker_intent_activates_after_ticket_admission_and_releases_on_its_route(
    capture: bool, with_statement: bool,
) -> None:
    """A method projection is activated by the arbiter before deferred BSL only."""

    events: list[tuple[str, object]] = []

    class Lease:
        def release(self, *, port) -> None:
            events.append(("release", port))

        def retain_outcome_unknown(self, *, port) -> None:
            events.append(("retain", port))

    class Activation:
        def pin_active(self, *, port):
            return None

        def activate(self, intent, *, port):
            events.append(("activate", intent, port))
            return Lease()

    owner = object()
    controller, arbiter, session, parser = _runtime(
        lambda: RoutePreparationSnapshot(owner, 1, (), ()),
        worker_activation=Activation(),
    )
    try:
        if capture:
            controller.submit_main("Результат = 1;").wait_settled(3)
        context = controller.await_preparation_context()
        source = "Функция Посчитать() Экспорт\nВозврат 1;\nКонецФункции\n"
        if with_statement:
            source += "Итог = Посчитать();"
        prepared = context.policy.prepare(
            _common(source, parser), context.capabilities.for_pipeline(), context
        )
        assert isinstance(prepared, PreparedCell)
        receipt = SubmissionReceipt()
        modifications_before = sum(name == "modify" for name, _thread in session.calls)
        accepted = controller.submit_cell(
            context, prepared, context.capabilities.for_pipeline().guards, receipt
        )
        assert isinstance(accepted, Accepted)
        assert receipt.ticket is accepted.ticket
        result = accepted.ticket.wait_settled(3)
        release_parent = accepted.ticket
        if not with_statement:
            assert result is None
        if with_statement and not capture:
            assert result.kind.value == "capture"
            assert [event[0] for event in events] == ["activate"]
            release_parent = controller.submit_resume()
            assert release_parent.wait_settled(3).kind.value == "completed"
        cleanup = release_parent.post_settlement_cleanup
        assert cleanup is not None
        assert cleanup.wait_settled(3) is None
        assert [event[0] for event in events] == ["activate", "release"]
        assert events[0][1] is prepared.payload.worker_intent
        assert events[0][2] is not events[1][1]
        if with_statement:
            if capture:
                assert session.expression.startswith(
                    "RuntimeKernelServer.ВыполнитьКодВКонтекстеОтладки("
                )
            else:
                assert sum(name == "modify" for name, _thread in session.calls) > modifications_before
        else:
            assert sum(name == "modify" for name, _thread in session.calls) == modifications_before
    finally:
        arbiter.close(timeout=3)


def test_main_worker_definition_prebuilds_exact_provenance_before_admission() -> None:
    from onec_runtime.execution.provenance import PreparedExecutionProvenanceReader
    from onec_runtime.execution.worker_activation import PrebuiltWorkerIntent
    from onec_runtime.server_worker import WorkerArtifact, WorkerSourceProvenance

    source = (
        "Функция УвеличитьНаПроцент(Значение)\n"
        "    Возврат Значение * (1 + ПроцентПовышения / 100);\n"
        "КонецФункции"
    )
    compiled_hash = source_sha256("compiled worker")
    map_hash = source_sha256("worker source map")
    build_calls: list[object] = []
    activations: list[object] = []
    prebuild_owner = object()

    class Lease:
        def release(self, *, port) -> None:
            pass

        def retain_outcome_unknown(self, *, port) -> None:
            raise AssertionError("Worker activation unexpectedly became unknown")

    class Activation:
        def pin_active(self, *, port):
            return None

        def prebuild(self, intent):
            build_calls.append(intent)
            artifact = WorkerArtifact(
                "Worker", compiled_hash, source_sha256("worker binary"),
                intent.exports,
                WorkerSourceProvenance(compiled_hash, map_hash),
            )
            return PrebuiltWorkerIntent(
                prebuild_owner, intent, intent.method_set_candidate, artifact, 0,
            )

        def activate(self, intent, *, port):
            activations.append(intent)
            return Lease()

    owner = object()
    controller, arbiter, _session, parser = _runtime(
        lambda: RoutePreparationSnapshot(owner, 1, ("ПроцентПовышения",), ()),
        worker_activation=Activation(),
    )
    try:
        context = controller.await_preparation_context()
        snapshots = context.capabilities.for_pipeline()
        prepared = context.policy.prepare(_common(source, parser), snapshots, context)
        assert isinstance(prepared, PreparedCell)
        provenance = PreparedExecutionProvenanceReader()(prepared)
        assert provenance.visible_source_sha256 == source_sha256(source)
        assert provenance.executed_source_sha256 == compiled_hash
        assert provenance.source_map_sha256 == map_hash
        assert provenance.mode == "main"
        assert len(build_calls) == 1
        assert arbiter.active_ticket is None

        accepted = controller.submit_cell(
            context, prepared, snapshots.guards, SubmissionReceipt(),
        )
        assert isinstance(accepted, Accepted)
        assert accepted.ticket.wait_settled(3) is None
        assert activations == [prepared.payload.worker_intent]
        assert len(build_calls) == 1
    finally:
        arbiter.close(timeout=3)


def test_worker_activation_unknown_stays_on_accepted_ticket_without_user_bsl() -> None:
    """An ambiguous activation is an owned ticket outcome, never a rejection."""

    calls = []

    class Lease:
        def release(self, *, port) -> None:
            calls.append(("release", port))

        def retain_outcome_unknown(self, *, port) -> None:
            calls.append(("retain", port))

    class Activation:
        def activate(self, intent, *, port):
            calls.append((intent, port))
            raise WorkerActivationUnknown(Lease(), "Worker promotion reply was lost")

    owner = object()
    controller, arbiter, session, parser = _runtime(
        lambda: RoutePreparationSnapshot(owner, 1, (), ()),
        worker_activation=Activation(),
    )
    try:
        context, prepared = _prepared(
            controller, parser,
            "Функция Посчитать() Экспорт\nВозврат 1;\nКонецФункции\nИтог = Посчитать();",
        )
        receipt = SubmissionReceipt()
        accepted = controller.submit_cell(
            context, prepared, context.capabilities.for_pipeline().guards, receipt
        )
        assert isinstance(accepted, Accepted)
        assert accepted.ticket.wait_unknown(3)
        assert receipt.ticket is accepted.ticket
        assert calls and calls[0][0] is prepared.payload.worker_intent
        assert calls[1][0] == "retain" and calls[1][1] is calls[0][1]
        assert not any(name == "modify" for name, _thread in session.calls)
        assert controller.main_operation is None

        arbiter.reconcile(accepted.ticket, lambda _port: ReadyForPolicy(None))
        assert accepted.ticket.wait_settled(3) is None
    finally:
        arbiter.close(timeout=3)


def test_confirmed_worker_release_failure_preserves_capture_cell_result() -> None:
    """A post-result pin debt must not replace an already confirmed eval."""

    released = Event()
    release_attempts = [0]

    class Lease:
        def release(self, *, port) -> None:
            release_attempts[0] += 1
            released.set()
            if release_attempts[0] == 1:
                raise ValueError("Worker pin release was rejected")

        def retain_outcome_unknown(self, *, port) -> None:
            raise AssertionError("The release failure was confirmed")

    class Activation:
        def pin_active(self, *, port):
            return None

        def activate(self, intent, *, port):
            return Lease()

    owner = object()
    controller, arbiter, _session, parser = _runtime(
        lambda: RoutePreparationSnapshot(owner, 1, (), ()),
        worker_activation=Activation(),
    )
    try:
        controller.submit_main("Результат = 1;").wait_settled(3)
        context, prepared = _prepared(
            controller, parser,
            "Функция Посчитать() Экспорт\nВозврат 1;\nКонецФункции\n"
            "Итог = Посчитать();",
        )
        accepted = controller.submit_cell(
            context, prepared, context.capabilities.for_pipeline().guards,
            SubmissionReceipt(),
        )
        assert isinstance(accepted, Accepted)
        result = accepted.ticket.wait_settled(3)
        assert result.error_occurred is False
        assert released.wait(3)
        cleanup = accepted.ticket.post_settlement_cleanup
        assert cleanup is not None
        with pytest.raises(ValueError, match="Worker pin release was rejected"):
            cleanup.wait_settled(3)
        assert controller.capture_scope is not None
        assert controller.capture_scope.context_state is CaptureContextState.READY
        later = controller.submit_capture_cell("РезультатИнструкции = 4;")
        assert later.status().phase == "queued"
        retry = arbiter.retry_post_settlement_cleanup(accepted.ticket)
        assert retry.wait_settled(3) is None
        assert release_attempts == [2]
        assert later.wait_settled(3).error_occurred is False
    finally:
        arbiter.close(timeout=3)


def test_confirmed_main_completion_survives_worker_pin_release_failure() -> None:
    """The completed MAIN command is publishable before Worker cleanup debt."""

    released = Event()
    release_attempts = [0]

    class Lease:
        def release(self, *, port) -> None:
            release_attempts[0] += 1
            released.set()
            if release_attempts[0] == 1:
                raise ValueError("Worker pin release was rejected")

        def retain_outcome_unknown(self, *, port) -> None:
            raise AssertionError("The release failure was confirmed")

    class Activation:
        def activate(self, intent, *, port):
            return Lease()

    owner = object()
    controller, arbiter, _session, parser = _runtime(
        lambda: RoutePreparationSnapshot(owner, 1, (), ()),
        session=CompleteSession(capture_count=0),
        worker_activation=Activation(),
    )
    try:
        context, prepared = _prepared(
            controller, parser,
            "Функция Посчитать() Экспорт\nВозврат 1;\nКонецФункции\n"
            "Итог = Посчитать();",
        )
        accepted = controller.submit_cell(
            context, prepared, context.capabilities.for_pipeline().guards,
            SubmissionReceipt(),
        )
        assert isinstance(accepted, Accepted)
        completed = accepted.ticket.wait_settled(3)
        assert completed.kind.value == "completed"
        assert completed.completion.result == 3
        assert released.wait(3)
        cleanup = accepted.ticket.post_settlement_cleanup
        assert cleanup is not None
        with pytest.raises(ValueError, match="Worker pin release was rejected"):
            cleanup.wait_settled(3)
        assert controller.main_operation.phase is MainPhase.COMPLETED
        with pytest.raises(ArbiterBusy, match="Confirmed cleanup debt requires retry"):
            arbiter.close(timeout=3)
        assert accepted.ticket.wait_settled(0) is completed

        retry = arbiter.retry_post_settlement_cleanup(accepted.ticket)
        assert retry.wait_settled(3) is None
        assert release_attempts == [2]
        assert accepted.ticket.wait_settled(0) is completed
        assert controller.main_operation.phase is MainPhase.COMPLETED
    finally:
        arbiter.close(timeout=3)


def test_confirmed_worker_activation_failure_settles_ticket_before_user_bsl() -> None:
    class Activation:
        def activate(self, intent, *, port):
            del intent, port
            raise ValueError("Worker artifact was rejected")

    owner = object()
    controller, arbiter, session, parser = _runtime(
        lambda: RoutePreparationSnapshot(owner, 1, (), ()),
        worker_activation=Activation(),
    )
    try:
        context, prepared = _prepared(
            controller, parser,
            "Функция Посчитать() Экспорт\nВозврат 1;\nКонецФункции\nИтог = Посчитать();",
        )
        accepted = controller.submit_cell(
            context, prepared, context.capabilities.for_pipeline().guards,
            SubmissionReceipt(),
        )
        assert isinstance(accepted, Accepted)
        with pytest.raises(ValueError, match="Worker artifact was rejected"):
            accepted.ticket.wait_settled(3)
        assert controller.main_operation is None
        assert not any(name == "modify" for name, _thread in session.calls)
    finally:
        arbiter.close(timeout=3)


def test_worker_main_dispatch_unknown_retains_moved_operation_lease() -> None:
    events = []

    class Lease:
        def release(self, *, port) -> None:
            events.append(("release", port))

        def retain_outcome_unknown(self, *, port) -> None:
            events.append(("retain", port))

    class Activation:
        def activate(self, intent, *, port):
            events.append(("activate", intent, port))
            return Lease()

    class UnknownInstructionSession(CompleteSession):
        def modify(self, variable, value_expression, *, on_transport_dispatch):
            if variable == "ТекущаяИнструкция":
                on_transport_dispatch()
                self._record("modify")
                raise OutcomeUnknown("instruction reply was lost")
            return super().modify(variable, value_expression,
                                 on_transport_dispatch=on_transport_dispatch)

    owner = object()
    controller, arbiter, session, parser = _runtime(
        lambda: RoutePreparationSnapshot(owner, 1, (), ()),
        session=UnknownInstructionSession(), worker_activation=Activation(),
    )
    try:
        context, prepared = _prepared(
            controller, parser,
            "Функция Посчитать() Экспорт\nВозврат 1;\nКонецФункции\nИтог = Посчитать();",
        )
        accepted = controller.submit_cell(
            context, prepared, context.capabilities.for_pipeline().guards,
            SubmissionReceipt(),
        )
        assert isinstance(accepted, Accepted)
        assert accepted.ticket.wait_unknown(3)
        assert controller.main_operation is not None
        assert controller.main_operation.phase is MainPhase.UNKNOWN
        assert [event[0] for event in events] == ["activate", "retain"]

        confirm_test_server_terminated(
            arbiter, accepted.ticket, arbiter.current_route, session, TARGET,
        )
    finally:
        arbiter.close(timeout=3)


def test_confirmed_worker_main_precontinue_failure_releases_lease_and_terminalizes_main() -> None:
    events = []

    class Lease:
        def release(self, *, port) -> None:
            events.append(("release", port))

        def retain_outcome_unknown(self, *, port) -> None:
            events.append(("retain", port))

    class Activation:
        def activate(self, intent, *, port):
            events.append(("activate", intent, port))
            return Lease()

    class RejectedInstructionSession(CompleteSession):
        def modify(self, variable, value_expression, *, on_transport_dispatch):
            if variable == "ТекущаяИнструкция":
                on_transport_dispatch()
                self._record("modify")
                return ModifyResult(UUID(int=91), "Ошибка", "", True, "rejected")
            return super().modify(variable, value_expression,
                                 on_transport_dispatch=on_transport_dispatch)

    owner = object()
    controller, arbiter, _session, parser = _runtime(
        lambda: RoutePreparationSnapshot(owner, 1, (), ()),
        session=RejectedInstructionSession(), worker_activation=Activation(),
    )
    try:
        context, prepared = _prepared(
            controller, parser,
            "Функция Посчитать() Экспорт\nВозврат 1;\nКонецФункции\nИтог = Посчитать();",
        )
        accepted = controller.submit_cell(
            context, prepared, context.capabilities.for_pipeline().guards,
            SubmissionReceipt(),
        )
        assert isinstance(accepted, Accepted)
        with pytest.raises(Exception, match="ТекущаяИнструкция"):
            accepted.ticket.wait_settled(3)
        assert controller.main_operation is not None
        assert controller.main_operation.phase is MainPhase.FAILED_BEFORE_DISPATCH
        assert [event[0] for event in events] == ["activate", "release"]
    finally:
        arbiter.close(timeout=3)


def test_worker_main_keeps_its_policy_and_message_key_across_capture_resume() -> None:
    class MessageSession(CompleteSession):
        def wait_evaluation_event(self, pending, *, timeout_s, on_transport_dispatch):
            if self.expression.startswith(
                "RuntimeKernelServer.ЗабратьСообщенияЯчейкиИзКонтекста("
            ):
                on_transport_dispatch()
                self._record("wait_eval")
                self.pending = None
                return EvaluationResult(
                    pending.result_id, "Строка", '"[]"', False,
                    value_string="[]",
                )
            return super().wait_evaluation_event(
                pending, timeout_s=timeout_s,
                on_transport_dispatch=on_transport_dispatch,
            )

    class Lease:
        def release(self, *, port) -> None:
            pass

        def retain_outcome_unknown(self, *, port) -> None:
            pass

    class Activation:
        def activate(self, intent, *, port):
            return Lease()

    class SettlementServices:
        def settle_main(self, outcome, payload):
            return ("published", outcome, payload)

    owner = object()
    controller, arbiter, _session, parser = _runtime(
        lambda: RoutePreparationSnapshot(owner, 1, (), ()),
        session=MessageSession(),
        worker_activation=Activation(),
        settlement_services=SettlementServices(),
    )
    try:
        context, prepared = _prepared(
            controller, parser,
            "Функция Посчитать() Экспорт\nВозврат 1;\nКонецФункции\nИтог = Посчитать();",
        )
        accepted = controller.submit_cell(
            context, prepared, context.capabilities.for_pipeline().guards,
            SubmissionReceipt(),
        )
        assert isinstance(accepted, Accepted)
        stopped = accepted.ticket.wait_settled(3)
        assert stopped[0] == "published"
        assert stopped[1].kind.value == "capture"
        operation = controller.main_operation
        assert operation is not None
        assert operation.message_collector_key == (
            prepared.payload.deferred_statement.message_collector_key
        )

        finished = controller.submit_resume().wait_settled(3)
        assert finished[0] == "published"
        assert finished[1].kind.value == "completed"
        assert finished[1].operation is operation
        assert finished[2] is prepared.payload
    finally:
        arbiter.close(timeout=3)


def test_existing_worker_generation_is_pinned_for_main_and_each_capture_cell() -> None:
    """Ordinary statements retain the active generation across one MAIN stop."""

    events: list[tuple[str, int, object]] = []

    class Lease:
        def __init__(self, number: int) -> None:
            self.number = number

        def release(self, *, port) -> None:
            events.append(("release", self.number, port))

        def retain_outcome_unknown(self, *, port) -> None:
            events.append(("retain", self.number, port))

    class ActiveWorker:
        def pin_active(self, *, port):
            number = len([event for event in events if event[0] == "pin"]) + 1
            events.append(("pin", number, port))
            return Lease(number)

        def activate(self, intent, *, port):
            raise AssertionError("No new Worker generation is being published")

    owner = object()
    controller, arbiter, _session, _parser = _runtime(
        lambda: RoutePreparationSnapshot(owner, 1, (), ()),
        worker_activation=ActiveWorker(),
    )
    try:
        main = controller.submit_main("Результат = 1;")
        assert main.wait_settled(3).kind.value == "capture"
        assert [(kind, number) for kind, number, _ in events] == [("pin", 1)]

        capture = controller.submit_capture_cell("Результат = 2;")
        assert capture.wait_settled(3).error_occurred is False
        assert capture.post_settlement_cleanup is not None
        assert capture.post_settlement_cleanup.wait_settled(3) is None
        assert [(kind, number) for kind, number, _ in events] == [
            ("pin", 1), ("pin", 2), ("release", 2),
        ]

        resumed = controller.submit_resume()
        assert resumed.wait_settled(3).kind.value == "completed"
        assert resumed.post_settlement_cleanup is not None
        assert resumed.post_settlement_cleanup.wait_settled(3) is None
        assert [(kind, number) for kind, number, _ in events] == [
            ("pin", 1), ("pin", 2), ("release", 2), ("release", 1),
        ]
        assert all(port is not None for _kind, _number, port in events)
    finally:
        arbiter.close(timeout=3)


def test_existing_worker_capture_pin_survives_unknown_eval_until_reconciliation() -> None:
    class AmbiguousCaptureSession(CompleteSession):
        def __init__(self) -> None:
            super().__init__()
            self.ambiguous_once = True

        def start_evaluation(self, expression, **kwargs):
            pending = super().start_evaluation(expression, **kwargs)
            if (
                self.ambiguous_once
                and expression.startswith("RuntimeKernelServer.ВыполнитьКодВКонтекстеОтладки(")
            ):
                self.ambiguous_once = False
                raise EvaluationDispatchUnknown(pending)
            return pending

    events: list[tuple[str, int]] = []

    class Lease:
        def __init__(self, number: int) -> None:
            self.number = number

        def release(self, *, port) -> None:
            events.append(("release", self.number))

        def retain_outcome_unknown(self, *, port) -> None:
            events.append(("retain", self.number))

    class ActiveWorker:
        def pin_active(self, *, port):
            number = len([event for event in events if event[0] == "pin"]) + 1
            events.append(("pin", number))
            return Lease(number)

        def activate(self, intent, *, port):
            raise AssertionError("No Worker publication is expected")

    owner = object()
    controller, arbiter, session, _parser = _runtime(
        lambda: RoutePreparationSnapshot(owner, 1, (), ()),
        session=AmbiguousCaptureSession(), worker_activation=ActiveWorker(),
    )
    try:
        controller.submit_main("Результат = 1;").wait_settled(3)
        ticket = controller.submit_capture_cell("Результат = 2;")
        assert ticket.wait_unknown(3)
        assert events == [("pin", 1), ("pin", 2)]
        assert ticket.post_settlement_cleanup is None

        controller.reconcile_capture_pending_eval(ticket)
        assert ticket.wait_settled(3).error_occurred is False
        assert ticket.post_settlement_cleanup is not None
        assert ticket.post_settlement_cleanup.wait_settled(3) is None
        assert events == [("pin", 1), ("pin", 2), ("release", 2)]
    finally:
        if 'ticket' in locals() and ticket.status().phase == 'unknown':
            confirm_test_server_terminated(
                arbiter, ticket, arbiter.current_route, session, TARGET,
            )
        arbiter.close(timeout=3)


def test_existing_worker_main_pin_releases_after_matching_completion_decode_error() -> None:
    """A confirmed command ID ends MAIN even when its result cannot be decoded."""
    from onec_runtime.execution.main.completion import MainScalarDecodeError

    class BadResultSession(CompleteSession):
        def __init__(self) -> None:
            super().__init__(capture_count=0)

        def wait_evaluation_event(self, pending, *, timeout_s, on_transport_dispatch):
            if self.expression == "Результат":
                on_transport_dispatch()
                self._record("wait_eval")
                self.pending = None
                return EvaluationResult(pending.result_id, "Число", "NaN", False)
            return super().wait_evaluation_event(
                pending, timeout_s=timeout_s,
                on_transport_dispatch=on_transport_dispatch,
            )

    events: list[str] = []

    class Lease:
        def release(self, *, port) -> None:
            events.append("release")

        def retain_outcome_unknown(self, *, port) -> None:
            events.append("retain")

    class ActiveWorker:
        def pin_active(self, *, port):
            events.append("pin")
            return Lease()

        def activate(self, intent, *, port):
            raise AssertionError("No Worker publication is expected")

    owner = object()
    controller, arbiter, _session, _parser = _runtime(
        lambda: RoutePreparationSnapshot(owner, 1, (), ()),
        session=BadResultSession(), worker_activation=ActiveWorker(),
    )
    try:
        ticket = controller.submit_main("Результат = 1;")
        with pytest.raises(MainScalarDecodeError):
            ticket.wait_settled(3)
        assert controller.main_operation.phase is MainPhase.COMPLETED
        assert ticket.post_settlement_cleanup is not None
        assert ticket.post_settlement_cleanup.wait_settled(3) is None
        assert events == ["pin", "release"]
    finally:
        arbiter.close(timeout=3)
