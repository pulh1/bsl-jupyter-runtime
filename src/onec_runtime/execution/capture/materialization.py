"""One CAPTURE transfer and its temporary key on an owned RDBG worker.

The caller prepares a trusted plan matching CaptureMaterializationPlan and
owns public decoding (including DataFrame construction). This executor owns
the remote steps, workspace ordering, and private-key cleanup evidence.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from onec_runtime.capture import build_live_current_capture_call
from onec_runtime.errors import CaptureValueCheckError, ProtocolError
from onec_runtime.execution.arbiter import OutcomeUnknown, Settlement
from onec_runtime.execution.capture.operation_executor import (
    CaptureOperationRepairRequired,
    ConfirmedTemporaryKeyCleanupFailure,
    TemporaryKeyCleanupOutcomeUnknown,
)
from onec_runtime.execution.capture.scope import (
    CaptureContextState,
    CaptureFrameIdentity,
    CaptureScope,
)
from onec_runtime.execution.capture.resources import TemporaryCleanupState
from onec_runtime.execution.evaluation import (
    EvaluationPort,
    EvaluationSuspended,
    evaluate_until_result,
)
from onec_runtime.experiment import bsl_string_literal
from onec_runtime.rdbg.models import (
    EvaluationResult,
    ModuleLocation,
    PendingEvaluation,
    StopEvent,
)
from onec_runtime.table_value import evaluation_to_python


_PRIVATE_KEY = re.compile(
    r"__(?:onec_compact_table|onec_value|onec_projection|onec_materialization)_[0-9a-f]{32}"
)


@runtime_checkable
class CaptureMaterializationPlan(Protocol):
    """Read-only transfer fields accepted independently of their builder."""

    @property
    def instruction(self) -> str: ...

    @property
    def private_key(self) -> str: ...

    @property
    def cleanup_instruction(self) -> str: ...

    @property
    def max_text_size(self) -> int: ...

    @property
    def decode(self) -> Callable[[object, str], bytes]: ...

    @property
    def admit_metadata(self) -> Callable[[object], object]: ...


class CaptureMaterializationPort(EvaluationPort, Protocol):
    """The worker-confined RDBG capabilities needed for one transfer."""

    def set_breakpoints(self, locations: tuple[ModuleLocation, ...]) -> None: ...


class TemporaryKeyCleanupSuspended(EvaluationSuspended):
    """Cleanup stopped with the exact pending expression and debugger stop."""

    def __init__(
        self,
        key: str,
        pending: PendingEvaluation,
        stop: StopEvent,
        *,
        policy_error: BaseException | None,
    ) -> None:
        super().__init__(pending, stop)
        self.key = key
        self.policy_error = policy_error


class CaptureMaterializationExecutor:
    """Run primary admission, optional payload read, and key deletion once."""

    def __init__(
        self, *, request_timeout_s: float = 30.0, wait_interval_s: float = 6.0
    ) -> None:
        self._request_timeout_s = request_timeout_s
        self._wait_interval_s = wait_interval_s

    def execute(
        self,
        scope: CaptureScope,
        plan: CaptureMaterializationPlan,
        *,
        port: CaptureMaterializationPort,
        shield_workspace: Callable[[CaptureMaterializationPort], None],
        restore_workspace: Callable[[CaptureMaterializationPort], None],
        on_confirmed_failure: Callable[[BaseException], None] | None = None,
    ) -> Settlement:
        """Return private bytes only after cleanup is confirmed.

        A confirmed admission or decode failure still deletes the tracked key
        and settles only this request. An unknown remote step or failed
        mandatory workspace restore keeps the arbiter ticket for repair. The
        plan must come from a CAPTURE materialization policy and already carry
        its safe handle, budgets, generation checks, and cleanup instruction.
        The optional callback publishes a confirmed failure before the caller's
        ticket is settled; unknown remote outcomes never invoke it.
        """

        self._validate(scope, plan)
        key = plan.private_key
        scope.track_temporary_key(key)
        try:
            shield_workspace(port)
        except BaseException:
            # Workspace installation cannot create the transfer key.
            scope.confirm_temporary_cleanup(key)
            raise
        try:
            first = self._evaluate(
                port,
                build_live_current_capture_call(
                    plan.instruction + "\nРезультатИнструкции = Результат;"
                ),
                stack_level=scope.kernel_stack_level,
                max_text_size=plan.max_text_size,
            )
        except (OutcomeUnknown, EvaluationSuspended):
            raise
        except BaseException as error:
            raise CaptureOperationRepairRequired("workspace_restore") from error
        try:
            restore_workspace(port)
        except BaseException as error:
            raise CaptureOperationRepairRequired("workspace_restore") from error

        policy_error: BaseException | None = None
        payload: bytes | None = None
        if first.error_occurred:
            policy_error = CaptureValueCheckError("CAPTURE value admission failed")
        else:
            try:
                metadata = plan.admit_metadata(evaluation_to_python(first))
            except BaseException as error:
                policy_error = error
            else:
                try:
                    response = self._evaluate(
                        port,
                        "RuntimeKernelServer."
                        "ЗабратьКомпактнуюМатериализациюИзКонтекста(e1cRuntimeКонтекст, "
                        + bsl_string_literal(key)
                        + ")",
                        stack_level=scope.kernel_stack_level,
                        max_text_size=plan.max_text_size,
                    )
                except (OutcomeUnknown, EvaluationSuspended):
                    raise
                except BaseException as error:
                    raise CaptureOperationRepairRequired("payload_read") from error
                if response.error_occurred:
                    policy_error = CaptureValueCheckError(
                        "CAPTURE materialization payload is unavailable"
                    )
                else:
                    try:
                        content = evaluation_to_python(response)
                        if not isinstance(content, str) or len(content) > plan.max_text_size:
                            raise CaptureValueCheckError(
                                "CAPTURE materialization payload is invalid"
                            )
                        payload = plan.decode(metadata, content)
                        if not isinstance(payload, bytes):
                            raise CaptureValueCheckError(
                                "CAPTURE materialization payload is invalid"
                            )
                    except BaseException as error:
                        policy_error = error

        try:
            deletion = self._evaluate(
                port,
                build_live_current_capture_call(
                    plan.cleanup_instruction + "\nРезультатИнструкции = Результат;"
                ),
                stack_level=scope.kernel_stack_level,
                max_text_size=plan.max_text_size,
            )
        except EvaluationSuspended as error:
            scope.note_temporary_cleanup_unknown(key)
            raise TemporaryKeyCleanupSuspended(
                key, error.pending, error.stop, policy_error=policy_error
            ) from error
        except BaseException as error:
            scope.note_temporary_cleanup_unknown(key)
            unknown = TemporaryKeyCleanupOutcomeUnknown(key)
            unknown.policy_error = policy_error
            raise unknown from error
        if deletion.error_occurred:
            scope.note_temporary_cleanup_failure(key)
            failure = ConfirmedTemporaryKeyCleanupFailure(key)
            failure.policy_error = policy_error
            if on_confirmed_failure is not None:
                on_confirmed_failure(failure)
            raise failure
        scope.confirm_temporary_cleanup(key)
        if policy_error is not None:
            if on_confirmed_failure is not None:
                on_confirmed_failure(policy_error)
            raise policy_error
        assert payload is not None
        return Settlement(payload)

    def _evaluate(
        self,
        port: CaptureMaterializationPort,
        expression: str,
        *,
        stack_level: int,
        max_text_size: int,
    ) -> EvaluationResult:
        return evaluate_until_result(
            port,
            expression,
            stack_level=stack_level,
            max_text_size=max_text_size,
            request_timeout_s=self._request_timeout_s,
            wait_interval_s=self._wait_interval_s,
        )

    def retry_confirmed_cleanup(
        self,
        scope: CaptureScope,
        key: str,
        *,
        port: CaptureMaterializationPort,
        shield_workspace: Callable[[CaptureMaterializationPort], None],
        restore_workspace: Callable[[CaptureMaterializationPort], None],
    ) -> Settlement:
        """Retry only a proved rejection of the idempotent private-key delete.

        An ambiguous deletion belongs to its original arbiter ticket and must
        be reconciled there. This operation never repeats admission or payload
        transfer, so a value-producing expression cannot run twice.
        """

        if (
            scope.context_state is not CaptureContextState.READY
            or scope.frame_identity is not CaptureFrameIdentity.CONFIRMED
            or scope.kernel_stack_level is None
            or not isinstance(key, str)
            or _PRIVATE_KEY.fullmatch(key) is None
            or not any(
                debt.key == key and debt.state is TemporaryCleanupState.CONFIRMED_FAILURE
                for debt in scope.temporary_cleanup_debts
            )
        ):
            raise ProtocolError("No confirmed CAPTURE cleanup failure can be retried")

        try:
            shield_workspace(port)
        except BaseException as error:
            raise CaptureOperationRepairRequired("workspace_restore") from error
        expression = build_live_current_capture_call(
            "e1cRuntimeКонтекст.Удалить(" + bsl_string_literal(key) + ");\n"
            "Результат = Истина;\nРезультатИнструкции = Результат;"
        )
        try:
            deletion = self._evaluate(
                port,
                expression,
                stack_level=scope.kernel_stack_level,
                max_text_size=2048,
            )
        except (OutcomeUnknown, EvaluationSuspended):
            scope.note_temporary_cleanup_unknown(key)
            raise
        except BaseException as error:
            scope.note_temporary_cleanup_unknown(key)
            raise TemporaryKeyCleanupOutcomeUnknown(key) from error

        if not deletion.error_occurred:
            scope.confirm_temporary_cleanup(key)
        try:
            restore_workspace(port)
        except BaseException as error:
            raise CaptureOperationRepairRequired("workspace_restore") from error
        if deletion.error_occurred:
            raise ConfirmedTemporaryKeyCleanupFailure(key)
        return Settlement(None)

    @staticmethod
    def _validate(scope: CaptureScope, plan: CaptureMaterializationPlan) -> None:
        if (
            scope.context_state is not CaptureContextState.READY
            or scope.frame_identity is not CaptureFrameIdentity.CONFIRMED
            or scope.kernel_stack_level is None
        ):
            raise RuntimeError("CAPTURE scope is not ready for materialization")
        if not isinstance(plan, CaptureMaterializationPlan):
            raise TypeError("CAPTURE transfer plan is required")
        if (
            not isinstance(plan.private_key, str)
            or _PRIVATE_KEY.fullmatch(plan.private_key) is None
            or not isinstance(plan.instruction, str)
            or not plan.instruction
            or not isinstance(plan.cleanup_instruction, str)
            or not plan.cleanup_instruction
            or type(plan.max_text_size) is not int
            or plan.max_text_size <= 0
            or not callable(plan.admit_metadata)
            or not callable(plan.decode)
        ):
            raise ProtocolError("CAPTURE transfer plan is invalid")
