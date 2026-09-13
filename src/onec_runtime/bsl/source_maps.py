"""Immutable identities and coordinate helpers for BSL source maps."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
from hmac import compare_digest, new as hmac_new
import json
import re
from secrets import token_bytes
from threading import Lock


_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_TEXT_WORK_OBSERVER = None
_RETAINED_PROOF_KEY = token_bytes(32)
_SOURCE_MAP_PROOF_KEY = token_bytes(32)
_WORKER_TEXT_PROOF_KEY = token_bytes(32)
_RETAINED_CONSTRUCTION_AUTHORITY = object()
_TRUSTED_TEXT_PROOF_AUTHORITY = object()
_NORMALIZED_WORKER_AUTHORITY = object()
_TRUSTED_TEXT_PROOFS: dict[int, _TrustedTextProof] = {}
_TRUSTED_TEXT_PROOFS_LOCK = Lock()


def _observe_text_work(event: str, width: int) -> None:
    observer = _TEXT_WORK_OBSERVER
    if observer is not None:
        observer(event, width)


def _validate_sha256(value: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError("source_sha256 must be a 64-character hexadecimal SHA-256 string")


def _require_int(value: object, name: str) -> None:
    if type(value) is not int:
        raise ValueError(f"{name} must be an integer")


def _require_str(value: object, name: str, *, nonempty: bool = False) -> None:
    if type(value) is not str or (nonempty and not value):
        suffix = " and must not be empty" if nonempty else ""
        raise ValueError(f"{name} must be a string{suffix}")


def source_sha256(text: str) -> str:
    """Return the SHA-256 digest of *text* encoded as UTF-8."""
    _require_str(text, "text")
    return sha256(text.encode("utf-8")).hexdigest()


class SourceUnitKind(StrEnum):
    NOTEBOOK_CELL = "notebook_cell"
    MODULE = "module"
    TEST_MODULE = "test_module"


class SourceArtifactKind(StrEnum):
    VISIBLE = "visible"
    WORKER_PROJECTION = "worker_projection"
    STATEMENT_PROJECTION = "statement_projection"
    SEMANTIC_LOWERING = "semantic_lowering"
    COLLECTOR_WRAPPER = "collector_wrapper"
    WORKER_MODULE = "worker_module"
    EXECUTED_BSL = "executed_bsl"


@dataclass(frozen=True, slots=True, order=True)
class SourceSpan:
    start: int
    end: int

    def __post_init__(self) -> None:
        _require_int(self.start, "span start")
        _require_int(self.end, "span end")
        if self.start < 0 or self.end < self.start:
            raise ValueError("span must be a nonnegative half-open interval")


@dataclass(frozen=True, slots=True)
class SourceUnitRef:
    kind: SourceUnitKind
    unit_id: str
    revision: int
    source_sha256: str

    def __post_init__(self) -> None:
        if type(self.kind) is not SourceUnitKind:
            raise ValueError("kind must be a SourceUnitKind")
        _require_str(self.unit_id, "unit_id", nonempty=True)
        _require_int(self.revision, "revision")
        _validate_sha256(self.source_sha256)
        if self.revision < 0:
            raise ValueError("revision must be nonnegative")


@dataclass(frozen=True, slots=True)
class SourceArtifactRef:
    kind: SourceArtifactKind
    source_sha256: str
    character_length: int
    line_ending_kind: str
    lowering_semantic_version: str | None = None
    wrapper_semantic_version: str | None = None
    mode: str | None = None
    worker_generation: int | None = None
    worker_manifest_sha256: str | None = None
    export_catalog_sha256: str | None = None

    def __post_init__(self) -> None:
        if type(self.kind) is not SourceArtifactKind:
            raise ValueError("kind must be a SourceArtifactKind")
        _require_int(self.character_length, "character_length")
        _require_str(self.line_ending_kind, "line_ending_kind")
        for name in (
            "lowering_semantic_version",
            "wrapper_semantic_version",
            "mode",
        ):
            value = getattr(self, name)
            if value is not None:
                _require_str(value, name)
        if self.worker_generation is not None:
            _require_int(self.worker_generation, "worker_generation")
        _validate_sha256(self.source_sha256)
        if self.character_length < 0:
            raise ValueError("character_length must be nonnegative")
        if self.worker_generation is not None and self.worker_generation < 0:
            raise ValueError("worker_generation must be nonnegative")
        for name in ("worker_manifest_sha256", "export_catalog_sha256"):
            value = getattr(self, name)
            if value is not None:
                _validate_sha256(value)


class MappingRelation(StrEnum):
    EXACT = "exact"
    DERIVED = "derived"
    SYNTHETIC = "synthetic"


@dataclass(frozen=True, slots=True)
class SourceMapSegment:
    generated: SourceSpan
    origin_ref: SourceUnitRef | SourceArtifactRef | None
    origin: SourceSpan | None
    relation: MappingRelation
    synthetic_region: str | None = None
    anchor_ref: SourceUnitRef | SourceArtifactRef | None = None
    anchor_span: SourceSpan | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.generated, SourceSpan):
            raise ValueError("generated must be a SourceSpan")
        if type(self.relation) is not MappingRelation:
            raise ValueError("relation must be a MappingRelation")
        if self.origin_ref is not None and not isinstance(
            self.origin_ref, (SourceUnitRef, SourceArtifactRef)
        ):
            raise ValueError("origin_ref must be a source reference")
        if self.origin is not None and not isinstance(self.origin, SourceSpan):
            raise ValueError("origin must be a SourceSpan")
        if (self.anchor_ref is None) != (self.anchor_span is None):
            raise ValueError("anchor reference and span must be provided together")
        if self.anchor_ref is not None and not isinstance(
            self.anchor_ref, (SourceUnitRef, SourceArtifactRef)
        ):
            raise ValueError("anchor_ref must be a source reference")
        if self.anchor_span is not None and not isinstance(self.anchor_span, SourceSpan):
            raise ValueError("anchor_span must be a SourceSpan")
        if self.synthetic_region is not None:
            _require_str(self.synthetic_region, "synthetic region", nonempty=True)

        if self.relation is MappingRelation.SYNTHETIC:
            if self.origin_ref is not None or self.origin is not None:
                raise ValueError("synthetic text must not claim an origin coordinate")
            if self.synthetic_region is None:
                raise ValueError("synthetic text requires a named region")
        else:
            if self.origin_ref is None or self.origin is None:
                raise ValueError("non-synthetic text requires an origin coordinate")
            if self.relation is MappingRelation.EXACT and (
                self.generated.end - self.generated.start != self.origin.end - self.origin.start
            ):
                raise ValueError("exact mapping must preserve one-to-one span lengths")


@dataclass(frozen=True, slots=True)
class MappedOffset:
    relation: MappingRelation
    unit: SourceUnitRef | None = None
    origin_span: SourceSpan | None = None
    synthetic_region: str | None = None
    anchor_unit: SourceUnitRef | None = None
    anchor_span: SourceSpan | None = None

    def __post_init__(self) -> None:
        if type(self.relation) is not MappingRelation:
            raise ValueError("relation must be a MappingRelation")
        if self.unit is not None and not isinstance(self.unit, SourceUnitRef):
            raise ValueError("mapped unit must be a SourceUnitRef")
        if self.origin_span is not None and not isinstance(self.origin_span, SourceSpan):
            raise ValueError("mapped origin must be a SourceSpan")
        if self.anchor_unit is not None and not isinstance(self.anchor_unit, SourceUnitRef):
            raise ValueError("mapped anchor unit must be a SourceUnitRef")
        if (self.anchor_unit is None) != (self.anchor_span is None):
            raise ValueError("mapped anchor unit and span must be provided together")


def _artifact_manifest(
    value: SourceArtifactRef,
    *,
    observe: bool = True,
) -> dict[str, object]:
    if observe:
        _observe_text_work("map_manifest_allocation", 1)
    return {
        "kind": value.kind.value,
        "source_sha256": value.source_sha256,
        "character_length": value.character_length,
        "line_ending_kind": value.line_ending_kind,
        "lowering_semantic_version": value.lowering_semantic_version,
        "wrapper_semantic_version": value.wrapper_semantic_version,
        "mode": value.mode,
        "worker_generation": value.worker_generation,
        "worker_manifest_sha256": value.worker_manifest_sha256,
        "export_catalog_sha256": value.export_catalog_sha256,
    }


def _reference_manifest(
    value: SourceUnitRef | SourceArtifactRef | None,
    *,
    observe: bool = True,
) -> dict[str, object] | None:
    if value is None:
        return None
    if isinstance(value, SourceArtifactRef):
        if observe:
            _observe_text_work("map_manifest_allocation", 1)
        return {
            "reference_kind": "artifact",
            **_artifact_manifest(value, observe=observe),
        }
    if observe:
        _observe_text_work("map_manifest_allocation", 1)
    return {
        "reference_kind": "unit",
        "kind": value.kind.value,
        "unit_id": value.unit_id,
        "revision": value.revision,
        "source_sha256": value.source_sha256,
    }


def _span_manifest(
    value: SourceSpan | None,
    *,
    observe: bool = True,
) -> list[int] | None:
    if value is not None and observe:
        _observe_text_work("map_manifest_allocation", 1)
    return None if value is None else [value.start, value.end]


def _proof_uint(mac: object, value: int) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("proof integer must be nonnegative")
    payload = value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")
    length = len(payload).to_bytes(8, "big", signed=False)
    mac.update(length)  # type: ignore[attr-defined]
    mac.update(payload)  # type: ignore[attr-defined]
    return len(length) + len(payload)


def _proof_optional_uint(mac: object, value: int | None) -> int:
    marker = b"\x00" if value is None else b"\x01"
    mac.update(marker)  # type: ignore[attr-defined]
    return 1 if value is None else 1 + _proof_uint(mac, value)


def _proof_text(mac: object, value: str) -> int:
    payload = value.encode("utf-8")
    width = len(payload).to_bytes(8, "big", signed=False)
    mac.update(width)  # type: ignore[attr-defined]
    mac.update(payload)  # type: ignore[attr-defined]
    return len(width) + len(payload)


def _proof_optional_text(mac: object, value: str | None) -> int:
    marker = b"\x00" if value is None else b"\x01"
    mac.update(marker)  # type: ignore[attr-defined]
    return 1 if value is None else 1 + _proof_text(mac, value)


def _proof_span(mac: object, value: SourceSpan | None) -> int:
    marker = b"\x00" if value is None else b"\x01"
    mac.update(marker)  # type: ignore[attr-defined]
    if value is None:
        return 1
    return 1 + _proof_uint(mac, value.start) + _proof_uint(mac, value.end)


def _proof_artifact(mac: object, value: SourceArtifactRef) -> int:
    width = _proof_text(mac, value.kind.value)
    width += _proof_text(mac, value.source_sha256)
    width += _proof_uint(mac, value.character_length)
    width += _proof_text(mac, value.line_ending_kind)
    width += _proof_optional_text(mac, value.lowering_semantic_version)
    width += _proof_optional_text(mac, value.wrapper_semantic_version)
    width += _proof_optional_text(mac, value.mode)
    width += _proof_optional_uint(mac, value.worker_generation)
    width += _proof_optional_text(mac, value.worker_manifest_sha256)
    width += _proof_optional_text(mac, value.export_catalog_sha256)
    return width


def _proof_reference(
    mac: object,
    value: SourceUnitRef | SourceArtifactRef | None,
) -> int:
    if value is None:
        mac.update(b"\x00")  # type: ignore[attr-defined]
        return 1
    if isinstance(value, SourceArtifactRef):
        mac.update(b"\x01")  # type: ignore[attr-defined]
        return 1 + _proof_artifact(mac, value)
    mac.update(b"\x02")  # type: ignore[attr-defined]
    width = 1 + _proof_text(mac, value.kind.value)
    width += _proof_text(mac, value.unit_id)
    width += _proof_uint(mac, value.revision)
    width += _proof_text(mac, value.source_sha256)
    return width


def _source_map_structural_proof(
    source_map: SourceMap,
    *,
    observe: bool,
) -> bytes:
    if observe:
        _observe_text_work(
            "semantic_structural_segment_visit",
            len(source_map.segments),
        )
    payload = _canonical_source_map_bytes(source_map, observe=False)
    return _source_map_structural_proof_from_canonical(payload)


def _source_map_structural_proof_from_canonical(payload: bytes) -> bytes:
    mac = hmac_new(_SOURCE_MAP_PROOF_KEY, digestmod="sha256")
    _observe_text_work("semantic_structural_hmac_update", 1)
    mac.update(payload)
    return mac.digest()


def _source_map_manifest(
    source_map: SourceMap,
    *,
    observe: bool,
) -> dict[str, object]:
    return {
        "generated": _artifact_manifest(source_map.generated, observe=observe),
        "segments": [
            {
                "generated": _span_manifest(segment.generated, observe=observe),
                "origin_ref": _reference_manifest(
                    segment.origin_ref,
                    observe=observe,
                ),
                "origin": _span_manifest(segment.origin, observe=observe),
                "relation": segment.relation.value,
                "synthetic_region": segment.synthetic_region,
                "anchor_ref": _reference_manifest(
                    segment.anchor_ref,
                    observe=observe,
                ),
                "anchor_span": _span_manifest(
                    segment.anchor_span,
                    observe=observe,
                ),
            }
            for segment in source_map.segments
        ],
    }


def _canonical_source_map_bytes(
    source_map: SourceMap,
    *,
    observe: bool,
) -> bytes:
    manifest = (
        source_map.to_manifest()
        if observe
        else _source_map_manifest(source_map, observe=False)
    )
    return json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class SourceMap:
    generated: SourceArtifactRef
    segments: tuple[SourceMapSegment, ...]
    _canonical_manifest_bytes: bytes = field(init=False, repr=False, compare=False)
    _canonical_sha256: str = field(init=False, repr=False, compare=False)
    _structural_proof: bytes = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.generated, SourceArtifactRef):
            raise ValueError("generated must be a SourceArtifactRef")
        if type(self.segments) is not tuple or not self.segments:
            raise ValueError("source map requires segments for full generated coverage")

        cursor = 0
        previous_start = -1
        for segment in self.segments:
            if not isinstance(segment, SourceMapSegment):
                raise ValueError("segments must contain SourceMapSegment values")
            if segment.generated.end > self.generated.character_length:
                raise ValueError("generated segment is out of bounds")
            if segment.generated.start < previous_start or segment.generated.start < cursor:
                raise ValueError("source map segments must be ordered and non-overlapping")
            if segment.generated.start > cursor:
                raise ValueError("source map must provide full generated coverage")
            previous_start = segment.generated.start
            cursor = max(cursor, segment.generated.end)
            for reference, span, name in (
                (segment.origin_ref, segment.origin, "origin"),
                (segment.anchor_ref, segment.anchor_span, "anchor"),
            ):
                if (
                    isinstance(reference, SourceArtifactRef)
                    and span is not None
                    and span.end > reference.character_length
                ):
                    raise ValueError(f"segment {name} is out of bounds")
        if cursor != self.generated.character_length:
            raise ValueError("source map must provide full generated coverage")
        _observe_text_work("map_manifest_segment_visit", len(self.segments))
        _observe_text_work("map_manifest_allocation", 2 + len(self.segments))
        payload = _canonical_source_map_bytes(self, observe=True)
        _observe_text_work("map_manifest_serialized_bytes", len(payload))
        _observe_text_work("map_manifest_hash", len(payload))
        object.__setattr__(self, "_canonical_manifest_bytes", payload)
        object.__setattr__(self, "_canonical_sha256", sha256(payload).hexdigest())
        object.__setattr__(
            self,
            "_structural_proof",
            _source_map_structural_proof_from_canonical(payload),
        )

    def map_offset(self, offset: int) -> MappedOffset:
        return map_offset(self, offset)

    def to_manifest(self) -> dict[str, object]:
        return _source_map_manifest(self, observe=True)

    @property
    def source_map_sha256(self) -> str:
        return self._canonical_sha256


def _validate_flattened_map(source_map: SourceMap, *, name: str) -> None:
    if getattr(source_map, "_compact_reload_map", False):
        return
    for segment in source_map.segments:
        if segment.relation is not MappingRelation.SYNTHETIC and not isinstance(
            segment.origin_ref, SourceUnitRef
        ):
            raise ValueError(f"{name} must reference visible source units")
        if segment.anchor_ref is not None and not isinstance(
            segment.anchor_ref, SourceUnitRef
        ):
            raise ValueError(f"{name} anchors must reference visible source units")


@dataclass(frozen=True, slots=True, repr=False)
class _TrustedTextProof:
    text_object_id: int
    source_sha256: str
    character_length: int


def _mint_trusted_text_proof(
    text: str,
    source_hash: str,
    *,
    authority: object,
) -> _TrustedTextProof:
    if authority is not _TRUSTED_TEXT_PROOF_AUTHORITY:
        raise ValueError("trusted text proof authority is invalid")
    proof = _TrustedTextProof(id(text), source_hash, len(text))
    with _TRUSTED_TEXT_PROOFS_LOCK:
        _TRUSTED_TEXT_PROOFS[id(proof)] = proof
    return proof


def _consume_trusted_text_proof(
    proof: object,
    text: str,
    artifact: SourceArtifactRef,
) -> bool:
    if not isinstance(proof, _TrustedTextProof):
        return False
    with _TRUSTED_TEXT_PROOFS_LOCK:
        registered = _TRUSTED_TEXT_PROOFS.pop(id(proof), None)
    return bool(
        registered is proof
        and proof.text_object_id == id(text)
        and proof.source_sha256 == artifact.source_sha256
        and proof.character_length == artifact.character_length
    )


@dataclass(frozen=True, slots=True, repr=False)
class _RetainedTextFragment:
    generated: SourceSpan
    text: str
    retained_source: object | None = None
    retained_span: SourceSpan | None = None
    line_endings: tuple[int, int, int, bool, bool] | None = None
    contains_nul: bool | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.generated, SourceSpan)
            or type(self.text) is not str
            or len(self.text) != self.generated.end - self.generated.start
            or (self.retained_source is None) != (self.retained_span is None)
        ):
            raise ValueError("retained text fragment is invalid")
        if self.line_endings is None:
            object.__setattr__(self, "line_endings", _line_ending_summary(self.text))
        elif self.line_endings != _line_ending_summary_from_counts(
            self.text,
            self.line_endings,
        ):
            raise ValueError("retained text fragment line-ending summary is invalid")
        if self.contains_nul is not None and type(self.contains_nul) is not bool:
            raise ValueError("retained text fragment NUL summary is invalid")
        _observe_text_work("fragment_allocate", 1)


def _line_ending_summary(text: str) -> tuple[int, int, int, bool, bool]:
    return (
        text.count("\r"),
        text.count("\n"),
        text.count("\r\n"),
        text.startswith("\n"),
        text.endswith("\r"),
    )


def _local_nul_summary(text: str) -> bool:
    _observe_text_work("fragment_nul_scan", len(text))
    return "\x00" in text


def _line_ending_summary_from_counts(
    text: str,
    summary: tuple[int, int, int, bool, bool],
) -> tuple[int, int, int, bool, bool]:
    if (
        type(summary) is not tuple
        or len(summary) != 5
        or any(type(value) is not int or value < 0 for value in summary[:3])
        or any(type(value) is not bool for value in summary[3:])
    ):
        raise ValueError("retained text fragment line-ending summary is invalid")
    # Immutable text identity is bound by the retained proof. Avoid rescanning
    # it here; only constant-time edge properties can be checked independently.
    if summary[3] != text.startswith("\n") or summary[4] != text.endswith("\r"):
        raise ValueError("retained text fragment line-ending summary is invalid")
    return summary


def _retained_state_proof(
    artifact: SourceArtifactRef,
    fragments: tuple[_RetainedTextFragment, ...],
) -> bytes:
    mac = hmac_new(_RETAINED_PROOF_KEY, digestmod="sha256")
    width = _proof_text(mac, artifact.source_sha256)
    width += _proof_uint(mac, artifact.character_length)
    width += _proof_uint(mac, len(fragments))
    for fragment in fragments:
        _observe_text_work("retained_proof_fragment_visit", 1)
        width += _proof_span(mac, fragment.generated)
        width += _proof_uint(mac, id(fragment.text))
        width += _proof_uint(mac, len(fragment.text))
        width += _proof_optional_uint(
            mac,
            None if fragment.retained_source is None else id(fragment.retained_source),
        )
        width += _proof_span(mac, fragment.retained_span)
        line_endings = fragment.line_endings
        assert line_endings is not None
        width += sum(_proof_uint(mac, value) for value in line_endings[:3])
        for value in line_endings[3:]:
            mac.update(b"\x01" if value else b"\x00")
            width += 1
        mac.update(
            b"\x00"
            if fragment.contains_nul is None
            else b"\x02"
            if fragment.contains_nul
            else b"\x01"
        )
        width += 1
    _observe_text_work("retained_proof_bytes", width)
    return mac.digest()


def _mapped_text_authority_proof(
    artifact: SourceArtifactRef,
    text: str,
) -> bytes:
    mac = hmac_new(_RETAINED_PROOF_KEY, digestmod="sha256")
    _proof_text(mac, artifact.source_sha256)
    _proof_uint(mac, artifact.character_length)
    _proof_uint(mac, id(text))
    _proof_uint(mac, len(text))
    return mac.digest()


def _validate_retained_fragments(
    artifact: SourceArtifactRef,
    fragments: object,
    proof: object,
    *,
    construction_authority: object | None = None,
) -> tuple[_RetainedTextFragment, ...]:
    _observe_text_work(
        "retained_metadata",
        len(fragments) if isinstance(fragments, tuple) else 0,
    )
    if (
        type(fragments) is not tuple
        or not fragments
        or any(not isinstance(item, _RetainedTextFragment) for item in fragments)
        or fragments[0].generated.start != 0
        or fragments[-1].generated.end != artifact.character_length
        or any(
            current.generated.end != following.generated.start
            for current, following in zip(fragments, fragments[1:], strict=False)
        )
        or not isinstance(proof, bytes)
        or (
            construction_authority is not _RETAINED_CONSTRUCTION_AUTHORITY
            and not compare_digest(proof, _retained_state_proof(artifact, fragments))
        )
    ):
        raise ValueError("retained fragment authority is invalid")
    return fragments


@dataclass(frozen=True, slots=True, weakref_slot=True, repr=False)
class _DeferredMappedSource:
    artifact: SourceArtifactRef
    source_map: SourceMap
    lineage: tuple[SourceMap, ...]
    local_source_map: SourceMap
    retained_fragments: tuple[_RetainedTextFragment, ...]
    retained_starts: tuple[int, ...]
    _retained_proof: bytes
    _construction_authority: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        retained = _validate_retained_fragments(
            self.artifact,
            self.retained_fragments,
            self._retained_proof,
            construction_authority=self._construction_authority,
        )
        if self._construction_authority is not _RETAINED_CONSTRUCTION_AUTHORITY:
            raise ValueError("retained fragment construction authority is invalid")
        if self.retained_starts != tuple(
            fragment.generated.start for fragment in retained
        ):
            raise ValueError("retained fragment index is invalid")

    @property
    def character_length(self) -> int:
        return self.artifact.character_length


def _detached_retained_source(
    source: _DeferredMappedSource,
) -> _DeferredMappedSource:
    fragments = tuple(
        _RetainedTextFragment(
            fragment.generated,
            fragment.text,
            line_endings=fragment.line_endings,
            contains_nul=fragment.contains_nul,
        )
        for fragment in source.retained_fragments
    )
    return _DeferredMappedSource(
        source.artifact,
        source.source_map,
        source.lineage,
        source.local_source_map,
        fragments,
        tuple(fragment.generated.start for fragment in fragments),
        _retained_state_proof(source.artifact, fragments),
        _RETAINED_CONSTRUCTION_AUTHORITY,
    )


def _validate_retained_source(
    source: MappedSource | _DeferredMappedSource,
) -> None:
    if isinstance(source, MappedSource) and not compare_digest(
        source._text_authority_proof,
        _mapped_text_authority_proof(source.artifact, source.text),
    ):
        raise ValueError("mapped text authority is invalid")
    retained = (
        source._retained_fragments
        if isinstance(source, MappedSource)
        else source.retained_fragments
    )
    starts = (
        source._retained_starts
        if isinstance(source, MappedSource)
        else source.retained_starts
    )
    _validate_retained_fragments(
        source.artifact,
        retained,
        source._retained_proof,
    )
    if starts != tuple(fragment.generated.start for fragment in retained):
        raise ValueError("retained fragment index is invalid")


def _validated_mapped_text_hash(source: MappedSource) -> str:
    if (
        not isinstance(source, MappedSource)
        or source.artifact.kind is not SourceArtifactKind.WORKER_MODULE
        or not isinstance(source._worker_text_proof, bytes)
        or not compare_digest(
            source._text_authority_proof,
            _mapped_text_authority_proof(source.artifact, source.text),
        )
        or not compare_digest(
            source._worker_text_proof,
            _normalized_worker_text_proof(source.artifact, source.text),
        )
    ):
        raise ValueError("mapped text authority is invalid")
    return source.artifact.source_sha256


def _retained_fragments_for_span(
    source: MappedSource | _DeferredMappedSource,
    span: SourceSpan,
    *,
    scan_local_nul: bool = True,
) -> tuple[_RetainedTextFragment, ...]:
    length = (
        len(source.text)
        if isinstance(source, MappedSource)
        else source.character_length
    )
    if not isinstance(span, SourceSpan) or span.end > length:
        raise ValueError("retained fragment span must be within the source")
    retained = (
        source._retained_fragments
        if isinstance(source, MappedSource)
        else source.retained_fragments
    )
    starts = (
        source._retained_starts
        if isinstance(source, MappedSource)
        else source.retained_starts
    )
    result: list[_RetainedTextFragment] = []
    cursor = span.start
    index = max(0, bisect_right(starts, span.start) - 1)
    while index < len(retained):
        fragment = retained[index]
        index += 1
        if fragment.generated.start >= span.end:
            break
        start = max(fragment.generated.start, span.start)
        end = min(fragment.generated.end, span.end)
        if start >= end:
            continue
        local_start = start - fragment.generated.start
        local_end = end - fragment.generated.start
        if local_start == 0 and local_end == len(fragment.text):
            text = fragment.text
            line_endings = fragment.line_endings
            contains_nul = fragment.contains_nul
        else:
            _observe_text_work("slice", local_end - local_start)
            text = fragment.text[local_start:local_end]
            line_endings = None
            contains_nul = (
                _local_nul_summary(text)
                if scan_local_nul
                else fragment.contains_nul
            )
        retained_span = (
            SourceSpan(start, end)
            if fragment.retained_source is None
            else SourceSpan(
                fragment.retained_span.start + local_start,  # type: ignore[union-attr]
                fragment.retained_span.start + local_end,  # type: ignore[union-attr]
            )
        )
        result.append(
            _RetainedTextFragment(
                SourceSpan(cursor, cursor + len(text)),
                text,
                source if fragment.retained_source is None else fragment.retained_source,
                retained_span,
                line_endings,
                contains_nul,
            )
        )
        cursor += len(text)
    if cursor != span.end:
        raise ValueError("retained fragments do not cover the requested span")
    return tuple(result)


@dataclass(frozen=True, slots=True, repr=False)
class _MappedSemanticSummary:
    source_map_sha256: str
    lineage_sha256: str
    source_map_object_id: int
    lineage_object_id: int
    local_source_map_object_id: int | None
    structural_proofs: tuple[tuple[int, bytes], ...]
    authority_proof: bytes


def _canonical_lineage_sha256(lineage: tuple[SourceMap, ...]) -> str:
    digest_value = sha256()
    digest_value.update(b"[")
    width = 2
    for index, source_map in enumerate(lineage):
        if index:
            digest_value.update(b",")
            width += 1
        payload = source_map._canonical_manifest_bytes
        digest_value.update(payload)
        width += len(payload)
        _observe_text_work("semantic_lineage_entry_visit", 1)
    digest_value.update(b"]")
    _observe_text_work("semantic_lineage_serialized_bytes", width)
    _observe_text_work("semantic_lineage_hash", width)
    return digest_value.hexdigest()


def _mapped_semantic_authority_proof(
    artifact: SourceArtifactRef,
    source_map: SourceMap,
    lineage: tuple[SourceMap, ...],
    local_source_map: SourceMap | None,
    source_map_digest: str,
    lineage_digest: str,
    structural_proofs: tuple[tuple[int, bytes], ...],
) -> bytes:
    mac = hmac_new(_SOURCE_MAP_PROOF_KEY, digestmod="sha256")
    _proof_artifact(mac, artifact)
    _proof_uint(mac, id(source_map))
    _proof_uint(mac, id(lineage))
    _proof_optional_uint(
        mac,
        None if local_source_map is None else id(local_source_map),
    )
    _proof_text(mac, source_map_digest)
    _proof_text(mac, lineage_digest)
    for object_id, proof in structural_proofs:
        _proof_uint(mac, object_id)
        _proof_uint(mac, len(proof))
        mac.update(proof)
    return mac.digest()


def _build_mapped_semantic_summary(
    artifact: SourceArtifactRef,
    source_map: SourceMap,
    lineage: tuple[SourceMap, ...],
    local_source_map: SourceMap | None,
) -> _MappedSemanticSummary:
    maps: list[SourceMap] = []
    for item in (source_map, *lineage, local_source_map):
        if item is not None and all(item is not existing for existing in maps):
            maps.append(item)
    structural_proofs = tuple(
        (id(item), item._structural_proof)
        for item in maps
    )
    source_map_digest = source_map.source_map_sha256
    lineage_digest = _canonical_lineage_sha256(lineage)
    authority_proof = _mapped_semantic_authority_proof(
        artifact,
        source_map,
        lineage,
        local_source_map,
        source_map_digest,
        lineage_digest,
        structural_proofs,
    )
    return _MappedSemanticSummary(
        source_map_digest,
        lineage_digest,
        id(source_map),
        id(lineage),
        None if local_source_map is None else id(local_source_map),
        structural_proofs,
        authority_proof,
    )


def _normalized_worker_text_proof(
    artifact: SourceArtifactRef,
    text: str,
) -> bytes:
    mac = hmac_new(_WORKER_TEXT_PROOF_KEY, digestmod="sha256")
    _proof_artifact(mac, artifact)
    _proof_uint(mac, id(text))
    _proof_uint(mac, len(text))
    return mac.digest()


class MappedSource:
    """A private source artifact whose printable state never includes its text."""

    __slots__ = (
        "__weakref__",
        "_artifact",
        "_lineage",
        "_local_source_map",
        "_semantic_summary",
        "_retained_fragments",
        "_retained_proof",
        "_retained_starts",
        "_source_map",
        "_text",
        "_text_authority_proof",
        "_transform_parent",
        "_worker_text_proof",
    )

    def __init__(
        self,
        text: str,
        artifact: SourceArtifactRef,
        source_map: SourceMap,
        lineage: tuple[SourceMap, ...] = (),
        local_source_map: SourceMap | None = None,
        *,
        _retained_fragments: tuple[_RetainedTextFragment, ...] | None = None,
        _transform_parent: MappedSource | _DeferredMappedSource | None = None,
        _trusted_source_sha256: str | None = None,
        _trusted_text_proof: _TrustedTextProof | None = None,
        _retained_authority: object | None = None,
        _normalized_worker_authority: object | None = None,
    ) -> None:
        _require_str(text, "mapped source text")
        if not isinstance(artifact, SourceArtifactRef):
            raise ValueError("mapped source artifact must be a SourceArtifactRef")
        if _trusted_source_sha256 is not None:
            raise ValueError("trusted source hash requires a builder-owned proof")
        text_is_trusted = _consume_trusted_text_proof(
            _trusted_text_proof,
            text,
            artifact,
        )
        if len(text) != artifact.character_length or (
            not text_is_trusted and source_sha256(text) != artifact.source_sha256
        ):
            raise ValueError("mapped source artifact hash or length does not match its text")
        if not isinstance(source_map, SourceMap) or source_map.generated != artifact:
            raise ValueError("mapped source map must identify the exact artifact")
        _validate_flattened_map(source_map, name="flattened source map")
        if type(lineage) is not tuple or any(not isinstance(item, SourceMap) for item in lineage):
            raise ValueError("mapped source lineage must contain source maps")
        if local_source_map is not None and local_source_map.generated != artifact:
            raise ValueError("local source map must identify the exact artifact")
        if local_source_map is not None and (not lineage or lineage[-1] != local_source_map):
            raise ValueError("local source map must be the latest lineage entry")
        if _retained_fragments is not None and (
            _retained_authority is not _RETAINED_CONSTRUCTION_AUTHORITY
            or not text_is_trusted
        ):
            raise ValueError("retained fragment construction authority is invalid")
        retained = (
            (
                _RetainedTextFragment(
                    SourceSpan(0, len(text)),
                    text,
                ),
            )
            if _retained_fragments is None
            else _retained_fragments
        )
        retained_proof = _retained_state_proof(artifact, retained)
        _validate_retained_fragments(
            artifact,
            retained,
            retained_proof,
            construction_authority=_RETAINED_CONSTRUCTION_AUTHORITY,
        )
        if _transform_parent is not None and not isinstance(
            _transform_parent,
            (MappedSource, _DeferredMappedSource),
        ):
            raise ValueError("mapped source transform parent is invalid")
        object.__setattr__(self, "_text", text)
        object.__setattr__(
            self,
            "_text_authority_proof",
            _mapped_text_authority_proof(artifact, text),
        )
        object.__setattr__(self, "_artifact", artifact)
        object.__setattr__(self, "_source_map", source_map)
        object.__setattr__(self, "_lineage", lineage)
        object.__setattr__(self, "_local_source_map", local_source_map)
        object.__setattr__(
            self,
            "_semantic_summary",
            _build_mapped_semantic_summary(
                artifact,
                source_map,
                lineage,
                local_source_map,
            ),
        )
        object.__setattr__(self, "_retained_fragments", retained)
        object.__setattr__(self, "_retained_starts", tuple(
            fragment.generated.start for fragment in retained
        ))
        object.__setattr__(self, "_retained_proof", retained_proof)
        object.__setattr__(self, "_transform_parent", _transform_parent)
        if _normalized_worker_authority is not None and (
            _normalized_worker_authority is not _NORMALIZED_WORKER_AUTHORITY
            or artifact.kind is not SourceArtifactKind.WORKER_MODULE
        ):
            raise ValueError("normalized Worker authority is invalid")
        object.__setattr__(
            self,
            "_worker_text_proof",
            None
            if _normalized_worker_authority is None
            else _normalized_worker_text_proof(artifact, text),
        )

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("MappedSource is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("MappedSource is immutable")

    @property
    def text(self) -> str:
        return self._text

    @property
    def artifact(self) -> SourceArtifactRef:
        return self._artifact

    @property
    def source_map(self) -> SourceMap:
        return self._source_map

    @property
    def lineage(self) -> tuple[SourceMap, ...]:
        return self._lineage

    @property
    def local_source_map(self) -> SourceMap:
        if self._local_source_map is None:
            raise ValueError("visible or externally assembled source has no local source map")
        return self._local_source_map

    @property
    def transform_parent(self) -> MappedSource | _DeferredMappedSource | None:
        return self._transform_parent

    @property
    def source_map_sha256(self) -> str:
        return self.source_map.source_map_sha256

    def lineage_manifest(self) -> tuple[dict[str, object], ...]:
        return tuple(source_map.to_manifest() for source_map in self.lineage)

    def __repr__(self) -> str:
        segment_count = (
            getattr(self.source_map, "compact_interval_count")
            if getattr(self.source_map, "_compact_reload_map", False)
            else len(self.source_map.segments)
        )
        return (
            "MappedSource(text=<redacted>, artifact="
            f"{self.artifact.kind.value}:{self.artifact.source_sha256}, "
            f"segments={segment_count}, lineage={len(self.lineage)})"
        )

    def __deepcopy__(self, memo: dict[int, object]) -> MappedSource:
        return self


def _mapped_semantic_summary(
    source: MappedSource,
    *,
    validate_structure: bool,
) -> _MappedSemanticSummary:
    if not isinstance(source, MappedSource):
        raise ValueError("mapped semantic authority is invalid")
    summary = source._semantic_summary
    maps: list[SourceMap] = []
    for item in (source.source_map, *source.lineage, source._local_source_map):
        if item is not None and all(item is not existing for existing in maps):
            maps.append(item)
    current_proofs = tuple(
        (id(item), item._structural_proof)
        for item in maps
    )
    expected_authority = _mapped_semantic_authority_proof(
        source.artifact,
        source.source_map,
        source.lineage,
        source._local_source_map,
        summary.source_map_sha256,
        summary.lineage_sha256,
        summary.structural_proofs,
    )
    if (
        summary.source_map_object_id != id(source.source_map)
        or summary.lineage_object_id != id(source.lineage)
        or summary.local_source_map_object_id
        != (None if source._local_source_map is None else id(source._local_source_map))
        or summary.structural_proofs != current_proofs
        or not compare_digest(summary.authority_proof, expected_authority)
    ):
        raise ValueError("mapped semantic authority is invalid")
    if validate_structure:
        for source_map, (_, expected_proof) in zip(
            maps,
            summary.structural_proofs,
            strict=True,
        ):
            compact_validator = getattr(
                source_map,
                "_validated_compact_structural_proof",
                None,
            )
            actual_proof = (
                compact_validator()
                if compact_validator is not None
                else _source_map_structural_proof(source_map, observe=True)
            )
            if not compare_digest(
                expected_proof,
                actual_proof,
            ):
                raise ValueError("mapped semantic authority is invalid")
    _observe_text_work("semantic_summary_reuse", 1)
    return summary


def _trusted_mapped_semantic_summary(source: MappedSource) -> _MappedSemanticSummary:
    return _mapped_semantic_summary(source, validate_structure=False)


def _validated_mapped_semantic_summary(source: MappedSource) -> _MappedSemanticSummary:
    return _mapped_semantic_summary(source, validate_structure=True)


def _mapped_segment(segment: SourceMapSegment, offset: int, *, eof: bool = False) -> MappedOffset:
    if segment.relation is MappingRelation.SYNTHETIC:
        anchor_unit = segment.anchor_ref if isinstance(segment.anchor_ref, SourceUnitRef) else None
        return MappedOffset(
            MappingRelation.SYNTHETIC,
            synthetic_region=segment.synthetic_region,
            anchor_unit=anchor_unit,
            anchor_span=segment.anchor_span if anchor_unit is not None else None,
        )

    unit = segment.origin_ref if isinstance(segment.origin_ref, SourceUnitRef) else None
    if unit is None or segment.origin is None:
        raise ValueError("flattened source map contains an intermediate artifact reference")
    anchor_unit = (
        segment.anchor_ref
        if isinstance(segment.anchor_ref, SourceUnitRef)
        else None
    )
    anchor_span = segment.anchor_span if anchor_unit is not None else None
    if segment.relation is MappingRelation.DERIVED:
        return MappedOffset(
            MappingRelation.DERIVED,
            unit,
            segment.origin,
            anchor_unit=anchor_unit,
            anchor_span=anchor_span,
        )
    delta = offset - segment.generated.start
    origin_offset = segment.origin.start + delta
    width = 0 if eof or segment.generated.start == segment.generated.end else 1
    return MappedOffset(
        MappingRelation.EXACT,
        unit,
        SourceSpan(origin_offset, origin_offset + width),
        anchor_unit=anchor_unit,
        anchor_span=anchor_span,
    )


def map_offset(source_map: SourceMap, offset: int) -> MappedOffset:
    if not isinstance(source_map, SourceMap):
        raise ValueError("source_map must be a SourceMap")
    if type(offset) is not int or not 0 <= offset <= source_map.generated.character_length:
        raise ValueError("offset must be within the generated source")

    positive = tuple(
        segment
        for segment in source_map.segments
        if segment.generated.start < segment.generated.end
    )
    if offset == source_map.generated.character_length:
        if positive:
            return _mapped_segment(positive[-1], offset, eof=True)
        exact_boundaries = tuple(
            segment
            for segment in source_map.segments
            if segment.relation is MappingRelation.EXACT
            and segment.origin is not None
            and segment.origin.start == segment.origin.end
        )
        if len(exact_boundaries) == 1:
            return _mapped_segment(exact_boundaries[0], offset, eof=True)
        raise ValueError("offset has no mappable position in the generated source")

    starts = tuple(segment.generated.start for segment in positive)
    index = bisect_right(starts, offset) - 1
    if index < 0 or offset >= positive[index].generated.end:
        raise ValueError("offset has no generated source mapping")
    return _mapped_segment(positive[index], offset)


_RELATION_COMPOSITION: dict[tuple[MappingRelation, MappingRelation], MappingRelation] = {
    (MappingRelation.EXACT, MappingRelation.EXACT): MappingRelation.EXACT,
    (MappingRelation.EXACT, MappingRelation.DERIVED): MappingRelation.DERIVED,
    (MappingRelation.EXACT, MappingRelation.SYNTHETIC): MappingRelation.SYNTHETIC,
    (MappingRelation.DERIVED, MappingRelation.EXACT): MappingRelation.DERIVED,
    (MappingRelation.DERIVED, MappingRelation.DERIVED): MappingRelation.DERIVED,
    (MappingRelation.DERIVED, MappingRelation.SYNTHETIC): MappingRelation.SYNTHETIC,
    (MappingRelation.SYNTHETIC, MappingRelation.EXACT): MappingRelation.SYNTHETIC,
    (MappingRelation.SYNTHETIC, MappingRelation.DERIVED): MappingRelation.SYNTHETIC,
    (MappingRelation.SYNTHETIC, MappingRelation.SYNTHETIC): MappingRelation.SYNTHETIC,
}


def _overlaps(segment: SourceMapSegment, span: SourceSpan) -> bool:
    if segment.generated.start == segment.generated.end:
        return span.start <= segment.generated.start <= span.end
    return segment.generated.start < span.end and span.start < segment.generated.end


def _project_parent_piece(
    local: SourceMapSegment,
    parent: SourceMapSegment,
    generated: SourceSpan,
    parent_piece: SourceSpan,
    local_anchor: tuple[SourceUnitRef | None, SourceSpan | None],
) -> SourceMapSegment:
    relation = _RELATION_COMPOSITION[(local.relation, parent.relation)]
    if relation is MappingRelation.SYNTHETIC:
        region = (
            parent.synthetic_region
            if parent.relation is MappingRelation.SYNTHETIC
            else local.synthetic_region
        )
        anchor_ref = parent.anchor_ref
        anchor_span = parent.anchor_span
        return SourceMapSegment(
            generated,
            None,
            None,
            relation,
            region or "composed_synthetic",
            anchor_ref,
            anchor_span,
        )

    assert parent.origin is not None
    if parent.relation is MappingRelation.EXACT:
        delta_start = parent_piece.start - parent.generated.start
        delta_end = parent_piece.end - parent.generated.start
        origin = SourceSpan(parent.origin.start + delta_start, parent.origin.start + delta_end)
    else:
        origin = parent.origin
    anchor_ref, anchor_span = local_anchor
    if anchor_ref is None and isinstance(parent.anchor_ref, SourceUnitRef):
        anchor_ref = parent.anchor_ref
        anchor_span = parent.anchor_span
    region = (
        local.synthetic_region
        if local.relation is MappingRelation.DERIVED
        else parent.synthetic_region
        if parent.relation is MappingRelation.DERIVED
        else None
    )
    return SourceMapSegment(
        generated,
        parent.origin_ref,
        origin,
        relation,
        region if relation is MappingRelation.DERIVED else None,
        anchor_ref,
        anchor_span,
    )


def _anchor_for_parent_span(
    parent_map: SourceMap, span: SourceSpan
) -> tuple[SourceUnitRef | None, SourceSpan | None]:
    if span.start == span.end:
        mapped = map_offset(parent_map, span.start)
        if mapped.unit is not None and mapped.origin_span is not None:
            point = mapped.origin_span.start
            return mapped.unit, SourceSpan(point, point)
        return mapped.anchor_unit, mapped.anchor_span

    candidates: list[tuple[SourceUnitRef, SourceSpan]] = []
    for segment in parent_map.segments:
        if not _overlaps(segment, span) or segment.generated.start == segment.generated.end:
            continue
        intersection = SourceSpan(
            max(segment.generated.start, span.start),
            min(segment.generated.end, span.end),
        )
        if segment.relation is MappingRelation.EXACT and isinstance(
            segment.origin_ref, SourceUnitRef
        ):
            assert segment.origin is not None
            start = segment.origin.start + intersection.start - segment.generated.start
            end = segment.origin.start + intersection.end - segment.generated.start
            candidates.append((segment.origin_ref, SourceSpan(start, end)))
        elif segment.relation is MappingRelation.DERIVED and isinstance(
            segment.origin_ref, SourceUnitRef
        ):
            assert segment.origin is not None
            candidates.append((segment.origin_ref, segment.origin))
        elif isinstance(segment.anchor_ref, SourceUnitRef) and segment.anchor_span is not None:
            candidates.append((segment.anchor_ref, segment.anchor_span))
    if not candidates or any(unit != candidates[0][0] for unit, _ in candidates[1:]):
        return None, None
    return candidates[0][0], SourceSpan(
        min(candidate.start for _, candidate in candidates),
        max(candidate.end for _, candidate in candidates),
    )


def _compose_segment(
    local: SourceMapSegment, parent_map: SourceMap
) -> list[SourceMapSegment]:
    if local.relation is MappingRelation.SYNTHETIC:
        anchor_ref: SourceUnitRef | None = None
        anchor_span: SourceSpan | None = None
        if local.anchor_span is not None:
            anchor_ref, anchor_span = _anchor_for_parent_span(parent_map, local.anchor_span)
        return [
            SourceMapSegment(
                local.generated,
                None,
                None,
                MappingRelation.SYNTHETIC,
                local.synthetic_region,
                anchor_ref,
                anchor_span,
            )
        ]

    assert local.origin is not None
    local_anchor: tuple[SourceUnitRef | None, SourceSpan | None] = (None, None)
    if local.anchor_span is not None:
        local_anchor = _anchor_for_parent_span(parent_map, local.anchor_span)
    local_generated_length = local.generated.end - local.generated.start
    local_origin_length = local.origin.end - local.origin.start
    if local_generated_length != local_origin_length:
        positive_parents = [
            segment
            for segment in parent_map.segments
            if segment.generated.start < segment.generated.end
            and _overlaps(segment, local.origin)
        ]
        if local.origin.start < local.origin.end and len(positive_parents) != 1:
            raise ValueError(
                "unequal-width derived fragment crosses multiple parent segments"
            )
        unit, origin = _anchor_for_parent_span(parent_map, local.origin)
        synthetic_parent = next(
            (
                segment
                for segment in positive_parents
                if segment.relation is MappingRelation.SYNTHETIC
            ),
            None,
        )
        if synthetic_parent is not None:
            anchor_ref = (
                synthetic_parent.anchor_ref
                if isinstance(synthetic_parent.anchor_ref, SourceUnitRef)
                else local_anchor[0] or unit
            )
            anchor_span = (
                synthetic_parent.anchor_span
                if anchor_ref == synthetic_parent.anchor_ref
                else local_anchor[1] or origin
            )
            return [
                SourceMapSegment(
                    local.generated,
                    None,
                    None,
                    MappingRelation.SYNTHETIC,
                    synthetic_parent.synthetic_region
                    or local.synthetic_region
                    or "composed_synthetic",
                    anchor_ref,
                    anchor_span,
                )
            ]
        if unit is None or origin is None:
            raise ValueError("derived fragment crosses multiple parent origins")
        anchor_ref, anchor_span = local_anchor
        if anchor_ref is None and len(positive_parents) == 1 and isinstance(
            positive_parents[0].anchor_ref, SourceUnitRef
        ):
            anchor_ref = positive_parents[0].anchor_ref
            anchor_span = positive_parents[0].anchor_span
        return [
            SourceMapSegment(
                local.generated,
                unit,
                origin,
                MappingRelation.DERIVED,
                local.synthetic_region,
                anchor_ref,
                anchor_span,
            )
        ]

    composed: list[SourceMapSegment] = []
    for parent in parent_map.segments:
        if not _overlaps(parent, local.origin):
            continue
        if parent.generated.start == parent.generated.end:
            parent_piece = parent.generated
        else:
            parent_piece = SourceSpan(
                max(parent.generated.start, local.origin.start),
                min(parent.generated.end, local.origin.end),
            )
        generated = SourceSpan(
            local.generated.start + parent_piece.start - local.origin.start,
            local.generated.start + parent_piece.end - local.origin.start,
        )
        composed.append(
            _project_parent_piece(
                local,
                parent,
                generated,
                parent_piece,
                local_anchor,
            )
        )
    if not composed:
        raise ValueError("local segment has no parent mapping")
    return composed


def _coalesce_segments(segments: list[SourceMapSegment]) -> tuple[SourceMapSegment, ...]:
    result: list[SourceMapSegment] = []
    for segment in segments:
        if not result:
            result.append(segment)
            continue
        previous = result[-1]
        positive = (
            previous.generated.start < previous.generated.end
            and segment.generated.start < segment.generated.end
        )
        common = (
            positive
            and previous.generated.end == segment.generated.start
            and previous.relation is segment.relation
            and previous.origin_ref == segment.origin_ref
            and previous.synthetic_region == segment.synthetic_region
            and previous.anchor_ref == segment.anchor_ref
            and previous.anchor_span == segment.anchor_span
        )
        origins_join = (
            previous.origin is None
            and segment.origin is None
            or previous.origin is not None
            and segment.origin is not None
            and previous.origin.end == segment.origin.start
        )
        if common and origins_join:
            origin = (
                None
                if previous.origin is None
                else SourceSpan(
                    previous.origin.start,
                    segment.origin.end,  # type: ignore[union-attr]
                )
            )
            result[-1] = SourceMapSegment(
                SourceSpan(previous.generated.start, segment.generated.end),
                previous.origin_ref,
                origin,
                previous.relation,
                previous.synthetic_region,
                previous.anchor_ref,
                previous.anchor_span,
            )
        else:
            result.append(segment)
    return tuple(result)


def compose_source_maps(local: SourceMap, parent: SourceMap) -> SourceMap:
    if not isinstance(local, SourceMap) or not isinstance(parent, SourceMap):
        raise ValueError("composition requires SourceMap values")
    _validate_flattened_map(parent, name="flattened parent map")
    for segment in local.segments:
        for reference in (segment.origin_ref, segment.anchor_ref):
            if isinstance(reference, SourceArtifactRef) and reference != parent.generated:
                raise ValueError("local map does not reference the exact parent artifact")
        if segment.relation is not MappingRelation.SYNTHETIC and not isinstance(
            segment.origin_ref, SourceArtifactRef
        ):
            raise ValueError("local map origin must reference its parent artifact")
        if segment.anchor_ref is not None and not isinstance(segment.anchor_ref, SourceArtifactRef):
            raise ValueError("local map anchor must reference its parent artifact")

    composed = [
        projected
        for segment in local.segments
        for projected in _compose_segment(segment, parent)
    ]
    result = SourceMap(local.generated, _coalesce_segments(composed))
    _validate_flattened_map(result, name="composed flattened source map")
    return result


def _compose_reused_source_maps(local: SourceMap, parent: SourceMap) -> SourceMap:
    """Compose monotonic imported fragments in one pass, else use the general path."""
    if (
        any(
            segment.generated.start == segment.generated.end
            for segment in parent.segments
        )
        or any(
            segment.relation is MappingRelation.SYNTHETIC
            or segment.anchor_ref is not None
            or segment.origin is None
            or segment.origin.start == segment.origin.end
            for segment in local.segments
        )
        or any(
            current.origin is None
            or following.origin is None
            or current.origin.start > following.origin.start
            for current, following in zip(
                local.segments,
                local.segments[1:],
                strict=False,
            )
        )
    ):
        return compose_source_maps(local, parent)
    _validate_flattened_map(parent, name="flattened parent map")
    for segment in local.segments:
        if (
            segment.origin_ref != parent.generated
            or segment.anchor_ref is not None
        ):
            return compose_source_maps(local, parent)

    positive = tuple(
        segment
        for segment in parent.segments
        if segment.generated.start < segment.generated.end
    )
    composed: list[SourceMapSegment] = []
    parent_index = 0
    for segment in local.segments:
        assert segment.origin is not None
        while (
            parent_index < len(positive)
            and positive[parent_index].generated.end <= segment.origin.start
        ):
            parent_index += 1
        overlapping: list[SourceMapSegment] = []
        scan = parent_index
        while (
            scan < len(positive)
            and positive[scan].generated.start < segment.origin.end
        ):
            if _overlaps(positive[scan], segment.origin):
                overlapping.append(positive[scan])
            scan += 1
        generated_width = segment.generated.end - segment.generated.start
        origin_width = segment.origin.end - segment.origin.start
        if generated_width != origin_width:
            if len(overlapping) != 1:
                return compose_source_maps(local, parent)
            parent_segment = overlapping[0]
            if parent_segment.relation is MappingRelation.SYNTHETIC:
                composed.append(
                    SourceMapSegment(
                        segment.generated,
                        None,
                        None,
                        MappingRelation.SYNTHETIC,
                        parent_segment.synthetic_region
                        or segment.synthetic_region
                        or "composed_synthetic",
                        parent_segment.anchor_ref,
                        parent_segment.anchor_span,
                    )
                )
                continue
            assert parent_segment.origin is not None
            origin = (
                SourceSpan(
                    parent_segment.origin.start
                    + segment.origin.start
                    - parent_segment.generated.start,
                    parent_segment.origin.start
                    + segment.origin.end
                    - parent_segment.generated.start,
                )
                if parent_segment.relation is MappingRelation.EXACT
                else parent_segment.origin
            )
            composed.append(
                SourceMapSegment(
                    segment.generated,
                    parent_segment.origin_ref,
                    origin,
                    MappingRelation.DERIVED,
                    segment.synthetic_region,
                    parent_segment.anchor_ref,
                    parent_segment.anchor_span,
                )
            )
            continue
        if not overlapping:
            return compose_source_maps(local, parent)
        for parent_segment in overlapping:
            parent_piece = SourceSpan(
                max(parent_segment.generated.start, segment.origin.start),
                min(parent_segment.generated.end, segment.origin.end),
            )
            generated = SourceSpan(
                segment.generated.start
                + parent_piece.start
                - segment.origin.start,
                segment.generated.start
                + parent_piece.end
                - segment.origin.start,
            )
            composed.append(
                _project_parent_piece(
                    segment,
                    parent_segment,
                    generated,
                    parent_piece,
                    (None, None),
                )
            )
    result = SourceMap(local.generated, _coalesce_segments(composed))
    _validate_flattened_map(result, name="composed flattened source map")
    return result


def _line_ending_kind(text: str) -> str:
    crlf_count = text.count("\r\n")
    bare_lf_count = text.count("\n") - crlf_count
    bare_cr_count = text.count("\r") - crlf_count
    kinds = sum(count > 0 for count in (crlf_count, bare_lf_count, bare_cr_count))
    if kinds > 1:
        return "mixed"
    if crlf_count:
        return "crlf"
    if bare_lf_count:
        return "lf"
    if bare_cr_count:
        return "cr"
    return "none"


def _line_ending_kind_fragments(
    fragments: list[_RetainedTextFragment],
) -> str:
    """Classify line endings without materializing retained fragments."""
    cr_count = 0
    lf_count = 0
    crlf_count = 0
    previous_ended_cr = False
    for fragment in fragments:
        summary = fragment.line_endings
        assert summary is not None
        cr_count += summary[0]
        lf_count += summary[1]
        crlf_count += summary[2]
        if previous_ended_cr and summary[3]:
            crlf_count += 1
        if fragment.text:
            previous_ended_cr = summary[4]
    bare_cr_count = cr_count - crlf_count
    bare_lf_count = lf_count - crlf_count
    kinds = sum(count > 0 for count in (crlf_count, bare_lf_count, bare_cr_count))
    if kinds > 1:
        return "mixed"
    if crlf_count:
        return "crlf"
    if bare_lf_count:
        return "lf"
    if bare_cr_count:
        return "cr"
    return "none"


def mapped_visible_source(text: str, unit: SourceUnitRef) -> MappedSource:
    _require_str(text, "visible source")
    if not isinstance(unit, SourceUnitRef):
        raise ValueError("visible source unit must be a SourceUnitRef")
    if source_sha256(text) != unit.source_sha256:
        raise ValueError("visible source hash does not match its source unit")
    artifact = SourceArtifactRef(
        SourceArtifactKind.VISIBLE,
        unit.source_sha256,
        len(text),
        _line_ending_kind(text),
    )
    segment = SourceMapSegment(
        SourceSpan(0, len(text)),
        unit,
        SourceSpan(0, len(text)),
        MappingRelation.EXACT,
    )
    source_map = SourceMap(artifact, (segment,))
    return MappedSource(text, artifact, source_map)


def compose_mapped_sources(
    sources: tuple[MappedSource, ...],
    *,
    visible_sources: tuple[MappedSource, ...],
    kind: SourceArtifactKind,
) -> MappedSource:
    """Join complete mapped fragments with checked, multi-origin provenance.

    Visible originals fence hashes, bounds, and the bytes of exact mappings.
    Newlines are synthetic and anchored to the following fragment. The result
    retains no prior assembled artifact or generation through transform parents.
    """
    if type(sources) is not tuple or type(visible_sources) is not tuple:
        raise ValueError("mapped composition requires immutable source tuples")
    visible: dict[SourceUnitRef, str] = {}
    identities: dict[tuple[SourceUnitKind, str, int], SourceUnitRef] = {}
    for source in visible_sources:
        _validated_mapped_semantic_summary(source)
        unit = source.source_map.map_offset(0).unit
        if (
            source.artifact.kind is not SourceArtifactKind.VISIBLE
            or unit is None
            or source_sha256(source.text) != unit.source_sha256
            or source.artifact.source_sha256 != unit.source_sha256
            or source.artifact.character_length != len(source.text)
            or source.source_map.segments != (
                SourceMapSegment(
                    SourceSpan(0, len(source.text)),
                    unit,
                    SourceSpan(0, len(source.text)),
                    MappingRelation.EXACT,
                ),
            )
        ):
            raise ValueError("mapped composition requires hash-matched visible sources")
        key = (unit.kind, unit.unit_id, unit.revision)
        if key in identities and identities[key] != unit:
            raise ValueError("mapped composition has conflicting source identities")
        identities[key] = unit
        visible[unit] = source.text

    parts: list[str] = []
    segments: list[SourceMapSegment] = []
    local_segments: list[SourceMapSegment] = []
    lineage: list[SourceMap] = []
    cursor = 0
    for source in sources:
        _validated_mapped_semantic_summary(source)
        if (
            len(source.text) != source.artifact.character_length
            or source_sha256(source.text) != source.artifact.source_sha256
        ):
            raise ValueError("mapped composition fragment hash or length does not match")
        for segment in source.source_map.segments:
            for reference, span in (
                (segment.origin_ref, segment.origin),
                (segment.anchor_ref, segment.anchor_span),
            ):
                if reference is not None and (
                    reference not in visible
                    or span is None
                    or span.end > len(visible[reference])
                ):
                    raise ValueError("mapped composition origin is not covered by visible sources")
            if segment.relation is MappingRelation.EXACT:
                assert segment.origin_ref is not None and segment.origin is not None
                if (
                    source.text[segment.generated.start:segment.generated.end]
                    != visible[segment.origin_ref][segment.origin.start:segment.origin.end]
                ):
                    raise ValueError("mapped composition exact origin text does not match")
        if parts:
            anchor = source.source_map.map_offset(0)
            join = SourceMapSegment(
                SourceSpan(cursor, cursor + 1),
                None,
                None,
                MappingRelation.SYNTHETIC,
                "notebook_method_join",
                anchor.unit or anchor.anchor_unit,
                anchor.origin_span or anchor.anchor_span,
            )
            parts.append("\n")
            segments.append(join)
            local_segments.append(join)
            cursor += 1
        parts.append(source.text)
        for segment in source.source_map.segments:
            segments.append(SourceMapSegment(
                SourceSpan(cursor + segment.generated.start, cursor + segment.generated.end),
                segment.origin_ref,
                segment.origin,
                segment.relation,
                segment.synthetic_region,
                segment.anchor_ref,
                segment.anchor_span,
            ))
        local_segments.append(SourceMapSegment(
            SourceSpan(cursor, cursor + len(source.text)),
            source.artifact,
            SourceSpan(0, len(source.text)),
            MappingRelation.EXACT,
        ))
        lineage.extend(source.lineage)
        cursor += len(source.text)
    text = "".join(parts)
    artifact = SourceArtifactRef(kind, source_sha256(text), len(text), _line_ending_kind(text))
    if not segments:
        empty = SourceMapSegment(
            SourceSpan(0, 0), None, None, MappingRelation.SYNTHETIC, "empty_notebook_methods"
        )
        segments.append(empty)
        local_segments.append(empty)
    flattened = SourceMap(artifact, _coalesce_segments(segments))
    local = SourceMap(artifact, _coalesce_segments(local_segments))
    return MappedSource(text, artifact, flattened, (*lineage, local), local)


_ARTIFACT_IDENTITY_FIELDS = {
    "lowering_semantic_version",
    "wrapper_semantic_version",
    "mode",
    "worker_generation",
    "worker_manifest_sha256",
    "export_catalog_sha256",
}


class SourceTransformBuilder:
    def __init__(
        self,
        parent: MappedSource | _DeferredMappedSource,
        *,
        _normalized_parent_authority: object | None = None,
    ) -> None:
        if not isinstance(parent, (MappedSource, _DeferredMappedSource)):
            raise ValueError("transform parent must be a mapped source")
        if (
            _normalized_parent_authority is not None
            and _normalized_parent_authority is not _NORMALIZED_WORKER_AUTHORITY
        ):
            raise ValueError("normalized parent authority is invalid")
        self._parent = parent
        self._normalized_parent = _normalized_parent_authority is not None
        self._parts: list[_RetainedTextFragment] = []
        self._segments: list[SourceMapSegment] = []
        self._cursor = 0
        self._validated_retained_sources: set[int] = set()

    def _validate_parent_span(self, span: SourceSpan) -> None:
        length = (
            len(self._parent.text)
            if isinstance(self._parent, MappedSource)
            else self._parent.character_length
        )
        if not isinstance(span, SourceSpan) or span.end > length:
            raise ValueError("fragment span must be within the parent source")

    def _validate_normalized_worker_policy(
        self,
        kind: SourceArtifactKind,
        authority: object | None,
    ) -> None:
        if authority is None:
            return
        if (
            authority is not _NORMALIZED_WORKER_AUTHORITY
            or kind is not SourceArtifactKind.WORKER_MODULE
        ):
            raise ValueError("normalized Worker authority is invalid")
        for fragment in self._parts:
            _observe_text_work("worker_policy_fragment_visit", 1)
            if fragment.contains_nul is None:
                raise ValueError("normalized Worker fragment authority is incomplete")
            if fragment.contains_nul:
                raise ValueError("Worker source contains a NUL character")
        _observe_text_work("normalized_worker_authority", 1)

    def _append(
        self,
        text: str,
        segment: SourceMapSegment,
        *,
        retained_source: object | None = None,
        retained_span: SourceSpan | None = None,
    ) -> None:
        _observe_text_work("segment_allocate", 1)
        self._parts.append(
            _RetainedTextFragment(
                SourceSpan(self._cursor, self._cursor + len(text)),
                text,
                retained_source,
                retained_span,
                contains_nul=_local_nul_summary(text),
            )
        )
        self._segments.append(segment)
        self._cursor += len(text)

    def _append_retained(
        self,
        retained_source: MappedSource | _DeferredMappedSource,
        retained_span: SourceSpan,
        segment: SourceMapSegment,
    ) -> None:
        _observe_text_work("segment_allocate", 1)
        source_identity = id(retained_source)
        if source_identity not in self._validated_retained_sources:
            _validate_retained_source(retained_source)
            self._validated_retained_sources.add(source_identity)
        trusted_normalized_parent = (
            self._normalized_parent and retained_source is self._parent
        )
        fragments = _retained_fragments_for_span(
            retained_source,
            retained_span,
            scan_local_nul=not trusted_normalized_parent,
        )
        retained_cursor = retained_span.start
        for fragment in fragments:
            width = len(fragment.text)
            self._parts.append(
                _RetainedTextFragment(
                    SourceSpan(self._cursor, self._cursor + width),
                    fragment.text,
                    retained_source,
                    SourceSpan(retained_cursor, retained_cursor + width),
                    fragment.line_endings,
                    (
                        False
                        if trusted_normalized_parent
                        and fragment.contains_nul is None
                        else fragment.contains_nul
                    ),
                )
            )
            self._cursor += width
            retained_cursor += width
        _observe_text_work("retained_fragment", retained_span.end - retained_span.start)
        self._segments.append(segment)

    def _exact_segment(self, span: SourceSpan, width: int) -> SourceMapSegment:
        return SourceMapSegment(
            SourceSpan(self._cursor, self._cursor + width),
            self._parent.artifact,
            span,
            MappingRelation.EXACT,
        )

    def copy(self, span: SourceSpan) -> None:
        self._validate_parent_span(span)
        width = span.end - span.start
        self._append_retained(
            self._parent,
            span,
            self._exact_segment(span, width),
        )

    def exact(self, text: str, span: SourceSpan) -> None:
        """Append already materialized exact text for a deferred parent."""
        _require_str(text, "exact text")
        self._validate_parent_span(span)
        if len(text) != span.end - span.start:
            raise ValueError("exact text must preserve the parent span length")
        self._append(text, self._exact_segment(span, len(text)))

    def derived(
        self,
        text: str,
        span: SourceSpan,
        region: str,
        *,
        anchor: SourceSpan | None = None,
    ) -> None:
        _require_str(text, "derived text")
        _require_str(region, "derived region", nonempty=True)
        self._validate_parent_span(span)
        if anchor is not None:
            self._validate_parent_span(anchor)
        self._append(
            text,
            SourceMapSegment(
                SourceSpan(self._cursor, self._cursor + len(text)),
                self._parent.artifact,
                span,
                MappingRelation.DERIVED,
                region,
                self._parent.artifact if anchor is not None else None,
                anchor,
            ),
        )

    def reuse_exact(self, span: SourceSpan) -> None:
        """Import an unchanged exact parent segment without entering copy work."""
        self.copy(span)

    def retained_exact(
        self,
        retained_source: MappedSource | _DeferredMappedSource,
        retained_span: SourceSpan,
        parent_span: SourceSpan,
    ) -> None:
        """Map an existing materialized span exactly against the current parent."""
        self._validate_parent_span(parent_span)
        width = retained_span.end - retained_span.start
        if width != parent_span.end - parent_span.start:
            raise ValueError("retained exact text must preserve span length")
        self._append_retained(
            retained_source,
            retained_span,
            self._exact_segment(parent_span, width),
        )

    def synthetic(self, text: str, anchor: SourceSpan, region: str) -> None:
        _require_str(text, "synthetic text")
        _require_str(region, "synthetic region", nonempty=True)
        self._validate_parent_span(anchor)
        self._append(
            text,
            SourceMapSegment(
                SourceSpan(self._cursor, self._cursor + len(text)),
                None,
                None,
                MappingRelation.SYNTHETIC,
                region,
                self._parent.artifact,
                anchor,
            ),
        )

    def reuse_derived(
        self,
        text: str,
        span: SourceSpan,
        region: str,
        *,
        anchor: SourceSpan | None = None,
    ) -> None:
        """Import an unchanged derived segment against the current parent."""
        _require_str(text, "derived text")
        _require_str(region, "derived region", nonempty=True)
        self._validate_parent_span(span)
        if anchor is not None:
            self._validate_parent_span(anchor)
        self._append(
            text,
            SourceMapSegment(
                SourceSpan(self._cursor, self._cursor + len(text)),
                self._parent.artifact,
                span,
                MappingRelation.DERIVED,
                region,
                self._parent.artifact if anchor is not None else None,
                anchor,
            ),
        )

    def retained_derived(
        self,
        retained_source: MappedSource | _DeferredMappedSource,
        retained_span: SourceSpan,
        span: SourceSpan,
        region: str,
        *,
        anchor: SourceSpan | None = None,
    ) -> None:
        """Map an existing materialized span as derived current-parent text."""
        _require_str(region, "derived region", nonempty=True)
        self._validate_parent_span(span)
        if anchor is not None:
            self._validate_parent_span(anchor)
        width = retained_span.end - retained_span.start
        self._append_retained(
            retained_source,
            retained_span,
            SourceMapSegment(
                SourceSpan(self._cursor, self._cursor + width),
                self._parent.artifact,
                span,
                MappingRelation.DERIVED,
                region,
                self._parent.artifact if anchor is not None else None,
                anchor,
            ),
        )

    def reuse_synthetic(self, text: str, anchor: SourceSpan, region: str) -> None:
        """Import an unchanged synthetic segment against the current parent."""
        _require_str(text, "synthetic text")
        _require_str(region, "synthetic region", nonempty=True)
        self._validate_parent_span(anchor)
        self._append(
            text,
            SourceMapSegment(
                SourceSpan(self._cursor, self._cursor + len(text)),
                None,
                None,
                MappingRelation.SYNTHETIC,
                region,
                self._parent.artifact,
                anchor,
            ),
        )

    def retained_synthetic(
        self,
        retained_source: MappedSource | _DeferredMappedSource,
        retained_span: SourceSpan,
        anchor: SourceSpan,
        region: str,
    ) -> None:
        """Map an existing materialized span as synthetic current-parent text."""
        _require_str(region, "synthetic region", nonempty=True)
        self._validate_parent_span(anchor)
        width = retained_span.end - retained_span.start
        self._append_retained(
            retained_source,
            retained_span,
            SourceMapSegment(
                SourceSpan(self._cursor, self._cursor + width),
                None,
                None,
                MappingRelation.SYNTHETIC,
                region,
                self._parent.artifact,
                anchor,
            ),
        )

    def _artifact(
        self,
        kind: SourceArtifactKind,
        identity: dict[str, object],
        *,
        deferred: bool,
    ) -> tuple[SourceArtifactRef, str | None]:
        if type(kind) is not SourceArtifactKind:
            raise ValueError("artifact kind must be a SourceArtifactKind")
        unknown = set(identity) - _ARTIFACT_IDENTITY_FIELDS
        if unknown:
            raise ValueError(
                f"unsupported artifact identity metadata: {sorted(unknown)!r}"
            )
        if not self._segments:
            raise ValueError("transform must contain at least one explicit fragment")
        if deferred:
            digest = sha256()
            for fragment in self._parts:
                digest.update(fragment.text.encode("utf-8"))
            text_hash = digest.hexdigest()
            _observe_text_work("projection_hash", self._cursor)
            text = None
        else:
            _observe_text_work("materialize", self._cursor)
            text = "".join(fragment.text for fragment in self._parts)
            _observe_text_work("final_hash", self._cursor)
            text_hash = source_sha256(text)
        ending = _line_ending_kind_fragments(self._parts)
        artifact = SourceArtifactRef(
            kind,
            text_hash,
            self._cursor,
            ending,
            **identity,  # type: ignore[arg-type]
        )
        return artifact, text

    def build_deferred(
        self,
        kind: SourceArtifactKind,
        **identity: object,
    ) -> _DeferredMappedSource:
        artifact, _ = self._artifact(kind, identity, deferred=True)
        local = SourceMap(artifact, _coalesce_segments(self._segments))
        flattened = _compose_reused_source_maps(local, self._parent.source_map)
        lineage = (*self._parent.lineage, local)
        retained = tuple(self._parts)
        return _DeferredMappedSource(
            artifact,
            flattened,
            lineage,
            local,
            retained,
            tuple(fragment.generated.start for fragment in retained),
            _retained_state_proof(artifact, retained),
            _RETAINED_CONSTRUCTION_AUTHORITY,
        )

    def build(
        self,
        kind: SourceArtifactKind,
        *,
        _normalized_worker_authority: object | None = None,
        **identity: object,
    ) -> MappedSource:
        self._validate_normalized_worker_policy(
            kind,
            _normalized_worker_authority,
        )
        artifact, text = self._artifact(kind, identity, deferred=False)
        assert text is not None
        local = SourceMap(artifact, _coalesce_segments(self._segments))
        flattened = compose_source_maps(local, self._parent.source_map)
        lineage = (*self._parent.lineage, local)
        proof = _mint_trusted_text_proof(
            text,
            artifact.source_sha256,
            authority=_TRUSTED_TEXT_PROOF_AUTHORITY,
        )
        return MappedSource(
            text,
            artifact,
            flattened,
            lineage,
            local,
            _retained_fragments=tuple(self._parts),
            _transform_parent=self._parent,
            _trusted_text_proof=proof,
            _retained_authority=_RETAINED_CONSTRUCTION_AUTHORITY,
            _normalized_worker_authority=_normalized_worker_authority,
        )

    def build_reused(
        self,
        kind: SourceArtifactKind,
        *,
        _normalized_worker_authority: object | None = None,
        **identity: object,
    ) -> MappedSource:
        """Build imported monotonic segments with a linear source-map splice."""
        self._validate_normalized_worker_policy(
            kind,
            _normalized_worker_authority,
        )
        artifact, text = self._artifact(kind, identity, deferred=False)
        assert text is not None
        local = SourceMap(artifact, _coalesce_segments(self._segments))
        flattened = _compose_reused_source_maps(local, self._parent.source_map)
        lineage = (*self._parent.lineage, local)
        proof = _mint_trusted_text_proof(
            text,
            artifact.source_sha256,
            authority=_TRUSTED_TEXT_PROOF_AUTHORITY,
        )
        return MappedSource(
            text,
            artifact,
            flattened,
            lineage,
            local,
            _retained_fragments=tuple(self._parts),
            _transform_parent=self._parent,
            _trusted_text_proof=proof,
            _retained_authority=_RETAINED_CONSTRUCTION_AUTHORITY,
            _normalized_worker_authority=_normalized_worker_authority,
        )

    def build_canonical_reused(
        self,
        kind: SourceArtifactKind,
        *,
        _normalized_worker_authority: object | None = None,
        **identity: object,
    ) -> MappedSource:
        """Build a final source whose retained state owns no prior generation."""
        if not isinstance(self._parent, _DeferredMappedSource):
            raise ValueError("canonical retained build requires a deferred parent")
        self._validate_normalized_worker_policy(
            kind,
            _normalized_worker_authority,
        )
        artifact, text = self._artifact(kind, identity, deferred=False)
        assert text is not None
        local = SourceMap(artifact, _coalesce_segments(self._segments))
        flattened = _compose_reused_source_maps(local, self._parent.source_map)
        lineage = (*self._parent.lineage, local)
        retained = tuple(
            _RetainedTextFragment(
                fragment.generated,
                fragment.text,
                line_endings=fragment.line_endings,
                contains_nul=fragment.contains_nul,
            )
            for fragment in self._parts
        )
        parent = _detached_retained_source(self._parent)
        proof = _mint_trusted_text_proof(
            text,
            artifact.source_sha256,
            authority=_TRUSTED_TEXT_PROOF_AUTHORITY,
        )
        return MappedSource(
            text,
            artifact,
            flattened,
            lineage,
            local,
            _retained_fragments=retained,
            _transform_parent=parent,
            _trusted_text_proof=proof,
            _retained_authority=_RETAINED_CONSTRUCTION_AUTHORITY,
            _normalized_worker_authority=_normalized_worker_authority,
        )


class LineIndex:
    """Map zero-based Unicode code-point offsets to one-based line/columns."""

    def __init__(self, source: str) -> None:
        _require_str(source, "source")
        self._length = len(source)
        self._line_starts = (0, *(i + 1 for i, char in enumerate(source) if char == "\n"))

    def offset_to_line_column(self, offset: int) -> tuple[int, int]:
        if type(offset) is not int or not 0 <= offset <= self._length:
            raise ValueError("offset must be within the source")
        line_index = bisect_right(self._line_starts, offset) - 1
        return line_index + 1, offset - self._line_starts[line_index] + 1

    def line_column_to_offset(self, line: int, column: int) -> int:
        if type(line) is not int or not 1 <= line <= len(self._line_starts):
            raise ValueError("line must be within the source")
        if type(column) is not int or column < 1:
            raise ValueError("column must be positive")
        line_index = line - 1
        start = self._line_starts[line_index]
        next_start = (
            self._line_starts[line_index + 1]
            if line_index + 1 < len(self._line_starts)
            else self._length + 1
        )
        if start + column > next_start:
            raise ValueError("column must be within the source line")
        return start + column - 1

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
