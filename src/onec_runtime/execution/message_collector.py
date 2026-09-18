"""Source-mapped message collector around a notebook statement.

The semantic lowerer replaces ``Сообщить`` with an append to a per-cell array
in ``Контекст``. This wrapper owns that array for both successful and failed
BSL execution, then makes it available to the completion reader.
"""

from __future__ import annotations

from onec_runtime.bsl import (
    MappedSource, SourceArtifactKind, SourceSpan, SourceTransformBuilder,
)


def with_message_collector(
    source: MappedSource, messages_intercepted: int, key: str,
) -> MappedSource:
    """Wrap an intercepted statement without losing visible source locations."""

    if not messages_intercepted:
        return source
    start = SourceSpan(0, 0)
    end = SourceSpan(len(source.text), len(source.text))
    finalize = (
        'Контекст.Вставить("__onec_cell_messages_result_key", "'
        + key
        + '");\n'
        + 'Контекст.Вставить("__onec_cell_messages_result", Контекст.'
        + key
        + ');\n'
        + 'Контекст.Удалить("'
        + key
        + '");'
    )
    builder = SourceTransformBuilder(source)
    builder.synthetic(
        f'Контекст.Вставить("{key}", Новый Массив);\n',
        start, "message_collector_initialize",
    )
    builder.synthetic("Попытка\n", start, "message_collector_try")
    builder.copy(SourceSpan(0, len(source.text)))
    builder.synthetic(
        "\n" + finalize + "\n", end, "message_collector_finalize",
    )
    builder.synthetic("Исключение\n", end, "message_collector_exception")
    builder.synthetic(
        finalize + "\n", end, "message_collector_finalize",
    )
    builder.synthetic(
        "ВызватьИсключение;\n", end, "message_collector_rethrow",
    )
    builder.synthetic("КонецПопытки;", end, "message_collector_end")
    return builder.build(
        SourceArtifactKind.COLLECTOR_WRAPPER,
        wrapper_semantic_version="message-collector-v1",
        mode=source.artifact.mode,
    )
