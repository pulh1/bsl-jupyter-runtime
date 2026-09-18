"""The public Session must release its admission lock during ticket waits."""

from contextlib import contextmanager, nullcontext
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

from onec_runtime.execution.arbiter import ArbiterBusy
from onec_runtime.session import RuntimeSession

from tests.unit.test_extension_session import session_config


def test_generic_execution_wait_allows_public_namespace_snapshot(tmp_path: Path) -> None:
    entered, release, snapshot_done = Event(), Event(), Event()
    execution_errors: list[BaseException] = []
    snapshot_errors: list[BaseException] = []
    snapshots: list[object] = []

    class TicketApi:
        def __init__(self) -> None:
            self.wait_handoff = nullcontext

        @contextmanager
        def execution_caller_handoff(self, release_session_lock):
            self.wait_handoff = release_session_lock
            try:
                yield
            finally:
                self.wait_handoff = nullcontext

        def execute_bsl(self, _source: str) -> str:
            with self.wait_handoff():
                entered.set()
                assert release.wait(3), "ticket wait was not released"
            return "settled"

        def namespace_snapshot(self) -> str:
            return "current snapshot"

        def owns_debug_ui_stream(self) -> bool:
            return True

    api = TicketApi()
    runtime = RuntimeSession(
        session_config(tmp_path), SimpleNamespace(), SimpleNamespace(),
        SimpleNamespace(target=None), api, SimpleNamespace(),
        heartbeat_interval_s=60.0,
    )

    def execute() -> None:
        try:
            assert runtime.execute_bsl("Результат = 1;") == "settled"
        except BaseException as error:
            execution_errors.append(error)

    def snapshot() -> None:
        try:
            snapshots.append(runtime.namespace_snapshot())
        except BaseException as error:
            snapshot_errors.append(error)
        finally:
            snapshot_done.set()

    caller = Thread(target=execute, name="ticket-waiter")
    observer = Thread(target=snapshot, name="namespace-reader")
    try:
        caller.start()
        assert entered.wait(1), "execution did not reach the ticket wait"
        observer.start()
        assert snapshot_done.wait(0.5), "public snapshot was blocked by ticket wait"
        assert snapshots == ["current snapshot"]
        assert snapshot_errors == []
    finally:
        release.set()
        if caller.ident is not None:
            caller.join(3)
        if observer.ident is not None:
            observer.join(3)
        runtime._heartbeat_stop.set()
        runtime._heartbeat_thread.join(2)
    assert execution_errors == []


def test_runtime_heartbeat_ticket_wait_releases_public_operation_lock(
    tmp_path: Path,
) -> None:
    entered, release = Event(), Event()
    heartbeat_errors: list[BaseException] = []
    direct_calls: list[str] = []

    class Ticket:
        def wait_unknown(self, timeout=None):
            entered.set()
            assert release.wait(3), "heartbeat ticket was not released"
            return False

        def wait_settled(self, timeout=None):
            return {"rtt_ms": 1.0}

    class ArbiterApi:
        def try_heartbeat_ticket(self):
            return Ticket()

        def owns_debug_ui_stream(self) -> bool:
            return False

        def namespace_snapshot(self) -> str:
            return "current snapshot"

    runtime = RuntimeSession(
        session_config(tmp_path), SimpleNamespace(ensure_running=lambda: None),
        SimpleNamespace(),
        SimpleNamespace(target=None, heartbeat=lambda: direct_calls.append("direct")),
        ArbiterApi(), SimpleNamespace(), heartbeat_interval_s=60.0,
    )

    def heartbeat() -> None:
        try:
            runtime._heartbeat_tick()
        except BaseException as error:
            heartbeat_errors.append(error)

    worker = Thread(target=heartbeat, name="heartbeat-caller")
    try:
        worker.start()
        assert entered.wait(1), "runtime did not await the arbiter heartbeat ticket"
        assert runtime.namespace_snapshot() == "current snapshot"
        assert direct_calls == []
    finally:
        release.set()
        if worker.ident is not None:
            worker.join(3)
        runtime._heartbeat_stop.set()
        runtime._heartbeat_thread.join(2)
    assert heartbeat_errors == []


def test_unknown_heartbeat_ticket_does_not_wait_for_impossible_settlement(
    tmp_path: Path,
) -> None:
    unknown_observed, settlement_waited = Event(), Event()

    class Ticket:
        def wait_unknown(self, timeout=None):
            unknown_observed.set()
            return True

        def wait_settled(self, timeout=None):
            settlement_waited.set()
            raise AssertionError("unknown heartbeat cannot settle without reconciliation")

    class ArbiterApi:
        def try_heartbeat_ticket(self):
            return Ticket()

    runtime = RuntimeSession(
        session_config(tmp_path), SimpleNamespace(ensure_running=lambda: None),
        SimpleNamespace(), SimpleNamespace(target=None),
        ArbiterApi(), SimpleNamespace(), heartbeat_interval_s=60.0,
    )
    try:
        runtime._heartbeat_tick()
        assert unknown_observed.is_set()
        assert not settlement_waited.is_set()
    finally:
        runtime._heartbeat_stop.set()
        runtime._heartbeat_thread.join(2)


def test_close_waits_for_bounded_heartbeat_transport_before_closing_owner(
    tmp_path: Path,
) -> None:
    heartbeat_entered, release_heartbeat = Event(), Event()
    heartbeat_done, close_done = Event(), Event()
    close_errors: list[BaseException] = []

    class Ticket:
        def wait_unknown(self, timeout=None):
            heartbeat_entered.set()
            assert release_heartbeat.wait(4)
            heartbeat_done.set()
            return False

        def wait_settled(self, timeout=None):
            if not heartbeat_done.is_set():
                heartbeat_entered.set()
                assert release_heartbeat.wait(4)
                heartbeat_done.set()
            return {"rtt_ms": 1.0}

    class ArbiterApi:
        def __init__(self) -> None:
            self.scheduled = False

        def try_heartbeat_ticket(self):
            if self.scheduled:
                return None
            self.scheduled = True
            return Ticket()

        def close(self) -> None:
            if not heartbeat_done.is_set():
                raise ArbiterBusy("heartbeat still owns RDBG")

    runtime = RuntimeSession(
        session_config(tmp_path), SimpleNamespace(close=lambda: None),
        SimpleNamespace(close=lambda: None), SimpleNamespace(target=None),
        ArbiterApi(), SimpleNamespace(), heartbeat_interval_s=0.01,
    )
    assert heartbeat_entered.wait(1)

    def close_owner() -> None:
        try:
            runtime.close()
        except BaseException as error:
            close_errors.append(error)
        finally:
            close_done.set()

    closer = Thread(target=close_owner)
    closer.start()
    try:
        assert not close_done.wait(2.1)
    finally:
        release_heartbeat.set()
        closer.join(3)
        if not runtime.is_closed:
            runtime.close()
    assert close_done.is_set()
    assert close_errors == []
    assert runtime.is_closed


def test_generic_capture_resume_wait_allows_public_namespace_snapshot(
    tmp_path: Path,
) -> None:
    entered, release = Event(), Event()
    resume_errors: list[BaseException] = []
    snapshots: list[object] = []

    class TicketApi:
        def __init__(self) -> None:
            self.wait_handoff = nullcontext

        @contextmanager
        def execution_caller_handoff(self, release_session_lock):
            self.wait_handoff = release_session_lock
            try:
                yield
            finally:
                self.wait_handoff = nullcontext

        def resume_capture(self, **_arguments):
            with self.wait_handoff():
                entered.set()
                assert release.wait(3), "resume ticket wait was not released"
            return "resumed"

        def namespace_snapshot(self) -> str:
            return "current snapshot"

        def owns_debug_ui_stream(self) -> bool:
            return True

    runtime = RuntimeSession(
        session_config(tmp_path), SimpleNamespace(), SimpleNamespace(),
        SimpleNamespace(target=None), TicketApi(), SimpleNamespace(),
        heartbeat_interval_s=60.0,
    )

    def resume() -> None:
        try:
            assert runtime.resume_capture() == "resumed"
        except BaseException as error:
            resume_errors.append(error)

    caller = Thread(target=resume, name="resume-ticket-waiter")
    try:
        caller.start()
        assert entered.wait(1), "resume did not reach ticket wait"
        observer = Thread(target=lambda: snapshots.append(runtime.namespace_snapshot()))
        observer.start()
        observer.join(0.5)
        assert not observer.is_alive(), "public snapshot was blocked by resume wait"
        assert snapshots == ["current snapshot"]
    finally:
        release.set()
        if caller.ident is not None:
            caller.join(3)
        runtime._heartbeat_stop.set()
        runtime._heartbeat_thread.join(2)
    assert resume_errors == []


def test_generic_materialization_wait_allows_public_namespace_snapshot(
    tmp_path: Path,
) -> None:
    entered, release = Event(), Event()
    materialization_errors: list[BaseException] = []

    class TicketApi:
        def __init__(self) -> None:
            self.wait_handoff = nullcontext

        @contextmanager
        def execution_caller_handoff(self, release_session_lock):
            self.wait_handoff = release_session_lock
            try:
                yield
            finally:
                self.wait_handoff = nullcontext

        def validate_value_reference(self, _handle: str) -> None:
            pass

        def materialization_kind(self, _handle: str) -> str:
            with self.wait_handoff():
                entered.set()
                assert release.wait(3), "materialization ticket was not released"
            return "table"

        def namespace_snapshot(self) -> str:
            return "current snapshot"

    runtime = RuntimeSession(
        session_config(tmp_path), SimpleNamespace(), SimpleNamespace(),
        SimpleNamespace(target=None), TicketApi(), SimpleNamespace(),
        heartbeat_interval_s=60.0,
    )

    def materialize() -> None:
        try:
            assert runtime.materialization_kind("e1cRuntimeКонтекст.Таблица") == "table"
        except BaseException as error:
            materialization_errors.append(error)

    caller = Thread(target=materialize, name="materialization-ticket-waiter")
    observer: Thread | None = None
    try:
        caller.start()
        assert entered.wait(1), "materialization did not reach ticket wait"
        observer = Thread(target=lambda: runtime.namespace_snapshot())
        observer.start()
        observer.join(0.5)
        assert not observer.is_alive(), "public snapshot was blocked by materialization"
    finally:
        release.set()
        if caller.ident is not None:
            caller.join(3)
        if observer is not None and observer.ident is not None:
            observer.join(3)
        runtime._heartbeat_stop.set()
        runtime._heartbeat_thread.join(2)
    assert materialization_errors == []


def test_generic_debug_resume_wait_allows_public_namespace_snapshot(
    tmp_path: Path,
) -> None:
    entered, release = Event(), Event()
    resume_errors: list[BaseException] = []

    class TicketApi:
        def __init__(self) -> None:
            self.wait_handoff = nullcontext

        @contextmanager
        def execution_caller_handoff(self, release_session_lock):
            self.wait_handoff = release_session_lock
            try:
                yield
            finally:
                self.wait_handoff = nullcontext

        def resume_debug_stop(self, **_arguments):
            with self.wait_handoff():
                entered.set()
                assert release.wait(3), "debug resume ticket was not released"
            return "resumed"

        def namespace_snapshot(self) -> str:
            return "current snapshot"

    runtime = RuntimeSession(
        session_config(tmp_path), SimpleNamespace(), SimpleNamespace(),
        SimpleNamespace(target=None), TicketApi(), SimpleNamespace(),
        heartbeat_interval_s=60.0,
    )

    def resume() -> None:
        try:
            assert runtime.resume_debug_stop() == "resumed"
        except BaseException as error:
            resume_errors.append(error)

    caller = Thread(target=resume, name="debug-resume-ticket-waiter")
    observer: Thread | None = None
    try:
        caller.start()
        assert entered.wait(1), "debug resume did not reach ticket wait"
        observer = Thread(target=lambda: runtime.namespace_snapshot())
        observer.start()
        observer.join(0.5)
        assert not observer.is_alive(), "public snapshot was blocked by debug resume"
    finally:
        release.set()
        if caller.ident is not None:
            caller.join(3)
        if observer is not None and observer.ident is not None:
            observer.join(3)
        runtime._heartbeat_stop.set()
        runtime._heartbeat_thread.join(2)
    assert resume_errors == []
