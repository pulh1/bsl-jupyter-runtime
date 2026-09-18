"""Completion schema helpers use one fenced MAIN or CAPTURE arbiter ticket."""

import pytest
from uuid import UUID

from onec_runtime.errors import ProtocolError
from onec_runtime.execution.arbiter import RdbgArbiter, RouteToken
from onec_runtime.execution.capture.cell_evaluator import CaptureCellEvaluator
from onec_runtime.execution.capture.executor import CaptureExecutor
from onec_runtime.execution.completion_fields import (
    CompletionFieldsPlan, CompletionFieldsService,
)
from onec_runtime.execution.controller.controller import ExecutionController
from onec_runtime.execution.main import MainExecutor
from onec_runtime.execution.worker_activation import WorkerMaterializationSnapshot
from onec_runtime.rdbg.models import EvaluationResult, TargetId
from onec_runtime.runtime_api import RuntimeNamespaceSnapshot
from onec_runtime.stop_routing import BreakpointRegistry
from onec_runtime.table_value import evaluation_to_python

from test_execution_completion_fields import schema
from test_execution_controller_routes import BUSINESS, KERNEL, CompleteSession
from test_execution_route_sequence import TARGET


_HELPER = "СериализоватьДопущенныеИменаСвойствДляПодсказки"


class CompletionSession(CompleteSession):
    def __init__(self, wire: str) -> None:
        super().__init__()
        self.helper_wire = wire
        self.helper_expressions: list[tuple[str, int]] = []

    def start_evaluation(
        self, expression, *, max_text_size, stack_level, timeout_s,
        on_transport_dispatch,
    ):
        if _HELPER in expression:
            self.helper_expressions.append((expression, stack_level))
        return super().start_evaluation(
            expression,
            max_text_size=max_text_size,
            stack_level=stack_level,
            timeout_s=timeout_s,
            on_transport_dispatch=on_transport_dispatch,
        )

    def wait_evaluation_event(self, pending, *, timeout_s, on_transport_dispatch):
        if _HELPER not in self.expression:
            return super().wait_evaluation_event(
                pending, timeout_s=timeout_s,
                on_transport_dispatch=on_transport_dispatch,
            )
        on_transport_dispatch()
        self._record("wait_eval")
        self.pending = None
        return EvaluationResult(
            pending.result_id,
            "Строка",
            '"private debugger presentation"',
            False,
            value_string=self.helper_wire,
        )


def _bound(wire: str):
    session = CompletionSession(wire)
    arbiter = RdbgArbiter(session, RouteToken("completion-test", 1, 0, "main"))
    controller = ExecutionController(
        arbiter,
        MainExecutor(poll_interval_s=0.1),
        CaptureExecutor(None, KERNEL, decode_command_id=evaluation_to_python),
        CaptureCellEvaluator(),
        BreakpointRegistry(KERNEL, (BUSINESS,)),
        runtime_generation=1,
        initial_target_id=TARGET,
    )
    return session, arbiter, controller


def _service(controller, *, namespace=None):
    return CompletionFieldsService(
        controller,
        namespace_snapshot=namespace or (
            lambda: RuntimeNamespaceSnapshot(1, 2, ("Данные",))
        ),
        worker_catalog_snapshot=lambda: WorkerMaterializationSnapshot(0, ()),
    )


def test_main_idle_completion_uses_one_arbiter_worker_and_private_policy() -> None:
    session, arbiter, controller = _bound(schema("Номер", "Название"))
    try:
        assert _service(controller).completion_fields("e1cRuntimeКонтекст.Данные") == (
            "Номер", "Название",
        )
        assert len(session.helper_expressions) == 1
        expression, level = session.helper_expressions[0]
        assert expression.startswith("RuntimeValueTransferServer.")
        assert level == 0
        assert "private" not in str(session.helper_expressions)
        assert {thread for _, thread in session.calls} == {arbiter._worker}
    finally:
        arbiter.close(timeout=3)


def test_capture_ready_completion_uses_kernel_frame_and_retains_stop() -> None:
    session, arbiter, controller = _bound(schema("Номер"))
    try:
        controller.submit_main("Результат = 1;").wait_settled(3)
        scope = controller.capture_scope
        assert scope is not None

        assert _service(controller).completion_fields("e1cRuntimeКонтекст.Данные") == ("Номер",)
        assert len(session.helper_expressions) == 1
        expression, level = session.helper_expressions[0]
        assert "ВыполнитьКодВКонтекстеОтладки" in expression
        assert level == scope.kernel_stack_level
        assert controller.capture_scope is scope
        assert {thread for _, thread in session.calls} == {arbiter._worker}
    finally:
        arbiter.close(timeout=3)


def test_completion_rechecks_namespace_on_worker_before_any_rdbg_effect() -> None:
    session, arbiter, controller = _bound(schema("Номер"))
    reads = [0]

    def namespace():
        reads[0] += 1
        return RuntimeNamespaceSnapshot(1, reads[0] + 1, ("Данные",))

    try:
        with pytest.raises(ProtocolError, match="namespace or Worker catalog changed"):
            _service(controller, namespace=namespace).completion_fields("e1cRuntimeКонтекст.Данные")
        assert session.helper_expressions == []
        assert session.calls == []
    finally:
        arbiter.close(timeout=3)


def test_completion_rejects_stale_capture_scope_before_dispatch() -> None:
    session, arbiter, controller = _bound(schema("Номер"))
    try:
        controller.submit_main("Результат = 1;").wait_settled(3)
        scope = controller.capture_scope
        assert scope is not None
        scope.mark_closed()
        prior_calls = tuple(session.calls)

        with pytest.raises(ProtocolError):
            _service(controller).completion_fields("e1cRuntimeКонтекст.Данные")
        assert tuple(session.calls) == prior_calls
    finally:
        arbiter.close(timeout=3)


def test_completion_rejects_private_schema_without_returning_raw_result() -> None:
    session, arbiter, controller = _bound("private target payload")
    try:
        with pytest.raises(ProtocolError) as failure:
            _service(controller).completion_fields("e1cRuntimeКонтекст.Данные")
        assert "private" not in str(failure.value)
        assert len(session.helper_expressions) == 1
        assert {thread for _, thread in session.calls} == {arbiter._worker}
    finally:
        arbiter.close(timeout=3)


def test_completion_rejects_changed_main_target_inside_ticket_before_rdbg() -> None:
    session, arbiter, controller = _bound(schema("Номер"))

    def change_target() -> None:
        controller._initial_target_id = TargetId(UUID(int=2), "different-target")

    plan = CompletionFieldsPlan(
        "e1cRuntimeКонтекст.Данные", "Результат = e1cRuntimeКонтекст.Данные;", change_target,
    )
    try:
        with pytest.raises(ProtocolError, match="fence changed"):
            controller.submit_completion_helper(plan).wait_settled(3)
        assert session.calls == []
    finally:
        arbiter.close(timeout=3)


def test_completion_rejects_active_successor_admission_before_rdbg() -> None:
    session, arbiter, controller = _bound(schema("Номер"))
    try:
        controller.submit_main("Результат = 1;").wait_settled(3)
        prior_calls = tuple(session.calls)
        controller._continuation_admission = object()

        with pytest.raises(ProtocolError, match="continuation prevents completion"):
            _service(controller).completion_fields("e1cRuntimeКонтекст.Данные")
        assert tuple(session.calls) == prior_calls
    finally:
        arbiter.close(timeout=3)
