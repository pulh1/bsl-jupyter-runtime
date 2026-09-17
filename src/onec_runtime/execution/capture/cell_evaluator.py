"""Primary user CAPTURE eval over the current arbiter worker capability."""

from __future__ import annotations

from onec_runtime.capture import build_live_current_capture_call
from onec_runtime.execution.capture.scope import (
    CaptureContextState,
    CaptureFrameIdentity,
    CaptureScope,
)
from onec_runtime.execution.evaluation import EvaluationPort, evaluate_until_result
from onec_runtime.rdbg.models import EvaluationResult


class CaptureCellEvaluator:
    """Dispatch one lowered cell expression within one ready CAPTURE scope.

    The caller supplies the worker-bound port for each operation. This adapter
    returns the raw evaluation result; result policy, messages, workspace and
    cleanup belong to the enclosing CAPTURE operation.
    """

    def __init__(
        self,
        *,
        request_timeout_s: float = 30.0,
        wait_interval_s: float = 6.0,
        max_text_size: int = 307_200,
    ) -> None:
        self._request_timeout_s = request_timeout_s
        self._wait_interval_s = wait_interval_s
        self._max_text_size = max_text_size

    def evaluate(
        self,
        scope: CaptureScope,
        lowered_source: str,
        *,
        port: EvaluationPort,
    ) -> EvaluationResult:
        if (
            scope.context_state is not CaptureContextState.READY
            or scope.frame_identity is not CaptureFrameIdentity.CONFIRMED
            or scope.kernel_stack_level is None
        ):
            raise RuntimeError("CAPTURE scope is not ready for user evaluation")
        return evaluate_until_result(
            port,
            build_live_current_capture_call(lowered_source),
            stack_level=scope.kernel_stack_level,
            max_text_size=self._max_text_size,
            request_timeout_s=self._request_timeout_s,
            wait_interval_s=self._wait_interval_s,
        )
