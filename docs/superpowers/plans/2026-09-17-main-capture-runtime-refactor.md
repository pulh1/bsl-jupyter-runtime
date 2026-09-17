# MAIN/CAPTURE runtime refactor: implementation plan

**Spec:** [MAIN/CAPTURE execution design](../specs/2026-09-17-main-capture-execution-design.md).

**Goal:** Make MAIN and CAPTURE distinct execution mechanisms with explicit operation lifetimes, one RDBG owner, and one mode-agnostic notebook pipeline. A notebook Stop requests actual target termination and safe session replacement. Execution has no fixed duration limit.

**Constraints:** Preserve existing public APIs while migrating their internals. No live 1C claim from unit tests. Keep all new runtime behavior in `src/onec_runtime` and notebook lifecycle in `packages/jupyter`. Treat source text, frame values, IDs and transport payloads as private. Never convert an unverified target into a proven lost frame.

## 0. Stabilize protocol evidence and domain lifetimes

Files: `src/onec_runtime/rdbg/session.py`, `src/onec_runtime/errors.py`, `src/onec_runtime/capture_evaluation.py`, `src/onec_runtime/execution/main/operation.py`, `src/onec_runtime/prototype_runtime.py` and focused unit tests.

- [x] Make a stop wait interval preserve the RDBG target and EXECUTING state.
- [x] Keep the exact pending eval capability after an ambiguous transport/response outcome; reject another eval.
- [x] Let the CAPTURE owner poll the retained capability and expose outcome uncertainty until a correlated result arrives, including inline resume steps.
- [x] Track one MAIN command through CAPTURE, multiple stops, completion and uncertain Continue.
- [x] Distinguish a safe local pre-Continue failure from target loss, and record proven command completion before result decoding.
- [x] Verify RDBG, MAIN, CAPTURE and notebook replacement focused tests together. The initial combined run passed 385 tests; notebook replacement passed 60.

These are vertical compatibility changes. They do not complete the architecture below.

## 1. One RDBG arbiter and ticket

Files: new `src/onec_runtime/execution/arbiter.py`, `contracts.py` and `tests/unit/test_rdbg_arbiter.py`; later integrate through `session.py` and `prototype_runtime.py`.

1. Specify immutable `RouteToken` (runtime generation, route epoch, context revision and opaque owner), `RdbgOperation` (one accepted logical request) and `ExecutionTicket` (waiter and operation outcome). Ticket settlement is distinct from MAIN command termination.
2. Add an arbiter mailbox and a single worker owning all post-bootstrap RDBG I/O and event reads. The mailbox lock covers only queue/route/active record; no transport, Worker activation, callbacks or waits under it. No separately acquired OperationSlot/EventStreamLease.
3. Reject stale route tokens and duplicate preparation nonce before remote effects. Recheck guards when dequeuing. An accepted ticket exists before transport entry. A queued Stop cancels before effects; an active Stop initiates explicit termination and never reports success on a mere waiter detach.
4. Prepare MAIN, CAPTURE, inspection, materialization, heartbeat and cleanup plans against arbiter ports, then switch the runtime to those plans as one ownership cutover. The legacy CAPTURE coordinator has its own worker and cannot be called from an arbiter plan or given a worker-confined `SessionPort`: the former deadlocks and the latter creates a second event reader. Remove direct runtime calls into `RdbgSession` outside bootstrap/teardown. Verify exactly one reader and no overlapping dispatch with event-gated concurrency tests.
5. Ensure ambiguous eval/Continue/modifyValue retains its owning ticket and capability. Poll bounded request intervals indefinitely while target ownership remains valid; status can report uncertainty without a fixed BSL execution deadline.

Cutover inventory: MAIN command writes, `Continue`, stop polling and completion eval; CAPTURE locals, transfer, kernel frame, command ID, begin-context, user eval and helper eval; inspection, materialization and cleanup; breakpoint shield/restore, root writeback and resume; idle heartbeat, recovery and shutdown. Each multi-step CAPTURE request owns one ticket through its required cleanup. Route handoff occurs inside the arbiter worker after a recognized stop creates its `CaptureScope` and before the first setup request. On a confirmed CAPTURE `Continue`, handoff returns to the same `MainOperation`; an unknown `Continue` retains its owner and route fence. The public integration test exercises MAIN → CAPTURE cell → resume → MAIN completion and records the thread of every post-bootstrap RDBG call, including heartbeat.

Acceptance: a third operation submitted during a pending eval cannot send RDBG; detaching one waiter does not free its active operation; a late correlated event settles the original ticket exactly once.

## 2. Extract MAIN execution mechanism

Files: `src/onec_runtime/execution/main/executor.py` and `operation.py`, `src/onec_runtime/execution/controller/controller.py`, focused MAIN tests.

1. Move the ordered MAIN protocol (workspace, command/root writes, Continue, stop wait, completion read) from the prototype controller into `MainExecutor`. It receives an arbiter port and a `MainOperation`; it does not choose a CAPTURE route.
2. Controller creates `MainOperation` only at admission; executor changes its lifecycle from admitted through running/stopped/completed/unknown/lost. A CAPTURE stop suspends this same object; later stops reuse it. A local failure before Continue can terminate the rejected operation without claiming target loss.
3. Controller classifies returned stops and creates a `CaptureScope` only for a recognized capture point. Completion identity is recorded independently of presentation/decoding errors.
4. Move MAIN wait into the arbiter worker so `RuntimeSession` and `RuntimeApi` release caller writer locks before waiting. A finite poll/request deadline cannot make the MAIN command terminal. A canceled notebook waiter does not orphan the remote operation.

Acceptance: tests cover CAPTURE → resume → CAPTURE → completion with the same MAIN ID, a long series of empty poll intervals, and a notebook waiter interrupted while the owner later receives its stop.

## 3. Extract CAPTURE scope and execution mechanism

Files: `src/onec_runtime/execution/capture/{scope,executor,writeback,inspection,materialization}.py`, controller and focused CAPTURE tests.

1. Create one `CaptureScope` per confirmed stop with a remote identity fence plus a separate local stop sequence. Keep frame identity, staged setup, active ticket, dirty-root ledger and resource debts in the scope. An operation failure does not mark the frame lost.
2. Move CAPTURE setup, user eval, helper eval, variables, materialization and resume from prototype controller/coordinator into `CaptureExecutor`. It receives the arbiter port and scope, not the concrete controller. Retire the coordinator after its ownership cases move to arbiter tickets.
3. For setup, preserve evidence by stage. A failed locals/frame/MAIN-ID read does not by itself lose the stop. Repeat a setup step only if the same physical stop can be confirmed; current RDBG stop/stack observations have no remote stop nonce and cannot rule out an external Continue followed by another stop at the same location. Until stronger evidence exists, expose `setup_failed` and block dependent actions without promising an unsafe retry. For materialization, retain temporary-key cleanup debt and breakpoint workspace repair separately from frame identity. Worker pin debt is generation-owned.
4. Resume uses per-root writeback records: export, `modifyValue`, cleanup, Continue. Never repeat unknown writes or Continue. Before the first write, a confirmed export error leaves the frame paused. A late result can settle the original ticket.
5. Test value access, `to_df()`, BSL/Worker error, cleanup failure, unknown transport, partial writeback and second CAPTURE operation on the same scope. No global FAILED transition without target/frame loss evidence.

Acceptance: frame usability and last operation outcome are independently observable; a local CAPTURE cell error leaves a later valid cell runnable at the same stop.

## 4. Generic notebook pipeline and route policies

Files: `src/onec_runtime/execution/{pipeline,contracts}.py`, `execution/main/policy.py`, `execution/capture/policy.py`, `src/onec_runtime/bsl/semantic_lowering.py` and `runtime_api.py`.

1. Split mode-independent parse/split/source-map work into `CommonCellParser`. Controller returns a `PreparationContext` with an opaque route token, a fresh nonce and a selected `CellPolicy`.
2. Policies own namespace/Worker snapshot requirements, message collection, dirty roots, outcome settlement and immutable `LoweringProfile`. The shared AST traversal receives the profile and creates isolated working state per preparation. No shared mutable lowerer between concurrent preparations.
3. Add a versioned read-only preparation check and atomic `submit_cell` admission. A stale local preparation retries before target/Worker side effects; an accepted operation never silently retries an unknown effect.
4. Make `RuntimeApi.execute_bsl` delegate to `CellExecutionPipeline.execute`. It must not import `LoweringMode`/`OperationState` or branch on concrete MAIN/CAPTURE policy/executor. Migrate prepared ticket paths too.
5. Write a contract test injecting a third fake policy and executor through a route binding. It must pass parse → prepare → submit → settle with no change to generic pipeline/API.

Acceptance: static review finds no mode branch in the generic BSL path, and concurrency tests prove preparation holds no RDBG queue and stale snapshots dispatch nothing.

## 5. Stop, target termination and automatic replacement

Files: `src/onec_runtime/session.py`, arbiter/controller, `packages/jupyter/src/onec_runtime_jupyter/session.py` and focused session/Jupyter tests.

1. Map notebook Stop/KeyboardInterrupt to `request_stop(ticket)`. Before first effect cancel queued work; after dispatch request target termination. Distinguish `stop_requested`, `stop_unknown` and `target_terminated`. Never say code stopped before termination evidence.
2. If termination is confirmed, close the old runtime and start a new owned 1C session in the same Python kernel. Fence old route/ticket/value handles and proxy aliases with a globally advancing runtime generation. Replacement starts only after old shutdown is terminal.
3. If termination outcome is unknown, retain the old owner for retry/inspection and do not expose a new ready session. If user merely detaches an internal waiter, keep polling the original ticket.
4. Test concurrent old/new generation aliases and Python proxies, confirmed versus unknown termination, CAPTURE and MAIN Stop, and a long-running operation without an execution deadline. Live 1C verification is a separate opt-in gate.

The arbiter worker checks a Stop request after each bounded MAIN stop or CAPTURE eval poll interval and before the next remote side effect. If the remote result arrives first, the ordinary operation may settle; otherwise the worker keeps the original ticket and pending capability, then performs target teardown on that same worker. No second event reader or concurrent RDBG writer is part of the normal Stop path. The 30-second grace period limits confirmation waiting and produces `stop_unknown` when evidence is unavailable; it does not limit BSL execution. An HTTP request still needs a genuine finite total deadline so a Stop checkpoint can eventually run.

Server `terminateDbgTarget` acknowledgement alone is not target-loss evidence. Confirm absence of the bound client, expected target and related session targets in a fresh debugger registry (or exact RAC session absence). The existing Debug UI disappearance fallback in `RuntimeSession.close()` is insufficient for Stop success. In file mode, confirm exit of the owned debuggee process. Revoke old remote proxies at the Stop fence, keep the old owner through `stop_unknown`, and publish a new runtime only after target absence, old arbiter shutdown and cleanup are complete.

## 6. Finish migration

- [ ] Remove `PrototypeRuntimeController`/`PrototypeRuntimeApi` names after callers use the new interfaces; retain compatibility aliases only where tested public imports need them.
- [ ] Put concrete architecture guidance in `execution/AGENTS.md` and route subdirectories.
- [ ] Run focused tests first, then the unit and integration contract suites from this checkout's source paths. Record any baseline race separately. Run live tests only with opt-in temporary infobase.
- [ ] Request whole-branch architecture/code review against the spec; fix blocking findings, then report the actual static and live qualification separately.
