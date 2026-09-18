from hashlib import sha256
from pathlib import Path
from uuid import UUID
from xml.etree import ElementTree

import pytest

from onec_runtime.bsl import SourceUnitKind
from onec_runtime.bsl.diagnostics import VisibleSourceContext
from onec_runtime.bsl.module_universe import analyze_worker_module, lower_worker_module
from onec_runtime.bsl.parser_target import PythonParserTarget, parse_raw_module
from onec_runtime.config import RuntimeConfig
from onec_runtime.server_worker import (
    NotebookWorkerArtifactBuilder,
    stage_worker_module_instruction,
)
from onec_runtime.worker_universe import (
    WorkerModuleArtifactBuilder,
    WorkerModuleArtifactCache,
    WorkerUniverseRegistry,
    prepare_worker_root_instruction,
)
from onec_runtime.worker_stage_protocol import (
    WorkerStageBatch, WorkerStageEntry, stage_worker_batch_instruction,
)

from tests.integration.test_worker_universe_1c import (
    _EXPECTED_PRODUCT_VERSION,
    _RealPhaseFaultExecutor,
    _platform_executable_identities,
    _settings_delete_source,
    _settings_missing_source,
    _transactional_capture_source,
    worker_universe_source_contract,
)


ROOT = Path(__file__).parents[2]
MD = "http://v8.1c.ru/8.3/MDClasses"


def worker_metadata(version: str) -> tuple[str, str]:
    root = ElementTree.parse(
        ROOT / "tests" / "fixtures" / "onec" / "HotReloadWorker" / version / "Worker.xml"
    ).getroot()
    worker = root.find(f"{{{MD}}}ExternalDataProcessor")
    assert worker is not None
    name = worker.findtext(f"{{{MD}}}Properties/{{{MD}}}Name")
    return str(worker.attrib["uuid"]), str(name)


def test_worker_variants_keep_one_platform_identity_but_change_source() -> None:
    assert worker_metadata("v1") == worker_metadata("v2")
    assert worker_metadata("v1")[1] == "Worker"
    roots = [
        ROOT / "tests" / "fixtures" / "onec" / "HotReloadWorker" / version / "Worker.xml"
        for version in ("v1", "v2")
    ]
    canonical_root = ROOT / "onec" / "Worker" / "Worker.xml"
    assert roots[0].read_text(encoding="utf-8") == canonical_root.read_text(
        encoding="utf-8"
    )
    assert roots[0].read_bytes() == roots[1].read_bytes()
    modules = [
        ROOT
        / "tests"
        / "fixtures"
        / "onec"
        / "HotReloadWorker"
        / version
        / "Worker"
        / "Ext"
        / "ObjectModule.bsl"
        for version in ("v1", "v2")
    ]
    hashes = [sha256(path.read_bytes()).hexdigest() for path in modules]
    assert hashes[0] != hashes[1]
    assert 'Возврат "v1"' in modules[0].read_text(encoding="utf-8-sig")
    assert 'Возврат "v2"' in modules[1].read_text(encoding="utf-8-sig")
    for module in modules:
        source = module.read_text(encoding="utf-8-sig")
        assert "Функция Посчитать() Экспорт" in source
        assert "Процедура ИзменитьРезультат(ИзменяемоеЗначение) Экспорт" in source


def test_live_worker_universe_fixture_has_two_independent_module_units() -> None:
    """Break caught: the live proof must never hide A and B in one Worker source."""
    contract = worker_universe_source_contract()

    assert tuple(unit.logical_name for unit in contract.g17_units) == (
        "JupyterBslFixtureCallerServer",
        "JupyterBslFixtureCalleeServer",
    )
    assert all(
        segment.origin_ref.kind is SourceUnitKind.MODULE
        for unit in contract.g17_units
        for segment in unit.mapped_source.source_map.segments
    )
    assert contract.module_a.mapped_source is contract.g18_units[0].mapped_source
    assert (
        contract.module_b_g17.mapped_source.artifact.source_sha256
        != contract.module_b_g18.mapped_source.artifact.source_sha256
    )


def test_live_worker_universe_fixture_exercises_dependency_value_and_source_shapes() -> None:
    """Break caught: call-only fixtures cannot prove dependency value semantics."""
    contract = worker_universe_source_contract()
    source_a = contract.module_a.mapped_source.text
    source_b = contract.module_b_g17.mapped_source.text
    combined = "\n".join((source_a, source_b)).casefold()

    assert "jupyterbslfixturecalleeserver.\u0432\u044b\u043f\u043e\u043b\u043d\u0438\u0442\u044c\u0448\u0430\u0433(" in combined
    assert "\u0442\u0438\u043f\u0437\u043d\u0447(jupyterbslfixturecalleeserver)" in combined
    assert "\u0441\u0442\u0440\u043e\u043a\u0430(jupyterbslfixturecalleeserver)" in combined
    assert "\u0441\u0440\u0430\u0432\u043d\u0438\u0442\u044c\u043f\u043e\u0432\u0442\u043e\u0440\u043d\u044b\u0435\u0441\u0441\u044b\u043b\u043a\u0438(jupyterbslfixturecalleeserver" in combined
    assert "\u043f\u0440\u043e\u0446\u0435\u0434\u0443\u0440\u0430 " in combined
    assert "\u0444\u0443\u043d\u043a\u0446\u0438\u044f " in combined
    assert "\u043f\u0435\u0440\u0435\u043c " in combined
    assert "#\u043e\u0431\u043b\u0430\u0441\u0442\u044c" in combined

    forbidden = (
        "comconnector",
        "win32com",
        "pythoncom",
        "\u043a\u0430\u0434\u0440\u043e\u0432\u044b\u0439\u0443\u0447\u0435\u0442",
        "\u043a\u0430\u0434\u0440\u043e\u0432\u044b\u0439\u0443\u0447\u0435\u0442\u0440\u0430\u0441\u0448\u0438\u0440\u0435\u043d\u043d\u044b\u0439",
        "runtimeworkeractivegeneration",
        "\u0432\u043d\u0435\u0448\u043d\u0438\u0435\u043e\u0431\u0440\u0430\u0431\u043e\u0442\u043a\u0438.\u043f\u043e\u0434\u043a\u043b\u044e\u0447\u0438\u0442\u044c",
    )
    assert not any(marker in combined for marker in forbidden)


def test_live_worker_universe_fixture_lowers_each_cycle_member_independently() -> None:
    """Break caught: a cycle must stay two EPFs with static A/B/self bindings."""
    contract = worker_universe_source_contract()
    parser = PythonParserTarget.from_generated()

    analyses = tuple(
        analyze_worker_module(unit, contract.catalog, parser)
        for unit in contract.g17_units
    )
    lowered = tuple(lower_worker_module(analysis) for analysis in analyses)

    assert [analysis.unit.logical_name for analysis in analyses] == [
        "JupyterBslFixtureCallerServer",
        "JupyterBslFixtureCalleeServer",
    ]
    assert [
        tuple(binding.target_module for binding in analysis.dependencies)
        for analysis in analyses
    ] == [
        (
            "JupyterBslFixtureCalleeServer",
            "JupyterBslFixtureCallerServer",
        ),
        (
            "JupyterBslFixtureCalleeServer",
            "JupyterBslFixtureCallerServer",
        ),
    ]
    assert {
        use.category
        for binding in analyses[0].dependencies
        for use in binding.uses
        if binding.target_module == "JupyterBslFixtureCalleeServer"
    } == {"call", "value"}
    assert lowered[0].mapped_source.artifact.source_sha256 != lowered[1].mapped_source.artifact.source_sha256


def _real_promotion_instruction(tmp_path: Path) -> str:
    contract = worker_universe_source_contract()
    config = RuntimeConfig(
        workspace=tmp_path / "phase-source-contract",
        platform_bin=Path(r"C:\Program Files\1cv8\8.3.27.2170\bin"),
    )
    packer = NotebookWorkerArtifactBuilder(config)
    builder = WorkerModuleArtifactBuilder(
        packer,
        cache=WorkerModuleArtifactCache(),
        packer_version="worker-epf-v1",
        target_profile="runtime-session-server-v1",
    )
    artifacts = []
    for unit in contract.g18_units:
        lowered = lower_worker_module(
            analyze_worker_module(
                unit,
                contract.catalog,
                PythonParserTarget.from_generated(),
            )
        )
        references = {
            reference
            for segment in unit.mapped_source.source_map.segments
            for reference in (segment.origin_ref, segment.anchor_ref)
            if hasattr(reference, "unit_id")
        }
        artifacts.append(
            builder.build(
                lowered,
                visible_source_context=VisibleSourceContext(
                    {
                        reference: unit.mapped_source.text
                        for reference in references
                    }
                ),
            )
        )
    host = WorkerUniverseRegistry(runtime_generation=1, context_generation=1)
    return prepare_worker_root_instruction(
        host.prepare(tuple(artifacts)),
        UUID("11111111-2222-3333-4444-555555555555"),
        "",
    )


def _line_end(source: str, offset: int) -> int:
    newline = source.find("\n", offset)
    return len(source) if newline < 0 else newline


def test_phase_faults_bracket_real_boundary_actions(tmp_path: Path) -> None:
    registration = "OnecRuntime_aaaaaaaa_aaaaaaaaaaaaaaaa"
    stage = stage_worker_module_instruction(
        b"real-artifact-bytes",
        logical_name="ModuleA",
        artifact_sha256="a" * 64,
        registration_name=registration,
    )
    promotion = _real_promotion_instruction(tmp_path)

    for phase in ("upload", "connect"):
        injected = _RealPhaseFaultExecutor(lambda source: source, phase)._inject(stage)
        throw = injected.index(f"task9-{phase}-fault")
        action = (
            injected.index("Base64Значение(")
            if phase == "upload"
            else injected.index(
                f'        АдресАртефактаWorker, "{registration}", Ложь);'
            )
        )
        if phase == "upload":
            assert throw > _line_end(injected, action)
            assert throw < injected.index('ЭтапАртефактаWorker = "connect";')
        else:
            # A classified failed Connect must not create a session-ledger entry.
            assert throw < action
            assert throw < injected.index("\nИсключение\n", action)

    for phase in ("create", "wire", "probe"):
        injected = _RealPhaseFaultExecutor(
            lambda source: source,
            phase,
        )._inject(promotion)
        throw = injected.index(f"task9-{phase}-fault")
        if phase == "create":
            action = injected.index("ВнешниеОбработки.Создать(")
            boundary_end = _line_end(injected, action)
        elif phase == "wire":
            action = injected.index(" = ЦельЗависимостиWorker")
            boundary_end = _line_end(injected, action)
        else:
            check = injected.index("Если ТипЗнч(", injected.index('= "probe";'))
            boundary_end = _line_end(
                injected,
                injected.index("КонецЕсли;", check),
            )
        assert throw > boundary_end


@pytest.mark.parametrize("phase", ("upload", "connect"))
def test_phase_faults_reach_current_batch_boundary(phase: str) -> None:
    stage = stage_worker_batch_instruction(
        WorkerStageBatch(0, (WorkerStageEntry("ModuleA", "a" * 64,
            "OnecRuntime_aaaaaaaa_aaaaaaaaaaaaaaaa", b"real-artifact-bytes"),)),
        batch_count=1, transaction_id=UUID("11111111-2222-3333-4444-555555555555"),
    )
    executor = _RealPhaseFaultExecutor(lambda source: source, phase)
    injected = executor(stage)
    assert executor.injected_source == injected
    throw = injected.index(f"task9-{phase}-fault")
    assert throw > injected.index("ПоместитьВоВременноеХранилище(")
    assert throw < injected.index("ВнешниеОбработки.Подключить(")
    boundary = injected.index('ФазаWorker0 = "connect";')
    assert (throw < boundary) if phase == "upload" else (throw > boundary)


def test_transaction_capture_uses_database_write_before_capture_and_rollback() -> None:
    source = _transactional_capture_source(
        "ДоПаузы = 1;\n"
        "СинтетическийРезультат = RuntimeKernelServer.СинтетическийCapture(100);\n"
        "Результат = ДоПаузы;",
        item_key="task9-item",
        marker="task9-marker",
    )

    write = source.index("ХранилищеОбщихНастроек.Сохранить(")
    read_inside = source.index("ХранилищеОбщихНастроек.Загрузить(")
    capture = source.index("СинтетическийCapture(100)")
    rollback = source.index("ОтменитьТранзакцию();")
    assert write < read_inside < capture < rollback
    assert "e1cRuntimeКонтекст" not in source
    parse_raw_module(source, PythonParserTarget.from_generated())
    parse_raw_module(
        _settings_delete_source("task9-item"),
        PythonParserTarget.from_generated(),
    )
    parse_raw_module(
        _settings_missing_source("task9-item"),
        PythonParserTarget.from_generated(),
    )
    assert "ИмяПользователя()" in _settings_delete_source("task9-item")
    assert "Не ТранзакцияАктивна()" in _settings_missing_source("task9-item")


def test_platform_precondition_records_exact_versions_parents_and_sha(
    tmp_path: Path,
) -> None:
    platform = tmp_path / "platform"
    platform.mkdir()
    payloads = {
        "1cv8.exe": b"client",
        "1cv8c.exe": b"console-client",
        "dbgs.exe": b"debug-server",
    }
    for filename, payload in payloads.items():
        (platform / filename).write_bytes(payload)

    identities = _platform_executable_identities(
        platform,
        version_reader=lambda _path: _EXPECTED_PRODUCT_VERSION,
    )

    assert tuple(item.filename for item in identities) == tuple(payloads)
    assert all(
        item.product_version == _EXPECTED_PRODUCT_VERSION for item in identities
    )
    assert tuple(item.sha256 for item in identities) == tuple(
        sha256(payload).hexdigest() for payload in payloads.values()
    )

    with pytest.raises(ValueError, match="ProductVersion mismatch"):
        _platform_executable_identities(
            platform,
            version_reader=lambda _path: "8.3.24.1667",
        )
    (platform / "dbgs.exe").unlink()
    with pytest.raises(ValueError, match="parent mismatch"):
        _platform_executable_identities(
            platform,
            version_reader=lambda _path: _EXPECTED_PRODUCT_VERSION,
        )
