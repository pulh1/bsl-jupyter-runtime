"""One user CAPTURE cell as a single arbiter worker plan.

This slice owns the primary expression and mandatory local operation ordering.
Worker pin, dirty-root tracking, message transfer and materialization still
belong to the enclosing CAPTURE policy and resource owners.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from onec_runtime.execution.arbiter import OutcomeUnknown, Settlement
from onec_runtime.execution.capture.cell_evaluator import CaptureCellEvaluator
from onec_runtime.execution.capture.scope import (
    CaptureContextState,
    CaptureFrameIdentity,
    CaptureScope,
)
from onec_runtime.execution.evaluation import EvaluationPort, EvaluationSuspended
from onec_runtime.rdbg.models import EvaluationResult, ModuleLocation


class CaptureCellWorkerPort(EvaluationPort, Protocol):
    """Owned RDBG capabilities used by workspace callbacks and cell eval."""

    def set_breakpoints(self, locations: tuple[ModuleLocation, ...]) -> None: ...


class CaptureOperationRepairRequired(OutcomeUnknown):
    """Mandatory resource work failed; keep the ticket, not a lost frame."""

    def __init__(self, stage: str, *, policy_error: BaseException | None = None) -> None:
        self.stage = stage
        self.policy_error = policy_error
        super().__init__(f"CAPTURE operation requires {stage} repair")


class ConfirmedTemporaryKeyCleanupFailure(RuntimeError):
    """Deletion of one tracked key was rejected with a confirmed outcome.

    The cleanup callback may raise this only after its remote capability has
    retired, or when it knows no deletion command was sent. It must have
    finished any mandatory cleanup before reporting this isolated key debt.
    Other cleanup errors may have unknown effects and keep the ticket owned.
    """

    def __init__(self, key: str) -> None:
        self.key = key
        self.policy_error: BaseException | None = None
        super().__init__("temporary key cleanup was rejected")


class TemporaryKeyCleanupOutcomeUnknown(OutcomeUnknown):
    """A deletion may have run; retain its exact RDBG owner and key debt.

    A cleanup callback should wrap an ambiguous RDBG error with this exception
    when it knows which tracked key the deletion targeted.
    """

    def __init__(self, key: str) -> None:
        self.key = key
        self.policy_error: BaseException | None = None
        super().__init__("temporary key cleanup outcome is unknown")


class CaptureCellOperationExecutor:
    """Run a cell without keeping the worker port or pending eval as state."""

    def __init__(self, evaluator: CaptureCellEvaluator) -> None:
        self._evaluator = evaluator

    def execute(
        self,
        scope: CaptureScope,
        lowered_source: str,
        *,
        port: CaptureCellWorkerPort,
        shield_workspace: Callable[[CaptureCellWorkerPort], None],
        restore_workspace: Callable[[CaptureCellWorkerPort], None],
        cleanup: Callable[[CaptureCellWorkerPort], None],
        result_policy: Callable[[EvaluationResult], object],
    ) -> Settlement:
        """Settle only after result, workspace restoration, and cleanup.

        A pending/unknown eval or debugger stop exits before dependent RDBG
        effects. Its capability remains with the arbiter ticket for the owner
        to reconcile. Result policy receives the raw confirmed BSL result and
        may report a cell failure without changing the CAPTURE frame. Callbacks
        must use the supplied worker port for any RDBG work. A confirmed,
        isolated temporary-key deletion error becomes scope debt and settles
        this cell; unknown deletion or unclassified cleanup errors retain its
        owner. Worker pin, dirty roots, message transfer and materialization
        are outside this slice.
        """

        if (
            scope.context_state is not CaptureContextState.READY
            or scope.frame_identity is not CaptureFrameIdentity.CONFIRMED
            or scope.kernel_stack_level is None
        ):
            raise RuntimeError("CAPTURE scope is not ready for user evaluation")

        shield_workspace(port)
        try:
            result = self._evaluator.evaluate(scope, lowered_source, port=port)
        except (OutcomeUnknown, EvaluationSuspended):
            raise
        except BaseException as error:
            # Even a confirmed pre-dispatch rejection leaves the acknowledged
            # breakpoint shield installed. Repair it before the next cell.
            raise CaptureOperationRepairRequired("workspace_restore") from error
        try:
            restore_workspace(port)
        except BaseException as error:
            raise CaptureOperationRepairRequired("workspace_restore") from error
        policy_error: BaseException | None = None
        value: object = None
        try:
            value = result_policy(result)
        except BaseException as error:
            policy_error = error
        try:
            cleanup(port)
        except TemporaryKeyCleanupOutcomeUnknown as error:
            try:
                scope.note_temporary_cleanup_unknown(error.key)
            except (KeyError, ValueError) as tracking_error:
                raise CaptureOperationRepairRequired(
                    "cleanup", policy_error=policy_error
                ) from tracking_error
            error.policy_error = policy_error
            raise
        except ConfirmedTemporaryKeyCleanupFailure as error:
            try:
                scope.note_temporary_cleanup_failure(error.key)
            except (KeyError, ValueError) as tracking_error:
                raise CaptureOperationRepairRequired(
                    "cleanup", policy_error=policy_error
                ) from tracking_error
            error.policy_error = policy_error
            raise
        except BaseException as error:
            raise CaptureOperationRepairRequired(
                "cleanup", policy_error=policy_error
            ) from error
        if policy_error is not None:
            raise policy_error
        return Settlement(value)
