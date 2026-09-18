"""Manager origin and temporary-table schema stay on one CAPTURE arbiter."""

from threading import Event, current_thread
from types import SimpleNamespace
from uuid import UUID

import pytest

from onec_runtime.errors import ProtocolError, StaleCaptureError
from onec_runtime.execution.arbiter import RdbgArbiter, RouteToken
from onec_runtime.execution.capture.cell_evaluator import CaptureCellEvaluator
from onec_runtime.execution.capture.executor import CaptureExecutor
from onec_runtime.execution.controller.controller import ExecutionController
from onec_runtime.execution.main import MainExecutor, MainPhase
from onec_runtime.observation import ManagerOrigin, SelectionKind, ValueSelection
from onec_runtime.execution.public_facade import PublicExecutionFacade
from onec_runtime.rdbg.models import (
    CollectionCell, CollectionRow, EvaluationResult, FrameVariable,
    LocalVariablesResult, PendingEvaluation,
)
from onec_runtime.stop_routing import BreakpointRegistry
from onec_runtime.table_value import evaluation_to_python

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
        if self.expression.startswith(
            "RuntimeKernelServer.ПолучитьСхемуВременнойТаблицыОтладки("
        ):
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
            'Контекст.КонтекстОтладки.Query.Manager, "Staff")',
            2,
        )
        assert all(call[3] is arbiter._worker for call in session.metadata_calls)
        assert "private debugger" not in repr(result)
    finally:
        arbiter.close(timeout=3)


def test_selected_table_and_unsafe_name_reject_before_any_new_ticket() -> None:
    session, arbiter, _controller, service = _bound()
    try:
        manager = service.resolve_manager_origin(ManagerOrigin("frame", "Query", ("Manager",)))
        before = tuple(session.metadata_calls)
        with pytest.raises(ProtocolError, match="selected table"):
            service.temporary_tables(
                manager["handle"], names=("Staff",), cursor=0, limit=1,
                selection=ValueSelection(SelectionKind.TABLE_ROWS, limit=5),
            )
        with pytest.raises(ProtocolError):
            service.temporary_tables(
                manager["handle"], names=("Staff;Delete",), cursor=0,
                limit=1, selection=None,
            )
        assert tuple(session.metadata_calls) == before
    finally:
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
        assert len(session.metadata_calls) == 2
    finally:
        arbiter.close(timeout=3)
