from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from IPython.core.error import UsageError
from IPython.core.interactiveshell import InteractiveShell
from IPython.utils.capture import capture_output

import onec_runtime.privacy as privacy
from onec_runtime_jupyter import BslCellError
from onec_runtime_mcp.agent.contracts import OperationExecutionProvenance
from onec_runtime_jupyter.extension import (
    MACHINE_MIME_TYPE,
    NotebookDisplayConfig,
    OnecRuntimeMagics,
    _display_reply,
    install_runtime,
    load_ipython_extension,
    unload_ipython_extension,
)
from onec_runtime.capture_evaluation import (
    CaptureEvaluationKind,
    CapturePhase,
    CaptureStatus,
)
from onec_runtime.errors import CaptureEvaluationPendingError
from onec_runtime.capture_inspection import DebugFrame, StackPage
from onec_runtime.capture_values import (
    DeniedValueNode,
    SafeValuePath,
    ValueNode,
    ValuePage,
    ValueRoot,
    ValueRootKind,
    ValueShape,
)
from onec_runtime.prototype_runtime import OperationState
from onec_runtime.runtime_api import (
    RuntimeReply,
    RuntimeReplyKind,
    RuntimeStatus,
)
from onec_runtime.bsl import (
    DiagnosticStage,
    SourceArtifactKind,
    SourceSpan,
    SourceTransformBuilder,
    SourceUnitKind,
    SourceUnitRef,
    VisibleSourceContext,
    mapped_visible_source,
    parse_platform_diagnostic,
    remap_platform_diagnostic,
    source_sha256,
)


class FakeShell:
    def __init__(self) -> None:
        self.user_ns: dict[str, object] = {}
        self.registered: object | None = None

    def register_magics(self, magics: object) -> None:
        self.registered = magics


class FakeRuntime:
    def __init__(self) -> None:
        self.sources: list[str] = []
        self.source_units: list[SourceUnitRef] = []
        self.dirty_roots: tuple[str, ...] = ()
        self.capture_sources: list[tuple[str, Path]] = []

    def configure_capture_source(self, project: str, source_root: Path) -> None:
        self.capture_sources.append((project, source_root))

    def execute_bsl(
        self,
        source: str,
        *,
        source_unit: SourceUnitRef,
    ) -> RuntimeReply:
        self.sources.append(source)
        self.source_units.append(source_unit)
        return RuntimeReply(
            RuntimeReplyKind.CAPTURED,
            7,
            OperationState.CAPTURED,
            stop_sequence=1,
        )

    def resume_capture(self, *, dirty_roots: tuple[str, ...] = ()) -> RuntimeReply:
        self.dirty_roots = dirty_roots
        return RuntimeReply(
            RuntimeReplyKind.MAIN_COMPLETED,
            7,
            OperationState.COMPLETED,
            result=42,
        )

    def status(self) -> RuntimeStatus:
        return RuntimeStatus(OperationState.CAPTURED, 1, 7, None)

    def namespace_snapshot(self):  # type: ignore[no-untyped-def]
        from onec_runtime.runtime_api import RuntimeNamespaceSnapshot

        return RuntimeNamespaceSnapshot(1, 1, ("ГДФЛ",))

    def validate_value_reference(self, handle: str) -> None:
        del handle


def test_install_runtime_attaches_optional_project_bridge_and_contains_failure(monkeypatch):
    from onec_runtime_jupyter import lsp_kernel
    shell, runtime = FakeShell(), FakeRuntime()
    attached = []
    monkeypatch.setattr(lsp_kernel, 'install_project_bridge', lambda s, r: attached.append((s, r)))
    install_runtime(shell, runtime)
    assert attached == [(shell, runtime)]
    def fail(*args):
        raise RuntimeError('sensitive project path must not escape')
    monkeypatch.setattr(lsp_kernel, 'install_project_bridge', fail)
    install_runtime(shell, runtime)
    assert shell.user_ns['bsl'] is not None


def test_interactive_session_installs_core_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from onec_runtime_jupyter import InteractiveRuntimeSession
    from onec_runtime_jupyter import session as session_module

    shell = FakeShell()
    runtime = FakeRuntime()
    runtime.close = lambda: None  # type: ignore[attr-defined]
    monkeypatch.setattr(
        session_module.RuntimeSession,
        "start",
        lambda _config, *, progress: (progress("Запуск 1С"), runtime)[1],
    )

    session = InteractiveRuntimeSession.start(object(), shell=shell)  # type: ignore[arg-type]

    assert session.runtime is runtime
    assert shell.user_ns["_onec_runtime"] is runtime


def test_interactive_session_delegates_resolved_capture_source(
    tmp_path: Path,
) -> None:
    from onec_runtime_jupyter import InteractiveRuntimeSession

    runtime = FakeRuntime()
    session = InteractiveRuntimeSession(runtime)
    source_root = tmp_path / "source" / ".." / "source"

    session.configure_capture_source("ut", source_root)

    assert runtime.capture_sources == [("ut", source_root.resolve())]


def _exact_diagnostic(source: str, unit: SourceUnitRef):  # type: ignore[no-untyped-def]
    visible = mapped_visible_source(source, unit)
    builder = SourceTransformBuilder(visible)
    builder.synthetic(
        "// generated one\r\n// generated two\r\n",
        SourceSpan(0, 0),
        "runtime_prelude",
    )
    builder.copy(SourceSpan(0, len(source)))
    executed = builder.build(SourceArtifactKind.EXECUTED_BSL)
    return remap_platform_diagnostic(
        parse_platform_diagnostic(
            "{<Неизвестный модуль>(5,1)}: rdbg_pid=9182 "
            "token=private-connection"
        ),
        executed,
        stage=DiagnosticStage.EXECUTION,
        visible_source_context=VisibleSourceContext({unit: source}),
    )


def _failed_reply_with_exact_diagnostic() -> tuple[RuntimeReply, str]:
    source = 'Первая = "😀";\r\nВторая = 2;\r\nОшибка();'
    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL,
        "jupyter-test-session:execution:7",
        7,
        source_sha256(source),
    )
    diagnostic = _exact_diagnostic(source, unit)
    return (
        RuntimeReply(
            RuntimeReplyKind.MAIN_COMPLETED,
            9,
            OperationState.FAILED,
            error="RAW platform fallback pid=9182 token=private-connection",
            succeeded=False,
            diagnostic=diagnostic,
        ),
        source,
    )


@pytest.mark.parametrize(
    ("kind", "state", "magic"),
    [
        (
            RuntimeReplyKind.MAIN_COMPLETED, OperationState.FAILED,
            "%%bsl\nА = 1;\nБ = 2;\nОшибка();",
        ),
        (
            RuntimeReplyKind.CAPTURE_CELL, OperationState.CAPTURED,
            "%%bsl\nА = 1;\nБ = 2;\nОшибка();",
        ),
        (RuntimeReplyKind.MAIN_COMPLETED, OperationState.FAILED, "%bsl_resume"),
    ],
)
@pytest.mark.parametrize("normalized", [False, True])
@pytest.mark.parametrize("traceback_mode", ["Context", "Verbose"])
def test_failed_reply_is_an_ipython_error_with_safe_rich_diagnostics(
    kind: RuntimeReplyKind, state: OperationState, magic: str, normalized: bool,
    traceback_mode: str,
) -> None:
    """A failed reply must not make Run All regard the BSL cell as successful."""
    shell = InteractiveShell.instance()
    shell.InteractiveTB.set_mode(mode=traceback_mode)
    runtime = FakeRuntime()

    def failed(source: str = "", *, source_unit=None, **kwargs) -> RuntimeReply:
        diagnostic = None
        if normalized:
            diagnostic = (
                _exact_diagnostic(source, source_unit)
                if source_unit is not None
                else _failed_reply_with_exact_diagnostic()[0].diagnostic
            )
        return RuntimeReply(
            kind, 9, state, succeeded=False,
            error="RAW platform token=private-connection pid=9182",
            diagnostic=diagnostic,
        )

    runtime.execute_bsl = failed
    runtime.resume_capture = failed
    install_runtime(shell, runtime)
    load_ipython_extension(shell)
    try:
        with capture_output() as captured:
            result = shell.run_cell(magic)
        assert result.success is False
        assert isinstance(result.error_in_exec, BslCellError)
        assert "BSL" in str(result.error_in_exec)
        assert len(captured.outputs) == 1
        payload = captured.outputs[0].data[MACHINE_MIME_TYPE]
        assert payload["succeeded"] is False
        assert payload["kind"] == kind.value
        if normalized and magic.startswith("%%bsl"):
            assert payload["diagnostic"]["stage"] == "execution"
        rendered = captured.stdout + captured.stderr + json.dumps(
            captured.outputs[0].data, ensure_ascii=False
        ) + repr(result.error_in_exec)
        for secret in ("RAW platform", "private-connection", "9182"):
            assert secret not in rendered
    finally:
        InteractiveShell.clear_instance()


def test_runtime_exception_remains_an_ipython_error() -> None:
    shell = InteractiveShell.instance()
    runtime = FakeRuntime()

    def fail(*args, **kwargs):
        raise RuntimeError("controlled backend exception")

    runtime.execute_bsl = fail
    install_runtime(shell, runtime)
    load_ipython_extension(shell)
    try:
        with capture_output():
            result = shell.run_cell("%%bsl\nОшибка();")
        assert result.success is False
        assert type(result.error_in_exec) is RuntimeError
    finally:
        InteractiveShell.clear_instance()


@pytest.mark.parametrize("mode", ["presentation", "diagnostic"])
@pytest.mark.parametrize("action", ["call", "same_runtime_reinstall", "resume"])
def test_retained_cell_diagnostic_keeps_issued_identity_without_new_cell_excerpt(
    monkeypatch: pytest.MonkeyPatch, mode: str, action: str,
) -> None:
    shell = FakeShell()
    runtime = FakeRuntime()
    config = getattr(NotebookDisplayConfig, mode)()
    install_runtime(shell, runtime, display=config)
    magics = OnecRuntimeMagics(shell)  # type: ignore[arg-type]
    original = "Функция Старый()\nПеременная = 1;\nОшибка();\nКонецФункции"
    magics.bsl("", original)
    original_unit = runtime.source_units[0]
    failed = RuntimeReply(
        RuntimeReplyKind.CAPTURE_CELL, 9, OperationState.CAPTURED, succeeded=False,
        error="RAW token=private-connection", diagnostic=_exact_diagnostic(original, original_unit),
    )
    runtime.execute_bsl = lambda *args, **kwargs: failed
    runtime.resume_capture = lambda **kwargs: failed
    if action == "same_runtime_reinstall":
        install_runtime(shell, runtime, display=config)
    published = []
    monkeypatch.setattr("onec_runtime_jupyter.extension.display", published.append)
    with pytest.raises(BslCellError):
        if action == "resume":
            magics.bsl_resume("")
        else:
            magics.bsl("", "Результат = Старый(); // NEW_CELL_CONTENT_" * 4)
    payload = published[0].payload
    assert payload["source_unit"]["unit_id"] == original_unit.unit_id
    assert payload["source_unit"]["source_sha256"] == source_sha256(original)
    assert payload["diagnostic"]["visible_location"]["line"] == 3
    assert payload["diagnostic"]["excerpt"] is None
    if mode == "diagnostic":
        assert payload["diagnostic_details"]["excerpt"] is None
    rendered = json.dumps(payload, ensure_ascii=False)
    for forbidden in ("NEW_CELL_CONTENT", original, "private-connection"):
        assert forbidden not in rendered


@pytest.mark.parametrize("defect", ["hash", "unknown_unit", "replacement", "unload"])
def test_retained_diagnostic_identity_requires_current_attachment_issuance(
    monkeypatch: pytest.MonkeyPatch, defect: str,
) -> None:
    from onec_runtime_jupyter.extension import unload_ipython_extension

    shell = FakeShell()
    runtime = FakeRuntime()
    install_runtime(shell, runtime, display=NotebookDisplayConfig.diagnostic())
    magics = OnecRuntimeMagics(shell)  # type: ignore[arg-type]
    original = "Функция Старый()\nПеременная = 1;\nОшибка();\nКонецФункции"
    magics.bsl("", original)
    unit = runtime.source_units[0]
    diagnostic = _exact_diagnostic(original, unit)
    if defect in {"hash", "unknown_unit"}:
        wrong = replace(unit, **({"source_sha256": "f" * 64} if defect == "hash" else {"unit_id": "unknown-cell"}))
        diagnostic = replace(
            diagnostic, source_unit=wrong,
            visible_location=replace(diagnostic.visible_location, source_unit=wrong),
        )
    elif defect == "replacement":
        runtime = FakeRuntime()
        install_runtime(shell, runtime, display=NotebookDisplayConfig.diagnostic())
    else:
        unload_ipython_extension(shell)
        install_runtime(shell, runtime, display=NotebookDisplayConfig.diagnostic())
    runtime.execute_bsl = lambda *args, **kwargs: RuntimeReply(
        RuntimeReplyKind.MAIN_COMPLETED, 9, OperationState.FAILED,
        succeeded=False, diagnostic=diagnostic,
    )
    published = []
    monkeypatch.setattr("onec_runtime_jupyter.extension.display", published.append)
    with pytest.raises(BslCellError):
        magics.bsl("", "Результат = Старый();")
    assert published[0].payload["diagnostic"] == {}
    assert published[0].payload["diagnostic_details"] == {}
    assert "source_unit" not in published[0].payload


def test_unitless_unmapped_diagnostic_keeps_stage_without_invented_source() -> None:
    from onec_runtime.bsl import MappingConfidence

    reply, source = _failed_reply_with_exact_diagnostic()
    diagnostic = replace(
        reply.diagnostic, source_unit=None, visible_location=None,
        related_visible_span=None, mapping_confidence=MappingConfidence.UNKNOWN,
    )
    displayed = _display_reply(
        replace(reply, diagnostic=diagnostic), NotebookDisplayConfig.diagnostic(),
        visible_source=source, source_unit=reply.diagnostic.source_unit,
    )
    assert displayed.payload["diagnostic"]["stage"] == "execution"
    assert displayed.payload["diagnostic"]["mapping_confidence"] == "unknown"
    assert displayed.payload["diagnostic"]["visible_location"] is None
    assert displayed.payload["diagnostic_details"]["excerpt"] is None
    assert "source_unit" not in displayed.payload


def test_issued_origin_does_not_override_current_cell_text_hash_guard() -> None:
    reply, source = _failed_reply_with_exact_diagnostic()
    unit = reply.diagnostic.source_unit
    displayed = _display_reply(
        reply, NotebookDisplayConfig.diagnostic(), visible_source=source + "\n",
        source_unit=unit, known_source_unit=unit,
    )
    assert displayed.payload["diagnostic"] == {}
    assert "source_unit" not in displayed.payload


def test_bsl_magic_passes_visible_cell_and_returns_stable_mime_bundle() -> None:
    shell = FakeShell()
    runtime = FakeRuntime()
    install_runtime(shell, runtime)
    magics = OnecRuntimeMagics(shell)  # type: ignore[arg-type]

    displayed = magics.bsl("", "ГДФЛ = Расчет.Ндфл.Посчитать();")

    assert runtime.sources == ["ГДФЛ = Расчет.Ндфл.Посчитать();"]
    assert runtime.source_units[0].source_sha256 == source_sha256(
        "ГДФЛ = Расчет.Ндфл.Посчитать();"
    )
    assert displayed.payload["kind"] == "captured"
    assert displayed.payload["operation_id"] == 7
    bundle = displayed._repr_mimebundle_()
    assert bundle[MACHINE_MIME_TYPE] == displayed.payload
    assert "application/json" not in bundle
    assert "CAPTURED" in bundle["text/plain"]


def test_bsl_magic_renders_acknowledged_user_evaluation_pending_as_safe_mime_bundle() -> None:
    """VS Code receives this display through the ordinary Jupyter MIME path."""

    shell = FakeShell()
    runtime = FakeRuntime()
    runtime._poisoned_error = None  # type: ignore[attr-defined]
    evaluation_id = "a4f1d86e1e1d4d45b0948e021f669d1f"
    guidance = "runtime.current_capture().wait(timeout_s=10)"
    source = (
        "СекретныйИсточник = worker://private-handle; "
        "result_id=private-result; generation=987; url=https://private.invalid"
    )
    dispatches = 0

    def pending(
        sent: str,
        *,
        source_unit: SourceUnitRef,
    ) -> RuntimeReply:
        nonlocal dispatches
        dispatches += 1
        runtime.sources.append(sent)
        runtime.source_units.append(source_unit)
        raise CaptureEvaluationPendingError(
            evaluation_id,
            CaptureEvaluationKind.USER_BSL,
        )

    runtime.execute_bsl = pending  # type: ignore[method-assign]
    install_runtime(shell, runtime)

    displayed = OnecRuntimeMagics(shell).bsl("", source)  # type: ignore[arg-type]

    assert displayed is not None
    bundle = displayed._repr_mimebundle_()
    assert bundle["text/plain"] == (
        f"evaluation_id={evaluation_id}\n"
        "evaluation_kind=user_bsl\n"
        + guidance
    )
    assert bundle["text/html"] == (
        f"<pre>evaluation_id={evaluation_id}\n"
        "evaluation_kind=user_bsl\n"
        + guidance
        + "</pre>"
    )
    assert bundle[MACHINE_MIME_TYPE] == {
        "evaluation_id": evaluation_id,
        "evaluation_kind": "user_bsl",
        "guidance": guidance,
    }
    rendered = json.dumps(bundle, ensure_ascii=False)
    for secret in (
        "СекретныйИсточник",
        "worker://private-handle",
        "private-result",
        "generation=987",
        "https://private.invalid",
    ):
        assert secret not in rendered
    assert dispatches == 1
    assert runtime._poisoned_error is None  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    (
        "untrusted_evaluation_id",
        "untrusted_evaluation_kind",
        "expected_evaluation_id",
        "expected_evaluation_kind",
        "secret",
    ),
    (
        (
            "malformed-private-receipt",
            CaptureEvaluationKind.USER_BSL,
            "<unavailable>",
            "user_bsl",
            "malformed-private-receipt",
        ),
        (
            "https://private.invalid/evaluation",
            CaptureEvaluationKind.USER_BSL,
            "<unavailable>",
            "user_bsl",
            "private.invalid",
        ),
        (
            "worker://private-handle",
            CaptureEvaluationKind.USER_BSL,
            "<unavailable>",
            "user_bsl",
            "private-handle",
        ),
        (
            "generation=987-private",
            CaptureEvaluationKind.USER_BSL,
            "<unavailable>",
            "user_bsl",
            "987-private",
        ),
        (
            "result_id=private-result",
            CaptureEvaluationKind.USER_BSL,
            "<unavailable>",
            "user_bsl",
            "private-result",
        ),
        (
            "\x1b[31mprivate-control",
            CaptureEvaluationKind.USER_BSL,
            "<unavailable>",
            "user_bsl",
            "private-control",
        ),
        (
            "overlong-private-" + "a" * 300,
            CaptureEvaluationKind.USER_BSL,
            "<unavailable>",
            "user_bsl",
            "overlong-private",
        ),
        (
            "a4f1d86e-1e1d-4d45-b094-8e021f669d1f",
            "worker://private-kind",
            "a4f1d86e1e1d4d45b0948e021f669d1f",
            "unknown",
            "private-kind",
        ),
    ),
    ids=(
        "malformed-id",
        "url-id",
        "handle-id",
        "generation-id",
        "result-id",
        "control-id",
        "overlong-id",
        "untrusted-kind",
    ),
)
def test_bsl_magic_pending_receipt_redacts_untrusted_exception_fields(
    untrusted_evaluation_id: str,
    untrusted_evaluation_kind: object,
    expected_evaluation_id: str,
    expected_evaluation_kind: str,
    secret: str,
) -> None:
    """The Jupyter and VS Code MIME bundle never reflects exception fields."""

    shell = FakeShell()
    runtime = FakeRuntime()
    receipt = CaptureEvaluationPendingError(
        untrusted_evaluation_id,
        CaptureEvaluationKind.USER_BSL,
    )
    receipt.evaluation_kind = untrusted_evaluation_kind  # type: ignore[assignment]

    def pending(
        _source: str,
        *,
        source_unit: SourceUnitRef,
    ) -> RuntimeReply:
        del source_unit
        raise receipt

    runtime.execute_bsl = pending  # type: ignore[method-assign]
    install_runtime(shell, runtime)

    displayed = OnecRuntimeMagics(shell).bsl(  # type: ignore[arg-type]
        "", "РезультатИнструкции = 904;"
    )

    assert displayed is not None
    bundle = displayed._repr_mimebundle_()
    guidance = "runtime.current_capture().wait(timeout_s=10)"
    expected_text = (
        f"evaluation_id={expected_evaluation_id}\n"
        f"evaluation_kind={expected_evaluation_kind}\n"
        + guidance
    )
    assert bundle["text/plain"] == expected_text
    assert bundle["text/html"] == (
        "<pre>"
        + expected_text.replace("<", "&lt;").replace(">", "&gt;")
        + "</pre>"
    )
    assert bundle[MACHINE_MIME_TYPE] == {
        "evaluation_id": expected_evaluation_id,
        "evaluation_kind": expected_evaluation_kind,
        "guidance": guidance,
    }
    for rendered in (
        bundle["text/plain"],
        bundle["text/html"],
        json.dumps(bundle[MACHINE_MIME_TYPE], ensure_ascii=False),
    ):
        assert secret not in rendered


def test_presentation_mode_prints_bsl_messages_without_visible_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    shell = FakeShell()
    runtime = FakeRuntime()
    install_runtime(shell, runtime, display=NotebookDisplayConfig.presentation())
    magics = OnecRuntimeMagics(shell)  # type: ignore[arg-type]
    runtime.execute_bsl = lambda _source, *, source_unit: RuntimeReply(  # type: ignore[method-assign]
        RuntimeReplyKind.MAIN_COMPLETED,
        8,
        OperationState.COMPLETED,
        messages=("первая строка", "вторая строка"),
    )

    displayed = magics.bsl("", 'Сообщить("первая строка");')

    assert displayed is None
    assert capsys.readouterr().out == "первая строка\nвторая строка\n"


def test_presentation_mode_suppresses_empty_success_status() -> None:
    shell = FakeShell()
    runtime = FakeRuntime()
    install_runtime(shell, runtime, display=NotebookDisplayConfig.presentation())
    runtime.execute_bsl = lambda _source, *, source_unit: RuntimeReply(  # type: ignore[method-assign]
        RuntimeReplyKind.MAIN_COMPLETED, 8, OperationState.COMPLETED,
    )

    assert OnecRuntimeMagics(shell).bsl("", "Запрос = Новый Запрос;") is None  # type: ignore[arg-type]


def test_presentation_mode_suppresses_worker_loaded_status() -> None:
    shell = FakeShell()
    runtime = FakeRuntime()
    install_runtime(shell, runtime, display=NotebookDisplayConfig.presentation())
    runtime.execute_bsl = lambda _source, *, source_unit: RuntimeReply(  # type: ignore[method-assign]
        RuntimeReplyKind.WORKER_LOADED, 8, OperationState.COMPLETED,
        result="worker-generation",
    )

    assert OnecRuntimeMagics(shell).bsl(  # type: ignore[arg-type]
        "", "Процедура Показать()\nКонецПроцедуры"
    ) is None


@pytest.mark.parametrize(
    ("stage", "platform_text", "expected"),
    [
        (
            DiagnosticStage.EXECUTION,
            "{<Неизвестный модуль>(1,1)}: Ошибка при вызове метода контекста "
            "(Записать)\nпо причине:\nНе заполнено обязательное поле Наименование",
            "Не заполнено обязательное поле Наименование",
        ),
        (
            DiagnosticStage.COMPILATION,
            "{<Неизвестный модуль>(1,1)}: Ошибка инициализации модуля"
            "\nпо причине:\nПроцедура или функция с указанным именем не определена "
            "(ОтменитьТрранзакцию)\n[ОшибкаКомпиляцииВстроенногоЯзыка]",
            "ОтменитьТрранзакцию",
        ),
    ],
)
def test_presentation_mode_shows_nested_platform_cause(stage, platform_text, expected):
    source = "Элемент.Записать();"
    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL, "nested-error", 1, source_sha256(source),
    )
    diagnostic = remap_platform_diagnostic(
        parse_platform_diagnostic(platform_text), mapped_visible_source(source, unit),
        stage=stage, visible_source_context=VisibleSourceContext({unit: source}),
    )
    reply = RuntimeReply(
        RuntimeReplyKind.MAIN_COMPLETED, 9, OperationState.FAILED,
        succeeded=False, error="BSL execution failed", diagnostic=diagnostic,
    )

    displayed = _display_reply(
        reply, NotebookDisplayConfig.presentation(),
        visible_source=source, source_unit=unit,
    )

    assert expected in displayed.text
    assert "строка 1" in displayed.text


def test_presentation_mode_shows_write_cause_when_platform_has_no_cell_location():
    source = "Элемент.Записать();"
    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL, "unlocated-write", 1, source_sha256(source),
    )
    diagnostic = remap_platform_diagnostic(
        parse_platform_diagnostic(
            "Ошибка при вызове метода контекста (Записать)\n"
            "по причине:\nНе заполнено обязательное поле Наименование"
        ),
        mapped_visible_source(source, unit),
        stage=DiagnosticStage.EXECUTION,
        visible_source_context=VisibleSourceContext({unit: source}),
    )
    provenance = OperationExecutionProvenance(
        visible_source_sha256=unit.source_sha256,
        executed_source_sha256=diagnostic.execution_artifact_sha256,
        source_map_sha256=diagnostic.source_map_sha256,
        mode="main", worker_generation=None, worker_manifest_sha256=None,
    )
    reply = RuntimeReply(
        RuntimeReplyKind.MAIN_COMPLETED, 9, OperationState.FAILED,
        succeeded=False, error="BSL execution failed", diagnostic=diagnostic,
    )

    displayed = _display_reply(
        reply, NotebookDisplayConfig.presentation(),
        visible_source=source, source_unit=unit,
        execution_provenance=provenance,
    )

    assert "Не заполнено обязательное поле Наименование" in displayed.text
    assert "строка" not in displayed.text


def test_diagnostic_mode_keeps_visible_structured_json() -> None:
    shell = FakeShell()
    runtime = FakeRuntime()
    install_runtime(shell, runtime, display=NotebookDisplayConfig.diagnostic())
    magics = OnecRuntimeMagics(shell)  # type: ignore[arg-type]

    displayed = magics.bsl("", "ГДФЛ = 1;")

    assert displayed is not None
    bundle = displayed._repr_mimebundle_()
    assert bundle["application/json"] == displayed.payload
    assert bundle[MACHINE_MIME_TYPE] == displayed.payload


def test_jupyter_presentation_shows_visible_location_without_raw_platform_text() -> None:
    """Break caught: normalized failures copy the raw RuntimeReply error."""
    reply, source = _failed_reply_with_exact_diagnostic()

    displayed = _display_reply(
        reply,
        NotebookDisplayConfig.presentation(),
        visible_source=source,
        source_unit=reply.diagnostic.source_unit,
    )

    assert "строка 3" in displayed.text
    assert displayed.payload["diagnostic"]["mapping_confidence"] == "exact"
    assert displayed.payload["diagnostic"]["excerpt"] is None
    assert "runtime_summary" not in displayed.payload["diagnostic"]
    assert "excerpt=" not in displayed.text
    assert displayed.payload["source_unit"] == {
        "kind": "notebook_cell",
        "unit_id": "jupyter-test-session:execution:7",
        "revision": 7,
        "source_sha256": source_sha256(source),
    }
    assert "source_unit" not in displayed.payload["diagnostic"]
    encoded = json.dumps(displayed.payload, ensure_ascii=False)
    assert "platform_diagnostic" not in encoded
    assert "RAW platform fallback" not in encoded
    assert "private-connection" not in encoded
    assert "error" not in displayed.payload


def test_jupyter_public_identity_is_only_cell_revision_and_literal_source_hash() -> None:
    """Break caught: Jupyter publishes kernel identity or business source text."""
    source = (
        'СекретныйРасчет = "😀";\r\n'
        "Промежуточный = 1;\r\n"
        "Ошибка();"
    )
    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL,
        "cell-visible-7",
        7,
        "931d51523350a90ed7a0e43a78ce540420f646e33d3cade8cf54dbea2fabb703",
    )
    diagnostic = _exact_diagnostic(source, unit)
    reply = RuntimeReply(
        RuntimeReplyKind.MAIN_COMPLETED,
        12,
        OperationState.FAILED,
        error=(
            "kernel_id=kernel-secret-42 process_id=9182 "
            "Authorization: Bearer private-kernel-token"
        ),
        succeeded=False,
        diagnostic=diagnostic,
    )

    displayed = _display_reply(
        reply,
        NotebookDisplayConfig.presentation(),
        visible_source=source,
        source_unit=unit,
    )

    assert displayed.payload["source_unit"] == {
        "kind": "notebook_cell",
        "unit_id": "cell-visible-7",
        "revision": 7,
        "source_sha256": (
            "931d51523350a90ed7a0e43a78ce540420f646e33d3cade8cf54dbea2fabb703"
        ),
    }
    assert displayed.payload["diagnostic"]["visible_location"] == {
        "line": 3,
        "column": 1,
        "span": {"start": 44, "end": 45},
    }
    encoded = json.dumps(displayed.payload, ensure_ascii=False)
    for forbidden in (
        source,
        "СекретныйРасчет",
        "kernel-secret-42",
        "9182",
        "private-kernel-token",
        "private-connection",
    ):
        assert forbidden not in encoded


def test_jupyter_diagnostic_mode_includes_only_bounded_redacted_expert_details() -> None:
    """Break caught: diagnostic mode has no bounded expert projection."""
    reply, source = _failed_reply_with_exact_diagnostic()

    displayed = _display_reply(
        reply,
        NotebookDisplayConfig.diagnostic(),
        visible_source=source,
        source_unit=reply.diagnostic.source_unit,
    )

    details = displayed.payload["diagnostic_details"]
    assert displayed.payload["diagnostic"]["excerpt"] is None
    assert "runtime_summary" not in displayed.payload["diagnostic"]
    assert details["excerpt"] == "О"
    assert details["runtime_summary"] == "BSL execution failed"
    assert details["lowered_location"]["line"] == 5
    assert len(details["platform_diagnostic"]) <= 4096
    assert details["platform_diagnostic_redacted"] is True
    assert details["worker_generation"] is None
    assert details["worker_manifest_sha256"] is None
    encoded = json.dumps(details, ensure_ascii=False)
    assert "9182" not in encoded
    assert "private-connection" not in encoded
    assert "generated one" not in encoded
    assert source not in encoded


def test_jupyter_diagnostic_mode_binds_exact_worker_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: diagnostic mode drops or guesses Worker provenance."""

    class ProvenanceRuntime(FakeRuntime):
        def execute_bsl(
            self,
            source: str,
            *,
            source_unit: SourceUnitRef,
            on_execution_provenance=None,
        ) -> RuntimeReply:  # type: ignore[no-untyped-def]
            self.sources.append(source)
            self.source_units.append(source_unit)
            diagnostic = _exact_diagnostic(source, source_unit)
            provenance = OperationExecutionProvenance(
                    visible_source_sha256=source_unit.source_sha256,
                    executed_source_sha256=diagnostic.execution_artifact_sha256,
                    source_map_sha256=diagnostic.source_map_sha256,
                    mode="main",
                    worker_generation=17,
                    worker_manifest_sha256="f" * 64,
            )
            if on_execution_provenance is not None:
                on_execution_provenance(provenance)
            return RuntimeReply(
                RuntimeReplyKind.MAIN_COMPLETED,
                10,
                OperationState.FAILED,
                error="raw fallback",
                succeeded=False,
                diagnostic=diagnostic,
            )

    shell = FakeShell()
    runtime = ProvenanceRuntime()
    install_runtime(shell, runtime, display=NotebookDisplayConfig.diagnostic())
    magics = OnecRuntimeMagics(shell)  # type: ignore[arg-type]
    source = 'Первая = "😀";\r\nВторая = 2;\r\nОшибка();'

    published = []
    monkeypatch.setattr("onec_runtime_jupyter.extension.display", published.append)
    with pytest.raises(BslCellError):
        magics.bsl("", source)

    assert len(published) == 1
    displayed = published[0]
    details = displayed.payload["diagnostic_details"]
    assert details["worker_generation"] == 17
    assert details["worker_manifest_sha256"] == "f" * 64


def test_public_and_expert_diagnostic_renderers_have_exact_allowlists_and_fail_closed() -> None:
    """Break caught: generic dataclass serialization widens public/expert wires."""
    reply, _ = _failed_reply_with_exact_diagnostic()
    diagnostic = reply.diagnostic
    assert diagnostic is not None

    public = privacy.diagnostic_to_public_wire(
        replace(diagnostic, runtime_summary="pid=9182 token=attacker-authored")
    )
    expert = privacy.diagnostic_to_expert_wire(diagnostic)

    assert set(public) == {
        "diagnostic_id",
        "runtime_summary",
        "stage",
        "mapping_confidence",
        "visible_location",
        "related_visible_span",
        "excerpt",
        "synthetic_region",
    }
    assert set(expert) == set(public) | {
        "lowered_location",
        "platform_diagnostic",
        "platform_diagnostic_sha256",
        "platform_diagnostic_truncated",
        "platform_diagnostic_redacted",
        "execution_artifact_sha256",
        "source_map_sha256",
        "worker_generation",
        "worker_manifest_sha256",
    }
    assert public["runtime_summary"] == "BSL execution failed"
    assert "platform_diagnostic" not in json.dumps(public, ensure_ascii=False)
    assert "9182" not in json.dumps(expert, ensure_ascii=False)
    assert privacy.diagnostic_to_public_wire(
        replace(diagnostic, diagnostic_id="not-a-sha256")
    ) == {}
    assert privacy.diagnostic_to_expert_wire(
        replace(diagnostic, diagnostic_id="not-a-sha256")
    ) == {}


@pytest.mark.parametrize(
    ("private_text", "secrets"),
    (
        ('{"token":"private-json","pid":9182}', ("private-json", "9182")),
        ("token private-space password hunter2", ("private-space", "hunter2")),
        ("process_id=44321 credentials: top-secret", ("44321", "top-secret")),
        (
            "rdbgSessionId=private-session rdbgConnectionToken:private-connect",
            ("private-session", "private-connect"),
        ),
        (
            "Authorization: Bearer private-bearer apiKey=private-api",
            ("private-bearer", "private-api"),
        ),
        (
            '{"client_secret":"private-client","rdbgDebuggeeId":"debuggee-42"}',
            ("private-client", "debuggee-42"),
        ),
        (
            "dbPassword = private-db credential_id: credential-42",
            ("private-db", "credential-42"),
        ),
        (
            "authorizationHeader: private-header",
            ("private-header",),
        ),
    ),
)
def test_expert_renderer_conservatively_redacts_json_prose_and_camelcase_identities(
    private_text: str,
    secrets: tuple[str, ...],
) -> None:
    """Break caught: common runtime identity spellings bypass expert redaction."""
    source = "Ошибка();"
    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL,
        "redaction-session:execution:1",
        1,
        source_sha256(source),
    )
    diagnostic = remap_platform_diagnostic(
        parse_platform_diagnostic(
            f"{{<Неизвестный модуль>(1,1)}}: {private_text}"
        ),
        mapped_visible_source(source, unit),
        stage=DiagnosticStage.EXECUTION,
        visible_source_context=VisibleSourceContext({unit: source}),
    )

    expert = privacy.diagnostic_to_expert_wire(diagnostic)
    encoded = json.dumps(expert, ensure_ascii=False)

    assert expert["platform_diagnostic_redacted"] is True
    assert len(expert["platform_diagnostic"]) <= 4096
    assert all(secret not in encoded for secret in secrets)


@pytest.mark.parametrize(
    ("platform_text", "secret"),
    (
        ('{"rdbg_session":"private-session"}', "private-session"),
        ("rdbg_connection=private-connection", "private-connection"),
        ("rdbg_client: private-client", "private-client"),
        ("rdbg_server private-server", "private-server"),
        ('{"rdbg_process":"private-process"}', "private-process"),
        ("rdbg_subject=private-subject", "private-subject"),
        ("rdbg_target:private-target", "private-target"),
        ("rdbg_object private-object", "private-object"),
        ('{"rdbg_property":"private-property"}', "private-property"),
        ("rdbg_seance=private-seance", "private-seance"),
        ('{"rdbgSession":"camel-session"}', "camel-session"),
        ("rdbgConnection=camel-connection", "camel-connection"),
        ("rdbgClient: camel-client", "camel-client"),
        ("rdbgServer camel-server", "camel-server"),
        ('{"rdbgProcess":"camel-process"}', "camel-process"),
        ("rdbgSubject=camel-subject", "camel-subject"),
        ("rdbgTarget:camel-target", "camel-target"),
        ("rdbgObject camel-object", "camel-object"),
        ('{"rdbgProperty":"camel-property"}', "camel-property"),
        ("rdbgSeance=camel-seance", "camel-seance"),
        ("rdbg.session=dot-session", "dot-session"),
        ('{"rdbg-connection":"hyphen-connection"}', "hyphen-connection"),
    ),
)
def test_platform_renderer_redacts_suffixless_rdbg_identity_assignments(
    platform_text: str,
    secret: str,
) -> None:
    """Break caught: suffixless RDBG identity keys bypass redaction."""
    bounded, truncated, redacted = privacy.bounded_platform_diagnostic(
        platform_text,
        truncated=False,
        redacted=False,
    )

    assert bounded is not None
    assert secret not in bounded
    assert "<redacted>" in bounded
    assert len(bounded) <= 4096
    assert truncated is False
    assert redacted is True


def test_platform_renderer_preserves_non_assignment_rdbg_diagnostic_prose() -> None:
    """Break caught: mentioning RDBG becomes enough to erase safe prose."""
    prose = "RDBG session connection failed while inspecting the target object"

    assert privacy.bounded_platform_diagnostic(
        prose,
        truncated=False,
        redacted=False,
    ) == (prose, False, False)


def test_platform_diagnostic_markers_distinguish_redaction_from_truncation() -> None:
    """Break caught: pure truncation is mislabeled as identity redaction."""
    pure_truncation = privacy.bounded_platform_diagnostic(
        "x" * 4100,
        truncated=False,
        redacted=False,
    )
    pure_redaction = privacy.bounded_platform_diagnostic(
        "token private-value",
        truncated=False,
        redacted=False,
    )
    expansion_then_cut = privacy.bounded_platform_diagnostic(
        "x" * 4090 + " pid=1",
        truncated=False,
        redacted=False,
    )

    assert pure_truncation == ("x" * 4096, True, False)
    assert pure_redaction[1:] == (False, True)
    assert "private-value" not in pure_redaction[0]
    assert len(expansion_then_cut[0]) == 4096
    assert expansion_then_cut[1:] == (True, True)


def test_jupyter_source_units_are_session_scoped_monotonic_and_exact() -> None:
    """Break caught: the adapter executes source without an exact cell identity."""
    shell = FakeShell()
    shell.kernel = SimpleNamespace(
        session=SimpleNamespace(session="raw-kernel-connection-token")
    )
    runtime = FakeRuntime()
    install_runtime(shell, runtime)
    magics = OnecRuntimeMagics(shell)  # type: ignore[arg-type]
    first_source = 'Значение = "😀";\r\nРезультат = Значение;'
    second_source = "Результат = 2;\n"

    magics.bsl("", first_source)
    install_runtime(shell, runtime)
    magics.bsl("", second_source)

    first, second = runtime.source_units
    first_session = first.unit_id.rsplit(":execution:", 1)[0]
    second_session = second.unit_id.rsplit(":execution:", 1)[0]
    assert first_session == second_session
    assert first.unit_id.endswith(":execution:1")
    assert second.unit_id.endswith(":execution:2")
    assert (first.revision, second.revision) == (1, 2)
    assert first.source_sha256 == source_sha256(first_source)
    assert second.source_sha256 == source_sha256(second_source)
    assert "raw-kernel-connection-token" not in first.unit_id
    assert runtime.sources == [first_source, second_source]


def test_jupyter_source_state_resists_namespace_substitution_and_allocates_atomically() -> None:
    """Break caught: mutable user namespace state can reset or replace cell identity."""
    shell = FakeShell()
    runtime = FakeRuntime()
    install_runtime(shell, runtime)
    magics = OnecRuntimeMagics(shell)  # type: ignore[arg-type]

    magics.bsl("", "Результат = 1;")
    namespace_state = shell.user_ns.pop(
        "_onec_runtime_source_session",
        SimpleNamespace(),
    )
    namespace_state.session_id = "raw-connection-token"  # type: ignore[attr-defined]
    namespace_state.execution_counter = -100  # type: ignore[attr-defined]
    shell.user_ns["_onec_runtime_source_session"] = namespace_state
    install_runtime(shell, runtime)
    magics.bsl("", "Результат = 2;")

    concurrent_sources = [f"Результат = {index};" for index in range(3, 35)]
    with ThreadPoolExecutor(max_workers=8) as executor:
        tuple(executor.map(lambda source: magics.bsl("", source), concurrent_sources))

    assert "_onec_runtime_source_session" not in shell.user_ns
    assert [unit.revision for unit in runtime.source_units[:2]] == [1, 2]
    assert sorted(unit.revision for unit in runtime.source_units) == list(range(1, 35))
    assert len({unit.unit_id for unit in runtime.source_units}) == 34
    assert all("raw-connection-token" not in unit.unit_id for unit in runtime.source_units)


def test_jupyter_rejects_runtime_without_exact_source_unit_protocol() -> None:
    """Break caught: a legacy source-only runtime receives an unfenced execution."""
    shell = FakeShell()
    runtime = FakeRuntime()

    def legacy_execute(source: str) -> RuntimeReply:
        del source
        return RuntimeReply(
            RuntimeReplyKind.MAIN_COMPLETED,
            1,
            OperationState.COMPLETED,
        )

    runtime.execute_bsl = legacy_execute  # type: ignore[method-assign]
    install_runtime(shell, runtime)
    magics = OnecRuntimeMagics(shell)  # type: ignore[arg-type]

    with pytest.raises(TypeError):
        magics.bsl("", "Результат = 1;")


def test_resume_magic_parses_explicit_dirty_roots() -> None:
    shell = FakeShell()
    runtime = FakeRuntime()
    install_runtime(shell, runtime)
    magics = OnecRuntimeMagics(shell)  # type: ignore[arg-type]

    displayed = magics.bsl_resume("Скаляр Результат")

    assert runtime.dirty_roots == ("Скаляр", "Результат")
    assert displayed.payload["kind"] == "main_completed"


def test_status_magic_is_read_only() -> None:
    shell = FakeShell()
    runtime = FakeRuntime()
    install_runtime(shell, runtime)
    magics = OnecRuntimeMagics(shell)  # type: ignore[arg-type]

    displayed = magics.bsl_status("")

    assert displayed.payload == {
        "state": "captured",
        "runtime_generation": 1,
        "operation_id": 7,
        "worker_generation": None,
    }


def test_missing_runtime_fails_closed() -> None:
    shell = FakeShell()
    magics = OnecRuntimeMagics(shell)  # type: ignore[arg-type]

    with pytest.raises(UsageError, match="install_runtime"):
        magics.bsl("", "Значение = 1;")


def test_extension_registers_magics() -> None:
    shell = FakeShell()

    load_ipython_extension(shell)  # type: ignore[arg-type]

    assert isinstance(shell.registered, OnecRuntimeMagics)


class _FakeTypeFormatter:
    def __init__(self) -> None:
        self.type_printers: dict[type[object], object] = {}
        self.deferred_printers: dict[tuple[str, str], object] = {}

    def for_type(self, value_type: type[object], formatter: object) -> object | None:
        previous = self.type_printers.get(value_type)
        key = (value_type.__module__, value_type.__name__)
        if previous is None and key in self.deferred_printers:
            previous = self.deferred_printers.pop(key)
            self.type_printers[value_type] = previous
        self.type_printers[value_type] = formatter
        return previous

    def pop(self, value_type: type[object]) -> object:
        if value_type in self.type_printers:
            return self.type_printers.pop(value_type)
        return self.deferred_printers.pop(
            (value_type.__module__, value_type.__name__)
        )

    def for_type_by_name(
        self,
        module: str,
        name: str,
        formatter: object,
    ) -> object | None:
        previous = self.deferred_printers.get((module, name))
        self.deferred_printers[(module, name)] = formatter
        return previous


class _FakeDisplayFormatter:
    def __init__(self) -> None:
        self.formatters = {
            "text/plain": _FakeTypeFormatter(),
            "text/html": _FakeTypeFormatter(),
        }


def test_extension_registers_capture_snapshot_formatters_without_global_alias() -> None:
    shell = FakeShell()
    shell.display_formatter = _FakeDisplayFormatter()  # type: ignore[attr-defined]
    plain = shell.display_formatter.formatters["text/plain"]  # type: ignore[attr-defined]
    html = shell.display_formatter.formatters["text/html"]  # type: ignore[attr-defined]
    deferred_key = (StackPage.__module__, StackPage.__name__)
    previous_plain = object()
    previous_html = object()
    plain.for_type_by_name(*deferred_key, previous_plain)
    html.for_type_by_name(*deferred_key, previous_html)

    load_ipython_extension(shell)  # type: ignore[arg-type]

    expected = {
        CaptureStatus, StackPage, DebugFrame, ValuePage, ValueNode, DeniedValueNode,
    }
    assert expected <= plain.type_printers.keys()
    assert expected <= html.type_printers.keys()
    assert deferred_key not in plain.deferred_printers
    assert deferred_key not in html.deferred_printers
    assert "capture" not in shell.user_ns

    status = CaptureStatus(7, 2, 1, CapturePhase.PAUSED)

    class Printer:
        def __init__(self) -> None:
            self.value = ""

        def text(self, value: str) -> None:
            self.value += value

    printer = Printer()
    plain.type_printers[CaptureStatus](status, printer, False)
    rich = html.type_printers[CaptureStatus](status)
    assert printer.value.startswith("CAPTURE: paused")
    assert rich.startswith('<section class="onec-capture onec-capture-status">')

    unload_ipython_extension(shell)  # type: ignore[arg-type]

    assert not (expected & plain.type_printers.keys())
    assert not (expected & html.type_printers.keys())
    assert plain.deferred_printers[deferred_key] is previous_plain
    assert html.deferred_printers[deferred_key] is previous_html
