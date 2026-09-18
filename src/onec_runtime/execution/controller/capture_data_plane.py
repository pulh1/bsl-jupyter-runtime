"""Build CAPTURE variable reads for an already admitted scope and route."""

from __future__ import annotations

from collections.abc import Callable

from onec_runtime.capture_values import VariableRole
from onec_runtime.execution.arbiter import (
    ExecutionTicket, Plan, RouteToken, SessionPort, Settlement,
)
from onec_runtime.execution.capture.inspection import CaptureInspectionExecutor
from onec_runtime.execution.capture.scope import CaptureScope


SubmitCapturePlan = Callable[[RouteToken, Plan], ExecutionTicket]


def submit_variable(
    scope: CaptureScope,
    route: RouteToken,
    inspection: CaptureInspectionExecutor,
    submit: SubmitCapturePlan,
    name: str,
    *,
    stack_level: int,
) -> ExecutionTicket:
    """Submit one variable read without owning CAPTURE admission or state."""

    def plan(port: SessionPort) -> Settlement:
        return Settlement(inspection.read_variable(
            scope, name, stack_level=stack_level, port=port,
        ))

    return submit(route, plan)


def submit_variable_page(
    scope: CaptureScope,
    route: RouteToken,
    inspection: CaptureInspectionExecutor,
    submit: SubmitCapturePlan,
    *,
    stack_level: int,
    start: int,
    stop: int,
    role: VariableRole,
    parameter_names: tuple[str, ...],
) -> ExecutionTicket:
    """Submit a bounded page of safe native variable names."""

    def plan(port: SessionPort) -> Settlement:
        return Settlement(inspection.read_variable_page(
            scope,
            stack_level=stack_level,
            start=start,
            stop=stop,
            role=role,
            parameter_names=parameter_names,
            port=port,
        ))

    return submit(route, plan)


def submit_typed_variable_page(
    scope: CaptureScope,
    route: RouteToken,
    inspection: CaptureInspectionExecutor,
    submit: SubmitCapturePlan,
    *,
    stack_level: int,
    start: int,
    stop: int,
    role: VariableRole,
    parameter_names: tuple[str, ...],
) -> ExecutionTicket:
    """Submit a bounded page of safe native variable metadata."""

    def plan(port: SessionPort) -> Settlement:
        return Settlement(inspection.read_typed_variable_page(
            scope,
            stack_level=stack_level,
            start=start,
            stop=stop,
            port=port,
            role=role,
            parameter_names=parameter_names,
        ))

    return submit(route, plan)
