from __future__ import annotations

from uuid import UUID

import pytest

from onec_runtime.breakpoint_workspace import (
    BreakpointWorkspaceController,
    BreakpointWorkspaceOutcomeUnknown,
    WorkspaceSnapshot,
)
from onec_runtime.errors import ProtocolError
from onec_runtime.rdbg.models import ModuleLocation


SERVICE = ModuleLocation(
    "ExtensionModule", "", UUID(int=1), UUID(int=2), 1
)
CAPTURE = ModuleLocation(
    "ExtensionModule", "", UUID(int=1), UUID(int=3), 7
)
USER = ModuleLocation(
    "ConfigurationModule", "", UUID(int=4), UUID(int=5), 9
)
WORKER = ModuleLocation(
    "ExtMDModule",
    "e1cib/tempstorage/00000000-0000-0000-0000-000000000001?seanceId=fake",
    UUID(int=6),
    UUID(int=7),
    2,
)


class _Session:
    def __init__(self) -> None:
        self.calls: list[tuple[ModuleLocation, ...]] = []
        self.failure: BaseException | None = None

    def set_breakpoints(self, points: tuple[ModuleLocation, ...]) -> None:
        self.calls.append(points)
        if self.failure is not None:
            raise self.failure


def _owner(session: _Session) -> BreakpointWorkspaceController:
    return BreakpointWorkspaceController(
        session,
        WorkspaceSnapshot(0, SERVICE, (), (), (), False),
    )


def test_workspace_success_is_aggregate_not_point_verification() -> None:
    session = _Session()
    owner = _owner(session)
    desired = owner.prepare(
        captures=(), ordinary_users=(), worker_slots=(WORKER,), shielded=False
    )

    receipt = owner.install(desired)

    assert session.calls == [(SERVICE, WORKER)]
    assert receipt.requested_digest == desired.digest
    assert receipt.version == desired.version
    assert not hasattr(receipt, "verified_breakpoints")


def test_worker_duplicates_collapse_but_cross_group_overlap_is_rejected() -> None:
    session = _Session()
    owner = _owner(session)

    desired = owner.prepare(
        captures=(),
        ordinary_users=(),
        worker_slots=(WORKER, WORKER),
        shielded=False,
    )
    assert desired.worker_slots == (WORKER,)

    with pytest.raises(ProtocolError, match="overlap"):
        owner.prepare(
            captures=(WORKER,),
            ordinary_users=(),
            worker_slots=(WORKER,),
            shielded=False,
        )
    assert session.calls == []


def test_shield_removes_only_capture_points() -> None:
    session = _Session()
    owner = _owner(session)
    desired = owner.prepare(
        captures=(CAPTURE,),
        ordinary_users=(USER,),
        worker_slots=(WORKER,),
        shielded=True,
    )

    assert desired.effective_locations == (SERVICE, USER, WORKER)


def test_unchanged_effective_workspace_skips_transport() -> None:
    session = _Session()
    owner = _owner(session)
    first = owner.prepare(
        captures=(), ordinary_users=(), worker_slots=(WORKER,), shielded=False
    )
    owner.install(first)
    equivalent = owner.prepare(
        captures=(), ordinary_users=(), worker_slots=(WORKER,), shielded=False
    )

    owner.install(equivalent)

    assert session.calls == [(SERVICE, WORKER)]
    assert owner.confirmed_snapshot is equivalent


def test_dispatched_failure_quarantines_workspace_owner() -> None:
    session = _Session()
    owner = _owner(session)
    desired = owner.prepare(
        captures=(), ordinary_users=(), worker_slots=(WORKER,), shielded=False
    )
    session.failure = TimeoutError("unknown")

    with pytest.raises(BreakpointWorkspaceOutcomeUnknown):
        owner.install(desired)

    assert owner.confirmed_snapshot.version == 0
    with pytest.raises(BreakpointWorkspaceOutcomeUnknown):
        owner.require_confirmed()
    with pytest.raises(BreakpointWorkspaceOutcomeUnknown):
        owner.prepare(
            captures=(), ordinary_users=(), worker_slots=(), shielded=False
        )


def test_forged_and_stale_snapshots_are_rejected_before_transport() -> None:
    session = _Session()
    owner = _owner(session)
    first = owner.prepare(
        captures=(), ordinary_users=(), worker_slots=(WORKER,), shielded=False
    )
    forged = WorkspaceSnapshot(1, SERVICE, (), (), (WORKER,), False)

    with pytest.raises(ProtocolError, match="proposal"):
        owner.install(forged)
    owner.install(first)
    with pytest.raises(ProtocolError, match="proposal"):
        owner.install(first)

    assert session.calls == [(SERVICE, WORKER)]


def test_workspace_repr_redacts_private_worker_url() -> None:
    owner = _owner(_Session())
    desired = owner.prepare(
        captures=(), ordinary_users=(), worker_slots=(WORKER,), shielded=False
    )

    assert "e1cib" not in repr(desired)
    assert "seanceId" not in repr(desired)
