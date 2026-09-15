# Full BSL Error Diagnostics — Core Design

**Date:** 2026-09-15

**Status:** Proposed

**Scope:** `src/onec_runtime` diagnostic model, parser, source-map projection, and contract validation

## Problem

The 1C extension already evaluates `ПодробноеПредставлениеОшибки(ИнформацияОбОшибке())` after a failed BSL operation. That value contains the platform's detailed diagnostic, including nested causes, module locations, stack entries, and platform-provided text fragments. The runtime transports enough text through RDBG, but the Python core currently:

- retains one 64 KiB UTF-8-bounded, verbatim evidence value;
- selects one primary main-cell location;
- exposes a separate Worker-only frame projection;
- does not model the cause chain or the complete mixed native/generated stack;
- cannot remap every generated frame independently.

As a result, notebook and MCP adapters cannot present diagnostics at the level available in a normal 1C:Enterprise session even though the platform has already supplied the evidence.

## Goals

1. Preserve a bounded, private copy of the detailed 1C diagnostic as the source of truth.
2. Parse an ordered cause chain and complete ordered stack without inventing structure for unknown text.
3. Remap every generated main-cell or Worker frame through the exact source map admitted for the operation.
4. Preserve native configuration-module locations as direct 1C coordinates. A source map is neither required nor useful when source and executed module are 1:1.
5. Keep all current `NormalizedDiagnostic` primary fields and `worker_frames` behavior compatible.
6. Guarantee that diagnostic work cannot change the BSL result or the runtime lifecycle.
7. Add no work to a successful cell path.

## Non-goals

This first, independently deliverable core tranche does not:

- change the 1C extension or the RDBG protocol;
- stop on RDBG exceptions or request a live debugger stack;
- change operation coordination, capture fencing, or generation pin lifetime;
- introduce a configuration `source_root` resolver;
- change Jupyter rendering or MCP schemas;
- expose source text in public artifacts;
- unify postmortem error traces with live capture frames or live variable inspection.

Jupyter, MCP, late capture completion, and `source_root` links are integration tranches built on this contract after the overlapping capture-inspection work lands.

## Chosen approach

Parse the detailed diagnostic already returned by 1C in Python and enrich the existing immutable core diagnostic. This has the smallest runtime surface:

- no additional 1C calls;
- no debugger mode changes;
- no change to successful execution;
- parser/remapper failures can be isolated after the operation has already failed.

Rejected alternatives:

- **RDBG exception stops and live stack reads.** They alter execution behavior, require more lifecycle coordination, and conflict with the requirement that diagnostics must not affect runtime.
- **A new 1C-side structured error protocol.** It duplicates platform parsing in BSL and expands the extension contract despite the detailed representation already being available.
- **Adapter-specific parsing.** It would make Jupyter, MCP, and future VS Code behavior diverge and would duplicate source-map logic.

## Core data model

All added types are frozen, slotted dataclasses and contain bounded values only.

### Diagnostic text spans

`DiagnosticTextSpan(start, end)` addresses a half-open range in the retained private diagnostic text. It is distinct from `SourceSpan`, which addresses BSL source. Spans avoid copying platform prose into every cause and frame.

The existing `_PrivatePlatformEvidence` remains the only owner of retained platform text. Generic dataclass serialization must continue to redact it.

### Parsed structure

`ParsedDiagnosticCause` contains:

- `ordinal`: zero-based outer-to-inner order;
- `summary_span`: the cause prose excluding recognized frame locators and structural separators;
- `block_span`: the complete retained cause block;
- `frame_ordinals`: ordered references to retained parsed frames.

`ParsedDiagnosticFrame` contains:

- `ordinal`: zero-based platform order;
- `cause_ordinal`: the owning retained cause, or `None` when no retained cause can be assigned;
- `location`: the existing `PlatformDiagnosticLocation`;
- `block_span`: locator plus its associated platform text up to the next locator or cause boundary;
- `detail_span`: platform-provided text following the locator, if present.

`ParsedPlatformDiagnostic` gains:

- `causes: tuple[ParsedDiagnosticCause, ...]`;
- `frames: tuple[ParsedDiagnosticFrame, ...]`;
- `opaque_spans: tuple[DiagnosticTextSpan, ...]` for retained text not classified as a cause, frame, or structural separator;
- `frames_truncated: bool`;
- `causes_truncated: bool`.

The existing `locations`, `line`, `column`, `module_name`, `additional_locations`, and compilation-marker fields remain and are populated with the same selection rules as today.

### Normalized error trace

`ErrorTraceFrameOrigin` has four values:

- `EXECUTED_ARTIFACT` for generated main-cell code;
- `WORKER_ARTIFACT` for generated Worker modules;
- `NATIVE_MODULE` for ordinary 1C configuration and extension modules;
- `UNKNOWN` when the module cannot be classified safely.

`ErrorTraceFrame` contains:

- platform order and optional cause ordinal;
- origin and original `PlatformDiagnosticLocation`;
- diagnostic `block_span` and `detail_span`;
- optional Worker registration/logical/revision/artifact identity;
- `mapping_confidence`, `source_unit`, `visible_location`, `related_visible_span`, `lowered_location`, and `synthetic_region` using current mapping semantics;
- optional dependency and method anchors for existing Worker dependency diagnostics;
- optional `visible_line_span`, identifying the exact hash-fenced original-source line that a local adapter may slice later.

`ErrorTraceCause` contains its ordinal, summary/block spans, and retained normalized frame ordinals.

`NormalizedDiagnostic` gains:

- `causes: tuple[ErrorTraceCause, ...] = ()`;
- `frames: tuple[ErrorTraceFrame, ...] = ()`;
- `frames_truncated: bool = False`;
- `causes_truncated: bool = False`.

The first applicable normalized frame continues to populate the existing primary mapping fields. `worker_frames` remains as a compatibility projection from Worker and unknown generated frames in platform order. Existing consumers therefore keep their current behavior until deliberately migrated.

## Parsing rules

Parsing is conservative and operates only on bounded private text.

1. Compute `platform_diagnostic_sha256` from the complete original UTF-8 input.
2. Retain at most 64 KiB of UTF-8. Truncation must stop before a partial code point. `platform_diagnostic_truncated` reports loss relative to the complete input.
3. Recognize module locators only at line starts and only in canonical forms:
   - `{Module.Path(line,column)}`;
   - `{Module.Path(line)}`;
   - both current spellings of the unknown module.
4. Apply existing module length, component count, numeric-width, and Worker-registration allowlists. Runtime-contract validation still rejects non-positive or out-of-range normalized coordinates. General line-only locations are parsed, but generated-code remapping remains exact-or-unknown under the rules below.
5. Recognize a cause boundary only when `по причине:` occupies its structural line, ignoring surrounding horizontal whitespace and case. The text before the first boundary is the outer cause; subsequent blocks are ordered inner causes.
6. Associate a locator's detail with that frame until the next canonical locator or cause boundary. Preserve line breaks and platform wording through spans; do not rewrite the text.
7. Preserve all unclassified ranges as opaque spans. Raw retained text is authoritative when a future platform version produces an unfamiliar layout.
8. Preserve the existing strict terminal compilation-marker rule so arbitrary prose cannot reclassify the diagnostic stage.

Limits are independent:

- retained detailed text: 64 KiB UTF-8;
- retained frames: 128;
- retained causes: 32;
- module locator: 512 characters and 32 components;
- coordinates: existing runtime-contract maximum.

`frames_truncated` and `causes_truncated` report recognized items beyond their respective limits within the retained text. `platform_diagnostic_truncated` separately reports the 64 KiB text bound; it may conceal additional unparsed structure, so these flags are not inferred from one another.

## Frame classification and mapping

Mapping is performed independently for each retained frame. One failure must not discard other frames.

### Generated main-cell frames

An unknown-module locator can be classified as `EXECUTED_ARTIFACT` only when the caller supplies the exact `MappedSource` admitted for that failed operation. Its one-based line and column are converted against the executed artifact, then mapped with the existing exact/derived/synthetic rules.

If a column is absent, the same conservative single-exact-segment rule currently used for line-only Worker frames applies. Blank, ambiguous, synthetic, zero, and out-of-range lines remain unmapped.

### Worker frames

A canonical `ВнешняяОбработка.OnecRuntime_<identity>.МодульОбъекта` locator can be classified as `WORKER_ARTIFACT` only through the immutable manifest and artifacts pinned to the operation. Matching remains case-insensitive for the observed registration and exact for the pinned manifest identity. Each matched frame is mapped through that artifact's `MappedSource` and hash-fenced visible context.

Unknown or stale Worker registrations remain present in the trace with `UNKNOWN` confidence; they are not silently removed.

### Native configuration frames

Any other accepted module locator is `NATIVE_MODULE`. Its module, line, and optional column are already the correct platform coordinates because the module source is executed 1:1. The frame is considered a valid direct location even though `mapping_confidence` is `UNKNOWN`, because that enum continues to describe source-map confidence only.

When a future `source_root` is configured, the configuration-source resolver may attach a file/link and provide hash-fenced source for excerpts. It must not change the platform location or become the source of diagnostic prose. Without `source_root`, the platform's own detail fragment remains available through `detail_span`.

### Source fragments

The core does not duplicate BSL source strings in frames. It records:

- the platform fragment through `detail_span` in private detailed text;
- an original-source `visible_line_span` only when the source unit hash matches the supplied visible context.

At rendering time, a local adapter may slice the current cell, Worker source, or resolved configuration file only after its SHA-256 matches `SourceUnitRef.source_sha256`. For an exact mapped frame this produces the original line. If source is unavailable, stale, derived, or synthetic, the adapter shows the platform/generated fragment explicitly instead of presenting it as original source.

## Pure core API

Introduce one module-level pure function, `normalize_platform_diagnostic_trace`, that receives only immutable evidence:

- parsed platform diagnostic;
- optional exact main `MappedSource` and visible source context;
- optional pinned Worker manifest and diagnostic artifacts;
- diagnostic stage.

Its intended signature is:

```python
def normalize_platform_diagnostic_trace(
    parsed: ParsedPlatformDiagnostic,
    *,
    stage: DiagnosticStage,
    executed: MappedSource | None = None,
    visible_source_context: VisibleSourceContext | None = None,
    pinned_manifest_sha256: str | None = None,
    pinned_artifacts: tuple[WorkerDiagnosticArtifact, ...] = (),
) -> NormalizedDiagnostic: ...
```

It returns the complete normalized trace. It performs no RDBG requests, filesystem reads, registry lookups, or cache writes. The function is initially importable from `onec_runtime.bsl.diagnostics` but is not yet re-exported as part of the broad `onec_runtime.bsl` facade.

Existing entry points remain:

- `remap_platform_diagnostic(...)` delegates to the pure enrichment path and preserves its primary-result behavior;
- `remap_worker_runtime_diagnostic(...)` delegates with the pinned Worker evidence and preserves `worker_frames`;
- `remap_worker_stage_diagnostic(...)` keeps its current exact single-artifact admission rules;
- `normalize_source_error(...)` remains unchanged except for empty default trace fields.

The core tranche need not export new symbols through `bsl/__init__.py`; tests and subsequent integration can import the defining module directly. This avoids overlap with the parallel capture module-syntax registry work.

## Failure isolation and successful-path cost

The integration invariant is strict:

```text
if platform_error_text == "":
    return successful_result

try:
    parse and enrich diagnostic
except BaseException:
    retain the original failed BSL result with a minimal safe diagnostic
```

Therefore, a successful cell performs no diagnostic parsing, remapping, source-root resolution, filesystem access, additional diagnostic-specific RDBG evaluation, or diagnostic-specific allocation. The existing `Ошибка` read that decides whether execution succeeded is unchanged. Existing source maps may already exist because execution needs them, but diagnostics add no new successful-path work. No diagnostic cache is introduced.

On an error path:

- a whole-parser failure cannot replace or mask the BSL failure;
- a per-frame classification or mapping failure degrades only that frame;
- a future `source_root` lookup failure removes only the optional link/excerpt;
- hashes and provenance are never guessed from current mutable state.

The first tranche implements only pure core code, so it cannot yet change either success or failure runtime behavior. Runtime wiring will add an explicit spy test proving that every diagnostic collaborator has zero calls on success.

## Privacy and serialization

- Detailed platform text and its spans remain private evidence.
- The core sanitizer measures private diagnostic text as UTF-8 and accepts at most 64 KiB; it validates all new collection sizes, ordinals, spans, coordinates, labels, identities, and nested types fail-closed.
- Existing public and expert wire shapes do not change in this tranche.
- Expert serialization emits the retained diagnostic verbatim up to the same 64 KiB UTF-8 bound. It does not mask token-, password-, authorization-, PID-, session-, or connection-like substrings: text emitted by 1C is debugging evidence.
- No automatic public artifact contains BSL source or a reversible source path.
- Future Jupyter local rendering may use private text and explicitly provided hash-matched source. Ordinary MCP responses remain short; fuller text/frames require an explicit expert diagnostic contract.

## Compatibility

The change is additive at the dataclass boundary:

- new tuple and boolean fields have defaults;
- existing diagnostic IDs and entrypoint-specific primary-location selection stay stable for existing cases; trace order does not redefine the legacy primary;
- existing `locations` and `worker_frames` order and meaning stay stable;
- deterministic parser/lowering diagnostics receive empty traces;
- public/expert serializers emit their current keys only;
- Compact/public Jupyter and MCP paths omit platform diagnostic text. Existing diagnostic/expert/private paths may carry the expanded verbatim diagnostic without a schema change.

If changing the retained-text bound changes the digest-independent `platform_diagnostic` value for long inputs, that is intentional. The SHA-256 continues to identify the complete original input.

## Interaction with capture-inspection work

The parallel capture-inspection plan overlaps in runtime orchestration and adapters, not in this parser/model tranche.

| Area | This core tranche | Capture-inspection owner / later integration |
|---|---|---|
| Detailed-text parser and trace model | Implement here | Reuse |
| Main/Worker pure source-map enrichment | Implement here | Reuse with operation pin |
| Capture coordinator and late completion | Do not modify | Capture tasks; call enrichment before releasing the generation pin |
| `prototype_runtime.py`, `runtime_api.py`, `session.py` | Do not modify | Coordinate after capture lifecycle changes |
| Configuration module resolver / `source_root` | Do not implement | Reuse the capture configuration-source resolver; never create a second resolver |
| Live stopped-call stack and variables | Separate types | Capture owns `StackPage`/`DebugFrame`; may later share module identity/location primitives only |
| Jupyter rendering | Do not modify | Integrate after capture Jupyter changes |
| MCP contracts and schemas | Do not modify | Integrate after capture MCP changes |
| 1C extension | No changes | No changes required |

To keep the tranche independently mergeable, implementation is limited to:

- `src/onec_runtime/bsl/diagnostics.py`;
- `src/onec_runtime/runtime_contracts.py` for validation and the private-text bound;
- focused diagnostic and contract tests.

It deliberately avoids `errors.py`, runtime orchestration, configuration resolution, RDBG, and extension sources. The approved evidence-bound work may touch privacy and MCP diagnostic transport only to preserve the same verbatim 64 KiB private/expert evidence; it does not change compact/public omission, wire key sets, or adapter rendering.

## Test strategy

Core work follows test-driven development with focused tests first.

### Parser tests

- multiple outer-to-inner causes and multiple frames per cause;
- mixed `{module(line,column)}` and `{module(line)}` frames;
- platform detail/block span boundaries and opaque ranges;
- Russian case/whitespace variants only at structural cause lines;
- unknown layouts preserved without invented causes;
- 64 KiB UTF-8 truncation at a code-point boundary and full-input SHA-256;
- independent 128-frame and 32-cause truncation flags;
- existing compilation-marker and primary-location rules unchanged;
- generic serialization still redacts private evidence.

### Mapping tests

- every main generated frame maps in platform order;
- every pinned Worker frame maps through its own artifact;
- mixed native, main, known Worker, stale Worker, and unknown frames remain ordered;
- line-only exact mapping and all ambiguity fallbacks;
- one malformed or unmappable frame does not affect the others;
- native module coordinates remain direct without a source map;
- visible line spans appear only under an exact source hash fence;
- dependency-binding anchors remain compatible;
- legacy primary fields and `worker_frames` equal their pre-change projections.

### Contract and regression tests

- sanitizer accepts valid maximum-size traces and rejects oversized or malformed nested values without raising;
- public/expert wire dictionaries keep their exact existing key sets;
- deterministic diagnostics keep empty traces;
- focused existing diagnostic acceptance tests pass;
- full static suite remains green.

The later runtime integration adds the successful-path zero-call spy and error-path failure-injection tests. Live qualification against 1C 8.3.27.2170 and 8.5.1.1529 is a separate opt-in acceptance step and must not be inferred from static tests.

## Delivery sequence

1. Land the independent typed parser/model/remapper tranche described here.
2. Rebase or merge it into the capture-inspection line and wire late completion while the correct operation/generation evidence is pinned.
3. Reuse the capture configuration-source resolver for optional native module file links and hash-fenced excerpts.
4. Add Jupyter human rendering: reason, full mapped stack, and explicit generated/platform fragments; retain raw detail in diagnostic mode.
5. Add an explicit MCP expert schema while preserving short ordinary responses.
6. Let VS Code consume the same core contract when its adapter is implemented.

## Acceptance criteria for the core tranche

- The retained detailed diagnostic is bounded by 64 KiB UTF-8 and hashed from the complete input.
- Up to 32 ordered causes and 128 ordered frames are represented with independent truncation flags.
- Every eligible generated frame is remapped independently through exact immutable evidence.
- Native module frames retain their direct 1C locations without requiring a source map or `source_root`.
- Existing primary fields, Worker compatibility frames, serializer shapes, and deterministic diagnostics remain compatible.
- No runtime lifecycle, resolver, RDBG, or extension file changes are present. Any privacy/MCP transport change is limited to the approved shared evidence bound and preserves compact/public omission and existing wire key sets.
- Focused and full static tests pass.
