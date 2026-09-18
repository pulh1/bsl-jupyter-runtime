"""Statement preparation uses one guarded snapshot and fresh lowering state."""

from __future__ import annotations

from importlib import import_module

import pytest

from onec_runtime.bsl.parser_target import PythonParserTarget
from onec_runtime.bsl.semantic_lowering import WorkerExport
from onec_runtime.bsl.source_maps import SourceUnitKind, SourceUnitRef, source_sha256
from onec_runtime.execution.common import NotebookCommonParser
from onec_runtime.execution.contracts import PreparationContext, PreparedCell, SourceDiagnostic
from onec_runtime.execution.main.policy import MainCellPolicy
from onec_runtime.execution.capture.policy import CaptureCellPolicy


def _contract():
    return import_module("onec_runtime.execution.snapshot_binding")


def _common(source: str, parser: PythonParserTarget):
    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL, "snapshot-binding", 1, source_sha256(source)
    )
    common = NotebookCommonParser(parser).prepare(source, unit)
    assert not isinstance(common, SourceDiagnostic)
    return common


def test_real_lowerer_is_fresh_and_uses_namespace_and_worker_catalog_snapshot() -> None:
    contract = _contract()
    parser = PythonParserTarget.from_generated()
    owner = object()
    catalog = (WorkerExport("Расчет.Посчитать", "Посчитать"),)
    snapshot = contract.RoutePreparationSnapshot(owner, 7, ("Клиент",), catalog)
    common = _common("Итог = Клиент + Расчет.Посчитать();", parser)
    binding = contract.SnapshotRouteBinding(parser, owner=owner, version=7)
    policy = MainCellPolicy(binding)
    context = PreparationContext(object(), object(), policy, object())

    request_a = binding.bind(common, snapshot.for_pipeline())
    request_b = binding.bind(common, snapshot.for_pipeline())
    first = policy.prepare(common, snapshot.for_pipeline(), context)
    second = policy.prepare(common, snapshot.for_pipeline(), context)

    assert isinstance(first, PreparedCell)
    assert isinstance(second, PreparedCell)
    assert request_a.lowerer is not request_b.lowerer
    assert "Контекст.Клиент" in first.payload.statement.lowering.source
    assert "Контекст.RuntimeWorker.Посчитать()" in first.payload.statement.lowering.source
    assert first.payload.statement.lowering.worker_dependencies == (
        "Расчет.Посчитать",
    )
    assert first.payload.statement.lowering.source == second.payload.statement.lowering.source
    assert snapshot.namespace_names == ("Клиент",)
    assert snapshot.worker_exports == catalog


def test_separate_snapshot_does_not_inherit_namespace_or_catalog() -> None:
    contract = _contract()
    parser = PythonParserTarget.from_generated()
    owner = object()
    common = _common("Итог = Клиент + Расчет.Посчитать();", parser)
    snapshot = contract.RoutePreparationSnapshot(owner, 8, (), ())
    policy = MainCellPolicy(contract.SnapshotRouteBinding(parser, owner=owner, version=8))
    context = PreparationContext(object(), object(), policy, object())

    prepared = policy.prepare(common, snapshot.for_pipeline(), context)

    assert isinstance(prepared, PreparedCell)
    source = prepared.payload.statement.lowering.source
    assert "Контекст.Клиент" not in source
    assert "Контекст.RuntimeWorker.Посчитать()" not in source
    assert prepared.payload.statement.lowering.worker_dependencies == ()


@pytest.mark.parametrize("policy_type", [MainCellPolicy, CaptureCellPolicy])
def test_default_snapshot_binding_reports_effective_message_collector_key(policy_type) -> None:
    contract = _contract()
    parser = PythonParserTarget.from_generated()
    owner = object()
    snapshot = contract.RoutePreparationSnapshot(owner, 7, (), ())
    common = _common('Сообщить("message");', parser)
    policy = policy_type(contract.SnapshotRouteBinding(parser, owner=owner, version=7))
    context = PreparationContext(object(), object(), policy, snapshot)

    prepared = policy.prepare(common, snapshot.for_pipeline(), context)

    assert isinstance(prepared, PreparedCell)
    statement = prepared.payload.statement
    assert statement.message_collector_key == "__onec_cell_messages"
    assert "__onec_cell_messages" in statement.lowering.source


def test_binding_rejects_stale_version_and_foreign_owner_before_lowering() -> None:
    contract = _contract()
    parser = PythonParserTarget.from_generated()
    owner = object()
    snapshot = contract.RoutePreparationSnapshot(owner, 7, ("Клиент",), ())
    common = _common("Итог = Клиент;", parser)
    binding = contract.SnapshotRouteBinding(parser, owner=owner, version=8)

    with pytest.raises(contract.RouteSnapshotMismatch, match="version"):
        binding.bind(common, snapshot.for_pipeline())

    foreign = contract.SnapshotRouteBinding(parser, owner=object(), version=7)
    with pytest.raises(contract.RouteSnapshotMismatch, match="owner"):
        foreign.bind(common, snapshot.for_pipeline())


def test_binding_rejects_mixed_snapshot_components() -> None:
    contract = _contract()
    parser = PythonParserTarget.from_generated()
    owner = object()
    snapshot = contract.RoutePreparationSnapshot(owner, 7, ("Клиент",), ())
    common = _common("Итог = Клиент;", parser)
    binding = contract.SnapshotRouteBinding(parser, owner=owner, version=7)
    mixed = snapshot.for_pipeline()
    from onec_runtime.execution.contracts import PreparationSnapshots

    mixed = PreparationSnapshots(("Другой",), mixed.worker_catalog, mixed.guards)
    with pytest.raises(contract.RouteSnapshotMismatch, match="namespace"):
        binding.bind(common, mixed)


@pytest.mark.parametrize("policy_type", [MainCellPolicy, CaptureCellPolicy])
def test_mixed_cell_prepares_worker_intent_and_lowers_call_with_candidate_catalog(
    policy_type,
) -> None:
    contract = _contract()
    parser = PythonParserTarget.from_generated()
    owner = object()
    existing = WorkerExport("СтарыйМетод", "СтарыйМетод")
    snapshot = contract.RoutePreparationSnapshot(owner, 7, ("Клиент",), (existing,))
    common = _common(
        "Функция Посчитать() Экспорт\nВозврат 1;\nКонецФункции\nИтог = Посчитать();",
        parser,
    )
    policy = policy_type(contract.SnapshotRouteBinding(parser, owner=owner, version=7))
    context = PreparationContext(object(), object(), policy, snapshot)

    prepared = policy.prepare(common, snapshot.for_pipeline(), context)

    assert isinstance(prepared, PreparedCell)
    intent = prepared.payload.worker_intent
    assert intent.projection is common.source_maps.worker_candidate
    assert intent.cell is common.parsed_units
    assert intent.cell.methods[0].mapped_source.source_map_sha256
    assert intent.exports == (WorkerExport("Посчитать", "Посчитать"),)
    assert intent.candidate_catalog == (existing, WorkerExport("Посчитать", "Посчитать"))
    assert intent.namespace_names == ("Клиент",)
    assert intent.guard == snapshot.for_pipeline().guards
    assert prepared.payload.statement is None
    assert "Контекст.RuntimeWorker.Посчитать()" in prepared.payload.deferred_statement.lowering.source


@pytest.mark.parametrize("policy_type", [MainCellPolicy, CaptureCellPolicy])
def test_method_only_cell_prepares_local_worker_intent_without_statement(policy_type) -> None:
    contract = _contract()
    parser = PythonParserTarget.from_generated()
    owner = object()
    snapshot = contract.RoutePreparationSnapshot(owner, 7, (), ())
    common = _common("Функция Посчитать() Экспорт\nВозврат 1;\nКонецФункции", parser)
    policy = policy_type(contract.SnapshotRouteBinding(parser, owner=owner, version=7))
    context = PreparationContext(object(), object(), policy, snapshot)

    prepared = policy.prepare(common, snapshot.for_pipeline(), context)

    assert isinstance(prepared, PreparedCell)
    assert prepared.payload.statement is None
    assert prepared.payload.worker_intent.projection is common.source_maps.worker_candidate
    assert prepared.payload.worker_intent.cell is common.parsed_units
    assert prepared.payload.worker_intent.exports == (WorkerExport("Посчитать", "Посчитать"),)


def test_worker_intent_preserves_previous_method_set_for_local_merge() -> None:
    from onec_runtime.bsl.notebook_cells import split_notebook_cell
    from onec_runtime.bsl.notebook_methods import merge_notebook_methods

    contract = _contract()
    parser = PythonParserTarget.from_generated()
    owner = object()
    previous_source = "Функция Старый() Экспорт\nВозврат 1;\nКонецФункции"
    previous_unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL, "previous-method", 1,
        source_sha256(previous_source),
    )
    previous_cell = split_notebook_cell(
        parser, previous_source, source_unit=previous_unit,
    )
    previous = merge_notebook_methods(None, previous_cell)
    snapshot = contract.RoutePreparationSnapshot(
        owner, 7, (), previous.exports, previous,
    )
    common = _common("Функция Свежий() Экспорт\nВозврат 2;\nКонецФункции", parser)
    policy = MainCellPolicy(contract.SnapshotRouteBinding(parser, owner=owner, version=7))
    context = PreparationContext(object(), object(), policy, snapshot)

    prepared = policy.prepare(common, snapshot.for_pipeline(), context)

    assert isinstance(prepared, PreparedCell)
    intent = prepared.payload.worker_intent
    assert intent.previous_methods is previous
    assert intent.method_set_candidate.exports == (
        WorkerExport("Старый", "Старый"), WorkerExport("Свежий", "Свежий"),
    )
    assert "Функция Старый()" in intent.method_set_candidate.mapped_source.text
    assert "Функция Свежий()" in intent.method_set_candidate.mapped_source.text

    with pytest.raises(ValueError, match="method catalog"):
        contract.RoutePreparationSnapshot(owner, 7, (), (), previous)
