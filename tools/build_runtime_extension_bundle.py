from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from hashlib import sha256
from pathlib import Path
from uuid import UUID

_WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_WORKSPACE / "src"))

from onec_runtime.errors import ExtensionBundleError, ProcessStartError
from onec_runtime.extension_bundle import (
    EXTENSION_NAME,
    PRODUCT_ID,
    BreakpointContract,
    ExtensionBundle,
    ExtensionManifest,
    fingerprint_extension_dump,
    materialize_extension_bundle,
    read_extension_manifest,
)
from onec_runtime.rdbg.models import ModuleLocation
from onec_runtime.toolchain import (
    HEADLESS_ARGS,
    ToolResult,
    create_file_infobase_at_command,
    dump_product_extension_cfe_command,
    load_product_extension_source_command,
    run_tool_command,
)

_MANIFEST_SCHEMA_VERSION = 1
_MANIFEST_FILENAME = "extension-manifest.json"
_MANAGED_SOURCE = Path("Ext/ManagedApplicationModule.bsl")
_SERVER_SOURCE = Path("CommonModules/RuntimeKernelServer/Ext/Module.bsl")
_MANAGED_PROPERTY_ID = UUID("d22e852a-cf8a-4f77-8ccb-3548e7792bea")
_COMMON_MODULE_PROPERTY_ID = UUID("d5963243-262e-4398-b4d7-fb16d06484f6")


def _fail(message: str) -> ExtensionBundleError:
    return ExtensionBundleError(message)


def _marker_line(path: Path, marker: str) -> int:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise _fail(f"breakpoint source is missing or invalid UTF-8: {path}") from error
    matches = [number for number, line in enumerate(lines, start=1) if marker in line]
    if len(matches) != 1:
        raise _fail(f"{path} must contain exactly one {marker} marker")
    return matches[0]


def build_extension_manifest(cfe_path: Path, dump_root: Path) -> ExtensionManifest:
    """Bind one freshly dumped CFE to its strict XML/BSL artifact contract."""
    if cfe_path.name != f"{EXTENSION_NAME}.cfe":
        raise _fail(f"release CFE must be named {EXTENSION_NAME}.cfe")
    try:
        cfe_size = cfe_path.stat().st_size
        cfe_sha256 = sha256(cfe_path.read_bytes()).hexdigest()
    except OSError as error:
        raise _fail(f"release CFE is unreadable: {cfe_path}") from error
    if cfe_size == 0:
        raise _fail(f"release CFE is empty: {cfe_path}")

    fingerprints = fingerprint_extension_dump(dump_root)
    server_ids = {
        item.object_id
        for item in fingerprints.artifact.metadata
        if item.name == "CommonModule.RuntimeKernelServer"
    }
    if len(server_ids) != 1:
        raise _fail("dump must identify CommonModule.RuntimeKernelServer exactly once")
    server_id = next(iter(server_ids))
    location = {
        "module_type": "ExtensionModule",
        "url": "",
        "extension_name": EXTENSION_NAME,
        "ext_id": 0,
    }
    breakpoints = BreakpointContract(
        managed=ModuleLocation(
            **location,
            object_id=fingerprints.identity.root_id,
            property_id=_MANAGED_PROPERTY_ID,
            line=_marker_line(
                dump_root / _MANAGED_SOURCE,
                "@runtime-extension-service-breakpoint",
            ),
        ),
        server_entry=ModuleLocation(
            **location,
            object_id=server_id,
            property_id=_COMMON_MODULE_PROPERTY_ID,
            line=_marker_line(
                dump_root / _SERVER_SOURCE,
                "@runtime-server-extension-entry-breakpoint",
            ),
        ),
        server_service=ModuleLocation(
            **location,
            object_id=server_id,
            property_id=_COMMON_MODULE_PROPERTY_ID,
            line=_marker_line(
                dump_root / _SERVER_SOURCE,
                "@runtime-server-extension-service-breakpoint",
            ),
        ),
    )
    return ExtensionManifest(
        schema_version=_MANIFEST_SCHEMA_VERSION,
        product_id=PRODUCT_ID,
        extension_name=EXTENSION_NAME,
        artifact_version=fingerprints.artifact.artifact_version,
        protocol_version=fingerprints.artifact.protocol_version,
        cfe_filename=cfe_path.name,
        cfe_size=cfe_size,
        cfe_sha256=cfe_sha256,
        fingerprints=fingerprints,
        breakpoints=breakpoints,
    )


def _location_payload(location: ModuleLocation) -> dict[str, object]:
    return {
        "module_type": location.module_type,
        "url": location.url,
        "object_id": str(location.object_id),
        "property_id": str(location.property_id),
        "line": location.line,
        "extension_name": location.extension_name,
        "ext_id": location.ext_id,
    }


def _manifest_payload(manifest: ExtensionManifest) -> dict[str, object]:
    fingerprints = manifest.fingerprints
    identity = fingerprints.identity
    artifact = fingerprints.artifact
    return {
        "schema_version": manifest.schema_version,
        "product_id": manifest.product_id,
        "extension_name": manifest.extension_name,
        "artifact_version": manifest.artifact_version,
        "protocol_version": manifest.protocol_version,
        "cfe_filename": manifest.cfe_filename,
        "cfe_size": manifest.cfe_size,
        "cfe_sha256": manifest.cfe_sha256,
        "fingerprints": {
            "identity": {
                "product_id": identity.product_id,
                "extension_name": identity.extension_name,
                "root_id": str(identity.root_id),
                "runtime_module_ids": [
                    str(value) for value in identity.runtime_module_ids
                ],
                "purpose": identity.purpose,
                "name_prefix": identity.name_prefix,
                "vendor": identity.vendor,
            },
            "artifact": {
                "artifact_version": artifact.artifact_version,
                "protocol_version": artifact.protocol_version,
                "language_bound_by_name": artifact.language_bound_by_name,
                "metadata": [
                    {"name": item.name, "object_id": str(item.object_id)}
                    for item in artifact.metadata
                ],
                "source_sha256": dict(artifact.source_sha256),
            },
            "identity_sha256": fingerprints.identity_sha256,
            "artifact_sha256": fingerprints.artifact_sha256,
        },
        "breakpoints": {
            "managed": _location_payload(manifest.breakpoints.managed),
            "server_entry": _location_payload(manifest.breakpoints.server_entry),
            "server_service": _location_payload(manifest.breakpoints.server_service),
        },
    }


def write_extension_manifest(manifest: ExtensionManifest, path: Path) -> None:
    """Write a deterministic manifest without exposing a partial destination."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}-", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(temporary_fd, "w", encoding="utf-8", newline="") as stream:
            json.dump(_manifest_payload(manifest), stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _dump_product_extension_files_command(
    designer_exe: Path,
    infobase_dir: Path,
    output_dir: Path,
    log_path: Path,
) -> list[str]:
    return [
        str(designer_exe),
        "DESIGNER",
        "/F",
        str(infobase_dir),
        "/DumpConfigToFiles",
        str(output_dir),
        "-Extension",
        EXTENSION_NAME,
        "-Format",
        "Hierarchical",
        *HEADLESS_ARGS,
        "/Out",
        str(log_path),
    ]


def _write_transcript(path: Path, entries: list[dict[str, object]]) -> None:
    path.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="",
    )


def _run_recorded(
    command: list[str],
    log_path: Path,
    transcript_path: Path,
    entries: list[dict[str, object]],
) -> ToolResult:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.unlink(missing_ok=True)
    entry: dict[str, object] = {
        "command": command,
        "log_path": str(log_path),
        "returncode": None,
    }
    entries.append(entry)
    _write_transcript(transcript_path, entries)
    try:
        result = run_tool_command(command, log_path)
    except ProcessStartError as error:
        entry["error"] = str(error)
        _write_transcript(transcript_path, entries)
        raise
    entry["returncode"] = result.returncode
    _write_transcript(transcript_path, entries)
    return result


def _require_non_empty_file(path: Path, label: str, log_path: Path) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise ProcessStartError(
            f"1C did not create non-empty {label}: {path}; see {log_path}"
        )


def _publish_bundle(
    cfe_path: Path,
    manifest_path: Path,
    output_root: Path,
    validation_runtime: Path,
) -> ExtensionBundle:
    output_root.mkdir(parents=True, exist_ok=True)
    publication = Path(tempfile.mkdtemp(prefix=".bundle-", dir=output_root))
    try:
        staged_cfe = publication / cfe_path.name
        staged_manifest = publication / manifest_path.name
        shutil.copy2(cfe_path, staged_cfe)
        shutil.copy2(manifest_path, staged_manifest)
        validated = materialize_extension_bundle(
            staged_cfe, staged_manifest, validation_runtime
        )
        parsed = read_extension_manifest(staged_manifest)
        if validated.manifest != parsed:
            raise _fail("validated manifest changed while staging resource outputs")
        final_cfe = output_root / cfe_path.name
        final_manifest = output_root / manifest_path.name
        previous_cfe = publication / ".previous-cfe"
        previous_manifest = publication / ".previous-manifest"
        cfe_existed = final_cfe.exists()
        manifest_existed = final_manifest.exists()
        if cfe_existed:
            shutil.copy2(final_cfe, previous_cfe)
        if manifest_existed:
            shutil.copy2(final_manifest, previous_manifest)
        try:
            os.replace(staged_cfe, final_cfe)
            os.replace(staged_manifest, final_manifest)
        except BaseException:
            if cfe_existed:
                os.replace(previous_cfe, final_cfe)
            else:
                final_cfe.unlink(missing_ok=True)
            if manifest_existed:
                os.replace(previous_manifest, final_manifest)
            else:
                final_manifest.unlink(missing_ok=True)
            raise
        final_parsed = read_extension_manifest(final_manifest)
        if final_parsed != parsed:
            raise _fail("published extension manifest changed during replacement")
        if (
            final_cfe.stat().st_size != final_parsed.cfe_size
            or sha256(final_cfe.read_bytes()).hexdigest() != final_parsed.cfe_sha256
        ):
            raise _fail("published extension CFE disagrees with its manifest")
        return ExtensionBundle(final_cfe, final_parsed, output_root)
    finally:
        shutil.rmtree(publication, ignore_errors=True)


def build_runtime_extension_bundle(
    *,
    source_root: Path,
    output_root: Path,
    platform_bin: Path,
    artifact_version: str,
    protocol_version: str,
) -> ExtensionBundle:
    """Build a release bundle using only canonical repository source."""
    source_root = source_root.resolve()
    output_root = output_root.resolve()
    platform_bin = platform_bin.resolve()
    if source_root.name != EXTENSION_NAME or source_root.parent.name != "onec":
        raise _fail(
            f"source_root must identify repository source onec/{EXTENSION_NAME}"
        )
    source_fingerprints = fingerprint_extension_dump(source_root)
    if source_fingerprints.artifact.artifact_version != artifact_version:
        raise _fail(
            "requested artifact version does not match canonical XML and BSL source"
        )
    if source_fingerprints.artifact.protocol_version != protocol_version:
        raise _fail("requested protocol version does not match canonical BSL source")

    designer_exe = platform_bin / "1cv8.exe"
    if not designer_exe.is_file():
        raise _fail(f"1C Designer executable is missing: {designer_exe}")
    build_parent = source_root.parents[1] / ".runtime" / "extension-builds"
    build_parent.mkdir(parents=True, exist_ok=True)
    run_root = Path(tempfile.mkdtemp(prefix="build-", dir=build_parent))
    infobase = run_root / "infobase"
    artifact_root = run_root / "artifacts"
    staged_source = run_root / "source"
    cfe_path = artifact_root / f"{EXTENSION_NAME}.cfe"
    dump_root = artifact_root / "dump"
    manifest_path = artifact_root / _MANIFEST_FILENAME
    logs = run_root / "logs"
    transcript_path = run_root / "command-transcript.json"
    entries: list[dict[str, object]] = []
    shutil.copytree(source_root, staged_source)
    artifact_root.mkdir()

    create_log = logs / "01-create-infobase.log"
    _run_recorded(
        create_file_infobase_at_command(designer_exe, infobase, create_log),
        create_log,
        transcript_path,
        entries,
    )
    _require_non_empty_file(infobase / "1Cv8.1CD", "1Cv8.1CD", create_log)

    load_log = logs / "02-load-extension-source.log"
    _run_recorded(
        load_product_extension_source_command(
            designer_exe, infobase, staged_source, load_log
        ),
        load_log,
        transcript_path,
        entries,
    )

    dump_cfe_log = logs / "03-dump-extension-cfe.log"
    _run_recorded(
        dump_product_extension_cfe_command(
            designer_exe, infobase, cfe_path, dump_cfe_log
        ),
        dump_cfe_log,
        transcript_path,
        entries,
    )
    _require_non_empty_file(cfe_path, "CFE", dump_cfe_log)

    dump_source_log = logs / "04-dump-extension-source.log"
    _run_recorded(
        _dump_product_extension_files_command(
            designer_exe, infobase, dump_root, dump_source_log
        ),
        dump_source_log,
        transcript_path,
        entries,
    )
    manifest = build_extension_manifest(cfe_path, dump_root)
    if manifest.fingerprints != source_fingerprints:
        raise _fail("Designer dump disagrees with canonical repository source")
    if (
        manifest.artifact_version != artifact_version
        or manifest.protocol_version != protocol_version
    ):
        raise _fail("built extension manifest disagrees with requested versions")
    write_extension_manifest(manifest, manifest_path)
    parsed = read_extension_manifest(manifest_path)
    if parsed != manifest:
        raise _fail("generated extension manifest failed strict round-trip validation")
    return _publish_bundle(
        cfe_path,
        manifest_path,
        output_root,
        run_root / "validation-runtime",
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the checked-in universal runtime extension bundle."
    )
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--platform-bin", type=Path, required=True)
    parser.add_argument("--artifact-version", required=True)
    parser.add_argument("--protocol-version", required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    workspace = args.workspace.resolve()
    bundle = build_runtime_extension_bundle(
        source_root=workspace / "onec" / EXTENSION_NAME,
        output_root=workspace / "src" / "onec_runtime" / "resources" / "extension",
        platform_bin=args.platform_bin,
        artifact_version=args.artifact_version,
        protocol_version=args.protocol_version,
    )
    print(
        json.dumps(
            {
                "cfe_path": str(bundle.cfe_path),
                "cfe_size": bundle.manifest.cfe_size,
                "cfe_sha256": bundle.manifest.cfe_sha256,
                "manifest_path": str(bundle.cache_dir / _MANIFEST_FILENAME),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
