"""Canonical provenance for consumer-owned generated parser artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import TypeAlias


ManifestOptionValue: TypeAlias = bool | int | str
ManifestOptions: TypeAlias = tuple[tuple[str, ManifestOptionValue], ...]


def parsergen_package_sha256(parsergen_source: Path) -> str:
    """Hash the normalized Python inputs for one parsergen package checkout."""
    package = parsergen_source / "parsergen"
    digest = sha256()
    for source in sorted(package.rglob("*.py"), key=lambda path: path.as_posix()):
        relative = source.relative_to(parsergen_source).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        normalized = source.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        digest.update(normalized)
        digest.update(b"\0")
    return digest.hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


@dataclass(frozen=True, slots=True)
class ParserArtifactManifest:
    """Every consumer input that distinguishes one parser artifact from another."""

    schema_version: int
    backend_id: str
    artifact_role: str
    grammar_source_sha256: str
    parsergen_package_sha256: str | None
    entrypoints: tuple[tuple[str, str], ...]
    lookahead: int
    production_names: tuple[str, ...]
    optimizer_options: ManifestOptions
    codegen_options: ManifestOptions
    identity_sha256: str

    def to_json(self) -> str:
        return _canonical_json(
            {
                "schema_version": self.schema_version,
                "backend_id": self.backend_id,
                "artifact_role": self.artifact_role,
                "grammar_source_sha256": self.grammar_source_sha256,
                "parsergen_package_sha256": self.parsergen_package_sha256,
                "entrypoints": self.entrypoints,
                "lookahead": self.lookahead,
                "production_names": self.production_names,
                "optimizer_options": self.optimizer_options,
                "codegen_options": self.codegen_options,
                "identity_sha256": self.identity_sha256,
            }
        )


def build_parser_artifact_manifest(
    *,
    backend_id: str,
    artifact_role: str,
    grammar_bytes: bytes,
    parsergen_package_sha256: str | None,
    entrypoints: tuple[tuple[str, str], ...],
    lookahead: int,
    production_names: tuple[str, ...],
    optimizer_options: ManifestOptions,
    codegen_options: ManifestOptions,
) -> ParserArtifactManifest:
    """Build an identity from raw inputs before any parsing normalization."""
    payload = {
        "schema_version": 2,
        "backend_id": backend_id,
        "artifact_role": artifact_role,
        "grammar_source_sha256": sha256(grammar_bytes).hexdigest(),
        "parsergen_package_sha256": parsergen_package_sha256,
        "entrypoints": entrypoints,
        "lookahead": lookahead,
        "production_names": production_names,
        "optimizer_options": optimizer_options,
        "codegen_options": codegen_options,
    }
    return ParserArtifactManifest(
        **payload,
        identity_sha256=sha256(_canonical_json(payload).encode("utf-8")).hexdigest(),
    )


def verify_parser_artifact_manifest(raw: str) -> ParserArtifactManifest:
    """Decode a manifest only when its recorded consumer identity is authentic."""
    decoded = json.loads(raw)
    if decoded.get("schema_version") != 2:
        raise ValueError("unsupported parser artifact manifest schema")
    recorded_identity = decoded.pop("identity_sha256")
    expected_identity = sha256(_canonical_json(decoded).encode("utf-8")).hexdigest()
    if recorded_identity != expected_identity:
        raise ValueError("generated parser artifact identity is invalid")
    decoded["entrypoints"] = tuple(tuple(item) for item in decoded["entrypoints"])
    decoded["production_names"] = tuple(decoded["production_names"])
    decoded["optimizer_options"] = tuple(
        tuple(item) for item in decoded["optimizer_options"]
    )
    decoded["codegen_options"] = tuple(tuple(item) for item in decoded["codegen_options"])
    return ParserArtifactManifest(**decoded, identity_sha256=recorded_identity)
