"""Bind free notebook-method reads to the live request-scoped BSL context."""

from __future__ import annotations

from onec_runtime.bsl.lexer import tokenize
from onec_runtime.bsl.parser_target import PythonParserTarget
from onec_runtime.bsl.semantic_lowering import SemanticNotebookLowerer, WorkerExport
from onec_runtime.bsl.source_maps import (
    MappedSource,
    SourceArtifactKind,
    SourceSpan,
    SourceTransformBuilder,
)
from onec_runtime.errors import ProtocolError


WORKER_GLOBALS = "__OnecNotebookGlobals"
_CONTEXT_PREFIX = f"{WORKER_GLOBALS}."


def bind_notebook_method_globals(
    source: MappedSource,
    *,
    context_names: tuple[str, ...],
    exports: tuple[WorkerExport, ...],
) -> tuple[MappedSource, tuple[str, ...]]:
    """Qualify persistent roots without changing method-local BSL semantics."""
    if not context_names:
        return source, ()
    if any(
        token.type == "ID" and token.text.casefold() == WORKER_GLOBALS.casefold()
        for token in tokenize(source.text)
    ):
        raise ProtocolError("Notebook method uses a reserved Worker globals name")
    binding = SemanticNotebookLowerer(
        PythonParserTarget.from_generated(),
        context_names=context_names,
        worker_exports=exports,
        platform_globals=("RuntimeContextStoreServer",),
    ).bind_module(source.text)
    references = sorted(
        (
            reference
            for method in binding.method_scopes
            for reference in method.references
            if reference.kind == "persistent"
        ),
        key=lambda reference: reference.span.start,
    )
    if not references:
        return source, ()

    builder = SourceTransformBuilder(source)
    builder.synthetic(
        f"Перем {WORKER_GLOBALS} Экспорт;\n",
        SourceSpan(0, 0),
        "notebook_method_globals_declaration",
    )
    cursor = 0
    names: dict[str, str] = {}
    for reference in references:
        root = reference.span
        names.setdefault(reference.name.casefold(), reference.name)
        if root.start < cursor:
            raise ValueError("overlapping notebook method bindings")
        builder.copy(SourceSpan(cursor, root.start))
        builder.synthetic(
            _CONTEXT_PREFIX,
            SourceSpan(root.start, root.start),
            "notebook_method_persistent_read",
        )
        cursor = root.start
    builder.copy(SourceSpan(cursor, len(source.text)))
    return builder.build(SourceArtifactKind.WORKER_PROJECTION), tuple(names.values())
