# Task 7 GREEN implementation report

Date: 2026-09-16

Branch: `feature/capture-resume`

Base: `f6df29d` — approved Task 6 terminal-axis integration

## Review remediation — 2026-09-16

The independent Sol xhigh review at
`task-7-sol-xhigh-review.md` found an interruption window between coordinator
resume adoption and delivery of the ordinary `submit_resume()` return, plus
missing resume-specific shutdown, Worker-ownership, and failure-classification
coverage.

### Additional RED commits

- `440afed` — `test: cover interrupted resume ticket adoption`
- `9fa760d` — `test: extend capture resume lifecycle coverage`

### GREEN commit

- `d37ecfc` — `fix: make capture resume handoff interruption-safe`

`_CaptureResumeSubmission` is now a dynamic handoff receipt, matching the
existing evaluation-submission pattern. The coordinator publishes its ticket
under its condition with record adoption; the RuntimeApi detaches through that
receipt for every `BaseException`, including a failure before Python assigns
the normal local ticket. Detached completion therefore retires the Session
ticket and listener exactly once.

The controller now distinguishes a confirmed root-export rejection before any
frame mutation from a writeback/cleanup/Continue failure after mutation. The
former restores the paused CAPTURE fence; the latter remains recovery-required.

The new matrix covers KeyboardInterrupt and timeout in the exact
adoption-before-return window, all resume boundaries without test-side manual
detachment, terminal MAIN/successor CAPTURE/user breakpoint routing, real
Worker registry pin/lease/context/namespace continuity, normal and kernel
shutdown with attached and detached post-Continue waiters, and a late worker
exit after target-death finalization. The shutdown cases assert terminal API
axes, empty Worker leases/registrations, generation-slot release, one Continue,
and exact Session listener retirement.

The selected regressions were run against the test-only state before the GREEN
source was restored: two adoption rows and the pre-mutation fence row failed as
expected. They then passed after the implementation.

## RED commits

- `8e5511d` — `test: require controller-owned capture resume`
- `3f7ce2d` — `test: require caller-side resume completion delivery`
- `ec2833c` — `test: retain unproven resume shutdown ownership`
- `12c23ef` — `test: classify failed controller-owned resume`
- `7c1be04` — `test: detach interrupted resume handoff`

## Implemented resume ownership

`CaptureEvaluationCoordinator` now owns a distinct `CaptureResumeRequest` and
`CaptureResumeTicket`, separate from evaluation records and evaluation kinds.
Submission atomically reserves the paused fence as `resuming` before the
worker executes a staged-root transfer. The same coordinator worker executes
root export, CAPTURE cleanup, `Continue`, and the next-stop wait. Its private
inline executor keeps CAPTURE `evalExpr` ownership on that single stream.

The initiating API and Session calls submit under their ordinary admission
locks, then hand off both the RuntimeApi writer and Session operation lock
while waiting only on the ticket condition. A timeout or `KeyboardInterrupt`
detaches the initiating waiter; it cannot cancel, redispatch, or duplicate the
accepted resume. A new regression also covers interruption after acceptance
but before the ticket wait begins.

The old frame-backed `CaptureView` remains status-readable during `resuming`
and becomes stale exactly after `Continue` acknowledgement. Inspections and a
second resume receive typed `CaptureBusyError` during that interval. Terminal
MAIN, successor CAPTURE, and user-breakpoint next stops retain their existing
routing semantics. A detached Session ticket/listener is delivered by a short
Session-owned notifier after coordinator ticket publication, avoiding a
coordinator-to-MCP admission-lock cycle.

Resume completion classifies the old fence explicitly. A confirmed root-export
rejection before any frame mutation keeps the paused capture and its Session
fence usable. Post-mutation writeback, required cleanup, and uncertain Continue
still expose `recovery_required` through the control-plane snapshot and typed
`CaptureRecoveryRequiredError`. A narrow RuntimeApi generation lock detaches
the resume-owned pin slot before its Worker disposition or CAPTURE helper work
runs.

For shutdown, an unproven close retains a live resume record until the worker
has actually exited. It does not publish a false finalized state while a
controller-owned continuation can still settle.

## Debugging note

The earlier apparent prototype-suite hang was isolated with `-vv` and
faulthandler. The known cycle was an attached coordinator completion invoking
a Session/MCP listener while the initiating MCP caller held the CaptureService
admission lock and waited for the ticket. Attached delivery now runs on the
initiating thread after its lock handoff; detached delivery is queued after
ticket publication. `test_prototype_runtime.py` subsequently passed 94/94
under that diagnostic invocation.

## Verification

```text
resume lifecycle exact: 15 passed
resume lifecycle repeated: 10/10 invocations passed (15 rows each)
core coordinator/control-plane/RuntimeApi/prototype group: 542 passed in 15.81s
agent capture inspection nearby group: 32 passed in 1.25s
Jupyter adapter/session-shutdown/kernel group: 139 passed, 1 known Windows Proactor/pyzmq warning in 44.06s
prototype isolation with -vv/faulthandler: 94 passed in 3.99s
Windows event-handle regression repeated: 20/20 passed
full unit retry: 4499 passed, 58 skipped, 1 known Windows Proactor/pyzmq warning in 250.42s
```

The first full unit attempt had one unrelated Windows handle-counter
fluctuation: the unchanged `test_windows_owned_handles.py[event]` saw its
process-wide sample fall from 975 to 974 while creating an event. The exact
event row passed 20/20 immediately afterwards; the complete retry passed.

`python -m compileall -q src/onec_runtime packages/jupyter/src` and
`git diff --check` passed. No live 1C qualification was run; all evidence is
unit/scripted-transport coverage.

## Remediation verification

```text
adoption receipt exact: 2 passed, repeated 10/10
selected test-only RED baseline: 3 expected failures
resume lifecycle: 22 passed
resume shutdown regressions: 6 passed, repeated 5/5
full Jupyter session shutdown: 59 passed
controller/API focused group: 583 passed
MCP capture-inspection/continue group: 67 passed
Jupyter adapter: 81 passed
Jupyter kernel process: 5 passed, 1 known Windows Proactor/pyzmq warning
full unit: 4512 passed, 58 skipped, 1 known Windows Proactor/pyzmq warning
python -m compileall -q src/onec_runtime packages/jupyter/src packages/mcp/src: exit 0
git diff --check f6df29d..HEAD: exit 0
```

## Admission-transaction remediation — 2026-09-16

The updated independent Sol xhigh review found a second interruption window:
`CaptureEvaluationCoordinator.submit_resume()` called the controller `admit`
closure, which changed `CAPTURED` to `RESUMING`, before it published the active
resume record or handoff receipt.  A `KeyboardInterrupt` or timeout at that
return boundary could therefore leave the controller resuming while the
coordinator still reported paused, with no Session/MCP owner able to observe or
finish the operation.

### RED and GREEN commits

- `8a0d981` — `test: cover interrupted capture resume admission`
- `f66eab5` — `test: cover transactional capture resume admission`
- `12d9cbb` — `fix: make capture resume admission transactional`

The deterministic regression uses `sys.settrace` at the real controller
`admit` closure return, immediately after its state assignment.  It covers both
RuntimeApi and Session entry points and both `KeyboardInterrupt` and
`TimeoutError`.  Each accepted interruption leaves one detached coordinator
owner, preserves the Session/MCP fence, rejects a duplicate resume while work
is active, sends exactly one `Continue`, and retires the Session ticket and
listener exactly once at terminal delivery.

Admission is now a two-stage transaction.  A side-effect-free controller
preflight occurs before coordinator ownership.  The coordinator then publishes
the active record, ticket/receipt, and `RESUMING` phase under its condition
before committing the controller's local `RESUMING` state.  The defined order
is RuntimeApi single-writer admission, coordinator condition, then controller
local state commit.  If that final commit is interrupted, the already-published
owner is detached and completes the accepted operation; no user hook or remote
dispatch falls inside this state/receipt window.  Preflight failure leaves the
controller captured, the coordinator paused, and no active record, Session
ticket, root export, or `Continue`; a subsequent retry succeeds.

### Admission-remediation verification

```text
selected test-only RED baseline: 6 expected failures
admission exact (including earlier receipt rows): 8 passed
admission exact repeated: 10/10 invocations passed (6 rows each)
resume lifecycle: 28 passed
full Jupyter session shutdown: 59 passed, 1 known Windows Proactor/pyzmq warning
MCP capture group: 87 passed
prototype/RuntimeApi/Jupyter-adapter/control-plane focused group: 529 passed
full unit: 4518 passed, 58 skipped, 1 known Windows Proactor/pyzmq warning
python -m compileall -q src/onec_runtime packages/jupyter/src packages/mcp/src: exit 0
git diff --check f0a328b..HEAD: exit 0
git diff --check f6df29d..HEAD: exit 0
```

No live 1C qualification was run; this evidence uses unit and scripted
transport coverage.
