"""Ordered setup of one CAPTURE stop through a debugger capability port."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from onec_runtime.capture import (
    build_capture_transfer_call,
    build_live_capture_begin_call,
    build_live_capture_end_call,
)
from onec_runtime.errors import BslExecutionError, ProtocolError
from onec_runtime.execution.capture.scope import CaptureContextState, CaptureScope
from onec_runtime.rdbg.models import (
    EvaluationResult,
    FrameVariable,
    LocalVariablesResult,
    ModuleLocation,
    StopEvent,
)
from onec_runtime.table_value import evaluation_to_python


class CaptureRdbgPort(Protocol):
    """The setup capabilities supplied by the runtime's RDBG owner."""

    def local_variables(self, stack_level: int = 0) -> LocalVariablesResult: ...

    def evaluate(
        self, expression: str, *, stack_level: int = 0
    ) -> EvaluationResult: ...


class CaptureCommandMismatchError(ProtocolError):
    """The stopped kernel frame belongs to another MAIN command."""


@dataclass(frozen=True, slots=True)
class CaptureSetupResult:
    variables: tuple[FrameVariable, ...]
    observed_command_id: object


class CaptureExecutor:
    """Owns target calls for opening a CAPTURE context, without route state."""

    def __init__(
        self,
        rdbg: CaptureRdbgPort | None,
        kernel_location: ModuleLocation,
        *,
        decode_command_id: Callable[[EvaluationResult], object],
    ) -> None:
        self._rdbg = rdbg
        self._kernel_location = kernel_location
        self._decode_command_id = decode_command_id

    def open_scope(
        self, scope: CaptureScope, *, port: CaptureRdbgPort | None = None
    ) -> CaptureSetupResult:
        """Collect the business frame and confirm the suspended MAIN identity."""

        rdbg = self._port(port)
        local_result = rdbg.local_variables(stack_level=0)
        if local_result.error_occurred:
            raise ProtocolError(local_result.error_text)
        scope.record_locals(tuple(local_result.variables))

        transfer = rdbg.evaluate(
            build_capture_transfer_call(
                variable.name for variable in local_result.variables
            )
        )
        if transfer.error_occurred:
            raise BslExecutionError(transfer.error_text)
        address = evaluation_to_python(transfer)
        if not isinstance(address, str) or not address:
            raise ProtocolError("Capture temporary-storage address is invalid")
        scope.record_transfer(address)

        stack_level = self._locate_kernel_context_frame(scope.stop, rdbg)
        scope.record_kernel_frame(stack_level)
        command_evidence = rdbg.evaluate(
            "ИдентификаторКоманды", stack_level=stack_level
        )
        if command_evidence.error_occurred:
            raise BslExecutionError(command_evidence.error_text)
        observed_command_id = self._decode_command_id(command_evidence)
        if not scope.record_main_command(observed_command_id):
            raise CaptureCommandMismatchError(
                f"Captured command {observed_command_id!r} does not match active "
                f"operation {scope.identity.main_command_id}"
            )

        begin = rdbg.evaluate(
            build_live_capture_begin_call(address), stack_level=stack_level
        )
        if begin.error_occurred:
            raise BslExecutionError(begin.error_text)
        scope.record_context_begun()
        # The controller publishes READY only after installing the operation
        # owner. A successful remote begin alone cannot admit CAPTURE cells.
        return CaptureSetupResult(scope.frame_variables, observed_command_id)

    def end_scope(
        self, scope: CaptureScope, *, port: CaptureRdbgPort | None = None
    ) -> None:
        """End the live context; controller releases the frame after Continue."""

        if (
            scope.context_state is not CaptureContextState.READY
            or scope.kernel_stack_level is None
        ):
            raise ProtocolError("A ready CAPTURE context is required")
        result = self._port(port).evaluate(
            build_live_capture_end_call(), stack_level=scope.kernel_stack_level
        )
        if result.error_occurred:
            raise BslExecutionError(result.error_text)

    def _port(self, port: CaptureRdbgPort | None) -> CaptureRdbgPort:
        selected = self._rdbg if port is None else port
        if selected is None:
            raise RuntimeError("CAPTURE RDBG port is not bound")
        return selected

    def _locate_kernel_context_frame(
        self, stop: StopEvent, rdbg: CaptureRdbgPort
    ) -> int:
        required = {
            "e1cruntimeконтекст", "текущаяинструкция", "идентификаторкоманды",
        }
        stack = stop.stack
        frames = stop.stack_frames
        if not stack or not frames:
            raise ProtocolError("Capture stop has no exact stack mapping for runtime kernel")
        if (
            len(frames) != len(stack)
            or tuple(frame.location for frame in frames) != stack
            or any(frame.level < 0 for frame in frames)
            or len({frame.level for frame in frames}) != len(frames)
        ):
            raise ProtocolError("Capture stop stack mapping is incoherent")
        for frame in frames:
            stack_level = frame.level
            if stack_level <= 0 or stack_level >= 8:
                continue
            if not self._same_kernel_module(frame.location):
                continue
            local_result = rdbg.local_variables(stack_level=stack_level)
            if local_result.error_occurred:
                continue
            names = {variable.name.casefold() for variable in local_result.variables}
            if required.issubset(names):
                return stack_level
        raise ProtocolError("Runtime kernel context frame was not found")

    def _same_kernel_module(self, location: ModuleLocation) -> bool:
        kernel = self._kernel_location
        return (
            location.module_type == kernel.module_type
            and location.url == kernel.url
            and location.object_id == kernel.object_id
            and location.property_id == kernel.property_id
            and location.extension_name == kernel.extension_name
            and location.ext_id == kernel.ext_id
        )
