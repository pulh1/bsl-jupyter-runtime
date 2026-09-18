"""Concrete route policies keep lowering and settlement out of the pipeline."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
from importlib import import_module

import pytest

from onec_runtime.bsl import LoweringMode, SemanticNotebookLowerer, WorkerExport
from onec_runtime.bsl.parser_target import PythonParserTarget
from onec_runtime.bsl.source_maps import SourceUnitKind, SourceUnitRef, source_sha256
from onec_runtime.execution.common import NotebookCommonParser
from onec_runtime.execution.contracts import (
    PreparationContext,
    PreparationSnapshots,
    PreparedCell,
    SourceDiagnostic,
)
from onec_runtime.execution.preparation import RoutePreparationInput


def _policies():
    return (
        import_module("onec_runtime.execution.main.policy"),
        import_module("onec_runtime.execution.capture.policy"),
    )


def _common(source: str, parser: PythonParserTarget):
    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL, "route-policy", 1, source_sha256(source)
    )
    common = NotebookCommonParser(parser).prepare(source, unit)
    assert not isinstance(common, SourceDiagnostic)
    return common


class _Binding:
    def __init__(self, parser: PythonParserTarget) -> None:
        self.parser = parser
        self.modes: list[LoweringMode] = []

    def bind(self, common, snapshots):
        assert snapshots.namespace == ("existing",)
        assert snapshots.worker_catalog == ("worker-version",)
        return RoutePreparationInput(
            statement=common.source_maps.statement_execution,
            lowerer=SemanticNotebookLowerer(self.parser),
            candidate_catalog=(),
            operation_pin=None,
            message_key_factory=lambda mode: self._message_key(mode),
            complete_catalog=lambda catalog: catalog,
            pinned_catalog=lambda pin: pytest.fail("unexpected pin catalog"),
            temporary_catalog=lambda lowerer, catalog: nullcontext(),
            with_pin_prelude=lambda lowered, pin, mode: lowered,
        )

    def worker_intent(self, common, snapshots):
        assert common.source_maps.worker_candidate is None
        return None

    def _message_key(self, mode):
        self.modes.append(mode)
        return "__main_messages" if mode is LoweringMode.MAIN else "__capture_messages"


class _MainSettlement:
    def settle_main(self, outcome, payload):
        return ("main", outcome, payload.statement.message_collector_key)


class _CaptureSettlement:
    def settle_capture(self, outcome, payload):
        return ("capture", outcome, payload.dirty_roots)


def test_route_policies_apply_distinct_profiles_and_preserve_opaque_fence() -> None:
    main, capture = _policies()
    parser = PythonParserTarget.from_generated()
    common = _common("Скаляр = 1;", parser)
    binding = _Binding(parser)
    snapshots = PreparationSnapshots(("existing",), ("worker-version",), (1, 2))
    token = object()
    nonce = object()

    main_policy = main.MainCellPolicy(binding)
    capture_policy = capture.CaptureCellPolicy(binding)
    main_context = PreparationContext(token, nonce, main_policy, object())
    capture_context = PreparationContext(token, nonce, capture_policy, object())

    main_prepared = main_policy.prepare(common, snapshots, main_context)
    capture_prepared = capture_policy.prepare(common, snapshots, capture_context)

    assert isinstance(main_prepared, PreparedCell)
    assert isinstance(capture_prepared, PreparedCell)
    assert main_prepared.route_token is token
    assert main_prepared.preparation_nonce is nonce
    assert capture_prepared.route_token is token
    assert capture_prepared.preparation_nonce is nonce
    assert main_prepared.payload.statement.lowering.mapped_source.artifact.mode == "main"
    assert capture_prepared.payload.statement.lowering.mapped_source.artifact.mode == "capture"
    assert binding.modes == [LoweringMode.MAIN, LoweringMode.CAPTURE]
    assert main_policy.settle("ok", main_prepared, _MainSettlement()) == (
        "main", "ok", "__main_messages"
    )
    assert capture_policy.settle("ok", capture_prepared, _CaptureSettlement()) == (
        "capture", "ok", ()
    )


@pytest.mark.parametrize("policy_name,key", [
    ("MainCellPolicy", "__main_messages"),
    ("CaptureCellPolicy", "__capture_messages"),
])
def test_message_call_gets_a_context_collector_before_remote_dispatch(
    policy_name: str, key: str,
) -> None:
    parser = PythonParserTarget.from_generated()
    common = _common('Сообщить("hello");', parser)
    binding = _Binding(parser)
    module = _policies()[0 if policy_name == "MainCellPolicy" else 1]
    policy = getattr(module, policy_name)(binding)
    context = PreparationContext(object(), object(), policy, object())
    snapshots = PreparationSnapshots(("existing",), ("worker-version",), (1, 2))

    prepared = policy.prepare(common, snapshots, context)

    assert isinstance(prepared, PreparedCell)
    statement = prepared.payload.statement
    assert statement.lowering.messages_intercepted == 1
    source = statement.lowering.source
    assert source.index(f'e1cRuntimeКонтекст.Вставить("{key}", Новый Массив);') < source.index(
        f"e1cRuntimeКонтекст.{key}.Добавить"
    )
    assert source.count('e1cRuntimeКонтекст.Вставить("__onec_cell_messages_result",') == 2
    assert source.count("ВызватьИсключение;") == 1


@pytest.mark.parametrize("policy_name", ["MainCellPolicy", "CaptureCellPolicy"])
def test_notebook_worker_call_binds_globals_and_message_sink_for_both_routes(
    policy_name: str,
) -> None:
    parser = PythonParserTarget.from_generated()
    common = _common("Сообщить(УвеличитьНаПроцент(100));", parser)

    class WorkerBinding(_Binding):
        def bind(self, common, snapshots):
            request = super().bind(common, snapshots)
            return replace(
                request,
                candidate_catalog=(WorkerExport("УвеличитьНаПроцент", "УвеличитьНаПроцент"),),
                worker_globals=("ПроцентПовышения",),
                worker_messages=True,
            )

    binding = WorkerBinding(parser)
    module = _policies()[0 if policy_name == "MainCellPolicy" else 1]
    policy = getattr(module, policy_name)(binding)
    context = PreparationContext(object(), object(), policy, object())
    prepared = policy.prepare(
        common, PreparationSnapshots(("existing",), ("worker-version",), (1, 2)), context,
    )

    assert isinstance(prepared, PreparedCell)
    source = prepared.payload.statement.lowering.source
    assert 'e1cRuntimeКонтекст.RuntimeWorker.УвеличитьНаПроцент(100)' in source
    assert 'e1cRuntimeКонтекст.ПроцентПовышения' in source
    assert '__OnecNotebookGlobals = __OnecNotebookBoundGlobals' in source
    assert source.count('__OnecNotebookGlobals = __OnecNotebookPreviousGlobals') == 2
    assert '__OnecWorkerMessageSink = e1cRuntimeКонтекст.__main_messages' in source or (
        '__OnecWorkerMessageSink = e1cRuntimeКонтекст.__capture_messages' in source
    )
    assert source.count('__OnecWorkerMessageSink = __OnecPinnedWorkerGenerationPreviousMessageSink') == 2


@pytest.mark.parametrize("policy_name", ["MainCellPolicy", "CaptureCellPolicy"])
def test_worker_message_sink_is_installed_without_a_direct_message_call(
    policy_name: str,
) -> None:
    parser = PythonParserTarget.from_generated()
    common = _common("Результат = ПоказатьОклад();", parser)

    class WorkerBinding(_Binding):
        def bind(self, common, snapshots):
            return replace(
                super().bind(common, snapshots),
                candidate_catalog=(WorkerExport("ПоказатьОклад", "ПоказатьОклад"),),
                worker_messages=True,
            )

    module = _policies()[0 if policy_name == "MainCellPolicy" else 1]
    policy = getattr(module, policy_name)(WorkerBinding(parser))
    prepared = policy.prepare(
        common, PreparationSnapshots(("existing",), ("worker-version",), (1, 2)),
        PreparationContext(object(), object(), policy, object()),
    )

    assert isinstance(prepared, PreparedCell)
    source = prepared.payload.statement.lowering.source
    assert "e1cRuntimeКонтекст.RuntimeWorker.ПоказатьОклад()" in source
    assert "__OnecWorkerMessageSink = e1cRuntimeКонтекст." in source
    assert source.count('e1cRuntimeКонтекст.Вставить("__onec_cell_messages_result",') == 2


def test_capture_policy_exposes_dirty_roots_before_dispatch() -> None:
    _, capture = _policies()
    parser = PythonParserTarget.from_generated()
    common = _common("e1cRuntimeКонтекстОтладки.Скаляр = 2;", parser)
    policy = capture.CaptureCellPolicy(_Binding(parser))
    context = PreparationContext(object(), object(), policy, object())
    snapshots = PreparationSnapshots(("existing",), ("worker-version",), (1, 2))

    prepared = policy.prepare(common, snapshots, context)

    assert isinstance(prepared, PreparedCell)
    assert prepared.payload.dirty_roots == ("Скаляр",)
    assert prepared.payload.common.source_maps is common.source_maps


def test_main_policy_returns_source_diagnostic_for_capture_namespace() -> None:
    main, _ = _policies()
    parser = PythonParserTarget.from_generated()
    common = _common("e1cRuntimeКонтекстОтладки.Скаляр = 2;", parser)
    policy = main.MainCellPolicy(_Binding(parser))
    context = PreparationContext(object(), object(), policy, object())
    snapshots = PreparationSnapshots(("existing",), ("worker-version",), (1, 2))

    diagnostic = policy.prepare(common, snapshots, context)

    assert isinstance(diagnostic, SourceDiagnostic)
    assert diagnostic.mapped_source is common.source_maps.statement_execution
    assert diagnostic.visible_source_context is not None


def test_policy_rejects_preparation_bound_to_another_statement() -> None:
    main, _ = _policies()
    parser = PythonParserTarget.from_generated()
    common = _common("Скаляр = 1;", parser)
    other = _common("Другой = 2;", parser)

    class WrongBinding(_Binding):
        def bind(self, common, snapshots):
            request = super().bind(common, snapshots)
            return RoutePreparationInput(
                statement=other.source_maps.statement_execution,
                lowerer=request.lowerer,
                candidate_catalog=request.candidate_catalog,
                operation_pin=request.operation_pin,
                message_key_factory=request.message_key_factory,
                complete_catalog=request.complete_catalog,
                pinned_catalog=request.pinned_catalog,
                temporary_catalog=request.temporary_catalog,
                with_pin_prelude=request.with_pin_prelude,
            )

    policy = main.MainCellPolicy(WrongBinding(parser))
    context = PreparationContext(object(), object(), policy, object())
    snapshots = PreparationSnapshots(("existing",), ("worker-version",), (1, 2))

    with pytest.raises(ValueError, match="statement"):
        policy.prepare(common, snapshots, context)
