"""Ports and opaque values for the route-independent BSL cell pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from onec_runtime.bsl.diagnostics import DiagnosticStage, VisibleSourceContext
from onec_runtime.bsl.source_maps import MappedSource, SourceUnitRef


@dataclass(frozen=True, slots=True)
class CommonCell:
    source_unit: SourceUnitRef
    parsed_units: object
    source_maps: object
    source_hash: str


@dataclass(frozen=True, slots=True)
class OperationSourceMapBundle:
    """Private Worker and statement branches of one visible notebook cell."""

    visible: SourceUnitRef
    worker_candidate: MappedSource | None = field(repr=False)
    statement_execution: MappedSource | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class PreparationContext:
    """A controller-selected route; generic callers do not inspect its token."""

    route_token: object
    preparation_nonce: object
    policy: CellPolicy
    capabilities: object


@dataclass(frozen=True, slots=True)
class PreparationSnapshots:
    """Owner-provided snapshots and guards for optimistic admission."""

    namespace: object
    worker_catalog: object
    guards: object


@dataclass(frozen=True, slots=True)
class PreparedCell:
    """One local preparation attempt, with route-specific data in payload."""

    route_token: object
    preparation_nonce: object
    payload: object


@dataclass(frozen=True, slots=True)
class Accepted:
    ticket: ExecutionTicket


@dataclass(frozen=True, slots=True)
class StalePreparation:
    reason: str


class StalePreparedDispatch(RuntimeError):
    """An adopted ticket became stale before any remote side effect."""


@dataclass(frozen=True, slots=True)
class Current:
    """The route and snapshot guards still describe the current context."""


@dataclass(frozen=True, slots=True)
class SourceDiagnostic:
    message: str
    error: Exception | None = field(default=None, repr=False)
    mapped_source: MappedSource | None = field(default=None, repr=False)
    visible_source_context: VisibleSourceContext | None = field(default=None, repr=False)
    stage: DiagnosticStage | None = None


@dataclass(frozen=True, slots=True)
class Unavailable:
    reason: str


@dataclass(frozen=True, slots=True)
class Rejected:
    reason: StalePreparation | Unavailable


class CellPolicy(Protocol):
    def prepare(
        self, common: CommonCell, snapshots: PreparationSnapshots, context: PreparationContext
    ) -> PreparedCell | SourceDiagnostic: ...

    def settle(self, outcome: object, prepared: PreparedCell, services: object) -> object: ...


class ExecutionTicket(Protocol):
    def wait_initiator(self) -> object: ...


class SubmissionReceipt:
    """Admission handoff visible to the caller during ``submit_cell``.

    The controller must adopt its ticket before Worker activation, transport
    entry, or any other target side effect. A rejected submission leaves this
    receipt empty. Its single writer is the synchronous admission call.
    """

    __slots__ = ("_ticket",)

    def __init__(self) -> None:
        self._ticket: ExecutionTicket | None = None

    @property
    def ticket(self) -> ExecutionTicket | None:
        return self._ticket

    def adopt(self, ticket: ExecutionTicket) -> None:
        if self._ticket is not None:
            raise RuntimeError("Submission receipt already owns a ticket")
        self._ticket = ticket


class CommonCellParser(Protocol):
    def prepare(
        self, source: str, source_unit: SourceUnitRef
    ) -> CommonCell | SourceDiagnostic: ...


class PreparationSnapshotReader(Protocol):
    def read_for(self, capabilities: object) -> PreparationSnapshots: ...


class ExecutionControllerPort(Protocol):
    def await_preparation_context(self) -> PreparationContext | Unavailable: ...

    def validate_preparation(
        self, context: PreparationContext, guards: object
    ) -> Current | StalePreparation | Unavailable: ...

    def submit_cell(
        self,
        context: PreparationContext,
        prepared: PreparedCell,
        guards: object,
        receipt: SubmissionReceipt,
    ) -> Accepted | Rejected: ...

    def request_stop(self, ticket: ExecutionTicket) -> object: ...


class ReplyPresenter(Protocol):
    def diagnostic_reply(self, diagnostic: SourceDiagnostic) -> object: ...

    def unavailable_reply(self, unavailable: Unavailable) -> object: ...
