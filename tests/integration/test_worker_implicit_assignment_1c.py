from __future__ import annotations

from base64 import b64encode
import ast
import os
from pathlib import Path

import pytest

from onec_runtime.config import RuntimeConfig
from onec_runtime.experiment import bsl_string_literal
from onec_runtime.session import RuntimeSession, RuntimeSessionConfig
from onec_runtime.worker_epf import build_worker_epf


pytestmark = [pytest.mark.integration, pytest.mark.live_1c]
_LIVE_FLAG = "ONEC_RUNTIME_RUN_LIVE_IMPLICIT_ASSIGNMENT"

_CONTROL = """Функция OnecПроверка() Экспорт
    Локальная = 1;
    Возврат Локальная;
КонецФункции
"""
_TARGET = """Функция OnecПроверка() Экспорт
    КадровыйУчет = 1;
    Возврат КадровыйУчет;
КонецФункции
"""


def _upload_and_call(session: RuntimeSession, payload: bytes, name: str) -> object:
    encoded = bsl_string_literal(b64encode(payload).decode("ascii"))
    source = f"""Адрес = ПоместитьВоВременноеХранилище(Base64Значение({encoded}));
Имя = ВнешниеОбработки.Подключить(Адрес, {bsl_string_literal(name)}, Ложь);
Объект = ВнешниеОбработки.Создать(Имя, Ложь);
Результат = Объект.OnecПроверка();"""
    return session.runtime_api._controller.execute_system_main(source)


def test_runtime_product_imports_no_com_packages() -> None:
    source_root = Path(__file__).resolve().parents[2] / "src" / "onec_runtime"
    forbidden: list[tuple[str, str]] = []
    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.name)
        for node in ast.walk(tree):
            names: tuple[str, ...] = ()
            if isinstance(node, ast.Import):
                names = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                names = (node.module,)
            for imported in names:
                if imported.split(".", 1)[0].casefold() in {"win32com", "comtypes"}:
                    forbidden.append((path.name, imported))
    assert forbidden == []


def test_live_implicit_local_does_not_mask_common_module_write(tmp_path: Path) -> None:
    if os.environ.get(_LIVE_FLAG) != "1":
        pytest.skip(f"set {_LIVE_FLAG}=1 for the bounded live gate")
    if os.environ.get("ONEC_RUNTIME_PASSWORD", ""):
        pytest.fail("the approved live gate requires an empty password")
    required = (
        "ONEC_RUNTIME_PLATFORM_BIN",
        "ONEC_RUNTIME_CONNECTION_STRING",
        "ONEC_RUNTIME_USERNAME",
    )
    if any(not os.environ.get(name) for name in required):
        pytest.skip("live 1C configuration is incomplete")

    workspace = Path(__file__).resolve().parents[2]
    runtime = RuntimeConfig(
        workspace=workspace,
        platform_bin=Path(os.environ["ONEC_RUNTIME_PLATFORM_BIN"]),
        connection_string=os.environ["ONEC_RUNTIME_CONNECTION_STRING"],
        username=os.environ["ONEC_RUNTIME_USERNAME"],
    )
    control = build_worker_epf(_CONTROL, tmp_path / "Control.epf").read_bytes()
    target = build_worker_epf(_TARGET, tmp_path / "Target.epf").read_bytes()
    session: RuntimeSession | None = None
    try:
        session = RuntimeSession.start(
            RuntimeSessionConfig(runtime, tmp_path / "evidence"),
        )
        preflight = session.runtime_api._controller.execute_system_main(
            "Результат = Строка(ТипЗнч(КадровыйУчет));"
        )
        assert preflight.succeeded is True
        assert "общий модуль" in str(preflight.result).casefold()

        control_reply = _upload_and_call(session, control, "OnecImplicitControl")
        assert control_reply.succeeded is True
        assert control_reply.result == 1

        target_reply = _upload_and_call(session, target, "OnecImplicitTarget")
        assert target_reply.succeeded is False
        diagnostic = " ".join(
            (
                str(target_reply.error),
                str(getattr(target_reply, "diagnostic", "")),
                *tuple(str(item) for item in target_reply.messages),
            )
        )
        assert "поле объекта недоступно для записи" in diagnostic.casefold()
    finally:
        if session is not None:
            session.close()
