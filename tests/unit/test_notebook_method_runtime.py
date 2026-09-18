"""Notebook Worker methods through the public post-bootstrap execution route."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from time import monotonic

import pytest

from onec_runtime.bsl import (
    CommonModuleCatalogSnapshot,
    CommonModuleDescriptor,
    CommonModuleScope,
    SourceUnitKind,
    SourceUnitRef,
    WorkerModuleUnit,
    mapped_visible_source,
    source_sha256,
)
from onec_runtime.errors import BslExecutionError, ProtocolError
from onec_runtime.execution.pipeline import StalePreparedCell
from onec_runtime.runtime_models import OperationState, RuntimeReplyKind

from test_execution_route_sequence import BUSINESS
from worker_debug_fixtures import public_notebook_runtime


PAIR = 'Функция А()\nВозврат Б();\nКонецФункции\nФункция Б()\nВозврат 1;\nКонецФункции'
UPDATE = 'Функция Б()\nВозврат 2;\nКонецФункции'
THIRD = 'Функция В()\nВозврат 3;\nКонецФункции'


@pytest.fixture
def runtime(tmp_path: Path):
    opened = []

    def create(*, captured: bool = False):
        api, composed, session = public_notebook_runtime(tmp_path, captured=captured)
        opened.append(api)
        return api, composed, session

    yield create
    for api in reversed(opened):
        # A rejected publication callback can leave its already admitted
        # ticket running after the caller receives the callback error.
        ticket = api._arbiter.active_ticket
        if ticket is not None:
            ticket.wait_settled(timeout=3.0)
        api.close()


def _exports(composed) -> set[str]:
    return {
        item.public_path.casefold()
        for item in composed.worker_activation.snapshot().worker_exports
    }


def _debug_view(composed):
    host = composed.worker_universe
    pin = host.pin_active()
    try:
        return host._operation_debug_view(pin)
    finally:
        host.release_pin(pin)


def _arm_capture(api) -> None:
    api.configure_capture_points((BUSINESS,))
    api.prepare_capture_ticket()


def _module(name: str, revision: int) -> tuple[WorkerModuleUnit, CommonModuleCatalogSnapshot]:
    source = (
        "Функция Версия() Экспорт\n"
        f"Возврат {revision};\n"
        "КонецФункции\n"
    )
    catalog = CommonModuleCatalogSnapshot.create(
        profile="server-test",
        preprocessor_profile="server",
        revision=revision,
        modules=(CommonModuleDescriptor(name, CommonModuleScope.SERVER),),
    )
    unit = SourceUnitRef(SourceUnitKind.MODULE, name, revision, source_sha256(source))
    return WorkerModuleUnit(name, "module", revision, mapped_visible_source(source, unit)), catalog


def test_notebook_worker_message_reaches_calling_main_cell(runtime) -> None:
    api, composed, session = runtime()
    loaded = api.execute_bsl(
        'Процедура Показать()\n    Сообщить("Успех");\nКонецПроцедуры'
    )
    assert loaded.kind is RuntimeReplyKind.WORKER_LOADED
    assert "__OnecWorkerMessage" in _debug_view(composed).modules[0].mapped_source.text

    session.message_values = ("Успех",)
    called = api.execute_bsl("Показать();")

    assert called.kind is RuntimeReplyKind.MAIN_COMPLETED
    assert called.messages == ("Успех",)
    assert any(
        name == "ТекущаяИнструкция" and "__OnecWorkerMessageSink" in source
        for name, source in session.command_writes
    )


@pytest.mark.parametrize("prepared", (False, True))
def test_notebook_worker_message_reaches_capture_cell(runtime, prepared) -> None:
    api, _composed, session = runtime(captured=True)
    assert api.execute_bsl(
        'Процедура Показать()\nСообщить("Успех");\nКонецПроцедуры'
    ).succeeded
    _arm_capture(api)
    assert api.execute_bsl("Результат = 1;").kind is RuntimeReplyKind.CAPTURED
    session.message_values = ("Успех",)
    if prepared:
        candidate = api.prepare_bsl("Показать();")
        reply = api.execute_prepared_bsl(candidate)
    else:
        reply = api.execute_bsl("Показать();")
    assert reply.messages == ("Успех",)
    assert reply.succeeded


def test_invalid_worker_method_does_not_enter_later_value_guard(runtime) -> None:
    api, composed, _session = runtime()
    failed = api.execute_bsl("Функция Б(\nКонецФункции")
    assert failed.succeeded is False
    assert api.status().worker_generation is None
    assert api.execute_bsl("Результат = 1;").succeeded
    assert _exports(composed) == set()


def test_notebook_method_reads_current_persistent_variable_across_main_cells(runtime) -> None:
    api, composed, session = runtime()
    assert api.execute_bsl("А = 100;").succeeded
    assert api.execute_bsl("Функция ПолучитьА()\nВозврат А;\nКонецФункции").succeeded
    mapped = _debug_view(composed).modules[0].mapped_source
    assert "Возврат __OnecNotebookGlobals.А;" in mapped.text
    origin = mapped.source_map.map_offset(mapped.text.index(".А;", mapped.text.index("Возврат")) + 1)
    assert origin.unit is not None
    assert origin.unit.kind is SourceUnitKind.NOTEBOOK_CELL

    assert api.execute_bsl("Результат = ПолучитьА();").succeeded
    assert api.execute_bsl("А = 200;").succeeded
    assert api.execute_bsl("Результат = ПолучитьА();").succeeded
    calls = [source for name, source in session.command_writes if name == "ТекущаяИнструкция"]
    assert sum("ПолучитьА()" in source for source in calls) == 2
    assert all("__OnecNotebookBoundGlobals.Вставить(" in source for source in calls[-2:])


def test_notebook_method_local_assignment_does_not_bind_global(runtime) -> None:
    api, composed, _ = runtime()
    assert api.execute_bsl("А = 100;").succeeded
    assert api.execute_bsl(
        "Функция ЛокальнаяА()\nА = 1;\nВозврат А;\nКонецФункции"
    ).succeeded
    source = _debug_view(composed).modules[0].mapped_source.text
    assert "Возврат __OnecNotebookGlobals.А;" not in source
    assert "Возврат А;" in source


@pytest.mark.parametrize("prepared", (False, True))
def test_upsert_preserves_caller_and_distinct_source_origins(runtime, prepared) -> None:
    api, composed, session = runtime()
    original = SourceUnitRef(SourceUnitKind.NOTEBOOK_CELL, "pair", 1, source_sha256(PAIR))
    updated = SourceUnitRef(SourceUnitKind.NOTEBOOK_CELL, "helper", 2, source_sha256(UPDATE))
    assert api.execute_bsl(PAIR, source_unit=original).succeeded
    if prepared:
        candidate = api.prepare_bsl(UPDATE, source_unit=updated)
        assert api.execute_prepared_bsl(candidate).succeeded
    else:
        assert api.execute_bsl(UPDATE, source_unit=updated).succeeded
    assert _exports(composed) == {"а", "б"}
    module = _debug_view(composed).modules[0]
    assert module.source_units == (original, updated)
    assert api.execute_bsl("Результат = А();").succeeded
    assert any(
        name == "ТекущаяИнструкция" and '.Получить(""Worker"").А()' in source
        for name, source in session.command_writes
    )


def test_failed_worker_candidate_does_not_reappear_on_later_upsert(runtime) -> None:
    api, composed, session = runtime()
    assert api.execute_bsl(PAIR).succeeded
    previous = api.status().worker_generation
    session.worker_target.fault = "create"
    with pytest.raises(BslExecutionError, match="platform create failure"):
        api.execute_bsl(THIRD)
    assert api.status().worker_generation is previous
    session.worker_target.fault = None
    assert api.execute_bsl(UPDATE).succeeded
    assert _exports(composed) == {"а", "б"}


@pytest.mark.parametrize("notebook_first", (False, True))
def test_named_and_notebook_writers_retain_complete_catalog(runtime, notebook_first) -> None:
    api, composed, _ = runtime()
    first, catalog = _module("МодульА", 1)
    if notebook_first:
        assert api.execute_bsl(PAIR).succeeded
    api.load_worker_modules((first,), common_modules=catalog)
    if not notebook_first:
        assert api.execute_bsl(PAIR).succeeded
    assert _exports(composed) == {"модульа.версия", "а", "б"}
    second, catalog = _module("МодульА", 2)
    api.load_worker_modules((second,), common_modules=catalog)
    assert api.execute_bsl(UPDATE).succeeded
    assert _exports(composed) == {"модульа.версия", "а", "б"}
    assert {module.logical_name for module in composed.worker_universe.active_manifest.modules} == {
        "МодульА", "Worker",
    }


def test_reserved_worker_module_is_rejected_before_first_publication(runtime) -> None:
    api, _composed, session = runtime()
    unit, catalog = _module("Worker", 1)
    with pytest.raises(ProtocolError, match="reserved|зарезервирован"):
        api.load_worker_modules((unit,), common_modules=catalog)
    assert api.status().worker_generation is None
    assert session.worker_instructions == []


@pytest.mark.parametrize("prepared", (False, True))
def test_known_capture_failure_keeps_stop_and_allows_repair(runtime, prepared) -> None:
    api, composed, session = runtime(captured=True)
    assert api.execute_bsl(UPDATE).succeeded
    _arm_capture(api)
    assert api.execute_bsl("Результат = Б();").kind is RuntimeReplyKind.CAPTURED
    original = api.status().worker_generation
    session.capture_error = "{<Неизвестный модуль>(1,25)}: Деление на ноль"
    source = "РезультатИнструкции = 1 / 0;"
    if prepared:
        candidate = api.prepare_bsl(source)
        failed = api.execute_prepared_bsl(candidate)
    else:
        failed = api.execute_bsl(source)
    assert failed.succeeded is False
    assert api.status().state is OperationState.CAPTURED
    assert api.status().worker_generation is original
    assert composed.execution.core.controller.capture_scope is not None
    assert api.execute_bsl("РезультатИнструкции = Б();").succeeded


def test_prepared_capture_becomes_stale_after_notebook_publication(runtime) -> None:
    api, _composed, _session = runtime(captured=True)
    assert api.execute_bsl(UPDATE).succeeded
    _arm_capture(api)
    assert api.execute_bsl("Результат = Б();").kind is RuntimeReplyKind.CAPTURED
    candidate = api.prepare_bsl("РезультатИнструкции = Б();")
    assert api.execute_bsl("Функция Б()\nВозврат 44;\nКонецФункции").succeeded
    with pytest.raises(StalePreparedCell, match="execution route changed"):
        api.execute_prepared_bsl(candidate)


@pytest.mark.parametrize("statements_only", (False, True))
def test_retained_generation_rejects_conflicting_explicit_source_identity(
    runtime, statements_only,
) -> None:
    api, _composed, _session = runtime(captured=True)
    unit = SourceUnitRef(SourceUnitKind.NOTEBOOK_CELL, "stable-cell", 1, source_sha256(UPDATE))
    assert api.execute_bsl(UPDATE, source_unit=unit).succeeded
    _arm_capture(api)
    assert api.execute_bsl("Результат = Б();").kind is RuntimeReplyKind.CAPTURED
    assert api.execute_bsl(UPDATE.replace("Возврат 2", "Возврат 3")).succeeded
    active = api.status().worker_generation
    source = (
        "РезультатИнструкции = 4;" if statements_only
        else UPDATE.replace("Возврат 2", "Возврат 4")
    )
    conflict = SourceUnitRef(SourceUnitKind.NOTEBOOK_CELL, "stable-cell", 1, source_sha256(source))
    with pytest.raises(ProtocolError, match="source identit"):
        api.execute_bsl(source, source_unit=conflict)
    assert api.status().worker_generation is active


def test_explicit_identity_can_be_reused_after_retained_worker_pin_releases(runtime) -> None:
    api, composed, _session = runtime(captured=True)
    original = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL, "retained-cell", 1, source_sha256(UPDATE),
    )
    replacement = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL, "retained-cell", 1, source_sha256(THIRD),
    )
    assert api.execute_bsl(UPDATE, source_unit=original).succeeded
    _arm_capture(api)
    assert api.execute_bsl("Результат = Б();").kind is RuntimeReplyKind.CAPTURED
    assert api.execute_bsl(UPDATE.replace("Возврат 2", "Возврат 4")).succeeded
    with pytest.raises(ProtocolError, match="source identit"):
        api.execute_bsl(THIRD, source_unit=replacement)

    assert api.resume_capture().kind is RuntimeReplyKind.MAIN_COMPLETED
    assert original not in composed.worker_universe.retained_source_units()
    assert api.execute_bsl(THIRD, source_unit=replacement).succeeded


def test_concurrent_direct_cell_cannot_reuse_worker_source_identity(runtime, monkeypatch) -> None:
    api, composed, _session = runtime()
    original = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL, "concurrent-cell", 1, source_sha256(UPDATE),
    )
    statement = "Результат = Б();"
    conflict = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL, "concurrent-cell", 1,
        source_sha256(statement),
    )
    entered_first, entered_second = Event(), Event()
    release_first, release_second = Event(), Event()
    parser = api._pipeline._parser
    original_prepare = parser.prepare

    def held_prepare(source, source_unit):
        if source == UPDATE:
            entered_first.set()
            assert release_first.wait(5)
        elif source == statement:
            entered_second.set()
            assert release_second.wait(5)
        return original_prepare(source, source_unit)

    monkeypatch.setattr(parser, "prepare", held_prepare)
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            first = pool.submit(api.execute_bsl, UPDATE, source_unit=original)
            assert entered_first.wait(5)
            second = pool.submit(api.execute_bsl, statement, source_unit=conflict)
            deadline = monotonic() + 5
            while not entered_second.is_set() and not second.done() and monotonic() < deadline:
                entered_second.wait(0.01)
            assert entered_second.is_set() or second.done()
            release_first.set()
            assert first.result(timeout=10).kind is RuntimeReplyKind.WORKER_LOADED
            assert original in composed.worker_universe.retained_source_units()
            release_second.set()
            with pytest.raises(ProtocolError, match="source identit"):
                second.result(timeout=10)
        finally:
            release_first.set()
            release_second.set()


def test_unused_prepared_candidate_owns_source_identity_until_discard(runtime) -> None:
    api, _composed, _session = runtime()
    unit = SourceUnitRef(SourceUnitKind.NOTEBOOK_CELL, "prepared-cell", 1, source_sha256(UPDATE))
    prepared = api.prepare_bsl(UPDATE, source_unit=unit)
    conflict = SourceUnitRef(SourceUnitKind.NOTEBOOK_CELL, "prepared-cell", 1, source_sha256(THIRD))
    with pytest.raises(ProtocolError, match="source identit"):
        api.execute_bsl(THIRD, source_unit=conflict)
    api.discard_prepared_bsl(prepared)
    assert api.execute_bsl(THIRD, source_unit=conflict).succeeded


@pytest.mark.parametrize("captured", (False, True))
def test_mixed_provenance_is_published_before_worker_mutation(runtime, captured) -> None:
    api, composed, session = runtime(captured=captured)
    assert api.execute_bsl(UPDATE).succeeded
    if captured:
        _arm_capture(api)
        assert api.execute_bsl("Результат = Б();").kind is RuntimeReplyKind.CAPTURED
    previous = api.status().worker_generation
    before = len(session.worker_instructions)
    saved = []

    def persist(provenance):
        assert api.status().worker_generation is previous
        assert len(session.worker_instructions) == before
        saved.append(provenance)

    statement = "РезультатИнструкции = В();" if captured else "Результат = В();"
    reply = api.execute_bsl(THIRD + "\n" + statement, on_execution_provenance=persist)
    assert reply.succeeded
    assert len(saved) == 1
    assert saved[0].visible_source_sha256 == source_sha256(THIRD + "\n" + statement)
    assert api.status().worker_generation is not previous


def test_prepared_main_provenance_matches_final_dispatch(runtime) -> None:
    api, _composed, _session = runtime()
    assert api.execute_bsl(UPDATE).succeeded
    source = THIRD + "\nРезультат = В();"
    candidate = api.prepare_bsl(source)
    provenance = api.prepared_bsl_execution_provenance(candidate)
    admitted = []
    reply = api.execute_prepared_bsl(candidate, on_execution_provenance=admitted.append)
    assert reply.succeeded
    assert admitted == [provenance]
    assert provenance.visible_source_sha256 == source_sha256(source)


@pytest.mark.parametrize("captured", (False, True))
def test_provenance_rejection_prevents_worker_publication(runtime, captured) -> None:
    api, composed, session = runtime(captured=captured)
    assert api.execute_bsl(UPDATE).succeeded
    if captured:
        _arm_capture(api)
        assert api.execute_bsl("Результат = Б();").kind is RuntimeReplyKind.CAPTURED
    active = api.status().worker_generation
    before = len(session.worker_instructions)

    def reject(_provenance):
        raise OSError("planned durable persistence failure")

    statement = "РезультатИнструкции = В();" if captured else "Результат = В();"
    with pytest.raises(OSError, match="durable persistence"):
        api.execute_bsl(THIRD + "\n" + statement, on_execution_provenance=reject)
    assert api.status().worker_generation is active
    assert len(session.worker_instructions) == before
    assert _exports(composed) == {"б"}
