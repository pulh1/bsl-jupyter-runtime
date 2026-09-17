"""Static guard for the opt-in CAPTURE lifecycle acceptance scenario."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest

from onec_runtime.bsl import LoweringMode, SemanticNotebookLowerer
from onec_runtime.bsl.parser_target import PythonParserTarget
from onec_runtime.kernel import COMMON_MODULE_PROPERTY_ID, SERVER_COMMON_MODULE_OBJECT_ID


_LIVE_TEST = (
    Path(__file__).parents[1]
    / "integration"
    / "test_capture_lifecycle_acceptance_1c.py"
)
_SPEC = importlib.util.spec_from_file_location("capture_lifecycle_live_test", _LIVE_TEST)
assert _SPEC is not None and _SPEC.loader is not None
live = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = live
sys.path.insert(0, str(_LIVE_TEST.parent))
try:
    _SPEC.loader.exec_module(live)
finally:
    sys.path.pop(0)


@pytest.mark.parametrize(("failure_kind", "value"), live.SCENARIOS)
def test_live_capture_sources_lower_and_parse(failure_kind: str, value: int) -> None:
    parser = PythonParserTarget.from_generated()
    lowerer = SemanticNotebookLowerer(parser)
    for source in (
        live._failing_capture_source(failure_kind),
        live._corrected_capture_source(value),
    ):
        lowered = lowerer.lower(source, mode=LoweringMode.CAPTURE)
        parser.parse(lowered.source, "БлокНоутбука")


def test_live_capture_uses_executable_fixture_frame_outside_kernel() -> None:
    location = live._fixture_capture_location()
    assert location.module_type == "ExtensionModule"
    assert location.url == ""
    assert location.extension_name == live.FIXTURE_EXTENSION_NAME
    assert str(location.property_id) == COMMON_MODULE_PROPERTY_ID
    assert str(location.object_id) != SERVER_COMMON_MODULE_OBJECT_ID
    source = live._fixture_callee_source().splitlines()
    assert source[location.line - 1].strip() == live.CALLEE_CAPTURE_FRAGMENT
    assert live.FIXTURE_MODULE in live._main_fixture_capture_source()
    parser = PythonParserTarget.from_generated()
    lowered = SemanticNotebookLowerer(parser).lower(
        live._main_fixture_capture_source(), mode=LoweringMode.MAIN,
    )
    parser.parse(lowered.source, "БлокНоутбука")


def test_live_capture_gate_skips_before_harness_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    marks = {
        mark.name
        for mark in live.test_capture_error_retry_inspection_materialization_and_resume_live.pytestmark
    }
    assert {"integration", "live_1c", "parametrize"} <= marks
    monkeypatch.delenv(live.LIVE_FLAG, raising=False)
    monkeypatch.setattr(
        live,
        "_fresh_live_harness",
        lambda *_args, **_kwargs: pytest.fail("live harness started before opt-in"),
    )
    with pytest.raises(pytest.skip.Exception, match=live.LIVE_FLAG):
        live.test_capture_error_retry_inspection_materialization_and_resume_live(
            tmp_path, "direct", 7,
        )


def test_live_capture_requires_explicit_platform_path_before_harness(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv(live.LIVE_FLAG, "1")
    monkeypatch.delenv("ONEC_PLATFORM_BIN", raising=False)
    monkeypatch.setattr(
        live,
        "_fresh_live_harness",
        lambda *_args, **_kwargs: pytest.fail("live harness started without platform path"),
    )
    with pytest.raises(pytest.skip.Exception, match="ONEC_PLATFORM_BIN"):
        live.test_capture_error_retry_inspection_materialization_and_resume_live(
            tmp_path, "direct", 7,
        )
