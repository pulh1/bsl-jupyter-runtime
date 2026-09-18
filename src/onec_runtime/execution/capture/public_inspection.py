"""Facade-facing CAPTURE inspection bound to controller-owned tickets."""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from threading import RLock
from typing import Callable, Protocol, TypeAlias

from onec_runtime.capture_inspection import (
    DebugFrame, ResolvedFrameSource, StackDescriptor,
)
from onec_runtime.capture_values import (
    CaptureContextView, CaptureValuePolicy, ValueRoot, ValueRootKind,
)
from onec_runtime.errors import ProtocolError
from onec_runtime.execution.capture.data_plane import CaptureTicketDataPlane
from onec_runtime.execution.capture.scope import CaptureScope
from onec_runtime.execution.capture.value_projection import CaptureTicketValueProjection
from onec_runtime.rdbg.models import StackFrame


CaptureSourceResolver: TypeAlias = Callable[
    [tuple[StackFrame, ...]], tuple[ResolvedFrameSource | None, ...]
]


class CaptureInspectionController(Protocol):
    """Controller capabilities used by the public CAPTURE inspection bridge."""

    capture_scope: CaptureScope | None


class CaptureInspection:
    """One stack, frame, and context handle fenced to the current CAPTURE stop."""

    def __init__(
        self,
        data: CaptureTicketDataPlane,
        values: CaptureTicketValueProjection,
    ) -> None:
        self._data = data
        self._values = values

    @property
    def stack(self) -> StackDescriptor:
        """Return the saved public stack for this exact CAPTURE stop.

        The returned descriptor checks the same stop fence on every page read.
        """

        return self._data.stack

    def frame(self, native_level: int) -> DebugFrame:
        """Return one physical frame with ticket-backed bounded variables."""

        return self._values.bind_frame(self._data.frame(native_level))

    @property
    def context(self) -> CaptureContextView:
        """Return root context variables through controller-owned page tickets."""

        return self._values.context


class CaptureInspectionBridge:
    """Create facade-facing inspection handles for the controller's active scope.

    This bridge uses saved stack inventory plus controller submission ports. It
    deliberately contains no RDBG session or transport access.
    """

    def __init__(
        self,
        controller: CaptureInspectionController,
        *,
        policy: CaptureValuePolicy = CaptureValuePolicy(),
        wait_handoff: Callable[[], AbstractContextManager[None]] = nullcontext,
        resolve_sources: CaptureSourceResolver | None = None,
    ) -> None:
        if not isinstance(policy, CaptureValuePolicy):
            raise TypeError("CAPTURE value policy is invalid")
        if not callable(wait_handoff):
            raise TypeError("CAPTURE ticket wait handoff is invalid")
        if resolve_sources is not None and not callable(resolve_sources):
            raise TypeError("CAPTURE source resolver is invalid")
        self._controller = controller
        self._policy = policy
        self._wait_handoff = wait_handoff
        self._resolve_sources = resolve_sources
        self._resolver_lock = RLock()

    def configure_source_resolver(
        self, resolver: CaptureSourceResolver | None,
    ) -> None:
        """Bind source metadata for future stops while no scope is active."""

        if resolver is not None and not callable(resolver):
            raise TypeError("CAPTURE source resolver is invalid")
        with self._resolver_lock:
            if self._controller.capture_scope is not None:
                raise ProtocolError("Cannot change source resolver during an active CAPTURE scope")
            self._resolve_sources = resolver

    def current(self) -> CaptureInspection:
        """Bind a stack/frame/context API to the active ready CAPTURE scope."""

        with self._resolver_lock:
            scope = self._controller.capture_scope
            resolve_sources = self._resolve_sources
        if not isinstance(scope, CaptureScope):
            raise ProtocolError("CAPTURE inspection requires an active scope")
        data: CaptureTicketDataPlane

        def resolve_parameters(root: ValueRoot) -> tuple[str, ...]:
            native_level = (
                root.native_level if root.kind is ValueRootKind.FRAME
                else scope.frame_stack_level
            )
            if type(native_level) is not int or native_level < 0:
                raise ProtocolError("CAPTURE source frame is unavailable")
            return data.frame_parameters(native_level)

        values = CaptureTicketValueProjection(
            self._controller, scope, policy=self._policy,  # type: ignore[arg-type]
            wait_handoff=self._wait_handoff,
            resolve_parameters=resolve_parameters,
        )
        data = CaptureTicketDataPlane(
            self._controller, scope, wait_handoff=self._wait_handoff,  # type: ignore[arg-type]
            resolve_sources=resolve_sources,
            bind_frame=values.bind_frame,
        )
        return CaptureInspection(data, values)


__all__ = [
    "CaptureInspection", "CaptureInspectionBridge",
    "CaptureInspectionController", "CaptureSourceResolver",
]
