from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import os
from uuid import uuid4

import pytest

from onec_runtime.config import RuntimeConfig
from onec_runtime.errors import CommandTimeout, ProtocolError, RdbgDebugUiNotRegistered, TargetLost
from onec_runtime.extension_state import ExtensionStateStore, InfobaseExtensionLock
from onec_runtime.processes import FileModeProcesses, debuggee_command
from onec_runtime.rdbg.models import DebugTarget, TargetId
from onec_runtime.session import RuntimeSession, RuntimeSessionConfig
from onec_runtime.artifacts import ArtifactWriter
from onec_runtime import toolchain


def server_config(tmp_path: Path, **kwargs: object) -> RuntimeConfig:
    platform = tmp_path / "bin"
    platform.mkdir(exist_ok=True)
    for name in ("1cv8.exe", "1cv8c.exe"):
        (platform / name).touch()
    return RuntimeConfig(
        workspace=tmp_path,
        platform_bin=platform,
        connection_string='Srvr="localhost:1541";Ref="runtime_test";',
        username="runtime-user",
        password="private-password",
        **kwargs,
    )


def test_server_connection_does_not_require_local_database_or_debugger(tmp_path: Path) -> None:
    config = server_config(tmp_path)

    assert config.infobase_arguments == ("/S", r"localhost:1541\runtime_test")
    assert config.is_server_infobase
    assert config.uses_external_infobase
    assert config.server_target_type == "Server"
    assert config.infobase_debug_alias == "runtime_test"
    assert config.debug_port == 1550
    assert config.build_infobase_dir == tmp_path / ".runtime" / "build-infobase"
    assert "private-password" not in repr(config)
    with pytest.raises(ValueError, match="server infobase"):
        _ = config.infobase_dir


def test_server_debugger_alias_can_be_explicit(tmp_path: Path) -> None:
    config = server_config(tmp_path, debug_alias="cluster-alias", debug_port=1650)
    assert config.infobase_debug_alias == "cluster-alias"
    assert config.debug_port == 1650


@pytest.mark.parametrize("connection", ["", "host", "host\\", "\\base", "host\\base\\other", "host\\base;Pwd=secret", "host\\base\n"])
def test_invalid_server_connection_is_rejected_privately(tmp_path: Path, connection: str) -> None:
    original = server_config(tmp_path)
    with pytest.raises(ValueError, match="connection_string") as error:
        replace(original, connection_string=connection)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("port", [0, 65536, True])
def test_invalid_external_debugger_port_is_rejected(tmp_path: Path, port: int) -> None:
    with pytest.raises(ValueError, match="debug_port"):
        server_config(tmp_path, debug_port=port)


def test_server_client_launch_routes_execute_and_credentials_correctly(tmp_path: Path) -> None:
    config = server_config(tmp_path)
    command = debuggee_command(config, 1550)
    assert command[command.index("/S") + 1] == r"localhost:1541\runtime_test"
    assert "/F" not in command
    assert command[command.index("/S") + 2 : command.index("/S") + 4] == ["/Execute", str(config.kernel_epf)]
    assert command[command.index("/N") + 1] == "runtime-user"
    assert command[command.index("/P") + 1] == "private-password"


@pytest.mark.parametrize("name", [
    "dump_target_extension_files_command", "dump_target_extension_cfe_command",
    "load_target_extension_cfe_command",
    "apply_product_extension_command", "load_target_extension_source_command",
    "deploy_extension_command", "update_extension_command", "apply_extension_command",
    "apply_target_extension_source_command",
])
def test_every_target_designer_operation_uses_server_connection(tmp_path: Path, name: str) -> None:
    config = server_config(tmp_path)
    function = getattr(toolchain, name)
    if name in {"dump_target_extension_files_command", "dump_target_extension_cfe_command", "load_target_extension_cfe_command"}:
        command = function(config, tmp_path / "artifact", tmp_path / "operation.log")
    elif name in {"apply_product_extension_command", "load_target_extension_source_command", "apply_target_extension_source_command"}:
        command = function(config, tmp_path / "artifact")
    else:
        command = function(config)
    assert command[command.index("/S") + 1] == r"localhost:1541\runtime_test"
    assert "/F" not in command
    assert command[command.index("/N") + 1] == config.username
    assert command[command.index("/P") + 1] == config.password
    if "/UpdateDBCfg" in command:
        assert "-Dynamic-" in command


def test_server_runtime_still_builds_workers_in_local_file_database(tmp_path: Path) -> None:
    config = server_config(tmp_path)
    command = toolchain.build_worker_command(config)
    assert command[command.index("/F") + 1] == str(config.build_infobase_dir)
    assert "/S" not in command
    with pytest.raises(ValueError, match="server infobase"):
        toolchain.create_infobase_command(config)


def test_server_extension_state_keys_are_stable_and_database_specific(tmp_path: Path) -> None:
    config = server_config(tmp_path)
    equivalent = replace(config, connection_string='Srvr="LOCALHOST:1541";Ref="RUNTIME_TEST";')
    different = replace(config, connection_string='Srvr="localhost:1541";Ref="other";')
    store = ExtensionStateStore(tmp_path, config.infobase_identity)
    same_store = ExtensionStateStore(tmp_path, equivalent.infobase_identity)
    other_store = ExtensionStateStore(tmp_path, different.infobase_identity)
    lock = InfobaseExtensionLock(tmp_path, config.infobase_identity)
    assert store.path == same_store.path
    assert store.path != other_store.path
    assert store.path.stem == lock.path.stem
    assert "private-password" not in config.infobase_identity


def test_file_connection_identity_preserves_existing_cache_key(tmp_path: Path) -> None:
    config = server_config(tmp_path)
    (config.platform_bin / "dbgs.exe").touch()
    file_config = replace(config, connection_string=None)
    assert file_config.infobase_arguments == ("/F", str(file_config.infobase_dir))
    assert file_config.server_target_type == "ServerEmulation"
    assert file_config.infobase_debug_alias == "DefAlias"
    assert ExtensionStateStore(tmp_path, file_config.infobase_identity).path == ExtensionStateStore(tmp_path, file_config.infobase_dir).path


def test_server_process_owner_starts_and_closes_only_the_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = server_config(tmp_path, debug_port=1650)
    processes = FileModeProcesses(config)
    events: list[str] = []

    def spawn(command: list[str], stem: str, **kwargs: object) -> object:
        assert command[0] == str(config.client_exe)
        assert command[command.index("/DEBUGGERURL") + 1] == "http://127.0.0.1:1650"
        events.append("client-start")
        return SimpleNamespace(
            pid=os.getpid(),
            ensure_running=lambda: events.append("client-check"),
            close=lambda _timeout: events.append("client-close"),
        )

    monkeypatch.setattr(processes, "_spawn", spawn)
    assert processes.start_debug_server() == 1650
    processes.start_debuggee(1650, execute_external=False)
    processes.ensure_running()
    assert processes.debug_server is None
    processes.close()
    assert events == ["client-start", "client-check", "client-close"]


def test_server_session_cleanup_detaches_shared_debugger_after_closing_owned_client(tmp_path: Path) -> None:
    config = server_config(tmp_path)
    events: list[str] = []
    processes = FileModeProcesses(config)
    processes.debuggee = SimpleNamespace(pid=os.getpid(), close=lambda _timeout: events.append("client-close"))
    session = RuntimeSession(
        RuntimeSessionConfig(config, tmp_path / "evidence"),
        processes,
        SimpleNamespace(close=lambda: events.append("transport-close")),
        SimpleNamespace(detach=lambda: events.append("debugger-detach"),
                        terminate_bound_server_session=lambda: events.append("server-terminate")),
        SimpleNamespace(
            close=lambda: events.append("api-close"),
            owns_debug_ui_stream=lambda: False,
        ),
        ArtifactWriter(tmp_path / "evidence", "test"),
        heartbeat_interval_s=3600,
    )
    try:
        snapshot = session.owned_process_snapshot()
        assert [item["role"] for item in snapshot] == ["onec"]
    finally:
        session.close()
    assert events == ["api-close", "server-terminate", "client-close", "debugger-detach", "transport-close"]
    session.close()
    assert len(events) == 5


def test_kernel_shutdown_terminates_server_before_worker_cleanup(tmp_path: Path) -> None:
    config = server_config(tmp_path)
    events: list[str] = []
    processes = SimpleNamespace(close=lambda **_kwargs: events.append("client-close"))
    session = RuntimeSession(
        RuntimeSessionConfig(config, tmp_path / "evidence"),
        processes,
        SimpleNamespace(close=lambda: events.append("transport-close")),
        SimpleNamespace(
            terminate_bound_server_session=lambda: events.append("server-terminate") or True,
            detach=lambda: events.append("debugger-detach"),
        ),
        SimpleNamespace(
            close=lambda: events.append("api-close"),
            owns_debug_ui_stream=lambda: False,
        ),
        ArtifactWriter(tmp_path / "evidence", "test"),
        heartbeat_interval_s=3600,
    )

    session.close_for_kernel_shutdown()

    assert events == ["server-terminate", "client-close", "debugger-detach", "transport-close"]
    session.close()
    assert events == ["server-terminate", "client-close", "debugger-detach", "transport-close"]


@pytest.mark.parametrize("startup", [False, True])
def test_server_close_waits_for_old_target_absence_before_becoming_terminal(
    tmp_path: Path, startup: bool,
) -> None:
    from onec_runtime.session import _StartupAttemptCleanup

    events: list[str] = []
    seance_id = uuid4()
    client = TargetId(uuid4(), "runtime_test", seance_id=seance_id)
    server = TargetId(uuid4(), "runtime_test", seance_id=seance_id)
    old_target_visible = True

    def verify_absence(expected_target: TargetId) -> object:
        assert expected_target == server
        events.append("verify-absence")
        if old_target_visible:
            raise CommandTimeout("old target remains in debugger registry")
        return object()

    rdbg = SimpleNamespace(
        target=DebugTarget(server, "Server", "stopped"),
        _bound_client_target=client,
        terminate_bound_server_session=lambda: events.append("server-terminate") or True,
        wait_for_bound_server_targets_absent=verify_absence,
        detach=lambda: events.append("debugger-detach"),
    )
    processes = SimpleNamespace(close=lambda **_kwargs: events.append("client-close"))
    transport = SimpleNamespace(close=lambda: events.append("transport-close"))
    config = server_config(tmp_path)
    if startup:
        owner = _StartupAttemptCleanup(config, processes, transport, rdbg)
        close = owner.retry_cleanup
    else:
        owner = RuntimeSession(
            RuntimeSessionConfig(config, tmp_path / "evidence"),
            processes,
            transport,
            rdbg,
            SimpleNamespace(close=lambda: events.append("api-close")),
            ArtifactWriter(tmp_path / "evidence", "test"),
            heartbeat_interval_s=3600,
        )
        close = owner.close

    # The active RDBG target may change after the owner was created; teardown
    # must still verify the server target from this owner's original session.
    rdbg.target = DebugTarget(
        TargetId(uuid4(), "runtime_test", seance_id=uuid4()), "Server", "stopped"
    )
    with pytest.raises(ProtocolError, match="cleanup failed"):
        close()
    assert owner._server_session_terminated is False
    assert "verify-absence" in events
    assert "debugger-detach" not in events
    assert "transport-close" not in events
    if not startup:
        assert owner.is_closed is False

    old_target_visible = False
    close()
    assert owner._server_session_terminated is True
    assert events.count("server-terminate") == 1
    assert events.count("verify-absence") == 2
    assert events[-2:] == ["debugger-detach", "transport-close"]
    if not startup:
        assert owner.is_closed is True


def test_kernel_shutdown_retries_native_termination_before_closing_client(tmp_path: Path) -> None:
    events: list[str] = []

    def terminate() -> bool:
        events.append("server-terminate")
        if events.count("server-terminate") == 1:
            raise ProtocolError("temporary RDBG failure")
        return True

    session = RuntimeSession(
        RuntimeSessionConfig(server_config(tmp_path), tmp_path / "evidence"),
        SimpleNamespace(close=lambda **_kwargs: events.append("client-close")),
        SimpleNamespace(close=lambda: events.append("transport-close")),
        SimpleNamespace(terminate_bound_server_session=terminate,
                        detach=lambda: events.append("debugger-detach")),
        SimpleNamespace(
            close=lambda: events.append("api-close"),
            owns_debug_ui_stream=lambda: False,
        ),
        ArtifactWriter(tmp_path / "evidence", "test"),
        heartbeat_interval_s=3600,
    )

    with pytest.raises(ProtocolError, match="cleanup failed"):
        session.close_for_kernel_shutdown()
    assert events == ["server-terminate"]

    session.close_for_kernel_shutdown()
    assert events == [
        "server-terminate", "server-terminate", "client-close",
        "debugger-detach", "transport-close",
    ]


def test_server_session_cleanup_retries_failed_debug_ui_deregistration(
    tmp_path: Path,
) -> None:
    class FailOnceRdbg:
        def __init__(self) -> None:
            self.detach_calls = 0

        def terminate_bound_server_session(self) -> None:
            events.append("server-terminate")

        def detach(self) -> None:
            self.detach_calls += 1
            events.append("debugger-detach")
            if self.detach_calls == 1:
                raise ProtocolError("debug UI deregistration failed")

    config = server_config(tmp_path)
    events: list[str] = []
    processes = FileModeProcesses(config)
    processes.debuggee = SimpleNamespace(
        pid=os.getpid(), close=lambda _timeout: events.append("client-close")
    )
    rdbg = FailOnceRdbg()
    session = RuntimeSession(
        RuntimeSessionConfig(config, tmp_path / "evidence"),
        processes,
        SimpleNamespace(close=lambda: events.append("transport-close")),
        rdbg,
        SimpleNamespace(
            close=lambda: events.append("api-close"),
            owns_debug_ui_stream=lambda: False,
        ),
        ArtifactWriter(tmp_path / "evidence", "test"),
        heartbeat_interval_s=3600,
    )

    with pytest.raises(ProtocolError, match="cleanup failed"):
        session.close()

    assert events == ["api-close", "server-terminate", "client-close", "debugger-detach"]
    assert session._debug_ui_detached is False
    assert session._transport_closed is False
    assert session._closed is False

    session.close()

    assert events == [
        "api-close",
        "server-terminate",
        "client-close",
        "debugger-detach",
        "debugger-detach",
        "transport-close",
    ]
    assert session._debug_ui_detached is True
    assert session._transport_closed is True
    assert session._closed is True


@pytest.mark.parametrize("startup", [False, True])
def test_lost_debug_ui_still_closes_owned_client_and_transport(
    tmp_path: Path, startup: bool,
) -> None:
    from onec_runtime.session import _StartupAttemptCleanup

    events: list[str] = []
    config = server_config(tmp_path)
    processes = SimpleNamespace(close=lambda **_kwargs: events.append("client-close"))
    transport = SimpleNamespace(close=lambda: events.append("transport-close"))

    def terminate() -> bool:
        events.append("native-terminate")
        raise RdbgDebugUiNotRegistered("owned debug UI disappeared")

    rdbg = SimpleNamespace(
        terminate_bound_server_session=terminate,
        detach=lambda: events.append("detach"),
    )
    if startup:
        owner = _StartupAttemptCleanup(config, processes, transport, rdbg)
        close = owner.retry_cleanup
    else:
        owner = RuntimeSession(
            RuntimeSessionConfig(config, tmp_path / "evidence"),
            processes,
            transport,
            rdbg,
            SimpleNamespace(close=lambda: events.append("api-close")),
            ArtifactWriter(tmp_path / "evidence", "test"),
            heartbeat_interval_s=3600,
        )
        close = owner.close

    close()
    assert events == [
        *([] if startup else ["api-close"]),
        "native-terminate", "client-close", "detach", "transport-close",
    ]
    close()
    assert events.count("native-terminate") == 1


def test_heartbeat_lost_debug_ui_releases_owned_runtime(tmp_path: Path) -> None:
    events: list[str] = []
    config = server_config(tmp_path)

    def lost_heartbeat() -> None:
        events.append("heartbeat")
        raise RdbgDebugUiNotRegistered("owned debug UI disappeared")

    def terminate() -> bool:
        events.append("native-terminate")
        raise RdbgDebugUiNotRegistered("owned debug UI disappeared")

    session = RuntimeSession(
        RuntimeSessionConfig(config, tmp_path / "evidence"),
        SimpleNamespace(close=lambda **_kwargs: events.append("client-close")),
        SimpleNamespace(close=lambda: events.append("transport-close")),
        SimpleNamespace(
            heartbeat=lost_heartbeat,
            terminate_bound_server_session=terminate,
            detach=lambda: events.append("detach"),
        ),
        SimpleNamespace(
            close=lambda: events.append("api-close"),
            owns_debug_ui_stream=lambda: False,
        ),
        ArtifactWriter(tmp_path / "evidence", "test"),
        heartbeat_interval_s=0.01,
    )
    try:
        session._heartbeat_thread.join(1)
        assert session._closed
        assert events == [
            "heartbeat", "api-close", "native-terminate", "client-close",
            "detach", "transport-close",
        ]
    finally:
        session.close()


def test_heartbeat_closes_when_owned_client_has_exited(tmp_path: Path) -> None:
    events: list[str] = []
    config = server_config(tmp_path)

    def client_exited() -> None:
        events.append("client-check")
        raise TargetLost("owned 1C client exited")

    session = RuntimeSession(
        RuntimeSessionConfig(config, tmp_path / "evidence"),
        SimpleNamespace(
            ensure_running=client_exited,
            close=lambda **_kwargs: events.append("client-close"),
        ),
        SimpleNamespace(close=lambda: events.append("transport-close")),
        SimpleNamespace(
            heartbeat=lambda: events.append("heartbeat"),
            terminate_bound_server_session=lambda: events.append("native-terminate") or False,
            detach=lambda: events.append("detach"),
        ),
        SimpleNamespace(
            close=lambda: events.append("api-close"),
            owns_debug_ui_stream=lambda: False,
        ),
        ArtifactWriter(tmp_path / "evidence", "test"),
        heartbeat_interval_s=0.01,
    )
    try:
        session._heartbeat_thread.join(1)
        assert session._closed
        assert events == [
            "heartbeat", "client-check", "api-close", "native-terminate",
            "client-close", "detach", "transport-close",
        ]
    finally:
        session.close()


@pytest.mark.parametrize("startup", [False, True])
def test_native_client_cleanup_retry_preserves_order_and_grace(tmp_path: Path, startup: bool) -> None:
    from onec_runtime.session import _StartupAttemptCleanup

    events: list[object] = []

    def terminate() -> bool:
        events.append("native-terminate")
        if events.count("native-terminate") == 1:
            raise ProtocolError("server termination uncertain")
        return True

    config = server_config(tmp_path)
    processes = SimpleNamespace(close=lambda **kwargs: events.append(("client-close", kwargs)))
    transport = SimpleNamespace(close=lambda: events.append("transport-close"))
    rdbg = SimpleNamespace(terminate_bound_server_session=terminate, detach=lambda: events.append("detach"))
    if startup:
        owner = _StartupAttemptCleanup(config, processes, transport, rdbg)
        close = owner.retry_cleanup
    else:
        owner = RuntimeSession(
            RuntimeSessionConfig(config, tmp_path / "evidence"), processes, transport, rdbg,
            SimpleNamespace(close=lambda: None), ArtifactWriter(tmp_path / "evidence", "test"),
            heartbeat_interval_s=3600,
        )
        close = owner.close
    with pytest.raises(ProtocolError, match="cleanup failed"):
        close()
    assert events == ["native-terminate"]
    close()
    assert events == ["native-terminate", "native-terminate", ("client-close", {"graceful_client_timeout_s": 3.0}), "detach", "transport-close"]
    close()
    assert len(events) == 5


@pytest.mark.parametrize("startup", [False, True])
def test_client_process_cleanup_failure_keeps_transport_for_retry(tmp_path: Path, startup: bool) -> None:
    from onec_runtime.session import _StartupAttemptCleanup

    events: list[object] = []
    grace_periods: list[float] = []

    def close_processes(*, graceful_client_timeout_s: float) -> None:
        grace_periods.append(graceful_client_timeout_s)
        events.append("client-close")
        if len(grace_periods) == 1:
            raise ProtocolError("client still alive")

    config = server_config(tmp_path)
    processes = SimpleNamespace(close=close_processes)
    transport = SimpleNamespace(close=lambda: events.append("transport-close"))
    rdbg = SimpleNamespace(
        terminate_bound_server_session=lambda: events.append("native-terminate") or True,
        detach=lambda: events.append("detach"),
    )
    if startup:
        owner = _StartupAttemptCleanup(config, processes, transport, rdbg)
        close = owner.retry_cleanup
    else:
        owner = RuntimeSession(
            RuntimeSessionConfig(config, tmp_path / "evidence"), processes, transport, rdbg,
            SimpleNamespace(close=lambda: None), ArtifactWriter(tmp_path / "evidence", "test"),
            heartbeat_interval_s=3600,
        )
        close = owner.close
    with pytest.raises(ProtocolError, match="cleanup failed"):
        close()
    assert events == ["native-terminate", "client-close"]
    close()
    assert events == ["native-terminate", "client-close", "client-close", "detach", "transport-close"]
    assert grace_periods == [3.0, 3.0]
