from __future__ import annotations

from base64 import b64encode
from bisect import bisect_right
from dataclasses import dataclass, replace
from enum import Enum
from hashlib import sha256
from hmac import compare_digest, digest
import json
from pathlib import Path
from re import fullmatch, search
from secrets import token_bytes
from types import MappingProxyType
from typing import Literal, cast
from weakref import ReferenceType, WeakKeyDictionary, ref

from onec_runtime.config import RuntimeConfig
from onec_runtime.errors import (
    BslExecutionError,
    ProtocolError,
)
from onec_runtime.experiment import bsl_string_literal
from onec_runtime.worker_stage_protocol import (
    _safe_identifier as _worker_stage_safe_identifier,
)
from onec_runtime.bsl import (
    DiagnosticStage,
    MappedSource,
    SemanticNotebookLowerer,
    SourceArtifactKind,
    SourceUnitRef,
    VisibleSourceContext,
    parse_platform_diagnostic,
    remap_platform_diagnostic,
)
from onec_runtime.bsl.diagnostics import (
    WorkerDiagnosticArtifact,
    remap_worker_stage_diagnostic,
)
from onec_runtime.bsl.parser_target import PythonParserTarget
from onec_runtime.bsl.source_maps import (
    SourceArtifactRef,
    SourceMap,
    SourceMapSegment,
    SourceSpan,
    SourceUnitRef,
)
from onec_runtime.bsl.module_universe import (
    WorkerSemanticAdmission,
    _validate_worker_semantic_admission,
)
from onec_runtime.bsl.semantic_lowering import WorkerExport
from onec_runtime.bsl.worker_reload_source_map import (
    CompactMappedWorkerSource,
    CompactReloadSourceMap,
    snapshot_compact_mapped_worker_source,
)


@dataclass(frozen=True, slots=True)
class WorkerSourceProvenance:
    source_sha256: str
    source_map_sha256: str
    worker_generation: int | None = None
    worker_manifest_sha256: str | None = None

    def __post_init__(self) -> None:
        for name in ("source_sha256", "source_map_sha256"):
            if fullmatch(r"[0-9a-f]{64}", getattr(self, name)) is None:
                raise ValueError(f"{name} must be a lowercase sha256")
        if (
            self.worker_generation is not None
            and (
                type(self.worker_generation) is not int
                or self.worker_generation < 0
            )
        ):
            raise ValueError("worker_generation must be a nonnegative integer")
        if (
            self.worker_manifest_sha256 is not None
            and fullmatch(r"[0-9a-f]{64}", self.worker_manifest_sha256) is None
        ):
            raise ValueError("worker_manifest_sha256 must be a lowercase sha256")


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class WorkerArtifact:
    logical_name: str
    source_sha256: str
    artifact_sha256: str
    exports: tuple[WorkerExport, ...] = ()
    source_provenance: WorkerSourceProvenance | None = None

    @property
    def source_map_sha256(self) -> str | None:
        return (
            None
            if self.source_provenance is None
            else self.source_provenance.source_map_sha256
        )

    @property
    def _admission(self) -> _WorkerArtifactAdmission | None:
        capability = _worker_artifact_capability(self, required=False)
        return None if capability is None else capability.admission


_WORKER_ARTIFACT_STAGE_INSTRUCTIONS: WeakKeyDictionary[
    object,
    str,
] = WeakKeyDictionary()


@dataclass(frozen=True, slots=True, repr=False, eq=False, weakref_slot=True, init=False)
class WorkerArtifactStage:
    """One content-addressed external-processing connect operation."""

    logical_name: str
    artifact_sha256: str
    registration_name: str
    phase: Literal["connect"]

    def __init__(
        self,
        logical_name: str,
        artifact_sha256: str,
        registration_name: str,
        phase: Literal["connect"],
        instruction: str,
    ) -> None:
        if (
            not _worker_safe_identifier(logical_name)
            or fullmatch(r"[0-9a-f]{64}", artifact_sha256) is None
            or not _worker_safe_identifier(registration_name)
            or phase != "connect"
            or not isinstance(instruction, str)
            or not instruction
        ):
            raise ValueError("worker artifact stage is invalid")
        object.__setattr__(self, "logical_name", logical_name)
        object.__setattr__(self, "artifact_sha256", artifact_sha256)
        object.__setattr__(self, "registration_name", registration_name)
        object.__setattr__(self, "phase", phase)
        _WORKER_ARTIFACT_STAGE_INSTRUCTIONS[self] = instruction

    @property
    def instruction(self) -> str:
        instruction = _WORKER_ARTIFACT_STAGE_INSTRUCTIONS.get(self)
        if instruction is None:
            raise ProtocolError("Worker artifact stage capability is unavailable")
        return instruction

    def __deepcopy__(self, memo: dict[int, object]) -> WorkerArtifactStage:
        del memo
        return self

    def __repr__(self) -> str:
        return (
            "WorkerArtifactStage("
            f"logical_name={self.logical_name!r}, "
            f"artifact_sha256={self.artifact_sha256!r}, "
            f"registration_name={self.registration_name!r}, "
            f"phase={self.phase!r}, instruction=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class WorkerArtifactStageFailure:
    artifact_sha256: str
    phase: Literal["decode", "upload", "connect", "create"]

    def __post_init__(self) -> None:
        if (
            fullmatch(r"[0-9a-f]{64}", self.artifact_sha256) is None
            or self.phase not in {"decode", "upload", "connect", "create"}
        ):
            raise ValueError("worker artifact stage failure identity is invalid")


@dataclass(frozen=True, slots=True, repr=False)
class _AdmittedWorkerBinary:
    source_bytes: bytes
    artifact_bytes: bytes
    source_sha256: str
    artifact_sha256: str

    def __repr__(self) -> str:
        return "<redacted admitted worker binary>"

    def __deepcopy__(self, memo: dict[int, object]) -> _AdmittedWorkerBinary:
        del memo
        return self


@dataclass(frozen=True, slots=True, repr=False)
class _AdmittedWorkerSnapshot:
    binary: _AdmittedWorkerBinary
    proof: bytes
    mapped_source: MappedSource
    mapped_guard: tuple[int, ...]
    source_map_sha256: str
    worker_generation: int | None
    worker_manifest_sha256: str | None
    catalog: tuple[WorkerExport, ...]
    visible_context_snapshot: _WorkerVisibleContextSnapshot | None
    visible_context_fingerprint: str
    visible_context_guard: tuple[object, ...]
    source_path: Path
    artifact_path: Path
    expected_version: str | None
    expected_value: int | None

    @property
    def source_bytes(self) -> bytes:
        return self.binary.source_bytes

    @property
    def artifact_bytes(self) -> bytes:
        return self.binary.artifact_bytes

    @property
    def source_sha256(self) -> str:
        return self.binary.source_sha256

    @property
    def artifact_sha256(self) -> str:
        return self.binary.artifact_sha256

    def __repr__(self) -> str:
        return "<redacted admitted worker snapshot>"

    def __deepcopy__(self, memo: dict[int, object]) -> _AdmittedWorkerSnapshot:
        del memo
        return self


class _WorkerArtifactAdmission:
    """Weak owner capability over one immutable admitted snapshot."""

    __slots__ = ("__owner", "__snapshot")

    def __init__(
        self,
        owner: WorkerArtifact,
        snapshot: _AdmittedWorkerSnapshot,
    ) -> None:
        object.__setattr__(self, "_WorkerArtifactAdmission__owner", ref(owner))
        object.__setattr__(self, "_WorkerArtifactAdmission__snapshot", snapshot)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("worker artifact admission snapshots are immutable")

    def __repr__(self) -> str:
        return "<redacted worker artifact admission>"

    def __deepcopy__(self, memo: dict[int, object]) -> str:
        return "<redacted worker artifact admission>"

    def snapshot(self, owner: WorkerArtifact) -> _AdmittedWorkerSnapshot:
        owner_ref: ReferenceType[WorkerArtifact] = self.__owner
        if owner_ref() is not owner:
            raise ProtocolError("Runtime API requires a proven worker artifact")
        return self.__snapshot

    def contents(
        self,
        owner: WorkerArtifact,
    ) -> tuple[bytes, bytes, bytes, MappedSource]:
        snapshot = self.snapshot(owner)
        return (
            snapshot.source_bytes,
            snapshot.artifact_bytes,
            snapshot.proof,
            _snapshot_mapped_source(snapshot.mapped_source)[0],
        )

    def diagnostic_context(
        self,
        owner: WorkerArtifact,
    ) -> VisibleSourceContext | None:
        snapshot = _clone_visible_context_snapshot(
            self.snapshot(owner).visible_context_snapshot
        )
        return None if snapshot is None else snapshot.context

    def context_snapshot(
        self,
        owner: WorkerArtifact,
    ) -> _WorkerVisibleContextSnapshot | None:
        return _clone_visible_context_snapshot(
            self.snapshot(owner).visible_context_snapshot
        )

    def catalog(self, owner: WorkerArtifact) -> tuple[WorkerExport, ...]:
        catalog = self.snapshot(owner).catalog
        if catalog != owner.exports:
            raise ProtocolError("Runtime API requires a proven worker artifact")
        return catalog


class _ImmutableLineIndex:
    __slots__ = ("_length", "_line_starts")

    def __init__(self, length: int, line_starts: tuple[int, ...]) -> None:
        if (
            type(length) is not int
            or length < 0
            or not line_starts
            or line_starts[0] != 0
            or any(type(start) is not int for start in line_starts)
            or any(
                current >= following
                for current, following in zip(line_starts, line_starts[1:])
            )
            or line_starts[-1] > length
        ):
            raise ProtocolError("Worker visible source line index is invalid")
        object.__setattr__(self, "_length", length)
        object.__setattr__(self, "_line_starts", line_starts)

    def offset_to_line_column(self, offset: int) -> tuple[int, int]:
        if type(offset) is not int or not 0 <= offset <= self._length:
            raise ValueError("offset must be within the source")
        line_index = bisect_right(self._line_starts, offset) - 1
        return line_index + 1, offset - self._line_starts[line_index] + 1

    def line_range(self, line: int) -> SourceSpan:
        if type(line) is not int or not 1 <= line <= len(self._line_starts):
            raise ValueError("line must be within the source")
        start = self._line_starts[line - 1]
        end = (
            self._line_starts[line]
            if line < len(self._line_starts)
            else self._length
        )
        return SourceSpan(start, end)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Worker visible line indexes are immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("Worker visible line indexes are immutable")

    def __repr__(self) -> str:
        return "<redacted worker visible line index>"

    def __deepcopy__(self, memo: dict[int, object]) -> _ImmutableLineIndex:
        return self


class _ImmutableVisibleSourceContext(VisibleSourceContext):
    __slots__ = ()

    def __init__(
        self,
        indices: dict[
            tuple[object, str, int],
            tuple[str, _ImmutableLineIndex],
        ],
    ) -> None:
        object.__setattr__(self, "_indices", MappingProxyType(dict(indices)))

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Worker visible source contexts are immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("Worker visible source contexts are immutable")

    def __deepcopy__(
        self,
        memo: dict[int, object],
    ) -> _ImmutableVisibleSourceContext:
        return self


class _WorkerVisibleContextSnapshot:
    __slots__ = ("context", "fingerprint")

    def __init__(
        self,
        context: _ImmutableVisibleSourceContext,
        fingerprint: str,
    ) -> None:
        object.__setattr__(self, "context", context)
        object.__setattr__(self, "fingerprint", fingerprint)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Worker visible context snapshots are immutable")

    def __repr__(self) -> str:
        return "<redacted worker visible context snapshot>"

    def __deepcopy__(
        self,
        memo: dict[int, object],
    ) -> _WorkerVisibleContextSnapshot:
        return self


def _clone_visible_context_snapshot(
    snapshot: _WorkerVisibleContextSnapshot | None,
) -> _WorkerVisibleContextSnapshot | None:
    if snapshot is None:
        return None
    try:
        source_indices = snapshot.context._indices  # type: ignore[attr-defined]
        indices: dict[
            tuple[object, str, int],
            tuple[str, _ImmutableLineIndex],
        ] = {}
        for key, (source_hash, line_index) in source_indices.items():
            indices[key] = (
                source_hash,
                _ImmutableLineIndex(
                    line_index._length,  # type: ignore[attr-defined]
                    line_index._line_starts,  # type: ignore[attr-defined]
                ),
            )
        return _WorkerVisibleContextSnapshot(
            _ImmutableVisibleSourceContext(indices),
            snapshot.fingerprint,
        )
    except Exception:
        raise ProtocolError(
            "Worker visible source context fingerprint does not match"
        ) from None


class _WorkerArtifactCapability:
    __slots__ = (
        "artifact_path",
        "source_path",
        "expected_version",
        "expected_value",
        "admission",
    )

    def __init__(
        self,
        *,
        artifact_path: Path | None,
        source_path: Path | None,
        expected_version: str | None,
        expected_value: int | None,
        admission: _WorkerArtifactAdmission | None,
    ) -> None:
        object.__setattr__(self, "artifact_path", artifact_path)
        object.__setattr__(self, "source_path", source_path)
        object.__setattr__(self, "expected_version", expected_version)
        object.__setattr__(self, "expected_value", expected_value)
        object.__setattr__(self, "admission", admission)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Worker artifact capabilities are immutable")

    def __repr__(self) -> str:
        return "<redacted worker artifact capability>"

    def __deepcopy__(self, memo: dict[int, object]) -> str:
        return "<redacted worker artifact capability>"


_WORKER_ARTIFACT_CAPABILITIES: WeakKeyDictionary[
    WorkerArtifact,
    _WorkerArtifactCapability,
] = WeakKeyDictionary()


def _bind_worker_artifact_capability(
    artifact: WorkerArtifact,
    *,
    artifact_path: Path | None,
    source_path: Path | None,
    expected_version: str | None,
    expected_value: int | None,
    admission: _WorkerArtifactAdmission | None,
) -> None:
    _WORKER_ARTIFACT_CAPABILITIES[artifact] = _WorkerArtifactCapability(
        artifact_path=artifact_path,
        source_path=source_path,
        expected_version=expected_version,
        expected_value=expected_value,
        admission=admission,
    )


def _worker_artifact_capability(
    artifact: WorkerArtifact,
    *,
    required: bool = True,
) -> _WorkerArtifactCapability | None:
    capability = _WORKER_ARTIFACT_CAPABILITIES.get(artifact)
    if capability is None and required:
        raise ProtocolError("Runtime API requires a proven worker artifact")
    return capability


_ARTIFACT_PROOF_KEY = token_bytes(32)
_SOURCE_CATALOG_PROOF_KEY = token_bytes(32)


def _span_structure(span: SourceSpan | None) -> tuple[int, int] | None:
    return None if span is None else (span.start, span.end)


def _reference_structure(
    reference: SourceUnitRef | SourceArtifactRef | None,
) -> tuple[object, ...] | None:
    if reference is None:
        return None
    if isinstance(reference, SourceUnitRef):
        return (
            "unit",
            reference.kind.value,
            reference.unit_id,
            reference.revision,
            reference.source_sha256,
        )
    return (
        "artifact",
        reference.kind.value,
        reference.source_sha256,
        reference.character_length,
        reference.line_ending_kind,
        reference.lowering_semantic_version,
        reference.wrapper_semantic_version,
        reference.mode,
        reference.worker_generation,
        reference.worker_manifest_sha256,
        reference.export_catalog_sha256,
    )


def _source_map_structure(source_map: SourceMap) -> tuple[object, ...]:
    return (
        _reference_structure(source_map.generated),
        tuple(
            (
                _span_structure(segment.generated),
                _reference_structure(segment.origin_ref),
                _span_structure(segment.origin),
                segment.relation.value,
                segment.synthetic_region,
                _reference_structure(segment.anchor_ref),
                _span_structure(segment.anchor_span),
            )
            for segment in source_map.segments
        ),
    )


def _mapped_source_structure(mapped: MappedSource) -> tuple[object, ...]:
    if isinstance(mapped, CompactMappedWorkerSource) and isinstance(
        mapped.source_map,
        CompactReloadSourceMap,
    ):
        local = mapped.local_source_map
        assert isinstance(local, CompactReloadSourceMap)
        return (
            "compact-reload-v1",
            id(mapped.text),
            len(mapped.text),
            _reference_structure(mapped.artifact),
            mapped.source_map.source_map_sha256,
            mapped.source_map._canonical_manifest_bytes,
            local.source_map_sha256,
            local._canonical_manifest_bytes,
        )
    try:
        local_source_map = mapped.local_source_map
        local_index = next(
            (
                index
                for index, source_map in enumerate(mapped.lineage)
                if source_map is local_source_map
            ),
            None,
        )
        local_structure = _source_map_structure(local_source_map)
    except ValueError:
        local_index = None
        local_structure = None
    return (
        id(mapped.text),
        len(mapped.text),
        _reference_structure(mapped.artifact),
        _source_map_structure(mapped.source_map),
        tuple(_source_map_structure(item) for item in mapped.lineage),
        local_index,
        local_structure,
    )


def _clone_source_reference(
    reference: SourceUnitRef | SourceArtifactRef | None,
) -> SourceUnitRef | SourceArtifactRef | None:
    if reference is None:
        return None
    return replace(reference)


def _clone_source_map(source_map: SourceMap) -> SourceMap:
    return SourceMap(
        replace(source_map.generated),
        tuple(
            SourceMapSegment(
                generated=replace(segment.generated),
                origin_ref=_clone_source_reference(segment.origin_ref),
                origin=(
                    None if segment.origin is None else replace(segment.origin)
                ),
                relation=segment.relation,
                synthetic_region=segment.synthetic_region,
                anchor_ref=_clone_source_reference(segment.anchor_ref),
                anchor_span=(
                    None
                    if segment.anchor_span is None
                    else replace(segment.anchor_span)
                ),
            )
            for segment in source_map.segments
        ),
    )


def _snapshot_mapped_source(
    mapped: MappedSource,
) -> tuple[MappedSource, tuple[object, ...]]:
    structure = _mapped_source_structure(mapped)
    if isinstance(mapped, CompactMappedWorkerSource):
        snapshot = snapshot_compact_mapped_worker_source(mapped)
        if _mapped_source_structure(snapshot) != structure:
            raise ProtocolError("Worker mapped source snapshot is invalid")
        return snapshot, structure
    map_cache: dict[int, SourceMap] = {}

    def clone_map(source_map: SourceMap) -> SourceMap:
        cloned = map_cache.get(id(source_map))
        if cloned is None:
            cloned = _clone_source_map(source_map)
            map_cache[id(source_map)] = cloned
        return cloned

    source_map = clone_map(mapped.source_map)
    lineage = tuple(clone_map(item) for item in mapped.lineage)
    try:
        local_source_map = clone_map(mapped.local_source_map)
    except ValueError:
        local_source_map = None
    snapshot = object.__new__(MappedSource)
    object.__setattr__(snapshot, "_text", mapped.text)
    object.__setattr__(snapshot, "_artifact", replace(mapped.artifact))
    object.__setattr__(snapshot, "_source_map", source_map)
    object.__setattr__(snapshot, "_lineage", lineage)
    object.__setattr__(snapshot, "_local_source_map", local_source_map)
    if (
        source_map.generated != snapshot.artifact
        or any(
            (
                segment.relation.value != "synthetic"
                and not isinstance(segment.origin_ref, SourceUnitRef)
            )
            or (
                segment.anchor_ref is not None
                and not isinstance(segment.anchor_ref, SourceUnitRef)
            )
            for segment in source_map.segments
        )
        or (
            local_source_map is not None
            and (
                local_source_map.generated != snapshot.artifact
                or not lineage
                or lineage[-1] != local_source_map
            )
        )
        or _mapped_source_structure(snapshot) != structure
    ):
        raise ProtocolError("Worker mapped source snapshot is invalid")
    return snapshot, structure


class _WorkerSourceCatalogValidation:
    """Exact semantic catalog result reusable only for unchanged source bytes."""

    __slots__ = (
        "__path",
        "__source",
        "__source_sha256",
        "__catalog",
        "__mapped_source_snapshot",
        "__mapped_source_structure",
        "__path_read_confirmed",
        "__proof",
    )

    def __init__(
        self,
        path: Path,
        source: bytes,
        source_sha256: str,
        catalog: tuple[WorkerExport, ...],
        mapped_source_snapshot: MappedSource | None,
        mapped_source_structure: tuple[object, ...] | None,
        path_read_confirmed: bool,
        proof: bytes,
    ) -> None:
        object.__setattr__(self, "_WorkerSourceCatalogValidation__path", path)
        object.__setattr__(self, "_WorkerSourceCatalogValidation__source", source)
        object.__setattr__(
            self,
            "_WorkerSourceCatalogValidation__source_sha256",
            source_sha256,
        )
        object.__setattr__(self, "_WorkerSourceCatalogValidation__catalog", catalog)
        object.__setattr__(
            self,
            "_WorkerSourceCatalogValidation__mapped_source_snapshot",
            mapped_source_snapshot,
        )
        object.__setattr__(
            self,
            "_WorkerSourceCatalogValidation__mapped_source_structure",
            mapped_source_structure,
        )
        object.__setattr__(
            self,
            "_WorkerSourceCatalogValidation__path_read_confirmed",
            path_read_confirmed,
        )
        object.__setattr__(self, "_WorkerSourceCatalogValidation__proof", proof)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Worker source catalog validations are immutable")

    def __repr__(self) -> str:
        return "<redacted worker source catalog validation>"

    def contents(
        self,
    ) -> tuple[
        Path,
        bytes,
        str,
        tuple[WorkerExport, ...],
        MappedSource | None,
        tuple[object, ...] | None,
        bool,
        bytes,
    ]:
        return (
            self.__path,
            self.__source,
            self.__source_sha256,
            self.__catalog,
            self.__mapped_source_snapshot,
            self.__mapped_source_structure,
            self.__path_read_confirmed,
            self.__proof,
        )


def _source_catalog_proof(
    path: Path,
    source: bytes,
    source_sha256: str,
    catalog: tuple[WorkerExport, ...],
    mapped_source_snapshot: MappedSource | None = None,
    mapped_source_structure: tuple[object, ...] | None = None,
    *,
    path_read_confirmed: bool = False,
) -> bytes:
    fields = (
        str(path),
        source_sha256,
        str(id(source)),
        str(id(mapped_source_snapshot)),
        str(id(mapped_source_structure)),
        "1" if path_read_confirmed else "0",
        *(
            f"{item.public_path}\0{item.method}\0{item.receiver_module or ''}"
            for item in catalog
        ),
    )
    return digest(
        _SOURCE_CATALOG_PROOF_KEY,
        "\x1f".join(fields).encode("utf-8"),
        "sha256",
    )


def validate_worker_source_catalog(
    source_path: Path,
    exports: tuple[WorkerExport, ...],
) -> _WorkerSourceCatalogValidation:
    """Semantically validate one exact source/catalog pair for later packaging."""
    resolved = Path(source_path).resolve()
    if not resolved.is_file():
        raise ProtocolError("Worker source file does not exist")
    source = resolved.read_bytes()
    catalog = _validate_source_catalog(resolved, source, exports)
    source_hash = sha256(source).hexdigest()
    return _WorkerSourceCatalogValidation(
        resolved,
        source,
        source_hash,
        catalog,
        None,
        None,
        False,
        _source_catalog_proof(resolved, source, source_hash, catalog),
    )


def _validated_source_catalog_snapshot(
    validation: _WorkerSourceCatalogValidation,
) -> tuple[
    Path,
    bytes,
    str,
    tuple[WorkerExport, ...],
    MappedSource | None,
    tuple[object, ...] | None,
    bool,
]:
    if not isinstance(validation, _WorkerSourceCatalogValidation):
        raise ProtocolError("Worker source catalog validation is invalid")
    try:
        (
            path,
            source,
            source_hash,
            catalog,
            mapped_snapshot,
            mapped_structure,
            path_read_confirmed,
            proof,
        ) = validation.contents()
        valid = compare_digest(
            proof,
            _source_catalog_proof(
                path,
                source,
                source_hash,
                catalog,
                mapped_snapshot,
                mapped_structure,
                path_read_confirmed=path_read_confirmed,
            ),
        )
    except Exception:
        raise ProtocolError("Worker source catalog validation is invalid") from None
    if not valid:
        raise ProtocolError("Worker source catalog validation is invalid")
    return (
        path,
        source,
        source_hash,
        catalog,
        mapped_snapshot,
        mapped_structure,
        path_read_confirmed,
    )


def reuse_worker_source_catalog_validation(
    validation: _WorkerSourceCatalogValidation,
    source_path: Path,
    exports: tuple[WorkerExport, ...],
) -> _WorkerSourceCatalogValidation:
    """Bind a proven catalog to another path only for byte-identical source."""
    _, validated_source, _, validated_catalog, _, _, _ = (
        _validated_source_catalog_snapshot(validation)
    )
    resolved = Path(source_path).resolve()
    catalog = validate_worker_export_catalog(exports, require_nonempty=True)
    try:
        source = resolved.read_bytes()
    except OSError:
        raise ProtocolError("Worker source catalog validation is invalid") from None
    if source != validated_source or catalog != validated_catalog:
        raise ProtocolError("Worker source catalog validation is invalid")
    source_hash = sha256(source).hexdigest()
    return _WorkerSourceCatalogValidation(
        resolved,
        source,
        source_hash,
        catalog,
        None,
        None,
        False,
        _source_catalog_proof(resolved, source, source_hash, catalog),
    )


def _reuse_worker_source_catalog(
    validation: _WorkerSourceCatalogValidation,
    source_path: Path,
    exports: tuple[WorkerExport, ...],
    mapped_source: MappedSource | None,
) -> tuple[tuple[WorkerExport, ...], bytes, str, MappedSource | None]:
    semantic_snapshot = False
    try:
        (
            validated_path,
            validated_source,
            source_hash,
            catalog,
            mapped_snapshot,
            mapped_structure,
            path_read_confirmed,
        ) = _validated_source_catalog_snapshot(validation)
        semantic_snapshot = mapped_structure is not None
        source = (
            validated_source
            if path_read_confirmed
            else source_path.read_bytes()
        )
        valid = (
            validated_path == source_path
            and validated_source == source
            and catalog == exports
            and (
                mapped_structure is None
                or (
                    mapped_source is not None
                    and _mapped_source_structure(mapped_source)
                    == mapped_structure
                )
            )
        )
    except Exception:
        message = (
            "Worker module artifact admission is invalid"
            if semantic_snapshot
            else "Worker source catalog validation is invalid"
        )
        raise ProtocolError(message) from None
    if not valid:
        message = (
            "Worker module artifact admission is invalid"
            if semantic_snapshot
            else "Worker source catalog validation is invalid"
        )
        raise ProtocolError(message)
    return catalog, source, source_hash, mapped_snapshot


def _semantic_worker_source_catalog_validation(
    admission: WorkerSemanticAdmission,
    source: MappedSource,
    source_path: Path,
    source_bytes: bytes,
    exports: tuple[WorkerExport, ...],
    *,
    expected_packer_identity: str,
) -> _WorkerSourceCatalogValidation:
    """Bind a semantic result to the one generated-file read-back."""
    try:
        catalog = validate_worker_export_catalog(exports, require_nonempty=True)
        lowered = _validate_worker_semantic_admission(
            admission,
            source=source,
            exported_methods=tuple(item.method for item in catalog),
            expected_packer_identity=expected_packer_identity,
        )
        logical_name = lowered.analysis.unit.logical_name
        expected_identity = tuple(
            sorted(
                (
                    f"{logical_name}.{method}".casefold(),
                    method.casefold(),
                    logical_name.casefold(),
                )
                for method in lowered.analysis.exported_methods
            )
        )
        if (
            source_bytes != source.text.encode("utf-8")
            or _worker_export_catalog_identity(catalog) != expected_identity
        ):
            raise ValueError
        resolved = source_path.resolve()
        source_hash = sha256(source_bytes).hexdigest()
        if source_hash != source.artifact.source_sha256:
            raise ValueError
        mapped_snapshot, mapped_structure = _snapshot_mapped_source(source)
        return _WorkerSourceCatalogValidation(
            resolved,
            source_bytes,
            source_hash,
            catalog,
            mapped_snapshot,
            mapped_structure,
            True,
            _source_catalog_proof(
                resolved,
                source_bytes,
                source_hash,
                catalog,
                mapped_snapshot,
                mapped_structure,
                path_read_confirmed=True,
            ),
        )
    except Exception:
        raise ProtocolError("Worker module artifact admission is invalid") from None


def validate_worker_export_catalog(
    exports: tuple[WorkerExport, ...],
    *,
    require_nonempty: bool = False,
) -> tuple[WorkerExport, ...]:
    """Validate a concrete public worker catalog before any runtime instruction."""
    catalog: dict[str, WorkerExport] = {}
    for export in exports:
        if not isinstance(export, WorkerExport):
            raise ProtocolError("Worker export catalog is invalid")
        key = export.public_path.casefold()
        if key in catalog:
            raise ValueError(f"duplicate worker export {export.public_path!r}")
        catalog[key] = export
    if require_nonempty and not catalog:
        raise ProtocolError("Worker artifact must declare a nonempty export catalog")
    return tuple(catalog.values())


def build_worker_artifact(
    *,
    logical_name: str,
    source_path: Path,
    artifact_path: Path,
    expected_version: str | None = None,
    expected_value: int | None = None,
    exports: tuple[WorkerExport, ...],
    source_validation: _WorkerSourceCatalogValidation | None = None,
) -> WorkerArtifact:
    """Compatibility builder for source-only Worker packages.

    Notebook production uses ``NotebookWorkerArtifactBuilder`` with mapped input;
    source-only callers receive an explicitly synthetic diagnostic origin.
    """
    if (expected_version is None) != (expected_value is None):
        raise ValueError(
            "Worker artifact expected_version and expected_value must be both set or both omitted"
        )
    source_path = source_path.resolve()
    artifact_path = artifact_path.resolve()
    catalog, source_bytes, artifact_bytes, source_hash, _ = _validate_artifact_files(
        source_path,
        artifact_path,
        exports,
        source_validation=source_validation,
    )
    from onec_runtime.worker_epf import _synthetic_worker_module_source

    mapped = _synthetic_worker_module_source(
        source_bytes.decode("utf-8"),
        normalize=False,
    )
    return _build_admitted_worker_artifact(
        logical_name=logical_name,
        source_path=source_path,
        artifact_path=artifact_path,
        source_bytes=source_bytes,
        artifact_bytes=artifact_bytes,
        source_bytes_sha256=source_hash,
        mapped=mapped,
        visible_context_snapshot=None,
        expected_version=expected_version,
        expected_value=expected_value,
        exports=catalog,
    )


def _build_admitted_worker_artifact(
    *,
    logical_name: str,
    source_path: Path,
    artifact_path: Path,
    source_bytes: bytes,
    artifact_bytes: bytes,
    source_bytes_sha256: str,
    mapped: MappedSource,
    visible_context_snapshot: _WorkerVisibleContextSnapshot | None,
    expected_version: str | None,
    expected_value: int | None,
    exports: tuple[WorkerExport, ...],
    binary_snapshot: _AdmittedWorkerBinary | None = None,
    mapped_snapshot: bool = False,
) -> WorkerArtifact:
    if not mapped_snapshot:
        mapped, _ = _snapshot_mapped_source(mapped)
    if mapped.artifact.kind is not SourceArtifactKind.WORKER_MODULE:
        raise ProtocolError("Worker admission requires a final WORKER_MODULE map")
    if binary_snapshot is None:
        if (
            mapped.text.encode("utf-8") != source_bytes
            or fullmatch(r"[0-9a-f]{64}", source_bytes_sha256) is None
        ):
            raise ProtocolError("Worker mapped source does not match source snapshot")
        binary = _AdmittedWorkerBinary(
            source_bytes,
            artifact_bytes,
            source_bytes_sha256,
            sha256(artifact_bytes).hexdigest(),
        )
    else:
        binary = binary_snapshot
        if (
            source_bytes is not binary.source_bytes
            or artifact_bytes is not binary.artifact_bytes
            or source_bytes_sha256 != binary.source_sha256
        ):
            raise ProtocolError("Worker mapped source does not match source snapshot")
    if binary.source_sha256 != mapped.artifact.source_sha256:
        raise ProtocolError("Worker mapped source does not match source snapshot")
    source_map_hash = mapped.source_map_sha256
    provenance = WorkerSourceProvenance(
        mapped.artifact.source_sha256,
        source_map_hash,
        mapped.artifact.worker_generation,
        mapped.artifact.worker_manifest_sha256,
    )
    artifact = WorkerArtifact(
        logical_name=logical_name,
        source_sha256=mapped.artifact.source_sha256,
        artifact_sha256=binary.artifact_sha256,
        exports=exports,
        source_provenance=provenance,
    )
    context_fingerprint = (
        "" if visible_context_snapshot is None else visible_context_snapshot.fingerprint
    )
    snapshot = _AdmittedWorkerSnapshot(
        binary=binary,
        proof=b"",
        mapped_source=mapped,
        mapped_guard=_mapped_source_guard(mapped),
        source_map_sha256=source_map_hash,
        worker_generation=mapped.artifact.worker_generation,
        worker_manifest_sha256=mapped.artifact.worker_manifest_sha256,
        catalog=exports,
        visible_context_snapshot=visible_context_snapshot,
        visible_context_fingerprint=context_fingerprint,
        visible_context_guard=_visible_context_snapshot_guard(
            visible_context_snapshot
        ),
        source_path=source_path,
        artifact_path=artifact_path,
        expected_version=expected_version,
        expected_value=expected_value,
    )
    snapshot = replace(snapshot, proof=_artifact_proof(artifact, snapshot))
    admission = _WorkerArtifactAdmission(artifact, snapshot)
    _bind_worker_artifact_capability(
        artifact,
        artifact_path=artifact_path,
        source_path=source_path,
        expected_version=expected_version,
        expected_value=expected_value,
        admission=admission,
    )
    return artifact


class NotebookWorkerArtifactBuilder:
    """Build a generic worker EPF from generated-AST method source.

    The builder writes only runtime-generated worker inputs.  Candidate admission
    and the eventual module-generation publication remain owned by the
    Worker universe registry.
    """

    def __init__(self, config: RuntimeConfig, *, logical_name: str = "Worker") -> None:
        self._config = config
        self._logical_name = logical_name

    def __call__(
        self,
        source: MappedSource,
        exports: tuple[WorkerExport, ...],
        *,
        visible_source_context: VisibleSourceContext,
        semantic_admission: WorkerSemanticAdmission | None = None,
        semantic_packer_identity: str | None = None,
    ) -> WorkerArtifact:
        if not isinstance(source, MappedSource):
            raise TypeError("Notebook Worker source must be a MappedSource")
        if not source.text.strip():
            raise ProtocolError("Notebook worker source is empty")
        catalog = validate_worker_export_catalog(exports, require_nonempty=True)
        from onec_runtime.worker_epf import (
            build_worker_epf,
            prepare_worker_module_source,
        )

        mapped = (
            source
            if (
                semantic_admission is not None
                and source.artifact.kind is SourceArtifactKind.WORKER_MODULE
            )
            else prepare_worker_module_source(source)
        )
        visible_context_snapshot = _snapshot_visible_source_context(
            mapped,
            visible_source_context,
            required=True,
        )
        source_hash = mapped.artifact.source_sha256
        runtime_dir = self._config.runtime_dir
        build_dir = self._config.build_dir
        root = runtime_dir / "generated" / "notebook-workers" / source_hash
        source_module = root / "Worker" / "Ext" / "ObjectModule.bsl"
        artifact_path = build_dir / "notebook-workers" / f"{source_hash}.epf"
        source_module.parent.mkdir(parents=True, exist_ok=True)
        source_module.write_text(mapped.text, encoding="utf-8", newline="\n")
        source_validation = None
        if semantic_admission is not None:
            if semantic_packer_identity is None:
                raise ProtocolError("Worker module artifact admission is invalid")
            try:
                source_bytes = source_module.read_bytes()
            except OSError:
                raise ProtocolError(
                    "Worker module artifact admission is invalid"
                ) from None
            source_validation = _semantic_worker_source_catalog_validation(
                semantic_admission,
                source,
                source_module,
                source_bytes,
                catalog,
                expected_packer_identity=semantic_packer_identity,
            )
        elif semantic_packer_identity is not None:
            raise ProtocolError("Worker module artifact admission is invalid")
        build_worker_epf(mapped, artifact_path)
        (
            catalog,
            source_bytes,
            artifact_bytes,
            source_hash,
            admitted_mapped,
        ) = _validate_artifact_files(
            source_module,
            artifact_path,
            catalog,
            source_validation=source_validation,
            mapped_source=mapped,
        )
        return _build_admitted_worker_artifact(
            logical_name=self._logical_name,
            source_path=source_module,
            artifact_path=artifact_path,
            source_bytes=source_bytes,
            artifact_bytes=artifact_bytes,
            source_bytes_sha256=source_hash,
            mapped=mapped if admitted_mapped is None else admitted_mapped,
            visible_context_snapshot=visible_context_snapshot,
            expected_version=None,
            expected_value=None,
            exports=catalog,
            mapped_snapshot=admitted_mapped is not None,
        )


def rebind_worker_artifact_binary(
    artifact: WorkerArtifact,
    *,
    logical_name: str,
    mapped: MappedSource,
    visible_source_context: VisibleSourceContext,
    exports: tuple[WorkerExport, ...],
) -> WorkerArtifact:
    """Bind admitted byte-identical EPF content to one exact source-map revision."""
    try:
        admitted_catalog = validate_production_worker_artifact(artifact)
        admitted = _validated_admitted_snapshot(artifact)
        admitted_mapped = admitted.mapped_source
        catalog = validate_worker_export_catalog(exports, require_nonempty=True)
        if (
            not isinstance(mapped, MappedSource)
            or mapped.artifact.kind is not SourceArtifactKind.WORKER_MODULE
            or mapped.text != admitted_mapped.text
            or mapped.artifact.source_sha256 != admitted_mapped.artifact.source_sha256
            or _worker_export_catalog_identity(catalog)
            != _worker_export_catalog_identity(admitted_catalog)
        ):
            raise ProtocolError("Worker binary rebind admission is invalid")
        snapshot = _snapshot_visible_source_context(
            mapped,
            visible_source_context,
            required=True,
        )
        return _build_admitted_worker_artifact(
            logical_name=logical_name,
            source_path=admitted.source_path,
            artifact_path=admitted.artifact_path,
            source_bytes=admitted.source_bytes,
            artifact_bytes=admitted.artifact_bytes,
            source_bytes_sha256=admitted.source_sha256,
            mapped=mapped,
            visible_context_snapshot=snapshot,
            expected_version=admitted.expected_version,
            expected_value=admitted.expected_value,
            exports=catalog,
            binary_snapshot=admitted.binary,
        )
    except ProtocolError:
        raise
    except Exception:
        raise ProtocolError("Worker binary rebind admission is invalid") from None


def _worker_export_catalog_identity(
    catalog: tuple[WorkerExport, ...],
) -> tuple[tuple[str, str, str | None], ...]:
    return tuple(
        sorted(
            (
                item.public_path.casefold(),
                item.method.casefold(),
                (
                    None
                    if item.receiver_module is None
                    else item.receiver_module.casefold()
                ),
            )
            for item in catalog
        )
    )


def _mapped_source_guard(mapped: MappedSource) -> tuple[int, ...]:
    """Cheap identity fence for objects that are immutable after admission."""
    source_map = mapped.source_map
    lineage = mapped.lineage
    if isinstance(source_map, CompactReloadSourceMap):
        local = mapped.local_source_map
        if not isinstance(local, CompactReloadSourceMap):
            raise ProtocolError("Worker mapped source snapshot is invalid")
        return (
            id(mapped),
            id(mapped.text),
            id(mapped.artifact),
            *source_map.compact_guard(),
            id(lineage),
            *local.compact_guard(),
        )
    guard = [
        id(mapped),
        id(mapped.text),
        id(mapped.artifact),
        id(source_map),
        id(source_map.segments),
        id(lineage),
    ]
    for item in lineage:
        guard.extend((id(item), id(item.segments)))
    return tuple(guard)


def _visible_context_snapshot_guard(
    snapshot: _WorkerVisibleContextSnapshot | None,
) -> tuple[object, ...]:
    if snapshot is None:
        return ()
    try:
        context = snapshot.context
        indices = context._indices  # type: ignore[attr-defined]
        entries = tuple(
            sorted(
                (
                    getattr(kind, "value", str(kind)),
                    unit_id,
                    revision,
                    source_hash,
                    id(entry),
                    id(line_index),
                    line_index._length,  # type: ignore[attr-defined]
                    id(line_index._line_starts),  # type: ignore[attr-defined]
                )
                for (kind, unit_id, revision), entry in indices.items()
                for source_hash, line_index in (entry,)
            )
        )
    except Exception:
        raise ProtocolError(
            "Worker visible source context fingerprint does not match"
        ) from None
    return (id(snapshot), id(context), id(indices), entries)


def _snapshot_visible_source_context(
    mapped: MappedSource,
    visible_source_context: VisibleSourceContext | None,
    *,
    required: bool,
) -> _WorkerVisibleContextSnapshot | None:
    if visible_source_context is None:
        if required or _visible_context_manifest(mapped, None):
            raise ProtocolError("Worker visible source context is required for its map")
        return None
    if not isinstance(visible_source_context, VisibleSourceContext):
        raise ProtocolError("Worker visible source context does not match its map")
    indices: dict[
        tuple[object, str, int],
        tuple[str, _ImmutableLineIndex],
    ] = {}
    for unit in _referenced_visible_units(mapped):
        source_hash, length, line_starts = _visible_line_index_components(
            visible_source_context,
            unit,
        )
        indices[(unit.kind, unit.unit_id, unit.revision)] = (
            source_hash,
            _ImmutableLineIndex(length, line_starts),
        )
    context = _ImmutableVisibleSourceContext(indices)
    fingerprint = _visible_context_fingerprint(mapped, context)
    return _WorkerVisibleContextSnapshot(context, fingerprint)


def _referenced_visible_units(mapped: MappedSource) -> tuple[SourceUnitRef, ...]:
    units: dict[tuple[object, str, int], SourceUnitRef] = {}
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
    for reference in references:
        if reference is None:
            continue
        if not isinstance(reference, SourceUnitRef):
            raise ProtocolError(
                "Worker visible source context does not match its flattened map"
            )
        key = (reference.kind, reference.unit_id, reference.revision)
        existing = units.get(key)
        if existing is not None and existing.source_sha256 != reference.source_sha256:
            raise ProtocolError(
                "Worker visible source context has conflicting source identities"
            )
        units[key] = reference
    return tuple(
        sorted(
            units.values(),
            key=lambda unit: (
                unit.kind.value,
                unit.unit_id,
                unit.revision,
                unit.source_sha256,
            ),
        )
    )


def _visible_line_index_components(
    context: VisibleSourceContext,
    unit: SourceUnitRef,
) -> tuple[str, int, tuple[int, ...]]:
    try:
        entry = context._indices.get(  # type: ignore[attr-defined]
            (unit.kind, unit.unit_id, unit.revision)
        )
        source_hash, line_index = entry
        length = line_index._length
        line_starts = line_index._line_starts
    except (AttributeError, TypeError, ValueError):
        raise ProtocolError(
            "Worker visible source context does not cover every mapped unit"
        ) from None
    if source_hash != unit.source_sha256 or type(line_starts) is not tuple:
        raise ProtocolError(
            "Worker visible source context does not match its mapped unit"
        )
    validated = _ImmutableLineIndex(length, line_starts)
    return source_hash, validated._length, validated._line_starts


def _visible_context_manifest(
    mapped: MappedSource,
    context: VisibleSourceContext | None,
) -> tuple[dict[str, object], ...]:
    entries: list[dict[str, object]] = []
    compact_spans = getattr(mapped.source_map, "compact_visible_spans", None)
    mapped_spans = (
        compact_spans()
        if compact_spans is not None
        else tuple(
            (role, reference, span)
            for segment in mapped.source_map.segments
            for role, reference, span in (
                ("origin", segment.origin_ref, segment.origin),
                ("anchor", segment.anchor_ref, segment.anchor_span),
            )
            if reference is not None and span is not None
        )
    )
    for segment_index, (role, reference, span) in enumerate(mapped_spans):
        if reference is None:
            continue
        if not isinstance(reference, SourceUnitRef) or span is None:
            raise ProtocolError(
                "Worker visible source context does not match its flattened map"
            )
        start = None if context is None else context.line_column(reference, span.start)
        end = None if context is None else context.line_column(reference, span.end)
        if context is not None and (start is None or end is None):
            raise ProtocolError(
                "Worker visible source context does not cover every mapped span"
            )
        entries.append(
            {
                "segment": segment_index,
                "role": role,
                "kind": reference.kind.value,
                "unit_id": reference.unit_id,
                "revision": reference.revision,
                "source_sha256": reference.source_sha256,
                "span": [span.start, span.end],
                "start": None if start is None else list(start),
                "end": None if end is None else list(end),
            }
        )
    return tuple(entries)


def _visible_context_unit_manifest(
    mapped: MappedSource,
    context: VisibleSourceContext,
) -> tuple[dict[str, object], ...]:
    entries: list[dict[str, object]] = []
    for unit in _referenced_visible_units(mapped):
        source_hash, length, line_starts = _visible_line_index_components(
            context,
            unit,
        )
        entries.append(
            {
                "kind": unit.kind.value,
                "unit_id": unit.unit_id,
                "revision": unit.revision,
                "source_sha256": source_hash,
                "character_length": length,
                "line_starts": list(line_starts),
            }
        )
    return tuple(entries)


def _visible_context_fingerprint(
    mapped: MappedSource,
    context: VisibleSourceContext,
) -> str:
    manifest = {
        "units": _visible_context_unit_manifest(mapped, context),
        "occurrences": _visible_context_manifest(mapped, context),
    }
    payload = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def validate_production_worker_artifact(
    artifact: WorkerArtifact,
) -> tuple[WorkerExport, ...]:
    """Revalidate a complete production artifact immediately before activation."""
    return _validated_admitted_snapshot(artifact).catalog


def _admission_contents(
    artifact: WorkerArtifact,
) -> tuple[bytes, bytes, bytes, MappedSource]:
    capability = _worker_artifact_capability(artifact)
    assert capability is not None
    admission = capability.admission
    if not isinstance(admission, _WorkerArtifactAdmission):
        raise ProtocolError("Runtime API requires a proven worker artifact")
    return admission.contents(artifact)


def _validated_admission_contents(
    artifact: WorkerArtifact,
) -> tuple[bytes, bytes, bytes, MappedSource]:
    snapshot = _validated_admitted_snapshot(artifact)
    return (
        snapshot.source_bytes,
        snapshot.artifact_bytes,
        snapshot.proof,
        _snapshot_mapped_source(snapshot.mapped_source)[0],
    )


def _validated_admitted_snapshot(
    artifact: WorkerArtifact,
) -> _AdmittedWorkerSnapshot:
    capability = _worker_artifact_capability(artifact)
    assert capability is not None
    admission = capability.admission
    if not isinstance(admission, _WorkerArtifactAdmission):
        raise ProtocolError("Runtime API requires a proven worker artifact")
    snapshot = admission.snapshot(artifact)
    mapped = snapshot.mapped_source
    provenance = artifact.source_provenance
    if provenance is None or provenance.source_sha256 != artifact.source_sha256:
        raise ProtocolError("Worker artifact source map provenance is invalid")
    if mapped.artifact.kind is not SourceArtifactKind.WORKER_MODULE:
        raise ProtocolError("Worker artifact source map does not describe WORKER_MODULE")
    if (
        not snapshot.source_bytes
        or not snapshot.artifact_bytes
        or snapshot.source_sha256 != artifact.source_sha256
        or snapshot.artifact_sha256 != artifact.artifact_sha256
        or mapped.artifact.source_sha256 != artifact.source_sha256
    ):
        raise ProtocolError("Worker artifact source map source hash does not match")
    if snapshot.source_map_sha256 != provenance.source_map_sha256:
        raise ProtocolError("Worker artifact source map hash does not match")
    if (
        provenance.worker_generation != snapshot.worker_generation
        or provenance.worker_manifest_sha256
        != snapshot.worker_manifest_sha256
    ):
        raise ProtocolError("Worker artifact source map provenance is invalid")
    if (
        capability.source_path != snapshot.source_path
        or capability.artifact_path != snapshot.artifact_path
        or capability.expected_version != snapshot.expected_version
        or capability.expected_value != snapshot.expected_value
        or snapshot.mapped_guard != _mapped_source_guard(mapped)
    ):
        raise ProtocolError("Worker artifact source map proof does not match admission")
    context_snapshot = snapshot.visible_context_snapshot
    if context_snapshot is None:
        if _visible_context_manifest(mapped, None):
            raise ProtocolError("Worker visible source context is missing from admission")
        if snapshot.visible_context_fingerprint or snapshot.visible_context_guard:
            raise ProtocolError("Worker visible source context fingerprint does not match")
    else:
        if (
            not compare_digest(
                snapshot.visible_context_fingerprint,
                context_snapshot.fingerprint,
            )
            or snapshot.visible_context_guard
            != _visible_context_snapshot_guard(context_snapshot)
        ):
            raise ProtocolError("Worker visible source context fingerprint does not match")
    if not compare_digest(
        snapshot.proof,
        _artifact_proof(artifact, snapshot),
    ):
        raise ProtocolError("Worker artifact source map proof does not match admission")
    if admission.catalog(artifact) != snapshot.catalog:
        raise ProtocolError("Runtime API requires a proven worker artifact")
    return snapshot


def _validate_artifact_files(
    source_path: Path,
    artifact_path: Path,
    exports: tuple[WorkerExport, ...],
    *,
    source_validation: _WorkerSourceCatalogValidation | None = None,
    mapped_source: MappedSource | None = None,
) -> tuple[
    tuple[WorkerExport, ...],
    bytes,
    bytes,
    str,
    MappedSource | None,
]:
    catalog = validate_worker_export_catalog(exports, require_nonempty=True)
    if not source_path.is_file():
        raise ProtocolError("Worker source file does not exist")
    if not artifact_path.is_file():
        raise ProtocolError("Worker artifact file does not exist")
    if source_validation is None:
        source_bytes = source_path.read_bytes()
        catalog = _validate_source_catalog(source_path, source_bytes, exports)
        source_hash = sha256(source_bytes).hexdigest()
        mapped_snapshot = None
    elif isinstance(source_validation, _WorkerSourceCatalogValidation):
        catalog, source_bytes, source_hash, mapped_snapshot = (
            _reuse_worker_source_catalog(
                source_validation,
                source_path,
                exports,
                mapped_source,
            )
        )
    else:
        raise ProtocolError("Worker source catalog validation is invalid")
    return (
        catalog,
        source_bytes,
        artifact_path.read_bytes(),
        source_hash,
        mapped_snapshot,
    )


def _validate_source_catalog(
    source_path: Path,
    source_bytes: bytes,
    exports: tuple[WorkerExport, ...],
) -> tuple[WorkerExport, ...]:
    catalog = validate_worker_export_catalog(exports, require_nonempty=True)
    source = source_bytes.decode("utf-8")
    binding = SemanticNotebookLowerer(PythonParserTarget.from_generated()).bind_module(source)
    exported = {name.casefold() for name in binding.exported_method_names}
    for export in catalog:
        if export.method.casefold() not in exported:
            raise ProtocolError(
                f"Worker catalog method {export.method!r} is not exported by {source_path}"
            )
    return catalog


def _artifact_proof(
    artifact: WorkerArtifact,
    snapshot: _AdmittedWorkerSnapshot,
) -> bytes:
    fields = (
        artifact.logical_name,
        str(snapshot.artifact_path),
        str(snapshot.source_path),
        artifact.source_sha256,
        "" if artifact.source_map_sha256 is None else artifact.source_map_sha256,
        (
            ""
            if artifact.source_provenance is None
            or artifact.source_provenance.worker_generation is None
            else str(artifact.source_provenance.worker_generation)
        ),
        (
            ""
            if artifact.source_provenance is None
            or artifact.source_provenance.worker_manifest_sha256 is None
            else artifact.source_provenance.worker_manifest_sha256
        ),
        artifact.artifact_sha256,
        "" if snapshot.expected_version is None else snapshot.expected_version,
        str(snapshot.expected_value),
        snapshot.source_sha256,
        snapshot.artifact_sha256,
        snapshot.source_map_sha256,
        snapshot.visible_context_fingerprint,
        str(id(snapshot.binary)),
        str(id(snapshot.source_bytes)),
        str(id(snapshot.artifact_bytes)),
        repr(snapshot.mapped_guard),
        repr(snapshot.visible_context_guard),
        str(id(snapshot.catalog)),
        *(
            f"{item.public_path}\0{item.method}\0{item.receiver_module or ''}"
            for item in artifact.exports
        ),
    )
    return digest(_ARTIFACT_PROOF_KEY, "\x1f".join(fields).encode("utf-8"), "sha256")


def stage_worker_module_instruction(
    content: bytes,
    *,
    logical_name: str,
    artifact_sha256: str,
    registration_name: str,
) -> str:
    """Upload and connect one module without touching the active generation."""
    if not isinstance(content, bytes) or not content:
        raise ValueError("worker artifact content must be nonempty bytes")
    if (
        not _worker_safe_identifier(logical_name)
        or fullmatch(r"[0-9a-f]{64}", artifact_sha256) is None
        or not _worker_safe_identifier(registration_name)
    ):
        raise ValueError("worker artifact stage identity is invalid")
    encoded = bsl_string_literal(b64encode(content).decode("ascii"))
    name = bsl_string_literal(registration_name)
    logical_digest = sha256(logical_name.casefold().encode("utf-8")).hexdigest()
    marker = bsl_string_literal(
        _worker_artifact_stage_marker(
            artifact_sha256=artifact_sha256,
            logical_name_sha256=logical_digest,
            phase="connect",
        )
    )
    return "\n".join(
        (
            f"МаркерАртефактаWorker = {marker};",
            "Попытка",
            '    ЭтапАртефактаWorker = "upload";',
            '    АдресАртефактаWorker = ПоместитьВоВременноеХранилище('
            f"Base64Значение({encoded}));",
            '    ЭтапАртефактаWorker = "connect";',
            "    ИмяАртефактаWorker = ВнешниеОбработки.Подключить(",
            f"        АдресАртефактаWorker, {name}, Ложь);",
            "Исключение",
            "    ОшибкаАртефактаWorker = ОписаниеОшибки();",
            "    ВызватьИсключение МаркерАртефактаWorker",
            '        + ";boundary=" + ЭтапАртефактаWorker',
            "        + Символы.ПС + ОшибкаАртефактаWorker;",
            "КонецПопытки;",
            "Результат = ИмяАртефактаWorker;",
        )
    )


_WORKER_ARTIFACT_STAGE_PREFIX = "onec-worker-artifact-stage="


def _worker_artifact_stage_marker(
    *,
    artifact_sha256: str,
    logical_name_sha256: str,
    phase: Literal["connect"],
) -> str:
    return (
        f"{_WORKER_ARTIFACT_STAGE_PREFIX}"
        f"artifact_sha256={artifact_sha256};"
        f"logical_name_sha256={logical_name_sha256};phase={phase}"
    )


def _worker_safe_identifier(value: object) -> bool:
    return _worker_stage_safe_identifier(value)


def _worker_reload_platform_message(error: BaseException) -> str:
    """Remove our control header without changing the platform diagnostic body."""
    message = str(error)
    artifact_marker = message.find(_WORKER_ARTIFACT_STAGE_PREFIX)
    if artifact_marker >= 0:
        artifact_line = message[
            artifact_marker + len(_WORKER_ARTIFACT_STAGE_PREFIX) :
        ]
        header, separator, remainder = artifact_line.partition("\n")
        if separator and _worker_artifact_stage_header(header.rstrip("\r")) is not None:
            length = search(r";diagnostic_utf16_length=([1-9][0-9]{0,5})\r?$", header)
            if length is not None:
                # 1C appends the outer Raise stack after this UTF-16-sized body.
                units = int(length.group(1))
                encoded = remainder[:units].encode("utf-16-le")
                if len(encoded) < units * 2:
                    return message
                try:
                    return encoded[:units * 2].decode("utf-16-le")
                except UnicodeDecodeError:
                    return message
            return remainder
    return message


def _worker_artifact_stage_header(
    value: str,
) -> tuple[str, str, Literal["upload", "connect", "create"]] | None:
    match = fullmatch(
        r"artifact_sha256=([0-9a-f]{64});"
        r"logical_name_sha256=([0-9a-f]{64});"
        r"(?:phase=connect;boundary=(upload|connect)|phase=create;boundary=(create)"
        r"(?:;diagnostic_utf16_length=[1-9][0-9]{0,5})?)",
        value,
    )
    if match is None:
        return None
    artifact_sha256, logical_name_sha256, boundary, create = match.groups()
    return (
        artifact_sha256,
        logical_name_sha256,
        cast(Literal["upload", "connect", "create"], boundary or create),
    )


def _worker_artifact_failure_identity(
    error: BaseException,
) -> tuple[str, str, Literal["upload", "connect", "create"]] | None:
    message = str(error)
    marker = message.find(_WORKER_ARTIFACT_STAGE_PREFIX)
    if marker < 0:
        return None
    value = message[marker + len(_WORKER_ARTIFACT_STAGE_PREFIX) :].splitlines()[0]
    return _worker_artifact_stage_header(value.rstrip("\r"))


def worker_artifact_stage_failure(
    error: BaseException,
) -> WorkerArtifactStageFailure | None:
    """Return the exact admitted artifact/phase marker without diagnostic prose."""
    identity = _worker_artifact_failure_identity(error)
    if identity is None:
        return None
    artifact_sha256, _logical_name_sha256, phase = identity
    return WorkerArtifactStageFailure(artifact_sha256, phase)


def _worker_artifact_diagnostic_source_from_snapshot(
    artifact: WorkerArtifact,
    snapshot: _AdmittedWorkerSnapshot,
    *,
    logical_name: str,
    revision: int,
    registration_name: str,
    manifest_sha256: str,
    artifact_sha256: str,
    source_map_sha256: str,
) -> WorkerDiagnosticArtifact:
    """Bind one already-validated admitted snapshot without cloning it."""
    mapped = snapshot.mapped_source
    visible_context_snapshot = snapshot.visible_context_snapshot
    if (
        not isinstance(artifact, WorkerArtifact)
        or not isinstance(snapshot, _AdmittedWorkerSnapshot)
        or artifact.logical_name.casefold() != logical_name.casefold()
        or artifact.source_sha256 != snapshot.source_sha256
        or artifact.artifact_sha256 != artifact_sha256
        or snapshot.artifact_sha256 != artifact_sha256
        or artifact.source_map_sha256 != source_map_sha256
        or snapshot.source_map_sha256 != source_map_sha256
        or mapped.artifact.source_sha256 != snapshot.source_sha256
        or mapped.source_map_sha256 != source_map_sha256
        or (
            visible_context_snapshot is not None
            and not isinstance(
                visible_context_snapshot.context,
                _ImmutableVisibleSourceContext,
            )
        )
    ):
        raise ProtocolError("Worker diagnostic source identity is invalid")
    try:
        return WorkerDiagnosticArtifact(
            logical_name=logical_name,
            revision=revision,
            artifact_sha256=artifact_sha256,
            registration_name=registration_name,
            manifest_sha256=manifest_sha256,
            source_map_sha256=source_map_sha256,
            mapped_source=mapped,
            visible_source_context=(
                None
                if visible_context_snapshot is None
                else visible_context_snapshot.context
            ),
        )
    except ValueError:
        raise ProtocolError("Worker diagnostic source identity is invalid") from None


def worker_artifact_diagnostic_source(
    artifact: WorkerArtifact,
    *,
    logical_name: str,
    revision: int,
    registration_name: str,
    manifest_sha256: str,
    artifact_sha256: str,
    source_map_sha256: str,
) -> WorkerDiagnosticArtifact:
    """Expose an admitted source map as a private, manifest-fenced descriptor."""
    snapshot = _validated_admitted_snapshot(artifact)
    mapped = _snapshot_mapped_source(snapshot.mapped_source)[0]
    visible_context_snapshot = _clone_visible_context_snapshot(
        snapshot.visible_context_snapshot
    )
    if (
        artifact.artifact_sha256 != artifact_sha256
        or artifact.source_map_sha256 != source_map_sha256
        or snapshot.source_map_sha256 != source_map_sha256
    ):
        raise ProtocolError("Worker diagnostic source identity is invalid")
    try:
        return WorkerDiagnosticArtifact(
            logical_name=logical_name,
            revision=revision,
            artifact_sha256=artifact_sha256,
            registration_name=registration_name,
            manifest_sha256=manifest_sha256,
            source_map_sha256=source_map_sha256,
            mapped_source=mapped,
            visible_source_context=(
                None
                if visible_context_snapshot is None
                else visible_context_snapshot.context
            ),
        )
    except ValueError:
        raise ProtocolError("Worker diagnostic source identity is invalid") from None


def remap_worker_artifact_stage_error(
    error: BslExecutionError,
    *,
    candidate_manifest_sha256: str,
    candidate_artifacts: tuple[WorkerDiagnosticArtifact, ...],
) -> BslExecutionError:
    """Attach exact or explicitly-unmapped evidence to one stage failure."""
    identity = worker_artifact_stage_failure(error)
    if identity is None:
        return error
    parsed = parse_platform_diagnostic(_worker_reload_platform_message(error))
    if identity.phase == "create":
        header = _worker_artifact_failure_identity(error)
        assert header is not None
        candidate_artifacts = tuple(
            artifact for artifact in candidate_artifacts
            if sha256(artifact.logical_name.casefold().encode("utf-8")).hexdigest()
            == header[1]
        )
    diagnostic = remap_worker_stage_diagnostic(
        parsed,
        artifact_sha256=identity.artifact_sha256,
        phase=identity.phase,
        candidate_manifest_sha256=candidate_manifest_sha256,
        candidate_artifacts=candidate_artifacts,
    )
    return BslExecutionError(
        str(error),
        messages=error.messages,
        diagnostic=diagnostic,
    )
