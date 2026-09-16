"""Persistent, single-runtime domain service for the local agent boundary."""

from __future__ import annotations

import hashlib
import json
from math import isfinite
import os
import re
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field, replace
from functools import wraps
from pathlib import Path
from threading import Event, RLock
from time import monotonic
from uuid import uuid4

from onec_runtime_mcp.agent.code_store import CodeConflict, CodeDeleteRequest, CodePutRequest, NotebookCodeStore
from onec_runtime_mcp.agent.contracts import (
    AgentOperationState,
    BackendExecution,
    CapabilityDescriptor,
    CapabilityMode,
    CodeDescriptor,
    CodeLanguage,
    CodeMode,
    CodeRevision,
    FailureCategory,
    MAX_MESSAGE_LENGTH,
    MAX_OPERATION_MESSAGES,
    MethodFailure,
    OperationDescriptor,
    RetrySafety,
    RuntimeDescriptor,
    ServiceResponse,
    StateChanged,
    WorkspaceDescriptor,
    sanitize_normalized_diagnostic,
    to_wire,
)
from onec_runtime_mcp.agent.operations import OperationRegistry
from onec_runtime_mcp.agent.capture_contracts import CaptureFence, CapturePointRequest
from onec_runtime_mcp.agent.capture_service import CaptureService
from onec_runtime_mcp.agent.operation_view import OperationViewProjector
from onec_runtime_mcp.agent.facade_contracts import (
    AgentDiagnosticView,
    AgentOperationKind,
    MAX_DIAGNOSTIC_EXCERPT_LENGTH,
    MutationConfidence,
    OperationViewFacts,
    RecoveryAction,
)
from onec_runtime_mcp.agent.observation import (
    ObservationItem,
    ObservationPlan,
    ObservationResult,
    ObservationSourceKind,
    SelectionKind,
    ValueSelection,
)
from onec_runtime_mcp.agent.onec_values import OnecValueResolver, publish_onec_bindings
from onec_runtime_mcp.agent.proxies import (
    ProxyDescriptor,
    ProxyLifetime,
    ProxyProvenance,
    ProxyRealm,
    ProxyRegistry,
    ReleasedProxy,
    StaleProxy,
    ValueBudget,
)
from onec_runtime_mcp.agent.python_protocol import PythonWorkspaceLimits
from onec_runtime_mcp.agent.python_workspace import PythonBindingConflict, PythonWorkspace
from onec_runtime_mcp.agent.runtime_backend import (
    CaptureHypothesisPreparationError,
    CapabilityDenied,
    MainPreparationSourceError,
    RuntimeBackend,
    RuntimeBackendFactory,
)
from onec_runtime_mcp.agent.value_service import (
    OnecMaterializationBridge,
    PythonValueResolver,
    UnsupportedValueOperation,
    ValueService,
)
from onec_runtime_mcp.agent.value_policy import (
    ValueBudgetProfiles,
    ValueCostClass,
)
from onec_runtime.bsl import (
    DiagnosticStage,
    LoweringMode,
    NormalizedDiagnostic,
    SemanticLoweringError,
    SemanticNotebookLowerer,
    VisibleSourceContext,
    normalize_source_error,
)
from onec_runtime.bsl.lexer import BslLexError
from onec_runtime.bsl.notebook_cells import split_notebook_cell
from onec_runtime.bsl.parser_target import BslParseError, PythonParserTarget
from onec_runtime.bsl.source_maps import (
    SourceUnitKind,
    SourceUnitRef,
    mapped_visible_source,
    source_sha256,
)


_MODE_ORDER = {CapabilityMode.OBSERVE: 0, CapabilityMode.EXPERIMENT: 1, CapabilityMode.COMMIT: 2, CapabilityMode.ADMIN: 3}
_CALLER_MAX_LENGTH = 128
_DEFAULT_MODE = CapabilityMode.EXPERIMENT


class RuntimeConflict(RuntimeError):
    """Requested runtime ownership conflicts with the admitted generation."""


class UnsupportedOperation(RuntimeError):
    """The requested capability is intentionally unavailable in this slice."""


class OwnershipUncertain(RuntimeError):
    """Runtime cleanup could not prove that ownership was released."""


@dataclass(slots=True)
class _AdmittedRuntime:
    backend: RuntimeBackend
    runtime_id: str
    generation: int
    mode: CapabilityMode
    profile: str = "default"
    closing: bool = False


@dataclass(slots=True)
class _StartupRequest:
    profile: str
    mode: CapabilityMode
    operation_id: str
    callers: set[str] = field(default_factory=set)


def _serialized_capture_execution(method):  # type: ignore[no-untyped-def]
    """Keep one full capture operation/publication inside its admission gate."""

    @wraps(method)
    def locked(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        with self._capture.capture_admission():
            return method(self, *args, **kwargs)

    return locked


class AgentWorkspaceService:
    """Own one backend, with selection scoped to authenticated caller selectors."""

    def __init__(self, project_root: str | Path, runtime_factory: RuntimeBackendFactory, *, maximum_mode: CapabilityMode = CapabilityMode.OBSERVE, service_instance_id: str | None = None) -> None:
        self.project_root = Path(project_root).resolve(strict=True)
        if not isinstance(maximum_mode, CapabilityMode):
            raise TypeError("maximum_mode must be a CapabilityMode")
        if service_instance_id is not None and (not isinstance(service_instance_id, str) or not service_instance_id):
            raise ValueError("service_instance_id must be a non-empty string")
        self.service_instance_id = service_instance_id or str(uuid4())
        self._factory = runtime_factory
        self._maximum_mode = maximum_mode
        self._state_root = self.project_root / ".runtime" / "agent-service"
        self._ownership_path = self._state_root / "runtime-owner.json"
        self._orphaned_ownership = self._load_runtime_ownership()
        self._store = NotebookCodeStore(self.project_root, self._state_root)
        self._operations = OperationRegistry(self.project_root)
        self._capture = CaptureService(self._state_root)
        self._proxy_registry = ProxyRegistry()
        self._value_resolvers: dict[ProxyRealm, object] = {}
        self._value_service = ValueService(self._proxy_registry, self._value_resolvers)  # type: ignore[arg-type]
        self._python_workspace: PythonWorkspace | None = None
        self._runtime: _AdmittedRuntime | None = None
        self._unknown_startup_cleanup: Callable[[], None] | None = None
        self._closed = False
        self._selected: dict[str, str] = {}
        self._inline: dict[str, CodeRevision] = self._load_inline()
        self._lock = RLock()
        recovered_startup = self._operations.recoverable_startup()
        self._startup_request: _StartupRequest | None = (
            None
            if recovered_startup is None
            else _StartupRequest(
                recovered_startup[1],
                CapabilityMode(recovered_startup[2]),
                recovered_startup[0].operation_id,
                set(),
            )
        )
        self._handlers: dict[str, Callable[[Mapping[str, object], str], object]] = {
            "workspace.open": self._workspace_open, "workspace.status": self._workspace_status, "workspace.capabilities": self._workspace_capabilities, "workspace.close": self._workspace_close,
            "workspace.variables": self._workspace_variables, "workspace.variable": self._workspace_variable, "workspace.variable_history": self._workspace_variable_history, "workspace.snapshot_variables": self._workspace_snapshot_variables, "workspace.delete_variables": self._workspace_delete_variables,
            "runtime.start": self._runtime_start, "runtime.ensure": self._runtime_ensure, "runtime.list": self._runtime_list, "runtime.select": self._runtime_select, "runtime.status": self._runtime_status, "runtime.request_mode": self._runtime_request_mode, "runtime.restart": self._runtime_restart, "runtime.close": self._runtime_close,
            "code.list": self._code_list, "code.get": self._code_get, "code.put": self._code_put, "code.diff": self._code_diff, "code.history": self._code_history, "code.run": self._code_run, "code.run_inline": self._code_run_inline, "code.promote": self._code_promote, "code.delete": self._code_delete,
            "capture.run_until": self._capture_run_until, "capture.inspect": self._capture_inspect,
            "capture.stack": self._capture_stack, "capture.frame": self._capture_frame,
            "capture.hypothesis": self._capture_hypothesis, "capture.continue": self._capture_continue,
            "operation.list": self._operation_list, "operation.status": self._operation_status, "operation.wait": self._operation_wait, "operation.view": self._operation_view, "operation.output": self._operation_output, "operation.result": self._operation_result, "operation.stop_waiting": self._operation_stop_waiting, "operation.abort_generation": self._operation_abort_generation, "operation.explain_failure": self._operation_explain_failure,
            "python.variables": self._python_variables, "python.run": self._python_run, "python.inspect": self._python_inspect, "python.imports": self._python_imports, "python.reset": self._python_reset, "python.status": self._python_status,
        }
        for method in (
            "value.inspect", "value.describe", "value.size", "value.preview", "value.get",
            "value.select", "value.snapshot", "value.materialize", "value.to_df",
            "value.compare", "value.release", "value.export",
        ):
            self._handlers[method] = (
                lambda arguments, caller, method=method: self._value_dispatch(
                    method, arguments, caller
                )
            )

    def call(self, method: str, arguments: Mapping[str, object], *, caller_id: str = "default") -> ServiceResponse:
        if not isinstance(method, str) or not isinstance(arguments, Mapping) or not self._valid_caller_id(caller_id):
            return self._failure(FailureCategory.INVALID_REQUEST, StateChanged.NO, RetrySafety.NO)
        if self._closed:
            return self._failure(FailureCategory.LOST, StateChanged.NO, RetrySafety.NO)
        handler = self._handlers.get(method)
        if handler is None:
            return self._failure(FailureCategory.INVALID_REQUEST, StateChanged.NO, RetrySafety.NO)
        try:
            return ServiceResponse.success(handler(arguments, caller_id))
        except (UnsupportedOperation, UnsupportedValueOperation):
            return self._failure(FailureCategory.UNSUPPORTED, StateChanged.NO, RetrySafety.NO)
        except (StaleProxy, ReleasedProxy):
            return self._failure(FailureCategory.STALE, StateChanged.NO, RetrySafety.NO)
        except CapabilityDenied:
            return self._failure(FailureCategory.DENIED, StateChanged.NO, RetrySafety.NO)
        except (ValueError, TypeError):
            return self._failure(FailureCategory.INVALID_REQUEST, StateChanged.NO, RetrySafety.NO)
        except (CodeConflict, RuntimeConflict):
            return self._failure(FailureCategory.CONFLICT, StateChanged.NO, RetrySafety.NO)
        except OwnershipUncertain:
            return self._failure(FailureCategory.PLATFORM_FAILURE, StateChanged.UNKNOWN, RetrySafety.AFTER_STATUS_CHECK)
        except RuntimeError:
            return self._failure(FailureCategory.PLATFORM_FAILURE, StateChanged.UNKNOWN, RetrySafety.AFTER_STATUS_CHECK)
        except BaseException:
            return self._failure(FailureCategory.UNKNOWN, StateChanged.UNKNOWN, RetrySafety.AFTER_STATUS_CHECK)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._unknown_startup_cleanup is not None:
                self._close_owned_runtime()
            self._closed = True
            with suppress(OwnershipUncertain):
                self._close_owned_runtime()
            python_workspace = self._python_workspace
            self._python_workspace = None
            self._value_resolvers.pop(ProxyRealm.PYTHON, None)
        if python_workspace is not None:
            python_workspace.close()
        self._operations.shutdown()

    def _workspace_open(self, args: Mapping[str, object], caller: str) -> WorkspaceDescriptor:
        self._only(args, {"project"})
        if args.get("project", str(self.project_root)) != str(self.project_root):
            raise ValueError("project must equal configured root")
        with self._lock:
            selected_runtime_id = self._selected.get(caller)
        return WorkspaceDescriptor("workspace", self.project_root.name, str(self.project_root), selected_runtime_id, tuple(self._capabilities().values()))

    def _workspace_status(self, args: Mapping[str, object], caller: str) -> dict[str, object]:
        self._only(args, set())
        with self._lock:
            result: dict[str, object] = {"project_root": str(self.project_root), "service_instance_id": self.service_instance_id, "current_runtime_id": self._selected.get(caller)}
            if self._runtime is not None:
                result["runtime"] = self._runtime.backend.status()
            if self._python_workspace is not None:
                result["python"] = self._python_workspace.status()
        return result

    def _workspace_variables(
        self, args: Mapping[str, object], caller: str
    ) -> tuple[ProxyDescriptor, ...]:
        self._only(args, {"namespace", "changed_since", "include_system"})
        namespace = args.get("namespace", "all")
        if namespace not in {"all", "bsl", "python"}:
            raise ValueError("namespace must be all, bsl, or python")
        changed_since = args.get("changed_since", 0)
        if type(changed_since) is not int or changed_since < 0:
            raise ValueError("changed_since must be non-negative")
        if changed_since != 0:
            raise UnsupportedOperation()
        if args.get("include_system", False) is not False:
            raise UnsupportedOperation()
        bsl = (
            self._proxy_registry.current(ProxyRealm.ONEC)
            if namespace in {"all", "bsl"}
            else ()
        )
        python = (
            self._python_workspace.variables()
            if namespace in {"all", "python"} and self._python_workspace is not None
            else ()
        )
        return tuple(sorted((*bsl, *python), key=lambda item: item.qualified_name.casefold()))

    def _workspace_variable(
        self, args: Mapping[str, object], caller: str
    ) -> ProxyDescriptor:
        self._only(args, {"qualified_name"})
        qualified_name = self._string(args, "qualified_name")
        if qualified_name.casefold().startswith("python."):
            if self._python_workspace is None or qualified_name not in {
                item.qualified_name
                for item in self._python_workspace.variables()
            }:
                raise ValueError("Python variable binding is unknown")
        return self._proxy_registry.resolve_name(qualified_name)

    def _workspace_variable_history(
        self, args: Mapping[str, object], caller: str
    ) -> tuple[ProxyDescriptor, ...]:
        self._only(args, {"qualified_name"})
        qualified_name = self._string(args, "qualified_name")
        return self._proxy_registry.history(qualified_name)

    def _workspace_snapshot_variables(
        self, args: Mapping[str, object], caller: str
    ) -> tuple[ProxyDescriptor, ...]:
        self._only(args, {"names", "budget"})
        names = self._string_sequence(args.get("names"), name="names")
        sources = tuple(
            self._workspace_variable({"qualified_name": name}, caller)
            for name in names
        )
        snapshots: list[ProxyDescriptor] = []
        try:
            for source in sources:
                snapshot = self._value_dispatch(
                    "value.snapshot",
                    {"proxy_id": source.proxy_id, "budget": args.get("budget")},
                    caller,
                )
                if not isinstance(snapshot, ProxyDescriptor):
                    raise TypeError("snapshot result is invalid")
                snapshots.append(snapshot)
        except BaseException as error:
            for snapshot in reversed(snapshots):
                try:
                    self._value_dispatch(
                        "value.release", {"proxy_id": snapshot.proxy_id}, caller
                    )
                except BaseException as cleanup_error:
                    error.add_note(
                        "snapshot rollback failed: "
                        f"{type(cleanup_error).__name__}: {cleanup_error}"
                    )
            raise
        return tuple(snapshots)

    def _workspace_delete_variables(
        self, args: Mapping[str, object], caller: str
    ) -> object:
        del caller
        self._only(args, {"names", "expected_versions"})
        names = self._string_sequence(args.get("names"), name="names")
        expected = args.get("expected_versions")
        if not isinstance(expected, Mapping) or set(expected) != set(names):
            raise ValueError("expected_versions must exactly match names")
        try:
            deleted = self._python().delete_bindings(expected)  # type: ignore[arg-type]
        except PythonBindingConflict as error:
            raise RuntimeConflict() from error
        return {"deleted": deleted}

    def _workspace_capabilities(self, args: Mapping[str, object], caller: str) -> dict[str, CapabilityDescriptor]:
        self._only(args, set())
        return self._capabilities()

    def _workspace_close(self, args: Mapping[str, object], caller: str) -> dict[str, object]:
        self._only(args, {"policy"})
        policy = self._policy(args, {"detach", "abort_generation"})
        with self._lock:
            if policy == "detach":
                self._selected.pop(caller, None)
            else:
                if self._runtime is not None:
                    self._require_close_authorized(caller)
                self._close_owned_runtime()
        return {"closed": True, "policy": policy}

    def _runtime_start(self, args: Mapping[str, object], caller: str) -> RuntimeDescriptor:
        self._only(args, {"mode", "profile"})
        with self._lock:
            if self._orphaned_ownership is not None:
                raise OwnershipUncertain()
            if self._startup_request is not None:
                status = self._operations.status(self._startup_request.operation_id)
                if status.state in {
                    AgentOperationState.COMPLETED,
                    AgentOperationState.FAILED,
                }:
                    self._startup_request = None
            if self._runtime is not None or self._startup_request is not None:
                raise RuntimeConflict()
            return self._start(args, caller)

    def _runtime_ensure(
        self, args: Mapping[str, object], caller: str
    ) -> RuntimeDescriptor | OperationDescriptor:
        self._only(args, {"mode", "profile"})
        requested = self._requested_mode(args)
        profile = self._runtime_profile(args)
        if _MODE_ORDER[requested] > _MODE_ORDER[self._maximum_mode]:
            raise CapabilityDenied()
        with self._lock:
            if self._orphaned_ownership is not None:
                raise OwnershipUncertain()
            runtime = self._runtime
            if runtime is not None:
                if runtime.closing:
                    raise OwnershipUncertain()
                if (
                    runtime.profile != profile
                    or _MODE_ORDER[requested] > _MODE_ORDER[runtime.mode]
                ):
                    raise RuntimeConflict()
                descriptor = runtime.backend.status()
                if self._startup_request is not None:
                    self._startup_request = None
                self._selected[caller] = runtime.runtime_id
                return descriptor

            startup = self._startup_request
            if startup is not None:
                status = self._operations.status(startup.operation_id)
                if status.state in {
                    AgentOperationState.COMPLETED,
                    AgentOperationState.FAILED,
                }:
                    self._startup_request = None
                    startup = None
            if startup is not None:
                if (
                    startup.profile != profile
                    or _MODE_ORDER[requested] > _MODE_ORDER[startup.mode]
                ):
                    raise RuntimeConflict()
                startup.callers.add(caller)
                return status

            gate = Event()
            operation_id: list[str] = []
            inputs_sha256 = hashlib.sha256(
                json.dumps(
                    {"mode": requested.value, "profile": profile},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            operation = self._operations.submit_startup(
                {
                    "inputs_sha256": inputs_sha256,
                    "startup_profile": profile,
                    "startup_mode": requested.value,
                },
                lambda: self._bootstrap_runtime(
                    profile,
                    requested,
                    self._startup_operation_id(gate, operation_id),
                ),
            )
            operation_id.append(operation.operation_id)
            self._startup_request = _StartupRequest(
                profile,
                requested,
                operation.operation_id,
                {caller},
            )
            gate.set()
            return operation

    @staticmethod
    def _startup_operation_id(gate: Event, holder: list[str]) -> str:
        gate.wait()
        return holder[0]

    def _capture_request_operation(
        self, request_id: str, fingerprint: str
    ) -> str | None:
        durable = self._operations.capture_request_operation(
            request_id, fingerprint
        )
        secondary = self._capture.request_operation(request_id, fingerprint)
        if durable is not None and secondary is not None and durable != secondary:
            raise ValueError("capture request journals disagree")
        return durable or secondary

    def _execute_admitted_capture(
        self,
        gate: Event,
        holder: list[str],
        rejected: Event,
        execute: Callable[[str], BackendExecution],
    ) -> BackendExecution:
        operation_id = self._startup_operation_id(gate, holder)
        if rejected.is_set():
            return BackendExecution(
                AgentOperationState.FAILED,
                (),
                False,
                "capture_request_journal_failed",
                failure_stage="capture_request_journal",
            )
        return execute(operation_id)

    def _record_capture_request_or_reject(
        self,
        *,
        request_id: str,
        fingerprint: str,
        operation_id: str,
        gate: Event,
        rejected: Event,
    ) -> None:
        try:
            self._capture.record_request(request_id, fingerprint, operation_id)
        except BaseException:
            rejected.set()
            try:
                self._operations.set_view_facts(
                    operation_id,
                    OperationViewFacts(
                        failure={
                            "stage": "capture_request_journal",
                            "partial_results": {},
                        }
                    ),
                )
            except BaseException:
                # The gate must always be released.  The operation runner will
                # still durably transition to FAILED even if optional view
                # evidence cannot be appended.
                pass
            finally:
                gate.set()
        else:
            gate.set()

    def _bootstrap_runtime(
        self,
        profile: str,
        mode: CapabilityMode,
        operation_id: str,
    ) -> BackendExecution:
        backend: RuntimeBackend | None = None
        candidate: _AdmittedRuntime | None = None
        try:
            with self._lock:
                if self._unknown_startup_cleanup is not None:
                    self._reconcile_unknown_startup(None)
            backend = self._factory.start(mode=mode)
            descriptor = backend.status()
            if (
                descriptor.mode is not mode
                or _MODE_ORDER[descriptor.mode] > _MODE_ORDER[self._maximum_mode]
            ):
                try:
                    backend.close()
                except BaseException:
                    with self._lock:
                        self._runtime = _AdmittedRuntime(
                            backend,
                            descriptor.runtime_id,
                            descriptor.generation,
                            descriptor.mode,
                            profile,
                            closing=True,
                        )
                        self._selected.clear()
                    return BackendExecution(
                        AgentOperationState.UNKNOWN, (), False, "unknown"
                    )
                return BackendExecution(
                    AgentOperationState.FAILED, (), False, "absent"
                )
            candidate = _AdmittedRuntime(
                backend,
                descriptor.runtime_id,
                descriptor.generation,
                descriptor.mode,
                profile,
            )
            with self._lock:
                startup = self._startup_request
                if (
                    self._closed
                    or startup is None
                    or startup.operation_id != operation_id
                ):
                    backend.close()
                    return BackendExecution(
                        AgentOperationState.UNKNOWN, (), False, "absent"
                    )
                self._install_onec_resolver(backend)
                self._write_runtime_ownership(candidate, operation_id=operation_id)
                self._runtime = candidate
                for caller in startup.callers:
                    self._selected[caller] = candidate.runtime_id
            return BackendExecution(
                AgentOperationState.COMPLETED, (), False, descriptor.state
            )
        except BaseException as error:
            cleanup_failed = False
            retry_cleanup = getattr(error, "retry_cleanup", None)
            startup_cleanup_pending = False
            if backend is not None:
                try:
                    backend.close()
                except BaseException:
                    cleanup_failed = True
            with self._lock:
                startup_cleanup_pending = backend is None and (
                    callable(retry_cleanup)
                    or self._unknown_startup_cleanup is not None
                )
                if candidate is not None:
                    self._proxy_registry.invalidate_runtime_generation(
                        candidate.runtime_id,
                        candidate.generation,
                    )
                    if not cleanup_failed:
                        self._clear_runtime_ownership()
                if backend is not None and self._runtime is not None and self._runtime.backend is backend:
                    self._runtime = None
                self._value_resolvers.pop(ProxyRealm.ONEC, None)
                self._selected.clear()
                if callable(retry_cleanup):
                    self._unknown_startup_cleanup = retry_cleanup
                if cleanup_failed and backend is not None:
                    self._runtime = _AdmittedRuntime(
                        backend, backend.runtime_id, 1, mode, profile, closing=True
                    )
            return BackendExecution(
                (
                    AgentOperationState.UNKNOWN
                    if cleanup_failed or startup_cleanup_pending
                    else AgentOperationState.FAILED
                ),
                (),
                False,
                "unknown" if cleanup_failed or startup_cleanup_pending else "absent",
            )

    def _runtime_list(self, args: Mapping[str, object], caller: str) -> tuple[RuntimeDescriptor, ...]:
        self._only(args, set())
        with self._lock:
            return () if self._runtime is None else (self._runtime.backend.status(),)

    def _runtime_select(self, args: Mapping[str, object], caller: str) -> RuntimeDescriptor:
        self._only(args, {"runtime_id"})
        with self._lock:
            runtime = self._require_usable_runtime(caller, self._string(args, "runtime_id"), allow_unselected=True)
            descriptor = runtime.backend.status()
            self._selected[caller] = runtime.runtime_id
            return descriptor

    def _runtime_status(self, args: Mapping[str, object], caller: str) -> RuntimeDescriptor:
        self._only(args, {"runtime_id"})
        requested = self._string(args, "runtime_id") if "runtime_id" in args else None
        with self._lock:
            runtime = self._require_runtime(caller, requested, allow_unselected=requested is not None)
            return runtime.backend.status()

    def _runtime_request_mode(self, args: Mapping[str, object], caller: str) -> dict[str, object]:
        self._only(args, {"mode"})
        mode = self._mode(args.get("mode"))
        if _MODE_ORDER[mode] > _MODE_ORDER[self._maximum_mode]:
            raise CapabilityDenied()
        return {"requested_mode": mode, "maximum_mode": self._maximum_mode, "granted": False}

    def _runtime_restart(self, args: Mapping[str, object], caller: str) -> RuntimeDescriptor:
        self._only(args, {"policy", "mode"})
        self._policy(args, {"abort_generation"})
        requested = self._requested_mode(args)
        if _MODE_ORDER[requested] > _MODE_ORDER[self._maximum_mode]:
            raise CapabilityDenied()
        with self._lock:
            if self._orphaned_ownership is not None:
                reconcile = getattr(self._factory, "reconcile_orphaned_runtime", None)
                if (
                    not callable(reconcile)
                    or reconcile(self._orphaned_ownership) is not True
                ):
                    raise OwnershipUncertain()
                self._clear_runtime_ownership()
                profile = self._orphaned_ownership["profile"]
                self._orphaned_ownership = None
                return self._start(
                    {"mode": requested.value, "profile": profile},
                    caller,
                )
            if self._runtime is None and self._startup_request is not None:
                startup = self._startup_request
                status = self._operations.status(startup.operation_id)
                if status.state is not AgentOperationState.UNKNOWN:
                    raise RuntimeConflict()
                self._reconcile_unknown_startup(startup)
                return self._start(
                    {"mode": requested.value, "profile": startup.profile},
                    caller,
                )
            runtime = self._require_close_authorized(caller)
            profile = runtime.profile
            self._close_owned_runtime()
            return self._start(
                {"mode": requested.value, "profile": profile},
                caller,
            )

    def _runtime_close(self, args: Mapping[str, object], caller: str) -> dict[str, object]:
        self._only(args, {"policy"})
        self._policy(args, {"abort_generation"})
        with self._lock:
            if self._runtime is None and self._unknown_startup_cleanup is not None:
                self._reconcile_unknown_startup(self._startup_request)
                return {"closed": True, "policy": "abort_generation"}
            self._require_close_authorized(caller)
            self._close_owned_runtime()
        return {"closed": True, "policy": "abort_generation"}

    def _code_list(self, args: Mapping[str, object], caller: str) -> tuple[CodeDescriptor, ...]:
        self._only(args, {"container", "filters"})
        entries = self._store.list(self._string(args, "container"))
        filters = args.get("filters", {})
        if not isinstance(filters, Mapping) or set(filters) - {"language", "mode"}:
            raise ValueError("unsupported code filters")
        language = self._language(filters["language"]) if "language" in filters else None
        mode = self._code_mode(filters["mode"]) if "mode" in filters else None
        return tuple(entry for entry in entries if (language is None or entry.language is language) and (mode is None or entry.mode is mode))

    def _code_get(self, args: Mapping[str, object], caller: str) -> CodeRevision:
        self._only(args, {"cell_id", "revision"})
        cell_id = self._string(args, "cell_id")
        revision = args.get("revision")
        if revision is not None and type(revision) is not int:
            raise ValueError("revision must be integer")
        if cell_id in self._inline:
            inline = self._inline[cell_id]
            if revision not in {None, inline.revision}:
                raise ValueError("inline revision not found")
            return inline
        return self._store.get(cell_id, revision)

    def _code_put(self, args: Mapping[str, object], caller: str) -> CodeDescriptor:
        self._only(args, {"cell_id", "source", "language", "mode", "expected_revision", "expected_document_sha256", "outputs"})
        cell_id = self._string(args, "cell_id")
        previous = self._store.get(cell_id)
        revision = self._store.put(cell_id=cell_id, source=self._string(args, "source"), language=self._language(args.get("language")), mode=self._code_mode(args.get("mode")), expected_revision=self._positive(args, "expected_revision"), expected_document_sha256=self._string(args, "expected_document_sha256"), outputs=self._string_sequence(args.get("outputs", ()), name="outputs"))
        if self._capture.active_source_matches(
            revision=previous.revision, source_sha256=previous.source_sha256
        ):
            self._capture.invalidate_capture(self._proxy_registry)
        return self._descriptor_for(revision)

    def _code_diff(self, args: Mapping[str, object], caller: str) -> dict[str, object]:
        self._only(args, {"cell_id", "left", "right"})
        return {"diff": self._store.diff(self._string(args, "cell_id"), self._positive(args, "left"), self._positive(args, "right"))}

    def _code_history(self, args: Mapping[str, object], caller: str) -> dict[str, object]:
        self._only(args, {"cell_id"})
        cell_id = self._string(args, "cell_id")
        revisions = (self._inline[cell_id],) if cell_id in self._inline else self._store.history(cell_id)
        operations = self._operations.list()
        return {"revisions": tuple(self._descriptor_for(item) for item in revisions), "operations": tuple(item for item in operations if item.cell_id == cell_id)}

    def _code_run(self, args: Mapping[str, object], caller: str) -> object:
        self._only(args, {"cell_id", "revision", "source_sha256", "inputs", "outputs", "wait_s", "observe"})
        revision = self._code_get({"cell_id": self._string(args, "cell_id"), "revision": self._positive(args, "revision")}, caller)
        if self._string(args, "source_sha256") != revision.source_sha256:
            raise RuntimeConflict()
        return self._submit_revision(revision, args, caller)

    def _code_run_inline(self, args: Mapping[str, object], caller: str) -> object:
        self._only(args, {"language", "mode", "source", "inputs", "outputs", "wait_s", "observe"})
        source = self._string(args, "source")
        revision = CodeRevision(f"inline-{uuid4()}", 1, source, hashlib.sha256(source.encode()).hexdigest(), hashlib.sha256(source.encode()).hexdigest(), self._language(args.get("language")), self._code_mode(args.get("mode")), self._string_sequence(args.get("outputs", ()), name="outputs"))
        if revision.mode is not CodeMode.MAIN:
            raise UnsupportedOperation()
        self._persist_inline(revision)
        self._inline[revision.cell_id] = revision
        return self._submit_revision(
            revision,
            args,
            caller,
            operation_kind=AgentOperationKind.CODE_RUN_INLINE,
        )

    def _capture_run_until(self, args: Mapping[str, object], caller: str) -> object:
        self._only(
            args,
            {"cell_id", "revision", "source_sha256", "points", "wait_s", "request_id"},
        )
        revision = self._code_get(
            {
                "cell_id": self._string(args, "cell_id"),
                "revision": self._positive(args, "revision"),
            },
            caller,
        )
        if self._string(args, "source_sha256") != revision.source_sha256:
            raise RuntimeConflict()
        if revision.language is not CodeLanguage.BSL or revision.mode is not CodeMode.MAIN:
            raise UnsupportedOperation()
        points_raw = args.get("points")
        if isinstance(points_raw, str) or not isinstance(points_raw, (tuple, list)):
            raise ValueError("points must be a sequence")
        points = tuple(CapturePointRequest.from_wire(item) for item in points_raw)
        if not points or len(points) > 32:
            raise ValueError("points must be a non-empty bounded sequence")
        if len({item.name.casefold() for item in points}) != len(points):
            raise ValueError("capture point names must be unique")
        request_id = self._string(args, "request_id")
        points_wire = tuple(
            {
                "name": point.name,
                "project": point.project,
                "module": point.module,
                "procedure": point.procedure,
                "line": point.line,
                "source_fragment": point.source_fragment,
            }
            for point in points
        )
        inputs_sha256 = hashlib.sha256(
            json.dumps(
                {
                    "cell_id": revision.cell_id,
                    "revision": revision.revision,
                    "source_sha256": revision.source_sha256,
                    "points": points_wire,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        existing = self._capture_request_operation(request_id, inputs_sha256)
        if existing is not None:
            return self._operation_view(
                {"operation_id": existing, "after_message_cursor": 0}, caller
            )
        with self._lock:
            # Recheck beneath the admission lock: two retries may both have
            # observed an absent request before either durable submission.
            existing = self._capture_request_operation(request_id, inputs_sha256)
            if existing is not None:
                return self._operation_view(
                    {"operation_id": existing, "after_message_cursor": 0}, caller
                )
            runtime = self._require_usable_runtime(caller, None)
            if _MODE_ORDER[runtime.mode] < _MODE_ORDER[CapabilityMode.EXPERIMENT]:
                raise CapabilityDenied()
            gate = Event()
            rejected = Event()
            operation_id: list[str] = []
            operation = self._operations.submit(
                {
                    "operation_kind": AgentOperationKind.CAPTURE_RUN_UNTIL.value,
                    "runtime_id": runtime.runtime_id,
                    "runtime_generation": runtime.generation,
                    "code_id": revision.cell_id,
                    "revision": revision.revision,
                    "source_sha256": revision.source_sha256,
                    "inputs_sha256": inputs_sha256,
                    "request_id": request_id,
                },
                lambda: self._execute_admitted_capture(
                    gate,
                    operation_id,
                    rejected,
                    lambda admitted_operation_id: self._execute_capture_run_until(
                        runtime,
                        revision,
                        points,
                        admitted_operation_id,
                    ),
                ),
            )
            operation_id.append(operation.operation_id)
            self._record_capture_request_or_reject(
                request_id=request_id,
                fingerprint=inputs_sha256,
                operation_id=operation.operation_id,
                gate=gate,
                rejected=rejected,
            )
        self._operations.wait(
            operation.operation_id,
            self._number({"wait_s": args.get("wait_s", 30.0)}, "wait_s"),
            waiter_id=caller,
        )
        return self._operation_view(
            {"operation_id": operation.operation_id, "after_message_cursor": 0}, caller
        )

    def _capture_inspect(self, args: Mapping[str, object], caller: str) -> object:
        self._only(args, {"fence", "filters", "cursor", "limit", "observe"})
        runtime = self._require_usable_runtime(caller, None)
        fence = CaptureFence.from_wire(args.get("fence"))
        filters = args.get("filters", {})
        if not isinstance(filters, Mapping):
            raise ValueError("capture inspection filters must be a mapping")
        observe_raw = args.get("observe")
        observe = None if observe_raw is None else ObservationPlan.from_wire(observe_raw)
        inspection_profile = (
            "agent_metadata" if observe is None else observe.budget_profile
        )
        return self._capture.inspect(
            runtime.backend,  # type: ignore[arg-type]
            self._proxy_registry,
            fence=fence,
            runtime_id=runtime.runtime_id,
            runtime_generation=runtime.generation,
            context_generation=runtime.generation,
            filters=filters,
            cursor=args.get("cursor", 0),
            limit=args.get("limit", 20),
            observe=observe,
            timeout_s=ValueBudgetProfiles().resolve(
                inspection_profile
            ).timeout_seconds,
        )

    def _capture_stack(self, args: Mapping[str, object], caller: str) -> object:
        self._only(args, {"fence", "cursor", "limit"})
        fence = CaptureFence.from_wire(args.get("fence"))
        cursor = args.get("cursor", 0)
        limit = args.get("limit", 20)
        if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("capture stack page is invalid")
        runtime = self._require_usable_runtime(caller, None)
        return self._capture.stack(
            runtime.backend, fence=fence, cursor=cursor, limit=limit,
            timeout_s=ValueBudgetProfiles().resolve("agent_metadata").timeout_seconds,
        )

    def _capture_frame(self, args: Mapping[str, object], caller: str) -> object:
        self._only(args, {"fence", "level", "cursor", "limit", "name"})
        fence = CaptureFence.from_wire(args.get("fence"))
        level = args.get("level")
        cursor = args.get("cursor", 0)
        limit = args.get("limit", 20)
        name = args.get("name")
        if type(level) is not int or level < 0:
            raise ValueError("capture frame level is invalid")
        if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("capture frame page is invalid")
        if name is not None and (
            not isinstance(name, str)
            or not name
            or len(name) > 256
            or re.fullmatch(r"[^\W\d]\w*", name, re.UNICODE) is None
        ):
            raise ValueError("capture variable name is invalid")
        runtime = self._require_usable_runtime(caller, None)
        return self._capture.frame(
            runtime.backend, fence=fence, level=level, cursor=cursor,
            limit=limit, name=name,
            timeout_s=ValueBudgetProfiles().resolve("agent_preview").timeout_seconds,
        )

    def _capture_continue(self, args: Mapping[str, object], caller: str) -> object:
        """Submit the sole durable continuation for one active capture fence."""
        self._only(args, {"fence", "next_points", "observe", "request_id", "wait_s"})
        fence = CaptureFence.from_wire(args.get("fence"))
        points_raw = args.get("next_points", ())
        if isinstance(points_raw, str) or not isinstance(points_raw, (tuple, list)):
            raise ValueError("next_points must be a sequence")
        points = tuple(CapturePointRequest.from_wire(item) for item in points_raw)
        if len(points) > 32 or len({point.name.casefold() for point in points}) != len(points):
            raise ValueError("next_points must be bounded and uniquely named")
        observe_raw = args.get("observe")
        observe = None if observe_raw is None else ObservationPlan.from_wire(observe_raw)
        request_id = self._string(args, "request_id")
        fingerprint = hashlib.sha256(json.dumps(
            {"fence": to_wire(fence), "next_points": [to_wire(point) for point in points],
             "observe": None if observe is None else to_wire(observe)},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        existing = self._capture_request_operation(request_id, fingerprint)
        if existing is not None:
            return self._operation_view({"operation_id": existing, "after_message_cursor": 0}, caller)
        self._capture.current_capture(fence)
        with self._lock:
            existing = self._capture_request_operation(request_id, fingerprint)
            if existing is not None:
                return self._operation_view({"operation_id": existing, "after_message_cursor": 0}, caller)
            runtime = self._require_usable_runtime(caller, None)
            if _MODE_ORDER[runtime.mode] < _MODE_ORDER[CapabilityMode.EXPERIMENT]:
                raise CapabilityDenied()
            self._capture.current_capture(fence)
            gate = Event()
            rejected = Event()
            operation_id: list[str] = []
            operation = self._operations.submit(
                {"operation_kind": AgentOperationKind.CAPTURE_CONTINUE.value,
                 "runtime_id": runtime.runtime_id, "runtime_generation": runtime.generation,
                 "code_id": fence.operation_id, "revision": fence.source_revision,
                 "source_sha256": fence.source_sha256,
                 "inputs_sha256": fingerprint, "request_id": request_id},
                lambda: self._execute_admitted_capture(
                    gate,
                    operation_id,
                    rejected,
                    lambda admitted_operation_id: self._execute_capture_continue(
                        runtime, fence, points, observe, admitted_operation_id
                    ),
                ),
            )
            operation_id.append(operation.operation_id)
            self._record_capture_request_or_reject(
                request_id=request_id,
                fingerprint=fingerprint,
                operation_id=operation.operation_id,
                gate=gate,
                rejected=rejected,
            )
        self._operations.wait(operation.operation_id, self._number({"wait_s": args.get("wait_s", 30.0)}, "wait_s"), waiter_id=caller)
        return self._operation_view({"operation_id": operation.operation_id, "after_message_cursor": 0}, caller)

    def _capture_hypothesis(self, args: Mapping[str, object], caller: str) -> object:
        """Run one exact CAPTURE revision while preserving the current stop.

        The exact revision is admitted before preparation.  Preparation then
        runs in the one-wide operation/capture lane before evaluation, frame
        staging, observations, or any continuation.
        """
        self._only(args, {"fence", "code_ref", "observe", "request_id", "wait_s"})
        fence = CaptureFence.from_wire(args.get("fence"))
        code_ref = args.get("code_ref")
        if not isinstance(code_ref, Mapping) or set(code_ref) != {
            "cell_id", "revision", "source_sha256"
        }:
            raise ValueError("capture hypothesis requires exact code_ref")
        revision = self._code_get(
            {
                "cell_id": self._string(code_ref, "cell_id"),
                "revision": self._positive(code_ref, "revision"),
            },
            caller,
        )
        if self._string(code_ref, "source_sha256") != revision.source_sha256:
            raise RuntimeConflict()
        if revision.language is not CodeLanguage.BSL or revision.mode is not CodeMode.CAPTURE:
            raise UnsupportedOperation()
        observe_raw = args.get("observe")
        observe = None if observe_raw is None else ObservationPlan.from_wire(observe_raw)
        request_id = self._string(args, "request_id")
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "fence": to_wire(fence),
                    "cell_id": revision.cell_id,
                    "revision": revision.revision,
                    "source_sha256": revision.source_sha256,
                    "observe": None if observe is None else to_wire(observe),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        existing = self._capture_request_operation(request_id, fingerprint)
        if existing is not None:
            return self._operation_view({"operation_id": existing, "after_message_cursor": 0}, caller)
        # Reject stale fences before creating a runtime-lane operation.  The
        # same check is repeated inside the operation to close the admission
        # race with an explicit continuation.
        self._capture.current_capture(fence)
        with self._lock:
            existing = self._capture_request_operation(request_id, fingerprint)
            if existing is not None:
                return self._operation_view({"operation_id": existing, "after_message_cursor": 0}, caller)
            runtime = self._require_usable_runtime(caller, None)
            if _MODE_ORDER[runtime.mode] < _MODE_ORDER[CapabilityMode.EXPERIMENT]:
                raise CapabilityDenied()
            self._capture.current_capture(fence)
            gate = Event()
            rejected = Event()
            operation_id: list[str] = []
            operation = self._operations.submit(
                {
                    "operation_kind": AgentOperationKind.CAPTURE_HYPOTHESIS.value,
                    "runtime_id": runtime.runtime_id,
                    "runtime_generation": runtime.generation,
                    "code_id": revision.cell_id,
                    "revision": revision.revision,
                    "source_sha256": revision.source_sha256,
                    "inputs_sha256": fingerprint,
                    "request_id": request_id,
                },
                lambda: self._execute_admitted_capture(
                    gate,
                    operation_id,
                    rejected,
                    lambda admitted_operation_id: self._execute_capture_hypothesis(
                        runtime,
                        revision,
                        fence,
                        observe,
                        admitted_operation_id,
                    ),
                ),
            )
            operation_id.append(operation.operation_id)
            self._record_capture_request_or_reject(
                request_id=request_id,
                fingerprint=fingerprint,
                operation_id=operation.operation_id,
                gate=gate,
                rejected=rejected,
            )
        self._operations.wait(
            operation.operation_id,
            self._number({"wait_s": args.get("wait_s", 30.0)}, "wait_s"),
            waiter_id=caller,
        )
        return self._operation_view({"operation_id": operation.operation_id, "after_message_cursor": 0}, caller)

    @_serialized_capture_execution
    def _quarantine_capture_preparation(
        self, runtime: _AdmittedRuntime, fence: CaptureFence
    ) -> None:
        """Revoke all inspection owners without executing or continuing 1C."""
        runtime.closing = True
        with suppress(BaseException):
            self._capture.invalidate_capture(self._proxy_registry)
        with suppress(BaseException):
            runtime.backend.quarantine_capture_inspection(fence)

    def _code_promote(self, args: Mapping[str, object], caller: str) -> CodeDescriptor:
        self._only(args, {"operation_id", "cell_id", "expected_revision", "expected_document_sha256"})
        operation = self._operations.status(self._string(args, "operation_id"))
        inline = self._inline.get(operation.cell_id or "")
        if inline is None:
            raise ValueError("only inline operation promotion is available")
        revision = self._store.promote(CodePutRequest(self._string(args, "cell_id"), inline.source, inline.language, inline.mode, self._positive(args, "expected_revision"), self._string(args, "expected_document_sha256"), inline.outputs))
        return self._descriptor_for(revision)

    def _code_delete(self, args: Mapping[str, object], caller: str) -> None:
        self._only(args, {"cell_id", "expected_revision", "expected_document_sha256"})
        self._store.delete(CodeDeleteRequest(self._string(args, "cell_id"), self._positive(args, "expected_revision"), self._string(args, "expected_document_sha256")))

    def _operation_list(self, args: Mapping[str, object], caller: str) -> object:
        self._only(args, {"filters"})
        return self._operations.list(args.get("filters"))

    def _operation_status(self, args: Mapping[str, object], caller: str) -> object:
        self._only(args, {"operation_id"})
        return self._operations.status(self._string(args, "operation_id"))

    def _operation_wait(self, args: Mapping[str, object], caller: str) -> object:
        self._only(args, {"operation_id", "timeout_s", "after_cursor"})
        return self._operations.wait(self._string(args, "operation_id"), self._number(args, "timeout_s"), args.get("after_cursor"), caller)

    def _operation_view(self, args: Mapping[str, object], caller: str) -> object:
        del caller
        self._only(
            args,
            {
                "operation_id",
                "after_message_cursor",
                "message_limit",
                "changed_limit",
                "output_limit",
            },
        )
        return OperationViewProjector(
            self._operations,
            lambda proxy_id: self._proxy_registry.resolve(proxy_id),
        ).project(
            self._string(args, "operation_id"),
            after_message_cursor=args.get("after_message_cursor", 0),  # type: ignore[arg-type]
            message_limit=args.get("message_limit", 20),  # type: ignore[arg-type]
            changed_limit=args.get("changed_limit", 100),  # type: ignore[arg-type]
            output_limit=args.get("output_limit", 100),  # type: ignore[arg-type]
        )

    def _operation_output(self, args: Mapping[str, object], caller: str) -> object:
        self._only(args, {"operation_id", "after_cursor", "limits"})
        return self._operations.output(self._string(args, "operation_id"), args.get("after_cursor", 0), args.get("limits"))

    def _operation_result(self, args: Mapping[str, object], caller: str) -> object:
        self._only(args, {"operation_id"})
        descriptor = self._operations.result(self._string(args, "operation_id"))
        if descriptor.result_present:
            raise UnsupportedOperation()
        return descriptor

    def _operation_stop_waiting(self, args: Mapping[str, object], caller: str) -> object:
        self._only(args, {"operation_id"})
        return self._operations.stop_waiting(self._string(args, "operation_id"), caller)

    def _operation_abort_generation(self, args: Mapping[str, object], caller: str) -> object:
        self._only(args, {"operation_id"})
        descriptor = self._operations.status(self._string(args, "operation_id"))
        with self._lock:
            if self._runtime is not None and (self._runtime.runtime_id, self._runtime.generation) == (descriptor.runtime_id, descriptor.runtime_generation):
                self._require_close_authorized(caller)
                return self._close_owned_runtime()
            try:
                return self._operations.mark_generation_aborted(descriptor.runtime_id, descriptor.runtime_generation or 1)
            except BaseException as error:
                raise OwnershipUncertain() from error

    def _operation_explain_failure(self, args: Mapping[str, object], caller: str) -> dict[str, object]:
        self._only(args, {"operation_id"})
        operation_id = self._string(args, "operation_id")
        snapshot = self._operations.view_snapshot(operation_id)
        descriptor = snapshot.operation
        failure = snapshot.facts.failure
        public_diagnostic: AgentDiagnosticView | None = None
        if failure is not None and failure.get("diagnostic") is not None:
            try:
                public_diagnostic = AgentDiagnosticView.from_wire(
                    failure["diagnostic"]
                )
            except (TypeError, ValueError):
                public_diagnostic = None
        state_changed = (
            StateChanged.UNKNOWN
            if descriptor.state
            in {AgentOperationState.UNKNOWN, AgentOperationState.FAILED}
            else StateChanged.NO
        )
        if failure is not None:
            try:
                state_changed = StateChanged(failure.get("state_changed"))
            except (TypeError, ValueError):
                pass
        category = FailureCategory.LOST if descriptor.state is AgentOperationState.UNKNOWN else (FailureCategory.PLATFORM_FAILURE if descriptor.state is AgentOperationState.FAILED else FailureCategory.INVALID_REQUEST)
        diagnostic_details = (
            None
            if public_diagnostic is None
            else self._operations.expert_diagnostic(
                descriptor.operation_id,
                public_diagnostic.diagnostic_id,
            )
        )
        return {
            "operation_id": descriptor.operation_id,
            "category": category,
            "state": descriptor.state,
            "state_changed": state_changed,
            "safe_to_retry": descriptor.safe_to_retry,
            "cell_id": descriptor.cell_id,
            "revision": descriptor.revision,
            "source_sha256": descriptor.source_sha256,
            "recommended_actions": (
                "runtime.status",
                "code.get",
                "runtime.restart",
            ),
            "diagnostic": (
                None if public_diagnostic is None else to_wire(public_diagnostic)
            ),
            "diagnostic_details": diagnostic_details,
        }

    def _value_dispatch(
        self,
        method: str,
        arguments: Mapping[str, object],
        caller: str,
    ) -> object:
        proxy_fields = (
            ("left_proxy_id", "right_proxy_id")
            if method == "value.compare"
            else (() if method == "value.export" else ("proxy_id",))
        )
        capture_scoped = False
        with self._lock:
            for field in proxy_fields:
                proxy_id = arguments.get(field)
                if isinstance(proxy_id, str):
                    descriptor = (
                        self._proxy_registry.metadata(proxy_id)
                        if method == "value.release"
                        else self._proxy_registry.resolve(proxy_id)
                    )
                    if descriptor.realm is ProxyRealm.ONEC:
                        runtime = self._require_usable_runtime(caller, None)
                        if descriptor.fence.runtime_id != runtime.runtime_id:
                            raise StaleProxy("1C proxy belongs to another runtime")
                        capture_scoped = (
                            capture_scoped
                            or descriptor.lifetime is ProxyLifetime.FRAME
                        )
                        if method in {
                            "value.snapshot",
                            "value.materialize",
                            "value.to_df",
                        }:
                            self._python()
        if capture_scoped:
            # Frame reads, derived publications, and releases share the same
            # boundary as capture inspection and continuation rollback.  Python
            # and persistent context proxies remain independent.
            with self._capture.capture_admission():
                return self._value_service.dispatch(method, arguments)
        return self._value_service.dispatch(method, arguments)

    def _python_variables(
        self, args: Mapping[str, object], caller: str
    ) -> tuple[ProxyDescriptor, ...]:
        self._only(args, {"filters"})
        filters = args.get("filters", {})
        if not isinstance(filters, Mapping) or filters:
            raise ValueError("Python variable filters are not implemented")
        return self._workspace_variables({"namespace": "python"}, caller)

    def _python_run(self, args: Mapping[str, object], caller: str) -> object:
        del caller
        self._only(args, {"code", "inputs", "outputs", "wait_s"})
        if "wait_s" in args:
            self._python_wait(args["wait_s"])
        return self._python().run(
            self._string(args, "code"),
            inputs=self._python_inputs(args.get("inputs", {})),
            outputs=self._string_sequence(args.get("outputs"), name="outputs"),
        )

    def _python_inspect(self, args: Mapping[str, object], caller: str) -> object:
        del caller
        self._only(args, {"proxy_id"})
        return self._python().inspect(self._string(args, "proxy_id"))

    def _python_imports(self, args: Mapping[str, object], caller: str) -> object:
        del caller
        self._only(args, set())
        return self._python().imports()

    def _python_reset(self, args: Mapping[str, object], caller: str) -> object:
        del caller
        self._only(args, {"policy"})
        if args.get("policy") != "clear":
            raise ValueError("Python reset requires clear policy")
        return self._python().reset()

    def _python_status(self, args: Mapping[str, object], caller: str) -> object:
        del caller
        self._only(args, set())
        return self._python().status()

    def _submit_revision(
        self,
        revision: CodeRevision,
        args: Mapping[str, object],
        caller: str,
        *,
        operation_kind: AgentOperationKind = AgentOperationKind.CODE_RUN,
    ) -> object:
        observation = (
            None
            if args.get("observe") is None
            else ObservationPlan.from_wire(args["observe"])
        )
        if revision.mode is not CodeMode.MAIN:
            raise UnsupportedOperation()
        if revision.language is CodeLanguage.PYTHON:
            if "outputs" not in args and not revision.outputs:
                raise ValueError("Python execution requires explicit outputs")
            if "wait_s" in args:
                self._python_wait(args["wait_s"])
            outputs = self._string_sequence(
                args.get("outputs", revision.outputs), name="outputs"
            )
            python_inputs = self._python_inputs(args.get("inputs", {}))
            gate = Event()
            operation_id: list[str] = []
            inputs_sha256 = hashlib.sha256(
                json.dumps(
                    {
                        "inputs": {
                            name: descriptor.proxy_id
                            for name, descriptor in python_inputs.items()
                        },
                        "outputs": outputs,
                        "observe": (
                            None if observation is None else to_wire(observation)
                        ),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            operation = self._operations.submit(
                {
                    "operation_kind": operation_kind.value,
                    "runtime_id": "python-workspace",
                    "runtime_generation": self._python().generation,
                    "code_id": revision.cell_id,
                    "revision": revision.revision,
                    "source_sha256": revision.source_sha256,
                    "inputs_sha256": inputs_sha256,
                    "observation": (
                        None if observation is None else to_wire(observation)
                    ),
                },
                lambda: self._execute_python_operation(
                    revision,
                    python_inputs,
                    outputs,
                    observation,
                    self._startup_operation_id(gate, operation_id),
                ),
            )
            operation_id.append(operation.operation_id)
            gate.set()
            return (
                operation
                if "wait_s" not in args
                else self._operations.wait(
                    operation.operation_id,
                    self._number(args, "wait_s"),
                    waiter_id=caller,
                )
            )
        if revision.language is not CodeLanguage.BSL:
            raise UnsupportedOperation()
        inputs = args.get("inputs")
        if not isinstance(inputs, Mapping) or inputs:
            raise UnsupportedOperation()
        with self._lock:
            runtime = self._require_usable_runtime(caller, None)
            if _MODE_ORDER[runtime.mode] < _MODE_ORDER[CapabilityMode.EXPERIMENT]:
                raise CapabilityDenied()
            gate = Event()
            operation_id: list[str] = []
            observation_wire = None if observation is None else to_wire(observation)
            inputs_sha256 = hashlib.sha256(
                json.dumps(
                    {"inputs": {}, "observe": observation_wire},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            operation = self._operations.submit(
                {
                    "operation_kind": operation_kind.value,
                    "runtime_id": runtime.runtime_id,
                    "runtime_generation": runtime.generation,
                    "code_id": revision.cell_id,
                    "revision": revision.revision,
                    "source_sha256": revision.source_sha256,
                    "inputs_sha256": inputs_sha256,
                    "observation": observation_wire,
                },
                lambda: self._execute_bsl_with_observation(
                    runtime,
                    revision,
                    observation,
                    self._startup_operation_id(gate, operation_id),
                ),
            )
            operation_id.append(operation.operation_id)
            gate.set()
        terminal = operation if "wait_s" not in args else self._operations.wait(operation.operation_id, self._number(args, "wait_s"), waiter_id=caller)
        return terminal

    def _execute_python_operation(
        self,
        revision: CodeRevision,
        inputs: Mapping[str, ProxyDescriptor],
        outputs: tuple[str, ...],
        observation: ObservationPlan | None,
        operation_id: str,
    ) -> BackendExecution:
        result = self._python().run(
            revision.source,
            inputs=inputs,
            outputs=outputs,
            cell_id=revision.cell_id,
            revision=revision.revision,
            source_sha256=revision.source_sha256,
        )
        exact_outputs: dict[str, ProxyDescriptor] = {}
        post_failure: Mapping[str, object] | None = None
        try:
            exact_outputs = {
                name: self._proxy_registry.snapshot_exact(descriptor.proxy_id)
                for name, descriptor in result.outputs.items()
            }
        except BaseException:
            post_failure = {
                "stage": "python_output_publication",
                "partial_results": {},
            }
        observed = OperationViewFacts()
        if result.succeeded and observation is not None:
            try:
                observed = self._observe_after_execution(observation)
            except BaseException:
                observed = OperationViewFacts(
                    failure={"stage": "observation", "partial_results": {}}
                )
        view_outputs = {**exact_outputs, **dict(observed.outputs)}
        failure = (
            {
                "stage": "python_execution",
                "partial_results": {},
            }
            if not result.succeeded
            else post_failure or observed.failure
        )
        try:
            self._operations.set_view_facts(
                operation_id,
                OperationViewFacts(
                    changed_variables=tuple(exact_outputs.values()),
                    change_confidence=MutationConfidence.EXACT,
                    outputs=view_outputs,
                    failure=failure,
                ),
            )
        except BaseException:
            evidence_message = "operation view evidence unavailable"
        else:
            evidence_message = ""
        messages = tuple(
            item[:MAX_MESSAGE_LENGTH]
            for item in (result.stdout, result.stderr, evidence_message)
            if item
        )[:MAX_OPERATION_MESSAGES]
        return BackendExecution(
            AgentOperationState.COMPLETED
            if result.succeeded
            else AgentOperationState.FAILED,
            messages,
            bool(view_outputs),
            "ready" if result.succeeded else "failed",
        )

    def _execute_bsl_with_observation(
        self,
        runtime: _AdmittedRuntime,
        revision: CodeRevision,
        observation: ObservationPlan | None,
        operation_id: str,
    ) -> BackendExecution:
        try:
            before_names = {
                name.casefold() for name in runtime.backend.namespace_snapshot().names
            }
        except BaseException:
            before_names = None
        raw_outcome = runtime.backend.execute_bsl_with_provenance(
            revision.source,
            source_unit=self._source_unit_for_revision(revision),
            on_execution_provenance=lambda provenance: (
                self._operations.set_execution_provenance(
                    operation_id,
                    provenance,
                )
            ),
        )
        outcome = self._sanitize_backend_execution(raw_outcome)
        if outcome.terminal_state is not AgentOperationState.COMPLETED:
            if outcome.terminal_state is AgentOperationState.FAILED:
                self._operations.set_view_facts(
                    operation_id,
                    OperationViewFacts(
                        failure=self._execution_failure_facts(
                            outcome,
                            operation_id=operation_id,
                            revision=revision,
                        )
                    ),
                )
            return outcome
        descriptor = OperationDescriptor(
            operation_id=operation_id,
            state=AgentOperationState.COMPLETED,
            runtime_id=runtime.runtime_id,
            runtime_generation=runtime.generation,
            cell_id=revision.cell_id,
            revision=revision.revision,
            source_sha256=revision.source_sha256,
            result_present=outcome.result_present,
        )
        changed_variables: tuple[ProxyDescriptor, ...] = ()
        change_confidence = MutationConfidence.UNKNOWN
        post_failure: Mapping[str, object] | None = None
        try:
            after = runtime.backend.namespace_snapshot()
            after_by_key = {name.casefold(): name for name in after.names}
            reported = {
                name.casefold(): name for name in outcome.changed_roots
            }
            if before_names is None and not reported:
                raise RuntimeError("namespace delta is unavailable")
            changed_by_key = dict(reported)
            if before_names is not None:
                changed_by_key.update(
                    {
                        name.casefold(): name
                        for name in after.names
                        if name.casefold() not in before_names
                    }
                )
            if any(key not in after_by_key for key in changed_by_key):
                raise RuntimeError("reported namespace delta is inconsistent")
            changed_names = tuple(
                after_by_key[key] for key in changed_by_key
            )
            if changed_names:
                change_confidence = (
                    MutationConfidence.DECLARED
                    if reported
                    else MutationConfidence.EXACT
                )
            changed_variables = tuple(
                self._proxy_registry.snapshot_exact(item.proxy_id)
                for item in publish_onec_bindings(
                    runtime.backend,
                    self._proxy_registry,
                    descriptor,
                    names=changed_names,
                )
            )
        except BaseException:
            post_failure = {
                "stage": "namespace_publication",
                "partial_results": {},
            }
        try:
            observed = (
                OperationViewFacts()
                if observation is None
                else self._observe_after_execution(observation)
            )
        except BaseException:
            observed = OperationViewFacts(
                failure={"stage": "observation", "partial_results": {}}
            )
        failure = post_failure or observed.failure
        try:
            self._operations.set_view_facts(
                operation_id,
                OperationViewFacts(
                    changed_variables=changed_variables,
                    change_confidence=change_confidence,
                    outputs=observed.outputs,
                    capture=observed.capture,
                    failure=failure,
                    recovery=observed.recovery,
                ),
            )
        except BaseException:
            messages = (
                *outcome.messages[: MAX_OPERATION_MESSAGES - 1],
                "operation view evidence unavailable",
            )
            return replace(outcome, messages=messages)
        return outcome

    @_serialized_capture_execution
    def _execute_capture_run_until(
        self,
        runtime: _AdmittedRuntime,
        revision: CodeRevision,
        points: tuple[CapturePointRequest, ...],
        operation_id: str,
    ) -> BackendExecution:
        try:
            prepared_main = runtime.backend.prepare_main_for_capture(
                revision.source,
                source_unit=self._source_unit_for_revision(revision),
            )
        except MainPreparationSourceError as error:
            outcome = BackendExecution(
                AgentOperationState.FAILED,
                (error.diagnostic.runtime_summary,),
                False,
                "ready",
                failure_stage=error.stage,
                diagnostic=error.diagnostic,
                state_changed=StateChanged.NO,
            )
            self._operations.set_view_facts(
                operation_id,
                OperationViewFacts(
                    failure=self._execution_failure_facts(
                        outcome,
                        operation_id=operation_id,
                        revision=revision,
                    )
                ),
            )
            return outcome
        except BaseException:
            return self._uncertain_capture_setup_failure(
                runtime, operation_id, stage="capture_preparation"
            )
        try:
            self._operations.set_execution_provenance(
                operation_id,
                runtime.backend.prepared_main_execution_provenance(
                    prepared_main
                ),
            )
        except BaseException:
            return self._uncertain_capture_setup_failure(
                runtime,
                operation_id,
                stage="capture_preparation",
            )
        try:
            prepared_main = runtime.backend.activate_prepared_main_for_capture(
                prepared_main
            )
        except BaseException:
            return self._uncertain_capture_setup_failure(
                runtime, operation_id, stage="capture_activation"
            )
        try:
            result = self._capture.run_until(
                runtime.backend,  # type: ignore[arg-type]
                operation_id=operation_id,
                prepared_main=prepared_main,
                source_revision=revision.revision,
                source_sha256=revision.source_sha256,
                points=points,
            )
        except BaseException:
            try:
                runtime.backend.discard_prepared_main_for_capture(prepared_main)
            except BaseException:
                pass
            return self._uncertain_capture_setup_failure(
                runtime,
                operation_id,
                stage="capture_setup_after_activation",
            )
        if result.user_main_dispatched is not True:
            try:
                runtime.backend.discard_prepared_main_for_capture(prepared_main)
            except BaseException:
                pass
            return self._uncertain_capture_setup_failure(
                runtime,
                operation_id,
                stage="capture_setup_after_activation",
            )
        safe_execution = self._sanitize_backend_execution(result.execution)
        if safe_execution is not result.execution:
            result = replace(result, execution=safe_execution)
        failure = result.failure
        if failure is None and result.execution.terminal_state is AgentOperationState.FAILED:
            failure = self._execution_failure_facts(
                result.execution,
                operation_id=operation_id,
                revision=revision,
            )
        self._operations.set_view_facts(
            operation_id,
            OperationViewFacts(
                capture=result.capture,
                failure=failure,
                recovery=result.recovery,
            ),
        )
        if result.capture is not None:
            self._capture.activate_capture_view(result.capture, self._proxy_registry)
            listener = getattr(runtime.backend, "add_capture_resume_listener", None)
            if callable(listener):
                listener(lambda resumed: self._capture.invalidate_if_active(resumed, self._proxy_registry))
        return result.execution

    def _uncertain_capture_setup_failure(
        self,
        runtime: _AdmittedRuntime,
        operation_id: str,
        *,
        stage: str,
    ) -> BackendExecution:
        runtime.closing = True
        self._proxy_registry.invalidate_runtime_generation(
            runtime.runtime_id,
            runtime.generation,
        )
        recovery = (
            RecoveryAction("workspace.status", {}),
            RecoveryAction(
                "runtime.close",
                {"policy": "abort_generation"},
            ),
            RecoveryAction(
                "runtime.ensure",
                {"mode": runtime.mode.value, "profile": runtime.profile},
            ),
        )
        self._operations.set_view_facts(
            operation_id,
            OperationViewFacts(
                failure={
                    "stage": stage,
                    "partial_results": {},
                    "state_changed": StateChanged.UNKNOWN.value,
                },
                recovery=recovery,
            ),
        )
        return BackendExecution(
            AgentOperationState.UNKNOWN,
            (),
            False,
            "unknown",
            state_changed=StateChanged.UNKNOWN,
        )

    @_serialized_capture_execution
    def _execute_capture_continue(
        self,
        runtime: _AdmittedRuntime,
        fence: CaptureFence,
        points: tuple[CapturePointRequest, ...],
        observation: ObservationPlan | None,
        operation_id: str,
    ) -> BackendExecution:
        result = self._capture.continue_capture(
            runtime.backend,  # type: ignore[arg-type]
            self._proxy_registry,
            fence=fence,
            operation_id=operation_id,
            next_points=points,
        )
        if result.quarantine_runtime:
            runtime.closing = True
            self._proxy_registry.invalidate_runtime_generation(
                runtime.runtime_id, runtime.generation
            )
        capture = result.capture
        outputs: Mapping[str, ProxyDescriptor] = {}
        failure = result.failure
        if capture is not None:
            self._capture.activate_capture_view(capture, self._proxy_registry)
            listener = getattr(runtime.backend, "add_capture_resume_listener", None)
            if callable(listener):
                listener(lambda resumed: self._capture.invalidate_if_active(resumed, self._proxy_registry))
            if observation is not None:
                try:
                    observed = self._observe_capture_after_execution(runtime, capture.fence, observation)
                    outputs = observed.outputs
                    failure = failure or observed.failure
                except BaseException:
                    failure = failure or {"stage": "observation", "partial_results": {}}
        elif result.execution.terminal_state is AgentOperationState.FAILED and failure is None:
            failure = {"stage": result.execution.failure_stage or "execution", "partial_results": {}}
        self._operations.set_view_facts(
            operation_id,
            OperationViewFacts(capture=capture, outputs=outputs, failure=failure, recovery=result.recovery),
        )
        return result.execution

    @_serialized_capture_execution
    def _execute_capture_hypothesis(
        self,
        runtime: _AdmittedRuntime,
        revision: CodeRevision,
        fence: CaptureFence,
        observation: ObservationPlan | None,
        operation_id: str,
    ) -> BackendExecution:
        """Normalize one paused capture cell to a CAPTURED operation view.

        Runtime compile/execution errors intentionally do not unwind the
        captured controller context.  They are evidence about a hypothesis,
        not permission to send Continue or synthesize rollback code.
        """
        self._capture.current_capture(fence)
        try:
            prepared = runtime.backend.prepare_capture_hypothesis(
                revision.source,
                fence,
            )
        except CaptureHypothesisPreparationError as error:
            outcome = BackendExecution(
                AgentOperationState.FAILED,
                (error.diagnostic.runtime_summary,),
                False,
                "captured",
                failure_stage=error.stage,
                diagnostic=error.diagnostic,
                state_changed=StateChanged.NO,
            )
            self._operations.set_view_facts(
                operation_id,
                OperationViewFacts(
                    failure=self._execution_failure_facts(
                        outcome,
                        operation_id=operation_id,
                        revision=revision,
                    ),
                ),
            )
            return outcome
        except BaseException:
            self._quarantine_capture_preparation(runtime, fence)
            recovery = (
                RecoveryAction("workspace.status", {}),
                RecoveryAction(
                    "runtime.close",
                    {"policy": "abort_generation"},
                ),
                RecoveryAction(
                    "runtime.ensure",
                    {"mode": runtime.mode.value, "profile": runtime.profile},
                ),
            )
            self._operations.set_view_facts(
                operation_id,
                OperationViewFacts(
                    failure={
                        "stage": "capture_preparation",
                        "partial_results": {},
                        "state_changed": StateChanged.UNKNOWN.value,
                    },
                    recovery=recovery,
                ),
            )
            return BackendExecution(
                AgentOperationState.UNKNOWN,
                (),
                False,
                "unknown",
            )
        try:
            self._operations.set_execution_provenance(
                operation_id,
                runtime.backend.prepared_capture_hypothesis_provenance(
                    prepared
                ),
            )
        except BaseException:
            self._quarantine_capture_preparation(runtime, fence)
            self._operations.set_view_facts(
                operation_id,
                OperationViewFacts(
                    failure={
                        "stage": "capture_preparation",
                        "partial_results": {},
                        "state_changed": StateChanged.UNKNOWN.value,
                    },
                ),
            )
            return BackendExecution(
                AgentOperationState.UNKNOWN,
                (),
                False,
                "unknown",
            )
        try:
            capture = self._capture.current_capture(fence)
        except BaseException:
            self._quarantine_capture_preparation(runtime, fence)
            self._operations.set_view_facts(
                operation_id,
                OperationViewFacts(
                    failure={
                        "stage": "capture_preparation",
                        "partial_results": {},
                        "state_changed": StateChanged.UNKNOWN.value,
                    },
                ),
            )
            return BackendExecution(
                AgentOperationState.UNKNOWN,
                (),
                False,
                "unknown",
            )
        try:
            outcome = self._sanitize_backend_execution(
                runtime.backend.execute_capture_hypothesis(prepared, fence)
            )
        except BaseException:
            outcome = BackendExecution(
                AgentOperationState.UNKNOWN,
                (),
                False,
                "unknown",
            )
        failure: Mapping[str, object] | None = None
        outputs = {}
        changed_variables: tuple[ProxyDescriptor, ...] = ()
        confidence = MutationConfidence.UNKNOWN
        paused_success = (
            outcome.terminal_state is AgentOperationState.CAPTURED
            and outcome.failure_stage is None
            and outcome.runtime_state == "captured"
        )
        paused_execution_failure = (
            outcome.terminal_state is AgentOperationState.FAILED
            and outcome.failure_stage == "execution"
            and outcome.runtime_state == "captured"
        )
        if paused_success:
            try:
                capture = self._capture.stage_dirty_roots(
                    fence, outcome.capture_dirty_roots
                )
            except BaseException:
                # The backend has already proved the controller remains
                # paused.  Retain that capture even if local staging evidence
                # could not be persisted; do not invent rollback/Continue.
                capture = self._capture.current_capture(fence)
                failure = {"stage": "capture_staging", "partial_results": {}}
            if failure is not None:
                self._operations.set_view_facts(
                    operation_id,
                    OperationViewFacts(capture=capture, failure=failure),
                )
                return BackendExecution(
                    AgentOperationState.CAPTURED, outcome.messages[:MAX_OPERATION_MESSAGES],
                    outcome.result_present, "captured", outcome.changed_roots,
                )
            try:
                changed_variables = self._publish_capture_persistent_changes(
                    runtime, revision, operation_id, outcome.changed_roots
                )
                confidence = (
                    MutationConfidence.DECLARED if changed_variables else MutationConfidence.UNKNOWN
                )
            except BaseException:
                failure = {"stage": "namespace_publication", "partial_results": {}}
            if observation is not None:
                try:
                    observed = self._observe_capture_after_execution(
                        runtime, fence, observation
                    )
                except BaseException:
                    observed = OperationViewFacts(
                        failure={"stage": "observation", "partial_results": {}}
                    )
                outputs = dict(observed.outputs)
                failure = failure or observed.failure
        elif paused_execution_failure:
            # A captured hypothesis executor must leave the original debugger
            # stop in place even when BSL compile/execute reports an error.
            try:
                capture = self._capture.stage_dirty_roots(
                    fence,
                    outcome.capture_dirty_roots,
                )
            except BaseException:
                capture = self._capture.current_capture(fence)
            failure = self._execution_failure_facts(
                outcome,
                operation_id=operation_id,
                revision=revision,
            )
        else:
            # There is no proven paused state, so retaining stale frame
            # handles would make later observations lie about the runtime.
            self._capture.invalidate_capture(self._proxy_registry)
            self._operations.set_view_facts(
                operation_id,
                OperationViewFacts(
                    failure={"stage": "capture_hypothesis_transport", "partial_results": {}},
                    recovery=(
                        RecoveryAction("workspace.status", {}),
                        RecoveryAction(
                            "operation.wait",
                            {
                                "operation_id": operation_id,
                                "timeout_s": 0,
                                "after_event_cursor": 0,
                                "after_message_cursor": 0,
                            },
                        ),
                        RecoveryAction(
                            "runtime.close", {"policy": "abort_generation"}
                        ),
                    ),
                ),
            )
            return outcome
        self._operations.set_view_facts(
            operation_id,
            OperationViewFacts(
                changed_variables=changed_variables,
                change_confidence=confidence,
                outputs=outputs,
                capture=capture,
                failure=failure,
            ),
        )
        return BackendExecution(
            AgentOperationState.CAPTURED,
            outcome.messages[:MAX_OPERATION_MESSAGES],
            outcome.result_present or bool(outputs),
            "captured",
            outcome.changed_roots,
            diagnostic=outcome.diagnostic,
            state_changed=outcome.state_changed,
        )

    def _publish_capture_persistent_changes(
        self,
        runtime: _AdmittedRuntime,
        revision: CodeRevision,
        operation_id: str,
        changed_roots: tuple[str, ...],
    ) -> tuple[ProxyDescriptor, ...]:
        """Publish only runtime-declared notebook writes, never frame roots."""
        if not changed_roots:
            return ()
        names = {name.casefold(): name for name in runtime.backend.namespace_snapshot().names}
        requested = tuple(names[root.casefold()] for root in changed_roots if root.casefold() in names)
        if len(requested) != len({root.casefold() for root in changed_roots}):
            raise RuntimeError("capture persistent write is not in namespace")
        descriptor = OperationDescriptor(
            operation_id=operation_id,
            state=AgentOperationState.CAPTURED,
            runtime_id=runtime.runtime_id,
            runtime_generation=runtime.generation,
            cell_id=revision.cell_id,
            revision=revision.revision,
            source_sha256=revision.source_sha256,
            result_present=False,
        )
        return tuple(
            self._proxy_registry.snapshot_exact(item.proxy_id)
            for item in publish_onec_bindings(
                runtime.backend, self._proxy_registry, descriptor, names=requested
            )
        )

    @_serialized_capture_execution
    def _observe_capture_after_execution(
        self,
        runtime: _AdmittedRuntime,
        fence: CaptureFence,
        plan: ObservationPlan,
    ) -> OperationViewFacts:
        """Resolve frame/table handles, then use the shared bounded pipeline."""
        originals = {item.alias: item for item in plan.items}
        resolved_requests: dict[str, ObservationItem] = {}
        effective_items = tuple(
            replace(item, select=None)
            if item.source.kind is ObservationSourceKind.TEMPORARY_TABLE and item.select is not None
            else item
            for item in plan.items
        )

        def prepare_item(  # type: ignore[no-untyped-def]
            item,
            requested,
            remaining_items,
            remaining_rows,
            timeout_s,
        ):
            if item.source.kind is not ObservationSourceKind.TEMPORARY_TABLE:
                resolved_requests[item.alias] = requested
                return item, requested
            if requested.select is not None:
                resolved_requests[item.alias] = requested
                return item, requested
            if item.result in {
                ObservationResult.PYTHON,
                ObservationResult.DATAFRAME,
            }:
                # A metadata inventory proxy is deliberately not a full-table
                # capability.  Full Python/DataFrame transfer therefore needs
                # a caller-supplied finite row selector.
                raise UnsupportedOperation()
            if item.result is not ObservationResult.PREVIEW:
                resolved_requests[item.alias] = requested
                return item, requested

            metadata_item = replace(requested, result=ObservationResult.PROXY)
            inspection = self._capture.inspect(
                runtime.backend,
                self._proxy_registry,
                fence=fence,
                runtime_id=runtime.runtime_id,
                runtime_generation=runtime.generation,
                context_generation=runtime.generation,
                filters={},
                cursor=0,
                limit=20,
                observe=ObservationPlan((metadata_item,), plan.budget_profile),
                timeout_s=timeout_s,
            )
            descriptor = inspection.temporary_tables[0]
            if not descriptor.schema or remaining_rows <= 0:
                raise ValueError("temporary-table preview lacks a bounded schema")
            columns = tuple(descriptor.schema[:remaining_items])
            if not columns:
                raise ValueError("temporary-table preview column budget is exhausted")
            synthesized = ValueSelection(
                SelectionKind.TABLE_ROWS,
                offset=0,
                limit=min(remaining_rows, 100),
                columns=columns,
            )
            requested = replace(requested, select=synthesized)
            resolved_requests[item.alias] = requested
            return replace(item, select=None), requested

        def resolve(item, timeout_s):  # type: ignore[no-untyped-def]
            requested = resolved_requests.get(item.alias, originals[item.alias])
            if item.source.kind is ObservationSourceKind.CONTEXT_BINDING:
                return self._proxy_registry.resolve_name(item.source.name or "")
            if item.source.kind is ObservationSourceKind.FRAME_LOCAL:
                inspection = self._capture.inspect(
                    runtime.backend, self._proxy_registry, fence=fence,
                    runtime_id=runtime.runtime_id, runtime_generation=runtime.generation,
                    context_generation=runtime.generation,
                    filters={"name": item.source.name or ""}, cursor=0, limit=20,
                    timeout_s=timeout_s,
                )
                descriptor = next(
                    variable for variable in inspection.variables
                    if variable.name.casefold() == (item.source.name or "").casefold()
                )
                return self._proxy_registry.snapshot_exact(descriptor.proxy_id)
            if item.source.kind is ObservationSourceKind.TEMPORARY_TABLE:
                metadata_item = replace(
                    requested, result=ObservationResult.PROXY
                )
                inspection = self._capture.inspect(
                    runtime.backend, self._proxy_registry, fence=fence,
                    runtime_id=runtime.runtime_id, runtime_generation=runtime.generation,
                    context_generation=runtime.generation, filters={}, cursor=0, limit=20,
                    observe=ObservationPlan((metadata_item,), plan.budget_profile),
                    timeout_s=timeout_s,
                )
                return self._proxy_registry.snapshot_exact(inspection.temporary_tables[0].table_id)
            if item.source.kind is ObservationSourceKind.TEMPORARY_TABLE_MANAGER:
                metadata_item = replace(item, result=ObservationResult.PROXY)
                inspection = self._capture.inspect(
                    runtime.backend, self._proxy_registry, fence=fence,
                    runtime_id=runtime.runtime_id, runtime_generation=runtime.generation,
                    context_generation=runtime.generation, filters={}, cursor=0, limit=20,
                    observe=ObservationPlan((metadata_item,), plan.budget_profile),
                    timeout_s=timeout_s,
                )
                manager = inspection.temporary_table_managers[0]
                manager_handle = self._capture.manager_handle(
                    manager.manager_id,
                    fence=fence,
                )
                runtime.backend.validate_value_reference(manager_handle)
                proxy = self._proxy_registry.register_frame(
                    qualified_name=f"capture.{fence.capture_intent_id}.{manager.manager_id}",
                    type_name="МенеджерВременныхТаблиц", runtime_id=runtime.runtime_id,
                    runtime_generation=runtime.generation, context_generation=runtime.generation,
                    capture_fence=fence,
                    provenance=ProxyProvenance(
                        "capture", fence.source_revision, fence.source_sha256, fence.operation_id
                    ),
                    resolver_handle=manager_handle,
                    capabilities=("describe",),
                )
                return self._proxy_registry.snapshot_exact(proxy.proxy_id)
            raise UnsupportedOperation()

        return self._observe_after_execution(
            ObservationPlan(effective_items, plan.budget_profile),
            resolve_source=resolve,
            accounting_plan=plan,
            prepare_item=prepare_item,
        )

    @staticmethod
    def _source_unit_for_revision(revision: CodeRevision) -> SourceUnitRef:
        return SourceUnitRef(
            SourceUnitKind.NOTEBOOK_CELL,
            revision.cell_id,
            revision.revision,
            revision.source_sha256,
        )

    @staticmethod
    def _prepare_bsl_source(
        source: str,
        *,
        source_unit: SourceUnitRef | None = None,
        mode: LoweringMode = LoweringMode.MAIN,
    ) -> NormalizedDiagnostic | None:
        """Prepare one exact revision without touching the live 1C target."""
        unit = source_unit or SourceUnitRef(
            SourceUnitKind.NOTEBOOK_CELL,
            "anonymous-notebook-cell",
            0,
            source_sha256(source),
        )
        visible = mapped_visible_source(source, unit)
        context = VisibleSourceContext({unit: source})
        target = PythonParserTarget.from_generated()
        try:
            cell = split_notebook_cell(target, source, source_unit=unit)
        except (BslParseError, BslLexError) as error:
            return normalize_source_error(
                error=error,
                source=visible,
                stage=DiagnosticStage.PARSING,
                visible_source_context=context,
            )
        if cell.statements is None:
            return None
        try:
            SemanticNotebookLowerer(target).lower_mapped(
                cell.statements,
                mode=mode,
            )
        except (BslParseError, BslLexError) as error:
            return normalize_source_error(
                error=error,
                source=cell.statements,
                stage=DiagnosticStage.PARSING,
                visible_source_context=context,
            )
        except SemanticLoweringError as error:
            return normalize_source_error(
                error=error,
                source=cell.statements,
                stage=DiagnosticStage.LOWERING,
                visible_source_context=context,
            )
        return None

    def _execution_failure_facts(
        self,
        outcome: BackendExecution,
        *,
        operation_id: str | None = None,
        revision: CodeRevision | None = None,
    ) -> Mapping[str, object]:
        failure: dict[str, object] = {
            "stage": outcome.failure_stage or "execution",
            "partial_results": {},
            "state_changed": outcome.state_changed.value,
        }
        if outcome.diagnostic is not None:
            diagnostic = self._visible_diagnostic_facts(outcome.diagnostic)
            if diagnostic is not None:
                failure["diagnostic"] = diagnostic
                if operation_id is not None and revision is not None:
                    try:
                        self._operations.record_diagnostic(
                            outcome.diagnostic,
                            excerpt=self._diagnostic_excerpt(
                                outcome.diagnostic,
                                revision,
                            ),
                            operation_id=operation_id,
                        )
                    except BaseException:
                        # Public compact evidence is intentionally independent
                        # of missing/corrupt private expert detail.
                        pass
        return failure

    @staticmethod
    def _visible_diagnostic_facts(
        diagnostic: NormalizedDiagnostic,
    ) -> Mapping[str, object] | None:
        """Project only normalized visible coordinates into public operation facts."""
        safe = sanitize_normalized_diagnostic(diagnostic)
        if safe is None:
            return None
        visible_location = None
        if safe.visible_location is not None:
            location = safe.visible_location
            visible_location = {
                "line": location.line,
                "column": location.column,
                "span": {
                    "start": location.span.start,
                    "end": location.span.end,
                },
            }
        related_visible_span = None
        if safe.related_visible_span is not None:
            related_visible_span = {
                "start": safe.related_visible_span.start,
                "end": safe.related_visible_span.end,
            }
        return to_wire(
            AgentDiagnosticView(
                diagnostic_id=safe.diagnostic_id,
                stage=safe.stage.value,
                mapping_confidence=safe.mapping_confidence.value,
                visible_location=visible_location,
                related_visible_span=related_visible_span,
                excerpt=None,
                synthetic_region=safe.synthetic_region,
            )
        )  # type: ignore[return-value]

    @classmethod
    def _diagnostic_excerpt(
        cls,
        diagnostic: NormalizedDiagnostic,
        revision: CodeRevision,
    ) -> str | None:
        safe = sanitize_normalized_diagnostic(diagnostic)
        if safe is None or safe.visible_location is None or safe.source_unit is None:
            return None
        if (
            hashlib.sha256(revision.source.encode("utf-8")).hexdigest()
            != revision.source_sha256
            or safe.source_unit != cls._source_unit_for_revision(revision)
        ):
            return None
        span = safe.visible_location.span
        if (
            span.end > len(revision.source)
            or span.end - span.start > MAX_DIAGNOSTIC_EXCERPT_LENGTH
        ):
            return None
        return revision.source[span.start : span.end]

    @staticmethod
    def _sanitize_backend_execution(outcome: object) -> BackendExecution:
        """Fail closed if an injected backend crosses the diagnostic boundary."""
        if not isinstance(outcome, BackendExecution):
            return BackendExecution(
                AgentOperationState.UNKNOWN, (), False, "unknown"
            )
        if outcome.diagnostic is None:
            return outcome
        diagnostic = sanitize_normalized_diagnostic(outcome.diagnostic)
        if diagnostic is None:
            return BackendExecution(
                AgentOperationState.UNKNOWN,
                (),
                False,
                "unknown",
                state_changed=StateChanged.UNKNOWN,
            )
        messages = outcome.messages
        if (
            outcome.failure_stage in {"parsing", "lowering"}
            and outcome.state_changed is StateChanged.NO
        ):
            messages = (diagnostic.runtime_summary,)
        return replace(outcome, diagnostic=diagnostic, messages=messages)

    def _observe_after_execution(
        self,
        plan: ObservationPlan,
        *,
        resolve_source=None,  # type: ignore[no-untyped-def]
        accounting_plan: ObservationPlan | None = None,
        prepare_item=None,  # type: ignore[no-untyped-def]
    ) -> OperationViewFacts:
        outputs: dict[str, ProxyDescriptor] = {}
        unavailable: dict[str, str] = {}
        budget = ValueBudgetProfiles().resolve(plan.budget_profile)
        profiles = ValueBudgetProfiles()
        charged_items = 0
        charged_rows = 0
        charged_bytes = 0
        requested_items = plan.items if accounting_plan is None else accounting_plan.items
        if (
            len(requested_items) != len(plan.items)
            or any(
                requested.alias.casefold() != effective.alias.casefold()
                for requested, effective in zip(requested_items, plan.items, strict=True)
            )
        ):
            raise ValueError("observation accounting plan does not match execution plan")
        deadline = monotonic() + budget.timeout_seconds
        for index, (item, requested) in enumerate(
            zip(plan.items, requested_items, strict=True)
        ):
            try:
                remaining_items = budget.max_items - charged_items
                remaining_rows = budget.max_rows - charged_rows
                remaining_bytes = budget.max_bytes - charged_bytes
                remaining_observations = len(plan.items) - index
                remaining_time = deadline - monotonic()
                if (
                    remaining_items <= 0
                    or remaining_time <= 0
                ):
                    raise ValueError("observation aggregate budget exhausted")
                item_deadline = min(
                    deadline,
                    monotonic() + remaining_time / remaining_observations,
                )
                if prepare_item is not None:
                    item, requested = prepare_item(
                        item,
                        requested,
                        remaining_items,
                        remaining_rows,
                        self._observation_remaining(item_deadline),
                    )
                self._observation_remaining(item_deadline)

                selection = requested.select
                bounded_scan = selection is not None
                full_scan = item.result in {
                    ObservationResult.PYTHON,
                    ObservationResult.DATAFRAME,
                }
                preview_scan = item.result is ObservationResult.PREVIEW
                if bounded_scan and not profiles.permits(
                    plan.budget_profile, ValueCostClass.BOUNDED_SCAN
                ):
                    raise ValueError(
                        "budget profile does not permit bounded projection"
                    )
                if full_scan and not profiles.permits(
                    plan.budget_profile, ValueCostClass.FULL_SCAN
                ):
                    raise ValueError(
                        "budget profile does not permit full materialization"
                    )

                item_charge = 1
                row_charge = 0
                transfer_items = 1
                transfer_rows = 1
                if selection is not None:
                    if selection.kind.value == "table_rows":
                        assert selection.limit is not None
                        row_charge = selection.offset + selection.limit
                        transfer_rows = selection.limit
                        item_charge = max(1, len(selection.columns))
                        transfer_items = item_charge
                    elif selection.kind.value == "slice":
                        assert selection.limit is not None
                        item_charge = selection.offset + selection.limit
                        transfer_items = selection.limit
                    else:
                        item_charge = len(selection.names)
                        transfer_items = item_charge
                    if len(selection.columns) > budget.max_items:
                        raise ValueError("observation column budget exceeded")
                elif preview_scan or full_scan:
                    transfer_items = max(1, remaining_items // remaining_observations)
                    transfer_rows = max(1, remaining_rows // remaining_observations)
                    item_charge = transfer_items
                    row_charge = transfer_rows

                if item_charge > budget.max_items or row_charge > budget.max_rows:
                    raise ValueError("observation per-item budget exceeded")
                if item_charge > remaining_items or row_charge > remaining_rows:
                    raise ValueError("observation aggregate budget exhausted")

                costly = bounded_scan or preview_scan or full_scan
                byte_charge = (
                    max(1, remaining_bytes // remaining_observations)
                    if costly
                    else 0
                )
                transfer_bytes = max(
                    1, byte_charge or remaining_bytes // remaining_observations
                )
                # Reserve before source resolution.  A native temporary-table
                # selection may consume its scan even if later publication or
                # materialization fails, so it must not be refunded or charged
                # again after its effective selector is cleared.
                charged_items += item_charge
                charged_rows += row_charge
                charged_bytes += byte_charge
                if resolve_source is None:
                    if item.source.kind is not ObservationSourceKind.CONTEXT_BINDING:
                        raise UnsupportedOperation()
                    proxy = self._proxy_registry.resolve_name(item.source.name or "")
                else:
                    proxy = resolve_source(
                        item, self._observation_remaining(item_deadline)
                    )
                # Source resolution may itself be a native table selection.
                # Never start transfer/decode after that command consumed the
                # item's deadline.
                self._observation_remaining(item_deadline)
                if (
                    proxy.realm is ProxyRealm.ONEC
                    and (
                        item.select is not None
                        or item.result
                        in {ObservationResult.PYTHON, ObservationResult.DATAFRAME}
                    )
                ):
                    # Python/bridge setup belongs to this item.  _python caches
                    # only a successful workspace, so earlier and later proxy
                    # observations remain independent of a setup failure.
                    self._python()
                    self._observation_remaining(item_deadline)
                if item.select is not None:
                    proxy = self._value_service.project(
                        proxy.proxy_id,
                        item.select,
                        budget=self._observation_budget(
                            budget,
                            item_deadline,
                            transfer_items,
                            transfer_rows,
                            transfer_bytes,
                        ),
                    )
                if item.result is ObservationResult.PREVIEW:
                    item_budget = self._observation_budget(
                        budget,
                        item_deadline,
                        transfer_items,
                        transfer_rows,
                        transfer_bytes,
                    )
                    inspection = self._value_service.inspect(
                        proxy.proxy_id,
                        detail="preview",
                        budget_profile=plan.budget_profile,
                        budget=item_budget,
                    )
                    proxy = replace(
                        proxy,
                        known_size=inspection.known_size,
                        bounded_preview=inspection.bounded_preview,
                    )
                elif item.result is ObservationResult.PYTHON:
                    item_budget = self._observation_budget(
                        budget,
                        item_deadline,
                        transfer_items,
                        transfer_rows,
                        transfer_bytes,
                    )
                    materialized = self._value_service.dispatch(
                        "value.materialize",
                        {
                            "proxy_id": proxy.proxy_id,
                            "target": "python",
                            "policy": {},
                            "budget": {
                                "depth": item_budget.max_depth,
                                "items": item_budget.max_items,
                                "rows": item_budget.max_rows,
                                "bytes": item_budget.max_bytes,
                                "timeout_s": item_budget.timeout_seconds,
                            },
                        },
                    )
                    if not isinstance(materialized, ProxyDescriptor):
                        raise TypeError("materialization did not return a proxy")
                    proxy = materialized
                elif item.result is ObservationResult.DATAFRAME:
                    item_budget = self._observation_budget(
                        budget,
                        item_deadline,
                        transfer_items,
                        transfer_rows,
                        transfer_bytes,
                    )
                    dataframe = self._value_service.dispatch(
                        "value.to_df",
                        {
                            "proxy_id": proxy.proxy_id,
                            "columns": None,
                            "refs": "presentation",
                            "budget": {
                                "depth": item_budget.max_depth,
                                "items": item_budget.max_items,
                                "rows": item_budget.max_rows,
                                "bytes": item_budget.max_bytes,
                                "timeout_s": item_budget.timeout_seconds,
                            },
                        },
                    )
                    if not isinstance(dataframe, ProxyDescriptor):
                        raise TypeError("DataFrame conversion did not return a proxy")
                    proxy = dataframe
                self._observation_remaining(item_deadline)
                exact = self._proxy_registry.snapshot_exact(proxy.proxy_id)
                outputs[item.alias] = replace(
                    exact,
                    known_size=proxy.known_size,
                    bounded_preview=proxy.bounded_preview,
                )
            except BaseException:
                unavailable[item.alias] = "unavailable"
        failure = (
            None
            if not unavailable
            else {"stage": "observation", "partial_results": unavailable}
        )
        return OperationViewFacts(outputs=outputs, failure=failure)

    @staticmethod
    def _observation_remaining(deadline: float) -> float:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("observation deadline exceeded")
        return remaining

    @classmethod
    def _observation_budget(
        cls,
        profile: ValueBudget,
        deadline: float,
        max_items: int,
        max_rows: int,
        max_bytes: int,
    ) -> ValueBudget:
        return ValueBudget(
            profile.max_depth,
            max_items,
            max_rows,
            max_bytes,
            cls._observation_remaining(deadline),
        )

    def _start(self, args: Mapping[str, object], caller: str) -> RuntimeDescriptor:
        mode = self._requested_mode(args)
        profile = self._runtime_profile(args)
        if _MODE_ORDER[mode] > _MODE_ORDER[self._maximum_mode]:
            raise CapabilityDenied()
        if self._runtime is not None:
            raise RuntimeConflict()
        if self._unknown_startup_cleanup is not None:
            self._reconcile_unknown_startup(None)
        try:
            backend = self._factory.start(mode=mode)
        except BaseException as error:
            retry_cleanup = getattr(error, "retry_cleanup", None)
            if callable(retry_cleanup):
                self._unknown_startup_cleanup = retry_cleanup
                raise OwnershipUncertain() from None
            raise
        try:
            descriptor = backend.status()
        except BaseException:
            try:
                backend.close()
            except BaseException:
                self._runtime = _AdmittedRuntime(
                    backend, backend.runtime_id, 1, mode, profile, closing=True
                )
                self._selected.clear()
                raise OwnershipUncertain()
            raise
        candidate = _AdmittedRuntime(
            backend,
            descriptor.runtime_id,
            descriptor.generation,
            descriptor.mode,
            profile,
        )
        if descriptor.mode is not mode or _MODE_ORDER[descriptor.mode] > _MODE_ORDER[self._maximum_mode]:
            candidate.closing = True
            try:
                backend.close()
            except BaseException as error:
                self._runtime = candidate
                self._selected.clear()
                raise OwnershipUncertain() from error
            raise CapabilityDenied()
        try:
            self._install_onec_resolver(backend)
            self._write_runtime_ownership(candidate, operation_id="runtime-start")
        except BaseException:
            candidate.closing = True
            try:
                backend.close()
            except BaseException as error:
                self._runtime = candidate
                self._selected.clear()
                raise OwnershipUncertain() from error
            self._proxy_registry.invalidate_runtime_generation(
                candidate.runtime_id,
                candidate.generation,
            )
            self._value_resolvers.pop(ProxyRealm.ONEC, None)
            self._clear_runtime_ownership()
            raise
        self._runtime = candidate
        self._selected[caller] = candidate.runtime_id
        return descriptor

    def _require_runtime(self, caller: str, requested: object, *, allow_unselected: bool = False) -> _AdmittedRuntime:
        if self._runtime is None:
            raise ValueError("no runtime selected")
        if requested is not None and requested != self._runtime.runtime_id:
            raise ValueError("runtime is not available")
        if not allow_unselected and self._selected.get(caller) != self._runtime.runtime_id:
            raise ValueError("runtime is not selected")
        return self._runtime

    def _require_usable_runtime(self, caller: str, requested: object, *, allow_unselected: bool = False) -> _AdmittedRuntime:
        runtime = self._require_runtime(caller, requested, allow_unselected=allow_unselected)
        if runtime.closing:
            raise OwnershipUncertain()
        return runtime

    def _require_close_authorized(self, caller: str) -> _AdmittedRuntime:
        if self._runtime is None:
            raise ValueError("no runtime selected")
        return self._require_runtime(caller, None, allow_unselected=self._runtime.closing)

    def _close_owned_runtime(self) -> tuple[OperationDescriptor, ...]:
        runtime = self._runtime
        if runtime is None:
            if self._unknown_startup_cleanup is not None:
                self._reconcile_unknown_startup(self._startup_request)
            return ()
        runtime.closing = True
        self._selected.clear()
        self._proxy_registry.invalidate_runtime_generation(
            runtime.runtime_id,
            runtime.generation,
        )
        self._capture.invalidate_capture(self._proxy_registry)
        try:
            changed = self._operations.mark_generation_aborted(runtime.runtime_id, runtime.generation)
        except BaseException as error:
            raise OwnershipUncertain() from error
        try:
            runtime.backend.close()
        except BaseException as error:
            raise OwnershipUncertain() from error
        self._clear_runtime_ownership()
        self._runtime = None
        self._value_resolvers.pop(ProxyRealm.ONEC, None)
        self._selected.clear()
        return changed

    def _reconcile_unknown_startup(
        self,
        startup: _StartupRequest | None,
    ) -> None:
        reconcile = getattr(self._factory, "reconcile_unknown_startup", None)
        facts: dict[str, object] = {}
        if startup is not None:
            facts = {
                "operation_id": startup.operation_id,
                "profile": startup.profile,
                "mode": startup.mode,
            }
        if callable(reconcile):
            reconciled = reconcile(**facts) is True
        else:
            retry_cleanup = self._unknown_startup_cleanup
            if retry_cleanup is None:
                reconciled = False
            else:
                try:
                    retry_cleanup()
                except BaseException:
                    reconciled = False
                else:
                    reconciled = True
        if not reconciled:
            raise OwnershipUncertain()
        self._unknown_startup_cleanup = None
        if startup is not None:
            self._operations.abandon_unknown_startup(startup.operation_id)
            if self._startup_request is startup:
                self._startup_request = None

    def _load_runtime_ownership(self) -> dict[str, object] | None:
        if not self._ownership_path.exists():
            return None
        try:
            payload = json.loads(self._ownership_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise OwnershipUncertain() from error
        text_fields = (
            "service_instance_id",
            "runtime_id",
            "profile",
            "mode",
            "operation_id",
        )
        processes = payload.get("processes")
        if (
            not isinstance(payload, dict)
            or set(payload) != {*text_fields, "generation", "processes"}
            or any(
                not isinstance(payload[name], str) or not payload[name]
                for name in text_fields
            )
            or type(payload["generation"]) is not int
            or payload["generation"] <= 0
            or not isinstance(processes, list)
            or any(
                not isinstance(item, dict)
                or set(item) != {"role", "pid", "create_time", "executable"}
                or not isinstance(item["role"], str)
                or type(item["pid"]) is not int
                or item["pid"] <= 0
                or isinstance(item["create_time"], bool)
                or not isinstance(item["create_time"], (int, float))
                or not isfinite(float(item["create_time"]))
                or float(item["create_time"]) <= 0
                or not isinstance(item["executable"], str)
                or not item["executable"]
                for item in processes
            )
        ):
            raise OwnershipUncertain()
        return payload

    def _write_runtime_ownership(
        self,
        runtime: _AdmittedRuntime,
        *,
        operation_id: str,
    ) -> None:
        snapshot = getattr(runtime.backend, "ownership_snapshot", None)
        processes = () if not callable(snapshot) else snapshot()
        if not isinstance(processes, tuple) or any(
            not isinstance(item, Mapping) for item in processes
        ):
            raise OwnershipUncertain()
        payload = {
            "service_instance_id": self.service_instance_id,
            "runtime_id": runtime.runtime_id,
            "generation": runtime.generation,
            "profile": runtime.profile,
            "mode": runtime.mode.value,
            "operation_id": operation_id,
            "processes": [dict(item) for item in processes],
        }
        self._state_root.mkdir(parents=True, exist_ok=True)
        temporary = self._ownership_path.with_name(
            f"{self._ownership_path.name}.{uuid4().hex}.tmp"
        )
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._ownership_path)
        finally:
            with suppress(FileNotFoundError):
                temporary.unlink()

    def _clear_runtime_ownership(self) -> None:
        with suppress(FileNotFoundError):
            self._ownership_path.unlink()

    def _python(self) -> PythonWorkspace:
        with self._lock:
            if self._closed:
                raise RuntimeError("agent service is closed")
            if self._python_workspace is None:
                workspace = PythonWorkspace.start(
                    self.project_root,
                    self.project_root / ".runtime" / "agent-service" / "python",
                    PythonWorkspaceLimits(
                        timeout_seconds=30,
                        max_code_bytes=256 * 1024,
                        max_request_bytes=1024 * 1024,
                        max_response_bytes=1024 * 1024,
                        max_stdout_bytes=64 * 1024,
                        max_stderr_bytes=64 * 1024,
                        max_variables=1000,
                        allowed_imports=(
                            "pandas",
                            "numpy",
                            "math",
                            "statistics",
                            "datetime",
                            "collections",
                            "itertools",
                        ),
                    ),
                    registry=self._proxy_registry,
                )
                self._python_workspace = workspace
                self._value_resolvers[ProxyRealm.PYTHON] = PythonValueResolver(workspace)
                if self._runtime is not None:
                    self._install_onec_resolver(
                        self._runtime.backend,
                        seed_namespace=False,
                    )
            return self._python_workspace

    def _install_onec_resolver(
        self,
        backend: RuntimeBackend,
        *,
        seed_namespace: bool = True,
    ) -> None:
        resolver = OnecValueResolver(backend, self._proxy_registry)
        if not seed_namespace:
            self._value_resolvers[ProxyRealm.ONEC] = (
                resolver
                if self._python_workspace is None
                else OnecMaterializationBridge(resolver, self._python_workspace)
            )
            return
        snapshot = backend.namespace_snapshot()
        for name in snapshot.names:
            backend.validate_value_reference(f"Контекст.{name}")
        provenance = ProxyProvenance(
            "runtime-admission",
            1,
            hashlib.sha256(
                json.dumps(
                    {
                        "runtime_id": backend.runtime_id,
                        "runtime_generation": snapshot.runtime_generation,
                        "context_generation": snapshot.context_generation,
                        "names": snapshot.names,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            f"runtime-admission-{backend.runtime_id}",
        )
        self._proxy_registry.register_context_batch(
            tuple(
                {
                    "qualified_name": f"bsl.{name}",
                    "type_name": "Неизвестно",
                    "runtime_id": backend.runtime_id,
                    "runtime_generation": snapshot.runtime_generation,
                    "context_generation": snapshot.context_generation,
                    "provenance": provenance,
                    "resolver_handle": f"Контекст.{name}",
                    "capabilities": (
                        "describe",
                        "size",
                        "preview",
                        "get",
                        "select",
                        "snapshot",
                        "materialize",
                        "to_df",
                    ),
                }
                for name in snapshot.names
            )
        )
        self._value_resolvers[ProxyRealm.ONEC] = (
            resolver
            if self._python_workspace is None
            else OnecMaterializationBridge(resolver, self._python_workspace)
        )

    def _python_inputs(self, value: object) -> dict[str, object]:
        if not isinstance(value, Mapping):
            raise ValueError("inputs must be a mapping")
        result: dict[str, object] = {}
        for name, proxy_id in value.items():
            if not isinstance(name, str) or not isinstance(proxy_id, str):
                raise ValueError("Python inputs must map names to proxy ids")
            descriptor = self._proxy_registry.resolve(proxy_id)
            if descriptor.realm is not ProxyRealm.PYTHON:
                raise ValueError("materialize 1C values before passing them to Python")
            result[name] = descriptor
        return result

    def _inline_root(self) -> Path:
        return self.project_root / ".runtime" / "agent-service" / "inline"

    def _persist_inline(self, revision: CodeRevision) -> None:
        root = self._inline_root()
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{revision.cell_id}.json"
        payload = {"cell_id": revision.cell_id, "revision": revision.revision, "source": revision.source, "source_sha256": revision.source_sha256, "document_sha256": revision.document_sha256, "language": revision.language.value, "mode": revision.mode.value, "outputs": list(revision.outputs)}
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    def _load_inline(self) -> dict[str, CodeRevision]:
        result: dict[str, CodeRevision] = {}
        root = self._inline_root()
        if not root.exists():
            return result
        for path in root.glob("inline-*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                revision = CodeRevision(data["cell_id"], data["revision"], data["source"], data["source_sha256"], data["document_sha256"], CodeLanguage(data["language"]), CodeMode(data["mode"]), tuple(data.get("outputs", ())))
                if revision.cell_id.startswith("inline-") and revision.revision == 1 and hashlib.sha256(revision.source.encode()).hexdigest() == revision.source_sha256:
                    result[revision.cell_id] = revision
            except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError):
                continue
        return result

    def _capabilities(self) -> dict[str, CapabilityDescriptor]:
        return {"bsl_main": CapabilityDescriptor("bsl_main", True), "capture": CapabilityDescriptor("capture", True), "value_proxies": CapabilityDescriptor("value_proxies", True), "value_export": CapabilityDescriptor("value_export", False, "deferred"), "python_workspace": CapabilityDescriptor("python_workspace", True), "isolation": CapabilityDescriptor("isolation", False, "deferred")}

    @staticmethod
    def _descriptor_for(revision: CodeRevision) -> CodeDescriptor:
        return CodeDescriptor(revision.cell_id, revision.revision, revision.language, revision.mode, revision.source_sha256, revision.outputs)

    def _failure(self, category: FailureCategory, state: StateChanged, retry: RetrySafety) -> ServiceResponse:
        return ServiceResponse.fail(MethodFailure(category, state, retry, {"service_instance_id": self.service_instance_id}, diagnostic_id=f"diag_{uuid4().hex}"))

    @staticmethod
    def _only(args: Mapping[str, object], allowed: set[str]) -> None:
        if set(args) - allowed:
            raise ValueError("unsupported arguments")

    @staticmethod
    def _string(args: Mapping[str, object], name: str) -> str:
        value = args.get(name)
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name} must be non-empty")
        return value

    @staticmethod
    def _string_sequence(value: object, *, name: str) -> tuple[str, ...]:
        if isinstance(value, str) or not isinstance(value, (tuple, list)):
            raise ValueError(f"{name} must be a sequence")
        result = tuple(value)
        if any(not isinstance(item, str) or not item for item in result):
            raise ValueError(f"{name} must contain non-empty strings")
        return result

    @staticmethod
    def _positive(args: Mapping[str, object], name: str) -> int:
        value = args.get(name)
        if type(value) is not int or value <= 0:
            raise ValueError(f"{name} must be positive")
        return value

    @staticmethod
    def _number(args: Mapping[str, object], name: str) -> float:
        value = args.get(name)
        if type(value) not in {int, float} or isinstance(value, bool):
            raise ValueError(f"{name} must be number")
        return float(value)

    @staticmethod
    def _python_wait(value: object) -> float:
        if (
            type(value) not in {int, float}
            or isinstance(value, bool)
            or not isfinite(float(value))
            or not 0 <= float(value) <= 30
        ):
            raise ValueError("wait_s must be between 0 and 30")
        return float(value)

    def _requested_mode(self, args: Mapping[str, object]) -> CapabilityMode:
        return self._mode(args.get("mode", _DEFAULT_MODE))

    @staticmethod
    def _runtime_profile(args: Mapping[str, object]) -> str:
        value = args.get("profile", "default")
        if not isinstance(value, str) or not value.strip() or len(value) > 128:
            raise ValueError("profile must be a bounded non-empty string")
        return value

    @staticmethod
    def _mode(value: object) -> CapabilityMode:
        try:
            return value if isinstance(value, CapabilityMode) else CapabilityMode(value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as error:
            raise ValueError("invalid capability mode") from error

    @staticmethod
    def _language(value: object) -> CodeLanguage:
        try:
            return value if isinstance(value, CodeLanguage) else CodeLanguage(value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as error:
            raise ValueError("invalid code language") from error

    @staticmethod
    def _code_mode(value: object) -> CodeMode:
        try:
            return value if isinstance(value, CodeMode) else CodeMode(value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as error:
            raise ValueError("invalid code mode") from error

    @staticmethod
    def _policy(args: Mapping[str, object], allowed: set[str]) -> str:
        value = args.get("policy")
        if not isinstance(value, str) or value not in allowed:
            raise ValueError("invalid lifecycle policy")
        return value

    @staticmethod
    def _valid_caller_id(value: object) -> bool:
        return isinstance(value, str) and 0 < len(value) <= _CALLER_MAX_LENGTH and value.isascii() and value.strip() == value
