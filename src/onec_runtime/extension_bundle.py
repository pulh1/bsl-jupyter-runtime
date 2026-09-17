from __future__ import annotations

import importlib.resources
import json
import os
import re
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from uuid import UUID
from xml.etree import ElementTree

from onec_runtime.errors import ExtensionBundleError
from onec_runtime.rdbg.models import ModuleLocation

EXTENSION_NAME = "OnecInteractiveRuntime"
PRODUCT_ID = "onec-interactive-runtime"

_MANIFEST_FILENAME = "extension-manifest.json"
_MANIFEST_SCHEMA_VERSION = 1
_MD_NAMESPACE = "http://v8.1c.ru/8.3/MDClasses"
_DUMP_NAMESPACE = "http://v8.1c.ru/8.3/xcf/dumpinfo"
_MANAGED_SOURCE = "Ext/ManagedApplicationModule.bsl"
_SERVER_SOURCE = "CommonModules/RuntimeKernelServer/Ext/Module.bsl"
_VALUE_TRANSFER_SOURCE = "CommonModules/RuntimeValueTransferServer/Ext/Module.bsl"
_TABLE_TRANSFER_SOURCE = "CommonModules/RuntimeTableTransferServer/Ext/Module.bsl"
_PROTOCOL_SOURCES = frozenset(
    {
        _MANAGED_SOURCE,
        _SERVER_SOURCE,
        _VALUE_TRANSFER_SOURCE,
        _TABLE_TRANSFER_SOURCE,
    }
)
_PROTOCOL_VERSION = "3"
_PERMANENT_IDENTITY_RUNTIME_MODULE_NAMES = (
    "RuntimeContextStoreServer",
    "RuntimeKernelServer",
    "RuntimeTableTransferServer",
    "RuntimeValueTransferServer",
)
_MANAGED_APPLICATION_MODULE_PROPERTY_ID = UUID("d22e852a-cf8a-4f77-8ccb-3548e7792bea")
_COMMON_MODULE_PROPERTY_ID = UUID("d5963243-262e-4398-b4d7-fb16d06484f6")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_HANDSHAKE_ASSIGNMENTS = {
    "product_id": re.compile(r'^\s*ИдентификаторПродуктаRuntime\s*=\s*"([^"]+)";\s*$'),
    "artifact_version": re.compile(r'^\s*ВерсияАртефактаRuntime\s*=\s*"([^"]+)";\s*$'),
    "protocol_version": re.compile(r'^\s*ВерсияПротоколаRuntime\s*=\s*"([^"]+)";\s*$'),
}


@dataclass(frozen=True, slots=True)
class MetadataIdentity:
    name: str
    object_id: UUID


@dataclass(frozen=True, slots=True)
class PermanentExtensionIdentity:
    product_id: str
    extension_name: str
    root_id: UUID
    runtime_module_ids: tuple[UUID, ...]
    purpose: str
    name_prefix: str
    vendor: str


@dataclass(frozen=True, slots=True)
class ExactExtensionArtifact:
    artifact_version: str
    protocol_version: str
    language_bound_by_name: bool
    metadata: tuple[MetadataIdentity, ...]
    source_sha256: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class ExtensionFingerprints:
    identity: PermanentExtensionIdentity
    artifact: ExactExtensionArtifact
    identity_sha256: str
    artifact_sha256: str


@dataclass(frozen=True, slots=True)
class BreakpointContract:
    managed: ModuleLocation
    server_entry: ModuleLocation
    server_service: ModuleLocation


@dataclass(frozen=True, slots=True)
class ExtensionHandshakeEvidence:
    target_type: str
    product_id: str
    artifact_version: str
    protocol_version: str
    location: ModuleLocation


@dataclass(frozen=True, slots=True)
class ExtensionManifest:
    schema_version: int
    product_id: str
    extension_name: str
    artifact_version: str
    protocol_version: str
    cfe_filename: str
    cfe_size: int
    cfe_sha256: str
    fingerprints: ExtensionFingerprints
    breakpoints: BreakpointContract


@dataclass(frozen=True, slots=True)
class ExtensionBundle:
    cfe_path: Path
    manifest: ExtensionManifest
    cache_dir: Path


def _fail(message: str) -> ExtensionBundleError:
    return ExtensionBundleError(message)


def _object(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise _fail(f"{label} must be a JSON object")
    return value


def _exact_keys(value: Mapping[str, object], expected: set[str], *, label: str) -> None:
    unknown = sorted(set(value) - expected)
    missing = sorted(expected - set(value))
    if unknown:
        raise _fail(f"{label} contains unknown properties: {', '.join(unknown)}")
    if missing:
        raise _fail(f"{label} is missing required properties: {', '.join(missing)}")


def _string(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise _fail(f"{label} must be a non-empty string")
    return value


def _integer(value: object, *, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise _fail(f"{label} must be an integer greater than or equal to {minimum}")
    return value


def _boolean(value: object, *, label: str) -> bool:
    if not isinstance(value, bool):
        raise _fail(f"{label} must be a boolean")
    return value


def _uuid(value: object, *, label: str) -> UUID:
    text = _string(value, label=label)
    try:
        parsed = UUID(text)
    except ValueError as error:
        raise _fail(f"{label} must be a valid UUID") from error
    if str(parsed) != text:
        raise _fail(f"{label} must be a canonical lowercase UUID")
    return parsed


def _hash(value: object, *, label: str) -> str:
    text = _string(value, label=label)
    if _SHA256.fullmatch(text) is None:
        raise _fail(f"{label} must be a lowercase SHA-256 digest")
    return text


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _identity_json(identity: PermanentExtensionIdentity) -> dict[str, object]:
    return {
        "product_id": identity.product_id,
        "extension_name": identity.extension_name,
        "root_id": str(identity.root_id),
        "runtime_module_ids": [str(value) for value in identity.runtime_module_ids],
        "purpose": identity.purpose,
        "name_prefix": identity.name_prefix,
        "vendor": identity.vendor,
    }


def _artifact_json(artifact: ExactExtensionArtifact) -> dict[str, object]:
    return {
        "artifact_version": artifact.artifact_version,
        "protocol_version": artifact.protocol_version,
        "language_bound_by_name": artifact.language_bound_by_name,
        "metadata": [
            {"name": item.name, "object_id": str(item.object_id)}
            for item in artifact.metadata
        ],
        "source_sha256": dict(artifact.source_sha256),
    }


def _parse_identity(value: object) -> PermanentExtensionIdentity:
    payload = _object(value, label="fingerprints.identity")
    _exact_keys(
        payload,
        {
            "product_id",
            "extension_name",
            "root_id",
            "runtime_module_ids",
            "purpose",
            "name_prefix",
            "vendor",
        },
        label="fingerprints.identity",
    )
    module_values = payload["runtime_module_ids"]
    if not isinstance(module_values, list) or not module_values:
        raise _fail(
            "fingerprints.identity.runtime_module_ids must be a non-empty array"
        )
    module_ids = tuple(
        _uuid(item, label=f"fingerprints.identity.runtime_module_ids[{index}]")
        for index, item in enumerate(module_values)
    )
    if len(set(module_ids)) != len(module_ids):
        raise _fail("fingerprints.identity.runtime_module_ids contains duplicate UUIDs")
    identity = PermanentExtensionIdentity(
        product_id=_string(
            payload["product_id"], label="fingerprints.identity.product_id"
        ),
        extension_name=_string(
            payload["extension_name"], label="fingerprints.identity.extension_name"
        ),
        root_id=_uuid(payload["root_id"], label="fingerprints.identity.root_id"),
        runtime_module_ids=module_ids,
        purpose=_string(payload["purpose"], label="fingerprints.identity.purpose"),
        name_prefix=_string(
            payload["name_prefix"], label="fingerprints.identity.name_prefix"
        ),
        vendor=_string(payload["vendor"], label="fingerprints.identity.vendor"),
    )
    if identity.product_id != PRODUCT_ID:
        raise _fail("fingerprints.identity.product_id is not this product")
    if identity.extension_name != EXTENSION_NAME:
        raise _fail(
            "fingerprints.identity.extension_name is not the packaged extension"
        )
    if identity.purpose != "AddOn":
        raise _fail("fingerprints.identity.purpose must be AddOn")
    if identity.name_prefix != f"{EXTENSION_NAME}_":
        raise _fail("fingerprints.identity.name_prefix is invalid")
    if identity.vendor != PRODUCT_ID:
        raise _fail("fingerprints.identity.vendor is invalid")
    return identity


def _safe_relative_source(value: object, *, label: str) -> str:
    text = _string(value, label=label)
    path = Path(text)
    if (
        path.is_absolute()
        or path.as_posix() != text
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.suffix.casefold() != ".bsl"
    ):
        raise _fail(f"{label} must be a normalized relative BSL path")
    return text


def _parse_artifact(value: object) -> ExactExtensionArtifact:
    payload = _object(value, label="fingerprints.artifact")
    _exact_keys(
        payload,
        {
            "artifact_version",
            "protocol_version",
            "language_bound_by_name",
            "metadata",
            "source_sha256",
        },
        label="fingerprints.artifact",
    )
    metadata_values = payload["metadata"]
    if not isinstance(metadata_values, list) or not metadata_values:
        raise _fail("fingerprints.artifact.metadata must be a non-empty array")
    metadata: list[MetadataIdentity] = []
    names: set[str] = set()
    for index, raw in enumerate(metadata_values):
        item = _object(raw, label=f"fingerprints.artifact.metadata[{index}]")
        _exact_keys(
            item,
            {"name", "object_id"},
            label=f"fingerprints.artifact.metadata[{index}]",
        )
        name = _string(
            item["name"], label=f"fingerprints.artifact.metadata[{index}].name"
        )
        if name in names:
            raise _fail(
                f"fingerprints.artifact.metadata has duplicate metadata name {name!r}"
            )
        names.add(name)
        metadata.append(
            MetadataIdentity(
                name,
                _uuid(
                    item["object_id"],
                    label=f"fingerprints.artifact.metadata[{index}].object_id",
                ),
            )
        )
    sources = _object(
        payload["source_sha256"], label="fingerprints.artifact.source_sha256"
    )
    source_hashes = tuple(
        sorted(
            (
                _safe_relative_source(
                    name, label="fingerprints.artifact.source_sha256 key"
                ),
                _hash(digest, label=f"fingerprints.artifact.source_sha256[{name!r}]"),
            )
            for name, digest in sources.items()
        )
    )
    if {name for name, _ in source_hashes} != _PROTOCOL_SOURCES:
        raise _fail(
            "fingerprints.artifact.source_sha256 must bind all runtime protocol BSL modules"
        )
    return ExactExtensionArtifact(
        artifact_version=_string(
            payload["artifact_version"], label="fingerprints.artifact.artifact_version"
        ),
        protocol_version=_string(
            payload["protocol_version"], label="fingerprints.artifact.protocol_version"
        ),
        language_bound_by_name=_boolean(
            payload["language_bound_by_name"],
            label="fingerprints.artifact.language_bound_by_name",
        ),
        metadata=tuple(metadata),
        source_sha256=source_hashes,
    )


def _parse_fingerprints(value: object) -> ExtensionFingerprints:
    payload = _object(value, label="fingerprints")
    _exact_keys(
        payload,
        {"identity", "artifact", "identity_sha256", "artifact_sha256"},
        label="fingerprints",
    )
    identity = _parse_identity(payload["identity"])
    artifact = _parse_artifact(payload["artifact"])
    identity_hash = _hash(
        payload["identity_sha256"], label="fingerprints.identity_sha256"
    )
    artifact_hash = _hash(
        payload["artifact_sha256"], label="fingerprints.artifact_sha256"
    )
    if identity_hash != _canonical_sha256(_identity_json(identity)):
        raise _fail("identity fingerprint SHA-256 does not match its contract")
    if artifact_hash != _canonical_sha256(_artifact_json(artifact)):
        raise _fail(
            "artifact fingerprint SHA-256 does not match its source hashes and contract"
        )
    return ExtensionFingerprints(
        identity=identity,
        artifact=artifact,
        identity_sha256=identity_hash,
        artifact_sha256=artifact_hash,
    )


def _parse_location(value: object, *, label: str) -> ModuleLocation:
    payload = _object(value, label=label)
    _exact_keys(
        payload,
        {
            "module_type",
            "url",
            "object_id",
            "property_id",
            "line",
            "extension_name",
            "ext_id",
        },
        label=label,
    )
    url = payload["url"]
    if not isinstance(url, str):
        raise _fail(f"{label}.url must be a string")
    location = ModuleLocation(
        module_type=_string(payload["module_type"], label=f"{label}.module_type"),
        url=url,
        object_id=_uuid(payload["object_id"], label=f"{label}.object_id"),
        property_id=_uuid(payload["property_id"], label=f"{label}.property_id"),
        line=_integer(payload["line"], label=f"{label}.line", minimum=1),
        extension_name=_string(
            payload["extension_name"], label=f"{label}.extension_name"
        ),
        ext_id=_integer(payload["ext_id"], label=f"{label}.ext_id"),
    )
    if location.extension_name != EXTENSION_NAME:
        raise _fail(f"{label}.extension_name is not the packaged extension")
    return location


def _parse_breakpoints(
    value: object, fingerprints: ExtensionFingerprints
) -> BreakpointContract:
    payload = _object(value, label="breakpoints")
    _exact_keys(
        payload,
        {"managed", "server_entry", "server_service"},
        label="breakpoints",
    )
    result = BreakpointContract(
        managed=_parse_location(payload["managed"], label="breakpoints.managed"),
        server_entry=_parse_location(
            payload["server_entry"], label="breakpoints.server_entry"
        ),
        server_service=_parse_location(
            payload["server_service"], label="breakpoints.server_service"
        ),
    )
    if result.managed.module_type != "ExtensionModule":
        raise _fail("breakpoints.managed.module_type must be ExtensionModule")
    if result.managed.object_id != fingerprints.identity.root_id:
        raise _fail("breakpoints.managed.object_id is invalid")
    if result.managed.property_id != _MANAGED_APPLICATION_MODULE_PROPERTY_ID:
        raise _fail("breakpoints.managed.property_id is invalid")
    server_object_ids = {
        item.object_id
        for item in fingerprints.artifact.metadata
        if item.name == "CommonModule.RuntimeKernelServer"
    }
    if len(server_object_ids) != 1:
        raise _fail(
            "fingerprints.artifact.metadata must identify CommonModule.RuntimeKernelServer"
        )
    server_object_id = next(iter(server_object_ids))
    for label, location in (
        ("server_entry", result.server_entry),
        ("server_service", result.server_service),
    ):
        if location.module_type != "ExtensionModule":
            raise _fail(f"breakpoints.{label}.module_type must be ExtensionModule")
        if location.property_id != _COMMON_MODULE_PROPERTY_ID:
            raise _fail(f"breakpoints.{label}.property_id is invalid")
        if location.object_id != server_object_id:
            raise _fail(
                f"breakpoints.{label}.object_id disagrees with RuntimeKernelServer metadata"
            )
        if location.url or location.ext_id != 0:
            raise _fail(f"breakpoints.{label} has invalid URL or extension id")
    if result.managed.url or result.managed.ext_id != 0:
        raise _fail("breakpoints.managed has invalid URL or extension id")
    if (
        result.server_entry.object_id != result.server_service.object_id
        or result.server_entry.property_id != result.server_service.property_id
        or result.server_entry.url != result.server_service.url
        or result.server_entry.ext_id != result.server_service.ext_id
        or result.server_entry.line == result.server_service.line
    ):
        raise _fail("server breakpoint coordinates do not describe one module")
    return result


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _fail(f"extension manifest contains duplicate JSON property {key!r}")
        result[key] = value
    return result


def read_extension_manifest(path: Path) -> ExtensionManifest:
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique_json_object
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _fail(
            f"extension manifest is unreadable or invalid JSON: {path}"
        ) from error
    root = _object(payload, label="extension manifest")
    _exact_keys(
        root,
        {
            "schema_version",
            "product_id",
            "extension_name",
            "artifact_version",
            "protocol_version",
            "cfe_filename",
            "cfe_size",
            "cfe_sha256",
            "fingerprints",
            "breakpoints",
        },
        label="extension manifest",
    )
    schema_version = _integer(root["schema_version"], label="schema_version", minimum=1)
    if schema_version != _MANIFEST_SCHEMA_VERSION:
        raise _fail(f"unsupported extension manifest schema version: {schema_version}")
    product_id = _string(root["product_id"], label="product_id")
    extension_name = _string(root["extension_name"], label="extension_name")
    artifact_version = _string(root["artifact_version"], label="artifact_version")
    protocol_version = _string(root["protocol_version"], label="protocol_version")
    cfe_filename = _string(root["cfe_filename"], label="cfe_filename")
    if product_id != PRODUCT_ID:
        raise _fail("manifest product_id is not this product")
    if extension_name != EXTENSION_NAME:
        raise _fail("manifest extension_name is not the packaged extension")
    if protocol_version != _PROTOCOL_VERSION:
        raise _fail("manifest protocol_version must be protocol 3")
    if cfe_filename != f"{EXTENSION_NAME}.cfe":
        raise _fail("manifest cfe_filename is invalid")
    fingerprints = _parse_fingerprints(root["fingerprints"])
    if (
        fingerprints.identity.product_id != product_id
        or fingerprints.identity.extension_name != extension_name
    ):
        raise _fail(
            "manifest identity fingerprint disagrees with the manifest identity"
        )
    root_metadata_ids = {
        item.object_id
        for item in fingerprints.artifact.metadata
        if item.name == f"Configuration.{extension_name}"
    }
    if root_metadata_ids != {fingerprints.identity.root_id}:
        raise _fail(
            "fingerprints.identity.root_id disagrees with the exact metadata artifact"
        )
    identity_metadata_names = {
        f"CommonModule.{name}" for name in _PERMANENT_IDENTITY_RUNTIME_MODULE_NAMES
    }
    identity_metadata = {
        item.name: item.object_id
        for item in fingerprints.artifact.metadata
        if item.name in identity_metadata_names
    }
    expected_identity_module_count = len(_PERMANENT_IDENTITY_RUNTIME_MODULE_NAMES)
    identity_metadata_ids = tuple(identity_metadata.values())
    identity_runtime_ids = fingerprints.identity.runtime_module_ids
    if (
        len(identity_metadata) != expected_identity_module_count
        or set(identity_metadata) != identity_metadata_names
        or len(set(identity_metadata_ids)) != expected_identity_module_count
        or len(identity_runtime_ids) != expected_identity_module_count
        or len(set(identity_runtime_ids)) != expected_identity_module_count
        or set(identity_metadata_ids) != set(identity_runtime_ids)
    ):
        raise _fail(
            "fingerprints.identity.runtime_module_ids disagree with the exact metadata artifact"
        )
    if (
        fingerprints.artifact.artifact_version != artifact_version
        or fingerprints.artifact.protocol_version != protocol_version
    ):
        raise _fail(
            "manifest artifact fingerprint disagrees with the manifest versions"
        )
    if fingerprints.artifact.language_bound_by_name:
        raise _fail("manifest artifact must not control the infobase language")
    return ExtensionManifest(
        schema_version=schema_version,
        product_id=product_id,
        extension_name=extension_name,
        artifact_version=artifact_version,
        protocol_version=protocol_version,
        cfe_filename=cfe_filename,
        cfe_size=_integer(root["cfe_size"], label="cfe_size"),
        cfe_sha256=_hash(root["cfe_sha256"], label="cfe_sha256"),
        fingerprints=fingerprints,
        breakpoints=_parse_breakpoints(root["breakpoints"], fingerprints),
    )


def _parse_xml(path: Path, *, label: str) -> ElementTree.Element:
    try:
        return ElementTree.parse(path).getroot()
    except (OSError, ElementTree.ParseError) as error:
        raise _fail(f"{label} is missing or malformed: {path}") from error


def _required_text(parent: ElementTree.Element, name: str, *, label: str) -> str:
    value = parent.findtext(f"{{{_MD_NAMESPACE}}}{name}")
    if value is None or not value:
        raise _fail(f"{label}.{name} is missing")
    return value


def _metadata_inventory(source_dir: Path) -> tuple[MetadataIdentity, ...]:
    root = _parse_xml(source_dir / "ConfigDumpInfo.xml", label="ConfigDumpInfo.xml")
    values: list[MetadataIdentity] = []
    names: set[str] = set()
    for index, item in enumerate(
        root.findall(
            f"{{{_DUMP_NAMESPACE}}}ConfigVersions/{{{_DUMP_NAMESPACE}}}Metadata"
        )
    ):
        name = item.attrib.get("name", "")
        raw_id = item.attrib.get("id", "")
        if not name or not raw_id:
            raise _fail(f"ConfigDumpInfo.xml metadata entry {index} is incomplete")
        if name in names:
            raise _fail(f"ConfigDumpInfo.xml contains duplicate metadata name {name!r}")
        names.add(name)
        try:
            object_id = UUID(raw_id.split(".", 1)[0])
        except ValueError as error:
            raise _fail(
                f"ConfigDumpInfo.xml metadata {name!r} has an invalid UUID"
            ) from error
        values.append(MetadataIdentity(name, object_id))
    if not values:
        raise _fail("ConfigDumpInfo.xml contains no metadata inventory")
    return tuple(sorted(values, key=lambda value: value.name))


def _read_bsl(path: Path) -> str:
    try:
        return (
            path.read_text(encoding="utf-8-sig")
            .replace("\r\n", "\n")
            .replace("\r", "\n")
        )
    except (OSError, UnicodeDecodeError) as error:
        raise _fail(
            f"runtime BSL module is missing or invalid UTF-8: {path}"
        ) from error


def _handshake(source: str, *, label: str) -> tuple[str, str, str]:
    values: dict[str, str] = {}
    for field, pattern in _HANDSHAKE_ASSIGNMENTS.items():
        matches = [
            match.group(1)
            for line in source.splitlines()
            if (match := pattern.fullmatch(line))
        ]
        if len(matches) != 1:
            raise _fail(f"{label} handshake must assign {field} exactly once")
        values[field] = matches[0]
    return values["product_id"], values["artifact_version"], values["protocol_version"]


def _marker_line(source: str, marker: str, *, label: str) -> int:
    matches = [
        line_number
        for line_number, line in enumerate(source.splitlines(), start=1)
        if marker in line
    ]
    if len(matches) != 1:
        raise _fail(f"{label} must contain exactly one {marker} marker")
    return matches[0]


def _metadata_id(inventory: tuple[MetadataIdentity, ...], name: str) -> UUID:
    matches = [item.object_id for item in inventory if item.name == name]
    if len(matches) != 1:
        raise _fail(f"ConfigDumpInfo.xml must contain metadata {name!r} exactly once")
    return matches[0]


def fingerprint_extension_dump(
    source_dir: Path,
    *,
    expected_product_id: str | None = PRODUCT_ID,
) -> ExtensionFingerprints:
    configuration_root = _parse_xml(
        source_dir / "Configuration.xml", label="Configuration.xml"
    )
    configuration = configuration_root.find(f"{{{_MD_NAMESPACE}}}Configuration")
    if configuration is None:
        raise _fail("Configuration.xml has no Configuration object")
    properties = configuration.find(f"{{{_MD_NAMESPACE}}}Properties")
    if properties is None:
        raise _fail("Configuration.xml has no Configuration.Properties")
    extension_name = _required_text(
        properties, "Name", label="Configuration.Properties"
    )
    purpose = _required_text(
        properties, "ConfigurationExtensionPurpose", label="Configuration.Properties"
    )
    keep_mapping = _required_text(
        properties,
        "KeepMappingToExtendedConfigurationObjectsByIDs",
        label="Configuration.Properties",
    )
    name_prefix = _required_text(
        properties, "NamePrefix", label="Configuration.Properties"
    )
    vendor = _required_text(properties, "Vendor", label="Configuration.Properties")
    artifact_version = _required_text(
        properties, "Version", label="Configuration.Properties"
    )
    belonging = _required_text(
        properties, "ObjectBelonging", label="Configuration.Properties"
    )
    if extension_name != EXTENSION_NAME:
        raise _fail(f"extension name must be {EXTENSION_NAME}")
    if belonging == "Customization" or belonging != "Adopted":
        raise _fail("configuration ObjectBelonging must be Adopted, not Customization")
    if purpose != "AddOn":
        raise _fail("configuration extension purpose must be AddOn")
    if keep_mapping != "false":
        raise _fail("configuration KeepMapping must be false")
    if name_prefix != f"{EXTENSION_NAME}_":
        raise _fail("configuration NamePrefix is invalid")
    if expected_product_id is not None and vendor != expected_product_id:
        raise _fail("configuration Vendor is invalid")
    controlled_properties = (
        "DefaultLanguage",
        "InterfaceCompatibilityMode",
    )
    if any(
        properties.find(f"{{{_MD_NAMESPACE}}}{name}") is not None
        for name in controlled_properties
    ):
        raise _fail(
            "configuration must not control DefaultLanguage or "
            "InterfaceCompatibilityMode"
        )
    try:
        root_id = UUID(configuration.attrib["uuid"])
    except (KeyError, ValueError) as error:
        raise _fail("Configuration.xml root uuid must be a valid UUID") from error

    inventory = _metadata_inventory(source_dir)
    if _metadata_id(inventory, f"Configuration.{extension_name}") != root_id:
        raise _fail("Configuration root UUID disagrees with ConfigDumpInfo.xml")
    if any(item.name.startswith("Language.") for item in inventory):
        raise _fail("ConfigDumpInfo.xml must not contain language metadata")

    child_objects = configuration.find(f"{{{_MD_NAMESPACE}}}ChildObjects")
    if child_objects is None:
        raise _fail("Configuration.xml has no Configuration.ChildObjects")
    permitted_child_tags = {f"{{{_MD_NAMESPACE}}}CommonModule"}
    for child in child_objects:
        if child.tag not in permitted_child_tags:
            child_kind = child.tag.rsplit("}", 1)[-1]
            raise _fail(
                f"Configuration.ChildObjects child object kind {child_kind!r} is not permitted"
            )
    module_names = [
        item.text
        for item in child_objects.findall(f"{{{_MD_NAMESPACE}}}CommonModule")
        if item.text
    ]
    if len(set(module_names)) != len(module_names):
        raise _fail("Configuration.xml contains duplicate CommonModule children")
    missing_identity_modules = [
        name
        for name in _PERMANENT_IDENTITY_RUNTIME_MODULE_NAMES
        if name not in module_names
    ]
    if missing_identity_modules:
        raise _fail(
            "Configuration.xml is missing fixed identity runtime module(s): "
            + ", ".join(missing_identity_modules)
        )
    owned_module_ids = {
        name: _metadata_id(inventory, f"CommonModule.{name}") for name in module_names
    }
    if len(set(owned_module_ids.values())) != len(owned_module_ids):
        raise _fail("owned common modules must have distinct UUIDs")
    runtime_module_ids = tuple(
        sorted(
            (
                owned_module_ids[name]
                for name in _PERMANENT_IDENTITY_RUNTIME_MODULE_NAMES
            ),
            key=str,
        )
    )

    managed_source = _read_bsl(source_dir / Path(_MANAGED_SOURCE))
    server_source = _read_bsl(source_dir / Path(_SERVER_SOURCE))
    managed_handshake = _handshake(managed_source, label="managed runtime module")
    server_handshake = _handshake(server_source, label="server runtime module")
    if managed_handshake != server_handshake:
        raise _fail("managed and server runtime handshake assignments disagree")
    if managed_handshake[0] != vendor:
        raise _fail("runtime handshake product_id disagrees with Vendor")
    if expected_product_id is not None and managed_handshake[0] != expected_product_id:
        raise _fail("runtime handshake product_id is invalid")
    if managed_handshake[1] != artifact_version:
        raise _fail(
            "runtime handshake artifact version disagrees with Configuration.xml"
        )
    protocol_version = managed_handshake[2]

    source_hashes = tuple(
        sorted(
            (
                relative,
                sha256(_read_bsl(source_dir / Path(relative)).encode("utf-8")).hexdigest(),
            )
            for relative in _PROTOCOL_SOURCES
        )
    )
    identity = PermanentExtensionIdentity(
        product_id=managed_handshake[0],
        extension_name=extension_name,
        root_id=root_id,
        runtime_module_ids=runtime_module_ids,
        purpose=purpose,
        name_prefix=name_prefix,
        vendor=vendor,
    )
    artifact = ExactExtensionArtifact(
        artifact_version=artifact_version,
        protocol_version=protocol_version,
        language_bound_by_name=False,
        metadata=inventory,
        source_sha256=source_hashes,
    )
    return ExtensionFingerprints(
        identity=identity,
        artifact=artifact,
        identity_sha256=_canonical_sha256(_identity_json(identity)),
        artifact_sha256=_canonical_sha256(_artifact_json(artifact)),
    )


def _verify_cfe(path: Path, manifest: ExtensionManifest) -> None:
    try:
        size = path.stat().st_size
    except OSError as error:
        raise _fail(f"extension CFE is unreadable: {path}") from error
    if size != manifest.cfe_size:
        raise _fail(f"extension CFE byte count is {size}, expected {manifest.cfe_size}")
    digest = sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise _fail(f"extension CFE is unreadable: {path}") from error
    if digest.hexdigest() != manifest.cfe_sha256:
        raise _fail("extension CFE SHA-256 does not match the manifest")


def _fsync_copy(source: Path, destination: Path) -> None:
    try:
        with source.open("rb") as input_stream, destination.open("xb") as output_stream:
            shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
            output_stream.flush()
            os.fsync(output_stream.fileno())
    except OSError as error:
        raise _fail(
            f"could not materialize packaged extension resource: {source}"
        ) from error


def _valid_cached_bundle(cache_dir: Path, manifest: ExtensionManifest) -> bool:
    cfe_path = cache_dir / manifest.cfe_filename
    cached_manifest_path = cache_dir / _MANIFEST_FILENAME
    try:
        cached = read_extension_manifest(cached_manifest_path)
        _verify_cfe(cfe_path, cached)
    except ExtensionBundleError:
        return False
    return cached == manifest


def materialize_extension_bundle(
    cfe: Path, manifest: Path, runtime_dir: Path
) -> ExtensionBundle:
    parsed = read_extension_manifest(manifest)
    if cfe.name != parsed.cfe_filename:
        raise _fail(
            f"extension CFE filename {cfe.name!r} does not match manifest filename {parsed.cfe_filename!r}"
        )
    _verify_cfe(cfe, parsed)
    cache_parent = runtime_dir / "cache" / "extensions"
    cache_dir = cache_parent / parsed.cfe_sha256
    if cache_dir.is_dir():
        if not _valid_cached_bundle(cache_dir, parsed):
            raise _fail(f"extension bundle cache entry is corrupt: {cache_dir}")
        return ExtensionBundle(cache_dir / parsed.cfe_filename, parsed, cache_dir)

    cache_parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{parsed.cfe_sha256}-", dir=cache_parent)
    )
    try:
        staged_cfe = temporary / parsed.cfe_filename
        staged_manifest = temporary / _MANIFEST_FILENAME
        _fsync_copy(cfe, staged_cfe)
        _fsync_copy(manifest, staged_manifest)
        staged_parsed = read_extension_manifest(staged_manifest)
        _verify_cfe(staged_cfe, staged_parsed)
        if staged_parsed != parsed:
            raise _fail("materialized extension manifest changed during copying")
        try:
            os.replace(temporary, cache_dir)
        except OSError as error:
            if not cache_dir.is_dir() or not _valid_cached_bundle(cache_dir, parsed):
                raise _fail(
                    f"could not atomically publish extension bundle: {cache_dir}"
                ) from error
        return ExtensionBundle(cache_dir / parsed.cfe_filename, parsed, cache_dir)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def packaged_extension_bundle(runtime_dir: Path) -> ExtensionBundle:
    root = importlib.resources.files("onec_runtime").joinpath("resources", "extension")
    try:
        with importlib.resources.as_file(root) as resource_root:
            return materialize_extension_bundle(
                resource_root / f"{EXTENSION_NAME}.cfe",
                resource_root / _MANIFEST_FILENAME,
                runtime_dir,
            )
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError) as error:
        raise _fail("packaged runtime extension resources are missing") from error


__all__ = [
    "EXTENSION_NAME",
    "PRODUCT_ID",
    "BreakpointContract",
    "ExactExtensionArtifact",
    "ExtensionBundle",
    "ExtensionFingerprints",
    "ExtensionHandshakeEvidence",
    "ExtensionManifest",
    "MetadataIdentity",
    "PermanentExtensionIdentity",
    "fingerprint_extension_dump",
    "materialize_extension_bundle",
    "packaged_extension_bundle",
    "read_extension_manifest",
]
