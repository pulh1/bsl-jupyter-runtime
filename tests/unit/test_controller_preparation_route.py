"""Controller-selected statement preparation has one fenced admission path."""

from threading import Event, get_ident

import pytest

from onec_runtime.bsl.parser_target import PythonParserTarget
from onec_runtime.bsl.source_maps import SourceUnitKind, SourceUnitRef, source_sha256
from onec_runtime.execution.arbiter import (
    CancelledBeforeEffect, RdbgArbiter, RouteToken, Settlement,
)
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


def _runtime(snapshot_provider, *, session=None):
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
    )
    return controller, arbiter, session, parser


def _prepared(controller, parser, source: str):
    context = controller.await_preparation_context()
    common = _common(source, parser)
    prepared = context.policy.prepare(common, context.capabilities.for_pipeline(), context)
    assert isinstance(prepared, PreparedCell)
    return context, prepared


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

    def provider():
        if get_ident() != caller_thread:
            entered.set()
            assert release.wait(3)
        return RoutePreparationSnapshot(owner, version[0], (), ())

    controller, arbiter, session, parser = _runtime(provider)
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
        assert controller.main_operation.phase is MainPhase.FAILED_BEFORE_DISPATCH
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


def test_main_in_flight_reports_temporary_unavailable_without_blocking_caller() -> None:
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
    try:
        ticket = controller.submit_main("Результат = 1;")
        assert entered.wait(3)
        unavailable = controller.await_preparation_context()
        assert isinstance(unavailable, Unavailable)
        assert "active" in unavailable.reason
        release.set()
        assert ticket.wait_initiator().kind.value == "capture"
    finally:
        release.set()
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
