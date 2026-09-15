# Capture Inspection API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only, debugger-like Python CAPTURE API and make acknowledged CAPTURE evaluations survive caller timeout or interruption without poisoning the runtime or losing their late result.

**Architecture:** `src/onec_runtime` owns typed capture views, evaluation lifecycle, source resolution, syntax indexing, value policy, and bounded RDBG plans. A controller-owned `CaptureEvaluationCoordinator` is the only CAPTURE `evalExpr` event consumer and keeps its own short-lived control lock; Jupyter only renders snapshots and adapts pending outcomes, while MCP keeps its existing public tools through a private adapter. The work lands in lifecycle-first order so issue #5 behavior is independently testable before stack and value inspection are added.

**Tech Stack:** Python 3.12, pytest, RDBG fake transports, existing BSL semantic parser/projection, IPython/Jupyter display adapters.

**Spec:** [`docs/superpowers/specs/2026-09-15-capture-inspection-api-design.md`](../specs/2026-09-15-capture-inspection-api-design.md)

## Global Constraints

- Keep issue [#5](https://github.com/pulh1/bsl-jupyter-runtime/issues/5) open. This implementation covers coordinator ownership, safe `status()`/`wait()`, late results, absence of redispatch, and correct guard errors. It does not claim the RDBG stall root cause, guard caching or the wider `to_df()` pipeline, in-place uncertain-dispatch recovery, or live 1C qualification.
- `src/onec_runtime` must not import Jupyter or MCP. Frontends consume core contracts.
- Every public page is finite and immutable. `repr`, `str`, `_repr_html_` and page redisplay perform no RDBG, file-system, or parser work.
- Preserve capture fences, Worker privacy, output budgets, and single-writer admission semantics. No public diagnostic contains BSL source, target URLs, value handles, RDBG result IDs, module UUIDs, or Worker generation data.
- No compatibility layer is required for old public capture dictionaries. Existing internal MCP consumers must be migrated or routed through a private adapter in the same change.
- Source resolution must preserve the runtime's existing two-format contract: Designer export and EDT with either the project root or its `src` directory configured. New stack behavior must work for both formats.
- Unit and fake-transport tests are not live 1C qualification. Do not enable or report live tests unless explicitly run against a disposable infobase.

## Thread and Lock Ownership

Implement these rules before adding any new inspection behavior:

| Participant | Owns | Must never do |
| --- | --- | --- |
| Initiating Python/Jupyter thread | Validate arguments, acquire ordinary operation admission, create the generation-pin lease, submit one coordinator record, and wait as an initiating waiter | Hold `RuntimeSession._operation_lock` or `RuntimeApi._single_writer` while waiting for RDBG, workspace restoration, or a late result |
| `CaptureEvaluationCoordinator` worker thread | Dispatch every CAPTURE `evalExpr`, own the returned `PendingEvaluation`, poll the RDBG event stream, retain the generation pin and cleanup leases, restore workspace, and publish one outcome | Acquire `RuntimeSession._operation_lock` or `RuntimeApi._single_writer`; execute concurrent CAPTURE evaluations; expose private request data through status or logs |
| Control-plane callers | Read `current_capture()`, `status()` and `wait()` from the coordinator snapshot/condition | Call `_require_available()`, run a Worker privacy guard, acquire a data-plane lock, or poll RDBG |
| Shutdown caller | Mark coordinator closing, invalidate the target/transport to stop polling, join with a finite deadline, then release quarantined resources | Wait indefinitely for the user-operation lock or release a pin while the coordinator may still consume a late event |

Locking rules:

1. `RuntimeSession._operation_lock` and `RuntimeApi._single_writer` remain admission locks for data-plane operations. Submission must return a ticket before either lock is released; the caller then waits outside both locks.
2. Add a dedicated `CaptureEvaluationCoordinator._condition`. It protects records, phase, bounded timing, waiter state, and shutdown flags. `Condition.wait()` is the only permitted wait while using it and releases the mutex; no RDBG, file, parser, cleanup, or callback work runs while the mutex is held.
3. Protect RuntimeApi generation-pin slots with a narrow `_generation_lock`. The coordinator receives a lease object at submission and releases or quarantines it outside `_condition`. Do not hold `_generation_lock` across RDBG work.
4. When admission needs more than one lock, use the order `RuntimeSession._operation_lock` → `RuntimeApi._single_writer` → `_generation_lock` → coordinator `_condition`, releasing each as soon as its state transition is complete. The coordinator worker only takes `_condition` for snapshots and `_generation_lock` for an isolated lease transition, never both together.
5. A controller phase/data-plane reservation, not a mutex held by the initiating thread, prevents overlapping operations while phase is `evaluating` or `resuming`. Rejected calls receive the typed lifecycle error before any privacy guard or dispatch.
6. The coordinator is the sole consumer of a pending CAPTURE capability. Observer `capture.wait()` calls wait on `_condition`; they never call `RdbgSession.wait_evaluation_event()`.

## Error Naming Decision

Use `CaptureEvaluationPendingError(evaluation_id, evaluation_kind)` when an internal evaluation such as the `to_df()` public-value guard was acknowledged by RDBG but outlives its initiating caller's deadline. The exception says the coordinator still owns live work and the caller may use `capture.wait()`.

Reserve `CaptureInspectionTimeout` for a bounded inspection/read deadline after which no acknowledged evaluation remains pending. A `capture.wait()` timeout returns `CaptureEvaluationOutcome(state="pending")`; it does not raise either exception. This distinction must be covered by tests and retained in public documentation.

---

### Task 1: Add immutable lifecycle models and typed errors

**Files:**
- Create: `src/onec_runtime/capture_evaluation.py`
- Modify: `src/onec_runtime/errors.py`
- Create: `tests/unit/test_capture_evaluation_models.py`

- [ ] Write failing tests for the safe enums and immutable records: `CapturePhase`, `CaptureEvaluationKind`, `CaptureEvaluationState`, `CaptureEvaluationTiming`, `CaptureFailureDiagnostic`, `CaptureStatus`, and `CaptureEvaluationOutcome`.
- [ ] Test the phase/capability matrix from the spec and require timing offsets/counts to be non-negative, saturated at fixed maxima, and free of private fields.
- [ ] Write failing tests for `CaptureEvaluationPendingError`, `CaptureBusyError`, `CaptureOutcomeUnknownError`, `CaptureRecoveryRequiredError`, `NoActiveCaptureError`, `NoCaptureEvaluationError`, and `StaleCaptureError`. Assert that pending/busy errors expose only safe evaluation ID, kind, and phase.
- [ ] Run `uv run pytest tests/unit/test_capture_evaluation_models.py -q`; expect import/collection failures because the models do not exist.
- [ ] Implement frozen dataclasses/enums in `capture_evaluation.py` and the public errors in `errors.py`. Use explicit constructors and bounded/sanitized messages; do not put a request object or raw exception in a dataclass field or `repr`.
- [ ] Run `uv run pytest tests/unit/test_capture_evaluation_models.py -q`; expect all tests to pass.
- [ ] Commit with `git add src/onec_runtime/capture_evaluation.py src/onec_runtime/errors.py tests/unit/test_capture_evaluation_models.py && git commit -m "feat: define capture evaluation lifecycle"`.

### Task 2: Build the single-owner evaluation coordinator

**Files:**
- Modify: `src/onec_runtime/capture_evaluation.py`
- Modify: `src/onec_runtime/rdbg/session.py`
- Create: `tests/unit/test_capture_evaluation_coordinator.py`
- Modify: `tests/unit/rdbg/test_session.py`

Define the internal surface before implementation:

```python
class CaptureEvaluationCoordinator:
    def submit(self, request: CaptureEvaluationRequest) -> CaptureEvaluationTicket: ...
    def status(self, fence: CaptureFence) -> CaptureStatus: ...
    def wait(self, fence: CaptureFence, evaluation_id: str | None, timeout_s: float | None) -> CaptureEvaluationOutcome: ...
    def begin_close(self) -> None: ...
    def join(self, timeout_s: float) -> bool: ...
```

`CaptureEvaluationRequest` holds private dispatch, polling, restoration, result-policy, continuation, pin-lease, and cleanup-lease callbacks with `repr=False`. `CaptureEvaluationTicket.wait_initiator()` is distinct from observer `wait()` so only initiator timeout/interruption abandons downstream continuation.

- [ ] Write a fake RDBG driver test where `start_evaluation()` returns a `PendingEvaluation`, polling repeatedly raises interval `CommandTimeout`, the initiator detaches, and a late result is consumed exactly once by the coordinator worker.
- [ ] Add tests for one active record, multiple observer waiters, repeated pending waits, an observer timeout that does not detach the initiator, and rejection of a second submission without dispatch.
- [ ] Add tests for all phase evidence events, bounded poll coalescing, retained last-user plus observed/internal outcome slots, result-policy failure, restoration failure, and an unexpected stop.
- [ ] Add an RDBG-session contract test proving `wait_evaluation_event()` timeout leaves the registered capability pending and a later call can consume its result.
- [ ] Run `uv run pytest tests/unit/test_capture_evaluation_coordinator.py tests/unit/rdbg/test_session.py -q`; expect coordinator imports/tests to fail.
- [ ] Implement the coordinator with one dedicated daemon worker, one active record, a queue/condition, bounded polling intervals, and immutable published snapshots. Record `created`, `dispatch_entered`, `rdbg_acknowledged`, `initiating_waiter_detached`, `last_poll`, `result_received`, `workspace_restored`, and `outcome_published` evidence.
- [ ] Keep dispatch-before-acceptance exceptions distinct from acknowledged pending work. Interval `CommandTimeout` after acknowledgement updates `last_poll` and loops over the same capability.
- [ ] Run the focused tests; expect all to pass and no test thread to survive teardown.
- [ ] Commit with `git add src/onec_runtime/capture_evaluation.py src/onec_runtime/rdbg/session.py tests/unit/test_capture_evaluation_coordinator.py tests/unit/rdbg/test_session.py && git commit -m "feat: coordinate capture evaluations"`.

### Task 3: Move controller CAPTURE evaluation and restoration under the coordinator

**Files:**
- Modify: `src/onec_runtime/prototype_runtime.py`
- Modify: `tests/unit/test_prototype_runtime.py`
- Create: `tests/unit/test_capture_evaluation_lifecycle.py`

- [ ] Write failing tests for user `%%bsl`: acknowledged/no-result timeout, `KeyboardInterrupt`, late success, late BSL error, workspace restoration failure, target loss, and genuinely uncertain dispatch.
- [ ] Assert acknowledged pending work leaves `OperationState.EVALUATING_CAPTURE`, keeps one `PendingEvaluation`, performs no redispatch, and does not clear its workspace before a result. Assert uncertain acceptance alone maps to `outcome_unknown`.
- [ ] Run `uv run pytest tests/unit/test_capture_evaluation_lifecycle.py tests/unit/test_prototype_runtime.py -q`; expect current synchronous `_execute_capture()` behavior to fail the new lifecycle assertions.
- [ ] Replace direct wait ownership in `PrototypeRuntimeController._execute_capture()` and `_handle_capture_evaluation_event()` with a coordinator request whose worker invokes `RdbgSession.start_evaluation()`, polls the returned capability, restores the exact workspace, and publishes the result.
- [ ] Route controller-owned CAPTURE helpers currently calling `RdbgSession.evaluate()` through the same coordinator and label each request `user_bsl`, `inspection`, or `materialization_helper` explicitly.
- [ ] Keep public phase conversion in one controller snapshot function; do not infer kind from generated BSL.
- [ ] Run the focused tests; expect all to pass.
- [ ] Commit with `git add src/onec_runtime/prototype_runtime.py tests/unit/test_prototype_runtime.py tests/unit/test_capture_evaluation_lifecycle.py && git commit -m "refactor: give coordinator capture event ownership"`.

### Task 4: Split data-plane admission from waiting and expose the control plane

**Files:**
- Modify: `src/onec_runtime/runtime_api.py`
- Modify: `src/onec_runtime/session.py`
- Create: `src/onec_runtime/capture_inspection.py`
- Create: `tests/unit/test_capture_control_plane.py`
- Modify: `tests/unit/test_runtime_api.py`

Introduce the first public view surface:

```python
class CaptureView:
    def status(self) -> CaptureStatus: ...
    def wait(self, timeout_s: float | None = None, evaluation_id: str | None = None) -> CaptureEvaluationOutcome: ...

class RuntimeSession:
    def current_capture(self) -> CaptureView: ...
```

- [ ] Write a threaded regression proving the initiating caller can be blocked or interrupted while another thread calls `runtime.current_capture()`, `capture.status()`, and `capture.wait(timeout_s=0)` without acquiring `_operation_lock`, `_single_writer`, `_require_available()`, or a Worker guard.
- [ ] Add tests that acknowledged pending work keeps the generation pin and leaves `_poisoned_error` unset; pre-acceptance ambiguity still calls `retain_outcome_unknown()` and poisons/quarantines according to the existing promotion contract.
- [ ] Add capture-fence tests across `operation_id`, capture generation, and `stop_sequence`, plus absent/stale/paused/evaluating/resuming/recovery/unknown states.
- [ ] Run `uv run pytest tests/unit/test_capture_control_plane.py tests/unit/test_runtime_api.py -q`; expect lock-independent access and pin ownership assertions to fail.
- [ ] Split `RuntimeSession.execute_bsl()` and RuntimeApi CAPTURE execution into short admission/submission and waiter phases. Release `_operation_lock` and `_single_writer` before `wait_initiator()`; rely on the controller phase as the data-plane reservation.
- [ ] Add `_generation_lock` and transfer the pin lease to the coordinator record before transport dispatch. Remove the acknowledged-pending path from `_finish_capture_evaluation_pin_locked()`/`retain_outcome_unknown()`; preserve it only for uncertain acceptance.
- [ ] Implement `RuntimeSession.current_capture()` and the status/wait path as direct coordinator control-plane calls with fence checks. Export only the typed view from the session, not raw controller records.
- [ ] Run the focused tests; expect all to pass.
- [ ] Commit with `git add src/onec_runtime/runtime_api.py src/onec_runtime/session.py src/onec_runtime/capture_inspection.py tests/unit/test_capture_control_plane.py tests/unit/test_runtime_api.py && git commit -m "feat: expose lock-independent capture status"`.

### Task 5: Make shutdown finite while an evaluation is pending

**Files:**
- Modify: `src/onec_runtime/runtime_api.py`
- Modify: `src/onec_runtime/session.py`
- Modify: `src/onec_runtime/prototype_runtime.py`
- Modify: `tests/unit/test_capture_control_plane.py`
- Modify: `tests/unit/test_jupyter_session_shutdown.py`

- [ ] Write a failing test with a coordinator poll that never returns. Call normal close and kernel-shutdown close from another thread and assert both finish within the configured deadline.
- [ ] Assert shutdown marks the coordinator closing, rejects new submissions, invalidates/stops the target transport, joins the event consumer, and only then releases or quarantines pin/temporary leases.
- [ ] Run `uv run pytest tests/unit/test_capture_control_plane.py tests/unit/test_jupyter_session_shutdown.py -q`; expect the existing single-writer shutdown path to block.
- [ ] Add `begin_close()` before ordinary data-plane lock acquisition, transport invalidation to wake polling, bounded `join()`, and safe abandoned-operation evidence when termination cannot be proven.
- [ ] Run the focused tests; expect all to pass with no leaked thread.
- [ ] Commit with `git add src/onec_runtime/runtime_api.py src/onec_runtime/session.py src/onec_runtime/prototype_runtime.py tests/unit/test_capture_control_plane.py tests/unit/test_jupyter_session_shutdown.py && git commit -m "fix: bound capture coordinator shutdown"`.

### Task 6: Correct public-value guard lifecycle and the `to_df()` regression

**Files:**
- Modify: `src/onec_runtime/runtime_api.py`
- Modify: `src/onec_runtime/session.py`
- Modify: `packages/jupyter/src/onec_runtime_jupyter/extension.py`
- Modify: `tests/unit/test_runtime_api.py`
- Modify: `tests/unit/test_jupyter_value_proxy.py`
- Create: `tests/unit/test_capture_guard_lifecycle.py`

- [ ] Write the exact issue #5 regression: `OnecValueProxy.to_df()` starts a `public_value_guard`; fake RDBG acknowledges but withholds its result; the initiating deadline raises `CaptureEvaluationPendingError` or `KeyboardInterrupt`; schema and transfer counters remain zero; one record/capability/pin remains; the next cell observes it; a second `to_df()` raises `CaptureBusyError` without dispatch; late `False` restores `paused` without starting the abandoned transfer; MAIN resume is admitted.
- [ ] Add sibling cases for guard `True`, `False`, confirmed BSL failure, invalid result, uncertain dispatch, restoration failure, and interrupted initiator. Assert exact errors: access denied, value check, pending, outcome unknown, or recovery required.
- [ ] Assert a broad `BaseException` handler can never translate pending, interruption, busy, ambiguous dispatch, or restoration failure to `"Worker generation objects are not public values"`.
- [ ] Run `uv run pytest tests/unit/test_capture_guard_lifecycle.py tests/unit/test_runtime_api.py tests/unit/test_jupyter_value_proxy.py -q`; expect the current guard fallback and redispatch behavior to fail.
- [ ] Route `_execute_worker_instruction()` and `_require_public_value_handles_locked()` through coordinator submission with `evaluation_kind=PUBLIC_VALUE_GUARD`. Interpret only a confirmed Boolean; transfer temporary-handle cleanup leases to the coordinator on initiator detachment.
- [ ] Check capture lifecycle before `_require_available()` and privacy probing in proxy/materialization entry points. Raise `CaptureEvaluationPendingError` only for the initiating internal call whose acknowledged evaluation exceeded its deadline; every later data-plane call raises `CaptureBusyError` with the same safe ID/kind.
- [ ] Ensure late `False` runs mandatory workspace/handle cleanup and publishes a safe internal completion, but never invokes the abandoned table continuation.
- [ ] Run the focused tests; expect all to pass.
- [ ] Commit with `git add src/onec_runtime/runtime_api.py src/onec_runtime/session.py packages/jupyter/src/onec_runtime_jupyter/extension.py tests/unit/test_runtime_api.py tests/unit/test_jupyter_value_proxy.py tests/unit/test_capture_guard_lifecycle.py && git commit -m "fix: retain pending capture guard evaluations"`.

### Task 7: Adapt `%%bsl` pending outcomes without changing completed output

**Files:**
- Modify: `packages/jupyter/src/onec_runtime_jupyter/extension.py`
- Modify: `tests/unit/test_jupyter_adapter.py`
- Modify: `tests/unit/test_notebook_method_runtime.py`

- [ ] Write failing adapter tests for an acknowledged user-BSL evaluation that reaches the notebook command deadline and for `KeyboardInterrupt`. Require a bounded pending display containing evaluation ID, safe kind, and `runtime.current_capture().wait(...)` guidance; require no poison and no redispatch.
- [ ] Preserve existing display behavior for immediate completion and `BslExecutionError`.
- [ ] Run `uv run pytest tests/unit/test_jupyter_adapter.py tests/unit/test_notebook_method_runtime.py -q`; expect pending presentation tests to fail.
- [ ] Change the magic to submit and wait through the core ticket. Convert only deadline-pending to the pending display; allow `KeyboardInterrupt` to propagate after detaching the initiating waiter.
- [ ] Run the focused tests; expect all to pass.
- [ ] Commit with `git add packages/jupyter/src/onec_runtime_jupyter/extension.py tests/unit/test_jupyter_adapter.py tests/unit/test_notebook_method_runtime.py && git commit -m "feat: show pending capture evaluations in jupyter"`.

### Task 8: Extract a shared compact syntax index for hot reload and capture

**Files:**
- Create: `src/onec_runtime/bsl/module_syntax.py`
- Modify: `src/onec_runtime/bsl/full_ast_worker_projection.py`
- Modify: `src/onec_runtime/bsl/worker_projection_model.py`
- Modify: `src/onec_runtime/bsl/__init__.py`
- Create: `tests/unit/test_module_syntax_registry.py`
- Modify: `tests/unit/test_bsl_full_ast_worker_projection.py`
- Modify: `tests/unit/test_bsl_worker_projection_model.py`

Define compact immutable source facts:

```python
@dataclass(frozen=True)
class ModuleSyntaxIndex:
    source_sha256: str
    parser_identity: str
    methods: tuple[MethodSyntaxInfo, ...]

class ModuleSyntaxRegistry:
    def get(self, module: ModuleIdentity, source_sha256: str, parser_identity: str) -> ModuleSyntaxIndex | None: ...
    def publish(self, module: ModuleIdentity, index: ModuleSyntaxIndex) -> None: ...
```

- [ ] Write failing tests for ordered method spans/parameters, method lookup by line, registry version keys, and immutable publication.
- [ ] Count parser calls: hot reload parses once and publishes the compact index; capture lookup of the same exact hash/parser identity parses zero additional times.
- [ ] Add generation tests: publication of G2 does not retarget a MAIN stop pinned to G1; failed/unknown promotion does not activate candidate syntax.
- [ ] Run `uv run pytest tests/unit/test_module_syntax_registry.py tests/unit/test_bsl_full_ast_worker_projection.py tests/unit/test_bsl_worker_projection_model.py -q`; expect missing-index failures.
- [ ] Generalize the existing full-AST projection to emit both the current Worker model and reusable `ModuleSyntaxIndex` from the same parse. Keep full AST/tokens temporary.
- [ ] Publish only after successful hot-reload admission/promotion and retain versioned older entries.
- [ ] Run the focused tests; expect all to pass.
- [ ] Commit with `git add src/onec_runtime/bsl/module_syntax.py src/onec_runtime/bsl/full_ast_worker_projection.py src/onec_runtime/bsl/worker_projection_model.py src/onec_runtime/bsl/__init__.py tests/unit/test_module_syntax_registry.py tests/unit/test_bsl_full_ast_worker_projection.py tests/unit/test_bsl_worker_projection_model.py && git commit -m "feat: share module syntax indexes"`.

### Task 9: Resolve source modules lazily and pin source versions

**Files:**
- Create: `src/onec_runtime/configuration_source.py`
- Modify: `src/onec_runtime/bsl/module_catalog.py`
- Modify: `src/onec_runtime/capture_source.py`
- Modify: `src/onec_runtime/session.py`
- Create: `tests/unit/test_configuration_source_layout.py`
- Create: `tests/unit/test_capture_source_resolver.py`
- Modify: `tests/unit/test_bsl_module_catalog.py`
- Modify: `tests/unit/test_capture_source_configuration.py`

- [ ] Build paired synthetic source trees with the same UUIDs and BSL: Designer metadata/module paths and EDT `.mdo`/direct-module paths. Cover both a common module and `Documents/ПриемНаРаботу` object module, plus base/extension identity.
- [ ] Write failing root-layout tests for a Designer root, an EDT project root containing `src`, the EDT `src` root itself, unsafe links/junctions, and an ambiguous root containing both direct and nested metadata trees.
- [ ] Write failing tests for equivalent `(object_id, property_id, extension)` resolution, canonical module name/role, line mapping, batched lookup, early exit, and no full index at startup in both formats.
- [ ] Add counters proving one directory pass resolves a batch of unresolved stack modules, positive/negative cache keys include source-root identity plus catalog generation, and explicit `refresh_capture_sources()` invalidates negative results.
- [ ] Write source-version tests for Worker artifact/generation pins and trusted-export `(path, size, mtime)` signatures. Change a file before and during read and require `source_changed`.
- [ ] Run `uv run pytest tests/unit/test_configuration_source_layout.py tests/unit/test_capture_source_resolver.py tests/unit/test_bsl_module_catalog.py tests/unit/test_capture_source_configuration.py -q`; expect the common-module-only resolver to fail broader identities and paired-layout tests.
- [ ] Extract shared `ConfigurationSourceLayout` root normalization and Designer/EDT path rules into `configuration_source.py`; make `SessionCommonModuleCatalog` consume it so capture and hot reload cannot disagree about the configured root.
- [ ] Extend `capture_source.py` with `CaptureModuleResolver`, `CaptureSourceCatalog`, and immutable `SourceVersionRef`. Map property IDs to module roles through explicit metadata-kind tables, scan only relevant roots/kinds for the requested batch, and cache encountered descriptions.
- [ ] Add `RuntimeSession.refresh_capture_sources()` and successful-hot-reload invalidation/publication hooks. Do not add a background watcher.
- [ ] Run the focused tests; expect all to pass.
- [ ] Commit with `git add src/onec_runtime/configuration_source.py src/onec_runtime/bsl/module_catalog.py src/onec_runtime/capture_source.py src/onec_runtime/session.py tests/unit/test_configuration_source_layout.py tests/unit/test_capture_source_resolver.py tests/unit/test_bsl_module_catalog.py tests/unit/test_capture_source_configuration.py && git commit -m "feat: resolve capture sources on demand"`.

### Task 10: Add fast and method-enriched stack pages

**Files:**
- Modify: `src/onec_runtime/capture_inspection.py`
- Modify: `src/onec_runtime/runtime_api.py`
- Modify: `src/onec_runtime/session.py`
- Modify: `tests/unit/test_capture_lazy_frames.py`
- Create: `tests/unit/test_capture_stack_views.py`

Public interfaces:

```python
capture.stack[:20]
capture.stack[0]
capture.stack.native[:20]
stack_page.with_methods(timeout_s=None)
frame.with_method(timeout_s=None)
frame.variables[:20]
frame.parameters[:20]
frame.locals[:20]
```

- [ ] Write failing tests for fresh native inventories on repeated slices, visible-to-native level mapping, collapsed runtime markers, unknown module fallback, and exact fence validation.
- [ ] Add a parser spy proving basic stack construction and rendering perform zero AST calls and native mode performs no directory or AST work.
- [ ] Add enrichment tests proving saved frames are not reread from RDBG, unique missing versions parse once, registry entries parse zero times, source changes return per-frame `source_changed`, and deadline produces partial `method_status="timeout"` without changing CAPTURE phase.
- [ ] Run `uv run pytest tests/unit/test_capture_lazy_frames.py tests/unit/test_capture_stack_views.py -q`; expect typed view/import failures.
- [ ] Implement immutable `StackPage`, `DebugFrame`, `RuntimeFrameMarker`, visible/native stack descriptors, safe physical identity, and source-version pins. Keep UUID/private identity out of ordinary display.
- [ ] Implement local-only `with_methods()`/`with_method()` using the saved page plus `ModuleSyntaxRegistry`; cap its deadline by runtime command timeout.
- [ ] Run the focused tests; expect all to pass.
- [ ] Commit with `git add src/onec_runtime/capture_inspection.py src/onec_runtime/runtime_api.py src/onec_runtime/session.py tests/unit/test_capture_lazy_frames.py tests/unit/test_capture_stack_views.py && git commit -m "feat: add capture stack views"`.

### Task 11: Add context views, safe paths, and bounded value expansion

**Files:**
- Create: `src/onec_runtime/capture_values.py`
- Modify: `src/onec_runtime/capture_inspection.py`
- Modify: `src/onec_runtime/runtime_api.py`
- Modify: `src/onec_runtime/prototype_runtime.py`
- Create: `tests/unit/test_capture_context_views.py`
- Create: `tests/unit/test_capture_value_expansion.py`
- Modify: `tests/unit/test_worker_breakpoint_privacy.py`

Implement only the advertised shapes: capture context, native frame, `Структура`, `ФиксированнаяСтруктура`, `Массив`, `ФиксированныйМассив`, `ТаблицаЗначений`, and value-table row.

- [ ] Write failing tests for `capture.context.variables`, `.parameters`, `.locals`, case-insensitive exact lookup, source-order parameters, ambiguity/missing errors, and fallback guidance when method source is unavailable.
- [ ] Prove staged context and native frame semantics: a BSL rebinding appears on a new context page, the old page stays unchanged, and a new native-frame page keeps the original binding until writeback.
- [ ] Add protocol fixtures and work counters for every supported shape. Test finite slices only: non-negative start/stop, no step, at most 100 items/fields/rows/columns, and preview length at most 512 characters.
- [ ] Add rejection tests for arbitrary expressions, unsafe/fabricated paths, maps, value trees, property-bearing application objects, undocumented layouts, and over-wide tables before row values are fetched.
- [ ] Add privacy tests for direct Worker values, aliases, and nested descendants. Require lifecycle errors to win before privacy checking and require denied page entries to redact type/preview and be non-expandable.
- [ ] Run `uv run pytest tests/unit/test_capture_context_views.py tests/unit/test_capture_value_expansion.py tests/unit/test_worker_breakpoint_privacy.py -q`; expect missing-view/adapter failures.
- [ ] Implement frozen safe-path segments, `ValueNode`, `ValuePage`, variable collections, shape adapters, and `CaptureValuePolicy`. Every fresh slice reprojects its live safe path; values and mutable membership are never cached.
- [ ] Route every target-side projection/cleanup step through the coordinator with explicit `inspection` or `materialization_helper` kind. Normalize only after every exposed value passes the public guard.
- [ ] Run the focused tests; expect all to pass.
- [ ] Commit with `git add src/onec_runtime/capture_values.py src/onec_runtime/capture_inspection.py src/onec_runtime/runtime_api.py src/onec_runtime/prototype_runtime.py tests/unit/test_capture_context_views.py tests/unit/test_capture_value_expansion.py tests/unit/test_worker_breakpoint_privacy.py && git commit -m "feat: inspect capture context values"`.

### Task 12: Add side-effect-free Jupyter rendering

**Files:**
- Create: `packages/jupyter/src/onec_runtime_jupyter/capture_display.py`
- Modify: `packages/jupyter/src/onec_runtime_jupyter/extension.py`
- Create: `tests/unit/test_jupyter_capture_display.py`
- Modify: `tests/unit/test_jupyter_adapter.py`

- [ ] Build prepared status, stack, frame, and value snapshots. Write failing text/HTML rendering tests for hierarchical output, collapsed markers, redaction, pagination, method status, and pending guidance.
- [ ] Attach spies that fail if rendering contacts RuntimeSession, RDBG, source resolver, or parser. Redisplay the same page twice and require byte-identical output with zero calls.
- [ ] Run `uv run pytest tests/unit/test_jupyter_capture_display.py tests/unit/test_jupyter_adapter.py -q`; expect missing renderer failures.
- [ ] Implement plain-text and `_repr_html_` rendering from stored immutable data only. Register formatters/protocol methods without injecting a global `capture` alias.
- [ ] Run the focused tests; expect all to pass.
- [ ] Commit with `git add packages/jupyter/src/onec_runtime_jupyter/capture_display.py packages/jupyter/src/onec_runtime_jupyter/extension.py tests/unit/test_jupyter_capture_display.py tests/unit/test_jupyter_adapter.py && git commit -m "feat: render capture inspection pages"`.

### Task 13: Keep existing MCP capture tools internally coherent

**Files:**
- Modify: `packages/mcp/src/onec_runtime_mcp/agent/capture_contracts.py`
- Modify: `packages/mcp/src/onec_runtime_mcp/agent/capture_service.py`
- Modify: `packages/mcp/src/onec_runtime_mcp/agent/runtime_backend.py`
- Modify: `packages/mcp/src/onec_runtime_mcp/agent/facade.py`
- Modify: `packages/mcp/src/onec_runtime_mcp/server.py`
- Modify: `tests/unit/test_agent_capture_contracts.py`
- Modify: `tests/unit/test_agent_capture_inspect.py`
- Modify: `tests/unit/test_mcp_server.py`

- [ ] Write failing compatibility tests for the existing MCP `capture.stack` and `capture.frame` wire responses backed by the new typed core. Require the same bounded/privacy-safe public payloads and no new MCP tool.
- [ ] Rename the MCP-local `CaptureView` contract if needed to avoid shadowing core `CaptureView`; keep the wire schema stable.
- [ ] Run `uv run pytest tests/unit/test_agent_capture_contracts.py tests/unit/test_agent_capture_inspect.py tests/unit/test_mcp_server.py -q`; expect failures where backends still consume old dictionaries.
- [ ] Replace direct dictionary consumption with a private adapter over typed core pages. Keep pagination/validation at the MCP boundary and avoid exposing source pins, evaluation internals, or value handles.
- [ ] Run the focused tests; expect all to pass.
- [ ] Commit with `git add packages/mcp/src/onec_runtime_mcp/agent/capture_contracts.py packages/mcp/src/onec_runtime_mcp/agent/capture_service.py packages/mcp/src/onec_runtime_mcp/agent/runtime_backend.py packages/mcp/src/onec_runtime_mcp/agent/facade.py packages/mcp/src/onec_runtime_mcp/server.py tests/unit/test_agent_capture_contracts.py tests/unit/test_agent_capture_inspect.py tests/unit/test_mcp_server.py && git commit -m "refactor: adapt mcp capture inspection"`.

### Task 14: Verify the complete contract and report issue #5 coverage

**Files:**
- Create: `docs/issue-5-capture-evaluation-coverage.md`

- [ ] Run the lifecycle and guard suite first:

  ```powershell
  uv run pytest tests/unit/test_capture_evaluation_models.py tests/unit/test_capture_evaluation_coordinator.py tests/unit/test_capture_evaluation_lifecycle.py tests/unit/test_capture_control_plane.py tests/unit/test_capture_guard_lifecycle.py tests/unit/test_runtime_api.py tests/unit/test_prototype_runtime.py tests/unit/test_jupyter_value_proxy.py -q
  ```

  Expect all tests to pass.

- [ ] Run the inspection/source/privacy suite:

  ```powershell
  uv run pytest tests/unit/test_module_syntax_registry.py tests/unit/test_configuration_source_layout.py tests/unit/test_capture_source_resolver.py tests/unit/test_capture_stack_views.py tests/unit/test_capture_context_views.py tests/unit/test_capture_value_expansion.py tests/unit/test_worker_breakpoint_privacy.py -q
  ```

  Expect all tests to pass.

- [ ] Run frontend and integration-contract tests:

  ```powershell
  uv run pytest tests/unit/test_jupyter_adapter.py tests/unit/test_jupyter_capture_display.py tests/unit/test_jupyter_session_shutdown.py tests/unit/test_agent_capture_contracts.py tests/unit/test_agent_capture_inspect.py tests/unit/test_mcp_server.py -q
  ```

  Expect all tests to pass.

- [ ] Run `uv run pytest tests/unit -q`; expect the full non-live unit suite to pass.
- [ ] Confirm no acceptance or demo notebook changed. This feature is covered by focused Python/fake-transport contracts and does not require a generated notebook update.
- [ ] Write `docs/issue-5-capture-evaluation-coverage.md` with a table mapping these covered criteria to exact tests: coordinator ownership, safe status/wait, late results, absence of redispatch, and guard error classification.
- [ ] In the same report, state that issue #5 remains open for the 1C/RDBG stall root cause, repeated guard calls/wider `to_df()` pipeline, in-place uncertain-dispatch recovery, and opt-in live 1C qualification. State explicitly whether any live qualification was run.
- [ ] Review the final diff for raw local paths, credentials, RDBG values/handles, generated outputs, and accidental changes under `notebooks/demo`.
- [ ] Run `git status --short` and verify only intended source, tests, generated acceptance artifacts, and documentation are present.
- [ ] Commit with `git add docs/issue-5-capture-evaluation-coverage.md && git commit -m "docs: report issue 5 lifecycle coverage"`.

## Completion Gate

Before calling the work complete:

- [ ] Run `rg -n "TODO|TBD|FIXME|GitHub issue #5 closure|close(s|d)? issue #5" src packages tests docs/issue-5-capture-evaluation-coverage.md`; investigate every match and leave no placeholder or closure claim introduced by this work.
- [ ] Confirm every acknowledged CAPTURE `evalExpr` path enters `CaptureEvaluationCoordinator`; direct controller/session evaluation is allowed only outside CAPTURE or inside the coordinator driver.
- [ ] Confirm `CaptureEvaluationPendingError` and `CaptureInspectionTimeout` have distinct tests and call sites.
- [ ] Confirm `current_capture()`, `status()`, `wait()`, and close remain reachable during an indefinitely pending synthetic evaluation.
- [ ] Confirm fast stack tests report zero parser calls and enriched stack tests parse only missing pinned versions.
- [ ] Confirm the same common-module and document-object frames resolve from Designer, EDT project-root, and EDT `src` fixtures.
- [ ] Confirm no live 1C qualification is claimed from unit/fake-transport results and issue #5 is still open.
