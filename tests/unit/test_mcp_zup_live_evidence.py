from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

import pytest
import integration.evidence.live_evidence as live_evidence

from integration.evidence.live_evidence import (
    EXPECTED_CALL_SEQUENCE,
    EXPECTED_MESSAGES,
    ExpectedLiveResult,
    LiveEvidenceError,
    build_mcp_zup_fail_observations,
    build_mcp_zup_terminal_fail_observations,
    evidence_identity,
    verify_mcp_zup_evidence,
    write_mcp_zup_evidence,
)


def _hash(label: str) -> str:
    return sha256(label.encode("utf-8")).hexdigest()


def _evidence() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    environment: dict[str, object] = {
        "schema": "onec-mcp-zup-main-environment-v1",
        "attempt": 1,
        "platform": {
            "version": "8.3.27.2170",
            "profile": "zup",
            "bin_identity_sha256": _hash("bin"),
            "executables_sha256": {
                "1cv8c.exe": _hash("1cv8c"),
                "dbgs.exe": _hash("dbgs"),
            },
        },
        "database": {
            "target_identity_sha256": _hash("base"),
            "mutation_performed": False,
        },
    }
    observations: dict[str, object] = {
        "schema": "onec-mcp-zup-main-observations-v1",
        "attempt": 1,
        "result": "PASS",
        "service": {
            "maximum_mode": "experiment",
            "service_identity_sha256": _hash("service"),
            "frontend_a_identity_sha256": _hash("mcp-a"),
            "frontend_b_identity_sha256": _hash("mcp-b"),
            "separate_processes": True,
            "service_alive_after_a_exit": True,
            "onec_alive_after_a_exit": True,
            "control_timeout_s": 10.0,
            "local_timeout_origin_observed": False,
        },
        "code": {
            "cell_id": "mcp-zup-main-acceptance",
            "language": "bsl",
            "mode": "main",
            "revision": 1,
            "source_sha256": _hash("source"),
            "document_sha256": _hash("document"),
            "artifact_sha256": _hash("notebook"),
        },
        "runtime": {
            "a_identity_sha256": _hash("runtime"),
            "b_identity_sha256": _hash("runtime"),
            "a_generation": 1,
            "b_generation": 1,
        },
        "operation": {
            "identity_sha256": _hash("operation"),
            "history_identity_sha256": _hash("operation"),
            "call_sequence": list(EXPECTED_CALL_SEQUENCE),
            "observed_states": ["running", "completed", "completed"],
            "terminal_state": "completed",
            "messages": list(EXPECTED_MESSAGES),
            "result_present": True,
            "result_access": "unavailable_until_value_proxies",
            "result_call_ok": False,
            "result_failure_category": "unsupported",
            "no_raw_result": True,
        },
        "timings": [
            {"phase": phase, "duration_s": 0.01}
            for phase in (
                "service_start",
                "runtime_ensure",
                "code_run",
                "frontend_reconnect",
                "operation_recovery",
                "runtime_close",
                "owned_cleanup",
            )
        ],
        "negative_assertions": {
            "no_infobase_path": True,
            "no_pid": True,
            "no_process_command": True,
            "no_raw_platform_uuid": True,
            "no_raw_rdbg": True,
            "no_saved_source": True,
            "no_token": True,
            "no_username": True,
        },
    }
    cleanup: dict[str, object] = {
        "schema": "onec-mcp-zup-main-cleanup-v1",
        "attempt": 1,
        "all_absent": True,
        "owned_cleanup_error_count": 0,
        "identities": [
            {
                "role": role,
                "identity_sha256": _hash(role),
                "absent_after_cleanup": True,
            }
            for role in ("service", "mcp_a", "mcp_b", "designer", "dbgs", "onec")
        ],
    }
    return environment, observations, cleanup


def test_writer_and_independent_verifier_recompute_all_live_gates(tmp_path: Path) -> None:
    environment, observations, cleanup = _evidence()
    run_dir = tmp_path / "run"

    write_mcp_zup_evidence(
        run_dir,
        environment=environment,
        observations=observations,
        cleanup=cleanup,
    )
    verified = verify_mcp_zup_evidence(
        run_dir,
        expected_result=ExpectedLiveResult.PASS,
    )

    assert verified == {
        "status": "PASS",
        "attempt": 1,
        "platform_version": "8.3.27.2170",
        "profile": "zup",
        "terminal_state": "completed",
        "message_count": 3,
        "result_present": True,
        "frontend_reconnect": True,
        "owned_process_count": 0,
    }
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary == verified
    assert set((run_dir / "manifest.sha256").read_text(encoding="ascii").splitlines()) == {
        f"{sha256((run_dir / name).read_bytes()).hexdigest()}  {name}"
        for name in ("environment.json", "observations.json", "cleanup.json", "summary.json")
    }


@pytest.mark.parametrize(
    ("section", "field", "value", "message"),
    [
        ("operation", "messages", [EXPECTED_MESSAGES[1], EXPECTED_MESSAGES[0], EXPECTED_MESSAGES[2]], "messages"),
        ("operation", "terminal_state", "running", "terminal"),
        ("operation", "result_call_ok", True, "result"),
        ("runtime", "b_generation", 2, "generation"),
        ("service", "service_alive_after_a_exit", False, "frontend"),
    ],
)
def test_verifier_rejects_mutated_semantics(
    tmp_path: Path,
    section: str,
    field: str,
    value: object,
    message: str,
) -> None:
    environment, observations, cleanup = _evidence()
    run_dir = tmp_path / "run"
    write_mcp_zup_evidence(run_dir, environment=environment, observations=observations, cleanup=cleanup)
    payload = json.loads((run_dir / "observations.json").read_text(encoding="utf-8"))
    payload[section][field] = value
    (run_dir / "observations.json").write_text(json.dumps(payload), encoding="utf-8")
    _refresh_manifest(run_dir, "observations.json")

    with pytest.raises(LiveEvidenceError, match=message):
        verify_mcp_zup_evidence(run_dir, expected_result=ExpectedLiveResult.PASS)


def test_verifier_rejects_manifest_drift_cleanup_failure_and_raw_identifiers(tmp_path: Path) -> None:
    environment, observations, cleanup = _evidence()
    run_dir = tmp_path / "run"
    write_mcp_zup_evidence(run_dir, environment=environment, observations=observations, cleanup=cleanup)
    (run_dir / "cleanup.json").write_text("{}", encoding="utf-8")
    with pytest.raises(LiveEvidenceError, match="manifest"):
        verify_mcp_zup_evidence(run_dir, expected_result=ExpectedLiveResult.PASS)

    environment, observations, cleanup = _evidence()
    cleanup["all_absent"] = False
    other = tmp_path / "cleanup-failed"
    write_mcp_zup_evidence(other, environment=environment, observations=observations, cleanup=cleanup)
    with pytest.raises(LiveEvidenceError, match="cleanup"):
        verify_mcp_zup_evidence(other, expected_result=ExpectedLiveResult.PASS)

    environment, observations, cleanup = _evidence()
    observations["raw_pid"] = 12345
    leaked = tmp_path / "leaked"
    write_mcp_zup_evidence(leaked, environment=environment, observations=observations, cleanup=cleanup)
    with pytest.raises(LiveEvidenceError, match="public evidence"):
        verify_mcp_zup_evidence(leaked, expected_result=ExpectedLiveResult.PASS)


def test_verifier_rejects_duplicate_manifest_entries(tmp_path: Path) -> None:
    environment, observations, cleanup = _evidence()
    run_dir = tmp_path / "duplicate-manifest"
    write_mcp_zup_evidence(
        run_dir,
        environment=environment,
        observations=observations,
        cleanup=cleanup,
    )
    manifest = run_dir / "manifest.sha256"
    first = manifest.read_text(encoding="ascii").splitlines()[0]
    manifest.write_text(
        manifest.read_text(encoding="ascii") + first + "\n",
        encoding="ascii",
    )

    with pytest.raises(LiveEvidenceError, match="manifest"):
        verify_mcp_zup_evidence(
            run_dir,
            expected_result=ExpectedLiveResult.PASS,
        )


def test_verifier_never_trusts_a_summary_pass(tmp_path: Path) -> None:
    environment, observations, cleanup = _evidence()
    observations["result"] = "FAIL"
    run_dir = tmp_path / "run"
    write_mcp_zup_evidence(run_dir, environment=environment, observations=observations, cleanup=cleanup)
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    summary["status"] = "PASS"
    (run_dir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    _refresh_manifest(run_dir, "summary.json")

    with pytest.raises(LiveEvidenceError, match="result"):
        verify_mcp_zup_evidence(run_dir, expected_result=ExpectedLiveResult.PASS)


def test_writer_preserves_partial_fail_in_an_empty_attempt_directory(tmp_path: Path) -> None:
    environment, _, cleanup = _evidence()
    observations = build_mcp_zup_fail_observations(
        attempt=1,
        completed_call_sequence=["A:workspace.open"],
        attempt_duration_s=16.25,
        configured_control_timeout_s=10.0,
        static_check_passed=True,
        source_sha256=_hash("source"),
        artifact_sha256=_hash("notebook"),
        failure_category="platform_failure",
        failure_state_changed="unknown",
        failure_safe_to_retry="after_status_check",
        runtime_ensure_duration_s=None,
        local_timeout_origin_observed=False,
    )
    cleanup["identities"] = [
        item for item in cleanup["identities"] if item["role"] != "mcp_b"
    ]
    run_dir = tmp_path / "attempt-1"
    run_dir.mkdir()

    write_mcp_zup_evidence(
        run_dir,
        environment=environment,
        observations=observations,
        cleanup=cleanup,
    )

    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "FAIL"
    assert summary["terminal_state"] is None
    assert observations["failure"] == {
        "category": "platform_failure",
        "state_changed": "unknown",
        "safe_to_retry": "after_status_check",
    }
    assert observations["diagnosis"] == {
        "configured_control_timeout_s": 10.0,
        "local_timeout_origin_observed": False,
        "timeout_cause": "source_level_hypothesis_only",
    }
    assert observations["timings"] == [
        {
            "phase": "attempt_total_including_setup_and_cleanup",
            "duration_s": 16.25,
        }
    ]
    assert (run_dir / "manifest.sha256").is_file()
    assert verify_mcp_zup_evidence(
        run_dir,
        expected_result=ExpectedLiveResult.FAIL,
    )["status"] == "FAIL"


def test_fail_verifier_checks_cleanup_and_boundary_before_rejecting_result(tmp_path: Path) -> None:
    environment, _, cleanup = _evidence()
    observations = build_mcp_zup_fail_observations(
        attempt=1,
        completed_call_sequence=["A:workspace.open"],
        attempt_duration_s=16.25,
        configured_control_timeout_s=10.0,
        static_check_passed=True,
        source_sha256=_hash("source"),
        artifact_sha256=_hash("notebook"),
        failure_category="platform_failure",
        failure_state_changed="unknown",
        failure_safe_to_retry="after_status_check",
        runtime_ensure_duration_s=None,
        local_timeout_origin_observed=False,
    )
    cleanup["identities"] = [
        item for item in cleanup["identities"] if item["role"] != "mcp_b"
    ]
    cleanup["all_absent"] = False
    run_dir = tmp_path / "cleanup-fail"
    write_mcp_zup_evidence(run_dir, environment=environment, observations=observations, cleanup=cleanup)
    with pytest.raises(LiveEvidenceError, match="cleanup"):
        verify_mcp_zup_evidence(run_dir, expected_result=ExpectedLiveResult.FAIL)

    environment, _, cleanup = _evidence()
    cleanup["identities"] = [
        item for item in cleanup["identities"] if item["role"] != "mcp_b"
    ]
    observations["diagnosis"]["configured_control_timeout_s"] = 181.0
    other = tmp_path / "boundary-fail"
    write_mcp_zup_evidence(other, environment=environment, observations=observations, cleanup=cleanup)
    with pytest.raises(LiveEvidenceError, match="boundary"):
        verify_mcp_zup_evidence(other, expected_result=ExpectedLiveResult.FAIL)


def test_verifier_requires_a_trusted_expected_result_and_rejects_rehashed_pass(
    tmp_path: Path,
) -> None:
    environment, observations, cleanup = _evidence()
    run_dir = tmp_path / "synthetic-pass"
    write_mcp_zup_evidence(
        run_dir,
        environment=environment,
        observations=observations,
        cleanup=cleanup,
    )

    with pytest.raises(TypeError):
        verify_mcp_zup_evidence(run_dir)
    expected_result = getattr(live_evidence, "ExpectedLiveResult", None)
    assert expected_result is not None
    with pytest.raises(LiveEvidenceError, match="expected result"):
        verify_mcp_zup_evidence(
            run_dir,
            expected_result=expected_result.FAIL,
        )


def test_verifier_cli_requires_expected_result(tmp_path: Path) -> None:
    environment, observations, cleanup = _evidence()
    run_dir = tmp_path / "run"
    write_mcp_zup_evidence(
        run_dir,
        environment=environment,
        observations=observations,
        cleanup=cleanup,
    )

    with pytest.raises(SystemExit):
        live_evidence.main([str(run_dir)])
    assert live_evidence.main(
        [str(run_dir), "--expected-result", "PASS"]
    ) == 0


def test_attempt_two_pass_verifies_explicit_control_contract(tmp_path: Path) -> None:
    environment, observations, cleanup = _evidence()
    environment["attempt"] = 2
    observations["attempt"] = 2
    cleanup["attempt"] = 2
    observations["service"].update(
        {
            "control_timeout_s": 180.0,
            "local_timeout_origin_observed": False,
        }
    )
    run_dir = tmp_path / "attempt-2"
    write_mcp_zup_evidence(
        run_dir,
        environment=environment,
        observations=observations,
        cleanup=cleanup,
    )

    verified = verify_mcp_zup_evidence(
        run_dir,
        expected_result=ExpectedLiveResult.PASS,
    )

    assert verified["attempt"] == 2


def test_attempt_two_fail_builder_retains_measured_ensure_without_timeout_claim() -> None:
    observations = build_mcp_zup_fail_observations(
        attempt=2,
        completed_call_sequence=["A:workspace.open"],
        attempt_duration_s=25.0,
        configured_control_timeout_s=180.0,
        static_check_passed=True,
        source_sha256=_hash("source"),
        artifact_sha256=_hash("notebook"),
        failure_category="platform_failure",
        failure_state_changed="unknown",
        failure_safe_to_retry="after_status_check",
        runtime_ensure_duration_s=20.0,
        local_timeout_origin_observed=False,
    )

    assert observations["attempt"] == 2
    assert observations["runtime_ensure_measurement"] == {
        "observed": True,
        "duration_s": 20.0,
    }
    assert observations["diagnosis"] == {
        "configured_control_timeout_s": 180.0,
        "local_timeout_origin_observed": False,
        "timeout_cause": "not_observed",
    }


def test_attempt_two_terminal_fail_proves_reconnect_without_claiming_main_success(
    tmp_path: Path,
) -> None:
    environment, _, cleanup = _evidence()
    environment["attempt"] = 2
    cleanup["attempt"] = 2
    observations = build_mcp_zup_terminal_fail_observations(
        attempt=2,
        completed_call_sequence=list(EXPECTED_CALL_SEQUENCE[:-1]),
        configured_control_timeout_s=180.0,
        source_sha256=_hash("source"),
        artifact_sha256=_hash("notebook"),
        terminal_state="unknown",
        result_present=False,
        messages=(),
        timings=(
            ("service_start", 0.8),
            ("runtime_ensure", 40.15),
            ("code_run", 0.19),
            ("frontend_reconnect", 0.08),
            ("owned_cleanup", 0.23),
        ),
    )
    run_dir = tmp_path / "attempt-2"
    write_mcp_zup_evidence(
        run_dir,
        environment=environment,
        observations=observations,
        cleanup=cleanup,
    )

    verified = verify_mcp_zup_evidence(
        run_dir,
        expected_result=ExpectedLiveResult.FAIL,
    )

    assert verified == {
        "status": "FAIL",
        "attempt": 2,
        "platform_version": "8.3.27.2170",
        "profile": "zup",
        "terminal_state": "unknown",
        "message_count": 0,
        "result_present": False,
        "frontend_reconnect": True,
        "owned_process_count": 0,
    }


def test_terminal_fail_cleanup_roles_are_order_independent(tmp_path: Path) -> None:
    environment, _, cleanup = _evidence()
    environment["attempt"] = 3
    cleanup["attempt"] = 3
    cleanup["identities"] = list(reversed(cleanup["identities"]))
    observations = build_mcp_zup_terminal_fail_observations(
        attempt=3,
        completed_call_sequence=list(EXPECTED_CALL_SEQUENCE[:-1]),
        configured_control_timeout_s=180.0,
        source_sha256=_hash("source"),
        artifact_sha256=_hash("notebook"),
        terminal_state="unknown",
        result_present=False,
        messages=(),
        timings=(
            ("service_start", 0.8),
            ("runtime_ensure", 27.38),
            ("code_run", 0.16),
            ("frontend_reconnect", 0.11),
            ("owned_cleanup", 0.19),
        ),
    )
    run_dir = tmp_path / "attempt-3"
    write_mcp_zup_evidence(
        run_dir,
        environment=environment,
        observations=observations,
        cleanup=cleanup,
    )

    verified = verify_mcp_zup_evidence(
        run_dir,
        expected_result=ExpectedLiveResult.FAIL,
    )

    assert verified["status"] == "FAIL"


def test_terminal_fail_verifier_rejects_a_forged_completed_operation(
    tmp_path: Path,
) -> None:
    environment, _, cleanup = _evidence()
    environment["attempt"] = 2
    cleanup["attempt"] = 2
    observations = build_mcp_zup_terminal_fail_observations(
        attempt=2,
        completed_call_sequence=list(EXPECTED_CALL_SEQUENCE[:-1]),
        configured_control_timeout_s=180.0,
        source_sha256=_hash("source"),
        artifact_sha256=_hash("notebook"),
        terminal_state="unknown",
        result_present=False,
        messages=(),
        timings=(
            ("service_start", 0.8),
            ("runtime_ensure", 40.15),
            ("code_run", 0.19),
            ("frontend_reconnect", 0.08),
            ("owned_cleanup", 0.23),
        ),
    )
    observations["operation"]["terminal_state"] = "completed"
    run_dir = tmp_path / "forged"
    write_mcp_zup_evidence(
        run_dir,
        environment=environment,
        observations=observations,
        cleanup=cleanup,
    )

    with pytest.raises(LiveEvidenceError, match="terminal"):
        verify_mcp_zup_evidence(
            run_dir,
            expected_result=ExpectedLiveResult.FAIL,
        )


def test_identity_hash_is_salted_stable_and_does_not_embed_private_values() -> None:
    first = evidence_identity("public-salt", "dbgs", "1234", "created", "C:/private/dbgs.exe")
    second = evidence_identity("public-salt", "dbgs", "1234", "created", "C:/private/dbgs.exe")
    other = evidence_identity("different-salt", "dbgs", "1234", "created", "C:/private/dbgs.exe")

    assert first == second
    assert first != other
    assert len(first) == 64
    assert "1234" not in first and "private" not in first


def _refresh_manifest(run_dir: Path, changed_name: str) -> None:
    path = run_dir / "manifest.sha256"
    lines = path.read_text(encoding="ascii").splitlines()
    path.write_text(
        "\n".join(
            f"{sha256((run_dir / changed_name).read_bytes()).hexdigest()}  {changed_name}"
            if line.endswith(f"  {changed_name}")
            else line
            for line in lines
        )
        + "\n",
        encoding="ascii",
    )
