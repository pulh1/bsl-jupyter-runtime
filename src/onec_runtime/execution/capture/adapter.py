"""Bridge a per-stop owned RDBG port to CAPTURE setup's narrow interface."""

from __future__ import annotations

from typing import Protocol

from onec_runtime.execution.evaluation import EvaluationPort, evaluate_until_result
from onec_runtime.rdbg.models import EvaluationResult, LocalVariablesResult


class CaptureSetupWorkerPort(EvaluationPort, Protocol):
    """Capabilities supplied by the arbiter worker for one CAPTURE stop."""

    def local_variables(self, stack_level: int = 0) -> LocalVariablesResult: ...


class CaptureSetupAdapter:
    """Run each setup expression once and wait on its exact pending capability.

    The supplied port owns worker confinement and protocol correlation. Each
    wait has a finite transport interval; the BSL expression has no deadline.
    """

    def __init__(
        self,
        port: CaptureSetupWorkerPort,
        *,
        request_timeout_s: float = 30.0,
        wait_interval_s: float = 6.0,
    ) -> None:
        self._port = port
        self._request_timeout_s = request_timeout_s
        self._wait_interval_s = wait_interval_s

    def local_variables(self, stack_level: int = 0) -> LocalVariablesResult:
        return self._port.local_variables(stack_level=stack_level)

    def evaluate(self, expression: str, *, stack_level: int = 0) -> EvaluationResult:
        return evaluate_until_result(
            self._port,
            expression,
            stack_level=stack_level,
            request_timeout_s=self._request_timeout_s,
            wait_interval_s=self._wait_interval_s,
        )
