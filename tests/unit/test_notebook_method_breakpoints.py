from dataclasses import replace
from uuid import UUID

import pytest

from onec_runtime.bsl.source_maps import SourceUnitKind, SourceUnitRef, source_sha256
from onec_runtime.errors import ProtocolError
from onec_runtime.rdbg.models import StackFrame, StopEvent, TargetId
from onec_runtime.worker_breakpoints import (
    WorkerBreakpointConflict,
    WorkerBreakpointCoordinator,
    WorkerBreakpointRejection,
    WorkerBreakpointReloadPolicy,
    WorkerBreakpointResolution,
    map_generated_line,
    map_worker_stop,
    resolve_source_line,
)
from worker_debug_fixtures import notebook_debug_views


PAIR = 'Функция А()\nВозврат Б();\nКонецФункции\nФункция Б()\nВозврат 1;\nКонецФункции'
UPDATE = 'Функция Б()\nВозврат 2;\nКонецФункции'


def _upsert_views(tmp_path):
    original = SourceUnitRef(SourceUnitKind.NOTEBOOK_CELL, "pair", 1, source_sha256(PAIR))
    updated = SourceUnitRef(SourceUnitKind.NOTEBOOK_CELL, "helper", 2, source_sha256(UPDATE))
    first, second = notebook_debug_views(
        tmp_path, ((PAIR, original), (UPDATE, updated)),
    )
    return first, second, original, updated


def test_merged_worker_has_one_physical_view_and_two_exact_source_identities(tmp_path):
    first, second, original, updated = _upsert_views(tmp_path)
    assert first.modules[0].source_unit == original
    assert len(second.modules) == 1
    module = second.modules[0]
    assert module.source_units == (original, updated)
    with pytest.raises(ProtocolError, match="ambiguous"):
        _ = module.source_unit
    for source, expected_line in ((original, 2), (updated, 5)):
        mapping = resolve_source_line(module, source, 2)
        assert mapping.generated_line == expected_line
        assert mapping.reason is None
        location = map_generated_line(module, expected_line)
        assert location.source_unit == source
        assert location.line == 2
    removed_helper = resolve_source_line(module, original, 5)
    assert removed_helper.reason is WorkerBreakpointRejection.UNMAPPED_SOURCE_LINE


def test_strict_reload_preserves_retained_caller_and_binds_new_helper(tmp_path):
    first, second, original, updated = _upsert_views(tmp_path)
    coordinator = WorkerBreakpointCoordinator(session_id=UUID(int=381))
    coordinator.set_views((first,))
    caller = coordinator.prepare_add(original, "Worker", 2, enabled=True, column=None)
    coordinator.commit(caller)
    publication = coordinator.prepare_generation(second, WorkerBreakpointReloadPolicy.STRICT)
    coordinator.commit(publication)
    assert publication.removed_ids == ()
    assert coordinator.status(caller.result_id).resolution is WorkerBreakpointResolution.RESOLVED
    helper = coordinator.prepare_add(updated, "Worker", 2, enabled=True, column=None)
    coordinator.commit(helper)
    bindings = coordinator.bindings_for_view(second)
    assert {binding.breakpoint_id for binding in bindings} == {caller.result_id, helper.result_id}
    assert {binding.location.line for binding in bindings} == {2, 5}
    old_bindings = coordinator.bindings_for_view(first)
    assert {binding.breakpoint_id for binding in old_bindings} == {caller.result_id}


def test_merged_worker_stack_keeps_each_frames_own_source(tmp_path):
    _, view, original, updated = _upsert_views(tmp_path)
    module = view.modules[0]
    helper = module.registration.module_location(5)
    caller = module.registration.module_location(2)
    target = TargetId(UUID(int=382), "test")
    event = StopEvent(
        target, helper, "callStackFormed", stop_by_breakpoint=True,
        stack=(helper, caller),
        stack_frames=(StackFrame(target, 0, helper), StackFrame(target, 1, caller)),
    )
    mapped = map_worker_stop(event, operation_id=9, view=view, origin="main", bindings=())
    assert mapped.location.source_unit == updated
    assert tuple(frame.source.source_unit for frame in mapped.frames) == (updated, original)
    assert tuple(frame.source.line for frame in mapped.frames) == (2, 2)


def test_merged_debug_view_rejects_forged_or_incomplete_source_membership(tmp_path):
    _, view, original, updated = _upsert_views(tmp_path)
    module = view.modules[0]
    forged = replace(updated, source_sha256="f" * 64)
    with pytest.raises(ProtocolError, match="does not match"):
        resolve_source_line(module, forged, 2)
    with pytest.raises(ValueError, match="source"):
        replace(module, source_units=(original,))


def test_strict_reload_rejects_breakpoint_in_replaced_helper_body(tmp_path):
    first, second, original, _ = _upsert_views(tmp_path)
    coordinator = WorkerBreakpointCoordinator(session_id=UUID(int=383))
    coordinator.set_views((first,))
    helper = coordinator.prepare_add(original, "Worker", 5, enabled=True, column=None)
    coordinator.commit(helper)
    with pytest.raises(WorkerBreakpointConflict) as caught:
        coordinator.prepare_generation(second, WorkerBreakpointReloadPolicy.STRICT)
    assert caught.value.rejections[0].breakpoint_id == helper.result_id
    assert caught.value.rejections[0].reason is WorkerBreakpointRejection.UNMAPPED_SOURCE_LINE


def test_debug_admission_rejects_same_source_identity_with_conflicting_hash(tmp_path):
    from onec_runtime.bsl.source_maps import MappedSource, SourceMap

    _, view, original, _ = _upsert_views(tmp_path)
    module = view.modules[0]
    forged = replace(original, source_sha256="f" * 64)
    source_map = module.mapped_source.source_map
    segments = list(source_map.segments)
    index = next(index for index, segment in enumerate(segments) if segment.origin_ref == original)
    segments[index] = replace(segments[index], origin_ref=forged)
    forged_map = SourceMap(source_map.generated, tuple(segments))
    mapped = MappedSource(module.mapped_source.text, source_map.generated, forged_map)
    with pytest.raises(ProtocolError, match="conflicting source identit"):
        replace(
            module, mapped_source=mapped, source_map_sha256=mapped.source_map_sha256,
            source_units=(*module.source_units, forged),
        )
