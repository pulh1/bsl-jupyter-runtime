"""Notebook AST projection through current route, Worker and reply owners."""

from __future__ import annotations

from contextlib import nullcontext
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import pytest

from onec_runtime.artifacts import ArtifactWriter
from onec_runtime.bsl import (
    LoweringMode, SemanticNotebookLowerer, SourceUnitKind, SourceUnitRef,
    VisibleSourceContext, WorkerExport,
)
from onec_runtime.bsl.notebook_cells import split_notebook_cell
from onec_runtime.bsl.parser_target import PythonParserTarget
from onec_runtime.config import RuntimeConfig
from onec_runtime.execution.capture.policy import CaptureCellPolicy
from onec_runtime.execution.capture.scope import CaptureScope
from onec_runtime.execution.capture.writeback import (
    CaptureExportFailed, CaptureWritebackExecutor, RootWritePhase,
    WritebackDisposition,
)
from onec_runtime.execution.common import NotebookCommonParser
from onec_runtime.execution.contracts import (
    PreparationContext, PreparationSnapshots, PreparedCell, SourceDiagnostic,
)
from onec_runtime.execution.main.policy import MainCellPolicy
from onec_runtime.execution.preparation import RoutePreparationInput
from onec_runtime_jupyter.extension import MACHINE_MIME_TYPE, _display_reply
from onec_runtime.rdbg.models import (
    EvaluationResult, ModuleLocation, PendingEvaluation, StackFrame, StopEvent,
    TargetId,
)
from onec_runtime.recovery_journal import RecoveryJournal
from onec_runtime.runtime_models import OperationState, RuntimeReply, RuntimeReplyKind
from onec_runtime.server_worker import NotebookWorkerArtifactBuilder
from onec_runtime.worker_epf import read_worker_source


PARSER = PythonParserTarget.from_generated()
SNAPSHOTS = PreparationSnapshots((), (), (1, 1))


def _common(source: str):
    digest = sha256(source.encode("utf-8")).hexdigest()
    unit = SourceUnitRef(SourceUnitKind.NOTEBOOK_CELL, digest, 1, digest)
    result = NotebookCommonParser(PARSER).prepare(source, unit)
    assert not isinstance(result, SourceDiagnostic)
    return result


class _CountingLowerer(SemanticNotebookLowerer):
    def __init__(self) -> None:
        super().__init__(PARSER)
        self.calls = 0

    def lower_mapped(self, *args, **kwargs):
        self.calls += 1
        return super().lower_mapped(*args, **kwargs)


class _Binding:
    def __init__(self, *, catalog: tuple[WorkerExport, ...] = (), intent: object = None) -> None:
        self.catalog = catalog
        self.intent = intent
        self.events: list[str] = []
        self.lowerers: list[_CountingLowerer] = []

    def worker_intent(self, common, snapshots):
        self.events.append("worker_intent")
        return self.intent

    def bind(self, common, snapshots):
        self.events.append("bind")
        lowerer = _CountingLowerer()
        self.lowerers.append(lowerer)
        return RoutePreparationInput(
            statement=common.source_maps.statement_execution,
            lowerer=lowerer,
            candidate_catalog=self.catalog,
            operation_pin=None,
            message_key_factory=lambda mode: "__messages_" + mode.value,
            complete_catalog=lambda catalog: catalog,
            pinned_catalog=lambda _pin: pytest.fail("unexpected pinned catalog"),
            temporary_catalog=lambda _lowerer, _catalog: nullcontext(),
            with_pin_prelude=lambda lowered, _pin, _mode: lowered,
        )


def _prepare(policy, source: str) -> PreparedCell:
    result = policy.prepare(
        _common(source), SNAPSHOTS,
        PreparationContext(object(), object(), policy, object()),
    )
    assert isinstance(result, PreparedCell)
    return result


def _ready_scope() -> CaptureScope:
    target = TargetId(UUID(int=1), "notebook-test")
    business = ModuleLocation("ConfigModule", "", UUID(int=2), UUID(int=3), 42)
    kernel = ModuleLocation("ExtensionModule", "", UUID(int=4), UUID(int=5), 60)
    stop = StopEvent(
        target, business, "callStackFormed",
        stack=(business, kernel),
        stack_frames=(
            StackFrame(target, 0, business),
            StackFrame(target, 1, kernel),
        ),
    )
    scope = CaptureScope.from_stop(1, 7, stop, 1)
    scope.record_locals(())
    scope.record_transfer("temporary-address")
    scope.record_kernel_frame(1)
    assert scope.record_main_command(7)
    scope.record_context_begun()
    scope.mark_ready()
    return scope


def test_route_policies_lower_main_and_capture_once_and_freeze_dirty_root() -> None:
    main_binding = _Binding()
    capture_binding = _Binding()
    main = _prepare(MainCellPolicy(main_binding), "ГДФЛ = Расчет.Ндфл.Посчитать();")
    capture = _prepare(
        CaptureCellPolicy(capture_binding),
        "КонтекстОтладки.Скаляр = 41;",
    )

    assert main.payload.statement.lowering.mapped_source.artifact.mode == LoweringMode.MAIN.value
    assert capture.payload.statement.lowering.mapped_source.artifact.mode == LoweringMode.CAPTURE.value
    assert main_binding.events == ["bind"]
    assert capture_binding.events == ["bind"]
    assert [lowerer.calls for lowerer in main_binding.lowerers] == [1]
    assert [lowerer.calls for lowerer in capture_binding.lowerers] == [1]
    assert capture.payload.dirty_roots == ("Скаляр",)
    scope = _ready_scope()
    scope.admit_cell_dirty_roots(capture.payload.dirty_roots)
    scope.admit_cell_dirty_roots(("Результат", "Скаляр"))
    assert scope.begin_writeback().roots == ("Скаляр", "Результат")


def test_generated_ast_splits_mixed_cell_into_exported_worker_methods_and_main_statements() -> None:
    source = (
        "Функция Посчитать() Экспорт\n"
        "    Возврат 41;\n"
        "КонецФункции;\n"
        "Результат = Посчитать();"
    )

    cell = split_notebook_cell(PARSER, source)

    assert cell.worker_source == source[: source.index("Результат")].rstrip()
    assert cell.statement_source == "Результат = Посчитать();"
    assert cell.exports == (WorkerExport("Посчитать", "Посчитать"),)


def test_generated_ast_projects_nonexported_notebook_methods_to_worker_catalog() -> None:
    source = (
        "Функция СкрытыйПомощник()\n"
        "    Возврат 41;\n"
        "КонецФункции;\n"
        "Функция Посчитать() Экспорт\n"
        "    Возврат СкрытыйПомощник();\n"
        "КонецФункции;\n"
        "Результат = Посчитать();"
    )

    cell = split_notebook_cell(PARSER, source)

    assert "Функция СкрытыйПомощник() Экспорт" in cell.worker_source
    assert cell.exports == (
        WorkerExport("СкрытыйПомощник", "СкрытыйПомощник"),
        WorkerExport("Посчитать", "Посчитать"),
    )


def test_mixed_cell_stages_worker_intent_and_defers_main_statement_until_activation() -> None:
    source = (
        "Функция Посчитать()\n"
        "    Возврат 41;\n"
        "КонецФункции;\n"
        "Результат = Посчитать();"
    )
    common = _common(source)
    candidate = object()
    prebuilt = object()
    binding = _Binding(catalog=common.parsed_units.exports, intent=candidate)

    def prebuild(intent: object) -> object:
        assert intent is candidate
        binding.events.append("prebuild")
        return prebuilt

    policy = MainCellPolicy(binding, prebuild_worker=prebuild)
    prepared = _prepare(policy, source)

    assert binding.events == ["worker_intent", "bind", "prebuild"]
    assert prepared.payload.worker_intent is prebuilt
    assert prepared.payload.statement is None
    deferred = prepared.payload.deferred_statement
    assert deferred is not None
    assert "Посчитать()" in deferred.lowering.source
    assert "RuntimeWorker" in deferred.lowering.source
    PARSER.parse(deferred.lowering.source, "БлокНоутбука")


def test_method_only_cell_stages_worker_without_main_statement() -> None:
    source = "Процедура Обновить() Экспорт\nКонецПроцедуры;"
    candidate = object()
    binding = _Binding(intent=candidate)
    policy = MainCellPolicy(binding)

    prepared = _prepare(policy, source)

    assert binding.events == ["worker_intent"]
    assert prepared.payload.worker_intent is candidate
    assert prepared.payload.statement is None
    assert prepared.payload.deferred_statement is None


def test_offline_notebook_worker_builder_admits_artifact_and_jupyter_worker_reply(
    tmp_path: Path,
) -> None:
    platform = tmp_path / "platform"
    platform.mkdir()
    for executable in ("1cv8.exe", "1cv8c.exe", "dbgs.exe"):
        (platform / executable).write_bytes(b"stub")
    config = RuntimeConfig(tmp_path, platform)
    source = "Процедура Обновить()\nКонецПроцедуры;"
    common = _common(source)
    mapped = common.source_maps.worker_candidate
    assert mapped is not None
    artifact = NotebookWorkerArtifactBuilder(config)(
        mapped, common.parsed_units.exports,
        visible_source_context=VisibleSourceContext({common.source_unit: source}),
    )

    generated = next(
        (config.runtime_dir / "generated" / "notebook-workers").glob(
            "*/Worker/Ext/ObjectModule.bsl"
        )
    )
    built = next((config.build_dir / "notebook-workers").glob("*.epf"))
    assert generated.read_text(encoding="utf-8") == read_worker_source(built)
    assert "Процедура Обновить() Экспорт" in generated.read_text(encoding="utf-8")
    assert artifact.exports == common.parsed_units.exports
    reply = RuntimeReply(RuntimeReplyKind.WORKER_LOADED, 0, OperationState.IDLE)
    assert _display_reply(reply)._repr_mimebundle_()[MACHINE_MIME_TYPE]["kind"] == "worker_loaded"


def test_notebook_source_is_redacted_from_artifacts_journal_and_jupyter_reply(
    tmp_path: Path,
) -> None:
    marker = "RAW_BSL_AND_EPF_MARKER"
    cell = split_notebook_cell(
        PARSER,
        f'Функция Метод() Экспорт\nСообщить("{marker}");\nКонецФункции;',
    )
    artifacts = ArtifactWriter(tmp_path, "privacy")
    artifacts.append_jsonl("events.jsonl", cell)
    captured: list[object] = []
    journal = RecoveryJournal(lambda _name, value: captured.append(value))
    journal.record("events.jsonl", "system", candidate=cell)
    journal.flush()
    displayed = _display_reply(RuntimeReply(
        RuntimeReplyKind.MAIN_COMPLETED, 7, OperationState.COMPLETED, cell,
    ))

    assert marker not in repr(cell)
    assert marker not in (artifacts.run_dir / "events.jsonl").read_text(encoding="utf-8")
    assert marker not in repr(captured)
    assert marker not in repr(displayed._repr_mimebundle_())


def test_confirmed_capture_export_failure_keeps_dirty_roots_for_retry() -> None:
    scope = _ready_scope()
    scope.admit_cell_dirty_roots(("Скаляр",))
    ledger = scope.begin_writeback()

    class FailedExportPort:
        def start_evaluation(
            self, expression: str, *, max_text_size: int,
            stack_level: int, timeout_s: float,
        ) -> PendingEvaluation:
            assert "Скаляр" in expression
            return PendingEvaluation(scope.identity.target_id, UUID(int=11), self)

        def wait_evaluation_event(
            self, pending: PendingEvaluation, *, timeout_s: float,
        ) -> EvaluationResult:
            return EvaluationResult(
                pending.result_id, "Ошибка", "", True, "private export error",
            )

        def modify(self, *_args: object, **_kwargs: object) -> None:
            pytest.fail("confirmed export failure must not modify frame")

    with pytest.raises(CaptureExportFailed):
        CaptureWritebackExecutor().flush(FailedExportPort(), ledger, stack_level=0)

    assert scope.dirty_roots == ("Скаляр",)
    assert scope.begin_writeback() is ledger
    assert ledger.disposition is WritebackDisposition.PAUSED_EXPORT_FAILED
    assert ledger.record("Скаляр").phase is RootWritePhase.FAILED
    ledger.retry_confirmed_export("Скаляр")
    assert ledger.record("Скаляр").phase is RootWritePhase.UNATTEMPTED
    assert scope.dirty_roots == ("Скаляр",)


def test_messages_are_exposed_in_reply_and_notebook_mime_payload() -> None:
    reply = RuntimeReply(
        RuntimeReplyKind.MAIN_COMPLETED,
        9,
        OperationState.COMPLETED,
        messages=("первое", "второе"),
    )

    displayed = _display_reply(reply)
    bundle = displayed._repr_mimebundle_()

    assert bundle[MACHINE_MIME_TYPE]["messages"] == ["первое", "второе"]
    assert "первое" not in bundle["text/plain"]
