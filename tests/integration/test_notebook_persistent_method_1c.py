"""Live gate for notebook method reads of variables from preceding cells."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from integration.jupyter_bsl_fixture.extension import (
    install_minimal_host_configuration,
    install_product_extension,
)
from integration.support.configurator_agent import configure_extensions_unsafe
from onec_runtime.config import RuntimeConfig
from onec_runtime.extension_bundle import EXTENSION_NAME
from onec_runtime.session import RuntimeSession, RuntimeSessionConfig
from onec_runtime.toolchain import create_empty_infobase


@pytest.mark.integration
@pytest.mark.live_1c
@pytest.mark.timeout(300)
def test_method_reads_latest_notebook_variable(tmp_path: Path) -> None:
    if os.environ.get("ONEC_RUN_NOTEBOOK_METHOD_GLOBAL_LIVE") != "1":
        pytest.skip("set ONEC_RUN_NOTEBOOK_METHOD_GLOBAL_LIVE=1 for this live gate")
    platform = Path(
        os.environ.get("ONEC_PLATFORM_BIN", r"C:\Program Files\1cv8\8.3.27.2170\bin")
    )
    if not (platform / "1cv8.exe").is_file():
        pytest.skip("1C platform is unavailable")
    repository = Path(__file__).resolve().parents[2]
    target_root = tmp_path / "target"
    runtime_config = RuntimeConfig(
        workspace=target_root,
        platform_bin=platform,
    )
    create_empty_infobase(runtime_config)
    install_minimal_host_configuration(
        runtime_config,
        repository / "tests" / "fixtures" / "onec" / "MinimalHostConfiguration",
        tmp_path / "host-install-logs",
    )
    install_product_extension(runtime_config, tmp_path / "product-install-logs")
    configure_extensions_unsafe(
        runtime_config, (EXTENSION_NAME,), tmp_path / "configurator-agent"
    )

    session: RuntimeSession | None = None
    try:
        session = RuntimeSession.start(
            RuntimeSessionConfig(runtime_config, tmp_path / "runtime-evidence")
        )
        first = session.execute_bsl("А = 100;")
        assert first.succeeded, first
        method = session.execute_bsl(
            "Функция ПолучитьА()\n    Возврат А;\nКонецФункции"
        )
        assert method.succeeded, method
        called = session.execute_bsl("Сообщить(ПолучитьА());")
        assert called.succeeded, (called.error, called.diagnostic, called.messages)
        assert called.messages == ("100",)

        updated = session.execute_bsl("А = 200;")
        assert updated.succeeded, updated
        called_again = session.execute_bsl("Сообщить(ПолучитьА());")
        assert called_again.succeeded, (
            called_again.error, called_again.diagnostic, called_again.messages
        )
        assert called_again.messages == ("200",)
    finally:
        if session is not None:
            session.close()
