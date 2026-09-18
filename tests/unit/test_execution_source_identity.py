"""Source identities supplied to the public execution facade."""

import pytest

from onec_runtime.bsl.source_maps import SourceUnitKind, SourceUnitRef, source_sha256
from onec_runtime.errors import ProtocolError
from onec_runtime.execution.source_identity import NotebookSourceIdentityFactory


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
