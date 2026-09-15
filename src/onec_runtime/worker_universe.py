from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256
from hmac import compare_digest, digest
import json
from re import fullmatch
from secrets import token_bytes, token_urlsafe
from threading import RLock
from typing import Callable, Literal, Protocol
from uuid import UUID, uuid4
from weakref import WeakKeyDictionary

from onec_runtime.bsl.diagnostics import (
    VisibleSourceContext,
    WorkerDiagnosticArtifact,
    parse_platform_diagnostic,
    remap_worker_stage_diagnostic,
)
from onec_runtime.bsl.lexer import KEYWORDS
from onec_runtime.bsl.module_universe import (
    DependencyUse,
    LoweredWorkerModule,
    ModuleDependencyBinding,
    WorkerSemanticAdmission,
    worker_semantic_admission,
)
from onec_runtime.bsl.semantic_lowering import WorkerExport
from onec_runtime.bsl.source_maps import (
    LineIndex,
    MappedSource,
    SourceUnitKind,
    SourceUnitRef,
)
from onec_runtime.errors import (
    BslExecutionError,
    ProtocolError,
    WorkerPromotionOutcomeUnknown,
)
from onec_runtime.performance_profile import PhaseRecorder
from onec_runtime.rdbg.models import ModuleLocation
from onec_runtime.server_worker import (
    WorkerArtifact,
    WorkerArtifactStage,
    _AdmittedWorkerSnapshot,
    _validated_admitted_snapshot,
    _worker_artifact_diagnostic_source_from_snapshot,
    _worker_reload_platform_message,
    rebind_worker_artifact_binary,
    stage_worker_module_instruction,
    validate_production_worker_artifact,
    validate_worker_export_catalog,
    worker_artifact_stage_failure,
)
from onec_runtime.worker_stage_protocol import (
    _temp_storage_session_id,
    WorkerStageBatch,
    WorkerStageBatchOutcome,
    WorkerStageRegistrationReceipt,
    WorkerStageEntry,
    build_worker_stage_batches,
    parse_worker_stage_batch_outcome,
    stage_worker_batch_instruction,
)


_WORKER_MODULE_OBJECT_ID = UUID("2a00a4fa-8ea9-4dc4-9de1-472044c40101")
_WORKER_MODULE_PROPERTY_ID = UUID("a637f77f-3840-441d-a1c3-699c8c5cb7e0")
_WORKER_LOCATOR_PROFILE_ID = "external-processing-object-module-8.3.27"
_WORKER_LOCATOR_PLATFORM_BUILD = "8.3.27.2170"
_REGISTRATION_RECEIPT_ADMISSION = object()


_DESCRIPTOR_PROOF_KEY = token_bytes(32)
_BINARY_CAPSULE_PROOF_KEY = token_bytes(32)
_PREPARED_ROOT_RECEIPT_PREFIX = "onec-worker-prepared-root-receipt-v1"
_ROOT_SWAP_RECEIPT_PREFIX = "onec-worker-root-swap-receipt-v1"
_ROOT_DISCARD_RECEIPT_PREFIX = "onec-worker-root-discard-receipt-v1"
_BSL_KEYWORDS = frozenset(KEYWORDS)
_FIXED_UNQUALIFIED_WORKER_IDENTIFIERS = frozenset(
    value.casefold()
    for value in (
        "Результат",
        "Контекст",
        "ВнешниеОбработки",
        "ПоместитьВоВременноеХранилище",
        "Base64Значение",
        "ОписаниеОшибки",
        "ИнформацияОбОшибке",
        "ПодробноеПредставлениеОшибки",
        "СтрДлина",
        "Символы",
        "ТипЗнч",
        "Тип",
        "Соответствие",
        "ФиксированноеСоответствие",
        "Массив",
        "ФиксированныйМассив",
        "Структура",
        "ФиксированнаяСтруктура",
    )
)


class _NotebookArtifactBuilder(Protocol):
    def __call__(
        self,
        source: MappedSource,
        exports: tuple[WorkerExport, ...],
        *,
        visible_source_context: VisibleSourceContext,
        semantic_admission: WorkerSemanticAdmission | None = None,
        semantic_packer_identity: str | None = None,
    ) -> WorkerArtifact: ...


@dataclass(frozen=True, slots=True)
class WorkerModuleBinaryKey:
    logical_name: str
    lowered_source_sha256: str
    dependency_bindings_sha256: str
    transform_version: str
    packer_version: str
    target_profile: str

    def __post_init__(self) -> None:
        if (
            not _safe_identity(self.logical_name)
            or not _sha256_value(self.lowered_source_sha256)
            or not _sha256_value(self.dependency_bindings_sha256)
            or not _safe_identity(self.transform_version)
            or not _safe_identity(self.packer_version)
            or not _safe_identity(self.target_profile)
        ):
            raise ValueError("worker module binary key is invalid")
        object.__setattr__(self, "logical_name", self.logical_name.casefold())

    @classmethod
    def create(
        cls,
        lowered: LoweredWorkerModule,
        *,
        packer_version: str,
        target_profile: str,
    ) -> WorkerModuleBinaryKey:
        if not isinstance(lowered, LoweredWorkerModule):
            raise TypeError("lowered worker module is required")
        return cls(
            lowered.analysis.unit.logical_name,
            lowered.mapped_source.artifact.source_sha256,
            lowered.dependency_bindings_sha256,
            lowered.transform_version,
            packer_version,
            target_profile,
        )


@dataclass(frozen=True, slots=True, repr=False, weakref_slot=True, eq=False)
class WorkerModuleArtifact:
    logical_name: str
    revision: int
    source_sha256: str
    binary_key: WorkerModuleBinaryKey
    artifact_sha256: str
    dependency_bindings: tuple[ModuleDependencyBinding, ...]
    exports: tuple[WorkerExport, ...]
    source_map_sha256: str
    worker_artifact: WorkerArtifact

    def __repr__(self) -> str:
        return (
            "WorkerModuleArtifact("
            f"logical_name={self.logical_name!r}, revision={self.revision}, "
            f"artifact_sha256={self.artifact_sha256!r}, payload=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class _WorkerModuleBinaryCapsule:
    key: WorkerModuleBinaryKey
    worker_artifact: WorkerArtifact
    proof: bytes


class WorkerModuleArtifactCache:
    """Content-addressed storage for admitted, generation-independent binaries."""

    __slots__ = ("_capsules",)

    def __init__(self) -> None:
        self._capsules: dict[WorkerModuleBinaryKey, _WorkerModuleBinaryCapsule] = {}

    def __len__(self) -> int:
        return len(self._capsules)

    def _admit_capsule(self, capsule: _WorkerModuleBinaryCapsule) -> None:
        try:
            if not isinstance(capsule, _WorkerModuleBinaryCapsule):
                raise ProtocolError("Worker binary cache admission is invalid")
            key = capsule.key
            worker_artifact = capsule.worker_artifact
            validate_production_worker_artifact(worker_artifact)
            if (
                not compare_digest(
                    capsule.proof,
                    _binary_capsule_proof(key, worker_artifact),
                )
                or worker_artifact.logical_name.casefold()
                != key.logical_name.casefold()
                or worker_artifact.source_sha256 != key.lowered_source_sha256
                or not _exports_match_receiver(
                    key.logical_name,
                    worker_artifact.exports,
                )
            ):
                raise ProtocolError("Worker binary cache admission is invalid")
            existing = self._capsules.get(key)
            if existing is not None:
                validate_production_worker_artifact(existing.worker_artifact)
                if (
                    not compare_digest(
                        existing.proof,
                        _binary_capsule_proof(
                            existing.key,
                            existing.worker_artifact,
                        ),
                    )
                    or existing.key != key
                    or existing.worker_artifact.artifact_sha256
                    != worker_artifact.artifact_sha256
                ):
                    raise ProtocolError("Worker binary cache admission is invalid")
                return
            self._capsules[key] = capsule
        except ProtocolError:
            raise
        except Exception:
            raise ProtocolError("Worker binary cache admission is invalid") from None

    def _lookup(self, key: WorkerModuleBinaryKey) -> WorkerArtifact | None:
        capsule = self._capsules.get(key)
        if capsule is None:
            return None
        try:
            if capsule.key != key:
                raise ProtocolError("Worker binary cache admission is invalid")
            validate_production_worker_artifact(capsule.worker_artifact)
            if (
                not compare_digest(
                    capsule.proof,
                    _binary_capsule_proof(key, capsule.worker_artifact),
                )
                or capsule.worker_artifact.logical_name.casefold()
                != key.logical_name.casefold()
                or capsule.worker_artifact.source_sha256
                != key.lowered_source_sha256
                or not _exports_match_receiver(
                    key.logical_name,
                    capsule.worker_artifact.exports,
                )
            ):
                raise ProtocolError("Worker binary cache admission is invalid")
            return capsule.worker_artifact
        except ProtocolError:
            raise
        except Exception:
            raise ProtocolError("Worker binary cache admission is invalid") from None

    def _prune(self, live_keys: frozenset[WorkerModuleBinaryKey]) -> None:
        """Drop capsules not reachable from a confirmed generation inventory."""
        if (
            type(live_keys) is not frozenset
            or any(not isinstance(key, WorkerModuleBinaryKey) for key in live_keys)
        ):
            raise TypeError("worker binary cache live keys are invalid")
        self._capsules = {
            key: capsule
            for key, capsule in self._capsules.items()
            if key in live_keys
        }


class WorkerModuleArtifactBuilder:
    __slots__ = (
        "_artifact_builder",
        "_cache",
        "_packer_version",
        "_target_profile",
    )

    def __init__(
        self,
        artifact_builder: _NotebookArtifactBuilder,
        *,
        cache: WorkerModuleArtifactCache | None = None,
        packer_version: str,
        target_profile: str,
    ) -> None:
        if not callable(artifact_builder):
            raise TypeError("notebook worker artifact builder must be callable")
        if cache is not None and not isinstance(cache, WorkerModuleArtifactCache):
            raise TypeError("worker module artifact cache is invalid")
        if not _safe_identity(packer_version) or not _safe_identity(target_profile):
            raise ValueError("worker module artifact builder identity is invalid")
        self._artifact_builder = artifact_builder
        self._cache = cache if cache is not None else WorkerModuleArtifactCache()
        self._packer_version = packer_version
        self._target_profile = target_profile

    def build(
        self,
        lowered: LoweredWorkerModule,
        *,
        visible_source_context: VisibleSourceContext,
        profiler: PhaseRecorder | None = None,
    ) -> WorkerModuleArtifact:
        try:
            _validate_lowered_worker_module(lowered)
            if not isinstance(visible_source_context, VisibleSourceContext):
                raise ProtocolError("Worker module artifact admission is invalid")
            key = WorkerModuleBinaryKey.create(
                lowered,
                packer_version=self._packer_version,
                target_profile=self._target_profile,
            )
            semantic_admission = worker_semantic_admission(
                lowered,
                packer_identity=self._packer_version,
            )
            logical_name = lowered.analysis.unit.logical_name
            exports = _qualified_exports(lowered)
            cached = self._cache._lookup(key)
            if cached is None:
                package = lambda: self._artifact_builder(
                    lowered.mapped_source,
                    exports,
                    visible_source_context=visible_source_context,
                    semantic_admission=semantic_admission,
                    semantic_packer_identity=self._packer_version,
                )
                packed = (
                    package()
                    if profiler is None
                    else profiler.measure(
                        "epf_packaging",
                        package,
                        item_count=lambda _result: 1,
                    )
                )
                binary = rebind_worker_artifact_binary(
                    packed,
                    logical_name=logical_name,
                    mapped=lowered.mapped_source,
                    visible_source_context=visible_source_context,
                    exports=exports,
                )
                self._cache._admit_capsule(_mint_binary_capsule(key, binary))
                worker_artifact = binary
            else:
                worker_artifact = rebind_worker_artifact_binary(
                    cached,
                    logical_name=logical_name,
                    mapped=lowered.mapped_source,
                    visible_source_context=visible_source_context,
                    exports=exports,
                )
            artifact = WorkerModuleArtifact(
                logical_name=logical_name,
                revision=lowered.analysis.unit.revision,
                source_sha256=key.lowered_source_sha256,
                binary_key=key,
                artifact_sha256=worker_artifact.artifact_sha256,
                dependency_bindings=lowered.analysis.dependencies,
                exports=exports,
                source_map_sha256=worker_artifact.source_map_sha256 or "",
                worker_artifact=worker_artifact,
            )
            unit = lowered.analysis.unit
            _bind_descriptor_admission(
                artifact,
                descriptor_cache_identity=(
                    unit.logical_name.casefold(),
                    unit.kind,
                    unit.revision,
                    unit.mapped_source.artifact.source_sha256,
                    unit.mapped_source.source_map_sha256,
                ),
            )
            return artifact
        except Exception:
            raise ProtocolError("Worker module artifact admission is invalid") from None

    def _prune_cache(self, live_keys: frozenset[WorkerModuleBinaryKey]) -> None:
        self._cache._prune(live_keys)


class _WorkerModuleArtifactAdmission:
    __slots__ = ("proof", "compatibility", "descriptor_cache_identity")

    def __init__(
        self,
        proof: bytes,
        *,
        compatibility: bool = False,
        descriptor_cache_identity: tuple[str, str, int, str, str] | None = None,
    ) -> None:
        object.__setattr__(self, "proof", proof)
        object.__setattr__(self, "compatibility", compatibility)
        object.__setattr__(
            self,
            "descriptor_cache_identity",
            descriptor_cache_identity,
        )

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("worker module artifact admissions are immutable")

    def __repr__(self) -> str:
        return "<redacted worker module artifact admission>"


_DESCRIPTOR_ADMISSIONS: WeakKeyDictionary[
    WorkerModuleArtifact,
    _WorkerModuleArtifactAdmission,
] = WeakKeyDictionary()


def _validate_descriptor_cache_identity(
    artifact: WorkerModuleArtifact,
    identity: tuple[str, str, int, str, str] | None,
    *,
    compatibility: bool,
) -> None:
    if compatibility:
        if identity is not None:
            raise ProtocolError("Worker module descriptor cache identity is invalid")
        return
    if (
        not isinstance(identity, tuple)
        or len(identity) != 5
        or identity[0] != artifact.logical_name.casefold()
        or identity[1] not in ("module", "test-module")
        or identity[2] != artifact.revision
        or not _sha256_value(identity[3])
        or not _sha256_value(identity[4])
    ):
        raise ProtocolError("Worker module descriptor cache identity is invalid")


def _worker_module_descriptor_cache_identity(
    artifact: WorkerModuleArtifact,
) -> tuple[str, str, int, str, str] | None:
    admission = _DESCRIPTOR_ADMISSIONS.get(artifact)
    if admission is None:
        raise ProtocolError("Worker module descriptor cache identity is invalid")
    identity = admission.descriptor_cache_identity
    _validate_descriptor_cache_identity(
        artifact,
        identity,
        compatibility=admission.compatibility,
    )
    return identity


def _bind_descriptor_admission(
    artifact: WorkerModuleArtifact,
    *,
    compatibility: bool = False,
    descriptor_cache_identity: tuple[str, str, int, str, str] | None = None,
) -> None:
    _validate_descriptor_cache_identity(
        artifact,
        descriptor_cache_identity,
        compatibility=compatibility,
    )
    _DESCRIPTOR_ADMISSIONS[artifact] = _WorkerModuleArtifactAdmission(
        _descriptor_proof(artifact, compatibility=compatibility),
        compatibility=compatibility,
        descriptor_cache_identity=descriptor_cache_identity,
    )


def worker_module_artifact_from_notebook(
    artifact: WorkerArtifact,
    *,
    revision: int,
    target_profile: str,
) -> WorkerModuleArtifact:
    """Admit a current notebook Worker binary into the module universe.

    The returned descriptor retains the exact admitted binary and mapped source;
    only its host-side export descriptors gain the canonical ``Worker`` receiver.
    """
    try:
        catalog = validate_production_worker_artifact(artifact)
        snapshot = _validated_admitted_snapshot(artifact)
        mapped = snapshot.mapped_source
        if (
            artifact.logical_name.casefold() != "worker"
            or type(revision) is not int
            or revision <= 0
            or not _safe_identity(target_profile)
            or any(item.receiver_module is not None for item in catalog)
        ):
            raise ProtocolError("Notebook Worker admission is invalid")
        exports = tuple(
            WorkerExport(
                f"Worker.{item.method}",
                item.method,
                receiver_module="Worker",
            )
            for item in catalog
        )
        validate_worker_export_catalog(exports, require_nonempty=True)
        dependency_sha256 = _dependency_bindings_sha256(
            (),
            validate_bindings=True,
        )
        key = WorkerModuleBinaryKey(
            "Worker",
            artifact.source_sha256,
            dependency_sha256,
            "notebook-worker-adapter-v1",
            "notebook-admitted-binary-v1",
            target_profile,
        )
        descriptor = WorkerModuleArtifact(
            logical_name="Worker",
            revision=revision,
            source_sha256=artifact.source_sha256,
            binary_key=key,
            artifact_sha256=artifact.artifact_sha256,
            dependency_bindings=(),
            exports=exports,
            source_map_sha256=snapshot.source_map_sha256,
            worker_artifact=artifact,
        )
        _bind_descriptor_admission(descriptor, compatibility=True)
        return descriptor
    except ProtocolError as error:
        if str(error) == "Notebook Worker admission is invalid":
            raise
        raise ProtocolError("Notebook Worker admission is invalid") from None
    except Exception:
        raise ProtocolError("Notebook Worker admission is invalid") from None


def _mint_binary_capsule(
    key: WorkerModuleBinaryKey,
    worker_artifact: WorkerArtifact,
) -> _WorkerModuleBinaryCapsule:
    validate_production_worker_artifact(worker_artifact)
    return _WorkerModuleBinaryCapsule(
        key,
        worker_artifact,
        _binary_capsule_proof(key, worker_artifact),
    )


def _binary_capsule_proof(
    key: WorkerModuleBinaryKey,
    worker_artifact: WorkerArtifact,
) -> bytes:
    artifact_proof = _validated_admitted_snapshot(worker_artifact).proof
    manifest = {
        "schema": "onec-worker-module-binary-capsule-v1",
        "binary_key": {
            "logical_name": key.logical_name,
            "lowered_source_sha256": key.lowered_source_sha256,
            "dependency_bindings_sha256": key.dependency_bindings_sha256,
            "transform_version": key.transform_version,
            "packer_version": key.packer_version,
            "target_profile": key.target_profile,
        },
        "worker_artifact": {
            "logical_name": worker_artifact.logical_name.casefold(),
            "source_sha256": worker_artifact.source_sha256,
            "artifact_sha256": worker_artifact.artifact_sha256,
            "source_map_sha256": worker_artifact.source_map_sha256,
            "admission_proof_hex": artifact_proof.hex(),
        },
    }
    payload = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return digest(
        _BINARY_CAPSULE_PROOF_KEY,
        payload,
        "sha256",
    )


@dataclass(frozen=True, slots=True, repr=False)
class _ValidatedWorkerModuleArtifact:
    """Private result of one complete revision-descriptor admission."""

    descriptor: WorkerModuleArtifact
    snapshot: _AdmittedWorkerSnapshot
    descriptor_cache_identity: tuple[str, str, int, str, str] | None

    def __repr__(self) -> str:
        return "<validated worker module artifact>"


def _validated_worker_module_artifact(
    artifact: WorkerModuleArtifact,
) -> _ValidatedWorkerModuleArtifact:
    """Fail-closed production admission for a revision descriptor and capsule."""
    try:
        if not isinstance(artifact, WorkerModuleArtifact):
            raise ProtocolError("Worker module artifact admission is invalid")
        admission = _DESCRIPTOR_ADMISSIONS.get(artifact)
        if admission is None or not compare_digest(
            admission.proof,
            _descriptor_proof(
                artifact,
                compatibility=admission.compatibility,
            ),
        ):
            raise ProtocolError("Worker module artifact admission is invalid")
        descriptor_cache_identity = _worker_module_descriptor_cache_identity(artifact)
        if admission.compatibility:
            # The public compatibility factory performed the live-file
            # production admission before it minted this descriptor.  From
            # here on the descriptor owns that exact immutable admission
            # snapshot: staging must not reopen a path that may have changed
            # after admission (the stage instruction below consumes the
            # admitted bytes, not the path).
            snapshot = _validated_admitted_snapshot(artifact.worker_artifact)
            mapped = snapshot.mapped_source
            catalog = validate_worker_export_catalog(
                artifact.worker_artifact.exports,
                require_nonempty=True,
            )
        else:
            snapshot = _validated_admitted_snapshot(artifact.worker_artifact)
            mapped = snapshot.mapped_source
            catalog = snapshot.catalog
        validate_worker_export_catalog(artifact.exports, require_nonempty=True)
        dependency_sha256 = _dependency_bindings_sha256(
            artifact.dependency_bindings,
            validate_bindings=True,
        )
        compact_spans = getattr(mapped.source_map, "compact_visible_spans", None)
        references = (
            tuple(reference for _role, reference, _span in compact_spans())
            if compact_spans is not None
            else tuple(
                reference
                for segment in mapped.source_map.segments
                for reference in (segment.origin_ref, segment.anchor_ref)
            )
        )
        visible_revisions = {
            reference.revision
            for reference in references
            if (
                getattr(reference, "kind", None)
                in (SourceUnitKind.MODULE, SourceUnitKind.TEST_MODULE)
                and getattr(reference, "unit_id", "").casefold()
                == artifact.logical_name.casefold()
            )
        }
        compatibility_catalog = (
            admission.compatibility
            and artifact.logical_name == "Worker"
            and artifact.binary_key.transform_version
            == "notebook-worker-adapter-v1"
            and artifact.binary_key.packer_version
            == "notebook-admitted-binary-v1"
            and artifact.dependency_bindings == ()
            and len(artifact.exports) == len(catalog)
            and all(
                descriptor.receiver_module == "Worker"
                and source.receiver_module is None
                and descriptor.public_path.casefold()
                == f"worker.{source.method}".casefold()
                and descriptor.method.casefold() == source.method.casefold()
                for descriptor, source in zip(
                    artifact.exports,
                    catalog,
                    strict=True,
                )
            )
        )
        if (
            artifact.logical_name.casefold()
            != artifact.binary_key.logical_name.casefold()
            or artifact.logical_name.casefold()
            != artifact.worker_artifact.logical_name.casefold()
            or type(artifact.revision) is not int
            or artifact.revision < 0
            or (
                not admission.compatibility
                and visible_revisions != {artifact.revision}
            )
            or artifact.source_sha256
            != artifact.binary_key.lowered_source_sha256
            or artifact.source_sha256 != artifact.worker_artifact.source_sha256
            or artifact.source_sha256 != mapped.artifact.source_sha256
            or artifact.artifact_sha256 != artifact.worker_artifact.artifact_sha256
            or artifact.source_map_sha256 != artifact.worker_artifact.source_map_sha256
            or artifact.source_map_sha256 != snapshot.source_map_sha256
            or dependency_sha256
            != artifact.binary_key.dependency_bindings_sha256
            or (
                not admission.compatibility
                and artifact.exports != artifact.worker_artifact.exports
            )
            or (
                not admission.compatibility
                and artifact.exports != catalog
            )
            or (admission.compatibility and not compatibility_catalog)
            or not _exports_match_receiver(artifact.logical_name, artifact.exports)
        ):
            raise ProtocolError("Worker module artifact admission is invalid")
        return _ValidatedWorkerModuleArtifact(
            artifact,
            snapshot,
            descriptor_cache_identity,
        )
    except ProtocolError as error:
        if str(error) == "Worker module artifact admission is invalid":
            raise
        raise ProtocolError("Worker module artifact admission is invalid") from None
    except Exception:
        raise ProtocolError("Worker module artifact admission is invalid") from None


def validate_worker_module_artifact(
    artifact: WorkerModuleArtifact,
) -> tuple[WorkerExport, ...]:
    """Validate one descriptor and return its public export catalog."""
    return _validated_worker_module_artifact(artifact).descriptor.exports


def _validate_lowered_worker_module(lowered: LoweredWorkerModule) -> None:
    if not isinstance(lowered, LoweredWorkerModule):
        raise ProtocolError("Worker module artifact admission is invalid")
    if (
        lowered.mapped_source.artifact.worker_generation is not None
        or lowered.mapped_source.artifact.worker_manifest_sha256 is not None
        or not _safe_identity(lowered.transform_version)
        or lowered.dependency_bindings_sha256
        != _dependency_bindings_sha256(
            lowered.analysis.dependencies,
            validate_bindings=True,
        )
    ):
        raise ProtocolError("Worker module artifact admission is invalid")


def _qualified_exports(lowered: LoweredWorkerModule) -> tuple[WorkerExport, ...]:
    logical_name = lowered.analysis.unit.logical_name
    exports = tuple(
        WorkerExport(
            f"{logical_name}.{method}",
            method,
            receiver_module=logical_name,
        )
        for method in lowered.analysis.exported_methods
    )
    return validate_worker_export_catalog(exports, require_nonempty=True)


def _exports_match_receiver(
    logical_name: str,
    exports: tuple[WorkerExport, ...],
) -> bool:
    return all(
        item.receiver_module is not None
        and item.receiver_module.casefold() == logical_name.casefold()
        and item.public_path.casefold()
        == f"{logical_name}.{item.method}".casefold()
        for item in exports
    )


def _binding_manifest(binding: ModuleDependencyBinding) -> dict[str, object]:
    return {
        "target_module": binding.target_module,
        "export_variable": binding.export_variable,
        "uses": [
            {
                "method_name": use.method_name,
                "category": use.category,
                "span": [use.span.start, use.span.end],
                "method_declaration": [
                    use.method_declaration.start,
                    use.method_declaration.end,
                ],
            }
            for use in binding.uses
        ],
    }


def _binding_sha256(binding: ModuleDependencyBinding) -> str:
    payload = json.dumps(
        _binding_manifest(binding),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _dependency_bindings_sha256(
    bindings: tuple[ModuleDependencyBinding, ...],
    *,
    validate_bindings: bool,
) -> str:
    if type(bindings) is not tuple:
        raise ProtocolError("Worker module artifact admission is invalid")
    if validate_bindings:
        for binding in bindings:
            if (
                not isinstance(binding, ModuleDependencyBinding)
                or type(binding.uses) is not tuple
                or any(not isinstance(use, DependencyUse) for use in binding.uses)
                or binding.sha256 != _binding_sha256(binding)
            ):
                raise ProtocolError("Worker module artifact admission is invalid")
    payload = json.dumps(
        [binding.sha256 for binding in bindings],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _descriptor_proof(
    artifact: WorkerModuleArtifact,
    *,
    compatibility: bool = False,
) -> bytes:
    binding_payload = json.dumps(
        [_binding_manifest(binding) | {"sha256": binding.sha256} for binding in artifact.dependency_bindings],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    export_payload = json.dumps(
        [
            [item.public_path, item.method, item.receiver_module]
            for item in artifact.exports
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    key_payload = json.dumps(
        [
            artifact.binary_key.logical_name,
            artifact.binary_key.lowered_source_sha256,
            artifact.binary_key.dependency_bindings_sha256,
            artifact.binary_key.transform_version,
            artifact.binary_key.packer_version,
            artifact.binary_key.target_profile,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    fields = (
        "compatibility" if compatibility else "module",
        artifact.logical_name,
        str(artifact.revision),
        artifact.source_sha256,
        key_payload,
        artifact.artifact_sha256,
        binding_payload,
        export_payload,
        artifact.source_map_sha256,
        artifact.worker_artifact.logical_name,
        artifact.worker_artifact.source_sha256,
        artifact.worker_artifact.artifact_sha256,
        artifact.worker_artifact.source_map_sha256 or "",
    )
    return digest(
        _DESCRIPTOR_PROOF_KEY,
        "\x1f".join(fields).encode("utf-8"),
        "sha256",
    )


def _sha256_value(value: object) -> bool:
    return isinstance(value, str) and fullmatch(r"[0-9a-f]{64}", value) is not None


def _safe_identity(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and "\x00" not in value
        and "\r" not in value
        and "\n" not in value
        and "/" not in value
        and "\\" not in value
        and "|" not in value
    )


class WorkerUniverseState(Enum):
    EMPTY = "empty"
    READY = "ready"
    PREPARING = "preparing"
    BROKEN = "broken"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class ManifestModule:
    logical_name: str
    revision: int
    artifact_sha256: str
    registration_name: str

    def __post_init__(self) -> None:
        if (
            not _bsl_identifier(self.logical_name)
            or type(self.revision) is not int
            or self.revision < 0
            or not _sha256_value(self.artifact_sha256)
            or self.registration_name
            != registration_name(self.logical_name, self.artifact_sha256)
        ):
            raise ValueError("worker manifest module is invalid")


@dataclass(frozen=True, slots=True)
class DependencyTarget:
    source_module: str
    export_variable: str
    target_kind: Literal["original", "overloaded"]
    target_module: str

    def __post_init__(self) -> None:
        if not _valid_dependency_target(self):
            raise ValueError("worker dependency target is invalid")


@dataclass(frozen=True, slots=True)
class WorkerUniverseManifest:
    generation: int
    modules: tuple[ManifestModule, ...]
    wiring: tuple[DependencyTarget, ...]
    exports: tuple[WorkerExport, ...]
    sha256: str

    def __post_init__(self) -> None:
        if (
            type(self.generation) is not int
            or self.generation <= 0
            or type(self.modules) is not tuple
            or not self.modules
            or any(not isinstance(item, ManifestModule) for item in self.modules)
            or self.modules != tuple(sorted(self.modules, key=_manifest_module_sort_key))
            or type(self.wiring) is not tuple
            or any(not isinstance(item, DependencyTarget) for item in self.wiring)
            or any(not _valid_dependency_target(item) for item in self.wiring)
            or self.wiring != tuple(sorted(self.wiring, key=_dependency_target_sort_key))
            or type(self.exports) is not tuple
            or self.exports != tuple(sorted(self.exports, key=_worker_export_sort_key))
            or any(
                not _bsl_identifier(item.method)
                or not _bsl_identifier(item.receiver_module)
                for item in self.exports
            )
            or not _sha256_value(self.sha256)
        ):
            raise ValueError("worker universe manifest is invalid")
        module_names = tuple(item.logical_name.casefold() for item in self.modules)
        registration_names = tuple(
            item.registration_name.casefold() for item in self.modules
        )
        if (
            len(module_names) != len(set(module_names))
            or len(registration_names) != len(set(registration_names))
        ):
            raise ValueError("worker universe manifest is invalid")
        binding_keys = tuple(
            (item.source_module.casefold(), item.export_variable.casefold())
            for item in self.wiring
        )
        if len(binding_keys) != len(set(binding_keys)):
            raise ValueError("worker universe manifest is invalid")
        loaded = set(module_names)
        if any(
            item.source_module.casefold() not in loaded
            or (
                item.target_kind == "overloaded"
                and item.target_module.casefold() not in loaded
            )
            or (
                item.target_kind == "original"
                and item.target_module.casefold() in loaded
            )
            for item in self.wiring
        ):
            raise ValueError("worker universe manifest is invalid")
        try:
            validate_worker_export_catalog(self.exports, require_nonempty=True)
        except (ProtocolError, ValueError) as error:
            raise ValueError("worker universe manifest is invalid") from error
        if any(
            item.receiver_module is None
            or item.receiver_module.casefold() not in loaded
            for item in self.exports
        ):
            raise ValueError("worker universe manifest is invalid")
        if self.sha256 != _worker_manifest_sha256(
            self.generation,
            self.modules,
            self.wiring,
            self.exports,
        ):
            raise ValueError("worker universe manifest is invalid")


@dataclass(frozen=True, slots=True, eq=False)
class WorkerGenerationHandle:
    runtime_generation: int
    context_generation: int
    generation: int
    manifest_sha256: str

    def __post_init__(self) -> None:
        if (
            type(self.runtime_generation) is not int
            or self.runtime_generation <= 0
            or type(self.context_generation) is not int
            or self.context_generation <= 0
            or type(self.generation) is not int
            or self.generation <= 0
            or not _sha256_value(self.manifest_sha256)
        ):
            raise ValueError("worker generation handle is invalid")


@dataclass(frozen=True, slots=True, repr=False)
class WorkerGenerationConfirmation:
    handle: WorkerGenerationHandle
    released_registrations: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.handle, WorkerGenerationHandle)
            or type(self.released_registrations) is not tuple
            or any(
                not _bsl_identifier(name)
                for name in self.released_registrations
            )
            or self.released_registrations
            != tuple(sorted(self.released_registrations, key=str.casefold))
            or len(self.released_registrations)
            != len({name.casefold() for name in self.released_registrations})
        ):
            raise ValueError("worker generation confirmation is invalid")

    def __repr__(self) -> str:
        return (
            "WorkerGenerationConfirmation("
            "handle=<redacted>, released_registrations=<redacted>, "
            f"released_count={len(self.released_registrations)})"
        )


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class OperationGenerationPin:
    handle: WorkerGenerationHandle
    manifest: WorkerUniverseManifest
    root_key: str
    export_catalog: tuple[WorkerExport, ...]
    lease_id: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.handle, WorkerGenerationHandle)
            or not isinstance(self.manifest, WorkerUniverseManifest)
            or self.handle.generation != self.manifest.generation
            or self.handle.manifest_sha256 != self.manifest.sha256
            or not _safe_identity(self.root_key)
            or type(self.export_catalog) is not tuple
            or not _effective_export_catalog_matches(
                self.manifest,
                self.export_catalog,
            )
            or not isinstance(self.lease_id, str)
            or len(self.lease_id) < 32
        ):
            raise ValueError("worker operation generation pin is invalid")

    def __repr__(self) -> str:
        return (
            "OperationGenerationPin("
            f"handle={self.handle!r}, root_key=<redacted>, "
            f"exports={len(self.export_catalog)}, lease_id=<redacted>)"
        )


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class WorkerUniverseCandidate:
    handle: WorkerGenerationHandle
    manifest: WorkerUniverseManifest
    export_catalog: tuple[WorkerExport, ...]
    artifacts: tuple[WorkerModuleArtifact, ...]
    new_artifacts: tuple[WorkerModuleArtifact, ...]
    previous: WorkerGenerationHandle | None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.handle, WorkerGenerationHandle)
            or not isinstance(self.manifest, WorkerUniverseManifest)
            or self.handle.generation != self.manifest.generation
            or self.handle.manifest_sha256 != self.manifest.sha256
            or not _effective_export_catalog_matches(
                self.manifest,
                self.export_catalog,
            )
            or type(self.artifacts) is not tuple
            or not self.artifacts
            or any(not isinstance(item, WorkerModuleArtifact) for item in self.artifacts)
            or type(self.new_artifacts) is not tuple
            or any(
                not any(item is artifact for artifact in self.artifacts)
                for item in self.new_artifacts
            )
            or (
                self.previous is not None
                and not isinstance(self.previous, WorkerGenerationHandle)
            )
        ):
            raise ValueError("worker universe candidate is invalid")

    def __repr__(self) -> str:
        return (
            "WorkerUniverseCandidate("
            f"handle={self.handle!r}, modules={len(self.artifacts)}, "
            f"new_artifacts={len(self.new_artifacts)}, payload=<redacted>)"
        )


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class _SealedWorkerStageEntry:
    """Exact target payload and diagnostics retained for one sealed module."""

    descriptor: WorkerModuleArtifact
    logical_name: str
    revision: int
    artifact_sha256: str
    registration_name: str
    artifact_bytes: bytes
    admitted_snapshot: _AdmittedWorkerSnapshot

    def __repr__(self) -> str:
        return (
            "_SealedWorkerStageEntry("
            f"logical_name={self.logical_name!r}, revision={self.revision}, "
            f"artifact_sha256={self.artifact_sha256!r}, payload=<redacted>)"
        )


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class _SealedWorkerActivation:
    """Private capability proving one exact pending candidate was admitted."""

    candidate: WorkerUniverseCandidate
    artifact_views: tuple[_ValidatedWorkerModuleArtifact, ...]
    stage_entries: tuple[_SealedWorkerStageEntry, ...]
    diagnostics: tuple[WorkerDiagnosticArtifact, ...]
    debug_view: WorkerGenerationDebugView | None = None

    def __repr__(self) -> str:
        return (
            "_SealedWorkerActivation("
            f"generation={self.candidate.handle.generation}, "
            f"modules={len(self.artifact_views)}, payload=<redacted>)"
        )


def _seal_worker_activation(
    candidate: WorkerUniverseCandidate,
    artifact_views: tuple[_ValidatedWorkerModuleArtifact, ...],
) -> _SealedWorkerActivation:
    """Bind the already-admitted manifest views to their exact candidate."""
    if (
        not isinstance(candidate, WorkerUniverseCandidate)
        or type(artifact_views) is not tuple
        or len(artifact_views) != len(candidate.manifest.modules)
        or len(artifact_views) != len(candidate.artifacts)
    ):
        raise ProtocolError("Worker sealed activation is invalid")
    entries: list[_SealedWorkerStageEntry] = []
    diagnostics: list[WorkerDiagnosticArtifact] = []
    for view, artifact, module in zip(
        artifact_views,
        candidate.artifacts,
        candidate.manifest.modules,
        strict=True,
    ):
        if (
            not isinstance(view, _ValidatedWorkerModuleArtifact)
            or view.descriptor is not artifact
            or not isinstance(view.snapshot, _AdmittedWorkerSnapshot)
            or artifact.logical_name != module.logical_name
            or artifact.revision != module.revision
            or artifact.artifact_sha256 != module.artifact_sha256
            or view.snapshot.source_sha256 != artifact.source_sha256
            or view.snapshot.artifact_sha256 != artifact.artifact_sha256
            or view.snapshot.source_map_sha256 != artifact.source_map_sha256
            or view.snapshot.mapped_source.source_map_sha256
            != artifact.source_map_sha256
        ):
            raise ProtocolError("Worker sealed activation is invalid")
        artifact_bytes = view.snapshot.artifact_bytes
        if type(artifact_bytes) is not bytes:
            raise ProtocolError("Worker sealed activation is invalid")
        entries.append(
            _SealedWorkerStageEntry(
                artifact,
                module.logical_name,
                module.revision,
                module.artifact_sha256,
                module.registration_name,
                artifact_bytes,
                view.snapshot,
            )
        )
        diagnostics.append(
            _worker_artifact_diagnostic_source_from_snapshot(
                artifact.worker_artifact,
                view.snapshot,
                logical_name=module.logical_name,
                revision=module.revision,
                registration_name=module.registration_name,
                manifest_sha256=candidate.manifest.sha256,
                artifact_sha256=module.artifact_sha256,
                source_map_sha256=artifact.source_map_sha256,
            )
        )
    return _SealedWorkerActivation(
        candidate,
        artifact_views,
        tuple(entries),
        tuple(diagnostics),
    )


@dataclass(frozen=True, slots=True, repr=False)
class WorkerPreparedRootReceipt:
    transaction_id: UUID
    generation: int
    manifest_sha256: str
    candidate_root_key: str
    previous_root_key: str
    generation_create_wire_probe_ms: int

    def __post_init__(self) -> None:
        if (
            type(self.transaction_id) is not UUID
            or type(self.generation) is not int
            or self.generation <= 0
            or not _sha256_value(self.manifest_sha256)
            or not _safe_identity(self.candidate_root_key)
            or (
                self.previous_root_key != ""
                and not _safe_identity(self.previous_root_key)
            )
            or self.previous_root_key == self.candidate_root_key
            or type(self.generation_create_wire_probe_ms) is not int
            or self.generation_create_wire_probe_ms < 0
        ):
            raise ValueError("worker prepared root receipt is invalid")

    def __repr__(self) -> str:
        return (
            "WorkerPreparedRootReceipt("
            f"transaction_id={self.transaction_id!r}, "
            f"generation={self.generation}, "
            f"manifest_sha256={self.manifest_sha256!r}, "
            "candidate_root_key=<redacted>, previous_root_key=<redacted>, "
            "generation_create_wire_probe_ms=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class WorkerPromotionReceipt:
    transaction_id: UUID
    generation: int
    manifest_sha256: str
    root_key: str
    previous_root_key: str
    acknowledged: bool
    generation_create_wire_probe_ms: int
    root_swap_ms: int

    def __post_init__(self) -> None:
        if (
            type(self.transaction_id) is not UUID
            or type(self.generation) is not int
            or self.generation <= 0
            or not _sha256_value(self.manifest_sha256)
            or not _safe_identity(self.root_key)
            or (
                self.previous_root_key != ""
                and not _safe_identity(self.previous_root_key)
            )
            or self.previous_root_key == self.root_key
            or type(self.acknowledged) is not bool
            or type(self.generation_create_wire_probe_ms) is not int
            or self.generation_create_wire_probe_ms < 0
            or type(self.root_swap_ms) is not int
            or self.root_swap_ms < 0
        ):
            raise ValueError("worker promotion receipt is invalid")

    def __repr__(self) -> str:
        return (
            "WorkerPromotionReceipt("
            f"transaction_id={self.transaction_id!r}, "
            f"generation={self.generation}, "
            f"manifest_sha256={self.manifest_sha256!r}, "
            "root_key=<redacted>, previous_root_key=<redacted>, "
            f"acknowledged={self.acknowledged!r}, "
            "generation_create_wire_probe_ms=<redacted>, "
            "root_swap_ms=<redacted>)"
        )


@dataclass(slots=True)
class _GenerationRecord:
    handle: WorkerGenerationHandle
    manifest: WorkerUniverseManifest
    export_catalog: tuple[WorkerExport, ...]
    root_key: str
    active: bool
    explicitly_retained: bool
    descriptor_cache_identities: frozenset[tuple[str, str, int, str, str]]
    binary_keys: frozenset[WorkerModuleBinaryKey]
    debug_view: WorkerGenerationDebugView | None
    operation_pins: int = 0


@dataclass(slots=True)
class _OperationLease:
    pin: OperationGenerationPin
    outcome_unknown: bool = False


GenerationFence = int | Callable[[], int]


@dataclass(frozen=True, slots=True)
class _WorkerUniverseLiveInventory:
    manifest_sha256s: frozenset[str]
    artifact_identities: frozenset[tuple[str, int, str]]
    descriptor_cache_identities: frozenset[tuple[str, str, int, str, str]]
    binary_keys: frozenset[WorkerModuleBinaryKey]


@dataclass(frozen=True, slots=True, repr=False)
class WorkerLifecycleReleasePlan:
    owner: object
    expected_fence: tuple[int, int]
    release: WorkerGenerationHandle | OperationGenerationPin
    remaining_views: tuple[WorkerGenerationDebugView, ...]
    released_names: tuple[str, ...]
    next_registration_refcounts: tuple[tuple[str, int], ...]
    next_registration_artifacts: tuple[tuple[str, tuple[str, str]], ...]

    def __repr__(self) -> str:
        return (
            "WorkerLifecycleReleasePlan(owner=<redacted>, "
            f"expected_fence={self.expected_fence!r}, "
            f"release={type(self.release).__name__}, "
            f"remaining_views={len(self.remaining_views)}, "
            f"released_names={len(self.released_names)})"
        )


class WorkerUniverseRegistry:
    """Atomic host registry for immutable Worker module generations."""

    def __init__(
        self,
        *,
        runtime_generation: GenerationFence,
        context_generation: GenerationFence,
    ) -> None:
        self._lock = RLock()
        self._runtime_generation_source = _fence_source(runtime_generation)
        self._context_generation_source = _fence_source(context_generation)
        self._runtime_generation = self._runtime_generation_source()
        self._context_generation = self._context_generation_source()
        _validate_positive_generation(self._runtime_generation)
        _validate_positive_generation(self._context_generation)
        self._state = WorkerUniverseState.EMPTY
        self._next_generation = 1
        self._pending: WorkerUniverseCandidate | None = None
        self._sealed_activations: dict[int, _SealedWorkerActivation] = {}
        self._active_generation: int | None = None
        self._handles: dict[int, WorkerGenerationHandle] = {}
        self._generations: dict[int, _GenerationRecord] = {}
        self._leases: dict[str, _OperationLease] = {}
        self._registration_refcounts: dict[str, int] = {}
        self._registration_artifacts: dict[str, tuple[str, str]] = {}
        self._quarantine_holds: set[str] = set()
        self._release_owner = object()

    @property
    def state(self) -> WorkerUniverseState:
        with self._lock:
            return self._state

    @property
    def active_handle(self) -> WorkerGenerationHandle | None:
        with self._lock:
            self._require_active_read()
            record = self._active_record()
            return None if record is None else record.handle

    @property
    def active_manifest(self) -> WorkerUniverseManifest | None:
        with self._lock:
            self._require_active_read()
            record = self._active_record()
            return None if record is None else record.manifest

    @property
    def active_root_key(self) -> str | None:
        with self._lock:
            self._require_active_read()
            record = self._active_record()
            return None if record is None else record.root_key

    def _confirmed_live_inventory(self) -> _WorkerUniverseLiveInventory | None:
        """Return exact cache roots only while host lifecycle state is certain."""
        with self._lock:
            if self._state not in (
                WorkerUniverseState.EMPTY,
                WorkerUniverseState.READY,
            ):
                return None
            try:
                self._require_current_fence()
            except ProtocolError:
                return None
            manifests = tuple(
                record.manifest
                for record in self._generations.values()
                if (
                    record.active
                    or record.explicitly_retained
                    or record.operation_pins > 0
                )
            )
            return _WorkerUniverseLiveInventory(
                frozenset(manifest.sha256 for manifest in manifests),
                frozenset(
                    (
                        module.logical_name.casefold(),
                        module.revision,
                        module.artifact_sha256,
                    )
                    for manifest in manifests
                    for module in manifest.modules
                ),
                frozenset(
                    identity
                    for record in self._generations.values()
                    if (
                        record.active
                        or record.explicitly_retained
                        or record.operation_pins > 0
                    )
                    for identity in record.descriptor_cache_identities
                ),
                frozenset(
                    binary_key
                    for record in self._generations.values()
                    if (
                        record.active
                        or record.explicitly_retained
                        or record.operation_pins > 0
                    )
                    for binary_key in record.binary_keys
                ),
            )

    def preview_handle(
        self,
        artifacts: tuple[WorkerModuleArtifact, ...],
        *,
        export_catalog: tuple[WorkerExport, ...] | None = None,
    ) -> WorkerGenerationHandle:
        """Preview the next exact identity without reserving or staging it.

        A later preparation (including an aborted one) can invalidate this
        preview. Callers must fence the actual candidate before target mutation.
        """
        with self._lock:
            self._require_available("preview")
            if self._state is WorkerUniverseState.PREPARING:
                raise ProtocolError("Worker universe already has a pending candidate")
            self._require_current_fence()
            _, manifest = _build_worker_manifest(self._next_generation, artifacts)
            catalog = manifest.exports if export_catalog is None else export_catalog
            if not _effective_export_catalog_matches(manifest, catalog):
                raise ProtocolError("Worker generation export catalog does not match its manifest")
            _require_registration_ownership(manifest, self._registration_artifacts)
            return WorkerGenerationHandle(
                self._runtime_generation, self._context_generation,
                self._next_generation, manifest.sha256,
            )

    def prepare(
        self,
        artifacts: tuple[WorkerModuleArtifact, ...],
        *,
        export_catalog: tuple[WorkerExport, ...] | None = None,
    ) -> WorkerUniverseCandidate:
        with self._lock:
            self._require_available("prepare")
            if self._state is WorkerUniverseState.PREPARING:
                raise ProtocolError("Worker universe already has a pending candidate")
            self._require_current_fence()
            generation = self._next_generation
            ordered_views, manifest = _build_worker_manifest(generation, artifacts)
            ordered = tuple(view.descriptor for view in ordered_views)
            effective_catalog = (
                manifest.exports if export_catalog is None else export_catalog
            )
            if not _effective_export_catalog_matches(manifest, effective_catalog):
                raise ProtocolError(
                    "Worker generation export catalog does not match its manifest"
                )
            _require_registration_ownership(
                manifest,
                self._registration_artifacts,
            )
            handle = WorkerGenerationHandle(
                self._runtime_generation,
                self._context_generation,
                generation,
                manifest.sha256,
            )
            new_artifacts = tuple(
                artifact
                for artifact, module in zip(ordered, manifest.modules, strict=True)
                if module.registration_name not in self._registration_refcounts
            )
            previous = self._active_record()
            candidate = WorkerUniverseCandidate(
                handle,
                manifest,
                effective_catalog,
                ordered,
                new_artifacts,
                None if previous is None else previous.handle,
            )
            activation = _seal_worker_activation(candidate, ordered_views)
            self._next_generation += 1
            self._handles[generation] = handle
            self._pending = candidate
            self._sealed_activations[generation] = activation
            self._state = WorkerUniverseState.PREPARING
            return candidate

    def discard(self, candidate: WorkerUniverseCandidate) -> None:
        with self._lock:
            self._require_available("discard")
            self._require_pending(candidate)
            try:
                self._require_current_fence()
            except ProtocolError:
                self._quarantine_candidate(candidate)
                self._pending = None
                self._state = WorkerUniverseState.BROKEN
                raise
            self._handles.pop(candidate.handle.generation, None)
            self._sealed_activations.pop(candidate.handle.generation, None)
            self._pending = None
            self._state = (
                WorkerUniverseState.READY
                if self._active_generation is not None
                else WorkerUniverseState.EMPTY
            )

    def _discard_exact_pending_without_fence(
        self,
        candidate: WorkerUniverseCandidate,
    ) -> None:
        """Undo an internally returned exact candidate before target access.

        Server preparation alone may use this after its post-prepare fence
        check fails.  Caller-supplied promotion still uses the public,
        fence-authenticated lifecycle paths.
        """

        with self._lock:
            self._require_available("internal candidate cleanup")
            self._require_pending(candidate)
            activation = self._sealed_activations.get(
                candidate.handle.generation
            )
            if activation is None or activation.candidate is not candidate:
                raise ProtocolError(
                    "Worker internal candidate cleanup is uncertain"
                )
            self._handles.pop(candidate.handle.generation, None)
            self._sealed_activations.pop(candidate.handle.generation, None)
            self._pending = None
            self._state = (
                WorkerUniverseState.READY
                if self._active_generation is not None
                else WorkerUniverseState.EMPTY
            )

    def confirm(
        self,
        candidate: WorkerUniverseCandidate,
        receipt: WorkerPromotionReceipt,
    ) -> WorkerGenerationConfirmation:
        with self._lock:
            self._require_available("confirm")
            self._require_current_fence()
            self._require_pending(candidate)
            current = self._active_record()
            expected_previous_root = "" if current is None else current.root_key
            if (
                not isinstance(receipt, WorkerPromotionReceipt)
                or not receipt.acknowledged
                or receipt.generation != candidate.handle.generation
                or receipt.manifest_sha256 != candidate.manifest.sha256
                or receipt.root_key != _generation_root_key(candidate.handle.generation)
                or receipt.previous_root_key != expected_previous_root
            ):
                raise ProtocolError("Worker promotion receipt is stale or unacknowledged")

            registration_refcounts = dict(self._registration_refcounts)
            registration_artifacts = dict(self._registration_artifacts)
            old_record = current
            if old_record is not None and not old_record.explicitly_retained:
                _adjust_manifest_registrations(
                    registration_refcounts,
                    old_record.manifest,
                    -1,
                )
            _adjust_manifest_registrations(
                registration_refcounts,
                candidate.manifest,
                1,
            )
            _require_registration_ownership(
                candidate.manifest,
                registration_artifacts,
            )
            for module in candidate.manifest.modules:
                registration_artifacts[module.registration_name] = (
                    module.logical_name.casefold(),
                    module.artifact_sha256,
                )
            released = _remove_zero_registration_refcounts(
                registration_refcounts,
                registration_artifacts,
                self._quarantine_holds,
            )
            confirmation = WorkerGenerationConfirmation(
                candidate.handle,
                released,
            )
            record = _GenerationRecord(
                candidate.handle,
                candidate.manifest,
                candidate.export_catalog,
                receipt.root_key,
                active=True,
                explicitly_retained=True,
                descriptor_cache_identities=frozenset(
                    identity
                    for artifact in candidate.artifacts
                    if (
                        identity := _worker_module_descriptor_cache_identity(
                            artifact
                        )
                    )
                    is not None
                ),
                binary_keys=frozenset(
                    artifact.binary_key for artifact in candidate.artifacts
                ),
                debug_view=self._sealed_activations[
                    candidate.handle.generation
                ].debug_view,
            )

            if old_record is not None:
                old_record.active = False
            self._generations[candidate.handle.generation] = record
            self._active_generation = candidate.handle.generation
            self._registration_refcounts = registration_refcounts
            self._registration_artifacts = registration_artifacts
            self._sealed_activations.pop(candidate.handle.generation, None)
            self._pending = None
            self._state = WorkerUniverseState.READY
            self._collect_unretained_generations()
            return confirmation

    def pin_active(self) -> OperationGenerationPin:
        with self._lock:
            self._require_available("pin")
            self._require_current_fence()
            record = self._active_record()
            if record is None:
                raise ProtocolError("Worker universe has no active generation")
            lease_id = _new_lease_id(self._leases)
            pin = OperationGenerationPin(
                record.handle,
                record.manifest,
                record.root_key,
                record.export_catalog,
                lease_id,
            )
            registration_refcounts = dict(self._registration_refcounts)
            _adjust_manifest_registrations(
                registration_refcounts,
                record.manifest,
                1,
            )
            record.operation_pins += 1
            self._registration_refcounts = registration_refcounts
            self._leases[lease_id] = _OperationLease(pin)
            return pin

    def release_generation(
        self,
        handle: WorkerGenerationHandle,
    ) -> tuple[str, ...]:
        return self._commit_release(self._preview_release(handle))

    def release_pin(self, pin: OperationGenerationPin) -> tuple[str, ...]:
        return self._commit_release(self._preview_release(pin))

    def _preview_release(
        self,
        release: WorkerGenerationHandle | OperationGenerationPin,
    ) -> WorkerLifecycleReleasePlan:
        with self._lock:
            self._require_available("preview release")
            self._require_current_fence()
            refcounts = dict(self._registration_refcounts)
            artifacts = dict(self._registration_artifacts)
            if type(release) is WorkerGenerationHandle:
                record = self._require_generation(release)
                if not record.explicitly_retained:
                    raise ProtocolError("Worker generation handle is stale or released")
                if not record.active:
                    _adjust_manifest_registrations(refcounts, record.manifest, -1)
            elif type(release) is OperationGenerationPin:
                lease = self._require_lease(release)
                if lease.outcome_unknown:
                    raise ProtocolError(
                        "Outcome-unknown Worker pin is retained until teardown"
                    )
                record = self._require_generation(release.handle)
                if record.operation_pins <= 0:
                    raise ProtocolError("Worker generation pin refcount is invalid")
                _adjust_manifest_registrations(refcounts, record.manifest, -1)
            else:
                raise TypeError("Worker lifecycle release capability is required")
            released = _remove_zero_registration_refcounts(
                refcounts,
                artifacts,
                self._quarantine_holds,
            )
            remaining = tuple(
                item.debug_view
                for _generation, item in sorted(self._generations.items())
                if item.debug_view is not None
                and self._record_live_after_release(item, record, release)
            )
            return WorkerLifecycleReleasePlan(
                self._release_owner,
                (self._runtime_generation, self._context_generation),
                release,
                remaining,
                released,
                tuple(sorted(refcounts.items(), key=lambda item: item[0].casefold())),
                tuple(sorted(artifacts.items(), key=lambda item: item[0].casefold())),
            )

    def _commit_release(
        self,
        plan: WorkerLifecycleReleasePlan,
    ) -> tuple[str, ...]:
        if type(plan) is not WorkerLifecycleReleasePlan:
            raise TypeError("Worker lifecycle release plan is required")
        with self._lock:
            self._require_available("commit release")
            self._require_current_fence()
            if (
                plan.owner is not self._release_owner
                or plan.expected_fence
                != (self._runtime_generation, self._context_generation)
            ):
                raise ProtocolError("Worker lifecycle release plan is stale")
            current = self._preview_release(plan.release)
            if (
                current.remaining_views != plan.remaining_views
                or current.released_names != plan.released_names
                or current.next_registration_refcounts
                != plan.next_registration_refcounts
                or current.next_registration_artifacts
                != plan.next_registration_artifacts
            ):
                raise ProtocolError("Worker lifecycle release plan is stale")
            release = plan.release
            if type(release) is WorkerGenerationHandle:
                record = self._require_generation(release)
                record.explicitly_retained = False
            else:
                lease = self._require_lease(release)
                record = self._require_generation(release.handle)
                del self._leases[release.lease_id]
                record.operation_pins -= 1
            self._registration_refcounts = dict(plan.next_registration_refcounts)
            self._registration_artifacts = dict(plan.next_registration_artifacts)
            self._collect_unretained_generations()
            return plan.released_names

    @staticmethod
    def _record_live_after_release(
        item: _GenerationRecord,
        released_record: _GenerationRecord,
        release: WorkerGenerationHandle | OperationGenerationPin,
    ) -> bool:
        explicitly_retained = item.explicitly_retained
        operation_pins = item.operation_pins
        if item is released_record:
            if type(release) is WorkerGenerationHandle:
                explicitly_retained = False
            else:
                operation_pins -= 1
        return item.active or explicitly_retained or operation_pins > 0

    def retain_outcome_unknown(self, pin: OperationGenerationPin) -> None:
        with self._lock:
            self._require_available("retain outcome-unknown pin")
            lease = self._require_lease(pin)
            lease.outcome_unknown = True
            if self._pending is not None:
                self._quarantine_candidate(self._pending)
            self._state = WorkerUniverseState.BROKEN
            self._pending = None
            self._require_current_fence()

    def mark_broken(
        self,
        candidate: WorkerUniverseCandidate | None = None,
    ) -> None:
        with self._lock:
            self._require_available("mark broken")
            if candidate is not None:
                self._require_pending(candidate)
            elif self._pending is not None:
                candidate = self._pending
            if candidate is not None:
                self._quarantine_candidate(candidate)
            self._pending = None
            self._state = WorkerUniverseState.BROKEN

    def registration_refcount(self, name: str) -> int:
        if not isinstance(name, str):
            raise TypeError("worker registration name must be a string")
        with self._lock:
            return self._registration_refcounts.get(name, 0) + (
                1 if name in self._quarantine_holds else 0
            )

    def teardown(self) -> tuple[str, ...]:
        with self._lock:
            if self._pending is not None:
                self._quarantine_candidate(self._pending)
            registrations = tuple(
                sorted(
                    set(self._registration_refcounts) | self._quarantine_holds,
                    key=str.casefold,
                )
            )
            self._pending = None
            self._active_generation = None
            self._sealed_activations.clear()
            self._handles.clear()
            self._generations.clear()
            self._leases.clear()
            self._registration_refcounts.clear()
            self._registration_artifacts.clear()
            self._quarantine_holds.clear()
            self._state = WorkerUniverseState.CLOSED
            return registrations

    close = teardown

    def _active_record(self) -> _GenerationRecord | None:
        if self._active_generation is None:
            return None
        return self._generations.get(self._active_generation)

    def _require_available(self, operation: str) -> None:
        if self._state in (WorkerUniverseState.BROKEN, WorkerUniverseState.CLOSED):
            raise ProtocolError(
                f"Worker universe {operation} is unavailable in {self._state.value} state"
            )

    def _require_current_fence(self) -> None:
        if (
            self._runtime_generation_source() != self._runtime_generation
            or self._context_generation_source() != self._context_generation
        ):
            raise ProtocolError("Worker universe runtime/context fence is stale")

    def _require_active_read(self) -> None:
        if self._state is WorkerUniverseState.BROKEN:
            raise ProtocolError("Worker universe active generation is unavailable in broken state")
        if self._state is not WorkerUniverseState.CLOSED:
            self._require_current_fence()

    def _require_pending(self, candidate: WorkerUniverseCandidate) -> None:
        if (
            not isinstance(candidate, WorkerUniverseCandidate)
            or self._state is not WorkerUniverseState.PREPARING
            or self._pending is not candidate
            or self._handles.get(candidate.handle.generation) is not candidate.handle
        ):
            raise ProtocolError("Worker universe candidate is stale or forged")

    def _require_sealed_activation(
        self,
        candidate: WorkerUniverseCandidate,
    ) -> _SealedWorkerActivation:
        """Return the O(1), exact-candidate capability for pending staging."""
        with self._lock:
            self._require_available("sealed activation")
            self._require_current_fence()
            self._require_pending(candidate)
            activation = self._sealed_activations.get(candidate.handle.generation)
            if activation is None or activation.candidate is not candidate:
                raise ProtocolError("Worker sealed activation is stale or forged")
            return activation

    def _candidate_diagnostics(
        self,
        candidate: WorkerUniverseCandidate,
    ) -> tuple[WorkerDiagnosticArtifact, ...]:
        """Return the exact diagnostics sealed for one pending candidate."""
        with self._lock:
            return self._require_sealed_activation(candidate).diagnostics

    def _bind_candidate_debug_view(
        self,
        candidate: WorkerUniverseCandidate,
        debug_view: WorkerGenerationDebugView,
    ) -> None:
        with self._lock:
            activation = self._require_sealed_activation(candidate)
            if (
                type(debug_view) is not WorkerGenerationDebugView
                or debug_view.handle is not candidate.handle
                or debug_view.manifest is not candidate.manifest
                or activation.debug_view is not None
            ):
                raise ProtocolError("Worker candidate debug view is invalid")
            self._sealed_activations[candidate.handle.generation] = (
                _SealedWorkerActivation(
                    activation.candidate,
                    activation.artifact_views,
                    activation.stage_entries,
                    activation.diagnostics,
                    debug_view,
                )
            )

    def _candidate_debug_view(
        self,
        candidate: WorkerUniverseCandidate,
    ) -> WorkerGenerationDebugView:
        with self._lock:
            view = self._require_sealed_activation(candidate).debug_view
            if view is None:
                raise ProtocolError("Worker candidate debug view is unavailable")
            return view

    def _operation_debug_view(
        self,
        pin: OperationGenerationPin,
    ) -> WorkerGenerationDebugView:
        with self._lock:
            self._require_current_fence()
            lease = self._require_lease(pin)
            record = self._require_generation(lease.pin.handle)
            if record.debug_view is None:
                raise ProtocolError("Worker generation debug view is unavailable")
            return record.debug_view

    def _retained_debug_views(self) -> tuple[WorkerGenerationDebugView, ...]:
        with self._lock:
            if self._state is WorkerUniverseState.CLOSED:
                raise ProtocolError("Worker generation debug views are unavailable")
            self._require_current_fence()
            return tuple(
                record.debug_view
                for _generation, record in sorted(self._generations.items())
                if record.debug_view is not None
                and (
                    record.active
                    or record.explicitly_retained
                    or record.operation_pins > 0
                )
            )

    def _require_generation(
        self,
        handle: WorkerGenerationHandle,
    ) -> _GenerationRecord:
        if (
            not isinstance(handle, WorkerGenerationHandle)
            or handle.runtime_generation != self._runtime_generation
            or handle.context_generation != self._context_generation
            or self._handles.get(handle.generation) is not handle
        ):
            raise ProtocolError("Worker generation handle is stale or forged")
        record = self._generations.get(handle.generation)
        if (
            record is None
            or record.handle is not handle
            or handle.manifest_sha256 != record.manifest.sha256
        ):
            raise ProtocolError("Worker generation handle is stale or released")
        return record

    def _require_lease(self, pin: OperationGenerationPin) -> _OperationLease:
        if not isinstance(pin, OperationGenerationPin):
            raise ProtocolError("Worker generation pin is stale or forged")
        lease = self._leases.get(pin.lease_id)
        if lease is None or lease.pin is not pin:
            raise ProtocolError("Worker generation pin is stale or forged")
        return lease

    def _install_registration_refcounts(
        self,
        refcounts: dict[str, int],
    ) -> tuple[str, ...]:
        released = _remove_zero_registration_refcounts(
            refcounts,
            self._registration_artifacts,
            self._quarantine_holds,
        )
        self._registration_refcounts = refcounts
        return released

    def _collect_unretained_generations(self) -> None:
        for generation, record in tuple(self._generations.items()):
            if not record.active and not record.explicitly_retained and record.operation_pins == 0:
                del self._generations[generation]
                self._handles.pop(generation, None)

    def _quarantine_candidate(self, candidate: WorkerUniverseCandidate) -> None:
        for artifact in candidate.new_artifacts:
            self._quarantine_holds.add(
                registration_name(artifact.logical_name, artifact.artifact_sha256)
            )


def prepare_worker_root_instruction(
    candidate: WorkerUniverseCandidate,
    transaction_id: UUID,
    previous_root_key: str,
) -> str:
    """Create and seal a candidate root without changing the active pointer."""
    if not isinstance(candidate, WorkerUniverseCandidate):
        raise TypeError("worker universe candidate is required")
    if type(transaction_id) is not UUID:
        raise TypeError("worker root transaction id must be a UUID")
    if previous_root_key != "" and not _safe_identity(previous_root_key):
        raise ValueError("previous Worker root key is invalid")
    manifest = candidate.manifest
    reserved_names = {
        binding.target_module.casefold()
        for binding in manifest.wiring
        if binding.target_kind == "original"
    }
    reserved_names.update(
        module.logical_name.casefold() for module in manifest.modules
    )

    def allocate_local(preferred: str) -> str:
        candidate_name = preferred
        suffix = 0
        while candidate_name.casefold() in reserved_names:
            suffix += 1
            candidate_name = f"{preferred}_{suffix}"
        reserved_names.add(candidate_name.casefold())
        return candidate_name

    phase_variable = allocate_local("ЭтапПубликацииWorker")
    objects_variable = allocate_local("ОбъектыКандидатаWorker")
    dependency_variables = tuple(
        (
            allocate_local(f"ИсточникЗависимостиWorker{index}"),
            allocate_local(f"ЦельЗависимостиWorker{index}"),
        )
        for index, _ in enumerate(manifest.wiring)
    )
    module_variables = tuple(
        allocate_local(f"ОбъектМодуляWorker{index}")
        for index, _ in enumerate(manifest.modules)
    )
    exports_variable = allocate_local("ЭкспортыКандидатаWorker")
    exports_data_variable = allocate_local("ДанныеЭкспортовКандидатаWorker")
    modules_variable = allocate_local("МодулиКандидатаWorker")
    root_data_variable = allocate_local("ДанныеКорняКандидатаWorker")
    root_variable = allocate_local("КореньКандидатаWorker")
    prepared_data_variable = allocate_local("ДанныеПодготовленногоКорняWorker")
    prepared_variable = allocate_local("ПодготовленныйКореньWorker")
    error_variable = allocate_local("ОшибкаПубликацииWorker")
    artifact_error_variable = allocate_local("МаркерОшибкиАртефактаWorker")
    prepare_started_variable = allocate_local("НачалоПодготовкиКандидатаWorker")
    prepare_finished_variable = allocate_local("КонецПодготовкиКандидатаWorker")
    lines = [
        f'{phase_variable} = "create";',
        f'{artifact_error_variable} = "";',
        "Попытка",
        f"    {prepare_started_variable} = "
        "ТекущаяУниверсальнаяДатаВМиллисекундах();",
        f"    {objects_variable} = Новый Соответствие;",
    ]
    for module in manifest.modules:
        artifact_error_header = (
            "onec-worker-artifact-stage="
            f"artifact_sha256={module.artifact_sha256};"
            f"logical_name_sha256={sha256(module.logical_name.casefold().encode('utf-8')).hexdigest()};"
            "phase=create;boundary=create"
        )
        lines.append(f"    {artifact_error_variable} = {_bsl_string(artifact_error_header)};")
        lines.append(
            f"    {objects_variable}.Вставить("
            f"{_bsl_string(module.logical_name)}, ВнешниеОбработки.Создать("
            f"{_bsl_string(module.registration_name)}, Ложь));"
        )
    lines.append(f'    {artifact_error_variable} = "";')
    lines.append(f'    {phase_variable} = "wire";')
    for index, binding in enumerate(manifest.wiring):
        source_variable, target_variable = dependency_variables[index]
        lines.append(
            f"    {source_variable} = {objects_variable}.Получить("
            f"{_bsl_string(binding.source_module)});"
        )
        if binding.target_kind == "overloaded":
            lines.append(
                f"    {target_variable} = {objects_variable}.Получить("
                f"{_bsl_string(binding.target_module)});"
            )
        else:
            lines.append(f"    {target_variable} = {binding.target_module};")
        lines.extend(
            (
                f"    {source_variable}.{binding.export_variable} = {target_variable};",
                f"    Если ТипЗнч({source_variable}.{binding.export_variable}) "
                f"<> ТипЗнч({target_variable}) Тогда",
                '        ВызватьИсключение "worker dependency field type mismatch";',
                "    КонецЕсли;",
            )
        )
    lines.append(f'    {phase_variable} = "probe";')
    for index, module in enumerate(manifest.modules):
        object_variable = module_variables[index]
        lines.extend(
            (
                f"    {object_variable} = {objects_variable}.Получить("
                f"{_bsl_string(module.logical_name)});",
                f"    Если ТипЗнч({object_variable}) = Тип(\"Неопределено\") Тогда",
                '        ВызватьИсключение "worker module object is unavailable";',
                "    КонецЕсли;",
            )
        )
    lines.append(f"    {exports_variable} = Новый Массив;")
    for export in manifest.exports:
        lines.append(
            f"    {exports_variable}.Добавить("
            f"{_bsl_string(export.public_path)});"
        )
    root_key = _generation_root_key(manifest.generation)
    lines.extend(
        (
            f"    {modules_variable} = Новый ФиксированноеСоответствие("
            f"{objects_variable});",
            f"    {exports_variable} = Новый ФиксированныйМассив("
            f"{exports_variable});",
            f"    {exports_data_variable} = Новый Структура;",
            f'    {exports_data_variable}.Вставить("Kind", '
            '"OnecWorkerExportsV1");',
            f'    {exports_data_variable}.Вставить("Items", '
            f"{exports_variable});",
            f"    {exports_variable} = Новый ФиксированнаяСтруктура("
            f"{exports_data_variable});",
            f"    {root_data_variable} = Новый Структура;",
            f'    {root_data_variable}.Вставить("ManifestSha256", '
            f"{_bsl_string(manifest.sha256)});",
            f'    {root_data_variable}.Вставить("RootKey", '
            f"{_bsl_string(root_key)});",
            f'    {root_data_variable}.Вставить("Modules", '
            f"{modules_variable});",
            f'    {root_data_variable}.Вставить("Exports", '
            f"{exports_variable});",
        )
    )
    lines.extend(
        (
            f"    {root_variable} = Новый ФиксированнаяСтруктура("
            f"{root_data_variable});",
            f"    {prepare_finished_variable} = "
            "ТекущаяУниверсальнаяДатаВМиллисекундах();",
            "Исключение",
            f"    {error_variable} = ПодробноеПредставлениеОшибки(ИнформацияОбОшибке());",
            f'    Если {artifact_error_variable} <> "" Тогда',
            f'        {error_variable} = {artifact_error_variable} + ";diagnostic_utf16_length=" '
            f'+ Формат(СтрДлина({error_variable}), "ЧГ=0; ЧДЦ=0; ЧН=0") '
            f"+ Символы.ПС + {error_variable};",
            "    КонецЕсли;",
            '    ВызватьИсключение "onec-worker-root-prepare-stage=" '
            f"+ {phase_variable} + Символы.ПС + {error_variable};",
            "КонецПопытки;",
            f"{prepared_data_variable} = Новый Структура;",
            f'{prepared_data_variable}.Вставить("TransactionId", '
            f"{_bsl_string(str(transaction_id))});",
            f'{prepared_data_variable}.Вставить("Generation", '
            f"{manifest.generation});",
            f'{prepared_data_variable}.Вставить("ManifestSha256", '
            f"{_bsl_string(manifest.sha256)});",
            f'{prepared_data_variable}.Вставить("CandidateRootKey", '
            f"{_bsl_string(root_key)});",
            f'{prepared_data_variable}.Вставить("PreviousRootKey", '
            f"{_bsl_string(previous_root_key)});",
            f'{prepared_data_variable}.Вставить("Root", {root_variable});',
            f'{prepared_data_variable}.Вставить("PrepareMs", '
            f"{prepare_finished_variable} - {prepare_started_variable});",
            f"{prepared_variable} = Новый ФиксированнаяСтруктура("
            f"{prepared_data_variable});",
            f"Контекст.Вставить({_bsl_string(_prepared_root_context_key(transaction_id))}, "
            f"{prepared_variable});",
            "Результат = "
            f"{_bsl_string(_PREPARED_ROOT_RECEIPT_PREFIX + '|' + str(transaction_id) + '|')} + "
            f'Формат({manifest.generation}, "ЧГ=0; ЧДЦ=0; ЧН=0") + "|" + '
            f"{_bsl_string(manifest.sha256 + '|' + root_key + '|' + _wire_root_key(previous_root_key) + '|')} + "
            f"Формат({prepare_finished_variable} - {prepare_started_variable}, "
            '"ЧГ=0; ЧДЦ=0; ЧН=0");',
        )
    )
    return "\n".join(lines)


def swap_worker_root_instruction(prepared: WorkerPreparedRootReceipt) -> str:
    """Guard and publish one exact prepared root as the active root."""
    if type(prepared) is not WorkerPreparedRootReceipt:
        raise TypeError("worker prepared root receipt is required")
    prepared_key = _prepared_root_context_key(prepared.transaction_id)
    outcome_key = _root_swap_outcome_context_key(prepared.transaction_id)
    previous = prepared.previous_root_key
    lines = [
        "Результат = Неопределено;",
        f"Если Не Контекст.Свойство({_bsl_string(outcome_key)}, Результат) Тогда",
        "    ПодготовленныйКореньWorker = Неопределено;",
        "    ТекущийКореньWorker = Неопределено;",
        "    Попытка",
        f"        Если Не Контекст.Свойство({_bsl_string(prepared_key)}, "
        "ПодготовленныйКореньWorker) Тогда",
        '            ВызватьИсключение "prepared root is unavailable";',
        "        КонецЕсли;",
        "        Если ПодготовленныйКореньWorker.TransactionId <> "
        f"{_bsl_string(str(prepared.transaction_id))} Или "
        "ПодготовленныйКореньWorker.Generation <> "
        f"{prepared.generation} Или "
        "ПодготовленныйКореньWorker.ManifestSha256 <> "
        f"{_bsl_string(prepared.manifest_sha256)} Или "
        "ПодготовленныйКореньWorker.CandidateRootKey <> "
        f"{_bsl_string(prepared.candidate_root_key)} Или "
        "ПодготовленныйКореньWorker.PreviousRootKey <> "
        f"{_bsl_string(previous)} Тогда",
        '            ВызватьИсключение "prepared root identity mismatch";',
        "        КонецЕсли;",
    ]
    if previous:
        lines.extend(
            (
                '        Если Не Контекст.Свойство("RuntimeWorkerActiveGeneration", '
                "ТекущийКореньWorker) Или ТекущийКореньWorker.RootKey <> "
                f"{_bsl_string(previous)} Тогда",
                '            ВызватьИсключение "previous root identity mismatch";',
                "        КонецЕсли;",
            )
        )
    else:
        lines.extend(
            (
                '        Если Контекст.Свойство("RuntimeWorkerActiveGeneration", '
                "ТекущийКореньWorker) Тогда",
                '            ВызватьИсключение "unexpected previous root";',
                "        КонецЕсли;",
            )
        )
    lines.extend(
        (
            "    Исключение",
            "        ОшибкаСменыКорняWorker = ОписаниеОшибки();",
            '        ВызватьИсключение "onec-worker-root-swap-stage=guard" '
            "+ Символы.ПС + ОшибкаСменыКорняWorker;",
            "    КонецПопытки;",
            "    НачалоСменыКорняWorker = "
            "ТекущаяУниверсальнаяДатаВМиллисекундах();",
            '    Контекст.Вставить("RuntimeWorkerActiveGeneration", '
            "ПодготовленныйКореньWorker.Root);",
            f"    Контекст.Удалить({_bsl_string(prepared_key)});",
            "    КонецСменыКорняWorker = "
            "ТекущаяУниверсальнаяДатаВМиллисекундах();",
            "    Результат = "
            f"{_bsl_string(_ROOT_SWAP_RECEIPT_PREFIX + '|' + str(prepared.transaction_id) + '|')} + "
            f'Формат({prepared.generation}, "ЧГ=0; ЧДЦ=0; ЧН=0") + "|" + '
            f"{_bsl_string(prepared.manifest_sha256 + '|' + prepared.candidate_root_key + '|' + _wire_root_key(previous) + '|1|')} + "
            "Формат(ПодготовленныйКореньWorker.PrepareMs, "
            '"ЧГ=0; ЧДЦ=0; ЧН=0") + "|" + '
            "Формат(КонецСменыКорняWorker - НачалоСменыКорняWorker, "
            '"ЧГ=0; ЧДЦ=0; ЧН=0");',
            f"    Контекст.Вставить({_bsl_string(outcome_key)}, Результат);",
            "КонецЕсли;",
        )
    )
    return "\n".join(lines)


def discard_worker_root_instruction(prepared: WorkerPreparedRootReceipt) -> str:
    """Discard an exact prepared root without touching the active pointer."""
    if type(prepared) is not WorkerPreparedRootReceipt:
        raise TypeError("worker prepared root receipt is required")
    prepared_key = _prepared_root_context_key(prepared.transaction_id)
    outcome_key = _root_discard_outcome_context_key(prepared.transaction_id)
    return "\n".join(
        (
            "Результат = Неопределено;",
            f"Если Не Контекст.Свойство({_bsl_string(outcome_key)}, Результат) Тогда",
            "    ПодготовленныйКореньWorker = Неопределено;",
            "    Попытка",
            f"        Если Не Контекст.Свойство({_bsl_string(prepared_key)}, "
            "ПодготовленныйКореньWorker) Тогда",
            '            ВызватьИсключение "prepared root is unavailable";',
            "        КонецЕсли;",
            "        Если ПодготовленныйКореньWorker.TransactionId <> "
            f"{_bsl_string(str(prepared.transaction_id))} Или "
            "ПодготовленныйКореньWorker.Generation <> "
            f"{prepared.generation} Или "
            "ПодготовленныйКореньWorker.ManifestSha256 <> "
            f"{_bsl_string(prepared.manifest_sha256)} Тогда",
            '            ВызватьИсключение "prepared root identity mismatch";',
            "        КонецЕсли;",
            "    Исключение",
            "        ОшибкаУдаленияКорняWorker = ОписаниеОшибки();",
            '        ВызватьИсключение "onec-worker-root-discard-stage=guard" '
            "+ Символы.ПС + ОшибкаУдаленияКорняWorker;",
            "    КонецПопытки;",
            f"    Контекст.Удалить({_bsl_string(prepared_key)});",
            "    Результат = "
            f"{_bsl_string(_ROOT_DISCARD_RECEIPT_PREFIX + '|' + str(prepared.transaction_id) + '|' + str(prepared.generation) + '|' + prepared.manifest_sha256 + '|' + prepared.candidate_root_key)};",
            f"    Контекст.Вставить({_bsl_string(outcome_key)}, Результат);",
            "КонецЕсли;",
        )
    )


def _wire_root_key(value: str) -> str:
    return "-" if value == "" else value


def _unwire_root_key(value: str) -> str:
    return "" if value == "-" else value


def _prepared_root_context_key(transaction_id: UUID) -> str:
    return f"__OnecWorkerPrepared_{transaction_id.hex}"


def _root_swap_outcome_context_key(transaction_id: UUID) -> str:
    return f"__OnecWorkerSwapOutcome_{transaction_id.hex}"


def _root_discard_outcome_context_key(transaction_id: UUID) -> str:
    return f"__OnecWorkerDiscardOutcome_{transaction_id.hex}"


def _bsl_string(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


@dataclass(frozen=True, slots=True, repr=False)
class WorkerRegistrationRecord:
    target_incarnation_id: UUID
    canonical_module: str
    artifact_sha256: str
    registration_name: str
    exact_temp_storage_url: str
    locator_profile_id: str
    stage_transaction_id: UUID
    batch_digest: str
    platform_build: str
    receipt_admission: object

    def __post_init__(self) -> None:
        if (
            type(self.target_incarnation_id) is not UUID
            or not _safe_identity(self.canonical_module)
            or self.canonical_module != self.canonical_module.casefold()
            or not _sha256_value(self.artifact_sha256)
            or not _bsl_identifier(self.registration_name)
            or _temp_storage_session_id(self.exact_temp_storage_url) is None
            or self.locator_profile_id != _WORKER_LOCATOR_PROFILE_ID
            or type(self.stage_transaction_id) is not UUID
            or not _sha256_value(self.batch_digest)
            or type(self.platform_build) is not str
            or not self.platform_build
            or self.receipt_admission is not _REGISTRATION_RECEIPT_ADMISSION
        ):
            raise ValueError("worker registration record is invalid")

    def module_location(self, generated_line: int) -> ModuleLocation:
        if type(generated_line) is not int or generated_line < 1:
            raise ValueError("generated line must be positive")
        if (
            self.receipt_admission is not _REGISTRATION_RECEIPT_ADMISSION
            or self.platform_build != _WORKER_LOCATOR_PLATFORM_BUILD
        ):
            raise ProtocolError("Worker breakpoint locator is unsupported")
        return ModuleLocation(
            "ExtMDModule",
            self.exact_temp_storage_url,
            _WORKER_MODULE_OBJECT_ID,
            _WORKER_MODULE_PROPERTY_ID,
            generated_line,
            "",
            0,
        )

    def __repr__(self) -> str:
        return (
            "WorkerRegistrationRecord("
            f"target_incarnation_id={self.target_incarnation_id!r}, "
            f"canonical_module={self.canonical_module!r}, "
            f"artifact_sha256={self.artifact_sha256!r}, "
            f"registration_name={self.registration_name!r}, "
            "exact_temp_storage_url=<redacted>, "
            f"locator_profile_id={self.locator_profile_id!r}, "
            f"stage_transaction_id={self.stage_transaction_id!r}, "
            f"batch_digest={self.batch_digest!r}, "
            f"platform_build={self.platform_build!r}, "
            "receipt_admission=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class WorkerModuleDebugView:
    source_units: tuple[SourceUnitRef, ...]
    canonical_module: str
    mapped_source: MappedSource
    visible_context: VisibleSourceContext | None
    artifact_sha256: str
    source_map_sha256: str
    registration: WorkerRegistrationRecord
    generated_line_index: LineIndex = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            type(self.source_units) is not tuple
            or not self.source_units
            or any(not isinstance(unit, SourceUnitRef) for unit in self.source_units)
            or not _safe_identity(self.canonical_module)
            or self.canonical_module != self.canonical_module.casefold()
            or not isinstance(self.mapped_source, MappedSource)
            or (
                self.visible_context is not None
                and not isinstance(self.visible_context, VisibleSourceContext)
            )
            or not _sha256_value(self.artifact_sha256)
            or not _sha256_value(self.source_map_sha256)
            or self.mapped_source.source_map_sha256 != self.source_map_sha256
            or type(self.registration) is not WorkerRegistrationRecord
            or self.registration.canonical_module != self.canonical_module
            or self.registration.artifact_sha256 != self.artifact_sha256
        ):
            raise ValueError("worker module debug view is invalid")
        if self.source_units != _worker_debug_source_units(self.mapped_source):
            raise ValueError("worker debug sources do not match the mapped source")
        object.__setattr__(
            self,
            "generated_line_index",
            LineIndex(self.mapped_source.text),
        )

    @property
    def source_unit(self) -> SourceUnitRef:
        """Compatibility accessor for a module with one proven visible source."""
        if len(self.source_units) != 1:
            raise ProtocolError("Worker debug source identity is ambiguous")
        return self.source_units[0]

    def __repr__(self) -> str:
        return (
            "WorkerModuleDebugView("
            f"source_units={self.source_units!r}, "
            f"canonical_module={self.canonical_module!r}, "
            f"artifact_sha256={self.artifact_sha256!r}, "
            f"source_map_sha256={self.source_map_sha256!r}, "
            "mapped_source=<redacted>, visible_context=<redacted>, "
            "registration=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class WorkerGenerationDebugView:
    handle: WorkerGenerationHandle
    manifest: WorkerUniverseManifest
    modules: tuple[WorkerModuleDebugView, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.handle, WorkerGenerationHandle)
            or not isinstance(self.manifest, WorkerUniverseManifest)
            or self.handle.generation != self.manifest.generation
            or self.handle.manifest_sha256 != self.manifest.sha256
            or type(self.modules) is not tuple
            or len(self.modules) != len(self.manifest.modules)
            or any(type(module) is not WorkerModuleDebugView for module in self.modules)
            or any(
                module.canonical_module != descriptor.logical_name.casefold()
                or module.artifact_sha256 != descriptor.artifact_sha256
                or module.registration.registration_name
                != descriptor.registration_name
                for module, descriptor in zip(
                    self.modules,
                    self.manifest.modules,
                    strict=True,
                )
            )
        ):
            raise ValueError("worker generation debug view is invalid")

    def __repr__(self) -> str:
        return (
            "WorkerGenerationDebugView("
            f"handle={self.handle!r}, manifest_sha256={self.manifest.sha256!r}, "
            f"modules={len(self.modules)}, provenance=<redacted>)"
        )


def _worker_debug_source_units(
    mapped_source: MappedSource,
) -> tuple[SourceUnitRef, ...]:
    """Enumerate exact identities from an admitted flattened map, without aliases."""
    compact_spans = getattr(mapped_source.source_map, "compact_visible_spans", None)
    references = (
        tuple(reference for _role, reference, _span in compact_spans())
        if callable(compact_spans)
        else tuple(
            reference
            for segment in mapped_source.source_map.segments
            for reference in (segment.origin_ref, segment.anchor_ref)
        )
    )
    units: dict[tuple[object, str, int], SourceUnitRef] = {}
    for reference in references:
        if reference is None:
            continue
        if not isinstance(reference, SourceUnitRef):
            raise ProtocolError("Worker debug source map is not flattened")
        key = (reference.kind, reference.unit_id, reference.revision)
        existing = units.get(key)
        if existing is not None and existing.source_sha256 != reference.source_sha256:
            raise ProtocolError("Worker debug sources have conflicting source identities")
        units[key] = reference
    return tuple(units.values())


@dataclass(frozen=True, slots=True)
class WorkerMutationReservation:
    token: UUID
    commit: Callable[[object], object] = field(repr=False)
    abort: Callable[[BaseException], object] = field(repr=False)


@dataclass(frozen=True, slots=True)
class PreparedWorkerMutation:
    instruction: str = field(repr=False)
    reservation: WorkerMutationReservation = field(repr=False)
    owner: ServerWorkerUniverseRegistry = field(repr=False)

    def commit(self, result: object) -> object:
        return self.owner.commit_mutation(self, result)

    def abort(self, error: BaseException) -> object:
        return self.owner.abort_mutation(self, error)


class ServerWorkerUniverseRegistry:
    """Target-side counterpart for a host Worker universe registry."""

    def __init__(
        self,
        host: WorkerUniverseRegistry,
        instruction_executor: Callable[[str], object],
        *,
        target_incarnation_id: UUID | None = None,
        platform_build: str = _WORKER_LOCATOR_PLATFORM_BUILD,
        mutation_executor: Callable[[PreparedWorkerMutation], object] | None = None,
    ) -> None:
        if not isinstance(host, WorkerUniverseRegistry):
            raise TypeError("host Worker universe registry is required")
        if not callable(instruction_executor):
            raise TypeError("worker universe instruction executor must be callable")
        if target_incarnation_id is not None and type(target_incarnation_id) is not UUID:
            raise TypeError("target incarnation ID must be a UUID")
        if type(platform_build) is not str or not platform_build:
            raise TypeError("platform build must be a non-empty string")
        self._host = host
        self._instruction_executor = instruction_executor
        self._mutation_executor = mutation_executor
        self._mutations: dict[UUID, PreparedWorkerMutation] = {}
        self._claimed_mutations: set[UUID] = set()
        self._target_incarnation_id = target_incarnation_id or uuid4()
        self._platform_build = platform_build
        self._lock = RLock()
        # Exact 8.3.27 has no supported external-processing Disconnect API.
        # Keep every successful content identity for this target session,
        # including identities whose host generation refs reached zero.
        self._registrations: dict[str, WorkerRegistrationRecord] = {}
        self._candidate_registrations: dict[int, set[str]] = {}
        self._instantiated_registrations: set[str] = set()
        self._compile_failed_registrations: set[str] = set()
        self._prepared_roots: dict[
            UUID,
            tuple[WorkerUniverseCandidate, WorkerPreparedRootReceipt],
        ] = {}
        self._storage_session_id: str | None = None
        self._orphan_urls: set[str] = set()
        self._broken = False

    def reserve_mutation(
        self, instruction: str, *, commit: Callable[[object], object],
        abort: Callable[[BaseException], object],
    ) -> PreparedWorkerMutation:
        with self._lock:
            if self._broken or self._host.state is WorkerUniverseState.CLOSED or self._mutations:
                raise ProtocolError("Worker target mutation is unavailable")
            reservation = WorkerMutationReservation(uuid4(), commit, abort)
            prepared = PreparedWorkerMutation(instruction, reservation, self)
            self._mutations[reservation.token] = prepared
            return prepared

    def _take_mutation(self, prepared: PreparedWorkerMutation) -> WorkerMutationReservation:
        reservation = self._require_exact_mutation(prepared)
        del self._mutations[reservation.token]
        self._claimed_mutations.discard(reservation.token)
        return reservation

    def _require_exact_mutation(self, prepared: PreparedWorkerMutation) -> WorkerMutationReservation:
        if type(prepared) is not PreparedWorkerMutation or prepared.owner is not self:
            raise ProtocolError("Worker mutation reservation is stale or forged")
        reservation = prepared.reservation
        if self._mutations.get(reservation.token) is not prepared:
            raise ProtocolError("Worker mutation reservation is stale or forged")
        return reservation

    def commit_mutation(self, prepared: PreparedWorkerMutation, result: object) -> object:
        with self._lock:
            reservation = self._take_mutation(prepared)
            return reservation.commit(result)

    def abort_mutation(self, prepared: PreparedWorkerMutation, error: BaseException) -> object:
        with self._lock:
            reservation = self._take_mutation(prepared)
            return reservation.abort(error)

    def execute_mutation(self, prepared: PreparedWorkerMutation) -> object:
        with self._lock:
            if self._broken or self._host.state is WorkerUniverseState.CLOSED:
                raise ProtocolError("Worker target mutation is unavailable")
            reservation = self._require_exact_mutation(prepared)
            if reservation.token in self._claimed_mutations:
                raise ProtocolError("Worker mutation execution was already claimed")
            # Claim once before crossing either remote boundary. Completion
            # consumes this claim; another caller cannot replay or race it.
            self._claimed_mutations.add(reservation.token)
        # A coordinator adapter owns commit/abort after accepting this plan.
        # In particular the caller must never abort its timed-out ticket.
        if self._mutation_executor is not None:
            return self._mutation_executor(prepared)
        try:
            result = self._instruction_executor(prepared.instruction)
        except BaseException as error:
            return self.abort_mutation(prepared, error)
        return self.commit_mutation(prepared, result)

    def prepare(
        self,
        artifacts: tuple[WorkerModuleArtifact, ...],
    ) -> WorkerUniverseCandidate:
        with self._lock:
            if self._broken:
                raise ProtocolError("Worker target registry is broken")
            if self._mutations:
                raise ProtocolError("Worker target mutation is unavailable")
            candidate = self._host.prepare(artifacts)
            try:
                activation = self._sealed_activation(candidate)
                batches = self._stage_batches_from_activation(activation)
            except BaseException:
                try:
                    self._host._discard_exact_pending_without_fence(candidate)
                except BaseException:
                    self._break_pending(candidate)
                    raise _promotion_unknown(candidate) from None
                self._candidate_registrations.pop(candidate.handle.generation, None)
                raise
        self._stage_candidate(candidate, batches=batches)
        with self._lock:
            try:
                debug_view = self._build_candidate_debug_view(activation)
                if debug_view is not None:
                    self._host._bind_candidate_debug_view(candidate, debug_view)
            except BaseException as error:
                self._abort_pre_swap(candidate, error)
        return candidate

    def stages(
        self,
        candidate: WorkerUniverseCandidate,
    ) -> tuple[WorkerArtifactStage, ...]:
        with self._lock:
            return self._stages(candidate)

    def _stages(
        self,
        candidate: WorkerUniverseCandidate,
    ) -> tuple[WorkerArtifactStage, ...]:
        return tuple(
            WorkerArtifactStage(
                entry.logical_name,
                entry.artifact_sha256,
                entry.registration_name,
                "connect",
                stage_worker_module_instruction(
                    entry.artifact_bytes,
                    logical_name=entry.logical_name,
                    artifact_sha256=entry.artifact_sha256,
                    registration_name=entry.registration_name,
                ),
            )
            for entry in self._unstaged_sealed_entries(candidate)
        )

    def _unstaged_sealed_entries(
        self,
        candidate: WorkerUniverseCandidate,
        *,
        profiler: PhaseRecorder | None = None,
    ) -> tuple[_SealedWorkerStageEntry, ...]:
        return self._unstaged_sealed_entries_from_activation(
            self._sealed_activation(candidate, profiler=profiler)
        )

    def _sealed_activation(
        self,
        candidate: WorkerUniverseCandidate,
        *,
        profiler: PhaseRecorder | None = None,
    ) -> _SealedWorkerActivation:
        if not isinstance(candidate, WorkerUniverseCandidate):
            raise TypeError("worker universe candidate is required")
        if self._broken:
            raise ProtocolError("Worker target registry is broken")
        return (
            self._host._require_sealed_activation(candidate)
            if profiler is None
            else profiler.measure(
                "artifact_stage_sealed_validation",
                lambda: self._host._require_sealed_activation(candidate),
                item_count=lambda result: len(result.stage_entries),
            )
        )

    def _unstaged_sealed_entries_from_activation(
        self,
        activation: _SealedWorkerActivation,
    ) -> tuple[_SealedWorkerStageEntry, ...]:
        new_artifact_ids = {
            id(artifact) for artifact in activation.candidate.new_artifacts
        }
        unstaged: list[_SealedWorkerStageEntry] = []
        for entry in activation.stage_entries:
            expected_owner = (
                entry.logical_name.casefold(),
                entry.artifact_sha256,
            )
            owner = self._registrations.get(entry.registration_name)
            if owner is not None:
                if (
                    type(owner) is not WorkerRegistrationRecord
                    or owner.target_incarnation_id != self._target_incarnation_id
                    or (owner.canonical_module, owner.artifact_sha256)
                    != expected_owner
                ):
                    raise ProtocolError(
                        "Worker target registration ownership is invalid"
                    )
                continue
            if id(entry.descriptor) not in new_artifact_ids:
                raise ProtocolError("Worker target registration ownership is missing")
            unstaged.append(entry)
        return tuple(unstaged)

    def _stage_batches(
        self,
        candidate: WorkerUniverseCandidate,
        *,
        profiler: PhaseRecorder | None = None,
    ) -> tuple[WorkerStageBatch, ...]:
        return self._stage_batches_from_activation(
            self._sealed_activation(candidate, profiler=profiler)
        )

    def _stage_batches_from_activation(
        self,
        activation: _SealedWorkerActivation,
    ) -> tuple[WorkerStageBatch, ...]:
        return build_worker_stage_batches(
            tuple(
                WorkerStageEntry(
                    entry.logical_name,
                    entry.artifact_sha256,
                    entry.registration_name,
                    entry.artifact_bytes,
                )
                for entry in self._unstaged_sealed_entries_from_activation(
                    activation,
                )
            )
        )

    def _build_candidate_debug_view(
        self,
        activation: _SealedWorkerActivation,
    ) -> WorkerGenerationDebugView | None:
        modules: list[WorkerModuleDebugView] = []
        for entry, descriptor in zip(
            activation.stage_entries,
            activation.candidate.manifest.modules,
            strict=True,
        ):
            registration = self._registration_view(entry.registration_name)
            snapshot = entry.admitted_snapshot
            visible_snapshot = snapshot.visible_context_snapshot
            source_units = _worker_debug_source_units(snapshot.mapped_source)
            if not source_units:
                return None
            modules.append(
                WorkerModuleDebugView(
                    source_units,
                    descriptor.logical_name.casefold(),
                    snapshot.mapped_source,
                    None if visible_snapshot is None else visible_snapshot.context,
                    descriptor.artifact_sha256,
                    entry.descriptor.source_map_sha256,
                    registration,
                )
            )
        return WorkerGenerationDebugView(
            activation.candidate.handle,
            activation.candidate.manifest,
            tuple(modules),
        )

    def prepare_root(
        self, candidate: WorkerUniverseCandidate, *, transaction_id: UUID,
        profiler: PhaseRecorder | None = None,
    ) -> WorkerPreparedRootReceipt:
        if type(transaction_id) is not UUID:
            raise TypeError("worker root transaction id must be a UUID")
        with self._lock:
            if self._broken:
                raise ProtocolError("Worker target registry is broken")
            if transaction_id in self._prepared_roots:
                raise ProtocolError("Worker root transaction was already used")
            activation = self._sealed_activation(candidate, profiler=profiler)
            try:
                batches = self._stage_batches_from_activation(activation)
            except BaseException as error:
                self._abort_pre_swap(candidate, error)
        staged_item_count = sum(len(batch.entries) for batch in batches)
        stage = lambda: self._stage_candidate(candidate, batches=batches, profiler=profiler)
        if profiler is not None and staged_item_count:
            profiler.measure("artifact_staging", stage, item_count=lambda _result: staged_item_count)
        else:
            stage()
        with self._lock:
            if activation.debug_view is None:
                try:
                    debug_view = self._build_candidate_debug_view(activation)
                    if debug_view is not None:
                        self._host._bind_candidate_debug_view(candidate, debug_view)
                except BaseException as error:
                    self._abort_pre_swap(candidate, error)
        mutation = self.reserve_prepare_root(candidate, transaction_id=transaction_id)
        receipt = self.execute_mutation(mutation)
        if profiler is not None:
            profiler.record_duration("generation_create_wire_probe",
                                     wall_ns=receipt.generation_create_wire_probe_ms * 1_000_000, item_count=1)
        return receipt

    def reserve_prepare_root(
        self, candidate: WorkerUniverseCandidate, *, transaction_id: UUID,
    ) -> PreparedWorkerMutation:
        if type(transaction_id) is not UUID:
            raise TypeError("worker root transaction id must be a UUID")
        with self._lock:
            if transaction_id in self._prepared_roots:
                raise ProtocolError("Worker root transaction was already used")
            self._sealed_activation(candidate)
            previous_root_key = self._host.active_root_key or ""

            def abort(error: BaseException) -> object:
                if _worker_promotion_failure_phase(error) is not None:
                    self._record_uninstantiable_candidate_module(candidate, error)
                    self._abort_pre_swap(candidate, error)
                self._break_pending(candidate)
                raise _promotion_unknown(candidate) from None

            def commit(result: object) -> WorkerPreparedRootReceipt:
                try:
                    receipt = _worker_prepared_root_receipt(result)
                    if (
                        receipt.transaction_id != transaction_id
                        or receipt.generation != candidate.handle.generation
                        or receipt.manifest_sha256 != candidate.manifest.sha256
                        or receipt.candidate_root_key != _generation_root_key(candidate.handle.generation)
                        or receipt.previous_root_key != previous_root_key
                    ):
                        raise ProtocolError("Worker prepared root receipt is stale")
                except BaseException:
                    self._break_pending(candidate)
                    raise _promotion_unknown(candidate) from None
                self._prepared_roots[transaction_id] = (candidate, receipt)
                instantiated = {module.registration_name for module in candidate.manifest.modules}
                self._instantiated_registrations.update(instantiated)
                self._compile_failed_registrations.difference_update(instantiated)
                return receipt

            return self.reserve_mutation(
                prepare_worker_root_instruction(candidate, transaction_id, previous_root_key),
                commit=commit, abort=abort,
            )

    def _prepared_entry(self, prepared: WorkerPreparedRootReceipt) -> WorkerUniverseCandidate:
        if self._broken:
            raise ProtocolError("Worker target registry is broken")
        entry = self._prepared_roots.get(getattr(prepared, "transaction_id", None))
        if entry is None or entry[1] is not prepared:
            raise ProtocolError("Worker prepared root is stale or forged")
        return entry[0]

    def reserve_swap_root(
        self, prepared: WorkerPreparedRootReceipt, *,
        profiler: PhaseRecorder | None = None,
    ) -> PreparedWorkerMutation:
        with self._lock:
            candidate = self._prepared_entry(prepared)

            def abort(error: BaseException) -> object:
                if _worker_root_swap_failure_phase(error) is not None:
                    raise error
                self._break_pending(candidate)
                raise _promotion_unknown(candidate) from None

            def commit(result: object) -> WorkerGenerationHandle:
                try:
                    receipt = _worker_promotion_receipt(result)
                    if (
                        receipt.transaction_id != prepared.transaction_id
                        or receipt.generation != prepared.generation
                        or receipt.manifest_sha256 != prepared.manifest_sha256
                        or receipt.root_key != prepared.candidate_root_key
                        or receipt.previous_root_key != prepared.previous_root_key
                        or receipt.generation_create_wire_probe_ms != prepared.generation_create_wire_probe_ms
                    ):
                        raise ProtocolError("Worker root swap receipt is stale")
                    confirmation = self._host.confirm(candidate, receipt)
                except BaseException:
                    self._break_pending(candidate)
                    raise _promotion_unknown(candidate) from None
                del self._prepared_roots[prepared.transaction_id]
                self._candidate_registrations.pop(candidate.handle.generation, None)
                if profiler is not None:
                    profiler.record_duration("root_swap", wall_ns=receipt.root_swap_ms * 1_000_000, item_count=1)
                return confirmation.handle

            return self.reserve_mutation(swap_worker_root_instruction(prepared), commit=commit, abort=abort)

    def swap_root(
        self, prepared: WorkerPreparedRootReceipt, *, profiler: PhaseRecorder | None = None,
    ) -> WorkerGenerationHandle:
        return self.execute_mutation(self.reserve_swap_root(prepared, profiler=profiler))

    def reserve_discard_root(self, prepared: WorkerPreparedRootReceipt) -> PreparedWorkerMutation:
        with self._lock:
            candidate = self._prepared_entry(prepared)

            def abort(error: BaseException) -> object:
                self._break_pending(candidate)
                raise _promotion_unknown(candidate) from None

            def commit(result: object) -> None:
                try:
                    _worker_root_discard_receipt(result, prepared)
                    self._host.discard(candidate)
                except BaseException as error:
                    abort(error)
                del self._prepared_roots[prepared.transaction_id]
                self._candidate_registrations.pop(candidate.handle.generation, None)

            return self.reserve_mutation(discard_worker_root_instruction(prepared), commit=commit, abort=abort)

    def discard_root(self, prepared: WorkerPreparedRootReceipt) -> None:
        self.execute_mutation(self.reserve_discard_root(prepared))

    def quarantine_root(self, prepared: WorkerPreparedRootReceipt) -> None:
        """Retain both possible roots after another participant becomes unknown."""
        with self._lock:
            if self._broken:
                return
            entry = self._prepared_roots.get(
                getattr(prepared, "transaction_id", None)
            )
            if entry is None or entry[1] is not prepared:
                raise ProtocolError("Worker prepared root is stale or forged")
            candidate, _exact_receipt = entry
            self._break_pending(candidate)

    def promote(
        self,
        candidate: WorkerUniverseCandidate,
        *,
        profiler: PhaseRecorder | None = None,
    ) -> WorkerGenerationHandle:
        prepared = self.prepare_root(
            candidate,
            transaction_id=uuid4(),
            profiler=profiler,
        )
        try:
            return self.swap_root(prepared, profiler=profiler)
        except BaseException as error:
            if _worker_root_swap_failure_phase(error) is not None:
                self.discard_root(prepared)
            raise

    def privacy_registration_snapshot(self) -> tuple[str, ...]:
        """Return every exact registration that can still own a Worker object."""

        with self._lock:
            registrations = tuple(
                sorted(
                    self._registrations.keys() - self._compile_failed_registrations,
                    key=str.casefold,
                )
            )
            if (
                self._broken
                or self._host.state is not WorkerUniverseState.READY
                or self._host.active_handle is None
                or not registrations
                or len({name.casefold() for name in registrations})
                != len(registrations)
            ):
                raise ProtocolError(
                    "Worker privacy registration snapshot is unavailable"
                )
            for name in registrations:
                owner = self._registrations.get(name)
                if (
                    not _bsl_identifier(name)
                    or type(owner) is not WorkerRegistrationRecord
                    or owner.registration_name != name
                    or owner.target_incarnation_id != self._target_incarnation_id
                ):
                    raise ProtocolError(
                        "Worker privacy registration snapshot is invalid"
                    )
            return registrations

    def _record_uninstantiable_candidate_module(
        self,
        candidate: WorkerUniverseCandidate,
        error: BaseException,
    ) -> None:
        """Exclude a proven compile-invalid registration from type probes."""
        failure = worker_artifact_stage_failure(error)
        if (
            failure is None
            or failure.phase != "create"
            or not parse_platform_diagnostic(
                _worker_reload_platform_message(error)
            ).has_compilation_marker
        ):
            return
        matches = tuple(
            module.registration_name for module in candidate.manifest.modules
            if module.artifact_sha256 == failure.artifact_sha256
        )
        if len(matches) == 1 and matches[0] not in self._instantiated_registrations:
            self._compile_failed_registrations.add(matches[0])

    def _registration_view(self, registration_name: str) -> WorkerRegistrationRecord:
        with self._lock:
            if not _bsl_identifier(registration_name):
                raise ProtocolError("Worker registration identity is invalid")
            record = self._registrations.get(registration_name)
            if (
                type(record) is not WorkerRegistrationRecord
                or record.target_incarnation_id != self._target_incarnation_id
                or record.registration_name != registration_name
                or record.receipt_admission is not _REGISTRATION_RECEIPT_ADMISSION
            ):
                raise ProtocolError("Worker registration identity is unavailable")
            return record

    def release(self, handle: WorkerGenerationHandle) -> None:
        with self._lock:
            if self._broken:
                raise ProtocolError("Worker target registry is broken")
            self._host.release_generation(handle)

    def release_pin(self, pin: OperationGenerationPin) -> None:
        with self._lock:
            if self._broken:
                raise ProtocolError("Worker target registry is broken")
        self._host.release_pin(pin)

    def preview_release(
        self,
        release: WorkerGenerationHandle | OperationGenerationPin,
    ) -> WorkerLifecycleReleasePlan:
        with self._lock:
            if self._broken:
                raise ProtocolError("Worker target registry is broken")
            return self._host._preview_release(release)

    def commit_release(self, plan: WorkerLifecycleReleasePlan) -> tuple[str, ...]:
        with self._lock:
            if self._broken:
                raise ProtocolError("Worker target registry is broken")
            return self._host._commit_release(plan)

    def teardown(self) -> None:
        with self._lock:
            self._host.teardown()
            # RuntimeSession owns target-process termination; never dispatch an
            # unsupported cleanup instruction while forgetting the host ledger.
            self._registrations.clear()
            self._candidate_registrations.clear()
            self._instantiated_registrations.clear()
            self._compile_failed_registrations.clear()
            self._prepared_roots.clear()
            self._mutations.clear()
            self._claimed_mutations.clear()
            self._orphan_urls.clear()
            self._storage_session_id = None

    def abandon_target(self) -> None:
        """Forget target registrations when the target cannot execute cleanup.

        The caller owns terminating the target transport/process.  No target
        instruction is attempted here: host leases and handles are closed, all
        target identities are forgotten, and this registry is permanently
        unusable.
        """
        with self._lock:
            self._host.teardown()
            self._registrations.clear()
            self._candidate_registrations.clear()
            self._instantiated_registrations.clear()
            self._compile_failed_registrations.clear()
            self._prepared_roots.clear()
            self._mutations.clear()
            self._claimed_mutations.clear()
            self._orphan_urls.clear()
            self._storage_session_id = None
            self._broken = True

    def _stage_candidate(
        self, candidate: WorkerUniverseCandidate, *, batches: tuple[WorkerStageBatch, ...],
        profiler: PhaseRecorder | None = None,
    ) -> None:
        if not batches:
            return
        possible_registrations = {entry.registration_name for batch in batches for entry in batch.entries}
        transaction_id = uuid4()
        for batch in batches:
            def stage_batch() -> object:
                build = lambda: stage_worker_batch_instruction(
                    batch, batch_count=len(batches), transaction_id=transaction_id,
                )
                try:
                    instruction = build() if profiler is None else profiler.measure(
                        "artifact_stage_base64", build, item_count=lambda _result: len(batch.entries),
                    )
                except BaseException as error:
                    with self._lock:
                        self._abort_pre_swap(candidate, error)
                mutation = self.reserve_stage_batch(
                    candidate, batch, instruction=instruction, batch_count=len(batches),
                    transaction_id=transaction_id, possible_registrations=possible_registrations,
                )
                execute = lambda: self.execute_mutation(mutation)
                return execute() if profiler is None else profiler.measure(
                    "artifact_stage_executor", execute, item_count=lambda _result: len(batch.entries),
                )
            if profiler is None:
                stage_batch()
            else:
                profiler.measure("artifact_stage_batch", stage_batch, item_count=lambda _result: len(batch.entries))

    def reserve_stage_batch(
        self, candidate: WorkerUniverseCandidate, batch: WorkerStageBatch, *,
        instruction: str, batch_count: int, transaction_id: UUID,
        possible_registrations: set[str],
    ) -> PreparedWorkerMutation:
        with self._lock:
            generation = candidate.handle.generation

            def abort(error: BaseException) -> object:
                self._candidate_registrations.setdefault(generation, set()).update(possible_registrations)
                self._break_pending(candidate)
                raise _promotion_unknown(candidate) from None

            def commit(result: object) -> None:
                try:
                    outcome = parse_worker_stage_batch_outcome(
                        result, batch, batch_count=batch_count, transaction_id=transaction_id,
                    )
                    self._record_stage_entries(
                        batch, outcome, self._candidate_registrations.setdefault(generation, set()),
                    )
                except BaseException as error:
                    abort(error)
                if outcome.failure is not None and outcome.failure.orphan_url is not None:
                    self._orphan_urls.add(outcome.failure.orphan_url)
                if outcome.failure is None:
                    return
                if outcome.failure.outcome == "known_pre_swap":
                    self._abort_pre_swap(candidate, self._stage_failure_error(candidate, batch, outcome))
                abort(ProtocolError("Worker stage outcome is unknown"))

            return self.reserve_mutation(instruction, commit=commit, abort=abort)

    def _stage_failure_error(
        self,
        candidate: WorkerUniverseCandidate,
        batch: WorkerStageBatch,
        outcome: WorkerStageBatchOutcome,
    ) -> BslExecutionError:
        failure = outcome.failure
        if failure is None:
            raise ProtocolError("Worker stage failure is missing")
        entry = batch.entries[failure.item_index]
        artifacts = tuple(
            artifact
            for artifact in self._host._candidate_diagnostics(candidate)
            if artifact.registration_name == entry.registration_name
            and artifact.artifact_sha256 == entry.artifact_sha256
        )
        diagnostic = remap_worker_stage_diagnostic(
            parse_platform_diagnostic(failure.diagnostic),
            artifact_sha256=entry.artifact_sha256,
            phase=failure.phase,
            candidate_manifest_sha256=candidate.manifest.sha256,
            candidate_artifacts=artifacts,
        )
        return BslExecutionError(failure.diagnostic, diagnostic=diagnostic)

    def _record_stage_entries(
        self,
        batch: WorkerStageBatch,
        outcome: WorkerStageBatchOutcome,
        owned: set[str],
    ) -> None:
        for entry, receipt in zip(
            batch.entries,
            outcome.connected,
            strict=False,
        ):
            self._record_stage_entry(
                entry,
                receipt,
                transaction_id=outcome.transaction_id,
                batch_digest=outcome.batch_digest,
            )
            owned.add(entry.registration_name)

    def _record_stage_entry(
        self,
        entry: WorkerStageEntry,
        receipt: WorkerStageRegistrationReceipt,
        *,
        transaction_id: UUID,
        batch_digest: str,
    ) -> None:
        storage_session_id = _temp_storage_session_id(receipt.temp_storage_url)
        if storage_session_id is None:
            raise ProtocolError("Worker registration URL is invalid")
        if self._storage_session_id is None:
            self._storage_session_id = storage_session_id
        elif self._storage_session_id != storage_session_id:
            raise ProtocolError("Worker registration storage session is inconsistent")
        record = WorkerRegistrationRecord(
            self._target_incarnation_id,
            entry.logical_name.casefold(),
            entry.artifact_sha256,
            entry.registration_name,
            receipt.temp_storage_url,
            _WORKER_LOCATOR_PROFILE_ID,
            transaction_id,
            batch_digest,
            self._platform_build,
            _REGISTRATION_RECEIPT_ADMISSION,
        )
        previous = self._registrations.get(entry.registration_name)
        if previous is not None and previous != record:
            raise ProtocolError("Worker target registration ownership is invalid")
        self._registrations[entry.registration_name] = record

    def _abort_pre_swap(
        self,
        candidate: WorkerUniverseCandidate,
        error: BaseException,
    ) -> None:
        # Successful staging remains reusable and privacy-visible for the
        # target session even though this generation candidate is discarded.
        self._candidate_registrations.pop(candidate.handle.generation, None)
        try:
            self._host.discard(candidate)
        except BaseException:
            self._break_pending(candidate)
            raise _promotion_unknown(candidate) from None
        raise error

    def _break_pending(self, candidate: WorkerUniverseCandidate) -> None:
        self._broken = True
        try:
            self._host.mark_broken(candidate)
        except ProtocolError:
            if self._host.state is not WorkerUniverseState.BROKEN:
                self._host.mark_broken()

def _worker_prepared_root_receipt(value: object) -> WorkerPreparedRootReceipt:
    if type(value) is WorkerPreparedRootReceipt:
        return value
    if type(value) is not str:
        raise ProtocolError("Worker prepared root receipt is invalid")
    fields = value.split("|")
    if (
        len(fields) != 7
        or any(not field for field in fields)
        or fields[0] != _PREPARED_ROOT_RECEIPT_PREFIX
        or fullmatch(r"(?:0|[1-9][0-9]*)", fields[2]) is None
        or fullmatch(r"(?:0|[1-9][0-9]*)", fields[6]) is None
    ):
        raise ProtocolError("Worker prepared root receipt is invalid")
    try:
        transaction_id = UUID(fields[1])
        if str(transaction_id) != fields[1]:
            raise ValueError
        return WorkerPreparedRootReceipt(
            transaction_id,
            int(fields[2]),
            fields[3],
            fields[4],
            _unwire_root_key(fields[5]),
            int(fields[6]),
        )
    except (TypeError, ValueError):
        raise ProtocolError("Worker prepared root receipt is invalid") from None


def _worker_promotion_receipt(value: object) -> WorkerPromotionReceipt:
    if isinstance(value, WorkerPromotionReceipt):
        return value
    if type(value) is not str:
        raise ProtocolError("Worker promotion receipt is invalid")
    fields = value.split("|")
    if (
        len(fields) != 9
        or any(not field for field in fields)
        or fields[0] != _ROOT_SWAP_RECEIPT_PREFIX
        or fields[6] != "1"
        or fullmatch(r"(?:0|[1-9][0-9]*)", fields[2]) is None
        or fullmatch(r"(?:0|[1-9][0-9]*)", fields[7]) is None
        or fullmatch(r"(?:0|[1-9][0-9]*)", fields[8]) is None
    ):
        raise ProtocolError("Worker promotion receipt is invalid")
    try:
        transaction_id = UUID(fields[1])
        if str(transaction_id) != fields[1]:
            raise ValueError
        return WorkerPromotionReceipt(
            transaction_id,
            int(fields[2]),
            fields[3],
            fields[4],
            _unwire_root_key(fields[5]),
            True,
            int(fields[7]),
            int(fields[8]),
        )
    except (TypeError, ValueError):
        raise ProtocolError("Worker promotion receipt is invalid") from None


def _worker_root_discard_receipt(
    value: object,
    prepared: WorkerPreparedRootReceipt,
) -> None:
    expected = "|".join(
        (
            _ROOT_DISCARD_RECEIPT_PREFIX,
            str(prepared.transaction_id),
            str(prepared.generation),
            prepared.manifest_sha256,
            prepared.candidate_root_key,
        )
    )
    if type(value) is not str or not compare_digest(value, expected):
        raise ProtocolError("Worker root discard receipt is invalid")


def _worker_promotion_failure_phase(error: BaseException) -> str | None:
    prefix = "onec-worker-root-prepare-stage="
    message = str(error)
    marker = message.find(prefix)
    if marker < 0:
        return None
    phase = message[marker + len(prefix) :].splitlines()[0].rstrip("\r").strip()
    return phase if phase in {"create", "wire", "probe"} else None


def _worker_root_swap_failure_phase(error: BaseException) -> str | None:
    prefix = "onec-worker-root-swap-stage="
    message = str(error)
    marker = message.find(prefix)
    if marker < 0:
        return None
    phase = message[marker + len(prefix) :].splitlines()[0].rstrip("\r").strip()
    return phase if phase == "guard" else None


def _promotion_unknown(
    candidate: WorkerUniverseCandidate,
) -> WorkerPromotionOutcomeUnknown:
    return WorkerPromotionOutcomeUnknown(
        candidate.handle.generation,
        candidate.manifest.sha256,
    )


def registration_name(logical_name: str, artifact_sha256: str) -> str:
    if not _bsl_identifier(logical_name) or not _sha256_value(artifact_sha256):
        raise ValueError("worker registration identity is invalid")
    logical_digest = sha256(logical_name.casefold().encode("utf-8")).hexdigest()[:8]
    value = f"OnecRuntime_{logical_digest}_{artifact_sha256[:16]}"
    if not _bsl_identifier(value) or len(value) > 80:
        raise ValueError("worker registration identity is invalid")
    return value


def _build_worker_manifest(
    generation: int,
    artifacts: tuple[WorkerModuleArtifact, ...],
) -> tuple[tuple[_ValidatedWorkerModuleArtifact, ...], WorkerUniverseManifest]:
    if type(artifacts) is not tuple or not artifacts:
        raise ProtocolError("Worker universe requires a nonempty artifact tuple")
    try:
        views = tuple(_validated_worker_module_artifact(artifact) for artifact in artifacts)
        ordered_views = tuple(
            sorted(
                views,
                key=lambda view: (
                    view.descriptor.logical_name.casefold(),
                    view.descriptor.artifact_sha256,
                    view.descriptor.revision,
                ),
            )
        )
        ordered = tuple(view.descriptor for view in ordered_views)
        names = tuple(item.logical_name.casefold() for item in ordered)
        if len(names) != len(set(names)):
            raise ProtocolError("Worker universe has duplicate logical modules")
        modules = tuple(
            ManifestModule(
                artifact.logical_name,
                artifact.revision,
                artifact.artifact_sha256,
                registration_name(artifact.logical_name, artifact.artifact_sha256),
            )
            for artifact in ordered
        )
        artifact_by_name = {
            artifact.logical_name.casefold(): artifact for artifact in ordered
        }
        wiring: list[DependencyTarget] = []
        seen_bindings: set[tuple[str, str]] = set()
        for artifact in ordered:
            for binding in artifact.dependency_bindings:
                binding_key = (
                    artifact.logical_name.casefold(),
                    binding.export_variable.casefold(),
                )
                if binding_key in seen_bindings:
                    raise ProtocolError("Worker universe dependency binding is ambiguous")
                seen_bindings.add(binding_key)
                overloaded = artifact_by_name.get(binding.target_module.casefold())
                target_kind: Literal["original", "overloaded"]
                if overloaded is None:
                    target_kind = "original"
                    target_name = binding.target_module
                else:
                    target_kind = "overloaded"
                    target_name = overloaded.logical_name
                wiring.append(
                    DependencyTarget(
                        artifact.logical_name,
                        binding.export_variable,
                        target_kind,
                        target_name,
                    )
                )
        wiring_tuple = tuple(sorted(wiring, key=_dependency_target_sort_key))
        exports = tuple(
            sorted(
                (item for artifact in ordered for item in artifact.exports),
                key=_worker_export_sort_key,
            )
        )
        validate_worker_export_catalog(exports, require_nonempty=True)
        digest_value = _worker_manifest_sha256(
            generation,
            modules,
            wiring_tuple,
            exports,
        )
        return ordered_views, WorkerUniverseManifest(
            generation,
            modules,
            wiring_tuple,
            exports,
            digest_value,
        )
    except ProtocolError:
        raise
    except Exception:
        raise ProtocolError("Worker universe manifest admission is invalid") from None


def _worker_manifest_sha256(
    generation: int,
    modules: tuple[ManifestModule, ...],
    wiring: tuple[DependencyTarget, ...],
    exports: tuple[WorkerExport, ...],
) -> str:
    manifest = {
        "schema": "onec-worker-universe-manifest-v1",
        "generation": generation,
        "modules": [
            {
                "logical_name": item.logical_name.casefold(),
                "revision": item.revision,
                "artifact_sha256": item.artifact_sha256,
                "registration_name": item.registration_name,
            }
            for item in modules
        ],
        "wiring": [
            {
                "source_module": item.source_module.casefold(),
                "export_variable": item.export_variable.casefold(),
                "target_kind": item.target_kind,
                "target_module": item.target_module.casefold(),
            }
            for item in wiring
        ],
        "exports": [
            {
                "public_path": item.public_path.casefold(),
                "method": item.method.casefold(),
                "receiver_module": (
                    None
                    if item.receiver_module is None
                    else item.receiver_module.casefold()
                ),
            }
            for item in exports
        ],
    }
    payload = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def _manifest_module_sort_key(item: ManifestModule) -> tuple[str, str, int]:
    return item.logical_name.casefold(), item.artifact_sha256, item.revision


def _dependency_target_sort_key(
    item: DependencyTarget,
) -> tuple[str, str, str, str]:
    return (
        item.source_module.casefold(),
        item.export_variable.casefold(),
        item.target_kind,
        item.target_module.casefold(),
    )


def _worker_export_sort_key(item: WorkerExport) -> tuple[str, str, str]:
    return (
        item.public_path.casefold(),
        item.method.casefold(),
        "" if item.receiver_module is None else item.receiver_module.casefold(),
    )


def _effective_export_catalog_matches(
    manifest: WorkerUniverseManifest,
    catalog: tuple[WorkerExport, ...],
) -> bool:
    if type(catalog) is not tuple:
        return False
    try:
        admitted = validate_worker_export_catalog(catalog, require_nonempty=True)
    except (ProtocolError, ValueError):
        return False
    if admitted != catalog:
        return False
    manifest_receivers = {
        (export.receiver_module.casefold(), export.method.casefold())
        for export in manifest.exports
        if export.receiver_module is not None
    }
    catalog_receivers = {
        (export.receiver_module.casefold(), export.method.casefold())
        for export in catalog
        if export.receiver_module is not None
    }
    if catalog_receivers != manifest_receivers:
        return False
    return all(
        export.receiver_module is not None
        and _bsl_identifier(export.receiver_module)
        and _bsl_identifier(export.method)
        and (
            export.receiver_module.casefold(),
            export.method.casefold(),
        )
        in manifest_receivers
        for export in catalog
    )


def _adjust_manifest_registrations(
    refcounts: dict[str, int],
    manifest: WorkerUniverseManifest,
    delta: Literal[-1, 1],
) -> None:
    for module in manifest.modules:
        current = refcounts.get(module.registration_name, 0)
        updated = current + delta
        if updated < 0:
            raise ProtocolError("Worker registration refcount would become negative")
        refcounts[module.registration_name] = updated


def _remove_zero_registration_refcounts(
    refcounts: dict[str, int],
    registration_artifacts: dict[str, tuple[str, str]],
    quarantine_holds: set[str],
) -> tuple[str, ...]:
    released = tuple(
        sorted(
            (
                name
                for name, count in refcounts.items()
                if count == 0 and name not in quarantine_holds
            ),
            key=str.casefold,
        )
    )
    for name in released:
        del refcounts[name]
        registration_artifacts.pop(name, None)
    return released


def _require_registration_ownership(
    manifest: WorkerUniverseManifest,
    registration_artifacts: dict[str, tuple[str, str]],
) -> None:
    for module in manifest.modules:
        owner = registration_artifacts.get(module.registration_name)
        expected = (module.logical_name.casefold(), module.artifact_sha256)
        if owner is not None and owner != expected:
            raise ProtocolError("Worker registration identity collision")


def _fence_source(value: GenerationFence) -> Callable[[], int]:
    if callable(value):
        return value
    _validate_positive_generation(value)
    return lambda: value


def _validate_positive_generation(value: object) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError("worker generation fence must be positive")


def _new_lease_id(leases: dict[str, _OperationLease]) -> str:
    while True:
        value = token_urlsafe(32)
        if value not in leases:
            return value


def _generation_root_key(generation: int) -> str:
    _validate_positive_generation(generation)
    return f"generation-{generation}"


def _bsl_identifier(value: object) -> bool:
    return (
        isinstance(value, str)
        and fullmatch(r"[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё_0-9]*", value) is not None
        and value.upper() not in _BSL_KEYWORDS
    )


def _valid_dependency_target(value: object) -> bool:
    return (
        isinstance(value, DependencyTarget)
        and _bsl_identifier(value.source_module)
        and _bsl_identifier(value.export_variable)
        and value.target_kind in ("original", "overloaded")
        and _bsl_identifier(value.target_module)
        and not (
            value.target_kind == "original"
            and value.target_module.casefold()
            in _FIXED_UNQUALIFIED_WORKER_IDENTIFIERS
        )
    )
