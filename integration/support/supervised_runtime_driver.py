from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from typing import Any

from onec_runtime.artifacts import ExistingArtifactSink, utc_now
from onec_runtime.bootstrap import (
    enable_server_kernel_loop,
    wait_for_managed_startup_stop,
    wait_for_server_entry_then_service,
)
from onec_runtime.config import RuntimeConfig
from onec_runtime.controller_worker import PhaseBarrier
from onec_runtime.errors import ProtocolError
from onec_runtime.fault_injection import FaultPoint
from onec_runtime.extension_bundle import packaged_extension_bundle
from onec_runtime.kernel import synthetic_capture_locations
from onec_runtime.prototype_runtime import PrototypeRuntimeController
from onec_runtime.recovery_journal import RecoveryJournal
from onec_runtime.rdbg.session import RdbgSession
from onec_runtime.rdbg.transport import RdbgTransport, TranscriptEntry


OUTER_MAIN = "СинтетическийРезультат = СинтетическийCapture(40);"
DIRTY_ROOTS = ("Скаляр", "Результат")


class OneCRuntimeDriver:
    """Child-owned RDBG driver for one externally supervised generation."""

    def __init__(
        self,
        config: RuntimeConfig,
        generation_id: int,
        debug_port: int,
        run_dir: Path,
        *,
        transport_factory: Callable[..., Any] = RdbgTransport,
        session_factory: Callable[..., Any] = RdbgSession,
        controller_factory: Callable[..., Any] = PrototypeRuntimeController,
    ) -> None:
        self._config = config
        self._generation_id = generation_id
        self._artifacts = ExistingArtifactSink(run_dir)
        breakpoints = packaged_extension_bundle(
            config.runtime_dir
        ).manifest.breakpoints
        self._startup_location = breakpoints.managed
        self._entry_location = breakpoints.server_entry
        self._service_location = breakpoints.server_service
        module_path = (
            (config.workspace / "onec" / "OnecInteractiveRuntime")
            / "CommonModules"
            / "RuntimeKernelServer"
            / "Ext"
            / "Module.bsl"
        )
        self._capture_points = synthetic_capture_locations(module_path)
        self._transport = transport_factory(
            config.debug_host,
            debug_port,
            transcript=self._record_transcript,
        )
        self._session = session_factory(
            self._transport,
            self._startup_location,
            break_on_next=True,
        )
        self._controller_factory = controller_factory
        self._controller: Any | None = None
        self._prepared = False
        self._attached = False
        self._closed = False

    def prepare_debug_ui(self) -> None:
        if self._prepared:
            return
        self._session.initialize()
        self._session.set_service_breakpoint()
        self._prepared = True

    def attach_runtime(self) -> None:
        if not self._prepared:
            raise ProtocolError("DebugUI must be prepared before runtime attach")
        if self._attached:
            return
        wait_for_managed_startup_stop(
            self._session,
            self._startup_location,
            timeout_s=90.0,
        )
        if (
            self._session.target is None
            or self._session.target.target_type != "ManagedClient"
        ):
            raise ProtocolError("Runtime bootstrap did not stop on ManagedClient")

        guard_evidence = enable_server_kernel_loop(self._session)
        entry_stop, service_stop = wait_for_server_entry_then_service(
            self._session,
            self._entry_location,
            self._service_location,
            timeout_s=90.0,
        )
        self._artifacts.write_json(
            "guard-bootstrap.json",
            {
                **guard_evidence.as_dict(),
                "entry_stop": asdict(entry_stop),
                "service_stop": asdict(service_stop),
            },
        )
        self._attached = True

    def execute_phase(self, point: FaultPoint, barrier: PhaseBarrier) -> object:
        if not self._attached:
            raise ProtocolError("Runtime must be attached before phase execution")
        journal = RecoveryJournal(self._operation_sink(point))
        controller = self._controller_factory(
            self._session,
            self._service_location,
            command_timeout_s=90.0,
            runtime_generation=self._generation_id,
            journal=journal,
            fault_hook=barrier,
        )
        self._controller = controller
        controller.execute_main(OUTER_MAIN, capture_points=self._capture_points)
        if point is FaultPoint.AFTER_CAPTURE_CHECKPOINT:
            return None
        if point is FaultPoint.AFTER_FIRST_ROOT_WRITE:
            controller.resume(dirty_roots=DIRTY_ROOTS)
            return None
        if point is FaultPoint.AFTER_CONTINUE_ACK:
            controller.resume()
            return None
        raise ValueError(f"Unsupported runtime fault point: {point!r}")

    def status(self) -> dict[str, object]:
        if self._controller is None:
            return {"state": "idle"}
        state = self._controller.state
        value = state.value if hasattr(state, "value") else state
        return {"state": value if type(value) is str else "unknown"}

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._session.detach()
        finally:
            self._transport.close()

    def _operation_sink(
        self,
        point: FaultPoint,
    ) -> Callable[[str, object], None]:
        def append(original_stream: str, value: object) -> None:
            payload = dict(value) if isinstance(value, dict) else {"value": value}
            payload["original_stream"] = original_stream
            payload["generation"] = self._generation_id
            payload["phase"] = point.value
            self._artifacts.append_jsonl("operation-events.jsonl", payload)

        return append

    def _record_transcript(self, entry: TranscriptEntry) -> None:
        self._artifacts.append_jsonl(
            "rdbg-transcript.jsonl",
            {
                "timestamp": utc_now(),
                "monotonic_ns": entry.monotonic_ns,
                "event": "rdbg_exchange",
                "command": entry.command,
                "status_code": entry.status_code,
                "duration_ms": entry.duration_ms,
                "error_present": bool(entry.error),
                "request_bytes": len(entry.request),
                "request_sha256": sha256(entry.request).hexdigest(),
                "response_bytes": len(entry.response),
                "response_sha256": sha256(entry.response).hexdigest(),
            },
        )
