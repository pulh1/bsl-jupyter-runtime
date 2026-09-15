# Task 7 GREEN implementation report

Date: 2026-09-16

Branch: `feature/capture-resume`

Base: `f6df29d` — approved Task 6 terminal-axis integration

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

Resume completion classifies the old fence explicitly. A confirmed failed
writeback keeps the controller's existing `partial_writeback_failure` state
for agent compatibility while exposing `recovery_required` through the
control-plane snapshot and typed `CaptureRecoveryRequiredError`. A narrow
RuntimeApi generation lock detaches the resume-owned pin slot before its
Worker disposition or CAPTURE helper work runs.

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
