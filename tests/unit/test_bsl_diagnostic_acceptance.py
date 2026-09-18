from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from threading import Event

import pytest

import onec_runtime.privacy as privacy
import onec_runtime.bsl.module_delta as module_delta
from onec_runtime_mcp.agent.contracts import (
    AgentOperationState,
    BackendExecution,
    OperationExecutionProvenance,
    StateChanged,
    to_wire,
)
from onec_runtime_mcp.agent.operation_view import OperationViewProjector
from onec_runtime_mcp.agent.operations import OperationRegistry
from onec_runtime.bsl.diagnostics import (
    DiagnosticStage,
    NormalizedDiagnostic,
    VisibleSourceContext,
    WorkerDiagnosticArtifact,
    parse_platform_diagnostic,
    remap_platform_diagnostic,
    remap_worker_runtime_diagnostic,
)
from onec_runtime.bsl.module_delta import (
    build_full_worker_semantic_snapshot,
    try_build_worker_semantic_delta,
)
from onec_runtime.bsl.module_catalog import (
    CommonModuleCatalogSnapshot,
    CommonModuleDescriptor,
    CommonModuleScope,
)
from onec_runtime.bsl.module_universe import WorkerModuleUnit
from onec_runtime.bsl.notebook_cells import split_notebook_cell
from onec_runtime.bsl.parser_target import PythonParserTarget
from onec_runtime.bsl.semantic_lowering import (
    LoweringMode,
    SemanticNotebookLowerer,
    WorkerExport,
)
from onec_runtime.bsl.source_maps import (
    LineIndex,
    MappedSource,
    SourceSpan,
    SourceUnitKind,
    SourceUnitRef,
    mapped_visible_source,
    source_sha256,
)
from onec_runtime_jupyter.extension import NotebookDisplayConfig, _display_reply
from onec_runtime.prototype_runtime import OperationState, PrototypeRuntimeController
from onec_runtime.runtime_api import PrototypeRuntimeApi, RuntimeReply, RuntimeReplyKind


_MIXED_SOURCE = (
    "Процедура Удвоить(Значение)\n"
    "    Возврат Значение * 2;\n"
    "КонецПроцедуры\n\n"
    "Исходное = 21;\n"
    "Результат = Удвоить(Исходное);"
)

_MULTILINE_SOURCE_LEAK_CONTROL = "ПерваяСтрока();\nВтораяСтрока();"
_SOURCE_BEARING_KEYS = frozenset(
    {
        "business_source",
        "executed_source",
        "generated_source",
        "lowered_source",
        "mapped_source",
        "source",
        "statement_source",
        "visible_source",
        "worker_source",
    }
)


def _assert_no_source_material(
    value: object,
    *,
    forbidden_sources: tuple[str, ...],
) -> None:
    """Apply the acceptance gate to one candidate public/evidence value."""
    assert forbidden_sources
    assert all(type(source) is str and source for source in forbidden_sources)

    def visit(item: object, trail: tuple[str, ...]) -> None:
        if isinstance(item, Mapping):
            for key, nested in item.items():
                assert type(key) is str, (
                    f"non-text key at {'.'.join(trail) or '<root>'}"
                )
                current = (*trail, key)
                assert key.casefold() not in _SOURCE_BEARING_KEYS, (
                    f"source-bearing key at {'.'.join(current)}"
                )
                visit(nested, current)
            return
        if isinstance(item, (tuple, list)):
            for index, nested in enumerate(item):
                visit(nested, (*trail, str(index)))
            return
        if isinstance(item, str):
            for source in forbidden_sources:
                assert source not in item, (
                    f"full source at {'.'.join(trail) or '<root>'}"
                )

    visit(value, ())


@pytest.mark.parametrize(
    ("candidate", "failure"),
    (
        (
            {"nested": [{"payload": _MULTILINE_SOURCE_LEAK_CONTROL}]},
            "full source",
        ),
        (
            json.loads(
                '{"event":"leak","payload":"'
                'ПерваяСтрока();\\nВтораяСтрока();"}'
            ),
            "full source",
        ),
        (
            {"visible_source": "hash-placeholder"},
            "source-bearing key",
        ),
    ),
    ids=("structured-value", "parsed-jsonl", "source-bearing-key"),
)
def test_source_privacy_gate_rejects_multiline_and_source_key_controls(
    candidate: object,
    failure: str,
) -> None:
    """Break caught: escaped JSON scanning accepts source text or raw-source keys."""
    with pytest.raises(AssertionError, match=failure):
        _assert_no_source_material(
            candidate,
            forbidden_sources=(_MULTILINE_SOURCE_LEAK_CONTROL,),
        )


@dataclass(frozen=True, slots=True)
class _ScenarioSpec:
    source: str
    platform_line: int
    platform_column: int
    branch: str = "main"
    context_names: tuple[str, ...] = ()
    worker_exports: tuple[tuple[str, str], ...] = ()


_SCENARIOS = {
    "persistent_reference": _ScenarioSpec(
        "// вводная\nРезультат = 0;\nРезультат = Сохраненное;",
        2,
        32,
        context_names=("Сохраненное",),
    ),
    "message_argument": _ScenarioSpec(
        "Результат = 0;\n"
        "Результат = 1;\n"
        "Результат = 2;\n"
        'Сообщить("Ошибка 😀");',
        6,
        58,
    ),
    "worker_export_argument": _ScenarioSpec(
        "Результат = 0;\n"
        "Результат = 1;\n"
        "Результат = 2;\n"
        "Результат = 3;\n"
        "Результат = Удвоить(Повтор + Повтор);",
        5,
        101,
        context_names=("Повтор",),
        worker_exports=(("Удвоить", "Удвоить"),),
    ),
    "worker_export_callee": _ScenarioSpec(
        "Результат = Удвоить(1);",
        1,
        46,
        worker_exports=(("Удвоить", "Удвоить"),),
    ),
    "main_result_channel": _ScenarioSpec(
        "// 1\n// 2\n// 3\n// 4\n// 5\nРезультат = 1 / 0;",
        1,
        1,
    ),
    "capture_result_channel": _ScenarioSpec(
        "e1cRuntimeКонтекстОтладки.Счётчик = 1;\n"
        "РезультатИнструкции = e1cRuntimeКонтекстОтладки.Счётчик;",
        2,
        1,
        branch="capture",
    ),
    "mixed_method": _ScenarioSpec(
        _MIXED_SOURCE,
        2,
        13,
        branch="worker",
    ),
    "mixed_statement": _ScenarioSpec(
        _MIXED_SOURCE,
        2,
        73,
        branch="main",
        worker_exports=(("Удвоить", "Удвоить"),),
    ),
    "synthetic_wrapper": _ScenarioSpec(
        'Сообщить("x");',
        2,
        1,
    ),
    "lf_nonbmp": _ScenarioSpec(
        'Маркер = "😀";\nРезультат = Маркер;',
        2,
        32,
    ),
    "crlf_nonbmp": _ScenarioSpec(
        'Первая = "😀";\r\nРезультат = Первая;',
        2,
        32,
    ),
    "multiline_repeated": _ScenarioSpec(
        "Результат = (\n    Повтор +\n    Повтор);",
        3,
        24,
        context_names=("Повтор",),
    ),
    "nearest": _ScenarioSpec(
        "Ответ = 1;",
        1,
        30,
    ),
}


@dataclass(frozen=True, slots=True)
class _AcceptanceResult:
    public_diagnostic: dict[str, object]
    diagnostic: NormalizedDiagnostic
    executed: MappedSource
    lowered_text: str
    source: str
    source_unit: SourceUnitRef


class _AcceptanceScenario:
    def __init__(
        self,
        case: str,
        spec: _ScenarioSpec,
        parser_target: PythonParserTarget,
    ) -> None:
        self._case = case
        self._spec = spec
        self._parser_target = parser_target

    def run(self) -> _AcceptanceResult:
        spec = self._spec
        unit = SourceUnitRef(
            SourceUnitKind.NOTEBOOK_CELL,
            f"acceptance-{self._case}",
            1,
            source_sha256(spec.source),
        )
        cell = split_notebook_cell(
            self._parser_target,
            spec.source,
            source_unit=unit,
        )
        mode = (
            LoweringMode.CAPTURE
            if spec.branch == "capture"
            else LoweringMode.MAIN
        )
        if spec.branch == "worker":
            assert cell.worker is not None
            mapped = cell.worker
            lowered_text = mapped.text
        else:
            assert cell.statements is not None
            lowered = SemanticNotebookLowerer(
                self._parser_target,
                context_names=spec.context_names,
                worker_exports=tuple(
                    WorkerExport(public, method)
                    for public, method in spec.worker_exports
                ),
            ).lower_mapped(cell.statements, mode=mode)
            mapped = lowered.mapped_source
            lowered_text = mapped.text
            if lowered.messages_intercepted:
                mapped = PrototypeRuntimeController._with_message_collector_mapped(
                    mapped,
                    lowered.messages_intercepted,
                    "__acceptance_messages",
                )
        executed = PrototypeRuntimeController._as_executed_source(
            mapped,
            mode=mode,
        )
        parsed = parse_platform_diagnostic(
            "{<Неизвестный модуль>"
            f"({spec.platform_line},{spec.platform_column})"
            "}: Ошибка приемки"
        )
        diagnostic = remap_platform_diagnostic(
            parsed,
            executed,
            stage=DiagnosticStage.EXECUTION,
            visible_source_context=VisibleSourceContext({unit: spec.source}),
        )
        return _AcceptanceResult(
            public_diagnostic=privacy.diagnostic_to_public_wire(diagnostic),
            diagnostic=diagnostic,
            executed=executed,
            lowered_text=lowered_text,
            source=spec.source,
            source_unit=unit,
        )


@pytest.fixture(scope="module")
def parser_target() -> PythonParserTarget:
    return PythonParserTarget.from_generated()


@pytest.fixture
def scenario_factory(parser_target: PythonParserTarget):  # type: ignore[no-untyped-def]
    def create(case: str) -> _AcceptanceScenario:
        return _AcceptanceScenario(case, _SCENARIOS[case], parser_target)

    return create


@pytest.mark.parametrize(
    ("case", "confidence", "visible_line"),
    (
        ("persistent_reference", "exact", 3),
        ("message_argument", "exact", 4),
        ("worker_export_argument", "exact", 5),
        ("worker_export_callee", "exact", 1),
        ("main_result_channel", "exact", 6),
        ("capture_result_channel", "exact", 2),
        ("mixed_method", "exact", 2),
        ("mixed_statement", "exact", 6),
        ("synthetic_wrapper", "synthetic", None),
    ),
)
def test_visible_diagnostic_acceptance(
    case: str,
    confidence: str,
    visible_line: int | None,
    scenario_factory,
) -> None:
    """Break caught: a production transform loses or invents a visible line."""
    result = scenario_factory(case).run()

    assert result.public_diagnostic["mapping_confidence"] == confidence
    location = result.public_diagnostic.get("visible_location")
    assert (None if location is None else location["line"]) == visible_line


def test_same_name_worker_callee_acceptance_uses_literal_visible_coordinates(
    scenario_factory,
) -> None:
    """Break caught: acceptance only probes arguments beyond a Worker callee."""
    result = scenario_factory("worker_export_callee").run()

    assert result.public_diagnostic["mapping_confidence"] == "exact"
    assert result.public_diagnostic["visible_location"] == {
        "line": 1,
        "column": 13,
        "span": {"start": 12, "end": 13},
    }


@pytest.mark.parametrize(
    (
        "case",
        "column",
        "span",
        "visible_sha256",
        "executed_sha256",
        "source_map_sha256",
    ),
    (
        (
            "persistent_reference",
            13,
            {"start": 38, "end": 39},
            "b078ef31ebf4e18a5138c6c7368366e023271c6b6a63dcc8998250ce91b640d2",
            "3c83eaf7c4bb4c4560931245ad1f2d77abd695cc3bffe0ad7ac675907a6220c2",
            "9edc36c7b003bcfa2ea3c6dc36abdbc4f8003d3822449323653c219643cfa989",
        ),
        (
            "message_argument",
            11,
            {"start": 55, "end": 56},
            "ed69d8e16cbca333191c7107b713a814b1635222a336246cce859025066e1995",
            "e8476a552cae68b62d9d18dc3d54cf42b8c8dca35052262dc7d5f1ca677cd758",
            "f71f3a276b9bb639d7954ac47fc7ad5aea927f5003bb256ae2de6d9fc43fb39a",
        ),
        (
            "worker_export_argument",
            30,
            {"start": 89, "end": 90},
            "81609f2207ad37fe9a2d2546372157ce97ead44e52f36fdeb75e252a45502ea2",
            "14393fb80fe564dcb372a8540e6e96a57d3addff0db75a8aa24329999e6b2d78",
            "25addc240be5677840fa5990a1d533564dc4e759656ac39d007fda8cf1fd9a25",
        ),
        (
            "main_result_channel",
            1,
            {"start": 25, "end": 26},
            "14ebe3499133c3c18b2311ed9f1b0abdc06b1c94355c9eccec7182424393a260",
            "dc3f53f39d08cce70e6d62136f620a1360a99fd36a1f00dd3435dae603d71d5e",
            "0bacf7aec59c10f20b1b741c41cddf4e78b95016bee926210914ea034bdc8347",
        ),
        (
            "capture_result_channel",
            1,
            {"start": 39, "end": 40},
            "345b49f9bfa0d79f0c7e715bcf0a4137e30aaa2cf70d3e93356cc3b8b7ebd35b",
            "345b49f9bfa0d79f0c7e715bcf0a4137e30aaa2cf70d3e93356cc3b8b7ebd35b",
            "fe1e2d1e54b98ec135f9614983eb12ccc84f92eb14d085e19c4b529d7617302a",
        ),
        (
            "mixed_method",
            13,
            {"start": 40, "end": 41},
            "f7435200eebd0a50404044633019fcd64cbdbbf8e941da01d56567ac9f633337",
            "a099a26ae5cc75c855ebc12db54c7398aa6ea9a187cf63b1f06419156503eff2",
            "a357d9bb679c2bd8395ec4dfe93067c64bfb844dd31ed940654513305d327c32",
        ),
        (
            "mixed_statement",
            21,
            {"start": 105, "end": 106},
            "f7435200eebd0a50404044633019fcd64cbdbbf8e941da01d56567ac9f633337",
            "558af9310d0544d910b1409c3c0887ab322978a908666874bdb633fff9ea241b",
            "ef01267eddce9ce59e2ac9969dc6553277f57ecac2f8265baf221dc313e98656",
        ),
    ),
)
def test_exact_acceptance_coordinates_and_hash_fences_are_literal(
    case: str,
    column: int,
    span: dict[str, int],
    visible_sha256: str,
    executed_sha256: str,
    source_map_sha256: str,
    scenario_factory,
) -> None:
    """Break caught: point mapping widens spans or crosses an artifact fence."""
    result = scenario_factory(case).run()
    location = result.public_diagnostic["visible_location"]

    assert location["column"] == column
    assert location["span"] == span
    assert result.source_unit.source_sha256 == visible_sha256
    assert result.executed.artifact.source_sha256 == executed_sha256
    assert result.executed.source_map_sha256 == source_map_sha256


@pytest.mark.parametrize(
    (
        "case",
        "line_ending_kind",
        "line",
        "column",
        "span",
        "visible_sha256",
        "executed_sha256",
        "source_map_sha256",
    ),
    (
        (
            "lf_nonbmp",
            "lf",
            2,
            13,
            {"start": 26, "end": 27},
            "ad532c34236f369af9a2951bcc755c85b6be588e401858ae098a627e5ba98a0f",
            "fb4803ddc5c38e63f037f61bb8e6b50f343955b0ad22be95aa2100e32b7c0514",
            "c1ea3a1f447688ff23ffe6c63a1a8dbdb02d2fffb6957d22853ac707497c985a",
        ),
        (
            "crlf_nonbmp",
            "crlf",
            2,
            13,
            {"start": 27, "end": 28},
            "fd127c3ef185152ecf6223e24cdf596f232d681dace5dac509c1d31bffdaf925",
            "881c9e12f8cbfe305a9fe587daa32d67e40bd4503fd46b63b6d5112bd6689060",
            "c094e717fd9228c1bc4f380673d8103853f9fc89dd76e50ec8f702c277dceac7",
        ),
        (
            "multiline_repeated",
            "lf",
            3,
            5,
            {"start": 31, "end": 32},
            "79f15ad497126d11922d02b152cd39d780871be7c39f5737cbabdf230dbd1b8b",
            "cd03d5b10fc7bfc72c8fed055c902c41a05d8d907eea094d46dcf1b23539306f",
            "da46df76f4988f9913478a81c4b1bcecf01537717aeda7c7c11ef8d6edd8ec68",
        ),
    ),
)
def test_line_endings_unicode_multiline_and_repeated_fragments_are_code_point_exact(
    case: str,
    line_ending_kind: str,
    line: int,
    column: int,
    span: dict[str, int],
    visible_sha256: str,
    executed_sha256: str,
    source_map_sha256: str,
    scenario_factory,
) -> None:
    """Break caught: UTF-16 units or newline normalization shift visible spans."""
    result = scenario_factory(case).run()
    location = result.public_diagnostic["visible_location"]

    assert location == {"line": line, "column": column, "span": span}
    assert result.executed.artifact.line_ending_kind == line_ending_kind
    assert result.source_unit.source_sha256 == visible_sha256
    assert result.executed.artifact.source_sha256 == executed_sha256
    assert result.executed.source_map_sha256 == source_map_sha256


@pytest.mark.parametrize(
    (
        "case",
        "confidence",
        "related_span",
        "synthetic_region",
        "visible_sha256",
        "executed_sha256",
        "source_map_sha256",
    ),
    (
        (
            "nearest",
            "nearest",
            {"start": 0, "end": 5},
            None,
            "7b05cd84c72886168cfb4e0bd74a6a1f1fc50a481aeaee6aa6faa61840ac7f73",
            "7a070f516a059415206ecdbb6c5d14066535097d09be055c71009e3ed3259a8c",
            "c5a2258f29b57d681c2624435c8c468bc11095b5c51966cba0e64322a8037f83",
        ),
        (
            "synthetic_wrapper",
            "synthetic",
            {"start": 0, "end": 9},
            "message_collector_try",
            "13ac8b858884aaa085d253571232f942553c7432898073c9db49bada08e0dace",
            "6ffd6751d18b4adfd14ee2f6ddc1d0b3fe2299a819cd24584af53a3d139b2e8b",
            "ae551d95000ef448c8aa92bbe23e4aca3d16c374ed5c8e21f3407ac50f01fee3",
        ),
    ),
)
def test_nearest_and_synthetic_diagnostics_never_invent_visible_positions(
    case: str,
    confidence: str,
    related_span: dict[str, int],
    synthetic_region: str | None,
    visible_sha256: str,
    executed_sha256: str,
    source_map_sha256: str,
    scenario_factory,
) -> None:
    """Break caught: non-exact relations are promoted to fabricated coordinates."""
    result = scenario_factory(case).run()
    public = result.public_diagnostic

    assert public["mapping_confidence"] == confidence
    assert public["visible_location"] is None
    assert public["related_visible_span"] == related_span
    assert public["synthetic_region"] == synthetic_region
    assert result.source_unit.source_sha256 == visible_sha256
    assert result.executed.artifact.source_sha256 == executed_sha256
    assert result.executed.source_map_sha256 == source_map_sha256


def test_unknown_host_coordinates_never_borrow_a_visible_or_related_position(
    scenario_factory,
) -> None:
    """Break caught: host-module coordinates are interpreted as executed BSL."""
    result = scenario_factory("main_result_channel").run()
    unknown = remap_platform_diagnostic(
        parse_platform_diagnostic("{ОбщийМодуль.Сервис(1,1)}: Ошибка"),
        result.executed,
        stage=DiagnosticStage.EXECUTION,
        visible_source_context=VisibleSourceContext(
            {result.source_unit: result.source}
        ),
    )
    public = privacy.diagnostic_to_public_wire(unknown)

    assert public["mapping_confidence"] == "unknown"
    assert public["visible_location"] is None
    assert public["related_visible_span"] is None
    assert public["synthetic_region"] is None
    assert unknown.lowered_location is None


def test_main_failure_keeps_the_next_safe_run_available_with_exact_visible_line() -> None:
    """Break caught: diagnostic processing strands MAIN in an unusable state."""
    from test_prototype_runtime import SERVICE, ScriptedSession

    source = "// 1\n// 2\n// 3\n// 4\n// 5\nРезультат = 1 / 0;"
    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL,
        "acceptance-main-runtime",
        1,
        source_sha256(source),
    )
    session = ScriptedSession(
        (SERVICE, SERVICE),
        completion_errors=(
            "{<Неизвестный модуль>(1,15)}: Деление на ноль",
            "",
        ),
    )
    controller = PrototypeRuntimeController(session, SERVICE)
    api = PrototypeRuntimeApi(controller)

    failed = api.execute_bsl(source, source_unit=unit)
    safe_source = "Результат = 1;"
    recovered = api.execute_bsl(
        safe_source,
        source_unit=SourceUnitRef(
            SourceUnitKind.NOTEBOOK_CELL,
            "acceptance-main-runtime-next",
            2,
            source_sha256(safe_source),
        ),
    )

    assert failed.succeeded is False
    assert failed.diagnostic is not None
    assert failed.diagnostic.mapping_confidence.value == "exact"
    assert failed.diagnostic.visible_location is not None
    assert (
        failed.diagnostic.visible_location.line,
        failed.diagnostic.visible_location.column,
        failed.diagnostic.visible_location.span.start,
        failed.diagnostic.visible_location.span.end,
    ) == (6, 15, 39, 40)
    assert recovered.succeeded is True
    assert recovered.operation_id == 2
    assert controller.state is OperationState.COMPLETED


def test_capture_failure_stays_paused_restores_workspace_and_sends_no_continue() -> None:
    """Break caught: failed CAPTURE evaluation resumes or clears the paused fence."""
    from test_prototype_runtime import (
        CAPTURE_A,
        SERVICE,
        ScriptedSession,
        evaluation,
        workspace_calls,
    )

    session = ScriptedSession(
        (CAPTURE_A,),
        capture_evaluations=(
            evaluation(
                "Ошибка",
                "boom",
                error="{<Неизвестный модуль>(2,25)}: Деление на ноль",
            ),
        ),
    )
    controller = PrototypeRuntimeController(session, SERVICE)
    api = PrototypeRuntimeApi(controller, capture_points=(CAPTURE_A,))
    captured = api.execute_bsl("Результат = Capture();")
    assert captured.kind is RuntimeReplyKind.CAPTURED
    continue_calls_before = session.continue_count
    source = (
        "e1cRuntimeКонтекстОтладки.Счётчик = 1;\n"
        "РезультатИнструкции = 1 / 0;"
    )

    failed = api.execute_bsl(
        source,
        source_unit=SourceUnitRef(
            SourceUnitKind.NOTEBOOK_CELL,
            "acceptance-capture-runtime",
            1,
            source_sha256(source),
        ),
    )

    assert failed.succeeded is False
    assert failed.state is OperationState.CAPTURED
    assert controller.state is OperationState.CAPTURED
    assert failed.diagnostic is not None
    assert failed.diagnostic.mapping_confidence.value == "exact"
    assert failed.diagnostic.visible_location is not None
    assert failed.diagnostic.visible_location.line == 2
    assert session.continue_count == continue_calls_before
    assert workspace_calls(session)[-2:] == [(SERVICE,), (SERVICE, CAPTURE_A)]


def test_jupyter_and_agent_share_public_visible_semantics_without_source_leakage(
    tmp_path: Path,
    scenario_factory,
) -> None:
    """Break caught: frontend projections disagree or copy business source."""
    result = scenario_factory("message_argument").run()
    reply = RuntimeReply(
        RuntimeReplyKind.MAIN_COMPLETED,
        41,
        OperationState.FAILED,
        error="RAW rdbg pid=9182 token=private-fallback",
        succeeded=False,
        diagnostic=result.diagnostic,
    )
    displayed = _display_reply(
        reply,
        NotebookDisplayConfig.presentation(),
        visible_source=result.source,
        source_unit=result.source_unit,
    )

    registry = OperationRegistry(tmp_path)
    release = Event()
    holder: list[str] = []
    provenance = OperationExecutionProvenance(
        visible_source_sha256=result.source_unit.source_sha256,
        executed_source_sha256=result.executed.artifact.source_sha256,
        source_map_sha256=result.executed.source_map_sha256,
        mode="main",
    )

    def execute() -> BackendExecution:
        assert release.wait(1)
        registry.set_execution_provenance(holder[0], provenance)
        return BackendExecution(
            AgentOperationState.FAILED,
            (),
            False,
            "ready",
            failure_stage="execution",
            diagnostic=result.diagnostic,
            state_changed=StateChanged.NO,
        )

    submitted = registry.submit(
        {
            "operation_kind": "code_run",
            "runtime_id": "runtime-acceptance",
            "runtime_generation": 1,
            "code_id": result.source_unit.unit_id,
            "revision": 1,
            "source_sha256": result.source_unit.source_sha256,
            "inputs_sha256": "1" * 64,
        },
        execute,
    )
    holder.append(submitted.operation_id)
    release.set()
    assert (
        registry.wait(submitted.operation_id, timeout_s=2).state
        is AgentOperationState.FAILED
    )
    agent_view = OperationViewProjector(registry, lambda _proxy: None).project(
        submitted.operation_id
    )
    agent_wire = to_wire(agent_view)
    agent_diagnostic = agent_wire["failure"]["diagnostic"]
    jupyter_diagnostic = displayed.payload["diagnostic"]

    expected_public_diagnostic = {
        "diagnostic_id": (
            "656563f3a836e8a8002ef35919f7d537fc1a1172cc005e05c0db5dd5378f019d"
        ),
        "stage": "execution",
        "mapping_confidence": "exact",
        "visible_location": {
            "line": 4,
            "column": 11,
            "span": {"start": 55, "end": 56},
        },
        "related_visible_span": None,
        "excerpt": None,
        "synthetic_region": None,
    }
    assert result.source[55:56] == "О"
    assert agent_diagnostic == expected_public_diagnostic
    assert jupyter_diagnostic == expected_public_diagnostic
    assert "excerpt=" not in displayed.text
    assert displayed.payload["source_unit"] == {
        "kind": "notebook_cell",
        "unit_id": "acceptance-message_argument",
        "revision": 1,
        "source_sha256": "ed69d8e16cbca333191c7107b713a814b1635222a336246cce859025066e1995",
    }
    public_surfaces = {
        "jupyter": displayed.payload,
        "agent": to_wire(agent_view),
    }
    _assert_no_source_material(
        public_surfaces,
        forbidden_sources=(
            result.source,
            result.lowered_text,
            result.executed.text,
        ),
    )
    encoded = json.dumps(public_surfaces, ensure_ascii=False)
    for forbidden in (
        "RAW rdbg",
        "9182",
        "private-fallback",
    ):
        assert forbidden not in encoded
    registry.shutdown()


def _seed_durable_failure(
    root: Path,
    result: _AcceptanceResult,
) -> tuple[str, OperationExecutionProvenance]:
    registry = OperationRegistry(root)
    release = Event()
    holder: list[str] = []
    provenance = OperationExecutionProvenance(
        visible_source_sha256=result.source_unit.source_sha256,
        executed_source_sha256=result.executed.artifact.source_sha256,
        source_map_sha256=result.executed.source_map_sha256,
        mode="main",
    )

    def execute() -> BackendExecution:
        assert release.wait(1)
        operation_id = holder[0]
        registry.set_execution_provenance(operation_id, provenance)
        registry.record_diagnostic(
            result.diagnostic,
            excerpt="/",
            operation_id=operation_id,
        )
        return BackendExecution(
            AgentOperationState.FAILED,
            (),
            False,
            "ready",
            failure_stage="execution",
            diagnostic=result.diagnostic,
            state_changed=StateChanged.NO,
        )

    submitted = registry.submit(
        {
            "operation_kind": "code_run",
            "runtime_id": "runtime-durable",
            "runtime_generation": 1,
            "code_id": result.source_unit.unit_id,
            "revision": result.source_unit.revision,
            "source_sha256": result.source_unit.source_sha256,
            "inputs_sha256": "2" * 64,
        },
        execute,
    )
    holder.append(submitted.operation_id)
    release.set()
    assert (
        registry.wait(submitted.operation_id, timeout_s=2).state
        is AgentOperationState.FAILED
    )
    registry.shutdown()
    return submitted.operation_id, provenance


def test_operation_restart_rehydrates_hash_only_reference_and_keeps_compact_view_lazy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario_factory,
) -> None:
    """Break caught: restart loses diagnostics or compact views open private data."""
    result = scenario_factory("main_result_channel").run()
    operation_id, provenance = _seed_durable_failure(tmp_path, result)
    public_path = tmp_path / ".runtime" / "agent-service" / "operations.jsonl"
    private_path = (
        tmp_path / ".runtime" / "agent-service" / "diagnostics.private.jsonl"
    )
    events = [
        json.loads(line)
        for line in public_path.read_text(encoding="utf-8").splitlines()
    ]
    references = [event for event in events if event["event"] == "private_diagnostic_ref"]

    assert len(references) == 1
    assert set(references[0]) == {"cursor", "event", "operation_id", "reference"}
    reference = references[0]["reference"]
    assert set(reference) == {"diagnostic_id", "content_integrity_sha256"}
    assert reference["diagnostic_id"] == result.diagnostic.diagnostic_id
    assert len(reference["content_integrity_sha256"]) == 64
    private_records = [
        json.loads(line)
        for line in private_path.read_text(encoding="utf-8").splitlines()
    ]
    forbidden_sources = (
        result.source,
        result.lowered_text,
        result.executed.text,
    )
    _assert_no_source_material(
        events,
        forbidden_sources=forbidden_sources,
    )
    _assert_no_source_material(
        private_records,
        forbidden_sources=forbidden_sources,
    )

    recovered = OperationRegistry(tmp_path)
    monkeypatch.setattr(
        recovered,
        "_ensure_private_diagnostics_loaded_locked",
        lambda: (_ for _ in ()).throw(
            AssertionError("compact operation view opened private diagnostics")
        ),
    )
    compact = OperationViewProjector(recovered, lambda _proxy: None).project(
        operation_id
    )
    compact_wire = to_wire(compact)
    _assert_no_source_material(
        compact_wire,
        forbidden_sources=forbidden_sources,
    )

    assert compact.state is AgentOperationState.FAILED
    assert compact.execution_provenance == provenance
    assert compact.failure["diagnostic"]["visible_location"] == {
        "line": 6,
        "column": 1,
        "span": {"start": 25, "end": 26},
    }
    assert "private_diagnostic_ref" not in json.dumps(compact_wire)
    assert reference["content_integrity_sha256"] not in json.dumps(compact_wire)
    recovered.shutdown()

    expert_registry = OperationRegistry(tmp_path)
    expert = expert_registry.expert_diagnostic(
        operation_id,
        result.diagnostic.diagnostic_id,
    )
    assert expert is not None
    _assert_no_source_material(
        expert,
        forbidden_sources=forbidden_sources,
    )
    assert expert["excerpt"] == "/"
    assert expert["execution_artifact_sha256"] == (
        "dc3f53f39d08cce70e6d62136f620a1360a99fd36a1f00dd3435dae603d71d5e"
    )
    assert expert["source_map_sha256"] == (
        "0bacf7aec59c10f20b1b741c41cddf4e78b95016bee926210914ea034bdc8347"
    )
    assert "private_diagnostic_ref" not in expert
    assert reference["content_integrity_sha256"] not in json.dumps(expert)
    expert_registry.shutdown()


@pytest.mark.parametrize("mutation", ("malformed", "conflicting"))
def test_rejected_private_record_mutations_never_reflect_into_public_or_expert_views(
    tmp_path: Path,
    mutation: str,
    scenario_factory,
) -> None:
    """Break caught: rejected private records are reflected after restart."""
    result = scenario_factory("main_result_channel").run()
    operation_id, _ = _seed_durable_failure(tmp_path, result)
    private_path = (
        tmp_path / ".runtime" / "agent-service" / "diagnostics.private.jsonl"
    )
    sentinel = f"REJECTED-{mutation}-RDBG-pid-9182-token-private"
    if mutation == "malformed":
        private_path.write_text(
            '{"diagnostic_id":"' + sentinel + '"\n',
            encoding="utf-8",
        )
    else:
        original_line = private_path.read_text(encoding="utf-8").strip()
        conflicting = json.loads(original_line)
        conflicting["excerpt"] = sentinel
        content = {
            key: value
            for key, value in conflicting.items()
            if key != "content_integrity_sha256"
        }
        conflicting["content_integrity_sha256"] = sha256(
            json.dumps(
                content,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        private_path.write_text(
            original_line
            + "\n"
            + json.dumps(conflicting, ensure_ascii=False, separators=(",", ":"))
            + "\n",
            encoding="utf-8",
        )

    recovered = OperationRegistry(tmp_path)
    compact = OperationViewProjector(recovered, lambda _proxy: None).project(
        operation_id
    )
    expert = recovered.expert_diagnostic(operation_id, result.diagnostic.diagnostic_id)
    encoded = json.dumps(
        {"compact": to_wire(compact), "expert": expert},
        ensure_ascii=False,
    )
    _assert_no_source_material(
        {"compact": to_wire(compact), "expert": expert},
        forbidden_sources=(
            result.source,
            result.lowered_text,
            result.executed.text,
        ),
    )

    assert compact.failure["diagnostic"]["diagnostic_id"] == (
        result.diagnostic.diagnostic_id
    )
    assert expert is None
    assert sentinel not in encoded
    assert result.source not in encoded
    recovered.shutdown()


def test_source_map_manifest_and_diagnostic_wires_never_contain_full_sources(
    scenario_factory,
) -> None:
    """Break caught: map serialization publishes visible or generated BSL."""
    result = scenario_factory("message_argument").run()
    manifest = result.executed.source_map.to_manifest()
    public = result.public_diagnostic
    expert = privacy.diagnostic_to_expert_wire(result.diagnostic)
    _assert_no_source_material(
        {
            "source_map_manifest": manifest,
            "public_response": public,
            "expert_response": expert,
        },
        forbidden_sources=(
            result.source,
            result.lowered_text,
            result.executed.text,
        ),
    )
    assert "<redacted>" in repr(result.executed)
    manifest_text = json.dumps(manifest, ensure_ascii=False, sort_keys=True)
    assert result.source_unit.source_sha256 in manifest_text
    assert result.executed.artifact.source_sha256 in manifest_text
    assert result.executed.source_map_sha256 not in manifest_text


@pytest.mark.parametrize("line_ending", ("\n", "\r\n"))
def test_delta_lowering_compile_line_only_and_runtime_two_frame_diagnostics_match_full(
    line_ending: str,
) -> None:
    """Break caught: reused segments must map canonical Worker frames identically."""
    def source(value: str) -> str:
        return (
            "Функция Первый() Экспорт\n"
            f"    Возврат {value};\n"
            "КонецФункции\n"
            "Функция Второй()\n"
            "    Возврат 2;\n"
            "КонецФункции\n"
        ).replace("\n", line_ending)

    catalog = CommonModuleCatalogSnapshot.create(
        profile="server-test",
        preprocessor_profile="server",
        revision=1,
        modules=(CommonModuleDescriptor("МодульА", CommonModuleScope.SERVER),),
    )

    def unit(text: str, revision: int) -> WorkerModuleUnit:
        reference = SourceUnitRef(
            SourceUnitKind.MODULE,
            "МодульА",
            revision,
            source_sha256(text),
        )
        return WorkerModuleUnit(
            "МодульА",
            "module",
            revision,
            mapped_visible_source(text, reference),
        )

    target = PythonParserTarget.from_generated()
    previous = build_full_worker_semantic_snapshot(unit(source("1"), 1), catalog, target)
    candidate = unit(source("100000"), 2)
    merged = try_build_worker_semantic_delta(previous, candidate, catalog, target)
    assert merged is not None
    assert merged.delta_evidence is not None
    delta_lowered = module_delta.lower_worker_module_delta(
        previous,
        merged.analysis,
        merged.delta_evidence,
    )
    full = build_full_worker_semantic_snapshot(
        candidate,
        catalog,
        PythonParserTarget.from_generated(),
    ).lowered
    assert delta_lowered is not None

    manifest = "c" * 64
    registrations = (
        "OnecRuntime_aaaaaaaa_aaaaaaaaaaaaaaaa",
        "OnecRuntime_bbbbbbbb_bbbbbbbbbbbbbbbb",
    )

    def artifacts(mapped: MappedSource) -> tuple[WorkerDiagnosticArtifact, ...]:
        reference = candidate.mapped_source.source_map.segments[0].origin_ref
        assert isinstance(reference, SourceUnitRef)
        context = VisibleSourceContext({reference: candidate.mapped_source.text})
        return tuple(
            WorkerDiagnosticArtifact(
                logical_name="МодульА",
                revision=2,
                artifact_sha256=character * 64,
                registration_name=registration,
                manifest_sha256=manifest,
                source_map_sha256=mapped.source_map_sha256,
                mapped_source=mapped,
                visible_source_context=context,
            )
            for character, registration in zip(("a", "b"), registrations, strict=True)
        )

    line_index = LineIndex(full.mapped_source.text)
    callee_offset = full.mapped_source.text.index("Возврат 100000")
    caller_offset = full.mapped_source.text.index("Возврат 2")
    callee_line, callee_column = line_index.offset_to_line_column(callee_offset)
    caller_line, caller_column = line_index.offset_to_line_column(caller_offset)
    compile_line_only = parse_platform_diagnostic(
        "{ВнешняяОбработка."
        f"{registrations[0]}.МодульОбъекта({callee_line})}}: compile "
        "[ОшибкаКомпиляцииВстроенногоЯзыка]"
    )
    runtime_two_frame = parse_platform_diagnostic(
        "{ВнешняяОбработка."
        f"{registrations[0]}.МодульОбъекта({callee_line},{callee_column})}}: callee\n"
        "{ВнешняяОбработка."
        f"{registrations[1]}.МодульОбъекта({caller_line},{caller_column})}}: caller"
    )

    for parsed in (compile_line_only, runtime_two_frame):
        actual = remap_worker_runtime_diagnostic(
            parsed,
            pinned_manifest_sha256=manifest,
            pinned_artifacts=artifacts(delta_lowered.mapped_source),
        )
        expected = remap_worker_runtime_diagnostic(
            parsed,
            pinned_manifest_sha256=manifest,
            pinned_artifacts=artifacts(full.mapped_source),
        )
        assert actual == expected
        assert actual.worker_frames
        assert all(
            frame.mapping_confidence.value == "exact"
            for frame in actual.worker_frames
        )


def test_resolved_reload_diagnostics_anchor_compact_dependency_regions() -> None:
    """Break caught: compact reload insertions must retain useful diagnostics."""
    from onec_runtime.bsl.diagnostics import MappingConfidence
    from onec_runtime.bsl.module_universe import lower_resolved_worker_module
    from onec_runtime.bsl.worker_dependency_resolver import (
        resolve_worker_dependencies,
    )
    from onec_runtime.bsl.full_ast_worker_projection import parse_full_ast_module

    source = "Процедура P()\n    Альфа.X();\nКонецПроцедуры"
    unit_ref = SourceUnitRef(
        SourceUnitKind.MODULE,
        "Тест",
        1,
        source_sha256(source),
    )
    unit = WorkerModuleUnit(
        "Тест",
        "module",
        1,
        mapped_visible_source(source, unit_ref),
    )
    catalog = CommonModuleCatalogSnapshot.create(
        profile="server-test",
        preprocessor_profile="server",
        revision=1,
        modules=(CommonModuleDescriptor("Альфа", CommonModuleScope.SERVER),),
    )
    plan = resolve_worker_dependencies(parse_full_ast_module(source), catalog)
    mapped = lower_resolved_worker_module(unit, plan).mapped_source
    method_span = plan.methods[0].source.declaration_span
    context = VisibleSourceContext({unit_ref: source})

    def remap(offset: int):  # type: ignore[no-untyped-def]
        line, column = LineIndex(mapped.text).offset_to_line_column(offset)
        parsed = parse_platform_diagnostic(
            f"{{<Неизвестный модуль>({line},{column})}}: Ошибка"
        )
        return remap_platform_diagnostic(
            parsed,
            mapped,
            stage=DiagnosticStage.EXECUTION,
            visible_source_context=context,
        )

    field = remap(mapped.text.index("__OnecDependency"))
    declaration = remap(mapped.text.index("Перем Альфа"))
    initializer = remap(mapped.text.index("Альфа = __OnecDependency"))
    original = remap(mapped.text.rindex("Альфа.X"))

    assert field.mapping_confidence is MappingConfidence.SYNTHETIC
    assert field.related_visible_span == SourceSpan(0, 0)
    assert declaration.mapping_confidence is MappingConfidence.NEAREST
    assert declaration.related_visible_span == method_span
    assert initializer.mapping_confidence is MappingConfidence.NEAREST
    assert initializer.related_visible_span == method_span
    assert original.mapping_confidence is MappingConfidence.EXACT
    assert original.visible_location is not None
    assert original.visible_location.span.start == source.index("Альфа.X")
