from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import nbformat
from nbformat import NotebookNode

from integration.jupyter_bsl_fixture.cells import (
    CAPTURE_ERROR_RECOVERY_SOURCE,
    CAPTURE_ERROR_SOURCE,
    CAPTURE_MAIN_SOURCE,
    CAPTURE_METHOD_CALL_SOURCE,
    CAPTURE_METHOD_SOURCE,
    CAPTURE_MIXED_ERROR_RECOVERY_SOURCE,
    CAPTURE_MIXED_ERROR_SOURCE,
    CAPTURE_MIXED_SOURCE,
    CAPTURE_SNAPSHOT_SOURCE,
    CAPTURE_STACK_SOURCE,
    CAPTURE_WRITE_SOURCE,
    MAIN_ERROR_SOURCE,
    MAIN_METHOD_CALL_SOURCE,
    MAIN_METHOD_SOURCE,
    MAIN_MIXED_SOURCE,
    MAIN_RECOVERY_SOURCE,
    MAIN_STATEMENT_SOURCE,
)
from integration.jupyter_bsl_fixture.extension import (
    CALLEE_CAPTURE_FRAGMENT,
    CALLER_CAPTURE_FRAGMENT,
)
from onec_runtime.bsl.notebook_cells import split_notebook_cell
from onec_runtime.bsl.parser_target import PythonParserTarget
from onec_runtime.errors import ProtocolError


REQUIRED_TAGS = (
    "parameters",
    "setup",
    "status-main",
    "main-statement",
    "main-proxy",
    "main-method",
    "main-proxy-repeat",
    "main-method-call",
    "main-mixed",
    "main-error",
    "main-recovery",
    "capture-arm",
    "capture-main",
    "capture-snapshot",
    "capture-proxy",
    "capture-method",
    "capture-proxy-repeat",
    "capture-method-call",
    "capture-mixed",
    "capture-mixed-error",
    "capture-mixed-error-recovery",
    "capture-error",
    "capture-error-recovery",
    "capture-write",
    "capture-resume-a",
    "capture-stack",
    "capture-resume-b",
    "final",
    "cleanup",
)
EXPECTED_ERROR_TAGS = frozenset(
    {"main-error", "capture-mixed-error", "capture-error"}
)


def _cell_id(tag: str) -> str:
    return sha256(f"jupyter-bsl-fixture:{tag}".encode("utf-8")).hexdigest()[:16]


def _code(
    tag: str,
    source: str,
    *,
    runtime_mode: str,
    expected_error: bool = False,
) -> NotebookNode:
    metadata: dict[str, object] = {
        "tags": [tag],
        "runtime_mode": runtime_mode,
    }
    if expected_error:
        metadata["expected_error"] = True
        metadata["tags"] = [tag, "raises-exception"]
    cell = nbformat.v4.new_code_cell(
        source=source,
        execution_count=None,
        outputs=[],
        metadata=metadata,
    )
    cell["id"] = _cell_id(tag)
    return cell


def _markdown(tag: str, source: str) -> NotebookNode:
    cell = nbformat.v4.new_markdown_cell(source=source, metadata={"tags": [tag]})
    cell["id"] = _cell_id(tag)
    return cell


_SETUP_SOURCE = f'''from datetime import datetime
from decimal import Decimal
import json
import os
from pathlib import Path

from IPython.display import JSON
from onec_runtime.capture_source import CapturePointRequest
from onec_runtime.config import RuntimeConfig
from onec_runtime.session import RuntimeSessionConfig
from onec_runtime.value_materialization import ONEC_UNDEFINED
from onec_runtime_jupyter import InteractiveRuntimeSession
from onec_runtime_jupyter.extension import NotebookDisplayConfig

CALLEE_CAPTURE_FRAGMENT = {CALLEE_CAPTURE_FRAGMENT!r}
CALLER_CAPTURE_FRAGMENT = {CALLER_CAPTURE_FRAGMENT!r}

workspace = Path(os.environ["ONEC_RUNTIME_WORKSPACE"])
runtime_config = RuntimeConfig(
    workspace=workspace,
    platform_bin=Path(os.environ["ONEC_RUNTIME_PLATFORM_BIN"]),
    connection_string=os.environ["ONEC_RUNTIME_CONNECTION_STRING"],
    username="",
)
fixture = InteractiveRuntimeSession.start(
    RuntimeSessionConfig(
        runtime_config,
        Path(os.environ["ONEC_RUNTIME_EVIDENCE_DIR"]),
        CHUNK_SIZE,
    ),
    shell=get_ipython(),
    display=NotebookDisplayConfig.diagnostic(),
)
fixture.configure_capture_source(
    "jupyter_fixture",
    Path(os.environ["ONEC_RUNTIME_FIXTURE_SOURCE"]),
)
Path(os.environ["ONEC_RUNTIME_PROCESS_SNAPSHOT"]).write_text(
    json.dumps(fixture.owned_process_snapshot(), ensure_ascii=False),
    encoding="utf-8",
)
python_sentinel = "alive"'''


_MAIN_PROXY_SOURCE = '''main_scalar = Счетчик.materialize(
    max_depth=8, max_items=32, max_bytes=65536
)
main_nested_first = Вложенные.materialize(
    max_depth=8, max_items=32, max_bytes=65536
)
main_nested_second = Вложенные.materialize(
    max_depth=8, max_items=32, max_bytes=65536
)
main_rows = ТаблицаДанных.head(2).to_df(chunk_size=128)
assert main_scalar == Decimal("5")
assert type(main_nested_first["Флаг"]) is bool and main_nested_first["Флаг"] is True
assert main_nested_first["Момент"] == datetime(2026, 8, 27, 12, 30, 0)
assert main_nested_first["Пусто"] is ONEC_UNDEFINED
assert main_nested_first["Числа"] == [Decimal("3"), Decimal("5")]
assert main_nested_second == main_nested_first
assert list(main_rows.columns) == ["Код", "Сумма"]
assert main_rows["Код"].tolist() == ["A", "B"]
assert [int(value) for value in main_rows["Сумма"]] == [10, 20]
main_table_proxy = ТаблицаДанных
JSON({"status": "PASS", "nested_numbers": [3, 5], "rows": 2})'''


_MAIN_PROXY_REPEAT_SOURCE = '''main_rows_repeat = main_table_proxy.head(2).to_df(chunk_size=128)
assert main_rows_repeat["Код"].tolist() == ["A", "B"]
JSON({"status": "PASS", "same_generation": True, "rows": 2})'''


_CAPTURE_PROXY_SOURCE = '''capture_scalar = CaptureCounter.materialize(
    max_depth=8, max_items=32, max_bytes=65536
)
capture_nested_first = CaptureNested.materialize(
    max_depth=8, max_items=32, max_bytes=65536
)
capture_nested_second = CaptureNested.materialize(
    max_depth=8, max_items=32, max_bytes=65536
)
capture_rows = CaptureTable.head(2).to_df(chunk_size=128)
assert capture_scalar == Decimal("10")
assert capture_nested_first["Флаг"] is True
assert capture_nested_first["Момент"] == datetime(2026, 8, 27, 12, 30, 0)
assert capture_nested_first["Пусто"] is ONEC_UNDEFINED
assert capture_nested_first["Числа"] == [Decimal("3"), Decimal("5")]
assert capture_nested_second == capture_nested_first
assert list(capture_rows.columns) == ["Код", "Сумма"]
assert capture_rows["Код"].tolist() == ["A", "B"]
assert [int(value) for value in capture_rows["Сумма"]] == [10, 20]
capture_table_proxy = CaptureTable
JSON({"status": "PASS", "counter": 10, "rows": 2})'''


_CAPTURE_PROXY_REPEAT_SOURCE = '''capture_rows_repeat = capture_table_proxy.head(2).to_df(chunk_size=128)
assert capture_rows_repeat["Код"].tolist() == ["A", "B"]
JSON({"status": "PASS", "same_generation": True, "rows": 2})'''


def build_fixture_notebook() -> NotebookNode:
    cells = [
        _markdown(
            "title",
            "# Jupyter + BSL fixture acceptance\n\n"
            "Технический live-smoke MAIN/CAPTURE, включая mixed CAPTURE cells.",
        ),
        _code("parameters", "CHUNK_SIZE = 128", runtime_mode="python"),
        _code("setup", _SETUP_SOURCE, runtime_mode="python"),
        _code("status-main", "%bsl_status", runtime_mode="main"),
        _code("main-statement", f"%%bsl\n{MAIN_STATEMENT_SOURCE}", runtime_mode="main"),
        _code("main-proxy", _MAIN_PROXY_SOURCE, runtime_mode="python"),
        _code("main-method", f"%%bsl\n{MAIN_METHOD_SOURCE}", runtime_mode="main"),
        _code("main-proxy-repeat", _MAIN_PROXY_REPEAT_SOURCE, runtime_mode="python"),
        _code("main-method-call", f"%%bsl\n{MAIN_METHOD_CALL_SOURCE}", runtime_mode="main"),
        _code("main-mixed", f"%%bsl\n{MAIN_MIXED_SOURCE}", runtime_mode="main"),
        _code(
            "main-error",
            f"%%bsl\n{MAIN_ERROR_SOURCE}",
            runtime_mode="main",
            expected_error=True,
        ),
        _code("main-recovery", f"%%bsl\n{MAIN_RECOVERY_SOURCE}", runtime_mode="main"),
        _code(
            "capture-arm",
            '''capture_points = fixture.resolve_capture_points((
    CapturePointRequest("callee", source_fragment=CALLEE_CAPTURE_FRAGMENT),
    CapturePointRequest("caller", source_fragment=CALLER_CAPTURE_FRAGMENT),
))
capture_bindings = fixture.verify_capture_points(capture_points)
fixture.configure_capture_points(
    tuple(binding.location for binding in capture_bindings)
)
capture_points''',
            runtime_mode="main",
        ),
        _code("capture-main", f"%%bsl\n{CAPTURE_MAIN_SOURCE}", runtime_mode="main"),
        _code("capture-snapshot", f"%%bsl\n{CAPTURE_SNAPSHOT_SOURCE}", runtime_mode="capture"),
        _code("capture-proxy", _CAPTURE_PROXY_SOURCE, runtime_mode="python"),
        _code("capture-method", f"%%bsl\n{CAPTURE_METHOD_SOURCE}", runtime_mode="capture"),
        _code("capture-proxy-repeat", _CAPTURE_PROXY_REPEAT_SOURCE, runtime_mode="python"),
        _code("capture-method-call", f"%%bsl\n{CAPTURE_METHOD_CALL_SOURCE}", runtime_mode="capture"),
        _code("capture-mixed", f"%%bsl\n{CAPTURE_MIXED_SOURCE}", runtime_mode="capture"),
        _code(
            "capture-mixed-error",
            f"%%bsl\n{CAPTURE_MIXED_ERROR_SOURCE}",
            runtime_mode="capture",
            expected_error=True,
        ),
        _code(
            "capture-mixed-error-recovery",
            f"%%bsl\n{CAPTURE_MIXED_ERROR_RECOVERY_SOURCE}",
            runtime_mode="capture",
        ),
        _code(
            "capture-error",
            f"%%bsl\n{CAPTURE_ERROR_SOURCE}",
            runtime_mode="capture",
            expected_error=True,
        ),
        _code(
            "capture-error-recovery",
            f"%%bsl\n{CAPTURE_ERROR_RECOVERY_SOURCE}",
            runtime_mode="capture",
        ),
        _code("capture-write", f"%%bsl\n{CAPTURE_WRITE_SOURCE}", runtime_mode="capture"),
        _code(
            "capture-resume-a",
            'get_ipython().run_line_magic(\n'
            '    "bsl_resume", "ЛокальныйСчетчик ЛокальнаяСтруктура"\n'
            ')',
            runtime_mode="capture",
        ),
        _code("capture-stack", f"%%bsl\n{CAPTURE_STACK_SOURCE}", runtime_mode="capture"),
        _code("capture-resume-b", "%bsl_resume", runtime_mode="capture"),
        _code(
            "final",
            '''final_status = fixture.status()
assert final_status.state.value == "completed"
assert python_sentinel == "alive"
assert int(main_scalar) == 5
assert int(capture_scalar) == 10
JSON({
    "status": "PASS",
    "main_proxy_rows": len(main_rows),
    "capture_proxy_rows": len(capture_rows),
    "python_sentinel": python_sentinel,
})''',
            runtime_mode="python",
        ),
        _code("cleanup", "fixture.close()\nfixture.close()", runtime_mode="python"),
    ]
    return nbformat.v4.new_notebook(
        cells=cells,
        metadata={
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            },
            "language_info": {"name": "python", "version": "3.12"},
            "onec_runtime_fixture": {
                "version": 2,
            },
        },
    )


def serialize_fixture_notebook(notebook: NotebookNode) -> str:
    return nbformat.writes(notebook, version=4) + "\n"


def verify_fixture_notebook(path: Path, *, allow_outputs: bool) -> None:
    try:
        notebook = nbformat.read(path, as_version=4)
        nbformat.validate(notebook)
    except Exception as error:
        raise ProtocolError("Jupyter BSL fixture notebook is invalid") from error
    code_cells = [cell for cell in notebook.cells if cell.cell_type == "code"]
    tags = [cell.metadata.get("tags", [""])[0] for cell in code_cells]
    for required in REQUIRED_TAGS:
        if tags.count(required) != 1:
            raise ProtocolError(f"fixture notebook requires one {required} cell")
    if tags != list(REQUIRED_TAGS):
        raise ProtocolError("fixture notebook cells are out of order")
    identifiers = [cell.get("id", "") for cell in notebook.cells]
    if not all(identifiers) or len(set(identifiers)) != len(identifiers):
        raise ProtocolError("fixture notebook cell ids are invalid")
    serialized = serialize_fixture_notebook(notebook)
    if "C:\\" in serialized:
        raise ProtocolError("fixture notebook contains a machine-specific path")
    if "Mixed notebook cells require MAIN_READY" in serialized:
        raise ProtocolError("fixture notebook embeds the temporary CAPTURE error")
    parser = PythonParserTarget.from_generated()
    for cell in code_cells:
        tag = cell.metadata["tags"][0]
        if cell.metadata.get("runtime_mode") not in {"main", "capture", "python"}:
            raise ProtocolError(f"fixture notebook {tag} runtime mode is invalid")
        if not allow_outputs and cell.get("outputs"):
            raise ProtocolError(f"fixture notebook {tag} contains outputs")
        if not allow_outputs and cell.get("execution_count") is not None:
            raise ProtocolError(f"fixture notebook {tag} contains execution count")
        if bool(cell.metadata.get("expected_error")) != (tag in EXPECTED_ERROR_TAGS):
            raise ProtocolError(f"fixture notebook {tag} expected-error metadata is invalid")
        if ("raises-exception" in cell.metadata["tags"]) != (tag in EXPECTED_ERROR_TAGS):
            raise ProtocolError(f"fixture notebook {tag} expected-error tag is invalid")
        if not cell.source.startswith("%%bsl\n"):
            continue
        split_notebook_cell(parser, cell.source.removeprefix("%%bsl\n"))
    expected_code_cells = [
        cell for cell in build_fixture_notebook().cells if cell.cell_type == "code"
    ]
    if [cell.source for cell in code_cells] != [
        cell.source for cell in expected_code_cells
    ]:
        raise ProtocolError("fixture notebook cell source differs from contract")
