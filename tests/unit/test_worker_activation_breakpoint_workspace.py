from __future__ import annotations

from uuid import UUID

import pytest

from onec_runtime.breakpoint_workspace import (
    BreakpointWorkspaceController, BreakpointWorkspaceOutcomeUnknown, WorkspaceSnapshot,
)
from onec_runtime.bsl.source_maps import SourceUnitKind, SourceUnitRef, source_sha256
from onec_runtime.execution.common import NotebookCommonParser
from onec_runtime.execution.snapshot_binding import RoutePreparationSnapshot, SnapshotRouteBinding
from onec_runtime.execution.worker_activation import WorkerUniverseActivationAdapter
from onec_runtime.execution.worker_breakpoint_workspace import WorkerBreakpointWorkspace
from onec_runtime.execution.worker import WorkerActivationUnknown
from onec_runtime.errors import BslExecutionError, ProtocolError
from onec_runtime.rdbg.models import ModuleLocation
from onec_runtime.worker_breakpoints import WorkerBreakpointCoordinator, WorkerBreakpointResolution
from onec_runtime.worker_universe import WorkerUniverseRegistry, WorkerUniverseState
from onec_runtime.bsl.parser_target import PythonParserTarget

from test_worker_universe import _UniverseTargetExecutor, _notebook_builder


SERVICE = ModuleLocation("ExtensionModule", "", UUID(int=1), UUID(int=2), 1)
CAPTURE = ModuleLocation("ExtensionModule", "", UUID(int=1), UUID(int=3), 7)
USER = ModuleLocation("ConfigurationModule", "", UUID(int=4), UUID(int=5), 9)
SOURCE = "Функция Первый() Экспорт\nВозврат 1;\nКонецФункции"
SOURCE_2 = "Функция Второй() Экспорт\nВозврат 2;\nКонецФункции"


class _DeniedLegacySession:
    def set_breakpoints(self, _locations):
        raise AssertionError("Breakpoint writes must use the admitted port")


class _Port:
    def __init__(self, target: _UniverseTargetExecutor) -> None:
        self.target = target
        self.breakpoint_calls: list[tuple[ModuleLocation, ...]] = []
        self.failure: BaseException | None = None

    def set_breakpoints(self, locations: tuple[ModuleLocation, ...]) -> None:
        if not self.breakpoint_calls:
            assert self.target.active_root_key is None
        self.breakpoint_calls.append(locations)
        if self.failure is not None:
            raise self.failure


def test_worker_activation_installs_full_workspace_before_root_swap(tmp_path) -> None:
    parser = PythonParserTarget.from_generated()
    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL, "activation", 1, source_sha256(SOURCE),
    )
    common = NotebookCommonParser(parser).prepare(SOURCE, unit)
    owner = object()
    intent = SnapshotRouteBinding(parser, owner=owner, version=1).worker_intent(
        common, RoutePreparationSnapshot(owner, 1, (), ()).for_pipeline(),
    )
    host = WorkerUniverseRegistry(runtime_generation=7, context_generation=3)
    target = _UniverseTargetExecutor()
    port = _Port(target)
    breakpoints = WorkerBreakpointCoordinator(session_id=UUID(int=80))
    pending = breakpoints.prepare_add(
        unit, "worker", 2, enabled=True, column=None,
    )
    breakpoints.commit(pending)
    workspace = BreakpointWorkspaceController(
        _DeniedLegacySession(), WorkspaceSnapshot(0, SERVICE, (CAPTURE,), (USER,), (), False),
    )

    def execute(supplied_port, source):
        assert supplied_port is port
        candidate = host._pending
        assert candidate is not None
        target.acknowledge(candidate)
        return target(source)

    adapter = WorkerUniverseActivationAdapter(
        host,
        notebook_builder=_notebook_builder(tmp_path),
        instruction_runner=execute,
        worker_breakpoints_present=lambda: bool(breakpoints.list_statuses()),
        breakpoint_workspace=WorkerBreakpointWorkspace(breakpoints, workspace),
        target_profile="server-test",
    )

    with pytest.raises(TypeError, match="port"):
        adapter.activate(intent, port=None)
    assert host._pending is None

    lease = adapter.activate(intent, port=port)

    assert lease.handle is host.active_handle
    assert len(port.breakpoint_calls) == 1
    assert port.breakpoint_calls[0][:3] == (SERVICE, CAPTURE, USER)
    assert len(port.breakpoint_calls[0]) == 4
    assert workspace.confirmed_snapshot.worker_slots == port.breakpoint_calls[0][3:]
    status = breakpoints.status(pending.result_id)
    assert status.resolution is WorkerBreakpointResolution.RESOLVED
    assert status.installed_binding_count == 1
    ordinary = adapter.pin_active(port=port)
    assert ordinary is not None and ordinary.pin.handle is lease.handle
    ordinary.release(port=port)


def test_prebuilt_capture_worker_reuses_exact_artifact_only_after_admission(tmp_path) -> None:
    """A prepared Worker must not rebuild after its provenance is journaled."""
    parser = PythonParserTarget.from_generated()
    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL, "prepared-capture", 1, source_sha256(SOURCE),
    )
    common = NotebookCommonParser(parser).prepare(SOURCE, unit)
    owner = object()
    intent = SnapshotRouteBinding(parser, owner=owner, version=1).worker_intent(
        common, RoutePreparationSnapshot(owner, 1, (), ()).for_pipeline(),
    )
    host = WorkerUniverseRegistry(runtime_generation=7, context_generation=3)
    target = _UniverseTargetExecutor()
    port = _Port(target)
    builder = _notebook_builder(tmp_path)
    build_count = 0
    built_artifact = None

    def counting_builder(*args, **kwargs):
        nonlocal build_count, built_artifact
        build_count += 1
        built_artifact = builder(*args, **kwargs)
        return built_artifact

    def execute(supplied_port, source):
        assert supplied_port is port
        candidate = host._pending
        assert candidate is not None
        target.acknowledge(candidate)
        return target(source)

    adapter = WorkerUniverseActivationAdapter(
        host,
        notebook_builder=counting_builder,
        instruction_runner=execute,
        worker_breakpoints_present=lambda: False,
        target_profile="server-test",
    )

    prebuilt = adapter.prebuild_for_capture(intent)
    provenance = prebuilt.source_provenance
    assert provenance.source_sha256
    assert provenance.source_map_sha256
    assert built_artifact is not None
    assert provenance == built_artifact.source_provenance
    assert build_count == 1
    assert target.calls == []
    assert host.active_handle is None
    assert repr(prebuilt) == "<redacted prebuilt Worker intent>"
    with pytest.raises(AttributeError, match="immutable"):
        prebuilt.intent = intent
    with pytest.raises(TypeError, match="port"):
        adapter.activate(prebuilt, port=None)
    other = WorkerUniverseActivationAdapter(
        host,
        notebook_builder=counting_builder,
        instruction_runner=execute,
        worker_breakpoints_present=lambda: False,
        target_profile="server-test",
    )
    with pytest.raises(ProtocolError, match="another runtime"):
        other.activate(prebuilt, port=port)
    assert target.calls == []

    lease = adapter.activate(prebuilt, port=port)
    assert lease.handle is host.active_handle
    assert build_count == 1
    assert any(built_artifact.artifact_sha256 in call for call in target.calls)
    with pytest.raises(ProtocolError, match="consumed"):
        adapter.activate(prebuilt, port=port)
    assert build_count == 1


def test_unknown_worker_workspace_install_retains_candidate_without_swap(tmp_path) -> None:
    parser = PythonParserTarget.from_generated()
    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL, "activation", 1, source_sha256(SOURCE),
    )
    common = NotebookCommonParser(parser).prepare(SOURCE, unit)
    owner = object()
    intent = SnapshotRouteBinding(parser, owner=owner, version=1).worker_intent(
        common, RoutePreparationSnapshot(owner, 1, (), ()).for_pipeline(),
    )
    host = WorkerUniverseRegistry(runtime_generation=7, context_generation=3)
    target = _UniverseTargetExecutor()
    port = _Port(target)
    port.failure = TimeoutError("transport reply missing")
    breakpoints = WorkerBreakpointCoordinator(session_id=UUID(int=81))
    pending = breakpoints.prepare_add(unit, "worker", 2, enabled=True, column=None)
    breakpoints.commit(pending)
    workspace = BreakpointWorkspaceController(
        _DeniedLegacySession(), WorkspaceSnapshot(0, SERVICE, (CAPTURE,), (USER,), (), False),
    )

    def execute(supplied_port, source):
        assert supplied_port is port
        candidate = host._pending
        assert candidate is not None
        target.acknowledge(candidate)
        return target(source)

    adapter = WorkerUniverseActivationAdapter(
        host,
        notebook_builder=_notebook_builder(tmp_path),
        instruction_runner=execute,
        worker_breakpoints_present=lambda: bool(breakpoints.list_statuses()),
        breakpoint_workspace=WorkerBreakpointWorkspace(breakpoints, workspace),
        target_profile="server-test",
    )

    with pytest.raises(WorkerActivationUnknown) as caught:
        adapter.activate(intent, port=port)

    assert isinstance(caught.value.__cause__, BreakpointWorkspaceOutcomeUnknown)
    assert target.active_root_key is None
    assert adapter.snapshot().revision == 0
    assert host.state is WorkerUniverseState.BROKEN
    assert breakpoints.status(pending.result_id).installed_binding_count == 0
    with pytest.raises(BreakpointWorkspaceOutcomeUnknown):
        workspace.require_confirmed()


def test_releasing_old_pin_removes_only_its_physical_worker_slot(tmp_path) -> None:
    parser = PythonParserTarget.from_generated()
    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL, "activation", 1, source_sha256(SOURCE),
    )
    common = NotebookCommonParser(parser).prepare(SOURCE, unit)
    second_unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL, "activation", 2, source_sha256(SOURCE_2),
    )
    second_common = NotebookCommonParser(parser).prepare(SOURCE_2, second_unit)
    owner = object()
    host = WorkerUniverseRegistry(runtime_generation=7, context_generation=3)
    target = _UniverseTargetExecutor()
    port = _Port(target)
    breakpoints = WorkerBreakpointCoordinator(session_id=UUID(int=82))
    pending = breakpoints.prepare_add(unit, "worker", 2, enabled=True, column=None)
    breakpoints.commit(pending)
    workspace = BreakpointWorkspaceController(
        _DeniedLegacySession(), WorkspaceSnapshot(0, SERVICE, (CAPTURE,), (USER,), (), False),
    )

    def execute(supplied_port, source):
        assert supplied_port is port
        candidate = host._pending
        assert candidate is not None
        target.acknowledge(candidate)
        return target(source)

    adapter = WorkerUniverseActivationAdapter(
        host,
        notebook_builder=_notebook_builder(tmp_path),
        instruction_runner=execute,
        worker_breakpoints_present=lambda: bool(breakpoints.list_statuses()),
        breakpoint_workspace=WorkerBreakpointWorkspace(breakpoints, workspace),
        target_profile="server-test",
    )

    def intent(revision):
        snapshot = RoutePreparationSnapshot(
            owner, revision, (), adapter.worker_exports, adapter.active_methods,
        )
        return SnapshotRouteBinding(parser, owner=owner, version=revision).worker_intent(
            common if revision == 1 else second_common,
            snapshot.for_pipeline(),
        )

    first = adapter.activate(intent(1), port=port)
    first_slot = workspace.confirmed_snapshot.worker_slots[0]
    second = adapter.activate(intent(2), port=port)

    assert first_slot in workspace.confirmed_snapshot.worker_slots
    assert len(workspace.confirmed_snapshot.worker_slots) == 2
    assert breakpoints.status(pending.result_id).installed_binding_count == 2

    first.release(port=port)

    assert workspace.confirmed_snapshot.worker_slots == (
        next(slot for slot in workspace.confirmed_snapshot.worker_slots if slot != first_slot),
    )
    assert port.breakpoint_calls[-1] == (
        SERVICE, CAPTURE, USER, *workspace.confirmed_snapshot.worker_slots,
    )
    assert breakpoints.status(pending.result_id).installed_binding_count == 1
    second.release(port=port)


def test_confirmed_swap_guard_failure_restores_previous_workspace(tmp_path) -> None:
    parser = PythonParserTarget.from_generated()
    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL, "activation", 1, source_sha256(SOURCE),
    )
    common = NotebookCommonParser(parser).prepare(SOURCE, unit)
    owner = object()
    intent = SnapshotRouteBinding(parser, owner=owner, version=1).worker_intent(
        common, RoutePreparationSnapshot(owner, 1, (), ()).for_pipeline(),
    )
    host = WorkerUniverseRegistry(runtime_generation=7, context_generation=3)

    class GuardFailureTarget(_UniverseTargetExecutor):
        def __call__(self, source):
            if "onec-worker-root-swap-stage=guard" in source:
                raise BslExecutionError("onec-worker-root-swap-stage=guard\nplanned")
            return super().__call__(source)

    target = GuardFailureTarget()
    port = _Port(target)
    breakpoints = WorkerBreakpointCoordinator(session_id=UUID(int=83))
    pending = breakpoints.prepare_add(unit, "worker", 2, enabled=True, column=None)
    breakpoints.commit(pending)
    workspace = BreakpointWorkspaceController(
        _DeniedLegacySession(), WorkspaceSnapshot(0, SERVICE, (CAPTURE,), (USER,), (), False),
    )

    def execute(supplied_port, source):
        assert supplied_port is port
        candidate = host._pending
        assert candidate is not None
        target.acknowledge(candidate)
        return target(source)

    adapter = WorkerUniverseActivationAdapter(
        host,
        notebook_builder=_notebook_builder(tmp_path),
        instruction_runner=execute,
        worker_breakpoints_present=lambda: bool(breakpoints.list_statuses()),
        breakpoint_workspace=WorkerBreakpointWorkspace(breakpoints, workspace),
        target_profile="server-test",
    )

    with pytest.raises(BslExecutionError, match="planned"):
        adapter.activate(intent, port=port)

    assert len(port.breakpoint_calls) == 2
    assert port.breakpoint_calls[-1] == (SERVICE, CAPTURE, USER)
    assert workspace.confirmed_snapshot.worker_slots == ()
    assert breakpoints.status(pending.result_id).resolution is WorkerBreakpointResolution.PENDING
    assert adapter.snapshot().revision == 0
    assert target.active_root_key is None


def test_unknown_root_swap_quarantines_breakpoint_plan_without_replaying(tmp_path) -> None:
    parser = PythonParserTarget.from_generated()
    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL, "activation", 1, source_sha256(SOURCE),
    )
    common = NotebookCommonParser(parser).prepare(SOURCE, unit)
    owner = object()
    intent = SnapshotRouteBinding(parser, owner=owner, version=1).worker_intent(
        common, RoutePreparationSnapshot(owner, 1, (), ()).for_pipeline(),
    )
    host = WorkerUniverseRegistry(runtime_generation=7, context_generation=3)
    target = _UniverseTargetExecutor()
    target.fault = "swap-ack"
    port = _Port(target)
    breakpoints = WorkerBreakpointCoordinator(session_id=UUID(int=84))
    pending = breakpoints.prepare_add(unit, "worker", 2, enabled=True, column=None)
    breakpoints.commit(pending)
    workspace = BreakpointWorkspaceController(
        _DeniedLegacySession(), WorkspaceSnapshot(0, SERVICE, (CAPTURE,), (USER,), (), False),
    )

    def execute(supplied_port, source):
        assert supplied_port is port
        candidate = host._pending
        assert candidate is not None
        target.acknowledge(candidate)
        return target(source)

    adapter = WorkerUniverseActivationAdapter(
        host,
        notebook_builder=_notebook_builder(tmp_path),
        instruction_runner=execute,
        worker_breakpoints_present=lambda: bool(breakpoints.list_statuses()),
        breakpoint_workspace=WorkerBreakpointWorkspace(breakpoints, workspace),
        target_profile="server-test",
    )

    with pytest.raises(WorkerActivationUnknown):
        adapter.activate(intent, port=port)

    assert len(port.breakpoint_calls) == 1
    assert target.active_root_key is not None
    assert adapter.snapshot().revision == 0
    assert breakpoints.status(pending.result_id).installed_binding_count == 0
    with pytest.raises(BreakpointWorkspaceOutcomeUnknown):
        workspace.require_confirmed()
