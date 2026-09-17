# RDBG operation ownership: implementation plan

> **For agentic workers:** This is the first independently testable implementation slice of the [MAIN/CAPTURE execution spec](../specs/2026-09-17-main-capture-execution-design.md). Execute its tasks in order with red-green-refactor and focused verification.

**Goal:** Keep a live RDBG target and the exact evaluation capability usable across interval timeouts and ambiguous eval transport failures.

**Architecture:** `RdbgSession` owns protocol IDs and pending capabilities; interval waits do not infer target loss. The caller receives a typed dispatch uncertainty carrying the same capability if transport entry occurred. The later arbiter can adopt it without resending `evalExpr`.

**Tech Stack:** Python 3.12, pytest, RDBG XML/HTTP client.

**Spec:** `docs/superpowers/specs/2026-09-17-main-capture-execution-design.md`, sections 6, 7 and 9.

## Global constraints

- Keep `RdbgSession` as the only owner of low-level `result_id` correlation.
- Never repeat an `evalExpr` after transport entry merely because its HTTP result is unknown.
- A wait interval is neither a BSL execution deadline nor proof of target loss.
- Keep unit tests independent of a live 1C installation.
- Preserve public session invalidation and explicit target teardown behavior.

---

### Task 1: Nonfatal stop wait intervals

**Files:**
- Modify: `src/onec_runtime/rdbg/session.py:492`
- Test: `tests/unit/rdbg/test_session.py`

**Interfaces:**
- `RdbgSession.wait_for_any_stop(timeout_s)` continues to raise `CommandTimeout` for an empty interval.
- Its `SessionState.EXECUTING` and target ownership survive that exception, so another wait can accept the eventual stop.

- [x] Add a unit test: create `ready_session(FakeTransport())`, set state `EXECUTING`, call `wait_for_any_stop(timeout_s=0.001)` and assert `CommandTimeout`, state `EXECUTING` and same target. Queue a matching `StopEvent` and assert a second call returns it and reaches `READY`.
- [x] Run the exact test and confirm it fails because the first wait changes state to `FAILED`.
- [x] Remove the state transition from the interval deadline in `wait_for_any_stop`; retain the exception and target identity.
- [x] Run the exact test and `tests/unit/rdbg/test_session.py`; check no unrelated expectation relies on timeout poisoning.
- [ ] Commit this slice.

### Task 2: Retain an ambiguous eval capability

**Files:**
- Modify: `src/onec_runtime/rdbg/session.py:126` and `:659`
- Modify: `src/onec_runtime/errors.py`
- Test: `tests/unit/rdbg/test_session.py`

**Interfaces:**
- `RdbgSession.start_evaluation()` and `start_collection_evaluation()` return the existing `PendingEvaluation` on confirmed dispatch.
- If transport has been entered and raises, a typed `EvaluationDispatchUnknown` carries that same `PendingEvaluation`. The session retains it for a later correlated `exprEvaluated`. Failure before transport entry releases it.

- [ ] Add a unit test whose `FakeTransport.request("evalExpr")` raises `CommandTimeout`. Assert a typed exception exposes one pending capability; a second `start_evaluation()` is rejected; inject one matching `EvaluationResult`; assert `wait_evaluation_event(pending)` returns it without a second `evalExpr`.
- [ ] Add a second test: invalidate the session before transport admission, then assert the pre-entry failure leaves no pending capability.
- [ ] Run both tests and confirm the first fails because `_start_evaluation_request()` drops the capability.
- [ ] Implement an admission/transport-entry callback boundary inside `_request()`; wrap only exceptions raised after that boundary in `EvaluationDispatchUnknown`, retaining the pending record. Keep ordinary pre-entry exceptions and their cleanup.
- [ ] Run both tests, `tests/unit/rdbg/test_session.py`, and `tests/unit/test_capture_evaluation_lifecycle.py`.
- [ ] Commit this slice.

### Task 3: Boundary and regression check

**Files:**
- Test: `tests/unit/test_prototype_runtime.py`
- Test: `tests/unit/test_capture_control_plane.py`

**Interfaces:** Existing controller/coordinator code must preserve its current public error contract while the low-level RDBG capability remains retained; follow-on arbiter integration owns recovery.

- [ ] Run the listed controller tests using this checkout's `src` on `sys.path`; identify any changed exception classification.
- [ ] Add a behavioral regression only if a controller contract changed: after ambiguous dispatch, no second `evalExpr` is sent to the same target; status remains unknown rather than failed BSL.
- [ ] Run the regression red, then apply the smallest compatibility change and run green.
- [ ] Run `git diff --check` and record the static test result. Live RDBG qualification remains a separate opt-in stage.
