from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from onec_runtime_mcp.agent.contracts import FailureCategory, RetrySafety, StateChanged
from onec_runtime_mcp.agent.control_protocol import ControlEndpoint
from onec_runtime_mcp.agent.service_client import ServiceClient


def _client(tmp_path: Path) -> ServiceClient:
    token = tmp_path / "token.json"
    token.write_text(json.dumps({"token": "private"}), encoding="utf-8")
    return ServiceClient(
        ControlEndpoint("127.0.0.1", 32123, "instance", tmp_path / "endpoint.json", token)
    )


@pytest.mark.parametrize("failure", [httpx.ConnectError("no route"), ValueError("bad json")])
def test_transport_and_json_failures_are_structured_platform_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    monkeypatch.setattr(httpx, "post", lambda *_args, **_kwargs: (_ for _ in ()).throw(failure))

    response = _client(tmp_path).call("workspace.status", {})

    assert response.ok is False
    assert response.failure.category is FailureCategory.PLATFORM_FAILURE
    assert response.failure.state_changed is StateChanged.UNKNOWN
    assert response.failure.safe_to_retry is RetrySafety.AFTER_STATUS_CHECK


def test_failure_wire_requires_all_fences_and_preserves_optional_fields(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client._response_from_wire(
        {
            "ok": False,
            "value": None,
            "failure": {
                "category": "conflict",
                "state_changed": "no",
                "safe_to_retry": "after_status_check",
                "current_state": {"revision": 4},
                "operation_id": "op-1",
                "affected_proxies": ["proxy-1"],
                "partial_results": {"a": 1},
                "event_cursor": 7,
                "recommended_actions": ["operation.wait"],
                "diagnostic_id": "diag-1",
            },
        }
    )

    assert response.failure.operation_id == "op-1"
    assert response.failure.event_cursor == 7
    assert response.failure.affected_proxies == ("proxy-1",)


def test_malformed_failure_wire_is_fail_closed(tmp_path: Path) -> None:
    with pytest.raises((TypeError, ValueError)):
        _client(tmp_path)._response_from_wire(
            {"ok": False, "failure": {"category": "conflict"}}
        )
