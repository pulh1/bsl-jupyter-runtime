from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import shutil
import subprocess
from xml.etree import ElementTree

from onec_runtime.config import RuntimeConfig
from onec_runtime.extension_bundle import packaged_extension_bundle
from onec_runtime.errors import ProcessStartError
from onec_runtime.toolchain import (
    apply_product_extension,
    create_file_infobase_at,
    load_target_extension_cfe,
    run_tool_command,
)


FIXTURE_EXTENSION_NAME = "JupyterBslTestFixture"
CALLEE_CAPTURE_FRAGMENT = "ЛокальныйСчетчик = ЛокальныйСчетчик + 2;"
CALLER_CAPTURE_FRAGMENT = "ЗначениеИзСтека = РезультатПодчиненного.Счетчик;"
_CAPTURE_FRAGMENTS = (CALLEE_CAPTURE_FRAGMENT, CALLER_CAPTURE_FRAGMENT)
_CONFIGURATION_UUID = "7b22aaef-49a8-4f1b-9c1b-515394df8d01"
_MODULE_UUIDS = {
    "JupyterBslFixtureCallerServer": "7b22aaef-49a8-4f1b-9c1b-515394df8d02",
    "JupyterBslFixtureCalleeServer": "7b22aaef-49a8-4f1b-9c1b-515394df8d03",
}


@dataclass(frozen=True, slots=True)
class FixtureExtensionSourceContract:
    extension_name: str
    module_names: tuple[str, ...]
    fragment_counts: dict[str, int]
    source_sha256: str


@dataclass(frozen=True, slots=True)
class FixtureExtensionArtifact:
    source_root: Path
    cfe_path: Path
    source_sha256: str


def _source_files(source_root: Path) -> tuple[Path, ...]:
    return tuple(
        path
        for path in sorted(source_root.rglob("*"))
        if path.is_file() and path.suffix.casefold() in {".xml", ".bsl"}
    )


def _source_digest(source_root: Path, files: tuple[Path, ...]) -> str:
    digest = sha256()
    for path in files:
        relative = path.relative_to(source_root).as_posix().encode("utf-8")
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _local_name(element: ElementTree.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _properties(element: ElementTree.Element) -> dict[str, str]:
    properties = next(
        (child for child in element if _local_name(child) == "Properties"), None
    )
    if properties is None:
        return {}
    return {_local_name(child): child.text or "" for child in properties}


def validate_fixture_extension_source(
    source_root: Path,
) -> FixtureExtensionSourceContract:
    root = Path(source_root).resolve()
    files = _source_files(root)
    if not files:
        raise ProcessStartError(f"Fixture extension source is empty: {root}")
    bsl_text = "\n".join(
        path.read_text(encoding="utf-8-sig")
        for path in files
        if path.suffix.casefold() == ".bsl"
    )
    counts = {fragment: bsl_text.count(fragment) for fragment in _CAPTURE_FRAGMENTS}
    if any(count != 1 for count in counts.values()):
        raise ProcessStartError(
            "Fixture capture fragment must occur exactly once: "
            + ", ".join(f"{fragment!r}={count}" for fragment, count in counts.items())
        )
    configuration_path = root / "Configuration.xml"
    try:
        configuration = ElementTree.parse(configuration_path).getroot()
    except (OSError, ElementTree.ParseError) as error:
        raise ProcessStartError(
            f"Fixture extension Configuration.xml is invalid: {configuration_path}"
        ) from error
    configuration_element = next(
        (
            element
            for element in configuration.iter()
            if _local_name(element) == "Configuration"
        ),
        None,
    )
    if configuration_element is None:
        raise ProcessStartError("Fixture extension Configuration element is missing")
    configuration_properties = _properties(configuration_element)
    extension_name = configuration_properties.get("Name", "")
    if extension_name != FIXTURE_EXTENSION_NAME:
        raise ProcessStartError(
            f"Fixture extension name must be {FIXTURE_EXTENSION_NAME}"
        )
    expected_configuration = {
        "ConfigurationExtensionPurpose": "AddOn",
        "NamePrefix": "JupyterBslTestFixture_",
        "ConfigurationExtensionCompatibilityMode": "Version8_3_27",
    }
    if (
        configuration_element.get("uuid") != _CONFIGURATION_UUID
        or any(
            configuration_properties.get(name) != value
            for name, value in expected_configuration.items()
        )
    ):
        raise ProcessStartError("Fixture extension configuration identity is invalid")
    for uncontrolled in ("DefaultLanguage", "InterfaceCompatibilityMode"):
        if uncontrolled in configuration_properties:
            raise ProcessStartError(
                f"Fixture extension must not control {uncontrolled}"
            )
    modules_root = root / "CommonModules"
    module_names = tuple(
        element.text
        for element in configuration.iter()
        if element.tag.endswith("CommonModule") and isinstance(element.text, str)
    )
    expected_modules = (
        "JupyterBslFixtureCallerServer",
        "JupyterBslFixtureCalleeServer",
    )
    if module_names != expected_modules:
        raise ProcessStartError(
            f"Fixture extension modules must be {expected_modules!r}"
        )
    for module_name in module_names:
        module_source = modules_root / module_name / "Ext" / "Module.bsl"
        if not module_source.is_file():
            raise ProcessStartError(f"Fixture module source is missing: {module_source}")
        module_xml = modules_root / f"{module_name}.xml"
        try:
            module_root = ElementTree.parse(module_xml).getroot()
            module_element = next(
                element
                for element in module_root.iter()
                if _local_name(element) == "CommonModule"
            )
        except (OSError, ElementTree.ParseError, StopIteration) as error:
            raise ProcessStartError(f"Fixture module metadata is invalid: {module_xml}") from error
        module_properties = _properties(module_element)
        expected_properties = {
            "Name": module_name,
            "Global": "false",
            "Server": "true",
            "ExternalConnection": "true",
            "ClientOrdinaryApplication": "true",
            "ServerCall": "false",
        }
        if (
            module_element.get("uuid") != _MODULE_UUIDS[module_name]
            or any(
                module_properties.get(name) != value
                for name, value in expected_properties.items()
            )
        ):
            raise ProcessStartError(f"Fixture module contract is invalid: {module_name}")
    dump_info_path = root / "ConfigDumpInfo.xml"
    try:
        dump_root = ElementTree.parse(dump_info_path).getroot()
    except (OSError, ElementTree.ParseError) as error:
        raise ProcessStartError(f"Fixture ConfigDumpInfo.xml is invalid: {dump_info_path}") from error
    dump_mapping = {
        element.get("name", ""): element.get("id", "")
        for element in dump_root.iter()
        if _local_name(element) == "Metadata"
    }
    expected_mapping = {
        f"Configuration.{FIXTURE_EXTENSION_NAME}": _CONFIGURATION_UUID,
        **{
            f"CommonModule.{name}": object_id
            for name, object_id in _MODULE_UUIDS.items()
        },
    }
    if any(dump_mapping.get(name) != object_id for name, object_id in expected_mapping.items()):
        raise ProcessStartError("Fixture ConfigDumpInfo identity mapping is invalid")
    return FixtureExtensionSourceContract(
        extension_name,
        module_names,
        counts,
        _source_digest(root, files),
    )


def _build_command(
    designer: Path,
    build_infobase: Path,
    *arguments: str,
    log_path: Path,
) -> list[str]:
    return [
        str(designer),
        "DESIGNER",
        "/F",
        str(build_infobase),
        *arguments,
        "-Extension",
        FIXTURE_EXTENSION_NAME,
        "/DisableStartupDialogs",
        "/DisableStartupMessages",
        "/Out",
        str(log_path),
    ]


def build_fixture_extension(
    platform_bin: Path,
    source_root: Path,
    work_root: Path,
) -> FixtureExtensionArtifact:
    contract = validate_fixture_extension_source(source_root)
    root = Path(work_root).resolve()
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    contract_source = Path(source_root).resolve()
    build_source = root / "source"
    shutil.copytree(contract_source, build_source)
    build_infobase = root / "build-infobase"
    designer = Path(platform_bin).resolve() / "1cv8.exe"
    create_file_infobase_at(designer, build_infobase, logs / "create.log")
    cfe_path = root / f"{FIXTURE_EXTENSION_NAME}.cfe"
    commands = (
        _build_command(
            designer,
            build_infobase,
            "/LoadConfigFromFiles",
            str(build_source),
            "-updateConfigDumpInfo",
            "-Format",
            "Hierarchical",
            log_path=logs / "load-source.log",
        ),
        _build_command(
            designer,
            build_infobase,
            "/DumpCfg",
            str(cfe_path),
            log_path=logs / "dump.log",
        ),
    )
    for command, log_path in zip(
        commands,
        (logs / "load-source.log", logs / "dump.log"),
        strict=True,
    ):
        run_tool_command(command, log_path)
    if not cfe_path.is_file() or cfe_path.stat().st_size == 0:
        raise ProcessStartError(
            f"1C did not create non-empty fixture CFE: {cfe_path}"
        )
    return FixtureExtensionArtifact(
        contract_source,
        cfe_path,
        contract.source_sha256,
    )


def _target_prefix(config: RuntimeConfig) -> list[str]:
    return [
        str(config.designer_exe),
        "DESIGNER",
        "/F",
        str(config.infobase_dir),
        "/N",
        config.username,
        "/P",
        "",
    ]


def _target_command(
    config: RuntimeConfig,
    *arguments: str,
    log_path: Path,
) -> list[str]:
    return [
        *_target_prefix(config),
        *arguments,
        "-Extension",
        FIXTURE_EXTENSION_NAME,
        "/DisableStartupDialogs",
        "/DisableStartupMessages",
        "/Out",
        str(log_path),
    ]


def install_fixture_extension(
    config: RuntimeConfig,
    artifact: FixtureExtensionArtifact,
    log_root: Path,
) -> None:
    cfe_path = Path(artifact.cfe_path).resolve()
    if not cfe_path.is_file() or cfe_path.stat().st_size == 0:
        raise ProcessStartError(f"Fixture CFE is missing or empty: {cfe_path}")
    logs = Path(log_root).resolve()
    logs.mkdir(parents=True, exist_ok=True)
    operations = (
        (
            _target_command(
                config,
                "/LoadCfg",
                str(cfe_path),
                log_path=logs / "load.log",
            ),
            logs / "load.log",
        ),
        (
            _target_command(
                config,
                "/UpdateDBCfg",
                "-Dynamic+",
                log_path=logs / "update.log",
            ),
            logs / "update.log",
        ),
    )
    for command, log_path in operations:
        run_tool_command(command, log_path)


def install_minimal_host_configuration(
    config: RuntimeConfig,
    source_root: Path,
    log_root: Path,
) -> None:
    source = Path(source_root).resolve()
    if not (source / "Configuration.xml").is_file():
        raise ProcessStartError(
            f"Minimal host configuration source is missing: {source}"
        )
    logs = Path(log_root).resolve()
    logs.mkdir(parents=True, exist_ok=True)
    # Designer updates ConfigDumpInfo.xml while loading. Keep fixture sources immutable.
    staged_source = logs / "host-source"
    shutil.copytree(source, staged_source, dirs_exist_ok=True)
    operations = (
        (
            [
                *_target_prefix(config),
                "/LoadConfigFromFiles",
                str(staged_source),
                "-updateConfigDumpInfo",
                "-Format",
                "Hierarchical",
                "/DisableStartupDialogs",
                "/DisableStartupMessages",
                "/Out",
                str(logs / "load.log"),
            ],
            logs / "load.log",
        ),
        (
            [
                *_target_prefix(config),
                "/CheckConfig",
                "-ConfigLogIntegrity",
                "-IncorrectReferences",
                "-ThinClient",
                "-Server",
                "/DisableStartupDialogs",
                "/DisableStartupMessages",
                "/Out",
                str(logs / "check.log"),
            ],
            logs / "check.log",
        ),
        (
            [
                *_target_prefix(config),
                "/UpdateDBCfg",
                "-Dynamic+",
                "/DisableStartupDialogs",
                "/DisableStartupMessages",
                "/Out",
                str(logs / "update.log"),
            ],
            logs / "update.log",
        ),
    )
    for command, log_path in operations:
        run_tool_command(command, log_path)


def install_product_extension(
    config: RuntimeConfig,
    log_root: Path,
) -> None:
    logs = Path(log_root).resolve()
    logs.mkdir(parents=True, exist_ok=True)
    bundle = packaged_extension_bundle(config.runtime_dir)
    load_target_extension_cfe(config, bundle.cfe_path, logs / "load.log")
    apply_product_extension(config, logs / "update.log")
