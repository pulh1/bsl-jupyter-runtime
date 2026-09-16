from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
import json
import re
from uuid import uuid4

import pytest

from onec_runtime.bsl import (
    LoweringMode,
    SemanticLoweringResult,
    SemanticNotebookLowerer,
    SourceArtifactKind,
    SourceSpan,
    SourceTransformBuilder,
    SourceUnitKind,
    SourceUnitRef,
    WorkerExport,
    mapped_visible_source,
)
from onec_runtime.bsl.notebook_cells import split_notebook_cell
from onec_runtime.bsl.parser_target import PythonParserTarget
from onec_runtime.artifacts import ArtifactWriter
from onec_runtime.config import RuntimeConfig
from onec_runtime.capture_evaluation import CaptureResumeTicket
from onec_runtime.errors import PoisonedRuntimeError, ProtocolError
from onec_runtime_jupyter.extension import MACHINE_MIME_TYPE, _display_reply
from onec_runtime.prototype_runtime import (
    CaptureCellResult,
    MainCompletion,
    OperationHandle,
    OperationState,
)
from onec_runtime.runtime_api import PrototypeRuntimeApi, RuntimeReply, RuntimeReplyKind
from onec_runtime.recovery_journal import RecoveryJournal
from onec_runtime.worker_stage_protocol import (
    WORKER_STAGE_SCHEMA,
    WORKER_STAGE_SCHEMA_VERSION,
)
from onec_runtime.server_worker import (
    NotebookWorkerArtifactBuilder,
    WorkerArtifact,
    build_worker_artifact,
)


@dataclass
class RecordingLowerer:
    exports: tuple[WorkerExport, ...] = ()

    def __post_init__(self) -> None:
        self.calls: list[tuple[str, LoweringMode]] = []

    def set_worker_exports(self, exports: tuple[WorkerExport, ...]) -> None:
        self.exports = exports

    @staticmethod
    def prepare_worker_exports(
        exports: tuple[WorkerExport, ...],
    ) -> tuple[WorkerExport, ...]:
        catalog: dict[str, WorkerExport] = {}
        for export in exports:
            key = export.public_path.casefold()
            if key in catalog:
                raise ValueError(f"duplicate worker export {export.public_path!r}")
            catalog[key] = export
        return tuple(catalog.values())

    def commit_worker_exports(self, exports: tuple[WorkerExport, ...]) -> None:
        self.exports = exports

    def force_commit_worker_exports(self, exports: tuple[WorkerExport, ...]) -> None:
        self.exports = exports

    def lower(
        self,
        source: str,
        *,
        mode: LoweringMode,
        message_collector_key: str = "__onec_cell_messages",
    ) -> SemanticLoweringResult:
        del message_collector_key
        self.calls.append((source, mode))
        export_method = self.exports[0].method if self.exports else ""
        digest = sha256(source.encode("utf-8")).hexdigest()
        visible = mapped_visible_source(
            source,
            SourceUnitRef(SourceUnitKind.NOTEBOOK_CELL, digest, 0, digest),
        )
        builder = SourceTransformBuilder(visible)
        builder.synthetic(
            f"LOWERED[{mode.value}:{export_method}] ",
            SourceSpan(0, 0),
            "recording_lowerer_prefix",
        )
        builder.copy(SourceSpan(0, len(source)))
        return SemanticLoweringResult(
            mapped_source=builder.build(
                SourceArtifactKind.SEMANTIC_LOWERING,
                mode=mode.value,
            ),
            context_names=(),
            dirty_roots=("Скаляр",) if mode is LoweringMode.CAPTURE else (),
            persistent_write_roots=(),
            worker_dependencies=tuple(item.public_path for item in self.exports),
            messages_intercepted=1 if "Сообщить" in source else 0,
            edits=(),
        )


class _ImmediateResumeOwner:
    @staticmethod
    def _wait_resume_initiator(
        record: SimpleNamespace,
        _timeout_s: float | None,
    ) -> object:
        if record.error is not None:
            raise record.error
        return record.result

    @staticmethod
    def _detach_resume_initiator(record: SimpleNamespace) -> None:
        record.detached = True

    @staticmethod
    def _resume_initiator_detached(record: SimpleNamespace) -> bool:
        return bool(record.detached)


_IMMEDIATE_RESUME_OWNER = _ImmediateResumeOwner()


class FakeController:
    runtime_generation = 1

    def __init__(self, lowerer: RecordingLowerer) -> None:
        self.lowerer = lowerer
        self.state = OperationState.COMPLETED
        self.operation_id = 0
        self.main_sources: list[str] = []
        self.execute_main_calls = 0
        self.capture_sources: list[str] = []
        self.resume_roots: list[tuple[str, ...]] = []
        self.fail_resume = False
        self.worker_results = deque(("v1", "v2"))

    def execute_main(self, source: str, **kwargs: object) -> MainCompletion:
        self.execute_main_calls += 1
        self.main_sources.append(source)
        self.operation_id += 1
        dispatch = kwargs.get("on_transport_dispatch")
        if callable(dispatch):
            dispatch()
        return MainCompletion(OperationHandle(self.operation_id, source, source), None, "", True)

    def execute_capture(
        self,
        source: str,
        *,
        on_transport_dispatch=None,  # type: ignore[no-untyped-def]
    ) -> CaptureCellResult:
        self.capture_sources.append(source)
        if on_transport_dispatch is not None:
            on_transport_dispatch()
        return CaptureCellResult(self.operation_id, source, source, None)

    def execute_mapped_main(
        self,
        _visible_source: str,
        mapped_source: object,
        **kwargs: object,
    ) -> MainCompletion:
        return self.execute_main(mapped_source.text, **kwargs)  # type: ignore[attr-defined]

    def execute_mapped_capture(
        self,
        _visible_source: str,
        mapped_source: object,
        **kwargs: object,
    ) -> CaptureCellResult:
        return self.execute_capture(  # type: ignore[attr-defined]
            mapped_source.text,
            on_transport_dispatch=kwargs.get("on_transport_dispatch"),
        )

    def execute_system_main(self, source: str) -> MainCompletion:
        self.main_sources.append(source)
        self.operation_id += 1
        if f'"{WORKER_STAGE_SCHEMA}"' in source:
            result: object = _stage_result(source)
        elif "onec-worker-root-prepare-stage=" in source:
            result = _prepared_root_result(source)
        elif "onec-worker-root-swap-stage=guard" in source:
            result = _root_swap_result(source)
        else:
            result = self.worker_results.popleft()
        return MainCompletion(
            OperationHandle(self.operation_id, source, source),
            result,
            "",
            True,
        )

    def execute_system_capture(
        self,
        source: str,
        *,
        evaluation_kind: object,
    ) -> CaptureCellResult:
        del evaluation_kind
        self.capture_sources.append(source)
        return CaptureCellResult(self.operation_id, source, source, self.worker_results.popleft())

    def _resume_owned(
        self,
        *,
        dirty_roots: tuple[str, ...] = (),
        on_transport_dispatch=None,  # type: ignore[no-untyped-def]
    ) -> MainCompletion:
        self.resume_roots.append(dirty_roots)
        if self.fail_resume:
            raise ProtocolError("planned resume failure")
        if on_transport_dispatch is not None:
            on_transport_dispatch()
        self.state = OperationState.COMPLETED
        return MainCompletion(OperationHandle(self.operation_id, "main", "main"), None, "", True)

    def submit_resume(self, **kwargs: object) -> CaptureResumeTicket:
        before_resume = kwargs.pop("before_resume", None)
        completion = kwargs.pop("completion", None)
        kwargs.pop("detached_completion", None)
        result: object | None = None
        error: BaseException | None = None
        try:
            if callable(before_resume):
                before_resume(object())
            result = self._resume_owned(**kwargs)
        except BaseException as caught:
            error = caught
        if callable(completion):
            try:
                result = completion(result, error)
            except BaseException as caught:
                if error is None:
                    error = caught
                    result = None
        record = SimpleNamespace(result=result, error=error, detached=False)
        return CaptureResumeTicket(
            uuid4().hex,
            _IMMEDIATE_RESUME_OWNER,  # type: ignore[arg-type]
            record,  # type: ignore[arg-type]
        )

    def resume_debug_stop(
        self,
        *,
        on_transport_dispatch=None,  # type: ignore[no-untyped-def]
    ) -> MainCompletion:
        del on_transport_dispatch
        raise AssertionError("not used")


def artifact(tmp_path: Path, version: str, exports: tuple[WorkerExport, ...]) -> WorkerArtifact:
    path = tmp_path / f"Worker-{version}.epf"
    path.write_bytes(version.encode("ascii"))
    source = tmp_path / f"Worker-{version}.bsl"
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
        expected_value=1,
        exports=exports,
    )


def _stage_result(source: str) -> str:
    header = re.search(
        r'"onec-worker-stage-batch-receipt", 2, '
        r'"([0-9a-f-]{36})", (\d+), (\d+), "([0-9a-f]{64})", '
        r'СтатусWorker',
        source,
    )
    assert header is not None
    entries = re.findall(
        r'Новый Структура\('
        r'"registration_name,artifact_sha256,temp_storage_url", '
        r'"([A-Za-z_][A-Za-z0-9_]*)", "([0-9a-f]{64})", '
        r'АдресАртефактаWorker(\d+)\);',
        source,
    )
    transaction_id = header.group(1)
    return json.dumps(
        {
            "schema": WORKER_STAGE_SCHEMA,
            "schema_version": WORKER_STAGE_SCHEMA_VERSION,
            "transaction_id": transaction_id,
            "batch_index": int(header.group(2)),
            "batch_count": int(header.group(3)),
            "batch_digest": header.group(4),
            "status": "succeeded",
            "connected": [
                {
                    "registration_name": name,
                    "artifact_sha256": artifact_sha256,
                    "temp_storage_url": (
                        f"e1cib/tempstorage/{transaction_id}-{index}"
                        "?seanceId=notebook-test"
                    ),
                }
                for name, artifact_sha256, index in entries
            ],
            "failure": False,
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
    root = re.search(r'Вставить\("CandidateRootKey", "([^"|]+)"\);', source)
    previous = re.search(r'Вставить\("PreviousRootKey", "([^"|]*)"\);', source)
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
    generation = re.search(r'Формат\((\d+), "ЧГ=0; ЧДЦ=0; ЧН=0"\)', source)
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


def test_api_lowers_main_and_capture_once_and_flushes_discovered_dirty_root(tmp_path: Path) -> None:
    lowerer = RecordingLowerer()
    controller = FakeController(lowerer)
    api = PrototypeRuntimeApi(controller)

    main = api.execute_bsl("ГДФЛ = Расчет.Ндфл.Посчитать();")
    controller.state = OperationState.CAPTURED
    capture = api.execute_bsl("КонтекстОтладки.Скаляр = 41;")
    resumed = api.resume_capture(dirty_roots=("Результат", "Скаляр"))

    assert main.kind is RuntimeReplyKind.MAIN_COMPLETED
    assert capture.kind is RuntimeReplyKind.CAPTURE_CELL
    assert resumed.kind is RuntimeReplyKind.MAIN_COMPLETED
    assert lowerer.calls == [
        ("ГДФЛ = Расчет.Ндфл.Посчитать();", LoweringMode.MAIN),
        ("КонтекстОтладки.Скаляр = 41;", LoweringMode.CAPTURE),
    ]
    assert controller.main_sources[0] == "LOWERED[main:] ГДФЛ = Расчет.Ндфл.Посчитать();"
    assert controller.capture_sources[0] == "LOWERED[capture:] КонтекстОтладки.Скаляр = 41;"
    assert controller.resume_roots == [("Скаляр", "Результат")]


def test_generated_ast_splits_mixed_cell_into_exported_worker_methods_and_main_statements() -> None:
    source = (
        "Функция Посчитать() Экспорт\n"
        "    Возврат 41;\n"
        "КонецФункции;\n"
        "Результат = Посчитать();"
    )

    cell = split_notebook_cell(PythonParserTarget.from_generated(), source)

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

    cell = split_notebook_cell(PythonParserTarget.from_generated(), source)

    assert "Функция СкрытыйПомощник() Экспорт" in cell.worker_source
    assert cell.exports == (
        WorkerExport("СкрытыйПомощник", "СкрытыйПомощник"),
        WorkerExport("Посчитать", "Посчитать"),
    )


def test_mixed_cell_loads_methods_before_lowering_and_executing_main_statements(
    tmp_path: Path,
) -> None:
    lowerer = SemanticNotebookLowerer(PythonParserTarget.from_generated())
    controller = FakeController(lowerer)  # type: ignore[arg-type]
    built: list[str] = []

    def build_notebook_worker(source: str, exports: tuple[WorkerExport, ...]) -> WorkerArtifact:
        built.append(source)
        return artifact(tmp_path, "v1", exports)

    api = PrototypeRuntimeApi(controller, notebook_worker_builder=build_notebook_worker)
    source = (
        "Функция Посчитать()\n"
        "    Возврат 41;\n"
        "КонецФункции;\n"
        "Результат = Посчитать();"
    )

    reply = api.execute_bsl(source)

    assert built == [
        "Функция Посчитать() Экспорт\n"
        "    Возврат 41;\n"
        "КонецФункции"
    ]
    assert reply.kind is RuntimeReplyKind.MAIN_COMPLETED
    assert controller.execute_main_calls == 1
    executed = controller.main_sources[-1]
    generation = api.worker_generation_handle
    assert generation is not None
    assert executed.startswith(
        "__OnecPinnedWorkerGeneration = "
        "Контекст.RuntimeWorkerActiveGeneration;\n"
    )
    assert (
        "Если __OnecPinnedWorkerGeneration.ManifestSha256 <> "
        f'"{generation.manifest_sha256}" Тогда'
    ) in executed
    assert executed.endswith(
        'Результат = __OnecPinnedWorkerGeneration.Modules.Получить('
        '"Worker").Посчитать();'
    )
    PythonParserTarget.from_generated().parse(executed, "БлокНоутбука")


def test_method_only_cell_loads_worker_without_executing_main(tmp_path: Path) -> None:
    lowerer = SemanticNotebookLowerer(PythonParserTarget.from_generated())
    controller = FakeController(lowerer)  # type: ignore[arg-type]
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=lambda source, exports: artifact(tmp_path, "v1", exports),
    )

    reply = api.execute_bsl(
        "Процедура Обновить() Экспорт\nКонецПроцедуры;"
    )

    assert reply.kind is RuntimeReplyKind.WORKER_LOADED
    assert reply.result == api.worker_generation_handle
    assert controller.execute_main_calls == 0


def test_offline_notebook_worker_builder_projects_source_admits_artifact_and_returns_jupyter_reply(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    platform = tmp_path / "platform"
    platform.mkdir()
    for executable in ("1cv8.exe", "1cv8c.exe", "dbgs.exe"):
        (platform / executable).write_bytes(b"stub")
    config = RuntimeConfig(workspace, platform)
    controller = FakeController(SemanticNotebookLowerer(PythonParserTarget.from_generated()))  # type: ignore[arg-type]
    controller.worker_results = deque((True,))
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=NotebookWorkerArtifactBuilder(config),
    )

    reply = api.execute_bsl("Процедура Обновить()\nКонецПроцедуры;")

    assert reply.kind is RuntimeReplyKind.WORKER_LOADED
    generated = next(
        (config.runtime_dir / "generated" / "notebook-workers").glob(
            "*/Worker/Ext/ObjectModule.bsl"
        )
    )
    assert generated.read_text(encoding="utf-8") == "Процедура Обновить() Экспорт\nКонецПроцедуры"
    from onec_runtime.worker_epf import read_worker_source

    built = next((config.build_dir / "notebook-workers").glob("*.epf"))
    assert read_worker_source(built) == generated.read_text(encoding="utf-8")
    assert _display_reply(reply)._repr_mimebundle_()[MACHINE_MIME_TYPE]["kind"] == "worker_loaded"


def test_notebook_sources_and_system_operation_handles_are_redacted_from_outputs(
    tmp_path: Path,
) -> None:
    marker = "RAW_BSL_AND_EPF_MARKER"
    cell = split_notebook_cell(
        PythonParserTarget.from_generated(),
        f"Функция Метод() Экспорт\nСообщить(\"{marker}\");\nКонецФункции;",
    )
    operation = OperationHandle(7, marker, marker)
    completion = MainCompletion(operation, None, "", True)
    artifacts = ArtifactWriter(tmp_path, "privacy")
    artifacts.append_jsonl("events.jsonl", completion)
    captured: list[object] = []
    journal = RecoveryJournal(lambda _name, value: captured.append(value))
    journal.record("events.jsonl", "system", operation=operation)
    journal.flush()
    displayed = _display_reply(
        RuntimeReply(RuntimeReplyKind.MAIN_COMPLETED, 7, OperationState.COMPLETED, operation)
    )

    assert marker not in repr(cell)
    assert marker not in repr(operation)
    assert marker not in (artifacts.run_dir / "events.jsonl").read_text(encoding="utf-8")
    assert marker not in repr(captured)
    assert marker not in repr(displayed._repr_mimebundle_())


def test_failed_resume_keeps_discovered_dirty_roots_for_retry() -> None:
    lowerer = RecordingLowerer()
    controller = FakeController(lowerer)
    controller.state = OperationState.CAPTURED
    api = PrototypeRuntimeApi(controller)

    api.execute_bsl("КонтекстОтладки.Скаляр = 41;")
    controller.fail_resume = True
    with pytest.raises(ProtocolError, match="planned resume failure"):
        api.resume_capture()
    controller.fail_resume = False
    api.resume_capture()

    assert controller.resume_roots == [("Скаляр",), ("Скаляр",)]


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
