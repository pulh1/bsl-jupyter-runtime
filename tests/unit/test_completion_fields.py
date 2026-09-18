from threading import RLock, Thread, Event
from time import monotonic, sleep
from types import SimpleNamespace

import pytest

from onec_runtime.capture_evaluation import (
    CaptureEvaluationCoordinator,
    CaptureEvaluationKind,
    CaptureEvaluationTicket,
    CapturePhase,
)
from onec_runtime.errors import (
    BslExecutionError,
    CaptureOutcomeUnknownError,
    CaptureRecoveryRequiredError,
    CaptureValueAccessDeniedError,
    CaptureValueCheckError,
    ProtocolError,
    TargetLost,
)
from onec_runtime.prototype_runtime import OperationState
from onec_runtime.runtime_api import PrototypeRuntimeApi
from onec_runtime.session import RuntimeSession

from test_capture_evaluation_lifecycle import ControlledCaptureSession, close_owner
from test_prototype_runtime import SERVICE, captured_controller


class Controller:
    def __init__(self):
        self.state = OperationState.COMPLETED
        self.runtime_generation = 1
        self.operation_id = 7
        self.command_timeout_s = 30.0
        self.lowerer = SimpleNamespace(persistent_names=("Данные",))
        self.fields = ("Номер", "Название")
        self.calls = []
        self.admission_sources = []
        self.target_requests = []
        self.admission_outcome = "R"
        self.collection_size_override = None
        self.collection_row_limit = None

    def execute_system_inspection(self, source):
        self.admission_sources.append(source)
        self.calls.append((source, self.command_timeout_s))
        self.target_requests.append(("completion", source))
        rows = (
            (self.admission_outcome, "")
            if self.admission_outcome != "R"
            else ("R", "")
        ,) + (() if self.admission_outcome != "R" else tuple(
            ("R", name) for name in self.fields
        ))
        declared_size = (
            len(rows)
            if self.collection_size_override is None
            else self.collection_size_override
        )
        if self.collection_row_limit is not None:
            rows = rows[:self.collection_row_limit]
        wire = "\n".join((
            f"C\t{declared_size}",
            *(f"{outcome}\t{name}" for outcome, name in rows),
        ))
        return wire


def _captured_completion_runtime(
    session: ControlledCaptureSession,
) -> tuple[RuntimeSession, PrototypeRuntimeApi, object]:
    controller = captured_controller(session, command_timeout_s=1)
    api = PrototypeRuntimeApi(controller)
    api._namespace_names = ("Данные",)
    runtime = object.__new__(RuntimeSession)
    runtime.runtime_api = api
    runtime._operation_lock = RLock()
    runtime._closed = False
    return runtime, api, controller


def _completion_wire(
    names: tuple[str, ...] = ("Номер", "Название"),
    *,
    outcome: str = "R",
) -> str:
    rows = ((outcome, ""),) if outcome != "R" else (
        ("R", ""), *(("R", name) for name in names)
    )
    payload = "\n".join((
        f"C\t{len(rows)}",
        *(f"{state}\t{name}" for state, name in rows),
    ))
    return '"' + payload + '"'


def test_captured_session_completion_returns_admitted_schema_in_one_inspection() -> None:
    session = ControlledCaptureSession()
    runtime, api, controller = _captured_completion_runtime(session)
    fields: list[tuple[str, ...]] = []
    errors: list[BaseException] = []

    def complete() -> None:
        try:
            fields.append(runtime.completion_fields("e1cRuntimeКонтекст.Данные"))
        except BaseException as error:
            errors.append(error)

    caller = Thread(target=complete, name="captured-session-completion-ready")
    try:
        caller.start()
        assert session.accepted.wait(1), "completion was not acknowledged"
        assert session.polling.wait(1), "completion was not polled"
        session.complete(_completion_wire(), type_name="Строка")
        caller.join(1)

        assert not caller.is_alive()
        assert fields == [("Номер", "Название")]
        assert errors == []
        assert api.current_capture().status().phase is CapturePhase.PAUSED
        assert controller.state is OperationState.CAPTURED
        assert controller.breakpoint_workspaces[-1].phase == "full-restore"
        sources = [call[1][0] for call in session.calls if call[0] == "start_evaluation"]
        assert len(sources) == 1
        assert "СериализоватьДопущенныеИменаСвойствДляПодсказки(" in sources[0]
        assert "ДопуститьЗначение(" not in sources[0]
        assert "evaluate_collection" not in sources[0]
    finally:
        if session.capture_pending is not None:
            session.complete()
        caller.join(1)
        close_owner(controller, session)


def test_captured_session_completion_interrupt_detaches_only_the_waiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interrupted completion caller leaves its owned inspection observable."""
    session = ControlledCaptureSession()
    runtime, api, controller = _captured_completion_runtime(session)

    def interrupt_after_ack(
        _ticket: CaptureEvaluationTicket,
        timeout_s: float | None = None,
    ) -> object:
        del timeout_s
        assert session.accepted.wait(1), "completion was not acknowledged"
        raise KeyboardInterrupt

    try:
        monkeypatch.setattr(CaptureEvaluationTicket, "wait_initiator", interrupt_after_ack)
        with pytest.raises(KeyboardInterrupt):
            runtime.completion_fields("e1cRuntimeКонтекст.Данные")

        assert runtime._operation_lock.acquire(blocking=False)
        runtime._operation_lock.release()
        status = runtime.current_capture().status()
        assert status.phase is CapturePhase.EVALUATING
        assert status.evaluation_kind is CaptureEvaluationKind.INSPECTION
        assert status.pending_evaluation_id is not None
        assert session.capture_start_count == 1

        session.complete(_completion_wire(), type_name="Строка")
        assert api.current_capture().wait(
            timeout_s=1, evaluation_id=status.pending_evaluation_id
        ).state.value == "completed"
    finally:
        if session.capture_pending is not None:
            session.complete()
        close_owner(controller, session)


@pytest.mark.parametrize(
    ("presentation", "type_name", "error", "expected"),
    (
        (
            _completion_wire(outcome="D|worker_generation_value"),
            "Строка",
            "",
            CaptureValueAccessDeniedError,
        ),
        (
            _completion_wire(outcome="E|value_admission_failed"),
            "Строка",
            "",
            CaptureValueCheckError,
        ),
        ("\"not a completion envelope\"", "Строка", "", ProtocolError),
        ("private platform failure", "Ошибка", "private", BslExecutionError),
    ),
    ids=("denied", "admission_failure", "invalid", "bsl_failure"),
)
def test_captured_session_completion_classifies_confirmed_outcomes(
    presentation: str,
    type_name: str,
    error: str,
    expected: type[BaseException],
) -> None:
    session = ControlledCaptureSession()
    runtime, api, controller = _captured_completion_runtime(session)
    errors: list[BaseException] = []

    def complete() -> None:
        try:
            runtime.completion_fields("e1cRuntimeКонтекст.Данные")
        except BaseException as caught:
            errors.append(caught)

    caller = Thread(target=complete, name="captured-session-completion-outcome")
    try:
        caller.start()
        assert session.accepted.wait(1)
        session.complete(presentation, type_name=type_name, error=error)
        caller.join(1)

        assert not caller.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], expected)
        assert api.current_capture().status().phase is CapturePhase.PAUSED
        assert controller.state is OperationState.CAPTURED
        assert session.capture_start_count == 1
    finally:
        if session.capture_pending is not None:
            session.complete()
        caller.join(1)
        close_owner(controller, session)


def test_captured_session_completion_dispatch_uncertainty_is_not_a_worker_error() -> None:
    session = ControlledCaptureSession(dispatch_error=OSError("transport lost"))
    runtime, api, controller = _captured_completion_runtime(session)
    try:
        with pytest.raises(CaptureOutcomeUnknownError):
            runtime.completion_fields("e1cRuntimeКонтекст.Данные")
        status = api.current_capture().status()
        assert status.phase is CapturePhase.OUTCOME_UNKNOWN
        assert status.evaluation_kind is None
        assert session.capture_start_count == 1
    finally:
        close_owner(controller, session)


def test_captured_session_completion_restore_failure_requires_recovery() -> None:
    session = ControlledCaptureSession(fail_workspace_on_call=3)
    runtime, api, controller = _captured_completion_runtime(session)
    errors: list[BaseException] = []

    def complete() -> None:
        try:
            runtime.completion_fields("e1cRuntimeКонтекст.Данные")
        except BaseException as caught:
            errors.append(caught)

    caller = Thread(target=complete, name="captured-session-completion-restore")
    try:
        caller.start()
        assert session.accepted.wait(1)
        session.complete(_completion_wire(), type_name="Строка")
        caller.join(1)

        assert not caller.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], CaptureRecoveryRequiredError)
        assert api.current_capture().status().phase is CapturePhase.RECOVERY_REQUIRED
        assert session.capture_start_count == 1
    finally:
        if session.capture_pending is not None:
            session.complete()
        caller.join(1)
        close_owner(controller, session)


def test_completion_reads_only_current_schema_without_inferencing_value_types():
    controller = Controller()
    api = PrototypeRuntimeApi(controller)
    assert api.completion_fields("e1cRuntimeКонтекст.Данные", table_row=True) == ("Номер", "Название")
    assert "СериализоватьДопущенныеИменаСвойствДляПодсказки(e1cRuntimeКонтекст.Данные, Истина" in controller.calls[0][0]
    assert 0 < controller.calls[0][1] <= 1.0
    assert controller.command_timeout_s == 30.0
    assert controller.operation_id == 7
    controller.fields = ("ОбновленнаяКолонка",)
    assert api.completion_fields("e1cRuntimeКонтекст.Данные", table_row=True) == ("ОбновленнаяКолонка",)


@pytest.mark.parametrize("handle", [
    "e1cRuntimeКонтекст.Данные[0]", "e1cRuntimeКонтекст.Данные.Удалить()", "e1cRuntimeКонтекст.Данные;Удалить()",
    "e1cRuntimeКонтекст.Несуществующая", "e1cRuntimeКонтекст.RuntimeWorkerActiveGeneration",
])
def test_completion_rejects_unregistered_reserved_and_executable_paths(handle):
    controller = Controller()
    with pytest.raises(ProtocolError):
        PrototypeRuntimeApi(controller).completion_fields(handle)
    assert not controller.calls


@pytest.mark.parametrize("state", [OperationState.MAIN_PENDING, OperationState.RECOVERING])
def test_completion_rejects_running_and_uncertain_state(state):
    controller = Controller()
    controller.state = state
    with pytest.raises(ProtocolError):
        PrototypeRuntimeApi(controller).completion_fields("e1cRuntimeКонтекст.Данные")
    assert not controller.calls


def test_completion_does_not_reuse_previous_fields_after_schema_failure():
    controller = Controller()
    api = PrototypeRuntimeApi(controller)
    assert api.completion_fields("e1cRuntimeКонтекст.Данные") == ("Номер", "Название")
    controller.fields = ("Имя", "имя")
    with pytest.raises(ProtocolError):
        api.completion_fields("e1cRuntimeКонтекст.Данные")
    controller.fields = ()
    assert api.completion_fields("e1cRuntimeКонтекст.Данные") == ()


def test_completion_is_one_consumer_owned_admission_and_schema_request():
    controller = Controller()
    api = PrototypeRuntimeApi(controller)
    api._worker_generation_handle = object()
    controller.state = OperationState.FAILED
    assert api.completion_fields("e1cRuntimeКонтекст.Данные") == ("Номер", "Название")
    assert controller.state is OperationState.FAILED and controller.operation_id == 7
    assert [request[0] for request in controller.target_requests] == ["completion"]
    assert "ДопущенныеИменаСвойств" in controller.calls[-1][0]


def test_completion_preserves_the_marker_and_all_128_admitted_names():
    controller = Controller()
    controller.fields = tuple(f"Поле{index}" for index in range(128))

    fields = PrototypeRuntimeApi(controller).completion_fields("e1cRuntimeКонтекст.Данные")

    assert fields == controller.fields
    assert len(fields) == 128


def test_completion_rejects_a_truncated_marker_plus_128_name_result():
    controller = Controller()
    controller.fields = tuple(f"Поле{index}" for index in range(128))
    controller.collection_row_limit = 128

    with pytest.raises(ProtocolError, match="Invalid completion field schema"):
        PrototypeRuntimeApi(controller).completion_fields("e1cRuntimeКонтекст.Данные")


@pytest.mark.parametrize(
    ("outcome", "error_type"),
    (("D|worker_generation_value", CaptureValueAccessDeniedError),
     ("E|value_admission_failed", CaptureValueCheckError)),
)
def test_completion_denial_or_failure_has_no_second_schema_target_read(outcome, error_type):
    controller = Controller()
    controller.admission_outcome = outcome

    with pytest.raises(error_type):
        PrototypeRuntimeApi(controller).completion_fields("e1cRuntimeКонтекст.Данные")

    assert [request[0] for request in controller.target_requests] == ["completion"]


def test_admission_closed_api_and_quarantined_capture_refuse_inspection():
    controller = Controller()
    api = PrototypeRuntimeApi(controller)
    api._admission_closed = True
    with pytest.raises(ProtocolError):
        api.completion_fields("e1cRuntimeКонтекст.Данные")
    api._admission_closed = False
    api._capture_inspection_quarantined = True
    controller.state = OperationState.CAPTURED
    with pytest.raises(ProtocolError):
        api.completion_fields("e1cRuntimeКонтекст.Данные")
    assert not controller.calls


def test_session_completion_does_not_wait_for_another_operation():
    session = object.__new__(RuntimeSession)
    session._operation_lock = RLock()
    session._closed = False
    session.runtime_api = PrototypeRuntimeApi(Controller())
    locked, release = Event(), Event()
    def owner():
        with session._operation_lock:
            locked.set()
            release.wait(5)
    thread = Thread(target=owner)
    thread.start()
    assert locked.wait(2)
    try:
        with pytest.raises(ProtocolError):
            session.completion_fields("e1cRuntimeКонтекст.Данные")
    finally:
        release.set()
        thread.join(2)
    assert session.completion_fields("e1cRuntimeКонтекст.Данные") == ("Номер", "Название")


def test_ready_completion_releases_session_and_api_locks_while_ticket_waits() -> None:
    """A ready inspection adopts before the handoff, then waits lock-free."""
    session = ControlledCaptureSession()
    controller_module = __import__(
        "onec_runtime.prototype_runtime", fromlist=("PrototypeRuntimeController",)
    )
    controller = controller_module.PrototypeRuntimeController(
        session, SERVICE, command_timeout_s=1.0,
    )
    controller.state = OperationState.COMPLETED
    api = PrototypeRuntimeApi(controller)
    api._namespace_names = ("Данные",)
    runtime = object.__new__(RuntimeSession)
    runtime.runtime_api = api
    runtime._operation_lock = RLock()
    runtime._closed = False
    fields: list[tuple[str, ...]] = []
    errors: list[BaseException] = []

    def complete() -> None:
        try:
            fields.append(runtime.completion_fields("e1cRuntimeКонтекст.Данные", timeout_s=1.0))
        except BaseException as error:
            errors.append(error)

    caller = Thread(target=complete, name="ready-session-completion-waiter")
    try:
        caller.start()
        assert session.accepted.wait(1), "ready inspection was not acknowledged"
        assert session.polling.wait(1), "ready inspection was not polled"

        # The coordinator owns the acknowledged request now. The original
        # caller must no longer block control-plane work on either outer lock.
        assert runtime._operation_lock.acquire(blocking=False)
        runtime._operation_lock.release()
        assert api._lock.acquire(blocking=False)
        api._lock.release()
        assert runtime.status().state is OperationState.RECOVERING
        with pytest.raises(ProtocolError):
            runtime.completion_fields("e1cRuntimeКонтекст.Данные", timeout_s=0.1)
        assert session.capture_start_count == 1

        session.complete(_completion_wire(), type_name="Строка")
        caller.join(1)
        assert not caller.is_alive()
        assert fields == [("Номер", "Название")]
        assert errors == []
        assert controller.state is OperationState.COMPLETED
    finally:
        if session.capture_pending is not None:
            session.complete(_completion_wire(), type_name="Строка")
        caller.join(1)
        close_owner(controller, session)


@pytest.mark.parametrize("captured", (True, False), ids=("capture", "ready"))
def test_completion_handoff_defers_heartbeat_while_controller_owns_debug_stream(
    captured: bool,
) -> None:
    """The released Session lock must not let heartbeat poll RDBG concurrently."""
    session = ControlledCaptureSession()
    if captured:
        runtime, api, controller = _captured_completion_runtime(session)
    else:
        controller_module = __import__(
            "onec_runtime.prototype_runtime", fromlist=("PrototypeRuntimeController",)
        )
        controller = controller_module.PrototypeRuntimeController(
            session, SERVICE, command_timeout_s=1.0,
        )
        controller.state = OperationState.COMPLETED
        api = PrototypeRuntimeApi(controller)
        api._namespace_names = ("Данные",)
        runtime = object.__new__(RuntimeSession)
        runtime.runtime_api = api
        runtime._operation_lock = RLock()
        runtime._closed = False

    heartbeats: list[str] = []
    process_checks: list[str] = []
    session.heartbeat = lambda: heartbeats.append("called")  # type: ignore[attr-defined]
    runtime._rdbg = session
    runtime._processes = SimpleNamespace(
        ensure_running=lambda: process_checks.append("called")
    )
    fields: list[tuple[str, ...]] = []
    errors: list[BaseException] = []

    def complete() -> None:
        try:
            fields.append(runtime.completion_fields("e1cRuntimeКонтекст.Данные", timeout_s=1.0))
        except BaseException as error:
            errors.append(error)

    caller = Thread(target=complete, name="completion-heartbeat-handoff")
    try:
        caller.start()
        assert session.accepted.wait(1), "completion was not acknowledged"
        assert session.polling.wait(1), "coordinator did not start polling"
        assert runtime._operation_lock.acquire(blocking=False)
        runtime._operation_lock.release()

        runtime._heartbeat_tick()

        assert heartbeats == []
        assert process_checks == ["called"]
        session.complete(_completion_wire(), type_name="Строка")
        caller.join(1)
        assert not caller.is_alive()
        assert fields == [("Номер", "Название")]
        assert errors == []
    finally:
        if session.capture_pending is not None:
            session.complete(_completion_wire(), type_name="Строка")
        caller.join(1)
        close_owner(controller, session)


def test_heartbeat_defers_debug_stream_for_a_detached_controller_owned_resume() -> None:
    """A pending resume has the same exclusive RDBG-stream ownership as evaluation."""
    class PendingResumeSession(ControlledCaptureSession):
        def __init__(self) -> None:
            super().__init__(auto_helpers=True)
            self.initial_capture = True
            self.resume_waiting = Event()
            self.release_resume = Event()

        def wait_for_any_stop(self, *, timeout_s: float):
            if self.initial_capture:
                self.initial_capture = False
                return super().wait_for_any_stop(timeout_s=timeout_s)
            self.resume_waiting.set()
            assert self.release_resume.wait(timeout_s)
            raise TargetLost("synthetic resume completion")

    session = PendingResumeSession()
    runtime, _api, controller = _captured_completion_runtime(session)
    heartbeats: list[str] = []
    process_checks: list[str] = []
    session.heartbeat = lambda: heartbeats.append("called")  # type: ignore[attr-defined]
    runtime._rdbg = session
    runtime._processes = SimpleNamespace(
        ensure_running=lambda: process_checks.append("called")
    )
    owner = controller._capture_evaluation_owner()
    try:
        ticket = controller.submit_resume()
        assert session.resume_waiting.wait(1), "resume owner did not enter stop wait"
        ticket.detach_initiator()
        assert ticket.initiator_detached
        assert owner.owns_debug_ui_stream()

        runtime._heartbeat_tick()

        assert heartbeats == []
        assert process_checks == ["called"]
        assert owner.owns_debug_ui_stream()
        session.release_resume.set()
        deadline = monotonic() + 1.0
        while owner.owns_debug_ui_stream() and monotonic() < deadline:
            sleep(0.005)
        assert not owner.owns_debug_ui_stream()
    finally:
        session.release_resume.set()
        close_owner(controller, session)


def test_ready_completion_submit_interruption_detaches_adopted_ticket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exception after adoption leaves the event owner as sole consumer."""
    session = ControlledCaptureSession()
    controller_module = __import__(
        "onec_runtime.prototype_runtime", fromlist=("PrototypeRuntimeController",)
    )
    controller = controller_module.PrototypeRuntimeController(
        session, SERVICE, command_timeout_s=1.0,
    )
    controller.state = OperationState.COMPLETED
    api = PrototypeRuntimeApi(controller)
    api._namespace_names = ("Данные",)
    runtime = object.__new__(RuntimeSession)
    runtime.runtime_api = api
    runtime._operation_lock = RLock()
    runtime._closed = False
    original_submit = CaptureEvaluationCoordinator.submit_evaluation

    def interrupt_after_adoption(
        owner: CaptureEvaluationCoordinator,
        request: object,
    ) -> CaptureEvaluationTicket:
        ticket = original_submit(owner, request)  # type: ignore[arg-type]
        raise RuntimeError("planned submit-return interruption")

    monkeypatch.setattr(
        CaptureEvaluationCoordinator,
        "submit_evaluation",
        interrupt_after_adoption,
    )
    try:
        with pytest.raises(RuntimeError, match="planned submit-return interruption"):
            runtime.completion_fields("e1cRuntimeКонтекст.Данные", timeout_s=1.0)

        assert session.accepted.wait(1)
        owner = controller.ready_inspection_evaluation_owner()
        assert owner is not None and owner._active is not None
        assert owner._active.initiator_attached is False
        assert controller.state is OperationState.RECOVERING
        assert runtime._operation_lock.acquire(blocking=False)
        runtime._operation_lock.release()
        with pytest.raises(ProtocolError):
            runtime.completion_fields("e1cRuntimeКонтекст.Данные", timeout_s=0.1)
        assert session.capture_start_count == 1

        session.complete(_completion_wire(), type_name="Строка")
        deadline = monotonic() + 1.0
        while controller.state is OperationState.RECOVERING and monotonic() < deadline:
            sleep(0.005)
        assert controller.state is OperationState.COMPLETED
    finally:
        if session.capture_pending is not None:
            session.complete(_completion_wire(), type_name="Строка")
        close_owner(controller, session)
