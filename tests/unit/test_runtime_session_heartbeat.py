"""Real runtime/RDBG synchronization with only external effects replaced."""
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from uuid import UUID
from xml.etree import ElementTree

import httpx
import pytest

from onec_runtime.bsl import WorkerModuleUnit
from onec_runtime.rdbg.session import SessionState
from onec_runtime.rdbg.transport import RdbgTransport
from onec_runtime.rdbg.xml_codec import RDBG_NS
from onec_runtime.session import RuntimeSession
from onec_runtime.worker_universe import WorkerGenerationHandle
from tests.unit.rdbg.test_session import (
    CAPTURE_A, LOCATION, ready_session, started_payload, target_states_payload,
)
from tests.unit.test_extension_session import _Closeable, _worker_unit, session_config


class ModuleApi:
    def __init__(self) -> None:
        self.handle = WorkerGenerationHandle(1, 1, 1, "a" * 64)
        self.active_units: dict[str, WorkerModuleUnit] = {}

    def load_worker_modules(self, units, *, common_modules, **kwargs):
        assert len(units) == 1 and units[0].logical_name == "Probe"
        assert common_modules is not None
        self.active_units.update((unit.logical_name.casefold(), unit) for unit in units)
        return self.handle

    def confirmed_worker_module_units(
        self, handle: WorkerGenerationHandle,
    ) -> tuple[WorkerModuleUnit, ...]:
        assert handle is self.handle and self.active_units
        return tuple(self.active_units[name] for name in sorted(self.active_units))

    def owns_debug_ui_stream(self) -> bool:
        return False


def runtime_session(tmp_path: Path, http, *, breakpoints=()):
    transport = RdbgTransport(
        "127.0.0.1", 12345, client=httpx.Client(transport=httpx.MockTransport(http))
    )
    rdbg = ready_session(transport)
    if breakpoints:
        rdbg.set_breakpoints(breakpoints)
    source = tmp_path / "source"
    (source / "CommonModules").mkdir(parents=True)
    api = ModuleApi()
    session = RuntimeSession(
        replace(session_config(tmp_path), source_root=source),
        _Closeable(), transport, rdbg, api, SimpleNamespace(),
        heartbeat_interval_s=0.02,
    )
    return session, rdbg, api


def test_empty_lease_ping_does_not_hold_public_load_for_seconds(tmp_path: Path) -> None:
    """Break: an empty lease long-poll stalls a load behind the real operation lock."""
    entered, release, loaded, checked = Event(), Event(), Event(), Event()
    requests, results, errors = [], [], []

    def http(request: httpx.Request) -> httpx.Response:
        command = request.url.params["cmd"]
        requests.append((command, request.url.params.get("dbgui")))
        if command == "pingDebugUIParams":
            entered.set()
            # Honor the actual HTTP read wait, but release promptly in failed-test cleanup.
            release.wait(request.extensions["timeout"]["read"])
            raise httpx.ReadTimeout("empty lease response", request=request)
        if command == "getDbgAllTargetStates":
            checked.set()
            return httpx.Response(200, content=target_states_payload())
        assert command == "test"
        return httpx.Response(200)

    session, rdbg, api = runtime_session(tmp_path, http)
    units = (_worker_unit("Probe"),)

    def load() -> None:
        try:
            results.append(session.load_worker_modules(units))
        except BaseException as error:
            errors.append(error)
        finally:
            loaded.set()

    worker = Thread(target=load, name="test-public-load")
    try:
        assert entered.wait(1), "the real heartbeat did not issue its lease ping"
        worker.start()
        assert loaded.wait(0.75), "empty heartbeat long-poll blocked public load"
        assert errors == []
        assert results == [api.handle]
        assert checked.is_set(), "load escaped before heartbeat target verification"
        assert ("pingDebugUIParams", str(rdbg.ui_id)) in requests
    finally:
        release.set()
        session.close()
        if worker.ident is not None:
            worker.join(1)
        session._heartbeat_thread.join(1)
        assert not worker.is_alive()
        assert not session._heartbeat_thread.is_alive()


@pytest.mark.parametrize("first_failure", ["transport", "target-lost"])
def test_next_natural_heartbeat_survives_a_transient_failure(
    tmp_path: Path, first_failure: str,
) -> None:
    """Break: one transport/target-check failure permanently disables keepalives."""
    checked = Event()
    pings = []

    def http(request: httpx.Request) -> httpx.Response:
        command = request.url.params["cmd"]
        if command == "pingDebugUIParams":
            pings.append(request.url.params.get("dbgui"))
            if len(pings) == 1 and first_failure == "transport":
                raise httpx.ConnectError("transient offline failure", request=request)
            return httpx.Response(200)
        if command == "getDbgAllTargetStates":
            if len(pings) == 1:
                return httpx.Response(200, content=(
                    f'<response xmlns="{RDBG_NS}"><result>success</result></response>'.encode()
                ))
            checked.set()
            return httpx.Response(200, content=target_states_payload())
        assert command == "test"
        return httpx.Response(200)

    session, rdbg, api = runtime_session(tmp_path, http)
    try:
        assert checked.wait(1), "the next scheduled keepalive never checked its target"
        assert session.load_worker_modules((_worker_unit("Probe"),)) is api.handle
        assert rdbg.state is SessionState.READY
        assert len(pings) >= 2 and all(ui == str(rdbg.ui_id) for ui in pings)
    finally:
        session.close()
        session._heartbeat_thread.join(1)
        assert not session._heartbeat_thread.is_alive()


def test_heartbeat_target_attachment_and_breakpoints_remain_serialized(tmp_path: Path) -> None:
    """Break: moving heartbeat event/attach/breakpoint processing outside the load lock."""
    target_id = UUID("33333333-3333-3333-3333-333333333333")
    attaching, release, worker_entered, loaded = Event(), Event(), Event(), Event()
    commands, breakpoints, results, errors = [], [], [], []

    def http(request: httpx.Request) -> httpx.Response:
        command = request.url.params["cmd"]
        commands.append(command)
        if command == "pingDebugUIParams":
            payload = started_payload(target_id) if commands.count(command) == 1 else b""
            return httpx.Response(200, content=payload)
        if command == "attachDetachDbgTargets":
            attaching.set()
            assert release.wait(2), "test attachment gate was not released"
        elif command == "setBreakpoints":
            breakpoints.append(request.content)
        elif command == "getDbgAllTargetStates":
            return httpx.Response(200, content=target_states_payload())
        else:
            assert command in ("test", "clearBreakOnNextStatement")
        return httpx.Response(200)

    session, rdbg, api = runtime_session(tmp_path, http, breakpoints=(LOCATION, CAPTURE_A))

    def load() -> None:
        try:
            worker_entered.set()
            results.append(session.load_worker_modules((_worker_unit("Probe"),)))
        except BaseException as error:
            errors.append(error)
        finally:
            loaded.set()

    worker = Thread(target=load, name="test-load-during-target-attach")
    try:
        assert attaching.wait(1)
        worker.start()
        assert worker_entered.wait(1)
        assert not loaded.wait(0.05), "load overlapped serialized target attachment"
        release.set()
        assert loaded.wait(0.75)
        assert errors == [] and results == [api.handle]
        assert rdbg.attached_targets[target_id].target_type == "ManagedClient"
        assert commands[:6] == ["setBreakpoints", "test", "pingDebugUIParams",
                                "clearBreakOnNextStatement", "attachDetachDbgTargets", "setBreakpoints"]
        assert len(breakpoints) == 2
        for payload in breakpoints:
            root = ElementTree.fromstring(payload)
            assert [node.text for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "line"] == ["16", "73"]
    finally:
        release.set()
        session.close()
        if worker.ident is not None:
            worker.join(1)
        session._heartbeat_thread.join(1)
        assert not worker.is_alive()
        assert not session._heartbeat_thread.is_alive()
