from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
import gc
import json
from pathlib import Path
import pickle
import re
from threading import Event, Thread
from uuid import UUID
from weakref import ref

import pytest

import onec_runtime.errors as runtime_errors
import onec_runtime.bsl.module_universe as module_universe
import onec_runtime.server_worker as server_worker
import onec_runtime.worker_epf as worker_epf
import onec_runtime.worker_stage_protocol as worker_stage_protocol
import onec_runtime.worker_universe as worker_universe
from onec_runtime.bsl import (
    CommonModuleScope,
    LineIndex,
    MappingRelation,
    VisibleSourceContext,
    mapped_visible_source,
    source_sha256,
)
from onec_runtime.bsl.module_universe import (
    CommonModuleDescriptor,
    CommonModuleCatalogSnapshot,
    LoweredWorkerModule,
    WorkerModuleUnit,
    analyze_worker_module,
    lower_worker_module,
)
from onec_runtime.bsl.parser_target import PythonParserTarget
from onec_runtime.bsl.source_maps import SourceUnitKind, SourceUnitRef
from onec_runtime.config import RuntimeConfig
from onec_runtime.errors import ProtocolError
from onec_runtime.performance_profile import PhaseRecorder
from onec_runtime.server_worker import (
    NotebookWorkerArtifactBuilder,
    build_worker_artifact,
)
from onec_runtime.bsl.semantic_lowering import WorkerExport
from onec_runtime.worker_universe import (
    WorkerModuleArtifact,
    WorkerModuleArtifactBuilder,
    WorkerModuleArtifactCache,
    WorkerModuleBinaryKey,
    validate_worker_module_artifact,
    worker_module_artifact_from_notebook,
)


SOURCE = (
    "Функция Рассчитать() Экспорт\n"
    "    Возврат 41;\n"
    "КонецФункции\n"
)


@pytest.mark.parametrize("action", ["prepare", "promote", "discard"])
def test_target_remote_mutations_do_not_hold_target_or_host_lock(tmp_path, action):
    artifacts = _generation_artifacts(tmp_path)[:2]
    host = worker_universe.WorkerUniverseRegistry(runtime_generation=7, context_generation=3)
    target = _UniverseTargetExecutor()
    ownership = []

    def execute(source):
        ownership.append((registry._lock._is_owned(), host._lock._is_owned()))
        return target(source)

    registry = worker_universe.ServerWorkerUniverseRegistry(host, execute)
    if action == "prepare":
        registry.prepare(artifacts)
    else:
        candidate = host.prepare(artifacts)
        target.acknowledge(candidate)
        if action == "promote":
            registry.promote(candidate)
        else:
            prepared = registry.prepare_root(candidate, transaction_id=UUID(int=9))
            registry.discard_root(prepared)
    assert ownership and not any(any(held) for held in ownership), ownership


def test_root_transaction_validation_precedes_any_staging(tmp_path):
    host = worker_universe.WorkerUniverseRegistry(runtime_generation=7, context_generation=3)
    candidate = host.prepare(_generation_artifacts(tmp_path)[:2])
    target = _UniverseTargetExecutor()
    registry = worker_universe.ServerWorkerUniverseRegistry(host, target)
    with pytest.raises(TypeError, match="transaction id"):
        registry.prepare_root(candidate, transaction_id="invalid")
    assert not target.calls
    assert not registry._registrations


def _mutation_registry(*, coordinated=False, remote=None):
    calls, settlements = [], []
    host = worker_universe.WorkerUniverseRegistry(runtime_generation=7, context_generation=3)
    def execute(source):
        assert not registry._lock._is_owned()
        calls.append(source)
        return remote(source) if remote is not None else "confirmed"
    def submit(plan):
        assert not registry._lock._is_owned()
        try:
            result = execute(plan.instruction)
        except BaseException as error:
            return plan.abort(error)
        return plan.commit(result)
    registry = worker_universe.ServerWorkerUniverseRegistry(
        host, execute, mutation_executor=submit if coordinated else None,
    )
    plan = registry.reserve_mutation(
        "original", commit=lambda result: settlements.append(("commit", result)),
        abort=lambda error: settlements.append(("abort", type(error).__name__)),
    )
    return registry, plan, calls, settlements


@pytest.mark.parametrize("coordinated", [False, True])
@pytest.mark.parametrize("failed", [False, True])
def test_worker_mutation_replay_cannot_dispatch_after_commit_or_abort(coordinated, failed):
    def remote(source):
        if failed:
            raise OSError("synthetic dispatch failure")
        return "confirmed"
    registry, plan, calls, settlements = _mutation_registry(coordinated=coordinated, remote=remote)
    registry.execute_mutation(plan)
    with pytest.raises(ProtocolError):
        registry.execute_mutation(plan)
    assert calls == ["original"]
    assert settlements == [("abort", "OSError") if failed else ("commit", "confirmed")]
    assert not registry._mutations


@pytest.mark.parametrize("coordinated", [False, True])
@pytest.mark.parametrize("invalid", ["instruction", "reservation", "owner", "foreign", "teardown", "abandon"])
def test_worker_mutation_invalid_plan_is_rejected_before_executor(coordinated, invalid):
    registry, plan, calls, settlements = _mutation_registry(coordinated=coordinated)
    other, other_plan, other_calls, _ = _mutation_registry(coordinated=coordinated)
    attempted = plan
    if invalid == "instruction":
        attempted = replace(plan, instruction="forged")
    elif invalid == "reservation":
        attempted = replace(plan, reservation=replace(plan.reservation))
    elif invalid == "owner":
        attempted = replace(plan, owner=other)
    elif invalid == "foreign":
        attempted = other_plan
    elif invalid == "teardown":
        registry.teardown()
    else:
        registry.abandon_target()
    with pytest.raises(ProtocolError):
        registry.execute_mutation(attempted)
    assert not calls and not other_calls
    assert not settlements
    if invalid not in {"teardown", "abandon"}:
        registry.execute_mutation(plan)
        assert calls == ["original"]
        assert settlements == [("commit", "confirmed")]


@pytest.mark.parametrize("coordinated", [False, True])
def test_concurrent_worker_mutation_claim_has_one_remote_owner(coordinated):
    entered, release, second_finished = Event(), Event(), Event()
    errors = []
    def remote(source):
        entered.set()
        assert release.wait(2)
        return "confirmed"
    registry, plan, calls, settlements = _mutation_registry(coordinated=coordinated, remote=remote)
    def execute(*, second=False):
        try:
            registry.execute_mutation(plan)
        except BaseException as error:
            errors.append(error)
        finally:
            if second:
                second_finished.set()
    first = Thread(target=execute)
    second = Thread(target=lambda: execute(second=True))
    first.start()
    try:
        assert entered.wait(1)
        second.start()
        assert second_finished.wait(0.5), "duplicate owner entered the blocked remote executor"
        assert len(errors) == 1 and isinstance(errors[0], ProtocolError)
    finally:
        release.set()
        first.join(2)
        if second.ident is not None:
            second.join(2)
        assert not first.is_alive() and not second.is_alive()
    assert calls == ["original"]
    assert settlements == [("commit", "confirmed")]
    assert not registry._mutations


@pytest.mark.parametrize("unavailable", ["broken", "closed", "registry_broken"])
def test_worker_mutation_reservation_rejects_unavailable_host(unavailable):
    host = worker_universe.WorkerUniverseRegistry(runtime_generation=7, context_generation=3)
    registry = worker_universe.ServerWorkerUniverseRegistry(host, lambda source: None)
    if unavailable == 'broken':
        host.mark_broken()
    elif unavailable == 'closed':
        host.teardown()
    else:
        registry._broken = True
    with pytest.raises(ProtocolError, match='unavailable'):
        registry.reserve_mutation('synthetic', commit=lambda result: result, abort=lambda error: None)
    assert not registry._mutations and not registry._claimed_mutations


@pytest.mark.parametrize("coordinated", [False, True])
@pytest.mark.parametrize("unavailable", ["broken", "quarantined", "closed", "registry_broken"])
def test_reserved_worker_swap_cannot_dispatch_to_unavailable_host(tmp_path, coordinated, unavailable):
    host = worker_universe.WorkerUniverseRegistry(runtime_generation=7, context_generation=3)
    artifacts = _generation_artifacts(tmp_path)
    target = _UniverseTargetExecutor()
    calls, submissions = [], []
    def execute(source):
        assert not registry._lock._is_owned() and not host._lock._is_owned()
        calls.append(source)
        return target(source)
    registry = worker_universe.ServerWorkerUniverseRegistry(host, execute)
    active = host.prepare(artifacts[:2])
    target.acknowledge(active)
    registry.promote(active)
    pin = host.pin_active()
    candidate = host.prepare((artifacts[0], artifacts[2]))
    target.acknowledge(candidate)
    prepared = registry.prepare_root(candidate, transaction_id=UUID(int=701))
    mutation = registry.reserve_swap_root(prepared)
    if unavailable == 'broken':
        host.mark_broken(candidate)
    elif unavailable == 'quarantined':
        host.retain_outcome_unknown(pin)
    elif unavailable == 'closed':
        host.teardown()
    else:
        registry._broken = True
    def submit(plan):
        submissions.append(plan)
        return plan.commit(execute(plan.instruction))
    if coordinated:
        registry._mutation_executor = submit
    before = len(calls)
    for _ in range(2):
        with pytest.raises(ProtocolError, match='unavailable'):
            registry.execute_mutation(mutation)
    assert len(calls) == before and not submissions
    # The unexecuted reservation stays owned locally, never claimed/committed.
    assert registry._mutations == {mutation.reservation.token: mutation}
    assert not registry._claimed_mutations
    assert prepared.transaction_id in registry._prepared_roots
    registry.abandon_target()
    assert not registry._mutations and not registry._claimed_mutations
    assert not registry._prepared_roots


class _CountingArtifactBuilder:
    def __init__(self, wrapped: NotebookWorkerArtifactBuilder) -> None:
        self.wrapped = wrapped
        self.calls = 0

    def __call__(self, *args: object, **kwargs: object):
        self.calls += 1
        return self.wrapped(*args, **kwargs)


def _lowered(
    source: str = SOURCE,
    *,
    revision: int = 1,
    logical_name: str = "МодульРасчета",
    common_modules: tuple[CommonModuleDescriptor, ...] = (),
) -> tuple[LoweredWorkerModule, VisibleSourceContext]:
    unit_ref = SourceUnitRef(
        SourceUnitKind.MODULE,
        logical_name,
        revision,
        source_sha256(source),
    )
    mapped = mapped_visible_source(source, unit_ref)
    catalog = CommonModuleCatalogSnapshot.create(
        profile="server-test",
        preprocessor_profile="server",
        revision=1,
        modules=common_modules,
    )
    unit = WorkerModuleUnit(
        logical_name,
        "module",
        revision,
        mapped,
    )
    analyzed = analyze_worker_module(
        unit,
        catalog,
        PythonParserTarget.from_generated(),
    )
    return lower_worker_module(analyzed), VisibleSourceContext({unit_ref: source})


def _notebook_builder(tmp_path: Path) -> NotebookWorkerArtifactBuilder:
    platform = tmp_path / "platform"
    platform.mkdir()
    for executable in ("1cv8.exe", "1cv8c.exe", "dbgs.exe"):
        (platform / executable).write_bytes(b"stub")
    return NotebookWorkerArtifactBuilder(RuntimeConfig(tmp_path, platform))


def _builder(
    tmp_path: Path,
) -> tuple[WorkerModuleArtifactBuilder, _CountingArtifactBuilder, WorkerModuleArtifactCache]:
    packer = _CountingArtifactBuilder(_notebook_builder(tmp_path))
    cache = WorkerModuleArtifactCache()
    return (
        WorkerModuleArtifactBuilder(
            packer,
            cache=cache,
            packer_version="worker-epf-v1",
            target_profile="server-test",
        ),
        packer,
        cache,
    )


def test_semantically_admitted_packaging_does_not_reparse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first semantic analysis is the packaging admission proof."""
    lowered, context = _lowered()
    builder, _, _ = _builder(tmp_path)

    def reject_packaging_parse(*_args: object, **_kwargs: object) -> object:
        pytest.fail("packaging reparsed source")

    monkeypatch.setattr(
        server_worker.SemanticNotebookLowerer,
        "bind_module",
        reject_packaging_parse,
    )

    artifact = builder.build(lowered, visible_source_context=context)

    assert validate_worker_module_artifact(artifact) == artifact.exports


def test_admitted_snapshot_reads_and_hashes_large_payload_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: downstream admission consumers must not reopen or rehash payloads."""
    large_source = SOURCE.replace(
        "    Возврат 41;",
        f"    // {'x' * 131_072}\n    Возврат 41;",
    )
    lowered, context = _lowered(large_source)
    builder, _, _ = _builder(tmp_path)
    original_read_bytes = Path.read_bytes
    original_sha256 = server_worker.sha256
    reads = {"source": 0, "artifact": 0}
    payloads: dict[str, bytes] = {}
    hashes = {"source": 0, "artifact": 0}

    def counted_read_bytes(path: Path) -> bytes:
        payload = original_read_bytes(path)
        resolved = path.resolve()
        label = None
        if (
            resolved.name == "ObjectModule.bsl"
            and "notebook-workers" in resolved.parts
        ):
            label = "source"
        elif resolved.suffix == ".epf" and "notebook-workers" in resolved.parts:
            label = "artifact"
        if label is not None:
            reads[label] += 1
            payloads[label] = payload
        return payload

    def counted_sha256(payload: bytes = b"", *args: object, **kwargs: object):
        if isinstance(payload, bytes):
            for label, admitted in payloads.items():
                if payload is admitted or payload == admitted:
                    hashes[label] += 1
        return original_sha256(payload, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", counted_read_bytes)
    monkeypatch.setattr(server_worker, "sha256", counted_sha256)

    artifact = builder.build(lowered, visible_source_context=context)
    assert validate_worker_module_artifact(artifact) == artifact.exports
    snapshot = server_worker._validated_admitted_snapshot(
        artifact.worker_artifact
    )
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=1,
        context_generation=1,
    )
    candidate = host.prepare((artifact,))
    target = worker_universe.ServerWorkerUniverseRegistry(host, lambda _source: None)
    stages = target.stages(candidate)
    module = candidate.manifest.modules[0]
    diagnostic = server_worker.worker_artifact_diagnostic_source(
        artifact.worker_artifact,
        logical_name=artifact.logical_name,
        revision=artifact.revision,
        registration_name=module.registration_name,
        manifest_sha256=candidate.manifest.sha256,
        artifact_sha256=artifact.artifact_sha256,
        source_map_sha256=artifact.source_map_sha256,
    )

    assert len(stages) == 1
    encoded_artifact = base64.b64encode(snapshot.artifact_bytes).decode("ascii")
    assert encoded_artifact in stages[0].instruction
    assert diagnostic.mapped_source is not snapshot.mapped_source
    assert (
        diagnostic.mapped_source.source_map.to_manifest()
        == snapshot.mapped_source.source_map.to_manifest()
    )
    assert reads == {"source": 1, "artifact": 1}
    assert hashes == {"source": 1, "artifact": 1}


def test_semantic_admission_is_bound_to_the_exact_lowered_object(
    tmp_path: Path,
) -> None:
    lowered, context = _lowered()
    forged = replace(lowered)
    builder, packer, _ = _builder(tmp_path)

    with pytest.raises(
        ProtocolError,
        match="Worker module artifact admission is invalid",
    ):
        builder.build(forged, visible_source_context=context)

    assert packer.calls == 0


@pytest.mark.parametrize(
    "tamper",
    ("lowered_source", "exports", "catalog", "transform"),
)
def test_semantic_admission_rejects_mutated_semantic_identity(
    tmp_path: Path,
    tamper: str,
) -> None:
    lowered, context = _lowered()
    builder, packer, _ = _builder(tmp_path)
    if tamper == "lowered_source":
        replacement, _ = _lowered(SOURCE.replace("41", "42"))
        object.__setattr__(lowered, "mapped_source", replacement.mapped_source)
    elif tamper == "exports":
        object.__setattr__(
            lowered.analysis,
            "exported_methods",
            ("ДругойМетод",),
        )
    elif tamper == "catalog":
        identity = getattr(lowered.analysis, "catalog_identity", None)
        assert identity is not None
        object.__setattr__(
            lowered.analysis,
            "catalog_identity",
            (*identity[:-1], "f" * 64),
        )
    else:
        object.__setattr__(lowered, "transform_version", "forged-transform-v1")

    with pytest.raises(
        ProtocolError,
        match="Worker module artifact admission is invalid",
    ):
        builder.build(lowered, visible_source_context=context)

    assert packer.calls == 0


@pytest.mark.parametrize(
    "tamper",
    ("raw_text", "lowered_text", "source_map", "lineage", "dependency"),
)
def test_semantic_admission_rejects_in_place_canonical_identity_tamper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    modules = (
        CommonModuleDescriptor("КадровыйУчет", CommonModuleScope.SERVER),
    )
    lowered, context = _lowered(
        (
            "Функция Рассчитать() Экспорт\n"
            "    Возврат КадровыйУчет.Получить();\n"
            "КонецФункции\n"
        ),
        common_modules=modules,
    )
    if tamper == "raw_text":
        raw = lowered.analysis.unit.mapped_source
        object.__setattr__(raw, "_text", raw.text + " ")
    elif tamper == "lowered_text":
        mapped = lowered.mapped_source
        object.__setattr__(mapped, "_text", mapped.text + " ")
    elif tamper == "source_map":
        source_map = lowered.mapped_source.source_map
        first = source_map.segments[0]
        object.__setattr__(
            source_map,
            "segments",
            (replace(first, synthetic_region="tampered-map"), *source_map.segments[1:]),
        )
    elif tamper == "lineage":
        lineage_map = lowered.mapped_source.lineage[0]
        assert lineage_map is not lowered.mapped_source.source_map
        first = lineage_map.segments[0]
        object.__setattr__(
            lineage_map,
            "segments",
            (
                replace(first, synthetic_region="tampered-lineage"),
                *lineage_map.segments[1:],
            ),
        )
    else:
        binding = lowered.analysis.dependencies[0]
        object.__setattr__(binding, "target_module", "ПодмененныйМодуль")

    native_packer_calls = 0
    original_native_packer = worker_epf.build_worker_epf

    def counted_native_packer(*args: object, **kwargs: object) -> None:
        nonlocal native_packer_calls
        native_packer_calls += 1
        original_native_packer(*args, **kwargs)

    monkeypatch.setattr(worker_epf, "build_worker_epf", counted_native_packer)
    builder, _, _ = _builder(tmp_path)

    if tamper == "dependency":
        with pytest.raises(
            ValueError,
            match="worker semantic admission is invalid",
        ):
            module_universe.worker_semantic_admission(
                lowered,
                packer_identity="worker-epf-v1",
            )
        with pytest.raises(
            ProtocolError,
            match="Worker module artifact admission is invalid",
        ):
            builder.build(lowered, visible_source_context=context)
    else:
        with pytest.raises(
            ProtocolError,
            match="Worker module artifact admission is invalid",
        ):
            builder.build(lowered, visible_source_context=context)

    assert native_packer_calls == 0


def test_notebook_packaging_rejects_semantic_admission_from_another_lowered_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, _ = _lowered(revision=1)
    second, second_context = _lowered(revision=2)
    admission_factory = getattr(module_universe, "worker_semantic_admission", None)
    assert callable(admission_factory)
    native_packer_calls = 0
    original_native_packer = worker_epf.build_worker_epf

    def counted_native_packer(*args: object, **kwargs: object) -> None:
        nonlocal native_packer_calls
        native_packer_calls += 1
        original_native_packer(*args, **kwargs)

    monkeypatch.setattr(worker_epf, "build_worker_epf", counted_native_packer)

    with pytest.raises(
        ProtocolError,
        match="Worker module artifact admission is invalid",
    ):
        _notebook_builder(tmp_path)(
            second.mapped_source,
            (
                WorkerExport(
                    "МодульРасчета.Рассчитать",
                    "Рассчитать",
                    receiver_module="МодульРасчета",
                ),
            ),
            visible_source_context=second_context,
            semantic_admission=admission_factory(
                first,
                packer_identity="worker-epf-v1",
            ),
            semantic_packer_identity="worker-epf-v1",
        )

    assert native_packer_calls == 0


def test_semantic_admission_rejects_cross_packer_identity_before_native_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lowered, context = _lowered()
    admission_factory = getattr(module_universe, "worker_semantic_admission", None)
    assert callable(admission_factory)
    native_packer_calls = 0
    original_native_packer = worker_epf.build_worker_epf

    def counted_native_packer(*args: object, **kwargs: object) -> None:
        nonlocal native_packer_calls
        native_packer_calls += 1
        original_native_packer(*args, **kwargs)

    monkeypatch.setattr(worker_epf, "build_worker_epf", counted_native_packer)

    with pytest.raises(
        ProtocolError,
        match="Worker module artifact admission is invalid",
    ):
        _notebook_builder(tmp_path)(
            lowered.mapped_source,
            (
                WorkerExport(
                    "МодульРасчета.Рассчитать",
                    "Рассчитать",
                    receiver_module="МодульРасчета",
                ),
            ),
            visible_source_context=context,
            semantic_admission=admission_factory(
                lowered,
                packer_identity="packer-a",
            ),
            semantic_packer_identity="packer-b",
        )

    assert native_packer_calls == 0


def _generation_artifacts(
    tmp_path: Path,
) -> tuple[
    WorkerModuleArtifact,
    WorkerModuleArtifact,
    WorkerModuleArtifact,
    WorkerModuleArtifact,
]:
    builder, _, _ = _builder(tmp_path)
    modules = (
        CommonModuleDescriptor("МодульА", CommonModuleScope.SERVER),
        CommonModuleDescriptor("МодульБ", CommonModuleScope.SERVER),
        CommonModuleDescriptor("Оригинальный", CommonModuleScope.SERVER),
    )

    def build(logical_name: str, source: str, revision: int) -> WorkerModuleArtifact:
        lowered, context = _lowered(
            source,
            logical_name=logical_name,
            revision=revision,
            common_modules=modules,
        )
        return builder.build(lowered, visible_source_context=context)

    a17 = build(
        "МодульА",
        (
            "Функция Рассчитать() Экспорт\n"
            "    Возврат МодульБ.Рассчитать();\n"
            "КонецФункции\n"
        ),
        17,
    )
    b17 = build(
        "МодульБ",
        (
            "Функция Рассчитать() Экспорт\n"
            "    Возврат МодульА.Рассчитать();\n"
            "КонецФункции\n"
        ),
        17,
    )
    b18 = build(
        "МодульБ",
        (
            "Функция Рассчитать() Экспорт\n"
            "    Возврат МодульА.Рассчитать() + 1;\n"
            "КонецФункции\n"
        ),
        18,
    )
    original = build(
        "ПотребительОригинального",
        (
            "Функция Рассчитать() Экспорт\n"
            "    Возврат Оригинальный.Получить();\n"
            "КонецФункции\n"
        ),
        1,
    )
    return a17, b17, b18, original


def _batch_generation_artifacts(
    tmp_path: Path,
    count: int,
) -> tuple[WorkerModuleArtifact, ...]:
    builder, _, _ = _builder(tmp_path)
    modules = tuple(
        CommonModuleDescriptor(f"BatchModule{index}", CommonModuleScope.SERVER)
        for index in range(count)
    )
    artifacts = []
    for index, module in enumerate(modules, start=1):
        source = SOURCE.replace("Возврат 41", f"Возврат {index}")
        lowered, context = _lowered(
            source,
            logical_name=module.canonical_name,
            revision=index,
            common_modules=modules,
        )
        artifacts.append(builder.build(lowered, visible_source_context=context))
    return tuple(artifacts)


def _large_batch_generation_artifacts(
    count: int,
    *,
    payload_size: int,
    shared_payload: bytes | None = None,
) -> tuple[tuple[WorkerModuleArtifact, ...], dict[str, bytes]]:
    payloads: dict[str, bytes] = {}

    def package(
        mapped,
        exports,
        *,
        visible_source_context,
        **_kwargs: object,
    ):
        logical_name = exports[0].receiver_module
        assert logical_name is not None
        seed = worker_universe.sha256(mapped.text.encode("utf-8")).digest()
        payload = (
            shared_payload
            if shared_payload is not None
            else (seed * ((payload_size + len(seed) - 1) // len(seed)))[:payload_size]
        )
        payloads[logical_name] = payload
        source_bytes = mapped.text.encode("utf-8")
        return server_worker._build_admitted_worker_artifact(
            logical_name=logical_name,
            source_path=Path(f"{logical_name}.bsl"),
            artifact_path=Path(f"{logical_name}.epf"),
            source_bytes=source_bytes,
            artifact_bytes=payload,
            source_bytes_sha256=worker_universe.sha256(source_bytes).hexdigest(),
            mapped=mapped,
            visible_context_snapshot=server_worker._snapshot_visible_source_context(
                mapped,
                visible_source_context,
                required=True,
            ),
            expected_version=None,
            expected_value=None,
            exports=exports,
        )

    builder = WorkerModuleArtifactBuilder(
        package,
        cache=WorkerModuleArtifactCache(),
        packer_version="large-worker-epf-v1",
        target_profile="server-test",
    )
    modules = tuple(
        CommonModuleDescriptor(f"LargeBatchModule{index}", CommonModuleScope.SERVER)
        for index in range(count)
    )
    artifacts: list[WorkerModuleArtifact] = []
    for index, module in enumerate(modules, start=1):
        source = SOURCE.replace("Возврат 41", f"Возврат {index}")
        lowered, context = _lowered(
            source,
            logical_name=module.canonical_name,
            revision=index,
            common_modules=modules,
        )
        artifacts.append(builder.build(lowered, visible_source_context=context))
    return tuple(artifacts), payloads


def _acknowledged_receipt(candidate, transaction_id: UUID | None = None):
    return worker_universe.WorkerPromotionReceipt(
        transaction_id=transaction_id or UUID(int=1),
        generation=candidate.handle.generation,
        manifest_sha256=candidate.manifest.sha256,
        root_key=f"generation-{candidate.handle.generation}",
        previous_root_key=(
            ""
            if candidate.previous is None
            else f"generation-{candidate.previous.generation}"
        ),
        acknowledged=True,
        generation_create_wire_probe_ms=13,
        root_swap_ms=2,
    )


def _candidate_diagnostic_artifacts(candidate):
    return tuple(
        server_worker.worker_artifact_diagnostic_source(
            artifact.worker_artifact,
            logical_name=module.logical_name,
            revision=module.revision,
            registration_name=module.registration_name,
            manifest_sha256=candidate.manifest.sha256,
            artifact_sha256=module.artifact_sha256,
            source_map_sha256=artifact.source_map_sha256,
        )
        for artifact, module in zip(
            candidate.artifacts,
            candidate.manifest.modules,
            strict=True,
        )
    )


class _UniverseTargetExecutor:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.registrations: set[str] = set()
        self.registration_urls: dict[str, str] = {}
        self.disconnects: list[str] = []
        self.stage_batch_calls = 0
        self.stage_batch_registrations: list[tuple[str, ...]] = []
        self.stage_batch_payload_observations: list[
            tuple[tuple[int, str], ...]
        ] = []
        self.storage_session_id = "universe-target"
        self.receipt: str | None = None
        self.receipt_manifest_sha256: str | None = None
        self.active_manifest_sha256: str | None = None
        self.active_root_key: str | None = None
        self.prepared_roots: dict[str, tuple[int, str, str, str]] = {}
        self.fault: str | None = None
        self.stage_failure_batch_index: int | None = None
        self.stage_failure_item_index = 0

    @staticmethod
    def _batch_entries(source: str) -> tuple[tuple[str, str], ...]:
        entries = re.findall(
            r'Новый Структура\('
            r'"registration_name,artifact_sha256,temp_storage_url", '
            r'"([A-Za-z_][A-Za-z0-9_]*)", "([0-9a-f]{64})", '
            r'АдресАртефактаWorker(\d+)\);',
            source,
        )
        assert entries
        assert tuple(int(entry[2]) for entry in entries) == tuple(
            range(len(entries))
        )
        return tuple((entry[0], entry[1]) for entry in entries)

    @staticmethod
    def _batch_identity(source: str) -> tuple[str, int, int, str]:
        match = re.search(
            r'"onec-worker-stage-batch-receipt", 2, '
            r'"([0-9a-f-]{36})", (\d+), (\d+), "([0-9a-f]{64})", '
            r'СтатусWorker',
            source,
        )
        assert match is not None
        return (
            match.group(1),
            int(match.group(2)),
            int(match.group(3)),
            match.group(4),
        )

    def _url(self, transaction_id: str, batch_index: int, item_index: int) -> str:
        return (
            f"e1cib/tempstorage/{transaction_id}-{batch_index}-{item_index}"
            f"?seanceId={self.storage_session_id}"
        )

    def _batch_outcome(
        self,
        *,
        transaction_id: str,
        batch_index: int,
        batch_count: int,
        batch_digest: str,
        entries: tuple[tuple[str, str], ...],
        connected_count: int,
        failure: dict[str, object] | None,
    ) -> str:
        connected = []
        for item_index, (registration_name, artifact_sha256) in enumerate(
            entries[:connected_count]
        ):
            url = self._url(transaction_id, batch_index, item_index)
            self.registration_urls[registration_name] = url
            connected.append(
                {
                    "registration_name": registration_name,
                    "artifact_sha256": artifact_sha256,
                    "temp_storage_url": url,
                }
            )
        return json.dumps(
            {
                "schema": worker_stage_protocol.WORKER_STAGE_SCHEMA,
                "schema_version": worker_stage_protocol.WORKER_STAGE_SCHEMA_VERSION,
                "transaction_id": transaction_id,
                "batch_index": batch_index,
                "batch_count": batch_count,
                "batch_digest": batch_digest,
                "status": "succeeded" if failure is None else "failed",
                "connected": connected,
                "failure": False if failure is None else failure,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @staticmethod
    def _batch_payload_observation(source: str) -> tuple[tuple[int, str], ...]:
        encoded = tuple(
            re.finditer(
                r'ДанныеАртефактаWorker(\d+) = Base64Значение\('
                r'"([A-Za-z0-9+/]+={0,2})"\);',
                source,
            )
        )
        assert tuple(int(match.group(1)) for match in encoded) == tuple(
            range(len(encoded))
        )
        payloads = tuple(
            base64.b64decode(match.group(2), validate=True)
            for match in encoded
        )
        return tuple(
            (len(payload), worker_universe.sha256(payload).hexdigest())
            for payload in payloads
        )

    def __call__(self, source: str) -> object:
        self.calls.append(source)
        if '"onec-worker-stage-batch-receipt"' in source:
            self.stage_batch_calls += 1
            entries = self._batch_entries(source)
            self.stage_batch_payload_observations.append(
                self._batch_payload_observation(source)
            )
            self.stage_batch_registrations.append(
                tuple(registration for registration, _sha in entries)
            )
            transaction_id, batch_index, batch_count, batch_digest = (
                self._batch_identity(source)
            )
            if self.fault in {"decode", "upload", "connect"} and (
                self.stage_failure_batch_index is None
                or self.stage_failure_batch_index == batch_index
            ):
                item_index = self.stage_failure_item_index
                assert 0 <= item_index < len(entries)
                for registration, _sha in entries[:item_index]:
                    self.registrations.add(registration)
                diagnostic = (
                    "{ВнешняяОбработка."
                    f"{entries[item_index][0]}.МодульОбъекта(1,1)}}: "
                    f"platform {self.fault} failure "
                    "[ОшибкаКомпиляцииВстроенногоЯзыка]"
                )
                orphan_url = (
                    None
                    if self.fault in {"decode", "upload"}
                    else self._url(transaction_id, batch_index, item_index)
                )
                return self._batch_outcome(
                    transaction_id=transaction_id,
                    batch_index=batch_index,
                    batch_count=batch_count,
                    batch_digest=batch_digest,
                    entries=entries,
                    connected_count=item_index,
                    failure={
                        "item_index": item_index,
                        "phase": self.fault,
                        "outcome": (
                            "known_pre_swap"
                            if self.fault in {"decode", "upload"}
                            else "registration_outcome_unknown"
                        ),
                        "diagnostic": diagnostic,
                        "orphan_url": orphan_url,
                    },
                )
            if self.fault == "marker-mismatch":
                return self._batch_outcome(
                    transaction_id=transaction_id,
                    batch_index=batch_index,
                    batch_count=batch_count,
                    batch_digest="f" * 64,
                    entries=entries,
                    connected_count=0,
                    failure={
                        "item_index": 0,
                        "phase": "connect",
                        "outcome": "registration_outcome_unknown",
                        "diagnostic": "sensitive platform failure",
                        "orphan_url": self._url(transaction_id, batch_index, 0),
                    },
                )
            if self.fault == "registration-result-mismatch":
                self.registrations.add(
                    "OnecRuntime_untracked_ffffffffffffffff"
                )
                return self._batch_outcome(
                    transaction_id=transaction_id,
                    batch_index=batch_index,
                    batch_count=batch_count,
                    batch_digest=batch_digest,
                    entries=entries,
                    connected_count=0,
                    failure={
                        "item_index": 0,
                        "phase": "registration-result",
                        "outcome": "registration_outcome_unknown",
                        "diagnostic": "platform registration identity mismatch",
                        "orphan_url": self._url(transaction_id, batch_index, 0),
                    },
                )
            if self.fault == "registration-marker-mismatch":
                outcome = json.loads(
                    self._batch_outcome(
                        transaction_id=transaction_id,
                        batch_index=batch_index,
                        batch_count=batch_count,
                        batch_digest=batch_digest,
                        entries=entries,
                        connected_count=1,
                        failure=None,
                    )
                )
                outcome["connected"][0]["registration_name"] = (
                    "OnecRuntime_wrong_ffffffffffffffff"
                )
                return json.dumps(outcome, separators=(",", ":"))
            for registration, _sha in entries:
                self.registrations.add(registration)
            if self.fault == "connect-unacknowledged":
                raise OSError("connect response lost")
            if self.fault == "timeout":
                raise TimeoutError("sensitive target timeout")
            if self.fault == "receipt":
                return "invalid-worker-stage-batch-receipt"
            receipt_entries = entries
            if self.fault == "receipt-order":
                receipt_entries = tuple(reversed(receipt_entries))
            elif self.fault == "receipt-identity":
                receipt_entries = (
                    (entries[0][0], "f" * 64),
                    *entries[1:],
                )
            return self._batch_outcome(
                transaction_id=transaction_id,
                batch_index=batch_index,
                batch_count=batch_count,
                batch_digest=batch_digest,
                entries=receipt_entries,
                connected_count=len(receipt_entries),
                failure=None,
            )
        if "onec-worker-artifact-stage=" in source and "phase=connect" in source:
            registration = re.search(
                r"OnecRuntime_[0-9a-f]{8}_[0-9a-f]{16}", source
            )
            assert registration is not None
            if self.fault in {"upload", "connect"}:
                marker = re.search(
                    r"onec-worker-artifact-stage="
                    r"artifact_sha256=[0-9a-f]{64};"
                    r"logical_name_sha256=[0-9a-f]{64};phase=connect",
                    source,
                )
                assert marker is not None
                raise runtime_errors.BslExecutionError(
                    f"{marker.group(0)};boundary={self.fault}\nplatform {self.fault} failure"
                )
            if self.fault == "connect-unacknowledged":
                self.registrations.add(registration.group(0))
                raise OSError("connect response lost")
            self.registrations.add(registration.group(0))
            return registration.group(0)
        if "onec-worker-root-prepare-stage=" in source:
            assert self.receipt_manifest_sha256 is not None
            if self.fault in {"create", "wire", "probe"}:
                raise runtime_errors.BslExecutionError(
                    f"onec-worker-root-prepare-stage={self.fault}\n"
                    f"platform {self.fault} failure"
                )
            transaction = re.search(
                r"onec-worker-prepared-root-receipt-v1\|([0-9a-f-]{36})\|",
                source,
            )
            generation = re.search(r'Вставить\("Generation", (\d+)\);', source)
            manifest = re.search(
                r'Вставить\("ManifestSha256", "([0-9a-f]{64})"\);',
                source,
            )
            root = re.search(
                r'Вставить\("CandidateRootKey", "([^"|]+)"\);', source
            )
            previous = re.search(
                r'Вставить\("PreviousRootKey", "([^"|]*)"\);', source
            )
            assert transaction and generation and manifest and root and previous
            identity = (
                int(generation.group(1)),
                manifest.group(1),
                root.group(1),
                previous.group(1),
            )
            self.prepared_roots[transaction.group(1)] = identity
            return (
                "onec-worker-prepared-root-receipt-v1|"
                f"{transaction.group(1)}|{identity[0]}|{identity[1]}|"
                f"{identity[2]}|{identity[3] or '-'}|13"
            )
        if "onec-worker-root-swap-stage=guard" in source:
            transaction = re.search(
                r"onec-worker-root-swap-receipt-v1\|([0-9a-f-]{36})\|",
                source,
            )
            assert transaction is not None
            identity = self.prepared_roots[transaction.group(1)]
            generation, manifest, root, previous = identity
            if previous != (self.active_root_key or ""):
                raise runtime_errors.BslExecutionError(
                    "onec-worker-root-swap-stage=guard\nprevious root mismatch"
                )
            if self.fault == "swap-ack":
                self.active_manifest_sha256 = manifest
                self.active_root_key = root
                raise OSError("target response lost")
            self.active_manifest_sha256 = manifest
            self.active_root_key = root
            del self.prepared_roots[transaction.group(1)]
            if self.fault == "receipt":
                return "invalid-promotion-receipt"
            return (
                "onec-worker-root-swap-receipt-v1|"
                f"{transaction.group(1)}|{generation}|{manifest}|{root}|"
                f"{previous or '-'}|1|13|2"
            )
        if "onec-worker-root-discard-stage=guard" in source:
            transaction = re.search(
                r"onec-worker-root-discard-receipt-v1\|([0-9a-f-]{36})\|",
                source,
            )
            assert transaction is not None
            generation, manifest, root, _previous = self.prepared_roots.pop(
                transaction.group(1)
            )
            return (
                "onec-worker-root-discard-receipt-v1|"
                f"{transaction.group(1)}|{generation}|{manifest}|{root}"
            )
        if "ВнешниеОбработки.Отключить" in source:
            registration = re.search(
                r"OnecRuntime_[0-9a-f]{8}_[0-9a-f]{16}", source
            )
            assert registration is not None
            self.disconnects.append(registration.group(0))
            if self.fault == "disconnect":
                raise OSError("disconnect response lost")
            self.registrations.remove(registration.group(0))
            return True
        raise AssertionError("unexpected target instruction")

    def acknowledge(self, candidate) -> None:
        receipt = _acknowledged_receipt(candidate)
        self.receipt_manifest_sha256 = receipt.manifest_sha256
        self.receipt = "split-root-workflow"


class _PreSwapTimestampFailureExecutor(_UniverseTargetExecutor):
    def __init__(self, timer_index: int, classified_phase: str) -> None:
        super().__init__()
        self.timer_index = timer_index
        self.classified_phase = classified_phase

    def __call__(self, source: str) -> object:
        if "onec-worker-root-prepare-stage=" not in source:
            return super().__call__(source)
        self.calls.append(source)
        timestamps = tuple(
            match.start()
            for match in re.finditer(
                re.escape("ТекущаяУниверсальнаяДатаВМиллисекундах()"),
                source,
            )
        )
        try_start = source.index("Попытка\n")
        catch_start = source.index("\nИсключение\n") + 1
        create = source.index("Новый Соответствие")
        immutable_root = source.index(
            "Новый ФиксированнаяСтруктура(ДанныеКорня"
        )
        seal = source.index('Контекст.Вставить("__OnecWorkerPrepared_')
        timer = timestamps[self.timer_index]
        if self.timer_index == 0:
            assert try_start < timer < create < catch_start < seal
        else:
            assert try_start < immutable_root < timer < catch_start < seal
        raise runtime_errors.BslExecutionError(
            "onec-worker-root-prepare-stage="
            f"{self.classified_phase}\nplanned timestamp failure"
        )


def _registry_live_state_snapshot(
    registry: worker_universe.WorkerUniverseRegistry,
) -> dict[str, object]:
    return {
        "state": registry._state,
        "next_generation": registry._next_generation,
        "pending": id(registry._pending),
        "active_generation": registry._active_generation,
        "handles_container": id(registry._handles),
        "handles": tuple(
            sorted(
                (generation, id(handle))
                for generation, handle in registry._handles.items()
            )
        ),
        "generations_container": id(registry._generations),
        "generations": tuple(
            sorted(
                (
                    generation,
                    id(record),
                    id(record.handle),
                    id(record.manifest),
                    record.root_key,
                    record.active,
                    record.explicitly_retained,
                    record.operation_pins,
                )
                for generation, record in registry._generations.items()
            )
        ),
        "leases_container": id(registry._leases),
        "leases": tuple(
            sorted(
                (
                    id(lease_id),
                    id(lease),
                    id(lease.pin),
                    lease.outcome_unknown,
                )
                for lease_id, lease in registry._leases.items()
            )
        ),
        "refcounts_container": id(registry._registration_refcounts),
        "refcounts": tuple(sorted(registry._registration_refcounts.items())),
        "owners_container": id(registry._registration_artifacts),
        "owners": tuple(sorted(registry._registration_artifacts.items())),
        "quarantine_container": id(registry._quarantine_holds),
        "quarantine": tuple(sorted(registry._quarantine_holds)),
    }


def test_server_registry_builds_exact_new_artifact_stages(tmp_path: Path) -> None:
    a17, b17, _, _ = _generation_artifacts(tmp_path)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    candidate = host.prepare((a17, b17))
    server_registry = worker_universe.ServerWorkerUniverseRegistry(
        host,
        lambda source: True,
    )

    stages = server_registry.stages(candidate)

    assert [(stage.artifact_sha256, stage.phase) for stage in stages] == [
        (artifact.artifact_sha256, "connect")
        for artifact in candidate.new_artifacts
    ]
    assert all(
        "RuntimeWorkerActiveGeneration" not in stage.instruction
        for stage in stages
    )


def test_prepare_validates_candidate_once_and_seals_exact_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prepare is the sole full descriptor-validation boundary for staging."""
    a17, b17, _, _ = _generation_artifacts(tmp_path)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    calls = 0
    original = worker_universe._validated_worker_module_artifact

    def counted(artifact: WorkerModuleArtifact):
        nonlocal calls
        calls += 1
        return original(artifact)

    monkeypatch.setattr(worker_universe, "_validated_worker_module_artifact", counted)

    candidate = host.prepare((a17, b17))
    activation = host._require_sealed_activation(candidate)

    assert calls == 2
    assert activation.candidate is candidate
    assert tuple(entry.logical_name for entry in activation.stage_entries) == (
        "МодульА",
        "МодульБ",
    )
    assert all(
        entry.admitted_snapshot is view.snapshot
        for entry, view in zip(
            activation.stage_entries,
            activation.artifact_views,
            strict=True,
        )
    )
    assert tuple(entry.artifact_bytes for entry in activation.stage_entries) == tuple(
        worker_universe._validated_admitted_snapshot(
            artifact.worker_artifact
        ).artifact_bytes
        for artifact in candidate.artifacts
    )
    assert calls == 2


def test_prepare_reuses_each_admitted_snapshot_for_validation_and_sealing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A descriptor admission obtains its proven snapshot only once."""
    a17, b17, _, _ = _generation_artifacts(tmp_path)
    expected_snapshots = {
        artifact: server_worker._validated_admitted_snapshot(
            artifact.worker_artifact
        )
        for artifact in (a17, b17)
    }
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    calls: list[object] = []
    original = worker_universe._validated_admitted_snapshot

    def counted(artifact: object):
        calls.append(artifact)
        return original(artifact)

    monkeypatch.setattr(worker_universe, "_validated_admitted_snapshot", counted)
    monkeypatch.setattr(server_worker, "_validated_admitted_snapshot", counted)

    candidate = host.prepare((a17, b17))
    activation = host._require_sealed_activation(candidate)

    assert calls == [artifact.worker_artifact for artifact in candidate.artifacts]
    for view, stage in zip(
        activation.artifact_views,
        activation.stage_entries,
        strict=True,
    ):
        expected = expected_snapshots[view.descriptor]
        assert view.snapshot is expected
        assert stage.admitted_snapshot is expected
        assert stage.artifact_bytes is expected.artifact_bytes
        for value in (repr(view), repr(stage), repr(activation)):
            assert repr(expected) not in value
            assert expected.artifact_bytes[:32].hex() not in value


def test_sealed_candidate_diagnostics_share_admitted_snapshots_without_cloning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: candidate diagnostics must not clone sealed map/context state."""
    a17, b17, _, _ = _generation_artifacts(tmp_path)
    snapshots = tuple(
        server_worker._validated_admitted_snapshot(artifact.worker_artifact)
        for artifact in (a17, b17)
    )
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    diagnostic_descriptors: list[object] = []
    original_diagnostic_artifact = server_worker.WorkerDiagnosticArtifact

    def reject_clone(*_args: object, **_kwargs: object) -> object:
        pytest.fail("sealed candidate diagnostics cloned admitted state")

    def counted_diagnostic_artifact(*args: object, **kwargs: object):
        diagnostic = original_diagnostic_artifact(*args, **kwargs)
        diagnostic_descriptors.append(diagnostic)
        return diagnostic

    monkeypatch.setattr(server_worker, "_snapshot_mapped_source", reject_clone)
    monkeypatch.setattr(server_worker, "_clone_visible_context_snapshot", reject_clone)
    monkeypatch.setattr(
        server_worker,
        "WorkerDiagnosticArtifact",
        counted_diagnostic_artifact,
    )

    candidate = host.prepare((a17, b17))
    activation = host._require_sealed_activation(candidate)
    diagnostics = host._candidate_diagnostics(candidate)

    assert diagnostics is activation.diagnostics
    assert host._candidate_diagnostics(candidate) is diagnostics
    assert len(diagnostic_descriptors) == len(diagnostics)
    assert all(
        observed is diagnostic
        for observed, diagnostic in zip(
            diagnostic_descriptors,
            diagnostics,
            strict=True,
        )
    )
    assert tuple(item.artifact_sha256 for item in diagnostics) == tuple(
        view.descriptor.artifact_sha256 for view in activation.artifact_views
    )
    for diagnostic, snapshot in zip(diagnostics, snapshots, strict=True):
        assert diagnostic.mapped_source is snapshot.mapped_source
        assert snapshot.visible_context_snapshot is not None
        assert (
            diagnostic.visible_source_context
            is snapshot.visible_context_snapshot.context
        )
        with pytest.raises(AttributeError, match="immutable"):
            diagnostic.mapped_source._text = "tampered"  # type: ignore[attr-defined]
        with pytest.raises(AttributeError, match="immutable"):
            diagnostic.visible_source_context._indices = {}  # type: ignore[union-attr]

        private_values = (
            repr(snapshot),
            snapshot.source_bytes[:16].hex(),
            snapshot.artifact_bytes[:16].hex(),
            str(snapshot.source_path),
            str(snapshot.artifact_path),
        )
        assert all(value not in repr(diagnostic) for value in private_values)
        assert not any(
            hasattr(diagnostic, name)
            for name in (
                "admitted_snapshot",
                "artifact_bytes",
                "source_bytes",
                "source_path",
                "artifact_path",
                "visible_context_snapshot",
            )
        )


@pytest.mark.parametrize(
    "deleted_state",
    ("mapped_source", "visible_context", "nested_line_index"),
)
def test_sealed_candidate_diagnostics_reject_deletion_without_losing_exact_mapping(
    tmp_path: Path,
    deleted_state: str,
) -> None:
    """Shared diagnostic state stays admitted and exactly remappable after del."""
    a17, _, _, _ = _generation_artifacts(tmp_path)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    candidate = host.prepare((a17,))
    diagnostics = host._candidate_diagnostics(candidate)
    diagnostic = diagnostics[0]
    context = diagnostic.visible_source_context
    assert context is not None
    line_index = next(
        iter(context._indices.values())  # type: ignore[attr-defined]
    )[1]
    exact_segment = next(
        segment
        for segment in diagnostic.mapped_source.source_map.segments
        if segment.relation is MappingRelation.EXACT
        and segment.generated.start < segment.generated.end
    )
    line, column = LineIndex(diagnostic.mapped_source.text).offset_to_line_column(
        exact_segment.generated.start
    )
    parsed = server_worker.parse_platform_diagnostic(
        "{ВнешняяОбработка."
        f"{diagnostic.registration_name}.МодульОбъекта({line},{column})}}: "
        "immutable snapshot [ОшибкаКомпиляцииВстроенногоЯзыка]"
    )

    def exact_mapping() -> tuple[object, ...]:
        remapped = server_worker.remap_worker_stage_diagnostic(
            parsed,
            artifact_sha256=diagnostic.artifact_sha256,
            phase="connect",
            candidate_manifest_sha256=candidate.manifest.sha256,
            candidate_artifacts=diagnostics,
        )
        assert remapped.mapping_confidence.value == "exact"
        assert remapped.source_unit is not None
        assert remapped.visible_location is not None
        return (
            remapped.source_unit,
            remapped.visible_location,
            remapped.source_map_sha256,
            remapped.execution_artifact_sha256,
        )

    expected_mapping = exact_mapping()
    expected_exports = validate_worker_module_artifact(a17)

    with pytest.raises(AttributeError, match="immutable"):
        if deleted_state == "mapped_source":
            del diagnostic.mapped_source._text  # type: ignore[attr-defined]
        elif deleted_state == "visible_context":
            del context._indices  # type: ignore[attr-defined]
        else:
            del line_index._line_starts  # type: ignore[attr-defined]

    assert host._candidate_diagnostics(candidate) is diagnostics
    assert validate_worker_module_artifact(a17) == expected_exports
    assert exact_mapping() == expected_mapping


def test_candidate_diagnostics_require_exact_pending_identity_and_current_fence(
    tmp_path: Path,
) -> None:
    a17, b17, _, _ = _generation_artifacts(tmp_path)
    runtime_generation = [7]
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=lambda: runtime_generation[0],
        context_generation=3,
    )
    candidate = host.prepare((a17, b17))

    with pytest.raises(ProtocolError, match="candidate"):
        host._candidate_diagnostics(replace(candidate))

    diagnostics = host._candidate_diagnostics(candidate)
    runtime_generation[0] = 8
    with pytest.raises(ProtocolError, match="fence"):
        host._candidate_diagnostics(candidate)
    assert diagnostics is host._sealed_activations[
        candidate.handle.generation
    ].diagnostics


def test_sealing_rejects_snapshot_source_map_from_another_revision(
    tmp_path: Path,
) -> None:
    builder, _, _ = _builder(tmp_path)
    lowered_r1, context_r1 = _lowered(revision=1)
    lowered_r2, context_r2 = _lowered(revision=2)
    revision_1 = builder.build(lowered_r1, visible_source_context=context_r1)
    revision_2 = builder.build(lowered_r2, visible_source_context=context_r2)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    candidate = host.prepare((revision_1,))
    admitted = host._require_sealed_activation(candidate).artifact_views[0]
    foreign_snapshot = server_worker._validated_admitted_snapshot(
        revision_2.worker_artifact
    )

    with pytest.raises(ProtocolError, match="sealed activation"):
        worker_universe._seal_worker_activation(
            candidate,
            (replace(admitted, snapshot=foreign_snapshot),),
        )


def test_sealed_activation_requires_exact_pending_candidate_identity(
    tmp_path: Path,
) -> None:
    a17, b17, _, _ = _generation_artifacts(tmp_path)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    candidate = host.prepare((a17, b17))
    same_fields_different_object = replace(candidate)

    with pytest.raises(ProtocolError, match="candidate"):
        host._require_sealed_activation(same_fields_different_object)

    assert host._require_sealed_activation(candidate).candidate is candidate


def test_sealed_activation_rejects_stale_runtime_context_fence(tmp_path: Path) -> None:
    a17, b17, _, _ = _generation_artifacts(tmp_path)
    runtime_generation = [7]
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=lambda: runtime_generation[0],
        context_generation=3,
    )
    candidate = host.prepare((a17, b17))
    runtime_generation[0] = 8

    with pytest.raises(ProtocolError, match="fence"):
        host._require_sealed_activation(candidate)


def test_prepare_rejects_descriptor_forged_after_build_before_sealing(
    tmp_path: Path,
) -> None:
    a17, _, _, _ = _generation_artifacts(tmp_path)
    object.__setattr__(a17, "source_sha256", "f" * 64)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )

    with pytest.raises(ProtocolError, match="module artifact admission"):
        host.prepare((a17,))

    assert host._pending is None
    assert host._sealed_activations == {}


def test_discard_removes_sealed_activation_for_ordinary_candidate(tmp_path: Path) -> None:
    a17, b17, _, _ = _generation_artifacts(tmp_path)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    candidate = host.prepare((a17, b17))
    diagnostics = host._candidate_diagnostics(candidate)
    assert candidate.handle.generation in host._sealed_activations

    host.discard(candidate)

    assert host.state is worker_universe.WorkerUniverseState.EMPTY
    assert host._sealed_activations == {}
    with pytest.raises(ProtocolError, match="candidate|stale"):
        host._require_sealed_activation(candidate)
    with pytest.raises(ProtocolError, match="candidate|stale"):
        host._candidate_diagnostics(candidate)
    assert len(diagnostics) == 2


def test_sealed_activations_follow_empty_ready_broken_and_closed_lifecycles(
    tmp_path: Path,
) -> None:
    a17, b17, b18, _ = _generation_artifacts(tmp_path)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )

    assert host.state is worker_universe.WorkerUniverseState.EMPTY
    assert host._sealed_activations == {}
    active = host.prepare((a17, b17))
    assert set(host._sealed_activations) == {active.handle.generation}
    host.confirm(active, _acknowledged_receipt(active))

    assert host.state is worker_universe.WorkerUniverseState.READY
    assert host._sealed_activations == {}
    pending = host.prepare((a17, b18))
    diagnostics = host._candidate_diagnostics(pending)
    host.mark_broken(pending)

    assert host.state is worker_universe.WorkerUniverseState.BROKEN
    assert set(host._sealed_activations) == {pending.handle.generation}
    assert host._sealed_activations[pending.handle.generation].diagnostics is diagnostics
    host.teardown()

    assert host.state is worker_universe.WorkerUniverseState.CLOSED
    assert host._sealed_activations == {}


def test_prepare_root_does_not_publish_and_swap_guards_previous_root(
    tmp_path: Path,
) -> None:
    a17, b17, _, _ = _generation_artifacts(tmp_path)
    candidate = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    ).prepare((a17, b17))

    transaction_id = UUID("11111111-2222-3333-4444-555555555555")
    source = worker_universe.prepare_worker_root_instruction(
        candidate,
        transaction_id,
        "generation-6",
    )


def _prepared_instruction(candidate) -> str:
    previous = (
        ""
        if candidate.previous is None
        else f"generation-{candidate.previous.generation}"
    )
    return worker_universe.prepare_worker_root_instruction(
        candidate,
        UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
        previous,
    )

    assert 'Контекст.Вставить("RuntimeWorkerActiveGeneration"' not in source
    assert f"__OnecWorkerPrepared_{transaction_id.hex}" in source
    assert source.rindex("ВнешниеОбработки.Создать") < source.index(
        "__OnecDependency_"
    )
    prepared = worker_universe.WorkerPreparedRootReceipt(
        transaction_id,
        candidate.handle.generation,
        candidate.manifest.sha256,
        f"generation-{candidate.handle.generation}",
        "generation-6",
        13,
    )
    swap = worker_universe.swap_worker_root_instruction(prepared)

    assert swap.count('Контекст.Вставить("RuntimeWorkerActiveGeneration"') == 1
    assert 'ТекущийКореньWorker.RootKey <> "generation-6"' in swap
    assert swap.index("previous root identity mismatch") < swap.index(
        'Контекст.Вставить("RuntimeWorkerActiveGeneration"'
    )


def test_prepare_and_swap_instructions_time_separate_phases(
    tmp_path: Path,
) -> None:
    a17, b17, _, _ = _generation_artifacts(tmp_path)
    candidate = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    ).prepare((a17, b17))

    source = _prepared_instruction(candidate)
    prepared = worker_universe.WorkerPreparedRootReceipt(
        UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
        candidate.handle.generation,
        candidate.manifest.sha256,
        "generation-1",
        "",
        13,
    )
    swap_source = worker_universe.swap_worker_root_instruction(prepared)

    timestamp = "ТекущаяУниверсальнаяДатаВМиллисекундах()"
    timestamps = tuple(
        match.start() for match in re.finditer(re.escape(timestamp), source)
    )
    create = source.index("Новый Соответствие")
    immutable_root = source.index("Новый ФиксированнаяСтруктура(ДанныеКорня")
    seal = source.index('Контекст.Вставить("__OnecWorkerPrepared_')
    result = source.index(
        'Результат = "onec-worker-prepared-root-receipt-v1|'
    )
    assert len(timestamps) == 2
    assert timestamps[0] < create
    assert immutable_root < timestamps[1] < seal < result
    assert 'Контекст.Вставить("RuntimeWorkerActiveGeneration"' not in source
    assert swap_source.count(
        'Контекст.Вставить("RuntimeWorkerActiveGeneration"'
    ) == 1
    assert swap_source.count("ТекущаяУниверсальнаяДатаВМиллисекундах()") == 2


@pytest.mark.parametrize(
    ("timer_index", "classified_phase"),
    ((0, "create"), (1, "probe")),
)
def test_pre_swap_timestamp_failure_is_classified_and_keeps_connected_artifacts(
    tmp_path: Path,
    timer_index: int,
    classified_phase: str,
) -> None:
    """Break caught: telemetry before the swap must remain a known failure."""
    a17, b17, _, _ = _generation_artifacts(tmp_path)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    candidate = host.prepare((a17, b17))
    target = _PreSwapTimestampFailureExecutor(timer_index, classified_phase)
    server = worker_universe.ServerWorkerUniverseRegistry(host, target)

    with pytest.raises(
        runtime_errors.BslExecutionError,
        match=rf"root-prepare-stage={classified_phase}",
    ):
        server.promote(candidate)

    assert host.state is worker_universe.WorkerUniverseState.EMPTY
    assert target.registrations == set(server._registrations)
    assert target.registrations
    assert target.disconnects == []
    assert server._broken is False


def test_promotion_receipt_rejects_negative_target_timings() -> None:
    with pytest.raises(ProtocolError, match="promotion receipt is invalid"):
        worker_universe._worker_promotion_receipt(
            {
                "generation": 17,
                "manifest_sha256": "a" * 64,
                "root_key": "RuntimeWorkerGeneration17",
                "acknowledged": True,
                "generation_create_wire_probe_ms": -1,
                "root_swap_ms": 0,
            }
        )


def test_promotion_receipt_accepts_exact_real_transport_scalar() -> None:
    transaction_id = UUID("11111111-2222-3333-4444-555555555555")
    receipt = worker_universe._worker_promotion_receipt(
        f"onec-worker-root-swap-receipt-v1|{transaction_id}|17|"
        + "a" * 64
        + "|generation-17|generation-16|1|13|2"
    )

    assert receipt == worker_universe.WorkerPromotionReceipt(
        transaction_id=transaction_id,
        generation=17,
        manifest_sha256="a" * 64,
        root_key="generation-17",
        previous_root_key="generation-16",
        acknowledged=True,
        generation_create_wire_probe_ms=13,
        root_swap_ms=2,
    )


@pytest.mark.parametrize(
    "value",
    (
        "",
        "onec-worker-universe-promotion-receipt-v1|17|"
        + "a" * 64
        + "|generation-17|1|13|2|extra",
        "onec-worker-universe-promotion-receipt-v1||"
        + "a" * 64
        + "|generation-17|1|13|2",
        "onec-worker-universe-promotion-receipt-v1|\u0661\u0667|"
        + "a" * 64
        + "|generation-17|1|13|2",
        "onec-worker-universe-promotion-receipt-v1|17|bad-sha|"
        "generation-17|1|13|2",
        "onec-worker-universe-promotion-receipt-v1|17|"
        + "a" * 64
        + "|generation-17|true|13|2",
        "onec-worker-universe-promotion-receipt-v1|17|"
        + "a" * 64
        + "|generation-17|1|-1|2",
        "onec-worker-universe-promotion-receipt-v1|17|"
        + "a" * 64
        + "|generation-17|1|13|2.5",
    ),
)
def test_promotion_receipt_rejects_noncanonical_scalar(value: str) -> None:
    with pytest.raises(ProtocolError, match="promotion receipt is invalid"):
        worker_universe._worker_promotion_receipt(value)


def test_promotion_receipt_identity_rejects_wire_delimiter() -> None:
    with pytest.raises(ValueError, match="promotion receipt is invalid"):
        worker_universe.WorkerPromotionReceipt(
            transaction_id=UUID(int=1),
            generation=17,
            manifest_sha256="a" * 64,
            root_key="generation|17",
            previous_root_key="generation-16",
            acknowledged=True,
            generation_create_wire_probe_ms=13,
            root_swap_ms=2,
        )


def test_server_promotion_records_only_authenticated_target_timings(
    tmp_path: Path,
) -> None:
    a17, b17, _, _ = _generation_artifacts(tmp_path)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    candidate = host.prepare((a17, b17))
    target = _UniverseTargetExecutor()
    target.acknowledge(candidate)
    profiler = PhaseRecorder()

    worker_universe.ServerWorkerUniverseRegistry(host, target).promote(
        candidate,
        profiler=profiler,
    )

    assert [event.phase for event in profiler.events] == [
        "artifact_stage_sealed_validation",
        "artifact_stage_base64",
        "artifact_stage_executor",
        "artifact_stage_batch",
        "artifact_staging",
        "generation_create_wire_probe",
        "root_swap",
    ]
    staging_events = {
        event.phase: event
        for event in profiler.events
        if event.phase.startswith("artifact_stage")
    }
    assert set(staging_events) == {
        "artifact_stage_sealed_validation",
        "artifact_stage_base64",
        "artifact_stage_executor",
        "artifact_stage_batch",
    }
    assert all(event.item_count == 2 for event in staging_events.values())
    assert all(event.wall_ns >= 0 for event in staging_events.values())
    assert target.stage_batch_calls == 1
    assert [event.wall_ns for event in profiler.events[-2:]] == [
        13_000_000,
        2_000_000,
    ]


def test_server_promotion_stages_then_confirms_and_returns_only_handle(
    tmp_path: Path,
) -> None:
    a17, b17, _, _ = _generation_artifacts(tmp_path)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    candidate = host.prepare((a17, b17))
    target = _UniverseTargetExecutor()
    target.acknowledge(candidate)
    server_registry = worker_universe.ServerWorkerUniverseRegistry(host, target)

    handle = server_registry.promote(candidate)

    assert handle is candidate.handle
    assert host.active_handle is candidate.handle
    assert target.active_manifest_sha256 == candidate.manifest.sha256
    assert target.registrations == {
        module.registration_name for module in candidate.manifest.modules
    }
    assert all(
        "RuntimeWorkerActiveGeneration" not in source
        for source in target.calls[:-1]
    )
    assert target.calls[-1].count(
        'Контекст.Вставить("RuntimeWorkerActiveGeneration", '
        "ПодготовленныйКореньWorker.Root);"
    ) == 1


def test_batch_promotion_stages_two_fresh_artifacts_in_one_target_call(
    tmp_path: Path,
) -> None:
    a17, b17, _, _ = _generation_artifacts(tmp_path)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    candidate = host.prepare((a17, b17))
    target = _UniverseTargetExecutor()
    target.acknowledge(candidate)
    server_registry = worker_universe.ServerWorkerUniverseRegistry(host, target)

    handle = server_registry.promote(candidate)

    assert handle is candidate.handle
    assert target.stage_batch_calls == 1
    assert len(target.registrations) == 2
    assert sum(
        "RuntimeWorkerActiveGeneration" in call for call in target.calls
    ) == 1


def test_registration_record_retains_exact_url_and_mints_module_location(
    tmp_path: Path,
) -> None:
    a17, _b17, _a18, _b18 = _generation_artifacts(tmp_path)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    candidate = host.prepare((a17,))
    target = _UniverseTargetExecutor()
    target.acknowledge(candidate)
    registry = worker_universe.ServerWorkerUniverseRegistry(
        host,
        target,
        target_incarnation_id=UUID(int=2),
        platform_build="8.3.27.2170",
    )

    registry.promote(candidate)

    module = candidate.manifest.modules[0]
    record = registry._registration_view(module.registration_name)
    location = record.module_location(17)
    assert location.module_type == "ExtMDModule"
    assert location.url == target.registration_urls[module.registration_name]
    assert location.object_id == UUID("2a00a4fa-8ea9-4dc4-9de1-472044c40101")
    assert location.property_id == UUID("a637f77f-3840-441d-a1c3-699c8c5cb7e0")
    assert location.line == 17
    assert location.extension_name == ""
    assert location.ext_id == 0
    assert location.url not in repr(record)


def test_registration_record_reuses_exact_url_without_reupload(
    tmp_path: Path,
) -> None:
    a17, b17, _a18, _b18 = _generation_artifacts(tmp_path)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    target = _UniverseTargetExecutor()
    registry = worker_universe.ServerWorkerUniverseRegistry(host, target)
    first = host.prepare((a17, b17))
    target.acknowledge(first)
    first_handle = registry.promote(first)
    module = first.manifest.modules[0]
    record = registry._registration_view(module.registration_name)
    calls = target.stage_batch_calls
    registry.release(first_handle)

    second = host.prepare((a17, b17))
    target.acknowledge(second)
    registry.promote(second)

    assert target.stage_batch_calls == calls
    assert registry._registration_view(module.registration_name) is record
    assert record.exact_temp_storage_url == target.registration_urls[
        module.registration_name
    ]


def test_registration_record_rejects_unsupported_platform_locator(
    tmp_path: Path,
) -> None:
    a17, _b17, _a18, _b18 = _generation_artifacts(tmp_path)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    candidate = host.prepare((a17,))
    target = _UniverseTargetExecutor()
    target.acknowledge(candidate)
    registry = worker_universe.ServerWorkerUniverseRegistry(
        host,
        target,
        platform_build="8.3.28.1",
    )
    registry.promote(candidate)
    record = registry._registration_view(
        candidate.manifest.modules[0].registration_name
    )

    with pytest.raises(ProtocolError, match="unsupported"):
        record.module_location(1)


def test_changed_temp_storage_session_quarantines_target(
    tmp_path: Path,
) -> None:
    a17, b17, b18, _original = _generation_artifacts(tmp_path)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    target = _UniverseTargetExecutor()
    registry = worker_universe.ServerWorkerUniverseRegistry(host, target)
    first = host.prepare((a17, b17))
    target.acknowledge(first)
    registry.promote(first)
    target.storage_session_id = "different-target-session"
    candidate = host.prepare((a17, b18))
    target.acknowledge(candidate)

    with pytest.raises(runtime_errors.WorkerPromotionOutcomeUnknown):
        registry.promote(candidate)

    assert registry._broken is True
    assert host.state is worker_universe.WorkerUniverseState.BROKEN


def test_confirmed_generation_retains_target_bound_debug_view(
    tmp_path: Path,
) -> None:
    a17, b17, _b18, _original = _generation_artifacts(tmp_path)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    candidate = host.prepare((a17, b17))
    target = _UniverseTargetExecutor()
    target.acknowledge(candidate)
    registry = worker_universe.ServerWorkerUniverseRegistry(host, target)
    handle = registry.promote(candidate)
    pin = host.pin_active()

    view = host._operation_debug_view(pin)

    assert view.handle is handle
    assert view.manifest is candidate.manifest
    assert tuple(module.canonical_module for module in view.modules) == tuple(
        item.logical_name.casefold() for item in candidate.manifest.modules
    )
    for module in view.modules:
        assert module.registration is registry._registration_view(
            module.registration.registration_name
        )
        assert module.mapped_source.source_map_sha256 == module.source_map_sha256
        assert module.source_unit.revision in {17}


def test_old_generation_debug_view_lives_until_last_pin_release(
    tmp_path: Path,
) -> None:
    a17, b17, b18, _original = _generation_artifacts(tmp_path)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    target = _UniverseTargetExecutor()
    registry = worker_universe.ServerWorkerUniverseRegistry(host, target)
    first = host.prepare((a17, b17))
    target.acknowledge(first)
    first_handle = registry.promote(first)
    pin = host.pin_active()
    first_view = host._operation_debug_view(pin)
    second = host.prepare((a17, b18))
    target.acknowledge(second)
    registry.promote(second)
    registry.release(first_handle)

    assert host._operation_debug_view(pin) is first_view
    assert first_view in host._retained_debug_views()

    registry.release_pin(pin)
    with pytest.raises(ProtocolError, match="stale|released"):
        host._operation_debug_view(pin)


def test_batch_staging_transports_two_large_sealed_payloads_exactly() -> None:
    artifacts, payloads = _large_batch_generation_artifacts(
        2,
        payload_size=2 * 1024 * 1024,
    )
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    candidate = host.prepare(artifacts)
    activation = host._require_sealed_activation(candidate)
    target = _UniverseTargetExecutor()
    target.acknowledge(candidate)
    registry = worker_universe.ServerWorkerUniverseRegistry(host, target)

    batches = registry._stage_batches(candidate)
    registry.promote(candidate)

    assert len(batches) == 1
    assert all(
        batch_entry.artifact_bytes is sealed_entry.artifact_bytes
        for batch_entry, sealed_entry in zip(
            batches[0].entries,
            activation.stage_entries,
            strict=True,
        )
    )
    assert target.stage_batch_calls == 1
    assert target.stage_batch_payload_observations == [
        tuple(
            (len(payloads[module.logical_name]), module.artifact_sha256)
            for module in candidate.manifest.modules
        )
    ]
    stage_source = next(
        source
        for source in target.calls
        if '"onec-worker-stage-batch-receipt"' in source
    )
    PythonParserTarget.from_generated().parse(stage_source, "Модуль")
    for module in candidate.manifest.modules:
        encoded = base64.b64encode(payloads[module.logical_name]).decode("ascii")
        if encoded not in stage_source:
            pytest.fail("large sealed payload is absent from batch instruction")
        assert module.artifact_sha256 in stage_source
    sentinel = activation.stage_entries[0].artifact_bytes[:32].hex()
    assert sentinel not in repr(batches)
    assert sentinel not in repr(activation)
    with pytest.raises(ProtocolError) as captured:
        worker_stage_protocol.parse_worker_stage_batch_outcome(
            "invalid-large-batch-receipt",
            batches[0],
            batch_count=1,
            transaction_id=UUID(int=1),
        )
    assert sentinel not in str(captured.value)


def test_batch_staging_large_ten_entry_boundary_is_ordered_and_single_call() -> None:
    artifacts, payloads = _large_batch_generation_artifacts(
        10,
        payload_size=256 * 1024,
    )
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    candidate = host.prepare(artifacts)
    target = _UniverseTargetExecutor()
    target.acknowledge(candidate)

    worker_universe.ServerWorkerUniverseRegistry(host, target).promote(candidate)

    expected = tuple(
        (
            len(payloads[module.logical_name]),
            module.artifact_sha256,
        )
        for module in candidate.manifest.modules
    )
    assert target.stage_batch_calls == 1
    assert target.stage_batch_payload_observations == [expected]
    assert target.stage_batch_registrations == [
        tuple(module.registration_name for module in candidate.manifest.modules)
    ]


@pytest.mark.parametrize(
    ("artifact_count", "expected_batch_calls"),
    ((10, 1), (11, 2), (21, 3)),
)
def test_batch_staging_chunks_manifest_in_groups_of_ten(
    tmp_path: Path,
    artifact_count: int,
    expected_batch_calls: int,
) -> None:
    artifacts = _batch_generation_artifacts(tmp_path, artifact_count)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    candidate = host.prepare(artifacts)
    target = _UniverseTargetExecutor()
    target.acknowledge(candidate)

    worker_universe.ServerWorkerUniverseRegistry(host, target).promote(candidate)

    assert target.stage_batch_calls == expected_batch_calls
    assert tuple(
        registration
        for batch in target.stage_batch_registrations
        for registration in batch
    ) == tuple(module.registration_name for module in candidate.manifest.modules)


def test_batch_staging_reuses_cached_artifact_and_stages_only_new_content(
    tmp_path: Path,
) -> None:
    a17, b17, b18, _ = _generation_artifacts(tmp_path)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    target = _UniverseTargetExecutor()
    registry = worker_universe.ServerWorkerUniverseRegistry(host, target)
    first = host.prepare((a17, b17))
    target.acknowledge(first)
    registry.promote(first)
    target.stage_batch_calls = 0
    target.stage_batch_registrations.clear()
    second = host.prepare((a17, b18))
    target.acknowledge(second)

    registry.promote(second)

    assert target.stage_batch_calls == 1
    assert target.stage_batch_registrations == [
        tuple(
            module.registration_name
            for module in second.manifest.modules
            if module.artifact_sha256 == b18.artifact_sha256
        )
    ]


def test_batch_staging_all_cached_candidate_skips_target_staging_call(
    tmp_path: Path,
) -> None:
    a17, b17, _, _ = _generation_artifacts(tmp_path)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    target = _UniverseTargetExecutor()
    registry = worker_universe.ServerWorkerUniverseRegistry(host, target)
    first = host.prepare((a17, b17))
    target.acknowledge(first)
    registry.promote(first)
    target.stage_batch_calls = 0
    second = host.prepare((a17, b17))
    target.acknowledge(second)

    registry.promote(second)

    assert target.stage_batch_calls == 0


def test_target_cached_host_new_candidate_has_no_staging_profile_events(
    tmp_path: Path,
) -> None:
    artifacts = _batch_generation_artifacts(tmp_path, 2)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    target = _UniverseTargetExecutor()
    registry = worker_universe.ServerWorkerUniverseRegistry(host, target)
    first = host.prepare((artifacts[0],))
    target.acknowledge(first)
    first_handle = registry.promote(first)
    parked = host.prepare((artifacts[1],))
    target.acknowledge(parked)
    registry.promote(parked)
    registry.release(first_handle)
    reused = host.prepare((artifacts[0],))
    target.acknowledge(reused)
    profiler = PhaseRecorder()
    stage_calls_before = target.stage_batch_calls

    handle = registry.promote(reused, profiler=profiler)

    assert reused.new_artifacts == (artifacts[0],)
    assert handle is reused.handle
    assert target.stage_batch_calls == stage_calls_before
    assert [event.phase for event in profiler.events] == [
        "artifact_stage_sealed_validation",
        "generation_create_wire_probe",
        "root_swap",
    ]


def test_mixed_target_cached_and_new_candidate_profiles_exact_staged_count(
    tmp_path: Path,
) -> None:
    artifacts = _batch_generation_artifacts(tmp_path, 3)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    target = _UniverseTargetExecutor()
    registry = worker_universe.ServerWorkerUniverseRegistry(host, target)
    first = host.prepare((artifacts[0],))
    target.acknowledge(first)
    first_handle = registry.promote(first)
    parked = host.prepare((artifacts[1],))
    target.acknowledge(parked)
    registry.promote(parked)
    registry.release(first_handle)
    candidate = host.prepare((artifacts[0], artifacts[2]))
    target.acknowledge(candidate)
    profiler = PhaseRecorder()
    stage_calls_before = target.stage_batch_calls

    handle = registry.promote(candidate, profiler=profiler)

    assert candidate.new_artifacts == (artifacts[0], artifacts[2])
    assert handle is candidate.handle
    assert target.stage_batch_calls == stage_calls_before + 1
    assert target.stage_batch_registrations[-1] == (
        worker_universe.registration_name(
            artifacts[2].logical_name,
            artifacts[2].artifact_sha256,
        ),
    )
    item_counts = {
        event.phase: event.item_count
        for event in profiler.events
        if event.phase in {
            "artifact_staging",
            "artifact_stage_base64",
            "artifact_stage_executor",
            "artifact_stage_batch",
        }
    }
    assert item_counts == {
        "artifact_stage_base64": 1,
        "artifact_stage_executor": 1,
        "artifact_stage_batch": 1,
        "artifact_staging": 1,
    }


@pytest.mark.parametrize(
    ("item_index", "confirmed_before", "phase"),
    ((0, 0, "decode"), (1, 1, "upload")),
)
def test_batch_staging_known_failure_confirms_only_preceding_items(
    tmp_path: Path,
    item_index: int,
    confirmed_before: int,
    phase: str,
) -> None:
    active_path = tmp_path / "active"
    candidate_path = tmp_path / "candidate"
    active_path.mkdir()
    candidate_path.mkdir()
    a17, b17, _, _ = _generation_artifacts(active_path)
    artifacts = _batch_generation_artifacts(candidate_path, 3)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    previous = host.prepare((a17, b17))
    host.confirm(previous, _acknowledged_receipt(previous))
    candidate = host.prepare(artifacts)
    diagnostics = _candidate_diagnostic_artifacts(candidate)
    target = _UniverseTargetExecutor()
    target.fault = phase
    target.stage_failure_item_index = item_index
    registry = worker_universe.ServerWorkerUniverseRegistry(host, target)

    with pytest.raises(runtime_errors.BslExecutionError) as captured:
        registry.promote(candidate)

    assert phase in str(captured.value)
    assert len(registry._registrations) == confirmed_before
    assert tuple(registry._registrations) == tuple(
        module.registration_name
        for module in candidate.manifest.modules[:confirmed_before]
    )
    assert host.active_handle is previous.handle
    assert host.state is worker_universe.WorkerUniverseState.READY
    assert registry._broken is False
    assert registry._candidate_registrations == {}
    remapped = server_worker.remap_worker_artifact_stage_error(
        captured.value,
        candidate_manifest_sha256=candidate.manifest.sha256,
        candidate_artifacts=diagnostics,
    )
    assert remapped.diagnostic is not None
    assert remapped.diagnostic.source_unit is not None
    assert (
        remapped.diagnostic.source_unit.unit_id
        == candidate.manifest.modules[item_index].logical_name
    )


def test_batch_known_failure_maps_item_one_with_identical_artifact_sha() -> None:
    shared_payload = b"same-epf-binary" * 256
    artifacts, _payloads = _large_batch_generation_artifacts(
        2,
        payload_size=len(shared_payload),
        shared_payload=shared_payload,
    )
    assert artifacts[0].artifact_sha256 == artifacts[1].artifact_sha256
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    candidate = host.prepare(artifacts)
    diagnostics = _candidate_diagnostic_artifacts(candidate)
    target = _UniverseTargetExecutor()
    target.fault = "upload"
    target.stage_failure_item_index = 1
    registry = worker_universe.ServerWorkerUniverseRegistry(host, target)

    with pytest.raises(runtime_errors.BslExecutionError) as captured:
        registry.promote(candidate)

    remapped = server_worker.remap_worker_artifact_stage_error(
        captured.value,
        candidate_manifest_sha256=candidate.manifest.sha256,
        candidate_artifacts=diagnostics,
    )
    assert remapped.diagnostic is not None
    assert remapped.diagnostic.mapping_confidence.value == "exact"
    assert remapped.diagnostic.source_unit is not None
    assert remapped.diagnostic.source_unit.unit_id == (
        candidate.manifest.modules[1].logical_name
    )


def test_second_batch_second_entry_connect_failure_keeps_exact_diagnostic_mapping(
    tmp_path: Path,
) -> None:
    artifacts = _batch_generation_artifacts(tmp_path, 12)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    candidate = host.prepare(artifacts)
    diagnostics = host._candidate_diagnostics(candidate)
    target = _UniverseTargetExecutor()
    target.fault = "upload"
    target.stage_failure_batch_index = 1
    target.stage_failure_item_index = 1
    registry = worker_universe.ServerWorkerUniverseRegistry(host, target)

    with pytest.raises(runtime_errors.BslExecutionError) as captured:
        registry.promote(candidate)

    expected_module = candidate.manifest.modules[11]
    expected_diagnostic = diagnostics[11]
    remapped = server_worker.remap_worker_artifact_stage_error(
        captured.value,
        candidate_manifest_sha256=candidate.manifest.sha256,
        candidate_artifacts=diagnostics,
    )

    assert target.stage_batch_calls == 2
    assert len(registry._registrations) == 11
    assert remapped.diagnostic is not None
    assert remapped.diagnostic.mapping_confidence.value == "exact"
    assert remapped.diagnostic.source_unit is not None
    assert (
        remapped.diagnostic.source_unit.unit_id,
        remapped.diagnostic.source_unit.revision,
    ) == (expected_module.logical_name, expected_module.revision)
    assert remapped.diagnostic.visible_location is not None
    assert (
        remapped.diagnostic.visible_location.line,
        remapped.diagnostic.visible_location.column,
    ) == (1, 1)
    assert (
        remapped.diagnostic.execution_artifact_sha256
        == expected_diagnostic.mapped_source.artifact.source_sha256
    )
    assert (
        expected_diagnostic.artifact_sha256
        == expected_module.artifact_sha256
    )
    assert (
        remapped.diagnostic.source_map_sha256
        == expected_diagnostic.source_map_sha256
    )


def test_known_failure_does_not_attach_private_identity_to_exception() -> None:
    shared_payload = b"same-private-epf-binary" * 256
    artifacts, _payloads = _large_batch_generation_artifacts(
        2,
        payload_size=len(shared_payload),
        shared_payload=shared_payload,
    )
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    candidate = host.prepare(artifacts)
    target = _UniverseTargetExecutor()
    target.fault = "upload"
    target.stage_failure_item_index = 1
    registry = worker_universe.ServerWorkerUniverseRegistry(host, target)

    with pytest.raises(runtime_errors.BslExecutionError) as captured:
        registry.promote(candidate)

    error = captured.value
    serialized = pickle.dumps(error)
    failed_logical_name = candidate.manifest.modules[1].logical_name
    assert vars(error) == {}
    assert failed_logical_name.encode("utf-8") not in serialized
    assert b"_onec_authenticated_worker_stage_identity" not in serialized
    assert not hasattr(server_worker, "_AuthenticatedWorkerStageIdentity")
    assert not hasattr(server_worker, "_bind_authenticated_worker_stage_identity")
    assert not hasattr(server_worker, "_authenticated_worker_stage_identity")
    assert not hasattr(server_worker, "_WORKER_STAGE_IDENTITY_PROOF")


@pytest.mark.parametrize(
    "fault",
    (
        "marker-mismatch",
        "connect",
        "connect-unacknowledged",
        "timeout",
        "receipt",
        "receipt-order",
        "receipt-identity",
    ),
)
def test_batch_staging_outcome_unknown_quarantines_without_confirming(
    tmp_path: Path,
    fault: str,
) -> None:
    artifacts = _batch_generation_artifacts(tmp_path, 3)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    candidate = host.prepare(artifacts)
    activation = host._require_sealed_activation(candidate)
    target = _UniverseTargetExecutor()
    target.fault = fault
    registry = worker_universe.ServerWorkerUniverseRegistry(host, target)

    with pytest.raises(runtime_errors.WorkerPromotionOutcomeUnknown) as captured:
        registry.promote(candidate)

    expected = {
        module.registration_name for module in candidate.manifest.modules
    }
    assert captured.value.generation == candidate.handle.generation
    assert "sensitive" not in str(captured.value)
    assert registry._registrations == {}
    assert registry._candidate_registrations == {
        candidate.handle.generation: expected
    }
    assert registry._broken is True
    assert host.state is worker_universe.WorkerUniverseState.BROKEN
    assert host._sealed_activations[candidate.handle.generation] is activation
    assert expected <= host._quarantine_holds
    assert target.disconnects == []

    registry.teardown()

    assert registry._candidate_registrations == {}
    assert host._sealed_activations == {}


def test_wrong_registration_in_known_failure_marker_is_outcome_unknown() -> None:
    artifacts, _payloads = _large_batch_generation_artifacts(
        2,
        payload_size=1024,
    )
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    candidate = host.prepare(artifacts)
    activation = host._require_sealed_activation(candidate)
    target = _UniverseTargetExecutor()
    target.fault = "registration-marker-mismatch"
    registry = worker_universe.ServerWorkerUniverseRegistry(host, target)

    with pytest.raises(runtime_errors.WorkerPromotionOutcomeUnknown):
        registry.promote(candidate)

    expected = {
        module.registration_name for module in candidate.manifest.modules
    }
    assert registry._registrations == {}
    assert registry._candidate_registrations == {
        candidate.handle.generation: expected
    }
    assert host.state is worker_universe.WorkerUniverseState.BROKEN
    assert host._sealed_activations[candidate.handle.generation] is activation
    assert expected <= host._quarantine_holds
    assert target.disconnects == []


def test_batch_registration_result_mismatch_is_outcome_unknown() -> None:
    artifacts, _payloads = _large_batch_generation_artifacts(
        2,
        payload_size=1024,
    )
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    candidate = host.prepare(artifacts)
    activation = host._require_sealed_activation(candidate)
    target = _UniverseTargetExecutor()
    target.fault = "registration-result-mismatch"
    registry = worker_universe.ServerWorkerUniverseRegistry(host, target)

    with pytest.raises(runtime_errors.WorkerPromotionOutcomeUnknown) as captured:
        registry.promote(candidate)

    expected = {
        module.registration_name for module in candidate.manifest.modules
    }
    assert "registration identity mismatch" not in str(captured.value)
    assert registry._registrations == {}
    assert registry._candidate_registrations == {
        candidate.handle.generation: expected
    }
    assert host.state is worker_universe.WorkerUniverseState.BROKEN
    assert host._sealed_activations[candidate.handle.generation] is activation
    assert expected <= host._quarantine_holds
    assert target.registrations == {
        "OnecRuntime_untracked_ffffffffffffffff"
    }
    assert target.disconnects == []
    assert len(target.calls) == 1

    registry.teardown()

    assert registry._candidate_registrations == {}
    assert host._sealed_activations == {}


def test_promotion_outcome_unknown_exposes_only_generation_identity() -> None:
    error = runtime_errors.WorkerPromotionOutcomeUnknown(17, "a" * 64)

    assert error.generation == 17
    assert error.manifest_sha256 == "a" * 64
    assert "source" not in repr(error).casefold()
    assert "path" not in repr(error).casefold()


def test_stages_reuse_only_complete_owned_registration_identity(
    tmp_path: Path,
) -> None:
    a17, b17, _, _ = _generation_artifacts(tmp_path)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    target = _UniverseTargetExecutor()
    server_registry = worker_universe.ServerWorkerUniverseRegistry(host, target)
    first = host.prepare((a17, b17))
    target.acknowledge(first)
    first_handle = server_registry.promote(first)
    second = host.prepare((a17, b17))

    assert second.new_artifacts == ()
    assert server_registry.stages(second) == ()

    missing = second.manifest.modules[0].registration_name
    server_registry._registrations.pop(missing)
    with pytest.raises(ProtocolError, match="ownership"):
        server_registry.stages(second)

    host.discard(second)
    host.release_generation(first_handle)


def test_stages_reject_corrupt_registration_owner_instead_of_hash_only_reuse(
    tmp_path: Path,
) -> None:
    a17, b17, _, _ = _generation_artifacts(tmp_path)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    target = _UniverseTargetExecutor()
    server_registry = worker_universe.ServerWorkerUniverseRegistry(host, target)
    first = host.prepare((a17, b17))
    target.acknowledge(first)
    server_registry.promote(first)
    second = host.prepare((a17, b17))
    module = second.manifest.modules[0]
    server_registry._registrations[module.registration_name] = (
        "другоймодуль",
        module.artifact_sha256,
    )

    with pytest.raises(ProtocolError, match="ownership"):
        server_registry.stages(second)

    assert host.active_handle is first.handle


def test_pre_target_registration_collision_aborts_candidate_and_keeps_registry_reusable(
    tmp_path: Path,
) -> None:
    builder, _, _ = _builder(tmp_path)

    def build(logical_name: str) -> WorkerModuleArtifact:
        lowered, context = _lowered(logical_name=logical_name)
        return builder.build(lowered, visible_source_context=context)

    left = build("Module118")
    right = build("Module88684")
    parking = build("ParkingModule")
    registration = worker_universe.registration_name(
        left.logical_name,
        left.artifact_sha256,
    )
    assert left.artifact_sha256 == right.artifact_sha256
    assert registration == worker_universe.registration_name(
        right.logical_name,
        right.artifact_sha256,
    )
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    target = _UniverseTargetExecutor()
    registry = worker_universe.ServerWorkerUniverseRegistry(host, target)
    first = host.prepare((left,))
    target.acknowledge(first)
    first_handle = registry.promote(first)
    parked = host.prepare((parking,))
    target.acknowledge(parked)
    parked_handle = registry.promote(parked)
    registry.release(first_handle)
    assert host.registration_refcount(registration) == 0
    retained = registry._registrations[registration]
    assert (retained.canonical_module, retained.artifact_sha256) == (
        left.logical_name.casefold(),
        left.artifact_sha256,
    )
    candidate = host.prepare((right,))
    target.acknowledge(candidate)
    calls_before = tuple(target.calls)

    with pytest.raises(
        ProtocolError,
        match="Worker target registration ownership is invalid",
    ):
        registry.promote(candidate)

    assert tuple(target.calls) == calls_before
    assert host.state is worker_universe.WorkerUniverseState.READY
    assert host.active_handle is parked_handle
    assert host.active_manifest is parked.manifest
    assert host._pending is None
    assert candidate.handle.generation not in host._sealed_activations
    assert registry._candidate_registrations == {}
    assert registry._broken is False
    reused = host.prepare((left,))
    target.acknowledge(reused)
    assert registry.promote(reused) is reused.handle
    assert host.active_handle is reused.handle


def test_replayed_promoted_candidate_is_rejected_without_registry_cleanup(
    tmp_path: Path,
) -> None:
    a17, b17, _, _ = _generation_artifacts(tmp_path)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    target = _UniverseTargetExecutor()
    registry = worker_universe.ServerWorkerUniverseRegistry(host, target)
    candidate = host.prepare((a17, b17))
    target.acknowledge(candidate)
    active_handle = registry.promote(candidate)
    active_manifest_sha256 = target.active_manifest_sha256
    target.calls.clear()
    profiler = PhaseRecorder()

    with pytest.raises(ProtocolError) as captured:
        registry.promote(candidate, profiler=profiler)

    assert type(captured.value) is ProtocolError
    assert captured.value.args == ("Worker universe candidate is stale or forged",)
    assert target.calls == []
    assert host.state is worker_universe.WorkerUniverseState.READY
    assert host.active_handle is active_handle
    assert target.active_manifest_sha256 == active_manifest_sha256
    assert registry._broken is False
    assert registry._candidate_registrations == {}
    assert [(event.phase, event.error_present) for event in profiler.events] == [
        ("artifact_stage_sealed_validation", True),
    ]
    retry = host.prepare((a17, b17))
    target.acknowledge(retry)
    assert registry.promote(retry) is retry.handle


def test_equal_foreign_candidate_is_rejected_without_discarding_pending(
    tmp_path: Path,
) -> None:
    a17, b17, b18, _ = _generation_artifacts(tmp_path)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    target = _UniverseTargetExecutor()
    registry = worker_universe.ServerWorkerUniverseRegistry(host, target)
    active = host.prepare((a17, b17))
    target.acknowledge(active)
    active_handle = registry.promote(active)
    pending = host.prepare((a17, b18))
    forged = replace(pending)
    target.acknowledge(pending)
    target.calls.clear()

    with pytest.raises(ProtocolError) as captured:
        registry.promote(forged)

    assert type(captured.value) is ProtocolError
    assert captured.value.args == ("Worker universe candidate is stale or forged",)
    assert target.calls == []
    assert host.state is worker_universe.WorkerUniverseState.PREPARING
    assert host.active_handle is active_handle
    assert host._pending is pending
    assert registry._broken is False
    assert registry.promote(pending) is pending.handle


def test_stale_fence_candidate_is_rejected_without_discarding_pending(
    tmp_path: Path,
) -> None:
    a17, b17, b18, _ = _generation_artifacts(tmp_path)
    runtime_generation = [7]
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=lambda: runtime_generation[0],
        context_generation=3,
    )
    target = _UniverseTargetExecutor()
    registry = worker_universe.ServerWorkerUniverseRegistry(host, target)
    active = host.prepare((a17, b17))
    target.acknowledge(active)
    active_handle = registry.promote(active)
    pending = host.prepare((a17, b18))
    target.acknowledge(pending)
    target.calls.clear()
    runtime_generation[0] = 8

    with pytest.raises(ProtocolError) as captured:
        registry.promote(pending)

    assert type(captured.value) is ProtocolError
    assert captured.value.args == (
        "Worker universe runtime/context fence is stale",
    )
    assert target.calls == []
    assert host.state is worker_universe.WorkerUniverseState.PREPARING
    assert host._pending is pending
    assert registry._broken is False
    runtime_generation[0] = 7
    assert host.active_handle is active_handle
    assert registry.promote(pending) is pending.handle


@pytest.mark.parametrize("fault", ("upload", "create", "wire", "probe"))
def test_pre_swap_fault_preserves_old_root_and_session_scopes_connected_candidate(
    tmp_path: Path,
    fault: str,
) -> None:
    a17, b17, b18, _ = _generation_artifacts(tmp_path)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    target = _UniverseTargetExecutor()
    server_registry = worker_universe.ServerWorkerUniverseRegistry(host, target)
    first = host.prepare((a17, b17))
    target.acknowledge(first)
    server_registry.promote(first)
    active_before = host.active_manifest
    registrations_before = set(target.registrations)
    candidate = host.prepare((a17, b18))
    candidate_registration = candidate.new_artifacts[0].worker_artifact.logical_name
    candidate_registration = next(
        module.registration_name
        for module in candidate.manifest.modules
        if module.logical_name == candidate_registration
    )
    target.acknowledge(candidate)
    target.fault = fault

    with pytest.raises(runtime_errors.BslExecutionError, match=fault):
        server_registry.promote(candidate)

    assert host.state is worker_universe.WorkerUniverseState.READY
    assert host.active_manifest is active_before
    assert target.active_manifest_sha256 == active_before.sha256
    expected_registrations = registrations_before
    if fault in {"create", "wire", "probe"}:
        expected_registrations |= {candidate_registration}
    assert target.registrations == expected_registrations
    assert set(server_registry._registrations) == expected_registrations
    assert target.disconnects == []


def test_unacknowledged_swap_quarantines_candidate_without_guessing_active_root(
    tmp_path: Path,
) -> None:
    a17, b17, b18, _ = _generation_artifacts(tmp_path)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    target = _UniverseTargetExecutor()
    server_registry = worker_universe.ServerWorkerUniverseRegistry(host, target)
    first = host.prepare((a17, b17))
    target.acknowledge(first)
    server_registry.promote(first)
    candidate = host.prepare((a17, b18))
    target.acknowledge(candidate)
    target.fault = "swap-ack"

    with pytest.raises(runtime_errors.WorkerPromotionOutcomeUnknown) as captured:
        server_registry.promote(candidate)

    assert captured.value.generation == candidate.handle.generation
    assert captured.value.manifest_sha256 == candidate.manifest.sha256
    assert host.state is worker_universe.WorkerUniverseState.BROKEN
    with pytest.raises(ProtocolError, match="broken"):
        _ = host.active_manifest
    assert target.active_manifest_sha256 == candidate.manifest.sha256
    assert target.disconnects == []
    assert {
        module.registration_name for module in candidate.manifest.modules
    } <= target.registrations


def test_absent_disconnect_api_cannot_break_confirmed_promotion(
    tmp_path: Path,
) -> None:
    a17, b17, b18, _ = _generation_artifacts(tmp_path)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    target = _UniverseTargetExecutor()
    server_registry = worker_universe.ServerWorkerUniverseRegistry(host, target)
    first = host.prepare((a17, b17))
    target.acknowledge(first)
    first_handle = server_registry.promote(first)
    b17_registration = next(
        module.registration_name
        for module in first.manifest.modules
        if module.logical_name == "МодульБ"
    )
    host.release_generation(first_handle)
    candidate = host.prepare((a17, b18))
    target.acknowledge(candidate)
    target.fault = "disconnect"

    promoted = server_registry.promote(candidate)

    assert host.active_handle is promoted
    assert host.state is worker_universe.WorkerUniverseState.READY
    assert b17_registration in target.registrations
    assert b17_registration in server_registry._registrations
    assert target.disconnects == []


def test_server_prepare_stages_only_new_content_before_atomic_promotion(
    tmp_path: Path,
) -> None:
    a17, b17, _, _ = _generation_artifacts(tmp_path)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    target = _UniverseTargetExecutor()
    server_registry = worker_universe.ServerWorkerUniverseRegistry(host, target)

    candidate = server_registry.prepare((a17, b17))

    assert host.state is worker_universe.WorkerUniverseState.PREPARING
    assert host.active_manifest is None
    assert target.active_manifest_sha256 is None
    assert target.registrations == {
        module.registration_name for module in candidate.manifest.modules
    }
    assert target.stage_batch_calls == 1
    assert len(target.calls) == 1

    target.acknowledge(candidate)
    assert server_registry.promote(candidate) is candidate.handle
    assert len(target.calls) == 3


@pytest.mark.parametrize("with_active", (False, True))
def test_server_prepare_fence_change_before_sealed_lookup_discards_exact_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    with_active: bool,
) -> None:
    a17, b17, b18, _ = _generation_artifacts(tmp_path)
    runtime_generation = [7]
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=lambda: runtime_generation[0],
        context_generation=3,
    )
    target = _UniverseTargetExecutor()
    registry = worker_universe.ServerWorkerUniverseRegistry(host, target)
    active_handle = None
    attempted_artifacts = (a17, b17)
    if with_active:
        active = registry.prepare((a17, b17))
        target.acknowledge(active)
        active_handle = registry.promote(active)
        attempted_artifacts = (a17, b18)
    original_prepare = host.prepare
    created: list[worker_universe.WorkerUniverseCandidate] = []
    flip_fence = [True]

    def prepare_then_change_fence(
        artifacts: tuple[WorkerModuleArtifact, ...],
    ) -> worker_universe.WorkerUniverseCandidate:
        candidate = original_prepare(artifacts)
        created.append(candidate)
        if flip_fence and flip_fence.pop():
            runtime_generation[0] = 8
        return candidate

    monkeypatch.setattr(host, "prepare", prepare_then_change_fence)
    target.calls.clear()

    with pytest.raises(ProtocolError) as captured:
        registry.prepare(attempted_artifacts)

    assert type(captured.value) is ProtocolError
    assert captured.value.args == (
        "Worker universe runtime/context fence is stale",
    )
    assert target.calls == []
    candidate = created[0]
    assert host._pending is None
    assert candidate.handle.generation not in host._sealed_activations
    assert candidate.handle.generation not in host._handles
    assert host.state is (
        worker_universe.WorkerUniverseState.READY
        if with_active
        else worker_universe.WorkerUniverseState.EMPTY
    )
    assert registry._broken is False
    runtime_generation[0] = 7
    assert host.active_handle is active_handle
    retry = registry.prepare(attempted_artifacts)
    target.acknowledge(retry)
    assert registry.promote(retry) is retry.handle


def test_server_prepare_unprovable_internal_cleanup_breaks_without_target_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a17, b17, _, _ = _generation_artifacts(tmp_path)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    target = _UniverseTargetExecutor()
    registry = worker_universe.ServerWorkerUniverseRegistry(host, target)
    original_prepare = host.prepare

    def prepare_then_lose_seal(
        artifacts: tuple[WorkerModuleArtifact, ...],
    ) -> worker_universe.WorkerUniverseCandidate:
        candidate = original_prepare(artifacts)
        host._sealed_activations.pop(candidate.handle.generation)
        return candidate

    monkeypatch.setattr(host, "prepare", prepare_then_lose_seal)

    with pytest.raises(runtime_errors.WorkerPromotionOutcomeUnknown):
        registry.prepare((a17, b17))

    assert target.calls == []
    assert host.state is worker_universe.WorkerUniverseState.BROKEN
    assert host._pending is None
    assert registry._broken is True


def test_server_prepare_local_validation_failure_discards_empty_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a17, b17, _, _ = _generation_artifacts(tmp_path)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    target = _UniverseTargetExecutor()
    server_registry = worker_universe.ServerWorkerUniverseRegistry(host, target)

    def reject_local_stage(*args: object, **kwargs: object) -> str:
        raise ProtocolError("local worker stage validation failed")

    monkeypatch.setattr(
        worker_universe,
        "stage_worker_batch_instruction",
        reject_local_stage,
    )

    with pytest.raises(ProtocolError, match="local worker stage validation failed"):
        server_registry.prepare((a17, b17))

    assert host.state is worker_universe.WorkerUniverseState.EMPTY
    assert host.active_handle is None
    assert host._pending is None
    assert host._registration_refcounts == {}
    assert host._registration_artifacts == {}
    assert target.calls == []
    assert target.registrations == set()
    assert server_registry._registrations == {}
    assert server_registry._candidate_registrations == {}


def test_server_prepare_local_validation_failure_restores_ready_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a17, b17, b18, _ = _generation_artifacts(tmp_path)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    target = _UniverseTargetExecutor()
    server_registry = worker_universe.ServerWorkerUniverseRegistry(host, target)
    first = server_registry.prepare((a17, b17))
    target.acknowledge(first)
    first_handle = server_registry.promote(first)
    target_calls_before = tuple(target.calls)
    target_registrations_before = set(target.registrations)
    owned_before = dict(server_registry._registrations)
    refcounts_before = dict(host._registration_refcounts)

    def reject_local_stage(*args: object, **kwargs: object) -> str:
        raise ProtocolError("local worker stage validation failed")

    monkeypatch.setattr(
        worker_universe,
        "stage_worker_batch_instruction",
        reject_local_stage,
    )

    with pytest.raises(ProtocolError, match="local worker stage validation failed"):
        server_registry.prepare((a17, b18))

    assert host.state is worker_universe.WorkerUniverseState.READY
    assert host.active_handle is first_handle
    assert host._pending is None
    assert host._registration_refcounts == refcounts_before
    assert tuple(target.calls) == target_calls_before
    assert target.registrations == target_registrations_before
    assert server_registry._registrations == owned_before
    assert server_registry._candidate_registrations == {}


def test_promote_local_batch_build_failure_discards_candidate_and_stays_usable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a17, b17, b18, _ = _generation_artifacts(tmp_path)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    target = _UniverseTargetExecutor()
    registry = worker_universe.ServerWorkerUniverseRegistry(host, target)
    first = host.prepare((a17, b17))
    target.acknowledge(first)
    first_handle = registry.promote(first)
    candidate = host.prepare((a17, b18))
    calls_before = tuple(target.calls)
    registrations_before = dict(registry._registrations)
    failure = MemoryError("local Base64 allocation failed")

    with monkeypatch.context() as patch:
        def reject_batch_build(*_args: object, **_kwargs: object) -> str:
            raise failure

        patch.setattr(
            worker_universe,
            "stage_worker_batch_instruction",
            reject_batch_build,
        )
        with pytest.raises(MemoryError) as captured:
            registry.promote(candidate)

    assert captured.value is failure
    assert host.state is worker_universe.WorkerUniverseState.READY
    assert host.active_handle is first_handle
    assert host._pending is None
    assert candidate.handle.generation not in host._sealed_activations
    assert registry._broken is False
    assert registry._candidate_registrations == {}
    assert registry._registrations == registrations_before
    assert tuple(target.calls) == calls_before

    retry = host.prepare((a17, b18))
    target.acknowledge(retry)
    assert registry.promote(retry) is retry.handle


@pytest.mark.parametrize("cleanup_fault", ("stale-fence", "discard-error"))
def test_server_prepare_uncertain_local_cleanup_quarantines_without_target_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_fault: str,
) -> None:
    a17, b17, _, _ = _generation_artifacts(tmp_path)
    runtime_generation = [7]
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=lambda: runtime_generation[0],
        context_generation=3,
    )
    target = _UniverseTargetExecutor()
    server_registry = worker_universe.ServerWorkerUniverseRegistry(host, target)

    def reject_local_stage(*args: object, **kwargs: object) -> str:
        if cleanup_fault == "stale-fence":
            runtime_generation[0] += 1
        raise ProtocolError("sensitive local worker stage detail")

    monkeypatch.setattr(
        worker_universe,
        "stage_worker_batch_instruction",
        reject_local_stage,
    )
    if cleanup_fault == "discard-error":
        def lose_discard(candidate: object) -> None:
            raise OSError("discard response lost")

        monkeypatch.setattr(
            host,
            "discard",
            lose_discard,
        )

    with pytest.raises(runtime_errors.WorkerPromotionOutcomeUnknown) as captured:
        server_registry.prepare((a17, b17))

    assert captured.value.generation == 1
    assert "sensitive local worker stage detail" not in str(captured.value)
    assert host.state is worker_universe.WorkerUniverseState.BROKEN
    assert host._pending is None
    assert host._quarantine_holds == {
        worker_universe.registration_name(
            artifact.logical_name,
            artifact.artifact_sha256,
        )
        for artifact in (a17, b17)
    }
    assert target.calls == []
    assert target.registrations == set()
    assert server_registry._registrations == {}
    assert server_registry._broken is True


def test_release_retires_host_refs_but_keeps_registration_until_session_teardown(
    tmp_path: Path,
) -> None:
    a17, b17, b18, _ = _generation_artifacts(tmp_path)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    target = _UniverseTargetExecutor()
    server_registry = worker_universe.ServerWorkerUniverseRegistry(host, target)
    first = host.prepare((a17, b17))
    target.acknowledge(first)
    first_handle = server_registry.promote(first)
    second = host.prepare((a17, b18))
    target.acknowledge(second)
    second_handle = server_registry.promote(second)
    b17_registration = next(
        module.registration_name
        for module in first.manifest.modules
        if module.logical_name == "МодульБ"
    )
    b18_registration = next(
        module.registration_name
        for module in second.manifest.modules
        if module.logical_name == "МодульБ"
    )

    assert {b17_registration, b18_registration} <= target.registrations
    assert target.disconnects == []

    server_registry.release(first_handle)

    assert target.disconnects == []
    assert b17_registration in target.registrations
    assert b17_registration in server_registry.privacy_registration_snapshot()
    assert host.registration_refcount(b17_registration) == 0
    assert b18_registration in target.registrations
    assert host.active_handle is second_handle


def test_wrapped_compile_failure_is_excluded_from_later_privacy_probes(
    tmp_path: Path,
) -> None:
    a17, b17, b18, _ = _generation_artifacts(tmp_path)

    class CompileFailingTarget(_UniverseTargetExecutor):
        failed_module: WorkerModuleArtifact | None = None

        def __call__(self, source: str) -> object:
            module = self.failed_module
            if module is not None and "onec-worker-root-prepare-stage=" in source:
                registration = worker_universe.registration_name(
                    module.logical_name, module.artifact_sha256
                )
                marker = (
                    "onec-worker-artifact-stage="
                    f"artifact_sha256={module.artifact_sha256};"
                    "logical_name_sha256="
                    f"{worker_universe.sha256(module.logical_name.casefold().encode('utf-8')).hexdigest()};"
                    "phase=create;boundary=create"
                )
                assert marker in source
                body = (
                    "Ошибка инициализации модуля\nпо причине:\n"
                    f"{{ВнешняяОбработка.{registration}.МодульОбъекта(1,1)}}: "
                    "Неизвестная процедура\n[ОшибкаКомпиляцииВстроенногоЯзыка]"
                )
                length = len(body.encode("utf-16-le")) // 2
                raise runtime_errors.BslExecutionError(
                    "onec-worker-root-prepare-stage=create\n"
                    f"{marker};diagnostic_utf16_length={length}\n"
                    f"{body}\n{{(35)}}: внешнее исключение\n"
                    "[ИсключениеВызванноеИзВстроенногоЯзыка]"
                )
            return super().__call__(source)

    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7, context_generation=3
    )
    target = CompileFailingTarget()
    registry = worker_universe.ServerWorkerUniverseRegistry(host, target)
    first = host.prepare((a17, b17))
    target.acknowledge(first)
    registry.promote(first)
    second = host.prepare((a17, b18))
    target.acknowledge(second)
    target.failed_module = b18

    with pytest.raises(runtime_errors.BslExecutionError):
        registry.promote(second)

    failed_registration = worker_universe.registration_name(
        b18.logical_name, b18.artifact_sha256
    )
    assert failed_registration in target.registrations
    assert failed_registration not in registry.privacy_registration_snapshot()


def test_logically_retired_registration_stays_connected_private_and_never_disconnects(
    tmp_path: Path,
) -> None:
    """Exact 8.3.27 has no external-processing Disconnect lifecycle API."""
    a17, b17, b18, _ = _generation_artifacts(tmp_path)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    target = _UniverseTargetExecutor()
    server_registry = worker_universe.ServerWorkerUniverseRegistry(host, target)
    first = host.prepare((a17, b17))
    target.acknowledge(first)
    first_handle = server_registry.promote(first)
    retired_registration = next(
        module.registration_name
        for module in first.manifest.modules
        if module.logical_name == "МодульБ"
    )

    server_registry.release(first_handle)
    second = host.prepare((a17, b18))
    target.acknowledge(second)
    # A real exact-platform executor rejects any generated Disconnect call.
    target.fault = "disconnect"
    second_handle = server_registry.promote(second)

    assert host.active_handle is second_handle
    assert host.registration_refcount(retired_registration) == 0
    assert retired_registration in server_registry.privacy_registration_snapshot()
    assert retired_registration in target.registrations
    assert target.disconnects == []
    assert all("ВнешниеОбработки.Отключить" not in call for call in target.calls)

    connected_before_teardown = set(target.registrations)
    server_registry.teardown()

    assert target.registrations == connected_before_teardown
    assert target.disconnects == []
    assert all("ВнешниеОбработки.Отключить" not in call for call in target.calls)
    assert server_registry._registrations == {}
    assert host.state is worker_universe.WorkerUniverseState.CLOSED


def test_server_registry_exposes_no_direct_disconnect_surface(
    tmp_path: Path,
) -> None:
    a17, b17, _, _ = _generation_artifacts(tmp_path)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    target = _UniverseTargetExecutor()
    server_registry = worker_universe.ServerWorkerUniverseRegistry(host, target)
    candidate = host.prepare((a17, b17))
    target.acknowledge(candidate)
    server_registry.promote(candidate)

    assert not hasattr(server_registry, "disconnect_unused")
    assert target.disconnects == []
    assert host.state is worker_universe.WorkerUniverseState.READY


def test_promotion_instruction_wires_original_and_overloaded_targets_and_probes_all(
    tmp_path: Path,
) -> None:
    a17, b17, _, original = _generation_artifacts(tmp_path)
    candidate = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    ).prepare((a17, b17, original))

    source = _prepared_instruction(candidate)

    first_wiring = source.index("ИсточникЗависимостиWorker0")
    assert source.count("ВнешниеОбработки.Создать") == len(candidate.manifest.modules)
    assert all(
        source.index(module.registration_name) < first_wiring
        for module in candidate.manifest.modules
    )
    assert 'ЦельЗависимостиWorker0 = ОбъектыКандидатаWorker.Получить(' in source
    assert " = Оригинальный;" in source
    assert source.count("ТипЗнч(ИсточникЗависимостиWorker") == len(
        candidate.manifest.wiring
    )
    assert "МетодДоступен(" not in source
    assert source.count("ТипЗнч(ОбъектМодуляWorker") == len(
        candidate.manifest.modules
    )
    assert source.count("ЭкспортыКандидатаWorker.Добавить(") == len(
        candidate.manifest.exports
    )
    assert all(export.public_path in source for export in candidate.manifest.exports)
    assert "Новый ФиксированноеСоответствие" in source
    assert "Новый ФиксированнаяСтруктура" in source
    assert candidate.manifest.sha256 in source
    assert 'Контекст.Вставить("RuntimeWorkerActiveGeneration"' not in source
    assert source.index(
        'Контекст.Вставить("__OnecWorkerPrepared_'
    ) < source.index(
        'Результат = "onec-worker-prepared-root-receipt-v1|'
    )
    PythonParserTarget.from_generated().parse(source, "Модуль")


def test_promotion_root_keeps_reserved_module_names_only_in_nested_module_map(
    tmp_path: Path,
) -> None:
    builder, _, _ = _builder(tmp_path)
    names = ("Modules", "Exports", "ManifestSha256")
    catalog = tuple(
        CommonModuleDescriptor(name, CommonModuleScope.SERVER)
        for name in names
    )
    artifacts = []
    for index, name in enumerate(names, start=1):
        lowered, context = _lowered(
            SOURCE,
            revision=index,
            logical_name=name,
            common_modules=catalog,
        )
        artifacts.append(builder.build(lowered, visible_source_context=context))
    candidate = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    ).prepare(tuple(artifacts))

    source = _prepared_instruction(candidate)

    root_keys = re.findall(
        r'^\s*ДанныеКорняКандидатаWorker\.\u0412ставить\("([^"]+)"',
        source,
        flags=re.MULTILINE,
    )
    assert root_keys == ["ManifestSha256", "RootKey", "Modules", "Exports"]
    assert "Новый ФиксированноеСоответствие(ОбъектыКандидатаWorker)" in source
    assert candidate.manifest.sha256 in source
    assert all(export.public_path in source for export in candidate.manifest.exports)
    PythonParserTarget.from_generated().parse(source, "Модуль")


def test_promotion_wraps_exports_in_distinct_marked_immutable_container(
    tmp_path: Path,
) -> None:
    """A raw fixed string array is indistinguishable from an ordinary public value."""
    builder, _, _ = _builder(tmp_path)
    catalog = (
        CommonModuleDescriptor("КадровыйУчет", CommonModuleScope.SERVER),
    )
    lowered, context = _lowered(
        SOURCE,
        revision=17,
        logical_name="КадровыйУчет",
        common_modules=catalog,
    )
    artifact = builder.build(lowered, visible_source_context=context)
    candidate = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    ).prepare((artifact,))

    source = _prepared_instruction(candidate)

    assert "ДанныеЭкспортовКандидатаWorker = Новый Структура;" in source
    assert (
        'ДанныеЭкспортовКандидатаWorker.Вставить("Kind", '
        '"OnecWorkerExportsV1");'
    ) in source
    assert (
        'ДанныеЭкспортовКандидатаWorker.Вставить("Items", '
        "ЭкспортыКандидатаWorker);"
    ) in source
    assert (
        "ЭкспортыКандидатаWorker = Новый ФиксированнаяСтруктура("
        "ДанныеЭкспортовКандидатаWorker);"
    ) in source
    assert candidate.manifest.exports[0].public_path in source
    PythonParserTarget.from_generated().parse(source, "Модуль")


def test_promotion_local_does_not_shadow_original_module_named_like_first_target_temp(
    tmp_path: Path,
) -> None:
    builder, _, _ = _builder(tmp_path)
    target_name = "ИсточникЗависимостиWorker0"
    catalog = (
        CommonModuleDescriptor("Потребитель", CommonModuleScope.SERVER),
        CommonModuleDescriptor(target_name, CommonModuleScope.SERVER),
    )
    lowered, context = _lowered(
        (
            "Функция Рассчитать() Экспорт\n"
            f"    Возврат {target_name}.Получить();\n"
            "КонецФункции\n"
        ),
        logical_name="Потребитель",
        common_modules=catalog,
    )
    artifact = builder.build(lowered, visible_source_context=context)
    candidate = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    ).prepare((artifact,))

    source = _prepared_instruction(candidate)

    assert re.search(rf"^\s*{target_name}\s*=", source, re.MULTILINE) is None
    assert f"= {target_name};" in source
    PythonParserTarget.from_generated().parse(source, "Модуль")


def test_promotion_local_allocation_reserves_all_original_targets_deterministically(
    tmp_path: Path,
) -> None:
    builder, _, _ = _builder(tmp_path)
    target_names = (
        "ИсточникЗависимостиWorker0",
        "ЦельЗависимостиWorker0",
        "ОбъектыКандидатаWorker",
        "КореньКандидатаWorker",
    )
    catalog = (
        CommonModuleDescriptor("Потребитель", CommonModuleScope.SERVER),
        *(
            CommonModuleDescriptor(name, CommonModuleScope.SERVER)
            for name in target_names
        ),
    )
    expression = " + ".join(f"{name}.Получить()" for name in target_names)
    lowered, context = _lowered(
        (
            "Функция Рассчитать() Экспорт\n"
            f"    Возврат {expression};\n"
            "КонецФункции\n"
        ),
        logical_name="Потребитель",
        common_modules=catalog,
    )
    artifact = builder.build(lowered, visible_source_context=context)
    candidate = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    ).prepare((artifact,))

    first = _prepared_instruction(candidate)
    second = _prepared_instruction(candidate)

    assert first == second
    for target_name in target_names:
        assert re.search(rf"^\s*{target_name}\s*=", first, re.MULTILINE) is None
        assert f"= {target_name};" in first
    PythonParserTarget.from_generated().parse(first, "Модуль")


def test_fixed_result_dependency_is_rejected_before_candidate_or_source_generation(
    tmp_path: Path,
) -> None:
    builder, _, _ = _builder(tmp_path)
    catalog = (
        CommonModuleDescriptor("Потребитель", CommonModuleScope.SERVER),
        CommonModuleDescriptor("Результат", CommonModuleScope.SERVER),
    )
    lowered, context = _lowered(
        (
            "Функция Рассчитать() Экспорт\n"
            "    Возврат Результат.Получить();\n"
            "КонецФункции\n"
        ),
        logical_name="Потребитель",
        common_modules=catalog,
    )
    artifact = builder.build(lowered, visible_source_context=context)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )

    with pytest.raises(ProtocolError, match="manifest admission is invalid"):
        host.prepare((artifact,))

    assert host.state is worker_universe.WorkerUniverseState.EMPTY
    assert host._pending is None
    assert host._handles == {}


@pytest.mark.parametrize(
    "target_name",
    (
        "Результат",
        "рЕзУлЬтАт",
        "Контекст",
        "ВнешниеОбработки",
        "ПоместитьВоВременноеХранилище",
        "Base64Значение",
        "ОписаниеОшибки",
        "Символы",
        "ТипЗнч",
        "Тип",
        "Соответствие",
        "ФиксированноеСоответствие",
        "Массив",
        "ФиксированныйМассив",
        "Структура",
        "ФиксированнаяСтруктура",
    ),
)
def test_original_dependency_rejects_fixed_unqualified_runtime_identifiers(
    target_name: str,
) -> None:
    with pytest.raises(ValueError, match="dependency target is invalid"):
        worker_universe.DependencyTarget(
            "Потребитель",
            "__OnecDependency_Источник",
            "original",
            target_name,
        )


@pytest.mark.parametrize(
    "target_name",
    ("Если", "еСлИ", "IF", "if", "Возврат", "RETURN"),
)
def test_dependency_target_rejects_russian_and_english_bsl_keywords(
    target_name: str,
) -> None:
    with pytest.raises(ValueError, match="dependency target is invalid"):
        worker_universe.DependencyTarget(
            "Потребитель",
            "__OnecDependency_Источник",
            "original",
            target_name,
        )


def test_manifest_readmission_rejects_forged_keyword_dependency_target(
    tmp_path: Path,
) -> None:
    _, _, _, original = _generation_artifacts(tmp_path)
    candidate = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    ).prepare((original,))
    valid = candidate.manifest.wiring[0]
    forged = object.__new__(worker_universe.DependencyTarget)
    object.__setattr__(forged, "source_module", valid.source_module)
    object.__setattr__(forged, "export_variable", valid.export_variable)
    object.__setattr__(forged, "target_kind", valid.target_kind)
    object.__setattr__(forged, "target_module", "Если")
    wiring = (forged,)
    digest = worker_universe._worker_manifest_sha256(
        candidate.manifest.generation,
        candidate.manifest.modules,
        wiring,
        candidate.manifest.exports,
    )

    with pytest.raises(ValueError, match="manifest is invalid"):
        worker_universe.WorkerUniverseManifest(
            candidate.manifest.generation,
            candidate.manifest.modules,
            wiring,
            candidate.manifest.exports,
            digest,
        )


def test_dependency_target_preserves_valid_unicode_identifiers() -> None:
    target = worker_universe.DependencyTarget(
        "МодульЁж",
        "__OnecDependency_ОбщийМодульЁж",
        "original",
        "ОбщийМодульЁж",
    )

    assert target.target_module == "ОбщийМодульЁж"


@pytest.mark.parametrize("fault", ("connect-unacknowledged", "receipt"))
def test_unacknowledged_target_boundary_never_guesses_old_active_generation(
    tmp_path: Path,
    fault: str,
) -> None:
    a17, b17, _, _ = _generation_artifacts(tmp_path)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    target = _UniverseTargetExecutor()
    target.fault = fault
    server_registry = worker_universe.ServerWorkerUniverseRegistry(host, target)
    candidate = host.prepare((a17, b17))
    target.acknowledge(candidate)

    with pytest.raises(runtime_errors.WorkerPromotionOutcomeUnknown):
        server_registry.promote(candidate)

    assert host.state is worker_universe.WorkerUniverseState.BROKEN
    with pytest.raises(ProtocolError, match="broken"):
        _ = host.active_handle
    assert server_registry._registrations == {}
    assert server_registry._candidate_registrations == {
        candidate.handle.generation: {
            module.registration_name for module in candidate.manifest.modules
        }
    }
    assert target.disconnects == []


def test_operation_pin_release_drops_host_ref_but_keeps_target_registration(
    tmp_path: Path,
) -> None:
    a17, b17, b18, _ = _generation_artifacts(tmp_path)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    target = _UniverseTargetExecutor()
    server_registry = worker_universe.ServerWorkerUniverseRegistry(host, target)
    first = host.prepare((a17, b17))
    target.acknowledge(first)
    first_handle = server_registry.promote(first)
    pin = host.pin_active()
    second = host.prepare((a17, b18))
    target.acknowledge(second)
    server_registry.promote(second)
    b17_registration = next(
        module.registration_name
        for module in first.manifest.modules
        if module.logical_name == "МодульБ"
    )

    server_registry.release(first_handle)
    assert b17_registration in target.registrations
    assert target.disconnects == []

    server_registry.release_pin(pin)

    assert target.disconnects == []
    assert b17_registration in target.registrations
    assert b17_registration in server_registry.privacy_registration_snapshot()
    assert host.registration_refcount(b17_registration) == 0


def test_teardown_forgets_ledger_without_dispatching_target_cleanup(
    tmp_path: Path,
) -> None:
    a17, b17, _, _ = _generation_artifacts(tmp_path)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    target = _UniverseTargetExecutor()
    target.fault = "swap-ack"
    server_registry = worker_universe.ServerWorkerUniverseRegistry(host, target)
    candidate = host.prepare((a17, b17))
    target.acknowledge(candidate)
    with pytest.raises(runtime_errors.WorkerPromotionOutcomeUnknown):
        server_registry.promote(candidate)
    owned = set(server_registry._registrations)
    target.fault = None

    server_registry.teardown()

    assert target.disconnects == []
    assert target.registrations == owned
    assert server_registry._registrations == {}
    assert host.state is worker_universe.WorkerUniverseState.CLOSED


def test_abandon_target_closes_host_without_target_execution_and_forbids_reuse(
    tmp_path: Path,
) -> None:
    a17, b17, _, _ = _generation_artifacts(tmp_path)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    target = _UniverseTargetExecutor()
    server_registry = worker_universe.ServerWorkerUniverseRegistry(host, target)
    candidate = host.prepare((a17, b17))
    target.acknowledge(candidate)
    handle = server_registry.promote(candidate)
    pin = host.pin_active()
    owned = set(server_registry._registrations)
    calls_before = tuple(target.calls)

    server_registry.abandon_target()

    assert tuple(target.calls) == calls_before
    assert target.registrations == owned
    assert server_registry._registrations == {}
    assert server_registry._candidate_registrations == {}
    assert server_registry._broken is True
    assert host.state is worker_universe.WorkerUniverseState.CLOSED
    with pytest.raises(ProtocolError, match="broken"):
        server_registry.prepare((a17, b17))
    with pytest.raises(ProtocolError, match="broken"):
        server_registry.promote(candidate)
    with pytest.raises(ProtocolError, match="broken"):
        server_registry.release(handle)
    with pytest.raises(ProtocolError, match="broken"):
        server_registry.release_pin(pin)


def test_teardown_ignores_empty_host_only_pre_target_quarantine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a17, b17, _, _ = _generation_artifacts(tmp_path)
    runtime_generation = [7]
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=lambda: runtime_generation[0],
        context_generation=3,
    )
    target = _UniverseTargetExecutor()
    server_registry = worker_universe.ServerWorkerUniverseRegistry(host, target)

    def reject_before_target(*args: object, **kwargs: object) -> str:
        runtime_generation[0] += 1
        raise ProtocolError("local stage validation failed")

    monkeypatch.setattr(
        worker_universe,
        "stage_worker_batch_instruction",
        reject_before_target,
    )
    with pytest.raises(runtime_errors.WorkerPromotionOutcomeUnknown):
        server_registry.prepare((a17, b17))
    host_only = set(host._quarantine_holds)
    assert host_only
    assert server_registry._registrations == {}

    server_registry.teardown()
    server_registry.teardown()

    assert host.state is worker_universe.WorkerUniverseState.CLOSED
    assert target.calls == []
    assert target.disconnects == []
    assert target.registrations == set()
    assert server_registry._registrations == {}


def test_teardown_forgets_ready_ledger_without_touching_target_or_host_only_hold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a17, b17, b18, _ = _generation_artifacts(tmp_path)
    runtime_generation = [7]
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=lambda: runtime_generation[0],
        context_generation=3,
    )
    target = _UniverseTargetExecutor()
    server_registry = worker_universe.ServerWorkerUniverseRegistry(host, target)
    active = server_registry.prepare((a17, b17))
    target.acknowledge(active)
    server_registry.promote(active)
    target_owned = set(server_registry._registrations)
    assert target.registrations == target_owned

    def reject_before_target(*args: object, **kwargs: object) -> str:
        runtime_generation[0] += 1
        raise ProtocolError("local stage validation failed")

    monkeypatch.setattr(
        worker_universe,
        "stage_worker_batch_instruction",
        reject_before_target,
    )
    with pytest.raises(runtime_errors.WorkerPromotionOutcomeUnknown):
        server_registry.prepare((a17, b18))
    host_only = set(host._quarantine_holds) - target_owned
    assert host_only
    assert host_only.isdisjoint(target.registrations)

    server_registry.teardown()
    calls_after_first = tuple(target.calls)
    server_registry.teardown()

    assert tuple(target.calls) == calls_after_first
    assert target.disconnects == []
    assert target.registrations == target_owned
    assert host_only.isdisjoint(target.registrations)
    assert server_registry._registrations == {}
    assert host.state is worker_universe.WorkerUniverseState.CLOSED


def test_reusing_logically_retired_artifact_does_not_reconnect_registration(
    tmp_path: Path,
) -> None:
    a17, b17, b18, _ = _generation_artifacts(tmp_path)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    target = _UniverseTargetExecutor()
    server_registry = worker_universe.ServerWorkerUniverseRegistry(host, target)
    first = host.prepare((a17, b17))
    target.acknowledge(first)
    first_handle = server_registry.promote(first)
    second = host.prepare((a17, b18))
    target.acknowledge(second)
    server_registry.promote(second)
    server_registry.release(first_handle)
    b17_registration = next(
        module.registration_name
        for module in first.manifest.modules
        if module.logical_name == "МодульБ"
    )
    assert host.registration_refcount(b17_registration) == 0
    stage_calls_before = sum(
        worker_stage_protocol.WORKER_STAGE_SCHEMA in call for call in target.calls
    )
    connected_before = set(target.registrations)

    reused = host.prepare((a17, b17))
    target.acknowledge(reused)
    server_registry.promote(reused)

    assert sum(worker_stage_protocol.WORKER_STAGE_SCHEMA in call for call in target.calls) == (
        stage_calls_before
    )
    assert target.registrations == connected_before
    assert b17_registration in server_registry.privacy_registration_snapshot()
    assert target.disconnects == []


def test_server_staging_rejects_forged_candidate_before_payload_or_target_access(
    tmp_path: Path,
) -> None:
    a17, b17, _, _ = _generation_artifacts(tmp_path)
    host = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    target = _UniverseTargetExecutor()
    server_registry = worker_universe.ServerWorkerUniverseRegistry(host, target)
    candidate = host.prepare((a17, b17))
    forged = replace(candidate)

    with pytest.raises(ProtocolError, match="candidate"):
        server_registry.stages(forged)

    assert target.calls == []
    assert server_registry._registrations == {}
    assert host.state is worker_universe.WorkerUniverseState.PREPARING


def test_same_lowered_bytes_reuse_binary_but_keep_revision_descriptor(
    tmp_path: Path,
) -> None:
    builder, packer, cache = _builder(tmp_path)
    lowered_r1, context_r1 = _lowered(revision=1)
    lowered_r2, context_r2 = _lowered(revision=2)

    first = builder.build(lowered_r1, visible_source_context=context_r1)
    second = builder.build(lowered_r2, visible_source_context=context_r2)

    assert first.binary_key == second.binary_key
    assert first.artifact_sha256 == second.artifact_sha256
    assert first.revision == 1 and second.revision == 2
    assert first.source_map_sha256 != second.source_map_sha256
    assert first.worker_artifact is not second.worker_artifact
    assert packer.calls == 1
    assert len(cache) == 1
    assert validate_worker_module_artifact(first) == first.exports
    assert validate_worker_module_artifact(second) == second.exports


def test_binary_key_contains_every_binary_input_and_no_generation_identity() -> None:
    lowered, _ = _lowered()
    base = WorkerModuleBinaryKey.create(
        lowered,
        packer_version="packer-v1",
        target_profile="profile-v1",
    )

    changed_source, _ = _lowered(SOURCE.replace("41", "42"))
    changed_dependency = replace(lowered, dependency_bindings_sha256="f" * 64)
    assert WorkerModuleBinaryKey.create(
        changed_source,
        packer_version="packer-v1",
        target_profile="profile-v1",
    ) != base
    assert WorkerModuleBinaryKey.create(
        changed_dependency,
        packer_version="packer-v1",
        target_profile="profile-v1",
    ) != base
    assert replace(base, transform_version="transform-v2") != base
    assert replace(base, packer_version="packer-v2") != base
    assert replace(base, target_profile="profile-v2") != base
    assert "revision" not in WorkerModuleBinaryKey.__dataclass_fields__
    assert "overloaded" not in WorkerModuleBinaryKey.__dataclass_fields__
    assert "generation" not in WorkerModuleBinaryKey.__dataclass_fields__
    assert "manifest" not in WorkerModuleBinaryKey.__dataclass_fields__


def test_revision_and_override_membership_do_not_change_binary_key() -> None:
    lowered_r1, _ = _lowered(revision=1)
    lowered_r2, _ = _lowered(revision=2)

    first = WorkerModuleBinaryKey.create(
        lowered_r1,
        packer_version="packer-v1",
        target_profile="profile-v1",
    )
    second = WorkerModuleBinaryKey.create(
        lowered_r2,
        packer_version="packer-v1",
        target_profile="profile-v1",
    )

    assert first == second


@pytest.mark.parametrize(
    "mutation",
    (
        lambda item: replace(item, source_sha256="f" * 64),
        lambda item: replace(item, source_map_sha256="e" * 64),
        lambda item: replace(item, revision=item.revision + 1),
        lambda item: replace(
            item,
            binary_key=replace(item.binary_key, dependency_bindings_sha256="d" * 64),
        ),
        lambda item: replace(item, exports=()),
        lambda item: replace(
            item,
            worker_artifact=replace(item.worker_artifact, exports=()),
        ),
    ),
)
def test_descriptor_worker_pair_tampering_fails_production_admission(
    mutation,
    tmp_path: Path,
) -> None:
    builder, _, _ = _builder(tmp_path)
    lowered, context = _lowered()
    artifact = builder.build(lowered, visible_source_context=context)

    with pytest.raises(ProtocolError, match="module artifact admission"):
        validate_worker_module_artifact(mutation(artifact))


def test_descriptor_repr_serialization_and_errors_do_not_leak_payloads_or_paths(
    tmp_path: Path,
) -> None:
    source_sentinel = "СверхСекретныйМаркерИсходника"
    source = SOURCE.replace("41", f'41 // {source_sentinel}')
    builder, _, _ = _builder(tmp_path)
    lowered, context = _lowered(source)
    artifact = builder.build(lowered, visible_source_context=context)
    capability = artifact.worker_artifact._admission
    assert capability is not None
    _, artifact_bytes, _, _ = capability.contents(artifact.worker_artifact)
    binary_sentinel = artifact_bytes[:32].hex()

    views = (
        repr(artifact),
        json.dumps(asdict(artifact), ensure_ascii=False, default=str),
    )
    for view in views:
        assert source_sentinel not in view
        assert source not in view
        assert str(tmp_path.resolve()) not in view
        assert binary_sentinel not in view

    tampered = replace(artifact, source_sha256="f" * 64)
    with pytest.raises(ProtocolError) as caught:
        validate_worker_module_artifact(tampered)
    message = str(caught.value)
    assert source_sentinel not in message
    assert source not in message
    assert str(tmp_path.resolve()) not in message
    assert binary_sentinel not in message


def test_cache_real_insertion_boundary_rejects_wrong_key_capsule(tmp_path: Path) -> None:
    builder, _, cache = _builder(tmp_path)
    lowered, context = _lowered()
    artifact = builder.build(lowered, visible_source_context=context)
    wrong_key = replace(artifact.binary_key, logical_name="ДругойМодуль")
    admitted = cache._capsules[artifact.binary_key]
    forged = worker_universe._WorkerModuleBinaryCapsule(
        wrong_key,
        artifact.worker_artifact,
        admitted.proof,
    )

    with pytest.raises(ProtocolError, match="binary cache"):
        cache._admit_capsule(forged)


def test_product_module_has_no_runtime_parsergen_com_or_spike_imports() -> None:
    module_path = Path(__file__).parents[2] / "src" / "onec_runtime" / "worker_universe.py"
    source = module_path.read_text(encoding="utf-8").casefold()

    for forbidden in (
        "parsergen",
        "integration.spikes",
        "comconnector",
        "win32com",
        "pythoncom",
        "кадровыйучет",
    ):
        assert forbidden not in source


def test_binary_key_rejects_path_shaped_public_identity() -> None:
    lowered, _ = _lowered()

    with pytest.raises(ValueError, match="binary key"):
        WorkerModuleBinaryKey.create(
            lowered,
            packer_version=r"C:\private\packer",
            target_profile="server-test",
        )


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("dependency_bindings_sha256", "f" * 64),
        ("transform_version", "transform-v2"),
        ("packer_version", "packer-v2"),
        ("target_profile", "server-other"),
    ),
)
def test_cache_rejects_binary_admitted_under_forged_complete_key(
    field_name: str,
    value: str,
    tmp_path: Path,
) -> None:
    builder, _, cache = _builder(tmp_path)
    lowered, context = _lowered()
    artifact = builder.build(lowered, visible_source_context=context)
    forged = replace(artifact.binary_key, **{field_name: value})
    admitted = cache._capsules[artifact.binary_key]
    replay = worker_universe._WorkerModuleBinaryCapsule(
        forged,
        artifact.worker_artifact,
        admitted.proof,
    )

    with pytest.raises(ProtocolError, match="binary cache"):
        cache._admit_capsule(replay)


def test_cross_profile_poisoning_cannot_skip_profile_specific_packaging(
    tmp_path: Path,
) -> None:
    packer = _CountingArtifactBuilder(_notebook_builder(tmp_path))
    cache = WorkerModuleArtifactCache()
    lowered, context = _lowered()
    profile_a = WorkerModuleArtifactBuilder(
        packer,
        cache=cache,
        packer_version="worker-epf-v1",
        target_profile="server-a",
    )
    first = profile_a.build(lowered, visible_source_context=context)
    forged = replace(first.binary_key, target_profile="server-b")
    admitted = cache._capsules[first.binary_key]
    replay = worker_universe._WorkerModuleBinaryCapsule(
        forged,
        first.worker_artifact,
        admitted.proof,
    )

    with pytest.raises(ProtocolError, match="binary cache"):
        cache._admit_capsule(replay)

    profile_b = WorkerModuleArtifactBuilder(
        packer,
        cache=cache,
        packer_version="worker-epf-v1",
        target_profile="server-b",
    )
    second = profile_b.build(lowered, visible_source_context=context)

    assert packer.calls == 2
    assert first.binary_key != second.binary_key


def test_binary_key_and_cache_identity_are_case_insensitive_for_logical_name(
    tmp_path: Path,
) -> None:
    builder, packer, _ = _builder(tmp_path)
    first_lowered, first_context = _lowered(logical_name="МодульРасчета")
    second_lowered, second_context = _lowered(logical_name="модульрасчета")

    first = builder.build(first_lowered, visible_source_context=first_context)
    second = builder.build(second_lowered, visible_source_context=second_context)

    assert first.binary_key == second.binary_key
    assert packer.calls == 1
    assert first.logical_name == "МодульРасчета"
    assert second.logical_name == "модульрасчета"
    assert first.exports[0].receiver_module == "МодульРасчета"
    assert second.exports[0].receiver_module == "модульрасчета"


def test_capsule_proof_cannot_be_replayed_across_delimiter_ambiguous_keys(
    tmp_path: Path,
) -> None:
    packer = _CountingArtifactBuilder(_notebook_builder(tmp_path))
    cache = WorkerModuleArtifactCache()
    lowered, context = _lowered(logical_name="МодульРасчета")
    first_builder = WorkerModuleArtifactBuilder(
        packer,
        cache=cache,
        packer_version="packer-a\x1fpacker-b",
        target_profile="profile-c",
    )
    first = first_builder.build(lowered, visible_source_context=context)
    admitted = cache._capsules[first.binary_key]
    replay_key = replace(
        first.binary_key,
        packer_version="packer-a",
        target_profile="packer-b\x1fprofile-c",
    )
    assert replay_key != first.binary_key
    replay = worker_universe._WorkerModuleBinaryCapsule(
        replay_key,
        first.worker_artifact,
        admitted.proof,
    )

    with pytest.raises(ProtocolError, match="binary cache"):
        cache._admit_capsule(replay)

    second_builder = WorkerModuleArtifactBuilder(
        packer,
        cache=cache,
        packer_version="packer-a",
        target_profile="packer-b\x1fprofile-c",
    )
    second = second_builder.build(lowered, visible_source_context=context)

    assert second.binary_key == replay_key
    assert packer.calls == 2
    assert len(cache) == 2


def test_cache_has_no_public_insertion_method_that_always_rejects() -> None:
    cache = WorkerModuleArtifactCache()

    assert not hasattr(cache, "admit")


def test_builder_lookup_rejects_persisted_delimiter_replay_before_packaging(
    tmp_path: Path,
) -> None:
    packer = _CountingArtifactBuilder(_notebook_builder(tmp_path))
    cache = WorkerModuleArtifactCache()
    lowered, context = _lowered(logical_name="МодульРасчета")
    first_builder = WorkerModuleArtifactBuilder(
        packer,
        cache=cache,
        packer_version="packer-a\x1fpacker-b",
        target_profile="profile-c",
    )
    first = first_builder.build(lowered, visible_source_context=context)
    admitted = cache._capsules[first.binary_key]
    replay_key = replace(
        first.binary_key,
        packer_version="packer-a",
        target_profile="packer-b\x1fprofile-c",
    )
    cache._capsules[replay_key] = worker_universe._WorkerModuleBinaryCapsule(
        replay_key,
        first.worker_artifact,
        admitted.proof,
    )
    replay_builder = WorkerModuleArtifactBuilder(
        packer,
        cache=cache,
        packer_version="packer-a",
        target_profile="packer-b\x1fprofile-c",
    )

    with pytest.raises(ProtocolError, match="module artifact admission"):
        replay_builder.build(lowered, visible_source_context=context)

    assert packer.calls == 1


def test_manifest_is_deterministic_and_wires_original_and_overloaded_cycle(
    tmp_path: Path,
) -> None:
    a17, b17, _, original = _generation_artifacts(tmp_path)
    registry = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )

    first = registry.prepare((original, b17, a17))
    registry.discard(first)
    second = registry.prepare((a17, original, b17))

    assert first.manifest.sha256 != second.manifest.sha256
    assert tuple(item.logical_name for item in second.manifest.modules) == (
        "МодульА",
        "МодульБ",
        "ПотребительОригинального",
    )
    assert [
        (item.source_module, item.target_kind, item.target_module)
        for item in second.manifest.wiring
    ] == [
        ("МодульА", "overloaded", "МодульБ"),
        ("МодульБ", "overloaded", "МодульА"),
        ("ПотребительОригинального", "original", "Оригинальный"),
    ]
    assert all(
        worker_universe.registration_name(
            module.logical_name,
            module.artifact_sha256,
        )
        == module.registration_name
        for module in second.manifest.modules
    )


def test_same_generation_inputs_have_same_manifest_independent_of_input_order(
    tmp_path: Path,
) -> None:
    a17, b17, _, original = _generation_artifacts(tmp_path)
    left = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    ).prepare((original, b17, a17))
    right = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    ).prepare((a17, original, b17))

    assert left.manifest == right.manifest


def test_registration_is_content_addressed_and_old_generation_retains_it(
    tmp_path: Path,
) -> None:
    a17, b17, b18, _ = _generation_artifacts(tmp_path)
    registry = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    g17 = registry.prepare((a17, b17))
    assert registry.active_manifest is None
    registry.confirm(g17, _acknowledged_receipt(g17))
    pin = registry.pin_active()
    g18 = registry.prepare((a17, b18))
    registry.confirm(g18, _acknowledged_receipt(g18))

    b17_registration = next(
        item.registration_name
        for item in g17.manifest.modules
        if item.logical_name == "МодульБ"
    )
    b18_registration = next(
        item.registration_name
        for item in g18.manifest.modules
        if item.logical_name == "МодульБ"
    )
    assert b17_registration != b18_registration
    assert registry.registration_refcount(b17_registration) == 2

    registry.release_generation(g17.handle)
    assert registry.registration_refcount(b17_registration) == 1
    registry.release_pin(pin)
    assert registry.registration_refcount(b17_registration) == 0


def test_confirm_reports_zero_transition_once_and_readded_artifact_restages(
    tmp_path: Path,
) -> None:
    a17, b17, _, _ = _generation_artifacts(tmp_path)
    registry = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    g17 = registry.prepare((a17, b17))
    confirmed_g17 = registry.confirm(g17, _acknowledged_receipt(g17))
    assert isinstance(confirmed_g17, worker_universe.WorkerGenerationConfirmation)
    assert confirmed_g17.handle is g17.handle
    assert confirmed_g17.released_registrations == ()
    b17_registration = next(
        item.registration_name
        for item in g17.manifest.modules
        if item.logical_name == "МодульБ"
    )
    registry.release_generation(g17.handle)
    g18 = registry.prepare((a17,))

    confirmed_g18 = registry.confirm(g18, _acknowledged_receipt(g18))

    assert confirmed_g18.handle is g18.handle
    assert confirmed_g18.released_registrations == (b17_registration,)
    assert b17_registration not in repr(confirmed_g18)
    assert "<redacted>" in repr(confirmed_g18)
    assert b17_registration not in registry._registration_refcounts
    assert b17_registration not in registry._registration_artifacts
    assert registry.registration_refcount(b17_registration) == 0
    g19 = registry.prepare((a17, b17))
    assert tuple(item.logical_name for item in g19.new_artifacts) == ("МодульБ",)
    assert registry.confirm(
        g19,
        _acknowledged_receipt(g19),
    ).released_registrations == ()
    assert registry.release_generation(g18.handle) == ()


def test_revision_changes_manifest_but_reuses_content_registration(tmp_path: Path) -> None:
    builder, _, _ = _builder(tmp_path)
    lowered_r1, context_r1 = _lowered(revision=1)
    lowered_r2, context_r2 = _lowered(revision=2)
    first_artifact = builder.build(lowered_r1, visible_source_context=context_r1)
    second_artifact = builder.build(lowered_r2, visible_source_context=context_r2)
    first = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    ).prepare((first_artifact,))
    second = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    ).prepare((second_artifact,))

    assert first.manifest.sha256 != second.manifest.sha256
    assert (
        first.manifest.modules[0].registration_name
        == second.manifest.modules[0].registration_name
    )


def test_inter_generation_full_registration_identity_reuses_revision_and_casing(
    tmp_path: Path,
) -> None:
    builder, _, _ = _builder(tmp_path)
    first_lowered, first_context = _lowered(
        logical_name="Module118",
        revision=1,
    )
    second_lowered, second_context = _lowered(
        logical_name="module118",
        revision=2,
    )
    first = builder.build(first_lowered, visible_source_context=first_context)
    second = builder.build(second_lowered, visible_source_context=second_context)
    registry = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    g1 = registry.prepare((first,))
    registry.confirm(g1, _acknowledged_receipt(g1))

    g2 = registry.prepare((second,))

    assert first.artifact_sha256 == second.artifact_sha256
    assert g2.new_artifacts == ()
    confirmation = registry.confirm(g2, _acknowledged_receipt(g2))
    assert confirmation.handle is g2.handle
    assert confirmation.released_registrations == ()


def test_registration_name_is_bounded_bsl_identifier_and_case_insensitive() -> None:
    digest = "a" * 64

    upper = worker_universe.registration_name("ОченьДлинноеИмяОбщегоМодуля", digest)
    lower = worker_universe.registration_name("оченьдлинноеимяобщегомодуля", digest)

    assert upper == lower
    assert re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", upper)
    assert len(upper) <= 80
    assert upper.endswith("_" + digest[:16])


def test_real_registration_digest_collision_is_rejected_before_candidate_mutation(
    tmp_path: Path,
) -> None:
    builder, _, _ = _builder(tmp_path)
    left_lowered, left_context = _lowered(logical_name="Module118")
    right_lowered, right_context = _lowered(logical_name="Module88684")
    left = builder.build(left_lowered, visible_source_context=left_context)
    right = builder.build(right_lowered, visible_source_context=right_context)
    registry = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )

    assert left.artifact_sha256 == right.artifact_sha256
    assert worker_universe.registration_name(
        left.logical_name,
        left.artifact_sha256,
    ) == worker_universe.registration_name(
        right.logical_name,
        right.artifact_sha256,
    )

    with pytest.raises(ProtocolError) as caught:
        registry.prepare((left, right))

    message = str(caught.value)
    assert message == "Worker universe manifest admission is invalid"
    assert "Module118" not in message
    assert "Module88684" not in message
    assert str(tmp_path.resolve()) not in message
    assert registry.state is worker_universe.WorkerUniverseState.EMPTY
    assert registry.active_manifest is None
    assert registry._registration_refcounts == {}


def test_inter_generation_registration_collision_rejects_until_old_owner_zero(
    tmp_path: Path,
) -> None:
    builder, _, _ = _builder(tmp_path)

    def build(logical_name: str) -> WorkerModuleArtifact:
        lowered, context = _lowered(logical_name=logical_name)
        return builder.build(lowered, visible_source_context=context)

    left = build("Module118")
    right = build("Module88684")
    parking = build("ParkingModule")
    registry = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    g1 = registry.prepare((left,))
    registry.confirm(g1, _acknowledged_receipt(g1))
    registration = g1.manifest.modules[0].registration_name
    refcounts_before = dict(registry._registration_refcounts)

    with pytest.raises(
        ProtocolError,
        match="Worker registration identity collision",
    ):
        registry.prepare((right,))

    assert registry.state is worker_universe.WorkerUniverseState.READY
    assert registry.active_handle is g1.handle
    assert registry._pending is None
    assert registry._registration_refcounts == refcounts_before

    pin = registry.pin_active()
    registry.release_generation(g1.handle)
    parked = registry.prepare((parking,))
    registry.confirm(parked, _acknowledged_receipt(parked))
    with pytest.raises(
        ProtocolError,
        match="Worker registration identity collision",
    ):
        registry.prepare((right,))

    assert registry.release_pin(pin) == (registration,)
    restaged = registry.prepare((right,))
    assert restaged.new_artifacts == (right,)
    registry.confirm(restaged, _acknowledged_receipt(restaged))
    assert registry.active_handle is restaged.handle


def test_confirm_revalidates_registration_owner_before_active_state_mutation(
    tmp_path: Path,
) -> None:
    builder, _, _ = _builder(tmp_path)

    def build(logical_name: str) -> WorkerModuleArtifact:
        lowered, context = _lowered(logical_name=logical_name)
        return builder.build(lowered, visible_source_context=context)

    active_artifact = build("ParkingModule")
    candidate_artifact = build("Module118")
    registry = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    active = registry.prepare((active_artifact,))
    registry.confirm(active, _acknowledged_receipt(active))
    candidate = registry.prepare((candidate_artifact,))
    registration = candidate.manifest.modules[0].registration_name
    active_before = registry.active_handle
    refcounts_before = dict(registry._registration_refcounts)
    registry._registration_artifacts[registration] = (
        "module88684",
        candidate_artifact.artifact_sha256,
    )

    with pytest.raises(
        ProtocolError,
        match="Worker registration identity collision",
    ):
        registry.confirm(candidate, _acknowledged_receipt(candidate))

    assert registry.active_handle is active_before
    assert registry._registration_refcounts == refcounts_before
    assert registry._pending is candidate
    assert registry.state is worker_universe.WorkerUniverseState.PREPARING


def test_confirm_result_validation_failure_leaves_all_live_state_unchanged(
    tmp_path: Path,
) -> None:
    a17, b17, b18, _ = _generation_artifacts(tmp_path)
    registry = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    active = registry.prepare((a17, b17))
    registry.confirm(active, _acknowledged_receipt(active))
    pin = registry.pin_active()
    pending = registry.prepare((a17, b18))
    registry._quarantine_holds.add(
        worker_universe.registration_name("QuarantineProbe", "b" * 64)
    )
    invalid_registration = "not-a-bsl-registration"
    registry._registration_refcounts[invalid_registration] = 0
    registry._registration_artifacts[invalid_registration] = (
        "corruption-probe",
        "c" * 64,
    )
    before = _registry_live_state_snapshot(registry)

    with pytest.raises(
        ValueError,
        match="worker generation confirmation is invalid",
    ):
        registry.confirm(pending, _acknowledged_receipt(pending))

    assert _registry_live_state_snapshot(registry) == before
    assert registry.state is worker_universe.WorkerUniverseState.PREPARING
    assert registry.active_handle is active.handle
    assert registry.active_manifest is active.manifest
    assert registry.active_root_key == "generation-1"
    assert registry._pending is pending
    assert registry._handles[pending.handle.generation] is pending.handle
    assert registry._leases[pin.lease_id].pin is pin


def test_manifest_rejects_noncanonical_or_non_bsl_export_catalog(tmp_path: Path) -> None:
    a17, b17, _, _ = _generation_artifacts(tmp_path)
    candidate = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    ).prepare((a17, b17))
    reversed_exports = tuple(reversed(candidate.manifest.exports))
    reversed_sha = worker_universe._worker_manifest_sha256(
        candidate.manifest.generation,
        candidate.manifest.modules,
        candidate.manifest.wiring,
        reversed_exports,
    )

    with pytest.raises(ValueError, match="manifest"):
        replace(
            candidate.manifest,
            exports=reversed_exports,
            sha256=reversed_sha,
        )

    unsafe = (
        WorkerExport(
            "МодульА.Небезопасный\x1fМетод",
            "Небезопасный\x1fМетод",
            receiver_module="МодульА",
        ),
        *candidate.manifest.exports[1:],
    )
    unsafe_sha = worker_universe._worker_manifest_sha256(
        candidate.manifest.generation,
        candidate.manifest.modules,
        candidate.manifest.wiring,
        unsafe,
    )
    with pytest.raises(ValueError, match="manifest"):
        replace(candidate.manifest, exports=unsafe, sha256=unsafe_sha)


def test_manifest_digest_covers_revision_wiring_and_complete_export_identity(
    tmp_path: Path,
) -> None:
    a17, b17, _, _ = _generation_artifacts(tmp_path)
    manifest = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    ).prepare((a17, b17)).manifest
    changed_module = replace(manifest.modules[0], revision=18)
    changed_wiring = replace(manifest.wiring[0], export_variable="ДругойAlias")
    changed_export = WorkerExport(
        "МодульА.ДругойМетод",
        "ДругойМетод",
        receiver_module="МодульА",
    )

    assert worker_universe._worker_manifest_sha256(
        manifest.generation,
        (changed_module, *manifest.modules[1:]),
        manifest.wiring,
        manifest.exports,
    ) != manifest.sha256
    assert worker_universe._worker_manifest_sha256(
        manifest.generation,
        manifest.modules,
        (changed_wiring, *manifest.wiring[1:]),
        manifest.exports,
    ) != manifest.sha256
    assert worker_universe._worker_manifest_sha256(
        manifest.generation,
        manifest.modules,
        manifest.wiring,
        (changed_export, *manifest.exports[1:]),
    ) != manifest.sha256


def test_self_dependency_is_valid_overloaded_wiring(tmp_path: Path) -> None:
    builder, _, _ = _builder(tmp_path)
    modules = (
        CommonModuleDescriptor("Самоссылочный", CommonModuleScope.SERVER),
    )
    lowered, context = _lowered(
        (
            "Функция Рассчитать() Экспорт\n"
            "    Возврат Самоссылочный.Рассчитать();\n"
            "КонецФункции\n"
        ),
        logical_name="Самоссылочный",
        common_modules=modules,
    )
    artifact = builder.build(lowered, visible_source_context=context)

    candidate = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    ).prepare((artifact,))

    assert len(candidate.manifest.wiring) == 1
    assert candidate.manifest.wiring[0].source_module == "Самоссылочный"
    assert candidate.manifest.wiring[0].target_module == "Самоссылочный"
    assert candidate.manifest.wiring[0].target_kind == "overloaded"


def test_manifest_wires_by_module_name_without_target_method_validation(
    tmp_path: Path,
) -> None:
    builder, _, _ = _builder(tmp_path)
    modules = (
        CommonModuleDescriptor("МодульА", CommonModuleScope.SERVER),
        CommonModuleDescriptor("МодульБ", CommonModuleScope.SERVER),
    )
    a_lowered, a_context = _lowered(
        (
            "Функция Рассчитать() Экспорт\n"
            "    Возврат МодульБ.НетТакогоМетода();\n"
            "КонецФункции\n"
        ),
        logical_name="МодульА",
        common_modules=modules,
    )
    b_lowered, b_context = _lowered(
        "Функция ДругойМетод() Экспорт\n    Возврат 1;\nКонецФункции\n",
        logical_name="МодульБ",
        common_modules=modules,
    )
    a = builder.build(a_lowered, visible_source_context=a_context)
    b = builder.build(b_lowered, visible_source_context=b_context)
    registry = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )

    candidate = registry.prepare((a, b))

    assert [(item.target_module, item.target_kind) for item in candidate.manifest.wiring] == [
        ("МодульБ", "overloaded")
    ]


def test_duplicate_case_insensitive_logical_module_and_public_path_are_rejected(
    tmp_path: Path,
) -> None:
    builder, _, _ = _builder(tmp_path)
    upper_lowered, upper_context = _lowered(logical_name="МодульА")
    lower_lowered, lower_context = _lowered(logical_name="модульа")
    upper = builder.build(upper_lowered, visible_source_context=upper_context)
    lower = builder.build(lower_lowered, visible_source_context=lower_context)
    registry = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )

    with pytest.raises(ProtocolError, match="duplicate logical"):
        registry.prepare((upper, lower))


def test_prepare_changes_only_lifecycle_until_exact_acknowledged_receipt(
    tmp_path: Path,
) -> None:
    a17, b17, b18, _ = _generation_artifacts(tmp_path)
    registry = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    g17 = registry.prepare((a17, b17))
    registry.confirm(g17, _acknowledged_receipt(g17))
    active_before = registry.active_manifest
    refcounts_before = {
        module.registration_name: registry.registration_refcount(
            module.registration_name
        )
        for module in active_before.modules
    }

    g18 = registry.prepare((a17, b18))

    assert registry.state is worker_universe.WorkerUniverseState.PREPARING
    assert registry.active_manifest is active_before
    assert g18.previous is g17.handle
    assert tuple(item.logical_name for item in g18.new_artifacts) == ("МодульБ",)
    assert {
        name: registry.registration_refcount(name) for name in refcounts_before
    } == refcounts_before

    unacknowledged = replace(_acknowledged_receipt(g18), acknowledged=False)
    with pytest.raises(ProtocolError, match="receipt"):
        registry.confirm(g18, unacknowledged)
    wrong_root = replace(_acknowledged_receipt(g18), root_key="generation-wrong")
    with pytest.raises(ProtocolError, match="receipt"):
        registry.confirm(g18, wrong_root)
    assert registry.active_manifest is active_before
    assert {
        name: registry.registration_refcount(name) for name in refcounts_before
    } == refcounts_before
    registry.discard(g18)
    assert registry.state is worker_universe.WorkerUniverseState.READY


def test_second_prepare_is_rejected_and_original_candidate_remains_confirmable(
    tmp_path: Path,
) -> None:
    a17, b17, b18, _ = _generation_artifacts(tmp_path)
    registry = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    g17 = registry.prepare((a17, b17))
    registry.confirm(g17, _acknowledged_receipt(g17))
    pending = registry.prepare((a17, b18))
    refcounts_before = dict(registry._registration_refcounts)
    handles_before = dict(registry._handles)

    with pytest.raises(ProtocolError, match="pending"):
        registry.prepare((a17, b17))

    assert registry.state is worker_universe.WorkerUniverseState.PREPARING
    assert registry._pending is pending
    assert registry._registration_refcounts == refcounts_before
    assert registry._handles == handles_before
    assert registry.active_handle is g17.handle
    forged = replace(pending)
    with pytest.raises(ProtocolError, match="candidate"):
        registry.confirm(forged, _acknowledged_receipt(forged))

    registry.confirm(pending, _acknowledged_receipt(pending))
    assert registry.active_handle is pending.handle


def test_discarded_candidate_is_stale_and_does_not_retain_handle_capability(
    tmp_path: Path,
) -> None:
    a17, b17, b18, _ = _generation_artifacts(tmp_path)
    registry = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    active = registry.prepare((a17, b17))
    registry.confirm(active, _acknowledged_receipt(active))
    discarded = registry.prepare((a17, b18))
    assert discarded.handle.generation in registry._handles

    registry.discard(discarded)
    assert discarded.handle.generation not in registry._handles
    assert set(registry._handles) == {active.handle.generation}
    with pytest.raises(ProtocolError, match="candidate"):
        registry.confirm(discarded, _acknowledged_receipt(discarded))


def test_stale_fence_discard_quarantines_pending_before_reporting_stale(
    tmp_path: Path,
) -> None:
    a17, b17, b18, _ = _generation_artifacts(tmp_path)
    runtime_generation = [7]
    registry = worker_universe.WorkerUniverseRegistry(
        runtime_generation=lambda: runtime_generation[0],
        context_generation=3,
    )
    active = registry.prepare((a17, b17))
    registry.confirm(active, _acknowledged_receipt(active))
    pending = registry.prepare((a17, b18))
    pending_registrations = {
        item.registration_name for item in pending.manifest.modules
    }
    runtime_generation[0] = 8

    with pytest.raises(ProtocolError, match="fence"):
        registry.discard(pending)

    assert registry.state is worker_universe.WorkerUniverseState.BROKEN
    assert registry._pending is None
    released = registry.teardown()
    assert pending_registrations <= set(released)
    assert len(released) == len(set(released))
    assert registry._handles == {}
    assert registry.teardown() == ()


def test_teardown_directly_from_preparing_includes_pending_registrations_once(
    tmp_path: Path,
) -> None:
    a17, b17, b18, _ = _generation_artifacts(tmp_path)
    registry = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    active = registry.prepare((a17, b17))
    registry.confirm(active, _acknowledged_receipt(active))
    pending = registry.prepare((a17, b18))
    pending_registrations = {
        item.registration_name for item in pending.manifest.modules
    }

    released = registry.teardown()

    assert pending_registrations <= set(released)
    assert len(released) == len(set(released))
    assert registry.state is worker_universe.WorkerUniverseState.CLOSED


def test_handles_and_operation_lease_ids_cannot_be_forged_or_replayed(
    tmp_path: Path,
) -> None:
    a17, b17, _, _ = _generation_artifacts(tmp_path)
    registry = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    candidate = registry.prepare((a17, b17))
    registry.confirm(candidate, _acknowledged_receipt(candidate))
    pin = registry.pin_active()

    with pytest.raises(ProtocolError, match="forged"):
        registry.release_generation(replace(candidate.handle))
    with pytest.raises(ProtocolError, match="forged"):
        registry.release_pin(replace(pin))

    registry.release_pin(pin)
    with pytest.raises(ProtocolError, match="stale|forged"):
        registry.release_pin(pin)
    registry.release_generation(candidate.handle)
    with pytest.raises(ProtocolError, match="stale|released"):
        registry.release_generation(candidate.handle)


def test_runtime_and_context_generation_sources_are_live_fences(tmp_path: Path) -> None:
    a17, b17, _, _ = _generation_artifacts(tmp_path)
    runtime_generation = [7]
    context_generation = [3]
    registry = worker_universe.WorkerUniverseRegistry(
        runtime_generation=lambda: runtime_generation[0],
        context_generation=lambda: context_generation[0],
    )
    candidate = registry.prepare((a17, b17))
    registry.confirm(candidate, _acknowledged_receipt(candidate))

    runtime_generation[0] = 8
    with pytest.raises(ProtocolError, match="fence"):
        registry.pin_active()
    runtime_generation[0] = 7
    context_generation[0] = 4
    with pytest.raises(ProtocolError, match="fence"):
        registry.prepare((a17, b17))

    with pytest.raises(ProtocolError, match="fence"):
        _ = registry.active_handle
    assert registry.teardown()


def test_g17_pin_survives_g18_and_g19_until_its_terminal_release(tmp_path: Path) -> None:
    a17, b17, b18, _ = _generation_artifacts(tmp_path)
    registry = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    g17 = registry.prepare((a17, b17))
    registry.confirm(g17, _acknowledged_receipt(g17))
    pin17 = registry.pin_active()
    g18 = registry.prepare((a17, b18))
    registry.confirm(g18, _acknowledged_receipt(g18))
    g19 = registry.prepare((a17,))
    registry.confirm(g19, _acknowledged_receipt(g19))
    b17_registration = next(
        item.registration_name
        for item in g17.manifest.modules
        if item.logical_name == "МодульБ"
    )

    registry.release_generation(g17.handle)
    registry.release_generation(g18.handle)
    registry.release_generation(g19.handle)

    assert pin17.handle is g17.handle
    assert pin17.manifest is g17.manifest
    assert registry.active_handle is g19.handle
    assert registry.registration_refcount(b17_registration) == 1
    released = registry.release_pin(pin17)
    assert b17_registration in released
    assert registry.registration_refcount(b17_registration) == 0
    assert g17.handle.generation not in registry._handles
    assert g18.handle.generation not in registry._handles


def test_candidate_rejects_an_export_subset_of_an_otherwise_valid_manifest(tmp_path: Path) -> None:
    a17, b17, _, _ = _generation_artifacts(tmp_path)
    registry = worker_universe.WorkerUniverseRegistry(runtime_generation=7, context_generation=3)
    with pytest.raises(ProtocolError, match="catalog"):
        registry.prepare((a17, b17), export_catalog=(WorkerExport(
            "МодульА.Рассчитать", "Рассчитать", receiver_module="МодульА"
        ),))


def test_operation_pin_owns_effective_notebook_lowering_catalog(tmp_path: Path) -> None:
    """The immutable generation record, not a runtime side map, owns aliases."""
    a17, _, _, _ = _generation_artifacts(tmp_path)
    registry = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    notebook_catalog = tuple(
        WorkerExport(
            export.method,
            export.method,
            receiver_module=a17.logical_name,
        )
        for export in a17.exports
    )

    candidate = registry.prepare((a17,), export_catalog=notebook_catalog)
    registry.confirm(candidate, _acknowledged_receipt(candidate))
    pin = registry.pin_active()

    assert candidate.export_catalog is notebook_catalog
    assert pin.export_catalog is notebook_catalog
    assert pin.export_catalog != pin.manifest.exports
    registry.release_pin(pin)


def test_outcome_unknown_pin_and_candidate_registrations_live_until_teardown(
    tmp_path: Path,
) -> None:
    a17, b17, b18, _ = _generation_artifacts(tmp_path)
    registry = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    g17 = registry.prepare((a17, b17))
    registry.confirm(g17, _acknowledged_receipt(g17))
    pin = registry.pin_active()
    g18 = registry.prepare((a17, b18))
    registry.mark_broken()

    candidate_registrations = {
        item.registration_name for item in g18.manifest.modules
    }
    with pytest.raises(ProtocolError, match="broken"):
        registry.release_pin(pin)
    with pytest.raises(ProtocolError, match="broken"):
        registry.prepare((a17, b17))
    with pytest.raises(ProtocolError, match="broken"):
        registry.pin_active()
    with pytest.raises(ProtocolError, match="broken"):
        registry.release_generation(g17.handle)
    with pytest.raises(ProtocolError, match="broken"):
        _ = registry.active_handle

    released = registry.teardown()
    assert tuple(sorted(released, key=str.casefold)) == released
    assert candidate_registrations <= set(released)
    assert registry.state is worker_universe.WorkerUniverseState.CLOSED
    assert all(registry.registration_refcount(name) == 0 for name in released)
    assert registry.teardown() == ()
    with pytest.raises(ProtocolError, match="closed"):
        registry.prepare((a17, b17))


def test_stale_fence_outcome_unknown_establishes_safety_before_reporting_stale(
    tmp_path: Path,
) -> None:
    a17, b17, b18, _ = _generation_artifacts(tmp_path)
    runtime_generation = [7]
    context_generation = [3]
    registry = worker_universe.WorkerUniverseRegistry(
        runtime_generation=lambda: runtime_generation[0],
        context_generation=lambda: context_generation[0],
    )
    active = registry.prepare((a17, b17))
    registry.confirm(active, _acknowledged_receipt(active))
    pin = registry.pin_active()
    pending = registry.prepare((a17, b18))
    all_known_registrations = {
        item.registration_name for item in active.manifest.modules
    } | {
        item.registration_name for item in pending.manifest.modules
    }
    runtime_generation[0] = 8

    with pytest.raises(ProtocolError, match="fence"):
        registry.retain_outcome_unknown(pin)

    assert registry.state is worker_universe.WorkerUniverseState.BROKEN
    assert all_known_registrations <= set(registry.teardown())


def test_explicit_outcome_unknown_operation_pin_cannot_release_before_teardown(
    tmp_path: Path,
) -> None:
    a17, b17, b18, _ = _generation_artifacts(tmp_path)
    registry = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    candidate = registry.prepare((a17, b17))
    registry.confirm(candidate, _acknowledged_receipt(candidate))
    pin = registry.pin_active()
    before = {
        item.registration_name: registry.registration_refcount(item.registration_name)
        for item in candidate.manifest.modules
    }
    pending = registry.prepare((a17, b18))
    pending_registrations = {
        item.registration_name for item in pending.manifest.modules
    }

    registry.retain_outcome_unknown(pin)

    assert registry.state is worker_universe.WorkerUniverseState.BROKEN
    assert {
        name: registry.registration_refcount(name) for name in before
    } == before
    with pytest.raises(ProtocolError, match="broken"):
        registry.release_pin(pin)
    assert set(before) | pending_registrations <= set(registry.teardown())


def test_concurrent_operation_pins_keep_old_generation_during_promotion(
    tmp_path: Path,
) -> None:
    a17, b17, b18, _ = _generation_artifacts(tmp_path)
    registry = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    g17 = registry.prepare((a17, b17))
    registry.confirm(g17, _acknowledged_receipt(g17))
    b17_registration = next(
        item.registration_name
        for item in g17.manifest.modules
        if item.logical_name == "МодульБ"
    )

    with ThreadPoolExecutor(max_workers=8) as executor:
        pins = tuple(executor.map(lambda _: registry.pin_active(), range(24)))
        g18 = registry.prepare((a17, b18))
        registry.confirm(g18, _acknowledged_receipt(g18))
        registry.release_generation(g17.handle)
        released = tuple(executor.map(registry.release_pin, pins))

    assert all(pin.handle is g17.handle for pin in pins)
    assert sum(b17_registration in names for names in released) == 1
    assert registry.registration_refcount(b17_registration) == 0


def test_pin_and_candidate_repr_redact_opaque_runtime_capabilities(tmp_path: Path) -> None:
    a17, b17, _, _ = _generation_artifacts(tmp_path)
    registry = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    candidate = registry.prepare((a17, b17))
    receipt = _acknowledged_receipt(candidate)
    root_key = receipt.root_key
    registry.confirm(candidate, receipt)
    pin = registry.pin_active()

    assert pin.lease_id not in repr(pin)
    assert root_key not in repr(pin)
    assert root_key not in repr(receipt)
    assert "<redacted>" in repr(pin)
    assert "worker_artifact" not in repr(candidate).casefold()
    assert str(tmp_path.resolve()) not in repr(candidate)


def test_confirmed_live_inventory_tracks_active_retained_and_pinned_generations(
    tmp_path: Path,
) -> None:
    """Break caught: pruning must not guess which old generations remain live."""
    a17, b17, a18, b18 = _generation_artifacts(tmp_path)
    registry = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    g17 = registry.prepare((a17, b17))
    registry.confirm(g17, _acknowledged_receipt(g17))
    pin = registry.pin_active()
    g18 = registry.prepare((a18, b18))
    registry.confirm(g18, _acknowledged_receipt(g18))
    registry.release_generation(g17.handle)

    retained = registry._confirmed_live_inventory()

    assert retained is not None
    assert retained.manifest_sha256s == frozenset(
        (g17.manifest.sha256, g18.manifest.sha256)
    )
    assert retained.artifact_identities == frozenset(
        (
            (item.logical_name.casefold(), item.revision, item.artifact_sha256)
            for item in (*g17.manifest.modules, *g18.manifest.modules)
        )
    )

    registry.release_pin(pin)
    current = registry._confirmed_live_inventory()

    assert current is not None
    assert current.manifest_sha256s == frozenset((g18.manifest.sha256,))
    assert current.artifact_identities == frozenset(
        (
            (item.logical_name.casefold(), item.revision, item.artifact_sha256)
            for item in g18.manifest.modules
        )
    )


def test_outcome_unknown_has_no_confirmed_pruning_inventory(tmp_path: Path) -> None:
    """Break caught: uncertain promotion state must never authorize eviction."""
    a17, b17, a18, b18 = _generation_artifacts(tmp_path)
    registry = worker_universe.WorkerUniverseRegistry(
        runtime_generation=7,
        context_generation=3,
    )
    g17 = registry.prepare((a17, b17))
    registry.confirm(g17, _acknowledged_receipt(g17))
    candidate = registry.prepare((a18, b18))
    registry.mark_broken(candidate)

    assert registry._confirmed_live_inventory() is None


def test_binary_cache_pruning_keeps_only_live_large_capsules() -> None:
    """Break caught: unique large binaries must remain O(live generations)."""
    cache = WorkerModuleArtifactCache()
    dead: list[ref[object]] = []
    current_key: WorkerModuleBinaryKey | None = None
    current_artifact = None

    for revision in range(1, 101):
        source = SOURCE.replace("41", str(revision))
        mapped = worker_epf._synthetic_worker_module_source(
            source,
            normalize=False,
        )
        artifact = server_worker._build_admitted_worker_artifact(
            logical_name="Worker",
            source_path=Path(f"source-{revision}.bsl"),
            artifact_path=Path(f"worker-{revision}.epf"),
            source_bytes=source.encode("utf-8"),
            artifact_bytes=(bytes((revision % 251,)) * (2 * 1024 * 1024)),
            source_bytes_sha256=source_sha256(source),
            mapped=mapped,
            visible_context_snapshot=None,
            expected_version=None,
            expected_value=None,
            exports=(WorkerExport("Worker.Рассчитать", "Рассчитать", receiver_module="Worker"),),
        )
        key = WorkerModuleBinaryKey(
            "Worker",
            artifact.source_sha256,
            "a" * 64,
            "notebook-worker-adapter-v1",
            "worker-epf-v1",
            "server-test",
        )
        cache._admit_capsule(worker_universe._mint_binary_capsule(key, artifact))
        cache._prune(frozenset((key,)))
        if current_artifact is not None:
            dead.append(ref(current_artifact))
        current_artifact = artifact
        current_key = key

    gc.collect()

    assert current_key is not None
    assert len(cache) == 1
    assert cache._lookup(current_key) is current_artifact
    assert all(item() is None for item in dead)


def _notebook_artifact(tmp_path: Path, *, logical_name: str = "Worker"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "notebook-worker.bsl"
    binary = tmp_path / "notebook-worker.epf"
    source.write_text(SOURCE, encoding="utf-8")
    binary.write_bytes(b"notebook-worker-binary")
    return build_worker_artifact(
        logical_name=logical_name,
        source_path=source,
        artifact_path=binary,
        exports=(WorkerExport("Рассчитать", "Рассчитать"),),
    )


def test_notebook_descriptor_preserves_binary_source_map_and_revision_identity(
    tmp_path: Path,
) -> None:
    """Notebook admission must not reparse, repackage, or replace bytes."""
    notebook = _notebook_artifact(tmp_path, logical_name="worker")

    descriptor = worker_module_artifact_from_notebook(
        notebook,
        revision=17,
        target_profile="server-test",
    )

    assert descriptor.logical_name == "Worker"
    assert descriptor.revision == 17
    assert descriptor.source_sha256 == notebook.source_sha256
    assert descriptor.artifact_sha256 == notebook.artifact_sha256
    assert descriptor.source_map_sha256 == notebook.source_map_sha256
    assert descriptor.worker_artifact.artifact_sha256 == notebook.artifact_sha256
    assert descriptor.dependency_bindings == ()
    assert descriptor.exports == (
        WorkerExport("Worker.Рассчитать", "Рассчитать", receiver_module="Worker"),
    )
    assert validate_worker_module_artifact(descriptor) == descriptor.exports


def test_notebook_descriptor_rejects_tamper_and_noncanonical_or_duplicate_exports(
    tmp_path: Path,
) -> None:
    """Notebook admission cannot launder forged artifacts."""
    notebook = _notebook_artifact(tmp_path)
    forged = replace(notebook, artifact_sha256="f" * 64)
    wrong_name = _notebook_artifact(tmp_path / "wrong", logical_name="Other")
    duplicate = replace(
        notebook,
        exports=(
            WorkerExport("Рассчитать", "Рассчитать"),
            WorkerExport("РАССЧИТАТЬ", "Рассчитать"),
        ),
    )

    for value in (forged, wrong_name, duplicate):
        with pytest.raises(ProtocolError, match="Notebook Worker admission"):
            worker_module_artifact_from_notebook(
                value,
                revision=1,
                target_profile="server-test",
            )


def test_notebook_descriptor_repr_never_exposes_source_binary_or_paths(
    tmp_path: Path,
) -> None:
    notebook = _notebook_artifact(tmp_path)
    descriptor = worker_module_artifact_from_notebook(
        notebook,
        revision=1,
        target_profile="server-test",
    )

    rendered = repr(descriptor)
    assert SOURCE not in rendered
    assert "notebook-worker-binary" not in rendered
    assert str(tmp_path.resolve()) not in rendered
