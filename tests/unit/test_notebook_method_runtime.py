"""Method publication contracts with the real controller, parser and EPF builder.

Only the external debugger session and target artifact transport are scripted.
"""
from pathlib import Path
import re
from threading import Barrier, Event, RLock, Thread, current_thread

import pytest

from onec_runtime.errors import (
    BslExecutionError,
    CaptureBusyError,
    CaptureEvaluationPendingError,
    CaptureOutcomeUnknownError,
    CaptureRecoveryRequiredError,
    ProtocolError,
)
from onec_runtime.prototype_runtime import PrototypeRuntimeController
from onec_runtime.runtime_api import PrototypeRuntimeApi
from onec_runtime.bsl.source_maps import SourceUnitKind, SourceUnitRef, source_sha256
from onec_runtime.worker_universe import WorkerModuleArtifactBuilder, WorkerModuleArtifactCache

from test_prototype_runtime import (
    CAPTURE_A,
    CAPTURE_B,
    SERVICE,
    USER,
    ScriptedSession,
    captured_controller,
    evaluation,
)
from test_runtime_api import (
    _notebook_worker_builder, _UniverseInstructionExecutor,
    _SemanticSnapshotFailureTarget, _common_module_catalog, _worker_module_unit,
)
from test_capture_evaluation_lifecycle import (
    ControlledCaptureSession,
    close_owner,
)


PAIR = 'Функция А()\nВозврат Б();\nКонецФункции\nФункция Б()\nВозврат 1;\nКонецФункции'
UPDATE = 'Функция Б()\nВозврат 2;\nКонецФункции'
THIRD = 'Функция В()\nВозврат 3;\nКонецФункции'


class TargetSession(ScriptedSession):
    lose_capture_reply = False
    lose_capture_resume = False

    def continue_evaluation(self, pending, stop):
        if self.lose_capture_resume:
            raise TimeoutError('planned pending evaluation resume loss')
        return super().continue_evaluation(pending, stop)

    def evaluate(self, expression, **kwargs):
        if self.lose_capture_reply and 'ВыполнитьКодВКонтекстеОтладки' in expression:
            raise TimeoutError('planned evaluation transport loss')
        if any(name in expression for name in ('УстановитьПинПоколенияWorker', 'ОчиститьПинПоколенияWorker')):
            self.calls.append(('evaluate', expression))
            return evaluation('Булево', 'Истина')
        return super().evaluate(expression, **kwargs)


def runtime(tmp_path: Path, *, target=None, captured=False):
    session = TargetSession((CAPTURE_A, CAPTURE_B, SERVICE) if captured else (SERVICE,) * 15)
    controller = PrototypeRuntimeController(session, SERVICE)
    builder = _notebook_worker_builder(tmp_path)
    api = PrototypeRuntimeApi(
        controller, notebook_worker_builder=builder,
        worker_module_builder=WorkerModuleArtifactBuilder(
            builder, cache=WorkerModuleArtifactCache(),
            packer_version='worker-epf-v1', target_profile='server-test',
        ),
        worker_instruction_executor=target or _UniverseInstructionExecutor(),
        capture_points=(CAPTURE_A, CAPTURE_B) if captured else (),
        user_breakpoints=(USER,) if captured else (),
    )
    return api, controller, session


class _NotebookShell:
    def __init__(self) -> None:
        self.user_ns: dict[str, object] = {}


def _session_backed_notebook_runtime(
    api: PrototypeRuntimeApi,
):  # type: ignore[no-untyped-def]
    from onec_runtime.session import RuntimeSession

    runtime = object.__new__(RuntimeSession)
    runtime.runtime_api = api
    runtime._operation_lock = RLock()
    runtime._closed = False
    return runtime


def test_jupyter_magic_pending_user_bsl_detaches_core_ticket_and_keeps_controls_available(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The notebook waiter must not retain either Session/API writer lock."""

    from onec_runtime.capture_evaluation import (
        CaptureEvaluationState,
        CaptureEvaluationTicket,
        CapturePhase,
    )
    from onec_runtime_jupyter.extension import (
        MACHINE_MIME_TYPE,
        OnecRuntimeMagics,
        install_runtime,
    )

    session = ControlledCaptureSession()
    controller = captured_controller(session, command_timeout_s=30)
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=_notebook_worker_builder(tmp_path),
    )
    runtime = _session_backed_notebook_runtime(api)
    shell = _NotebookShell()
    install_runtime(shell, runtime)
    magic = OnecRuntimeMagics(shell)  # type: ignore[arg-type]
    result: list[object] = []
    errors: list[BaseException] = []
    finished = Event()
    wait_handoff = Barrier(2)
    release_initiator = Event()
    original_wait = CaptureEvaluationTicket.wait_initiator

    def held_wait(
        ticket: CaptureEvaluationTicket,
        timeout_s: float | None = None,
    ) -> object:
        assert timeout_s == 30
        wait_handoff.wait(timeout=1)
        assert release_initiator.wait(1)
        return original_wait(ticket, timeout_s=0)

    monkeypatch.setattr(CaptureEvaluationTicket, "wait_initiator", held_wait)

    def execute() -> None:
        try:
            result.append(magic.bsl("", "РезультатИнструкции = 901;"))
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()

    initiator = Thread(target=execute, name="jupyter-user-bsl-initiator")
    contender_done = Event()
    contender_errors: list[BaseException] = []

    def contend() -> None:
        try:
            runtime.execute_bsl("РезультатИнструкции = 902;")
        except BaseException as error:
            contender_errors.append(error)
        finally:
            contender_done.set()

    contender = Thread(target=contend, name="jupyter-user-bsl-contender")
    try:
        initiator.start()
        assert session.accepted.wait(1), "user BSL evaluation was not acknowledged"
        wait_handoff.wait(timeout=1)
        capture = runtime.current_capture()
        status = capture.status()
        assert status.phase is CapturePhase.EVALUATING
        assert status.evaluation_kind.value == "user_bsl"
        assert status.pending_evaluation_id is not None
        assert re.fullmatch(
            r"capture-eval-v1-[0-9a-f]{32}",
            status.pending_evaluation_id,
        )
        assert capture.wait(timeout_s=0).state is CaptureEvaluationState.PENDING

        contender.start()
        assert contender_done.wait(1), "Session/API writer lock stayed with waiter"
        assert len(contender_errors) == 1
        assert isinstance(contender_errors[0], CaptureBusyError)
        assert session.capture_start_count == 1

        release_initiator.set()
        assert finished.wait(1), "notebook command deadline did not detach waiter"
        assert errors == []
        assert len(result) == 1 and result[0] is not None
        bundle = result[0]._repr_mimebundle_()  # type: ignore[union-attr]
        expected_pending = (
            f"evaluation_id={status.pending_evaluation_id}\n"
            "evaluation_kind=user_bsl\n"
            "runtime.current_capture().wait(timeout_s=10)"
        )
        assert bundle["text/plain"] == expected_pending  # type: ignore[index]
        assert bundle["text/html"] == f"<pre>{expected_pending}</pre>"  # type: ignore[index]
        assert (
            bundle[MACHINE_MIME_TYPE]["evaluation_id"]
            == status.pending_evaluation_id
        )  # type: ignore[index]
        assert (
            bundle[MACHINE_MIME_TYPE]["evaluation_kind"] == "user_bsl"
        )  # type: ignore[index]
        assert api._poisoned_error is None
        assert session.capture_start_count == 1

        session.complete()
        outcome = capture.wait(
            timeout_s=1,
            evaluation_id=status.pending_evaluation_id,
        )
        assert outcome.state is CaptureEvaluationState.COMPLETED
        assert outcome.evaluation_id == status.pending_evaluation_id
        assert session.capture_start_count == 1
    finally:
        release_initiator.set()
        if session.capture_pending is not None:
            session.complete()
        initiator.join(1)
        if contender.ident is not None:
            contender.join(1)
        close_owner(controller, session)


def test_jupyter_magic_keyboard_interrupt_detaches_core_ticket_and_keeps_late_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from onec_runtime.capture_evaluation import (
        CaptureEvaluationState,
        CaptureEvaluationTicket,
        CapturePhase,
    )
    from onec_runtime_jupyter.extension import OnecRuntimeMagics, install_runtime

    session = ControlledCaptureSession()
    controller = captured_controller(session, command_timeout_s=1)
    api = PrototypeRuntimeApi(
        controller,
        notebook_worker_builder=_notebook_worker_builder(tmp_path),
    )
    runtime = _session_backed_notebook_runtime(api)
    shell = _NotebookShell()
    install_runtime(shell, runtime)
    magic = OnecRuntimeMagics(shell)  # type: ignore[arg-type]
    caller = current_thread()
    original_wait = CaptureEvaluationTicket.wait_initiator

    def interrupting_wait(
        ticket: CaptureEvaluationTicket,
        timeout_s: float | None = None,
    ) -> object:
        assert session.accepted.wait(1)
        owner = ticket._coordinator
        original_condition_wait = owner._condition.wait

        def interrupt_after_acknowledgement(
            wait_timeout: float | None = None,
        ) -> bool:
            if current_thread() is caller:
                raise KeyboardInterrupt
            return original_condition_wait(wait_timeout)

        monkeypatch.setattr(
            owner._condition,
            "wait",
            interrupt_after_acknowledgement,
        )
        try:
            return original_wait(ticket, timeout_s)
        finally:
            monkeypatch.setattr(owner._condition, "wait", original_condition_wait)

    monkeypatch.setattr(
        CaptureEvaluationTicket,
        "wait_initiator",
        interrupting_wait,
    )
    try:
        with pytest.raises(KeyboardInterrupt):
            magic.bsl("", "РезультатИнструкции = 903;")

        capture = runtime.current_capture()
        status = capture.status()
        assert status.phase is CapturePhase.EVALUATING
        assert status.evaluation_kind.value == "user_bsl"
        assert status.evaluation_timing is not None
        assert status.evaluation_timing.initiating_waiter_detached_ms is not None
        assert api._poisoned_error is None
        assert session.capture_start_count == 1

        session.complete()
        outcome = capture.wait(
            timeout_s=1,
            evaluation_id=status.pending_evaluation_id,
        )
        assert outcome.state is CaptureEvaluationState.COMPLETED
        assert outcome.evaluation_id == status.pending_evaluation_id
        assert session.capture_start_count == 1
    finally:
        if session.capture_pending is not None:
            session.complete()
        close_owner(controller, session)


def paths(api):
    return {entry[0] for entry in api._controller.lowerer.worker_export_identity}


def test_notebook_worker_message_reaches_calling_cell(tmp_path):
    api, _controller, session = runtime(tmp_path)
    loaded = api.execute_bsl(
        'Процедура Показать()\n    Сообщить("Успех");\nКонецПроцедуры'
    )
    assert loaded.succeeded
    artifact = api._worker_generation_diagnostics[
        api.worker_generation_handle.manifest_sha256
    ][0]
    assert 'RuntimeKernelServer.ДобавитьСообщение(' in artifact.mapped_source.text

    session.message_values = ["Успех"]
    called = api.execute_bsl('Показать();')

    assert called.messages == ("Успех",)
    sent = [value for operation, value in session.calls
            if operation == 'modify' and value[0] == 'ТекущаяИнструкция']
    assert '__OnecWorkerMessageSink' in sent[-1][1]
    assert '__onec_cell_messages_' in sent[-1][1]


def test_notebook_method_reads_current_persistent_variable_across_cells(tmp_path):
    api, _controller, session = runtime(tmp_path)
    assert api.execute_bsl('А = 100;').succeeded
    assert api.execute_bsl(
        'Функция ПолучитьА()\n    Возврат А;\nКонецФункции'
    ).succeeded

    artifact = api._worker_generation_diagnostics[
        api.worker_generation_handle.manifest_sha256
    ][0]
    mapped = artifact.mapped_source
    assert 'Возврат __OnecNotebookGlobals.А;' in mapped.text
    origin = mapped.source_map.map_offset(mapped.text.index('.А;', mapped.text.index('Возврат')) + 1)
    assert origin.unit is not None
    assert origin.unit.kind is SourceUnitKind.NOTEBOOK_CELL

    assert api.execute_bsl('Сообщить(ПолучитьА());').succeeded
    assert api.execute_bsl('А = 200;').succeeded
    assert api.execute_bsl('Сообщить(ПолучитьА());').succeeded
    sent = [value for operation, value in session.calls
            if operation == 'modify' and value[0] == 'ТекущаяИнструкция']
    assert sum('ПолучитьА()' in value[1] for value in sent) == 2
    for value in sent[-2:]:
        assert '__OnecNotebookBoundGlobals.Вставить(' in value[1]
        assert 'Контекст.А);' in value[1]
        assert value[1].count(
            '__OnecNotebookGlobals = __OnecNotebookPreviousGlobals;'
        ) == 2


def test_notebook_method_local_assignment_does_not_bind_global(tmp_path):
    api, _controller, _session = runtime(tmp_path)
    assert api.execute_bsl('А = 100;').succeeded
    assert api.execute_bsl(
        'Функция ЛокальнаяА()\n'
        '    А = 1;\n'
        '    Возврат А;\n'
        'КонецФункции'
    ).succeeded
    artifact = api._worker_generation_diagnostics[
        api.worker_generation_handle.manifest_sha256
    ][0]
    assert 'Возврат __OnecNotebookGlobals.А;' not in artifact.mapped_source.text
    assert 'Возврат А;' in artifact.mapped_source.text


def test_compile_invalid_worker_registration_is_not_recreated_by_value_guard(tmp_path):
    class CompileFailingTarget(_UniverseInstructionExecutor):
        fail_next_create = False

        def __call__(self, source):
            if self.fail_next_create and 'onec-worker-root-prepare-stage=' in source:
                self.fail_next_create = False
                marker = re.search(
                    r'onec-worker-artifact-stage=artifact_sha256=[0-9a-f]{64};'
                    r'logical_name_sha256=[0-9a-f]{64};phase=create;boundary=create',
                    source,
                )
                assert marker is not None
                self.last_error = BslExecutionError(
                    'onec-worker-root-prepare-stage=create\n'
                    + marker.group(0)
                    + '\nпо причине:\n'
                    + '{<Неизвестный модуль>(2,5)}: '
                    + 'Процедура не определена\n[ОшибкаКомпиляцииВстроенногоЯзыка]'
                )
                raise self.last_error
            return super().__call__(source)

    target = CompileFailingTarget()
    api, _controller, _session = runtime(tmp_path, target=target)
    assert api.execute_bsl('Процедура Показать()\nКонецПроцедуры').succeeded
    before = set(api._worker_universe_target.privacy_registration_snapshot())

    target.fail_next_create = True
    failed = api.execute_bsl(
        'Процедура Показать()\n    НеизвестнаяОперация();\nКонецПроцедуры'
    )

    assert not failed.succeeded
    assert failed.diagnostic is not None
    from onec_runtime.server_worker import worker_artifact_stage_failure
    from onec_runtime.bsl.diagnostics import parse_platform_diagnostic
    assert worker_artifact_stage_failure(target.last_error) is not None
    assert parse_platform_diagnostic(str(target.last_error)).has_compilation_marker
    assert api._worker_universe_target._compile_failed_registrations
    registrations = api._worker_universe_target.privacy_registration_snapshot()
    assert set(registrations) == before
    assert len(api._worker_universe_target._registrations) > len(registrations)
    before_validation = list(target.sources)
    assert api.validate_value_reference("Контекст.Число") == "Контекст.Число"
    assert target.sources == before_validation


@pytest.mark.parametrize('prepared', [False, True])
def test_notebook_worker_message_reaches_capture_cell(tmp_path, prepared):
    api, _controller, session = runtime(tmp_path, captured=True)
    loaded = api.execute_bsl(
        'Процедура Показать()\n    Сообщить("Успех");\nКонецПроцедуры'
    )
    assert loaded.succeeded
    api.execute_bsl('Результат = Capture();')

    session.message_values = ["Успех"]
    if prepared:
        candidate = api.prepare_capture_hypothesis('Показать();')
        called = api.execute_prepared_capture_hypothesis(candidate)
    else:
        called = api.execute_bsl('Показать();')

    assert called.succeeded
    assert called.messages == ("Успех",)
    evaluations = [
        str(value) for operation, value in session.calls if operation == 'evaluate'
    ]
    capture_source = next(
        source for source in reversed(evaluations)
        if 'ВыполнитьКодВКонтекстеОтладки' in source
    )
    assert '__OnecWorkerMessageSink' in capture_source


@pytest.mark.parametrize('prepared', [False, True])
def test_upsert_preserves_caller_and_distinct_source_origins(tmp_path, prepared):
    """Replacing B must retain A and its original source, in both entry paths."""
    api, controller, session = runtime(tmp_path)
    api.execute_bsl(PAIR)
    if prepared:
        candidate = api.prepare_main_for_capture(UPDATE)
        activated = api.activate_prepared_main_for_capture(candidate)
        api.execute_prepared_main_for_capture(activated)
    else:
        api.execute_bsl(UPDATE)
    assert paths(api) == {'а', 'б'}
    artifacts = api._worker_generation_diagnostics[api.worker_generation_handle.manifest_sha256]
    mapped = artifacts[0].mapped_source
    caller_unit = mapped.source_map.map_offset(mapped.text.index('Возврат Б')).unit
    helper_unit = mapped.source_map.map_offset(mapped.text.index('Возврат 2')).unit
    assert caller_unit is not None and helper_unit is not None
    assert caller_unit.unit_id == helper_unit.unit_id
    assert (caller_unit.revision, helper_unit.revision) == (1, 2)
    assert caller_unit.source_sha256 != helper_unit.source_sha256
    # The retained method must be callable via the notebook bare-name receiver.
    assert api.execute_bsl('Результат = А();').succeeded
    sent = [value for operation, value in session.calls if operation == 'modify' and value[0] == 'ТекущаяИнструкция']
    assert '.Получить(""Worker"").А()' in sent[-1][1]


def test_failed_candidate_does_not_reappear_on_later_upsert(tmp_path):
    target = _SemanticSnapshotFailureTarget()
    api, _, _ = runtime(tmp_path, target=target)
    api.execute_bsl(PAIR)
    previous = api.worker_generation_handle
    target.failure = 'swap_guard'
    with pytest.raises(BslExecutionError):
        api.execute_bsl(THIRD)
    assert api.worker_generation_handle is previous
    target.failure = None
    api.execute_bsl(UPDATE)
    assert paths(api) == {'а', 'б'}


@pytest.mark.parametrize('notebook_first', [False, True])
def test_named_and_notebook_writers_retain_each_others_complete_catalog(tmp_path, notebook_first):
    api, _, _ = runtime(tmp_path)
    catalog = _common_module_catalog('МодульА')
    if notebook_first:
        api.execute_bsl(PAIR)
    api.load_worker_modules((_worker_module_unit('МодульА', 1, catalog),), common_modules=catalog)
    if not notebook_first:
        api.execute_bsl(PAIR)
    assert paths(api) == {'модульа.версия', 'а', 'б'}
    api.load_worker_modules((_worker_module_unit('МодульА', 2, catalog),), common_modules=catalog)
    assert paths(api) == {'модульа.версия', 'а', 'б'}
    api.execute_bsl(UPDATE)
    assert paths(api) == {'модульа.версия', 'а', 'б'}
    assert {item.logical_name for item in api._worker_universe.active_manifest.modules} == {'МодульА', 'Worker'}


def test_reserved_worker_module_is_rejected_before_first_publication(tmp_path):
    api, _, _ = runtime(tmp_path)
    catalog = _common_module_catalog('Worker')
    with pytest.raises(ProtocolError, match='reserved|зарезервирован'):
        api.load_worker_modules((_worker_module_unit('Worker', 1, catalog),), common_modules=catalog)
    assert api.worker_generation_handle is None


@pytest.mark.parametrize('prepared', [False, True])
def test_capture_evaluation_uses_new_generation_while_original_main_pin_survives(tmp_path, prepared):
    api, controller, session = runtime(tmp_path, captured=True)
    api.execute_bsl(UPDATE)
    g1 = api.worker_generation_handle
    stopped = api.execute_bsl('Результат = Б();')
    original = api._operation_generation_pin
    assert original.handle is g1
    api.execute_bsl('Функция Б(Параметр)\nВозврат Параметр;\nКонецФункции')
    g2 = api.worker_generation_handle
    if prepared:
        candidate = api.prepare_capture_hypothesis('РезультатИнструкции = Б(22);')
        api.execute_prepared_capture_hypothesis(candidate)
    else:
        api.execute_bsl('РезультатИнструкции = Б(22);')
    expressions = [value[0] if isinstance(value, tuple) else value
                   for operation, value in session.calls if operation == 'evaluate']
    executed = [text for text in expressions
                if 'ВыполнитьКодТекущегоКонтекстаОтладки' in text
                or 'ВыполнитьКодВКонтекстеОтладки' in text]
    assert g2.manifest_sha256 in executed[-1]
    assert '__OnecPinnedWorkerGeneration = Контекст.RuntimeWorkerActiveGeneration;' in executed[-1]
    assert api._operation_generation_pin is original
    resumed = api.resume_capture()
    assert resumed.operation_id == stopped.operation_id and resumed.stop_sequence == 2
    assert api._operation_generation_pin is original


def test_prepared_capture_becomes_stale_after_notebook_publication(tmp_path):
    api, _, _ = runtime(tmp_path, captured=True)
    api.execute_bsl(UPDATE)
    api.execute_bsl('Результат = Б();')
    candidate = api.prepare_capture_hypothesis('РезультатИнструкции = Б();')
    api.execute_bsl('Функция Б()\nВозврат 44;\nКонецФункции')
    with pytest.raises(ProtocolError, match='stale'):
        api.execute_prepared_capture_hypothesis(candidate)


@pytest.mark.parametrize("uncertain", [False, True])
def test_prepared_capture_coordinator_owns_pin_after_waiter_timeout(tmp_path, monkeypatch, uncertain):
    from dataclasses import replace
    from threading import Event, Thread, current_thread
    from onec_runtime.capture_evaluation import CaptureEvaluationCoordinator, CapturePhase
    from onec_runtime.errors import CaptureEvaluationPendingError
    from onec_runtime.prototype_runtime import CaptureCellResult
    from test_capture_evaluation_coordinator import Driver, FENCE

    api, controller, _ = runtime(tmp_path, captured=True)
    api.execute_bsl(UPDATE)
    api.execute_bsl('Результат = Б();')
    prepared = api.prepare_capture_hypothesis('РезультатИнструкции = Б();')
    original_pin = api._operation_generation_pin
    coordinator = CaptureEvaluationCoordinator(FENCE, poll_interval_s=0.01)
    driver = Driver()
    tickets = []
    caller_finished = Event()
    errors = []
    pin_dispositions = []
    for method_name in ('release_pin', 'retain_outcome_unknown'):
        original = getattr(api._worker_universe, method_name)
        def checked(pin, original=original, method_name=method_name):
            assert not api._lock.locked()
            assert not api._evaluation_pin_lock.locked()
            assert not api._worker_universe_target._lock._is_owned()
            pin_dispositions.append(method_name)
            return original(pin)
        monkeypatch.setattr(api._worker_universe, method_name, checked)
    def submit(*, pin_lease, completion):
        assert api._writer_owner == current_thread().ident
        assert api._lock.locked(), "submission must retain runtime writer"
        assert api._evaluation_generation_pin is None, "pin slot must detach before dispatch"
        def settle(value, error):
            return completion(CaptureCellResult(controller.operation_id, "visible", "lowered", value), error)
        ticket = coordinator.submit_evaluation(replace(
            driver.request(cleanup_leases=()), completion=settle, pin_lease=pin_lease,
        ))
        tickets.append(ticket)
        return ticket
    monkeypatch.setattr(api, '_execute_prepared_capture_handoff', lambda handoff: handoff.execute_owned(submit))
    # The real public ticket wait detaches; only its default timeout is shortened.
    from onec_runtime.capture_evaluation import CaptureEvaluationTicket
    wait = CaptureEvaluationTicket.wait_initiator
    monkeypatch.setattr(CaptureEvaluationTicket, 'wait_initiator', lambda ticket, timeout_s=None: wait(ticket, 0.01))
    def caller():
        try:
            api.execute_prepared_capture_hypothesis(prepared)
        except BaseException as error:
            errors.append(error)
        finally:
            caller_finished.set()
    thread = Thread(target=caller)
    thread.start()
    try:
        assert caller_finished.wait(1)
        assert len(errors) == 1 and isinstance(errors[0], CaptureEvaluationPendingError), errors
        assert api._poisoned_error is None
        assert len(api._worker_universe._leases) == 2
        assert api._evaluation_generation_pin is None
        assert api._operation_generation_pin is original_pin
        if uncertain:
            driver.events.put(OSError('private disconnected stream'))
        else:
            driver.result()
        outcome = coordinator.wait(FENCE, tickets[0].evaluation_id, timeout_s=1)
        if uncertain:
            assert coordinator.status(FENCE).phase == CapturePhase.RECOVERY_REQUIRED
            assert sum(lease.outcome_unknown for lease in api._worker_universe._leases.values()) == 1
        else:
            assert outcome.result == 42
            assert len(api._worker_universe._leases) == 1
        assert pin_dispositions == ['retain_outcome_unknown' if uncertain else 'release_pin']
    finally:
        coordinator.begin_close()
        driver.closed.set()
        assert coordinator.join(2)
        thread.join(2)
        assert not thread.is_alive()


@pytest.mark.parametrize("detach", ["timeout", "interrupt", "attached"])
def test_owned_late_bsl_failure_records_dirty_roots_before_paused(tmp_path, monkeypatch, detach):
    from dataclasses import replace
    from threading import current_thread
    from onec_runtime.capture_evaluation import CaptureEvaluationCoordinator, CaptureEvaluationState, CapturePhase
    from onec_runtime.errors import CaptureEvaluationPendingError
    from onec_runtime.prototype_runtime import CaptureCellResult
    from test_capture_evaluation_coordinator import Driver, FENCE

    api, controller, _ = runtime(tmp_path, captured=True)
    api.execute_bsl(UPDATE)
    api.execute_bsl('Результат = Б();')
    context_before = controller.lowerer.persistent_names
    prepared = api.prepare_capture_hypothesis(
        'НовоеЗначение = 12; КонтекстОтладки.Скаляр = 778; ВызватьИсключение "synthetic failure";'
    )
    coordinator = CaptureEvaluationCoordinator(FENCE, poll_interval_s=0.01)
    driver = Driver()
    tickets, pin_observations = [], []
    class Waiter:
        def __init__(self, ticket):
            self.ticket = ticket
        def wait_initiator(self):
            assert driver.polling.wait(1)
            if detach == "attached":
                driver.result(failed=True)
            return self.ticket.wait_initiator(1 if detach == "attached" else 0.01)
    def submit(*, pin_lease, completion):
        def settle(value, error):
            return completion(CaptureCellResult(controller.operation_id, "visible", "lowered", value), error)
        def dispose(disposition):
            pin_observations.append((
                disposition, tuple(api._pending_dirty_roots.values()),
                controller.lowerer.persistent_names, coordinator.status(FENCE).phase,
            ))
            pin_lease(disposition)
        ticket = coordinator.submit_evaluation(replace(
            driver.request(cleanup_leases=()), completion=settle, pin_lease=dispose,
        ))
        tickets.append(ticket)
        return Waiter(ticket)
    monkeypatch.setattr(api, '_execute_prepared_capture_handoff', lambda handoff: handoff.execute_owned(submit))
    caller_thread = current_thread()
    original_wait = coordinator._condition.wait
    def interrupt_wait(timeout=None):
        if current_thread() is caller_thread:
            raise KeyboardInterrupt()
        return original_wait(timeout)
    try:
        if detach == "interrupt":
            monkeypatch.setattr(coordinator._condition, 'wait', interrupt_wait)
        if detach == "attached":
            reply = api.execute_prepared_capture_hypothesis(prepared)
            assert not reply.succeeded
            assert reply.capture_dirty_roots == ("Скаляр",)
            assert reply.changed_roots == ("НовоеЗначение",)
        else:
            with pytest.raises(KeyboardInterrupt if detach == "interrupt" else CaptureEvaluationPendingError):
                api.execute_prepared_capture_hypothesis(prepared)
        monkeypatch.setattr(coordinator._condition, 'wait', original_wait)
        if detach != "attached":
            assert not api._pending_dirty_roots
            driver.result(failed=True)
        outcome = coordinator.wait(FENCE, tickets[0].evaluation_id, timeout_s=1)
        assert outcome.state is CaptureEvaluationState.FAILED
        assert outcome.diagnostic.code == "bsl_error"
        assert pin_observations == [("release", ("Скаляр",), context_before, CapturePhase.EVALUATING)]
        assert tuple(api._pending_dirty_roots.values()) == ("Скаляр",)
        assert coordinator.status(FENCE).phase is CapturePhase.PAUSED
        assert len(api._worker_universe._leases) == 1
    finally:
        coordinator.begin_close()
        driver.closed.set()
        assert coordinator.join(2)


@pytest.mark.parametrize("window", ["notify", "adapter"])
@pytest.mark.parametrize("error_type", [KeyboardInterrupt, RuntimeError])
@pytest.mark.parametrize("late", ["success", "bsl_failure", "uncertain"])
def test_owned_submit_interruption_keeps_accepted_record_owner(tmp_path, monkeypatch, window, error_type, late):
    from dataclasses import replace
    from threading import current_thread
    from onec_runtime.capture_evaluation import CaptureEvaluationCoordinator, CapturePhase
    from onec_runtime.prototype_runtime import CaptureCellResult
    from test_capture_evaluation_coordinator import Driver, FENCE

    api, controller, _ = runtime(tmp_path, captured=True)
    api.execute_bsl(UPDATE)
    api.execute_bsl('Результат = Б();')
    context_before = controller.lowerer.persistent_names
    prepared = api.prepare_capture_hypothesis('НовоеЗначение = 12; КонтекстОтладки.Скаляр = 778;')
    coordinator = CaptureEvaluationCoordinator(FENCE, poll_interval_s=.01)
    driver = Driver()
    initiating = current_thread()
    notify = coordinator._condition.notify_all
    completions, disposals, continuations, cleanups = [], [], [], []

    def interrupted_notify():
        notify()
        if current_thread() is initiating:
            raise error_type('interrupted after acceptance')

    def submit(*, pin_lease, completion):
        def settle(value, error):
            completions.append((current_thread(), error))
            return completion(CaptureCellResult(controller.operation_id, 'visible', 'lowered', value), error)
        # An adapter may wrap either callback. Acceptance must survive this.
        def dispose(disposition):
            assert not api._lock.locked()
            assert not api._evaluation_pin_lock.locked()
            disposals.append(disposition)
            pin_lease(disposition)
        ticket = coordinator.submit_evaluation(replace(
            driver.request(cleanup_leases=(lambda: cleanups.append(current_thread()),)),
            pin_lease=dispose, completion=settle,
            continuation=lambda value: continuations.append(value),
        ))
        if window == 'adapter':
            raise error_type('interrupted after acceptance')
        return ticket

    monkeypatch.setattr(api, '_execute_prepared_capture_handoff', lambda handoff: handoff.execute_owned(submit))
    try:
        if window == 'notify':
            monkeypatch.setattr(coordinator._condition, 'notify_all', interrupted_notify)
        with pytest.raises(error_type, match='after acceptance'):
            api.execute_prepared_capture_hypothesis(prepared)
        monkeypatch.setattr(coordinator._condition, 'notify_all', notify)
        assert driver.polling.wait(1)
        record = coordinator._active
        assert record is not None and record.acknowledged
        assert not record.initiator_attached
        assert len(api._worker_universe._leases) == 2
        assert not disposals and not completions and not cleanups
        assert controller.lowerer.persistent_names != context_before
        if late == 'uncertain':
            driver.events.put(OSError('synthetic stream loss'))
        else:
            driver.result(failed=late == 'bsl_failure')
        outcome = coordinator.wait(FENCE, record.evaluation_id, timeout_s=1)
        assert driver.dispatch_count == 1
        assert not continuations
        assert len(completions) == 1 and completions[0][0] is coordinator._worker
        if late == 'uncertain':
            assert coordinator.status(FENCE).phase is CapturePhase.RECOVERY_REQUIRED
            assert disposals == ['quarantine']
            assert sum(lease.outcome_unknown for lease in api._worker_universe._leases.values()) == 1
            assert not cleanups
        else:
            assert outcome.state.value == ('failed' if late == 'bsl_failure' else 'completed')
            assert tuple(api._pending_dirty_roots.values()) == ('Скаляр',)
            assert coordinator.status(FENCE).phase is CapturePhase.PAUSED
            assert disposals == ['release'] and len(api._worker_universe._leases) == 1
            assert cleanups == [coordinator._worker]
        if late != 'success':
            assert controller.lowerer.persistent_names == context_before
    finally:
        monkeypatch.setattr(coordinator._condition, 'notify_all', notify)
        coordinator.begin_close()
        driver.closed.set()
        assert coordinator.join(2)


def test_mixed_capture_added_name_uses_new_catalog_and_releases_cell_lease(tmp_path):
    api, _, session = runtime(tmp_path, captured=True)
    api.execute_bsl(UPDATE)
    api.execute_bsl('Результат = Б();')
    original = api._operation_generation_pin
    reply = api.execute_bsl(THIRD + '\nРезультатИнструкции = В();')
    assert reply.succeeded
    assert paths(api) == {'б', 'в'}
    assert api._operation_generation_pin is original
    assert api._evaluation_generation_pin is None
    assert len(api._worker_universe._leases) == 1
    expressions = [str(value) for operation, value in session.calls if operation == 'evaluate']
    assert any(api.worker_generation_handle.manifest_sha256 in text and '.В()' in text for text in expressions)


@pytest.mark.parametrize('prepared', [False, True])
def test_known_capture_failure_releases_evaluation_lease_and_remains_captured(tmp_path, prepared):
    api, controller, session = runtime(tmp_path, captured=True)
    api.execute_bsl(UPDATE)
    api.execute_bsl('Результат = Б();')
    original = api._operation_generation_pin
    session.capture_evaluations.append(evaluation('Ошибка', '', error='planned division by zero'))
    source = 'РезультатИнструкции = 1 / 0;'
    reply = (api.execute_prepared_capture_hypothesis(api.prepare_capture_hypothesis(source))
             if prepared else api.execute_bsl(source))
    assert not reply.succeeded
    assert controller.state.value == 'captured'
    assert api._operation_generation_pin is original
    assert len(api._worker_universe._leases) == 1
    assert api.execute_bsl('РезультатИнструкции = Б();').succeeded


@pytest.mark.parametrize('prepared', [False, True])
def test_unknown_capture_reply_retains_both_generation_leases_until_close(tmp_path, prepared):
    from onec_runtime.capture_evaluation import (
        CaptureEvaluationState,
        CapturePhase,
    )
    from onec_runtime.prototype_runtime import OperationState

    api, _, session = runtime(tmp_path, captured=True)
    api.execute_bsl(UPDATE)
    api.execute_bsl('Результат = Б();')
    original = api._operation_generation_pin
    api.execute_bsl(THIRD)
    source = 'РезультатИнструкции = В();'
    candidate = api.prepare_capture_hypothesis(source) if prepared else None
    session.lose_capture_reply = True
    with pytest.raises(CaptureOutcomeUnknownError) as caught:
        if prepared:
            api.execute_prepared_capture_hypothesis(candidate)
        else:
            api.execute_bsl(source)
    assert caught.value.diagnostic is not None
    assert caught.value.diagnostic.code == 'dispatch_uncertain'
    assert api._operation_generation_pin is original
    assert len(api._worker_universe._leases) == 2
    assert sum(lease.outcome_unknown for lease in api._worker_universe._leases.values()) == 1
    assert api.status().state is OperationState.RECOVERING
    capture = api.current_capture()
    capture_status = capture.status()
    outcome = capture.wait(timeout_s=0)
    assert capture_status.phase is CapturePhase.OUTCOME_UNKNOWN
    assert outcome.state is CaptureEvaluationState.UNKNOWN
    assert outcome.evaluation_id == caught.value.evaluation_id
    session.lose_capture_reply = False
    api.close()
    assert not api._worker_universe._leases
    assert api._notebook_method_set is None


@pytest.mark.parametrize('prepared', [False, True])
def test_main_stale_or_failed_preparation_does_not_commit_an_extra_name(tmp_path, prepared):
    api, _, _ = runtime(tmp_path)
    api.execute_bsl(PAIR)
    if prepared:
        candidate = api.prepare_main_for_capture(THIRD)
        api.execute_bsl(UPDATE)
        with pytest.raises(ProtocolError, match='stale'):
            api.activate_prepared_main_for_capture(candidate)
        with pytest.raises(ProtocolError, match='consumed'):
            api.activate_prepared_main_for_capture(candidate)
    else:
        reply = api.execute_bsl(THIRD + '\nКонтекстОтладки.Значение = 1;')
        assert not reply.succeeded
        api.execute_bsl(UPDATE)
    assert paths(api) == {'а', 'б'}


def test_prepared_capture_dispatch_admission_failure_releases_only_evaluation_lease(tmp_path, monkeypatch):
    api, _, _ = runtime(tmp_path, captured=True)
    api.execute_bsl(UPDATE)
    api.execute_bsl('Результат = Б();')
    original = api._operation_generation_pin
    candidate = api.prepare_capture_hypothesis('РезультатИнструкции = Б();')

    def reject_dispatch(*args, **kwargs):
        raise ProtocolError('planned dispatch admission failure')

    monkeypatch.setattr(api, '_require_operation_pin_dispatch_fence_locked', reject_dispatch)
    with pytest.raises(ProtocolError, match='dispatch admission'):
        api.execute_prepared_capture_hypothesis(candidate)
    assert api._operation_generation_pin is original
    assert len(api._worker_universe._leases) == 1


@pytest.mark.parametrize("error_type", [ProtocolError, BslExecutionError])
def test_owned_capture_submission_rejection_restores_prepared_namespace(tmp_path, monkeypatch, error_type):
    api, controller, _ = runtime(tmp_path, captured=True)
    api.execute_bsl(UPDATE)
    api.execute_bsl('Результат = Б();')
    context_before = controller.lowerer.persistent_names
    prepared = api.prepare_capture_hypothesis('НовоеЗначение = 12; КонтекстОтладки.Скаляр = 778;')
    def reject(**kwargs):
        raise error_type('rejected before record ownership')
    monkeypatch.setattr(api, '_execute_prepared_capture_handoff', lambda handoff: handoff.execute_owned(reject))
    if error_type is ProtocolError:
        with pytest.raises(ProtocolError, match='before record ownership'):
            api.execute_prepared_capture_hypothesis(prepared)
    else:
        assert not api.execute_prepared_capture_hypothesis(prepared).succeeded
    assert controller.lowerer.persistent_names == context_before
    assert not api._pending_dirty_roots
    assert len(api._worker_universe._leases) == 1


def test_owned_capture_rejection_disposes_pin_even_when_completion_fails():
    from contextlib import nullcontext
    from onec_runtime.runtime_api import _PreparedCaptureExecution
    dispositions = []
    def fail_completion(result, error):
        raise RuntimeError('local completion failed')
    def reject(**kwargs):
        raise ProtocolError('rejected before record ownership')
    handoff = _PreparedCaptureExecution(lambda: None, lambda: dispositions.append,
                                        fail_completion, nullcontext)
    with pytest.raises(RuntimeError, match='local completion failed'):
        handoff.execute_owned(reject)
    assert dispositions == ['release']


@pytest.mark.parametrize('prepared', [False, True])
def test_capture_evaluation_stop_requires_recovery_and_quarantines_g2(
    tmp_path, prepared,
):
    api, controller, session = runtime(tmp_path, captured=True)
    api.execute_bsl(UPDATE)
    api.execute_bsl('Результат = Б();')
    original = api._operation_generation_pin
    api.execute_bsl(THIRD)
    user_stop = ScriptedSession((USER,)).stops[0]
    session.pending_evaluation_stops.append(user_stop)
    source = 'РезультатИнструкции = В();'
    with pytest.raises(CaptureRecoveryRequiredError) as caught:
        if prepared:
            api.execute_prepared_capture_hypothesis(api.prepare_capture_hypothesis(source))
        else:
            api.execute_bsl(source)
    assert caught.value.diagnostic is not None
    assert caught.value.diagnostic.code == 'unexpected_stop'
    assert controller.state.value == 'recovering'
    assert api._evaluation_generation_pin is None
    assert len(api._worker_universe._leases) == 2
    assert sum(lease.outcome_unknown for lease in api._worker_universe._leases.values()) == 1
    assert api._operation_generation_pin is original
    with pytest.raises(CaptureRecoveryRequiredError):
        controller.resume_debug_stop()
    assert session.continue_count == 1


@pytest.mark.parametrize('statements_only', [False, True])
def test_retained_original_generation_rejects_conflicting_explicit_source_identity(tmp_path, statements_only):
    api, _, _ = runtime(tmp_path, captured=True)
    unit = SourceUnitRef(SourceUnitKind.NOTEBOOK_CELL, 'stable-cell', 1, source_sha256(UPDATE))
    api.execute_bsl(UPDATE, source_unit=unit)
    api.execute_bsl('Результат = Б();')
    api.execute_bsl(UPDATE.replace('Возврат 2', 'Возврат 3'))
    active = api.worker_generation_handle
    source = 'РезультатИнструкции = 4;' if statements_only else UPDATE.replace('Возврат 2', 'Возврат 4')
    conflict = SourceUnitRef(SourceUnitKind.NOTEBOOK_CELL, 'stable-cell', 1, source_sha256(source))
    with pytest.raises(ProtocolError, match='source identit'):
        api.execute_bsl(source, source_unit=conflict)
    assert api.worker_generation_handle is active


def test_unused_prepared_candidate_owns_source_identity_until_discard(tmp_path):
    api, _, _ = runtime(tmp_path)
    unit = SourceUnitRef(SourceUnitKind.NOTEBOOK_CELL, 'prepared-cell', 1, source_sha256(UPDATE))
    prepared = api.prepare_main_for_capture(UPDATE, source_unit=unit)
    conflict = SourceUnitRef(SourceUnitKind.NOTEBOOK_CELL, 'prepared-cell', 1, source_sha256(THIRD))
    with pytest.raises(ProtocolError, match='source identit'):
        api.execute_bsl(THIRD, source_unit=conflict)
    api.discard_prepared_main_for_capture(prepared)
    assert api.execute_bsl(THIRD, source_unit=conflict).succeeded


@pytest.mark.parametrize('captured', [False, True])
def test_mixed_provenance_is_saved_before_publication_and_matches_final_dispatch(tmp_path, captured, monkeypatch):
    api, controller, _ = runtime(tmp_path, captured=captured)
    api.execute_bsl(UPDATE)
    if captured:
        api.execute_bsl('Результат = Б();')
    previous = api.worker_generation_handle
    dispatched = []
    name = 'execute_mapped_capture' if captured else 'execute_mapped_main'
    original = getattr(controller, name)

    def observe_dispatch(visible, mapped, **kwargs):
        dispatched.append(mapped)
        return original(visible, mapped, **kwargs)

    monkeypatch.setattr(controller, name, observe_dispatch)
    saved = []

    def persist(provenance):
        assert api.worker_generation_handle is previous
        assert not dispatched
        saved.append(provenance)

    statement = 'РезультатИнструкции = В();' if captured else 'Результат = В();'
    assert api.execute_bsl(THIRD + '\n' + statement, on_execution_provenance=persist).succeeded
    assert len(saved) == len(dispatched) == 1
    assert saved[0].executed_source_sha256 == dispatched[0].artifact.source_sha256
    assert saved[0].source_map_sha256 == dispatched[0].source_map_sha256


def test_prepared_main_provenance_matches_final_generation_prelude(tmp_path, monkeypatch):
    api, controller, _ = runtime(tmp_path)
    api.execute_bsl(UPDATE)
    candidate = api.prepare_main_for_capture(THIRD + '\nРезультат = В();')
    provenance = api.prepared_main_execution_provenance(candidate)
    dispatched = []
    original = controller.execute_mapped_main

    def observe(visible, mapped, **kwargs):
        dispatched.append(mapped)
        return original(visible, mapped, **kwargs)

    monkeypatch.setattr(controller, 'execute_mapped_main', observe)
    activated = api.activate_prepared_main_for_capture(candidate)
    assert api.execute_prepared_main_for_capture(activated).succeeded
    assert provenance.executed_source_sha256 == dispatched[0].artifact.source_sha256
    assert provenance.source_map_sha256 == dispatched[0].source_map_sha256


def test_prepared_main_preview_is_read_only_and_fences_aborted_admission(tmp_path):
    target = _UniverseInstructionExecutor()
    api, _, _ = runtime(tmp_path, target=target)
    api.execute_bsl(UPDATE)
    active = api.worker_generation_handle
    methods = api._notebook_method_set
    before = tuple(target.sources)
    candidate = api.prepare_main_for_capture(THIRD + '\nРезультат = В();')
    payload = candidate.contents(api._prepared_main_owner)
    assert tuple(target.sources) == before
    assert api._worker_universe._pending is None
    intervening = api._worker_universe.prepare(
        api._notebook_publication_artifacts(payload.worker_artifact),
        export_catalog=api._notebook_effective_catalog(
            api._complete_notebook_catalog(payload.candidate_catalog)
        ),
    )
    api._worker_universe.discard(intervening)
    with pytest.raises(ProtocolError, match='preview is stale'):
        api.activate_prepared_main_for_capture(candidate)
    assert tuple(target.sources) == before
    assert api.worker_generation_handle is active
    assert api._notebook_method_set is methods
    assert api._worker_universe._pending is None
    assert paths(api) == {'б'}


@pytest.mark.parametrize('captured', [False, True])
def test_mixed_provenance_rejection_discards_candidate_before_target_mutation(tmp_path, captured):
    target = _UniverseInstructionExecutor()
    api, _, _ = runtime(tmp_path, target=target, captured=captured)
    api.execute_bsl(UPDATE)
    if captured:
        api.execute_bsl('Результат = Б();')
    active = api.worker_generation_handle
    original_pin = api._operation_generation_pin
    methods = api._notebook_method_set
    before = tuple(target.sources)
    observed = []

    def reject(provenance):
        observed.append(provenance)
        raise OSError('planned durable persistence failure')

    with pytest.raises(OSError, match='durable persistence'):
        api.execute_bsl(THIRD + '\nРезультат = В();', on_execution_provenance=reject)
    assert len(observed) == 1
    assert tuple(target.sources) == before
    assert api.worker_generation_handle is active
    assert api._notebook_method_set is methods
    assert api._operation_generation_pin is original_pin
    assert api._evaluation_generation_pin is None
    assert api._worker_universe._pending is None
    assert paths(api) == {'б'}
