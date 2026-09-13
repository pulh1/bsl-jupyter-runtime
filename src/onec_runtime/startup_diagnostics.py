"""Bounded, allowlisted explanations for native 1C startup logs."""

from __future__ import annotations

from pathlib import Path


def startup_log_hint(log_path: Path) -> str | None:
    """Classify known 1C failures without copying private log text into errors."""
    try:
        with log_path.open("rb") as stream:
            payload = stream.read(64 * 1024)
    except OSError:
        return None
    if payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = payload.decode("utf-16", errors="replace")
    else:
        text = payload.decode("utf-8-sig", errors="replace")
        if "\ufffd" in text:
            text = payload.decode("cp1251", errors="replace")
    lowered = text.casefold()
    if (
        "ошибка программного лицензирования" in lowered
        and "не предусматривает возможность запуска сервера 1с:предприятия" in lowered
    ):
        return (
            "используемая лицензия 1С не разрешает запуск сервера "
            "1С:Предприятия; проверьте серверную лицензию"
        )
    if (
        "error locking infobase for configuration" in lowered
        or "ошибка блокировки информационной базы" in lowered
    ):
        return "база заблокирована; закройте Конфигуратор для этой базы и повторите запуск"
    if (
        "the infobase user is not authenticated" in lowered
        or "пользователь иб не идентифицирован" in lowered
    ):
        return "пользователь ИБ не прошёл аутентификацию; проверьте username и password"
    if (
        "the infobase is not found" in lowered
        or "информационная база не найдена" in lowered
    ):
        return "информационная база не найдена; проверьте путь File или адрес и имя Srvr/Ref"
    return None
