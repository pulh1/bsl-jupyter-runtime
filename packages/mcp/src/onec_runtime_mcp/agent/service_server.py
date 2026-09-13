"""Foreground-only authenticated loopback HTTP server for AgentWorkspaceService."""

from __future__ import annotations

import hmac
import json
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from secrets import token_urlsafe
from threading import Event, Lock, Thread
from typing import Any

from onec_runtime_mcp.agent.contracts import ServiceResponse, to_wire
from onec_runtime_mcp.agent.control_protocol import (
    MAX_PAYLOAD_BYTES, ControlEndpoint, ControlProtocolError, _write_private_json, parse_call,
)
from onec_runtime_mcp.agent.service import AgentWorkspaceService


class AgentControlServer:
    def __init__(
        self,
        service: AgentWorkspaceService,
        project_root: str | Path | None = None,
        *,
        descriptor_path: str | Path | None = None,
    ) -> None:
        self.service = service
        self.project_root = Path(project_root or service.project_root).resolve(strict=True)
        self.token = token_urlsafe(32)
        self._seen: deque[str] = deque(maxlen=4096)
        self._seen_set: set[str] = set()
        self._seen_lock = Lock()
        self._http = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_type())
        if descriptor_path is None:
            control = self.project_root / ".runtime" / "agent-service" / "control"
        else:
            configured = Path(descriptor_path).resolve()
            if self.project_root not in configured.parents or configured.name != "endpoint.json":
                raise ValueError("service descriptor must be endpoint.json within the workspace")
            control = configured.parent
        self.endpoint = ControlEndpoint("127.0.0.1", self._http.server_port, service.service_instance_id, control / "endpoint.json", control / "token")
        self._thread: Thread | None = None
        self._closed = Event()

    @property
    def url(self) -> str:
        return self.endpoint.url

    def start(self) -> None:
        if self._thread is not None: return
        self.endpoint.write_descriptor()
        _write_private_json(self.endpoint.token_path, {"token": self.token})
        self._thread = Thread(target=self._http.serve_forever, name="onec-agent-control", daemon=True)
        self._thread.start()

    def close(self) -> None:
        if self._closed.is_set(): return
        self._closed.set()
        self._http.shutdown(); self._http.server_close()
        if self._thread is not None: self._thread.join(timeout=5); self._thread = None
        self.service.close()

    def wait_closed(self) -> None:
        self._closed.wait()

    def _handler_type(self) -> type[BaseHTTPRequestHandler]:
        parent = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, _format: str, *_args: Any) -> None:
                return None
            def do_GET(self) -> None:
                if self.path != "/health": return self._error(HTTPStatus.NOT_FOUND)
                if self.headers.get("Content-Length") not in {None, "0"}: return self._error(HTTPStatus.BAD_REQUEST)
                self.send_response(HTTPStatus.OK); self.send_header("Content-Length", "0"); self.end_headers()
            def do_POST(self) -> None:
                if self.path not in {"/v1/call", "/v1/shutdown"}: return self._error(HTTPStatus.NOT_FOUND)
                if not parent._authorized(self.headers.get("Authorization")): return self._error(HTTPStatus.UNAUTHORIZED)
                content_type = self.headers.get("Content-Type", "").split(";", 1)[0].lower()
                if content_type != "application/json": return self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
                try: length = int(self.headers.get("Content-Length", ""))
                except ValueError: return self._error(HTTPStatus.LENGTH_REQUIRED)
                if length < 0 or length > MAX_PAYLOAD_BYTES: return self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                payload = self.rfile.read(length)
                if self.path == "/v1/shutdown":
                    if payload not in {b"", b"{}"}: return self._error(HTTPStatus.BAD_REQUEST)
                    self._json(HTTPStatus.OK, {"ok": True})
                    Thread(target=parent.close, daemon=True).start(); return
                try: call = parse_call(payload)
                except ControlProtocolError: return self._error(HTTPStatus.BAD_REQUEST)
                if not parent._remember(call.request_id): return self._error(HTTPStatus.CONFLICT)
                response = parent.service.call(call.method, call.arguments, caller_id=call.caller_id)
                self._json(HTTPStatus.OK, to_wire(response))
            def _json(self, status: HTTPStatus, payload: object) -> None:
                data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                self.send_response(status); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
            def _error(self, status: HTTPStatus) -> None:
                self.send_response(status); self.send_header("Content-Length", "0"); self.end_headers()
        return Handler

    def _authorized(self, header: str | None) -> bool:
        return isinstance(header, str) and hmac.compare_digest(header, "Bearer " + self.token)
    def _remember(self, request_id: str) -> bool:
        with self._seen_lock:
            if request_id in self._seen_set:
                return False
            if len(self._seen) == self._seen.maxlen:
                self._seen_set.remove(self._seen[0])
            self._seen.append(request_id)
            self._seen_set.add(request_id)
            return True
