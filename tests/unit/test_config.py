import os
from pathlib import Path

import pytest

from onec_runtime.config import RuntimeConfig
from onec_runtime.session import RuntimeSessionConfig


def test_omitted_workspace_uses_stable_user_state_per_server_infobase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_state = tmp_path / "user-state"
    monkeypatch.setenv(
        "LOCALAPPDATA" if os.name == "nt" else "XDG_STATE_HOME",
        str(user_state),
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("1cv8.exe", "1cv8c.exe"):
        (bin_dir / name).touch()

    def config(reference: str) -> RuntimeConfig:
        return RuntimeConfig(
            platform_bin=bin_dir,
            connection_string=f'Srvr="localhost";Ref="{reference}";',
        )

    first = config("Payroll")
    assert first.workspace.parent == (
        user_state / "onec-interactive-runtime" / "workspaces"
    ).resolve()
    assert first.workspace == config("Payroll").workspace
    assert first.workspace != config("Accounting").workspace
    assert "Payroll" not in first.workspace.name
    assert RuntimeSessionConfig(first).evidence_root == first.workspace / "artifacts"


def test_omitted_workspace_keeps_temporary_infobase_under_user_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_state = tmp_path / "user-state"
    monkeypatch.setenv(
        "LOCALAPPDATA" if os.name == "nt" else "XDG_STATE_HOME",
        str(user_state),
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("1cv8.exe", "1cv8c.exe", "dbgs.exe"):
        (bin_dir / name).touch()

    config = RuntimeConfig(platform_bin=bin_dir)

    assert config.workspace.parent == (
        user_state / "onec-interactive-runtime" / "workspaces"
    ).resolve()
    assert config.infobase_dir == config.workspace / ".runtime" / "infobase"


def test_runtime_config_derives_required_executables(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("1cv8.exe", "1cv8c.exe", "dbgs.exe"):
        (bin_dir / name).touch()

    config = RuntimeConfig(workspace=tmp_path, platform_bin=bin_dir)

    assert config.designer_exe == bin_dir / "1cv8.exe"
    assert config.client_exe == bin_dir / "1cv8c.exe"
    assert config.debug_server_exe == bin_dir / "dbgs.exe"
    assert config.infobase_dir == tmp_path / ".runtime" / "infobase"
    assert config.build_infobase_dir == tmp_path / ".runtime" / "build-infobase"
    assert config.kernel_epf == tmp_path / ".runtime" / "build" / "Kernel.epf"


def test_runtime_config_has_no_legacy_extension_fields(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("1cv8.exe", "1cv8c.exe", "dbgs.exe"):
        (bin_dir / name).touch()

    config = RuntimeConfig(workspace=tmp_path, platform_bin=bin_dir)

    assert not hasattr(config, "extension_" + "profile")
    assert not hasattr(config, "runtime_extension_source")
    assert not hasattr(config, "runtime_extension_cfe")


def test_runtime_config_rejects_missing_platform_binary(tmp_path: Path) -> None:
    bin_dir = tmp_path / "missing"
    bin_dir.mkdir()
    (bin_dir / "1cv8.exe").touch()
    (bin_dir / "dbgs.exe").touch()

    with pytest.raises(ValueError, match="1cv8c.exe"):
        RuntimeConfig(workspace=tmp_path, platform_bin=bin_dir)


def test_runtime_config_accepts_external_infobase_without_moving_build_base(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("1cv8.exe", "1cv8c.exe", "dbgs.exe"):
        (bin_dir / name).touch()
    external_base = tmp_path / "existing-zup"
    external_base.mkdir()
    (external_base / "1Cv8.1CD").touch()

    config = RuntimeConfig(
        workspace=tmp_path,
        platform_bin=bin_dir,
        connection_string=f'File="{external_base}";',
        username="Савинская З.Ю. (Системный программист)",
    )

    assert config.infobase_dir == external_base.resolve()
    assert config.build_infobase_dir == tmp_path / ".runtime" / "build-infobase"
    assert config.username == "Савинская З.Ю. (Системный программист)"
    assert config.uses_external_infobase is True


def test_runtime_config_accepts_string_paths_for_file_infobase(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("1cv8.exe", "1cv8c.exe", "dbgs.exe"):
        (bin_dir / name).touch()
    base = tmp_path / "БП_KZ"
    base.mkdir()
    (base / "1Cv8.1CD").touch()

    config = RuntimeConfig(
        workspace=str(tmp_path),
        platform_bin=str(bin_dir),
        connection_string=f'File="{base}";',
    )

    assert config.workspace == tmp_path.resolve()
    assert config.platform_bin == bin_dir.resolve()
    assert config.infobase_arguments == ("/F", str(base.resolve()))


def test_file_connection_string_selects_quoted_path(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("1cv8.exe", "1cv8c.exe", "dbgs.exe"):
        (bin_dir / name).touch()
    base = tmp_path / "БП_KZ;проверка"
    base.mkdir()
    (base / "1Cv8.1CD").touch()

    config = RuntimeConfig(
        workspace=str(tmp_path),
        platform_bin=str(bin_dir),
        connection_string=f'File="{base}";',
    )

    assert config.infobase_dir == base.resolve()
    assert config.infobase_arguments == ("/F", str(base.resolve()))
    assert config.uses_external_infobase


def test_server_connection_string_selects_server_and_reference(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("1cv8.exe", "1cv8c.exe"):
        (bin_dir / name).touch()

    config = RuntimeConfig(
        workspace=str(tmp_path),
        platform_bin=str(bin_dir),
        connection_string='Srvr="localhost";Ref="ZUP_CORP_DEMP_JUPYTER";',
    )

    assert config.connection_string == 'Srvr="localhost";Ref="ZUP_CORP_DEMP_JUPYTER";'
    assert config.infobase_arguments == ("/S", r"localhost\ZUP_CORP_DEMP_JUPYTER")
    assert config.infobase_debug_alias == "ZUP_CORP_DEMP_JUPYTER"
    assert config.is_server_infobase


@pytest.mark.parametrize(
    "legacy_option,legacy_value",
    [
        ("infobase_path", "C:/old-base"),
        ("server_infobase", r"localhost\old-base"),
        ("infobase_name", "old-base"),
    ],
)
def test_connection_string_is_the_only_explicit_infobase_selector(
    tmp_path: Path, legacy_option: str, legacy_value: str,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("1cv8.exe", "1cv8c.exe", "dbgs.exe"):
        (bin_dir / name).touch()

    with pytest.raises(TypeError, match=legacy_option):
        RuntimeConfig(
            workspace=tmp_path,
            platform_bin=bin_dir,
            **{legacy_option: legacy_value},
        )


@pytest.mark.parametrize(
    "connection",
    [
        'File=C:\\secret;',
        'File="";',
        'Srvr="localhost";',
        'Ref="demo";',
        'File="C:\\base";Srvr="localhost";Ref="demo";',
        'Srvr="localhost";Ref="demo";Pwd="private-secret";',
        'File="C:\\base";File="C:\\other";',
        'File="C:\\base";garbage',
        'File="C:\\base"\n;',
        'File="C:\\base";\n',
    ],
)
def test_invalid_connection_string_is_rejected_without_echoing_values(
    tmp_path: Path, connection: str,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("1cv8.exe", "1cv8c.exe", "dbgs.exe"):
        (bin_dir / name).touch()

    with pytest.raises(ValueError, match="connection_string") as error:
        RuntimeConfig(tmp_path, bin_dir, connection_string=connection)
    assert "private-secret" not in str(error.value)


def test_session_config_defaults_evidence_root_and_accepts_string_source_root(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("1cv8.exe", "1cv8c.exe", "dbgs.exe"):
        (bin_dir / name).touch()
    source = tmp_path / "project"
    source.mkdir()
    runtime = RuntimeConfig(str(tmp_path), str(bin_dir))

    config = RuntimeSessionConfig(runtime, source_root=str(source))

    assert config.evidence_root == tmp_path.resolve() / "artifacts"
    assert config.source_root == source.resolve()


def test_session_config_accepts_explicit_string_evidence_root(tmp_path: Path) -> None:
    config = RuntimeSessionConfig(None, str(tmp_path / "evidence"))

    assert config.evidence_root == (tmp_path / "evidence").resolve()
