from __future__ import annotations

from datetime import datetime, timezone

import pytest
from IPython.core.formatters import HTMLFormatter, PlainTextFormatter

from onec_runtime.bsl import SourceSpan
from onec_runtime.bsl.module_syntax import MethodSyntaxInfo
from onec_runtime.capture_evaluation import (
    CaptureEvaluationKind,
    CaptureEvaluationTiming,
    CaptureFailureDiagnostic,
    CapturePhase,
    CaptureStatus,
)
from onec_runtime.capture_inspection import (
    ConfigurationFrameResolver,
    DebugFrame,
    RuntimeFrameMarker,
    StackPage,
)
from onec_runtime.capture_values import (
    DeniedValueNode,
    SafeValuePath,
    UnavailableValueNode,
    ValueNode,
    ValuePathSegmentKind,
    ValueRoot,
    ValueRootKind,
    ValueShape,
    ValuePage,
)
from onec_runtime.rdbg.session import RdbgSession
from onec_runtime.runtime_api import PrototypeRuntimeApi
from onec_runtime.session import RuntimeSession
from onec_runtime_jupyter.capture_display import (
    CaptureSnapshotDisplay,
    install_capture_formatters,
    remove_capture_formatters,
    render_capture_html,
    render_capture_text,
)


def _context_path(name: str = "Структура") -> SafeValuePath:
    return SafeValuePath(ValueRoot(ValueRootKind.CONTEXT)).child(
        ValuePathSegmentKind.VARIABLE,
        name,
    )


def _method() -> MethodSyntaxInfo:
    return MethodSyntaxInfo(
        "Рассчитать",
        SourceSpan(0, 12),
        ("Сотрудник", "Дата"),
        180,
        204,
    )


def test_pending_status_has_safe_capabilities_and_actionable_wait_guidance() -> None:
    status = CaptureStatus(
        operation_id=17,
        capture_generation=4,
        stop_sequence=2,
        phase=CapturePhase.EVALUATING,
        pending_evaluation_id="eval-<guard>",
        evaluation_kind=CaptureEvaluationKind.MATERIALIZATION_HELPER,
        evaluation_timing=CaptureEvaluationTiming(
            evaluation_id="eval-<guard>",
            created_at_utc=datetime(2026, 9, 15, tzinfo=timezone.utc),
            elapsed_ms=1250,
            poll_count=3,
        ),
    )

    text = render_capture_text(status)
    html = render_capture_html(status)

    assert "CAPTURE: evaluating" in text
    assert "materialization_helper" in text
    assert "eval-<guard>" in text
    assert "can_inspect: no" in text
    assert "can_wait: yes" in text
    assert "runtime.current_capture().wait(timeout_s=10)" in text
    assert "eval-&lt;guard&gt;" in html
    assert "eval-<guard>" not in html
    assert "runtime.current_capture().wait(timeout_s=10)" in html
    assert "<script" not in html


def test_terminal_status_renders_only_bounded_failure_snapshot() -> None:
    status = CaptureStatus(
        operation_id=7,
        capture_generation=2,
        stop_sequence=5,
        phase=CapturePhase.RECOVERY_REQUIRED,
        last_evaluation_id="eval-7",
        failure=CaptureFailureDiagnostic(
            "workspace_restore_failed",
            "Не удалось восстановить <workspace>",
            "Перезапустите runtime & повторите операцию",
        ),
    )

    text = render_capture_text(status)
    html = render_capture_html(status)

    assert "workspace_restore_failed" in text
    assert "Не удалось восстановить <workspace>" in text
    assert "&lt;workspace&gt;" in html
    assert "runtime &amp; повторите" in html


def test_stack_page_renders_tree_collapsed_frames_method_state_and_cursor() -> None:
    page = StackPage(
        frames=(
            DebugFrame(
                native_level=2,
                visible_index=0,
                source="Документ.ПриемНаРаботу.МодульОбъекта",
                line=186,
                source_status="resolved",
                detail="method",
                method=_method(),
                method_status="resolved",
            ),
            RuntimeFrameMarker(3),
            DebugFrame(
                native_level=6,
                visible_index=1,
                source="ОбщийМодуль.ПлановыеНачисленияСотрудников",
                line=742,
                source_status="resolved",
                detail="method",
                method_status="timeout",
                method_reason="work_budget_exhausted",
            ),
        ),
        total=5,
        next_cursor=2,
        detail="method",
    )

    text = render_capture_text(page)
    html = render_capture_html(page)

    assert text.splitlines()[0] == "Стек вызовов (method)"
    assert "├─ #0 Документ.ПриемНаРаботу.МодульОбъекта:186 — Рассчитать(Сотрудник, Дата)" in text
    assert "├─ … скрыто 3 служебных кадров" in text
    assert (
        "└─ #1 ОбщийМодуль.ПлановыеНачисленияСотрудников:742 "
        "— timeout (work_budget_exhausted)"
    ) in text
    assert "Показано кадров: 2 из 5; next_cursor=2" in text
    assert '<li class="onec-capture-runtime-marker">' in html
    assert "work_budget_exhausted" in html
    assert "next_cursor=2" in html


def test_native_stack_page_is_visibly_distinct_from_default_stack() -> None:
    page = StackPage(
        (
            DebugFrame(
                native_level=0,
                source="PRIVATE_RUNTIME_SOURCE",
                line=None,
                runtime_kernel=True,
            ),
        ),
        total=1,
        next_cursor=None,
        native=True,
    )

    assert render_capture_text(page).startswith("Стек вызовов (native, line)")
    assert "native, line" in render_capture_html(page)


def test_stack_render_limit_counts_visible_frames_separately_from_markers() -> None:
    frames = tuple(
        item
        for index in range(100)
        for item in (
            RuntimeFrameMarker(index + 1),
            DebugFrame(
                native_level=index * 2 + 1,
                visible_index=index,
                source=f"ОбщийМодуль.Модуль{index}",
                line=index + 1,
                source_status="resolved",
            ),
        )
    ) + (
        RuntimeFrameMarker(101),
    )
    page = StackPage(frames, total=100, next_cursor=None)

    text = render_capture_text(page)
    html = render_capture_html(page)

    assert "скрыто 1 служебных кадров" in text
    assert "скрыто 101 служебных кадров" in text
    assert "#99 ОбщийМодуль.Модуль99:100" in text
    assert "Показано кадров: 100 из 100; next_cursor=none" in text
    assert "не показано" not in text
    assert "#99 ОбщийМодуль.Модуль99:100" in html
    assert "Показано кадров: 100 из 100; next_cursor=none" in html
    assert html.count('class="onec-capture-runtime-marker"') == 101
    assert "onec-capture-truncated" not in html


def test_single_frame_render_redacts_runtime_coordinates_and_escapes_source() -> None:
    runtime_frame = DebugFrame(
        native_level=8,
        source="PRIVATE_KERNEL_URL_<script>",
        line=999,
        runtime_kernel=True,
        method_reason="PRIVATE_GENERATION_HANDLE",
    )
    source_frame = DebugFrame(
        native_level=1,
        visible_index=0,
        source="ОбщийМодуль.<Опасный>",
        line=12,
        source_status="resolved",
    )

    runtime_text = render_capture_text(runtime_frame)
    runtime_html = render_capture_html(runtime_frame)
    source_html = render_capture_html(source_frame)

    assert runtime_text == "native #8 служебный кадр"
    for secret in ("PRIVATE_KERNEL_URL", "PRIVATE_GENERATION_HANDLE", "999"):
        assert secret not in runtime_text
        assert secret not in runtime_html
    assert "&lt;Опасный&gt;" in source_html
    assert "<Опасный>" not in source_html


def test_method_signature_caps_parameter_expansion_before_rendering() -> None:
    method = MethodSyntaxInfo(
        "БольшойМетод",
        SourceSpan(0, 12),
        tuple(f"Параметр{index}" for index in range(40)),
        1,
        2,
    )
    frame = DebugFrame(
        native_level=0,
        visible_index=0,
        source="ОбщийМодуль.Большой",
        line=1,
        source_status="resolved",
        detail="method",
        method=method,
        method_status="resolved",
    )

    text = render_capture_text(frame)

    assert "Параметр31" in text
    assert "Параметр32" not in text
    assert text.endswith("…)")


def test_value_page_renders_hierarchy_redaction_cycles_and_pagination() -> None:
    path = _context_path()
    page = ValuePage(
        items=(
            ValueNode(
                "Оклад",
                "Число",
                "150000",
                None,
                False,
                ValueShape.SCALAR,
                path.child(ValuePathSegmentKind.FIELD, "Оклад"),
            ),
            ValueNode(
                "Данные",
                "Структура",
                "2 элемента <unsafe>",
                2,
                True,
                ValueShape.STRUCTURE,
                path.child(ValuePathSegmentKind.FIELD, "Данные"),
                cycle=True,
            ),
            DeniedValueNode("СлужебноеЗначение"),
        ),
        total=9,
        next_cursor=3,
        path=path,
        view="structure_fields",
        start=0,
        stop=3,
    )

    text = render_capture_text(page)
    html = render_capture_html(page)

    assert "e1cRuntimeКонтекстОтладки.Структура.structure_fields [0:3]" in text
    assert "├─ Оклад: Число = 150000" in text
    assert "├─ Данные: Структура = 2 элемента <unsafe> [size=2] ▸ ↻" in text
    assert "└─ СлужебноеЗначение: <private runtime value>" in text
    assert "Показано значений: 3 из 9; next_cursor=3" in text
    for secret in ("PRIVATE_TYPE", "PRIVATE_PREVIEW", "999"):
        assert secret not in text
        assert secret not in html
    assert "2 элемента &lt;unsafe&gt;" in html
    assert "onec-capture-private" in html


def test_value_node_protocol_exposes_plain_and_html_representations() -> None:
    node = ValueNode(
        "Сотрудник",
        "СправочникСсылка.Сотрудники",
        "Иванов & Петров",
        None,
        False,
        ValueShape.SCALAR,
        _context_path("Сотрудник"),
    )
    displayed = CaptureSnapshotDisplay(node)

    assert repr(displayed) == render_capture_text(node)
    assert displayed._repr_html_() == render_capture_html(node)
    assert "Иванов &amp; Петров" in displayed._repr_html_()


def test_denied_value_node_renders_from_its_exact_closed_contract_only() -> None:
    node = DeniedValueNode("СлужебноеЗначение")

    text = render_capture_text(node)
    html = render_capture_html(node)

    assert text == "СлужебноеЗначение: <private runtime value>"
    assert "onec-capture-private" in html
    assert CaptureSnapshotDisplay(node)._repr_html_() == html


def test_mixed_value_page_and_unavailable_node_render_without_value_metadata() -> None:
    path = _context_path()
    page = ValuePage(
        (
            UnavailableValueNode("Неподдерживаемое"),
            DeniedValueNode("Закрытое"),
            ValueNode(
                "Доступное", "Число", "42", None, False,
                ValueShape.SCALAR,
                path.child(ValuePathSegmentKind.FIELD, "Доступное"),
            ),
        ),
        3, None, path, "structure_fields", 0, 3,
    )

    text = render_capture_text(page)
    html = render_capture_html(page)

    assert "Неподдерживаемое: <unavailable>" in text
    assert "Закрытое: <private runtime value>" in text
    assert "Доступное: Число = 42" in text
    assert "onec-capture-unavailable" in html
    assert "Неподдерживаемое: &lt;unavailable&gt;" in html
    assert "Неподдерживаемое" in render_capture_text(
        UnavailableValueNode("Неподдерживаемое")
    )
    assert "onec-capture-unavailable" in CaptureSnapshotDisplay(
        UnavailableValueNode("Неподдерживаемое")
    )._repr_html_()
    assert "type_name" not in html


def test_ipython_formatters_render_mixed_value_page_without_formatter_errors() -> None:
    plain = PlainTextFormatter()
    html = HTMLFormatter()

    class Shell:
        pass

    shell = Shell()
    shell.display_formatter = type(
        "DisplayFormatter", (),
        {"formatters": {"text/plain": plain, "text/html": html}},
    )()
    path = _context_path()
    page = ValuePage(
        (UnavailableValueNode("Неподдерживаемое"), DeniedValueNode("Закрытое")),
        2, None, path, "variables", 0, 2,
    )

    install_capture_formatters(shell)
    try:
        assert "Неподдерживаемое: <unavailable>" in plain(page)
        assert "onec-capture-unavailable" in html(page)
    finally:
        remove_capture_formatters(shell)


def test_rendering_saved_pages_twice_is_byte_identical_and_has_zero_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def contacted(name: str):
        def fail(*_args: object, **_kwargs: object) -> None:
            calls.append(name)
            raise AssertionError(f"rendering contacted {name}")

        return fail

    monkeypatch.setattr(
        PrototypeRuntimeApi,
        "_require_available",
        contacted("runtime"),
    )
    monkeypatch.setattr(RuntimeSession, "status", contacted("runtime_session"))
    monkeypatch.setattr(RdbgSession, "evaluate", contacted("rdbg"))
    monkeypatch.setattr(
        ConfigurationFrameResolver,
        "__call__",
        contacted("source_resolver"),
    )
    monkeypatch.setattr(
        "onec_runtime.capture_inspection.parse_full_ast_module",
        contacted("parser"),
    )

    class PoisonOwner:
        def __getattribute__(self, name: str) -> object:
            calls.append(f"owner.{name}")
            raise AssertionError("rendering touched a live value owner")

    node = ValueNode(
        "Данные",
        "Структура",
        "1 элемент",
        1,
        True,
        ValueShape.STRUCTURE,
        _context_path(),
        _owner=PoisonOwner(),  # type: ignore[arg-type]
    )
    page = ValuePage((node,), 1, None, _context_path(), "variables", 0, 1)
    display = CaptureSnapshotDisplay(page)
    stack = CaptureSnapshotDisplay(
        StackPage(
            (
                DebugFrame(
                    native_level=0,
                    visible_index=0,
                    source="ОбщийМодуль.Демо",
                    line=1,
                    source_status="resolved",
                    _enricher=PoisonOwner(),  # type: ignore[arg-type]
                ),
            ),
            1,
            None,
            _enricher=PoisonOwner(),  # type: ignore[arg-type]
        )
    )

    first = (repr(display), display._repr_html_())
    second = (repr(display), display._repr_html_())
    stack_first = (repr(stack), stack._repr_html_())
    stack_second = (repr(stack), stack._repr_html_())

    assert first == second
    assert stack_first == stack_second
    assert calls == []


def test_rendering_is_bounded_even_for_an_invalidly_oversized_saved_page() -> None:
    path = _context_path()
    nodes = tuple(
        ValueNode(
            f"Поле{index}",
            "Строка",
            "x" * 512,
            None,
            False,
            ValueShape.SCALAR,
            path.child(ValuePathSegmentKind.FIELD, f"Поле{index}"),
        )
        for index in range(105)
    )
    page = ValuePage(nodes, 105, None, path, "structure_fields", 0, 105)

    text = render_capture_text(page)
    html = render_capture_html(page)

    assert "… не показано 5 элементов" in text
    assert "… не показано 5 элементов" in html
    assert "Поле99" in text
    assert "Поле100" not in text
    assert len(text) < 100_000
    assert len(html) < 500_000


@pytest.mark.parametrize("value", [object(), "not a capture snapshot", None])
def test_capture_snapshot_display_rejects_unknown_or_live_objects(value: object) -> None:
    with pytest.raises(TypeError, match="immutable capture snapshot"):
        CaptureSnapshotDisplay(value)


def test_real_ipython_formatters_restore_direct_and_deferred_registrations() -> None:
    plain = PlainTextFormatter()
    html = HTMLFormatter()

    class Shell:
        pass

    shell = Shell()
    shell.display_formatter = type(
        "DisplayFormatter",
        (),
        {"formatters": {"text/plain": plain, "text/html": html}},
    )()

    def old_plain_direct(*_args: object) -> str:
        return "old plain direct"

    def old_html_direct(*_args: object) -> str:
        return "old html direct"

    def old_plain_deferred(*_args: object) -> str:
        return "old plain deferred"

    def old_html_deferred(*_args: object) -> str:
        return "old html deferred"

    def newer_plain_direct(*_args: object) -> str:
        return "newer plain direct"

    def newer_html_direct(*_args: object) -> str:
        return "newer html direct"

    def newer_plain_deferred(*_args: object) -> str:
        return "newer plain deferred"

    def newer_html_deferred(*_args: object) -> str:
        return "newer html deferred"

    direct_previous = {
        plain: old_plain_direct,
        html: old_html_direct,
    }
    deferred_previous = {
        plain: old_plain_deferred,
        html: old_html_deferred,
    }
    newer_direct = {
        plain: newer_plain_direct,
        html: newer_html_direct,
    }
    newer_deferred = {
        plain: newer_plain_deferred,
        html: newer_html_deferred,
    }
    deferred_stack_key = (StackPage.__module__, StackPage.__name__)
    deferred_value_key = (ValuePage.__module__, ValuePage.__name__)
    for formatter in (plain, html):
        formatter.for_type(CaptureStatus, direct_previous[formatter])
        formatter.for_type_by_name(*deferred_stack_key, deferred_previous[formatter])

    install_capture_formatters(shell)

    for formatter in (plain, html):
        assert formatter.type_printers[CaptureStatus] is not direct_previous[formatter]
        assert formatter.type_printers[StackPage] is not deferred_previous[formatter]
        assert deferred_stack_key not in formatter.deferred_printers
        formatter.for_type(DebugFrame, newer_direct[formatter])
        formatter.for_type_by_name(*deferred_value_key, newer_deferred[formatter])

    remove_capture_formatters(shell)

    for formatter in (plain, html):
        assert formatter.type_printers[CaptureStatus] is direct_previous[formatter]
        assert StackPage not in formatter.type_printers
        assert formatter.deferred_printers[deferred_stack_key] is deferred_previous[formatter]
        assert formatter.type_printers[DebugFrame] is newer_direct[formatter]
        assert ValuePage not in formatter.type_printers
        assert formatter.deferred_printers[deferred_value_key] is newer_deferred[formatter]
