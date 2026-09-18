"""A prepared cell is a sealed, single-use admission candidate."""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from onec_runtime.bsl.source_maps import SourceUnitKind, SourceUnitRef, source_sha256
from onec_runtime.execution.contracts import (
    Accepted, CommonCell, Current, PreparationContext, PreparationSnapshots,
    PreparedCell, Rejected, StalePreparedDispatch, StalePreparation,
)
from onec_runtime.execution import pipeline as pipeline_module


CellExecutionPipeline = pipeline_module.CellExecutionPipeline


def _unit(source: str) -> SourceUnitRef:
    return SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL, "prepared-cell", 1, source_sha256(source),
    )


def test_third_policy_prepares_without_dispatch_and_submits_exact_snapshot_once() -> None:
    events: list[str] = []
    route = object()
    nonce = object()
    guards = object()
    capabilities = object()

    class Parser:
        def prepare(self, source, source_unit):
            events.append("parse")
            return CommonCell(source_unit, source, {}, source_sha256(source))

    class ThirdPolicy:
        def prepare(self, common, snapshots, context):
            events.append("prepare")
            assert snapshots.namespace == ("third",)
            return PreparedCell(context.route_token, context.preparation_nonce, "THIRD")

    policy = ThirdPolicy()
    context = PreparationContext(route, nonce, policy, capabilities)

    class Snapshots:
        def read_for(self, received):
            assert received is capabilities
            events.append("snapshot")
            return PreparationSnapshots(("third",), ("worker",), guards)

    class Ticket:
        def wait_initiator(self):
            events.append("wait")
            return "third-mode-result"

    class Controller:
        def await_preparation_context(self):
            events.append("route")
            return context

        def validate_preparation(self, received_context, received_guards):
            events.append("validate")
            assert received_context is context
            assert received_guards is guards
            return Current()

        def submit_cell(self, received_context, prepared, received_guards, receipt):
            events.append("submit")
            assert received_context is context
            assert received_guards is guards
            assert prepared.route_token is route
            assert prepared.preparation_nonce is nonce
            ticket = Ticket()
            receipt.adopt(ticket)
            return Accepted(ticket)

    pipeline = CellExecutionPipeline(Parser(), Controller(), Snapshots(), object())
    observed: list[PreparedCell] = []
    candidate = pipeline.prepare("hello", _unit("hello"), on_prepared=observed.append)

    assert isinstance(candidate, pipeline_module.PreparedCellHandle)
    assert len(observed) == 1
    assert events == ["parse", "route", "snapshot", "prepare"]
    assert pipeline.execute_prepared(candidate) == "third-mode-result"
    assert events == [
        "parse", "route", "snapshot", "prepare", "validate", "submit", "wait",
    ]
    with pytest.raises(RuntimeError, match="already consumed"):
        pipeline.execute_prepared(candidate)
    assert events.count("submit") == 1


@pytest.mark.parametrize("stale_at", ["validation", "admission"])
def test_stale_prepared_candidate_is_not_reprepared_or_dispatched(stale_at: str) -> None:
    events: list[str] = []
    guards = object()

    class Parser:
        def prepare(self, source, source_unit):
            events.append("parse")
            return CommonCell(source_unit, source, {}, "hash")

    class Policy:
        def prepare(self, common, snapshots, context):
            events.append("lower")
            return PreparedCell(context.route_token, context.preparation_nonce, "ready")

    class Snapshots:
        def read_for(self, capabilities):
            return PreparationSnapshots({}, {}, guards)

    class Controller:
        def await_preparation_context(self):
            events.append("route")
            return PreparationContext("third", "once", Policy(), ())

        def validate_preparation(self, context, received_guards):
            events.append("validate")
            assert received_guards is guards
            return StalePreparation("snapshot changed") if stale_at == "validation" else Current()

        def submit_cell(self, context, prepared, received_guards, receipt):
            events.append("admit")
            assert received_guards is guards
            return Rejected(StalePreparation("route changed"))

    pipeline = CellExecutionPipeline(Parser(), Controller(), Snapshots(), object())
    candidate = pipeline.prepare("hello", _unit("hello"))
    with pytest.raises(pipeline_module.StalePreparedCell, match="changed"):
        pipeline.execute_prepared(candidate)
    assert events.count("parse") == 1
    assert events.count("lower") == 1
    assert events.count("admit") == (0 if stale_at == "validation" else 1)
    with pytest.raises(RuntimeError, match="already consumed"):
        pipeline.execute_prepared(candidate)


def test_handle_is_bound_to_its_pipeline_and_foreign_attempt_does_not_consume_it() -> None:
    submissions: list[str] = []

    class Parser:
        def prepare(self, source, source_unit):
            return CommonCell(source_unit, source, {}, "hash")

    class Policy:
        def prepare(self, common, snapshots, context):
            return PreparedCell(context.route_token, context.preparation_nonce, "ready")

    class Snapshots:
        def read_for(self, capabilities):
            return PreparationSnapshots({}, {}, "guard")

    class Ticket:
        def wait_initiator(self):
            return "done"

    class Controller:
        def await_preparation_context(self):
            return PreparationContext("third", "nonce", Policy(), ())

        def validate_preparation(self, context, guards):
            return Current()

        def submit_cell(self, context, prepared, guards, receipt):
            submissions.append("admitted")
            ticket = Ticket()
            receipt.adopt(ticket)
            return Accepted(ticket)

    first = CellExecutionPipeline(Parser(), Controller(), Snapshots(), object())
    second = CellExecutionPipeline(Parser(), Controller(), Snapshots(), object())
    candidate = first.prepare("hello", _unit("hello"))
    with pytest.raises(TypeError, match="another pipeline"):
        second.execute_prepared(candidate)
    assert submissions == []
    assert first.execute_prepared(candidate) == "done"
    assert submissions == ["admitted"]


def test_prepared_interrupt_stops_only_adopted_ticket_and_releases_wait_lock() -> None:
    waiting = False
    entered: list[str] = []
    stopped: list[object] = []

    @contextmanager
    def handoff():
        nonlocal waiting
        assert not waiting
        waiting = True
        try:
            yield
        finally:
            waiting = False

    class Parser:
        def prepare(self, source, source_unit):
            assert not waiting
            return CommonCell(source_unit, source, {}, "hash")

    class Policy:
        def prepare(self, common, snapshots, context):
            assert not waiting
            return PreparedCell(context.route_token, context.preparation_nonce, "ready")

    class Snapshots:
        def read_for(self, capabilities):
            assert not waiting
            return PreparationSnapshots({}, {}, "guard")

    class Ticket:
        def wait_initiator(self):
            assert waiting
            entered.append("wait")
            raise KeyboardInterrupt

    ticket = Ticket()

    class Controller:
        def await_preparation_context(self):
            assert waiting
            entered.append("route")
            return PreparationContext("third", "nonce", Policy(), ())

        def validate_preparation(self, context, guards):
            assert not waiting
            entered.append("validate")
            return Current()

        def submit_cell(self, context, prepared, guards, receipt):
            assert not waiting
            entered.append("admit")
            receipt.adopt(ticket)
            return Accepted(ticket)

        def request_stop(self, owned):
            stopped.append(owned)

    pipeline = CellExecutionPipeline(Parser(), Controller(), Snapshots(), object())
    candidate = pipeline.prepare("hello", _unit("hello"), wait_handoff=handoff)
    with pytest.raises(KeyboardInterrupt):
        pipeline.execute_prepared(candidate, wait_handoff=handoff)
    assert entered == ["route", "validate", "admit", "wait"]
    assert stopped == [ticket]
    assert not waiting


@pytest.mark.parametrize("failure", ["interrupt_after_adoption", "settlement_stale"])
def test_prepared_submission_keeps_exact_ticket_when_caller_exits(failure: str) -> None:
    submitted: list[object] = []
    stopped: list[object] = []

    class Parser:
        def prepare(self, source, source_unit):
            return CommonCell(source_unit, source, {}, "hash")

    class Policy:
        def prepare(self, common, snapshots, context):
            return PreparedCell(context.route_token, context.preparation_nonce, "ready")

    class Snapshots:
        def read_for(self, capabilities):
            return PreparationSnapshots({}, {}, "guard")

    class Ticket:
        def wait_initiator(self):
            raise StalePreparedDispatch("settlement failed after remote effect")

    ticket = Ticket()

    class Controller:
        def await_preparation_context(self):
            return PreparationContext("third", "nonce", Policy(), ())

        def validate_preparation(self, context, guards):
            return Current()

        def submit_cell(self, context, prepared, guards, receipt):
            submitted.append(prepared)
            receipt.adopt(ticket)
            if failure == "interrupt_after_adoption":
                raise KeyboardInterrupt
            return Accepted(ticket)

        def request_stop(self, owned):
            stopped.append(owned)

    pipeline = CellExecutionPipeline(Parser(), Controller(), Snapshots(), object())
    candidate = pipeline.prepare("hello", _unit("hello"))
    if failure == "interrupt_after_adoption":
        with pytest.raises(KeyboardInterrupt):
            pipeline.execute_prepared(candidate)
        assert stopped == [ticket]
    else:
        with pytest.raises(StalePreparedDispatch, match="after remote effect"):
            pipeline.execute_prepared(candidate)
        assert stopped == []
    assert len(submitted) == 1
