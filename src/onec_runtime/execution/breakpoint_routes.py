"""Route operations over one full-replacement debugger breakpoint owner.

MAIN, CAPTURE and Worker publication must share the same
``BreakpointWorkspaceController``. Every target write here requires the
currently admitted arbiter port; the workspace owner's fallback session is
never used by a route operation.
"""

from __future__ import annotations

from typing import Protocol

from onec_runtime.breakpoint_workspace import (
    BreakpointWorkspaceController, WorkspaceInstallReceipt,
)
from onec_runtime.errors import ProtocolError
from onec_runtime.rdbg.models import ModuleLocation
from onec_runtime.stop_routing import BreakpointRegistry


class BreakpointRoutePort(Protocol):
    """The breakpoint capability of an admitted arbiter SessionPort."""

    def set_breakpoints(self, locations: tuple[ModuleLocation, ...]) -> None: ...


class RouteBreakpointWorkspace:
    """Typed MAIN/CAPTURE commands using the same owner as Worker breakpoints.

    ``plan_idle_captures`` is local only. The controller must call it under
    its idle admission lock, atomically replace its stop registry, and pass
    that registry to ``install_main`` on the next MAIN ticket. A stopped
    CAPTURE successor replacement runs in the same fenced resume ticket as
    writeback and Continue; the controller publishes it only after a receipt.
    """

    def __init__(self, owner: BreakpointWorkspaceController) -> None:
        if not isinstance(owner, BreakpointWorkspaceController):
            raise TypeError("one breakpoint workspace owner is required")
        self._owner = owner

    @property
    def worker_owner(self) -> BreakpointWorkspaceController:
        """Pass this exact owner to WorkerBreakpointWorkspace."""

        return self._owner

    def install_main(
        self, registry: BreakpointRegistry, *, port: BreakpointRoutePort,
    ) -> WorkspaceInstallReceipt:
        """Install all MAIN, capture, user and current Worker locations."""

        self._require_port(port)
        if not isinstance(registry, BreakpointRegistry):
            raise TypeError("MAIN breakpoint registry is required")
        current = self._owner.confirmed_snapshot
        if registry.service != current.service:
            raise ProtocolError("MAIN service point differs from workspace owner")
        desired = self._owner.prepare(
            captures=registry.captures,
            ordinary_users=registry.users,
            worker_slots=current.worker_slots,
            shielded=False,
        )
        return self._owner.install(desired, port=port)

    def shield_capture(self, *, port: BreakpointRoutePort) -> WorkspaceInstallReceipt:
        """Temporarily hide capture points while preserving other groups."""

        self._require_port(port)
        return self._replace_shield(True, port=port)

    def restore_capture(self, *, port: BreakpointRoutePort) -> WorkspaceInstallReceipt:
        """Restore the full confirmed workspace after a CAPTURE helper."""

        self._require_port(port)
        return self._replace_shield(False, port=port)

    def _replace_shield(
        self, shielded: bool, *, port: BreakpointRoutePort,
    ) -> WorkspaceInstallReceipt:
        current = self._owner.confirmed_snapshot
        desired = self._owner.prepare(
            captures=current.captures,
            ordinary_users=current.ordinary_users,
            worker_slots=current.worker_slots,
            shielded=shielded,
        )
        return self._owner.install(desired, port=port)

    def plan_idle_captures(
        self,
        registry: BreakpointRegistry,
        locations: tuple[ModuleLocation, ...],
    ) -> BreakpointRegistry:
        """Validate a local next-MAIN capture registry; make no target write.

        The controller must verify MAIN is terminal and arbiter has no pending
        activity while applying the returned registry under its own lock.
        """

        if not isinstance(registry, BreakpointRegistry):
            raise TypeError("current breakpoint registry is required")
        if type(locations) is not tuple or any(
            type(location) is not ModuleLocation for location in locations
        ):
            raise TypeError("capture locations must be an immutable location tuple")
        if registry.service != self._owner.confirmed_snapshot.service:
            raise ProtocolError("capture registry has another service point")
        self._owner.require_confirmed()
        return BreakpointRegistry(registry.service, locations, registry.users)

    def rearm_captured_successor(
        self,
        registry: BreakpointRegistry,
        locations: tuple[ModuleLocation, ...],
        *,
        port: BreakpointRoutePort,
    ) -> WorkspaceInstallReceipt:
        """Install successor points through the already fenced resume port.

        The controller calls this inside the same arbiter plan as writeback and
        Continue. It publishes its local registry only after this returns a
        confirmed receipt. An unknown install leaves that registry unchanged.
        """

        self._require_port(port)
        if not isinstance(registry, BreakpointRegistry):
            raise TypeError("current breakpoint registry is required")
        if type(locations) is not tuple or any(
            type(location) is not ModuleLocation for location in locations
        ):
            raise TypeError("successor locations must be an immutable tuple")
        self._owner.require_confirmed()
        current = self._owner.confirmed_snapshot
        if (
            registry.service != current.service
            or registry.captures != current.captures
            or registry.users != current.ordinary_users
            or current.shielded
        ):
            raise ProtocolError("CAPTURE successor workspace is stale")
        desired = self._owner.prepare(
            captures=locations,
            ordinary_users=registry.users,
            worker_slots=current.worker_slots,
            shielded=False,
        )
        return self._owner.install(desired, port=port)

    @staticmethod
    def _require_port(port: BreakpointRoutePort) -> None:
        if port is None or not callable(getattr(port, "set_breakpoints", None)):
            raise TypeError("an admitted arbiter breakpoint port is required")
