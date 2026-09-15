from threading import RLock, Thread, Event
from types import SimpleNamespace
from uuid import UUID

import pytest

from onec_runtime.errors import ProtocolError
from onec_runtime.prototype_runtime import OperationState
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

    def inspect_completion_fields(self, handle, *, table_row):
        self.calls.append((handle, table_row, self.command_timeout_s))
        return EvaluationResult(
            UUID(int=1), "ТаблицаЗначений", "", False,
            collection_size=len(self.fields),
            collection_rows=tuple(CollectionRow(i, (
                CollectionCell("Имя", "Строка", "", value_string=name),
            )) for i, name in enumerate(self.fields)),
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


def test_completion_with_loaded_worker_does_not_run_main_privacy_instructions():
    controller = Controller()
    def forbidden_instruction(source):
        raise AssertionError("Completion must not execute a MAIN instruction")
    api = PrototypeRuntimeApi(controller, worker_instruction_executor=forbidden_instruction)
    api._worker_generation_handle = object()
    controller.state = OperationState.FAILED
    assert api.completion_fields("Контекст.Данные") == ("Номер", "Название")
    assert controller.state is OperationState.FAILED and controller.operation_id == 7


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
