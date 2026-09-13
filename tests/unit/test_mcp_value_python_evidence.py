from __future__ import annotations

import json
from pathlib import Path

import pytest
import nbformat

from integration.evidence.value_python_evidence import (
    EXPECTED_CALL_SEQUENCE,
    verify_value_python_evidence,
    write_value_python_evidence,
)
from onec_runtime.bsl.notebook_cells import split_notebook_cell
from onec_runtime.bsl.parser_target import PythonParserTarget
from onec_runtime.errors import ProtocolError


def _environment() -> dict[str, object]:
    return {
        "schema_version": 1,
        "attempt": 1,
        "platform_version": "8.3.27.2170",
        "notebook_source_sha256": "a" * 64,
    }


def _observations() -> dict[str, object]:
    return {
        "calls": list(EXPECTED_CALL_SEQUENCE),
        "main_state": "completed",
        "bsl_variables": ["bsl.АгентСкаляр", "bsl.АгентТаблица"],
        "table_size": {
            "accuracy": "unknown",
            "cost": "scan_required",
            "rows": None,
        },
        "dataframe": {"rows": 100, "columns": 5, "refs": "both"},
        "python_summary": {
            "qualified_name": "python.ИтогиПоОрганизации",
            "rows": 3,
            "columns": 2,
        },
        "provenance_linked": True,
        "frontend_reconnect": True,
        "runtime_restarted": True,
        "onec_proxy_stale": True,
        "python_proxy_valid": True,
        "timings_s": {
            "main": 3.0,
            "to_df": 2.0,
            "python": 0.1,
            "restart": 4.0,
            "total": 10.0,
        },
    }


def _cleanup() -> dict[str, object]:
    return {
        "owned_process_count": 0,
        "cleanup_errors": [],
        "roles_observed": ["dbgs", "mcp", "onec", "python", "service"],
    }


def test_saved_value_python_notebook_hash_and_mixed_cell() -> None:
    notebook_path = (
        Path(__file__).resolve().parents[2]
        / "tests"
        / "fixtures"
        / "notebooks"
        / "mcp-zup-value-python-acceptance.ipynb"
    )
    notebook = nbformat.read(notebook_path, as_version=4)
    assert len(notebook.cells) == 1
    cell = notebook.cells[0]
    import hashlib

    source = str(cell.source)
    assert hashlib.sha256(source.encode("utf-8")).hexdigest() == (
        cell.metadata["onec_runtime"]["source_sha256"]
    )
    split = split_notebook_cell(PythonParserTarget.from_generated(), source)
    assert [item.public_path for item in split.exports] == [
        "СоздатьАгентТаблицу"
    ]


def _rehash(directory: Path) -> None:
    import hashlib

    manifest = {
        name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
        for name in ("environment.json", "observations.json", "cleanup.json", "summary.json")
    }
    (directory / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def test_value_python_evidence_verifies_independent_pass(tmp_path: Path) -> None:
    run_dir = write_value_python_evidence(
        tmp_path / "run",
        environment=_environment(),
        observations=_observations(),
        cleanup=_cleanup(),
    )

    verified = verify_value_python_evidence(run_dir, expected_result="PASS")

    assert verified["result"] == "PASS"
    assert all(verified["gates"].values())


def test_value_python_evidence_rejects_rehashed_forged_pass(tmp_path: Path) -> None:
    run_dir = write_value_python_evidence(
        tmp_path / "run",
        environment=_environment(),
        observations=_observations(),
        cleanup=_cleanup(),
    )
    observations = json.loads((run_dir / "observations.json").read_text(encoding="utf-8"))
    observations["frontend_reconnect"] = False
    (run_dir / "observations.json").write_text(
        json.dumps(observations, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    _rehash(run_dir)

    with pytest.raises(ProtocolError, match="independently reproducible"):
        verify_value_python_evidence(run_dir, expected_result="PASS")


def test_value_python_evidence_fail_closes_cleanup_leak(tmp_path: Path) -> None:
    cleanup = _cleanup()
    cleanup["owned_process_count"] = 1
    run_dir = write_value_python_evidence(
        tmp_path / "run",
        environment=_environment(),
        observations=_observations(),
        cleanup=cleanup,
    )

    assert verify_value_python_evidence(run_dir, expected_result="FAIL")["result"] == "FAIL"
    with pytest.raises(ProtocolError, match="does not match expectation"):
        verify_value_python_evidence(run_dir, expected_result="PASS")


@pytest.mark.parametrize(
    "private_value",
    [
        {"pid": 123},
        {"detail": "12345678-1234-1234-1234-123456789abc"},
        {"detail": "RDBG transcript"},
    ],
)
def test_value_python_evidence_rejects_private_public_data(
    tmp_path: Path, private_value: dict[str, object]
) -> None:
    observations = _observations()
    observations["private"] = private_value

    with pytest.raises(ProtocolError, match="private"):
        write_value_python_evidence(
            tmp_path / "run",
            environment=_environment(),
            observations=observations,
            cleanup=_cleanup(),
        )
