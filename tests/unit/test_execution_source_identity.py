"""Source identities supplied to the public execution facade."""

from types import SimpleNamespace

import pytest

from onec_runtime.bsl.source_maps import SourceUnitKind, SourceUnitRef, source_sha256
from onec_runtime.errors import ProtocolError
from onec_runtime.execution.contracts import (
    Accepted, CommonCell, Current, PreparationContext, PreparationSnapshots,
    PreparedCell,
)
from onec_runtime.execution.pipeline import CellExecutionPipeline
from onec_runtime.execution.public_facade import PublicExecutionFacade
from onec_runtime.execution.source_identity import NotebookSourceIdentityFactory
from onec_runtime.runtime_contracts import OperationExecutionProvenance
from onec_runtime.runtime_models import OperationState, RuntimeStatus


def test_factory_assigns_monotonic_revisions_to_anonymous_notebook_cells() -> None:
    factory = NotebookSourceIdentityFactory(lambda: ())

    first = factory("Первый = 1;")
    second = factory("Второй = 2;")

    assert first.kind is SourceUnitKind.NOTEBOOK_CELL
    assert first.unit_id.startswith("anonymous-notebook-")
    assert (first.unit_id, first.revision, first.source_sha256) == (
        second.unit_id, 1, source_sha256("Первый = 1;"),
    )
    assert (second.revision, second.source_sha256) == (2, source_sha256("Второй = 2;"))


def test_factory_rejects_explicit_identity_with_conflicting_retained_source() -> None:
    retained = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL, "cell-17", 4, source_sha256("Старый = 1;")
    )
    explicit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL, "cell-17", 4, source_sha256("Новый = 2;")
    )
    factory = NotebookSourceIdentityFactory(lambda: (retained,))

    with pytest.raises(ProtocolError, match="conflicts with a retained source"):
        factory.next_unit("Новый = 2;", explicit=explicit)


def test_factory_accepts_explicit_identity_when_retained_source_has_same_hash() -> None:
    source = "Результат = 42;"
    explicit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL, "cell-17", 4, source_sha256(source)
    )
    factory = NotebookSourceIdentityFactory(lambda: (explicit,))

    assert factory.next_unit(source, explicit=explicit) is explicit


def test_source_identity_adoption_does_not_wait_for_ticket_status() -> None:
    source = "Результат = 1;"
    changed = "Результат = 2;"
    unit = SourceUnitRef(SourceUnitKind.NOTEBOOK_CELL, "adopt-cell", 1, source_sha256(source))
    conflict = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL, "adopt-cell", 1, source_sha256(changed),
    )
    identity = NotebookSourceIdentityFactory(lambda: ())
    lease = identity.reserve(source, explicit=unit)

    class Ticket:
        def __init__(self) -> None:
            self.interrupted = True

        def status(self):
            if self.interrupted:
                raise RuntimeError("adoption queried pending ticket status")
            return SimpleNamespace(settled=False)

    ticket = Ticket()
    lease.adopt(ticket)
    lease.release()
    ticket.interrupted = False
    with pytest.raises(ProtocolError, match="source identit"):
        identity.next_unit(changed, explicit=conflict)


@pytest.mark.parametrize("failure", ("callback", "interrupt"))
@pytest.mark.parametrize("prepared", (False, True))
def test_adopted_ticket_retains_identity_until_settled(
    failure: str, prepared: bool,
) -> None:
    source = "Результат = 1;"
    changed = "Результат = 2;"
    unit = SourceUnitRef(SourceUnitKind.NOTEBOOK_CELL, "accepted-cell", 1, source_sha256(source))
    conflict = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL, "accepted-cell", 1, source_sha256(changed),
    )
    identity = NotebookSourceIdentityFactory(lambda: ())
    stops = []

    class Parser:
        def prepare(self, text, source_unit):
            return CommonCell(source_unit, text, {}, source_sha256(text))

    class Policy:
        def prepare(self, common, snapshots, context):
            return PreparedCell(context.route_token, context.preparation_nonce, common)

    class Snapshots:
        def read_for(self, capabilities):
            return PreparationSnapshots({}, {}, "guard")

    class Ticket:
        settled = False

        def status(self):
            return SimpleNamespace(settled=self.settled)

        def wait_initiator(self):
            if failure == "interrupt":
                raise KeyboardInterrupt
            return "completed"

    ticket = Ticket()

    class Controller:
        def await_preparation_context(self):
            return PreparationContext("route", object(), Policy(), ())

        def validate_preparation(self, context, guards):
            return Current()

        def submit_cell(self, context, prepared, guards, receipt):
            receipt.adopt(ticket)
            return Accepted(ticket)

        def request_stop(self, accepted):
            stops.append(accepted)

    digest = source_sha256(source)
    provenance = OperationExecutionProvenance(digest, digest, digest, "main")
    controller = Controller()
    facade = PublicExecutionFacade(
        CellExecutionPipeline(Parser(), controller, Snapshots(), object()),
        controller, object(), source_unit_factory=identity,
        source_identity=identity,
        status_reader=lambda: RuntimeStatus(OperationState.IDLE, 1, 0, None),
        provenance_reader=lambda _prepared: provenance,
    )

    def reject(_provenance):
        raise OSError("journal failed")

    callback = reject if failure == "callback" else None
    expected = OSError if failure == "callback" else KeyboardInterrupt
    candidate = facade.prepare_bsl(source, source_unit=unit) if prepared else None
    with pytest.raises(expected):
        if prepared:
            facade.execute_prepared_bsl(
                candidate, on_execution_provenance=callback,
            )
        else:
            facade.execute_bsl(
                source, source_unit=unit, on_execution_provenance=callback,
            )
    assert stops == ([ticket] if failure == "interrupt" else [])
    with pytest.raises(ProtocolError, match="source identit"):
        identity.next_unit(changed, explicit=conflict)

    ticket.settled = True
    assert identity.next_unit(changed, explicit=conflict) is conflict
