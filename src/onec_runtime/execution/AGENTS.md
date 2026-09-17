# Execution ownership

Design authority: [MAIN/CAPTURE execution spec](../../../docs/superpowers/specs/2026-09-17-main-capture-execution-design.md), especially sections 3, 4, 6, 7 and 10.

Current entry points are `../runtime_api.py`, `../prototype_runtime.py` and `../capture_evaluation.py`. Extracted pieces already in use are `main/operation.py`, `main/executor.py`, `capture/scope.py`, `capture/executor.py`, the common parser and the MAIN/CAPTURE statement preparers. `arbiter.py` and `pipeline.py` have tested contracts but are not yet wired into the runtime. The controller route context and single-owner RDBG migration described below remain target architecture.

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
- One arbiter owns runtime RDBG commands, pending tickets and event reading after bootstrap. Migration must not introduce a second reader alongside existing controller/coordinator paths. Executors use its port; they do not import concrete `RdbgSession` or controllers. RDBG retains protocol correlation, not MAIN/CAPTURE policy.
- A cell ticket can settle while its parent MAIN command and stopped CAPTURE scope remain live. Waiter detachment is not remote cancellation. Preserve dispatch evidence and leases through unknown outcomes; do not retry an ambiguous remote command or infer target loss from a wait interval.
- Namespace/Worker services own versions and publication. Policies own lowering and outcome settlement; executors own remote sequencing and mandatory cleanup. Settlement must survive waiter detachment, preserve source maps and avoid committing namespace changes for rejected preparation.
- Operation failure, frame identity and resource debts are separate facts. Status remains available while execution is pending; locks protecting admission must not be held while waiting for remote results.

Legacy mode branches and a shared mutable lowerer are migration debt, not the contract for new generic code. Adding a mode wrapper alone does not meet the spec's acceptance criterion: the generic path must accept a third fake policy/executor binding without changes. See the component guidance in `main/`, `capture/` and `controller/` before moving responsibility.

Focused checks: `tests/unit/test_runtime_api.py`, `test_main_operation.py`, `test_prototype_runtime.py`, `test_capture_evaluation_lifecycle.py` and `test_capture_control_plane.py`. Add contract coverage as the ports land for stale route/nonce/snapshot rejection, Worker failure after admission, isolated preparation and one event reader. These static tests do not qualify live 1C behavior.
