# MAIN command lifetime

Entry points: `operation.py` for command lifetime and `executor.py` for the
ordered command protocol. The approved design is
[`main-capture-execution-design.md`](../../../../docs/superpowers/specs/2026-09-17-main-capture-execution-design.md).

- `MainOperation` owns the lifecycle of one command, including every CAPTURE stop and continuation. Returning a notebook request does not complete the command.
- An ambiguous Continue is `unknown`; only acknowledged Continue is `running`. Completion requires the controller's command-ID validation.
- A lost waiter, frame, timeout, or recovery checkpoint does not prove target loss. Call `mark_lost` only with confirmed target loss.
- A nonterminal MAIN operation blocks admission of a replacement command, even when the legacy controller state reports FAILED.
- Local failure before command-field writes, or after confirmed writes but before Continue enters transport, becomes terminal `failed_before_dispatch`; an attempted command-field write with unresolved outcome remains `unknown`. Writes alone do not prove MAIN launch.
- A matching remote completion ID closes MAIN before result/message decoding; presentation errors do not reopen the command or block the next admission.
- `MainExecutor` writes the command fields, issues Continue, and waits across bounded RDBG poll intervals without an execution deadline. Its methods accept the port of the current operation; a default port exists only for the transitional controller. The controller still routes the returned stop.
- This package does not import concrete controllers or `RdbgSession`. Protocol value models may be shared.

The transitional controller still exposes `OperationHandle` and `OperationState` for existing clients. They are compatibility state, not the MAIN lifecycle authority. The controller still invokes the executor on its caller thread; arbiter ownership, typed completion contracts, status API integration and confirmed target termination remain separate migration steps.

Focused checks: `tests/unit/test_main_operation.py`, `tests/unit/test_main_executor.py` and `tests/unit/test_prototype_runtime.py`.
