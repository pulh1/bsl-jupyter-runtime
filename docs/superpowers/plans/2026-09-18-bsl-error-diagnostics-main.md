# BSL Error Diagnostics on MAIN/CAPTURE Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show the full bounded 1C cause chain and stack for failed BSL cells, remapped through exact notebook/Worker source maps, in the redesigned runtime and Jupyter adapter.

**Architecture:** Port the pure parser and trace model from `feature/bsl-error-diagnostics-core`; do not port its former `runtime_api` execution logic. Enrich only confirmed failures in `src/onec_runtime/execution`, using the operation's already-held Worker pin, and render the resulting private trace in `packages/jupyter`.

**Tech Stack:** Python 3.13, pytest, uv, 1C RDBG and BSL, Jupyter/IPython.

**Spec:** `docs/superpowers/specs/2026-09-18-bsl-error-diagnostics-main-design.md`

## Global Constraints

- Work on `feature/bsl-error-diagnostics-main` from master; do not merge master or the old feature branch wholesale.
- Read root and nested `AGENTS.md` before each component edit: `src/onec_runtime`, `src/onec_runtime/execution` and its `controller`, `main`, `capture` directories, `packages/jupyter`, `packages/mcp` if touched, and `tests`.
- Preserve MAIN/CAPTURE's single RDBG arbiter, operation fences, existing Worker pins, and successful-cell behavior. No diagnostic-specific RDBG request, pin, cache, parse, filesystem read, or other success-path work.
- Keep verbatim private 1C evidence at most 64 KiB UTF-8, at most 32 causes and 128 frames, with independent truncation flags. Do not invent missing 1C frames.
- Native 1C module coordinates need no source map. `source_root` only adds a local file hint or verified excerpt; its absence or failure cannot hide the platform text.
- Keep compact/public diagnostic payloads bounded and without source strings or raw 1C text. Do not add the deferred MCP expert schema.
- Run focused static tests before wider static tests. Live 1C qualification is opt-in, uses a disposable infobase, and must not commit its evidence.

## File ownership map

| File | Responsibility |
|---|---|
| `src/onec_runtime/bsl/diagnostics.py` | Pure bounded parser, ordered cause/frame model and source-map enrichment. |
| `src/onec_runtime/runtime_contracts.py`, `src/onec_runtime/privacy.py` | Trace validation and private/public serialization bounds. |
| `packages/mcp/src/onec_runtime_mcp/agent/operations.py`, `packages/mcp/src/onec_runtime_mcp/server.py` | Keep existing private/expert validation at the same byte bound without adding a tool or wire keys. |
| `src/onec_runtime/worker_universe.py` | Read exact diagnostic artifacts through an existing `OperationGenerationPin`. |
| `src/onec_runtime/execution/worker_activation.py` | Narrow read-only evidence adapter for an already-held lease. |
| `src/onec_runtime/execution/controller/controller.py` | Attach error-only evidence to confirmed MAIN/CAPTURE outcomes before pin release. |
| `src/onec_runtime/execution/reply_publication.py` | Normalize confirmed error text into `RuntimeReply.diagnostic`; never own RDBG. |
| `packages/jupyter/src/onec_runtime_jupyter/diagnostic_trace.py` | Human-only reason, stack and raw-detail formatting. |
| `packages/jupyter/src/onec_runtime_jupyter/diagnostic_sources.py` | Optional safe local source-file hints under configured `source_root`. |
| `packages/jupyter/src/onec_runtime_jupyter/extension.py` | Display integration and cell identity fencing. |

The prior branch at `c69b5e5` is a reference implementation for the pure core, privacy bounds, and Jupyter formatter. Selectively transplant those parts with `apply_patch` and reconcile against current master. Do not copy its `prototype_runtime.py`, `runtime_api.py`, obsolete tests, or old orchestration.

---

### Task 1: Pure ordered trace and source-map normalizer

**Files:**
- Modify: `src/onec_runtime/bsl/diagnostics.py`
- Test: `tests/unit/test_bsl_diagnostics.py`
- Test: `tests/unit/test_bsl_diagnostic_acceptance.py`

**Interfaces:**
- Consumes: existing `MappedSource`, `VisibleSourceContext`, `WorkerDiagnosticArtifact` and `parse_platform_diagnostic`.
- Produces: `DiagnosticTextSpan`, `ParsedDiagnosticCause`, `ParsedDiagnosticFrame`, `ErrorTraceCause`, `ErrorTraceFrame`, `ErrorTraceFrameOrigin`, and `normalize_platform_diagnostic_trace(parsed, *, stage, executed=None, visible_source_context=None, pinned_manifest_sha256=None, pinned_artifacts=()) -> NormalizedDiagnostic`.

- [ ] **Step 1: Add failing parser and mapping tests.** Port the focused RU/EN, nested cause, anonymous, native, line-only, Worker, ambiguous-map and truncation cases from `c69b5e5:tests/unit/test_bsl_diagnostics.py`. Include this small smoke assertion in the current suite:

```python
raw = "Error getting value\n{Документ.ПриемНаРаботу.МодульОбъекта(15)}:Вызвать();\nReason:\nUninitialized value"
parsed = parse_platform_diagnostic(raw)
assert len(parsed.causes) == 2
assert parsed.frames[0].location.module_name == "Документ.ПриемНаРаботу.МодульОбъекта"
assert parsed.frames[0].location.column is None
assert parsed.platform_diagnostic == raw
```

- [ ] **Step 2: Verify red.** Run `uv run --group dev python -m pytest tests/unit/test_bsl_diagnostics.py tests/unit/test_bsl_diagnostic_acceptance.py -q`; expect the new trace assertions to fail because master lacks `causes` and `frames`.
- [ ] **Step 3: Port only the pure core.** Bring the trace dataclasses, bounded structural parser and per-frame mapping helpers from `c69b5e5:src/onec_runtime/bsl/diagnostics.py` into the current file. Keep the public call shape:

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
    if not isinstance(parsed, ParsedPlatformDiagnostic):
        raise ValueError("parsed must be a ParsedPlatformDiagnostic")
    if type(stage) is not DiagnosticStage:
        raise ValueError("stage must be a DiagnosticStage")
    if executed is not None and not isinstance(executed, MappedSource):
        raise ValueError("executed must be a MappedSource or None")
    if visible_source_context is not None and not isinstance(
        visible_source_context, VisibleSourceContext,
    ):
        raise ValueError("visible_source_context must be a VisibleSourceContext")
    if executed is None and visible_source_context is not None:
        raise ValueError("visible source context requires an executed artifact")
    if pinned_manifest_sha256 is not None and type(pinned_manifest_sha256) is not str:
        raise ValueError("pinned manifest identity must be a string or None")
    if type(pinned_artifacts) is not tuple:
        raise ValueError("pinned artifacts must be an immutable tuple")
    if pinned_manifest_sha256 is None and pinned_artifacts:
        raise ValueError("pinned artifacts require a manifest identity")
    if pinned_manifest_sha256 is not None:
        _validate_worker_diagnostic_request(
            parsed, pinned_manifest_sha256, pinned_artifacts,
        )
    if pinned_manifest_sha256 is not None and (
        executed is None
        or (
            bool(parsed.locations)
            and parsed.locations[0].worker_artifact_location is not None
        )
    ):
        base = _remap_worker_runtime_primary(
            parsed, stage=stage,
            pinned_manifest_sha256=pinned_manifest_sha256,
            pinned_artifacts=pinned_artifacts,
        )
    elif executed is not None:
        base = _remap_platform_primary(
            parsed, executed, stage=stage,
            visible_source_context=visible_source_context,
        )
    else:
        base = _generic_trace_base(parsed, stage)
    causes = tuple(
        ErrorTraceCause(
            item.ordinal, item.summary_span, item.block_span,
            item.frame_ordinals, item.category,
        )
        for item in parsed.causes
    )
    frames, worker_frames = _normalize_trace_frames(
        parsed, executed=executed,
        visible_source_context=visible_source_context,
        pinned_manifest_sha256=pinned_manifest_sha256,
        pinned_artifacts=pinned_artifacts,
    )
    return replace(
        base, causes=causes, frames=frames,
        frames_truncated=parsed.frames_truncated,
        causes_truncated=parsed.causes_truncated,
        worker_frames=(
            base.worker_frames
            if executed is None and pinned_manifest_sha256 is not None
            else worker_frames
        ),
    )
```

The body is the corresponding tested implementation from commit `c69b5e5`, not a new runtime lookup. Preserve the old single-primary `remap_platform_diagnostic` and `remap_worker_stage_diagnostic` behavior while adding ordered trace fields. The UTF-8 limiter runs before structural parsing and hashes the full input.
- [ ] **Step 4: Verify green.** Run the two focused modules again and `uv run --group dev python -m pytest tests/unit/test_worker_universe.py -q`; inspect any failures caused by the new trace fields before moving on.
- [ ] **Step 5: Commit.** Stage only the three files above and commit `feat: port bounded BSL diagnostic trace core` after `git diff --check`.

### Task 2: Contract validation and verbatim private evidence

**Files:**
- Modify: `src/onec_runtime/runtime_contracts.py`
- Modify: `src/onec_runtime/privacy.py`
- Modify: `packages/mcp/src/onec_runtime_mcp/agent/operations.py` and `packages/mcp/src/onec_runtime_mcp/server.py` only where the existing private/expert validators still impose 4 KiB or rewrite 1C text
- Test: `tests/unit/test_runtime_contract_boundaries.py`
- Test: `tests/unit/test_mcp_profiles.py`
- Test: `tests/unit/test_mcp_server.py`
- Test: `tests/unit/test_agent_operation_view.py`

**Interfaces:**
- Consumes: Task 1 trace dataclasses and `NormalizedDiagnostic`.
- Produces: `MAX_PRIVATE_DIAGNOSTIC_BYTES = 65_536`; `sanitize_normalized_diagnostic(value) -> NormalizedDiagnostic | None` validates every cause/frame/span; `bounded_platform_diagnostic(value: str | None, *, truncated: bool, redacted: bool) -> tuple[str | None, bool, bool]` preserves UTF-8-bounded private text verbatim.

- [ ] **Step 1: Add failing boundary tests.** Transfer the trace validation cases from `c69b5e5:tests/unit/test_runtime_contract_boundaries.py` and test multibyte truncation and unchanged public keys:

```python
raw = "Ж" * 40_000
bounded, truncated, redacted = bounded_platform_diagnostic(
    raw, truncated=False, redacted=False,
)
assert len(bounded.encode("utf-8")) <= 65_536
assert truncated is True
assert redacted is False
assert bounded == "Ж" * (65_536 // 2)
```

- [ ] **Step 2: Verify red.** Run `uv run --group dev python -m pytest tests/unit/test_runtime_contract_boundaries.py tests/unit/test_mcp_profiles.py tests/unit/test_mcp_server.py tests/unit/test_agent_operation_view.py -q`; the 4 KiB master bound must fail the new assertion.
- [ ] **Step 3: Port validation and bound.** Reuse `c69b5e5` validation rules in `runtime_contracts.py`; reject wrong nested types, nonmonotonic spans, impossible origin/mapping combinations, oversized text and frame/cause counts without raising. In `privacy.py`, replace the expert-text rewrite with byte-bounded verbatim slicing while leaving public wire keys unchanged:

```python
encoded = value.encode("utf-8")
bounded = encoded[:MAX_PRIVATE_DIAGNOSTIC_BYTES].decode("utf-8", errors="ignore")
return bounded, truncated or len(encoded) > MAX_PRIVATE_DIAGNOSTIC_BYTES, redacted
```

- [ ] **Step 4: Verify green.** Run the Task 2 tests plus `tests/unit/test_bsl_diagnostics.py`; ensure public serialization still omits platform text and source strings. If the existing MCP expert validator has a 4 KiB cap, adjust that validator to `MAX_PRIVATE_DIAGNOSTIC_BYTES`; keep the exact existing wire key set and do not add an MCP expert tool.
- [ ] **Step 5: Commit.** Stage only touched Task 2 files and commit `feat: validate bounded private BSL trace evidence` after `git diff --check`.

### Task 3: Read Worker evidence from the existing operation pin

**Files:**
- Modify: `src/onec_runtime/worker_universe.py`
- Modify: `src/onec_runtime/execution/worker_activation.py`
- Modify: `src/onec_runtime/execution/controller/controller.py`
- Modify: `src/onec_runtime/execution/reply_publication.py` (optional evidence fields only)
- Test: `tests/unit/test_worker_universe.py`
- Test: `tests/unit/test_execution_controller_routes.py`
- Test: `tests/unit/test_execution_reply_publication.py`

**Interfaces:**
- Consumes: `OperationGenerationPin`, its manifest and retained `WorkerGenerationDebugView`.
- Produces: `WorkerUniverseRegistry.diagnostic_artifacts_for_pin(pin: OperationGenerationPin) -> tuple[WorkerDiagnosticArtifact, ...]`. Confirmed failed `MainYield`, `MainConfirmedDecodeFailure`, and `CaptureRemoteOutcome` carry optional `pinned_manifest_sha256` and `pinned_artifacts` fields with empty defaults; successful outcomes leave defaults untouched.

- [ ] **Step 1: Add a pin-fencing regression.** Extend `test_old_generation_debug_view_lives_until_last_pin_release` in `test_worker_universe.py`. After promoting generation 2 while holding generation 1's pin, assert:

```python
evidence = host.diagnostic_artifacts_for_pin(pin)
assert evidence
assert {item.manifest_sha256 for item in evidence} == {first.manifest.sha256}
assert {item.registration_name for item in evidence} == {
    module.registration_name for module in first.manifest.modules
}
```

Also assert a released pin raises `ProtocolError`, and add controller spies showing evidence retrieval is zero for success and exactly once for a failed MAIN/CAPTURE result.
- [ ] **Step 2: Verify red.** Run `uv run --group dev python -m pytest tests/unit/test_worker_universe.py tests/unit/test_execution_controller_routes.py tests/unit/test_execution_reply_publication.py -q`; expect the new pin method and outcome fields to be absent.
- [ ] **Step 3: Add the read-only pin adapter.** Under the registry's existing lease validation, convert the retained debug view and its matching manifest descriptors:

```python
view = self._operation_debug_view(pin)
return tuple(
    WorkerDiagnosticArtifact(
        logical_name=descriptor.logical_name,
        revision=descriptor.revision,
        artifact_sha256=module.artifact_sha256,
        registration_name=descriptor.registration_name,
        manifest_sha256=view.manifest.sha256,
        source_map_sha256=module.source_map_sha256,
        mapped_source=module.mapped_source,
        visible_source_context=module.visible_context,
    )
    for descriptor, module in zip(view.manifest.modules, view.modules, strict=True)
)
```

`WorkerUniverseActivationAdapter` accepts its own `_GenerationLease`, checks `lease.pin` is present, and delegates to the registry. Do not call `pin_active` again.
- [ ] **Step 4: Carry evidence only on confirmed error.** In controller MAIN completion (including `MainConfirmedDecodeFailure` after CAPTURE) use `_main_worker_leases[operation.command_id]`; in the CAPTURE ticket use the existing local `lease` before its dependent release ticket. Set the optional outcome fields only when the remote error string is nonempty or `EvaluationResult.error_occurred` is true. On any evidence failure leave the fields empty; do not fail the confirmed cell. A compact adapter boundary is:

```python
if remote_error and lease is not None and lease.pin is not None:
    manifest_sha256 = lease.pin.handle.manifest_sha256
    pinned_artifacts = activation.diagnostic_artifacts_for_lease(lease)
```

- [ ] **Step 5: Verify green and commit.** Run the three Task 3 modules plus `tests/unit/test_runtime_rdbg_single_owner.py`; commit `feat: retain exact Worker diagnostic evidence on failures` after `git diff --check`.

### Task 4: Publish full diagnostics for MAIN and CAPTURE failures

**Files:**
- Modify: `src/onec_runtime/execution/reply_publication.py`
- Test: `tests/unit/test_execution_reply_publication.py`
- Test: `tests/unit/test_cell_execution_pipeline.py`
- Test: `tests/unit/test_capture_stop_recovery_component.py`

**Interfaces:**
- Consumes: Task 1 `normalize_platform_diagnostic_trace`; Task 3 optional pinned evidence in confirmed outcomes; existing publication records retain the executed source and visible context.
- Produces: failed `RuntimeReply.diagnostic` with ordered causes/frames while `RuntimeReply.error`, `succeeded`, operation phase and cleanup remain unchanged.

- [ ] **Step 1: Add failing publication tests.** Extend existing MAIN completion and CAPTURE result tests with a native frame followed by a mapped notebook frame, a native-only failure with no `executed_source`, and a late MAIN error after CAPTURE. Assert both mixed frames survive in platform order and that diagnostic failure does not mask the BSL failure:

```python
assert reply.succeeded is False
assert reply.error == "BSL execution failed"
assert [frame.origin.value for frame in reply.diagnostic.frames] == [
    "native_module", "executed_artifact",
]
```

Spy on `parse_platform_diagnostic` and the Worker evidence collaborator in success cases; assert both have zero calls.
- [ ] **Step 2: Verify red.** Run `uv run --group dev python -m pytest tests/unit/test_execution_reply_publication.py tests/unit/test_cell_execution_pipeline.py tests/unit/test_capture_stop_recovery_component.py -q`; the new trace and success-path assertions must fail before the publisher change.
- [ ] **Step 3: Replace only the error path.** Keep `_execution_diagnostic` in `reply_publication.py` pure and error-gated. Parse even when `source is None`, so native frames remain visible; only an empty error returns `None` before parsing. Its failure body calls:

```python
return normalize_platform_diagnostic_trace(
    parse_platform_diagnostic(error),
    stage=DiagnosticStage.EXECUTION,
    executed=source,
    visible_source_context=visible,
    pinned_manifest_sha256=pinned_manifest_sha256,
    pinned_artifacts=pinned_artifacts,
)
```

Wrap only diagnostic enrichment in a failure-isolation guard that returns `None`; do not alter MAIN/CAPTURE success, result decoding, ledger settlement, or pin release. The existing Worker stage-error path in `worker_universe.py` uses the Task 1 parser without a second RDBG operation.
- [ ] **Step 4: Verify green and commit.** Run the three Task 4 modules plus `tests/unit/test_worker_universe.py` and `tests/unit/test_runtime_contract_boundaries.py`; commit `feat: publish mapped MAIN and CAPTURE error stacks` after `git diff --check`.

### Task 5: Render detailed trace in Jupyter and optional source links

**Files:**
- Create: `packages/jupyter/src/onec_runtime_jupyter/diagnostic_trace.py`
- Create: `packages/jupyter/src/onec_runtime_jupyter/diagnostic_sources.py`
- Modify: `packages/jupyter/src/onec_runtime_jupyter/extension.py`
- Test: `tests/unit/test_jupyter_adapter.py`
- Test: `tests/unit/test_runtime_source_root_config.py`

**Interfaces:**
- Consumes: validated `NormalizedDiagnostic`, current cell `SourceUnitRef` and source text, optional configured source root.
- Produces: `render_error_trace(diagnostic, *, heading, visible_source, source_unit, source_root=None) -> str | None` for human display only, and `DiagnosticSourceFiles.line_excerpt(frame: ErrorTraceFrame, platform_detail: str | None) -> str | None` for a matched local line. Public JSON remains compact; diagnostic mode retains bounded original 1C text.

- [ ] **Step 1: Add failing adapter tests.** Port display/fence cases from `c69b5e5:tests/unit/test_jupyter_adapter.py`. Assert Russian/English causes, ordered native and mapped frames, explicit platform fragment, raw text in diagnostic mode, no raw text in public payload, stale source hash suppression, and optional root hint. For a native local excerpt, require its stripped line to match the 1C detail fragment; a divergent local export keeps the platform fragment but shows no local excerpt. A minimal text assertion is:

```python
assert "Причины:" in display.text
assert "Стек (1С):" in display.text
assert "фрагмент 1С:" in display.text
assert "Исходное сообщение 1С:" in display.text
assert "platform_diagnostic" not in display.payload.get("diagnostic", {})
```

- [ ] **Step 2: Verify red.** Run `uv run --group dev python -m pytest tests/unit/test_jupyter_adapter.py tests/unit/test_runtime_source_root_config.py -q`; new trace-rendering assertions must fail.
- [ ] **Step 3: Port the two small helpers.** Adapt `c69b5e5:packages/jupyter/src/onec_runtime_jupyter/diagnostic_trace.py` and `diagnostic_sources.py` with `apply_patch`. Continue to validate through `sanitize_normalized_diagnostic`, verify `source_sha256(visible_source) == source_unit.source_sha256` before displaying an original line, and use `ConfigurationSourceLayout.safe_path` for optional file hints. Native frames display 1C coordinates and detail without a root. A local file line is labelled as a local export and shown only if it matches the stripped 1C detail fragment; otherwise show only the platform fragment. A local file mismatch removes only the optional hint or excerpt:

```python
detail = _span_text(evidence, frame.detail_span)
try:
    hint = source_files.hint(frame) if source_files is not None else None
    local_line = (
        source_files.line_excerpt(frame, detail)
        if source_files is not None else None
    )
except BaseException:
    hint = None
    local_line = None
```

The helper's native line check uses the already resolved relative hint, not a
second search:

```python
relative = self.hint(frame)
if relative is None or platform_detail is None:
    return None
path = self.layout.safe_path(self.layout.normalized_root / relative)
lines = path.read_text(encoding="utf-8-sig").splitlines()
line = frame.platform_location.line
if not 1 <= line <= len(lines):
    return None
local = lines[line - 1].strip()
return local if local and local == platform_detail.strip() else None
```

- [ ] **Step 4: Integrate at the current display boundary.** Call `render_error_trace` only after a failed `RuntimeReply` has passed source-unit fencing in `extension.py`; use the session's configured capture source root when present. Keep Python `_render_traceback_` compact and do not include raw text in the public MIME payload.
- [ ] **Step 5: Verify green and commit.** Run Task 5 tests plus `tests/unit/test_jupyter_capture_display.py`; commit `feat: show full mapped BSL trace in notebooks` after `git diff --check`.

### Task 6: Regression and live qualification

**Files:**
- Test: `tests/integration/test_worker_universe_1c.py` only if an opt-in fixture needs a compact assertion; do not save raw live evidence.
- Modify: `docs/superpowers/specs/2026-09-18-bsl-error-diagnostics-main-design.md` only to record measured deviations from the approved design.

**Interfaces:**
- Consumes: Tasks 1–5 completed and committed.
- Produces: test evidence and a final status report distinguishing static from live qualification.

- [ ] **Step 1: Re-run focused and full static tests.** Run:

```powershell
uv run --group dev python -m pytest tests/unit/test_bsl_diagnostics.py tests/unit/test_execution_reply_publication.py tests/unit/test_jupyter_adapter.py tests/unit/test_worker_universe.py -q
uv run --group dev python -m pytest tests/unit -q
```

- [ ] **Step 2: Check adjacent adapter and contract behavior.** Run `uv run --group dev python -m pytest tests/unit/test_mcp_profiles.py tests/unit/test_mcp_server.py tests/unit/test_agent_operation_view.py tests/unit/test_runtime_contract_boundaries.py -q`. Check `git diff --check`, `git status --short`, and the exact changed-file list against the ownership map. Do not add an MCP expert tool/schema.
- [ ] **Step 3: Qualify against 1C only when available.** Use the repository's existing opt-in live harness with disposable infobases. Exercise Russian/English failures, nested causes, MAIN completion after CAPTURE, CAPTURE failure then successful retry, and a successful-cell diagnostic-call spy. Keep platform output outside the repository; if the platform is unavailable, report live qualification as not run rather than passing.
- [ ] **Step 4: Review and hand off.** Request code review, resolve findings with focused reruns, then report commits, test counts, live results or limitation, and the remaining deferred MCP expert issue. Do not merge master without a separate request.
