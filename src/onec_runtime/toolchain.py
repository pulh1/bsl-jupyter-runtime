from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from onec_runtime.config import RuntimeConfig
from onec_runtime.credentials import redact_command
from onec_runtime.errors import ExtensionNotInstalled, ProcessStartError
from onec_runtime.extension_bundle import EXTENSION_NAME
from onec_runtime.startup_diagnostics import startup_log_hint

HEADLESS_ARGS = ["/DisableStartupDialogs", "/DisableStartupMessages"]
_MISSING_PRODUCT_EXTENSION_DIAGNOSTICS = (
    "Операция не может быть выполнена, так как расширение конфигурации "
    f"с указанным именем не найдено: {EXTENSION_NAME}",
    "Cannot perform the operation because the following configuration extension "
    f"is not found: {EXTENSION_NAME}",
)


@dataclass(frozen=True)
class ToolResult:
    command: tuple[str, ...]
    returncode: int
    log_path: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "command", redact_command(self.command))


def create_file_infobase_at_command(
    designer_exe: Path, infobase_dir: Path, log_path: Path
) -> list[str]:
    return [
        str(designer_exe),
        "CREATEINFOBASE",
        f"File={infobase_dir};",
        *HEADLESS_ARGS,
        "/Out",
        str(log_path),
    ]


def load_product_extension_source_command(
    designer_exe: Path,
    infobase_dir: Path,
    source_dir: Path,
    log_path: Path,
) -> list[str]:
    return [
        str(designer_exe),
        "DESIGNER",
        "/F",
        str(infobase_dir),
        "/LoadConfigFromFiles",
        str(source_dir),
        "-Extension",
        EXTENSION_NAME,
        "-updateConfigDumpInfo",
        "-Format",
        "Hierarchical",
        *HEADLESS_ARGS,
        "/Out",
        str(log_path),
    ]


def dump_product_extension_cfe_command(
    designer_exe: Path,
    infobase_dir: Path,
    output_cfe: Path,
    log_path: Path,
) -> list[str]:
    return [
        str(designer_exe),
        "DESIGNER",
        "/F",
        str(infobase_dir),
        "/DumpCfg",
        str(output_cfe),
        "-Extension",
        EXTENSION_NAME,
        *HEADLESS_ARGS,
        "/Out",
        str(log_path),
    ]


def _target_designer_command(config: RuntimeConfig) -> list[str]:
    return [
        str(config.designer_exe),
        "DESIGNER",
        *config.infobase_arguments,
        "/N",
        config.username,
        "/P",
        config.password,
    ]


def dump_target_extension_files_command(
    config: RuntimeConfig, output_dir: Path, log_path: Path
) -> list[str]:
    return [
        *_target_designer_command(config),
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


def dump_target_extension_cfe_command(
    config: RuntimeConfig, output_cfe: Path, log_path: Path
) -> list[str]:
    return [
        *_target_designer_command(config),
        "/DumpCfg",
        str(output_cfe),
        "-Extension",
        EXTENSION_NAME,
        *HEADLESS_ARGS,
        "/Out",
        str(log_path),
    ]


def load_target_extension_cfe_command(
    config: RuntimeConfig, input_cfe: Path, log_path: Path
) -> list[str]:
    return [
        *_target_designer_command(config),
        "/LoadCfg",
        str(input_cfe),
        "-Extension",
        EXTENSION_NAME,
        *HEADLESS_ARGS,
        "/Out",
        str(log_path),
    ]


def apply_product_extension_command(config: RuntimeConfig, log_path: Path) -> list[str]:
    dynamic_mode = "-Dynamic-" if config.uses_external_infobase else "-Dynamic+"
    return [
        *_target_designer_command(config),
        "/UpdateDBCfg",
        dynamic_mode,
        "-Extension",
        EXTENSION_NAME,
        *HEADLESS_ARGS,
        "/Out",
        str(log_path),
    ]


def create_infobase_command(config: RuntimeConfig) -> list[str]:
    return _create_infobase_command(
        config, config.infobase_dir, config.logs_dir / "create-infobase.log"
    )


def create_build_infobase_command(config: RuntimeConfig) -> list[str]:
    return _create_infobase_command(
        config,
        config.build_infobase_dir,
        config.logs_dir / "create-build-infobase.log",
    )


def _create_infobase_command(
    config: RuntimeConfig, infobase_dir: Path, log_path: Path
) -> list[str]:
    return [
        str(config.designer_exe),
        "CREATEINFOBASE",
        f"File={infobase_dir};",
        *HEADLESS_ARGS,
        "/Out",
        str(log_path),
    ]


def build_external_processor_command(config: RuntimeConfig) -> list[str]:
    return build_external_processor_from_files_command(
        config,
        config.workspace / "onec" / "Kernel" / "Kernel.xml",
        config.kernel_epf,
        config.logs_dir / "build-kernel.log",
    )


def build_worker_command(config: RuntimeConfig) -> list[str]:
    return build_external_processor_from_files_command(
        config,
        config.workspace / "onec" / "Worker" / "Worker.xml",
        config.worker_epf,
        config.logs_dir / "build-worker.log",
    )


def build_external_processor_from_files_command(
    config: RuntimeConfig,
    source_xml: Path,
    output_epf: Path,
    log_path: Path,
) -> list[str]:
    return [
        str(config.designer_exe),
        "DESIGNER",
        "/F",
        str(config.build_infobase_dir),
        "/LoadExternalDataProcessorOrReportFromFiles",
        str(source_xml),
        str(output_epf),
        *HEADLESS_ARGS,
        "/Out",
        str(log_path),
    ]


def load_extension_source_command(
    config: RuntimeConfig,
    source_dir: Path,
) -> list[str]:
    return [
        str(config.designer_exe),
        "DESIGNER",
        "/F",
        str(config.build_infobase_dir),
        "/LoadConfigFromFiles",
        str(source_dir),
        "-Extension",
        EXTENSION_NAME,
        "-updateConfigDumpInfo",
        "-format",
        "Hierarchical",
        *HEADLESS_ARGS,
        "/Out",
        str(config.logs_dir / "load-extension-source.log"),
    ]


def load_target_extension_source_command(
    config: RuntimeConfig,
    source_dir: Path,
) -> list[str]:
    return [
        str(config.designer_exe),
        "DESIGNER",
        *config.infobase_arguments,
        "/N",
        config.username,
        "/P",
        config.password,
        "/LoadConfigFromFiles",
        str(source_dir),
        "-Extension",
        EXTENSION_NAME,
        "-updateConfigDumpInfo",
        "-format",
        "Hierarchical",
        *HEADLESS_ARGS,
        "/Out",
        str(config.logs_dir / "load-target-extension-source.log"),
    ]


def dump_extension_command(
    config: RuntimeConfig,
    output_cfe: Path,
) -> list[str]:
    return [
        str(config.designer_exe),
        "DESIGNER",
        "/F",
        str(config.build_infobase_dir),
        "/DumpCfg",
        str(output_cfe),
        "-Extension",
        EXTENSION_NAME,
        *HEADLESS_ARGS,
        "/Out",
        str(config.logs_dir / "dump-extension.log"),
    ]


def deploy_extension_command(config: RuntimeConfig) -> list[str]:
    return [
        str(config.designer_exe),
        "DESIGNER",
        *config.infobase_arguments,
        "/N",
        config.username,
        "/P",
        config.password,
        "/LoadCfg",
        str(config.build_dir / f"{EXTENSION_NAME}.cfe"),
        "-Extension",
        EXTENSION_NAME,
        *HEADLESS_ARGS,
        "/Out",
        str(config.logs_dir / "load-extension.log"),
    ]


def update_extension_command(config: RuntimeConfig) -> list[str]:
    command = [
        str(config.designer_exe),
        "DESIGNER",
        *config.infobase_arguments,
        "/N",
        config.username,
        "/P",
        config.password,
        "/UpdateDBCfg",
        *HEADLESS_ARGS,
        "-Extension",
        EXTENSION_NAME,
        "/Out",
        str(config.logs_dir / "update-extension.log"),
    ]
    dynamic_mode = "-Dynamic-" if config.uses_external_infobase else "-Dynamic+"
    command.insert(command.index("-Extension"), dynamic_mode)
    return command


def apply_extension_command(config: RuntimeConfig) -> list[str]:
    dynamic_mode = "-Dynamic-" if config.uses_external_infobase else "-Dynamic+"
    return [
        str(config.designer_exe),
        "DESIGNER",
        *config.infobase_arguments,
        "/N",
        config.username,
        "/P",
        config.password,
        "/LoadCfg",
        str(config.build_dir / f"{EXTENSION_NAME}.cfe"),
        "-Extension",
        EXTENSION_NAME,
        "/UpdateDBCfg",
        dynamic_mode,
        *HEADLESS_ARGS,
        "/Out",
        str(config.logs_dir / "apply-extension.log"),
    ]


def apply_target_extension_source_command(
    config: RuntimeConfig,
    source_dir: Path,
) -> list[str]:
    dynamic_mode = "-Dynamic-" if config.uses_external_infobase else "-Dynamic+"
    return [
        str(config.designer_exe),
        "DESIGNER",
        *config.infobase_arguments,
        "/N",
        config.username,
        "/P",
        config.password,
        "/LoadConfigFromFiles",
        str(source_dir),
        "-Extension",
        EXTENSION_NAME,
        "-updateConfigDumpInfo",
        "-format",
        "Hierarchical",
        "/UpdateDBCfg",
        dynamic_mode,
        *HEADLESS_ARGS,
        "/Out",
        str(config.logs_dir / "apply-extension-source.log"),
    ]


def _prepare_runtime_directories(config: RuntimeConfig) -> None:
    config.runtime_dir.mkdir(parents=True, exist_ok=True)
    config.build_dir.mkdir(parents=True, exist_ok=True)
    config.logs_dir.mkdir(parents=True, exist_ok=True)


def _run(command: list[str], log_path: Path) -> ToolResult:
    try:
        completed = subprocess.run(command, shell=False, check=False)
    except OSError as error:
        raise ProcessStartError(
            f"Unable to start 1C tool (OS error {error.errno}); see {log_path}"
        ) from None
    result = ToolResult(tuple(command), completed.returncode, log_path)
    if completed.returncode != 0:
        raise ProcessStartError(
            f"1C tool exited with code {completed.returncode}; see {log_path}"
        )
    return result


def run_tool_command(command: list[str], log_path: Path) -> ToolResult:
    """Run one headless Designer command using the standard return-code policy."""
    return _run(command, log_path)


def _prepare_designer_log(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.unlink(missing_ok=True)


def _clear_output(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _require_non_empty_file(path: Path, label: str, log_path: Path) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise ProcessStartError(
            f"1C did not create non-empty {label}: {path}; see {log_path}"
        )


def _require_non_empty_dump(output_dir: Path, log_path: Path) -> None:
    if not output_dir.is_dir() or not any(
        path.is_file() and path.stat().st_size > 0 for path in output_dir.rglob("*")
    ):
        raise ProcessStartError(
            f"1C did not create a non-empty extension dump: {output_dir}; see {log_path}"
        )


def _missing_product_extension(log_path: Path) -> bool:
    try:
        text = log_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return False
    return any(
        line.strip() in _MISSING_PRODUCT_EXTENSION_DIAGNOSTICS
        for line in text.splitlines()
    )


def _run_product_command(command: list[str], log_path: Path) -> ToolResult:
    _prepare_designer_log(log_path)
    try:
        return run_tool_command(command, log_path)
    except ProcessStartError as error:
        if _missing_product_extension(log_path):
            raise ExtensionNotInstalled(
                f"The target infobase does not contain {EXTENSION_NAME}"
            ) from error
        hint = startup_log_hint(log_path)
        if hint is not None:
            raise ProcessStartError(
                f"Конфигуратор 1С: {hint}; подробности: {log_path}"
            ) from None
        raise


def create_file_infobase_at(
    designer_exe: Path, infobase_dir: Path, log_path: Path
) -> ToolResult:
    database_file = infobase_dir / "1Cv8.1CD"
    if database_file.exists():
        raise ProcessStartError(
            f"Refusing to reuse the existing infobase at {infobase_dir}"
        )
    infobase_dir.parent.mkdir(parents=True, exist_ok=True)
    result = _run_product_command(
        create_file_infobase_at_command(designer_exe, infobase_dir, log_path),
        log_path,
    )
    _require_non_empty_file(database_file, "1Cv8.1CD", log_path)
    return result


def load_product_extension_source(
    designer_exe: Path,
    infobase_dir: Path,
    source_dir: Path,
    log_path: Path,
) -> ToolResult:
    return _run_product_command(
        load_product_extension_source_command(
            designer_exe, infobase_dir, source_dir, log_path
        ),
        log_path,
    )


def dump_product_extension_cfe(
    designer_exe: Path,
    infobase_dir: Path,
    output_cfe: Path,
    log_path: Path,
) -> ToolResult:
    output_cfe.parent.mkdir(parents=True, exist_ok=True)
    _clear_output(output_cfe)
    result = _run_product_command(
        dump_product_extension_cfe_command(
            designer_exe, infobase_dir, output_cfe, log_path
        ),
        log_path,
    )
    _require_non_empty_file(output_cfe, "CFE", log_path)
    return result


def dump_target_extension_files(
    config: RuntimeConfig, output_dir: Path, log_path: Path
) -> ToolResult:
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    _clear_output(output_dir)
    result = _run_product_command(
        dump_target_extension_files_command(config, output_dir, log_path), log_path
    )
    _require_non_empty_dump(output_dir, log_path)
    return result


def dump_target_extension_cfe(
    config: RuntimeConfig, output_cfe: Path, log_path: Path
) -> ToolResult:
    output_cfe.parent.mkdir(parents=True, exist_ok=True)
    _clear_output(output_cfe)
    result = _run_product_command(
        dump_target_extension_cfe_command(config, output_cfe, log_path), log_path
    )
    _require_non_empty_file(output_cfe, "CFE", log_path)
    return result


def load_target_extension_cfe(
    config: RuntimeConfig, input_cfe: Path, log_path: Path
) -> ToolResult:
    return _run_product_command(
        load_target_extension_cfe_command(config, input_cfe, log_path), log_path
    )


def apply_product_extension(config: RuntimeConfig, log_path: Path) -> ToolResult:
    return _run_product_command(
        apply_product_extension_command(config, log_path), log_path
    )


def _create_empty_infobase(
    config: RuntimeConfig,
    infobase_dir: Path,
    command: list[str],
    log_path: Path,
    *,
    recreate: bool,
) -> ToolResult:
    _prepare_runtime_directories(config)
    database_file = infobase_dir / "1Cv8.1CD"
    if database_file.is_file():
        if recreate:
            raise ProcessStartError(
                "Refusing to delete the existing infobase; move .runtime/infobase aside first"
            )
        return ToolResult(tuple(command), 0, log_path)

    result = _run(command, log_path)
    if not database_file.is_file() or database_file.stat().st_size == 0:
        raise ProcessStartError(f"1C did not create {database_file}; see {log_path}")
    return result


def create_empty_infobase(
    config: RuntimeConfig, *, recreate: bool = False
) -> ToolResult:
    return _create_empty_infobase(
        config,
        config.infobase_dir,
        create_infobase_command(config),
        config.logs_dir / "create-infobase.log",
        recreate=recreate,
    )


def create_build_infobase(
    config: RuntimeConfig, *, recreate: bool = False
) -> ToolResult:
    return _create_empty_infobase(
        config,
        config.build_infobase_dir,
        create_build_infobase_command(config),
        config.logs_dir / "create-build-infobase.log",
        recreate=recreate,
    )


def build_external_processor_from_files(
    config: RuntimeConfig,
    source_xml: Path,
    output_epf: Path,
    log_path: Path,
) -> ToolResult:
    _prepare_runtime_directories(config)
    if not source_xml.is_file():
        raise ProcessStartError(f"External processor source is missing: {source_xml}")
    if not (config.build_infobase_dir / "1Cv8.1CD").is_file():
        raise ProcessStartError("The dedicated build infobase has not been created")
    output_epf.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    output_epf.unlink(missing_ok=True)
    result = _run(
        build_external_processor_from_files_command(
            config, source_xml, output_epf, log_path
        ),
        log_path,
    )
    if not output_epf.is_file() or output_epf.stat().st_size == 0:
        raise ProcessStartError(f"1C did not build {output_epf}; see {log_path}")
    return result


def build_kernel(config: RuntimeConfig) -> ToolResult:
    _prepare_runtime_directories(config)
    source = config.workspace / "onec" / "Kernel" / "Kernel.xml"
    if not source.is_file():
        raise ProcessStartError(f"Kernel source is missing: {source}")
    if not (config.build_infobase_dir / "1Cv8.1CD").is_file():
        raise ProcessStartError("The dedicated build infobase has not been created")

    command = build_external_processor_command(config)
    log_path = config.logs_dir / "build-kernel.log"
    result = _run(command, log_path)
    if not config.kernel_epf.is_file() or config.kernel_epf.stat().st_size == 0:
        raise ProcessStartError(f"1C did not build {config.kernel_epf}; see {log_path}")
    return result


def build_worker(config: RuntimeConfig) -> ToolResult:
    _prepare_runtime_directories(config)
    source = config.workspace / "onec" / "Worker" / "Ext" / "ObjectModule.bsl"
    if not source.is_file():
        raise ProcessStartError(f"Worker source is missing: {source}")
    from onec_runtime.worker_epf import build_worker_epf

    source_text = source.read_text(encoding="utf-8-sig")
    build_worker_epf(source_text, config.worker_epf)
    command = (
        "in-process-worker-epf",
        str(source),
        str(config.worker_epf),
    )
    log_path = config.logs_dir / "build-worker.log"
    log_path.write_text(
        "PASS\n"
        f"source_sha256={sha256(source_text.encode('utf-8')).hexdigest()}\n"
        f"artifact_sha256={sha256(config.worker_epf.read_bytes()).hexdigest()}\n",
        encoding="utf-8",
        newline="\n",
    )
    return ToolResult(command, 0, log_path)


def build_runtime_extension(
    config: RuntimeConfig,
) -> tuple[ToolResult, ToolResult]:
    _prepare_runtime_directories(config)
    source = config.workspace / "onec" / EXTENSION_NAME
    if not (source / "Configuration.xml").is_file():
        raise ProcessStartError(
            f"Runtime extension source is missing: {source}"
        )
    if not (config.build_infobase_dir / "1Cv8.1CD").is_file():
        raise ProcessStartError("The dedicated build infobase has not been created")
    output_cfe = config.build_dir / f"{EXTENSION_NAME}.cfe"
    output_cfe.unlink(missing_ok=True)
    load_log = config.logs_dir / "load-extension-source.log"
    loaded = _run(
        load_extension_source_command(config, source),
        load_log,
    )
    dump_log = config.logs_dir / "dump-extension.log"
    dumped = _run(dump_extension_command(config, output_cfe), dump_log)
    if not output_cfe.is_file() or output_cfe.stat().st_size == 0:
        raise ProcessStartError(f"1C did not build {output_cfe}")
    return loaded, dumped


def deploy_runtime_extension(
    config: RuntimeConfig,
) -> tuple[ToolResult, ToolResult]:
    output_cfe = config.build_dir / f"{EXTENSION_NAME}.cfe"
    if not output_cfe.is_file() or output_cfe.stat().st_size == 0:
        raise ProcessStartError(f"{output_cfe.name} has not been built")
    if not (config.infobase_dir / "1Cv8.1CD").is_file():
        raise ProcessStartError("The dedicated runtime infobase has not been created")
    loaded = _run(
        deploy_extension_command(config),
        config.logs_dir / "load-extension.log",
    )
    updated = _run(
        apply_extension_command(config),
        config.logs_dir / "apply-extension.log",
    )
    return loaded, updated


def deploy_runtime_extension_from_source(
    config: RuntimeConfig,
) -> tuple[ToolResult, ToolResult]:
    source = config.workspace / "onec" / EXTENSION_NAME
    if not (source / "Configuration.xml").is_file():
        raise ProcessStartError(
            f"Runtime extension source is missing: {source}"
        )
    if not (config.infobase_dir / "1Cv8.1CD").is_file():
        raise ProcessStartError("The dedicated runtime infobase has not been created")
    loaded = _run(
        load_target_extension_source_command(config, source),
        config.logs_dir / "load-target-extension-source.log",
    )
    updated = _run(
        apply_target_extension_source_command(config, source),
        config.logs_dir / "apply-extension-source.log",
    )
    return loaded, updated
