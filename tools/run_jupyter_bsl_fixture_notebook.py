from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import sys


WORKSPACE = Path(__file__).resolve().parents[1]
for entry in (
    WORKSPACE,
    WORKSPACE / "src",
    WORKSPACE / "packages" / "jupyter" / "src",
):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from integration.jupyter_bsl_fixture.extension import (  # noqa: E402
    FIXTURE_EXTENSION_NAME,
    build_fixture_extension,
    install_fixture_extension,
    install_minimal_host_configuration,
    install_product_extension,
)
from integration.support.configurator_agent import (  # noqa: E402
    configure_extensions_unsafe,
)
from onec_runtime.extension_bundle import EXTENSION_NAME  # noqa: E402
from integration.jupyter_bsl_fixture.live import (  # noqa: E402
    execute_fixture_notebook,
    verify_fixture_live_artifact,
)
from onec_runtime.config import RuntimeConfig  # noqa: E402
from onec_runtime.toolchain import create_empty_infobase  # noqa: E402


EXPECTED_PLATFORM = Path(r"C:\Program Files\1cv8\8.3.27.2170\bin")


def _platform_bin() -> Path:
    if os.environ.get("ONEC_RUN_JUPYTER_BSL_INTEGRATION") != "1":
        raise SystemExit(
            "set ONEC_RUN_JUPYTER_BSL_INTEGRATION=1 for live Jupyter + BSL smoke"
        )
    configured = Path(os.environ.get("ONEC_PLATFORM_BIN", EXPECTED_PLATFORM)).resolve()
    if configured.parent.name not in {"8.3.27.2170", "8.5.1.1529"}:
        raise SystemExit("Jupyter + BSL smoke requires 1C 8.3.27.2170 or 8.5.1.1529")
    for executable in ("1cv8.exe", "1cv8c.exe", "dbgs.exe"):
        if not (configured / executable).is_file():
            raise SystemExit(f"required platform executable is missing: {executable}")
    return configured


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the opt-in Jupyter + BSL fixture smoke."
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        required=True,
        help="Directory under which the persistent run and evidence are written.",
    )
    args = parser.parse_args()
    platform = _platform_bin()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    run_root = args.output_root.resolve() / f"jupyter-bsl-{timestamp}-{os.getpid()}"
    run_root.mkdir(parents=True, exist_ok=False)

    fixture_source = (
        WORKSPACE / "tests" / "fixtures" / "onec" / "JupyterBslTestFixture"
    )
    artifact = build_fixture_extension(
        platform,
        fixture_source,
        run_root / "fixture-build",
    )
    runtime_config = RuntimeConfig(
        workspace=run_root / "target",
        platform_bin=platform,
    )
    create_empty_infobase(runtime_config)
    install_minimal_host_configuration(
        runtime_config,
        WORKSPACE / "tests" / "fixtures" / "onec" / "MinimalHostConfiguration",
        run_root / "host-install-logs",
    )
    install_product_extension(
        runtime_config,
        run_root / "product-install-logs",
    )
    install_fixture_extension(
        runtime_config,
        artifact,
        run_root / "fixture-install-logs",
    )
    configure_extensions_unsafe(
        runtime_config,
        (EXTENSION_NAME, FIXTURE_EXTENSION_NAME),
        run_root / "configurator-agent",
    )
    environment = {
        "ONEC_RUNTIME_WORKSPACE": str(WORKSPACE),
        "ONEC_RUNTIME_PLATFORM_BIN": str(platform),
        "ONEC_RUNTIME_CONNECTION_STRING": f'File="{runtime_config.infobase_dir}";',
        "ONEC_RUNTIME_EVIDENCE_DIR": str(run_root / "runtime-evidence"),
        "ONEC_RUNTIME_FIXTURE_SOURCE": str(artifact.source_root),
        "ONEC_RUNTIME_PROCESS_SNAPSHOT": str(run_root / "owned-processes.json"),
    }
    evidence = execute_fixture_notebook(
        WORKSPACE / "tests" / "fixtures" / "notebooks" / "jupyter-bsl-fixture-acceptance.ipynb",
        run_root / "executed.ipynb",
        environment=environment,
        ownership_markers=(str(runtime_config.infobase_dir.resolve()),),
    )
    summary = verify_fixture_live_artifact(evidence)
    print(evidence)
    print(
        "PASS "
        f"capture={summary['capture_sequences']} "
        f"writeback={summary['writeback_value']} "
        f"final={summary['final_result']} "
        f"elapsed_s={summary['elapsed_s']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
