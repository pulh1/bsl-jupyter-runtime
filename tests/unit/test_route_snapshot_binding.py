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


def test_statement_binding_refuses_unprepared_worker_method_definitions() -> None:
    contract = _contract()
    parser = PythonParserTarget.from_generated()
    owner = object()
    snapshot = contract.RoutePreparationSnapshot(owner, 7, (), ())
    common = _common(
        "Функция Посчитать() Экспорт\nВозврат 1;\nКонецФункции\nИтог = Посчитать();",
        parser,
    )
    assert common.parsed_units.has_methods
    binding = contract.SnapshotRouteBinding(parser, owner=owner, version=7)

    with pytest.raises(ValueError, match="Worker methods"):
        binding.bind(common, snapshot.for_pipeline())
