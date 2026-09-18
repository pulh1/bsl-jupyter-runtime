"""Controller-selected statement preparation has one fenced admission path."""

from threading import Event, Thread, get_ident

import pytest

from onec_runtime.bsl.parser_target import PythonParserTarget
from onec_runtime.bsl.source_maps import SourceUnitKind, SourceUnitRef, source_sha256
from onec_runtime.execution.arbiter import (
    CancelledBeforeEffect, RdbgArbiter, RouteToken, Settlement, WaiterDetached,
)
import onec_runtime.execution.arbiter as arbiter_module
from onec_runtime.errors import EvaluationDispatchUnknown
from onec_runtime.execution.capture.cell_evaluator import CaptureCellEvaluator
from onec_runtime.execution.capture.executor import CaptureExecutor
from onec_runtime.execution.capture.policy import CaptureCellPolicy
from onec_runtime.execution.common import NotebookCommonParser
from onec_runtime.execution.contracts import (
    Accepted, Current, PreparedCell, Rejected, SourceDiagnostic,
    StalePreparation, SubmissionReceipt, Unavailable,
)
from onec_runtime.execution.main import MainExecutor, MainPhase
from onec_runtime.execution.main.policy import MainCellPolicy
from onec_runtime.execution.snapshot_binding import RoutePreparationSnapshot
from onec_runtime.rdbg.models import EvaluationResult
from onec_runtime.stop_routing import BreakpointRegistry
from onec_runtime.table_value import evaluation_to_python

from test_execution_controller_routes import BUSINESS, CompleteSession, KERNEL


def _common(source: str, parser: PythonParserTarget):
    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL, "controller-route", 1, source_sha256(source)
    )
    result = NotebookCommonParser(parser).prepare(source, unit)
    assert not isinstance(result, SourceDiagnostic)
    return result


def _runtime(snapshot_provider, *, session=None, settlement_services=None):
    from onec_runtime.execution.controller.controller import ExecutionController

    session = session or CompleteSession()
    arbiter = RdbgArbiter(session, RouteToken("runtime-1", 1, 0, "main"))
    parser = PythonParserTarget.from_generated()
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
    )
    return controller, arbiter, session, parser


def _prepared(controller, parser, source: str):
    context = controller.await_preparation_context()
    common = _common(source, parser)
    prepared = context.policy.prepare(common, context.capabilities.for_pipeline(), context)
    assert isinstance(prepared, PreparedCell)
    return context, prepared


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

        def finish(port):
            result = port.wait_evaluation_event(pending, timeout_s=1)
            port.set_breakpoints((KERNEL, BUSINESS))
            return arbiter_module.ReadyForPolicy(result)

        arbiter.reconcile(ticket, finish)
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
            from onec_runtime.execution.termination import FileTerminationConfirmed
            arbiter.retire_terminated_target(
                ticket, arbiter.current_route,
                FileTerminationConfirmed(
                    (ticket.status().pending_capability or session.target).target_id,
                    1234, -15,
                ),
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


def test_worker_method_artifact_path_is_explicitly_outside_statement_binding() -> None:
    owner = object()
    controller, arbiter, session, parser = _runtime(
        lambda: RoutePreparationSnapshot(owner, 1, (), ())
    )
    try:
        context = controller.await_preparation_context()
        common = _common(
            "Функция Посчитать() Экспорт\nВозврат 1;\nКонецФункции\n"
            "Итог = Посчитать();",
            parser,
        )
        with pytest.raises(ValueError, match="Worker methods require an artifact"):
            context.policy.prepare(common, context.capabilities.for_pipeline(), context)
        assert session.calls == []
    finally:
        arbiter.close(timeout=3)
