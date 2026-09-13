from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Literal, Mapping

from onec_runtime.errors import ProtocolError


EVIDENCE_FILES = (
    "environment.json",
    "observations.json",
    "cleanup.json",
    "summary.json",
)
EXPECTED_CALL_SEQUENCE = (
    "A:workspace.open",
    "A:runtime.ensure",
    "A:code.list",
    "A:code.get",
    "A:code.run",
    "A:workspace.variables",
    "A:value.size",
    "A:value.to_df",
    "A:python.inspect.frame",
    "A:python.run",
    "A:python.inspect.summary",
    "A:frontend.exit",
    "B:workspace.status",
    "B:python.variables",
    "B:python.inspect",
    "B:runtime.ensure",
    "B:runtime.restart",
    "B:value.describe.stale",
    "B:python.inspect.after_restart",
    "B:runtime.close",
    "owner:service.shutdown",
)
_UUID = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_BANNED_KEYS = {
    "pid",
    "create_time",
    "executable",
    "command",
    "token",
    "endpoint",
    "database",
    "infobase",
    "username",
    "source",
    "uuid",
}
_BANNED_TEXT = ("rdbg", "1cv8.1cd", "password", "пароль=")


def write_value_python_evidence(
    directory: str | Path,
    *,
    environment: Mapping[str, object],
    observations: Mapping[str, object],
    cleanup: Mapping[str, object],
) -> Path:
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=False)
    values = {
        "environment.json": dict(environment),
        "observations.json": dict(observations),
        "cleanup.json": dict(cleanup),
    }
    _privacy_check(values)
    summary = _calculate_summary(values["observations.json"], values["cleanup.json"])
    values["summary.json"] = summary
    for name in EVIDENCE_FILES:
        _write_json(root / name, values[name])
    manifest = {
        name: sha256((root / name).read_bytes()).hexdigest()
        for name in EVIDENCE_FILES
    }
    _write_json(root / "manifest.json", manifest)
    return root


def verify_value_python_evidence(
    directory: str | Path, *, expected_result: Literal["PASS", "FAIL"]
) -> dict[str, object]:
    if expected_result not in {"PASS", "FAIL"}:
        raise ValueError("expected_result must be PASS or FAIL")
    root = Path(directory)
    expected_files = {*EVIDENCE_FILES, "manifest.json"}
    observed_files = {item.name for item in root.iterdir() if item.is_file()}
    if observed_files != expected_files:
        raise ProtocolError("value/Python evidence file set is invalid")
    values = {name: _read_json(root / name) for name in EVIDENCE_FILES}
    manifest = _read_json(root / "manifest.json")
    if not isinstance(manifest, dict) or set(manifest) != set(EVIDENCE_FILES):
        raise ProtocolError("value/Python evidence manifest is invalid")
    for name in EVIDENCE_FILES:
        digest = manifest.get(name)
        if not isinstance(digest, str) or digest != sha256((root / name).read_bytes()).hexdigest():
            raise ProtocolError("value/Python evidence hash mismatch")
    _privacy_check(values)
    environment = values["environment.json"]
    if not isinstance(environment, dict) or environment != {
        "schema_version": 1,
        "attempt": 1,
        "platform_version": "8.3.27.2170",
        "notebook_source_sha256": environment.get("notebook_source_sha256"),
    } or not _hash(environment.get("notebook_source_sha256")):
        raise ProtocolError("value/Python environment evidence is invalid")
    calculated = _calculate_summary(
        values["observations.json"], values["cleanup.json"]
    )
    if values["summary.json"] != calculated:
        raise ProtocolError("value/Python summary is not independently reproducible")
    if calculated["result"] != expected_result:
        raise ProtocolError("value/Python evidence result does not match expectation")
    return calculated


def _calculate_summary(
    observations: object, cleanup: object
) -> dict[str, object]:
    if not isinstance(observations, dict) or not isinstance(cleanup, dict):
        raise ProtocolError("value/Python evidence root is invalid")
    timings = observations.get("timings_s")
    timing_gate = (
        isinstance(timings, dict)
        and set(timings) == {"main", "to_df", "python", "restart", "total"}
        and all(type(value) in {int, float} and 0 <= value <= 600 for value in timings.values())
        and timings["total"] >= max(timings.values())
    )
    gates = {
        "call_sequence": observations.get("calls") == list(EXPECTED_CALL_SEQUENCE),
        "main_completed": observations.get("main_state") == "completed",
        "bsl_variables": observations.get("bsl_variables")
        == ["bsl.АгентСкаляр", "bsl.АгентТаблица"],
        "table_size": _table_size_gate(observations.get("table_size")),
        "dataframe": _dataframe_gate(observations.get("dataframe")),
        "python_summary": _python_summary_gate(observations.get("python_summary")),
        "provenance": observations.get("provenance_linked") is True,
        "frontend_reconnect": observations.get("frontend_reconnect") is True,
        "restart_fences": observations.get("runtime_restarted") is True
        and observations.get("onec_proxy_stale") is True
        and observations.get("python_proxy_valid") is True,
        "timings": timing_gate,
        "cleanup": cleanup == {
            "owned_process_count": 0,
            "cleanup_errors": [],
            "roles_observed": ["dbgs", "mcp", "onec", "python", "service"],
        },
    }
    return {
        "schema_version": 1,
        "result": "PASS" if all(gates.values()) else "FAIL",
        "gates": gates,
    }


def _table_size_gate(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {"accuracy", "cost", "rows"}:
        return False
    if value.get("accuracy") == "exact" and value.get("cost") == "cheap":
        return type(value.get("rows")) is int and 1 <= value["rows"] <= 100
    return (
        value.get("accuracy") == "unknown"
        and value.get("cost") == "scan_required"
        and value.get("rows") is None
    )


def _dataframe_gate(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"rows", "columns", "refs"}
        and type(value.get("rows")) is int
        and 1 <= value["rows"] <= 100
        and type(value.get("columns")) is int
        and value["columns"] >= 5
        and value.get("refs") == "both"
    )


def _python_summary_gate(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"qualified_name", "rows", "columns"}
        and value.get("qualified_name") == "python.ИтогиПоОрганизации"
        and type(value.get("rows")) is int
        and value["rows"] >= 1
        and type(value.get("columns")) is int
        and value["columns"] == 2
    )


def _privacy_check(value: object) -> None:
    def walk(item: object) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or key.casefold() in _BANNED_KEYS:
                    raise ProtocolError("value/Python evidence contains a private key")
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)
        elif isinstance(item, str):
            folded = item.casefold()
            if _UUID.search(item) or any(marker in folded for marker in _BANNED_TEXT):
                raise ProtocolError("value/Python evidence contains private text")
    walk(value)


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProtocolError("value/Python evidence JSON is invalid") from error


def _hash(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


__all__ = [
    "EXPECTED_CALL_SEQUENCE",
    "verify_value_python_evidence",
    "write_value_python_evidence",
]
