from __future__ import annotations

from dataclasses import dataclass
import json
from multiprocessing.connection import Connection
from pathlib import Path
from threading import Event
from time import monotonic, sleep

import pytest

import onec_runtime.supervised_processes as supervised_processes_module
from onec_runtime.artifacts import ExistingArtifactSink
from onec_runtime.config import RuntimeConfig
from onec_runtime.errors import InvalidMessageSequence
from onec_runtime.processes import FileModeProcesses
from onec_runtime.rdbg.transport import TranscriptEntry
from onec_runtime.supervised_processes import SupervisedGenerationProcesses
from onec_runtime.supervisor_protocol import (
    PROTOCOL_VERSION,
    ControlMessage,
    MessageKind,
    MessageSender,
)


def spawned_worker(
    generation_id: int,
    connection: Connection,
    debug_port: int,
    run_dir: Path,
    marker: str,
) -> None:
    sender = MessageSender(generation_id)
    connection.send(
        sender.create(
            MessageKind.DEBUG_READY,
            debug_port=debug_port,
            run_dir=str(run_dir),
            worker_arg=marker,
            worker_arg_type=type(marker).__name__,
        )
    )
    request = connection.recv()
    connection.send(
        sender.create(MessageKind.STATUS_RESULT, request_kind=request.kind.value)
    )
    Event().wait()


def invalid_ready_worker(
    generation_id: int,
    connection: Connection,
    debug_port: int,
    run_dir: Path,
    marker: str,
) -> None:
    connection.send(
        ControlMessage(
            PROTOCOL_VERSION,
            generation_id,
            2,
            MessageKind.DEBUG_READY,
            {},
        )
    )
    Event().wait()


def wrong_first_message_worker(
    generation_id: int,
    connection: Connection,
    debug_port: int,
    run_dir: Path,
    marker: str,
) -> None:
    sender = MessageSender(generation_id)
    connection.send(sender.create(MessageKind.STATUS_RESULT, state="idle"))
    connection.send(sender.create(MessageKind.DEBUG_READY))
    Event().wait()


def naturally_exiting_worker(
    generation_id: int,
    connection: Connection,
    debug_port: int,
    run_dir: Path,
    marker: str,
) -> None:
    connection.send(MessageSender(generation_id).create(MessageKind.DEBUG_READY))


class FakeOwned:
    def __init__(self, pid: int, calls: list[tuple[int, float]]) -> None:
        self.pid = pid
        self.calls = calls

    def close(self, timeout_s: float = 10.0) -> None:
        self.calls.append((self.pid, timeout_s))


@dataclass
class FakeClock:
    now: float = 0.0

    def __call__(self) -> float:
        return self.now


class TimeoutConsumingProcess:
    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self.pid = 103
        self.exitcode: int | None = None
        self.join_timeouts: list[float] = []
        self.terminated = False
        self.killed = False
        self.closed = False

    def is_alive(self) -> bool:
        return self.exitcode is None

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def join(self, timeout: float | None = None) -> None:
        wait_s = 0.0 if timeout is None else timeout
        self.join_timeouts.append(wait_s)
        self._clock.now += wait_s
        if self.killed:
            self.exitcode = -9

    def close(self) -> None:
        self.closed = True


@dataclass
class BehaviorDataclass:
    def launch(self) -> None:
        return None


class BehaviorDict(dict[object, object]):
    def close(self) -> None:
        return None


class BehaviorList(list[object]):
    def launch(self) -> None:
        return None


def runtime_config(tmp_path: Path) -> RuntimeConfig:
    platform = tmp_path / "bin"
    platform.mkdir()
    for name in ("1cv8.exe", "1cv8c.exe", "dbgs.exe"):
        (platform / name).touch()
    return RuntimeConfig(tmp_path, platform)


def test_existing_artifact_sink_appends_in_the_given_run_directory(
    tmp_path: Path,
) -> None:
    sink = ExistingArtifactSink(tmp_path)

    sink.append_jsonl("controller-events.jsonl", {"event": "ready"})

    assert list(tmp_path.iterdir()) == [tmp_path / "controller-events.jsonl"]
    assert json.loads(
        (tmp_path / "controller-events.jsonl").read_text(encoding="utf-8")
    ) == {"event": "ready"}


def test_existing_artifact_sink_preserves_transcript_shape(tmp_path: Path) -> None:
    sink = ExistingArtifactSink(tmp_path)

    sink.transcript(
        TranscriptEntry(
            monotonic_ns=123,
            command="pingDebugUIParams",
            request="запрос".encode(),
            status_code=200,
            response="ответ".encode(),
            duration_ms=1.25,
            error="",
        )
    )

    payload = json.loads(
        (tmp_path / "rdbg-transcript.jsonl").read_text(encoding="utf-8")
    )
    assert set(payload) == {
        "timestamp",
        "monotonic_ns",
        "command",
        "status_code",
        "duration_ms",
        "error",
        "request_xml",
        "response_xml",
    }
    assert payload | {"timestamp": "ignored"} == {
        "timestamp": "ignored",
        "monotonic_ns": 123,
        "command": "pingDebugUIParams",
        "status_code": 200,
        "duration_ms": 1.25,
        "error": "",
        "request_xml": "запрос",
        "response_xml": "ответ",
    }


def test_controller_uses_spawn_and_receives_only_driver_arguments(
    tmp_path: Path,
) -> None:
    processes = SupervisedGenerationProcesses(
        runtime_config(tmp_path),
        7,
        spawned_worker,
        ("driver-marker",),
    )
    try:
        processes.start_controller(1550, tmp_path)
        message = processes.receive(5.0)

        assert processes._context.get_start_method() == "spawn"
        assert processes.controller_pid is not None
        assert message.kind is MessageKind.DEBUG_READY
        assert message.payload == {
            "debug_port": 1550,
            "run_dir": str(tmp_path),
            "worker_arg": "driver-marker",
            "worker_arg_type": "str",
        }
        processes.send(MessageSender(7).create(MessageKind.STATUS))
        response = processes.receive(5.0)
        assert response.kind is MessageKind.STATUS_RESULT
        assert response.payload == {"request_kind": "status"}
    finally:
        processes.terminate_controller(2.0)

    assert processes.controller_exitcode() is not None
    assert processes._controller is None


@pytest.mark.parametrize(
    "unsafe_arg",
    [
        lambda: None,
        [lambda: None],
        {"launch": lambda: None},
        object(),
    ],
)
def test_worker_arguments_reject_nested_callbacks(
    tmp_path: Path,
    unsafe_arg: object,
) -> None:
    config = runtime_config(tmp_path)

    with pytest.raises(TypeError, match="data-only"):
        SupervisedGenerationProcesses(config, 7, spawned_worker, (unsafe_arg,))


def test_worker_arguments_reject_nested_process_owners(tmp_path: Path) -> None:
    config = runtime_config(tmp_path)

    with pytest.raises(TypeError, match="data-only"):
        SupervisedGenerationProcesses(
            config,
            7,
            spawned_worker,
            ({"owner": [FileModeProcesses(config)]},),
        )


@pytest.mark.parametrize(
    "unsafe_arg",
    [
        BehaviorDataclass(),
        BehaviorDict(),
        BehaviorList(),
    ],
)
def test_worker_arguments_reject_behavior_bearing_dto_and_container_subclasses(
    tmp_path: Path,
    unsafe_arg: object,
) -> None:
    config = runtime_config(tmp_path)

    with pytest.raises(TypeError, match="data-only"):
        SupervisedGenerationProcesses(
            config,
            7,
            spawned_worker,
            ({"nested": unsafe_arg},),
        )


def test_worker_arguments_accept_nested_serializable_driver_data(
    tmp_path: Path,
) -> None:
    config = runtime_config(tmp_path)
    processes = SupervisedGenerationProcesses(
        config,
        7,
        spawned_worker,
        (config, {"locations": [tmp_path], "enabled": True}),
    )

    processes.terminate_controller(2.0)


def test_debuggee_cannot_start_until_parent_receives_debug_ready(
    tmp_path: Path,
) -> None:
    processes = SupervisedGenerationProcesses(
        runtime_config(tmp_path), 8, spawned_worker, ("driver-marker",)
    )
    try:
        processes.start_controller(1551, tmp_path)

        with pytest.raises(InvalidMessageSequence):
            processes.start_debuggee(1551)

        assert processes.receive(5.0).kind is MessageKind.DEBUG_READY
    finally:
        processes.terminate_controller(2.0)


def test_invalid_debug_ready_sequence_does_not_unlock_debuggee(
    tmp_path: Path,
) -> None:
    processes = SupervisedGenerationProcesses(
        runtime_config(tmp_path), 8, invalid_ready_worker, ("driver-marker",)
    )
    try:
        processes.start_controller(1551, tmp_path)

        with pytest.raises(InvalidMessageSequence, match="sequence"):
            processes.receive(5.0)
        with pytest.raises(InvalidMessageSequence, match="DEBUG_READY"):
            processes.start_debuggee(1551)
    finally:
        processes.terminate_controller(2.0)


def test_wrong_first_controller_message_permanently_fails_startup_gate(
    tmp_path: Path,
) -> None:
    processes = SupervisedGenerationProcesses(
        runtime_config(tmp_path),
        8,
        wrong_first_message_worker,
        ("driver-marker",),
    )
    try:
        processes.start_controller(1551, tmp_path)

        with pytest.raises(InvalidMessageSequence, match="first.*DEBUG_READY"):
            processes.receive(5.0)
        with pytest.raises(InvalidMessageSequence, match="startup.*failed"):
            processes.receive(5.0)
        with pytest.raises(InvalidMessageSequence, match="DEBUG_READY"):
            processes.start_debuggee(1551)
    finally:
        processes.terminate_controller(2.0)


def test_all_stopped_tracks_each_owned_resource_and_cleanup_is_idempotent(
    tmp_path: Path,
) -> None:
    calls: list[tuple[int, float]] = []
    processes = SupervisedGenerationProcesses(
        runtime_config(tmp_path), 9, spawned_worker, ("driver-marker",)
    )
    processes._file_processes.debuggee = FakeOwned(101, calls)  # type: ignore[assignment]
    processes._file_processes.debug_server = FakeOwned(102, calls)  # type: ignore[assignment]

    assert processes.all_stopped() is False
    processes.terminate_onec(3.0)
    assert processes.all_stopped() is False
    processes.terminate_dbgs(4.0)
    assert processes.all_stopped() is True

    processes.terminate_controller(2.0)
    processes.terminate_onec(3.0)
    processes.terminate_dbgs(4.0)
    assert calls == [(101, 3.0), (102, 4.0)]


def test_controller_terminate_and_kill_share_one_absolute_timeout_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    monkeypatch.setattr(
        supervised_processes_module,
        "monotonic",
        clock,
        raising=False,
    )
    processes = SupervisedGenerationProcesses(
        runtime_config(tmp_path), 9, spawned_worker, ("driver-marker",)
    )
    controller = TimeoutConsumingProcess(clock)
    processes._controller = controller  # type: ignore[assignment]
    processes._controller_started = True

    processes.terminate_controller(2.0)

    assert controller.join_timeouts[:2] == [2.0, 0.0]
    assert sum(controller.join_timeouts[:2]) == pytest.approx(2.0)
    assert controller.terminated is True
    assert controller.killed is True
    assert controller.closed is True
    assert processes.controller_exitcode() == -9
    assert processes._controller is None
    assert processes._parent_connection.closed
    assert processes._child_connection.closed

    processes.terminate_controller(2.0)
    assert controller.join_timeouts == [2.0, 0.0, 0.0]


def test_natural_controller_exit_is_reaped_before_all_stopped(
    tmp_path: Path,
) -> None:
    processes = SupervisedGenerationProcesses(
        runtime_config(tmp_path),
        10,
        naturally_exiting_worker,
        ("driver-marker",),
    )
    processes.start_controller(1552, tmp_path)
    assert processes.receive(5.0).kind is MessageKind.DEBUG_READY

    deadline = monotonic() + 5.0
    while monotonic() < deadline and not processes.all_stopped():
        sleep(0.01)

    assert processes.all_stopped() is True
    assert processes.controller_exitcode() == 0
    assert processes._controller is None
    assert processes._parent_connection.closed
    assert processes._child_connection.closed

    processes.terminate_controller(2.0)
    processes.terminate_controller(2.0)
    assert processes.all_stopped() is True
