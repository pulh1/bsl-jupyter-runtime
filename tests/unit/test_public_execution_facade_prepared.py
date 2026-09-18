"""Prepared public BSL cells retain exact identity and admission ownership."""

from __future__ import annotations

import pytest
from typing import get_args, get_type_hints

from onec_runtime.bsl.module_catalog import SessionCommonModuleCatalog
from onec_runtime.bsl.source_maps import SourceUnitKind, SourceUnitRef, source_sha256
from onec_runtime.errors import ProtocolError
from onec_runtime.execution.contracts import (
    Accepted, CommonCell, Current, PreparationContext, PreparationSnapshots,
    PreparedCell, SourceDiagnostic, Unavailable,
)
from onec_runtime.execution import public_facade as facade_module
from onec_runtime.execution.pipeline import CellExecutionPipeline
from onec_runtime.execution.reply_presenter import RuntimeReplyPresenter
from onec_runtime.execution.source_identity import NotebookSourceIdentityFactory
from onec_runtime.prototype_runtime import OperationState
from onec_runtime.runtime_api import RuntimeReplyKind, RuntimeStatus
from onec_runtime.runtime_contracts import OperationExecutionProvenance


def _unit(source: str) -> SourceUnitRef:
    return SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL, "prepared-public", 3, source_sha256(source),
    )


def _status() -> RuntimeStatus:
    return RuntimeStatus(OperationState.CAPTURED, 7, 19, None)


class _Arbiter:
    pass


def _ready_facade(*, provenance_reader=None):
    events: list[object] = []

    class Parser:
        def prepare(self, source, source_unit):
            events.append(("parse", source_unit))
            return CommonCell(source_unit, source, {}, source_sha256(source))

    class ThirdPolicy:
        def prepare(self, common, snapshots, context):
            events.append(("prepare", common.source_unit))
            return PreparedCell(context.route_token, context.preparation_nonce, common)

    class Snapshots:
        def read_for(self, capabilities):
            return PreparationSnapshots({}, {}, "exact-guards")

    class Ticket:
        def wait_initiator(self):
            events.append("wait")
            return "third-route-reply"

    class Controller:
        def await_preparation_context(self):
            events.append("route")
            return PreparationContext("third-route", object(), ThirdPolicy(), ())

        def validate_preparation(self, context, guards):
            events.append(("validate", guards))
            return Current()

        def submit_cell(self, context, prepared, guards, receipt):
            events.append(("submit", guards))
            ticket = Ticket()
            receipt.adopt(ticket)
            return Accepted(ticket)

    controller = Controller()
    pipeline = CellExecutionPipeline(Parser(), controller, Snapshots(), RuntimeReplyPresenter(_status))
    facade = facade_module.PublicExecutionFacade(
        pipeline, controller, _Arbiter(),
        source_unit_factory=_unit, status_reader=_status,
        provenance_reader=provenance_reader,
    )
    return facade, pipeline, controller, events


def test_public_prepare_has_no_admission_and_preserves_source_and_provenance() -> None:
    source = "Результат = 3;"
    exact_unit = _unit(source)
    digest = source_sha256(source)
    evidence = OperationExecutionProvenance(digest, digest, digest, "main")
    read: list[PreparedCell] = []
    published: list[OperationExecutionProvenance] = []

    def read_provenance(prepared):
        read.append(prepared)
        return evidence

    facade, _, _, events = _ready_facade(provenance_reader=read_provenance)
    handle = facade.prepare_bsl(source, source_unit=exact_unit)

    assert isinstance(handle, facade_module.PreparedBslCell)
    assert handle.source_unit is exact_unit
    assert not any(isinstance(event, tuple) and event[0] == "submit" for event in events)
    assert facade.prepared_bsl_execution_provenance(handle) is evidence
    assert facade.execute_prepared_bsl(
        handle, on_execution_provenance=published.append,
    ) == "third-route-reply"
    assert published == [evidence]
    assert len(read) == 1
    assert [event for event in events if isinstance(event, tuple) and event[0] == "submit"] == [
        ("submit", "exact-guards"),
    ]
    with pytest.raises(RuntimeError, match="already consumed"):
        facade.execute_prepared_bsl(handle)


def test_foreign_facade_cannot_consume_a_prepared_cell() -> None:
    facade, pipeline, controller, events = _ready_facade()
    other = facade_module.PublicExecutionFacade(
        pipeline, controller, _Arbiter(),
        source_unit_factory=_unit, status_reader=_status,
    )
    handle = facade.prepare_bsl("Результат = 3;")

    with pytest.raises(TypeError, match="another facade"):
        other.execute_prepared_bsl(handle)
    assert facade.execute_prepared_bsl(handle) == "third-route-reply"
    assert sum(isinstance(event, tuple) and event[0] == "submit" for event in events) == 1


@pytest.mark.parametrize("local_outcome", ["diagnostic", "unavailable"])
def test_prepare_returns_safe_typed_reply_for_local_failure(local_outcome: str) -> None:
    events: list[str] = []

    class Parser:
        def prepare(self, source, source_unit):
            events.append("parse")
            if local_outcome == "diagnostic":
                return SourceDiagnostic("internal secret text")
            return CommonCell(source_unit, source, {}, source_sha256(source))

    class Controller:
        def await_preparation_context(self):
            events.append("route")
            return Unavailable("runtime is busy")

    pipeline = CellExecutionPipeline(Parser(), Controller(), object(), RuntimeReplyPresenter(_status))
    facade = facade_module.PublicExecutionFacade(
        pipeline, Controller(), _Arbiter(),
        source_unit_factory=_unit, status_reader=_status,
    )

    result = facade.prepare_bsl("Результат = 3;")
    assert result.kind is RuntimeReplyKind.SOURCE_FAILED
    assert result.operation_id == 19
    if local_outcome == "diagnostic":
        assert result.error == "BSL source processing failed"
        assert events == ["parse"]
    else:
        assert result.error == "runtime is busy"
        assert events == ["parse", "route"]


def test_explicit_source_hash_mismatch_fails_before_generic_preparation() -> None:
    facade, _, _, events = _ready_facade()
    old_unit = _unit("old")
    with pytest.raises(ProtocolError, match="source identity"):
        facade.prepare_bsl("new", source_unit=old_unit)
    assert events == []


def test_retained_source_identity_conflict_fails_before_generic_preparation() -> None:
    old_unit = _unit("old")
    replacement = SourceUnitRef(
        old_unit.kind, old_unit.unit_id, old_unit.revision, source_sha256("new"),
    )
    facade, pipeline, controller, events = _ready_facade()
    facade_with_retained_identity = facade_module.PublicExecutionFacade(
        pipeline, controller, _Arbiter(),
        source_unit_factory=_unit, status_reader=_status,
        source_identity=NotebookSourceIdentityFactory(lambda: (old_unit,)),
    )

    with pytest.raises(ProtocolError, match="conflicts"):
        facade_with_retained_identity.prepare_bsl("new", source_unit=replacement)
    assert events == []


def test_prepared_provenance_must_match_the_exact_visible_source() -> None:
    source = "Результат = 3;"
    other = source_sha256("Результат = 4;")
    evidence = OperationExecutionProvenance(other, other, other, "main")
    facade, _, _, events = _ready_facade(provenance_reader=lambda prepared: evidence)
    handle = facade.prepare_bsl(source)

    with pytest.raises(ProtocolError, match="visible source"):
        facade.prepared_bsl_execution_provenance(handle)
    assert not any(isinstance(event, tuple) and event[0] == "submit" for event in events)


def test_worker_module_entrypoint_keeps_lazy_catalog_source_as_a_public_input(
    tmp_path,
) -> None:
    """Session can pass a lazy catalog without forcing a snapshot in facade."""

    (tmp_path / "CommonModules").mkdir()
    source = SessionCommonModuleCatalog(tmp_path, profile="test")
    facade, _, _, _ = _ready_facade()
    received: list[object] = []

    class Lifecycle:
        def load_worker_modules(self, units, *, common_modules, breakpoint_policy, profiler):
            received.append(common_modules)
            return "loaded"

    facade._worker_module_service = Lifecycle()
    assert facade.load_worker_modules((), common_modules=source) == "loaded"
    assert received == [source]
    assert SessionCommonModuleCatalog in get_args(
        get_type_hints(facade_module.PublicExecutionFacade.load_worker_modules)["common_modules"]
    )
