"""Bind notebook values referenced by methods in the active Worker module."""

from __future__ import annotations

from onec_runtime.bsl.source_maps import (
    MappedSource, SourceArtifactKind, SourceSpan, SourceTransformBuilder,
)
from onec_runtime.experiment import bsl_string_literal


def with_notebook_worker_globals(
    source: MappedSource, names: tuple[str, ...],
) -> MappedSource:
    """Set the Worker global binding for one invocation and always restore it."""

    if not names:
        return source
    worker = "__OnecNotebookGlobalWorker"
    previous = "__OnecNotebookPreviousGlobals"
    globals_name = "__OnecNotebookBoundGlobals"
    initialization = (
        f'{worker} = e1cRuntimeКонтекст.RuntimeWorkerActiveGeneration.Modules.Получить("Worker");\n'
        f"{previous} = {worker}.__OnecNotebookGlobals;\n"
        f"{globals_name} = Новый Структура;\n"
    )
    for name in names:
        initialization += (
            f"{globals_name}.Вставить({bsl_string_literal(name)}, e1cRuntimeКонтекст.{name});\n"
        )
    initialization += f"{worker}.__OnecNotebookGlobals = {globals_name};\n"
    restore = f"{worker}.__OnecNotebookGlobals = {previous};\n"
    start = SourceSpan(0, 0)
    end = SourceSpan(len(source.text), len(source.text))
    builder = SourceTransformBuilder(source)
    builder.synthetic(initialization, start, "worker_globals_initialize")
    builder.synthetic("Попытка\n", start, "worker_globals_try")
    builder.copy(SourceSpan(0, len(source.text)))
    builder.synthetic("\n" + restore, end, "worker_globals_finalize")
    builder.synthetic("Исключение\n", end, "worker_globals_exception")
    builder.synthetic(restore, end, "worker_globals_finalize")
    builder.synthetic("ВызватьИсключение;\n", end, "worker_globals_rethrow")
    builder.synthetic("КонецПопытки;", end, "worker_globals_end")
    return builder.build(
        SourceArtifactKind.COLLECTOR_WRAPPER,
        wrapper_semantic_version="notebook-globals-v1",
        mode=source.artifact.mode,
    )
