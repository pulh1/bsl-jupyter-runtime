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
