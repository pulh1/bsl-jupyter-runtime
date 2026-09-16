# Final Capture Debug-Stream Ownership Implementation Plan

> **For agentic workers:** Execute inline in this isolated worktree. The parent task forbids subagents.

**Goal:** Prevent RuntimeSession heartbeats and obsolete completion helpers from bypassing the controller-owned RDBG evaluation stream.

**Architecture:** The coordinator exposes a condition-protected, read-only indication that it owns a live remote evaluation or resume. The controller combines its CAPTURE and ready-inspection owners, RuntimeApi exposes that signal to RuntimeSession, and the heartbeat tick defers all Debug UI traffic while it is true. The obsolete direct CAPTURE completion collection API is removed; RuntimeApi remains the only completion route and dispatches through its structured INSPECTION request.

**Tech Stack:** Python 3.12, pytest, RuntimeSession, PrototypeRuntimeApi, PrototypeRuntimeController, CaptureEvaluationCoordinator.

**Spec:** `C:/repo/bsl-jupyter-runtime/.worktrees/capture-inspection-design/docs/superpowers/plans/2026-09-15-capture-inspection-api.md` — Thread and Lock Ownership and Task 8.

## Global Constraints

- Submission adopts a coordinator ticket while RuntimeSession and RuntimeApi admission locks are held; only ticket waiting releases them.
- Coordinator workers are the only consumers of an acknowledged RDBG pending capability.
- Control-plane reads and close remain lock-independent and no heartbeat may contend for an owned Debug UI stream.
- Preserve protocol 2, artifact 0.1.3, and existing CFE; no BSL source changes are expected.
- No production compatibility adapters; test doubles use the explicit contract.

### Task 1: Add failing ownership regressions

**Files:**
- Modify: `tests/unit/test_completion_fields.py`
- Modify: `tests/unit/test_capture_evaluation_lifecycle.py`
- Modify: `tests/unit/test_prototype_runtime.py`

- [ ] Add a real RuntimeSession completion handoff test for both CAPTURE and ready inspection: after RDBG acknowledgement and outer-lock release, a heartbeat tick must not call the RDBG heartbeat while the coordinator worker polls.
- [ ] Add a lifecycle inventory regression requiring `PrototypeRuntimeController` to expose no direct `inspect_completion_fields` collection-evaluation entrypoint.
- [ ] Run the exact tests and confirm they fail because the heartbeat has no stream gate and the obsolete method remains.
- [ ] Commit only the failing tests and plan as `test: cover final capture stream ownership`.

### Task 2: Gate heartbeats and remove the bypass

**Files:**
- Modify: `src/onec_runtime/capture_evaluation.py`
- Modify: `src/onec_runtime/prototype_runtime.py`
- Modify: `src/onec_runtime/runtime_api.py`
- Modify: `src/onec_runtime/session.py`
- Modify: `tests/unit/test_capture_evaluation_lifecycle.py`
- Modify: `tests/unit/test_prototype_runtime.py`

- [ ] Add `CaptureEvaluationCoordinator.owns_debug_ui_stream()` under its condition. It returns true only while an active evaluation or resume record can perform RDBG work.
- [ ] Add controller and RuntimeApi read-only composition over CAPTURE and ready-inspection owners. The RuntimeSession heartbeat tick checks it after nonblocking operation-lock acquisition and returns before RDBG or process heartbeat work.
- [ ] Delete `PrototypeRuntimeController.inspect_completion_fields()` and update the direct-session inventory to list only permitted synchronous non-CAPTURE methods.
- [ ] Run the exact RED tests and the affected completion/coordinator/session tests until green.
- [ ] Commit production and completed regressions as `fix: serialize capture debug stream ownership`.

### Task 3: Verify and report

**Files:**
- Modify: `.superpowers/sdd/2026-09-15-capture-inspection-api/task-8-foundation-report.md`

- [ ] Run focused completion, heartbeat, controller inventory, coordinator, RuntimeSession, Jupyter/runtime, and MCP-minimum tests; then the relevant full unit suite.
- [ ] Run `uv run python -m compileall src packages`, inspect `git diff --check`, and confirm BSL/CFE inputs are unchanged.
- [ ] Record exact commands, results, no-CFE rationale, worktree and commit IDs in the Task 8 report for Sol xhigh rereview.
