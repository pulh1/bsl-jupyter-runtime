from __future__ import annotations

from dataclasses import dataclass
import json

import pytest

from onec_runtime_mcp.agent.contracts import to_wire
from onec_runtime_mcp.agent.contracts import (
    AgentOperationState,
    CapabilityMode,
    OperationDescriptor,
    RuntimeDescriptor,
    ServiceResponse,
)
from onec_runtime_mcp.agent.facade import AgentFacade, AgentFacadeError
from onec_runtime_mcp.agent.facade_contracts import AgentOperationKind, AgentOperationView
from test_agent_service import ensure_ready, seeded_service


@dataclass
class RecordingClient:
    responses: list[ServiceResponse]

    def __post_init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def call(self, method: str, arguments: dict[str, object]) -> ServiceResponse:
        self.calls.append((method, arguments))
        return self.responses.pop(0)


def _operation(state: AgentOperationState) -> OperationDescriptor:
    return OperationDescriptor(
        "op-1",
        state,
        "runtime-1",
        1,
        "cell-1",
        2,
        "a" * 64,
        0,
        1,
        False,
    )


def _view(state: AgentOperationState) -> dict[str, object]:
    return {
        "operation": {
            "operation_id": "op-1",
            "kind": "code_run",
            "runtime_id": "runtime-1",
            "runtime_generation": 1,
            "cell_id": "cell-1",
            "revision": 2,
            "source_sha256": "a" * 64,
        },
        "state": state.value,
        "messages": ["done"] if state is AgentOperationState.COMPLETED else [],
        "next_message_cursor": 2,
        "next_event_cursor": 3,
        "changed_variables": [],
        "change_confidence": "unknown",
        "outputs": {},
        "capture": None,
        "failure": None,
        "recovery": [],
        "truncation": {
            "messages": False,
            "changed_variables": False,
            "outputs": False,
        },
    }


def test_code_run_and_wait_return_same_canonical_view_shape() -> None:
    client = RecordingClient(
        [
            ServiceResponse.success(_operation(AgentOperationState.QUEUED)),
            ServiceResponse.success(_view(AgentOperationState.QUEUED)),
            ServiceResponse.success(_operation(AgentOperationState.COMPLETED)),
            ServiceResponse.success(_view(AgentOperationState.COMPLETED)),
        ]
    )
    facade = AgentFacade(client)

    started = facade.code_run(
        {
            "cell_id": "cell-1",
            "revision": 2,
            "source_sha256": "a" * 64,
            "inputs": {},
        }
    )
    completed = facade.operation_wait(
        {
            "operation_id": "op-1",
            "timeout_s": 5,
            "after_event_cursor": started.next_event_cursor,
            "after_message_cursor": started.next_message_cursor,
        }
    )

    assert type(started) is type(completed) is AgentOperationView
    assert started.operation.kind is AgentOperationKind.CODE_RUN
    assert completed.messages == ("done",)
    assert client.calls == [
        (
            "code.run",
            {
                "cell_id": "cell-1",
                "revision": 2,
                "source_sha256": "a" * 64,
                "inputs": {},
            },
        ),
        (
            "operation.view",
            {"operation_id": "op-1", "after_message_cursor": 0},
        ),
        (
            "operation.wait",
            {
                "operation_id": "op-1",
                "timeout_s": 5,
                "after_cursor": 3,
            },
        ),
        (
            "operation.view",
            {"operation_id": "op-1", "after_message_cursor": 2},
        ),
    ]


def test_facade_rehydrates_exact_bounded_diagnostic_and_provenance() -> None:
    """Break caught: facade copies arbitrary failure strings across the wire."""
    wire = _view(AgentOperationState.FAILED)
    wire["execution_provenance"] = {
        "visible_source_sha256": "a" * 64,
        "executed_source_sha256": "b" * 64,
        "source_map_sha256": "c" * 64,
        "mode": "main",
        "worker_generation": None,
        "worker_manifest_sha256": None,
    }
    diagnostic = {
        "diagnostic_id": "d" * 64,
        "stage": "execution",
        "mapping_confidence": "exact",
        "visible_location": {
            "line": 1,
            "column": 1,
            "span": {"start": 0, "end": 1},
        },
        "related_visible_span": None,
        "excerpt": "Результат = 1;",
        "synthetic_region": None,
    }
    wire["failure"] = {
        "stage": "execution",
        "partial_results": {},
        "state_changed": "no",
        "diagnostic": diagnostic,
    }
    facade = AgentFacade(
        RecordingClient(
            [
                ServiceResponse.success(_operation(AgentOperationState.FAILED)),
                ServiceResponse.success(wire),
            ]
        )
    )

    view = facade.code_run(
        {
            "cell_id": "cell-1",
            "revision": 2,
            "source_sha256": "a" * 64,
            "inputs": {},
        }
    )
    diagnostic["excerpt"] = "mutated secret"

    assert view.execution_provenance.executed_source_sha256 == "b" * 64
    assert view.failure["diagnostic"]["excerpt"] == "Результат = 1;"
    assert "mutated secret" not in json.dumps(to_wire(view), ensure_ascii=False)


def test_facade_rejects_non_contract_diagnostic_fields_fail_closed() -> None:
    """Break caught: facade forwards a raw backend diagnostic extension field."""
    wire = _view(AgentOperationState.FAILED)
    wire["failure"] = {
        "stage": "execution",
        "partial_results": {},
        "state_changed": "unknown",
        "diagnostic": {
            "diagnostic_id": "d" * 64,
            "stage": "execution",
            "mapping_confidence": "unknown",
            "visible_location": None,
            "related_visible_span": None,
            "excerpt": None,
            "synthetic_region": None,
            "platform_diagnostic": "rdbg_pid=9182 token=private",
        },
    }
    facade = AgentFacade(
        RecordingClient(
            [
                ServiceResponse.success(_operation(AgentOperationState.FAILED)),
                ServiceResponse.success(wire),
            ]
        )
    )

    with pytest.raises((TypeError, ValueError)):
        facade.code_run(
            {
                "cell_id": "cell-1",
                "revision": 2,
                "source_sha256": "a" * 64,
                "inputs": {},
            }
        )


def test_runtime_ensure_returns_startup_view_then_ready_descriptor() -> None:
    ready = RuntimeDescriptor("runtime-1", 1, "ready", CapabilityMode.EXPERIMENT)
    client = RecordingClient(
        [
            ServiceResponse.success(_operation(AgentOperationState.RUNNING)),
            ServiceResponse.success(_view(AgentOperationState.RUNNING)),
            ServiceResponse.success(ready),
        ]
    )
    facade = AgentFacade(client)

    starting = facade.runtime_ensure({"profile": "zup", "mode": "experiment"})
    admitted = facade.runtime_ensure({"profile": "zup", "mode": "observe"})

    assert isinstance(starting, AgentOperationView)
    assert admitted is ready


def test_code_run_passes_validated_observation_plan_as_structured_wire() -> None:
    client = RecordingClient(
        [
            ServiceResponse.success(_operation(AgentOperationState.QUEUED)),
            ServiceResponse.success(_view(AgentOperationState.QUEUED)),
        ]
    )
    facade = AgentFacade(client)

    facade.code_run(
        {
            "cell_id": "cell-1",
            "revision": 2,
            "source_sha256": "a" * 64,
            "inputs": {},
            "observe": {
                "items": [
                    {
                        "alias": "result",
                        "source": {
                            "kind": "context_binding",
                            "name": "bsl.Результат",
                        },
                    }
                ],
                "budget_profile": "agent_metadata",
            },
        }
    )

    method, arguments = client.calls[0]
    assert method == "code.run"
    assert arguments["observe"]["items"][0]["result"] == "proxy"


def test_materialize_resolves_server_profile_and_domain_policy_shape() -> None:
    client = RecordingClient([ServiceResponse.success({"proxy_id": "python-1"})])
    facade = AgentFacade(client)

    facade.value_materialize(
        {
            "proxy_id": "onec-1",
            "budget_profile": "agent_dataframe",
            "refs": "both",
        }
    )

    method, arguments = client.calls[0]
    assert method == "value.materialize"
    assert arguments == {
        "proxy_id": "onec-1",
        "target": "python",
        "policy": {"refs": "both"},
        "budget": {
            "depth": 8,
            "items": 200_000,
            "rows": 10_000,
            "bytes": 64 * 1024 * 1024,
            "timeout_s": 30.0,
        },
    }


def test_materialize_rejects_profile_that_does_not_allow_full_scan() -> None:
    facade = AgentFacade(RecordingClient([]))

    with pytest.raises(ValueError, match="full materialization"):
        facade.value_materialize(
            {"proxy_id": "onec-1", "budget_profile": "agent_preview"}
        )


def test_facade_normalizes_unexpected_client_transport_failure() -> None:
    class BrokenClient:
        def call(self, method: str, arguments: dict[str, object]) -> ServiceResponse:
            del method, arguments
            raise RuntimeError("private endpoint detail")

    with pytest.raises(AgentFacadeError) as raised:
        AgentFacade(BrokenClient()).workspace_status({})

    assert raised.value.failure.category.value == "platform_failure"
    assert raised.value.failure.current_state == {"control_transport": "unavailable"}


def test_completed_bsl_observation_publishes_proxy_in_durable_view(tmp_path) -> None:
    service, backend, _ = seeded_service(tmp_path)
    ensure_ready(service)
    assert service.call("code.list", {"container": "demo.ipynb"}).ok
    revision = service.call("code.get", {"cell_id": "cell-main"}).value

    operation = service.call(
        "code.run",
        {
            "cell_id": revision.cell_id,
            "revision": revision.revision,
            "source_sha256": revision.source_sha256,
            "inputs": {},
            "wait_s": 1.0,
            "observe": {
                "items": [
                    {
                        "alias": "threshold",
                        "source": {
                            "kind": "context_binding",
                            "name": "bsl.Порог",
                        },
                    }
                ],
                "budget_profile": "agent_metadata",
            },
        },
    )
    assert operation.ok
    view = service.call(
        "operation.view", {"operation_id": operation.value.operation_id}
    )

    assert view.ok
    assert view.value.state is AgentOperationState.COMPLETED
    assert view.value.outputs["threshold"].qualified_name == "bsl.Порог"
    assert backend.sources == [revision.source]


def test_saved_python_cell_returns_same_canonical_operation_view(tmp_path) -> None:
    service, _, _ = seeded_service(tmp_path)
    try:
        service.call("code.list", {"container": "demo.ipynb"})
        current = service.call("code.get", {"cell_id": "cell-main"}).value
        saved = service.call(
            "code.put",
            {
                "cell_id": current.cell_id,
                "source": "answer = 40 + 2",
                "language": "python",
                "mode": "main",
                "outputs": ["answer"],
                "expected_revision": current.revision,
                "expected_document_sha256": current.document_sha256,
            },
        ).value
        revision = service.call(
            "code.get", {"cell_id": saved.cell_id, "revision": saved.revision}
        ).value

        view = AgentFacade(service).code_run(
            {
                "cell_id": revision.cell_id,
                "revision": revision.revision,
                "source_sha256": revision.source_sha256,
                "inputs": {},
                "wait_s": 1.0,
            }
        )

        assert view.state is AgentOperationState.COMPLETED
        assert view.operation.kind is AgentOperationKind.CODE_RUN
        assert view.outputs["answer"].bounded_preview.scalar == 42
    finally:
        service.close()


def test_observation_partial_failure_keeps_proven_execution_completed(tmp_path) -> None:
    service, _, _ = seeded_service(tmp_path)
    ensure_ready(service)
    assert service.call("code.list", {"container": "demo.ipynb"}).ok
    revision = service.call("code.get", {"cell_id": "cell-main"}).value
    operation = service.call(
        "code.run",
        {
            "cell_id": revision.cell_id,
            "revision": revision.revision,
            "source_sha256": revision.source_sha256,
            "inputs": {},
            "wait_s": 1.0,
            "observe": {
                "items": [
                    {
                        "alias": "missing",
                        "source": {
                            "kind": "context_binding",
                            "name": "bsl.НетТакойПеременной",
                        },
                    }
                ],
                "budget_profile": "agent_metadata",
            },
        },
    ).value

    view = service.call("operation.view", {"operation_id": operation.operation_id}).value
    assert view.state is AgentOperationState.COMPLETED
    assert view.outputs == {}
    assert view.failure["stage"] == "observation"
    assert view.failure["partial_results"] == {"missing": "unavailable"}


def test_inline_bsl_view_has_exact_kind_changed_delta_and_preview(tmp_path) -> None:
    service, _, _ = seeded_service(tmp_path)
    ensure_ready(service)

    operation = service.call(
        "code.run_inline",
        {
            "language": "bsl",
            "mode": "main",
            "source": "Порог = 2;",
            "inputs": {},
            "wait_s": 1.0,
            "observe": {
                "items": [
                    {
                        "alias": "threshold",
                        "source": {
                            "kind": "context_binding",
                            "name": "bsl.Порог",
                        },
                        "result": "preview",
                    }
                ],
                "budget_profile": "agent_preview",
            },
        },
    )
    view = service.call(
        "operation.view", {"operation_id": operation.value.operation_id}
    ).value

    assert view.operation.kind is AgentOperationKind.CODE_RUN_INLINE
    assert view.changed_variables == ()
    assert view.outputs["threshold"].bounded_preview.scalar == 42


def test_observation_full_scan_requires_dataframe_budget_without_downgrading_execution(
    tmp_path,
) -> None:
    service, _, _ = seeded_service(tmp_path)
    ensure_ready(service)
    operation = service.call(
        "code.run_inline",
        {
            "language": "bsl",
            "mode": "main",
            "source": "Порог = 2;",
            "inputs": {},
            "wait_s": 1.0,
            "observe": {
                "items": [
                    {
                        "alias": "threshold",
                        "source": {
                            "kind": "context_binding",
                            "name": "bsl.Порог",
                        },
                        "result": "python",
                    }
                ],
                "budget_profile": "agent_preview",
            },
        },
    )
    view = service.call(
        "operation.view", {"operation_id": operation.value.operation_id}
    ).value

    assert view.state is AgentOperationState.COMPLETED
    assert dict(view.outputs) == {}
    assert view.failure["stage"] == "observation"
