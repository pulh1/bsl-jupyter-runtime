from __future__ import annotations

from threading import Thread, current_thread

import pytest

from onec_runtime.capture_evaluation import (
    CaptureEvaluationKind,
    CaptureEvaluationState,
    CapturePhase,
)
from onec_runtime.errors import (
    CaptureBusyError,
    CaptureEvaluationPendingError,
    CaptureOutcomeUnknownError,
    CaptureRecoveryRequiredError,
    CaptureValueAccessDeniedError,
    CaptureValueCheckError,
)
from onec_runtime.prototype_runtime import OperationState
from onec_runtime.runtime_api import PrototypeRuntimeApi
from onec_runtime_jupyter.extension import OnecValueProxy

from test_capture_evaluation_lifecycle import (
    ControlledCaptureSession,
    capture_probe,
    close_owner,
)
from test_capture_control_plane import _eventually
from test_prototype_runtime import CAPTURE_A, SERVICE, captured_controller


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
    # The first stop captures the paused frame; the second is the MAIN
    # completion consumed only after the coordinator has removed the late
    # materialization payload.
    session = ControlledCaptureSession(stops=(CAPTURE_A, SERVICE))
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
        assert "Контекст.Удалить(\"\"__onec_compact_table_" in cleanup_source
        assert controller.state is OperationState.CAPTURED
        api.resume_capture()
        assert controller.state is OperationState.COMPLETED
    finally:
        if session.capture_pending is not None:
            session.complete()
        caller.join(1)
        close_owner(controller, session)


def test_interrupted_table_materialization_detaches_the_capture_waiter() -> None:
    """KeyboardInterrupt leaves the admitted transfer and cleanup with its owner."""
    session = ControlledCaptureSession()
    controller = captured_controller(session, command_timeout_s=1)
    api = PrototypeRuntimeApi(controller)
    proxy = _table_proxy(api, runtime_generation=controller.runtime_generation)
    owner = capture_probe(controller).owner
    wait = owner._condition.wait
    caller = current_thread()

    def interrupt_caller_wait(timeout: float | None = None) -> object:
        if current_thread() is caller:
            raise KeyboardInterrupt
        return wait(timeout)

    owner._condition.wait = interrupt_caller_wait  # type: ignore[method-assign]
    try:
        with pytest.raises(KeyboardInterrupt):
            proxy.to_df()

        assert session.accepted.wait(1), "materialization was not acknowledged"
        assert api.current_capture().status().phase is CapturePhase.EVALUATING
        assert session.capture_start_count == 1

        # Restore normal observation before settling the late result.  No
        # caller-owned read/decode is permitted after its interrupt.
        owner._condition.wait = wait  # type: ignore[method-assign]
        session.auto_helpers = True
        session.complete("R|1|1|1|" + "a" * 64 + "|4", type_name="Строка")
        outcome = api.current_capture().wait(timeout_s=1)

        assert outcome.state is CaptureEvaluationState.COMPLETED
        assert api.current_capture().status().phase is CapturePhase.PAUSED
        assert session.capture_start_count == 2
        sources = [call[1][0] for call in session.calls if call[0] == "start_evaluation"]
        assert all("ЗабратьКомпактнуюМатериализациюИзКонтекста" not in source for source in sources)
    finally:
        owner._condition.wait = wait  # type: ignore[method-assign]
        if session.capture_pending is not None:
            session.complete()
        close_owner(controller, session)


def test_confirmed_table_materialization_restore_failure_skips_payload_read() -> None:
    """A confirmed envelope still cannot read the target after restore fails."""
    session = ControlledCaptureSession(fail_workspace_on_call=3)
    controller = captured_controller(session, command_timeout_s=1)
    api = PrototypeRuntimeApi(controller)
    proxy = _table_proxy(api, runtime_generation=controller.runtime_generation)
    errors: list[BaseException] = []

    def materialize() -> None:
        try:
            proxy.to_df()
        except BaseException as error:
            errors.append(error)

    caller = Thread(target=materialize, name="materialization-restore-failure")
    try:
        caller.start()
        assert session.accepted.wait(1), "materialization was not acknowledged"
        assert session.polling.wait(1), "materialization was not polling"
        session.auto_helpers = True
        session.complete("R|1|1|1|" + "a" * 64 + "|4", type_name="Строка")
        caller.join(1)

        assert not caller.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], CaptureRecoveryRequiredError)
        status = api.current_capture().status()
        assert status.phase is CapturePhase.RECOVERY_REQUIRED
        assert session.capture_start_count == 1
        sources = [call[1][0] for call in session.calls if call[0] == "start_evaluation"]
        assert all("ЗабратьКомпактнуюМатериализациюИзКонтекста" not in source for source in sources)
    finally:
        if session.capture_pending is not None:
            session.complete()
        caller.join(1)
        close_owner(controller, session)


def test_recursive_materialization_detaches_busy_waiter_and_late_payload() -> None:
    """The recursive serializer follows the same single-record lifecycle."""
    session = ControlledCaptureSession()
    controller = captured_controller(session, command_timeout_s=0.02)
    api = PrototypeRuntimeApi(controller)
    errors: list[BaseException] = []

    def materialize() -> None:
        try:
            api.materialize_value(
                "Контекст.Данные", max_depth=2, max_items=3, max_bytes=1024
            )
        except BaseException as error:
            errors.append(error)

    caller = Thread(target=materialize, name="recursive-materialization-initiator")
    try:
        caller.start()
        assert session.accepted.wait(1), "recursive materialization was not acknowledged"
        caller.join(1)

        assert len(errors) == 1
        assert isinstance(errors[0], CaptureEvaluationPendingError)
        pending = api.current_capture().status()
        assert pending.evaluation_kind is CaptureEvaluationKind.MATERIALIZATION_HELPER
        with pytest.raises(CaptureBusyError) as busy:
            api.materialize_value(
                "Контекст.Данные", max_depth=2, max_items=3, max_bytes=1024
            )
        assert busy.value.evaluation_id == pending.pending_evaluation_id
        assert session.capture_start_count == 1

        session.auto_helpers = True
        session.complete("R|1|1|1|" + "a" * 64 + "|4", type_name="Строка")
        _eventually(lambda: api.current_capture().status().phase is CapturePhase.PAUSED)
        settled = api.current_capture().wait(
            timeout_s=1,
            evaluation_id=errors[0].evaluation_id,
        )

        assert settled.state is CaptureEvaluationState.COMPLETED
        assert session.capture_start_count == 2
        sources = [call[1][0] for call in session.calls if call[0] == "start_evaluation"]
        assert "СериализоватьЗначение(" in sources[0]
        assert all("ЗабратьКомпактнуюМатериализациюИзКонтекста" not in source for source in sources)
    finally:
        if session.capture_pending is not None:
            session.complete()
        caller.join(1)
        close_owner(controller, session)


@pytest.mark.parametrize(
    ("envelope", "expected_error"),
    (
        ("D|worker_generation_value", CaptureValueAccessDeniedError),
        ("not an admission envelope", CaptureValueCheckError),
    ),
)
def test_table_admission_rejects_before_private_payload_transfer(
    envelope: str,
    expected_error: type[Exception],
) -> None:
    session = ControlledCaptureSession()
    controller = captured_controller(session, command_timeout_s=1)
    api = PrototypeRuntimeApi(controller)
    proxy = _table_proxy(api, runtime_generation=controller.runtime_generation)
    errors: list[BaseException] = []

    def materialize() -> None:
        try:
            proxy.to_df()
        except BaseException as error:
            errors.append(error)

    caller = Thread(target=materialize, name="materialization-admission")
    try:
        caller.start()
        assert session.accepted.wait(1), "materialization was not acknowledged"
        assert session.polling.wait(1), "materialization was not polling"
        session.auto_helpers = True
        session.complete(envelope, type_name="Строка")
        caller.join(1)

        assert not caller.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], expected_error)
        _eventually(lambda: api.current_capture().status().phase is CapturePhase.PAUSED)
        sources = [call[1][0] for call in session.calls if call[0] == "start_evaluation"]
        assert len(sources) == 2
        assert all("ЗабратьКомпактнуюМатериализациюИзКонтекста" not in source for source in sources)
        assert "Контекст.Удалить(\"\"__onec_compact_table_" in sources[-1]
    finally:
        if session.capture_pending is not None:
            session.complete()
        caller.join(1)
        close_owner(controller, session)


def test_table_bsl_failure_is_a_value_check_before_payload_read() -> None:
    session = ControlledCaptureSession()
    controller = captured_controller(session, command_timeout_s=1)
    api = PrototypeRuntimeApi(controller)
    proxy = _table_proxy(api, runtime_generation=controller.runtime_generation)
    errors: list[BaseException] = []

    def materialize() -> None:
        try:
            proxy.to_df()
        except BaseException as error:
            errors.append(error)

    caller = Thread(target=materialize, name="materialization-bsl-failure")
    try:
        caller.start()
        assert session.accepted.wait(1)
        assert session.polling.wait(1)
        session.auto_helpers = True
        session.complete("private platform failure", type_name="Ошибка", error="private")
        caller.join(1)

        assert len(errors) == 1
        assert isinstance(errors[0], CaptureValueCheckError)
        _eventually(lambda: api.current_capture().status().phase is CapturePhase.PAUSED)
        sources = [call[1][0] for call in session.calls if call[0] == "start_evaluation"]
        assert len(sources) == 2
        assert all("ЗабратьКомпактнуюМатериализациюИзКонтекста" not in source for source in sources)
    finally:
        if session.capture_pending is not None:
            session.complete()
        caller.join(1)
        close_owner(controller, session)


def test_table_dispatch_uncertainty_is_not_a_worker_error() -> None:
    session = ControlledCaptureSession(dispatch_error=OSError("transport lost"))
    controller = captured_controller(session, command_timeout_s=1)
    api = PrototypeRuntimeApi(controller)
    proxy = _table_proxy(api, runtime_generation=controller.runtime_generation)
    try:
        with pytest.raises(CaptureOutcomeUnknownError):
            proxy.to_df()
        status = api.current_capture().status()
        assert status.phase is CapturePhase.OUTCOME_UNKNOWN
        assert status.evaluation_kind is None
        assert session.capture_start_count == 1
    finally:
        close_owner(controller, session)


def test_table_cleanup_uncertainty_requires_recovery() -> None:
    session = ControlledCaptureSession()
    controller = captured_controller(session, command_timeout_s=1)
    api = PrototypeRuntimeApi(controller)
    proxy = _table_proxy(api, runtime_generation=controller.runtime_generation)
    errors: list[BaseException] = []

    def materialize() -> None:
        try:
            proxy.to_df()
        except BaseException as error:
            errors.append(error)

    caller = Thread(target=materialize, name="materialization-cleanup-uncertain")
    try:
        caller.start()
        assert session.accepted.wait(1)
        session.auto_helpers = True
        session.dispatch_error = OSError("cleanup transport lost")
        session.complete("R|1|1|1|" + "a" * 64 + "|4", type_name="Строка")
        caller.join(1)

        assert len(errors) == 1
        assert isinstance(errors[0], CaptureRecoveryRequiredError)
        assert api.current_capture().status().phase is CapturePhase.RECOVERY_REQUIRED
        assert session.capture_start_count == 2
    finally:
        if session.capture_pending is not None:
            session.complete()
        caller.join(1)
        close_owner(controller, session)


def test_proxy_table_projection_uses_one_inspection_record() -> None:
    session = ControlledCaptureSession()
    controller = captured_controller(session, command_timeout_s=0.02)
    api = PrototypeRuntimeApi(controller)
    proxy = _table_proxy(api, runtime_generation=controller.runtime_generation)
    errors: list[BaseException] = []

    def project() -> None:
        try:
            proxy.head(1).to_df()
        except BaseException as error:
            errors.append(error)

    caller = Thread(target=project, name="projection-initiator")
    try:
        caller.start()
        assert session.accepted.wait(1), "projection was not acknowledged"
        caller.join(1)

        assert len(errors) == 1
        assert isinstance(errors[0], CaptureEvaluationPendingError)
        pending = api.current_capture().status()
        assert pending.evaluation_kind is CaptureEvaluationKind.INSPECTION
        assert session.capture_start_count == 1
    finally:
        if session.capture_pending is not None:
            session.complete()
        caller.join(1)
        close_owner(controller, session)


def test_materialization_kind_uses_an_explicit_inspection_record() -> None:
    session = ControlledCaptureSession()
    controller = captured_controller(session, command_timeout_s=0.02)
    api = PrototypeRuntimeApi(controller)
    errors: list[BaseException] = []

    def inspect_kind() -> None:
        try:
            api.materialization_kind("Контекст.Таблица")
        except BaseException as error:
            errors.append(error)

    caller = Thread(target=inspect_kind, name="kind-inspection-initiator")
    try:
        caller.start()
        assert session.accepted.wait(1), "kind inspection was not acknowledged"
        caller.join(1)

        assert len(errors) == 1
        assert isinstance(errors[0], CaptureEvaluationPendingError)
        assert (
            api.current_capture().status().evaluation_kind
            is CaptureEvaluationKind.INSPECTION
        )
        assert session.capture_start_count == 1
    finally:
        if session.capture_pending is not None:
            session.complete()
        caller.join(1)
        close_owner(controller, session)


@pytest.mark.parametrize(
    ("operation", "evaluation_kind"),
    (
        (
            lambda api: api.materialize_value(
                "Контекст.Данные", max_depth=2, max_items=3, max_bytes=1024
            ),
            CaptureEvaluationKind.MATERIALIZATION_HELPER,
        ),
        (
            lambda api: api.project_value(
                "Контекст.Данные", {"offset": 0, "limit": 1},
                max_depth=2, max_items=3, max_bytes=1024,
            ),
            CaptureEvaluationKind.INSPECTION,
        ),
    ),
)
def test_dynamic_routes_admit_before_route_or_payload_access(
    operation,  # type: ignore[no-untyped-def]
    evaluation_kind: CaptureEvaluationKind,
) -> None:
    session = ControlledCaptureSession()
    controller = captured_controller(session, command_timeout_s=0.02)
    api = PrototypeRuntimeApi(controller)
    errors: list[BaseException] = []

    def materialize() -> None:
        try:
            operation(api)
        except BaseException as error:
            errors.append(error)

    caller = Thread(target=materialize, name="dynamic-materialization-initiator")
    try:
        caller.start()
        assert session.accepted.wait(1)
        caller.join(1)

        assert len(errors) == 1
        assert isinstance(errors[0], CaptureEvaluationPendingError)
        assert api.current_capture().status().evaluation_kind is evaluation_kind
        sources = [call[1][0] for call in session.calls if call[0] == "start_evaluation"]
        assert len(sources) == 1
        source = sources[0]
        assert source.index("ДопуститьЗначение(") < source.index("ПолучитьВидМатериализации(")
        assert source.index("ПолучитьВидМатериализации(") < source.index("Контекст.Вставить(")
        assert "СериализоватьКомпактнуюТаблицу(" in source
        assert "СериализоватьЗначение(" in source
    finally:
        if session.capture_pending is not None:
            session.complete()
        caller.join(1)
        close_owner(controller, session)
