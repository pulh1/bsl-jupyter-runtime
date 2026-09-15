from __future__ import annotations

from dataclasses import asdict
import hashlib
import json

import pytest
import onec_runtime.privacy as privacy

from onec_runtime.bsl.diagnostics import (
    DiagnosticTextSpan,
    DiagnosticCoordinateSpace,
    DiagnosticStage,
    ErrorTraceFrameOrigin,
    MappingConfidence,
    PlatformCoordinateCodec,
    VisibleSourceContext,
    WorkerDiagnosticArtifact,
    normalize_platform_diagnostic_trace,
    parse_platform_diagnostic,
    normalize_source_error,
    remap_platform_diagnostic,
    remap_worker_runtime_diagnostic,
    remap_worker_stage_diagnostic,
)
from onec_runtime.bsl.lexer import BslLexError, tokenize
from onec_runtime.bsl.parser_target import BslParseError, PythonParserTarget
from onec_runtime.bsl.semantic_lowering import (
    LoweringMode,
    SemanticLoweringError,
    SemanticNotebookLowerer,
)
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


def _visible(source: str) -> MappedSource:
    return mapped_visible_source(
        source,
        SourceUnitRef(
            SourceUnitKind.NOTEBOOK_CELL,
            "diagnostic-cell",
            3,
            source_sha256(source),
        ),
    )


def _wrapped(source: str) -> MappedSource:
    visible = _visible(source)
    builder = SourceTransformBuilder(visible)
    builder.copy(SourceSpan(0, len(source)))
    return builder.build(SourceArtifactKind.EXECUTED_BSL)


def _synthetic_prelude(source: str = "Результат = 1;") -> MappedSource:
    visible = _visible(source)
    builder = SourceTransformBuilder(visible)
    builder.synthetic(
        "// collector\n",
        SourceSpan(0, 0),
        "message_collector_prelude",
    )
    builder.copy(SourceSpan(0, len(source)))
    return builder.build(SourceArtifactKind.EXECUTED_BSL)


def _visible_context(source: str) -> VisibleSourceContext:
    visible = _visible(source)
    unit = visible.source_map.segments[0].origin_ref
    assert isinstance(unit, SourceUnitRef)
    return VisibleSourceContext({unit: source})


def _worker_diagnostic_artifact(
    logical_name: str,
    revision: int,
    registration_name: str,
    artifact_sha256: str,
    manifest_sha256: str,
    *,
    source: str | None = None,
) -> WorkerDiagnosticArtifact:
    text = source or f"Функция {logical_name}() Экспорт\nВозврат 1;\nКонецФункции"
    unit = SourceUnitRef(
        SourceUnitKind.MODULE,
        logical_name,
        revision,
        source_sha256(text),
    )
    visible = mapped_visible_source(text, unit)
    builder = SourceTransformBuilder(visible)
    builder.copy(SourceSpan(0, len(text)))
    mapped = builder.build(SourceArtifactKind.WORKER_PROJECTION)
    return WorkerDiagnosticArtifact(
        logical_name=logical_name,
        revision=revision,
        artifact_sha256=artifact_sha256,
        registration_name=registration_name,
        manifest_sha256=manifest_sha256,
        source_map_sha256=mapped.source_map_sha256,
        mapped_source=mapped,
        visible_source_context=VisibleSourceContext({unit: text}),
    )


def _worker_diagnostic_artifact_from_mapped(
    *,
    logical_name: str,
    revision: int,
    registration_name: str,
    artifact_sha256: str,
    manifest_sha256: str,
    mapped: MappedSource,
    unit: SourceUnitRef,
    visible_source: str,
) -> WorkerDiagnosticArtifact:
    return WorkerDiagnosticArtifact(
        logical_name=logical_name,
        revision=revision,
        artifact_sha256=artifact_sha256,
        registration_name=registration_name,
        manifest_sha256=manifest_sha256,
        source_map_sha256=mapped.source_map_sha256,
        mapped_source=mapped,
        visible_source_context=VisibleSourceContext({unit: visible_source}),
    )


def _diagnostic_text(raw: str, span: DiagnosticTextSpan | None) -> str | None:
    return None if span is None else raw[span.start : span.end]


def test_parses_ordered_causes_frames_and_platform_fragments() -> None:
    raw = (
        "Ошибка оболочки\n"
        "{ОбщийМодуль.Верхний.Модуль(10,2)}: верхний кадр\n"
        "по причине:\n"
        "Деление на ноль\n"
        "{<Неизвестный модуль>(2,5)}: выражение 1 / 0\n"
        "  дополнительный контекст\n"
        "{ОбщийМодуль.Нижний.Модуль(20)}: вызывающий кадр"
    )

    parsed = parse_platform_diagnostic(raw)

    assert [_diagnostic_text(raw, item.summary_span) for item in parsed.causes] == [
        "Ошибка оболочки",
        "Деление на ноль",
    ]
    assert [item.frame_ordinals for item in parsed.causes] == [(0,), (1, 2)]
    assert [item.cause_ordinal for item in parsed.frames] == [0, 1, 1]
    assert [_diagnostic_text(raw, item.detail_span) for item in parsed.frames] == [
        "верхний кадр",
        "выражение 1 / 0\n  дополнительный контекст",
        "вызывающий кадр",
    ]
    assert [item.location.column for item in parsed.frames] == [2, 5, None]


def test_nonstructural_cause_text_does_not_split_chain() -> None:
    raw = "Оболочка: по причине: значение\n{ОбщийМодуль.Сервис.Модуль(2,1)}: сбой"

    parsed = parse_platform_diagnostic(raw)

    assert len(parsed.causes) == 1
    assert parsed.causes[0].frame_ordinals == (0,)


def test_cause_and_frame_limits_are_independent() -> None:
    raw = "\nпо причине:\n".join(
        f"Причина {index}\n{{Модуль{index}(1,1)}}: кадр"
        for index in range(33)
    )
    raw += "\n" + "\n".join(
        f"{{ДополнительныйМодуль{index}(1,1)}}: кадр"
        for index in range(96)
    )

    parsed = parse_platform_diagnostic(raw)

    assert len(parsed.causes) == 32
    assert parsed.causes_truncated is True
    assert len(parsed.frames) == 128
    assert parsed.frames_truncated is True
    assert parsed.platform_diagnostic_truncated is False


def test_unrecognized_blocks_are_retained_as_opaque_text() -> None:
    raw = (
        "{ОбщийМодуль.Первый.Модуль(1,1)}: first\n"
        "{not a module label(7,4)}: opaque\n"
        "{ОбщийМодуль.Второй.Модуль(2,1)}: second"
    )

    parsed = parse_platform_diagnostic(raw)

    opaque = "".join(raw[span.start : span.end] for span in parsed.opaque_spans)
    assert "{not a module label(7,4)}: opaque" in opaque


def test_parses_unknown_module_location_without_rewriting_message() -> None:
    raw = "{<Неизвестный модуль>(5,17)}: Деление на 0"

    parsed = parse_platform_diagnostic(raw)

    assert (parsed.line, parsed.column) == (5, 17)
    assert parsed.coordinate_space is DiagnosticCoordinateSpace.EXECUTED_BSL
    assert parsed.platform_diagnostic == raw
    assert parsed.platform_diagnostic_redacted is False


def test_parses_plain_unknown_module_and_host_module_as_distinct_spaces() -> None:
    unknown = parse_platform_diagnostic(
        "{Неизвестный модуль(46,18)}: Таблица не найдена"
    )
    host = parse_platform_diagnostic(
        "{Обработка.Расчет.МодульОбъекта(12,7)}: Ошибка"
    )

    assert unknown.coordinate_space is DiagnosticCoordinateSpace.EXECUTED_BSL
    assert unknown.module_name == "Неизвестный модуль"
    assert host.coordinate_space is DiagnosticCoordinateSpace.HOST_MODULE
    assert host.module_name == "Обработка.Расчет.МодульОбъекта"
    assert (host.line, host.column) == (12, 7)


def test_compilation_marker_is_structural_and_selects_compilation_stage() -> None:
    raw = (
        "{<Неизвестный модуль>(1,4)}: Процедура не определена\n"
        "[ОшибкаКомпиляцииВстроенногоЯзыка]"
    )
    parsed = parse_platform_diagnostic(raw)

    diagnostic = remap_platform_diagnostic(
        parsed,
        _wrapped("abc"),
        stage=DiagnosticStage.EXECUTION,
    )

    assert parsed.has_compilation_marker is True
    assert diagnostic.stage is DiagnosticStage.COMPILATION


def test_nested_execute_compile_cause_maps_one_exact_visible_code_point() -> None:
    source = "Task10UndefinedCompileProcedure();"
    visible = _visible(source)
    builder = SourceTransformBuilder(visible)
    builder.synthetic(
        "// runtime line 1\n"
        "// runtime line 2\n"
        "// runtime line 3\n"
        "// runtime line 4\n",
        SourceSpan(0, 0),
        "runtime_prelude",
    )
    builder.copy(SourceSpan(0, len(source)))
    executed = builder.build(SourceArtifactKind.EXECUTED_BSL)
    raw = (
        "Ошибка транспортной оболочки\n"
        "по причине:\n"
        "{<Неизвестный модуль>(5,1)}: Процедура не определена\n"
        "[ОшибкаКомпиляцииВстроенногоЯзыка]"
    )

    parsed = parse_platform_diagnostic(raw)
    diagnostic = remap_platform_diagnostic(
        parsed,
        executed,
        stage=DiagnosticStage.EXECUTION,
        visible_source_context=_visible_context(source),
    )

    assert (parsed.line, parsed.column) == (5, 1)
    assert parsed.has_compilation_marker is True
    assert diagnostic.stage is DiagnosticStage.COMPILATION
    assert diagnostic.mapping_confidence is MappingConfidence.EXACT
    assert diagnostic.visible_location is not None
    assert diagnostic.visible_location.span == SourceSpan(0, 1)


@pytest.mark.parametrize(
    "raw",
    (
        "Оболочка\n"
        "{<Неизвестный модуль>(5,1)}: Ошибка\n"
        "[ОшибкаКомпиляцииВстроенногоЯзыка]",
        "Оболочка\n"
        "{<Неизвестный модуль>(5,1)}: Ошибка\n"
        "по причине:\n"
        "[ОшибкаКомпиляцииВстроенногоЯзыка]",
        "Оболочка\nпо причине:\n"
        "{ОбщийМодуль.Сервис.Модуль(5,1)}: Ошибка\n"
        "[ОшибкаКомпиляцииВстроенногоЯзыка]",
        "Оболочка\nпо причине:\n"
        "{<Неизвестный модуль>(5,1)}: Ошибка\n"
        "{<Неизвестный модуль>(6,1)}: Еще ошибка\n"
        "[ОшибкаКомпиляцииВстроенногоЯзыка]",
        "Оболочка\nпо причине:\n"
        "{<Неизвестный модуль>(5,1)}: Ошибка\n"
        "[ОшибкаКомпиляцииВстроенногоЯзыка] trailing",
    ),
)
def test_nested_compile_location_rejects_ambiguous_or_unfenced_shapes(
    raw: str,
) -> None:
    parsed = parse_platform_diagnostic(raw)

    assert parsed.line is None
    assert parsed.column is None
    assert parsed.has_compilation_marker is False


def test_recognizes_common_inline_compilation_marker() -> None:
    parsed = parse_platform_diagnostic(
        "{<Неизвестный модуль>(1,4)}: Процедура не определена "
        "[ОшибкаКомпиляцииВстроенногоЯзыка]"
    )

    assert parsed.has_compilation_marker is True


@pytest.mark.parametrize(
    "raw",
    (
        "{<Неизвестный модуль>(1,1)}: prefix"
        "[ОшибкаКомпиляцииВстроенногоЯзыка]",
        "{<Неизвестный модуль>(1,1)}: "
        "[ОшибкаКомпиляцииВстроенногоЯзыка] suffix",
        "[ОшибкаКомпиляцииВстроенногоЯзыка]\n"
        "{<Неизвестный модуль>(1,1)}: Ошибка",
        "Просто текст [ОшибкаКомпиляцииВстроенногоЯзыка]",
    ),
)
def test_rejects_embedded_prefixed_or_suffixed_compilation_markers(raw: str) -> None:
    assert parse_platform_diagnostic(raw).has_compilation_marker is False


def test_records_only_allowlisted_locations_at_line_anchors() -> None:
    raw = (
        "{<Неизвестный модуль>(5,17)}: Верхняя ошибка\n"
        "по причине: 99,88\n"
        "{ОбщийМодуль.Сервис.Модуль(2,9)}: Вложенная ошибка"
    )

    parsed = parse_platform_diagnostic(raw)

    assert (parsed.line, parsed.column) == (5, 17)
    assert parsed.additional_locations == ((2, 9),)


def test_unknown_format_does_not_search_prose_for_coordinates() -> None:
    parsed = parse_platform_diagnostic(
        "Ошибка сервера в модуле на строке 5, колонке 17"
    )

    assert parsed.coordinate_space is DiagnosticCoordinateSpace.UNKNOWN
    assert parsed.line is None
    assert parsed.column is None
    assert parsed.additional_locations == ()


def test_rejects_non_module_braced_labels() -> None:
    parsed = parse_platform_diagnostic("{not a module label(5,17)}: Ошибка")

    assert parsed.coordinate_space is DiagnosticCoordinateSpace.UNKNOWN
    assert parsed.line is None


def test_platform_diagnostic_is_utf8_byte_bounded_and_hashes_original() -> None:
    raw = "{<Неизвестный модуль>(1,1)}: " + "😀" * 20_000

    parsed = parse_platform_diagnostic(raw)
    retained_size = len(parsed.platform_diagnostic.encode("utf-8"))

    assert retained_size <= 64 * 1024
    assert retained_size + len("😀".encode("utf-8")) > 64 * 1024
    assert parsed.platform_diagnostic.encode("utf-8").decode("utf-8") == (
        parsed.platform_diagnostic
    )
    assert parsed.platform_diagnostic_truncated is True
    assert parsed.platform_diagnostic_sha256 == hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()
    assert parsed.platform_diagnostic_redacted is False


def test_parses_canonical_line_only_native_and_unknown_locations() -> None:
    parsed = parse_platform_diagnostic(
        "{ОбщийМодуль.Сервис.Модуль(12)}: native\n"
        "{<Неизвестный модуль>(3)}: generated"
    )

    assert [
        (item.module_name, item.line, item.column)
        for item in parsed.locations
    ] == [
        ("ОбщийМодуль.Сервис.Модуль", 12, None),
        ("<Неизвестный модуль>", 3, None),
    ]


def test_line_only_parser_rejects_zero_and_does_not_classify_decorated_worker() -> None:
    registration = "OnecRuntime_aaaaaaaa_aaaaaaaaaaaaaaaa"
    parsed = parse_platform_diagnostic(
        "{ОбщийМодуль.Сервис.Модуль(0)}: zero\n"
        f"{{Decorator.ВнешняяОбработка.{registration}.МодульОбъекта(2)}}: decorated"
    )

    assert len(parsed.locations) == 1
    assert parsed.locations[0].module_name.startswith("Decorator.")
    assert parsed.locations[0].worker_artifact_location is None


def test_private_platform_text_fails_closed_under_generic_serialization() -> None:
    secret = "TOP-SECRET-PLATFORM-PROSE"
    raw = f"{{<Неизвестный модуль>(1,1)}}: {secret}"
    parsed = parse_platform_diagnostic(raw)
    normalized = remap_platform_diagnostic(
        parsed,
        _wrapped("x"),
        stage=DiagnosticStage.EXECUTION,
        visible_source_context=_visible_context("x"),
    )

    for model in (parsed, normalized):
        generic = asdict(model)
        assert secret not in repr(model)
        assert secret not in repr(generic)
        assert not hasattr(generic["_platform_evidence"], "text")
        assert secret not in json.dumps(model, default=str, ensure_ascii=False)
        assert secret not in json.dumps(generic, default=str, ensure_ascii=False)
        with pytest.raises(TypeError):
            json.dumps(model, ensure_ascii=False)
        with pytest.raises(TypeError):
            json.dumps(generic, ensure_ascii=False)

    assert parsed.platform_diagnostic == raw
    assert normalized.platform_diagnostic == raw


def test_main_trace_maps_every_generated_frame_and_visible_line() -> None:
    source = "Первый();\nВторой();"
    diagnostic = remap_platform_diagnostic(
        parse_platform_diagnostic(
            "{<Неизвестный модуль>(1,1)}: first\n"
            "{<Неизвестный модуль>(2,1)}: second"
        ),
        _wrapped(source),
        stage=DiagnosticStage.EXECUTION,
        visible_source_context=_visible_context(source),
    )

    assert [frame.origin for frame in diagnostic.frames] == [
        ErrorTraceFrameOrigin.EXECUTED_ARTIFACT,
        ErrorTraceFrameOrigin.EXECUTED_ARTIFACT,
    ]
    assert [frame.mapping_confidence for frame in diagnostic.frames] == [
        MappingConfidence.EXACT,
        MappingConfidence.EXACT,
    ]
    assert [frame.visible_location.line for frame in diagnostic.frames] == [1, 2]
    context = _visible_context(source)
    unit = diagnostic.frames[0].source_unit
    assert unit is not None
    assert [frame.visible_line_span for frame in diagnostic.frames] == [
        context.line_range(unit, 1),
        context.line_range(unit, 2),
    ]


def test_native_trace_keeps_direct_platform_coordinates_without_map() -> None:
    diagnostic = normalize_platform_diagnostic_trace(
        parse_platform_diagnostic(
            "{ОбщийМодуль.Сервис.Модуль(12)}: native frame"
        ),
        stage=DiagnosticStage.EXECUTION,
    )

    frame = diagnostic.frames[0]
    assert frame.origin is ErrorTraceFrameOrigin.NATIVE_MODULE
    assert (frame.platform_location.line, frame.platform_location.column) == (12, None)
    assert frame.mapping_confidence is MappingConfidence.UNKNOWN
    assert frame.visible_location is None


@pytest.mark.parametrize("pinned_artifacts", ([], {}, "", 0))
def test_trace_rejects_falsey_non_tuple_pinned_artifacts_without_manifest(
    pinned_artifacts: object,
) -> None:
    with pytest.raises(ValueError, match="pinned artifacts"):
        normalize_platform_diagnostic_trace(
            parse_platform_diagnostic("native frame"),
            stage=DiagnosticStage.EXECUTION,
            pinned_artifacts=pinned_artifacts,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("pinned_manifest_sha256", (b"", 0, [], {}))
def test_trace_rejects_non_string_pinned_manifest_identity(
    pinned_manifest_sha256: object,
) -> None:
    with pytest.raises(ValueError, match="manifest identity"):
        normalize_platform_diagnostic_trace(
            parse_platform_diagnostic("native frame"),
            stage=DiagnosticStage.EXECUTION,
            pinned_manifest_sha256=pinned_manifest_sha256,  # type: ignore[arg-type]
        )


def test_trace_order_does_not_redefine_legacy_primary_location() -> None:
    source = "Результат = 1;"
    diagnostic = remap_platform_diagnostic(
        parse_platform_diagnostic(
            "{ОбщийМодуль.Сервис.Модуль(7,3)}: host\n"
            "{<Неизвестный модуль>(1,1)}: generated"
        ),
        _wrapped(source),
        stage=DiagnosticStage.EXECUTION,
        visible_source_context=_visible_context(source),
    )

    assert diagnostic.mapping_confidence is MappingConfidence.UNKNOWN
    assert diagnostic.visible_location is None
    assert diagnostic.frames[0].origin is ErrorTraceFrameOrigin.NATIVE_MODULE
    assert diagnostic.frames[1].mapping_confidence is MappingConfidence.EXACT


def test_line_only_main_frames_map_only_one_exact_code_span() -> None:
    exact_source = "    Результат = 1;"
    exact = remap_platform_diagnostic(
        parse_platform_diagnostic("{<Неизвестный модуль>(1)}: exact"),
        _wrapped(exact_source),
        stage=DiagnosticStage.EXECUTION,
        visible_source_context=_visible_context(exact_source),
    )

    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL,
        "ambiguous-cell",
        1,
        source_sha256("Первый();Пропуск();Второй();"),
    )
    visible = mapped_visible_source("Первый();Пропуск();Второй();", unit)
    builder = SourceTransformBuilder(visible)
    builder.copy(SourceSpan(0, 9))
    builder.copy(SourceSpan(19, len(visible.text)))
    ambiguous_source = builder.build(SourceArtifactKind.EXECUTED_BSL)
    ambiguous = remap_platform_diagnostic(
        parse_platform_diagnostic("{<Неизвестный модуль>(1)}: ambiguous"),
        ambiguous_source,
        stage=DiagnosticStage.EXECUTION,
        visible_source_context=VisibleSourceContext(
            {unit: "Первый();Пропуск();Второй();"}
        ),
    )

    assert exact.frames[0].mapping_confidence is MappingConfidence.EXACT
    assert exact.frames[0].lowered_location.column == 5
    assert ambiguous.frames[0].mapping_confidence is MappingConfidence.UNKNOWN
    assert ambiguous.frames[0].lowered_location is None


def test_coordinate_codec_uses_exact_unicode_and_crlf_artifact() -> None:
    source = "Первая\r\n😀Вторая\n"
    codec = PlatformCoordinateCodec(source)

    assert codec.to_offset(2, 1) == source.index("😀")
    assert codec.to_offset(2, 2) == source.index("В")
    assert codec.to_offset(1, 8) == source.index("\n")
    assert codec.to_offset(4, 1) is None
    assert codec.to_offset(2, 100) is None


def test_exact_location_maps_to_one_visible_code_point() -> None:
    source = "Первая\r\n😀Ошибка"
    executed = _wrapped(source)

    diagnostic = remap_platform_diagnostic(
        parse_platform_diagnostic("{<Неизвестный модуль>(2,2)}: Ошибка"),
        executed,
        stage=DiagnosticStage.EXECUTION,
        visible_source_context=_visible_context(source),
    )

    offset = source.index("О")
    assert diagnostic.mapping_confidence is MappingConfidence.EXACT
    assert diagnostic.visible_location is not None
    assert diagnostic.visible_location.span == SourceSpan(offset, offset + 1)
    assert diagnostic.visible_location.source_unit.unit_id == "diagnostic-cell"
    assert diagnostic.related_visible_span is None
    assert diagnostic.lowered_location is not None
    assert (diagnostic.lowered_location.line, diagnostic.lowered_location.column) == (2, 2)


def test_transformed_exact_location_uses_visible_crlf_non_bmp_coordinates() -> None:
    source = "Первая\r\n😀Ошибка"
    visible = _visible(source)
    builder = SourceTransformBuilder(visible)
    builder.synthetic("// prelude\n", SourceSpan(0, 0), "runtime_prelude")
    builder.copy(SourceSpan(0, len(source)))
    executed = builder.build(SourceArtifactKind.EXECUTED_BSL)

    diagnostic = remap_platform_diagnostic(
        parse_platform_diagnostic("{<Неизвестный модуль>(3,2)}: Ошибка"),
        executed,
        stage=DiagnosticStage.EXECUTION,
        visible_source_context=_visible_context(source),
    )

    offset = source.index("О")
    assert diagnostic.mapping_confidence is MappingConfidence.EXACT
    assert diagnostic.visible_location is not None
    assert (diagnostic.visible_location.line, diagnostic.visible_location.column) == (
        2,
        2,
    )
    assert diagnostic.visible_location.span == SourceSpan(offset, offset + 1)


@pytest.mark.parametrize("include_context", (False, True))
def test_missing_or_hash_mismatched_visible_context_degrades_exact_mapping(
    include_context: bool,
) -> None:
    source = "Ошибка"
    visible = _visible(source)
    unit = visible.source_map.segments[0].origin_ref
    assert isinstance(unit, SourceUnitRef)
    context = VisibleSourceContext({unit: "stale source"}) if include_context else None

    diagnostic = remap_platform_diagnostic(
        parse_platform_diagnostic("{<Неизвестный модуль>(1,1)}: Ошибка"),
        _wrapped(source),
        stage=DiagnosticStage.EXECUTION,
        visible_source_context=context,
    )

    assert diagnostic.mapping_confidence is MappingConfidence.NEAREST
    assert diagnostic.visible_location is None
    assert diagnostic.related_visible_span == SourceSpan(0, 1)


def test_derived_location_becomes_nearest_without_exact_visible_column() -> None:
    visible = _visible("Имя")
    builder = SourceTransformBuilder(visible)
    builder.derived("ИМЯ", SourceSpan(0, 3), "normalized_name")
    executed = builder.build(SourceArtifactKind.EXECUTED_BSL)

    diagnostic = remap_platform_diagnostic(
        parse_platform_diagnostic("{<Неизвестный модуль>(1,2)}: Ошибка"),
        executed,
        stage=DiagnosticStage.EXECUTION,
    )

    assert diagnostic.mapping_confidence is MappingConfidence.NEAREST
    assert diagnostic.visible_location is None
    assert diagnostic.related_visible_span == SourceSpan(0, 3)
    assert diagnostic.source_unit is not None
    assert diagnostic.source_unit.unit_id == "diagnostic-cell"


def test_synthetic_wrapper_location_omits_visible_coordinates() -> None:
    diagnostic = remap_platform_diagnostic(
        parse_platform_diagnostic("{<Неизвестный модуль>(1,1)}: Ошибка"),
        _synthetic_prelude(),
        stage=DiagnosticStage.EXECUTION,
    )

    assert diagnostic.mapping_confidence is MappingConfidence.SYNTHETIC
    assert diagnostic.visible_location is None
    assert diagnostic.related_visible_span is not None
    assert diagnostic.synthetic_region == "message_collector_prelude"


@pytest.mark.parametrize(
    "raw",
    (
        "{ОбщийМодуль.Сервис.Модуль(1,1)}: Ошибка",
        "{<Неизвестный модуль>(99,1)}: Ошибка",
        "{<Неизвестный модуль>(1,0)}: Ошибка",
        "Нераспознанная ошибка",
    ),
)
def test_host_invalid_and_unrecognized_coordinates_remain_unknown(raw: str) -> None:
    diagnostic = remap_platform_diagnostic(
        parse_platform_diagnostic(raw),
        _wrapped("abc"),
        stage=DiagnosticStage.EXECUTION,
    )

    assert diagnostic.mapping_confidence is MappingConfidence.UNKNOWN
    assert diagnostic.visible_location is None
    assert diagnostic.related_visible_span is None
    assert diagnostic.lowered_location is None


def test_source_errors_have_stable_codes_and_half_open_spans() -> None:
    with pytest.raises(BslLexError) as lexical:
        tokenize("$")
    assert lexical.value.code == "unexpected_character"
    assert lexical.value.span == SourceSpan(0, 1)
    assert "Unexpected character" in str(lexical.value)

    target = PythonParserTarget.from_generated()
    with pytest.raises(BslParseError) as parsed:
        target.parse_ast("Результат = ;", "БлокНоутбука")
    assert parsed.value.code == "unexpected_token"
    assert parsed.value.span == SourceSpan(12, 12)
    assert "Unexpected" in str(parsed.value)

    with pytest.raises(SemanticLoweringError) as lowered:
        SemanticNotebookLowerer(target).lower(
            "Значение;",
            mode=LoweringMode.MAIN,
        )
    assert lowered.value.code == "bare_access_chain_statement"
    assert lowered.value.span == SourceSpan(0, 8)
    assert "at 0" in str(lowered.value)


def test_deterministic_parse_error_normalizes_through_exact_input() -> None:
    source = "Результат = ;"
    target = PythonParserTarget.from_generated()
    with pytest.raises(BslParseError) as raised:
        target.parse_ast(source, "БлокНоутбука")

    diagnostic = normalize_source_error(
        raised.value,
        _wrapped(source),
        stage=DiagnosticStage.PARSING,
        visible_source_context=_visible_context(source),
    )

    assert diagnostic.stage is DiagnosticStage.PARSING
    assert diagnostic.code == "unexpected_token"
    assert diagnostic.mapping_confidence is MappingConfidence.EXACT
    assert diagnostic.visible_location is not None
    assert (diagnostic.visible_location.line, diagnostic.visible_location.column) == (
        1,
        13,
    )
    assert diagnostic.visible_location.span == SourceSpan(12, 13)
    assert diagnostic.platform_diagnostic is None
    assert diagnostic.platform_diagnostic_sha256 is None
    assert diagnostic.runtime_summary == "BSL parsing failed"
    assert diagnostic.causes == ()
    assert diagnostic.frames == ()
    assert diagnostic.frames_truncated is False
    assert diagnostic.causes_truncated is False


def test_rich_trace_does_not_expand_existing_wire_shapes() -> None:
    """Break caught: trace fields leak into public or expert wire contracts."""
    raw = "{<Неизвестный модуль>(1,1)}: " + "x" * 10_000
    diagnostic = remap_platform_diagnostic(
        parse_platform_diagnostic(raw),
        _wrapped("Результат = 1;"),
        stage=DiagnosticStage.EXECUTION,
    )

    assert set(privacy.diagnostic_to_public_wire(diagnostic)) == {
        "diagnostic_id",
        "runtime_summary",
        "stage",
        "mapping_confidence",
        "visible_location",
        "related_visible_span",
        "excerpt",
        "synthetic_region",
    }
    assert set(privacy.diagnostic_to_expert_wire(diagnostic)) == {
        "diagnostic_id",
        "runtime_summary",
        "stage",
        "mapping_confidence",
        "visible_location",
        "related_visible_span",
        "excerpt",
        "synthetic_region",
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
    expert = privacy.diagnostic_to_expert_wire(diagnostic)
    assert expert["platform_diagnostic"] == raw
    assert expert["platform_diagnostic_truncated"] is False
    assert expert["platform_diagnostic_redacted"] is False


@pytest.mark.parametrize("phase", ("connect", "create"))
def test_worker_stage_diagnostic_resolves_only_exact_candidate_artifact(phase: str) -> None:
    manifest = "c" * 64
    artifact = _worker_diagnostic_artifact(
        "МодульБ",
        18,
        "OnecRuntime_bbbbbbbb_bbbbbbbbbbbbbbbb",
        "b" * 64,
        manifest,
    )
    raw = (
        "{ВнешняяОбработка."
        f"{artifact.registration_name}.МодульОбъекта(1,1)}}: Ошибка "
        "[ОшибкаКомпиляцииВстроенногоЯзыка]"
    )

    exact = remap_worker_stage_diagnostic(
        parse_platform_diagnostic(raw),
        artifact_sha256="b" * 64,
        phase=phase,
        candidate_manifest_sha256=manifest,
        candidate_artifacts=(artifact,),
    )
    missing = remap_worker_stage_diagnostic(
        parse_platform_diagnostic(raw),
        artifact_sha256="a" * 64,
        phase=phase,
        candidate_manifest_sha256=manifest,
        candidate_artifacts=(artifact,),
    )
    stale = remap_worker_stage_diagnostic(
        parse_platform_diagnostic(raw),
        artifact_sha256="b" * 64,
        phase=phase,
        candidate_manifest_sha256="d" * 64,
        candidate_artifacts=(artifact,),
    )

    assert exact.mapping_confidence is MappingConfidence.EXACT
    assert exact.source_unit is not None
    assert (exact.source_unit.unit_id, exact.source_unit.revision) == ("МодульБ", 18)
    assert exact.source_map_sha256 == artifact.mapped_source.source_map_sha256
    for unmapped in (missing, stale):
        assert unmapped.mapping_confidence is MappingConfidence.UNKNOWN
        assert unmapped.source_unit is None
        assert unmapped.source_map_sha256 is None
        assert unmapped.platform_diagnostic == raw


@pytest.mark.parametrize("compilation", (False, True))
def test_create_error_preserves_cause_stage_and_exact_logical_identity(compilation: bool) -> None:
    from onec_runtime.errors import BslExecutionError
    from onec_runtime.server_worker import remap_worker_artifact_stage_error

    manifest = "c" * 64
    first = _worker_diagnostic_artifact(
        "МодульА", 18, "OnecRuntime_aaaaaaaa_aaaaaaaaaaaaaaaa", "b" * 64, manifest,
    )
    second = _worker_diagnostic_artifact(
        "МодульБ", 18, "OnecRuntime_bbbbbbbb_bbbbbbbbbbbbbbbb", "b" * 64, manifest,
    )
    marker = "ОшибкаКомпиляцииВстроенногоЯзыка" if compilation else "ОшибкаВоВремяВыполненияВстроенногоЯзыка"
    body = (
        "Ошибка инициализации модуля 😀\nпо причине:\n"
        f"{{ВнешняяОбработка.{second.registration_name}.МодульОбъекта(1,1)}}: failure\n"
        f"[{marker}]"
    )
    header = (
        "onec-worker-root-prepare-stage=create\n"
        f"onec-worker-artifact-stage=artifact_sha256={'b' * 64};"
        f"logical_name_sha256={hashlib.sha256('модульб'.encode()).hexdigest()};"
        "phase=create;boundary=create;"
        f"diagnostic_utf16_length={len(body.encode('utf-16-le')) // 2}\n"
    )
    error = BslExecutionError(header + body + "\n{(70)}:ВызватьИсключение;\n[ОшибкаВоВремяВыполненияВстроенногоЯзыка]")
    result = remap_worker_artifact_stage_error(
        error, candidate_manifest_sha256=manifest, candidate_artifacts=(first, second),
    ).diagnostic
    assert result is not None
    assert result.platform_diagnostic == body
    assert result.stage is (DiagnosticStage.COMPILATION if compilation else DiagnosticStage.EXECUTION)
    assert result.mapping_confidence is MappingConfidence.EXACT
    assert result.source_unit is not None
    assert result.source_unit.unit_id == "МодульБ"


def test_worker_diagnostic_artifact_rejects_stale_source_map_identity() -> None:
    artifact = _worker_diagnostic_artifact(
        "МодульА",
        17,
        "OnecRuntime_aaaaaaaa_aaaaaaaaaaaaaaaa",
        "a" * 64,
        "c" * 64,
    )

    with pytest.raises(ValueError, match="source map"):
        WorkerDiagnosticArtifact(
            logical_name=artifact.logical_name,
            revision=artifact.revision,
            artifact_sha256=artifact.artifact_sha256,
            registration_name=artifact.registration_name,
            manifest_sha256=artifact.manifest_sha256,
            source_map_sha256="d" * 64,
            mapped_source=artifact.mapped_source,
            visible_source_context=artifact.visible_source_context,
        )


def test_worker_stage_ambiguous_artifact_identity_remains_explicitly_unmapped() -> None:
    manifest = "c" * 64
    first = _worker_diagnostic_artifact(
        "МодульА",
        17,
        "OnecRuntime_aaaaaaaa_aaaaaaaaaaaaaaaa",
        "a" * 64,
        manifest,
    )
    second = _worker_diagnostic_artifact(
        "МодульБ",
        18,
        "OnecRuntime_bbbbbbbb_aaaaaaaaaaaaaaaa",
        "a" * 64,
        manifest,
    )
    raw = "{<Неизвестный модуль>(1,1)}: ambiguous"

    diagnostic = remap_worker_stage_diagnostic(
        parse_platform_diagnostic(raw),
        artifact_sha256="a" * 64,
        phase="connect",
        candidate_manifest_sha256=manifest,
        candidate_artifacts=(first, second),
    )

    assert diagnostic.mapping_confidence is MappingConfidence.UNKNOWN
    assert diagnostic.code == "worker_artifact_identity_unmapped"
    assert diagnostic.platform_diagnostic == raw


@pytest.mark.parametrize(
    "region",
    (
        "worker_dependency_alias_declaration",
        "worker_dependency_alias_initializer",
    ),
)
def test_generated_dependency_alias_reports_binding_and_both_anchors(
    region: str,
) -> None:
    source = (
        "Функция Проверить()\n"
        "Возврат КадровыйУчет.Рассчитать();\n"
        "КонецФункции"
    )
    unit = SourceUnitRef(
        SourceUnitKind.MODULE,
        "МодульА",
        17,
        source_sha256(source),
    )
    visible = mapped_visible_source(source, unit)
    dependency = SourceSpan(
        source.index("КадровыйУчет"),
        source.index("КадровыйУчет") + len("КадровыйУчет"),
    )
    method = SourceSpan(0, source.index("\n"))
    builder = SourceTransformBuilder(visible)
    builder.derived(
        "КадровыйУчет = __OnecDependency;",
        dependency,
        region,
        anchor=method,
    )
    mapped = builder.build(SourceArtifactKind.WORKER_PROJECTION)
    artifact = WorkerDiagnosticArtifact(
        logical_name="МодульА",
        revision=17,
        artifact_sha256="a" * 64,
        registration_name="OnecRuntime_aaaaaaaa_aaaaaaaaaaaaaaaa",
        manifest_sha256="c" * 64,
        source_map_sha256=mapped.source_map_sha256,
        mapped_source=mapped,
        visible_source_context=VisibleSourceContext({unit: source}),
    )

    diagnostic = remap_worker_stage_diagnostic(
        parse_platform_diagnostic(
            "{ВнешняяОбработка."
            f"{artifact.registration_name}.МодульОбъекта(1,1)}}: binding"
        ),
        artifact_sha256="a" * 64,
        phase="connect",
        candidate_manifest_sha256="c" * 64,
        candidate_artifacts=(artifact,),
    )

    assert diagnostic.code == "dependency_binding"
    assert diagnostic.synthetic_region == "dependency_binding"
    assert diagnostic.dependency_anchor == dependency
    assert diagnostic.method_anchor == method
    assert diagnostic.source_unit == unit


def test_generated_dependency_field_reports_binding_and_dependency_anchor() -> None:
    source = "Возврат КадровыйУчет.Рассчитать();"
    unit = SourceUnitRef(
        SourceUnitKind.MODULE,
        "МодульА",
        17,
        source_sha256(source),
    )
    dependency = SourceSpan(
        source.index("КадровыйУчет"),
        source.index("КадровыйУчет") + len("КадровыйУчет"),
    )
    builder = SourceTransformBuilder(mapped_visible_source(source, unit))
    builder.synthetic(
        "Перем __OnecDependency Экспорт;",
        dependency,
        "worker_dependency_field",
    )
    mapped = builder.build(SourceArtifactKind.WORKER_PROJECTION)
    artifact = WorkerDiagnosticArtifact(
        logical_name="МодульА",
        revision=17,
        artifact_sha256="a" * 64,
        registration_name="OnecRuntime_aaaaaaaa_aaaaaaaaaaaaaaaa",
        manifest_sha256="c" * 64,
        source_map_sha256=mapped.source_map_sha256,
        mapped_source=mapped,
        visible_source_context=VisibleSourceContext({unit: source}),
    )

    diagnostic = remap_worker_stage_diagnostic(
        parse_platform_diagnostic(
            "{ВнешняяОбработка."
            f"{artifact.registration_name}.МодульОбъекта(1,1)}}: binding"
        ),
        artifact_sha256="a" * 64,
        phase="connect",
        candidate_manifest_sha256="c" * 64,
        candidate_artifacts=(artifact,),
    )

    assert diagnostic.code == "dependency_binding"
    assert diagnostic.synthetic_region == "dependency_binding"
    assert diagnostic.dependency_anchor == dependency
    assert diagnostic.method_anchor is None
    assert diagnostic.source_unit == unit


def test_runtime_worker_frames_map_callee_and_caller_independently() -> None:
    manifest = "c" * 64
    module_a = _worker_diagnostic_artifact(
        "МодульА",
        17,
        "OnecRuntime_aaaaaaaa_aaaaaaaaaaaaaaaa",
        "a" * 64,
        manifest,
    )
    module_b = _worker_diagnostic_artifact(
        "МодульБ",
        18,
        "OnecRuntime_bbbbbbbb_bbbbbbbbbbbbbbbb",
        "b" * 64,
        manifest,
    )
    raw = (
        "{ВнешняяОбработка."
        f"{module_b.registration_name}.МодульОбъекта(1,1)}}: callee failed\n"
        "{ВнешняяОбработка."
        f"{module_a.registration_name}.МодульОбъекта(1,1)}}: caller"
    )

    diagnostic = remap_worker_runtime_diagnostic(
        parse_platform_diagnostic(raw),
        pinned_manifest_sha256=manifest,
        pinned_artifacts=(module_a, module_b),
    )

    assert [frame.logical_name for frame in diagnostic.worker_frames] == [
        "МодульБ",
        "МодульА",
    ]
    assert [frame.revision for frame in diagnostic.worker_frames] == [18, 17]
    assert all(
        frame.mapping_confidence is MappingConfidence.EXACT
        for frame in diagnostic.worker_frames
    )
    assert diagnostic.source_unit is not None
    assert (diagnostic.source_unit.unit_id, diagnostic.source_unit.revision) == (
        "МодульБ",
        18,
    )
    assert diagnostic.platform_diagnostic == raw


def test_runtime_primary_identity_ignores_stale_manifest_source_map() -> None:
    current_manifest = "c" * 64
    stale = _worker_diagnostic_artifact(
        "МодульА",
        16,
        "OnecRuntime_aaaaaaaa_aaaaaaaaaaaaaaaa",
        "a" * 64,
        "d" * 64,
        source="Функция МодульА() Экспорт\nВозврат 0;\nКонецФункции",
    )
    current = _worker_diagnostic_artifact(
        "МодульА",
        17,
        stale.registration_name,
        stale.artifact_sha256,
        current_manifest,
    )

    diagnostic = remap_worker_runtime_diagnostic(
        parse_platform_diagnostic(
            "{ВнешняяОбработка."
            f"{current.registration_name}.МодульОбъекта(1,1)}}: current failure"
        ),
        pinned_manifest_sha256=current_manifest,
        pinned_artifacts=(stale, current),
    )

    assert diagnostic.source_unit is not None
    assert diagnostic.source_unit.revision == 17
    assert diagnostic.source_map_sha256 == current.source_map_sha256


def test_compound_platform_locator_preserves_exact_components() -> None:
    registration = "OnecRuntime_aaaaaaaa_aaaaaaaaaaaaaaaa"
    raw = (
        "{ВнешняяОбработка."
        f"{registration}.МодульОбъекта(7,11)}}: failure"
    )

    parsed = parse_platform_diagnostic(raw)

    assert len(parsed.locations) == 1
    assert parsed.locations[0].module_components == (
        "ВнешняяОбработка",
        registration,
        "МодульОбъекта",
    )
    assert parsed.platform_diagnostic == raw


def test_only_canonical_shape_is_classified_as_worker_artifact_location() -> None:
    registration = "OnecRuntime_aaaaaaaa_aaaaaaaaaaaaaaaa"
    raw = (
        "{ВнешняяОбработка."
        f"{registration}.МодульОбъекта(1,1)}}: canonical\n"
        f"{{{registration}(2,2)}}: bare\n"
        "{Decorator.ВнешняяОбработка."
        f"{registration}.МодульОбъекта(3,3)}}: decorated"
    )

    parsed = parse_platform_diagnostic(raw)

    assert parsed.locations[0].worker_artifact_location is not None
    assert (
        parsed.locations[0].worker_artifact_location.registration_name
        == registration
    )
    assert parsed.locations[1].worker_artifact_location is None
    assert parsed.locations[2].worker_artifact_location is None


@pytest.mark.parametrize("phase", ("upload", "connect"))
def test_worker_stage_uses_nested_exact_registration_location_not_host_wrapper(
    phase: str,
) -> None:
    manifest = "c" * 64
    registration = "OnecRuntime_bbbbbbbb_bbbbbbbbbbbbbbbb"
    artifact = _worker_diagnostic_artifact(
        "МодульБ",
        18,
        registration,
        "b" * 64,
        manifest,
    )
    raw = (
        "{ОбщийМодуль.Host.Модуль(99,99)}: wrapper\n"
        "{ВнешняяОбработка."
        f"{registration.lower()}.МодульОбъекта(1,1)}}: artifact\n"
        " [ОшибкаКомпиляцииВстроенногоЯзыка]"
    )

    diagnostic = remap_worker_stage_diagnostic(
        parse_platform_diagnostic(raw),
        artifact_sha256=artifact.artifact_sha256,
        phase=phase,
        candidate_manifest_sha256=manifest,
        candidate_artifacts=(artifact,),
    )

    assert diagnostic.mapping_confidence is MappingConfidence.EXACT
    assert diagnostic.source_unit is not None
    assert (diagnostic.source_unit.unit_id, diagnostic.source_unit.revision) == (
        "МодульБ",
        18,
    )
    assert diagnostic.lowered_location is not None
    assert (diagnostic.lowered_location.line, diagnostic.lowered_location.column) == (
        1,
        1,
    )
    assert diagnostic.platform_diagnostic == raw


@pytest.mark.parametrize("phase", ("upload", "connect"))
@pytest.mark.parametrize(
    "locator",
    (
        "{registration}",
        "ОбщийМодуль.{registration}.Модуль",
        "Decorator.ВнешняяОбработка.{registration}.МодульОбъекта",
        "ВнешняяОбработка.{registration}.МодульОбъекта.Decorator",
        "ВнешняяОбработка.OnecRuntime_deadbeef_deadbeefdeadbeef.МодульОбъекта",
    ),
)
def test_worker_stage_rejects_noncanonical_or_wrong_artifact_locator(
    phase: str,
    locator: str,
) -> None:
    manifest = "c" * 64
    artifact = _worker_diagnostic_artifact(
        "МодульБ",
        18,
        "OnecRuntime_bbbbbbbb_bbbbbbbbbbbbbbbb",
        "b" * 64,
        manifest,
    )
    raw = (
        "{" + locator.format(registration=artifact.registration_name)
        + "(1,1)}: failure"
    )

    diagnostic = remap_worker_stage_diagnostic(
        parse_platform_diagnostic(raw),
        artifact_sha256=artifact.artifact_sha256,
        phase=phase,
        candidate_manifest_sha256=manifest,
        candidate_artifacts=(artifact,),
    )

    assert diagnostic.mapping_confidence is MappingConfidence.UNKNOWN
    assert diagnostic.code == "worker_artifact_location_unmapped"
    assert diagnostic.source_unit is None
    assert diagnostic.platform_diagnostic == raw


@pytest.mark.parametrize("phase", ("upload", "connect"))
def test_worker_stage_rejects_duplicate_matching_canonical_locations(
    phase: str,
) -> None:
    manifest = "c" * 64
    artifact = _worker_diagnostic_artifact(
        "МодульБ",
        18,
        "OnecRuntime_bbbbbbbb_bbbbbbbbbbbbbbbb",
        "b" * 64,
        manifest,
    )
    raw = (
        "{ВнешняяОбработка."
        f"{artifact.registration_name}.МодульОбъекта(1,1)}}: first\n"
        "{ВнешняяОбработка."
        f"{artifact.registration_name}.МодульОбъекта(2,2)}}: duplicate"
    )

    diagnostic = remap_worker_stage_diagnostic(
        parse_platform_diagnostic(raw),
        artifact_sha256=artifact.artifact_sha256,
        phase=phase,
        candidate_manifest_sha256=manifest,
        candidate_artifacts=(artifact,),
    )

    assert diagnostic.mapping_confidence is MappingConfidence.UNKNOWN
    assert diagnostic.code == "worker_artifact_location_ambiguous"
    assert diagnostic.source_unit is None
    assert diagnostic.platform_diagnostic == raw


@pytest.mark.parametrize("phase", ("upload", "connect"))
def test_worker_stage_canonical_wrapper_and_registration_are_case_insensitive(
    phase: str,
) -> None:
    manifest = "c" * 64
    artifact = _worker_diagnostic_artifact(
        "МодульБ",
        18,
        "OnecRuntime_bbbbbbbb_bbbbbbbbbbbbbbbb",
        "b" * 64,
        manifest,
    )
    raw = (
        "{ВНЕШНЯЯОБРАБОТКА."
        f"{artifact.registration_name.upper()}.модульобъекта(1,1)}}: failure"
    )

    diagnostic = remap_worker_stage_diagnostic(
        parse_platform_diagnostic(raw),
        artifact_sha256=artifact.artifact_sha256,
        phase=phase,
        candidate_manifest_sha256=manifest,
        candidate_artifacts=(artifact,),
    )

    assert diagnostic.mapping_confidence is MappingConfidence.EXACT
    assert diagnostic.source_unit is not None
    assert diagnostic.source_unit.unit_id == "МодульБ"
    assert diagnostic.platform_diagnostic == raw


@pytest.mark.parametrize("phase", ("upload", "connect"))
def test_worker_stage_unknown_host_only_location_remains_unmapped(phase: str) -> None:
    manifest = "c" * 64
    artifact = _worker_diagnostic_artifact(
        "МодульБ",
        18,
        "OnecRuntime_bbbbbbbb_bbbbbbbbbbbbbbbb",
        "b" * 64,
        manifest,
    )
    raw = (
        "{<Неизвестный модуль>(1,1)}: host wrapper\n"
        " [ОшибкаКомпиляцииВстроенногоЯзыка]"
    )

    diagnostic = remap_worker_stage_diagnostic(
        parse_platform_diagnostic(raw),
        artifact_sha256=artifact.artifact_sha256,
        phase=phase,
        candidate_manifest_sha256=manifest,
        candidate_artifacts=(artifact,),
    )

    assert diagnostic.mapping_confidence is MappingConfidence.UNKNOWN
    assert diagnostic.code == "worker_artifact_location_unmapped"
    assert diagnostic.source_unit is None
    assert diagnostic.source_map_sha256 is None
    assert diagnostic.platform_diagnostic == raw


def test_worker_stage_does_not_parse_canonical_locator_beyond_prose_bound() -> None:
    manifest = "c" * 64
    artifact = _worker_diagnostic_artifact(
        "МодульБ",
        18,
        "OnecRuntime_bbbbbbbb_bbbbbbbbbbbbbbbb",
        "b" * 64,
        manifest,
    )
    raw = (
        "x" * (64 * 1024)
        + "\n{ВнешняяОбработка."
        + artifact.registration_name
        + ".МодульОбъекта(1,1)}: hidden"
    )

    diagnostic = remap_worker_stage_diagnostic(
        parse_platform_diagnostic(raw),
        artifact_sha256=artifact.artifact_sha256,
        phase="upload",
        candidate_manifest_sha256=manifest,
        candidate_artifacts=(artifact,),
    )

    assert diagnostic.mapping_confidence is MappingConfidence.UNKNOWN
    assert diagnostic.code == "worker_artifact_location_unmapped"
    assert diagnostic.platform_diagnostic_truncated
    assert diagnostic.platform_diagnostic == "x" * (64 * 1024)


def test_worker_stage_does_not_fallback_to_wrapper_or_wrong_worker_location() -> None:
    manifest = "c" * 64
    artifact = _worker_diagnostic_artifact(
        "МодульБ",
        18,
        "OnecRuntime_bbbbbbbb_bbbbbbbbbbbbbbbb",
        "b" * 64,
        manifest,
    )
    raw = (
        "{ОбщийМодуль.Host.Модуль(1,1)}: wrapper\n"
        "{ВнешняяОбработка.OnecRuntime_deadbeef_deadbeefdeadbeef."
        "МодульОбъекта(1,1)}: stale artifact"
    )

    diagnostic = remap_worker_stage_diagnostic(
        parse_platform_diagnostic(raw),
        artifact_sha256=artifact.artifact_sha256,
        phase="connect",
        candidate_manifest_sha256=manifest,
        candidate_artifacts=(artifact,),
    )

    assert diagnostic.mapping_confidence is MappingConfidence.UNKNOWN
    assert diagnostic.code == "worker_artifact_location_unmapped"
    assert diagnostic.source_unit is None
    assert diagnostic.source_map_sha256 is None
    assert diagnostic.platform_diagnostic == raw


def test_runtime_compound_worker_frames_preserve_known_and_unknown_order() -> None:
    manifest = "c" * 64
    module_a = _worker_diagnostic_artifact(
        "МодульА",
        17,
        "OnecRuntime_aaaaaaaa_aaaaaaaaaaaaaaaa",
        "a" * 64,
        manifest,
    )
    module_b = _worker_diagnostic_artifact(
        "МодульБ",
        18,
        "OnecRuntime_bbbbbbbb_bbbbbbbbbbbbbbbb",
        "b" * 64,
        manifest,
    )
    unknown = "OnecRuntime_deadbeef_deadbeefdeadbeef"
    raw = (
        "{ВнешняяОбработка."
        f"{module_b.registration_name}.МодульОбъекта(1,1)}}: callee\n"
        "{ВнешняяОбработка."
        f"{unknown}.МодульОбъекта(2,3)}}: unknown worker\n"
        "{<Неизвестный модуль>(4,5)}: platform frame\n"
        "{ВнешняяОбработка."
        f"{module_a.registration_name.upper()}.МодульОбъекта(1,1)}}: caller"
    )

    diagnostic = remap_worker_runtime_diagnostic(
        parse_platform_diagnostic(raw),
        pinned_manifest_sha256=manifest,
        pinned_artifacts=(module_a, module_b),
    )

    assert [frame.logical_name for frame in diagnostic.worker_frames] == [
        "МодульБ",
        None,
        None,
        "МодульА",
    ]
    assert [frame.registration_name for frame in diagnostic.worker_frames] == [
        module_b.registration_name,
        unknown,
        "<Неизвестный модуль>",
        module_a.registration_name.upper(),
    ]
    assert [frame.mapping_confidence for frame in diagnostic.worker_frames] == [
        MappingConfidence.EXACT,
        MappingConfidence.UNKNOWN,
        MappingConfidence.UNKNOWN,
        MappingConfidence.EXACT,
    ]
    assert [frame.logical_name for frame in diagnostic.frames] == [
        "МодульБ",
        None,
        None,
        "МодульА",
    ]
    assert [frame.platform_location.module_name for frame in diagnostic.frames] == [
        item.location.module_name for item in parse_platform_diagnostic(raw).frames
    ]
    assert diagnostic.platform_diagnostic == raw


def test_mixed_trace_maps_worker_main_and_native_frames_in_order() -> None:
    """Break caught: trace mapping must admit each pinned Worker separately."""
    from onec_runtime.runtime_contracts import sanitize_normalized_diagnostic

    manifest = "c" * 64
    worker = _worker_diagnostic_artifact(
        "МодульБ",
        18,
        "OnecRuntime_bbbbbbbb_bbbbbbbbbbbbbbbb",
        "b" * 64,
        manifest,
    )
    source = "Результат = 1;"
    raw = (
        f"{{ВнешняяОбработка.{worker.registration_name}.МодульОбъекта(2,1)}}: worker\n"
        "{ОбщийМодуль.Сервис.Модуль(7,3)}: native\n"
        "{<Неизвестный модуль>(1,1)}: main"
    )

    diagnostic = normalize_platform_diagnostic_trace(
        parse_platform_diagnostic(raw),
        stage=DiagnosticStage.EXECUTION,
        executed=_wrapped(source),
        visible_source_context=_visible_context(source),
        pinned_manifest_sha256=manifest,
        pinned_artifacts=(worker,),
    )

    assert [frame.origin for frame in diagnostic.frames] == [
        ErrorTraceFrameOrigin.WORKER_ARTIFACT,
        ErrorTraceFrameOrigin.NATIVE_MODULE,
        ErrorTraceFrameOrigin.EXECUTED_ARTIFACT,
    ]
    assert diagnostic.frames[0].logical_name == "МодульБ"
    assert diagnostic.frames[0].mapping_confidence is MappingConfidence.EXACT
    assert diagnostic.frames[1].mapping_confidence is MappingConfidence.UNKNOWN
    assert diagnostic.frames[2].mapping_confidence is MappingConfidence.EXACT
    assert sanitize_normalized_diagnostic(diagnostic) is not None


def test_stale_worker_frame_degrades_without_hiding_other_frames() -> None:
    """Break caught: stale Worker identity must not discard following frames."""
    manifest = "c" * 64
    worker = _worker_diagnostic_artifact(
        "МодульА",
        17,
        "OnecRuntime_aaaaaaaa_aaaaaaaaaaaaaaaa",
        "a" * 64,
        manifest,
    )
    stale = "OnecRuntime_deadbeef_deadbeefdeadbeef"
    raw = (
        f"{{ВнешняяОбработка.{stale}.МодульОбъекта(1,1)}}: stale\n"
        f"{{ВнешняяОбработка.{worker.registration_name}.МодульОбъекта(2,1)}}: current"
    )

    diagnostic = remap_worker_runtime_diagnostic(
        parse_platform_diagnostic(raw),
        pinned_manifest_sha256=manifest,
        pinned_artifacts=(worker,),
    )

    assert [frame.origin for frame in diagnostic.frames] == [
        ErrorTraceFrameOrigin.UNKNOWN,
        ErrorTraceFrameOrigin.WORKER_ARTIFACT,
    ]
    assert diagnostic.frames[0].registration_name == stale
    assert diagnostic.frames[0].mapping_confidence is MappingConfidence.UNKNOWN
    assert diagnostic.frames[1].mapping_confidence is MappingConfidence.EXACT


def test_one_worker_mapping_failure_does_not_remove_later_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: a single Worker mapper error must stay frame-local."""
    import onec_runtime.bsl.diagnostics as diagnostics_module

    manifest = "c" * 64
    broken = _worker_diagnostic_artifact(
        "Сломанный",
        1,
        "OnecRuntime_aaaaaaaa_aaaaaaaaaaaaaaaa",
        "a" * 64,
        manifest,
    )
    healthy = _worker_diagnostic_artifact(
        "Рабочий",
        2,
        "OnecRuntime_bbbbbbbb_bbbbbbbbbbbbbbbb",
        "b" * 64,
        manifest,
    )
    real_mapper = diagnostics_module._worker_runtime_frame

    def fail_selected_frame(location, artifact, observed_registration):
        if artifact.logical_name == "Сломанный":
            raise ValueError("injected per-frame failure")
        return real_mapper(location, artifact, observed_registration)

    monkeypatch.setattr(
        diagnostics_module,
        "_worker_runtime_frame",
        fail_selected_frame,
    )
    diagnostic = remap_worker_runtime_diagnostic(
        parse_platform_diagnostic(
            f"{{ВнешняяОбработка.{broken.registration_name}.МодульОбъекта(2,1)}}: broken\n"
            f"{{ВнешняяОбработка.{healthy.registration_name}.МодульОбъекта(2,1)}}: healthy"
        ),
        pinned_manifest_sha256=manifest,
        pinned_artifacts=(broken, healthy),
    )

    assert [frame.mapping_confidence for frame in diagnostic.frames] == [
        MappingConfidence.UNKNOWN,
        MappingConfidence.EXACT,
    ]


def test_native_trace_mapping_failure_degrades_without_hiding_main_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: native conversion errors must use the frame-local fallback."""
    import onec_runtime.bsl.diagnostics as diagnostics_module
    from onec_runtime.runtime_contracts import sanitize_normalized_diagnostic

    source = "Результат = 1;"
    monkeypatch.setattr(
        diagnostics_module,
        "_native_trace_frame",
        lambda frame: (_ for _ in ()).throw(ValueError("injected native failure")),
    )
    diagnostic = normalize_platform_diagnostic_trace(
        parse_platform_diagnostic(
            "{ОбщийМодуль.Сервис.Модуль(7,3)}: native\n"
            "{<Неизвестный модуль>(1,1)}: main"
        ),
        stage=DiagnosticStage.EXECUTION,
        executed=_wrapped(source),
        visible_source_context=_visible_context(source),
    )

    assert [frame.origin for frame in diagnostic.frames] == [
        ErrorTraceFrameOrigin.UNKNOWN,
        ErrorTraceFrameOrigin.EXECUTED_ARTIFACT,
    ]
    assert diagnostic.frames[0].mapping_confidence is MappingConfidence.UNKNOWN
    assert diagnostic.frames[1].source_unit is not None
    assert sanitize_normalized_diagnostic(diagnostic) is not None


def test_pinned_worker_trace_honors_explicit_stage_and_legacy_wrapper_execution() -> None:
    """Break caught: the Worker primary path previously hard-coded execution."""
    manifest = "c" * 64
    worker = _worker_diagnostic_artifact(
        "МодульБ",
        18,
        "OnecRuntime_bbbbbbbb_bbbbbbbbbbbbbbbb",
        "b" * 64,
        manifest,
    )
    parsed = parse_platform_diagnostic(
        "{ВнешняяОбработка."
        f"{worker.registration_name}.МодульОбъекта(1,1)}}: worker"
    )

    compilation = normalize_platform_diagnostic_trace(
        parsed,
        stage=DiagnosticStage.COMPILATION,
        pinned_manifest_sha256=manifest,
        pinned_artifacts=(worker,),
    )
    explicit_execution = normalize_platform_diagnostic_trace(
        parsed,
        stage=DiagnosticStage.EXECUTION,
        pinned_manifest_sha256=manifest,
        pinned_artifacts=(worker,),
    )
    legacy = remap_worker_runtime_diagnostic(
        parsed,
        pinned_manifest_sha256=manifest,
        pinned_artifacts=(worker,),
    )

    assert compilation.stage is DiagnosticStage.COMPILATION
    assert compilation.runtime_summary == "BSL compilation failed"
    assert compilation.diagnostic_id != explicit_execution.diagnostic_id
    assert legacy.stage is DiagnosticStage.EXECUTION
    assert legacy.runtime_summary == "BSL execution failed"
    assert legacy.diagnostic_id == explicit_execution.diagnostic_id


def test_sanitizer_keeps_pinned_worker_trace_beyond_legacy_location_cap() -> None:
    """Break caught: independent legacy retention cannot erase a valid trace frame."""
    from onec_runtime.runtime_contracts import sanitize_normalized_diagnostic

    manifest = "c" * 64
    worker = _worker_diagnostic_artifact(
        "ПозднийМодуль",
        18,
        "OnecRuntime_bbbbbbbb_bbbbbbbbbbbbbbbb",
        "b" * 64,
        manifest,
    )
    raw = (
        "{<Неизвестный модуль>(10000001,1)}: skipped\n" * 128
        + "{ВнешняяОбработка."
        + worker.registration_name
        + ".МодульОбъекта(1,1)}: retained"
    )

    parsed = parse_platform_diagnostic(raw)
    diagnostic = remap_worker_runtime_diagnostic(
        parsed,
        pinned_manifest_sha256=manifest,
        pinned_artifacts=(worker,),
    )

    assert len(parsed.locations) == 128
    assert len(parsed.frames) == 1
    assert diagnostic.frames[0].mapping_confidence is MappingConfidence.EXACT
    assert all(
        frame.mapping_confidence is MappingConfidence.UNKNOWN
        for frame in diagnostic.worker_frames
    )
    assert sanitize_normalized_diagnostic(diagnostic) is not None


def test_sanitizer_keeps_exact_trace_after_one_shot_legacy_worker_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: legacy fallback and trace mapping fail independently."""
    import onec_runtime.bsl.diagnostics as diagnostics_module
    from onec_runtime.runtime_contracts import sanitize_normalized_diagnostic

    manifest = "c" * 64
    worker = _worker_diagnostic_artifact(
        "МодульБ",
        18,
        "OnecRuntime_bbbbbbbb_bbbbbbbbbbbbbbbb",
        "b" * 64,
        manifest,
    )
    real_mapper = diagnostics_module._worker_runtime_frame
    calls = 0

    def fail_once(location, artifact, observed_registration):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("injected legacy-only failure")
        return real_mapper(location, artifact, observed_registration)

    monkeypatch.setattr(diagnostics_module, "_worker_runtime_frame", fail_once)
    diagnostic = remap_worker_runtime_diagnostic(
        parse_platform_diagnostic(
            "{ВнешняяОбработка."
            f"{worker.registration_name}.МодульОбъекта(1,1)}}: worker"
        ),
        pinned_manifest_sha256=manifest,
        pinned_artifacts=(worker,),
    )

    assert calls == 2
    assert diagnostic.worker_frames[0].mapping_confidence is MappingConfidence.UNKNOWN
    assert diagnostic.frames[0].mapping_confidence is MappingConfidence.EXACT
    assert sanitize_normalized_diagnostic(diagnostic) is not None


def test_runtime_canonical_line_only_worker_frames_map_exactly_in_order() -> None:
    manifest = "c" * 64
    source_a = "Функция А() Экспорт\n    Возврат 1;\nКонецФункции"
    source_b = "Функция Б() Экспорт\n    Возврат 2;\nКонецФункции"
    module_a = _worker_diagnostic_artifact(
        "МодульА",
        17,
        "OnecRuntime_aaaaaaaa_aaaaaaaaaaaaaaaa",
        "a" * 64,
        manifest,
        source=source_a,
    )
    module_b = _worker_diagnostic_artifact(
        "МодульБ",
        18,
        "OnecRuntime_bbbbbbbb_bbbbbbbbbbbbbbbb",
        "b" * 64,
        manifest,
        source=source_b,
    )
    raw = (
        "{ВнешняяОбработка."
        f"{module_b.registration_name}.МодульОбъекта(2)}}: callee\n"
        "{ВнешняяОбработка."
        f"{module_a.registration_name}.МодульОбъекта(2)}}: caller"
    )

    diagnostic = remap_worker_runtime_diagnostic(
        parse_platform_diagnostic(raw),
        pinned_manifest_sha256=manifest,
        pinned_artifacts=(module_a, module_b),
    )

    assert [frame.logical_name for frame in diagnostic.worker_frames] == [
        "МодульБ",
        "МодульА",
    ]
    assert [frame.mapping_confidence for frame in diagnostic.worker_frames] == [
        MappingConfidence.EXACT,
        MappingConfidence.EXACT,
    ]
    assert [frame.visible_location.span for frame in diagnostic.worker_frames] == [
        SourceSpan(source_b.index("Возврат"), source_b.index("Возврат") + 1),
        SourceSpan(source_a.index("Возврат"), source_a.index("Возврат") + 1),
    ]
    assert [frame.lowered_location.column for frame in diagnostic.worker_frames] == [
        5,
        5,
    ]


def test_line_only_parser_keeps_native_paths_without_worker_classification() -> None:
    registration = "OnecRuntime_aaaaaaaa_aaaaaaaaaaaaaaaa"
    raw = (
        "{ОбщийМодуль.RuntimeKernelServer.Модуль(12)}: host\n"
        "{Decorator.ВнешняяОбработка."
        f"{registration}.МодульОбъекта(2)}}: decorated"
    )

    parsed = parse_platform_diagnostic(raw)

    assert [item.line for item in parsed.locations] == [12, 2]
    assert all(
        item.worker_artifact_location is None for item in parsed.locations
    )


def test_line_only_worker_unknown_registration_never_maps_to_known_artifact() -> None:
    manifest = "c" * 64
    artifact = _worker_diagnostic_artifact(
        "МодульА",
        17,
        "OnecRuntime_aaaaaaaa_aaaaaaaaaaaaaaaa",
        "a" * 64,
        manifest,
    )
    raw = (
        "{ВнешняяОбработка."
        "OnecRuntime_deadbeef_deadbeefdeadbeef.МодульОбъекта(2)}: unknown"
    )

    diagnostic = remap_worker_runtime_diagnostic(
        parse_platform_diagnostic(raw),
        pinned_manifest_sha256=manifest,
        pinned_artifacts=(artifact,),
    )

    assert len(diagnostic.worker_frames) == 1
    assert diagnostic.worker_frames[0].logical_name is None
    assert diagnostic.worker_frames[0].mapping_confidence is MappingConfidence.UNKNOWN
    assert diagnostic.worker_frames[0].visible_location is None


def test_line_only_worker_mapping_rejects_multiple_exact_spans_on_one_line() -> None:
    manifest = "c" * 64
    registration = "OnecRuntime_aaaaaaaa_aaaaaaaaaaaaaaaa"
    visible_source = "Первый();Пропуск();Второй();"
    unit = SourceUnitRef(
        SourceUnitKind.MODULE,
        "МодульА",
        17,
        source_sha256(visible_source),
    )
    visible = mapped_visible_source(visible_source, unit)
    builder = SourceTransformBuilder(visible)
    builder.copy(SourceSpan(0, visible_source.index("Пропуск")))
    builder.copy(SourceSpan(visible_source.index("Второй"), len(visible_source)))
    mapped = builder.build(SourceArtifactKind.WORKER_PROJECTION)
    artifact = _worker_diagnostic_artifact_from_mapped(
        logical_name="МодульА",
        revision=17,
        registration_name=registration,
        artifact_sha256="a" * 64,
        manifest_sha256=manifest,
        mapped=mapped,
        unit=unit,
        visible_source=visible_source,
    )

    diagnostic = remap_worker_runtime_diagnostic(
        parse_platform_diagnostic(
            f"{{ВнешняяОбработка.{registration}.МодульОбъекта(1)}}: ambiguous"
        ),
        pinned_manifest_sha256=manifest,
        pinned_artifacts=(artifact,),
    )

    frame = diagnostic.worker_frames[0]
    assert frame.mapping_confidence is MappingConfidence.UNKNOWN
    assert frame.visible_location is None
    assert frame.lowered_location is None


@pytest.mark.parametrize("case", ("blank", "generated", "zero", "out-of-range"))
def test_line_only_worker_mapping_fails_closed_for_ambiguous_lines(case: str) -> None:
    manifest = "c" * 64
    registration = "OnecRuntime_aaaaaaaa_aaaaaaaaaaaaaaaa"
    if case == "generated":
        visible_source = "Якорь"
        unit = SourceUnitRef(
            SourceUnitKind.MODULE,
            "МодульА",
            17,
            source_sha256(visible_source),
        )
        visible = mapped_visible_source(visible_source, unit)
        builder = SourceTransformBuilder(visible)
        builder.synthetic("Runtime();", SourceSpan(0, 1), "runtime-only")
        mapped = builder.build(SourceArtifactKind.WORKER_PROJECTION)
        artifact = _worker_diagnostic_artifact_from_mapped(
            logical_name="МодульА",
            revision=17,
            registration_name=registration,
            artifact_sha256="a" * 64,
            manifest_sha256=manifest,
            mapped=mapped,
            unit=unit,
            visible_source=visible_source,
        )
        line = 1
    else:
        source = "\nВозврат 1;"
        artifact = _worker_diagnostic_artifact(
            "МодульА",
            17,
            registration,
            "a" * 64,
            manifest,
            source=source,
        )
        line = {"blank": 1, "zero": 0, "out-of-range": 99}[case]
    parsed = parse_platform_diagnostic(
        f"{{ВнешняяОбработка.{registration}.МодульОбъекта({line})}}: invalid"
    )

    if case == "zero":
        assert parsed.locations == ()
        return
    diagnostic = remap_worker_runtime_diagnostic(
        parsed,
        pinned_manifest_sha256=manifest,
        pinned_artifacts=(artifact,),
    )

    frame = diagnostic.worker_frames[0]
    assert frame.mapping_confidence is MappingConfidence.UNKNOWN
    assert frame.visible_location is None
    assert frame.lowered_location is None


def test_runtime_uses_only_canonical_worker_frames_and_preserves_unknown_order() -> None:
    manifest = "c" * 64
    module_a = _worker_diagnostic_artifact(
        "МодульА",
        17,
        "OnecRuntime_aaaaaaaa_aaaaaaaaaaaaaaaa",
        "a" * 64,
        manifest,
    )
    module_b = _worker_diagnostic_artifact(
        "МодульБ",
        18,
        "OnecRuntime_bbbbbbbb_bbbbbbbbbbbbbbbb",
        "b" * 64,
        manifest,
    )
    unknown = "OnecRuntime_deadbeef_deadbeefdeadbeef"
    raw = (
        "{ВнешняяОбработка."
        f"{module_b.registration_name}.МодульОбъекта(1,1)}}: callee\n"
        "{Decorator.ВнешняяОбработка."
        f"{module_a.registration_name}.МодульОбъекта(2,2)}}: not a frame\n"
        f"{{{module_a.registration_name}(3,3)}}: bare not a frame\n"
        "{ВнешняяОбработка."
        f"{unknown}.МодульОбъекта(4,4)}}: unknown\n"
        "{ВНЕШНЯЯОБРАБОТКА."
        f"{module_a.registration_name.upper()}.модульобъекта(1,1)}}: caller"
    )

    diagnostic = remap_worker_runtime_diagnostic(
        parse_platform_diagnostic(raw),
        pinned_manifest_sha256=manifest,
        pinned_artifacts=(module_a, module_b),
    )

    assert [frame.logical_name for frame in diagnostic.worker_frames] == [
        "МодульБ",
        None,
        "МодульА",
    ]
    assert [frame.registration_name for frame in diagnostic.worker_frames] == [
        module_b.registration_name,
        unknown,
        module_a.registration_name.upper(),
    ]
    assert [frame.mapping_confidence for frame in diagnostic.worker_frames] == [
        MappingConfidence.EXACT,
        MappingConfidence.UNKNOWN,
        MappingConfidence.EXACT,
    ]
    assert diagnostic.platform_diagnostic == raw


def test_platform_locator_parsing_is_bounded_without_partial_nearest_match() -> None:
    oversized = ".".join(["Wrapper"] * 80)
    raw = f"{{{oversized}(1,1)}}: oversized"

    parsed = parse_platform_diagnostic(raw)

    assert parsed.locations == ()
    assert parsed.module_name is None
    assert parsed.platform_diagnostic == raw
