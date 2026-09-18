from __future__ import annotations

from pathlib import Path

import nbformat
import pytest

from integration.jupyter_bsl_fixture.extension import (
    CALLEE_CAPTURE_FRAGMENT,
    CALLER_CAPTURE_FRAGMENT,
)
from integration.jupyter_bsl_fixture.notebook import (
    EXPECTED_ERROR_TAGS,
    REQUIRED_TAGS,
    build_fixture_notebook,
    serialize_fixture_notebook,
    verify_fixture_notebook,
)
from onec_runtime.bsl.notebook_cells import split_notebook_cell
from onec_runtime.bsl.parser_target import PythonParserTarget
from onec_runtime.errors import ProtocolError


def _code_cells(notebook: nbformat.NotebookNode) -> list[nbformat.NotebookNode]:
    return [cell for cell in notebook.cells if cell.cell_type == "code"]


def _by_tag(notebook: nbformat.NotebookNode) -> dict[str, nbformat.NotebookNode]:
    return {
        cell.metadata["tags"][0]: cell
        for cell in _code_cells(notebook)
    }


def test_builder_is_deterministic_clean_and_ordered() -> None:
    first = build_fixture_notebook()
    second = build_fixture_notebook()

    assert serialize_fixture_notebook(first) == serialize_fixture_notebook(second)
    assert [cell.metadata["tags"][0] for cell in _code_cells(first)] == list(
        REQUIRED_TAGS
    )
    assert all(cell.execution_count is None for cell in _code_cells(first))
    assert all(cell.outputs == [] for cell in _code_cells(first))
    identifiers = [cell.id for cell in first.cells]
    assert all(identifiers)
    assert len(identifiers) == len(set(identifiers))


def test_checked_in_fixture_is_in_test_fixtures_and_matches_builder() -> None:
    repository = Path(__file__).resolve().parents[2]
    path = repository / "tests" / "fixtures" / "notebooks" / "jupyter-bsl-fixture-acceptance.ipynb"

    assert path.read_text(encoding="utf-8") == serialize_fixture_notebook(build_fixture_notebook())
    verify_fixture_notebook(path, allow_outputs=False)


def test_notebook_covers_procedure_statement_and_mixed_cells_in_both_modes() -> None:
    notebook = build_fixture_notebook()
    cells = _by_tag(notebook)

    main_method = split_notebook_cell(
        PythonParserTarget.from_generated(), cells["main-method"].source.removeprefix("%%bsl\n")
    )
    capture_method = split_notebook_cell(
        PythonParserTarget.from_generated(), cells["capture-method"].source.removeprefix("%%bsl\n")
    )
    main_mixed = split_notebook_cell(
        PythonParserTarget.from_generated(), cells["main-mixed"].source.removeprefix("%%bsl\n")
    )
    capture_mixed = split_notebook_cell(
        PythonParserTarget.from_generated(), cells["capture-mixed"].source.removeprefix("%%bsl\n")
    )
    capture_mixed_error = split_notebook_cell(
        PythonParserTarget.from_generated(), cells["capture-mixed-error"].source.removeprefix("%%bsl\n")
    )

    assert main_method.worker_source
    assert not main_method.statement_source
    assert capture_method.worker_source
    assert not capture_method.statement_source
    assert main_mixed.worker_source
    assert main_mixed.statement_source
    assert capture_mixed.worker_source
    assert capture_mixed.statement_source
    assert capture_mixed_error.worker_source
    assert capture_mixed_error.statement_source
    assert cells["main-method-call"].metadata["runtime_mode"] == "main"
    assert cells["capture-method-call"].metadata["runtime_mode"] == "capture"
    assert cells["capture-mixed-error"].metadata["expected_error"] is True


def test_notebook_contract_marks_mixed_capture_as_supported() -> None:
    metadata = build_fixture_notebook().metadata["onec_runtime_fixture"]

    assert metadata == {"version": 2}


def test_proxy_cells_cover_main_and_capture_materialize_and_to_df() -> None:
    cells = _by_tag(build_fixture_notebook())

    for tag in ("main-proxy", "capture-proxy"):
        source = cells[tag].source
        assert ".materialize(" in source
        assert ".head(2).to_df(" in source
        assert "max_depth=8" in source
        assert "max_items=32" in source
        assert "max_bytes=65536" in source
        assert "chunk_size=128" in source
        assert "datetime(2026, 8, 27, 12, 30, 0)" in source


def test_capture_flow_has_two_source_points_error_writeback_and_two_resumes() -> None:
    cells = _by_tag(build_fixture_notebook())

    assert "CALLEE_CAPTURE_FRAGMENT" in cells["capture-arm"].source
    assert "CALLER_CAPTURE_FRAGMENT" in cells["capture-arm"].source
    assert "e1cRuntimeКонтекстОтладки.ЛокальныйСчетчик = 900" in cells["capture-error"].source
    assert (
        "FixtureMixedCapture(CaptureMixedState, e1cRuntimeКонтекстОтладки.ЛокальныйСчетчик)"
        in cells["capture-mixed"].source
    )
    assert (
        "e1cRuntimeКонтекстОтладки.ЛокальныйMixed = CaptureMixedState.Результат"
        in cells["capture-mixed"].source
    )
    assert "e1cRuntimeКонтекстОтладки.ЛокальныйСчетчик = 901" in cells["capture-mixed-error"].source
    assert "ОшибкаMixedCapture = 1 / 0" in cells["capture-mixed-error"].source
    assert "FixtureMixedCaptureAfterError" in cells["capture-mixed-error-recovery"].source
    assert "e1cRuntimeКонтекстОтладки.ЛокальныйСчетчик = 40" in cells["capture-write"].source
    assert "ЛокальныйСчетчик ЛокальнаяСтруктура" in cells["capture-resume-a"].source
    assert cells["capture-resume-b"].source == "%bsl_resume"
    assert EXPECTED_ERROR_TAGS == frozenset(
        {"main-error", "capture-mixed-error", "capture-error"}
    )


def test_setup_embeds_capture_fragments_without_repo_only_runtime_import() -> None:
    setup = _by_tag(build_fixture_notebook())["setup"].source

    assert "from integration" not in setup
    assert f"CALLEE_CAPTURE_FRAGMENT = {CALLEE_CAPTURE_FRAGMENT!r}" in setup
    assert f"CALLER_CAPTURE_FRAGMENT = {CALLER_CAPTURE_FRAGMENT!r}" in setup
    assert '"jupyter_fixture"' in setup
    assert '"jupyter-fixture"' not in setup


def test_every_bsl_cell_is_accepted_by_generated_product_parser() -> None:
    parser = PythonParserTarget.from_generated()

    for cell in _code_cells(build_fixture_notebook()):
        if not cell.source.startswith("%%bsl\n"):
            continue
        split_notebook_cell(parser, cell.source.removeprefix("%%bsl\n"))


def test_validator_accepts_capture_mixed_and_rejects_outputs_and_duplicate_tags(
    tmp_path: Path,
) -> None:
    notebook = build_fixture_notebook()
    path = tmp_path / "fixture.ipynb"
    nbformat.write(notebook, path)
    verify_fixture_notebook(path, allow_outputs=False)

    _by_tag(notebook)["parameters"].metadata["tags"] = ["setup"]
    nbformat.write(notebook, path)
    with pytest.raises(ProtocolError, match="requires one"):
        verify_fixture_notebook(path, allow_outputs=False)

    notebook = build_fixture_notebook()
    _by_tag(notebook)["capture-mixed"].outputs = [
        nbformat.v4.new_output("display_data", data={"text/plain": "unexpected"})
    ]
    nbformat.write(notebook, path)
    with pytest.raises(ProtocolError, match="contains outputs"):
        verify_fixture_notebook(path, allow_outputs=False)


def test_serialized_notebook_has_no_machine_path_or_temporary_capture_error() -> None:
    serialized = serialize_fixture_notebook(build_fixture_notebook())

    assert "C:\\" not in serialized
    assert "Mixed notebook cells require MAIN_READY" not in serialized
