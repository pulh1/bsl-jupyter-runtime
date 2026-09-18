"""Public logical Worker breakpoint operations on the shared arbiter owner.

The caller supplies the same catalog and workspace used by Worker activation.
Mutation admission is checked both before submission and on the arbiter worker.
Only that worker may install a complete debugger breakpoint workspace. Reads
use the confirmed logical catalog and stay available while a write is pending.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from typing import Callable
from uuid import UUID

from onec_runtime.breakpoint_workspace import (
    BreakpointWorkspaceController,
    BreakpointWorkspaceOutcomeUnknown,
)
from onec_runtime.errors import ProtocolError
from onec_runtime.execution.arbiter import (
    OutcomeUnknown,
    RdbgArbiter,
    SessionPort,
    Settlement,
)
from onec_runtime.bsl.source_maps import SourceUnitRef
from onec_runtime.worker_breakpoints import (
    WorkerBreakpointCoordinator,
    WorkerBreakpointPlan,
    WorkerBreakpointStatus,
)


class WorkerBreakpointService:
    """Prepare, install, and publish Worker breakpoints at stable route boundaries.

    ``require_mutation_boundary`` must reject a live MAIN dispatch, CAPTURE
    resume, or any route where a full workspace replacement is unsafe. The
    controller will supply that admission check when the public path is bound.
    It must not send RDBG commands itself.
    """

    def __init__(
        self,
        arbiter: RdbgArbiter,
        breakpoints: WorkerBreakpointCoordinator,
        workspace: BreakpointWorkspaceController,
        *,
        require_mutation_boundary: Callable[[], None],
        wait_handoff: Callable[[], AbstractContextManager[None]] = nullcontext,
    ) -> None:
        if not isinstance(arbiter, RdbgArbiter):
            raise TypeError("one RDBG arbiter is required")
        if not isinstance(breakpoints, WorkerBreakpointCoordinator):
            raise TypeError("Worker breakpoint coordinator is required")
        if not isinstance(workspace, BreakpointWorkspaceController):
            raise TypeError("shared breakpoint workspace is required")
        if not callable(require_mutation_boundary):
            raise TypeError("Worker breakpoint admission check is required")
        if not callable(wait_handoff):
            raise TypeError("Worker breakpoint ticket wait handoff is invalid")
        self._arbiter = arbiter
        self._breakpoints = breakpoints
        self._workspace = workspace
        self._require_mutation_boundary = require_mutation_boundary
        self._wait_handoff = wait_handoff

    @property
    def arbiter(self) -> RdbgArbiter:
        """The sole RDBG owner used for every breakpoint mutation."""

        return self._arbiter

    def add_worker_breakpoint(
        self,
        source_unit: SourceUnitRef,
        canonical_module: str,
        line: int,
        *,
        enabled: bool = True,
        column: int | None = None,
    ) -> WorkerBreakpointStatus:
        status = self._mutate(
            lambda: self._breakpoints.prepare_add(
                source_unit, canonical_module, line, enabled=enabled, column=column,
            ),
            return_status=True,
        )
        assert isinstance(status, WorkerBreakpointStatus)
        return status

    def remove_worker_breakpoint(self, breakpoint_id: UUID) -> None:
        self._mutate(lambda: self._breakpoints.prepare_remove(breakpoint_id))

    def set_worker_breakpoint_enabled(
        self, breakpoint_id: UUID, enabled: bool,
    ) -> WorkerBreakpointStatus:
        status = self._mutate(
            lambda: self._breakpoints.prepare_enabled(breakpoint_id, enabled),
            return_status=True,
        )
        assert isinstance(status, WorkerBreakpointStatus)
        return status

    def worker_breakpoint_status(
        self, breakpoint_id: UUID,
    ) -> WorkerBreakpointStatus:
        return self._breakpoints.status(breakpoint_id)

    def list_worker_breakpoints(self) -> tuple[WorkerBreakpointStatus, ...]:
        return self._breakpoints.list_statuses()

    def _mutate(
        self,
        prepare: Callable[[], WorkerBreakpointPlan],
        *,
        return_status: bool = False,
    ) -> WorkerBreakpointStatus | None:
        self._require_mutation_boundary()
        route = self._arbiter.current_route

        def plan(port: SessionPort) -> Settlement:
            self._require_mutation_boundary()
            self._workspace.require_confirmed()
            proposal = prepare()
            current = self._workspace.confirmed_snapshot
            installed = False
            if current.worker_slots != proposal.desired_slots:
                desired = self._workspace.prepare(
                    captures=current.captures,
                    ordinary_users=current.ordinary_users,
                    worker_slots=proposal.desired_slots,
                    shielded=current.shielded,
                )
                try:
                    self._workspace.install(desired, port=port)
                except BreakpointWorkspaceOutcomeUnknown:
                    self._quarantine(proposal)
                    raise
                installed = True
            try:
                self._breakpoints.commit(proposal, workspace_confirmed=True)
            except BaseException as error:
                if installed:
                    self._workspace.quarantine()
                    self._quarantine(proposal)
                    raise OutcomeUnknown(
                        "Worker breakpoint publication outcome is unknown"
                    ) from error
                raise
            return Settlement(
                self._breakpoints.status(proposal.result_id)
                if return_status else None
            )

        ticket = self._arbiter.submit(route, plan)
        try:
            self._arbiter.dispatch(ticket)
        except BaseException:
            ticket.cancel_queued()
            raise
        try:
            with self._wait_handoff():
                if ticket.wait_unknown():
                    raise BreakpointWorkspaceOutcomeUnknown(
                        "Worker breakpoint workspace outcome is unknown"
                    )
                return ticket.wait_settled()
        except KeyboardInterrupt:
            # Caller interruption does not cancel an admitted workspace plan.
            ticket.detach_waiter()
            raise

    def _quarantine(self, proposal: WorkerBreakpointPlan) -> None:
        try:
            self._breakpoints.quarantine(proposal)
        except ProtocolError:
            # A post-install catalog failure may already have consumed it.
            pass
