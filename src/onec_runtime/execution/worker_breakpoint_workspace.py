"""Worker generation and debugger breakpoint publication on one arbiter port."""

from __future__ import annotations

from uuid import uuid4

from onec_runtime.breakpoint_workspace import (
    BreakpointWorkspaceController,
    BreakpointWorkspaceOutcomeUnknown,
    WorkspaceSnapshot,
)
from onec_runtime.errors import ProtocolError, WorkerPromotionOutcomeUnknown
from onec_runtime.execution.arbiter import OutcomeUnknown, SessionPort
from onec_runtime.rdbg.models import ModuleLocation
from onec_runtime.worker_breakpoints import (
    WorkerBreakpointConflict,
    WorkerBreakpointCoordinator,
    WorkerBreakpointPlan,
    WorkerBreakpointReloadPolicy,
)
from onec_runtime.worker_universe import (
    OperationGenerationPin,
    ServerWorkerUniverseRegistry,
    WorkerGenerationHandle,
    WorkerUniverseCandidate,
    WorkerUniverseRegistry,
)


class WorkerBreakpointWorkspace:
    """Keep the logical Worker catalog and complete RDBG workspace in step.

    The caller must own an admitted arbiter activity. Every breakpoint write
    uses its ``SessionPort``; the workspace owner's fallback session is never
    used. A lost reply quarantines the proposal and leaves the generation for
    explicit reconciliation rather than replaying a possible side effect.
    """

    def __init__(
        self,
        breakpoints: WorkerBreakpointCoordinator,
        workspace: BreakpointWorkspaceController,
        *,
        reload_policy: WorkerBreakpointReloadPolicy = WorkerBreakpointReloadPolicy.STRICT,
    ) -> None:
        if not isinstance(breakpoints, WorkerBreakpointCoordinator):
            raise TypeError("Worker breakpoint coordinator is required")
        if not isinstance(workspace, BreakpointWorkspaceController):
            raise TypeError("Breakpoint workspace owner is required")
        if type(reload_policy) is not WorkerBreakpointReloadPolicy:
            raise TypeError("Worker breakpoint reload policy is invalid")
        self._breakpoints = breakpoints
        self._workspace = workspace
        self._reload_policy = reload_policy

    def promote(
        self,
        host: WorkerUniverseRegistry,
        target: ServerWorkerUniverseRegistry,
        candidate: WorkerUniverseCandidate,
        *,
        port: SessionPort,
    ) -> WorkerGenerationHandle:
        self._require_port(port)
        self._workspace.require_confirmed()
        previous = self._workspace.confirmed_snapshot
        prepared = None
        plan: WorkerBreakpointPlan | None = None
        installed = False
        swapped = False
        try:
            prepared = target.prepare_root(candidate, transaction_id=uuid4())
            plan = self._breakpoints.prepare_generation(
                host._candidate_debug_view(candidate), self._reload_policy,
            )
            desired = self._prepare_workspace(plan.desired_slots)
            self._workspace.install(desired, port=port)
            installed = True
            handle = target.swap_root(prepared)
            swapped = True
            self._breakpoints.commit(plan, workspace_confirmed=True)
            return handle
        except WorkerBreakpointConflict:
            if prepared is not None:
                target.discard_root(prepared)
            raise
        except (BreakpointWorkspaceOutcomeUnknown, WorkerPromotionOutcomeUnknown, OutcomeUnknown):
            self._quarantine(plan)
            if prepared is not None and not swapped:
                target.quarantine_root(prepared)
            raise
        except BaseException as error:
            if swapped:
                self._quarantine(plan)
                raise OutcomeUnknown(
                    "Worker breakpoint catalog could not confirm the active root"
                ) from error
            try:
                if installed:
                    self._restore(previous, port=port)
                if prepared is not None:
                    target.discard_root(prepared)
            except BaseException as restoration_error:
                self._quarantine(plan)
                raise OutcomeUnknown(
                    "Worker breakpoint workspace could not be restored"
                ) from restoration_error
            raise

    def release(
        self,
        target: ServerWorkerUniverseRegistry,
        release: WorkerGenerationHandle | OperationGenerationPin,
        *,
        port: SessionPort,
    ) -> None:
        self._require_port(port)
        self._workspace.require_confirmed()
        lifecycle = target.preview_release(release)
        plan = self._breakpoints.prepare_release(lifecycle.remaining_views)
        previous = self._workspace.confirmed_snapshot
        desired = self._prepare_workspace(plan.desired_slots)
        try:
            self._workspace.install(desired, port=port)
        except BreakpointWorkspaceOutcomeUnknown:
            self._quarantine(plan)
            raise
        try:
            target.commit_release(lifecycle)
        except BaseException:
            try:
                self._restore(previous, port=port)
            except BaseException as restoration_error:
                self._quarantine(plan)
                raise OutcomeUnknown(
                    "Worker release workspace could not be restored"
                ) from restoration_error
            raise
        try:
            self._breakpoints.commit(plan, workspace_confirmed=True)
        except BaseException as error:
            self._quarantine(plan)
            raise OutcomeUnknown(
                "Worker lifecycle changed without a confirmed breakpoint catalog"
            ) from error

    def _prepare_workspace(
        self, slots: tuple[ModuleLocation, ...],
    ) -> WorkspaceSnapshot:
        previous = self._workspace.confirmed_snapshot
        return self._workspace.prepare(
            captures=previous.captures,
            ordinary_users=previous.ordinary_users,
            worker_slots=slots,
            shielded=previous.shielded,
        )

    def _restore(self, snapshot: WorkspaceSnapshot, *, port: SessionPort) -> None:
        restored = self._workspace.prepare(
            captures=snapshot.captures,
            ordinary_users=snapshot.ordinary_users,
            worker_slots=snapshot.worker_slots,
            shielded=snapshot.shielded,
        )
        self._workspace.install(restored, port=port)

    @staticmethod
    def _require_port(port: SessionPort) -> None:
        if port is None or not callable(getattr(port, "set_breakpoints", None)):
            raise TypeError("An admitted arbiter breakpoint port is required")

    def _quarantine(self, plan: WorkerBreakpointPlan | None) -> None:
        if plan is not None:
            try:
                self._breakpoints.quarantine(plan)
            except ProtocolError:
                pass
        self._workspace.quarantine()
