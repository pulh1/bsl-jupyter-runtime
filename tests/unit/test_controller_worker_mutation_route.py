"""Worker mutations receive the exact current controller route."""

from onec_runtime.execution.composition import build_execution_core
from onec_runtime.execution.snapshot_binding import RoutePreparationSnapshot
from onec_runtime.execution.worker_mutation import (
    CapturePausedWorkerRoute, MainPausedWorkerRoute,
)

from test_execution_controller_routes import BUSINESS, KERNEL, CompleteSession, TARGET


class _Replies:
    def diagnostic_reply(self, diagnostic):
        raise AssertionError(diagnostic.message)

    def unavailable_reply(self, unavailable):
        raise AssertionError(unavailable.reason)


def test_worker_route_tracks_main_capture_and_same_main_completion() -> None:
    owner = object()
    core = build_execution_core(
        CompleteSession(), KERNEL, runtime_generation=1,
        capture_locations=(BUSINESS,), initial_target_id=TARGET,
        snapshot_provider=lambda: RoutePreparationSnapshot(owner, 1, (), ()),
        reply_presenter=_Replies(),
    )
    try:
        initial = core.controller.worker_mutation_route()
        assert isinstance(initial, MainPausedWorkerRoute)
        assert initial.target_id == TARGET

        core.controller.submit_main("Результат = 1;").wait_settled(3)
        captured = core.controller.worker_mutation_route()
        assert isinstance(captured, CapturePausedWorkerRoute)
        assert captured.main_operation is core.controller.main_operation
        assert captured.scope is core.controller.capture_scope

        core.controller.submit_resume().wait_settled(3)
        completed = core.controller.worker_mutation_route()
        assert isinstance(completed, MainPausedWorkerRoute)
        assert completed.previous_main is captured.main_operation
    finally:
        core.arbiter.close(timeout=3)
