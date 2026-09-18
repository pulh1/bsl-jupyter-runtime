# Remove Legacy MAIN/CAPTURE Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task by task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the old MAIN/CAPTURE execution path after the public cutover, without retaining compatibility dispatch.

**Architecture:** `RuntimeSession.start()` already constructs `PublicExecutionFacade`, `ExecutionController` and one `RdbgArbiter`. Move data contracts that the new path still imports out of `runtime_api.py` and `prototype_runtime.py`, remove the old execution classes and their fallback branches, then reduce the controller by extracting focused coordination without introducing another RDBG owner.

**Tech Stack:** Python 3.11+, pytest, 1C extension protocol 5, Jupyter, MCP.

**Spec:** [MAIN/CAPTURE execution design](../specs/2026-09-17-main-capture-execution-design.md).

## Global constraints

- Keep one RDBG reader/writer behind `RdbgArbiter` after bootstrap. Every extracted collaborator receives controller state and/or an admitted `SessionPort`; it never constructs another `RdbgSession` owner.
- Keep `MainOperation` alive across CAPTURE stops and retain pending capabilities through unknown outcomes. A local timeout or failed cell does not prove frame or target loss.
- No compatibility path for `PrototypeRuntimeController`, `PrototypeRuntimeApi` or `CaptureEvaluationCoordinator` is required. Preserve public data types and supported `RuntimeSession` behavior, not these classes.
- Check nested `AGENTS.md`, run focused tests before the offline suite, use disposable infobases for opt-in live checks, and keep evidence under ignored `artifacts/`.

---

### Task 1: Correct ownership findings before deleting code

**Files:** `src/onec_runtime/rdbg/session.py`, `src/onec_runtime/execution/{arbiter.py,local_wait.py,public_facade.py}`, affected direct-ticket callers, `src/onec_runtime/execution/main/{executor.py,operation.py}`; focused tests under `tests/unit`.

- [x] Add a regression with a foreign `StopEvent` before a matching `EvaluationResult`; verify it fails, then select the pending target/result without consuming the foreign stop.
- [x] Add deterministic interrupt tests at `SubmissionReceipt.adopt` and after submit/before dispatch; verify RED, then publish the receipt atomically with arbiter queue admission and retire pre-effect tickets on unwind.
- [x] Audit every direct-ticket `submit()`/`dispatch()` pair, including MAIN idle value transfer, Worker publication/breakpoints and controller inspection/resume. A `KeyboardInterrupt` after queue admission and before dispatch retires the unready ticket even when no cell `SubmissionReceipt` was supplied.
- [x] Add interrupt tests for direct-ticket waits; verify RED, then route `KeyboardInterrupt` to `controller.request_stop(ticket)` while a local wait timeout only detaches. Bound local mailbox wait slices without a BSL execution deadline so Windows ipykernel can deliver Ctrl+C.
- [x] Add ambiguous command-field write tests; verify RED, then keep `MainOperation.phase == UNKNOWN` until a confirmed write response or proven pre-transport refusal.
- [x] Run focused checks (including 109 controller/RDBG/interrupt tests) and `git diff --check`. Full offline suite waits for Task 4 stabilization.

### Task 2: Extract shared public contracts

**Files:** create `src/onec_runtime/runtime_models.py` and `src/onec_runtime/execution/continuation_models.py`; modify imports in `session.py`, `execution/`, Jupyter, MCP, integration and tests.

**Interfaces:** `runtime_models.py` provides `OperationState`, `RuntimeReplyKind`, `RuntimeReply`, `RuntimeStatus`, `RuntimeNamespaceSnapshot`, `CaptureCorrelationTicket` and `PartialWritebackError`. `continuation_models.py` provides immutable `ContinuationAttemptSpec` and `ContinuationAttemptEvidence` with the current validation rules. Callers import these symbols from their new owner; the old modules do not re-export them.

- [x] Add import-closure tests that import the public session/facade and DTOs without importing either old implementation module. Confirm RED against the current import graph.
- [x] Move the exact DTO fields, enum values and validation into the two modules. Use postponed annotations and `TYPE_CHECKING` where a capture type would create a cycle. Update production imports and public API docs.
- [x] Run import-closure, public facade, session, Jupyter and MCP focused tests. Confirm object types and wire values remain identical.

### Task 3: Remove old dispatch and shutdown branches

**Files:** `src/onec_runtime/session.py`, `packages/mcp/src/onec_runtime_mcp/agent/runtime_backend.py`, focused session/MCP tests.

- [x] Replace `PrototypeRuntimeApi | PublicExecutionFacade` with the new facade contract. In `prepare_main_for_capture`, activation, provenance, CAPTURE preparation/execution, capture-source configuration and shutdown, retain only the new route.
- [x] In MCP, accept `PreparedMainExecutionAttempt` from the public facade only. Remove checks for `_PreparedMainExecutionAttempt` and old backend comments.
- [x] Rewrite tests that supplied a legacy runtime double to use the public facade contract; delete only tests whose sole subject is removed compatibility behavior.
- [x] Run the focused session shutdown, handoff, MCP and Jupyter tests.

### Task 4: Remove old executors and coordinator

**Files:** `src/onec_runtime/{runtime_api.py,prototype_runtime.py,capture_evaluation.py}`, `integration/support/supervised_runtime_driver.py`, `integration/zup_worker_universe_acceptance.py`, `integration/zup_worker_universe_notebook.py`, affected benchmark tooling and legacy-only tests.

- [x] Move public CAPTURE snapshots, `AdmissionEnvelopeV1` and `CaptureTransferPlan` out of coordinator implementation without changing their validation or redaction. Keep their documented import path only if needed for a supported public API; remove the coordinator, its requests, thread/condition machinery and legacy tickets.
- [x] Remove `integration/support/supervised_runtime_driver.py` and its isolated legacy-only test: repository search shows no other caller. Migrate the ZUP Worker universe acceptance and its benchmark helpers away from private `_worker_universe*`, `_worker_module_artifacts` and `_controller.execute_system_capture` attributes of the old API. Where the scenario needs evidence, expose a narrow read-only snapshot from the new Worker lifecycle owner instead of rebuilding a second execution route.
- [x] Delete `PrototypeRuntimeController` and `PrototypeRuntimeApi` along with their private helpers. Remove legacy-only tests; preserve behavior tests by expressing them through the new public path where the behavior remains supported.
- [x] Verify `rg` finds no imports or constructors of the three removed classes in product or integration code. Run import-closure and focused public route tests before the full offline suite.

### Task 5: Reduce controller orchestration

**Files:** `src/onec_runtime/execution/controller/controller.py`, new focused modules under `execution/controller/`, route and ownership tests.

The remaining continuation and Worker extractions are structural follow-up after the live-qualified cutover. `ExecutionController` still owns admission and route state and remains large; do not treat these unchecked items as a new state-machine or RDBG-ownership requirement.

- [x] Extract Stop request/observation to `StopCoordinator`; the controller supplies exact operation/scope/target facts and applies confirmed target loss. The coordinator reads ticket mailboxes, not RDBG events.
- [x] Extract CAPTURE data-plane ticket setup first. Variable/page submitters live in `capture_data_plane.py`; descriptor registry, manager metadata, materialization and cleanup plans live in `capture_private_data_plane.py`. The controller still owns exact scope/route admission, ledger and direct ticket submission; the shared completion helper remains in the controller.
- [ ] Extract local continuation planning and per-root evidence. Keep remote breakpoint rollback and the ordered resume transaction in the controller; do not give a planner an RDBG reader or copied route state.
- [ ] Extract Worker activation and generation-lease accounting. Keep route admission, creation of `MainOperation`/`CaptureScope`, ticket submission and stop routing in the controller.
- [ ] For each extraction, first run an existing public behavior test, move the algorithm without changing its RDBG plan or ticket ownership, then rerun route, interrupt and single-owner tests. Do not add a second state enum or event reader.

### Task 6: Final qualification and documentation

**Files:** `docs/python-api.md`, all repository `AGENTS.md` that describe the execution path; ignored evidence under `artifacts/`.

- [x] Run `python -m pytest tests/unit -q` once after focused checks and run VS Code unit tests/compile if its files changed. Final offline suite: 4828 passed, 57 skipped; no VS Code files changed.
- [x] Run the complete `01-overview.ipynb` and CAPTURE error/repair/materialization scenario on a disposable 1C infobase. Run separate file-mode live Stop/replacement gates for long-running MAIN and CAPTURE, recording exact target-exit evidence. Server-mode live qualification remains unavailable without a server infobase.
- [x] Review all `AGENTS.md` for removed legacy references and current execution ownership, run `git diff --check`, verify generated notebooks have empty outputs/execution counts and inspect staged files for local evidence.
- [ ] Commit and push the verified branch. State explicitly any live gate that remains unverified.
