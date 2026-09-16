"""Opt-in live qualification for typed CAPTURE inspection."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

import pytest

from onec_runtime.capture_evaluation import CapturePhase
from onec_runtime.capture_inspection import DebugFrame
from onec_runtime.capture_values import ValueNode, ValueShape
from onec_runtime.runtime_api import RuntimeReplyKind
from test_worker_universe_1c import _enter_synthetic_capture, _fresh_live_harness


LIVE_FLAG = "ONEC_RUN_CAPTURE_INSPECTION_LIVE"


@dataclass(frozen=True, slots=True)
class LiveShapeExpectation:
    name: str
    type_name: str
    shape: ValueShape
    alias: str
    children: tuple[tuple[str | int, str, str], ...] = ()


ADVERTISED_SHAPES = (
    LiveShapeExpectation(
        "QualificationStructure",
        "Структура",
        ValueShape.STRUCTURE,
        "fields",
        (("Code", "Число", "7"), ("Name", "Строка", "row")),
    ),
    LiveShapeExpectation(
        "QualificationFixedStructure",
        "ФиксированнаяСтруктура",
        ValueShape.FIXED_STRUCTURE,
        "fields",
        (("Code", "Число", "7"), ("Name", "Строка", "row")),
    ),
    LiveShapeExpectation(
        "QualificationArray",
        "Массив",
        ValueShape.ARRAY,
        "items",
        ((0, "Число", "7"), (1, "Строка", "row")),
    ),
    LiveShapeExpectation(
        "QualificationFixedArray",
        "ФиксированныйМассив",
        ValueShape.FIXED_ARRAY,
        "items",
        ((0, "Число", "7"), (1, "Строка", "row")),
    ),
    LiveShapeExpectation(
        "QualificationTable",
        "ТаблицаЗначений",
        ValueShape.VALUE_TABLE,
        "rows",
    ),
    LiveShapeExpectation(
        "QualificationRow",
        "СтрокаТаблицыЗначений",
        ValueShape.VALUE_TABLE_ROW,
        "fields",
    ),
)


def _shape_capture_source() -> str:
    return """КонтекстОтладки.QualificationStructure = Новый Структура("Code,Name", 7, "row");
КонтекстОтладки.QualificationFixedStructure = Новый ФиксированнаяСтруктура(КонтекстОтладки.QualificationStructure);
КонтекстОтладки.QualificationArray = Новый Массив;
КонтекстОтладки.QualificationArray.Добавить(7);
КонтекстОтладки.QualificationArray.Добавить("row");
КонтекстОтладки.QualificationFixedArray = Новый ФиксированныйМассив(КонтекстОтладки.QualificationArray);
КонтекстОтладки.QualificationTable = Новый ТаблицаЗначений;
КонтекстОтладки.QualificationTable.Колонки.Добавить("Code");
КонтекстОтладки.QualificationTable.Колонки.Добавить("Name");
КонтекстОтладки.QualificationRow = КонтекстОтладки.QualificationTable.Добавить();
КонтекстОтладки.QualificationRow.Code = 7;
КонтекстОтладки.QualificationRow.Name = "row";
РезультатИнструкции = Истина;"""


def _require_live_opt_in() -> None:
    if os.environ.get(LIVE_FLAG) != "1":
        pytest.skip(f"set {LIVE_FLAG}=1 for the disposable typed CAPTURE gate")


@pytest.mark.integration
@pytest.mark.live_1c
@pytest.mark.timeout(600)
def test_typed_capture_inspection_live(tmp_path: Path) -> None:
    _require_live_opt_in()
    with _fresh_live_harness(
        tmp_path,
        live_flag=LIVE_FLAG,
        emit_evidence=False,
    ) as harness:
        _enter_synthetic_capture(harness)
        populated = harness.session.execute_bsl(_shape_capture_source())
        assert populated.kind is RuntimeReplyKind.CAPTURE_CELL
        assert populated.succeeded is True

        capture = harness.session.current_capture()
        assert capture.status().phase is CapturePhase.PAUSED

        context_page = capture.context.variables[:20]
        expected_names = {item.name for item in ADVERTISED_SHAPES}
        assert expected_names <= {item.name for item in context_page.items}
        assert len(context_page.items) <= 20
        assert context_page.total >= len(context_page.items)

        for expected in ADVERTISED_SHAPES:
            node = capture.context.variables[expected.name]
            assert node.name == expected.name
            assert node.type_name == expected.type_name
            assert node.shape is expected.shape
            assert node.expandable is True
            children = node.children[:2]
            aliased = getattr(node, expected.alias)[:2]
            if expected.children:
                assert children.total == len(expected.children)
                assert children.next_cursor is None
                assert aliased.total == len(expected.children)
                assert aliased.next_cursor is None
                assert all(isinstance(item, ValueNode) for item in children.items)
                assert all(isinstance(item, ValueNode) for item in aliased.items)
                assert tuple(
                    (item.name, item.type_name, item.preview)
                    for item in children.items
                ) == expected.children
                assert tuple(
                    (item.name, item.type_name, item.preview)
                    for item in aliased.items
                ) == expected.children
            else:
                assert tuple(item.name for item in children.items) == tuple(
                    item.name for item in aliased.items
                )
                assert len(children.items) <= 2

        table = capture.context.variables["QualificationTable"]
        columns = table.columns[:2]
        assert [item.name for item in columns.items] == ["Code", "Name"]
        row = table.rows[0]
        assert row.type_name == "СтрокаТаблицыЗначений"
        assert row.shape is ValueShape.VALUE_TABLE_ROW
        assert [item.name for item in row.fields[:2].items] == ["Code", "Name"]
        assert row.children["Code"].preview == "7"

        native_page = capture.stack.native[:8]
        assert native_page.native is True
        assert len(native_page.frames) <= 8
        native_frames = tuple(
            frame
            for frame in native_page.frames
            if isinstance(frame, DebugFrame) and not frame.runtime_kernel
        )
        assert native_frames
        native_variables = native_frames[0].variables[:20]
        assert native_variables.items
        assert len(native_variables.items) <= 20
        representatives = tuple(
            item
            for item in native_variables.items
            if isinstance(item, ValueNode) and item.type_name
        )
        assert representatives
        representative = representatives[0]
        assert isinstance(representative.name, str) and representative.name
        assert isinstance(representative.type_name, str) and representative.type_name

        completed = harness.session.resume_capture()
        assert completed.kind is RuntimeReplyKind.MAIN_COMPLETED
        assert completed.succeeded is True
