from __future__ import annotations

import ast

import nbformat

from onec_runtime.bsl.notebook_cells import split_notebook_cell
from onec_runtime.bsl.parser_target import PythonParserTarget
from tools import build_demo_notebooks as builder


def test_demo_notebooks_are_standalone_and_capture_uses_notebook_flow(tmp_path, monkeypatch) -> None:
    demo_dir = tmp_path / "demo"
    monkeypatch.setattr(builder, "DEMO_DEST", demo_dir)

    builder.build()

    assert sorted(path.name for path in demo_dir.glob("*.ipynb")) == [
        "01-overview.ipynb", "03-capture.ipynb"
    ]
    overview = nbformat.read(demo_dir / "01-overview.ipynb", as_version=4)
    capture = nbformat.read(demo_dir / "03-capture.ipynb", as_version=4)
    for notebook in (overview, capture):
        nbformat.validate(notebook)
        assert notebook.metadata.kernelspec.name == "onec-demo"
        assert all(
            cell.cell_type != "code" or (cell.execution_count is None and not cell.outputs)
            for cell in notebook.cells
        )
        assert "ЗУП КОРП 3.1.38.92" in notebook.cells[0].source

    assert overview.cells[2].source == capture.cells[2].source
    sources = [cell.source for cell in capture.cells]
    joined = "\n".join(sources)
    assert "demo_support" not in joined
    assert "runtime.add_capture_point(" in joined
    assert "runtime.clear_capture_points()" in joined
    assert "runtime.runtime_api.capture_stack(" in joined
    assert "151350000" not in joined
    assert "166485000" not in joined
    assert any(source.startswith("%%bsl\nПланПовтор = КадровыйУчет.КадровыеДанныеСотрудников(") for source in sources)
    assert "runtime.execute_bsl('ПланПовтор" not in joined


def test_current_demo_code_cells_parse(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(builder, "DEMO_DEST", tmp_path / "demo")
    builder.build()
    parser = PythonParserTarget.from_generated()

    for path in (tmp_path / "demo").glob("*.ipynb"):
        notebook = nbformat.read(path, as_version=4)
        for cell in notebook.cells:
            if cell.cell_type != "code":
                continue
            if cell.source.startswith("%%bsl\n"):
                split_notebook_cell(parser, cell.source.removeprefix("%%bsl\n"))
            else:
                ast.parse(cell.source)
