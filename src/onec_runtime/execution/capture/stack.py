"""Fenced saved stack inventory for the existing CAPTURE stack descriptor.

Stack pages need no new debugger request: the stop already supplied physical
frames. This internal backend supplies those frames to LocalStackAdapter,
which maps public frames and masks runtime-kernel coordinates. The owning
controller must validate the active route/stop in ``validate_current``.
"""

from __future__ import annotations

from collections.abc import Callable

from onec_runtime.errors import ProtocolError, StaleCaptureError
from onec_runtime.execution.capture.scope import (
    CaptureContextState,
    CaptureFrameIdentity,
    CaptureScope,
)
from onec_runtime.rdbg.models import ModuleLocation, StackFrame


class CaptureStackInventoryAdapter:
    """Read one scope's saved physical frames under its owner's fence."""

    def __init__(
        self,
        scope: CaptureScope,
        fence: object,
        *,
        validate_current: Callable[[object], None],
    ) -> None:
        if not isinstance(scope, CaptureScope):
            raise TypeError("CAPTURE scope is required")
        if not callable(validate_current):
            raise TypeError("CAPTURE stack fence validator is required")
        kernel = next(
            (
                item for item in scope.stop.stack_frames
                if item.level == scope.kernel_stack_level
            ),
            None,
        )
        if kernel is None:
            raise ProtocolError("CAPTURE kernel frame is unavailable")
        self._scope = scope
        self._fence = fence
        self._validate_current = validate_current
        self._kernel_location = kernel.location

    def read_stack(self, fence: object) -> tuple[StackFrame, ...]:
        """Return internal frames only; the public adapter must mask them."""

        if fence != self._fence:
            raise StaleCaptureError()
        self._validate_current(fence)
        scope = self._scope
        if (
            scope.context_state is not CaptureContextState.READY
            or scope.frame_identity is not CaptureFrameIdentity.CONFIRMED
            or scope.inspection_target_id != scope.identity.target_id
            or not scope.published
        ):
            raise StaleCaptureError()
        frames = scope.stack_frames
        if (
            type(frames) is not tuple
            or not frames
            or any(
                type(frame) is not StackFrame
                or frame.target_id != scope.identity.target_id
                or type(frame.level) is not int
                or frame.level < 0
                for frame in frames
            )
            or tuple(frame.level for frame in frames)
            != tuple(sorted({frame.level for frame in frames}))
            or frames[0].level != 0
            or frames[0].location != scope.identity.location
            or not any(
                frame.level == scope.kernel_stack_level
                and self._same_module(frame.location, self._kernel_location)
                for frame in frames
            )
        ):
            raise ProtocolError("CAPTURE stack inventory is invalid")
        self._validate_current(fence)
        return frames

    def is_runtime_frame(self, frame: StackFrame) -> bool:
        """Classify every physical frame in the same module as the kernel."""

        return self._same_module(frame.location, self._kernel_location)

    @staticmethod
    def _same_module(left: ModuleLocation, right: ModuleLocation) -> bool:
        return (
            left.module_type == right.module_type
            and left.url == right.url
            and left.object_id == right.object_id
            and left.property_id == right.property_id
            and left.extension_name == right.extension_name
            and left.ext_id == right.ext_id
        )
