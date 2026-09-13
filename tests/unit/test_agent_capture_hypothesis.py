from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path

import nbformat
import pytest

import onec_runtime_mcp.agent.contracts as agent_contracts
from onec_runtime_mcp.agent.capture_contracts import CaptureFence, CaptureView, ResolvedCapturePoint
from onec_runtime_mcp.agent.contracts import AgentOperationState, BackendExecution, CapabilityMode, CodeLanguage, CodeMode, CodeRevision, RuntimeDescriptor, ServiceResponse, StateChanged, to_wire
from onec_runtime_mcp.agent.facade import AgentFacade
from onec_runtime_mcp.agent.runtime_backend import CaptureHypothesisPreparationError, OnecRuntimeBackend
from onec_runtime_mcp.agent.observation import ManagerOrigin, ObservationItem, ObservationPlan, ObservationResult, ObservationSource, ObservationSourceKind
from onec_runtime_mcp.agent.proxies import SizeAccuracy, ValueSize
from onec_runtime_mcp.agent.service import AgentWorkspaceService, _AdmittedRuntime
from onec_runtime.prototype_runtime import OperationState
from onec_runtime.errors import ProtocolError
from onec_runtime.runtime_api import RuntimeNamespaceSnapshot, RuntimeReply, RuntimeReplyKind


def _request() -> dict[str, object]:
    return {
        "fence": {
            "capture_intent_id": "capture-intent",
            "operation_id": "op-capture",
            "source_revision": 7,
            "source_sha256": "a" * 64,
            "capture_generation": 1,
            "stop_sequence": 1,
        },
        "code_ref": {
            "cell_id": "cell-hypothesis",
            "revision": 2,
            "source_sha256": "b" * 64,
        },
        "request_id": "hypothesis-request-1",
        "observe": {"items": []},
    }


def test_facade_routes_one_fenced_hypothesis_request_to_the_domain() -> None:
    # Break caught: removing this explicit paused-CAPTURE intent or routing it
    # through generic code.run makes its fence and operation kind disappear.
    class Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def call(self, method: str, arguments: dict[str, object]) -> ServiceResponse:
            self.calls.append((method, arguments))
            return ServiceResponse.success(
                {
                    "operation": {
                        "operation_id": "op-hypothesis", "kind": "capture_hypothesis",
                        "runtime_id": "runtime-1", "runtime_generation": 1,
                        "cell_id": "cell-hypothesis", "revision": 2,
                        "source_sha256": "b" * 64,
                    },
                    "state": "captured", "messages": [], "next_message_cursor": 0,
                    "next_event_cursor": 1, "changed_variables": [],
                    "change_confidence": "unknown", "outputs": {}, "capture": None,
                    "failure": None, "recovery": [],
                    "truncation": {"messages": False, "changed_variables": False, "outputs": False},
                }
            )

    client = Client()

    result = AgentFacade(client).capture_hypothesis(_request())

    assert result.operation.kind.value == "capture_hypothesis"
    assert client.calls[0][0] == "capture.hypothesis"
    assert client.calls[0][1]["fence"] == _request()["fence"]
    assert client.calls[0][1]["code_ref"] == _request()["code_ref"]


class _Runtime:
    runtime_id = "runtime-1"

    def __init__(self) -> None:
        self.sources: list[str] = []
        self.continue_calls = 0
        self.fail_execution = False
        self.unknown_execution = False
        self.manager_calls = 0
        self.table_calls: list[object] = []
        self.table_timeouts: list[float | None] = []
        self.table_transfer_calls: list[dict[str, object]] = []
        self.table_materialize_calls: list[tuple[str, dict[str, object]]] = []
        self.is_closed = False
        self.prepare_calls = 0
        self.prepare_error: BaseException | None = None
        self.execution_diagnostic = None
        self.before_execute = None
        self.quarantine_calls: list[CaptureFence] = []
        self.selected_table_delay = None

    def execute_bsl(self, source: str) -> BackendExecution:
        self.sources.append(source)
        if self.unknown_execution:
            return BackendExecution(AgentOperationState.UNKNOWN, (), False, "unknown")
        if self.fail_execution:
            return BackendExecution(
                AgentOperationState.FAILED,
                ("compile diagnostic",),
                False,
                "captured",
                failure_stage="execution",
                diagnostic=self.execution_diagnostic,
                state_changed=(
                    StateChanged.PARTIAL
                    if self.execution_diagnostic is not None
                    else StateChanged.UNKNOWN
                ),
            )
        # Mirrors OnecRuntimeBackend: a successful CAPTURE cell remains at
        # the controller's paused CAPTURED state rather than MAIN's COMPLETED.
        return BackendExecution(AgentOperationState.CAPTURED, ("hypothesis message",), True, "captured")

    def prepare_capture_hypothesis(
        self, source: str, capture: CaptureFence
    ) -> object:
        assert capture == _fence()
        self.prepare_calls += 1
        if self.prepare_error is not None:
            raise self.prepare_error
        from onec_runtime.bsl import (
            DiagnosticStage,
            LoweringMode,
            SemanticLoweringError,
            SemanticNotebookLowerer,
            VisibleSourceContext,
            normalize_source_error,
        )
        from onec_runtime.bsl.lexer import BslLexError
        from onec_runtime.bsl.notebook_cells import split_notebook_cell
        from onec_runtime.bsl.parser_target import BslParseError, PythonParserTarget
        from onec_runtime.bsl.source_maps import (
            SourceUnitKind,
            SourceUnitRef,
            mapped_visible_source,
            source_sha256,
        )

        target = PythonParserTarget.from_generated()
        unit = SourceUnitRef(
            SourceUnitKind.NOTEBOOK_CELL,
            "anonymous-notebook-cell",
            0,
            source_sha256(source),
        )
        visible = mapped_visible_source(source, unit)
        context = VisibleSourceContext({unit: source})
        try:
            cell = split_notebook_cell(target, source, source_unit=unit)
        except (BslParseError, BslLexError) as error:
            raise CaptureHypothesisPreparationError(
                normalize_source_error(
                    error,
                    visible,
                    stage=DiagnosticStage.PARSING,
                    visible_source_context=context,
                )
            ) from None
        if cell.has_methods or cell.statements is None:
            error = SemanticLoweringError(
                "CAPTURE hypotheses require executable statements without methods"
            )
            raise CaptureHypothesisPreparationError(
                normalize_source_error(
                    error,
                    visible,
                    stage=DiagnosticStage.LOWERING,
                    visible_source_context=context,
                )
            )
        try:
            lowering = SemanticNotebookLowerer(target).lower_mapped(
                cell.statements, mode=LoweringMode.CAPTURE
            )
        except (BslParseError, BslLexError) as error:
            raise CaptureHypothesisPreparationError(
                normalize_source_error(
                    error,
                    cell.statements,
                    stage=DiagnosticStage.PARSING,
                    visible_source_context=context,
                )
            ) from None
        except SemanticLoweringError as error:
            raise CaptureHypothesisPreparationError(
                normalize_source_error(
                    error,
                    cell.statements,
                    stage=DiagnosticStage.LOWERING,
                    visible_source_context=context,
                )
            ) from None
        provenance_type = getattr(
            agent_contracts,
            "OperationExecutionProvenance",
            None,
        )
        provenance = (
            object()
            if provenance_type is None
            else provenance_type(
                visible_source_sha256=source_sha256(source),
                executed_source_sha256=(
                    lowering.mapped_source.artifact.source_sha256
                ),
                source_map_sha256=lowering.mapped_source.source_map_sha256,
                mode="capture",
            )
        )
        return (
            source,
            lowering.dirty_roots,
            provenance,
        )

    def prepared_capture_hypothesis_provenance(
        self, prepared: object
    ) -> object:
        _, _, provenance = prepared  # type: ignore[misc]
        provenance_type = getattr(
            agent_contracts,
            "OperationExecutionProvenance",
            None,
        )
        assert provenance_type is not None
        assert isinstance(provenance, provenance_type)
        return provenance

    def execute_capture_hypothesis(
        self, prepared: object, capture: CaptureFence
    ) -> BackendExecution:
        assert capture == _fence()
        source, dirty_roots, _ = prepared  # type: ignore[misc]
        if self.before_execute is not None:
            self.before_execute()
        return replace(
            self.execute_bsl(source),
            capture_dirty_roots=dirty_roots,
        )

    def namespace_snapshot(self) -> RuntimeNamespaceSnapshot:
        return RuntimeNamespaceSnapshot(1, 1, ())

    def require_public_value_handle(self, handle: str) -> None:
        del handle

    def status(self) -> RuntimeDescriptor:
        return RuntimeDescriptor(
            self.runtime_id,
            1,
            "captured",
            CapabilityMode.EXPERIMENT,
            active_operation_id="9",
        )

    def close(self) -> None:
        self.is_closed = True

    def quarantine_capture_inspection(self, capture: CaptureFence) -> None:
        self.quarantine_calls.append(capture)

    def frame_variables(
        self, capture: CaptureFence, *, filters, cursor, limit,
        timeout_s: float | None = None,
    ):  # type: ignore[no-untyped-def]
        del timeout_s
        assert capture == _fence()
        items = (
            {"name": "Сумма", "type_name": "Число", "role": "local", "handle": "frame-sum"},
            {"name": "Второе", "type_name": "Число", "role": "local", "handle": "frame-second"},
            {"name": "Третье", "type_name": "Число", "role": "local", "handle": "frame-third"},
        )
        matching = tuple(item for item in items if not filters or filters["name"].casefold() in item["name"].casefold())
        return {"items": matching[cursor : cursor + limit], "total": len(matching), "next_cursor": None}

    def resolve_manager_origin(
        self, capture: CaptureFence, origin: ManagerOrigin, *,
        timeout_s: float | None = None,
    ) -> dict[str, object]:
        del timeout_s
        assert capture == _fence()
        self.manager_calls += 1
        return {
            "key": "query-temporary-tables",
            "handle": "manager-native",
            "type_name": "МенеджерВременныхТаблиц",
        }

    def temporary_tables(
        self, capture: CaptureFence, manager_handle: str, *, names, cursor, limit, selection,
        timeout_s: float | None = None,  # type: ignore[no-untyped-def]
    ) -> dict[str, object]:
        assert capture == _fence()
        assert manager_handle == "manager-native"
        self.table_calls.append(selection)
        self.table_timeouts.append(timeout_s)
        if selection is not None and self.selected_table_delay is not None:
            self.selected_table_delay()
        selected_rows = None if selection is None else selection.limit
        return {
            "items": (
                {
                    "name": "Итоги",
                    "schema": ("Сотрудник", "Сумма"),
                    "known_size": ValueSize(
                        rows=3 if selected_rows is None else selected_rows,
                        accuracy=SizeAccuracy.EXACT,
                    ),
                    "handle": "table-full" if selection is None else "table-selected",
                },
            ),
            "total": 1,
            "next_cursor": None,
        }

    def materialization_kind(
        self, handle: str, *, timeout_s: float | None = None
    ) -> str:
        del timeout_s
        assert handle in {"table-full", "table-selected"}
        return "table"

    def materialize_table_payload(self, handle: str, **options: object) -> bytes:
        assert handle in {"table-full", "table-selected"}
        self.table_transfer_calls.append(dict(options))
        schema = {
            "version": 1,
            "columns": ["Сотрудник", "Сумма"],
            "kinds": ["string", "integer"],
            "reference_modes": {},
        }
        return (
            json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
            + "\n"
            + json.dumps(["Иванов", 3], ensure_ascii=False, separators=(",", ":"))
            + "\n"
        ).encode()

    def materialize_value(self, handle: str, **options: object) -> object:
        # Production capture metadata handles are deliberately rejected by
        # PrototypeRuntimeController.capture_value_handle.  Keeping that
        # contract in this fake prevents a metadata-only inventory proxy from
        # silently becoming an unbounded row transfer in unit tests.
        assert handle == "table-selected"
        self.table_materialize_calls.append((handle, dict(options)))
        return ({"Сотрудник": "Иванов", "Сумма": 3},)


class _Factory:
    def start(self, *, mode: CapabilityMode) -> object:
        raise AssertionError(f"unexpected runtime start: {mode}")


class _Clock:
    def __init__(self) -> None:
        self.value = 1_000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _fence() -> CaptureFence:
    return CaptureFence("capture-intent", "op-capture", 7, "a" * 64, 1, 1)


def _capture() -> CaptureView:
    return CaptureView(
        _fence(),
        ResolvedCapturePoint(
            "before", "zup", "Payroll", "Run", 17, 7, "a" * 64, 17, "Выполнить();"
        ),
        None,
    )


def _captured_service(
    tmp_path: Path, *, source: str = "КонтекстОтладки.Сумма = 3;"
) -> tuple[AgentWorkspaceService, _Runtime, dict[str, object]]:
    digest = sha256(source.encode()).hexdigest()
    cell = nbformat.v4.new_code_cell(source=source, id="cell-hypothesis")
    cell.metadata["onec_runtime"] = {
        "revision": 2,
        "language": "bsl",
        "mode": "capture",
        "source_sha256": digest,
    }
    nbformat.write(nbformat.v4.new_notebook(cells=[cell]), tmp_path / "demo.ipynb")
    runtime = _Runtime()
    service = AgentWorkspaceService(tmp_path, _Factory(), maximum_mode=CapabilityMode.EXPERIMENT)
    service._runtime = _AdmittedRuntime(runtime, runtime.runtime_id, 1, CapabilityMode.EXPERIMENT)  # type: ignore[arg-type]
    service._install_onec_resolver(runtime, seed_namespace=False)  # type: ignore[arg-type]
    service._selected["default"] = runtime.runtime_id
    service._capture.activate_capture_view(_capture(), service._proxy_registry)
    assert service.call("code.list", {"container": "demo.ipynb"}).ok
    return service, runtime, {
        "fence": {
            "capture_intent_id": "capture-intent", "operation_id": "op-capture",
            "source_revision": 7, "source_sha256": "a" * 64,
            "capture_generation": 1, "stop_sequence": 1,
        },
        "code_ref": {"cell_id": "cell-hypothesis", "revision": 2, "source_sha256": digest},
        "request_id": "hypothesis-service-1",
        "wait_s": 2.0,
    }


def _capture_execution_diagnostic(
    source: str,
    *,
    line: int = 1,
    column: int = 1,
):  # type: ignore[no-untyped-def]
    from onec_runtime.bsl import (
        DiagnosticStage,
        LoweringMode,
        SemanticNotebookLowerer,
        VisibleSourceContext,
        parse_platform_diagnostic,
        remap_platform_diagnostic,
    )
    from onec_runtime.bsl.notebook_cells import split_notebook_cell
    from onec_runtime.bsl.parser_target import PythonParserTarget
    from onec_runtime.bsl.source_maps import SourceUnitKind, SourceUnitRef

    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL,
        "anonymous-notebook-cell",
        0,
        sha256(source.encode()).hexdigest(),
    )
    target = PythonParserTarget.from_generated()
    cell = split_notebook_cell(target, source, source_unit=unit)
    assert cell.statements is not None
    executed = SemanticNotebookLowerer(target).lower_mapped(
        cell.statements,
        mode=LoweringMode.CAPTURE,
    ).mapped_source
    return remap_platform_diagnostic(
        parse_platform_diagnostic(
            "{<Неизвестный модуль>"
            f"({line}, {column})"
            "}: rdbg_pid=9182 token=private"
        ),
        executed,
        stage=DiagnosticStage.EXECUTION,
        visible_source_context=VisibleSourceContext({unit: source}),
    )


def _manager_id(service: AgentWorkspaceService, request: dict[str, object]) -> str:
    response = service.call(
        "capture.inspect",
        {
            "fence": request["fence"],
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
                                "root": "Запрос",
                                "fields": ["МенеджерВременныхТаблиц"],
                            },
                        },
                    }
                ]
            },
        },
    )
    assert response.ok
    return response.value.temporary_table_managers[0].manager_id


def _table_item(
    alias: str,
    manager_id: str,
    *,
    offset: int = 0,
    limit: int = 1,
    result: str = "proxy",
    columns: list[str] | None = None,
) -> dict[str, object]:
    return {
        "alias": alias,
        "source": {
            "kind": "temporary_table",
            "manager_id": manager_id,
            "table": "Итоги",
        },
        "select": {
            "kind": "table_rows",
            "offset": offset,
            "limit": limit,
            "columns": ["Сотрудник", "Сумма"] if columns is None else columns,
        },
        "result": result,
    }


def test_hypothesis_executes_the_exact_capture_revision_and_remains_paused(tmp_path: Path) -> None:
    # Break caught: a generic code run could execute a different revision or
    # return COMPLETED, which would permit an implicit debugger continuation.
    service, runtime, request = _captured_service(tmp_path)
    try:
        response = service.call("capture.hypothesis", request)

        assert response.ok
        view = response.value
        assert view.state is AgentOperationState.CAPTURED
        assert view.capture is not None and view.capture.paused is True
        assert view.capture.mutable_object_caveat == (
            "live mutable-object mutations are immediate and non-transactional"
        )
        assert view.capture.dirty_roots == ("Сумма",)
        assert view.messages == ("hypothesis message",)
        assert runtime.sources == ["КонтекстОтладки.Сумма = 3;"]
        assert runtime.continue_calls == 0
    finally:
        service.close()


def test_hypothesis_request_journal_failure_terminates_submission_and_releases_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, runtime, request = _captured_service(tmp_path)
    gates: list[object] = []
    original_startup = service._startup_operation_id

    def traced_startup(gate, holder):  # type: ignore[no-untyped-def]
        gates.append(gate)
        return original_startup(gate, holder)

    original_record = service._capture.record_request
    failures = [OSError("request journal open failed after submit")]

    def fail_once(request_id: str, fingerprint: str, operation_id: str) -> None:
        if failures:
            raise failures.pop()
        original_record(request_id, fingerprint, operation_id)

    monkeypatch.setattr(service, "_startup_operation_id", traced_startup)
    monkeypatch.setattr(service._capture, "record_request", fail_once)
    request = {**request, "request_id": "journal-failure-hypothesis", "wait_s": 0.05}
    try:
        failed = service.call("capture.hypothesis", request)
        follow_up = service._operations.submit(
            {
                "operation_kind": "code_run",
                "runtime_id": runtime.runtime_id,
                "runtime_generation": 1,
                "code_id": "after-journal-failure",
                "revision": 1,
                "source_sha256": "f" * 64,
                "inputs_sha256": "follow-up-hypothesis",
            },
            lambda: BackendExecution(
                AgentOperationState.COMPLETED, (), False, "ready"
            ),
        )
        follow_up = service._operations.wait(follow_up.operation_id, 0.05)

        assert (failed.ok, follow_up.state) == (
            True,
            AgentOperationState.COMPLETED,
        )
        assert failed.value.state is AgentOperationState.FAILED
        assert failed.value.failure == {
            "stage": "capture_request_journal",
            "partial_results": {},
        }
        assert runtime.sources == []
        replay = service.call("capture.hypothesis", request)
        assert replay.ok
        assert replay.value.operation.operation_id == failed.value.operation.operation_id
        assert replay.value.state is AgentOperationState.FAILED
        assert runtime.sources == []
    finally:
        for gate in gates:
            gate.set()  # type: ignore[attr-defined]
        service.close()


def test_hypothesis_stages_multiple_roots_and_keeps_message_and_event_cursors_separate(
    tmp_path: Path,
) -> None:
    source = "КонтекстОтладки.Сумма = 3; КонтекстОтладки.Итого = 4;"
    service, runtime, request = _captured_service(tmp_path, source=source)
    try:
        response = service.call("capture.hypothesis", request)

        assert response.ok
        assert response.value.capture.dirty_roots == ("Сумма", "Итого")
        assert response.value.messages == ("hypothesis message",)
        assert response.value.next_message_cursor > 0
        assert response.value.next_event_cursor > 0
        message_cursor = response.value.next_message_cursor
        event_cursor = response.value.next_event_cursor
        later = service.call(
            "operation.view",
            {
                "operation_id": response.value.operation.operation_id,
                "after_message_cursor": response.value.next_message_cursor,
            },
        )
        assert later.ok
        assert later.value.messages == ()
        assert later.value.next_message_cursor == message_cursor
        assert later.value.next_event_cursor == event_cursor
        assert runtime.sources == [source]
    finally:
        service.close()


def test_execution_error_preserves_the_capture_and_a_later_valid_hypothesis(tmp_path: Path) -> None:
    # Break caught: normalizing this failure to FAILED would lose the paused
    # frame or encourage the caller to resume/rollback before the next test.
    service, runtime, request = _captured_service(tmp_path)
    try:
        runtime.fail_execution = True
        failed = service.call("capture.hypothesis", request)

        assert failed.ok
        assert failed.value.state is AgentOperationState.CAPTURED
        assert failed.value.failure["stage"] == "execution"
        assert failed.value.capture is not None and failed.value.capture.paused is True
        assert runtime.continue_calls == 0

        runtime.fail_execution = False
        retry = service.call("capture.hypothesis", {**request, "request_id": "hypothesis-service-2"})
        assert retry.ok
        assert retry.value.state is AgentOperationState.CAPTURED
        assert len(runtime.sources) == 2
    finally:
        service.close()


def test_capture_execution_failure_keeps_partial_facts_and_predispatch_provenance(
    tmp_path: Path,
) -> None:
    """Break caught: CAPTURE failure drops its pause or calls preparation unprovenanced."""
    source = "КонтекстОтладки.Сумма = 3;"
    service, runtime, request = _captured_service(tmp_path, source=source)
    runtime.fail_execution = True
    runtime.execution_diagnostic = _capture_execution_diagnostic(source)
    observed_before_execute: list[object] = []

    def observe_provenance() -> None:
        descriptor = service._operations.list()[-1]
        observed_before_execute.append(
            service._operations.view_snapshot(
                descriptor.operation_id
            ).execution_provenance
        )

    runtime.before_execute = observe_provenance
    try:
        response = service.call("capture.hypothesis", request)

        assert response.ok
        view = response.value
        assert observed_before_execute == [view.execution_provenance]
        assert view.execution_provenance.visible_source_sha256 == sha256(
            source.encode()
        ).hexdigest()
        assert (
            view.execution_provenance.executed_source_sha256
            == runtime.execution_diagnostic.execution_artifact_sha256
        )
        assert (
            view.execution_provenance.source_map_sha256
            == runtime.execution_diagnostic.source_map_sha256
        )
        assert view.execution_provenance.mode == "capture"
        assert view.state is AgentOperationState.CAPTURED
        assert view.capture is not None and view.capture.paused is True
        assert view.capture.dirty_roots == ("Сумма",)
        assert view.failure["partial_results"] == {}
        assert view.failure["state_changed"] == "partial"
        assert view.failure["diagnostic"]["stage"] == "execution"
        assert runtime.continue_calls == 0
        public_journal = (
            tmp_path / ".runtime" / "agent-service" / "operations.jsonl"
        ).read_text(encoding="utf-8")
        assert source not in public_journal
        assert "rdbg_pid" not in public_journal
        assert "user_main_dispatched" not in public_journal
    finally:
        service.close()


def test_capture_acceptance_remaps_second_line_and_never_continues(
    tmp_path: Path,
) -> None:
    """Break caught: CAPTURE remapping resumes the target or loses line two."""
    source = (
        "КонтекстОтладки.Сумма = 3;\n"
        "РезультатИнструкции = КонтекстОтладки.Сумма;"
    )
    service, runtime, request = _captured_service(tmp_path, source=source)
    runtime.fail_execution = True
    runtime.execution_diagnostic = _capture_execution_diagnostic(
        source,
        line=2,
        column=23,
    )
    try:
        response = service.call("capture.hypothesis", request)

        assert response.ok
        view = response.value
        assert view.state is AgentOperationState.CAPTURED
        assert view.capture is not None and view.capture.paused is True
        assert runtime.continue_calls == 0
        assert view.failure["diagnostic"] == {
            "diagnostic_id": (
                "79fc35bf09c79cc010aa83154fc037403ba9ddaba6411e76ca7322a5bbf1b67a"
            ),
            "stage": "execution",
            "mapping_confidence": "exact",
            "visible_location": {
                "line": 2,
                "column": 23,
                "span": {"start": 49, "end": 50},
            },
            "related_visible_span": None,
            "excerpt": None,
            "synthetic_region": None,
        }
        assert view.execution_provenance.visible_source_sha256 == (
            "6672655e5860350ccf31d4a29ec09829c939eda1378fa2832e5856dac626f9e6"
        )
        assert view.execution_provenance.executed_source_sha256 == (
            "6672655e5860350ccf31d4a29ec09829c939eda1378fa2832e5856dac626f9e6"
        )
        assert view.execution_provenance.source_map_sha256 == (
            "686390169edb0c721ec32591be7d603a73742aa48c7f129f50d05a8f66490ca1"
        )

        public_path = (
            tmp_path / ".runtime" / "agent-service" / "operations.jsonl"
        )
        private_path = (
            tmp_path
            / ".runtime"
            / "agent-service"
            / "diagnostics.private.jsonl"
        )
        public_journal = public_path.read_text(encoding="utf-8")
        private_journal = private_path.read_text(encoding="utf-8")
        encoded = json.dumps(to_wire(view), ensure_ascii=False)
        diagnostic_id = view.failure["diagnostic"]["diagnostic_id"]
        expert = service._operations.expert_diagnostic(
            view.operation.operation_id,
            diagnostic_id,
        )
        expert_encoded = json.dumps(expert, ensure_ascii=False)
        assert source not in public_journal
        assert source not in private_journal
        assert source not in encoded
        assert source not in expert_encoded
        for forbidden in ("rdbg_pid", "9182", "token=private"):
            assert forbidden not in public_journal
            assert forbidden not in encoded
            assert forbidden not in expert_encoded
    finally:
        service.close()


def test_hypothesis_missing_provenance_capability_aborts_before_target_call(
    tmp_path: Path,
) -> None:
    """Break caught: a legacy prepared CAPTURE can execute without a manifest."""
    service, runtime, request = _captured_service(tmp_path)
    runtime.prepared_capture_hypothesis_provenance = None  # type: ignore[method-assign]
    try:
        response = service.call("capture.hypothesis", request)

        assert response.ok
        assert response.value.state is AgentOperationState.UNKNOWN
        assert response.value.execution_provenance is None
        assert response.value.failure == {
            "stage": "capture_preparation",
            "partial_results": {},
            "state_changed": "unknown",
        }
        assert runtime.sources == []
        assert runtime.quarantine_calls == [_fence()]
        assert runtime.continue_calls == 0
    finally:
        service.close()


def test_capture_lowering_failure_remains_paused_without_continue(
    tmp_path: Path,
) -> None:
    """Break caught: known CAPTURE preparation failures must be admitted."""
    service, runtime, request = _captured_service(
        tmp_path,
        source="КонтекстОтладки;",
    )
    try:
        response = service.call("capture.hypothesis", request)

        assert response.ok
        view = response.value
        assert view.state is AgentOperationState.FAILED
        assert view.failure["state_changed"] == "no"
        assert view.failure["diagnostic"]["stage"] == "lowering"
        assert service._capture.current_capture(_fence()).paused is True
        assert runtime.status().state == "captured"
        assert runtime.sources == []
        assert runtime.continue_calls == 0
    finally:
        service.close()


def test_parse_error_leaves_the_same_fence_paused_for_a_valid_hypothesis(tmp_path: Path) -> None:
    invalid, runtime, request = _captured_service(tmp_path, source="Результат = ;")
    try:
        rejected = invalid.call("capture.hypothesis", request)

        assert rejected.ok
        assert rejected.value.state is AgentOperationState.FAILED
        assert rejected.value.failure["stage"] == "parsing"
        assert rejected.value.failure["state_changed"] == "no"
        assert rejected.value.failure["diagnostic"]["stage"] == "parsing"
        assert rejected.value.operation.cell_id == "cell-hypothesis"
        assert rejected.value.operation.revision == 2
        assert (
            rejected.value.operation.source_sha256
            == request["code_ref"]["source_sha256"]
        )
        assert runtime.sources == []
        assert invalid._capture.current_capture(_fence()).paused is True

        source = "КонтекстОтладки.Сумма = 4;"
        digest = sha256(source.encode()).hexdigest()
        invalid._inline["inline-good"] = CodeRevision(
            "inline-good", 1, source, digest, digest, CodeLanguage.BSL, CodeMode.CAPTURE
        )
        recovered = invalid.call(
            "capture.hypothesis",
            {
                **request,
                "code_ref": {"cell_id": "inline-good", "revision": 1, "source_sha256": digest},
                "request_id": "hypothesis-after-parse-error",
            },
        )
        assert recovered.ok
        assert recovered.value.state is AgentOperationState.CAPTURED
        assert runtime.sources == [source]
    finally:
        invalid.close()


def test_semantic_lowering_error_is_distinct_from_parsing_and_never_executes(tmp_path: Path) -> None:
    service, runtime, request = _captured_service(tmp_path, source="КонтекстОтладки = 1;")
    try:
        response = service.call("capture.hypothesis", request)

        assert response.ok
        assert response.value.state is AgentOperationState.FAILED
        assert response.value.failure["stage"] == "lowering"
        assert response.value.failure["state_changed"] == "no"
        assert response.value.failure["diagnostic"]["stage"] == "lowering"
        assert runtime.sources == []
        assert service._capture.current_capture(_fence()).paused is True
    finally:
        service.close()


def test_preparation_infrastructure_failure_is_admitted_unknown_and_quarantines_capture(
    tmp_path: Path,
) -> None:
    # Break caught: a stale/unavailable controller during admitted preparation
    # makes pause ownership uncertain. Treating it like a harmless parse error
    # leaves the session ticket and frame proxies falsely usable.
    private = "RDBG target=secret pid=4242 token=private-prepared-artifact"
    service, runtime, request = _captured_service(tmp_path)
    runtime.prepare_error = ProtocolError(private)
    operations_before = tuple(service._operations.list())
    try:
        failed = service.call("capture.hypothesis", request)

        assert failed.ok
        assert failed.value.state is AgentOperationState.UNKNOWN
        assert failed.value.failure == {
            "stage": "capture_preparation",
            "partial_results": {},
            "state_changed": "unknown",
        }
        assert tuple(to_wire(action) for action in failed.value.recovery) == (
            {"method": "workspace.status", "arguments": {}},
            {
                "method": "runtime.close",
                "arguments": {"policy": "abort_generation"},
            },
            {
                "method": "runtime.ensure",
                "arguments": {"mode": "experiment", "profile": "default"},
            },
        )
        assert private not in json.dumps(
            to_wire(failed.value), ensure_ascii=False,
            sort_keys=True,
        )
        assert runtime.quarantine_calls == [_fence()]
        assert service._runtime is not None and service._runtime.closing is True
        operations_after = tuple(service._operations.list())
        assert len(operations_after) == len(operations_before) + 1
        assert operations_after[-1].operation_id == failed.value.operation.operation_id
        assert (
            failed.value.operation.source_sha256
            == request["code_ref"]["source_sha256"]
        )
        with pytest.raises(Exception):
            service._capture.current_capture(_fence())

        prepare_calls = runtime.prepare_calls
        frame_calls_before = len(runtime.table_calls)
        inspect = service.call(
            "capture.inspect",
            {"fence": request["fence"], "filters": {}, "cursor": 0, "limit": 20},
        )
        retry = service.call(
            "capture.hypothesis",
            {**request, "request_id": "blocked-after-preparation-failure"},
        )
        assert inspect.ok is False and retry.ok is False
        assert runtime.prepare_calls == prepare_calls
        assert len(runtime.table_calls) == frame_calls_before

        # These are ordered, executable recovery actions: workspace status is available
        # while quarantined and explicit abort closes the uncertain generation.
        assert service.call("workspace.status", {}).ok
        assert service.call(
            "runtime.close", {"policy": "abort_generation"}
        ).ok
        assert runtime.is_closed is True
    finally:
        service.close()


def test_stale_fence_after_admitted_preparation_is_quarantined(
    tmp_path: Path,
) -> None:
    # Preparation can succeed and still race a replaced pause before execution.
    # The admitted operation becomes UNKNOWN, the sealed artifact remains
    # unconsumed, and every former inspection route fails closed.
    service, runtime, request = _captured_service(tmp_path)
    original_prepare = runtime.prepare_capture_hypothesis

    def prepare_then_stale(source: str, capture: CaptureFence) -> object:
        prepared = original_prepare(source, capture)
        service._capture.invalidate_capture(service._proxy_registry)
        return prepared

    runtime.prepare_capture_hypothesis = prepare_then_stale  # type: ignore[method-assign]
    operations_before = tuple(service._operations.list())
    try:
        response = service.call("capture.hypothesis", request)

        assert response.ok
        assert response.value.state is AgentOperationState.UNKNOWN
        assert response.value.failure == {
            "stage": "capture_preparation",
            "partial_results": {},
            "state_changed": "unknown",
        }
        assert runtime.quarantine_calls == [_fence()]
        assert service._runtime is not None and service._runtime.closing is True
        operations_after = tuple(service._operations.list())
        assert len(operations_after) == len(operations_before) + 1
        assert operations_after[-1].operation_id == response.value.operation.operation_id
        assert runtime.sources == []
        assert runtime.continue_calls == 0
    finally:
        service.close()


def test_partial_frame_observation_failure_keeps_prior_output_dirty_root_and_pause(tmp_path: Path) -> None:
    service, runtime, request = _captured_service(tmp_path)
    try:
        request["observe"] = {
            "items": [
                {"alias": "first", "source": {"kind": "frame_local", "name": "Сумма"}},
                {"alias": "missing", "source": {"kind": "frame_local", "name": "НетТакой"}},
            ]
        }
        response = service.call("capture.hypothesis", request)

        assert response.ok
        view = response.value
        assert view.state is AgentOperationState.CAPTURED
        assert set(view.outputs) == {"first"}
        assert view.failure["stage"] == "observation"
        assert view.capture.dirty_roots == ("Сумма",)
        assert runtime.continue_calls == 0
    finally:
        service.close()


def test_hypothesis_request_replay_executes_once(tmp_path: Path) -> None:
    service, runtime, request = _captured_service(tmp_path)
    try:
        first = service.call("capture.hypothesis", request)
        replay = service.call("capture.hypothesis", request)

        assert first.ok and replay.ok
        assert first.value.operation.operation_id == replay.value.operation.operation_id
        assert runtime.sources == ["КонтекстОтладки.Сумма = 3;"]
    finally:
        service.close()


def test_staging_failure_keeps_capture_paused_and_allows_the_next_hypothesis(
    tmp_path: Path, monkeypatch
) -> None:
    # Break caught: an exception while recording a proven root must not let the
    # operation lane collapse to FAILED after the backend proved it is paused.
    service, runtime, request = _captured_service(tmp_path)
    original = service._capture.stage_dirty_roots
    calls = 0

    def fail_once(fence, roots):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("staging storage unavailable")
        return original(fence, roots)

    monkeypatch.setattr(service._capture, "stage_dirty_roots", fail_once)
    try:
        failed = service.call("capture.hypothesis", request)

        assert failed.ok
        assert failed.value.state is AgentOperationState.CAPTURED
        assert failed.value.failure["stage"] == "capture_staging"
        assert failed.value.capture is not None and failed.value.capture.paused is True

        recovered = service.call("capture.hypothesis", {**request, "request_id": "after-staging"})
        assert recovered.ok
        assert recovered.value.capture.dirty_roots == ("Сумма",)
        assert runtime.continue_calls == 0
    finally:
        service.close()


def test_unknown_capture_execution_never_claims_the_frame_remains_paused(tmp_path: Path) -> None:
    # Break caught: converting an infrastructure-unknown outcome into CAPTURED
    # would publish a stale frame that can no longer be safely inspected.
    service, runtime, request = _captured_service(tmp_path)
    runtime.unknown_execution = True
    try:
        response = service.call("capture.hypothesis", request)

        assert response.ok
        assert response.value.state is AgentOperationState.UNKNOWN
        assert response.value.capture is None
        assert response.value.failure["stage"] == "capture_hypothesis_transport"
        assert [to_wire(action) for action in response.value.recovery] == [
            {"method": "workspace.status", "arguments": {}},
            {
                "method": "operation.wait",
                "arguments": {
                    "operation_id": response.value.operation.operation_id,
                    "timeout_s": 0,
                    "after_event_cursor": 0,
                    "after_message_cursor": 0,
                },
            },
            {
                "method": "runtime.close",
                "arguments": {"policy": "abort_generation"},
            },
        ]
        assert runtime.continue_calls == 0
    finally:
        service.close()


def test_observation_workspace_setup_failure_preserves_staged_capture(
    tmp_path: Path, monkeypatch
) -> None:
    # Break caught: failure creating the optional Python bridge after a paused
    # cell must be reported as observation evidence, not generic FAILED.
    service, runtime, request = _captured_service(tmp_path)
    request["observe"] = {
        "budget_profile": "agent_dataframe",
        "items": [
            {"alias": "sum", "source": {"kind": "frame_local", "name": "Сумма"}, "result": "python"}
        ],
    }
    monkeypatch.setattr(service, "_python", lambda: (_ for _ in ()).throw(OSError("workspace unavailable")))
    try:
        response = service.call("capture.hypothesis", request)

        assert response.ok
        assert response.value.state is AgentOperationState.CAPTURED
        assert response.value.failure["stage"] == "observation"
        assert response.value.capture.dirty_roots == ("Сумма",)
        assert runtime.continue_calls == 0
    finally:
        service.close()


def test_selected_table_metadata_profile_rejects_before_backend_scan(tmp_path: Path) -> None:
    # Break caught: consuming the Task 3 selector before shared accounting used
    # to bypass the metadata profile and still call the table backend.
    service, runtime, request = _captured_service(tmp_path)
    try:
        manager_id = _manager_id(service, request)
        request["observe"] = {
            "budget_profile": "agent_metadata",
            "items": [_table_item("sample", manager_id)],
        }

        response = service.call("capture.hypothesis", request)

        assert response.ok
        assert response.value.state is AgentOperationState.CAPTURED
        assert response.value.outputs == {}
        assert response.value.failure["partial_results"] == {"sample": "unavailable"}
        assert runtime.table_calls == []
    finally:
        service.close()


def test_selected_table_preview_limit_and_offset_must_fit_before_backend_scan(
    tmp_path: Path,
) -> None:
    # Break caught: a syntactically bounded 10k selector must not inherit the
    # agent_preview profile's 100-row transfer allowance.
    service, runtime, request = _captured_service(tmp_path)
    try:
        manager_id = _manager_id(service, request)
        request["observe"] = {
            "budget_profile": "agent_preview",
            "items": [_table_item("sample", manager_id, offset=99, limit=2)],
        }

        response = service.call("capture.hypothesis", request)

        assert response.ok
        assert response.value.outputs == {}
        assert response.value.failure["partial_results"] == {"sample": "unavailable"}
        assert runtime.table_calls == []
    finally:
        service.close()


def test_selected_tables_share_one_aggregate_budget_and_keep_prior_output(
    tmp_path: Path,
) -> None:
    # Break caught: clearing both selectors made two 60-row native selections
    # look free even though the preview profile permits 100 rows in aggregate.
    service, runtime, request = _captured_service(tmp_path)
    try:
        manager_id = _manager_id(service, request)
        request["observe"] = {
            "budget_profile": "agent_preview",
            "items": [
                _table_item("first", manager_id, limit=60),
                _table_item("second", manager_id, limit=60, columns=["Сумма"]),
            ],
        }

        response = service.call("capture.hypothesis", request)

        assert response.ok
        assert set(response.value.outputs) == {"first"}
        assert response.value.failure["partial_results"] == {"second": "unavailable"}
        assert len(runtime.table_calls) == 1
    finally:
        service.close()


def test_row_budget_exhaustion_does_not_hide_later_proxy_only_item(
    tmp_path: Path,
) -> None:
    # A mixed plan accounts rows only for row consumers.  A proxy descriptor
    # after an exact 100-row transfer still fits the preview profile.
    service, runtime, request = _captured_service(tmp_path)
    try:
        manager_id = _manager_id(service, request)
        request["observe"] = {
            "budget_profile": "agent_preview",
            "items": [
                _table_item("all_rows", manager_id, limit=100),
                {
                    "alias": "scalar_proxy",
                    "source": {"kind": "frame_local", "name": "Сумма"},
                },
            ],
        }

        response = service.call("capture.hypothesis", request)

        assert response.ok
        assert set(response.value.outputs) == {"all_rows", "scalar_proxy"}
        assert response.value.failure is None
        assert len(runtime.table_calls) == 1
    finally:
        service.close()


def test_selected_table_dataframe_selects_and_transfers_once(tmp_path: Path) -> None:
    service, runtime, request = _captured_service(tmp_path)
    try:
        manager_id = _manager_id(service, request)
        request["observe"] = {
            "budget_profile": "agent_dataframe",
            "items": [_table_item("frame", manager_id, limit=1, result="dataframe")],
        }

        response = service.call("capture.hypothesis", request)

        assert response.ok
        assert set(response.value.outputs) == {"frame"}
        assert len(runtime.table_calls) == 1
        assert len(runtime.table_transfer_calls) == 1
        assert runtime.table_transfer_calls[0]["max_rows"] == 1
    finally:
        service.close()


def test_unselected_table_preview_synthesizes_one_bounded_head_selection_and_transfer(
    tmp_path: Path,
) -> None:
    # Break caught: capture.inspect returns a metadata-only table inventory
    # handle.  Preview must first read its bounded schema, then authorize one
    # server-owned head selection and materialize only that selected handle.
    service, runtime, request = _captured_service(tmp_path)
    try:
        manager_id = _manager_id(service, request)
        request["observe"] = {
            "budget_profile": "agent_preview",
            "items": [
                {
                    "alias": "manager",
                    "source": {
                        "kind": "temporary_table_manager",
                        "origin": {
                            "namespace": "frame",
                            "root": "Запрос",
                            "fields": ["МенеджерВременныхТаблиц"],
                        },
                    },
                },
                {
                    "alias": "table_preview",
                    "source": {
                        "kind": "temporary_table",
                        "manager_id": manager_id,
                        "table": "Итоги",
                    },
                    "result": "preview",
                },
            ],
        }

        response = service.call("capture.hypothesis", request)

        assert response.ok
        assert set(response.value.outputs) == {"manager", "table_preview"}
        assert response.value.outputs["manager"].type_name == "МенеджерВременныхТаблиц"
        assert response.value.outputs["table_preview"].bounded_preview is not None
        assert runtime.table_calls[0] is None
        selection = runtime.table_calls[1]
        assert selection.offset == 0
        assert selection.limit == 100
        assert selection.columns == ("Сотрудник", "Сумма")
        assert len(runtime.table_calls) == 2
        assert runtime.table_materialize_calls[0][0] == "table-selected"
        assert len(runtime.table_materialize_calls) == 1
        assert runtime.table_transfer_calls == []
    finally:
        service.close()


def test_unselected_dataframe_profile_preview_never_requests_over_100_capture_rows(
    tmp_path: Path,
) -> None:
    service, runtime, request = _captured_service(tmp_path)
    try:
        manager_id = _manager_id(service, request)
        request["observe"] = {
            "budget_profile": "agent_dataframe",
            "items": [
                {
                    "alias": "table_preview",
                    "source": {
                        "kind": "temporary_table",
                        "manager_id": manager_id,
                        "table": "Итоги",
                    },
                    "result": "preview",
                }
            ],
        }

        response = service.call("capture.hypothesis", request)

        assert response.ok
        selections = [selection for selection in runtime.table_calls if selection is not None]
        assert len(selections) == 1
        assert selections[0].limit == 100
    finally:
        service.close()


def test_unselected_temporary_table_dataframe_requires_an_explicit_bounded_selection(
    tmp_path: Path,
) -> None:
    # Break caught: a dataframe request against the inventory proxy otherwise
    # attempts to turn a metadata-only handle into an unbounded full-table
    # transfer.  Callers must state the finite row selection explicitly.
    service, runtime, request = _captured_service(tmp_path)
    try:
        manager_id = _manager_id(service, request)
        request["observe"] = {
            "budget_profile": "agent_dataframe",
            "items": [
                {
                    "alias": "frame",
                    "source": {
                        "kind": "temporary_table",
                        "manager_id": manager_id,
                        "table": "Итоги",
                    },
                    "result": "dataframe",
                }
            ],
        }

        response = service.call("capture.hypothesis", request)

        assert response.ok
        assert response.value.state is AgentOperationState.CAPTURED
        assert response.value.outputs == {}
        assert response.value.failure["partial_results"] == {"frame": "unavailable"}
        assert runtime.table_calls == []
        assert runtime.table_transfer_calls == []
        assert runtime.continue_calls == 0
    finally:
        service.close()


def test_selection_and_preview_share_one_deadline_and_transfer_gets_only_remaining_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Break caught: resetting the five-second preview profile after native
    # selection gives selection plus transfer N times the advertised timeout.
    clock = _Clock()
    import onec_runtime_mcp.agent.capture_service as capture_service_module
    import onec_runtime_mcp.agent.service as service_module

    monkeypatch.setattr(service_module, "monotonic", clock)
    monkeypatch.setattr(capture_service_module, "monotonic", clock, raising=False)
    service, runtime, request = _captured_service(tmp_path)
    runtime.selected_table_delay = lambda: clock.advance(2.0)
    try:
        manager_id = _manager_id(service, request)
        request["observe"] = {
            "budget_profile": "agent_preview",
            "items": [
                _table_item("sample", manager_id, limit=1, result="preview")
            ],
        }

        response = service.call("capture.hypothesis", request)

        assert response.ok
        assert set(response.value.outputs) == {"sample"}
        assert runtime.table_timeouts == [pytest.approx(5.0)]
        assert len(runtime.table_materialize_calls) == 1
        transfer_timeout = runtime.table_materialize_calls[0][1]["timeout_s"]
        assert 0 < transfer_timeout <= 3.0
    finally:
        service.close()


def test_slow_native_selection_times_out_as_partial_observation_without_transfer_or_continue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Break caught: checking only before source resolution lets a slow RDBG
    # selection exceed the plan deadline and still start a second transfer.
    clock = _Clock()
    import onec_runtime_mcp.agent.capture_service as capture_service_module
    import onec_runtime_mcp.agent.service as service_module

    monkeypatch.setattr(service_module, "monotonic", clock)
    monkeypatch.setattr(capture_service_module, "monotonic", clock, raising=False)
    service, runtime, request = _captured_service(tmp_path)
    runtime.selected_table_delay = lambda: clock.advance(6.0)
    try:
        manager_id = _manager_id(service, request)
        request["observe"] = {
            "budget_profile": "agent_preview",
            "items": [
                {
                    "alias": "first",
                    "source": {"kind": "frame_local", "name": "Сумма"},
                },
                _table_item("slow", manager_id, limit=1, result="preview"),
            ],
        }

        response = service.call("capture.hypothesis", request)

        assert response.ok
        assert response.value.state is AgentOperationState.CAPTURED
        assert set(response.value.outputs) == {"first"}
        assert response.value.failure["partial_results"] == {"slow": "unavailable"}
        assert runtime.table_timeouts and runtime.table_timeouts[-1] is not None
        assert runtime.table_materialize_calls == []
        assert response.value.capture.paused is True
        assert runtime.continue_calls == 0
    finally:
        service.close()


def test_proxy_outputs_before_and_after_python_setup_failure_remain_available(
    tmp_path: Path, monkeypatch
) -> None:
    # Break caught: eager plan-wide Python setup erased proxy evidence that did
    # not need Python and prevented later independent proxy observations.
    service, runtime, request = _captured_service(tmp_path)
    python_attempts = 0

    def unavailable_python():  # type: ignore[no-untyped-def]
        nonlocal python_attempts
        python_attempts += 1
        raise OSError("workspace unavailable")

    monkeypatch.setattr(service, "_python", unavailable_python)
    request["observe"] = {
        "budget_profile": "agent_dataframe",
        "items": [
            {"alias": "first", "source": {"kind": "frame_local", "name": "Сумма"}},
            {
                "alias": "python_value",
                "source": {"kind": "frame_local", "name": "Второе"},
                "result": "python",
            },
            {"alias": "third", "source": {"kind": "frame_local", "name": "Третье"}},
        ],
    }
    try:
        response = service.call("capture.hypothesis", request)

        assert response.ok
        assert set(response.value.outputs) == {"first", "third"}
        assert response.value.failure["partial_results"] == {"python_value": "unavailable"}
        assert python_attempts == 1
        assert response.value.capture.paused is True
        assert runtime.continue_calls == 0

        retry = service.call(
            "capture.hypothesis",
            {
                **request,
                "request_id": "after-python-setup-failure",
                "observe": {
                    "budget_profile": "agent_metadata",
                    "items": [
                        {
                            "alias": "still_paused",
                            "source": {"kind": "frame_local", "name": "Сумма"},
                        }
                    ],
                },
            },
        )
        assert retry.ok
        assert set(retry.value.outputs) == {"still_paused"}
        assert retry.value.capture.paused is True
        assert python_attempts == 1
    finally:
        service.close()


def test_zup_backend_marks_a_successful_capture_cell_as_paused_captured() -> None:
    # Break caught: treating this production reply as COMPLETED skips capture
    # root staging; treating an unsuccessful reply as success hides execution
    # diagnostics while the controller remains paused.
    class Session:
        def execute_bsl(self, source: str) -> RuntimeReply:
            assert source == "КонтекстОтладки.Сумма = 3;"
            return RuntimeReply(
                RuntimeReplyKind.CAPTURE_CELL, 9, OperationState.CAPTURED,
                result=3, succeeded=True,
            )

    outcome = OnecRuntimeBackend("runtime-zup", Session()).execute_bsl("КонтекстОтладки.Сумма = 3;")  # type: ignore[arg-type]

    assert outcome.terminal_state is AgentOperationState.CAPTURED
    assert outcome.runtime_state == "captured"
    assert outcome.failure_stage is None


def test_stale_hypothesis_fence_is_rejected_before_runtime_execution(tmp_path: Path) -> None:
    service, runtime, request = _captured_service(tmp_path)
    try:
        request["fence"] = {**request["fence"], "stop_sequence": 2}  # type: ignore[dict-item]

        response = service.call("capture.hypothesis", request)

        assert response.ok is False
        assert runtime.sources == []
    finally:
        service.close()


def test_hypothesis_source_hash_mismatch_is_rejected_before_backend_preparation(
    tmp_path: Path,
) -> None:
    service, runtime, request = _captured_service(tmp_path)
    try:
        request["code_ref"] = {
            **request["code_ref"],  # type: ignore[dict-item]
            "source_sha256": "f" * 64,
        }

        response = service.call("capture.hypothesis", request)

        assert response.ok is False
        assert runtime.prepare_calls == 0
        assert runtime.sources == []
        assert service._capture.current_capture(_fence()).paused is True
    finally:
        service.close()
