"""One breakpoint workspace reports confirmed and uncertain Worker reloads."""

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from onec_runtime.breakpoint_workspace import (
    BreakpointWorkspaceController, BreakpointWorkspaceOutcomeUnknown,
    WorkspaceSnapshot,
)
from onec_runtime.errors import WorkerPromotionOutcomeUnknown
from onec_runtime.execution.worker_breakpoint_workspace import WorkerBreakpointWorkspace
from onec_runtime.rdbg.models import ModuleLocation
from onec_runtime.worker_breakpoints import (
    WorkerBreakpointCatalogSnapshot, WorkerBreakpointConflict,
    WorkerBreakpointCoordinator, WorkerBreakpointPlan,
    WorkerBreakpointRejection, WorkerBreakpointReloadOutcome,
    WorkerBreakpointReloadPolicy, WorkerBreakpointReloadRemoval,
)
from onec_runtime.worker_universe import WorkerGenerationHandle


SERVICE = ModuleLocation("ExtensionModule", "", UUID(int=1), UUID(int=2), 1)
WORKER_SLOT = ModuleLocation("ExtensionModule", "", UUID(int=1), UUID(int=3), 5)


class _DeniedLegacySession:
    def set_breakpoints(self, _locations):
        raise AssertionError("workspace must use supplied port")


class _Port:
    def __init__(self, failure=None):
        self.calls = []
        self.failure = failure

    def set_breakpoints(self, locations):
        self.calls.append(locations)
        if self.failure is not None:
            raise self.failure


class _Target:
    def __init__(self, handle):
        self.handle = handle
        self.prepared = object()
        self.discarded = []
        self.quarantined = []
        self.discard_failure = None

    def prepare_root(self, candidate, *, transaction_id):
        assert candidate.handle is self.handle
        assert isinstance(transaction_id, UUID)
        return self.prepared

    def swap_root(self, prepared):
        assert prepared is self.prepared
        return self.handle

    def discard_root(self, prepared):
        self.discarded.append(prepared)
        if self.discard_failure is not None:
            raise self.discard_failure

    def quarantine_root(self, prepared):
        self.quarantined.append(prepared)


def _harness(monkeypatch, *, conflict=False, removal=False):
    handle = WorkerGenerationHandle(1, 1, 1, "a" * 64)
    target = _Target(handle)
    candidate = SimpleNamespace(handle=handle)
    host = SimpleNamespace(_candidate_debug_view=lambda _candidate: object())
    coordinator = WorkerBreakpointCoordinator(session_id=UUID(int=3))
    observed = []
    removal_detail = WorkerBreakpointReloadRemoval(
        uuid4(), WorkerBreakpointRejection.SOURCE_IDENTITY_MISMATCH,
    )

    def prepare(_view, policy):
        observed.append(policy)
        if conflict:
            raise WorkerBreakpointConflict((removal_detail,))
        snapshot = coordinator.snapshot()
        return WorkerBreakpointPlan(
            object(), uuid4(), snapshot.catalog_version, uuid4(), (WORKER_SLOT,),
            (removal_detail.breakpoint_id,) if removal else (),
            (removal_detail,) if removal else (), (),
            WorkerBreakpointCatalogSnapshot(
                snapshot.catalog_version + 1, snapshot.statuses,
                snapshot.tombstone_ids,
            ),
        )

    monkeypatch.setattr(
        WorkerBreakpointCoordinator, "prepare_generation",
        lambda _self, view, policy: prepare(view, policy),
    )
    monkeypatch.setattr(
        WorkerBreakpointCoordinator, "commit",
        lambda _self, plan, *, workspace_confirmed: None,
    )
    workspace_owner = BreakpointWorkspaceController(
        _DeniedLegacySession(), WorkspaceSnapshot(0, SERVICE, (), (), (), False),
    )
    workspace = WorkerBreakpointWorkspace(coordinator, workspace_owner)
    return workspace, target, candidate, host, observed, removal_detail


def test_per_call_reset_report_records_only_confirmed_removals(monkeypatch) -> None:
    workspace, target, candidate, host, observed, detail = _harness(
        monkeypatch, removal=True,
    )
    port = _Port()

    handle = workspace.promote(
        host, target, candidate, port=port,
        reload_policy=WorkerBreakpointReloadPolicy.RESET_INCOMPATIBLE,
    )

    assert handle is candidate.handle
    assert observed == [WorkerBreakpointReloadPolicy.RESET_INCOMPATIBLE]
    assert len(port.calls) == 1
    report = workspace.last_reload_report
    assert report is not None
    assert report.outcome is WorkerBreakpointReloadOutcome.COMMITTED
    assert report.policy is WorkerBreakpointReloadPolicy.RESET_INCOMPATIBLE
    assert report.planned_removals == (detail,)
    assert report.committed_removals == (detail.breakpoint_id,)


def test_workspace_promotion_without_logical_points_leaves_reload_report_absent(
    monkeypatch,
) -> None:
    workspace, target, candidate, host, _observed, _detail = _harness(monkeypatch)

    workspace.promote(
        host, target, candidate, port=_Port(), record_report=False,
    )

    assert workspace.last_reload_report is None


def test_strict_conflict_discard_is_reported_aborted(monkeypatch) -> None:
    workspace, target, candidate, host, observed, _detail = _harness(
        monkeypatch, conflict=True,
    )

    with pytest.raises(WorkerBreakpointConflict):
        workspace.promote(host, target, candidate, port=_Port())

    assert observed == [WorkerBreakpointReloadPolicy.STRICT]
    assert target.discarded == [target.prepared]
    assert workspace.last_reload_report.outcome is WorkerBreakpointReloadOutcome.ABORTED


def test_unknown_workspace_install_is_reported_quarantined(monkeypatch) -> None:
    workspace, target, candidate, host, _observed, _detail = _harness(monkeypatch)
    port = _Port(TimeoutError("lost breakpoint reply"))

    with pytest.raises(BreakpointWorkspaceOutcomeUnknown):
        workspace.promote(host, target, candidate, port=port)

    assert workspace.last_reload_report.outcome is WorkerBreakpointReloadOutcome.QUARANTINED
    assert target.quarantined == [target.prepared]


def test_unknown_strict_discard_is_quarantined_not_aborted(monkeypatch) -> None:
    workspace, target, candidate, host, _observed, _detail = _harness(
        monkeypatch, conflict=True,
    )
    target.discard_failure = WorkerPromotionOutcomeUnknown(1, "a" * 64)

    with pytest.raises(WorkerPromotionOutcomeUnknown):
        workspace.promote(host, target, candidate, port=_Port())

    assert workspace.last_reload_report.outcome is WorkerBreakpointReloadOutcome.QUARANTINED
