from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
from time import monotonic, time
from typing import Any

import nbformat
from nbclient import NotebookClient
import psutil

from onec_runtime.errors import ProtocolError
from onec_runtime_jupyter.extension import MACHINE_MIME_TYPE


ARTIFACT_FILES = (
    "executed.ipynb",
    "summary.json",
    "timings.json",
    "processes.json",
)
_OWNED_PROCESS_NAMES = {"dbgs.exe", "1cv8c.exe"}
_FAILURE_PROCESSES_ATTRIBUTE = "_onec_notebook_processes"


@dataclass(frozen=True, slots=True)
class NotebookExecutionSpec:
    source: Path
    destination: Path
    working_directory: Path
    environment: Mapping[str, str]
    timeout_s: int
    process_snapshot: Path
    ownership_markers: Sequence[str]
    kernel_name: str = "python3"


@dataclass(frozen=True, slots=True)
class NotebookExecutionResult:
    executed_notebook: Path
    elapsed_s: float
    processes: dict[str, object]


def tagged_cells(notebook: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for cell in notebook.cells:
        for tag in cell.metadata.get("tags", []):
            if tag == "raises-exception":
                continue  # nbclient execution control, not a scenario identity
            if tag in result:
                raise ProtocolError(f"duplicate notebook tag: {tag}")
            result[tag] = cell
    return result


def _data_payloads(cell: Any, mime: str) -> list[object]:
    return [
        output.get("data", {}).get(mime)
        for output in cell.get("outputs", [])
        if output.get("data", {}).get(mime) is not None
    ]


def canonical_reply(cell: Any, tag: str) -> dict[str, Any]:
    replies = _data_payloads(cell, MACHINE_MIME_TYPE)
    if len(replies) != 1 or not isinstance(replies[0], dict):
        raise ProtocolError(f"notebook {tag} requires one canonical runtime reply")
    reply = dict(replies[0])
    messages = reply.get("messages", [])
    if not isinstance(messages, list) or any(not isinstance(item, str) for item in messages):
        raise ProtocolError(f"notebook {tag} runtime messages are invalid")
    stdout = "".join(
        str(output.get("text", ""))
        for output in cell.get("outputs", [])
        if output.get("output_type") == "stream" and output.get("name") == "stdout"
    )
    expected = "".join(f"{message}\n" for message in messages)
    if stdout != expected:
        raise ProtocolError(f"notebook {tag} messages do not match stdout")
    return reply


def json_output(cell: Any, tag: str) -> dict[str, Any]:
    payloads = _data_payloads(cell, "application/json")
    if len(payloads) != 1 or not isinstance(payloads[0], dict):
        raise ProtocolError(f"notebook {tag} requires one application/json payload")
    return dict(payloads[0])


def _duration_s(cell: Any) -> float:
    execution = cell.metadata.get("execution", {})
    try:
        busy = datetime.fromisoformat(execution["iopub.status.busy"].replace("Z", "+00:00"))
        idle = datetime.fromisoformat(execution["iopub.status.idle"].replace("Z", "+00:00"))
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise ProtocolError("notebook cell timing is missing or invalid") from error
    duration = (idle - busy).total_seconds()
    if duration < 0:
        raise ProtocolError("notebook cell timing is negative")
    return duration


def _output_mime_hashes(cell: Any) -> dict[str, str]:
    values: dict[str, list[object]] = {}
    for output in cell.get("outputs", []):
        for mime, value in output.get("data", {}).items():
            values.setdefault(mime, []).append(value)
    return {
        mime: sha256(
            json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        for mime, value in sorted(values.items())
    }


def execution_timings(notebook: Any) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for cell in notebook.cells:
        if cell.cell_type != "code":
            continue
        tags = cell.metadata.get("tags", [])
        tag = tags[0] if tags else ""
        if not isinstance(cell.execution_count, int) or cell.execution_count <= 0:
            raise ProtocolError(f"notebook {tag} cell was not executed")
        result.append(
            {
                "tag": tag,
                "execution_count": cell.execution_count,
                "duration_s": round(_duration_s(cell), 6),
                "status": "PASS",
                "output_mime_sha256": _output_mime_hashes(cell),
            }
        )
    return result


def partial_execution_timings(notebook: Any) -> list[dict[str, object]]:
    """Return evidence for the executed prefix of a failed notebook."""
    result: list[dict[str, object]] = []
    for cell in notebook.cells:
        if cell.cell_type != "code" or not isinstance(cell.execution_count, int):
            continue
        try:
            duration = round(_duration_s(cell), 6)
        except ProtocolError:
            duration = None
        result.append(
            {
                "tag": cell.metadata.get("tags", [""])[0],
                "execution_count": cell.execution_count,
                "duration_s": duration,
                "status": (
                    "FAIL"
                    if any(
                        output.get("output_type") == "error"
                        for output in cell.get("outputs", [])
                    )
                    else "PASS"
                ),
                "output_mime_sha256": _output_mime_hashes(cell),
            }
        )
    return result


def _validated_markers(markers: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(str(marker).strip().casefold() for marker in markers)
    if not normalized or any(not marker for marker in normalized):
        raise ProtocolError("owned process markers must be non-empty")
    if len(set(normalized)) != len(normalized):
        raise ProtocolError("owned process markers must be unique")
    return normalized


def _terminate_process(process: Any) -> str:
    try:
        process.terminate()
        process.wait(2.0)
        return "terminated"
    except psutil.TimeoutExpired:
        process.kill()
        process.wait(2.0)
        return "killed"


def _snapshot_identities(path: Path) -> list[dict[str, object]]:
    if not path.is_file():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProtocolError(f"owned process snapshot is invalid: {path}") from error
    if not isinstance(value, list):
        raise ProtocolError(f"owned process snapshot must be a list: {path}")
    result: list[dict[str, object]] = []
    for item in value:
        if (
            not isinstance(item, dict)
            or type(item.get("pid")) is not int
            or not isinstance(item.get("create_time"), (int, float))
            or type(item.get("role")) is not str
        ):
            raise ProtocolError(f"owned process identity is invalid: {path}")
        result.append(dict(item))
    return result


def reconcile_owned_processes(
    snapshot_path: Path,
    ownership_markers: Sequence[str],
    *,
    started_at: float,
) -> dict[str, object]:
    markers = _validated_markers(ownership_markers)
    reconciled: list[dict[str, object]] = []
    protected_reused_pids: set[int] = set()
    survivors: list[dict[str, object]] = []
    for identity in _snapshot_identities(Path(snapshot_path)):
        pid = int(identity["pid"])
        role = str(identity["role"])
        try:
            process = psutil.Process(pid)
            if process.create_time() != float(identity["create_time"]):
                protected_reused_pids.add(pid)
                continue
            action = _terminate_process(process)
            reconciled.append({"pid": pid, "role": role, "action": action})
        except psutil.NoSuchProcess:
            continue
        except (psutil.AccessDenied, OSError) as error:
            survivors.append({"pid": pid, "role": role, "error": type(error).__name__})
    for process in psutil.process_iter(("pid", "name", "create_time", "cmdline")):
        try:
            if process.pid in protected_reused_pids:
                continue
            name = process.name().casefold()
            created = float(process.create_time())
            command = " ".join(process.cmdline()).casefold()
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
        if (
            name not in _OWNED_PROCESS_NAMES
            or created < started_at - 1.0
            or not any(marker in command for marker in markers)
        ):
            continue
        try:
            action = _terminate_process(process)
            reconciled.append(
                {"pid": process.pid, "role": f"fallback:{name}", "action": action}
            )
        except psutil.NoSuchProcess:
            continue
        except (psutil.AccessDenied, OSError) as error:
            survivors.append(
                {
                    "pid": getattr(process, "pid", -1),
                    "role": "fallback",
                    "error": type(error).__name__,
                }
            )
    return {
        "owned_process_count": len(survivors),
        "owned_processes": survivors,
        "reconciled": reconciled,
    }


def _failing_cell_tag(notebook: Any) -> str:
    for cell in notebook.cells:
        if cell.cell_type != "code":
            continue
        tags = cell.metadata.get("tags", [])
        if "raises-exception" in tags:
            continue
        if any(output.get("output_type") == "error" for output in cell.get("outputs", [])):
            return tags[0] if tags else "<untagged>"
    return "<unknown>"


def execute_notebook(spec: NotebookExecutionSpec) -> NotebookExecutionResult:
    source = Path(spec.source).resolve()
    destination = Path(spec.destination).resolve()
    working_directory = Path(spec.working_directory).resolve()
    snapshot = Path(spec.process_snapshot).resolve()
    markers = _validated_markers(spec.ownership_markers)
    if not source.is_file():
        raise ProtocolError(f"source notebook is missing: {source}")
    if destination.exists():
        raise ProtocolError(f"executed notebook already exists: {destination}")
    if type(spec.timeout_s) is not int or spec.timeout_s <= 0:
        raise ProtocolError("notebook timeout must be a positive integer")
    notebook = nbformat.read(source, as_version=4)
    nbformat.validate(notebook)
    destination.parent.mkdir(parents=True, exist_ok=True)
    runtime_root = working_directory / ".notebook-live"
    ipython_dir = runtime_root / "ipython"
    jupyter_runtime = runtime_root / "jupyter-runtime"
    ipython_dir.mkdir(parents=True, exist_ok=True)
    jupyter_runtime.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    environment.update({key: str(value) for key, value in spec.environment.items()})
    environment.setdefault("JUPYTER_PLATFORM_DIRS", "1")
    environment.setdefault("PYDEVD_DISABLE_FILE_VALIDATION", "1")
    environment["IPYTHONDIR"] = str(ipython_dir)
    environment["JUPYTER_RUNTIME_DIR"] = str(jupyter_runtime)
    started_at = time()
    started = monotonic()
    failure: BaseException | None = None
    processes: dict[str, object] = {
        "owned_process_count": 0,
        "owned_processes": [],
        "reconciled": [],
    }
    try:
        NotebookClient(
            notebook,
            timeout=spec.timeout_s,
            kernel_name=spec.kernel_name,
            allow_errors=False,
            record_timing=True,
            resources={"metadata": {"path": str(working_directory)}},
        ).execute(env=environment)
    except BaseException as error:
        failure = error
        raise
    finally:
        persistence_error: BaseException | None = None
        cleanup_error: BaseException | None = None
        try:
            nbformat.write(notebook, destination)
        except BaseException as error:
            persistence_error = error
        try:
            processes = reconcile_owned_processes(snapshot, markers, started_at=started_at)
        except BaseException as error:
            cleanup_error = error
        primary = failure or persistence_error or cleanup_error
        if primary is not None:
            setattr(primary, _FAILURE_PROCESSES_ATTRIBUTE, processes)
            primary.add_note(f"notebook failing cell tag: {_failing_cell_tag(notebook)}")
            if persistence_error is not None and persistence_error is not primary:
                primary.add_note(
                    f"executed notebook persistence failed: {type(persistence_error).__name__}"
                )
            if cleanup_error is not None and cleanup_error is not primary:
                primary.add_note(
                    f"owned process reconciliation failed: {type(cleanup_error).__name__}"
                )
        if failure is None:
            if persistence_error is not None:
                raise persistence_error
            if cleanup_error is not None:
                raise cleanup_error
    return NotebookExecutionResult(
        destination,
        monotonic() - started,
        processes,
    )


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def write_live_artifact(
    run_dir: Path,
    executed_notebook: Path,
    *,
    summary: Mapping[str, object],
    processes: Mapping[str, object],
) -> Path:
    target_root = Path(run_dir).resolve()
    target_root.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(Path(executed_notebook), target_root / "executed.ipynb")
    notebook = nbformat.read(target_root / "executed.ipynb", as_version=4)
    (target_root / "summary.json").write_bytes(_json_bytes(dict(summary)))
    (target_root / "timings.json").write_bytes(_json_bytes(execution_timings(notebook)))
    (target_root / "processes.json").write_bytes(_json_bytes(dict(processes)))
    lines = [
        f"{sha256((target_root / name).read_bytes()).hexdigest()}  {name}"
        for name in ARTIFACT_FILES
    ]
    (target_root / "manifest.sha256").write_text(
        "\n".join(lines) + "\n", encoding="ascii", newline="\n"
    )
    return target_root


def write_failure_artifact(
    run_dir: Path,
    executed_notebook: Path,
    *,
    failure: BaseException,
) -> Path:
    """Persist a manifest-bound diagnostic bundle for a failed execution."""
    target_root = Path(run_dir).resolve()
    target_root.mkdir(parents=True, exist_ok=False)
    target_notebook = target_root / "executed.ipynb"
    shutil.copyfile(Path(executed_notebook), target_notebook)
    notebook = nbformat.read(target_notebook, as_version=4)
    timings = partial_execution_timings(notebook)
    processes = getattr(
        failure,
        _FAILURE_PROCESSES_ATTRIBUTE,
        {"owned_process_count": 0, "owned_processes": [], "reconciled": []},
    )
    summary = {
        "status": "FAIL",
        "failure_type": type(failure).__name__,
        "failing_cell_tag": _failing_cell_tag(notebook),
        "executed_code_cells": len(timings),
    }
    (target_root / "summary.json").write_bytes(_json_bytes(summary))
    (target_root / "timings.json").write_bytes(_json_bytes(timings))
    (target_root / "processes.json").write_bytes(_json_bytes(processes))
    lines = [
        f"{sha256((target_root / name).read_bytes()).hexdigest()}  {name}"
        for name in ARTIFACT_FILES
    ]
    (target_root / "manifest.sha256").write_text(
        "\n".join(lines) + "\n", encoding="ascii", newline="\n"
    )
    failure.add_note(f"notebook failure evidence: {target_root}")
    return target_root


def _verify_manifest(run_dir: Path) -> None:
    manifest = run_dir / "manifest.sha256"
    if not manifest.is_file():
        raise ProtocolError("live notebook manifest is missing")
    observed: dict[str, str] = {}
    for line in manifest.read_text(encoding="ascii").splitlines():
        parts = line.split("  ", 1)
        if len(parts) != 2:
            raise ProtocolError("live notebook manifest is invalid")
        observed[parts[1]] = parts[0]
    if set(observed) != set(ARTIFACT_FILES):
        raise ProtocolError("live notebook manifest file set is invalid")
    for name in ARTIFACT_FILES:
        path = run_dir / name
        if not path.is_file() or sha256(path.read_bytes()).hexdigest() != observed[name]:
            raise ProtocolError(f"live notebook manifest hash mismatch: {name}")


def verify_live_artifact(
    run_dir: Path,
    *,
    recompute_summary: Callable[[Path], dict[str, object]],
) -> dict[str, object]:
    root = Path(run_dir).resolve()
    _verify_manifest(root)
    recomputed = recompute_summary(root / "executed.ipynb")
    summary = json.loads((root / "summary.json").read_text(encoding="utf-8"))
    if summary != recomputed:
        raise ProtocolError("live notebook summary does not match executed notebook")
    timings = json.loads((root / "timings.json").read_text(encoding="utf-8"))
    if (
        not isinstance(timings, list)
        or len(timings) != recomputed.get("executed_code_cells")
    ):
        raise ProtocolError("live notebook timings are invalid")
    processes = json.loads((root / "processes.json").read_text(encoding="utf-8"))
    if (
        not isinstance(processes, dict)
        or processes.get("owned_process_count") != 0
        or processes.get("owned_processes") != []
    ):
        raise ProtocolError("live notebook owned process cleanup failed")
    return {**recomputed, "owned_process_count": 0}
