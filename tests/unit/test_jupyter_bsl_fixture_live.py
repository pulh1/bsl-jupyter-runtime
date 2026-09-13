from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import nbformat
import pytest

from integration.jupyter_bsl_fixture.live import (
    execute_fixture_notebook,
    verify_fixture_execution,
    verify_fixture_live_artifact,
)
from integration.jupyter_bsl_fixture.cells import CAPTURE_MIXED_ERROR_SOURCE
from integration.jupyter_bsl_fixture.notebook import (
    EXPECTED_ERROR_TAGS,
    REQUIRED_TAGS,
    build_fixture_notebook,
)
from integration.notebook_live import NotebookExecutionResult
from onec_runtime.errors import ProtocolError
from onec_runtime.bsl import source_sha256
from onec_runtime_jupyter.extension import MACHINE_MIME_TYPE


def _cells(notebook: nbformat.NotebookNode) -> dict[str, nbformat.NotebookNode]:
    return {
        cell.metadata["tags"][0]: cell
        for cell in notebook.cells
        if cell.cell_type == "code"
    }


def _runtime_payload(
    *,
    kind: str,
    operation_id: int,
    state: str,
    result: object = None,
    succeeded: bool = True,
    stop_sequence: int | None = None,
    messages: list[str] | None = None,
    diagnostic: dict[str, object] | None = None,
    location: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "kind": kind,
        "operation_id": operation_id,
        "state": state,
        "result": result,
        "succeeded": succeeded,
        "stop_sequence": stop_sequence,
        "messages": list(messages or []),
        "diagnostic": diagnostic,
        "location": location,
    }


def _attach_runtime(cell: nbformat.NotebookNode, payload: dict[str, object]) -> None:
    messages = payload.get("messages", [])
    cell.outputs = [
        *(
            [
                nbformat.v4.new_output(
                    "stream",
                    name="stdout",
                    text="".join(f"{message}\n" for message in messages),
                )
            ]
            if messages
            else []
        ),
        nbformat.v4.new_output(
            "execute_result",
            data={MACHINE_MIME_TYPE: payload, "text/plain": "runtime"},
            execution_count=cell.execution_count,
        ),
    ]
    if payload.get("succeeded") is False:
        cell.outputs[-1]["output_type"] = "display_data"
        cell.outputs[-1].pop("execution_count")
        cell.outputs.append(nbformat.v4.new_output(
            "error", ename="BslCellError", evalue="BSL execution failed", traceback=[]
        ))


def _reply_payload(cell: nbformat.NotebookNode) -> dict[str, object]:
    return next(
        output["data"][MACHINE_MIME_TYPE]
        for output in cell.outputs
        if MACHINE_MIME_TYPE in output.get("data", {})
    )


def _attach_json(cell: nbformat.NotebookNode, payload: dict[str, object]) -> None:
    cell.outputs = [
        nbformat.v4.new_output(
            "display_data",
            data={"application/json": payload, "text/plain": "json"},
        )
    ]


def _executed_notebook() -> nbformat.NotebookNode:
    notebook = build_fixture_notebook()
    cells = _cells(notebook)
    busy = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
    for index, tag in enumerate(REQUIRED_TAGS, start=1):
        cell = cells[tag]
        cell.execution_count = index
        start = busy + timedelta(milliseconds=100 * index)
        cell.metadata["execution"] = {
            "iopub.status.busy": start.isoformat(),
            "iopub.status.idle": (start + timedelta(milliseconds=50)).isoformat(),
        }

    _attach_runtime(
        cells["status-main"],
        {
            "state": "idle",
            "runtime_generation": 1,
            "operation_id": 0,
            "worker": None,
            "messages": [],
        },
    )
    _attach_runtime(
        cells["main-statement"],
        _runtime_payload(
            kind="main_completed",
            operation_id=1,
            state="completed",
            result=5,
            messages=["main-ready"],
        ),
    )
    _attach_json(
        cells["main-proxy"],
        {"status": "PASS", "nested_numbers": [3, 5], "rows": 2},
    )
    _attach_runtime(
        cells["main-method"],
        _runtime_payload(
            kind="worker_loaded",
            operation_id=1,
            state="completed",
            result={"runtime_generation": 1, "context_generation": 1,
                    "generation": 1, "manifest_sha256": "a" * 64},
        ),
    )
    _attach_json(
        cells["main-proxy-repeat"],
        {"status": "PASS", "same_generation": True, "rows": 2},
    )
    _attach_runtime(
        cells["main-method-call"],
        _runtime_payload(
            kind="main_completed", operation_id=2, state="completed", result=12
        ),
    )
    _attach_runtime(
        cells["main-mixed"],
        _runtime_payload(
            kind="main_completed", operation_id=3, state="completed", result=21
        ),
    )
    _attach_runtime(
        cells["main-error"],
        _runtime_payload(
            kind="main_completed",
            operation_id=4,
            state="failed",
            succeeded=False,
            diagnostic={"stage": "execution"},
        ),
    )
    _attach_runtime(
        cells["main-recovery"],
        _runtime_payload(
            kind="main_completed",
            operation_id=5,
            state="completed",
            result="999|12|21",
            messages=["main-recovery:999:12:21"],
        ),
    )
    capture_operation = 6
    callee_location = {
        "module_type": "ExtensionModule",
        "extension_name": "JupyterBslTestFixture",
        "line": 26,
    }
    caller_location = {
        "module_type": "ExtensionModule",
        "extension_name": "JupyterBslTestFixture",
        "line": 4,
    }
    _attach_runtime(
        cells["capture-main"],
        _runtime_payload(
            kind="captured",
            operation_id=capture_operation,
            state="captured",
            stop_sequence=1,
            location=callee_location,
        ),
    )
    _attach_runtime(
        cells["capture-snapshot"],
        _runtime_payload(
            kind="capture_cell",
            operation_id=capture_operation,
            state="captured",
            result=10,
        ),
    )
    _attach_json(
        cells["capture-proxy"],
        {"status": "PASS", "counter": 10, "rows": 2},
    )
    _attach_runtime(
        cells["capture-method"],
        _runtime_payload(
            kind="worker_loaded",
            operation_id=capture_operation,
            state="captured",
            result={"runtime_generation": 1, "context_generation": 1,
                    "generation": 3, "manifest_sha256": "b" * 64},
        ),
    )
    _attach_json(
        cells["capture-proxy-repeat"],
        {"status": "PASS", "same_generation": True, "rows": 2},
    )
    _attach_runtime(
        cells["capture-method-call"],
        _runtime_payload(
            kind="capture_cell",
            operation_id=capture_operation,
            state="captured",
            result=15,
        ),
    )
    _attach_runtime(
        cells["capture-mixed"],
        _runtime_payload(
            kind="capture_cell",
            operation_id=capture_operation,
            state="captured",
            result=20,
        ),
    )
    _attach_runtime(
        cells["capture-mixed-error"],
        _runtime_payload(
            kind="capture_cell",
            operation_id=capture_operation,
            state="captured",
            succeeded=False,
            diagnostic={
                "diagnostic_id": "d" * 64,
                "stage": "execution",
                "mapping_confidence": "exact",
                "visible_location": {
                    "line": 5,
                    "column": 1,
                    "span": {"start": 160, "end": 161},
                },
            },
        ),
    )
    _reply_payload(cells["capture-mixed-error"])[
        "source_unit"
    ] = {
        "kind": "notebook_cell",
        "unit_id": "fixture:execution:20",
        "revision": 20,
        "source_sha256": source_sha256(CAPTURE_MIXED_ERROR_SOURCE),
    }
    _attach_runtime(
        cells["capture-mixed-error-recovery"],
        _runtime_payload(
            kind="capture_cell",
            operation_id=capture_operation,
            state="captured",
            result=902,
        ),
    )
    _attach_runtime(
        cells["capture-error"],
        _runtime_payload(
            kind="capture_cell",
            operation_id=capture_operation,
            state="captured",
            succeeded=False,
            diagnostic={"stage": "execution"},
        ),
    )
    _attach_runtime(
        cells["capture-error-recovery"],
        _runtime_payload(
            kind="capture_cell",
            operation_id=capture_operation,
            state="captured",
            result=900,
            messages=["capture-recovery:900"],
        ),
    )
    _attach_runtime(
        cells["capture-write"],
        _runtime_payload(
            kind="capture_cell",
            operation_id=capture_operation,
            state="captured",
            result=40,
        ),
    )
    _attach_runtime(
        cells["capture-resume-a"],
        _runtime_payload(
            kind="captured",
            operation_id=capture_operation,
            state="captured",
            stop_sequence=2,
            location=caller_location,
        ),
    )
    _attach_runtime(
        cells["capture-stack"],
        _runtime_payload(
            kind="capture_cell",
            operation_id=capture_operation,
            state="captured",
            result=42,
            messages=["capture-stack:после:mixed=20"],
        ),
    )
    _attach_runtime(
        cells["capture-resume-b"],
        _runtime_payload(
            kind="main_completed",
            operation_id=capture_operation,
            state="completed",
            result=43,
        ),
    )
    _attach_json(
        cells["final"],
        {
            "status": "PASS",
            "main_proxy_rows": 2,
            "capture_proxy_rows": 2,
            "python_sentinel": "alive",
        },
    )
    return notebook


def _write_executed(tmp_path: Path) -> tuple[Path, nbformat.NotebookNode]:
    notebook = _executed_notebook()
    path = tmp_path / "executed.ipynb"
    nbformat.write(notebook, path)
    return path, notebook


def test_verify_fixture_execution_accepts_complete_flow(tmp_path: Path) -> None:
    path, _ = _write_executed(tmp_path)

    summary = verify_fixture_execution(path)

    assert summary["status"] == "PASS"
    assert summary["capture_operation_id"] == 6
    assert summary["capture_sequences"] == [1, 2]
    assert summary["writeback_value"] == 42
    assert summary["final_result"] == 43
    assert summary["main_method_result"] == 12
    assert summary["main_mixed_result"] == 21
    assert summary["capture_method_result"] == 15
    assert summary["capture_mixed_result"] == 20
    assert summary["capture_mixed_writeback"] == 20
    assert summary["capture_mixed_error_recovery"] == 902
    assert summary["capture_mixed_diagnostic_binding"] == "exact"
    assert summary["main_proxy_rows"] == 2
    assert summary["capture_proxy_rows"] == 2
    assert summary["executed_code_cells"] == len(REQUIRED_TAGS)
    assert summary["within_target_budget"] is True


@pytest.mark.parametrize(("tag", "field", "value"), [
    ("main-method", "generation", True),
    ("main-method", "manifest_sha256", "bad"),
    ("main-method", "runtime_generation", 2),
    ("capture-method", "generation", 1),
    ("capture-method", "runtime_generation", 2),
    ("capture-method", "context_generation", 2),
    ("capture-method", "manifest_sha256", "bad"),
])
def test_verifier_rejects_invalid_worker_generation(
    tmp_path: Path, tag: str, field: str, value: object,
) -> None:
    path, notebook = _write_executed(tmp_path)
    _reply_payload(_cells(notebook)[tag])["result"][field] = value
    nbformat.write(notebook, path)
    with pytest.raises(ProtocolError, match="generation is invalid"):
        verify_fixture_execution(path)


@pytest.mark.parametrize(
    ("tag", "field", "value", "message"),
    (
        ("capture-snapshot", "operation_id", 7, "operation identity"),
        ("capture-mixed", "result", 19, "capture-mixed"),
        ("capture-mixed-error", "succeeded", True, "mixed CAPTURE error"),
        (
            "capture-mixed-error-recovery",
            "result",
            901,
            "capture-mixed-error-recovery",
        ),
        ("capture-resume-a", "stop_sequence", 3, "capture sequence"),
        ("capture-error", "state", "failed", "CAPTURE error"),
        ("capture-error-recovery", "result", 10, "failure side effect"),
        ("capture-stack", "result", 41, "write-back"),
        ("capture-resume-b", "result", 44, "final result"),
    ),
)
def test_verifier_rejects_semantic_mutation(
    tmp_path: Path, tag: str, field: str, value: object, message: str
) -> None:
    path, notebook = _write_executed(tmp_path)
    cell = _cells(notebook)[tag]
    payload = _reply_payload(cell)
    payload[field] = value
    nbformat.write(notebook, path)

    with pytest.raises(ProtocolError, match=message):
        verify_fixture_execution(path)


def test_verifier_rejects_unexpected_jupyter_error_output(tmp_path: Path) -> None:
    path, notebook = _write_executed(tmp_path)
    _cells(notebook)["main-proxy"].outputs.append(
        nbformat.v4.new_output(
            "error", ename="AssertionError", evalue="wrong", traceback=[]
        )
    )
    nbformat.write(notebook, path)

    with pytest.raises(ProtocolError, match="main-proxy"):
        verify_fixture_execution(path)


@pytest.mark.parametrize("tag", sorted(EXPECTED_ERROR_TAGS))
def test_verifier_requires_real_jupyter_error_for_expected_failure(
    tmp_path: Path, tag: str,
) -> None:
    path, notebook = _write_executed(tmp_path)
    cell = _cells(notebook)[tag]
    cell.outputs = [output for output in cell.outputs if output.output_type != "error"]
    nbformat.write(notebook, path)
    with pytest.raises(ProtocolError, match=f"{tag}.*expected.*error"):
        verify_fixture_execution(path)


@pytest.mark.parametrize("ename", ["AssertionError", "RuntimeError", "BslCellError"])
def test_verifier_rejects_wrong_or_duplicate_expected_error(
    tmp_path: Path, ename: str,
) -> None:
    path, notebook = _write_executed(tmp_path)
    cell = _cells(notebook)["main-error"]
    if ename == "BslCellError":
        cell.outputs.append(
            nbformat.v4.new_output("error", ename=ename, evalue="wrong", traceback=[])
        )
    else:
        cell.outputs[-1]["ename"] = ename
    nbformat.write(notebook, path)
    with pytest.raises(ProtocolError, match="main-error"):
        verify_fixture_execution(path)


def test_verifier_rejects_unbound_mixed_capture_diagnostic(tmp_path: Path) -> None:
    path, notebook = _write_executed(tmp_path)
    payload = _reply_payload(_cells(notebook)["capture-mixed-error"])
    payload["diagnostic"] = {}
    nbformat.write(notebook, path)

    with pytest.raises(ProtocolError, match="diagnostic binding"):
        verify_fixture_execution(path)


def test_verifier_reports_consistent_fail_closed_mixed_diagnostic(
    tmp_path: Path,
) -> None:
    path, notebook = _write_executed(tmp_path)
    payload = _reply_payload(_cells(notebook)["capture-mixed-error"])
    payload["diagnostic"] = {}
    payload["diagnostic_details"] = {}
    payload.pop("source_unit")
    nbformat.write(notebook, path)

    summary = verify_fixture_execution(path)

    assert summary["capture_mixed_diagnostic_binding"] == "unavailable"


@pytest.mark.parametrize("inconsistent", (False, True))
def test_verifier_checks_unknown_mixed_diagnostic_without_inventing_a_location(
    tmp_path: Path, inconsistent: bool,
) -> None:
    path, notebook = _write_executed(tmp_path)
    payload = _reply_payload(_cells(notebook)["capture-mixed-error"])
    payload.pop("source_unit")
    payload["diagnostic"] = {
        "diagnostic_id": "d" * 64, "stage": "execution",
        "mapping_confidence": "unknown", "visible_location": None,
    }
    payload["diagnostic_details"] = {
        **payload["diagnostic"],
        "lowered_location": {"line": 5} if inconsistent else None,
    }
    nbformat.write(notebook, path)
    if inconsistent:
        with pytest.raises(ProtocolError, match="diagnostic binding"):
            verify_fixture_execution(path)
    else:
        assert verify_fixture_execution(path)["capture_mixed_diagnostic_binding"] == "unavailable"


def test_execute_fixture_notebook_writes_and_verifies_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.ipynb"
    destination = tmp_path / "result" / "executed.ipynb"
    nbformat.write(build_fixture_notebook(), source)

    def fake_execute(spec) -> NotebookExecutionResult:
        spec.destination.parent.mkdir(parents=True, exist_ok=True)
        nbformat.write(_executed_notebook(), spec.destination)
        return NotebookExecutionResult(
            spec.destination.resolve(),
            1.0,
            {"owned_process_count": 0, "owned_processes": [], "reconciled": []},
        )

    monkeypatch.setattr(
        "integration.jupyter_bsl_fixture.live.execute_notebook", fake_execute
    )

    run_dir = execute_fixture_notebook(
        source,
        destination,
        environment={"ONEC_RUNTIME_PROCESS_SNAPSHOT": str(tmp_path / "owned.json")},
        ownership_markers=(str(tmp_path / "infobase"),),
    )

    summary = verify_fixture_live_artifact(run_dir)
    assert summary["status"] == "PASS"
    assert summary["owned_process_count"] == 0
    assert json.loads((run_dir / "processes.json").read_text(encoding="utf-8"))[
        "owned_process_count"
    ] == 0


def test_execute_fixture_notebook_persists_failure_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.ipynb"
    destination = tmp_path / "result" / "executed.ipynb"
    nbformat.write(build_fixture_notebook(), source)

    def fake_execute(spec) -> NotebookExecutionResult:
        spec.destination.parent.mkdir(parents=True, exist_ok=True)
        notebook = _executed_notebook()
        _cells(notebook)["capture-stack"].outputs[-1]["data"][MACHINE_MIME_TYPE][
            "result"
        ] = 41
        nbformat.write(notebook, spec.destination)
        return NotebookExecutionResult(
            spec.destination.resolve(),
            1.0,
            {"owned_process_count": 0, "owned_processes": [], "reconciled": []},
        )

    monkeypatch.setattr(
        "integration.jupyter_bsl_fixture.live.execute_notebook", fake_execute
    )

    with pytest.raises(ProtocolError, match="write-back") as raised:
        execute_fixture_notebook(
            source,
            destination,
            environment={"ONEC_RUNTIME_PROCESS_SNAPSHOT": str(tmp_path / "owned.json")},
            ownership_markers=(str(tmp_path / "infobase"),),
        )

    run_dir = destination.with_suffix(".evidence")
    assert json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))[
        "status"
    ] == "FAIL"
    assert any("failure evidence" in note for note in raised.value.__notes__)
    assert (run_dir / "manifest.sha256").is_file()
