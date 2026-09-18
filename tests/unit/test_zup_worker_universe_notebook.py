from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import json
import os
from pathlib import Path
from types import SimpleNamespace

import nbformat
from nbclient import NotebookClient
import pytest

import integration.zup_worker_universe_acceptance as zup_acceptance

from integration.zup_worker_universe_notebook import (
    EXPECTED_ERROR_TAGS,
    REQUIRED_CODE_TAGS,
    WorkerUniverseZupAcceptance,
    _MAIN_MIXED,
    build_notebook,
    serialize_notebook,
    verify_notebook,
)
from integration.jupyter_bsl_fixture.cells import (
    CAPTURE_MIXED_ERROR_RECOVERY_SOURCE,
    CAPTURE_MIXED_ERROR_SOURCE,
    CAPTURE_MIXED_SOURCE,
)
from integration.zup_worker_universe_acceptance import (
    PHASES,
    REFERENCE_INFOBASE_IDENTITY_SHA256,
    REFERENCE_EXTENSION_SHA256,
    REFERENCE_PLATFORM_SHA256,
    REFERENCE_PLATFORM_VERSION,
    REFERENCE_SOURCE_SHA256,
    IncrementalAccounting,
    ReferenceObservation,
    ZupStaticPreflight,
    _run_verified_live,
    build_profile_catalog,
    build_compact_evidence,
    nearest_rank,
    reference_compatibility_inventory,
    run_worker_universe_zup_acceptance,
    verify_compact_evidence,
    verify_worker_universe_zup_evidence,
)
from onec_runtime.bsl.diagnostics import (
    DiagnosticStage,
    MappingConfidence,
    NormalizedDiagnostic,
    VisibleSourceLocation,
    WorkerRuntimeFrameDiagnostic,
)
from onec_runtime.bsl.notebook_cells import split_notebook_cell
from onec_runtime.bsl.parser_target import PythonParserTarget
from onec_runtime.bsl.source_maps import SourceSpan, SourceUnitKind, SourceUnitRef
from onec_runtime.errors import (
    BslExecutionError,
    ProtocolError,
    StaleWorkerGeneration,
)
from onec_runtime.performance_profile import PhaseRecorder
from onec_runtime.runtime_models import (
    OperationState,
    RuntimeNamespaceSnapshot,
    RuntimeReply,
    RuntimeReplyKind,
)
from onec_runtime.worker_universe import WorkerGenerationHandle


def _code_cells(notebook: nbformat.NotebookNode) -> list[nbformat.NotebookNode]:
    return [cell for cell in notebook.cells if cell.cell_type == "code"]


def _by_tag(notebook: nbformat.NotebookNode) -> dict[str, nbformat.NotebookNode]:
    return {
        cell.metadata["tags"][0]: cell
        for cell in _code_cells(notebook)
    }


def test_builder_is_deterministic_clean_and_reader_ordered() -> None:
    first = build_notebook()
    second = build_notebook()

    assert serialize_notebook(first) == serialize_notebook(second)
    assert [cell.metadata["tags"][0] for cell in _code_cells(first)] == list(
        REQUIRED_CODE_TAGS
    )
    assert all(cell.execution_count is None for cell in _code_cells(first))
    assert all(cell.outputs == [] for cell in _code_cells(first))
    identifiers = [cell.id for cell in first.cells]
    assert all(identifiers)
    assert len(identifiers) == len(set(identifiers))

    markdown = [cell.source for cell in first.cells if cell.cell_type == "markdown"]
    assert [
        "## Goal & Setup",
        "## MAIN acceptance",
        "## CAPTURE acceptance",
        "## Negative diagnostics & lifecycle",
        "## Performance results",
        "## Cleanup & validation status",
    ] == [source.splitlines()[0] for source in markdown[1:]]


def test_notebook_covers_main_capture_diagnostics_lifecycle_and_performance() -> None:
    cells = _by_tag(build_notebook())

    assert "Процедура" in cells["main-procedure"].source
    assert "Функция" in cells["main-function"].source
    main_mixed = split_notebook_cell(PythonParserTarget.from_generated(), _MAIN_MIXED)
    assert main_mixed.worker_source and main_mixed.statement_source
    assert "privacy" in cells["main-proxy"].source
    assert "same_method" in cells["main-same-methods"].source

    assert "Процедура" in cells["capture-procedure"].source
    assert "Функция" in cells["capture-function"].source
    capture_sources = {
        "capture-mixed-success": CAPTURE_MIXED_SOURCE,
        "capture-mixed-error": CAPTURE_MIXED_ERROR_SOURCE,
    }
    for tag in ("capture-mixed-success", "capture-mixed-error"):
        split = split_notebook_cell(
            PythonParserTarget.from_generated(),
            capture_sources[tag],
        )
        assert split.worker_source and split.statement_source
    recovery = split_notebook_cell(
        PythonParserTarget.from_generated(),
        CAPTURE_MIXED_ERROR_RECOVERY_SOURCE,
    )
    assert not recovery.worker_source and recovery.statement_source
    assert "FixtureMixedCaptureAfterError" in recovery.statement_source
    assert "serialization" in cells["capture-proxy"].source
    assert "lifecycle" in cells["capture-pin-promotions"].source

    assert "assert_compile_diagnostic" in cells["diagnostic-compile"].source
    assert "assert_runtime_diagnostic" in cells["diagnostic-runtime"].source
    assert "assert_stale_handle" in cells["stale-handle"].source
    assert "assert_failed_dependency" in cells["failed-dependency"].source
    assert "iterations=ITERATIONS" in cells["main-load"].source
    assert "warmup_iterations=WARMUP_ITERATIONS" in cells["main-load"].source
    assert "2_500" in cells["performance-results"].source
    assert "onec-worker-universe-zup-acceptance-v3" in cells["performance-results"].source
    assert "catalog_setup_ms" in cells["performance-results"].source
    assert "catalog_extension" in cells["performance-results"].source
    assert "active_generation_unchanged" in cells["performance-results"].source
    assert "runtime_dispatches" in cells["performance-results"].source
    assert "incompatible_instrumentation" not in cells["performance-results"].source
    assert "atomic_promotion_phase_split_unavailable" not in serialize_notebook(
        build_notebook()
    )
    assert cells["capture-mixed-error"].metadata["expected_error"] is True
    assert EXPECTED_ERROR_TAGS == frozenset({"capture-mixed-error"})


def test_real_capture_worker_reload_is_visible_only_to_the_next_operation() -> None:
    exercise = getattr(
        zup_acceptance,
        "_exercise_capture_reload_visibility",
        None,
    )
    assert exercise is not None
    original_generation = object()
    promoted_generation = object()
    capture_location = object()
    worker_source = "Процедура Task10NextOperation()\nКонецПроцедуры"
    probe_source = "РезультатИнструкции = Task10NextOperation();"

    class FakeRuntimeApi:
        def __init__(self) -> None:
            self.worker_generation_handle: object | None = original_generation
            self.resume_results = [7101, 7102]

        def resume_capture(self) -> RuntimeReply:
            result = self.resume_results.pop(0)
            session.state = OperationState.COMPLETED
            return RuntimeReply(
                RuntimeReplyKind.MAIN_COMPLETED,
                result,
                OperationState.COMPLETED,
                result=result,
            )

    class FakeSession:
        def __init__(self) -> None:
            self.runtime_api = FakeRuntimeApi()
            self.state = OperationState.IDLE
            self.sources: list[str] = []

        def status(self) -> object:
            return SimpleNamespace(
                state=self.state,
                worker_generation=self.runtime_api.worker_generation_handle,
            )

        def resume_capture(self) -> RuntimeReply:
            return self.runtime_api.resume_capture()

        def execute_bsl(self, source: str) -> RuntimeReply:
            self.sources.append(source)
            if "СинтетическийCapture" in source:
                self.state = OperationState.CAPTURED
                return RuntimeReply(
                    RuntimeReplyKind.CAPTURED,
                    len(self.sources),
                    OperationState.CAPTURED,
                    location=capture_location,  # type: ignore[arg-type]
                )
            if source == worker_source:
                self.runtime_api.worker_generation_handle = promoted_generation
                return RuntimeReply(
                    RuntimeReplyKind.WORKER_LOADED,
                    len(self.sources),
                    OperationState.CAPTURED,
                    result=promoted_generation,
                )
            if source == probe_source:
                return RuntimeReply(
                    RuntimeReplyKind.CAPTURE_CELL,
                    len(self.sources),
                    OperationState.CAPTURED,
                    result=17,
                )
            raise AssertionError("unexpected semantic source")

    session = FakeSession()

    exercise(
        session,
        capture_location,
        worker_source=worker_source,
        probe_source=probe_source,
        probe_expected=17,
        phase=71,
    )

    assert session.status().state is OperationState.COMPLETED
    assert session.runtime_api.worker_generation_handle is promoted_generation
    assert session.sources[1:] == [
        worker_source,
        session.sources[2],
        probe_source,
    ]
    assert "СинтетическийCapture" in session.sources[0]
    assert "СинтетическийCapture" in session.sources[2]


def test_capture_measurement_uses_public_suspension_and_terminal_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    location = object()
    monkeypatch.setattr(
        zup_acceptance, "_synthetic_capture_location", lambda _path: location
    )

    class FakeSession:
        def __init__(self) -> None:
            self.config = SimpleNamespace(runtime=SimpleNamespace(runtime_dir=Path(".")))
            self.state = OperationState.IDLE
            self.points = ()

        def configure_capture_points(self, points):
            self.points = points

        def execute_bsl(self, source):
            assert "СинтетическийCapture" in source
            self.state = OperationState.CAPTURED
            return RuntimeReply(
                RuntimeReplyKind.CAPTURED, 1, self.state, location=location
            )

        def status(self):
            return SimpleNamespace(state=self.state)

        def resume_capture(self):
            assert self.state is OperationState.CAPTURED
            self.state = OperationState.COMPLETED
            return RuntimeReply(
                RuntimeReplyKind.MAIN_COMPLETED, 1, self.state,
                result="1719|1719",
            )

    session = FakeSession()
    with zup_acceptance._controlled_capture_reload_context(session):
        assert session.points == (location,)
        assert session.status().state is OperationState.CAPTURED
    assert session.status().state is OperationState.COMPLETED


def test_all_bsl_cells_are_accepted_by_the_generated_product_parser() -> None:
    parser = PythonParserTarget.from_generated()

    for cell in _code_cells(build_notebook()):
        if cell.source.startswith("%%bsl\n"):
            split_notebook_cell(parser, cell.source.removeprefix("%%bsl\n"))


def test_validator_rejects_outputs_reordering_and_machine_private_data(
    tmp_path: Path,
) -> None:
    notebook = build_notebook()
    path = tmp_path / "acceptance.ipynb"
    path.write_text(serialize_notebook(notebook), encoding="utf-8")
    verify_notebook(path, allow_outputs=False)

    _by_tag(notebook)["parameters"].outputs = [
        nbformat.v4.new_output("display_data", data={"text/plain": "unexpected"})
    ]
    path.write_text(serialize_notebook(notebook), encoding="utf-8")
    with pytest.raises(ProtocolError, match="contains outputs"):
        verify_notebook(path, allow_outputs=False)

    notebook = build_notebook()
    first, second = notebook.cells[1], notebook.cells[2]
    notebook.cells[1], notebook.cells[2] = second, first
    path.write_text(serialize_notebook(notebook), encoding="utf-8")
    with pytest.raises(ProtocolError, match="out of order"):
        verify_notebook(path, allow_outputs=False)

    notebook = build_notebook()
    _by_tag(notebook)["setup"].source += '\nprivate_root = r"C:\\\\private\\\\zup"'
    path.write_text(serialize_notebook(notebook), encoding="utf-8")
    with pytest.raises(ProtocolError, match="private or machine-specific"):
        verify_notebook(path, allow_outputs=False)


def test_notebook_has_no_live_claim_or_embedded_private_evidence() -> None:
    notebook = build_notebook()
    serialized = serialize_notebook(notebook)

    assert notebook.metadata["onec_worker_universe_acceptance"] == {
        "schema": "onec-worker-universe-zup-notebook-v1",
        "live_status": "UNVERIFIED",
    }
    assert "C:\\" not in serialized
    assert "1Cv8.1CD" not in serialized
    assert "ONEC_RUNTIME_USERNAME" not in serialized
    assert "ONEC_RUNTIME_PASSWORD" not in serialized
    assert "source_text" not in serialized
    assert "target_object" not in serialized


def test_checked_in_notebook_is_byte_equivalent_to_the_builder() -> None:
    workspace = Path(__file__).parents[2]
    path = workspace / "tests" / "fixtures" / "notebooks" / "zup-worker-universe-acceptance.ipynb"

    assert path.read_text(encoding="utf-8") == serialize_notebook(build_notebook())
    verify_notebook(path, allow_outputs=False)


def test_notebook_facade_reports_exact_offline_prerequisite_without_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "ONEC_RUNTIME_WORKSPACE",
        "ONEC_RUNTIME_PLATFORM_BIN",
        "ONEC_RUNTIME_INFOBASE",
        "ONEC_ZUP_SOURCE_ROOT",
    ):
        monkeypatch.delenv(name, raising=False)

    facade = WorkerUniverseZupAcceptance.from_environment()

    assert facade.verify_reference_target() == {
        "status": "incompatible_prerequisites",
        "sla_claimed": False,
        "compatibility_inventory": [
            {"code": "notebook_live_environment_missing", "count": 1}
        ],
        "validation_command": (
            "uv run python tools/build_zup_worker_universe_notebook.py --check"
        ),
    }
    assert facade.close() == {"status": "NOT_STARTED"}


def test_notebook_executes_top_to_bottom_without_fabricating_live_cleanup() -> None:
    notebook = build_notebook()
    workspace = Path(__file__).parents[2]

    executed = NotebookClient(
        notebook,
        timeout=60,
        kernel_name="python3",
    ).execute(cwd=str(workspace))

    cleanup = _by_tag(executed)["cleanup"].outputs[-1]["data"]["text/plain"]
    assert "NOT_STARTED" in cleanup


def test_notebook_facade_exact_preflight_runs_real_acceptance_and_uses_real_cleanup(
    tmp_path: Path,
) -> None:
    calls: list[tuple[object, Path, int, int]] = []
    config = object()
    source_root = tmp_path / "approved-source"
    source_root.mkdir()
    run_dir = tmp_path / "run"
    evidence = {
        "schema": "onec-worker-universe-zup-acceptance-v3",
        "status": "PASS",
        "sla_claimed": True,
        "catalog_setup_ms": 41.0,
        "modes": {"main": {}, "capture": {}},
        "gates": _pass_gates(),
        "cleanup": {"owned_processes": 0, "private_source_files": 0},
        "compatibility_inventory": [],
    }
    facade = WorkerUniverseZupAcceptance(
        config,
        source_root,
        preflight_inspector=lambda actual, root: ZupStaticPreflight(()),
        acceptance_runner=lambda actual, *, source_root, iterations,
        warmup_iterations: (
            calls.append((actual, source_root, iterations, warmup_iterations))
            or run_dir
        ),
        evidence_reader=lambda actual: evidence if actual == run_dir else {},
    )

    assert facade.verify_reference_target()["status"] == "READY"
    assert facade.run(iterations=60, warmup_iterations=3) == evidence
    assert facade.close() == evidence["cleanup"]
    assert calls == [(config, source_root, 60, 3)]


def _reference_observation() -> ReferenceObservation:
    return ReferenceObservation(
        platform_version=REFERENCE_PLATFORM_VERSION,
        platform_sha256=REFERENCE_PLATFORM_SHA256,
        infobase_identity_sha256=REFERENCE_INFOBASE_IDENTITY_SHA256,
        source_sha256=REFERENCE_SOURCE_SHA256,
        catalog_sha256="c" * 64,
        extension_sha256=REFERENCE_EXTENSION_SHA256,
        target_extension_current=True,
        session_fresh=True,
    )


def test_reference_preflight_requires_every_recorded_identity_and_fresh_session() -> None:
    exact = _reference_observation()

    assert reference_compatibility_inventory(exact) == []
    assert reference_compatibility_inventory(
        replace(exact, extension_sha256="e" * 64)
    ) == [{"code": "extension_identity_mismatch", "count": 1}]

    mismatched = ReferenceObservation(
        platform_version="8.3.27.9999",
        platform_sha256="a" * 64,
        infobase_identity_sha256="b" * 64,
        source_sha256={
            "КадровыйУчет": "c" * 64,
            "КадровыйУчетРасширенный": "d" * 64,
        },
        catalog_sha256="not-a-hash",
        extension_sha256="not-a-hash",
        target_extension_current=False,
        session_fresh=False,
    )

    assert reference_compatibility_inventory(mismatched) == [
        {"code": "platform_version_mismatch", "count": 1},
        {"code": "platform_identity_mismatch", "count": 1},
        {"code": "infobase_identity_mismatch", "count": 1},
        {"code": "source_identity_mismatch", "count": 2},
        {"code": "catalog_identity_invalid", "count": 1},
        {"code": "extension_identity_invalid", "count": 1},
        {"code": "target_extension_not_current", "count": 1},
        {"code": "stale_target_session", "count": 1},
    ]


def test_profile_catalog_resolves_only_approved_modules_by_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = (
        Path(__file__).parents[1]
        / "fixtures"
        / "onec"
        / "JupyterBslTestFixture"
    )
    before = tuple(
        sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
    )

    reads: list[Path] = []
    original_read_bytes = Path.read_bytes

    def observed_read_bytes(path: Path) -> bytes:
        reads.append(path)
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", observed_read_bytes)

    provider = zup_acceptance.SessionCommonModuleCatalog(
        root,
        profile="runtime-session-server-v1",
        preprocessor_profile="server",
    )
    catalog = provider.ensure_modules(
        (
            "JupyterBslFixtureCalleeServer",
            "JupyterBslFixtureCallerServer",
        )
    )

    assert len(catalog.sha256) == 64
    assert tuple(module.canonical_name for module in catalog.modules) == (
        "JupyterBslFixtureCalleeServer",
        "JupyterBslFixtureCallerServer",
    )
    assert all(module.scope.value in {"server", "client_server"} for module in catalog.modules)
    assert reads
    assert all(path.suffix.casefold() == ".xml" for path in reads)
    assert tuple(
        sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
    ) == before


def test_catalog_plus_source_admission_resolves_candidate_without_reading_its_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "approved-source"
    common_modules = source_root / "CommonModules"
    common_modules.mkdir(parents=True)
    names = (
        "КадровыйУчет",
        "КадровыйУчетРасширенный",
        "ВнешняяЗависимость",
        "Нетarget",
    )
    for index, name in enumerate(names, start=1):
        (common_modules / f"{name}.xml").write_text(
            _common_module_metadata(name),
            encoding="utf-8",
        )
        extension = common_modules / name / "Ext"
        extension.mkdir(parents=True)
        body = f"Возврат {index};"
        if name == "КадровыйУчет":
            body = "Возврат ВнешняяЗависимость.Fixture3();"
        (extension / "Module.bsl").write_text(
            f"Функция Fixture{index}() Экспорт\n    {body}\nКонецФункции",
            encoding="utf-8",
        )
    reads: list[Path] = []
    original_read_text = Path.read_text

    def observed_read_text(path: Path, *args, **kwargs) -> str:
        if path.suffix.casefold() == ".bsl":
            reads.append(path)
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", observed_read_text)
    bundle = zup_acceptance.admit_zup_source_bundle(source_root)
    units = zup_acceptance._worker_units(bundle)
    models = tuple(
        zup_acceptance.parse_full_ast_module(unit.mapped_source.text)
        for unit in units
    )
    monkeypatch.setattr(
        PythonParserTarget,
        "from_generated",
        lambda: (_ for _ in ()).throw(AssertionError("catalog parsed module source")),
    )
    snapshot = build_profile_catalog(source_root, models)

    assert [unit.name for unit in bundle.units] == [
        "КадровыйУчет",
        "КадровыйУчетРасширенный",
    ]
    assert {module.canonical_name for module in snapshot.modules} == {
        "ВнешняяЗависимость",
        "КадровыйУчет",
        "КадровыйУчетРасширенный",
    }
    analyses, inventory = zup_acceptance._admission_inventory(
        units,
        models,
        snapshot,
    )
    assert inventory == []
    assert [binding.target_module for binding in analyses[0].dependencies] == [
        "ВнешняяЗависимость"
    ]
    assert reads == [
        common_modules / "КадровыйУчет" / "Ext" / "Module.bsl",
        common_modules / "КадровыйУчетРасширенный" / "Ext" / "Module.bsl",
    ]


def _phase_samples(count: int = 60) -> dict[str, dict[str, list[float]]]:
    return {
        mode: {
            phase: [
                (
                    float(index + 10)
                    if phase == "end_to_end"
                    else float((phase_index + 1) / 10 + index / 100)
                )
                for index in range(count)
            ]
            for phase_index, phase in enumerate(PHASES)
        }
        for mode in ("main", "capture")
    }


def _parser_call_samples(
    count: int = 60,
) -> dict[str, dict[str, list[int]]]:
    return {
        mode: {
            "full_module_parses": [1] * count,
            "delta_method_parses": [0] * count,
            "packaging_validation_parses": [0] * count,
        }
        for mode in ("main", "capture")
    }


def test_measurement_phase_contract_matches_production_reload_order() -> None:
    assert PHASES == (
        "semantic_parse",
        "ast_model_extract",
        "catalog_validation",
        "dependency_analysis",
        "resolved_analysis_adapter",
        "alias_transform",
        "source_map_composition",
        "admission",
        "epf_packaging",
        "artifact_staging",
        "generation_create_wire_probe",
        "root_swap",
        "end_to_end",
    )


def _pass_gates() -> dict[str, str]:
    return {
        "main": "PASS",
        "capture": "PASS",
        "serialization": "PASS",
        "diagnostics": "PASS",
        "lifecycle": "PASS",
        "failure_cleanup": "PASS",
        "privacy": "PASS",
    }


def test_runtime_checkpoint_uses_public_idle_and_worker_status(monkeypatch) -> None:
    checkpoint = {"runtime_git_commit": "a" * 40}
    monkeypatch.setattr(
        zup_acceptance, "active_parser_acceptance_checkpoint",
        lambda: checkpoint,
    )

    class Session:
        state = OperationState.IDLE
        worker_generation = None

        def status(self):
            return SimpleNamespace(
                state=self.state, worker_generation=self.worker_generation,
            )

    session = Session()
    assert zup_acceptance.require_fresh_parser_runtime(
        session, checkpoint,
    )["fresh_empty_session_cache"] is True
    session.worker_generation = object()
    with pytest.raises(ProtocolError, match="not fresh"):
        zup_acceptance.require_fresh_parser_runtime(session, checkpoint)


def test_incremental_promotion_uses_public_generation_and_phase_evidence() -> None:
    before = WorkerGenerationHandle(1, 1, 17, "a" * 64)
    after = WorkerGenerationHandle(1, 1, 18, "b" * 64)
    units = (
        SimpleNamespace(logical_name="КадровыйУчет"),
        SimpleNamespace(logical_name="КадровыйУчетРасширенный"),
    )

    class Session:
        worker_generation = before

        def __init__(self) -> None:
            self.runtime_api = SimpleNamespace(
                confirmed_worker_module_units=lambda handle: units if handle is after else (),
            )

        def status(self):
            return SimpleNamespace(worker_generation=self.worker_generation)

        def load_worker_modules(self, received, *, profiler):
            assert received is units
            for phase in zup_acceptance._INCREMENTAL_COUNTERS:
                profiler.measure(
                    phase, lambda: None,
                    item_count=(lambda _result: 1) if phase == "artifact_staging" else None,
                )
            self.worker_generation = after
            return after

    handle, accounting = zup_acceptance._observe_incremental_promotion(
        Session(), units,
    )
    assert handle is after
    assert accounting.unchanged_parse == 0
    assert accounting.changed_parse == 1
    assert accounting.changed_artifact_staging == 1


def test_capture_promotion_checks_public_canaries_and_terminal_generation() -> None:
    g17 = WorkerGenerationHandle(1, 1, 17, "a" * 64)
    g18 = WorkerGenerationHandle(1, 1, 18, "b" * 64)
    g19 = WorkerGenerationHandle(1, 1, 19, "c" * 64)
    units = (
        SimpleNamespace(logical_name="КадровыйУчет"),
        SimpleNamespace(logical_name="КадровыйУчетРасширенный"),
    )

    class Session:
        worker_generation = g17
        loads = 0
        capture_cells = 0

        def __init__(self) -> None:
            self.runtime_api = SimpleNamespace(
                confirmed_worker_module_units=lambda handle: units if handle is g18 else (),
            )

        def status(self):
            return SimpleNamespace(worker_generation=self.worker_generation)

        def load_worker_modules(self, received, *, profiler=None):
            assert received is units
            self.loads += 1
            if profiler is not None:
                for phase in zup_acceptance._INCREMENTAL_COUNTERS:
                    profiler.measure(
                        phase, lambda: None,
                        item_count=(lambda _result: 1) if phase == "artifact_staging" else None,
                    )
            self.worker_generation = (g18, g19)[self.loads - 1]
            return self.worker_generation

        def execute_bsl(self, source):
            assert "__OnecTask10CrossCall" in source
            if self.capture_cells < 2:
                self.capture_cells += 1
                return RuntimeReply(RuntimeReplyKind.CAPTURE_CELL, 1, OperationState.CAPTURED, 1719)
            return RuntimeReply(RuntimeReplyKind.MAIN_COMPLETED, 2, OperationState.COMPLETED, 1719)

        def resume_capture(self):
            return RuntimeReply(
                RuntimeReplyKind.MAIN_COMPLETED, 1, OperationState.COMPLETED,
                "1717|1717",
            )

    session = Session()
    observed_g18, observed_g19, _ = zup_acceptance._exercise_captured_two_promotion_lifecycle(
        session, g17=g17, g18_units=units, g19_units=units,
    )
    assert (observed_g18, observed_g19) == (g18, g19)
    assert session.capture_cells == 2


def _observed_incremental() -> IncrementalAccounting:
    return IncrementalAccounting(
        unchanged_parse=0,
        unchanged_dependency_analysis=0,
        unchanged_lowering=0,
        unchanged_source_map_composition=0,
        unchanged_admission=0,
        unchanged_packaging=0,
        unchanged_artifact_staging=0,
        changed_parse=1,
        changed_dependency_analysis=1,
        changed_lowering=1,
        changed_source_map_composition=1,
        changed_admission=1,
        changed_packaging=1,
        changed_artifact_staging=1,
    )


def _valid_compact_evidence(
    *,
    catalog_setup_ms: float = 41.0,
    phase_samples: dict[str, dict[str, list[float]]] | None = None,
) -> dict[str, object]:
    return build_compact_evidence(
        reference=_reference_observation(),
        warmup_iterations=3,
        measured_iterations=60,
        catalog_setup_ms=catalog_setup_ms,
        phase_samples=phase_samples or _phase_samples(),
        parser_call_samples=_parser_call_samples(),
        accounting=_observed_incremental(),
        catalog_extension=_catalog_extension_evidence(),
        admission={
            "catalog_modules": 2,
            "accepted_modules": 2,
            "accepted_bindings": 2,
            "original_targets": 1,
            "same_generation_targets": 1,
            "unsupported": [],
        },
        gates=_pass_gates(),
    )


def test_compact_evidence_keeps_exact_privacy_safe_parser_counters() -> None:
    evidence = _valid_compact_evidence()

    for mode in ("main", "capture"):
        assert evidence["modes"][mode]["parser_calls"] == {
            "full_module_parses": [1] * 60,
            "delta_method_parses": [0] * 60,
            "packaging_validation_parses": [0] * 60,
        }
    encoded = json.dumps(evidence, ensure_ascii=False).casefold()
    assert "source_text" not in encoded
    assert "private_path" not in encoded
    assert "registration_name" not in encoded
    assert "username" not in encoded


@pytest.mark.parametrize(
    "mutation",
    (
        lambda counters: counters.pop("full_module_parses"),
        lambda counters: counters.update({"unexpected_parses": [0] * 60}),
        lambda counters: counters["delta_method_parses"].__setitem__(0, -1),
        lambda counters: counters["delta_method_parses"].__setitem__(0, True),
        lambda counters: counters["full_module_parses"].__setitem__(0, 0),
        lambda counters: counters["delta_method_parses"].__setitem__(0, 1),
        lambda counters: counters["packaging_validation_parses"].__setitem__(0, 1),
    ),
)
def test_compact_evidence_rejects_missing_extra_negative_or_incompatible_parser_counters(
    mutation,
) -> None:
    evidence = _valid_compact_evidence()
    mutation(evidence["modes"]["main"]["parser_calls"])

    with pytest.raises(ProtocolError, match="parser call evidence"):
        verify_compact_evidence(evidence)


def test_nearest_rank_percentiles_use_literal_measured_samples() -> None:
    samples = tuple(float(value) for value in range(1, 61))

    assert nearest_rank(samples, 0.50) == 30.0
    assert nearest_rank(samples, 0.95) == 57.0


def test_compact_evidence_keeps_catalog_setup_outside_reload_samples() -> None:
    evidence = build_compact_evidence(
        reference=_reference_observation(),
        warmup_iterations=3,
        measured_iterations=60,
        catalog_setup_ms=41.0,
        phase_samples=_phase_samples(),
        parser_call_samples=_parser_call_samples(),
        accounting=_observed_incremental(),
        catalog_extension=_catalog_extension_evidence(),
        admission={
            "catalog_modules": 2,
            "accepted_modules": 2,
            "accepted_bindings": 2,
            "original_targets": 1,
            "same_generation_targets": 1,
            "unsupported": [],
        },
        gates=_pass_gates(),
    )

    assert evidence["schema"] == "onec-worker-universe-zup-acceptance-v3"
    assert evidence["catalog_setup_ms"] == 41.0
    assert len(
        evidence["modes"]["main"]["phase_samples_ms"]["catalog_validation"]
    ) == 60


def test_compact_evidence_retains_exact_catalog_extension_gate() -> None:
    evidence = _valid_compact_evidence()

    assert evidence["catalog_extension"] == _catalog_extension_evidence()


@pytest.mark.parametrize(
    ("path", "value"),
    (
        (("success", "runtime_dispatches"), 0),
        (("success", "unchanged", "semantic_parse"), 1),
        (("success", "unchanged", "dependency_analysis"), 0),
        (("success", "new", "artifact_staging"), 1),
        (("rollback", "attempts"), 1),
        (("rollback", "runtime_dispatches"), 1),
        (("rollback", "active_generation_unchanged"), False),
        (("rollback", "build", "semantic_parse"), 0),
        (("rollback", "build", "dependency_analysis"), True),
    ),
)
def test_compact_evidence_rejects_catalog_extension_mutations(
    path: tuple[str, ...],
    value: object,
) -> None:
    evidence = _valid_compact_evidence()
    nested = evidence["catalog_extension"]
    for key in path[:-1]:
        nested = nested[key]
    nested[path[-1]] = value

    with pytest.raises(ProtocolError, match="catalog extension evidence"):
        verify_compact_evidence(evidence)


def test_compact_evidence_rejects_catalog_extension_extra_or_missing_fields() -> None:
    evidence = _valid_compact_evidence()
    evidence["catalog_extension"]["success"]["unexpected"] = 0
    with pytest.raises(ProtocolError, match="catalog extension evidence"):
        verify_compact_evidence(evidence)

    evidence = _valid_compact_evidence()
    evidence.pop("catalog_extension")
    with pytest.raises(ProtocolError, match="compact evidence is invalid"):
        verify_compact_evidence(evidence)


def test_terminal_v2_rejects_catalog_extension_pass_claims() -> None:
    evidence = zup_acceptance._terminal_payload(
        [{"code": "source_root_missing", "count": 1}],
        warmup_iterations=3,
        measured_iterations=60,
    )
    evidence["catalog_extension"] = _catalog_extension_evidence()

    with pytest.raises(ProtocolError, match="terminal compatibility evidence"):
        verify_compact_evidence(evidence)


@pytest.mark.parametrize("catalog_setup_ms", (-0.001, float("nan"), float("inf")))
def test_compact_evidence_verifier_rejects_invalid_catalog_setup(
    catalog_setup_ms: float,
) -> None:
    evidence = build_compact_evidence(
        reference=_reference_observation(),
        warmup_iterations=3,
        measured_iterations=60,
        catalog_setup_ms=41.0,
        phase_samples=_phase_samples(),
        parser_call_samples=_parser_call_samples(),
        accounting=_observed_incremental(),
        catalog_extension=_catalog_extension_evidence(),
        admission={
            "catalog_modules": 2,
            "accepted_modules": 2,
            "accepted_bindings": 2,
            "original_targets": 1,
            "same_generation_targets": 1,
            "unsupported": [],
        },
        gates=_pass_gates(),
    )
    evidence["catalog_setup_ms"] = catalog_setup_ms

    with pytest.raises(ProtocolError, match="catalog setup evidence"):
        verify_compact_evidence(evidence)


def test_compact_evidence_builder_rejects_boolean_catalog_setup() -> None:
    with pytest.raises(ProtocolError, match="catalog setup evidence"):
        _valid_compact_evidence(catalog_setup_ms=True)


def test_compact_evidence_rejects_v1_without_inferring_catalog_setup() -> None:
    evidence = build_compact_evidence(
        reference=_reference_observation(),
        warmup_iterations=3,
        measured_iterations=60,
        catalog_setup_ms=41.0,
        phase_samples=_phase_samples(),
        parser_call_samples=_parser_call_samples(),
        accounting=_observed_incremental(),
        catalog_extension=_catalog_extension_evidence(),
        admission={
            "catalog_modules": 2,
            "accepted_modules": 2,
            "accepted_bindings": 2,
            "original_targets": 1,
            "same_generation_targets": 1,
            "unsupported": [],
        },
        gates=_pass_gates(),
    )
    evidence["schema"] = "onec-worker-universe-zup-acceptance-v1"
    evidence.pop("catalog_setup_ms")

    with pytest.raises(ProtocolError, match="compact evidence is invalid"):
        verify_compact_evidence(evidence)


def test_compact_evidence_rejects_previous_rollback_schema() -> None:
    evidence = _valid_compact_evidence()
    evidence["schema"] = "onec-worker-universe-zup-acceptance-v2"

    with pytest.raises(ProtocolError, match="compact evidence is invalid"):
        verify_compact_evidence(evidence)


def test_compact_evidence_recomputes_phases_and_incremental_accounting() -> None:
    accounting = _observed_incremental()
    evidence = build_compact_evidence(
        reference=_reference_observation(),
        warmup_iterations=3,
        measured_iterations=60,
        catalog_setup_ms=41.0,
        phase_samples=_phase_samples(),
        parser_call_samples=_parser_call_samples(),
        accounting=accounting,
        catalog_extension=_catalog_extension_evidence(),
        admission={
            "catalog_modules": 2,
            "accepted_modules": 2,
            "accepted_bindings": 3,
            "original_targets": 1,
            "same_generation_targets": 2,
            "unsupported": [],
        },
        gates=_pass_gates(),
    )

    verified = verify_compact_evidence(evidence)

    assert verified["status"] == "PASS"
    assert verified["sla_claimed"] is True
    assert verified["modes"]["main"]["end_to_end"]["p50_ms"] == 39.0
    assert verified["modes"]["main"]["end_to_end"]["p95_ms"] == 66.0
    assert verified["modes"]["main"]["dominant_phase"] == "root_swap"
    accounting_rows = verified["modes"]["main"]["phase_accounting"]
    assert len(accounting_rows["component_sum_ms"]) == 60
    assert len(accounting_rows["unattributed_ms"]) == 60
    assert all(value >= 0 for value in accounting_rows["unattributed_ms"])
    assert verified["incremental"]["unchanged"] == {
        "logical_name": "КадровыйУчет",
        "module_sha256": verified["incremental"]["unchanged"]["module_sha256"],
        "semantic_parse": 0,
        "dependency_analysis": 0,
        "alias_transform": 0,
        "source_map_composition": 0,
        "admission": 0,
        "epf_packaging": 0,
        "artifact_staging": 0,
    }
    assert verified["incremental"]["changed"] == {
        "logical_name": "КадровыйУчетРасширенный",
        "module_sha256": verified["incremental"]["changed"]["module_sha256"],
        "semantic_parse": 1,
        "dependency_analysis": 1,
        "alias_transform": 1,
        "source_map_composition": 1,
        "admission": 1,
        "epf_packaging": 1,
        "artifact_staging": 1,
    }


@pytest.mark.parametrize(
    ("section", "counter", "value"),
    (
        ("unchanged", "semantic_parse", False),
        ("unchanged", "epf_packaging", False),
        ("changed", "dependency_analysis", True),
        ("changed", "artifact_staging", True),
    ),
)
def test_incremental_counters_reject_bool_values(
    section: str,
    counter: str,
    value: bool,
) -> None:
    evidence = build_compact_evidence(
        reference=_reference_observation(),
        warmup_iterations=3,
        measured_iterations=60,
        catalog_setup_ms=41.0,
        phase_samples=_phase_samples(),
        parser_call_samples=_parser_call_samples(),
        accounting=_observed_incremental(),
        catalog_extension=_catalog_extension_evidence(),
        admission={
            "catalog_modules": 2,
            "accepted_modules": 2,
            "accepted_bindings": 2,
            "original_targets": 1,
            "same_generation_targets": 1,
            "unsupported": [],
        },
        gates=_pass_gates(),
    )
    evidence["incremental"][section][counter] = value

    with pytest.raises(ProtocolError, match="incremental evidence"):
        verify_compact_evidence(evidence)


@pytest.mark.parametrize(
    ("warmup", "measured"),
    ((2, 60), (4, 60), (3, 59), (3, 61), (3.0, 60), (3, 60.0)),
)
def test_pass_sla_requires_exact_three_plus_sixty(
    warmup: object,
    measured: object,
) -> None:
    with pytest.raises(ValueError, match="exactly 3 warmup and 60 measured"):
        build_compact_evidence(
            reference=_reference_observation(),
            warmup_iterations=warmup,
            measured_iterations=measured,
            catalog_setup_ms=41.0,
            phase_samples=_phase_samples(int(measured)),
            parser_call_samples=_parser_call_samples(int(measured)),
            accounting=_observed_incremental(),
            catalog_extension=_catalog_extension_evidence(),
            admission={
                "catalog_modules": 2,
                "accepted_modules": 2,
                "accepted_bindings": 2,
                "original_targets": 1,
                "same_generation_targets": 1,
                "unsupported": [],
            },
            gates=_pass_gates(),
        )


@pytest.mark.parametrize(("mode", "count"), (("main", 59), ("capture", 61)))
def test_pass_sla_rejects_any_phase_sample_count_other_than_sixty(
    mode: str,
    count: int,
) -> None:
    samples = _phase_samples()
    samples[mode]["root_swap"] = samples[mode]["root_swap"][:count]
    if count > 60:
        samples[mode]["root_swap"].append(1.0)

    with pytest.raises(ProtocolError, match="phase samples are incomplete"):
        _valid_compact_evidence(phase_samples=samples)


@pytest.mark.parametrize("mode", ("main", "capture"))
def test_pass_sla_rejects_p95_at_threshold_in_either_mode(mode: str) -> None:
    samples = _phase_samples()
    samples[mode]["end_to_end"] = [2_500.0] * 60

    with pytest.raises(ProtocolError, match="measured SLA gate"):
        _valid_compact_evidence(phase_samples=samples)


def test_compact_evidence_rejects_impossible_phase_accounting() -> None:
    samples = _phase_samples()
    samples["main"]["catalog_validation"][0] = 10_000.0

    with pytest.raises(ProtocolError, match="phase accounting"):
        build_compact_evidence(
            reference=_reference_observation(),
            warmup_iterations=3,
            measured_iterations=60,
            catalog_setup_ms=41.0,
            phase_samples=samples,
            parser_call_samples=_parser_call_samples(),
            accounting=IncrementalAccounting.clean_update(),
            catalog_extension=_catalog_extension_evidence(),
            admission={
                "catalog_modules": 2,
                "accepted_modules": 2,
                "accepted_bindings": 2,
                "original_targets": 1,
                "same_generation_targets": 1,
                "unsupported": [],
            },
            gates=_pass_gates(),
        )


@pytest.mark.parametrize(
    "mutation",
    (
        lambda value: value.update({"private_path": r"C:\\private\\zup"}),
        lambda value: value["reference"].update({"username": "private-user"}),
        lambda value: value["admission"].update({"source_text": "Функция Секрет()"}),
        lambda value: value["gates"].update(
            {"target_object": "<ВнешняяОбработкаОбъект>"}
        ),
        lambda value: value["gates"].update(
            {"note": "Функция Секрет() Экспорт\nКонецФункции"}
        ),
        lambda value: value["gates"].update(
            {"registration_id": "6f9c5a90-286f-4e69-a734-bda7680b5f43"}
        ),
        lambda value: value["gates"].update({"root_key": "opaque-root"}),
        lambda value: value["gates"].update(
            {"object": "<External Processing Object instance>"}
        ),
        lambda value: value["gates"].update(
            {"object": "External Processing Object instance"}
        ),
        lambda value: value["gates"].update(
            {"object": "<Внешняя Обработка Объект instance>"}
        ),
        lambda value: value["gates"].update({"unc": r"\\server\share\zup"}),
        lambda value: value["gates"].update({"posix": "/srv/private/zup"}),
        lambda value: value["gates"].update({"posix": "/private"}),
        lambda value: value["gates"].update(
            {"snippet": "Если Флаг Тогда\nРезультат = 1;\nКонецЕсли;"}
        ),
        lambda value: value["gates"].update({"snippet": "Секрет = 1;"}),
    ),
)
def test_compact_evidence_rejects_private_paths_source_credentials_and_objects(
    mutation,
) -> None:
    evidence = build_compact_evidence(
        reference=_reference_observation(),
        warmup_iterations=3,
        measured_iterations=60,
        catalog_setup_ms=41.0,
        phase_samples=_phase_samples(),
        parser_call_samples=_parser_call_samples(),
        accounting=IncrementalAccounting.clean_update(),
        catalog_extension=_catalog_extension_evidence(),
        admission={
            "catalog_modules": 2,
            "accepted_modules": 2,
            "accepted_bindings": 2,
            "original_targets": 1,
            "same_generation_targets": 1,
            "unsupported": [],
        },
        gates=_pass_gates(),
    )
    mutation(evidence)

    with pytest.raises(ProtocolError, match="private evidence"):
        verify_compact_evidence(evidence)


def test_compact_evidence_rejects_reference_extra_fields() -> None:
    evidence = build_compact_evidence(
        reference=_reference_observation(),
        warmup_iterations=3,
        measured_iterations=60,
        catalog_setup_ms=41.0,
        phase_samples=_phase_samples(),
        parser_call_samples=_parser_call_samples(),
        accounting=_observed_incremental(),
        catalog_extension=_catalog_extension_evidence(),
        admission={
            "catalog_modules": 2,
            "accepted_modules": 2,
            "accepted_bindings": 2,
            "original_targets": 1,
            "same_generation_targets": 1,
            "unsupported": [],
        },
        gates=_pass_gates(),
    )
    evidence["reference"]["unexpected"] = "public-looking"

    with pytest.raises(ProtocolError, match="reference evidence"):
        verify_compact_evidence(evidence)


@pytest.mark.parametrize(
    ("field", "value"),
    (("warmup_iterations", 2), ("measured_iterations", 59)),
)
def test_terminal_v2_rejects_non_exact_iteration_budget(
    field: str,
    value: int,
) -> None:
    evidence = zup_acceptance._terminal_payload(
        [{"code": "source_root_missing", "count": 1}],
        warmup_iterations=3,
        measured_iterations=60,
    )
    evidence[field] = value

    with pytest.raises(ProtocolError, match="iteration evidence"):
        verify_compact_evidence(evidence)


def test_pass_v2_rejects_non_empty_compatibility_inventory() -> None:
    evidence = _valid_compact_evidence()
    evidence["compatibility_inventory"] = [
        {"code": "unexpected_pass_inventory", "count": 1}
    ]

    with pytest.raises(ProtocolError, match="compatibility inventory"):
        verify_compact_evidence(evidence)


def test_pass_v2_rejects_extra_mode_fields() -> None:
    evidence = _valid_compact_evidence()
    evidence["modes"]["main"]["unexpected"] = 1

    with pytest.raises(ProtocolError, match="mode evidence"):
        verify_compact_evidence(evidence)


def test_pass_v2_rejects_end_to_end_alias_different_from_percentiles() -> None:
    evidence = _valid_compact_evidence()
    evidence["modes"]["capture"]["end_to_end"]["p95_ms"] += 1.0

    with pytest.raises(ProtocolError, match="end-to-end evidence"):
        verify_compact_evidence(evidence)


def test_no_live_terminal_inventory_is_written_before_runner_is_called(
    tmp_path: Path,
) -> None:
    config = SimpleNamespace(
        platform_bin=tmp_path / "missing-platform" / "bin",
        infobase_dir=tmp_path / "missing-infobase",
        artifacts_dir=tmp_path / "artifacts",
    )
    live_calls: list[object] = []

    run_dir = run_worker_universe_zup_acceptance(
        config,
        iterations=60,
        warmup_iterations=3,
        source_root=None,
        _live_runner=lambda *args, **kwargs: live_calls.append((args, kwargs)),
    )
    outcome = verify_worker_universe_zup_evidence(run_dir)

    assert live_calls == []
    assert outcome["status"] == "incompatible_prerequisites"
    assert outcome["sla_claimed"] is False
    assert outcome["modes"] == {}
    assert outcome["compatibility_inventory"] == [
        {"code": "platform_directory_missing", "count": 1},
        {"code": "infobase_file_missing", "count": 1},
        {"code": "source_root_missing", "count": 1},
    ]


def test_semantic_gate_outcomes_require_observed_checks_not_pass_constants() -> None:
    result_type = getattr(zup_acceptance, "SemanticAcceptanceResult", None)
    assert result_type is not None

    with pytest.raises(ProtocolError, match="observed semantic checks"):
        result_type(
            admission={
                "catalog_modules": 2,
                "accepted_modules": 2,
                "accepted_bindings": 2,
                "original_targets": 1,
                "same_generation_targets": 1,
                "unsupported": [],
            },
            accounting=_observed_incremental(),
            checks={name: True for name in _pass_gates() if name != "privacy"},
        ).gates()


def test_compile_diagnostic_contract_requires_exact_unit_revision_mapping_and_span() -> None:
    verifier = getattr(zup_acceptance, "_verify_compile_diagnostic_contract", None)
    assert verifier is not None
    source = (
        "Функция __OnecTask10CompileFailure()\n"
        "    Возврат Task10UndefinedCompileName;\n"
        "КонецФункции"
    )
    marker = "Task10UndefinedCompileName"
    offset = source.index(marker)
    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL,
        "task10-compile-diagnostic",
        17,
        zup_acceptance.source_sha256(source),
    )
    location = VisibleSourceLocation(unit, 2, 13, SourceSpan(offset, offset + 1))
    diagnostic = NormalizedDiagnostic(
        "compile-id",
        "compile-failed",
        DiagnosticStage.COMPILATION,
        MappingConfidence.EXACT,
        source_unit=unit,
        visible_location=location,
    )

    verifier(diagnostic, source=source, source_unit=unit, marker=marker)
    with pytest.raises(ProtocolError, match="compile diagnostic contract"):
        verifier(
            replace(diagnostic, mapping_confidence=MappingConfidence.UNKNOWN),
            source=source,
            source_unit=unit,
            marker=marker,
        )
    with pytest.raises(ProtocolError, match="compile diagnostic contract"):
        verifier(
            replace(
                diagnostic,
                visible_location=replace(
                    location,
                    span=SourceSpan(offset + 1, offset + 2),
                ),
            ),
            source=source,
            source_unit=unit,
            marker=marker,
        )


def test_compile_diagnostic_probe_is_a_statement_only_undefined_procedure() -> None:
    probe = getattr(zup_acceptance, "_compile_diagnostic_probe", None)
    assert callable(probe)

    source, marker = probe()
    split = split_notebook_cell(PythonParserTarget.from_generated(), source)

    assert marker == "Task10UndefinedCompileProcedure"
    assert source == f"{marker}();\nРезультат = Истина;"
    assert not split.worker_source
    assert split.statement_source == source


def test_runtime_diagnostic_contract_requires_exact_callee_caller_frame_order() -> None:
    verifier = getattr(zup_acceptance, "_verify_runtime_diagnostic_contract", None)
    assert verifier is not None
    callee_source = "Функция Throw() Экспорт\n    Task10RuntimeThrowMarker();\nКонецФункции"
    caller_source = "Функция Call() Экспорт\n    Возврат A.Throw();\nКонецФункции"
    callee_marker = "Task10RuntimeThrowMarker"
    caller_marker = "A.Throw"
    callee_offset = callee_source.index(callee_marker)
    caller_offset = caller_source.index(caller_marker)
    callee = SourceUnitRef(
        SourceUnitKind.MODULE,
        "КадровыйУчетРасширенный",
        19,
        zup_acceptance.source_sha256(callee_source),
    )
    caller = SourceUnitRef(
        SourceUnitKind.MODULE,
        "КадровыйУчет",
        17,
        zup_acceptance.source_sha256(caller_source),
    )
    frames = (
        WorkerRuntimeFrameDiagnostic(
            "redacted-callee",
            callee.unit_id,
            callee.revision,
            "a" * 64,
            MappingConfidence.EXACT,
            source_unit=callee,
            visible_location=VisibleSourceLocation(
                callee, 2, 5, SourceSpan(callee_offset, callee_offset + 1)
            ),
        ),
        WorkerRuntimeFrameDiagnostic(
            "redacted-caller",
            caller.unit_id,
            caller.revision,
            "b" * 64,
            MappingConfidence.EXACT,
            source_unit=caller,
            visible_location=VisibleSourceLocation(
                caller, 2, 13, SourceSpan(caller_offset, caller_offset + 1)
            ),
        ),
    )
    diagnostic = NormalizedDiagnostic(
        "runtime-id",
        "runtime-failed",
        DiagnosticStage.EXECUTION,
        MappingConfidence.EXACT,
        worker_frames=frames,
    )

    verifier(
        diagnostic,
        units=((callee, callee_source), (caller, caller_source)),
        markers=(callee_marker, caller_marker),
    )
    with pytest.raises(ProtocolError, match="runtime diagnostic contract"):
        verifier(
            replace(diagnostic, worker_frames=tuple(reversed(frames))),
            units=((callee, callee_source), (caller, caller_source)),
            markers=(callee_marker, caller_marker),
        )
    with pytest.raises(ProtocolError, match="runtime diagnostic contract"):
        verifier(
            replace(diagnostic, worker_frames=(*frames, frames[-1])),
            units=((callee, callee_source), (caller, caller_source)),
            markers=(callee_marker, caller_marker),
        )


def test_worker_proxy_privacy_uses_bare_names_and_exact_context_handles() -> None:
    verifier = getattr(zup_acceptance, "_require_worker_proxy_privacy", None)
    assert verifier is not None

    class FakeSession:
        def __init__(self) -> None:
            self.handles: list[str] = []

        def namespace_snapshot(self) -> RuntimeNamespaceSnapshot:
            return RuntimeNamespaceSnapshot(7, 3, ())

        def validate_value_reference(self, handle: str) -> None:
            self.handles.append(handle)
            raise ProtocolError("Worker generation objects are not public values")

    session = FakeSession()

    verifier(session)

    assert session.handles == [
        "e1cRuntimeКонтекст.RuntimeWorkerActiveGeneration",
        "e1cRuntimeКонтекст.RuntimeWorkerActiveGeneration.Modules.КадровыйУчет",
    ]
    assert all("e1cRuntimeКонтекст.e1cRuntimeКонтекст" not in handle for handle in session.handles)


def test_capture_pair_result_uses_locale_stable_integer_formatting() -> None:
    builder = getattr(zup_acceptance, "_capture_pair_result_source", None)
    assert builder is not None

    source = builder("Task10BeforeCapture", "Task10AfterCapture")

    assert source == (
        'Результат = Формат(Task10BeforeCapture, "ЧГ=0") + "|" + '
        'Формат(Task10AfterCapture, "ЧГ=0");'
    )
    assert "Строка(" not in source


def test_pinned_capture_canary_uses_capture_result_receiver() -> None:
    builder = getattr(zup_acceptance, "_pinned_capture_canary_source", None)
    assert builder is not None

    source = builder("КадровыйУчет")

    assert source == (
        "РезультатИнструкции = "
        "КадровыйУчет.__OnecTask10CrossCall();"
    )
    assert not source.startswith("Результат =")


def _measurement_base_units() -> tuple[object, ...]:
    units = []
    for name, revision, source in (
        ("КадровыйУчет", 17, "Функция База() Экспорт\n    Возврат 17;\nКонецФункции"),
        (
            "КадровыйУчетРасширенный",
            19,
            "Функция База() Экспорт\n    Возврат 19;\nКонецФункции",
        ),
    ):
        source_ref = SourceUnitRef(
            SourceUnitKind.MODULE,
            name,
            revision,
            zup_acceptance.source_sha256(source),
        )
        units.append(
            zup_acceptance.WorkerModuleUnit(
                name,
                "module",
                revision,
                zup_acceptance.mapped_visible_source(source, source_ref),
            )
        )
    return tuple(units)


def _common_module_metadata(name: str, *, server: bool = True) -> str:
    return (
        "<MetaDataObject><CommonModule><Properties>"
        f"<Name>{name}</Name><Global>false</Global>"
        f"<Server>{str(server).lower()}</Server>"
        "<ClientManagedApplication>false</ClientManagedApplication>"
        "<ClientOrdinaryApplication>false</ClientOrdinaryApplication>"
        "</Properties></CommonModule></MetaDataObject>"
    )


def test_catalog_mirror_contains_only_metadata_and_matches_preflight(
    tmp_path: Path,
) -> None:
    mirror_catalog = getattr(zup_acceptance, "_mirror_common_module_metadata", None)
    assert mirror_catalog is not None
    source_root = tmp_path / "approved-source"
    common_modules = source_root / "CommonModules"
    common_modules.mkdir(parents=True)
    for name in ("КадровыйУчет", "КадровыйУчетРасширенный", "Нетarget"):
        (common_modules / f"{name}.xml").write_text(
            _common_module_metadata(name),
            encoding="utf-8",
        )
        extension = common_modules / name / "Ext"
        extension.mkdir(parents=True)
        (extension / "Module.bsl").write_text(
            "Функция Fixture() Экспорт\n    Возврат 1;\nКонецФункции",
            encoding="utf-8",
        )
    private_root = tmp_path / ".private-semantic"
    private_root.mkdir()

    mirror_root = mirror_catalog(source_root, private_root)
    models = tuple(
        zup_acceptance.parse_full_ast_module(
            f"Процедура P{index}()\nКонецПроцедуры"
        )
        for index in range(2)
    )
    preflight = build_profile_catalog(source_root, models)
    measured = zup_acceptance.SessionCommonModuleCatalog(
        mirror_root,
        profile="runtime-session-server-v1",
        preprocessor_profile="server",
    ).ensure_modules(("КадровыйУчет", "КадровыйУчетРасширенный"))

    assert preflight.profile == measured.profile == "runtime-session-server-v1"
    assert preflight.preprocessor_profile == measured.preprocessor_profile
    assert preflight.modules == measured.modules
    assert preflight.sha256 == measured.sha256
    assert sorted(
        path.relative_to(mirror_root).as_posix()
        for path in mirror_root.rglob("*")
        if path.is_file()
    ) == [
        "CommonModules/КадровыйУчет.xml",
        "CommonModules/КадровыйУчетРасширенный.xml",
        "CommonModules/Нетarget.xml",
    ]


def test_measured_catalog_identity_rejects_preflight_mismatch(tmp_path: Path) -> None:
    require_match = getattr(
        zup_acceptance,
        "_require_matching_catalog_snapshots",
        None,
    )
    assert require_match is not None
    roots = []
    for suffix, names in (("preflight", ("МодульА",)), ("measured", ("МодульБ",))):
        root = tmp_path / suffix
        common_modules = root / "CommonModules"
        common_modules.mkdir(parents=True)
        for name in names:
            (common_modules / f"{name}.xml").write_text(
                _common_module_metadata(name),
                encoding="utf-8",
            )
        roots.append(root)
    snapshots = tuple(
        zup_acceptance.SessionCommonModuleCatalog(
            root,
            profile="runtime-session-server-v1",
            preprocessor_profile="server",
        ).ensure_modules(names)
        for root, names in zip(
            roots,
            (("МодульА",), ("МодульБ",)),
            strict=True,
        )
    )

    with pytest.raises(ProtocolError, match="measured catalog identity"):
        require_match(*snapshots)


def test_session_catalog_read_audit_observes_first_load_snapshot(
    tmp_path: Path,
) -> None:
    bind_audit = getattr(zup_acceptance, "_bind_catalog_read_audit", None)
    assert bind_audit is not None
    source_root = tmp_path / ".private-catalog-source"
    common_modules = source_root / "CommonModules"
    common_modules.mkdir(parents=True)
    for name in ("КадровыйУчет", "КадровыйУчетРасширенный"):
        (common_modules / f"{name}.xml").write_text(
            _common_module_metadata(name),
            encoding="utf-8",
        )
    session = zup_acceptance.RuntimeSession.__new__(zup_acceptance.RuntimeSession)
    session._common_module_catalog = zup_acceptance.SessionCommonModuleCatalog(
        source_root,
        profile="runtime-session-server-v1",
        preprocessor_profile="server",
    )

    catalog = bind_audit(session, source_root)
    snapshot = catalog.resolve_candidates(
        ("КадровыйУчет", "КадровыйУчетРасширенный")
    )
    session.runtime_api = SimpleNamespace()
    recorder = PhaseRecorder()
    recorder.record_duration(
        "catalog_validation",
        wall_ns=1,
        item_count=len(snapshot.modules),
    )
    setup = zup_acceptance._catalog_setup_from_first_load(session, recorder)

    assert session._require_common_module_catalog() is catalog
    assert catalog.metadata_reads == 2
    assert setup.snapshot is snapshot
    assert setup.elapsed_ms >= 0


class _MeasuredReloadSession:
    def __init__(
        self,
        *,
        omitted_phase: str | None = None,
        phase_item_counts: dict[str, int] | None = None,
        catalog: object | None = None,
        first_full_warmup: bool = False,
    ) -> None:
        self.omitted_phase = omitted_phase
        self.phase_item_counts = phase_item_counts or {}
        self.calls: list[tuple[tuple[object, ...], PhaseRecorder]] = []
        self.releases: list[object] = []
        self.call_modes: list[str] = []
        self.mode = "main"
        self.catalog = catalog
        self.first_full_warmup = first_full_warmup
        self.runtime_api = SimpleNamespace()
        self.state = OperationState.IDLE

    def status(self) -> object:
        return SimpleNamespace(state=self.state, worker_generation=None)

    def _require_common_module_catalog(self) -> object:
        assert self.catalog is not None
        return self.catalog

    def load_worker_modules(
        self,
        units: tuple[object, ...],
        *,
        profiler: PhaseRecorder,
    ) -> object:
        self.calls.append((units, profiler))
        self.call_modes.append(self.mode)
        profiler.record_parser_call("full_module_parses")
        snapshot = None
        if self.catalog is not None:
            snapshot = self.catalog.ensure_modules(
                unit.logical_name for unit in units
            )
        call_number = len(self.calls)
        for phase in PHASES:
            if phase == self.omitted_phase:
                continue
            if phase == "artifact_staging":
                for drill_down, item_count in (
                    ("artifact_stage_sealed_validation", 2),
                    ("artifact_stage_base64", 1),
                    ("artifact_stage_executor", 1),
                    ("artifact_stage_batch", 1),
                ):
                    profiler.record_duration(
                        drill_down,
                        wall_ns=(3 if drill_down == "artifact_stage_batch" else 1),
                        item_count=item_count,
                    )
            duration_ms = (
                call_number + 20
                if phase == "end_to_end"
                else call_number if phase == "catalog_validation" else 1
            )
            profiler.record_duration(
                phase,
                wall_ns=duration_ms * 1_000_000,
                item_count=self.phase_item_counts.get(
                    phase,
                    (
                        len(snapshot.modules)
                        if phase == "catalog_validation" and snapshot is not None
                        else 1 if phase != "end_to_end" else 2
                    ),
                ),
            )
        return SimpleNamespace(generation=call_number)

    def release_worker_generation(self, handle: object) -> None:
        self.releases.append(handle)


class _ObservedExtensionCatalog(zup_acceptance.SessionCommonModuleCatalog):
    def __init__(self, source_root: Path) -> None:
        super().__init__(
            source_root,
            profile="runtime-session-server-v1",
            preprocessor_profile="server",
        )
        self.metadata_reads = 0

    def _read_metadata(self, metadata_path: Path):
        self.metadata_reads += 1
        return super()._read_metadata(metadata_path)


def _worker_unit_cache_key(unit: object) -> tuple[object, ...]:
    return (
        unit.logical_name.casefold(),
        unit.kind,
        unit.revision,
        unit.mapped_source.artifact.source_sha256,
        unit.mapped_source.source_map_sha256,
    )


class _CatalogExtensionSession:
    def __init__(
        self,
        catalog: _ObservedExtensionCatalog,
        active_units: tuple[object, ...],
        *,
        corrupt_rollback: bool = False,
    ) -> None:
        self.catalog = catalog
        self.load_attempts = 0
        self.runtime_dispatches = 0
        self.dispatched_units: list[tuple[object, ...]] = []
        self.releases: list[object] = []
        self.active_handle = SimpleNamespace(generation=145)
        self.confirmed_units = active_units
        self.corrupt_rollback = corrupt_rollback
        self.runtime_api = SimpleNamespace(
            confirmed_worker_module_units=self.confirmed_worker_module_units,
        )

    def status(self) -> object:
        return SimpleNamespace(worker_generation=self.active_handle)

    def confirmed_worker_module_units(self, handle: object) -> tuple[object, ...]:
        assert handle is self.active_handle
        return self.confirmed_units

    def _require_common_module_catalog(self) -> _ObservedExtensionCatalog:
        return self.catalog

    def load_worker_modules(
        self,
        units: tuple[object, ...],
        *,
        profiler: PhaseRecorder,
    ) -> object:
        self.load_attempts += 1

        def reload() -> object:
            additions = [
                unit
                for unit in units
                if _worker_unit_cache_key(unit)
                not in {_worker_unit_cache_key(item) for item in self.confirmed_units}
            ]
            for _unit in additions:
                profiler.record_duration("semantic_parse", wall_ns=1)
                profiler.record_duration("ast_model_extract", wall_ns=1)
            snapshot = profiler.measure(
                "catalog_validation",
                lambda: self.catalog.ensure_modules(
                    unit.logical_name for unit in units
                ),
                item_count=lambda result: len(result.modules),
            )
            assert snapshot is self.catalog.ensure_initialized()
            self.runtime_dispatches += 1
            self.dispatched_units.append(units)
            addition_keys = {
                _worker_unit_cache_key(unit) for unit in additions
            }
            for unit in units:
                if _worker_unit_cache_key(unit) not in addition_keys:
                    profiler.record_duration("dependency_analysis", wall_ns=1)
            for unit in additions:
                key = _worker_unit_cache_key(unit)
                for phase in (
                    "dependency_analysis",
                    "resolved_analysis_adapter",
                    "alias_transform",
                    "source_map_composition",
                    "admission",
                    "epf_packaging",
                ):
                    profiler.record_duration(
                        phase,
                        wall_ns=1,
                        item_count=1 if phase == "epf_packaging" else 0,
                    )
            for phase in zup_acceptance._STAGING_DRILL_DOWN_PHASES:
                profiler.record_duration(
                    phase,
                    wall_ns=1,
                    item_count=(
                        len(units)
                        if phase == "artifact_stage_sealed_validation"
                        else len(additions)
                    ),
                )
            profiler.record_duration(
                "artifact_staging",
                wall_ns=1,
                item_count=len(additions),
            )
            profiler.record_duration(
                "generation_create_wire_probe",
                wall_ns=1,
                item_count=1,
            )
            profiler.record_duration("root_swap", wall_ns=1, item_count=1)
            profiler.record_duration("worker_generation_publication", wall_ns=1)
            handle = SimpleNamespace(
                generation=self.active_handle.generation + 1
            )
            self.active_handle = handle
            self.confirmed_units = units
            return handle

        try:
            return profiler.measure(
                "end_to_end",
                reload,
                item_count=lambda _result: len(units),
            )
        except ProtocolError:
            if self.corrupt_rollback:
                self.confirmed_units = ()
            raise

    def release_worker_generation(self, handle: object) -> None:
        self.releases.append(handle)


def _catalog_extension_evidence() -> dict[str, object]:
    zero_build = {counter: 0 for counter in zup_acceptance._INCREMENTAL_COUNTERS}
    new_build = {counter: 2 for counter in zup_acceptance._INCREMENTAL_COUNTERS}
    existing_build = {
        counter: (
            2
            if counter == "dependency_analysis"
            else 0
        )
        for counter in zup_acceptance._INCREMENTAL_COUNTERS
    }
    return {
        "success": {
            "xml_reads": 2,
            "revision_delta": 1,
            "runtime_dispatches": 1,
            "unchanged": existing_build,
            "new": new_build,
        },
        "rollback": {
            "attempts": 2,
            "xml_reads": 3,
            "revision_delta": 0,
            "runtime_dispatches": 0,
            "catalog_unchanged": True,
            "active_generation_unchanged": True,
            "confirmed_units_unchanged": True,
            "build": {
                counter: (4 if counter == "semantic_parse" else 0)
                for counter in zup_acceptance._INCREMENTAL_COUNTERS
            },
        },
    }


def test_measure_mode_discards_three_warmups_and_changes_only_extended_source() -> None:
    measure = getattr(zup_acceptance, "_measure_mode", None)
    assert measure is not None
    base_units = _measurement_base_units()
    main_session = _MeasuredReloadSession()
    capture_session = _MeasuredReloadSession()

    main = measure(main_session, "main", base_units, warmups=3, iterations=60)
    capture = measure(
        capture_session,
        "capture",
        base_units,
        warmups=3,
        iterations=60,
    )

    assert set(main) == set(PHASES)
    assert set(capture) == set(PHASES)
    assert all(len(main[phase]) == 60 for phase in PHASES)
    assert all(len(capture[phase]) == 60 for phase in PHASES)
    assert main["catalog_validation"][0] == 4.0
    assert capture["catalog_validation"][0] == 4.0
    assert len(main_session.calls) == len(capture_session.calls) == 63
    assert len(main_session.releases) == len(capture_session.releases) == 63
    assert all(
        isinstance(profiler, PhaseRecorder)
        for _, profiler in (*main_session.calls, *capture_session.calls)
    )
    assert all(
        tuple(
            event.phase
            for event in profiler.events
            if event.phase.startswith("artifact_stage_")
        )
        == (
            "artifact_stage_sealed_validation",
            "artifact_stage_base64",
            "artifact_stage_executor",
            "artifact_stage_batch",
        )
        for _, profiler in (*main_session.calls, *capture_session.calls)
    )

    main_extended_revisions = []
    capture_extended_revisions = []
    main_extended_hashes = set()
    capture_extended_hashes = set()
    for calls, revisions, hashes in (
        (main_session.calls, main_extended_revisions, main_extended_hashes),
        (capture_session.calls, capture_extended_revisions, capture_extended_hashes),
    ):
        for units, _ in calls:
            assert units[0] is base_units[0]
            assert units[0].revision == 17
            assert units[0].mapped_source is base_units[0].mapped_source
            assert units[1].logical_name == "КадровыйУчетРасширенный"
            assert units[1].mapped_source.text != base_units[1].mapped_source.text
            revisions.append(units[1].revision)
            hashes.add(units[1].mapped_source.artifact.source_sha256)
    assert len(main_extended_hashes) == len(capture_extended_hashes) == 63
    assert set(main_extended_revisions).isdisjoint(capture_extended_revisions)


def test_measure_mode_requires_one_full_parse_for_each_changed_module() -> None:
    parser_samples: dict[str, list[int]] = {}
    session = _MeasuredReloadSession(first_full_warmup=True)

    zup_acceptance._measure_mode(
        session,
        "main",
        _measurement_base_units(),
        warmups=1,
        iterations=2,
        _parser_call_samples=parser_samples,
    )

    assert parser_samples == {
        "full_module_parses": [1, 1],
        "delta_method_parses": [0, 0],
        "packaging_validation_parses": [0, 0],
    }


def test_measure_mode_rejects_missing_phase_without_padding_a_zero() -> None:
    measure = getattr(zup_acceptance, "_measure_mode", None)
    assert measure is not None
    session = _MeasuredReloadSession(omitted_phase="epf_packaging")

    with pytest.raises(ProtocolError, match="reload phase stream"):
        measure(session, "main", _measurement_base_units(), warmups=3, iterations=60)

    assert len(session.calls) == 1
    assert all(event.phase != "epf_packaging" for event in session.calls[0][1].events)


def test_measure_mode_rejects_staging_more_than_the_changed_module() -> None:
    measure = getattr(zup_acceptance, "_measure_mode", None)
    assert measure is not None
    session = _MeasuredReloadSession(phase_item_counts={"artifact_staging": 2})

    with pytest.raises(ProtocolError, match="incremental phase accounting"):
        measure(session, "main", _measurement_base_units(), warmups=3, iterations=60)

    assert len(session.calls) == 1


def test_catalog_setup_is_observed_from_first_production_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observe_setup = getattr(zup_acceptance, "_catalog_setup_from_first_load", None)
    collect = getattr(zup_acceptance, "_collect_mode_samples", None)
    assert observe_setup is not None
    assert collect is not None
    source_root = tmp_path / "catalog-source"
    common_modules = source_root / "CommonModules"
    common_modules.mkdir(parents=True)
    for name in ("КадровыйУчет", "КадровыйУчетРасширенный"):
        (common_modules / f"{name}.xml").write_text(
            _common_module_metadata(name),
            encoding="utf-8",
        )
    catalog = zup_acceptance.SessionCommonModuleCatalog(
        source_root,
        profile="test-zup-session",
        preprocessor_profile="server",
    )
    session = _MeasuredReloadSession(catalog=catalog)
    reads: list[Path] = []
    original_read_bytes = Path.read_bytes

    def observed_read_bytes(path: Path) -> bytes:
        reads.append(path)
        return original_read_bytes(path)

    @contextmanager
    def captured(actual: _MeasuredReloadSession):
        assert actual is session
        assert actual.status().state is OperationState.IDLE
        actual.mode = "capture"
        actual.state = OperationState.CAPTURED
        try:
            yield
        finally:
            actual.state = OperationState.IDLE
            actual.mode = "main"

    monkeypatch.setattr(Path, "read_bytes", observed_read_bytes)

    first_profile = PhaseRecorder()
    session.load_worker_modules(_measurement_base_units(), profiler=first_profile)
    catalog_setup = observe_setup(session, first_profile)
    samples, parser_calls = collect(
        session,
        _measurement_base_units(),
        warmups=3,
        iterations=60,
        _capture_context_factory=captured,
    )

    assert catalog_setup.elapsed_ms >= 0
    assert catalog_setup.snapshot.profile == "test-zup-session"
    assert catalog_setup.snapshot.modules == catalog.ensure_initialized().modules
    assert len(reads) == 2
    assert all(path.suffix.casefold() == ".xml" for path in reads)
    assert session.call_modes == ["main"] * 64 + ["capture"] * 63
    assert all(len(samples[mode][phase]) == 60 for mode in samples for phase in PHASES)
    assert parser_calls == _parser_call_samples()
    assert session.status().state is OperationState.IDLE


def test_catalog_extension_gate_retains_real_success_and_both_rollbacks(
    tmp_path: Path,
) -> None:
    gate = getattr(zup_acceptance, "_run_missing_module_extension_gate", None)
    assert gate is not None
    mirror_root = tmp_path / ".private-catalog-source"
    common_modules = mirror_root / "CommonModules"
    common_modules.mkdir(parents=True)
    active_units = _measurement_base_units()
    for unit in active_units:
        (common_modules / f"{unit.logical_name}.xml").write_text(
            _common_module_metadata(unit.logical_name),
            encoding="utf-8",
        )
    catalog = _ObservedExtensionCatalog(mirror_root)
    catalog.ensure_modules(unit.logical_name for unit in active_units)
    session = _CatalogExtensionSession(catalog, active_units)

    evidence = gate(session, mirror_root, active_units)

    assert evidence == _catalog_extension_evidence()
    assert session.load_attempts == 3
    assert session.runtime_dispatches == 1
    assert len(session.releases) == 1
    assert len(session.dispatched_units) == 1
    dispatched = session.dispatched_units[0]
    assert dispatched[:2] == active_units
    assert dispatched[3].logical_name in dispatched[2].mapped_source.text
    assert dispatched[2].logical_name in dispatched[3].mapped_source.text
    assert sorted(path.suffix.casefold() for path in common_modules.iterdir()) == [
        ".xml",
        ".xml",
        ".xml",
        ".xml",
    ]


def test_catalog_extension_gate_rejects_changed_confirmed_sources_on_rollback(
    tmp_path: Path,
) -> None:
    mirror_root = tmp_path / "catalog-source"
    common_modules = mirror_root / "CommonModules"
    common_modules.mkdir(parents=True)
    active_units = _measurement_base_units()
    for unit in active_units:
        (common_modules / f"{unit.logical_name}.xml").write_text(
            _common_module_metadata(unit.logical_name), encoding="utf-8"
        )
    catalog = _ObservedExtensionCatalog(mirror_root)
    catalog.ensure_modules(unit.logical_name for unit in active_units)
    session = _CatalogExtensionSession(
        catalog, active_units, corrupt_rollback=True
    )

    with pytest.raises(ProtocolError, match="rollback evidence"):
        zup_acceptance._run_missing_module_extension_gate(
            session, mirror_root, active_units
        )


def test_verified_live_builds_pass_from_observed_semantics_and_reload_samples(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, Path, Path]] = []
    gates = _pass_gates()
    admission = {
        "catalog_modules": 417,
        "accepted_modules": 2,
        "accepted_bindings": 145,
        "original_targets": 0,
        "same_generation_targets": 145,
        "unsupported": [],
    }
    config = object()
    catalog_root = tmp_path / "catalog-source"
    common_modules = catalog_root / "CommonModules"
    common_modules.mkdir(parents=True)
    for name in ("КадровыйУчет", "КадровыйУчетРасширенный"):
        (common_modules / f"{name}.xml").write_text(
            _common_module_metadata(name),
            encoding="utf-8",
        )
    measured_catalog = zup_acceptance.SessionCommonModuleCatalog(
        catalog_root,
        profile="runtime-session-server-v1",
        preprocessor_profile="server",
    ).ensure_initialized()
    replacements: list[tuple[Path, Path]] = []

    def record_replace(source: object, destination: object) -> None:
        replacements.append((Path(source), Path(destination)))
        os.replace(source, destination)

    monkeypatch.setattr(
        zup_acceptance,
        "os",
        SimpleNamespace(replace=record_replace, fsync=os.fsync),
        raising=False,
    )

    def semantic_runner(
        config,
        source_root,
        run_dir,
        *,
        preflight,
        iterations,
        warmup_iterations,
    ):
        assert isinstance(preflight, ZupStaticPreflight)
        assert (warmup_iterations, iterations) == (3, 60)
        calls.append((config, source_root, run_dir))
        result_type = getattr(zup_acceptance, "SemanticAcceptanceResult", None)
        assert result_type is not None
        return result_type(
            admission=admission,
            accounting=_observed_incremental(),
            checks={name: True for name in gates},
            catalog_setup_ms=41.0,
            catalog_snapshot=measured_catalog,
            phase_samples=_phase_samples(),
            parser_call_samples=_parser_call_samples(),
            catalog_extension=_catalog_extension_evidence(),
        )

    outcome = _run_verified_live(
        config,
        Path("approved-source"),
        iterations=60,
        warmup_iterations=3,
        preflight=ZupStaticPreflight(
            (),
            catalog=measured_catalog,
            extension_sha256=REFERENCE_EXTENSION_SHA256,
        ),
        run_dir=tmp_path,
        _semantic_runner=semantic_runner,
    )

    assert calls == [(config, Path("approved-source"), tmp_path)]
    assert outcome["schema"] == "onec-worker-universe-zup-acceptance-v3"
    assert outcome["status"] == "PASS"
    assert outcome["sla_claimed"] is True
    assert outcome["catalog_setup_ms"] == 41.0
    assert outcome["catalog_extension"] == _catalog_extension_evidence()
    assert len(outcome["modes"]["main"]["phase_samples_ms"]["end_to_end"]) == 60
    assert len(outcome["modes"]["capture"]["phase_samples_ms"]["end_to_end"]) == 60
    assert outcome["gates"] == gates
    assert outcome["admission"] == admission
    assert outcome["incremental"]["unchanged"]["semantic_parse"] == 0
    assert outcome["incremental"]["unchanged"]["dependency_analysis"] == 0
    assert outcome["incremental"]["unchanged"]["alias_transform"] == 0
    assert outcome["incremental"]["unchanged"]["source_map_composition"] == 0
    assert outcome["incremental"]["unchanged"]["admission"] == 0
    assert outcome["incremental"]["unchanged"]["epf_packaging"] == 0
    assert outcome["incremental"]["unchanged"]["artifact_staging"] == 0
    assert outcome["reference"] == {
        "platform_version": REFERENCE_PLATFORM_VERSION,
        "platform_sha256": REFERENCE_PLATFORM_SHA256,
        "infobase_identity_sha256": REFERENCE_INFOBASE_IDENTITY_SHA256,
        "source_sha256": dict(REFERENCE_SOURCE_SHA256),
        "catalog_sha256": measured_catalog.sha256,
        "extension_sha256": REFERENCE_EXTENSION_SHA256,
        "target_extension_current": True,
        "session_fresh": True,
    }
    assert outcome["compatibility_inventory"] == []
    assert verify_compact_evidence(outcome) == outcome
    persisted_path = tmp_path / "pass-measurement.json"
    persisted = json.loads(persisted_path.read_text("utf-8"))
    assert persisted == outcome
    assert verify_compact_evidence(persisted) == persisted
    assert persisted["measured_iterations"] == 60
    assert set(persisted["modes"]) == {"main", "capture"}
    for mode in ("main", "capture"):
        assert set(persisted["modes"][mode]["phase_samples_ms"]) == set(PHASES)
        assert all(
            len(values) == 60
            for values in persisted["modes"][mode]["phase_samples_ms"].values()
        )
        assert persisted["modes"][mode]["parser_calls"] == {
            "full_module_parses": [1] * 60,
            "delta_method_parses": [0] * 60,
            "packaging_validation_parses": [0] * 60,
        }
    assert len(replacements) == 1
    temporary, destination = replacements[0]
    assert destination == persisted_path
    assert temporary.parent == tmp_path
    assert temporary != destination
    assert not temporary.exists()


def test_verified_live_persists_nonpass_measurements_before_sla_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog_root = tmp_path / "catalog-source"
    common_modules = catalog_root / "CommonModules"
    common_modules.mkdir(parents=True)
    for name in ("КадровыйУчет", "КадровыйУчетРасширенный"):
        (common_modules / f"{name}.xml").write_text(
            _common_module_metadata(name),
            encoding="utf-8",
        )
    measured_catalog = zup_acceptance.SessionCommonModuleCatalog(
        catalog_root,
        profile="runtime-session-server-v1",
        preprocessor_profile="server",
    ).ensure_initialized()
    samples = _phase_samples()
    samples["capture"]["end_to_end"] = [3_000.0] * 60
    result_type = getattr(zup_acceptance, "SemanticAcceptanceResult", None)
    assert result_type is not None
    replacements: list[tuple[Path, Path]] = []

    def record_replace(source: object, destination: object) -> None:
        replacements.append((Path(source), Path(destination)))
        os.replace(source, destination)

    monkeypatch.setattr(
        zup_acceptance,
        "os",
        SimpleNamespace(replace=record_replace, fsync=os.fsync),
        raising=False,
    )

    def semantic_runner(*_args, **_kwargs):
        return result_type(
            admission={
                "catalog_modules": 417,
                "accepted_modules": 2,
                "accepted_bindings": 145,
                "original_targets": 0,
                "same_generation_targets": 145,
                "unsupported": [],
            },
            accounting=_observed_incremental(),
            checks={name: True for name in _pass_gates()},
            catalog_setup_ms=41.0,
            catalog_snapshot=measured_catalog,
            phase_samples=samples,
            parser_call_samples=_parser_call_samples(),
            catalog_extension=_catalog_extension_evidence(),
        )

    with pytest.raises(ProtocolError, match="measured SLA gate"):
        _run_verified_live(
            object(),
            Path("approved-source"),
            iterations=60,
            warmup_iterations=3,
            preflight=ZupStaticPreflight(
                (),
                catalog=measured_catalog,
                extension_sha256=REFERENCE_EXTENSION_SHA256,
            ),
            run_dir=tmp_path,
            _semantic_runner=semantic_runner,
        )

    raw = json.loads((tmp_path / "nonpass-measurement.json").read_text("utf-8"))
    assert set(raw) == {
        "schema",
        "status",
        "sla_claimed",
        "failure_code",
        "counts",
        "catalog_setup_ms",
        "modes",
    }
    assert raw["schema"] == "onec-worker-universe-zup-measurement-nonpass-v1"
    assert raw["status"] == "DONE_WITH_CONCERNS"
    assert raw["sla_claimed"] is False
    assert raw["failure_code"] == "measured_sla_failed"
    assert raw["counts"] == {
        "catalog_modules": 417,
        "warmup_iterations": 3,
        "measured_iterations": 60,
    }
    assert raw["modes"]["capture"]["end_to_end"]["p95_ms"] == 3_000.0
    assert len(raw["modes"]["main"]["phase_samples_ms"]["end_to_end"]) == 60
    assert len(replacements) == 1
    temporary, destination = replacements[0]
    assert destination == tmp_path / "nonpass-measurement.json"
    assert temporary.parent == tmp_path
    assert temporary != destination
    assert not temporary.exists()


def test_verified_live_does_not_persist_nonpass_for_a_non_sla_failure(
    tmp_path: Path,
) -> None:
    catalog_root = tmp_path / "catalog-source"
    common_modules = catalog_root / "CommonModules"
    common_modules.mkdir(parents=True)
    for name in ("КадровыйУчет", "КадровыйУчетРасширенный"):
        (common_modules / f"{name}.xml").write_text(
            _common_module_metadata(name),
            encoding="utf-8",
        )
    measured_catalog = zup_acceptance.SessionCommonModuleCatalog(
        catalog_root,
        profile="runtime-session-server-v1",
        preprocessor_profile="server",
    ).ensure_initialized()
    samples = _phase_samples()
    samples["capture"]["end_to_end"] = [3_000.0] * 60
    result_type = getattr(zup_acceptance, "SemanticAcceptanceResult", None)
    assert result_type is not None
    pass_path = tmp_path / "pass-measurement.json"
    retained = json.dumps(_valid_compact_evidence(), ensure_ascii=False, indent=2)
    pass_path.write_text(retained, encoding="utf-8")

    def semantic_runner(*_args, **_kwargs):
        return result_type(
            admission={
                "catalog_modules": 417,
                "accepted_modules": 3,
                "accepted_bindings": 145,
                "original_targets": 0,
                "same_generation_targets": 145,
                "unsupported": [],
            },
            accounting=_observed_incremental(),
            checks={name: True for name in _pass_gates()},
            catalog_setup_ms=41.0,
            catalog_snapshot=measured_catalog,
            phase_samples=samples,
            parser_call_samples=_parser_call_samples(),
            catalog_extension=_catalog_extension_evidence(),
        )

    with pytest.raises(ProtocolError, match="real-module admission"):
        _run_verified_live(
            object(),
            Path("approved-source"),
            iterations=60,
            warmup_iterations=3,
            preflight=ZupStaticPreflight(
                (),
                catalog=measured_catalog,
                extension_sha256=REFERENCE_EXTENSION_SHA256,
            ),
            run_dir=tmp_path,
            _semantic_runner=semantic_runner,
        )

    assert not (tmp_path / "nonpass-measurement.json").exists()
    assert pass_path.read_text(encoding="utf-8") == retained
