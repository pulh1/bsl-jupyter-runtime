from __future__ import annotations

import math

import pytest

import onec_runtime_mcp.agent.contracts as contracts
import onec_runtime_mcp.agent.facade_contracts as facade_contracts
from onec_runtime_mcp.agent.contracts import (
    MAX_OPERATION_MESSAGES,
    AgentOperationState,
    CapabilityDescriptor,
    CapabilityMode,
    CodeLanguage,
    CodeMode,
    CodeRevision,
    FailureCategory,
    MethodFailure,
    OperationDescriptor,
    OperationOutput,
    RetrySafety,
    RuntimeDescriptor,
    ServiceResponse,
    StateChanged,
    WorkspaceDescriptor,
    failure_from_exception,
    to_wire,
)
from onec_runtime.errors import ProtocolError


def test_failure_contract_is_exact_and_json_safe() -> None:
    response = ServiceResponse.fail(
        MethodFailure(
            category=FailureCategory.CONFLICT,
            state_changed=StateChanged.NO,
            safe_to_retry=RetrySafety.AFTER_STATUS_CHECK,
            current_state={"revision": 4},
            recommended_actions=("code.get", "code.diff"),
            diagnostic_id="diag-1",
        )
    )

    assert to_wire(response)["failure"] == {
        "category": "conflict",
        "state_changed": "no",
        "safe_to_retry": "after_status_check",
        "current_state": {"revision": 4},
        "operation_id": None,
        "affected_proxies": [],
        "partial_results": {},
        "event_cursor": None,
        "recommended_actions": ["code.get", "code.diff"],
        "diagnostic_id": "diag-1",
    }


def test_wire_serializer_rejects_unknown_objects_and_secret_repr() -> None:
    class Secret:
        def __repr__(self) -> str:
            return "Пароль=secret"

    with pytest.raises(TypeError, match="wire-safe"):
        to_wire(Secret())


def test_agent_diagnostic_view_is_exact_immutable_and_wire_safe() -> None:
    """Break caught: arbitrary diagnostic mappings can leak or mutate after validation."""
    location = {
        "line": 3,
        "column": 7,
        "span": {"start": 18, "end": 24},
    }
    diagnostic = facade_contracts.AgentDiagnosticView(
        diagnostic_id="a" * 64,
        stage="execution",
        mapping_confidence="exact",
        visible_location=location,
        related_visible_span=None,
        excerpt="Результат = 1;",
        synthetic_region=None,
    )
    location["line"] = 99

    assert to_wire(diagnostic) == {
        "diagnostic_id": "a" * 64,
        "stage": "execution",
        "mapping_confidence": "exact",
        "visible_location": {
            "line": 3,
            "column": 7,
            "span": {"start": 18, "end": 24},
        },
        "related_visible_span": None,
        "excerpt": "Результат = 1;",
        "synthetic_region": None,
    }
    with pytest.raises(TypeError):
        diagnostic.visible_location["line"] = 4  # type: ignore[index]


@pytest.mark.parametrize(
    "wire",
    [
        {
            "diagnostic_id": "a" * 64,
            "stage": "execution",
            "mapping_confidence": "exact",
            "visible_location": {
                "line": 3,
                "column": 7,
                "span": {"start": 18, "end": 24},
                "executed_source": "Секрет = 1;",
            },
            "related_visible_span": None,
            "excerpt": None,
            "synthetic_region": None,
        },
        {
            "diagnostic_id": "a" * 64,
            "stage": "execution",
            "mapping_confidence": "exact",
            "visible_location": None,
            "related_visible_span": None,
            "excerpt": "x" * 10_000,
            "synthetic_region": None,
        },
        {
            "diagnostic_id": "pid=9182 token=secret",
            "stage": "rdbg_private_stage",
            "mapping_confidence": "trusted_by_backend",
            "visible_location": None,
            "related_visible_span": None,
            "excerpt": None,
            "synthetic_region": None,
        },
    ],
)
def test_agent_diagnostic_view_rejects_unbounded_or_non_allowlisted_wire(
    wire: dict[str, object],
) -> None:
    """Break caught: malformed backend strings are copied into public failure facts."""
    with pytest.raises((TypeError, ValueError)):
        facade_contracts.AgentDiagnosticView.from_wire(wire)


def test_execution_provenance_has_exact_wire_contract_and_hash_fences() -> None:
    """Break caught: operation provenance accepts mutable or non-hash identities."""
    provenance = contracts.OperationExecutionProvenance(
        visible_source_sha256="a" * 64,
        executed_source_sha256="b" * 64,
        source_map_sha256="c" * 64,
        mode="capture",
        worker_generation=17,
        worker_manifest_sha256="d" * 64,
    )

    assert to_wire(provenance) == {
        "visible_source_sha256": "a" * 64,
        "executed_source_sha256": "b" * 64,
        "source_map_sha256": "c" * 64,
        "mode": "capture",
        "worker_generation": 17,
        "worker_manifest_sha256": "d" * 64,
    }
    with pytest.raises(ValueError, match="visible_source_sha256"):
        contracts.OperationExecutionProvenance(
            visible_source_sha256="visible source text",
            executed_source_sha256="b" * 64,
            source_map_sha256="c" * 64,
            mode="capture",
        )


@pytest.mark.parametrize(
    "failure",
    [
        {
            "stage": "execution",
            "partial_results": {},
            "platform_diagnostic": "rdbg_pid=9182 token=private",
        },
        {
            "stage": "execution",
            "partial_results": {"result": "visible business source"},
        },
        {
            "stage": "x" * 1_000,
            "partial_results": {},
        },
    ],
)
def test_public_operation_failure_facts_reject_raw_or_unbounded_fields(
    failure: dict[str, object],
) -> None:
    """Break caught: public view_facts accepts arbitrary backend/source strings."""
    with pytest.raises((TypeError, ValueError)):
        facade_contracts.OperationViewFacts(failure=failure)


def test_wire_allowlist_has_no_public_runtime_registration_hook() -> None:
    assert not hasattr(contracts, "register_wire_dataclass")


def test_operation_descriptor_exposes_a_non_negative_event_cursor() -> None:
    descriptor = OperationDescriptor(
        operation_id="op-1",
        state=AgentOperationState.RUNNING,
        event_cursor=7,
    )
    assert to_wire(descriptor)["event_cursor"] == 7

    with pytest.raises(ValueError, match="event_cursor.*non-negative"):
        OperationDescriptor(operation_id="op-1", state=AgentOperationState.RUNNING, event_cursor=-1)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [("current_state", ["not", "a", "mapping"]), ("partial_results", 7)],
)
def test_failure_requires_mapping_shaped_state_fields(
    field_name: str,
    value: object,
) -> None:
    arguments: dict[str, object] = {
        "category": FailureCategory.CONFLICT,
        "state_changed": StateChanged.NO,
        "safe_to_retry": RetrySafety.NO,
        "current_state": {},
        "partial_results": {},
        "diagnostic_id": "diag-1",
    }
    arguments[field_name] = value

    with pytest.raises(TypeError, match=field_name):
        MethodFailure(**arguments)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("factory", "arguments"),
    [
        (
            MethodFailure,
            {
                "category": FailureCategory.CONFLICT,
                "state_changed": StateChanged.NO,
                "safe_to_retry": RetrySafety.NO,
                "current_state": {},
                "affected_proxies": "proxy-1",
                "diagnostic_id": "diag-1",
            },
        ),
        (
            MethodFailure,
            {
                "category": FailureCategory.CONFLICT,
                "state_changed": StateChanged.NO,
                "safe_to_retry": RetrySafety.NO,
                "current_state": {},
                "recommended_actions": "operation.status",
                "diagnostic_id": "diag-1",
            },
        ),
        (
            OperationOutput,
            {
                "operation_id": "op-1",
                "messages": "secret",
                "next_cursor": 1,
                "has_more": False,
            },
        ),
    ],
)
def test_sequence_contract_fields_reject_bare_strings(
    factory: object,
    arguments: dict[str, object],
) -> None:
    with pytest.raises(TypeError, match="sequence"):
        factory(**arguments)  # type: ignore[operator]


def test_enums_have_the_exact_public_values() -> None:
    assert {item.value for item in CapabilityMode} == {
        "observe",
        "experiment",
        "commit",
        "admin",
    }
    assert {item.value for item in FailureCategory} == {
        "invalid_request",
        "conflict",
        "stale",
        "denied",
        "limit",
        "unsupported",
        "platform_failure",
        "lost",
        "unknown",
    }
    assert {item.value for item in StateChanged} == {"no", "yes", "partial", "unknown"}
    assert {item.value for item in RetrySafety} == {
        "yes",
        "no",
        "after_status_check",
    }
    assert {item.value for item in AgentOperationState} == {
        "queued",
        "running",
        "completed",
        "captured",
        "failed",
        "unknown",
    }
    assert {item.value for item in CodeLanguage} == {"bsl", "python", "markdown"}
    assert {item.value for item in CodeMode} == {"main", "capture", "worker"}


def test_revision_and_runtime_generation_must_be_positive() -> None:
    with pytest.raises(ValueError, match="revision.*positive"):
        CodeRevision(
            cell_id="cell-main",
            revision=0,
            source="Ответ = 1;",
            source_sha256="a" * 64,
            document_sha256="b" * 64,
            language=CodeLanguage.BSL,
            mode=CodeMode.MAIN,
        )
    with pytest.raises(ValueError, match="generation.*positive"):
        RuntimeDescriptor(
            runtime_id="runtime-1",
            generation=0,
            state="ready",
            mode=CapabilityMode.OBSERVE,
        )


def test_operation_output_rejects_unbounded_messages() -> None:
    with pytest.raises(ValueError, match="messages.*bounded"):
        OperationOutput(
            operation_id="op-1",
            messages=tuple("message" for _ in range(MAX_OPERATION_MESSAGES + 1)),
            next_cursor=1,
            has_more=False,
        )


def test_workspace_rejects_duplicate_capability_names() -> None:
    capability = CapabilityDescriptor(
        name="capture",
        available=False,
        reason="unavailable_until_slice_2",
    )
    with pytest.raises(ValueError, match="duplicate capability"):
        WorkspaceDescriptor(
            workspace_id="workspace-1",
            project_name="demo",
            capabilities=(capability, capability),
        )


def test_protocol_errors_map_without_message_or_traceback() -> None:
    failure = failure_from_exception(
        ProtocolError("target=secret traceback=also-secret"),
        {"state": "running"},
    )
    wire = to_wire(failure)

    assert wire["category"] == "platform_failure"
    assert wire["state_changed"] == "unknown"
    assert wire["safe_to_retry"] == "after_status_check"
    assert wire["current_state"] == {"state": "running"}
    assert "target=secret" not in str(wire)
    assert "traceback" not in str(wire)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_wire_serializer_rejects_non_finite_floats(value: float) -> None:
    with pytest.raises(TypeError, match="wire-safe"):
        to_wire(value)
