"""Module generations use one stopped-route arbiter owner."""

from pathlib import Path
from threading import Event, Thread, current_thread
from types import SimpleNamespace
from uuid import UUID

import pytest

from onec_runtime.breakpoint_workspace import (
    BreakpointWorkspaceController, WorkspaceSnapshot,
)
from onec_runtime.bsl.source_maps import (
    SourceUnitKind, SourceUnitRef, mapped_visible_source, source_sha256,
)
from onec_runtime.errors import ProtocolError, StaleWorkerGeneration
from onec_runtime.bsl.module_catalog import (
    CommonModuleCatalogSnapshot, CommonModuleDescriptor, CommonModuleScope,
    SessionCommonModuleCatalog,
)
from onec_runtime.bsl.module_universe import WorkerModuleUnit
from onec_runtime.execution.arbiter import (
    OutcomeUnknown, RdbgArbiter, RouteToken, SessionPort, Settlement,
)
from onec_runtime.execution.worker_activation import (
    WorkerActivationSnapshot, WorkerUniverseActivationAdapter,
)
from onec_runtime.execution.worker import WorkerActivationUnknown
from onec_runtime.execution.worker_breakpoint_workspace import WorkerBreakpointWorkspace
from onec_runtime.execution.worker_catalog_resolver import resolve_worker_module_catalog
from onec_runtime.execution.worker_module_lifecycle import WorkerModuleLifecycleService
from onec_runtime.execution.worker_mutation import MainPausedWorkerRoute
from onec_runtime.performance_profile import PhaseRecorder
from onec_runtime.rdbg.models import ModuleLocation, TargetId
from onec_runtime.worker_breakpoints import (
    WorkerBreakpointReloadOutcome, WorkerBreakpointReloadPolicy,
    WorkerBreakpointReloadReport, WorkerBreakpointCoordinator,
)
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

    resolution = resolve_worker_module_catalog(CATALOG, (unit,))
    artifacts = preparer((unit,), resolution, None)
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
    def __init__(self, *, breakpoints=False) -> None:
        self.supports_breakpoint_reload = True
        self.active = None
        self.calls = []
        self.artifact_batches = []
        self.released = []
        self.failure = None
        self.breakpoints = breakpoints
        self.report = None
        self.report_reads = 0
        self.remove_points_on_publish = False

    def snapshot(self):
        return WorkerActivationSnapshot(0, (), None, self.active)

    def publish_modules(self, artifacts, *, port, reload_policy):
        assert isinstance(port, SessionPort)
        port.heartbeat()
        self.calls.append(tuple(artifact.logical_name for artifact in artifacts))
        self.artifact_batches.append(artifacts)
        if self.failure is not None:
            raise self.failure
        self.active = WorkerGenerationHandle(1, 1, len(self.calls), "a" * 64)
        if self.breakpoints:
            from uuid import uuid4
            self.report = WorkerBreakpointReloadReport(
                uuid4(), self.active, reload_policy,
                WorkerBreakpointReloadOutcome.COMMITTED, (), (), 1,
            )
            if self.remove_points_on_publish:
                self.breakpoints = False
        return self.active

    def release_generation(self, handle, *, port):
        assert isinstance(port, SessionPort)
        port.heartbeat()
        self.released.append(handle)

    def last_worker_breakpoint_reload_report(self):
        self.report_reads += 1
        return self.report


def _bound(*, breakpoints=False, changed=None):
    unit = _lowered()[0].analysis.unit
    session = HeartbeatSession()
    arbiter = RdbgArbiter(session, RouteToken("worker-test", 1, 0, "main"))
    publisher = Publisher(breakpoints=breakpoints)
    route = [MainPausedWorkerRoute(TARGET)]
    reader = lambda: route[0]

    def prepare(units, resolution, profiler):
        assert isinstance(resolution.catalog, CommonModuleCatalogSnapshot)
        assert len(resolution.models) == len(units)
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
        worker_breakpoints_present=lambda: publisher.breakpoints,
    )
    return unit, session, arbiter, publisher, service


def _source_unit(name: str, source: str) -> WorkerModuleUnit:
    reference = SourceUnitRef(
        SourceUnitKind.MODULE, name, 1, source_sha256(source),
    )
    return WorkerModuleUnit(
        name, "module", 1, mapped_visible_source(source, reference),
    )


def _add_common_module(root: Path, name: str) -> None:
    directory = root / "CommonModules"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.xml").write_text(
        "<MetaDataObject><CommonModule><Properties>"
        f"<Name>{name}</Name><Global>false</Global><Server>true</Server>"
        "<ClientManagedApplication>false</ClientManagedApplication>"
        "<ClientOrdinaryApplication>false</ClientOrdinaryApplication>"
        "</Properties></CommonModule></MetaDataObject>",
        encoding="utf-8",
    )


def test_lazy_catalog_resolves_parsed_bare_names_and_required_module(tmp_path) -> None:
    _add_common_module(tmp_path, "МодульА")
    _add_common_module(tmp_path, "МодульБ")
    unit = _source_unit(
        "МодульА",
        "Функция Версия() Экспорт\nВозврат МодульБ.Версия();\nКонецФункции\n",
    )
    source = SessionCommonModuleCatalog(tmp_path, profile="server-test")

    resolution = resolve_worker_module_catalog(source, (unit,))

    assert isinstance(resolution.catalog, CommonModuleCatalogSnapshot)
    assert tuple(item.canonical_name for item in resolution.catalog.modules) == (
        "МодульА", "МодульБ",
    )


def test_lazy_catalog_passes_one_parsed_model_into_artifact_preparation(tmp_path) -> None:
    from onec_runtime.bsl.parser_target import PythonParserTarget
    from onec_runtime.execution.worker_module_lifecycle import WorkerModuleArtifactPreparer

    _add_common_module(tmp_path, "МодульА")
    unit = _source_unit(
        "МодульА",
        "Функция Версия() Экспорт\nВозврат 1;\nКонецФункции\n",
    )
    profiler = PhaseRecorder()
    resolution = resolve_worker_module_catalog(
        SessionCommonModuleCatalog(tmp_path, profile="server-test"),
        (unit,), profiler=profiler,
    )
    preparer = WorkerModuleArtifactPreparer(
        PythonParserTarget.from_generated(), _builder(tmp_path)[0],
    )

    artifacts = preparer((unit,), resolution, profiler)

    assert len(artifacts) == 1
    assert artifacts[0].logical_name == unit.logical_name
    assert profiler.parser_calls.full_module_parses == 1


def test_lazy_catalog_uses_retained_units_before_next_publication(tmp_path) -> None:
    _add_common_module(tmp_path, "МодульА")
    first = _source_unit(
        "МодульА",
        "Функция Версия() Экспорт\nВозврат Поздний.Версия();\nКонецФункции\n",
    )
    source = SessionCommonModuleCatalog(tmp_path, profile="server-test")
    _, session, arbiter, publisher, service = _bound()
    try:
        service.load_worker_modules((first,), common_modules=source)
        _add_common_module(tmp_path, "Поздний")
        _add_common_module(tmp_path, "МодульБ")
        second = _source_unit(
            "МодульБ",
            "Функция Версия() Экспорт\nВозврат 2;\nКонецФункции\n",
        )

        service.load_worker_modules((second,), common_modules=source)

        assert tuple(item.canonical_name for item in service._catalog.modules) == (
            "МодульА", "МодульБ", "Поздний",
        )
        assert publisher.calls == [("МодульА",), ("МодульА", "МодульБ")]
        assert session.calls == [arbiter._worker, arbiter._worker]
    finally:
        arbiter.close(timeout=3)


def test_catalog_growth_preserves_retained_local_binding_and_reuses_its_artifact(
    tmp_path,
) -> None:
    from onec_runtime.bsl.parser_target import PythonParserTarget
    from onec_runtime.execution.worker_module_lifecycle import WorkerModuleArtifactPreparer

    first = _source_unit(
        "МодульА",
        "Функция Версия() Экспорт\n"
        "    Локальная = 1;\n"
        "    Возврат Локальная;\n"
        "КонецФункции\n",
    )
    second = _source_unit(
        "МодульБ",
        "Функция Версия() Экспорт\nВозврат 2;\nКонецФункции\n",
    )
    initial = CommonModuleCatalogSnapshot.create(
        profile="server-test", preprocessor_profile="server", revision=1,
        modules=(CommonModuleDescriptor("МодульА", CommonModuleScope.SERVER),),
    )
    expanded = CommonModuleCatalogSnapshot.create(
        profile="server-test", preprocessor_profile="server", revision=2,
        modules=tuple(
            CommonModuleDescriptor(name, CommonModuleScope.SERVER)
            for name in ("МодульА", "МодульБ", "Локальная")
        ),
    )
    arbiter = RdbgArbiter(
        HeartbeatSession(), RouteToken("worker-test", 1, 0, "main"),
    )
    publisher = Publisher()
    preparer = WorkerModuleArtifactPreparer(
        PythonParserTarget.from_generated(), _builder(tmp_path)[0],
    )
    service = WorkerModuleLifecycleService(
        arbiter, publisher, prepare_artifacts=preparer,
        route_provider=lambda: MainPausedWorkerRoute(TARGET),
        require_mutation_boundary=lambda: None,
        worker_breakpoints_present=lambda: False,
    )
    try:
        service.load_worker_modules((first,), common_modules=initial)
        retained = publisher.artifact_batches[0][0]
        profile = PhaseRecorder()
        service.load_worker_modules(
            (second,), common_modules=expanded, profiler=profile,
        )
        assert profile.parser_calls.full_module_parses == 1
        assert publisher.artifact_batches[1][0] is retained
        assert service.confirmed_worker_module_units(
            publisher.active,
        ) == (first, second)
    finally:
        arbiter.close(timeout=3)


def test_nonmonotonic_catalog_is_rejected_before_candidate_dependency_analysis(
    tmp_path,
) -> None:
    from onec_runtime.bsl.parser_target import PythonParserTarget
    from onec_runtime.execution.worker_module_lifecycle import WorkerModuleArtifactPreparer

    unit = _source_unit(
        "МодульА",
        "МодульБ.Версия();\n"
        "Функция Версия() Экспорт\nВозврат 1;\nКонецФункции\n",
    )
    original = CommonModuleCatalogSnapshot.create(
        profile="server-test", preprocessor_profile="server", revision=1,
        modules=(CommonModuleDescriptor("МодульА", CommonModuleScope.SERVER),),
    )
    changed_in_place = CommonModuleCatalogSnapshot.create(
        profile="server-test", preprocessor_profile="server", revision=1,
        modules=tuple(
            CommonModuleDescriptor(name, CommonModuleScope.SERVER)
            for name in ("МодульА", "МодульБ")
        ),
    )
    arbiter = RdbgArbiter(
        HeartbeatSession(), RouteToken("worker-test", 1, 0, "main"),
    )
    publisher = Publisher()
    service = WorkerModuleLifecycleService(
        arbiter, publisher,
        prepare_artifacts=WorkerModuleArtifactPreparer(
            PythonParserTarget.from_generated(), _builder(tmp_path)[0],
        ),
        route_provider=lambda: MainPausedWorkerRoute(TARGET),
        require_mutation_boundary=lambda: None,
        worker_breakpoints_present=lambda: False,
    )
    try:
        service.load_worker_modules((unit,), common_modules=original)
        with pytest.raises(ProtocolError, match="not monotonic"):
            service.load_worker_modules(
                (unit,), common_modules=changed_in_place,
            )
        assert len(publisher.artifact_batches) == 1
    finally:
        arbiter.close(timeout=3)


def test_failed_publication_keeps_only_confirmed_preparation_for_retry(
    tmp_path,
) -> None:
    from onec_runtime.bsl.parser_target import PythonParserTarget
    from onec_runtime.execution.worker_module_lifecycle import WorkerModuleArtifactPreparer

    original = _source_unit(
        "МодульА",
        "Функция Версия() Экспорт\nВозврат 1;\nКонецФункции\n",
    )
    changed = _source_unit(
        "МодульА",
        "Функция Версия() Экспорт\nВозврат 2;\nКонецФункции\n",
    )
    catalog = CommonModuleCatalogSnapshot.create(
        profile="server-test", preprocessor_profile="server", revision=1,
        modules=(CommonModuleDescriptor("МодульА", CommonModuleScope.SERVER),),
    )
    arbiter = RdbgArbiter(
        HeartbeatSession(), RouteToken("worker-test", 1, 0, "main"),
    )
    publisher = Publisher()
    service = WorkerModuleLifecycleService(
        arbiter, publisher,
        prepare_artifacts=WorkerModuleArtifactPreparer(
            PythonParserTarget.from_generated(), _builder(tmp_path)[0],
        ),
        route_provider=lambda: MainPausedWorkerRoute(TARGET),
        require_mutation_boundary=lambda: None,
        worker_breakpoints_present=lambda: False,
    )
    try:
        service.load_worker_modules((original,), common_modules=catalog)
        confirmed_artifact = publisher.artifact_batches[-1][0]
        confirmed_handle = publisher.active
        publisher.failure = ProtocolError("publication rejected")
        with pytest.raises(ProtocolError, match="publication rejected"):
            service.load_worker_modules((changed,), common_modules=catalog)
        publisher.failure = None
        assert service.confirmed_worker_module_units(
            confirmed_handle,
        ) == (original,)
        profile = PhaseRecorder()
        service.load_worker_modules(
            (original,), common_modules=catalog, profiler=profile,
        )
        assert profile.parser_calls.full_module_parses == 0
        assert publisher.artifact_batches[-1][0] is confirmed_artifact
        profile = PhaseRecorder()
        service.load_worker_modules(
            (changed,), common_modules=catalog, profiler=profile,
        )
        assert profile.parser_calls.full_module_parses == 1
        assert publisher.artifact_batches[-1][0] is not confirmed_artifact
    finally:
        arbiter.close(timeout=3)


def test_missing_required_module_fails_before_arbiter_dispatch(tmp_path) -> None:
    (tmp_path / "CommonModules").mkdir()
    unit = _source_unit(
        "Отсутствует",
        "Функция Версия() Экспорт\nВозврат 1;\nКонецФункции\n",
    )
    source = SessionCommonModuleCatalog(tmp_path, profile="server-test")
    _, session, arbiter, publisher, service = _bound()
    try:
        with pytest.raises(ProtocolError, match="missing common module"):
            service.load_worker_modules((unit,), common_modules=source)
        assert publisher.calls == []
        assert session.calls == []
        assert arbiter.active_ticket is None
    finally:
        arbiter.close(timeout=3)


def test_lifecycle_exposes_exact_arbiter_owner_read_only() -> None:
    _, _, arbiter, _, service = _bound()
    try:
        assert service.arbiter is arbiter
        with pytest.raises(AttributeError):
            service.arbiter = object()
    finally:
        arbiter.close(timeout=3)


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


def test_load_waits_for_prior_value_reply_cleanup_before_route_admission() -> None:
    unit, _session, arbiter, publisher, service = _bound()
    cleanup_entered = Event()
    release_cleanup = Event()
    finished = Event()
    outcomes = []

    def cleanup_plan(_port):
        cleanup_entered.set()
        assert release_cleanup.wait(3)
        return Settlement(None)

    def parent_plan(port):
        port.register_post_settlement_cleanup(cleanup_plan)
        return Settlement(None)

    def load():
        try:
            outcomes.append(service.load_worker_modules((unit,), common_modules=CATALOG))
        except BaseException as error:
            outcomes.append(error)
        finally:
            finished.set()

    parent = arbiter.submit(arbiter.current_route, parent_plan)
    arbiter.dispatch(parent)
    worker = Thread(target=load)
    try:
        parent.wait_settled(3)
        assert cleanup_entered.wait(3)
        worker.start()
        assert not finished.wait(0.1)
        release_cleanup.set()
        assert finished.wait(3)
        assert len(outcomes) == 1
        assert isinstance(outcomes[0], WorkerGenerationHandle)
        assert publisher.calls == [(unit.logical_name,)]
    finally:
        release_cleanup.set()
        worker.join(3) if worker.ident is not None else None
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


def test_active_breakpoints_use_per_call_policy_and_report() -> None:
    unit, session, arbiter, publisher, service = _bound(breakpoints=True)
    try:
        handle = service.load_worker_modules(
            (unit,), common_modules=CATALOG,
            breakpoint_policy=WorkerBreakpointReloadPolicy.RESET_INCOMPATIBLE,
        )
        report = service.last_worker_breakpoint_reload_report()
        assert report is publisher.report
        assert report.candidate_handle is handle
        assert report.policy is WorkerBreakpointReloadPolicy.RESET_INCOMPATIBLE
        assert publisher.calls == [(unit.logical_name,)]
        assert session.calls == [arbiter._worker]
    finally:
        arbiter.close(timeout=3)


def test_reset_report_is_checked_even_when_all_breakpoints_are_removed() -> None:
    unit, session, arbiter, publisher, service = _bound(breakpoints=True)
    publisher.remove_points_on_publish = True
    try:
        service.load_worker_modules(
            (unit,), common_modules=CATALOG,
            breakpoint_policy=WorkerBreakpointReloadPolicy.RESET_INCOMPATIBLE,
        )
        assert publisher.breakpoints is False
        assert publisher.report_reads == 1
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


def test_module_publication_retains_notebook_method_route_names(tmp_path) -> None:
    from onec_runtime.bsl.parser_target import PythonParserTarget
    from onec_runtime.execution.common import NotebookCommonParser
    from onec_runtime.execution.snapshot_binding import (
        RoutePreparationSnapshot, SnapshotRouteBinding,
    )

    lowered, context = _lowered()
    descriptor = _builder(tmp_path)[0].build(
        lowered, visible_source_context=context,
    )
    notebook_source = "Функция Первый() Экспорт\nВозврат 1;\nКонецФункции"
    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL, "notebook-before-module", 1,
        source_sha256(notebook_source),
    )
    parser = PythonParserTarget.from_generated()
    common = NotebookCommonParser(parser).prepare(notebook_source, unit)
    owner = object()
    intent = SnapshotRouteBinding(parser, owner=owner, version=1).worker_intent(
        common, RoutePreparationSnapshot(owner, 1, (), ()).for_pipeline(),
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

    notebook_root = tmp_path / "notebook-after-module"
    notebook_root.mkdir()
    adapter = WorkerUniverseActivationAdapter(
        host, notebook_builder=_notebook_builder(notebook_root),
        instruction_runner=execute,
        worker_breakpoints_present=lambda: False,
        target_profile="server-test",
    )
    adapter.activate(intent, port=port)
    adapter.publish_modules((descriptor,), port=port)

    snapshot = adapter.snapshot()
    RoutePreparationSnapshot(
        owner, 3, (), snapshot.worker_exports, snapshot.active_methods,
    )
    assert any(
        export.public_path == "Первый" and export.receiver_module == "Worker"
        for export in snapshot.worker_exports
    )
    assert any(
        export.public_path.startswith("МодульРасчета.")
        for export in snapshot.worker_exports
    )


def test_module_publication_uses_shared_breakpoint_workspace_and_reports_policy(
    tmp_path,
) -> None:
    lowered, context = _lowered()
    descriptor = _builder(tmp_path)[0].build(
        lowered, visible_source_context=context,
    )
    unit = lowered.analysis.unit
    reference = SourceUnitRef(
        SourceUnitKind.MODULE, unit.logical_name, unit.revision,
        source_sha256(unit.mapped_source.text),
    )
    host = WorkerUniverseRegistry(runtime_generation=1, context_generation=1)
    target = _UniverseTargetExecutor()
    breakpoints = WorkerBreakpointCoordinator(session_id=UUID(int=50))
    point = breakpoints.prepare_add(
        reference, unit.logical_name.casefold(), 2, enabled=True, column=None,
    )
    breakpoints.commit(point)
    service_location = ModuleLocation(
        "ExtensionModule", "", UUID(int=1), UUID(int=2), 1,
    )

    class DeniedSession:
        def set_breakpoints(self, _locations):
            pytest.fail("direct RDBG workspace mutation")

    owner = BreakpointWorkspaceController(
        DeniedSession(), WorkspaceSnapshot(0, service_location, (), (), (), False),
    )

    class Port:
        def __init__(self):
            self.writes = []

        def set_breakpoints(self, locations):
            self.writes.append(locations)

    port = Port()

    def execute(supplied_port, source):
        assert supplied_port is port
        candidate = host._pending
        if candidate is not None:
            target.acknowledge(candidate)
        return target(source)

    notebook_root = tmp_path / "notebook"
    notebook_root.mkdir()
    adapter = WorkerUniverseActivationAdapter(
        host, notebook_builder=_notebook_builder(notebook_root),
        instruction_runner=execute,
        worker_breakpoints_present=lambda: bool(breakpoints.list_statuses()),
        breakpoint_workspace=WorkerBreakpointWorkspace(breakpoints, owner),
        target_profile="server-test",
    )

    handle = adapter.publish_modules(
        (descriptor,), port=port,
        reload_policy=WorkerBreakpointReloadPolicy.RESET_INCOMPATIBLE,
    )
    report = adapter.last_worker_breakpoint_reload_report()
    assert report is not None
    assert report.candidate_handle is handle
    assert report.policy is WorkerBreakpointReloadPolicy.RESET_INCOMPATIBLE
    assert report.outcome is WorkerBreakpointReloadOutcome.COMMITTED
    assert port.writes
