from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
from threading import Event, Thread

import nbformat
import pytest

import onec_runtime_mcp.agent.contracts as agent_contracts
from onec_runtime_mcp.agent.contracts import (
    AgentOperationState,
    BackendExecution,
    CapabilityMode,
    FailureCategory,
    OperationDescriptor,
    RuntimeDescriptor,
    ServiceResponse,
    to_wire,
)
from onec_runtime_mcp.agent.capture_contracts import CaptureFence
from onec_runtime_mcp.agent.proxies import ProxyProvenance, ProxyRealm
from onec_runtime_mcp.agent.service import AgentWorkspaceService
from onec_runtime.errors import ProtocolError
from onec_runtime.runtime_api import RuntimeNamespaceSnapshot
from onec_runtime.bsl import (
    DiagnosticStage,
    MappingConfidence,
    NormalizedDiagnostic,
    SourceUnitKind,
    SourceUnitRef,
    VisibleSourceContext,
    mapped_visible_source,
    parse_platform_diagnostic,
    remap_platform_diagnostic,
    source_sha256,
)


class FakeRuntimeBackend:
    runtime_id = "runtime-test"

    def __init__(self, mode: CapabilityMode = CapabilityMode.EXPERIMENT) -> None:
        self.sources: list[str] = []
        self.closed = False
        self.close_calls = 0
        self.mode = mode
        self.names: tuple[str, ...] = ("Порог",)
        self.names_after_execute: tuple[str, ...] | None = None
        self.changed_roots: tuple[str, ...] = ()
        self.project_calls: list[tuple[object, object, dict[str, object]]] = []
        self.value_calls: list[tuple[str, dict[str, object]]] = []
        self.guard_calls: list[str] = []
        self.forbidden_handles: set[str] = set()

    def execute_bsl(self, source: str) -> BackendExecution:
        self.sources.append(source)
        if self.names_after_execute is not None:
            self.names = self.names_after_execute
        return BackendExecution.completed(
            messages=(),
            result_present=True,
            changed_roots=self.changed_roots,
        )

    def execute_bsl_with_provenance(
        self,
        source: str,
        *,
        source_unit: object,
        on_execution_provenance,
    ) -> BackendExecution:  # type: ignore[no-untyped-def]
        digest = hashlib.sha256(source.encode()).hexdigest()
        assert getattr(source_unit, "source_sha256") == digest
        mapped = mapped_visible_source(source, source_unit)  # type: ignore[arg-type]
        on_execution_provenance(
            agent_contracts.OperationExecutionProvenance(
                visible_source_sha256=digest,
                executed_source_sha256=mapped.artifact.source_sha256,
                source_map_sha256=mapped.source_map_sha256,
                mode="main",
            )
        )
        return self.execute_bsl(source)

    def status(self) -> RuntimeDescriptor:
        return RuntimeDescriptor(self.runtime_id, 1, "ready", self.mode)

    @property
    def is_closed(self) -> bool:
        return self.closed

    def namespace_snapshot(self) -> RuntimeNamespaceSnapshot:
        return RuntimeNamespaceSnapshot(1, 1, self.names)

    def validate_value_reference(self, handle: str) -> None:
        self.guard_calls.append(handle)
        if handle in self.forbidden_handles:
            raise ProtocolError("Worker generation objects are not public values")

    def materialize_value(self, handle: str, **options: object) -> object:
        self.value_calls.append((handle, options))
        return 42

    def materialize_table(self, handle: str, **options: object) -> object:
        del handle, options
        return []

    def materialization_kind(
        self, handle: str, *, timeout_s: float | None = None
    ) -> str:
        del timeout_s
        del handle
        return "value"

    def materialize_value_payload(self, handle: str, **options: object) -> bytes:
        del handle, options
        return json.dumps(
            {"version": 1, "root": {"t": "number", "v": "42"}},
            separators=(",", ":"),
        ).encode()

    def materialize_table_payload(self, handle: str, **options: object) -> bytes:
        del handle, options
        raise AssertionError("table payload not expected")

    def project_value_payload(self, handle, selection, **options):  # type: ignore[no-untyped-def]
        self.project_calls.append((handle, selection, options))
        return "value", json.dumps(
            {"version": 1, "root": {"t": "number", "v": "42"}},
            separators=(",", ":"),
        ).encode()

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True


class FakeRuntimeFactory:
    def __init__(self, backend: FakeRuntimeBackend) -> None:
        self.backend = backend
        self.starts = 0

    def start(self, *, mode: CapabilityMode) -> FakeRuntimeBackend:
        self.starts += 1
        self.backend.mode = mode
        return self.backend


@pytest.mark.parametrize("inline", [False, True])
def test_main_execution_provenance_is_durable_before_saved_or_inline_target_call(
    tmp_path: Path,
    inline: bool,
) -> None:
    """Break caught: saved/inline MAIN dispatch races ahead of its durable manifest."""
    source = "Результат = 42;"
    digest = hashlib.sha256(source.encode()).hexdigest()
    if not inline:
        cell = nbformat.v4.new_code_cell(source=source, id="cell-main")
        cell.metadata["onec_runtime"] = {
            "revision": 1,
            "language": "bsl",
            "mode": "main",
            "source_sha256": digest,
        }
        nbformat.write(
            nbformat.v4.new_notebook(cells=[cell]),
            tmp_path / "demo.ipynb",
        )

    class ProvenanceBackend(FakeRuntimeBackend):
        def __init__(self) -> None:
            super().__init__()
            self.observed_before_target: list[object] = []
            self.target_calls = 0

        def execute_bsl_with_provenance(
            self,
            exact_source: str,
            *,
            source_unit: object,
            on_execution_provenance,
        ) -> BackendExecution:  # type: ignore[no-untyped-def]
            assert exact_source == source
            assert getattr(source_unit, "source_sha256") == digest
            manifest = agent_contracts.OperationExecutionProvenance(
                visible_source_sha256=digest,
                executed_source_sha256="b" * 64,
                source_map_sha256="c" * 64,
                mode="main",
            )
            on_execution_provenance(manifest)
            descriptor = service._operations.list()[-1]
            self.observed_before_target.append(
                service._operations.view_snapshot(
                    descriptor.operation_id
                ).execution_provenance
            )
            self.target_calls += 1
            return BackendExecution.completed(messages=(), result_present=False)

    backend = ProvenanceBackend()
    service = AgentWorkspaceService(
        tmp_path,
        FakeRuntimeFactory(backend),
        maximum_mode=CapabilityMode.EXPERIMENT,
    )
    try:
        ensure_ready(service, {"mode": "experiment"})
        if inline:
            response = service.call(
                "code.run_inline",
                {
                    "language": "bsl",
                    "mode": "main",
                    "source": source,
                    "inputs": {},
                    "wait_s": 2.0,
                },
            )
        else:
            assert service.call("code.list", {"container": "demo.ipynb"}).ok
            response = service.call(
                "code.run",
                {
                    "cell_id": "cell-main",
                    "revision": 1,
                    "source_sha256": digest,
                    "inputs": {},
                    "wait_s": 2.0,
                },
            )
        assert response.ok
        view = service.call(
            "operation.view",
            {"operation_id": response.value.operation_id},
        ).value

        assert backend.target_calls == 1
        assert backend.observed_before_target == [view.execution_provenance]
        assert view.execution_provenance.visible_source_sha256 == digest
        assert view.execution_provenance.executed_source_sha256 == "b" * 64
        public_journal = (
            tmp_path / ".runtime" / "agent-service" / "operations.jsonl"
        ).read_text(encoding="utf-8")
        assert source not in public_journal
        assert public_journal.count('"event":"execution_provenance"') == 1
    finally:
        service.close()


def test_missing_main_provenance_capability_fails_before_target_call(
    tmp_path: Path,
) -> None:
    """Break caught: a legacy backend can bypass the pre-dispatch journal fence."""
    source = "Результат = 42;"

    class MissingProvenanceBackend(FakeRuntimeBackend):
        execute_bsl_with_provenance = None

    backend = MissingProvenanceBackend()
    service = AgentWorkspaceService(
        tmp_path,
        FakeRuntimeFactory(backend),
        maximum_mode=CapabilityMode.EXPERIMENT,
    )
    try:
        ensure_ready(service, {"mode": "experiment"})
        response = service.call(
            "code.run_inline",
            {
                "language": "bsl",
                "mode": "main",
                "source": source,
                "inputs": {},
                "wait_s": 2.0,
            },
        )
        view = service.call(
            "operation.view",
            {"operation_id": response.value.operation_id},
        ).value

        assert view.state is AgentOperationState.FAILED
        assert view.execution_provenance is None
        assert backend.sources == []
        assert source not in (
            tmp_path / ".runtime" / "agent-service" / "operations.jsonl"
        ).read_text(encoding="utf-8")
    finally:
        service.close()


def seeded_service(tmp_path: Path) -> tuple[AgentWorkspaceService, FakeRuntimeBackend, FakeRuntimeFactory]:
    notebook = tmp_path / "demo.ipynb"
    source = "Пароль = 'super-secret';"
    cell = nbformat.v4.new_code_cell(source=source, id="cell-main")
    cell.metadata["onec_runtime"] = {
        "revision": 1,
        "language": "bsl",
        "mode": "main",
        "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
    }
    nbformat.write(nbformat.v4.new_notebook(cells=[cell]), notebook)
    backend = FakeRuntimeBackend()
    factory = FakeRuntimeFactory(backend)
    return (
        AgentWorkspaceService(tmp_path, factory, maximum_mode=CapabilityMode.EXPERIMENT),
        backend,
        factory,
    )


def ensure_ready(
    service: AgentWorkspaceService,
    arguments: dict[str, object] | None = None,
    *,
    caller_id: str = "default",
) -> object:
    requested = arguments or {}
    response = service.call("runtime.ensure", requested, caller_id=caller_id)
    assert response.ok
    if isinstance(response.value, OperationDescriptor):
        terminal = service.call(
            "operation.wait",
            {"operation_id": response.value.operation_id, "timeout_s": 2.0},
            caller_id=caller_id,
        )
        assert terminal.ok and terminal.value.state is AgentOperationState.COMPLETED
        runtime = service._runtime
        assert runtime is not None
        response = ServiceResponse.success(
            RuntimeDescriptor(
                runtime.runtime_id,
                runtime.generation,
                "ready",
                runtime.mode,
            )
        )
    assert isinstance(response.value, RuntimeDescriptor)
    return response


def test_initial_mcp_namespace_seed_validates_all_handles_before_publication(
    tmp_path: Path,
) -> None:
    service, backend, _ = seeded_service(tmp_path)
    backend.names = ("БезопасноеИмя", "АлиасМодуля")
    backend.forbidden_handles.add("e1cRuntimeКонтекст.АлиасМодуля")
    try:
        with pytest.raises(
            ProtocolError,
            match="^Worker generation objects are not public values$",
        ):
            service._install_onec_resolver(backend)

        assert backend.guard_calls == [
            "e1cRuntimeКонтекст.БезопасноеИмя",
            "e1cRuntimeКонтекст.АлиасМодуля",
        ]
        assert service._proxy_registry.current(ProxyRealm.ONEC) == ()
        assert ProxyRealm.ONEC not in service._value_resolvers
    finally:
        service.close()


def _seed_failed_expert_diagnostic(
    service: AgentWorkspaceService,
    *,
    source: str = "Результат = 1;",
    platform_text: str = (
        "{<Неизвестный модуль>(1,1)}: rdbg_pid=9182 "
        "token=private-connection"
    ),
    exact_source_identity: bool = True,
    source_unit_kind: SourceUnitKind = SourceUnitKind.NOTEBOOK_CELL,
    with_provenance: bool = True,
) -> tuple[str, NormalizedDiagnostic]:
    unit = SourceUnitRef(
        source_unit_kind,
        "cell-main",
        1,
        source_sha256(source),
    )
    visible = mapped_visible_source(source, unit)
    diagnostic = remap_platform_diagnostic(
        parse_platform_diagnostic(platform_text),
        visible,
        stage=DiagnosticStage.EXECUTION,
        visible_source_context=VisibleSourceContext({unit: source}),
    )
    if not exact_source_identity:
        diagnostic = replace(diagnostic, source_unit=None)
    started = Event()
    release = Event()

    def execute() -> BackendExecution:
        started.set()
        if not release.wait(timeout=2):
            raise RuntimeError("test did not release failed expert operation")
        return BackendExecution(
            AgentOperationState.FAILED,
            (),
            False,
            "ready",
            failure_stage="execution",
            diagnostic=diagnostic,
            state_changed=agent_contracts.StateChanged.NO,
        )

    submitted = service._operations.submit(
        {
            "operation_kind": "code_run",
            "runtime_id": "runtime-test",
            "runtime_generation": 1,
            "code_id": unit.unit_id,
            "revision": unit.revision,
            "source_sha256": unit.source_sha256,
            "inputs_sha256": "b" * 64,
        },
        execute,
    )
    assert started.wait(timeout=2)
    try:
        service._operations.record_diagnostic(
            diagnostic,
            excerpt=source[:1],
            operation_id=submitted.operation_id,
        )
        if with_provenance:
            assert diagnostic.execution_artifact_sha256 is not None
            assert diagnostic.source_map_sha256 is not None
            service._operations.set_execution_provenance(
                submitted.operation_id,
                agent_contracts.OperationExecutionProvenance(
                    visible_source_sha256=unit.source_sha256,
                    executed_source_sha256=diagnostic.execution_artifact_sha256,
                    source_map_sha256=diagnostic.source_map_sha256,
                    mode="main",
                    worker_generation=17,
                    worker_manifest_sha256="f" * 64,
                ),
            )
    finally:
        release.set()
    assert (
        service._operations.wait(submitted.operation_id, timeout_s=2).state
        is AgentOperationState.FAILED
    )
    return submitted.operation_id, diagnostic


def _reseal_private_diagnostic(record: dict[str, object]) -> None:
    content = {
        key: value
        for key, value in record.items()
        if key != "content_integrity_sha256"
    }
    record["content_integrity_sha256"] = hashlib.sha256(
        json.dumps(
            content,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def test_code_put_invalidates_only_the_active_capture_source_fence(tmp_path: Path) -> None:
    service, backend, _ = seeded_service(tmp_path)
    assert service.call("code.list", {"container": "demo.ipynb", "filters": {}}).ok
    current = service._store.get("cell-main")
    active = CaptureFence("intent", "operation", current.revision, current.source_sha256, 1, 1)
    service._capture.activate_capture(active, service._proxy_registry)

    response = service.call(
        "code.put",
        {
            "cell_id": "cell-main",
            "source": "Порог = 2;",
            "language": "bsl",
            "mode": "main",
            "expected_revision": current.revision,
            "expected_document_sha256": current.document_sha256,
            "outputs": (),
        },
    )

    assert response.ok
    assert not service._capture.active_source_matches(
        revision=active.source_revision, source_sha256=active.source_sha256
    )
    with pytest.raises(Exception):
        service._capture.inspect(
            backend, service._proxy_registry, fence=active, runtime_id=backend.runtime_id,
            runtime_generation=1, context_generation=1, filters={}, cursor=0, limit=1,
        )


def test_service_restart_quarantines_admitted_owner_without_reconciliation(
    tmp_path: Path,
) -> None:
    first, backend, _ = seeded_service(tmp_path)
    second: AgentWorkspaceService | None = None
    try:
        ensure_ready(first, {"mode": "experiment"}, caller_id="owner")
        assert (tmp_path / ".runtime" / "agent-service" / "runtime-owner.json").is_file()

        replacement_factory = FakeRuntimeFactory(FakeRuntimeBackend())
        second = AgentWorkspaceService(
            tmp_path,
            replacement_factory,
            maximum_mode=CapabilityMode.EXPERIMENT,
        )
        blocked = second.call(
            "runtime.ensure",
            {"mode": "experiment"},
            caller_id="replacement",
        )

        assert blocked.ok is False
        assert blocked.failure.category is FailureCategory.PLATFORM_FAILURE
        assert replacement_factory.starts == 0
        assert backend.closed is False
    finally:
        if second is not None:
            second.close()
        first.close()


def test_namespace_snapshot_failure_rejects_runtime_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, backend, factory = seeded_service(tmp_path)
    monkeypatch.setattr(
        backend,
        "namespace_snapshot",
        lambda: (_ for _ in ()).throw(OSError("snapshot unavailable")),
    )
    try:
        terminal = ensure_terminal(service, {"mode": "experiment"})

        assert terminal.state is AgentOperationState.FAILED
        assert backend.closed is True
        assert factory.starts == 1
        assert not (
            tmp_path / ".runtime" / "agent-service" / "runtime-owner.json"
        ).exists()
        assert service.call("workspace.variables", {"namespace": "bsl"}).value == ()
    finally:
        service.close()


def ensure_terminal(
    service: AgentWorkspaceService,
    arguments: dict[str, object] | None = None,
    *,
    caller_id: str = "default",
) -> OperationDescriptor:
    startup = service.call(
        "runtime.ensure", arguments or {}, caller_id=caller_id
    )
    assert startup.ok and isinstance(startup.value, OperationDescriptor)
    terminal = service.call(
        "operation.wait",
        {"operation_id": startup.value.operation_id, "timeout_s": 2.0},
        caller_id=caller_id,
    )
    assert terminal.ok and isinstance(terminal.value, OperationDescriptor)
    return terminal.value


def test_code_run_uses_exact_saved_revision_and_returns_operation(tmp_path: Path) -> None:
    service, backend, _ = seeded_service(tmp_path)
    assert service.call("workspace.open", {}).ok
    ensure_ready(service, {"mode": "experiment"})
    assert service.call("code.list", {"container": "demo.ipynb"}).ok
    revision = service.call("code.get", {"cell_id": "cell-main"}).value
    response = service.call(
        "code.run",
        {"cell_id": "cell-main", "revision": revision.revision,
         "source_sha256": revision.source_sha256, "inputs": {}, "wait_s": 1.0},
    )
    assert response.ok is True
    assert response.value.state is AgentOperationState.COMPLETED
    assert backend.sources == [revision.source]


def test_python_inline_publishes_only_declared_outputs_and_supports_derived_code(
    tmp_path: Path,
) -> None:
    service, _, _ = seeded_service(tmp_path)
    try:
        first = service.call(
            "code.run_inline",
            {
                "language": "python",
                "mode": "main",
                "source": "tmp = 40\nanswer = tmp + 2",
                "inputs": {},
                "outputs": ["answer"],
                "wait_s": 1.0,
            },
        )
        assert first.ok is True
        first_view = service.call(
            "operation.view", {"operation_id": first.value.operation_id}
        ).value
        answer = first_view.outputs["answer"]
        assert answer.qualified_name == "python.answer"
        assert answer.provenance.cell_id.startswith("inline-")

        variables = service.call(
            "workspace.variables", {"namespace": "python"}
        )
        assert variables.ok is True
        assert [item.qualified_name for item in variables.value] == ["python.answer"]

        derived = service.call(
            "python.run",
            {
                "code": "result = source * 2",
                "inputs": {"source": answer.proxy_id},
                "outputs": ["result"],
            },
        )
        assert derived.ok is True
        assert derived.value.outputs["result"].bounded_preview.scalar == 84
        assert {
            item.qualified_name
            for item in service.call(
                "workspace.variables", {"namespace": "all"}
            ).value
        } == {"python.answer", "python.result"}
    finally:
        service.close()


def test_completed_bsl_main_is_published_with_operation_provenance(
    tmp_path: Path,
) -> None:
    service, _, _ = seeded_service(tmp_path)
    try:
        ensure_ready(service, {"mode": "experiment"})
        assert service.call("code.list", {"container": "demo.ipynb"}).ok
        revision = service.call("code.get", {"cell_id": "cell-main"}).value
        terminal = service.call(
            "code.run",
            {
                "cell_id": revision.cell_id,
                "revision": revision.revision,
                "source_sha256": revision.source_sha256,
                "inputs": {},
                "wait_s": 1.0,
            },
        ).value

        variables = service.call(
            "workspace.variables", {"namespace": "bsl"}
        )
        assert variables.ok is True
        assert [item.qualified_name for item in variables.value] == ["bsl.Порог"]
        assert variables.value[0].provenance.operation_id == "runtime-admission-runtime-test"
    finally:
        service.close()


def test_changed_variables_contains_only_newly_proven_namespace_names(
    tmp_path: Path,
) -> None:
    service, backend, _ = seeded_service(tmp_path)
    try:
        ensure_ready(service, {"mode": "experiment"})
        backend.names_after_execute = ("Порог", "НоваяПеременная")
        operation = service.call(
            "code.run_inline",
            {
                "language": "bsl",
                "mode": "main",
                "source": "НоваяПеременная = 1;",
                "inputs": {},
                "wait_s": 1.0,
            },
        ).value

        view = service.call(
            "operation.view", {"operation_id": operation.operation_id}
        ).value
        assert [item.qualified_name for item in view.changed_variables] == [
            "bsl.НоваяПеременная"
        ]
        assert view.changed_variables[0].consistency.value == "exact"
        assert view.change_confidence.value == "exact"
        assert service.call(
            "workspace.variable", {"qualified_name": "bsl.Порог"}
        ).value.provenance.operation_id == "runtime-admission-runtime-test"
    finally:
        service.close()


def test_existing_dirty_root_is_rebound_and_stales_prior_exact_snapshot(
    tmp_path: Path,
) -> None:
    service, backend, _ = seeded_service(tmp_path)
    try:
        ensure_ready(service, {"mode": "experiment"})
        first = service.call(
            "code.run_inline",
            {
                "language": "bsl",
                "mode": "main",
                "source": "Результат = Порог;",
                "inputs": {},
                "wait_s": 1.0,
                "observe": {
                    "items": [
                        {
                            "alias": "threshold",
                            "source": {"kind": "context_binding", "name": "bsl.Порог"},
                        }
                    ]
                },
            },
        ).value
        first_proxy = service.call(
            "operation.view", {"operation_id": first.operation_id}
        ).value.outputs["threshold"]

        backend.changed_roots = ("Порог",)
        second = service.call(
            "code.run_inline",
            {
                "language": "bsl",
                "mode": "main",
                "source": "Порог = 2;",
                "inputs": {},
                "wait_s": 1.0,
            },
        ).value
        second_view = service.call(
            "operation.view", {"operation_id": second.operation_id}
        ).value

        assert [item.qualified_name for item in second_view.changed_variables] == [
            "bsl.Порог"
        ]
        assert second_view.change_confidence.value == "declared"
        stale = service.call(
            "value.inspect",
            {"proxy_id": first_proxy.proxy_id, "detail": "metadata"},
        )
        assert stale.ok is False
        assert stale.failure.category.value == "stale"
    finally:
        service.close()


def test_namespace_publication_failure_does_not_rewrite_completed_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, backend, _ = seeded_service(tmp_path)
    try:
        ensure_ready(service, {"mode": "experiment"})
        backend.names_after_execute = ("Порог", "НоваяПеременная")

        def fail_publication(**kwargs: object) -> object:
            if kwargs.get("qualified_name") == "bsl.НоваяПеременная":
                raise OSError("journal unavailable")
            raise AssertionError("unexpected binding publication")

        monkeypatch.setattr(service._proxy_registry, "register_context", fail_publication)
        operation = service.call(
            "code.run_inline",
            {
                "language": "bsl",
                "mode": "main",
                "source": "НоваяПеременная = 1;",
                "inputs": {},
                "wait_s": 1.0,
            },
        ).value

        view = service.call(
            "operation.view", {"operation_id": operation.operation_id}
        ).value
        assert view.state is AgentOperationState.COMPLETED
        assert view.failure["stage"] == "namespace_publication"
    finally:
        service.close()


def test_view_journal_failure_does_not_rewrite_completed_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, _ = seeded_service(tmp_path)
    try:
        ensure_ready(service, {"mode": "experiment"})

        def fail_view_facts(*args: object, **kwargs: object) -> object:
            del args, kwargs
            raise OSError("journal unavailable")

        monkeypatch.setattr(service._operations, "set_view_facts", fail_view_facts)
        operation = service.call(
            "code.run_inline",
            {
                "language": "bsl",
                "mode": "main",
                "source": "Результат = Порог;",
                "inputs": {},
                "wait_s": 1.0,
            },
        ).value

        assert operation.state is AgentOperationState.COMPLETED
        output = service.call(
            "operation.output",
            {"operation_id": operation.operation_id, "after_cursor": 0},
        ).value
        assert output.messages == ("operation view evidence unavailable",)
    finally:
        service.close()


def test_selected_observation_uses_bounded_onec_projection(tmp_path: Path) -> None:
    service, backend, _ = seeded_service(tmp_path)
    try:
        ensure_ready(service, {"mode": "experiment"})
        operation = service.call(
            "code.run_inline",
            {
                "language": "bsl",
                "mode": "main",
                "source": "Результат = Порог;",
                "inputs": {},
                "wait_s": 1.0,
                "observe": {
                    "items": [
                        {
                            "alias": "sample",
                            "source": {
                                "kind": "context_binding",
                                "name": "bsl.Порог",
                            },
                            "select": {"kind": "slice", "offset": 0, "limit": 5},
                        }
                    ],
                    "budget_profile": "agent_preview",
                },
            },
        ).value

        view = service.call(
            "operation.view", {"operation_id": operation.operation_id}
        ).value
        assert view.state is AgentOperationState.COMPLETED
        assert view.failure is None
        assert view.outputs["sample"].realm.value == "python"
        assert backend.project_calls[0][2]["max_items"] == 5
    finally:
        service.close()


def test_selected_observation_cannot_exceed_aggregate_preview_budget(
    tmp_path: Path,
) -> None:
    service, backend, _ = seeded_service(tmp_path)
    try:
        ensure_ready(service, {"mode": "experiment"})
        operation = service.call(
            "code.run_inline",
            {
                "language": "bsl",
                "mode": "main",
                "source": "Результат = Порог;",
                "inputs": {},
                "wait_s": 1.0,
                "observe": {
                    "items": [
                        {
                            "alias": "first",
                            "source": {"kind": "context_binding", "name": "bsl.Порог"},
                            "select": {"kind": "slice", "offset": 0, "limit": 60},
                        },
                        {
                            "alias": "second",
                            "source": {"kind": "context_binding", "name": "bsl.Порог"},
                            "select": {"kind": "slice", "offset": 60, "limit": 60},
                        },
                    ],
                    "budget_profile": "agent_preview",
                },
            },
        ).value

        view = service.call(
            "operation.view", {"operation_id": operation.operation_id}
        ).value
        assert view.state is AgentOperationState.COMPLETED
        assert set(view.outputs) == {"first"}
        assert view.failure["stage"] == "observation"
        assert len(backend.project_calls) == 1
    finally:
        service.close()


def test_direct_previews_share_one_aggregate_scan_budget(tmp_path: Path) -> None:
    service, backend, _ = seeded_service(tmp_path)
    try:
        ensure_ready(service, {"mode": "experiment"})
        operation = service.call(
            "code.run_inline",
            {
                "language": "bsl",
                "mode": "main",
                "source": "Результат = Порог;",
                "inputs": {},
                "wait_s": 1.0,
                "observe": {
                    "items": [
                        {
                            "alias": "first",
                            "source": {
                                "kind": "context_binding",
                                "name": "bsl.Порог",
                            },
                            "result": "preview",
                        },
                        {
                            "alias": "second",
                            "source": {
                                "kind": "context_binding",
                                "name": "bsl.Порог",
                            },
                            "result": "preview",
                        },
                    ],
                    "budget_profile": "agent_preview",
                },
            },
        ).value

        view = service.call(
            "operation.view", {"operation_id": operation.operation_id}
        ).value
        assert view.failure is None
        assert set(view.outputs) == {"first", "second"}
        assert [call[1]["max_items"] for call in backend.value_calls] == [50, 50]
        assert sum(call[1]["max_items"] for call in backend.value_calls) == 100
    finally:
        service.close()


def test_python_binding_delete_requires_exact_versions(tmp_path: Path) -> None:
    service, _, _ = seeded_service(tmp_path)
    try:
        proxy = service.call(
            "python.run",
            {"code": "x = 1", "inputs": {}, "outputs": ["x"]},
        ).value.outputs["x"]

        conflict = service.call(
            "workspace.delete_variables",
            {"names": ["python.x"], "expected_versions": {"python.x": 2}},
        )
        assert conflict.ok is False
        assert conflict.failure.category.value == "conflict"
        assert service.call("workspace.variable", {"qualified_name": "python.x"}).ok

        deleted = service.call(
            "workspace.delete_variables",
            {
                "names": ["python.x"],
                "expected_versions": {"python.x": proxy.version},
            },
        )
        assert deleted.value == {"deleted": ("python.x",)}
        assert service.call(
            "workspace.variables", {"namespace": "python"}
        ).value == ()
    finally:
        service.close()


def test_saved_python_cell_uses_declared_output_metadata_and_revision_provenance(
    tmp_path: Path,
) -> None:
    service, _, _ = seeded_service(tmp_path)
    try:
        assert service.call("code.list", {"container": "demo.ipynb"}).ok
        current = service.call("code.get", {"cell_id": "cell-main"}).value
        saved = service.call(
            "code.put",
            {
                "cell_id": current.cell_id,
                "source": "private_value = 40\nanswer = private_value + 2",
                "language": "python",
                "mode": "main",
                "outputs": ["answer"],
                "expected_revision": current.revision,
                "expected_document_sha256": current.document_sha256,
            },
        )
        assert saved.ok is True
        revision = service.call(
            "code.get", {"cell_id": "cell-main", "revision": saved.value.revision}
        ).value

        result = service.call(
            "code.run",
            {
                "cell_id": revision.cell_id,
                "revision": revision.revision,
                "source_sha256": revision.source_sha256,
                "inputs": {},
                "wait_s": 1.0,
            },
        )
        assert result.ok is True
        result_view = service.call(
            "operation.view", {"operation_id": result.value.operation_id}
        ).value
        assert set(result_view.outputs) == {"answer"}
        proxy = result_view.outputs["answer"]
        assert proxy.provenance.cell_id == revision.cell_id
        assert proxy.provenance.revision == revision.revision
    finally:
        service.close()


def test_onec_proxy_materializes_then_python_derives_and_survives_runtime_close(
    tmp_path: Path,
) -> None:
    service, _, _ = seeded_service(tmp_path)
    budget = {
        "depth": 8,
        "items": 100,
        "rows": 100,
        "bytes": 1024 * 1024,
        "timeout_s": 5,
    }
    try:
        ensure_ready(service, {"mode": "experiment"})
        assert service.call("code.list", {"container": "demo.ipynb"}).ok
        revision = service.call("code.get", {"cell_id": "cell-main"}).value
        assert service.call(
            "code.run",
            {
                "cell_id": revision.cell_id,
                "revision": revision.revision,
                "source_sha256": revision.source_sha256,
                "inputs": {},
                "wait_s": 1.0,
            },
        ).ok
        onec = service.call(
            "workspace.variable", {"qualified_name": "bsl.Порог"}
        ).value
        snapshot = service.call(
            "value.materialize",
            {
                "proxy_id": onec.proxy_id,
                "target": "python",
                "policy": {"refs": "presentation"},
                "budget": budget,
            },
        )
        assert snapshot.ok is True
        assert service.call(
            "workspace.variables", {"namespace": "python"}
        ).value == ()
        derived = service.call(
            "python.run",
            {
                "code": "answer = int(source) + 1",
                "inputs": {"source": snapshot.value.proxy_id},
                "outputs": ["answer"],
            },
        ).value.outputs["answer"]

        assert service.call(
            "runtime.close", {"policy": "abort_generation"}
        ).ok
        assert service.call(
            "python.inspect", {"proxy_id": derived.proxy_id}
        ).value.preview == 43
        assert service.call("value.describe", {"proxy_id": onec.proxy_id}).failure.category.value == "stale"
    finally:
        service.close()


def test_variable_catalog_keeps_namespaces_versions_and_origin_history(
    tmp_path: Path,
) -> None:
    service, _, _ = seeded_service(tmp_path)
    try:
        ensure_ready(service, {"mode": "experiment"})
        assert service.call("code.list", {"container": "demo.ipynb"}).ok
        revision = service.call("code.get", {"cell_id": "cell-main"}).value
        assert service.call(
            "code.run",
            {
                "cell_id": revision.cell_id,
                "revision": revision.revision,
                "source_sha256": revision.source_sha256,
                "inputs": {},
                "wait_s": 1.0,
            },
        ).ok
        first = service.call(
            "python.run", {"code": "Порог = 1", "inputs": {}, "outputs": ["Порог"]}
        ).value.outputs["Порог"]
        second = service.call(
            "python.run", {"code": "Порог = 2", "inputs": {}, "outputs": ["Порог"]}
        ).value.outputs["Порог"]

        variables = service.call(
            "workspace.variables", {"namespace": "all"}
        ).value
        assert {item.qualified_name for item in variables} == {
            "bsl.Порог",
            "python.Порог",
        }
        history = service.call(
            "workspace.variable_history", {"qualified_name": "python.Порог"}
        ).value
        assert [item.version for item in history] == [first.version, second.version]
        assert history[0].provenance.operation_id != history[1].provenance.operation_id
    finally:
        service.close()


def test_value_release_unpins_proxy_without_deleting_python_binding(
    tmp_path: Path,
) -> None:
    service, _, _ = seeded_service(tmp_path)
    try:
        original = service.call(
            "python.run", {"code": "x = 42", "inputs": {}, "outputs": ["x"]}
        ).value.outputs["x"]

        first = service.call("value.release", {"proxy_id": original.proxy_id})
        second = service.call("value.release", {"proxy_id": original.proxy_id})
        replacement = service.call(
            "workspace.variable", {"qualified_name": "python.x"}
        ).value

        assert first.value == {"released": True, "binding_deleted": False}
        assert second.value == {"released": False, "binding_deleted": False}
        assert replacement.version == original.version + 1
        assert service.call(
            "python.inspect", {"proxy_id": replacement.proxy_id}
        ).value.preview == 42
    finally:
        service.close()


def test_runtime_is_singleton_and_selection_is_per_caller(tmp_path: Path) -> None:
    service, _, factory = seeded_service(tmp_path)
    first = ensure_ready(
        service, {"mode": "experiment"}, caller_id="first"
    )
    second = ensure_ready(service, {"mode": "observe"}, caller_id="second")
    conflict = service.call("runtime.start", {"mode": "experiment"}, caller_id="second")
    assert first.ok and second.ok
    assert first.value.runtime_id == second.value.runtime_id
    assert factory.starts == 1
    assert not conflict.ok
    assert conflict.failure.category.value == "conflict"


def test_failed_ensure_does_not_select_caller_for_execution_or_close(
    tmp_path: Path,
) -> None:
    class FailOnceStatusBackend(FakeRuntimeBackend):
        def __init__(self) -> None:
            super().__init__()
            self.status_calls = 0

        def status(self) -> RuntimeDescriptor:
            self.status_calls += 1
            if self.status_calls == 2:
                raise RuntimeError("transient status failure")
            return super().status()

    backend = FailOnceStatusBackend()
    service, _, _ = seeded_service(tmp_path)
    service = AgentWorkspaceService(
        tmp_path,
        FakeRuntimeFactory(backend),
        maximum_mode=CapabilityMode.EXPERIMENT,
    )
    startup = service.call("runtime.ensure", {}, caller_id="owner")
    assert startup.ok and isinstance(startup.value, OperationDescriptor)
    terminal = service.call(
        "operation.wait",
        {"operation_id": startup.value.operation_id, "timeout_s": 2.0},
        caller_id="owner",
    )
    assert terminal.ok and terminal.value.state is AgentOperationState.COMPLETED

    failed = service.call("runtime.ensure", {}, caller_id="observer")
    assert failed.ok is False
    assert failed.failure.category.value == "platform_failure"
    assert "observer" not in service._selected
    assert service.call("code.list", {"container": "demo.ipynb"}).ok
    revision = service.call("code.get", {"cell_id": "cell-main"}).value
    denied_run = service.call(
        "code.run",
        {
            "cell_id": "cell-main",
            "revision": revision.revision,
            "source_sha256": revision.source_sha256,
            "inputs": {},
        },
        caller_id="observer",
    )
    denied_close = service.call(
        "runtime.close",
        {"policy": "abort_generation"},
        caller_id="observer",
    )

    assert denied_run.ok is False
    assert denied_close.ok is False
    assert backend.sources == []
    assert backend.closed is False
    assert service.call("runtime.status", {}, caller_id="owner").ok


def test_capabilities_and_raw_result_fail_closed(tmp_path: Path) -> None:
    service, _, _ = seeded_service(tmp_path)
    capabilities = service.call("workspace.capabilities", {}).value
    assert capabilities["capture"].available is True
    assert capabilities["value_proxies"].available is True
    assert capabilities["value_export"].available is False
    assert capabilities["python_workspace"].available is True
    assert capabilities["isolation"].available is False
    assert service.call("workspace.status", {}).ok
    assert "super-secret" not in str(service.call("workspace.status", {}).value)


def test_capture_recovery_actions_are_supported_service_actions(tmp_path: Path) -> None:
    service, _, _ = seeded_service(tmp_path)
    ensure_ready(service)
    assert service.call("code.list", {"container": "demo.ipynb"}).ok
    revision = service.call("code.get", {"cell_id": "cell-main"}).value
    submitted = service.call(
        "code.run",
        {
            "cell_id": revision.cell_id,
            "revision": revision.revision,
            "source_sha256": revision.source_sha256,
            "inputs": {},
        },
    )
    assert submitted.ok
    operation_id = submitted.value.operation_id

    # These are the only executable recovery actions advertised by capture.
    assert service.call("runtime.status", {}).ok
    assert service.call("operation.explain_failure", {"operation_id": operation_id}).ok
    assert service.call("operation.abort_generation", {"operation_id": operation_id}).ok


def test_agent_value_release_is_idempotent_without_deleting_bsl_binding(
    tmp_path: Path,
) -> None:
    service, backend, _ = seeded_service(tmp_path)
    ensure_ready(service, {"mode": "experiment"})
    proxy = service._proxy_registry.register_context(
        qualified_name="bsl.Порог",
        type_name="Число",
        runtime_id=backend.runtime_id,
        runtime_generation=1,
        context_generation=1,
        provenance=ProxyProvenance("cell-main", 1, "a" * 64, "op-1"),
        resolver_handle="e1cRuntimeКонтекст.Порог",
    )

    assert service.call("value.describe", {"proxy_id": proxy.proxy_id}).ok
    first = service.call("value.release", {"proxy_id": proxy.proxy_id})
    second = service.call("value.release", {"proxy_id": proxy.proxy_id})

    assert first.value == {"released": True, "binding_deleted": False}
    assert second.value == {"released": False, "binding_deleted": False}
    replacement = service.call(
        "workspace.variable", {"qualified_name": "bsl.Порог"}
    ).value
    assert replacement.version == proxy.version + 1
    assert backend.closed is False


def test_only_code_get_exposes_saved_source(tmp_path: Path) -> None:
    service, _, _ = seeded_service(tmp_path)
    assert service.call("code.list", {"container": "demo.ipynb"}).ok
    history = service.call("code.history", {"cell_id": "cell-main"})
    assert "super-secret" not in str(to_wire(history))
    assert "super-secret" not in str(to_wire(service.call("code.diff", {"cell_id": "cell-main", "left": 1, "right": 1})))
    assert "super-secret" in service.call("code.get", {"cell_id": "cell-main"}).value.source


def test_unsupported_execution_and_raw_result_use_unsupported_failure(tmp_path: Path) -> None:
    service, _, _ = seeded_service(tmp_path)
    ensure_ready(service, {"mode": "experiment"})
    assert service.call("code.list", {"container": "demo.ipynb"}).ok
    revision = service.call("code.get", {"cell_id": "cell-main"}).value
    unsupported = service.call("code.run", {"cell_id": "cell-main", "revision": 1, "source_sha256": revision.source_sha256, "inputs": {"x": "not-a-proxy"}})
    complete = service.call("code.run", {"cell_id": "cell-main", "revision": 1, "source_sha256": revision.source_sha256, "inputs": {}, "wait_s": 1.0})
    result = service.call("operation.result", {"operation_id": complete.value.operation_id})
    assert unsupported.failure.category.value == "unsupported"
    assert result.failure.category.value == "unsupported"


def test_abort_generation_closes_the_owning_runtime(tmp_path: Path) -> None:
    service, backend, _ = seeded_service(tmp_path)
    ensure_ready(service, {"mode": "experiment"})
    assert service.call("code.list", {"container": "demo.ipynb"}).ok
    revision = service.call("code.get", {"cell_id": "cell-main"}).value
    operation = service.call("code.run", {"cell_id": "cell-main", "revision": 1, "source_sha256": revision.source_sha256, "inputs": {}}).value
    aborted = service.call("operation.abort_generation", {"operation_id": operation.operation_id})
    assert aborted.ok
    assert backend.closed is True


def test_observe_ceiling_denies_default_experiment_without_starting_or_executing(tmp_path: Path) -> None:
    backend = FakeRuntimeBackend()
    factory = FakeRuntimeFactory(backend)
    service = AgentWorkspaceService(tmp_path, factory, maximum_mode=CapabilityMode.OBSERVE)
    denied = service.call("runtime.ensure", {})
    assert denied.failure.category.value == "denied"
    assert factory.starts == 0
    assert backend.sources == []


def test_observe_runtime_cannot_execute_bsl(tmp_path: Path) -> None:
    service, backend, _ = seeded_service(tmp_path)
    service._maximum_mode = CapabilityMode.OBSERVE
    ensure_ready(service, {"mode": "observe"})
    assert service.call("code.list", {"container": "demo.ipynb"}).ok
    revision = service.call("code.get", {"cell_id": "cell-main"}).value
    denied = service.call("code.run", {"cell_id": "cell-main", "revision": 1, "source_sha256": revision.source_sha256, "inputs": {}})
    assert denied.failure.category.value == "denied"
    assert backend.sources == []


def test_abort_policy_closes_when_status_fails_after_runtime_admission(tmp_path: Path) -> None:
    class FailingStatusBackend(FakeRuntimeBackend):
        def __init__(self) -> None:
            super().__init__()
            self.status_calls = 0

        def status(self) -> RuntimeDescriptor:
            self.status_calls += 1
            if self.status_calls > 1:
                raise RuntimeError("secret status failure")
            return super().status()

    backend = FailingStatusBackend()
    service = AgentWorkspaceService(tmp_path, FakeRuntimeFactory(backend), maximum_mode=CapabilityMode.EXPERIMENT)
    ensure_ready(service, {"mode": "experiment"})
    response = service.call("runtime.close", {"policy": "abort_generation"})
    assert backend.closed is True
    assert backend.close_calls == 1
    assert response.ok is True


def test_lifecycle_policies_are_explicit_and_workspace_detach_does_not_close(tmp_path: Path) -> None:
    service, backend, _ = seeded_service(tmp_path)
    ensure_ready(service, {"mode": "experiment"})
    assert service.call("runtime.close", {}).failure.category.value == "invalid_request"
    assert service.call("runtime.close", {"policy": "graceful"}).failure.category.value == "invalid_request"
    assert service.call("workspace.close", {"policy": "detach"}).ok
    assert backend.closed is False
    assert service.call("workspace.close", {"policy": "abort_generation"}).failure.category.value == "invalid_request"
    assert service.call("runtime.select", {"runtime_id": backend.runtime_id}).ok
    assert service.call("workspace.close", {"policy": "abort_generation"}).ok
    assert backend.closed is True


def test_workspace_abort_close_without_runtime_remains_idempotent(tmp_path: Path) -> None:
    service, backend, _ = seeded_service(tmp_path)
    response = service.call("workspace.close", {"policy": "abort_generation"}, caller_id="observer")
    assert response.ok
    assert backend.close_calls == 0


def test_inline_revision_survives_restart_and_promotes_exact_source(tmp_path: Path) -> None:
    service, _, _ = seeded_service(tmp_path)
    ensure_ready(service, {"mode": "experiment"})
    source = "Сообщить(\"inline secret\");"
    operation = service.call("code.run_inline", {"language": "bsl", "mode": "main", "source": source, "inputs": {}}).value
    restarted = AgentWorkspaceService(tmp_path, FakeRuntimeFactory(FakeRuntimeBackend()), maximum_mode=CapabilityMode.EXPERIMENT)
    recovered = restarted.call("code.get", {"cell_id": operation.cell_id, "revision": 1})
    assert recovered.ok and recovered.value.source == source
    assert restarted.call("code.list", {"container": "demo.ipynb"}).ok
    destination = restarted.call("code.get", {"cell_id": "cell-main"}).value
    promoted = restarted.call("code.promote", {"operation_id": operation.operation_id, "cell_id": "cell-main", "expected_revision": destination.revision, "expected_document_sha256": destination.document_sha256})
    assert promoted.ok
    assert restarted.call("code.get", {"cell_id": "cell-main"}).value.source == source


def test_code_diff_returns_authorized_bounded_diff(tmp_path: Path) -> None:
    service, _, _ = seeded_service(tmp_path)
    assert service.call("code.list", {"container": "demo.ipynb"}).ok
    before = service.call("code.get", {"cell_id": "cell-main"}).value
    saved = service.call("code.put", {"cell_id": "cell-main", "source": "Ответ = 2;", "language": "bsl", "mode": "main", "expected_revision": before.revision, "expected_document_sha256": before.document_sha256})
    assert saved.ok
    diff = service.call("code.diff", {"cell_id": "cell-main", "left": 1, "right": 2})
    assert diff.ok and "-Пароль = 'super-secret';" in diff.value["diff"] and "+Ответ = 2;" in diff.value["diff"]


def test_inline_persistence_failure_happens_before_backend_execution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, backend, _ = seeded_service(tmp_path)
    ensure_ready(service, {"mode": "experiment"})
    monkeypatch.setattr(service, "_persist_inline", lambda _revision: (_ for _ in ()).throw(OSError("disk full")))
    response = service.call("code.run_inline", {"language": "bsl", "mode": "main", "source": "Ответ = 1;", "inputs": {}})
    assert response.ok is False
    assert backend.sources == []


def test_history_and_explain_failure_include_operation_provenance_without_source(tmp_path: Path) -> None:
    service, _, _ = seeded_service(tmp_path)
    ensure_ready(service, {"mode": "experiment"})
    assert service.call("code.list", {"container": "demo.ipynb"}).ok
    revision = service.call("code.get", {"cell_id": "cell-main"}).value
    operation = service.call("code.run", {"cell_id": "cell-main", "revision": 1, "source_sha256": revision.source_sha256, "inputs": {}, "wait_s": 1.0}).value
    history = service.call("code.history", {"cell_id": "cell-main"})
    explained = service.call("operation.explain_failure", {"operation_id": operation.operation_id})
    assert [item.operation_id for item in history.value["operations"]] == [operation.operation_id]
    assert explained.value["source_sha256"] == revision.source_sha256
    assert "super-secret" not in str(explained.value)


def test_explain_failure_resolves_only_the_exact_bounded_private_diagnostic(
    tmp_path: Path,
) -> None:
    """Break caught: the expert route never resolves the private diagnostic record."""
    service, _, _ = seeded_service(tmp_path)
    try:
        operation_id, diagnostic = _seed_failed_expert_diagnostic(service)

        explained = service.call(
            "operation.explain_failure",
            {"operation_id": operation_id},
        )

        assert explained.ok
        assert explained.value["diagnostic"]["diagnostic_id"] == diagnostic.diagnostic_id
        details = explained.value["diagnostic_details"]
        assert details["diagnostic_id"] == diagnostic.diagnostic_id
        assert details["excerpt"] == "Р"
        assert details["lowered_location"]["line"] == 1
        assert details["execution_artifact_sha256"] == diagnostic.execution_artifact_sha256
        assert details["source_map_sha256"] == diagnostic.source_map_sha256
        assert details["worker_generation"] == 17
        assert details["worker_manifest_sha256"] == "f" * 64
        assert details["platform_diagnostic_redacted"] is True
        assert "code" not in details
        assert "source_unit" not in details
        encoded = json.dumps(explained.value, ensure_ascii=False, default=str)
        assert "9182" not in encoded
        assert "private-connection" not in encoded
        assert "content_integrity_sha256" not in encoded
        journal = [
            json.loads(line)
            for line in (
                tmp_path / ".runtime" / "agent-service" / "operations.jsonl"
            )
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        anchors = [
            event for event in journal if event["event"] == "private_diagnostic_ref"
        ]
        assert len(anchors) == 1
        assert set(anchors[0]["reference"]) == {
            "diagnostic_id",
            "content_integrity_sha256",
        }
        assert anchors[0]["reference"]["diagnostic_id"] == diagnostic.diagnostic_id
        assert len(anchors[0]["reference"]["content_integrity_sha256"]) == 64
        assert "platform_diagnostic" not in anchors[0]
        assert "excerpt" not in anchors[0]
        assert source_sha256("Результат = 1;") in encoded
        assert "Результат = 1;" not in encoded
    finally:
        service.close()


@pytest.mark.parametrize(
    ("exact_source_identity", "source_unit_kind", "with_provenance"),
    (
        (False, SourceUnitKind.NOTEBOOK_CELL, True),
        (True, SourceUnitKind.MODULE, True),
        (True, SourceUnitKind.NOTEBOOK_CELL, False),
    ),
)
def test_explain_failure_requires_exact_source_identity_and_provenance_binding(
    tmp_path: Path,
    exact_source_identity: bool,
    source_unit_kind: SourceUnitKind,
    with_provenance: bool,
) -> None:
    """Break caught: absent source/provenance silently skips expert binding."""
    service, _, _ = seeded_service(tmp_path)
    try:
        operation_id, diagnostic = _seed_failed_expert_diagnostic(
            service,
            exact_source_identity=exact_source_identity,
            source_unit_kind=source_unit_kind,
            with_provenance=with_provenance,
        )

        explained = service.call(
            "operation.explain_failure",
            {"operation_id": operation_id},
        )

        assert explained.ok
        assert explained.value["diagnostic"]["diagnostic_id"] == diagnostic.diagnostic_id
        assert explained.value["diagnostic_details"] is None
    finally:
        service.close()


@pytest.mark.parametrize(
    "private_mutation",
    (
        "missing",
        "corrupt",
        "conflicted",
        "mismatched",
        "hash_mismatched",
        "truncated_replaced",
        "excerpt_replaced",
        "integrity_missing",
    ),
)
def test_explain_failure_missing_or_untrusted_private_record_fails_closed(
    tmp_path: Path,
    private_mutation: str,
) -> None:
    """Break caught: expert lookup falls back to raw or mismatched evidence."""
    service, _, _ = seeded_service(tmp_path)
    platform_text = (
        "{<Неизвестный модуль>(1,1)}: " + "x" * 5000
        if private_mutation == "truncated_replaced"
        else "{<Неизвестный модуль>(1,1)}: rdbg_pid=9182 "
        "token=private-connection"
    )
    operation_id, diagnostic = _seed_failed_expert_diagnostic(
        service,
        platform_text=platform_text,
    )
    service.close()
    private_path = (
        tmp_path / ".runtime" / "agent-service" / "diagnostics.private.jsonl"
    )
    if private_mutation == "missing":
        private_path.unlink()
    elif private_mutation == "corrupt":
        private_path.write_bytes(b"\xff\xfeinvalid-private-record")
    else:
        original = private_path.read_text(encoding="utf-8").strip()
        changed = json.loads(original)
        if private_mutation == "conflicted":
            changed["excerpt"] = "ПоддельныйФрагмент"
            _reseal_private_diagnostic(changed)
            private_path.write_text(
                original
                + "\n"
                + json.dumps(changed, ensure_ascii=False, separators=(",", ":"))
                + "\n",
                encoding="utf-8",
            )
        elif private_mutation == "mismatched":
            changed["source_unit_id"] = "different-cell"
            _reseal_private_diagnostic(changed)
            private_path.write_text(
                json.dumps(changed, ensure_ascii=False, separators=(",", ":"))
                + "\n",
                encoding="utf-8",
            )
        elif private_mutation == "hash_mismatched":
            changed["platform_diagnostic"] = "different bounded platform text"
            changed["platform_diagnostic_sha256"] = hashlib.sha256(
                changed["platform_diagnostic"].encode("utf-8")
            ).hexdigest()
            _reseal_private_diagnostic(changed)
            private_path.write_text(
                json.dumps(changed, ensure_ascii=False, separators=(",", ":"))
                + "\n",
                encoding="utf-8",
            )
        elif private_mutation == "truncated_replaced":
            changed["platform_diagnostic"] = "z" * len(
                changed["platform_diagnostic"]
            )
            changed["platform_diagnostic_sha256"] = "c" * 64
            _reseal_private_diagnostic(changed)
            private_path.write_text(
                json.dumps(changed, ensure_ascii=False, separators=(",", ":"))
                + "\n",
                encoding="utf-8",
            )
        elif private_mutation == "excerpt_replaced":
            changed["excerpt"] = "X"
            _reseal_private_diagnostic(changed)
            private_path.write_text(
                json.dumps(changed, ensure_ascii=False, separators=(",", ":"))
                + "\n",
                encoding="utf-8",
            )
        else:
            changed.pop("content_integrity_sha256", None)
            private_path.write_text(
                json.dumps(changed, ensure_ascii=False, separators=(",", ":"))
                + "\n",
                encoding="utf-8",
            )

    recovered = AgentWorkspaceService(
        tmp_path,
        FakeRuntimeFactory(FakeRuntimeBackend()),
        maximum_mode=CapabilityMode.EXPERIMENT,
    )
    try:
        explained = recovered.call(
            "operation.explain_failure",
            {"operation_id": operation_id},
        )

        assert explained.ok
        assert explained.value["diagnostic"]["diagnostic_id"] == diagnostic.diagnostic_id
        assert explained.value["diagnostic_details"] is None
        encoded = json.dumps(explained.value, ensure_ascii=False, default=str)
        assert "9182" not in encoded
        assert "private-connection" not in encoded
        assert "Результат = 1;" not in encoded
    finally:
        recovered.close()


def test_restart_requires_abort_policy(tmp_path: Path) -> None:
    service, _, factory = seeded_service(tmp_path)
    ensure_ready(service, {"mode": "experiment"})
    assert service.call("runtime.restart", {"policy": "graceful"}).failure.category.value == "invalid_request"
    restarted = service.call("runtime.restart", {"policy": "abort_generation", "mode": "experiment"})
    assert restarted.ok
    assert factory.starts == 2


def test_explicit_restart_is_executable_for_a_quarantined_closing_generation(
    tmp_path: Path,
) -> None:
    service, backend, factory = seeded_service(tmp_path)
    ensure_ready(service)
    assert service._runtime is not None
    service._runtime.closing = True

    restarted = service.call(
        "runtime.restart",
        {"policy": "abort_generation", "mode": "experiment"},
    )

    assert restarted.ok, restarted.failure
    assert backend.close_calls == 1
    assert service._runtime is not None
    assert service._runtime.closing is False
    assert factory.starts == 2


def test_code_list_honors_language_and_mode_filters(tmp_path: Path) -> None:
    service, _, _ = seeded_service(tmp_path)
    listed = service.call("code.list", {"container": "demo.ipynb", "filters": {"language": "python"}})
    assert listed.ok and listed.value == ()
    main = service.call("code.list", {"container": "demo.ipynb", "filters": {"language": "bsl", "mode": "main"}})
    assert [item.cell_id for item in main.value] == ["cell-main"]


def test_backend_mode_mismatch_is_closed_and_never_admitted(tmp_path: Path) -> None:
    class MaliciousBackend(FakeRuntimeBackend):
        def status(self) -> RuntimeDescriptor:
            return RuntimeDescriptor(self.runtime_id, 1, "ready", CapabilityMode.ADMIN)

    backend = MaliciousBackend()
    factory = FakeRuntimeFactory(backend)
    service = AgentWorkspaceService(tmp_path, factory, maximum_mode=CapabilityMode.EXPERIMENT)
    denied = ensure_terminal(service)
    assert denied.state is AgentOperationState.FAILED
    assert backend.close_calls == 1
    assert service.call("runtime.start", {"mode": "experiment"}).failure.category.value != "conflict"
    assert backend.sources == []


def test_close_failure_retains_ownership_and_blocks_replacement_until_retry(tmp_path: Path) -> None:
    class FlakyCloseBackend(FakeRuntimeBackend):
        def __init__(self) -> None:
            super().__init__()
            self.fail_close = True

        def close(self) -> None:
            self.close_calls += 1
            if self.fail_close:
                raise RuntimeError("secret close failure")
            self.closed = True

    backend = FlakyCloseBackend()
    factory = FakeRuntimeFactory(backend)
    service = AgentWorkspaceService(tmp_path, factory, maximum_mode=CapabilityMode.EXPERIMENT)
    admitted = ensure_ready(service)
    failed = service.call("runtime.close", {"policy": "abort_generation"})
    assert failed.ok is False and failed.failure.state_changed.value == "unknown"
    assert factory.starts == 1
    assert service.call("runtime.start", {"mode": "experiment"}).ok is False
    assert service.call("runtime.restart", {"policy": "abort_generation"}).ok is False
    backend.fail_close = False
    assert service.call("runtime.close", {"policy": "abort_generation"}).ok
    ensure_ready(service)
    assert factory.starts == 2


def test_mismatched_backend_with_unconfirmed_cleanup_blocks_second_start(tmp_path: Path) -> None:
    class MaliciousUnclosable(FakeRuntimeBackend):
        def status(self) -> RuntimeDescriptor:
            return RuntimeDescriptor(self.runtime_id, 1, "ready", CapabilityMode.ADMIN)

        def close(self) -> None:
            self.close_calls += 1
            raise RuntimeError("secret close failure")

    backend = MaliciousUnclosable()
    factory = FakeRuntimeFactory(backend)
    service = AgentWorkspaceService(tmp_path, factory, maximum_mode=CapabilityMode.EXPERIMENT)
    first = ensure_terminal(service)
    second = service.call("runtime.start", {"mode": "experiment"})
    assert first.state is AgentOperationState.UNKNOWN
    assert second.failure.category.value == "conflict"
    assert factory.starts == 1


def test_quarantined_mismatched_backend_cannot_be_selected_or_execute(tmp_path: Path) -> None:
    class MaliciousUnclosable(FakeRuntimeBackend):
        def status(self) -> RuntimeDescriptor:
            return RuntimeDescriptor(self.runtime_id, 1, "ready", CapabilityMode.ADMIN)

        def close(self) -> None:
            self.close_calls += 1
            raise RuntimeError("secret close failure")

    notebook = tmp_path / "demo.ipynb"
    source = "Ответ = 1;"
    cell = nbformat.v4.new_code_cell(source=source, id="cell-main")
    cell.metadata["onec_runtime"] = {"revision": 1, "language": "bsl", "mode": "main", "source_sha256": hashlib.sha256(source.encode()).hexdigest()}
    nbformat.write(nbformat.v4.new_notebook(cells=[cell]), notebook)
    backend = MaliciousUnclosable()
    service = AgentWorkspaceService(tmp_path, FakeRuntimeFactory(backend), maximum_mode=CapabilityMode.EXPERIMENT)
    assert ensure_terminal(service).state is AgentOperationState.UNKNOWN
    assert service.call("runtime.select", {"runtime_id": "runtime-test"}).ok is False
    assert service.call("code.list", {"container": "demo.ipynb"}).ok
    revision = service.call("code.get", {"cell_id": "cell-main"}).value
    denied = service.call("code.run", {"cell_id": "cell-main", "revision": 1, "source_sha256": revision.source_sha256, "inputs": {}})
    assert denied.ok is False
    assert backend.sources == []


def test_runtime_catalog_covers_default_selection_status_list_and_mode_request(tmp_path: Path) -> None:
    service, _, _ = seeded_service(tmp_path)
    started = ensure_ready(service, caller_id="owner")
    assert started.ok and started.value.mode is CapabilityMode.EXPERIMENT
    assert [item.runtime_id for item in service.call("runtime.list", {}).value] == ["runtime-test"]
    assert service.call("runtime.status", {}, caller_id="owner").value.runtime_id == "runtime-test"
    selected = service.call("runtime.select", {"runtime_id": "runtime-test"}, caller_id="other")
    assert selected.ok
    assert service.call("runtime.request_mode", {"mode": "observe"}).ok
    assert service.call("runtime.request_mode", {"mode": "admin"}).failure.category.value == "denied"


def test_code_delete_and_operation_catalog_handlers(tmp_path: Path) -> None:
    service, _, _ = seeded_service(tmp_path)
    ensure_ready(service)
    assert service.call("code.list", {"container": "demo.ipynb"}).ok
    revision = service.call("code.get", {"cell_id": "cell-main"}).value
    operation = service.call("code.run", {"cell_id": "cell-main", "revision": 1, "source_sha256": revision.source_sha256, "inputs": {}, "wait_s": 1.0}).value
    operation_ids = [
        item.operation_id for item in service.call("operation.list", {}).value
    ]
    assert operation_ids[-1] == operation.operation_id
    assert len(operation_ids) == 2
    assert service.call("operation.status", {"operation_id": operation.operation_id}).value.state is AgentOperationState.COMPLETED
    assert service.call("operation.wait", {"operation_id": operation.operation_id, "timeout_s": 0.01}).value.operation_id == operation.operation_id
    assert service.call("operation.output", {"operation_id": operation.operation_id, "after_cursor": 0, "limits": {"messages": 1}}).value.messages == ()
    assert service.call("operation.stop_waiting", {"operation_id": operation.operation_id}).ok
    assert service.call("operation.result", {"operation_id": operation.operation_id}).failure.category.value == "unsupported"
    deleted = service.call("code.delete", {"cell_id": "cell-main", "expected_revision": revision.revision, "expected_document_sha256": revision.document_sha256})
    assert deleted.ok
    assert service.call("code.get", {"cell_id": "cell-main"}).ok is False


@pytest.mark.parametrize(
    ("method", "arguments"),
    [
        ("workspace.status", {"unexpected": True}),
        ("runtime.ensure", {"unexpected": True}),
        ("code.list", {"container": "demo.ipynb", "unexpected": True}),
        ("operation.list", {"unexpected": True}),
    ],
)
def test_handler_families_reject_unexpected_arguments(tmp_path: Path, method: str, arguments: dict[str, object]) -> None:
    service, _, _ = seeded_service(tmp_path)
    response = service.call(method, arguments)
    assert response.ok is False
    assert response.failure.category.value == "invalid_request"


def test_execution_rejects_unsupported_languages_modes_inputs_and_revision_fences(tmp_path: Path) -> None:
    service, backend, _ = seeded_service(tmp_path)
    ensure_ready(service)
    assert service.call("code.list", {"container": "demo.ipynb"}).ok
    revision = service.call("code.get", {"cell_id": "cell-main"}).value
    for language, mode in (("bsl", "capture"), ("bsl", "worker")):
        response = service.call("code.run_inline", {"language": language, "mode": mode, "source": "x", "inputs": {}})
        assert response.failure.category.value == "unsupported"
    missing_python_outputs = service.call(
        "code.run_inline",
        {"language": "python", "mode": "main", "source": "x = 1", "inputs": {}},
    )
    assert missing_python_outputs.failure.category.value == "invalid_request"
    assert service.call("code.run", {"cell_id": "cell-main", "revision": 1, "source_sha256": revision.source_sha256, "inputs": {"x": "value"}}).failure.category.value == "unsupported"
    assert service.call("code.run", {"cell_id": "cell-main", "revision": 2, "source_sha256": revision.source_sha256, "inputs": {}}).ok is False
    assert service.call("code.run", {"cell_id": "cell-main", "revision": 1, "source_sha256": "stale", "inputs": {}}).failure.category.value == "conflict"
    assert backend.sources == []


def test_no_selection_unknown_method_and_unknown_operation_fail_closed(tmp_path: Path) -> None:
    service, _, _ = seeded_service(tmp_path)
    assert service.call("unknown.method", {}).failure.category.value == "invalid_request"
    ensure_ready(service, caller_id="owner")
    assert service.call("code.list", {"container": "demo.ipynb"}).ok
    revision = service.call("code.get", {"cell_id": "cell-main"}).value
    assert service.call("code.run", {"cell_id": "cell-main", "revision": 1, "source_sha256": revision.source_sha256, "inputs": {}}, caller_id="other").ok is False
    for method, arguments in (("operation.status", {"operation_id": "missing"}), ("operation.result", {"operation_id": "missing"}), ("operation.explain_failure", {"operation_id": "missing"})):
        assert service.call(method, arguments).ok is False


def test_rejected_candidate_is_not_published_while_successful_cleanup_is_pending(tmp_path: Path) -> None:
    close_entered = Event()
    release_close = Event()

    class BlockingRejectedBackend(FakeRuntimeBackend):
        def status(self) -> RuntimeDescriptor:
            return RuntimeDescriptor(self.runtime_id, 1, "ready", CapabilityMode.ADMIN)

        def close(self) -> None:
            self.close_calls += 1
            close_entered.set()
            if not release_close.wait(2):
                raise RuntimeError("test cleanup timed out")
            self.closed = True

    notebook = tmp_path / "demo.ipynb"
    source = "Ответ = 1;"
    cell = nbformat.v4.new_code_cell(source=source, id="cell-main")
    cell.metadata["onec_runtime"] = {"revision": 1, "language": "bsl", "mode": "main", "source_sha256": hashlib.sha256(source.encode()).hexdigest()}
    nbformat.write(nbformat.v4.new_notebook(cells=[cell]), notebook)
    backend = BlockingRejectedBackend()
    service = AgentWorkspaceService(tmp_path, FakeRuntimeFactory(backend), maximum_mode=CapabilityMode.EXPERIMENT)
    assert service.call("code.list", {"container": "demo.ipynb"}).ok
    revision = service.call("code.get", {"cell_id": "cell-main"}).value
    result: list[object] = []
    starter = Thread(target=lambda: result.append(service.call("runtime.ensure", {})))
    starter.start()
    assert close_entered.wait(1)
    assert service._runtime is None

    observations: list[object] = []
    observation_done = Event()

    def observe_candidate() -> None:
        observations.append(service.call("runtime.list", {}))
        observations.append(service.call("runtime.select", {"runtime_id": backend.runtime_id}, caller_id="observer"))
        observations.append(
            service.call(
                "code.run",
                {"cell_id": "cell-main", "revision": 1, "source_sha256": revision.source_sha256, "inputs": {}},
                caller_id="observer",
            )
        )
        observation_done.set()

    observer = Thread(target=observe_candidate)
    observer.start()
    observation_done.wait(0.1)
    release_close.set()
    starter.join(2)
    observer.join(2)

    assert not starter.is_alive()
    assert not observer.is_alive()
    listed, selected, executed = observations
    assert listed.ok and listed.value == ()
    assert selected.ok is False
    assert executed.ok is False
    assert backend.sources == []
    assert len(result) == 1 and result[0].ok
    assert isinstance(result[0].value, OperationDescriptor)
    terminal = service.call(
        "operation.wait",
        {"operation_id": result[0].value.operation_id, "timeout_s": 2.0},
    )
    assert terminal.ok and terminal.value.state is AgentOperationState.FAILED


def test_quarantined_runtime_status_is_available_by_explicit_id_without_selection(tmp_path: Path) -> None:
    class UnclosableRejectedBackend(FakeRuntimeBackend):
        def status(self) -> RuntimeDescriptor:
            return RuntimeDescriptor(self.runtime_id, 1, "ready", CapabilityMode.ADMIN)

        def close(self) -> None:
            self.close_calls += 1
            raise RuntimeError("secret close failure")

    backend = UnclosableRejectedBackend()
    service = AgentWorkspaceService(tmp_path, FakeRuntimeFactory(backend), maximum_mode=CapabilityMode.EXPERIMENT)
    assert ensure_terminal(service).state is AgentOperationState.UNKNOWN
    assert service._runtime is not None and service._runtime.closing is True

    status = service.call("runtime.status", {"runtime_id": backend.runtime_id}, caller_id="observer")
    listed = service.call("runtime.list", {}, caller_id="observer")
    assert status.ok and status.value.runtime_id == backend.runtime_id
    assert status.value.mode is CapabilityMode.ADMIN
    assert listed.ok and [item.runtime_id for item in listed.value] == [backend.runtime_id]
    assert service.call("runtime.status", {}, caller_id="observer").ok is False
    assert service.call("runtime.select", {"runtime_id": backend.runtime_id}, caller_id="observer").ok is False


@pytest.mark.parametrize("entrypoint", ["runtime.close", "workspace.close", "operation.abort_generation"])
def test_unselected_caller_cannot_close_healthy_runtime(tmp_path: Path, entrypoint: str) -> None:
    service, backend, _ = seeded_service(tmp_path)
    ensure_ready(service, caller_id="owner")
    arguments: dict[str, object] = {"policy": "abort_generation"}
    if entrypoint == "operation.abort_generation":
        assert service.call("code.list", {"container": "demo.ipynb"}).ok
        revision = service.call("code.get", {"cell_id": "cell-main"}).value
        operation = service.call(
            "code.run",
            {"cell_id": "cell-main", "revision": 1, "source_sha256": revision.source_sha256, "inputs": {}, "wait_s": 1.0},
            caller_id="owner",
        ).value
        arguments = {"operation_id": operation.operation_id}

    denied = service.call(entrypoint, arguments, caller_id="observer")
    assert denied.ok is False
    assert denied.failure.category.value == "invalid_request"
    assert backend.closed is False
    assert service.call("runtime.status", {}, caller_id="owner").ok


def test_quarantined_cleanup_allows_unselected_retry(tmp_path: Path) -> None:
    class FlakyRejectedBackend(FakeRuntimeBackend):
        def __init__(self) -> None:
            super().__init__()
            self.fail_close = True

        def status(self) -> RuntimeDescriptor:
            return RuntimeDescriptor(self.runtime_id, 1, "ready", CapabilityMode.ADMIN)

        def close(self) -> None:
            self.close_calls += 1
            if self.fail_close:
                raise RuntimeError("secret close failure")
            self.closed = True

    backend = FlakyRejectedBackend()
    service = AgentWorkspaceService(tmp_path, FakeRuntimeFactory(backend), maximum_mode=CapabilityMode.EXPERIMENT)
    assert (
        ensure_terminal(service, caller_id="starter").state
        is AgentOperationState.UNKNOWN
    )
    backend.fail_close = False
    retried = service.call("runtime.close", {"policy": "abort_generation"}, caller_id="cleanup")
    assert retried.ok
    assert backend.closed is True
    assert service.call("runtime.list", {}).value == ()


def test_close_retries_failed_abort_journal_before_closing_or_running_queued_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, backend, _ = seeded_service(tmp_path)
    admitted = ensure_ready(service, caller_id="owner")
    assert admitted.ok
    assert service.call("code.list", {"container": "demo.ipynb"}).ok
    revision = service.call("code.get", {"cell_id": "cell-main"}).value
    service._operations._execution_lane.acquire()
    try:
        operation = service.call(
            "code.run",
            {
                "cell_id": "cell-main",
                "revision": revision.revision,
                "source_sha256": revision.source_sha256,
                "inputs": {},
            },
            caller_id="owner",
        ).value
        assert operation.state is AgentOperationState.QUEUED
        original_append = service._operations._append_event
        failed_once = False

        def fail_first_abort(payload: object) -> None:
            nonlocal failed_once
            if (
                not failed_once
                and isinstance(payload, dict)
                and payload.get("event") == "unknown"
                and payload.get("reason") == "generation_aborted"
            ):
                failed_once = True
                raise OSError("transient abort journal failure")
            original_append(payload)  # type: ignore[arg-type]

        monkeypatch.setattr(service._operations, "_append_event", fail_first_abort)

        failed = service.call(
            "runtime.close",
            {"policy": "abort_generation"},
            caller_id="owner",
        )

        assert failed.ok is False
        assert failed.failure.category.value == "platform_failure"
        assert failed.failure.state_changed.value == "unknown"
        assert backend.close_calls == 0
        assert service._runtime is not None and service._runtime.closing is True
        assert service._selected == {}
        assert service.call(
            "runtime.status",
            {"runtime_id": admitted.value.runtime_id},
            caller_id="observer",
        ).ok
        assert service.call("runtime.list", {}, caller_id="observer").ok

        retried = service.call(
            "runtime.close",
            {"policy": "abort_generation"},
            caller_id="cleanup",
        )
        assert retried.ok
        assert backend.close_calls == 1
        assert service.call("runtime.list", {}).value == ()
        assert service._operations.status(operation.operation_id).state is AgentOperationState.UNKNOWN
    finally:
        service._operations._execution_lane.release()

    future = service._operations._operations[operation.operation_id].future
    assert future is not None
    future.result(timeout=1)
    assert backend.sources == []


def test_operation_abort_quarantines_before_journal_failure_and_allows_cleanup_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, backend, _ = seeded_service(tmp_path)
    admitted = ensure_ready(service, caller_id="owner")
    assert admitted.ok
    assert service.call("code.list", {"container": "demo.ipynb"}).ok
    revision = service.call("code.get", {"cell_id": "cell-main"}).value
    service._operations._execution_lane.acquire()
    try:
        operation = service.call(
            "code.run",
            {
                "cell_id": "cell-main",
                "revision": revision.revision,
                "source_sha256": revision.source_sha256,
                "inputs": {},
            },
            caller_id="owner",
        ).value
        assert operation.state is AgentOperationState.QUEUED
        original_append = service._operations._append_event
        original_close = backend.close
        abort_attempts: list[tuple[bool, dict[str, str], int]] = []
        ordering: list[str] = []

        def fail_first_abort(payload: object) -> None:
            if (
                isinstance(payload, dict)
                and payload.get("event") == "unknown"
                and payload.get("reason") == "generation_aborted"
            ):
                runtime = service._runtime
                abort_attempts.append(
                    (
                        runtime is not None and runtime.closing,
                        service._selected.copy(),
                        backend.close_calls,
                    )
                )
                if len(abort_attempts) == 1:
                    raise OSError("transient abort journal failure")
                original_append(payload)  # type: ignore[arg-type]
                ordering.append("abort_persisted")
                return
            original_append(payload)  # type: ignore[arg-type]

        def close_after_durable_abort() -> None:
            assert ordering == ["abort_persisted"]
            ordering.append("backend_closed")
            original_close()

        monkeypatch.setattr(service._operations, "_append_event", fail_first_abort)
        monkeypatch.setattr(backend, "close", close_after_durable_abort)

        failed = service.call(
            "operation.abort_generation",
            {"operation_id": operation.operation_id},
            caller_id="owner",
        )

        assert failed.ok is False
        assert failed.failure.category.value == "platform_failure"
        assert failed.failure.state_changed.value == "unknown"
        assert abort_attempts == [(True, {}, 0)]
        assert backend.close_calls == 0
        assert service._runtime is not None and service._runtime.closing is True
        assert service._selected == {}
        assert service.call(
            "runtime.status",
            {"runtime_id": admitted.value.runtime_id},
            caller_id="observer",
        ).ok
        assert service.call("runtime.list", {}, caller_id="observer").ok
        assert service.call("runtime.ensure", {}, caller_id="owner").ok is False
        assert service.call(
            "runtime.select",
            {"runtime_id": admitted.value.runtime_id},
            caller_id="owner",
        ).ok is False
        assert service.call(
            "code.run",
            {
                "cell_id": "cell-main",
                "revision": revision.revision,
                "source_sha256": revision.source_sha256,
                "inputs": {},
            },
            caller_id="owner",
        ).ok is False

        retried = service.call(
            "runtime.close",
            {"policy": "abort_generation"},
            caller_id="cleanup",
        )
        assert retried.ok
        assert ordering == ["abort_persisted", "backend_closed"]
        assert backend.close_calls == 1
        assert service.call("runtime.list", {}).value == ()
        assert service._operations.status(operation.operation_id).state is AgentOperationState.UNKNOWN
    finally:
        service._operations._execution_lane.release()

    future = service._operations._operations[operation.operation_id].future
    assert future is not None
    future.result(timeout=1)
    assert backend.sources == []


def test_operation_abort_marks_current_generation_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, backend, _ = seeded_service(tmp_path)
    ensure_ready(service, caller_id="owner")
    assert service.call("code.list", {"container": "demo.ipynb"}).ok
    revision = service.call("code.get", {"cell_id": "cell-main"}).value
    service._operations._execution_lane.acquire()
    try:
        operation = service.call(
            "code.run",
            {
                "cell_id": "cell-main",
                "revision": revision.revision,
                "source_sha256": revision.source_sha256,
                "inputs": {},
            },
            caller_id="owner",
        ).value
        original_mark = service._operations.mark_generation_aborted
        mark_calls: list[tuple[str, int]] = []

        def counted_mark(runtime_id: str, generation: int) -> object:
            mark_calls.append((runtime_id, generation))
            return original_mark(runtime_id, generation)

        monkeypatch.setattr(service._operations, "mark_generation_aborted", counted_mark)
        aborted = service.call(
            "operation.abort_generation",
            {"operation_id": operation.operation_id},
            caller_id="owner",
        )

        assert aborted.ok
        assert mark_calls == [(backend.runtime_id, 1)]
        assert backend.close_calls == 1
    finally:
        service._operations._execution_lane.release()

    future = service._operations._operations[operation.operation_id].future
    assert future is not None
    future.result(timeout=1)
    assert backend.sources == []


def test_operation_abort_historical_generation_never_closes_current_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, backend, _ = seeded_service(tmp_path)
    ensure_ready(service, caller_id="owner")
    service._operations._execution_lane.acquire()
    try:
        historical = service._operations.submit(
            {
                "runtime_id": "runtime-historical",
                "runtime_generation": 7,
                "code_id": "cell-historical",
                "revision": 1,
                "source_sha256": "historical-source",
                "inputs_sha256": "historical-inputs",
            },
            lambda: pytest.fail("historical generation must not execute after abort"),
        )
        original_append = service._operations._append_event
        failed_once = False

        def fail_first_abort(payload: object) -> None:
            nonlocal failed_once
            if (
                not failed_once
                and isinstance(payload, dict)
                and payload.get("event") == "unknown"
                and payload.get("reason") == "generation_aborted"
            ):
                failed_once = True
                raise OSError("transient historical journal failure")
            original_append(payload)  # type: ignore[arg-type]

        monkeypatch.setattr(service._operations, "_append_event", fail_first_abort)
        failed = service.call(
            "operation.abort_generation",
            {"operation_id": historical.operation_id},
            caller_id="observer",
        )
        assert failed.ok is False
        assert failed.failure.category.value == "platform_failure"
        assert failed.failure.state_changed.value == "unknown"
        assert backend.close_calls == 0
        assert service.call("runtime.status", {}, caller_id="owner").ok

        retried = service.call(
            "operation.abort_generation",
            {"operation_id": historical.operation_id},
            caller_id="observer",
        )
        assert retried.ok
        assert service._operations.status(historical.operation_id).state is AgentOperationState.UNKNOWN
        assert backend.close_calls == 0
        assert service.call("runtime.status", {}, caller_id="owner").ok
    finally:
        service._operations._execution_lane.release()

    future = service._operations._operations[historical.operation_id].future
    assert future is not None
    future.result(timeout=1)


@pytest.mark.parametrize("missing", ["revision", "source_sha256"])
def test_code_run_requires_revision_and_hash_fences(tmp_path: Path, missing: str) -> None:
    service, backend, _ = seeded_service(tmp_path)
    ensure_ready(service)
    assert service.call("code.list", {"container": "demo.ipynb"}).ok
    revision = service.call("code.get", {"cell_id": "cell-main"}).value
    arguments: dict[str, object] = {
        "cell_id": "cell-main",
        "revision": revision.revision,
        "source_sha256": revision.source_sha256,
        "inputs": {},
    }
    arguments.pop(missing)
    denied = service.call("code.run", arguments)
    assert denied.ok is False
    assert denied.failure.category.value == "invalid_request"
    assert backend.sources == []


@pytest.mark.parametrize(
    ("method", "arguments"),
    [
        ("runtime.start", {"mode": 1}),
        ("runtime.ensure", {"mode": {"name": "experiment"}}),
        ("runtime.ensure", {"mode": "unknown"}),
        ("runtime.request_mode", {"mode": None}),
    ],
)
def test_runtime_mode_validation_rejects_wrong_types_and_unknown_values(
    tmp_path: Path,
    method: str,
    arguments: dict[str, object],
) -> None:
    backend = FakeRuntimeBackend()
    factory = FakeRuntimeFactory(backend)
    service = AgentWorkspaceService(tmp_path, factory, maximum_mode=CapabilityMode.EXPERIMENT)
    denied = service.call(method, arguments)
    assert denied.ok is False
    assert denied.failure.category.value == "invalid_request"
    assert factory.starts == 0


def test_runtime_mode_validation_happens_before_restart_closes_runtime(tmp_path: Path) -> None:
    service, backend, factory = seeded_service(tmp_path)
    ensure_ready(service, caller_id="owner")
    denied = service.call(
        "runtime.restart",
        {"policy": "abort_generation", "mode": "unknown"},
        caller_id="owner",
    )
    assert denied.ok is False
    assert denied.failure.category.value == "invalid_request"
    assert backend.closed is False
    assert factory.starts == 1
    assert service.call("runtime.status", {}, caller_id="owner").ok


def test_parser_failure_is_failed_operation_without_backend_target_call(
    tmp_path: Path,
) -> None:
    """Break caught: deterministic preparation must follow durable admission."""

    class PreparingRuntimeBackend(FakeRuntimeBackend):
        def __init__(self) -> None:
            super().__init__()
            self.target_execute_calls = 0

        def execute_bsl(self, source: str) -> BackendExecution:
            from onec_runtime_mcp.agent.contracts import StateChanged
            from onec_runtime.bsl import DiagnosticStage, normalize_source_error
            from onec_runtime.bsl.lexer import BslLexError
            from onec_runtime.bsl.notebook_cells import split_notebook_cell
            from onec_runtime.bsl.parser_target import BslParseError, PythonParserTarget
            from onec_runtime.bsl.source_maps import (
                SourceUnitKind,
                SourceUnitRef,
                mapped_visible_source,
                source_sha256,
            )

            unit = SourceUnitRef(
                SourceUnitKind.NOTEBOOK_CELL,
                "backend-preparation",
                1,
                source_sha256(source),
            )
            mapped = mapped_visible_source(source, unit)
            try:
                split_notebook_cell(
                    PythonParserTarget.from_generated(),
                    source,
                    source_unit=unit,
                )
            except (BslLexError, BslParseError) as error:
                return BackendExecution(
                    AgentOperationState.FAILED,
                    (),
                    False,
                    "ready",
                    failure_stage="parsing",
                    diagnostic=normalize_source_error(
                        error,
                        mapped,
                        stage=DiagnosticStage.PARSING,
                    ),
                    state_changed=StateChanged.NO,
                )
            self.target_execute_calls += 1
            return super().execute_bsl(source)

    backend = PreparingRuntimeBackend()
    service = AgentWorkspaceService(
        tmp_path,
        FakeRuntimeFactory(backend),
        maximum_mode=CapabilityMode.EXPERIMENT,
    )
    source = "Результат = ;"
    try:
        ensure_ready(service, {"mode": "experiment"})

        submitted = service.call(
            "code.run_inline",
            {
                "language": "bsl",
                "mode": "main",
                "source": source,
                "inputs": {},
                "wait_s": 2.0,
            },
        )

        assert submitted.ok
        view_response = service.call(
            "operation.view",
            {"operation_id": submitted.value.operation_id},
        )
        assert view_response.ok
        view = view_response.value
        assert view.state is AgentOperationState.FAILED
        assert view.operation.cell_id.startswith("inline-")
        assert view.operation.revision == 1
        assert view.operation.source_sha256 == hashlib.sha256(
            source.encode()
        ).hexdigest()
        assert view.failure["state_changed"] == "no"
        assert view.failure["diagnostic"]["stage"] == "parsing"
        assert backend.target_execute_calls == 0
    finally:
        service.close()


def test_agent_boundary_downgrades_malformed_diagnostic_without_public_copy() -> None:
    secret = "rdbg_pid=9182 token=do-not-persist"
    malformed = NormalizedDiagnostic(
        secret * 10,
        secret * 500,
        DiagnosticStage.PARSING,
        MappingConfidence.UNKNOWN,
        code=secret * 10,
    )
    injected = BackendExecution(
        AgentOperationState.FAILED,
        (secret,),
        False,
        "failed",
        failure_stage="parsing",
        diagnostic=malformed,
    )

    outcome = AgentWorkspaceService._sanitize_backend_execution(injected)

    assert outcome.terminal_state is AgentOperationState.UNKNOWN
    assert outcome.diagnostic is None
    assert outcome.messages == ()
    assert AgentWorkspaceService._visible_diagnostic_facts(malformed) is None
    assert secret not in json.dumps(to_wire(outcome), ensure_ascii=False)
