"""One public value entry point chooses the controller's exact stopped route."""

import json
from base64 import b64encode
from decimal import Decimal
from hashlib import sha256
from threading import Event, Thread

import pytest

from onec_runtime.execution.arbiter import RdbgArbiter, RouteToken
from onec_runtime.execution.arbiter import ArbiterBusy, OutcomeUnknown, Settlement
from onec_runtime.execution.capture.manager_metadata import CaptureSelectedTableDescriptor
from onec_runtime.execution.capture.selected_table_materialization import CaptureSelectedTableTransferRequest
from onec_runtime.execution.capture.ticket_materialization import WorkerTransferCatalog
from onec_runtime.errors import CaptureValueCheckError, ProtocolError
from onec_runtime.execution.main.idle_materialization import MainIdleTargetFence
from onec_runtime.execution.worker_activation import WorkerMaterializationSnapshot
from onec_runtime.table_materialization import ReferencePolicy
from onec_runtime.value_materialization import MaterializationOptions

from test_capture_stack_inventory_adapter import ready_scope
from test_capture_ticket_data_plane import Ticket
from test_main_idle_materialization import Session, TARGET


def test_value_router_uses_capture_ticket_for_current_scope() -> None:
    from onec_runtime.execution.value_materialization_router import (
        ValueMaterializationRouter,
    )

    payload = json.dumps({
        "version": 1, "root": {"t": "number", "v": "9"},
    }).encode("utf-8")
    scope = ready_scope()

    class Controller:
        capture_scope = scope
        plan = None

        def value_route_snapshot(self):
            return scope

        def main_idle_fence(self):
            return None

        def main_idle_fence_in_ticket(self):
            return None

        def submit_capture_materialization(self, plan, *, _before_first_effect=None):
            assert _before_first_effect is not None
            _before_first_effect()
            self.plan = plan
            return Ticket(payload)

    session = Session([])
    arbiter = RdbgArbiter(session, RouteToken("runtime", 1, 0, "main"))
    controller = Controller()
    router = ValueMaterializationRouter(
        controller, arbiter,
        runtime_generation=7, context_generation=4,
        worker_catalog_snapshot=lambda: WorkerMaterializationSnapshot(0, ()),
    )
    try:
        assert router.materialize_value(
            "e1cRuntimeКонтекст.Сумма", MaterializationOptions(max_bytes=4096),
        ) == Decimal("9")
        assert "СериализоватьЗначение" in controller.plan.instruction
        assert session.calls == []
    finally:
        arbiter.close(timeout=3)


def test_value_router_uses_main_idle_ticket_after_capture_is_gone() -> None:
    from onec_runtime.execution.value_materialization_router import (
        ValueMaterializationRouter,
    )

    from base64 import b64encode
    from hashlib import sha256

    payload = b'{"version":1,"root":{"t":"number","v":"12.50"}}'
    encoded = b64encode(payload).decode("ascii")
    session = Session([
        f"R|7|4|{len(payload)}|{sha256(payload).hexdigest()}|{len(encoded)}",
        encoded,
    ])
    route = RouteToken("runtime", 1, 0, "main")
    arbiter = RdbgArbiter(session, route)
    fence = MainIdleTargetFence(route, TARGET)

    class Controller:
        capture_scope = None

        def value_route_snapshot(self):
            return fence

        def main_idle_fence(self):
            return fence

        def main_idle_fence_in_ticket(self):
            return fence

    router = ValueMaterializationRouter(
        Controller(), arbiter,
        runtime_generation=7, context_generation=4,
        worker_catalog_snapshot=lambda: WorkerMaterializationSnapshot(0, ()),
    )
    try:
        assert router.materialize_value(
            "e1cRuntimeКонтекст.Сумма", MaterializationOptions(max_bytes=4096),
        ) == Decimal("12.50")
        assert len([call for call in session.calls if call[0] == "start"]) == 2
    finally:
        arbiter.close(timeout=3)


@pytest.mark.parametrize("transfer_kind", ["value", "table"])
def test_second_main_value_transfer_waits_for_the_first_dependent_cleanup(
    transfer_kind: str,
) -> None:
    from onec_runtime.execution.value_materialization_router import ValueMaterializationRouter
    from test_compact_table import compact_payload

    payload = (
        compact_payload()
        if transfer_kind == "table"
        else b'{"version":1,"root":{"t":"number","v":"12"}}'
    )
    encoded = b64encode(payload).decode("ascii")
    envelope = f"R|7|4|{len(payload)}|{sha256(payload).hexdigest()}|{len(encoded)}"
    cleanup_entered = Event()
    release_cleanup = Event()

    class PausedCleanupSession(Session):
        def wait_evaluation_event(self, pending, *, timeout_s, on_transport_dispatch):
            if len([call for call in self.calls if call[0] == "start"]) == 2:
                cleanup_entered.set()
                assert release_cleanup.wait(3)
            return super().wait_evaluation_event(
                pending, timeout_s=timeout_s,
                on_transport_dispatch=on_transport_dispatch,
            )

    session = PausedCleanupSession([
        "eval-error", "Истина", envelope, encoded,
    ])
    route = RouteToken("runtime", 1, 0, "main")
    arbiter = RdbgArbiter(session, route)
    fence = MainIdleTargetFence(route, TARGET)

    class Controller:
        capture_scope = None

        def value_route_snapshot(self):
            return None if arbiter.has_pending_operations else fence

        def main_idle_fence(self):
            return self.value_route_snapshot()

        def main_idle_fence_in_ticket(self):
            return fence

    router = ValueMaterializationRouter(
        Controller(), arbiter,
        runtime_generation=7, context_generation=4,
        worker_catalog_snapshot=lambda: WorkerMaterializationSnapshot(0, ()),
    )
    result = []
    errors = []

    def transfer():
        if transfer_kind == "table":
            return router.to_df(
                "e1cRuntimeКонтекст.Таблица", max_rows=5,
                policy=ReferencePolicy(
                    ref_columns={"Employee": "both", "Department": "uuid"},
                ),
            )
        return router.materialize_value("e1cRuntimeКонтекст.Сумма")

    def second_transfer() -> None:
        try:
            result.append(transfer())
        except BaseException as error:
            errors.append(error)

    caller = Thread(target=second_transfer)
    try:
        with pytest.raises(CaptureValueCheckError, match="admission failed"):
            transfer()
        assert cleanup_entered.wait(1)
        caller.start()
        assert not errors
        assert len([call for call in session.calls if call[0] == "start"]) == 2
        release_cleanup.set()
        caller.join(3)
        assert not caller.is_alive()
        assert errors == []
        assert len(result) == 1
        assert (
            result[0]["Name"].tolist() == ["Alice", "Bob"]
            if transfer_kind == "table" else result[0] == Decimal("12")
        )
        assert len([call for call in session.calls if call[0] == "start"]) == 4
    finally:
        release_cleanup.set()
        caller.join(3) if caller.ident is not None else None
        arbiter.close(timeout=3)


def test_main_value_route_reports_confirmed_cleanup_debt_until_retry() -> None:
    from onec_runtime.execution.value_materialization_router import ValueMaterializationRouter

    payload = b'{"version":1,"root":{"t":"number","v":"12"}}'
    encoded = b64encode(payload).decode("ascii")
    envelope = f"R|7|4|{len(payload)}|{sha256(payload).hexdigest()}|{len(encoded)}"
    session = Session([
        "eval-error", "cleanup-error", "Истина", envelope, encoded,
    ])
    route = RouteToken("runtime", 1, 0, "main")
    arbiter = RdbgArbiter(session, route)
    fence = MainIdleTargetFence(route, TARGET)

    class Controller:
        capture_scope = None

        def value_route_snapshot(self):
            return None if arbiter.has_pending_operations else fence

        def main_idle_fence(self):
            return self.value_route_snapshot()

        def main_idle_fence_in_ticket(self):
            return fence

    router = ValueMaterializationRouter(
        Controller(), arbiter,
        runtime_generation=7, context_generation=4,
        worker_catalog_snapshot=lambda: WorkerMaterializationSnapshot(0, ()),
    )
    try:
        with pytest.raises(CaptureValueCheckError, match="admission failed"):
            router.materialize_value("e1cRuntimeКонтекст.Сумма")
        assert session.cleanup_error_seen.wait(1)
        with pytest.raises(ArbiterBusy, match="cleanup debt"):
            router.materialize_value("e1cRuntimeКонтекст.Сумма")
        assert len([call for call in session.calls if call[0] == "start"]) == 2

        key, = router.retryable_main_cleanup_keys
        router.retry_main_cleanup(key)
        assert router.materialize_value("e1cRuntimeКонтекст.Сумма") == Decimal("12")
    finally:
        arbiter.close(timeout=3)


def test_main_value_route_preserves_unknown_cleanup_owner() -> None:
    from onec_runtime.execution.evaluation import wait_for_pending_result
    from onec_runtime.execution.value_materialization_router import ValueMaterializationRouter

    payload = b'{"version":1,"root":{"t":"number","v":"12"}}'
    encoded = b64encode(payload).decode("ascii")
    envelope = f"R|7|4|{len(payload)}|{sha256(payload).hexdigest()}|{len(encoded)}"
    session = Session(["eval-error", "cleanup-unknown", "Истина"])
    route = RouteToken("runtime", 1, 0, "main")
    arbiter = RdbgArbiter(session, route)
    fence = MainIdleTargetFence(route, TARGET)

    class Controller:
        capture_scope = None

        def value_route_snapshot(self):
            return None if arbiter.has_pending_operations else fence

        def main_idle_fence(self):
            return self.value_route_snapshot()

        def main_idle_fence_in_ticket(self):
            return fence

    router = ValueMaterializationRouter(
        Controller(), arbiter,
        runtime_generation=7, context_generation=4,
        worker_catalog_snapshot=lambda: WorkerMaterializationSnapshot(0, ()),
    )
    try:
        with pytest.raises(CaptureValueCheckError, match="admission failed"):
            router.materialize_value("e1cRuntimeКонтекст.Сумма")
        assert session.cleanup_error_seen.wait(1)
        with pytest.raises(OutcomeUnknown, match="Dependent cleanup"):
            router.materialize_value("e1cRuntimeКонтекст.Сумма")
        cleanup = arbiter.active_ticket
        assert cleanup is not None and cleanup.status().phase == "unknown"
        pending = cleanup._pending
        assert pending is not None
        arbiter.reconcile(
            cleanup, lambda port: Settlement(wait_for_pending_result(port, pending)),
        )
        assert cleanup.wait_settled(1).error_occurred is False
    finally:
        arbiter.close(timeout=3)


def test_main_value_route_does_not_follow_cleanup_into_another_user_ticket() -> None:
    from onec_runtime.execution.value_materialization_router import ValueMaterializationRouter

    cleanup_entered = Event()
    release_cleanup = Event()
    user_entered = Event()
    release_user = Event()
    session = Session([])
    route = RouteToken("runtime", 1, 0, "main")
    arbiter = RdbgArbiter(session, route)
    fence = MainIdleTargetFence(route, TARGET)

    class Controller:
        capture_scope = None

        def value_route_snapshot(self):
            return None if arbiter.has_pending_operations else fence

        def main_idle_fence(self):
            return self.value_route_snapshot()

        def main_idle_fence_in_ticket(self):
            return fence

    router = ValueMaterializationRouter(
        Controller(), arbiter,
        runtime_generation=7, context_generation=4,
        worker_catalog_snapshot=lambda: WorkerMaterializationSnapshot(0, ()),
    )

    def parent_plan(port):
        def cleanup(_port):
            cleanup_entered.set()
            assert release_cleanup.wait(3)
            return Settlement(None)

        port.register_post_settlement_cleanup(cleanup)
        return Settlement("reply")

    parent = arbiter.submit(route, parent_plan)
    arbiter.dispatch(parent)
    assert parent.wait_settled(1) == "reply"
    assert cleanup_entered.wait(1)
    user = arbiter.submit(
        route, lambda _port: (
            user_entered.set(), release_user.wait(3), Settlement(None)
        )[2],
    )
    arbiter.dispatch(user)
    errors = []
    caller = Thread(target=lambda: _capture_error(
        lambda: router.materialize_value("e1cRuntimeКонтекст.Сумма"), errors,
    ))
    try:
        caller.start()
        release_cleanup.set()
        assert user_entered.wait(1)
        caller.join(1)
        assert not caller.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], ProtocolError)
        assert session.calls == []
    finally:
        release_cleanup.set()
        release_user.set()
        caller.join(3) if caller.ident is not None else None
        user.wait_settled(3)
        arbiter.close(timeout=3)


def _capture_error(call, errors: list[BaseException]) -> None:
    try:
        call()
    except BaseException as error:
        errors.append(error)


def test_value_router_builds_bounded_head_on_the_capture_ticket() -> None:
    from onec_runtime.execution.value_materialization_router import (
        ValueMaterializationRouter,
    )

    from test_compact_table import compact_payload

    scope = ready_scope()

    class Controller:
        capture_scope = scope
        plan = None

        def value_route_snapshot(self):
            return scope

        def main_idle_fence(self):
            return None

        def main_idle_fence_in_ticket(self):
            return None

        def submit_capture_materialization(self, plan, *, _before_first_effect=None):
            assert _before_first_effect is not None
            _before_first_effect()
            self.plan = plan
            return Ticket(compact_payload())

    arbiter = RdbgArbiter(Session([]), RouteToken("runtime", 1, 0, "main"))
    controller = Controller()
    router = ValueMaterializationRouter(
        controller, arbiter,
        runtime_generation=7, context_generation=4,
        worker_catalog_snapshot=lambda: WorkerMaterializationSnapshot(0, ()),
    )
    try:
        frame = router.head_to_df(
            "e1cRuntimeКонтекст.Таблица", 2,
            policy=ReferencePolicy(
                ref_columns={"Employee": "both", "Department": "uuid"},
            ),
        )
        assert frame["Name"].tolist() == ["Alice", "Bob"]
        assert "Для ИндексПроекции = 0 По Мин(e1cRuntimeКонтекст.Таблица.Количество() - 1, 1)" in controller.plan.instruction
    finally:
        arbiter.close(timeout=3)


def test_dynamic_materialize_forwards_local_wait_budget_on_both_routes() -> None:
    from onec_runtime.execution.value_materialization_router import ValueMaterializationRouter

    scope = ready_scope()
    route = RouteToken("runtime", 1, 0, "main")
    fence = MainIdleTargetFence(route, TARGET)
    arbiter = RdbgArbiter(Session([]), route)

    class Controller:
        capture_scope = scope
        selected = scope

        def value_route_snapshot(self):
            return self.selected

        def main_idle_fence(self):
            return fence

        def main_idle_fence_in_ticket(self):
            return fence

    class Service:
        def __init__(self):
            self.calls = []

        def materialize(self, handle, options=None, *, table_policy=None, timeout_s=None):
            self.calls.append((handle, timeout_s))
            return "value"

    controller = Controller()
    router = ValueMaterializationRouter(
        controller, arbiter,
        runtime_generation=7, context_generation=4,
        worker_catalog_snapshot=lambda: WorkerMaterializationSnapshot(0, ()),
    )
    capture_service = Service()
    main_service = Service()
    router._capture_dynamic = lambda _scope: capture_service
    router._main = main_service
    try:
        with pytest.raises(ValueError, match="timeout_s"):
            router.materialize("e1cRuntimeКонтекст.X", timeout_s=0)
        assert capture_service.calls == []
        assert router.materialize("e1cRuntimeКонтекст.X", timeout_s=0.25) == "value"
        controller.selected = fence
        assert router.materialize("e1cRuntimeКонтекст.X", timeout_s=0.5) == "value"
        assert capture_service.calls == [("e1cRuntimeКонтекст.X", 0.25)]
        assert main_service.calls == [("e1cRuntimeКонтекст.X", 0.5)]
    finally:
        arbiter.close(timeout=3)


def test_selected_table_router_passes_deferred_request_on_capture_only() -> None:
    from onec_runtime.execution.value_materialization_router import ValueMaterializationRouter

    scope = ready_scope()
    handle = "capture_table_" + "b" * 32
    descriptor = CaptureSelectedTableDescriptor(
        scope, "Query", ("Manager",), "Staff", 3, 5, (),
    )

    class Controller:
        capture_scope = scope
        selected = scope
        request = None

        def value_route_snapshot(self):
            return self.selected

        def main_idle_fence(self):
            return None

        def main_idle_fence_in_ticket(self):
            return None

        def require_capture_table_descriptor(self, requested, actual_scope):
            assert requested == handle and actual_scope is scope
            return descriptor

        def submit_capture_materialization(self, request, *, _before_first_effect=None):
            assert _before_first_effect is not None
            _before_first_effect()
            self.request = request
            return Ticket(b"private verified bytes")

    arbiter = RdbgArbiter(Session([]), RouteToken("runtime", 1, 0, "main"))
    controller = Controller()
    router = ValueMaterializationRouter(
        controller, arbiter,
        runtime_generation=7, context_generation=4,
        worker_catalog_snapshot=lambda: WorkerMaterializationSnapshot(3, ()),
    )
    try:
        router.validate_selected_table_handle(handle)
        assert router.transfer_selected_table(
            handle, ReferencePolicy(), max_rows=5, max_bytes=2048,
            catalog=WorkerTransferCatalog(3, ()), timeout_s=None,
            relative_offset=2, relative_limit=1,
        ) == b"private verified bytes"
        assert isinstance(controller.request, CaptureSelectedTableTransferRequest)
        assert controller.request.relative_offset == 2
        assert controller.request.relative_limit == 1
        controller.selected = None
        with pytest.raises(ProtocolError):
            router.transfer_selected_table(
                handle, ReferencePolicy(), max_rows=5, max_bytes=2048,
                catalog=WorkerTransferCatalog(3, ()), timeout_s=None,
            )
    finally:
        arbiter.close(timeout=3)


def test_selected_table_to_df_uses_same_deferred_route() -> None:
    from test_compact_table import compact_payload
    from onec_runtime.execution.value_materialization_router import ValueMaterializationRouter

    scope = ready_scope()
    handle = "capture_table_" + "c" * 32

    class Controller:
        capture_scope = scope
        request = None

        def value_route_snapshot(self):
            return scope

        def main_idle_fence(self):
            return None

        def main_idle_fence_in_ticket(self):
            return None

        def submit_capture_materialization(self, request, *, _before_first_effect=None):
            _before_first_effect()
            self.request = request
            return Ticket(compact_payload())

    arbiter = RdbgArbiter(Session([]), RouteToken("runtime", 1, 0, "main"))
    controller = Controller()
    router = ValueMaterializationRouter(
        controller, arbiter,
        runtime_generation=7, context_generation=4,
        worker_catalog_snapshot=lambda: WorkerMaterializationSnapshot(3, ()),
    )
    try:
        frame = router.to_df(
            handle, max_rows=5,
            policy=ReferencePolicy(
                ref_columns={"Employee": "both", "Department": "uuid"},
            ),
        )
        assert frame["Name"].tolist() == ["Alice", "Bob"]
        assert isinstance(controller.request, CaptureSelectedTableTransferRequest)
        assert controller.request.handle == handle
    finally:
        arbiter.close(timeout=3)
