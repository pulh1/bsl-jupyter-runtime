from __future__ import annotations

import json
from math import nan
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock, Thread

import pytest

import onec_runtime_mcp.agent.contracts as contracts
from onec_runtime_mcp.agent.contracts import AgentOperationState, RetrySafety
from onec_runtime_mcp.agent.operations import BackendExecution, OperationRegistry


def command(cell_id: str, *, revision: int = 1, generation: int = 1) -> dict[str, object]:
    return {
        "runtime_id": "runtime-1",
        "runtime_generation": generation,
        "code_id": cell_id,
        "revision": revision,
        "source_sha256": "source-sha-1",
        "inputs_sha256": "inputs-sha-1",
    }


def completed(*messages: str) -> BackendExecution:
    return BackendExecution.completed(messages=messages, result_present=True, runtime_state="ready")


def journal_path(root: Path) -> Path:
    return root / ".runtime" / "agent-service" / "operations.jsonl"


def read_events(root: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in journal_path(root).read_text(encoding="utf-8").splitlines()]


def write_operation_events(root: Path, *, completed: str, running: str) -> None:
    path = journal_path(root)
    path.parent.mkdir(parents=True)
    rows = [
        {
            "cursor": 1,
            "event": "submitted",
            "operation_id": completed,
            "runtime_id": "runtime-1",
            "runtime_generation": 1,
            "code_id": "cell-1",
            "revision": 1,
            "source_sha256": "source-1",
            "inputs_sha256": "inputs-1",
        },
        {"cursor": 2, "event": "completed", "operation_id": completed, "result_present": True},
        {
            "cursor": 3,
            "event": "submitted",
            "operation_id": running,
            "runtime_id": "runtime-1",
            "runtime_generation": 1,
            "code_id": "cell-2",
            "revision": 1,
            "source_sha256": "source-2",
            "inputs_sha256": "inputs-2",
        },
        {"cursor": 4, "event": "started", "operation_id": running},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_timeout_returns_same_running_operation_and_never_resubmits(tmp_path: Path) -> None:
    gate = Event()
    started = Event()
    calls = 0

    def execute() -> BackendExecution:
        nonlocal calls
        calls += 1
        started.set()
        gate.wait(2)
        return completed("готово")

    registry = OperationRegistry(tmp_path)
    submitted = registry.submit(command("cell-main", revision=3), execute)
    assert started.wait(1)
    timed_out = registry.wait(submitted.operation_id, timeout_s=0.01)
    same = registry.submit(command("cell-main", revision=3), execute)
    assert timed_out.state is AgentOperationState.RUNNING
    assert same.operation_id == submitted.operation_id
    assert calls == 1
    gate.set()


def test_shutdown_closes_the_owned_executor_once(tmp_path: Path) -> None:
    calls: list[bool] = []

    class Executor:
        def submit(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("submission is not expected")

        def shutdown(self, *, wait: bool) -> None:
            calls.append(wait)

    registry = OperationRegistry(tmp_path, executor_factory=lambda: Executor())  # type: ignore[arg-type]
    registry._executor_locked()
    registry.shutdown()
    registry.shutdown()
    assert calls == [False]


def test_restart_rehydrates_terminal_and_marks_running_unknown(tmp_path: Path) -> None:
    write_operation_events(tmp_path, completed="op-1", running="op-2")
    registry = OperationRegistry(tmp_path)
    assert registry.status("op-1").state is AgentOperationState.COMPLETED
    interrupted = registry.status("op-2")
    assert interrupted.state is AgentOperationState.UNKNOWN
    assert interrupted.safe_to_retry is RetrySafety.AFTER_STATUS_CHECK
    assert read_events(tmp_path)[-1]["reason"] == "service_restart_during_execution"


def test_messages_have_monotonic_cursors_and_are_bounded_by_pagination(tmp_path: Path) -> None:
    registry = OperationRegistry(tmp_path)
    operation = registry.submit(command("cell-main"), lambda: completed("one", "two", "three"))
    terminal = registry.wait(operation.operation_id, timeout_s=1)
    page = registry.output(terminal.operation_id, after_cursor=0, limits={"messages": 2})
    rest = registry.output(terminal.operation_id, after_cursor=page.next_cursor, limits={"messages": 2})

    cursors = [int(row["cursor"]) for row in read_events(tmp_path)]
    assert cursors == sorted(cursors)
    assert len(set(cursors)) == len(cursors)
    assert page.messages == ("one", "two")
    assert page.has_more is True
    assert rest.messages == ("three",)
    assert rest.has_more is False


def test_journal_never_contains_raw_backend_result_or_exception_text(tmp_path: Path) -> None:
    class SecretBackendError(Exception):
        def __str__(self) -> str:
            return "Пароль=super-secret"

    registry = OperationRegistry(tmp_path)
    failed = registry.submit(command("cell-failure"), lambda: (_ for _ in ()).throw(SecretBackendError()))
    terminal = registry.wait(failed.operation_id, timeout_s=1)

    payload = journal_path(tmp_path).read_text(encoding="utf-8")
    assert terminal.state is AgentOperationState.FAILED
    assert "super-secret" not in payload
    assert "SecretBackendError" not in payload


def test_backend_uncertainty_becomes_unknown_and_result_is_descriptor_only(tmp_path: Path) -> None:
    registry = OperationRegistry(tmp_path)
    submitted = registry.submit(
        command("cell-unknown"),
        lambda: BackendExecution(
            terminal_state=AgentOperationState.UNKNOWN,
            messages=("connection lost",),
            result_present=False,
            runtime_state="unknown",
        ),
    )
    terminal = registry.wait(submitted.operation_id, timeout_s=1)
    result = registry.result(submitted.operation_id)

    assert terminal.state is AgentOperationState.UNKNOWN
    assert terminal.safe_to_retry is RetrySafety.AFTER_STATUS_CHECK
    assert result == terminal


def test_second_mutation_waits_behind_the_single_execution_lane(tmp_path: Path) -> None:
    first_gate = Event()
    first_started = Event()
    second_started = Event()
    registry = OperationRegistry(tmp_path)

    first = registry.submit(
        command("cell-first"),
        lambda: (first_started.set(), first_gate.wait(2), completed("first"))[2],
    )
    assert first_started.wait(1)
    second = registry.submit(command("cell-second"), lambda: (second_started.set(), completed("second"))[1])

    assert registry.status(second.operation_id).state is AgentOperationState.QUEUED
    assert not second_started.is_set()
    first_gate.set()
    assert second_started.wait(1)
    assert registry.wait(first.operation_id, timeout_s=1).state is AgentOperationState.COMPLETED
    assert registry.wait(second.operation_id, timeout_s=1).state is AgentOperationState.COMPLETED


def test_stop_waiting_releases_the_targeted_waiter_without_stopping_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = Event()
    started = Event()
    waiter_registered = Event()
    waiter_finished = Event()
    observed: list[object] = []
    registry = OperationRegistry(tmp_path)
    submitted = registry.submit(
        command("cell-main"),
        lambda: (started.set(), gate.wait(2), completed("later"))[2],
    )
    assert started.wait(1)
    original_register = registry._register_waiter_locked

    def registering_waiter(operation_id: str, waiter_id: str) -> None:
        original_register(operation_id, waiter_id)
        waiter_registered.set()

    monkeypatch.setattr(registry, "_register_waiter_locked", registering_waiter)

    def wait_for_operation() -> None:
        try:
            observed.append(
                registry.wait(
                    submitted.operation_id,
                    timeout_s=1,
                    waiter_id="waiter-1",
                )
            )
        finally:
            waiter_finished.set()

    waiter = Thread(target=wait_for_operation)
    waiter.start()
    assert waiter_registered.wait(1)

    descriptor = registry.stop_waiting(submitted.operation_id, "waiter-1")
    assert waiter_finished.wait(1)
    waiter.join()
    assert descriptor.state is AgentOperationState.RUNNING
    assert observed[0].state is AgentOperationState.RUNNING
    assert registry.status(submitted.operation_id).state is AgentOperationState.RUNNING
    gate.set()
    assert registry.wait(submitted.operation_id, timeout_s=1).state is AgentOperationState.COMPLETED


def test_transition_is_flushed_before_waiters_can_observe_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry = OperationRegistry(tmp_path)
    published: list[dict[str, object]] = []
    original_publish = registry._publish_transition

    def checking_publish(payload: dict[str, object]) -> None:
        assert read_events(tmp_path)[-1] == payload
        published.append(payload.copy())
        original_publish(payload)

    monkeypatch.setattr(registry, "_publish_transition", checking_publish)
    submitted = registry.submit(command("cell-main"), lambda: completed("done"))
    assert registry.wait(submitted.operation_id, timeout_s=1).state is AgentOperationState.COMPLETED
    assert [payload["event"] for payload in published] == ["started", "message", "completed"]


@pytest.mark.parametrize("timeout_s", (-1, nan, 61))
def test_wait_rejects_invalid_timeouts_without_touching_executor(tmp_path: Path, timeout_s: float) -> None:
    def executor_factory() -> ThreadPoolExecutor:
        pytest.fail("invalid wait must not create an executor")

    registry = OperationRegistry(tmp_path, executor_factory=executor_factory)
    with pytest.raises(ValueError):
        registry.wait("missing-operation", timeout_s=timeout_s)


def test_wait_after_the_exposed_cursor_blocks_until_a_later_transition(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gate = Event()
    started = Event()
    waiter_registered = Event()
    waiter_finished = Event()
    observed: list[object] = []
    registry = OperationRegistry(tmp_path)
    submitted = registry.submit(
        command("cell-main"),
        lambda: (started.set(), gate.wait(2), completed("done"))[2],
    )
    assert started.wait(1)
    before = registry.status(submitted.operation_id)
    original_register = registry._register_waiter_locked

    def registering_waiter(operation_id: str, waiter_id: str) -> None:
        original_register(operation_id, waiter_id)
        waiter_registered.set()

    monkeypatch.setattr(registry, "_register_waiter_locked", registering_waiter)

    def wait_for_later_transition() -> None:
        try:
            observed.append(
                registry.wait(
                    submitted.operation_id,
                    timeout_s=1,
                    after_cursor=before.event_cursor,
                    waiter_id="cursor-waiter",
                )
            )
        finally:
            waiter_finished.set()

    waiter = Thread(target=wait_for_later_transition)
    waiter.start()
    assert waiter_registered.wait(1)
    assert not waiter_finished.is_set()
    gate.set()
    assert waiter_finished.wait(1)
    waiter.join()
    assert observed[0].event_cursor > before.event_cursor


def test_wait_after_the_exposed_cursor_returns_current_state_only_after_timeout(tmp_path: Path) -> None:
    gate = Event()
    started = Event()
    registry = OperationRegistry(tmp_path)
    submitted = registry.submit(
        command("cell-main"),
        lambda: (started.set(), gate.wait(2), completed())[2],
    )
    assert started.wait(1)
    before = registry.status(submitted.operation_id)

    timed_out = registry.wait(submitted.operation_id, timeout_s=0.01, after_cursor=before.event_cursor)
    assert timed_out.state is AgentOperationState.RUNNING
    assert timed_out.event_cursor == before.event_cursor
    gate.set()


def test_wide_injected_executor_never_runs_two_backend_calls_at_once(tmp_path: Path) -> None:
    release = Event()
    first_started = Event()
    second_started = Event()
    counter_lock = Lock()
    active = 0
    maximum_active = 0
    executor = ThreadPoolExecutor(max_workers=4)
    registry = OperationRegistry(tmp_path, executor_factory=lambda: executor)

    def execute(started: Event) -> BackendExecution:
        nonlocal active, maximum_active
        with counter_lock:
            active += 1
            maximum_active = max(maximum_active, active)
        started.set()
        release.wait(2)
        with counter_lock:
            active -= 1
        return completed()

    first = registry.submit(command("cell-first"), lambda: execute(first_started))
    assert first_started.wait(1)
    second = registry.submit(command("cell-second"), lambda: execute(second_started))
    assert not second_started.wait(0.05)
    release.set()
    assert second_started.wait(1)
    assert registry.wait(first.operation_id, timeout_s=1).state is AgentOperationState.COMPLETED
    assert registry.wait(second.operation_id, timeout_s=1).state is AgentOperationState.COMPLETED
    assert maximum_active == 1
    executor.shutdown(wait=True)


def test_abort_generation_persists_unknown_without_cancelling_other_generation(tmp_path: Path) -> None:
    gate = Event()
    started = Event()
    registry = OperationRegistry(tmp_path)
    aborted = registry.submit(
        command("cell-main", generation=1),
        lambda: (started.set(), gate.wait(2), completed())[2],
    )
    other = registry.submit(command("cell-next", generation=2), lambda: completed())
    assert started.wait(1)

    changed = registry.mark_generation_aborted("runtime-1", 1)
    assert [item.operation_id for item in changed] == [aborted.operation_id]
    assert registry.status(aborted.operation_id).state is AgentOperationState.UNKNOWN
    assert registry.status(other.operation_id).state is AgentOperationState.QUEUED
    gate.set()
    assert registry.wait(other.operation_id, timeout_s=1).state is AgentOperationState.COMPLETED


def test_execution_provenance_is_recorded_once_and_rehydrated_immutably(
    tmp_path: Path,
) -> None:
    """Break caught: restart loses provenance or permits a second artifact identity."""
    release = Event()
    holder: list[str] = []
    registry = OperationRegistry(tmp_path)
    provenance = contracts.OperationExecutionProvenance(
        visible_source_sha256="a" * 64,
        executed_source_sha256="b" * 64,
        source_map_sha256="c" * 64,
        mode="main",
    )

    def execute() -> BackendExecution:
        assert release.wait(1)
        registry.set_execution_provenance(holder[0], provenance)
        return completed()

    submitted = registry.submit(
        {**command("cell-provenance"), "source_sha256": "a" * 64},
        execute,
    )
    holder.append(submitted.operation_id)
    release.set()
    assert registry.wait(submitted.operation_id, timeout_s=1).state is AgentOperationState.COMPLETED
    first_events = read_events(tmp_path)

    same = registry.set_execution_provenance(submitted.operation_id, provenance)
    assert same.execution_provenance == provenance
    assert read_events(tmp_path) == first_events
    registry.shutdown()

    recovered = OperationRegistry(tmp_path)
    assert recovered.view_snapshot(submitted.operation_id).execution_provenance == provenance
    assert recovered.set_execution_provenance(
        submitted.operation_id, provenance
    ).execution_provenance == provenance
    with pytest.raises(ValueError, match="immutable"):
        recovered.set_execution_provenance(
            submitted.operation_id,
            contracts.OperationExecutionProvenance(
                visible_source_sha256="a" * 64,
                executed_source_sha256="d" * 64,
                source_map_sha256="c" * 64,
                mode="main",
            ),
        )
    assert sum(
        event["event"] == "execution_provenance" for event in read_events(tmp_path)
    ) == 1
    recovered.shutdown()


@pytest.mark.parametrize(
    "terminal_event",
    ["completed", "captured", "failed", "unknown"],
)
def test_recovery_ignores_first_provenance_event_after_terminal_transition(
    tmp_path: Path,
    terminal_event: str,
) -> None:
    """Break caught: restart installs provenance that live code rejects as late."""
    operation_id = f"op-late-{terminal_event}"
    rows = [
        {
            "cursor": 1,
            "event": "submitted",
            "operation_id": operation_id,
            "runtime_id": "runtime-1",
            "runtime_generation": 1,
            "code_id": "cell-late",
            "revision": 1,
            "source_sha256": "a" * 64,
            "inputs_sha256": "inputs-sha-1",
        },
        {"cursor": 2, "event": "started", "operation_id": operation_id},
        {
            "cursor": 3,
            "event": terminal_event,
            "operation_id": operation_id,
            "result_present": terminal_event == "completed",
        },
        {
            "cursor": 4,
            "event": "execution_provenance",
            "operation_id": operation_id,
            "provenance": {
                "visible_source_sha256": "a" * 64,
                "executed_source_sha256": "b" * 64,
                "source_map_sha256": "c" * 64,
                "mode": "main",
                "worker_generation": None,
                "worker_manifest_sha256": None,
            },
        },
    ]
    path = journal_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        "\n".join(json.dumps(row, separators=(",", ":")) for row in rows)
        + "\n",
        encoding="utf-8",
    )

    recovered = OperationRegistry(tmp_path)
    snapshot = recovered.view_snapshot(operation_id)

    assert snapshot.operation.state is AgentOperationState(terminal_event)
    assert snapshot.operation.event_cursor == 3
    assert snapshot.execution_provenance is None
    recovered.shutdown()
