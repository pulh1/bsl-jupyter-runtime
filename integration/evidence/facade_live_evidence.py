"""Publication-safe evidence for the bounded Agent MCP facade live run."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from hashlib import sha256
import hmac
import json
from pathlib import Path
import re


_FILES = ("environment.json", "observations.json", "cleanup.json", "summary.json")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_PRIVATE_KEYS = {
    "cmdline",
    "command",
    "create_time",
    "database_path",
    "infobase_path",
    "password",
    "pid",
    "process_id",
    "raw_source",
    "rdbg",
    "source",
    "token",
    "username",
}
_PRIVATE_TEXT = re.compile(r"(?i)(?:\b(?:pid|token|rdbg|file|usr|pwd)\s*[=:]|[a-z]:\\)")
_CALL_SEQUENCE = (
    "A:workspace.open",
    "A:runtime.ensure",
    "A:frontend.exit",
    "B:workspace.open",
    "B:runtime.ensure",
    "B:operation.wait",
    "B:runtime.ensure.ready",
    "B:code.list",
    "B:code.get",
    "B:code.run",
    "B:operation.wait.main",
    "B:value.inspect",
    "B:runtime.close",
    "owner:service.shutdown",
)
_ROLES = ("service", "mcp_a", "mcp_b", "designer", "dbgs", "onec")
_NEGATIVE_ASSERTIONS = {
    "no_database_path",
    "no_pid",
    "no_process_command",
    "no_raw_rdbg",
    "no_saved_source",
    "no_token",
    "no_username",
    "no_value_payload",
}


class AgentFacadeLiveEvidenceError(RuntimeError):
    """The public evidence bundle is incomplete, mutable, or privacy-unsafe."""


def build_agent_facade_live_summary(
    *,
    environment: Mapping[str, object],
    observations: Mapping[str, object],
    cleanup: Mapping[str, object],
) -> dict[str, object]:
    """Independently derive the compact summary from raw allowlisted facts."""
    attempt = _validate_environment(environment)
    _validate_cleanup(cleanup, attempt=attempt)
    if observations.get("attempt") != attempt:
        raise AgentFacadeLiveEvidenceError("evidence attempt identities differ")
    result = observations.get("result")
    if result == "PASS":
        _validate_pass_observations(
            observations, attempt=attempt, environment=environment
        )
        startup = _mapping(observations["startup"], "startup")
        runtime = _mapping(observations["runtime"], "runtime")
        code = _mapping(observations["code"], "code")
        observation = _mapping(observations["observation"], "observation")
        summary: dict[str, object] = {
            "schema": "onec-agent-facade-live-summary-v1",
            "status": "PASS",
            "attempt": attempt,
            "profile": "agent",
            "startup_operation_identity_sha256": startup["operation_identity_sha256"],
            "startup_submission_count": startup["startup_submission_count"],
            "startup_terminal_state": startup["observed_states"][-1],  # type: ignore[index]
            "runtime_identity_sha256": runtime["ready_identity_sha256"],
            "runtime_generation": runtime["ready_generation"],
            "code_operation_identity_sha256": code["operation_identity_sha256"],
            "code_terminal_state": code["terminal_state"],
            "observed_proxy": observation["proxy_qualified_name"],
            "materialization_calls": observation["materialization_calls"],
            "cleanup_roles": list(_ROLES),
            "owned_process_count": len(_ROLES),
            "cleanup_error_count": 0,
        }
    elif result == "FAIL":
        _validate_fail_observations(observations, attempt=attempt)
        failure = _mapping(observations["failure"], "failure")
        summary = {
            "schema": "onec-agent-facade-live-summary-v1",
            "status": "FAIL",
            "attempt": attempt,
            "profile": "agent",
            "failure_boundary": failure["boundary"],
            "failure_type": failure["type"],
            "completed_call_sequence": list(observations["call_sequence"]),  # type: ignore[arg-type]
            "cleanup_roles": [
                item["role"]
                for item in cleanup["identities"]  # type: ignore[union-attr]
            ],
            "owned_process_count": len(cleanup["identities"]),  # type: ignore[arg-type]
            "cleanup_error_count": cleanup["owned_cleanup_error_count"],
        }
    else:
        raise AgentFacadeLiveEvidenceError("observations result must be PASS or FAIL")
    summary["source_files_sha256"] = {
        "environment.json": _json_hash(environment),
        "observations.json": _json_hash(observations),
        "cleanup.json": _json_hash(cleanup),
    }
    _assert_public(summary)
    return summary


def write_agent_facade_live_evidence(
    destination: Path,
    *,
    environment: Mapping[str, object],
    observations: Mapping[str, object],
    cleanup: Mapping[str, object],
) -> dict[str, object]:
    """Write a new immutable-shaped bundle; never overwrite an attempt."""
    destination = Path(destination)
    if destination.exists():
        raise AgentFacadeLiveEvidenceError("evidence attempt already exists")
    summary = build_agent_facade_live_summary(
        environment=environment, observations=observations, cleanup=cleanup
    )
    values = {
        "environment.json": dict(environment),
        "observations.json": dict(observations),
        "cleanup.json": dict(cleanup),
        "summary.json": summary,
    }
    for value in values.values():
        _assert_public(value)
    destination.mkdir(parents=True)
    hashes: dict[str, str] = {}
    for name in _FILES:
        payload = _canonical(values[name])
        (destination / name).write_bytes(payload)
        hashes[name] = sha256(payload).hexdigest()
    manifest = "".join(f"{hashes[name]}  {name}\n" for name in _FILES)
    (destination / "manifest.sha256").write_text(manifest, encoding="ascii", newline="\n")
    return summary


def verify_agent_facade_live_evidence(
    destination: Path,
    *,
    expected_result: str,
    expected_implementation: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Recompute every semantic gate and exact manifest from raw public facts."""
    if expected_result not in {"PASS", "FAIL"}:
        raise AgentFacadeLiveEvidenceError("expected_result must be PASS or FAIL")
    destination = Path(destination)
    expected_names = {*_FILES, "manifest.sha256"}
    if not destination.is_dir() or {item.name for item in destination.iterdir()} != expected_names:
        raise AgentFacadeLiveEvidenceError("evidence file set is not exact")
    values = {name: _read_json(destination / name) for name in _FILES}
    for value in values.values():
        _assert_public(value)
    if values["observations.json"].get("result") != expected_result:
        raise AgentFacadeLiveEvidenceError("evidence result does not match expectation")
    attempt = values["environment.json"].get("attempt")
    if type(attempt) is not int or attempt < 1:
        raise AgentFacadeLiveEvidenceError("evidence attempt is invalid")
    recorded_implementation = values["environment.json"].get("implementation")
    if attempt >= 2:
        if expected_implementation is None:
            raise AgentFacadeLiveEvidenceError(
                "attempt 2+ requires a trusted implementation expectation"
            )
        if not hmac.compare_digest(
            _canonical(recorded_implementation), _canonical(expected_implementation)
        ):
            raise AgentFacadeLiveEvidenceError(
                "evidence implementation does not match the trusted expectation"
            )
    recomputed = build_agent_facade_live_summary(
        environment=values["environment.json"],
        observations=values["observations.json"],
        cleanup=values["cleanup.json"],
    )
    if not hmac.compare_digest(
        _canonical(values["summary.json"]), _canonical(recomputed)
    ):
        raise AgentFacadeLiveEvidenceError("summary is not derived from raw evidence")
    actual_manifest = (destination / "manifest.sha256").read_text(encoding="ascii")
    expected_manifest = "".join(
        f"{sha256((destination / name).read_bytes()).hexdigest()}  {name}\n"
        for name in _FILES
    )
    if not hmac.compare_digest(actual_manifest, expected_manifest):
        raise AgentFacadeLiveEvidenceError("manifest does not bind the exact bundle")
    return recomputed


def _validate_environment(value: Mapping[str, object]) -> int:
    attempt = value.get("attempt")
    if type(attempt) is not int or attempt < 1:
        raise AgentFacadeLiveEvidenceError("environment attempt is invalid")
    fields = {"schema", "attempt", "profile", "maximum_mode", "platform", "database", "notebook"}
    if attempt >= 2:
        fields.add("implementation")
    _exact(value, fields, "environment")
    if (
        value["schema"] != "onec-agent-facade-live-environment-v1"
        or value["profile"] != "agent"
        or value["maximum_mode"] != "experiment"
    ):
        raise AgentFacadeLiveEvidenceError("environment identity is invalid")
    platform = _mapping(value["platform"], "platform")
    _exact(platform, {"version", "bin_identity_sha256", "executables_sha256"}, "platform")
    if platform["version"] != "8.3.27.2170":
        raise AgentFacadeLiveEvidenceError("platform version is invalid")
    _require_hash(platform["bin_identity_sha256"], "platform bin")
    executables = _mapping(platform["executables_sha256"], "executables")
    _exact(executables, {"1cv8c.exe", "dbgs.exe"}, "executables")
    for name, digest in executables.items():
        _require_hash(digest, name)
    database = _mapping(value["database"], "database")
    mutation_field = (
        "test_cell_requests_database_mutation"
        if attempt >= 2
        else "mutation_performed"
    )
    _exact(database, {"target_identity_sha256", mutation_field}, "database")
    _require_hash(database["target_identity_sha256"], "database")
    if database[mutation_field] is not False:
        raise AgentFacadeLiveEvidenceError("acceptance cell must not request database mutation")
    notebook = _mapping(value["notebook"], "notebook")
    _exact(notebook, {"artifact_sha256", "cell_source_sha256"}, "notebook")
    _require_hash(notebook["artifact_sha256"], "notebook")
    _require_hash(notebook["cell_source_sha256"], "cell source")
    if attempt >= 2:
        implementation = _mapping(value["implementation"], "implementation")
        _exact(
            implementation,
            {
                "harness_sha256",
                "verifier_sha256",
                "runtime_tree_sha256",
                "notebook_artifact_sha256",
                "notebook_cell_source_sha256",
            },
            "implementation",
        )
        for name, digest in implementation.items():
            _require_hash(digest, name)
        if (
            implementation["notebook_artifact_sha256"]
            != notebook["artifact_sha256"]
            or implementation["notebook_cell_source_sha256"]
            != notebook["cell_source_sha256"]
        ):
            raise AgentFacadeLiveEvidenceError(
                "trusted implementation does not bind the notebook"
            )
    return attempt


def _validate_pass_observations(
    value: Mapping[str, object],
    *,
    attempt: int,
    environment: Mapping[str, object],
) -> None:
    _exact(value, {"schema", "attempt", "result", "call_sequence", "startup", "runtime", "code", "observation", "service", "negative_assertions"}, "observations")
    if value["schema"] != "onec-agent-facade-live-observations-v1" or value["attempt"] != attempt or attempt < 2 or value["result"] != "PASS":
        raise AgentFacadeLiveEvidenceError("PASS observations identity is invalid")
    if tuple(value["call_sequence"]) != _CALL_SEQUENCE:  # type: ignore[arg-type]
        raise AgentFacadeLiveEvidenceError("agent call sequence is invalid")
    startup = _mapping(value["startup"], "startup")
    _exact(startup, {"operation_identity_sha256", "frontend_a_operation_identity_sha256", "frontend_b_operation_identity_sha256", "observed_states", "published_before_ready", "frontend_a_exited_before_terminal", "startup_submission_count", "next_event_cursors", "next_message_cursors", "truncation_complete", "wait_call_count"}, "startup")
    operation_hashes = (
        startup["operation_identity_sha256"],
        startup["frontend_a_operation_identity_sha256"],
        startup["frontend_b_operation_identity_sha256"],
    )
    for digest in operation_hashes:
        _require_hash(digest, "startup operation")
    if len(set(operation_hashes)) != 1:
        raise AgentFacadeLiveEvidenceError("frontends did not join one startup operation")
    if not _startup_state_sequence(startup["observed_states"]):
        raise AgentFacadeLiveEvidenceError("startup state transition is invalid")
    if startup["published_before_ready"] is not True or startup["frontend_a_exited_before_terminal"] is not True or startup["startup_submission_count"] != 1:
        raise AgentFacadeLiveEvidenceError("durable startup invariants are not proven")
    event_cursors = startup["next_event_cursors"]
    message_cursors = startup["next_message_cursors"]
    if not _cursor_sequence(event_cursors, length=3, strictly_terminal=True) or not _cursor_sequence(message_cursors, length=3):
        raise AgentFacadeLiveEvidenceError("startup cursors are invalid")
    if startup["truncation_complete"] != [True, True, True]:
        raise AgentFacadeLiveEvidenceError("startup views were truncated")
    if type(startup["wait_call_count"]) is not int or not 1 <= startup["wait_call_count"] <= 6:
        raise AgentFacadeLiveEvidenceError("startup wait count is invalid")
    runtime = _mapping(value["runtime"], "runtime")
    _exact(runtime, {"ready_identity_sha256", "ready_generation", "ready_state", "descriptor_returned"}, "runtime")
    _require_hash(runtime["ready_identity_sha256"], "runtime")
    if type(runtime["ready_generation"]) is not int or runtime["ready_generation"] < 1 or runtime["ready_state"] != "idle" or runtime["descriptor_returned"] is not True:
        raise AgentFacadeLiveEvidenceError("READY runtime identity is invalid")
    code = _mapping(value["code"], "code")
    _exact(code, {"cell_id", "language", "mode", "revision", "source_sha256", "document_sha256", "operation_identity_sha256", "terminal_state", "next_event_cursor", "next_message_cursor", "truncation_complete", "wait_call_count"}, "code")
    if code["cell_id"] != "mcp-zup-agent-facade-acceptance" or code["language"] != "bsl" or code["mode"] != "main" or code["revision"] != 1 or code["terminal_state"] != "completed":
        raise AgentFacadeLiveEvidenceError("saved MAIN execution is invalid")
    for name in ("source_sha256", "document_sha256", "operation_identity_sha256"):
        _require_hash(code[name], name)
    notebook = _mapping(environment["notebook"], "notebook")
    if code["source_sha256"] != notebook["cell_source_sha256"]:
        raise AgentFacadeLiveEvidenceError(
            "executed source does not match the trusted notebook source"
        )
    if type(code["next_event_cursor"]) is not int or code["next_event_cursor"] < 1 or type(code["next_message_cursor"]) is not int or code["next_message_cursor"] < 0:
        raise AgentFacadeLiveEvidenceError("code cursors are invalid")
    if code["truncation_complete"] is not True:
        raise AgentFacadeLiveEvidenceError("code view was truncated")
    if type(code["wait_call_count"]) is not int or not 1 <= code["wait_call_count"] <= 6:
        raise AgentFacadeLiveEvidenceError("code wait count is invalid")
    observation = _mapping(value["observation"], "observation")
    _exact(observation, {"requested_alias", "requested_binding", "requested_result", "budget_profile", "output_count", "proxy_realm", "proxy_qualified_name", "proxy_consistency", "inspect_detail", "inspection_actions", "materialization_calls", "raw_value_present"}, "observation")
    if observation != {
        "requested_alias": "scalar",
        "requested_binding": "bsl.АгентСкаляр",
        "requested_result": "proxy",
        "budget_profile": "agent_metadata",
        "output_count": 1,
        "proxy_realm": "onec",
        "proxy_qualified_name": "bsl.АгентСкаляр",
        "proxy_consistency": "exact",
        "inspect_detail": "auto",
        "inspection_actions": [
            {"name": "size", "cost": "bounded_scan", "available": False},
            {"name": "preview", "cost": "bounded_scan", "available": False},
            {"name": "materialize", "cost": "full_scan", "available": False},
            {"name": "to_df", "cost": "full_scan", "available": False},
        ],
        "materialization_calls": 0,
        "raw_value_present": False,
    }:
        raise AgentFacadeLiveEvidenceError("proxy-only scalar observation is invalid")
    service = _mapping(value["service"], "service")
    _exact(service, {"separate_frontends", "service_alive_after_a_exit", "runtime_alive_after_a_exit"}, "service")
    if any(item is not True for item in service.values()):
        raise AgentFacadeLiveEvidenceError("frontend replacement is not proven")
    negative = _mapping(value["negative_assertions"], "negative assertions")
    if set(negative) != _NEGATIVE_ASSERTIONS or any(item is not True for item in negative.values()):
        raise AgentFacadeLiveEvidenceError("privacy assertions are incomplete")


def _validate_fail_observations(value: Mapping[str, object], *, attempt: int) -> None:
    _exact(value, {"schema", "attempt", "result", "call_sequence", "failure", "negative_assertions"}, "FAIL observations")
    if value["schema"] != "onec-agent-facade-live-observations-v1" or value["attempt"] != attempt or value["result"] != "FAIL":
        raise AgentFacadeLiveEvidenceError("FAIL observations identity is invalid")
    calls = value["call_sequence"]
    if isinstance(calls, str) or not isinstance(calls, Sequence) or tuple(calls) != _CALL_SEQUENCE[: len(calls)]:
        raise AgentFacadeLiveEvidenceError("FAIL call sequence is not an exact prefix")
    failure = _mapping(value["failure"], "failure")
    _exact(failure, {"boundary", "type"}, "failure")
    if not all(isinstance(failure[name], str) and failure[name] for name in failure):
        raise AgentFacadeLiveEvidenceError("failure facts are invalid")
    negative = _mapping(value["negative_assertions"], "negative assertions")
    if set(negative) != _NEGATIVE_ASSERTIONS or any(item is not True for item in negative.values()):
        raise AgentFacadeLiveEvidenceError("privacy assertions are incomplete")


def _validate_cleanup(value: Mapping[str, object], *, attempt: int) -> None:
    _exact(value, {"schema", "attempt", "runtime_closed_explicitly", "all_absent", "owned_cleanup_error_count", "identities"}, "cleanup")
    if value["schema"] != "onec-agent-facade-live-cleanup-v1" or value["attempt"] != attempt:
        raise AgentFacadeLiveEvidenceError("cleanup identity is invalid")
    identities = value["identities"]
    if isinstance(identities, (str, bytes)) or not isinstance(identities, Sequence):
        raise AgentFacadeLiveEvidenceError("cleanup identities are invalid")
    roles: list[str] = []
    for raw in identities:
        item = _mapping(raw, "cleanup identity")
        _exact(item, {"role", "identity_sha256", "absent_after_cleanup"}, "cleanup identity")
        if not isinstance(item["role"], str):
            raise AgentFacadeLiveEvidenceError("cleanup role is invalid")
        roles.append(item["role"])
        _require_hash(item["identity_sha256"], "cleanup identity")
        if item["absent_after_cleanup"] is not True:
            raise AgentFacadeLiveEvidenceError("owned process remains alive")
    if value["all_absent"] is not True or value["owned_cleanup_error_count"] != 0:
        raise AgentFacadeLiveEvidenceError("owned cleanup failed")
    if value.get("runtime_closed_explicitly") is True:
        if tuple(roles) != _ROLES:
            raise AgentFacadeLiveEvidenceError("PASS cleanup roles are not exact")
    elif not roles:
        raise AgentFacadeLiveEvidenceError("FAIL cleanup must retain owned identities")


def _cursor_sequence(value: object, *, length: int, strictly_terminal: bool = False) -> bool:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or len(value) != length:
        return False
    if any(type(item) is not int or item < 0 for item in value):
        return False
    if any(current < previous for previous, current in zip(value, value[1:])):
        return False
    return not strictly_terminal or value[-1] > value[0]


def _startup_state_sequence(value: object) -> bool:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return False
    states = list(value)
    if len(states) != 3 or states[0] not in {"queued", "running"}:
        return False
    if states[-1] != "completed":
        return False
    order = {"queued": 0, "running": 1, "completed": 2}
    try:
        ranks = [order[item] for item in states]
    except (KeyError, TypeError):
        return False
    return ranks == sorted(ranks)


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise AgentFacadeLiveEvidenceError(f"{name} must be an object")
    return value


def _exact(value: Mapping[str, object], names: set[str], label: str) -> None:
    if set(value) != names:
        raise AgentFacadeLiveEvidenceError(f"{label} fields are not exact")


def _require_hash(value: object, name: str) -> None:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise AgentFacadeLiveEvidenceError(f"{name} hash is invalid")


def _assert_public(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).casefold() in _PRIVATE_KEYS:
                raise AgentFacadeLiveEvidenceError(f"private field is forbidden: {key}")
            _assert_public(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            _assert_public(item)
    elif isinstance(value, str) and _PRIVATE_TEXT.search(value):
        raise AgentFacadeLiveEvidenceError("private text is forbidden")


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _json_hash(value: object) -> str:
    return sha256(_canonical(value)).hexdigest()


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise AgentFacadeLiveEvidenceError(f"invalid evidence file: {path.name}") from error
    if not isinstance(value, dict):
        raise AgentFacadeLiveEvidenceError(f"evidence file is not an object: {path.name}")
    return value
