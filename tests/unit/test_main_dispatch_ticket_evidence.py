"""MAIN dispatch evidence belongs to the exact accepted arbiter ticket."""

from __future__ import annotations

from threading import Event

import pytest
from arbiter_test_cleanup import confirm_test_server_terminated

from onec_runtime.errors import ProtocolError, RdbgTransportTimeout
from onec_runtime.execution.contracts import Accepted, SubmissionReceipt
from onec_runtime.execution.main import MainPhase
from onec_runtime.execution.snapshot_binding import RoutePreparationSnapshot
from onec_runtime.rdbg.models import ModifyResult

from test_controller_preparation_route import _prepared, _runtime
from test_execution_controller_routes import CompleteSession


def test_main_ticket_is_pending_until_its_own_continue_transport_entry() -> None:
    entered = Event()
    release = Event()

    class PausedContinue(CompleteSession):
        def continue_(self, *, on_transport_dispatch):
            entered.set()
            assert release.wait(3)
            return super().continue_(on_transport_dispatch=on_transport_dispatch)

    owner = object()
    controller, arbiter, _session, _parser = _runtime(
        lambda: RoutePreparationSnapshot(owner, 1, (), ()),
        session=PausedContinue(),
    )
    ticket = None
    try:
        ticket = controller.submit_main("Результат = 1;")
        assert entered.wait(3)
        assert controller.main_dispatch_evidence(ticket) is None
        release.set()
        ticket.wait_settled(3)
        assert controller.main_dispatch_evidence(ticket) is True
    finally:
        release.set()
        if ticket is not None:
            try:
                ticket.wait_settled(3)
            except Exception:
                pass
        arbiter.close(timeout=3)


def test_confirmed_main_write_failure_proves_continue_was_never_entered() -> None:
    from uuid import UUID

    class FailedWrite(CompleteSession):
        def modify(self, variable, value_expression, *, on_transport_dispatch):
            if variable == "ТекущаяИнструкция":
                on_transport_dispatch()
                self._record("modify")
                return ModifyResult(UUID(int=4), "Булево", "Ложь", True, "denied")
            return super().modify(
                variable, value_expression, on_transport_dispatch=on_transport_dispatch,
            )

    owner = object()
    controller, arbiter, _session, _parser = _runtime(
        lambda: RoutePreparationSnapshot(owner, 1, (), ()),
        session=FailedWrite(),
    )
    try:
        ticket = controller.submit_main("Результат = 1;")
        with pytest.raises(Exception):
            ticket.wait_settled(3)
        assert controller.main_dispatch_evidence(ticket) is False
    finally:
        arbiter.close(timeout=3)


@pytest.mark.parametrize("ambiguous_field", ["ТекущаяИнструкция", "ИдентификаторКоманды"])
def test_ambiguous_main_command_write_keeps_operation_live(
    ambiguous_field: str,
) -> None:
    class AmbiguousWrite(CompleteSession):
        def modify(self, variable, value_expression, *, on_transport_dispatch):
            if variable == ambiguous_field:
                on_transport_dispatch()
                raise RdbgTransportTimeout("command write outcome is unknown")
            return super().modify(
                variable, value_expression, on_transport_dispatch=on_transport_dispatch,
            )

    owner = object()
    controller, arbiter, session, _parser = _runtime(
        lambda: RoutePreparationSnapshot(owner, 1, (), ()),
        session=AmbiguousWrite(),
    )
    ticket = None
    try:
        ticket = controller.submit_main("Результат = 1;")
        assert ticket.wait_unknown(3)
        operation = controller.main_operation
        assert operation is not None
        assert operation.phase is MainPhase.UNKNOWN
        assert not operation.terminal
        assert operation.command_dispatch_attempted is False
        with pytest.raises(ProtocolError, match="MAIN command is still active"):
            controller.submit_main("Результат = 2;")
    finally:
        if ticket is not None and ticket.status().phase == "unknown":
            confirm_test_server_terminated(
                arbiter, ticket, arbiter.current_route, session,
                session.target.target_id,
            )
        arbiter.close(timeout=3)


@pytest.mark.parametrize("with_statement", [False, True])
def test_worker_ticket_distinguishes_activation_from_user_main_dispatch(
    with_statement: bool,
) -> None:
    entered = Event()
    release = Event()

    class Lease:
        def release(self, *, port):
            pass

        def retain_outcome_unknown(self, *, port):
            pass

    class Activation:
        def pin_active(self, *, port):
            return None

        def activate(self, intent, *, port):
            entered.set()
            assert release.wait(3)
            return Lease()

    owner = object()
    controller, arbiter, _session, parser = _runtime(
        lambda: RoutePreparationSnapshot(owner, 1, (), ()),
        worker_activation=Activation(),
    )
    ticket = None
    try:
        source = "Функция Посчитать() Экспорт\nВозврат 1;\nКонецФункции\n"
        if with_statement:
            source += "Итог = Посчитать();"
        context, prepared = _prepared(controller, parser, source)
        receipt = SubmissionReceipt()
        accepted = controller.submit_cell(
            context, prepared, context.capabilities.for_pipeline().guards, receipt,
        )
        assert isinstance(accepted, Accepted)
        ticket = accepted.ticket
        assert receipt.ticket is ticket
        assert entered.wait(3)
        assert controller.main_dispatch_evidence(ticket) is None
        release.set()
        ticket.wait_settled(3)
        assert controller.main_dispatch_evidence(ticket) is with_statement
    finally:
        release.set()
        if ticket is not None:
            try:
                ticket.wait_settled(3)
            except Exception:
                pass
        arbiter.close(timeout=3)


def test_worker_only_ticket_does_not_inherit_prior_main_dispatch_evidence() -> None:
    class Lease:
        def release(self, *, port):
            pass

        def retain_outcome_unknown(self, *, port):
            pass

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
        first = controller.submit_main("Результат = 1;")
        first.wait_settled(3)
        controller.submit_resume().wait_settled(3)
        assert controller.main_dispatch_evidence(first) is True

        context, prepared = _prepared(
            controller, parser,
            "Функция Посчитать() Экспорт\nВозврат 1;\nКонецФункции\n",
        )
        receipt = SubmissionReceipt()
        accepted = controller.submit_cell(
            context, prepared, context.capabilities.for_pipeline().guards, receipt,
        )
        assert isinstance(accepted, Accepted)
        assert accepted.ticket.wait_settled(3) is None
        assert controller.main_dispatch_evidence(accepted.ticket) is False
        assert controller.main_dispatch_evidence(first) is True
    finally:
        arbiter.close(timeout=3)


def test_main_only_prepared_claim_rejects_capture_context_and_payload() -> None:
    owner = object()
    controller, arbiter, _session, parser = _runtime(
        lambda: RoutePreparationSnapshot(owner, 1, (), ()),
        capture_snapshot_provider=lambda _operation: RoutePreparationSnapshot(
            owner, 2, (), (),
        ),
    )
    try:
        main_context, main_prepared = _prepared(
            controller, parser, "Результат = 1;",
        )
        controller.require_main_prepared_cell(main_context, main_prepared)
        controller.submit_main("Результат = 1;").wait_settled(3)

        capture_context, capture_prepared = _prepared(
            controller, parser, "Результат = 2;",
        )
        with pytest.raises(ProtocolError, match="MAIN"):
            controller.require_main_prepared_cell(
                capture_context, capture_prepared,
            )
        with pytest.raises(ProtocolError, match="MAIN"):
            controller.require_main_prepared_cell(
                main_context, capture_prepared,
            )
    finally:
        arbiter.close(timeout=3)
