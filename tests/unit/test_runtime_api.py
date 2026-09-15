from collections import deque
from base64 import b64encode
import gc
from hashlib import sha256
from pathlib import Path
from threading import Event, Lock, Thread
from types import SimpleNamespace
from uuid import UUID
from weakref import ref
import json
import re

import pytest

import onec_runtime_mcp.agent.contracts as agent_contracts
from onec_runtime_mcp.agent.capture_contracts import CaptureFence
from onec_runtime_mcp.agent.onec_values import OnecValueResolver
from onec_runtime_mcp.agent.proxies import ProxyProvenance, ProxyRegistry
from onec_runtime.errors import (
    BslExecutionError,
    PoisonedRuntimeError,
    ProtocolError,
    StaleWorkerGeneration,
    WorkerPromotionOutcomeUnknown,
)
from onec_runtime.breakpoint_workspace import (
    BreakpointWorkspaceController,
    WorkspaceSnapshot,
)
from onec_runtime.performance_profile import PhaseRecorder
from onec_runtime.prototype_runtime import (
    CaptureCellResult,
    CapturedStop,
    DebugStop,
    MainCompletion,
    OperationHandle,
    OperationState,
    PrototypeRuntimeController,
)
from onec_runtime.fault_injection import FaultPoint, InjectedTransportFailure
from onec_runtime.rdbg.models import (
    CollectionCell,
    CollectionRow,
    EvaluationResult,
    DebugTarget,
    ModuleLocation,
    StackFrame,
    StopEvent,
    TargetId,
)
from onec_runtime.recovery import RecoveryPhase
from onec_runtime.runtime_api import (
    OperationSourceMapBundle,
    PrototypeRuntimeApi,
    RuntimeReplyKind,
)
from onec_runtime.bsl import (
    CommonModuleCatalogSnapshot,
    CommonModuleDescriptor,
    CommonModuleScope,
    DiagnosticStage,
    LineIndex,
    MappingConfidence,
    VisibleSourceContext,
    WorkerExport,
    parse_platform_diagnostic,
    remap_platform_diagnostic,
    SemanticNotebookLowerer,
    SessionCommonModuleCatalog,
)
from onec_runtime.session import RuntimeSession
from onec_runtime.bsl.lexer import BslLexError
from onec_runtime.bsl.notebook_cells import split_notebook_cell
from onec_runtime.bsl.parser_target import BslParseError, PythonParserTarget
from onec_runtime.bsl.source_maps import SourceUnitKind, SourceUnitRef, source_sha256
from onec_runtime.bsl.source_maps import mapped_visible_source
from onec_runtime.bsl.module_universe import (
    WorkerModuleUnit,
)
from onec_runtime.bsl.diagnostics import remap_worker_stage_diagnostic
from onec_runtime.config import RuntimeConfig
from onec_runtime.server_worker import (
    NotebookWorkerArtifactBuilder,
    WorkerArtifact,
    build_worker_artifact,
)
from onec_runtime.worker_universe import (
    WorkerGenerationHandle,
    WorkerModuleArtifactBuilder,
    WorkerModuleArtifactCache,
    WorkerUniverseCandidate,
    WorkerUniverseRegistry,
)
from onec_runtime.worker_stage_protocol import (
    WORKER_STAGE_SCHEMA,
    WORKER_STAGE_SCHEMA_VERSION,
)
from onec_runtime.worker_breakpoints import (
    WorkerBreakpointConflict,
    WorkerBreakpointCoordinator,
    WorkerBreakpointReloadOutcome,
    WorkerBreakpointReloadPolicy,
    WorkerBreakpointResolution,
)
from onec_runtime.stop_routing import StopReason
from test_prototype_runtime import (
    CAPTURE_A,
    CAPTURE_B,
    SERVICE,
    USER,
    ScriptedSession,
    evaluation,
)


LOCATION = ModuleLocation(
    "ExtensionModule",
    "",
    UUID("11111111-1111-1111-1111-111111111111"),
    UUID("22222222-2222-2222-2222-222222222222"),
    42,
    "OnecInteractiveRuntime",
)


def _stage_source_identity(
    source: str,
) -> tuple[str, int, int, str, tuple[tuple[str, str, int], ...]]:
    header = re.search(
        r'"onec-worker-stage-batch-receipt", 2, '
        r'"([0-9a-f-]{36})", (\d+), (\d+), "([0-9a-f]{64})", '
        r'СтатусWorker',
        source,
    )
    entries = tuple(
        (
            registration_name,
            artifact_sha256,
            int(item_index),
        )
        for registration_name, artifact_sha256, item_index in re.findall(
            r'Новый Структура\('
            r'"registration_name,artifact_sha256,temp_storage_url", '
            r'"([A-Za-z_][A-Za-z0-9_]*)", "([0-9a-f]{64})", '
            r'АдресАртефактаWorker(\d+)\);',
            source,
        )
    )
    assert header is not None
    assert tuple(item[2] for item in entries) == tuple(range(len(entries)))
    return (
        header.group(1),
        int(header.group(2)),
        int(header.group(3)),
        header.group(4),
        entries,
    )


def _stage_result(
    source: str,
    *,
    failure_phase: str | None = None,
    diagnostic: str = "planned staging failure",
) -> str:
    transaction_id, batch_index, batch_count, batch_digest, entries = (
        _stage_source_identity(source)
    )
    failed = failure_phase is not None
    connected_count = 0 if failed else len(entries)
    return json.dumps(
        {
            "schema": WORKER_STAGE_SCHEMA,
            "schema_version": WORKER_STAGE_SCHEMA_VERSION,
            "transaction_id": transaction_id,
            "batch_index": batch_index,
            "batch_count": batch_count,
            "batch_digest": batch_digest,
            "status": "failed" if failed else "succeeded",
            "connected": [
                {
                    "registration_name": registration_name,
                    "artifact_sha256": artifact_sha256,
                    "temp_storage_url": (
                        f"e1cib/tempstorage/{transaction_id}-{batch_index}-{item_index}"
                        "?seanceId=runtime-api-target"
                    ),
                }
                for registration_name, artifact_sha256, item_index in entries[
                    :connected_count
                ]
            ],
            "failure": (
                False
                if not failed
                else {
                    "item_index": 0,
                    "phase": failure_phase,
                    "outcome": (
                        "known_pre_swap"
                        if failure_phase in {"decode", "upload"}
                        else "registration_outcome_unknown"
                    ),
                    "diagnostic": diagnostic,
                    "orphan_url": (
                        False
                        if failure_phase in {"decode", "upload"}
                        else (
                            f"e1cib/tempstorage/{transaction_id}-{batch_index}-0"
                            "?seanceId=runtime-api-target"
                        )
                    ),
                }
            ),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _prepared_root_result(source: str) -> str:
    transaction = re.search(
        r"onec-worker-prepared-root-receipt-v1\|([0-9a-f-]{36})\|",
        source,
    )
    generation = re.search(r'Вставить\("Generation", (\d+)\);', source)
    manifest = re.search(
        r'Вставить\("ManifestSha256", "([0-9a-f]{64})"\);', source
    )
    root = re.search(
        r'Вставить\("CandidateRootKey", "([^"|]+)"\);', source
    )
    previous = re.search(
        r'Вставить\("PreviousRootKey", "([^"|]*)"\);', source
    )
    assert transaction and generation and manifest and root and previous
    return (
        "onec-worker-prepared-root-receipt-v1|"
        f"{transaction.group(1)}|{generation.group(1)}|{manifest.group(1)}|"
        f"{root.group(1)}|{previous.group(1) or '-'}|13"
    )


def _root_swap_result(source: str) -> str:
    transaction = re.search(
        r"onec-worker-root-swap-receipt-v1\|([0-9a-f-]{36})\|",
        source,
    )
    generation = re.search(
        r'Формат\((\d+), "ЧГ=0; ЧДЦ=0; ЧН=0"\)', source
    )
    identity = re.search(
        r'"([0-9a-f]{64})\|(generation-\d+)\|([^|" ]+)\|1\|"',
        source,
    )
    assert transaction and generation and identity
    return (
        "onec-worker-root-swap-receipt-v1|"
        f"{transaction.group(1)}|{generation.group(1)}|{identity.group(1)}|"
        f"{identity.group(2)}|{identity.group(3)}|1|13|2"
    )


def _root_discard_result(source: str) -> str:
    receipt = re.search(
        r"onec-worker-root-discard-receipt-v1\|[0-9a-f-]{36}\|\d+\|"
        r"[0-9a-f]{64}\|generation-\d+",
        source,
    )
    assert receipt is not None
    return receipt.group(0)


class _UniverseInstructionExecutor:
    def __init__(self) -> None:
        self.sources: list[str] = []
        self.disconnects: list[str] = []
        self.query_results: deque[object] = deque()

    def __call__(self, source: str) -> object:
        self.sources.append(source)
        import re

        if f'"{WORKER_STAGE_SCHEMA}"' in source:
            return _stage_result(source)

        registration = re.search(
            r"OnecRuntime_[0-9a-f]{8}_[0-9a-f]{16}", source
        )
        if "onec-worker-artifact-stage=" in source and "phase=connect" in source:
            assert registration is not None
            return registration.group(0)
        if "onec-worker-root-prepare-stage=" in source:
            return _prepared_root_result(source)
        if "onec-worker-root-swap-stage=guard" in source:
            return _root_swap_result(source)
        if "onec-worker-root-discard-stage=guard" in source:
            return _root_discard_result(source)
        if "\u0412\u043d\u0435\u0448\u043d\u0438\u0435\u041e\u0431\u0440\u0430\u0431\u043e\u0442\u043a\u0438.\u041e\u0442\u043a\u043b\u044e\u0447\u0438\u0442\u044c" in source:
            assert registration is not None
            self.disconnects.append(registration.group(0))
            return True
        if 'Modules.\u041f\u043e\u043b\u0443\u0447\u0438\u0442\u044c("Worker")' in source:
            return self.query_results.popleft()
        raise AssertionError("unexpected Worker universe instruction")


def _common_module_catalog(*names: str) -> CommonModuleCatalogSnapshot:
    return CommonModuleCatalogSnapshot.create(
        profile="server-test",
        preprocessor_profile="server",
        revision=1,
        modules=tuple(
            CommonModuleDescriptor(name, CommonModuleScope.SERVER)
            for name in names
        ),
    )


def _worker_module_unit(
    logical_name: str,
    revision: int,
    catalog: CommonModuleCatalogSnapshot,
) -> WorkerModuleUnit:
    source = (
        "\u0424\u0443\u043d\u043a\u0446\u0438\u044f \u0412\u0435\u0440\u0441\u0438\u044f() \u042d\u043a\u0441\u043f\u043e\u0440\u0442\n"
        f'    \u0412\u043e\u0437\u0432\u0440\u0430\u0442 "{logical_name}-{revision}";\n'
        "\u041a\u043e\u043d\u0435\u0446\u0424\u0443\u043d\u043a\u0446\u0438\u0438\n"
    )
    unit = SourceUnitRef(
        SourceUnitKind.MODULE,
        logical_name,
        revision,
        source_sha256(source),
    )
    return WorkerModuleUnit(
        logical_name,
        "module",
        revision,
        mapped_visible_source(source, unit),
    )


def _worker_module_source_unit(
    logical_name: str,
    revision: int,
    source: str,
) -> WorkerModuleUnit:
    reference = SourceUnitRef(
        SourceUnitKind.MODULE,
        logical_name,
        revision,
        source_sha256(source),
    )
    return WorkerModuleUnit(
        logical_name,
        "module",
        revision,
        mapped_visible_source(source, reference),
    )


def _add_runtime_common_module(source_root: Path, name: str, *, edt: bool = False) -> None:
    directory = source_root / "CommonModules"
    directory.mkdir(parents=True, exist_ok=True)
    if edt:
        directory = directory / name
        directory.mkdir()
        (directory / f"{name}.mdo").write_text(
            '<mdclass:CommonModule xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass">'
            f"<name>{name}</name><server>true</server></mdclass:CommonModule>",
            encoding="utf-8",
        )
        return
    (directory / f"{name}.xml").write_text(
        "<MetaDataObject><CommonModule><Properties>"
        f"<Name>{name}</Name>"
        "<Global>false</Global>"
        "<Server>true</Server>"
        "<ClientManagedApplication>false</ClientManagedApplication>"
        "<ClientOrdinaryApplication>false</ClientOrdinaryApplication>"
        "</Properties></CommonModule></MetaDataObject>",
        encoding="utf-8",
    )


class _CountingModuleArtifactBuilder:
    def __init__(self, wrapped: WorkerModuleArtifactBuilder) -> None:
        self.wrapped = wrapped
        self.calls_by_module: dict[str, int] = {}
        self.lowered_by_module: dict[str, object] = {}

    def build(self, *args: object, **kwargs: object):
        lowered = args[0]
        name = lowered.analysis.unit.logical_name
        self.calls_by_module[name] = self.calls_by_module.get(name, 0) + 1
        self.lowered_by_module[name] = lowered
        return self.wrapped.build(*args, **kwargs)


class _SemanticSnapshotFailureTarget(_UniverseInstructionExecutor):
    def __init__(self) -> None:
        super().__init__()
        self.failure: str | None = None

    def __call__(self, source: str) -> object:
        if self.failure == "staging" and f'"{WORKER_STAGE_SCHEMA}"' in source:
            return _stage_result(
                source,
                failure_phase="upload",
                diagnostic="planned staging failure",
            )
        if (
            self.failure in {"create", "wire"}
            and "onec-worker-root-prepare-stage=" in source
        ):
            raise BslExecutionError(
                "onec-worker-root-prepare-stage="
                f"{self.failure}\nplanned promotion failure"
            )
        if (
            self.failure == "unknown"
            and "onec-worker-root-swap-stage=guard" in source
        ):
            raise TimeoutError("planned unknown promotion outcome")
        if (
            self.failure == "swap_guard"
            and "onec-worker-root-swap-stage=guard" in source
        ):
            raise BslExecutionError(
                "onec-worker-root-swap-stage=guard\n"
                "planned guarded swap failure"
            )
        if (
            self.failure == "discard_unknown"
            and "onec-worker-root-discard-stage=guard" in source
        ):
            raise TimeoutError("planned unknown discard outcome")
        return super().__call__(source)


class _RevisionFailingModuleArtifactBuilder:
    def __init__(self, wrapped: WorkerModuleArtifactBuilder) -> None:
        self.wrapped = wrapped
        self.failed_revision: int | None = None

    def build(self, *args: object, **kwargs: object):
        lowered = args[0]
        if lowered.analysis.unit.revision == self.failed_revision:
            raise OSError("planned semantic artifact build failure")
        return self.wrapped.build(*args, **kwargs)


class _PostBuildFailingModuleArtifactBuilder:
    def __init__(self, wrapped: WorkerModuleArtifactBuilder) -> None:
        self.wrapped = wrapped
        self.failed_module: str | None = None
        self.failed_revisions: set[int] = set()
        self.prune_calls = 0
        self.fail_prune = False

    def build(self, *args: object, **kwargs: object):
        artifact = self.wrapped.build(*args, **kwargs)
        lowered = args[0]
        unit = lowered.analysis.unit
        if (
            unit.revision in self.failed_revisions
            and (
                self.failed_module is None
                or unit.logical_name == self.failed_module
            )
        ):
            raise OSError("planned post-admission artifact build failure")
        return artifact

    def _prune_cache(self, live_keys: frozenset[object]) -> None:
        self.prune_calls += 1
        if self.fail_prune:
            raise RuntimeError("planned cache prune failure")
        self.wrapped._prune_cache(live_keys)  # type: ignore[arg-type]


TARGET = TargetId(
    UUID("33333333-3333-3333-3333-333333333333"),
    "DefAlias",
    UUID("44444444-4444-4444-4444-444444444444"),
    1,
    UUID("55555555-5555-5555-5555-555555555555"),
    "1",
)

_NO_UNIVERSE_RESULT = object()


def _notebook_worker_builder(tmp_path: Path) -> NotebookWorkerArtifactBuilder:
    platform = tmp_path / "platform"
    platform.mkdir(exist_ok=True)
    for executable in ("1cv8.exe", "1cv8c.exe", "dbgs.exe"):
        (platform / executable).write_bytes(b"stub")
    return NotebookWorkerArtifactBuilder(RuntimeConfig(tmp_path, platform))


def _mixed_capture_source(statement: str) -> str:
    return (
        "Процедура ОбновитьMixedCapture(Состояние, Значение)\n"
        "    Состояние.Результат = Значение * 2;\n"
        "КонецПроцедуры;\n"
        f"{statement}"
    )


def test_runtime_worker_export_identity_binds_receiver_and_legacy_none() -> None:
    legacy = (
        WorkerExport("МодульРасчета.Рассчитать", "Рассчитать"),
    )
    qualified = (
        WorkerExport(
            "МодульРасчета.Рассчитать",
            "Рассчитать",
            receiver_module="МодульРасчета",
        ),
    )

    assert PrototypeRuntimeApi._worker_export_identity(legacy) == (
        ("модульрасчета.рассчитать", "рассчитать", None),
    )
    assert PrototypeRuntimeApi._worker_export_identity(qualified) == (
        ("модульрасчета.рассчитать", "рассчитать", "модульрасчета"),
    )


def test_operation_source_map_bundle_keeps_mixed_branches_independent() -> None:
    """Break caught: Worker and statement maps must never be concatenated."""
    source = (
        "Процедура Метод()\nКонецПроцедуры;\n"
        "Результат = 1;"
    )
    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL,
        "agent-cell",
        9,
        source_sha256(source),
    )
    cell = split_notebook_cell(
        PythonParserTarget.from_generated(), source, source_unit=unit
    )

    bundle = OperationSourceMapBundle(unit, cell.worker, cell.statements)

    assert bundle.visible == unit
    assert bundle.worker_candidate is cell.worker
    assert bundle.statement_execution is cell.statements
    assert bundle.worker_candidate is not None
    assert bundle.statement_execution is not None
    assert bundle.worker_candidate.artifact != bundle.statement_execution.artifact
    assert source not in repr(bundle)


def test_execute_bsl_binds_the_exact_supplied_visible_source_unit() -> None:
    """Break caught: runtime preparation must hash-fence caller source identity."""
    source = "Результат = 1;"
    wrong_source = "Результат = 2;"
    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL,
        "agent-cell",
        10,
        source_sha256(wrong_source),
    )
    controller = FakeController()
    api = PrototypeRuntimeApi(controller)

    with pytest.raises(ValueError, match="hash"):
        api.execute_bsl(source, source_unit=unit)

    assert controller.main_sources == []


def test_runtime_passes_mapped_worker_and_visible_context_to_production_builder(
    tmp_path: Path,
) -> None:
    """Break caught: runtime must not downgrade the production Worker branch to text."""
    platform = tmp_path / "platform"
    platform.mkdir()
    for executable in ("1cv8.exe", "1cv8c.exe", "dbgs.exe"):
        (platform / executable).write_bytes(b"stub")

    class RecordingBuilder(NotebookWorkerArtifactBuilder):
        received: tuple[object, object] | None = None

        def __call__(self, source, exports, *, visible_source_context=None):  # type: ignore[no-untyped-def]
            self.received = source, visible_source_context
            return super().__call__(
                source,
                exports,
                visible_source_context=visible_source_context,
            )

    builder = RecordingBuilder(RuntimeConfig(tmp_path, platform))
    controller = FakeController()
    controller.worker_results = deque((True,))
    unit_source = "Процедура Обновить() Экспорт\nКонецПроцедуры;"
    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL,
        "runtime-worker-cell",
        6,
        source_sha256(unit_source),
    )
    api = PrototypeRuntimeApi(controller, notebook_worker_builder=builder)

    reply = api.execute_bsl(unit_source, source_unit=unit)

    assert reply.kind is RuntimeReplyKind.WORKER_LOADED
    assert builder.received is not None
    mapped, context = builder.received
    from onec_runtime.bsl import VisibleSourceContext
    from onec_runtime.bsl.source_maps import MappedSource

    assert isinstance(mapped, MappedSource)
    assert isinstance(context, VisibleSourceContext)
    assert mapped.source_map.map_offset(0).unit == unit
    assert context.line_column(unit, unit_source.index("Конец")) == (2, 1)


def test_notebook_worker_compile_failure_returns_mapped_reply(tmp_path: Path) -> None:
    platform = tmp_path / "platform"
    platform.mkdir()
    for executable in ("1cv8.exe", "1cv8c.exe", "dbgs.exe"):
        (platform / executable).write_bytes(b"stub")
    source = (
        "Процедура Создать()\n"
        "    ОтменитьТрранзакцию();\n"
        "КонецПроцедуры"
    )
    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL, "worker-compile-cell", 1,
        source_sha256(source),
    )
    api = PrototypeRuntimeApi(
        FakeController(),
        notebook_worker_builder=NotebookWorkerArtifactBuilder(
            RuntimeConfig(tmp_path, platform)
        ),
    )

    def reject_artifact(_artifact, **_kwargs):
        position = source.index("ОтменитьТрранзакцию")
        line, column = LineIndex(source).offset_to_line_column(position)
        diagnostic = remap_platform_diagnostic(
            parse_platform_diagnostic(
                f"{{<Неизвестный модуль>({line},{column})}}: "
                "Процедура или функция с указанным именем не определена "
                "(ОтменитьТрранзакцию) [ОшибкаКомпиляцииВстроенногоЯзыка]"
            ),
            mapped_visible_source(source, unit),
            stage=DiagnosticStage.COMPILATION,
            visible_source_context=VisibleSourceContext({unit: source}),
        )
        raise BslExecutionError("raw platform compile failure", diagnostic=diagnostic)

    api._publish_notebook_worker_artifact_locked = reject_artifact

    reply = api.execute_bsl(source, source_unit=unit)

    assert reply.kind is RuntimeReplyKind.SOURCE_FAILED
    assert not reply.succeeded
    assert reply.diagnostic is not None
    assert reply.diagnostic.stage is DiagnosticStage.COMPILATION
    assert reply.diagnostic.visible_location is not None
    assert reply.diagnostic.visible_location.line == 2


def test_mixed_cell_prepares_invalid_statements_before_worker_activation(
    tmp_path: Path,
) -> None:
    """Break caught: a method-first cell must not activate before statement prep."""
    from onec_runtime.bsl.semantic_lowering import SemanticNotebookLowerer

    platform = tmp_path / "platform"
    platform.mkdir()
    for executable in ("1cv8.exe", "1cv8c.exe", "dbgs.exe"):
        (platform / executable).write_bytes(b"stub")
    source = (
        "Процедура Обновить() Экспорт\n"
        "КонецПроцедуры;\n"
        "КонтекстОтладки.Значение = 1;"
    )
    controller = FakeController()
    controller.lowerer = SemanticNotebookLowerer(PythonParserTarget.from_generated())
    controller.worker_results = deque((True,))
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=NotebookWorkerArtifactBuilder(
            RuntimeConfig(tmp_path, platform)
        ),
    )

    reply = api.execute_bsl(source)

    assert reply.kind is RuntimeReplyKind.SOURCE_FAILED
    assert reply.diagnostic is not None
    assert reply.diagnostic.stage is DiagnosticStage.LOWERING
    assert api.worker_generation_handle is None
    assert controller.main_sources == []


def test_mixed_capture_cell_reloads_worker_once_then_executes_statement(
    tmp_path: Path,
) -> None:
    """Break caught: CAPTURE must compose candidate reload and statement dispatch."""
    from onec_runtime.bsl.semantic_lowering import SemanticNotebookLowerer

    platform = tmp_path / "platform"
    platform.mkdir()
    for executable in ("1cv8.exe", "1cv8c.exe", "dbgs.exe"):
        (platform / executable).write_bytes(b"stub")
    builder = NotebookWorkerArtifactBuilder(RuntimeConfig(tmp_path, platform))
    controller = FakeController()
    controller.lowerer = SemanticNotebookLowerer(PythonParserTarget.from_generated())
    controller.worker_results = deque((True,))
    api = PrototypeRuntimeApi(controller, notebook_worker_builder=builder)
    first = api.execute_bsl(
        "Процедура ДоCapture()\nКонецПроцедуры;"
    )
    assert first.kind is RuntimeReplyKind.WORKER_LOADED
    assert api.worker_generation_handle is not None
    assert api.worker_generation_handle.generation == 1

    source = (
        "Процедура ОбновитьMixedCapture(Состояние, Значение)\n"
        "    Состояние.Результат = Значение * 2;\n"
        "КонецПроцедуры;\n"
        "СостояниеMixedCapture = Новый Структура(\"Результат\", 0);\n"
        "ОбновитьMixedCapture(СостояниеMixedCapture, "
        "КонтекстОтладки.Значение);\n"
        "КонтекстОтладки.Значение = СостояниеMixedCapture.Результат;\n"
        "РезультатИнструкции = КонтекстОтладки.Значение;"
    )
    captured = api.execute_bsl("Результат = Capture();")
    assert captured.kind is RuntimeReplyKind.CAPTURED
    controller.operation_id = 41
    controller.stop_sequence = 7
    runtime_generation = controller.runtime_generation
    controller.worker_results = deque((True,))
    observed: list[object] = []

    def persist(provenance: object) -> None:
        assert api.worker_generation_handle is not None
        assert api.worker_generation_handle.generation == 1
        assert controller.capture_sources == []
        observed.append(provenance)

    reply = api.execute_bsl(source, on_execution_provenance=persist)

    assert reply.kind is RuntimeReplyKind.CAPTURE_CELL
    assert reply.result == 778
    assert reply.operation_id == 41
    assert reply.state is OperationState.CAPTURED
    assert controller.state is OperationState.CAPTURED
    assert controller.operation_id == 41
    assert controller.stop_sequence == 7
    assert controller.runtime_generation == runtime_generation
    assert api.worker_generation_handle is not None
    assert api.worker_generation_handle.generation == 2
    assert len(observed) == 1
    assert observed[0].mode == "capture"  # type: ignore[union-attr]
    assert len(controller.capture_sources) == 4
    assert "ОбновитьMixedCapture" in controller.capture_sources[-1]

    controller.worker_results = deque((True,))
    repeated = api.execute_bsl(source)

    assert repeated.kind is RuntimeReplyKind.CAPTURE_CELL
    assert repeated.operation_id == 41
    assert repeated.state is OperationState.CAPTURED
    assert controller.runtime_generation == runtime_generation
    assert api.worker_generation_handle is not None
    assert api.worker_generation_handle.generation == 3
    assert len(controller.capture_sources) == 7


def test_mixed_capture_parse_failure_never_builds_or_mutates_target() -> None:
    """Break caught: invalid statement text must not reach Worker preparation."""
    controller = FakeController()
    controller.state = OperationState.CAPTURED
    controller.operation_id = 17
    builder_calls: list[object] = []

    def builder(*args: object, **kwargs: object) -> WorkerArtifact:
        builder_calls.append((args, kwargs))
        raise AssertionError("builder must not be called")

    api = PrototypeRuntimeApi(controller, notebook_worker_builder=builder)

    reply = api.execute_bsl(_mixed_capture_source("$"))

    assert reply.kind is RuntimeReplyKind.SOURCE_FAILED
    assert reply.diagnostic is not None
    assert reply.diagnostic.stage is DiagnosticStage.PARSING
    assert builder_calls == []
    assert api.worker_generation_handle is None
    assert controller.capture_sources == []
    assert controller.operation_id == 17
    assert controller.state is OperationState.CAPTURED


def test_mixed_capture_build_failure_never_activates_or_dispatches_statement() -> None:
    """Break caught: a failed Worker build must leave CAPTURE target untouched."""
    from onec_runtime.bsl.semantic_lowering import SemanticNotebookLowerer

    controller = FakeController()
    controller.state = OperationState.CAPTURED
    controller.operation_id = 18
    controller.lowerer = SemanticNotebookLowerer(PythonParserTarget.from_generated())

    def builder(*_args: object, **_kwargs: object) -> WorkerArtifact:
        raise OSError("planned Worker build failure")

    api = PrototypeRuntimeApi(controller, notebook_worker_builder=builder)

    with pytest.raises(OSError, match="planned Worker build failure"):
        api.execute_bsl(
            _mixed_capture_source(
                "РезультатИнструкции = ОбновитьMixedCapture;"
            )
        )

    assert api.worker_generation_handle is None
    assert controller.capture_sources == []
    assert controller.operation_id == 18
    assert controller.state is OperationState.CAPTURED


def test_mixed_capture_lowering_failure_keeps_visible_statement_coordinates(
    tmp_path: Path,
) -> None:
    """Break caught: candidate-aware lowering must finish before Worker activation."""
    from onec_runtime.bsl.semantic_lowering import SemanticNotebookLowerer

    controller = FakeController()
    controller.state = OperationState.CAPTURED
    controller.operation_id = 19
    controller.lowerer = SemanticNotebookLowerer(PythonParserTarget.from_generated())
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=_notebook_worker_builder(tmp_path),
    )

    reply = api.execute_bsl(
        _mixed_capture_source("РезультатИнструкции = КонтекстОтладки;")
    )

    assert reply.kind is RuntimeReplyKind.SOURCE_FAILED
    assert reply.diagnostic is not None
    assert reply.diagnostic.stage is DiagnosticStage.LOWERING
    assert reply.diagnostic.mapping_confidence is MappingConfidence.EXACT
    assert reply.diagnostic.visible_location is not None
    assert reply.diagnostic.visible_location.line == 4
    assert api.worker_generation_handle is None
    assert controller.capture_sources == []
    assert controller.operation_id == 19
    assert controller.state is OperationState.CAPTURED


def test_mixed_capture_provenance_failure_precedes_worker_activation(
    tmp_path: Path,
) -> None:
    """Break caught: exact provenance must be durable before the first target mutation."""
    from onec_runtime.bsl.semantic_lowering import SemanticNotebookLowerer

    controller = FakeController()
    controller.state = OperationState.CAPTURED
    controller.operation_id = 20
    controller.lowerer = SemanticNotebookLowerer(PythonParserTarget.from_generated())
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=_notebook_worker_builder(tmp_path),
    )

    with pytest.raises(OSError, match="planned provenance failure"):
        api.execute_bsl(
            _mixed_capture_source(
                "РезультатИнструкции = ОбновитьMixedCapture;"
            ),
            on_execution_provenance=lambda _value: (_ for _ in ()).throw(
                OSError("planned provenance failure")
            ),
        )

    assert api.worker_generation_handle is None
    assert controller.capture_sources == []
    assert controller.operation_id == 20
    assert controller.state is OperationState.CAPTURED


def test_mixed_capture_activation_failure_does_not_dispatch_statement(
    tmp_path: Path,
) -> None:
    """Break caught: a failed reload must not fall through into statement execution."""
    from onec_runtime.bsl.semantic_lowering import SemanticNotebookLowerer

    controller = FakeController()
    controller.lowerer = SemanticNotebookLowerer(PythonParserTarget.from_generated())
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=_notebook_worker_builder(tmp_path),
    )
    controller.worker_results = deque((True,))
    api.execute_bsl("Процедура ДоCapture()\nКонецПроцедуры;")
    previous = api.worker_generation_handle
    assert previous is not None

    captured = api.execute_bsl("Результат = Capture();")
    assert captured.kind is RuntimeReplyKind.CAPTURED
    controller.operation_id = 21
    controller.stop_sequence = 4
    controller.fail_worker_promotion = True
    source = _mixed_capture_source(
        "РезультатИнструкции = ОбновитьMixedCapture;"
    )

    with pytest.raises(WorkerPromotionOutcomeUnknown):
        api.execute_bsl(source)

    assert api.worker_generation_handle == previous
    assert len(controller.capture_sources) == 3
    assert source not in controller.capture_sources
    assert controller.operation_id == 21
    assert controller.stop_sequence == 4
    assert controller.state is OperationState.CAPTURED


@pytest.mark.parametrize(
    "fence_field",
    ("runtime_generation", "operation_id", "stop_sequence"),
)
def test_mixed_capture_activation_fails_closed_when_capture_fence_changes(
    tmp_path: Path,
    fence_field: str,
) -> None:
    """Break caught: Worker activation must not dispatch into a different stop."""
    from onec_runtime.bsl.semantic_lowering import SemanticNotebookLowerer

    controller = FakeController()
    controller.lowerer = SemanticNotebookLowerer(PythonParserTarget.from_generated())
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=_notebook_worker_builder(tmp_path),
    )
    controller.worker_results = deque((True,))
    api.execute_bsl("Процедура ДоCapture()\nКонецПроцедуры;")

    captured = api.execute_bsl("Результат = Capture();")
    assert captured.kind is RuntimeReplyKind.CAPTURED
    controller.operation_id = 23
    controller.stop_sequence = 6
    controller.worker_results = deque((True,))
    system_calls = 0
    original = controller.execute_system_capture

    def drift_after_activation(source: str) -> CaptureCellResult:
        nonlocal system_calls
        result = original(source)
        system_calls += 1
        if system_calls == 1:
            setattr(controller, fence_field, getattr(controller, fence_field) + 1)
        return result

    controller.execute_system_capture = drift_after_activation  # type: ignore[method-assign]

    expected_error = (
        WorkerPromotionOutcomeUnknown
        if fence_field == "runtime_generation"
        else ProtocolError
    )
    expected_message = (
        "promotion outcome is unknown"
        if fence_field == "runtime_generation"
        else "CAPTURE fence changed"
    )
    with pytest.raises(expected_error, match=expected_message):
        api.execute_bsl(
            _mixed_capture_source(
                "РезультатИнструкции = ОбновитьMixedCapture;"
            )
        )

    assert api.worker_generation_handle is not None
    assert api.worker_generation_handle.generation == (
        1 if fence_field == "runtime_generation" else 2
    )
    assert system_calls == (1 if fence_field == "runtime_generation" else 3)
    assert len(controller.capture_sources) == (
        1 if fence_field == "runtime_generation" else 3
    )


def test_mixed_capture_statement_failure_keeps_committed_worker_and_recovers(
    tmp_path: Path,
) -> None:
    """Break caught: post-reload BSL failure must not roll back Worker generation."""
    from onec_runtime.bsl.semantic_lowering import SemanticNotebookLowerer

    controller = FakeController()
    controller.lowerer = SemanticNotebookLowerer(PythonParserTarget.from_generated())
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=_notebook_worker_builder(tmp_path),
    )
    controller.worker_results = deque((True,))
    api.execute_bsl("Процедура ДоCapture()\nКонецПроцедуры;")
    assert api.worker_generation_handle is not None
    assert api.worker_generation_handle.generation == 1

    captured = api.execute_bsl("Результат = Capture();")
    assert captured.kind is RuntimeReplyKind.CAPTURED
    controller.operation_id = 22
    controller.stop_sequence = 5
    controller.worker_results = deque((True,))

    def fail_statement(source: str, **kwargs: object) -> CaptureCellResult:
        dispatch = kwargs.get("on_transport_dispatch")
        assert callable(dispatch)
        dispatch()
        controller.capture_sources.append(source)
        raise BslExecutionError("planned statement failure")

    controller.execute_capture = fail_statement  # type: ignore[method-assign]
    failed = api.execute_bsl(
        _mixed_capture_source(
            "КонтекстОтладки.Значение = 84;\n"
            "РезультатИнструкции = ОбновитьMixedCapture;"
        )
    )

    assert failed.kind is RuntimeReplyKind.CAPTURE_CELL
    assert failed.succeeded is False
    assert failed.error == "BSL execution failed"
    assert api.worker_generation_handle is not None
    assert api.worker_generation_handle.generation == 2
    assert controller.operation_id == 22
    assert controller.stop_sequence == 5
    assert controller.state is OperationState.CAPTURED
    assert len(controller.capture_sources) == 4

    controller.execute_capture = FakeController.execute_capture.__get__(  # type: ignore[method-assign]
        controller,
        FakeController,
    )
    recovered = api.execute_bsl(
        "РезультатИнструкции = КонтекстОтладки.Значение;"
    )
    completed = api.resume_capture()

    assert recovered.kind is RuntimeReplyKind.CAPTURE_CELL
    assert recovered.succeeded is True
    assert completed.kind is RuntimeReplyKind.MAIN_COMPLETED
    assert controller.resume_roots == [("Значение",)]


def test_mixed_capture_execution_diagnostic_maps_to_exact_visible_statement(
    tmp_path: Path,
) -> None:
    """Break caught: the statement branch must retain the mixed cell source unit."""
    from onec_runtime.bsl.semantic_lowering import SemanticNotebookLowerer

    controller = FakeController()
    controller.lowerer = SemanticNotebookLowerer(PythonParserTarget.from_generated())
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=_notebook_worker_builder(tmp_path),
    )
    controller.worker_results = deque((True,))
    api.execute_bsl("Процедура ДоCapture()\nКонецПроцедуры;")
    captured = api.execute_bsl("Результат = Capture();")
    assert captured.kind is RuntimeReplyKind.CAPTURED
    controller.operation_id = 24
    controller.stop_sequence = 8
    controller.worker_results = deque((True,))
    source = _mixed_capture_source(
        "КонтекстОтладки.Значение = 901;\nОшибкаMixed = 1 / 0;"
    )
    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL,
        "mixed-capture-diagnostic",
        4,
        source_sha256(source),
    )

    def fail_mapped(
        visible: str,
        lowered,
        *,
        visible_source_context: VisibleSourceContext,
        **_kwargs: object,
    ) -> CaptureCellResult:
        controller.capture_sources.append(visible)
        offset = lowered.text.index("/")
        line, column = LineIndex(lowered.text).offset_to_line_column(offset)
        diagnostic = remap_platform_diagnostic(
            parse_platform_diagnostic(
                f"{{<Неизвестный модуль>({line},{column})}}: division by zero"
            ),
            lowered,
            stage=DiagnosticStage.EXECUTION,
            visible_source_context=visible_source_context,
        )
        raise BslExecutionError("planned statement failure", diagnostic=diagnostic)

    controller.execute_mapped_capture = fail_mapped  # type: ignore[attr-defined]

    failed = api.execute_bsl(source, source_unit=unit)

    assert failed.succeeded is False
    assert failed.diagnostic is not None
    assert failed.diagnostic.mapping_confidence is MappingConfidence.EXACT
    assert failed.diagnostic.source_unit == unit
    assert failed.diagnostic.visible_location is not None
    assert failed.diagnostic.visible_location.line == 5


def test_main_capture_preparation_seals_both_branches_before_worker_activation(
    tmp_path: Path,
) -> None:
    """Break caught: capture arming must follow Worker build and MAIN lowering."""
    from onec_runtime.bsl.semantic_lowering import SemanticNotebookLowerer

    platform = tmp_path / "platform"
    platform.mkdir()
    for executable in ("1cv8.exe", "1cv8c.exe", "dbgs.exe"):
        (platform / executable).write_bytes(b"stub")
    source = (
        "Функция Capture() Экспорт\n"
        "Возврат 42;\n"
        "КонецФункции;\n"
        "Результат = Capture();"
    )

    class CountingLowerer(SemanticNotebookLowerer):
        lower_calls = 0

        def lower_mapped(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            self.lower_calls += 1
            return super().lower_mapped(*args, **kwargs)

    class CountingBuilder(NotebookWorkerArtifactBuilder):
        build_calls = 0
        artifact = None

        def __call__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            self.build_calls += 1
            self.artifact = super().__call__(*args, **kwargs)
            return self.artifact

    controller = FakeController()
    controller.lowerer = CountingLowerer(PythonParserTarget.from_generated())
    controller.worker_results = deque((True,))
    builder = CountingBuilder(RuntimeConfig(tmp_path, platform))
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=builder,
    )

    prepared = api.prepare_main_for_capture(source)
    provenance = api.prepared_main_execution_provenance(prepared)

    assert repr(prepared) == "<redacted prepared main execution>"
    assert api.worker_generation_handle is None
    assert controller.main_sources == []
    assert controller.lowerer.worker_export_identity == ()
    assert builder.build_calls == 1
    assert controller.lowerer.lower_calls == 1
    assert builder.artifact is not None
    assert provenance.visible_source_sha256 == source_sha256(source)
    assert provenance.mode == "main"
    assert provenance.worker_generation == (
        builder.artifact.source_provenance.worker_generation
    )
    assert provenance.worker_manifest_sha256 == (
        builder.artifact.source_provenance.worker_manifest_sha256
    )
    assert source not in repr(provenance)
    activated = api.activate_prepared_main_for_capture(prepared)
    assert repr(activated) == "<redacted activated main execution>"
    assert api.worker_generation_handle is not None
    assert builder.build_calls == 1
    assert controller.lowerer.lower_calls == 1
    api.configure_capture_points((LOCATION,))
    ticket = api.prepare_capture_ticket()
    assert ticket.expected_operation_id == controller.operation_id + 1
    with pytest.raises(ProtocolError, match="activated prepared main"):
        api.execute_prepared_main_for_capture(prepared)
    with pytest.raises(ProtocolError, match="already consumed"):
        api.activate_prepared_main_for_capture(prepared)
    reply = api.execute_prepared_main_for_capture(activated)
    assert reply.kind is RuntimeReplyKind.CAPTURED
    assert reply.operation_id == ticket.expected_operation_id
    assert reply.capture_ticket == ticket.ticket_id
    assert api.worker_generation_handle is not None
    assert controller.main_sources
    assert builder.build_calls == 1
    assert controller.lowerer.lower_calls == 1
    with pytest.raises(ProtocolError, match="already consumed"):
        api.execute_prepared_main_for_capture(activated)
    other = PrototypeRuntimeApi(controller)
    with pytest.raises(ProtocolError, match="owned prepared main"):
        other.activate_prepared_main_for_capture(prepared)
    with pytest.raises(ProtocolError, match="owned activated main"):
        other.execute_prepared_main_for_capture(activated)


def test_activated_main_drift_consumes_capability_without_reactivation() -> None:
    """Break caught: restoring a drifted fence must not make phase two reusable."""
    from onec_runtime.bsl.semantic_lowering import SemanticNotebookLowerer

    controller = FakeController()
    controller.lowerer = SemanticNotebookLowerer(PythonParserTarget.from_generated())
    api = PrototypeRuntimeApi(controller)
    prepared = api.prepare_main_for_capture("Результат = 1;")
    activated = api.activate_prepared_main_for_capture(prepared)

    controller.operation_id = 1
    with pytest.raises(ProtocolError, match="stale"):
        api.execute_prepared_main_for_capture(activated)
    controller.operation_id = 0
    with pytest.raises(ProtocolError, match="already consumed"):
        api.execute_prepared_main_for_capture(activated)

    assert controller.main_sources == []
    assert api.worker_generation_handle is None


def test_controller_exception_after_user_main_entry_has_authoritative_dispatch_evidence() -> None:
    """Break caught: only RuntimeApi may prove that controller MAIN was entered."""
    from copy import deepcopy

    from onec_runtime_mcp.agent.contracts import to_wire
    from onec_runtime.bsl.semantic_lowering import SemanticNotebookLowerer

    secret = "controller_pid=9182 token=do-not-persist"

    class RaisingController(FakeController):
        def execute_main(self, source: str, **kwargs: object) -> MainCompletion:
            dispatch = kwargs.get("on_transport_dispatch")
            assert callable(dispatch)
            dispatch()
            self.main_sources.append(source)
            raise BslExecutionError(secret, messages=(secret,))

    controller = RaisingController()
    controller.lowerer = SemanticNotebookLowerer(PythonParserTarget.from_generated())
    api = PrototypeRuntimeApi(controller)
    activated = api.activate_prepared_main_for_capture(
        api.prepare_main_for_capture("Результат = 1;")
    )

    attempt = api._attempt_prepared_main_for_capture(activated)

    assert attempt.user_main_dispatched is True
    assert repr(attempt) == "<redacted prepared main execution attempt>"
    assert deepcopy(attempt) == "<redacted prepared main execution attempt>"
    with pytest.raises(TypeError, match="wire-safe"):
        to_wire(attempt)
    with pytest.raises(BslExecutionError, match="controller_pid"):
        attempt.reply()
    assert controller.main_sources == ["Результат = 1;"]
    assert secret not in repr(attempt)


def test_discard_activated_main_consumes_owner_capability_without_target_call() -> None:
    """Break caught: a setup return must not leave phase two reusable."""
    from onec_runtime.bsl.semantic_lowering import SemanticNotebookLowerer

    controller = FakeController()
    controller.lowerer = SemanticNotebookLowerer(PythonParserTarget.from_generated())
    api = PrototypeRuntimeApi(controller)
    other = PrototypeRuntimeApi(controller)
    prepared = api.prepare_main_for_capture("\u0420\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442 = 1;")
    activated = api.activate_prepared_main_for_capture(prepared)

    with pytest.raises(ProtocolError, match="owned activated main"):
        other.discard_prepared_main_for_capture(activated)
    controller.operation_id = 1
    api.discard_prepared_main_for_capture(activated)
    controller.operation_id = 0

    with pytest.raises(ProtocolError, match="already consumed"):
        api.execute_prepared_main_for_capture(activated)
    with pytest.raises(ProtocolError, match="already consumed"):
        api.discard_prepared_main_for_capture(activated)
    assert controller.main_sources == []
    assert api.worker_generation_handle is None


def test_prepared_main_drift_consumes_capability_before_worker_activation() -> None:
    """Break caught: a stale phase-one token must never become activatable again."""
    from onec_runtime.bsl.semantic_lowering import SemanticNotebookLowerer

    controller = FakeController()
    controller.lowerer = SemanticNotebookLowerer(PythonParserTarget.from_generated())
    api = PrototypeRuntimeApi(controller)
    prepared = api.prepare_main_for_capture("Результат = 1;")

    controller.operation_id = 1
    with pytest.raises(ProtocolError, match="stale"):
        api.activate_prepared_main_for_capture(prepared)
    controller.operation_id = 0
    with pytest.raises(ProtocolError, match="already consumed"):
        api.activate_prepared_main_for_capture(prepared)

    assert controller.main_sources == []
    assert api.worker_generation_handle is None


@pytest.mark.parametrize(
    ("source", "stage", "code"),
    (
        ("$", DiagnosticStage.PARSING, "unexpected_character"),
        (
            "Результат = КонтекстОтладки.Скаляр;",
            DiagnosticStage.LOWERING,
            "capture_namespace_mode",
        ),
    ),
)
def test_deterministic_source_failure_returns_nonexecuted_mapped_reply(
    source: str,
    stage: DiagnosticStage,
    code: str,
) -> None:
    """Break caught: known source errors must not escape or call the target."""
    from onec_runtime.bsl.semantic_lowering import SemanticNotebookLowerer

    controller = FakeController()
    controller.lowerer = SemanticNotebookLowerer(PythonParserTarget.from_generated())
    api = PrototypeRuntimeApi(controller)

    reply = api.execute_bsl(source)

    assert reply.kind is RuntimeReplyKind.SOURCE_FAILED
    assert reply.succeeded is False
    assert reply.operation_id == 0
    assert reply.state is OperationState.COMPLETED
    assert reply.diagnostic is not None
    assert reply.diagnostic.stage is stage
    assert reply.diagnostic.code == code
    assert reply.diagnostic.mapping_confidence is MappingConfidence.EXACT
    assert reply.diagnostic.visible_location is not None
    assert reply.diagnostic.visible_location.line == 1
    assert controller.main_sources == []
    assert controller.capture_sources == []


def test_execute_bsl_journaling_callback_precedes_exact_main_dispatch() -> None:
    """Break caught: MAIN target dispatch can precede durable exact provenance."""
    from onec_runtime.bsl.semantic_lowering import SemanticNotebookLowerer

    class RecordingLowerer(SemanticNotebookLowerer):
        last = None

        def lower_mapped(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            self.last = super().lower_mapped(*args, **kwargs)
            return self.last

    source = "Результат = 1;"
    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL,
        "agent-main-cell",
        4,
        source_sha256(source),
    )
    controller = FakeController()
    lowerer = RecordingLowerer(PythonParserTarget.from_generated())
    controller.lowerer = lowerer
    api = PrototypeRuntimeApi(controller)
    observed: list[object] = []

    def persist(provenance: object) -> None:
        assert controller.main_sources == []
        assert controller.capture_sources == []
        assert api.worker_generation_handle is None
        observed.append(provenance)

    reply = api.execute_bsl(
        source,
        source_unit=unit,
        on_execution_provenance=persist,
    )

    assert reply.kind is RuntimeReplyKind.MAIN_COMPLETED
    assert len(observed) == 1
    provenance = observed[0]
    assert isinstance(provenance, agent_contracts.OperationExecutionProvenance)
    assert lowerer.last is not None
    assert provenance == agent_contracts.OperationExecutionProvenance(
        visible_source_sha256=unit.source_sha256,
        executed_source_sha256=(
            lowerer.last.mapped_source.artifact.source_sha256
        ),
        source_map_sha256=lowerer.last.mapped_source.source_map_sha256,
        mode="main",
    )


def test_execute_bsl_provenance_journal_failure_aborts_before_target_mutation() -> None:
    """Break caught: a failed provenance fsync is swallowed after MAIN dispatch."""
    from onec_runtime.bsl.semantic_lowering import SemanticNotebookLowerer

    controller = FakeController()
    lowerer = SemanticNotebookLowerer(PythonParserTarget.from_generated())
    controller.lowerer = lowerer
    api = PrototypeRuntimeApi(controller)

    with pytest.raises(OSError, match="provenance fsync failed"):
        api.execute_bsl(
            "Результат = 1;",
            on_execution_provenance=lambda _value: (_ for _ in ()).throw(
                OSError("provenance fsync failed")
            ),
        )

    assert controller.main_sources == []
    assert controller.capture_sources == []
    assert api.worker_generation_handle is None
    assert lowerer.persistent_names == ()


def test_api_rejects_unmapped_main_controller_without_test_compatibility() -> None:
    """Break caught: missing mapped MAIN support must not select text by inference."""
    from onec_runtime.bsl.semantic_lowering import SemanticNotebookLowerer

    controller = FakeController()
    controller.execute_mapped_main = None  # type: ignore[method-assign]
    controller.lowerer = SemanticNotebookLowerer(PythonParserTarget.from_generated())
    lowered_calls: list[str] = []

    def execute_lowered_main(
        visible: str,
        lowered: str,
        **_kwargs: object,
    ) -> MainCompletion:
        lowered_calls.append(lowered)
        return MainCompletion(OperationHandle(1, visible, lowered), None, "", True)

    controller.execute_lowered_main = execute_lowered_main  # type: ignore[attr-defined]
    api = PrototypeRuntimeApi(controller)

    with pytest.raises(ProtocolError, match="mapped MAIN"):
        api.execute_bsl("Результат = 1;")

    assert lowered_calls == []


def test_api_rejects_unmapped_capture_controller_without_test_compatibility() -> None:
    """Break caught: missing mapped CAPTURE support must not dispatch lowered text."""
    from onec_runtime.bsl.semantic_lowering import SemanticNotebookLowerer

    controller = FakeController()
    controller.execute_mapped_capture = None  # type: ignore[method-assign]
    controller.state = OperationState.CAPTURED
    controller.operation_id = 1
    controller.lowerer = SemanticNotebookLowerer(PythonParserTarget.from_generated())
    lowered_calls: list[str] = []

    def execute_lowered_capture(
        visible: str,
        lowered: str,
        **_kwargs: object,
    ) -> CaptureCellResult:
        lowered_calls.append(lowered)
        return CaptureCellResult(1, visible, lowered, None)

    controller.execute_lowered_capture = execute_lowered_capture  # type: ignore[attr-defined]
    api = PrototypeRuntimeApi(controller)

    with pytest.raises(ProtocolError, match="mapped CAPTURE"):
        api.execute_bsl("КонтекстОтладки.Скаляр = 2;")

    assert lowered_calls == []


def test_prepared_capture_rejects_unmapped_controller_without_test_compatibility() -> None:
    """Break caught: prepared artifacts must keep mapped dispatch mandatory."""
    from onec_runtime.bsl.semantic_lowering import SemanticNotebookLowerer

    controller = FakeController()
    controller.execute_mapped_capture = None  # type: ignore[method-assign]
    controller.state = OperationState.CAPTURED
    controller.operation_id = 1
    controller.stop_sequence = 1
    controller.cell_sequence = 0
    controller.lowerer = SemanticNotebookLowerer(PythonParserTarget.from_generated())
    controller.message_collector_key = lambda _mode: "__messages"  # type: ignore[attr-defined]
    lowered_calls: list[str] = []

    def execute_lowered_capture(
        visible: str,
        lowered: str,
        **_kwargs: object,
    ) -> CaptureCellResult:
        lowered_calls.append(lowered)
        return CaptureCellResult(1, visible, lowered, None)

    controller.execute_lowered_capture = execute_lowered_capture  # type: ignore[attr-defined]
    api = PrototypeRuntimeApi(controller)
    prepared = api.prepare_capture_hypothesis("КонтекстОтладки.Скаляр = 2;")
    provenance = api.prepared_capture_hypothesis_provenance(prepared)

    assert provenance.mode == "capture"
    assert provenance.visible_source_sha256 == source_sha256(
        "КонтекстОтладки.Скаляр = 2;"
    )

    with pytest.raises(ProtocolError, match="mapped CAPTURE"):
        api.execute_prepared_capture_hypothesis(prepared)

    assert lowered_calls == []


def test_projection_instruction_is_bounded_and_stages_only_projected_rows() -> None:
    source = PrototypeRuntimeApi._projection_instruction(
        "Контекст.Таблица",
        context_key="__onec_projection_" + "a" * 32,
        kind="table_rows",
        offset=20,
        limit=10,
        columns=("Сотрудник", "Сумма"),
        names=(),
    )

    assert "Для ИндексПроекции = 20 По Мин(Контекст.Таблица.Количество() - 1, 29)" in source
    assert 'Скопировать(СтрокиПроекции, "Сотрудник,Сумма")' in source
    assert source.index("RuntimeValueTransferServer.ДопуститьЗначение(") < source.index(
        "Для ИндексПроекции"
    )
    assert source.index("RuntimeTableTransferServer.СериализоватьКомпактнуюТаблицу(") < source.index(
        "Контекст.Вставить"
    )


class FakeController:
    runtime_generation = 1
    _supports_worker_universe_receipts = True

    def __init__(self) -> None:
        self.state = OperationState.COMPLETED
        self.operation_id = 0
        self.stop_sequence = 0
        self.main_sources: list[str] = []
        self.capture_sources: list[str] = []
        self.resume_roots: list[tuple[str, ...]] = []
        self.capture_points_seen: list[tuple[ModuleLocation, ...]] = []
        self.worker_results = deque(
            ("v1", "v2", "v2")
        )
        self.context_reads: list[tuple[str, int]] = []
        self.context_drops: list[str] = []
        self.context_value = ""
        self.table_sample_calls: list[tuple[str, int]] = []
        self.table_declared_schema_calls: list[str] = []
        self.fail_worker_promotion = False
        self.worker_pin_installs: list[str] = []
        self.worker_pin_clears = 0

    def execute_main(self, source: str, **kwargs: object) -> MainCompletion | CapturedStop:
        dispatch = kwargs.get("on_transport_dispatch")
        if callable(dispatch):
            dispatch()
        self.main_sources.append(source)
        self.capture_points_seen.append(
            tuple(kwargs.get("capture_points", ()))  # type: ignore[arg-type]
        )
        self.operation_id += 1
        self.stop_sequence = 0
        operation = OperationHandle(self.operation_id, source, source)
        if "Capture" in source:
            self.state = OperationState.CAPTURED
            self.stop_sequence += 1
            return CapturedStop(
                operation, LOCATION, self.stop_sequence, (), self.operation_id
            )
        self.state = OperationState.COMPLETED
        return MainCompletion(operation, 42, "", True)

    def execute_capture(self, source: str, **kwargs: object) -> CaptureCellResult:
        dispatch = kwargs.get("on_transport_dispatch")
        if callable(dispatch):
            dispatch()
        self.capture_sources.append(source)
        return CaptureCellResult(self.operation_id, source, source, 778)

    def execute_mapped_main(
        self,
        _visible_source: str,
        mapped_source: object,
        **kwargs: object,
    ) -> MainCompletion | CapturedStop:
        return self.execute_main(mapped_source.text, **kwargs)  # type: ignore[attr-defined]

    def execute_mapped_capture(
        self,
        _visible_source: str,
        mapped_source: object,
        **kwargs: object,
    ) -> CaptureCellResult:
        return self.execute_capture(mapped_source.text, **kwargs)  # type: ignore[attr-defined]

    def resume(
        self,
        *,
        dirty_roots: tuple[str, ...] = (),
        on_transport_dispatch=None,
        **_kwargs: object,
    ) -> MainCompletion:
        if callable(on_transport_dispatch):
            on_transport_dispatch()
        self.resume_roots.append(dirty_roots)
        self.state = OperationState.COMPLETED
        operation = OperationHandle(self.operation_id, "main", "main")
        return MainCompletion(operation, None, "", True)

    def resume_debug_stop(self, **kwargs: object) -> DebugStop:
        dispatch = kwargs.get("on_transport_dispatch")
        if callable(dispatch):
            dispatch()
        operation = OperationHandle(self.operation_id, "main", "main")
        stop = StopEvent(TARGET, LOCATION, "callStackFormed")
        return DebugStop(operation, stop, StopReason.USER_BREAKPOINT)

    def execute_system_main(self, source: str) -> MainCompletion:
        self.main_sources.append(source)
        self.operation_id += 1
        operation = OperationHandle(self.operation_id, source, source)
        result = self._worker_universe_result(source)
        if result is _NO_UNIVERSE_RESULT:
            result = self.worker_results.popleft()
        return MainCompletion(operation, result, "", True)

    def execute_system_capture(self, source: str) -> CaptureCellResult:
        self.capture_sources.append(source)
        result = self._worker_universe_result(source)
        if result is _NO_UNIVERSE_RESULT:
            result = self.worker_results.popleft()
        return CaptureCellResult(
            self.operation_id,
            source,
            source,
            result,
        )

    def install_capture_worker_generation_pin(self, manifest_sha256: str) -> None:
        self.worker_pin_installs.append(manifest_sha256)

    def clear_capture_worker_generation_pin(self) -> None:
        self.worker_pin_clears += 1

    def _worker_universe_result(self, source: str) -> object:
        import re

        if f'"{WORKER_STAGE_SCHEMA}"' in source:
            return _stage_result(source)
        registration = re.search(
            r"OnecRuntime_[0-9a-f]{8}_[0-9a-f]{16}", source
        )
        if "onec-worker-artifact-stage=" in source and "phase=connect" in source:
            assert registration is not None
            return registration.group(0)
        if "onec-worker-root-prepare-stage=" in source:
            return _prepared_root_result(source)
        if "onec-worker-root-swap-stage=guard" in source:
            if self.fail_worker_promotion:
                raise TimeoutError("planned Worker promotion response loss")
            return _root_swap_result(source)
        if "onec-worker-root-discard-stage=guard" in source:
            return _root_discard_result(source)
        if "\u0412\u043d\u0435\u0448\u043d\u0438\u0435\u041e\u0431\u0440\u0430\u0431\u043e\u0442\u043a\u0438.\u041e\u0442\u043a\u043b\u044e\u0447\u0438\u0442\u044c" in source:
            return True
        return _NO_UNIVERSE_RESULT

    def take_context_string(self, key: str, *, max_text_size: int) -> str:
        self.context_reads.append((key, max_text_size))
        return self.context_value

    def drop_context_value(self, key: str) -> None:
        self.context_drops.append(key)

    def inspect_table_sample(
        self, handle: str, *, page_size: int = 16
    ) -> EvaluationResult:
        self.table_sample_calls.append((handle, page_size))
        return EvaluationResult(
            UUID("66666666-6666-6666-6666-666666666666"),
            "ТаблицаЗначений",
            "ТаблицаЗначений",
            False,
            collection_size=1,
            collection_rows=(
                CollectionRow(
                    0,
                    (CollectionCell("Имя", "Строка", "А", value_string="А"),),
                ),
            ),
        )

    def inspect_declared_table_schema(self, handle: str) -> EvaluationResult:
        self.table_declared_schema_calls.append(handle)
        return EvaluationResult(
            UUID("77777777-7777-7777-7777-777777777777"),
            "ТаблицаЗначений",
            "ТаблицаЗначений",
            False,
            collection_size=1,
            collection_rows=(
                CollectionRow(
                    0,
                    (
                        CollectionCell("Имя", "Строка", '"Имя"', value_string="Имя"),
                        CollectionCell("Вид", "Строка", '"nullable_string"', value_string="nullable_string"),
                        CollectionCell("Ссылка", "Булево", "Ложь", value_boolean=False),
                    ),
                ),
            ),
        )


def artifact(tmp_path: Path, version: str, value: int) -> WorkerArtifact:
    path = tmp_path / f"Worker-{version}.epf"
    path.write_bytes(version.encode("ascii"))
    source = tmp_path / f"Worker-{version}.bsl"
    exports = (
        WorkerExport("Расчет.Ндфл.Посчитать", "Посчитать"),
        WorkerExport("Расчет.Ндфл.ИзменитьРезультат", "ИзменитьРезультат"),
        WorkerExport("Расчет.Ндфл.Версия", "Версия"),
    )
    source.write_text(
        "\n".join(
            f"Функция {export.method}() Экспорт\n"
            "    Возврат Неопределено;\n"
            "КонецФункции;"
            for export in exports
        ),
        encoding="utf-8",
    )
    return build_worker_artifact(
        logical_name="Worker",
        source_path=source,
        artifact_path=path,
        expected_version=version,
        expected_value=value,
        exports=exports,
    )


def test_api_routes_main_then_capture_cell_and_preserves_operation() -> None:
    controller = FakeController()
    api = PrototypeRuntimeApi(controller, capture_points=(LOCATION,))

    captured = api.execute_bsl("Результат = Capture();")
    capture_cell = api.execute_bsl("Локальная = 778;")
    completed = api.resume_capture(dirty_roots=("Скаляр",))

    assert captured.kind is RuntimeReplyKind.CAPTURED
    assert capture_cell.kind is RuntimeReplyKind.CAPTURE_CELL
    assert completed.kind is RuntimeReplyKind.MAIN_COMPLETED
    assert captured.operation_id == capture_cell.operation_id == completed.operation_id == 1
    assert capture_cell.result == 778
    assert controller.resume_roots == [("Скаляр",)]


def test_api_routes_ordinary_main_and_reports_failed_completion() -> None:
    controller = FakeController()
    api = PrototypeRuntimeApi(controller)

    completed = api.execute_bsl("ГДФЛ = Расчет.Ндфл.Посчитать();")

    assert completed.kind is RuntimeReplyKind.MAIN_COMPLETED
    assert completed.result == 42
    assert completed.succeeded is True


def test_api_arms_capture_points_for_the_next_main_execution() -> None:
    controller = FakeController()
    api = PrototypeRuntimeApi(controller)
    second = ModuleLocation(
        LOCATION.module_type,
        LOCATION.url,
        LOCATION.object_id,
        LOCATION.property_id,
        LOCATION.line + 1,
        LOCATION.extension_name,
    )

    api.configure_capture_points((LOCATION, second))
    api.execute_bsl("Результат = Capture();")

    assert controller.capture_points_seen == [(LOCATION, second)]


def test_capture_ticket_prepares_expected_controller_operation_and_marks_actual_stop() -> None:
    controller = FakeController()
    api = PrototypeRuntimeApi(controller, capture_points=(LOCATION,))

    ticket = api.prepare_capture_ticket()
    reply = api.execute_bsl("Результат = Capture();")

    assert ticket.expected_operation_id == 1
    assert ticket.expected_stop_sequence == 1
    assert reply.operation_id == ticket.expected_operation_id
    assert reply.capture_ticket == ticket.ticket_id


def test_capture_ticket_is_not_attached_to_a_delayed_controller_stop() -> None:
    controller = FakeController()

    def delayed(source: str, **kwargs: object) -> CapturedStop:
        del kwargs
        controller.state = OperationState.CAPTURED
        return CapturedStop(OperationHandle(999, source, source), LOCATION, 1, ())

    controller.execute_main = delayed  # type: ignore[method-assign]
    api = PrototypeRuntimeApi(controller, capture_points=(LOCATION,))

    ticket = api.prepare_capture_ticket()
    reply = api.execute_bsl("Результат = Capture();")

    assert reply.operation_id == 999
    assert reply.capture_ticket is None
    assert reply.operation_id != ticket.expected_operation_id


def test_each_new_main_capture_ticket_expects_first_stop_after_prior_capture_resumes() -> None:
    controller = FakeController()
    api = PrototypeRuntimeApi(controller, capture_points=(LOCATION,))

    first_ticket = api.prepare_capture_ticket()
    first = api.execute_bsl("Результат = Capture();")
    api.resume_capture()
    api.configure_capture_points((LOCATION,))
    second_ticket = api.prepare_capture_ticket()
    second = api.execute_bsl("Результат = Capture();")

    assert (first_ticket.expected_stop_sequence, first.stop_sequence) == (1, 1)
    assert (second_ticket.expected_stop_sequence, second.stop_sequence) == (1, 1)
    assert first.operation_id == 1
    assert second.operation_id == 2


@pytest.mark.parametrize(
    "state",
    [
        OperationState.MAIN_PENDING,
        OperationState.CAPTURED,
        OperationState.DEBUG_STOPPED,
    ],
)
def test_api_rejects_capture_reconfiguration_outside_main_ready(
    state: OperationState,
) -> None:
    controller = FakeController()
    controller.state = state
    api = PrototypeRuntimeApi(controller)

    with pytest.raises(ProtocolError, match="main-ready"):
        api.configure_capture_points((LOCATION,))


def test_api_rejects_duplicate_and_non_positive_capture_points() -> None:
    api = PrototypeRuntimeApi(FakeController())
    invalid = ModuleLocation(
        LOCATION.module_type,
        LOCATION.url,
        LOCATION.object_id,
        LOCATION.property_id,
        0,
        LOCATION.extension_name,
    )

    with pytest.raises(ProtocolError, match="duplicate"):
        api.configure_capture_points((LOCATION, LOCATION))
    with pytest.raises(ProtocolError, match="positive"):
        api.configure_capture_points((invalid,))


def test_api_rejects_overlapping_mutation() -> None:
    entered = Event()
    release = Event()
    controller = FakeController()
    original = controller.execute_main

    def blocking(source: str, **kwargs: object) -> MainCompletion | CapturedStop:
        entered.set()
        release.wait(timeout=2)
        return original(source, **kwargs)

    controller.execute_main = blocking  # type: ignore[method-assign]
    api = PrototypeRuntimeApi(controller)
    thread = Thread(target=lambda: api.execute_bsl("Первый = 1;"))
    thread.start()
    assert entered.wait(timeout=1)
    try:
        with pytest.raises(ProtocolError, match="already executing"):
            api.execute_bsl("Второй = 2;")
    finally:
        release.set()
        thread.join(timeout=2)


def test_status_is_read_only_and_reports_generation_and_worker(tmp_path: Path) -> None:
    catalog = _common_module_catalog("МодульА")
    api = _semantic_snapshot_runtime(tmp_path, catalog)
    handle = api.load_worker_modules(
        (_worker_module_unit("МодульА", 1, catalog),),
        common_modules=catalog,
    )

    status = api.status()

    assert status.state is OperationState.COMPLETED
    assert status.runtime_generation == 1
    assert status.operation_id == api._controller.operation_id
    assert status.worker_generation == handle


def test_status_reads_capture_control_plane_while_api_writer_is_owned() -> None:
    """Break caught: status must not wait on or enter the data-plane writer."""
    from test_capture_evaluation_lifecycle import (
        ControlledCaptureSession,
        close_owner,
    )
    from test_prototype_runtime import captured_controller

    transport = ControlledCaptureSession()
    controller = captured_controller(transport, command_timeout_s=2.0)
    api = PrototypeRuntimeApi(controller)
    initiator_errors: list[BaseException] = []
    observer_errors: list[BaseException] = []
    observed = []

    def initiate() -> None:
        try:
            api.execute_bsl("РезультатИнструкции = 901;")
        except BaseException as error:
            initiator_errors.append(error)

    def observe() -> None:
        try:
            observed.append(api.status())
        except BaseException as error:
            observer_errors.append(error)

    initiator = Thread(target=initiate, name="capture-status-initiator")
    observer = Thread(target=observe, name="capture-status-observer")
    try:
        initiator.start()
        assert transport.accepted.wait(1)
        controller.state = OperationState.CAPTURED
        assert api._lock.acquire(blocking=False)
        try:
            observer.start()
            observer.join(0.2)
        finally:
            api._lock.release()

        assert not observer.is_alive(), "status waited behind the API writer"
        assert observer_errors == []
        assert len(observed) == 1
        assert observed[0].state is OperationState.EVALUATING_CAPTURE
        assert initiator_errors == []
    finally:
        if transport.capture_pending is not None:
            transport.complete()
        initiator.join(2)
        observer.join(2)
        assert not initiator.is_alive()
        assert not observer.is_alive()
        close_owner(controller, transport)


def _catalog_snapshot(
    revision: int,
    *modules: tuple[str, CommonModuleScope],
    profile: str = "server-test",
    preprocessor_profile: str = "server",
) -> CommonModuleCatalogSnapshot:
    return CommonModuleCatalogSnapshot.create(
        profile=profile,
        preprocessor_profile=preprocessor_profile,
        revision=revision,
        modules=tuple(
            CommonModuleDescriptor(name, scope) for name, scope in modules
        ),
    )


def _semantic_snapshot_runtime(
    tmp_path: Path,
    catalog: CommonModuleCatalogSnapshot,
    *,
    target: _UniverseInstructionExecutor | None = None,
    builder: object | None = None,
) -> PrototypeRuntimeApi:
    packer = _notebook_worker_builder(tmp_path)
    module_builder = (
        WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        )
        if builder is None
        else builder
    )
    return PrototypeRuntimeApi(
        FakeController(),
        notebook_worker_builder=packer,
        worker_module_builder=module_builder,
        worker_instruction_executor=(
            _UniverseInstructionExecutor() if target is None else target
        ),
    )


class _BreakpointWorkspaceSession:
    def __init__(self) -> None:
        self.calls: list[tuple[ModuleLocation, ...]] = []
        self.fail_on_call: int | None = None

    def set_breakpoints(self, locations: tuple[ModuleLocation, ...]) -> None:
        self.calls.append(locations)
        if len(self.calls) == self.fail_on_call:
            raise TimeoutError("planned breakpoint workspace response loss")


def _attach_breakpoint_workspace(controller: FakeController):
    session = _BreakpointWorkspaceSession()
    owner = BreakpointWorkspaceController(
        session,
        WorkspaceSnapshot(0, LOCATION, (), (), (), False),
    )
    controller.breakpoint_workspace_owner = owner
    controller.install_worker_workspace = owner.install
    return owner, session


def _install_logical_breakpoint(api: PrototypeRuntimeApi, *, line: int = 2) -> UUID:
    view = api._worker_universe._retained_debug_views()[0]
    module = view.modules[0]
    plan = api._worker_breakpoints.prepare_add(
        module.source_unit,
        module.canonical_module,
        line,
        enabled=True,
        column=None,
    )
    owner = api._controller.breakpoint_workspace_owner
    current = owner.confirmed_snapshot
    desired = owner.prepare(
        captures=current.captures,
        ordinary_users=current.ordinary_users,
        worker_slots=plan.desired_slots,
        shielded=current.shielded,
    )
    api._controller.install_worker_workspace(desired)
    api._worker_breakpoints.commit(plan)
    return plan.result_id


def test_public_logical_breakpoint_mutations_install_then_commit(
    tmp_path: Path,
) -> None:
    catalog = _common_module_catalog("МодульА")
    packer = _notebook_worker_builder(tmp_path)
    controller = FakeController()
    owner, workspace_session = _attach_breakpoint_workspace(controller)
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=_UniverseInstructionExecutor(),
    )
    api.load_worker_modules(
        (_worker_module_unit("МодульА", 17, catalog),),
        common_modules=catalog,
    )
    view = api._worker_universe._retained_debug_views()[0]
    module = view.modules[0]
    calls_before = len(workspace_session.calls)

    status = api.add_worker_breakpoint(
        module.source_unit,
        module.canonical_module,
        2,
    )

    assert status.resolution is WorkerBreakpointResolution.RESOLVED
    assert status.installed_binding_count == 1
    assert len(owner.confirmed_snapshot.worker_slots) == 1
    assert len(workspace_session.calls) == calls_before + 1
    duplicate = api.add_worker_breakpoint(
        module.source_unit,
        module.canonical_module,
        2,
    )
    assert duplicate.breakpoint.id == status.breakpoint.id
    assert len(workspace_session.calls) == calls_before + 1

    disabled = api.set_worker_breakpoint_enabled(status.breakpoint.id, False)
    assert disabled.enabled is False
    assert owner.confirmed_snapshot.worker_slots == ()
    api.remove_worker_breakpoint(status.breakpoint.id)
    api.remove_worker_breakpoint(status.breakpoint.id)
    assert api.list_worker_breakpoints() == ()

    with pytest.raises(ProtocolError, match="unknown"):
        api.worker_breakpoint_status(UUID(int=999))


def test_public_breakpoint_mutation_is_busy_during_capture_evaluation_stop() -> None:
    controller = FakeController()
    _attach_breakpoint_workspace(controller)
    controller.state = OperationState.CAPTURE_DEBUG_STOPPED
    api = PrototypeRuntimeApi(controller)
    source_unit = SourceUnitRef(
        SourceUnitKind.MODULE,
        "МодульА",
        1,
        "a" * 64,
    )

    with pytest.raises(ProtocolError, match="stable runtime boundary"):
        api.add_worker_breakpoint(source_unit, "МодульА", 2)

    assert api.list_worker_breakpoints() == ()


def test_breakpoint_strict_rejects_candidate_without_mutating_root_or_catalog(
    tmp_path: Path,
) -> None:
    catalog = _common_module_catalog("МодульА")
    target = _SemanticSnapshotFailureTarget()
    controller = FakeController()
    owner, _session = _attach_breakpoint_workspace(controller)
    packer = _notebook_worker_builder(tmp_path)
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=target,
    )
    old_handle = api.load_worker_modules(
        (_worker_module_unit("МодульА", 1, catalog),),
        common_modules=catalog,
    )
    breakpoint_id = _install_logical_breakpoint(api)
    old_catalog = api._worker_breakpoints.snapshot()
    old_workspace = owner.confirmed_snapshot

    with pytest.raises(WorkerBreakpointConflict):
        api.load_worker_modules(
            (_worker_module_unit("МодульА", 2, catalog),),
            common_modules=catalog,
            breakpoint_policy=WorkerBreakpointReloadPolicy.STRICT,
        )

    assert api._worker_universe.active_handle is old_handle
    assert api._worker_breakpoints.snapshot() is old_catalog
    assert api._worker_breakpoints.status(breakpoint_id).enabled is True
    assert owner.confirmed_snapshot is old_workspace
    report = api.last_worker_breakpoint_reload_report()
    assert report is not None
    assert report.outcome is WorkerBreakpointReloadOutcome.ABORTED
    assert report.committed_removals == ()


def test_breakpoint_strict_discard_uncertainty_quarantines_runtime(
    tmp_path: Path,
) -> None:
    catalog = _common_module_catalog("МодульА")
    target = _SemanticSnapshotFailureTarget()
    controller = FakeController()
    _attach_breakpoint_workspace(controller)
    packer = _notebook_worker_builder(tmp_path)
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=target,
    )
    api.load_worker_modules(
        (_worker_module_unit("МодульА", 1, catalog),),
        common_modules=catalog,
    )
    _install_logical_breakpoint(api)
    target.failure = "discard_unknown"

    with pytest.raises(WorkerPromotionOutcomeUnknown):
        api.load_worker_modules(
            (_worker_module_unit("МодульА", 2, catalog),),
            common_modules=catalog,
            breakpoint_policy=WorkerBreakpointReloadPolicy.STRICT,
        )

    assert api._worker_active_modules == {}
    report = api.last_worker_breakpoint_reload_report()
    assert report is not None
    assert report.outcome is WorkerBreakpointReloadOutcome.QUARANTINED
    with pytest.raises(WorkerPromotionOutcomeUnknown):
        api.status()


def test_pre_workspace_failure_with_discard_uncertainty_quarantines_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _common_module_catalog("МодульА")
    target = _SemanticSnapshotFailureTarget()
    controller = FakeController()
    _attach_breakpoint_workspace(controller)
    packer = _notebook_worker_builder(tmp_path)
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=target,
    )
    api.load_worker_modules(
        (_worker_module_unit("МодульА", 1, catalog),),
        common_modules=catalog,
    )
    _install_logical_breakpoint(api)

    def fail_plan(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("planned breakpoint mapping failure")

    monkeypatch.setattr(WorkerBreakpointCoordinator, "prepare_generation", fail_plan)
    target.failure = "discard_unknown"

    with pytest.raises(WorkerPromotionOutcomeUnknown):
        api.load_worker_modules(
            (_worker_module_unit("МодульА", 2, catalog),),
            common_modules=catalog,
        )

    assert api._worker_active_modules == {}
    report = api.last_worker_breakpoint_reload_report()
    assert report is not None
    assert report.outcome is WorkerBreakpointReloadOutcome.QUARANTINED
    with pytest.raises(WorkerPromotionOutcomeUnknown):
        api.status()


def test_breakpoint_reset_abort_restores_workspace_without_catalog_deletion(
    tmp_path: Path,
) -> None:
    catalog = _common_module_catalog("МодульА")
    target = _SemanticSnapshotFailureTarget()
    controller = FakeController()
    owner, session = _attach_breakpoint_workspace(controller)
    packer = _notebook_worker_builder(tmp_path)
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=target,
    )
    old_handle = api.load_worker_modules(
        (_worker_module_unit("МодульА", 1, catalog),),
        common_modules=catalog,
    )
    breakpoint_id = _install_logical_breakpoint(api)
    old_catalog = api._worker_breakpoints.snapshot()
    old_effective = owner.confirmed_snapshot.effective_locations
    target.failure = "swap_guard"

    with pytest.raises(BslExecutionError, match="guarded swap failure"):
        api.load_worker_modules(
            (_worker_module_unit("МодульА", 2, catalog),),
            common_modules=catalog,
            breakpoint_policy=WorkerBreakpointReloadPolicy.RESET_INCOMPATIBLE,
        )

    assert api._worker_universe.active_handle is old_handle
    assert api._worker_breakpoints.snapshot() is old_catalog
    assert api._worker_breakpoints.status(breakpoint_id).enabled is True
    assert owner.confirmed_snapshot.effective_locations == old_effective
    assert session.calls[-1] == old_effective
    report = api.last_worker_breakpoint_reload_report()
    assert report is not None
    assert report.outcome is WorkerBreakpointReloadOutcome.ABORTED
    assert tuple(item.breakpoint_id for item in report.planned_removals) == (
        breakpoint_id,
    )
    assert report.committed_removals == ()


def test_breakpoint_reset_commits_deletion_only_after_guarded_swap(
    tmp_path: Path,
) -> None:
    catalog = _common_module_catalog("МодульА")
    target = _SemanticSnapshotFailureTarget()
    controller = FakeController()
    owner, _session = _attach_breakpoint_workspace(controller)
    packer = _notebook_worker_builder(tmp_path)
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=target,
    )
    api.load_worker_modules(
        (_worker_module_unit("МодульА", 1, catalog),),
        common_modules=catalog,
    )
    breakpoint_id = _install_logical_breakpoint(api)

    new_handle = api.load_worker_modules(
        (_worker_module_unit("МодульА", 2, catalog),),
        common_modules=catalog,
        breakpoint_policy=WorkerBreakpointReloadPolicy.RESET_INCOMPATIBLE,
    )

    assert api._worker_universe.active_handle is new_handle
    assert api._worker_breakpoints.list_statuses() == ()
    assert owner.confirmed_snapshot.worker_slots == ()
    report = api.last_worker_breakpoint_reload_report()
    assert report is not None
    assert report.outcome is WorkerBreakpointReloadOutcome.COMMITTED
    assert report.committed_removals == (breakpoint_id,)


@pytest.mark.parametrize("failure", ["workspace", "unknown"])
def test_breakpoint_reset_unknown_outcome_quarantines_without_claiming_deletion(
    tmp_path: Path,
    failure: str,
) -> None:
    catalog = _common_module_catalog("МодульА")
    target = _SemanticSnapshotFailureTarget()
    controller = FakeController()
    owner, session = _attach_breakpoint_workspace(controller)
    packer = _notebook_worker_builder(tmp_path)
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=target,
    )
    api.load_worker_modules(
        (_worker_module_unit("МодульА", 1, catalog),),
        common_modules=catalog,
    )
    breakpoint_id = _install_logical_breakpoint(api)
    old_catalog = api._worker_breakpoints.snapshot()
    if failure == "workspace":
        session.fail_on_call = len(session.calls) + 1
    else:
        target.failure = "unknown"

    with pytest.raises((TimeoutError, WorkerPromotionOutcomeUnknown, ProtocolError)):
        api.load_worker_modules(
            (_worker_module_unit("МодульА", 2, catalog),),
            common_modules=catalog,
            breakpoint_policy=WorkerBreakpointReloadPolicy.RESET_INCOMPATIBLE,
        )

    assert api._worker_breakpoints.snapshot() is old_catalog
    assert api._worker_breakpoints.status(breakpoint_id).enabled is True
    report = api.last_worker_breakpoint_reload_report()
    assert report is not None
    assert report.outcome is WorkerBreakpointReloadOutcome.QUARANTINED
    assert report.committed_removals == ()
    with pytest.raises(ProtocolError):
        api.status()


def test_breakpoint_release_keeps_shared_slot_and_master_registration(
    tmp_path: Path,
) -> None:
    catalog = _common_module_catalog("МодульА")
    target = _SemanticSnapshotFailureTarget()
    controller = FakeController()
    owner, _session = _attach_breakpoint_workspace(controller)
    packer = _notebook_worker_builder(tmp_path)
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=target,
    )
    unit = _worker_module_unit("МодульА", 1, catalog)
    first = api.load_worker_modules((unit,), common_modules=catalog)
    breakpoint_id = _install_logical_breakpoint(api)
    api.release_worker_generation(first)

    second = api.load_worker_modules(
        (unit,),
        common_modules=catalog,
        breakpoint_policy=WorkerBreakpointReloadPolicy.STRICT,
    )

    assert api._worker_universe.active_handle is second
    assert api._worker_breakpoints.status(breakpoint_id).installed_binding_count == 1
    assert len(owner.confirmed_snapshot.worker_slots) == 1
    retained_views = api._worker_universe._retained_debug_views()
    assert api._worker_breakpoints._views == retained_views
    assert tuple(view.handle for view in retained_views) == (second,)
    assert len(api._worker_universe_target._registrations) == 1


def test_post_publication_breakpoint_reconciliation_failure_keeps_new_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _common_module_catalog("МодульА")
    target = _SemanticSnapshotFailureTarget()
    controller = FakeController()
    _owner, _session = _attach_breakpoint_workspace(controller)
    packer = _notebook_worker_builder(tmp_path)
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=target,
    )
    unit = _worker_module_unit("МодульА", 1, catalog)
    first = api.load_worker_modules((unit,), common_modules=catalog)
    _install_logical_breakpoint(api)
    api.release_worker_generation(first)

    original_install = api._install_worker_breakpoint_workspace
    install_calls = 0

    def fail_after_swap(snapshot: WorkspaceSnapshot) -> object:
        nonlocal install_calls
        install_calls += 1
        if install_calls == 2:
            raise RuntimeError("planned post-swap workspace reconciliation failure")
        return original_install(snapshot)

    monkeypatch.setattr(
        api,
        "_install_worker_breakpoint_workspace",
        fail_after_swap,
    )

    with pytest.raises(
        PoisonedRuntimeError,
        match="post-publication breakpoint reconciliation",
    ):
        api.load_worker_modules(
            (unit,),
            common_modules=catalog,
            breakpoint_policy=WorkerBreakpointReloadPolicy.STRICT,
        )

    current = api._worker_universe.active_handle
    assert current is not None and current is not first
    assert api._worker_generation_handle is current
    assert api._api_owned_worker_generation_handle is current
    retained = api._worker_universe._confirmed_live_inventory()
    assert retained is not None
    assert retained.manifest_sha256s == frozenset((current.manifest_sha256,))
    assert len(api._worker_universe_target._registrations) == 1
    with pytest.raises(PoisonedRuntimeError):
        api.status()


@pytest.mark.parametrize(
    ("failure_point", "quarantined_plan_count"),
    (("prepare_release", 0), ("workspace_owner", 1)),
)
def test_post_publication_breakpoint_preparation_failure_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
    quarantined_plan_count: int,
) -> None:
    """Every post-swap reconciliation preparation failure preserves G2 roots."""
    catalog = _common_module_catalog("МодульА")
    target = _SemanticSnapshotFailureTarget()
    controller = FakeController()
    _owner, _session = _attach_breakpoint_workspace(controller)
    packer = _notebook_worker_builder(tmp_path)
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=target,
    )
    unit = _worker_module_unit("МодульА", 1, catalog)
    first = api.load_worker_modules((unit,), common_modules=catalog)
    _install_logical_breakpoint(api)
    pin = api._worker_universe.pin_active()
    api.release_worker_generation(first)
    assert api._api_owned_worker_generation_handle is None

    prune_calls = 0
    original_prune = api._prune_worker_caches_locked

    def track_prune() -> None:
        nonlocal prune_calls
        prune_calls += 1
        original_prune()

    release_calls = 0
    original_release = api._release_worker_lifecycle_locked

    def track_release(release: WorkerGenerationHandle | object) -> None:
        nonlocal release_calls
        release_calls += 1
        original_release(release)  # type: ignore[arg-type]

    monkeypatch.setattr(api, "_prune_worker_caches_locked", track_prune)
    monkeypatch.setattr(api, "_release_worker_lifecycle_locked", track_release)
    if failure_point == "prepare_release":

        def fail_before_plan(
            _coordinator: WorkerBreakpointCoordinator,
            _views: object,
        ) -> object:
            raise RuntimeError("planned reconciliation plan preparation failure")

        monkeypatch.setattr(
            WorkerBreakpointCoordinator,
            "prepare_release",
            fail_before_plan,
        )
    else:
        original_owner = api._worker_breakpoint_workspace_owner
        owner_calls = 0

        def fail_owner_after_plan() -> BreakpointWorkspaceController:
            nonlocal owner_calls
            owner_calls += 1
            if owner_calls == 2:
                raise RuntimeError("planned reconciliation workspace owner failure")
            return original_owner()

        monkeypatch.setattr(
            api,
            "_worker_breakpoint_workspace_owner",
            fail_owner_after_plan,
        )

    with pytest.raises(
        PoisonedRuntimeError,
        match="post-publication breakpoint reconciliation",
    ):
        api.load_worker_modules(
            (unit,),
            common_modules=catalog,
            breakpoint_policy=WorkerBreakpointReloadPolicy.STRICT,
        )

    current = api._worker_universe.active_handle
    assert current is not None and current is not first
    assert api._worker_generation_handle is current
    assert api._api_owned_worker_generation_handle is current
    retained = api._worker_universe._confirmed_live_inventory()
    assert retained is not None
    assert retained.manifest_sha256s == frozenset((
        first.manifest_sha256,
        current.manifest_sha256,
    ))
    lease = api._worker_universe._leases[pin.lease_id]
    assert lease.pin is pin
    assert api._worker_universe._generations[first.generation].operation_pins == 1
    assert api._worker_active_modules == {}
    assert prune_calls == 0
    assert release_calls == 0
    assert len(api._worker_breakpoints._quarantined_plans) == quarantined_plan_count
    with pytest.raises(PoisonedRuntimeError):
        api.status()

    api.close()


def test_active_module_cache_publishes_only_successful_source_models(
    tmp_path: Path,
) -> None:
    """Break caught: only successfully published source/model state survives."""
    catalog = _catalog_snapshot(1, ("МодульА", CommonModuleScope.SERVER))
    revision_one = _worker_module_unit("МодульА", 1, catalog)
    revision_two = _worker_module_unit("МодульА", 2, catalog)
    api = _semantic_snapshot_runtime(tmp_path, catalog)

    api.load_worker_modules((revision_one,), common_modules=catalog)
    first = api._worker_active_modules["модульа"]
    assert first.unit is revision_one

    api.load_worker_modules((revision_one,), common_modules=catalog)
    assert api._worker_active_modules["модульа"].model is first.model
    assert api._worker_active_modules["модульа"].artifact is first.artifact

    api.load_worker_modules((revision_two,), common_modules=catalog)
    second = api._worker_active_modules["модульа"]
    assert second is not first
    assert second.unit is revision_two

    api.load_worker_modules((revision_one,), common_modules=catalog)
    assert api._worker_active_modules["модульа"].unit is revision_one


def test_changed_source_does_not_call_the_legacy_delta_builder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: the active reload path must not consult method-delta state."""
    import onec_runtime.bsl.module_delta as module_delta

    catalog = _catalog_snapshot(1, ("МодульА", CommonModuleScope.SERVER))
    api = _semantic_snapshot_runtime(tmp_path, catalog)
    api.load_worker_modules(
        (_worker_module_unit("МодульА", 1, catalog),),
        common_modules=catalog,
    )

    def reject_delta(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("legacy method delta entered the active path")

    monkeypatch.setattr(
        module_delta,
        "try_build_worker_semantic_delta",
        reject_delta,
    )
    api.load_worker_modules(
        (_worker_module_unit("МодульА", 2, catalog),),
        common_modules=catalog,
    )

    assert api._worker_active_modules["модульа"].unit.revision == 2


def test_changed_source_does_not_call_a_second_module_parser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: full-AST model extraction must not trigger another parse."""
    import onec_runtime.bsl.module_universe as module_universe

    catalog = _catalog_snapshot(1, ("МодульА", CommonModuleScope.SERVER))
    api = _semantic_snapshot_runtime(tmp_path, catalog)
    api.load_worker_modules(
        (_worker_module_unit("МодульА", 1, catalog),),
        common_modules=catalog,
    )
    def reject_full_ast(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("secondary module parser entered the active path")

    monkeypatch.setattr(module_universe, "parse_raw_module", reject_full_ast)

    candidate = _worker_module_unit("МодульА", 2, catalog)
    api.load_worker_modules((candidate,), common_modules=catalog)

    assert api._worker_active_modules["модульа"].unit is candidate


def test_signature_change_runs_one_full_ast_parse_without_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: a structural edit must still perform one full-AST parse."""
    import onec_runtime.runtime_api as runtime_api
    from onec_runtime.bsl.full_ast_worker_projection import parse_full_ast_module

    catalog = _catalog_snapshot(1, ("МодульА", CommonModuleScope.SERVER))
    api = _semantic_snapshot_runtime(tmp_path, catalog)
    api.load_worker_modules(
        (_worker_module_unit("МодульА", 1, catalog),),
        common_modules=catalog,
    )
    original = _worker_module_unit("МодульА", 2, catalog)
    changed_source = original.mapped_source.text.replace(
        "Функция Версия()",
        "Функция Версия(Параметр)",
    )
    reference = SourceUnitRef(
        SourceUnitKind.MODULE,
        "МодульА",
        2,
        source_sha256(changed_source),
    )
    candidate = WorkerModuleUnit(
        "МодульА",
        "module",
        2,
        mapped_visible_source(changed_source, reference),
    )
    parsed_sources: list[str] = []

    def observed_parse(source: str, **kwargs: object):
        parsed_sources.append(source)
        return parse_full_ast_module(source, **kwargs)

    monkeypatch.setattr(
        runtime_api,
        "parse_full_ast_module",
        observed_parse,
    )

    api.load_worker_modules((candidate,), common_modules=catalog)

    assert parsed_sources == [changed_source]


def test_production_module_model_retains_full_ast_parser_identity(
    tmp_path: Path,
) -> None:
    """Admission provenance must name the parser that built the active model."""
    from onec_runtime.bsl.full_ast_worker_projection import full_ast_parser_identity

    catalog = _catalog_snapshot(1, ("МодульА", CommonModuleScope.SERVER))
    wrapped = WorkerModuleArtifactBuilder(
        _notebook_worker_builder(tmp_path),
        cache=WorkerModuleArtifactCache(),
        packer_version="worker-epf-v1",
        target_profile=catalog.profile,
    )
    builder = _CountingModuleArtifactBuilder(wrapped)
    api = _semantic_snapshot_runtime(tmp_path, catalog, builder=builder)

    api.load_worker_modules(
        (_worker_module_unit("МодульА", 1, catalog),),
        common_modules=catalog,
    )

    active = api._worker_active_modules["модульа"]
    assert active.model.parser_identity == full_ast_parser_identity()
    assert builder.lowered_by_module["МодульА"].analysis.parser_identity == (
        full_ast_parser_identity()
    )


def test_full_ast_parse_error_keeps_the_confirmed_active_model(
    tmp_path: Path,
) -> None:
    """Break caught: full-AST syntax errors cannot replace confirmed state."""
    catalog = _catalog_snapshot(1, ("МодульА", CommonModuleScope.SERVER))
    api = _semantic_snapshot_runtime(tmp_path, catalog)
    api.load_worker_modules(
        (_worker_module_unit("МодульА", 1, catalog),),
        common_modules=catalog,
    )
    confirmed = api._worker_active_modules["модульа"]
    original = _worker_module_unit("МодульА", 2, catalog)
    changed_source = re.sub(
        r'Возврат "[^"]+";',
        "Возврат (",
        original.mapped_source.text,
    )
    reference = SourceUnitRef(
        SourceUnitKind.MODULE,
        "МодульА",
        2,
        source_sha256(changed_source),
    )
    candidate = WorkerModuleUnit(
        "МодульА",
        "module",
        2,
        mapped_visible_source(changed_source, reference),
    )

    with pytest.raises(BslParseError) as caught:
        api.load_worker_modules((candidate,), common_modules=catalog)

    assert caught.value.span.start >= changed_source.index("Возврат (")
    assert api._worker_active_modules == {"модульа": confirmed}


@pytest.mark.parametrize("line_ending", ("\n", "\r\n"))
@pytest.mark.parametrize("delimiter", ('"', "'"))
def test_changed_source_lex_error_runs_one_projection_without_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    line_ending: str,
    delimiter: str,
) -> None:
    """Break caught: a lexical failure must not enter a second parser path."""
    import onec_runtime.runtime_api as runtime_api
    from onec_runtime.bsl.full_ast_worker_projection import parse_full_ast_module

    catalog = _catalog_snapshot(1, ("МодульА", CommonModuleScope.SERVER))
    old_source = line_ending.join(
        (
            "Функция Первый() Экспорт",
            "    Возврат 1;",
            "КонецФункции",
            "Функция Второй()",
            f"    // delimiter in an unchanged comment: {delimiter}",
            "    Возврат 2;",
            "КонецФункции",
            "",
        )
    )
    replacement = (
        'Возврат "cross-boundary'
        if delimiter == '"'
        else "Возврат '20260901"
    )
    new_source = old_source.replace("Возврат 1;", replacement)

    def make_unit(source: str, revision: int) -> WorkerModuleUnit:
        reference = SourceUnitRef(
            SourceUnitKind.MODULE,
            "МодульА",
            revision,
            source_sha256(source),
        )
        return WorkerModuleUnit(
            "МодульА",
            "module",
            revision,
            mapped_visible_source(source, reference),
        )

    previous_unit = make_unit(old_source, 1)
    candidate = make_unit(new_source, 2)
    api = _semantic_snapshot_runtime(tmp_path, catalog)
    api.load_worker_modules((previous_unit,), common_modules=catalog)
    confirmed = api._worker_active_modules["модульа"]
    parse_sources: list[str] = []

    def record_parse(source: str, **kwargs: object):
        parse_sources.append(source)
        return parse_full_ast_module(source, **kwargs)

    monkeypatch.setattr(runtime_api, "parse_full_ast_module", record_parse)
    recorder = PhaseRecorder()

    with pytest.raises((BslLexError, BslParseError)):
        api.load_worker_modules(
            (candidate,),
            common_modules=catalog,
            profiler=recorder,
        )

    assert parse_sources == [new_source]
    assert [(event.phase, event.error_present) for event in recorder.events] == [
        ("semantic_parse", True),
    ]
    assert api._worker_active_modules == {"модульа": confirmed}


def test_worker_build_failure_keeps_confirmed_active_state(
    tmp_path: Path,
) -> None:
    """Break caught: a failed artifact build cannot publish candidate state."""
    catalog = _catalog_snapshot(1, ("МодульА", CommonModuleScope.SERVER))
    packer = _notebook_worker_builder(tmp_path)
    builder = _RevisionFailingModuleArtifactBuilder(
        WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        )
    )
    api = PrototypeRuntimeApi(
        FakeController(),
        notebook_worker_builder=packer,
        worker_module_builder=builder,
        worker_instruction_executor=_UniverseInstructionExecutor(),
    )
    api.load_worker_modules(
        (_worker_module_unit("МодульА", 1, catalog),),
        common_modules=catalog,
    )
    confirmed = api._worker_active_modules["модульа"]
    builder.failed_revision = 2

    with pytest.raises(OSError, match="semantic artifact build failure"):
        api.load_worker_modules(
            (_worker_module_unit("МодульА", 2, catalog),),
            common_modules=catalog,
        )

    assert api._worker_active_modules == {"модульа": confirmed}


@pytest.mark.parametrize("failure", ("staging", "create", "wire"))
def test_known_publication_failure_keeps_confirmed_active_state(
    tmp_path: Path,
    failure: str,
) -> None:
    """Break caught: a known pre-swap failure cannot replace active state."""
    catalog = _catalog_snapshot(1, ("МодульА", CommonModuleScope.SERVER))
    target = _SemanticSnapshotFailureTarget()
    api = _semantic_snapshot_runtime(tmp_path, catalog, target=target)
    confirmed_handle = api.load_worker_modules(
        (_worker_module_unit("МодульА", 1, catalog),),
        common_modules=catalog,
    )
    confirmed = api._worker_active_modules["модульа"]
    target.failure = failure

    with pytest.raises(BslExecutionError):
        api.load_worker_modules(
            (_worker_module_unit("МодульА", 2, catalog),),
            common_modules=catalog,
        )

    assert api._worker_active_modules == {"модульа": confirmed}
    api.release_worker_generation(confirmed_handle)


def test_unknown_promotion_outcome_discards_all_active_models(
    tmp_path: Path,
) -> None:
    """Break caught: an uncertain target swap cannot leave trusted active state."""
    catalog = _catalog_snapshot(1, ("МодульА", CommonModuleScope.SERVER))
    target = _SemanticSnapshotFailureTarget()
    api = _semantic_snapshot_runtime(tmp_path, catalog, target=target)
    api.load_worker_modules(
        (_worker_module_unit("МодульА", 1, catalog),),
        common_modules=catalog,
    )
    target.failure = "unknown"

    with pytest.raises(WorkerPromotionOutcomeUnknown):
        api.load_worker_modules(
            (_worker_module_unit("МодульА", 2, catalog),),
            common_modules=catalog,
        )

    assert api._worker_active_modules == {}
    with pytest.raises(WorkerPromotionOutcomeUnknown):
        api.status()


def test_post_swap_commit_failure_discards_all_active_models(
    tmp_path: Path,
) -> None:
    """Break caught: a swapped generation without host commit has no trusted state."""
    catalog = _catalog_snapshot(1, ("МодульА", CommonModuleScope.SERVER))
    packer = _notebook_worker_builder(tmp_path)
    binary_cache = WorkerModuleArtifactCache()
    controller = _PinnedOperationController()
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=binary_cache,
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=_UniverseInstructionExecutor(),
    )
    api.load_worker_modules(
        (_worker_module_unit("МодульА", 1, catalog),),
        common_modules=catalog,
    )

    def reject_commit(_admission: object) -> None:
        raise RuntimeError("planned host catalog commit failure")

    controller.lowerer.commit_worker_exports = reject_commit  # type: ignore[method-assign]

    with pytest.raises(ProtocolError, match="export catalog could not be committed"):
        api.load_worker_modules(
            (_worker_module_unit("МодульА", 2, catalog),),
            common_modules=catalog,
        )

    assert api._worker_active_modules == {}
    assert len(binary_cache) == 2
    with pytest.raises(ProtocolError, match="export catalog could not be committed"):
        api.status()


def test_active_model_cache_retains_only_current_revision_until_close(
    tmp_path: Path,
) -> None:
    """Break caught: history/release must not grow or delete the current model."""
    catalog = _catalog_snapshot(1, ("МодульА", CommonModuleScope.SERVER))
    api = _semantic_snapshot_runtime(tmp_path, catalog)
    first_handle: WorkerGenerationHandle | None = None

    for revision in range(1, 101):
        handle = api.load_worker_modules(
            (_worker_module_unit("МодульА", revision, catalog),),
            common_modules=catalog,
        )
        if first_handle is None:
            first_handle = handle

    assert first_handle is not None
    assert tuple(api._worker_active_modules) == ("модульа",)
    current = api._worker_active_modules["модульа"]
    assert current.unit.revision == 100

    with pytest.raises(StaleWorkerGeneration):
        api.release_worker_generation(first_handle)
    assert api._worker_active_modules == {"модульа": current}

    api.close()
    assert api._worker_active_modules == {}


def test_publication_automatically_retires_superseded_handle_and_honors_manual_release(
    tmp_path: Path,
) -> None:
    """Returned handles are not a historical-retention API."""
    catalog = _catalog_snapshot(1, ("МодульА", CommonModuleScope.SERVER))
    api = _semantic_snapshot_runtime(tmp_path, catalog)
    g17 = api.load_worker_modules(
        (_worker_module_unit("МодульА", 17, catalog),), common_modules=catalog
    )
    g18 = api.load_worker_modules(
        (_worker_module_unit("МодульА", 18, catalog),), common_modules=catalog
    )

    with pytest.raises(StaleWorkerGeneration):
        api.release_worker_generation(g17)

    api.release_worker_generation(g18)
    g19 = api.load_worker_modules(
        (_worker_module_unit("МодульА", 19, catalog),), common_modules=catalog
    )

    with pytest.raises(StaleWorkerGeneration):
        api.release_worker_generation(g18)
    live = api._worker_universe._confirmed_live_inventory()
    assert live is not None
    assert live.manifest_sha256s == frozenset((g19.manifest_sha256,))
    assert api.worker_generation_handle is g19


@pytest.mark.parametrize("after_release_commit", (False, True))
def test_automatic_retirement_failure_poisoned_after_confirmed_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    after_release_commit: bool,
) -> None:
    """A confirmed new root cannot be reported with stale caller metadata."""
    catalog = _catalog_snapshot(1, ("МодульА", CommonModuleScope.SERVER))
    api = _semantic_snapshot_runtime(tmp_path, catalog)
    g17 = api.load_worker_modules(
        (_worker_module_unit("МодульА", 17, catalog),), common_modules=catalog
    )
    release = api._release_worker_lifecycle_locked

    def fail_retirement(handle: object) -> None:
        assert handle is g17
        if after_release_commit:
            release(handle)  # type: ignore[arg-type]
        raise RuntimeError("planned automatic retirement failure")

    monkeypatch.setattr(api, "_release_worker_lifecycle_locked", fail_retirement)

    with pytest.raises(
        PoisonedRuntimeError,
        match="confirmed publication followed by retirement failure",
    ):
        api.load_worker_modules(
            (_worker_module_unit("МодульА", 18, catalog),), common_modules=catalog
        )

    assert api._worker_active_modules == {}
    assert api._notebook_worker_descriptor is None
    assert api.worker_generation_handle is not g17
    current = api.worker_generation_handle
    assert current is not None
    live_after_failure = api._worker_universe._confirmed_live_inventory()
    assert live_after_failure is not None
    assert live_after_failure.manifest_sha256s == frozenset(
        (current.manifest_sha256,)
        if after_release_commit
        else (g17.manifest_sha256, current.manifest_sha256)
    )
    assert set(api._worker_generation_diagnostics) == {
        g17.manifest_sha256,
        current.manifest_sha256,
    }
    with pytest.raises(PoisonedRuntimeError):
        api.status()


def test_poisoned_notebook_activation_keeps_prepared_pin_for_teardown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-publication failure cannot release a prepared pin through poison."""
    catalog = _common_module_catalog("МодульА")
    packer = _notebook_worker_builder(tmp_path)
    api = PrototypeRuntimeApi(
        _PinnedOperationController(),
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=_UniverseInstructionExecutor(),
    )
    g17 = api.load_worker_modules(
        (_worker_module_unit("МодульА", 17, catalog),), common_modules=catalog
    )
    prepared = api.prepare_main_for_capture(
        "Функция НоваяФункция() Экспорт\n"
        "Возврат 1;\n"
        "КонецФункции\n"
        "Результат = НоваяФункция();"
    )
    prepared_pin = prepared.contents(api._prepared_main_owner).operation_pin
    assert prepared_pin is not None and prepared_pin.handle is g17
    release_pins: list[object] = []
    release_pin = api._release_generation_pin_locked

    def fail_retirement(handle: object) -> None:
        assert handle is g17
        raise RuntimeError("planned retirement failure during activation")

    def record_pin_release(pin: object) -> None:
        release_pins.append(pin)
        release_pin(pin)  # type: ignore[arg-type]

    monkeypatch.setattr(api, "_release_worker_lifecycle_locked", fail_retirement)
    monkeypatch.setattr(api, "_release_generation_pin_locked", record_pin_release)

    with pytest.raises(
        PoisonedRuntimeError,
        match="confirmed publication followed by retirement failure",
    ):
        api.activate_prepared_main_for_capture(prepared)

    assert release_pins == []
    assert prepared_pin.lease_id in api._worker_universe._leases
    api.close()


def test_worker_lifecycle_prunes_only_unreachable_artifacts_and_diagnostics(
    tmp_path: Path,
) -> None:
    """Break caught: sequential hot reload must be O(live generations)."""
    catalog = _catalog_snapshot(
        1,
        ("МодульА", CommonModuleScope.SERVER),
        ("МодульБ", CommonModuleScope.SERVER),
    )
    packer = _notebook_worker_builder(tmp_path)
    binary_cache = WorkerModuleArtifactCache()
    builder = WorkerModuleArtifactBuilder(
        packer,
        cache=binary_cache,
        packer_version="worker-epf-v1",
        target_profile=catalog.profile,
    )
    controller = _PinnedOperationController()
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=builder,
        worker_instruction_executor=_UniverseInstructionExecutor(),
    )
    handles: list[WorkerGenerationHandle] = []
    obsolete_descriptors: list[ref[object]] = []

    for revision in range(1, 61):
        previous_artifacts: tuple[WorkerModuleArtifact, ...] = ()
        if revision > 2:
            previous_artifacts = tuple(
                artifact
                for artifact in api._worker_module_artifacts.values()
                if artifact.revision == revision - 1
            )
            obsolete_descriptors.extend(ref(item) for item in previous_artifacts)
        handle = api.load_worker_modules(
            (
                _worker_module_unit("МодульА", revision, catalog),
                _worker_module_unit("МодульБ", revision, catalog),
            ),
            common_modules=catalog,
        )
        handles.append(handle)
        if revision == 1:
            assert api.execute_bsl("Результат = Capture();").kind is RuntimeReplyKind.CAPTURED
        previous_artifacts = ()

    live = api._worker_universe._confirmed_live_inventory()

    assert live is not None
    assert live.manifest_sha256s == frozenset(
        (handles[0].manifest_sha256, handles[-1].manifest_sha256)
    )
    assert len(live.artifact_identities) == 4
    assert {
        artifact.revision for artifact in api._worker_module_artifacts.values()
    } == {1, 60}
    assert set(api._worker_generation_diagnostics) == set(live.manifest_sha256s)
    assert len(binary_cache) == 4
    assert tuple(api._worker_active_modules) == ("модульа", "модульб")
    assert {
        active.unit.revision
        for active in api._worker_active_modules.values()
    } == {60}

    g1_diagnostic = api._worker_generation_diagnostics[
        handles[0].manifest_sha256
    ][0]
    exact_segment = next(
        segment
        for segment in g1_diagnostic.mapped_source.source_map.segments
        if (
            segment.relation.value == "exact"
            and segment.generated.end > segment.generated.start
        )
    )
    line, column = LineIndex(
        g1_diagnostic.mapped_source.text
    ).offset_to_line_column(exact_segment.generated.start)
    mapped = api._worker_runtime_diagnostic(
        "{ВнешняяОбработка."
        f"{g1_diagnostic.registration_name}.МодульОбъекта({line},{column})}}: pinned",
        None,
    )
    assert mapped is not None
    assert mapped.mapping_confidence is MappingConfidence.EXACT
    assert mapped.source_unit is not None
    assert mapped.source_unit.revision == 1
    compilation = remap_worker_stage_diagnostic(
        parse_platform_diagnostic(
            "{ВнешняяОбработка."
            f"{g1_diagnostic.registration_name}.МодульОбъекта({line},{column})}}: compile"
        ),
        artifact_sha256=g1_diagnostic.artifact_sha256,
        phase="connect",
        candidate_manifest_sha256=handles[0].manifest_sha256,
        candidate_artifacts=api._worker_generation_diagnostics[
            handles[0].manifest_sha256
        ],
    )
    assert compilation.mapping_confidence is MappingConfidence.EXACT
    assert compilation.source_unit is not None
    assert compilation.source_unit.revision == 1

    current_key = next(
        key for key, artifact in api._worker_module_artifacts.items()
        if artifact.revision == 60 and artifact.logical_name == "МодульА"
    )
    current_descriptor = api._worker_module_artifacts[current_key]
    api.release_worker_generation(handles[-1])
    cache_hit_handle = api.load_worker_modules(
        (
            _worker_module_unit("МодульА", 60, catalog),
            _worker_module_unit("МодульБ", 60, catalog),
        ),
        common_modules=catalog,
    )
    assert api._worker_module_artifacts[current_key] is current_descriptor

    assert api.resume_capture().kind is RuntimeReplyKind.MAIN_COMPLETED
    api.release_worker_generation(cache_hit_handle)
    api.load_worker_modules(
        (
            _worker_module_unit("МодульА", 61, catalog),
            _worker_module_unit("МодульБ", 61, catalog),
        ),
        common_modules=catalog,
    )
    gc.collect()

    assert {
        artifact.revision for artifact in api._worker_module_artifacts.values()
    } == {61}
    assert set(api._worker_generation_diagnostics) == {
        api.worker_generation_handle.manifest_sha256
    }
    assert len(binary_cache) == 2
    assert all(item() is None for item in obsolete_descriptors)

    old = (
        _worker_module_unit("МодульА", 7, catalog),
        _worker_module_unit("МодульБ", 7, catalog),
    )
    assert not any(
        artifact.revision == 7 for artifact in api._worker_module_artifacts.values()
    )
    api.load_worker_modules(old, common_modules=catalog)
    assert any(
        artifact.revision == 7 for artifact in api._worker_module_artifacts.values()
    )

    api.close()

    assert api._worker_module_artifacts == {}
    assert api._worker_generation_diagnostics == {}
    assert len(binary_cache) == 0


def test_worker_unknown_promotion_outcome_never_prunes_candidate_binary(
    tmp_path: Path,
) -> None:
    """Break caught: an uncertain candidate must retain every possible binary."""
    catalog = _catalog_snapshot(1, ("МодульА", CommonModuleScope.SERVER))
    packer = _notebook_worker_builder(tmp_path)
    binary_cache = WorkerModuleArtifactCache()
    target = _SemanticSnapshotFailureTarget()
    api = PrototypeRuntimeApi(
        FakeController(),
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=binary_cache,
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=target,
    )
    api.load_worker_modules(
        (_worker_module_unit("МодульА", 1, catalog),),
        common_modules=catalog,
    )
    confirmed_keys = frozenset(binary_cache._capsules)
    confirmed_descriptors = dict(api._worker_module_artifacts)
    target.failure = "unknown"

    with pytest.raises(WorkerPromotionOutcomeUnknown):
        api.load_worker_modules(
            (_worker_module_unit("МодульА", 2, catalog),),
            common_modules=catalog,
        )

    assert len(binary_cache) == 2
    assert frozenset(binary_cache._capsules) > confirmed_keys
    assert api._worker_module_artifacts == confirmed_descriptors
    assert api._worker_universe._confirmed_live_inventory() is None


def test_worker_pruning_distinguishes_same_binary_descriptor_source_identity(
    tmp_path: Path,
) -> None:
    """Break caught: a coarse manifest identity must not retain a stale map."""
    catalog = _catalog_snapshot(1, ("МодульА", CommonModuleScope.SERVER))
    source = (
        "Функция Версия() Экспорт\n"
        '    Возврат "shared";\n'
        "КонецФункции\n"
    )

    def unit(kind: str) -> WorkerModuleUnit:
        ref_kind = (
            SourceUnitKind.MODULE
            if kind == "module"
            else SourceUnitKind.TEST_MODULE
        )
        reference = SourceUnitRef(
            ref_kind,
            "МодульА",
            1,
            source_sha256(source),
        )
        return WorkerModuleUnit(
            "МодульА",
            kind,  # type: ignore[arg-type]
            1,
            mapped_visible_source(source, reference),
        )

    packer = _notebook_worker_builder(tmp_path)
    binary_cache = WorkerModuleArtifactCache()
    api = PrototypeRuntimeApi(
        FakeController(),
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=binary_cache,
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=_UniverseInstructionExecutor(),
    )
    module = unit("module")
    test_module = unit("test-module")
    g1 = api.load_worker_modules((module,), common_modules=catalog)
    first = next(iter(api._worker_module_artifacts.values()))
    g2 = api.load_worker_modules((test_module,), common_modules=catalog)
    second = next(
        artifact
        for key, artifact in api._worker_module_artifacts.items()
        if key[1] == "test-module"
    )

    assert first.artifact_sha256 == second.artifact_sha256
    assert first.source_map_sha256 != second.source_map_sha256
    assert len(binary_cache) == 1

    assert len(api._worker_module_artifacts) == 1
    assert next(iter(api._worker_module_artifacts)) == (
        "модульа",
        "test-module",
        1,
        test_module.mapped_source.artifact.source_sha256,
        test_module.mapped_source.source_map_sha256,
    )
    assert next(iter(api._worker_module_artifacts.values())) is second

    g3 = api.load_worker_modules((test_module,), common_modules=catalog)

    assert next(iter(api._worker_module_artifacts.values())) is second
    assert api.worker_generation_handle is g3


@pytest.mark.parametrize("failure", ("staging", "create", "wire"))
def test_known_pre_swap_failures_prune_admitted_candidate_capsules(
    tmp_path: Path,
    failure: str,
) -> None:
    """Break caught: discarded known candidates must not accumulate binaries."""
    catalog = _catalog_snapshot(1, ("МодульА", CommonModuleScope.SERVER))
    packer = _notebook_worker_builder(tmp_path)
    binary_cache = WorkerModuleArtifactCache()
    target = _SemanticSnapshotFailureTarget()
    api = PrototypeRuntimeApi(
        FakeController(),
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=binary_cache,
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=target,
    )
    current = api.load_worker_modules(
        (_worker_module_unit("МодульА", 1, catalog),),
        common_modules=catalog,
    )
    target.failure = failure

    for revision in range(2, 8):
        with pytest.raises(BslExecutionError):
            api.load_worker_modules(
                (_worker_module_unit("МодульА", revision, catalog),),
                common_modules=catalog,
            )

    assert api._worker_universe.state.value == "ready"
    assert api.worker_generation_handle is current
    assert len(api._worker_module_artifacts) == 1
    assert len(api._worker_generation_diagnostics) == 1
    assert len(binary_cache) == 1


def test_multi_module_post_build_failure_prunes_every_candidate_capsule(
    tmp_path: Path,
) -> None:
    """Break caught: a later module failure must prune all earlier build work."""
    catalog = _catalog_snapshot(
        1,
        ("МодульА", CommonModuleScope.SERVER),
        ("МодульБ", CommonModuleScope.SERVER),
    )
    packer = _notebook_worker_builder(tmp_path)
    binary_cache = WorkerModuleArtifactCache()
    builder = _PostBuildFailingModuleArtifactBuilder(
        WorkerModuleArtifactBuilder(
            packer,
            cache=binary_cache,
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        )
    )
    api = PrototypeRuntimeApi(
        FakeController(),
        notebook_worker_builder=packer,
        worker_module_builder=builder,
        worker_instruction_executor=_UniverseInstructionExecutor(),
    )
    api.load_worker_modules(
        (
            _worker_module_unit("МодульА", 1, catalog),
            _worker_module_unit("МодульБ", 1, catalog),
        ),
        common_modules=catalog,
    )
    builder.failed_module = "МодульБ"
    builder.failed_revisions = set(range(2, 8))

    for revision in range(2, 8):
        with pytest.raises(OSError, match="post-admission artifact build failure"):
            api.load_worker_modules(
                (
                    _worker_module_unit("МодульА", revision, catalog),
                    _worker_module_unit("МодульБ", revision, catalog),
                ),
                common_modules=catalog,
            )

    assert api._worker_universe.state.value == "ready"
    assert len(api._worker_module_artifacts) == 2
    assert len(api._worker_generation_diagnostics) == 1
    assert len(binary_cache) == 2


def test_failure_pruning_never_masks_the_original_build_error(tmp_path: Path) -> None:
    """Break caught: cleanup failure must not replace the admitted build fault."""
    catalog = _catalog_snapshot(1, ("МодульА", CommonModuleScope.SERVER))
    packer = _notebook_worker_builder(tmp_path)
    builder = _PostBuildFailingModuleArtifactBuilder(
        WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        )
    )
    api = PrototypeRuntimeApi(
        FakeController(),
        notebook_worker_builder=packer,
        worker_module_builder=builder,
        worker_instruction_executor=_UniverseInstructionExecutor(),
    )
    api.load_worker_modules(
        (_worker_module_unit("МодульА", 1, catalog),),
        common_modules=catalog,
    )
    builder.failed_revisions = {2}
    builder.fail_prune = True
    prior_prune_calls = builder.prune_calls

    with pytest.raises(OSError, match="post-admission artifact build failure"):
        api.load_worker_modules(
            (_worker_module_unit("МодульА", 2, catalog),),
            common_modules=catalog,
        )

    assert builder.prune_calls == prior_prune_calls + 1


def test_catalog_may_grow_without_rebuilding_cached_module(
    tmp_path: Path,
) -> None:
    """Break caught: catalog growth must not invalidate source-stable artifacts."""
    admitted = _catalog_snapshot(1, ("МодульА", CommonModuleScope.SERVER))
    extended = _catalog_snapshot(
        2,
        ("МодульА", CommonModuleScope.SERVER),
        ("МодульБ", CommonModuleScope.SERVER),
    )
    packer = _notebook_worker_builder(tmp_path)
    builder = _CountingModuleArtifactBuilder(
        WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=admitted.profile,
        )
    )
    target = _UniverseInstructionExecutor()
    api = PrototypeRuntimeApi(
        FakeController(),
        worker_module_builder=builder,
        worker_instruction_executor=target,
    )
    api.load_worker_modules(
        (_worker_module_unit("МодульА", 1, admitted),),
        common_modules=admitted,
    )
    api.load_worker_modules(
        (_worker_module_unit("МодульА", 1, admitted),),
        common_modules=extended,
    )

    assert builder.calls_by_module == {"МодульА": 1}


@pytest.mark.parametrize(
    "changed",
    ("removed", "scope", "profile", "preprocessor", "revision", "same_revision"),
)
def test_catalog_rejects_non_monotonic_change(
    tmp_path: Path,
    changed: str,
) -> None:
    """Break caught: trusted catalog history cannot rewrite prior identities."""
    admitted = _catalog_snapshot(2, ("МодульА", CommonModuleScope.SERVER))
    if changed == "removed":
        candidate = _catalog_snapshot(3)
    elif changed == "scope":
        candidate = _catalog_snapshot(
            3,
            ("МодульА", CommonModuleScope.CLIENT_SERVER),
        )
    elif changed == "profile":
        candidate = _catalog_snapshot(
            3,
            ("МодульА", CommonModuleScope.SERVER),
            profile="other-profile",
        )
    elif changed == "preprocessor":
        candidate = _catalog_snapshot(
            3,
            ("МодульА", CommonModuleScope.SERVER),
            preprocessor_profile="client",
        )
    elif changed == "revision":
        candidate = _catalog_snapshot(1, ("МодульА", CommonModuleScope.SERVER))
    else:
        candidate = _catalog_snapshot(
            2,
            ("модульа", CommonModuleScope.SERVER),
        )
    packer = _notebook_worker_builder(tmp_path)
    api = PrototypeRuntimeApi(
        FakeController(),
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=admitted.profile,
        ),
        worker_instruction_executor=_UniverseInstructionExecutor(),
    )
    module_a = _worker_module_unit("МодульА", 1, admitted)
    api.load_worker_modules((module_a,), common_modules=admitted)

    with pytest.raises(
        ProtocolError,
        match="catalog is not a monotonic extension",
    ):
        api.load_worker_modules((module_a,), common_modules=candidate)


def test_failed_catalog_growth_preserves_trusted_snapshot_and_artifact_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: a failed candidate cannot seed trusted runtime cache state."""
    admitted = _catalog_snapshot(1, ("МодульА", CommonModuleScope.SERVER))
    extended = _catalog_snapshot(
        2,
        ("МодульА", CommonModuleScope.SERVER),
        ("МодульБ", CommonModuleScope.SERVER),
    )
    module_a = _worker_module_unit("МодульА", 1, admitted)
    module_b = _worker_module_unit("МодульБ", 1, extended)
    packer = _notebook_worker_builder(tmp_path)
    builder = _CountingModuleArtifactBuilder(
        WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=admitted.profile,
        )
    )
    api = PrototypeRuntimeApi(
        FakeController(),
        worker_module_builder=builder,
        worker_instruction_executor=_UniverseInstructionExecutor(),
    )
    api.load_worker_modules((module_a,), common_modules=admitted)
    trusted_cache = dict(api._worker_module_artifacts)
    original = PrototypeRuntimeApi._worker_candidate_diagnostics

    def reject(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise ProtocolError("planned candidate admission failure")

    monkeypatch.setattr(PrototypeRuntimeApi, "_worker_candidate_diagnostics", reject)
    with pytest.raises(ProtocolError, match="candidate admission failure"):
        api.load_worker_modules((module_b,), common_modules=extended)

    assert api._worker_catalog_snapshot is admitted
    assert api._worker_module_artifacts == trusted_cache

    monkeypatch.setattr(PrototypeRuntimeApi, "_worker_candidate_diagnostics", original)
    api.load_worker_modules((module_b,), common_modules=extended)
    assert builder.calls_by_module == {"МодульА": 1, "МодульБ": 2}


def test_namespace_snapshot_commits_only_successfully_executed_bsl_names() -> None:
    from onec_runtime.bsl.parser_target import PythonParserTarget
    from onec_runtime.bsl.semantic_lowering import SemanticNotebookLowerer

    controller = FakeController()
    controller.lowerer = SemanticNotebookLowerer(PythonParserTarget.from_generated())
    outcomes = deque((False, True))

    def execute_mapped_main(
        visible: str,
        mapped: object,
        **_kwargs: object,
    ) -> MainCompletion:
        controller.operation_id += 1
        succeeded = outcomes.popleft()
        return MainCompletion(
            OperationHandle(controller.operation_id, visible, mapped.text),  # type: ignore[attr-defined]
            None,
            "" if succeeded else "planned",
            succeeded,
        )

    controller.execute_mapped_main = execute_mapped_main  # type: ignore[method-assign]
    api = PrototypeRuntimeApi(controller)

    failed = api.execute_bsl("ОшибочноеИмя = 1;")
    assert failed.succeeded is False
    assert api.namespace_snapshot().names == ()

    completed = api.execute_bsl("УспешноеИмя = 2;")
    assert completed.succeeded is True
    assert api.namespace_snapshot().names == ("УспешноеИмя",)


def test_namespace_snapshot_defers_main_names_until_capture_completion() -> None:
    from onec_runtime.bsl.parser_target import PythonParserTarget
    from onec_runtime.bsl.semantic_lowering import SemanticNotebookLowerer

    controller = FakeController()
    controller.lowerer = SemanticNotebookLowerer(PythonParserTarget.from_generated())

    def execute_mapped_main(
        visible: str,
        mapped: object,
        **_kwargs: object,
    ) -> CapturedStop:
        controller.operation_id += 1
        controller.state = OperationState.CAPTURED
        return CapturedStop(
            OperationHandle(controller.operation_id, visible, mapped.text),  # type: ignore[attr-defined]
            LOCATION,
            1,
            (),
        )

    controller.execute_mapped_main = execute_mapped_main  # type: ignore[method-assign]
    api = PrototypeRuntimeApi(controller)

    captured = api.execute_bsl("ИмяИзMain = 1;")
    assert captured.kind is RuntimeReplyKind.CAPTURED
    assert api.namespace_snapshot().names == ()

    capture_cell = api.execute_bsl("ИмяИзCapture = 2;")
    assert capture_cell.kind is RuntimeReplyKind.CAPTURE_CELL
    assert api.namespace_snapshot().names == ("ИмяИзCapture",)

    completed = api.resume_capture()
    assert completed.kind is RuntimeReplyKind.MAIN_COMPLETED
    assert api.namespace_snapshot().names == ("ИмяИзMain", "ИмяИзCapture")


def test_api_materializes_table_without_active_worker() -> None:
    controller = FakeController()
    api = PrototypeRuntimeApi(controller)
    controller.worker_results.clear()
    schema = {
        "version": 1,
        "columns": ["Имя"],
        "kinds": ["string"],
        "reference_modes": {},
    }
    row = ["А"]
    content = (
        json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        + "\n"
        + json.dumps(row, ensure_ascii=False, separators=(",", ":"))
        + "\n"
    ).encode()
    encoded = b64encode(content).decode()
    controller.context_value = encoded
    controller.worker_results.append(
        f"R|1|1|{len(content)}|{sha256(content).hexdigest()}|{len(encoded)}"
    )

    profiler = PhaseRecorder()
    frame = api.materialize_table("Контекст.Таблица", profiler=profiler)

    assert frame.to_dict(orient="records") == [{"Имя": "А"}]
    assert len(controller.main_sources) == 1
    assert controller.table_declared_schema_calls == []
    assert controller.table_sample_calls == []
    assert "RuntimeTableTransferServer.СериализоватьКомпактнуюТаблицу" in controller.main_sources[0]
    assert "ТипыОбъектовWorker = Новый Массив;" in controller.main_sources[0]
    assert len(controller.context_reads) == 1
    assert controller.context_reads[0][1] >= len(encoded)
    assert [event.phase for event in profiler.events] == [
        "table.prepare_jsonl",
        "table.transfer_base64",
        "table.decode_base64",
        "table.build_dataframe",
    ]


def test_api_enforces_table_payload_row_budget_before_transport() -> None:
    controller = FakeController()
    api = PrototypeRuntimeApi(controller)
    controller.worker_results.clear()
    content = (
        json.dumps(
            {
                "version": 1,
                "columns": ["Имя"],
                "kinds": ["string"],
                "reference_modes": {},
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
        + json.dumps(["А"], ensure_ascii=False, separators=(",", ":"))
        + "\n"
    ).encode()
    encoded = b64encode(content).decode()
    controller.context_value = encoded
    controller.worker_results.extend(
        (
            "table",
            f"R|1|1|{len(content)}|{sha256(content).hexdigest()}|{len(encoded)}",
        )
    )

    payload = api.materialize_table_payload(
        "Контекст.Таблица", max_rows=3, max_bytes=4096
    )

    assert payload == content
    assert "ТипыОбъектовWorker, 3, 4096);" in controller.main_sources[1]


def test_api_project_to_df_transfers_only_bounded_table_projection() -> None:
    controller = FakeController()
    api = PrototypeRuntimeApi(controller)
    controller.worker_results.clear()
    content = (
        json.dumps(
            {
                "version": 1,
                "columns": ["Имя"],
                "kinds": ["string"],
                "reference_modes": {},
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
        + json.dumps(["А"], ensure_ascii=False, separators=(",", ":"))
        + "\n"
    ).encode()
    encoded = b64encode(content).decode()
    controller.context_value = encoded
    controller.worker_results.extend(
        (True, f"R|1|1|{len(content)}|{sha256(content).hexdigest()}|{len(encoded)}")
    )

    frame = api.project_to_df(
        "Контекст.Таблица",
        {"offset": 20, "limit": 10},
        refs="both",
        max_bytes=4096,
    )

    assert frame.to_dict("records") == [{"Имя": "А"}]
    assert "Для ИндексПроекции = 20" in controller.main_sources[0]
    assert "Контекст.Таблица.Скопировать" in controller.main_sources[0]
    assert "ТипыОбъектовWorker, 10, 4096);" in controller.main_sources[1]
    assert controller.context_drops[-1].startswith("__onec_projection_")


def test_api_project_value_decodes_only_bounded_array_projection() -> None:
    controller = FakeController()
    api = PrototypeRuntimeApi(controller)
    controller.worker_results.clear()
    content = json.dumps(
        {
            "version": 1,
            "root": {"t": "array", "v": [{"t": "number", "v": "5"}]},
        },
        separators=(",", ":"),
    ).encode()
    encoded = b64encode(content).decode()
    controller.context_value = encoded
    controller.worker_results.extend(
        (
            "value",
            True,
            f"R|1|1|{len(content)}|{sha256(content).hexdigest()}|{len(encoded)}",
        )
    )

    result = api.project_value(
        "Контекст.Массив",
        {"offset": 5, "limit": 15},
        max_items=100,
        max_bytes=4096,
    )

    assert result == [5]
    assert "Для ИндексПроекции = 5" in controller.main_sources[1]
    assert controller.context_drops[-1].startswith("__onec_projection_")


def test_api_project_value_preserves_table_materialize_dataframe_semantics() -> None:
    controller = FakeController()
    api = PrototypeRuntimeApi(controller)
    controller.worker_results.clear()
    content = (
        json.dumps(
            {
                "version": 1,
                "columns": ["Имя"],
                "kinds": ["string"],
                "reference_modes": {},
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
        + json.dumps(["А"], ensure_ascii=False, separators=(",", ":"))
        + "\n"
    ).encode()
    encoded = b64encode(content).decode()
    controller.context_value = encoded
    controller.worker_results.extend(
        (
            "table",
            True,
            f"R|1|1|{len(content)}|{sha256(content).hexdigest()}|{len(encoded)}",
        )
    )

    result = api.project_value(
        "Контекст.Таблица",
        {"offset": 0, "limit": 10},
        refs="both",
        max_items=100,
        max_bytes=4096,
    )

    assert result.to_dict("records") == [{"Имя": "А"}]
    assert "Контекст.Таблица.Скопировать" in controller.main_sources[1]


def test_api_project_value_keeps_route_and_projection_under_one_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = FakeController()
    api = PrototypeRuntimeApi(controller)
    controller.worker_results.clear()
    content = json.dumps(
        {
            "version": 1,
            "root": {"t": "array", "v": [{"t": "number", "v": "5"}]},
        },
        separators=(",", ":"),
    ).encode()
    encoded = b64encode(content).decode()
    controller.context_value = encoded
    controller.worker_results.extend(
        (
            "value",
            True,
            f"R|1|1|{len(content)}|{sha256(content).hexdigest()}|{len(encoded)}",
        )
    )
    route_returned = Event()
    continue_projection = Event()
    original_materialization_kind = api.materialization_kind

    def pause_after_public_route(handle: str) -> str:
        result = original_materialization_kind(handle)
        route_returned.set()
        assert continue_projection.wait(timeout=2)
        return result

    monkeypatch.setattr(api, "materialization_kind", pause_after_public_route)
    results: list[object] = []
    failures: list[BaseException] = []

    def project() -> None:
        try:
            results.append(
                api.project_value(
                    "Контекст.Массив",
                    {"offset": 0, "limit": 1},
                    max_items=10,
                    max_bytes=4096,
                )
            )
        except BaseException as error:  # pragma: no cover - asserted below
            failures.append(error)

    thread = Thread(target=project)
    thread.start()
    if route_returned.wait(timeout=0.25):
        try:
            with pytest.raises(ProtocolError, match="already executing"):
                api.status()
        finally:
            continue_projection.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert failures == []
    assert results == [[5]]


def test_api_routes_recursive_value_materialization_without_active_worker() -> None:
    controller = FakeController()
    api = PrototypeRuntimeApi(controller)
    controller.worker_results.clear()
    content = json.dumps(
        {
            "version": 1,
            "root": {
                "t": "structure",
                "v": [["Name", {"t": "string", "v": "value"}]],
            },
        },
        separators=(",", ":"),
    ).encode()
    encoded = b64encode(content).decode()
    controller.context_value = encoded
    controller.worker_results.extend(
        (
            "value",
            f"R|1|1|{len(content)}|{sha256(content).hexdigest()}|{len(encoded)}",
        )
    )

    result = api.materialize_value(
        "Контекст.Данные", refs="both", max_depth=7, max_items=99, max_bytes=4096
    )

    assert result == {"Name": "value"}
    assert len(controller.main_sources) == 2
    assert (
        controller.main_sources[0]
        == "Результат = RuntimeValueTransferServer."
        "ПолучитьВидМатериализации(Контекст.Данные);"
    )
    assert "СериализоватьЗначение(Контекст.Данные, \"both\", 7, 99, 4096, ТипыОбъектовWorker)" in (
        controller.main_sources[1]
    )
    assert len(controller.context_reads) == 1


def test_api_routes_table_materialize_to_existing_dataframe_transport() -> None:
    controller = FakeController()
    api = PrototypeRuntimeApi(controller)
    controller.worker_results.clear()
    content = (
        json.dumps(
            {
                "version": 1,
                "columns": ["Имя"],
                "kinds": ["string"],
                "reference_modes": {},
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
        + json.dumps(["А"], ensure_ascii=False, separators=(",", ":"))
        + "\n"
    ).encode()
    encoded = b64encode(content).decode()
    controller.context_value = encoded
    controller.worker_results.extend(
        (
            "table",
            f"R|1|1|{len(content)}|{sha256(content).hexdigest()}|{len(encoded)}",
        )
    )

    result = api.materialize_value("Контекст.Таблица")

    assert result.to_dict(orient="records") == [{"Имя": "А"}]
    assert controller.table_declared_schema_calls == []
    assert len(controller.main_sources) == 2


class _RuntimeApiPreviewBackend:
    runtime_id = "runtime-preview"
    is_closed = False

    def __init__(self, api: PrototypeRuntimeApi) -> None:
        self.api = api

    def materialize_value(self, handle: str, **options: object) -> object:
        return self.api.materialize_value(handle, **options)

    def validate_value_reference(self, handle: str) -> None:
        self.api.validate_value_reference(handle)


class _DeadlineAwarePreviewController(FakeController):
    def __init__(self) -> None:
        super().__init__()
        self.command_timeout_s = 30.0
        self.preview_command_timeouts: list[float] = []

    def execute_system_capture(self, source: str) -> CaptureCellResult:
        self.preview_command_timeouts.append(self.command_timeout_s)
        return super().execute_system_capture(source)

    def inspect_declared_table_schema(self, handle: str) -> EvaluationResult:
        self.preview_command_timeouts.append(self.command_timeout_s)
        return super().inspect_declared_table_schema(handle)

    def take_context_string(self, key: str, *, max_text_size: int) -> str:
        self.preview_command_timeouts.append(self.command_timeout_s)
        return super().take_context_string(key, max_text_size=max_text_size)


def _frame_table_preview(
    controller: FakeController,
    *,
    payload: bytes,
    items: int,
    byte_limit: int,
    type_name: str = "РезультатЗапроса",
) -> object:
    encoded = b64encode(payload).decode()
    controller.context_value = encoded
    controller.worker_results.clear()
    controller.worker_results.extend(
        (
            "table",
            f"R|1|1|{len(payload)}|{sha256(payload).hexdigest()}|{len(encoded)}",
        )
    )
    api = PrototypeRuntimeApi(controller)
    registry = ProxyRegistry()
    proxy = registry.register_frame(
        qualified_name="capture.intent.Таблица",
        type_name=type_name,
        runtime_id=_RuntimeApiPreviewBackend.runtime_id,
        runtime_generation=1,
        context_generation=1,
        capture_fence=CaptureFence("intent", "operation", 1, "a" * 64, 1, 1),
        provenance=ProxyProvenance("capture", 1, "a" * 64, "operation"),
        resolver_handle="Контекст.КонтекстОтладки.Таблица",
        capabilities=("preview",),
    )
    return OnecValueResolver(
        _RuntimeApiPreviewBackend(api),  # type: ignore[arg-type]
        registry,
    ).preview(
        proxy,
        limits={"items": items, "bytes": byte_limit, "timeout_s": 0.75},
    )


def test_frame_table_preview_uses_server_owned_row_byte_and_deadline_bounds() -> None:
    """Break caught: value.preview must not transfer a full frame-local table."""
    controller = _DeadlineAwarePreviewController()
    controller.state = OperationState.CAPTURED
    payload = (
        json.dumps(
            {
                "version": 1,
                "columns": ["Имя"],
                "kinds": ["string"],
                "reference_modes": {},
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
        + json.dumps(["А"], ensure_ascii=False, separators=(",", ":"))
        + "\n"
    ).encode()

    preview = _frame_table_preview(
        controller,
        payload=payload,
        items=1,
        byte_limit=512,
    )

    assert preview.sample == ({"Имя": "А"},)
    assert preview.type_name == "РезультатЗапроса"
    assert preview.truncated is True
    assert len(controller.capture_sources) == 2
    transfer_source = controller.capture_sources[1]
    assert "ТипыОбъектовWorker, 1, 512);" in transfer_source
    assert controller.table_declared_schema_calls == []
    assert len(controller.context_reads) == 1
    assert len(controller.preview_command_timeouts) == 3
    assert all(0 < value <= 0.75 for value in controller.preview_command_timeouts)
    assert controller.command_timeout_s == 30.0


def test_frame_table_preview_rejects_oversize_payload_before_transfer() -> None:
    """Break caught: a table route must enforce max_bytes before Base64 transfer."""
    controller = FakeController()
    controller.state = OperationState.CAPTURED
    payload = b"x" * 65

    with pytest.raises(ProtocolError, match="admission result"):
        _frame_table_preview(
            controller,
            payload=payload,
            items=1,
            byte_limit=64,
        )

    assert len(controller.capture_sources) == 2
    assert "ТипыОбъектовWorker, 1, 64);" in controller.capture_sources[1]
    assert controller.context_reads == []


def test_api_rejects_unknown_server_materialization_route() -> None:
    controller = FakeController()
    api = PrototypeRuntimeApi(controller)
    controller.worker_results.clear()
    controller.worker_results.append("executable")

    with pytest.raises(ProtocolError, match="route"):
        api.materialize_value("Контекст.Данные")

    assert controller.context_reads == []


def test_api_materializes_recursive_value_while_capture_is_paused() -> None:
    controller = FakeController()
    controller.state = OperationState.CAPTURED
    api = PrototypeRuntimeApi(controller)
    controller.worker_results.clear()
    content = json.dumps(
        {"version": 1, "root": {"t": "string", "v": "capture"}},
        separators=(",", ":"),
    ).encode()
    encoded = b64encode(content).decode()
    controller.context_value = encoded
    controller.worker_results.extend(
        (
            "value",
            f"R|1|1|{len(content)}|{sha256(content).hexdigest()}|{len(encoded)}",
        )
    )

    assert api.materialize_value("Контекст.Данные") == "capture"
    assert len(controller.capture_sources) == 2
    assert all("РезультатИнструкции = Результат;" in source for source in controller.capture_sources)


class _PinnedOperationController(FakeController):
    def __init__(self) -> None:
        super().__init__()
        self.lowerer = SemanticNotebookLowerer(PythonParserTarget.from_generated())
        self.cell_sequence = 0
        self.main_mapped_sources: list[str] = []
        self.capture_mapped_sources: list[str] = []

    def message_collector_key(self, mode: object) -> str:
        return "__onec_messages_" + getattr(mode, "value", str(mode))

    def execute_mapped_main(
        self,
        visible: str,
        mapped: object,
        **_kwargs: object,
    ) -> CapturedStop:
        dispatch = _kwargs.get("on_transport_dispatch")
        if callable(dispatch):
            dispatch()
        self.main_mapped_sources.append(mapped.text)
        self.operation_id += 1
        self.stop_sequence = 1
        self.cell_sequence = 0
        self.state = OperationState.CAPTURED
        operation = OperationHandle(self.operation_id, visible, mapped.text)
        return CapturedStop(
            operation,
            LOCATION,
            self.stop_sequence,
            (),
            self.operation_id,
        )

    def execute_mapped_capture(
        self,
        visible: str,
        mapped: object,
        **_kwargs: object,
    ) -> CaptureCellResult:
        dispatch = _kwargs.get("on_transport_dispatch")
        if callable(dispatch):
            dispatch()
        self.capture_mapped_sources.append(mapped.text)
        self.cell_sequence += 1
        return CaptureCellResult(
            self.operation_id,
            visible,
            mapped.text,
            self.cell_sequence,
        )


def test_main_worker_stop_maps_through_operation_pin_and_resumes(
    tmp_path: Path,
) -> None:
    catalog = _common_module_catalog("МодульА")
    packer = _notebook_worker_builder(tmp_path)
    target = _UniverseInstructionExecutor()

    class WorkerStopController(_PinnedOperationController):
        worker_stop: StopEvent | None = None

        def execute_mapped_main(
            self,
            visible: str,
            mapped: object,
            **kwargs: object,
        ) -> DebugStop:
            dispatch = kwargs.get("on_transport_dispatch")
            if callable(dispatch):
                dispatch()
            assert self.worker_stop is not None
            self.operation_id += 1
            self.state = OperationState.DEBUG_STOPPED
            operation = OperationHandle(self.operation_id, visible, mapped.text)
            return DebugStop(operation, self.worker_stop, StopReason.USER_BREAKPOINT)

        def resume_debug_stop(self, **kwargs: object) -> MainCompletion:
            dispatch = kwargs.get("on_transport_dispatch")
            if callable(dispatch):
                dispatch()
            self.state = OperationState.COMPLETED
            return MainCompletion(
                OperationHandle(self.operation_id, "visible", "lowered"),
                42,
                "",
                True,
            )

    controller = WorkerStopController()
    _attach_breakpoint_workspace(controller)
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=target,
    )
    api.load_worker_modules(
        (_worker_module_unit("МодульА", 17, catalog),),
        common_modules=catalog,
    )
    breakpoint_id = _install_logical_breakpoint(api)
    view = api._worker_universe._retained_debug_views()[0]
    binding = api._worker_breakpoints.bindings_for_view(view)[0]
    event_target = TargetId(UUID(int=807), "test")
    controller.worker_stop = StopEvent(
        event_target,
        binding.location,
        "callStackFormed",
        stop_by_breakpoint=True,
        stack=(binding.location,),
        stack_frames=(StackFrame(event_target, 0, binding.location),),
    )

    stopped = api.execute_bsl("Результат = МодульА.Версия();")

    assert stopped.kind is RuntimeReplyKind.DEBUG_STOPPED
    assert stopped.debug_stop is not None
    assert stopped.debug_stop.location.source_unit.revision == 17
    assert stopped.debug_stop.breakpoint_ids == (breakpoint_id,)
    assert binding.location.url not in repr(stopped.debug_stop)
    assert api.operation_worker_generation is view.handle

    completed = api.resume_debug_stop()

    assert completed.kind is RuntimeReplyKind.MAIN_COMPLETED
    assert api.operation_worker_generation is None


def test_capture_worker_call_with_installed_breakpoint_continues_without_stop(
    tmp_path: Path,
) -> None:
    catalog = _common_module_catalog("МодульА")
    packer = _notebook_worker_builder(tmp_path)
    target = _UniverseInstructionExecutor()
    controller = _PinnedOperationController()
    _attach_breakpoint_workspace(controller)
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=target,
    )
    api.load_worker_modules(
        (_worker_module_unit("МодульА", 17, catalog),),
        common_modules=catalog,
    )
    _install_logical_breakpoint(api)
    view = api._worker_universe._retained_debug_views()[0]

    captured = api.execute_bsl("Capture = Истина;")
    pinned = api.operation_worker_generation
    calls_before = tuple(controller.capture_mapped_sources)
    capture_cell = api.execute_bsl("Результат = МодульА.Версия();")

    assert captured.kind is RuntimeReplyKind.CAPTURED
    assert capture_cell.kind is RuntimeReplyKind.CAPTURE_CELL
    assert capture_cell.succeeded is True
    assert capture_cell.state is OperationState.CAPTURED
    assert len(controller.capture_mapped_sources) == len(calls_before) + 1
    assert controller.state is OperationState.CAPTURED
    assert api.operation_worker_generation is pinned is view.handle


def test_prepared_capture_worker_call_with_installed_breakpoint_continues(
    tmp_path: Path,
) -> None:
    catalog = _common_module_catalog("МодульА")
    packer = _notebook_worker_builder(tmp_path)
    target = _UniverseInstructionExecutor()
    controller = _PinnedOperationController()
    _attach_breakpoint_workspace(controller)
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=target,
    )
    api.load_worker_modules(
        (_worker_module_unit("МодульА", 17, catalog),),
        common_modules=catalog,
    )
    _install_logical_breakpoint(api)
    api.execute_bsl("Capture = Истина;")
    prepared = api.prepare_capture_hypothesis("Результат = МодульА.Версия();")
    calls_before = tuple(controller.capture_mapped_sources)

    capture_cell = api.execute_prepared_capture_hypothesis(prepared)

    assert capture_cell.kind is RuntimeReplyKind.CAPTURE_CELL
    assert capture_cell.succeeded is True
    assert capture_cell.state is OperationState.CAPTURED
    assert len(controller.capture_mapped_sources) == len(calls_before) + 1
    assert api.operation_worker_generation is not None
    with pytest.raises(ProtocolError, match="already consumed"):
        api.execute_prepared_capture_hypothesis(prepared)


def test_adding_worker_override_builds_only_the_new_module(tmp_path: Path) -> None:
    """Break caught: an unchanged unit must not be parsed/lowered/packed again."""
    catalog = _common_module_catalog("МодульА", "МодульБ")
    module_a = _worker_module_unit("МодульА", 1, catalog)
    module_b = _worker_module_unit("МодульБ", 1, catalog)
    packer = _notebook_worker_builder(tmp_path)
    builder = _CountingModuleArtifactBuilder(
        WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        )
    )
    target = _UniverseInstructionExecutor()
    api = PrototypeRuntimeApi(
        FakeController(),
        notebook_worker_builder=packer,
        worker_module_builder=builder,
        worker_instruction_executor=target,
    )

    first = api.load_worker_modules((module_a,), common_modules=catalog)
    second = api.load_worker_modules((module_a, module_b), common_modules=catalog)

    assert isinstance(first, WorkerGenerationHandle)
    assert second.generation == first.generation + 1
    assert builder.calls_by_module == {"МодульА": 1, "МодульБ": 1}


def test_full_ast_module_cache_parses_first_source_once_and_reuses_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: admitted source models must not be parsed a second time."""
    import onec_runtime.runtime_api as runtime_api
    from onec_runtime.bsl.full_ast_worker_projection import parse_full_ast_module

    catalog = _common_module_catalog("МодульА")
    module_a = _worker_module_unit("МодульА", 1, catalog)
    api = _semantic_snapshot_runtime(tmp_path, catalog)
    parsed_sources: list[str] = []

    def observed_parse(source: str, **kwargs: object):
        parsed_sources.append(source)
        return parse_full_ast_module(source, **kwargs)

    monkeypatch.setattr(
        runtime_api,
        "parse_full_ast_module",
        observed_parse,
    )

    api.load_worker_modules((module_a,), common_modules=catalog)
    api.load_worker_modules((module_a,), common_modules=catalog)

    assert parsed_sources == [module_a.mapped_source.text]


@pytest.mark.parametrize("failure", [None, "staging", "wire", "unknown"])
def test_syntax_candidates_activate_only_after_confirmed_worker_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None,
) -> None:
    """A parsed G2 must never retarget G1, even when promotion loses its reply."""
    from onec_runtime.bsl.full_ast_worker_projection import full_ast_parser_identity
    from onec_runtime.performance_profile import PhaseRecorder

    catalog = _common_module_catalog("МодульА")
    first = _worker_module_unit("МодульА", 1, catalog)
    second = _worker_module_unit("МодульА", 2, catalog)
    target = _SemanticSnapshotFailureTarget()
    api = _semantic_snapshot_runtime(tmp_path, catalog, target=target)
    g1 = api.load_worker_modules((first,), common_modules=catalog)
    first_index = api._worker_module_syntax("МодульА", generation=g1)
    assert first_index.source_sha256 == first.mapped_source.artifact.source_sha256
    original_publish = api._publish_worker_artifacts_locked
    staged = []

    def observe_candidate(*args: object, **kwargs: object):
        candidate = api.module_syntax_registry.get(
            api._worker_module_identity(second), second.mapped_source.artifact.source_sha256,
            full_ast_parser_identity(),
        )
        assert candidate is not None
        staged.append(candidate)
        assert api._worker_module_syntax("МодульА") is first_index
        return original_publish(*args, **kwargs)

    monkeypatch.setattr(api, "_publish_worker_artifacts_locked", observe_candidate)
    target.failure = failure
    profiler = PhaseRecorder()
    if failure is None:
        g2 = api.load_worker_modules((second,), common_modules=catalog, profiler=profiler)
        assert api._worker_module_syntax("МодульА", generation=g2) is staged[0]
        assert api._worker_module_syntax("МодульА") is staged[0]
    else:
        expected = WorkerPromotionOutcomeUnknown if failure == "unknown" else BslExecutionError
        with pytest.raises(expected):
            api.load_worker_modules((second,), common_modules=catalog, profiler=profiler)
        assert api.worker_generation_handle is g1
        assert api._worker_module_syntax("МодульА") is first_index
        assert all(staged[0] not in entries.values()
                   for entries in api._worker_syntax_generations.values())
    assert len(staged) == 1
    assert api._worker_module_syntax("МодульА", generation=g1) is first_index
    assert api.module_syntax_registry.get(
        api._worker_module_identity(second), second.mapped_source.artifact.source_sha256,
        full_ast_parser_identity(),
    ) is staged[0]
    assert profiler.parser_calls.full_module_parses == 1


def test_main_later_stop_keeps_g1_syntax_after_g2_and_next_main_uses_g2(tmp_path: Path) -> None:
    """Default capture lookup must follow the operation pin, never latest source."""
    catalog = _common_module_catalog("МодульА")
    packer = _notebook_worker_builder(tmp_path)

    class LaterStopController(_PinnedOperationController):
        stop_again = True

        def resume(self, **kwargs: object):
            if self.stop_again:
                self.stop_again = False
                self.stop_sequence += 1
                return CapturedStop(
                    OperationHandle(self.operation_id, "visible", "lowered"),
                    LOCATION, self.stop_sequence, (), self.operation_id,
                )
            return super().resume(**kwargs)

    controller = LaterStopController()
    api = PrototypeRuntimeApi(
        controller, notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer, cache=WorkerModuleArtifactCache(), packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ), worker_instruction_executor=_UniverseInstructionExecutor(),
    )
    first = _worker_module_unit("МодульА", 1, catalog)
    second = _worker_module_unit("МодульА", 2, catalog)
    g1 = api.load_worker_modules((first,), common_modules=catalog)
    assert api.execute_bsl("Результат = Capture();").kind is RuntimeReplyKind.CAPTURED
    first_index = api._worker_module_syntax("МодульА")
    g2 = api.load_worker_modules((second,), common_modules=catalog)
    assert api.resume_capture().kind is RuntimeReplyKind.CAPTURED
    assert controller.stop_sequence == 2
    assert api.operation_worker_generation is g1
    assert api._worker_module_syntax("МодульА") is first_index
    assert first_index.source_sha256 == first.mapped_source.artifact.source_sha256
    assert api.resume_capture().kind is RuntimeReplyKind.MAIN_COMPLETED
    assert api.execute_bsl("Результат = Capture();").kind is RuntimeReplyKind.CAPTURED
    assert api.operation_worker_generation is g2
    assert api._worker_module_syntax("МодульА").source_sha256 == second.mapped_source.artifact.source_sha256


def test_capture_syntax_lookup_and_reload_cache_hit_parse_zero_additional_times(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The registry and active-model cache must share the single original parse."""
    from onec_runtime.bsl.full_ast_worker_projection import full_ast_parser_identity
    from onec_runtime.bsl.parser_target import PythonParserTarget

    catalog = _common_module_catalog("МодульА")
    unit = _worker_module_unit("МодульА", 1, catalog)
    api = _semantic_snapshot_runtime(tmp_path, catalog)
    original_parse = PythonParserTarget.parse_tokens_ast
    parses = []

    def observe_parse(self, *args, **kwargs):
        parses.append(args[1])
        return original_parse(self, *args, **kwargs)

    monkeypatch.setattr(PythonParserTarget, "parse_tokens_ast", observe_parse)
    api.load_worker_modules((unit,), common_modules=catalog)
    assert parses == ["Модуль"]
    index = api.module_syntax_registry.get(
        api._worker_module_identity(unit), unit.mapped_source.artifact.source_sha256,
        full_ast_parser_identity(),
    )
    assert index.method_at_line(2).name == "Версия"
    api.load_worker_modules((unit,), common_modules=catalog)
    assert api._worker_module_syntax("модульа") is index
    assert parses == ["Модуль"]


def test_source_model_cache_hit_stages_syntax_for_the_new_logical_unit_kind(tmp_path: Path) -> None:
    """Reusing source facts must not omit publication under a new module identity."""
    from onec_runtime.bsl.full_ast_worker_projection import full_ast_parser_identity
    from onec_runtime.performance_profile import PhaseRecorder

    catalog = _common_module_catalog("МодульА")
    unit = _worker_module_unit("МодульА", 1, catalog)
    api = _semantic_snapshot_runtime(tmp_path, catalog)
    api.load_worker_modules((unit,), common_modules=catalog)
    source = unit.mapped_source.text
    test_unit = WorkerModuleUnit(
        "МодульА", "test-module", 2,
        mapped_visible_source(source, SourceUnitRef(
            SourceUnitKind.TEST_MODULE, "МодульА", 2, source_sha256(source),
        )),
    )
    profiler = PhaseRecorder()
    api.load_worker_modules((test_unit,), common_modules=catalog, profiler=profiler)
    assert api._worker_module_identity(test_unit) != api._worker_module_identity(unit)
    assert api.module_syntax_registry.get(
        api._worker_module_identity(test_unit), source_sha256(source), full_ast_parser_identity(),
    ) is api._worker_module_syntax("МодульА")
    assert profiler.parser_calls.full_module_parses == 0


def test_worker_module_load_upserts_into_the_full_active_universe(
    tmp_path: Path,
) -> None:
    """Break caught: loading B must not silently remove an active A override."""
    catalog = _common_module_catalog("МодульА", "МодульБ")
    module_a = _worker_module_unit("МодульА", 1, catalog)
    module_b = _worker_module_unit("МодульБ", 1, catalog)
    api = _semantic_snapshot_runtime(tmp_path, catalog)

    api.load_worker_modules((module_a,), common_modules=catalog)
    api.load_worker_modules((module_b,), common_modules=catalog)

    manifest = api._worker_universe.active_manifest
    assert manifest is not None
    assert tuple(item.logical_name for item in manifest.modules) == (
        "МодульА",
        "МодульБ",
    )


@pytest.mark.parametrize("source_format", ["designer", "edt", "edt-project"])
def test_catalog_manager_resolves_a_parsed_bare_root_in_the_first_generation(
    tmp_path: Path,
    source_format: str,
) -> None:
    """Break caught: catalog validation before parse misses first-load dependencies."""
    source_root = tmp_path / "source"
    content_root = source_root / "src" if source_format == "edt-project" else source_root
    _add_runtime_common_module(content_root, "МодульА", edt=source_format != "designer")
    _add_runtime_common_module(content_root, "МодульБ", edt=source_format != "designer")
    catalog = SessionCommonModuleCatalog(source_root, profile="server-test")
    module_a = _worker_module_source_unit(
        "МодульА",
        1,
        "Функция Версия() Экспорт\n"
        "    Возврат МодульБ.Версия();\n"
        "КонецФункции\n",
    )
    module_b = _worker_module_source_unit(
        "МодульБ",
        1,
        "Функция Версия() Экспорт\n"
        "    Возврат 2;\n"
        "КонецФункции\n",
    )
    packer = _notebook_worker_builder(tmp_path)
    api = PrototypeRuntimeApi(
        FakeController(),
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile="server-test",
        ),
        worker_instruction_executor=_UniverseInstructionExecutor(),
    )

    api.load_worker_modules((module_a, module_b), common_modules=catalog)

    manifest = api._worker_universe.active_manifest
    assert manifest is not None
    assert [
        (item.source_module, item.target_kind, item.target_module)
        for item in manifest.wiring
    ] == [("МодульА", "overloaded", "МодульБ")]


def test_catalog_growth_reresolves_an_omitted_active_model_without_reparse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: a late common module must rebind saved reads, not rescan source."""
    import onec_runtime.runtime_api as runtime_api
    from onec_runtime.bsl.full_ast_worker_projection import parse_full_ast_module

    source_root = tmp_path / "source"
    _add_runtime_common_module(source_root, "МодульА")
    catalog = SessionCommonModuleCatalog(source_root, profile="server-test")
    module_a = _worker_module_source_unit(
        "МодульА",
        1,
        "Функция Версия() Экспорт\n"
        "    Возврат МодульБ.Версия();\n"
        "КонецФункции\n",
    )
    packer = _notebook_worker_builder(tmp_path)
    builder = _CountingModuleArtifactBuilder(
        WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile="server-test",
        )
    )
    api = PrototypeRuntimeApi(
        FakeController(),
        notebook_worker_builder=packer,
        worker_module_builder=builder,
        worker_instruction_executor=_UniverseInstructionExecutor(),
    )
    parsed_sources: list[str] = []

    def observed_parse(source: str, **kwargs: object):
        parsed_sources.append(source)
        return parse_full_ast_module(source, **kwargs)

    monkeypatch.setattr(
        runtime_api,
        "parse_full_ast_module",
        observed_parse,
        raising=False,
    )

    api.load_worker_modules((module_a,), common_modules=catalog)
    _add_runtime_common_module(source_root, "МодульБ")
    module_b = _worker_module_source_unit(
        "МодульБ",
        1,
        "Функция Версия() Экспорт\n"
        "    Возврат 2;\n"
        "КонецФункции\n",
    )
    api.load_worker_modules((module_b,), common_modules=catalog)

    manifest = api._worker_universe.active_manifest
    assert manifest is not None
    assert parsed_sources == [
        module_a.mapped_source.text,
        module_b.mapped_source.text,
    ]
    assert builder.calls_by_module == {"МодульА": 2, "МодульБ": 1}
    assert [
        (item.source_module, item.target_kind, item.target_module)
        for item in manifest.wiring
    ] == [("МодульА", "overloaded", "МодульБ")]


def test_changed_worker_source_runs_one_new_full_ast_parse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: changed source must use one full AST, never method delta."""
    import onec_runtime.runtime_api as runtime_api
    from onec_runtime.bsl.full_ast_worker_projection import parse_full_ast_module

    catalog = _common_module_catalog("МодульА")
    first = _worker_module_unit("МодульА", 1, catalog)
    second = _worker_module_unit("МодульА", 2, catalog)
    api = _semantic_snapshot_runtime(tmp_path, catalog)
    parsed_sources: list[str] = []

    def observed_parse(source: str, **kwargs: object):
        parsed_sources.append(source)
        return parse_full_ast_module(source, **kwargs)

    monkeypatch.setattr(
        runtime_api,
        "parse_full_ast_module",
        observed_parse,
        raising=False,
    )

    api.load_worker_modules((first,), common_modules=catalog)
    api.load_worker_modules((second,), common_modules=catalog)

    assert parsed_sources == [
        first.mapped_source.text,
        second.mapped_source.text,
    ]


def test_catalog_growth_reuses_an_unchanged_semantic_plan_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: catalog generation alone must not rebuild unrelated EPFs."""
    import onec_runtime.runtime_api as runtime_api
    from onec_runtime.bsl.worker_dependency_resolver import (
        resolve_worker_dependencies,
    )

    source_root = tmp_path / "source"
    _add_runtime_common_module(source_root, "МодульА")
    catalog = SessionCommonModuleCatalog(source_root, profile="server-test")
    module_a = _worker_module_source_unit(
        "МодульА",
        1,
        "Функция Версия() Экспорт\n"
        "    Возврат 1;\n"
        "КонецФункции\n",
    )
    packer = _notebook_worker_builder(tmp_path)
    builder = _CountingModuleArtifactBuilder(
        WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile="server-test",
        )
    )
    api = PrototypeRuntimeApi(
        FakeController(),
        notebook_worker_builder=packer,
        worker_module_builder=builder,
        worker_instruction_executor=_UniverseInstructionExecutor(),
    )
    resolved: list[tuple[str, int]] = []

    def observed_resolve(model, snapshot, **kwargs):
        resolved.append((model.source_sha256, snapshot.revision))
        return resolve_worker_dependencies(model, snapshot, **kwargs)

    monkeypatch.setattr(
        runtime_api,
        "resolve_worker_dependencies",
        observed_resolve,
    )

    api.load_worker_modules((module_a,), common_modules=catalog)
    _add_runtime_common_module(source_root, "МодульБ")
    module_b = _worker_module_source_unit(
        "МодульБ",
        1,
        "Функция Версия() Экспорт\n"
        "    Возврат 2;\n"
        "КонецФункции\n",
    )
    api.load_worker_modules((module_b,), common_modules=catalog)

    assert builder.calls_by_module == {"МодульА": 1, "МодульБ": 1}
    assert [revision for _, revision in resolved] == [2, 3, 3]


def test_catalog_growth_preserves_materialized_implicit_locals_without_reparse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Frozen module/method locals remain explicit when the catalog grows."""
    import onec_runtime.runtime_api as runtime_api
    from onec_runtime.bsl.module_universe import lower_resolved_worker_module

    source_root = tmp_path / "source"
    _add_runtime_common_module(source_root, "МодульА")
    catalog = SessionCommonModuleCatalog(source_root, profile="server-test")
    module_a = _worker_module_source_unit(
        "МодульА",
        1,
        "Модульная = 1;\n"
        "Функция Версия() Экспорт\n"
        "    Локальная = 1;\n"
        "    Возврат Локальная;\n"
        "КонецФункции\n",
    )
    packer = _notebook_worker_builder(tmp_path)
    builder = _CountingModuleArtifactBuilder(
        WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile="server-test",
        )
    )
    api = PrototypeRuntimeApi(
        FakeController(),
        notebook_worker_builder=packer,
        worker_module_builder=builder,
        worker_instruction_executor=_UniverseInstructionExecutor(),
    )
    lowered_sources: dict[str, str] = {}

    def observed_lower(unit, plan, **kwargs):
        lowered = lower_resolved_worker_module(unit, plan, **kwargs)
        lowered_sources[unit.logical_name] = lowered.mapped_source.text
        return lowered

    monkeypatch.setattr(runtime_api, "lower_resolved_worker_module", observed_lower)

    api.load_worker_modules((module_a,), common_modules=catalog)
    admitted = api._worker_active_modules["модульа"]
    assert "Перем Модульная;\nМодульная = 1;" in lowered_sources["МодульА"]
    assert "    Перем Локальная;\n    Локальная = 1;" in lowered_sources[
        "МодульА"
    ]

    for name in ("Модульная", "Локальная", "МодульБ"):
        _add_runtime_common_module(source_root, name)
    module_b = _worker_module_source_unit(
        "МодульБ",
        1,
        "Функция Версия() Экспорт\n"
        "    Возврат 2;\n"
        "КонецФункции\n",
    )
    api.load_worker_modules((module_b,), common_modules=catalog)

    current = api._worker_active_modules["модульа"]
    assert current.artifact is admitted.artifact
    assert current.plan.implicit_local_names == ("модульная",)
    assert current.plan.methods[0].implicit_local_names == ("локальная",)
    assert builder.calls_by_module == {"МодульА": 1, "МодульБ": 1}


def test_failed_catalog_growth_keeps_active_models_and_retry_reresolves_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: catalog I/O may commit, but failed Worker state must not."""
    import onec_runtime.runtime_api as runtime_api
    from onec_runtime.bsl.full_ast_worker_projection import parse_full_ast_module

    source_root = tmp_path / "source"
    _add_runtime_common_module(source_root, "МодульА")
    catalog = SessionCommonModuleCatalog(source_root, profile="server-test")
    module_a = _worker_module_source_unit(
        "МодульА",
        1,
        "Функция Версия() Экспорт\n"
        "    Возврат МодульБ.Версия();\n"
        "КонецФункции\n",
    )
    packer = _notebook_worker_builder(tmp_path)
    builder = _RevisionFailingModuleArtifactBuilder(
        WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile="server-test",
        )
    )
    api = PrototypeRuntimeApi(
        FakeController(),
        notebook_worker_builder=packer,
        worker_module_builder=builder,
        worker_instruction_executor=_UniverseInstructionExecutor(),
    )
    parsed_sources: list[str] = []

    def observed_parse(source: str, **kwargs: object):
        parsed_sources.append(source)
        return parse_full_ast_module(source, **kwargs)

    monkeypatch.setattr(runtime_api, "parse_full_ast_module", observed_parse)
    api.load_worker_modules((module_a,), common_modules=catalog)
    confirmed = api._worker_active_modules["модульа"]
    confirmed_catalog = api._worker_catalog_snapshot
    _add_runtime_common_module(source_root, "МодульБ")
    module_b = _worker_module_source_unit(
        "МодульБ",
        2,
        "Функция Версия() Экспорт\n"
        "    Возврат 2;\n"
        "КонецФункции\n",
    )
    builder.failed_revision = 2

    with pytest.raises(OSError, match="semantic artifact build failure"):
        api.load_worker_modules((module_b,), common_modules=catalog)

    assert api._worker_active_modules == {"модульа": confirmed}
    assert api._worker_catalog_snapshot is confirmed_catalog
    assert catalog.ensure_initialized().revision > confirmed.plan.catalog_generation

    builder.failed_revision = None
    api.load_worker_modules((module_b,), common_modules=catalog)

    assert parsed_sources.count(module_a.mapped_source.text) == 1
    assert parsed_sources.count(module_b.mapped_source.text) == 2
    manifest = api._worker_universe.active_manifest
    assert manifest is not None
    assert tuple(item.logical_name for item in manifest.modules) == (
        "МодульА",
        "МодульБ",
    )


def test_reload_profiler_records_each_changed_module_build_phase(
    tmp_path: Path,
) -> None:
    catalog = _common_module_catalog("МодульА")
    packer = _notebook_worker_builder(tmp_path)
    builder = _CountingModuleArtifactBuilder(
        WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        )
    )
    api = PrototypeRuntimeApi(
        FakeController(),
        notebook_worker_builder=packer,
        worker_module_builder=builder,
        worker_instruction_executor=_UniverseInstructionExecutor(),
    )
    profiler = PhaseRecorder()

    api.load_worker_modules(
        (_worker_module_unit("МодульА", 1, catalog),),
        common_modules=catalog,
        profiler=profiler,
    )

    assert [event.phase for event in profiler.events] == [
        "semantic_parse",
        "ast_model_extract",
        "dependency_analysis",
        "resolved_analysis_adapter",
        "alias_transform",
        "source_map_composition",
        "admission",
        "epf_packaging",
        "artifact_stage_sealed_validation",
        "artifact_stage_base64",
        "artifact_stage_executor",
        "artifact_stage_batch",
        "artifact_staging",
        "generation_create_wire_probe",
        "root_swap",
    ]
    assert builder.calls_by_module == {"МодульА": 1}


def test_reload_profiler_distinguishes_full_ast_parse_from_cache_hit(
    tmp_path: Path,
) -> None:
    """Break caught: phase timing alone must not mislabel the parser path."""
    catalog = _common_module_catalog("МодульА")
    packer = _notebook_worker_builder(tmp_path)
    api = PrototypeRuntimeApi(
        FakeController(),
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=_UniverseInstructionExecutor(),
    )
    revision_one = _worker_module_unit("МодульА", 1, catalog)
    revision_two = _worker_module_unit("МодульА", 2, catalog)

    full = PhaseRecorder()
    api.load_worker_modules(
        (revision_one,), common_modules=catalog, profiler=full
    )
    cache_hit = PhaseRecorder()
    api.load_worker_modules(
        (revision_one,), common_modules=catalog, profiler=cache_hit
    )
    changed = PhaseRecorder()
    api.load_worker_modules(
        (revision_two,), common_modules=catalog, profiler=changed
    )

    assert full.parser_calls.full_module_parses == 1
    assert full.parser_calls.worker_profile_parses == 0
    assert cache_hit.parser_calls.full_module_parses == 0
    assert cache_hit.parser_calls.worker_profile_parses == 0
    assert changed.parser_calls.full_module_parses == 1
    assert changed.parser_calls.worker_profile_parses == 0
    assert full.parser_calls.as_tuple() == (1, 0, 0)
    assert cache_hit.parser_calls.as_tuple() == (0, 0, 0)
    assert changed.parser_calls.as_tuple() == (1, 0, 0)


def test_reload_profiler_records_sealed_validation_for_full_cache_hit(
    tmp_path: Path,
) -> None:
    catalog = _common_module_catalog("МодульА")
    module_a = _worker_module_unit("МодульА", 1, catalog)
    packer = _notebook_worker_builder(tmp_path)
    builder = _CountingModuleArtifactBuilder(
        WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        )
    )
    api = PrototypeRuntimeApi(
        FakeController(),
        notebook_worker_builder=packer,
        worker_module_builder=builder,
        worker_instruction_executor=_UniverseInstructionExecutor(),
    )
    api.load_worker_modules((module_a,), common_modules=catalog)
    profiler = PhaseRecorder()

    api.load_worker_modules(
        (module_a,),
        common_modules=catalog,
        profiler=profiler,
    )

    assert [event.phase for event in profiler.events] == [
        "artifact_stage_sealed_validation",
        "generation_create_wire_probe",
        "root_swap",
    ]
    assert builder.calls_by_module == {"МодульА": 1}


def test_reload_profiler_omits_epf_packaging_on_binary_cache_reuse(
    tmp_path: Path,
) -> None:
    """Break caught: an outer miss may still reuse the packaged binary."""
    catalog = _common_module_catalog("МодульА")
    source = (
        "Функция Версия() Экспорт\n"
        '    Возврат "stable";\n'
        "КонецФункции\n"
    )

    def stable_unit(revision: int) -> WorkerModuleUnit:
        reference = SourceUnitRef(
            SourceUnitKind.MODULE,
            "МодульА",
            revision,
            source_sha256(source),
        )
        return WorkerModuleUnit(
            "МодульА",
            "module",
            revision,
            mapped_visible_source(source, reference),
        )

    packer = _notebook_worker_builder(tmp_path)
    builder = _CountingModuleArtifactBuilder(
        WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        )
    )
    api = PrototypeRuntimeApi(
        FakeController(),
        notebook_worker_builder=packer,
        worker_module_builder=builder,
        worker_instruction_executor=_UniverseInstructionExecutor(),
    )
    api.load_worker_modules((stable_unit(1),), common_modules=catalog)
    profiler = PhaseRecorder()

    api.load_worker_modules(
        (stable_unit(2),),
        common_modules=catalog,
        profiler=profiler,
    )

    assert builder.calls_by_module == {"МодульА": 2}
    assert [event.phase for event in profiler.events] == [
        "resolved_analysis_adapter",
        "alias_transform",
        "source_map_composition",
        "admission",
        "artifact_stage_sealed_validation",
        "generation_create_wire_probe",
        "root_swap",
    ]


def test_session_reload_profiles_catalog_validation_and_end_to_end(
    tmp_path: Path,
) -> None:
    catalog = _common_module_catalog("МодульА")
    module_a = _worker_module_unit("МодульА", 1, catalog)
    packer = _notebook_worker_builder(tmp_path)
    api = PrototypeRuntimeApi(
        FakeController(),
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=_UniverseInstructionExecutor(),
    )
    session = RuntimeSession.__new__(RuntimeSession)
    session._operation_lock = Lock()
    session._active_worker_file_units = {}
    source_root = tmp_path / "source"
    _add_runtime_common_module(source_root, "МодульА")
    session._common_module_catalog = SessionCommonModuleCatalog(
        source_root,
        profile=catalog.profile,
    )
    session.runtime_api = api
    profiler = PhaseRecorder()

    session.load_worker_modules((module_a,), profiler=profiler)

    assert [event.phase for event in profiler.events] == [
        "semantic_parse",
        "ast_model_extract",
        "catalog_validation",
        "dependency_analysis",
        "resolved_analysis_adapter",
        "alias_transform",
        "source_map_composition",
        "admission",
        "epf_packaging",
        "artifact_stage_sealed_validation",
        "artifact_stage_base64",
        "artifact_stage_executor",
        "artifact_stage_batch",
        "artifact_staging",
        "generation_create_wire_probe",
        "root_swap",
        "end_to_end",
    ]


def test_worker_module_analysis_uses_catalog_supplied_at_load(
    tmp_path: Path,
) -> None:
    """Break caught: dependency membership is supplied at analysis, not unit creation."""
    unit_catalog = _common_module_catalog("МодульА")
    requested_catalog = _common_module_catalog("МодульА", "МодульБ")
    module_a = _worker_module_unit("МодульА", 1, unit_catalog)
    packer = _notebook_worker_builder(tmp_path)
    builder = _CountingModuleArtifactBuilder(
        WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=requested_catalog.profile,
        )
    )
    target = _UniverseInstructionExecutor()
    api = PrototypeRuntimeApi(
        FakeController(),
        notebook_worker_builder=packer,
        worker_module_builder=builder,
        worker_instruction_executor=target,
    )

    generation = api.load_worker_modules((module_a,), common_modules=requested_catalog)

    assert isinstance(generation, WorkerGenerationHandle)
    assert builder.calls_by_module == {"МодульА": 1}
    assert len(target.sources) == 3


def test_generation_pin_is_acquired_before_lowering_and_survives_capture_promotions(
    tmp_path: Path,
) -> None:
    """Break caught: paused hypotheses must never observe a newer active catalog."""
    catalog = _common_module_catalog("МодульА", "МодульБ", "МодульВ")
    module_a = _worker_module_unit("МодульА", 17, catalog)
    module_b = _worker_module_unit("МодульБ", 18, catalog)
    module_c = _worker_module_unit("МодульВ", 19, catalog)
    packer = _notebook_worker_builder(tmp_path)
    builder = WorkerModuleArtifactBuilder(
        packer,
        cache=WorkerModuleArtifactCache(),
        packer_version="worker-epf-v1",
        target_profile=catalog.profile,
    )
    target = _UniverseInstructionExecutor()
    controller = _PinnedOperationController()
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=builder,
        worker_instruction_executor=target,
    )
    g17 = api.load_worker_modules((module_a,), common_modules=catalog)
    observed_during_lowering: list[WorkerGenerationHandle | None] = []
    original_lower = controller.lowerer.lower_mapped

    def observe_pin(*args: object, **kwargs: object):
        observed_during_lowering.append(api.operation_worker_generation)
        return original_lower(*args, **kwargs)

    controller.lowerer.lower_mapped = observe_pin  # type: ignore[method-assign]

    captured = api.execute_bsl("Результат = Capture();")
    assert captured.kind is RuntimeReplyKind.CAPTURED
    assert api.operation_worker_generation is g17
    assert observed_during_lowering == [g17]
    assert controller.main_mapped_sources[0].startswith(
        "__OnecPinnedWorkerGeneration = "
        "Контекст.RuntimeWorkerActiveGeneration;\n"
    )

    g18 = api.load_worker_modules((module_b,), common_modules=catalog)
    g19 = api.load_worker_modules((module_c,), common_modules=catalog)
    assert g18.generation == g17.generation + 1
    assert g19.generation == g18.generation + 1
    live = api._worker_universe._confirmed_live_inventory()
    assert live is not None
    assert live.manifest_sha256s == frozenset((g17.manifest_sha256, g19.manifest_sha256))
    assert {
        view.handle for view in api._worker_universe._retained_debug_views()
    } == {g17, g19}
    assert set(api._worker_generation_diagnostics) == set(live.manifest_sha256s)
    assert target.disconnects == []

    for _ in range(2):
        prepared = api.prepare_capture_hypothesis(
            "Результат = МодульА.Версия();"
        )
        reply = api.execute_prepared_capture_hypothesis(prepared)
        assert reply.kind is RuntimeReplyKind.CAPTURE_CELL
        assert api.operation_worker_generation is g17
        assert g19.manifest_sha256 in controller.capture_mapped_sources[-1]
        assert (
            '__OnecPinnedWorkerGeneration.Modules.Получить("МодульА").Версия()'
            in controller.capture_mapped_sources[-1]
        )

    assert observed_during_lowering == [g17, g17, g17]
    completed = api.resume_capture()
    assert completed.kind is RuntimeReplyKind.MAIN_COMPLETED
    assert api.operation_worker_generation is None
    terminal_live = api._worker_universe._confirmed_live_inventory()
    assert terminal_live is not None
    assert terminal_live.manifest_sha256s == frozenset((g19.manifest_sha256,))
    assert {
        view.handle for view in api._worker_universe._retained_debug_views()
    } == {g19}
    assert set(api._worker_generation_diagnostics) == {g19.manifest_sha256}
    assert target.disconnects == []


def test_main_prelude_pins_one_local_generation_before_user_bsl(
    tmp_path: Path,
) -> None:
    """The local survives CAPTURE without making user Context a privacy authority."""
    catalog = _common_module_catalog("МодульА")
    module_a = _worker_module_unit("МодульА", 17, catalog)
    packer = _notebook_worker_builder(tmp_path)
    controller = _PinnedOperationController()
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=_UniverseInstructionExecutor(),
    )
    handle = api.load_worker_modules((module_a,), common_modules=catalog)

    assert api.execute_bsl("Результат = Capture();").kind is RuntimeReplyKind.CAPTURED

    source = controller.main_mapped_sources[-1]
    prelude = (
        "__OnecPinnedWorkerGeneration = "
        "Контекст.RuntimeWorkerActiveGeneration;"
    )
    assert source.startswith(prelude + "\n")
    manifest_check = (
        "Если __OnecPinnedWorkerGeneration.ManifestSha256 <> "
        f'"{handle.manifest_sha256}" Тогда\n'
        '    ВызватьИсключение "Worker generation pin mismatch";\n'
        "КонецЕсли;"
    )
    assert manifest_check in source
    assert source.index(prelude) < source.index("Результат = Capture();")
    assert source.index(manifest_check) < source.index("Результат = Capture();")
    assert "RuntimeWorkerPinnedOperationGeneration" not in source


def test_capture_transition_installs_exact_pin_and_clears_it_before_resume(
    tmp_path: Path,
) -> None:
    """Kernel-frame CAPTURE code needs a trusted ephemeral route to the host pin."""
    catalog = _common_module_catalog("МодульА")
    module_a = _worker_module_unit("МодульА", 17, catalog)
    packer = _notebook_worker_builder(tmp_path)
    controller = _PinnedOperationController()
    events: list[tuple[str, str | None]] = []
    execute_main = controller.execute_mapped_main
    resume = controller.resume

    def recorded_main(*args: object, **kwargs: object) -> CapturedStop:
        events.append(("main_dispatch", None))
        return execute_main(*args, **kwargs)

    def recorded_resume(**kwargs: object) -> MainCompletion:
        events.append(("resume_dispatch", None))
        return resume(**kwargs)

    controller.execute_mapped_main = recorded_main  # type: ignore[method-assign]
    controller.resume = recorded_resume  # type: ignore[method-assign]
    controller.install_capture_worker_generation_pin = (  # type: ignore[attr-defined]
        lambda manifest: events.append(("install", manifest))
    )
    controller.clear_capture_worker_generation_pin = (  # type: ignore[attr-defined]
        lambda: events.append(("clear", None))
    )
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=_UniverseInstructionExecutor(),
    )
    handle = api.load_worker_modules((module_a,), common_modules=catalog)

    captured = api.execute_bsl("Результат = Capture();")

    assert captured.kind is RuntimeReplyKind.CAPTURED
    assert events == [
        ("main_dispatch", None),
        ("install", handle.manifest_sha256),
    ]
    assert api.operation_worker_generation is handle

    capture = api.execute_bsl("РезультатИнструкции = МодульА.Версия();")

    assert capture.kind is RuntimeReplyKind.CAPTURE_CELL
    capture_source = controller.capture_mapped_sources[-1]
    manifest_check = (
        "Если __OnecPinnedWorkerGeneration.ManifestSha256 <> "
        f'"{handle.manifest_sha256}" Тогда\n'
        '    ВызватьИсключение "Worker generation pin mismatch";\n'
        "КонецЕсли;"
    )
    assert capture_source.startswith(
        "__OnecPinnedWorkerGeneration = Контекст.RuntimeWorkerActiveGeneration;\n"
        + manifest_check + "\n"
    )
    assert (
        '__OnecPinnedWorkerGeneration.Modules.Получить("МодульА").Версия()'
        in capture_source
    )
    assert "RuntimeWorkerPinnedOperationGeneration" not in capture_source

    api.resume_capture()

    assert events == [
        ("main_dispatch", None),
        ("install", handle.manifest_sha256),
        ("clear", None),
        ("resume_dispatch", None),
    ]
    assert api.operation_worker_generation is None


def test_local_reference_validation_does_not_run_worker_identity_policy(
    tmp_path: Path,
) -> None:
    """Dynamic Worker identity is checked only by a materialization serializer."""
    catalog = _common_module_catalog("МодульА")
    module_a = _worker_module_unit("МодульА", 17, catalog)
    packer = _notebook_worker_builder(tmp_path)
    target = _UniverseInstructionExecutor()
    controller = _PinnedOperationController()
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=target,
    )
    api.load_worker_modules((module_a,), common_modules=catalog)
    registrations = api._worker_universe_target.privacy_registration_snapshot()
    target.sources.clear()

    assert api.validate_value_reference("Контекст.АлиасМодуля") == "Контекст.АлиасМодуля"

    assert target.sources == []
    assert api._worker_universe_target.privacy_registration_snapshot() == registrations


def test_local_reference_validation_accepts_ordinary_values_without_target_io(
    tmp_path: Path,
) -> None:
    catalog = _common_module_catalog("МодульА")
    module_a = _worker_module_unit("МодульА", 17, catalog)
    packer = _notebook_worker_builder(tmp_path)
    target = _UniverseInstructionExecutor()
    api = PrototypeRuntimeApi(
        _PinnedOperationController(),
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=target,
    )
    api.load_worker_modules((module_a,), common_modules=catalog)
    target.sources.clear()

    assert (
        api.validate_value_reference("Контекст.ОбычноеФиксированноеСоответствие")
        == "Контекст.ОбычноеФиксированноеСоответствие"
    )
    assert api.validate_value_reference("Контекст.ОбычныйФиксированныйМассив") == (
        "Контекст.ОбычныйФиксированныйМассив"
    )

    assert target.sources == []


@pytest.mark.parametrize(
    "handle",
    (
        "Контекст.RuntimeWorkerActiveGeneration.Modules",
        "контекст.runtimeworkerpinnedoperationgeneration",
        "__onecPINNEDworkerGeneration.Modules",
        "Контекст.Значение; Результат = Ложь",
        "Контекст.Функция()",
    ),
)
def test_local_reference_validation_rejects_reserved_or_malformed_handle_before_target(
    handle: str,
) -> None:
    target = _UniverseInstructionExecutor()
    api = PrototypeRuntimeApi(
        _PinnedOperationController(), worker_instruction_executor=target
    )

    with pytest.raises(ProtocolError):
        api.validate_value_reference(handle)

    assert target.sources == []


def test_session_delegates_single_local_reference_validation() -> None:
    target = _UniverseInstructionExecutor()
    api = PrototypeRuntimeApi(
        _PinnedOperationController(), worker_instruction_executor=target
    )
    session = RuntimeSession.__new__(RuntimeSession)
    session.runtime_api = api
    session._operation_lock = Lock()

    assert session.validate_value_reference("Контекст.Обычное") == "Контекст.Обычное"

    assert target.sources == []


def test_terminal_resume_releases_g17_without_target_context_cleanup(
    tmp_path: Path,
) -> None:
    catalog = _common_module_catalog("МодульА", "МодульБ")
    module_a = _worker_module_unit("МодульА", 17, catalog)
    module_b = _worker_module_unit("МодульБ", 18, catalog)
    packer = _notebook_worker_builder(tmp_path)
    target = _UniverseInstructionExecutor()
    controller = _PinnedOperationController()
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=target,
    )
    g17 = api.load_worker_modules((module_a,), common_modules=catalog)
    assert api.execute_bsl("Результат = Capture();").kind is RuntimeReplyKind.CAPTURED
    api.release_worker_generation(g17)
    api.load_worker_modules((module_b,), common_modules=catalog)

    assert api.resume_capture().kind is RuntimeReplyKind.MAIN_COMPLETED

    assert not any(
        "onec-worker-operation-root-clear" in source for source in target.sources
    )
    assert target.disconnects == []


def test_ambiguous_resume_keeps_quarantined_g17_without_context_slot(
    tmp_path: Path,
) -> None:
    catalog = _common_module_catalog("МодульА")
    module_a = _worker_module_unit("МодульА", 17, catalog)
    packer = _notebook_worker_builder(tmp_path)
    target = _UniverseInstructionExecutor()
    controller = _PinnedOperationController()
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=target,
    )
    api.load_worker_modules((module_a,), common_modules=catalog)
    assert api.execute_bsl("Результат = Capture();").kind is RuntimeReplyKind.CAPTURED

    def lose_resume(**kwargs: object) -> object:
        dispatch = kwargs["on_transport_dispatch"]
        assert callable(dispatch)
        dispatch()
        raise TimeoutError("planned ambiguous resume")

    controller.resume = lose_resume  # type: ignore[method-assign]

    with pytest.raises(TimeoutError, match="ambiguous resume"):
        api.resume_capture()

    assert controller.main_mapped_sources[-1].startswith(
        "__OnecPinnedWorkerGeneration = "
        "Контекст.RuntimeWorkerActiveGeneration;"
    )
    assert not any(
        "onec-worker-operation-root-clear" in source for source in target.sources
    )
    assert target.disconnects == []
    with pytest.raises(WorkerPromotionOutcomeUnknown):
        api.status()


def test_pre_dispatch_capture_resume_preserves_original_pin_until_terminal_reply(
    tmp_path: Path,
) -> None:
    """A deterministic resume failure is not evidence that the paused MAIN ended."""
    catalog = _common_module_catalog("МодульА")
    module_g17 = _worker_module_unit("МодульА", 17, catalog)
    module_g18 = _worker_module_unit("МодульА", 18, catalog)
    packer = _notebook_worker_builder(tmp_path)
    controller = _PinnedOperationController()
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=_UniverseInstructionExecutor(),
    )
    g17 = api.load_worker_modules((module_g17,), common_modules=catalog)
    assert api.execute_bsl("Результат = Capture();").kind is RuntimeReplyKind.CAPTURED
    original_pin = api._operation_generation_pin
    assert original_pin is not None and original_pin.handle is g17
    api.load_worker_modules((module_g18,), common_modules=catalog)
    successful_resume = controller.resume

    def fail_before_dispatch(**_kwargs: object) -> MainCompletion:
        controller.state = OperationState.PARTIAL_WRITEBACK_FAILURE
        raise RuntimeError("planned local writeback failure")

    controller.resume = fail_before_dispatch  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="local writeback failure"):
        api.resume_capture()

    assert controller.state is OperationState.PARTIAL_WRITEBACK_FAILURE
    assert api._poisoned_error is None
    assert api._operation_generation_pin is original_pin
    assert api.operation_worker_generation is g17

    controller.state = OperationState.CAPTURED
    controller.resume = successful_resume  # type: ignore[method-assign]
    assert api.resume_capture().kind is RuntimeReplyKind.MAIN_COMPLETED
    assert api.operation_worker_generation is None


def test_pre_dispatch_debug_resume_preserves_original_pin_until_terminal_reply(
    tmp_path: Path,
) -> None:
    """A paused MAIN at a user breakpoint keeps its exact pin before Continue."""
    catalog = _common_module_catalog("МодульА")
    module_g17 = _worker_module_unit("МодульА", 17, catalog)
    module_g18 = _worker_module_unit("МодульА", 18, catalog)
    packer = _notebook_worker_builder(tmp_path)

    class DebugController(_PinnedOperationController):
        def execute_mapped_main(
            self, visible: str, mapped: object, **kwargs: object
        ) -> DebugStop:
            dispatch = kwargs.get("on_transport_dispatch")
            if callable(dispatch):
                dispatch()
            self.operation_id += 1
            self.state = OperationState.DEBUG_STOPPED
            return DebugStop(
                OperationHandle(self.operation_id, visible, mapped.text),  # type: ignore[attr-defined]
                StopEvent(TARGET, LOCATION, "callStackFormed"),
                StopReason.USER_BREAKPOINT,
            )

        def resume_debug_stop(self, **_kwargs: object) -> MainCompletion:
            raise RuntimeError("planned local debug resume failure")

    controller = DebugController()
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=_UniverseInstructionExecutor(),
    )
    g17 = api.load_worker_modules((module_g17,), common_modules=catalog)
    assert api.execute_bsl("Результат = МодульА.Версия();").kind is RuntimeReplyKind.DEBUG_STOPPED
    original_pin = api._operation_generation_pin
    assert original_pin is not None and original_pin.handle is g17
    api.load_worker_modules((module_g18,), common_modules=catalog)

    with pytest.raises(RuntimeError, match="local debug resume failure"):
        api.resume_debug_stop()

    assert api._poisoned_error is None
    assert api._operation_generation_pin is original_pin
    assert api.operation_worker_generation is g17

    def complete_debug_resume(**kwargs: object) -> MainCompletion:
        dispatch = kwargs.get("on_transport_dispatch")
        if callable(dispatch):
            dispatch()
        controller.state = OperationState.COMPLETED
        return MainCompletion(
            OperationHandle(controller.operation_id, "visible", "lowered"),
            42,
            "",
            True,
        )

    controller.resume_debug_stop = complete_debug_resume  # type: ignore[method-assign]
    assert api.resume_debug_stop().kind is RuntimeReplyKind.MAIN_COMPLETED
    assert api.operation_worker_generation is None


def test_pre_dispatch_capture_debug_resume_preserves_both_pins(
    tmp_path: Path,
) -> None:
    """A paused CAPTURE evaluation and its original MAIN both survive local failure."""
    catalog = _common_module_catalog("МодульА")
    module_g17 = _worker_module_unit("МодульА", 17, catalog)
    module_g18 = _worker_module_unit("МодульА", 18, catalog)
    packer = _notebook_worker_builder(tmp_path)

    class CaptureDebugController(_PinnedOperationController):
        def execute_mapped_capture(
            self, visible: str, mapped: object, **kwargs: object
        ) -> DebugStop:
            dispatch = kwargs.get("on_transport_dispatch")
            if callable(dispatch):
                dispatch()
            self.state = OperationState.CAPTURE_DEBUG_STOPPED
            return DebugStop(
                OperationHandle(self.operation_id, visible, mapped.text),  # type: ignore[attr-defined]
                StopEvent(TARGET, LOCATION, "callStackFormed"),
                StopReason.USER_BREAKPOINT,
            )

        def resume_debug_stop(self, **_kwargs: object) -> CaptureCellResult:
            raise RuntimeError("planned local capture debug resume failure")

    controller = CaptureDebugController()
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=_UniverseInstructionExecutor(),
    )
    g17 = api.load_worker_modules((module_g17,), common_modules=catalog)
    assert api.execute_bsl("Результат = Capture();").kind is RuntimeReplyKind.CAPTURED
    original_pin = api._operation_generation_pin
    assert original_pin is not None and original_pin.handle is g17
    g18 = api.load_worker_modules((module_g18,), common_modules=catalog)
    prepared = api.prepare_capture_hypothesis("Результат = МодульА.Версия();")
    assert (
        api.execute_prepared_capture_hypothesis(prepared).kind
        is RuntimeReplyKind.DEBUG_STOPPED
    )
    evaluation_pin = api._evaluation_generation_pin
    assert evaluation_pin is not None and evaluation_pin.handle is g18

    with pytest.raises(RuntimeError, match="local capture debug resume failure"):
        api.resume_debug_stop()

    assert api._poisoned_error is None
    assert api._operation_generation_pin is original_pin
    assert api._evaluation_generation_pin is evaluation_pin

    def finish_capture_evaluation(**kwargs: object) -> CaptureCellResult:
        dispatch = kwargs.get("on_transport_dispatch")
        if callable(dispatch):
            dispatch()
        controller.state = OperationState.CAPTURED
        return CaptureCellResult(controller.operation_id, "visible", "lowered", 1)

    controller.resume_debug_stop = finish_capture_evaluation  # type: ignore[method-assign]
    assert api.resume_debug_stop().kind is RuntimeReplyKind.CAPTURE_CELL
    assert api._evaluation_generation_pin is None
    assert api._operation_generation_pin is original_pin
    assert api.resume_capture().kind is RuntimeReplyKind.MAIN_COMPLETED
    assert api.operation_worker_generation is None


def test_capture_debug_terminal_bsl_error_still_becomes_failed_capture_cell(
    tmp_path: Path,
) -> None:
    """A terminal CAPTURE evaluation failure is trusted only after CAPTURED."""
    catalog = _common_module_catalog("МодульА")
    module_g17 = _worker_module_unit("МодульА", 17, catalog)
    module_g18 = _worker_module_unit("МодульА", 18, catalog)
    packer = _notebook_worker_builder(tmp_path)

    class CaptureDebugController(_PinnedOperationController):
        def execute_mapped_capture(
            self, visible: str, mapped: object, **kwargs: object
        ) -> DebugStop:
            dispatch = kwargs.get("on_transport_dispatch")
            if callable(dispatch):
                dispatch()
            self.state = OperationState.CAPTURE_DEBUG_STOPPED
            return DebugStop(
                OperationHandle(self.operation_id, visible, mapped.text),  # type: ignore[attr-defined]
                StopEvent(TARGET, LOCATION, "callStackFormed"),
                StopReason.USER_BREAKPOINT,
            )

        def resume_debug_stop(self, **kwargs: object) -> CaptureCellResult:
            dispatch = kwargs.get("on_transport_dispatch")
            if callable(dispatch):
                dispatch()
            self.state = OperationState.CAPTURED
            raise BslExecutionError("planned terminal capture evaluation failure")

    controller = CaptureDebugController()
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=_UniverseInstructionExecutor(),
    )
    g17 = api.load_worker_modules((module_g17,), common_modules=catalog)
    assert api.execute_bsl("Результат = Capture();").kind is RuntimeReplyKind.CAPTURED
    original_pin = api._operation_generation_pin
    assert original_pin is not None and original_pin.handle is g17
    g18 = api.load_worker_modules((module_g18,), common_modules=catalog)
    prepared = api.prepare_capture_hypothesis("Результат = МодульА.Версия();")
    assert (
        api.execute_prepared_capture_hypothesis(prepared).kind
        is RuntimeReplyKind.DEBUG_STOPPED
    )
    assert api._evaluation_generation_pin is not None
    assert api._evaluation_generation_pin.handle is g18

    failed = api.resume_debug_stop()

    assert failed.kind is RuntimeReplyKind.CAPTURE_CELL
    assert failed.succeeded is False
    assert api._evaluation_generation_pin is None
    assert api._operation_generation_pin is original_pin
    assert api.operation_worker_generation is g17
    assert api.resume_capture().kind is RuntimeReplyKind.MAIN_COMPLETED
    assert api.operation_worker_generation is None


def test_post_continue_recapture_failure_quarantines_original_main_generation(
    tmp_path: Path,
) -> None:
    """A failed recapture after Continue is not terminal MAIN evidence."""
    catalog = _common_module_catalog("МодульА")
    packer = _notebook_worker_builder(tmp_path)

    class RecaptureFailureSession(ScriptedSession):
        def evaluate(self, expression: str, **kwargs: object) -> EvaluationResult:
            if (
                "УстановитьПинПоколенияWorker" in expression
                or "ОчиститьПинПоколенияWorker" in expression
            ):
                return evaluation("Булево", "Истина")
            if (
                self.continue_count == 3
                and expression.startswith("ПоместитьВоВременноеХранилище(")
            ):
                return evaluation(
                    "Ошибка",
                    "planned recapture setup failure",
                    error="planned recapture setup failure",
                )
            return super().evaluate(expression, **kwargs)

    session = RecaptureFailureSession((CAPTURE_A, USER, CAPTURE_B))
    controller = PrototypeRuntimeController(session, SERVICE)
    api = PrototypeRuntimeApi(
        controller,
        capture_points=(CAPTURE_A, CAPTURE_B),
        user_breakpoints=(USER,),
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=_UniverseInstructionExecutor(),
    )
    g17 = api.load_worker_modules(
        (_worker_module_unit("МодульА", 17, catalog),),
        common_modules=catalog,
    )
    assert (
        api.execute_bsl("Результат = МодульА.Версия();").kind
        is RuntimeReplyKind.CAPTURED
    )
    original_pin = api._operation_generation_pin
    assert original_pin is not None and original_pin.handle is g17
    g18 = api.load_worker_modules(
        (_worker_module_unit("МодульА", 18, catalog),),
        common_modules=catalog,
    )

    assert api.resume_capture().kind is RuntimeReplyKind.DEBUG_STOPPED
    with pytest.raises(BslExecutionError, match="planned recapture setup failure"):
        api.resume_debug_stop()

    retained_views = api._worker_universe._retained_debug_views()
    assert tuple(view.handle for view in retained_views) == (g17, g18)
    lease = api._worker_universe._leases[original_pin.lease_id]
    assert lease.pin is original_pin
    assert lease.outcome_unknown is True
    assert api._worker_universe._generations[g17.generation].operation_pins == 1
    assert set(api._worker_generation_diagnostics) == {
        g17.manifest_sha256,
        g18.manifest_sha256,
    }
    with pytest.raises(WorkerPromotionOutcomeUnknown):
        api.status()

    api.close()


def test_close_tears_down_ambiguous_generation_without_target_context_slot(
    tmp_path: Path,
) -> None:
    """Quarantined generations are target-owned; no user-writable slot is cleaned."""
    catalog = _common_module_catalog("МодульА")
    module_a = _worker_module_unit("МодульА", 17, catalog)
    packer = _notebook_worker_builder(tmp_path)
    target = _UniverseInstructionExecutor()
    controller = _PinnedOperationController()
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=target,
    )
    api.load_worker_modules((module_a,), common_modules=catalog)
    assert api.execute_bsl("Результат = Capture();").kind is RuntimeReplyKind.CAPTURED

    def lose_resume(**kwargs: object) -> object:
        dispatch = kwargs["on_transport_dispatch"]
        assert callable(dispatch)
        dispatch()
        raise TimeoutError("planned ambiguous resume")

    controller.resume = lose_resume  # type: ignore[method-assign]
    with pytest.raises(TimeoutError, match="ambiguous resume"):
        api.resume_capture()

    api.close()

    assert not any(
        "onec-worker-operation-root-clear" in source for source in target.sources
    )
    assert target.disconnects == []


def test_close_tears_down_paused_generation_without_target_context_slot(
    tmp_path: Path,
) -> None:
    catalog = _common_module_catalog("МодульА")
    module_a = _worker_module_unit("МодульА", 17, catalog)
    packer = _notebook_worker_builder(tmp_path)
    target = _UniverseInstructionExecutor()
    controller = _PinnedOperationController()
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=target,
    )
    api.load_worker_modules((module_a,), common_modules=catalog)
    assert api.execute_bsl("Результат = Capture();").kind is RuntimeReplyKind.CAPTURED

    api.close()

    assert not any(
        "onec-worker-operation-root-clear" in source for source in target.sources
    )
    assert target.disconnects == []


def test_ordinary_main_holds_single_writer_from_pin_through_dispatch(
    tmp_path: Path,
) -> None:
    """G18 publication must not fit between the G17 pin and trusted prelude."""
    catalog = _common_module_catalog("МодульА")
    module_g17 = _worker_module_unit("МодульА", 17, catalog)
    module_g18 = _worker_module_unit("МодульА", 18, catalog)
    packer = _notebook_worker_builder(tmp_path)
    controller = _PinnedOperationController()
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=_UniverseInstructionExecutor(),
    )
    g17 = api.load_worker_modules((module_g17,), common_modules=catalog)
    entered_execution = Event()
    continue_execution = Event()
    original_execute = api._execute_bsl_with_generation_pin_locked

    def pause_after_pin(*args: object, **kwargs: object):
        entered_execution.set()
        assert continue_execution.wait(timeout=2)
        return original_execute(*args, **kwargs)

    api._execute_bsl_with_generation_pin_locked = pause_after_pin  # type: ignore[method-assign]
    replies: list[object] = []
    errors: list[BaseException] = []

    def run_main() -> None:
        try:
            replies.append(api.execute_bsl("Результат = МодульА.Версия();"))
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=run_main)
    thread.start()
    assert entered_execution.wait(timeout=2)
    try:
        with pytest.raises(ProtocolError, match="already executing"):
            api.load_worker_modules((module_g18,), common_modules=catalog)
    finally:
        continue_execution.set()
        thread.join(timeout=2)

    assert not thread.is_alive()
    assert errors == []
    assert len(replies) == 1
    assert api.worker_generation_handle is g17


def test_runtime_lowers_worker_call_through_the_pinned_nested_module_root(
    tmp_path: Path,
) -> None:
    catalog = _common_module_catalog("МодульА")
    module_a = _worker_module_unit("МодульА", 17, catalog)
    packer = _notebook_worker_builder(tmp_path)
    target = _UniverseInstructionExecutor()
    controller = _PinnedOperationController()
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=target,
    )
    api.load_worker_modules((module_a,), common_modules=catalog)

    api.execute_bsl("Результат = МодульА.Версия();")

    assert (
        '__OnecPinnedWorkerGeneration.Modules.Получить("МодульА").Версия()'
        in controller.main_mapped_sources[-1]
    )
    assert "Контекст.RuntimeWorker." not in controller.main_mapped_sources[-1]


def test_module_stage_compile_error_maps_through_exact_candidate_manifest(
    tmp_path: Path,
) -> None:
    catalog = _common_module_catalog("МодульА")
    module_a = _worker_module_unit("МодульА", 17, catalog)
    packer = _notebook_worker_builder(tmp_path)

    class CompileFailingTarget(_UniverseInstructionExecutor):
        def __call__(self, source: str) -> object:
            if f'"{WORKER_STAGE_SCHEMA}"' in source:
                _transaction, _index, _count, _digest, entries = (
                    _stage_source_identity(source)
                )
                registration = entries[0][0]
                return _stage_result(
                    source,
                    failure_phase="upload",
                    diagnostic=(
                    "{ОбщийМодуль.Host.Модуль(99,99)}: wrapper\n"
                    "{ВнешняяОбработка."
                    f"{registration}.МодульОбъекта(1,1)}}: compile failed "
                    "[ОшибкаКомпиляцииВстроенногоЯзыка]"
                    ),
                )
            return super().__call__(source)

    api = PrototypeRuntimeApi(
        FakeController(),
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=CompileFailingTarget(),
    )

    with pytest.raises(BslExecutionError) as raised:
        api.load_worker_modules((module_a,), common_modules=catalog)

    diagnostic = raised.value.diagnostic
    assert diagnostic is not None
    assert diagnostic.stage is DiagnosticStage.COMPILATION
    assert diagnostic.mapping_confidence is MappingConfidence.EXACT
    assert diagnostic.source_unit is not None
    assert (diagnostic.source_unit.unit_id, diagnostic.source_unit.revision) == (
        "МодульА",
        17,
    )
    assert diagnostic.platform_diagnostic is not None
    assert "compile failed" in diagnostic.platform_diagnostic


def test_local_diagnostic_admission_failure_discards_unstaged_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _common_module_catalog("МодульА")
    module_a = _worker_module_unit("МодульА", 17, catalog)
    packer = _notebook_worker_builder(tmp_path)
    target = _UniverseInstructionExecutor()
    api = PrototypeRuntimeApi(
        FakeController(),
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=target,
    )
    original = PrototypeRuntimeApi._worker_candidate_diagnostics

    def reject(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise ProtocolError("local diagnostic admission failed")

    monkeypatch.setattr(PrototypeRuntimeApi, "_worker_candidate_diagnostics", reject)
    with pytest.raises(ProtocolError, match="local diagnostic admission failed"):
        api.load_worker_modules((module_a,), common_modules=catalog)
    assert target.sources == []

    monkeypatch.setattr(PrototypeRuntimeApi, "_worker_candidate_diagnostics", original)
    handle = api.load_worker_modules((module_a,), common_modules=catalog)

    assert isinstance(handle, WorkerGenerationHandle)


def test_runtime_preserves_exact_sealed_candidate_diagnostics_after_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _common_module_catalog("МодульА", "МодульБ")
    packer = _notebook_worker_builder(tmp_path)
    api = PrototypeRuntimeApi(
        FakeController(),
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=_UniverseInstructionExecutor(),
    )
    returned: list[tuple[object, ...]] = []
    original = WorkerUniverseRegistry._candidate_diagnostics

    def capture(self: WorkerUniverseRegistry, candidate: WorkerUniverseCandidate):
        diagnostics = original(self, candidate)
        returned.append(diagnostics)
        return diagnostics

    monkeypatch.setattr(WorkerUniverseRegistry, "_candidate_diagnostics", capture)

    handle = api.load_worker_modules(
        (
            _worker_module_unit("МодульА", 17, catalog),
            _worker_module_unit("МодульБ", 18, catalog),
        ),
        common_modules=catalog,
    )

    retained = api._worker_generation_diagnostics[handle.manifest_sha256]
    assert returned == [retained]
    assert retained is returned[0]
    assert api._worker_universe._sealed_activations == {}
    assert tuple(item.revision for item in retained) == (17, 18)

    newer = api.load_worker_modules(
        (
            _worker_module_unit("МодульА", 19, catalog),
            _worker_module_unit("МодульБ", 20, catalog),
        ),
        common_modules=catalog,
    )

    assert len(returned) == 2
    assert handle.manifest_sha256 not in api._worker_generation_diagnostics
    assert api._worker_generation_diagnostics[newer.manifest_sha256] is returned[1]
    assert tuple(item.revision for item in retained) == (17, 18)


def test_runtime_failure_maps_callee_and_caller_through_operation_pin(
    tmp_path: Path,
) -> None:
    catalog = _common_module_catalog("МодульА", "МодульБ")
    module_a = _worker_module_unit("МодульА", 17, catalog)
    module_b = _worker_module_unit("МодульБ", 18, catalog)
    packer = _notebook_worker_builder(tmp_path)
    target = _UniverseInstructionExecutor()

    class FailedFrameController(_PinnedOperationController):
        error_text = ""

        def execute_mapped_main(
            self,
            visible: str,
            mapped: object,
            **kwargs: object,
        ) -> MainCompletion:
            dispatch = kwargs.get("on_transport_dispatch")
            if callable(dispatch):
                dispatch()
            self.main_mapped_sources.append(mapped.text)
            self.operation_id += 1
            self.state = OperationState.FAILED
            return MainCompletion(
                OperationHandle(self.operation_id, visible, mapped.text),
                None,
                self.error_text,
                False,
            )

    controller = FailedFrameController()
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=target,
    )
    api.load_worker_modules((module_a, module_b), common_modules=catalog)
    import re

    registrations: dict[str, str] = {}
    logical_names = {
        sha256(name.casefold().encode("utf-8")).hexdigest()[:8]: name
        for name in ("МодульА", "МодульБ")
    }
    for source in target.sources:
        if '"onec-worker-stage-batch-receipt"' not in source:
            continue
        for registration in set(
            re.findall(r"OnecRuntime_[0-9a-f]{8}_[0-9a-f]{16}", source)
        ):
            logical_digest = registration.split("_")[1]
            registrations[logical_names[logical_digest]] = registration
    controller.error_text = (
        "{ВнешняяОбработка."
        f'{registrations["МодульБ"]}.МодульОбъекта(1,1)}}: callee failed\n'
        "{ВнешняяОбработка."
        f'{registrations["МодульА"]}.МодульОбъекта(1,1)}}: caller'
    )

    reply = api.execute_bsl("Результат = МодульА.Версия();")

    assert reply.succeeded is False
    assert reply.diagnostic is not None
    assert [frame.logical_name for frame in reply.diagnostic.worker_frames] == [
        "МодульБ",
        "МодульА",
    ]
    assert [frame.revision for frame in reply.diagnostic.worker_frames] == [18, 17]


def test_prepared_g17_is_stale_after_same_catalog_g18_promotion_and_releases_once(
    tmp_path: Path,
) -> None:
    catalog = _common_module_catalog("МодульА")
    module_g17 = _worker_module_unit("МодульА", 17, catalog)
    module_g18 = _worker_module_unit("МодульА", 18, catalog)
    packer = _notebook_worker_builder(tmp_path)
    target = _UniverseInstructionExecutor()
    controller = _PinnedOperationController()
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=target,
    )
    g17 = api.load_worker_modules((module_g17,), common_modules=catalog)
    prepared = api.prepare_main_for_capture("Результат = МодульА.Версия();")
    g18 = api.load_worker_modules((module_g18,), common_modules=catalog)
    live_while_prepared = api._worker_universe._confirmed_live_inventory()
    assert live_while_prepared is not None
    assert live_while_prepared.manifest_sha256s == frozenset(
        (g17.manifest_sha256, g18.manifest_sha256)
    )

    with pytest.raises(ProtocolError, match="Prepared main is stale"):
        api.activate_prepared_main_for_capture(prepared)

    live_after_consume = api._worker_universe._confirmed_live_inventory()
    assert live_after_consume is not None
    assert live_after_consume.manifest_sha256s == frozenset((g18.manifest_sha256,))
    assert target.disconnects == []
    with pytest.raises(ProtocolError, match="already consumed"):
        api.activate_prepared_main_for_capture(prepared)
    assert target.disconnects == []


def test_activated_g17_is_stale_after_same_catalog_g18_and_releases_once(
    tmp_path: Path,
) -> None:
    """An activated capability retains G17 identity even when G18 has the same catalog."""
    catalog = _common_module_catalog("МодульА")
    module_g17 = _worker_module_unit("МодульА", 17, catalog)
    module_g18 = _worker_module_unit("МодульА", 18, catalog)
    packer = _notebook_worker_builder(tmp_path)
    target = _UniverseInstructionExecutor()
    controller = _PinnedOperationController()
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=target,
    )
    g17 = api.load_worker_modules((module_g17,), common_modules=catalog)
    activated = api.activate_prepared_main_for_capture(
        api.prepare_main_for_capture("Результат = МодульА.Версия();")
    )
    g18 = api.load_worker_modules((module_g18,), common_modules=catalog)
    live_while_activated = api._worker_universe._confirmed_live_inventory()
    assert live_while_activated is not None
    assert live_while_activated.manifest_sha256s == frozenset(
        (g17.manifest_sha256, g18.manifest_sha256)
    )

    with pytest.raises(ProtocolError, match="Activated prepared main is stale"):
        api.execute_prepared_main_for_capture(activated)

    live_after_consume = api._worker_universe._confirmed_live_inventory()
    assert live_after_consume is not None
    assert live_after_consume.manifest_sha256s == frozenset((g18.manifest_sha256,))
    assert api.operation_worker_generation is None
    assert api.worker_generation_handle is g18
    assert api._worker_universe._leases == {}
    assert target.disconnects == []
    assert api.status().state is OperationState.COMPLETED
    with pytest.raises(ProtocolError, match="already consumed"):
        api.execute_prepared_main_for_capture(activated)
    assert target.disconnects == []


def test_activated_main_restore_failure_releases_pin_once_without_poison(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Namespace setup is pre-dispatch, so its deterministic failure cannot quarantine."""
    catalog = _common_module_catalog("МодульА")
    module_g17 = _worker_module_unit("МодульА", 17, catalog)
    packer = _notebook_worker_builder(tmp_path)
    controller = _PinnedOperationController()
    target = _UniverseInstructionExecutor()
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=target,
    )
    api.load_worker_modules((module_g17,), common_modules=catalog)
    activated = api.activate_prepared_main_for_capture(
        api.prepare_main_for_capture("Результат = МодульА.Версия();")
    )
    release_calls: list[object] = []
    original_release = api._worker_universe_target.release_pin

    def record_release(pin: object) -> None:
        release_calls.append(pin)
        original_release(pin)  # type: ignore[arg-type]

    def fail_restore(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("planned activated namespace restore failure")

    monkeypatch.setattr(api._worker_universe_target, "release_pin", record_release)
    monkeypatch.setattr(api, "_restore_namespace_context", fail_restore)

    with pytest.raises(RuntimeError, match="activated namespace restore failure"):
        api.execute_prepared_main_for_capture(activated)

    assert release_calls == [activated.contents(api._prepared_main_owner).operation_pin]
    assert api.operation_worker_generation is None
    assert api._worker_universe._leases == {}
    assert controller.main_mapped_sources == []
    assert not any(
        "onec-worker-operation-root-clear" in source for source in target.sources
    )
    assert api.status().state is OperationState.COMPLETED
    with pytest.raises(ProtocolError, match="already consumed"):
        api.execute_prepared_main_for_capture(activated)
    assert len(release_calls) == 1


def test_prepared_main_capability_carries_and_discards_one_generation_pin(
    tmp_path: Path,
) -> None:
    """Break caught: abandoned preparation must release its exact lease once."""
    catalog = _common_module_catalog("МодульА", "МодульБ")
    module_a = _worker_module_unit("МодульА", 17, catalog)
    module_b = _worker_module_unit("МодульБ", 18, catalog)
    packer = _notebook_worker_builder(tmp_path)
    target = _UniverseInstructionExecutor()
    controller = _PinnedOperationController()
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=target,
    )
    g17 = api.load_worker_modules((module_a,), common_modules=catalog)
    observed: list[WorkerGenerationHandle | None] = []
    original_lower = controller.lowerer.lower_mapped

    def observe_pin(*args: object, **kwargs: object):
        observed.append(api.operation_worker_generation)
        return original_lower(*args, **kwargs)

    controller.lowerer.lower_mapped = observe_pin  # type: ignore[method-assign]

    prepared = api.prepare_main_for_capture("Результат = Capture();")

    assert observed == [g17]
    assert api.prepared_main_worker_generation(prepared) is g17
    g18 = api.load_worker_modules((module_b,), common_modules=catalog)
    live_while_prepared = api._worker_universe._confirmed_live_inventory()
    assert live_while_prepared is not None
    assert live_while_prepared.manifest_sha256s == frozenset(
        (g17.manifest_sha256, g18.manifest_sha256)
    )
    assert target.disconnects == []

    api.discard_prepared_main_for_capture(prepared)
    live_after_discard = api._worker_universe._confirmed_live_inventory()
    assert live_after_discard is not None
    assert live_after_discard.manifest_sha256s == frozenset((g18.manifest_sha256,))
    assert target.disconnects == []
    with pytest.raises(ProtocolError, match="already consumed"):
        api.discard_prepared_main_for_capture(prepared)
    assert target.disconnects == []


def test_unknown_main_outcome_quarantines_pin_and_poisons_runtime(
    tmp_path: Path,
) -> None:
    """Break caught: uncertain dispatch must never release its generation lease."""
    catalog = _common_module_catalog("МодульА")
    module_a = _worker_module_unit("МодульА", 17, catalog)
    packer = _notebook_worker_builder(tmp_path)
    target = _UniverseInstructionExecutor()
    controller = _PinnedOperationController()
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=target,
    )
    api.load_worker_modules((module_a,), common_modules=catalog)

    def lose_outcome(*_args: object, **kwargs: object) -> object:
        dispatch = kwargs.get("on_transport_dispatch")
        assert callable(dispatch)
        dispatch()
        controller.state = OperationState.RECOVERING
        raise TimeoutError("planned response loss")

    controller.execute_mapped_main = lose_outcome  # type: ignore[method-assign]

    with pytest.raises(TimeoutError, match="response loss"):
        api.execute_bsl("Результат = 1;")

    assert target.disconnects == []
    with pytest.raises(WorkerPromotionOutcomeUnknown):
        api.status()


def test_recovering_runtime_session_close_abandons_target_then_terminates_owned_runtime(
    tmp_path: Path,
) -> None:
    """RECOVERING cannot run BSL; host close still precedes transport teardown."""
    catalog = _common_module_catalog("МодульА")
    module_a = _worker_module_unit("МодульА", 17, catalog)
    packer = _notebook_worker_builder(tmp_path)
    target_id = TargetId(
        UUID("33333333-3333-3333-3333-333333333333"),
        "test-infobase",
    )

    def lose_transport(_point: FaultPoint) -> None:
        raise InjectedTransportFailure("planned owned transport loss")

    rdbg = SimpleNamespace(
        target=DebugTarget(target_id, "server", "Stopped", 7),
        heartbeat=lambda: {},
    )
    controller = PrototypeRuntimeController(
        rdbg,  # type: ignore[arg-type]
        LOCATION,
        fault_hook=lose_transport,
    )
    target = _UniverseInstructionExecutor()

    def recovering_target(source: str) -> object:
        if controller.state is OperationState.RECOVERING:
            raise ProtocolError("controller cannot execute while recovering")
        return target(source)

    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=recovering_target,
    )
    api.load_worker_modules((module_a,), common_modules=catalog)
    target_calls_before_close = len(target.sources)
    controller.operation_id = 1
    controller.active_operation = OperationHandle(1, "visible", "lowered")
    with pytest.raises(InjectedTransportFailure, match="owned transport loss"):
        controller._inject(FaultPoint.AFTER_CONTINUE_ACK, RecoveryPhase.RESUMING)
    assert controller.state is OperationState.RECOVERING
    close_events: list[str] = []

    class OwnedTransport:
        def close(self) -> None:
            close_events.append("transport")

    class OwnedProcesses:
        def close(self) -> None:
            close_events.append("processes")

    session = RuntimeSession(
        SimpleNamespace(source_root=None, runtime=SimpleNamespace(is_server_infobase=False)),  # type: ignore[arg-type]
        OwnedProcesses(),  # type: ignore[arg-type]
        OwnedTransport(),  # type: ignore[arg-type]
        rdbg,  # type: ignore[arg-type]
        api,
        SimpleNamespace(),  # type: ignore[arg-type]
        heartbeat_interval_s=60.0,
    )

    session.close()

    assert close_events == ["transport", "processes"]
    assert len(target.sources) == target_calls_before_close
    assert api._closed is True
    assert api._worker_universe.state.value == "closed"
    assert api._worker_universe_target._broken is True
    assert api._worker_universe_target._registrations == {}
    with pytest.raises(ProtocolError, match="closed"):
        api.status()


def test_local_failure_before_transport_dispatch_releases_without_poison(
    tmp_path: Path,
) -> None:
    catalog = _common_module_catalog("МодульА")
    module_a = _worker_module_unit("МодульА", 17, catalog)
    packer = _notebook_worker_builder(tmp_path)
    target = _UniverseInstructionExecutor()
    controller = _PinnedOperationController()
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=target,
    )
    api.load_worker_modules((module_a,), common_modules=catalog)

    def fail_before_transport(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("planned local controller setup failure")

    controller.execute_mapped_main = fail_before_transport  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="local controller setup failure"):
        api.execute_bsl("Результат = 1;")

    assert api.operation_worker_generation is None
    assert api._worker_universe._leases == {}
    assert api.status().state is OperationState.COMPLETED


def test_failed_controller_after_main_dispatch_quarantines_instead_of_releasing(
    tmp_path: Path,
) -> None:
    catalog = _common_module_catalog("МодульА")
    module_a = _worker_module_unit("МодульА", 17, catalog)
    packer = _notebook_worker_builder(tmp_path)
    target = _UniverseInstructionExecutor()
    controller = _PinnedOperationController()
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=target,
    )
    api.load_worker_modules((module_a,), common_modules=catalog)

    def lose_failed_outcome(*_args: object, **kwargs: object) -> object:
        dispatch = kwargs.get("on_transport_dispatch")
        assert callable(dispatch)
        dispatch()
        controller.state = OperationState.FAILED
        raise TimeoutError("planned failed response loss")

    controller.execute_mapped_main = lose_failed_outcome  # type: ignore[method-assign]

    with pytest.raises(TimeoutError, match="failed response loss"):
        api.execute_bsl("Результат = 1;")

    assert target.disconnects == []
    with pytest.raises(WorkerPromotionOutcomeUnknown):
        api.status()


def test_main_transport_loss_without_controller_transition_is_still_quarantined(
    tmp_path: Path,
) -> None:
    """Dispatch entry, not a controller state guess, establishes ambiguity."""
    catalog = _common_module_catalog("МодульА")
    module_a = _worker_module_unit("МодульА", 17, catalog)
    packer = _notebook_worker_builder(tmp_path)
    target = _UniverseInstructionExecutor()
    controller = _PinnedOperationController()
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=target,
    )
    api.load_worker_modules((module_a,), common_modules=catalog)

    def lose_outcome_without_state(*_args: object, **kwargs: object) -> object:
        dispatch = kwargs.get("on_transport_dispatch")
        assert callable(dispatch)
        dispatch()
        raise TimeoutError("planned state-free response loss")

    controller.execute_mapped_main = lose_outcome_without_state  # type: ignore[method-assign]

    with pytest.raises(TimeoutError, match="state-free response loss"):
        api.execute_bsl("Результат = 1;")

    assert target.disconnects == []
    with pytest.raises(WorkerPromotionOutcomeUnknown):
        api.status()


def test_failed_controller_after_continue_quarantines_instead_of_releasing(
    tmp_path: Path,
) -> None:
    catalog = _common_module_catalog("МодульА")
    module_a = _worker_module_unit("МодульА", 17, catalog)
    packer = _notebook_worker_builder(tmp_path)
    target = _UniverseInstructionExecutor()
    controller = _PinnedOperationController()
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=target,
    )
    api.load_worker_modules((module_a,), common_modules=catalog)
    assert api.execute_bsl("Результат = Capture();").kind is RuntimeReplyKind.CAPTURED

    def lose_continue(**kwargs: object) -> object:
        dispatch = kwargs.get("on_transport_dispatch")
        assert callable(dispatch)
        dispatch()
        controller.state = OperationState.FAILED
        raise TimeoutError("planned continue response loss")

    controller.resume = lose_continue  # type: ignore[method-assign]

    with pytest.raises(TimeoutError, match="continue response loss"):
        api.resume_capture()

    assert target.disconnects == []
    with pytest.raises(WorkerPromotionOutcomeUnknown):
        api.status()


def test_continue_transport_loss_without_controller_transition_is_quarantined(
    tmp_path: Path,
) -> None:
    catalog = _common_module_catalog("МодульА")
    module_a = _worker_module_unit("МодульА", 17, catalog)
    packer = _notebook_worker_builder(tmp_path)
    target = _UniverseInstructionExecutor()
    controller = _PinnedOperationController()
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=target,
    )
    api.load_worker_modules((module_a,), common_modules=catalog)
    assert api.execute_bsl("Результат = Capture();").kind is RuntimeReplyKind.CAPTURED

    def lose_continue_without_state(**kwargs: object) -> object:
        dispatch = kwargs.get("on_transport_dispatch")
        assert callable(dispatch)
        dispatch()
        raise TimeoutError("planned state-free continue loss")

    controller.resume = lose_continue_without_state  # type: ignore[method-assign]

    with pytest.raises(TimeoutError, match="state-free continue loss"):
        api.resume_capture()

    assert target.disconnects == []
    with pytest.raises(WorkerPromotionOutcomeUnknown):
        api.status()


def test_known_main_reply_then_namespace_failure_releases_without_poison(
    tmp_path: Path,
) -> None:
    catalog = _common_module_catalog("МодульА")
    module_a = _worker_module_unit("МодульА", 17, catalog)
    packer = _notebook_worker_builder(tmp_path)
    controller = _PinnedOperationController()
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=_UniverseInstructionExecutor(),
    )
    api.load_worker_modules((module_a,), common_modules=catalog)

    def complete(visible: str, mapped: object, **kwargs: object) -> MainCompletion:
        dispatch = kwargs.get("on_transport_dispatch")
        if callable(dispatch):
            dispatch()
        controller.operation_id += 1
        controller.state = OperationState.COMPLETED
        return MainCompletion(
            OperationHandle(controller.operation_id, visible, mapped.text),
            42,
            "",
            True,
        )

    def fail_namespace(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("planned local namespace failure")

    controller.execute_mapped_main = complete  # type: ignore[method-assign]
    api._finalize_namespace_reply = fail_namespace  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="local namespace failure"):
        api.execute_bsl("Результат = 1;")

    assert api.operation_worker_generation is None
    assert api._worker_universe._leases == {}
    assert api.status().state is OperationState.COMPLETED


def test_known_prepared_main_reply_then_namespace_failure_releases_once(
    tmp_path: Path,
) -> None:
    catalog = _common_module_catalog("МодульА")
    module_a = _worker_module_unit("МодульА", 17, catalog)
    packer = _notebook_worker_builder(tmp_path)
    controller = _PinnedOperationController()
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=_UniverseInstructionExecutor(),
    )
    api.load_worker_modules((module_a,), common_modules=catalog)

    def complete(visible: str, mapped: object, **kwargs: object) -> MainCompletion:
        dispatch = kwargs.get("on_transport_dispatch")
        if callable(dispatch):
            dispatch()
        controller.operation_id += 1
        controller.state = OperationState.COMPLETED
        return MainCompletion(
            OperationHandle(controller.operation_id, visible, mapped.text),
            42,
            "",
            True,
        )

    controller.execute_mapped_main = complete  # type: ignore[method-assign]
    activated = api.activate_prepared_main_for_capture(
        api.prepare_main_for_capture("Результат = 1;")
    )

    def fail_namespace(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("planned prepared namespace failure")

    api._finalize_namespace_reply = fail_namespace  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="prepared namespace failure"):
        api.execute_prepared_main_for_capture(activated)

    assert api.operation_worker_generation is None
    assert api._worker_universe._leases == {}
    assert api.status().state is OperationState.COMPLETED
    with pytest.raises(ProtocolError, match="already consumed"):
        api.execute_prepared_main_for_capture(activated)


def test_prepared_capture_transport_loss_quarantines_evaluation_and_retains_original_pin(
    tmp_path: Path,
) -> None:
    catalog = _common_module_catalog("МодульА")
    module_a = _worker_module_unit("МодульА", 17, catalog)
    packer = _notebook_worker_builder(tmp_path)
    target = _UniverseInstructionExecutor()
    controller = _PinnedOperationController()
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=target,
    )
    api.load_worker_modules((module_a,), common_modules=catalog)
    assert api.execute_bsl("Результат = Capture();").kind is RuntimeReplyKind.CAPTURED
    prepared = api.prepare_capture_hypothesis("Результат = 1;")
    original_generation = api.operation_worker_generation

    def lose_capture_reply(*_args: object, **kwargs: object) -> object:
        dispatch = kwargs["on_transport_dispatch"]
        assert callable(dispatch)
        dispatch()
        raise TimeoutError("planned prepared CAPTURE response loss")

    controller.execute_mapped_capture = lose_capture_reply  # type: ignore[method-assign]

    with pytest.raises(TimeoutError, match="prepared CAPTURE response loss"):
        api.execute_prepared_capture_hypothesis(prepared)

    assert target.disconnects == []
    assert api.operation_worker_generation is original_generation
    with pytest.raises(WorkerPromotionOutcomeUnknown):
        api.status()


def test_activated_prepared_main_keeps_same_pin_until_resume(tmp_path: Path) -> None:
    """Break caught: resealing MAIN must not mint or substitute a second pin."""
    catalog = _common_module_catalog("МодульА", "МодульБ")
    module_a = _worker_module_unit("МодульА", 17, catalog)
    module_b = _worker_module_unit("МодульБ", 18, catalog)
    packer = _notebook_worker_builder(tmp_path)
    target = _UniverseInstructionExecutor()
    controller = _PinnedOperationController()
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=target,
    )
    g17 = api.load_worker_modules((module_a,), common_modules=catalog)

    prepared = api.prepare_main_for_capture("Результат = Capture();")
    activated = api.activate_prepared_main_for_capture(prepared)

    assert api.activated_main_worker_generation(activated) is g17
    captured = api.execute_prepared_main_for_capture(activated)
    assert captured.kind is RuntimeReplyKind.CAPTURED
    assert api.operation_worker_generation is g17

    api.release_worker_generation(g17)
    api.load_worker_modules((module_b,), common_modules=catalog)
    assert target.disconnects == []
    api.resume_capture()
    assert target.disconnects == []


def test_resume_releases_generation_pin_when_namespace_finalization_fails(
    tmp_path: Path,
) -> None:
    """Break caught: a known terminal reply must release in a real finally."""
    catalog = _common_module_catalog("МодульА", "МодульБ")
    module_a = _worker_module_unit("МодульА", 17, catalog)
    module_b = _worker_module_unit("МодульБ", 18, catalog)
    packer = _notebook_worker_builder(tmp_path)
    target = _UniverseInstructionExecutor()
    controller = _PinnedOperationController()
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=target,
    )
    first = api.load_worker_modules((module_a,), common_modules=catalog)
    assert api.execute_bsl("Результат = Capture();").kind is RuntimeReplyKind.CAPTURED
    api.release_worker_generation(first)
    api.load_worker_modules((module_b,), common_modules=catalog)

    def fail_namespace(_reply: object) -> None:
        raise RuntimeError("planned namespace finalization failure")

    api._finalize_pending_namespace = fail_namespace  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="namespace finalization failure"):
        api.resume_capture()

    assert api.operation_worker_generation is None
    assert target.disconnects == []


def test_runtime_close_releases_active_and_pinned_generation_once(
    tmp_path: Path,
) -> None:
    catalog = _common_module_catalog("МодульА", "МодульБ")
    module_a = _worker_module_unit("МодульА", 17, catalog)
    module_b = _worker_module_unit("МодульБ", 18, catalog)
    packer = _notebook_worker_builder(tmp_path)
    target = _UniverseInstructionExecutor()
    controller = _PinnedOperationController()
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=target,
    )
    first = api.load_worker_modules((module_a,), common_modules=catalog)
    captured = api.execute_bsl("Результат = Capture();")
    assert captured.kind is RuntimeReplyKind.CAPTURED
    api.release_worker_generation(first)
    api.load_worker_modules((module_b,), common_modules=catalog)

    api.close()
    api.close()

    assert target.disconnects == []
    with pytest.raises(ProtocolError, match="closed"):
        api.status()


def test_stale_prepared_main_activation_releases_its_generation_pin(
    tmp_path: Path,
) -> None:
    catalog = _common_module_catalog("МодульА", "МодульБ")
    module_a = _worker_module_unit("МодульА", 17, catalog)
    module_b = _worker_module_unit("МодульБ", 18, catalog)
    packer = _notebook_worker_builder(tmp_path)
    target = _UniverseInstructionExecutor()
    controller = _PinnedOperationController()
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=target,
    )
    first = api.load_worker_modules((module_a,), common_modules=catalog)
    prepared = api.prepare_main_for_capture("Результат = 1;")
    api.release_worker_generation(first)
    api.load_worker_modules((module_b,), common_modules=catalog)
    controller.operation_id += 1

    with pytest.raises(ProtocolError, match="Prepared main is stale"):
        api.activate_prepared_main_for_capture(prepared)

    assert target.disconnects == []


@pytest.mark.parametrize(
    "handle",
    (
        "Контекст.RuntimeWorkerActiveGeneration",
        "Контекст.RuntimeWorkerActiveGeneration.Modules",
        "Контекст.RuntimeWorkerActiveGeneration.Modules.МодульА",
        "__OnecPinnedWorkerGeneration.Modules.МодульА",
    ),
)
def test_runtime_rejects_worker_generation_materialization_before_controller(
    handle: str,
) -> None:
    controller = FakeController()
    api = PrototypeRuntimeApi(controller)

    with pytest.raises(
        ProtocolError,
        match="^Worker generation objects are not public values$",
    ):
        api.materialize_value(handle)

    assert controller.main_sources == []
    assert controller.capture_sources == []


def test_runtime_rejects_operation_pin_returned_as_user_value(tmp_path: Path) -> None:
    catalog = _common_module_catalog("МодульА")
    module_a = _worker_module_unit("МодульА", 17, catalog)
    packer = _notebook_worker_builder(tmp_path)
    target = _UniverseInstructionExecutor()
    controller = _PinnedOperationController()
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=packer,
        worker_module_builder=WorkerModuleArtifactBuilder(
            packer,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=target,
    )
    api.load_worker_modules((module_a,), common_modules=catalog)

    def leak_pin(visible: str, lowered, **_kwargs: object) -> MainCompletion:  # type: ignore[no-untyped-def]
        del lowered
        assert api._operation_generation_pin is not None
        controller.operation_id += 1
        controller.state = OperationState.COMPLETED
        return MainCompletion(
            OperationHandle(controller.operation_id, visible, visible),
            api._operation_generation_pin,
            "",
            True,
        )

    controller.execute_mapped_main = leak_pin  # type: ignore[method-assign]

    with pytest.raises(
        ProtocolError,
        match="^Worker generation objects are not public values$",
    ):
        api.execute_bsl("Результат = 1;")

    assert api.operation_worker_generation is None
