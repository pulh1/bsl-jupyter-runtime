from __future__ import annotations

import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from uuid import uuid4

import httpx
import pytest

from onec_runtime_mcp.agent.control_protocol import ControlEndpoint
from onec_runtime_mcp.agent.control_protocol import ControlProtocolError, MAX_PAYLOAD_BYTES, parse_call
from onec_runtime_mcp.agent.service_client import ServiceClient
from onec_runtime_mcp.agent.service_server import AgentControlServer


class EmptyFactory:
    def start(self, **_kwargs: object) -> object:
        raise AssertionError("runtime startup is not expected")


@pytest.fixture
def service_server(tmp_path: Path):
    from onec_runtime_mcp.agent.service import AgentWorkspaceService

    server = AgentControlServer(AgentWorkspaceService(tmp_path, EmptyFactory()), tmp_path)
    server.start()
    try:
        yield server
    finally:
        server.close()


def test_control_client_requires_token_and_uses_json_only(service_server) -> None:
    denied = httpx.post(
        service_server.url + "/v1/call",
        json={"request_id": "0" * 36, "caller_id": "test", "method": "workspace.status", "arguments": {}},
    )
    assert denied.status_code == 401
    response = ServiceClient(service_server.endpoint, caller_id="test").call("workspace.status", {})
    assert response.ok is True
    assert response.value["project_root"] == str(service_server.project_root)
    assert service_server.token not in service_server.endpoint.descriptor_path.read_text(encoding="utf-8")


def test_protocol_rejects_non_json_bad_request_ids_and_non_loopback(tmp_path: Path, service_server) -> None:
    with pytest.raises(ValueError):
        ControlEndpoint("0.0.0.0", 9999, "instance", tmp_path / "endpoint.json", tmp_path / "token")
    headers = {"Authorization": "Bearer " + service_server.token, "Content-Type": "text/plain"}
    bad_type = httpx.post(service_server.url + "/v1/call", content=b"hello", headers=headers)
    assert bad_type.status_code == 415
    bad_id = httpx.post(service_server.url + "/v1/call", json={"request_id": "bad", "caller_id": "a", "method": "workspace.status", "arguments": {}}, headers={"Authorization": "Bearer " + service_server.token})
    assert bad_id.status_code == 400
    assert httpx.get(service_server.url + "/health").status_code == 200


def test_protocol_rejects_duplicate_ids_and_unauthenticated_shutdown(service_server) -> None:
    request_id = str(uuid4())
    payload = {"request_id": request_id, "caller_id": "a", "method": "workspace.status", "arguments": {}}
    headers = {"Authorization": "Bearer " + service_server.token}
    assert httpx.post(service_server.url + "/v1/call", json=payload, headers=headers).status_code == 200
    assert httpx.post(service_server.url + "/v1/call", json=payload, headers=headers).status_code == 409
    assert httpx.post(service_server.url + "/v1/shutdown", content=b"{}", headers={"Content-Type": "application/json"}).status_code == 401


def test_parser_accepts_reordered_fields_and_rejects_nonstandard_json_constants() -> None:
    request_id = str(uuid4())
    parsed = parse_call((f'{{"method":"workspace.status","arguments":{{}},"caller_id":"caller","request_id":"{request_id}"}}').encode())
    assert parsed.request_id == request_id and parsed.method == "workspace.status"
    for constant in (b'{"request_id":"00000000-0000-0000-0000-000000000000","caller_id":"caller","method":"x","arguments":{"x":NaN}}', b'{"request_id":"00000000-0000-0000-0000-000000000000","caller_id":"caller","method":"x","arguments":{"x":Infinity}}'):
        with pytest.raises(ControlProtocolError):
            parse_call(constant)


def test_concurrent_duplicate_request_id_dispatches_once(service_server, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    calls_lock = Lock()
    original = service_server.service.call

    def counted(*args: object, **kwargs: object):
        nonlocal calls
        with calls_lock:
            calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(service_server.service, "call", counted)
    request_id = str(uuid4())
    barrier = Barrier(8)
    payload = {"request_id": request_id, "caller_id": "caller", "method": "workspace.status", "arguments": {}}

    def post() -> int:
        barrier.wait()
        return httpx.post(service_server.url + "/v1/call", json=payload, headers={"Authorization": "Bearer " + service_server.token}).status_code

    with ThreadPoolExecutor(max_workers=8) as workers:
        statuses = list(workers.map(lambda _value: post(), range(8)))
    assert statuses.count(200) == 1
    assert statuses.count(409) == 7
    assert calls == 1


def test_client_maps_transport_timeout_to_sanitized_service_failure(service_server, monkeypatch: pytest.MonkeyPatch) -> None:
    def timeout(*_args: object, **_kwargs: object) -> object:
        raise httpx.TimeoutException("secret network detail")

    monkeypatch.setattr(httpx, "post", timeout)
    response = ServiceClient(service_server.endpoint, caller_id="caller").call("workspace.status", {})
    assert response.ok is False
    assert response.failure.category.value == "platform_failure"
    assert response.failure.current_state == {
        "control_transport": "timeout",
        "configured_timeout_s": 10.0,
    }


def test_parser_rejects_malformed_json_and_payload_cap() -> None:
    with pytest.raises(ControlProtocolError):
        parse_call(b"{")
    with pytest.raises(ControlProtocolError):
        parse_call(b"x" * (MAX_PAYLOAD_BYTES + 1))


def test_parser_rejects_extra_envelope_fields() -> None:
    payload = {"request_id": str(uuid4()), "caller_id": "caller", "method": "workspace.status", "arguments": {}, "extra": True}
    with pytest.raises(ControlProtocolError):
        parse_call(json.dumps(payload).encode())
