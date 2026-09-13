from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import nbformat
import pytest

from onec_runtime.errors import ProtocolError
from onec_runtime_jupyter.extension import MACHINE_MIME_TYPE

from integration.notebook_live import (
    NotebookExecutionSpec,
    canonical_reply,
    execute_notebook,
    execution_timings,
    json_output,
    reconcile_owned_processes,
    tagged_cells,
    verify_live_artifact,
    write_failure_artifact,
    write_live_artifact,
)


def _tagged_code(tag: str, source: str) -> nbformat.NotebookNode:
    cell = nbformat.v4.new_code_cell(source)
    cell.metadata["tags"] = [tag]
    return cell


def _timed(cell: nbformat.NotebookNode, *, count: int = 1) -> None:
    busy = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
    idle = busy + timedelta(milliseconds=250)
    cell.execution_count = count
    cell.metadata["execution"] = {
        "iopub.status.busy": busy.isoformat(),
        "iopub.status.idle": idle.isoformat(),
    }


def test_tagged_cells_rejects_duplicate_tag() -> None:
    notebook = nbformat.v4.new_notebook(
        cells=[_tagged_code("same", "1"), _tagged_code("same", "2")]
    )

    with pytest.raises(ProtocolError, match="duplicate notebook tag"):
        tagged_cells(notebook)


def test_tagged_cells_indexes_scenarios_with_shared_expected_error_control_tag() -> None:
    notebook = nbformat.v4.new_notebook(cells=[
        _tagged_code("main-error", "1"), _tagged_code("capture-error", "2")
    ])
    for cell in notebook.cells:
        cell.metadata["tags"].append("raises-exception")
    indexed = tagged_cells(notebook)
    assert set(indexed) == {"main-error", "capture-error"}
    assert indexed["main-error"].source == "1"
    assert indexed["capture-error"].source == "2"


def test_failure_artifact_identifies_unexpected_error_after_allowed_error(
    tmp_path: Path,
) -> None:
    allowed = _tagged_code("expected-negative", "raise ValueError('expected')")
    allowed.metadata["tags"].append("raises-exception")
    failed = _tagged_code("recovery", "raise AssertionError('unexpected')")
    for count, cell in enumerate((allowed, failed), 1):
        _timed(cell, count=count)
        cell.outputs = [nbformat.v4.new_output(
            "error", ename="ValueError", evalue="controlled", traceback=[]
        )]
    executed = tmp_path / "executed.ipynb"
    nbformat.write(nbformat.v4.new_notebook(cells=[allowed, failed]), executed)
    evidence = write_failure_artifact(
        tmp_path / "evidence", executed, failure=AssertionError("unexpected")
    )
    summary = json.loads((evidence / "summary.json").read_text(encoding="utf-8"))
    assert summary["failing_cell_tag"] == "recovery"


def test_canonical_reply_requires_machine_payload_and_exact_stdout() -> None:
    cell = _tagged_code("message", '%%bsl\nСообщить("one");')
    cell.outputs = [
        nbformat.v4.new_output("stream", name="stdout", text="one\n"),
        nbformat.v4.new_output(
            "execute_result",
            data={
                MACHINE_MIME_TYPE: {
                    "messages": ["one"],
                    "result": 1,
                    "succeeded": True,
                }
            },
            execution_count=1,
        ),
    ]

    assert canonical_reply(cell, "message")["result"] == 1

    cell.outputs[0]["text"] = "wrong\n"
    with pytest.raises(ProtocolError, match="stdout"):
        canonical_reply(cell, "message")


def test_json_output_requires_one_application_json_payload() -> None:
    cell = _tagged_code("proxy-check", "JSON({'status': 'PASS'})")
    cell.outputs = [
        nbformat.v4.new_output(
            "display_data", data={"application/json": {"status": "PASS"}}
        )
    ]

    assert json_output(cell, "proxy-check") == {"status": "PASS"}

    cell.outputs.append(
        nbformat.v4.new_output(
            "display_data", data={"application/json": {"status": "duplicate"}}
        )
    )
    with pytest.raises(ProtocolError, match="one application/json"):
        json_output(cell, "proxy-check")


def test_execution_timings_hashes_outputs_and_requires_positive_duration() -> None:
    cell = _tagged_code("timed", "1")
    cell.outputs = [
        nbformat.v4.new_output("execute_result", data={"text/plain": "1"}, execution_count=1)
    ]
    _timed(cell)

    timings = execution_timings(nbformat.v4.new_notebook(cells=[cell]))

    assert timings[0]["tag"] == "timed"
    assert timings[0]["duration_s"] == 0.25
    assert set(timings[0]["output_mime_sha256"]) == {"text/plain"}


def test_execute_notebook_forwards_environment_and_persists_result(tmp_path: Path) -> None:
    source = tmp_path / "source.ipynb"
    destination = tmp_path / "executed.ipynb"
    process_snapshot = tmp_path / "owned-processes.json"
    notebook = nbformat.v4.new_notebook(
        cells=[
            _tagged_code(
                "environment",
                "import os\nassert os.environ['ONEC_NOTEBOOK_RUNNER_SENTINEL'] == 'visible'",
            ),
            _tagged_code("result", "'done'"),
        ]
    )
    nbformat.write(notebook, source)

    result = execute_notebook(
        NotebookExecutionSpec(
            source=source,
            destination=destination,
            working_directory=tmp_path,
            environment={"ONEC_NOTEBOOK_RUNNER_SENTINEL": "visible"},
            timeout_s=30,
            process_snapshot=process_snapshot,
            ownership_markers=(str(tmp_path / "unique-infobase"),),
        )
    )

    executed = nbformat.read(destination, as_version=4)
    assert result.executed_notebook == destination.resolve()
    assert result.elapsed_s > 0
    assert result.processes["owned_process_count"] == 0
    assert all(cell.execution_count for cell in executed.cells)
    assert executed.cells[1].outputs[0]["data"]["text/plain"] == "'done'"


def test_execute_notebook_persists_failure_and_names_cell(tmp_path: Path) -> None:
    source = tmp_path / "source.ipynb"
    destination = tmp_path / "executed.ipynb"
    notebook = nbformat.v4.new_notebook(
        cells=[_tagged_code("explodes", "raise AssertionError('boom')")]
    )
    nbformat.write(notebook, source)

    with pytest.raises(Exception, match="boom") as raised:
        execute_notebook(
            NotebookExecutionSpec(
                source=source,
                destination=destination,
                working_directory=tmp_path,
                environment={},
                timeout_s=30,
                process_snapshot=tmp_path / "owned-processes.json",
                ownership_markers=(str(tmp_path / "unique-infobase"),),
            )
        )

    assert destination.is_file()
    assert any("explodes" in note for note in raised.value.__notes__)
    assert raised.value._onec_notebook_processes["owned_process_count"] == 0


def test_execute_notebook_reconciles_even_when_persistence_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.ipynb"
    destination = tmp_path / "executed.ipynb"
    nbformat.write(nbformat.v4.new_notebook(cells=[]), source)
    reconciled: list[bool] = []

    monkeypatch.setattr(
        "integration.notebook_live.NotebookClient.execute", lambda _self, **_kwargs: None
    )
    monkeypatch.setattr(
        "integration.notebook_live.nbformat.write",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    monkeypatch.setattr(
        "integration.notebook_live.reconcile_owned_processes",
        lambda *_args, **_kwargs: reconciled.append(True)
        or {"owned_process_count": 0, "owned_processes": [], "reconciled": []},
    )

    with pytest.raises(OSError, match="disk full"):
        execute_notebook(
            NotebookExecutionSpec(
                source=source,
                destination=destination,
                working_directory=tmp_path,
                environment={},
                timeout_s=30,
                process_snapshot=tmp_path / "owned.json",
                ownership_markers=(str(tmp_path / "unique-infobase"),),
            )
        )

    assert reconciled == [True]


class _FakeProcess:
    def __init__(
        self,
        pid: int,
        created: float,
        *,
        name: str,
        command: str,
    ) -> None:
        self.pid = pid
        self._created = created
        self._name = name
        self._command = command
        self.actions: list[str] = []

    def create_time(self) -> float:
        return self._created

    def name(self) -> str:
        return self._name

    def cmdline(self) -> list[str]:
        return [self._command]

    def terminate(self) -> None:
        self.actions.append("terminated")

    def wait(self, _timeout: float) -> int:
        return 0

    def kill(self) -> None:
        self.actions.append("killed")


def test_reconcile_terminates_exact_identity(tmp_path: Path, monkeypatch) -> None:
    snapshot = tmp_path / "owned.json"
    snapshot.write_text(
        json.dumps([{"pid": 41, "create_time": 10.0, "role": "dbgs"}]),
        encoding="utf-8",
    )
    process = _FakeProcess(41, 10.0, name="dbgs.exe", command="dbgs")
    monkeypatch.setattr("integration.notebook_live.psutil.Process", lambda _pid: process)
    monkeypatch.setattr("integration.notebook_live.psutil.process_iter", lambda _attrs: [])

    report = reconcile_owned_processes(snapshot, (str(tmp_path),), started_at=1.0)

    assert process.actions == ["terminated"]
    assert report == {
        "owned_process_count": 0,
        "owned_processes": [],
        "reconciled": [{"pid": 41, "role": "dbgs", "action": "terminated"}],
    }


def test_reconcile_never_touches_reused_pid(tmp_path: Path, monkeypatch) -> None:
    snapshot = tmp_path / "owned.json"
    snapshot.write_text(
        json.dumps([{"pid": 41, "create_time": 10.0, "role": "dbgs"}]),
        encoding="utf-8",
    )
    process = _FakeProcess(41, 11.0, name="python.exe", command="unrelated")
    monkeypatch.setattr("integration.notebook_live.psutil.Process", lambda _pid: process)
    monkeypatch.setattr("integration.notebook_live.psutil.process_iter", lambda _attrs: [])

    report = reconcile_owned_processes(snapshot, (str(tmp_path),), started_at=1.0)

    assert process.actions == []
    assert report["owned_process_count"] == 0


def test_reconcile_fallback_requires_name_time_and_unique_marker(
    tmp_path: Path, monkeypatch
) -> None:
    marker = str(tmp_path / "unique-infobase")
    owned = _FakeProcess(51, 20.0, name="1cv8c.exe", command=f"/F {marker}")
    designer = _FakeProcess(54, 20.0, name="1cv8.exe", command=f"DESIGNER /F {marker}")
    old = _FakeProcess(52, 1.0, name="dbgs.exe", command=marker)
    unrelated = _FakeProcess(53, 20.0, name="python.exe", command=marker)
    monkeypatch.setattr(
        "integration.notebook_live.psutil.process_iter",
        lambda _attrs: [owned, designer, old, unrelated],
    )

    report = reconcile_owned_processes(
        tmp_path / "missing.json", (marker,), started_at=10.0
    )

    assert owned.actions == ["terminated"]
    assert designer.actions == []
    assert old.actions == []
    assert unrelated.actions == []
    assert report["reconciled"] == [
        {"pid": 51, "role": "fallback:1cv8c.exe", "action": "terminated"}
    ]


def test_live_artifact_recomputes_summary_and_rejects_process_survivor(
    tmp_path: Path,
) -> None:
    executed = tmp_path / "executed-source.ipynb"
    cell = _tagged_code("done", "1")
    _timed(cell)
    nbformat.write(nbformat.v4.new_notebook(cells=[cell]), executed)
    run_dir = tmp_path / "evidence"
    summary = {"status": "PASS", "executed_code_cells": 1}
    write_live_artifact(
        run_dir,
        executed,
        summary=summary,
        processes={"owned_process_count": 0, "owned_processes": []},
    )

    assert verify_live_artifact(
        run_dir, recompute_summary=lambda _path: dict(summary)
    )["owned_process_count"] == 0

    processes = run_dir / "processes.json"
    processes.write_text(
        json.dumps({"owned_process_count": 1, "owned_processes": [{"pid": 9}]}),
        encoding="utf-8",
    )
    with pytest.raises(ProtocolError, match="manifest hash mismatch"):
        verify_live_artifact(run_dir, recompute_summary=lambda _path: dict(summary))
