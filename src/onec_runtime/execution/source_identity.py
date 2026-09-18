"""Notebook source identities for the public execution facade."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from threading import RLock
from uuid import uuid4
from weakref import WeakKeyDictionary, WeakSet

from onec_runtime.bsl.source_maps import SourceUnitKind, SourceUnitRef, source_sha256
from onec_runtime.errors import ProtocolError


class PreparedSourceIdentityLease:
    """One live preparation's identity, released on consumption or discard."""

    __slots__ = ("source_unit", "_owner", "__weakref__")

    def __init__(
        self, owner: NotebookSourceIdentityFactory, source_unit: SourceUnitRef,
    ) -> None:
        self._owner = owner
        self.source_unit = source_unit

    def release(self) -> None:
        self._owner._release_prepared(self)

    def adopt(self, ticket: object) -> None:
        self._owner._adopt_prepared(self, ticket)


class NotebookSourceIdentityFactory:
    """Allocate anonymous notebook revisions and validate explicit identities.

    ``retained_units`` supplies source identities still reachable from prepared
    work or retained debug views.  Reusing one of their logical identities
    with different text is forbidden, preserving source-map attribution.
    """

    def __init__(
        self,
        retained_units: Callable[[], Iterable[SourceUnitRef]],
        *,
        anonymous_notebook_id: str | None = None,
    ) -> None:
        if not callable(retained_units):
            raise TypeError("retained source reader must be callable")
        if anonymous_notebook_id is not None and (
            not isinstance(anonymous_notebook_id, str) or not anonymous_notebook_id
        ):
            raise ValueError("anonymous notebook id must be a non-empty string")
        self._retained_units = retained_units
        self._anonymous_notebook_id = (
            anonymous_notebook_id or f"anonymous-notebook-{uuid4().hex}"
        )
        self._anonymous_notebook_revision = 0
        self._lock = RLock()
        # The weak entries are the live preparations themselves, not a copy
        # of their identities. Abandoned handles stop retaining source text.
        self._prepared: WeakSet[PreparedSourceIdentityLease] = WeakSet()
        # An admitted ticket, rather than its caller, owns the lease until
        # settlement. Weak keys release it if the ticket itself is retired.
        self._adopted: WeakKeyDictionary[object, PreparedSourceIdentityLease] = (
            WeakKeyDictionary()
        )

    def __call__(self, source: str) -> SourceUnitRef:
        """Return a fresh anonymous identity, as required by the facade port."""

        return self.next_unit(source)

    def next_unit(
        self,
        source: str,
        *,
        explicit: SourceUnitRef | None = None,
    ) -> SourceUnitRef:
        """Return *explicit* after retention validation, or a fresh identity."""

        if not isinstance(source, str):
            raise TypeError("notebook source must be a string")
        if explicit is not None and not isinstance(explicit, SourceUnitRef):
            raise TypeError("explicit source unit must be a SourceUnitRef")
        self._prune_settled()
        with self._lock:
            return self._next_unit_locked(source, explicit)

    def reserve(
        self, source: str, *, explicit: SourceUnitRef | None = None,
    ) -> PreparedSourceIdentityLease:
        """Atomically validate and retain a locally prepared cell's identity."""

        if not isinstance(source, str):
            raise TypeError("notebook source must be a string")
        if explicit is not None and not isinstance(explicit, SourceUnitRef):
            raise TypeError("explicit source unit must be a SourceUnitRef")
        self._prune_settled()
        with self._lock:
            lease = PreparedSourceIdentityLease(
                self, self._next_unit_locked(source, explicit),
            )
            self._prepared.add(lease)
            return lease

    def _next_unit_locked(
        self, source: str, explicit: SourceUnitRef | None,
    ) -> SourceUnitRef:
        if explicit is not None:
            identity = explicit.kind, explicit.unit_id, explicit.revision
            retained_units = (
                *self._retained_units(),
                *(lease.source_unit for lease in self._prepared),
                *(lease.source_unit for lease in self._adopted.values()),
            )
            for retained in retained_units:
                if not isinstance(retained, SourceUnitRef):
                    raise TypeError("retained source reader returned an invalid unit")
                if (
                    (retained.kind, retained.unit_id, retained.revision) == identity
                    and retained.source_sha256 != explicit.source_sha256
                ):
                    raise ProtocolError(
                        "Notebook source identity conflicts with a retained source"
                    )
            return explicit
        self._anonymous_notebook_revision += 1
        return SourceUnitRef(
            SourceUnitKind.NOTEBOOK_CELL,
            self._anonymous_notebook_id,
            self._anonymous_notebook_revision,
            source_sha256(source),
        )

    def _release_prepared(self, lease: PreparedSourceIdentityLease) -> None:
        with self._lock:
            self._prepared.discard(lease)

    def _adopt_prepared(self, lease: PreparedSourceIdentityLease, ticket: object) -> None:
        status = getattr(ticket, "status", None)
        if not callable(status):
            raise TypeError("admitted source identity requires a ticket status")
        with self._lock:
            # Receipt adoption runs before arbiter dispatch. It must not wait
            # on the ticket while the arbiter mailbox lock is held.
            self._adopted[ticket] = lease

    def _prune_settled(self) -> None:
        # Query the arbiter mailbox outside the identity lock. Admission takes
        # these locks in the opposite order when the receipt adopts a ticket.
        with self._lock:
            tickets = tuple(self._adopted)
        settled = tuple(ticket for ticket in tickets if ticket.status().settled)
        if settled:
            with self._lock:
                for ticket in settled:
                    self._adopted.pop(ticket, None)
