import ast
import importlib.util
import inspect
from pathlib import Path
import sys

import pytest

from onec_runtime.capture_values import ValueShape
from onec_runtime.bsl import LoweringMode, SemanticNotebookLowerer
from onec_runtime.bsl.parser_target import PythonParserTarget


_LIVE_TEST = Path(__file__).parents[1] / "integration" / "test_capture_inspection_1c.py"
_SPEC = importlib.util.spec_from_file_location("capture_inspection_live_test", _LIVE_TEST)
assert _SPEC is not None and _SPEC.loader is not None
live = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = live
sys.path.insert(0, str(_LIVE_TEST.parent))
try:
    _SPEC.loader.exec_module(live)
finally:
    sys.path.pop(0)


def _called_names(function) -> set[str]:  # type: ignore[no-untyped-def]
    tree = ast.parse(inspect.getsource(function))
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            names.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
    return names


def test_live_capture_contract_enumerates_every_advertised_v1_shape() -> None:
    assert {
        (item.name, item.type_name, item.shape, item.alias)
        for item in live.ADVERTISED_SHAPES
    } == {
        ("QualificationStructure", "Структура", ValueShape.STRUCTURE, "fields"),
        (
            "QualificationFixedStructure",
            "ФиксированнаяСтруктура",
            ValueShape.FIXED_STRUCTURE,
            "fields",
        ),
        ("QualificationArray", "Массив", ValueShape.ARRAY, "items"),
        (
            "QualificationFixedArray",
            "ФиксированныйМассив",
            ValueShape.FIXED_ARRAY,
            "items",
        ),
        (
            "QualificationTable",
            "ТаблицаЗначений",
            ValueShape.VALUE_TABLE,
            "rows",
        ),
        (
            "QualificationRow",
            "СтрокаТаблицыЗначений",
            ValueShape.VALUE_TABLE_ROW,
            "fields",
        ),
    }


def test_live_shape_setup_is_valid_capture_code_for_every_named_root() -> None:
    parser = PythonParserTarget.from_generated()
    lowered = SemanticNotebookLowerer(parser).lower(
        live._shape_capture_source(),
        mode=LoweringMode.CAPTURE,
    )

    parser.parse(lowered.source, "БлокНоутбука")
    assert {item.name for item in live.ADVERTISED_SHAPES} <= set(
        lowered.dirty_roots
    )


def test_live_capture_gate_is_marked_and_skips_before_harness_by_default(
    monkeypatch,
    tmp_path,
) -> None:
    assert live.LIVE_FLAG == "ONEC_RUN_CAPTURE_INSPECTION_LIVE"
    marks = {mark.name for mark in live.test_typed_capture_inspection_live.pytestmark}
    assert {"integration", "live_1c"} <= marks
    monkeypatch.delenv(live.LIVE_FLAG, raising=False)
    monkeypatch.setattr(
        live,
        "_fresh_live_harness",
        lambda *_args, **_kwargs: pytest.fail("live harness started before opt-in"),
    )

    with pytest.raises(pytest.skip.Exception, match=live.LIVE_FLAG):
        live.test_typed_capture_inspection_live(tmp_path)


def test_live_capture_uses_typed_surface_and_disposable_infobase_harness() -> None:
    assert {
        "current_capture",
        "_fresh_live_harness",
        "_enter_synthetic_capture",
    } <= _called_names(live.test_typed_capture_inspection_live)
    assert {
        "create_empty_infobase",
        "rmtree",
        "_matching_owned_processes",
    } <= _called_names(live._fresh_live_harness)
    assert "session.close" in inspect.getsource(live._fresh_live_harness)
