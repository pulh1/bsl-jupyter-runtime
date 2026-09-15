"""Owned runtime cleanup must follow kernel lifetime, not frontend lifetime."""

import json
import os
import subprocess
import sys
from pathlib import Path
from threading import Event, RLock, Thread, current_thread
from time import monotonic
from types import SimpleNamespace

from IPython.core.interactiveshell import InteractiveShell
from jupyter_client import KernelManager
from traitlets.config import Config
import pytest

from onec_runtime.runtime_api import RuntimeNamespaceSnapshot
from onec_runtime.errors import ProtocolError, StaleCaptureError
from onec_runtime.recovery_journal import RecoveryJournal
from onec_runtime.runtime_api import PrototypeRuntimeApi
from onec_runtime.worker_universe import (
    WorkerModuleArtifactBuilder,
    WorkerModuleArtifactCache,
    WorkerUniverseState,
)
from onec_runtime_jupyter import InteractiveRuntimeSession, install_runtime
from onec_runtime_jupyter import session as session_module

from test_capture_control_plane import (
    ShutdownBlockingCaptureSession,
    attempt_shutdown_submission,
    observe_shutdown_control_plane,
    start_shutdown_evaluation,
)
from test_prototype_runtime import captured_controller
from test_runtime_api import (
    _UniverseInstructionExecutor,
    _common_module_catalog,
    _notebook_worker_builder,
    _worker_module_unit,
)


class RuntimeResource:
    """Replace the external 1C resource while exercising real adapter hooks."""

    def __init__(self) -> None:
        self.closes = 0
        self.fail = False

    def namespace_snapshot(self):
        return RuntimeNamespaceSnapshot(1, 1, ())

    def require_public_value_handle(self, handle):
        pass

    def close(self):
        self.closes += 1
        if self.fail:
            raise RuntimeError("secret token=private-connection")


class _ShutdownHeartbeat:
    def join(self, timeout: float) -> None:
        assert timeout == 2.0


class _ShutdownProcesses:
    def __init__(self, timeline: list[str]) -> None:
        self.timeline = timeline
        self.closed = Event()

    def close(self, **_options: object) -> None:
        self.timeline.append("processes_closed")
        self.closed.set()


class _ShutdownTransport:
    def __init__(self, rdbg: ShutdownBlockingCaptureSession) -> None:
        self.rdbg = rdbg

    def close(self) -> None:
        self.rdbg.invalidate()
        self.rdbg.shutdown_timeline.append("transport_closed")


class _ServerShutdownCaptureSession(ShutdownBlockingCaptureSession):
    def terminate_bound_server_session(self) -> bool:
        self.shutdown_timeline.append("server_termination_requested")
        return True

    def detach(self) -> None:
        self.shutdown_timeline.append("debug_ui_detached")


class _FailFirstAbandonedJournal(RecoveryJournal):
    def __init__(self) -> None:
        super().__init__()
        self.abandoned_attempts = 0

    def record(
        self,
        stream: str,
        event: str,
        **fields: object,
    ):  # type: ignore[no-untyped-def]
        if event == "capture_evaluation_shutdown_abandoned":
            self.abandoned_attempts += 1
            if self.abandoned_attempts == 1:
                raise OSError("private transient shutdown journal failure")
        return super().record(stream, event, **fields)


def _shutdown_runtime_session(
    rdbg: ShutdownBlockingCaptureSession,
    api: PrototypeRuntimeApi,
    *,
    server: bool,
):  # type: ignore[no-untyped-def]
    runtime = object.__new__(session_module.RuntimeSession)
    runtime.config = SimpleNamespace(
        chunk_size=128,
        runtime=SimpleNamespace(is_server_infobase=server),
    )
    runtime.runtime_api = api
    runtime._operation_lock = RLock()
    runtime._close_lock = RLock()
    runtime._closed = False
    runtime._runtime_api_closed = False
    runtime._transport_closed = False
    runtime._processes_closed = False
    runtime._server_session_terminated = not server
    runtime._debug_ui_detached = not server
    runtime._native_client_termination_requested = False
    runtime._rdbg = rdbg
    runtime._transport = _ShutdownTransport(rdbg)
    runtime._processes = _ShutdownProcesses(rdbg.shutdown_timeline)
    runtime._heartbeat_stop = Event()
    runtime._heartbeat_thread = _ShutdownHeartbeat()
    return runtime


def _real_worker_shutdown_api(
    tmp_path: Path,
    controller: object,
    journal: RecoveryJournal,
) -> tuple[PrototypeRuntimeApi, _UniverseInstructionExecutor, object]:
    """Publish two real Worker registrations owned by the RuntimeApi."""

    target = _UniverseInstructionExecutor()
    builder = _notebook_worker_builder(tmp_path)
    catalog = _common_module_catalog("МодульА", "МодульБ")
    api = PrototypeRuntimeApi(
        controller,  # type: ignore[arg-type]
        journal=journal,
        notebook_worker_builder=builder,
        worker_module_builder=WorkerModuleArtifactBuilder(
            builder,
            cache=WorkerModuleArtifactCache(),
            packer_version="worker-epf-v1",
            target_profile=catalog.profile,
        ),
        worker_instruction_executor=target,
    )
    handle = api.load_worker_modules(
        (
            _worker_module_unit("МодульА", 17, catalog),
            _worker_module_unit("МодульБ", 17, catalog),
        ),
        common_modules=catalog,
    )
    return api, target, handle


def _start_shutdown_thread(invoke, timeline: list[str]):  # type: ignore[no-untyped-def]
    finished = Event()
    errors: list[BaseException] = []

    def run() -> None:
        try:
            invoke()
        except BaseException as error:
            errors.append(error)
        finally:
            timeline.append("session_close_returned")
            finished.set()

    thread = Thread(
        target=run,
        name="jupyter-runtime-shutdown-caller",
        daemon=True,
    )
    thread.start()
    return thread, finished, errors


def _start_lock_holder(lock: RLock):  # type: ignore[valid-type, no-untyped-def]
    acquired = Event()
    release = Event()
    errors: list[BaseException] = []

    def hold() -> None:
        try:
            with lock:
                acquired.set()
                assert release.wait(2), "shutdown test did not release the operation lock"
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=hold, name="runtime-session-operation-lock-holder")
    thread.start()
    assert acquired.wait(1), "operation lock holder did not start"
    return thread, release, errors


class _ObservedOperationLock:
    def __init__(self) -> None:
        self._lock = RLock()
        self.supervisor_waiting = Event()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if current_thread().name == "onec-runtime-supervised-close":
            self.supervisor_waiting.set()
        return self._lock.acquire(blocking, timeout)

    def release(self) -> None:
        self._lock.release()

    def __enter__(self) -> "_ObservedOperationLock":
        self.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


def start_owned(monkeypatch, shell, runtime):
    monkeypatch.setattr(session_module.RuntimeSession, "start", lambda config, *, progress: runtime)
    return InteractiveRuntimeSession.start(object(), shell=shell)


def test_start_replaces_owned_session_and_shutdown_closes_current_once(monkeypatch):
    shell = InteractiveShell()
    first, second = RuntimeResource(), RuntimeResource()
    owners = [start_owned(monkeypatch, shell, first), start_owned(monkeypatch, shell, second)]
    try:
        assert first.closes == 1
        assert second.closes == 0
        shell.exit_now = True
        assert first.closes == second.closes == 1
        for owner in owners:
            owner.close()
        assert first.closes == second.closes == 1
    finally:
        for owner in owners:
            owner.close()


def test_replacement_closes_previous_before_starting_next(monkeypatch):
    shell = InteractiveShell()
    first, second = RuntimeResource(), RuntimeResource()
    owners = [start_owned(monkeypatch, shell, first)]
    try:
        def start_next(config, *, progress):
            assert first.closes == 1
            return second

        monkeypatch.setattr(session_module.RuntimeSession, "start", start_next)
        owners.append(InteractiveRuntimeSession.start(object(), shell=shell))
        assert second.closes == 0
    finally:
        for owner in owners:
            owner.close()


def test_failed_previous_close_prevents_replacement_start(monkeypatch):
    shell = InteractiveShell()
    first = RuntimeResource()
    owner = start_owned(monkeypatch, shell, first)
    first.fail = True
    try:
        def unexpected_start(config, *, progress):
            pytest.fail("replacement started before previous cleanup succeeded")

        monkeypatch.setattr(session_module.RuntimeSession, "start", unexpected_start)
        with pytest.raises(RuntimeError, match="secret token"):
            InteractiveRuntimeSession.start(object(), shell=shell)
        assert first.closes == 1
    finally:
        first.fail = False
        owner.close()


def test_start_in_another_shell_keeps_first_session_open(monkeypatch):
    first, second = RuntimeResource(), RuntimeResource()
    owners = [start_owned(monkeypatch, InteractiveShell(), first)]
    try:
        owners.append(start_owned(monkeypatch, InteractiveShell(), second))
        assert first.closes == second.closes == 0
    finally:
        for owner in owners:
            owner.close()


def test_replacement_does_not_close_externally_installed_runtime(monkeypatch):
    shell = InteractiveShell()
    external, owned = RuntimeResource(), RuntimeResource()
    install_runtime(shell, external)
    owner = start_owned(monkeypatch, shell, owned)
    try:
        assert external.closes == 0
        assert owned.closes == 0
    finally:
        owner.close()


def test_failed_new_start_leaves_previous_owner_closed(monkeypatch):
    shell = InteractiveShell()
    previous = RuntimeResource()
    owner = start_owned(monkeypatch, shell, previous)
    try:
        def fail_start(config, *, progress):
            raise RuntimeError("new start failed")

        monkeypatch.setattr(session_module.RuntimeSession, "start", fail_start)
        with pytest.raises(RuntimeError, match="new start failed"):
            InteractiveRuntimeSession.start(object(), shell=shell)
        assert previous.closes == 1
        shell.exit_now = True
        assert previous.closes == 1
    finally:
        owner.close()


def test_explicit_close_unregisters_shutdown_cleanup(monkeypatch):
    shell, runtime = InteractiveShell(), RuntimeResource()
    with start_owned(monkeypatch, shell, runtime):
        pass
    shell.exit_now = True
    assert runtime.closes == 1


def test_shell_shutdown_uses_runtime_shutdown_cleanup_before_ordinary_close(monkeypatch):
    class ShutdownResource(RuntimeResource):
        def __init__(self) -> None:
            super().__init__()
            self.shutdown_closes = 0

        def close_for_kernel_shutdown(self) -> None:
            self.shutdown_closes += 1

    shell, runtime = InteractiveShell(), ShutdownResource()
    owner = start_owned(monkeypatch, shell, runtime)
    shell.exit_now = True
    assert runtime.shutdown_closes == 1
    assert runtime.closes == 0
    owner.close()
    assert runtime.shutdown_closes == 1
    assert runtime.closes == 0


def test_packaged_jupyter_config_allows_server_session_cleanup_to_finish():
    config_path = (
        Path(__file__).resolve().parents[2]
        / "packages/jupyter/jupyter-config/onec-bsl.json"
    )
    manager = KernelManager(
        config=Config(json.loads(config_path.read_text(encoding="utf-8")))
    )
    # jupyter_client sends SIGTERM halfway through this budget.
    assert manager.shutdown_wait_time >= 120


def test_external_runtime_is_not_automatically_owned():
    shell, runtime = InteractiveShell(), RuntimeResource()
    install_runtime(shell, runtime)
    wrapper = InteractiveRuntimeSession(runtime)
    shell.exit_now = True
    assert runtime.closes == 0
    wrapper.close()
    assert runtime.closes == 1


def test_shutdown_failure_is_safe_and_retryable(monkeypatch, caplog):
    shell = InteractiveShell()
    failed = RuntimeResource()
    failed.fail = True
    owner = start_owned(monkeypatch, shell, failed)
    try:
        shell.exit_now = True
        assert failed.closes == 1
        assert caplog.records
        assert "cleanup" in caplog.text.lower()
        assert "secret" not in caplog.text
        assert "private-connection" not in caplog.text
        failed.fail = False
        owner.close()
        owner.close()
        assert failed.closes == 2
    finally:
        failed.fail = False
        owner.close()


def test_guardian_stops_only_after_runtime_cleanup_succeeds(monkeypatch):
    shell, runtime = InteractiveShell(), RuntimeResource()
    stopped: list[str] = []
    monkeypatch.setattr(
        session_module, "start_guardian",
        lambda _runtime: type("Guard", (), {"stop": lambda self: stopped.append("stop")})(),
        raising=False,
    )
    owner = start_owned(monkeypatch, shell, runtime)
    runtime.fail = True
    with pytest.raises(RuntimeError, match="secret token"):
        owner.close()
    assert stopped == []
    runtime.fail = False
    owner.close()
    assert stopped == ["stop"]


def test_guardian_start_failure_closes_new_runtime(monkeypatch):
    shell, runtime = InteractiveShell(), RuntimeResource()
    monkeypatch.setattr(
        session_module, "start_guardian",
        lambda _runtime: (_ for _ in ()).throw(RuntimeError("guardian unavailable")),
        raising=False,
    )
    monkeypatch.setattr(session_module.RuntimeSession, "start", lambda config, *, progress: runtime)
    with pytest.raises(RuntimeError, match="guardian unavailable"):
        InteractiveRuntimeSession.start(object(), shell=shell)
    assert runtime.closes == 1


def test_runtime_session_close_joins_pending_capture_consumer_before_return() -> None:
    rdbg = ShutdownBlockingCaptureSession(
        wake_on_invalidate=True,
        gate_invalidation=True,
    )
    journal = RecoveryJournal()
    controller = captured_controller(
        rdbg,
        command_timeout_s=0.05,
        journal=journal,
    )
    api = PrototypeRuntimeApi(controller, journal=journal)
    owner, _ticket, _cleanup_probe = start_shutdown_evaluation(controller, rdbg)
    join_timeouts = observe_shutdown_control_plane(owner, rdbg.shutdown_timeline)
    runtime = _shutdown_runtime_session(rdbg, api, server=False)
    holder, release_operation, holder_errors = _start_lock_holder(
        runtime._operation_lock
    )
    closer, finished, errors = _start_shutdown_thread(
        runtime.close,
        rdbg.shutdown_timeline,
    )
    caught: BaseException | None = None
    try:
        invalidation_while_operation_owned = rdbg.shutdown_invalidate_entered.wait(0.4)
        timeline_before_operation_release = tuple(rdbg.shutdown_timeline)
        caught = attempt_shutdown_submission(owner)
        rdbg.shutdown_allow_invalidate.set()
        release_operation.set()
        finished_in_deadline = finished.wait(0.6)
        close_join_timeouts = tuple(join_timeouts)
        timeline_before_rescue = tuple(rdbg.shutdown_timeline)
    finally:
        rdbg.shutdown_allow_invalidate.set()
        rdbg.shutdown_poll_release.set()
        release_operation.set()
        holder.join(2)
        owner.begin_close()
        assert owner.join(2)
        closer.join(2)

    assert finished_in_deadline, "RuntimeSession.close exceeded the configured deadline"
    assert not errors
    assert not holder_errors
    assert invalidation_while_operation_owned
    assert timeline_before_operation_release[:2] == (
        "coordinator_closing_entered",
        "transport_invalidated",
    )
    assert isinstance(caught, StaleCaptureError)
    assert close_join_timeouts
    assert all(0 <= timeout <= 0.05 for timeout in close_join_timeouts)
    assert timeline_before_rescue.index("transport_invalidated") < (
        timeline_before_rescue.index("event_consumer_join_entered")
    )
    assert timeline_before_rescue.index("event_consumer_join_returned_true") < (
        timeline_before_rescue.index("pin_quarantine")
    )
    assert timeline_before_rescue.index("pin_quarantine") < (
        timeline_before_rescue.index("session_close_returned")
    )
    assert rdbg.shutdown_cleanup_dispatches == 0
    assert timeline_before_rescue.count("pin_quarantine") == 1
    assert not holder.is_alive()
    assert not closer.is_alive()


@pytest.mark.parametrize(
    ("entry", "server"),
    (("normal", False), ("kernel", True)),
)
def test_runtime_session_close_is_bounded_while_operation_lock_stays_owned(
    entry: str,
    server: bool,
) -> None:
    rdbg = (
        _ServerShutdownCaptureSession(wake_on_invalidate=True)
        if server
        else ShutdownBlockingCaptureSession(wake_on_invalidate=True)
    )
    journal = RecoveryJournal()
    controller = captured_controller(
        rdbg,
        command_timeout_s=0.05,
        journal=journal,
    )
    api = PrototypeRuntimeApi(controller, journal=journal)
    owner, _ticket, cleanup_probe = start_shutdown_evaluation(controller, rdbg)
    runtime = _shutdown_runtime_session(rdbg, api, server=server)
    invocation = (
        runtime.close
        if entry == "normal"
        else runtime.close_for_kernel_shutdown
    )
    holder, release_operation, holder_errors = _start_lock_holder(
        runtime._operation_lock
    )
    closer, finished, errors = _start_shutdown_thread(
        invocation,
        rdbg.shutdown_timeline,
    )
    try:
        assert finished.wait(0.3), (
            "RuntimeSession shutdown waited indefinitely for its operation lock"
        )
        assert not errors
        assert not holder_errors
        assert holder.is_alive(), "test released the operation lock before deadline"
        assert owner.join(0.01), "CAPTURE owner was not joined before bounded return"
        assert cleanup_probe.dispositions == [
            (cleanup_probe.lease_identity, "quarantine")
        ]
        assert runtime._processes.closed.is_set() is False

        # Cleanup is supervised after the bounded caller returns. Releasing
        # the operation lock is teardown, not a rescue for the close result.
        release_operation.set()
        assert runtime._processes.closed.wait(1), (
            "deferred Session owner did not finish cleanup"
        )
    finally:
        release_operation.set()
        rdbg.shutdown_poll_release.set()
        holder.join(2)
        owner.begin_close()
        assert owner.join(2)
        closer.join(2)

    assert not holder.is_alive()
    assert not closer.is_alive()


@pytest.mark.parametrize(("entry", "server"), (("normal", False), ("kernel", True)))
def test_repeated_close_stays_bounded_while_supervisor_waits_for_operation_lock(
    entry: str,
    server: bool,
) -> None:
    rdbg = (
        _ServerShutdownCaptureSession(wake_on_invalidate=True)
        if server
        else ShutdownBlockingCaptureSession(wake_on_invalidate=True)
    )
    journal = RecoveryJournal()
    controller = captured_controller(
        rdbg,
        command_timeout_s=0.05,
        journal=journal,
    )
    api = PrototypeRuntimeApi(controller, journal=journal)
    owner, _ticket, _cleanup_probe = start_shutdown_evaluation(controller, rdbg)
    runtime = _shutdown_runtime_session(rdbg, api, server=server)
    runtime._operation_lock = _ObservedOperationLock()
    invocation = (
        runtime.close
        if entry == "normal"
        else runtime.close_for_kernel_shutdown
    )
    holder, release_operation, holder_errors = _start_lock_holder(
        runtime._operation_lock
    )
    first, first_finished, first_errors = _start_shutdown_thread(
        invocation,
        rdbg.shutdown_timeline,
    )
    second: Thread | None = None
    try:
        assert first_finished.wait(0.3), "first close exceeded its deadline"
        assert runtime._operation_lock.supervisor_waiting.wait(0.3), (
            "supervisor did not enter the real operation-lock wait"
        )
        second, second_finished, second_errors = _start_shutdown_thread(
            invocation,
            rdbg.shutdown_timeline,
        )
        second_bounded = second_finished.wait(0.3)
    finally:
        release_operation.set()
        rdbg.shutdown_poll_release.set()
        holder.join(2)
        first.join(2)
        if second is not None:
            second.join(2)
        owner.begin_close()
        assert owner.join(2)

    assert second_bounded, "retry close blocked behind the supervised owner"
    assert first_errors == []
    assert second_errors == []
    assert holder_errors == []
    assert not holder.is_alive()
    assert not first.is_alive()
    assert second is not None and not second.is_alive()


@pytest.mark.parametrize("entry", ("normal", "kernel"))
def test_interactive_owner_retains_guardian_and_hooks_until_core_close_finishes(
    entry: str,
) -> None:
    rdbg = _ServerShutdownCaptureSession(wake_on_invalidate=True)
    journal = RecoveryJournal()
    controller = captured_controller(
        rdbg,
        command_timeout_s=0.05,
        journal=journal,
    )
    api = PrototypeRuntimeApi(controller, journal=journal)
    capture_owner, _ticket, _cleanup_probe = start_shutdown_evaluation(
        controller,
        rdbg,
    )
    runtime = _shutdown_runtime_session(rdbg, api, server=True)
    stopped: list[str] = []
    guardian = SimpleNamespace(stop=lambda: stopped.append("stopped"))
    wrapper = InteractiveRuntimeSession(runtime, guardian)
    shell = InteractiveShell()
    wrapper._register_shutdown(shell)
    holder, release_operation, holder_errors = _start_lock_holder(
        runtime._operation_lock
    )
    invocation = wrapper.close if entry == "normal" else wrapper._close_at_shutdown
    closer, finished, errors = _start_shutdown_thread(
        invocation,
        rdbg.shutdown_timeline,
    )
    try:
        assert finished.wait(0.3), "interactive close exceeded its deadline"
        assert runtime._closed is False
        assert wrapper._closed is False
        assert stopped == []
        assert wrapper._shutdown_shell is shell

        release_operation.set()
        assert runtime._processes.closed.wait(1)
        deadline = 100
        while not runtime._closed and deadline:
            Event().wait(0.01)
            deadline -= 1
        assert runtime._closed is True
        wrapper.close()
    finally:
        release_operation.set()
        rdbg.shutdown_poll_release.set()
        holder.join(2)
        closer.join(2)
        capture_owner.begin_close()
        assert capture_owner.join(2)
        if not wrapper._closed:
            wrapper.close()

    assert errors == []
    assert holder_errors == []
    assert wrapper._closed is True
    assert stopped == ["stopped"]
    assert wrapper._shutdown_shell is None


@pytest.mark.parametrize("first_entry", ("normal", "kernel"))
@pytest.mark.parametrize("retry_entry", ("normal", "kernel"))
def test_late_worker_exit_keeps_abandoned_shutdown_retryable(
    first_entry: str,
    retry_entry: str,
) -> None:
    rdbg = _ServerShutdownCaptureSession(wake_on_invalidate=False)
    journal = _FailFirstAbandonedJournal()
    controller = captured_controller(
        rdbg,
        command_timeout_s=0.05,
        journal=journal,
    )
    api = PrototypeRuntimeApi(controller, journal=journal)
    capture_owner, ticket, cleanup_probe = start_shutdown_evaluation(
        controller,
        rdbg,
    )
    evaluation_id = ticket.evaluation_id
    del ticket
    runtime = _shutdown_runtime_session(rdbg, api, server=True)
    stopped: list[str] = []
    wrapper = InteractiveRuntimeSession(
        runtime,
        SimpleNamespace(stop=lambda: stopped.append("stopped")),
    )
    shell = InteractiveShell()
    wrapper._register_shutdown(shell)

    try:
        if first_entry == "normal":
            with pytest.raises(
                ProtocolError,
                match="ZUP demo cleanup failed: ProtocolError",
            ) as caught:
                wrapper.close()
            assert "private transient shutdown journal failure" not in (
                str(caught.value) + repr(caught.value)
            )
        else:
            wrapper._close_at_shutdown()

        assert runtime.is_closed is False
        assert runtime._processes_closed is True
        assert runtime._debug_ui_detached is True
        assert runtime._transport_closed is True
        assert runtime._runtime_api_closed is False
        assert api._capture_shutdown_finished is False
        assert api._closed is False
        assert wrapper._closed is False
        assert stopped == []
        assert wrapper._shutdown_shell is shell
        assert journal.abandoned_attempts == 1
        assert not any(
            event.event.startswith("capture_evaluation_shutdown_")
            for event in journal.events
        )

        rdbg.shutdown_poll_release.set()
        assert capture_owner.join(1), "coordinator worker did not exit after rescue"

        if retry_entry == "normal":
            wrapper.close()
        else:
            wrapper._close_at_shutdown()

        assert runtime.is_closed is True
        assert runtime._runtime_api_closed is True
        assert api._capture_shutdown_finished is True
        assert api._closed is True
        assert wrapper._closed is True
        assert stopped == ["stopped"]
        assert wrapper._shutdown_shell is None
        assert journal.abandoned_attempts == 2
        abandoned = [
            event
            for event in journal.events
            if event.event == "capture_evaluation_shutdown_abandoned"
        ]
        assert len(abandoned) == 1
        assert abandoned[0].fields == {
            "evaluation_id": evaluation_id,
            "evaluation_kind": "user_bsl",
            "termination_proven": False,
            "elapsed_ms": abandoned[0].fields["elapsed_ms"],
            "pin_disposition": "retained",
            "cleanup_disposition": "retained",
            "cleanup_lease_count": 1,
        }
        assert type(abandoned[0].fields["elapsed_ms"]) is int
        assert 0 <= abandoned[0].fields["elapsed_ms"] <= 0x7FFFFFFF
        assert not any(
            event.event == "capture_evaluation_shutdown_disposed"
            for event in journal.events
        )
        assert cleanup_probe.dispositions == []
        assert cleanup_probe.is_product_owned
        assert rdbg.shutdown_cleanup_dispatches == 0
        assert "pin_release" not in rdbg.shutdown_timeline
        assert "pin_quarantine" not in rdbg.shutdown_timeline
        for private in rdbg.shutdown_private_values:
            assert private not in repr(abandoned[0])

        wrapper.close()
        wrapper._close_at_shutdown()
        assert stopped == ["stopped"]
        assert journal.abandoned_attempts == 2
        assert cleanup_probe.dispositions == []
    finally:
        rdbg.shutdown_poll_release.set()
        capture_owner.begin_close()
        assert capture_owner.join(2)
        if not runtime.is_closed:
            try:
                runtime.close_for_kernel_shutdown()
            except ProtocolError:
                pass
        if not wrapper._closed:
            wrapper.close()


@pytest.mark.parametrize("first_entry", ("normal", "kernel"))
@pytest.mark.parametrize("retry_entry", ("normal", "kernel"))
def test_unproven_close_finalizes_real_worker_ownership(
    tmp_path: Path,
    first_entry: str,
    retry_entry: str,
) -> None:
    """Break caught: admission closure cannot stand in for Worker teardown."""

    rdbg = _ServerShutdownCaptureSession(wake_on_invalidate=False)
    journal = RecoveryJournal()
    controller = captured_controller(
        rdbg,
        command_timeout_s=0.05,
        journal=journal,
    )
    api, target, handle = _real_worker_shutdown_api(tmp_path, controller, journal)
    host = api._worker_universe
    target_registry = api._worker_universe_target
    registration_keys = tuple(sorted(target_registry._registrations, key=str.casefold))
    assert len(registration_keys) == 2
    assert host.state is WorkerUniverseState.READY
    assert {name: host.registration_refcount(name) for name in registration_keys} == {
        name: 1 for name in registration_keys
    }
    pin = api._pin_capture_evaluation_locked()
    assert pin is not None and pin.handle is handle
    pin_lease = api._detach_capture_evaluation_pin_locked()
    assert api._evaluation_generation_pin is None
    assert set(host._leases) == {pin.lease_id}
    assert {name: host.registration_refcount(name) for name in registration_keys} == {
        name: 2 for name in registration_keys
    }
    target_calls_before_close = tuple(target.sources)
    capture_owner, ticket, cleanup_probe = start_shutdown_evaluation(
        controller,
        rdbg,
        pin_lease=pin_lease,
    )
    evaluation_id = ticket.evaluation_id
    del ticket, pin_lease
    runtime = _shutdown_runtime_session(rdbg, api, server=True)
    stopped: list[str] = []
    wrapper = InteractiveRuntimeSession(
        runtime,
        SimpleNamespace(stop=lambda: stopped.append("stopped")),
    )
    shell = InteractiveShell()
    wrapper._register_shutdown(shell)
    invoke = wrapper.close if first_entry == "normal" else wrapper._close_at_shutdown
    retry = wrapper.close if retry_entry == "normal" else wrapper._close_at_shutdown

    try:
        invoke()

        assert runtime.is_closed is True
        assert api._capture_shutdown_finished is True
        assert api._capture_shutdown_termination_proven is False
        assert capture_owner.join(0) is False, "the unresponsive poll unexpectedly exited"
        assert host.state is WorkerUniverseState.CLOSED
        assert host._leases == {}
        assert host._registration_refcounts == {}
        assert target_registry._registrations == {}
        assert target_registry._broken is True
        assert api._worker_generation_handle is None
        assert api._api_owned_worker_generation_handle is None
        assert api._operation_generation_pin is None
        assert api._preparing_generation_pin is None
        assert api._evaluation_generation_pin is None
        assert api._worker_module_artifacts == {}
        assert api._worker_active_modules == {}
        assert tuple(target.sources) == target_calls_before_close
        assert target.disconnects == []
        assert cleanup_probe.dispositions == []
        assert rdbg.shutdown_cleanup_dispatches == 0
        abandoned = [
            event
            for event in journal.events
            if event.event == "capture_evaluation_shutdown_abandoned"
        ]
        assert len(abandoned) == 1
        assert abandoned[0].fields["evaluation_id"] == evaluation_id
        assert abandoned[0].fields["evaluation_kind"] == "user_bsl"
        assert abandoned[0].fields["termination_proven"] is False
        assert abandoned[0].fields["pin_disposition"] == "retained"
        assert abandoned[0].fields["cleanup_disposition"] == "retained"
        assert abandoned[0].fields["cleanup_lease_count"] == 1
        assert type(abandoned[0].fields["elapsed_ms"]) is int
        for private in rdbg.shutdown_private_values:
            assert private not in repr(abandoned[0])
        assert wrapper._closed is True
        assert stopped == ["stopped"]
        assert wrapper._shutdown_shell is None

        retry()
        api.close()
        assert host.state is WorkerUniverseState.CLOSED
        assert host._leases == {}
        assert target_registry._registrations == {}
        assert tuple(target.sources) == target_calls_before_close
        assert len(
            [
                event
                for event in journal.events
                if event.event == "capture_evaluation_shutdown_abandoned"
            ]
        ) == 1
        assert stopped == ["stopped"]
    finally:
        rdbg.shutdown_poll_release.set()
        capture_owner.begin_close()
        assert capture_owner.join(2)
        if not wrapper._closed:
            wrapper.close()


@pytest.mark.parametrize("first_entry", ("normal", "kernel"))
@pytest.mark.parametrize("retry_entry", ("normal", "kernel", "api"))
@pytest.mark.parametrize("late_exit_before_retry", (False, True))
def test_target_death_finalizes_real_worker_after_abandoned_publication_retry(
    tmp_path: Path,
    first_entry: str,
    retry_entry: str,
    late_exit_before_retry: bool,
) -> None:
    """A direct retry must abandon a target Session has already destroyed."""

    rdbg = _ServerShutdownCaptureSession(wake_on_invalidate=False)
    journal = _FailFirstAbandonedJournal()
    controller = captured_controller(
        rdbg,
        command_timeout_s=0.05,
        journal=journal,
    )
    api, target, handle = _real_worker_shutdown_api(tmp_path, controller, journal)
    host = api._worker_universe
    target_registry = api._worker_universe_target
    registration_keys = tuple(sorted(target_registry._registrations, key=str.casefold))
    assert len(registration_keys) == 2
    assert host.state is WorkerUniverseState.READY
    assert {name: host.registration_refcount(name) for name in registration_keys} == {
        name: 1 for name in registration_keys
    }
    pin = api._pin_capture_evaluation_locked()
    assert pin is not None and pin.handle is handle
    pin_lease = api._detach_capture_evaluation_pin_locked()
    assert set(host._leases) == {pin.lease_id}
    assert {name: host.registration_refcount(name) for name in registration_keys} == {
        name: 2 for name in registration_keys
    }
    target_calls_before_close = tuple(target.sources)
    capture_owner, ticket, cleanup_probe = start_shutdown_evaluation(
        controller,
        rdbg,
        pin_lease=pin_lease,
    )
    evaluation_id = ticket.evaluation_id
    del ticket, pin_lease
    runtime = _shutdown_runtime_session(rdbg, api, server=True)
    stopped: list[str] = []
    wrapper = InteractiveRuntimeSession(
        runtime,
        SimpleNamespace(stop=lambda: stopped.append("stopped")),
    )
    shell = InteractiveShell()
    wrapper._register_shutdown(shell)
    first = wrapper.close if first_entry == "normal" else wrapper._close_at_shutdown
    retry = {
        "normal": wrapper.close,
        "kernel": wrapper._close_at_shutdown,
        "api": api.close,
    }[retry_entry]
    worker_released = False

    try:
        if first_entry == "normal":
            with pytest.raises(
                ProtocolError,
                match="ZUP demo cleanup failed: ProtocolError",
            ) as caught:
                first()
            assert "private transient shutdown journal failure" not in (
                str(caught.value) + repr(caught.value)
            )
        else:
            first()

        # The Session has destroyed its target even though publication failed.
        assert runtime.is_closed is False
        assert runtime._server_session_terminated is True
        assert runtime._processes_closed is True
        assert runtime._debug_ui_detached is True
        assert runtime._transport_closed is True
        assert api._capture_shutdown_finished is False
        assert api._data_plane_finalized is False
        assert host.state is WorkerUniverseState.READY
        assert set(host._leases) == {pin.lease_id}
        assert {name: host.registration_refcount(name) for name in registration_keys} == {
            name: 2 for name in registration_keys
        }
        assert len(target_registry._registrations) == 2
        assert target_registry._broken is False
        assert journal.abandoned_attempts == 1
        assert not any(
            event.event.startswith("capture_evaluation_shutdown_")
            for event in journal.events
        )
        assert wrapper._closed is False
        assert stopped == []
        assert wrapper._shutdown_shell is shell

        if late_exit_before_retry:
            rdbg.shutdown_poll_release.set()
            worker_released = True
            assert capture_owner.join(1), "coordinator worker did not exit"

        started = monotonic()
        retry()
        assert monotonic() - started < 1.0, "retry exceeded its shutdown bound"

        if not late_exit_before_retry:
            if retry_entry == "api":
                # This is the P1 sequence: publication succeeds while the
                # worker is alive, then the cached false join result must not
                # prevent a later direct API retry from finishing locally.
                assert api._capture_shutdown_finished is True
                assert capture_owner.join(0) is False
            rdbg.shutdown_poll_release.set()
            worker_released = True
            assert capture_owner.join(1), "coordinator worker did not exit"
            api.close()
        else:
            # Every entry remains idempotent after the late worker exit.
            api.close()

        assert journal.abandoned_attempts == 2
        abandoned = [
            event
            for event in journal.events
            if event.event == "capture_evaluation_shutdown_abandoned"
        ]
        assert len(abandoned) == 1
        assert abandoned[0].fields == {
            "evaluation_id": evaluation_id,
            "evaluation_kind": "user_bsl",
            "termination_proven": False,
            "elapsed_ms": abandoned[0].fields["elapsed_ms"],
            "pin_disposition": "retained",
            "cleanup_disposition": "retained",
            "cleanup_lease_count": 1,
        }
        assert type(abandoned[0].fields["elapsed_ms"]) is int
        for private in rdbg.shutdown_private_values:
            assert private not in repr(abandoned[0])
        assert cleanup_probe.dispositions == []
        assert cleanup_probe.is_product_owned
        assert rdbg.shutdown_cleanup_dispatches == 0
        assert tuple(target.sources) == target_calls_before_close
        assert target.disconnects == []

        assert api._capture_shutdown_finished is True
        assert api._data_plane_finalized is True
        assert api._closed is True
        assert host.state is WorkerUniverseState.CLOSED
        assert host._leases == {}
        assert host._registration_refcounts == {}
        assert target_registry._registrations == {}
        assert target_registry._broken is True
        assert api._worker_generation_handle is None
        assert api._api_owned_worker_generation_handle is None
        assert api._operation_generation_pin is None
        assert api._preparing_generation_pin is None
        assert api._evaluation_generation_pin is None
        assert api._worker_module_artifacts == {}
        assert api._worker_generation_diagnostics == {}
        assert api._worker_active_modules == {}
        assert api._prepared_source_units == {}

        # A direct API finalization leaves the Session able to observe the
        # already-complete API axes and terminally release its Jupyter owner.
        if not runtime.is_closed:
            wrapper.close()
        assert runtime.is_closed is True
        assert wrapper._closed is True
        assert stopped == ["stopped"]
        assert wrapper._shutdown_shell is None

        api.close()
        wrapper.close()
        wrapper._close_at_shutdown()
        assert journal.abandoned_attempts == 2
        assert stopped == ["stopped"]
        assert target_registry._broken is True
    finally:
        if not worker_released:
            rdbg.shutdown_poll_release.set()
        capture_owner.begin_close()
        assert capture_owner.join(2)
        if not runtime.is_closed:
            try:
                runtime.close_for_kernel_shutdown()
            except ProtocolError:
                pass
        if not wrapper._closed:
            wrapper.close()


def test_jupyter_shutdown_from_another_thread_joins_pending_capture_consumer() -> None:
    rdbg = _ServerShutdownCaptureSession(wake_on_invalidate=True)
    journal = RecoveryJournal()
    controller = captured_controller(
        rdbg,
        command_timeout_s=0.05,
        journal=journal,
    )
    api = PrototypeRuntimeApi(controller, journal=journal)
    owner, _ticket, _cleanup_probe = start_shutdown_evaluation(controller, rdbg)
    join_timeouts = observe_shutdown_control_plane(owner, rdbg.shutdown_timeline)
    runtime = _shutdown_runtime_session(rdbg, api, server=True)
    interactive = InteractiveRuntimeSession(runtime)
    closer, finished, errors = _start_shutdown_thread(
        interactive._close_at_shutdown,
        rdbg.shutdown_timeline,
    )
    try:
        finished_in_deadline = finished.wait(0.6)
        close_join_timeouts = tuple(join_timeouts)
        timeline_before_rescue = tuple(rdbg.shutdown_timeline)
    finally:
        rdbg.shutdown_poll_release.set()
        owner.begin_close()
        assert owner.join(2)
        closer.join(2)

    assert finished_in_deadline, "Jupyter shutdown exceeded the configured deadline"
    assert not errors
    assert close_join_timeouts
    assert all(0 <= timeout <= 0.05 for timeout in close_join_timeouts)
    assert timeline_before_rescue.index("coordinator_closing_entered") < (
        timeline_before_rescue.index("transport_invalidated")
    )
    assert timeline_before_rescue.index("transport_invalidated") < (
        timeline_before_rescue.index("event_consumer_join_entered")
    )
    assert timeline_before_rescue.index("event_consumer_join_returned_true") < (
        timeline_before_rescue.index("pin_quarantine")
    )
    assert timeline_before_rescue.index("pin_quarantine") < (
        timeline_before_rescue.index("session_close_returned")
    )
    assert rdbg.shutdown_cleanup_dispatches == 0
    assert timeline_before_rescue.count("pin_quarantine") == 1
    assert not closer.is_alive()


def test_failed_prior_guard_is_retried_before_starting_next_runtime(monkeypatch):
    shell, runtime = InteractiveShell(), RuntimeResource()
    events: list[str] = []
    monkeypatch.setattr(
        session_module, "recover_failed_guards",
        lambda _config: events.append("recover") or (),
        raising=False,
    )

    def start_next(config, *, progress):
        events.append("start")
        return runtime

    monkeypatch.setattr(session_module.RuntimeSession, "start", start_next)
    owner = InteractiveRuntimeSession.start(object(), shell=shell)
    try:
        assert events == ["recover", "start"]
    finally:
        owner.close()


def environment():
    workspace = Path(__file__).resolve().parents[2]
    result = dict(os.environ)
    result["PYTHONPATH"] = os.pathsep.join(str(workspace / path) for path in (
        "src", "packages/jupyter/src", "packages/mcp/src",
    ))
    result["JUPYTER_PLATFORM_DIRS"] = "1"
    result["PYDEVD_DISABLE_FILE_VALIDATION"] = "1"
    return result


def setup_code(marker: Path, *, close_delay: float = 0) -> str:
    return f'''
from pathlib import Path
from unittest.mock import patch
from onec_runtime.runtime_api import RuntimeNamespaceSnapshot
from onec_runtime_jupyter import InteractiveRuntimeSession
from IPython.core.interactiveshell import InteractiveShell
class Resource:
    def namespace_snapshot(self):
        return RuntimeNamespaceSnapshot(1, 1, ())
    def require_public_value_handle(self, handle):
        pass
    def close(self):
        import time
        time.sleep({close_delay!r})
        with Path({str(marker)!r}).open("a") as output:
            output.write("closed\\n")
with patch("onec_runtime_jupyter.session.RuntimeSession.start", return_value=Resource()):
    owner = InteractiveRuntimeSession.start(object(), shell=InteractiveShell.instance())
'''


def test_normal_python_exit_closes_owned_runtime(tmp_path):
    marker = tmp_path / "closed.txt"
    result = subprocess.run(
        [sys.executable, "-c", setup_code(marker) + "\ndel owner\n"],
        env=environment(), capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert marker.exists(), "normal interpreter exit abandoned the owned runtime"
    assert marker.read_text() == "closed\n"


def test_failed_shell_cleanup_retries_at_python_exit_without_leaking_errors(tmp_path):
    marker = tmp_path / "closed.txt"
    code = setup_code(marker) + '''
original_close = owner.runtime.close
def fail_once():
    owner.runtime.close = original_close
    raise RuntimeError("secret token=private-connection")
owner.runtime.close = fail_once
InteractiveShell.instance().exit_now = True
'''
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=environment(), capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert marker.read_text() == "closed\n"
    assert "cleanup" in result.stderr.lower()
    assert "secret" not in result.stderr
    assert "private-connection" not in result.stderr


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("close_delay", [0, 3.5])
def test_real_kernel_shutdown_closes_before_interpreter_exit(tmp_path, restart, close_delay):
    marker = tmp_path / "closed.txt"
    exit_marker = tmp_path / "closed_before_atexit.txt"
    # The caller owns this deadline: jupyter_client sends SIGTERM halfway
    # through shutdown_wait_time. Give the deliberately slow resource 5 sec.
    # Keep the fast case on the unchanged upstream default as well.
    manager = KernelManager(**({"shutdown_wait_time": 10} if close_delay else {}))
    manager.kernel_spec.argv = [
        sys.executable, "-m", "ipykernel_launcher", "-f", "{connection_file}",
    ]
    manager.start_kernel(env=environment())
    client = manager.blocking_client()
    client.start_channels()
    try:
        client.wait_for_ready(timeout=20)
        code = setup_code(marker, close_delay=close_delay) + f'''
import atexit
atexit.register(lambda: Path({str(exit_marker)!r}).write_text(str(Path({str(marker)!r}).exists())))
'''
        reply = client.execute_interactive(code, timeout=20)
        assert reply["content"]["status"] == "ok", reply
        client.stop_channels()
        assert manager.is_alive()
        assert not marker.exists(), "frontend disconnect must preserve the runtime"
        client = manager.blocking_client()
        client.start_channels()
        client.wait_for_ready(timeout=20)
        reply = client.execute_interactive("assert owner.runtime is not None", timeout=20)
        assert reply["content"]["status"] == "ok", reply
        if restart:
            manager.restart_kernel(now=False)
        else:
            manager.shutdown_kernel(now=False)
        assert marker.exists(), "graceful kernel shutdown abandoned the owned runtime"
        assert marker.read_text() == "closed\n"
        assert exit_marker.read_text() == "True", "cleanup ran too late in atexit"
    finally:
        client.stop_channels()
        if manager.has_kernel:
            manager.shutdown_kernel(now=True)


def test_real_kernel_restart_waits_for_server_shutdown_cleanup(tmp_path):
    marker = tmp_path / "server-closed.txt"
    config_path = (
        Path(__file__).resolve().parents[2]
        / "packages/jupyter/jupyter-config/onec-bsl.json"
    )
    manager = KernelManager(
        config=Config(json.loads(config_path.read_text(encoding="utf-8")))
    )
    manager.kernel_spec.argv = [
        sys.executable, "-m", "ipykernel_launcher", "-f", "{connection_file}",
    ]
    manager.start_kernel(env=environment())
    client = manager.blocking_client()
    client.start_channels()
    try:
        client.wait_for_ready(timeout=20)
        code = setup_code(marker) + f'''
def close_for_kernel_shutdown():
    import time
    time.sleep(3.5)
    Path({str(marker)!r}).write_text("server-terminated\\n")
owner.runtime.close_for_kernel_shutdown = close_for_kernel_shutdown
'''
        reply = client.execute_interactive(code, timeout=20)
        assert reply["content"]["status"] == "ok", reply
        manager.restart_kernel(now=False)
        assert marker.read_text() == "server-terminated\n"
    finally:
        client.stop_channels()
        if manager.has_kernel:
            manager.shutdown_kernel(now=True)
