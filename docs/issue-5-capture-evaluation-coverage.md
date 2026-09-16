# GitHub issue #5 coverage

Issue [#5](https://github.com/pulh1/bsl-jupyter-runtime/issues/5) remains open.
This implementation covers the CAPTURE evaluation lifecycle and diagnostics
needed to keep an acknowledged RDBG evaluation under one owner after its
original caller stops waiting. It does not claim to eliminate the underlying
1C/RDBG stall.

## Covered lifecycle contract

| Criterion | Implemented contract | Regression evidence |
| --- | --- | --- |
| Coordinator ownership | `CaptureEvaluationCoordinator` is the sole owner of an acknowledged CAPTURE evaluation, including user BSL and internal inspection/materialization requests. A private controller-owned coordinator applies the same rule to ready-state completion inspection without creating a public capture. The evaluation-generation pin and RDBG event stream remain with their owner until terminal settlement. | `test_runtime_api_capture_path_transfers_real_task3_handoff_and_pin_ownership`, `test_controller_owned_internal_evaluations_use_explicit_kind_and_same_owner`, `test_session_proxy_adopts_ticket_before_releasing_admission_locks`, `test_ready_completion_stop_resumes_and_drains_the_real_rdbg_capability`, `test_ready_completion_timeout_retains_a_real_rdbg_owner_until_late_result` |
| Safe status and wait | `runtime.current_capture()`, `capture.status()` and `capture.wait(timeout_s=...)` remain reachable while the initiating waiter is detached and without taking the blocked Runtime API writer or Worker guard. Status exposes only a bounded evaluation ID, enum kind, phase and bounded timing evidence. | `test_capture_control_plane_bypasses_api_writer_availability_and_worker_guard`, `test_capture_control_plane_does_not_acquire_runtime_api_lock`, `test_interrupted_initiator_leaves_control_plane_and_pending_wait_reachable`, `test_pending_and_busy_errors_expose_only_safe_evaluation_facts` |
| Timeout and interrupt detachment | A timeout or `KeyboardInterrupt` detaches only that waiter after RDBG acceptance. It does not release the coordinator-owned pin, poison the Runtime API or start table transfer. Deferred temporary-table inspection publishes no server projection while its waiter is detached. | `test_acknowledged_timeout_keeps_one_pending_capability_and_rejects_redispatch`, `test_keyboard_interrupt_detaches_only_waiter_and_late_result_still_settles`, `test_acknowledged_pending_keeps_generation_pin_without_poison`, `test_interrupted_table_materialization_detaches_the_capture_waiter`, `test_detached_projected_table_has_no_server_projection_to_clean`, `test_projected_table_delivery_timeout_leaves_no_target_or_local_handle` |
| Late result | The coordinator accepts the original late result, restores the debug workspace and publishes one retained terminal outcome. A late result does not require the Runtime API writer. | `test_late_result_has_one_owner_after_repeated_interval_timeouts`, `test_late_success_restores_exact_workspace_and_completed_output_without_redispatch`, `test_late_completion_does_not_need_runtime_api_lock`, `test_recursive_materialization_detaches_busy_waiter_and_late_payload` |
| No redispatch | A second caller sees the existing pending capability or a busy/lifecycle result. `capture.wait()` polls that capability and never submits another `evalExpr`. | `test_second_submission_is_rejected_before_dispatch`, `test_acknowledged_timeout_keeps_one_pending_capability_and_rejects_redispatch`, `test_capture_wait_timeout_and_repeated_observation_never_redispatch`, `test_resume_timeout_detaches_waiter_without_redispatching_continue` |
| Value-admission errors | A denied value produces the privacy error only after an affirmative denial result. BSL failure, acknowledged pending work, uncertain dispatch and workspace-restoration failure remain distinct typed outcomes. None is rewritten as `Worker generation objects are not public values`. Payload transfer starts only after successful admission. Deferred table descriptors are revalidated and materialized through an internal bounded route; the public table path remains expression-free. | `test_table_admission_rejects_before_private_payload_transfer`, `test_table_bsl_failure_is_a_value_check_before_payload_read`, `test_table_dispatch_uncertainty_is_not_a_worker_error`, `test_table_cleanup_uncertainty_requires_recovery`, `test_workspace_restoration_failure_requires_recovery_after_confirmed_result`, `test_api_materializes_deferred_capture_table_through_trusted_internal_route`, `test_api_materializes_deferred_capture_table_payload_through_trusted_internal_route`, `test_session_to_df_materializes_deferred_capture_table_handle` |
| Controller-owned resume | Capture resume, including writeback, cleanup and `Continue`, is one controller-owned operation. An interrupted waiter leaves it running; new MAIN admission remains closed until its terminal outcome. | `test_resume_admission_is_controller_owned_before_root_export_and_preserves_next_stop`, `test_keyboard_interrupt_detaches_resume_waiter_and_controller_finishes_once`, `test_detached_resume_preserves_real_worker_pin_and_context_until_terminal`, `test_resume_timeout_detaches_waiter_without_redispatching_continue` |

The lifecycle tests use deterministic fake transports and real in-process
coordinator/session/runtime objects. They verify ownership, locking, dispatch
boundaries and error classification; they do not reproduce the platform stall
against a production infobase.

## Scope still open in issue #5

The following work remains in issue #5:

- determine why the original `evalExpr`/RDBG request can stall inside the 1C
  platform;
- investigate repeated admission checks and performance across the wider
  `to_df()` and materialization pipeline;
- add in-place recovery for a true uncertain-dispatch outcome where RDBG
  acceptance cannot be established;
- qualify the observed timeout and interrupt scenarios against live 1C/RDBG.

No live qualification of the issue #5 RDBG-stall scenarios was run. Unit and
fake-transport results are not presented as live evidence.

The repository also contains a separate opt-in typed CAPTURE inspection gate
for a disposable empty infobase. It was not run as part of this implementation
and therefore supplies no live evidence for issue #5.

A separate opt-in live 1C test was run during the protocol-2 serializer work
against a disposable empty infobase. It verified bounded reads for a
10,000-row `ТаблицаЗначений` and a real `РезультатЗапроса`, including a
controlled sensitivity mutation. That evidence qualifies serializer
boundedness only; it does not exercise the stalled public-value admission,
`to_df()` interruption, late-result or recovery scenarios from issue #5.
