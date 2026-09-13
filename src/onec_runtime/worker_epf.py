from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import UUID, uuid5

from onec_runtime.bsl.source_maps import (
    MappedSource,
    MappingRelation,
    SourceArtifactKind,
    SourceArtifactRef,
    SourceMap,
    SourceMapSegment,
    SourceSpan,
    SourceTransformBuilder,
    SourceUnitRef,
    _NORMALIZED_WORKER_AUTHORITY,
    _observe_text_work,
    _validated_mapped_text_hash,
    source_sha256,
)
from onec_runtime.epf_container import (
    EpfContainerError,
    build_container,
    raw_deflate,
    raw_inflate,
    read_container,
)
from onec_runtime.worker_epf_template import (
    WORKER_COPYINFO,
    WORKER_FILE_UUID,
    WORKER_METADATA,
    WORKER_MODULE_INFO,
    WORKER_MODULE_STREAM,
    WORKER_ROOT,
    WORKER_STREAM_NAMES,
    WORKER_VERSION,
)


_VERSION_NAMESPACE = UUID("42ff273a-cb53-4fd5-a844-34b27d69edb0")


def prepare_worker_module_source(source: MappedSource) -> MappedSource:
    """Map a Worker projection to the exact normalized EPF object module text."""
    if not isinstance(source, MappedSource):
        raise ValueError("Worker module source must be a MappedSource")
    _normalize_source(source.text)
    source = _without_worker_generation_identity(source)
    builder = SourceTransformBuilder(
        source,
        _normalized_parent_authority=_NORMALIZED_WORKER_AUTHORITY,
    )
    cursor = 0
    index = 0
    while index < len(source.text):
        if source.text[index] != "\r":
            index += 1
            continue
        if cursor < index:
            builder.copy(SourceSpan(cursor, index))
        end = index + 2 if source.text.startswith("\r\n", index) else index + 1
        parent = next(
            segment
            for segment in source.source_map.segments
            if segment.generated.start <= index < segment.generated.end
        )
        if end == index + 2 and parent.generated.end == index + 1:
            cursor = index + 1
            index = cursor
            continue
        region = (
            parent.synthetic_region
            if parent.relation is MappingRelation.DERIVED
            and parent.synthetic_region is not None
            else "worker_module_line_ending"
        )
        builder.derived("\n", SourceSpan(index, end), region)
        cursor = end
        index = end
    if cursor < len(source.text):
        builder.copy(SourceSpan(cursor, len(source.text)))
    return builder.build(
        SourceArtifactKind.WORKER_MODULE,
        _normalized_worker_authority=_NORMALIZED_WORKER_AUTHORITY,
    )


def _without_worker_generation_identity(source: MappedSource) -> MappedSource:
    artifact = _without_generation_artifact_identity(source.artifact)
    source_map = _without_generation_map_identity(source.source_map)
    lineage = tuple(
        _without_generation_map_identity(item)
        for item in source.lineage
    )
    try:
        source.local_source_map
    except ValueError:
        local = None
    else:
        local = lineage[-1]
    if (
        artifact == source.artifact
        and source_map == source.source_map
        and lineage == source.lineage
    ):
        return source
    return MappedSource(
        source.text,
        artifact,
        source_map,
        lineage,
        local,
    )


def _without_generation_artifact_identity(
    artifact: SourceArtifactRef,
) -> SourceArtifactRef:
    if artifact.worker_generation is None and artifact.worker_manifest_sha256 is None:
        return artifact
    return replace(
        artifact,
        worker_generation=None,
        worker_manifest_sha256=None,
    )


def _without_generation_map_identity(source_map: SourceMap) -> SourceMap:
    def clean_reference(
        reference: SourceArtifactRef | SourceUnitRef | None,
    ) -> SourceArtifactRef | SourceUnitRef | None:
        return (
            _without_generation_artifact_identity(reference)
            if isinstance(reference, SourceArtifactRef)
            else reference
        )

    return SourceMap(
        _without_generation_artifact_identity(source_map.generated),
        tuple(
            replace(
                segment,
                origin_ref=clean_reference(segment.origin_ref),
                anchor_ref=clean_reference(segment.anchor_ref),
            )
            for segment in source_map.segments
        ),
    )


def _synthetic_worker_module_source(
    source: str,
    *,
    normalize: bool = True,
) -> MappedSource:
    """Private compatibility adapter for legacy source-only Worker callers."""
    normalized = _normalize_source(source) if normalize else source
    if not normalize:
        if not normalized.strip():
            raise ValueError("Worker source is empty")
        if "\x00" in normalized:
            raise ValueError("Worker source contains a NUL character")
    crlf_count = normalized.count("\r\n")
    bare_lf_count = normalized.count("\n") - crlf_count
    bare_cr_count = normalized.count("\r") - crlf_count
    kinds = sum(count > 0 for count in (crlf_count, bare_lf_count, bare_cr_count))
    line_ending_kind = (
        "mixed"
        if kinds > 1
        else "crlf"
        if crlf_count
        else "lf"
        if bare_lf_count
        else "cr"
        if bare_cr_count
        else "none"
    )
    artifact = SourceArtifactRef(
        SourceArtifactKind.WORKER_MODULE,
        source_sha256(normalized),
        len(normalized),
        line_ending_kind,
    )
    source_map = SourceMap(
        artifact,
        (
            SourceMapSegment(
                SourceSpan(0, len(normalized)),
                None,
                None,
                MappingRelation.SYNTHETIC,
                "legacy_worker_source",
            ),
        ),
    )
    return MappedSource(normalized, artifact, source_map)


def build_worker_epf(source: str | MappedSource, output_path: Path) -> Path:
    if isinstance(source, MappedSource):
        normalized = source.text
        source_hash = _validated_mapped_text_hash(source)
        _observe_text_work("packaging_normalized_authority", len(normalized))
    else:
        _observe_text_work("packaging_normalize", len(source))
        normalized = _normalize_source(source)
        _observe_text_work("packaging_hash", len(normalized))
        source_hash = sha256(normalized.encode("utf-8")).hexdigest()
    nested_module = build_container(
        {
            "info": _encode_brace_text(WORKER_MODULE_INFO),
            "text": _encode_source(normalized, observe=True),
        }
    )
    streams = {
        WORKER_FILE_UUID: _encode_brace_text(WORKER_METADATA),
        WORKER_MODULE_STREAM: nested_module,
        "copyinfo": _encode_brace_text(WORKER_COPYINFO),
        "root": _encode_brace_text(WORKER_ROOT),
        "version": _encode_brace_text(WORKER_VERSION),
        "versions": _encode_brace_text(_versions(source_hash)),
    }
    artifact = build_container(
        {name: raw_deflate(payload) for name, payload in streams.items()}
    )
    recovered = read_worker_source(artifact)
    _observe_text_work("packaging_verify", len(recovered))
    if recovered != normalized:
        raise EpfContainerError("Generated Worker failed source self-verification")

    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="wb",
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            stream.write(artifact)
            temporary = Path(stream.name)
        temporary.replace(output_path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return output_path


def read_worker_source(payload_or_path: bytes | Path) -> str:
    payload = (
        payload_or_path
        if isinstance(payload_or_path, bytes)
        else payload_or_path.read_bytes()
    )
    streams = read_container(payload)
    if tuple(streams) != WORKER_STREAM_NAMES:
        raise EpfContainerError("EPF is not the supported runtime Worker")
    try:
        nested = read_container(raw_inflate(streams[WORKER_MODULE_STREAM]))
    except KeyError as error:
        raise EpfContainerError("Worker object module stream is missing") from error
    if tuple(nested) != ("info", "text"):
        raise EpfContainerError("Worker object module container is invalid")
    try:
        info = nested["info"].decode("utf-8-sig")
        source = nested["text"].decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise EpfContainerError("Worker object module is not UTF-8") from error
    if _normalize_newlines(info) != WORKER_MODULE_INFO:
        raise EpfContainerError("Worker object module metadata is invalid")
    return _normalize_newlines(source)


def _normalize_source(source: str) -> str:
    normalized = _normalize_newlines(source)
    if not normalized.strip():
        raise ValueError("Worker source is empty")
    if "\x00" in normalized:
        raise ValueError("Worker source contains a NUL character")
    return normalized


def _normalize_newlines(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _encode_source(source: str, *, observe: bool = False) -> bytes:
    if observe:
        _observe_text_work("packaging_encode", len(source))
    return source.replace("\n", "\r\n").encode("utf-8-sig")


def _encode_brace_text(value: str) -> bytes:
    return _encode_source(_normalize_newlines(value))


def _versions(source_hash: str) -> str:
    version_names = ("", *WORKER_STREAM_NAMES)
    items: list[str] = ["1", str(len(version_names))]
    for name in version_names:
        rendered_name = f'"{name}"' if name else '""'
        items.extend(
            (
                rendered_name,
                str(uuid5(_VERSION_NAMESPACE, f"{source_hash}:{name}")),
            )
        )
    return "{" + ",".join(items) + "}"
