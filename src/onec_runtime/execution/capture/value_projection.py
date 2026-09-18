"""Bounded CAPTURE variable projection through controller-owned tickets.

RDBG value presentations are private input.  Until a per-value materializer is
bound to this component path, this adapter exposes a bounded inventory as
``UnavailableValueNode`` records for pages. Exact-name lookup returns checked
metadata with an opaque preview and unknown shape; values cannot be expanded.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from typing import Callable, Protocol

from onec_runtime.capture_inspection import DebugFrame
from onec_runtime.capture_values import (
    CaptureContextView,
    CaptureValuePolicy,
    LocalCaptureValueAdapter,
    PrivateProjectedValue,
    PrivateValueProjection,
    SafePathSegment,
    ValueMetadata,
    ValueInspectionRequest,
    ValuePathSegmentKind,
    ValueRootKind,
    ValueShape,
    ValueViewKind,
    VariableRole,
)
from onec_runtime.errors import (
    CaptureShapeUnsupportedError,
    CaptureValueCheckError,
    ProtocolError,
    StaleCaptureError,
)
from onec_runtime.execution.capture.inspection import NativeVariablePage
from onec_runtime.execution.capture.scope import (
    CaptureContextState,
    CaptureFrameIdentity,
    CaptureScope,
)
from onec_runtime.rdbg.models import FrameVariable


class _CaptureTicket(Protocol):
    def wait_initiator(self) -> object: ...
    def detach_waiter(self) -> None: ...


class CaptureVariablePageController(Protocol):
    capture_scope: CaptureScope | None

    def submit_capture_variable_page(
        self, *, stack_level: int, start: int, stop: int,
    ) -> _CaptureTicket: ...

    def submit_capture_variable(
        self, name: str, *, stack_level: int = 0,
    ) -> _CaptureTicket: ...


class CaptureTicketValueProjection:
    """Attach bounded variable inventory and metadata to public CAPTURE frames."""

    def __init__(
        self,
        controller: CaptureVariablePageController,
        scope: CaptureScope,
        *,
        policy: CaptureValuePolicy = CaptureValuePolicy(),
        wait_handoff: Callable[[], AbstractContextManager[None]] = nullcontext,
    ) -> None:
        if not isinstance(scope, CaptureScope):
            raise TypeError("CAPTURE scope is required")
        if not isinstance(policy, CaptureValuePolicy):
            raise TypeError("CAPTURE value policy is invalid")
        if not callable(wait_handoff):
            raise TypeError("CAPTURE ticket wait handoff is invalid")
        self._controller = controller
        self._scope = scope
        self._fence = scope.identity
        self._wait_handoff = wait_handoff
        self._adapter = LocalCaptureValueAdapter(
            self, self._fence, policy=policy, resolve_parameters=lambda _root: (),
        )
        self._validate_current()

    def frame(self, native_level: int) -> CaptureContextView:
        self._validate_current()
        return self._adapter.frame(native_level)

    @property
    def context(self) -> CaptureContextView:
        self._validate_current()
        return self._adapter.context

    def bind_frame(self, frame: DebugFrame) -> DebugFrame:
        self._validate_current()
        return self._adapter.bind_frame(frame)

    def validate_inspection(self, fence: object) -> None:
        if fence != self._fence:
            raise StaleCaptureError()
        self._validate_current()

    def project_values(
        self, fence: object, request: ValueInspectionRequest,
    ) -> PrivateValueProjection:
        self.validate_inspection(fence)
        self._require_variable_root(request)
        native_level = request.path.root.native_level or 0
        if isinstance(request.exact, str):
            return self._selected_variable(request, native_level)
        result = self._wait(self._controller.submit_capture_variable_page(
            stack_level=native_level, start=request.start, stop=request.stop,
        ))
        self._validate_current()
        if not isinstance(result, NativeVariablePage):
            raise ProtocolError("CAPTURE variable page result is invalid")
        self._validate_page(result, request)
        return PrivateValueProjection(
            tuple(
                PrivateProjectedValue(name, lambda: None, unavailable=True)
                for name in result.names
            ),
            result.total,
            result.next_cursor,
        )

    def resolve_value(self, fence: object, path: object) -> PrivateProjectedValue:
        self.validate_inspection(fence)
        raise CaptureShapeUnsupportedError("CAPTURE value reads are not attached")

    def discover_table_columns(
        self, fence: object, path: object, limit: int,
    ) -> tuple[str, ...]:
        self.validate_inspection(fence)
        raise CaptureShapeUnsupportedError("CAPTURE value expansion is not attached")

    def _wait(self, ticket: _CaptureTicket) -> object:
        try:
            with self._wait_handoff():
                return ticket.wait_initiator()
        except KeyboardInterrupt:
            ticket.detach_waiter()
            raise

    def _validate_current(self) -> None:
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

    @staticmethod
    def _require_variable_root(request: ValueInspectionRequest) -> None:
        if (
            not isinstance(request, ValueInspectionRequest)
            or request.path.root.kind not in {ValueRootKind.CONTEXT, ValueRootKind.FRAME}
            or request.path.segments
            or request.view is not ValueViewKind.VARIABLES
            or request.role is not VariableRole.VARIABLES
            or request.exact is not None and not isinstance(request.exact, str)
        ):
            raise CaptureShapeUnsupportedError(
                "CAPTURE projection supports only root variable reads"
            )

    def _selected_variable(
        self, request: ValueInspectionRequest, native_level: int,
    ) -> PrivateValueProjection:
        name = request.exact
        assert isinstance(name, str)
        result = self._wait(self._controller.submit_capture_variable(
            name, stack_level=native_level,
        ))
        self._validate_current()
        if not isinstance(result, FrameVariable):
            raise ProtocolError("CAPTURE variable result is invalid")
        try:
            checked_name = SafePathSegment(
                ValuePathSegmentKind.VARIABLE, result.name,
            ).key
            if checked_name.casefold() != name.casefold():
                raise CaptureValueCheckError("CAPTURE variable result is invalid")
            size = result.collection_size
            if size is not None and (type(size) is not int or size < 0 or size > 10_000):
                raise CaptureValueCheckError("CAPTURE variable result is invalid")
            metadata = ValueMetadata(
                result.type_name, "<captured value>", size, ValueShape.UNDOCUMENTED,
            )
        except CaptureValueCheckError:
            raise
        except Exception as error:
            raise CaptureValueCheckError("CAPTURE variable result is invalid") from error
        return PrivateValueProjection(
            (PrivateProjectedValue(checked_name, lambda: metadata),), 1, None,
        )

    @staticmethod
    def _validate_page(
        page: NativeVariablePage, request: ValueInspectionRequest,
    ) -> None:
        if (
            type(page.names) is not tuple
            or type(page.total) is not int
            or page.total < len(page.names)
            or len(page.names) > request.stop - request.start
            or page.next_cursor is not None and (
                type(page.next_cursor) is not int
                or page.next_cursor != request.start + len(page.names)
                or page.next_cursor >= page.total
            )
        ):
            raise CaptureValueCheckError("CAPTURE variable page is invalid")
        try:
            names = tuple(
                SafePathSegment(ValuePathSegmentKind.VARIABLE, name).key
                for name in page.names
            )
        except Exception as error:
            raise CaptureValueCheckError("CAPTURE variable page is invalid") from error
        if len({name.casefold() for name in names}) != len(names):
            raise CaptureValueCheckError("CAPTURE variable page is ambiguous")


__all__ = ["CaptureTicketValueProjection", "CaptureVariablePageController"]
