"""A running MAIN command must remain observable from another notebook call."""

from threading import Event, Thread

from onec_runtime.runtime_api import PrototypeRuntimeApi

from test_prototype_runtime import SERVICE, ScriptedSession, runtime_module


def test_status_reports_running_main_without_waiting_for_its_notebook_writer() -> None:
    waiting = Event()
    release = Event()

    class WaitingSession(ScriptedSession):
        def wait_for_any_stop(self, *, timeout_s: float):
            waiting.set()
            assert release.wait(3), "MAIN stop was never released"
            return super().wait_for_any_stop(timeout_s=timeout_s)

    controller = runtime_module().PrototypeRuntimeController(
        WaitingSession((SERVICE,)), SERVICE
    )
    api = PrototypeRuntimeApi(controller)
    errors: list[BaseException] = []

    def run_main() -> None:
        try:
            api.execute_bsl("Результат = 1;")
        except BaseException as error:
            errors.append(error)

    caller = Thread(target=run_main, name="main-notebook-caller")
    caller.start()
    try:
        assert waiting.wait(3), "MAIN did not enter the stop wait"
        status = api.status()
        assert status.state is runtime_module().OperationState.MAIN_PENDING
        assert status.operation_id == controller.main_operation.command_id
    finally:
        release.set()
        caller.join(3)
    assert not caller.is_alive()
    assert errors == []
