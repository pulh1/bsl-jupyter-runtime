from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field
from typing import Any

from onec_runtime.bsl.lexer import tokenize
from onec_runtime.bsl.parser_target import PythonParserTarget
from onec_runtime.bsl.semantic_lowering import WorkerExport
from onec_runtime.bsl.source_maps import (
    MappedSource,
    SourceArtifactKind,
    SourceSpan,
    SourceTransformBuilder,
    SourceUnitKind,
    SourceUnitRef,
    mapped_visible_source,
    source_sha256,
)
from onec_runtime.errors import ProtocolError


@dataclass(frozen=True, slots=True)
class NotebookMethodProjection:
    """One complete AST method, including decorations and generated export."""

    export: WorkerExport
    mapped_source: MappedSource = field(repr=False)


@dataclass(frozen=True, slots=True)
class NotebookCellProjection:
    """Generated-AST projections of one immutable visible notebook cell."""

    visible: MappedSource = field(repr=False)
    worker: MappedSource | None = field(repr=False)
    statements: MappedSource | None = field(repr=False)
    exports: tuple[WorkerExport, ...]
    methods: tuple[NotebookMethodProjection, ...] = field(default=(), repr=False)

    @property
    def worker_source(self) -> str:
        return "" if self.worker is None else self.worker.text

    @property
    def statement_source(self) -> str:
        return "" if self.statements is None else self.statements.text

    @property
    def has_methods(self) -> bool:
        return self.worker is not None

    @property
    def has_statements(self) -> bool:
        return self.statements is not None


# Preserve imports of the former plain-string result type while making mapped
# projections authoritative.
NotebookCell = NotebookCellProjection


@dataclass(frozen=True, slots=True)
class _ProjectionItem:
    ordinal: int
    node: Any
    end: int


def split_notebook_cell(
    parser_target: PythonParserTarget,
    source: str,
    *,
    source_unit: SourceUnitRef | None = None,
) -> NotebookCellProjection:
    """Split a generated cell AST into independent mapped execution branches."""
    unit = source_unit or SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL,
        "anonymous-notebook-cell",
        0,
        source_sha256(source),
    )
    visible = mapped_visible_source(source, unit)
    root = parser_target.parse_ast(source, "ЯчейкаНоутбука")
    tokens = tokenize(source)
    token_starts = tuple(token.start for token in tokens)
    elements = root.Elements
    ordered: list[Any] = []
    while elements is not None and elements.Item is not None:
        ordered.append(elements.Item)
        elements = elements.Rest

    method_items: list[_ProjectionItem] = []
    statement_items: list[_ProjectionItem] = []
    exports: dict[str, WorkerExport] = {}
    for ordinal, item in enumerate(ordered):
        following_start = (
            ordered[ordinal + 1].span.start
            if ordinal + 1 < len(ordered)
            else len(source)
        )
        projected = _ProjectionItem(
            ordinal,
            item,
            _projection_end(item.span.end, following_start, tokens, token_starts),
        )
        if type(item).__name__ == "Method":
            declaration = item.Declaration
            export = WorkerExport(declaration.Name, declaration.Name)
            key = export.public_path.casefold()
            if key in exports:
                raise ProtocolError(
                    f"Notebook cell declares duplicate method {declaration.Name!r}"
                )
            exports[key] = export
            method_items.append(projected)
        else:
            statement_items.append(projected)

    return NotebookCellProjection(
        visible,
        _build_projection(
            visible,
            method_items,
            tokens,
            SourceArtifactKind.WORKER_PROJECTION,
        ),
        _build_projection(
            visible,
            statement_items,
            tokens,
            SourceArtifactKind.STATEMENT_PROJECTION,
        ),
        tuple(exports.values()),
        tuple(
            NotebookMethodProjection(
                export,
                _build_method_projection(visible, item, tokens),
            )
            for export, item in zip(exports.values(), method_items, strict=True)
        ),
    )


def _build_method_projection(
    visible: MappedSource,
    item: _ProjectionItem,
    tokens: tuple[Any, ...],
) -> MappedSource:
    builder = SourceTransformBuilder(visible)
    # The optional semicolon belongs to the notebook element separator, not
    # the method AST. In a composed Worker it starts the module body and makes
    # every subsequently appended declaration illegal on the platform.
    method = _ProjectionItem(item.ordinal, item.node, item.node.span.end)
    _copy_method(builder, method, tokens)
    return builder.build(SourceArtifactKind.WORKER_PROJECTION)


def _projection_end(
    item_end: int,
    following_start: int,
    tokens: tuple[Any, ...],
    token_starts: tuple[int, ...],
) -> int:
    """Include one grammar separator using its generated token span."""
    token_index = bisect_left(token_starts, item_end)
    following = (
        tokens[token_index]
        if token_index < len(tokens) and tokens[token_index].start < following_start
        else None
    )
    return following.end if following is not None and following.type == ";" else item_end


def _build_projection(
    visible: MappedSource,
    items: list[_ProjectionItem],
    tokens: tuple[Any, ...],
    kind: SourceArtifactKind,
) -> MappedSource | None:
    if not items:
        return None
    builder = SourceTransformBuilder(visible)
    previous: _ProjectionItem | None = None
    for item in items:
        if previous is not None:
            if item.ordinal == previous.ordinal + 1:
                separator = SourceSpan(previous.end, item.node.span.start)
                if separator.start < separator.end:
                    builder.copy(separator)
            else:
                builder.synthetic(
                    "\n",
                    SourceSpan(item.node.span.start, item.node.span.start),
                    "notebook_projection_join",
                )
        if kind is SourceArtifactKind.WORKER_PROJECTION:
            _copy_method(builder, item, tokens)
        else:
            builder.copy(SourceSpan(item.node.span.start, item.end))
        previous = item
    return builder.build(kind)


def _copy_method(
    builder: SourceTransformBuilder,
    item: _ProjectionItem,
    tokens: tuple[Any, ...],
) -> None:
    method = item.node
    declaration = method.Declaration
    if declaration.Export is not None:
        builder.copy(SourceSpan(method.span.start, item.end))
        return
    body_start = declaration.Body.span.start
    closing = next(
        (
            token
            for token in reversed(tokens)
            if method.span.start <= token.start < body_start and token.type == ")"
        ),
        None,
    )
    if closing is None:
        raise ProtocolError("Notebook method declaration has no parameter-list terminator")
    builder.copy(SourceSpan(method.span.start, closing.end))
    builder.synthetic(
        " Экспорт",
        SourceSpan(method.span.start, closing.end),
        "notebook_method_export",
    )
    builder.copy(SourceSpan(closing.end, item.end))
