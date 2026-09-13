from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

ROOT_ID = "11111111-1111-4111-8111-111111111111"
MANAGED_OBJECT_ID = "22222222-2222-4222-8222-222222222222"
MANAGED_RDBG_OBJECT_ID = ROOT_ID
SERVER_MODULE_ID = "44444444-4444-4444-8444-444444444444"
CONTEXT_MODULE_ID = "55555555-5555-4555-8555-555555555555"
TABLE_MODULE_ID = "66666666-6666-4666-8666-666666666666"
VALUE_MODULE_ID = "77777777-7777-4777-8777-777777777777"
MANAGED_PROPERTY_ID = "d22e852a-cf8a-4f77-8ccb-3548e7792bea"
COMMON_MODULE_PROPERTY_ID = "d5963243-262e-4398-b4d7-fb16d06484f6"


def _handshake(artifact_version: str, protocol_version: str) -> str:
    return (
        '\tИдентификаторПродуктаRuntime = "onec-interactive-runtime";\n'
        f'\tВерсияАртефактаRuntime = "{artifact_version}";\n'
        f'\tВерсияПротоколаRuntime = "{protocol_version}";\n'
    )


def write_dump_fixture(
    root: Path,
    *,
    extension_name: str = "OnecInteractiveRuntime",
    artifact_version: str = "0.1.0",
    protocol_version: str = "1",
) -> Path:
    dump = root / "dump"
    managed = dump / "Ext" / "ManagedApplicationModule.bsl"
    server = dump / "CommonModules" / "RuntimeKernelServer" / "Ext" / "Module.bsl"
    for parent in (managed.parent, server.parent):
        parent.mkdir(parents=True, exist_ok=True)

    (dump / "Configuration.xml").write_text(
        f'''<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">
  <Configuration uuid="{ROOT_ID}">
    <Properties>
      <ObjectBelonging>Adopted</ObjectBelonging>
      <Name>{extension_name}</Name>
      <ConfigurationExtensionPurpose>AddOn</ConfigurationExtensionPurpose>
      <KeepMappingToExtendedConfigurationObjectsByIDs>false</KeepMappingToExtendedConfigurationObjectsByIDs>
      <NamePrefix>OnecInteractiveRuntime_</NamePrefix>
      <Vendor>onec-interactive-runtime</Vendor>
      <Version>{artifact_version}</Version>
    </Properties>
    <ChildObjects>
      <CommonModule>RuntimeContextStoreServer</CommonModule>
      <CommonModule>RuntimeKernelServer</CommonModule>
      <CommonModule>RuntimeTableTransferServer</CommonModule>
      <CommonModule>RuntimeValueTransferServer</CommonModule>
    </ChildObjects>
  </Configuration>
</MetaDataObject>
''',
        encoding="utf-8",
        newline="",
    )
    (dump / "ConfigDumpInfo.xml").write_text(
        f'''<?xml version="1.0" encoding="UTF-8"?>
<ConfigDumpInfo xmlns="http://v8.1c.ru/8.3/xcf/dumpinfo">
  <ConfigVersions>
    <Metadata name="Configuration.{extension_name}" id="{ROOT_ID}" />
    <Metadata name="Configuration.{extension_name}.ManagedApplicationModule" id="{MANAGED_OBJECT_ID}.6" />
    <Metadata name="CommonModule.RuntimeContextStoreServer" id="{CONTEXT_MODULE_ID}" />
    <Metadata name="CommonModule.RuntimeContextStoreServer.Module" id="{CONTEXT_MODULE_ID}.0" />
    <Metadata name="CommonModule.RuntimeKernelServer" id="{SERVER_MODULE_ID}" />
    <Metadata name="CommonModule.RuntimeKernelServer.Module" id="{SERVER_MODULE_ID}.0" />
    <Metadata name="CommonModule.RuntimeTableTransferServer" id="{TABLE_MODULE_ID}" />
    <Metadata name="CommonModule.RuntimeTableTransferServer.Module" id="{TABLE_MODULE_ID}.0" />
    <Metadata name="CommonModule.RuntimeValueTransferServer" id="{VALUE_MODULE_ID}" />
    <Metadata name="CommonModule.RuntimeValueTransferServer.Module" id="{VALUE_MODULE_ID}.0" />
  </ConfigVersions>
</ConfigDumpInfo>
''',
        encoding="utf-8",
        newline="",
    )
    managed.write_text(
        "Процедура Запуск()\n"
        + _handshake(artifact_version, protocol_version)
        + "\tС = 1; // @runtime-extension-service-breakpoint\nКонецПроцедуры\n",
        encoding="utf-8-sig",
        newline="",
    )
    server.write_text(
        "Процедура Запустить()\n"
        + _handshake(artifact_version, protocol_version)
        + "\tКонтекст = Новый Структура; // @runtime-server-extension-entry-breakpoint\n"
        + "\tС = 1; // @runtime-server-extension-service-breakpoint\nКонецПроцедуры\n",
        encoding="utf-8-sig",
        newline="",
    )
    return dump


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def write_manifest_fixture(
    root: Path,
    *,
    cfe_sha256: str,
    cfe_size: int = 0,
) -> Path:
    identity = {
        "product_id": "onec-interactive-runtime",
        "extension_name": "OnecInteractiveRuntime",
        "root_id": ROOT_ID,
        "runtime_module_ids": [
            SERVER_MODULE_ID,
            CONTEXT_MODULE_ID,
            TABLE_MODULE_ID,
            VALUE_MODULE_ID,
        ],
        "purpose": "AddOn",
        "name_prefix": "OnecInteractiveRuntime_",
        "vendor": "onec-interactive-runtime",
    }
    artifact = {
        "artifact_version": "0.1.0",
        "protocol_version": "1",
        "language_bound_by_name": False,
        "metadata": [
            {
                "name": "CommonModule.RuntimeContextStoreServer",
                "object_id": CONTEXT_MODULE_ID,
            },
            {
                "name": "CommonModule.RuntimeContextStoreServer.Module",
                "object_id": CONTEXT_MODULE_ID,
            },
            {"name": "CommonModule.RuntimeKernelServer", "object_id": SERVER_MODULE_ID},
            {
                "name": "CommonModule.RuntimeKernelServer.Module",
                "object_id": SERVER_MODULE_ID,
            },
            {
                "name": "CommonModule.RuntimeTableTransferServer",
                "object_id": TABLE_MODULE_ID,
            },
            {
                "name": "CommonModule.RuntimeTableTransferServer.Module",
                "object_id": TABLE_MODULE_ID,
            },
            {
                "name": "CommonModule.RuntimeValueTransferServer",
                "object_id": VALUE_MODULE_ID,
            },
            {
                "name": "CommonModule.RuntimeValueTransferServer.Module",
                "object_id": VALUE_MODULE_ID,
            },
            {
                "name": "Configuration.OnecInteractiveRuntime",
                "object_id": ROOT_ID,
            },
            {
                "name": "Configuration.OnecInteractiveRuntime.ManagedApplicationModule",
                "object_id": MANAGED_OBJECT_ID,
            },
        ],
        "source_sha256": {
            "CommonModules/RuntimeKernelServer/Ext/Module.bsl": "05e02a1ff3063eb9208d97a94ccd2a4ce3ac0f8c6de1588587d2103d46ad11e9",
            "Ext/ManagedApplicationModule.bsl": "90738a90599154558cfb12e7646fcb8fd6b765c9381a03716368a05de374828a",
        },
    }
    location = {
        "module_type": "ExtensionModule",
        "url": "",
        "object_id": SERVER_MODULE_ID,
        "property_id": COMMON_MODULE_PROPERTY_ID,
        "line": 6,
        "extension_name": "OnecInteractiveRuntime",
        "ext_id": 0,
    }
    payload = {
        "schema_version": 1,
        "product_id": "onec-interactive-runtime",
        "extension_name": "OnecInteractiveRuntime",
        "artifact_version": "0.1.0",
        "protocol_version": "1",
        "cfe_filename": "OnecInteractiveRuntime.cfe",
        "cfe_size": cfe_size,
        "cfe_sha256": cfe_sha256,
        "fingerprints": {
            "identity": identity,
            "artifact": artifact,
            "identity_sha256": _canonical_sha256(identity),
            "artifact_sha256": _canonical_sha256(artifact),
        },
        "breakpoints": {
            "managed": {
                **location,
                "object_id": MANAGED_RDBG_OBJECT_ID,
                "property_id": MANAGED_PROPERTY_ID,
                "line": 5,
            },
            "server_entry": {**location, "line": 5},
            "server_service": {**location, "line": 6},
        },
    }
    path = root / "extension-manifest.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="",
    )
    return path


def marker_line(module_path: Path, marker: str) -> int:
    matches = [
        index
        for index, line in enumerate(
            module_path.read_text(encoding="utf-8-sig").splitlines(), start=1
        )
        if marker in line
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"expected exactly one {marker!r} marker, found {len(matches)}"
        )
    return matches[0]
