# Full BSL Error Diagnostics Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a bounded immutable core representation of the complete 1C detailed error, including causes and a source-mapped mixed stack, without touching runtime execution or adapters.

**Architecture:** Extend the existing conservative parser in `bsl/diagnostics.py`, retain one private 64 KiB UTF-8 evidence string, and represent causes/frames by spans into that string. Add one pure normalization entry point that maps main and pinned Worker frames independently while preserving every legacy primary field and `worker_frames`; validate the richer object fail-closed in `runtime_contracts.py` while keeping current wire schemas unchanged.

**Tech Stack:** Python 3.12+, frozen/slotted dataclasses, regular expressions, existing BSL `MappedSource`/`SourceMap`, pytest, uv.

**Spec:** `docs/superpowers/specs/2026-09-15-bsl-error-diagnostics-design.md`

## Global Constraints

- Work only in `src/onec_runtime/bsl/diagnostics.py`, `src/onec_runtime/runtime_contracts.py`, `tests/unit/test_bsl_diagnostics.py`, `tests/unit/test_bsl_diagnostic_acceptance.py`, and `tests/unit/test_runtime_contract_boundaries.py`.
- Do not modify `prototype_runtime.py`, `runtime_api.py`, `session.py`, `errors.py`, Jupyter, MCP, VS Code, configuration resolution, RDBG, or the 1C extension.
- Retain at most 64 KiB of UTF-8 diagnostic text, 128 frames, and 32 causes; report each truncation independently.
- Preserve raw-detail SHA-256 over the complete original UTF-8 input.
- Preserve existing diagnostic IDs, entrypoint-specific primary selection, `locations`, `worker_frames`, and public/expert wire key sets for existing inputs.
- Native 1C configuration locations are direct coordinates and require neither a source map nor `source_root`.
- Source excerpts are represented only by hash-fenced source spans; no BSL source string or path enters a public artifact.
- Compact/public Jupyter and MCP paths never emit platform diagnostic prose; diagnostic/expert/private paths preserve it verbatim within the shared 64 KiB UTF-8 bound.
- Each generated frame maps only through the exact `MappedSource` or immutable Worker manifest/artifact evidence supplied by the caller.
- A malformed/unmappable frame degrades independently and cannot remove other frames.
- This tranche adds no runtime wiring, so successful and failed execution behavior remains unchanged.
- Run tests as `uv run python -m pytest`; plain `uv run pytest` does not put the repository root on `sys.path` in this Windows checkout.
- Live 1C qualification is excluded; static tests must not be described as live qualification.

---

### Task 1: UTF-8 Evidence Bound and Canonical Line-Only Locations

**Files:**
- Modify: `src/onec_runtime/bsl/diagnostics.py:22-46,306-409`
- Modify: `src/onec_runtime/runtime_contracts.py`
- Modify: `src/onec_runtime/privacy.py`
- Modify: `packages/mcp/src/onec_runtime_mcp/agent/operations.py`
- Test: `tests/unit/test_bsl_diagnostics.py:134-312,1072-1099,1237-1273`
- Test: directly affected runtime-contract, privacy, and MCP diagnostic tests

**Interfaces:**
- Consumes: existing `ParsedPlatformDiagnostic`, `PlatformDiagnosticLocation`, `_parse_worker_artifact_location`.
- Produces: `_PLATFORM_DIAGNOSTIC_LIMIT_BYTES = 64 * 1024`, `_bound_platform_diagnostic(message: str) -> tuple[str, bool]`, one ordered `_accepted_platform_locations(text: str)` result reused by Task 2, and one 64 KiB UTF-8 verbatim diagnostic bound across core, private MCP storage, and expert output.

- [ ] **Step 1: Write failing UTF-8 and line-only parser tests**

Replace the current 4,096-code-point test and add direct native/unknown line-only coverage:

```python
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
```

Change `test_worker_stage_does_not_parse_canonical_locator_beyond_prose_bound` so its prefix is `"x" * (64 * 1024)` and its expected retained text is exactly that prefix.

Replace `test_line_only_worker_parser_rejects_host_and_decorated_shapes` with:

```python
def test_line_only_parser_keeps_native_paths_without_worker_classification() -> None:
    registration = "OnecRuntime_aaaaaaaa_aaaaaaaaaaaaaaaa"
    parsed = parse_platform_diagnostic(
        "{ОбщийМодуль.RuntimeKernelServer.Модуль(12)}: host\n"
        "{Decorator.ВнешняяОбработка."
        f"{registration}.МодульОбъекта(2)}}: decorated"
    )

    assert [item.line for item in parsed.locations] == [12, 2]
    assert all(
        item.worker_artifact_location is None for item in parsed.locations
    )
```

This is additive general line-only parsing; only the exact three-component `ВнешняяОбработка.OnecRuntime_<id>.МодульОбъекта` shape receives Worker classification.

- [ ] **Step 2: Run the new tests and verify the old behavior fails**

Run:

```powershell
uv run python -m pytest tests/unit/test_bsl_diagnostics.py -k "utf8_byte_bounded or canonical_line_only_native or line_only_parser_rejects or beyond_prose_bound or line_only_worker_parser" -v
```

Expected: the byte-bound and native/unknown line-only assertions fail because the parser still slices 4,096 code points and accepts line-only locations only for canonical Worker modules.

- [ ] **Step 3: Implement byte-safe retention and one canonical locator scanner**

Replace the two location regexes with one optional-column form and retain the match offsets internally:

```python
_PLATFORM_DIAGNOSTIC_LIMIT_BYTES = 64 * 1024
_PLATFORM_FRAME_LIMIT = 128
_PLATFORM_CAUSE_LIMIT = 32
_PLATFORM_COORDINATE_LIMIT = 10_000_000
_LOCATION_RE = re.compile(
    rf"^\{{(?P<module><Неизвестный модуль>|Неизвестный модуль|"
    rf"{_MODULE_IDENTIFIER}(?:\.{_MODULE_IDENTIFIER})*)"
    r"\((?P<line>[0-9]{1,10})"
    r"(?:\s*,\s*(?P<column>[0-9]{1,10}))?\)\}",
    re.MULTILINE,
)
_WORKER_REGISTRATION_RE = re.compile(
    r"OnecRuntime_[0-9a-f]{8}_[0-9a-f]{16}\Z",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class _AcceptedPlatformLocation:
    start: int
    end: int
    location: PlatformDiagnosticLocation


def _bound_platform_diagnostic(message: str) -> tuple[str, bool]:
    encoded = message.encode("utf-8")
    if len(encoded) <= _PLATFORM_DIAGNOSTIC_LIMIT_BYTES:
        return message, False
    return (
        encoded[:_PLATFORM_DIAGNOSTIC_LIMIT_BYTES].decode("utf-8", errors="ignore"),
        True,
    )


def _accepted_platform_locations(
    text: str,
) -> tuple[_AcceptedPlatformLocation, ...]:
    accepted: list[_AcceptedPlatformLocation] = []
    for match in _LOCATION_RE.finditer(text):
        module = match.group("module")
        components = (
            (module,) if module in _UNKNOWN_MODULES else tuple(module.split("."))
        )
        line = int(match.group("line"))
        column_text = match.group("column")
        column = None if column_text is None else int(column_text)
        if (
            len(module) > _MODULE_LOCATOR_LIMIT
            or len(components) > _MODULE_COMPONENT_LIMIT
            or (column is None and line <= 0)
        ):
            continue
        accepted.append(
            _AcceptedPlatformLocation(
                match.start(),
                match.end(),
                PlatformDiagnosticLocation(
                    module,
                    components,
                    _parse_worker_artifact_location(components),
                    line,
                    column,
                    (
                        DiagnosticCoordinateSpace.EXECUTED_BSL
                        if module in _UNKNOWN_MODULES
                        else DiagnosticCoordinateSpace.HOST_MODULE
                    ),
                ),
            )
        )
    return tuple(accepted)
```

Tighten `_parse_worker_artifact_location` at the same time so both column and line-only forms share one exact classification rule:

```python
def _parse_worker_artifact_location(
    components: tuple[str, ...],
) -> WorkerArtifactPlatformLocation | None:
    if (
        len(components) == 3
        and components[0].casefold() == "внешняяобработка"
        and _WORKER_REGISTRATION_RE.fullmatch(components[1]) is not None
        and components[2].casefold() == "модульобъекта"
    ):
        return WorkerArtifactPlatformLocation(components[1])
    return None
```

Place `_AcceptedPlatformLocation` after `PlatformDiagnosticLocation`. In `parse_platform_diagnostic`, call `_bound_platform_diagnostic`, scan once with `_accepted_platform_locations`, and use this legacy-primary selection:

```python
bounded, text_truncated = _bound_platform_diagnostic(message)
accepted = _accepted_platform_locations(bounded)
column_locations = tuple(
    item for item in accepted if item.location.column is not None
)
terminal_compilation = _COMPILATION_MARKER_RE.search(bounded) is not None
primary = (
    column_locations[0]
    if column_locations and column_locations[0].start == 0
    else None
)
if (
    primary is None
    and terminal_compilation
    and len(column_locations) == 1
    and (
        column_locations[0].location.coordinate_space
        is DiagnosticCoordinateSpace.EXECUTED_BSL
        or column_locations[0].location.worker_artifact_location is not None
    )
    and _NESTED_COMPILE_CAUSE_PREFIX_RE.search(
        bounded[: column_locations[0].start]
    )
    is not None
):
    primary = column_locations[0]
```

If `primary` is absent, keep all legacy primary fields empty. Otherwise populate them from `primary.location`, and populate `additional_locations` from `column_locations[1:]`. Retain only `accepted[:128]` in legacy `locations`. Set `platform_diagnostic_truncated=text_truncated` and keep `has_compilation_marker=primary is not None and terminal_compilation`.

- [ ] **Step 4: Run the complete diagnostic test module**

Run:

```powershell
uv run python -m pytest tests/unit/test_bsl_diagnostics.py -v
```

Expected: PASS, including the updated 64 KiB boundary tests and all existing primary/Worker parsing tests.

- [ ] **Step 5: Commit the bounded scanner**

```powershell
git add src/onec_runtime/bsl/diagnostics.py tests/unit/test_bsl_diagnostics.py
git commit -m "feat: retain bounded detailed BSL diagnostics"
```

---

### Task 2: Structured Causes, Frames, and Opaque Text

**Files:**
- Modify: `src/onec_runtime/bsl/diagnostics.py:69-188,306-409`
- Test: `tests/unit/test_bsl_diagnostics.py:134-341`

**Interfaces:**
- Consumes: `_AcceptedPlatformLocation` and `_accepted_platform_locations(text)` from Task 1.
- Produces: `DiagnosticTextSpan`, `ParsedDiagnosticCause`, `ParsedDiagnosticFrame`, and the `causes`, `frames`, `opaque_spans`, `frames_truncated`, `causes_truncated` fields on `ParsedPlatformDiagnostic`.

- [ ] **Step 1: Write failing cause/frame structure tests**

Import the new types directly from `onec_runtime.bsl.diagnostics` and add:

```python
def _diagnostic_text(raw: str, span: DiagnosticTextSpan | None) -> str | None:
    return None if span is None else raw[span.start:span.end]


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

    opaque = "".join(raw[span.start:span.end] for span in parsed.opaque_spans)
    assert "{not a module label(7,4)}: opaque" in opaque
```

Keep the existing private-evidence `asdict`/JSON redaction test unchanged.

- [ ] **Step 2: Run the structure tests and verify missing fields fail**

Run:

```powershell
uv run python -m pytest tests/unit/test_bsl_diagnostics.py -k "ordered_causes or nonstructural_cause or limits_are_independent or opaque" -v
```

Expected: collection or assertion failure because the parsed structure types and fields do not exist.

- [ ] **Step 3: Add immutable parsed types and conservative span helpers**

Add these types before `ParsedPlatformDiagnostic`:

```python
@dataclass(frozen=True, slots=True)
class DiagnosticTextSpan:
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class ParsedDiagnosticCause:
    ordinal: int
    summary_span: DiagnosticTextSpan
    block_span: DiagnosticTextSpan
    frame_ordinals: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ParsedDiagnosticFrame:
    ordinal: int
    cause_ordinal: int | None
    location: PlatformDiagnosticLocation
    block_span: DiagnosticTextSpan
    detail_span: DiagnosticTextSpan | None
```

Append defaulted fields to `ParsedPlatformDiagnostic` so deterministic positional behavior before those fields is preserved:

```python
    causes: tuple[ParsedDiagnosticCause, ...] = ()
    frames: tuple[ParsedDiagnosticFrame, ...] = ()
    opaque_spans: tuple[DiagnosticTextSpan, ...] = ()
    frames_truncated: bool = False
    causes_truncated: bool = False
```

Add a strict structural marker and helpers:

```python
_CAUSE_BOUNDARY_RE = re.compile(
    r"^[ \t]*по причине:[ \t]*(?:\r?\n|\Z)",
    re.IGNORECASE | re.MULTILINE,
)
_DIAGNOSTIC_BLOCK_START_RE = re.compile(r"^\{", re.MULTILINE)


def _trim_diagnostic_span(text: str, start: int, end: int) -> DiagnosticTextSpan:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return DiagnosticTextSpan(start, end)


def _frame_detail_span(
    text: str,
    locator_end: int,
    block_end: int,
) -> DiagnosticTextSpan | None:
    start = locator_end
    if start < block_end and text[start] == ":":
        start += 1
    span = _trim_diagnostic_span(text, start, block_end)
    return None if span.start == span.end else span


def _complement_spans(
    text_length: int,
    consumed: tuple[DiagnosticTextSpan, ...],
) -> tuple[DiagnosticTextSpan, ...]:
    merged: list[DiagnosticTextSpan] = []
    for span in sorted(consumed, key=lambda item: (item.start, item.end)):
        if span.start == span.end:
            continue
        if merged and span.start <= merged[-1].end:
            merged[-1] = DiagnosticTextSpan(
                merged[-1].start,
                max(merged[-1].end, span.end),
            )
        else:
            merged.append(span)
    opaque: list[DiagnosticTextSpan] = []
    cursor = 0
    for span in merged:
        if cursor < span.start:
            opaque.append(DiagnosticTextSpan(cursor, span.start))
        cursor = max(cursor, span.end)
    if cursor < text_length:
        opaque.append(DiagnosticTextSpan(cursor, text_length))
    return tuple(opaque)
```

Implement `_parse_diagnostic_structure(text, accepted)` with these exact rules and body:

1. Build cause blocks from `[0, first_marker.start)`, each marker end to the next marker start, and the last marker end to `len(text)`; an empty input yields no causes.
2. Keep the first 32 cause blocks and set `causes_truncated` when more exist.
3. Select locations whose numeric values do not exceed `_PLATFORM_COORDINATE_LIMIT`, then keep the first 128; line/column zero in the legacy two-coordinate form is retained as observed platform evidence but can never map to source. Legacy primary fields still use Task 1's accepted entries so their old behavior does not change.
4. Assign a retained frame to the containing retained cause; frames in causes beyond 32 get `cause_ordinal=None`.
5. End each frame block at the next line-starting braced diagnostic block or its cause block end, whichever comes first. An unrecognized braced block is therefore preserved as opaque text rather than absorbed into the preceding frame detail.
6. Set each cause summary to the trimmed text before its first accepted locator, or its entire trimmed block when it has no locator.
7. Compute opaque spans as the complement of cause-marker spans, retained non-empty summary spans, and retained frame blocks.

```python
def _trace_location_is_bounded(location: PlatformDiagnosticLocation) -> bool:
    return (
        0 <= location.line <= _PLATFORM_COORDINATE_LIMIT
        and (
            location.column is None
            or 0 <= location.column <= _PLATFORM_COORDINATE_LIMIT
        )
    )


def _parse_diagnostic_structure(
    text: str,
    accepted: tuple[_AcceptedPlatformLocation, ...],
) -> tuple[
    tuple[ParsedDiagnosticCause, ...],
    tuple[ParsedDiagnosticFrame, ...],
    tuple[DiagnosticTextSpan, ...],
    bool,
    bool,
]:
    markers = tuple(_CAUSE_BOUNDARY_RE.finditer(text))
    if not text:
        cause_blocks: tuple[DiagnosticTextSpan, ...] = ()
    else:
        starts = (0, *(match.end() for match in markers))
        ends = (*(match.start() for match in markers), len(text))
        cause_blocks = tuple(
            DiagnosticTextSpan(start, end)
            for start, end in zip(starts, ends, strict=True)
        )
    retained_cause_blocks = cause_blocks[:_PLATFORM_CAUSE_LIMIT]
    candidates = tuple(
        item for item in accepted if _trace_location_is_bounded(item.location)
    )
    retained_candidates = candidates[:_PLATFORM_FRAME_LIMIT]
    diagnostic_block_starts = tuple(
        match.start() for match in _DIAGNOSTIC_BLOCK_START_RE.finditer(text)
    )
    frames: list[ParsedDiagnosticFrame] = []
    for ordinal, item in enumerate(retained_candidates):
        cause_index = next(
            (
                index
                for index, block in enumerate(cause_blocks)
                if block.start <= item.start < block.end
            ),
            None,
        )
        cause_end = (
            len(text) if cause_index is None else cause_blocks[cause_index].end
        )
        next_block_start = next(
            (start for start in diagnostic_block_starts if start > item.start),
            cause_end,
        )
        block_end = min(next_block_start, cause_end)
        frames.append(
            ParsedDiagnosticFrame(
                ordinal,
                (
                    cause_index
                    if cause_index is not None
                    and cause_index < len(retained_cause_blocks)
                    else None
                ),
                item.location,
                DiagnosticTextSpan(item.start, block_end),
                _frame_detail_span(text, item.end, block_end),
            )
        )
    causes: list[ParsedDiagnosticCause] = []
    for ordinal, block in enumerate(retained_cause_blocks):
        first_locator = next(
            (item.start for item in accepted if block.start <= item.start < block.end),
            block.end,
        )
        causes.append(
            ParsedDiagnosticCause(
                ordinal,
                _trim_diagnostic_span(text, block.start, first_locator),
                block,
                tuple(
                    frame.ordinal
                    for frame in frames
                    if frame.cause_ordinal == ordinal
                ),
            )
        )
    consumed = (
        *(DiagnosticTextSpan(match.start(), match.end()) for match in markers),
        *(cause.summary_span for cause in causes),
        *(frame.block_span for frame in frames),
    )
    return (
        tuple(causes),
        tuple(frames),
        _complement_spans(len(text), tuple(consumed)),
        len(candidates) > _PLATFORM_FRAME_LIMIT,
        len(cause_blocks) > _PLATFORM_CAUSE_LIMIT,
    )
```

Pass those five values by keyword into `ParsedPlatformDiagnostic`; build legacy `locations` from the same first 128 accepted entries.

- [ ] **Step 4: Run parser and privacy regressions**

Run:

```powershell
uv run python -m pytest tests/unit/test_bsl_diagnostics.py -k "parse or diagnostic_is or private_platform or compound_platform_locator or line_only" -v
```

Expected: PASS. Verify `asdict()` still replaces `_PrivatePlatformEvidence` with the redacted sentinel and never exposes detail text through the new span objects.

- [ ] **Step 5: Commit the structured parser**

```powershell
git add src/onec_runtime/bsl/diagnostics.py tests/unit/test_bsl_diagnostics.py
git commit -m "feat: parse BSL error causes and frames"
```

---

### Task 3: Main-Cell and Native Trace Normalization

**Files:**
- Modify: `src/onec_runtime/bsl/diagnostics.py:235-290,595-778,830-873`
- Test: `tests/unit/test_bsl_diagnostics.py:342-480`

**Interfaces:**
- Consumes: parsed causes/frames from Task 2, existing `_map_executed_offset`, `PlatformCoordinateCodec`, `VisibleSourceContext.line_range`, and existing primary diagnostic construction.
- Produces: `ErrorTraceFrameOrigin`, `ErrorTraceCause`, `ErrorTraceFrame`, `normalize_platform_diagnostic_trace(...)`, and populated `NormalizedDiagnostic.causes/frames` for main and native locations.

- [ ] **Step 1: Write failing main/native trace tests**

Add imports for the new trace types/function and these tests:

```python
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
```

- [ ] **Step 2: Run main/native trace tests and verify they fail**

Run:

```powershell
uv run python -m pytest tests/unit/test_bsl_diagnostics.py -k "main_trace or native_trace or trace_order or line_only_main" -v
```

Expected: import or attribute failures because normalized trace types and fields do not exist.

- [ ] **Step 3: Add normalized trace types**

Add after `LoweredSourceLocation`:

```python
class ErrorTraceFrameOrigin(StrEnum):
    EXECUTED_ARTIFACT = "executed_artifact"
    WORKER_ARTIFACT = "worker_artifact"
    NATIVE_MODULE = "native_module"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ErrorTraceCause:
    ordinal: int
    summary_span: DiagnosticTextSpan
    block_span: DiagnosticTextSpan
    frame_ordinals: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ErrorTraceFrame:
    ordinal: int
    cause_ordinal: int | None
    origin: ErrorTraceFrameOrigin
    platform_location: PlatformDiagnosticLocation
    block_span: DiagnosticTextSpan
    detail_span: DiagnosticTextSpan | None
    mapping_confidence: MappingConfidence
    registration_name: str | None = None
    logical_name: str | None = None
    revision: int | None = None
    artifact_sha256: str | None = None
    source_unit: SourceUnitRef | None = None
    visible_location: VisibleSourceLocation | None = None
    visible_line_span: SourceSpan | None = None
    related_visible_span: SourceSpan | None = None
    lowered_location: LoweredSourceLocation | None = None
    synthetic_region: str | None = None
    dependency_anchor: SourceSpan | None = None
    method_anchor: SourceSpan | None = None
```

Append to `NormalizedDiagnostic` after `worker_frames`:

```python
    causes: tuple[ErrorTraceCause, ...] = ()
    frames: tuple[ErrorTraceFrame, ...] = ()
    frames_truncated: bool = False
    causes_truncated: bool = False
```

- [ ] **Step 4: Implement the pure main/native normalization path**

Rename the current `remap_platform_diagnostic` body to `_remap_platform_primary` without changing its code or ID calculation. Add a wrapper that calls the new pure function.

Rename `_line_only_worker_position` to `_line_only_mapped_position` and use it for both main and Worker artifacts. Add:

```python
def _visible_line_span(
    mapping: _MappedDiagnosticOffset,
    context: VisibleSourceContext | None,
) -> SourceSpan | None:
    if mapping.visible is None or context is None:
        return None
    return context.line_range(mapping.visible.source_unit, mapping.visible.line)


def _main_trace_frame(
    frame: ParsedDiagnosticFrame,
    executed: MappedSource,
    context: VisibleSourceContext | None,
) -> ErrorTraceFrame:
    location = frame.location
    if location.column is None:
        position = _line_only_mapped_position(executed, location.line)
        column = None if position is None else position[0]
        offset = None if position is None else position[1]
    else:
        column = location.column
        offset = PlatformCoordinateCodec(executed.text).to_offset(
            location.line,
            location.column,
        )
    mapping = _map_executed_offset(executed, offset, context)
    lowered = None
    if offset is not None and column is not None:
        width = 0 if offset == len(executed.text) else 1
        lowered = LoweredSourceLocation(
            location.line,
            column,
            offset,
            SourceSpan(offset, offset + width),
        )
    return ErrorTraceFrame(
        ordinal=frame.ordinal,
        cause_ordinal=frame.cause_ordinal,
        origin=ErrorTraceFrameOrigin.EXECUTED_ARTIFACT,
        platform_location=location,
        block_span=frame.block_span,
        detail_span=frame.detail_span,
        mapping_confidence=mapping.confidence,
        source_unit=mapping.source_unit,
        visible_location=mapping.visible,
        visible_line_span=_visible_line_span(mapping, context),
        related_visible_span=mapping.related,
        lowered_location=lowered,
        synthetic_region=mapping.synthetic_region,
    )
```

Add `_native_trace_frame` that copies ordinal, cause, location, and diagnostic spans; sets origin to `NATIVE_MODULE`; and sets mapping confidence to `UNKNOWN`. Add `_unknown_trace_frame` with the same fields and `UNKNOWN` origin.

```python
def _native_trace_frame(frame: ParsedDiagnosticFrame) -> ErrorTraceFrame:
    return ErrorTraceFrame(
        ordinal=frame.ordinal,
        cause_ordinal=frame.cause_ordinal,
        origin=ErrorTraceFrameOrigin.NATIVE_MODULE,
        platform_location=frame.location,
        block_span=frame.block_span,
        detail_span=frame.detail_span,
        mapping_confidence=MappingConfidence.UNKNOWN,
    )


def _unknown_trace_frame(
    frame: ParsedDiagnosticFrame,
    *,
    registration_name: str | None = None,
) -> ErrorTraceFrame:
    return ErrorTraceFrame(
        ordinal=frame.ordinal,
        cause_ordinal=frame.cause_ordinal,
        origin=ErrorTraceFrameOrigin.UNKNOWN,
        platform_location=frame.location,
        block_span=frame.block_span,
        detail_span=frame.detail_span,
        mapping_confidence=MappingConfidence.UNKNOWN,
        registration_name=registration_name,
    )


def _generic_trace_base(
    parsed: ParsedPlatformDiagnostic,
    stage: DiagnosticStage,
) -> NormalizedDiagnostic:
    effective_stage = (
        DiagnosticStage.COMPILATION if parsed.has_compilation_marker else stage
    )
    diagnostic_id = sha256(
        "|".join(
            (
                effective_stage.value,
                "platform_trace",
                parsed.platform_diagnostic_sha256,
            )
        ).encode("utf-8")
    ).hexdigest()
    return NormalizedDiagnostic(
        diagnostic_id=diagnostic_id,
        runtime_summary=_summary(effective_stage),
        stage=effective_stage,
        mapping_confidence=MappingConfidence.UNKNOWN,
        _platform_evidence=parsed._platform_evidence,
        platform_diagnostic_sha256=parsed.platform_diagnostic_sha256,
        platform_diagnostic_truncated=parsed.platform_diagnostic_truncated,
        platform_diagnostic_redacted=parsed.platform_diagnostic_redacted,
    )
```

Implement the public-in-module function with the exact signature from the spec:

```python
def normalize_platform_diagnostic_trace(
    parsed: ParsedPlatformDiagnostic,
    *,
    stage: DiagnosticStage,
    executed: MappedSource | None = None,
    visible_source_context: VisibleSourceContext | None = None,
    pinned_manifest_sha256: str | None = None,
    pinned_artifacts: tuple[WorkerDiagnosticArtifact, ...] = (),
) -> NormalizedDiagnostic:
```

Validate all supplied optional types. When `executed` exists, create the base with `_remap_platform_primary`; otherwise create a generic base whose ID hashes `stage.value`, `"platform_trace"`, and `parsed.platform_diagnostic_sha256`. Convert parsed causes directly. For each parsed frame:

- unknown module plus `executed` → `_main_trace_frame`;
- ordinary non-Worker module → `_native_trace_frame`;
- canonical Worker or unknown module without exact mapping evidence → `_unknown_trace_frame`.

Wrap each frame conversion in `try/except BaseException` and substitute `_unknown_trace_frame` on failure. Finish with the exact `replace` call in the concrete body below.

Use this concrete body before Task 4 adds pinned Worker mapping:

```python
    if not isinstance(parsed, ParsedPlatformDiagnostic):
        raise ValueError("parsed must be a ParsedPlatformDiagnostic")
    if type(stage) is not DiagnosticStage:
        raise ValueError("stage must be a DiagnosticStage")
    if executed is not None and not isinstance(executed, MappedSource):
        raise ValueError("executed must be a MappedSource or None")
    if visible_source_context is not None and not isinstance(
        visible_source_context,
        VisibleSourceContext,
    ):
        raise ValueError("visible_source_context must be a VisibleSourceContext")
    if executed is None and visible_source_context is not None:
        raise ValueError("visible source context requires an executed artifact")
    if pinned_manifest_sha256 is None:
        if pinned_artifacts:
            raise ValueError("pinned artifacts require a manifest identity")
    else:
        _validate_worker_diagnostic_request(
            parsed,
            pinned_manifest_sha256,
            pinned_artifacts,
        )
    base = (
        _remap_platform_primary(
            parsed,
            executed,
            stage=stage,
            visible_source_context=visible_source_context,
        )
        if executed is not None
        else _generic_trace_base(parsed, stage)
    )
    causes = tuple(
        ErrorTraceCause(
            item.ordinal,
            item.summary_span,
            item.block_span,
            item.frame_ordinals,
        )
        for item in parsed.causes
    )
    frames: list[ErrorTraceFrame] = []
    for item in parsed.frames:
        try:
            if item.location.module_name in _UNKNOWN_MODULES and executed is not None:
                normalized = _main_trace_frame(
                    item,
                    executed,
                    visible_source_context,
                )
            elif item.location.worker_artifact_location is not None:
                normalized = _unknown_trace_frame(
                    item,
                    registration_name=(
                        item.location.worker_artifact_location.registration_name
                    ),
                )
            elif item.location.module_name in _UNKNOWN_MODULES:
                normalized = _unknown_trace_frame(item)
            else:
                normalized = _native_trace_frame(item)
        except BaseException:
            normalized = _unknown_trace_frame(item)
        frames.append(normalized)
    return replace(
        base,
        causes=causes,
        frames=tuple(frames),
        frames_truncated=parsed.frames_truncated,
        causes_truncated=parsed.causes_truncated,
    )
```

The `remap_platform_diagnostic` wrapper passes `executed` and `visible_source_context`. Do not export the new types/function through `bsl/__init__.py` in this tranche.

- [ ] **Step 5: Run focused and complete diagnostic tests**

Run:

```powershell
uv run python -m pytest tests/unit/test_bsl_diagnostics.py -k "trace or exact_location or wrapper_location or line_only" -v
uv run python -m pytest tests/unit/test_bsl_diagnostics.py -v
```

Expected: both commands PASS; existing diagnostic IDs and primary fields remain unchanged.

- [ ] **Step 6: Commit main/native trace normalization**

```powershell
git add src/onec_runtime/bsl/diagnostics.py tests/unit/test_bsl_diagnostics.py
git commit -m "feat: normalize complete main BSL error traces"
```

---

### Task 4: Pinned Worker and Mixed Stack Normalization

**Files:**
- Modify: `src/onec_runtime/bsl/diagnostics.py:412-594,778-873`
- Test: `tests/unit/test_bsl_diagnostics.py:760-1423`
- Test: `tests/unit/test_bsl_diagnostic_acceptance.py:1130-1240`

**Interfaces:**
- Consumes: `normalize_platform_diagnostic_trace`, `ErrorTraceFrame`, `_worker_runtime_frame`, and immutable `WorkerDiagnosticArtifact` evidence.
- Produces: per-frame Worker mapping inside `NormalizedDiagnostic.frames`, exact legacy `worker_frames` projection, and a delegated `remap_worker_runtime_diagnostic`.

- [ ] **Step 1: Write failing mixed-stack and frame-isolation tests**

Add:

```python
def test_mixed_trace_maps_worker_main_and_native_frames_in_order() -> None:
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


def test_stale_worker_frame_degrades_without_hiding_other_frames() -> None:
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
```

Extend `test_runtime_compound_worker_frames_preserve_known_and_unknown_order` with:

```python
assert [frame.logical_name for frame in diagnostic.frames] == [
    "МодульБ",
    None,
    None,
    "МодульА",
]
assert [frame.platform_location.module_name for frame in diagnostic.frames] == [
    item.location.module_name for item in parse_platform_diagnostic(raw).frames
]
```

- [ ] **Step 2: Run Worker trace tests and verify they fail**

Run:

```powershell
uv run python -m pytest tests/unit/test_bsl_diagnostics.py -k "mixed_trace or stale_worker_frame or compound_worker_frames or mapping_failure" -v
```

Expected: the new Worker trace origins/mapping assertions fail while legacy Worker tests still pass.

- [ ] **Step 3: Reuse the existing Worker mapper for normalized frames**

Add a conversion that keeps a single mapping implementation:

```python
def _worker_trace_frame(
    frame: ParsedDiagnosticFrame,
    artifact: WorkerDiagnosticArtifact,
    observed_registration: str,
) -> tuple[ErrorTraceFrame, WorkerRuntimeFrameDiagnostic]:
    legacy = _worker_runtime_frame(
        frame.location,
        artifact,
        observed_registration,
    )
    visible_line = (
        None
        if legacy.visible_location is None
        or artifact.visible_source_context is None
        else artifact.visible_source_context.line_range(
            legacy.visible_location.source_unit,
            legacy.visible_location.line,
        )
    )
    return (
        ErrorTraceFrame(
            ordinal=frame.ordinal,
            cause_ordinal=frame.cause_ordinal,
            origin=ErrorTraceFrameOrigin.WORKER_ARTIFACT,
            platform_location=frame.location,
            block_span=frame.block_span,
            detail_span=frame.detail_span,
            mapping_confidence=legacy.mapping_confidence,
            registration_name=observed_registration,
            logical_name=legacy.logical_name,
            revision=legacy.revision,
            artifact_sha256=legacy.artifact_sha256,
            source_unit=legacy.source_unit,
            visible_location=legacy.visible_location,
            visible_line_span=visible_line,
            related_visible_span=legacy.related_visible_span,
            lowered_location=legacy.lowered_location,
            synthetic_region=legacy.synthetic_region,
            dependency_anchor=legacy.dependency_anchor,
            method_anchor=legacy.method_anchor,
        ),
        legacy,
    )
```

Add `_unknown_worker_projection(frame)` that returns a `WorkerRuntimeFrameDiagnostic` with the observed registration/module string and all identity fields `None`, matching the current implementation. In `normalize_platform_diagnostic_trace`, create a case-folded registration lookup restricted to `pinned_manifest_sha256`; only one exact match is accepted. A missing/duplicate/stale match produces an `UNKNOWN` trace frame plus the unknown legacy projection.

```python
def _unknown_worker_projection(
    observed_registration: str,
) -> WorkerRuntimeFrameDiagnostic:
    return WorkerRuntimeFrameDiagnostic(
        observed_registration,
        None,
        None,
        None,
        MappingConfidence.UNKNOWN,
    )


def _pinned_worker_artifact(
    registration_name: str,
    manifest_sha256: str,
    artifacts: tuple[WorkerDiagnosticArtifact, ...],
) -> WorkerDiagnosticArtifact | None:
    matches = tuple(
        artifact
        for artifact in artifacts
        if artifact.manifest_sha256 == manifest_sha256
        and artifact.registration_name.casefold() == registration_name.casefold()
    )
    return matches[0] if len(matches) == 1 else None
```

For every parsed frame, catch `BaseException` around only that frame's matching/mapping and create the unknown pair. Continue processing subsequent frames.

In `_remap_worker_runtime_primary`, wrap its existing call to `_worker_runtime_frame` with the same per-location `try/except BaseException` and append `_unknown_worker_projection(observed_registration)` on failure. This ensures an injected mapper failure cannot escape before the richer trace is assembled.

Replace Task 3's frame loop with this helper and assign both returned tuples in `normalize_platform_diagnostic_trace`:

```python
def _normalize_trace_frames(
    parsed: ParsedPlatformDiagnostic,
    *,
    executed: MappedSource | None,
    visible_source_context: VisibleSourceContext | None,
    pinned_manifest_sha256: str | None,
    pinned_artifacts: tuple[WorkerDiagnosticArtifact, ...],
) -> tuple[
    tuple[ErrorTraceFrame, ...],
    tuple[WorkerRuntimeFrameDiagnostic, ...],
]:
    frames: list[ErrorTraceFrame] = []
    worker_frames: list[WorkerRuntimeFrameDiagnostic] = []
    include_worker_projection = pinned_manifest_sha256 is not None
    for item in parsed.frames:
        worker_location = item.location.worker_artifact_location
        observed_registration = (
            None
            if worker_location is None
            else worker_location.registration_name
        )
        try:
            if worker_location is not None and pinned_manifest_sha256 is not None:
                artifact = _pinned_worker_artifact(
                    worker_location.registration_name,
                    pinned_manifest_sha256,
                    pinned_artifacts,
                )
                if artifact is None:
                    normalized = _unknown_trace_frame(
                        item,
                        registration_name=worker_location.registration_name,
                    )
                    legacy = _unknown_worker_projection(
                        worker_location.registration_name
                    )
                else:
                    normalized, legacy = _worker_trace_frame(
                        item,
                        artifact,
                        worker_location.registration_name,
                    )
                frames.append(normalized)
                worker_frames.append(legacy)
                continue
            if worker_location is not None:
                frames.append(
                    _unknown_trace_frame(
                        item,
                        registration_name=worker_location.registration_name,
                    )
                )
                continue
            if item.location.module_name in _UNKNOWN_MODULES:
                frames.append(
                    _main_trace_frame(item, executed, visible_source_context)
                    if executed is not None
                    else _unknown_trace_frame(item)
                )
                if include_worker_projection:
                    worker_frames.append(
                        _unknown_worker_projection(item.location.module_name)
                    )
                continue
            frames.append(_native_trace_frame(item))
        except BaseException:
            frames.append(
                _unknown_trace_frame(
                    item,
                    registration_name=observed_registration,
                )
            )
            if include_worker_projection and (
                worker_location is not None
                or item.location.module_name in _UNKNOWN_MODULES
            ):
                worker_frames.append(
                    _unknown_worker_projection(
                        observed_registration or item.location.module_name
                    )
                )
    return tuple(frames), tuple(worker_frames)
```

For pinned Worker-only calls, retain `base.worker_frames` in the final diagnostic so legacy parsing of unusual coordinates is unchanged. For a future combined main+Worker call, use the helper's compatibility tuple. The exact selection is:

```python
compatibility_frames = (
    base.worker_frames
    if executed is None and pinned_manifest_sha256 is not None
    else normalized_worker_frames
)
return replace(
    base,
    causes=causes,
    frames=normalized_frames,
    frames_truncated=parsed.frames_truncated,
    causes_truncated=parsed.causes_truncated,
    worker_frames=compatibility_frames,
)
```

- [ ] **Step 4: Delegate the legacy Worker entry point without changing identity semantics**

Extract the current body of `remap_worker_runtime_diagnostic` to `_remap_worker_runtime_primary`. It must keep its existing diagnostic ID inputs, code selection, primary Worker frame, artifact/source-map hash selection, and compatibility tuple.

Make `normalize_platform_diagnostic_trace` choose its base in this order:

1. `executed` supplied → `_remap_platform_primary`;
2. pinned Worker manifest supplied without `executed` → `_remap_worker_runtime_primary`;
3. neither supplied → generic trace-only base.

Replace the public Worker entry point with:

```python
def remap_worker_runtime_diagnostic(
    parsed: ParsedPlatformDiagnostic,
    *,
    pinned_manifest_sha256: str,
    pinned_artifacts: tuple[WorkerDiagnosticArtifact, ...],
) -> NormalizedDiagnostic:
    return normalize_platform_diagnostic_trace(
        parsed,
        stage=DiagnosticStage.EXECUTION,
        pinned_manifest_sha256=pinned_manifest_sha256,
        pinned_artifacts=pinned_artifacts,
    )
```

When both main and Worker evidence are supplied, the main primary remains authoritative while the compatibility `worker_frames` tuple is still built from canonical Worker entries and unknown-module entries in platform order. Native frames never enter `worker_frames`.

In the existing delta/full Worker diagnostic loop in `test_bsl_diagnostic_acceptance.py`, append these compatibility assertions after `assert actual == expected`:

```python
assert len(actual.frames) == len(actual.worker_frames)
assert [frame.logical_name for frame in actual.frames] == [
    frame.logical_name for frame in actual.worker_frames
]
assert [frame.mapping_confidence for frame in actual.frames] == [
    frame.mapping_confidence for frame in actual.worker_frames
]
assert actual.diagnostic_id == expected.diagnostic_id
assert actual.visible_location == expected.visible_location
```

- [ ] **Step 5: Run Worker diagnostics and source-map acceptance tests**

Run:

```powershell
uv run python -m pytest tests/unit/test_bsl_diagnostics.py -k "worker or mixed_trace or trace" -v
uv run python -m pytest tests/unit/test_bsl_diagnostic_acceptance.py -k "worker or source_map" -v
```

Expected: PASS. Existing Worker diagnostic IDs, dependency anchors, line-only behavior, generation pin matching, and `worker_frames` ordering remain unchanged.

- [ ] **Step 6: Commit mixed stack normalization**

```powershell
git add src/onec_runtime/bsl/diagnostics.py tests/unit/test_bsl_diagnostics.py tests/unit/test_bsl_diagnostic_acceptance.py
git commit -m "feat: normalize mixed BSL error stacks"
```

---

### Task 5: Contract Validation, Privacy Locks, and Full Regression

**Files:**
- Modify: `src/onec_runtime/runtime_contracts.py:15-122`
- Test: `tests/unit/test_runtime_contract_boundaries.py:1-45`
- Test: `tests/unit/test_bsl_diagnostics.py:302-341`
- Test: `tests/unit/test_bsl_diagnostic_acceptance.py:1040-1080`

**Interfaces:**
- Consumes: `DiagnosticTextSpan`, `ErrorTraceCause`, `ErrorTraceFrame`, `ErrorTraceFrameOrigin`, and enriched `NormalizedDiagnostic` from Tasks 2–4.
- Produces: fail-closed validation for every nested trace value while preserving existing public/expert serialization key shapes and the 64 KiB UTF-8 verbatim diagnostic bound established in Task 1.

- [ ] **Step 1: Write failing sanitizer and wire-shape tests**

In `test_runtime_contract_boundaries.py`, import `replace`, `pytest`, the new diagnostic types, `parse_platform_diagnostic`, `remap_platform_diagnostic`, `sanitize_normalized_diagnostic`, and the source-map builders. Add these concrete helpers:

```python
def _one_line_mapped_source(source: str) -> MappedSource:
    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL,
        "contract-diagnostic",
        1,
        source_sha256(source),
    )
    visible = mapped_visible_source(source, unit)
    builder = SourceTransformBuilder(visible)
    builder.copy(SourceSpan(0, len(source)))
    return builder.build(SourceArtifactKind.EXECUTED_BSL)


def _valid_trace_diagnostic() -> NormalizedDiagnostic:
    source = "Результат = 1;"
    return remap_platform_diagnostic(
        parse_platform_diagnostic("{<Неизвестный модуль>(1,1)}: failure"),
        _one_line_mapped_source(source),
        stage=DiagnosticStage.EXECUTION,
    )


def _mutate_trace_for_contract_case(
    diagnostic: NormalizedDiagnostic,
    mutation: str,
) -> NormalizedDiagnostic:
    frame = diagnostic.frames[0]
    cause = diagnostic.causes[0]
    text = diagnostic.platform_diagnostic
    assert text is not None
    if mutation == "bad_frame_ordinal":
        return replace(diagnostic, frames=(replace(frame, ordinal=9),))
    if mutation == "bad_cause_reference":
        return replace(
            diagnostic,
            frames=(replace(frame, cause_ordinal=31),),
        )
    if mutation == "oversized_frames":
        return replace(
            diagnostic,
            causes=(),
            frames=tuple(
                replace(frame, ordinal=index, cause_ordinal=None)
                for index in range(129)
            ),
        )
    if mutation == "oversized_causes":
        return replace(
            diagnostic,
            frames=(replace(frame, cause_ordinal=None),),
            causes=tuple(
                replace(cause, ordinal=index, frame_ordinals=())
                for index in range(33)
            ),
        )
    if mutation == "out_of_range_platform_coordinate":
        return replace(
            diagnostic,
            frames=(
                replace(
                    frame,
                    platform_location=replace(
                        frame.platform_location,
                        line=10_000_001,
                    ),
                ),
            ),
        )
    if mutation == "diagnostic_span_outside_text":
        return replace(
            diagnostic,
            frames=(
                replace(
                    frame,
                    block_span=DiagnosticTextSpan(0, len(text) + 1),
                ),
            ),
        )
    if mutation == "wrong_origin_enum":
        return replace(diagnostic, frames=(replace(frame, origin="native_module"),))
    if mutation == "invalid_worker_digest":
        return replace(diagnostic, frames=(replace(frame, artifact_sha256="bad"),))
    raise AssertionError(f"unknown contract mutation: {mutation}")
```

Then add:

```python
def test_sanitizer_accepts_maximum_bounded_error_trace() -> None:
    raw = "{<Неизвестный модуль>(1,1)}: " + "x" * (64 * 1024)
    parsed = parse_platform_diagnostic(raw)
    diagnostic = remap_platform_diagnostic(
        parsed,
        _one_line_mapped_source("Результат = 1;"),
        stage=DiagnosticStage.EXECUTION,
    )

    safe = sanitize_normalized_diagnostic(diagnostic)

    assert safe is not None
    assert safe.platform_diagnostic is not None
    assert len(safe.platform_diagnostic.encode("utf-8")) <= 64 * 1024
    assert safe.frames


@pytest.mark.parametrize(
    "mutation",
    (
        "bad_frame_ordinal",
        "bad_cause_reference",
        "oversized_frames",
        "oversized_causes",
        "out_of_range_platform_coordinate",
        "diagnostic_span_outside_text",
        "wrong_origin_enum",
        "invalid_worker_digest",
    ),
)
def test_sanitizer_rejects_malformed_nested_trace_without_raising(
    mutation: str,
) -> None:
    diagnostic = _valid_trace_diagnostic()
    malformed = _mutate_trace_for_contract_case(diagnostic, mutation)

    assert sanitize_normalized_diagnostic(malformed) is None
```

In `test_bsl_diagnostics.py`, add `import onec_runtime.privacy as privacy`, enrich an existing diagnostic, and assert exact unchanged key sets:

```python
def test_rich_trace_does_not_expand_existing_wire_shapes() -> None:
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
    assert len(expert["platform_diagnostic"]) == 4_096
    assert expert["platform_diagnostic_truncated"] is True
```

- [ ] **Step 2: Run boundary tests and verify malformed values are accepted today**

Run:

```powershell
uv run python -m pytest tests/unit/test_runtime_contract_boundaries.py tests/unit/test_bsl_diagnostics.py -k "sanitizer or wire_shape" -v
```

Expected: failures because the sanitizer still rejects valid evidence above 4,096 characters and does not inspect the new nested trace.

- [ ] **Step 3: Implement strict nested validation**

Import `DiagnosticCoordinateSpace`, `DiagnosticTextSpan`, `ErrorTraceCause`, `ErrorTraceFrame`, `ErrorTraceFrameOrigin`, `LoweredSourceLocation`, `PlatformDiagnosticLocation`, and `WorkerArtifactPlatformLocation` directly from `onec_runtime.bsl.diagnostics` to avoid changing `bsl/__init__.py`. Replace `MAX_PRIVATE_DIAGNOSTIC_LENGTH` with:

```python
MAX_PRIVATE_DIAGNOSTIC_BYTES = 64 * 1024
MAX_DIAGNOSTIC_FRAMES = 128
MAX_DIAGNOSTIC_CAUSES = 32
```

Add these validation helpers:

```python
def _bounded_diagnostic_span(value: object, text_length: int) -> bool:
    return (
        isinstance(value, DiagnosticTextSpan)
        and type(value.start) is int
        and type(value.end) is int
        and 0 <= value.start <= value.end <= text_length
    )


def _bounded_platform_location(value: object) -> bool:
    return (
        isinstance(value, PlatformDiagnosticLocation)
        and type(value.module_name) is str
        and 0 < len(value.module_name) <= 512
        and type(value.module_components) is tuple
        and 0 < len(value.module_components) <= 32
        and all(type(item) is str and item for item in value.module_components)
        and type(value.line) is int
        and 0 <= value.line <= MAX_DIAGNOSTIC_COORDINATE
        and (
            value.column is None
            or (
                type(value.column) is int
                and 0 <= value.column <= MAX_DIAGNOSTIC_COORDINATE
            )
        )
        and type(value.coordinate_space) is DiagnosticCoordinateSpace
        and (
            value.worker_artifact_location is None
            or (
                isinstance(
                    value.worker_artifact_location,
                    WorkerArtifactPlatformLocation,
                )
                and type(value.worker_artifact_location.registration_name) is str
                and 0 < len(value.worker_artifact_location.registration_name) <= 512
                and len(value.module_components) == 3
                and value.module_components[1]
                == value.worker_artifact_location.registration_name
            )
        )
    )


def _bounded_trace(
    value: NormalizedDiagnostic,
    platform_text: str | None,
) -> bool:
    if type(value.frames) is not tuple or len(value.frames) > MAX_DIAGNOSTIC_FRAMES:
        return False
    if type(value.causes) is not tuple or len(value.causes) > MAX_DIAGNOSTIC_CAUSES:
        return False
    if type(value.frames_truncated) is not bool or type(value.causes_truncated) is not bool:
        return False
    if (value.frames or value.causes) and platform_text is None:
        return False
    text_length = 0 if platform_text is None else len(platform_text)
    for index, cause in enumerate(value.causes):
        if (
            not isinstance(cause, ErrorTraceCause)
            or cause.ordinal != index
            or not _bounded_diagnostic_span(cause.summary_span, text_length)
            or not _bounded_diagnostic_span(cause.block_span, text_length)
            or not (
                cause.block_span.start
                <= cause.summary_span.start
                <= cause.summary_span.end
                <= cause.block_span.end
            )
            or type(cause.frame_ordinals) is not tuple
            or tuple(sorted(set(cause.frame_ordinals))) != cause.frame_ordinals
            or any(not 0 <= item < len(value.frames) for item in cause.frame_ordinals)
        ):
            return False
    for index, frame in enumerate(value.frames):
        if (
            not isinstance(frame, ErrorTraceFrame)
            or frame.ordinal != index
            or type(frame.origin) is not ErrorTraceFrameOrigin
            or not _bounded_platform_location(frame.platform_location)
            or not _bounded_trace_frame_fields(frame)
            or not _bounded_diagnostic_span(frame.block_span, text_length)
            or (
                frame.detail_span is not None
                and not _bounded_diagnostic_span(frame.detail_span, text_length)
            )
            or (
                frame.detail_span is not None
                and not (
                    frame.block_span.start
                    <= frame.detail_span.start
                    <= frame.detail_span.end
                    <= frame.block_span.end
                )
            )
            or (
                frame.cause_ordinal is not None
                and not 0 <= frame.cause_ordinal < len(value.causes)
            )
        ):
            return False
        if frame.cause_ordinal is not None and index not in value.causes[
            frame.cause_ordinal
        ].frame_ordinals:
            return False
    for cause in value.causes:
        if cause.frame_ordinals != tuple(
            frame.ordinal
            for frame in value.frames
            if frame.cause_ordinal == cause.ordinal
        ):
            return False
    return True
```

Add and call this exhaustive frame-field helper inside the frame loop before the cause-reference check:

```python
def _bounded_optional_label(value: object, *, maximum: int = 256) -> bool:
    return value is None or (
        type(value) is str and 0 < len(value) <= maximum
    )


def _bounded_optional_span(value: object) -> bool:
    return value is None or _bounded_span(value)


def _bounded_trace_frame_fields(frame: ErrorTraceFrame) -> bool:
    if type(frame.mapping_confidence) is not MappingConfidence:
        return False
    if not _bounded_optional_label(frame.registration_name, maximum=512):
        return False
    if not _bounded_optional_label(frame.logical_name):
        return False
    if frame.revision is not None and (
        type(frame.revision) is not int
        or not 0 <= frame.revision <= MAX_DIAGNOSTIC_COORDINATE
    ):
        return False
    if frame.artifact_sha256 is not None and (
        type(frame.artifact_sha256) is not str
        or _SHA256_RE.fullmatch(frame.artifact_sha256) is None
    ):
        return False
    if frame.source_unit is not None and (
        not isinstance(frame.source_unit, SourceUnitRef)
        or len(frame.source_unit.unit_id) > 256
    ):
        return False
    if frame.visible_location is not None:
        visible = frame.visible_location
        if (
            not isinstance(visible, VisibleSourceLocation)
            or not isinstance(visible.source_unit, SourceUnitRef)
            or not _bounded_positive_coordinate(visible.line)
            or not _bounded_positive_coordinate(visible.column)
            or not _bounded_span(visible.span)
            or len(visible.source_unit.unit_id) > 256
            or (
                frame.source_unit is not None
                and visible.source_unit != frame.source_unit
            )
        ):
            return False
    if frame.lowered_location is not None:
        lowered = frame.lowered_location
        if (
            not isinstance(lowered, LoweredSourceLocation)
            or not _bounded_positive_coordinate(lowered.line)
            or not _bounded_positive_coordinate(lowered.column)
            or type(lowered.offset) is not int
            or not 0 <= lowered.offset <= MAX_DIAGNOSTIC_COORDINATE
            or not _bounded_span(lowered.span)
        ):
            return False
    if any(
        not _bounded_optional_span(value)
        for value in (
            frame.visible_line_span,
            frame.related_visible_span,
            frame.dependency_anchor,
            frame.method_anchor,
        )
    ):
        return False
    if frame.synthetic_region is not None and (
        type(frame.synthetic_region) is not str
        or len(frame.synthetic_region) > MAX_DIAGNOSTIC_LABEL_LENGTH
        or _DIAGNOSTIC_LABEL_RE.fullmatch(frame.synthetic_region) is None
    ):
        return False
    return True
```

In `sanitize_normalized_diagnostic`, replace the old string-length check with `len(platform_diagnostic.encode("utf-8")) > MAX_PRIVATE_DIAGNOSTIC_BYTES`, then call `_bounded_trace(value, platform_diagnostic)` before the existing `replace(value, runtime_summary=_DIAGNOSTIC_SUMMARIES[value.stage])` return. Keep the outer `try/except BaseException` so all malformed cases return `None`.

Do not reintroduce an independent expert-output cap or content filter in `privacy.py`; Task 1 establishes one 64 KiB UTF-8 verbatim bound across retained evidence, private MCP storage, and expert output.

- [ ] **Step 4: Lock deterministic and legacy compatibility in acceptance tests**

In `test_bsl_diagnostics.py`, append these assertions to `test_deterministic_parse_error_normalizes_through_exact_input`:

```python
assert diagnostic.causes == ()
assert diagnostic.frames == ()
assert diagnostic.frames_truncated is False
assert diagnostic.causes_truncated is False
```

The delta/full Worker acceptance loop was locked in Task 4. Do not change any expected literal diagnostic IDs: the existing suite already asserts those IDs at adapter and backend boundaries, and the full regression run must keep them green.

- [ ] **Step 5: Run focused core and contract tests**

Run:

```powershell
uv run python -m pytest tests/unit/test_bsl_diagnostics.py tests/unit/test_bsl_diagnostic_acceptance.py tests/unit/test_runtime_contract_boundaries.py -v
```

Expected: PASS with no live-1C requirement.

- [ ] **Step 6: Run the full static suite and inspect scope**

Run:

```powershell
uv run python -m pytest
git diff --check master...HEAD
git diff --name-only master...HEAD
```

Expected: the baseline remains `4005 passed, 83 skipped` plus the new tests, `git diff --check` prints nothing, and the changed-file list contains only the two core files, three focused test files, the approved spec, and this plan.

- [ ] **Step 7: Commit contract validation and regression locks**

```powershell
git add src/onec_runtime/runtime_contracts.py tests/unit/test_runtime_contract_boundaries.py tests/unit/test_bsl_diagnostics.py tests/unit/test_bsl_diagnostic_acceptance.py
git commit -m "test: enforce full BSL diagnostic boundaries"
```

- [ ] **Step 8: Record final verification after the commit**

Run:

```powershell
git status --short
git log --oneline master..HEAD
uv run python -m pytest tests/unit/test_bsl_diagnostics.py tests/unit/test_bsl_diagnostic_acceptance.py tests/unit/test_runtime_contract_boundaries.py -q
```

Expected: clean status, the five implementation commits in order, and all focused tests passing. Report that these are static tests and that live 1C qualification remains pending by design.
