"""Exact CAPTURE data plans retain the admitted scope and route."""

from __future__ import annotations

import pytest

from onec_runtime.capture_values import VariableRole
from onec_runtime.execution.arbiter import RouteToken, Settlement
from onec_runtime.execution.controller.capture_data_plane import (
    submit_typed_variable_page,
    submit_variable,
    submit_variable_page,
)


@pytest.mark.parametrize("kind", ("variable", "page", "typed_page"))
def test_capture_data_plan_uses_admitted_scope_route_and_worker_port(kind: str) -> None:
    scope = object()
    route = RouteToken("runtime-1", 3, 7, "capture-2")
    port = object()
    result = object()
    ticket = object()
    observed: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    class Inspection:
        def read_variable(self, *args: object, **kwargs: object) -> object:
            observed.append(("variable", args, kwargs))
            return result

        def read_variable_page(self, *args: object, **kwargs: object) -> object:
            observed.append(("page", args, kwargs))
            return result

        def read_typed_variable_page(self, *args: object, **kwargs: object) -> object:
            observed.append(("typed_page", args, kwargs))
            return result

    def submit(selected_route: RouteToken, plan: object) -> object:
        assert selected_route is route
        assert observed == []
        settlement = plan(port)
        assert isinstance(settlement, Settlement)
        assert settlement.value is result
        return ticket

    inspection = Inspection()
    if kind == "variable":
        returned = submit_variable(
            scope, route, inspection, submit,
            "Amount", stack_level=1,
        )
        expected_kwargs = {"stack_level": 1, "port": port}
        expected_args = (scope, "Amount")
    elif kind == "page":
        returned = submit_variable_page(
            scope, route, inspection, submit,
            stack_level=1, start=2, stop=4,
            role=VariableRole.PARAMETERS, parameter_names=("Amount",),
        )
        expected_kwargs = {
            "stack_level": 1, "start": 2, "stop": 4,
            "role": VariableRole.PARAMETERS,
            "parameter_names": ("Amount",), "port": port,
        }
        expected_args = (scope,)
    else:
        returned = submit_typed_variable_page(
            scope, route, inspection, submit,
            stack_level=1, start=2, stop=4,
            role=VariableRole.PARAMETERS, parameter_names=("Amount",),
        )
        expected_kwargs = {
            "stack_level": 1, "start": 2, "stop": 4,
            "role": VariableRole.PARAMETERS,
            "parameter_names": ("Amount",), "port": port,
        }
        expected_args = (scope,)

    assert returned is ticket
    assert observed == [(kind, expected_args, expected_kwargs)]
