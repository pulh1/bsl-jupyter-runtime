from __future__ import annotations

import json
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Iterator, Literal

import pytest

import onec_runtime_mcp.mcp_entrypoint as mcp_entrypoint
from onec_runtime_mcp.agent.contracts import ServiceResponse
from onec_runtime_mcp.agent.control_protocol import ControlEndpoint
from onec_runtime_mcp.mcp_entrypoint import _connect_existing_service


@contextmanager
def unrelated_control_server(
    tmp_path: Path,
    *,
    outcome: Literal["wrong_instance", "wrong_token"],
) -> Iterator[list[str]]:
    authorizations: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            return None

        def do_GET(self) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self) -> None:
            authorizations.append(self.headers.get("Authorization", ""))
            if outcome == "wrong_token":
                self.send_response(HTTPStatus.UNAUTHORIZED)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            payload = json.dumps({"ok": True, "value": {"service_instance_id": "other-instance"}}).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    control = tmp_path / ".runtime" / "agent-service" / "control"
    endpoint = ControlEndpoint("127.0.0.1", server.server_port, "expected-instance", control / "endpoint.json", control / "token")
    endpoint.write_descriptor()
    endpoint.token_path.write_text(json.dumps({"token": "expected-token"}), encoding="utf-8")
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield authorizations
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("outcome", ["wrong_instance", "wrong_token"])
def test_connect_existing_service_rejects_health_only_unrelated_endpoints(
    tmp_path: Path,
    outcome: Literal["wrong_instance", "wrong_token"],
) -> None:
    with unrelated_control_server(tmp_path, outcome=outcome) as authorizations:
        with pytest.raises(ValueError, match="service descriptor"):
            _connect_existing_service(tmp_path)

    assert authorizations == ["Bearer expected-token"]


def test_configured_control_timeout_reaches_service_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = tmp_path / ".runtime" / "control"
    endpoint = ControlEndpoint(
        "127.0.0.1",
        32123,
        "expected-instance",
        control / "endpoint.json",
        control / "token",
    )
    endpoint.write_descriptor()
    endpoint.token_path.write_text(json.dumps({"token": "private"}), encoding="utf-8")
    observed: dict[str, object] = {}

    class FakeHealth:
        status_code = 200

    class FakeServiceClient:
        def __init__(
            self,
            configured_endpoint: ControlEndpoint,
            *,
            caller_id: str | None,
            timeout_s: float,
        ) -> None:
            observed.update(
                endpoint=configured_endpoint,
                caller_id=caller_id,
                timeout_s=timeout_s,
            )

        def call(self, method: str, arguments: dict[str, object]) -> ServiceResponse:
            assert method == "workspace.status"
            assert arguments == {}
            return ServiceResponse.success(
                {"service_instance_id": "expected-instance"}
            )

    monkeypatch.setenv("ONEC_RUNTIME_SERVICE_DESCRIPTOR", str(endpoint.descriptor_path))
    monkeypatch.setenv("ONEC_RUNTIME_CONTROL_TIMEOUT_S", "180")
    monkeypatch.setattr(mcp_entrypoint.httpx, "get", lambda *_args, **_kwargs: FakeHealth())
    monkeypatch.setattr(mcp_entrypoint, "ServiceClient", FakeServiceClient)

    _connect_existing_service(tmp_path)

    assert observed["endpoint"] == endpoint
    assert observed["timeout_s"] == 180.0


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "0", "-1", "180.1", "not-a-number"])
def test_control_timeout_rejects_nonfinite_nonpositive_and_excessive_values(
    raw: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONEC_RUNTIME_CONTROL_TIMEOUT_S", raw)

    with pytest.raises(ValueError, match="control timeout"):
        mcp_entrypoint._configured_control_timeout_s()


def test_control_timeout_default_is_deliberate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ONEC_RUNTIME_CONTROL_TIMEOUT_S", raising=False)

    assert mcp_entrypoint._configured_control_timeout_s() == 10.0


def test_mcp_profile_defaults_to_agent_and_accepts_expert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ONEC_RUNTIME_MCP_PROFILE", raising=False)
    assert mcp_entrypoint._configured_mcp_profile() == "agent"
    monkeypatch.setenv("ONEC_RUNTIME_MCP_PROFILE", "expert")
    assert mcp_entrypoint._configured_mcp_profile() == "expert"


@pytest.mark.parametrize("raw", ["", "full", "AGENT", "unknown"])
def test_mcp_profile_rejects_malformed_environment_before_stdio(
    raw: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONEC_RUNTIME_MCP_PROFILE", raw)

    with pytest.raises(ValueError, match="MCP profile"):
        mcp_entrypoint._configured_mcp_profile()


def test_main_rejects_bad_profile_before_service_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connected = False

    def unexpected_connect(_workspace: Path) -> object:
        nonlocal connected
        connected = True
        raise AssertionError("profile must be validated first")

    monkeypatch.setenv("ONEC_RUNTIME_MCP_PROFILE", "not-a-profile")
    monkeypatch.setattr(mcp_entrypoint, "_connect_existing_service", unexpected_connect)

    assert mcp_entrypoint.main(["--workspace", str(tmp_path)]) == 2
    assert connected is False
