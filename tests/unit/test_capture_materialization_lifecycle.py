from __future__ import annotations

from threading import Thread

import pytest

from onec_runtime.capture_evaluation import (
    CaptureEvaluationKind,
    CaptureEvaluationState,
    CapturePhase,
)
from onec_runtime.errors import CaptureBusyError, CaptureEvaluationPendingError
from onec_runtime.prototype_runtime import OperationState
from onec_runtime.runtime_api import PrototypeRuntimeApi
from onec_runtime_jupyter.extension import OnecValueProxy

from test_capture_evaluation_lifecycle import ControlledCaptureSession, close_owner
from test_capture_control_plane import _eventually
from test_prototype_runtime import captured_controller


def _table_proxy(api: PrototypeRuntimeApi, *, runtime_generation: int) -> OnecValueProxy:
    api._namespace_names = ("Таблица",)
    return OnecValueProxy(
        api,
        "Таблица",
        runtime_generation=runtime_generation,
        context_generation=1,
    )


def test_proxy_table_materialization_detaches_one_composite_capture_helper() -> None:
    """The coordinator owns both late cleanup and the single CAPTURE capability."""
    session = ControlledCaptureSession()
    controller = captured_controller(session, command_timeout_s=0.02)
    api = PrototypeRuntimeApi(controller)
    proxy = _table_proxy(api, runtime_generation=controller.runtime_generation)
    errors: list[BaseException] = []

    def materialize() -> None:
        try:
            proxy.to_df()
        except BaseException as error:
            errors.append(error)

    caller = Thread(target=materialize, name="materialization-initiator")
    try:
        caller.start()
        assert session.accepted.wait(1), "materialization was not acknowledged"
        caller.join(1)

        assert not caller.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], CaptureEvaluationPendingError)
        pending = api.current_capture().status()
        assert pending.phase is CapturePhase.EVALUATING
        assert pending.evaluation_kind is CaptureEvaluationKind.MATERIALIZATION_HELPER
        assert pending.pending_evaluation_id == errors[0].evaluation_id
        assert session.capture_start_count == 1
        started_sources = [
            call[1][0]
            for call in session.calls
            if call[0] == "start_evaluation"
        ]
        assert len(started_sources) == 1
        primary_source = started_sources[0]
        assert "СериализоватьКомпактнуюТаблицу" in primary_source
        assert primary_source.index("Если Не Материализация.Доступ Тогда") < primary_source.index(
            "Контекст.Вставить("
        )

        with pytest.raises(CaptureBusyError) as busy:
            proxy.to_df()
        assert busy.value.evaluation_id == pending.pending_evaluation_id
        assert busy.value.evaluation_kind is CaptureEvaluationKind.MATERIALIZATION_HELPER
        assert session.capture_start_count == 1

        # The detached caller must not fetch/decode the private payload.  The
        # coordinator still removes its server-side context key before it
        # returns CAPTURE to the paused state.
        session.auto_helpers = True
        session.complete("R|1|1|1|" + "a" * 64 + "|4", type_name="Строка")
        _eventually(lambda: api.current_capture().status().phase is CapturePhase.PAUSED)

        settled = api.current_capture().wait(
            timeout_s=1,
            evaluation_id=errors[0].evaluation_id,
        )
        assert settled.state is CaptureEvaluationState.COMPLETED
        assert session.capture_start_count == 2
        cleanup_source = [
            call[1][0]
            for call in session.calls
            if call[0] == "start_evaluation"
        ][-1]
        assert "УдалитьМатериализациюИзКонтекста" in cleanup_source
        assert controller.state is OperationState.CAPTURED
    finally:
        if session.capture_pending is not None:
            session.complete()
        caller.join(1)
        close_owner(controller, session)
