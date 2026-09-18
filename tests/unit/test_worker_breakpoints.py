from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from onec_runtime.bsl.source_maps import SourceUnitKind, SourceUnitRef
from onec_runtime.errors import ProtocolError
from onec_runtime.rdbg.models import ModuleLocation, StackFrame, StopEvent, TargetId
from onec_runtime.bsl.worker_reload_source_map import CompactReloadSourceMap
from onec_runtime.worker_breakpoints import (
    WorkerBreakpointConflict,
    WorkerBreakpointCoordinator,
    WorkerBreakpointRejection,
    WorkerBreakpointReloadPolicy,
    WorkerBreakpointResolution,
    WorkerBreakpointBinding,
    WorkerMappedFrame,
    map_generated_line,
    map_worker_stop,
    resolve_source_line,
)
from worker_debug_fixtures import worker_debug_view


def _debug_module(tmp_path: Path):
    return _debug_view(tmp_path).modules[0]


def _debug_view(tmp_path: Path, *, name: str = "МодульА", revision: int = 17):
    return worker_debug_view(tmp_path, name=name, revision=revision)


def test_source_line_roundtrips_to_one_generated_worker_line(
    tmp_path: Path,
) -> None:
    module = _debug_module(tmp_path)

    mapping = resolve_source_line(module, module.source_unit, 1)

    assert mapping.reason is None
    assert mapping.generated_line is not None
    location = map_generated_line(module, mapping.generated_line)
    assert location is not None
    assert location.source_unit == module.source_unit
    assert location.canonical_module == module.canonical_module
    assert location.line == 1
    assert module.registration.module_location(mapping.generated_line).line == (
        mapping.generated_line
    )


def test_source_line_out_of_range_is_map_proven_rejection(tmp_path: Path) -> None:
    module = _debug_module(tmp_path)

    mapping = resolve_source_line(module, module.source_unit, 99)

    assert mapping.generated_line is None
    assert mapping.reason is WorkerBreakpointRejection.LINE_OUT_OF_RANGE


def test_statement_line_maps_exactly_in_compact_worker_map(
    tmp_path: Path,
) -> None:
    module = _debug_module(tmp_path)

    mapping = resolve_source_line(module, module.source_unit, 2)

    assert mapping.generated_line == 2
    assert mapping.reason is None


def test_source_identity_mismatch_is_protocol_failure(tmp_path: Path) -> None:
    module = _debug_module(tmp_path)
    forged = SourceUnitRef(
        module.source_unit.kind,
        module.source_unit.unit_id,
        module.source_unit.revision,
        "f" * 64,
    )

    with pytest.raises(ProtocolError, match="does not match"):
        resolve_source_line(module, forged, 2)


def test_compact_resolution_does_not_materialize_generic_map(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _debug_module(tmp_path)
    assert isinstance(module.mapped_source.source_map, CompactReloadSourceMap)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("generic source map was materialized")

    monkeypatch.setattr(CompactReloadSourceMap, "materialize_generic", forbidden)

    assert resolve_source_line(module, module.source_unit, 1).generated_line == 1


def test_pending_add_is_idempotent_and_does_not_change_enabled() -> None:
    unit = SourceUnitRef(
        SourceUnitKind.MODULE,
        "WorkerA",
        1,
        "a" * 64,
    )
    coordinator = WorkerBreakpointCoordinator(session_id=UUID(int=1))
    first = coordinator.prepare_add(
        unit,
        "WorkerA",
        2,
        enabled=False,
        column=None,
    )
    coordinator.commit(first)
    second = coordinator.prepare_add(
        unit,
        "WorkerA",
        2,
        enabled=True,
        column=None,
    )
    coordinator.commit(second)

    status = coordinator.status(first.result_id)
    assert second.result_id == first.result_id
    assert status.resolution is WorkerBreakpointResolution.PENDING
    assert status.enabled is False
    assert status.installed_binding_count == 0


def test_retained_view_resolves_to_desired_slot_without_claiming_install(
    tmp_path: Path,
) -> None:
    view = _debug_view(tmp_path)
    module = view.modules[0]
    coordinator = WorkerBreakpointCoordinator(session_id=UUID(int=2))
    coordinator.set_views((view,))

    plan = coordinator.prepare_add(
        module.source_unit,
        module.canonical_module,
        2,
        enabled=True,
        column=None,
    )
    coordinator.commit(plan)

    status = coordinator.status(plan.result_id)
    assert status.resolution is WorkerBreakpointResolution.RESOLVED
    assert status.reason is None
    assert len(status.generations) == 1
    assert status.generations[0].resolution is WorkerBreakpointResolution.RESOLVED
    assert len(plan.desired_slots) == 1
    assert plan.desired_slots[0] == module.registration.module_location(2)
    assert status.installed_binding_count == 0
    assert "e1cib" not in repr(plan)


def test_disabled_resolved_breakpoint_has_no_desired_slot(tmp_path: Path) -> None:
    view = _debug_view(tmp_path)
    module = view.modules[0]
    coordinator = WorkerBreakpointCoordinator(session_id=UUID(int=3))
    coordinator.set_views((view,))

    plan = coordinator.prepare_add(
        module.source_unit,
        module.canonical_module,
        1,
        enabled=False,
        column=None,
    )
    coordinator.commit(plan)

    assert plan.desired_slots == ()
    assert coordinator.status(plan.result_id).resolution is (
        WorkerBreakpointResolution.RESOLVED
    )


def test_last_compatible_view_release_returns_breakpoint_to_pending(
    tmp_path: Path,
) -> None:
    view = _debug_view(tmp_path)
    module = view.modules[0]
    coordinator = WorkerBreakpointCoordinator(session_id=UUID(int=7))
    coordinator.set_views((view,))
    plan = coordinator.prepare_add(
        module.source_unit,
        module.canonical_module,
        1,
        enabled=True,
        column=None,
    )
    coordinator.commit(plan)
    resolved_version = coordinator.snapshot().catalog_version

    coordinator.set_views(())

    status = coordinator.status(plan.result_id)
    assert status.resolution is WorkerBreakpointResolution.PENDING
    assert status.reason is (
        WorkerBreakpointRejection.NO_RETAINED_COMPATIBLE_ARTIFACT
    )
    assert status.installed_binding_count == 0
    assert status.catalog_version == resolved_version + 1


def test_catalog_proposals_are_versioned_and_previous_snapshot_is_frozen() -> None:
    unit = SourceUnitRef(SourceUnitKind.MODULE, "WorkerA", 1, "b" * 64)
    coordinator = WorkerBreakpointCoordinator(session_id=UUID(int=4))
    old_snapshot = coordinator.snapshot()
    first = coordinator.prepare_add(
        unit, "WorkerA", 1, enabled=True, column=None
    )
    stale = coordinator.prepare_add(
        unit, "WorkerA", 2, enabled=True, column=None
    )

    coordinator.commit(first)

    assert old_snapshot.catalog_version == 0
    assert old_snapshot.statuses == ()
    assert coordinator.snapshot().catalog_version == 1
    with pytest.raises(ProtocolError, match="stale"):
        coordinator.commit(stale)
    with pytest.raises(ProtocolError, match="stale"):
        coordinator.commit(first)


def test_remove_tombstone_is_idempotent_but_unknown_id_is_rejected() -> None:
    unit = SourceUnitRef(SourceUnitKind.MODULE, "WorkerA", 1, "c" * 64)
    coordinator = WorkerBreakpointCoordinator(session_id=UUID(int=5))
    added = coordinator.prepare_add(
        unit, "WorkerA", 1, enabled=True, column=None
    )
    coordinator.commit(added)
    removed = coordinator.prepare_remove(added.result_id)
    coordinator.commit(removed)

    repeated = coordinator.prepare_remove(added.result_id)
    coordinator.commit(repeated)

    assert coordinator.list_statuses() == ()
    assert added.result_id in coordinator.snapshot().tombstone_ids
    with pytest.raises(ProtocolError, match="unknown"):
        coordinator.prepare_remove(UUID(int=999))


def test_catalog_validates_line_column_and_enable_operations() -> None:
    unit = SourceUnitRef(SourceUnitKind.MODULE, "WorkerA", 1, "d" * 64)
    coordinator = WorkerBreakpointCoordinator(session_id=UUID(int=6))

    with pytest.raises(ValueError, match="line"):
        coordinator.prepare_add(
            unit, "WorkerA", True, enabled=True, column=None
        )
    with pytest.raises(ValueError, match="columns"):
        coordinator.prepare_add(
            unit, "WorkerA", 1, enabled=True, column=1
        )
    with pytest.raises(ProtocolError, match="unknown"):
        coordinator.prepare_enabled(UUID(int=42), True)


def test_strict_generation_rejects_enabled_source_identity_mismatch(
    tmp_path: Path,
) -> None:
    current = _debug_view(tmp_path, revision=17)
    candidate = _debug_view(tmp_path, revision=18)
    module = current.modules[0]
    coordinator = WorkerBreakpointCoordinator(session_id=UUID(int=8))
    coordinator.set_views((current,))
    added = coordinator.prepare_add(
        module.source_unit,
        module.canonical_module,
        2,
        enabled=True,
        column=None,
    )
    coordinator.commit(added)
    old = coordinator.snapshot()

    with pytest.raises(WorkerBreakpointConflict) as caught:
        coordinator.prepare_generation(
            candidate,
            WorkerBreakpointReloadPolicy.STRICT,
        )

    assert caught.value.rejections[0].breakpoint_id == added.result_id
    assert caught.value.rejections[0].reason is (
        WorkerBreakpointRejection.SOURCE_IDENTITY_MISMATCH
    )
    assert coordinator.snapshot() is old


def test_strict_generation_allows_disabled_incompatibility_without_removal(
    tmp_path: Path,
) -> None:
    current = _debug_view(tmp_path, revision=17)
    candidate = _debug_view(tmp_path, revision=18)
    module = current.modules[0]
    coordinator = WorkerBreakpointCoordinator(session_id=UUID(int=9))
    coordinator.set_views((current,))
    added = coordinator.prepare_add(
        module.source_unit,
        module.canonical_module,
        2,
        enabled=False,
        column=None,
    )
    coordinator.commit(added)

    plan = coordinator.prepare_generation(
        candidate,
        WorkerBreakpointReloadPolicy.STRICT,
    )
    coordinator.commit(plan)

    assert plan.removed_ids == ()
    assert coordinator.status(added.result_id).enabled is False


def test_reset_generation_removes_proven_incompatible_enabled_and_disabled(
    tmp_path: Path,
) -> None:
    current = _debug_view(tmp_path, revision=17)
    candidate = _debug_view(tmp_path, revision=18)
    module = current.modules[0]
    coordinator = WorkerBreakpointCoordinator(session_id=UUID(int=10))
    coordinator.set_views((current,))
    enabled = coordinator.prepare_add(
        module.source_unit,
        module.canonical_module,
        1,
        enabled=True,
        column=None,
    )
    coordinator.commit(enabled)
    disabled = coordinator.prepare_add(
        module.source_unit,
        module.canonical_module,
        2,
        enabled=False,
        column=None,
    )
    coordinator.commit(disabled)

    plan = coordinator.prepare_generation(
        candidate,
        WorkerBreakpointReloadPolicy.RESET_INCOMPATIBLE,
    )
    coordinator.commit(plan)

    assert plan.removed_ids == (enabled.result_id, disabled.result_id)
    assert all(
        item.reason is WorkerBreakpointRejection.SOURCE_IDENTITY_MISMATCH
        for item in plan.removal_details
    )
    assert coordinator.list_statuses() == ()
    assert coordinator.snapshot().tombstone_ids.issuperset(plan.removed_ids)


def test_prepare_release_of_last_compatible_view_returns_point_to_pending(
    tmp_path: Path,
) -> None:
    view = _debug_view(tmp_path)
    module = view.modules[0]
    coordinator = WorkerBreakpointCoordinator(session_id=UUID(int=11))
    coordinator.set_views((view,))
    added = coordinator.prepare_add(
        module.source_unit,
        module.canonical_module,
        2,
        enabled=True,
        column=None,
    )
    coordinator.commit(added)

    release = coordinator.prepare_release(())
    coordinator.commit(release)

    status = coordinator.status(added.result_id)
    assert release.desired_slots == ()
    assert status.resolution is WorkerBreakpointResolution.PENDING
    assert status.reason is (
        WorkerBreakpointRejection.NO_RETAINED_COMPATIBLE_ARTIFACT
    )
    assert status.installed_binding_count == 0


def test_mapped_stop_uses_exact_generation_view_and_redacts_worker_locator(
    tmp_path: Path,
) -> None:
    view = _debug_view(tmp_path, revision=17)
    module = view.modules[0]
    mapping = resolve_source_line(module, module.source_unit, 2)
    assert mapping.generated_line is not None
    worker_location = module.registration.module_location(mapping.generated_line)
    native_location = ModuleLocation(
        "ConfigModule",
        "",
        UUID(int=91),
        UUID(int=92),
        7,
    )
    target = TargetId(UUID(int=93), "test")
    event = StopEvent(
        target,
        worker_location,
        "callStackFormed",
        stop_by_breakpoint=True,
        stack=(worker_location, native_location),
        stack_frames=(
            StackFrame(target, 0, worker_location),
            StackFrame(target, 1, native_location),
        ),
    )
    binding = WorkerBreakpointBinding(UUID(int=94), view.handle, worker_location)

    mapped = map_worker_stop(
        event,
        operation_id=9,
        view=view,
        origin="main",
        bindings=(binding,),
    )

    assert mapped.location.source_unit.revision == 17
    assert mapped.location.line == 2
    assert mapped.location.column is None
    assert isinstance(mapped.frames[0], WorkerMappedFrame)
    assert tuple(frame.level for frame in mapped.frames) == (0, 1)
    assert mapped.breakpoint_ids == (binding.breakpoint_id,)
    assert worker_location.url not in repr(mapped)


def test_worker_shaped_stale_location_is_not_downgraded_to_native(
    tmp_path: Path,
) -> None:
    view = _debug_view(tmp_path)
    module = view.modules[0]
    expected = module.registration.module_location(2)
    forged = ModuleLocation(
        expected.module_type,
        expected.url + "-stale",
        expected.object_id,
        expected.property_id,
        expected.line,
        expected.extension_name,
        expected.ext_id,
    )
    event = StopEvent(
        TargetId(UUID(int=95), "test"),
        forged,
        "callStackFormed",
        stop_by_breakpoint=True,
    )

    with pytest.raises(ProtocolError, match="stale or forged"):
        map_worker_stop(
            event,
            operation_id=1,
            view=view,
            origin="main",
            bindings=(),
        )
