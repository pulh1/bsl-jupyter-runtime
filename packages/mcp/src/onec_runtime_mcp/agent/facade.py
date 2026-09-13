"""Intent-oriented client facade over the complete Domain service catalog."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from onec_runtime_mcp.agent.contracts import (
    AgentOperationState,
    CapabilityMode,
    MethodFailure,
    FailureCategory,
    OperationDescriptor,
    OperationExecutionProvenance,
    RetrySafety,
    StateChanged,
    RuntimeDescriptor,
    ServiceResponse,
    to_wire,
)
from onec_runtime_mcp.agent.capture_contracts import CaptureFence, CaptureView
from onec_runtime_mcp.agent.facade_contracts import (
    AgentOperationIdentity,
    AgentOperationKind,
    AgentOperationView,
    MutationConfidence,
    OperationTruncation,
    RecoveryAction,
)
from onec_runtime_mcp.agent.observation import ObservationPlan
from onec_runtime_mcp.agent.value_policy import ValueBudgetProfiles, ValueCostClass
from onec_runtime_mcp.agent.proxies import (
    MeasurementCost,
    ProxyConsistency,
    ProxyDescriptor,
    ProxyFence,
    ProxyLifetime,
    ProxyProvenance,
    ProxyRealm,
    SizeAccuracy,
    ValuePreview,
    ValueSize,
)


class FacadeClient(Protocol):
    def call(self, method: str, arguments: dict[str, object]) -> ServiceResponse: ...


@dataclass(frozen=True, slots=True)
class AgentFacadeError(RuntimeError):
    failure: MethodFailure | None

    def __str__(self) -> str:
        return "agent facade request failed"


class AgentFacade:
    """Small explicit palette. It never owns a runtime or operation registry."""

    def __init__(self, client: FacadeClient) -> None:
        if not hasattr(client, "call"):
            raise TypeError("facade client must provide call")
        self._client = client

    def workspace_open(self, request: Mapping[str, object]) -> object:
        return self._value("workspace.open", request)

    def workspace_status(self, request: Mapping[str, object]) -> object:
        return self._value("workspace.status", request)

    def workspace_variables(self, request: Mapping[str, object]) -> object:
        return self._value("workspace.variables", request)

    def runtime_ensure(
        self, request: Mapping[str, object]
    ) -> RuntimeDescriptor | AgentOperationView:
        value = self._value("runtime.ensure", request)
        operation_id = _operation_id(value)
        if operation_id is not None:
            return self._operation_view(operation_id, after_message_cursor=0)
        return _runtime_descriptor(value)

    def runtime_restart(self, request: Mapping[str, object]) -> object:
        return self._value("runtime.restart", request)

    def runtime_close(self, request: Mapping[str, object]) -> object:
        return self._value("runtime.close", request)

    def code_list(self, request: Mapping[str, object]) -> object:
        return self._value("code.list", request)

    def code_get(self, request: Mapping[str, object]) -> object:
        return self._value("code.get", request)

    def code_put(self, request: Mapping[str, object]) -> object:
        return self._value("code.put", request)

    def code_run(self, request: Mapping[str, object]) -> AgentOperationView:
        arguments = self._execution_request(request)
        value = self._value("code.run", arguments)
        operation_id = _required_operation_id(value)
        return self._operation_view(operation_id, after_message_cursor=0)

    def code_run_inline(self, request: Mapping[str, object]) -> object:
        arguments = self._execution_request(request)
        value = self._value("code.run_inline", arguments)
        operation_id = _operation_id(value)
        return (
            self._operation_view(operation_id, after_message_cursor=0)
            if operation_id is not None
            else value
        )

    def capture_run_until(self, request: Mapping[str, object]) -> AgentOperationView:
        request_id = request.get("request_id")
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("capture.run_until requires a non-empty request_id")
        value = self._value("capture.run_until", request)
        return _agent_operation_view(value)

    def capture_hypothesis(self, request: Mapping[str, object]) -> AgentOperationView:
        allowed = {"fence", "code_ref", "observe", "request_id", "wait_s"}
        if set(request) - allowed:
            raise ValueError("unsupported capture.hypothesis argument")
        request_id = request.get("request_id")
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("capture.hypothesis requires a non-empty request_id")
        code_ref = request.get("code_ref")
        if not isinstance(code_ref, Mapping) or set(code_ref) != {
            "cell_id", "revision", "source_sha256"
        }:
            raise ValueError("capture.hypothesis requires an exact code_ref")
        arguments = dict(request)
        arguments["fence"] = to_wire(CaptureFence.from_wire(arguments.get("fence")))
        arguments["code_ref"] = dict(code_ref)
        if "observe" in arguments:
            arguments["observe"] = to_wire(ObservationPlan.from_wire(arguments["observe"]))
        return _agent_operation_view(self._value("capture.hypothesis", arguments))

    def capture_continue(self, request: Mapping[str, object]) -> AgentOperationView:
        """Resume one exact paused capture through the one-use continuation API."""
        allowed = {"fence", "next_points", "observe", "request_id", "wait_s"}
        if set(request) - allowed:
            raise ValueError("unsupported capture.continue argument")
        request_id = request.get("request_id")
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("capture.continue requires a non-empty request_id")
        arguments = dict(request)
        arguments["fence"] = to_wire(CaptureFence.from_wire(arguments.get("fence")))
        points = arguments.get("next_points", ())
        if isinstance(points, str) or not isinstance(points, (tuple, list)):
            raise ValueError("capture.continue next_points must be a sequence")
        arguments["next_points"] = [dict(point) for point in points if isinstance(point, Mapping)]
        if len(arguments["next_points"]) != len(points):
            raise ValueError("capture.continue next_points must contain mappings")
        if "observe" in arguments:
            arguments["observe"] = to_wire(ObservationPlan.from_wire(arguments["observe"]))
        return _agent_operation_view(self._value("capture.continue", arguments))

    def capture_inspect(self, request: Mapping[str, object]) -> object:
        allowed = {"fence", "filters", "cursor", "limit", "observe"}
        if set(request) - allowed:
            raise ValueError("unsupported capture.inspect argument")
        arguments = dict(request)
        arguments["fence"] = to_wire(CaptureFence.from_wire(arguments.get("fence")))
        filters = arguments.get("filters", {})
        if not isinstance(filters, Mapping) or set(filters) - {"name", "role", "type"}:
            raise ValueError("unsupported capture.inspect filter")
        arguments["filters"] = dict(filters)
        for name, default in (("cursor", 0), ("limit", 20)):
            value = arguments.get(name, default)
            if type(value) is not int or value < 0 or (name == "limit" and value == 0):
                raise ValueError(f"capture.inspect {name} is invalid")
            arguments[name] = value
        if "observe" in arguments:
            arguments["observe"] = to_wire(ObservationPlan.from_wire(arguments["observe"]))
        return self._value("capture.inspect", arguments)

    def capture_stack(self, request: Mapping[str, object]) -> object:
        if set(request) - {"fence", "cursor", "limit"}:
            raise ValueError("unsupported capture.stack argument")
        arguments = dict(request)
        arguments["fence"] = to_wire(CaptureFence.from_wire(arguments.get("fence")))
        cursor = arguments.get("cursor", 0)
        limit = arguments.get("limit", 20)
        if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("capture.stack page is invalid")
        arguments.update(cursor=cursor, limit=limit)
        return self._value("capture.stack", arguments)

    def capture_frame(self, request: Mapping[str, object]) -> object:
        if set(request) - {"fence", "level", "cursor", "limit", "name"}:
            raise ValueError("unsupported capture.frame argument")
        arguments = dict(request)
        arguments["fence"] = to_wire(CaptureFence.from_wire(arguments.get("fence")))
        level = arguments.get("level")
        cursor = arguments.get("cursor", 0)
        limit = arguments.get("limit", 20)
        name = arguments.get("name")
        if type(level) is not int or level < 0:
            raise ValueError("capture.frame level is invalid")
        if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("capture.frame page is invalid")
        if name is not None and (not isinstance(name, str) or not name or len(name) > 256):
            raise ValueError("capture.frame name is invalid")
        arguments.update(cursor=cursor, limit=limit)
        return self._value("capture.frame", arguments)

    def operation_wait(self, request: Mapping[str, object]) -> AgentOperationView:
        if set(request) - {
            "operation_id",
            "timeout_s",
            "after_event_cursor",
            "after_message_cursor",
        }:
            raise ValueError("unsupported operation.wait argument")
        operation_id = request.get("operation_id")
        after_event_cursor = request.get("after_event_cursor", 0)
        after_message_cursor = request.get("after_message_cursor", 0)
        if not isinstance(operation_id, str) or not operation_id:
            raise ValueError("operation_id must be non-empty")
        for name, cursor in (
            ("after_event_cursor", after_event_cursor),
            ("after_message_cursor", after_message_cursor),
        ):
            if type(cursor) is not int or cursor < 0:
                raise ValueError(f"{name} must be non-negative")
        arguments = {
            "operation_id": operation_id,
            "timeout_s": request.get("timeout_s", 0),
            "after_cursor": after_event_cursor,
        }
        self._value("operation.wait", arguments)
        return self._operation_view(
            operation_id,
            after_message_cursor=after_message_cursor,
        )

    def value_inspect(self, request: Mapping[str, object]) -> object:
        return self._value("value.inspect", request)

    def value_materialize(self, request: Mapping[str, object]) -> object:
        arguments = self._value_request(request)
        refs = arguments.pop("refs", "presentation")
        arguments["target"] = "python"
        arguments["policy"] = {"refs": refs}
        return self._value("value.materialize", arguments)

    def value_to_df(self, request: Mapping[str, object]) -> object:
        return self._value("value.to_df", self._value_request(request))

    def python_run(self, request: Mapping[str, object]) -> object:
        return self._value("python.run", request)

    def _execution_request(self, request: Mapping[str, object]) -> dict[str, object]:
        arguments = dict(request)
        if "observe" in arguments:
            plan = ObservationPlan.from_wire(arguments["observe"])
            arguments["observe"] = to_wire(plan)
        return arguments

    @staticmethod
    def _value_request(request: Mapping[str, object]) -> dict[str, object]:
        arguments = dict(request)
        profile = arguments.pop("budget_profile", "agent_dataframe")
        if not isinstance(profile, str):
            raise ValueError("budget_profile must be a string")
        profiles = ValueBudgetProfiles()
        if not profiles.permits(profile, ValueCostClass.FULL_SCAN):
            raise ValueError("budget profile does not permit full materialization")
        budget = profiles.resolve(profile)
        arguments["budget"] = {
            "depth": budget.max_depth,
            "items": budget.max_items,
            "rows": budget.max_rows,
            "bytes": budget.max_bytes,
            "timeout_s": budget.timeout_seconds,
        }
        return arguments

    def _operation_view(
        self, operation_id: str, *, after_message_cursor: int
    ) -> AgentOperationView:
        value = self._value(
            "operation.view",
            {
                "operation_id": operation_id,
                "after_message_cursor": after_message_cursor,
            },
        )
        return _agent_operation_view(value)

    def _value(self, method: str, request: Mapping[str, object]) -> object:
        if not isinstance(request, Mapping):
            raise TypeError("facade request must be a mapping")
        try:
            response = self._client.call(method, dict(request))
        except BaseException as error:
            raise AgentFacadeError(
                MethodFailure(
                    FailureCategory.PLATFORM_FAILURE,
                    StateChanged.UNKNOWN,
                    RetrySafety.AFTER_STATUS_CHECK,
                    {"control_transport": "unavailable"},
                    diagnostic_id=f"diag_{uuid4().hex}",
                )
            ) from error
        if not isinstance(response, ServiceResponse):
            raise RuntimeError("facade client returned an invalid response")
        if not response.ok:
            raise AgentFacadeError(response.failure)
        return response.value


def _operation_id(value: object) -> str | None:
    if isinstance(value, OperationDescriptor):
        return value.operation_id
    if isinstance(value, Mapping) and isinstance(value.get("operation_id"), str):
        return value["operation_id"]  # type: ignore[return-value]
    return None


def _required_operation_id(value: object) -> str:
    operation_id = _operation_id(value)
    if operation_id is None:
        raise RuntimeError("Domain execution did not return an operation")
    return operation_id


def _runtime_descriptor(value: object) -> RuntimeDescriptor:
    if isinstance(value, RuntimeDescriptor):
        return value
    if not isinstance(value, Mapping):
        raise RuntimeError("Domain runtime descriptor is invalid")
    return RuntimeDescriptor(
        runtime_id=value.get("runtime_id"),  # type: ignore[arg-type]
        generation=value.get("generation"),  # type: ignore[arg-type]
        state=value.get("state"),  # type: ignore[arg-type]
        mode=CapabilityMode(value.get("mode")),  # type: ignore[arg-type]
        active_operation_id=value.get("active_operation_id"),  # type: ignore[arg-type]
        health=value.get("health", ""),  # type: ignore[arg-type]
    )


def _agent_operation_view(value: object) -> AgentOperationView:
    if isinstance(value, AgentOperationView):
        return value
    if not isinstance(value, Mapping):
        raise RuntimeError("Domain operation view is invalid")
    identity = value.get("operation")
    truncation = value.get("truncation")
    if not isinstance(identity, Mapping) or not isinstance(truncation, Mapping):
        raise RuntimeError("Domain operation view is incomplete")
    changed = value.get("changed_variables", ())
    outputs = value.get("outputs", {})
    recovery = value.get("recovery", ())
    capture = value.get("capture")
    execution_provenance = value.get("execution_provenance")
    if (
        isinstance(changed, str)
        or not isinstance(changed, (tuple, list))
        or not isinstance(outputs, Mapping)
        or isinstance(recovery, str)
        or not isinstance(recovery, (tuple, list))
    ):
        raise RuntimeError("Domain operation view collections are invalid")
    return AgentOperationView(
        operation=AgentOperationIdentity(
            operation_id=identity.get("operation_id"),  # type: ignore[arg-type]
            kind=AgentOperationKind(identity.get("kind")),  # type: ignore[arg-type]
            runtime_id=identity.get("runtime_id"),  # type: ignore[arg-type]
            runtime_generation=identity.get("runtime_generation"),  # type: ignore[arg-type]
            cell_id=identity.get("cell_id"),  # type: ignore[arg-type]
            revision=identity.get("revision"),  # type: ignore[arg-type]
            source_sha256=identity.get("source_sha256"),  # type: ignore[arg-type]
        ),
        state=AgentOperationState(value.get("state")),  # type: ignore[arg-type]
        messages=tuple(value.get("messages", ())),  # type: ignore[arg-type]
        next_message_cursor=value.get("next_message_cursor"),  # type: ignore[arg-type]
        next_event_cursor=value.get("next_event_cursor"),  # type: ignore[arg-type]
        changed_variables=tuple(_proxy(item) for item in changed),
        change_confidence=MutationConfidence(
            value.get("change_confidence", "unknown")
        ),
        outputs={str(alias): _proxy(item) for alias, item in outputs.items()},
        capture=None
        if capture is None
        else capture
        if isinstance(capture, CaptureView)
        else CaptureView.from_wire(capture),
        failure=value.get("failure"),  # type: ignore[arg-type]
        recovery=tuple(RecoveryAction.from_wire(item) for item in recovery),
        truncation=OperationTruncation(
            messages=truncation.get("messages"),  # type: ignore[arg-type]
            changed_variables=truncation.get("changed_variables"),  # type: ignore[arg-type]
            outputs=truncation.get("outputs"),  # type: ignore[arg-type]
        ),
        execution_provenance=(
            None
            if execution_provenance is None
            else OperationExecutionProvenance.from_wire(execution_provenance)
        ),
    )


def _proxy(value: object) -> ProxyDescriptor:
    if isinstance(value, ProxyDescriptor):
        return value
    if not isinstance(value, Mapping):
        raise RuntimeError("proxy descriptor is invalid")
    fence = value.get("fence")
    provenance = value.get("provenance")
    if not isinstance(fence, Mapping) or not isinstance(provenance, Mapping):
        raise RuntimeError("proxy descriptor fence is invalid")
    known = value.get("known_size")
    preview = value.get("bounded_preview")
    return ProxyDescriptor(
        proxy_id=value.get("proxy_id"),  # type: ignore[arg-type]
        realm=ProxyRealm(value.get("realm")),  # type: ignore[arg-type]
        lifetime=ProxyLifetime(value.get("lifetime")),  # type: ignore[arg-type]
        qualified_name=value.get("qualified_name"),  # type: ignore[arg-type]
        type_name=value.get("type_name"),  # type: ignore[arg-type]
        version=value.get("version"),  # type: ignore[arg-type]
        consistency=ProxyConsistency(value.get("consistency")),  # type: ignore[arg-type]
        fence=ProxyFence.from_wire(fence),
        provenance=ProxyProvenance(
            cell_id=provenance.get("cell_id"),  # type: ignore[arg-type]
            revision=provenance.get("revision"),  # type: ignore[arg-type]
            source_sha256=provenance.get("source_sha256"),  # type: ignore[arg-type]
            operation_id=provenance.get("operation_id"),  # type: ignore[arg-type]
            parent_proxy_ids=tuple(provenance.get("parent_proxy_ids", ())),  # type: ignore[arg-type]
        ),
        capabilities=tuple(value.get("capabilities", ())),  # type: ignore[arg-type]
        known_size=None if known is None else _value_size(known),
        bounded_preview=None if preview is None else _value_preview(preview),
    )


def _value_size(value: object) -> ValueSize:
    if isinstance(value, ValueSize):
        return value
    if not isinstance(value, Mapping):
        raise RuntimeError("value size is invalid")
    return ValueSize(
        items=value.get("items"),  # type: ignore[arg-type]
        rows=value.get("rows"),  # type: ignore[arg-type]
        bytes=value.get("bytes"),  # type: ignore[arg-type]
        accuracy=SizeAccuracy(value.get("accuracy", "unknown")),  # type: ignore[arg-type]
        cost=MeasurementCost(value.get("cost", "cheap")),  # type: ignore[arg-type]
    )


def _value_preview(value: object) -> ValuePreview:
    if isinstance(value, ValuePreview):
        return value
    if not isinstance(value, Mapping):
        raise RuntimeError("value preview is invalid")
    return ValuePreview(
        type_name=value.get("type_name"),  # type: ignore[arg-type]
        scalar=value.get("scalar"),  # type: ignore[arg-type]
        sample=tuple(value.get("sample", ())),  # type: ignore[arg-type]
        truncated=value.get("truncated", False),  # type: ignore[arg-type]
    )


__all__ = ["AgentFacade", "AgentFacadeError"]
