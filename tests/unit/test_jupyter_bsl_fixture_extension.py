from __future__ import annotations

from pathlib import Path
import shutil
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest

from onec_runtime.config import RuntimeConfig
from onec_runtime.errors import ProcessStartError

from integration.jupyter_bsl_fixture.extension import (
    CALLEE_CAPTURE_FRAGMENT,
    CALLER_CAPTURE_FRAGMENT,
    FIXTURE_EXTENSION_NAME,
    build_fixture_extension,
    install_fixture_extension,
    install_minimal_host_configuration,
    install_product_extension,
    validate_fixture_extension_source,
)


REPOSITORY = Path(__file__).resolve().parents[2]
SOURCE = REPOSITORY / "tests" / "fixtures" / "onec" / FIXTURE_EXTENSION_NAME
PLATFORM = Path(r"C:\Program Files\1cv8\8.3.27.2170\bin")


def _extension_argument(command: list[str]) -> str:
    return command[command.index("-Extension") + 1]


def _runtime_config(tmp_path: Path) -> RuntimeConfig:
    platform = tmp_path / "platform"
    platform.mkdir()
    for executable in ("1cv8.exe", "1cv8c.exe", "dbgs.exe"):
        (platform / executable).write_bytes(b"fake")
    infobase = tmp_path / "target"
    infobase.mkdir()
    (infobase / "1Cv8.1CD").write_bytes(b"fake")
    return RuntimeConfig(
        workspace=tmp_path,
        platform_bin=platform,
        connection_string=f'File="{infobase}";',
        username="",
    )


def test_fixture_extension_source_exposes_two_unique_capture_points() -> None:
    contract = validate_fixture_extension_source(SOURCE)

    assert contract.extension_name == FIXTURE_EXTENSION_NAME
    assert contract.module_names == (
        "JupyterBslFixtureCallerServer",
        "JupyterBslFixtureCalleeServer",
    )
    assert contract.fragment_counts == {
        CALLEE_CAPTURE_FRAGMENT: 1,
        CALLER_CAPTURE_FRAGMENT: 1,
    }
    assert len(contract.source_sha256) == 64
    callee = (
        SOURCE
        / "CommonModules"
        / "JupyterBslFixtureCalleeServer"
        / "Ext"
        / "Module.bsl"
    ).read_text(encoding="utf-8")
    assert "ЛокальныйMixed = 0;" in callee
    assert '"Счетчик,Метка,Таблица,Mixed"' in callee


def test_fixture_extension_does_not_control_infobase_language() -> None:
    configuration = ElementTree.parse(SOURCE / "Configuration.xml").getroot()
    property_names = {
        element.tag.rsplit("}", 1)[-1]
        for element in configuration.iter()
    }
    assert "DefaultLanguage" not in property_names
    assert "InterfaceCompatibilityMode" not in property_names
    assert not (SOURCE / "Languages").exists()

    dump_info = (SOURCE / "ConfigDumpInfo.xml").read_text(encoding="utf-8-sig")
    assert 'name="Language.' not in dump_info


def test_source_validation_rejects_changed_metadata_identity(tmp_path: Path) -> None:
    source = tmp_path / FIXTURE_EXTENSION_NAME
    shutil.copytree(SOURCE, source)
    configuration = source / "Configuration.xml"
    configuration.write_text(
        configuration.read_text(encoding="utf-8").replace(
            "7b22aaef-49a8-4f1b-9c1b-515394df8d01",
            "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ProcessStartError, match="configuration identity"):
        validate_fixture_extension_source(source)


def test_source_validation_rejects_duplicate_capture_fragment(tmp_path: Path) -> None:
    source = tmp_path / FIXTURE_EXTENSION_NAME
    source.mkdir()
    (source / "Configuration.xml").write_text(
        f"<Name>{FIXTURE_EXTENSION_NAME}</Name>", encoding="utf-8"
    )
    module = source / "CommonModules" / "Broken" / "Ext"
    module.mkdir(parents=True)
    (module / "Module.bsl").write_text(
        f"{CALLEE_CAPTURE_FRAGMENT}\n{CALLEE_CAPTURE_FRAGMENT}\n"
        f"{CALLER_CAPTURE_FRAGMENT}\n",
        encoding="utf-8",
    )

    with pytest.raises(ProcessStartError, match="capture fragment"):
        validate_fixture_extension_source(source)


def test_build_uses_fixture_name_and_creates_nonempty_cfe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(
        "integration.jupyter_bsl_fixture.extension.create_file_infobase_at",
        lambda *_args: SimpleNamespace(returncode=0),
    )

    def fake_run(command: list[str], _log: Path) -> SimpleNamespace:
        commands.append(list(command))
        if "/DumpCfg" in command:
            Path(command[command.index("/DumpCfg") + 1]).write_bytes(b"fixture-cfe")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(
        "integration.jupyter_bsl_fixture.extension.run_tool_command", fake_run
    )

    artifact = build_fixture_extension(PLATFORM, SOURCE, tmp_path)

    assert artifact.cfe_path.read_bytes() == b"fixture-cfe"
    assert artifact.source_root == SOURCE.resolve()
    load_command = next(
        command for command in commands if "/LoadConfigFromFiles" in command
    )
    loaded_source = Path(
        load_command[load_command.index("/LoadConfigFromFiles") + 1]
    )
    assert loaded_source != SOURCE.resolve()
    assert loaded_source.parent == tmp_path.resolve()
    assert [
        next(item for item in command if item in {"/LoadConfigFromFiles", "/DumpCfg"})
        for command in commands
    ] == ["/LoadConfigFromFiles", "/DumpCfg"]
    assert {_extension_argument(command) for command in commands} == {
        FIXTURE_EXTENSION_NAME
    }


def test_build_rejects_missing_cfe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "integration.jupyter_bsl_fixture.extension.create_file_infobase_at",
        lambda *_args: SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(
        "integration.jupyter_bsl_fixture.extension.run_tool_command",
        lambda *_args: SimpleNamespace(returncode=0),
    )

    with pytest.raises(ProcessStartError, match="non-empty fixture CFE"):
        build_fixture_extension(PLATFORM, SOURCE, tmp_path)


def test_install_runs_load_and_update_for_fixture_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(
        "integration.jupyter_bsl_fixture.extension.run_tool_command",
        lambda command, _log: commands.append(list(command)),
    )
    artifact = SimpleNamespace(
        source_root=SOURCE.resolve(),
        cfe_path=tmp_path / "fixture.cfe",
        source_sha256="a" * 64,
    )
    artifact.cfe_path.write_bytes(b"fixture")
    config = _runtime_config(tmp_path)

    install_fixture_extension(config, artifact, tmp_path / "logs")

    assert [
        next(item for item in command if item in {"/LoadCfg", "/UpdateDBCfg"})
        for command in commands
    ] == ["/LoadCfg", "/UpdateDBCfg"]
    assert {_extension_argument(command) for command in commands} == {
        FIXTURE_EXTENSION_NAME
    }
    assert "-Dynamic+" in commands[-1]


def test_product_bootstrap_installs_packaged_cfe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _runtime_config(tmp_path)
    cfe = tmp_path / "OnecInteractiveRuntime.cfe"
    cfe.write_bytes(b"product")
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        "integration.jupyter_bsl_fixture.extension.packaged_extension_bundle",
        lambda _runtime_dir: SimpleNamespace(cfe_path=cfe),
    )
    for name in (
        "load_target_extension_cfe",
        "apply_product_extension",
    ):
        monkeypatch.setattr(
            f"integration.jupyter_bsl_fixture.extension.{name}",
            lambda *_args, _name=name: calls.append((_name, _args)),
        )

    install_product_extension(config, tmp_path / "product-logs")

    assert [name for name, _args in calls] == [
        "load_target_extension_cfe",
        "apply_product_extension",
    ]


def test_minimal_host_bootstrap_loads_checks_and_applies_base_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _runtime_config(tmp_path)
    source = tmp_path / "host-source"
    source.mkdir()
    (source / "Configuration.xml").write_text("<host/>", encoding="utf-8")
    commands: list[list[str]] = []
    monkeypatch.setattr(
        "integration.jupyter_bsl_fixture.extension.run_tool_command",
        lambda command, _log: commands.append(list(command)),
    )

    install_minimal_host_configuration(config, source, tmp_path / "host-logs")

    assert [
        next(
            item
            for item in command
            if item in {"/LoadConfigFromFiles", "/CheckConfig", "/UpdateDBCfg"}
        )
        for command in commands
    ] == ["/LoadConfigFromFiles", "/CheckConfig", "/UpdateDBCfg"]
    assert all("-Extension" not in command for command in commands)
    load = commands[0]
    staged_source = Path(load[load.index("/LoadConfigFromFiles") + 1])
    assert staged_source != source
    assert (staged_source / "Configuration.xml").read_bytes() == (source / "Configuration.xml").read_bytes()
