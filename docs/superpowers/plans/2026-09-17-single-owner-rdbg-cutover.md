# Single-owner RDBG cutover: MAIN → CAPTURE

Status: implementation gate. The MAIN → CAPTURE setup path cannot be enabled in
`PrototypeRuntimeController` alone while preserving one owner of `RdbgSession`.

## Why a partial switch is unsafe

- The controller constructs `MainExecutor`, `CaptureExecutor`, and
  `BreakpointWorkspaceController` with the raw session. `_execute_main()` invokes
  MAIN dispatch on its caller thread and `_route_stop()` enters `_begin_capture()`.
- `_begin_capture()` opens the scope, then starts `CaptureEvaluationCoordinator`.
  That coordinator has its own worker; `_capture_remote_step()` sends and polls
  eval directly through `self.session`. User cells, helpers, inspection,
  materialization, cleanup, and resume use this route. A settled arbiter ticket
  cannot lend its expired worker port to that coordinator.
- Keeping the MAIN arbiter ticket active instead would block every subsequent
  CAPTURE ticket. Closing the arbiter after setup would hand the session back to
  a second owner. Either choice defeats the agreed lifecycle.
- `_complete_main()` still uses synchronous `session.evaluate()`, and
  `RuntimeSession._heartbeat_tick()` calls `rdbg.heartbeat()` outside the
  arbiter. Breakpoint installs have an injectable port but default to the raw
  session. These paths also need ownership before the route is enabled.

## Cutover sequence and owners

1. **RuntimeSession:** finish bootstrap using its existing bootstrap owner,
   then construct one `RdbgArbiter` for the attached `RdbgSession` and pass it
   to the execution controller. Route heartbeat, shutdown, target termination,
   and recovery through arbiter plans or an explicit confirmed teardown state.
   No background reader may call the raw session after handoff.
2. **Controller and MAIN executor:** create the MAIN operation and publish an
   arbiter ticket before dispatch. Run `MainExecutor.dispatch(..., port=port)`
   and breakpoint installation on the arbiter worker; pass `port` to
   `BreakpointWorkspaceController.install`. Do not hold API/session writer locks
   while waiting for the ticket. Completion reads (`ЗавершеннаяКоманда`,
   `Результат`, `Ошибка`) must use owned pending eval on the same worker.
3. **Controller and CAPTURE executor:** classify the confirmed stop, create the
   `CaptureScope` before any setup read, and call `port.handoff_route(capture)`
   in the still-active MAIN plan. Then run
   `CaptureExecutor.open_scope(scope, port=CaptureSetupAdapter(port))`.
   Confirmed setup failure retains the CAPTURE route and stage evidence;
   uncertain eval keeps the ticket and pending capability owned. Publish READY
   only after setup and an arbiter-backed CAPTURE operation owner exist.
4. **CAPTURE operation owner:** replace the coordinator's worker, ticket, and
   event polling with arbiter plans. Use `CaptureCellEvaluator` for the primary
   user expression. In that same logical operation, run breakpoint shield and
   restore, message collection, Worker pin disposition, inline helper eval,
   inspection/materialization cleanup, and outcome settlement. `CaptureScope`
   stores frame, writeback, and debt evidence; the arbiter alone retains the
   protocol pending capability. Result policy receives raw `EvaluationResult`.
5. **Resume:** execute dirty-root transfer/writeback and context cleanup in
   one arbiter-owned resume plan. On confirmed `Continue`, hand off to the MAIN
   route in that plan and wait for the next stop with the same `MainOperation`.
   Partial/unknown writeback and unknown Continue retain ownership for exact
   reconciliation; they never replay the whole resume.
6. **Remove raw runtime access:** once every runtime operation is routed,
   remove the controller/coordinator raw-session fallback and reject any
   post-bootstrap direct RDBG call outside the arbiter worker. Keep low-level
   protocol correlation in `RdbgSession`.

## Port and test gates

- The worker port already exposes breakpoint replacement, local variables,
  modify, Continue, typed eval start/wait, collection start, and route handoff.
  Complete evidence-bearing stop continuation, completion/helper reads,
  teardown, and any inspection commands still missing from that port. A port
  is valid only in its active worker plan; never pass it to notebook callers or
  the old coordinator.
- First write a public RED integration test from `RuntimeApi.execute_bsl()`:
  MAIN dispatch → recognized CAPTURE stop → setup → CAPTURE cell → resume →
  MAIN completion, recording the thread of every RDBG call. All calls must
  have one worker owner, and no coordinator worker may start. Include both
  breakpoint installation and heartbeat contention.
- Add route/fence tests for queued old tokens, confirmed setup rejection,
  ambiguous setup eval with retained capability, CAPTURE BSL failure followed
  by another cell in the same scope, helper/materialization cleanup, partial
  writeback, unknown Continue, waiter detach, and Stop/termination evidence.
  No BSL execution timeout may be inferred from a bounded request interval.
- Run focused static tests before the full suite. Only then run the opt-in
  1C notebook acceptance on a temporary infobase; static tests are not live
  qualification.

**Enablement rule:** no product opt-in flag for the arbiter route until all
post-bootstrap runtime RDBG paths above are owned by it. A protocol-only test
harness may use the worker port earlier, but must not publish a usable CAPTURE
session with the old coordinator attached.
