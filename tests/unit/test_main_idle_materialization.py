"""MAIN-idle direct materialization stays inside one arbiter ticket."""

from base64 import b64encode
from decimal import Decimal
from hashlib import sha256
from threading import Event, Thread, get_ident
from uuid import UUID

import pytest

from onec_runtime.errors import CaptureValueCheckError, ProtocolError, RdbgTransportTimeout
from onec_runtime.execution.arbiter import RdbgArbiter, RouteToken, Settlement
from onec_runtime.execution.evaluation import wait_for_pending_result
from onec_runtime.execution.main.idle_materialization import (
    MainIdleMaterializationService,
    MainIdleTargetFence,
    WorkerMaterializationSnapshot,
)
from onec_runtime.rdbg.models import DebugTarget, EvaluationResult, PendingEvaluation, TargetId
from onec_runtime.table_materialization import ReferencePolicy
from onec_runtime.value_materialization import MaterializationOptions

from test_compact_table import compact_payload


TARGET = TargetId(UUID(int=1), "test")
ROUTE = RouteToken("runtime", 1, 0, "main")


class Session:
    target = DebugTarget(TARGET, "Server", "stopped")

    def __init__(self, responses: list[str]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, object, int]] = []
        self.cleanup_error_seen = Event()
        self._counter = 0
        self._pending: PendingEvaluation | None = None

    def start_evaluation(self, expression, *, on_transport_dispatch, **kwargs):
        on_transport_dispatch()
        self._counter += 1
        self._pending = PendingEvaluation(TARGET, UUID(int=10 + self._counter), self)
        self.calls.append(("start", expression, get_ident()))
        return self._pending

    def wait_evaluation_event(self, pending, *, timeout_s, on_transport_dispatch):
        assert pending is self._pending
        on_transport_dispatch()
        self.calls.append(("wait", pending, get_ident()))
        response = self._responses.pop(0)
        if response == "eval-error":
            return EvaluationResult(pending.result_id, "Ошибка", "", True)
        if response == "cleanup-error":
            self.cleanup_error_seen.set()
            return EvaluationResult(pending.result_id, "Ошибка", "", True)
        if response == "cleanup-unknown":
            self.cleanup_error_seen.set()
            raise RdbgTransportTimeout("cleanup outcome is unknown")
        return EvaluationResult(pending.result_id, "Строка", response, False)


class ExpressionOnlySession(Session):
    """Model evalExpr rejecting statement blocks before they can run."""

    def wait_evaluation_event(self, pending, *, timeout_s, on_transport_dispatch):
        expression = self.calls[-1][1]
        if not expression.startswith("RuntimeKernelServer."):
            on_transport_dispatch()
            self.calls.append(("wait", pending, get_ident()))
            self.cleanup_error_seen.set()
            return EvaluationResult(pending.result_id, "Ошибка", "", True)
        return super().wait_evaluation_event(
            pending, timeout_s=timeout_s,
            on_transport_dispatch=on_transport_dispatch,
        )


def test_main_idle_transfer_rejects_private_worker_root_before_dispatch() -> None:
    session = Session([])
    arbiter = RdbgArbiter(session, ROUTE)
    service = MainIdleMaterializationService(
        arbiter,
        main_idle_fence=lambda: MainIdleTargetFence(ROUTE, TARGET),
        runtime_generation=7, context_generation=3,
    )
    try:
        with pytest.raises(ProtocolError, match="Worker generation"):
            service.materialize_value("e1cRuntimeКонтекст.RuntimeWorkerActiveGeneration")
        with pytest.raises(ProtocolError, match="Worker generation"):
            service.to_df(
                "e1cRuntimeКонтекст.RuntimeWorkerPinnedOperationGeneration",
                max_rows=10,
            )
        assert session.calls == []
    finally:
        arbiter.close(timeout=3)


def test_direct_value_materialization_runs_all_remote_steps_in_one_arbiter_ticket() -> None:
    payload = b'{"version":1,"root":{"t":"number","v":"12.50"}}'
    encoded = b64encode(payload).decode("ascii")
    session = Session([
        f"R|7|3|{len(payload)}|{sha256(payload).hexdigest()}|{len(encoded)}",
        encoded,
        "Истина",
    ])
    arbiter = RdbgArbiter(session, ROUTE)
    service = MainIdleMaterializationService(
        arbiter,
        main_idle_fence=lambda: MainIdleTargetFence(ROUTE, TARGET),
        runtime_generation=7,
        context_generation=3,
    )
    try:
        value = service.materialize_value(
            "e1cRuntimeКонтекст.Сумма", MaterializationOptions(max_bytes=4096)
        )

        assert value == Decimal("12.50")
        assert len([call for call in session.calls if call[0] == "start"]) == 2
        assert "СериализоватьЗначение" in session.calls[0][1]
        assert "ЗабратьКомпактнуюМатериализациюИзКонтекста" in session.calls[2][1]
        assert {call[2] for call in session.calls} == {arbiter._worker.ident}
    finally:
        arbiter.close(timeout=3)


def test_main_idle_materialize_timeout_detaches_only_local_waiter() -> None:
    payload = b'{"version":1,"root":{"t":"number","v":"12"}}'
    encoded = b64encode(payload).decode("ascii")
    session = Session([
        f"R|7|3|{len(payload)}|{sha256(payload).hexdigest()}|{len(encoded)}",
        encoded,
        "Истина",
    ])
    arbiter = RdbgArbiter(session, ROUTE)
    release = Event()
    blocker_started = Event()
    blocker = arbiter.submit(
        ROUTE,
        lambda _port: (blocker_started.set(), release.wait(3), Settlement(None))[2],
    )
    arbiter.dispatch(blocker)
    service = MainIdleMaterializationService(
        arbiter,
        main_idle_fence=lambda: MainIdleTargetFence(ROUTE, TARGET),
        runtime_generation=7, context_generation=3,
    )
    try:
        assert blocker_started.wait(1)
        with pytest.raises(TimeoutError, match="Local waiter interval"):
            service.materialize("e1cRuntimeКонтекст.Сумма", timeout_s=0.02)

        pending = arbiter._queue[0]
        assert pending.status().waiter_detached is True
        assert pending.status().settled is False
        assert session.calls == []

        release.set()
        assert pending.wait_settled(1) == payload
        assert pending.status().settled is True
    finally:
        release.set()
        blocker.wait_settled(1)
        arbiter.close(timeout=3)


def test_materialization_rejects_target_changed_before_ticket_admission() -> None:
    session = Session([])
    arbiter = RdbgArbiter(session, ROUTE)
    service = MainIdleMaterializationService(
        arbiter,
        main_idle_fence=lambda: None,
        runtime_generation=7,
        context_generation=3,
    )
    try:
        with pytest.raises(ProtocolError, match="confirmed MAIN target"):
            service.materialize_value("e1cRuntimeКонтекст.Сумма")
        assert session.calls == []
    finally:
        arbiter.close(timeout=3)


def test_direct_table_materialization_uses_the_same_ticket_transfer_protocol() -> None:
    payload = compact_payload()
    encoded = b64encode(payload).decode("ascii")
    session = Session([
        f"R|7|3|{len(payload)}|{sha256(payload).hexdigest()}|{len(encoded)}",
        encoded,
        "Истина",
    ])
    arbiter = RdbgArbiter(session, ROUTE)
    service = MainIdleMaterializationService(
        arbiter,
        main_idle_fence=lambda: MainIdleTargetFence(ROUTE, TARGET),
        runtime_generation=7,
        context_generation=3,
    )
    try:
        frame = service.to_df(
            "e1cRuntimeКонтекст.Таблица",
            ReferencePolicy(refs="uuid", ref_columns={"Employee": "both"}),
            max_rows=100,
            max_bytes=32_768,
        )

        assert frame["Name"].tolist() == ["Alice", "Bob"]
        assert "СериализоватьКомпактнуюТаблицу" in session.calls[0][1]
        expressions = [call[1] for call in session.calls if call[0] == "start"]
        assert len(expressions) == 2
        assert expressions[0].startswith(
            "RuntimeKernelServer.ВыполнитьКодВКонтекстеMain(e1cRuntimeКонтекст, "
        )
        assert " + Символы.ПС + " in expressions[0]
    finally:
        arbiter.close(timeout=3)


def test_dynamic_main_head_materializes_only_the_requested_table_rows() -> None:
    payload = compact_payload()
    encoded = b64encode(payload).decode("ascii")
    session = Session([
        f"R|7|3|{len(payload)}|{sha256(payload).hexdigest()}|{len(encoded)}",
        encoded,
        "Истина",
    ])
    arbiter = RdbgArbiter(session, ROUTE)
    service = MainIdleMaterializationService(
        arbiter,
        main_idle_fence=lambda: MainIdleTargetFence(ROUTE, TARGET),
        runtime_generation=7,
        context_generation=3,
    )
    try:
        frame = service.head_to_df(
            "e1cRuntimeКонтекст.Таблица", 2,
            policy=ReferencePolicy(
                ref_columns={"Employee": "both", "Department": "uuid"},
            ),
        )

        assert frame["Name"].tolist() == ["Alice", "Bob"]
        assert "Для ИндексПроекции = 0 По Мин(e1cRuntimeКонтекст.Таблица.Количество() - 1, 1)" in session.calls[0][1]
        assert "СериализоватьКомпактнуюТаблицу" in session.calls[0][1]
        assert len([call for call in session.calls if call[0] == "start"]) == 2
    finally:
        arbiter.close(timeout=3)


def test_dynamic_main_materialize_selects_the_table_decoder() -> None:
    payload = compact_payload()
    encoded = b64encode(payload).decode("ascii")
    session = Session([
        f"R|7|3|{len(payload)}|{sha256(payload).hexdigest()}|{len(encoded)}",
        encoded,
        "Истина",
    ])
    arbiter = RdbgArbiter(session, ROUTE)
    service = MainIdleMaterializationService(
        arbiter,
        main_idle_fence=lambda: MainIdleTargetFence(ROUTE, TARGET),
        runtime_generation=7,
        context_generation=3,
    )
    try:
        frame = service.materialize(
            "e1cRuntimeКонтекст.Таблица",
            MaterializationOptions(max_bytes=32_768),
            table_policy=ReferencePolicy(
                ref_columns={"Employee": "both", "Department": "uuid"},
            ),
        )

        assert frame["Name"].tolist() == ["Alice", "Bob"]
        assert "ПолучитьВидМатериализации" in session.calls[0][1]
        assert "СериализоватьКомпактнуюТаблицу" in session.calls[0][1]
    finally:
        arbiter.close(timeout=3)


@pytest.mark.parametrize("count", [0, 10_001])
def test_dynamic_main_head_enforces_the_public_row_bound(count: int) -> None:
    session = Session([])
    arbiter = RdbgArbiter(session, ROUTE)
    service = MainIdleMaterializationService(
        arbiter,
        main_idle_fence=lambda: MainIdleTargetFence(ROUTE, TARGET),
        runtime_generation=7,
        context_generation=3,
    )
    try:
        with pytest.raises(ValueError, match="between 1 and 10000"):
            service.head_to_df("e1cRuntimeКонтекст.Таблица", count)
        assert session.calls == []
    finally:
        arbiter.close(timeout=3)


def test_materialization_rejects_a_non_main_fence_before_ticket_submission() -> None:
    session = Session([])
    arbiter = RdbgArbiter(session, ROUTE)
    service = MainIdleMaterializationService(
        arbiter,
        main_idle_fence=lambda: MainIdleTargetFence(
            RouteToken("runtime", 1, 0, "capture"), TARGET
        ),
        runtime_generation=7,
        context_generation=3,
    )
    try:
        with pytest.raises(ProtocolError, match="MAIN-idle route"):
            service.materialize_value("e1cRuntimeКонтекст.Сумма")
        assert session.calls == []
    finally:
        arbiter.close(timeout=3)


def test_confirmed_private_key_deletion_failure_is_retryable_arbiter_debt() -> None:
    session = Session([
        "eval-error",
        "cleanup-error",
        "Истина",
    ])
    arbiter = RdbgArbiter(session, ROUTE)
    service = MainIdleMaterializationService(
        arbiter,
        main_idle_fence=lambda: MainIdleTargetFence(ROUTE, TARGET),
        runtime_generation=7,
        context_generation=3,
    )
    try:
        with pytest.raises(CaptureValueCheckError, match="admission failed"):
            service.materialize_value("e1cRuntimeКонтекст.Сумма")
        assert session.cleanup_error_seen.wait(1)
        cleanup = arbiter.active_ticket
        if cleanup is not None:
            with pytest.raises(CaptureValueCheckError, match="cleanup"):
                cleanup.wait_initiator(1)
        assert len(service.retryable_cleanup_keys) == 1
        key = service.retryable_cleanup_keys[0]
        blocked = arbiter.submit(ROUTE, lambda _port: Settlement("must wait"))
        arbiter.dispatch(blocked)
        with pytest.raises(TimeoutError):
            blocked.wait_initiator(0.01)

        service.retry_cleanup(key)

        assert service.retryable_cleanup_keys == ()
        assert blocked.wait_initiator(1) == "must wait"
    finally:
        arbiter.close(timeout=3)


def test_main_admission_bsl_error_cleans_key_and_allows_next_transfer() -> None:
    payload = b'{"version":1,"root":{"t":"number","v":"2"}}'
    encoded = b64encode(payload).decode("ascii")
    session = ExpressionOnlySession([
        "eval-error",
        "Истина",
        f"R|7|3|{len(payload)}|{sha256(payload).hexdigest()}|{len(encoded)}",
        encoded,
        "Истина",
    ])
    arbiter = RdbgArbiter(session, ROUTE)
    service = MainIdleMaterializationService(
        arbiter,
        main_idle_fence=lambda: MainIdleTargetFence(ROUTE, TARGET),
        runtime_generation=7,
        context_generation=3,
    )
    try:
        with pytest.raises(CaptureValueCheckError, match="MAIN value admission failed"):
            service.materialize_value("e1cRuntimeКонтекст.Сумма")

        assert service.materialize(
            "e1cRuntimeКонтекст.Сумма", timeout_s=0.5,
        ) == Decimal("2")
        assert service.retryable_cleanup_keys == ()
    finally:
        arbiter.close(timeout=3)


def test_unknown_private_key_deletion_keeps_its_arbiter_owner() -> None:
    session = Session([
        "eval-error",
        "cleanup-unknown",
        "Истина",
    ])
    arbiter = RdbgArbiter(session, ROUTE)
    service = MainIdleMaterializationService(
        arbiter,
        main_idle_fence=lambda: MainIdleTargetFence(ROUTE, TARGET),
        runtime_generation=7,
        context_generation=3,
    )
    try:
        with pytest.raises(CaptureValueCheckError, match="admission failed"):
            service.materialize_value("e1cRuntimeКонтекст.Сумма")
        assert session.cleanup_error_seen.wait(1)
        cleanup = arbiter.active_ticket
        assert cleanup is not None and cleanup.wait_unknown(1)
        assert service.retryable_cleanup_keys == ()

        pending = cleanup._pending
        assert pending is not None
        arbiter.reconcile(
            cleanup,
            lambda port: Settlement(wait_for_pending_result(port, pending)),
        )
        assert cleanup.wait_initiator(1).error_occurred is False
    finally:
        arbiter.close(timeout=3)


def test_worker_catalog_change_after_plan_construction_rejects_ticket_before_eval() -> None:
    session = Session([])
    arbiter = RdbgArbiter(session, ROUTE)
    release = Event()
    snapshot_frozen = Event()
    catalog = [WorkerMaterializationSnapshot(1, ("old-registration",))]

    def read_catalog() -> WorkerMaterializationSnapshot:
        snapshot_frozen.set()
        return catalog[0]

    service = MainIdleMaterializationService(
        arbiter,
        main_idle_fence=lambda: MainIdleTargetFence(ROUTE, TARGET),
        runtime_generation=7,
        context_generation=3,
        worker_catalog_snapshot=read_catalog,
    )
    blocker = arbiter.submit(
        ROUTE, lambda _port: (release.wait(1), Settlement(None))[1],
    )
    arbiter.dispatch(blocker)
    errors: list[BaseException] = []

    def materialize() -> None:
        try:
            service.materialize_value("e1cRuntimeКонтекст.Сумма")
        except BaseException as error:
            errors.append(error)

    caller = Thread(target=materialize)
    try:
        caller.start()
        assert snapshot_frozen.wait(1)
        catalog[0] = WorkerMaterializationSnapshot(2, ("new-registration",))
        release.set()
        caller.join(1)

        assert not caller.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], ProtocolError)
        assert "Worker catalog changed" in str(errors[0])
        assert session.calls == []
    finally:
        release.set()
        caller.join(1)
        arbiter.close(timeout=3)
