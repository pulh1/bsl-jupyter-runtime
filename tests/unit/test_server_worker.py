import base64
from collections import deque
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path

import pytest

from onec_runtime.bsl import (
    CommonModuleCatalogSnapshot,
    DiagnosticStage,
    MappingConfidence,
    VisibleSourceContext,
    WorkerExport,
    mapped_visible_source,
)
import onec_runtime.bsl.module_universe as module_universe
from onec_runtime.bsl.module_universe import (
    WorkerModuleUnit,
    analyze_worker_module,
    lower_worker_module,
)
from onec_runtime.bsl.source_maps import (
    MappedSource,
    MappingRelation,
    SourceArtifactKind,
    SourceArtifactRef,
    SourceMap,
    SourceMapSegment,
    SourceSpan,
    SourceTransformBuilder,
    SourceUnitKind,
    SourceUnitRef,
    source_sha256,
)
from onec_runtime.bsl.parser_target import PythonParserTarget
from onec_runtime.config import RuntimeConfig
from onec_runtime.errors import (
    BslExecutionError,
    ProtocolError,
)
from onec_runtime.privacy import public_artifact_value
import onec_runtime.server_worker as server_worker
from onec_runtime.server_worker import (
    NotebookWorkerArtifactBuilder,
    WorkerArtifact,
    build_worker_artifact,
)


def test_worker_artifact_stage_instruction_has_exact_identity_without_active_swap() -> None:
    artifact_sha256 = "a" * 64

    stage = server_worker.WorkerArtifactStage(
        logical_name="МодульА",
        artifact_sha256=artifact_sha256,
        registration_name="OnecRuntime_01234567_aaaaaaaaaaaaaaaa",
        phase="connect",
        instruction=server_worker.stage_worker_module_instruction(
            b"epf-payload",
            logical_name="МодульА",
            artifact_sha256=artifact_sha256,
            registration_name="OnecRuntime_01234567_aaaaaaaaaaaaaaaa",
        ),
    )

    assert stage.phase == "connect"
    assert artifact_sha256 in stage.instruction
    assert sha256("модульа".encode("utf-8")).hexdigest() in stage.instruction
    assert "RuntimeWorkerActiveGeneration" not in stage.instruction
    assert base64.b64encode(b"epf-payload").decode("ascii") not in repr(stage)
    PythonParserTarget.from_generated().parse(stage.instruction, "Модуль")


def test_single_stage_instruction_keeps_legacy_marker_and_name_result() -> None:
    registration = "OnecRuntime_01234567_aaaaaaaaaaaaaaaa"

    source = server_worker.stage_worker_module_instruction(
        b"epf-payload",
        logical_name="МодульА",
        artifact_sha256="a" * 64,
        registration_name=registration,
    )

    assert "onec-worker-artifact-stage=" in source
    assert f'АдресАртефактаWorker, "{registration}", Ложь' in source
    assert "Результат = ИмяАртефактаWorker;" in source


def test_worker_artifact_stage_diagnostic_removes_only_verified_control_header() -> None:
    header = (
        "onec-worker-artifact-stage="
        f"artifact_sha256={'a' * 64};"
        f"logical_name_sha256={'b' * 64};"
        "phase=connect;boundary=connect"
    )
    diagnostic = "Ошибка компиляции\r\n{Модуль(17,9)}: Неизвестный оператор"
    error = BslExecutionError(f"transport prefix: {header}\r\n{diagnostic}")

    assert server_worker._worker_reload_platform_message(error) == diagnostic

    malformed = BslExecutionError(
        f"{header.replace('a' * 64, 'raw-source')}\n{diagnostic}"
    )
    assert server_worker._worker_reload_platform_message(malformed) == str(malformed)


@pytest.mark.parametrize("boundary", ("upload", "connect", "create"))
def test_worker_artifact_stage_failure_exposes_exact_artifact_phase(
    boundary: str,
) -> None:
    artifact_sha256 = "a" * 64
    logical_name_sha256 = "b" * 64
    error = BslExecutionError(
        "onec-worker-artifact-stage="
        f"artifact_sha256={artifact_sha256};"
        f"logical_name_sha256={logical_name_sha256};"
        f"phase={'create' if boundary == 'create' else 'connect'};boundary={boundary}\n"
        "{<Неизвестный модуль>(1,1)}: failure"
    )

    identity = server_worker.worker_artifact_stage_failure(error)

    assert identity is not None
    assert (identity.artifact_sha256, identity.phase) == (
        artifact_sha256,
        boundary,
    )


def test_worker_artifact_stage_public_serialization_cannot_reach_epf_payload() -> None:
    payload = b"private-epf-payload"
    artifact_sha256 = "a" * 64
    stage = server_worker.WorkerArtifactStage(
        "МодульА",
        artifact_sha256,
        "OnecRuntime_01234567_aaaaaaaaaaaaaaaa",
        "connect",
        server_worker.stage_worker_module_instruction(
            payload,
            logical_name="МодульА",
            artifact_sha256=artifact_sha256,
            registration_name="OnecRuntime_01234567_aaaaaaaaaaaaaaaa",
        ),
    )

    public = public_artifact_value(stage)

    assert isinstance(public, dict)
    assert "instruction" not in public
    assert base64.b64encode(payload).decode("ascii") not in json.dumps(
        public,
        ensure_ascii=False,
    )


def notebook_builder(tmp_path: Path) -> NotebookWorkerArtifactBuilder:
    platform = tmp_path / "platform"
    platform.mkdir()
    for executable in ("1cv8.exe", "1cv8c.exe", "dbgs.exe"):
        (platform / executable).write_bytes(b"stub")
    return NotebookWorkerArtifactBuilder(RuntimeConfig(tmp_path, platform))


def mapped_worker_candidate() -> tuple[MappedSource, VisibleSourceContext]:
    first = "Процедура Первая() Экспорт\nКонецПроцедуры;\n"
    second = "Процедура Вторая() Экспорт\nКонецПроцедуры;"
    first_unit = SourceUnitRef(
        SourceUnitKind.MODULE,
        "module-first",
        2,
        source_sha256(first),
    )
    second_unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL,
        "cell-mixed",
        3,
        source_sha256(second),
    )
    text = first + second
    generated = SourceArtifactRef(
        SourceArtifactKind.WORKER_PROJECTION,
        source_sha256(text),
        len(text),
        "lf",
    )
    source_map = SourceMap(
        generated,
        (
            SourceMapSegment(
                SourceSpan(0, len(first)),
                first_unit,
                SourceSpan(0, len(first)),
                MappingRelation.EXACT,
            ),
            SourceMapSegment(
                SourceSpan(len(first), len(text)),
                second_unit,
                SourceSpan(0, len(second)),
                MappingRelation.EXACT,
            ),
        ),
    )
    return (
        MappedSource(text, generated, source_map),
        VisibleSourceContext({first_unit: first, second_unit: second}),
    )


def semantic_admitted_worker_candidate():
    source = "Функция Посчитать() Экспорт\n    Возврат 1;\nКонецФункции\n"
    unit_ref = SourceUnitRef(
        SourceUnitKind.MODULE,
        "МодульРасчета",
        1,
        source_sha256(source),
    )
    unit = WorkerModuleUnit(
        "МодульРасчета",
        "module",
        1,
        mapped_visible_source(source, unit_ref),
    )
    analyzed = analyze_worker_module(
        unit,
        CommonModuleCatalogSnapshot.create(
            profile="server-test",
            preprocessor_profile="server",
            revision=1,
            modules=(),
        ),
        PythonParserTarget.from_generated(),
    )
    lowered = lower_worker_module(analyzed)
    return (
        lowered,
        VisibleSourceContext({unit_ref: source}),
        (
            WorkerExport(
                "МодульРасчета.Посчитать",
                "Посчитать",
                receiver_module="МодульРасчета",
            ),
        ),
    )


def test_notebook_worker_artifact_retains_private_map_and_public_hash(
    tmp_path: Path,
) -> None:
    """Break caught: Worker packaging must not discard or publish its exact map."""
    mapped, context = mapped_worker_candidate()
    worker = notebook_builder(tmp_path)(
        mapped,
        (
            WorkerExport("Первая", "Первая"),
            WorkerExport("Вторая", "Вторая"),
        ),
        visible_source_context=context,
    )

    assert worker.source_provenance == server_worker.WorkerSourceProvenance(
        worker.source_sha256,
        worker.source_map_sha256,
    )
    assert worker.source_map_sha256 is not None
    assert len(worker.source_map_sha256) == 64
    assert worker._admission is not None
    admitted_source, admitted_bytes, proof, admitted_map = worker._admission.contents(
        worker
    )
    capability = server_worker._worker_artifact_capability(worker)
    assert capability is not None
    assert capability.source_path is not None
    assert capability.artifact_path is not None
    assert admitted_source == capability.source_path.read_bytes()
    assert admitted_bytes == capability.artifact_path.read_bytes()
    assert len(proof) == 32
    assert admitted_map.artifact.kind is SourceArtifactKind.WORKER_MODULE
    assert admitted_map.artifact.source_sha256 == worker.source_sha256
    assert admitted_map.source_map_sha256 == worker.source_map_sha256

    public_json = json.dumps(public_artifact_value(worker), ensure_ascii=False)
    generic_json = json.dumps(asdict(worker), ensure_ascii=False, default=str)
    for private_value in (
        mapped.text,
        str(capability.artifact_path),
        str(capability.source_path),
        "module-first",
        "cell-mixed",
    ):
        assert private_value not in public_json
        assert private_value not in generic_json
    assert "_admission" not in public_json


def test_semantic_admission_packaging_reads_generated_source_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lowered, context, exports = semantic_admitted_worker_candidate()
    admission_factory = getattr(module_universe, "worker_semantic_admission", None)
    assert callable(admission_factory)
    original_read_bytes = Path.read_bytes
    source_reads = 0

    def counted_read_bytes(path: Path) -> bytes:
        nonlocal source_reads
        if (
            path.name == "ObjectModule.bsl"
            and "notebook-workers" in path.parts
        ):
            source_reads += 1
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", counted_read_bytes)

    worker = notebook_builder(tmp_path)(
        lowered.mapped_source,
        exports,
        visible_source_context=context,
        semantic_admission=admission_factory(
            lowered,
            packer_identity="worker-epf-v1",
        ),
        semantic_packer_identity="worker-epf-v1",
    )

    assert source_reads == 1
    assert server_worker.validate_production_worker_artifact(worker) == exports


def test_rebind_shares_immutable_binary_snapshot_and_ignores_later_file_edits(
    tmp_path: Path,
) -> None:
    """Break caught: a revision rebind must not regain authority from helper files."""
    lowered, context, exports = semantic_admitted_worker_candidate()
    admission_factory = module_universe.worker_semantic_admission
    worker = notebook_builder(tmp_path)(
        lowered.mapped_source,
        exports,
        visible_source_context=context,
        semantic_admission=admission_factory(
            lowered,
            packer_identity="worker-epf-v1",
        ),
        semantic_packer_identity="worker-epf-v1",
    )
    original = server_worker._validated_admitted_snapshot(worker)
    rebound = server_worker.rebind_worker_artifact_binary(
        worker,
        logical_name="МодульРасчета",
        mapped=lowered.mapped_source,
        visible_source_context=context,
        exports=exports,
    )
    rebound_snapshot = server_worker._validated_admitted_snapshot(rebound)
    capability = server_worker._worker_artifact_capability(worker)
    assert capability is not None
    assert capability.source_path is not None
    assert capability.artifact_path is not None

    capability.source_path.write_bytes(b"changed helper source")
    capability.artifact_path.write_bytes(b"changed helper artifact")
    admitted_after_edits = server_worker._validated_admitted_snapshot(rebound)
    stage = server_worker.stage_worker_module_instruction(
        admitted_after_edits.artifact_bytes,
        logical_name="МодульРасчета",
        artifact_sha256=rebound.artifact_sha256,
        registration_name="OnecRuntime_01234567_aaaaaaaaaaaaaaaa",
    )

    assert rebound_snapshot.binary is original.binary
    assert admitted_after_edits.source_bytes == original.source_bytes
    assert admitted_after_edits.artifact_bytes == original.artifact_bytes
    assert admitted_after_edits.mapped_source is not lowered.mapped_source
    assert (
        admitted_after_edits.mapped_source.source_map.to_manifest()
        == lowered.mapped_source.source_map.to_manifest()
    )
    assert base64.b64encode(original.artifact_bytes).decode("ascii") in stage
    private_values = (
        lowered.mapped_source.text,
        str(capability.source_path),
        str(capability.artifact_path),
        original.artifact_bytes[:16].hex(),
    )
    for view in (repr(original), repr(original.binary), repr(rebound_snapshot)):
        assert all(value not in view for value in private_values)


@pytest.mark.parametrize(
    "mutation",
    ("flattened_span", "flattened_reference", "lineage", "local_map"),
)
def test_admitted_snapshot_owns_independent_nested_mapped_graph(
    tmp_path: Path,
    mutation: str,
) -> None:
    """Break caught: nested mutations through the lowering graph cannot alter diagnostics."""
    lowered, context, exports = semantic_admitted_worker_candidate()
    worker = notebook_builder(tmp_path)(
        lowered.mapped_source,
        exports,
        visible_source_context=context,
        semantic_admission=module_universe.worker_semantic_admission(
            lowered,
            packer_identity="worker-epf-v1",
        ),
        semantic_packer_identity="worker-epf-v1",
    )
    admitted = server_worker._validated_admitted_snapshot(worker)
    flattened_before = admitted.mapped_source.source_map.to_manifest()
    lineage_before = admitted.mapped_source.lineage_manifest()
    try:
        local_before = admitted.mapped_source.local_source_map.to_manifest()
    except ValueError:
        local_before = None

    if mutation == "flattened_span":
        segment = lowered.mapped_source.source_map.segments[0]
        assert segment.origin is not None
        object.__setattr__(
            segment.origin,
            "start",
            segment.origin.start + 1,
        )
    elif mutation == "flattened_reference":
        reference = lowered.mapped_source.source_map.segments[0].origin_ref
        assert isinstance(reference, SourceUnitRef)
        object.__setattr__(reference, "unit_id", "ПодмененныйМодуль")
    elif mutation == "lineage":
        segment = lowered.mapped_source.lineage[0].segments[0]
        object.__setattr__(segment, "synthetic_region", "tampered-lineage")
    else:
        segment = lowered.mapped_source.local_source_map.segments[0]
        object.__setattr__(segment, "synthetic_region", "tampered-local-map")

    admitted_after = server_worker._validated_admitted_snapshot(worker)

    assert admitted_after.mapped_source is not lowered.mapped_source
    assert admitted_after.mapped_source.source_map.to_manifest() == flattened_before
    assert admitted_after.mapped_source.lineage_manifest() == lineage_before
    if local_before is not None:
        assert admitted_after.mapped_source.local_source_map.to_manifest() == local_before


def test_nested_mapped_graph_mutation_between_semantic_validation_and_mint_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: a valid semantic proof cannot admit a later in-place map edit."""
    lowered, context, exports = semantic_admitted_worker_candidate()
    import onec_runtime.worker_epf as worker_epf

    original_build = worker_epf.build_worker_epf

    def mutate_before_snapshot(source: str, output_path: Path) -> Path:
        segment = lowered.mapped_source.source_map.segments[0]
        assert segment.origin is not None
        object.__setattr__(segment.origin, "start", segment.origin.start + 1)
        return original_build(source, output_path)

    monkeypatch.setattr(worker_epf, "build_worker_epf", mutate_before_snapshot)

    with pytest.raises(
        ProtocolError,
        match="Worker module artifact admission is invalid",
    ):
        notebook_builder(tmp_path)(
            lowered.mapped_source,
            exports,
            visible_source_context=context,
            semantic_admission=module_universe.worker_semantic_admission(
                lowered,
                packer_identity="worker-epf-v1",
            ),
            semantic_packer_identity="worker-epf-v1",
        )


@pytest.mark.parametrize(
    "mutation",
    ("span", "reference", "lineage", "local_map"),
)
def test_worker_diagnostic_descriptor_mutation_cannot_change_admitted_source_map(
    tmp_path: Path,
    mutation: str,
) -> None:
    """Break caught: retained diagnostics must not mutate the admitted map."""
    lowered, context, exports = semantic_admitted_worker_candidate()
    worker = notebook_builder(tmp_path)(
        lowered.mapped_source,
        exports,
        visible_source_context=context,
        semantic_admission=module_universe.worker_semantic_admission(
            lowered,
            packer_identity="worker-epf-v1",
        ),
        semantic_packer_identity="worker-epf-v1",
    )
    admitted = server_worker._validated_admitted_snapshot(worker)
    original_manifest = admitted.mapped_source.source_map.to_manifest()
    original_lineage = admitted.mapped_source.lineage_manifest()
    original_local_map = admitted.mapped_source.local_source_map.to_manifest()

    def diagnostic():
        assert worker.source_map_sha256 is not None
        return server_worker.worker_artifact_diagnostic_source(
            worker,
            logical_name="МодульРасчета",
            revision=1,
            registration_name="OnecRuntime_01234567_aaaaaaaaaaaaaaaa",
            manifest_sha256="b" * 64,
            artifact_sha256=worker.artifact_sha256,
            source_map_sha256=worker.source_map_sha256,
        )

    retained = diagnostic()
    segment = next(
        item
        for item in retained.mapped_source.source_map.segments
        if item.origin is not None and isinstance(item.origin_ref, SourceUnitRef)
    )
    if mutation == "span":
        assert segment.origin is not None
        object.__setattr__(segment.origin, "start", segment.origin.start + 1)
    elif mutation == "reference":
        assert isinstance(segment.origin_ref, SourceUnitRef)
        object.__setattr__(segment.origin_ref, "unit_id", "ПодмененныйМодуль")
    elif mutation == "lineage":
        lineage_segment = retained.mapped_source.lineage[0].segments[0]
        object.__setattr__(
            lineage_segment,
            "synthetic_region",
            "tampered-outward-lineage",
        )
    else:
        local_segment = retained.mapped_source.local_source_map.segments[0]
        object.__setattr__(
            local_segment,
            "synthetic_region",
            "tampered-outward-local-map",
        )

    assert server_worker.validate_production_worker_artifact(worker) == exports
    admitted_after = server_worker._validated_admitted_snapshot(worker)
    future = diagnostic()

    assert retained.mapped_source is not admitted.mapped_source
    assert admitted_after.mapped_source.source_map.to_manifest() == original_manifest
    assert admitted_after.mapped_source.lineage_manifest() == original_lineage
    assert (
        admitted_after.mapped_source.local_source_map.to_manifest()
        == original_local_map
    )
    assert future.mapped_source is not retained.mapped_source
    assert future.mapped_source is not admitted_after.mapped_source
    assert future.mapped_source.source_map.to_manifest() == original_manifest
    assert future.mapped_source.lineage_manifest() == original_lineage
    assert future.mapped_source.local_source_map.to_manifest() == original_local_map


@pytest.mark.parametrize("adapter", ("capability", "validated"))
def test_admission_contents_adapter_cannot_mutate_admitted_source_map(
    tmp_path: Path,
    adapter: str,
) -> None:
    """Break caught: compatibility content adapters must not leak the admitted map."""
    lowered, context, exports = semantic_admitted_worker_candidate()
    worker = notebook_builder(tmp_path)(
        lowered.mapped_source,
        exports,
        visible_source_context=context,
        semantic_admission=module_universe.worker_semantic_admission(
            lowered,
            packer_identity="worker-epf-v1",
        ),
        semantic_packer_identity="worker-epf-v1",
    )
    admitted = server_worker._validated_admitted_snapshot(worker)
    original_manifest = admitted.mapped_source.source_map.to_manifest()
    if adapter == "capability":
        capability = worker._admission
        assert capability is not None
        outward = capability.contents(worker)[3]
    else:
        outward = server_worker._validated_admission_contents(worker)[3]

    reference = next(
        item.origin_ref
        for item in outward.source_map.segments
        if isinstance(item.origin_ref, SourceUnitRef)
    )
    assert isinstance(reference, SourceUnitRef)
    object.__setattr__(reference, "unit_id", "ПодмененныйМодуль")

    assert server_worker.validate_production_worker_artifact(worker) == exports
    admitted_after = server_worker._validated_admitted_snapshot(worker)
    assert outward is not admitted.mapped_source
    assert admitted_after.mapped_source.source_map.to_manifest() == original_manifest


def test_semantic_admission_rejects_changed_path_readback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lowered, context, exports = semantic_admitted_worker_candidate()
    admission_factory = getattr(module_universe, "worker_semantic_admission", None)
    assert callable(admission_factory)
    original_read_bytes = Path.read_bytes
    import onec_runtime.worker_epf as worker_epf

    original_native_packer = worker_epf.build_worker_epf
    native_packer_calls = 0

    def counted_native_packer(*args: object, **kwargs: object) -> None:
        nonlocal native_packer_calls
        native_packer_calls += 1
        original_native_packer(*args, **kwargs)

    def changed_source_readback(path: Path) -> bytes:
        if path.name == "ObjectModule.bsl":
            return b"forged generated source"
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", changed_source_readback)
    monkeypatch.setattr(worker_epf, "build_worker_epf", counted_native_packer)

    with pytest.raises(
        ProtocolError,
        match="Worker module artifact admission is invalid",
    ):
        notebook_builder(tmp_path)(
            lowered.mapped_source,
            exports,
            visible_source_context=context,
            semantic_admission=admission_factory(
                lowered,
                packer_identity="worker-epf-v1",
            ),
            semantic_packer_identity="worker-epf-v1",
        )

    assert native_packer_calls == 0


def test_legacy_final_worker_module_still_normalizes_crlf_before_packaging(
    tmp_path: Path,
) -> None:
    source = "Функция Посчитать() Экспорт\r\nВозврат 1;\r\nКонецФункции\r\n"
    unit_ref = SourceUnitRef(
        SourceUnitKind.MODULE,
        "МодульРасчета",
        1,
        source_sha256(source),
    )
    visible = mapped_visible_source(source, unit_ref)
    transform = SourceTransformBuilder(visible)
    transform.copy(SourceSpan(0, len(source)))
    final_source = transform.build(SourceArtifactKind.WORKER_MODULE)

    worker = notebook_builder(tmp_path)(
        final_source,
        (WorkerExport("Посчитать", "Посчитать"),),
        visible_source_context=VisibleSourceContext({unit_ref: source}),
    )
    admitted_map = server_worker._validated_admission_contents(worker)[3]

    assert "\r" not in admitted_map.text
    assert admitted_map.text == source.replace("\r\n", "\n")
    mapped = admitted_map.source_map.map_offset(admitted_map.text.index("Возврат"))
    assert mapped.origin_span is not None
    assert mapped.origin_span.start == source.index("Возврат")


def test_legacy_final_worker_module_still_drops_generation_identity(
    tmp_path: Path,
) -> None:
    source = "Функция Посчитать() Экспорт\nВозврат 1;\nКонецФункции\n"
    unit_ref = SourceUnitRef(
        SourceUnitKind.MODULE,
        "МодульРасчета",
        1,
        source_sha256(source),
    )
    visible = mapped_visible_source(source, unit_ref)
    transform = SourceTransformBuilder(visible)
    transform.copy(SourceSpan(0, len(source)))
    final_source = transform.build(
        SourceArtifactKind.WORKER_MODULE,
        worker_generation=19,
        worker_manifest_sha256="1" * 64,
    )

    worker = notebook_builder(tmp_path)(
        final_source,
        (WorkerExport("Посчитать", "Посчитать"),),
        visible_source_context=VisibleSourceContext({unit_ref: source}),
    )
    admitted_map = server_worker._validated_admission_contents(worker)[3]

    assert admitted_map.artifact.worker_generation is None
    assert admitted_map.artifact.worker_manifest_sha256 is None
    assert all(
        source_map.generated.worker_generation is None
        and source_map.generated.worker_manifest_sha256 is None
        for source_map in admitted_map.lineage
    )


def test_worker_artifact_generic_serializers_cannot_reach_private_capability(
    tmp_path: Path,
) -> None:
    """Break caught: public dataclass traversal must contain allowlisted fields only."""
    source_path = tmp_path / "private-source-marker.bsl"
    source_text = (
        'Функция Версия() Экспорт\nВозврат "private-version-marker";\n'
        "КонецФункции;"
    )
    source_path.write_text(source_text, encoding="utf-8")
    artifact_path = tmp_path / "private-artifact-marker.epf"
    artifact_bytes = b"private-artifact-bytes-marker"
    artifact_path.write_bytes(artifact_bytes)
    worker = build_worker_artifact(
        logical_name="Worker",
        source_path=source_path,
        artifact_path=artifact_path,
        expected_version="private-expected-version-marker",
        expected_value=791357,
        exports=(WorkerExport("Версия", "Версия"),),
    )
    copied = deepcopy(worker)
    views = (
        repr(worker),
        json.dumps(worker, ensure_ascii=False, default=str),
        json.dumps(asdict(worker), ensure_ascii=False, default=str),
        repr(copied),
        json.dumps(copied, ensure_ascii=False, default=str),
        json.dumps(asdict(copied), ensure_ascii=False, default=str),
        json.dumps(public_artifact_value(worker), ensure_ascii=False, default=str),
    )
    private_values = (
        str(source_path.resolve()),
        str(artifact_path.resolve()),
        source_text,
        artifact_bytes.decode("ascii"),
        "private-expected-version-marker",
        "791357",
    )

    assert set(asdict(worker)) == {
        "logical_name",
        "source_sha256",
        "artifact_sha256",
        "exports",
        "source_provenance",
    }
    assert copied._admission is None
    for private_value in private_values:
        for view in views:
            assert private_value not in view


def test_worker_artifact_public_constructor_rejects_private_capability_values(
    tmp_path: Path,
) -> None:
    """Break caught: private paths cannot enter through the public artifact API."""
    private_path = tmp_path / "private-worker.epf"

    with pytest.raises(TypeError, match="unexpected keyword argument 'path'"):
        WorkerArtifact(
            logical_name="Worker",
            source_sha256="a" * 64,
            artifact_sha256="b" * 64,
            path=private_path,
        )


def test_notebook_worker_builder_rejects_plain_string_source(
    tmp_path: Path,
) -> None:
    """Break caught: production Worker admission must never synthesize origin."""
    builder = notebook_builder(tmp_path)

    with pytest.raises(TypeError, match="MappedSource"):
        builder(
            "Функция Версия() Экспорт\nКонецФункции;",
            (WorkerExport("Версия", "Версия"),),
            visible_source_context=VisibleSourceContext({}),
        )

    assert not (tmp_path / ".runtime").exists()


def test_notebook_worker_builder_requires_visible_context(
    tmp_path: Path,
) -> None:
    """Break caught: a production mapped candidate cannot omit visible text proof."""
    mapped, _ = mapped_worker_candidate()

    with pytest.raises(ProtocolError, match="visible source context"):
        notebook_builder(tmp_path)(
            mapped,
            (
                WorkerExport("Первая", "Первая"),
                WorkerExport("Вторая", "Вторая"),
            ),
            visible_source_context=None,
        )

    assert not (tmp_path / ".runtime").exists()


def test_worker_source_provenance_reserves_future_generation_fields() -> None:
    provenance = server_worker.WorkerSourceProvenance("a" * 64, "b" * 64)

    assert provenance.worker_generation is None
    assert provenance.worker_manifest_sha256 is None


def test_worker_map_hash_mismatch_is_rejected_before_target_mutation(
    tmp_path: Path,
) -> None:
    mapped, context = mapped_worker_candidate()
    worker = notebook_builder(tmp_path)(
        mapped,
        (
            WorkerExport("Первая", "Первая"),
            WorkerExport("Вторая", "Вторая"),
        ),
        visible_source_context=context,
    )
    object.__setattr__(
        worker,
        "source_provenance",
        server_worker.WorkerSourceProvenance(worker.source_sha256, "f" * 64),
    )
    with pytest.raises(ProtocolError, match="source map"):
        server_worker.validate_production_worker_artifact(worker)


def test_worker_future_provenance_mismatch_is_rejected_before_target_mutation(
    tmp_path: Path,
) -> None:
    mapped, context = mapped_worker_candidate()
    worker = notebook_builder(tmp_path)(
        mapped,
        (
            WorkerExport("Первая", "Первая"),
            WorkerExport("Вторая", "Вторая"),
        ),
        visible_source_context=context,
    )
    assert worker.source_map_sha256 is not None
    object.__setattr__(
        worker,
        "source_provenance",
        server_worker.WorkerSourceProvenance(
            worker.source_sha256,
            worker.source_map_sha256,
            worker_generation=7,
            worker_manifest_sha256="e" * 64,
        ),
    )
    with pytest.raises(ProtocolError, match="source map provenance"):
        server_worker.validate_production_worker_artifact(worker)


def test_notebook_worker_rejects_mismatched_visible_context_before_packaging(
    tmp_path: Path,
) -> None:
    mapped, _ = mapped_worker_candidate()
    unrelated_source = "Процедура Другая() Экспорт\nКонецПроцедуры;"
    unrelated = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL,
        "other-cell",
        1,
        source_sha256(unrelated_source),
    )

    with pytest.raises(ProtocolError, match="visible source context"):
        notebook_builder(tmp_path)(
            mapped,
            (
                WorkerExport("Первая", "Первая"),
                WorkerExport("Вторая", "Вторая"),
            ),
            visible_source_context=VisibleSourceContext(
                {unrelated: unrelated_source}
            ),
        )

    assert not (tmp_path / ".runtime").exists()


def test_notebook_worker_rejects_same_identity_with_wrong_visible_hash(
    tmp_path: Path,
) -> None:
    """Break caught: context lookup must bind source hash, not only unit identity."""
    mapped, _ = mapped_worker_candidate()
    first_segment, second_segment = mapped.source_map.segments
    assert isinstance(first_segment.origin_ref, SourceUnitRef)
    assert isinstance(second_segment.origin_ref, SourceUnitRef)
    first_source = mapped.text[
        first_segment.generated.start : first_segment.generated.end
    ]
    wrong_second_source = "Процедура Подмена() Экспорт\nКонецПроцедуры;"
    wrong_second_ref = SourceUnitRef(
        second_segment.origin_ref.kind,
        second_segment.origin_ref.unit_id,
        second_segment.origin_ref.revision,
        source_sha256(wrong_second_source),
    )
    context = VisibleSourceContext(
        {
            first_segment.origin_ref: first_source,
            wrong_second_ref: wrong_second_source,
        }
    )

    with pytest.raises(ProtocolError, match="visible source context"):
        notebook_builder(tmp_path)(
            mapped,
            (
                WorkerExport("Первая", "Первая"),
                WorkerExport("Вторая", "Вторая"),
            ),
            visible_source_context=context,
        )

    assert not (tmp_path / ".runtime").exists()


def test_notebook_worker_validates_every_multi_span_context_boundary(
    tmp_path: Path,
) -> None:
    """Break caught: offset-zero presence cannot admit a later invalid source span."""
    mapped, context = mapped_worker_candidate()
    first_segment, second_segment = mapped.source_map.segments
    assert isinstance(second_segment.origin_ref, SourceUnitRef)
    assert second_segment.origin is not None
    split = second_segment.generated.end - 1
    second_length = second_segment.origin.end
    invalid_map = SourceMap(
        mapped.artifact,
        (
            first_segment,
            SourceMapSegment(
                SourceSpan(second_segment.generated.start, split),
                second_segment.origin_ref,
                SourceSpan(0, second_length - 1),
                MappingRelation.EXACT,
            ),
            SourceMapSegment(
                SourceSpan(split, second_segment.generated.end),
                second_segment.origin_ref,
                SourceSpan(second_length, second_length + 1),
                MappingRelation.EXACT,
            ),
        ),
    )
    invalid = MappedSource(mapped.text, mapped.artifact, invalid_map)

    with pytest.raises(ProtocolError, match="visible source context"):
        notebook_builder(tmp_path)(
            invalid,
            (
                WorkerExport("Первая", "Первая"),
                WorkerExport("Вторая", "Вторая"),
            ),
            visible_source_context=context,
        )

    assert not (tmp_path / ".runtime").exists()


def test_notebook_worker_validates_visible_anchor_span_boundaries(
    tmp_path: Path,
) -> None:
    """Break caught: synthetic anchors require the same full-span context fence."""
    mapped, context = mapped_worker_candidate()
    second_segment = mapped.source_map.segments[1]
    assert isinstance(second_segment.origin_ref, SourceUnitRef)
    assert second_segment.origin is not None
    invalid_map = SourceMap(
        mapped.artifact,
        (
            SourceMapSegment(
                SourceSpan(0, len(mapped.text)),
                None,
                None,
                MappingRelation.SYNTHETIC,
                "invalid_worker_anchor",
                second_segment.origin_ref,
                SourceSpan(second_segment.origin.end, second_segment.origin.end + 1),
            ),
        ),
    )

    with pytest.raises(ProtocolError, match="visible source context"):
        notebook_builder(tmp_path)(
            MappedSource(mapped.text, mapped.artifact, invalid_map),
            (
                WorkerExport("Первая", "Первая"),
                WorkerExport("Вторая", "Вторая"),
            ),
            visible_source_context=context,
        )

    assert not (tmp_path / ".runtime").exists()


@pytest.mark.parametrize(
    "boundary",
    ("diagnostic", "diagnostic_context", "context_snapshot"),
)
@pytest.mark.parametrize("mutation", ("mapping", "line_index"))
def test_worker_outward_visible_context_mutation_is_isolated(
    tmp_path: Path,
    boundary: str,
    mutation: str,
) -> None:
    """Break caught: retained diagnostic contexts must not poison admission."""
    mapped, context = mapped_worker_candidate()
    exports = (
        WorkerExport("Первая", "Первая"),
        WorkerExport("Вторая", "Вторая"),
    )
    worker = notebook_builder(tmp_path)(
        mapped,
        exports,
        visible_source_context=context,
    )
    assert worker._admission is not None
    admitted = server_worker._validated_admitted_snapshot(worker)
    internal_snapshot = admitted.visible_context_snapshot
    assert internal_snapshot is not None
    first = mapped.source_map.segments[0].origin_ref
    assert isinstance(first, SourceUnitRef)
    key = (first.kind, first.unit_id, first.revision)
    internal_index = internal_snapshot.context._indices[key][1]  # type: ignore[attr-defined]
    line_starts = internal_index._line_starts  # type: ignore[attr-defined]
    assert len(line_starts) >= 2
    mapped_offset = line_starts[1]
    expected_location = internal_snapshot.context.line_column(first, mapped_offset)

    def diagnostic():
        assert worker.source_map_sha256 is not None
        return server_worker.worker_artifact_diagnostic_source(
            worker,
            logical_name="Worker",
            revision=1,
            registration_name="OnecRuntime_01234567_aaaaaaaaaaaaaaaa",
            manifest_sha256="b" * 64,
            artifact_sha256=worker.artifact_sha256,
            source_map_sha256=worker.source_map_sha256,
        )

    def outward_context():
        if boundary == "diagnostic":
            owner = diagnostic()
            value = owner.visible_source_context
        elif boundary == "diagnostic_context":
            owner = worker._admission.diagnostic_context(worker)
            value = owner
        else:
            owner = worker._admission.context_snapshot(worker)
            assert owner is not None
            value = owner.context
        assert value is not None
        return value, owner

    outward, outward_owner = outward_context()
    assert outward is not internal_snapshot.context
    if boundary == "context_snapshot":
        assert outward_owner is not internal_snapshot

    if mutation == "mapping":
        object.__setattr__(outward, "_indices", {})
    else:
        outward_index = outward._indices[key][1]  # type: ignore[attr-defined]
        object.__setattr__(
            outward_index,
            "_line_starts",
            (line_starts[0], line_starts[1] + 1, *line_starts[2:]),
        )

    assert server_worker.validate_production_worker_artifact(worker) == exports
    admitted_after = server_worker._validated_admitted_snapshot(worker)
    assert admitted_after.visible_context_snapshot is internal_snapshot
    assert (
        admitted_after.visible_context_snapshot.context.line_column(
            first,
            mapped_offset,
        )
        == expected_location
    )
    future_context = diagnostic().visible_source_context
    assert future_context is not None
    assert future_context is not outward
    assert future_context is not internal_snapshot.context
    assert future_context.line_column(first, mapped_offset) == expected_location
    future_adapter_context, future_owner = outward_context()
    assert future_adapter_context is not outward
    assert future_adapter_context.line_column(first, mapped_offset) == expected_location
    if boundary == "context_snapshot":
        assert future_owner is not outward_owner


def test_worker_internal_context_fingerprint_tampering_is_rejected_before_target_mutation(
    tmp_path: Path,
) -> None:
    """Break caught: admission proof must bind the deterministic context digest."""
    mapped, context = mapped_worker_candidate()
    worker = notebook_builder(tmp_path)(
        mapped,
        (
            WorkerExport("Первая", "Первая"),
            WorkerExport("Вторая", "Вторая"),
        ),
        visible_source_context=context,
    )
    snapshot = server_worker._validated_admitted_snapshot(
        worker
    ).visible_context_snapshot
    assert snapshot is not None
    object.__setattr__(snapshot, "fingerprint", "f" * 64)
    with pytest.raises(ProtocolError, match="context fingerprint"):
        server_worker.validate_production_worker_artifact(worker)


def test_worker_outward_context_snapshot_fingerprint_mutation_is_isolated(
    tmp_path: Path,
) -> None:
    mapped, context = mapped_worker_candidate()
    worker = notebook_builder(tmp_path)(
        mapped,
        (
            WorkerExport("Первая", "Первая"),
            WorkerExport("Вторая", "Вторая"),
        ),
        visible_source_context=context,
    )
    assert worker._admission is not None
    snapshot = worker._admission.context_snapshot(worker)
    assert snapshot is not None
    internal_snapshot = server_worker._validated_admitted_snapshot(
        worker
    ).visible_context_snapshot
    assert internal_snapshot is not None
    with pytest.raises(AttributeError, match="immutable"):
        snapshot.fingerprint = "f" * 64
    object.__setattr__(snapshot, "fingerprint", "f" * 64)

    assert snapshot is not internal_snapshot
    assert snapshot.fingerprint != internal_snapshot.fingerprint
    assert server_worker.validate_production_worker_artifact(worker) == worker.exports


def test_worker_context_fingerprint_binds_complete_line_start_structure(
    tmp_path: Path,
) -> None:
    """Break caught: interior line starts are part of the admission proof."""
    mapped, context = mapped_worker_candidate()
    worker = notebook_builder(tmp_path)(
        mapped,
        (
            WorkerExport("Первая", "Первая"),
            WorkerExport("Вторая", "Вторая"),
        ),
        visible_source_context=context,
    )
    assert worker._admission is not None
    snapshot = worker._admission.context_snapshot(worker)
    assert snapshot is not None
    first = mapped.source_map.segments[0].origin_ref
    assert isinstance(first, SourceUnitRef)
    key = (first.kind, first.unit_id, first.revision)
    line_index = snapshot.context._indices[key][1]  # type: ignore[attr-defined]
    line_starts = line_index._line_starts  # type: ignore[attr-defined]
    assert len(line_starts) >= 3
    boundaries_before = (
        snapshot.context.line_column(first, line_starts[0]),
        snapshot.context.line_column(first, line_starts[-1]),
    )
    interior_before = snapshot.context.line_column(first, line_starts[1])
    fingerprint_before = server_worker._visible_context_fingerprint(
        mapped,
        snapshot.context,
    )

    object.__setattr__(
        line_index,
        "_line_starts",
        (line_starts[0], line_starts[1] + 1, *line_starts[2:]),
    )

    assert (
        snapshot.context.line_column(first, line_starts[0]),
        snapshot.context.line_column(first, line_starts[-1]),
    ) == boundaries_before
    assert snapshot.context.line_column(first, line_starts[1]) != interior_before
    assert (
        server_worker._visible_context_fingerprint(mapped, snapshot.context)
        != fingerprint_before
    )


def test_worker_context_snapshot_is_immutable_across_deepcopy(
    tmp_path: Path,
) -> None:
    mapped, context = mapped_worker_candidate()
    worker = notebook_builder(tmp_path)(
        mapped,
        (
            WorkerExport("Первая", "Первая"),
            WorkerExport("Вторая", "Вторая"),
        ),
        visible_source_context=context,
    )
    assert worker._admission is not None
    snapshot = worker._admission.context_snapshot(worker)
    assert snapshot is not None
    first = mapped.source_map.segments[0].origin_ref
    assert isinstance(first, SourceUnitRef)
    key = (first.kind, first.unit_id, first.revision)
    line_index = snapshot.context._indices[key][1]  # type: ignore[attr-defined]

    assert deepcopy(snapshot) is snapshot
    assert deepcopy(snapshot.context) is snapshot.context
    assert deepcopy(line_index) is line_index
    with pytest.raises(AttributeError, match="immutable"):
        line_index._line_starts = (0,)  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        snapshot.context._indices[key] = (first.source_sha256, line_index)  # type: ignore[attr-defined]


def test_worker_interior_line_start_tampering_is_rejected_before_target_mutation(
    tmp_path: Path,
) -> None:
    mapped, context = mapped_worker_candidate()
    worker = notebook_builder(tmp_path)(
        mapped,
        (
            WorkerExport("Первая", "Первая"),
            WorkerExport("Вторая", "Вторая"),
        ),
        visible_source_context=context,
    )
    snapshot = server_worker._validated_admitted_snapshot(
        worker
    ).visible_context_snapshot
    assert snapshot is not None
    first = mapped.source_map.segments[0].origin_ref
    assert isinstance(first, SourceUnitRef)
    key = (first.kind, first.unit_id, first.revision)
    line_index = snapshot.context._indices[key][1]  # type: ignore[attr-defined]
    line_starts = line_index._line_starts  # type: ignore[attr-defined]
    object.__setattr__(
        line_index,
        "_line_starts",
        (line_starts[0], line_starts[1] + 1, *line_starts[2:]),
    )
    with pytest.raises(ProtocolError, match="context fingerprint"):
        server_worker.validate_production_worker_artifact(worker)


def test_worker_artifact_admission_accepts_directive_bearing_export_catalog(
    tmp_path: Path,
) -> None:
    raw_source = (
        "#Область API\r\n"
        "#Если Сервер Тогда\r\n"
        "&НаСервере\r\n"
        "Функция Серверный() Экспорт\r\n"
        "    Возврат 1;\r\n"
        "КонецФункции\r\n"
        "#Иначе\r\n"
        "Функция Остальной() Экспорт\r\n"
        "    Возврат 2;\r\n"
        "КонецФункции\r\n"
        "#КонецЕсли\r\n"
        "#КонецОбласти\r\n"
    )
    source = tmp_path / "DirectiveWorker.bsl"
    epf = tmp_path / "DirectiveWorker.epf"
    source.write_bytes(raw_source.encode("utf-8"))
    epf.write_bytes(b"directive-worker")
    exports = (
        WorkerExport("API.Серверный", "Серверный"),
        WorkerExport("API.Остальной", "Остальной"),
    )

    worker = build_worker_artifact(
        logical_name="DirectiveWorker",
        source_path=source,
        artifact_path=epf,
        exports=exports,
    )

    assert worker.exports == exports
    assert source.read_bytes() == raw_source.encode("utf-8")


def test_production_validation_reuses_the_catalog_proven_at_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: activation must not semantically parse an admitted source twice."""
    source = tmp_path / "Worker.bsl"
    epf = tmp_path / "Worker.epf"
    source.write_text(
        "Функция Посчитать() Экспорт\n    Возврат 1;\nКонецФункции",
        encoding="utf-8",
    )
    epf.write_bytes(b"worker")
    exports = (WorkerExport("Посчитать", "Посчитать"),)
    worker = build_worker_artifact(
        logical_name="Worker",
        source_path=source,
        artifact_path=epf,
        exports=exports,
    )

    def reject_duplicate_parse(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("admitted source was parsed again")

    monkeypatch.setattr(
        server_worker,
        "_validate_source_catalog",
        reject_duplicate_parse,
    )

    assert server_worker.validate_production_worker_artifact(worker) == exports


def test_worker_artifact_admission_proof_binds_receiver_identity(
    tmp_path: Path,
) -> None:
    source = tmp_path / "QualifiedWorker.bsl"
    epf = tmp_path / "QualifiedWorker.epf"
    source.write_text(
        "Функция Рассчитать() Экспорт\n    Возврат 1;\nКонецФункции",
        encoding="utf-8",
    )
    epf.write_bytes(b"qualified-worker")
    worker = build_worker_artifact(
        logical_name="МодульРасчета",
        source_path=source,
        artifact_path=epf,
        exports=(
            WorkerExport(
                "МодульРасчета.Рассчитать",
                "Рассчитать",
                receiver_module="МодульРасчета",
            ),
        ),
    )
    object.__setattr__(
        worker,
        "exports",
        (WorkerExport("МодульРасчета.Рассчитать", "Рассчитать"),),
    )

    with pytest.raises(ProtocolError, match="proven worker artifact|proof"):
        server_worker.validate_production_worker_artifact(worker)


def test_qualified_export_catalog_rejects_case_insensitive_duplicate_paths() -> None:
    exports = (
        WorkerExport(
            "МодульРасчета.Рассчитать",
            "Рассчитать",
            receiver_module="МодульРасчета",
        ),
        WorkerExport(
            "модульрасчета.рассчитать",
            "рассчитать",
            receiver_module="модульрасчета",
        ),
    )

    with pytest.raises(ValueError, match="duplicate worker export"):
        server_worker.validate_worker_export_catalog(exports)


def test_artifact_builder_reuses_an_exact_source_catalog_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: packaging must not parse an unchanged validated source twice."""
    source = tmp_path / "Worker.bsl"
    epf = tmp_path / "Worker.epf"
    source.write_text(
        "Функция Посчитать() Экспорт\n    Возврат 1;\nКонецФункции",
        encoding="utf-8",
    )
    epf.write_bytes(b"worker")
    exports = (WorkerExport("Посчитать", "Посчитать"),)
    validation = server_worker.validate_worker_source_catalog(source, exports)

    def reject_duplicate_parse(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("validated source was parsed again")

    monkeypatch.setattr(
        server_worker,
        "_validate_source_catalog",
        reject_duplicate_parse,
    )

    worker = build_worker_artifact(
        logical_name="Worker",
        source_path=source,
        artifact_path=epf,
        exports=exports,
        source_validation=validation,
    )

    assert server_worker.validate_production_worker_artifact(worker) == exports


def test_artifact_builder_rejects_source_changed_after_catalog_validation(
    tmp_path: Path,
) -> None:
    """Break caught: a catalog token must remain bound to exact source bytes."""
    source = tmp_path / "Worker.bsl"
    epf = tmp_path / "Worker.epf"
    source.write_text(
        "Функция Посчитать() Экспорт\n    Возврат 1;\nКонецФункции",
        encoding="utf-8",
    )
    epf.write_bytes(b"worker")
    exports = (WorkerExport("Посчитать", "Посчитать"),)
    validation = server_worker.validate_worker_source_catalog(source, exports)
    source.write_text(
        "Функция Другая() Экспорт\n    Возврат 2;\nКонецФункции",
        encoding="utf-8",
    )

    with pytest.raises(ProtocolError, match="source catalog validation"):
        build_worker_artifact(
            logical_name="Worker",
            source_path=source,
            artifact_path=epf,
            exports=exports,
            source_validation=validation,
        )


def test_source_catalog_validation_can_move_to_an_identical_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: a new path for exact bytes must not force another parse."""
    first = tmp_path / "first.bsl"
    second = tmp_path / "second.bsl"
    source = "Функция Посчитать() Экспорт\nВозврат 1;\nКонецФункции"
    first.write_text(source, encoding="utf-8")
    second.write_text(source, encoding="utf-8")
    exports = (WorkerExport("Посчитать", "Посчитать"),)
    validation = server_worker.validate_worker_source_catalog(first, exports)

    def reject_duplicate_parse(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("identical source was parsed again")

    monkeypatch.setattr(
        server_worker,
        "_validate_source_catalog",
        reject_duplicate_parse,
    )

    moved = server_worker.reuse_worker_source_catalog_validation(
        validation,
        second,
        exports,
    )

    assert repr(moved) == "<redacted worker source catalog validation>"


def test_source_catalog_validation_cannot_move_to_changed_bytes(
    tmp_path: Path,
) -> None:
    """Break caught: path rebinding must compare source bytes, not a caller key."""
    first = tmp_path / "first.bsl"
    second = tmp_path / "second.bsl"
    first.write_text(
        "Функция Посчитать() Экспорт\nВозврат 1;\nКонецФункции",
        encoding="utf-8",
    )
    second.write_text(
        "Функция Посчитать() Экспорт\nВозврат 2;\nКонецФункции",
        encoding="utf-8",
    )
    exports = (WorkerExport("Посчитать", "Посчитать"),)
    validation = server_worker.validate_worker_source_catalog(first, exports)

    with pytest.raises(ProtocolError, match="source catalog validation"):
        server_worker.reuse_worker_source_catalog_validation(
            validation,
            second,
            exports,
        )
