"""Side-effect-free notebook rendering for saved CAPTURE inspection data."""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from threading import Lock
from typing import TypeAlias
from weakref import WeakKeyDictionary

from onec_runtime.bsl.module_syntax import MethodSyntaxInfo
from onec_runtime.capture_evaluation import CaptureStatus
from onec_runtime.capture_inspection import DebugFrame, RuntimeFrameMarker, StackPage
from onec_runtime.capture_values import (
    SafeValuePath,
    ValueNode,
    ValuePage,
    ValuePathSegmentKind,
    ValueRootKind,
)


MAX_RENDER_ITEMS = 100
MAX_STACK_MARKERS = MAX_RENDER_ITEMS + 1
MAX_METHOD_PARAMETERS = 32
MAX_LABEL_CHARS = 512
MAX_PATH_CHARS = 1_024

CaptureSnapshot: TypeAlias = CaptureStatus | StackPage | DebugFrame | ValueNode | ValuePage
_SNAPSHOT_TYPES = (CaptureStatus, StackPage, DebugFrame, ValueNode, ValuePage)


def _clean(value: str, limit: int = MAX_LABEL_CHARS) -> str:
    """Keep one bounded display line without terminal or HTML control text."""

    sample = value[: limit * 4 + 1]
    cleaned = "".join(character if character.isprintable() else " " for character in sample)
    return " ".join(cleaned.split())[:limit]


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def _capture_snapshot(value: object) -> CaptureSnapshot:
    # Exact types keep formatter dispatch from invoking overridden attributes on
    # an untrusted subclass.  The core constructors own snapshot validation.
    if type(value) not in _SNAPSHOT_TYPES:
        raise TypeError("value must be an immutable capture snapshot")
    return value  # type: ignore[return-value]


def _status_lines(status: CaptureStatus) -> list[str]:
    lines = [
        f"CAPTURE: {status.phase.value}",
        (
            f"Операция: {status.operation_id}; generation={status.capture_generation}; "
            f"stop_sequence={status.stop_sequence}"
        ),
        (
            f"Возможности: can_inspect: {_yes_no(status.can_inspect)}; "
            f"can_resume_capture: {_yes_no(status.can_resume_capture)}; "
            f"can_wait: {_yes_no(status.can_wait)}"
        ),
    ]
    if status.pending_evaluation_id is not None:
        kind = (
            "unknown"
            if status.evaluation_kind is None
            else status.evaluation_kind.value
        )
        lines.extend(
            (
                (
                    f"Ожидается evaluation: {_clean(status.pending_evaluation_id)} "
                    f"({kind})"
                ),
                "Продолжить ожидание: runtime.current_capture().wait(timeout_s=10)",
            )
        )
    elif status.last_evaluation_id is not None:
        lines.append(f"Последний evaluation: {_clean(status.last_evaluation_id)}")
    if status.evaluation_timing is not None:
        timing = status.evaluation_timing
        elapsed = "unknown" if timing.elapsed_ms is None else str(timing.elapsed_ms)
        lines.append(
            f"Время: elapsed_ms={elapsed}; polls={timing.poll_count}; "
            f"remote_steps={timing.remote_step_count}"
        )
    if status.failure is not None:
        lines.extend(
            (
                f"Ошибка: {_clean(status.failure.code, 256)}",
                f"Сообщение: {_clean(status.failure.message, 1_024)}",
                f"Действие: {_clean(status.failure.recommended_action, 1_024)}",
            )
        )
    return lines


def _frame_text(frame: DebugFrame) -> str:
    label = (
        f"#{frame.visible_index}"
        if frame.visible_index is not None
        else f"native #{frame.native_level}"
    )
    if frame.runtime_kernel:
        return f"{label} служебный кадр"
    line = "?" if frame.line is None else str(frame.line)
    text = f"{label} {_clean(frame.source)}:{line}"
    if type(frame.method) is MethodSyntaxInfo:
        method = frame.method
        parameters = [_clean(value, 256) for value in method.parameters[:MAX_METHOD_PARAMETERS]]
        if len(method.parameters) > MAX_METHOD_PARAMETERS:
            parameters.append("…")
        signature = f"{_clean(method.name)}({', '.join(parameters)})"
        text += " — " + _clean(signature)
    elif frame.method_status not in {"not_requested", "resolved"}:
        text += f" — {_clean(frame.method_status, 128)}"
        if frame.method_reason:
            text += f" ({_clean(frame.method_reason, 256)})"
    elif frame.source_status == "unavailable":
        text += " — исходный файл не найден"
    return text


def _bounded_stack_entries(
    frames: tuple[DebugFrame | RuntimeFrameMarker, ...],
) -> tuple[tuple[DebugFrame | RuntimeFrameMarker, ...], int]:
    entries: list[DebugFrame | RuntimeFrameMarker] = []
    visible_frames = 0
    markers = 0
    consumed = 0
    for frame in frames:
        if type(frame) is RuntimeFrameMarker:
            if markers >= MAX_STACK_MARKERS:
                break
            markers += 1
        else:
            if visible_frames >= MAX_RENDER_ITEMS:
                break
            visible_frames += 1
        entries.append(frame)
        consumed += 1
    return tuple(entries), len(frames) - consumed


def _stack_lines(page: StackPage) -> list[str]:
    entries, omitted = _bounded_stack_entries(page.frames)
    detail = _clean(page.detail, 64)
    mode = f"native, {detail}" if page.native else detail
    lines = [f"Стек вызовов ({mode})"]
    for index, frame in enumerate(entries):
        connector = "└─" if index == len(entries) - 1 else "├─"
        if type(frame) is RuntimeFrameMarker:
            rendered = f"… скрыто {frame.count} служебных кадров"
        else:
            rendered = _frame_text(frame)
        lines.append(f"{connector} {rendered}")
    if omitted:
        lines.append(f"… не показано {omitted} элементов стека")
    shown = sum(type(frame) is DebugFrame for frame in entries)
    cursor = "none" if page.next_cursor is None else str(page.next_cursor)
    lines.append(f"Показано кадров: {shown} из {page.total}; next_cursor={cursor}")
    return lines


def _path_text(path: SafeValuePath) -> str:
    root = path.root
    if root.kind is ValueRootKind.CONTEXT:
        text = "КонтекстОтладки"
    else:
        text = f"frame[{root.native_level}]"
    for segment in path.segments:
        if segment.kind in {
            ValuePathSegmentKind.VARIABLE,
            ValuePathSegmentKind.FIELD,
            ValuePathSegmentKind.COLUMN,
        }:
            text += "." + _clean(str(segment.key), 256)
        else:
            text += f"[{segment.key}]"
        if len(text) >= MAX_PATH_CHARS:
            return text[: MAX_PATH_CHARS - 1] + "…"
    return text


def _node_text(node: ValueNode) -> str:
    name = _clean(str(node.name), 256)
    if node.private:
        # Privacy-denied nodes intentionally ignore every presentation field
        # except the containing variable name.
        return f"{name}: <private runtime value>"
    type_name = "unknown" if node.type_name is None else _clean(node.type_name, 256)
    text = f"{name}: {type_name} = {_clean(node.preview)}"
    if node.size is not None:
        text += f" [size={node.size}]"
    if node.expandable:
        text += " ▸"
    if node.cycle:
        text += " ↻"
    return text


def _value_page_lines(page: ValuePage) -> list[str]:
    entries = page.items[:MAX_RENDER_ITEMS]
    lines = [
        f"{_path_text(page.path)}.{_clean(page.view, 64)} [{page.start}:{page.stop}]"
    ]
    for index, node in enumerate(entries):
        connector = "└─" if index == len(entries) - 1 else "├─"
        lines.append(f"{connector} {_node_text(node)}")
    omitted = len(page.items) - len(entries)
    if omitted:
        lines.append(f"… не показано {omitted} элементов")
    cursor = "none" if page.next_cursor is None else str(page.next_cursor)
    lines.append(
        f"Показано значений: {len(entries)} из {page.total}; next_cursor={cursor}"
    )
    return lines


def render_capture_text(value: CaptureSnapshot) -> str:
    """Render one prepared snapshot without consulting any live owner."""

    snapshot = _capture_snapshot(value)
    if type(snapshot) is CaptureStatus:
        return "\n".join(_status_lines(snapshot))
    if type(snapshot) is StackPage:
        return "\n".join(_stack_lines(snapshot))
    if type(snapshot) is DebugFrame:
        return _frame_text(snapshot)
    if type(snapshot) is ValuePage:
        return "\n".join(_value_page_lines(snapshot))
    return _node_text(snapshot)


def _html_text(value: str) -> str:
    return escape(value, quote=True)


def _status_html(status: CaptureStatus) -> str:
    lines = _status_lines(status)
    items = "".join(f"<li>{_html_text(line)}</li>" for line in lines[1:])
    return (
        '<section class="onec-capture onec-capture-status">'
        f"<h4>CAPTURE: <code>{_html_text(status.phase.value)}</code></h4>"
        f"<ul>{items}</ul></section>"
    )


def _stack_html(page: StackPage) -> str:
    entries, omitted = _bounded_stack_entries(page.frames)
    items: list[str] = []
    for frame in entries:
        if type(frame) is RuntimeFrameMarker:
            value = f"… скрыто {frame.count} служебных кадров"
            css_class = "onec-capture-runtime-marker"
        else:
            value = _frame_text(frame)
            css_class = "onec-capture-frame"
        items.append(f'<li class="{css_class}">{_html_text(value)}</li>')
    if omitted:
        items.append(
            '<li class="onec-capture-truncated">'
            f"{_html_text(f'… не показано {omitted} элементов стека')}</li>"
        )
    shown = sum(type(frame) is DebugFrame for frame in entries)
    cursor = "none" if page.next_cursor is None else str(page.next_cursor)
    footer = f"Показано кадров: {shown} из {page.total}; next_cursor={cursor}"
    detail = _clean(page.detail, 64)
    mode = f"native, {detail}" if page.native else detail
    return (
        '<section class="onec-capture onec-capture-stack">'
        f"<h4>Стек вызовов <small>({_html_text(mode)})</small></h4>"
        f"<ul>{''.join(items)}</ul><p>{_html_text(footer)}</p></section>"
    )


def _frame_html(frame: DebugFrame) -> str:
    css_class = "onec-capture-runtime-frame" if frame.runtime_kernel else "onec-capture-frame"
    return (
        f'<section class="onec-capture {css_class}"><code>'
        f"{_html_text(_frame_text(frame))}</code></section>"
    )


def _node_html(node: ValueNode) -> str:
    css_class = "onec-capture-private" if node.private else "onec-capture-value"
    return f'<span class="{css_class}">{_html_text(_node_text(node))}</span>'


def _value_page_html(page: ValuePage) -> str:
    entries = page.items[:MAX_RENDER_ITEMS]
    items = [f"<li>{_node_html(node)}</li>" for node in entries]
    omitted = len(page.items) - len(entries)
    if omitted:
        items.append(
            '<li class="onec-capture-truncated">'
            f"{_html_text(f'… не показано {omitted} элементов')}</li>"
        )
    cursor = "none" if page.next_cursor is None else str(page.next_cursor)
    header = (
        f"{_path_text(page.path)}.{_clean(page.view, 64)} "
        f"[{page.start}:{page.stop}]"
    )
    footer = f"Показано значений: {len(entries)} из {page.total}; next_cursor={cursor}"
    return (
        '<section class="onec-capture onec-capture-values">'
        f"<h4><code>{_html_text(header)}</code></h4>"
        f"<ul>{''.join(items)}</ul><p>{_html_text(footer)}</p></section>"
    )


def render_capture_html(value: CaptureSnapshot) -> str:
    """Return portable, escaped ``text/html`` for one prepared snapshot."""

    snapshot = _capture_snapshot(value)
    if type(snapshot) is CaptureStatus:
        return _status_html(snapshot)
    if type(snapshot) is StackPage:
        return _stack_html(snapshot)
    if type(snapshot) is DebugFrame:
        return _frame_html(snapshot)
    if type(snapshot) is ValuePage:
        return _value_page_html(snapshot)
    return (
        '<section class="onec-capture onec-capture-node">'
        f"{_node_html(snapshot)}</section>"
    )


@dataclass(frozen=True, slots=True)
class CaptureSnapshotDisplay:
    """IPython representation protocol over an immutable core snapshot."""

    snapshot: CaptureSnapshot

    def __post_init__(self) -> None:
        _capture_snapshot(self.snapshot)

    def __repr__(self) -> str:
        return render_capture_text(self.snapshot)

    def _repr_html_(self) -> str:
        return render_capture_html(self.snapshot)


def _plain_formatter(value: CaptureSnapshot, printer: object, cycle: bool) -> None:
    del cycle
    printer.text(render_capture_text(value))  # type: ignore[attr-defined]


def _html_formatter(value: CaptureSnapshot) -> str:
    return render_capture_html(value)


@dataclass(frozen=True, slots=True)
class _FormatterRegistration:
    formatter: object
    value_type: type[object]
    installed: object
    had_direct: bool
    previous_direct: object | None
    deferred_key: tuple[str, str]
    had_deferred: bool
    previous_deferred: object | None
    had_deferred_after_install: bool
    deferred_after_install: object | None


_REGISTRATIONS: WeakKeyDictionary[object, tuple[_FormatterRegistration, ...]] = (
    WeakKeyDictionary()
)
_REGISTRATION_LOCK = Lock()


def install_capture_formatters(shell: object) -> None:
    """Register standard IPython MIME formatters when the shell exposes them."""

    display_formatter = getattr(shell, "display_formatter", None)
    formatters = getattr(display_formatter, "formatters", None)
    if not isinstance(formatters, dict):
        return
    selected = (
        (formatters.get("text/plain"), _plain_formatter),
        (formatters.get("text/html"), _html_formatter),
    )
    if any(
        formatter is None
        or not callable(getattr(formatter, "for_type", None))
        or not isinstance(getattr(formatter, "type_printers", None), dict)
        for formatter, _callback in selected
    ):
        return
    with _REGISTRATION_LOCK:
        try:
            if shell in _REGISTRATIONS:
                return
        except TypeError:
            return
        registrations: list[_FormatterRegistration] = []
        for formatter, callback in selected:
            printers = formatter.type_printers
            deferred_printers = getattr(formatter, "deferred_printers", None)
            for value_type in _SNAPSHOT_TYPES:
                had_direct = value_type in printers
                previous_direct = printers.get(value_type)
                deferred_key = (value_type.__module__, value_type.__name__)
                had_deferred = (
                    isinstance(deferred_printers, dict)
                    and deferred_key in deferred_printers
                )
                previous_deferred = (
                    deferred_printers.get(deferred_key)
                    if isinstance(deferred_printers, dict)
                    else None
                )
                formatter.for_type(value_type, callback)
                had_deferred_after_install = (
                    isinstance(deferred_printers, dict)
                    and deferred_key in deferred_printers
                )
                deferred_after_install = (
                    deferred_printers.get(deferred_key)
                    if isinstance(deferred_printers, dict)
                    else None
                )
                registrations.append(
                    _FormatterRegistration(
                        formatter,
                        value_type,
                        callback,
                        had_direct,
                        previous_direct,
                        deferred_key,
                        had_deferred,
                        previous_deferred,
                        had_deferred_after_install,
                        deferred_after_install,
                    )
                )
        _REGISTRATIONS[shell] = tuple(registrations)


def remove_capture_formatters(shell: object) -> None:
    """Restore printers replaced by this extension without clobbering newer ones."""

    with _REGISTRATION_LOCK:
        try:
            registrations = _REGISTRATIONS.pop(shell, ())
        except TypeError:
            return
        for registration in registrations:
            printers = getattr(registration.formatter, "type_printers", None)
            if not isinstance(printers, dict):
                continue
            if printers.get(registration.value_type) is registration.installed:
                if registration.had_direct:
                    printers[registration.value_type] = registration.previous_direct
                else:
                    printers.pop(registration.value_type, None)
            deferred_printers = getattr(
                registration.formatter,
                "deferred_printers",
                None,
            )
            if not isinstance(deferred_printers, dict):
                continue
            has_current_deferred = registration.deferred_key in deferred_printers
            current_deferred = deferred_printers.get(registration.deferred_key)
            deferred_is_unchanged = (
                has_current_deferred == registration.had_deferred_after_install
                and (
                    not has_current_deferred
                    or current_deferred is registration.deferred_after_install
                )
            )
            if not deferred_is_unchanged:
                continue
            if registration.had_deferred:
                deferred_printers[registration.deferred_key] = (
                    registration.previous_deferred
                )
            else:
                deferred_printers.pop(registration.deferred_key, None)
