from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from onec_runtime.bsl.module_catalog import SessionCommonModuleCatalog
from onec_runtime.errors import ProtocolError
from tests.unit.test_capture_source_configuration import (
    MODULE_UUID,
    RecordingCaptureApi,
    bare_capture_session,
    write_common_module_source,
)


def _session(source_root: Path):
    api = RecordingCaptureApi()
    session = bare_capture_session(api)
    session._common_module_catalog = SessionCommonModuleCatalog(
        source_root, profile="runtime-session-server-v1"
    )
    session._file_capture_points = ()
    return session, api


def test_add_capture_point_resolves_relative_and_absolute_module_paths(
    tmp_path: Path,
) -> None:
    root = write_common_module_source(tmp_path / "sources")
    module = root / "CommonModules" / "Продажи" / "Module.bsl"
    session, api = _session(root)

    first = session.add_capture_point("CommonModules/Продажи/Module.bsl", 2)
    second = session.add_capture_point(str(module), 3)

    assert first.object_id == second.object_id == UUID(MODULE_UUID)
    assert (first.line, second.line) == (2, 3)
    assert api.calls == [(first,), (first, second)]
    session.clear_capture_points()
    assert api.calls[-1] == ()


def test_add_capture_point_accepts_edt_path_relative_to_project_root(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    write_common_module_source(project / "src")
    session, api = _session(project)

    point = session.add_capture_point(
        "src/CommonModules/Продажи/Module.bsl", 2
    )

    assert point.object_id == UUID(MODULE_UUID)
    assert api.calls == [(point,)]


def test_add_capture_point_resolves_designer_export(tmp_path: Path) -> None:
    root = tmp_path / "designer"
    common_modules = root / "CommonModules"
    module = common_modules / "Продажи" / "Ext" / "Module.bsl"
    module.parent.mkdir(parents=True)
    (common_modules / "Продажи.xml").write_text(
        f'<MetaDataObject><CommonModule uuid="{MODULE_UUID}"/></MetaDataObject>',
        encoding="utf-8",
    )
    module.write_text(
        "Функция Сумма() Экспорт\n"
        "    Возврат 42;\n"
        "КонецФункции\n",
        encoding="utf-8",
    )
    session, api = _session(root)

    point = session.add_capture_point(
        r"CommonModules\Продажи\Ext\Module.bsl", 2
    )

    assert point.object_id == UUID(MODULE_UUID)
    assert point.line == 2
    assert api.calls == [(point,)]


def test_add_capture_point_rejects_invalid_paths_and_lines_before_dispatch(
    tmp_path: Path,
) -> None:
    root = write_common_module_source(tmp_path / "sources")
    session, api = _session(root)

    for path, line in (
        (Path("CommonModules/Продажи/Module.bsl"), 2),
        ("CommonModules/Продажи/Module.bsl", 0),
        ("CommonModules/Продажи/Module.bsl", True),
        ("CommonModules/Продажи/Module.bsl", 1),
        ("CommonModules/Продажи/Module.bsl", 5),
    ):
        with pytest.raises((ValueError, ProtocolError)):
            session.add_capture_point(path, line)
    with pytest.raises(ProtocolError):
        session.add_capture_point("CommonModules/Неизвестный/Module.bsl", 2)
    assert api.calls == []


def test_add_capture_point_preserves_armed_points_when_dispatch_fails(
    tmp_path: Path,
) -> None:
    root = write_common_module_source(tmp_path / "sources")
    session, api = _session(root)
    first = session.add_capture_point("CommonModules/Продажи/Module.bsl", 2)
    api.failure = ProtocolError("debugger unavailable")

    with pytest.raises(ProtocolError, match="debugger unavailable"):
        session.add_capture_point("CommonModules/Продажи/Module.bsl", 3)

    assert session._file_capture_points == (first,)
    assert api.calls == [(first,)]
