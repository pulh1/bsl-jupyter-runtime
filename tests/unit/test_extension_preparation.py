from __future__ import annotations

import json
from pathlib import Path

import pytest

from onec_runtime.errors import ExtensionIdentityConflict, ExtensionLifecycleError, ExtensionNotInstalled
from onec_runtime.extension_lifecycle import LifecycleMode
from test_extension_lifecycle import (
    FakeExtensionTools,
    _current_dump,
    _foreign_dump,
    _older_dump,
    make_lifecycle,
)


class SafetyTools(FakeExtensionTools):
    safe_mode = True
    preparation_error: BaseException | None = None

    def disable_safe_mode(self, log_dir: Path) -> None:
        self._call("disable-safe-mode", log_dir)
        if self.preparation_error is not None:
            raise self.preparation_error
        self.safe_mode = False


@pytest.mark.parametrize("installed", ["absent", "current", "older"])
def test_auto_requires_live_safe_mode_check_for_current_extension(
    tmp_path: Path, installed: str,
) -> None:
    dumps = {
        "absent": [ExtensionNotInstalled("absent"), _current_dump(tmp_path / "after")],
        "current": [_current_dump(tmp_path / "current")],
        "older": [_older_dump(tmp_path / "old"), _current_dump(tmp_path / "updated")],
    }
    tools = SafetyTools(dumps=dumps[installed])
    fixture = make_lifecycle(tmp_path, tools=tools, marker_matches=False)

    decision = fixture.lifecycle.prepare()

    if installed == "current":
        assert decision.mode is LifecycleMode.PROBED
        assert tools.safe_mode is True
        assert "disable-safe-mode" not in tools.calls
    else:
        assert decision.mode is LifecycleMode.SLOW
        assert tools.safe_mode is False
        assert tools.calls[-1] == "disable-safe-mode"
    assert fixture.state_store.read() is None  # Still needs a live handshake.


def test_preparation_failure_cannot_leave_old_fast_marker(tmp_path: Path) -> None:
    tools = SafetyTools(dumps=[_current_dump(tmp_path / "current")])
    tools.preparation_error = ExtensionLifecycleError("safe mode remains enabled")
    fixture = make_lifecycle(tmp_path, tools=tools, marker_matches=True)

    with pytest.raises(ExtensionLifecycleError, match="safe mode remains enabled"):
        fixture.lifecycle.prepare(force_slow=True)

    assert fixture.state_store.read() is None
    assert tools.safe_mode is True


def test_legacy_marker_does_not_skip_safe_mode_preparation(tmp_path: Path) -> None:
    tools = SafetyTools(dumps=[_current_dump(tmp_path / "current")])
    fixture = make_lifecycle(tmp_path, tools=tools, marker_matches=True)
    payload = json.loads(fixture.state_store.path.read_text(encoding="utf-8"))
    payload.pop("safe_mode", None)
    fixture.state_store.path.write_text(json.dumps(payload), encoding="utf-8")

    decision = fixture.lifecycle.prepare()

    assert decision.mode is LifecycleMode.PROBED
    assert tools.safe_mode is True
    assert fixture.state_store.read() is None


def test_foreign_extension_never_has_safe_mode_changed(tmp_path: Path) -> None:
    tools = SafetyTools(dumps=[_foreign_dump(tmp_path / "foreign")])
    fixture = make_lifecycle(tmp_path, tools=tools, marker_matches=False)

    with pytest.raises(ExtensionIdentityConflict):
        fixture.lifecycle.prepare()

    assert tools.safe_mode is True
    assert "disable-safe-mode" not in tools.calls


def test_manual_never_changes_extension_safety(tmp_path: Path) -> None:
    tools = SafetyTools(fail_on_any_call=True)
    fixture = make_lifecycle(tmp_path, tools=tools, marker_matches=True)

    fixture.lifecycle.prepare_manual()

    assert tools.safe_mode is True
    assert tools.calls == []


@pytest.mark.parametrize("safe_mode", [True, None, 0, "false"])
def test_marker_requires_explicit_disabled_safe_mode(tmp_path: Path, safe_mode: object) -> None:
    fixture = make_lifecycle(tmp_path, tools=SafetyTools(), marker_matches=True)
    payload = json.loads(fixture.state_store.path.read_text(encoding="utf-8"))
    payload["safe_mode"] = safe_mode
    fixture.state_store.path.write_text(json.dumps(payload), encoding="utf-8")

    assert fixture.state_store.read() is None
