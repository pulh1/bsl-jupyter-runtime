"""Private CAPTURE inspection calls through one owned RDBG worker port."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from onec_runtime.errors import ProtocolError
from onec_runtime.execution.capture.scope import (
    CaptureContextState,
    CaptureFrameIdentity,
    CaptureScope,
)
from onec_runtime.execution.evaluation import EvaluationPort, evaluate_until_result
from onec_runtime.rdbg.models import (
    EvaluationResult,
    FrameVariable,
    LocalVariablesResult,
    ModuleLocation,
)


class CaptureInspectionPort(EvaluationPort, Protocol):
    """Worker-confined local read and expression evaluation capabilities."""

    def local_variables(
        self, stack_level: int = 0, *, timeout_s: float = 30.0
    ) -> LocalVariablesResult: ...


class CaptureInspectionUnavailable(ProtocolError):
    """A confirmed inspection failure that does not invalidate the frame."""


class CaptureInspectionExecutor:
    """Read private frame data while route admission belongs to the caller."""

    def __init__(
        self, *, request_timeout_s: float = 30.0, wait_interval_s: float = 6.0
    ) -> None:
        self._request_timeout_s = request_timeout_s
        self._wait_interval_s = wait_interval_s

    def read_variable(
        self,
        scope: CaptureScope,
        name: str,
        *,
        stack_level: int,
        port: CaptureInspectionPort,
    ) -> FrameVariable:
        """Read one named variable afresh; callers must bound public values.

        The controller/arbiter validates the route fence before this worker
        plan starts. A confirmed RDBG error remains a request failure; an
        ambiguous transport exception propagates so the ticket keeps owner.
        """

        self._require_frame(scope, stack_level, allow_kernel=False)
        if not isinstance(name, str) or len(name) > 256 or not name.isidentifier():
            raise ValueError("CAPTURE variable name is invalid")
        response = port.local_variables(
            stack_level=stack_level, timeout_s=self._request_timeout_s
        )
        if not isinstance(response, LocalVariablesResult) or response.error_occurred:
            raise CaptureInspectionUnavailable("CAPTURE frame variables are unavailable")
        matches = tuple(
            variable for variable in response.variables
            if isinstance(variable, FrameVariable)
            and variable.name.casefold() == name.casefold()
        )
        if len(matches) != 1:
            raise CaptureInspectionUnavailable("CAPTURE variable is unavailable")
        return matches[0]

    def evaluate_helper(
        self,
        scope: CaptureScope,
        source: str,
        *,
        stack_level: int,
        max_text_size: int = 307_200,
        port: CaptureInspectionPort,
        result_policy: Callable[[EvaluationResult], object],
    ) -> object:
        """Run one trusted helper expression and apply policy on the worker.

        The source must come from the CAPTURE inspection policy, which limits
        paths and payload size. Confirmed BSL errors are rejected here; the
        supplied pure policy decodes a successful result and bounds public values.
        Empty wait intervals reuse one pending capability without a BSL
        execution deadline. A stop or unknown outcome propagates with arbiter
        ownership intact.
        """

        self._require_frame(scope, stack_level, allow_kernel=True)
        if not isinstance(source, str) or not source:
            raise ValueError("CAPTURE helper source is invalid")
        if type(max_text_size) is not int or max_text_size <= 0:
            raise ValueError("CAPTURE helper text limit is invalid")
        if not callable(result_policy):
            raise TypeError("CAPTURE helper result policy is required")
        result = evaluate_until_result(
            port,
            source,
            stack_level=stack_level,
            max_text_size=max_text_size,
            request_timeout_s=self._request_timeout_s,
            wait_interval_s=self._wait_interval_s,
        )
        if result.error_occurred:
            raise CaptureInspectionUnavailable("CAPTURE helper evaluation failed")
        return result_policy(result)

    @staticmethod
    def _require_frame(
        scope: CaptureScope, stack_level: int, *, allow_kernel: bool
    ) -> None:
        if (
            scope.context_state is not CaptureContextState.READY
            or scope.frame_identity is not CaptureFrameIdentity.CONFIRMED
            or scope.inspection_target_id != scope.identity.target_id
        ):
            raise RuntimeError("CAPTURE scope is not ready for inspection")
        if type(stack_level) is not int:
            raise ValueError("CAPTURE frame level is unavailable")
        frame = next(
            (
                item for item in scope.stack_frames
                if item.level == stack_level
                and item.target_id == scope.identity.target_id
            ),
            None,
        )
        if frame is None:
            raise ValueError("CAPTURE frame level is unavailable")
        if not allow_kernel:
            kernel = next(
                (
                    item for item in scope.stack_frames
                    if item.level == scope.kernel_stack_level
                ),
                None,
            )
            if kernel is None or CaptureInspectionExecutor._same_module(
                frame.location, kernel.location
            ):
                raise ValueError("Runtime kernel variables are private")

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
