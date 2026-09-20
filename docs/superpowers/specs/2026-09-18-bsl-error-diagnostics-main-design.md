# BSL error diagnostics on the MAIN/CAPTURE execution architecture

**Date:** 2026-09-18
**Status:** Proposed for implementation
**Base:** `master` at `e008da1` (`v0.1.20`)

## Goal and boundaries

Bring the full BSL failure diagnostics developed on
`feature/bsl-error-diagnostics-core` into the current execution architecture.
The earlier functional design is recorded in that branch at
`docs/superpowers/specs/2026-09-15-bsl-error-diagnostics-design.md`.
This document defines the new integration boundaries; it does not authorize a
mechanical merge of the older runtime orchestration.

A failed `%%bsl` cell should show the 1C reason, ordered causes and stack,
with exact source-map remapping for generated notebook and Worker modules.
Native configuration-module coordinates are already 1:1 platform locations;
they require no source map. The original bounded 1C diagnostic remains
available in the notebook's diagnostic display. A successful cell must perform
no additional diagnostic parsing, lookups, filesystem reads, or RDBG calls.
Failure of the diagnostics pipeline must not alter the confirmed BSL outcome,
the MAIN/CAPTURE operation state, cleanup, or later admission.

Do not change the 1C extension, RDBG protocol, operation ownership, or
successful-cell path. Do not implement the deferred MCP expert contract in
this work. The existing compact/public MCP behavior remains bounded.

## Chosen approach

Port the reusable pure parser, trace model, source-map enrichment, and
bounded private evidence selectively. Then connect them at the new execution
reply boundary and render them in Jupyter. This preserves the ownership split
in `src/onec_runtime/execution`.

The rejected alternatives are a whole-branch merge (it would reintroduce the
removed `runtime_api`/`prototype_runtime` execution path) and a fresh parser
rewrite (it would discard the Russian/English and nested-cause regression work
already validated). No compatibility layer for the old orchestration is
needed; the project has no external users requiring it.

## Core diagnostic contract

`src/onec_runtime/bsl/diagnostics.py` owns parsing and pure normalization.
The trace preserves 1C's outer-to-inner cause and frame order, mixed
line/column and line-only locators, the platform-provided detail fragments,
unknown lines, and truncation evidence. It must not invent frames that 1C did
not report. Each eligible generated frame is mapped independently using the
exact executed `MappedSource` and immutable Worker artifact evidence. Unknown,
stale, ambiguous, or synthetic mappings remain explicitly untrusted.

The existing normalized diagnostic and private-evidence validation in
`runtime_contracts.py` receive the trace fields. Retain the 64 KiB UTF-8 bound
for verbatim private platform text, 32 causes, and 128 frames, with independent
truncation flags. Generic/public serialization must not expose source text or
the private raw diagnostic; expert/private paths may expose the bounded 1C
text without rewriting its content. Public summaries stay compact.

The pure normalizer takes parsed text, diagnostic stage, executed mapped
source, hash-fenced visible source context, and optionally the manifest-pinned
Worker artifacts. It performs no RDBG or filesystem operations and does not
query mutable runtime state. Local parsing/lowering errors retain their
existing deterministic normalization.

## Execution integration

`execution/reply_publication.py` is the MAIN/CAPTURE error handoff. It already
receives the confirmed remote error and the execution source evidence through
`MainPublicationRecord` or `CapturePublicationRecord`, assembled in
`execution/settlement.py`. Resolve Worker artifact evidence lazily after a
confirmed error, while the admitted operation's existing Worker generation
pin is still held. Do not create a diagnostic-only pin or snapshot on the
successful path. If the exact pinned manifest and artifact set cannot be
established, retain those frames as unknown rather than consulting the current
generation. This must not add an RDBG request or create another owner of
execution state.

Only after a non-empty remote error is confirmed, parse and enrich it. A
diagnostic exception degrades the presentation to the existing safe failed
reply; it never changes the remote failure classification, cleanup, pin
release, or future operation readiness. Worker publication/stage failures
remain under their new `worker_activation.py` and
`worker_module_lifecycle.py` owners; adapt those failure handoffs rather than
reviving old `runtime_api` methods. MAIN completion, late completion after
CAPTURE, and CAPTURE cell evaluation use the same pure normalizer and preserve
the original operation-bound source evidence.

## Jupyter rendering and optional source links

`packages/jupyter` owns human presentation. Port the trace formatter and
source helper from the diagnostics branch into the current `extension.py`
display path. Show reason, ordered stack with mapped notebook/Worker locations,
native module locations, and clearly labelled platform/generated fragments.
The detailed notebook mode also shows the bounded original 1C diagnostic.
Machine/public output remains compact and hash-fenced to the current cell or
retained source unit; the Python traceback does not dump adapter internals.

If `source_root` is configured, reuse the configuration-source layout and
module resolver to add an optional native-module file link and verified line
excerpt. File resolution or source mismatch may remove only that enrichment.
The source file is never the source of the 1C error text, and its contents do
not replace the platform-provided fragment. No `source_root` is needed to
display a native frame with its 1C coordinates and fragment.

## Verification and delivery

Implement in small, independently testable slices, starting with failing
focused tests:

1. Pure parser/model tests for Russian and English diagnostics, nested causes,
   mixed frames, truncation, native coordinates, and every source-map fallback.
2. New-architecture MAIN/CAPTURE and Worker failure tests, including late
   completion, exact operation pinning, diagnostic fault injection, and a
   zero-diagnostic-call successful-cell spy.
3. Jupyter display tests for reason/stack/fragments/raw detail, cell identity
   fencing, and optional `source_root`; existing compact/public privacy tests.
4. Focused suite, full static suite, then opt-in live 1C checks on temporary
   infobases where the installed platform permits. Static results must not be
   described as live qualification.

Work in a new branch from master and leave master unmerged. Before editing
each package, follow its `AGENTS.md`, including the nested execution-owner
instructions. Keep test fixtures small and synthetic; never commit live
infobases, raw run evidence, credentials, or local machine paths.
