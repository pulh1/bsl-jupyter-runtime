"""Opt-in live CAPTURE lifecycle acceptance on a disposable 1C infobase."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID
from xml.etree import ElementTree

import pytest

from integration.jupyter_bsl_fixture.extension import (
    CALLEE_CAPTURE_FRAGMENT,
    FIXTURE_EXTENSION_NAME,
)
from onec_runtime.capture_evaluation import CapturePhase
from onec_runtime.capture_inspection import DebugFrame
from onec_runtime.capture_values import UnavailableValueNode, ValueNode
from onec_runtime.errors import CaptureShapeUnsupportedError, MaterializationLimitError
from onec_runtime.kernel import COMMON_MODULE_PROPERTY_ID
from onec_runtime.rdbg.models import ModuleLocation
from onec_runtime.runtime_api import RuntimeReplyKind
from onec_runtime_jupyter.extension import OnecValueProxy
from test_worker_universe_1c import _fresh_live_harness


LIVE_FLAG = "ONEC_RUN_CAPTURE_LIFECYCLE_LIVE"
TABLE_NAME = "AcceptanceTable"
ARRAY_NAME = "AcceptanceArray"
FIXTURE_MODULE = "JupyterBslFixtureCalleeServer"
SCENARIOS = (("direct", 7), ("conditional", 11))
_FIXTURE_MODULE_ROOT = (
    Path(__file__).resolve().parents[1]
    / "fixtures" / "onec" / FIXTURE_EXTENSION_NAME
    / "CommonModules"
)


def _fixture_callee_source() -> str:
    return (
        _FIXTURE_MODULE_ROOT / FIXTURE_MODULE / "Ext" / "Module.bsl"
    ).read_text(encoding="utf-8-sig")


def _fixture_capture_location() -> ModuleLocation:
    lines = _fixture_callee_source().splitlines()
    executable = [
        number for number, line in enumerate(lines, start=1)
        if line.strip() == CALLEE_CAPTURE_FRAGMENT
    ]
    if len(executable) != 1:
        raise ValueError("fixture CAPTURE statement must be unique and executable")
    metadata = ElementTree.parse(_FIXTURE_MODULE_ROOT / f"{FIXTURE_MODULE}.xml")
    ns = "{http://v8.1c.ru/8.3/MDClasses}"
    module = metadata.getroot().find(f"{ns}CommonModule")
    if module is None or module.findtext(f"{ns}Properties/{ns}Name") != FIXTURE_MODULE:
        raise ValueError("fixture CAPTURE module metadata is invalid")
    return ModuleLocation(
        "ExtensionModule", "", UUID(module.attrib["uuid"]),
        UUID(COMMON_MODULE_PROPERTY_ID), executable[0], FIXTURE_EXTENSION_NAME,
    )


def _main_fixture_capture_source() -> str:
    return f"РезультатИнструкции = {FIXTURE_MODULE}.ВыполнитьШаг(10);"


def _failing_capture_source(kind: str) -> str:
    if kind == "direct":
        return 'ВызватьИсключение "deliberate capture failure";'
    if kind == "conditional":
        return '''Если Истина Тогда
    ВызватьИсключение "deliberate capture failure";
КонецЕсли;'''
    raise ValueError("unknown CAPTURE failure scenario")


def _corrected_capture_source(value: int) -> str:
    if type(value) is not int or value < 0:
        raise ValueError("acceptance value must be a non-negative integer")
    return f'''
{TABLE_NAME} = Новый ТаблицаЗначений;
{TABLE_NAME}.Колонки.Добавить("Значение", Новый ОписаниеТипов("Число"));
{TABLE_NAME}.Добавить().Значение = {value};
{ARRAY_NAME} = Новый Массив;
{ARRAY_NAME}.Добавить({value});
{ARRAY_NAME}.Добавить("corrected");
e1cRuntimeКонтекстОтладки.ЛокальныеЧисла = {ARRAY_NAME};
e1cRuntimeКонтекстОтладки.ЛокальнаяТаблица = {TABLE_NAME};
РезультатИнструкции = Истина;
'''


def _capture_identity(capture) -> tuple[int, int, int]:
    return (
        capture.operation_id, capture.capture_generation, capture.stop_sequence,
    )


@pytest.mark.integration
@pytest.mark.live_1c
@pytest.mark.timeout(600)
@pytest.mark.parametrize(("failure_kind", "value"), SCENARIOS)
def test_capture_error_retry_inspection_materialization_and_resume_live(
    tmp_path: Path, failure_kind: str, value: int,
) -> None:
    if os.environ.get(LIVE_FLAG) != "1":
        pytest.skip(f"set {LIVE_FLAG}=1 for the disposable CAPTURE lifecycle gate")
    if not os.environ.get("ONEC_PLATFORM_BIN"):
        pytest.skip("set ONEC_PLATFORM_BIN to the installed 1C platform bin directory")

    with _fresh_live_harness(
        tmp_path, live_flag=LIVE_FLAG, emit_evidence=False,
    ) as harness:
        location = _fixture_capture_location()
        harness.session.configure_capture_points((location,))
        captured = harness.session.execute_bsl(_main_fixture_capture_source())
        assert captured.kind is RuntimeReplyKind.CAPTURED
        assert captured.location == location
        capture = harness.session.current_capture()
        identity = _capture_identity(capture)

        failed = harness.session.execute_bsl(_failing_capture_source(failure_kind))
        assert failed.kind is RuntimeReplyKind.CAPTURE_CELL
        assert failed.succeeded is False
        assert capture.status().phase is CapturePhase.PAUSED
        assert _capture_identity(harness.session.current_capture()) == identity

        corrected = harness.session.execute_bsl(_corrected_capture_source(value))
        assert corrected.kind is RuntimeReplyKind.CAPTURE_CELL
        assert corrected.succeeded is True, corrected.error
        assert _capture_identity(capture) == identity
        assert capture.status().phase is CapturePhase.PAUSED
        assert _capture_identity(harness.session.current_capture()) == identity

        visible = capture.stack[:20]
        native = capture.stack.native[:20]
        assert native.total >= visible.total
        assert any(
            isinstance(frame, DebugFrame) and not frame.runtime_kernel
            for frame in visible.frames
        )
        frames = tuple(
            frame for frame in native.frames
            if isinstance(frame, DebugFrame) and not frame.runtime_kernel
        )
        assert frames
        assert frames[0].physical is not None
        assert frames[0].physical.object_id == location.object_id
        frame_variables = frames[0].variables[:20]
        assert frame_variables.items
        assert all(
            isinstance(item, UnavailableValueNode)
            for item in frame_variables.items
        )
        assert any(item.name == "ЛокальныйСчетчик" for item in frame_variables.items)
        selected_counter = frames[0].variables["ЛокальныйСчетчик"]
        assert isinstance(selected_counter, ValueNode)
        assert selected_counter.type_name == "Число"
        assert selected_counter.preview == "<captured value>"
        assert any(
            item.name == "ЛокальныйСчетчик"
            for item in frames[0].locals[:20].items
        )
        assert any(
            item.name == "НачальноеЗначение"
            for item in frames[0].parameters[:20].items
        )

        array_node = capture.context.variables["ЛокальныеЧисла"]
        table_node = capture.context.variables["ЛокальнаяТаблица"]
        assert array_node.name == "ЛокальныеЧисла"
        assert table_node.name == "ЛокальнаяТаблица"
        assert array_node.type_name == "Массив"
        assert table_node.type_name == "ТаблицаЗначений"
        assert array_node.preview == "<captured value>"
        assert table_node.preview == "<captured value>"
        with pytest.raises(CaptureShapeUnsupportedError):
            _ = array_node.items[0]
        with pytest.raises(CaptureShapeUnsupportedError):
            _ = table_node.rows[0]

        with pytest.raises(MaterializationLimitError, match="items limit 1"):
            harness.session.materialize(f"e1cRuntimeКонтекст.{ARRAY_NAME}", max_items=1)
        assert capture.status().phase is CapturePhase.PAUSED
        assert _capture_identity(harness.session.current_capture()) == identity

        materialized = harness.session.materialize(f"e1cRuntimeКонтекст.{ARRAY_NAME}")
        assert len(materialized) == 2
        assert materialized[0] == value
        assert materialized[1] == "corrected"
        direct_table = harness.session.to_df(f"e1cRuntimeКонтекст.{TABLE_NAME}")
        assert direct_table["Значение"].tolist() == [value]

        snapshot = harness.session.namespace_snapshot()
        assert TABLE_NAME.casefold() in {name.casefold() for name in snapshot.names}
        proxy = OnecValueProxy(
            harness.session, TABLE_NAME,
            runtime_generation=snapshot.runtime_generation,
            context_generation=snapshot.context_generation,
        )
        assert proxy.head(1).to_df()["Значение"].tolist() == [value]

        resumed = harness.session.resume_capture()
        assert resumed.kind is RuntimeReplyKind.MAIN_COMPLETED
        assert resumed.succeeded is True
        assert capture.status().phase is CapturePhase.STALE
        returned_table = harness.session.to_df(
            "e1cRuntimeКонтекст.РезультатИнструкции.Таблица"
        )
        assert returned_table["Значение"].tolist() == [value]
