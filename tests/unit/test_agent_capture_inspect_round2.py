from __future__ import annotations

from threading import RLock
from types import SimpleNamespace
from uuid import uuid4

from onec_runtime_mcp.agent.capture_contracts import CaptureFence
from onec_runtime_mcp.agent.observation import ManagerOrigin
from onec_runtime_mcp.agent.runtime_backend import OnecRuntimeBackend
from onec_runtime.session import RuntimeSession
from onec_runtime_mcp.agent.runtime_session import AgentRuntimeSession
from onec_runtime.prototype_runtime import (
    OperationHandle,
    OperationState,
    PrototypeRuntimeController,
)
from onec_runtime.rdbg.models import (
    CollectionCell,
    CollectionRow,
    EvaluationResult,
    FrameVariable,
    ModuleLocation,
    PendingEvaluation,
    TargetId,
)
from onec_runtime.runtime_api import PrototypeRuntimeApi
from onec_runtime.errors import ProtocolError
from onec_runtime_mcp.agent.observation import SelectionKind, ValueSelection


def fence() -> CaptureFence:
    return CaptureFence("intent", "operation", 3, "a" * 64, 1, 1)


class StrictRdbgInspectionSession:
    """Production-shaped RDBG seam: only fixed metadata calls are accepted."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.target_id = TargetId(uuid4(), uuid4(), 1)
        self._pending: dict[int, tuple[PendingEvaluation, EvaluationResult]] = {}

    def evaluate(self, expression: str, *, stack_level: int, **_kwargs: object) -> EvaluationResult:
        self.calls.append(("evaluate", (expression, stack_level)))
        if expression == 'ТипЗнч(Query.Manager) = Тип("МенеджерВременныхТаблиц")':
            assert stack_level == 0
            return EvaluationResult(uuid4(), "Булево", "Истина", False)
        raise AssertionError("projected table inspection must be one collection evaluation")

    def evaluate_collection(
        self, expression: str, *, start_index: int, page_size: int, stack_level: int, **_kwargs: object
    ) -> EvaluationResult:
        self.calls.append(("collection", (expression, start_index, page_size, stack_level)))
        assert expression.startswith((
            "RuntimeKernelServer.ПолучитьСхемуВременнойТаблицыОтладки(",
            "RuntimeTableTransferServer.ПолучитьКомпактнуюСхему("
            "RuntimeKernelServer.ПолучитьВременнуюТаблицуОтладки(",
        ))
        assert start_index == 0 and page_size == 101 and stack_level == 2
        row = CollectionRow(0, (CollectionCell("Имя", "Строка", '"Employee"', value_string="Employee"),))
        return EvaluationResult(uuid4(), "ТаблицаЗначений", "", False, collection_rows=(row,))

    def start_evaluation(
        self,
        expression: str,
        *,
        stack_level: int,
        timeout_s: float,
        max_text_size: int = 307_200,
        on_transport_dispatch=None,  # type: ignore[no-untyped-def]
    ) -> PendingEvaluation:
        if on_transport_dispatch is not None:
            on_transport_dispatch()
        result = self.evaluate(
            expression,
            stack_level=stack_level,
            timeout_s=timeout_s,
            max_text_size=max_text_size,
        )
        pending = PendingEvaluation(self.target_id, result.result_id, self)
        self._pending[id(pending)] = (pending, result)
        return pending

    def start_collection_evaluation(
        self,
        expression: str,
        *,
        start_index: int,
        page_size: int,
        stack_level: int,
        timeout_s: float,
        max_text_size: int = 4096,
        on_transport_dispatch=None,  # type: ignore[no-untyped-def]
    ) -> PendingEvaluation:
        if on_transport_dispatch is not None:
            on_transport_dispatch()
        result = self.evaluate_collection(
            expression,
            start_index=start_index,
            page_size=page_size,
            stack_level=stack_level,
            timeout_s=timeout_s,
            max_text_size=max_text_size,
        )
        pending = PendingEvaluation(self.target_id, result.result_id, self)
        self._pending[id(pending)] = (pending, result)
        return pending

    def wait_evaluation_event(
        self,
        pending: PendingEvaluation,
        *,
        timeout_s: float,
    ) -> EvaluationResult:
        del timeout_s
        stored, result = self._pending.pop(id(pending))
        assert stored is pending
        return result


def mark_captured(
    controller: PrototypeRuntimeController,
    rdbg: StrictRdbgInspectionSession,
) -> None:
    controller.active_operation = OperationHandle(1, "", "")
    controller._capture_target_id = rdbg.target_id
    controller.stop_sequence = 1
    controller.state = OperationState.CAPTURED
    controller._replace_capture_evaluation_coordinator()


def test_real_controller_api_session_backend_capture_metadata_path_is_bounded_and_opaque() -> None:
    rdbg = StrictRdbgInspectionSession()
    controller = PrototypeRuntimeController(rdbg, ModuleLocation("ExtensionModule", "", None, None, 1, "Runtime"))  # type: ignore[arg-type]
    mark_captured(controller, rdbg)
    controller.capture_frame_stack_level = 0
    controller.capture_kernel_stack_level = 2
    controller._capture_frame_variables = (
        FrameVariable("Amount", "Number", "999"),
        FrameVariable("Query", "Query", "<query>"),
    )
    api = PrototypeRuntimeApi(controller)
    session = object.__new__(RuntimeSession)
    session._operation_lock = RLock()
    session._active_capture_ticket = SimpleNamespace(
        capture_intent_id="intent", operation_id="operation", capture_generation=1, source_revision=3,
        source_sha256="a" * 64, stop_sequence=1,
    )
    session.runtime_api = api
    backend = OnecRuntimeBackend("runtime", AgentRuntimeSession(session))

    variables = backend.frame_variables(fence(), filters={"name": "Amount"}, cursor=0, limit=1)
    manager = backend.resolve_manager_origin(fence(), ManagerOrigin("frame", "Query", ("Manager",)))
    tables = backend.temporary_tables(
        fence(), manager["handle"], names=("Staff",), cursor=0, limit=1, selection=None
    )
    selected = backend.temporary_tables(
        fence(), manager["handle"], names=("Staff",), cursor=0, limit=1,
        selection=ValueSelection(
            SelectionKind.TABLE_ROWS, offset=2, limit=3, columns=("Employee",)
        ),
    )

    assert variables == {
        "items": ({"name": "Amount", "type_name": "Number", "role": "local", "handle": "Контекст.КонтекстОтладки.Amount"},),
        "total": 1,
        "next_cursor": None,
    }
    assert isinstance(manager["handle"], str) and manager["handle"].startswith("capture_manager_")
    assert isinstance(tables["items"][0]["handle"], str) and tables["items"][0]["handle"].startswith("capture_table_metadata_")
    assert tables["items"][0]["schema"] == ("Employee",)
    assert isinstance(selected["items"][0]["handle"], str) and selected["items"][0]["handle"].startswith("capture_table_")
    assert selected["items"][0]["schema"] == ("Employee",)
    assert rdbg.calls[0] == (
        "evaluate",
        ('ТипЗнч(Query.Manager) = Тип("МенеджерВременныхТаблиц")', 0),
    )
    assert rdbg.calls[1][0] == "collection"
    assert "ПолучитьСхемуВременнойТаблицыОтладки" in rdbg.calls[1][1][0]
    assert rdbg.calls[2][0] == "collection"
    assert "ПолучитьВременнуюТаблицуОтладки" in rdbg.calls[2][1][0]
    assert "СохранитьВременнуюТаблицуОтладки" not in rdbg.calls[2][1][0]
    assert rdbg.calls[2][1][3] == 2
    assert not any(call[0] == "local_variables" for call in rdbg.calls)


def test_successful_session_resume_notifies_the_exact_fence_but_failure_keeps_it() -> None:
    session = object.__new__(RuntimeSession)
    session._operation_lock = RLock()
    session._capture_resume_listeners = []
    active = SimpleNamespace(
        capture_intent_id="intent", operation_id="operation", capture_generation=1,
        source_revision=3, source_sha256="a" * 64, stop_sequence=1,
    )
    session._active_capture_ticket = active
    seen: list[CaptureFence] = []
    agent_session = AgentRuntimeSession(session)
    agent_session.add_capture_resume_listener(seen.append)
    session.runtime_api = SimpleNamespace(resume_capture=lambda **_kwargs: "resumed")

    assert agent_session.resume_capture() == "resumed"
    assert seen == [fence()]
    assert session._active_capture_ticket is None

    session._active_capture_ticket = active
    session.runtime_api = SimpleNamespace(
        resume_capture=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("pre-send")),
        status=lambda: SimpleNamespace(state=OperationState.CAPTURED),
    )
    try:
        session.resume_capture()
    except RuntimeError:
        pass
    else:
        raise AssertionError("failed resume must propagate")
    assert session._active_capture_ticket is active

    session.runtime_api = SimpleNamespace(
        resume_capture=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("ambiguous")),
        status=lambda: SimpleNamespace(state=OperationState.RECOVERING),
    )
    try:
        session.resume_capture()
    except RuntimeError:
        pass
    else:
        raise AssertionError("ambiguous resume must propagate")
    assert session._active_capture_ticket is None
    assert seen == [fence(), fence()]


def test_capture_table_selector_rejects_huge_position_before_rdbg_work() -> None:
    rdbg = StrictRdbgInspectionSession()
    controller = PrototypeRuntimeController(rdbg, ModuleLocation("ExtensionModule", "", None, None, 1, "Runtime"))  # type: ignore[arg-type]
    mark_captured(controller, rdbg)
    controller.capture_frame_stack_level = 0
    controller.capture_kernel_stack_level = 2
    controller._capture_frame_variables = (FrameVariable("Query", "Query", "<query>"),)
    manager = controller.resolve_capture_manager_origin("Query", ("Manager",))
    calls_after_manager_proof = tuple(rdbg.calls)

    try:
        controller.capture_temporary_tables(
            manager["handle"], names=("Staff",), cursor=0, limit=1,
            selection={"offset": 10_000_000, "limit": 1, "columns": ()},
        )
    except ProtocolError:
        pass
    else:
        raise AssertionError("huge projection offset must fail closed")
    assert tuple(rdbg.calls) == calls_after_manager_proof


def test_capture_extension_resolves_descriptor_through_its_data_result() -> None:
    source = (
        __import__("pathlib").Path(__file__).parents[2]
        / "onec" / "OnecInteractiveRuntime" / "CommonModules" / "RuntimeKernelServer" / "Ext" / "Module.bsl"
    ).read_text(encoding="utf-8")

    assert "ОписательТаблицы.ПолучитьДанные()" in source
    assert "RuntimeTableTransferServer.ПодготовитьТабличноеЗначение(ОписательТаблицы)" not in source
