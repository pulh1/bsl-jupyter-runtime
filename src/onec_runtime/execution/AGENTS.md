# Execution ownership

Design authority: [MAIN/CAPTURE execution spec](../../../docs/superpowers/specs/2026-09-17-main-capture-execution-design.md), especially sections 3, 4, 6, 7 and 10.

Public entry points remain `../runtime_api.py`, `../prototype_runtime.py` and `../capture_evaluation.py`. The new `controller/controller.py` composes MAIN → CAPTURE → MAIN on one `RdbgArbiter` in component tests, including cell evaluation, variable reads, materialization, writeback and resume. It is not yet selected by `RuntimeSession` or the public `RuntimeApi`. `pipeline.py`, route policies and isolated snapshot binding also have tested contracts but are not yet the public BSL path. Keep this distinction explicit in reports and tests.

```text
RuntimeSession -> RuntimeApi -> CellExecutionPipeline -> controller port
                                      |                    |
                                policy/service ports   route bindings
                                                       /          \
                                                 MAIN executor  CAPTURE executor
                                                       \          /
                                                        RdbgArbiter
                                                            |
                                                        RdbgSession
```

- The generic BSL pipeline parses once, obtains a stable `PreparationContext`, prepares through its policy, submits and waits. It must not choose a route using `OperationState`, `LoweringMode`, concrete policy types or private-method `getattr`. Only the controller chooses the route; `RuntimeSession` supplies its bindings.
- `PreparationContext` carries an opaque route token, a fresh preparation nonce and policy. `PreparedCell` carries mapped source, namespace delta, Worker intent and snapshot versions. Preparation uses isolated mutable working state from immutable namespace/Worker snapshots; it performs no target mutation. Admission consumes the nonce and validates route/fence and owner-supplied version guards, with revalidation before the first side effect.
- One arbiter must own runtime RDBG commands, pending tickets and event reading after bootstrap. The component path does this now; the public path still has direct session calls and a separate `CaptureEvaluationCoordinator` worker. Never turn on only part of the new path while those owners coexist. Executors use the arbiter port; RDBG retains protocol correlation, not MAIN/CAPTURE policy.
- A cell ticket can settle while its parent MAIN command and stopped CAPTURE scope remain live. Waiter detachment is not remote cancellation. Preserve dispatch evidence and leases through unknown outcomes; do not retry an ambiguous remote command or infer target loss from a wait interval.
- Namespace/Worker services own versions and publication. Policies own lowering and outcome settlement; executors own remote sequencing and mandatory cleanup. Settlement must survive waiter detachment, preserve source maps and avoid committing namespace changes for rejected preparation.
- A Worker activation receives the admitted ticket's `SessionPort`; its target mutation runner must never recurse into the legacy `RuntimeApi`. The active method-set object is part of the next preparation snapshot. The current adapter fails closed when Worker breakpoints require a workspace transaction that has not been wired yet. Every command using an existing Worker generation still needs its own pin; method publication alone does not cover it.
- A confirmed cell or MAIN reply settles before Worker pin release. Register release as a dependent arbiter cleanup ticket on the resulting route. A confirmed cleanup error keeps a retryable debt and holds later dispatch; an ambiguous cleanup keeps its own remote capability. Neither changes the confirmed parent reply.
- Operation failure, frame identity and resource debts are separate facts. Status remains available while execution is pending; locks protecting admission must not be held while waiting for remote results.

Legacy mode branches and a shared mutable lowerer are migration debt, not the contract for new generic code. Adding a mode wrapper alone does not meet the spec's acceptance criterion: the generic path must accept a third fake policy/executor binding without changes. See the component guidance in `main/`, `capture/` and `controller/` before moving responsibility.

Focused checks: `tests/unit/test_execution_controller_routes.py`, `test_capture_stop_recovery_component.py`, `test_rdbg_arbiter.py`, `test_cell_execution_pipeline.py`, `test_route_cell_policies.py`, `test_runtime_rdbg_single_owner.py`, plus legacy `test_runtime_api.py` and `test_prototype_runtime.py`. `test_runtime_rdbg_single_owner.py` is the public cutover gate and remains RED until the old owners retire. Static tests do not qualify live 1C behavior.
