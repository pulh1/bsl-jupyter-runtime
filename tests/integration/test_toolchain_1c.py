from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from onec_runtime.config import RuntimeConfig
from onec_runtime.toolchain import build_kernel, create_build_infobase, create_empty_infobase


pytestmark = pytest.mark.integration


def test_creates_empty_file_infobase_and_builds_kernel(tmp_path: Path) -> None:
    platform_bin = os.environ.get("ONEC_PLATFORM_BIN")
    if not platform_bin:
        pytest.skip("ONEC_PLATFORM_BIN is not set")

    workspace = tmp_path / "workspace"
    shutil.copytree(
        Path(__file__).parents[2] / "onec" / "Kernel", workspace / "onec" / "Kernel"
    )
    config = RuntimeConfig(workspace=workspace, platform_bin=Path(platform_bin))

    create_empty_infobase(config)
    create_build_infobase(config)
    build_kernel(config)

    assert (config.infobase_dir / "1Cv8.1CD").stat().st_size > 0
    assert (config.build_infobase_dir / "1Cv8.1CD").stat().st_size > 0
    assert config.kernel_epf.stat().st_size > 0
