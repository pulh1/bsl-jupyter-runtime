"""Small JSON-only client for a running local agent service."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from uuid import uuid4

import httpx

from onec_runtime_mcp.agent.contracts import FailureCategory, MethodFailure, RetrySafety, ServiceResponse, StateChanged
from onec_runtime_mcp.agent.control_protocol import ControlEndpoint


DEFAULT_CONTROL_TIMEOUT_S = 10.0


class ServiceClient:
    def __init__(self, endpoint: ControlEndpoint | str | Path, *, caller_id: str | None = None, timeout_s: float = DEFAULT_CONTROL_TIMEOUT_S) -> None:
        self.endpoint = ControlEndpoint.read(endpoint) if isinstance(endpoint, (str, Path)) else endpoint
        try: self._token = json.loads(self.endpoint.token_path.read_text(encoding="utf-8"))["token"]
        except (OSError, KeyError, json.JSONDecodeError, TypeError) as error: raise ValueError("invalid control token") from error
        if not isinstance(self._token, str) or not self._token: raise ValueError("invalid control token")
        self.caller_id = caller_id or str(uuid4()); self.timeout_s = timeout_s
    def call(self, method: str, arguments: dict[str, object]) -> ServiceResponse:
        try:
            response = httpx.post(self.endpoint.url + "/v1/call", json={"request_id": str(uuid4()), "caller_id": self.caller_id, "method": method, "arguments": arguments}, headers={"Authorization": "Bearer " + self._token}, timeout=self.timeout_s)
            response.raise_for_status(); payload = response.json()
        except httpx.TimeoutException:
            return self._transport_failure(
                "timeout", configured_timeout_s=self.timeout_s
            )
        except (httpx.HTTPError, ValueError):
            return self._transport_failure("unavailable")
        try:
            return self._response_from_wire(payload)
        except (KeyError, TypeError, ValueError):
            return self._transport_failure("invalid_response")

    @staticmethod
    def _response_from_wire(payload: object) -> ServiceResponse:
        if not isinstance(payload, Mapping) or type(payload.get("ok")) is not bool:
            raise ValueError("invalid control response")
        if payload["ok"] is True:
            if payload.get("failure") is not None:
                raise ValueError("successful response includes failure")
            return ServiceResponse.success(payload.get("value"))
        failure = payload.get("failure")
        if not isinstance(failure, Mapping):
            raise ValueError("failed response requires failure")
        required = {
            "category",
            "state_changed",
            "safe_to_retry",
            "current_state",
            "diagnostic_id",
        }
        allowed = required | {
            "operation_id",
            "affected_proxies",
            "partial_results",
            "event_cursor",
            "recommended_actions",
        }
        if not required <= set(failure) or set(failure) - allowed:
            raise ValueError("failure response shape is invalid")
        current_state = failure["current_state"]
        partial_results = failure.get("partial_results", {})
        affected = failure.get("affected_proxies", ())
        actions = failure.get("recommended_actions", ())
        if (
            not isinstance(current_state, Mapping)
            or not isinstance(partial_results, Mapping)
            or isinstance(affected, str)
            or not isinstance(affected, Sequence)
            or isinstance(actions, str)
            or not isinstance(actions, Sequence)
        ):
            raise TypeError("failure response fields are invalid")
        return ServiceResponse.fail(
            MethodFailure(
                category=FailureCategory(failure["category"]),
                state_changed=StateChanged(failure["state_changed"]),
                safe_to_retry=RetrySafety(failure["safe_to_retry"]),
                current_state=dict(current_state),
                operation_id=failure.get("operation_id"),  # type: ignore[arg-type]
                affected_proxies=tuple(affected),  # type: ignore[arg-type]
                partial_results=dict(partial_results),
                event_cursor=failure.get("event_cursor"),  # type: ignore[arg-type]
                recommended_actions=tuple(actions),  # type: ignore[arg-type]
                diagnostic_id=failure["diagnostic_id"],  # type: ignore[arg-type]
            )
        )

    @staticmethod
    def _transport_failure(marker: str, **state: object) -> ServiceResponse:
        return ServiceResponse.fail(
            MethodFailure(
                FailureCategory.PLATFORM_FAILURE,
                StateChanged.UNKNOWN,
                RetrySafety.AFTER_STATUS_CHECK,
                {"control_transport": marker, **state},  # type: ignore[arg-type]
                diagnostic_id=f"diag_{uuid4().hex}",
            )
        )
    def shutdown(self) -> None:
        response = httpx.post(self.endpoint.url + "/v1/shutdown", content=b"{}", headers={"Authorization": "Bearer " + self._token, "Content-Type": "application/json"}, timeout=self.timeout_s)
        response.raise_for_status()
