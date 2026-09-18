"""Manager origin and temporary-table schema stay on one CAPTURE arbiter."""

from dataclasses import replace
from threading import Event, current_thread
from types import SimpleNamespace
from uuid import UUID

import pytest

from onec_runtime.capture_evaluation import CaptureEvaluationState, CapturePhase
from onec_runtime.errors import ProtocolError, StaleCaptureError
from onec_runtime.execution.arbiter import RdbgArbiter, RouteToken
from onec_runtime.execution.capture.selected_table_materialization import (
    CaptureSelectedTableTransferRequest,
)
from onec_runtime.execution.capture.cell_evaluator import CaptureCellEvaluator
from onec_runtime.execution.capture.executor import CaptureExecutor
from onec_runtime.execution.controller.controller import ExecutionController
from onec_runtime.execution.main import MainExecutor, MainPhase
from onec_runtime.execution.value_materialization_router import ValueMaterializationRouter
from onec_runtime.execution.worker_activation import WorkerMaterializationSnapshot
from onec_runtime.observation import ManagerOrigin, SelectionKind, ValueSelection
from onec_runtime.execution.public_facade import PublicExecutionFacade
from onec_runtime.rdbg.models import (
    CollectionCell, CollectionRow, EvaluationResult, FrameVariable,
    LocalVariablesResult, PendingEvaluation,
)
from onec_runtime.stop_routing import BreakpointRegistry
from onec_runtime.table_value import evaluation_to_python
from onec_runtime.table_materialization import ReferencePolicy

from test_execution_controller_routes import BUSINESS, KERNEL, CompleteSession
from test_execution_route_sequence import TARGET
from test_public_execution_facade import _Pipeline, unit


class MetadataSession(CompleteSession):
    def __init__(self) -> None:
        super().__init__()
        self.metadata_calls: list[tuple[str, str, int, object]] = []
        self.manager_proof = True
        self.schema_names = ("Employee", "Amount")
        self.private_error = "private debugger value SECRET"

    def local_variables(self, stack_level=0, *, timeout_s, on_transport_dispatch):
        if stack_level != 0:
            return super().local_variables(
                stack_level, timeout_s=timeout_s,
                on_transport_dispatch=on_transport_dispatch,
            )
        on_transport_dispatch()
        self._record("locals")
        return LocalVariablesResult(
            UUID(int=141),
            (FrameVariable("Query", "Запрос", self.private_error),),
        )

    def start_evaluation(self, expression, *, max_text_size, stack_level,
                         timeout_s, on_transport_dispatch):
        if expression.startswith("ТипЗнч("):
            self.metadata_calls.append(("probe", expression, stack_level, current_thread()))
        return super().start_evaluation(
            expression, max_text_size=max_text_size, stack_level=stack_level,
            timeout_s=timeout_s, on_transport_dispatch=on_transport_dispatch,
        )

    def start_collection_evaluation(self, expression, *, start_index, page_size,
                                    max_text_size, stack_level, timeout_s,
                                    on_transport_dispatch):
        assert self.pending is None
        on_transport_dispatch()
        self._record("start_collection")
        self.expression = expression
        self.next_result_id += 1
        self.pending = PendingEvaluation(TARGET, UUID(int=self.next_result_id), self)
        self.metadata_calls.append(("schema", expression, stack_level, current_thread()))
        assert (start_index, page_size, max_text_size) == (0, 101, 4096)
        return self.pending

    def wait_evaluation_event(self, pending, *, timeout_s, on_transport_dispatch):
        if self.expression.startswith("ТипЗнч("):
            assert pending is self.pending
            on_transport_dispatch()
            self._record("wait_eval")
            self.pending = None
            if self.manager_proof == "malformed":
                return EvaluationResult(
                    pending.result_id, "Булево", self.private_error, False,
                )
            if self.manager_proof:
                return EvaluationResult(pending.result_id, "Булево", "Истина", False)
            return EvaluationResult(
                pending.result_id, "Ошибка", self.private_error, True,
                error_text=self.private_error,
            )
        if self.expression.startswith((
            "RuntimeKernelServer.ПолучитьСхемуВременнойТаблицыОтладки(",
            "RuntimeTableTransferServer.ПолучитьКомпактнуюСхему(",
        )):
            assert pending is self.pending
            on_transport_dispatch()
            self._record("wait_eval")
            self.pending = None
            return EvaluationResult(
                pending.result_id, "Массив", self.private_error, False,
                collection_size=len(self.schema_names),
                collection_rows=tuple(
                    CollectionRow(index, (CollectionCell(
                        "Имя", "Строка", f'"{name}"', value_string=name,
                    ),))
                    for index, name in enumerate(self.schema_names)
                ),
            )
        return super().wait_evaluation_event(
            pending, timeout_s=timeout_s,
            on_transport_dispatch=on_transport_dispatch,
        )


def _bound():
    from onec_runtime.execution.capture.manager_metadata import CaptureManagerMetadataService

    session = MetadataSession()
    arbiter = RdbgArbiter(session, RouteToken("manager-test", 1, 0, "main"))
    controller = ExecutionController(
        arbiter, MainExecutor(poll_interval_s=0.1),
        CaptureExecutor(None, KERNEL, decode_command_id=evaluation_to_python),
        CaptureCellEvaluator(), BreakpointRegistry(KERNEL, (BUSINESS,)),
        runtime_generation=1, initial_target_id=TARGET,
    )
    controller.submit_main("Результат = 1;").wait_settled(3)
    service = CaptureManagerMetadataService(controller)
    return session, arbiter, controller, service


def test_manager_proof_mints_one_scope_fenced_handle_without_private_result() -> None:
    session, arbiter, controller, service = _bound()
    try:
        first = service.resolve_manager_origin(
            ManagerOrigin("frame", "query", ("Manager",)),
        )
        alias = service.resolve_manager_origin(
            ManagerOrigin("frame", "QUERY", ("manager",)),
        )
        assert first == alias
        assert first["key"] == first["handle"]
        assert first["type_name"] == "МенеджерВременныхТаблиц"
        assert service.validate_value_reference(first["handle"]) == first["handle"]
        assert session.metadata_calls[0][:3] == (
            "probe", 'ТипЗнч(Query.Manager) = Тип("МенеджерВременныхТаблиц")', 0,
        )
        assert len(session.metadata_calls) == 1
        assert all(call[3] is arbiter._worker for call in session.metadata_calls)
        assert controller.capture_scope is not None
        assert controller.main_operation.phase is MainPhase.SUSPENDED_CAPTURE
        assert session.private_error not in repr(first)
    finally:
        arbiter.close(timeout=3)


def test_schema_only_inventory_returns_metadata_handle_and_never_reads_rows() -> None:
    session, arbiter, _controller, service = _bound()
    try:
        manager = service.resolve_manager_origin(ManagerOrigin("frame", "Query", ("Manager",)))
        result = service.temporary_tables(
            manager["handle"], names=("Staff",), cursor=0, limit=1,
            selection=None,
        )
        item = result["items"][0]
        assert result["total"] == 1 and result["next_cursor"] is None
        assert item["name"] == "Staff"
        assert item["schema"] == ("Employee", "Amount")
        assert item["handle"].startswith("capture_table_metadata_")
        assert service.validate_value_reference(item["handle"]) == item["handle"]
        assert session.metadata_calls[-1][:3] == (
            "schema",
            'RuntimeKernelServer.ПолучитьСхемуВременнойТаблицыОтладки('
            'e1cRuntimeКонтекст.e1cRuntimeКонтекстОтладки.Query.Manager, "Staff")',
            2,
        )
        assert all(call[3] is arbiter._worker for call in session.metadata_calls)
        assert "private debugger" not in repr(result)
    finally:
        arbiter.close(timeout=3)


def test_unsafe_name_rejects_before_any_new_ticket() -> None:
    session, arbiter, _controller, service = _bound()
    try:
        manager = service.resolve_manager_origin(ManagerOrigin("frame", "Query", ("Manager",)))
        before = tuple(session.metadata_calls)
        with pytest.raises(ProtocolError, match="selection"):
            service.temporary_tables(
                manager["handle"], names=("Staff",), cursor=0, limit=1,
                selection=ValueSelection(SelectionKind.FIELDS, names=("Amount",)),
            )
        with pytest.raises(ProtocolError, match="bounds"):
            service.temporary_tables(
                manager["handle"], names=("Staff",), cursor=0, limit=1,
                selection=ValueSelection(SelectionKind.TABLE_ROWS, limit=101),
            )
        with pytest.raises(ProtocolError, match="bounds"):
            service.temporary_tables(
                manager["handle"], names=("Staff",), cursor=0, limit=1,
                selection=ValueSelection(
                    SelectionKind.TABLE_ROWS, offset=10_000_000, limit=1,
                ),
            )
        with pytest.raises(ProtocolError):
            service.temporary_tables(
                manager["handle"], names=("Staff;Delete",), cursor=0,
                limit=1, selection=None,
            )
        assert tuple(session.metadata_calls) == before
    finally:
        arbiter.close(timeout=3)


def test_selected_table_registers_scope_fenced_deferred_descriptor() -> None:
    session, arbiter, controller, service = _bound()
    try:
        manager = service.resolve_manager_origin(
            ManagerOrigin("frame", "Query", ("Manager",)),
        )
        result = service.temporary_tables(
            manager["handle"], names=("Staff",), cursor=0, limit=1,
            selection=ValueSelection(
                SelectionKind.TABLE_ROWS, offset=3, limit=5,
                columns=("Employee",),
            ),
        )
        handle = result["items"][0]["handle"]
        assert handle.startswith("capture_table_")
        assert result["items"][0]["schema"] == ("Employee", "Amount")
        assert service.validate_value_reference(handle) == handle
        assert session.metadata_calls[-1][1] == (
            "RuntimeTableTransferServer.ПолучитьКомпактнуюСхему("
            "RuntimeKernelServer.ПолучитьВременнуюТаблицуОтладки("
            'e1cRuntimeКонтекст.e1cRuntimeКонтекстОтладки.Query.Manager, "Staff", 3, 5, '
            'СтрРазделить("Employee", ",")))'
        )
        assert controller.capture_scope is not None
        controller.invalidate_capture_inspection()
        with pytest.raises((ProtocolError, StaleCaptureError)):
            service.validate_value_reference(handle)
    finally:
        arbiter.close(timeout=3)


def test_selected_descriptor_resolves_inside_owned_materialization_ticket(monkeypatch) -> None:
    from onec_runtime.execution.arbiter import Settlement

    session, arbiter, controller, service = _bound()
    try:
        manager = service.resolve_manager_origin(
            ManagerOrigin("frame", "Query", ("Manager",)),
        )
        handle = service.temporary_tables(
            manager["handle"], names=("Staff",), cursor=0, limit=1,
            selection=ValueSelection(SelectionKind.TABLE_ROWS, offset=3, limit=5),
        )["items"][0]["handle"]
        scope = controller.capture_scope
        assert scope is not None
        observed = []

        def fake_execute(actual_scope, plan, *, port, shield_workspace,
                         restore_workspace, on_confirmed_failure):
            assert callable(on_confirmed_failure)
            observed.append((actual_scope, plan.instruction, current_thread()))
            return Settlement(b"private table bytes")

        monkeypatch.setattr(controller._capture_private_data_plane.materialization_executor, "execute", fake_execute)
        request = CaptureSelectedTableTransferRequest(
            handle, scope, ReferencePolicy(), max_rows=1, max_bytes=2048,
            runtime_generation=1, context_generation=1,
            worker_registrations=(),
            relative_offset=2, relative_limit=1,
        )
        assert controller.submit_capture_materialization(request).wait_settled(3) == b"private table bytes"
        assert observed[0][0] is scope
        assert observed[0][2] is arbiter._worker
        assert "ПолучитьВременнуюТаблицуОтладки" in observed[0][1]
        assert 'e1cRuntimeКонтекст.e1cRuntimeКонтекстОтладки.Query.Manager, "Staff", 5, 1, Новый Массив)' in observed[0][1]
        assert "capture_table_" not in observed[0][1]
        assert "private table bytes" not in repr(controller.capture_evaluation_ledger().status())
        controller.invalidate_capture_inspection()
        with pytest.raises((ProtocolError, StaleCaptureError)):
            controller.submit_capture_materialization(request)
    finally:
        arbiter.close(timeout=3)


def test_selected_descriptor_registry_is_owned_by_private_data_plane() -> None:
    from onec_runtime.execution.capture.manager_metadata import CaptureSelectedTableDescriptor
    from onec_runtime.execution.controller.capture_private_data_plane import CapturePrivateDataPlane

    _session, arbiter, controller, _service = _bound()
    try:
        scope = controller.capture_scope
        assert scope is not None
        descriptor = CaptureSelectedTableDescriptor(
            scope, "Query", ("Manager",), "Staff", 0, 1, ("Employee",),
        )
        handle = controller.register_capture_table_descriptor(descriptor)
        assert isinstance(controller._capture_private_data_plane, CapturePrivateDataPlane)
        assert controller.require_capture_table_descriptor(handle, scope) is descriptor
        assert "_capture_table_descriptors" not in vars(controller)
        assert "_capture_table_descriptor_scope" not in vars(controller)
    finally:
        arbiter.close(timeout=3)


def test_bad_selected_key_retires_ledger_before_caller_retries(monkeypatch) -> None:
    import onec_runtime.execution.controller.controller as controller_module
    from onec_runtime.execution.arbiter import Settlement

    session, arbiter, controller, service = _bound()
    gate = Event()
    entered = Event()
    try:
        manager = service.resolve_manager_origin(
            ManagerOrigin("frame", "Query", ("Manager",)),
        )
        handle = service.temporary_tables(
            manager["handle"], names=("Staff",), cursor=0, limit=1,
            selection=ValueSelection(SelectionKind.TABLE_ROWS, limit=2),
        )["items"][0]["handle"]
        scope = controller.capture_scope
        assert scope is not None
        original = controller_module._observe_capture_ticket

        def delayed(ticket, ledger, receipt_id):
            entered.set()
            gate.wait(3)
            original(ticket, ledger, receipt_id)

        monkeypatch.setattr(controller_module, "_observe_capture_ticket", delayed)
        monkeypatch.setattr(
            controller._capture_private_data_plane.materialization_executor, "execute",
            lambda *args, **kwargs: Settlement(b"confirmed"),
        )
        request = CaptureSelectedTableTransferRequest(
            handle, scope, ReferencePolicy(), 2, 2048, 1, 1, (),
        )
        with pytest.raises(ProtocolError):
            controller.submit_capture_materialization(
                replace(request, handle="capture_table_" + "0" * 32),
            ).wait_settled(3)
        assert entered.wait(3)
        with pytest.raises(ProtocolError, match="generation"):
            controller.submit_capture_materialization(
                replace(request, runtime_generation=2),
            ).wait_settled(3)
        assert controller.submit_capture_materialization(request).wait_settled(3) == b"confirmed"
    finally:
        gate.set()
        arbiter.close(timeout=3)


def test_confirmed_probe_failure_keeps_paused_scope_and_hides_private_error() -> None:
    session, arbiter, controller, service = _bound()
    session.manager_proof = False
    scope = controller.capture_scope
    try:
        with pytest.raises(ProtocolError) as failure:
            service.resolve_manager_origin(ManagerOrigin("frame", "Query", ("Manager",)))
        assert session.private_error not in str(failure.value)
        assert controller.capture_scope is scope
        assert controller.main_operation.phase is MainPhase.SUSPENDED_CAPTURE
        assert arbiter.active_ticket is None
        session.manager_proof = True
        assert service.resolve_manager_origin(
            ManagerOrigin("frame", "Query", ("Manager",)),
        )["handle"].startswith("capture_manager_")
    finally:
        arbiter.close(timeout=3)


def test_malformed_private_probe_and_ambiguous_schema_fail_closed() -> None:
    session, arbiter, controller, service = _bound()
    scope = controller.capture_scope
    try:
        session.manager_proof = "malformed"
        with pytest.raises(ProtocolError) as caught:
            service.resolve_manager_origin(ManagerOrigin("frame", "Query", ("Manager",)))
        assert session.private_error not in str(caught.value)
        session.manager_proof = True
        manager = service.resolve_manager_origin(ManagerOrigin("frame", "Query", ("Manager",)))
        session.schema_names = ("Employee", "employee")
        with pytest.raises(ProtocolError, match="ambiguous"):
            service.temporary_tables(
                manager["handle"], names=("Staff",), cursor=0, limit=1,
                selection=None,
            )
        assert controller.capture_scope is scope
        assert controller.main_operation.phase is MainPhase.SUSPENDED_CAPTURE
    finally:
        arbiter.close(timeout=3)


def test_confirmed_bad_probe_releases_ledger_before_observer_runs(monkeypatch) -> None:
    import onec_runtime.execution.controller.controller as controller_module

    session, arbiter, _controller, service = _bound()
    observer_entered = Event()
    release_observer = Event()
    original_observer = controller_module._observe_capture_ticket

    def delayed_observer(ticket, ledger, receipt_id):
        observer_entered.set()
        release_observer.wait(3)
        original_observer(ticket, ledger, receipt_id)

    monkeypatch.setattr(controller_module, "_observe_capture_ticket", delayed_observer)
    try:
        session.manager_proof = "malformed"
        with pytest.raises(ProtocolError):
            service.resolve_manager_origin(
                ManagerOrigin("frame", "Query", ("Manager",)),
            )
        assert observer_entered.wait(3)
        session.manager_proof = True
        assert service.resolve_manager_origin(
            ManagerOrigin("frame", "Query", ("Manager",)),
        )["handle"].startswith("capture_manager_")
    finally:
        release_observer.set()
        arbiter.close(timeout=3)


def test_manager_local_fence_rejection_retires_ledger_before_waiter(monkeypatch) -> None:
    import onec_runtime.execution.controller.controller as controller_module
    from onec_runtime.execution.capture.manager_metadata import CaptureManagerProbePlan

    _session, arbiter, controller, _service = _bound()
    scope = controller.capture_scope
    assert scope is not None
    release_observer = Event()
    original_observer = controller_module._observe_capture_ticket
    original_check = controller._require_capture_manager_metadata_ready_locked

    def delayed_observer(ticket, ledger, receipt_id):
        if receipt_id.startswith("manager-metadata-"):
            assert release_observer.wait(10)
        original_observer(ticket, ledger, receipt_id)

    def reject_on_worker(selected_scope, *, allow_pending=False):
        if allow_pending:
            raise ProtocolError("local manager fence changed")
        return original_check(selected_scope, allow_pending=allow_pending)

    monkeypatch.setattr(controller_module, "_observe_capture_ticket", delayed_observer)
    monkeypatch.setattr(
        controller, "_require_capture_manager_metadata_ready_locked", reject_on_worker,
    )
    try:
        plan = CaptureManagerProbePlan(scope, "Query", ("Manager",))
        with pytest.raises(ProtocolError, match="local manager fence changed"):
            controller.submit_capture_manager_metadata(plan).wait_settled(3)
        ledger = controller.capture_evaluation_ledger()
        assert ledger.status().phase is CapturePhase.PAUSED
        assert ledger.wait(1).state is CaptureEvaluationState.FAILED
    finally:
        release_observer.set()
        arbiter.close(timeout=3)


def test_manager_and_metadata_handles_fail_after_scope_invalidation() -> None:
    session, arbiter, controller, service = _bound()
    try:
        manager = service.resolve_manager_origin(ManagerOrigin("frame", "Query", ("Manager",)))
        table = service.temporary_tables(
            manager["handle"], names=("Staff",), cursor=0, limit=1,
            selection=None,
        )["items"][0]["handle"]
        controller.invalidate_capture_inspection()
        with pytest.raises((ProtocolError, StaleCaptureError)):
            service.validate_value_reference(manager["handle"])
        with pytest.raises((ProtocolError, StaleCaptureError)):
            service.validate_value_reference(table)
        assert session.metadata_calls[-1][0] == "schema"
    finally:
        arbiter.close(timeout=3)


def test_cached_manager_alias_rechecks_stop_before_return() -> None:
    session, arbiter, controller, service = _bound()
    try:
        first = service.resolve_manager_origin(ManagerOrigin("frame", "Query", ("Manager",)))
        ticks = [0]

        def clock() -> float:
            ticks[0] += 1
            if ticks[0] == 2:
                controller.invalidate_capture_inspection()
            return float(ticks[0])

        service._clock = clock
        with pytest.raises(StaleCaptureError):
            service.resolve_manager_origin(ManagerOrigin("frame", "query", ("manager",)))
        assert first["handle"].startswith("capture_manager_")
    finally:
        arbiter.close(timeout=3)


def test_successor_admission_blocks_helper_before_rdbg() -> None:
    session, arbiter, controller, service = _bound()
    try:
        controller._continuation_admission = object()
        before = tuple(session.metadata_calls)
        with pytest.raises(ProtocolError):
            service.resolve_manager_origin(ManagerOrigin("frame", "Query", ("Manager",)))
        assert tuple(session.metadata_calls) == before
    finally:
        arbiter.close(timeout=3)


def test_public_facade_routes_manager_and_schema_calls_through_controller() -> None:
    session, arbiter, controller, _service = _bound()
    facade = PublicExecutionFacade(
        _Pipeline(), controller, arbiter,
        source_unit_factory=unit, status_reader=lambda: SimpleNamespace(),
    )
    try:
        manager = facade.resolve_capture_manager_origin(
            ManagerOrigin("frame", "Query", ("Manager",)), timeout_s=1.0,
        )
        assert facade.validate_value_reference(manager["handle"]) == manager["handle"]
        table = facade.capture_temporary_tables(
            manager["handle"], names=("Staff",), cursor=0, limit=1,
            selection=None, timeout_s=1.0,
        )["items"][0]
        assert table["schema"] == ("Employee", "Amount")
        assert facade.validate_value_reference(table["handle"]) == table["handle"]
        selected = facade.capture_temporary_tables(
            manager["handle"], names=("Staff",), cursor=0, limit=1,
            selection=ValueSelection(SelectionKind.TABLE_ROWS, limit=2),
            timeout_s=1.0,
        )["items"][0]
        assert facade.validate_value_reference(selected["handle"]) == selected["handle"]
        assert len(session.metadata_calls) == 3
    finally:
        arbiter.close(timeout=3)


def test_selected_handle_materializes_through_facade_value_ports(monkeypatch) -> None:
    from onec_runtime.execution.arbiter import Settlement
    from test_value_projection_service import TABLE_PAYLOAD

    session, arbiter, controller, _service = _bound()
    session.schema_names = ("Amount",)
    observed = []

    def fake_execute(scope, plan, *, port, shield_workspace, restore_workspace,
                     on_confirmed_failure):
        assert callable(on_confirmed_failure)
        observed.append((scope, plan.instruction, current_thread()))
        return Settlement(TABLE_PAYLOAD)

    monkeypatch.setattr(controller._capture_private_data_plane.materialization_executor, "execute", fake_execute)
    catalog = lambda: WorkerMaterializationSnapshot(0, ())
    router = ValueMaterializationRouter(
        controller, arbiter, runtime_generation=1, context_generation=1,
        worker_catalog_snapshot=catalog,
    )
    facade = PublicExecutionFacade(
        _Pipeline(), controller, arbiter,
        source_unit_factory=unit, status_reader=lambda: SimpleNamespace(),
        namespace_reader=lambda: SimpleNamespace(),
        worker_catalog_snapshot=catalog, value_router=router,
        runtime_generation=1, context_generation=1,
    )
    try:
        manager = facade.resolve_capture_manager_origin(
            ManagerOrigin("frame", "Query", ("Manager",)),
        )
        handle = facade.capture_temporary_tables(
            manager["handle"], names=("Staff",), cursor=0, limit=1,
            selection=ValueSelection(SelectionKind.TABLE_ROWS, limit=2),
        )["items"][0]["handle"]
        assert facade.validate_value_reference(handle) == handle
        assert facade.materialization_kind(handle) == "table"
        assert facade.materialize_table_payload(
            handle, max_rows=2, max_bytes=2048,
        ) == TABLE_PAYLOAD
        frame = facade.project_to_df(
            handle, {"offset": 0, "limit": 1},
            max_rows=2, max_bytes=2048,
        )
        assert frame["Amount"].tolist() == [12.5]
        assert len(observed) == 2
        assert all(item[2] is arbiter._worker for item in observed)
        assert all("capture_table_" not in item[1] for item in observed)
    finally:
        arbiter.close(timeout=3)
