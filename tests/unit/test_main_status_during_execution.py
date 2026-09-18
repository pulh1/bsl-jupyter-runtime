"""A pending MAIN command remains locally observable during its stop wait."""

from threading import Event

from onec_runtime.execution.arbiter import RdbgArbiter, RouteToken
from onec_runtime.execution.capture.cell_evaluator import CaptureCellEvaluator
from onec_runtime.execution.capture.executor import CaptureExecutor
from onec_runtime.execution.controller.controller import ExecutionController
from onec_runtime.execution.main import MainExecutor
from onec_runtime.execution.namespace import RuntimeNamespaceOwner
from onec_runtime.execution.status_projection import ExecutionStatusProjection
from onec_runtime.execution.worker_activation import WorkerActivationSnapshot
from onec_runtime.runtime_models import OperationState
from onec_runtime.stop_routing import BreakpointRegistry
from onec_runtime.table_value import evaluation_to_python

from test_execution_route_sequence import BUSINESS, KERNEL, RouteSession


def test_status_reports_running_main_without_waiting_for_its_notebook_writer() -> None:
    waiting = Event()
    release = Event()

    class WaitingSession(RouteSession):
        def wait_for_any_stop(self, *, timeout_s, expected_target, on_transport_dispatch):
            stop = super().wait_for_any_stop(
                timeout_s=timeout_s,
                expected_target=expected_target,
                on_transport_dispatch=on_transport_dispatch,
            )
            waiting.set()
            assert release.wait(3), "MAIN stop was never released"
            return stop

    session = WaitingSession()
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
        ticket = controller.submit_main("Результат = 1;")
        assert waiting.wait(3), "MAIN did not enter the stop wait"
        pending = status.status()
        assert pending.state is OperationState.MAIN_PENDING
        assert pending.operation_id == controller.main_operation.command_id
        release.set()
        ticket.wait_settled(3)
    finally:
        release.set()
        arbiter.close(timeout=3)
