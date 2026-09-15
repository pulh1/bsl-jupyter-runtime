from __future__ import annotations

import stat
import warnings
from contextlib import contextmanager
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from threading import Event, RLock, Thread, current_thread
from time import sleep
from typing import Iterator, cast
from uuid import UUID, uuid4

import pandas as pd
import psutil

from onec_runtime.artifacts import ArtifactWriter
from onec_runtime.bootstrap import (
    ExtensionHandshakeContract,
    ServerGuardSession,
    enable_server_kernel_loop,
    observe_extension_handshake,
    verify_extension_handshake,
    verify_extension_safe_mode_disabled,
    wait_for_managed_startup_stop,
    wait_for_server_entry_then_service,
)
from onec_runtime.bsl import (
    SourceUnitKind,
    SourceUnitRef,
    mapped_visible_source,
    source_sha256,
)
from onec_runtime.bsl.module_universe import (
    WorkerModuleUnit,
)
from onec_runtime.bsl.module_catalog import SessionCommonModuleCatalog
from onec_runtime.capture_source import (
    CaptureBinding,
    CapturePointRequest,
    CaptureSourceConfig,
    CaptureSourceCatalog,
    SourceVersionRef,
    CommonModuleCaptureResolver,
    ResolvedCapturePoint,
)
from onec_runtime.configuration_source import SourceLayer
from onec_runtime.capture_inspection import CaptureView
from onec_runtime.config import RuntimeConfig
from onec_runtime.configurator_agent import ExtensionAgentEditor, edit_extension
from onec_runtime.errors import (
    CaptureBusyError,
    CaptureSourceNotConfigured,
    CommandTimeout,
    ExtensionHandshakeError,
    ManualExtensionUnavailable,
    ManualExtensionVersionWarning,
    ProcessStartError,
    ProtocolError,
    RdbgDebugUiNotRegistered,
    TargetLost,
    UnexpectedStop,
)
from onec_runtime.extension_bundle import (
    ExtensionBundle,
    ExtensionHandshakeEvidence,
    ExtensionManifest,
    packaged_extension_bundle,
)
from onec_runtime.extension_lifecycle import (
    ExtensionLifecycle,
    LifecycleDecision,
    LifecycleMode,
)
from onec_runtime.extension_state import ExtensionStateStore
from onec_runtime.performance_profile import PhaseRecorder
from onec_runtime.processes import FileModeProcesses
from onec_runtime.prototype_runtime import (
    ContinuationAttemptEvidence,
    ContinuationAttemptSpec,
    OperationState,
    PrototypeRuntimeController,
)
from onec_runtime.rdbg.models import ModuleLocation, TargetId
from onec_runtime.rdbg.session import RdbgSession
from onec_runtime.rdbg.transport import RdbgTransport, TranscriptEntry
from onec_runtime.recovery_journal import RecoveryJournal
from onec_runtime.runtime_api import (
    PrototypeRuntimeApi,
    RuntimeNamespaceSnapshot,
    RuntimeReply,
    RuntimeStatus,
)
from onec_runtime.runtime_contracts import OperationExecutionProvenance
from onec_runtime.server_worker import (
    NotebookWorkerArtifactBuilder,
)
from onec_runtime.startup_diagnostics import startup_log_hint
from onec_runtime.worker_universe import (
    WorkerGenerationHandle,
    WorkerModuleArtifactBuilder,
    WorkerModuleArtifactCache,
)
from onec_runtime.worker_breakpoints import (
    WorkerBreakpointReloadPolicy,
    WorkerBreakpointReloadReport,
    WorkerBreakpointStatus,
)
from onec_runtime.table_materialization import ReferenceMode
from onec_runtime.toolchain import (
    ToolResult,
    dump_target_extension_cfe,
    dump_target_extension_files,
)


class ExtensionMode(StrEnum):
    AUTO = "auto"
    MANUAL = "manual"


@dataclass(frozen=True, slots=True, repr=False)
class RuntimeSessionConfig:
    """Configuration owned by the headless runtime session."""

    runtime: RuntimeConfig
    evidence_root: Path | str | None = None
    chunk_size: int = 2400
    startup_profiler: PhaseRecorder | None = None
    capture_source: CaptureSourceConfig | None = None
    source_root: Path | str | None = None
    extension_mode: ExtensionMode = ExtensionMode.AUTO

    def __post_init__(self) -> None:
        if not isinstance(self.extension_mode, ExtensionMode):
            raise TypeError("extension_mode must be an ExtensionMode")
        evidence_root = self.evidence_root
        if evidence_root is None:
            if not isinstance(self.runtime, RuntimeConfig):
                raise ValueError("evidence_root requires a RuntimeConfig")
            evidence_root = self.runtime.artifacts_dir
        object.__setattr__(self, "evidence_root", Path(evidence_root).resolve())
        if self.source_root is not None:
            supplied_source_root = Path(self.source_root).absolute()
            try:
                # Resolve only after inspecting the original spelling: resolve()
                # erases symlink/junction evidence, including in parent paths.
                for component in (*reversed(supplied_source_root.parents), supplied_source_root):
                    status = component.lstat()
                    if (stat.S_ISLNK(status.st_mode) or
                            getattr(status, "st_file_attributes", 0) &
                            getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
                        raise ProtocolError("common-module source root is unsafe")
                resolved_source_root = supplied_source_root.resolve(strict=True)
            except OSError as error:
                raise ProtocolError(
                    "common-module source root is unsafe"
                ) from error
            object.__setattr__(
                self,
                "source_root",
                resolved_source_root,
            )
        if self.chunk_size <= 0:
            raise ValueError("chunk_size must be positive")


@dataclass(frozen=True, slots=True)
class RuntimeCaptureFence:
    capture_intent_id: str
    operation_id: str
    source_revision: int
    source_sha256: str
    capture_generation: int
    stop_sequence: int


@dataclass(frozen=True, slots=True)
class RuntimeCaptureArming:
    ticket_id: str
    expected_controller_operation_id: int
    expected_stop_sequence: int


@dataclass(frozen=True, slots=True)
class _ActiveCaptureTicket:
    ticket_id: str
    capture_intent_id: str
    operation_id: str
    capture_generation: int
    source_revision: int
    source_sha256: str
    stop_sequence: int


@dataclass(frozen=True, slots=True)
class _AttemptBootstrapEvidence:
    managed_stop: dict[str, object]
    server_entry_stop: dict[str, object]
    server_service_stop: dict[str, object]
    guard: dict[str, object]
    same_session: bool


class _OwnedAttemptFailure(Exception):
    def __init__(
        self,
        primary: BaseException,
        *,
        stage: str,
        repairable: bool,
        cleanup_succeeded: bool,
        cleanup_notes: tuple[str, ...],
    ) -> None:
        super().__init__(type(primary).__name__)
        self.primary = primary
        self.stage = stage
        self.repairable = repairable
        self.cleanup_succeeded = cleanup_succeeded
        self.cleanup_notes = cleanup_notes


_STARTUP_CLEANUP_RETRY_NOTE = (
    "Runtime startup cleanup is incomplete; retry with exception.retry_cleanup()"
)


def _expose_startup_cleanup_retry(
    error: BaseException,
    retry_cleanup: Callable[[], None],
) -> None:
    setattr(error, "retry_cleanup", retry_cleanup)
    notes = getattr(error, "__notes__", ())
    if _STARTUP_CLEANUP_RETRY_NOTE not in notes:
        error.add_note(_STARTUP_CLEANUP_RETRY_NOTE)


class _StartupAttemptCleanup:
    def __init__(
        self,
        runtime: RuntimeConfig,
        processes: FileModeProcesses,
        transport: RdbgTransport | None,
        rdbg: RdbgSession | None,
    ) -> None:
        self._server_mode = runtime.is_server_infobase
        self._processes = processes
        self._transport = transport
        self._rdbg = rdbg
        self._server_session_terminated = not self._server_mode or rdbg is None
        self._native_client_termination_requested = False
        self._processes_closed = False
        self._debug_ui_detached = not self._server_mode or rdbg is None
        self._transport_closed = transport is None
        self.cleanup_error_types: tuple[str, ...] = ()

    def _raise_cleanup_error(self, errors: list[BaseException]) -> None:
        self.cleanup_error_types = tuple(
            type(error).__name__ for error in errors
        )
        if errors:
            raise ProtocolError(
                "Runtime startup cleanup failed: "
                + ", ".join(self.cleanup_error_types)
            ) from None

    def retry_cleanup(self) -> None:
        """Retry incomplete startup cleanup in safe resource order."""
        errors: list[BaseException] = []
        self.cleanup_error_types = ()
        if self._server_mode:
            if not self._server_session_terminated:
                try:
                    assert self._rdbg is not None
                    self._native_client_termination_requested = self._rdbg.terminate_bound_server_session()
                except RdbgDebugUiNotRegistered:
                    # Native termination cannot use a UI the server has lost.
                    # Close only our client, then finish deregistration.
                    self._server_session_terminated = True
                except BaseException as error:
                    errors.append(error)
                else:
                    self._server_session_terminated = True
                self._raise_cleanup_error(errors)
            if not self._processes_closed:
                try:
                    if self._native_client_termination_requested:
                        self._processes.close(graceful_client_timeout_s=3.0)
                    else:
                        self._processes.close()
                except BaseException as error:
                    errors.append(error)
                else:
                    self._processes_closed = True
                self._raise_cleanup_error(errors)
            if not self._debug_ui_detached:
                try:
                    assert self._rdbg is not None
                    self._rdbg.detach()
                except BaseException as error:
                    errors.append(error)
                else:
                    self._debug_ui_detached = True
                self._raise_cleanup_error(errors)
            if not self._transport_closed:
                try:
                    assert self._transport is not None
                    self._transport.close()
                except BaseException as error:
                    errors.append(error)
                else:
                    self._transport_closed = True
                self._raise_cleanup_error(errors)
            return

        if not self._transport_closed:
            try:
                assert self._transport is not None
                self._transport.close()
            except BaseException as error:
                errors.append(error)
            else:
                self._transport_closed = True
        if not self._processes_closed:
            try:
                self._processes.close()
            except BaseException as error:
                errors.append(error)
            else:
                self._processes_closed = True
        self._raise_cleanup_error(errors)


def _start_rdbg_with_registration_retry(
    *,
    transport: RdbgTransport,
    location: ModuleLocation,
    alias: str,
    server_target_type: str,
    break_on_next: bool,
    progress: Callable[[str], None] | None,
    on_candidate: Callable[[RdbgSession], None] | None = None,
    attempts: int = 24,
    retry_interval_s: float = 5.0,
) -> RdbgSession:
    """Register a fresh debugger UI before any owned 1C client is launched."""
    if attempts < 1 or retry_interval_s < 0:
        raise ValueError("RDBG registration retry bounds are invalid")
    for number in range(1, attempts + 1):
        rdbg = RdbgSession(
            transport,
            location,
            alias=alias,
            server_target_type=server_target_type,
            break_on_next=break_on_next,
        )
        if on_candidate is not None:
            on_candidate(rdbg)
        try:
            rdbg.initialize()
            rdbg.set_service_breakpoint()
            rdbg.verify_registration()
        except RdbgDebugUiNotRegistered as error:
            try:
                rdbg.detach()
            except BaseException as cleanup_error:
                error.add_note(
                    "RDBG registration cleanup failed: "
                    f"{type(cleanup_error).__name__}"
                )
                raise
            if number == attempts:
                raise ProtocolError(
                    "RDBG debug UI registration did not stabilize after "
                    f"{attempts} attempts"
                ) from error
            if progress is not None:
                progress(
                    "Повторная регистрация интерфейса отладки 1С "
                    f"({number + 1}/{attempts})"
                )
            sleep(retry_interval_s)
        except BaseException as error:
            try:
                rdbg.detach()
            except BaseException as cleanup_error:
                error.add_note(
                    "RDBG registration cleanup failed: "
                    f"{type(cleanup_error).__name__}"
                )
            raise
        else:
            return rdbg
    raise AssertionError("RDBG registration retry bound was exceeded")


@dataclass(slots=True)
class _OnecInteractiveRuntimeTools:
    runtime: RuntimeConfig
    _editor: ExtensionAgentEditor | None = field(default=None, init=False)

    @contextmanager
    def mutation_session(self, log_dir: Path) -> Iterator[None]:
        if self._editor is not None:
            raise ProtocolError("extension agent session is already active")
        with edit_extension(self.runtime, log_dir) as editor:
            self._editor = editor
            try:
                yield
            finally:
                self._editor = None

    def dump_files(self, destination: Path, log_path: Path) -> ToolResult:
        if self._editor is not None:
            with self._editor.designer_access():
                return dump_target_extension_files(self.runtime, destination, log_path)
        return dump_target_extension_files(self.runtime, destination, log_path)

    def dump_cfe(self, destination: Path, log_path: Path) -> ToolResult:
        if self._editor is not None:
            with self._editor.designer_access():
                return dump_target_extension_cfe(self.runtime, destination, log_path)
        return dump_target_extension_cfe(self.runtime, destination, log_path)

    def load_cfe(self, source: Path, log_path: Path) -> None:
        if self._editor is None:
            raise ProtocolError("extension agent session is not active")
        self._editor.load_cfe(source)

    def apply(self, log_path: Path) -> None:
        if self._editor is None:
            raise ProtocolError("extension agent session is not active")
        self._editor.apply()

    def disable_safe_mode(self, log_dir: Path) -> None:
        if self._editor is None:
            raise ProtocolError("extension agent session is not active")
        self._editor.disable_safe_mode()


class _SessionContinuationAdmission:
    def __init__(
        self,
        session: "RuntimeSession",
        api_admission: object,
        *,
        active_ticket: _ActiveCaptureTicket | None,
        active_points: tuple[object, ...],
        active_locations: tuple[object, ...],
        arming: RuntimeCaptureArming | None,
    ) -> None:
        self._session = session
        self._api_admission = api_admission
        self._active_ticket = active_ticket
        self._active_points = active_points
        self._active_locations = active_locations
        self.arming = arming
        self._closed = False

    def commit(self) -> None:
        if not self._closed:
            getattr(self._api_admission, "commit")()
            self._closed = True

    def rollback(self) -> None:
        if self._closed:
            return
        getattr(self._api_admission, "rollback")()
        with self._session._operation_lock:
            self._session._active_capture_ticket = self._active_ticket
            self._session._active_capture_points = self._active_points
            self._session._active_capture_locations = self._active_locations
        self._closed = True

    def quarantine(self) -> None:
        if self._closed:
            return
        try:
            getattr(self._api_admission, "quarantine")()
        finally:
            with self._session._operation_lock:
                self._session._active_capture_ticket = None
                self._session._active_capture_points = ()
                self._session._active_capture_locations = ()
            self._closed = True


def runtime_bootstrap_locations(
    manifest: ExtensionManifest,
) -> tuple[ModuleLocation, ModuleLocation, ModuleLocation]:
    breakpoints = manifest.breakpoints
    return breakpoints.managed, breakpoints.server_entry, breakpoints.server_service


def _redacted_transcript_sink(
    artifacts: ArtifactWriter,
) -> Callable[[TranscriptEntry], None]:
    def sink(entry: TranscriptEntry) -> None:
        artifacts.append_jsonl(
            "rdbg-summary.jsonl",
            {
                "command": entry.command,
                "status_code": entry.status_code,
                "duration_ms": entry.duration_ms,
                "request_sha256": sha256(entry.request).hexdigest(),
                "response_sha256": sha256(entry.response).hexdigest(),
                "error": bool(entry.error),
            },
        )

    return sink


def _stop_summary(stop: object, *, target_type: str) -> dict[str, object]:
    location = getattr(stop, "location")
    return {
        "target_type": target_type,
        "module_type": location.module_type,
        "line": location.line,
        "extension_name": location.extension_name,
        "reason": getattr(stop, "reason"),
        "stop_by_breakpoint": getattr(stop, "stop_by_breakpoint"),
    }


def _handshake_summary(evidence: ExtensionHandshakeEvidence) -> dict[str, object]:
    location = evidence.location
    return {
        "target_type": evidence.target_type,
        "product_id": evidence.product_id,
        "artifact_version": evidence.artifact_version,
        "protocol_version": evidence.protocol_version,
        "location": {
            "module_type": location.module_type,
            "url": location.url,
            "object_id": str(location.object_id),
            "property_id": str(location.property_id),
            "line": location.line,
            "extension_name": location.extension_name,
            "ext_id": location.ext_id,
        },
    }


class RuntimeSession:
    """Own one headless 1C runtime/debugger pair."""

    def __init__(
        self,
        config: RuntimeSessionConfig,
        processes: FileModeProcesses,
        transport: RdbgTransport,
        rdbg: RdbgSession,
        runtime_api: PrototypeRuntimeApi,
        artifacts: ArtifactWriter,
        *,
        heartbeat_interval_s: float = 15.0,
    ) -> None:
        if heartbeat_interval_s <= 0:
            raise ValueError("heartbeat_interval_s must be positive")
        self.config = config
        self._processes = processes
        self._transport = transport
        self._rdbg = rdbg
        self.runtime_api = runtime_api
        self.artifacts = artifacts
        self._common_module_catalog = (
            None
            if config.source_root is None
            else SessionCommonModuleCatalog(
                config.source_root,
                profile="runtime-session-server-v1",
            )
        )
        self._worker_file_revisions: dict[str, int] = {}
        self._active_worker_file_units: dict[str, SourceUnitRef] = {}
        self._closed = False
        self._close_lock = RLock()
        self._transport_closed = False
        self._processes_closed = False
        self._runtime_api_closed = False
        self._debug_ui_detached = not config.runtime.is_server_infobase
        self._server_session_terminated = not config.runtime.is_server_infobase
        self._native_client_termination_requested = False
        self._operation_lock = RLock()
        self._capture_locations: dict[tuple[str, int], object] = {}
        self._capture_source_catalog: CaptureSourceCatalog | None = None
        self._capture_worker_sources: dict[str, SourceVersionRef] = {}
        self._capture_source_resolver: CommonModuleCaptureResolver | None = None
        self._capture_source_bindings: dict[
            tuple[str, int], CaptureBinding
        ] = {}
        self._active_capture_points: tuple[object, ...] = ()
        self._active_capture_locations: tuple[object, ...] = ()
        self._file_capture_points: tuple[ModuleLocation, ...] = ()
        self._active_capture_ticket: _ActiveCaptureTicket | None = None
        self._capture_resume_listeners: list[Callable[[RuntimeCaptureFence], None]] = []
        self._attempt_bootstrap_evidence: _AttemptBootstrapEvidence | None = None
        self._heartbeat_interval_s = heartbeat_interval_s
        self._heartbeat_stop = Event()
        self._heartbeat_thread = Thread(
            target=self._heartbeat_loop,
            name="onec-runtime-rdbg-heartbeat",
            daemon=True,
        )
        self._heartbeat_thread.start()

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.wait(self._heartbeat_interval_s):
            if not self._operation_lock.acquire(blocking=False):
                continue
            lost_debug_ui = False
            lost_owned_process = False
            try:
                if not self._closed:
                    try:
                        self._rdbg.heartbeat()
                    except RdbgDebugUiNotRegistered:
                        lost_debug_ui = True
                    except Exception:
                        # A transient transport failure must not permanently
                        # disable later keepalives. The next notebook command
                        # remains the authoritative place to surface it.
                        pass
                    ensure_running = getattr(self._processes, "ensure_running", None)
                    if callable(ensure_running):
                        try:
                            ensure_running()
                        except TargetLost:
                            lost_owned_process = True
                        except Exception:
                            # Process inspection can fail transiently too.
                            pass
            finally:
                self._operation_lock.release()
            if lost_debug_ui or lost_owned_process:
                try:
                    self.close()
                except BaseException:
                    # The owner retains incomplete cleanup for an explicit retry.
                    warnings.warn(
                        "1C runtime lost its debug UI or owned process; "
                        "cleanup is incomplete; "
                        "retry runtime.close()",
                        RuntimeWarning,
                        stacklevel=2,
                    )
                return

    @classmethod
    def start(
        cls,
        config: RuntimeSessionConfig,
        *,
        progress: Callable[[str], None] | None = None,
    ) -> "RuntimeSession":
        runtime = config.runtime
        artifacts = ArtifactWriter(config.evidence_root, "runtime-session")
        profiler = config.startup_profiler or PhaseRecorder()
        attempt_failures: list[dict[str, object]] = []
        primary_error: BaseException | None = None
        completed_session: RuntimeSession | None = None
        try:
            if progress is not None:
                progress("Загрузка расширения 1С")
            bundle = profiler.measure(
                "extension.bundle",
                lambda: packaged_extension_bundle(runtime.runtime_dir),
            )
            state_store = ExtensionStateStore(
                runtime.runtime_dir / "extension-state",
                runtime.infobase_identity,
            )
            lifecycle = ExtensionLifecycle(
                runtime,
                bundle,
                state_store,
                _OnecInteractiveRuntimeTools(runtime),
                profiler=profiler,
            )
            manual_mode = config.extension_mode is ExtensionMode.MANUAL
            if progress is not None:
                progress("Проверка состояния расширения 1С")
            decision = lifecycle.prepare_manual() if manual_mode else lifecycle.prepare()
            # An RDBG UI can vanish after the pre-launch probe while the client
            # starts. Keep one fresh full attempt separate from extension repair.
            attempt_limit = (1 if manual_mode else 2) + 1
            registration_retry_available = True
            extension_repair_available = not manual_mode

            for attempt_number in range(attempt_limit):
                try:
                    runtime_session, evidence = cls._start_attempt(
                        config,
                        artifacts,
                        bundle,
                        lifecycle,
                        profiler,
                        verify_safe_mode=decision.mode in {
                            LifecycleMode.FAST, LifecycleMode.PROBED,
                        },
                        progress=progress,
                    )
                except _OwnedAttemptFailure as failure:
                    attempt_failures.append(
                        {
                            "admitted": False,
                            "attempt": attempt_number + 1,
                            "cleanup_succeeded": failure.cleanup_succeeded,
                            "decision_mode": decision.mode.value,
                            "error_type": type(failure.primary).__name__,
                            "repairable": failure.repairable,
                            "stage": failure.stage,
                        }
                    )
                    manual_unavailable = (
                        manual_mode
                        and failure.stage in {"managed-bootstrap", "server-entry-wait"}
                        and isinstance(failure.primary, (CommandTimeout, UnexpectedStop))
                    )
                    if manual_unavailable:
                        unavailable = ManualExtensionUnavailable(
                            "The user-managed runtime extension is unavailable; "
                            "install and apply the CFE from the current release "
                            "and preserve its bootstrap ABI"
                        )
                        for note in failure.cleanup_notes:
                            unavailable.add_note(note)
                        retry_cleanup = getattr(
                            failure.primary, "retry_cleanup", None
                        )
                        if callable(retry_cleanup):
                            _expose_startup_cleanup_retry(
                                unavailable, retry_cleanup
                            )
                        raise unavailable from None
                    if (
                        registration_retry_available
                        and failure.cleanup_succeeded
                        and isinstance(failure.primary, RdbgDebugUiNotRegistered)
                    ):
                        registration_retry_available = False
                        if progress is not None:
                            progress("Повторный запуск после потери интерфейса отладки 1С")
                        continue
                    may_repair = (
                        extension_repair_available
                        and not manual_mode
                        and decision.mode in {LifecycleMode.FAST, LifecycleMode.PROBED}
                        and decision.retry_allowed
                        and failure.repairable
                        and failure.cleanup_succeeded
                    )
                    if not may_repair:
                        raise failure.primary.with_traceback(
                            failure.primary.__traceback__
                        ) from None
                    extension_repair_available = False

                    def repair() -> LifecycleDecision:
                        lifecycle.invalidate_marker()
                        if progress is not None:
                            progress("Повторная подготовка расширения через Конфигуратор 1С")
                        return lifecycle.prepare(force_slow=True)

                    try:
                        decision = profiler.measure("extension.retry", repair)
                    except BaseException as repair_error:  # noqa: BLE001
                        failure.primary.add_note(
                            "Runtime startup repair failed: "
                            f"{type(repair_error).__name__}"
                        )
                        retry_cleanup = getattr(repair_error, "retry_cleanup", None)
                        if callable(retry_cleanup):
                            _expose_startup_cleanup_retry(failure.primary, retry_cleanup)
                        raise failure.primary.with_traceback(
                            failure.primary.__traceback__
                        ) from None
                    continue

                try:
                    manifest = bundle.manifest
                    manual_extension: dict[str, object] | None = None
                    if manual_mode:
                        observed_version = lifecycle.accept_manual_handshake(
                            (evidence[0], evidence[1])
                        )
                        version_matches = observed_version == manifest.artifact_version
                        if not version_matches:
                            warnings.warn(
                                "User-managed runtime extension artifact version "
                                f"{observed_version!r} differs from packaged release "
                                f"{manifest.artifact_version!r}; continuing in manual mode",
                                ManualExtensionVersionWarning,
                                stacklevel=2,
                            )
                        manual_extension = {
                            "packaged_artifact_version": manifest.artifact_version,
                            "observed_artifact_version": observed_version,
                            "version_matches": version_matches,
                            "protocol_version": manifest.protocol_version,
                        }
                    else:
                        lifecycle.commit_handshake((evidence[0], evidence[1]))
                    details = runtime_session._attempt_bootstrap_evidence
                    if details is None:
                        raise ProtocolError("Runtime bootstrap evidence is unavailable")
                    bootstrap_payload: dict[str, object] = {
                        "status": "PASS",
                        "lifecycle": {
                            "mode": decision.mode.value,
                            "target_state": decision.target_state.value,
                        },
                        "bundle": {
                            "source": "packaged-release",
                            "artifact_sha256": manifest.fingerprints.artifact_sha256,
                            "cfe_sha256": manifest.cfe_sha256,
                            "identity_sha256": manifest.fingerprints.identity_sha256,
                        },
                        "attempt_failures": attempt_failures,
                        "handshakes": [
                            _handshake_summary(item) for item in evidence
                        ],
                        "managed_stop": details.managed_stop,
                        "server_entry_stop": details.server_entry_stop,
                        "server_service_stop": details.server_service_stop,
                        "guard": details.guard,
                        "same_session": details.same_session,
                    }
                    if manual_extension is not None:
                        bootstrap_payload["manual_extension"] = manual_extension
                    artifacts.write_json("bootstrap.json", bootstrap_payload)
                except BaseException as error:
                    try:
                        runtime_session.close()
                    except BaseException as cleanup_error:
                        error.add_note(
                            "Runtime startup cleanup failed: "
                            f"{type(cleanup_error).__name__}"
                        )
                        _expose_startup_cleanup_retry(
                            error, runtime_session.close
                        )
                    raise
                if config.capture_source is not None:
                    try:
                        source_options = {}
                        if config.capture_source.layer != SourceLayer.AUTO:
                            source_options["layer"] = config.capture_source.layer
                        if config.capture_source.extension_name is not None:
                            source_options["extension_name"] = config.capture_source.extension_name
                        runtime_session.configure_capture_source(
                            config.capture_source.project,
                            config.capture_source.source_root,
                            **source_options,
                        )
                    except BaseException as error:
                        try:
                            runtime_session.close()
                        except BaseException as cleanup_error:  # noqa: BLE001
                            error.add_note(
                                "Runtime startup cleanup failed: "
                                f"{type(cleanup_error).__name__}"
                            )
                            _expose_startup_cleanup_retry(
                                error, runtime_session.close
                            )
                        raise
                if progress is not None:
                    try:
                        progress("Сеанс 1С готов")
                    except BaseException as error:
                        try:
                            runtime_session.close()
                        except BaseException as cleanup_error:
                            error.add_note(
                                "Runtime startup cleanup failed: "
                                f"{type(cleanup_error).__name__}"
                            )
                            _expose_startup_cleanup_retry(
                                error, runtime_session.close
                            )
                        raise
                completed_session = runtime_session
                return runtime_session

            raise AssertionError("runtime startup retry bound was exceeded")
        except BaseException as error:
            primary_error = error
            raise
        finally:
            try:
                artifacts.write_json(
                    "bootstrap-phases.json",
                    [event.as_dict() for event in profiler.events],
                )
                artifacts.write_json("bootstrap-attempts.json", attempt_failures)
            except BaseException as evidence_error:
                if primary_error is not None:
                    primary_error.add_note(
                        "Runtime startup phase evidence write failed: "
                        f"{type(evidence_error).__name__}"
                    )
                else:
                    public_error = ProtocolError(
                        "Runtime startup phase evidence could not be written"
                    )
                    if completed_session is not None:
                        try:
                            completed_session.close()
                        except BaseException as cleanup_error:
                            public_error.add_note(
                                "Runtime startup cleanup failed: "
                                f"{type(cleanup_error).__name__}"
                            )
                            _expose_startup_cleanup_retry(
                                public_error, completed_session.close
                            )
                    raise public_error from None

    @classmethod
    def _start_attempt(
        cls,
        config: RuntimeSessionConfig,
        artifacts: ArtifactWriter,
        bundle: ExtensionBundle,
        lifecycle: ExtensionLifecycle,
        profiler: PhaseRecorder,
        *,
        verify_safe_mode: bool = False,
        progress: Callable[[str], None] | None = None,
    ) -> tuple["RuntimeSession", tuple[ExtensionHandshakeEvidence, ...]]:
        del lifecycle, profiler
        runtime = config.runtime
        processes = FileModeProcesses(runtime)
        transport: RdbgTransport | None = None
        rdbg: RdbgSession | None = None
        registration_candidates: list[RdbgSession] = []
        runtime_session: RuntimeSession | None = None
        stage = "process-start"
        try:
            managed_location, entry_location, service_location = (
                runtime_bootstrap_locations(bundle.manifest)
            )
            if progress is not None:
                progress("Запуск сервера отладки 1С")
            debug_port = processes.start_debug_server()
            transport = RdbgTransport(
                runtime.debug_host,
                debug_port,
                transcript=_redacted_transcript_sink(artifacts),
            )
            rdbg = _start_rdbg_with_registration_retry(
                transport=transport,
                location=managed_location,
                alias=runtime.infobase_debug_alias,
                server_target_type=runtime.server_target_type,
                break_on_next=not runtime.is_server_infobase,
                progress=progress,
                on_candidate=registration_candidates.append,
            )
            launch_token = "onec-runtime:" + uuid4().hex if runtime.is_server_infobase else None
            if progress is not None:
                progress("Запуск 1С:Предприятия")
            processes.start_debuggee(
                debug_port,
                execute_external=False,
                thick_client=False,
                startup_parameter=launch_token,
            )
            stage = "managed-bootstrap"
            if progress is not None:
                progress("Ожидание клиентского сеанса 1С")
            managed_stop = wait_for_managed_startup_stop(
                rdbg,
                managed_location,
                timeout_s=150.0,
                on_poll=processes.ensure_running,
            )
            if rdbg.target is None or rdbg.target.target_type != "ManagedClient":
                raise ProtocolError("Runtime bootstrap did not stop on ManagedClient")
            client_target = rdbg.target
            if runtime.is_server_infobase:
                rdbg.bind_server_session(launch_token=launch_token)
            contract = ExtensionHandshakeContract(
                bundle.manifest.product_id,
                bundle.manifest.artifact_version,
                bundle.manifest.protocol_version,
            )
            if config.extension_mode is ExtensionMode.MANUAL:
                def handshake(
                    actual: ServerGuardSession,
                    *,
                    target_type: str,
                    location: ModuleLocation,
                ) -> ExtensionHandshakeEvidence:
                    return observe_extension_handshake(
                        actual, target_type=target_type, location=location
                    )
            else:
                def handshake(
                    actual: ServerGuardSession,
                    *,
                    target_type: str,
                    location: ModuleLocation,
                ) -> ExtensionHandshakeEvidence:
                    return verify_extension_handshake(
                        actual, contract, target_type=target_type, location=location
                    )
            stage = "managed-handshake"
            if progress is not None:
                progress("Проверка расширения в клиентском сеансе")
            managed_handshake = handshake(
                rdbg,
                target_type="ManagedClient",
                location=managed_location,
            )
            stage = "server-guard"
            guard = enable_server_kernel_loop(rdbg)
            server_handshake: ExtensionHandshakeEvidence | None = None
            server_entry_target: TargetId | None = None

            def verify_server_entry(actual: object) -> None:
                nonlocal server_handshake, server_entry_target, stage
                stage = "server-entry-handshake"
                if rdbg.target is None or rdbg.target.target_type != runtime.server_target_type:
                    raise ProtocolError(
                        f"Runtime server entry did not stop on {runtime.server_target_type}"
                    )
                server_entry_target = rdbg.target.target_id
                server_handshake = handshake(
                    cast(ServerGuardSession, actual),
                    target_type=runtime.server_target_type,
                    location=entry_location,
                )
                stage = "server-service"

            stage = "server-entry-wait"
            if progress is not None:
                progress("Подключение серверного сеанса 1С")
            entry_stop, service_stop = wait_for_server_entry_then_service(
                rdbg,
                entry_location,
                service_location,
                timeout_s=150.0,
                on_entry=verify_server_entry,
                server_target_type=runtime.server_target_type,
            )
            if server_handshake is None or server_entry_target is None:
                raise ProtocolError("Runtime server entry handshake was not observed")
            if rdbg.target is None or rdbg.target.target_type != runtime.server_target_type:
                raise ProtocolError(f"Runtime bootstrap did not stop on {runtime.server_target_type}")
            server_target = rdbg.target
            if (
                client_target.target_id.seance_id is None
                or server_entry_target.seance_id is None
                or client_target.target_id.seance_id
                != server_entry_target.seance_id
            ):
                raise ProtocolError(
                    "Runtime client/server targets belong to different sessions"
                )
            if server_target.target_id != server_entry_target:
                raise ProtocolError(
                    "Runtime server entry/service stops belong to different server targets"
                )

            if verify_safe_mode:
                stage = "safe-mode-check"
                if progress is not None:
                    progress("Проверка безопасного режима расширения 1С")
                verify_extension_safe_mode_disabled(rdbg)

            journal = RecoveryJournal(artifacts.append_jsonl)
            controller = PrototypeRuntimeController(
                rdbg,
                service_location,
                command_timeout_s=90.0,
                journal=journal,
            )
            notebook_worker_builder = NotebookWorkerArtifactBuilder(runtime)
            worker_module_builder = WorkerModuleArtifactBuilder(
                notebook_worker_builder,
                cache=WorkerModuleArtifactCache(),
                packer_version="worker-epf-v1",
                target_profile="runtime-session-server-v1",
            )
            api = PrototypeRuntimeApi(
                controller,
                journal=journal,
                notebook_worker_builder=notebook_worker_builder,
                worker_module_builder=worker_module_builder,
            )
            runtime_session = cls(config, processes, transport, rdbg, api, artifacts)
            runtime_session._attempt_bootstrap_evidence = _AttemptBootstrapEvidence(
                managed_stop=_stop_summary(
                    managed_stop,
                    target_type="ManagedClient",
                ),
                server_entry_stop=_stop_summary(
                    entry_stop,
                    target_type=runtime.server_target_type,
                ),
                server_service_stop=_stop_summary(
                    service_stop,
                    target_type=runtime.server_target_type,
                ),
                guard=guard.as_dict(),
                same_session=True,
            )
            return runtime_session, (managed_handshake, server_handshake)
        except BaseException as error:
            if rdbg is None and registration_candidates:
                # The helper may have failed while detaching its last candidate.
                # Keep that exact UI available to the owned cleanup retry.
                rdbg = registration_candidates[-1]
            if stage in {"managed-bootstrap", "server-entry-wait"} and isinstance(
                error, (CommandTimeout, UnexpectedStop, TargetLost)
            ):
                log_path = runtime.logs_dir / "1c-messages.log"
                hint = startup_log_hint(log_path)
                if hint is not None:
                    error = ProcessStartError(
                        f"1С:Предприятие: {hint}; подробности: {log_path}"
                    )
            cleanup_errors: list[BaseException] = []
            cleanup_retry: Callable[[], None] | None = None
            cleanup_error_types: tuple[str, ...] = ()
            if runtime_session is not None:
                try:
                    runtime_session.close()
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)
                    cleanup_retry = runtime_session.close
            else:
                cleanup = _StartupAttemptCleanup(
                    runtime, processes, transport, rdbg
                )
                try:
                    cleanup.retry_cleanup()
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)
                    cleanup_error_types = cleanup.cleanup_error_types
                    cleanup_retry = cleanup.retry_cleanup
            # Keep only our cleanup diagnostics, separate from arbitrary primary notes.
            cleanup_notes: list[str] = []
            recorded_error_types = cleanup_error_types or tuple(
                type(recorded_cleanup_error).__name__
                for recorded_cleanup_error in cleanup_errors
            )
            for error_type in recorded_error_types:
                if not (
                    len(error_type) <= 64
                    and error_type.isascii()
                    and error_type.isidentifier()
                ):
                    error_type = "Exception"
                note = f"Runtime startup cleanup failed: {error_type}"
                cleanup_notes.append(note)
                error.add_note(note)
            if cleanup_retry is not None:
                _expose_startup_cleanup_retry(error, cleanup_retry)
            repairable = (
                stage in {"managed-handshake", "server-entry-handshake"}
                and isinstance(error, ExtensionHandshakeError)
            ) or (
                stage == "safe-mode-check"
                and isinstance(error, ExtensionHandshakeError)
            ) or (
                stage == "managed-bootstrap"
                and isinstance(error, (CommandTimeout, UnexpectedStop))
            )
            raise _OwnedAttemptFailure(
                error,
                stage=stage,
                repairable=repairable,
                cleanup_succeeded=not cleanup_errors,
                cleanup_notes=tuple(cleanup_notes),
            ) from None

    def execute_bsl(
        self,
        source: str,
        *,
        source_unit: SourceUnitRef | None = None,
        on_execution_provenance: (
            Callable[[OperationExecutionProvenance], None] | None
        ) = None,
    ) -> RuntimeReply:
        with self._operation_lock:
            arguments: dict[str, object] = {}
            if source_unit is not None:
                arguments["source_unit"] = source_unit
            if on_execution_provenance is not None:
                arguments["on_execution_provenance"] = (
                    on_execution_provenance
                )
            bind = getattr(
                self.runtime_api,
                "capture_session_caller_handoff",
                None,
            )
            if not callable(bind):
                return self.runtime_api.execute_bsl(source, **arguments)  # type: ignore[arg-type]
            with bind(self._release_operation_lock_for_capture_wait):
                return self.runtime_api.execute_bsl(source, **arguments)  # type: ignore[arg-type]

    @contextmanager
    def _release_operation_lock_for_capture_wait(self) -> Iterator[None]:
        self._operation_lock.release()
        try:
            yield
        finally:
            self._operation_lock.acquire()

    def configure_capture_source(
        self, project: str, source_root: Path | str, *,
        layer: SourceLayer | str = SourceLayer.AUTO,
        extension_name: str | None = None,
    ) -> None:
        with self._operation_lock:
            if self._active_capture_ticket is not None:
                raise ProtocolError(
                    "capture source cannot change during an active capture"
                )
            config = CaptureSourceConfig(project, source_root, layer, extension_name)
            configured = Path(config.source_root)
            native_metadata = any(path.is_file() for path in (
                configured / "Configuration.xml",
                configured / "Configuration" / "Configuration.mdo",
                configured / "src" / "Configuration" / "Configuration.mdo",
            ))
            # Preserve lazy symbolic capture for legacy metadata-only roots.
            # A native configuration or an explicit layer must bind immediately.
            catalog = CaptureSourceCatalog((config,)) if (
                native_metadata or config.layer != SourceLayer.AUTO or extension_name is not None
            ) else None
            resolver = CommonModuleCaptureResolver(project, source_root)
            self.runtime_api.configure_capture_points(())
            self._file_capture_points = ()
            self._capture_source_resolver = resolver
            self._capture_source_catalog = catalog
            self._capture_source_bindings = {}
            self._capture_locations = {}

    def clear_capture_source(self) -> None:
        with self._operation_lock:
            if self._active_capture_ticket is not None:
                raise ProtocolError(
                    "capture source cannot change during an active capture"
                )
            self.runtime_api.configure_capture_points(())
            self._file_capture_points = ()
            self._capture_source_resolver = None
            self._capture_source_catalog = None
            self._capture_source_bindings = {}
            self._capture_locations = {}

    def refresh_capture_sources(self) -> None:
        """Advance configured source generations after external metadata edits."""
        with self._operation_lock:
            if self._active_capture_ticket is not None:
                raise ProtocolError("capture source cannot change during an active capture")
            catalog = getattr(self, "_capture_source_catalog", None)
            if catalog is None:
                raise CaptureSourceNotConfigured("Capture source catalog is not configured")
            self.runtime_api.configure_capture_points(())
            catalog.refresh()
            self._capture_source_bindings = {}
            self._capture_locations = {}
            self._file_capture_points = ()

    def resolve_capture_points(
        self, points: Sequence[CapturePointRequest]
    ) -> tuple[ResolvedCapturePoint, ...]:
        with self._operation_lock:
            resolver = self._capture_source_resolver
            if resolver is None:
                raise CaptureSourceNotConfigured(
                    "Symbolic capture source is not configured; call "
                    "configure_capture_source(project, source_root) first"
                )
            bindings = resolver.resolve(points)
            indexed = {
                (binding.point.name.casefold(), binding.point.line): binding
                for binding in bindings
            }
            self._capture_source_bindings = indexed
            self._capture_locations = {
                key: binding.location for key, binding in indexed.items()
            }
            return tuple(binding.point for binding in bindings)

    def verify_capture_points(
        self, points: Sequence[ResolvedCapturePoint]
    ) -> tuple[CaptureBinding, ...]:
        with self._operation_lock:
            bindings: list[CaptureBinding] = []
            for point in points:
                binding = self._capture_source_bindings.get(
                    (point.name.casefold(), point.line)
                )
                if binding is None or binding.point != point:
                    raise ValueError(
                        "capture point was not resolved by this capture source"
                    )
                bindings.append(binding)
            resolver = self._capture_source_resolver
            if resolver is None:
                raise ValueError(
                    "capture point was not resolved by this capture source"
                )
            result = tuple(bindings)
            resolver.verify(result)
            return result

    def prepare_main_for_capture(
        self, source: str, *, source_unit: SourceUnitRef
    ) -> object:
        with self._operation_lock:
            if self._closed:
                raise ProtocolError("ZUP demo runtime session is closed")
            return self.runtime_api.prepare_main_for_capture(
                source,
                source_unit=source_unit,
            )

    def activate_prepared_main_for_capture(self, prepared: object) -> object:
        with self._operation_lock:
            if self._closed:
                raise ProtocolError("ZUP demo runtime session is closed")
            return self.runtime_api.activate_prepared_main_for_capture(prepared)

    def prepared_main_execution_provenance(
        self,
        prepared: object,
    ) -> OperationExecutionProvenance:
        with self._operation_lock:
            if self._closed:
                raise ProtocolError("ZUP demo runtime session is closed")
            return self.runtime_api.prepared_main_execution_provenance(
                prepared
            )

    def execute_prepared_main_for_capture(self, prepared: object) -> object:
        with self._operation_lock:
            if self._closed:
                raise ProtocolError("ZUP demo runtime session is closed")
            return self.runtime_api._attempt_prepared_main_for_capture(prepared)

    def discard_prepared_main_for_capture(self, prepared: object) -> None:
        with self._operation_lock:
            if self._closed:
                raise ProtocolError("ZUP demo runtime session is closed")
            self.runtime_api.discard_prepared_main_for_capture(prepared)

    def prepare_capture_hypothesis(
        self, source: str, capture: object
    ) -> object:
        with self._operation_lock:
            if self._closed:
                raise ProtocolError("ZUP demo runtime session is closed")
            self._require_capture_fence(capture)
            return self.runtime_api.prepare_capture_hypothesis(source)

    def execute_prepared_capture_hypothesis(
        self, prepared: object, capture: object
    ) -> RuntimeReply:
        with self._operation_lock:
            if self._closed:
                raise ProtocolError("ZUP demo runtime session is closed")
            self._require_capture_fence(capture)
            bind = getattr(
                self.runtime_api,
                "capture_session_caller_handoff",
                None,
            )
            if not callable(bind):
                return self.runtime_api.execute_prepared_capture_hypothesis(prepared)
            with bind(self._release_operation_lock_for_capture_wait):
                return self.runtime_api.execute_prepared_capture_hypothesis(prepared)

    def prepared_capture_hypothesis_provenance(
        self,
        prepared: object,
    ) -> OperationExecutionProvenance:
        with self._operation_lock:
            if self._closed:
                raise ProtocolError("ZUP demo runtime session is closed")
            return self.runtime_api.prepared_capture_hypothesis_provenance(
                prepared
            )

    def quarantine_capture_inspection(self, capture: object) -> None:
        """Revoke every inspection route when preparation ownership is uncertain."""
        with self._operation_lock:
            if self._closed:
                raise ProtocolError("ZUP demo runtime session is closed")
            self._require_capture_fence(capture)
            # Revoke the public session ticket before touching the lower layer:
            # even a failing controller cleanup must leave later reads fenced.
            self._active_capture_ticket = None
            self.runtime_api.invalidate_capture_inspection()

    def arm_capture_intent(self, intent: object) -> RuntimeCaptureArming:
        with self._operation_lock:
            previous_ticket = self._active_capture_ticket
            previous_points = self._active_capture_points
            previous_locations = tuple(getattr(self, "_active_capture_locations", ()))
            previous_file_points = tuple(getattr(self, "_file_capture_points", ()))
            bindings = self.verify_capture_points(intent.points)
            locations = [binding.location for binding in bindings]
            # This public evidence is intentionally planned before live RDBG
            # state changes and never includes the opaque ticket itself.
            self.artifacts.append_jsonl(
                "capture-tickets.jsonl",
                {
                    "capture_ticket_planned": True,
                    "capture_intent_id": intent.capture_intent_id,
                    "capture_generation": intent.capture_generation,
                    "source_revision": intent.source_revision,
                    "source_sha256": intent.source_sha256,
                },
            )
            try:
                if previous_ticket is not None:
                    self.runtime_api.configure_continuation_capture_points(tuple(locations))
                else:
                    self.runtime_api.configure_capture_points(tuple(locations))
                ticket = self.runtime_api.prepare_capture_ticket()
            except BaseException:
                try:
                    if previous_ticket is not None:
                        self.runtime_api.configure_continuation_capture_points(previous_locations)
                    else:
                        self.runtime_api.configure_capture_points(())
                finally:
                    self._active_capture_points = previous_points
                    self._active_capture_ticket = previous_ticket
                    self._file_capture_points = previous_file_points
                raise
            self._active_capture_points = tuple(intent.points)
            self._active_capture_locations = tuple(locations)
            self._file_capture_points = ()
            self._active_capture_ticket = _ActiveCaptureTicket(
                ticket.ticket_id,
                intent.capture_intent_id,
                intent.operation_id,
                intent.capture_generation,
                intent.source_revision,
                intent.source_sha256,
                ticket.expected_stop_sequence,
            )
            return RuntimeCaptureArming(
                ticket.ticket_id,
                ticket.expected_operation_id,
                ticket.expected_stop_sequence,
            )

    def prepare_capture_successor(
        self,
        intent: object | None,
        *,
        attempt: object,
    ) -> _SessionContinuationAdmission:
        """Transact session ticket/points with API/controller successor state."""
        with self._operation_lock:
            if self._closed:
                raise ProtocolError("ZUP demo runtime session is closed")
            previous_ticket = self._active_capture_ticket
            if previous_ticket is None:
                raise ProtocolError("successor admission requires an active capture")
            if attempt.capture_generation != previous_ticket.capture_generation:
                raise ValueError("continuation attempt capture_generation changed")
            previous_points = self._active_capture_points
            previous_locations = self._active_capture_locations
            locations: tuple[object, ...]
            if intent is None:
                locations = ()
            else:
                if (
                    intent.operation_id != attempt.request_operation_id
                    or intent.source_revision != previous_ticket.source_revision
                    or intent.source_sha256 != previous_ticket.source_sha256
                    or intent.capture_generation <= previous_ticket.capture_generation
                ):
                    raise ValueError("capture successor evidence changed")
                resolved_locations: list[object] = []
                for point in intent.points:
                    location = self._capture_locations.get(
                        (point.name.casefold(), point.line)
                    )
                    if location is None:
                        raise ValueError(
                            "capture intent was not resolved by this session"
                        )
                    resolved_locations.append(location)
                locations = tuple(resolved_locations)
            api_admission = self.runtime_api.begin_continuation_admission(
                ContinuationAttemptSpec(
                    attempt.attempt_id,
                    attempt.capture_generation,
                    attempt.request_operation_id,
                    attempt.dirty_roots,
                ),
                locations,  # type: ignore[arg-type]
            )
            try:
                ticket = getattr(api_admission, "ticket", None)
                arming: RuntimeCaptureArming | None = None
                next_points: tuple[object, ...] = ()
                next_locations: tuple[object, ...] = ()
                next_ticket: _ActiveCaptureTicket | None = None
                if intent is not None:
                    if ticket is None:
                        raise ProtocolError(
                            "successor capture ticket is unavailable"
                        )
                    arming = RuntimeCaptureArming(
                        ticket.ticket_id,
                        ticket.expected_operation_id,
                        ticket.expected_stop_sequence,
                    )
                    next_points = tuple(intent.points)
                    next_locations = locations
                    next_ticket = _ActiveCaptureTicket(
                        ticket.ticket_id,
                        intent.capture_intent_id,
                        intent.operation_id,
                        intent.capture_generation,
                        intent.source_revision,
                        intent.source_sha256,
                        ticket.expected_stop_sequence,
                    )
                self._active_capture_points = next_points
                self._active_capture_locations = next_locations
                self._active_capture_ticket = next_ticket
                return _SessionContinuationAdmission(
                    self,
                    api_admission,
                    active_ticket=previous_ticket,
                    active_points=previous_points,
                    active_locations=previous_locations,
                    arming=arming,
                )
            except BaseException:
                try:
                    getattr(api_admission, "rollback")()
                except BaseException:
                    self._active_capture_ticket = None
                    self._active_capture_points = ()
                    self._active_capture_locations = ()
                    raise
                self._active_capture_ticket = previous_ticket
                self._active_capture_points = previous_points
                self._active_capture_locations = previous_locations
                raise

    def continuation_admission_is_uncertain(self) -> bool:
        """Classify a failed lower-layer admission without exposing internals."""
        return self.runtime_api.continuation_admission_is_uncertain()

    def capture_location(
        self, location: object, *, ticket_id: str | None, intent: object
    ) -> object | None:
        with self._operation_lock:
            active = getattr(self, "_active_capture_ticket", None)
            if (
                active is None
                or ticket_id != active.ticket_id
                or intent.capture_intent_id != active.capture_intent_id
                or intent.capture_generation != active.capture_generation
                or intent.source_revision != active.source_revision
                or intent.source_sha256 != active.source_sha256
            ):
                return None
            for point in getattr(self, "_active_capture_points", ()):
                candidate = getattr(self, "_capture_locations", {}).get(
                    (point.name.casefold(), point.line)
                )
                if candidate == location:
                    # Arming is bound to the submitted MAIN revision, while a
                    # paused frame is fenced by the exact source identity that
                    # produced the observed stop.  Publish that transition at
                    # the same correlation boundary that proves the point.
                    self._active_capture_ticket = replace(
                        active,
                        source_revision=point.source_revision,
                        source_sha256=point.source_sha256,
                    )
                    return point
            return None

    def disarm_capture_intent(self, *, policy: str) -> None:
        if policy not in {"terminal_main", "terminal_no_stop"}:
            raise ValueError("capture disarm policy is unsupported")
        with self._operation_lock:
            active = self._active_capture_ticket
            self.runtime_api.configure_capture_points(())
            self._file_capture_points = ()
            self._active_capture_points = ()
            self._active_capture_ticket = None
            if active is not None:
                self._notify_capture_ended(active)

    def resume_capture(
        self,
        *,
        dirty_roots: tuple[str, ...] = (),
        continuation_attempt_id: str | None = None,
    ) -> RuntimeReply:
        with self._operation_lock:
            active = self._active_capture_ticket
            try:
                reply = self.runtime_api.resume_capture(
                    dirty_roots=dirty_roots,
                    continuation_attempt_id=continuation_attempt_id,
                )
            except CaptureBusyError:
                # The coordinator still owns this exact capture. A rejected
                # resume does not end the Session fence or notify listeners.
                raise
            except BaseException:
                # A non-CAPTURED runtime is not inspectable even when transport
                # outcome is unknown; expire the service fence before it can
                # issue another metadata request.
                if active is not None and self.runtime_api.status().state is not OperationState.CAPTURED:
                    self._active_capture_ticket = None
                    self._notify_capture_ended(active)
                raise
            if (
                active is not None
                and getattr(reply, "capture_ticket", None)
                == getattr(active, "ticket_id", None)
                and getattr(reply, "state", None) is OperationState.CAPTURED
            ):
                return reply
            if active is not None:
                self._active_capture_ticket = None
                self._notify_capture_ended(active)
            return reply

    def resume_debug_stop(
        self,
        *,
        timeout_s: float | None = None,
    ) -> RuntimeReply:
        with self._operation_lock:
            active = self._active_capture_ticket
            reply = self.runtime_api.resume_debug_stop(timeout_s=timeout_s)
            if active is None or reply.state in {
                OperationState.CAPTURED,
                OperationState.CAPTURE_DEBUG_STOPPED,
                OperationState.DEBUG_STOPPED,
            }:
                return reply
            self._active_capture_ticket = None
            self._notify_capture_ended(active)
            return reply

    def rearm_capture_successor(self, locations: tuple[object, ...]) -> None:
        with self._operation_lock:
            if self._active_capture_ticket is None:
                raise ProtocolError("successor rearm requires an active capture")
            self.runtime_api.configure_continuation_capture_points(locations)  # type: ignore[arg-type]
            self._active_capture_points = ()
            self._active_capture_locations = ()
            self._active_capture_ticket = None

    def continuation_attempt_evidence(
        self, attempt_id: str
    ) -> ContinuationAttemptEvidence:
        with self._operation_lock:
            return self.runtime_api.continuation_attempt_evidence(attempt_id)

    def add_capture_resume_listener(
        self, listener: Callable[[RuntimeCaptureFence], None]
    ) -> None:
        if not callable(listener):
            raise TypeError("capture resume listener must be callable")
        if not hasattr(self, "_capture_resume_listeners"):
            self._capture_resume_listeners = []
        self._capture_resume_listeners.append(listener)

    def _notify_capture_ended(self, active: _ActiveCaptureTicket) -> None:
        fence = RuntimeCaptureFence(
            active.capture_intent_id,
            active.operation_id,
            active.source_revision,
            active.source_sha256,
            active.capture_generation,
            active.stop_sequence,
        )
        for listener in tuple(getattr(self, "_capture_resume_listeners", ())):
            listener(fence)

    def _require_capture_fence(self, capture: object) -> None:
        active = self._active_capture_ticket
        if active is None or (
            active.capture_intent_id != capture.capture_intent_id
            or active.operation_id != capture.operation_id
            or active.capture_generation != capture.capture_generation
            or active.source_revision != capture.source_revision
            or active.source_sha256 != capture.source_sha256
            or active.stop_sequence != capture.stop_sequence
        ):
            raise ProtocolError("capture frame is stale or unavailable")

    def frame_variables(self, capture: object, *, filters: Mapping[str, object], cursor: int, limit: int, timeout_s: float | None = None) -> Mapping[str, object]:
        with self._operation_lock:
            self._require_capture_fence(capture)
            return self.runtime_api.capture_frame_variables(
                filters=filters, cursor=cursor, limit=limit, timeout_s=timeout_s
            )

    def capture_stack(self, capture: object, *, cursor: int, limit: int, timeout_s: float | None = None) -> Mapping[str, object]:
        with self._operation_lock:
            self._require_capture_fence(capture)
            return self.runtime_api.capture_stack(
                cursor=cursor, limit=limit, timeout_s=timeout_s
            )

    def capture_frame(self, capture: object, *, level: int, cursor: int, limit: int, name: str | None = None, timeout_s: float | None = None) -> Mapping[str, object]:
        with self._operation_lock:
            self._require_capture_fence(capture)
            return self.runtime_api.capture_frame(
                level=level, cursor=cursor, limit=limit, name=name,
                timeout_s=timeout_s,
            )

    def resolve_manager_origin(self, capture: object, origin: object, *, timeout_s: float | None = None) -> Mapping[str, object]:
        with self._operation_lock:
            self._require_capture_fence(capture)
            return self.runtime_api.resolve_capture_manager_origin(
                origin, timeout_s=timeout_s
            )

    def temporary_tables(self, capture: object, manager_handle: str, *, names: tuple[str, ...] | None, cursor: int, limit: int, selection: object | None, timeout_s: float | None = None) -> Mapping[str, object]:
        with self._operation_lock:
            self._require_capture_fence(capture)
            return self.runtime_api.capture_temporary_tables(
                manager_handle, names=names, cursor=cursor, limit=limit,
                selection=selection, timeout_s=timeout_s,
            )

    def load_worker_modules(
        self,
        units: tuple[WorkerModuleUnit, ...],
        *,
        breakpoint_policy: WorkerBreakpointReloadPolicy = (
            WorkerBreakpointReloadPolicy.STRICT
        ),
        profiler: PhaseRecorder | None = None,
    ) -> WorkerGenerationHandle:
        with self._operation_lock:
            def reload_modules() -> WorkerGenerationHandle:
                if type(units) is not tuple or not units:
                    raise ProtocolError(
                        "Worker module units must be a non-empty tuple"
                    )
                if any(not isinstance(unit, WorkerModuleUnit) for unit in units):
                    raise ProtocolError(
                        "Worker module catalog binding does not match"
                    )
                names = tuple(unit.logical_name.casefold() for unit in units)
                if len(names) != len(set(names)):
                    raise ProtocolError("Worker module names must be unique")
                catalog = self._require_common_module_catalog()
                generation = self.runtime_api.load_worker_modules(
                    units,
                    common_modules=catalog,
                    breakpoint_policy=breakpoint_policy,
                    profiler=profiler,
                )
                active_units = self.runtime_api.confirmed_worker_module_units(
                    generation
                )
                source_catalog = getattr(self, "_capture_source_catalog", None)
                previous = getattr(self, "_capture_worker_sources", {})
                published = {
                    unit.logical_name.casefold(): SourceVersionRef.worker(
                        artifact_id=unit.mapped_source.artifact.source_sha256,
                        generation=generation.generation,
                        source_text=unit.mapped_source.text,
                    )
                    for unit in active_units
                }
                # Retained SourceVersionRefs own old text; a successful
                # promotion only publishes the next generation's lookup.
                if source_catalog is not None and set(previous) != set(published):
                    source_catalog.refresh()
                self._capture_worker_sources = published
                self._active_worker_file_units.clear()
                return generation

            return (
                reload_modules()
                if profiler is None
                else profiler.measure(
                    "end_to_end",
                    reload_modules,
                    item_count=lambda _result: len(units),
                )
            )

    def load_worker_module(self, path: Path | str) -> WorkerGenerationHandle:
        """Publish a saved Designer or EDT common module by its BSL file path."""
        with self._operation_lock:
            name, source = self._require_common_module_catalog().read_worker_module_source(
                path
            )
            key = name.casefold()
            revision = self._worker_file_revisions.get(key, 0) + 1
            reference = SourceUnitRef(
                SourceUnitKind.MODULE,
                name,
                revision,
                source_sha256(source),
            )
            unit = WorkerModuleUnit(
                name,
                "module",
                revision,
                mapped_visible_source(source, reference),
            )
            generation = self.load_worker_modules((unit,))
            self._worker_file_revisions[key] = revision
            self._active_worker_file_units[key] = reference
            return generation

    def add_worker_breakpoint(
        self,
        path: str,
        line: int,
    ) -> WorkerBreakpointStatus:
        """Set a one-based source line breakpoint in a file-loaded Worker module."""
        if not isinstance(path, str) or not path:
            raise ValueError("Worker breakpoint path must be a non-empty string")
        if type(line) is not int or line < 1:
            raise ValueError("Worker breakpoint line must be positive")
        with self._operation_lock:
            name, source = self._require_common_module_catalog().read_worker_module_source(
                path
            )
            source_unit = self._active_worker_file_units.get(name.casefold())
            if source_unit is None:
                raise ProtocolError("Worker module must be loaded before adding a breakpoint")
            if source_unit.source_sha256 != source_sha256(source):
                raise ProtocolError("Worker module source changed; reload it before adding a breakpoint")
            return self.runtime_api.add_worker_breakpoint(
                source_unit,
                name.casefold(),
                line,
            )

    def remove_worker_breakpoint(self, breakpoint_id: UUID) -> None:
        with self._operation_lock:
            self.runtime_api.remove_worker_breakpoint(breakpoint_id)

    def set_worker_breakpoint_enabled(
        self,
        breakpoint_id: UUID,
        enabled: bool,
    ) -> WorkerBreakpointStatus:
        with self._operation_lock:
            return self.runtime_api.set_worker_breakpoint_enabled(
                breakpoint_id,
                enabled,
            )

    def worker_breakpoint_status(
        self,
        breakpoint_id: UUID,
    ) -> WorkerBreakpointStatus:
        with self._operation_lock:
            return self.runtime_api.worker_breakpoint_status(breakpoint_id)

    def list_worker_breakpoints(self) -> tuple[WorkerBreakpointStatus, ...]:
        with self._operation_lock:
            return self.runtime_api.list_worker_breakpoints()

    def last_worker_breakpoint_reload_report(
        self,
    ) -> WorkerBreakpointReloadReport | None:
        with self._operation_lock:
            return self.runtime_api.last_worker_breakpoint_reload_report()

    def _require_common_module_catalog(self) -> SessionCommonModuleCatalog:
        catalog = self._common_module_catalog
        if catalog is None:
            raise ProtocolError("Worker common-module source root is not configured")
        return catalog

    def release_worker_generation(self, handle: WorkerGenerationHandle) -> None:
        """Release the active API generation; superseded handles are already stale."""
        with self._operation_lock:
            self.runtime_api.release_worker_generation(handle)
            self._active_worker_file_units.clear()

    def configure_capture_points(self, locations: tuple[ModuleLocation, ...]) -> None:
        """Configure already-resolved debugger locations for an interactive client."""
        with self._operation_lock:
            self.runtime_api.configure_capture_points(locations)
            self._file_capture_points = tuple(locations)

    def add_capture_point(self, path: str, line: int) -> ModuleLocation:
        """Arm a common-module breakpoint using only a source path and line."""
        if not isinstance(path, str) or not path:
            raise ValueError("capture point path must be a non-empty string")
        if type(line) is not int or line < 1:
            raise ValueError("capture point line must be positive")
        with self._operation_lock:
            catalog = self._common_module_catalog
            if catalog is None:
                raise ProtocolError("capture source_root is not configured")
            name, _source = catalog.read_worker_module_source(path)
            location = CommonModuleCaptureResolver(
                "Notebook", catalog.source_root
            ).resolve_module_line(name, line)
            locations = (*self._file_capture_points, location)
            self.runtime_api.configure_capture_points(locations)
            self._file_capture_points = locations
            return location

    def clear_capture_points(self) -> None:
        self.configure_capture_points(())

    def to_df(
        self,
        handle: str,
        *,
        refs: str | ReferenceMode = ReferenceMode.PRESENTATION,
        ref_columns: dict[str, str | ReferenceMode] | None = None,
        uuid_suffix: str = "__uuid",
        chunk_size: int | None = None,
        profiler: PhaseRecorder | None = None,
    ) -> pd.DataFrame:
        with self._operation_lock:
            self._require_public_value_handle(handle)
            return self.runtime_api.materialize_table(
                handle,
                refs=refs,
                ref_columns=ref_columns,
                uuid_suffix=uuid_suffix,
                chunk_size=chunk_size or self.config.chunk_size,
                profiler=profiler,
            )

    def project_to_df(
        self,
        handle: str,
        selection: dict[str, object],
        **options: object,
    ) -> pd.DataFrame:
        """Materialize a bounded table projection for a frontend value proxy."""
        with self._operation_lock:
            self._require_public_value_handle(handle)
            return self.runtime_api.project_to_df(handle, selection, **options)

    def materialize(
        self,
        handle: str,
        *,
        refs: str | ReferenceMode = ReferenceMode.PRESENTATION,
        ref_columns: dict[str, str | ReferenceMode] | None = None,
        uuid_suffix: str = "__uuid",
        chunk_size: int | None = None,
        max_depth: int = 32,
        max_items: int = 100_000,
        max_bytes: int = 64 * 1024 * 1024,
        timeout_s: float | None = None,
        profiler: PhaseRecorder | None = None,
    ) -> object:
        with self._operation_lock:
            self._require_public_value_handle(handle)
            options: dict[str, object] = {
                "refs": refs,
                "ref_columns": ref_columns,
                "uuid_suffix": uuid_suffix,
                "chunk_size": chunk_size or self.config.chunk_size,
                "max_depth": max_depth,
                "max_items": max_items,
                "max_bytes": max_bytes,
                "profiler": profiler,
            }
            if timeout_s is not None:
                options["timeout_s"] = timeout_s
            return self.runtime_api.materialize_value(
                handle,
                **options,
            )

    def materialize_value(self, handle: str, **options: object) -> object:
        """Expose recursive materialization under the frontend proxy contract."""
        return self.materialize(handle, **options)

    def project_value(
        self,
        handle: str,
        selection: dict[str, object],
        **options: object,
    ) -> object:
        """Materialize a bounded recursive projection for a frontend proxy."""
        with self._operation_lock:
            self._require_public_value_handle(handle)
            return self.runtime_api.project_value(handle, selection, **options)

    def materialization_kind(
        self, handle: str, *, timeout_s: float | None = None
    ) -> str:
        with self._operation_lock:
            self._require_public_value_handle(handle)
            if timeout_s is None:
                return self.runtime_api.materialization_kind(handle)
            return self.runtime_api.materialization_kind(handle, timeout_s=timeout_s)

    def materialize_value_payload(self, handle: str, **options: object) -> bytes:
        with self._operation_lock:
            self._require_public_value_handle(handle)
            return self.runtime_api.materialize_value_payload(handle, **options)

    def materialize_table_payload(self, handle: str, **options: object) -> bytes:
        with self._operation_lock:
            self._require_public_value_handle(handle)
            return self.runtime_api.materialize_table_payload(handle, **options)

    def project_value_payload(self, handle: str, selection, **options: object):  # type: ignore[no-untyped-def]
        with self._operation_lock:
            self._require_public_value_handle(handle)
            return self.runtime_api.project_value_payload(
                handle,
                kind=selection.kind.value,
                offset=selection.offset,
                limit=selection.limit,
                columns=selection.columns,
                names=selection.names,
                **options,
            )

    def require_public_value_handle(self, handle: str) -> None:
        with self._operation_lock:
            self._require_public_value_handle(handle)

    def require_public_value_handles(self, handles: tuple[str, ...]) -> None:
        with self._operation_lock:
            self.runtime_api.require_public_value_handles(handles)

    def _require_public_value_handle(self, handle: object) -> None:
        if not isinstance(handle, str):
            return
        normalized = handle.casefold()
        if normalized.startswith(
            "Контекст.RuntimeWorkerActiveGeneration".casefold()
        ) or normalized.startswith("__OnecPinnedWorkerGeneration".casefold()):
            raise ProtocolError("Worker generation objects are not public values")
        self.runtime_api.require_public_value_handle(handle)

    def status(self) -> RuntimeStatus:
        return self.runtime_api.status()

    def current_capture(self) -> CaptureView:
        return self.runtime_api.current_capture()

    def namespace_snapshot(self) -> RuntimeNamespaceSnapshot:
        with self._operation_lock:
            if self._closed:
                raise ProtocolError("ZUP demo runtime session is closed")
            return self.runtime_api.namespace_snapshot()

    def completion_fields(
        self, handle: str, *, table_row: bool = False, timeout_s: float = 1.0
    ) -> tuple[str, ...]:
        """Return live field names without waiting behind a running cell."""
        if not self._operation_lock.acquire(blocking=False):
            raise ProtocolError("Runtime is already executing another request")
        try:
            if self._closed:
                raise ProtocolError("Runtime session is closed")
            return self.runtime_api.completion_fields(
                handle, table_row=table_row, timeout_s=timeout_s
            )
        finally:
            self._operation_lock.release()

    def close(self) -> None:
        self._close(shutdown=False)

    def close_for_kernel_shutdown(self) -> None:
        """Release the owned server session before local Worker bookkeeping.

        A Jupyter kernel has a short external shutdown deadline. Its Python
        objects disappear with the process, but the 1C server session does not.
        """
        self._close(shutdown=self.config.runtime.is_server_infobase)

    def _close(self, *, shutdown: bool) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._heartbeat_stop.set()
            if current_thread() is not self._heartbeat_thread:
                self._heartbeat_thread.join(timeout=2.0)
            errors: list[BaseException] = []
            with self._operation_lock:
                if not shutdown and not self._runtime_api_closed:
                    close_runtime_api = getattr(self.runtime_api, "close", None)
                    if not callable(close_runtime_api):
                        self._runtime_api_closed = True
                    else:
                        try:
                            close_runtime_api()
                        except BaseException as error:
                            errors.append(error)
                        else:
                            self._runtime_api_closed = True
                if self.config.runtime.is_server_infobase:
                    if not self._server_session_terminated:
                        try:
                            self._native_client_termination_requested = self._rdbg.terminate_bound_server_session()
                        except RdbgDebugUiNotRegistered:
                            # The UI is gone; fall back to closing our owned client.
                            self._server_session_terminated = True
                        except BaseException as error:
                            errors.append(error)
                        else:
                            self._server_session_terminated = True
                    # Server termination needs the owned client connection alive.
                    # The cluster debugger belongs to the service, not this session.
                    if self._server_session_terminated and not self._processes_closed:
                        try:
                            if self._native_client_termination_requested:
                                self._processes.close(graceful_client_timeout_s=3.0)
                            else:
                                self._processes.close()
                        except BaseException as error:
                            errors.append(error)
                        else:
                            self._processes_closed = True
                    if self._processes_closed and not self._debug_ui_detached:
                        try:
                            self._rdbg.detach()
                        except BaseException as error:
                            errors.append(error)
                        else:
                            self._debug_ui_detached = True
                if not self._transport_closed and (
                    not self.config.runtime.is_server_infobase
                    or self._debug_ui_detached
                ):
                    try:
                        self._transport.close()
                    except BaseException as error:
                        errors.append(error)
                    else:
                        self._transport_closed = True
                if not self.config.runtime.is_server_infobase and not self._processes_closed:
                    try:
                        self._processes.close()
                    except BaseException as error:
                        errors.append(error)
                    else:
                        self._processes_closed = True
                if (
                    shutdown
                    and self._server_session_terminated
                    and self._processes_closed
                    and self._debug_ui_detached
                    and self._transport_closed
                ):
                    # Local Worker generations die with this kernel. The
                    # authenticated server target and client have been closed.
                    self._runtime_api_closed = True
            if (
                self._runtime_api_closed
                and self._debug_ui_detached
                and self._transport_closed
                and self._processes_closed
            ):
                self._closed = True
            if errors:
                raise ProtocolError(
                    "ZUP demo cleanup failed: "
                    + ", ".join(type(error).__name__ for error in errors)
                ) from errors[0]

    def owned_process_snapshot(self) -> tuple[dict[str, object], ...]:
        """Return exact identities needed for crash-safe owner reconciliation."""
        identities: list[dict[str, object]] = []
        for role, owned in (
            ("dbgs", self._processes.debug_server),
            ("onec", self._processes.debuggee),
        ):
            if owned is None:
                continue
            process = psutil.Process(owned.pid)
            identities.append(
                {
                    "role": role,
                    "pid": owned.pid,
                    "create_time": process.create_time(),
                    "executable": str(Path(process.exe()).resolve()),
                }
            )
        expected_roles = {"onec"} if self.config.runtime.is_server_infobase else {"dbgs", "onec"}
        if {item["role"] for item in identities} != expected_roles:
            raise ProtocolError("ZUP runtime process ownership is incomplete")
        return tuple(identities)

    def __enter__(self) -> "RuntimeSession":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

