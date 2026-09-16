from __future__ import annotations

import os
import shutil
from hashlib import sha256
from pathlib import Path

import pytest

import tools.build_runtime_extension_bundle as builder
from onec_runtime.errors import ExtensionBundleError
from onec_runtime.extension_bundle import read_extension_manifest
from onec_runtime.toolchain import ToolResult
from tests.unit.extension_bundle_support import (
    marker_line,
    write_dump_fixture,
    write_manifest_fixture,
)
from tools.build_runtime_extension_bundle import (
    build_extension_manifest,
    build_runtime_extension_bundle,
    write_extension_manifest,
)


def _repository_source(tmp_path: Path) -> Path:
    fixture = write_dump_fixture(tmp_path / "fixture")
    source = tmp_path / "repository" / "onec" / "OnecInteractiveRuntime"
    source.parent.mkdir(parents=True)
    shutil.copytree(fixture, source)
    return source


def _publication_inputs(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "new"
    root.mkdir()
    cfe = root / "OnecInteractiveRuntime.cfe"
    cfe.write_bytes(b"new-cfe")
    manifest = write_manifest_fixture(
        root,
        cfe_sha256=sha256(cfe.read_bytes()).hexdigest(),
        cfe_size=cfe.stat().st_size,
    )
    return cfe, manifest


@pytest.mark.parametrize("fail_after_replace", [False, True])  # type: ignore[untyped-decorator]
def test_publication_failure_restores_exact_previous_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail_after_replace: bool,
) -> None:
    cfe, manifest = _publication_inputs(tmp_path)
    output = tmp_path / "resources"
    output.mkdir()
    final_cfe = output / cfe.name
    final_manifest = output / manifest.name
    final_cfe.write_bytes(b"previous-cfe")
    final_manifest.write_bytes(b"previous-manifest")
    real_replace = os.replace
    injected = False

    def fail_second_replace(source: Path, destination: Path) -> None:
        nonlocal injected
        if Path(destination) == final_manifest and not injected:
            injected = True
            if fail_after_replace:
                real_replace(source, destination)
            raise OSError("injected manifest publication failure")
        real_replace(source, destination)

    monkeypatch.setattr(builder.os, "replace", fail_second_replace)

    with pytest.raises(OSError, match="injected manifest publication failure"):
        builder._publish_bundle(
            cfe,
            manifest,
            output,
            tmp_path / "validation-runtime",
        )

    assert final_cfe.read_bytes() == b"previous-cfe"
    assert final_manifest.read_bytes() == b"previous-manifest"


@pytest.mark.parametrize("fail_after_replace", [False, True])  # type: ignore[untyped-decorator]
def test_first_publication_failure_restores_prior_absence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail_after_replace: bool,
) -> None:
    cfe, manifest = _publication_inputs(tmp_path)
    output = tmp_path / "resources"
    final_cfe = output / cfe.name
    final_manifest = output / manifest.name
    real_replace = os.replace
    injected = False

    def fail_second_replace(source: Path, destination: Path) -> None:
        nonlocal injected
        if Path(destination) == final_manifest and not injected:
            injected = True
            if fail_after_replace:
                real_replace(source, destination)
            raise OSError("injected manifest publication failure")
        real_replace(source, destination)

    monkeypatch.setattr(builder.os, "replace", fail_second_replace)

    with pytest.raises(OSError, match="injected manifest publication failure"):
        builder._publish_bundle(
            cfe,
            manifest,
            output,
            tmp_path / "validation-runtime",
        )

    assert not final_cfe.exists()
    assert not final_manifest.exists()


def test_manifest_builder_binds_cfe_dump_bsl_and_breakpoint_lines(
    tmp_path: Path,
) -> None:
    cfe = tmp_path / "OnecInteractiveRuntime.cfe"
    cfe.write_bytes(b"cfe-fixture")
    dump = write_dump_fixture(tmp_path)

    manifest = build_extension_manifest(cfe, dump)

    assert manifest.cfe_sha256 == sha256(cfe.read_bytes()).hexdigest()
    assert manifest.cfe_size == len(b"cfe-fixture")
    assert manifest.artifact_version == "0.1.3"
    assert manifest.protocol_version == "2"
    assert (
        manifest.breakpoints.managed.object_id
        == manifest.fingerprints.identity.root_id
    )
    assert manifest.breakpoints.managed.line == marker_line(
        dump / "Ext" / "ManagedApplicationModule.bsl",
        "@runtime-extension-service-breakpoint",
    )
    assert manifest.breakpoints.server_entry.extension_name == (
        "OnecInteractiveRuntime"
    )


def test_manifest_writer_round_trips_through_strict_reader(tmp_path: Path) -> None:
    cfe = tmp_path / "OnecInteractiveRuntime.cfe"
    cfe.write_bytes(b"cfe-fixture")
    manifest = build_extension_manifest(cfe, write_dump_fixture(tmp_path))
    path = tmp_path / "extension-manifest.json"

    write_extension_manifest(manifest, path)

    assert read_extension_manifest(path) == manifest


def test_builder_runs_clean_commands_in_order_with_a_fresh_infobase_each_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _repository_source(tmp_path)
    output = tmp_path / "resources"
    designer = tmp_path / "platform" / "1cv8.exe"
    designer.parent.mkdir(parents=True)
    designer.touch()
    commands: list[tuple[str, ...]] = []

    def record(command: list[str], log_path: Path) -> ToolResult:
        frozen = tuple(command)
        commands.append(frozen)
        infobase = Path(
            command[command.index("/F") + 1]
            if "/F" in command
            else command[2].removeprefix("File=").removesuffix(";")
        )
        if command[1] == "CREATEINFOBASE":
            infobase.mkdir(parents=True)
            (infobase / "1Cv8.1CD").write_bytes(b"fresh")
        elif "/DumpCfg" in command:
            Path(command[command.index("/DumpCfg") + 1]).write_bytes(b"built-cfe")
        elif "/DumpConfigToFiles" in command:
            dump = Path(command[command.index("/DumpConfigToFiles") + 1])
            loaded_source = Path(
                next(
                    entry[entry.index("/LoadConfigFromFiles") + 1]
                    for entry in commands
                    if "/LoadConfigFromFiles" in entry
                )
            )
            shutil.copytree(loaded_source, dump)
        return ToolResult(frozen, 0, log_path)

    monkeypatch.setattr(builder, "run_tool_command", record)

    first = build_runtime_extension_bundle(
        source_root=source,
        output_root=output,
        platform_bin=designer.parent,
        artifact_version="0.1.3",
        protocol_version="2",
    )
    second = build_runtime_extension_bundle(
        source_root=source,
        output_root=output,
        platform_bin=designer.parent,
        artifact_version="0.1.3",
        protocol_version="2",
    )

    assert first.manifest == second.manifest
    assert len(commands) == 8
    for offset in (0, 4):
        batch = commands[offset : offset + 4]
        assert batch[0][1] == "CREATEINFOBASE"
        assert "/LoadConfigFromFiles" in batch[1]
        loaded_source = Path(batch[1][batch[1].index("/LoadConfigFromFiles") + 1])
        assert loaded_source != source
        assert (
            loaded_source.parent
            == Path(batch[0][2].removeprefix("File=").removesuffix(";")).parent
        )
        assert builder.fingerprint_extension_dump(loaded_source) == (
            builder.fingerprint_extension_dump(source)
        )
        assert "-updateConfigDumpInfo" in batch[1]
        assert batch[1][batch[1].index("-Format") + 1] == "Hierarchical"
        assert "/DumpCfg" in batch[2]
        assert "/DumpConfigToFiles" in batch[3]
        assert all(
            command[command.index("-Extension") + 1] == "OnecInteractiveRuntime"
            for command in batch[1:]
        )
    first_base = Path(commands[0][2].removeprefix("File=").removesuffix(";"))
    second_base = Path(commands[4][2].removeprefix("File=").removesuffix(";"))
    assert first_base != second_base
    assert first_base.parent != second_base.parent
    assert first_base.parent.parent == second_base.parent.parent
    assert first_base.parent.parent == (
        source.parents[1] / ".runtime" / "extension-builds"
    )


def test_builder_rejects_requested_version_mismatch_before_starting_1c(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _repository_source(tmp_path)
    output = tmp_path / "resources"
    designer = tmp_path / "platform" / "1cv8.exe"
    designer.parent.mkdir(parents=True)
    designer.touch()

    def unexpected_run(command: list[str], log_path: Path) -> ToolResult:
        raise AssertionError(f"1C was started: {command}; log={log_path}")

    monkeypatch.setattr(builder, "run_tool_command", unexpected_run)

    with pytest.raises(ExtensionBundleError, match="requested artifact version"):
        build_runtime_extension_bundle(
            source_root=source,
            output_root=output,
            platform_bin=designer.parent,
            artifact_version="9.9.9",
            protocol_version="2",
        )


def test_builder_keeps_canonical_repository_source_immutable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _repository_source(tmp_path)
    config_dump_info = source / "ConfigDumpInfo.xml"
    original = config_dump_info.read_bytes()
    output = tmp_path / "resources"
    designer = tmp_path / "platform" / "1cv8.exe"
    designer.parent.mkdir(parents=True)
    designer.touch()

    def simulate_designer(command: list[str], log_path: Path) -> ToolResult:
        if command[1] == "CREATEINFOBASE":
            infobase = Path(command[2].removeprefix("File=").removesuffix(";"))
            infobase.mkdir(parents=True)
            (infobase / "1Cv8.1CD").write_bytes(b"fresh")
        elif "/LoadConfigFromFiles" in command:
            loaded_source = Path(command[command.index("/LoadConfigFromFiles") + 1])
            info = loaded_source / "ConfigDumpInfo.xml"
            info.write_text(
                info.read_text(encoding="utf-8").replace(
                    "</ConfigDumpInfo>",
                    "<!-- Designer refreshed dump info --></ConfigDumpInfo>",
                ),
                encoding="utf-8",
            )
        elif "/DumpCfg" in command:
            Path(command[command.index("/DumpCfg") + 1]).write_bytes(b"built-cfe")
        elif "/DumpConfigToFiles" in command:
            dump = Path(command[command.index("/DumpConfigToFiles") + 1])
            loaded_source = Path(
                next(
                    entry[entry.index("/LoadConfigFromFiles") + 1]
                    for entry in commands
                    if "/LoadConfigFromFiles" in entry
                )
            )
            shutil.copytree(loaded_source, dump)
        commands.append(command)
        return ToolResult(tuple(command), 0, log_path)

    commands: list[list[str]] = []
    monkeypatch.setattr(builder, "run_tool_command", simulate_designer)

    build_runtime_extension_bundle(
        source_root=source,
        output_root=output,
        platform_bin=designer.parent,
        artifact_version="0.1.3",
        protocol_version="2",
    )

    assert config_dump_info.read_bytes() == original
