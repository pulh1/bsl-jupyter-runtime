"""One user CAPTURE cell as a single arbiter worker plan.

This slice owns the primary expression and mandatory local operation ordering.
Worker pin, dirty-root tracking and materialization still belong to the
enclosing CAPTURE policy and resource owners. Message transfer follows the
primary eval on the same arbiter-owned port.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from onec_runtime.execution.arbiter import ConfirmedFailure, OutcomeUnknown, Settlement
from onec_runtime.execution.capture.cell_evaluator import CaptureCellEvaluator
from onec_runtime.execution.capture.messages import (
    CaptureMessageBslFailure, CaptureMessageCollector,
    CaptureMessageDecodeFailure,
)
from onec_runtime.execution.capture.scope import (
    CaptureContextState,
    CaptureFrameIdentity,
    CaptureScope,
    CaptureStopIdentity,
)
from onec_runtime.execution.evaluation import EvaluationPort, EvaluationSuspended
from onec_runtime.rdbg.models import EvaluationResult, ModuleLocation, PendingEvaluation


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


@dataclass(slots=True, repr=False)
class CaptureCellOperation:
    """One cell's confirmed result and repair progress, separate from its stop."""

    scope_identity: CaptureStopIdentity
    evaluation_started: bool = field(default=False, init=False)
    confirmed_result: EvaluationResult | None = field(default=None, init=False, repr=False)
    workspace_restored: bool = field(default=False, init=False)
    message_collector_key: str = field(default="", init=False, repr=False)
    message_collection_started: bool = field(default=False, init=False)
    message_collection_complete: bool = field(default=False, init=False)
    confirmed_message_result: EvaluationResult | None = field(default=None, init=False, repr=False)
    remote_outcome: object = field(default=None, init=False, repr=False)
    policy_started: bool = field(default=False, init=False)
    policy_value: object = field(default=None, init=False, repr=False)
    policy_error: BaseException | None = field(default=None, init=False, repr=False)
    cleanup_started: bool = field(default=False, init=False)
    cleanup_complete: bool = field(default=False, init=False)


class CaptureCellOperationExecutor:
    """Run a cell without keeping the worker port or pending eval as state."""

    def __init__(
        self, evaluator: CaptureCellEvaluator, *,
        message_collector: CaptureMessageCollector | None = None,
    ) -> None:
        self._evaluator = evaluator
        self._message_collector = message_collector

    def execute(
        self,
        scope: CaptureScope,
        lowered_source: str,
        *,
        port: CaptureCellWorkerPort,
        shield_workspace: Callable[[CaptureCellWorkerPort], None],
        restore_workspace: Callable[[CaptureCellWorkerPort], None],
        cleanup: Callable[[CaptureCellWorkerPort], None],
        result_policy: Callable[[object], object],
        operation: CaptureCellOperation | None = None,
        message_collector_key: str = "",
    ) -> Settlement | ConfirmedFailure:
        """Settle only after result, workspace restoration, and cleanup.

        A pending/unknown eval or debugger stop exits before dependent RDBG
        effects. Its capability remains with the arbiter ticket for the owner
        to reconcile. Result policy receives the raw BSL result by default, or
        CaptureRemoteOutcome when a message collector is configured. It may
        report a cell failure without changing the CAPTURE frame. Callbacks
        must use the supplied worker port for any RDBG work. A confirmed,
        isolated temporary-key deletion error becomes scope debt and settles
        this cell; unknown deletion or unclassified cleanup errors retain its
        owner. Worker pin, dirty roots and materialization
        are outside this slice.
        """

        if (
            scope.context_state is not CaptureContextState.READY
            or scope.frame_identity is not CaptureFrameIdentity.CONFIRMED
            or scope.kernel_stack_level is None
        ):
            raise RuntimeError("CAPTURE scope is not ready for user evaluation")

        record = operation or CaptureCellOperation(scope.identity)
        self._require_matching_operation(record, scope)
        if record.evaluation_started:
            raise RuntimeError("CAPTURE cell evaluation was already attempted")
        if self._message_collector is None and message_collector_key:
            raise ValueError("CAPTURE message collection requires a collector")
        record.message_collector_key = message_collector_key
        shield_workspace(port)
        record.evaluation_started = True
        try:
            result = self._evaluator.evaluate(scope, lowered_source, port=port)
        except (OutcomeUnknown, EvaluationSuspended):
            raise
        except BaseException as error:
            # Even a confirmed pre-dispatch rejection leaves the acknowledged
            # breakpoint shield installed. Repair it before the next cell.
            raise CaptureOperationRepairRequired("workspace_restore") from error
        record.confirmed_result = result
        return self._finish_confirmed_result(
            record, scope, port=port, restore_workspace=restore_workspace,
            cleanup=cleanup, result_policy=result_policy,
        )

    def repair_confirmed_result(
        self,
        operation: CaptureCellOperation,
        scope: CaptureScope,
        *,
        port: CaptureCellWorkerPort,
        restore_workspace: Callable[[CaptureCellWorkerPort], None],
        cleanup: Callable[[CaptureCellWorkerPort], None],
        result_policy: Callable[[object], object],
    ) -> Settlement | ConfirmedFailure:
        """Finish a saved result; never send its user eval expression again."""

        self._require_matching_operation(operation, scope)
        if operation.confirmed_result is None:
            raise RuntimeError("CAPTURE cell has no confirmed result to repair")
        return self._finish_confirmed_result(
            operation, scope, port=port, restore_workspace=restore_workspace,
            cleanup=cleanup, result_policy=result_policy,
        )

    def reconcile_pending_result(
        self,
        operation: CaptureCellOperation,
        scope: CaptureScope,
        pending: PendingEvaluation,
        *,
        port: CaptureCellWorkerPort,
        restore_workspace: Callable[[CaptureCellWorkerPort], None],
        cleanup: Callable[[CaptureCellWorkerPort], None],
        result_policy: Callable[[object], object],
    ) -> Settlement | ConfirmedFailure:
        """Finish the accepted expression identified by its pending capability."""

        self._require_matching_operation(operation, scope)
        if not operation.evaluation_started or operation.confirmed_result is not None:
            raise RuntimeError("CAPTURE cell has no pending result to reconcile")
        result = self._evaluator.await_pending(scope, pending, port=port)
        operation.confirmed_result = result
        return self._finish_confirmed_result(
            operation, scope, port=port, restore_workspace=restore_workspace,
            cleanup=cleanup, result_policy=result_policy,
        )

    def reconcile_pending_messages(
        self,
        operation: CaptureCellOperation,
        scope: CaptureScope,
        pending: PendingEvaluation,
        *,
        port: CaptureCellWorkerPort,
        restore_workspace: Callable[[CaptureCellWorkerPort], None],
        cleanup: Callable[[CaptureCellWorkerPort], None],
        result_policy: Callable[[object], object],
    ) -> Settlement | ConfirmedFailure:
        """Finish the exact pending collector without repeating either eval."""

        self._require_matching_operation(operation, scope)
        if (
            self._message_collector is None
            or operation.confirmed_result is None
            or not operation.workspace_restored
            or not operation.message_collection_started
            or operation.message_collection_complete
            or operation.confirmed_message_result is not None
        ):
            raise RuntimeError("CAPTURE cell has no pending message collection")
        message_result = self._message_collector.await_pending_result(port, pending)
        operation.confirmed_message_result = message_result
        self._complete_messages(operation, message_result)
        return self._finish_confirmed_result(
            operation, scope, port=port, restore_workspace=restore_workspace,
            cleanup=cleanup, result_policy=result_policy,
        )

    @staticmethod
    def _require_matching_operation(
        operation: CaptureCellOperation, scope: CaptureScope,
    ) -> None:
        if not isinstance(operation, CaptureCellOperation):
            raise TypeError("CAPTURE cell operation record is invalid")
        if operation.scope_identity != scope.identity:
            raise RuntimeError("CAPTURE cell operation belongs to another stop")
        if (
            scope.context_state is not CaptureContextState.READY
            or scope.frame_identity is not CaptureFrameIdentity.CONFIRMED
        ):
            raise RuntimeError("CAPTURE scope is not ready for cell repair")

    def _finish_confirmed_result(
        self,
        operation: CaptureCellOperation,
        scope: CaptureScope,
        *,
        port: CaptureCellWorkerPort,
        restore_workspace: Callable[[CaptureCellWorkerPort], None],
        cleanup: Callable[[CaptureCellWorkerPort], None],
        result_policy: Callable[[object], object],
    ) -> Settlement | ConfirmedFailure:
        result = operation.confirmed_result
        if result is None:
            raise RuntimeError("CAPTURE cell result is not confirmed")
        if not operation.workspace_restored:
            try:
                restore_workspace(port)
            except BaseException as error:
                raise CaptureOperationRepairRequired("workspace_restore") from error
            operation.workspace_restored = True

        if not operation.message_collection_complete:
            if operation.confirmed_message_result is not None:
                self._complete_messages(operation, operation.confirmed_message_result)
            elif operation.message_collection_started:
                raise CaptureOperationRepairRequired("message_collection")
            elif self._message_collector is None:
                operation.remote_outcome = result
                operation.message_collection_complete = True
            else:
                operation.message_collection_started = True
                try:
                    operation.remote_outcome = self._message_collector.collect(
                        port, result,
                        message_collector_key=operation.message_collector_key,
                        stack_level=scope.kernel_stack_level,
                    )
                except (CaptureMessageBslFailure, CaptureMessageDecodeFailure) as error:
                    operation.policy_started = True
                    operation.policy_error = error
                operation.message_collection_complete = operation.policy_error is not None or operation.remote_outcome is not None

        if not operation.policy_started:
            operation.policy_started = True
            try:
                operation.policy_value = result_policy(operation.remote_outcome)
            except BaseException as error:
                operation.policy_error = error

        if operation.cleanup_started and not operation.cleanup_complete:
            raise CaptureOperationRepairRequired(
                "cleanup", policy_error=operation.policy_error
            )
        if not operation.cleanup_started:
            operation.cleanup_started = True
            try:
                cleanup(port)
            except TemporaryKeyCleanupOutcomeUnknown as error:
                try:
                    scope.note_temporary_cleanup_unknown(error.key)
                except (KeyError, ValueError) as tracking_error:
                    raise CaptureOperationRepairRequired(
                        "cleanup", policy_error=operation.policy_error
                    ) from tracking_error
                error.policy_error = operation.policy_error
                raise
            except ConfirmedTemporaryKeyCleanupFailure as error:
                try:
                    scope.note_temporary_cleanup_failure(error.key)
                except (KeyError, ValueError) as tracking_error:
                    raise CaptureOperationRepairRequired(
                        "cleanup", policy_error=operation.policy_error
                    ) from tracking_error
                error.policy_error = operation.policy_error
                raise
            except BaseException as error:
                raise CaptureOperationRepairRequired(
                    "cleanup", policy_error=operation.policy_error
                ) from error
            operation.cleanup_complete = True

        if operation.policy_error is not None:
            return ConfirmedFailure(operation.policy_error)
        return Settlement(operation.policy_value)

    def _complete_messages(
        self, operation: CaptureCellOperation, message_result: EvaluationResult,
    ) -> None:
        collector = self._message_collector
        primary = operation.confirmed_result
        if collector is None or primary is None:
            raise RuntimeError("CAPTURE message result has no collector or primary eval")
        try:
            operation.remote_outcome = collector.complete(primary, message_result)
        except (CaptureMessageBslFailure, CaptureMessageDecodeFailure) as error:
            operation.policy_started = True
            operation.policy_error = error
        operation.message_collection_complete = True
