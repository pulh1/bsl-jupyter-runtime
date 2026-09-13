from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import keyword
import os
from pathlib import Path
from queue import Empty, Queue
import subprocess
import sys
from threading import Lock, Thread
from typing import cast
from uuid import uuid4

import psutil

from onec_runtime_mcp.agent.contracts import to_wire
from onec_runtime_mcp.agent.proxies import (
    ProxyDescriptor,
    ProxyProvenance,
    ProxyRealm,
    ProxyRegistry,
    ValuePreview,
    ValueBudget,
)
from onec_runtime_mcp.agent.python_protocol import (
    PythonImportDescriptor,
    PythonInspection,
    PythonRunResult,
    PythonWorkspaceLimits,
    PythonWorkspaceStatus,
)
from onec_runtime.errors import CommandTimeout, ProtocolError, StaleProxy


class PythonBindingConflict(ValueError):
    """A named Python binding no longer matches the caller's version fence."""


class PythonWorkspace:
    """Own a persistent child Python namespace through a bounded JSONL protocol."""

    def __init__(
        self,
        project_root: Path,
        state_root: Path,
        limits: PythonWorkspaceLimits,
        registry: ProxyRegistry,
        generation: int,
        process: subprocess.Popen[bytes],
    ) -> None:
        self.project_root = project_root
        self.state_root = state_root
        self.limits = limits
        self.registry = registry
        self.generation = generation
        self._process = process
        self.worker_pid = process.pid
        launcher = psutil.Process(process.pid)
        self._launcher_identity = (
            launcher.pid,
            launcher.create_time(),
            launcher.exe(),
        )
        self._worker_identity: tuple[int, float, str] | None = None
        self._responses: Queue[bytes | None] = Queue()
        self._request_lock = Lock()
        self._request_sequence = 0
        self._variables: dict[str, ProxyDescriptor] = {}
        self._public_events: list[dict[str, object]] = []
        self._closed = False
        self._lost = False
        self._stdout_thread = Thread(target=self._read_stdout, daemon=True)
        self._stderr_thread = Thread(target=self._drain_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

    @classmethod
    def start(
        cls,
        project_root: str | Path,
        state_root: str | Path,
        limits: PythonWorkspaceLimits,
        *,
        registry: ProxyRegistry | None = None,
    ) -> "PythonWorkspace":
        project = Path(project_root).resolve(strict=True)
        state = Path(state_root).resolve()
        if not state.is_relative_to(project):
            raise ValueError("state_root must be confined to project_root")
        if not isinstance(limits, PythonWorkspaceLimits):
            raise TypeError("limits must be PythonWorkspaceLimits")
        state.mkdir(parents=True, exist_ok=True)
        transfer_root = state / "python-transfers"
        transfer_root.mkdir(parents=True, exist_ok=True)
        generation = cls._next_generation(state)
        environment = os.environ.copy()
        source_root = str(Path(__file__).resolve().parents[2])
        current_path = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = os.pathsep.join(
            item for item in (source_root, current_path) if item
        )
        environment["ONEC_PYTHON_MAX_REQUEST_BYTES"] = str(limits.max_request_bytes)
        environment["ONEC_PYTHON_MAX_RESPONSE_BYTES"] = str(limits.max_response_bytes)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(
            [sys.executable, "-u", "-m", "onec_runtime_mcp.agent.python_worker"],
            cwd=project,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creationflags,
        )
        workspace = cls(
            project,
            state,
            limits,
            registry or ProxyRegistry(),
            generation,
            process,
        )
        try:
            response = workspace._request(
                "initialize",
                {
                    "allowed_imports": list(limits.allowed_imports),
                    "max_stdout_bytes": limits.max_stdout_bytes,
                    "max_stderr_bytes": limits.max_stderr_bytes,
                    "max_variables": limits.max_variables,
                    "private_log": str(state / "python-worker-private.log"),
                    "transfer_root": str(transfer_root),
                },
            )
            worker_pid = response.get("pid")
            if response.get("ok") is not True or type(worker_pid) is not int:
                raise ProtocolError("Python worker initialization response is invalid")
            workspace._admit_worker_identity(worker_pid)
        except BaseException:
            workspace.close()
            raise
        return workspace

    def status(self) -> PythonWorkspaceStatus:
        response = self._request("status", {})
        if (
            response.get("ok") is not True
            or response.get("state") != "ready"
            or type(response.get("variable_count")) is not int
        ):
            raise ProtocolError("Python worker response is invalid")
        return PythonWorkspaceStatus(
            generation=self.generation,
            pid=self.worker_pid,
            state="ready",
            variable_count=cast(int, response["variable_count"]),
            hard_memory_limit=False,
        )

    def variables(self) -> tuple[ProxyDescriptor, ...]:
        self._require_live()
        return tuple(self._variables.values())

    def run(
        self,
        code: str,
        *,
        inputs: Mapping[str, object],
        outputs: Sequence[str],
        cell_id: str = "python-inline",
        revision: int = 1,
        source_sha256: str | None = None,
    ) -> PythonRunResult:
        self._require_live()
        if not isinstance(code, str) or len(code.encode("utf-8")) > self.limits.max_code_bytes:
            raise ValueError("Python code must be a bounded string")
        if not isinstance(inputs, Mapping):
            raise TypeError("inputs must be a mapping")
        output_names = self._names(outputs, name="output", allow_private=False)
        if len(set(output_names)) != len(output_names):
            raise ValueError("output names must be unique")
        if len(output_names) > self.limits.max_variables:
            raise ValueError("output count exceeds workspace limit")
        encoded_inputs: dict[str, object] = {}
        for name, value in inputs.items():
            self._name(name, name="input", allow_private=False)
            if isinstance(value, ProxyDescriptor):
                descriptor = self.registry.resolve(value.proxy_id)
                if descriptor.realm is not ProxyRealm.PYTHON:
                    raise ValueError("Python workspace inputs require Python proxies")
                handle = self.registry.resolver_handle(descriptor.proxy_id)
                if not isinstance(handle, str):
                    raise StaleProxy("Python proxy has no worker object")
                encoded_inputs[name] = {"object_handle": handle}
            else:
                encoded_inputs[name] = {"literal": to_wire(value)}
        if not isinstance(cell_id, str) or not cell_id:
            raise ValueError("cell_id must be non-empty")
        if type(revision) is not int or revision <= 0:
            raise ValueError("revision must be positive")
        operation_id = f"py_{uuid4().hex}"
        observed_source_sha256 = hashlib.sha256(code.encode("utf-8")).hexdigest()
        if source_sha256 is not None and source_sha256 != observed_source_sha256:
            raise ValueError("source_sha256 does not match Python code")
        source_sha256 = observed_source_sha256
        response = self._request(
            "run",
            {
                "code": code,
                "inputs": encoded_inputs,
                "outputs": list(output_names),
            },
        )
        stdout, stderr = self._bounded_streams(response)
        if response.get("ok") is not True:
            category = response.get("error_category")
            diagnostic_id = response.get("diagnostic_id")
            if not isinstance(category, str) or not isinstance(diagnostic_id, str):
                raise ProtocolError("Python worker response is invalid")
            self._record_public("run", False, (), category)
            return PythonRunResult(
                succeeded=False,
                outputs={},
                stdout=stdout,
                stderr=stderr,
                stdout_truncated=response.get("stdout_truncated") is True,
                stderr_truncated=response.get("stderr_truncated") is True,
                error_category=category,
                diagnostic_id=diagnostic_id,
            )
        raw_outputs = response.get("outputs")
        if not isinstance(raw_outputs, dict) or set(raw_outputs) != set(output_names):
            raise ProtocolError("Python worker response is invalid")
        staged: list[tuple[str, str, str, ValuePreview | None]] = []
        for name in output_names:
            metadata = raw_outputs[name]
            if not isinstance(metadata, dict):
                raise ProtocolError("Python worker response is invalid")
            handle = metadata.get("object_handle")
            type_name = metadata.get("type_name")
            if not isinstance(handle, str) or not isinstance(type_name, str) or not type_name:
                raise ProtocolError("Python worker response is invalid")
            staged.append((name, handle, type_name, self._preview(type_name, metadata.get("preview"))))
        published: dict[str, ProxyDescriptor] = {}
        parent_ids = tuple(
            value.proxy_id for value in inputs.values() if isinstance(value, ProxyDescriptor)
        )
        try:
            for name, handle, type_name, preview in staged:
                descriptor = self.registry.register_python(
                    qualified_name=f"python.{name}",
                    type_name=type_name,
                    python_generation=self.generation,
                    provenance=ProxyProvenance(
                        cell_id,
                        revision,
                        source_sha256,
                        operation_id,
                        parent_proxy_ids=parent_ids,
                    ),
                    resolver_handle=handle,
                    capabilities=("describe", "size", "preview", "get", "select", "inspect"),
                    bounded_preview=preview,
                )
                self._variables[name] = descriptor
                published[name] = descriptor
        except BaseException:
            self._mark_lost()
            raise
        self._record_public(
            "run",
            True,
            tuple((name, item.type_name) for name, item in published.items()),
            "",
        )
        return PythonRunResult(
            succeeded=True,
            outputs=published,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=response.get("stdout_truncated") is True,
            stderr_truncated=response.get("stderr_truncated") is True,
        )

    def inspect(self, proxy_id: str) -> PythonInspection:
        descriptor = self.registry.resolve(proxy_id)
        if descriptor.realm is not ProxyRealm.PYTHON:
            raise ValueError("proxy is not a Python value")
        handle = self.registry.resolver_handle(proxy_id)
        if not isinstance(handle, str):
            raise StaleProxy("Python proxy has no worker object")
        response = self._request("inspect", {"object_handle": handle})
        if response.get("ok") is not True:
            raise StaleProxy("Python worker object is unavailable")
        type_name = response.get("type_name")
        if not isinstance(type_name, str) or not type_name:
            raise ProtocolError("Python worker response is invalid")
        preview = response.get("preview")
        if preview is not None and type(preview) not in {bool, int, float, str}:
            raise ProtocolError("Python worker response is invalid")
        shape = response.get("shape")
        if shape is not None and (
            not isinstance(shape, list) or any(type(item) is not int for item in shape)
        ):
            raise ProtocolError("Python worker response is invalid")
        dtype = response.get("dtype")
        memory_bytes = response.get("memory_bytes")
        return PythonInspection(
            proxy_id=descriptor.proxy_id,
            type_name=type_name,
            preview=preview,
            shape=None if shape is None else tuple(shape),
            dtype=dtype if isinstance(dtype, str) else None,
            memory_bytes=memory_bytes if type(memory_bytes) is int else None,
        )

    def ingest_typed_payload(
        self,
        payload: bytes,
        *,
        kind: str,
        options: Mapping[str, object],
        budget: ValueBudget,
        provenance: ProxyProvenance,
    ) -> ProxyDescriptor:
        self._require_live()
        if not isinstance(payload, bytes) or not payload or len(payload) > budget.max_bytes:
            raise ValueError("typed payload must fit the explicit byte budget")
        if kind not in {"value", "compact_table"}:
            raise ValueError("typed payload kind is invalid")
        if not isinstance(options, Mapping) or any(type(key) is not str for key in options):
            raise ValueError("typed payload options must be a string-keyed mapping")
        transfer_root = self.state_root / "python-transfers"
        transfer_path = transfer_root / f"{uuid4().hex}.payload"
        with transfer_path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        response: dict[str, object]
        try:
            response = self._request(
                "ingest",
                {
                    "path": str(transfer_path),
                    "kind": kind,
                    "byte_count": len(payload),
                    "payload_sha256": hashlib.sha256(payload).hexdigest(),
                    "max_bytes": budget.max_bytes,
                    "options": to_wire(options),
                },
                timeout=budget.timeout_seconds,
            )
        finally:
            transfer_path.unlink(missing_ok=True)
        if response.get("ok") is not True:
            category = response.get("error_category")
            if category == "limit":
                raise ValueError("typed payload exceeds materialization budget")
            raise ProtocolError("Python worker rejected typed payload")
        internal_handle = response.get("object_handle")
        type_name = response.get("type_name")
        if not isinstance(internal_handle, str) or not isinstance(type_name, str):
            raise ProtocolError("Python worker response is invalid")
        preview = self._preview(type_name, response.get("preview"))
        try:
            return self.registry.register_python(
                qualified_name=f"python.snapshot.{uuid4().hex}",
                type_name=type_name,
                python_generation=self.generation,
                provenance=provenance,
                resolver_handle=internal_handle,
                capabilities=("describe", "size", "preview", "get", "select", "inspect"),
                bounded_preview=preview,
            )
        except BaseException:
            try:
                self._request("release_object", {"object_handle": internal_handle})
            except BaseException:
                self._mark_lost()
            raise

    def imports(self) -> tuple[PythonImportDescriptor, ...]:
        response = self._request("imports", {})
        values = response.get("imports")
        if response.get("ok") is not True or not isinstance(values, list):
            raise ProtocolError("Python worker response is invalid")
        result: list[PythonImportDescriptor] = []
        for item in values:
            if not isinstance(item, dict):
                raise ProtocolError("Python worker response is invalid")
            result.append(PythonImportDescriptor(item.get("name"), item.get("version")))  # type: ignore[arg-type]
        return tuple(result)

    def release(self, proxy_id: str) -> bool:
        if self.registry.is_exact_snapshot(proxy_id):
            return self.registry.release(proxy_id)
        descriptor, handle, should_release = self.registry.release_target(proxy_id)
        if not should_release:
            return False
        if descriptor.realm is not ProxyRealm.PYTHON:
            raise ValueError("proxy is not a Python value")
        if not isinstance(handle, str):
            raise StaleProxy("Python proxy has no worker object")
        if descriptor.qualified_name.casefold().startswith("python.") and not descriptor.qualified_name.casefold().startswith("python.snapshot."):
            replacement = self.registry.register_python(
                qualified_name=descriptor.qualified_name,
                type_name=descriptor.type_name,
                python_generation=self.generation,
                provenance=descriptor.provenance,
                consistency=descriptor.consistency,
                resolver_handle=handle,
                capabilities=descriptor.capabilities,
                known_size=descriptor.known_size,
                bounded_preview=descriptor.bounded_preview,
            )
            raw_name = descriptor.qualified_name.split(".", 1)[1]
            self._variables[raw_name] = replacement
            released = self.registry.release(descriptor.proxy_id)
            if proxy_id != descriptor.proxy_id:
                self.registry.release(proxy_id)
            return released
        response = self._request("release_object", {"object_handle": handle})
        if response.get("ok") is not True:
            raise ProtocolError("Python worker could not release object")
        released = self.registry.release(descriptor.proxy_id)
        if proxy_id != descriptor.proxy_id:
            self.registry.release(proxy_id)
        return released

    def delete_bindings(
        self, expected_versions: Mapping[str, int]
    ) -> tuple[str, ...]:
        if not isinstance(expected_versions, Mapping) or not expected_versions:
            raise ValueError("expected_versions must be a non-empty mapping")
        staged: list[tuple[str, str, ProxyDescriptor]] = []
        for qualified_name, expected_version in expected_versions.items():
            if (
                not isinstance(qualified_name, str)
                or not qualified_name.casefold().startswith("python.")
                or type(expected_version) is not int
                or expected_version <= 0
            ):
                raise ValueError("Python binding fence is invalid")
            descriptor = self.registry.resolve_name(qualified_name)
            if descriptor.realm is not ProxyRealm.PYTHON:
                raise ValueError("only Python bindings can be deleted")
            if descriptor.version != expected_version:
                raise PythonBindingConflict("Python binding version conflict")
            raw_name = descriptor.qualified_name.split(".", 1)[1]
            staged.append((qualified_name, raw_name, descriptor))
        response = self._request(
            "delete_bindings", {"names": [raw_name for _, raw_name, _ in staged]}
        )
        if response.get("ok") is not True:
            raise ProtocolError("Python worker could not delete bindings")
        for _qualified_name, raw_name, descriptor in staged:
            self.registry.release(descriptor.proxy_id)
            self._variables.pop(raw_name, None)
        return tuple(qualified_name for qualified_name, _, _ in staged)

    def reset(self) -> PythonWorkspaceStatus:
        response = self._request("reset", {})
        if response.get("ok") is not True:
            raise ProtocolError("Python worker response is invalid")
        self.registry.invalidate_python_generation(self.generation)
        self.generation += 1
        try:
            self._write_generation(self.state_root, self.generation)
        except OSError as error:
            self._mark_lost()
            raise ProtocolError("Python generation state could not be persisted") from error
        self._variables.clear()
        return self.status()

    def public_transcript_bytes(self) -> bytes:
        return b"".join(
            json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            + b"\n"
            for event in self._public_events
        )

    def close(self) -> None:
        if self._closed:
            return
        try:
            if self._process.poll() is None:
                try:
                    self._request("close", {}, timeout=min(self.limits.timeout_seconds, 1.0))
                except BaseException:
                    pass
        finally:
            self.registry.invalidate_python_generation(self.generation)
            self._terminate_owned_process()
            self._variables.clear()
            self._closed = True

    def _request(
        self,
        method: str,
        arguments: Mapping[str, object],
        *,
        timeout: float | None = None,
    ) -> dict[str, object]:
        self._require_live()
        with self._request_lock:
            self._request_sequence += 1
            request = {"request_id": self._request_sequence, "method": method, **arguments}
            encoded = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(encoded) > self.limits.max_request_bytes:
                raise ValueError("Python worker request exceeds byte limit")
            stdin = self._process.stdin
            if stdin is None:
                self._mark_lost()
                raise ProtocolError("Python worker stdin is unavailable")
            try:
                stdin.write(encoded + b"\n")
                stdin.flush()
            except (BrokenPipeError, OSError) as error:
                self._mark_lost()
                raise ProtocolError("Python worker process is lost") from error
            try:
                line = self._responses.get(timeout=timeout or self.limits.timeout_seconds)
            except Empty as error:
                self._mark_lost()
                raise CommandTimeout("Python worker command timed out") from error
            if line is None:
                self._mark_lost()
                raise ProtocolError("Python worker process is lost")
            if len(line) > self.limits.max_response_bytes + 1:
                self._mark_lost()
                raise ProtocolError("Python worker response exceeds byte limit")
            try:
                response = json.loads(line)
            except json.JSONDecodeError as error:
                self._mark_lost()
                raise ProtocolError("Python worker response is malformed") from error
            if not isinstance(response, dict):
                raise ProtocolError("Python worker response is invalid")
            return response

    def _read_stdout(self) -> None:
        stdout = self._process.stdout
        if stdout is None:
            self._responses.put(None)
            return
        while True:
            line = stdout.readline(self.limits.max_response_bytes + 2)
            if not line:
                self._responses.put(None)
                return
            self._responses.put(line)

    def _drain_stderr(self) -> None:
        stderr = self._process.stderr
        if stderr is None:
            return
        log = self.state_root / "python-worker-launch.log"
        while True:
            block = stderr.read(4096)
            if not block:
                return
            try:
                with log.open("ab") as handle:
                    handle.write(block)
            except OSError:
                return

    def _mark_lost(self) -> None:
        if not self._lost:
            self._lost = True
            self.registry.invalidate_python_generation(self.generation)
            self._variables.clear()
        self._terminate_owned_process()

    def _terminate_owned_process(self) -> None:
        if self._worker_identity is not None:
            self._terminate_identity(self._worker_identity)
        self._terminate_identity(self._launcher_identity)
        try:
            self._process.wait(timeout=0.2)
        except (subprocess.TimeoutExpired, OSError):
            pass
        self._stdout_thread.join(timeout=0.5)
        self._stderr_thread.join(timeout=0.5)
        for stream in (self._process.stdin, self._process.stdout, self._process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    @staticmethod
    def _terminate_identity(identity: tuple[int, float, str]) -> None:
        try:
            current = psutil.Process(identity[0])
            if (current.create_time(), current.exe()) != identity[1:]:
                return
            current.terminate()
            try:
                current.wait(timeout=2.0)
            except psutil.TimeoutExpired:
                current.kill()
                current.wait(timeout=2.0)
        except psutil.NoSuchProcess:
            pass

    def _admit_worker_identity(self, worker_pid: int) -> None:
        worker = psutil.Process(worker_pid)
        launcher_pid = self._launcher_identity[0]
        if worker_pid != launcher_pid:
            try:
                launcher = psutil.Process(launcher_pid)
                if (launcher.create_time(), launcher.exe()) != self._launcher_identity[1:]:
                    raise ProtocolError("Python worker launcher identity changed")
                descendants = {
                    child.pid
                    for child in launcher.children(recursive=True)
                }
            except psutil.NoSuchProcess as error:
                raise ProtocolError("Python worker launcher disappeared") from error
            if worker_pid not in descendants:
                raise ProtocolError("Python worker PID is not owned by launcher")
        self.worker_pid = worker_pid
        self._worker_identity = (worker.pid, worker.create_time(), worker.exe())

    def _require_live(self) -> None:
        if self._closed:
            raise ProtocolError("Python workspace is closed")
        if self._lost or self._process.poll() is not None:
            self._mark_lost()
            raise ProtocolError("Python worker process is lost")

    def _bounded_streams(self, response: Mapping[str, object]) -> tuple[str, str]:
        stdout, stderr = response.get("stdout", ""), response.get("stderr", "")
        if not isinstance(stdout, str) or not isinstance(stderr, str):
            raise ProtocolError("Python worker response is invalid")
        if (
            len(stdout.encode("utf-8")) > self.limits.max_stdout_bytes
            or len(stderr.encode("utf-8")) > self.limits.max_stderr_bytes
        ):
            raise ProtocolError("Python worker stream exceeds byte limit")
        return stdout, stderr

    @staticmethod
    def _preview(type_name: str, value: object) -> ValuePreview | None:
        if value is None:
            return None
        if type(value) not in {bool, int, float, str}:
            raise ProtocolError("Python worker preview is invalid")
        return ValuePreview(type_name=type_name, scalar=value)

    @staticmethod
    def _name(value: object, *, name: str, allow_private: bool) -> str:
        if (
            not isinstance(value, str)
            or not value.isidentifier()
            or keyword.iskeyword(value)
            or (not allow_private and value.startswith("_"))
        ):
            raise ValueError(f"{name} name is invalid")
        return value

    @classmethod
    def _names(
        cls,
        values: Sequence[str],
        *,
        name: str,
        allow_private: bool,
    ) -> tuple[str, ...]:
        if isinstance(values, str) or not isinstance(values, (tuple, list)):
            raise TypeError(f"{name} names must be a sequence")
        return tuple(cls._name(value, name=name, allow_private=allow_private) for value in values)

    def _record_public(
        self,
        method: str,
        ok: bool,
        outputs: tuple[tuple[str, str], ...],
        category: str,
    ) -> None:
        self._public_events.append(
            {
                "method": method,
                "ok": ok,
                "outputs": [
                    {"name": name, "type_name": type_name}
                    for name, type_name in outputs
                ],
                "error_category": category,
            }
        )

    @classmethod
    def _next_generation(cls, state_root: Path) -> int:
        path = state_root / "python-generation.json"
        previous = 0
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if type(payload.get("generation")) is int and payload["generation"] > 0:
                    previous = payload["generation"]
            except (AttributeError, json.JSONDecodeError, OSError):
                previous = 0
        generation = previous + 1
        cls._write_generation(state_root, generation)
        return generation

    @staticmethod
    def _write_generation(state_root: Path, generation: int) -> None:
        path = state_root / "python-generation.json"
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump({"generation": generation}, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
