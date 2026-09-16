from pathlib import Path
import os
import sys

from jupyter_client import KernelManager
import nbformat
from nbclient import NotebookClient
from nbclient.exceptions import CellExecutionError
import pytest

from integration.jupyter_bsl_fixture.notebook import build_fixture_notebook
from onec_runtime_jupyter.extension import MACHINE_MIME_TYPE


@pytest.mark.parametrize("capture", [False, True])
@pytest.mark.parametrize("expected_error", [False, True])
def test_failed_bsl_cell_stops_nbclient_unless_explicitly_allowed(
    tmp_path: Path, capture: bool, expected_error: bool
) -> None:
    """Exercise real kernel transport; only the external 1C backend is replaced."""
    setup = '''from onec_runtime_jupyter import install_runtime, load_ipython_extension
from onec_runtime.runtime_api import RuntimeNamespaceSnapshot, RuntimeReply, RuntimeReplyKind, RuntimeStatus
from onec_runtime.prototype_runtime import OperationState
class FailedRuntime:
    def execute_bsl(self, source, *, source_unit):
        return RuntimeReply(KIND, 1, STATE, succeeded=False,
                            error="RAW token=private-connection pid=9182")
    def resume_capture(self, **kwargs):
        raise AssertionError("unused")
    def status(self):
        return RuntimeStatus(STATE, 1, 1, None)
    def namespace_snapshot(self):
        return RuntimeNamespaceSnapshot(1, 1, ())
    def validate_value_reference(self, handle):
        raise AssertionError("unused")
KIND = RuntimeReplyKind.CAPTURE_CELL if CAPTURE else RuntimeReplyKind.MAIN_COMPLETED
STATE = OperationState.CAPTURED if CAPTURE else OperationState.FAILED
runtime = FailedRuntime()
install_runtime(get_ipython(), runtime)
load_ipython_extension(get_ipython())
get_ipython().run_line_magic("xmode", "Verbose")
'''.replace("CAPTURE else", f"{capture!r} else")
    sentinel = tmp_path / "sentinel.txt"
    error_tag = "capture-error" if capture else "main-error"
    fixture_error = next(
        cell for cell in build_fixture_notebook().cells
        if error_tag in cell.metadata.get("tags", [])
    )
    notebook = nbformat.v4.new_notebook(cells=[
        nbformat.v4.new_code_cell(setup),
        nbformat.v4.new_code_cell(
            "%%bsl\nОшибка();",
            metadata=fixture_error.metadata if expected_error else {},
        ),
        nbformat.v4.new_code_cell(
            f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('ran')"
        ),
    ])
    workspace = Path(__file__).resolve().parents[2]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(str(workspace / path) for path in (
        "src", "packages/jupyter/src", "packages/mcp/src",
    ))
    manager = KernelManager(kernel_name="python3")
    manager.kernel_spec.argv = [
        sys.executable, "-m", "ipykernel_launcher", "-f", "{connection_file}",
    ]
    client = NotebookClient(notebook, km=manager, timeout=30, allow_errors=False)
    try:
        if expected_error:
            client.execute(env=environment)
        else:
            with pytest.raises(CellExecutionError, match="BslCellError"):
                client.execute(env=environment)
    finally:
        if manager.has_kernel:
            manager.shutdown_kernel(now=True)
    assert sentinel.exists() is expected_error
    assert (notebook.cells[-1].execution_count is not None) is expected_error
    outputs = notebook.cells[1].outputs
    assert [output.output_type for output in outputs] == ["display_data", "error"]
    assert outputs[0].data[MACHINE_MIME_TYPE]["succeeded"] is False
    assert outputs[1].ename == "BslCellError"
    serialized = str(outputs)
    for secret in ("RAW token", "private-connection", "9182"):
        assert secret not in serialized


def execute(client, code: str) -> tuple[dict[str, object], list[dict[str, object]]]:  # type: ignore[no-untyped-def]
    messages: list[dict[str, object]] = []
    reply = client.execute_interactive(
        code,
        timeout=20,
        output_hook=messages.append,
    )
    return reply, messages


def test_real_ipykernel_runs_python_and_bsl_cells_in_one_namespace() -> None:
    workspace = Path(__file__).resolve().parents[2]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(str(workspace / path) for path in (
        "src", "packages/jupyter/src", "packages/mcp/src",
    ))
    environment["JUPYTER_PLATFORM_DIRS"] = "1"
    environment["PYDEVD_DISABLE_FILE_VALIDATION"] = "1"
    manager = KernelManager()
    manager.kernel_spec.argv = [
        sys.executable, "-m", "ipykernel_launcher", "-f", "{connection_file}",
    ]
    manager.start_kernel(env=environment)
    client = manager.blocking_client()
    client.start_channels()
    try:
        client.wait_for_ready(timeout=20)
        setup = r'''
from onec_runtime_jupyter.extension import MACHINE_MIME_TYPE, install_runtime
from onec_runtime.prototype_runtime import OperationState
from onec_runtime.runtime_api import RuntimeNamespaceSnapshot, RuntimeReply, RuntimeReplyKind, RuntimeStatus

class StubRuntime:
    def __init__(self):
        self.sources = []
    def execute_bsl(self, source, *, source_unit):
        assert source_unit.source_sha256
        self.sources.append(source)
        return RuntimeReply(RuntimeReplyKind.MAIN_COMPLETED, 1, OperationState.COMPLETED, 42)
    def resume_capture(self, *, dirty_roots=()):
        return RuntimeReply(RuntimeReplyKind.MAIN_COMPLETED, 1, OperationState.COMPLETED)
    def status(self):
        return RuntimeStatus(OperationState.COMPLETED, 1, 1, None)
    def namespace_snapshot(self):
        return RuntimeNamespaceSnapshot(1, 1, ("ГДФЛ",))
    def validate_value_reference(self, handle):
        pass

python_marker = {"alive": True}
runtime = StubRuntime()
install_runtime(get_ipython(), runtime)
get_ipython().run_line_magic("load_ext", "onec_runtime_jupyter.extension")
'''
        setup_reply, _ = execute(client, setup)
        assert setup_reply["content"]["status"] == "ok"  # type: ignore[index]

        bsl_reply, bsl_messages = execute(
            client,
            "%%bsl\nГДФЛ = Расчет.Ндфл.Посчитать();",
        )
        assert bsl_reply["content"]["status"] == "ok"  # type: ignore[index]
        result = next(
            message["content"]["data"]  # type: ignore[index]
            for message in bsl_messages
            if message["msg_type"] == "execute_result"
        )
        assert result[MACHINE_MIME_TYPE]["kind"] == "main_completed"  # type: ignore[index]
        assert result[MACHINE_MIME_TYPE]["result"] == 42  # type: ignore[index]

        python_reply, python_messages = execute(
            client,
            "(python_marker['alive'], runtime.sources[-1], type(ГДФЛ).__name__, type(bsl.ГДФЛ).__name__)",
        )
        assert python_reply["content"]["status"] == "ok"  # type: ignore[index]
        python_result = next(
            message["content"]["data"]["text/plain"]  # type: ignore[index]
            for message in python_messages
            if message["msg_type"] == "execute_result"
        )
        assert "True" in python_result
        assert "ГДФЛ" in python_result
        assert "OnecValueProxy" in python_result
    finally:
        client.stop_channels()
        manager.shutdown_kernel(now=True)
