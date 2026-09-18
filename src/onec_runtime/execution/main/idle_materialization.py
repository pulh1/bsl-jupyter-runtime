"""Direct value and table materialization while MAIN is idle."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from threading import RLock
from typing import Protocol
from uuid import uuid4

import pandas as pd

from onec_runtime.capture_evaluation import CaptureEvaluationKind, CaptureTransferPlan
from onec_runtime.compact_table_backend import CompactRuntimeTableTransfer
from onec_runtime.errors import CaptureValueCheckError, ProtocolError
from onec_runtime.execution.arbiter import (
    ExecutionTicket, OutcomeUnknown, RdbgArbiter, RouteToken, SessionPort,
    Settlement,
)
from onec_runtime.execution.evaluation import wait_for_pending_result
from onec_runtime.execution.worker_activation import WorkerMaterializationSnapshot
from onec_runtime.execution.value_transfer_plan import (
    build_bounded_projection_instruction,
    build_dynamic_materialization_instruction,
    build_projection_transfer_plan,
    classify_materialization_payload,
)
from onec_runtime.execution.value_reference import validate_public_direct_handle
from onec_runtime.experiment import bsl_string_literal
from onec_runtime.rdbg.models import EvaluationResult, TargetId
from onec_runtime.table_materialization import ReferencePolicy
from onec_runtime.table_value import evaluation_to_python
from onec_runtime.value_materialization import MaterializationOptions, decode_value_payload
from onec_runtime.value_transfer_backend import RuntimeValueTransfer
from onec_runtime.compact_table import decode_compact_table_payload


class _TransferPlan(Protocol):
    instruction: str
    private_key: str
    cleanup_instruction: str
    max_text_size: int

    def decode(self, metadata: object, content: str) -> bytes: ...

    def admit_metadata(self, metadata: object) -> object: ...


@dataclass(frozen=True, slots=True)
class MainIdleTargetFence:
    """The exact idle MAIN route and target admitted for one transfer."""

    route: RouteToken
    target: TargetId

    def __post_init__(self) -> None:
        if not isinstance(self.route, RouteToken) or not isinstance(self.target, TargetId):
            raise TypeError("MAIN-idle route and target fence is invalid")


class MainIdleMaterializationService:
    """Submit each direct transfer as one MAIN-idle arbiter ticket.

    The caller supplies the exact locally confirmed idle MAIN route and target.
    The plan rechecks that fence immediately before its first remote effect,
    then binds every evaluation capability to that target. The existing
    transfer backends continue to own BSL generation, envelopes, budgets and
    decoding.
    """

    def __init__(
        self,
        arbiter: RdbgArbiter,
        *,
        main_idle_fence: Callable[[], MainIdleTargetFence | None],
        runtime_generation: int,
        context_generation: int,
        worker_type_registrations: Callable[[], tuple[str, ...]] = lambda: (),
        worker_catalog_snapshot: (
            Callable[[], WorkerMaterializationSnapshot] | None
        ) = None,
        wait_handoff: Callable[[], AbstractContextManager[None]] = nullcontext,
    ) -> None:
        if not isinstance(arbiter, RdbgArbiter):
            raise TypeError("MAIN materialization requires an RDBG arbiter")
        if not callable(main_idle_fence) or not callable(worker_type_registrations):
            raise TypeError("MAIN materialization readers must be callable")
        if worker_catalog_snapshot is not None and not callable(worker_catalog_snapshot):
            raise TypeError("Worker materialization snapshot reader must be callable")
        if not callable(wait_handoff):
            raise TypeError("MAIN materialization wait handoff must be callable")
        if type(runtime_generation) is not int or runtime_generation <= 0:
            raise ValueError("MAIN materialization runtime generation must be positive")
        if type(context_generation) is not int or context_generation <= 0:
            raise ValueError("MAIN materialization context generation must be positive")
        self._arbiter = arbiter
        self._main_idle_fence = main_idle_fence
        self._runtime_generation = runtime_generation
        self._context_generation = context_generation
        self._worker_type_registrations = worker_type_registrations
        self._worker_catalog_snapshot = worker_catalog_snapshot
        self._wait_handoff = wait_handoff
        self._cleanup_lock = RLock()
        self._cleanup_debts: dict[str, ExecutionTicket] = {}

    @property
    def retryable_cleanup_keys(self) -> tuple[str, ...]:
        """Return keys whose confirmed deletion rejection may be retried."""

        with self._cleanup_lock:
            return tuple(self._cleanup_debts)

    def retry_cleanup(self, key: str) -> None:
        """Retry only a confirmed private-key deletion failure."""

        if not isinstance(key, str):
            raise TypeError("MAIN materialization cleanup key must be a string")
        with self._cleanup_lock:
            parent = self._cleanup_debts.get(key)
        if parent is None:
            raise ProtocolError("MAIN materialization cleanup is not retryable")
        ticket = self._arbiter.retry_post_settlement_cleanup(parent)
        try:
            with self._wait_handoff():
                result = ticket.wait_initiator()
        except KeyboardInterrupt:
            ticket.detach_waiter()
            raise
        if result is not None:
            raise ProtocolError("MAIN materialization cleanup result is invalid")

    def materialize_value(
        self,
        handle: str,
        options: MaterializationOptions | None = None,
    ) -> object:
        """Decode one bounded direct Context value on the current MAIN target."""

        selected = options if options is not None else MaterializationOptions()
        if not isinstance(selected, MaterializationOptions):
            raise TypeError("value materialization options are invalid")
        validate_public_direct_handle(handle)
        catalog = self._freeze_worker_catalog()
        backend = RuntimeValueTransfer(
            _no_direct_rdbg, _no_direct_rdbg,
            context_cleaner=_no_direct_rdbg,
            runtime_generation=lambda: self._runtime_generation,
            context_generation=self._context_generation,
            worker_type_registrations=lambda: catalog.registrations,
            capture_executor=lambda plan, kind: self._execute_transfer(plan, kind, catalog),
        )
        return backend.materialize(handle, selected)

    def materialize(
        self,
        handle: str,
        options: MaterializationOptions | None = None,
        *,
        table_policy: ReferencePolicy | None = None,
    ) -> object:
        """Materialize a bounded value or table on the confirmed MAIN route."""

        selected = options or MaterializationOptions()
        if not isinstance(selected, MaterializationOptions):
            raise TypeError("value materialization options are invalid")
        if table_policy is not None and not isinstance(table_policy, ReferencePolicy):
            raise TypeError("table reference policy is invalid")
        safe_handle = validate_public_direct_handle(handle)
        catalog = self._freeze_worker_catalog()
        key = f"__onec_materialization_{uuid4().hex}"
        instruction = build_dynamic_materialization_instruction(
            safe_handle,
            context_key=key,
            options=selected,
            refs=selected.refs if table_policy is None else table_policy.refs,
            ref_columns=None if table_policy is None else table_policy.ref_columns,
            max_rows=selected.max_items,
            runtime_generation=self._runtime_generation,
            context_generation=self._context_generation,
            worker_type_registrations=catalog.registrations,
        )
        payload = self._execute_transfer(
            build_projection_transfer_plan(
                instruction,
                context_key=key,
                max_bytes=selected.max_bytes,
                runtime_generation=self._runtime_generation,
                context_generation=self._context_generation,
            ),
            CaptureEvaluationKind.MATERIALIZATION_HELPER,
            catalog,
        )
        if classify_materialization_payload(payload) == "table":
            return decode_compact_table_payload(
                payload, table_policy or ReferencePolicy(selected.refs),
            )
        return decode_value_payload(payload, selected)

    def to_df(
        self,
        handle: str,
        policy: ReferencePolicy | None = None,
        *,
        max_rows: int,
        max_bytes: int = 64 * 1024 * 1024,
    ) -> pd.DataFrame:
        """Decode one bounded direct Context table on the current MAIN target."""

        if policy is not None and not isinstance(policy, ReferencePolicy):
            raise TypeError("table reference policy is invalid")
        if type(max_rows) is not int or max_rows <= 0:
            raise ValueError("table row budget must be positive")
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("table byte budget must be positive")
        validate_public_direct_handle(handle)
        catalog = self._freeze_worker_catalog()
        backend = CompactRuntimeTableTransfer(
            _no_direct_rdbg, _no_direct_rdbg,
            runtime_generation=lambda: self._runtime_generation,
            context_generation=self._context_generation,
            context_cleaner=_no_direct_rdbg,
            max_text_size=((max_bytes + 2) // 3) * 4,
            max_payload_bytes=max_bytes,
            max_rows=max_rows,
            worker_type_registrations=lambda: catalog.registrations,
            capture_executor=lambda plan, kind: self._execute_transfer(plan, kind, catalog),
        )
        return backend.to_df(handle, policy or ReferencePolicy())

    def head_to_df(
        self,
        handle: str,
        count: int,
        *,
        policy: ReferencePolicy | None = None,
        max_bytes: int = 64 * 1024 * 1024,
    ) -> pd.DataFrame:
        """Transfer at most ``count`` leading rows on the confirmed MAIN route."""

        if type(count) is not int or not 0 < count <= 10_000:
            raise ValueError("head row count must be between 1 and 10000")
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("table byte budget must be positive")
        if policy is not None and not isinstance(policy, ReferencePolicy):
            raise TypeError("table reference policy is invalid")
        safe_handle = validate_public_direct_handle(handle)
        catalog = self._freeze_worker_catalog()
        selected_policy = policy or ReferencePolicy()
        key = f"__onec_projection_{uuid4().hex}"
        instruction = build_bounded_projection_instruction(
            safe_handle,
            context_key=key,
            kind="table_rows",
            offset=0,
            limit=count,
            columns=(),
            names=(),
            refs=selected_policy.refs,
            ref_columns=selected_policy.ref_columns,
            max_rows=count,
            max_bytes=max_bytes,
            runtime_generation=self._runtime_generation,
            context_generation=self._context_generation,
            worker_type_registrations=catalog.registrations,
        )
        payload = self._execute_transfer(
            build_projection_transfer_plan(
                instruction,
                context_key=key,
                max_bytes=max_bytes,
                runtime_generation=self._runtime_generation,
                context_generation=self._context_generation,
            ),
            CaptureEvaluationKind.MATERIALIZATION_HELPER,
            catalog,
        )
        if classify_materialization_payload(payload) != "table":
            raise ProtocolError("bounded table projection returned a value payload")
        return decode_compact_table_payload(payload, selected_policy)

    def _execute_transfer(
        self,
        plan: CaptureTransferPlan,
        kind: CaptureEvaluationKind,
        catalog: WorkerMaterializationSnapshot,
    ) -> bytes:
        if kind is not CaptureEvaluationKind.MATERIALIZATION_HELPER:
            raise ProtocolError("MAIN materialization kind is invalid")
        fence = self._require_fence()
        holder: dict[str, ExecutionTicket] = {}
        ticket = self._arbiter.submit(
            fence.route,
            lambda port: self._run_plan(plan, fence, catalog, port, holder["ticket"]),
        )
        holder["ticket"] = ticket
        self._arbiter.dispatch(ticket)
        try:
            with self._wait_handoff():
                result = ticket.wait_initiator()
        except KeyboardInterrupt:
            ticket.detach_waiter()
            raise
        if type(result) is not bytes:
            raise ProtocolError("MAIN materialization payload is invalid")
        return result

    def _run_plan(
        self,
        plan: _TransferPlan,
        fence: MainIdleTargetFence,
        catalog: WorkerMaterializationSnapshot,
        port: SessionPort,
        parent: ExecutionTicket,
    ) -> Settlement:
        self._require_current_fence(fence)
        self._require_same_worker_catalog(catalog)
        first = self._evaluate(port, plan.instruction, fence.target, plan.max_text_size)
        policy_error: BaseException | None = None
        payload: bytes | None = None
        if first.error_occurred:
            policy_error = CaptureValueCheckError("MAIN value admission failed")
        else:
            try:
                metadata = plan.admit_metadata(evaluation_to_python(first))
                content_result = self._evaluate(
                    port,
                    "RuntimeKernelServer.ЗабратьКомпактнуюМатериализациюИзКонтекста("
                    "Контекст, " + bsl_string_literal(plan.private_key) + ")",
                    fence.target,
                    plan.max_text_size,
                )
                if content_result.error_occurred:
                    raise CaptureValueCheckError("MAIN materialization payload is unavailable")
                content = evaluation_to_python(content_result)
                if not isinstance(content, str) or len(content) > plan.max_text_size:
                    raise CaptureValueCheckError("MAIN materialization payload is invalid")
                payload = plan.decode(metadata, content)
                if type(payload) is not bytes:
                    raise CaptureValueCheckError("MAIN materialization payload is invalid")
            except BaseException as error:
                policy_error = error
        port.register_post_settlement_cleanup(
            lambda cleanup_port: self._run_cleanup(plan, fence, cleanup_port, parent)
        )
        if policy_error is not None:
            raise policy_error
        assert payload is not None
        return Settlement(payload)

    def _run_cleanup(
        self,
        plan: _TransferPlan,
        fence: MainIdleTargetFence,
        port: SessionPort,
        parent: ExecutionTicket,
    ) -> Settlement:
        try:
            self._require_current_fence(fence)
            deletion = self._evaluate(
                port, plan.cleanup_instruction, fence.target, plan.max_text_size,
            )
            if deletion.error_occurred:
                raise CaptureValueCheckError("MAIN materialization cleanup failed")
        except OutcomeUnknown:
            raise
        except BaseException:
            with self._cleanup_lock:
                self._cleanup_debts[plan.private_key] = parent
            raise
        with self._cleanup_lock:
            self._cleanup_debts.pop(plan.private_key, None)
        return Settlement(None)

    def _evaluate(
        self, port: SessionPort, expression: str, target: TargetId, max_text_size: int,
    ) -> EvaluationResult:
        pending = port.start_evaluation(
            expression, max_text_size=max_text_size, stack_level=0, timeout_s=30.0,
        )
        if pending.target_id != target:
            raise OutcomeUnknown("MAIN materialization evaluation belongs to another target")
        return wait_for_pending_result(port, pending)

    def _require_fence(self) -> MainIdleTargetFence:
        fence = self._main_idle_fence()
        if not isinstance(fence, MainIdleTargetFence):
            raise ProtocolError("MAIN materialization requires a confirmed MAIN target")
        if fence.route.context_id != "main":
            raise ProtocolError("MAIN materialization requires an exact MAIN-idle route")
        if self._arbiter.current_route != fence.route:
            raise ProtocolError("MAIN materialization route fence is stale")
        return fence

    def _require_current_fence(self, expected: MainIdleTargetFence) -> None:
        if self._require_fence() != expected:
            raise ProtocolError("confirmed MAIN route or target changed before materialization")

    def _freeze_worker_catalog(self) -> WorkerMaterializationSnapshot:
        reader = self._worker_catalog_snapshot
        catalog = (
            WorkerMaterializationSnapshot(0, self._worker_type_registrations())
            if reader is None
            else reader()
        )
        if not isinstance(catalog, WorkerMaterializationSnapshot):
            raise TypeError("Worker materialization snapshot is invalid")
        return catalog

    def _require_same_worker_catalog(
        self, expected: WorkerMaterializationSnapshot,
    ) -> None:
        if self._freeze_worker_catalog() != expected:
            raise ProtocolError("Worker catalog changed before MAIN materialization")


def _no_direct_rdbg(*_args: object) -> object:
    raise ProtocolError("MAIN materialization requires its arbiter ticket")
