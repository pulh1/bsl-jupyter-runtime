"""Confirmed CAPTURE setup errors retain the stopped MAIN and exact stage."""

from dataclasses import replace

import pytest

from onec_runtime.errors import BslExecutionError, ProtocolError
from onec_runtime.execution.arbiter import RdbgArbiter, RouteToken
from onec_runtime.execution.capture.cell_evaluator import CaptureCellEvaluator
from onec_runtime.execution.capture.executor import CaptureExecutor
from onec_runtime.execution.controller.controller import ExecutionController
from onec_runtime.execution.main import MainExecutor
from onec_runtime.execution.namespace import RuntimeNamespaceOwner
from onec_runtime.execution.status_projection import ExecutionStatusProjection
from onec_runtime.execution.worker_activation import WorkerActivationSnapshot
from onec_runtime.rdbg.models import EvaluationResult, StackFrame
from onec_runtime.runtime_models import OperationState
from onec_runtime.stop_routing import BreakpointRegistry
from onec_runtime.table_value import evaluation_to_python

from test_execution_controller_routes import CompleteSession
from test_execution_route_sequence import BUSINESS, KERNEL


class FailingSetupSession(CompleteSession):
    def __init__(self, failure: str) -> None:
        super().__init__()
        self.failure = failure
        if failure == "kernel_frame":
            stop = self.stops.popleft()
            locations = (BUSINESS, BUSINESS, BUSINESS)
            self.stops.appendleft(replace(
                stop,
                stack=locations,
                stack_frames=tuple(
                    StackFrame(stop.target_id, level, location)
                    for level, location in enumerate(locations)
                ),
            ))

    def wait_evaluation_event(self, pending, *, timeout_s, on_transport_dispatch):
        expression = self.expression
        transfer = expression.startswith("ПоместитьВоВременноеХранилище(")
        begin = "НачатьКонтекстОтладки" in expression
        if (transfer and self.failure.startswith("transfer_")) or (
            begin and self.failure == "begin_bsl"
        ):
            on_transport_dispatch()
            self._record("wait_eval")
            self.pending = None
            if self.failure == "transfer_payload":
                return EvaluationResult(pending.result_id, "Число", "7", False)
            return EvaluationResult(
                pending.result_id, "Ошибка", "", True,
                "begin rejected" if begin else "transfer rejected",
            )
        return super().wait_evaluation_event(
            pending, timeout_s=timeout_s,
            on_transport_dispatch=on_transport_dispatch,
        )


@pytest.mark.parametrize(
    ("failure", "error_type", "stage", "frame_identity"),
    (
        ("transfer_bsl", BslExecutionError, "locals_read", "unverified"),
        ("transfer_payload", ProtocolError, "locals_read", "unverified"),
        ("kernel_frame", ProtocolError, "context_transferred", "unverified"),
        ("begin_bsl", BslExecutionError, "main_id_confirmed", "confirmed"),
    ),
)
def test_confirmed_capture_setup_error_preserves_stop_and_stage(
    failure: str,
    error_type: type[Exception],
    stage: str,
    frame_identity: str,
) -> None:
    session = FailingSetupSession(failure)
    arbiter = RdbgArbiter(session, RouteToken("runtime-1", 1, 0, "main"))
    controller = ExecutionController(
        arbiter,
        MainExecutor(poll_interval_s=0.1),
        CaptureExecutor(None, KERNEL, decode_command_id=evaluation_to_python),
        CaptureCellEvaluator(),
        BreakpointRegistry(KERNEL, (BUSINESS,)),
        runtime_generation=1,
    )
    worker_snapshot = lambda: WorkerActivationSnapshot(0, (), None, None)
    status = ExecutionStatusProjection(
        controller_facts=controller.status_facts,
        worker_snapshot=worker_snapshot,
        namespace=RuntimeNamespaceOwner(1, 1, worker_snapshot=worker_snapshot),
    )
    try:
        with pytest.raises(error_type):
            controller.submit_main("Результат = 1;").wait_settled(3)

        public = status.status()
        assert public.state is OperationState.CAPTURE_SETUP_FAILED
        assert public.capture_setup is not None
        assert public.capture_setup.context_state.value == "setup_failed"
        assert public.capture_setup.setup_stage.value == stage
        assert public.capture_setup.frame_identity.value == frame_identity
        assert controller.main_operation is not None
        assert not controller.main_operation.terminal
        assert controller.capture_scope is not None
        assert controller.main_operation.pending_stop is controller.capture_scope.stop

        before = tuple(session.calls)
        with pytest.raises(ProtocolError):
            controller.submit_main("Результат = 2;")
        assert tuple(session.calls) == before
    finally:
        arbiter.close(timeout=3)
