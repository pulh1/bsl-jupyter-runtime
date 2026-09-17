"""One confirmed CAPTURE cell error does not end its stopped MAIN frame."""

from hashlib import sha256
from threading import current_thread

import pytest

from onec_runtime.capture_evaluation import AdmissionEnvelopeV1
from onec_runtime.errors import BslExecutionError
from onec_runtime.execution.arbiter import RdbgArbiter, RouteToken
from onec_runtime.execution.capture.cell_evaluator import CaptureCellEvaluator
from onec_runtime.execution.capture.executor import CaptureExecutor
from onec_runtime.execution.capture.operation_executor import ConfirmedTemporaryKeyCleanupFailure
from onec_runtime.execution.capture.scope import CaptureContextState, CaptureFrameIdentity
from onec_runtime.execution.controller.controller import (
    ExecutionController,
    MainYieldKind,
)
from onec_runtime.execution.main import MainExecutor, MainPhase
from onec_runtime.rdbg.models import EvaluationResult
from onec_runtime.stop_routing import BreakpointRegistry
from onec_runtime.table_value import evaluation_to_python

from test_capture_materialization_executor import transfer_plan
from test_execution_controller_routes import CompleteSession
from test_execution_route_sequence import BUSINESS, KERNEL


class RecoverySession(CompleteSession):
    """Reply to exact test expressions while retaining RouteSession's trace."""

    def __init__(self, *, cleanup_fail_once: bool = False) -> None:
        super().__init__()
        self.started: list[str] = []
        self.modified: list[tuple[str, str]] = []
        self.envelope = AdmissionEnvelopeV1(7, 1, 1, sha256(b"a").hexdigest(), 4)
        self.cleanup_fail_once = cleanup_fail_once

    def start_evaluation(self, expression, **kwargs):
        self.started.append(expression)
        return super().start_evaluation(expression, **kwargs)

    def modify(self, variable, value_expression, *, on_transport_dispatch):
        self.modified.append((variable, value_expression))
        return super().modify(
            variable, value_expression,
            on_transport_dispatch=on_transport_dispatch,
        )

    def wait_evaluation_event(self, pending, *, timeout_s, on_transport_dispatch):
        expression = self.expression
        if "Результат = Падение;" in expression:
            on_transport_dispatch()
            self._record("wait_eval")
            self.pending = None
            return EvaluationResult(
                pending.result_id, "Ошибка", "", True, "confirmed BSL error"
            )
        if "Результат = 2;" in expression:
            on_transport_dispatch()
            self._record("wait_eval")
            self.pending = None
            return EvaluationResult(pending.result_id, "Число", "2", False)
        if "ЗабратьКомпактнуюМатериализациюИзКонтекста" in expression:
            on_transport_dispatch()
            self._record("wait_eval")
            self.pending = None
            return EvaluationResult(pending.result_id, "Строка", '"YQ=="', False)
        if "Контекст.Удалить" in expression and "__onec_compact_table_" in expression:
            on_transport_dispatch()
            self._record("wait_eval")
            self.pending = None
            if self.cleanup_fail_once:
                self.cleanup_fail_once = False
                return EvaluationResult(
                    pending.result_id, "Ошибка", "", True, "cleanup rejected"
                )
            return EvaluationResult(pending.result_id, "Булево", "Истина", False)
        if "admission" in expression:
            on_transport_dispatch()
            self._record("wait_eval")
            self.pending = None
            return EvaluationResult(
                pending.result_id, "Строка", self.envelope.encode(), False
            )
        return super().wait_evaluation_event(
            pending,
            timeout_s=timeout_s,
            on_transport_dispatch=on_transport_dispatch,
        )


def _user_result(result: EvaluationResult) -> object:
    if result.error_occurred:
        raise BslExecutionError(result.error_text)
    return evaluation_to_python(result)


def test_capture_error_retry_inspect_materialize_resume_same_main_and_scope() -> None:
    session = RecoverySession()
    arbiter = RdbgArbiter(session, RouteToken("runtime-1", 1, 0, "main"))
    controller = ExecutionController(
        arbiter,
        MainExecutor(poll_interval_s=0.1),
        CaptureExecutor(None, KERNEL, decode_command_id=evaluation_to_python),
        CaptureCellEvaluator(),
        BreakpointRegistry(KERNEL, (BUSINESS,)),
        runtime_generation=1,
    )
    try:
        captured = controller.submit_main("Результат = 1;").wait(3)
        assert captured.kind is MainYieldKind.CAPTURE
        main = captured.operation
        scope = captured.scope
        assert main.phase is MainPhase.SUSPENDED_CAPTURE

        failed = controller.submit_capture_cell(
            "Результат = Падение;",
            dirty_roots=("Результат",),
            result_policy=_user_result,
        )
        with pytest.raises(BslExecutionError, match="confirmed BSL error"):
            failed.wait(3)
        assert failed.status().settled
        assert controller.capture_scope is scope
        assert controller.main_operation is main
        assert scope.context_state is CaptureContextState.READY
        assert scope.frame_identity is CaptureFrameIdentity.CONFIRMED
        assert scope.dirty_roots == ("Результат",)
        assert main.phase is MainPhase.SUSPENDED_CAPTURE

        corrected = controller.submit_capture_cell(
            "Результат = 2;", result_policy=_user_result
        ).wait(3)
        assert corrected == 2
        assert controller.capture_scope is scope
        assert main.phase is MainPhase.SUSPENDED_CAPTURE

        variable = controller.submit_capture_variable("Amount", stack_level=0).wait(3)
        assert variable.name == "Amount"
        assert controller.capture_scope is scope

        key = "__onec_compact_table_" + "a" * 32
        payload = controller.submit_capture_materialization(transfer_plan(key)).wait(3)
        assert payload == b"a"
        assert scope.temporary_cleanup_debts == ()
        assert controller.capture_scope is scope

        resumed = controller.submit_resume().wait(3)
        assert resumed.kind is MainYieldKind.COMPLETED
        assert resumed.operation is main
        assert main.phase is MainPhase.COMPLETED
        assert scope.frame_identity is CaptureFrameIdentity.RELEASED
        assert scope.writeback_ledger.record("Результат").phase.value == "succeeded"
        assert controller.capture_scope is None

        assert sum("Результат = Падение;" in source for source in session.started) == 1
        assert sum("Результат = 2;" in source for source in session.started) == 1
        assert sum("admission" in source for source in session.started) == 1
        assert sum("ЗабратьКомпактнуюМатериализациюИзКонтекста" in source for source in session.started) == 1
        assert sum("Контекст.Удалить" in source and key in source for source in session.started) == 1
        assert [root for root, _ in session.modified].count("Результат") == 1
        call_names = [name for name, _ in session.calls]
        assert call_names.count("continue") == 2
        assert call_names.count("modify") == 3
        assert max(i for i, name in enumerate(call_names) if name == "modify") < max(
            i for i, name in enumerate(call_names) if name == "continue"
        )
        owner_threads = {thread for _, thread in session.calls}
        assert len(owner_threads) == 1
        assert next(iter(owner_threads)).name == "rdbg-arbiter"
        assert current_thread() not in owner_threads
    finally:
        arbiter.close(timeout=3)


def test_failed_materialization_cleanup_is_repairable_in_same_capture_stop() -> None:
    session = RecoverySession(cleanup_fail_once=True)
    arbiter = RdbgArbiter(session, RouteToken("runtime-1", 1, 0, "main"))
    controller = ExecutionController(
        arbiter,
        MainExecutor(poll_interval_s=0.1),
        CaptureExecutor(None, KERNEL, decode_command_id=evaluation_to_python),
        CaptureCellEvaluator(),
        BreakpointRegistry(KERNEL, (BUSINESS,)),
        runtime_generation=1,
    )
    try:
        scope = controller.submit_main("Результат = 1;").wait(3).scope
        key = "__onec_compact_table_" + "a" * 32
        with pytest.raises(ConfirmedTemporaryKeyCleanupFailure):
            controller.submit_capture_materialization(transfer_plan(key)).wait(3)
        assert controller.capture_scope is scope
        assert scope.context_state is CaptureContextState.READY
        assert scope.temporary_cleanup_debts[0].can_retry_delete

        controller.submit_capture_cleanup_retry(key).wait(3)
        assert scope.temporary_cleanup_debts == ()
        assert controller.submit_capture_cell(
            "Результат = 2;", result_policy=_user_result
        ).wait(3) == 2
        assert controller.submit_resume().wait(3).kind is MainYieldKind.COMPLETED
        assert sum("admission" in source for source in session.started) == 1
        assert sum("Контекст.Удалить" in source and key in source for source in session.started) == 2
        assert len({thread for _, thread in session.calls}) == 1
    finally:
        arbiter.close(timeout=3)
