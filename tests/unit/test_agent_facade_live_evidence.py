from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

import pytest

from integration.evidence.facade_live_evidence import (
    AgentFacadeLiveEvidenceError,
    build_agent_facade_live_summary,
    verify_agent_facade_live_evidence,
    write_agent_facade_live_evidence,
)


def _environment() -> dict[str, object]:
    return {
        "schema": "onec-agent-facade-live-environment-v1",
        "attempt": 2,
        "profile": "agent",
        "maximum_mode": "experiment",
        "platform": {
            "version": "8.3.27.2170",
            "bin_identity_sha256": "a" * 64,
            "executables_sha256": {"1cv8c.exe": "b" * 64, "dbgs.exe": "c" * 64},
        },
        "database": {
            "target_identity_sha256": "d" * 64,
            "test_cell_requests_database_mutation": False,
        },
        "notebook": {"artifact_sha256": "e" * 64, "cell_source_sha256": "f" * 64},
        "implementation": {
            "harness_sha256": "6" * 64,
            "verifier_sha256": "7" * 64,
            "runtime_tree_sha256": "8" * 64,
            "notebook_artifact_sha256": "e" * 64,
            "notebook_cell_source_sha256": "f" * 64,
        },
    }


def _expected_implementation() -> dict[str, object]:
    return dict(_environment()["implementation"])


def _observations(*, result: str = "PASS") -> dict[str, object]:
    return {
        "schema": "onec-agent-facade-live-observations-v1",
        "attempt": 2,
        "result": result,
        "call_sequence": [
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
        ],
        "startup": {
            "operation_identity_sha256": "1" * 64,
            "frontend_a_operation_identity_sha256": "1" * 64,
            "frontend_b_operation_identity_sha256": "1" * 64,
            "observed_states": ["running", "running", "completed"],
            "published_before_ready": True,
            "frontend_a_exited_before_terminal": True,
            "startup_submission_count": 1,
            "next_event_cursors": [2, 2, 4],
            "next_message_cursors": [0, 0, 0],
            "truncation_complete": [True, True, True],
            "wait_call_count": 1,
        },
        "runtime": {
            "ready_identity_sha256": "2" * 64,
            "ready_generation": 1,
            "ready_state": "idle",
            "descriptor_returned": True,
        },
        "code": {
            "cell_id": "mcp-zup-agent-facade-acceptance",
            "language": "bsl",
            "mode": "main",
            "revision": 1,
            "source_sha256": "f" * 64,
            "document_sha256": "3" * 64,
            "operation_identity_sha256": "4" * 64,
            "terminal_state": "completed",
            "next_event_cursor": 8,
            "next_message_cursor": 0,
            "truncation_complete": True,
            "wait_call_count": 1,
        },
        "observation": {
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
        },
        "service": {
            "separate_frontends": True,
            "service_alive_after_a_exit": True,
            "runtime_alive_after_a_exit": True,
        },
        "negative_assertions": {
            "no_database_path": True,
            "no_pid": True,
            "no_process_command": True,
            "no_raw_rdbg": True,
            "no_saved_source": True,
            "no_token": True,
            "no_username": True,
            "no_value_payload": True,
        },
    }


def _cleanup() -> dict[str, object]:
    return {
        "schema": "onec-agent-facade-live-cleanup-v1",
        "attempt": 2,
        "runtime_closed_explicitly": True,
        "all_absent": True,
        "owned_cleanup_error_count": 0,
        "identities": [
            {"role": role, "identity_sha256": str(index) * 64, "absent_after_cleanup": True}
            for index, role in enumerate(
                ("service", "mcp_a", "mcp_b", "designer", "dbgs", "onec"), start=1
            )
        ],
    }


def _fail_observations() -> dict[str, object]:
    return {
        "schema": "onec-agent-facade-live-observations-v1",
        "attempt": 1,
        "result": "FAIL",
        "call_sequence": ["A:workspace.open"],
        "failure": {"boundary": "frontend_a_startup_publication", "type": "AssertionError"},
        "negative_assertions": dict(_observations()["negative_assertions"]),
    }


def _legacy_fail_environment() -> dict[str, object]:
    value = _environment()
    value["attempt"] = 1
    value.pop("implementation")
    value["database"] = {
        "target_identity_sha256": "d" * 64,
        "mutation_performed": False,
    }
    return value


def _fail_cleanup() -> dict[str, object]:
    return {
        "schema": "onec-agent-facade-live-cleanup-v1",
        "attempt": 1,
        "runtime_closed_explicitly": False,
        "all_absent": True,
        "owned_cleanup_error_count": 0,
        "identities": [
            {
                "role": "service",
                "identity_sha256": "1" * 64,
                "absent_after_cleanup": True,
            }
        ],
    }


def _write(tmp_path: Path) -> Path:
    destination = tmp_path / "attempt-1"
    write_agent_facade_live_evidence(
        destination,
        environment=_environment(),
        observations=_observations(),
        cleanup=_cleanup(),
    )
    return destination


def _rewrite_manifest(destination: Path) -> None:
    names = ("environment.json", "observations.json", "cleanup.json", "summary.json")
    (destination / "manifest.sha256").write_text(
        "".join(
            f"{sha256((destination / name).read_bytes()).hexdigest()}  {name}\n"
            for name in names
        ),
        encoding="ascii",
        newline="\n",
    )


def test_verifier_accepts_only_exact_agent_facade_pass_bundle(tmp_path: Path) -> None:
    destination = _write(tmp_path)

    verified = verify_agent_facade_live_evidence(
        destination,
        expected_result="PASS",
        expected_implementation=_expected_implementation(),
    )

    assert verified == build_agent_facade_live_summary(
        environment=_environment(), observations=_observations(), cleanup=_cleanup()
    )
    assert verified["status"] == "PASS"


def test_verifier_accepts_sanitized_fail_only_under_explicit_fail_expectation(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "attempt-1"
    write_agent_facade_live_evidence(
        destination,
        environment=_legacy_fail_environment(),
        observations=_fail_observations(),
        cleanup=_fail_cleanup(),
    )

    verified = verify_agent_facade_live_evidence(destination, expected_result="FAIL")

    assert verified["status"] == "FAIL"
    with pytest.raises(AgentFacadeLiveEvidenceError):
        verify_agent_facade_live_evidence(
            destination,
            expected_result="PASS",
            expected_implementation=_expected_implementation(),
        )


@pytest.mark.parametrize(
    ("path", "value"),
    (
        (("observations", "startup", "startup_submission_count"), 2),
        (("observations", "startup", "frontend_b_operation_identity_sha256"), "9" * 64),
        (("observations", "startup", "next_event_cursors"), [2, 2]),
        (("observations", "observation", "requested_result"), "preview"),
        (("observations", "observation", "materialization_calls"), 1),
        (("observations", "observation", "raw_value_present"), True),
        (("cleanup", "all_absent"), False),
        (("cleanup", "owned_cleanup_error_count"), 1),
    ),
)
def test_verifier_rejects_semantic_mutation_even_after_rehash(
    tmp_path: Path, path: tuple[str, ...], value: object
) -> None:
    destination = _write(tmp_path)
    filename = f"{path[0]}.json"
    root = json.loads((destination / filename).read_text(encoding="utf-8"))
    target: object = root
    for key in path[1:-1]:
        assert isinstance(target, dict)
        target = target[key]
    assert isinstance(target, dict)
    target[path[-1]] = value
    (destination / filename).write_text(
        json.dumps(root, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    _rewrite_manifest(destination)

    with pytest.raises(AgentFacadeLiveEvidenceError):
        verify_agent_facade_live_evidence(
            destination,
            expected_result="PASS",
            expected_implementation=_expected_implementation(),
        )


def test_verifier_rejects_forged_summary_and_manifest_drift(tmp_path: Path) -> None:
    destination = _write(tmp_path)
    summary = json.loads((destination / "summary.json").read_text(encoding="utf-8"))
    summary["startup_submission_count"] = 99
    (destination / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
    )

    with pytest.raises(AgentFacadeLiveEvidenceError):
        verify_agent_facade_live_evidence(
            destination,
            expected_result="PASS",
            expected_implementation=_expected_implementation(),
        )


@pytest.mark.parametrize(
    "states",
    (
        ["queued", "queued", "completed"],
        ["queued", "completed", "completed"],
        ["running", "completed", "completed"],
    ),
)
def test_verifier_accepts_monotonic_startup_races(
    tmp_path: Path, states: list[str]
) -> None:
    observations = _observations()
    observations["startup"]["observed_states"] = states  # type: ignore[index]
    destination = tmp_path / "attempt-2"
    write_agent_facade_live_evidence(
        destination,
        environment=_environment(),
        observations=observations,
        cleanup=_cleanup(),
    )

    verified = verify_agent_facade_live_evidence(
        destination,
        expected_result="PASS",
        expected_implementation=_expected_implementation(),
    )

    assert verified["status"] == "PASS"


@pytest.mark.parametrize(
    ("mutation", "match"),
    (
        (("startup", "next_event_cursors", [2, 1, 4]), "cursors"),
        (("code", "source_sha256", "9" * 64), "source"),
    ),
)
def test_verifier_rejects_cursor_regression_and_unbound_code_source(
    tmp_path: Path,
    mutation: tuple[str, str, object],
    match: str,
) -> None:
    observations = _observations()
    section, field, changed = mutation
    observations[section][field] = changed  # type: ignore[index]
    with pytest.raises(AgentFacadeLiveEvidenceError, match=match):
        build_agent_facade_live_summary(
            environment=_environment(),
            observations=observations,
            cleanup=_cleanup(),
        )


def test_verifier_trusted_binding_includes_notebook_hashes(tmp_path: Path) -> None:
    destination = _write(tmp_path)
    expected = _expected_implementation()
    expected["notebook_artifact_sha256"] = "9" * 64

    with pytest.raises(AgentFacadeLiveEvidenceError, match="implementation"):
        verify_agent_facade_live_evidence(
            destination,
            expected_result="PASS",
            expected_implementation=expected,
        )


def test_verifier_rejects_duplicate_manifest_entry(tmp_path: Path) -> None:
    destination = _write(tmp_path)
    manifest = destination / "manifest.sha256"
    first_line = manifest.read_text(encoding="ascii").splitlines(keepends=True)[0]
    with manifest.open("a", encoding="ascii", newline="\n") as stream:
        stream.write(first_line)

    with pytest.raises(AgentFacadeLiveEvidenceError):
        verify_agent_facade_live_evidence(
            destination,
            expected_result="PASS",
            expected_implementation=_expected_implementation(),
        )


@pytest.mark.parametrize("secret", ("pid", "token", "rdbg", "source", "username"))
def test_writer_rejects_private_or_raw_fields(tmp_path: Path, secret: str) -> None:
    observations = deepcopy(_observations())
    observations[secret] = "private"

    with pytest.raises(AgentFacadeLiveEvidenceError):
        write_agent_facade_live_evidence(
            tmp_path / "attempt-1",
            environment=_environment(),
            observations=observations,
            cleanup=_cleanup(),
        )


def test_expected_result_is_mandatory_and_typed(tmp_path: Path) -> None:
    destination = _write(tmp_path)

    with pytest.raises(AgentFacadeLiveEvidenceError):
        verify_agent_facade_live_evidence(destination, expected_result="FAIL")
