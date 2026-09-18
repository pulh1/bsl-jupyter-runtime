# Execution ownership

Design authority: [MAIN/CAPTURE execution spec](../../../docs/superpowers/specs/2026-09-17-main-capture-execution-design.md), especially sections 3, 4, 6, 7 and 10.

Public `RuntimeSession.start()` transfers the verified stopped server target to `post_bootstrap.py`, which builds one `RdbgArbiter`, controller and pipeline. `public_facade.py` handles MAIN/CAPTURE cells, resume, status, namespace, heartbeat, Worker operations, bounded value routes and stopped CAPTURE inspection. `value_materialization_router.py` selects the exact stopped route for direct and dynamic transfers. Legacy modules remain for migration and standalone regression tests; never give them a second RDBG reader or writer in a running session. Static bootstrap and component tests are not live 1C qualification.

`post_bootstrap.py` composes an execution owner from a verified stopped target. Fresh startup creates namespace, Worker catalog, settlement, reply, source identity and breakpoint owners there. `reply_presenter.py` maps pre-dispatch diagnostics to the existing public reply type without raw exception text. The MAIN idle value route first admits with no pending RDBG activity, then rechecks its exact target inside its own ticket; do not require an empty arbiter queue at that second check.

```text
RuntimeSession -> PublicExecutionFacade -> CellExecutionPipeline
                      |                           |
                  API services              ExecutionController
                                               /          \
                                         MAIN executor  CAPTURE executor
                                               \          /
                                                RdbgArbiter
                                                     |
                                                RdbgSession
```

- The generic BSL pipeline parses once, obtains a stable `PreparationContext`, prepares through its policy, submits and waits. It must not choose a route using `OperationState`, `LoweringMode`, concrete policy types or private-method `getattr`. Only the controller chooses the route; `RuntimeSession` supplies its bindings.
- `PreparationContext` carries an opaque route token, a fresh preparation nonce and policy. `PreparedCell` carries mapped source, namespace delta, Worker intent and snapshot versions. Preparation uses isolated mutable working state from immutable namespace/Worker snapshots; it performs no target mutation. Admission consumes the nonce and validates route/fence and owner-supplied version guards, with revalidation before the first side effect.
- One arbiter owns runtime RDBG commands, pending tickets and event reading after bootstrap on the default public path. Legacy `RuntimeApi` and `CaptureEvaluationCoordinator` remain only for isolated regression and migration work; do not compose them beside `PublicExecutionFacade` in one session. Executors use the arbiter port; RDBG retains protocol correlation, not MAIN/CAPTURE policy.
- A cell ticket can settle while its parent MAIN command and stopped CAPTURE scope remain live. Waiter detachment is not remote cancellation. Preserve dispatch evidence and leases through unknown outcomes; do not retry an ambiguous remote command or infer target loss from a wait interval.
- Namespace/Worker services own versions and publication. Policies own lowering and outcome settlement; executors own remote sequencing and mandatory cleanup. Settlement must survive waiter detachment, preserve source maps and avoid committing namespace changes for rejected preparation.
- A Worker activation receives the admitted ticket's `SessionPort`; its target mutation runner must never recurse into the legacy `RuntimeApi`. `worker_mutation.py` uses the current controller route and one shared breakpoint workspace; `composition.py` binds the activation adapter after bootstrap. The active method-set object is part of the next preparation snapshot. Every command using an existing Worker generation still needs its own pin; method publication alone does not cover it.
- `breakpoint_routes.py` and `worker_breakpoint_workspace.py` use one `BreakpointWorkspaceController`. CAPTURE successor locations are installed in the same resume ticket before writeback and Continue; publish the local registry only after confirmed installation. Unknown installation retains arbiter ownership and the previous local route.
- A confirmed cell or MAIN reply settles before Worker pin release. Register release as a dependent arbiter cleanup ticket on the resulting route. A confirmed cleanup error keeps a retryable debt and holds later dispatch; an ambiguous cleanup keeps its own remote capability. Neither changes the confirmed parent reply.
- Operation failure, frame identity and resource debts are separate facts. Status remains available while execution is pending; locks protecting admission must not be held while waiting for remote results.

Legacy mode branches and a shared mutable lowerer are migration debt, not the contract for new generic code. Adding a mode wrapper alone does not meet the spec's acceptance criterion: the generic path must accept a third fake policy/executor binding without changes. See the component guidance in `main/`, `capture/` and `controller/` before moving responsibility.

Focused checks: `tests/unit/test_execution_controller_routes.py`, `test_capture_stop_recovery_component.py`, `test_rdbg_arbiter.py`, `test_cell_execution_pipeline.py`, `test_route_cell_policies.py`, `test_runtime_rdbg_single_owner.py`, `test_session_default_execution_owner.py`, plus legacy `test_runtime_api.py` and `test_prototype_runtime.py`. Static tests do not qualify live 1C behavior; final acceptance must run the notebook against a temporary infobase.
