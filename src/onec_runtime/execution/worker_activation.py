"""Publish notebook Worker methods within one arbiter-owned RDBG activity.

The route supplies the trusted BSL runner. It must execute every instruction
through the ``SessionPort`` passed to ``activate``; this module never opens a
second debugger reader or calls the legacy runtime controller.
"""

from __future__ import annotations

from dataclasses import replace
from threading import local
from typing import Callable

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
from onec_runtime.server_worker import (
    NotebookWorkerArtifactBuilder,
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
    ) -> None:
        self._host = host
        self._target = target
        self.handle = handle
        self.pin = pin
        self._candidate = candidate
        self._released = False
        self._retained_unknown = False

    def release(self, *, port: SessionPort) -> None:
        del port  # No target request is needed without Worker breakpoints.
        if self._retained_unknown:
            raise ProtocolError("Outcome-unknown Worker lease cannot be released")
        if self._released:
            return
        if self.pin is not None:
            self._target.release_pin(self.pin)
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
    Worker breakpoint reload is deliberately not accepted by this adapter;
    its workspace transaction needs a separate publication collaborator.
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
        self._bound = local()
        self._target = ServerWorkerUniverseRegistry(
            host,
            self._execute_bound_instruction,
            mutation_executor=self._execute_mutation,
        )
        self._active_methods: NotebookMethodSet | None = None
        self._active_descriptor: WorkerModuleArtifact | None = None
        self._active_handle: WorkerGenerationHandle | None = None
        self._worker_exports: tuple[WorkerExport, ...] = ()
        self._revision = 0

    @property
    def active_methods(self) -> NotebookMethodSet | None:
        return self._active_methods

    @property
    def active_handle(self) -> WorkerGenerationHandle | None:
        return self._active_handle

    @property
    def worker_exports(self) -> tuple[WorkerExport, ...]:
        return self._worker_exports

    def pin_active(self) -> _GenerationLease | None:
        """Pin the confirmed active root for an ordinary MAIN/CAPTURE cell."""
        if self._active_handle is None:
            return None
        pin = self._host.pin_active()
        if pin.handle is not self._active_handle:
            self._target.release_pin(pin)
            raise ProtocolError("Active Worker generation changed while pinning")
        return _GenerationLease(self._host, self._target, handle=pin.handle, pin=pin)

    def activate(
        self, intent: WorkerCandidateIntent, *, port: SessionPort,
    ) -> _GenerationLease:
        if not isinstance(intent, WorkerCandidateIntent):
            raise TypeError("Worker candidate intent is required")
        if intent.previous_methods is not self._active_methods:
            raise ProtocolError("Prepared Worker methods are stale")
        if self._breakpoints_present():
            raise ProtocolError("Worker breakpoint publication requires a workspace transaction")
        if getattr(self._bound, "port", None) is not None:
            raise ProtocolError("Worker activation is already bound to an RDBG port")

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
        if validate_production_worker_artifact(artifact) != method_set.exports:
            raise ProtocolError("Prepared Worker artifact catalog changed")
        descriptor = worker_module_artifact_from_notebook(
            artifact, revision=self._revision + 1,
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
            handle = self._target.promote(candidate)
        except (OutcomeUnknown, WorkerPromotionOutcomeUnknown) as error:
            raise WorkerActivationUnknown(
                _GenerationLease(self._host, self._target, candidate=candidate),
                "Worker activation outcome is unknown",
            ) from error
        finally:
            self._bound.port = None

        self._active_methods = method_set
        self._active_descriptor = descriptor
        previous = self._active_handle
        self._active_handle = handle
        self._worker_exports = intent.candidate_catalog
        self._revision += 1
        pin: OperationGenerationPin | None = None
        try:
            pin = self._host.pin_active()
            if pin.handle is not handle:
                self._target.release_pin(pin)
                pin = None
                raise ProtocolError("Promoted Worker generation changed before pinning")
            if previous is not None:
                self._target.release(previous)
        except BaseException as error:
            raise WorkerActivationUnknown(
                _GenerationLease(self._host, self._target, handle=handle,
                                 pin=pin),
                "Worker activation ownership could not be finalized",
            ) from error
        return _GenerationLease(self._host, self._target, handle=handle, pin=pin)

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
