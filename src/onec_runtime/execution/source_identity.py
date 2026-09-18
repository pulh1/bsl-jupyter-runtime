"""Notebook source identities for the public execution facade."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from threading import RLock
from uuid import uuid4

from onec_runtime.bsl.source_maps import SourceUnitKind, SourceUnitRef, source_sha256
from onec_runtime.errors import ProtocolError


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
        with self._lock:
            if explicit is not None:
                identity = explicit.kind, explicit.unit_id, explicit.revision
                for retained in self._retained_units():
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
