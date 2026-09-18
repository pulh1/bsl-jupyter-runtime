# MAIN command lifetime

Entry points: `operation.py` for command lifetime and `executor.py` for the
ordered command protocol. The approved design is
[`main-capture-execution-design.md`](../../../../docs/superpowers/specs/2026-09-17-main-capture-execution-design.md).

- `MainOperation` owns the lifecycle of one command, including every CAPTURE stop and continuation. Returning a notebook request does not complete the command.
- An ambiguous Continue is `unknown`; only acknowledged Continue is `running`. Completion requires the controller's command-ID validation.
- A lost waiter, frame, timeout, or recovery checkpoint does not prove target loss. Call `mark_lost` only with confirmed target loss.
- A nonterminal MAIN operation blocks admission of a replacement command regardless of the last cell reply.
- Local failure before command-field writes, or after confirmed writes but before Continue enters transport, becomes terminal `failed_before_dispatch`; an attempted command-field write with unresolved outcome remains `unknown`. Writes alone do not prove MAIN launch.
- A matching remote completion ID closes MAIN before result/message decoding; presentation errors do not reopen the command or block the next admission.
- `MainExecutor` writes the command fields, issues Continue, and waits across bounded RDBG poll intervals without an execution deadline. Production supplies the admitted ticket's `SessionPort` for each operation; the optional constructor port supports standalone component tests. The controller routes the returned stop.
- Trusted Worker publication helpers on a free MAIN service stop use `build_main_instruction_call()` and one direct `evalExpr` per bounded stage batch, prepare, or swap mutation. They do not write MAIN command fields, issue `Continue`, or consume a command ID; user MAIN cells still use `MainExecutor`. Keep the ticket, any exact pending eval, and breakpoint workspace owned after an unknown helper outcome.
- `idle_materialization.py` runs direct and dynamic Context value/table transfer plus bounded `head_to_df` while MAIN is at a confirmed service stop. Its admission, payload read and private-key deletion use one arbiter ticket and the same SessionPort. Admission requires an idle arbiter; inside its own ticket it rechecks the exact route/target and frozen Worker catalog without requiring an empty queue. Keep confirmed private-key deletion debts retryable without repeating the parent transfer.
- `completion.py` reads `ЗавершеннаяКоманда`, `Результат`, `Ошибка` and optional messages through the exact pending evaluation capability. Matching command ID marks `MainOperation` remotely completed before decoding result or messages. The default public path uses this reader through `ExecutionController`.
- This package does not import concrete controllers or `RdbgSession`. Protocol value models may be shared.

The public `RuntimeSession` runs MAIN through the arbiter worker and retains one operation across CAPTURE stops.

Focused checks: `tests/unit/test_main_operation.py`, `test_main_executor.py`, `test_main_completion_reader.py`, `test_execution_controller_routes.py` and `test_capture_stop_recovery_component.py`.
