"""Publication-safe evidence and independent verification for live MCP acceptance."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from enum import Enum
from hashlib import sha256
import hmac
import json
from math import isfinite
from pathlib import Path
import re


EXPECTED_MESSAGES = (
    "MCP_ACCEPTANCE_20260816_01",
    "MCP_ACCEPTANCE_20260816_02",
    "MCP_ACCEPTANCE_20260816_03",
)
EXPECTED_CALL_SEQUENCE = (
    "A:workspace.open",
    "A:runtime.ensure",
    "A:code.list",
    "A:code.get",
    "A:code.run",
    "A:frontend.exit",
    "B:workspace.open",
    "B:runtime.ensure",
    "B:runtime.status",
    "B:operation.status",
    "B:operation.wait",
    "B:operation.status",
    "B:operation.output",
    "B:operation.result",
    "B:code.history",
    "B:runtime.close",
    "owner:service.shutdown",
)
_EXPECTED_PHASES = (
    "service_start",
    "runtime_ensure",
    "code_run",
    "frontend_reconnect",
    "operation_recovery",
    "runtime_close",
    "owned_cleanup",
)
_NEGATIVE_ASSERTIONS = {
    "no_infobase_path",
    "no_pid",
    "no_process_command",
    "no_raw_platform_uuid",
    "no_raw_rdbg",
    "no_saved_source",
    "no_token",
    "no_username",
}
_FAIL_BOUNDARY = "runtime.ensure.sanitized_failure"
_FAIL_TYPE = "sanitized_service_failure"
_TERMINAL_FAIL_BOUNDARY = "main.operation_terminal"
_TERMINAL_FAIL_TYPE = "unexpected_operation_terminal_state"
_TERMINAL_FAIL_PHASES = (
    "service_start",
    "runtime_ensure",
    "code_run",
    "frontend_reconnect",
    "owned_cleanup",
)
_BUNDLE_FILES = (
    "environment.json",
    "observations.json",
    "cleanup.json",
    "summary.json",
)
_HASH = re.compile(r"^[0-9a-f]{64}$")
_UUID = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b"
)
_FORBIDDEN_KEYS = {
    "cmdline",
    "command",
    "create_time",
    "infobase_path",
    "password",
    "pid",
    "platform_uuid",
    "raw_pid",
    "source",
    "token",
    "username",
}


class LiveEvidenceError(RuntimeError):
    """The public live-evidence bundle does not independently prove acceptance."""


class ExpectedLiveResult(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"


def build_mcp_zup_fail_observations(
    *,
    attempt: int,
    completed_call_sequence: Sequence[str],
    attempt_duration_s: float,
    configured_control_timeout_s: float,
    static_check_passed: bool,
    source_sha256: str,
    artifact_sha256: str,
    failure_category: str,
    failure_state_changed: str,
    failure_safe_to_retry: str,
    runtime_ensure_duration_s: float | None,
    local_timeout_origin_observed: bool,
) -> dict[str, object]:
    """Build an allowlisted FAIL record without promoting hypotheses to facts."""
    if type(attempt) is not int or attempt < 1:
        raise ValueError("attempt must be a positive integer")
    if list(completed_call_sequence) != ["A:workspace.open"]:
        raise ValueError("FAIL call sequence must stop after A:workspace.open")
    for value, name in (
        (attempt_duration_s, "attempt duration"),
        (configured_control_timeout_s, "configured control timeout"),
    ):
        if type(value) not in {int, float} or not isfinite(float(value)) or value <= 0:
            raise ValueError(f"{name} must be a positive finite number")
    if configured_control_timeout_s > 180.0:
        raise ValueError("configured control timeout exceeds the frontend limit")
    if (
        failure_category,
        failure_state_changed,
        failure_safe_to_retry,
    ) != ("platform_failure", "unknown", "after_status_check"):
        raise ValueError("FAIL service failure tuple is invalid")
    if type(local_timeout_origin_observed) is not bool:
        raise TypeError("local timeout origin observation must be boolean")
    if runtime_ensure_duration_s is not None and (
        type(runtime_ensure_duration_s) not in {int, float}
        or not isfinite(float(runtime_ensure_duration_s))
        or runtime_ensure_duration_s < 0
    ):
        raise ValueError("runtime.ensure duration must be finite and non-negative")
    if local_timeout_origin_observed and runtime_ensure_duration_s is None:
        raise ValueError("a proven local timeout requires runtime.ensure timing")
    _require_hash(source_sha256, "source")
    _require_hash(artifact_sha256, "artifact")
    return {
        "schema": "onec-mcp-zup-main-observations-v1",
        "attempt": attempt,
        "result": "FAIL",
        "failure_boundary": _FAIL_BOUNDARY,
        "failure_type": _FAIL_TYPE,
        "failure": {
            "category": failure_category,
            "state_changed": failure_state_changed,
            "safe_to_retry": failure_safe_to_retry,
        },
        "completed_call_sequence": ["A:workspace.open"],
        "boundary_evidence": {
            "target_extension_static_check": static_check_passed is True,
            "guard_false_true_true": "not_observed",
            "server_entry": "not_observed",
            "server_service": "not_observed",
        },
        "diagnosis": {
            "configured_control_timeout_s": float(configured_control_timeout_s),
            "local_timeout_origin_observed": local_timeout_origin_observed,
            "timeout_cause": (
                "proven_local_control_timeout"
                if local_timeout_origin_observed
                else (
                    "source_level_hypothesis_only"
                    if attempt == 1
                    else "not_observed"
                )
            ),
        },
        "code_artifact_preflight": {
            "cell_id": "mcp-zup-main-acceptance",
            "source_sha256": source_sha256,
            "artifact_sha256": artifact_sha256,
        },
        "timings": [
            {
                "phase": "attempt_total_including_setup_and_cleanup",
                "duration_s": round(float(attempt_duration_s), 6),
            }
        ],
        "runtime_ensure_measurement": (
            {"observed": False}
            if runtime_ensure_duration_s is None
            else {
                "observed": True,
                "duration_s": round(float(runtime_ensure_duration_s), 6),
            }
        ),
        "negative_assertions": {name: True for name in sorted(_NEGATIVE_ASSERTIONS)},
    }


def build_mcp_zup_terminal_fail_observations(
    *,
    attempt: int,
    completed_call_sequence: Sequence[str],
    configured_control_timeout_s: float,
    source_sha256: str,
    artifact_sha256: str,
    terminal_state: str,
    result_present: bool,
    messages: Sequence[str],
    timings: Sequence[tuple[str, float]],
) -> dict[str, object]:
    """Build the late-FAIL projection after a proven frontend reconnect."""
    if type(attempt) is not int or attempt < 1:
        raise ValueError("attempt must be a positive integer")
    if list(completed_call_sequence) != list(EXPECTED_CALL_SEQUENCE[:-1]):
        raise ValueError("terminal FAIL call sequence is invalid")
    if (
        type(configured_control_timeout_s) not in {int, float}
        or not isfinite(float(configured_control_timeout_s))
        or not 0 < configured_control_timeout_s <= 180.0
    ):
        raise ValueError("configured control timeout is invalid")
    if terminal_state != "unknown" or result_present is not False or list(messages):
        raise ValueError("terminal FAIL operation facts are invalid")
    if [phase for phase, _ in timings] != list(_TERMINAL_FAIL_PHASES):
        raise ValueError("terminal FAIL timing phases are invalid")
    normalized_timings: list[dict[str, object]] = []
    for phase, duration in timings:
        if (
            type(duration) not in {int, float}
            or not isfinite(float(duration))
            or duration < 0
        ):
            raise ValueError("terminal FAIL timing duration is invalid")
        normalized_timings.append(
            {"phase": phase, "duration_s": round(float(duration), 6)}
        )
    _require_hash(source_sha256, "source")
    _require_hash(artifact_sha256, "artifact")
    return {
        "schema": "onec-mcp-zup-main-observations-v1",
        "attempt": attempt,
        "result": "FAIL",
        "failure_boundary": _TERMINAL_FAIL_BOUNDARY,
        "failure_type": _TERMINAL_FAIL_TYPE,
        "completed_call_sequence": list(completed_call_sequence),
        "service": {
            "control_timeout_s": float(configured_control_timeout_s),
            "frontend_reconnect": True,
        },
        "code_artifact_preflight": {
            "cell_id": "mcp-zup-main-acceptance",
            "source_sha256": source_sha256,
            "artifact_sha256": artifact_sha256,
        },
        "operation": {
            "terminal_state": terminal_state,
            "messages": list(messages),
            "result_present": result_present,
        },
        "timings": normalized_timings,
        "negative_assertions": {
            name: True for name in sorted(_NEGATIVE_ASSERTIONS)
        },
    }


def evidence_identity(salt: str, *parts: str) -> str:
    """Hash a private identity with a public run-specific salt and length framing."""
    if not isinstance(salt, str) or not salt:
        raise ValueError("salt must be a non-empty string")
    digest = sha256()
    for part in (salt, *parts):
        if not isinstance(part, str):
            raise TypeError("identity parts must be strings")
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def write_mcp_zup_evidence(
    run_dir: Path,
    *,
    environment: Mapping[str, object],
    observations: Mapping[str, object],
    cleanup: Mapping[str, object],
) -> Path:
    """Write canonical raw public facts plus a convenience summary and manifest."""
    if run_dir.exists():
        if not run_dir.is_dir() or any(run_dir.iterdir()):
            raise FileExistsError("evidence directory is not empty")
    else:
        run_dir.mkdir(parents=True, exist_ok=False)
    values = {
        "environment.json": environment,
        "observations.json": observations,
        "cleanup.json": cleanup,
        "summary.json": _derive_summary(environment, observations, cleanup),
    }
    for name, value in values.items():
        _write_json(run_dir / name, value)
    lines = [
        f"{sha256((run_dir / name).read_bytes()).hexdigest()}  {name}"
        for name in _BUNDLE_FILES
    ]
    (run_dir / "manifest.sha256").write_text(
        "\n".join(lines) + "\n", encoding="ascii", newline="\n"
    )
    return run_dir


def verify_mcp_zup_evidence(
    run_dir: Path,
    *,
    expected_result: ExpectedLiveResult,
) -> dict[str, object]:
    """Validate a bundle against a caller-supplied expected live result."""
    if type(expected_result) is not ExpectedLiveResult:
        raise TypeError("expected_result must be ExpectedLiveResult")
    _verify_manifest(run_dir)
    environment = _read_object(run_dir / "environment.json")
    observations = _read_object(run_dir / "observations.json")
    cleanup = _read_object(run_dir / "cleanup.json")
    for value in (environment, observations, cleanup):
        _assert_public(value)
    _verify_environment(environment)
    result = observations.get("result")
    if result != expected_result.value:
        raise LiveEvidenceError("observations do not match the trusted expected result")
    if expected_result is ExpectedLiveResult.PASS:
        _verify_observations(observations)
        expected_cleanup_roles = ["service", "mcp_a", "mcp_b", "designer", "dbgs", "onec"]
    else:
        _verify_fail_observations(observations)
        expected_cleanup_roles = (
            ["service", "mcp_a", "mcp_b", "designer", "dbgs", "onec"]
            if observations.get("failure_boundary") == _TERMINAL_FAIL_BOUNDARY
            else ["service", "mcp_a", "designer", "dbgs", "onec"]
        )
    _verify_cleanup(cleanup, expected_roles=expected_cleanup_roles)
    if environment["attempt"] != observations["attempt"] or cleanup["attempt"] != observations["attempt"]:
        raise LiveEvidenceError("attempt identity changed across evidence files")
    recomputed = _derive_summary(environment, observations, cleanup)
    summary = _read_object(run_dir / "summary.json")
    if summary != recomputed:
        raise LiveEvidenceError("summary does not match independently recomputed gates")
    return recomputed


def _derive_summary(
    environment: Mapping[str, object],
    observations: Mapping[str, object],
    cleanup: Mapping[str, object],
) -> dict[str, object]:
    platform = _object(environment.get("platform"), "platform")
    raw_operation = observations.get("operation")
    operation = raw_operation if isinstance(raw_operation, Mapping) else {}
    return {
        "status": observations.get("result"),
        "attempt": observations.get("attempt"),
        "platform_version": platform.get("version"),
        "profile": platform.get("profile"),
        "terminal_state": operation.get("terminal_state"),
        "message_count": len(operation.get("messages", ()))
        if isinstance(operation.get("messages"), Sequence)
        and not isinstance(operation.get("messages"), str)
        else 0,
        "result_present": operation.get("result_present"),
        "frontend_reconnect": _frontend_reconnect(observations),
        "owned_process_count": 0 if cleanup.get("all_absent") is True else None,
    }


def _verify_environment(value: Mapping[str, object]) -> None:
    _exact_keys(value, {"schema", "attempt", "platform", "database"}, "environment")
    if (
        value["schema"] != "onec-mcp-zup-main-environment-v1"
        or type(value["attempt"]) is not int
        or value["attempt"] < 1
    ):
        raise LiveEvidenceError("environment schema or attempt is invalid")
    platform = _object(value["platform"], "platform")
    _exact_keys(
        platform,
        {"version", "profile", "bin_identity_sha256", "executables_sha256"},
        "platform",
    )
    if platform["version"] != "8.3.27.2170" or platform["profile"] != "zup":
        raise LiveEvidenceError("platform version or profile is invalid")
    _require_hash(platform["bin_identity_sha256"], "platform bin identity")
    executables = _object(platform["executables_sha256"], "platform executables")
    _exact_keys(executables, {"1cv8c.exe", "dbgs.exe"}, "platform executables")
    for digest in executables.values():
        _require_hash(digest, "platform executable")
    database = _object(value["database"], "database")
    _exact_keys(database, {"target_identity_sha256", "mutation_performed"}, "database")
    _require_hash(database["target_identity_sha256"], "database identity")
    if database["mutation_performed"] is not False:
        raise LiveEvidenceError("database mutation gate failed")


def _verify_observations(value: Mapping[str, object]) -> None:
    if (
        value.get("schema") != "onec-mcp-zup-main-observations-v1"
        or type(value.get("attempt")) is not int
        or value["attempt"] < 1
    ):
        raise LiveEvidenceError("observations schema or attempt is invalid")
    if value.get("result") != "PASS":
        raise LiveEvidenceError("live result is not PASS")
    _exact_keys(
        value,
        {
            "schema",
            "attempt",
            "result",
            "service",
            "code",
            "runtime",
            "operation",
            "timings",
            "negative_assertions",
        },
        "public evidence observations",
    )
    service = _object(value["service"], "service")
    _exact_keys(
        service,
        {
            "maximum_mode",
            "service_identity_sha256",
            "frontend_a_identity_sha256",
            "frontend_b_identity_sha256",
            "separate_processes",
            "service_alive_after_a_exit",
            "onec_alive_after_a_exit",
            "control_timeout_s",
            "local_timeout_origin_observed",
        },
        "service",
    )
    for name in (
        "service_identity_sha256",
        "frontend_a_identity_sha256",
        "frontend_b_identity_sha256",
    ):
        _require_hash(service[name], name)
    if service["maximum_mode"] != "experiment":
        raise LiveEvidenceError("maximum service mode is invalid")
    if (
        type(service["control_timeout_s"]) not in {int, float}
        or not isfinite(float(service["control_timeout_s"]))
        or not 0 < service["control_timeout_s"] <= 180.0
        or service["local_timeout_origin_observed"] is not False
    ):
        raise LiveEvidenceError("control timeout contract is invalid")
    if any(
        service[name] is not True
        for name in (
            "separate_processes",
            "service_alive_after_a_exit",
            "onec_alive_after_a_exit",
        )
    ):
        raise LiveEvidenceError("frontend separation or survival gate failed")
    if len(
        {
            service["service_identity_sha256"],
            service["frontend_a_identity_sha256"],
            service["frontend_b_identity_sha256"],
        }
    ) != 3:
        raise LiveEvidenceError("service and frontend process identities are not separate")

    code = _object(value["code"], "code")
    _exact_keys(
        code,
        {
            "cell_id",
            "language",
            "mode",
            "revision",
            "source_sha256",
            "document_sha256",
            "artifact_sha256",
        },
        "code",
    )
    if (
        code["cell_id"] != "mcp-zup-main-acceptance"
        or code["language"] != "bsl"
        or code["mode"] != "main"
        or type(code["revision"]) is not int
        or code["revision"] < 1
    ):
        raise LiveEvidenceError("code revision contract is invalid")
    for name in ("source_sha256", "document_sha256", "artifact_sha256"):
        _require_hash(code[name], name)

    runtime = _object(value["runtime"], "runtime")
    _exact_keys(
        runtime,
        {"a_identity_sha256", "b_identity_sha256", "a_generation", "b_generation"},
        "runtime",
    )
    for name in ("a_identity_sha256", "b_identity_sha256"):
        _require_hash(runtime[name], name)
    if not hmac.compare_digest(str(runtime["a_identity_sha256"]), str(runtime["b_identity_sha256"])):
        raise LiveEvidenceError("runtime reconnect identity changed")
    if (
        type(runtime["a_generation"]) is not int
        or runtime["a_generation"] < 1
        or runtime["b_generation"] != runtime["a_generation"]
    ):
        raise LiveEvidenceError("runtime generation changed across frontend reconnect")

    operation = _object(value["operation"], "operation")
    _exact_keys(
        operation,
        {
            "identity_sha256",
            "history_identity_sha256",
            "call_sequence",
            "observed_states",
            "terminal_state",
            "messages",
            "result_present",
            "result_access",
            "result_call_ok",
            "result_failure_category",
            "no_raw_result",
        },
        "operation",
    )
    _require_hash(operation["identity_sha256"], "operation identity")
    _require_hash(operation["history_identity_sha256"], "operation history identity")
    if not hmac.compare_digest(
        str(operation["identity_sha256"]), str(operation["history_identity_sha256"])
    ):
        raise LiveEvidenceError("operation history identity changed")
    if operation["call_sequence"] != list(EXPECTED_CALL_SEQUENCE):
        raise LiveEvidenceError("MCP call sequence is invalid")
    _verify_states(operation["observed_states"])
    if operation["terminal_state"] != "completed":
        raise LiveEvidenceError("operation terminal state is not completed")
    if operation["messages"] != list(EXPECTED_MESSAGES):
        raise LiveEvidenceError("ordered operation messages are invalid")
    if (
        operation["result_present"] is not True
        or operation["result_access"] != "unavailable_until_value_proxies"
        or operation["result_call_ok"] is not False
        or operation["result_failure_category"] != "unsupported"
        or operation["no_raw_result"] is not True
    ):
        raise LiveEvidenceError("operation result redaction contract is invalid")

    timings = value["timings"]
    if not isinstance(timings, list) or [item.get("phase") for item in timings if isinstance(item, Mapping)] != list(_EXPECTED_PHASES):
        raise LiveEvidenceError("phase timing order is invalid")
    for item in timings:
        if not isinstance(item, Mapping) or set(item) != {"phase", "duration_s"}:
            raise LiveEvidenceError("phase timing schema is invalid")
        duration = item["duration_s"]
        if type(duration) not in {int, float} or not isfinite(float(duration)) or duration < 0:
            raise LiveEvidenceError("phase timing duration is invalid")

    negatives = _object(value["negative_assertions"], "negative assertions")
    _exact_keys(negatives, _NEGATIVE_ASSERTIONS, "negative assertions")
    if any(negatives[name] is not True for name in _NEGATIVE_ASSERTIONS):
        raise LiveEvidenceError("negative assertion gate failed")


def _verify_fail_observations(value: Mapping[str, object]) -> None:
    if value.get("failure_boundary") == _TERMINAL_FAIL_BOUNDARY:
        _verify_terminal_fail_observations(value)
        return
    _exact_keys(
        value,
        {
            "schema",
            "attempt",
            "result",
            "failure_boundary",
            "failure_type",
            "failure",
            "completed_call_sequence",
            "boundary_evidence",
            "diagnosis",
            "code_artifact_preflight",
            "timings",
            "runtime_ensure_measurement",
            "negative_assertions",
        },
        "live result FAIL observations",
    )
    if (
        value["schema"] != "onec-mcp-zup-main-observations-v1"
        or type(value["attempt"]) is not int
        or value["attempt"] < 1
        or value["result"] != "FAIL"
    ):
        raise LiveEvidenceError("live result FAIL observations schema is invalid")
    if value["failure_boundary"] != _FAIL_BOUNDARY or value["failure_type"] != _FAIL_TYPE:
        raise LiveEvidenceError("FAIL boundary diagnosis is invalid")
    failure = _object(value["failure"], "FAIL service failure")
    if dict(failure) != {
        "category": "platform_failure",
        "state_changed": "unknown",
        "safe_to_retry": "after_status_check",
    }:
        raise LiveEvidenceError("FAIL service failure tuple is invalid")
    if value["completed_call_sequence"] != ["A:workspace.open"]:
        raise LiveEvidenceError("FAIL completed call sequence is invalid")
    boundary = _object(value["boundary_evidence"], "FAIL boundary evidence")
    expected_boundary = {
        "target_extension_static_check": True,
        "guard_false_true_true": "not_observed",
        "server_entry": "not_observed",
        "server_service": "not_observed",
    }
    if dict(boundary) != expected_boundary:
        raise LiveEvidenceError("FAIL boundary evidence is invalid")
    diagnosis = _object(value["diagnosis"], "FAIL diagnosis")
    _exact_keys(
        diagnosis,
        {
            "configured_control_timeout_s",
            "local_timeout_origin_observed",
            "timeout_cause",
        },
        "FAIL diagnosis",
    )
    measurement = _object(
        value["runtime_ensure_measurement"],
        "FAIL runtime.ensure measurement",
    )
    observed = diagnosis["local_timeout_origin_observed"]
    if observed is False:
        expected_cause = (
            "source_level_hypothesis_only"
            if value["attempt"] == 1
            else "not_observed"
        )
        if dict(diagnosis) != {
            "configured_control_timeout_s": diagnosis["configured_control_timeout_s"],
            "local_timeout_origin_observed": False,
            "timeout_cause": expected_cause,
        }:
            raise LiveEvidenceError("FAIL boundary diagnosis is invalid")
        if (
            type(diagnosis["configured_control_timeout_s"]) not in {int, float}
            or not isfinite(float(diagnosis["configured_control_timeout_s"]))
            or not 0 < diagnosis["configured_control_timeout_s"] <= 180.0
        ):
            raise LiveEvidenceError("FAIL boundary diagnosis is invalid")
        if dict(measurement) != {"observed": False} and not (
            set(measurement) == {"observed", "duration_s"}
            and measurement["observed"] is True
            and type(measurement["duration_s"]) in {int, float}
            and isfinite(float(measurement["duration_s"]))
            and measurement["duration_s"] >= 0
        ):
            raise LiveEvidenceError("FAIL boundary diagnosis is invalid")
    elif observed is True:
        if (
            type(diagnosis["configured_control_timeout_s"]) not in {int, float}
            or not isfinite(float(diagnosis["configured_control_timeout_s"]))
            or not 0 < diagnosis["configured_control_timeout_s"] <= 180.0
            or diagnosis["timeout_cause"] != "proven_local_control_timeout"
            or set(measurement) != {"observed", "duration_s"}
            or measurement["observed"] is not True
            or type(measurement["duration_s"]) not in {int, float}
            or not isfinite(float(measurement["duration_s"]))
            or measurement["duration_s"] < 0
        ):
            raise LiveEvidenceError("FAIL boundary diagnosis is invalid")
    else:
        raise LiveEvidenceError("FAIL boundary diagnosis is invalid")
    preflight = _object(value["code_artifact_preflight"], "FAIL code artifact preflight")
    _exact_keys(
        preflight,
        {"cell_id", "source_sha256", "artifact_sha256"},
        "FAIL code artifact preflight",
    )
    if preflight["cell_id"] != "mcp-zup-main-acceptance":
        raise LiveEvidenceError("FAIL code artifact preflight cell is invalid")
    _require_hash(preflight["source_sha256"], "FAIL source")
    _require_hash(preflight["artifact_sha256"], "FAIL artifact")
    timings = value["timings"]
    if not isinstance(timings, list) or len(timings) != 1:
        raise LiveEvidenceError("FAIL timing schema is invalid")
    timing = timings[0]
    if not isinstance(timing, Mapping) or set(timing) != {"phase", "duration_s"}:
        raise LiveEvidenceError("FAIL timing schema is invalid")
    duration = timing["duration_s"]
    if (
        timing["phase"] != "attempt_total_including_setup_and_cleanup"
        or type(duration) not in {int, float}
        or not isfinite(float(duration))
        or duration <= 0
    ):
        raise LiveEvidenceError("FAIL total attempt timing is invalid")
    negatives = _object(value["negative_assertions"], "negative assertions")
    _exact_keys(negatives, _NEGATIVE_ASSERTIONS, "negative assertions")
    if any(negatives[name] is not True for name in _NEGATIVE_ASSERTIONS):
        raise LiveEvidenceError("negative assertion gate failed")


def _verify_terminal_fail_observations(value: Mapping[str, object]) -> None:
    _exact_keys(
        value,
        {
            "schema",
            "attempt",
            "result",
            "failure_boundary",
            "failure_type",
            "completed_call_sequence",
            "service",
            "code_artifact_preflight",
            "operation",
            "timings",
            "negative_assertions",
        },
        "terminal FAIL observations",
    )
    if (
        value["schema"] != "onec-mcp-zup-main-observations-v1"
        or type(value["attempt"]) is not int
        or value["attempt"] < 1
        or value["result"] != "FAIL"
        or value["failure_boundary"] != _TERMINAL_FAIL_BOUNDARY
        or value["failure_type"] != _TERMINAL_FAIL_TYPE
    ):
        raise LiveEvidenceError("terminal FAIL observations schema is invalid")
    if value["completed_call_sequence"] != list(EXPECTED_CALL_SEQUENCE[:-1]):
        raise LiveEvidenceError("terminal FAIL completed call sequence is invalid")
    service = _object(value["service"], "terminal FAIL service")
    _exact_keys(service, {"control_timeout_s", "frontend_reconnect"}, "terminal FAIL service")
    timeout = service["control_timeout_s"]
    if (
        type(timeout) not in {int, float}
        or not isfinite(float(timeout))
        or not 0 < timeout <= 180.0
        or service["frontend_reconnect"] is not True
    ):
        raise LiveEvidenceError("terminal FAIL frontend reconnect is invalid")
    preflight = _object(value["code_artifact_preflight"], "terminal FAIL code")
    _exact_keys(
        preflight,
        {"cell_id", "source_sha256", "artifact_sha256"},
        "terminal FAIL code",
    )
    if preflight["cell_id"] != "mcp-zup-main-acceptance":
        raise LiveEvidenceError("terminal FAIL code cell is invalid")
    _require_hash(preflight["source_sha256"], "terminal FAIL source")
    _require_hash(preflight["artifact_sha256"], "terminal FAIL artifact")
    operation = _object(value["operation"], "terminal FAIL operation")
    _exact_keys(
        operation,
        {"terminal_state", "messages", "result_present"},
        "terminal FAIL operation",
    )
    if dict(operation) != {
        "terminal_state": "unknown",
        "messages": [],
        "result_present": False,
    }:
        raise LiveEvidenceError("terminal FAIL operation terminal facts are invalid")
    timings = value["timings"]
    if (
        not isinstance(timings, list)
        or [item.get("phase") for item in timings if isinstance(item, Mapping)]
        != list(_TERMINAL_FAIL_PHASES)
    ):
        raise LiveEvidenceError("terminal FAIL timing phases are invalid")
    for item in timings:
        if not isinstance(item, Mapping) or set(item) != {"phase", "duration_s"}:
            raise LiveEvidenceError("terminal FAIL timing schema is invalid")
        duration = item["duration_s"]
        if (
            type(duration) not in {int, float}
            or not isfinite(float(duration))
            or duration < 0
        ):
            raise LiveEvidenceError("terminal FAIL timing duration is invalid")
    negatives = _object(value["negative_assertions"], "negative assertions")
    _exact_keys(negatives, _NEGATIVE_ASSERTIONS, "negative assertions")
    if any(negatives[name] is not True for name in _NEGATIVE_ASSERTIONS):
        raise LiveEvidenceError("negative assertion gate failed")


def _verify_states(value: object) -> None:
    if not isinstance(value, list) or not value:
        raise LiveEvidenceError("operation state sequence is missing")
    order = {"queued": 0, "running": 1, "completed": 2}
    try:
        ranks = [order[item] for item in value]
    except (KeyError, TypeError):
        raise LiveEvidenceError("operation state sequence is invalid") from None
    if ranks != sorted(ranks) or value[-1] != "completed":
        raise LiveEvidenceError("operation state sequence is invalid")


def _verify_cleanup(value: Mapping[str, object], *, expected_roles: list[str]) -> None:
    _exact_keys(
        value,
        {"schema", "attempt", "all_absent", "owned_cleanup_error_count", "identities"},
        "cleanup",
    )
    if (
        value["schema"] != "onec-mcp-zup-main-cleanup-v1"
        or type(value["attempt"]) is not int
        or value["attempt"] < 1
    ):
        raise LiveEvidenceError("cleanup schema or attempt is invalid")
    if value["all_absent"] is not True or value["owned_cleanup_error_count"] != 0:
        raise LiveEvidenceError("owned process cleanup gate failed")
    identities = value["identities"]
    if not isinstance(identities, list):
        raise LiveEvidenceError("cleanup identities are invalid")
    roles = [item.get("role") for item in identities if isinstance(item, Mapping)]
    if len(roles) != len(expected_roles) or set(roles) != set(expected_roles):
        raise LiveEvidenceError("cleanup identity roles are invalid")
    seen: set[str] = set()
    for item in identities:
        if not isinstance(item, Mapping):
            raise LiveEvidenceError("cleanup identity is invalid")
        _exact_keys(item, {"role", "identity_sha256", "absent_after_cleanup"}, "cleanup identity")
        _require_hash(item["identity_sha256"], "cleanup identity")
        if item["identity_sha256"] in seen or item["absent_after_cleanup"] is not True:
            raise LiveEvidenceError("owned process cleanup identity gate failed")
        seen.add(str(item["identity_sha256"]))


def _frontend_reconnect(observations: Mapping[str, object]) -> bool:
    if observations.get("failure_boundary") == _TERMINAL_FAIL_BOUNDARY:
        service = observations.get("service")
        return bool(
            isinstance(service, Mapping)
            and service.get("frontend_reconnect") is True
            and observations.get("completed_call_sequence")
            == list(EXPECTED_CALL_SEQUENCE[:-1])
        )
    try:
        service = _object(observations["service"], "service")
        runtime = _object(observations["runtime"], "runtime")
        operation = _object(observations["operation"], "operation")
        return bool(
            service.get("separate_processes") is True
            and service.get("service_alive_after_a_exit") is True
            and service.get("onec_alive_after_a_exit") is True
            and runtime.get("a_identity_sha256") == runtime.get("b_identity_sha256")
            and runtime.get("a_generation") == runtime.get("b_generation")
            and operation.get("identity_sha256") == operation.get("history_identity_sha256")
        )
    except (KeyError, LiveEvidenceError):
        return False


def _assert_public(value: object, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or key.casefold() in _FORBIDDEN_KEYS:
                raise LiveEvidenceError(f"public evidence contains a forbidden field at {path}")
            _assert_public(item, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _assert_public(item, f"{path}[{index}]")
        return
    if isinstance(value, str) and _UUID.search(value):
        raise LiveEvidenceError(f"public evidence contains a raw UUID at {path}")


def _verify_manifest(run_dir: Path) -> None:
    path = run_dir / "manifest.sha256"
    if not path.is_file():
        raise LiveEvidenceError("manifest is missing")
    observed: dict[str, str] = {}
    lines = path.read_text(encoding="ascii").splitlines()
    if len(lines) != len(_BUNDLE_FILES):
        raise LiveEvidenceError("manifest is invalid")
    for line in lines:
        parts = line.split("  ", 1)
        if len(parts) != 2 or not _HASH.fullmatch(parts[0]):
            raise LiveEvidenceError("manifest is invalid")
        if parts[1] in observed:
            raise LiveEvidenceError("manifest is invalid")
        observed[parts[1]] = parts[0]
    if set(observed) != set(_BUNDLE_FILES):
        raise LiveEvidenceError("manifest file set is invalid")
    for name in _BUNDLE_FILES:
        candidate = run_dir / name
        if not candidate.is_file() or not hmac.compare_digest(
            sha256(candidate.read_bytes()).hexdigest(), observed[name]
        ):
            raise LiveEvidenceError("manifest hash mismatch")


def _read_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LiveEvidenceError(f"invalid evidence file: {path.name}") from error
    if not isinstance(value, dict):
        raise LiveEvidenceError(f"evidence file is not an object: {path.name}")
    return value


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _object(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise LiveEvidenceError(f"{name} must be an object")
    return value


def _exact_keys(value: Mapping[str, object], expected: set[str], name: str) -> None:
    if set(value) != expected:
        raise LiveEvidenceError(f"{name} schema is invalid")


def _require_hash(value: object, name: str) -> None:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise LiveEvidenceError(f"{name} hash is invalid")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="onec-runtime-verify-mcp-zup-evidence")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument(
        "--expected-result",
        choices=[item.value for item in ExpectedLiveResult],
        required=True,
    )
    parsed = parser.parse_args(argv)
    try:
        verified = verify_mcp_zup_evidence(
            parsed.run_dir,
            expected_result=ExpectedLiveResult(parsed.expected_result),
        )
    except LiveEvidenceError as error:
        print(f"evidence verification failed: {error}")
        return 2
    print(json.dumps(verified, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
