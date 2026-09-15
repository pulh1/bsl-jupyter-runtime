from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

from onec_runtime.privacy import public_artifact_value
from onec_runtime.worker_breakpoints import WorkerBreakpointCoordinator
from test_worker_breakpoints import _debug_view


def _public_json(value: object) -> str:
    return json.dumps(
        public_artifact_value(value),
        ensure_ascii=False,
        default=str,
        sort_keys=True,
    )


def test_debug_views_and_workspace_plan_hide_physical_worker_locator(
    tmp_path: Path,
) -> None:
    view = _debug_view(tmp_path)
    module = view.modules[0]
    private_url = module.registration.exact_temp_storage_url
    registration_name = module.registration.registration_name
    coordinator = WorkerBreakpointCoordinator(session_id=UUID(int=91))
    coordinator.set_views((view,))
    plan = coordinator.prepare_add(
        module.source_unit,
        module.canonical_module,
        2,
        enabled=True,
        column=None,
    )

    for value in (view, module, plan, plan.desired_slots[0]):
        serialized = _public_json(value)
        assert private_url not in serialized
        assert registration_name not in serialized
        assert "e1cib/tempstorage" not in serialized.casefold()


def test_breakpoint_plan_repr_never_contains_private_locator(tmp_path: Path) -> None:
    view = _debug_view(tmp_path)
    module = view.modules[0]
    coordinator = WorkerBreakpointCoordinator(session_id=UUID(int=92))
    coordinator.set_views((view,))
    plan = coordinator.prepare_add(
        module.source_unit,
        module.canonical_module,
        1,
        enabled=True,
        column=None,
    )
    coordinator.commit(plan, workspace_confirmed=True)

    rendered = repr(plan)
    assert module.registration.exact_temp_storage_url not in rendered
    assert "location=<redacted>" in repr(coordinator.bindings_for_view(view)[0])


def test_capture_value_snapshots_expose_only_normalized_public_fields() -> None:
    from onec_runtime.capture_values import (
        SafePathSegment,
        SafeValuePath,
        ValueNode,
        ValuePage,
        ValuePathSegmentKind,
        ValueRoot,
        ValueRootKind,
        ValueShape,
    )

    secret = object()
    path = SafeValuePath(
        ValueRoot(ValueRootKind.CONTEXT),
        (SafePathSegment(ValuePathSegmentKind.VARIABLE, "Данные"),),
    )
    node = ValueNode(
        "Данные", "Структура", "2 elements", 2, True,
        ValueShape.STRUCTURE, path,
        _owner=secret,
    )
    page = ValuePage((node,), 1, None, SafeValuePath(path.root), "variables", 0, 20)

    node_wire = public_artifact_value(node)
    page_wire = public_artifact_value(page)
    serialized = json.dumps((node_wire, page_wire), ensure_ascii=False, default=str)
    assert set(node_wire) == {
        "name", "type_name", "preview", "size", "expandable", "shape",
        "path", "private", "cycle",
    }
    assert set(page_wire) == {
        "items", "total", "next_cursor", "path", "view", "start", "stop",
    }
    assert "object at" not in serialized
    assert "_owner" not in serialized and "_lineage" not in serialized
