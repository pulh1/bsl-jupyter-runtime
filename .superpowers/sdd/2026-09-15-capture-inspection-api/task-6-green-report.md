# Task 6 GREEN implementation report

Date: 2026-09-15

Branch: `feature/capture-shutdown`

Approved RED head: `7e16ee3` — `test: require joined writer shutdown`

GREEN commit: `2ac3aef` — `fix: bound capture coordinator shutdown`

## Task 5 integration

The final approved Task 5 chain was integrated before Task 6 production work.
The source commits and their cherry-picked branch commits are:

- `12f4c4f` -> `a49f3c3`
- `ff424dd` -> `bf20d16`
- `6138f49` -> `4ac4202`
- `d5d7c47` -> `960f735`
- `d490db3` -> `ebb954f`
- `9898f46` -> `b6692dc`
- `5f8ee7a` -> `0244884`
- `333003f` -> `46df325`
- `6017791` -> `a26eea6`
- `55b29a6` -> `aea6907`

Both import conflicts in `tests/unit/test_capture_control_plane.py` were
resolved by retaining the complete Task 5 inspection imports and the complete
Task 6 shutdown imports. Before production changes, the combined Task 5/Task 6
run produced exactly the six approved Task 6 failures and 361 existing passes.

## Implemented shutdown contract

`CaptureEvaluationCoordinator` now separates close admission, bounded worker
termination proof, and final lease disposition:

- `begin_close()` immediately rejects new submissions and wakes/detaches
  waiters without waiting for journal or data-plane locks.
- The controller invalidates the transport and performs a finite coordinator
  join bounded by the configured command timeout (and a one-second ceiling).
- `finish_close(True)` applies the already classified release/quarantine
  disposition only after the event-consumer thread has terminated.
- `finish_close(False)` records bounded abandoned evidence and leaves the real
  request, pin ownership, and cleanup leases held for supervised teardown.
- Repeated close is idempotent and does not duplicate pin or cleanup-lease
  disposition.
- Once shutdown starts, the coordinator does not dispatch remote cleanup
  instructions after transport invalidation.

The shutdown journal has a closed, value-free seven-field schema for both
proven and unproven termination. It contains the evaluation ID, enum kind,
termination proof, bounded elapsed time, pin/cleanup disposition enums, and
cleanup lease count. It contains no BSL source, value handle, target URL,
Worker identity, manifest, or exception text.

`PrototypeRuntimeApi.close()` now performs the CAPTURE control-plane shutdown
before entering its single-writer section. `RuntimeSession._close()` invokes
the same control plane before its operation lock, including the registered
Jupyter kernel-shutdown path. If termination cannot be proven, the API returns
within the deadline without releasing locally retained coordinator ownership;
session/process teardown remains the supervising owner.

Task 7 pending-resume/Continue shutdown is intentionally excluded.

## Verification

Approved Task 6 cases:

```text
8 passed, 137 deselected in 6.97s
```

Task 5 inventory plus core Task 6:

```text
7 passed, 115 deselected in 6.57s
```

Combined control-plane, RuntimeApi, and Jupyter shutdown:

```text
367 passed, 1 known Windows Proactor/pyzmq warning in 39.55s
```

Task 5 public/runtime group with warnings treated as errors:

```text
419 passed in 13.01s
```

Coordinator/lifecycle/prototype/session group with warnings treated as errors:

```text
351 passed, 1 skipped in 8.96s
```

RuntimeSession/RDBG/Jupyter shutdown group:

```text
89 passed, 1 known Windows Proactor/pyzmq warning in 29.21s
```

Full unit suite:

```text
4243 passed, 57 skipped, 1 known Windows Proactor/pyzmq warning in 247.81s
```

`python -m compileall -q src/onec_runtime` and `git diff --check` passed.

## Ordinary review remediation

The first ordinary GREEN review requested two P1 corrections. They were added
with separate RED and GREEN commits:

- `02b3d6d` — `test: close capture shutdown disposition races`
- `7fe4775` — `fix: retain capture shutdown dispositions atomically`

The deterministic race test lets an acknowledged result cross the initial
close check and blocks inside completion. An unproven close then claims the
same record and cleanup lease before the worker is released. After release,
the worker must exit without publishing an outcome or applying any pin or
cleanup disposition, while the single abandoned event and supervised
ownership remain unchanged.

The proven-shutdown disposition path now records completion separately for the
pin and every cleanup lease. It attempts every unfinished resource even when a
sibling callback raises, retains the record, and reports a bounded safe
`ProtocolError`. A repeated close retries only unfinished resources; completed
callbacks are never duplicated. Shutdown evidence and final release occur only
after every disposition succeeds. Evidence failures are also normalized and
remain retryable without repeating successful resource callbacks.

Review-fix verification:

```text
three exact review rows: 3 passed in 1.81s
completion-barrier race repeated: 10/10 passed
focused shutdown/lifecycle/runtime/Jupyter: 450 passed, 1 known warning
full unit suite: 4246 passed, 57 skipped, 1 known warning in 235.90s
```

`python -m compileall -q src/onec_runtime` and `git diff --check` passed again.

## Ordinary review r2 remediation

The second ordinary review required an ownership protocol shared with the real
RuntimeApi pin lease and a nonblocking publication reservation. The changes
are split into:

- `919cf59` — `test: require atomic capture lease outcomes`
- `2116ef8` — `fix: share capture pin disposition outcomes`

Every request now owns one internal idempotent pin lease with explicit states:
`unclaimed`, `in_flight`, `succeeded`, `failed_or_unknown`, and `retained`.
The worker's physical disposition and the shutdown supervisor's retention use
the same lease lock and state transition. If the worker claims first, an
unproven close retains the record but publishes no false `retained` evidence.
If the supervisor claims first, the worker cannot enter the physical callback.
A proven shutdown may dispose a previously retained lease after its worker has
joined.

`PrototypeRuntimeApi._detach_capture_evaluation_pin_locked()` now returns that
shared lease. Its physical callback no longer marks a private one-shot flag
before release. A callback exception permanently records
`failed_or_unknown`; repeated close reports the same safe failure without
re-dispatching the physical operation or publishing successful disposition
evidence.

Shutdown evidence publication is reserved under the coordinator condition
before journal I/O. A concurrent false/false or false/true finalizer returns
without waiting for the first journal write and cannot publish a second event.
Failed callback or journal attempts release the reservation for an explicit
later retry while retaining all ownership.

Review r2 verification:

```text
five exact review rows: 5 passed in 1.85s
shared lease race rows repeated: 10/10 passed
focused coordinator/control-plane/lifecycle/RuntimeApi: 431 passed (-W error)
focused including Jupyter shutdown: 454 passed, 1 known warning
full unit suite: 4250 passed, 57 skipped, 1 known warning in 251.95s
```

`python -m compileall -q src/onec_runtime` and `git diff --check` passed.

## Final Astra remediation

The final Astra review at `2116ef8` identified three shutdown admission gaps.
They were addressed with:

- `37cf02d` — `test: fence capture shutdown cleanup admission`
- `f7324af` — `test: model server cleanup in bounded session close`
- `785e116` — `fix: fence capture shutdown cleanup admission`

`RuntimeSession` now bounds acquisition of its data-plane operation lock by
the capture controller timeout. If that lock remains owned, the foreground
normal or kernel close returns after preserving capture ownership and starts a
single daemon supervisor that holds the Session strongly and completes local
process/session cleanup once the operation owner releases the lock.

`RdbgSession` now has one short request-admission gate shared with irreversible
invalidation. Request construction and capture dispatch markers remain outside
the gate; every ordinary transport request passes the final gate immediately
before transport admission. Invalidation never waits for network I/O. The
server-termination and detach requests are explicit close-owned teardown paths
that remain available after normal request admission is closed.

The coordinator rechecks closing after result evidence is durably flushed, so
a late result cannot start restoration after shutdown wins. It also preserves
the `quarantine` disposition when shutdown settles an acknowledged secondary
policy or continuation evaluation.

Final-review verification:

```text
seven exact Astra rows: 7 passed in 2.09s
RDBG session/server/detach: 61 passed in 0.68s (-W error)
coordinator/control-plane/lifecycle/RuntimeApi/Jupyter: 352 passed, 1 known warning in 39.56s
barrier races: 10/10 passed
full unit suite: 4257 passed, 57 skipped, 1 known warning in 268.35s
```

`python -m compileall -q src/onec_runtime packages/jupyter/src` and
`git diff 2116ef8..HEAD --check` passed.
`ruff` is not installed in the project environment, so no Ruff result is
claimed. No live 1C qualification was run; all evidence is unit/scripted
transport coverage.


## Ordinary review r4 remediation

The fourth ordinary GREEN review found that deferred Session cleanup could
make later close calls unbounded and that Jupyter discarded its external
shutdown owner before core cleanup had finished. The correction is split into:

- `8639c74` — `test: keep incomplete session shutdown retryable`
- `0c056be` — `fix: keep deferred session cleanup retryable`

The supervised closer now waits for the Session operation lock before entering
the close serialization lock. Foreground normal and kernel close calls use the
same configured bounded deadline for close-lock and operation-lock admission,
so a concurrent or repeated close does not wait behind the deferred owner.
After acquiring the operation lock, the supervisor performs the existing
serialized cleanup and releases both locks in ownership order.

`RuntimeSession.is_closed` exposes the terminal core outcome to the Jupyter
owner. A bounded deferred return leaves the wrapper open, keeps the server
guardian running, and preserves its shell and atexit retry hooks. Once the
supervisor completes core cleanup, a later normal or kernel close finalizes the
wrapper and stops the guardian exactly once.

Review r4 verification:

```text
four exact review rows: 4 passed in 2.29s
Jupyter shutdown file: 29 passed, 1 known Windows Proactor/pyzmq warning
control-plane/RDBG/RuntimeApi focused: 578 passed in 16.30s (-W error)
RuntimeSession/server/Jupyter nearby: 128 passed, 2 skipped in 3.96s (-W error)
full unit suite: 4261 passed, 57 skipped, 1 known warning in 240.71s
```

`python -m compileall -q src/onec_runtime packages/jupyter/src` and
`git diff --check` passed. `ruff` is not installed in the project environment,
so no Ruff result is claimed. No live 1C qualification was run.


## Final Sol r5 remediation

The final Sol xhigh r5 review found that a transient failure while recording
`capture_evaluation_shutdown_abandoned` left the retained shutdown record
without a retryable classification. If the coordinator worker exited before
the next close, that retry used the new termination proof and incorrectly
required a worker-provided release/quarantine disposition.

The correction is split into:

- `c489986` — `test: retry abandoned shutdown evidence`
- `e15dd88` — `fix: preserve abandoned shutdown classification`

The regression fails the first abandoned-evidence write after an unproven
close, releases the pending poll so the coordinator exits, and retries through
both the normal and kernel Interactive close entries. It requires one retained
abandoned classification, no pin or cleanup disposition, no remote cleanup,
bounded and private journal fields, terminal RuntimeSession and Interactive
owners, one guardian stop, removed shutdown hooks, and idempotent later closes.

A successfully retained unproven classification is now sticky across journal
write failure. Later close calls retry the same abandoned publication even if
their bounded join can now prove that the worker stopped. The abandoned flag is
set only after the shared pin lease accepts supervised retention, preserving
the existing path where an in-flight worker disposition must finish before a
later proven close can dispose the remaining resources.

Review-fix verification:

```text
RED exact regression: 2 failed at "CAPTURE shutdown record has no terminal disposition"
GREEN exact regression: 2 passed, 29 deselected in 2.13s (-W error)
GREEN exact regression repeated: 20/20 passed (-W error)
targeted shutdown publication/disposition races: 10 passed, 199 deselected in 2.71s (-W error)
coordinator/control-plane/lifecycle/RuntimeApi: 549 passed in 17.08s (-W error)
RuntimeSession/server/notebook/RDBG nearby: 236 passed, 1 skipped in 4.32s (-W error)
Jupyter shutdown rows excluding the five pyzmq rows: 26 passed, 5 deselected in 5.49s (-W error)
full Jupyter shutdown file: 31 passed, 1 known Windows Proactor/pyzmq warning in 28.54s
full unit suite: 4263 passed, 57 skipped, 1 known Windows Proactor/pyzmq warning in 237.75s
```

`python -m compileall -q src/onec_runtime packages/jupyter/src`,
`git diff --check 4e02fa3..HEAD`, and `git diff --check 0c056be..HEAD` passed.
No live 1C qualification was run; all evidence is unit/scripted transport
coverage.


## Bounded shutdown state-machine remediation

The Sol xhigh rereview found a second convergence failure: after a kernel-first
abandoned-evidence write failure, local server/process teardown set the Session
closed flags even though coordinator publication and RuntimeApi shutdown were
still incomplete. That made the only public retry owners unreachable and let
the Interactive wrapper stop its guardian and remove its hooks too early.

The correction is split into:

- `20230a6` — `test: require convergent capture shutdown state`
- `5d79c74` — `fix: converge capture shutdown owners`

`RuntimeSession` now derives terminal shutdown from one immutable state model
with three independent, monotonic axes: local resources, coordinator capture
publication, and RuntimeApi shutdown. The compatibility booleans no longer
decide terminal state independently. A retry skips completed axes and finishes
the remaining owner, so all normal-first/kernel-first and normal-retry/kernel-
retry combinations converge to the same result.

After target termination, `PrototypeRuntimeApi` has a local-only finalization
path. It requires completed coordinator publication, enters the existing
single-writer boundary while Session still owns its operation lock, abandons
the already-dead target without remote cleanup, clears API-owned state, and
becomes terminal. There is no admission gap between local target teardown and
API finalization. Interactive guardian and hook finalization continues only
after the composite core state is terminal.

The four-way regression uses a transient abandoned-journal failure and a late
worker exit. It requires two journal attempts, one retained abandoned event,
no redispatch, no pin/cleanup callback, no private failure text, one guardian
stop, hooks retained until convergence, and idempotent subsequent calls.

State-machine verification:

```text
RED four-way regression: 3 failed, 1 passed in 2.66s
GREEN four-way regression: 4 passed, 29 deselected in 2.49s (-W error)
GREEN four-way regression repeated: 20/20 passed (-W error)
bounded Session/guardian shutdown rows: 11 passed, 22 deselected in 2.99s (-W error)
capture coordinator file: 125 passed in 7.79s (-W error)
RuntimeApi file: 222 passed in 6.86s (-W error)
server-infobase file: 38 passed in 1.01s (-W error)
capture/lifecycle/prototype/RuntimeApi/Session nearby: 644 passed in 18.98s (-W error)
full Jupyter shutdown file: 33 passed, 1 known Windows Proactor/pyzmq warning in 29.09s
full unit suite: 4265 passed, 57 skipped, 1 known Windows Proactor/pyzmq warning in 243.50s
```

`python -m compileall -q src/onec_runtime packages/jupyter/src` and final
`git diff --check` passed. `ruff` is not installed in the project environment,
so no Ruff result is claimed. No live 1C qualification was run; all evidence
is unit/scripted transport coverage.

## RuntimeApi terminal-axis remediation

The final Sol xhigh review found that `PrototypeRuntimeApi._closed` represented
two incompatible facts. A normal close could set it after capture admission had
closed but before the unproven capture shutdown had been published or the real
Worker data plane had been finalized. `RuntimeSession` then inferred both
remaining shutdown axes from that one flag, reached a false terminal state
after local teardown, and made the post-target RuntimeApi finalizer unreachable.

The correction is split into:

- `e404a77` — `test: finalize unproven capture worker ownership`
- `2a74d51` — `fix: separate capture shutdown terminal axes`
- `2ddf449` — `test: use RuntimeApi admission closure state`

`PrototypeRuntimeApi` now keeps three independent monotonic facts:

- `_admission_closed` fences new public data-plane requests;
- `_capture_shutdown_finished` means capture publication is finalized; and
- `_data_plane_finalized` means API-owned data-plane resources have been
  finalized after target termination.

Admission closure never claims data-plane finalization. A normal unproven
close remains retryable through the existing post-target finalizer. That
finalizer can enter the single-writer boundary after admission has closed,
abandon the terminated Worker target without remote cleanup, release the
detached pin lease and module registrations, and clear API generation/cache
ownership exactly once.

`RuntimeSession` now uses the exact composite terminal predicate: local
resources terminal, capture publication finalized, and RuntimeApi data plane
finalized. It no longer derives either API completion axis from `_closed`.
Normal and kernel retry entries therefore finish any reachable axis after the
target has terminated; Interactive wrapper, guardian, and hook cleanup remain
pending until that composite state is terminal. The existing completion test
now explicitly models admission closure rather than mutating the obsolete
terminal sentinel.

The RED uses real `WorkerUniverseRegistry` and
`ServerWorkerUniverseRegistry` ownership with two Worker registrations and a
detached capture pin lease. Its four rows cover normal-first and kernel-first
shutdown, each followed by normal or kernel retry. Before the fix, both
normal-first rows left the Worker host `READY`. After finalization, every row
requires a closed host, no host leases, zero registration refcounts, an empty
broken server registry, cleared API generation/pin/module-cache ownership, no
target cleanup instruction or disconnect, one private bounded abandoned
journal event, one guardian stop, removed hooks only after convergence, and
idempotent later closes.

RuntimeApi terminal-axis verification:

```text
RED real-worker matrix: 2 failed (normal-first host remained READY), 2 passed
GREEN real-worker matrix: 4 passed in 2.29s
GREEN matrix plus late-worker retry: 8 passed in 2.79s
GREEN matrix repeated: 10/10 invocations passed (four rows each)
RuntimeApi recovery contract: 1 passed in 1.60s
completion/admission and real-worker rows: 16 passed in 2.28s
shutdown/control-plane/RuntimeApi focused: 384 passed, 1 known Windows Proactor/pyzmq warning in 40.25s
worker/server/prototype/guardian nearby: 376 passed in 8.98s
kernel process: 5 passed, 1 known Windows Proactor/pyzmq warning in 11.58s
full unit suite: 4269 passed, 57 skipped, 1 known Windows Proactor/pyzmq warning in 241.21s
```

The kernel and full-suite commands used `uv run python -m pytest` from the
worktree root so the checked-in root `integration` package is importable.
`python -m compileall -q src/onec_runtime` and `git diff --check` passed.
No live 1C qualification was run; all evidence is unit/scripted transport
coverage.
