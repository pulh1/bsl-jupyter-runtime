"""Live regression for compact transfer of enumeration-valued table cells."""

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
def test_to_df_serializes_filled_and_empty_enumeration_values(tmp_path: Path) -> None:
    """A real enumeration must reach pandas as its presentation or empty string."""
    if os.environ.get("ONEC_RUN_COMPACT_ENUM_1C") != "1":
        pytest.skip("set ONEC_RUN_COMPACT_ENUM_1C=1 for live 1C qualification")
    platform = Path(
        os.environ.get("ONEC_PLATFORM_BIN", r"C:\Program Files\1cv8\8.5.1.1529\bin")
    )
    if not (platform / "1cv8c.exe").is_file():
        pytest.skip("1C platform is unavailable")

    config = RuntimeConfig(workspace=tmp_path / "target", platform_bin=platform)
    create_empty_infobase(config)
    host = Path(__file__).resolve().parents[1] / "fixtures" / "onec" / "CompactEnumHost"
    install_minimal_host_configuration(config, host, tmp_path / "host-install-logs")
    install_product_extension(config, tmp_path / "product-install-logs")
    configure_extensions_unsafe(
        config, (EXTENSION_NAME,), tmp_path / "configurator-agent"
    )

    session = RuntimeSession.start(RuntimeSessionConfig(config, tmp_path / "evidence"))
    try:
        reply = session.execute_bsl(
            """
ТаблицаПеречисления = Новый ТаблицаЗначений;
ТаблицаПеречисления.Колонки.Добавить(
    "Вид", Новый ОписаниеТипов("ПеречислениеСсылка.RuntimeProbeEnum"));
Заполненная = ТаблицаПеречисления.Добавить();
Заполненная.Вид = Перечисления.RuntimeProbeEnum.Filled;
Пустая = ТаблицаПеречисления.Добавить();
Пустая.Вид = Перечисления.RuntimeProbeEnum.ПустаяСсылка();
"""
        )
        assert reply.succeeded, (reply.error, reply.diagnostic, reply.messages)

        frame = session.to_df("e1cRuntimeКонтекст.ТаблицаПеречисления")
        assert frame["Вид"].tolist() == ["Filled", ""]
    finally:
        session.close()
