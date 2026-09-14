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

    assert sorted(path.relative_to(demo_dir).as_posix() for path in demo_dir.rglob("*.ipynb")) == [
        "UT/05-ut-sales.ipynb", "ZUP/01-overview.ipynb", "ZUP/03-capture.ipynb"
    ]
    overview = nbformat.read(demo_dir / "ZUP" / "01-overview.ipynb", as_version=4)
    capture = nbformat.read(demo_dir / "ZUP" / "03-capture.ipynb", as_version=4)
    ut_sales = nbformat.read(demo_dir / "UT" / "05-ut-sales.ipynb", as_version=4)
    for notebook in (overview, capture, ut_sales):
        nbformat.validate(notebook)
        assert notebook.metadata.kernelspec.name == "onec-demo"
        assert all(
            cell.cell_type != "code" or (cell.execution_count is None and not cell.outputs)
            for cell in notebook.cells
        )
    for notebook in (overview, capture):
        assert "ЗУП КОРП 3.1.38.92" in notebook.cells[0].source

    assert "УТ 11.6.1.61" in ut_sales.cells[0].source
    assert "дата данных ИБ не фиксируется" in ut_sales.cells[0].source
    ut_sources = "\n".join(cell.source for cell in ut_sales.cells)
    assert "runtime.add_capture_point(" in ut_sources
    assert "runtime.clear_capture_points()" in ut_sources
    assert "runtime.runtime_api.capture_stack(" in ut_sources
    assert "runtime.load_worker_module(" in ut_sources
    assert "CommonModules\\ПродажиСервер\\Ext\\Module.bsl" in ut_sources

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

    for path in (tmp_path / "demo").rglob("*.ipynb"):
        notebook = nbformat.read(path, as_version=4)
        for cell in notebook.cells:
            if cell.cell_type != "code":
                continue
            if cell.source.startswith("%%bsl\n"):
                split_notebook_cell(parser, cell.source.removeprefix("%%bsl\n"))
            else:
                ast.parse(cell.source)
