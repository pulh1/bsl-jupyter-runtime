from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import nbformat

from integration.jupyter_bsl_fixture.cells import CAPTURE_MIXED_ERROR_SOURCE
from integration.jupyter_bsl_fixture.notebook import (
    EXPECTED_ERROR_TAGS,
    REQUIRED_TAGS,
    verify_fixture_notebook,
)
from integration.notebook_live import (
    NotebookExecutionSpec,
    canonical_reply,
    execute_notebook,
    execution_timings,
    json_output,
    tagged_cells,
    verify_live_artifact,
    write_failure_artifact,
    write_live_artifact,
)
from onec_runtime.errors import ProtocolError
from onec_runtime.bsl import source_sha256


_RUNTIME_TAGS = (
    "status-main",
    "main-statement",
    "main-method",
    "main-method-call",
    "main-mixed",
    "main-error",
    "main-recovery",
    "capture-main",
    "capture-snapshot",
    "capture-method",
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
)
_JSON_TAGS = (
    "main-proxy",
    "main-proxy-repeat",
    "capture-proxy",
    "capture-proxy-repeat",
    "final",
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ProtocolError(message)


def _valid_worker_generation(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    digest = value.get("manifest_sha256")
    return (
        all(
            type(value.get(key)) is int and value[key] > 0
            for key in ("runtime_generation", "context_generation", "generation")
        )
        and isinstance(digest, str)
        and len(digest) == 64
        and all(character in "0123456789abcdef" for character in digest)
    )


def _reject_kernel_errors(notebook: Any) -> None:
    for cell in notebook.cells:
        if cell.cell_type != "code":
            continue
        tag = cell.metadata.get("tags", ["<untagged>"])[0]
        errors = [
            output for output in cell.get("outputs", [])
            if output.get("output_type") == "error"
        ]
        if tag in EXPECTED_ERROR_TAGS:
            _require(
                len(errors) == 1 and errors[0].get("ename") == "BslCellError",
                f"notebook {tag} expected one BSL cell error output",
            )
        elif errors:
            raise ProtocolError(f"notebook {tag} has an unexpected error output")
        for output in cell.get("outputs", []):
            if output.get("output_type") == "stream" and output.get("name") == "stderr":
                raise ProtocolError(f"notebook {tag} has unexpected stderr output")


def _successful(
    reply: Mapping[str, object],
    *,
    tag: str,
    kind: str,
    state: str,
    result: object,
) -> None:
    _require(reply.get("succeeded") is True, f"notebook {tag} did not succeed")
    _require(reply.get("kind") == kind, f"notebook {tag} kind is invalid")
    _require(reply.get("state") == state, f"notebook {tag} state is invalid")
    _require(reply.get("result") == result, f"notebook {tag} result is invalid")


def _capture_location(reply: Mapping[str, object], *, line: int) -> None:
    location = reply.get("location")
    _require(isinstance(location, dict), "CAPTURE location is missing")
    _require(
        location.get("module_type") == "ExtensionModule"
        and location.get("extension_name") == "JupyterBslTestFixture"
        and type(location.get("line")) is int
        and location.get("line") == line,
        "CAPTURE extension location is invalid",
    )


def verify_fixture_execution(path: Path) -> dict[str, object]:
    executed = Path(path).resolve()
    verify_fixture_notebook(executed, allow_outputs=True)
    notebook = nbformat.read(executed, as_version=4)
    _reject_kernel_errors(notebook)
    timings = execution_timings(notebook)
    cells = tagged_cells(notebook)
    replies = {tag: canonical_reply(cells[tag], tag) for tag in _RUNTIME_TAGS}
    payloads = {tag: json_output(cells[tag], tag) for tag in _JSON_TAGS}

    status = replies["status-main"]
    _require(
        status.get("state") == "idle"
        and status.get("runtime_generation") == 1
        and status.get("operation_id") == 0
        and status.get("worker") is None,
        "initial MAIN state is invalid",
    )

    _successful(
        replies["main-statement"],
        tag="main-statement",
        kind="main_completed",
        state="completed",
        result=5,
    )
    _require(
        replies["main-statement"].get("messages") == ["main-ready"],
        "MAIN statement messages are invalid",
    )
    _require(
        payloads["main-proxy"]
        == {"status": "PASS", "nested_numbers": [3, 5], "rows": 2},
        "MAIN proxy serialization is invalid",
    )

    main_method = replies["main-method"]
    _require(main_method.get("kind") == "worker_loaded", "MAIN procedure was not loaded")
    _require(main_method.get("succeeded") is True, "MAIN procedure load failed")
    main_worker = main_method.get("result")
    _require(isinstance(main_worker, dict), "MAIN worker payload is invalid")
    main_generation = main_worker.get("generation")
    _require(
        _valid_worker_generation(main_worker)
        and main_worker["runtime_generation"] == status["runtime_generation"],
        "MAIN worker generation is invalid",
    )
    _require(
        payloads["main-proxy-repeat"]
        == {"status": "PASS", "same_generation": True, "rows": 2},
        "repeated MAIN proxy serialization is invalid",
    )
    _successful(
        replies["main-method-call"],
        tag="main-method-call",
        kind="main_completed",
        state="completed",
        result=12,
    )
    _successful(
        replies["main-mixed"],
        tag="main-mixed",
        kind="main_completed",
        state="completed",
        result=21,
    )
    main_error = replies["main-error"]
    _require(
        main_error.get("succeeded") is False
        and main_error.get("state") == "failed"
        and main_error.get("kind") == "main_completed"
        and isinstance(main_error.get("diagnostic"), dict),
        "expected MAIN error did not fail closed",
    )
    _successful(
        replies["main-recovery"],
        tag="main-recovery",
        kind="main_completed",
        state="completed",
        result="999|12|21",
    )

    capture = replies["capture-main"]
    operation_id = capture.get("operation_id")
    _require(type(operation_id) is int and operation_id > 0, "CAPTURE operation identity is invalid")
    _require(
        capture.get("kind") == "captured"
        and capture.get("state") == "captured"
        and capture.get("stop_sequence") == 1,
        "first capture sequence is invalid",
    )
    _capture_location(capture, line=26)
    capture_tags = (
        "capture-snapshot",
        "capture-method",
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
    )
    _require(
        all(replies[tag].get("operation_id") == operation_id for tag in capture_tags),
        "CAPTURE operation identity changed between cells",
    )
    _successful(
        replies["capture-snapshot"],
        tag="capture-snapshot",
        kind="capture_cell",
        state="captured",
        result=10,
    )
    _require(
        payloads["capture-proxy"]
        == {"status": "PASS", "counter": 10, "rows": 2},
        "CAPTURE proxy serialization is invalid",
    )
    capture_method = replies["capture-method"]
    capture_worker = capture_method.get("result")
    _require(
        capture_method.get("kind") == "worker_loaded"
        and capture_method.get("state") == "captured"
        and capture_method.get("succeeded") is True
        and _valid_worker_generation(capture_worker)
        and capture_worker["runtime_generation"] == main_worker["runtime_generation"]
        and capture_worker["context_generation"] == main_worker["context_generation"]
        and capture_worker["generation"] > main_generation,
        "CAPTURE procedure generation is invalid",
    )
    _require(
        payloads["capture-proxy-repeat"]
        == {"status": "PASS", "same_generation": True, "rows": 2},
        "repeated CAPTURE proxy serialization is invalid",
    )
    _successful(
        replies["capture-method-call"],
        tag="capture-method-call",
        kind="capture_cell",
        state="captured",
        result=15,
    )
    _successful(
        replies["capture-mixed"],
        tag="capture-mixed",
        kind="capture_cell",
        state="captured",
        result=20,
    )
    mixed_error = replies["capture-mixed-error"]
    mixed_diagnostic = mixed_error.get("diagnostic")
    mixed_location = (
        mixed_diagnostic.get("visible_location")
        if isinstance(mixed_diagnostic, dict)
        else None
    )
    mixed_source_unit = mixed_error.get("source_unit")
    _require(
        mixed_error.get("succeeded") is False
        and mixed_error.get("state") == "captured"
        and mixed_error.get("kind") == "capture_cell"
        and isinstance(mixed_diagnostic, dict),
        "mixed CAPTURE error partial-success state is invalid",
    )
    if mixed_diagnostic and mixed_diagnostic.get("mapping_confidence") == "unknown":
        details = mixed_error.get("diagnostic_details")
        _require(
            type(mixed_diagnostic.get("diagnostic_id")) is str
            and len(mixed_diagnostic["diagnostic_id"]) == 64
            and mixed_diagnostic.get("stage") == "execution"
            and mixed_location is None
            and "source_unit" not in mixed_error
            and isinstance(details, dict)
            and details.get("diagnostic_id") == mixed_diagnostic["diagnostic_id"]
            and details.get("mapping_confidence") == "unknown"
            and details.get("visible_location") is None
            and details.get("lowered_location") is None,
            "mixed CAPTURE error diagnostic binding is invalid",
        )
        mixed_diagnostic_binding = "unavailable"
    elif mixed_diagnostic:
        _require(
            type(mixed_diagnostic.get("diagnostic_id")) is str
            and len(mixed_diagnostic["diagnostic_id"]) == 64
            and mixed_diagnostic.get("stage") == "execution"
            and mixed_diagnostic.get("mapping_confidence") == "exact"
            and isinstance(mixed_location, dict)
            and mixed_location.get("line") == 5
            and isinstance(mixed_source_unit, dict)
            and mixed_source_unit.get("kind") == "notebook_cell"
            and mixed_source_unit.get("source_sha256")
            == source_sha256(CAPTURE_MIXED_ERROR_SOURCE),
            "mixed CAPTURE error diagnostic binding is invalid",
        )
        mixed_diagnostic_binding = "exact"
    else:
        _require(
            mixed_error.get("diagnostic_details") == {}
            and "source_unit" not in mixed_error,
            "mixed CAPTURE error diagnostic binding is invalid",
        )
        mixed_diagnostic_binding = "unavailable"
    _successful(
        replies["capture-mixed-error-recovery"],
        tag="capture-mixed-error-recovery",
        kind="capture_cell",
        state="captured",
        result=902,
    )
    capture_error = replies["capture-error"]
    _require(
        capture_error.get("succeeded") is False
        and capture_error.get("state") == "captured"
        and capture_error.get("kind") == "capture_cell"
        and isinstance(capture_error.get("diagnostic"), dict),
        "expected CAPTURE error did not preserve CAPTURE state",
    )
    recovery = replies["capture-error-recovery"]
    _require(
        recovery.get("succeeded") is True
        and recovery.get("state") == "captured"
        and recovery.get("result") == 900,
        "CAPTURE failure side effect is invalid",
    )
    _successful(
        replies["capture-write"],
        tag="capture-write",
        kind="capture_cell",
        state="captured",
        result=40,
    )
    resume_a = replies["capture-resume-a"]
    _require(
        resume_a.get("kind") == "captured"
        and resume_a.get("state") == "captured"
        and resume_a.get("stop_sequence") == 2,
        "second capture sequence is invalid",
    )
    _capture_location(resume_a, line=4)
    stack = replies["capture-stack"]
    _require(
        stack.get("succeeded") is True
        and stack.get("state") == "captured"
        and stack.get("result") == 42
        and stack.get("messages") == ["capture-stack:после:mixed=20"],
        "CAPTURE write-back was not visible in caller stack",
    )
    final_reply = replies["capture-resume-b"]
    _require(
        final_reply.get("kind") == "main_completed"
        and final_reply.get("state") == "completed"
        and final_reply.get("succeeded") is True
        and final_reply.get("result") == 43,
        "CAPTURE final result is invalid",
    )
    final = payloads["final"]
    _require(
        final
        == {
            "status": "PASS",
            "main_proxy_rows": 2,
            "capture_proxy_rows": 2,
            "python_sentinel": "alive",
        },
        "final Python state is invalid",
    )

    elapsed_s = round(sum(float(item["duration_s"]) for item in timings), 6)
    return {
        "status": "PASS",
        "capture_operation_id": operation_id,
        "capture_sequences": [1, 2],
        "writeback_value": 42,
        "final_result": 43,
        "main_method_result": 12,
        "main_mixed_result": 21,
        "main_error_value": 999,
        "capture_error_value": 900,
        "capture_method_result": 15,
        "capture_mixed_result": 20,
        "capture_mixed_writeback": 20,
        "capture_mixed_error_recovery": 902,
        "capture_mixed_diagnostic_binding": mixed_diagnostic_binding,
        "main_proxy_rows": 2,
        "capture_proxy_rows": 2,
        "executed_code_cells": len(timings),
        "elapsed_s": elapsed_s,
        "target_budget_s": 60,
        "within_target_budget": elapsed_s <= 60,
    }


def execute_fixture_notebook(
    source: Path,
    destination: Path,
    *,
    environment: Mapping[str, str],
    ownership_markers: Sequence[str],
) -> Path:
    source_path = Path(source).resolve()
    destination_path = Path(destination).resolve()
    verify_fixture_notebook(source_path, allow_outputs=False)
    snapshot_value = environment.get("ONEC_RUNTIME_PROCESS_SNAPSHOT")
    if not snapshot_value:
        raise ProtocolError("ONEC_RUNTIME_PROCESS_SNAPSHOT is required")
    run_dir = destination_path.with_suffix(".evidence")
    try:
        result = execute_notebook(
            NotebookExecutionSpec(
                source=source_path,
                destination=destination_path,
                working_directory=destination_path.parent,
                environment=environment,
                timeout_s=300,
                process_snapshot=Path(snapshot_value),
                ownership_markers=tuple(ownership_markers),
            )
        )
    except BaseException as error:
        if destination_path.is_file() and not run_dir.exists():
            write_failure_artifact(run_dir, destination_path, failure=error)
        raise
    try:
        summary = verify_fixture_execution(result.executed_notebook)
    except BaseException as error:
        setattr(error, "_onec_notebook_processes", result.processes)
        if not run_dir.exists():
            write_failure_artifact(run_dir, result.executed_notebook, failure=error)
        raise
    write_live_artifact(
        run_dir,
        result.executed_notebook,
        summary=summary,
        processes=result.processes,
    )
    verify_fixture_live_artifact(run_dir)
    return run_dir


def verify_fixture_live_artifact(run_dir: Path) -> dict[str, object]:
    return verify_live_artifact(
        Path(run_dir),
        recompute_summary=verify_fixture_execution,
    )
