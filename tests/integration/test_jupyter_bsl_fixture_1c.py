from __future__ import annotations

import os
from pathlib import Path

import psutil
import pytest

from integration.jupyter_bsl_fixture.extension import (
    FIXTURE_EXTENSION_NAME,
    FixtureExtensionArtifact,
    build_fixture_extension,
    install_fixture_extension,
    install_minimal_host_configuration,
    install_product_extension,
)
from integration.support.configurator_agent import configure_extensions_unsafe
from onec_runtime.extension_bundle import EXTENSION_NAME
from integration.jupyter_bsl_fixture.live import (
    execute_fixture_notebook,
    verify_fixture_live_artifact,
)
from onec_runtime.config import RuntimeConfig
from onec_runtime.toolchain import create_empty_infobase


REPOSITORY = Path(__file__).resolve().parents[2]
FIXTURE_SOURCE = (
    REPOSITORY / "tests" / "fixtures" / "onec" / "JupyterBslTestFixture"
)
HOST_SOURCE = REPOSITORY / "tests" / "fixtures" / "onec" / "MinimalHostConfiguration"
EXPECTED_PLATFORM = Path(r"C:\Program Files\1cv8\8.3.27.2170\bin")
_OWNED_NAMES = {"dbgs.exe", "1cv8.exe", "1cv8c.exe"}


def _platform_bin() -> Path:
    if os.environ.get("ONEC_RUN_JUPYTER_BSL_INTEGRATION") != "1":
        pytest.skip(
            "set ONEC_RUN_JUPYTER_BSL_INTEGRATION=1 for live Jupyter + BSL smoke"
        )
    configured = Path(os.environ.get("ONEC_PLATFORM_BIN", EXPECTED_PLATFORM)).resolve()
    if configured.parent.name not in {"8.3.27.2170", "8.5.1.1529"}:
        pytest.fail("Jupyter + BSL smoke requires 1C 8.3.27.2170 or 8.5.1.1529")
    for executable in ("1cv8.exe", "1cv8c.exe", "dbgs.exe"):
        if not (configured / executable).is_file():
            pytest.fail(f"required platform executable is missing: {executable}")
    return configured


@pytest.fixture(scope="session")
def fixture_extension_artifact(
    tmp_path_factory: pytest.TempPathFactory,
) -> FixtureExtensionArtifact:
    return build_fixture_extension(
        _platform_bin(),
        FIXTURE_SOURCE,
        tmp_path_factory.mktemp("jupyter-bsl-extension"),
    )


def _matching_owned_processes(infobase: Path) -> list[dict[str, object]]:
    marker = str(infobase.resolve()).casefold()
    survivors: list[dict[str, object]] = []
    for process in psutil.process_iter(("pid", "name", "cmdline")):
        try:
            name = process.name().casefold()
            command = " ".join(process.cmdline()).casefold()
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
        if name in _OWNED_NAMES and marker in command:
            survivors.append({"pid": process.pid, "name": name})
    return survivors


@pytest.mark.live_1c
@pytest.mark.timeout(300)
def test_jupyter_bsl_fixture_smoke(
    tmp_path: Path,
    fixture_extension_artifact: FixtureExtensionArtifact,
) -> None:
    platform = _platform_bin()
    target_root = tmp_path / "target"
    runtime_config = RuntimeConfig(
        workspace=target_root,
        platform_bin=platform,
    )
    create_empty_infobase(runtime_config)
    install_minimal_host_configuration(
        runtime_config,
        HOST_SOURCE,
        tmp_path / "host-install-logs",
    )
    install_product_extension(
        runtime_config,
        tmp_path / "product-install-logs",
    )
    install_fixture_extension(
        runtime_config,
        fixture_extension_artifact,
        tmp_path / "fixture-install-logs",
    )
    configure_extensions_unsafe(
        runtime_config,
        (EXTENSION_NAME, FIXTURE_EXTENSION_NAME),
        tmp_path / "configurator-agent",
    )
    environment = {
        "ONEC_RUNTIME_WORKSPACE": str(REPOSITORY),
        "ONEC_RUNTIME_PLATFORM_BIN": str(platform),
        "ONEC_RUNTIME_CONNECTION_STRING": f'File="{runtime_config.infobase_dir}";',
        "ONEC_RUNTIME_EVIDENCE_DIR": str(tmp_path / "runtime-evidence"),
        "ONEC_RUNTIME_FIXTURE_SOURCE": str(fixture_extension_artifact.source_root),
        "ONEC_RUNTIME_PROCESS_SNAPSHOT": str(tmp_path / "owned-processes.json"),
    }

    run_dir = execute_fixture_notebook(
        REPOSITORY / "tests" / "fixtures" / "notebooks" / "jupyter-bsl-fixture-acceptance.ipynb",
        tmp_path / "executed.ipynb",
        environment=environment,
        ownership_markers=(str(runtime_config.infobase_dir.resolve()),),
    )

    summary = verify_fixture_live_artifact(run_dir)
    assert summary["status"] == "PASS"
    assert summary["capture_sequences"] == [1, 2]
    assert summary["writeback_value"] == 42
    assert summary["final_result"] == 43
    assert summary["owned_process_count"] == 0
    assert _matching_owned_processes(runtime_config.infobase_dir) == []
