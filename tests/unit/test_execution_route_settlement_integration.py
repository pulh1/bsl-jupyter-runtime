"""Public reply publication from the single-owner execution component."""

from onec_runtime.bsl.source_maps import SourceUnitKind, SourceUnitRef, source_sha256
from onec_runtime.execution.composition import build_execution_core
from onec_runtime.execution.contracts import Accepted, SubmissionReceipt
from onec_runtime.execution.namespace import RuntimeNamespaceOwner
from onec_runtime.execution.settlement import RouteSettlementService
from onec_runtime.execution.worker_activation import WorkerActivationSnapshot
from onec_runtime.runtime_models import RuntimeReplyKind
from onec_runtime.rdbg.models import EvaluationResult
from onec_runtime.errors import EvaluationDispatchUnknown

from test_execution_controller_routes import BUSINESS, KERNEL, CompleteSession


class _Replies:
    def diagnostic_reply(self, diagnostic):
        raise AssertionError(diagnostic.message)

    def unavailable_reply(self, unavailable):
        raise AssertionError(unavailable.reason)


def test_main_publication_survives_capture_and_commits_names_on_completion() -> None:
    namespace = RuntimeNamespaceOwner(
        1, 1, worker_snapshot=lambda: WorkerActivationSnapshot(0, (), None, None)
    )
    settlement = RouteSettlementService(namespace)
    session = CompleteSession(capture_count=1)

    def capture_snapshot(operation):
        return namespace.snapshot(
            speculative_names=settlement.pending_main_names(operation)
        )

    core = build_execution_core(
        session, KERNEL,
        runtime_generation=1,
        capture_locations=(BUSINESS,),
        snapshot_provider=namespace.snapshot,
        capture_snapshot_provider=capture_snapshot,
        reply_presenter=_Replies(),
        settlement_services=settlement,
    )
    source = "НоваяПеременная = 1;"
    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL, "publication", 1, source_sha256(source)
    )
    try:
        captured = core.pipeline.execute(source, unit)
        assert captured.kind is RuntimeReplyKind.CAPTURED
        assert namespace.namespace_snapshot().names == ()
        context = core.controller.await_preparation_context()
        assert "НоваяПеременная" in context.capabilities.namespace_names

        capture_source = "РезультатИнструкции = 2;"
        capture_unit = SourceUnitRef(
            SourceUnitKind.NOTEBOOK_CELL, "publication-capture", 1,
            source_sha256(capture_source),
        )
        capture_reply = core.pipeline.execute(capture_source, capture_unit)
        assert capture_reply.kind is RuntimeReplyKind.CAPTURE_CELL
        assert capture_reply.succeeded
        assert "НоваяПеременная" not in namespace.namespace_snapshot().names

        completed = core.controller.submit_resume().wait_settled(3)

        assert completed.kind is RuntimeReplyKind.MAIN_COMPLETED
        assert "НоваяПеременная" in namespace.namespace_snapshot().names
    finally:
        core.arbiter.close(timeout=3)


def test_confirmed_main_decode_failure_settles_failed_reply_without_namespace_commit() -> None:
    class BadResultSession(CompleteSession):
        def wait_evaluation_event(self, pending, *, timeout_s, on_transport_dispatch):
            if self.expression == "Результат":
                on_transport_dispatch()
                self._record("wait_eval")
                self.pending = None
                return EvaluationResult(pending.result_id, "Число", "bad", False)
            return super().wait_evaluation_event(
                pending, timeout_s=timeout_s, on_transport_dispatch=on_transport_dispatch,
            )

    namespace = RuntimeNamespaceOwner(
        1, 1, worker_snapshot=lambda: WorkerActivationSnapshot(0, (), None, None)
    )
    session = BadResultSession(capture_count=0)
    core = build_execution_core(
        session, KERNEL, runtime_generation=1, capture_locations=(BUSINESS,),
        snapshot_provider=namespace.snapshot, reply_presenter=_Replies(),
        settlement_services=RouteSettlementService(namespace),
    )
    source = "НоваяПеременная = 1;"
    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL, "decode-failure", 1, source_sha256(source)
    )
    try:
        reply = core.pipeline.execute(source, unit)
        assert reply.kind is RuntimeReplyKind.MAIN_COMPLETED
        assert not reply.succeeded
        assert namespace.namespace_snapshot().names == ()
    finally:
        core.arbiter.close(timeout=3)


def test_worker_only_cell_publishes_confirmed_generation_handle() -> None:
    handle = object()

    class Lease:
        def __init__(self):
            self.handle = handle

        def release(self, *, port):
            pass

        def retain_outcome_unknown(self, *, port):
            raise AssertionError("Confirmed Worker activation cannot become unknown")

    class Activation:
        def activate(self, intent, *, port):
            return Lease()

        def pin_active(self, *, port):
            return None

    namespace = RuntimeNamespaceOwner(
        1, 1, worker_snapshot=lambda: WorkerActivationSnapshot(0, (), None, None)
    )
    core = build_execution_core(
        CompleteSession(), KERNEL, runtime_generation=1,
        capture_locations=(BUSINESS,), snapshot_provider=namespace.snapshot,
        reply_presenter=_Replies(),
        settlement_services=RouteSettlementService(namespace),
        worker_activation=Activation(),
    )
    source = "Функция Вычислить() Экспорт\nВозврат 1;\nКонецФункции"
    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL, "worker-only", 1, source_sha256(source)
    )
    try:
        reply = core.pipeline.execute(source, unit)
        assert reply.kind is RuntimeReplyKind.WORKER_LOADED
        assert reply.result is handle
    finally:
        core.arbiter.close(timeout=3)


def test_pending_capture_message_eval_reconciles_without_repeat_dispatch() -> None:
    from onec_runtime.execution.common import NotebookCommonParser

    class AmbiguousMessages(CompleteSession):
        def __init__(self):
            super().__init__()
            self.message_dispatches = 0

        def start_evaluation(self, expression, **kwargs):
            pending = super().start_evaluation(expression, **kwargs)
            if expression.startswith(
                "RuntimeKernelServer.ЗабратьСообщенияЯчейкиИзКонтекста("
            ):
                self.message_dispatches += 1
                if self.message_dispatches == 1:
                    raise EvaluationDispatchUnknown(pending)
            return pending

    namespace = RuntimeNamespaceOwner(
        1, 1, worker_snapshot=lambda: WorkerActivationSnapshot(0, (), None, None)
    )
    settlement = RouteSettlementService(namespace)
    session = AmbiguousMessages()
    core = build_execution_core(
        session, KERNEL, runtime_generation=1, capture_locations=(BUSINESS,),
        snapshot_provider=namespace.snapshot,
        capture_snapshot_provider=lambda operation: namespace.snapshot(
            speculative_names=settlement.pending_main_names(operation)
        ),
        reply_presenter=_Replies(), settlement_services=settlement,
    )
    try:
        core.controller.submit_main("Результат = 1;").wait_settled(3)
        source = "РезультатИнструкции = 2;"
        unit = SourceUnitRef(
            SourceUnitKind.NOTEBOOK_CELL, "pending-messages", 1, source_sha256(source)
        )
        context = core.controller.await_preparation_context()
        common = NotebookCommonParser(core.parser_target).prepare(source, unit)
        prepared = context.policy.prepare(
            common, context.capabilities.for_pipeline(), context
        )
        receipt = SubmissionReceipt()
        accepted = core.controller.submit_cell(
            context, prepared, context.capabilities.for_pipeline().guards, receipt
        )
        assert isinstance(accepted, Accepted)
        assert accepted.ticket.wait_unknown(3)

        core.controller.reconcile_capture_pending_eval(accepted.ticket)
        reply = accepted.ticket.wait_settled(3)

        assert reply.kind is RuntimeReplyKind.CAPTURE_CELL
        assert reply.succeeded
        assert session.message_dispatches == 1
        assert core.controller.capture_scope is not None
    finally:
        core.arbiter.close(timeout=3)


def test_capture_bsl_error_keeps_scope_for_corrected_cell() -> None:
    class ErrorOnce(CompleteSession):
        def __init__(self):
            super().__init__()
            self.capture_evaluations = 0

        def wait_evaluation_event(self, pending, *, timeout_s, on_transport_dispatch):
            if self.expression.startswith("RuntimeKernelServer.ВыполнитьКодВКонтекстеОтладки("):
                self.capture_evaluations += 1
                if self.capture_evaluations == 1:
                    on_transport_dispatch()
                    self._record("wait_eval")
                    self.pending = None
                    return EvaluationResult(
                        pending.result_id, "Ошибка", "", True, "planned BSL error"
                    )
            return super().wait_evaluation_event(
                pending, timeout_s=timeout_s, on_transport_dispatch=on_transport_dispatch,
            )

    namespace = RuntimeNamespaceOwner(
        1, 1, worker_snapshot=lambda: WorkerActivationSnapshot(0, (), None, None)
    )
    settlement = RouteSettlementService(namespace)
    core = build_execution_core(
        ErrorOnce(), KERNEL, runtime_generation=1, capture_locations=(BUSINESS,),
        snapshot_provider=namespace.snapshot,
        capture_snapshot_provider=lambda operation: namespace.snapshot(
            speculative_names=settlement.pending_main_names(operation)
        ),
        reply_presenter=_Replies(), settlement_services=settlement,
    )

    def run(source: str, revision: int):
        unit = SourceUnitRef(
            SourceUnitKind.NOTEBOOK_CELL, "capture-repair", revision,
            source_sha256(source),
        )
        return core.pipeline.execute(source, unit)

    try:
        assert run("Результат = 1;", 1).kind is RuntimeReplyKind.CAPTURED
        scope = core.controller.capture_scope
        failed = run("ВызватьИсключение \"ошибка\";", 2)
        assert failed.kind is RuntimeReplyKind.CAPTURE_CELL
        assert not failed.succeeded
        assert core.controller.capture_scope is scope

        corrected = run("РезультатИнструкции = 2;", 3)
        assert corrected.kind is RuntimeReplyKind.CAPTURE_CELL
        assert corrected.succeeded
        assert core.controller.capture_scope is scope
    finally:
        core.arbiter.close(timeout=3)
