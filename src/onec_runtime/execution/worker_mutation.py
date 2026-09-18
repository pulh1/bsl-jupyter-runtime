"""Run trusted Worker universe mutations inside the current arbiter activity.

The persistent Worker adapter calls this runner with its already admitted
``SessionPort``. MAIN helpers use reserved negative command IDs and leave the
user's ``MainOperation`` untouched. CAPTURE helpers evaluate in the confirmed
scope without writing the suspended MAIN command fields or issuing Continue.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import count
from threading import Lock
from typing import Callable

from onec_runtime.errors import BslExecutionError, ProtocolError
from onec_runtime.execution.arbiter import (
    ConfirmedFailure, OutcomeUnknown, SessionPort, Settlement,
)
from onec_runtime.execution.breakpoint_routes import RouteBreakpointWorkspace
from onec_runtime.execution.capture.operation_executor import (
    CaptureCellOperation, CaptureCellOperationExecutor,
)
from onec_runtime.execution.capture.scope import (
    CaptureContextState, CaptureFrameIdentity, CaptureScope,
)
from onec_runtime.execution.evaluation import EvaluationSuspended
from onec_runtime.execution.main import MainExecutor, MainOperation, MainPhase
from onec_runtime.execution.main.completion import (
    MainCommandMismatchError, read_main_completion,
)
from onec_runtime.rdbg.models import EvaluationResult, StopEvent, TargetId
from onec_runtime.stop_routing import BreakpointRegistry, StopReason, classify_stop
from onec_runtime.table_value import evaluation_to_python


@dataclass(frozen=True, slots=True)
class MainPausedWorkerRoute:
    """Service stop with no live user MAIN command."""

    target_id: TargetId
    previous_main: MainOperation | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class CapturePausedWorkerRoute:
    """Confirmed CAPTURE stop belonging to one suspended user MAIN command."""

    main_operation: MainOperation = field(repr=False)
    scope: CaptureScope = field(repr=False)


WorkerMutationRoute = MainPausedWorkerRoute | CapturePausedWorkerRoute


class WorkerMutationUnexpectedStop(OutcomeUnknown):
    """A system command stopped away from its completion service point."""

    def __init__(self, stop: StopEvent) -> None:
        self.stop = stop
        super().__init__("Worker mutation stopped before matching MAIN completion")


class WorkerMutationInstructionRunner:
    """Route one trusted mutation through an existing arbiter ``SessionPort``.

    ``route_provider`` must return a fresh controller-owned route view for the
    active plan. It must never submit a second arbiter ticket or call RDBG.
    The source comes from ``PreparedWorkerMutation``, not a notebook cell.
    """

    def __init__(
        self,
        main_executor: MainExecutor,
        capture_executor: CaptureCellOperationExecutor,
        *,
        registry_provider: Callable[[], BreakpointRegistry],
        breakpoint_routes: RouteBreakpointWorkspace,
        route_provider: Callable[[], WorkerMutationRoute],
    ) -> None:
        if not isinstance(main_executor, MainExecutor):
            raise TypeError("MAIN executor is required")
        if not isinstance(capture_executor, CaptureCellOperationExecutor):
            raise TypeError("CAPTURE cell executor is required")
        if not callable(registry_provider):
            raise TypeError("Current breakpoint registry provider is required")
        if not isinstance(breakpoint_routes, RouteBreakpointWorkspace):
            raise TypeError("Shared breakpoint route workspace is required")
        if not callable(route_provider):
            raise TypeError("Worker mutation route provider is required")
        self._main = main_executor
        self._capture = capture_executor
        self._registry_provider = registry_provider
        self._breakpoint_routes = breakpoint_routes
        self._route_provider = route_provider
        self._system_ids = count(-1, -1)
        self._id_lock = Lock()

    def __call__(self, port: SessionPort, instruction: str) -> object:
        if not isinstance(port, SessionPort):
            raise TypeError("The admitted arbiter SessionPort is required")
        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError("Worker mutation instruction must be nonempty text")
        registry = self._registry_provider()
        if not isinstance(registry, BreakpointRegistry):
            raise ProtocolError("Worker mutation breakpoint registry is unavailable")
        route = self._route_provider()
        if isinstance(route, MainPausedWorkerRoute):
            return self._execute_main(port, instruction, route, registry)
        if isinstance(route, CapturePausedWorkerRoute):
            return self._execute_capture(port, instruction, route)
        raise ProtocolError("Worker mutation route is unavailable")

    def _execute_main(
        self, port: SessionPort, instruction: str, route: MainPausedWorkerRoute,
        registry: BreakpointRegistry,
    ) -> object:
        previous = route.previous_main
        if previous is not None and (
            not previous.terminal or
            (previous.target is not None and previous.target != route.target_id)
        ):
            raise ProtocolError("Worker MAIN helper requires a free service stop")
        with self._id_lock:
            system_id = next(self._system_ids)
        operation = MainOperation(system_id, route.target_id)
        stop = self._main.dispatch(
            operation, instruction,
            install_workspace=lambda: self._install_main_helper_workspace(
                registry, port,
            ),
            before_command_write=lambda: None,
            before_continue=lambda: None,
            port=port,
        )
        if (
            stop.target_id != route.target_id
            or classify_stop(stop, registry).reason is not StopReason.MAIN_SERVICE
        ):
            raise WorkerMutationUnexpectedStop(stop)
        try:
            completion = read_main_completion(port, operation)
        except MainCommandMismatchError as error:
            raise OutcomeUnknown("Worker MAIN helper completion ID is mismatched") from error
        except BaseException as error:
            if operation.terminal and not isinstance(
                error, (OutcomeUnknown, EvaluationSuspended)
            ):
                self._breakpoint_routes.restore_capture(port=port)
            raise
        self._breakpoint_routes.restore_capture(port=port)
        if completion.error:
            raise BslExecutionError(completion.error, messages=completion.messages)
        return completion.result

    def _install_main_helper_workspace(
        self, registry: BreakpointRegistry, port: SessionPort,
    ) -> None:
        self._breakpoint_routes.install_main(registry, port=port)
        self._breakpoint_routes.shield_capture(port=port)

    def _execute_capture(
        self, port: SessionPort, instruction: str, route: CapturePausedWorkerRoute,
    ) -> object:
        operation = route.main_operation
        scope = route.scope
        if (
            operation.phase is not MainPhase.SUSPENDED_CAPTURE
            or scope.identity.main_command_id != operation.command_id
            or (operation.target is not None and scope.identity.target_id != operation.target)
            or scope.context_state is not CaptureContextState.READY
            or scope.frame_identity is not CaptureFrameIdentity.CONFIRMED
        ):
            raise ProtocolError("Worker CAPTURE helper requires the same ready stop")
        source = instruction + "\nРезультатИнструкции = Результат;"
        outcome = self._capture.execute(
            scope, source,
            port=port,
            shield_workspace=lambda worker: self._breakpoint_routes.shield_capture(
                port=worker,
            ),
            restore_workspace=lambda worker: self._breakpoint_routes.restore_capture(
                port=worker,
            ),
            cleanup=lambda worker: None,
            result_policy=self._decode_capture_result,
            operation=CaptureCellOperation(scope.identity),
        )
        if isinstance(outcome, ConfirmedFailure):
            raise outcome.error
        assert isinstance(outcome, Settlement)
        return outcome.value

    @staticmethod
    def _decode_capture_result(result: EvaluationResult) -> object:
        if result.error_occurred:
            raise BslExecutionError(result.error_text)
        return evaluation_to_python(result)
