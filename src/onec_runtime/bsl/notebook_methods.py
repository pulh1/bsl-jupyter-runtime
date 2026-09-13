"""Immutable notebook method upserts with original visible source ownership."""

from __future__ import annotations

from dataclasses import dataclass, field

from onec_runtime.bsl.diagnostics import VisibleSourceContext
from onec_runtime.bsl.lexer import Token, tokenize
from onec_runtime.bsl.notebook_cells import NotebookCellProjection, NotebookMethodProjection
from onec_runtime.bsl.semantic_lowering import WorkerExport
from onec_runtime.bsl.source_maps import (
    MappedSource,
    SourceArtifactKind,
    SourceSpan,
    SourceTransformBuilder,
    SourceUnitRef,
    compose_mapped_sources,
)
from onec_runtime.errors import ProtocolError


@dataclass(frozen=True, slots=True)
class _RetainedMethod:
    projection: NotebookMethodProjection = field(repr=False)
    visible: MappedSource = field(repr=False)


@dataclass(frozen=True, slots=True)
class NotebookMethodSet:
    """A candidate value; publication and commitment belong to the runtime."""

    mapped_source: MappedSource = field(repr=False)
    exports: tuple[WorkerExport, ...]
    intercepts_messages: bool
    _methods: tuple[_RetainedMethod, ...] = field(repr=False)
    _visible_sources: tuple[MappedSource, ...] = field(repr=False)
    bound_globals: tuple[str, ...] = ()

    @property
    def visible_source_context(self) -> VisibleSourceContext:
        # VisibleSourceContext is mutable. Hand out a detached index rather than
        # allowing consumers to alter the retained candidate's diagnostic state.
        return VisibleSourceContext(
            {_visible_unit(source): source.text for source in self._visible_sources}
        )


def _visible_unit(source: MappedSource) -> SourceUnitRef:
    unit = source.source_map.map_offset(0).unit
    if source.artifact.kind is not SourceArtifactKind.VISIBLE or unit is None:
        raise ProtocolError("Notebook method origin must be a visible source unit")
    return unit


def _unique_visible_sources(sources: tuple[MappedSource, ...]) -> tuple[MappedSource, ...]:
    retained: dict[tuple[object, str, int], MappedSource] = {}
    for source in sources:
        unit = _visible_unit(source)
        key = (unit.kind, unit.unit_id, unit.revision)
        existing = retained.get(key)
        if existing is not None and _visible_unit(existing).source_sha256 != unit.source_sha256:
            raise ProtocolError("Notebook methods have conflicting source identities")
        retained[key] = source
    return tuple(retained.values())


_MESSAGE_SINK = "__OnecWorkerMessageSink"
_MESSAGE_HELPER = "__OnecWorkerMessage"


def _message_call_tokens(source: str) -> tuple[Token, ...]:
    tokens = tokenize(source)
    return tuple(
        token
        for index, token in enumerate(tokens)
        if token.type == "ID"
        and token.text.casefold() in {"сообщить", "message"}
        and index + 1 < len(tokens)
        and tokens[index + 1].type == "("
        and (
            index == 0
            or tokens[index - 1].type not in {".", "ПРОЦЕДУРА", "ФУНКЦИЯ"}
        )
    )


def instrument_notebook_worker_messages(source: MappedSource) -> MappedSource:
    """Route notebook method messages to the active cell without losing origins."""
    calls = _message_call_tokens(source.text)
    if not calls:
        return source
    reserved = {_MESSAGE_SINK.casefold(), _MESSAGE_HELPER.casefold()}
    if any(
        token.type == "ID" and token.text.casefold() in reserved
        for token in tokenize(source.text)
    ):
        raise ProtocolError("Notebook method uses a reserved Worker message name")
    builder = SourceTransformBuilder(source)
    builder.synthetic(
        f"Перем {_MESSAGE_SINK} Экспорт;\n",
        SourceSpan(0, 0),
        "worker_message_sink_declaration",
    )
    position = 0
    for call in calls:
        builder.copy(SourceSpan(position, call.start))
        builder.derived(
            _MESSAGE_HELPER,
            SourceSpan(call.start, call.end),
            "worker_message_call",
        )
        position = call.end
    builder.copy(SourceSpan(position, len(source.text)))
    builder.synthetic(
        "\n\n"
        f"Процедура {_MESSAGE_HELPER}(Текст = Неопределено, Статус = Неопределено)\n"
        f"    Если {_MESSAGE_SINK} = Неопределено Тогда\n"
        "        Если Статус = Неопределено Тогда\n"
        "            Сообщить(Текст);\n"
        "        Иначе\n"
        "            Сообщить(Текст, Статус);\n"
        "        КонецЕсли;\n"
        "    Иначе\n"
        f"        RuntimeKernelServer.ДобавитьСообщение({_MESSAGE_SINK}, Текст, Статус);\n"
        "    КонецЕсли;\n"
        "КонецПроцедуры",
        SourceSpan(len(source.text), len(source.text)),
        "worker_message_helper",
    )
    return builder.build(SourceArtifactKind.WORKER_PROJECTION)


def merge_notebook_methods(
    previous: NotebookMethodSet | None,
    cell: NotebookCellProjection,
) -> NotebookMethodSet:
    """Replace declared names in place, append new names, retain omitted names."""
    if previous is not None and not isinstance(previous, NotebookMethodSet):
        raise ProtocolError("Previous notebook methods must be a NotebookMethodSet")
    if not isinstance(cell, NotebookCellProjection):
        raise ProtocolError("Notebook methods require a parsed cell projection")
    if tuple(method.export for method in cell.methods) != cell.exports:
        raise ProtocolError("Notebook method projections do not match their exports")
    # Check before replacement: even replacing the last old method cannot reuse
    # its explicit identity with a different hash.
    _unique_visible_sources(
        (() if previous is None else previous._visible_sources) + (cell.visible,)
    )
    methods = {
        method.projection.export.method.casefold(): method
        for method in (() if previous is None else previous._methods)
    }
    declared: set[str] = set()
    for projection in cell.methods:
        key = projection.export.method.casefold()
        if key in declared:
            raise ProtocolError("Notebook cell declares duplicate methods")
        declared.add(key)
        methods[key] = _RetainedMethod(projection, cell.visible)
    retained = tuple(methods.values())
    visible_sources = _unique_visible_sources(
        tuple(method.visible for method in retained) + (cell.visible,)
    )
    mapped = compose_mapped_sources(
        tuple(method.projection.mapped_source for method in retained),
        visible_sources=visible_sources,
        kind=SourceArtifactKind.WORKER_PROJECTION,
    )
    return NotebookMethodSet(
        mapped,
        tuple(method.projection.export for method in retained),
        bool(_message_call_tokens(mapped.text)),
        retained,
        visible_sources,
    )
