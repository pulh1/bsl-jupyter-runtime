"""Bounded CAPTURE variable projection through controller-owned tickets.

RDBG value presentations are private input. Bounded pages expose checked
name/type/size metadata with an opaque preview; unavailable entries retain only
their names. Exact-name lookup uses a separate owned ticket. Values cannot be
expanded until a per-value materializer is bound to this component path.
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
    MAX_TYPE_CHARS,
    ValueRoot,
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
from onec_runtime.execution.capture.inspection import (
    MAX_NATIVE_VARIABLE_INVENTORY, TypedNativeVariable, TypedNativeVariablePage,
)
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

    def submit_capture_typed_variable_page(
        self, *, stack_level: int, start: int, stop: int,
        role: VariableRole = VariableRole.VARIABLES,
        parameter_names: tuple[str, ...] = (),
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
        resolve_parameters: Callable[[ValueRoot], tuple[str, ...]] | None = None,
    ) -> None:
        if not isinstance(scope, CaptureScope):
            raise TypeError("CAPTURE scope is required")
        if not isinstance(policy, CaptureValuePolicy):
            raise TypeError("CAPTURE value policy is invalid")
        if not callable(wait_handoff):
            raise TypeError("CAPTURE ticket wait handoff is invalid")
        if resolve_parameters is not None and not callable(resolve_parameters):
            raise TypeError("CAPTURE parameter resolver is invalid")
        self._controller = controller
        self._scope = scope
        self._fence = scope.identity
        self._wait_handoff = wait_handoff
        self._adapter = LocalCaptureValueAdapter(
            self, self._fence, policy=policy,
            resolve_parameters=(
                resolve_parameters if resolve_parameters is not None
                else lambda _root: ()
            ),
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
        native_level = (
            request.path.root.native_level
            if request.path.root.kind is ValueRootKind.FRAME
            else self._scope.frame_stack_level
        )
        if type(native_level) is not int or native_level < 0:
            raise StaleCaptureError()
        if isinstance(request.exact, str):
            return self._selected_variable(request, native_level)
        result = self._wait(self._controller.submit_capture_typed_variable_page(
            stack_level=native_level, start=request.start, stop=request.stop,
            role=request.role, parameter_names=request.parameter_names,
        ))
        self._validate_current()
        if not isinstance(result, TypedNativeVariablePage):
            raise ProtocolError("CAPTURE typed variable page result is invalid")
        self._validate_typed_page(result, request)
        entries = []
        for item in result.variables:
            if item.type_name is None:
                entries.append(PrivateProjectedValue(
                    item.name, lambda: None, unavailable=True,
                ))
            else:
                metadata = ValueMetadata(
                    item.type_name, "<captured value>", item.collection_size,
                    ValueShape.UNDOCUMENTED,
                )
                entries.append(PrivateProjectedValue(
                    item.name, lambda metadata=metadata: metadata,
                ))
        return PrivateValueProjection(
            tuple(entries),
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
            or type(request.role) is not VariableRole
            or request.exact is not None and not isinstance(request.exact, str)
            or type(request.parameter_names) is not tuple
            or request.role is VariableRole.VARIABLES and request.parameter_names
        ):
            raise CaptureShapeUnsupportedError(
                "CAPTURE projection supports only root variable reads"
            )

    def _selected_variable(
        self, request: ValueInspectionRequest, native_level: int,
    ) -> PrivateValueProjection:
        name = request.exact
        assert isinstance(name, str)
        parameter_names = {item.casefold() for item in request.parameter_names}
        if (
            request.role is VariableRole.PARAMETERS
            and name.casefold() not in parameter_names
            or request.role is VariableRole.LOCALS
            and name.casefold() in parameter_names
        ):
            return PrivateValueProjection((), 0, None)
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
    def _validate_typed_page(
        page: TypedNativeVariablePage, request: ValueInspectionRequest,
    ) -> None:
        if (
            type(page.variables) is not tuple
            or type(page.total) is not int
            or not 0 <= page.total <= MAX_NATIVE_VARIABLE_INVENTORY
            or page.total < len(page.variables)
            or len(page.variables) > request.stop - request.start
            or page.next_cursor is not None and (
                type(page.next_cursor) is not int
                or page.next_cursor != request.start + len(page.variables)
                or page.next_cursor >= page.total
            )
        ):
            raise CaptureValueCheckError("CAPTURE variable page is invalid")
        try:
            names = tuple(
                SafePathSegment(ValuePathSegmentKind.VARIABLE, item.name).key
                for item in page.variables
            )
        except Exception as error:
            raise CaptureValueCheckError("CAPTURE variable page is invalid") from error
        if len({name.casefold() for name in names}) != len(names):
            raise CaptureValueCheckError("CAPTURE variable page is ambiguous")
        for item in page.variables:
            if not isinstance(item, TypedNativeVariable):
                raise CaptureValueCheckError("CAPTURE variable page is invalid")
            if item.type_name is None:
                if item.collection_size is not None:
                    raise CaptureValueCheckError("CAPTURE unavailable variable is invalid")
                continue
            if (
                not isinstance(item.type_name, str)
                or not 0 < len(item.type_name) <= MAX_TYPE_CHARS
                or item.collection_size is not None
                and (type(item.collection_size) is not int
                     or not 0 <= item.collection_size <= MAX_NATIVE_VARIABLE_INVENTORY)
            ):
                raise CaptureValueCheckError("CAPTURE variable metadata is invalid")
        if request.role is VariableRole.PARAMETERS:
            expected = request.parameter_names[request.start:request.stop]
            if (
                page.total != len(request.parameter_names)
                or tuple(name.casefold() for name in names)
                != tuple(name.casefold() for name in expected)
            ):
                raise CaptureValueCheckError("CAPTURE parameter page is invalid")
        elif request.role is VariableRole.LOCALS:
            parameters = {name.casefold() for name in request.parameter_names}
            if any(name.casefold() in parameters for name in names):
                raise CaptureValueCheckError("CAPTURE local page is invalid")


__all__ = ["CaptureTicketValueProjection", "CaptureVariablePageController"]
