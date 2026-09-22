from pathlib import Path
from xml.etree import ElementTree

from onec_runtime.extension_bundle import (
    fingerprint_extension_dump,
    materialize_extension_bundle,
)

WORKSPACE = Path(__file__).parents[2]
SOURCE = WORKSPACE / "onec" / "OnecInteractiveRuntime"
RESOURCE_ROOT = WORKSPACE / "src" / "onec_runtime" / "resources" / "extension"
MD = "http://v8.1c.ru/8.3/MDClasses"


def test_product_extension_metadata_is_universal() -> None:
    root = ElementTree.parse(SOURCE / "Configuration.xml").getroot()
    properties = root.find(f"{{{MD}}}Configuration/{{{MD}}}Properties")
    assert properties is not None
    assert properties.findtext(f"{{{MD}}}Name") == "OnecInteractiveRuntime"
    assert properties.findtext(f"{{{MD}}}NamePrefix") == "OnecInteractiveRuntime_"
    assert properties.findtext(f"{{{MD}}}ConfigurationExtensionPurpose") == "AddOn"
    assert (
        properties.findtext(f"{{{MD}}}KeepMappingToExtendedConfigurationObjectsByIDs")
        == "false"
    )
    assert properties.find(f"{{{MD}}}DefaultLanguage") is None
    assert properties.find(f"{{{MD}}}InterfaceCompatibilityMode") is None
    assert not (SOURCE / "Languages").exists()


def test_product_extension_exposes_exact_handshake_values() -> None:
    expected = {
        'ИдентификаторПродуктаRuntime = "onec-interactive-runtime";',
        'ВерсияАртефактаRuntime = "0.1.12";',
        'ВерсияПротоколаRuntime = "5";',
    }
    configuration = ElementTree.parse(SOURCE / "Configuration.xml").getroot()
    properties = configuration.find(f"{{{MD}}}Configuration/{{{MD}}}Properties")
    assert properties is not None
    assert properties.findtext(f"{{{MD}}}Version") == "0.1.12"
    for relative in (
        Path("Ext/ManagedApplicationModule.bsl"),
        Path("CommonModules/RuntimeKernelServer/Ext/Module.bsl"),
    ):
        source = (SOURCE / relative).read_text(encoding="utf-8-sig")
        assert expected <= {line.strip() for line in source.splitlines()}


def test_config_dump_info_tracks_every_owned_common_module() -> None:
    configuration = ElementTree.parse(SOURCE / "Configuration.xml").getroot()
    child_objects = configuration.find(f"{{{MD}}}Configuration/{{{MD}}}ChildObjects")
    assert child_objects is not None
    common_module_names = [
        element.text
        for element in child_objects.findall(f"{{{MD}}}CommonModule")
        if element.text is not None
    ]

    dump_root = ElementTree.parse(SOURCE / "ConfigDumpInfo.xml").getroot()
    dump_namespace = "http://v8.1c.ru/8.3/xcf/dumpinfo"
    metadata_ids = {
        element.attrib["name"]: element.attrib["id"]
        for element in dump_root.findall(
            f"{{{dump_namespace}}}ConfigVersions/{{{dump_namespace}}}Metadata"
        )
    }

    for name in common_module_names:
        module_root = ElementTree.parse(
            SOURCE / "CommonModules" / f"{name}.xml"
        ).getroot()
        module = module_root.find(f"{{{MD}}}CommonModule")
        assert module is not None
        module_id = module.attrib["uuid"]
        assert metadata_ids[f"CommonModule.{name}"] == module_id
        assert metadata_ids[f"CommonModule.{name}.Module"] == f"{module_id}.0"


def test_checked_in_bundle_matches_canonical_source_and_manifest(
    tmp_path: Path,
) -> None:
    bundle = materialize_extension_bundle(
        RESOURCE_ROOT / "OnecInteractiveRuntime.cfe",
        RESOURCE_ROOT / "extension-manifest.json",
        tmp_path / "runtime",
    )

    assert bundle.manifest.extension_name == "OnecInteractiveRuntime"
    assert bundle.manifest.artifact_version == "0.1.12"
    assert bundle.manifest.protocol_version == "5"
    assert bundle.manifest.fingerprints.artifact.language_bound_by_name is False
    assert bundle.manifest.fingerprints == fingerprint_extension_dump(SOURCE)
