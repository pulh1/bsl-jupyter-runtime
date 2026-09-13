"""An abrupt VS Code kernel exit must not leave its 1C license in use."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
import psutil

from onec_runtime.rdbg.models import TargetId
from onec_runtime_jupyter import session_guardian


SESSION = UUID("85236e38-1132-4646-86c4-d0c19ba95672")
INFOBASE = UUID("1b9552fe-8333-48de-9d43-a84c1b13b3bb")
CLIENT = UUID("1b9c0a1c-8c4e-4b00-a102-06cf46fcda28")
SERVER = UUID("dacf92c3-b017-45a4-b765-a7f7267732e8")


def runtime_resource(tmp_path: Path):
    target = TargetId(CLIENT, "runtime_test", SESSION, 1, INFOBASE)
    runtime_config = SimpleNamespace(
        is_server_infobase=True,
        workspace=tmp_path,
        platform_bin=tmp_path / "bin",
        infobase_arguments=("/S", r"localhost\runtime_test"),
        debug_host="127.0.0.1",
        debug_port=1550,
        infobase_debug_alias="runtime_test",
        password="private-password",
    )
    return SimpleNamespace(
        config=SimpleNamespace(runtime=runtime_config),
        _rdbg=SimpleNamespace(ui_id=UUID(int=99), _bound_client_target=target),
        owned_process_snapshot=lambda: (
            {"role": "onec", "pid": os.getpid(), "create_time": 123.0,
             "executable": str(tmp_path / "bin" / "1cv8c.exe")},
        ),
    )


def test_guard_lease_records_only_authenticated_session_and_process_identity(tmp_path):
    runtime = runtime_resource(tmp_path)

    lease = session_guardian.GuardLease.from_runtime(runtime)
    payload = lease.to_json()

    assert session_guardian.GuardLease.from_json(payload) == lease
    assert lease.session_id == SESSION
    assert lease.client_target_id == CLIENT
    assert lease.infobase_id == INFOBASE
    assert lease.ui_id == UUID(int=99)
    assert lease.client_pid == os.getpid()
    assert lease.owner_pid == os.getpid()
    assert lease.server_host == "localhost"
    assert "private-password" not in payload


def test_guard_refuses_an_unbound_or_file_runtime(tmp_path):
    runtime = runtime_resource(tmp_path)
    runtime._rdbg._bound_client_target = None
    with pytest.raises(ValueError, match="bound"):
        session_guardian.GuardLease.from_runtime(runtime)
    runtime.config.runtime.is_server_infobase = False
    assert session_guardian.GuardLease.from_runtime(runtime) is None


def test_guard_detects_reused_owner_pid(monkeypatch, tmp_path):
    lease = session_guardian.GuardLease.from_runtime(runtime_resource(tmp_path))
    monkeypatch.setattr(session_guardian.psutil, "Process", lambda _pid: SimpleNamespace(create_time=lambda: lease.owner_create_time + 5))
    assert not session_guardian._owner_alive(lease)


def test_guard_never_treats_inaccessible_owner_as_dead(monkeypatch, tmp_path):
    lease = session_guardian.GuardLease.from_runtime(runtime_resource(tmp_path))
    monkeypatch.setattr(
        session_guardian.psutil, "Process",
        lambda _pid: (_ for _ in ()).throw(psutil.AccessDenied(_pid)),
    )
    assert session_guardian._owner_alive(lease)


def test_rdbg_cleanup_reuses_old_ui_and_terminates_only_bound_server(monkeypatch, tmp_path):
    lease = session_guardian.GuardLease.from_runtime(runtime_resource(tmp_path))
    calls: list[tuple[str, bytes]] = []
    foreign = UUID(int=7)

    def target(target_id: UUID, seance: UUID) -> str:
        return (
            '<item><targetID xmlns="http://v8.1c.ru/8.2/debugger"'
            f'><id>{target_id}</id><infoBaseAlias>runtime_test</infoBaseAlias>'
            f'<seanceId>{seance}</seanceId><infoBaseInstanceId>{INFOBASE}</infoBaseInstanceId>'
            '<targetType>Server</targetType></targetID>'
            '<state>Working</state><stateNum>1</stateNum></item>'
        )

    states = (
        '<response>' + target(SERVER, SESSION) + target(foreign, UUID(int=8)) + '</response>'
    ).encode()

    class Transport:
        def __init__(self, host, port):
            assert (host, port) == (lease.debug_host, lease.debug_port)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def request(self, command, payload=b"", **_kwargs):
            calls.append((command, payload))
            return states if command == "getDbgAllTargetStates" else b""

    monkeypatch.setattr(session_guardian, "RdbgTransport", Transport)
    session_guardian._terminate_via_rdbg(lease)

    commands = [command for command, _ in calls]
    assert commands == [
        "getDbgAllTargetStates", "terminateDbgTarget", "getDbgAllTargetStates", "detachDebugUI",
    ]
    termination = calls[1][1]
    assert str(SERVER).encode() in termination
    assert str(foreign).encode() not in termination
    assert str(lease.ui_id).encode() in termination


def test_guard_closes_only_the_exact_owned_client_process(monkeypatch, tmp_path):
    lease = session_guardian.GuardLease.from_runtime(runtime_resource(tmp_path))
    events: list[str] = []

    class Process:
        def __init__(self, create_time: float):
            self.started = create_time

        def create_time(self):
            return self.started

        def exe(self):
            return lease.client_executable

        def terminate(self):
            events.append("terminate")

        def wait(self, timeout):
            events.append("wait")

    monkeypatch.setattr(session_guardian.psutil, "Process", lambda _pid: Process(lease.client_create_time + 10))
    assert not session_guardian._close_owned_client(lease)
    assert events == []

    monkeypatch.setattr(session_guardian.psutil, "Process", lambda _pid: Process(lease.client_create_time))
    assert session_guardian._close_owned_client(lease)
    assert events == ["terminate", "wait"]


def test_rac_termination_requires_exact_session_infobase_and_client_app(tmp_path):
    lease = session_guardian.GuardLease.from_runtime(runtime_resource(tmp_path))
    calls: list[list[str]] = []
    terminated = False

    def runner(command: list[str]) -> str:
        nonlocal terminated
        calls.append(command)
        if command[1:3] == ["cluster", "list"]:
            return "cluster : d9988e14-568b-4f4d-9d75-bca045a7fd78\n"
        if command[1:3] == ["session", "info"]:
            if terminated:
                raise RuntimeError("session absent")
            return (
                f"session : {SESSION}\ninfobase : {INFOBASE}\n"
                "app-id : 1CV8C\n"
            )
        if command[1:3] == ["session", "terminate"]:
            terminated = True
            return ""
        raise AssertionError(command)

    assert session_guardian._terminate_exact_rac_session(lease, runner)
    assert sum(command[1:3] == ["session", "terminate"] for command in calls) == 1
    assert sum(command[1:3] == ["session", "info"] for command in calls) >= 2
    assert any(f"--session={SESSION}" in command for command in calls)

    calls.clear()

    def wrong_infobase(command: list[str]) -> str:
        response = runner(command)
        return response.replace(str(INFOBASE), str(UUID(int=3)))

    assert not session_guardian._terminate_exact_rac_session(lease, wrong_infobase)
    assert not any(command[1:3] == ["session", "terminate"] for command in calls)


def test_rac_does_not_mistake_empty_cluster_discovery_for_absent_session(tmp_path):
    lease = session_guardian.GuardLease.from_runtime(runtime_resource(tmp_path))
    with pytest.raises(RuntimeError, match="cluster"):
        session_guardian._terminate_exact_rac_session(lease, lambda _command: "")


def test_failed_orphan_cleanup_retains_exact_lease_for_recovery(monkeypatch, tmp_path):
    lease = replace(
        session_guardian.GuardLease.from_runtime(runtime_resource(tmp_path)),
        owner_pid=99999999,
    )
    path = tmp_path / "orphan.json"
    path.write_text(lease.to_json(), encoding="utf-8")
    monkeypatch.setattr(session_guardian, "_owner_alive", lambda _lease: False)
    monkeypatch.setattr(
        session_guardian, "_cleanup",
        lambda _lease: {"session_id": str(SESSION), "rdbg": False, "rac": False},
    )

    session_guardian.run_guardian(path)

    assert path.exists()
    assert path.with_suffix(".outcome.json").exists()


def test_next_start_retries_only_failed_dead_owner_from_same_infobase(monkeypatch, tmp_path):
    runtime = runtime_resource(tmp_path)
    lease = replace(
        session_guardian.GuardLease.from_runtime(runtime), owner_pid=99999999,
    )
    directory = tmp_path / ".runtime" / "kernel-guardians"
    directory.mkdir(parents=True)
    path = directory / "orphan.json"
    path.write_text(lease.to_json(), encoding="utf-8")
    path.with_suffix(".outcome.json").write_text(
        '{"rdbg": false, "rac": false}', encoding="utf-8",
    )
    calls: list[UUID] = []
    monkeypatch.setattr(session_guardian, "_owner_alive", lambda _lease: False)
    monkeypatch.setattr(
        session_guardian, "_cleanup",
        lambda candidate: calls.append(candidate.session_id) or
        {"session_id": str(candidate.session_id), "rdbg": False, "rac": True},
    )

    assert session_guardian.recover_failed_guards(runtime.config) == ()
    assert calls == [SESSION]
    assert not path.exists()

    path.write_text(replace(lease, alias="another_base").to_json(), encoding="utf-8")
    assert session_guardian.recover_failed_guards(runtime.config) == ()
    assert calls == [SESSION]


def test_next_start_recovers_lease_when_guardian_process_disappeared(monkeypatch, tmp_path):
    runtime = runtime_resource(tmp_path)
    lease = replace(
        session_guardian.GuardLease.from_runtime(runtime), owner_pid=99999999,
    )
    directory = tmp_path / ".runtime" / "kernel-guardians"
    directory.mkdir(parents=True)
    path = directory / "orphan.json"
    path.write_text(lease.to_json(), encoding="utf-8")
    path.with_suffix(".ready").write_text(
        '{"pid": 88888888, "create_time": 1.0}', encoding="utf-8",
    )
    monkeypatch.setattr(session_guardian, "_process_alive", lambda pid, started: False, raising=False)
    monkeypatch.setattr(
        session_guardian, "_cleanup",
        lambda _lease: {"session_id": str(SESSION), "rdbg": False, "rac": True},
    )

    assert session_guardian.recover_failed_guards(runtime.config) == ()
    assert not path.exists()
    assert path.with_suffix(".outcome.json").exists()


def test_next_start_does_not_compete_with_running_guardian(monkeypatch, tmp_path):
    runtime = runtime_resource(tmp_path)
    lease = replace(
        session_guardian.GuardLease.from_runtime(runtime), owner_pid=99999999,
    )
    directory = tmp_path / ".runtime" / "kernel-guardians"
    directory.mkdir(parents=True)
    path = directory / "orphan.json"
    path.write_text(lease.to_json(), encoding="utf-8")
    path.with_suffix(".ready").write_text(
        '{"pid": 88888888, "create_time": 1.0}', encoding="utf-8",
    )
    monkeypatch.setattr(session_guardian, "_owner_alive", lambda _lease: False)
    monkeypatch.setattr(session_guardian, "_process_alive", lambda pid, started: True)
    monkeypatch.setattr(
        session_guardian, "_cleanup",
        lambda _lease: pytest.fail("competing cleanup started"),
    )

    assert session_guardian.recover_failed_guards(runtime.config) == ()
    assert path.exists()
