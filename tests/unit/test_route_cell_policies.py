"""Concrete route policies keep lowering and settlement out of the pipeline."""

from __future__ import annotations

from contextlib import nullcontext
from importlib import import_module

import pytest

from onec_runtime.bsl import LoweringMode, SemanticNotebookLowerer
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


def test_capture_policy_exposes_dirty_roots_before_dispatch() -> None:
    _, capture = _policies()
    parser = PythonParserTarget.from_generated()
    common = _common("КонтекстОтладки.Скаляр = 2;", parser)
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
    common = _common("КонтекстОтладки.Скаляр = 2;", parser)
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
