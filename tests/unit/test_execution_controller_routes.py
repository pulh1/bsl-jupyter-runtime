"""The controller keeps one MAIN operation across a CAPTURE stop."""

from collections import deque
from hashlib import sha256
from threading import Event
from uuid import UUID

import pytest

from onec_runtime.execution.arbiter import RdbgArbiter, RouteToken, Settlement
from onec_runtime.execution.capture.cell_evaluator import CaptureCellEvaluator
from onec_runtime.execution.capture.executor import CaptureExecutor
from onec_runtime.execution.main import MainExecutor, MainPhase
from onec_runtime.rdbg.models import EvaluationResult, LocalVariablesResult, StopEvent
from onec_runtime.stop_routing import BreakpointRegistry
from onec_runtime.table_value import evaluation_to_python

from test_execution_route_sequence import (
    BUSINESS,
    CAPTURE_STOP,
    KERNEL,
    MAIN_STOP,
    RouteSession,
    TARGET,
)


class CompleteSession(RouteSession):
    def __init__(self, capture_count: int = 1) -> None:
        super().__init__()
        self.stops = deque(
            [
                StopEvent(
                    CAPTURE_STOP.target_id,
                    CAPTURE_STOP.location,
                    CAPTURE_STOP.reason,
                    stop_by_breakpoint=True,
                    stack=CAPTURE_STOP.stack,
                    stack_frames=CAPTURE_STOP.stack_frames,
                )
                for _ in range(capture_count)
            ]
            + [StopEvent(TARGET, KERNEL, MAIN_STOP.reason, stop_by_breakpoint=True)]
        )

    def wait_evaluation_event(self, pending, *, timeout_s, on_transport_dispatch):
        on_transport_dispatch()
        if self.expression.startswith("RuntimeKernelServer.ПоместитьЗначениеКонтекстаОтладки("):
            self._record("wait_eval")
            self.pending = None
            return EvaluationResult(
                pending.result_id, "Строка", '"root-address"', False,
                value_string="root-address",
            )
        if self.expression == "ЗавершеннаяКоманда":
            self._record("wait_eval")
            self.pending = None
            return EvaluationResult(pending.result_id, "Число", "1", False)
        if self.expression == "Результат":
            self._record("wait_eval")
            self.pending = None
            return EvaluationResult(pending.result_id, "Число", "3", False)
        if self.expression == "Ошибка":
            self._record("wait_eval")
            self.pending = None
            return EvaluationResult(pending.result_id, "Строка", '""', False)
        if self.expression.startswith(
            "RuntimeKernelServer.ЗабратьСообщенияЯчейкиИзКонтекста("
        ):
            self._record("wait_eval")
            self.pending = None
            return EvaluationResult(
                pending.result_id, "Строка", '"[]"', False,
                value_string="[]",
            )
        return super().wait_evaluation_event(
            pending, timeout_s=timeout_s,
            on_transport_dispatch=lambda: None,
        )


def test_capture_variable_page_uses_owned_ticket_and_safe_names() -> None:
    from onec_runtime.execution.controller.controller import ExecutionController

    session = CompleteSession()
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
        controller.submit_main("Результат = 1;").wait_settled(3)
        page = controller.submit_capture_variable_page(
            stack_level=0, start=0, stop=10,
        ).wait_settled(3)
        assert page.names == ("Amount",)
        assert page.total == 1
        assert {thread for _, thread in session.calls} == {arbiter._worker}
    finally:
        arbiter.close(timeout=3)


def test_controller_runs_main_capture_cell_resume_and_completion_with_one_owner() -> None:
    from onec_runtime.execution.controller.controller import (
        ExecutionController,
        MainYieldKind,
    )

    session = CompleteSession()
    route = RouteToken("runtime-1", 1, 0, "main")
    arbiter = RdbgArbiter(session, route)
    controller = ExecutionController(
        arbiter,
        MainExecutor(poll_interval_s=0.1),
        CaptureExecutor(None, KERNEL, decode_command_id=evaluation_to_python),
        CaptureCellEvaluator(),
        BreakpointRegistry(KERNEL, (BUSINESS,)),
        runtime_generation=1,
    )
    try:
        main_ticket = controller.submit_main("Результат = 1;")
        first = main_ticket.wait(3)
        assert first.kind is MainYieldKind.CAPTURE
        main = controller.main_operation
        assert main is not None and main.command_id == 1
        assert main.phase is MainPhase.SUSPENDED_CAPTURE
        scope = controller.capture_scope
        assert scope is first.scope and scope.published

        cell_ticket = controller.submit_capture_cell("Результат = 2;")
        cell_result = cell_ticket.wait(3)
        assert cell_result.error_occurred is False
        assert controller.main_operation is main
        assert controller.capture_scope is scope

        resume_ticket = controller.submit_resume()
        completed = resume_ticket.wait(3)
        assert completed.kind is MainYieldKind.COMPLETED
        assert completed.completion.result == 3
        assert main.phase is MainPhase.COMPLETED
        assert controller.capture_scope is None
        assert scope.frame_identity.value == "released"
        assert len({thread for _, thread in session.calls}) == 1
    finally:
        arbiter.close(timeout=3)


def test_failed_capture_cell_preserves_stack_for_repair_cell() -> None:
    from onec_runtime.execution.capture.public_inspection import CaptureInspectionBridge
    from onec_runtime.execution.controller.controller import ExecutionController

    class BslRejectedOnce(CompleteSession):
        def __init__(self) -> None:
            super().__init__()
            self.rejected = False

        def wait_evaluation_event(self, pending, *, timeout_s, on_transport_dispatch):
            if (
                self.expression.startswith(
                    "RuntimeKernelServer.ВыполнитьКодВКонтекстеОтладки("
                )
                and not self.rejected
            ):
                self.rejected = True
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

    session = BslRejectedOnce()
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
        controller.submit_main("Результат = 1;").wait_settled(3)
        scope = controller.capture_scope
        assert scope is not None
        failed = controller.submit_capture_cell("ОшибочнаяКоманда();").wait_settled(3)
        assert failed.error_occurred is True
        assert controller.capture_scope is scope

        inspection = CaptureInspectionBridge(controller).current()
        assert inspection.stack[:1].total >= 1
        assert inspection.frame(0).variables[:1].total >= 1

        repaired = controller.submit_capture_cell("Результат = 2;").wait_settled(3)
        assert repaired.error_occurred is False
        assert controller.capture_scope is scope
    finally:
        arbiter.close(timeout=3)


def test_controller_repairs_confirmed_capture_restore_without_repeating_cell() -> None:
    from onec_runtime.execution.controller.controller import ExecutionController

    class RestoreRejectedOnce(CompleteSession):
        def __init__(self) -> None:
            super().__init__()
            self.restore_rejected = False
            self.user_evals = 0

        def start_evaluation(self, expression, **kwargs):
            if expression.startswith("RuntimeKernelServer.ВыполнитьКодВКонтекстеОтладки("):
                self.user_evals += 1
            return super().start_evaluation(expression, **kwargs)

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

    session = RestoreRejectedOnce()
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
        controller.submit_main("Результат = 1;").wait_settled(3)
        scope = controller.capture_scope
        ticket = controller.submit_capture_cell("Результат = 2;")
        assert ticket.wait_unknown(3)
        assert session.user_evals == 1
        assert controller.capture_scope is scope

        controller.repair_capture_cell_after_restore(ticket)
        assert ticket.wait_settled(3).error_occurred is False
        assert session.user_evals == 1
        assert controller.capture_scope is scope
        assert scope.published
        assert controller.submit_capture_cell("Результат = 3;").wait_settled(3).error_occurred is False
        assert session.user_evals == 2
        assert controller.capture_scope is scope
    finally:
        if arbiter.active_ticket is None:
            arbiter.close(timeout=3)


def test_second_capture_stop_has_new_scope_but_same_main_operation() -> None:
    from onec_runtime.execution.controller.controller import (
        ExecutionController,
        MainYieldKind,
    )

    session = CompleteSession(capture_count=2)
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
        first = controller.submit_main("Результат = 1;").wait(3)
        assert first.kind is MainYieldKind.CAPTURE
        first_scope = first.scope
        main = first.operation

        second = controller.submit_resume().wait(3)
        assert second.kind is MainYieldKind.CAPTURE
        assert second.operation is main
        assert second.scope is not first_scope
        assert second.scope.identity.local_stop_sequence == 2
        assert first_scope.frame_identity.value == "released"

        completed = controller.submit_resume().wait(3)
        assert completed.kind is MainYieldKind.COMPLETED
        assert completed.operation is main
        assert main.phase is MainPhase.COMPLETED
        assert len({thread for _, thread in session.calls}) == 1
    finally:
        arbiter.close(timeout=3)


def test_capture_resume_writes_dirty_root_before_continuing_main() -> None:
    from onec_runtime.execution.controller.controller import (
        ExecutionController,
        MainYieldKind,
    )

    session = CompleteSession()
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
        scope = captured.scope
        result = controller.submit_capture_cell(
            "Результат = 2;", dirty_roots=("Результат",)
        ).wait(3)
        assert result.error_occurred is False
        assert scope.dirty_roots == ("Результат",)

        resumed = controller.submit_resume().wait(3)
        assert resumed.kind is MainYieldKind.COMPLETED
        assert scope.writeback_ledger.record("Результат").phase.value == "succeeded"
        call_names = [name for name, _ in session.calls]
        assert call_names.index("modify", 2) < call_names.index("continue", 10)
        assert len({thread for _, thread in session.calls}) == 1
    finally:
        arbiter.close(timeout=3)


def test_explicit_resume_dirty_root_is_admitted_before_writeback() -> None:
    from onec_runtime.execution.controller.controller import (
        ExecutionController, MainYieldKind,
    )

    session = CompleteSession()
    arbiter = RdbgArbiter(session, RouteToken("runtime-1", 1, 0, "main"))
    controller = ExecutionController(
        arbiter, MainExecutor(poll_interval_s=0.1),
        CaptureExecutor(None, KERNEL, decode_command_id=evaluation_to_python),
        CaptureCellEvaluator(), BreakpointRegistry(KERNEL, (BUSINESS,)),
        runtime_generation=1,
    )
    try:
        captured = controller.submit_main("Результат = 1;").wait_settled(3)
        scope = captured.scope
        resumed = controller.submit_resume(
            dirty_roots=("Результат",),
        ).wait_settled(3)
        assert resumed.kind is MainYieldKind.COMPLETED
        assert scope.dirty_roots == ("Результат",)
        assert scope.writeback_ledger.record("Результат").phase.value == "succeeded"
    finally:
        arbiter.close(timeout=3)


def test_capture_transfer_revalidates_worker_catalog_before_remote_effect() -> None:
    from onec_runtime.errors import ProtocolError
    from onec_runtime.execution.controller.controller import ExecutionController

    session = CompleteSession()
    arbiter = RdbgArbiter(session, RouteToken("runtime-1", 1, 0, "main"))
    controller = ExecutionController(
        arbiter, MainExecutor(poll_interval_s=0.1),
        CaptureExecutor(None, KERNEL, decode_command_id=evaluation_to_python),
        CaptureCellEvaluator(), BreakpointRegistry(KERNEL, (BUSINESS,)),
        runtime_generation=1,
    )
    try:
        scope = controller.submit_main("Результат = 1;").wait_settled(3).scope
        before = len(session.calls)

        def reject_stale_catalog():
            raise ProtocolError("Worker catalog changed")

        with pytest.raises(ProtocolError, match="catalog changed"):
            controller.submit_capture_materialization(
                object(), _before_first_effect=reject_stale_catalog,
            ).wait_settled(3)
        assert session.calls[before:] == []
        assert controller.capture_scope is scope
        assert scope.frame_identity.value == "confirmed"
    finally:
        arbiter.close(timeout=3)


def test_resume_retry_keeps_frozen_explicit_root_ledger() -> None:
    from onec_runtime.execution.capture.writeback import CaptureExportFailed
    from onec_runtime.execution.controller.controller import ExecutionController

    class ExportRejectedOnce(CompleteSession):
        def __init__(self) -> None:
            super().__init__()
            self.rejected = False

        def wait_evaluation_event(self, pending, *, timeout_s, on_transport_dispatch):
            if (
                self.expression.startswith(
                    "RuntimeKernelServer.ПоместитьЗначениеКонтекстаОтладки("
                )
                and not self.rejected
            ):
                self.rejected = True
                on_transport_dispatch()
                self._record("wait_eval")
                self.pending = None
                return EvaluationResult(
                    pending.result_id, "Ошибка", "", True, "planned rejection"
                )
            return super().wait_evaluation_event(
                pending, timeout_s=timeout_s,
                on_transport_dispatch=on_transport_dispatch,
            )

    session = ExportRejectedOnce()
    arbiter = RdbgArbiter(session, RouteToken("runtime-1", 1, 0, "main"))
    controller = ExecutionController(
        arbiter, MainExecutor(poll_interval_s=0.1),
        CaptureExecutor(None, KERNEL, decode_command_id=evaluation_to_python),
        CaptureCellEvaluator(), BreakpointRegistry(KERNEL, (BUSINESS,)),
        runtime_generation=1,
    )
    try:
        scope = controller.submit_main("Результат = 1;").wait_settled(3).scope
        with pytest.raises(CaptureExportFailed):
            controller.submit_resume(
                dirty_roots=("Результат",),
            ).wait_settled(3)
        assert scope.writeback_ledger.roots == ("Результат",)
        scope.writeback_ledger.retry_confirmed_export("Результат")
        assert controller.submit_resume(
            dirty_roots=("Результат",),
        ).wait_settled(3).kind.value == "completed"
    finally:
        arbiter.close(timeout=3)


def test_confirmed_dirty_root_export_failure_keeps_capture_stop() -> None:
    from onec_runtime.execution.capture.writeback import (
        CaptureExportFailed,
        WritebackDisposition,
    )
    from onec_runtime.execution.controller.controller import ExecutionController

    class BrokenExportSession(CompleteSession):
        def wait_evaluation_event(self, pending, *, timeout_s, on_transport_dispatch):
            if self.expression.startswith("RuntimeKernelServer.ПоместитьЗначениеКонтекстаОтладки("):
                on_transport_dispatch()
                self._record("wait_eval")
                self.pending = None
                return EvaluationResult(
                    pending.result_id, "Ошибка", "", True, "planned export failure"
                )
            return super().wait_evaluation_event(
                pending, timeout_s=timeout_s,
                on_transport_dispatch=on_transport_dispatch,
            )

    session = BrokenExportSession()
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
        scope = captured.scope
        main = captured.operation
        controller.submit_capture_cell(
            "Результат = 2;", dirty_roots=("Результат",)
        ).wait(3)

        with pytest.raises(CaptureExportFailed):
            controller.submit_resume().wait(3)

        assert scope.writeback_ledger.disposition is WritebackDisposition.PAUSED_EXPORT_FAILED
        assert scope.frame_identity.value == "confirmed"
        assert main.phase is MainPhase.SUSPENDED_CAPTURE
        assert controller.capture_scope is scope
        assert [name for name, _ in session.calls].count("continue") == 1
    finally:
        arbiter.close(timeout=3)


def test_confirmed_variable_inspection_failure_does_not_poison_capture() -> None:
    from onec_runtime.execution.capture.inspection import CaptureInspectionUnavailable
    from onec_runtime.execution.controller.controller import ExecutionController

    class InspectionSession(CompleteSession):
        def __init__(self) -> None:
            super().__init__()
            self.reads = 0

        def local_variables(self, stack_level=0, *, timeout_s, on_transport_dispatch):
            self.reads += 1
            if self.reads == 3:
                on_transport_dispatch()
                self._record("locals")
                return LocalVariablesResult(
                    UUID(int=97), (), True, "private debugger detail"
                )
            return super().local_variables(
                stack_level, timeout_s=timeout_s,
                on_transport_dispatch=on_transport_dispatch,
            )

    session = InspectionSession()
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
        with pytest.raises(CaptureInspectionUnavailable) as failure:
            controller.submit_capture_variable("Amount", stack_level=0).wait(3)
        assert "private debugger detail" not in str(failure.value)
        assert controller.capture_scope is scope and scope.published

        variable = controller.submit_capture_variable("Amount", stack_level=0).wait(3)
        assert variable.name == "Amount"
        assert controller.submit_resume().wait(3).kind.value == "completed"
        assert len({thread for _, thread in session.calls}) == 1
    finally:
        arbiter.close(timeout=3)


@pytest.mark.parametrize("settle", [False, True])
def test_user_breakpoint_resumes_same_main_operation_on_owned_worker(
    settle: bool,
) -> None:
    from onec_runtime.execution.controller.controller import (
        ExecutionController,
        MainYieldKind,
    )
    from onec_runtime.rdbg.models import ModuleLocation

    user_location = ModuleLocation(
        "ExtensionModule", "", UUID(int=99), UUID(int=100), 10, "Business"
    )

    class UserStopSession(CompleteSession):
        def __init__(self) -> None:
            super().__init__()
            self.stops = deque(
                (
                    StopEvent(TARGET, user_location, "callStackFormed", stop_by_breakpoint=True),
                    StopEvent(TARGET, KERNEL, MAIN_STOP.reason, stop_by_breakpoint=True),
                )
            )

    session = UserStopSession()
    arbiter = RdbgArbiter(session, RouteToken("runtime-1", 1, 0, "main"))
    controller = ExecutionController(
        arbiter,
        MainExecutor(poll_interval_s=0.1),
        CaptureExecutor(None, KERNEL, decode_command_id=evaluation_to_python),
        CaptureCellEvaluator(),
        BreakpointRegistry(KERNEL, (BUSINESS,), (user_location,)),
        runtime_generation=1,
    )
    try:
        finalizer = (lambda outcome: ("published", outcome)) if settle else None
        stopped_reply = controller.submit_main(
            "Результат = 1;", _finalizer=finalizer,
        ).wait(3)
        stopped = stopped_reply[1] if settle else stopped_reply
        if settle:
            assert stopped_reply[0] == "published"
        assert stopped.kind is MainYieldKind.DEBUG_STOP
        assert stopped.operation.phase is MainPhase.SUSPENDED_USER

        finished_reply = controller.submit_resume_debug_stop().wait(3)
        finished = finished_reply[1] if settle else finished_reply
        if settle:
            assert finished_reply[0] == "published"
        assert finished.kind is MainYieldKind.COMPLETED
        assert finished.operation is stopped.operation
        assert finished.operation.phase is MainPhase.COMPLETED
        assert len({thread for _, thread in session.calls}) == 1
    finally:
        arbiter.close(timeout=3)


def test_resume_admission_fences_later_capture_cell_before_worker_runs() -> None:
    from onec_runtime.errors import ProtocolError
    from onec_runtime.execution.controller.controller import ExecutionController

    session = CompleteSession()
    arbiter = RdbgArbiter(session, RouteToken("runtime-1", 1, 0, "main"))
    controller = ExecutionController(
        arbiter,
        MainExecutor(poll_interval_s=0.1),
        CaptureExecutor(None, KERNEL, decode_command_id=evaluation_to_python),
        CaptureCellEvaluator(),
        BreakpointRegistry(KERNEL, (BUSINESS,)),
        runtime_generation=1,
    )
    entered = Event()
    release = Event()
    try:
        scope = controller.submit_main("Результат = 1;").wait(3).scope

        def hold_worker(port):
            entered.set()
            assert release.wait(3)
            return Settlement(None)

        blocker = arbiter.submit(arbiter.current_route, hold_worker)
        arbiter.dispatch(blocker)
        assert entered.wait(3)
        resume_ticket = controller.submit_resume()

        with pytest.raises(ProtocolError, match="resume"):
            controller.submit_capture_cell(
                "Результат = 4;", dirty_roots=("Результат",)
            )
        assert scope.dirty_roots == ()

        release.set()
        blocker.wait(3)
        assert resume_ticket.wait(3).kind.value == "completed"
    finally:
        release.set()
        if "resume_ticket" in locals():
            resume_ticket.wait_settled(3)
        arbiter.close(timeout=3)


def test_capture_materialization_uses_current_scope_and_one_rdbg_owner() -> None:
    from onec_runtime.capture_evaluation import AdmissionEnvelopeV1
    from onec_runtime.execution.controller.controller import ExecutionController
    from test_capture_materialization_executor import transfer_plan

    envelope = AdmissionEnvelopeV1(7, 1, 1, sha256(b"a").hexdigest(), 4)

    class MaterializationSession(CompleteSession):
        def wait_evaluation_event(self, pending, *, timeout_s, on_transport_dispatch):
            expression = self.expression
            if (
                "__onec_compact_table_" in expression
                or "ЗабратьКомпактнуюМатериализацию" in expression
                or '"admission"' in expression
            ):
                on_transport_dispatch()
                self._record("wait_eval")
                self.pending = None
                if "ЗабратьКомпактнуюМатериализацию" in expression:
                    return EvaluationResult(pending.result_id, "Строка", '"YQ=="', False)
                if "Контекст.Удалить" in expression:
                    return EvaluationResult(pending.result_id, "Булево", "Истина", False)
                return EvaluationResult(pending.result_id, "Строка", envelope.encode(), False)
            return super().wait_evaluation_event(
                pending, timeout_s=timeout_s,
                on_transport_dispatch=on_transport_dispatch,
            )

    session = MaterializationSession()
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
        payload = controller.submit_capture_materialization(
            transfer_plan(key)
        ).wait(3)
        assert payload == b"a"
        assert controller.capture_scope is scope and scope.published
        assert scope.temporary_cleanup_debts == ()
        assert controller.submit_resume().wait(3).kind.value == "completed"
        assert len({thread for _, thread in session.calls}) == 1
    finally:
        arbiter.close(timeout=3)
