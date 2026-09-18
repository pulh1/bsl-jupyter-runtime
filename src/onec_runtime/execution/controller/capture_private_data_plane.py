"""Private CAPTURE plans for an already admitted scope and route.

The controller holds the admission lock and owns the arbiter ticket. This
service only retains selected-table recipes and builds worker-side plans.
"""

from __future__ import annotations

from typing import Callable
from uuid import uuid4

from onec_runtime.errors import ProtocolError
from onec_runtime.execution.arbiter import (
    ConfirmedFailure, OutcomeUnknown, Plan, ReadyForPolicy, SessionPort,
    Settlement,
)
from onec_runtime.execution.capture.evaluation_ledger import CaptureEvaluationLedger
from onec_runtime.execution.capture.manager_metadata import (
    CaptureManagerMetadataPlan, CaptureSelectedTableDescriptor,
    evaluate_capture_manager_metadata,
)
from onec_runtime.execution.capture.materialization import (
    CaptureMaterializationExecutor, CaptureMaterializationPlan,
)
from onec_runtime.execution.capture.operation_executor import CaptureOperationRepairRequired
from onec_runtime.execution.capture.scope import CaptureScope
from onec_runtime.execution.capture.selected_table_materialization import (
    CaptureSelectedTableTransferRequest,
)
from onec_runtime.execution.evaluation import EvaluationSuspended


class CapturePrivateDataPlane:
    """Keep private table recipes and build plans without owning RDBG."""

    def __init__(
        self,
        *,
        shield_workspace: Callable[[SessionPort], None],
        restore_workspace: Callable[[SessionPort], None],
    ) -> None:
        self.materialization_executor = CaptureMaterializationExecutor()
        self._shield_workspace = shield_workspace
        self._restore_workspace = restore_workspace
        self._descriptors: dict[str, CaptureSelectedTableDescriptor] = {}
        self._descriptor_scope: CaptureScope | None = None

    def register_descriptor(self, descriptor: CaptureSelectedTableDescriptor) -> str:
        """Called under the controller lock after exact-stop validation."""

        if self._descriptor_scope is not descriptor.scope:
            self._descriptors.clear()
            self._descriptor_scope = descriptor.scope
        if len(self._descriptors) >= 128:
            raise ProtocolError("CAPTURE selected table descriptor limit exceeded")
        handle = "capture_table_" + uuid4().hex
        self._descriptors[handle] = descriptor
        return handle

    def require_descriptor(
        self, handle: str, scope: CaptureScope,
    ) -> CaptureSelectedTableDescriptor:
        """Called under the controller lock after exact-stop validation."""

        if (
            type(handle) is not str
            or self._descriptor_scope is not scope
            or handle not in self._descriptors
        ):
            raise ProtocolError("CAPTURE selected table handle is stale or invalid")
        return self._descriptors[handle]

    def manager_metadata_plan(
        self,
        metadata_plan: CaptureManagerMetadataPlan,
        ledger: CaptureEvaluationLedger,
        receipt_id: str,
        *,
        check_current: Callable[[], None],
    ) -> Plan:
        """Recheck the worker fence, run the helper, and decode privately."""

        def plan(port: SessionPort) -> ReadyForPolicy | ConfirmedFailure:
            try:
                check_current()
            except Exception as error:
                ledger.fail(receipt_id, "CAPTURE manager metadata preparation failed")
                return ConfirmedFailure(error)
            self._shield_workspace(port)
            try:
                result = evaluate_capture_manager_metadata(metadata_plan, port)
            except (OutcomeUnknown, EvaluationSuspended):
                raise
            except BaseException as error:
                raise CaptureOperationRepairRequired("workspace_restore") from error
            try:
                self._restore_workspace(port)
            except BaseException as error:
                raise CaptureOperationRepairRequired("workspace_restore") from error
            try:
                decoded = metadata_plan.decode(result)
            except Exception as error:
                # Retire the record before this failure wakes its waiter.
                ledger.fail(receipt_id, "CAPTURE manager metadata helper failed")
                return ConfirmedFailure(error)
            return ReadyForPolicy(decoded)

        return plan

    def materialization_plan(
        self,
        scope: CaptureScope,
        transfer_plan: CaptureMaterializationPlan | CaptureSelectedTableTransferRequest,
        ledger: CaptureEvaluationLedger,
        receipt_id: str,
        *,
        runtime_generation: int,
        require_descriptor: Callable[[str, CaptureScope], CaptureSelectedTableDescriptor],
        before_first_effect: Callable[[], None] | None,
    ) -> Plan:
        """Prepare a selected table on the worker before remote effects."""

        def plan(port: SessionPort) -> ReadyForPolicy | ConfirmedFailure:
            try:
                if before_first_effect is not None:
                    before_first_effect()
                selected_plan = transfer_plan
                if isinstance(selected_plan, CaptureSelectedTableTransferRequest):
                    if selected_plan.runtime_generation != runtime_generation:
                        raise ProtocolError("CAPTURE selected table generation is stale")
                    descriptor = require_descriptor(selected_plan.handle, scope)
                    selected_plan = selected_plan.prepare(descriptor)
            except Exception as error:
                # Local rejection must retire the ledger before caller wakeup.
                ledger.fail(receipt_id, "CAPTURE materialization preparation failed")
                return ConfirmedFailure(error)
            result = self.materialization_executor.execute(
                scope, selected_plan, port=port,
                shield_workspace=self._shield_workspace,
                restore_workspace=self._restore_workspace,
                on_confirmed_failure=lambda _error: ledger.fail(
                    receipt_id, "CAPTURE materialization failed"
                ),
            )
            return ReadyForPolicy(result.value, next_route=result.next_route)

        return plan

    def cleanup_retry_plan(self, scope: CaptureScope, key: str) -> Plan:
        """Retry only a confirmed private-key deletion in this scope."""

        def plan(port: SessionPort) -> Settlement:
            return self.materialization_executor.retry_confirmed_cleanup(
                scope, key, port=port,
                shield_workspace=self._shield_workspace,
                restore_workspace=self._restore_workspace,
            )

        return plan
