from threading import RLock, Thread, Event
from types import SimpleNamespace
from uuid import UUID

import pytest

from onec_runtime.errors import CaptureValueAccessDeniedError, CaptureValueCheckError, ProtocolError
from onec_runtime.prototype_runtime import MainCompletion, OperationHandle, OperationState
from onec_runtime.rdbg.models import CollectionCell, CollectionRow, EvaluationResult
from onec_runtime.runtime_api import PrototypeRuntimeApi
from onec_runtime.session import RuntimeSession


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

    def execute_system_main(self, source):
        self.admission_sources.append(source)
        self.target_requests.append(("precursor", source))
        return MainCompletion(OperationHandle(self.operation_id, source, source), "value", "", True)

    def inspect_completion_fields(self, handle, *, table_row, worker_type_registrations=()):
        self.calls.append((handle, table_row, self.command_timeout_s, worker_type_registrations))
        self.target_requests.append(("completion", handle))
        if self.admission_outcome != "R":
            return EvaluationResult(
                UUID(int=1), "ТаблицаЗначений", "", False,
                collection_size=1,
                collection_rows=(CollectionRow(0, (
                    CollectionCell("Состояние", "Строка", "", value_string=self.admission_outcome),
                    CollectionCell("Имя", "Строка", "", value_string=""),
                )),),
            )
        rows = (CollectionRow(0, (
            CollectionCell("Состояние", "Строка", "", value_string="R"),
            CollectionCell("Имя", "Строка", "", value_string=""),
        )),) + tuple(CollectionRow(index + 1, (
            CollectionCell("Состояние", "Строка", "", value_string="R"),
            CollectionCell("Имя", "Строка", "", value_string=name),
        )) for index, name in enumerate(self.fields))
        if self.collection_row_limit is not None:
            rows = rows[:self.collection_row_limit]
        return EvaluationResult(
            UUID(int=1), "ТаблицаЗначений", "", False,
            collection_size=(len(self.fields) + 1 if self.collection_size_override is None
                             else self.collection_size_override),
            collection_rows=rows,
        )


def test_completion_reads_only_current_schema_without_inferencing_value_types():
    controller = Controller()
    api = PrototypeRuntimeApi(controller)
    assert api.completion_fields("Контекст.Данные", table_row=True) == ("Номер", "Название")
    assert controller.calls[0][:2] == ("Контекст.Данные", True)
    assert 0 < controller.calls[0][2] <= 1.0
    assert controller.command_timeout_s == 30.0
    assert controller.operation_id == 7
    controller.fields = ("ОбновленнаяКолонка",)
    assert api.completion_fields("Контекст.Данные", table_row=True) == ("ОбновленнаяКолонка",)


@pytest.mark.parametrize("handle", [
    "Контекст.Данные[0]", "Контекст.Данные.Удалить()", "Контекст.Данные;Удалить()",
    "Контекст.Несуществующая", "Контекст.RuntimeWorkerActiveGeneration",
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
        PrototypeRuntimeApi(controller).completion_fields("Контекст.Данные")
    assert not controller.calls


def test_completion_does_not_reuse_previous_fields_after_schema_failure():
    controller = Controller()
    api = PrototypeRuntimeApi(controller)
    assert api.completion_fields("Контекст.Данные") == ("Номер", "Название")
    controller.fields = ("Имя", "имя")
    with pytest.raises(ProtocolError):
        api.completion_fields("Контекст.Данные")
    controller.fields = ()
    assert api.completion_fields("Контекст.Данные") == ()


def test_completion_is_one_consumer_owned_admission_and_schema_request():
    controller = Controller()
    api = PrototypeRuntimeApi(controller)
    api._worker_generation_handle = object()
    controller.state = OperationState.FAILED
    assert api.completion_fields("Контекст.Данные") == ("Номер", "Название")
    assert controller.state is OperationState.FAILED and controller.operation_id == 7
    assert controller.target_requests == [("completion", "Контекст.Данные")]
    assert controller.calls[-1][-1] == ()


def test_completion_preserves_the_marker_and_all_128_admitted_names():
    controller = Controller()
    controller.fields = tuple(f"Поле{index}" for index in range(128))

    fields = PrototypeRuntimeApi(controller).completion_fields("Контекст.Данные")

    assert fields == controller.fields
    assert len(fields) == 128


def test_completion_rejects_a_truncated_marker_plus_128_name_result():
    controller = Controller()
    controller.fields = tuple(f"Поле{index}" for index in range(128))
    controller.collection_row_limit = 128

    with pytest.raises(ProtocolError, match="Invalid completion field schema"):
        PrototypeRuntimeApi(controller).completion_fields("Контекст.Данные")


@pytest.mark.parametrize(
    ("outcome", "error_type"),
    (("D|worker_generation_value", CaptureValueAccessDeniedError),
     ("E|value_admission_failed", CaptureValueCheckError)),
)
def test_completion_denial_or_failure_has_no_second_schema_target_read(outcome, error_type):
    controller = Controller()
    controller.admission_outcome = outcome

    with pytest.raises(error_type):
        PrototypeRuntimeApi(controller).completion_fields("Контекст.Данные")

    assert controller.target_requests == [("completion", "Контекст.Данные")]


def test_admission_closed_api_and_quarantined_capture_refuse_inspection():
    controller = Controller()
    api = PrototypeRuntimeApi(controller)
    api._admission_closed = True
    with pytest.raises(ProtocolError):
        api.completion_fields("Контекст.Данные")
    api._admission_closed = False
    api._capture_inspection_quarantined = True
    controller.state = OperationState.CAPTURED
    with pytest.raises(ProtocolError):
        api.completion_fields("Контекст.Данные")
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
            session.completion_fields("Контекст.Данные")
    finally:
        release.set()
        thread.join(2)
    assert session.completion_fields("Контекст.Данные") == ("Номер", "Название")
