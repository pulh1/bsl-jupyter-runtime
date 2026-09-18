"""RuntimeSession's public calls may use the one-owner execution facade."""

from pathlib import Path
from dataclasses import replace
from threading import Event, Thread, current_thread
from types import SimpleNamespace

import pytest

from onec_runtime.bsl.source_maps import SourceUnitKind, SourceUnitRef, source_sha256
from onec_runtime.config import RuntimeConfig
from onec_runtime.execution.composition import build_execution_core
from onec_runtime.execution.arbiter import ArbiterBusy
from onec_runtime.execution.namespace import RuntimeNamespaceOwner
from onec_runtime.execution.public_facade import PublicExecutionFacade
from onec_runtime.execution.settlement import RouteSettlementService
from onec_runtime.execution.status_projection import ExecutionStatusProjection
from onec_runtime.execution.worker_activation import WorkerActivationSnapshot
from onec_runtime.prototype_runtime import OperationState
from onec_runtime.runtime_api import RuntimeReplyKind
from onec_runtime.session import RuntimeSession, RuntimeSessionConfig

from test_execution_controller_routes import BUSINESS, KERNEL, CompleteSession


class _Replies:
    def diagnostic_reply(self, diagnostic):
        raise AssertionError(diagnostic.message)

    def unavailable_reply(self, unavailable):
        raise AssertionError(unavailable.reason)


def _config(tmp_path: Path) -> RuntimeSessionConfig:
    platform_bin = tmp_path / "bin"
    platform_bin.mkdir()
    for executable in ("1cv8.exe", "1cv8c.exe", "dbgs.exe"):
        (platform_bin / executable).touch()
    infobase = tmp_path / "base"
    infobase.mkdir()
    (infobase / "1Cv8.1CD").write_bytes(b"synthetic")
    return RuntimeSessionConfig(
        RuntimeConfig(
            workspace=tmp_path / "workspace", platform_bin=platform_bin,
            connection_string=f'File="{infobase}";',
        ),
        evidence_root=tmp_path / "evidence",
    )


def test_public_main_capture_resume_and_heartbeat_use_one_arbiter_owner(tmp_path: Path) -> None:
    class GatedSession(CompleteSession):
        def __init__(self):
            super().__init__()
            self.eval_waiting = Event()
            self.release_eval = Event()

        def wait_evaluation_event(self, pending, *, timeout_s, on_transport_dispatch):
            if self.expression.startswith("RuntimeKernelServer.ВыполнитьКодВКонтекстеОтладки("):
                self.eval_waiting.set()
                assert self.release_eval.wait(5)
            return super().wait_evaluation_event(
                pending, timeout_s=timeout_s, on_transport_dispatch=on_transport_dispatch,
            )

        def heartbeat(self, *, on_transport_dispatch):
            on_transport_dispatch()
            self._record("heartbeat")
            return {"rtt_ms": 1.0}

    session = GatedSession()
    namespace = RuntimeNamespaceOwner(
        1, 1, worker_snapshot=lambda: WorkerActivationSnapshot(0, (), None, None)
    )
    settlement = RouteSettlementService(namespace)
    core = build_execution_core(
        session, KERNEL, runtime_generation=1, capture_locations=(),
        snapshot_provider=namespace.snapshot,
        capture_snapshot_provider=lambda operation: namespace.snapshot(
            speculative_names=settlement.pending_main_names(operation)
        ),
        reply_presenter=_Replies(), settlement_services=settlement,
    )

    projection = ExecutionStatusProjection(
        controller_facts=core.controller.status_facts,
        worker_snapshot=lambda: WorkerActivationSnapshot(0, (), None, None),
        namespace=namespace,
    )

    facade = PublicExecutionFacade(
        core.pipeline, core.controller, core.arbiter,
        source_unit_factory=lambda source: SourceUnitRef(
            SourceUnitKind.NOTEBOOK_CELL, "public-bridge", 1, source_sha256(source)
        ),
        status_reader=projection.status,
        namespace_reader=projection.namespace_snapshot,
    )
    process_checks = []
    runtime = RuntimeSession(
        _config(tmp_path),
        SimpleNamespace(ensure_running=lambda: process_checks.append("checked")),
        SimpleNamespace(), session, facade, SimpleNamespace(),
        heartbeat_interval_s=60.0,
    )
    runtime.configure_capture_points((BUSINESS,))
    capture_reply: list[object] = []
    capture_errors: list[BaseException] = []

    def capture_cell() -> None:
        try:
            capture_reply.append(runtime.execute_bsl("РезультатИнструкции = 2;"))
        except BaseException as error:
            capture_errors.append(error)

    caller = Thread(target=capture_cell, name="capture-notebook-caller")
    try:
        first = runtime.execute_bsl("Результат = 1;")
        assert first.kind is RuntimeReplyKind.CAPTURED
        assert runtime.status().state is OperationState.CAPTURED
        assert runtime.namespace_snapshot().runtime_generation == 1
        caller.start()
        assert session.eval_waiting.wait(5)
        runtime._heartbeat_tick()
        assert not any(name == "heartbeat" for name, _ in session.calls)
        session.release_eval.set()
        caller.join(5)
        assert not caller.is_alive()
        assert capture_errors == []
        assert capture_reply[0].kind is RuntimeReplyKind.CAPTURE_CELL

        completed = runtime.resume_capture()
        assert completed.kind is RuntimeReplyKind.MAIN_COMPLETED
        runtime._heartbeat_tick()
        assert process_checks == ["checked", "checked"]
        assert any(name == "heartbeat" for name, _ in session.calls)
        assert {thread for _, thread in session.calls} == {core.arbiter._worker}
        assert core.arbiter._worker is not current_thread()
    finally:
        session.release_eval.set()
        if caller.ident is not None:
            caller.join(5)
        runtime._heartbeat_stop.set()
        runtime._heartbeat_thread.join(2)
        facade.close()


def test_session_close_keeps_transport_and_target_while_arbiter_is_busy(
    tmp_path: Path,
) -> None:
    closed: list[str] = []

    class BusyFacade:
        def close(self):
            raise ArbiterBusy("remote operation still owns RDBG")

        def owns_debug_ui_stream(self):
            return True

    runtime = RuntimeSession(
        _config(tmp_path),
        SimpleNamespace(close=lambda: closed.append("process")),
        SimpleNamespace(close=lambda: closed.append("transport")),
        SimpleNamespace(target=None), BusyFacade(), SimpleNamespace(),
        heartbeat_interval_s=60.0,
    )
    try:
        with pytest.raises(ArbiterBusy):
            runtime.close()
        assert closed == []
        assert not runtime.is_closed
    finally:
        runtime._heartbeat_stop.set()
        runtime._heartbeat_thread.join(2)


def test_server_close_keeps_target_while_execution_arbiter_is_busy(
    tmp_path: Path,
) -> None:
    closed: list[str] = []

    class BusyFacade(PublicExecutionFacade):
        def __init__(self) -> None:
            pass

        def close(self) -> None:
            raise ArbiterBusy("remote operation still owns RDBG")

    config = _config(tmp_path)
    config = replace(
        config,
        runtime=replace(
            config.runtime,
            connection_string='Srvr="localhost";Ref="runtime_test";',
        ),
    )
    runtime = RuntimeSession(
        config,
        SimpleNamespace(close=lambda: closed.append("process")),
        SimpleNamespace(close=lambda: closed.append("transport")),
        SimpleNamespace(
            target=None,
            terminate_bound_server_session=lambda: closed.append("terminate"),
        ),
        BusyFacade(), SimpleNamespace(), heartbeat_interval_s=60.0,
    )
    try:
        with pytest.raises(ArbiterBusy):
            runtime.close_for_kernel_shutdown()
        assert closed == []
        assert not runtime.is_closed
    finally:
        runtime._heartbeat_stop.set()
        runtime._heartbeat_thread.join(2)
