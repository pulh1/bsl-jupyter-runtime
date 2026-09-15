from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from hashlib import sha256
from pathlib import Path

import pytest
from extension_bundle_support import (
    TABLE_MODULE_ID,
    VALUE_MODULE_ID,
    marker_line,
    write_dump_fixture,
    write_manifest_fixture,
)

from onec_runtime.errors import ExtensionBundleError
from onec_runtime.extension_bundle import (
    fingerprint_extension_dump,
    materialize_extension_bundle,
    packaged_extension_bundle,
    read_extension_manifest,
)


def _rewrite_json(path: Path, mutate: object) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert callable(mutate)
    mutate(payload)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _canonical_sha256(value: object) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _add_runtime_module(dump: Path, name: str, object_id: str) -> None:
    configuration = dump / "Configuration.xml"
    configuration_source = configuration.read_text(encoding="utf-8")
    assert configuration_source.count("</ChildObjects>") == 1
    configuration.write_text(
        configuration_source.replace(
            "</ChildObjects>", f"<CommonModule>{name}</CommonModule></ChildObjects>"
        ),
        encoding="utf-8",
    )
    dump_info = dump / "ConfigDumpInfo.xml"
    inventory_source = dump_info.read_text(encoding="utf-8")
    assert inventory_source.count("  </ConfigVersions>") == 1
    inventory = (
        f'    <Metadata name="CommonModule.{name}" id="{object_id}" />\n'
        f'    <Metadata name="CommonModule.{name}.Module" id="{object_id}.0" />\n'
    )
    dump_info.write_text(
        inventory_source.replace(
            "  </ConfigVersions>", inventory + "  </ConfigVersions>"
        ),
        encoding="utf-8",
    )


def test_dump_fingerprint_separates_permanent_identity_from_exact_artifact(
    tmp_path: Path,
) -> None:
    dump = write_dump_fixture(
        tmp_path,
        extension_name="OnecInteractiveRuntime",
        artifact_version="0.1.0",
        protocol_version="1",
    )

    fingerprints = fingerprint_extension_dump(dump)

    assert fingerprints.identity.product_id == "onec-interactive-runtime"
    assert fingerprints.identity.extension_name == "OnecInteractiveRuntime"
    assert fingerprints.identity.purpose == "AddOn"
    assert fingerprints.artifact.language_bound_by_name is False
    assert fingerprints.artifact.protocol_version == "1"
    assert fingerprints.artifact.metadata
    assert fingerprints.artifact.source_sha256 == tuple(
        sorted(fingerprints.artifact.source_sha256)
    )


def test_extra_runtime_module_changes_exact_artifact_but_not_permanent_identity(
    tmp_path: Path,
) -> None:
    dump = write_dump_fixture(tmp_path)
    before = fingerprint_extension_dump(dump)
    _add_runtime_module(
        dump,
        "RuntimeDiagnosticsServer",
        "88888888-8888-4888-8888-888888888888",
    )

    after = fingerprint_extension_dump(dump)

    assert after.identity == before.identity
    assert after.identity_sha256 == before.identity_sha256
    assert after.artifact != before.artifact
    assert after.artifact_sha256 != before.artifact_sha256
    assert {item.name for item in after.artifact.metadata} - {
        item.name for item in before.artifact.metadata
    } == {
        "CommonModule.RuntimeDiagnosticsServer",
        "CommonModule.RuntimeDiagnosticsServer.Module",
    }


def test_dump_fingerprint_requires_every_fixed_identity_runtime_module(
    tmp_path: Path,
) -> None:
    dump = write_dump_fixture(tmp_path)
    configuration = dump / "Configuration.xml"
    source = configuration.read_text(encoding="utf-8")
    declaration = "<CommonModule>RuntimeTableTransferServer</CommonModule>"
    assert source.count(declaration) == 1
    configuration.write_text(source.replace(declaration, ""), encoding="utf-8")

    with pytest.raises(ExtensionBundleError, match="fixed identity runtime module"):
        fingerprint_extension_dump(dump)


def test_fixed_identity_runtime_module_uuid_changes_permanent_identity(
    tmp_path: Path,
) -> None:
    dump = write_dump_fixture(tmp_path)
    before = fingerprint_extension_dump(dump)
    dump_info = dump / "ConfigDumpInfo.xml"
    source = dump_info.read_text(encoding="utf-8")
    replacement = "99999999-9999-4999-8999-999999999999"
    assert source.count(TABLE_MODULE_ID) == 2
    dump_info.write_text(
        source.replace(TABLE_MODULE_ID, replacement),
        encoding="utf-8",
    )

    after = fingerprint_extension_dump(dump)

    assert after.identity != before.identity
    assert after.identity_sha256 != before.identity_sha256


def test_dump_fingerprint_can_inspect_a_consistent_foreign_product(
    tmp_path: Path,
) -> None:
    dump = write_dump_fixture(tmp_path)
    configuration = dump / "Configuration.xml"
    source = configuration.read_text(encoding="utf-8")
    assert source.count("<Vendor>onec-interactive-runtime</Vendor>") == 1
    configuration.write_text(
        source.replace(
            "<Vendor>onec-interactive-runtime</Vendor>",
            "<Vendor>foreign-runtime-product</Vendor>",
        ),
        encoding="utf-8",
    )
    declaration = 'ИдентификаторПродуктаRuntime = "onec-interactive-runtime";'
    replacement = 'ИдентификаторПродуктаRuntime = "foreign-runtime-product";'
    for relative in (
        Path("Ext/ManagedApplicationModule.bsl"),
        Path("CommonModules/RuntimeKernelServer/Ext/Module.bsl"),
    ):
        module = dump / relative
        source = module.read_text(encoding="utf-8-sig")
        assert source.count(declaration) == 1
        module.write_text(
            source.replace(declaration, replacement),
            encoding="utf-8-sig",
        )

    with pytest.raises(ExtensionBundleError, match="Vendor"):
        fingerprint_extension_dump(dump)
    inspected = fingerprint_extension_dump(dump, expected_product_id=None)

    assert inspected.identity.product_id == "foreign-runtime-product"
    assert inspected.identity.vendor == "foreign-runtime-product"


def test_dump_fingerprint_normalizes_bsl_line_endings(tmp_path: Path) -> None:
    dump = write_dump_fixture(tmp_path)
    before = fingerprint_extension_dump(dump)
    module = dump / "Ext" / "ManagedApplicationModule.bsl"
    source = module.read_text(encoding="utf-8-sig")
    module.write_text(source.replace("\n", "\r\n"), encoding="utf-8-sig", newline="")

    after = fingerprint_extension_dump(dump)

    assert after.artifact.source_sha256 == before.artifact.source_sha256
    assert after.artifact_sha256 == before.artifact_sha256


@pytest.mark.parametrize(
    "relative",
    [
        Path("CommonModules/RuntimeTableTransferServer/Ext/Module.bsl"),
        Path("CommonModules/RuntimeValueTransferServer/Ext/Module.bsl"),
    ],
)
def test_dump_fingerprint_binds_each_protocol_serializer(
    tmp_path: Path, relative: Path
) -> None:
    dump = write_dump_fixture(tmp_path)
    before = fingerprint_extension_dump(dump)
    module = dump / relative
    module.write_text(
        module.read_text(encoding="utf-8-sig") + "\n// incompatible serializer change\n",
        encoding="utf-8-sig",
        newline="",
    )

    after = fingerprint_extension_dump(dump)

    assert after.artifact.source_sha256 != before.artifact.source_sha256
    assert after.artifact_sha256 != before.artifact_sha256


def test_manifest_parser_rejects_predecessor_protocol_one(tmp_path: Path) -> None:
    path = write_manifest_fixture(
        tmp_path,
        cfe_sha256="0" * 64,
        protocol_version="1",
    )

    with pytest.raises(ExtensionBundleError, match="protocol 2"):
        read_extension_manifest(path)


@pytest.mark.parametrize(
    ("property_name", "value", "message"),
    [
        ("ConfigurationExtensionPurpose", "Customization", "purpose"),
        ("KeepMappingToExtendedConfigurationObjectsByIDs", "true", "KeepMapping"),
    ],
)
def test_dump_fingerprint_rejects_non_universal_configuration_properties(
    tmp_path: Path,
    property_name: str,
    value: str,
    message: str,
) -> None:
    dump = write_dump_fixture(tmp_path)
    config = dump / "Configuration.xml"
    source = config.read_text(encoding="utf-8")
    start = f"<{property_name}>"
    end = f"</{property_name}>"
    prefix, tail = source.split(start, 1)
    _, suffix = tail.split(end, 1)
    config.write_text(f"{prefix}{start}{value}{end}{suffix}", encoding="utf-8")

    with pytest.raises(ExtensionBundleError, match=message):
        fingerprint_extension_dump(dump)


@pytest.mark.parametrize(
    "controlled_property",
    ("DefaultLanguage", "InterfaceCompatibilityMode"),
)
def test_dump_fingerprint_rejects_infobase_controlled_properties(
    tmp_path: Path, controlled_property: str
) -> None:
    dump = write_dump_fixture(tmp_path)
    config = dump / "Configuration.xml"
    source = config.read_text(encoding="utf-8")
    config.write_text(
        source.replace(
            "</Properties>",
            f"<{controlled_property}>controlled</{controlled_property}></Properties>",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ExtensionBundleError, match="must not control"):
        fingerprint_extension_dump(dump)


def test_dump_fingerprint_rejects_owned_catalog_child(tmp_path: Path) -> None:
    dump = write_dump_fixture(tmp_path)
    config = dump / "Configuration.xml"
    source = config.read_text(encoding="utf-8")
    config.write_text(
        source.replace(
            "</ChildObjects>", "<Catalog>OwnedData</Catalog></ChildObjects>"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ExtensionBundleError, match="child object kind.*Catalog"):
        fingerprint_extension_dump(dump)


def test_dump_fingerprint_rejects_language_child(tmp_path: Path) -> None:
    dump = write_dump_fixture(tmp_path)
    config = dump / "Configuration.xml"
    source = config.read_text(encoding="utf-8")
    config.write_text(
        source.replace("</ChildObjects>", "<Language>Русский</Language></ChildObjects>"),
        encoding="utf-8",
    )

    with pytest.raises(ExtensionBundleError, match="child object kind.*Language"):
        fingerprint_extension_dump(dump)


def test_dump_fingerprint_rejects_duplicate_metadata_names(tmp_path: Path) -> None:
    dump = write_dump_fixture(tmp_path)
    info = dump / "ConfigDumpInfo.xml"
    source = info.read_text(encoding="utf-8")
    duplicate = (
        '    <Metadata name="CommonModule.RuntimeKernelServer" '
        'id="66666666-6666-4666-8666-666666666666" />\n'
    )
    info.write_text(
        source.replace("  </ConfigVersions>", duplicate + "  </ConfigVersions>"),
        encoding="utf-8",
    )

    with pytest.raises(ExtensionBundleError, match="duplicate metadata name"):
        fingerprint_extension_dump(dump)


def test_dump_fingerprint_rejects_handshake_disagreement(tmp_path: Path) -> None:
    dump = write_dump_fixture(tmp_path)
    server = dump / "CommonModules" / "RuntimeKernelServer" / "Ext" / "Module.bsl"
    source = server.read_text(encoding="utf-8-sig")
    server.write_text(
        source.replace(
            'ВерсияПротоколаRuntime = "1";', 'ВерсияПротоколаRuntime = "2";'
        ),
        encoding="utf-8-sig",
    )

    with pytest.raises(ExtensionBundleError, match="handshake"):
        fingerprint_extension_dump(dump)


def test_manifest_parser_rejects_unknown_json_keys(tmp_path: Path) -> None:
    path = write_manifest_fixture(tmp_path, cfe_sha256="0" * 64)
    _rewrite_json(path, lambda payload: payload.update({"surprise": True}))

    with pytest.raises(ExtensionBundleError, match="unknown.*surprise"):
        read_extension_manifest(path)


def test_manifest_parser_rejects_duplicate_json_properties(tmp_path: Path) -> None:
    path = write_manifest_fixture(tmp_path, cfe_sha256="0" * 64)
    source = path.read_text(encoding="utf-8")
    path.write_text(
        source.replace(
            '"schema_version": 1,', '"schema_version": 1,\n  "schema_version": 1,'
        ),
        encoding="utf-8",
    )

    with pytest.raises(ExtensionBundleError, match="duplicate JSON property"):
        read_extension_manifest(path)


def test_manifest_parser_rejects_invalid_uuid(tmp_path: Path) -> None:
    path = write_manifest_fixture(tmp_path, cfe_sha256="0" * 64)
    _rewrite_json(
        path,
        lambda payload: payload["fingerprints"]["identity"].update(
            {"root_id": "not-a-uuid"}
        ),
    )

    with pytest.raises(ExtensionBundleError, match="root_id.*UUID"):
        read_extension_manifest(path)


def test_manifest_parser_rejects_permanent_identity_artifact_disagreement(
    tmp_path: Path,
) -> None:
    path = write_manifest_fixture(tmp_path, cfe_sha256="0" * 64)

    def replace_root_identity(payload: dict[str, object]) -> None:
        fingerprints = payload["fingerprints"]
        assert isinstance(fingerprints, dict)
        identity = fingerprints["identity"]
        assert isinstance(identity, dict)
        identity["root_id"] = "88888888-8888-4888-8888-888888888888"
        fingerprints["identity_sha256"] = sha256(
            json.dumps(
                identity,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    _rewrite_json(path, replace_root_identity)

    with pytest.raises(ExtensionBundleError, match="root_id"):
        read_extension_manifest(path)


def test_manifest_parser_allows_non_identity_runtime_module_inventory_changes(
    tmp_path: Path,
) -> None:
    path = write_manifest_fixture(tmp_path, cfe_sha256="0" * 64)

    def add_non_identity_module(payload: dict[str, object]) -> None:
        fingerprints = payload["fingerprints"]
        assert isinstance(fingerprints, dict)
        artifact = fingerprints["artifact"]
        assert isinstance(artifact, dict)
        metadata = artifact["metadata"]
        assert isinstance(metadata, list)
        metadata.extend(
            [
                {
                    "name": "CommonModule.RuntimeDiagnosticsServer",
                    "object_id": "88888888-8888-4888-8888-888888888888",
                },
                {
                    "name": "CommonModule.RuntimeDiagnosticsServer.Module",
                    "object_id": "88888888-8888-4888-8888-888888888888",
                },
            ]
        )
        fingerprints["artifact_sha256"] = _canonical_sha256(artifact)

    _rewrite_json(path, add_non_identity_module)

    manifest = read_extension_manifest(path)

    assert manifest.fingerprints.identity.runtime_module_ids
    assert any(
        item.name == "CommonModule.RuntimeDiagnosticsServer"
        for item in manifest.fingerprints.artifact.metadata
    )


def test_manifest_parser_rejects_fixed_identity_runtime_module_disagreement(
    tmp_path: Path,
) -> None:
    path = write_manifest_fixture(tmp_path, cfe_sha256="0" * 64)

    def replace_fixed_identity_id(payload: dict[str, object]) -> None:
        fingerprints = payload["fingerprints"]
        assert isinstance(fingerprints, dict)
        identity = fingerprints["identity"]
        assert isinstance(identity, dict)
        runtime_ids = identity["runtime_module_ids"]
        assert isinstance(runtime_ids, list)
        runtime_ids[0] = "99999999-9999-4999-8999-999999999999"
        fingerprints["identity_sha256"] = _canonical_sha256(identity)

    _rewrite_json(path, replace_fixed_identity_id)

    with pytest.raises(ExtensionBundleError, match="runtime_module_ids"):
        read_extension_manifest(path)


def test_manifest_parser_rejects_duplicate_uuid_across_fixed_identity_modules(
    tmp_path: Path,
) -> None:
    path = write_manifest_fixture(tmp_path, cfe_sha256="0" * 64)

    def duplicate_fixed_identity_id(payload: dict[str, object]) -> None:
        fingerprints = payload["fingerprints"]
        assert isinstance(fingerprints, dict)
        identity = fingerprints["identity"]
        assert isinstance(identity, dict)
        runtime_ids = identity["runtime_module_ids"]
        assert isinstance(runtime_ids, list)
        assert VALUE_MODULE_ID in runtime_ids
        runtime_ids.remove(VALUE_MODULE_ID)
        artifact = fingerprints["artifact"]
        assert isinstance(artifact, dict)
        metadata = artifact["metadata"]
        assert isinstance(metadata, list)
        value_module = next(
            item
            for item in metadata
            if isinstance(item, dict)
            and item.get("name") == "CommonModule.RuntimeValueTransferServer"
        )
        value_module["object_id"] = TABLE_MODULE_ID
        fingerprints["identity_sha256"] = _canonical_sha256(identity)
        fingerprints["artifact_sha256"] = _canonical_sha256(artifact)

    _rewrite_json(path, duplicate_fixed_identity_id)

    with pytest.raises(ExtensionBundleError, match="runtime_module_ids"):
        read_extension_manifest(path)


@pytest.mark.parametrize(
    "breakpoint",
    ["managed", "server_entry"],
)
def test_manifest_parser_rejects_breakpoint_object_identity_disagreement(
    tmp_path: Path, breakpoint: str
) -> None:
    path = write_manifest_fixture(tmp_path, cfe_sha256="0" * 64)
    _rewrite_json(
        path,
        lambda payload: payload["breakpoints"][breakpoint].update(
            {"object_id": "77777777-7777-4777-8777-777777777777"}
        ),
    )

    with pytest.raises(ExtensionBundleError, match="object_id"):
        read_extension_manifest(path)


def test_manifest_parser_rejects_source_hash_disagreement(tmp_path: Path) -> None:
    path = write_manifest_fixture(tmp_path, cfe_sha256="0" * 64)
    _rewrite_json(
        path,
        lambda payload: payload["fingerprints"]["artifact"]["source_sha256"].update(
            {"Ext/ManagedApplicationModule.bsl": "c" * 64}
        ),
    )

    with pytest.raises(ExtensionBundleError, match="artifact fingerprint SHA-256"):
        read_extension_manifest(path)


def test_manifest_parser_returns_immutable_typed_contract(tmp_path: Path) -> None:
    path = write_manifest_fixture(tmp_path, cfe_sha256="0" * 64)

    manifest = read_extension_manifest(path)

    assert manifest.schema_version == 1
    assert manifest.breakpoints.managed.extension_name == "OnecInteractiveRuntime"
    with pytest.raises(FrozenInstanceError):
        manifest.schema_version = 2  # type: ignore[misc]


def test_materialization_rejects_cfe_hash_mismatch(tmp_path: Path) -> None:
    cfe = tmp_path / "OnecInteractiveRuntime.cfe"
    cfe.write_bytes(b"wrong")
    manifest = write_manifest_fixture(
        tmp_path,
        cfe_sha256="0" * 64,
        cfe_size=5,
    )

    with pytest.raises(ExtensionBundleError, match="SHA-256"):
        materialize_extension_bundle(cfe, manifest, tmp_path / ".runtime")

    assert not (tmp_path / ".runtime" / "cache" / "extensions").exists()


def test_materialization_rejects_cfe_byte_count_before_cache_publish(
    tmp_path: Path,
) -> None:
    cfe = tmp_path / "OnecInteractiveRuntime.cfe"
    cfe.write_bytes(b"cfe")
    manifest = write_manifest_fixture(
        tmp_path,
        cfe_sha256=sha256(b"cfe").hexdigest(),
        cfe_size=4,
    )

    with pytest.raises(ExtensionBundleError, match="byte count"):
        materialize_extension_bundle(cfe, manifest, tmp_path / ".runtime")

    assert not (tmp_path / ".runtime" / "cache" / "extensions").exists()


def test_materialization_publishes_validated_pair_at_stable_cache_path(
    tmp_path: Path,
) -> None:
    cfe = tmp_path / "OnecInteractiveRuntime.cfe"
    cfe.write_bytes(b"cfe")
    digest = sha256(b"cfe").hexdigest()
    manifest_path = write_manifest_fixture(
        tmp_path,
        cfe_sha256=digest,
        cfe_size=3,
    )

    bundle = materialize_extension_bundle(cfe, manifest_path, tmp_path / ".runtime")

    assert bundle.cache_dir == tmp_path / ".runtime" / "cache" / "extensions" / digest
    assert bundle.cfe_path.read_bytes() == b"cfe"
    assert bundle.cfe_path.name == "OnecInteractiveRuntime.cfe"
    assert (
        bundle.cache_dir / "extension-manifest.json"
    ).read_bytes() == manifest_path.read_bytes()
    assert not list(bundle.cache_dir.parent.glob(f".{digest}-*"))


def test_packaged_bundle_materializes_only_import_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "installed-package"
    resources = package / "resources" / "extension"
    resources.mkdir(parents=True)
    cfe = resources / "OnecInteractiveRuntime.cfe"
    cfe.write_bytes(b"cfe")
    digest = sha256(b"cfe").hexdigest()
    source_manifest = write_manifest_fixture(
        tmp_path,
        cfe_sha256=digest,
        cfe_size=3,
    )
    source_manifest.replace(resources / "extension-manifest.json")
    requested_packages: list[str] = []

    def package_files(name: str) -> Path:
        requested_packages.append(name)
        return package

    monkeypatch.setattr(
        "onec_runtime.extension_bundle.importlib.resources.files", package_files
    )

    bundle = packaged_extension_bundle(tmp_path / ".runtime")

    assert requested_packages == ["onec_runtime"]
    assert bundle.cfe_path.read_bytes() == b"cfe"
    assert bundle.cache_dir.is_relative_to(tmp_path / ".runtime")


def test_marker_line_requires_one_unique_marker(tmp_path: Path) -> None:
    module = tmp_path / "Module.bsl"
    module.write_text("one // @marker\ntwo\n", encoding="utf-8")
    assert marker_line(module, "@marker") == 1
    module.write_text("one // @marker\ntwo // @marker\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="exactly one"):
        marker_line(module, "@marker")
