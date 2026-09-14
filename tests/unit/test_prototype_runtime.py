from __future__ import annotations

from base64 import b64encode
from collections import deque
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
import importlib
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from threading import Event, RLock
from uuid import UUID, uuid4

from mcp.client import Client
import pytest
import nbformat

from onec_runtime.artifacts import ArtifactWriter
from onec_runtime_mcp.agent.capture_contracts import (
    CaptureFence,
    CapturePointRequest,
    CaptureView,
    ResolvedCapturePoint,
)
from onec_runtime_mcp.agent.capture_service import CaptureArming, CaptureService
from onec_runtime_mcp.agent.contracts import AgentOperationState, CapabilityMode, to_wire
from onec_runtime_mcp.agent.facade import AgentFacade
from onec_runtime_mcp.agent.mcp_profiles import McpProfile
from onec_runtime_mcp.agent.observation import (
    ManagerOrigin,
    ObservationItem,
    ObservationPlan,
    ObservationSource,
    ObservationSourceKind,
    SelectionKind,
    ValueSelection,
)
from onec_runtime_mcp.agent.runtime_backend import OnecRuntimeBackend
from onec_runtime_mcp.agent.service import AgentWorkspaceService, _AdmittedRuntime
from onec_runtime_mcp.agent.proxies import ProxyRegistry, StaleProxy
from onec_runtime.capture import build_temporary_storage_value_expression
from onec_runtime.bsl import (
    DiagnosticStage,
    MappingConfidence,
    MappingRelation,
    SourceUnitKind,
    SourceUnitRef,
    WorkerExport,
    mapped_visible_source,
    source_sha256,
)
from onec_runtime.session import RuntimeSession, _ActiveCaptureTicket
from onec_runtime_mcp.agent.runtime_session import AgentRuntimeSession
from onec_runtime.errors import ProtocolError, RdbgTransportError
from onec_runtime.fault_injection import (
    CloseTransportAt,
    FaultPoint,
    InjectedTransportFailure,
)
from onec_runtime.rdbg.models import (
    CollectionCell,
    CollectionRow,
    DebugTarget,
    EvaluationResult,
    FrameVariable,
    LocalVariablesResult,
    ModifyResult,
    ModuleLocation,
    PendingEvaluation,
    StackFrame,
    StopEvent,
    TargetId,
)
from onec_runtime.rdbg.reconnect import ReconnectedSession
from onec_runtime.rdbg.xml_codec import RDBG_NS, parse_ping_events
from onec_runtime.recovery import (
    RecoveryIdentityEvidence,
    RecoveryOutcome,
)
from onec_runtime.recovery_journal import RecoveryJournal
from onec_runtime.stop_routing import StopReason
from onec_runtime.runtime_api import (
    CaptureCorrelationTicket,
    PrototypeRuntimeApi,
    RuntimeReplyKind,
)
from onec_runtime_mcp.server import create_mcp_server


TARGET = TargetId(UUID("22222222-2222-2222-2222-222222222222"), "DefAlias")
OBJECT = UUID("cb953767-f436-4a5b-9e09-13a67d6e0201")
PROPERTY = UUID("d5963243-262e-4398-b4d7-fb16d06484f6")
SERVICE = ModuleLocation(
    "ExtensionModule", "", OBJECT, PROPERTY, 60, "OnecInteractiveRuntime"
)
CAPTURE_A = ModuleLocation(
    "ExtensionModule", "", OBJECT, PROPERTY, 50, "OnecInteractiveRuntime"
)
CAPTURE_B = ModuleLocation(
    "ExtensionModule", "", OBJECT, PROPERTY, 51, "OnecInteractiveRuntime"
)
USER = ModuleLocation(
    "ExtensionModule", "", OBJECT, PROPERTY, 52, "OnecInteractiveRuntime"
)
UNKNOWN = ModuleLocation(
    "ExtensionModule", "", OBJECT, PROPERTY, 99, "OnecInteractiveRuntime"
)
BUSINESS = ModuleLocation(
    "ConfigModule", "", UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"), PROPERTY, 53, "Business"
)
WORKER_BREAKPOINT = ModuleLocation(
    "ExtMDModule",
    "e1cib/tempstorage/00000000-0000-0000-0000-000000000001?seanceId=fake",
    UUID("2a00a4fa-8ea9-4dc4-9de1-472044c40101"),
    UUID("a637f77f-3840-441d-a1c3-699c8c5cb7e0"),
    2,
)


def evaluation(
    type_name: str,
    presentation: str,
    *,
    error: str = "",
) -> EvaluationResult:
    return EvaluationResult(uuid4(), type_name, presentation, bool(error), error)


def test_system_main_preserves_retained_worker_breakpoints() -> None:
    runtime = runtime_module()
    session = ScriptedSession((SERVICE,))
    controller = runtime.PrototypeRuntimeController(session, SERVICE)
    owner = controller.breakpoint_workspace_owner
    desired = owner.prepare(
        captures=(),
        ordinary_users=(),
        worker_slots=(WORKER_BREAKPOINT,),
        shielded=False,
    )
    owner.install(desired)

    controller.execute_system_main("Результат = 1;")

    assert WORKER_BREAKPOINT in owner.confirmed_snapshot.effective_locations
    assert controller.breakpoint_workspaces
    assert all(
        WORKER_BREAKPOINT in event.locations
        for event in controller.breakpoint_workspaces
    )


def test_controller_cleanup_accepts_only_server_owned_projection_key() -> None:
    runtime = runtime_module()
    class CleanupSession:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def evaluate(self, expression: str, **_kwargs: object) -> EvaluationResult:
            self.calls.append(expression)
            return evaluation("Булево", "Истина")

    session = CleanupSession()
    controller = runtime.PrototypeRuntimeController(session, SERVICE)

    controller.drop_context_value("__onec_projection_" + "a" * 32)

    assert any("__onec_projection_" in expression for expression in session.calls)
    with pytest.raises(ProtocolError, match="context key"):
        controller.drop_context_value("__onec_projection_not-a-uuid")


class ScriptedSession:
    def __init__(
        self,
        stops: tuple[ModuleLocation, ...],
        *,
        completion_errors: tuple[str, ...] = (),
        failed_roots: tuple[str, ...] = (),
        capture_evaluations: tuple[EvaluationResult, ...] = (),
        main_results: tuple[EvaluationResult, ...] = (),
        messages: tuple[str, ...] = (),
        compact_payload: str = "",
        messages_in_result_slot: bool = False,
        fail_workspace_on_call: int | None = None,
        kernel_command_values: tuple[int, ...] = (),
        stacks: tuple[tuple[ModuleLocation, ...], ...] = (),
    ) -> None:
        configured_stacks = deque(stacks)
        self.stops = deque()
        for location in stops:
            stack = (
                configured_stacks.popleft()
                if configured_stacks
                else (location, location, SERVICE)
            )
            self.stops.append(StopEvent(
                TARGET,
                location,
                "callStackFormed",
                stop_by_breakpoint=True,
                suspended_by_other=False,
                stack=stack,
                stack_frames=tuple(
                    StackFrame(TARGET, level, frame_location)
                    for level, frame_location in enumerate(stack)
                ),
            ))
        self.completion_errors = deque(completion_errors)
        self.failed_roots = set(failed_roots)
        self.capture_evaluations = deque(capture_evaluations)
        self.main_results = deque(main_results)
        self.message_values = deque(messages)
        self.compact_payload = compact_payload
        self.messages_in_result_slot = messages_in_result_slot
        self.fail_workspace_on_call = fail_workspace_on_call
        self.kernel_command_values = deque(kernel_command_values)
        self.workspace_call_count = 0
        self.current_command = 0
        self.calls: list[tuple[str, object]] = []
        self.continue_count = 0
        self.target = DebugTarget(TARGET, "CLIENT", "Stopped", 7)
        self.invalidated = False
        self.pending_evaluation_stops: deque[StopEvent] = deque()
        self._pending_evaluation_owner = object()
        self._pending_evaluations: dict[
            int, tuple[PendingEvaluation, EvaluationResult, StopEvent | None]
        ] = {}

    def invalidate(self) -> None:
        self.invalidated = True
        self.target = None

    def set_breakpoints(self, locations: tuple[ModuleLocation, ...]) -> None:
        self.workspace_call_count += 1
        self.calls.append(("set_breakpoints", locations))
        if self.workspace_call_count == self.fail_workspace_on_call:
            raise ProtocolError("planned workspace failure")

    def modify(self, variable: str, value_expression: str) -> ModifyResult:
        self.calls.append(("modify", (variable, value_expression)))
        if variable == "ИдентификаторКоманды":
            self.current_command = int(value_expression)
        if variable in self.failed_roots:
            return ModifyResult(uuid4(), "Ошибка", "failed", True, "planned write failure")
        return ModifyResult(uuid4(), "Булево", "Истина", False)

    def continue_(self) -> None:
        self.continue_count += 1
        self.calls.append(("continue", self.continue_count))

    def wait_for_stop(
        self,
        allowed_locations: tuple[ModuleLocation, ...],
        *,
        timeout_s: float,
    ) -> StopEvent:
        self.calls.append(("wait", (allowed_locations, timeout_s)))
        return self.stops.popleft()

    def wait_for_any_stop(self, *, timeout_s: float) -> StopEvent:
        self.calls.append(("wait_any", timeout_s))
        return self.stops.popleft()

    def local_variables(self, stack_level: int = 0) -> LocalVariablesResult:
        self.calls.append(("local_variables", stack_level))
        if stack_level == 2:
            return LocalVariablesResult(
                uuid4(),
                (
                    FrameVariable("Контекст", "Структура", "Структура"),
                    FrameVariable("ТекущаяИнструкция", "Строка", '""'),
                    FrameVariable("ИдентификаторКоманды", "Число", "1"),
                ),
            )
        if stack_level != 0:
            return LocalVariablesResult(uuid4(), ())
        return LocalVariablesResult(
            uuid4(),
            (
                FrameVariable("Скаляр", "Число", "40"),
                FrameVariable("Результат", "Массив", "Массив"),
            ),
        )

    def evaluate(
        self,
        expression: str,
        *,
        stack_level: int = 0,
        timeout_s: float = 30.0,
        max_text_size: int = 307_200,
    ) -> EvaluationResult:
        call_value: object = (
            expression if stack_level == 0 else (expression, stack_level)
        )
        self.calls.append(("evaluate", call_value))
        if "ЗабратьКомпактнуюМатериализациюИзКонтекста" in expression:
            return evaluation(
                "Строка",
                '"' + self.compact_payload.replace('"', '""') + '"',
            )
        if expression.startswith("ПоместитьВоВременноеХранилище("):
            return evaluation("Строка", '"e1cib/tempstorage/capture"')
        if "ПоместитьЗначениеКонтекстаОтладки" in expression:
            return evaluation("Строка", '"e1cib/tempstorage/root"')
        if any(
            method in expression
            for method in (
                "НачатьКонтекстОтладки",
                "ВосстановитьКонтекстВыполнения",
            )
        ):
            return evaluation("Булево", "Истина")
        if any(
            method in expression
            for method in (
                "ВыполнитьКодТекущегоКонтекстаОтладки",
                "ВыполнитьКодВКонтекстеОтладки",
            )
        ):
            if self.capture_evaluations:
                return self.capture_evaluations.popleft()
            return evaluation("Число", "778")
        if "ЗавершитьКонтекстОтладки" in expression:
            return evaluation("Неопределено", "Неопределено")
        if "ЗабратьСообщенияЯчейкиИзКонтекста" in expression:
            payload = json.dumps(list(self.message_values), ensure_ascii=False)
            return evaluation("Строка", '"' + payload.replace('"', '""') + '"')
        if expression.endswith('.Свойство("__onec_cell_messages_result_key")'):
            return evaluation("Булево", "Истина" if self.message_values else "Ложь")
        if expression.endswith(".__onec_cell_messages_result_key"):
            suffix = "1" if self.messages_in_result_slot else "0"
            return evaluation("Строка", f'"__onec_cell_messages_1_1_{suffix}"')
        if expression.startswith("Контекст.Свойство("):
            is_result_slot = "Результат" in expression
            exists = bool(self.message_values) and is_result_slot == self.messages_in_result_slot
            return evaluation("Булево", "Истина" if exists else "Ложь")
        if expression.endswith(".Количество()") and "__onec_cell_messages" in expression:
            return evaluation("Число", str(len(self.message_values)))
        if "__onec_cell_messages" in expression and expression.endswith("]"):
            index = int(expression.rsplit("[", 1)[1][:-1])
            value = self.message_values[index].replace('"', '""')
            return evaluation("Строка", f'"{value}"')
        if expression.startswith("Контекст.Удалить("):
            self.message_values.clear()
            return evaluation("Неопределено", "Неопределено")
        if expression == "ЗавершеннаяКоманда":
            return evaluation("Число", str(self.current_command))
        if expression == "ИдентификаторКоманды":
            value = (
                self.kernel_command_values.popleft()
                if self.kernel_command_values
                else self.current_command
            )
            return evaluation("Число", str(value))
        if expression == "Результат":
            if self.main_results:
                return self.main_results.popleft()
            return evaluation("Неопределено", "Неопределено")
        if expression == "Ошибка":
            error = self.completion_errors.popleft() if self.completion_errors else ""
            return evaluation("Строка", f'"{error}"')
        if expression == (
            'ТипЗнч(Результат.МенеджерВременныхТаблиц) '
            '= Тип("МенеджерВременныхТаблиц")'
        ):
            return evaluation("Булево", "Истина")
        raise AssertionError(f"Unexpected expression: {expression}")

    def start_evaluation(
        self,
        expression: str,
        *,
        max_text_size: int = 307_200,
        stack_level: int = 0,
    ) -> PendingEvaluation:
        if self._pending_evaluations:
            raise ProtocolError("Another expression evaluation is already pending")
        result = self.evaluate(
            expression,
            max_text_size=max_text_size,
            stack_level=stack_level,
        )
        pending = PendingEvaluation(
            TARGET,
            result.result_id,
            self._pending_evaluation_owner,
        )
        self._pending_evaluations[id(pending)] = (pending, result, None)
        return pending

    def wait_evaluation_event(
        self,
        pending: PendingEvaluation,
        *,
        timeout_s: float,
    ) -> EvaluationResult | StopEvent:
        del timeout_s
        state = self._pending_evaluations.get(id(pending))
        if state is None or state[0] is not pending:
            raise ProtocolError("Pending evaluation is stale or foreign")
        _, result, suspended_stop = state
        if suspended_stop is not None:
            raise ProtocolError("Pending evaluation stop must be continued first")
        if self.pending_evaluation_stops:
            stop = self.pending_evaluation_stops.popleft()
            self._pending_evaluations[id(pending)] = (pending, result, stop)
            return stop
        del self._pending_evaluations[id(pending)]
        return result

    def continue_evaluation(
        self,
        pending: PendingEvaluation,
        stop: StopEvent,
    ) -> None:
        state = self._pending_evaluations.get(id(pending))
        if state is None or state[0] is not pending or state[2] is not stop:
            raise ProtocolError("Pending evaluation stop is stale or foreign")
        self.continue_()
        self._pending_evaluations[id(pending)] = (pending, state[1], None)

    def evaluate_collection(
        self,
        expression: str,
        *,
        start_index: int,
        page_size: int,
        timeout_s: float = 30.0,
        max_text_size: int = 4096,
        stack_level: int = 0,
    ) -> EvaluationResult:
        self.calls.append(
            (
                "evaluate_collection",
                (expression, start_index, page_size, stack_level),
            )
        )
        end = min(start_index + page_size, len(self.message_values))
        rows = tuple(
            CollectionRow(
                index,
                (
                    CollectionCell(
                        "Значение",
                        "Строка",
                        '"' + self.message_values[index].replace('"', '""') + '"',
                        value_string=self.message_values[index],
                    ),
                ),
            )
            for index in range(start_index, end)
        )
        return EvaluationResult(
            uuid4(),
            "Массив",
            "Массив",
            False,
            collection_size=len(self.message_values),
            collection_rows=rows,
        )


def runtime_module():  # type: ignore[no-untyped-def]
    return importlib.import_module("onec_runtime.prototype_runtime")


def mapped_source(source: str, unit_id: str, revision: int):  # type: ignore[no-untyped-def]
    return mapped_visible_source(
        source,
        SourceUnitRef(
            SourceUnitKind.NOTEBOOK_CELL,
            unit_id,
            revision,
            source_sha256(source),
        ),
    )


def test_message_wrapper_maps_user_body_and_marks_scaffolding_synthetic() -> None:
    runtime = runtime_module()
    controller = runtime.PrototypeRuntimeController(ScriptedSession(()), SERVICE)
    source = mapped_source(
        'Сообщить("до");\nРезультат = 1 / 0;',
        "cell-main",
        1,
    )

    wrapped = controller._with_message_collector_mapped(
        source,
        1,
        "__messages",
    )

    slash = wrapped.text.index("/")
    assert wrapped.source_map.map_offset(slash).relation is MappingRelation.EXACT
    finalize = wrapped.text.index("__onec_cell_messages_result_key")
    mapped_finalize = wrapped.source_map.map_offset(finalize)
    assert mapped_finalize.relation is MappingRelation.SYNTHETIC
    assert mapped_finalize.synthetic_region == "message_collector_finalize"
    assert mapped_finalize.anchor_unit is not None
    for marker, region in (
        ('Контекст.Вставить("__messages"', "message_collector_initialize"),
        ("Попытка", "message_collector_try"),
        ("Исключение", "message_collector_exception"),
        ("ВызватьИсключение", "message_collector_rethrow"),
        ("КонецПопытки", "message_collector_end"),
    ):
        mapped = wrapped.source_map.map_offset(wrapped.text.index(marker))
        assert mapped.relation is MappingRelation.SYNTHETIC
        assert mapped.synthetic_region == region
        assert mapped.anchor_unit is not None


def test_worker_message_wrapper_restores_previous_sink_on_success_and_failure() -> None:
    runtime = runtime_module()
    controller = runtime.PrototypeRuntimeController(ScriptedSession(()), SERVICE)
    source = mapped_source('Показать();', "worker-message-call", 1)

    wrapped = controller._with_message_collector_mapped(
        source, 1, "__messages", worker_messages=True,
    )

    attach = (
        '__OnecPinnedWorkerGenerationMessageObject.__OnecWorkerMessageSink = '
        'Контекст.__messages;'
    )
    restore = (
        '__OnecPinnedWorkerGenerationMessageObject.__OnecWorkerMessageSink = '
        '__OnecPinnedWorkerGenerationPreviousMessageSink;'
    )
    assert wrapped.text.count(attach) == 1
    assert wrapped.text.count(restore) == 2
    assert wrapped.text.index(attach) < wrapped.text.index('Показать();')
    assert wrapped.text.index('Показать();') < wrapped.text.index(restore)
    assert wrapped.text.index(restore) < wrapped.text.index('Исключение')
    assert restore in wrapped.text[wrapped.text.index('Исключение'):]
    assert wrapped.source_map.map_offset(wrapped.text.index('Показать();')).relation is MappingRelation.EXACT


def test_main_platform_failure_returns_visible_diagnostic_and_allows_next_run() -> None:
    runtime = runtime_module()
    raw = "{<Неизвестный модуль>(1,15)}: Деление на 0"
    session = ScriptedSession(
        (SERVICE, SERVICE),
        completion_errors=(raw, ""),
    )
    controller = runtime.PrototypeRuntimeController(session, SERVICE)
    api = PrototypeRuntimeApi(controller)

    failed = api.execute_bsl("Результат = 1 / 0;")

    assert failed.kind is RuntimeReplyKind.MAIN_COMPLETED
    assert failed.succeeded is False
    assert failed.error == "BSL execution failed"
    assert failed.diagnostic is not None
    assert failed.diagnostic.stage is DiagnosticStage.EXECUTION
    assert failed.diagnostic.mapping_confidence is MappingConfidence.EXACT
    assert failed.diagnostic.visible_location is not None
    assert (
        failed.diagnostic.visible_location.line,
        failed.diagnostic.visible_location.column,
    ) == (1, 15)
    assert failed.diagnostic.platform_diagnostic == raw
    assert failed.diagnostic.execution_artifact_sha256 is not None

    recovered = api.execute_bsl("Результат = 1;")

    assert recovered.succeeded is True
    assert recovered.operation_id == 2
    assert controller.active_operation is not None
    assert controller.active_operation.executed_source is not None
    assert (
        controller.active_operation.executed_source.artifact.kind.value
        == "executed_bsl"
    )
    assert (
        controller.active_operation.executed_source.text
        == controller.active_operation.lowered_source
    )
    assert (
        controller.active_operation.executed_source.artifact.source_sha256
        == source_sha256(controller.active_operation.lowered_source)
    )


def test_unlocated_main_failure_keeps_current_cell_origin_and_platform_cause() -> None:
    runtime = runtime_module()
    raw = (
        "Ошибка при вызове метода контекста (Записать)\n"
        "по причине:\nНе заполнено обязательное поле Наименование"
    )
    source = (
        "Элемент = Справочники.Номенклатура.СоздатьЭлемент();\n"
        "Элемент.Записать();"
    )
    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL, "missing-name", 1, source_sha256(source)
    )
    controller = runtime.PrototypeRuntimeController(
        ScriptedSession((SERVICE,), completion_errors=(raw,)), SERVICE
    )
    api = PrototypeRuntimeApi(controller)
    prepared = []

    reply = api.execute_bsl(
        source, source_unit=unit, on_execution_provenance=prepared.append
    )

    assert reply.succeeded is False
    assert reply.diagnostic is not None
    assert reply.diagnostic.platform_diagnostic == raw
    assert reply.diagnostic.visible_location is None
    assert reply.diagnostic.source_unit == unit
    assert len(prepared) == 1
    assert prepared[0].source_map_sha256 != reply.diagnostic.source_map_sha256
    from onec_runtime_jupyter.extension import (
        NotebookDisplayConfig,
        _display_reply,
    )

    displayed = _display_reply(
        reply, NotebookDisplayConfig.presentation(),
        visible_source=source, source_unit=unit,
    )
    assert "Не заполнено обязательное поле Наименование" in displayed.text
    assert "строка" not in displayed.text


def test_normalizer_failure_keeps_main_failure_safe_and_allows_next_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: diagnostic normalization must not replace a MAIN failure."""
    runtime = runtime_module()
    raw = "{<Неизвестный модуль>(1,15)}: PRIVATE_PLATFORM_MAIN_TEXT"
    session = ScriptedSession(
        (SERVICE, SERVICE),
        completion_errors=(raw, ""),
        messages=("before main failure",),
    )
    controller = runtime.PrototypeRuntimeController(session, SERVICE)
    api = PrototypeRuntimeApi(controller)

    def fail_normalization(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("forced normalizer failure")

    monkeypatch.setattr(runtime, "remap_platform_diagnostic", fail_normalization)

    failed = api.execute_bsl('Сообщить("before main failure"); Результат = 1 / 0;')

    assert failed.kind is RuntimeReplyKind.MAIN_COMPLETED
    assert failed.succeeded is False
    assert failed.error == "BSL execution failed"
    assert raw not in failed.error
    assert failed.diagnostic is None
    assert failed.messages == ("before main failure",)
    assert controller.state is runtime.OperationState.FAILED

    recovered = api.execute_bsl("Результат = 1;")

    assert recovered.succeeded is True
    assert recovered.operation_id == 2
    assert controller.state is runtime.OperationState.COMPLETED


def test_capture_platform_failure_keeps_paused_state_and_sends_no_continue() -> None:
    runtime = runtime_module()
    raw = "{<Неизвестный модуль>(1,25)}: Деление на 0"
    session = ScriptedSession(
        (CAPTURE_A,),
        capture_evaluations=(evaluation("Ошибка", "boom", error=raw),),
    )
    controller = runtime.PrototypeRuntimeController(session, SERVICE)
    api = PrototypeRuntimeApi(controller, capture_points=(CAPTURE_A,))
    captured = api.execute_bsl("Результат = Capture();")
    assert captured.kind is RuntimeReplyKind.CAPTURED
    before = session.continue_count

    reply = api.execute_bsl("РезультатИнструкции = 1 / 0;")

    assert reply.kind is RuntimeReplyKind.CAPTURE_CELL
    assert reply.succeeded is False
    assert reply.error == "BSL execution failed"
    assert reply.diagnostic is not None
    assert reply.diagnostic.stage is DiagnosticStage.EXECUTION
    assert reply.diagnostic.mapping_confidence is MappingConfidence.EXACT
    assert reply.diagnostic.visible_location is not None
    assert (
        reply.diagnostic.visible_location.line,
        reply.diagnostic.visible_location.column,
    ) == (1, 25)
    assert reply.state is runtime.OperationState.CAPTURED
    assert controller.state is runtime.OperationState.CAPTURED
    assert session.continue_count == before
    assert workspace_calls(session)[-2:] == [
        (SERVICE,),
        (SERVICE, CAPTURE_A),
    ]


def test_normalizer_failure_keeps_capture_failure_private_and_paused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: missing diagnostics must not leak raw CAPTURE prose."""
    runtime = runtime_module()
    raw = "{<Неизвестный модуль>(3,25)}: PRIVATE_PLATFORM_CAPTURE_TEXT"
    session = ScriptedSession(
        (CAPTURE_A,),
        capture_evaluations=(evaluation("Ошибка", "boom", error=raw),),
        messages=("before capture failure",),
        messages_in_result_slot=True,
    )
    controller = captured_controller(session)
    api = PrototypeRuntimeApi(controller)
    before = session.continue_count

    def fail_normalization(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("forced normalizer failure")

    monkeypatch.setattr(runtime, "remap_platform_diagnostic", fail_normalization)

    failed = api.execute_bsl(
        'Сообщить("before capture failure");\n'
        "КонтекстОтладки.Скаляр = 778;\n"
        "РезультатИнструкции = 1 / 0;"
    )

    assert failed.kind is RuntimeReplyKind.CAPTURE_CELL
    assert failed.succeeded is False
    assert failed.error == "BSL execution failed"
    assert raw not in failed.error
    assert failed.diagnostic is None
    assert failed.messages == ("before capture failure",)
    assert failed.capture_dirty_roots == ("Скаляр",)
    assert failed.state is runtime.OperationState.CAPTURED
    assert controller.state is runtime.OperationState.CAPTURED
    assert session.continue_count == before
    assert workspace_calls(session)[-2:] == [
        (SERVICE, USER),
        (SERVICE, CAPTURE_A, CAPTURE_B, USER),
    ]


def test_failed_capture_retains_dirty_root_for_reply_and_resume_writeback() -> None:
    """Break caught: failed CAPTURE cells must not drop proven dirty roots."""
    runtime = runtime_module()
    raw = "{<Неизвестный модуль>(3,25)}: Деление на 0"
    session = ScriptedSession(
        (CAPTURE_A, SERVICE),
        capture_evaluations=(evaluation("Ошибка", "boom", error=raw),),
    )
    controller = captured_controller(session)
    api = PrototypeRuntimeApi(controller)

    failed = api.execute_bsl(
        "НовоеИмя = 1;\n"
        "КонтекстОтладки.Скаляр = 778;\n"
        "РезультатИнструкции = 1 / 0;"
    )

    assert failed.succeeded is False
    assert failed.changed_roots == ("НовоеИмя",)
    assert failed.capture_dirty_roots == ("Скаляр",)

    resumed = api.resume_capture()

    assert resumed.kind is RuntimeReplyKind.MAIN_COMPLETED
    scalar_writes = [
        value
        for name, value in session.calls
        if name == "modify"
        and isinstance(value, tuple)
        and value[0] == "Скаляр"
    ]
    assert len(scalar_writes) == 1
    assert "e1cib/tempstorage/root" in scalar_writes[0][1]


def test_table_schema_sample_uses_the_capture_kernel_frame() -> None:
    session = ScriptedSession((), messages=("value",))
    controller = runtime_module().PrototypeRuntimeController(session, SERVICE)
    controller.state = runtime_module().OperationState.CAPTURED
    controller.capture_kernel_stack_level = 2

    result = controller.inspect_table_sample("Контекст.Таблица", page_size=16)

    assert result.collection_size == 1
    assert (
        "evaluate_collection",
        ("Контекст.Таблица", 0, 16, 2),
    ) in session.calls


def test_declared_table_schema_uses_extension_method_in_capture_kernel_frame() -> None:
    session = ScriptedSession((), messages=("value",))
    controller = runtime_module().PrototypeRuntimeController(session, SERVICE)
    controller.state = runtime_module().OperationState.CAPTURED
    controller.capture_kernel_stack_level = 2

    controller.inspect_declared_table_schema("Контекст.Таблица")

    assert (
        "evaluate_collection",
        (
            "RuntimeTableTransferServer.ПолучитьКомпактнуюСхему(Контекст.Таблица)",
            0,
            64,
            2,
        ),
    ) in session.calls


def test_completion_fields_use_bounded_schema_helper_in_capture_kernel_frame() -> None:
    session = ScriptedSession((), messages=("value",))
    controller = runtime_module().PrototypeRuntimeController(session, SERVICE)
    controller.state = runtime_module().OperationState.CAPTURED
    controller.capture_kernel_stack_level = 2

    controller.inspect_completion_fields("Контекст.Данные", table_row=True)

    assert ("evaluate_collection", (
        "RuntimeValueTransferServer.ПолучитьИменаСвойствДляПодсказки(Контекст.Данные, Истина)",
        0, 128, 2,
    )) in session.calls


def workspace_calls(session: ScriptedSession) -> list[tuple[ModuleLocation, ...]]:
    return [
        value
        for name, value in session.calls
        if name == "set_breakpoints" and isinstance(value, tuple)
    ]


def captured_controller(
    session: ScriptedSession,
    **controller_kwargs: object,
):  # type: ignore[no-untyped-def]
    runtime = runtime_module()
    controller = runtime.PrototypeRuntimeController(
        session,
        SERVICE,
        **controller_kwargs,
    )
    stopped = controller.execute_main(
        "Результат = СинтетическийCapture(40);",
        capture_points=(CAPTURE_A, CAPTURE_B),
        user_breakpoints=(USER,),
    )
    assert isinstance(stopped, runtime.CapturedStop)
    return controller


def test_capture_shield_and_restore_both_preserve_worker_breakpoints() -> None:
    session = ScriptedSession(
        (CAPTURE_A,),
        capture_evaluations=(evaluation("Число", "901"),),
    )
    controller = captured_controller(session)
    confirmed = controller.breakpoint_workspace_owner.confirmed_snapshot
    desired = controller.breakpoint_workspace_owner.prepare(
        captures=confirmed.captures,
        ordinary_users=confirmed.ordinary_users,
        worker_slots=(WORKER_BREAKPOINT,),
        shielded=False,
    )
    controller.install_worker_workspace(desired)

    controller.execute_capture("РезультатИнструкции = 901;")

    evaluation_workspace, restored_workspace = workspace_calls(session)[-2:]
    assert WORKER_BREAKPOINT in evaluation_workspace
    assert CAPTURE_A not in evaluation_workspace
    assert CAPTURE_B not in evaluation_workspace
    assert USER in evaluation_workspace
    assert WORKER_BREAKPOINT in restored_workspace
    assert CAPTURE_A in restored_workspace
    assert CAPTURE_B in restored_workspace


def test_capture_rejects_prequeued_same_location_stop_with_old_kernel_command() -> None:
    session = ScriptedSession(
        (SERVICE, CAPTURE_A),
        main_results=(evaluation("Число", "1"),),
        kernel_command_values=(1,),
    )
    controller = runtime_module().PrototypeRuntimeController(session, SERVICE)

    completed = controller.execute_main("Результат = 1;")
    assert completed.operation.operation_id == 1

    with pytest.raises(ProtocolError, match="Captured command 1 does not match active operation 2"):
        controller.execute_main("Результат = 2;", capture_points=(CAPTURE_A,))

    assert controller.state is runtime_module().OperationState.FAILED
    assert controller.stop_sequence == 0
    assert ("evaluate", ("ИдентификаторКоманды", 2)) in session.calls


def test_business_frame_forging_kernel_locals_is_not_selected_before_real_kernel() -> None:
    class ForgedSession(ScriptedSession):
        def local_variables(self, stack_level: int = 0) -> LocalVariablesResult:
            if stack_level in {1, 2}:
                return LocalVariablesResult(
                    uuid4(),
                    (
                        FrameVariable("Контекст", "Структура", "Структура"),
                        FrameVariable("ТекущаяИнструкция", "Строка", '\"\"'),
                        FrameVariable("ИдентификаторКоманды", "Число", "2"),
                    ),
                )
            return super().local_variables(stack_level)

    session = ForgedSession(
        (SERVICE, CAPTURE_A),
        main_results=(evaluation("Число", "1"),),
        kernel_command_values=(1,),
        stacks=((SERVICE, SERVICE, SERVICE), (CAPTURE_A, BUSINESS, SERVICE)),
    )
    controller = runtime_module().PrototypeRuntimeController(session, SERVICE)
    controller.execute_main("Результат = 1;")

    with pytest.raises(ProtocolError, match="Captured command 1 does not match active operation 2"):
        controller.execute_main("Результат = 2;", capture_points=(CAPTURE_A,))


@pytest.mark.parametrize(
    "stack",
    [(), (CAPTURE_A,), (CAPTURE_A, BUSINESS)],
)
def test_capture_rejects_absent_or_malformed_kernel_stack_mapping(
    stack: tuple[ModuleLocation, ...]
) -> None:
    session = ScriptedSession((CAPTURE_A,), stacks=(stack,))
    controller = runtime_module().PrototypeRuntimeController(session, SERVICE)

    with pytest.raises(ProtocolError, match="stack mapping|kernel context frame"):
        controller.execute_main("Результат = 1;", capture_points=(CAPTURE_A,))

    assert controller.state is runtime_module().OperationState.FAILED


@pytest.mark.parametrize(
    "frames",
    [
        (StackFrame(TARGET, 0, CAPTURE_A), StackFrame(TARGET, 0, SERVICE)),
        (StackFrame(TARGET, 0, CAPTURE_A), StackFrame(TARGET, -1, SERVICE)),
    ],
)
def test_capture_rejects_duplicate_or_nonphysical_stack_levels(
    frames: tuple[StackFrame, ...]
) -> None:
    session = ScriptedSession(())
    session.stops.append(
        StopEvent(
            TARGET,
            CAPTURE_A,
            "callStackFormed",
            stop_by_breakpoint=True,
            stack=(CAPTURE_A, SERVICE),
            stack_frames=frames,
        )
    )
    controller = runtime_module().PrototypeRuntimeController(session, SERVICE)

    with pytest.raises(ProtocolError, match="stack mapping is incoherent"):
        controller.execute_main("Результат = 1;", capture_points=(CAPTURE_A,))

    assert controller.state is runtime_module().OperationState.FAILED


def test_capture_accepts_the_configured_kernel_module_stack_frame() -> None:
    session = ScriptedSession(
        (CAPTURE_A,), stacks=((CAPTURE_A, BUSINESS, SERVICE),)
    )
    controller = runtime_module().PrototypeRuntimeController(session, SERVICE)

    captured = controller.execute_main("Результат = 1;", capture_points=(CAPTURE_A,))

    assert isinstance(captured, runtime_module().CapturedStop)
    assert controller.capture_kernel_stack_level == 2


def test_capture_exposes_observed_stack_only_after_explicit_request() -> None:
    session = ScriptedSession(
        (CAPTURE_A,), stacks=((CAPTURE_A, BUSINESS, SERVICE),)
    )
    controller = runtime_module().PrototypeRuntimeController(session, SERVICE)

    controller.execute_main("Результат = 1;", capture_points=(CAPTURE_A,))
    calls_at_stop = tuple(session.calls)
    stack = controller.capture_stack(cursor=0, limit=20)

    assert [frame["line"] for frame in stack["frames"]] == [None, 53, None]
    assert [frame["runtime_kernel"] for frame in stack["frames"]] == [True, False, True]
    assert tuple(session.calls) == calls_at_stop


def test_capture_rejects_kernel_frame_with_different_extension_id() -> None:
    wrong_kernel = ModuleLocation(
        SERVICE.module_type,
        SERVICE.url,
        SERVICE.object_id,
        SERVICE.property_id,
        SERVICE.line,
        SERVICE.extension_name,
        ext_id=1,
    )
    session = ScriptedSession((CAPTURE_A,), stacks=((CAPTURE_A, BUSINESS, wrong_kernel),))
    controller = runtime_module().PrototypeRuntimeController(session, SERVICE)

    with pytest.raises(ProtocolError, match="kernel context frame"):
        controller.execute_main("Результат = 1;", capture_points=(CAPTURE_A,))

    assert controller.state is runtime_module().OperationState.FAILED


def test_raw_ping_stack_preserves_physical_kernel_level_across_unaddressable_frame() -> None:
    payload = f"""<response xmlns="{RDBG_NS}"><result><cmdID>callStackFormed</cmdID><stopByBP>true</stopByBP>
      <targetID><id>{TARGET.id}</id><infoBaseAlias>{TARGET.infobase_alias}</infoBaseAlias></targetID>
      <callStack><moduleID><type>{SERVICE.module_type}</type><extensionName>{SERVICE.extension_name}</extensionName><objectID>{SERVICE.object_id}</objectID>
      <propertyID>{SERVICE.property_id}</propertyID></moduleID><lineNo>{SERVICE.line}</lineNo></callStack>
      <callStack><moduleID/><lineNo>777</lineNo></callStack>
      <callStack><moduleID><type>{BUSINESS.module_type}</type><objectID>{BUSINESS.object_id}</objectID>
      <propertyID>{BUSINESS.property_id}</propertyID></moduleID><lineNo>{BUSINESS.line}</lineNo></callStack>
      <callStack><moduleID><type>{CAPTURE_A.module_type}</type><extensionName>{CAPTURE_A.extension_name}</extensionName><objectID>{CAPTURE_A.object_id}</objectID>
      <propertyID>{CAPTURE_A.property_id}</propertyID></moduleID><lineNo>{CAPTURE_A.line}</lineNo></callStack>
      </result></response>""".encode()
    stop = parse_ping_events(payload)[0]

    class Session(ScriptedSession):
        def local_variables(self, stack_level: int = 0) -> LocalVariablesResult:
            if stack_level in {1, 3}:
                self.calls.append(("local_variables", stack_level))
                return LocalVariablesResult(
                    uuid4(),
                    (
                        FrameVariable("Контекст", "Структура", "Структура"),
                        FrameVariable("ТекущаяИнструкция", "Строка", '\"\"'),
                        FrameVariable("ИдентификаторКоманды", "Число", "1"),
                    ),
                )
            return super().local_variables(stack_level)

    session = Session((), kernel_command_values=(1,))
    session.stops.append(stop)
    controller = runtime_module().PrototypeRuntimeController(session, SERVICE)

    captured = controller.execute_main("Результат = 1;", capture_points=(CAPTURE_A,))

    assert isinstance(captured, runtime_module().CapturedStop)
    assert [frame.level for frame in stop.stack_frames] == [0, 1, 3]
    assert controller.capture_kernel_stack_level == 3
    assert ("local_variables", 1) not in session.calls
    assert ("local_variables", 3) in session.calls
    assert ("evaluate", ("ИдентификаторКоманды", 3)) in session.calls


def test_system_main_sends_verbatim_bsl_without_notebook_lowering() -> None:
    runtime = runtime_module()
    session = ScriptedSession(
        (SERVICE,),
        main_results=(evaluation("Строка", '"Worker"'),),
    )
    controller = runtime.PrototypeRuntimeController(session, SERVICE)
    source = (
        'ИмяWorker = ВнешниеОбработки.Подключить("C:\\Worker.epf", Ложь); '
        "Результат = ИмяWorker;"
    )

    completed = controller.execute_system_main(source)

    instruction = next(
        value[1]
        for name, value in session.calls
        if name == "modify" and value[0] == "ТекущаяИнструкция"
    )
    assert "Контекст.Вставить" not in instruction
    assert "ВнешниеОбработки.Подключить" in instruction
    assert completed.result == "Worker"
    assert completed.operation.visible_source == source
    assert completed.operation.lowered_source == source


def test_runtime_api_main_executes_worker_result_channel_once_lowered() -> None:
    session = ScriptedSession(
        (SERVICE,), main_results=(evaluation("Число", "29"),)
    )
    controller = runtime_module().PrototypeRuntimeController(session, SERVICE)
    controller.lowerer.set_worker_exports(
        (WorkerExport("Расчет.Ндфл.Посчитать", "Посчитать"),)
    )

    reply = PrototypeRuntimeApi(controller).execute_bsl(
        "Результат = Расчет.Ндфл.Посчитать();"
    )

    assert reply.kind is RuntimeReplyKind.MAIN_COMPLETED
    assert reply.result == 29
    assert controller.active_operation is not None
    assert controller.active_operation.visible_source == "Результат = Расчет.Ндфл.Посчитать();"
    assert controller.active_operation.lowered_source == (
        "Результат = Контекст.RuntimeWorker.Посчитать();"
    )


def test_runtime_api_capture_executes_worker_result_channel_once_lowered() -> None:
    session = ScriptedSession(
        (CAPTURE_A, SERVICE), capture_evaluations=(evaluation("Число", "29"),)
    )
    controller = captured_controller(session)
    controller.lowerer.set_worker_exports(
        (WorkerExport("Расчет.Ндфл.Посчитать", "Посчитать"),)
    )
    api = PrototypeRuntimeApi(controller)

    reply = api.execute_bsl(
        "КонтекстОтладки.Скаляр = 778; "
        "РезультатИнструкции = Расчет.Ндфл.Посчитать();"
    )
    resumed = api.resume_capture()

    assert reply.kind is RuntimeReplyKind.CAPTURE_CELL
    assert reply.result == 29
    assert reply.state is runtime_module().OperationState.CAPTURED
    assert resumed.kind is RuntimeReplyKind.MAIN_COMPLETED
    capture_call = next(
        value
        for name, value in session.calls
        if name == "evaluate" and "ВыполнитьКодВКонтекстеОтладки" in str(value)
    )
    assert "РезультатИнструкции = Контекст.RuntimeWorker.Посчитать();" in str(
        capture_call
    )
    assert "Контекст.RuntimeWorker.Посчитать()" in str(capture_call)


def test_runtime_api_prepared_capture_uses_active_worker_catalog_once_without_relowering() -> None:
    # Break caught: an agent-side standalone lowerer has no active Worker
    # catalog, while a second runtime-side lowering can diverge after preflight.
    session = ScriptedSession(
        (CAPTURE_A, SERVICE), capture_evaluations=(evaluation("Число", "29"),)
    )
    controller = captured_controller(session)
    controller.lowerer.set_worker_exports(
        (WorkerExport("Расчет.Ндфл.Посчитать", "Посчитать"),)
    )
    api = PrototypeRuntimeApi(controller)
    source = (
        "// exact saved revision identity must survive AST extraction\n"
        "  РезультатИнструкции = Расчет.Ндфл.Посчитать();"
    )

    prepared = api.prepare_capture_hypothesis(source)
    reply = api.execute_prepared_capture_hypothesis(prepared)

    assert reply.kind is RuntimeReplyKind.CAPTURE_CELL
    assert reply.result == 29
    assert source not in repr(prepared)
    with pytest.raises(TypeError):
        asdict(prepared)
    with pytest.raises(TypeError):
        to_wire(prepared)
    assert deepcopy(prepared) == "<redacted prepared capture hypothesis>"
    assert source not in repr(controller.journal.events)
    assert "capture_prepared_" not in repr(controller.journal.events)
    started = next(
        event for event in controller.journal.events if event.event == "cell_started"
    )
    assert started.fields["visible_sha256"] == sha256(source.encode("utf-8")).hexdigest()
    capture_calls = [
        value
        for name, value in session.calls
        if name == "evaluate" and "ВыполнитьКодВКонтекстеОтладки" in str(value)
    ]
    assert len(capture_calls) == 1
    assert "Контекст.RuntimeWorker.Посчитать()" in str(capture_calls[0])
    with pytest.raises(ProtocolError, match="consumed"):
        api.execute_prepared_capture_hypothesis(prepared)
    assert controller.cell_sequence == 1


def test_prepared_capture_platform_failure_keeps_exact_map_and_paused_state() -> None:
    runtime = runtime_module()
    raw = "{<Неизвестный модуль>(1,25)}: Деление на 0"
    session = ScriptedSession(
        (CAPTURE_A,),
        capture_evaluations=(evaluation("Ошибка", "boom", error=raw),),
    )
    controller = captured_controller(session)
    api = PrototypeRuntimeApi(controller)
    source = "РезультатИнструкции = 1 / 0;"
    prepared = api.prepare_capture_hypothesis(source)
    lowering = prepared.contents(api._prepared_capture_owner)[4]
    expected_unit = lowering.mapped_source.source_map.map_offset(
        lowering.source.index("/")
    ).unit
    before = session.continue_count

    reply = api.execute_prepared_capture_hypothesis(prepared)

    assert reply.succeeded is False
    assert reply.state is runtime.OperationState.CAPTURED
    assert reply.diagnostic is not None
    assert reply.diagnostic.mapping_confidence is MappingConfidence.EXACT
    assert reply.diagnostic.source_unit is not None
    assert reply.diagnostic.source_unit == expected_unit
    assert expected_unit.revision == 1
    assert expected_unit.source_sha256 == source_sha256(source)
    assert reply.diagnostic.visible_location is not None
    assert reply.diagnostic.visible_location.column == 25
    assert session.continue_count == before


def test_failed_prepared_capture_retains_dirty_root_for_resume_writeback() -> None:
    """Break caught: prepared CAPTURE failures must stage their dirty roots."""
    runtime = runtime_module()
    raw = "{<Неизвестный модуль>(3,25)}: Деление на 0"
    session = ScriptedSession(
        (CAPTURE_A, SERVICE),
        capture_evaluations=(evaluation("Ошибка", "boom", error=raw),),
    )
    controller = captured_controller(session)
    api = PrototypeRuntimeApi(controller)
    prepared = api.prepare_capture_hypothesis(
        "НовоеИмя = 1;\n"
        "КонтекстОтладки.Скаляр = 778;\n"
        "РезультатИнструкции = 1 / 0;"
    )

    failed = api.execute_prepared_capture_hypothesis(prepared)

    assert failed.succeeded is False
    assert failed.changed_roots == ("НовоеИмя",)
    assert failed.capture_dirty_roots == ("Скаляр",)

    resumed = api.resume_capture()

    assert resumed.kind is RuntimeReplyKind.MAIN_COMPLETED
    scalar_writes = [
        value
        for name, value in session.calls
        if name == "modify"
        and isinstance(value, tuple)
        and value[0] == "Скаляр"
    ]
    assert len(scalar_writes) == 1
    assert "e1cib/tempstorage/root" in scalar_writes[0][1]


def test_runtime_api_rejects_prepared_capture_after_worker_catalog_replacement_before_mutation() -> None:
    session = ScriptedSession((CAPTURE_A, SERVICE))
    controller = captured_controller(session)
    controller.lowerer.set_worker_exports(
        (WorkerExport("Расчет.Ндфл.Посчитать", "Посчитать"),)
    )
    api = PrototypeRuntimeApi(controller)
    prepared = api.prepare_capture_hypothesis(
        "РезультатИнструкции = Расчет.Ндфл.Посчитать();"
    )
    controller.lowerer.set_worker_exports(
        (WorkerExport("Расчет.Ндфл.Версия", "Версия"),)
    )

    with pytest.raises(ProtocolError, match="stale"):
        api.execute_prepared_capture_hypothesis(prepared)

    assert controller.cell_sequence == 0
    assert not any(
        name == "evaluate" and "ВыполнитьКодВКонтекстеОтладки" in str(value)
        for name, value in session.calls
    )


def test_runtime_api_preparation_quarantine_clears_controller_inspection_without_continue() -> None:
    # Break caught: a controller/session preparation fault must revoke even
    # direct inspection paths while leaving the suspended target untouched.
    session = ScriptedSession((CAPTURE_A, SERVICE))
    controller = captured_controller(session)
    api = PrototypeRuntimeApi(controller)
    continue_count = session.continue_count

    api.invalidate_capture_inspection()

    with pytest.raises(ProtocolError, match="quarantined|initialized"):
        api.capture_frame_variables(filters={}, cursor=0, limit=20, timeout_s=1.0)
    with pytest.raises(ProtocolError, match="initialized"):
        controller.capture_frame_variables(filters={}, cursor=0, limit=20)
    assert session.continue_count == continue_count


def test_capture_selection_passes_one_remaining_deadline_to_each_rdbg_command() -> None:
    # Break caught: the native selection helper used RDBG's default 30 seconds
    # and the following schema read received a fresh timeout, allowing N times
    # the observation budget.
    class TimeoutSession(ScriptedSession):
        def __init__(self) -> None:
            super().__init__((CAPTURE_A, SERVICE))
            self.capture_timeouts: list[float] = []

        def evaluate(
            self,
            expression: str,
            *,
            stack_level: int = 0,
            timeout_s: float = 30.0,
            max_text_size: int = 307_200,
        ) -> EvaluationResult:
            if "СохранитьВременнуюТаблицуОтладки" in expression:
                self.capture_timeouts.append(timeout_s)
                return evaluation("Булево", "Истина")
            return super().evaluate(
                expression,
                stack_level=stack_level,
                timeout_s=timeout_s,
                max_text_size=max_text_size,
            )

        def evaluate_collection(self, expression: str, **kwargs: object) -> EvaluationResult:
            if "ПолучитьКомпактнуюСхему" in expression:
                self.capture_timeouts.append(float(kwargs["timeout_s"]))
            return super().evaluate_collection(expression, **kwargs)  # type: ignore[arg-type]

    session = TimeoutSession()
    controller = captured_controller(session)
    api = PrototypeRuntimeApi(controller)
    continue_count = session.continue_count
    manager = api.resolve_capture_manager_origin(
        ManagerOrigin(
            "frame", "Результат", ("МенеджерВременныхТаблиц",)
        ),
        timeout_s=1.5,
    )

    api.capture_temporary_tables(
        manager["handle"],
        names=("Итоги",),
        cursor=0,
        limit=20,
        selection=ValueSelection(
            SelectionKind.TABLE_ROWS,
            offset=0,
            limit=1,
            columns=("Сумма",),
        ),
        timeout_s=1.5,
    )

    assert len(session.capture_timeouts) == 2
    assert all(0 < timeout <= 1.5 for timeout in session.capture_timeouts)
    assert session.capture_timeouts[1] <= session.capture_timeouts[0]
    assert session.continue_count == continue_count


def test_production_manager_origin_proves_full_frame_chain_and_casefolds_aliases(
    tmp_path: Path,
) -> None:
    # The controller must prove the leaf type in the suspended business frame;
    # a syntactically valid root alone is not authority to mint a manager.
    private = "RAW_MANAGER_VALUE_SECRET"

    class ManagerOriginSession(ScriptedSession):
        def __init__(self) -> None:
            super().__init__((CAPTURE_A, SERVICE))
            self.manager_probes: list[tuple[str, int, float]] = []

        def evaluate(
            self,
            expression: str,
            *,
            stack_level: int = 0,
            timeout_s: float = 30.0,
            max_text_size: int = 307_200,
        ) -> EvaluationResult:
            if expression.startswith("ТипЗнч("):
                self.manager_probes.append((expression, stack_level, timeout_s))
                folded = expression.casefold()
                if "несуществующий" in folded:
                    return evaluation("Ошибка", private, error=private)
                if expression == 'ТипЗнч(Скаляр) = Тип("МенеджерВременныхТаблиц")':
                    return evaluation("Булево", "Ложь")
                if "живойнеоднозначный" in folded:
                    return evaluation("Строка", '"Истина"')
                expected = (
                    'типзнч(результат.запрос.вложенный.'
                    'менеджервременныхтаблиц) = '
                    'тип("менеджервременныхтаблиц")'
                )
                if folded == expected:
                    return evaluation("Булево", "Истина")
                return evaluation("Булево", "Ложь")
            return super().evaluate(
                expression,
                stack_level=stack_level,
                timeout_s=timeout_s,
                max_text_size=max_text_size,
            )

    session = ManagerOriginSession()
    controller = captured_controller(session)
    api = PrototypeRuntimeApi(controller)
    continue_count = session.continue_count
    origin = ManagerOrigin(
        "frame",
        "результат",
        ("Запрос", "Вложенный", "МенеджерВременныхТаблиц"),
    )

    first = api.resolve_capture_manager_origin(origin, timeout_s=1.5)
    alias = api.resolve_capture_manager_origin(
        ManagerOrigin(
            "frame",
            "РЕЗУЛЬТАТ",
            ("запрос", "ВЛОЖЕННЫЙ", "менеджервременныхтаблиц"),
        ),
        timeout_s=1.5,
    )
    probe_count_after_alias = len(session.manager_probes)

    rejected = (
        ManagerOrigin("frame", "Результат", ("Несуществующий",)),
        ManagerOrigin("frame", "Скаляр", ()),
        ManagerOrigin("frame", "Результат", ("ЖивойНеоднозначный",)),
    )
    rejected_errors: list[ProtocolError | None] = []
    for invalid in rejected:
        try:
            api.resolve_capture_manager_origin(invalid, timeout_s=1.5)
        except ProtocolError as error:
            rejected_errors.append(error)
        else:
            rejected_errors.append(None)

    assert (
        first == alias,
        len(session.manager_probes),
        tuple(error is not None for error in rejected_errors),
        probe_count_after_alias,
    ) == (True, 4, (True, True, True), 1)
    assert first["type_name"] == "МенеджерВременныхТаблиц"
    assert set(first) == {"key", "handle", "type_name"}
    expression, stack_level, timeout_s = session.manager_probes[0]
    assert expression == (
        'ТипЗнч(Результат.Запрос.Вложенный.МенеджерВременныхТаблиц) '
        '= Тип("МенеджерВременныхТаблиц")'
    )
    assert stack_level == 0
    assert 0 < timeout_s <= 1.5
    assert controller._capture_manager_paths[first["handle"]] == (
        "Контекст.КонтекстОтладки.Результат.Запрос.Вложенный."
        "МенеджерВременныхТаблиц"
    )

    assert all(
        error is not None and private not in str(error)
        for error in rejected_errors
    )
    assert len(controller._capture_manager_paths) == 1
    assert len(session.manager_probes) == 4
    assert all(level == 0 and 0 < timeout <= 1.5 for _, level, timeout in session.manager_probes)
    assert session.continue_count == continue_count

    # Carry the same real controller/RDBG seam through the production backend
    # and CaptureService.  Case aliases must converge on one public manager ID
    # while provenance stays identifier-only.
    demo = RuntimeSession.__new__(RuntimeSession)
    demo._operation_lock = RLock()
    demo._closed = False
    demo.config = type("Config", (), {"chunk_size": 2400})()
    demo.runtime_api = api
    fence = CaptureFence("manager-intent", "manager-operation", 7, "a" * 64, 1, 1)
    demo._active_capture_ticket = _ActiveCaptureTicket(
        "private-ticket",
        fence.capture_intent_id,
        fence.operation_id,
        fence.capture_generation,
        fence.source_revision,
        fence.source_sha256,
        fence.stop_sequence,
    )
    backend = OnecRuntimeBackend(
        "runtime-manager-origin", AgentRuntimeSession(demo), mode=CapabilityMode.EXPERIMENT
    )
    capture = CaptureService(tmp_path)
    registry = ProxyRegistry()
    capture.activate_capture_view(
        CaptureView(
            fence,
            ResolvedCapturePoint(
                "before", "zup", "Payroll", "Run", 17, 7, "a" * 64, 17,
                "Выполнить();",
            ),
            None,
        ),
        registry,
    )

    def manager_plan(manager_origin: ManagerOrigin) -> ObservationPlan:
        return ObservationPlan(
            (
                ObservationItem(
                    "manager",
                    ObservationSource(
                        ObservationSourceKind.TEMPORARY_TABLE_MANAGER,
                        origin=manager_origin,
                    ),
                ),
            )
        )

    first_inspection = capture.inspect(
        backend,
        registry,
        fence=fence,
        runtime_id=backend.runtime_id,
        runtime_generation=1,
        context_generation=1,
        filters={},
        cursor=0,
        limit=20,
        observe=manager_plan(origin),
        timeout_s=1.5,
    )
    alias_inspection = capture.inspect(
        backend,
        registry,
        fence=fence,
        runtime_id=backend.runtime_id,
        runtime_generation=1,
        context_generation=1,
        filters={},
        cursor=0,
        limit=20,
        observe=manager_plan(
            ManagerOrigin(
                "frame",
                "РЕЗУЛЬТАТ",
                ("запрос", "ВЛОЖЕННЫЙ", "менеджервременныхтаблиц"),
            )
        ),
        timeout_s=1.5,
    )

    first_manager = first_inspection.temporary_table_managers[0]
    alias_manager = alias_inspection.temporary_table_managers[0]
    assert first_manager.manager_id == alias_manager.manager_id
    assert first_manager.origin == origin
    public = json.dumps(to_wire(first_manager), ensure_ascii=False, sort_keys=True)
    assert private not in public and "private-ticket" not in public


def test_capture_hypothesis_preflight_and_replay_use_the_active_runtime_worker_catalog(
    tmp_path: Path,
) -> None:
    # Break caught: service-side standalone lowering rejects a valid active
    # Worker export, and replay must never consume a second prepared artifact.
    rdbg = ScriptedSession(
        (CAPTURE_A, SERVICE), capture_evaluations=(evaluation("Число", "29"),)
    )
    controller = captured_controller(rdbg)
    controller.lowerer.set_worker_exports(
        (WorkerExport("Расчет.Ндфл.Посчитать", "Посчитать"),)
    )
    api = PrototypeRuntimeApi(controller)
    legacy_execute_calls = 0

    def reject_legacy_execute(source: str):  # type: ignore[no-untyped-def]
        nonlocal legacy_execute_calls
        legacy_execute_calls += 1
        raise AssertionError(f"hypothesis relowered through execute_bsl: {source}")

    api.execute_bsl = reject_legacy_execute  # type: ignore[method-assign]
    session = RuntimeSession.__new__(RuntimeSession)
    session._operation_lock = RLock()
    session._closed = False
    session.runtime_api = api
    fence = CaptureFence("capture-intent", "op-capture", 7, "a" * 64, 1, 1)
    session._active_capture_ticket = _ActiveCaptureTicket(
        "ticket",
        fence.capture_intent_id,
        fence.operation_id,
        fence.capture_generation,
        fence.source_revision,
        fence.source_sha256,
        fence.stop_sequence,
    )
    session.close = lambda: None  # type: ignore[method-assign]
    backend = OnecRuntimeBackend(
        "runtime-worker", AgentRuntimeSession(session), mode=CapabilityMode.EXPERIMENT
    )
    source = (
        "КонтекстОтладки.Сумма = 3; "
        "РезультатИнструкции = Расчет.Ндфл.Посчитать();"
    )
    digest = sha256(source.encode()).hexdigest()
    cell = nbformat.v4.new_code_cell(source=source, id="cell-worker-hypothesis")
    cell.metadata["onec_runtime"] = {
        "revision": 1,
        "language": "bsl",
        "mode": "capture",
        "source_sha256": digest,
    }
    nbformat.write(nbformat.v4.new_notebook(cells=[cell]), tmp_path / "worker.ipynb")
    service = AgentWorkspaceService(
        tmp_path,
        type("Factory", (), {"start": lambda self, *, mode: backend})(),
        maximum_mode=CapabilityMode.EXPERIMENT,
    )
    service._runtime = _AdmittedRuntime(
        backend, backend.runtime_id, 1, CapabilityMode.EXPERIMENT
    )
    service._selected["default"] = backend.runtime_id
    service._capture.activate_capture_view(
        CaptureView(
            fence,
            ResolvedCapturePoint(
                "before", "zup", "Payroll", "Run", 17, 7, "a" * 64, 17,
                "Выполнить();",
            ),
            None,
        ),
        service._proxy_registry,
    )
    assert service.call("code.list", {"container": "worker.ipynb"}).ok
    request = {
        "fence": {
            "capture_intent_id": fence.capture_intent_id,
            "operation_id": fence.operation_id,
            "source_revision": fence.source_revision,
            "source_sha256": fence.source_sha256,
            "capture_generation": fence.capture_generation,
            "stop_sequence": fence.stop_sequence,
        },
        "code_ref": {
            "cell_id": "cell-worker-hypothesis",
            "revision": 1,
            "source_sha256": digest,
        },
        "request_id": "worker-hypothesis-replay",
        "wait_s": 2.0,
    }

    replacement: AgentWorkspaceService | None = None
    try:
        first = AgentFacade(service).capture_hypothesis(request)  # type: ignore[arg-type]
        replacement = AgentWorkspaceService(
            tmp_path,
            type("Factory", (), {"start": lambda self, *, mode: (_ for _ in ()).throw(AssertionError(mode))})(),
            maximum_mode=CapabilityMode.EXPERIMENT,
        )
        assert replacement.call("code.list", {"container": "worker.ipynb"}).ok
        replay = AgentFacade(replacement).capture_hypothesis(request)  # type: ignore[arg-type]

        assert first.state is AgentOperationState.CAPTURED
        assert replay.operation.operation_id == first.operation.operation_id
        assert first.capture is not None and first.capture.dirty_roots == ("Сумма",)
        assert legacy_execute_calls == 0
        public_wire = to_wire(first)
        encoded_wire = json.dumps(public_wire, ensure_ascii=False, sort_keys=True)
        assert source not in encoded_wire
        assert "capture_prepared_" not in encoded_wire
        assert "prepared" not in encoded_wire.casefold()
        capture_calls = [
            value
            for name, value in rdbg.calls
            if name == "evaluate" and "ВыполнитьКодВКонтекстеОтладки" in str(value)
        ]
        assert len(capture_calls) == 1
        assert str(capture_calls[0]).count(
            "Контекст.RuntimeWorker.Посчитать()"
        ) == 1
    finally:
        if replacement is not None:
            replacement.close()
        service.close()


def test_unselected_preview_uses_one_native_selection_and_one_transfer_through_full_stack(
    tmp_path: Path,
) -> None:
    # Controller -> RuntimeApi -> RuntimeSession -> backend -> service.
    # The controller owns both metadata and selected handles and rejects the
    # metadata handle from materialization, so this cannot pass via a fake-only
    # full-table resolver.
    payload = json.dumps(
        {"version": 1, "root": {"t": "null"}}, separators=(",", ":")
    ).encode("utf-8")
    encoded = b64encode(payload).decode("ascii")
    metadata = (
        f"1|1|{len(payload)}|{sha256(payload).hexdigest()}|{len(encoded)}"
    )

    class StrictCaptureSession(ScriptedSession):
        def __init__(self) -> None:
            super().__init__(
                (CAPTURE_A, SERVICE),
                capture_evaluations=(
                    evaluation("Число", "3"),
                    evaluation("Строка", '"value"'),
                    evaluation("Строка", '"' + metadata + '"'),
                ),
                compact_payload=encoded,
            )
            self.selection_timeouts: list[float] = []

        def evaluate(
            self,
            expression: str,
            *,
            stack_level: int = 0,
            timeout_s: float = 30.0,
            max_text_size: int = 307_200,
        ) -> EvaluationResult:
            if "СохранитьВременнуюТаблицуОтладки" in expression:
                self.selection_timeouts.append(timeout_s)
                self.calls.append(("evaluate", expression))
                return evaluation("Булево", "Истина")
            return super().evaluate(
                expression,
                stack_level=stack_level,
                timeout_s=timeout_s,
                max_text_size=max_text_size,
            )

        def evaluate_collection(self, expression: str, **kwargs: object) -> EvaluationResult:
            if "ПолучитьСхемуВременнойТаблицыОтладки" in expression:
                row = CollectionRow(
                    0,
                    (
                        CollectionCell(
                            "Имя",
                            "Строка",
                            '"Сумма"',
                            value_string="Сумма",
                        ),
                    ),
                )
                return EvaluationResult(
                    uuid4(),
                    "Массив",
                    "Массив",
                    False,
                    collection_size=1,
                    collection_rows=(row,),
                )
            if "ПолучитьКомпактнуюСхему" in expression:
                return EvaluationResult(
                    uuid4(),
                    "Массив",
                    "Массив",
                    False,
                    collection_size=0,
                    collection_rows=(),
                )
            return super().evaluate_collection(expression, **kwargs)  # type: ignore[arg-type]

    rdbg = StrictCaptureSession()
    controller = captured_controller(rdbg)
    api = PrototypeRuntimeApi(controller)
    session = RuntimeSession.__new__(RuntimeSession)
    session._operation_lock = RLock()
    session._closed = False
    session.config = type("Config", (), {"chunk_size": 2400})()
    session.runtime_api = api
    fence = CaptureFence("capture-intent", "op-capture", 7, "a" * 64, 1, 1)
    session._active_capture_ticket = _ActiveCaptureTicket(
        "ticket",
        fence.capture_intent_id,
        fence.operation_id,
        fence.capture_generation,
        fence.source_revision,
        fence.source_sha256,
        fence.stop_sequence,
    )
    session.close = lambda: None  # type: ignore[method-assign]
    backend = OnecRuntimeBackend(
        "runtime-strict-preview", AgentRuntimeSession(session), mode=CapabilityMode.EXPERIMENT
    )
    source = "РезультатИнструкции = 3;"
    digest = sha256(source.encode()).hexdigest()
    cell = nbformat.v4.new_code_cell(source=source, id="cell-preview")
    cell.metadata["onec_runtime"] = {
        "revision": 1,
        "language": "bsl",
        "mode": "capture",
        "source_sha256": digest,
    }
    nbformat.write(nbformat.v4.new_notebook(cells=[cell]), tmp_path / "preview.ipynb")
    service = AgentWorkspaceService(
        tmp_path,
        type("Factory", (), {"start": lambda self, *, mode: backend})(),
        maximum_mode=CapabilityMode.EXPERIMENT,
    )
    service._runtime = _AdmittedRuntime(
        backend, backend.runtime_id, 1, CapabilityMode.EXPERIMENT
    )
    service._selected["default"] = backend.runtime_id
    service._install_onec_resolver(backend, seed_namespace=False)
    service._capture.activate_capture_view(
        CaptureView(
            fence,
            ResolvedCapturePoint(
                "before", "zup", "Payroll", "Run", 17, 7, "a" * 64, 17,
                "Выполнить();",
            ),
            None,
        ),
        service._proxy_registry,
    )
    assert service.call("code.list", {"container": "preview.ipynb"}).ok
    manager_response = service.call(
        "capture.inspect",
        {
            "fence": to_wire(fence),
            "filters": {},
            "cursor": 0,
            "limit": 20,
            "observe": {
                "items": [
                    {
                        "alias": "manager",
                        "source": {
                            "kind": "temporary_table_manager",
                            "origin": {
                                "namespace": "frame",
                                "root": "Результат",
                                "fields": ["МенеджерВременныхТаблиц"],
                            },
                        },
                    }
                ]
            },
        },
    )
    assert manager_response.ok
    manager_id = manager_response.value.temporary_table_managers[0].manager_id
    request = {
        "fence": to_wire(fence),
        "code_ref": {
            "cell_id": "cell-preview",
            "revision": 1,
            "source_sha256": digest,
        },
        "request_id": "strict-preview",
        "wait_s": 2.0,
        "observe": {
            "budget_profile": "agent_preview",
            "items": [
                {
                    "alias": "preview",
                    "source": {
                        "kind": "temporary_table",
                        "manager_id": manager_id,
                        "table": "Итоги",
                    },
                    "result": "preview",
                }
            ],
        },
    }
    try:
        response = service.call("capture.hypothesis", request)

        assert response.ok
        assert set(response.value.outputs) == {"preview"}, (
            response.value.failure,
            rdbg.calls,
        )
        assert response.value.outputs["preview"].bounded_preview is not None
        native_selections = [
            value
            for name, value in rdbg.calls
            if name == "evaluate" and "СохранитьВременнуюТаблицуОтладки" in str(value)
        ]
        transfers = [
            value
            for name, value in rdbg.calls
            if name == "evaluate" and "СериализоватьЗначение" in str(value)
        ]
        assert len(native_selections) == 1
        assert len(transfers) == 1
        assert len(rdbg.selection_timeouts) == 1
        assert 0 < rdbg.selection_timeouts[0] <= 5.0
        assert rdbg.continue_count == 1
    finally:
        service.close()


def test_notebook_message_cell_uses_isolated_collector_and_returns_messages() -> None:
    runtime = runtime_module()
    expected_messages = ("первое\nсообщение", "", "второе")
    session = ScriptedSession(
        (SERVICE,),
        main_results=(evaluation("Число", "42"),),
        messages=expected_messages,
    )
    controller = runtime.PrototypeRuntimeController(session, SERVICE)

    completed = controller.execute_main('Сообщить("первое"); Сообщить("второе");')

    instruction = completed.operation.lowered_source
    assert 'Контекст.Вставить("__onec_cell_messages_1_1_0", Новый Массив);' in instruction
    assert "Контекст.__onec_cell_messages_1_1_0.Добавить(Строка(" in instruction
    assert "Исключение" in instruction
    assert instruction.count(
        'Контекст.Вставить("__onec_cell_messages_result_key"'
    ) == 2
    assert completed.result == 42
    assert completed.messages == expected_messages
    assert not any(
        name == "evaluate" and str(value).startswith("Контекст.Удалить(")
        for name, value in session.calls
    )


def test_notebook_messages_are_read_with_one_kernel_context_json_call() -> None:
    runtime = runtime_module()
    session = ScriptedSession(
        (SERVICE,),
        messages=("first", "", "last"),
    )
    controller = runtime.PrototypeRuntimeController(session, SERVICE)

    completed = controller.execute_main(
        'Сообщить("first"); Сообщить(""); Сообщить("last");'
    )

    assert completed.messages == ("first", "", "last")
    message_calls = [
        value
        for name, value in session.calls
        if name == "evaluate" and "ЗабратьСообщенияЯчейкиИзКонтекста" in str(value)
    ]
    assert len(message_calls) == 1
    assert isinstance(message_calls[0], str)


def test_compact_table_payload_is_taken_once_from_kernel_context() -> None:
    runtime = runtime_module()
    session = ScriptedSession((), compact_payload="QUJD")
    controller = runtime.PrototypeRuntimeController(session, SERVICE)
    key = "__onec_compact_table_0123456789abcdef0123456789abcdef"

    payload = controller.take_context_string(key, max_text_size=100_000_000)

    assert payload == "QUJD"
    calls = [
        value
        for name, value in session.calls
        if name == "evaluate"
        and "ЗабратьКомпактнуюМатериализациюИзКонтекста" in str(value)
    ]
    assert len(calls) == 1
    assert key in str(calls[0])


def test_system_main_is_rejected_while_capture_is_active() -> None:
    session = ScriptedSession((CAPTURE_A,))
    controller = captured_controller(session)

    with pytest.raises(ProtocolError, match="captured"):
        controller.execute_system_main("Результат = 1;")


def test_system_capture_sends_verbatim_bsl_without_notebook_lowering() -> None:
    session = ScriptedSession(
        (CAPTURE_A,),
        capture_evaluations=(evaluation("Строка", '"v2"'),),
    )
    controller = captured_controller(session)
    context_names_before = controller.lowerer.context_names
    source = "Результат = Контекст.RuntimeWorker.Версия();"

    cell = controller.execute_system_capture(source)

    capture_call = next(
        value
        for name, value in reversed(session.calls)
        if name == "evaluate"
        and "ВыполнитьКодВКонтекстеОтладки" in str(value)
    )
    assert source in str(capture_call)
    assert "Контекст.Вставить" not in capture_call
    assert cell.result == "v2"
    assert cell.visible_source == source
    assert cell.lowered_source == source
    assert controller.lowerer.context_names == context_names_before
    assert controller.state is runtime_module().OperationState.CAPTURED


def test_system_capture_is_rejected_without_active_capture() -> None:
    runtime = runtime_module()
    session = ScriptedSession(())
    controller = runtime.PrototypeRuntimeController(session, SERVICE)

    with pytest.raises(ProtocolError, match="idle"):
        controller.execute_system_capture("Результат = 1;")


def test_rejected_notebook_main_does_not_mutate_later_capture_lowering() -> None:
    session = ScriptedSession((CAPTURE_A,))
    controller = captured_controller(session)

    with pytest.raises(ProtocolError, match="captured"):
        controller.execute_main("ОтклоненнаяПеременная = 1;")

    assert "отклоненнаяпеременная" not in controller.lowerer.context_names

    controller.execute_capture("РезультатИнструкции = ОтклоненнаяПеременная;")

    capture_call = next(
        value
        for name, value in reversed(session.calls)
        if name == "evaluate"
        and "ВыполнитьКодВКонтекстеОтладки" in str(value)
    )
    assert "Контекст.ОтклоненнаяПеременная" not in str(capture_call)
    assert "ОтклоненнаяПеременная" in str(capture_call)


def test_capture_cell_journals_hashes_without_source_or_values() -> None:
    rows: list[tuple[str, object]] = []
    journal = RecoveryJournal(lambda name, value: rows.append((name, value)))
    session = ScriptedSession(
        (CAPTURE_A,),
        capture_evaluations=(evaluation("Число", "901"),),
    )
    controller = captured_controller(session, journal=journal)

    controller.execute_capture("РезультатИнструкции = 901;")

    events = [
        value
        for name, value in rows
        if name == "write-journal.jsonl" and isinstance(value, dict)
    ]
    assert [event["event"] for event in events[-2:]] == [
        "cell_started",
        "cell_completed",
    ]
    assert len(events[-2]["visible_sha256"]) == 64
    assert "source" not in events[-2]
    assert "result" not in events[-1]


def test_resume_journals_each_root_before_and_after_modify() -> None:
    rows: list[tuple[str, object]] = []
    journal = RecoveryJournal(lambda name, value: rows.append((name, value)))
    session = ScriptedSession((CAPTURE_A, SERVICE))
    controller = captured_controller(session, journal=journal)

    controller.resume(dirty_roots=("Скаляр",))

    events = [
        str(value["event"])
        for name, value in rows
        if name == "write-journal.jsonl" and isinstance(value, dict)
    ]
    planned = events.index("root_write_planned")
    succeeded = events.index("root_write_succeeded")
    cleanup = events.index("capture_cleanup_completed")
    continued = events.index("continue_acknowledged")
    assert planned < succeeded < cleanup < continued


def test_captured_fault_recovers_only_exact_paused_evidence() -> None:
    fault = CloseTransportAt(FaultPoint.AFTER_CAPTURE_CHECKPOINT, lambda: None)
    old_session = ScriptedSession((CAPTURE_A,))
    controller = runtime_module().PrototypeRuntimeController(
        old_session,
        SERVICE,
        fault_hook=fault,
    )

    with pytest.raises(InjectedTransportFailure):
        controller.execute_main(
            "Результат = СинтетическийCapture(40);",
            capture_points=(CAPTURE_A,),
        )

    new_session = ScriptedSession(())

    def reconnect(checkpoint, observe_executing):  # type: ignore[no-untyped-def]
        assert observe_executing is False
        stop = StopEvent(TARGET, CAPTURE_A, "callStackFormed", stack=(CAPTURE_A,))
        return ReconnectedSession(
            new_session,
            RecoveryIdentityEvidence(new_session.target, stop),
        )

    result = controller.recover_transport(reconnect)

    assert result.outcome is RecoveryOutcome.RECOVERED
    assert controller.state is runtime_module().OperationState.CAPTURED
    assert controller.session is new_session
    assert old_session.invalidated is True


def test_flushing_fault_never_replays_or_continues() -> None:
    fault = CloseTransportAt(FaultPoint.AFTER_FIRST_ROOT_WRITE, lambda: None)
    session = ScriptedSession((CAPTURE_A,))
    controller = captured_controller(session, fault_hook=fault)

    with pytest.raises(InjectedTransportFailure):
        controller.resume(dirty_roots=("Скаляр", "Результат"))
    reconnect_calls: list[object] = []
    result = controller.recover_transport(
        lambda *args: reconnect_calls.append(args)  # type: ignore[arg-type,return-value]
    )

    assert result.outcome is RecoveryOutcome.LOST
    assert controller.state is runtime_module().OperationState.LOST
    root_calls = [
        call
        for call in session.calls
        if call[0] == "modify" and call[1][0] in {"Скаляр", "Результат"}
    ]
    assert root_calls == [
        (
            "modify",
            (
                "Скаляр",
                build_temporary_storage_value_expression(
                    "e1cib/tempstorage/root"
                ),
            ),
        )
    ]
    assert session.continue_count == 1
    assert reconnect_calls == []


def test_resuming_fault_does_not_send_second_continue() -> None:
    fault = CloseTransportAt(FaultPoint.AFTER_CONTINUE_ACK, lambda: None)
    session = ScriptedSession((CAPTURE_A,))
    controller = captured_controller(session, fault_hook=fault)

    with pytest.raises(InjectedTransportFailure):
        controller.resume()
    before = session.continue_count
    new_session = ScriptedSession((SERVICE,))
    new_session.current_command = controller.active_operation.operation_id

    def reconnect(checkpoint, observe_executing):  # type: ignore[no-untyped-def]
        assert observe_executing is True
        return ReconnectedSession(new_session, None)

    result = controller.recover_transport(reconnect)

    assert result.outcome is RecoveryOutcome.RECOVERED
    assert session.continue_count == before
    assert isinstance(result.continuation, runtime_module().MainCompletion)


def test_captured_recovery_mismatch_loses_generation_once() -> None:
    lost: list[object] = []
    fault = CloseTransportAt(FaultPoint.AFTER_CAPTURE_CHECKPOINT, lambda: None)
    session = ScriptedSession((CAPTURE_A,))
    controller = runtime_module().PrototypeRuntimeController(
        session,
        SERVICE,
        fault_hook=fault,
        on_generation_lost=lost.append,
    )
    with pytest.raises(InjectedTransportFailure):
        controller.execute_main("Значение = 1;", capture_points=(CAPTURE_A,))

    mismatched = ScriptedSession(())
    wrong = ModuleLocation(
        CAPTURE_A.module_type,
        CAPTURE_A.url,
        CAPTURE_A.object_id,
        CAPTURE_A.property_id,
        CAPTURE_A.line + 1,
        CAPTURE_A.extension_name,
    )
    result = controller.recover_transport(
        lambda checkpoint, observe: ReconnectedSession(
            mismatched,
            RecoveryIdentityEvidence(
                mismatched.target,
                StopEvent(TARGET, wrong, "callStackFormed", stack=(wrong,)),
            ),
        )
    )

    assert result.outcome is RecoveryOutcome.LOST
    assert len(lost) == 1
    with pytest.raises(ProtocolError, match="lost"):
        controller.execute_capture("Значение = 2;")
    with pytest.raises(ProtocolError, match="lost"):
        controller.resume()


def test_operation_identity_survives_capture_and_final_completion() -> None:
    runtime = runtime_module()
    session = ScriptedSession((CAPTURE_A, SERVICE))
    controller = runtime.PrototypeRuntimeController(session, SERVICE)

    captured = controller.execute_main(
        "МаркерНоутбука = 777;", capture_points=(CAPTURE_A,)
    )
    cell = controller.execute_capture(
        "МаркерИзCapture = МаркерНоутбука + 1; РезультатИнструкции = МаркерИзCapture;"
    )
    completed = controller.resume(dirty_roots=("Скаляр",))

    assert isinstance(captured, runtime.CapturedStop)
    assert captured.operation.operation_id == 1
    assert cell.operation_id == 1
    assert cell.result == 778
    assert isinstance(completed, runtime.MainCompletion)
    assert completed.operation.operation_id == 1
    assert completed.succeeded is True
    assert controller.state is runtime.OperationState.COMPLETED
    assert session.continue_count == 2
    assert any(
        name == "evaluate"
        and isinstance(value, tuple)
        and "НачатьКонтекстОтладкиВКонтексте" in value[0]
        and value[1] == 2
        for name, value in session.calls
    )
    assert (
        "modify",
        (
            "Скаляр",
            build_temporary_storage_value_expression(
                "e1cib/tempstorage/root"
            ),
        ),
    ) in session.calls


def test_rearm_capture_successor_replaces_live_workspace_before_continue() -> None:
    # Break caught: changing only RuntimeApi metadata leaves the controller's
    # frozen breakpoint registry armed at the former capture locations.
    runtime = runtime_module()
    session = ScriptedSession((CAPTURE_A, CAPTURE_B))
    controller = runtime.PrototypeRuntimeController(session, SERVICE)
    controller.execute_main("Результат = 1;", capture_points=(CAPTURE_A,))

    controller.rearm_capture_successor((CAPTURE_B,))
    resumed = controller.resume()

    assert isinstance(resumed, runtime.CapturedStop)
    assert resumed.location == CAPTURE_B
    assert controller.registry.captures == (CAPTURE_B,)
    assert controller.capture_points == (CAPTURE_B,)
    assert ("set_breakpoints", (SERVICE, CAPTURE_B)) in session.calls


def test_second_capture_keeps_main_operation_and_reinitializes_frame_structure() -> None:
    runtime = runtime_module()
    session = ScriptedSession((CAPTURE_A, CAPTURE_B, SERVICE))
    controller = runtime.PrototypeRuntimeController(session, SERVICE)

    first = controller.execute_main(
        "РезультатВызова = СинтетическийCapture(40);",
        capture_points=(CAPTURE_A, CAPTURE_B),
    )
    second = controller.resume()
    completed = controller.resume()

    assert isinstance(first, runtime.CapturedStop)
    assert isinstance(second, runtime.CapturedStop)
    assert isinstance(completed, runtime.MainCompletion)
    assert first.operation.operation_id == second.operation.operation_id == 1
    assert second.stop_sequence == 2
    begin_calls = [
        value
        for name, value in session.calls
        if name == "evaluate"
        and any(
            method in str(value)
            for method in (
                    "НачатьКонтекстОтладкиВКонтексте",
                    "ПоместитьВоВременноеХранилище",
            )
        )
    ]
    kernel_calls = [value for value in begin_calls if isinstance(value, tuple)]
    frame_calls = [value for value in begin_calls if isinstance(value, str)]
    assert len(kernel_calls) == 2
    assert len(frame_calls) == 2
    assert all(value[1] == 2 for value in kernel_calls)


def test_partial_writeback_failure_keeps_target_paused() -> None:
    runtime = runtime_module()
    session = ScriptedSession((CAPTURE_A,), failed_roots=("Скаляр",))
    controller = runtime.PrototypeRuntimeController(session, SERVICE)
    controller.execute_main("Результат = СинтетическийCapture(40);", capture_points=(CAPTURE_A,))

    with pytest.raises(runtime.PartialWritebackError, match="Скаляр"):
        controller.resume(dirty_roots=("Скаляр", "Результат"))

    assert controller.state is runtime.OperationState.PARTIAL_WRITEBACK_FAILURE
    assert session.continue_count == 1
    assert [entry.root for entry in controller.write_journal] == ["Скаляр"]
    assert controller.write_journal[0].succeeded is False


def test_continuation_attempt_evidence_is_exact_ordered_and_not_historical() -> None:
    runtime = runtime_module()
    session = ScriptedSession(
        (CAPTURE_A,), failed_roots=("Второй",)
    )
    controller = runtime.PrototypeRuntimeController(session, SERVICE)
    controller.execute_main(
        "Результат = СинтетическийCapture(40);", capture_points=(CAPTURE_A,)
    )
    # A prior generation/root journal entry must never bleed into this attempt.
    controller._set_root_status(
        "Исторический", "f" * 64, runtime.SideEffectStatus.SUCCEEDED
    )
    attempt = runtime.ContinuationAttemptSpec(
        "attempt-round4",
        capture_generation=2,
        request_operation_id="continue-request",
        dirty_roots=("Первый", "Второй", "Третий"),
    )
    admission = controller.begin_continuation_admission(attempt, (CAPTURE_B,))

    with pytest.raises(runtime.PartialWritebackError, match="Второй"):
        controller.resume(
            dirty_roots=attempt.dirty_roots,
            continuation_attempt_id=attempt.attempt_id,
        )

    evidence = controller.continuation_attempt_evidence(attempt.attempt_id)
    assert evidence.root_statuses == (
        ("Первый", "succeeded"),
        ("Второй", "failed"),
        ("Третий", "unattempted"),
    )
    assert evidence.continue_state == "unattempted"
    admission.quarantine()
    assert controller.state is runtime.OperationState.RECOVERING


def test_runtime_api_continuation_admission_restores_exact_metadata_and_controller_workspace() -> None:
    runtime = runtime_module()
    session = ScriptedSession((CAPTURE_A,))
    controller = runtime.PrototypeRuntimeController(session, SERVICE)
    api = PrototypeRuntimeApi(controller, capture_points=(CAPTURE_A,))
    controller.execute_main(
        "Результат = СинтетическийCapture(40);", capture_points=(CAPTURE_A,)
    )
    previous_ticket = api.prepare_capture_ticket()
    attempt = runtime.ContinuationAttemptSpec(
        "attempt-api-rollback",
        capture_generation=2,
        request_operation_id="continue-request",
        dirty_roots=(),
    )

    admission = api.begin_continuation_admission(attempt, (CAPTURE_B,))
    assert controller.registry.captures == (CAPTURE_B,)
    assert api._capture_points == (CAPTURE_B,)
    assert api._capture_ticket != previous_ticket

    admission.rollback()

    assert controller.state is runtime.OperationState.CAPTURED
    assert controller.registry.captures == (CAPTURE_A,)
    assert controller._breakpoint_workspace == (SERVICE, CAPTURE_A)
    assert api._capture_points == (CAPTURE_A,)
    assert api._capture_ticket == previous_ticket


def test_runtime_api_quarantines_if_admission_rollback_workspace_is_uncertain() -> None:
    runtime = runtime_module()
    session = ScriptedSession((CAPTURE_A,))
    controller = runtime.PrototypeRuntimeController(session, SERVICE)
    api = PrototypeRuntimeApi(controller, capture_points=(CAPTURE_A,))
    controller.execute_main("Результат = 1;", capture_points=(CAPTURE_A,))
    api.prepare_capture_ticket()
    attempt = runtime.ContinuationAttemptSpec(
        "attempt-rollback-uncertain", 2, "continue-request", ()
    )
    admission = api.begin_continuation_admission(attempt, (CAPTURE_B,))
    session.fail_workspace_on_call = session.workspace_call_count + 1

    with pytest.raises(ProtocolError, match="rollback is uncertain"):
        admission.rollback()

    assert controller.state is runtime.OperationState.RECOVERING
    assert api._capture_ticket is None
    assert api._capture_inspection_quarantined is True


def test_runtime_api_quarantines_controller_journal_fsync_uncertainty() -> None:
    runtime = runtime_module()
    fail = False

    def sink(_name: str, _value: object) -> None:
        if fail:
            raise OSError("fsync failed")

    session = ScriptedSession((CAPTURE_A,))
    controller = runtime.PrototypeRuntimeController(
        session, SERVICE, journal=RecoveryJournal(sink)
    )
    api = PrototypeRuntimeApi(controller, capture_points=(CAPTURE_A,))
    controller.execute_main("Результат = 1;", capture_points=(CAPTURE_A,))
    api.prepare_capture_ticket()
    fail = True
    attempt = runtime.ContinuationAttemptSpec(
        "attempt-fsync-uncertain", 2, "continue-request", ()
    )

    with pytest.raises(OSError, match="fsync failed"):
        api.begin_continuation_admission(attempt, (CAPTURE_B,))

    assert controller.state is runtime.OperationState.RECOVERING
    assert api._capture_ticket is None
    assert api._capture_inspection_quarantined is True


@pytest.mark.parametrize("rollback_fails", (False, True))
def test_runtime_api_ticket_publication_failure_restores_or_quarantines_exactly(
    monkeypatch: pytest.MonkeyPatch,
    rollback_fails: bool,
) -> None:
    runtime = runtime_module()
    session = ScriptedSession((CAPTURE_A,))
    controller = runtime.PrototypeRuntimeController(session, SERVICE)
    api = PrototypeRuntimeApi(controller, capture_points=(CAPTURE_A,))
    controller.execute_main("Результат = 1;", capture_points=(CAPTURE_A,))
    previous_ticket = api.prepare_capture_ticket()

    def fail_ticket_publication() -> CaptureCorrelationTicket:
        api._capture_ticket = CaptureCorrelationTicket("candidate", 99, 99)
        if rollback_fails:
            session.fail_workspace_on_call = session.workspace_call_count + 1
        raise OSError("planned ticket publication failure")

    monkeypatch.setattr(
        api, "_prepare_capture_ticket_locked", fail_ticket_publication
    )
    attempt = runtime.ContinuationAttemptSpec(
        f"attempt-ticket-{rollback_fails}", 1, "continue-request", ()
    )

    with pytest.raises((OSError, ProtocolError)):
        api.begin_continuation_admission(attempt, (CAPTURE_B,))

    if rollback_fails:
        assert controller.state is runtime.OperationState.RECOVERING
        assert api._capture_ticket is None
        assert api._capture_inspection_quarantined is True
    else:
        assert controller.state is runtime.OperationState.CAPTURED
        assert controller.registry.captures == (CAPTURE_A,)
        assert api._capture_points == (CAPTURE_A,)
        assert api._capture_ticket == previous_ticket
        assert api._capture_inspection_quarantined is False


def test_offline_full_stack_successor_attempt_has_exact_durable_order(
    tmp_path: Path,
) -> None:
    """CaptureService -> backend -> session -> API -> controller, no live 1C."""
    rows: list[tuple[str, object]] = []
    journal = RecoveryJournal(lambda name, value: rows.append((name, value)))
    rdbg = ScriptedSession((CAPTURE_A, CAPTURE_B))
    controller = runtime_module().PrototypeRuntimeController(
        rdbg, SERVICE, journal=journal
    )
    controller.execute_main(
        "Результат = СинтетическийCapture(40);", capture_points=(CAPTURE_A,)
    )
    api = PrototypeRuntimeApi(controller, capture_points=(CAPTURE_A,))
    demo = object.__new__(RuntimeSession)
    demo._operation_lock = RLock()
    demo._closed = False
    demo.runtime_api = api
    demo._capture_resume_listeners = []
    before = ResolvedCapturePoint(
        "before", "zup", "Payroll", "Run", 17, 7, "a" * 64, 17, "До();"
    )
    after = ResolvedCapturePoint(
        "after", "zup", "Payroll", "Run", 23, 7, "a" * 64, 23, "После();"
    )
    fence = CaptureFence("capture-intent", "op-capture", 7, "a" * 64, 1, 1)
    demo._capture_locations = {
        (before.name.casefold(), before.line): CAPTURE_A,
        (after.name.casefold(), after.line): CAPTURE_B,
    }
    demo.resolve_capture_points = lambda points: (after,)  # type: ignore[method-assign]
    demo._active_capture_points = (before,)
    demo._active_capture_locations = (CAPTURE_A,)
    demo._active_capture_ticket = _ActiveCaptureTicket(
        "old-ticket",
        fence.capture_intent_id,
        fence.operation_id,
        fence.capture_generation,
        fence.source_revision,
        fence.source_sha256,
        fence.stop_sequence,
    )
    backend = OnecRuntimeBackend(
        "runtime-1", AgentRuntimeSession(demo), mode=CapabilityMode.EXPERIMENT
    )
    capture = CaptureService(tmp_path)
    capture.activate_capture_view(
        CaptureView(fence, before, None, dirty_roots=("Скаляр",))
    )

    result = capture.continue_capture(
        backend,
        ProxyRegistry(),
        fence=fence,
        operation_id="op-continue",
        next_points=(
            CapturePointRequest("after", "zup", "Payroll", "Run", 23),
        ),
    )

    assert result.execution.terminal_state is AgentOperationState.CAPTURED
    assert result.capture is not None
    assert result.capture.fence.capture_generation == 2
    assert rdbg.continue_count == 2  # initial MAIN start plus one continuation
    events = [
        value
        for name, value in rows
        if name == "write-journal.jsonl" and isinstance(value, dict)
    ]
    names = [event["event"] for event in events]
    planned = names.index("root_write_planned")
    sent = names.index("root_write_sent")
    succeeded = names.index("root_write_succeeded")
    continue_sent = names.index("continue_sent")
    assert planned < sent < succeeded < continue_sent
    attempt_id = next(
        event["attempt_id"]
        for event in events
        if event["event"] == "continuation_attempt_started"
    )
    assert isinstance(attempt_id, str)
    evidence = controller.continuation_attempt_evidence(attempt_id)
    assert evidence.root_statuses == (("Скаляр", "succeeded"),)
    assert evidence.continue_state == "acknowledged"


def _offline_continuation_stack(
    tmp_path: Path,
    rdbg: ScriptedSession,
    *,
    dirty_roots: tuple[str, ...],
    user_breakpoints: tuple[ModuleLocation, ...] = (),
) -> tuple[
    object,
    PrototypeRuntimeApi,
    RuntimeSession,
    OnecRuntimeBackend,
    CaptureService,
    CaptureFence,
    ResolvedCapturePoint,
    ResolvedCapturePoint,
]:
    controller = runtime_module().PrototypeRuntimeController(rdbg, SERVICE)
    controller.execute_main(
        "Результат = СинтетическийCapture(40);",
        capture_points=(CAPTURE_A,),
        user_breakpoints=user_breakpoints,
    )
    api = PrototypeRuntimeApi(controller, capture_points=(CAPTURE_A,))
    demo = object.__new__(RuntimeSession)
    demo._operation_lock = RLock()
    demo._closed = False
    demo.runtime_api = api
    demo._capture_resume_listeners = []
    before = ResolvedCapturePoint(
        "before", "zup", "Payroll", "Run", 17, 7, "a" * 64, 17, "До();"
    )
    after = ResolvedCapturePoint(
        "after", "zup", "Payroll", "Run", 23, 7, "a" * 64, 23, "После();"
    )
    fence = CaptureFence("capture-intent", "op-capture", 7, "a" * 64, 1, 1)
    demo._capture_locations = {
        (before.name.casefold(), before.line): CAPTURE_A,
        (after.name.casefold(), after.line): CAPTURE_B,
    }
    demo.resolve_capture_points = lambda points: (after,)  # type: ignore[method-assign]
    demo._active_capture_points = (before,)
    demo._active_capture_locations = (CAPTURE_A,)
    demo._active_capture_ticket = _ActiveCaptureTicket(
        "old-ticket",
        fence.capture_intent_id,
        fence.operation_id,
        fence.capture_generation,
        fence.source_revision,
        fence.source_sha256,
        fence.stop_sequence,
    )
    backend = OnecRuntimeBackend(
        "runtime-1", AgentRuntimeSession(demo), mode=CapabilityMode.EXPERIMENT
    )
    capture = CaptureService(tmp_path)
    capture.activate_capture_view(
        CaptureView(fence, before, None, dirty_roots=dirty_roots)
    )
    return controller, api, demo, backend, capture, fence, before, after


def test_offline_full_stack_partial_write_quarantines_with_exact_attempt_only(
    tmp_path: Path,
) -> None:
    rdbg = ScriptedSession((CAPTURE_A,), failed_roots=("Второй",))
    controller, api, demo, backend, capture, fence, _before, _after = (
        _offline_continuation_stack(
            tmp_path,
            rdbg,
            dirty_roots=("Первый", "Второй", "Третий"),
        )
    )

    result = capture.continue_capture(
        backend,
        ProxyRegistry(),
        fence=fence,
        operation_id="op-continue",
        next_points=(CapturePointRequest("after", "zup", "Payroll", "Run", 23),),
    )

    assert result.execution.terminal_state is AgentOperationState.UNKNOWN
    assert result.quarantine_runtime is True
    assert result.failure == {
        "stage": "capture_transport",
        "partial_results": {
            "Первый": "succeeded",
            "Второй": "failed",
            "Третий": "unattempted",
        },
        "continue_state": "unattempted",
    }
    assert controller.state is runtime_module().OperationState.RECOVERING
    assert api._capture_inspection_quarantined is True
    assert demo._active_capture_ticket is None
    with pytest.raises(StaleProxy):
        capture.current_capture(fence)
    assert rdbg.continue_count == 1


def test_offline_full_stack_resolver_failure_restores_every_paused_layer(
    tmp_path: Path,
) -> None:
    class ResolverFailureSession(ScriptedSession):
        def evaluate(self, expression: str, **kwargs: object) -> EvaluationResult:
            if "ПоместитьЗначениеКонтекстаОтладки" in expression:
                return evaluation("Ошибка", "", error="resolver failed")
            return super().evaluate(expression, **kwargs)  # type: ignore[arg-type]

    rdbg = ResolverFailureSession((CAPTURE_A,))
    controller, api, demo, backend, capture, fence, _before, _after = (
        _offline_continuation_stack(
            tmp_path, rdbg, dirty_roots=("Первый", "Второй")
        )
    )

    result = capture.continue_capture(
        backend,
        ProxyRegistry(),
        fence=fence,
        operation_id="op-continue",
        next_points=(CapturePointRequest("after", "zup", "Payroll", "Run", 23),),
    )

    assert result.execution.terminal_state is AgentOperationState.FAILED
    assert result.failure == {
        "stage": "capture_writeback",
        "partial_results": {"Первый": "failed", "Второй": "unattempted"},
        "continue_state": "unattempted",
    }
    assert controller.state is runtime_module().OperationState.CAPTURED
    assert controller.registry.captures == (CAPTURE_A,)
    assert api._capture_points == (CAPTURE_A,)
    assert demo._active_capture_ticket is not None
    assert capture.current_capture(fence).fence == fence
    assert rdbg.continue_count == 1


def test_offline_full_stack_deterministic_arm_failure_restores_every_layer(
    tmp_path: Path,
) -> None:
    rdbg = ScriptedSession((CAPTURE_A,))
    controller, api, demo, backend, capture, fence, _before, _after = (
        _offline_continuation_stack(tmp_path, rdbg, dirty_roots=())
    )
    rdbg.fail_workspace_on_call = rdbg.workspace_call_count + 1

    result = capture.continue_capture(
        backend,
        ProxyRegistry(),
        fence=fence,
        operation_id="op-continue",
        next_points=(CapturePointRequest("after", "zup", "Payroll", "Run", 23),),
    )

    assert result.execution.terminal_state is AgentOperationState.UNKNOWN
    assert result.quarantine_runtime is True
    assert controller.state is runtime_module().OperationState.RECOVERING
    assert controller.registry.captures == (CAPTURE_A,)
    assert api._capture_points == (CAPTURE_A,)
    assert api._capture_ticket is None
    assert demo._active_capture_ticket is None
    with pytest.raises(ProtocolError):
        capture.current_capture(fence)
    assert rdbg.continue_count == 1


def test_offline_full_stack_session_ticket_publication_failure_restores_all(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rdbg = ScriptedSession((CAPTURE_A,))
    controller, api, demo, backend, capture, fence, _before, _after = (
        _offline_continuation_stack(tmp_path, rdbg, dirty_roots=())
    )
    previous_ticket = demo._active_capture_ticket

    def fail_ticket(*_args: object, **_kwargs: object) -> object:
        raise ValueError("planned session ticket publication failure")

    monkeypatch.setattr("onec_runtime.session._ActiveCaptureTicket", fail_ticket)

    result = capture.continue_capture(
        backend,
        ProxyRegistry(),
        fence=fence,
        operation_id="op-continue",
        next_points=(CapturePointRequest("after", "zup", "Payroll", "Run", 23),),
    )

    assert result.execution.terminal_state is AgentOperationState.FAILED
    assert result.quarantine_runtime is False
    assert controller.state is runtime_module().OperationState.CAPTURED
    assert controller.registry.captures == (CAPTURE_A,)
    assert api._capture_points == (CAPTURE_A,)
    assert demo._active_capture_ticket == previous_ticket
    assert capture.current_capture(fence).fence == fence
    assert rdbg.continue_count == 1


def test_offline_full_stack_arm_restore_uncertainty_quarantines_every_layer(
    tmp_path: Path,
) -> None:
    class RestoreLossSession(ScriptedSession):
        failed_workspace_calls: set[int]

        def set_breakpoints(self, locations: tuple[ModuleLocation, ...]) -> None:
            super().set_breakpoints(locations)
            if self.workspace_call_count in self.failed_workspace_calls:
                raise ProtocolError("planned workspace restore failure")

    rdbg = RestoreLossSession((CAPTURE_A,))
    rdbg.failed_workspace_calls = set()
    controller, api, demo, backend, capture, fence, _before, _after = (
        _offline_continuation_stack(tmp_path, rdbg, dirty_roots=())
    )
    rdbg.failed_workspace_calls = {
        rdbg.workspace_call_count + 1,
        rdbg.workspace_call_count + 2,
    }

    result = capture.continue_capture(
        backend,
        ProxyRegistry(),
        fence=fence,
        operation_id="op-continue",
        next_points=(CapturePointRequest("after", "zup", "Payroll", "Run", 23),),
    )

    assert result.execution.terminal_state is AgentOperationState.UNKNOWN
    assert result.quarantine_runtime is True
    assert controller.state is runtime_module().OperationState.RECOVERING
    assert api._capture_inspection_quarantined is True
    assert api._capture_ticket is None
    assert demo._active_capture_ticket is None
    with pytest.raises(StaleProxy):
        capture.current_capture(fence)
    assert rdbg.continue_count == 1


def test_offline_full_stack_invalid_arm_evidence_rolls_back_every_layer(
    tmp_path: Path,
) -> None:
    rdbg = ScriptedSession((CAPTURE_A,))
    controller, api, demo, backend, capture, fence, _before, _after = (
        _offline_continuation_stack(tmp_path, rdbg, dirty_roots=())
    )
    prepare = backend.prepare_capture_successor

    def invalid_prepare(intent, *, attempt):  # type: ignore[no-untyped-def]
        admission = prepare(intent, attempt=attempt)

        class InvalidEvidenceAdmission:
            arming = CaptureArming("", 0, 0)

            def commit(self) -> None:
                admission.commit()

            def rollback(self) -> None:
                admission.rollback()

            def quarantine(self) -> None:
                admission.quarantine()

        return InvalidEvidenceAdmission()

    backend.prepare_capture_successor = invalid_prepare  # type: ignore[method-assign]

    result = capture.continue_capture(
        backend,
        ProxyRegistry(),
        fence=fence,
        operation_id="op-continue",
        next_points=(CapturePointRequest("after", "zup", "Payroll", "Run", 23),),
    )

    assert result.execution.terminal_state is AgentOperationState.FAILED
    assert result.quarantine_runtime is False
    assert controller.state is runtime_module().OperationState.CAPTURED
    assert controller.registry.captures == (CAPTURE_A,)
    assert api._capture_points == (CAPTURE_A,)
    assert demo._active_capture_ticket is not None
    assert capture.current_capture(fence).fence == fence
    assert rdbg.continue_count == 1


def test_offline_full_stack_service_arming_fsync_uncertainty_quarantines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rdbg = ScriptedSession((CAPTURE_A,))
    controller, api, demo, backend, capture, fence, _before, _after = (
        _offline_continuation_stack(tmp_path, rdbg, dirty_roots=())
    )
    monkeypatch.setattr(
        capture,
        "_journal_arming",
        lambda *_args: (_ for _ in ()).throw(OSError("planned fsync loss")),
    )

    result = capture.continue_capture(
        backend,
        ProxyRegistry(),
        fence=fence,
        operation_id="op-continue",
        next_points=(CapturePointRequest("after", "zup", "Payroll", "Run", 23),),
    )

    assert result.execution.terminal_state is AgentOperationState.UNKNOWN
    assert result.quarantine_runtime is True
    assert controller.state is runtime_module().OperationState.RECOVERING
    assert api._capture_inspection_quarantined is True
    assert api._capture_ticket is None
    assert demo._active_capture_ticket is None
    with pytest.raises(StaleProxy):
        capture.current_capture(fence)
    assert rdbg.continue_count == 1


def test_offline_full_stack_root_transport_loss_preserves_exact_ordered_evidence(
    tmp_path: Path,
) -> None:
    class RootTransportLossSession(ScriptedSession):
        def modify(self, variable: str, value_expression: str) -> ModifyResult:
            if variable == "Второй":
                raise RdbgTransportError("planned root transport loss")
            return super().modify(variable, value_expression)

    rdbg = RootTransportLossSession((CAPTURE_A,))
    controller, _api, _demo, backend, capture, fence, _before, _after = (
        _offline_continuation_stack(
            tmp_path,
            rdbg,
            dirty_roots=("Первый", "Второй", "Третий"),
        )
    )

    result = capture.continue_capture(
        backend,
        ProxyRegistry(),
        fence=fence,
        operation_id="op-continue",
        next_points=(CapturePointRequest("after", "zup", "Payroll", "Run", 23),),
    )

    assert result.execution.terminal_state is AgentOperationState.UNKNOWN
    assert result.failure == {
        "stage": "capture_transport",
        "partial_results": {
            "Первый": "succeeded",
            "Второй": "outcome_unknown",
            "Третий": "unattempted",
        },
        "continue_state": "unattempted",
    }
    assert result.quarantine_runtime is True
    assert controller.state is runtime_module().OperationState.RECOVERING
    assert rdbg.continue_count == 1


def test_offline_full_stack_later_resolver_transport_loss_keeps_prior_success(
    tmp_path: Path,
) -> None:
    class ResolverTransportLossSession(ScriptedSession):
        resolver_calls = 0

        def evaluate(self, expression: str, **kwargs: object) -> EvaluationResult:
            if "ПоместитьЗначениеКонтекстаОтладки" in expression:
                self.resolver_calls += 1
                if self.resolver_calls == 2:
                    raise RdbgTransportError("planned resolver transport loss")
            return super().evaluate(expression, **kwargs)  # type: ignore[arg-type]

    rdbg = ResolverTransportLossSession((CAPTURE_A,))
    controller, _api, _demo, backend, capture, fence, _before, _after = (
        _offline_continuation_stack(
            tmp_path,
            rdbg,
            dirty_roots=("Первый", "Второй", "Третий"),
        )
    )

    result = capture.continue_capture(
        backend,
        ProxyRegistry(),
        fence=fence,
        operation_id="op-continue",
        next_points=(CapturePointRequest("after", "zup", "Payroll", "Run", 23),),
    )

    assert result.execution.terminal_state is AgentOperationState.UNKNOWN
    assert result.failure == {
        "stage": "capture_transport",
        "partial_results": {
            "Первый": "succeeded",
            "Второй": "failed",
            "Третий": "unattempted",
        },
        "continue_state": "unattempted",
    }
    assert result.quarantine_runtime is True
    assert controller.state is runtime_module().OperationState.RECOVERING
    assert rdbg.continue_count == 1


def test_offline_full_stack_continue_transport_loss_sends_once_and_is_unknown(
    tmp_path: Path,
) -> None:
    class ContinueTransportLossSession(ScriptedSession):
        def continue_(self) -> None:
            super().continue_()
            if self.continue_count == 2:
                raise RdbgTransportError("planned Continue transport loss")

    rdbg = ContinueTransportLossSession((CAPTURE_A,))
    controller, _api, _demo, backend, capture, fence, _before, _after = (
        _offline_continuation_stack(tmp_path, rdbg, dirty_roots=())
    )

    result = capture.continue_capture(
        backend,
        ProxyRegistry(),
        fence=fence,
        operation_id="op-continue",
        next_points=(CapturePointRequest("after", "zup", "Payroll", "Run", 23),),
    )

    assert result.execution.terminal_state is AgentOperationState.UNKNOWN
    assert result.failure is not None
    assert result.failure["partial_results"] == {}
    assert result.failure["continue_state"] == "outcome_unknown"
    assert result.quarantine_runtime is True
    assert controller.state is runtime_module().OperationState.RECOVERING
    assert rdbg.continue_count == 2


def test_offline_workspace_restart_replays_frontend_disconnect_after_send(
    tmp_path: Path,
) -> None:
    rdbg = ScriptedSession((CAPTURE_A, CAPTURE_B))
    _controller, _api, demo, backend, _capture, fence, before, _after = (
        _offline_continuation_stack(tmp_path, rdbg, dirty_roots=())
    )
    demo.close = lambda: setattr(demo, "_closed", True)  # type: ignore[method-assign]
    continued = backend.continue_capture

    def disconnect_after_send(*, dirty_roots, attempt_id):  # type: ignore[no-untyped-def]
        continued(dirty_roots=dirty_roots, attempt_id=attempt_id)
        raise OSError("planned frontend disconnect after Continue")

    backend.continue_capture = disconnect_after_send  # type: ignore[method-assign]

    class NeverStart:
        def start(self, *, mode):  # type: ignore[no-untyped-def]
            raise AssertionError("durable continuation replay must not start a runtime")

    request = {
        "fence": to_wire(fence),
        "next_points": [
            {
                "name": "after",
                "project": "zup",
                "module": "Payroll",
                "procedure": "Run",
                "line": 23,
            }
        ],
        "request_id": "full-stack-disconnect-request",
        "wait_s": 2.0,
    }
    first_service = AgentWorkspaceService(
        tmp_path, NeverStart(), maximum_mode=CapabilityMode.EXPERIMENT
    )
    first_service._runtime = _AdmittedRuntime(  # type: ignore[assignment]
        backend, "runtime-1", 1, CapabilityMode.EXPERIMENT
    )
    first_service._selected["default"] = "runtime-1"
    first_service._capture.activate_capture_view(CaptureView(fence, before, None))
    first = first_service.call("capture.continue", request)
    first_service.close()

    restarted = AgentWorkspaceService(
        tmp_path, NeverStart(), maximum_mode=CapabilityMode.EXPERIMENT
    )
    try:
        replay = restarted.call("capture.continue", request)

        assert first.ok and replay.ok
        assert first.value.state is AgentOperationState.UNKNOWN
        assert replay.value.state is AgentOperationState.UNKNOWN
        assert replay.value.operation.operation_id == first.value.operation.operation_id
        assert rdbg.continue_count == 2
    finally:
        restarted.close()


def test_offline_full_stack_continue_wait_loss_sends_once_then_quarantines(
    tmp_path: Path,
) -> None:
    from onec_runtime.errors import CommandTimeout

    class WaitLossSession(ScriptedSession):
        def wait_for_any_stop(self, *, timeout_s: float) -> StopEvent:
            if self.continue_count >= 2:
                raise CommandTimeout("stop wait timed out")
            return super().wait_for_any_stop(timeout_s=timeout_s)

    rdbg = WaitLossSession((CAPTURE_A,))
    controller, _api, _demo, backend, capture, fence, _before, _after = (
        _offline_continuation_stack(tmp_path, rdbg, dirty_roots=())
    )

    result = capture.continue_capture(
        backend,
        ProxyRegistry(),
        fence=fence,
        operation_id="op-continue",
        next_points=(CapturePointRequest("after", "zup", "Payroll", "Run", 23),),
    )

    assert result.execution.terminal_state is AgentOperationState.UNKNOWN
    assert result.failure is not None
    assert result.failure["continue_state"] == "acknowledged"
    assert result.quarantine_runtime is True
    assert controller.state is runtime_module().OperationState.RECOVERING
    assert rdbg.continue_count == 2


def test_offline_full_stack_empty_successor_without_roots_is_terminal(
    tmp_path: Path,
) -> None:
    rdbg = ScriptedSession((CAPTURE_A, SERVICE))
    controller, _api, demo, backend, capture, fence, _before, _after = (
        _offline_continuation_stack(tmp_path, rdbg, dirty_roots=())
    )

    result = capture.continue_capture(
        backend,
        ProxyRegistry(),
        fence=fence,
        operation_id="op-continue",
        next_points=(),
    )

    assert result.execution.terminal_state is AgentOperationState.COMPLETED
    assert result.capture is None
    assert controller.state is runtime_module().OperationState.COMPLETED
    assert controller.registry.captures == ()
    assert demo._active_capture_ticket is None
    assert rdbg.continue_count == 2


@pytest.mark.parametrize(
    ("later_stop", "users"),
    ((CAPTURE_A, ()), (UNKNOWN, ()), (USER, (USER,))),
)
def test_offline_full_stack_old_wrong_or_user_stop_never_rebinds_old_capture(
    tmp_path: Path,
    later_stop: ModuleLocation,
    users: tuple[ModuleLocation, ...],
) -> None:
    rdbg = ScriptedSession((CAPTURE_A, later_stop))
    controller, _api, _demo, backend, capture, fence, _before, _after = (
        _offline_continuation_stack(
            tmp_path, rdbg, dirty_roots=(), user_breakpoints=users
        )
    )

    result = capture.continue_capture(
        backend,
        ProxyRegistry(),
        fence=fence,
        operation_id="op-continue",
        next_points=(CapturePointRequest("after", "zup", "Payroll", "Run", 23),),
    )

    assert result.execution.terminal_state is AgentOperationState.UNKNOWN
    assert result.capture is None
    assert result.quarantine_runtime is True
    assert controller.state is runtime_module().OperationState.RECOVERING
    assert rdbg.continue_count == 2
    with pytest.raises(StaleProxy):
        capture.current_capture(fence)


class _OfflineOwnedResource:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class _OfflineHeartbeatThread:
    def __init__(self) -> None:
        self.join_calls = 0

    def join(self, *, timeout: float) -> None:
        assert timeout == 2.0
        self.join_calls += 1


class _OfflineBackendFactory:
    def __init__(self, backend: OnecRuntimeBackend) -> None:
        self.backend = backend
        self.start_calls = 0

    def start(self, *, mode: CapabilityMode) -> OnecRuntimeBackend:
        assert mode is CapabilityMode.EXPERIMENT
        self.start_calls += 1
        if self.start_calls != 1:
            raise AssertionError("offline runtime backend must be owned exactly once")
        return self.backend


def _offline_mcp_stack(
    project: Path,
    rdbg: ScriptedSession,
) -> tuple[
    AgentWorkspaceService,
    OnecRuntimeBackend,
    RuntimeSession,
    object,
    _OfflineBackendFactory,
    _OfflineOwnedResource,
    _OfflineOwnedResource,
    _OfflineHeartbeatThread,
]:
    controller = runtime_module().PrototypeRuntimeController(
        rdbg, SERVICE, journal=RecoveryJournal(lambda _name, _value: None)
    )
    api = PrototypeRuntimeApi(controller)
    transport = _OfflineOwnedResource()
    processes = _OfflineOwnedResource()
    heartbeat = _OfflineHeartbeatThread()
    demo = object.__new__(RuntimeSession)
    demo.config = SimpleNamespace(chunk_size=2400, runtime=SimpleNamespace(is_server_infobase=False))
    demo.runtime_api = api
    demo.artifacts = ArtifactWriter(project / "artifacts", "offline-capture-mcp")
    demo._operation_lock = RLock()
    demo._close_lock = RLock()
    demo._closed = False
    demo._runtime_api_closed = False
    demo._transport_closed = False
    demo._processes_closed = False
    demo._server_session_terminated = True
    demo._debug_ui_detached = True
    demo._transport = transport
    demo._processes = processes
    demo._heartbeat_stop = Event()
    demo._heartbeat_thread = heartbeat
    demo._capture_locations = {}
    demo._capture_source_resolver = None
    demo._capture_source_bindings = {}
    demo._active_capture_points = ()
    demo._active_capture_locations = ()
    demo._active_capture_ticket = None
    demo._capture_resume_listeners = []
    demo.owned_process_snapshot = lambda: ()  # type: ignore[method-assign]
    demo.configure_capture_source("zup", project.parent)
    backend = OnecRuntimeBackend(
        "runtime-offline", AgentRuntimeSession(demo), mode=CapabilityMode.EXPERIMENT
    )
    factory = _OfflineBackendFactory(backend)
    service = AgentWorkspaceService(
        project, factory, maximum_mode=CapabilityMode.EXPERIMENT
    )
    return (
        service,
        backend,
        demo,
        controller,
        factory,
        transport,
        processes,
        heartbeat,
    )


def _offline_capture_notebook(project: Path) -> tuple[str, str]:
    main_source = "Результат = 1;"
    capture_source = "КонтекстОтладки.Скаляр = 41;"
    cells = []
    for cell_id, mode, source in (
        ("main-cell", "main", main_source),
        ("capture-cell", "capture", capture_source),
    ):
        cell = nbformat.v4.new_code_cell(source=source, id=cell_id)
        cell.metadata["onec_runtime"] = {
            "revision": 1,
            "language": "bsl",
            "mode": mode,
            "source_sha256": sha256(source.encode("utf-8")).hexdigest(),
        }
        cells.append(cell)
    nbformat.write(
        nbformat.v4.new_notebook(cells=cells), project / "capture.ipynb"
    )
    return (
        sha256(main_source.encode("utf-8")).hexdigest(),
        sha256(capture_source.encode("utf-8")).hexdigest(),
    )


def _mcp_value(result: object) -> object:
    payload = getattr(result, "structured_content", None)
    assert isinstance(payload, dict), "official MCP result has no structured envelope"
    assert payload.get("ok") is True, payload
    return payload.get("value")


async def _mcp_terminal(client: Client, view: dict[str, object]) -> dict[str, object]:
    current = view
    while current.get("state") in {"queued", "running"}:
        operation = current.get("operation")
        assert isinstance(operation, dict)
        operation_id = operation.get("operation_id")
        assert isinstance(operation_id, str)
        waited = await client.call_tool(
            "operation.wait", {"operation_id": operation_id, "timeout_s": 2.0}
        )
        value = _mcp_value(waited)
        assert isinstance(value, dict)
        current = value
    return current


def _assert_private_free_mcp(value: object) -> None:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True).casefold()
    for marker in (
        "attempt_id",
        "ticket_id",
        "private-rdbg-evidence",
        "rdbgtransporterror",
    ):
        assert marker not in encoded


def test_offline_official_mcp_capture_continuation_lifecycle_and_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Official SDK -> facade -> service -> real production continuation stack."""
    module_source = "\n".join(
        (
            "Процедура СоздатьВТЗарплатаКВыплате() Экспорт",
            *("Строка = 1;" for _ in range(18)),
            "КонецПроцедуры",
            "Функция ЗарплатаКВыплате() Экспорт",
            "Строка = 1;",
            "Результат = 1;",
            "КонецФункции",
        )
    )
    module_path = tmp_path / "CommonModules" / "Payroll" / "Module.bsl"
    module_path.parent.mkdir(parents=True)
    module_path.write_text(module_source, encoding="utf-8")
    (module_path.parent / "Payroll.mdo").write_text(
        '<mdclass:CommonModule xmlns:mdclass="urn:test" '
        f'uuid="{OBJECT}"/>',
        encoding="utf-8",
    )
    before_location = ModuleLocation(
        "ConfigModule", "", OBJECT, PROPERTY, 17
    )
    after_location = ModuleLocation(
        "ConfigModule", "", OBJECT, PROPERTY, 23
    )
    before_point = {
        "name": "before",
        "project": "zup",
        "module": "Payroll",
        "procedure": "СоздатьВТЗарплатаКВыплате",
        "line": 17,
    }
    after_point = {
        "name": "after",
        "project": "zup",
        "module": "Payroll",
        "procedure": "ЗарплатаКВыплате",
        "line": 23,
    }

    async def start_capture(
        client: Client,
        main_hash: str,
        *,
        request_suffix: str,
    ) -> dict[str, object]:
        ensured = _mcp_value(
            await client.call_tool("runtime.ensure", {"mode": "experiment"})
        )
        assert isinstance(ensured, dict)
        if "operation" in ensured:
            ensured = await _mcp_terminal(client, ensured)
            assert ensured["state"] == "completed"
        listed = _mcp_value(
            await client.call_tool(
                "code.list", {"container": "capture.ipynb"}
            )
        )
        assert isinstance(listed, list) and len(listed) == 2
        captured = _mcp_value(
            await client.call_tool(
                "capture.run_until",
                {
                    "cell_id": "main-cell",
                    "revision": 1,
                    "source_sha256": main_hash,
                    "points": [before_point],
                    "request_id": f"run-{request_suffix}",
                    "wait_s": 2.0,
                },
            )
        )
        assert isinstance(captured, dict)
        captured = await _mcp_terminal(client, captured)
        assert captured["state"] == "captured"
        assert isinstance(captured.get("capture"), dict)
        return captured

    async def success_scenario(project: Path) -> None:
        project.mkdir()
        main_hash, capture_hash = _offline_capture_notebook(project)
        rdbg = ScriptedSession((before_location, after_location))
        (
            service,
            backend,
            demo,
            _controller,
            factory,
            transport,
            processes,
            heartbeat,
        ) = _offline_mcp_stack(project, rdbg)
        continuation_request: dict[str, object] | None = None
        continued: dict[str, object] | None = None
        try:
            async with Client(
                create_mcp_server(service, profile=McpProfile.CAPTURE)
            ) as first_client:
                captured = await start_capture(
                    first_client, main_hash, request_suffix="success"
                )
                capture = captured["capture"]
                assert isinstance(capture, dict)
                fence = capture["fence"]
                inspected = _mcp_value(
                    await first_client.call_tool(
                        "capture.inspect",
                        {"fence": fence, "filters": {"name": "Скаляр"}},
                    )
                )
                assert isinstance(inspected, dict)
                variables = inspected["variables"]
                assert isinstance(variables, list) and len(variables) == 1
                old_proxy_id = variables[0]["proxy_id"]

                hypothesis = _mcp_value(
                    await first_client.call_tool(
                        "capture.hypothesis",
                        {
                            "fence": fence,
                            "code_ref": {
                                "cell_id": "capture-cell",
                                "revision": 1,
                                "source_sha256": capture_hash,
                            },
                            "request_id": "hypothesis-success",
                            "wait_s": 2.0,
                        },
                    )
                )
                assert isinstance(hypothesis, dict)
                hypothesis = await _mcp_terminal(first_client, hypothesis)
                assert hypothesis["state"] == "captured"
                continuation_request = {
                    "fence": fence,
                    "request_id": "continue-success",
                    "next_points": [after_point],
                    "observe": {
                        "items": [
                            {
                                "alias": "after_scalar",
                                "source": {
                                    "kind": "frame_local",
                                    "name": "Скаляр",
                                },
                                "result": "proxy",
                            }
                        ],
                        "budget_profile": "agent_metadata",
                    },
                    "wait_s": 2.0,
                }
                raw_continued = _mcp_value(
                    await first_client.call_tool(
                        "capture.continue", continuation_request
                    )
                )
                assert isinstance(raw_continued, dict)
                continued = await _mcp_terminal(first_client, raw_continued)
                assert continued["state"] == "captured"
                next_capture = continued["capture"]
                assert isinstance(next_capture, dict)
                assert next_capture["fence"]["capture_generation"] == 2
                output = continued["outputs"]["after_scalar"]
                assert output["fence"]["capture_fence"]["capture_generation"] == 2
                observed = _mcp_value(
                    await first_client.call_tool(
                        "value.inspect", {"proxy_id": output["proxy_id"]}
                    )
                )
                assert isinstance(observed, dict)
                stale = await first_client.call_tool(
                    "value.inspect", {"proxy_id": old_proxy_id}
                )
                stale_payload = stale.structured_content
                assert stale_payload["ok"] is False
                assert stale_payload["failure"]["category"] == "stale"
                _assert_private_free_mcp(continued)

            assert continuation_request is not None and continued is not None
            async with Client(
                create_mcp_server(service, profile=McpProfile.CAPTURE)
            ) as replacement_client:
                replay = _mcp_value(
                    await replacement_client.call_tool(
                        "capture.continue", continuation_request
                    )
                )
                assert isinstance(replay, dict)
                assert replay["operation"]["operation_id"] == continued["operation"]["operation_id"]
            scalar_writes = [
                value
                for name, value in rdbg.calls
                if name == "modify" and value[0] == "Скаляр"
            ]
            assert len(scalar_writes) == 1
            assert rdbg.continue_count == 2
            assert factory.start_calls == 1
        finally:
            service.close()
        assert backend.is_closed is True
        assert demo._closed is True
        assert transport.close_calls == processes.close_calls == heartbeat.join_calls == 1

    async def unknown_scenario(project: Path) -> None:
        project.mkdir()
        main_hash, _capture_hash = _offline_capture_notebook(project)

        class ContinueEvidenceLossSession(ScriptedSession):
            controller_under_test: object | None = None

            def continue_(self) -> None:
                super().continue_()
                if self.continue_count == 2:
                    controller = self.controller_under_test
                    assert controller is not None
                    controller._continuation_attempts.clear()  # type: ignore[attr-defined]
                    raise RdbgTransportError("private-rdbg-evidence")

        rdbg = ContinueEvidenceLossSession((before_location,))
        (
            service,
            backend,
            demo,
            controller,
            factory,
            transport,
            processes,
            heartbeat,
        ) = _offline_mcp_stack(project, rdbg)
        rdbg.controller_under_test = controller
        request: dict[str, object] | None = None
        unknown: dict[str, object] | None = None
        try:
            async with Client(
                create_mcp_server(service, profile=McpProfile.CAPTURE)
            ) as first_client:
                captured = await start_capture(
                    first_client, main_hash, request_suffix="unknown"
                )
                capture = captured["capture"]
                assert isinstance(capture, dict)
                request = {
                    "fence": capture["fence"],
                    "request_id": "continue-unknown",
                    "next_points": [after_point],
                    "wait_s": 2.0,
                }
                raw_unknown = _mcp_value(
                    await first_client.call_tool("capture.continue", request)
                )
                assert isinstance(raw_unknown, dict)
                unknown = await _mcp_terminal(first_client, raw_unknown)
                assert unknown["state"] == "unknown"
                assert unknown["failure"]["continue_state"] == "outcome_unknown"
                assert {item["method"] for item in unknown["recovery"]} >= {
                    "operation.wait",
                    "runtime.close",
                    "runtime.restart",
                }
                _assert_private_free_mcp(unknown)
                assert controller.state is runtime_module().OperationState.RECOVERING

            assert request is not None and unknown is not None
            async with Client(
                create_mcp_server(service, profile=McpProfile.CAPTURE)
            ) as replacement_client:
                replay = _mcp_value(
                    await replacement_client.call_tool("capture.continue", request)
                )
                assert isinstance(replay, dict)
                assert replay["operation"]["operation_id"] == unknown["operation"]["operation_id"]
                assert replay["failure"]["continue_state"] == "outcome_unknown"
                closed = _mcp_value(
                    await replacement_client.call_tool(
                        "runtime.close", {"policy": "abort_generation"}
                    )
                )
                assert closed == {"closed": True, "policy": "abort_generation"}
                status = _mcp_value(
                    await replacement_client.call_tool("workspace.status", {})
                )
                assert isinstance(status, dict)
                assert status["current_runtime_id"] is None
                assert "runtime" not in status
            assert rdbg.continue_count == 2
            assert factory.start_calls == 1
        finally:
            service.close()
        assert backend.is_closed is True
        assert demo._closed is True
        assert transport.close_calls == processes.close_calls == heartbeat.join_calls == 1

    asyncio.run(success_scenario(tmp_path / "success"))
    asyncio.run(unknown_scenario(tmp_path / "unknown"))


def test_planned_main_error_does_not_poison_next_operation() -> None:
    runtime = runtime_module()
    session = ScriptedSession((SERVICE, SERVICE), completion_errors=("planned", ""))
    controller = runtime.PrototypeRuntimeController(session, SERVICE)

    failed = controller.execute_main('ВызватьИсключение "planned";')
    recovered = controller.execute_main("ПослеОшибки = 1;")

    assert isinstance(failed, runtime.MainCompletion)
    assert failed.succeeded is False
    assert failed.error == "planned"
    assert isinstance(recovered, runtime.MainCompletion)
    assert recovered.succeeded is True
    assert recovered.operation.operation_id == 2
    assert controller.state is runtime.OperationState.COMPLETED


def test_user_breakpoint_is_not_main_completion_and_keeps_operation() -> None:
    runtime = runtime_module()
    session = ScriptedSession((USER, SERVICE))
    controller = runtime.PrototypeRuntimeController(session, SERVICE)

    stopped = controller.execute_main(
        "РезультатИнструкции = RuntimeKernelServer.ВызовСОбычнойТочкой(910);",
        user_breakpoints=(USER,),
    )
    completed = controller.resume_debug_stop()

    assert isinstance(stopped, runtime.DebugStop)
    assert stopped.reason is StopReason.USER_BREAKPOINT
    assert stopped.operation.operation_id == completed.operation.operation_id == 1
    assert controller.state is runtime.OperationState.COMPLETED


def test_worker_workspace_breakpoint_routes_as_resumable_user_stop() -> None:
    runtime = runtime_module()
    session = ScriptedSession((USER, SERVICE))
    controller = runtime.PrototypeRuntimeController(session, SERVICE)
    initial = controller.breakpoint_workspace_owner.confirmed_snapshot
    worker_workspace = controller.breakpoint_workspace_owner.prepare(
        captures=initial.captures,
        ordinary_users=initial.ordinary_users,
        worker_slots=(USER,),
        shielded=initial.shielded,
    )
    controller.install_worker_workspace(worker_workspace)

    stopped = controller.execute_main("РезультатИнструкции = 910;")
    completed = controller.resume_debug_stop()

    assert isinstance(stopped, runtime.DebugStop)
    assert stopped.reason is StopReason.USER_BREAKPOINT
    assert stopped.operation.operation_id == completed.operation.operation_id == 1
    assert controller.state is runtime.OperationState.COMPLETED


def test_unknown_stop_remains_paused_and_cannot_resume_as_user_stop() -> None:
    runtime = runtime_module()
    session = ScriptedSession((UNKNOWN,))
    controller = runtime.PrototypeRuntimeController(session, SERVICE)

    stopped = controller.execute_main("РезультатИнструкции = 1;")

    assert isinstance(stopped, runtime.DebugStop)
    assert stopped.reason is StopReason.UNKNOWN
    assert session.continue_count == 1
    with pytest.raises(ProtocolError, match="user breakpoint"):
        controller.resume_debug_stop()


def test_capture_cell_masks_only_capture_points_then_restores_full_workspace() -> None:
    runtime = runtime_module()
    session = ScriptedSession(
        (CAPTURE_A,),
        capture_evaluations=(evaluation("Число", "901"),),
    )
    controller = captured_controller(session)

    cell = controller.execute_capture("РезультатИнструкции = 901;")

    assert cell.result == 901
    assert workspace_calls(session)[-2:] == [
        (SERVICE, USER),
        (SERVICE, CAPTURE_A, CAPTURE_B, USER),
    ]
    assert controller.state is runtime.OperationState.CAPTURED


def test_capture_evaluation_worker_stop_resumes_same_pending_evaluation() -> None:
    runtime = runtime_module()
    session = ScriptedSession(
        (CAPTURE_A,),
        capture_evaluations=(evaluation("Число", "901"),),
    )
    controller = captured_controller(session)
    confirmed = controller.breakpoint_workspace_owner.confirmed_snapshot
    worker_workspace = controller.breakpoint_workspace_owner.prepare(
        captures=confirmed.captures,
        ordinary_users=confirmed.ordinary_users,
        worker_slots=(WORKER_BREAKPOINT,),
        shielded=False,
    )
    controller.install_worker_workspace(worker_workspace)
    session.pending_evaluation_stops.append(
        StopEvent(
            TARGET,
            WORKER_BREAKPOINT,
            "callStackFormed",
            stop_by_breakpoint=True,
            stack=(WORKER_BREAKPOINT,),
            stack_frames=(StackFrame(TARGET, 0, WORKER_BREAKPOINT),),
        )
    )

    stopped = controller.execute_capture("РезультатИнструкции = 901;")

    assert isinstance(stopped, runtime.DebugStop)
    assert stopped.reason is StopReason.USER_BREAKPOINT
    assert controller.state is runtime.OperationState.CAPTURE_DEBUG_STOPPED
    assert controller.pending_capture_evaluation is not None
    shielded = controller.breakpoint_workspace_owner.confirmed_snapshot
    assert shielded.shielded is True
    assert CAPTURE_A not in shielded.effective_locations
    assert WORKER_BREAKPOINT in shielded.effective_locations

    cell = controller.resume_debug_stop()

    assert isinstance(cell, runtime.CaptureCellResult)
    assert cell.result == 901
    assert controller.state is runtime.OperationState.CAPTURED
    assert controller.pending_capture_evaluation is None
    restored = controller.breakpoint_workspace_owner.confirmed_snapshot
    assert restored.shielded is False
    assert CAPTURE_A in restored.effective_locations
    assert WORKER_BREAKPOINT in restored.effective_locations


def test_bsl_error_restores_workspace_and_next_capture_cell_succeeds() -> None:
    runtime = runtime_module()
    session = ScriptedSession(
        (CAPTURE_A,),
        capture_evaluations=(
            evaluation("Ошибка", "boom", error="boom"),
            evaluation("Число", "902"),
        ),
    )
    controller = captured_controller(session)

    with pytest.raises(runtime.BslExecutionError, match="boom"):
        controller.execute_capture('ВызватьИсключение "boom";')
    recovered = controller.execute_capture("РезультатИнструкции = 902;")

    assert recovered.result == 902
    assert controller.state is runtime.OperationState.CAPTURED
    assert workspace_calls(session)[-4:] == [
        (SERVICE, USER),
        (SERVICE, CAPTURE_A, CAPTURE_B, USER),
        (SERVICE, USER),
        (SERVICE, CAPTURE_A, CAPTURE_B, USER),
    ]


def test_failed_capture_cell_consumes_its_message_collector() -> None:
    runtime = runtime_module()
    session = ScriptedSession(
        (CAPTURE_A,),
        capture_evaluations=(evaluation("Ошибка", "boom", error="boom"),),
        messages=("before failure",),
        messages_in_result_slot=True,
    )
    controller = captured_controller(session)

    with pytest.raises(runtime.BslExecutionError, match="boom") as caught:
        controller.execute_capture('Сообщить("before failure"); ВызватьИсключение "boom";')

    assert caught.value.messages == ("before failure",)
    assert tuple(session.message_values) == ("before failure",)
    assert any(
        name == "evaluate"
        and isinstance(value, tuple)
        and "ЗабратьСообщенияЯчейкиИзКонтекста" in value[0]
        and value[1] == 2
        for name, value in session.calls
    )


def test_main_journal_hashes_lifecycle_without_source_text() -> None:
    rows: list[tuple[str, object]] = []
    journal = RecoveryJournal(lambda name, value: rows.append((name, value)))
    session = ScriptedSession((SERVICE,))
    controller = runtime_module().PrototypeRuntimeController(session, SERVICE, journal=journal)

    controller.execute_main('Секрет = "not-in-journal";')

    events = [
        value
        for name, value in rows
        if name == "write-journal.jsonl" and isinstance(value, dict)
    ]
    lifecycle = [
        event for event in events if event["event"].startswith("main_")
    ]
    assert [event["event"] for event in lifecycle] == ["main_started", "main_completed"]
    assert lifecycle[0]["runtime_generation"] == 1
    assert lifecycle[0]["operation_id"] == 1
    assert lifecycle[0]["state_before"] == "idle"
    assert len(lifecycle[0]["visible_sha256"]) == 64
    assert len(lifecycle[0]["lowered_sha256"]) == 64
    assert lifecycle[1]["visible_sha256"] == lifecycle[0]["visible_sha256"]
    assert lifecycle[1]["lowered_sha256"] == lifecycle[0]["lowered_sha256"]
    assert "source" not in lifecycle[0]
    assert "not-in-journal" not in str(lifecycle)


def test_main_journal_marks_failed_completion() -> None:
    rows: list[tuple[str, object]] = []
    journal = RecoveryJournal(lambda name, value: rows.append((name, value)))
    session = ScriptedSession((SERVICE,), completion_errors=("boom",))
    controller = runtime_module().PrototypeRuntimeController(session, SERVICE, journal=journal)

    completed = controller.execute_main("Значение = 1;")

    events = [
        value
        for name, value in rows
        if name == "write-journal.jsonl" and isinstance(value, dict)
    ]
    assert completed.succeeded is False
    assert [event["event"] for event in events[-2:]] == ["main_started", "main_failed"]
    assert events[-1]["visible_sha256"] == events[-2]["visible_sha256"]
    assert events[-1]["lowered_sha256"] == events[-2]["lowered_sha256"]


def test_main_setup_failure_is_journaled_after_main_started() -> None:
    rows: list[tuple[str, object]] = []
    journal = RecoveryJournal(lambda name, value: rows.append((name, value)))
    session = ScriptedSession((SERVICE,), fail_workspace_on_call=1)
    controller = runtime_module().PrototypeRuntimeController(session, SERVICE, journal=journal)

    with pytest.raises(ProtocolError, match="outcome is unknown"):
        controller.execute_main(
            'Секрет = "digest-only";',
            capture_points=(CAPTURE_A,),
        )

    events = [
        value
        for name, value in rows
        if name == "write-journal.jsonl" and isinstance(value, dict)
    ]
    lifecycle = [event for event in events if event["event"].startswith("main_")]
    assert [event["event"] for event in lifecycle] == ["main_started", "main_failed"]
    assert len(lifecycle[0]["visible_sha256"]) == 64
    assert lifecycle[1]["visible_sha256"] == lifecycle[0]["visible_sha256"]
    assert lifecycle[1]["lowered_sha256"] == lifecycle[0]["lowered_sha256"]
    assert "digest-only" not in str(lifecycle)


def test_main_transport_dispatch_callback_excludes_local_setup_failure() -> None:
    runtime = runtime_module()
    failed_session = ScriptedSession((SERVICE,), fail_workspace_on_call=1)
    failed = runtime.PrototypeRuntimeController(failed_session, SERVICE)
    failed_boundaries: list[str] = []

    with pytest.raises(ProtocolError, match="outcome is unknown"):
        failed.execute_main(
            "Результат = 1;",
            capture_points=(CAPTURE_A,),
            on_transport_dispatch=lambda: failed_boundaries.append("dispatch"),
        )

    assert failed_boundaries == []
    assert failed_session.continue_count == 0

    session = ScriptedSession((SERVICE,))
    controller = runtime.PrototypeRuntimeController(session, SERVICE)
    observed: list[tuple[int, object]] = []
    controller.execute_main(
        "Результат = 1;",
        on_transport_dispatch=lambda: observed.append(
            (session.continue_count, controller.state)
        ),
    )

    assert observed == [(0, runtime.OperationState.MAIN_PENDING)]
    assert session.continue_count == 1


def test_capture_and_continue_callbacks_mark_actual_transport_boundaries() -> None:
    runtime = runtime_module()
    session = ScriptedSession(
        (CAPTURE_A, SERVICE),
        capture_evaluations=(evaluation("Число", "901"),),
    )
    controller = captured_controller(session)
    capture_boundaries: list[object] = []

    controller.execute_capture(
        "РезультатИнструкции = 901;",
        on_transport_dispatch=lambda: capture_boundaries.append(controller.state),
    )

    assert capture_boundaries == [runtime.OperationState.EVALUATING_CAPTURE]
    continue_boundaries: list[tuple[int, object]] = []
    controller.resume(
        on_transport_dispatch=lambda: continue_boundaries.append(
            (session.continue_count, controller.state)
        )
    )

    assert continue_boundaries == [(1, runtime.OperationState.RESUMING)]
    assert session.continue_count == 2


def test_main_completion_decode_failure_records_only_safe_phase_metadata() -> None:
    rows: list[tuple[str, object]] = []
    journal = RecoveryJournal(lambda name, value: rows.append((name, value)))
    session = ScriptedSession(
        (SERVICE,),
        main_results=(evaluation("Число", "sensitive-invalid-number"),),
    )
    controller = runtime_module().PrototypeRuntimeController(
        session,
        SERVICE,
        journal=journal,
    )

    with pytest.raises(ProtocolError, match="MAIN completion result"):
        controller.execute_main("Результат = 1;")

    failed = [
        value
        for name, value in rows
        if name == "write-journal.jsonl"
        and isinstance(value, dict)
        and value.get("event") == "main_failed"
    ][-1]
    assert failed["error_type"] == "CompletionDecodeError"
    assert failed["failure_phase"] == "result"
    assert failed["evaluation_type"] == "Число"
    assert failed["exact_decimal_present"] is False
    assert "sensitive-invalid-number" not in str(failed)


def test_restore_failure_blocks_resume_without_continue() -> None:
    runtime = runtime_module()
    session = ScriptedSession(
        (CAPTURE_A,),
        capture_evaluations=(evaluation("Число", "901"),),
        fail_workspace_on_call=3,
    )
    controller = captured_controller(session)

    with pytest.raises(runtime.BreakpointRestoreError):
        controller.execute_capture("РезультатИнструкции = 901;")

    assert controller.state is runtime.OperationState.BREAKPOINT_RESTORE_FAILURE
    assert session.continue_count == 1
    with pytest.raises(ProtocolError, match="breakpoint_restore_failure"):
        controller.resume()


def test_shield_install_failure_does_not_start_evaluation() -> None:
    runtime = runtime_module()
    session = ScriptedSession((CAPTURE_A,), fail_workspace_on_call=2)
    controller = captured_controller(session)

    with pytest.raises(ProtocolError, match="outcome is unknown"):
        controller.execute_capture("РезультатИнструкции = 901;")

    capture_evaluations = [
        value
        for name, value in session.calls
        if name == "evaluate" and "ВыполнитьКодВКонтекстеОтладки" in str(value)
    ]
    assert capture_evaluations == []
    assert controller.state is runtime.OperationState.RECOVERING
