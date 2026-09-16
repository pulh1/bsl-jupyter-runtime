# Task 8 foundation report

## Delivered foundation

- `AdmissionEnvelopeV1` is a closed protocol-2 result parser shared by the
  compact-table and recursive-value transports. It accepts only ready,
  denied, and confirmed-admission-error envelopes before a context payload is
  read.
- The extension is protocol `2`, artifact `0.1.3`. Its fingerprint covers the
  managed and kernel handshakes and both transfer serializer sources.
- Both BSL serializers classify the root before traversal and classify each
  descendant before encoding. Worker-generation values produce the sealed
  denied envelope and no context payload publication.
- Materialization builds generic server-side table serialization. Python no
  longer performs a schema read before target admission, and the old
  precomputed-schema instruction path has been removed.
- The obsolete remote public-value guard and its evaluation kind have been
  removed. Core, Jupyter, MCP, and the ZUP adapter call target-I/O-free local
  `validate_value_reference()` before publication; dynamic policy stays in the
  consuming materialization request.

## Protocol and bundle evidence

`tools/build_runtime_extension_bundle.py` ran with the installed 1C Designer
`8.3.27.2170`, artifact `0.1.3`, and protocol `2`. It produced
`OnecInteractiveRuntime.cfe` (21,798 bytes), whose SHA-256 is
`f52fa7d8d036035ad98bd6f857eb05ea18330e19b66c043c14ff129c616ebd7f`.
This is a Designer compilation and bundle build, not live-infobase
qualification.

The lifecycle suite includes a MANUAL-mode predecessor-protocol regression:
protocol `1` fails during handshake before any target materialization or
inspection request.

## Deferred by plan dependency

Coordinator pending/late-result lifecycle integration remains for the work
that follows Task 7. This foundation does not claim that integration.

## Validation

The focused remediation suite completed with `310 passed` in 6.12 seconds. It
includes both BSL serializers, their Python instruction builders, generic
table transport, Runtime API projection/materialization routes, completion,
and capture-value redaction models.

The nearby suite completed with `707 passed, 1 skipped` in 11.88 seconds. It
includes capture-evaluation models, both transports, extension
bundle/build/lifecycle/session, Runtime API, completion, Jupyter value proxy,
and the materialization bridge. Its lifecycle coverage retains the MANUAL
protocol-`1` rejection before any target materialization or inspection call.

`python -m compileall -q src/onec_runtime packages/jupyter packages/mcp` and
`git diff --check` pass with the final source.

## Review remediation

- Both successful BSL serializers now return `Доступ = Истина`. A behavioral
  unit test faithfully executes the emitted BSL `Структура.Вставить` success
  branches against each generated protocol-2 instruction and reaches `R`, so
  a missing dynamic property cannot be hidden by Designer syntax compilation.
- `materialization_kind`, completion, and projection paths generate inline
  root admission before target type/schema/descendant reads. Projection builds
  and serializes its bounded result in that same instruction; only admitted
  Base64 is written to the temporary context key.
- Value-inspection backends now return admission decisions as part of each
  projection. There is no callable `CaptureValuePolicy.private_guard`; a
  non-exact denial is an immutable exact wire object containing only `name`,
  `access: "denied"`, and `expandable: false`.
- Generic compact-table classification uses declared types before observations,
  preserving enumerations and all-null reference columns for per-column
  reference modes. The obsolete `columns` and `schema_reader` compatibility
  parameters are absent from the generic Python transport.

## Final Sol P1 remediation

- Jupyter now formats the exact closed `DeniedValueNode` contract. It never
  reads a removed `ValueNode.private` property or any path, type, preview, or
  handle from a denied node.
- Completion has one consumer-owned target operation:
  `ПолучитьДопущенныеИменаСвойствДляПодсказки` constructs Worker types, admits
  the root, and reads the bounded collection schema only after admission. Its
  first row is an `R` marker; `D|worker_generation_value` and
  `E|value_admission_failed` return without a second schema request.
- The compact BSL classifier receives `МаксимумСтрок` and checks the bounded
  row counter before each cell access. It retains declared types for enum and
  all-null reference columns, then verifies later serialized cells against
  that bounded classification. The regression includes a sentinel row after
  the allowed page.

## Final validation

The exact new-review regression suite completed with `232 passed` in 4.64
seconds. It covers the denied Jupyter renderer, adapter registration, one
completion target-request spy with D/E cases, BSL completion instruction, and
the bounded compact-classifier sentinel.

The transport/runtime nearby suite completed with `398 passed` in 8.48
seconds. The Jupyter, bundle, extension-lifecycle, and runtime-session suite
completed with `324 passed, 1 skipped` in 28.27 seconds; the materialization
bridge completed with `6 passed` in 3.03 seconds.

The CFE above was rebuilt after these changes with Designer 8.3.27.2170.
`python -m compileall -q src/onec_runtime packages/jupyter packages/mcp` and
`git diff --check` pass. `ruff` is not installed in this uv environment, so it
was not a validation gate.

## Final Sol P2 remediation

- Completion reserves the full closed page: one `R` marker plus 128 names.
  The controller requests `page_size=129`; the Runtime API requires an exact
  integer `collection_size` in `1..129` and requires that it equals the
  received row count before it parses the marker or any name. The boundary
  regression feeds a real `EvaluationResult` with the marker and 128 names,
  and separately proves that a reported 129-row result truncated to 128 rows
  raises `ProtocolError` rather than silently omitting `Поле127`.
- The former independent Python sentinel model has been replaced by an opt-in
  `live_1c` integration qualification. It builds a disposable CFE from the
  product source with three local fault probes: immediately after the actual
  serializer-loop cell read, immediately after the classifier cell read, and
  immediately before the actual query-result cell copy. The test installs
  that CFE in a disposable empty infobase, passes a 10,000-row
  `ТаблицаЗначений` with a sentinel immediately after `max_rows=3`, and also
  serializes a real `РезультатЗапроса` whose fourth row is the sentinel. The
  normal handler records `bounded` only when `ИнформацияОбОшибке().Описание`
  exactly equals `Превышен лимит строк компактной таблицы`; every other
  exception or probe is a failure.

  The same integration test has a controlled mutation variant which moves the
  real serializer row-limit guard after its cell read. That variant produces
  `serializer_probe|bounded`, while the unmodified ordering produces
  `bounded|bounded`; the live test therefore fails if the serializer guard is
  moved after the read it must protect.

The opt-in live qualification ran with
`ONEC_RUN_EXTENSION_BUNDLE_INTEGRATION=1`: `1 passed, 6 deselected in 51.34
seconds` in the initial probe and `2 passed, 6 deselected in 103.77 seconds`
with the final sensitivity mutation. This is live 1C evidence against a
temporary infobase; it is not a claim about the static unit suite. The
released product BSL sources did not change in this P2 pass, so the checked-in
protocol-2/artifact-0.1.3 CFE and manifest remain the bundle recorded above.

The final direct harness suite completed with `131 passed, 8 skipped` in 2.80
seconds. The broader affected unit suite completed with `571 passed` in 36.76
seconds. `python -m compileall -q src/onec_runtime packages/jupyter
packages/mcp` and `git diff --check` pass after the final change.

## Task 8 lifecycle completion

- `OnecValueProxy.to_df()` transfers a prebuilt `CaptureTransferPlan` through
  the controller-owned coordinator. Its first value-consuming generated BSL
  branch admits the root before it may serialize or publish a temporary
  context value. The coordinator owns the acknowledged capability, pin and
  cleanup lease; a caller deadline detaches without fetching or decoding the
  private payload.
- The lifecycle regressions cover pending, busy-without-redispatch, denied and
  malformed admission, confirmed BSL failure, dispatch uncertainty and cleanup
  dispatch uncertainty. They assert the public typed outcomes
  `CaptureEvaluationPendingError`, `CaptureBusyError`,
  `CaptureValueAccessDeniedError`, `CaptureValueCheckError`,
  `CaptureOutcomeUnknownError`, and `CaptureRecoveryRequiredError` rather
  than a Worker-object error.
- Recursive `materialize_value()` and generic `project_value()` now each send
  one dynamic instruction. It performs `ДопуститьЗначение`, then determines
  the server route, serializes the table or recursive value, and only then
  writes the sealed payload. Python identifies that route only from the
  integrity-checked payload header; it does not perform a target-side
  guard-then-use route query. Captured calls use explicit
  `MATERIALIZATION_HELPER` or `INSPECTION` coordinator records.
- Completion remains a single bounded consumer-owned collection request whose
  server helper admits before enumerating names. Its 129-row closed page keeps
  the admission marker plus all 128 permitted names.

No 1C module changed in this lifecycle pass, so the checked-in protocol-2,
artifact-0.1.3 CFE and four-source manifest remain the matching Designer-built
bundle recorded above.

## Lifecycle validation

- RED `5780fca` adds recursive materialization and projection regressions that
  fail when a separate public route request is made. GREEN `fd18e93` makes
  both paths composite and updates every affected fake to supply only the
  sealed result envelope.
- `uv run python -m pytest tests/unit/test_runtime_api.py
  tests/unit/test_capture_materialization_lifecycle.py
  tests/unit/test_jupyter_value_proxy.py tests/unit/test_completion_fields.py
  tests/unit/test_prototype_runtime.py -q` completed with `334 passed` in
  8.31 seconds.
- The focused composite set completed with `240 passed` in 6.70 seconds;
  the dynamic source parser check wrapped both generated instructions in a
  BSL procedure and passed the pinned parser.
- `python -m compileall -q src/onec_runtime packages/jupyter/src
  packages/mcp/src` and `git diff --check` pass.

## Lifecycle completion follow-up

- The capture-transfer controller now shields the debugger workspace before
  its one composite materialization request and restores the full workspace
  after the confirmed remote result, before admission policy or any optional
  payload continuation. A restore failure therefore reports
  `CaptureRecoveryRequiredError` and cannot trigger a payload fetch or a
  second materialization dispatch.
- Table and recursive value materialization both have coordinator lifecycle
  regressions: acknowledged pending receipt, explicit
  `MATERIALIZATION_HELPER` status, a second call rejected as
  `CaptureBusyError` without redispatch, and a late sealed result cleaned by
  the owner with no payload fetch/decode. The table route also proves an
  interrupted initiating waiter detaches on `KeyboardInterrupt` and does not
  cancel the owner cleanup.
- RED `fbee17f` demonstrated that a confirmed table envelope could reach its
  continuation without a transfer workspace restore. GREEN `a10dd68` adds
  the controller-owned shield/restore contract to the first transfer step.
  `065212f` records the direct interrupt regression and `7b1b2b6` updates
  the test transport to distinguish a user cell's message-key cleanup from a
  transfer cleanup. The latter also updates stale state-store expectations to
  sole protocol `2`.

The materialization lifecycle/restore exact suite passed `15 passed` in 2.18
seconds. The expanded coordinator/runtime/backend suite passed `477 passed`
in 10.94 seconds. Jupyter/value routes passed `51 passed` in 25.61 seconds;
the MCP minimum passed `23 passed` in 2.94 seconds. The final full unit suite
passed `4554 passed, 58 skipped` in 249.59 seconds (one existing Windows ZMQ
Proactor warning).

## Final Task 8 Session wait-handoff correction

- `RuntimeSession` now binds every public materialization route to
  `capture_session_caller_handoff` while it owns `_operation_lock`: `to_df`,
  `project_to_df`, `materialize`/`materialize_value`, `project_value`,
  `materialization_kind`, and the table/value/projection payload routes.
  There is no compatibility adapter; the test Runtime API exposes the same
  required method and enters the supplied factory.
- The helper callback composes the existing API writer handoff around the
  Session release.  It drops API writer ownership first and the Session lock
  second; on result, timeout, or `KeyboardInterrupt`, it reacquires the
  Session lock before the API writer.  The binding remains thread-local and
  is removed on every public-call exit.
- A production-path regression creates `OnecValueProxy(RuntimeSession)`, has
  RDBG acknowledge its table materialization and withhold the result, then
  starts a second Session call.  The second call receives `CaptureBusyError`
  before 100 ms, the first waiter remains pending until its deadline, one
  dispatch was sent, and `status`/`wait(0)` remain available.  A companion
  regression proves a `KeyboardInterrupt` returns with the Session operation
  lock reacquired.  A parameterized surface test exercises every listed
  Session materialization/payload route and verifies the required handoff
  binding and lock restoration.

RED `a6a83e1` reproduces the real Session/proxy deadline blockage. GREEN
`224d345` supplies the composed handoff and the route, timeout, and interrupt
regressions. No BSL module changed, so the matching protocol-2/artifact-0.1.3
CFE and four-source manifest remain unchanged.

The exact Session regression suite passed `17 passed` in 3.03 seconds. The
expanded RuntimeSession/coordinator/Jupyter/MCP suite passed `673 passed` in
45.33 seconds. `python -m compileall -q src/onec_runtime
packages/jupyter/src packages/mcp/src` and `git diff --check` pass.
The final unit suite passed `4565 passed, 58 skipped` in 252.29 seconds, with
only the existing Windows ZMQ Proactor warning.

## Final Task 8 pre-submit handoff correction

- The controller now adopts the `CaptureEvaluationTicket` while the initiating
  Session operation lock and Runtime API writer are still owned.  The composed
  helper handoff begins only after successful `submit_evaluation()` and wraps
  only `ticket.wait_initiator()`.  A `return_ticket` caller never enters the
  handoff.
- The Task 7 submit/adoption `BaseException` path remains around the unchanged
  adoption operation.  It still inspects coordinator state and restores the
  controller to `CAPTURED` only when no record was adopted; an adopted record
  remains coordinator-owned for normal late-result handling.
- The Session-backed production regression places a barrier directly in
  `CaptureEvaluationCoordinator.submit_evaluation()`.  Before the barrier is
  released, neither the Session lock nor API writer is acquirable and a second
  `OnecValueProxy.to_df()` cannot cross admission.  After release, the first
  request has an active `MATERIALIZATION_HELPER` ticket and exactly one
  dispatch; the second caller promptly receives `CaptureBusyError` while the
  first remains in the owned wait.  It therefore detects a handoff moved ahead
  of ticket adoption as well as a missing handoff around the wait.

RED `9ec1c71` establishes the pre-submit barrier failure. GREEN `d1612a1`
separates submit/adoption from the coordinator wait in the common controller
path, covering every helper/materialization route without a compatibility
layer. No BSL module changed; the protocol-2/artifact-0.1.3 CFE and four-source
manifest remain unchanged.

The exact barrier, ACK/withheld, interrupt, and Session-route set passed
`18 passed` in 3.62 seconds. The expanded lifecycle/runtime/Jupyter/MCP suite
passed `674 passed` in 45.77 seconds. The final unit suite passed `4566
passed, 58 skipped` in 252.71 seconds, with only the existing Windows ZMQ
Proactor warning. `python -m compileall -q src/onec_runtime
packages/jupyter/src packages/mcp/src` and `git diff --check` pass.

## Completion inspection lifecycle ownership

`RuntimeSession.completion_fields()` now binds the existing composed capture
handoff while it owns its operation lock. `PrototypeRuntimeApi.completion_fields()`
validates the local Context path under the data-plane writer, then submits one
`INSPECTION` instruction through `_execute_worker_instruction`. In a captured
runtime that is a coordinator-owned `execute_system_capture` request: ticket
submission and adoption occur while the Session and API locks remain held; the
composed handoff encloses only the initiator ticket wait.

The generated BSL calls
`RuntimeValueTransferServer.ПолучитьДопущенныеИменаСвойствДляПодсказки` and
serializes its bounded marker-plus-names table into a scalar `C<TAB>count`
contract. The parser requires the declared count, one root marker, and at most
128 unique identifiers. This preserves all 128 names and turns a truncated or
malformed reply into `ProtocolError`. The server helper's own source contract
admits the root before it enumerates names, so no denied root reaches a schema
or field read. The former `RuntimeController.inspect_completion_fields` and
its direct caller-thread `evaluate_collection` implementation were removed;
there is no compatibility adapter.

The captured RuntimeSession regressions cover a ready inspection, confirmed
D/E admission outcomes, malformed reply, confirmed BSL error, dispatch
uncertainty, restoration failure, caller deadline, and `KeyboardInterrupt`.
For an acknowledged withheld result they assert one `INSPECTION` record and
capability, a shielded workspace, accessible `status`/`current_capture`/`wait`,
a prompt second-call `CaptureBusyError` without redispatch, and a late result's
single full workspace restoration. The interruption case proves the operation
lock is reacquired while the coordinator retains the pending inspection.
The generated instruction is parsed by the pinned BSL parser; the extension
source contract proves helper admission precedes its field-name read.

RED `6179224` makes a real Session completion wait for an owned inspection
instead of accepting a direct collection call. GREEN `be2f526` provides the
coordinator route, scalar wire parser, Session handoff, and outcome matrix.
No BSL module changed in this correction, so the checked-in Designer-built CFE,
sole protocol `2`, artifact `0.1.3`, and four-source manifest remain unchanged.

The exact completion/control-plane suite passed `25 passed` in 2.61 seconds.
The expanded completion/coordinator/runtime/Jupyter/MCP suite passed `599
passed` in 46.26 seconds. The final full unit suite passed `4576 passed, 58
skipped` in 258.08 seconds, with only the existing Windows ZMQ Proactor
warning. `uv run python -m compileall -q src/onec_runtime packages/jupyter/src
packages/mcp/src`, `git diff --check`, and the no-`inspect_completion_fields`
source check pass.

## Ready-state completion lifecycle correction

A non-CAPTURE `completion_fields()` call previously reached
`_execute_worker_instruction()` and therefore `execute_system_main()`. That
created a new MAIN operation for a read-only schema request, changing the
operation id, state, active operation, registry, histories, workspace,
journal, and continue count.

The controller now owns `execute_system_inspection()`: it accepts only the
trusted bounded completion scalar expression, evaluates it directly through
RDBG, and returns only scalar text. It does not allocate an operation, change
the registry or workspace, write the recovery journal, or continue the target.
A confirmed target failure remains `BslExecutionError`; an unexpected RDBG stop
propagates without partially changing the prior ready lifecycle. CAPTURE keeps
the coordinator-owned `INSPECTION` ticket path unchanged.

Both branches execute the same server expression,
`RuntimeValueTransferServer.СериализоватьДопущенныеИменаСвойствДляПодсказки`.
The new bounded server serializer calls the existing root-admitting schema
helper before iterating names and emits the checked scalar `C<TAB>count`
format. The direct scalar evaluation allows 75,000 bytes, covering the marker
and 128 maximum-length Unicode identifiers. No collection API or standalone
completion guard was restored.

The real `PrototypeRuntimeController` regressions establish a completed MAIN
before inspection, then prove a successful scalar read leaves operation id,
state, active operation, registry identity, histories, workspace, journal, and
continue count unchanged. A synthetic `UnexpectedStop` and a confirmed BSL
error each preserve the same snapshot. The generated expression is parsed by
the pinned BSL parser and the server source test proves admission occurs before
field enumeration.

RED `187c688` demonstrates the second-MAIN mutation. GREEN `7f8c2fd` adds the
immediate controller route, scalar extension function, fixture updates, and a
Designer-rebuilt bundle. The checked-in CFE was rebuilt with Designer 8.3.27.2170:
21,951 bytes, SHA-256
`0b0a871ba44dc0250abe2d5bafc62b38060ef9a61cace94a1330d9dbe20ce924`.
The regenerated manifest matches the canonical four-source fingerprint and
retains sole protocol `2` and artifact `0.1.3`; `ConfigDumpInfo` did not change.

The final focused completion/controller/extension suite passed `82 passed` in
2.66 seconds. The expanded completion/coordinator/runtime/Jupyter/MCP/bundle
suite passed `657 passed` in 46.61 seconds. The final full unit suite passed
`4579 passed, 58 skipped` in 251.16 seconds, with only the existing Windows ZMQ
Proactor warning. `uv run python -m compileall -q src/onec_runtime
packages/jupyter/src packages/mcp/src`, `git diff --check`, and the manifest
fingerprint/protocol/artifact verification pass.
