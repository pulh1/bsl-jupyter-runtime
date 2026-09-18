"""Publish notebook Worker methods within one arbiter-owned RDBG activity.

The route supplies the trusted BSL runner. It must execute every instruction
through the ``SessionPort`` passed to ``activate``; this module never opens a
second debugger reader or calls the legacy runtime controller.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from threading import RLock, local
from typing import Callable

from onec_runtime.breakpoint_workspace import BreakpointWorkspaceOutcomeUnknown
from onec_runtime.bsl.notebook_method_globals import bind_notebook_method_globals
from onec_runtime.bsl.notebook_methods import (
    NotebookMethodSet,
    instrument_notebook_worker_messages,
)
from onec_runtime.bsl.semantic_lowering import WorkerExport
from onec_runtime.errors import BslExecutionError, ProtocolError, WorkerPromotionOutcomeUnknown
from onec_runtime.execution.arbiter import OutcomeUnknown, SessionPort
from onec_runtime.execution.preparation import WorkerCandidateIntent
from onec_runtime.execution.worker import WorkerActivationUnknown
from onec_runtime.execution.worker_breakpoint_workspace import WorkerBreakpointWorkspace
from onec_runtime.worker_breakpoints import (
    WorkerBreakpointReloadPolicy, WorkerBreakpointReloadReport,
)
from onec_runtime.server_worker import (
    NotebookWorkerArtifactBuilder, WorkerArtifact, WorkerSourceProvenance,
    validate_production_worker_artifact,
)
from onec_runtime.worker_universe import (
    OperationGenerationPin,
    PreparedWorkerMutation,
    ServerWorkerUniverseRegistry,
    WorkerGenerationHandle,
    WorkerModuleArtifact,
    WorkerUniverseCandidate,
    WorkerUniverseRegistry,
    WorkerUniverseState,
    worker_module_artifact_from_notebook,
)


@dataclass(frozen=True, slots=True)
class WorkerActivationSnapshot:
    """One atomic published Worker generation for route preparation."""

    revision: int
    worker_exports: tuple[WorkerExport, ...] = field(repr=False)
    active_methods: NotebookMethodSet | None = field(repr=False)
    active_handle: WorkerGenerationHandle | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class WorkerMaterializationSnapshot:
    """One published Worker revision and its exact privacy registrations."""

    revision: int
    registrations: tuple[str, ...] = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("Worker materialization revision is invalid")
        if type(self.registrations) is not tuple or any(
            not isinstance(item, str) or not item for item in self.registrations
        ):
            raise ValueError("Worker materialization registrations are invalid")


class PrebuiltWorkerIntent:
    """One-use local Worker artifact for a later admitted activation.

    The EPF and its source map are built before capture setup, so provenance
    can be journaled before target mutation. Only the owning adapter may
    consume the artifact; the controller still admits the activation ticket.
    """

    __slots__ = (
        "_owner", "_intent", "_method_set", "_artifact", "_revision",
        "_consumed", "_lock",
    )

    def __init__(
        self,
        owner: object,
        intent: WorkerCandidateIntent,
        method_set: NotebookMethodSet,
        artifact: WorkerArtifact,
        revision: int,
    ) -> None:
        object.__setattr__(self, "_owner", owner)
        object.__setattr__(self, "_intent", intent)
        object.__setattr__(self, "_method_set", method_set)
        object.__setattr__(self, "_artifact", artifact)
        object.__setattr__(self, "_revision", revision)
        object.__setattr__(self, "_consumed", False)
        object.__setattr__(self, "_lock", RLock())

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("prebuilt Worker intents are immutable")

    def __repr__(self) -> str:
        return "<redacted prebuilt Worker intent>"

    def __deepcopy__(self, memo: dict[int, object]) -> str:
        del memo
        return "<redacted prebuilt Worker intent>"

    @property
    def method_set_candidate(self) -> NotebookMethodSet:
        """Retain the existing route settlement's visible-source contract."""

        return self._intent.method_set_candidate

    @property
    def intent(self) -> WorkerCandidateIntent:
        return self._intent

    @property
    def source_provenance(self) -> WorkerSourceProvenance:
        provenance = self._artifact.source_provenance
        if not isinstance(provenance, WorkerSourceProvenance):
            raise ProtocolError("Prebuilt Worker provenance is incomplete")
        return provenance

    def consume(
        self, owner: object,
    ) -> tuple[WorkerCandidateIntent, NotebookMethodSet, WorkerArtifact, int]:
        with self._lock:
            if self._owner is not owner:
                raise ProtocolError("Prebuilt Worker belongs to another runtime")
            if self._consumed:
                raise ProtocolError("Prebuilt Worker intent was already consumed")
            object.__setattr__(self, "_consumed", True)
            return self._intent, self._method_set, self._artifact, self._revision


class _GenerationLease:
    """Retain an exact Worker pin, including an incomplete activation."""

    def __init__(
        self,
        host: WorkerUniverseRegistry,
        target: ServerWorkerUniverseRegistry,
        *,
        handle: WorkerGenerationHandle | None = None,
        pin: OperationGenerationPin | None = None,
        candidate: WorkerUniverseCandidate | None = None,
        worker_breakpoints_present: Callable[[], bool],
        breakpoint_workspace: WorkerBreakpointWorkspace | None = None,
    ) -> None:
        self._host = host
        self._target = target
        self.handle = handle
        self.pin = pin
        self._candidate = candidate
        self._breakpoints_present = worker_breakpoints_present
        self._breakpoint_workspace = breakpoint_workspace
        self._released = False
        self._retained_unknown = False

    def release(self, *, port: SessionPort) -> None:
        if self._retained_unknown:
            raise ProtocolError("Outcome-unknown Worker lease cannot be released")
        if self._released:
            return
        if self._breakpoints_present() and self._breakpoint_workspace is None:
            raise ProtocolError("Worker breakpoint release requires a workspace transaction")
        if self.pin is not None:
            if self._breakpoint_workspace is None:
                self._target.release_pin(self.pin)
            else:
                self._breakpoint_workspace.release(self._target, self.pin, port=port)
        self._released = True

    def retain_outcome_unknown(self, *, port: SessionPort) -> None:
        del port
        if self._retained_unknown:
            return
        if self._released:
            raise ProtocolError("Released Worker lease cannot be retained")
        if self.pin is not None:
            if self._host.state not in (WorkerUniverseState.BROKEN, WorkerUniverseState.CLOSED):
                self._host.retain_outcome_unknown(self.pin)
        else:
            if self._host.state not in (WorkerUniverseState.BROKEN, WorkerUniverseState.CLOSED):
                self._host.mark_broken(self._candidate)
        self._retained_unknown = True


class WorkerUniverseActivationAdapter:
    """Build, promote, and pin notebook methods using one supplied RDBG port.

    The adapter owns one persistent host/target registry pair for a session.
    ``instruction_runner`` is route specific and must use the supplied port.
    ``breakpoint_workspace`` is the publication collaborator for logical Worker
    breakpoints and full-replacement RDBG workspace writes.
    The next route snapshot must use the exact ``active_methods`` object,
    because activation binds global names into a new immutable method set.

    Once a target mutation reservation is claimed, the current registry has
    no confirmed pretransport cancellation path. An exception other than a
    confirmed BSL failure therefore retains unknown ownership, even if its
    cause may have been local.
    """

    def __init__(
        self,
        host: WorkerUniverseRegistry,
        *,
        notebook_builder: NotebookWorkerArtifactBuilder,
        instruction_runner: Callable[[SessionPort, str], object],
        worker_breakpoints_present: Callable[[], bool],
        breakpoint_workspace: WorkerBreakpointWorkspace | None = None,
        target_profile: str = "notebook-worker",
        base_artifacts: tuple[WorkerModuleArtifact, ...] = (),
    ) -> None:
        if not isinstance(host, WorkerUniverseRegistry):
            raise TypeError("Worker universe registry is required")
        if not callable(notebook_builder) or not callable(instruction_runner):
            raise TypeError("Worker builder and instruction runner are required")
        if not callable(worker_breakpoints_present):
            raise TypeError("Worker breakpoint inventory callback is required")
        if not isinstance(target_profile, str) or not target_profile:
            raise ValueError("Worker target profile is required")
        self._host = host
        self._builder = notebook_builder
        self._instruction_runner = instruction_runner
        self._target_profile = target_profile
        self._base_artifacts = base_artifacts
        self._breakpoints_present = worker_breakpoints_present
        self._breakpoint_workspace = breakpoint_workspace
        self._bound = local()
        self._target = ServerWorkerUniverseRegistry(
            host,
            self._execute_bound_instruction,
            mutation_executor=self._execute_mutation,
        )
        self._snapshot_lock = RLock()
        self._published = WorkerActivationSnapshot(0, (), None, None)
        self._notebook_descriptor: WorkerModuleArtifact | None = None
        self._prebuild_owner = object()

    def snapshot(self) -> WorkerActivationSnapshot:
        """Return one immutable version; never combine separate live getters."""
        with self._snapshot_lock:
            return self._published

    def materialization_snapshot(self) -> WorkerMaterializationSnapshot:
        """Read the active Worker revision and privacy registrations together."""

        with self._snapshot_lock:
            published = self._published
            registrations = (
                () if published.active_handle is None
                else self._target.privacy_registration_snapshot()
            )
            return WorkerMaterializationSnapshot(published.revision, registrations)

    @property
    def active_methods(self) -> NotebookMethodSet | None:
        return self.snapshot().active_methods

    @property
    def active_handle(self) -> WorkerGenerationHandle | None:
        return self.snapshot().active_handle

    @property
    def worker_exports(self) -> tuple[WorkerExport, ...]:
        return self.snapshot().worker_exports

    @property
    def supports_breakpoint_reload(self) -> bool:
        return self._breakpoint_workspace is not None

    def last_worker_breakpoint_reload_report(
        self,
    ) -> WorkerBreakpointReloadReport | None:
        workspace = self._breakpoint_workspace
        return None if workspace is None else workspace.last_reload_report

    def publish_modules(
        self, artifacts: tuple[WorkerModuleArtifact, ...], *, port: SessionPort,
        reload_policy: WorkerBreakpointReloadPolicy = WorkerBreakpointReloadPolicy.STRICT,
    ) -> WorkerGenerationHandle:
        """Promote a complete module graph through this adapter's bound port.

        A breakpoint-bearing reload uses the same workspace owner and report
        as notebook Worker activation.
        """

        if port is None:
            raise TypeError("An admitted arbiter port is required to publish Worker")
        if (
            type(artifacts) is not tuple
            or not artifacts
            or any(type(artifact) is not WorkerModuleArtifact for artifact in artifacts)
        ):
            raise TypeError("Worker module artifacts are required")
        names = tuple(artifact.logical_name.casefold() for artifact in artifacts)
        if len(names) != len(set(names)) or "worker" in names:
            raise ProtocolError("Worker module artifact names are invalid")
        if type(reload_policy) is not WorkerBreakpointReloadPolicy:
            raise TypeError("Worker breakpoint reload policy is invalid")
        workspace = self._breakpoint_workspace
        breakpoints_present = self._breakpoints_present()
        if breakpoints_present and workspace is None:
            raise ProtocolError("Worker breakpoint reload requires a policy/report port")
        if getattr(self._bound, "port", None) is not None:
            raise ProtocolError("Worker activation is already bound to an RDBG port")
        published = self.snapshot()
        if self._host.active_handle is not published.active_handle:
            raise ProtocolError("Worker active generation changed before publication")
        notebook = self._notebook_descriptor
        candidate = self._host.prepare(
            artifacts + (() if notebook is None else (notebook,)),
        )
        self._bound.port = port
        try:
            handle = (
                self._target.promote(candidate)
                if workspace is None else workspace.promote(
                    self._host, self._target, candidate,
                    port=port, reload_policy=reload_policy,
                    record_report=breakpoints_present,
                )
            )
        except (
            OutcomeUnknown, WorkerPromotionOutcomeUnknown,
            BreakpointWorkspaceOutcomeUnknown,
        ) as error:
            raise WorkerActivationUnknown(
                _GenerationLease(
                    self._host, self._target, candidate=candidate,
                    worker_breakpoints_present=self._breakpoints_present,
                    breakpoint_workspace=self._breakpoint_workspace,
                ),
                "Worker module publication outcome is unknown",
            ) from error
        finally:
            self._bound.port = None
        previous = published.active_handle
        if previous is not None:
            try:
                if workspace is None:
                    self._target.release(previous)
                else:
                    workspace.release(self._target, previous, port=port)
            except BaseException as error:
                raise WorkerActivationUnknown(
                    _GenerationLease(
                        self._host, self._target, handle=handle,
                        worker_breakpoints_present=self._breakpoints_present,
                        breakpoint_workspace=self._breakpoint_workspace,
                    ),
                    "Worker module ownership could not be finalized",
                ) from error
        with self._snapshot_lock:
            self._base_artifacts = artifacts
            self._published = WorkerActivationSnapshot(
                published.revision + 1, candidate.export_catalog,
                published.active_methods, handle,
            )
        return handle

    def release_generation(
        self, handle: WorkerGenerationHandle, *, port: SessionPort,
    ) -> None:
        """Release an explicit generation handle inside an admitted ticket."""

        if port is None:
            raise TypeError("An admitted arbiter port is required to release Worker")
        if not isinstance(handle, WorkerGenerationHandle):
            raise TypeError("Worker generation handle is required")
        workspace = self._breakpoint_workspace
        if self._breakpoints_present() and workspace is None:
            raise ProtocolError("Worker breakpoint release requires a policy/report port")
        if workspace is None:
            self._target.release(handle)
        else:
            workspace.release(self._target, handle, port=port)

    def pin_active(self, *, port: SessionPort) -> _GenerationLease | None:
        """Pin the confirmed active root for an ordinary MAIN/CAPTURE cell."""
        if port is None:
            raise TypeError("An admitted arbiter port is required to pin Worker")
        published = self.snapshot()
        if published.active_handle is None:
            return None
        if self._breakpoints_present() and self._breakpoint_workspace is None:
            raise ProtocolError("Worker breakpoint pin requires a workspace transaction")
        pin = self._host.pin_active()
        if pin.handle is not published.active_handle:
            self._target.release_pin(pin)
            raise ProtocolError("Active Worker generation changed while pinning")
        return _GenerationLease(
            self._host, self._target, handle=pin.handle, pin=pin,
            worker_breakpoints_present=self._breakpoints_present,
            breakpoint_workspace=self._breakpoint_workspace,
        )

    def prebuild_for_capture(
        self, intent: WorkerCandidateIntent,
    ) -> PrebuiltWorkerIntent:
        """Build locally without a debugger port or Worker publication."""

        if not isinstance(intent, WorkerCandidateIntent):
            raise TypeError("Worker candidate intent is required")
        published = self.snapshot()
        if intent.previous_methods is not published.active_methods:
            raise ProtocolError("Prepared Worker methods are stale")
        method_set, artifact = self._build_artifact(intent)
        if validate_production_worker_artifact(artifact) != method_set.exports:
            raise ProtocolError("Prepared Worker artifact catalog changed")
        current = self.snapshot()
        if (
            current.revision != published.revision
            or current.active_methods is not published.active_methods
        ):
            raise ProtocolError("Worker generation changed during local prebuild")
        prepared = PrebuiltWorkerIntent(
            self._prebuild_owner, intent, method_set, artifact, published.revision,
        )
        prepared.source_provenance
        return prepared

    def _build_artifact(
        self, intent: WorkerCandidateIntent,
    ) -> tuple[NotebookMethodSet, WorkerArtifact]:
        method_set = intent.method_set_candidate
        bound_source, bound_globals = bind_notebook_method_globals(
            method_set.mapped_source,
            context_names=intent.namespace_names,
            exports=method_set.exports,
        )
        method_set = replace(method_set, bound_globals=bound_globals)
        artifact = self._builder(
            instrument_notebook_worker_messages(bound_source),
            method_set.exports,
            visible_source_context=method_set.visible_source_context,
        )
        return method_set, artifact

    def activate(
        self, intent: WorkerCandidateIntent | PrebuiltWorkerIntent, *, port: SessionPort,
    ) -> _GenerationLease:
        if not isinstance(intent, (WorkerCandidateIntent, PrebuiltWorkerIntent)):
            raise TypeError("Worker candidate intent is required")
        if port is None or (
            self._breakpoint_workspace is not None
            and not callable(getattr(port, "set_breakpoints", None))
        ):
            raise TypeError("An admitted arbiter port is required to activate Worker")
        prebuilt = (
            intent.consume(self._prebuild_owner)
            if isinstance(intent, PrebuiltWorkerIntent) else None
        )
        if prebuilt is not None:
            intent, method_set, artifact, prepared_revision = prebuilt
        published = self.snapshot()
        if intent.previous_methods is not published.active_methods:
            raise ProtocolError("Prepared Worker methods are stale")
        if prebuilt is not None and prepared_revision != published.revision:
            raise ProtocolError("Prebuilt Worker generation is stale")
        if self._breakpoints_present() and self._breakpoint_workspace is None:
            raise ProtocolError("Worker breakpoint publication requires a workspace transaction")
        if getattr(self._bound, "port", None) is not None:
            raise ProtocolError("Worker activation is already bound to an RDBG port")

        if prebuilt is None:
            method_set, artifact = self._build_artifact(intent)
        if validate_production_worker_artifact(artifact) != method_set.exports:
            raise ProtocolError("Prepared Worker artifact catalog changed")
        descriptor = worker_module_artifact_from_notebook(
            artifact, revision=published.revision + 1,
            target_profile=self._target_profile,
        )
        exports = tuple(
            item if item.receiver_module is not None else WorkerExport(
                item.public_path, item.method, receiver_module="Worker",
            )
            for item in intent.candidate_catalog
        )
        candidate = self._host.prepare(
            self._base_artifacts + (descriptor,), export_catalog=exports,
        )
        self._bound.port = port
        try:
            handle = (
                self._target.promote(candidate)
                if self._breakpoint_workspace is None
                else self._breakpoint_workspace.promote(
                    self._host, self._target, candidate, port=port,
                    record_report=self._breakpoints_present(),
                )
            )
        except (
            OutcomeUnknown, WorkerPromotionOutcomeUnknown,
            BreakpointWorkspaceOutcomeUnknown,
        ) as error:
            raise WorkerActivationUnknown(
                _GenerationLease(
                    self._host, self._target, candidate=candidate,
                    worker_breakpoints_present=self._breakpoints_present,
                    breakpoint_workspace=self._breakpoint_workspace,
                ),
                "Worker activation outcome is unknown",
            ) from error
        finally:
            self._bound.port = None

        previous = published.active_handle
        pin: OperationGenerationPin | None = None
        try:
            pin = self._host.pin_active()
            if pin.handle is not handle:
                self._target.release_pin(pin)
                pin = None
                raise ProtocolError("Promoted Worker generation changed before pinning")
            if previous is not None:
                if self._breakpoint_workspace is None:
                    self._target.release(previous)
                else:
                    self._breakpoint_workspace.release(
                        self._target, previous, port=port,
                    )
        except BaseException as error:
            raise WorkerActivationUnknown(
                _GenerationLease(
                    self._host, self._target, handle=handle, pin=pin,
                    worker_breakpoints_present=self._breakpoints_present,
                    breakpoint_workspace=self._breakpoint_workspace,
                ),
                "Worker activation ownership could not be finalized",
            ) from error
        with self._snapshot_lock:
            self._notebook_descriptor = descriptor
            self._published = WorkerActivationSnapshot(
                published.revision + 1, intent.candidate_catalog, method_set, handle,
            )
        return _GenerationLease(
            self._host, self._target, handle=handle, pin=pin,
            worker_breakpoints_present=self._breakpoints_present,
            breakpoint_workspace=self._breakpoint_workspace,
        )

    def _execute_bound_instruction(self, instruction: str) -> object:
        port = getattr(self._bound, "port", None)
        if port is None:
            raise ProtocolError("Worker mutation has no arbiter port")
        return self._instruction_runner(port, instruction)

    def _execute_mutation(self, mutation: PreparedWorkerMutation) -> object:
        try:
            result = self._execute_bound_instruction(mutation.instruction)
        except BslExecutionError as error:
            return mutation.abort(error)
        except BaseException as error:
            # The command may have entered transport. Keep its claimed
            # reservation and let the arbiter ticket own reconciliation.
            raise OutcomeUnknown("Worker mutation outcome is unknown") from error
        return mutation.commit(result)
