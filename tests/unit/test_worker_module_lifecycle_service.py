"""Module generations use one stopped-route arbiter owner."""

from threading import current_thread
from types import SimpleNamespace

import pytest

from onec_runtime.errors import ProtocolError, StaleWorkerGeneration
from onec_runtime.bsl.module_catalog import (
    CommonModuleCatalogSnapshot, CommonModuleDescriptor, CommonModuleScope,
)
from onec_runtime.execution.arbiter import (
    OutcomeUnknown, RdbgArbiter, RouteToken, SessionPort, Settlement,
)
from onec_runtime.execution.worker_activation import (
    WorkerActivationSnapshot, WorkerUniverseActivationAdapter,
)
from onec_runtime.execution.worker import WorkerActivationUnknown
from onec_runtime.execution.worker_module_lifecycle import WorkerModuleLifecycleService
from onec_runtime.execution.worker_mutation import MainPausedWorkerRoute
from onec_runtime.rdbg.models import TargetId
from onec_runtime.worker_breakpoints import WorkerBreakpointReloadPolicy
from onec_runtime.worker_universe import WorkerGenerationHandle, WorkerUniverseRegistry

from test_execution_route_sequence import TARGET
from test_worker_universe import (
    _UniverseTargetExecutor, _builder, _lowered, _notebook_builder,
)


def test_pure_module_preparer_builds_validated_artifacts_without_rdbg(tmp_path) -> None:
    from onec_runtime.bsl.parser_target import PythonParserTarget
    from onec_runtime.execution.worker_module_lifecycle import (
        WorkerModuleArtifactPreparer,
    )
    from onec_runtime.worker_universe import validate_worker_module_artifact

    unit = _lowered()[0].analysis.unit
    builder = _builder(tmp_path)[0]
    preparer = WorkerModuleArtifactPreparer(
        PythonParserTarget.from_generated(), builder,
    )

    artifacts = preparer((unit,), CATALOG, None)
    assert len(artifacts) == 1
    assert artifacts[0].logical_name == unit.logical_name
    assert validate_worker_module_artifact(artifacts[0]) == artifacts[0].exports


CATALOG = CommonModuleCatalogSnapshot.create(
    profile="server-test", preprocessor_profile="server", revision=1,
    modules=(CommonModuleDescriptor("МодульРасчета", CommonModuleScope.SERVER),),
)


class HeartbeatSession:
    def __init__(self) -> None:
        self.target = None
        self.calls = []

    def heartbeat(self, *, on_transport_dispatch):
        on_transport_dispatch()
        self.calls.append(current_thread())
        return {"ok": True}


class Publisher:
    def __init__(self) -> None:
        self.active = None
        self.calls = []
        self.released = []
        self.failure = None

    def snapshot(self):
        return WorkerActivationSnapshot(0, (), None, self.active)

    def publish_modules(self, artifacts, *, port):
        assert isinstance(port, SessionPort)
        port.heartbeat()
        self.calls.append(tuple(artifact.logical_name for artifact in artifacts))
        if self.failure is not None:
            raise self.failure
        self.active = WorkerGenerationHandle(1, 1, len(self.calls), "a" * 64)
        return self.active

    def release_generation(self, handle, *, port):
        assert isinstance(port, SessionPort)
        port.heartbeat()
        self.released.append(handle)


def _bound(*, breakpoints=False, changed=None):
    unit = _lowered()[0].analysis.unit
    session = HeartbeatSession()
    arbiter = RdbgArbiter(session, RouteToken("worker-test", 1, 0, "main"))
    publisher = Publisher()
    route = [MainPausedWorkerRoute(TARGET)]
    reader = lambda: route[0]

    def prepare(units, catalog, profiler):
        assert catalog is not None
        assert profiler is None
        if changed is not None:
            route[0] = changed
        return tuple(
            SimpleNamespace(logical_name=item.logical_name, revision=item.revision)
            for item in units
        )

    service = WorkerModuleLifecycleService(
        arbiter, publisher,
        prepare_artifacts=prepare,
        route_provider=reader,
        require_mutation_boundary=lambda: None,
        worker_breakpoints_present=lambda: breakpoints,
    )
    return unit, session, arbiter, publisher, service


def test_load_confirms_units_on_one_arbiter_worker_and_release_is_serialized() -> None:
    unit, session, arbiter, publisher, service = _bound()
    try:
        handle = service.load_worker_modules((unit,), common_modules=CATALOG)
        assert service.confirmed_worker_module_units(handle) == (unit,)
        assert service.last_worker_breakpoint_reload_report() is None
        service.release_worker_generation(handle)
        assert publisher.released == [handle]
        assert session.calls == [arbiter._worker, arbiter._worker]
    finally:
        arbiter.close(timeout=3)


def test_stale_route_rejects_publication_before_remote_effect() -> None:
    changed = MainPausedWorkerRoute(TargetId(TARGET.id, "other-target"))
    unit, session, arbiter, publisher, service = _bound(changed=changed)
    try:
        with pytest.raises(ProtocolError, match="route"):
            service.load_worker_modules((unit,), common_modules=CATALOG)
        assert publisher.calls == []
        assert session.calls == []
    finally:
        arbiter.close(timeout=3)


def test_release_rejects_handle_superseded_by_another_worker_generation() -> None:
    unit, session, arbiter, publisher, service = _bound()
    try:
        handle = service.load_worker_modules((unit,), common_modules=CATALOG)
        publisher.active = WorkerGenerationHandle(1, 1, 2, "b" * 64)
        prior_calls = tuple(session.calls)
        with pytest.raises(StaleWorkerGeneration):
            service.release_worker_generation(handle)
        assert publisher.released == []
        assert tuple(session.calls) == prior_calls
    finally:
        arbiter.close(timeout=3)


def test_active_breakpoints_reject_module_reload_without_report_port() -> None:
    unit, session, arbiter, publisher, service = _bound(breakpoints=True)
    try:
        with pytest.raises(ProtocolError, match="breakpoint"):
            service.load_worker_modules(
                (unit,), common_modules=CATALOG,
                breakpoint_policy=WorkerBreakpointReloadPolicy.RESET_INCOMPATIBLE,
            )
        assert publisher.calls == []
        assert session.calls == []
    finally:
        arbiter.close(timeout=3)


def test_unknown_publication_retains_exact_lease_and_arbiter_ticket() -> None:
    unit, session, arbiter, publisher, service = _bound()

    class Lease:
        def __init__(self):
            self.retained = []

        def retain_outcome_unknown(self, *, port):
            self.retained.append(port)

    lease = Lease()
    publisher.failure = WorkerActivationUnknown(lease, "unconfirmed root")
    try:
        with pytest.raises(OutcomeUnknown):
            service.load_worker_modules((unit,), common_modules=CATALOG)
        ticket = arbiter.active_ticket
        assert ticket is not None
        assert ticket.status().phase == "unknown"
        assert len(lease.retained) == 1
        assert isinstance(lease.retained[0], SessionPort)
        assert publisher.calls == [(unit.logical_name,)]
        arbiter.reconcile(ticket, lambda _port: Settlement(None))
        ticket.wait_settled(3)
    finally:
        arbiter.close(timeout=3)


def test_existing_activation_owner_can_publish_module_artifacts_on_supplied_port(
    tmp_path,
) -> None:
    lowered, context = _lowered()
    descriptor = _builder(tmp_path)[0].build(
        lowered, visible_source_context=context,
    )
    host = WorkerUniverseRegistry(runtime_generation=1, context_generation=1)
    target = _UniverseTargetExecutor()
    port = object()

    def execute(supplied_port, source):
        assert supplied_port is port
        candidate = host._pending
        if candidate is not None:
            target.acknowledge(candidate)
        return target(source)

    notebook_root = tmp_path / "notebook"
    notebook_root.mkdir()
    adapter = WorkerUniverseActivationAdapter(
        host,
        notebook_builder=_notebook_builder(notebook_root),
        instruction_runner=execute,
        worker_breakpoints_present=lambda: False,
        target_profile="server-test",
    )

    handle = adapter.publish_modules((descriptor,), port=port)
    assert adapter.snapshot().active_handle is handle
    assert adapter.snapshot().worker_exports == descriptor.exports
    assert adapter.materialization_snapshot().registrations
    adapter.release_generation(handle, port=port)
