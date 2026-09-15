# Capture Inspection API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only, debugger-like Python CAPTURE API and make acknowledged CAPTURE evaluations survive caller timeout or interruption without poisoning the runtime or losing their late result.

**Architecture:** `src/onec_runtime` owns typed capture views, evaluation lifecycle, source resolution, syntax indexing, value policy, and bounded RDBG plans. A controller-owned `CaptureEvaluationCoordinator` worker is the only owner of CAPTURE RDBG evaluations and resume remote steps; it keeps its own short-lived control lock while callers wait without data-plane or Worker lifecycle locks. Jupyter only renders snapshots and adapts pending outcomes, while MCP keeps its existing public tools through a private adapter. The work lands in lifecycle-first order so issue #5 behavior is independently testable before stack and value inspection are added.

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
| Worker-universe or transfer caller | Under its component lock, reserve a local ledger transition and construct a structured remote-step/cleanup plan; after releasing that lock, submit the plan and wait | Hold `ServerWorkerUniverseRegistry._lock`, a target RLock, or a transfer-backend lock across coordinator submission, ticket wait, RDBG I/O, or a completion callback |
| `CaptureEvaluationCoordinator` worker thread | Dispatch every CAPTURE `evalExpr`, own the returned `PendingEvaluation`, poll the RDBG event stream, retain generation-pin and pre-registered cleanup leases, run private inline cleanup/result steps, own resume through the next event, and publish outcomes | Acquire `RuntimeSession._operation_lock` or `RuntimeApi._single_writer`; recursively call public coordinator submission; execute concurrent CAPTURE target operations; expose private request data through status or logs |
| Control-plane callers | Read `current_capture()`, `status()` and `wait()` from the coordinator snapshot/condition | Call `_require_available()`, run a Worker privacy guard, acquire a data-plane lock, or poll RDBG |
| Shutdown caller | Mark coordinator closing, invalidate the target/transport to stop polling, join with a finite deadline, then release quarantined resources | Wait indefinitely for the user-operation lock or release a pin while the coordinator may still consume a late event |

Locking rules:

1. `RuntimeSession._operation_lock` and `RuntimeApi._single_writer` remain admission locks for data-plane operations. Submission must return a ticket before either lock is released; the caller then waits outside both locks.
2. Add a dedicated `CaptureEvaluationCoordinator._condition`. It protects records, phase, bounded timing, waiter state, and shutdown flags. `Condition.wait()` is the only permitted wait while using it and releases the mutex; no RDBG, file, parser, cleanup, or callback work runs while the mutex is held.
3. Protect RuntimeApi generation-pin slots with a narrow `_generation_lock`. Pin acquisition calls `RuntimeApi._worker_universe.pin_active()` before attaching the returned lease under `_generation_lock`. Release first detaches the slot under `_generation_lock`, then calls `release_pin()` or `retain_outcome_unknown()` after releasing it. No generation lock is held across a Worker target lock or RDBG work.
4. Refactor `ServerWorkerUniverseRegistry`/target operations to split `reserve`, remote execution, and `commit`/`abort`. Their RLocks protect only local ledger transitions. They are never held while `_instruction_executor` submits or waits, and coordinator completion callbacks acquire them only after every coordinator/generation lock has been released.
5. The only permitted nested outer-lock order is `RuntimeSession._operation_lock` → `RuntimeApi._single_writer`. A narrow generation lock, Worker/transfer lock, and coordinator `_condition` must not be held simultaneously with one another. Admission may briefly acquire `_condition` while holding the two outer locks because the coordinator worker never acquires either outer lock; it must release all outer locks before waiting.
6. A controller phase/data-plane reservation, not a mutex held by the initiating thread, prevents overlapping operations while phase is `evaluating` or `resuming`. Rejected calls receive the typed lifecycle error before any privacy guard or dispatch.
7. The coordinator is the sole consumer of a pending CAPTURE capability. Observer `capture.wait()` calls wait on `_condition`; they never call `RdbgSession.wait_evaluation_event()`.
8. Result policy, mandatory cleanup, and resume use a private coordinator-thread step executor against the active record/request. They never call public `submit()` and never create a second active record.

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
    def submit_evaluation(self, request: CaptureEvaluationRequest) -> CaptureEvaluationTicket: ...
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

### Task 3: Make Worker lifecycle and transfer plans safe for cross-thread completion

**Files:**
- Modify: `src/onec_runtime/capture_evaluation.py`
- Modify: `src/onec_runtime/worker_universe.py`
- Modify: `src/onec_runtime/runtime_api.py`
- Modify: `src/onec_runtime/compact_table_backend.py`
- Modify: `src/onec_runtime/value_transfer_backend.py`
- Create: `tests/unit/test_capture_remote_step_ownership.py`
- Modify: `tests/unit/test_worker_universe.py`
- Modify: `tests/unit/test_compact_table_backend.py`
- Modify: `tests/unit/test_value_transfer_backend.py`
- Modify: `tests/unit/test_notebook_method_runtime.py`

Replace executor-under-lock callbacks with structured plans:

```python
@dataclass(frozen=True)
class PreparedWorkerMutation:
    instruction: str = field(repr=False)
    reservation: WorkerMutationReservation = field(repr=False)

@dataclass(frozen=True)
class CaptureCleanupLease:
    private_key: str = field(repr=False)
    cleanup_step: CaptureRemoteStep = field(repr=False)

class CaptureStepContext:
    def execute_inline(self, step: CaptureRemoteStep) -> EvaluationResult: ...
```

The coordinator constructs `CaptureStepContext`; it is callable only on its worker thread for the active record. Worker `reserve_*()` returns a `PreparedWorkerMutation` after releasing the target lock; `commit_*()`/`abort_*()` reacquire it only for local ledger state. Transfer `prepare_*()` creates every temporary key and `CaptureCleanupLease` before its first remote step.

- [ ] Write a threaded deadlock regression: a CAPTURE hot-reload caller reserves Worker mutation and waits on a coordinator ticket; late completion commits it and releases its generation pin without either thread holding `ServerWorkerUniverseRegistry._lock` across the wait.
- [ ] Add the same regression for `execute_prepared_capture_hypothesis()`. After acknowledged dispatch, caller timeout/interruption must detach and leave final classification/pin cleanup to the coordinator rather than its current caller-owned `finally`.
- [ ] Instrument Worker target locks and fail the test if `_instruction_executor`, coordinator `submit()`, ticket wait, `release_pin()`, or `retain_outcome_unknown()` runs while the target RLock is held.
- [ ] Add transfer-backend tests requiring temporary keys/cleanup leases to exist in the request record before first dispatch. Use a barrier so the late result arrives before caller `finally`; require mandatory cleanup to finish before `paused` is published.
- [ ] Add failure cases for result received followed by cleanup dispatch uncertainty and cleanup failure. Require `recovery_required`, explicit `cleanup_status`, quarantined lease, and no recursive coordinator submission.
- [ ] Run `uv run pytest tests/unit/test_capture_remote_step_ownership.py tests/unit/test_worker_universe.py tests/unit/test_compact_table_backend.py tests/unit/test_value_transfer_backend.py tests/unit/test_notebook_method_runtime.py -q`; expect executor-under-lock and caller-owned cleanup assertions to fail.
- [ ] Refactor Worker lifecycle into local `reserve`/`commit`/`abort` transitions with remote `PreparedWorkerMutation` execution outside the target lock. Move acknowledged CAPTURE hypothesis completion and generation-pin disposition into the coordinator record.
- [ ] Refactor compact-table and value-transfer backends to prepare structured remote steps and cleanup leases before dispatch. Remove caller-side cleanup dispatch; its `finally` only detaches/abandons the optional continuation.
- [ ] Implement coordinator-private `CaptureStepContext.execute_inline()` with a worker-thread/active-record assertion. Route result-policy and mandatory cleanup through it and reject recursive public submission.
- [ ] Run the focused tests; expect all to pass without a live thread or retained temporary lease in normal completion.
- [ ] Commit with `git add src/onec_runtime/capture_evaluation.py src/onec_runtime/worker_universe.py src/onec_runtime/runtime_api.py src/onec_runtime/compact_table_backend.py src/onec_runtime/value_transfer_backend.py tests/unit/test_capture_remote_step_ownership.py tests/unit/test_worker_universe.py tests/unit/test_compact_table_backend.py tests/unit/test_value_transfer_backend.py tests/unit/test_notebook_method_runtime.py && git commit -m "refactor: separate capture remote steps from ledgers"`.

### Task 4: Move controller CAPTURE evaluation and restoration under the coordinator

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

### Task 5: Split data-plane admission from waiting and expose the control plane

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

- [ ] Write a threaded regression proving the initiating caller can be blocked or interrupted while another thread calls `runtime.status()`, `runtime.current_capture()`, `capture.status()`, and `capture.wait(timeout_s=0)` without acquiring `_operation_lock`, `_single_writer`, `_require_available()`, or a Worker guard.
- [ ] Add tests that acknowledged pending work keeps the generation pin and leaves `_poisoned_error` unset; pre-acceptance ambiguity still calls `retain_outcome_unknown()` and poisons/quarantines according to the existing promotion contract.
- [ ] Add capture-fence tests across `operation_id`, capture generation, and `stop_sequence`, plus absent/stale/paused/evaluating/resuming/recovery/unknown states.
- [ ] Run `uv run pytest tests/unit/test_capture_control_plane.py tests/unit/test_runtime_api.py -q`; expect lock-independent access and pin ownership assertions to fail.
- [ ] Split `RuntimeSession.execute_bsl()` and RuntimeApi CAPTURE execution into short admission/submission and waiter phases. Release `_operation_lock` and `_single_writer` before `wait_initiator()`; rely on the controller phase as the data-plane reservation.
- [ ] Add `_generation_lock` and transfer the pin lease to the coordinator record before transport dispatch. Remove the acknowledged-pending path from `_finish_capture_evaluation_pin_locked()`/`retain_outcome_unknown()`; preserve it only for uncertain acceptance.
- [ ] Make `RuntimeSession.status()` and RuntimeApi status use the controller's immutable control-plane snapshot. Implement `RuntimeSession.current_capture()` and capture status/wait as direct coordinator control-plane calls with fence checks. Export only typed snapshots/views from the session, not raw controller records.
- [ ] Run the focused tests; expect all to pass.
- [ ] Commit with `git add src/onec_runtime/runtime_api.py src/onec_runtime/session.py src/onec_runtime/capture_inspection.py tests/unit/test_capture_control_plane.py tests/unit/test_runtime_api.py && git commit -m "feat: expose lock-independent capture status"`.

### Task 6: Make shutdown finite while an evaluation is pending

**Files:**
- Modify: `src/onec_runtime/runtime_api.py`
- Modify: `src/onec_runtime/session.py`
- Modify: `src/onec_runtime/prototype_runtime.py`
- Modify: `tests/unit/test_capture_control_plane.py`
- Modify: `tests/unit/test_jupyter_session_shutdown.py`

- [ ] Write a failing test with an evaluation poll that never returns. Call normal close and kernel-shutdown close from another thread and assert both finish within the configured deadline.
- [ ] Assert shutdown marks the coordinator closing, rejects new submissions, invalidates/stops the target transport, joins the event consumer, and only then releases or quarantines pin/temporary leases.
- [ ] Run `uv run pytest tests/unit/test_capture_control_plane.py tests/unit/test_jupyter_session_shutdown.py -q`; expect the existing single-writer shutdown path to block.
- [ ] Add `begin_close()` before ordinary data-plane lock acquisition, transport invalidation to wake polling, bounded `join()`, and safe abandoned-operation evidence when termination cannot be proven.
- [ ] Run the focused tests; expect all to pass with no leaked thread.
- [ ] Commit with `git add src/onec_runtime/runtime_api.py src/onec_runtime/session.py src/onec_runtime/prototype_runtime.py tests/unit/test_capture_control_plane.py tests/unit/test_jupyter_session_shutdown.py && git commit -m "fix: bound capture coordinator shutdown"`.

### Task 7: Give the controller full ownership of resume

**Files:**
- Modify: `src/onec_runtime/capture_evaluation.py`
- Modify: `src/onec_runtime/prototype_runtime.py`
- Modify: `src/onec_runtime/runtime_api.py`
- Modify: `src/onec_runtime/session.py`
- Modify: `packages/jupyter/src/onec_runtime_jupyter/extension.py`
- Create: `tests/unit/test_capture_resume_lifecycle.py`
- Modify: `tests/unit/test_prototype_runtime.py`
- Modify: `tests/unit/test_runtime_api.py`
- Modify: `tests/unit/test_jupyter_adapter.py`

Use the coordinator's single worker but a distinct request/ticket:

```python
class CaptureEvaluationCoordinator:
    def submit_resume(self, request: CaptureResumeRequest) -> CaptureResumeTicket: ...

class CaptureResumeTicket:
    def wait_initiator(self, timeout_s: float | None) -> RuntimeReply: ...
```

`CaptureResumeRequest` is not an evaluation record and has no `evaluation_kind`. It privately owns dirty-root writeback state, required CAPTURE cleanup, Continue dispatch, next-event routing, the MAIN generation pin, and session capture-ticket finalization.

- [ ] Write a failing test proving admission atomically changes `paused` to `resuming` before the first root-export step and immediately rejects inspection, another resume, and new MAIN without dispatch.
- [ ] Add barriers at root export, frame `modify`, required CAPTURE cleanup, Continue dispatch, and `wait_for_any_stop()`. At every barrier interrupt the initiating waiter and prove the coordinator worker keeps the same resume request and caller-side `_operation_lock`/`_single_writer` are free.
- [ ] While a detached post-Continue `wait_for_any_stop()` remains pending, call normal close and kernel-shutdown close from another thread. Require both to stop transport polling and finish within the configured shutdown deadline.
- [ ] Assert an old `CaptureView` becomes stale exactly after Continue acknowledgement. Before that it reports `resuming` but cannot inspect; after that `runtime.status()` remains lock-independent and reports the active prior MAIN rather than MAIN-ready state.
- [ ] Deliver each possible next event: terminal MAIN completion admits a new MAIN; a new CAPTURE stop creates a different fence/view; a user breakpoint remains stopped and rejects new MAIN until separately resumed.
- [ ] Prove resume does not restart the Jupyter kernel, RuntimeSession, owned 1C session, `runtime_generation`, or `context_generation`. After successful terminal completion, a new MAIN sees persistent `Контекст` names/values and published notebook methods, receives a new operation ID, and pins the then-active Worker generation.
- [ ] Prove dirty `КонтекстОтладки` roots affect only the resumed suspended frame: they participate in the old MAIN result but are not promoted to persistent notebook names. Pending notebook names publish only on successful terminal completion; capture descriptors remain stale and persistent-context proxies keep their existing generation fence.
- [ ] Add failures before any mutation, after the first acknowledged root mutation, during mandatory cleanup, and at uncertain Continue. Permit return to `paused` only when no mutation is proven; otherwise require `recovery_required` and preserve/quarantine the exact leases.
- [ ] Assert `RuntimeSession._active_capture_ticket` and listeners are finalized by the controller completion callback even when the initiating Session call was interrupted.
- [ ] Run `uv run pytest tests/unit/test_capture_resume_lifecycle.py tests/unit/test_prototype_runtime.py tests/unit/test_runtime_api.py tests/unit/test_jupyter_adapter.py -q`; expect caller-owned resume and lock assertions to fail.
- [ ] Split `RuntimeSession.resume_capture()` and `RuntimeApi.resume_capture()` into short validation/admission/submission and an outer-lock-free ticket wait. Move the current caller `try`/`finally`, dirty-root loop, cleanup, Continue and next-stop routing into `CaptureResumeRequest` execution on the coordinator worker.
- [ ] Use `CaptureStepContext.execute_inline()` for root-export and CAPTURE-cleanup evaluations; direct non-evaluation RDBG steps (`modify`, `continue_`, `wait_for_any_stop`) remain worker-owned parts of the same resume request.
- [ ] Make ordinary `RuntimeSession.status()` use the lock-independent controller snapshot so later notebook cells can tell `resuming`/`main_pending` from `completed` after the old capture becomes stale.
- [ ] Run the focused tests; expect all to pass.
- [ ] Commit with `git add src/onec_runtime/capture_evaluation.py src/onec_runtime/prototype_runtime.py src/onec_runtime/runtime_api.py src/onec_runtime/session.py packages/jupyter/src/onec_runtime_jupyter/extension.py tests/unit/test_capture_resume_lifecycle.py tests/unit/test_prototype_runtime.py tests/unit/test_runtime_api.py tests/unit/test_jupyter_adapter.py && git commit -m "refactor: make capture resume controller-owned"`.

### Task 8: Correct public-value guard lifecycle and the `to_df()` regression

**Files:**
- Modify: `src/onec_runtime/runtime_api.py`
- Modify: `src/onec_runtime/session.py`
- Modify: `packages/jupyter/src/onec_runtime_jupyter/extension.py`
- Modify: `tests/unit/test_runtime_api.py`
- Modify: `tests/unit/test_jupyter_value_proxy.py`
- Create: `tests/unit/test_capture_guard_lifecycle.py`

- [ ] Write the exact issue #5 regression: `OnecValueProxy.to_df()` starts a `public_value_guard`; fake RDBG acknowledges but withholds its result; the initiating deadline raises `CaptureEvaluationPendingError` or `KeyboardInterrupt`; schema and transfer counters remain zero; one record/capability/pin remains; the next cell observes it; a second `to_df()` raises `CaptureBusyError` without dispatch; late `False` restores `paused` without starting the abandoned transfer; MAIN resume is admitted.
- [ ] Add sibling cases for guard `True`, `False`, confirmed BSL failure, invalid result, transport timeout before acknowledgement, acknowledged pending timeout, uncertain dispatch, cleanup dispatch uncertainty, restoration failure, and interrupted initiator. Assert exact errors: access denied, value check, pending, outcome unknown, or recovery required.
- [ ] Assert a broad `BaseException` handler can never translate pending, interruption, busy, ambiguous dispatch, or restoration failure to `"Worker generation objects are not public values"`.
- [ ] Run `uv run pytest tests/unit/test_capture_guard_lifecycle.py tests/unit/test_runtime_api.py tests/unit/test_jupyter_value_proxy.py -q`; expect the current guard fallback and redispatch behavior to fail.
- [ ] Change `_execute_worker_instruction()` to require a structured request with an explicit `evaluation_kind`; it must not choose a kind itself. Only `_require_public_value_handles_locked()` supplies `PUBLIC_VALUE_GUARD`; generic transfer/publication plumbing supplies `MATERIALIZATION_HELPER`, and bounded projections supply `INSPECTION`.
- [ ] Interpret only a confirmed guard Boolean. Ensure every temporary-handle cleanup lease was registered on the record before dispatch; initiator detachment only abandons the optional table continuation.
- [ ] Add a kind-routing test for guard, generic helper, and inspection plus a test that guard result-policy/cleanup uses the coordinator-private inline executor and never recursively submits a new record.
- [ ] Check capture lifecycle before `_require_available()` and privacy probing in proxy/materialization entry points. Raise `CaptureEvaluationPendingError` only for the initiating internal call whose acknowledged evaluation exceeded its deadline; every later data-plane call raises `CaptureBusyError` with the same safe ID/kind.
- [ ] Ensure late `False` runs mandatory workspace/handle cleanup and publishes a safe internal completion, but never invokes the abandoned table continuation.
- [ ] Run the focused tests; expect all to pass.
- [ ] Commit with `git add src/onec_runtime/runtime_api.py src/onec_runtime/session.py packages/jupyter/src/onec_runtime_jupyter/extension.py tests/unit/test_runtime_api.py tests/unit/test_jupyter_value_proxy.py tests/unit/test_capture_guard_lifecycle.py && git commit -m "fix: retain pending capture guard evaluations"`.

### Task 9: Adapt `%%bsl` pending outcomes without changing completed output

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

### Task 10: Extract a shared compact syntax index for hot reload and capture

**Files:**
- Create: `src/onec_runtime/bsl/module_syntax.py`
- Modify: `src/onec_runtime/bsl/full_ast_worker_projection.py`
- Modify: `src/onec_runtime/bsl/worker_projection_model.py`
- Modify: `src/onec_runtime/bsl/__init__.py`
- Modify: `src/onec_runtime/runtime_api.py`
- Create: `tests/unit/test_module_syntax_registry.py`
- Modify: `tests/unit/test_bsl_full_ast_worker_projection.py`
- Modify: `tests/unit/test_bsl_worker_projection_model.py`
- Modify: `tests/unit/test_runtime_api.py`

Define compact immutable source facts:

```python
@dataclass(frozen=True)
class ModuleSyntaxIndex:
    source_sha256: str
    parser_identity: tuple[str, str]
    methods: tuple[MethodSyntaxInfo, ...]

class ModuleSyntaxRegistry:
    def get(self, module: ModuleIdentity, source_sha256: str, parser_identity: tuple[str, str]) -> ModuleSyntaxIndex | None: ...
    def publish(self, module: ModuleIdentity, index: ModuleSyntaxIndex) -> None: ...
```

- [ ] Write failing tests for ordered method spans/parameters, method lookup by line, registry version keys, and immutable publication.
- [ ] Count parser calls: hot reload parses once and publishes the compact index; capture lookup of the same exact hash/parser identity parses zero additional times.
- [ ] Add RuntimeApi generation tests at the actual parse/promotion hooks: construction may stage G2 syntax as a candidate, successful publication activates its source generation, a MAIN stop pinned to G1 stays on G1, and failed/unknown promotion never activates G2.
- [ ] Run `uv run pytest tests/unit/test_module_syntax_registry.py tests/unit/test_bsl_full_ast_worker_projection.py tests/unit/test_bsl_worker_projection_model.py tests/unit/test_runtime_api.py -q`; expect missing-index/publication-hook failures.
- [ ] Generalize the existing full-AST projection to emit both the current Worker model and reusable `ModuleSyntaxIndex` from the same parse. Keep full AST/tokens temporary.
- [ ] Preserve the existing `full_ast_parser_identity()` pair instead of collapsing it to a string. Wire RuntimeApi's real parse/cache and successful-promotion paths to stage candidate syntax separately from activating a source generation; retain versioned older entries.
- [ ] Run the focused tests; expect all to pass.
- [ ] Commit with `git add src/onec_runtime/bsl/module_syntax.py src/onec_runtime/bsl/full_ast_worker_projection.py src/onec_runtime/bsl/worker_projection_model.py src/onec_runtime/bsl/__init__.py src/onec_runtime/runtime_api.py tests/unit/test_module_syntax_registry.py tests/unit/test_bsl_full_ast_worker_projection.py tests/unit/test_bsl_worker_projection_model.py tests/unit/test_runtime_api.py && git commit -m "feat: share module syntax indexes"`.

### Task 11: Resolve source modules lazily and pin source versions

**Files:**
- Create: `src/onec_runtime/configuration_source.py`
- Modify: `src/onec_runtime/bsl/module_catalog.py`
- Modify: `src/onec_runtime/capture_source.py`
- Modify: `src/onec_runtime/session.py`
- Create: `tests/unit/test_configuration_source_layout.py`
- Create: `tests/unit/test_capture_source_resolver.py`
- Modify: `tests/unit/test_bsl_module_catalog.py`
- Modify: `tests/unit/test_capture_source_configuration.py`

Normalize configuration input to this internal identity before any scan:

```python
@dataclass(frozen=True)
class SourceRootBinding:
    project: str
    configured_root: Path
    normalized_root: Path
    layout: SourceTreeLayout          # DESIGNER or EDT
    layer: SourceLayer                # BASE or EXTENSION after binding
    extension_name: str | None        # required only for EXTENSION
```

Configuration input has `layer=AUTO|BASE|EXTENSION`. `AUTO` discovers once from format-native metadata; `EXTENSION` requires an explicit name. Explicit layer/name must match metadata exactly. A mismatch is an error and never falls back to base or another extension.

- [ ] Build paired synthetic source trees with the same UUIDs and BSL: Designer metadata/module paths and EDT `.mdo`/direct-module paths. Cover both a common module and `Documents/ПриемНаРаботу` object module, plus base/extension identity.
- [ ] Write failing root-layout tests for a Designer root, an EDT project root containing `src`, the EDT `src` root itself, unsafe links/junctions, and an ambiguous root containing both direct and nested metadata trees.
- [ ] Write failing tests for equivalent `(object_id, property_id, extension)` resolution, canonical module name/role, line mapping, batched lookup, early exit, and no full index at startup in both formats. Use the same object UUID in base and extension fixtures and require the explicit layer/name discriminator to choose the correct source.
- [ ] Test `AUTO`, explicit `BASE`, explicit named `EXTENSION`, metadata mismatch, and absence of a configured matching extension. Mismatch or absence returns a typed unavailable/configuration error and never searches another layer.
- [ ] Add counters proving one directory pass resolves a batch of unresolved stack modules, positive/negative cache keys include source-root identity plus catalog generation, and explicit `refresh_capture_sources()` invalidates negative results.
- [ ] Write source-version tests for Worker artifact/generation pins and trusted-export `(path, size, mtime)` signatures. Change a file before and during read and require `source_changed`.
- [ ] Run `uv run pytest tests/unit/test_configuration_source_layout.py tests/unit/test_capture_source_resolver.py tests/unit/test_bsl_module_catalog.py tests/unit/test_capture_source_configuration.py -q`; expect the common-module-only resolver to fail broader identities and paired-layout tests.
- [ ] Extract shared `ConfigurationSourceLayout` root normalization and Designer/EDT path rules into `configuration_source.py`; make `SessionCommonModuleCatalog` consume it so capture and hot reload cannot disagree about the configured root.
- [ ] Extend `capture_source.py` with `CaptureModuleResolver`, `CaptureSourceCatalog`, and immutable `SourceVersionRef`. Map property IDs to module roles through explicit metadata-kind tables, scan only relevant roots/kinds for the requested batch, and cache encountered descriptions.
- [ ] Add `RuntimeSession.refresh_capture_sources()` and successful-hot-reload invalidation/publication hooks. Do not add a background watcher.
- [ ] Run the focused tests; expect all to pass.
- [ ] Commit with `git add src/onec_runtime/configuration_source.py src/onec_runtime/bsl/module_catalog.py src/onec_runtime/capture_source.py src/onec_runtime/session.py tests/unit/test_configuration_source_layout.py tests/unit/test_capture_source_resolver.py tests/unit/test_bsl_module_catalog.py tests/unit/test_capture_source_configuration.py && git commit -m "feat: resolve capture sources on demand"`.

### Task 12: Add fast and method-enriched stack pages

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
stack_page.with_methods(work_budget_s=None)
frame.with_method(work_budget_s=None)
frame.variables[:20]
frame.parameters[:20]
frame.locals[:20]
```

- [ ] Write failing tests for fresh native inventories on repeated slices, visible-to-native level mapping, collapsed runtime markers, unknown module fallback, and exact fence validation.
- [ ] Add a parser spy proving basic stack construction and rendering perform zero AST calls and native mode performs no directory or AST work.
- [ ] Add enrichment tests proving saved frames are not reread from RDBG, unique missing versions parse once, registry entries parse zero times, source changes return per-frame `source_changed`, and a soft work budget produces partial `method_status="timeout"` without changing CAPTURE phase. Use a fake monotonic clock that advances across one synchronous parser return; do not claim that an already-running parser call is preempted.
- [ ] Add a maximum source-file-size test that rejects an oversized missing syntax version before parsing and preserves module-and-line output with a bounded status.
- [ ] Run `uv run pytest tests/unit/test_capture_lazy_frames.py tests/unit/test_capture_stack_views.py -q`; expect typed view/import failures.
- [ ] Implement immutable `StackPage`, `DebugFrame`, `RuntimeFrameMarker`, visible/native stack descriptors, safe physical identity, and source-version pins. Keep UUID/private identity out of ordinary display.
- [ ] Implement local-only `with_methods()`/`with_method()` using the saved page plus `ModuleSyntaxRegistry`. Cap the soft work budget by runtime command timeout, check it before and after every read/parse, and document that version one does not preempt one synchronous parser invocation already in progress.
- [ ] Run the focused tests; expect all to pass.
- [ ] Commit with `git add src/onec_runtime/capture_inspection.py src/onec_runtime/runtime_api.py src/onec_runtime/session.py tests/unit/test_capture_lazy_frames.py tests/unit/test_capture_stack_views.py && git commit -m "feat: add capture stack views"`.

### Task 13: Add context views, safe paths, and bounded value expansion

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

### Task 14: Add side-effect-free Jupyter rendering

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

### Task 15: Keep existing MCP capture tools internally coherent

**Files:**
- Modify: `packages/mcp/src/onec_runtime_mcp/agent/capture_contracts.py`
- Modify: `packages/mcp/src/onec_runtime_mcp/agent/capture_service.py`
- Modify: `packages/mcp/src/onec_runtime_mcp/agent/runtime_backend.py`
- Modify: `packages/mcp/src/onec_runtime_mcp/agent/facade.py`
- Modify: `packages/mcp/src/onec_runtime_mcp/server.py`
- Modify: `tests/unit/test_agent_capture_contracts.py`
- Modify: `tests/unit/test_agent_capture_inspect.py`
- Modify: `tests/unit/test_agent_capture_inspect_round4.py`
- Modify: `tests/unit/test_agent_capture_inspect_round5.py`
- Modify: `tests/unit/test_agent_temporary_table_managers.py`
- Modify: `tests/unit/test_mcp_server.py`

- [ ] Write failing compatibility tests for the existing MCP `capture.stack`, `capture.frame`, frame-variable, manager-origin, and temporary-table inspection responses backed by the new typed core/private adapter. Require the same bounded/privacy-safe public payloads and no new MCP tool.
- [ ] Rename the MCP-local `CaptureView` contract if needed to avoid shadowing core `CaptureView`; keep the wire schema stable.
- [ ] Run `uv run pytest tests/unit/test_agent_capture_contracts.py tests/unit/test_agent_capture_inspect.py tests/unit/test_agent_capture_inspect_round4.py tests/unit/test_agent_capture_inspect_round5.py tests/unit/test_agent_temporary_table_managers.py tests/unit/test_mcp_server.py -q`; expect failures where backends still consume old dictionaries.
- [ ] Replace direct dictionary consumption with a named private adapter over typed core pages. Where an existing MCP contract uses manager-origin or temporary-table shapes outside the new public Python value matrix, keep that bounded behavior inside the private adapter instead of advertising it through public `ValueNode`.
- [ ] Run the focused tests; expect all to pass.
- [ ] Commit with `git add packages/mcp/src/onec_runtime_mcp/agent/capture_contracts.py packages/mcp/src/onec_runtime_mcp/agent/capture_service.py packages/mcp/src/onec_runtime_mcp/agent/runtime_backend.py packages/mcp/src/onec_runtime_mcp/agent/facade.py packages/mcp/src/onec_runtime_mcp/server.py tests/unit/test_agent_capture_contracts.py tests/unit/test_agent_capture_inspect.py tests/unit/test_agent_capture_inspect_round4.py tests/unit/test_agent_capture_inspect_round5.py tests/unit/test_agent_temporary_table_managers.py tests/unit/test_mcp_server.py && git commit -m "refactor: adapt mcp capture inspection"`.

### Task 16: Verify the complete contract and report issue #5 coverage

**Files:**
- Create: `docs/issue-5-capture-evaluation-coverage.md`

- [ ] Run the lifecycle and guard suite first:

  ```powershell
  uv run pytest tests/unit/test_capture_evaluation_models.py tests/unit/test_capture_evaluation_coordinator.py tests/unit/test_capture_remote_step_ownership.py tests/unit/test_capture_evaluation_lifecycle.py tests/unit/test_capture_control_plane.py tests/unit/test_capture_resume_lifecycle.py tests/unit/test_capture_guard_lifecycle.py tests/unit/test_runtime_api.py tests/unit/test_prototype_runtime.py tests/unit/test_jupyter_value_proxy.py -q
  ```

  Expect all tests to pass.

- [ ] Run the inspection/source/privacy suite:

  ```powershell
  uv run pytest tests/unit/test_module_syntax_registry.py tests/unit/test_configuration_source_layout.py tests/unit/test_capture_source_resolver.py tests/unit/test_capture_stack_views.py tests/unit/test_capture_context_views.py tests/unit/test_capture_value_expansion.py tests/unit/test_worker_breakpoint_privacy.py -q
  ```

  Expect all tests to pass.

- [ ] Run frontend and integration-contract tests:

  ```powershell
  uv run pytest tests/unit/test_jupyter_adapter.py tests/unit/test_jupyter_capture_display.py tests/unit/test_jupyter_session_shutdown.py tests/unit/test_agent_capture_contracts.py tests/unit/test_agent_capture_inspect.py tests/unit/test_agent_capture_inspect_round4.py tests/unit/test_agent_capture_inspect_round5.py tests/unit/test_agent_temporary_table_managers.py tests/unit/test_mcp_server.py -q
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
- [ ] Confirm no Worker-universe/target or transfer-backend lock is held across coordinator submission, ticket wait, RDBG work, pin release, or a completion callback.
- [ ] Confirm every temporary cleanup lease is registered before its first possible creation dispatch and `paused` is not published while mandatory cleanup is outstanding.
- [ ] Confirm detached resume keeps new MAIN admission closed until terminal completion and `runtime.status()` remains lock-independent throughout.
- [ ] Confirm `CaptureEvaluationPendingError` and `CaptureInspectionTimeout` have distinct tests and call sites.
- [ ] Confirm `current_capture()`, `status()`, `wait()`, and close remain reachable during an indefinitely pending synthetic evaluation.
- [ ] Confirm fast stack tests report zero parser calls and enriched stack tests parse only missing pinned versions.
- [ ] Confirm the same common-module and document-object frames resolve from Designer, EDT project-root, and EDT `src` fixtures.
- [ ] Confirm no live 1C qualification is claimed from unit/fake-transport results and issue #5 is still open.
