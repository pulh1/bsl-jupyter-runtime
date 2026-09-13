import ast
from pathlib import Path

import pytest

import onec_runtime.toolchain as toolchain_module
from onec_runtime.config import RuntimeConfig
from onec_runtime.errors import ExtensionNotInstalled, ProcessStartError
from onec_runtime.extension_bundle import EXTENSION_NAME
from onec_runtime.toolchain import (
    ToolResult,
    apply_product_extension_command,
    build_external_processor_command,
    build_external_processor_from_files,
    build_external_processor_from_files_command,
    build_runtime_extension,
    build_worker,
    create_file_infobase_at,
    create_file_infobase_at_command,
    create_infobase_command,
    deploy_extension_command,
    deploy_runtime_extension,
    deploy_runtime_extension_from_source,
    dump_extension_command,
    dump_product_extension_cfe,
    dump_product_extension_cfe_command,
    dump_target_extension_cfe,
    dump_target_extension_cfe_command,
    dump_target_extension_files,
    dump_target_extension_files_command,
    load_extension_source_command,
    load_product_extension_source_command,
    load_target_extension_cfe_command,
    load_target_extension_source_command,
    update_extension_command,
)
from onec_runtime.worker_epf import read_worker_source


def make_config(tmp_path: Path) -> RuntimeConfig:
    bin_dir = tmp_path / "Program Files" / "1cv8" / "bin"
    bin_dir.mkdir(parents=True)
    for name in ("1cv8.exe", "1cv8c.exe", "dbgs.exe"):
        (bin_dir / name).touch()
    return RuntimeConfig(workspace=tmp_path, platform_bin=bin_dir)


def make_minimal_extension_source(config: RuntimeConfig) -> None:
    canonical = config.workspace / "onec" / EXTENSION_NAME
    (canonical / "Languages").mkdir(parents=True)
    (canonical / "Configuration.xml").write_text("<Configuration />", encoding="utf-8")
    (canonical / "Languages" / "Русский.xml").write_text(
        "<ExtendedConfigurationObject>"
        "c78f1a83-0be9-4936-b338-a4a39cd589c8"
        "</ExtendedConfigurationObject>",
        encoding="utf-8",
    )
    (canonical / "ConfigDumpInfo.xml").write_text(
        '<Metadata name="Language.Русский" configVersion="old"/>',
        encoding="utf-8",
    )
def test_create_infobase_command_preserves_argument_boundaries(tmp_path: Path) -> None:
    config = make_config(tmp_path)

    assert create_infobase_command(config) == [
        str(config.designer_exe),
        "CREATEINFOBASE",
        f"File={config.infobase_dir};",
        "/DisableStartupDialogs",
        "/DisableStartupMessages",
        "/Out",
        str(config.logs_dir / "create-infobase.log"),
    ]


def test_product_dump_command_has_fixed_name_and_explicit_paths(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    output = tmp_path / "dump"
    log = tmp_path / "dump.log"

    command = dump_target_extension_files_command(config, output, log)

    assert command[command.index("/DumpConfigToFiles") + 1] == str(output)
    assert command[command.index("-Extension") + 1] == EXTENSION_NAME
    assert command[command.index("/Out") + 1] == str(log)
    assert "empty" not in command and "zup" not in command


def test_product_extension_target_commands_keep_load_and_apply_separate(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    cfe = tmp_path / "OnecInteractiveRuntime.cfe"
    log = tmp_path / "target.log"

    dump = dump_target_extension_cfe_command(config, cfe, log)
    load = load_target_extension_cfe_command(config, cfe, log)
    apply = apply_product_extension_command(config, log)

    assert dump[dump.index("/DumpCfg") + 1] == str(cfe)
    assert load[load.index("/LoadCfg") + 1] == str(cfe)
    assert "/UpdateDBCfg" in apply and "/LoadCfg" not in apply
    assert all(
        command[command.index("-Extension") + 1] == EXTENSION_NAME
        for command in (dump, load, apply)
    )
    assert "-Dynamic+" in apply


def test_product_extension_release_build_commands_use_only_explicit_paths(
    tmp_path: Path,
) -> None:
    designer = tmp_path / "bin" / "1cv8.exe"
    infobase = tmp_path / "fresh infobase"
    source = tmp_path / "source"
    cfe = tmp_path / "release" / "OnecInteractiveRuntime.cfe"
    log = tmp_path / "logs" / "designer.log"

    create = create_file_infobase_at_command(designer, infobase, log)
    load = load_product_extension_source_command(designer, infobase, source, log)
    dump = dump_product_extension_cfe_command(designer, infobase, cfe, log)

    assert create == [
        str(designer),
        "CREATEINFOBASE",
        f"File={infobase};",
        "/DisableStartupDialogs",
        "/DisableStartupMessages",
        "/Out",
        str(log),
    ]
    assert load[load.index("/F") + 1] == str(infobase)
    assert load[load.index("/LoadConfigFromFiles") + 1] == str(source)
    assert load[load.index("-Extension") + 1] == EXTENSION_NAME
    assert dump[dump.index("/F") + 1] == str(infobase)
    assert dump[dump.index("/DumpCfg") + 1] == str(cfe)
    assert dump[dump.index("-Extension") + 1] == EXTENSION_NAME


@pytest.mark.parametrize("diagnostic", [
    "Операция не может быть выполнена, так как расширение конфигурации "
    "с указанным именем не найдено: OnecInteractiveRuntime\n",
    "Cannot perform the operation because the following configuration extension "
    "is not found: OnecInteractiveRuntime\n",
])
def test_product_dump_maps_only_exact_missing_diagnostic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, diagnostic: str
) -> None:
    config = make_config(tmp_path)
    log = tmp_path / "missing.log"
    def fail_run(command: list[str], log_path: Path) -> ToolResult:
        log_path.write_text(diagnostic, encoding="utf-8-sig")
        raise ProcessStartError(f"1C tool exited with code 1; see {log_path}")

    monkeypatch.setattr(toolchain_module, "_run", fail_run)

    with pytest.raises(ExtensionNotInstalled):
        dump_target_extension_files(config, tmp_path / "dump", log)


@pytest.mark.parametrize("diagnostic", [
    "Операция не может быть выполнена, так как расширение конфигурации "
    "с указанным именем не найдено: OnecInteractiveRuntimeOther\n",
    "Cannot perform the operation because the following configuration extension "
    "is not found: OnecInteractiveRuntimeOther\n",
    "Access denied: OnecInteractiveRuntime\n",
])
def test_product_dump_does_not_install_for_unrelated_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, diagnostic: str
) -> None:
    config = make_config(tmp_path)

    def fail_run(command: list[str], log_path: Path) -> ToolResult:
        log_path.write_text(diagnostic, encoding="utf-8")
        raise ProcessStartError("designer failed")

    monkeypatch.setattr(toolchain_module, "_run", fail_run)
    with pytest.raises(ProcessStartError) as raised:
        dump_target_extension_files(config, tmp_path / "dump", tmp_path / "inspect.log")
    assert not isinstance(raised.value, ExtensionNotInstalled)


def test_product_dump_keeps_authentication_failure_as_process_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_config(tmp_path)
    log = tmp_path / "authentication.log"

    def fail_run(command: list[str], log_path: Path) -> ToolResult:
        log_path.write_text("The infobase user is not authenticated\n", encoding="utf-8")
        raise ProcessStartError(f"1C tool exited with code 1; see {log_path}")

    monkeypatch.setattr(toolchain_module, "_run", fail_run)

    with pytest.raises(ProcessStartError) as captured:
        dump_target_extension_files(config, tmp_path / "dump", log)

    assert not isinstance(captured.value, ExtensionNotInstalled)
    assert "не прошёл аутентификацию" in str(captured.value)


def test_product_dump_reports_infobase_lock_without_disclosing_log_contents(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_config(tmp_path)
    log = tmp_path / "inspect.log"
    private_text = "private user data"

    def fail_run(command: list[str], log_path: Path) -> ToolResult:
        log_path.write_text(
            "Error locking infobase for configuration.\n" + private_text,
            encoding="utf-8",
        )
        raise ProcessStartError(f"1C tool exited with code 1; see {log_path}")

    monkeypatch.setattr(toolchain_module, "_run", fail_run)

    with pytest.raises(ProcessStartError) as caught:
        dump_target_extension_files(config, tmp_path / "dump", log)

    assert "закройте Конфигуратор" in str(caught.value)
    assert str(log) in str(caught.value)
    assert private_text not in str(caught.value)


def test_product_dump_reports_missing_infobase_from_designer_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    log = tmp_path / "inspect.log"

    def fail_run(command: list[str], log_path: Path) -> ToolResult:
        log_path.write_text(
            "The infobase is not found.\nprivate user data",
            encoding="utf-8",
        )
        raise ProcessStartError(f"1C tool exited with code 1; see {log_path}")

    monkeypatch.setattr(toolchain_module, "_run", fail_run)

    with pytest.raises(ProcessStartError) as caught:
        dump_target_extension_files(config, tmp_path / "dump", log)

    assert "информационная база не найдена" in str(caught.value)
    assert "Srvr/Ref" in str(caught.value)
    assert "private user data" not in str(caught.value)


def test_product_dump_clears_stale_missing_extension_diagnostic_before_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_config(tmp_path)
    log = tmp_path / "stale-missing.log"
    log.write_text(
        "Операция не может быть выполнена, так как расширение конфигурации "
        "с указанным именем не найдено: OnecInteractiveRuntime\n",
        encoding="utf-8",
    )

    def fail_run(command: list[str], log_path: Path) -> ToolResult:
        assert not log_path.exists()
        raise ProcessStartError("authentication failed")

    monkeypatch.setattr(toolchain_module, "_run", fail_run)

    with pytest.raises(ProcessStartError, match="authentication failed") as captured:
        dump_target_extension_files(config, tmp_path / "dump", log)

    assert not isinstance(captured.value, ExtensionNotInstalled)


def test_product_output_operations_reject_zero_exit_without_fresh_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_config(tmp_path)

    def no_output(command: list[str], log_path: Path) -> ToolResult:
        return ToolResult(tuple(command), 0, log_path)

    monkeypatch.setattr(toolchain_module, "_run", no_output)

    with pytest.raises(ProcessStartError, match="1Cv8.1CD"):
        create_file_infobase_at(
            config.designer_exe,
            tmp_path / "fresh-infobase",
            tmp_path / "create.log",
        )
    with pytest.raises(ProcessStartError, match="CFE"):
        dump_product_extension_cfe(
            config.designer_exe,
            tmp_path / "fresh-infobase",
            tmp_path / "product.cfe",
            tmp_path / "product.log",
        )
    with pytest.raises(ProcessStartError, match="CFE"):
        dump_target_extension_cfe(
            config, tmp_path / "target.cfe", tmp_path / "target.log"
        )
    with pytest.raises(ProcessStartError, match="extension dump"):
        dump_target_extension_files(config, tmp_path / "dump", tmp_path / "dump.log")


def test_product_output_operations_accept_fresh_non_empty_results(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_config(tmp_path)
    infobase = tmp_path / "fresh-infobase"
    product_cfe = tmp_path / "product.cfe"
    target_cfe = tmp_path / "target.cfe"
    dump_dir = tmp_path / "dump"

    def write_output(command: list[str], log_path: Path) -> ToolResult:
        if command[1] == "CREATEINFOBASE":
            infobase.mkdir()
            (infobase / "1Cv8.1CD").write_bytes(b"database")
        elif "/DumpConfigToFiles" in command:
            dump_dir.mkdir()
            (dump_dir / "Configuration.xml").write_text(
                "<Configuration />", encoding="utf-8"
            )
        elif command[command.index("/DumpCfg") + 1] == str(product_cfe):
            product_cfe.write_bytes(b"product-cfe")
        elif command[command.index("/DumpCfg") + 1] == str(target_cfe):
            target_cfe.write_bytes(b"target-cfe")
        return ToolResult(tuple(command), 0, log_path)

    monkeypatch.setattr(toolchain_module, "_run", write_output)

    create_file_infobase_at(config.designer_exe, infobase, tmp_path / "create.log")
    dump_product_extension_cfe(
        config.designer_exe,
        infobase,
        product_cfe,
        tmp_path / "product.log",
    )
    dump_target_extension_cfe(config, target_cfe, tmp_path / "target.log")
    dump_target_extension_files(config, dump_dir, tmp_path / "dump.log")


def test_product_dump_operations_remove_stale_outputs_before_designer_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_config(tmp_path)
    dump_dir = tmp_path / "dump"
    dump_dir.mkdir()
    (dump_dir / "stale.xml").write_text("stale", encoding="utf-8")
    cfe = tmp_path / "release" / "OnecInteractiveRuntime.cfe"
    cfe.parent.mkdir()
    cfe.write_bytes(b"stale")
    commands: list[tuple[str, ...]] = []

    def record(command: list[str], log_path: Path) -> ToolResult:
        if "/DumpConfigToFiles" in command:
            assert not dump_dir.exists()
            dump_dir.mkdir()
            (dump_dir / "Configuration.xml").write_text(
                "<Configuration />", encoding="utf-8"
            )
        if "/DumpCfg" in command:
            assert not cfe.exists()
            cfe.write_bytes(b"fresh-cfe")
        result = ToolResult(tuple(command), 0, log_path)
        commands.append(result.command)
        return result

    monkeypatch.setattr(toolchain_module, "_run", record)

    dump_target_extension_files(config, dump_dir, tmp_path / "dump.log")
    dump_target_extension_cfe(config, cfe, tmp_path / "cfe.log")

    assert [command[command.index("-Extension") + 1] for command in commands] == [
        EXTENSION_NAME,
        EXTENSION_NAME,
    ]


def test_build_kernel_command_uses_root_xml_and_output_epf(tmp_path: Path) -> None:
    config = make_config(tmp_path)

    assert build_external_processor_command(config) == [
        str(config.designer_exe),
        "DESIGNER",
        "/F",
        str(config.build_infobase_dir),
        "/LoadExternalDataProcessorOrReportFromFiles",
        str(config.workspace / "onec" / "Kernel" / "Kernel.xml"),
        str(config.kernel_epf),
        "/DisableStartupDialogs",
        "/DisableStartupMessages",
        "/Out",
        str(config.logs_dir / "build-kernel.log"),
    ]


def test_build_worker_uses_in_process_packer_without_build_infobase_or_designer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config(tmp_path)
    source = config.workspace / "onec" / "Worker" / "Ext" / "ObjectModule.bsl"
    source.parent.mkdir(parents=True)
    source.write_text("Процедура Ping() Экспорт\nКонецПроцедуры", encoding="utf-8")

    monkeypatch.setattr(
        toolchain_module,
        "_run",
        lambda *_args, **_kwargs: pytest.fail("Designer must not build Worker.epf"),
    )

    result = build_worker(config)

    assert not config.build_infobase_dir.exists()
    assert result.returncode == 0
    assert result.command[0] == "in-process-worker-epf"
    assert result.log_path.read_text(encoding="utf-8").startswith("PASS")
    assert read_worker_source(config.worker_epf) == source.read_text(encoding="utf-8")


def test_explicit_external_processor_build_preserves_all_path_boundaries(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    source = tmp_path / "source dir" / "Worker.xml"
    output = tmp_path / "output dir" / "Worker-v1.epf"
    log = tmp_path / "log dir" / "worker-v1.log"

    assert build_external_processor_from_files_command(config, source, output, log) == [
        str(config.designer_exe),
        "DESIGNER",
        "/F",
        str(config.build_infobase_dir),
        "/LoadExternalDataProcessorOrReportFromFiles",
        str(source),
        str(output),
        "/DisableStartupDialogs",
        "/DisableStartupMessages",
        "/Out",
        str(log),
    ]


def test_explicit_external_processor_build_rejects_missing_source(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    config.build_infobase_dir.mkdir(parents=True)
    (config.build_infobase_dir / "1Cv8.1CD").touch()

    with pytest.raises(ProcessStartError, match="source is missing"):
        build_external_processor_from_files(
            config,
            tmp_path / "missing.xml",
            tmp_path / "Worker.epf",
            tmp_path / "worker.log",
        )


def test_explicit_external_processor_build_rejects_empty_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config(tmp_path)
    source = tmp_path / "Worker.xml"
    output = tmp_path / "Worker.epf"
    log = tmp_path / "worker.log"
    source.write_text("<root />", encoding="utf-8")
    config.build_infobase_dir.mkdir(parents=True)
    (config.build_infobase_dir / "1Cv8.1CD").touch()

    def no_output(command: list[str], log_path: Path) -> ToolResult:
        return ToolResult(tuple(command), 0, log_path)

    monkeypatch.setattr(toolchain_module, "_run", no_output)

    with pytest.raises(ProcessStartError, match="did not build"):
        build_external_processor_from_files(config, source, output, log)


def test_explicit_external_processor_build_never_accepts_stale_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config(tmp_path)
    source = tmp_path / "Worker.xml"
    output = tmp_path / "Worker.epf"
    log = tmp_path / "worker.log"
    source.write_text("<root />", encoding="utf-8")
    output.write_bytes(b"stale-worker")
    config.build_infobase_dir.mkdir(parents=True)
    (config.build_infobase_dir / "1Cv8.1CD").touch()

    def successful_without_output(command: list[str], log_path: Path) -> ToolResult:
        assert not output.exists()
        return ToolResult(tuple(command), 0, log_path)

    monkeypatch.setattr(toolchain_module, "_run", successful_without_output)

    with pytest.raises(ProcessStartError, match="did not build"):
        build_external_processor_from_files(config, source, output, log)


def test_source_deploy_never_executes_bsl_to_change_extension_security(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config(tmp_path)
    config.infobase_dir.mkdir(parents=True)
    (config.infobase_dir / "1Cv8.1CD").touch()
    make_minimal_extension_source(config)
    commands: list[tuple[str, ...]] = []

    def record(command: list[str], log_path: Path) -> ToolResult:
        result = ToolResult(tuple(command), 0, log_path)
        commands.append(result.command)
        return result

    monkeypatch.setattr(toolchain_module, "_run", record)

    results = deploy_runtime_extension_from_source(config)

    assert len(results) == 2
    assert len(commands) == 2
    assert all("/CheckConfig" not in command for command in commands)
    assert all("ENTERPRISE" not in command for command in commands)
    assert all("/Execute" not in command for command in commands)
    assert "/LoadConfigFromFiles" in commands[-1]
    assert "/UpdateDBCfg" in commands[-1]


def test_cfe_deploy_applies_extension_in_same_designer_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config(tmp_path)
    config.infobase_dir.mkdir(parents=True)
    (config.infobase_dir / "1Cv8.1CD").touch()
    artifact = config.build_dir / f"{EXTENSION_NAME}.cfe"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"cfe")
    commands: list[tuple[str, ...]] = []

    def record(command: list[str], log_path: Path) -> ToolResult:
        result = ToolResult(tuple(command), 0, log_path)
        commands.append(result.command)
        return result

    monkeypatch.setattr(toolchain_module, "_run", record)

    deploy_runtime_extension(config)

    assert len(commands) == 2
    assert all("/CheckConfig" not in command for command in commands)
    assert "/LoadCfg" in commands[-1]
    assert "/UpdateDBCfg" in commands[-1]
    assert commands[-1].count("-Extension") == 1


def test_target_extension_source_is_loaded_in_extended_configuration_context(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    staged = tmp_path / "staged extension"

    command = load_target_extension_source_command(config, staged)

    assert command[command.index("/F") + 1] == str(config.infobase_dir)
    assert command[command.index("/LoadConfigFromFiles") + 1] == str(staged)
    assert command[command.index("-Extension") + 1] == "OnecInteractiveRuntime"
    assert "-updateConfigDumpInfo" in command
    assert "/DisableStartupDialogs" in command
    assert "/DisableStartupMessages" in command
    assert command[command.index("/N") + 1] == ""
    assert command[command.index("/P") + 1] == ""


def test_external_extension_deploy_uses_named_passwordless_user(tmp_path: Path) -> None:
    base_config = make_config(tmp_path)
    external_base = tmp_path / "zup"
    external_base.mkdir()
    (external_base / "1Cv8.1CD").touch()
    username = "Савинская З.Ю. (Системный программист)"
    config = RuntimeConfig(
        workspace=tmp_path,
        platform_bin=base_config.platform_bin,
        connection_string=f'File="{external_base}";',
        username=username,
    )

    for command in (
        deploy_extension_command(config),
        update_extension_command(config),
    ):
        assert "/DisableStartupDialogs" in command
        assert "/DisableStartupMessages" in command
        assert command[command.index("/F") + 1] == str(external_base.resolve())
        assert command[command.index("/N") + 1] == username
        assert command[command.index("/P") + 1] == ""

    zup_update = update_extension_command(config)
    assert "-Dynamic+" not in zup_update
    assert "-Dynamic-" in zup_update


def test_product_commands_use_source_output_and_fixed_logs(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    staged = tmp_path / "staged zup extension"
    output = config.build_dir / f"{EXTENSION_NAME}.cfe"

    load = load_extension_source_command(config, staged)
    dump = dump_extension_command(config, output)

    assert load[load.index("/LoadConfigFromFiles") + 1] == str(staged)
    assert load[load.index("/Out") + 1] == str(
        config.logs_dir / "load-extension-source.log"
    )
    assert dump[dump.index("/DumpCfg") + 1] == str(output)
    assert dump[dump.index("/Out") + 1] == str(
        config.logs_dir / "dump-extension.log"
    )


def test_product_build_never_accepts_stale_cfe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config(tmp_path)
    make_minimal_extension_source(config)
    config.build_infobase_dir.mkdir(parents=True)
    (config.build_infobase_dir / "1Cv8.1CD").touch()
    output = config.build_dir / f"{EXTENSION_NAME}.cfe"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"stale-cfe")

    def successful_without_output(command: list[str], log_path: Path) -> ToolResult:
        if "/DumpCfg" in command:
            assert not output.exists()
        return ToolResult(tuple(command), 0, log_path)

    monkeypatch.setattr(toolchain_module, "_run", successful_without_output)

    with pytest.raises(ProcessStartError, match="did not build"):
        build_runtime_extension(config)


def test_every_extension_toolchain_caller_uses_fixed_product_api() -> None:
    workspace = Path(__file__).parents[2]
    tool_names = {
        "build_runtime_extension",
        "deploy_runtime_extension",
        "deploy_runtime_extension_from_source",
    }
    observed: dict[str, set[str]] = {}
    caller_paths = (
        tuple((workspace / "src" / "onec_runtime").glob("*.py"))
        + tuple((workspace / "integration").glob("*.py"))
        + tuple((workspace / "tools").glob("*.py"))
    )
    for path in caller_paths:
        if path.name == "toolchain.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id not in tool_names:
                continue
            assert len(node.args) == 1, f"{path.name}: {node.func.id} is not fixed"
            observed.setdefault(path.name, set()).add(node.func.id)

    assert observed
