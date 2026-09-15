# Capture Inspection API Design

**Date:** 2026-09-15
**Status:** Approved after independent written-spec review; ready for implementation

## Problem

CAPTURE pauses a business call and exposes `КонтекстОтладки` to BSL cells, but
Python inspection is currently split across low-level dictionary APIs such as
`runtime.runtime_api.capture_stack()` and value proxies. Stack frames contain
physical module identifiers instead of familiar configuration names. Frame
variables have no debugger-like hierarchy. A timed-out or interrupted CAPTURE
evaluation can also leave the controller in `evaluating_capture`, after which a
second call may hide the real state behind an unrelated error about Worker
generation objects.

The first version of the new API must make the common debugging path obvious:
acquire the current capture, confirm the source location quickly, inspect
`КонтекстОтладки`, and expand only the values the user asks for.

## Goals

- Make `КонтекстОтладки` the primary high-level inspection surface.
- Show the stack using configuration module names and source line numbers.
- Offer a fast stack mode that never parses BSL and an explicit enriched mode
  that adds method names and parameters.
- Support bounded, sequential expansion of structures, collections and tables.
- Give callers a non-blocking view of CAPTURE lifecycle state after timeout or
  interruption.
- Cover the lifecycle and diagnostic aspects of both observed scenarios from
  GitHub issue #5: a pending user `%%bsl` evaluation and a pending internal
  public-value guard reached through `OnecValueProxy.to_df()`.
- Keep every live object fenced to the exact capture stop.
- Reuse syntax information already produced by hot reload.
- Resolve configuration sources in both Designer export and EDT project layouts.
- Keep the core data model suitable for a later MCP adapter.

## Non-goals

- Python-side mutation of captured values. Mutations continue to use `%%bsl`
  and `КонтекстОтладки`.
- An automatically injected global `capture` variable in Jupyter.
- Arbitrary BSL expression evaluation through the inspection API.
- Redesigning bulk materialization or DataFrame transfer. The public-value guard
  and any other internal CAPTURE evaluation reached from `to_df()` are in scope
  for lifecycle ownership, but the transfer itself is unchanged.
- Caching or optimizing repeated public-value guards, diagnosing why 1C/RDBG
  sometimes does not return an evaluation result, or providing in-place
  recovery after genuinely uncertain dispatch.
- Live 1C qualification of the RDBG-stall scenarios from issue #5. It remains
  opt-in follow-up work and is never inferred from fake-transport tests.
- Generic expansion of every 1C collection or object type. Version one supports
  only the shapes listed in the value-expansion matrix below.
- New MCP tools in the first version.
- Source layouts other than Designer export and EDT project/`src` layouts.
- Compatibility guarantees for the current low-level capture dictionaries.

## User-facing entry point

The capture is acquired explicitly:

```python
capture = runtime.current_capture()
```

No Jupyter global alias is created. This makes capture identity explicit,
avoids namespace collisions and works in ordinary Python as well as Jupyter.

`current_capture()` returns a `CaptureView` bound to the current capture fence.
It may return that view while a CAPTURE evaluation is still pending so that the
caller can inspect lifecycle state. If no capture fence exists, it raises
`NoActiveCaptureError` and includes the current runtime state.

The initial surface is deliberately small:

```python
capture.status()
capture.wait(timeout_s=10)
capture.context
capture.stack
```

## Live views and snapshot pages

`CaptureView`, `CaptureContextView`, `DebugFrame` and `ValueNode` are live
descriptors. They contain a safe symbolic path plus the fence that identifies
the active stop. A new slice or child request reads the current debugger-side
state.

Pages are immutable snapshots:

```python
page = capture.context.locals[:20]
```

Displaying `page` repeatedly does not contact RDBG, scan source directories or
parse BSL. To refresh the values, the caller executes the slice again:

```python
page = capture.context.locals[:20]
```

This distinction also applies to stack and child pages. A page remains
displayable after the capture ends. A live descriptor cannot issue another
request after its fence becomes stale.

All `repr`, `str` and Jupyter rich-display implementations operate only on data
already stored in a snapshot or descriptor. They perform no debugger or source
I/O.

## Capture context

`capture.context` represents the `КонтекстОтладки` namespace used by CAPTURE
BSL cells:

```python
capture.context.parameters[:20]
capture.context.locals[:20]
capture.context.variables[:20]
capture.context.locals["СтруктураДанных"]
```

- `parameters` contains values whose names match the parameters of the stopped
  source method, in source order.
- `locals` contains the remaining values and does not duplicate parameters.
- `variables` is the complete lower-level view and does not require method
  resolution.

Requesting `parameters` or `locals` explicitly resolves the stopped method.
If the source or method is unavailable, the request raises
`CaptureSourceUnavailableError` and directs the caller to `variables`; it does
not guess a classification. This rule is the same for `capture.context` and
arbitrary stack frames.

BSL identifiers are matched case-insensitively while preserving the spelling
reported by the debugger or source. An exact-name lookup must produce one
value. A missing or ambiguous match raises a typed lookup error.

The context is live for the same stop. A BSL cell may change it:

```bsl
КонтекстОтладки.Оклад = 55000;
```

A later Python slice reads the new value. Previously obtained pages retain the
old presentation.

`capture.context` and the physical frame are deliberately different views.
The context is a staged structure copied from the frame at capture entry. Until
resume performs writeback, rebinding a staged field—for example, assigning a
new scalar to `КонтекстОтладки.Оклад`—is visible through `capture.context` but
the corresponding `capture.stack[0].variables` binding remains unchanged.
In-place mutation of a mutable object referenced by both views may be visible
through both views according to normal 1C reference semantics. Repeating either
slice must report the current value of its own view.

## Stack views

### Fast source stack

The default mode resolves module names and line numbers without building an
AST:

```python
stack = capture.stack[:20]
```

```text
Стек вызовов
├─ #0 Документ.ПриемНаРаботу.МодульОбъекта:186
├─ #1 ОбщийМодуль.ПлановыеНачисленияСотрудников:742
└─ … скрыто 3 служебных кадра
```

`capture.stack` contains application/source frames. Consecutive runtime frames
are represented by collapsed markers. Each visible `DebugFrame` retains its
`native_level`, so it can be correlated with the physical RDBG stack.

Each saved frame also retains a `SourceVersionRef`. For a Worker frame this is
the exact artifact/generation identity that owns the physical frame. For a
configuration frame it is the trusted-export path and file signature observed
while constructing the fast page. Method enrichment must use this pinned
reference; it never selects source merely because that source is currently the
latest hot reload version.

`capture.stack[0]` means the first visible source frame, not necessarily native
RDBG level zero. The exact physical stack is available separately:

```python
capture.stack.native[:20]
capture.stack.native[3]
```

The native view performs no source scan or AST parse. Default presentation does
not expose UUIDs; the native data model may retain bounded physical identity for
diagnostics.

If a configuration source cannot be mapped, the frame remains in the stack:

```text
#2 Модуль конфигурации:417 — исходный файл не найден
```

### Method-enriched stack

Method names are an explicit second step on an existing page:

```python
detailed = stack.with_methods()
```

```text
Стек вызовов
├─ #0 Документ.ПриемНаРаботу.МодульОбъекта
│     ОбработкаПроведения(Отказ, РежимПроведения):186
└─ #1 ОбщийМодуль.ПлановыеНачисленияСотрудников
      РассчитатьПоказатели(...):742
```

`with_methods()` does not reread RDBG. It enriches the frames already present
in the saved page and returns a new immutable page. It deduplicates modules,
uses the shared syntax registry first and parses only missing source versions.
Source enrichment has a finite timeout capped by the runtime command timeout.
Source enrichment is local work and does not change CAPTURE lifecycle state. If
its deadline expires, `with_methods()` returns a new partial page: frames
already resolved remain enriched and unresolved frames stay in module-and-line
form with `method_status="timeout"`. A missing or unparsable source similarly
falls back with its specific status instead of raising
`CaptureInspectionTimeout`.

A single frame can be enriched independently:

```python
frame = capture.stack[0]
detailed_frame = frame.with_method()
```

Each frame exposes:

```python
frame.native_level
frame.source
frame.line
frame.source_status   # "runtime_verified", "trusted_export" or "unavailable"
frame.detail          # "line" or "method"
frame.method          # None until resolved
frame.method_status   # "not_requested", "resolved", "timeout", "source_changed" or "unavailable"
frame.variables[:20]
frame.parameters[:20]
frame.locals[:20]
```

`variables` uses RDBG data without an AST. Requesting `parameters` or `locals`
is an explicit source-dependent operation and resolves the method only for
that frame if necessary.

## Sequential value expansion

A variable is represented by a `ValueNode`:

```python
data = capture.context.locals["СтруктураДанных"]

data.name
data.type_name
data.preview
data.size
data.expandable
```

The preview is bounded to 512 characters. Containers display a summary and an
expansion marker; they are not materialized automatically.

Every node has a universal child view:

```python
data.children[:20]
data.children["ЗначенияПоказателей"]
```

Known 1C container shapes also expose semantic aliases:

```python
structure.fields[:20]
array.items[:20]
table.columns[:20]
table.rows[:10]
row.fields[:20]
```

Aliases return the same page and node model as `children`; they only select the
appropriate child kind. Version one supports this explicit matrix:

| Shape | Views | Backing operation | Bound |
| --- | --- | --- | --- |
| Capture context | `variables`, `parameters`, `locals` | Re-enumerate the current staged context through a bounded server-owned projection for each fresh page | API pages contain at most 100 variables. |
| Other native frame | `variables`, `parameters`, `locals` | One fresh `evalLocalVariables` inventory for each page request, then apply the requested page locally | The API page and each preview are bounded, but RDBG does not page the inventory itself. |
| Structure or fixed structure | `children`, `fields` | Bounded property-name discovery followed by a projection of only those fields | `Структура` and `ФиксированнаяСтруктура` only; at most 100 requested field names and one-level previews. |
| Array or fixed array | `children`, `items` | RDBG collection paging or an equivalent server-owned bounded projection | At most 100 elements from the requested offset. |
| Value table | `children`, `columns`, `rows` | Schema-width/name discovery followed by a bounded row projection | At most 100 rows and 100 selected columns. Discovery may inspect schema metadata required to detect a wider table, but no row values are fetched when the schema is rejected. |
| Value-table row | `children`, `fields` | An exact bounded row projection for every fresh child request | At most the supported 100-column schema. |

Other shapes return `CaptureShapeUnsupportedError`. In particular, arbitrary
property-bearing application objects, map keys, value trees and undocumented
RDBG collection layouts are not claimed as version-one capabilities. They can
be added only after a protocol fixture and live qualification establish their
paging and presentation behavior.

Exact child lookup uses a validated field name or non-negative index supported
by the node's declared shape. User text is never interpolated as an arbitrary
BSL expression.

Slices must have finite non-negative `start` and `stop`, no step, and contain at
most 100 items. Negative indexing and unbounded iteration are rejected. Each
public request is bounded by the configured command timeout and a finite output
page. A single public request may use more than one internal RDBG operation—for
example, table schema discovery followed by row projection—but must not inspect
row values or property values outside the declared page. Native-frame inventory
and container schema/name metadata are the explicit exceptions: RDBG may return
an unpaged native inventory or schema response; where server-side projection is
available, an adapter reads at most one extra name as an overflow sentinel
before rejecting the shape. Deeper traversal requires another explicit child
request, so one public operation exposes one level.

Source/method metadata and the parameter-versus-local name classification may
be cached for the pinned source version. Debugger values, previews and mutable
container membership are never reused to satisfy a new slice. The unavoidable
unpaged `evalLocalVariables` inventory for a native frame is performed again
for each such request and is reported honestly in work counters.

An example page is self-contained:

```text
КонтекстОтладки.locals [0:20]
├─ Сотрудник: СправочникСсылка.Сотрудники = Иванов Иван Иванович
├─ Оклад: Число = 50 000
├─ СтруктураДанных: Структура, 8 полей ▸
└─ ЗначенияПоказателей: ТаблицаЗначений, 146 строк ▸
```

### Value privacy

Safe paths constrain what can be executed; a separate `CaptureValuePolicy`
constrains what can be disclosed. Every variable page, exact lookup and child
page applies the existing public-value guard before exposing a preview, handle
or descendants. The check follows the underlying value identity and therefore
also rejects an ordinary-looking alias that refers to a Worker generation
object.

The validation order is fixed:

```text
capture fence and lifecycle state
  → safe path and shape
  → public-value policy for the known root
  → bounded debugger projection into private temporary handles
  → public-value policy for every projected value
  → normalized snapshot
```

The policy check may itself use the existing bounded Worker-type probe. No
projected value is formatted, serialized or returned before that value passes
the guard. Temporary handles are controller-owned and are removed after the
page is normalized or the operation fails.

A denied value may retain its variable name in the containing frame page, but
its type and presentation are redacted:

```text
СлужебноеЗначение: <private runtime value>
```

It is not expandable. Exact access raises
`CaptureValueAccessDeniedError`. The native stack renderer continues to redact
runtime-kernel URLs, private module identity and internal generation handles.
Lifecycle failures such as `CaptureBusyError` are decided before the privacy
guard, so they cannot be mistranslated into a Worker-object error.

## Source module resolution

The resolver supports the two source layouts already accepted by the runtime:

- Designer export, where metadata descriptions are `Type/Name.xml` and module
  files are under `Type/Name/Ext`;
- EDT, where the configured root may be the project containing `src` or the
  `src` directory itself, metadata descriptions are `Type/Name/Name.mdo`, and
  module files are stored directly under `Type/Name`.

Root normalization and layout detection are shared with the existing
Designer/EDT common-module catalog. A root containing both a direct metadata
tree and an EDT `src` metadata tree is rejected as ambiguous. Format-native
configuration metadata supplies base/extension identity; a file-name pattern
alone never decides that identity.

The resolver does not build a complete configuration index at startup. For a
stack page it collects the distinct unresolved physical module identities and
resolves them as one batch:

```text
{(object_id, property_id, extension), ...}
                  ↓ one directory pass
{module name, module kind, source path, source identity}
```

The resolver:

- checks positive and negative caches first;
- scans only source roots and metadata kinds relevant to the unresolved batch;
- uses a layout adapter to map the same `(object_id, property_id, extension)`
  identity to the appropriate Designer or EDT metadata/module path;
- caches descriptions encountered during the scan;
- stops early when all requested identities are found;
- keeps base configuration and extension identities separate;
- scopes every cache key by project/source-root identity, base configuration or
  extension, module type, object ID and property ID.

There is no background filesystem watcher in version one. Source-catalog
generation advances through two explicit inputs:

- a successful hot reload invalidates syntax for its known source unit and, if
  it introduces or renames a unit, advances the affected catalog generation;
- `runtime.refresh_capture_sources()` explicitly advances the generation for a
  configured source root after files or metadata objects are added, removed or
  renamed outside hot reload.

Positive mappings for an unchanged metadata description remain cached. A
negative result remains valid only for the catalog generation in which it was
created, so an explicit refresh makes a newly added module discoverable.

The fast stack requires this module mapping but never requires an AST. Both
layouts must resolve at least common-module modules and document object modules,
including `Документ.ПриемНаРаботу.МодульОбъекта`, through the same public model.

## Shared syntax registry and hot reload

Hot reload already calls `parse_full_ast_module()` and retains a compact
`ParsedModuleModel` for each active Worker module. The source projection must be
generalized so capture can reuse the same parse without depending on Worker
internals.

The shared projection contains only durable source facts:

```python
ModuleSyntaxIndex(
    source_sha256=...,
    parser_identity=...,
    methods=(
        MethodSyntaxInfo(
            name="ОбработкаПроведения",
            span=...,
            parameters=("Отказ", "РежимПроведения"),
        ),
    ),
)
```

The full AST and token stream remain temporary. Hot reload publishes the
compact index to `ModuleSyntaxRegistry` after parsing. Capture looks up the
index by logical module identity, exact source hash and parser identity.

The registry is versioned rather than destructively invalidated:

```text
(module identity, source_sha256, parser identity) → ModuleSyntaxIndex
```

Successful hot reload makes the new source hash available to operations that
are subsequently pinned to the new Worker generation. It does not retarget a
MAIN operation that already owns an older generation pin. A later
`stop_sequence` of that same MAIN therefore continues to resolve Worker frames
through the older artifact and syntax version. Unknown promotion outcome does
not activate the candidate syntax version.

Source selection is performed for each physical frame. Worker frames use the
owning artifact/generation and the existing strict Worker source mapping.
Configuration frames use the explicitly configured Designer or EDT source tree
and carry `source_status="trusted_export"`; matching metadata UUIDs do not
upgrade that status to runtime verification.

The fast page pins each trusted-export file's path, size and modification time.
`with_methods()` opens the pinned path and checks that signature both before
and after reading the same open file. If either signature differs from the fast
page, or the file changes during the read, enrichment returns
`method_status="source_changed"` and requires a fresh stack page. If the file
is unchanged, enrichment computes its content hash, parses it and retains that
hash in the enriched snapshot. This detects changes visible through the pinned
file signature, including changes during the read. A trusted export is not a
cryptographic runtime proof: replacement content that preserves both size and
modification time is outside this guarantee and remains
`source_status="trusted_export"`.

If a module in an enriched stack has never participated in hot reload, capture
parses that exact configured source lazily, projects the same compact model and
adds it to the registry. A basic stack request never takes this path.

## Capture state and interrupted evaluations

The capture handle exposes control-plane state without contacting RDBG:

```python
status = capture.status()
```

The status read uses a thread-safe controller snapshot and must not wait behind
the single-writer path of a pending evaluation.

`CaptureStatus` contains:

```text
operation_id
capture_generation
stop_sequence
phase
can_inspect
can_resume_capture
can_wait
pending_evaluation_id
evaluation_kind
last_evaluation_id
last_user_evaluation_id
evaluation_timing
failure
```

`pending_evaluation_id` is `None` when no evaluation is outstanding.
`evaluation_kind` is `None` in that case; otherwise it is one of the safe enum
values `user_bsl`, `public_value_guard`, `inspection` or
`materialization_helper`. It never contains BSL source, a value handle, a
module identity or Worker data.

`last_evaluation_id` identifies the most recent settled waitable outcome for
this capture. `last_user_evaluation_id` preserves the most recent user-BSL
outcome when a detached internal evaluation settles later. Both are `None`
before a matching evaluation. `evaluation_timing` describes the pending
evaluation when present, otherwise the record selected by
`last_evaluation_id`. `failure` is either `None` or an immutable, bounded
`CaptureFailureDiagnostic` with stable `code`, sanitized `message` and
`recommended_action`. It is populated only in a terminal controller failure
state. The remaining fence fields needed for validation stay internal and are
not included in ordinary presentation.

Public phases are:

- `paused`: stack and context can be inspected;
- `evaluating`: one user or internal CAPTURE evaluation is pending;
- `resuming`: writeback, CAPTURE cleanup or Continue is in progress;
- `recovery_required`: evaluation or resume cleanup did not restore the
  controller invariants;
- `outcome_unknown`: dispatch occurred but its result cannot be established;
- `stale`: the handle's stop is no longer current.

The public API does not model a nested debugger stop inside a CAPTURE cell.
CAPTURE BSL is executed through `evalExpr`, and the live contract establishes
that a breakpoint in a called Worker method is ignored during that evaluation.
Defensive handling of an unexpected RDBG stop remains an internal controller
failure path rather than a version-one inspection capability.

The capability flags are derived from the phase and retained record rather
than set independently:

| Phase | `can_inspect` | `can_resume_capture` | `can_wait` |
| --- | --- | --- | --- |
| `paused` | yes | yes | yes only when an evaluation outcome is retained |
| `evaluating` | no | no | yes |
| `resuming` | no | no | yes only when an evaluation outcome is retained |
| `outcome_unknown` | no | no | yes; it returns the retained unknown outcome |
| `recovery_required` | no | no | yes only when an evaluation outcome is retained |
| `stale` | no | no | no |

### Evaluation ownership

`CaptureEvaluationCoordinator` is owned by the runtime controller. It is the
only consumer of the RDBG evaluation event stream. Python callers and Jupyter
magics observe its records through a condition/future; they never poll RDBG or
compete to consume a result.

This ownership applies to every `evalExpr` executed against a paused CAPTURE,
not only visible `%%bsl` cells. In particular,
`execute_system_capture()`, `_execute_worker_instruction()`, the Worker
public-value probe, bounded inspection projections and materialization helpers
must submit their target-side evaluation through the same coordinator. Direct
calls to `RdbgSession.evaluate()` from these paths are prohibited. At most one
coordinator record may own a pending RDBG capability for a capture.

Internal call sites pass `evaluation_kind` explicitly; the coordinator never
infers it by inspecting generated BSL text. Pin installation/cleanup and generic
transfer plumbing use `materialization_helper` unless a more specific caller
labels the operation `public_value_guard` or `inspection`.

Submission transfers the evaluation to the coordinator before transport I/O.
The coordinator, rather than the requesting Python call stack, owns dispatch,
the evaluation-generation pin, event consumption, temporary handles and
workspace restoration. Synchronous `%%bsl`, proxy and inspection calls are
waiters on that operation. They must not hold the runtime single-writer lock
while blocked on its future. A Python `KeyboardInterrupt` therefore cannot
cancel the coordinator halfway through dispatch or cleanup.

Before an operation can reach a transport dispatch, the coordinator creates a
`CaptureEvaluationRecord` containing:

```text
evaluation_id                 # controller-generated stable ID
evaluation_kind               # safe enum only
capture fence
private BSL source or internal-plan identity
evaluation-generation pin
dispatch state and optional RDBG capability
message collector
workspace-restoration state
downstream continuation state
initiating-waiter state
public-observer flag
timing evidence
immutable outcome, when settled
```

The private source/plan identity is used only to execute and correlate the
operation; it is excluded from status, journal events, exceptions and ordinary
object representation. An internal composite call has exactly one initiating
waiter and an explicit downstream continuation. If that initiating waiter
detaches, the coordinator finishes the already dispatched evaluation and
required cleanup but marks that continuation abandoned. For example, a late
`False` result from a detached
`public_value_guard` restores CAPTURE to `paused`, but does not start table
transfer for the abandoned `to_df()` call.

Calls to `capture.wait()` are observers. Attaching, timing out or interrupting
an observer never detaches the initiating waiter, abandons its continuation or
changes coordinator ownership. The first observer attachment marks the record
publicly observed so its eventual safe outcome is retained for repeated reads.

The public `evaluation_id` identifies that logical coordinator operation.
Private RDBG result IDs identify its remote steps. A bounded inspection plan may
require more than one sequential remote step, including removal of a temporary
handle, but the record owns at most one RDBG capability at a time and never
starts an optional downstream step after its initiating waiter detaches.
Required cleanup
remains part of the record. A normal terminal outcome and return to `paused`
require confirmed cleanup. An abnormal `unknown` or `failed` outcome may be
published when cleanup cannot safely run or complete, but records explicit
`cleanup_status` (`not_started`, `unknown` or `failed`), keeps its cleanup leases
quarantined and prohibits inspection/resume until shutdown or recovery.

Temporary context values use coordinator-owned cleanup leases. When a waiter
unwinds after timeout or `KeyboardInterrupt`, its caller-side `finally` block
transfers the lease to the record and must not call `drop_context_value()` or
dispatch any other cleanup evaluation. The coordinator removes the value only
after the current capability settles and the next required remote step can be
started safely.

The evaluation-generation pin and restoration responsibility belong to the
record, not to any initiating or observing caller. The record remains the
single pending record until it settles. The coordinator retains two bounded
settled slots: the last `user_bsl` record and the last publicly observed,
initiator-detached or abnormally terminated internal record. A normally
completed internal helper with an attached initiator and no observer is returned
to its caller and discarded. Thus an internal guard never erases the retained
user-cell outcome, while an observed or interrupted guard remains available to
`capture.wait()`. Each retained ID is repeatable until its slot is replaced by a
later qualifying record or its capture fence becomes stale. Outcome objects
already returned to Python remain immutable snapshots.

State transitions are defined at the failure boundaries:

| Boundary | Coordinator action | Public result |
| --- | --- | --- |
| Coordinator cancellation or failure before transport dispatch is possible | Mark the record failed, release its pin and keep the capture `paused` | Retained `failed` outcome |
| Transport call was entered but dispatch/acceptance cannot be proved or disproved | Quarantine the pin in the capture record and prohibit inspection/resume | `outcome_unknown` with retained `unknown` outcome |
| RDBG acknowledges a pending evaluation | Bind its capability to the record and keep sole event ownership | `evaluating`; dispatch is no longer uncertain |
| Initiating-caller timeout after acknowledgement | Detach only the initiating waiter; retain capability, pin, workspace and continuation state | Remain `evaluating`; notebook `%%bsl` returns pending and proxy/inspection APIs raise `CaptureEvaluationPendingError` |
| Initiating-caller `KeyboardInterrupt` after acknowledgement | Propagate the interrupt only to that caller and mark its continuation abandoned | Remain `evaluating`; `capture.wait()` can receive the late result |
| Late result and all required cleanup succeed | Publish the retained outcome, release the evaluation pin and return to `paused` | `completed` or `failed` according to the confirmed result; no redispatch |
| Evaluation result, workspace restoration and public-result normalization succeed with the original waiter attached | Publish result/messages, release the evaluation pin and return to `paused` | Retained `completed` outcome for `user_bsl`; internal result is returned to its caller |
| BSL evaluation fails and workspace restoration succeeds | Publish the BSL error, release the evaluation pin and return to `paused` | Retained `failed` outcome |
| Confirmed local normalization/delivery fails after restoration | Publish no raw result, release the evaluation pin and return to `paused` | Retained `failed` outcome with the typed delivery error |
| Workspace restoration fails after any evaluation result | Retain diagnostic ownership and prohibit inspection/resume | `recovery_required` with a retained `failed` outcome and restoration diagnostic |
| An unexpected RDBG stop event arrives during CAPTURE evaluation | Retain the stop diagnostic without exposing it as a nested capture | `recovery_required` with a retained `failed` outcome |
| Debug target or RDBG session is lost | Invalidate the capture fence with the loss reason | `stale` |

`RDBG acknowledges` means the `evalExpr` transport request returned normally
and `RdbgSession.start_evaluation()` returned its registered
`PendingEvaluation` capability. Merely entering the transport call is dispatch
evidence, not acceptance evidence.

After acknowledgement, the coordinator calls `wait_evaluation_event()` in
bounded polling intervals. A `CommandTimeout` from one such interval means only
that no event arrived in that interval: the registered capability remains
pending and the coordinator polls it again. It does not publish `unknown`, stop
the coordinator or impose the detached caller's deadline on the remote
operation.

Once RDBG has returned the pending capability, caller timeout or interruption
must not call `retain_outcome_unknown()`, set RuntimeApi `_poisoned_error`, clear
the evaluation-generation pin or synthesize a Worker-promotion failure. Those
actions are reserved for a genuinely uncertain dispatch/acceptance boundary.
The coordinator is the only code allowed to poll the acknowledged capability,
and every retrying public API observes `CaptureBusyError` rather than sending a
second `evalExpr`.

The runtime must not clear an evaluation-generation pin or claim that the
capture is inspectable until the corresponding transition proves that doing so
is safe. The coordinator seals messages, restores its workspace and applies any
required result policy before publishing a terminal outcome; intermediate
result objects remain private. Recovery is an explicit controller operation
outside this read-only inspection API. Version one does not claim in-place
recovery from `outcome_unknown` or `recovery_required`: their diagnostic
directs the caller to close and restart the runtime, and teardown releases any
quarantined pins and temporary handles.

### Public-value guard result contract

The Worker public-value guard is an internal evaluation with
`evaluation_kind="public_value_guard"`. Only a confirmed Boolean result decides
privacy:

| Guard observation | Public behavior |
| --- | --- |
| `True` | Raise `CaptureValueAccessDeniedError`; do not expose or materialize the value |
| `False` | Admit the value; continue the operation only while its initiating waiter remains attached |
| Confirmed BSL error, invalid result or local guard failure | Raise `CaptureValueCheckError`; do not claim that the value is a Worker object |
| Acknowledged evaluation still pending at initiating-caller timeout | Keep `evaluating`, detach the initiating waiter and expose the record through `status()`/`wait()` |
| Dispatch/acceptance uncertain | Enter `outcome_unknown` |
| Workspace restoration failed | Enter `recovery_required` |
| `KeyboardInterrupt` in the initiating waiter | Propagate it to that caller; coordinator retains ownership |

No `BaseException` catch may translate timeout, interruption, busy state,
transport ambiguity or restoration failure into "Worker generation objects are
not public values". If a `to_df()` initiating waiter detaches during the guard, table
schema inspection and table transfer do not begin. A second `to_df()` while the
record is pending fails before privacy probing with `CaptureBusyError` carrying
the same safe evaluation ID and kind.

### Resume ownership

Accepting `resume` is an atomic control-plane transition from `paused` to
`resuming`, before the first staged-root transfer. At that moment all live stack,
context and value capabilities stop admitting new target requests;
`can_inspect` and `can_resume_capture` become false. Existing snapshot pages
remain displayable.

The runtime controller owns writeback, CAPTURE cleanup, Continue dispatch and
the wait for the next stop independently of the caller. `KeyboardInterrupt`
detaches the resume waiter but does not make the old capture inspectable again.
Successful Continue acknowledgement makes the old `CaptureView` stale even if
waiting for the next stop later times out. A new stop creates a new fence and
view. A failed writeback before any mutation may return to `paused` only when
the controller can prove no staged root changed; partial writeback, uncertain
cleanup or uncertain Continue enters `recovery_required`.

### Control-plane diagnostics and timing

`runtime.current_capture()`, `capture.status()` and `capture.wait()` use a
dedicated thread-safe control-plane snapshot/condition. They do not call
RuntimeApi `_require_available()`, acquire the user-operation single-writer lock
or perform a Worker public-value check. Consequently they remain reachable from
the next notebook cell while an acknowledged evaluation owns the data-plane
lock, generation pin or Worker quarantine. `status()` is always an immediate
local snapshot; `wait()` blocks only on the coordinator condition for the
selected evaluation ID.

Each coordinator record exposes safe `CaptureEvaluationTiming` evidence:

```text
evaluation_id
created_at_utc
elapsed_ms
dispatch_entered_ms
rdbg_acknowledged_ms
initiating_waiter_detached_ms
last_poll_ms
result_received_ms
workspace_restored_ms
outcome_published_ms
remote_step_count
poll_count
```

All `*_ms` values are optional non-negative monotonic offsets from record
creation, saturated at a fixed integer maximum. `created_at_utc` is rounded to
whole seconds. Counts are bounded integers. The snapshot contains no BSL source,
expressions, value handles, result values, module identity, target URL or Worker
manifest data.

`initiating_waiter_detached_ms` describes only the original
Jupyter/data-plane waiter that owns the downstream continuation. Timeouts and
interrupts of observer calls to `capture.wait()` do not change continuation
state or overwrite that evidence.

The journal records the corresponding evidence events:

- `capture_evaluation_record_created`;
- `capture_evaluation_dispatch_entered`;
- `capture_evaluation_rdbg_acknowledged`;
- `capture_evaluation_initiating_waiter_detached`, with safe reason `timeout` or
  `interrupt`;
- a coalesced `capture_evaluation_poll_progress` containing only bounded poll
  count and last-poll offset;
- `capture_evaluation_result_received`;
- `capture_evaluation_workspace_restored`;
- `capture_evaluation_outcome_published`.

Every event contains only evaluation ID, safe `evaluation_kind`, capture fence
counters, bounded elapsed fields, state and optional safe error category.
Remote steps include a bounded `step_index`; their RDBG result IDs and payloads
remain private. Poll evidence is updated in memory on every poll but journaled
only up to a fixed maximum number of progress events per record. Further polls
update the local status snapshot without appending journal records; terminal
publication, when it occurs, includes the final aggregate. An indefinitely
pending evaluation therefore cannot grow the journal without bound.

### Waiting for an outcome

`capture.wait(timeout_s=..., evaluation_id=None)` attaches to the matching
record. With no explicit ID it selects the pending evaluation, or otherwise the
record named by `last_evaluation_id`. An explicit ID can select the pending
record, the retained user record or the retained observed, detached or abnormal
internal record. It never resubmits BSL source. If the capture has no matching
evaluation record, it raises `NoCaptureEvaluationError`.

It returns an immutable `CaptureEvaluationOutcome`:

```python
outcome = capture.wait(timeout_s=10)

outcome.state          # "pending", "completed", "failed" or "unknown"
outcome.result
outcome.messages
outcome.error
outcome.diagnostic
outcome.evaluation_id
outcome.evaluation_kind
outcome.timing
```

Only fields appropriate to the state are populated. A wait timeout returns a
`pending` outcome; it does not raise `CommandTimeout`. `KeyboardInterrupt`
detaches that waiter without modifying the record, its generation pin or its
event consumer. A later call waits for or returns the same evaluation ID.
Once settled, repeated calls return the same immutable outcome. Messages are
an immutable sequence limited by the runtime command-output budget,
diagnostics are bounded and sanitized, and a completed `result` passes the same
public-value policy as other Python-visible values. `diagnostic` reports message
truncation when that budget is reached. A public outcome for any internal
evaluation never exposes its raw Boolean, helper result or target handle through
`result`; it reports only completion/failure, safe kind, timing and diagnostic.
Attaching `capture.wait()` is observation only and never revives an abandoned
`to_df()` or inspection continuation.

The original bounded inspection or proxy call raises
`CaptureEvaluationPendingError` with safe evaluation ID/kind when its
acknowledged internal evaluation outlives that caller's deadline. The name is
deliberately different from `CaptureInspectionTimeout`: it states that a remote
evaluation is still owned and may complete later. The waiter detached; the
remote evaluation did not fail or become unknown. The notebook `%%bsl` adapter
may instead display the equivalent `pending` outcome. `capture.wait()` itself
always returns `pending` on its own timeout.

Inspection while an evaluation is pending raises `CaptureBusyError` containing
the pending evaluation ID and phase. `CaptureOutcomeUnknownError` is used by
inspection/resume attempts in
`outcome_unknown`; `wait()` represents the same fact as an `unknown` outcome.
Inspection/resume in `recovery_required` raises
`CaptureRecoveryRequiredError` with the bounded controller diagnostic. These
errors must not be translated into public-value or Worker-generation errors.

The public error classes are:

- `NoActiveCaptureError`;
- `NoCaptureEvaluationError`;
- `StaleCaptureError`;
- `CaptureBusyError`;
- `CaptureOutcomeUnknownError`;
- `CaptureRecoveryRequiredError`;
- `CaptureEvaluationPendingError`;
- `CaptureInspectionTimeout`;
- `CaptureValueCheckError`;
- `CaptureLookupError`;
- `CapturePathError`;
- `CaptureSourceUnavailableError`;
- `CaptureShapeUnsupportedError`;
- `CaptureValueAccessDeniedError`.

`CaptureEvaluationPendingError` applies only after RDBG acceptance is known and
the coordinator still owns the evaluation. `CaptureInspectionTimeout` is
reserved for a bounded inspection/read deadline when no acknowledged evaluation
remains pending. Local source enrichment returns per-frame
`method_status="timeout"`. Neither exception is used for an ordinary
`capture.wait()` timeout.

### Runtime shutdown

`runtime.close()` has a control-plane shutdown path and must not wait to acquire
a single-writer lock held by a detached data-plane operation. It first marks the
coordinator closing and rejects new submissions, detaches all public waiters,
then invalidates/stops the RDBG target or transport so a pending poll can
terminate. The coordinator joins within the configured shutdown deadline. Only
after the event consumer has stopped, or target termination proves that no late
event can arrive, may teardown release coordinator-owned pins and temporary
handles.

If orderly target termination cannot be established within that deadline,
shutdown journals a bounded abandoned-operation diagnostic and lets supervised
process teardown own the remaining resources. It never waits indefinitely and
never reclassifies the pending value as a Worker privacy denial.

## Component boundaries

`src/onec_runtime` owns all behavior and typed models:

- `CaptureView`, `CaptureStatus`, pages, frames and value nodes;
- capture fence validation, the lock-independent control plane and
  `CaptureEvaluationCoordinator` for user and internal evaluations;
- bounded RDBG operation plans and safe value paths;
- `CaptureValuePolicy`, backed by the existing public-value guard;
- shared Designer/EDT source-root normalization and layout adapters;
- demand-driven configuration source resolution;
- pinned `SourceVersionRef` resolution;
- the shared `ModuleSyntaxRegistry` used by capture and hot reload.

`packages/jupyter` owns presentation only:

- plain-text tree rendering;
- HTML rendering of already-fetched pages;
- notebook display configuration;
- adapting the synchronous `%%bsl` experience to coordinator outcomes.

Jupyter formatters must not contact RDBG, scan files or parse source.
Ordinary completed and failed BSL outcomes keep their current notebook
presentation. If the notebook-side wait reaches its command timeout, the magic
returns a pending outcome with the evaluation ID and a `capture.wait()` hint
instead of poisoning the runtime with `CommandTimeout`. `KeyboardInterrupt`
still interrupts the cell, but the coordinator continues owning the operation;
the next Python cell can call `runtime.current_capture().status()` or `wait()`.

Existing proxy/materialization entry points consult the capture control-plane
state before `_require_available()` and before any privacy probe. While an
internal evaluation is pending, a repeated `to_df()` therefore receives
`CaptureBusyError` with safe evaluation ID/kind. It cannot reach the old broad
guard exception handler or begin another remote instruction.

`packages/mcp` gains no new public tools in the first version. Core models
nevertheless use finite pages, scalar metadata and explicit status so a later
MCP adapter can serialize them as `describe` and `list_children` operations
without accepting arbitrary expressions.

The new typed inspection engine is the canonical implementation. Existing
dictionary APIs have no compatibility guarantee. They may be removed, migrated
or temporarily retained as private adapters according to the smallest safe
implementation path; no deprecation shim is required. Existing MCP code that
currently calls those dictionaries is migrated in the same change or routed
through such a private adapter, so the repository remains internally coherent.

## Data flow

A fast stack request follows this path:

```text
capture.stack[:20]
  → validate fence and can_inspect state
  → page cached native RDBG frames for the stop
  → batch-resolve unknown module identities
  → pin one SourceVersionRef per visible physical frame
  → filter/collapse runtime frames
  → return immutable StackPage(detail="line")
```

Method enrichment follows this path:

```text
stack_page.with_methods()
  → collect unique pinned unresolved source versions in the page
  → reject a trusted-export file whose pinned signature changed
  → reuse ModuleSyntaxRegistry entries
  → lazily parse only missing versions
  → map each frame line to a method span
  → return immutable StackPage(detail="method")
```

A value request follows this path:

```text
node.children[0:20]
  → validate node fence and safe path
  → reject busy, unknown or stale capture state
  → validate declared shape and public-value policy for the root
  → execute the shape adapter's finite RDBG operation plan into temporary handles
  → privacy-check every projected handle
  → normalize type, preview, size and child keys
  → return immutable ValuePage
```

The issue #5 `to_df()` guard path follows this flow without redesigning table
transfer:

```text
OnecValueProxy.to_df()
  → read capture control-plane state before RuntimeApi availability/privacy work
  → submit one coordinator operation(kind="public_value_guard")
  → RDBG acknowledges: coordinator retains capability and generation pin
  → attached waiter + False: restore workspace, then continue existing transfer
  → detached initiating waiter + late False: restore workspace, publish safe outcome, stop
  → True: CaptureValueAccessDeniedError
  → pending: CaptureBusyError for every later data-plane call; no redispatch
  → uncertain acceptance: CaptureOutcomeUnknownError
```

## Testing strategy

Core tests use synthetic RDBG responses and paired small Designer-format and EDT
source trees. They must prove behavior rather than depend on a live infobase.

Required tests cover:

- `current_capture()` acquisition in paused, evaluating, resuming, unknown,
  recovery and absent states;
- the exact `CaptureStatus` phase/capability matrix;
- safe `evaluation_kind` and timing snapshots containing no source, handle,
  Worker or target data;
- ordered coordinator journal evidence from record creation through outcome
  publication, including coalesced poll progress and waiter-detach reason;
- exact fence checking across operation, generation and `stop_sequence`;
- saved-page display after the capture becomes stale;
- fresh reads for repeated slices and no I/O from page representation;
- two native-frame slices performing two fresh inventories, and a row child
  slice re-projecting its live path instead of reusing saved row fields;
- a BSL change through `КонтекстОтладки`, where a new context page shows the
  new scalar, the old page retains the old scalar, and the native frame retains
  its original scalar until resume writeback;
- basic stack rendering with zero parser calls;
- native-to-visible frame indexing and collapsed runtime markers;
- one batched directory scan for multiple unresolved stack modules;
- equivalent module identity, name, role and source-line resolution for Designer
  roots, EDT project roots and EDT `src` roots, covering a common module and the
  `ПриемНаРаботу` document object module;
- rejection of an ambiguous root containing both direct and nested `src`
  metadata trees;
- positive and generation-scoped negative resolver caching, hot-reload
  invalidation and explicit source refresh;
- `with_methods()` parsing each missing source version once;
- reuse of a syntax index already produced by hot reload;
- source-hash switching only after successful hot reload publication;
- a MAIN operation that remains pinned to generation G1 at a later stop after
  hot reload publishes G2;
- `source_changed` when a trusted-export file changes between the fast page and
  `with_methods()`;
- `source_changed` when its signature changes during the enrichment read;
- partial `with_methods()` output with `method_status="timeout"` and no CAPTURE
  lifecycle transition;
- procedure lookup by line and ordered parameter classification;
- fallback to module-and-line frames after mapping or parse failure;
- protocol fixtures for every advertised shape and bound in the expansion
  matrix;
- safe structure, array, value-table and row expansion;
- rejection of unsupported maps, value trees, undocumented layouts and value
  tables wider than the supported schema bound;
- rejection of unbounded slices, unsafe names and fabricated path segments;
- privacy rejection for direct Worker values, aliases and nested descendants,
  with lifecycle errors taking precedence;
- exact public-value-guard classification for `True`, `False`, BSL failure,
  invalid result, acknowledged pending, uncertain dispatch, restoration failure
  and interrupted waiter;
- evaluation faults before dispatch, during uncertain dispatch, after RDBG
  acknowledgement, after a result and during workspace restoration;
- no generation quarantine, `retain_outcome_unknown()` or RuntimeApi poison
  when an acknowledged evaluation merely outlives or loses its initiating
  waiter;
- a successful BSL result rejected during privacy/normalization, yielding a
  failed outcome while safely returning the capture to `paused`;
- an unexpected RDBG stop during CAPTURE evaluation, yielding
  `recovery_required` without exposing a nested public stack;
- late evaluation completion after caller timeout or `KeyboardInterrupt`;
- repeated `wait()` attaching to the same evaluation rather than dispatching
  another one;
- multiple simultaneous waiters with one RDBG event consumer;
- an attached `to_df()` initiator plus a `capture.wait()` observer, proving that
  observer timeout/interrupt does not abandon transfer and that the observed
  safe internal outcome remains repeatable until its bounded slot is replaced;
- one active record across user BSL, guard, inspection and materialization
  helper kinds, including bounded mandatory cleanup steps;
- transfer of temporary-handle cleanup leases on waiter detachment, with no
  cleanup `evalExpr` until the acknowledged capability settles;
- immutable, idempotent completed/failed/unknown outcomes and a pending outcome
  on wait timeout;
- lock-independent `current_capture()`, `status()` and `wait()` while data-plane
  evaluation and Worker pins remain active;
- atomic transition to `resuming`, caller detachment, successful Continue making
  the old view stale, and partial/uncertain writeback requiring recovery;
- bounded shutdown of a coordinator with an indefinitely pending evaluation;
- indefinitely pending synthetic polling that never exceeds the fixed maximum
  journal progress-event count while `status().evaluation_timing.last_poll_ms`
  continues to advance;
- distinct busy, inspection-timeout, unknown-outcome, recovery and stale
  errors;
- migration or private adaptation of every existing MCP dictionary consumer.

The second observed scenario from issue #5 has a dedicated regression test:

1. `OnecValueProxy.to_df()` reaches a `public_value_guard` coordinator record.
2. The fake RDBG transport acknowledges its `evalExpr` but withholds the result.
3. The original waiter raises `CaptureEvaluationPendingError` on timeout or
   receives `KeyboardInterrupt`.
4. Table schema inspection and transfer counters remain zero.
5. Exactly one pending record, RDBG capability and generation pin remain owned
   by the coordinator; the runtime is not poisoned or quarantined as unknown.
6. From a simulated next notebook cell, `runtime.current_capture()`,
   `capture.status()` and `capture.wait(timeout_s=...)` reach that record without
   the single-writer path. Status reports `phase="evaluating"`, the same
   evaluation ID and `evaluation_kind="public_value_guard"`.
7. A second `to_df()` performs no dispatch and raises `CaptureBusyError`, never
   the Worker-object privacy message.
8. Delivery of the late `False` result is consumed by the coordinator without a
   second dispatch; required workspace restoration completes and the abandoned
   table transfer still does not begin.
9. The retained internal outcome is `completed`, CAPTURE returns to `paused`
   and resume of MAIN is admitted.

Sibling cases replace the late result with `True`, a confirmed BSL error and a
workspace-restoration failure to prove the access-denied, value-check and
recovery-required branches. A separate uncertain-dispatch test proves that
`outcome_unknown` remains reserved for absence of acceptance evidence.

Performance contracts are asserted primarily with work counters:

- parser call count is zero for the basic stack;
- directory scan count is at most one per unresolved batch;
- parse count equals the number of unique missing source versions;
- cached syntax versions require zero subsequent parses;
- each value shape executes only its declared finite operation plan;
- no container property value, table row or table cell outside the requested
  page is read;
- the unavoidable native-frame inventory and schema/name metadata work is
  counted separately; normalized output and downstream work retain byte limits,
  while raw unpaged RDBG response size is measured as a protocol cost and only
  the command-time limit can stop a slow request.

A separate benchmark uses equivalent large synthetic Designer and EDT
configuration trees and a stack of 15–20 distinct modules. The EDT case is run
with both the project root and its `src` directory as configuration inputs. Live
1C qualification remains opt-in and is reported separately from static and
synthetic tests. Every value shape advertised as a live platform capability
receives an opt-in live qualification; synthetic normalizer tests alone do not
establish RDBG behavior.

Jupyter tests render prepared pages and assert that the renderer performs no
runtime calls. Any generated acceptance notebook remains under
`tests/fixtures/notebooks`, has empty outputs and is regenerated by its owning
builder script.

## Acceptance criteria

The first version is complete when this flow works:

```python
capture = runtime.current_capture()

capture.status().phase == "paused"

stack = capture.stack[:20]          # no AST parse
detailed = stack.with_methods()     # only missing syntax versions parsed

locals_page = capture.context.locals[:20]
data = capture.context.locals["СтруктураДанных"]
children = data.children[:20]

if capture.status().can_wait:
    outcome = capture.wait(timeout_s=10)
    outcome.state                     # pending/completed/failed/unknown
    outcome.evaluation_kind           # safe enum
```

The fast stack identifies the stopped configuration module and line without
AST work and pins the source used by later enrichment. Detailed enrichment
shows the method when matching source is available. Context and supported value
requests are finite, live, privacy-checked and read-only. Unsupported shapes
fail explicitly. Snapshot pages render without side effects. A wait timeout or
interruption leaves one queryable evaluation record and never turns into a
misleading Worker-object error. Inspection stays unavailable until the runtime
has proved that evaluation cleanup restored the paused capture.

The issue #5 guard scenario is also an acceptance flow: after an acknowledged
guard loses its original waiter, the next cell can acquire the capture, observe
`phase="evaluating"` and `evaluation_kind="public_value_guard"`, wait for the
same evaluation ID, and resume MAIN after the late result and workspace restore.
No table transfer or second `evalExpr` occurs on the detached path.

## GitHub issue #5 coverage

This implementation covers the lifecycle and diagnostic part of
[GitHub issue #5](https://github.com/pulh1/bsl-jupyter-runtime/issues/5). It does
not claim full resolution or close the issue. Covered criteria are:

- acknowledged pending user `%%bsl` and internal public-value-guard evaluations
  retain coordinator ownership after timeout or interruption;
- the next notebook cell can reach safe status and wait entry points;
- no retry dispatches BSL while the record is pending;
- public-value guard outcomes and lifecycle failures remain distinct;
- late results restore a safe paused CAPTURE or report recovery explicitly;
- bounded phase/timing evidence is journaled;
- focused tests cover both observed scenarios and all error classifications.

The implementation report must list each covered criterion with its test
evidence and leave issue #5 open. It must also list the work that remains in the
issue:

- investigation of the underlying 1C/RDBG stall;
- repeated public-value-guard calls and the wider `to_df()` pipeline;
- in-place recovery when dispatch acceptance truly cannot be established;
- opt-in live 1C qualification of the observed stalls.

The report must state whether any opt-in live qualification was run. Static and
fake-transport tests are not to be described as live qualification.
