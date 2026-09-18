"""Fenced CAPTURE data available through one controller/arbiter owner.

Saved stack frames need no RDBG command. Fresh named variables and private
materialization bytes use controller tickets; this adapter never accesses the
debugger session or exposes an unqualified value projection. Full public
``CaptureView.context`` needs additional paged projection tickets.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from math import isfinite
from typing import Callable, Protocol

from onec_runtime.bsl.module_syntax import ModuleSyntaxRegistry
from onec_runtime.capture_inspection import (
    DebugFrame, LocalStackAdapter, ResolvedFrameSource, StackDescriptor,
)
from onec_runtime.errors import CaptureSourceUnavailableError, ProtocolError, StaleCaptureError
from onec_runtime.execution.capture.scope import (
    CaptureContextState, CaptureFrameIdentity, CaptureScope,
)
from onec_runtime.execution.capture.materialization import CaptureMaterializationPlan
from onec_runtime.execution.capture.selected_table_materialization import (
    CaptureSelectedTableTransferRequest,
)
from onec_runtime.execution.capture.stack import CaptureStackInventoryAdapter
from onec_runtime.execution.local_wait import (
    LocalWaitStatus, validate_local_wait_timeout, wait_initiator_locally,
)
from onec_runtime.rdbg.models import FrameVariable, StackFrame


class _CaptureTicket(Protocol):
    def wait_initiator(self, timeout: float | None = None) -> object: ...

    def detach_waiter(self) -> None: ...

    def status(self) -> LocalWaitStatus: ...


class CaptureTicketController(Protocol):
    """Only the CAPTURE ticket capabilities already owned by the controller."""

    capture_scope: CaptureScope | None

    def submit_capture_variable(
        self, name: str, *, stack_level: int = 0,
    ) -> _CaptureTicket: ...

    def submit_capture_materialization(
        self, plan: CaptureMaterializationPlan | CaptureSelectedTableTransferRequest,
        *, _before_first_effect: Callable[[], None] | None = None,
    ) -> _CaptureTicket: ...

    def submit_capture_cleanup_retry(self, key: str) -> _CaptureTicket: ...


class CaptureTicketDataPlane:
    """Bind saved stack and private ticket results to one exact CAPTURE stop.

    ``read_private_variable`` returns a raw debugger model only to trusted
    value policy code. Its presentation must not be sent to notebook, MCP or
    editor callers. ``materialize_private_payload`` likewise returns bytes to
    a trusted decoder. Public value descriptors require separate projection
    capabilities with bounded admission for every selected descendant.
    """

    def __init__(
        self,
        controller: CaptureTicketController,
        scope: CaptureScope,
        *,
        wait_handoff: Callable[[], AbstractContextManager[None]] = nullcontext,
        resolve_sources: Callable[
            [tuple[StackFrame, ...]], tuple[ResolvedFrameSource | None, ...]
        ] | None = None,
        module_registry: ModuleSyntaxRegistry | None = None,
        source_timeout_s: float = 30.0,
        before_materialization: Callable[[], None] | None = None,
        bind_frame: Callable[[DebugFrame], DebugFrame] | None = None,
    ) -> None:
        if not isinstance(scope, CaptureScope):
            raise TypeError("CAPTURE scope is required")
        if not callable(wait_handoff):
            raise TypeError("CAPTURE ticket wait handoff is invalid")
        if resolve_sources is not None and not callable(resolve_sources):
            raise TypeError("CAPTURE source resolver is invalid")
        if before_materialization is not None and not callable(before_materialization):
            raise TypeError("CAPTURE materialization preflight is invalid")
        if bind_frame is not None and not callable(bind_frame):
            raise TypeError("CAPTURE frame binder is invalid")
        if (
            isinstance(source_timeout_s, bool)
            or not isinstance(source_timeout_s, (int, float))
            or not isfinite(float(source_timeout_s))
            or source_timeout_s <= 0
        ):
            raise ValueError("CAPTURE source work budget must be positive")
        self._controller = controller
        self._scope = scope
        self._fence = scope.identity
        self._wait_handoff = wait_handoff
        self._resolve_sources = resolve_sources
        self._module_registry = module_registry or ModuleSyntaxRegistry()
        self._before_materialization = before_materialization
        self._bind_frame = bind_frame
        self._source_timeout_s = float(source_timeout_s)
        self._stack: StackDescriptor | None = None
        self._stack_adapter: LocalStackAdapter | None = None
        self._require_current()

    @property
    def stack(self) -> StackDescriptor:
        """Return bounded public stack pages from the saved stop inventory."""

        self._require_current()
        if self._stack is None:
            backend = CaptureStackInventoryAdapter(
                self._scope, self._fence, validate_current=self._validate_fence,
            )
            adapter = LocalStackAdapter(
                backend,
                self._fence,
                resolve_sources=self._resolve_saved_sources,
                is_runtime_frame=backend.is_runtime_frame,
                registry=self._module_registry,
                command_timeout_s=self._source_timeout_s,
                bind_frame=self._bind_frame,
            )
            self._stack_adapter = adapter
            self._stack = adapter.stack
        return self._stack

    def frame_parameters(self, native_level: int) -> tuple[str, ...]:
        """Resolve parameter names from this stop's saved physical frame source."""

        self._require_current()
        frame = self.stack.native[native_level]
        if frame.runtime_kernel:
            raise CaptureSourceUnavailableError("runtime frame has no public parameters")
        adapter = self._stack_adapter
        assert adapter is not None
        enriched = adapter.native_frame_with_method(native_level)
        self._require_current()
        if enriched.method is None:
            raise CaptureSourceUnavailableError(
                "method source unavailable; use variables for unclassified values"
            )
        return enriched.method.parameters

    def frame(self, native_level: int) -> DebugFrame:
        """Return one physical frame, with values only when a binder was supplied."""

        frame = self.stack.native[native_level]
        if not isinstance(frame, DebugFrame):
            raise ProtocolError("CAPTURE frame result is invalid")
        return frame

    def read_private_variable(
        self, name: str, *, stack_level: int = 0,
    ) -> FrameVariable:
        """Read one named raw variable for a trusted bounded value policy."""

        self._require_current()
        if not isinstance(name, str) or not name or len(name) > 256 or not name.isidentifier():
            raise ValueError("CAPTURE variable name is invalid")
        if type(stack_level) is not int or stack_level < 0:
            raise ValueError("CAPTURE stack level is invalid")
        result = self._wait(self._controller.submit_capture_variable(
            name, stack_level=stack_level,
        ))
        self._require_current()
        if not isinstance(result, FrameVariable):
            raise ProtocolError("CAPTURE variable result is invalid")
        return result

    def materialize_private_payload(
        self,
        plan: CaptureMaterializationPlan | CaptureSelectedTableTransferRequest,
        *, timeout_s: float | None = None,
    ) -> bytes:
        """Run a qualified plan or deferred selected-table request."""

        local_wait = validate_local_wait_timeout(timeout_s)
        self._require_current()
        result = self._wait(self._controller.submit_capture_materialization(
            plan, _before_first_effect=self._before_materialization,
        ), timeout_s=local_wait)
        self._require_current()
        if type(result) is not bytes:
            raise ProtocolError("CAPTURE materialization payload is invalid")
        return result

    def retry_temporary_cleanup(self, key: str) -> None:
        """Retry one controller-confirmed temporary key deletion debt."""

        self._require_current()
        result = self._wait(self._controller.submit_capture_cleanup_retry(key))
        self._require_current()
        if result is not None:
            raise ProtocolError("CAPTURE cleanup result is invalid")

    def _wait(self, ticket: _CaptureTicket, *, timeout_s: float | None = None) -> object:
        return wait_initiator_locally(
            ticket, timeout_s=timeout_s, wait_handoff=self._wait_handoff,
        )

    def _validate_fence(self, fence: object) -> None:
        if fence != self._fence:
            raise StaleCaptureError()
        self._require_current()

    def _require_current(self) -> None:
        scope = self._scope
        if (
            self._controller.capture_scope is not scope
            or scope.identity != self._fence
            or scope.context_state is not CaptureContextState.READY
            or scope.frame_identity is not CaptureFrameIdentity.CONFIRMED
            or scope.inspection_target_id != scope.identity.target_id
            or not scope.published
        ):
            raise StaleCaptureError()

    def _resolve_saved_sources(
        self, frames: tuple[StackFrame, ...],
    ) -> tuple[ResolvedFrameSource | None, ...]:
        self._require_current()
        resolver = self._resolve_sources
        if resolver is None:
            return (None,) * len(frames)
        try:
            resolved = resolver(frames)
        except Exception:
            # Optional source lookup can include private local paths in errors.
            resolved = (None,) * len(frames)
        self._require_current()
        if (
            type(resolved) is not tuple
            or len(resolved) != len(frames)
            or any(
                item is not None and not isinstance(item, ResolvedFrameSource)
                for item in resolved
            )
        ):
            raise ProtocolError("CAPTURE source resolution is invalid")
        return resolved
