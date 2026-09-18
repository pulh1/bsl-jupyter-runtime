"""The cell pipeline is independent of the runtime's concrete routes."""

import pytest
from contextlib import contextmanager

from onec_runtime.bsl.source_maps import SourceUnitKind, SourceUnitRef, source_sha256
from onec_runtime.execution.contracts import (
    Accepted,
    CommonCell,
    Current,
    PreparationContext,
    PreparationSnapshots,
    PreparedCell,
    Rejected,
    SourceDiagnostic,
    StalePreparedDispatch,
    StalePreparation,
    SubmissionReceipt,
    Unavailable,
)
from onec_runtime.execution.pipeline import CellExecutionPipeline


def unit(source: str) -> SourceUnitRef:
    return SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL, "pipeline-cell", 1, source_sha256(source)
    )


def test_controller_selected_third_policy_runs_without_pipeline_route_changes() -> None:
    class Parser:
        def prepare(self, source: str, source_unit: SourceUnitRef) -> CommonCell:
            return CommonCell(source_unit, source, {"visible": source_unit}, "hash-abc")

    class Snapshots:
        def read_for(self, capabilities: object) -> PreparationSnapshots:
            assert capabilities == frozenset({"synthetic"})
            return PreparationSnapshots({"prefix": "third"}, {"version": 4}, (2, 4))

    class ThirdPolicy:
        def prepare(self, common, snapshots, context):
            assert common.parsed_units == "abc"
            assert snapshots.namespace == {"prefix": "third"}
            return PreparedCell(context.route_token, context.preparation_nonce, "ABC")

        def settle(self, outcome, prepared, services):
            return f"{services}:{outcome}:{prepared.payload}"

    class ThirdExecutor:
        def execute(self, prepared):
            return f"executed-{prepared.payload}"

    class ThirdRoute:
        policy = ThirdPolicy()
        executor = ThirdExecutor()

    class Ticket:
        def __init__(self, context, prepared, executor):
            self.context = context
            self.prepared = prepared
            self.executor = executor

        def wait_initiator(self):
            remote = self.executor.execute(self.prepared)
            return self.context.policy.settle(remote, self.prepared, "third")

    class Controller:
        def __init__(self, route):
            self.route = route

        def await_preparation_context(self):
            return PreparationContext("third-route", "nonce-1", self.route.policy, frozenset({"synthetic"}))

        def submit_cell(self, context, prepared, guards, receipt):
            assert guards == (2, 4)
            assert prepared.preparation_nonce == context.preparation_nonce
            ticket = Ticket(context, prepared, self.route.executor)
            receipt.adopt(ticket)
            return Accepted(ticket)

    class Replies:
        def diagnostic_reply(self, diagnostic):
            raise AssertionError("unexpected source diagnostic")

        def unavailable_reply(self, unavailable):
            raise AssertionError("unexpected unavailable route")

    pipeline = CellExecutionPipeline(Parser(), Controller(ThirdRoute()), Snapshots(), Replies())

    assert pipeline.execute("abc", unit("abc")) == "third:executed-ABC:ABC"


def test_stale_preparation_retries_locally_without_dispatching_old_cell() -> None:
    parse_count = 0
    prepared_nonces: list[str] = []
    dispatched: list[str] = []

    class Parser:
        def prepare(self, source, source_unit):
            nonlocal parse_count
            parse_count += 1
            return CommonCell(source_unit, source, {}, "same-source")

    class Policy:
        def prepare(self, common, snapshots, context):
            prepared_nonces.append(context.preparation_nonce)
            return PreparedCell(context.route_token, context.preparation_nonce, common.parsed_units)

    class Snapshots:
        def read_for(self, capabilities):
            return PreparationSnapshots({}, {}, "version-1")

    class Ticket:
        def __init__(self, prepared):
            self.prepared = prepared

        def wait_initiator(self):
            dispatched.append(self.prepared.preparation_nonce)
            return "new-route-result"

    class Controller:
        def __init__(self):
            self.next_route = 0

        def await_preparation_context(self):
            self.next_route += 1
            return PreparationContext(f"route-{self.next_route}", f"nonce-{self.next_route}", Policy(), ())

        def submit_cell(self, context, prepared, guards, receipt):
            if context.route_token == "route-1":
                return Rejected(StalePreparation("route changed during preparation"))
            ticket = Ticket(prepared)
            receipt.adopt(ticket)
            return Accepted(ticket)

    class Replies:
        def diagnostic_reply(self, diagnostic):
            raise AssertionError("unexpected source diagnostic")

        def unavailable_reply(self, unavailable):
            raise AssertionError("unexpected unavailable route")

    pipeline = CellExecutionPipeline(Parser(), Controller(), Snapshots(), Replies())

    assert pipeline.execute("source", unit("source")) == "new-route-result"
    assert parse_count == 1
    assert prepared_nonces == ["nonce-1", "nonce-2"]
    assert dispatched == ["nonce-2"]


def test_stale_prepared_dispatch_reprepares_after_adoption_without_stopping_ticket() -> None:
    prepared_nonces: list[str] = []
    stopped: list[object] = []

    class Parser:
        def prepare(self, source, source_unit):
            return CommonCell(source_unit, source, {}, "same-source")

    class Policy:
        def prepare(self, common, snapshots, context):
            prepared_nonces.append(context.preparation_nonce)
            return PreparedCell(context.route_token, context.preparation_nonce, common.parsed_units)

    class Snapshots:
        def read_for(self, capabilities):
            return PreparationSnapshots({}, {}, "guard")

    class Ticket:
        def __init__(self, result):
            self.result = result

        def wait_initiator(self):
            return self.result

    stale_ticket = Ticket("must not be awaited")
    accepted_ticket = Ticket("reprepared-result")

    class Controller:
        def __init__(self):
            self.attempts = 0

        def await_preparation_context(self):
            self.attempts += 1
            return PreparationContext(
                f"route-{self.attempts}",
                f"nonce-{self.attempts}",
                Policy(),
                (),
            )

        def submit_cell(self, context, prepared, guards, receipt):
            if context.preparation_nonce == "nonce-1":
                receipt.adopt(stale_ticket)
                raise StalePreparedDispatch("guard changed before remote dispatch")
            receipt.adopt(accepted_ticket)
            return Accepted(accepted_ticket)

        def request_stop(self, ticket):
            stopped.append(ticket)

    class Replies:
        def diagnostic_reply(self, diagnostic):
            raise AssertionError("unexpected source diagnostic")

        def unavailable_reply(self, unavailable):
            raise AssertionError("unexpected unavailable route")

    pipeline = CellExecutionPipeline(Parser(), Controller(), Snapshots(), Replies())

    assert pipeline.execute("source", unit("source")) == "reprepared-result"
    assert prepared_nonces == ["nonce-1", "nonce-2"]
    assert stopped == []


def test_stale_named_failure_from_accepted_ticket_does_not_repeat_remote_dispatch() -> None:
    dispatches = []

    class Parser:
        def prepare(self, source, source_unit):
            return CommonCell(source_unit, source, {}, "same-source")

    class Policy:
        def prepare(self, common, snapshots, context):
            return PreparedCell(context.route_token, context.preparation_nonce, common.parsed_units)

    class Snapshots:
        def read_for(self, capabilities):
            return PreparationSnapshots({}, {}, "guard")

    class Ticket:
        def __init__(self, dispatch_number):
            self.dispatch_number = dispatch_number

        def wait_initiator(self):
            if self.dispatch_number == 1:
                raise StalePreparedDispatch("policy settlement failed after remote effect")
            return "duplicated-remote-effect"

    class Controller:
        def await_preparation_context(self):
            return PreparationContext("route", object(), Policy(), ())

        def submit_cell(self, context, prepared, guards, receipt):
            dispatches.append(prepared.preparation_nonce)
            ticket = Ticket(len(dispatches))
            receipt.adopt(ticket)
            return Accepted(ticket)

    class Replies:
        def diagnostic_reply(self, diagnostic):
            raise AssertionError("unexpected source diagnostic")

        def unavailable_reply(self, unavailable):
            raise AssertionError("unexpected unavailable route")

    pipeline = CellExecutionPipeline(Parser(), Controller(), Snapshots(), Replies())

    with pytest.raises(StalePreparedDispatch, match="policy settlement failed"):
        pipeline.execute("source", unit("source"))
    assert len(dispatches) == 1


def test_policy_diagnostic_is_published_only_after_guard_validation() -> None:
    class Parser:
        def prepare(self, source, source_unit):
            return CommonCell(source_unit, source, {}, "hash")

    class Policy:
        def prepare(self, common, snapshots, context):
            return SourceDiagnostic("unknown variable")

    class Snapshots:
        def read_for(self, capabilities):
            return PreparationSnapshots({}, {}, "namespace-v3")

    class Controller:
        def await_preparation_context(self):
            return PreparationContext("route", "nonce", Policy(), ())

        def validate_preparation(self, context, guards):
            assert context.route_token == "route"
            assert guards == "namespace-v3"
            return Current()

        def submit_cell(self, context, prepared, guards, receipt):
            raise AssertionError("diagnostic must not be dispatched")

    class Replies:
        def diagnostic_reply(self, diagnostic):
            return {"source_error": diagnostic.message}

        def unavailable_reply(self, unavailable):
            raise AssertionError("unexpected unavailable route")

    pipeline = CellExecutionPipeline(Parser(), Controller(), Snapshots(), Replies())

    assert pipeline.execute("source", unit("source")) == {"source_error": "unknown variable"}


def test_interrupt_after_admission_requests_remote_stop_for_accepted_ticket() -> None:
    stop_requests: list[object] = []

    class Parser:
        def prepare(self, source, source_unit):
            return CommonCell(source_unit, source, {}, "hash")

    class Policy:
        def prepare(self, common, snapshots, context):
            return PreparedCell(context.route_token, context.preparation_nonce, common.parsed_units)

    class Snapshots:
        def read_for(self, capabilities):
            return PreparationSnapshots({}, {}, "guard")

    class Ticket:
        def wait_initiator(self):
            raise KeyboardInterrupt

    ticket = Ticket()

    class Controller:
        def await_preparation_context(self):
            return PreparationContext("route", "nonce", Policy(), ())

        def submit_cell(self, context, prepared, guards, receipt):
            receipt.adopt(ticket)
            return Accepted(ticket)

        def request_stop(self, accepted_ticket):
            stop_requests.append(accepted_ticket)

    class Replies:
        def diagnostic_reply(self, diagnostic):
            raise AssertionError("unexpected source diagnostic")

        def unavailable_reply(self, unavailable):
            raise AssertionError("unexpected unavailable route")

    pipeline = CellExecutionPipeline(Parser(), Controller(), Snapshots(), Replies())

    with pytest.raises(KeyboardInterrupt):
        pipeline.execute("source", unit("source"))
    assert stop_requests == [ticket]


def test_rejected_unavailable_preparation_returns_typed_reply_without_waiting() -> None:
    class Parser:
        def prepare(self, source, source_unit):
            return CommonCell(source_unit, source, {}, "hash")

    class Policy:
        def prepare(self, common, snapshots, context):
            return PreparedCell(context.route_token, context.preparation_nonce, common.parsed_units)

    class Snapshots:
        def read_for(self, capabilities):
            return PreparationSnapshots({}, {}, "guard")

    class Controller:
        def await_preparation_context(self):
            return PreparationContext("route", "nonce", Policy(), ())

        def submit_cell(self, context, prepared, guards, receipt):
            return Rejected(Unavailable("target is stopping"))

    class Replies:
        def diagnostic_reply(self, diagnostic):
            raise AssertionError("unexpected source diagnostic")

        def unavailable_reply(self, unavailable):
            return {"unavailable": unavailable.reason}

    pipeline = CellExecutionPipeline(Parser(), Controller(), Snapshots(), Replies())

    assert pipeline.execute("source", unit("source")) == {"unavailable": "target is stopping"}


def test_stale_route_diagnostic_is_discarded_and_reprepared() -> None:
    class Parser:
        def prepare(self, source, source_unit):
            return CommonCell(source_unit, source, {}, "hash")

    class Policy:
        def prepare(self, common, snapshots, context):
            if context.preparation_nonce == "old":
                return SourceDiagnostic("obsolete diagnostic")
            return PreparedCell(context.route_token, context.preparation_nonce, "ready")

    class Snapshots:
        def read_for(self, capabilities):
            return PreparationSnapshots({}, {}, "guard")

    class Ticket:
        def wait_initiator(self):
            return "new-route-result"

    class Controller:
        def __init__(self):
            self.calls = 0

        def await_preparation_context(self):
            self.calls += 1
            nonce = "old" if self.calls == 1 else "new"
            return PreparationContext(f"route-{self.calls}", nonce, Policy(), ())

        def validate_preparation(self, context, guards):
            assert context.preparation_nonce == "old"
            return StalePreparation("route changed")

        def submit_cell(self, context, prepared, guards, receipt):
            assert context.preparation_nonce == "new"
            ticket = Ticket()
            receipt.adopt(ticket)
            return Accepted(ticket)

    class Replies:
        def diagnostic_reply(self, diagnostic):
            raise AssertionError("stale diagnostic was published")

        def unavailable_reply(self, unavailable):
            raise AssertionError("unexpected unavailable route")

    pipeline = CellExecutionPipeline(Parser(), Controller(), Snapshots(), Replies())

    assert pipeline.execute("source", unit("source")) == "new-route-result"


def test_unavailable_context_returns_without_reading_snapshots() -> None:
    class Parser:
        def prepare(self, source, source_unit):
            return CommonCell(source_unit, source, {}, "hash")

    class Controller:
        def await_preparation_context(self):
            return Unavailable("previous operation is unresolved")

    class Snapshots:
        def read_for(self, capabilities):
            raise AssertionError("no stable route means no snapshot read")

    class Replies:
        def diagnostic_reply(self, diagnostic):
            raise AssertionError("unexpected source diagnostic")

        def unavailable_reply(self, unavailable):
            return unavailable.reason

    pipeline = CellExecutionPipeline(Parser(), Controller(), Snapshots(), Replies())

    assert pipeline.execute("source", unit("source")) == "previous operation is unresolved"


def test_common_parse_diagnostic_returns_before_requesting_route() -> None:
    class Parser:
        def prepare(self, source, source_unit):
            return SourceDiagnostic("syntax error")

    class Controller:
        def await_preparation_context(self):
            raise AssertionError("a syntax error does not need a runtime route")

    class Replies:
        def diagnostic_reply(self, diagnostic):
            return diagnostic.message

    pipeline = CellExecutionPipeline(Parser(), Controller(), object(), Replies())

    assert pipeline.execute("broken", unit("broken")) == "syntax error"


def test_interrupt_during_submit_after_ticket_adoption_requests_stop_once() -> None:
    stop_requests: list[object] = []

    class Parser:
        def prepare(self, source, source_unit):
            return CommonCell(source_unit, source, {}, "hash")

    class Policy:
        def prepare(self, common, snapshots, context):
            return PreparedCell(context.route_token, context.preparation_nonce, common.parsed_units)

    class Snapshots:
        def read_for(self, capabilities):
            return PreparationSnapshots({}, {}, "guard")

    class Ticket:
        def wait_initiator(self):
            raise AssertionError("submit never returned")

    ticket = Ticket()

    class Controller:
        def await_preparation_context(self):
            return PreparationContext("route", "nonce", Policy(), ())

        def submit_cell(self, context, prepared, guards, receipt: SubmissionReceipt):
            receipt.adopt(ticket)
            raise KeyboardInterrupt

        def request_stop(self, accepted_ticket):
            stop_requests.append(accepted_ticket)

    class Replies:
        def diagnostic_reply(self, diagnostic):
            raise AssertionError("unexpected source diagnostic")

        def unavailable_reply(self, unavailable):
            raise AssertionError("unexpected unavailable route")

    pipeline = CellExecutionPipeline(Parser(), Controller(), Snapshots(), Replies())

    with pytest.raises(KeyboardInterrupt):
        pipeline.execute("source", unit("source"))
    assert stop_requests == [ticket]


def test_interrupt_during_submit_before_ticket_adoption_has_nothing_to_stop() -> None:
    stop_requests: list[object] = []

    class Parser:
        def prepare(self, source, source_unit):
            return CommonCell(source_unit, source, {}, "hash")

    class Policy:
        def prepare(self, common, snapshots, context):
            return PreparedCell(context.route_token, context.preparation_nonce, common.parsed_units)

    class Snapshots:
        def read_for(self, capabilities):
            return PreparationSnapshots({}, {}, "guard")

    class Controller:
        def await_preparation_context(self):
            return PreparationContext("route", "nonce", Policy(), ())

        def submit_cell(self, context, prepared, guards, receipt: SubmissionReceipt):
            raise KeyboardInterrupt

        def request_stop(self, ticket):
            stop_requests.append(ticket)

    class Replies:
        def diagnostic_reply(self, diagnostic):
            raise AssertionError("unexpected source diagnostic")

        def unavailable_reply(self, unavailable):
            raise AssertionError("unexpected unavailable route")

    pipeline = CellExecutionPipeline(Parser(), Controller(), Snapshots(), Replies())

    with pytest.raises(KeyboardInterrupt):
        pipeline.execute("source", unit("source"))
    assert stop_requests == []


def test_submission_receipt_cannot_replace_an_adopted_ticket() -> None:
    receipt = SubmissionReceipt()
    first_ticket = object()
    receipt.adopt(first_ticket)

    with pytest.raises(RuntimeError, match="already owns"):
        receipt.adopt(object())

    assert receipt.ticket is first_ticket


def test_wait_handoff_wraps_route_and_ticket_wait_but_not_preparation_or_admission() -> None:
    waiting = False
    entered: list[str] = []

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
            return PreparedCell(context.route_token, context.preparation_nonce, common.parsed_units)

    class Snapshots:
        def read_for(self, capabilities):
            assert not waiting
            return PreparationSnapshots({}, {}, "guard")

    class Ticket:
        def wait_initiator(self):
            assert waiting
            entered.append("wait")
            return "settled"

    class Controller:
        def await_preparation_context(self):
            assert waiting
            entered.append("route")
            return PreparationContext("route", "nonce", Policy(), ())

        def submit_cell(self, context, prepared, guards, receipt):
            assert not waiting
            entered.append("admit")
            ticket = Ticket()
            receipt.adopt(ticket)
            return Accepted(ticket)

    class Replies:
        pass

    pipeline = CellExecutionPipeline(Parser(), Controller(), Snapshots(), Replies())
    assert pipeline.execute("source", unit("source"), wait_handoff=handoff) == "settled"
    assert entered == ["route", "admit", "wait"]
    assert not waiting


def test_admitted_callback_sees_only_accepted_prepared_cell() -> None:
    admitted: list[PreparedCell] = []

    class Parser:
        def prepare(self, source, source_unit):
            return CommonCell(source_unit, source, {}, "hash")

    class Policy:
        def prepare(self, common, snapshots, context):
            return PreparedCell(context.route_token, context.preparation_nonce, common.parsed_units)

    class Snapshots:
        def read_for(self, capabilities):
            return PreparationSnapshots({}, {}, "guard")

    class Ticket:
        def wait_initiator(self):
            assert len(admitted) == 1
            return "settled"

    class Controller:
        def __init__(self):
            self.calls = 0

        def await_preparation_context(self):
            self.calls += 1
            return PreparationContext("route", f"nonce-{self.calls}", Policy(), ())

        def submit_cell(self, context, prepared, guards, receipt):
            if self.calls == 1:
                return Rejected(StalePreparation("changed"))
            ticket = Ticket()
            receipt.adopt(ticket)
            return Accepted(ticket)

    pipeline = CellExecutionPipeline(Parser(), Controller(), Snapshots(), object())
    assert pipeline.execute("source", unit("source"), on_admitted=admitted.append) == "settled"
    assert [prepared.preparation_nonce for prepared in admitted] == ["nonce-2"]


def test_prepared_callback_rejects_unsupported_artifact_before_admission() -> None:
    calls: list[str] = []

    class Parser:
        def prepare(self, source, source_unit):
            return CommonCell(source_unit, source, {}, "hash")

    class Policy:
        def prepare(self, common, snapshots, context):
            return PreparedCell(context.route_token, context.preparation_nonce, "worker-only")

    class Snapshots:
        def read_for(self, capabilities):
            return PreparationSnapshots({}, {}, "guard")

    class Controller:
        def await_preparation_context(self):
            return PreparationContext("route", "nonce", Policy(), ())

        def submit_cell(self, context, prepared, guards, receipt):
            calls.append("admission")
            raise AssertionError("unsupported source must fail before admission")

    def reject_artifact(prepared):
        calls.append("prepared")
        raise ValueError("Worker artifact not built")

    pipeline = CellExecutionPipeline(Parser(), Controller(), Snapshots(), object())
    with pytest.raises(ValueError, match="Worker artifact"):
        pipeline.execute(
            "source", unit("source"),
            on_prepared=reject_artifact,
            on_admitted=lambda prepared: calls.append("admitted"),
        )
    assert calls == ["prepared"]
