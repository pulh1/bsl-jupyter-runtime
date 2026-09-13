from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from integration.evidence.capture_live_evidence import (
    CAPTURE_PASS_CALL_SEQUENCE,
    CaptureEvidenceError,
    ExpectedCaptureCleanupFacts,
    ExpectedCaptureResult,
    build_capture_live_summary,
    capture_attempt_paths,
    capture_identity_salt,
    load_capture_published_regression_expectation,
    process_identity_sha256,
    verify_capture_live_evidence,
    verify_capture_published_regression,
    write_capture_live_evidence,
)


def _h(label: str) -> str:
    return sha256(label.encode("utf-8")).hexdigest()


def _preflight() -> dict[str, object]:
    return {
        "platform": {
            "version": "8.3.27.2170",
            "bin_identity_sha256": _h("platform-bin"),
            "service_python_identity_sha256": _h("python-path"),
            "service_python_sha256": _h("python-exe"),
            "executables_sha256": {
                "1cv8.exe": _h("1cv8"),
                "1cv8c.exe": _h("1cv8c"),
                "dbgs.exe": _h("dbgs"),
            },
        },
        "source": {
            "tree_sha256": _h("zup-source-tree"),
            "capture_module_sha256": _h("capture-source"),
        },
        "notebook": {
            "artifact_sha256": _h("notebook"),
            "main_source_sha256": _h("main-source"),
            "hypothesis_source_sha256": _h("hypothesis-source"),
        },
        "implementation": {
            "runtime_tree_sha256": _h("runtime-tree"),
            "harness_sha256": _h("harness"),
            "capture_evidence_support_sha256": _h("capture-support"),
            "process_evidence_support_sha256": _h("process-support"),
            "verifier_sha256": _h("verifier"),
            "evaluator_sha256": _h("evaluator"),
        },
    }


def _environment(attempt: int = 1) -> dict[str, object]:
    before = _preflight()
    return {
        "schema": "onec-agent-capture-live-environment-v1",
        "attempt": attempt,
        "profile": "capture",
        "maximum_mode": "experiment",
        "database": {
            "target_identity_sha256": _h("database"),
            "mutation_requested": False,
        },
        "snapshots": {
            name: {"pre": deepcopy(value), "post": deepcopy(value)}
            for name, value in before.items()
        },
    }


def _fence(
    *,
    intent: str,
    operation: str,
    generation: int,
    stop_sequence: int,
) -> dict[str, object]:
    return {
        "capture_intent_identity_sha256": _h(intent),
        "operation_identity_sha256": _h(operation),
        "source_revision": 1,
        "source_sha256": _h("capture-source"),
        "capture_generation": generation,
        "stop_sequence": stop_sequence,
    }


def _capture(
    *,
    name: str,
    intent: str,
    operation: str,
    generation: int,
    stop_sequence: int,
    line: int,
) -> dict[str, object]:
    return {
        "fence": _fence(
            intent=intent,
            operation=operation,
            generation=generation,
            stop_sequence=stop_sequence,
        ),
        "location": {
            "name": name,
            "project": "zup",
            "module": "Payroll",
            "procedure": "Calculate",
            "line": line,
            "executable_line": line,
            "source_revision": 1,
            "source_sha256": _h("capture-source"),
            "module_type_identity_sha256": _h("ConfigModule"),
            "extension_identity_sha256": _h("base-configuration"),
            "object_identity_sha256": _h("payroll-module-object"),
            "property_identity_sha256": _h("common-module-property"),
        },
        "state": "captured",
    }


def _observations() -> dict[str, object]:
    manager = _h("manager-a")
    root_names = [_h("Сумма"), _h("Порог")]
    return {
        "schema": "onec-agent-capture-live-observations-v1",
        "attempt": 1,
        "result": "PASS",
        "call_sequence": list(CAPTURE_PASS_CALL_SEQUENCE),
        "call_metrics": {
            "automatic_hash_calls": 0,
            "full_materialization_calls": 0,
        },
        "code": {
            "main_cell_id": "zup_capture_main",
            "main_revision": 3,
            "main_source_sha256": _h("main-source"),
            "hypothesis_cell_id": "zup_capture_hypothesis",
            "hypothesis_revision": 2,
            "hypothesis_source_sha256": _h("hypothesis-source"),
        },
        "frontend": {
            "sessions": ["A", "B"],
            "replacement_count": 1,
            "frontend_a_identity_sha256": _h("frontend-a"),
            "frontend_b_identity_sha256": _h("frontend-b"),
            "frontend_a_state_before_b": "absent",
            "replayed_operation_identity_sha256": _h("main-operation"),
        },
        "capture_a": _capture(
            name="capture_a",
            intent="intent-a",
            operation="main-operation",
            generation=1,
            stop_sequence=41,
            line=117,
        ),
        "locals": {
            "selected_count": 2,
            "proxy_count": 2,
            "selected_name_sha256s": [_h("manager-local"), _h("description-local")],
            "budget": {
                "profile": "agent_metadata",
                "max_depth": 1,
                "max_items": 20,
                "max_rows": 20,
                "max_bytes": 16384,
                "timeout_ms": 1000,
                "cost_class": "metadata",
            },
        },
        "manager": {
            "origin": "frame_local",
            "alias": "manager_a",
            "origin_local_name_sha256": _h("manager-local"),
            "table_name_sha256": _h("payroll-table"),
            "manager_identity_sha256": manager,
            "table_count": 3,
        },
        "table_a": {
            "alias": "table_a",
            "table_name_sha256": _h("payroll-table"),
            "manager_identity_sha256": manager,
            "proxy_identity_sha256": _h("table-a-proxy"),
            "known_size": 1200,
            "schema_column_count": 12,
            "automatic_hash_calls": 0,
            "full_materialization_calls": 0,
            "head": {
                "requested_rows": 5,
                "returned_rows": 5,
                "returned_columns": 4,
                "transfer_bytes": 4096,
                "dataframe_identity_sha256": _h("head-dataframe"),
            },
            "budget": {
                "profile": "agent_dataframe",
                "max_depth": 8,
                "max_items": 200000,
                "max_rows": 10000,
                "max_bytes": 67108864,
                "timeout_ms": 30000,
                "cost_class": "full_scan",
            },
        },
        "hypothesis": {
            "operation_identity_sha256": _h("hypothesis-operation"),
            "operation_state": "captured",
            "observation_state": "partial",
            "failure_stage": "observation",
            "capture_state_before": "captured",
            "capture_state_after": "captured",
            "capture_generation_before": 1,
            "capture_generation_after": 1,
            "dirty_root_count": 2,
        },
        "continuation": {
            "operation_identity_sha256": _h("continue-operation"),
            "call_count": 1,
            "continue_state": "acknowledged",
            "dirty_root_name_sha256s": root_names,
            "acknowledged_root_name_sha256s": list(root_names),
            "old_proxy_check_count": 4,
            "old_proxy_stale_count": 4,
            "old_proxy_identity_sha256s": [
                _h("manager-local-proxy"),
                _h("description-local-proxy"),
                _h("table-a-proxy"),
                _h("head-dataframe"),
            ],
            "stale_proxy_identity_sha256s": [
                _h("manager-local-proxy"),
                _h("description-local-proxy"),
                _h("table-a-proxy"),
                _h("head-dataframe"),
            ],
            "old_proxy_capture_generation": 1,
        },
        "capture_b": _capture(
            name="capture_b",
            intent="intent-b",
            operation="continue-operation",
            generation=2,
            stop_sequence=58,
            line=164,
        ),
        "downstream": {
            "manager_alias": "manager_b",
            "table_alias": "table_b",
            "result_local_name_sha256": _h("downstream-result-local"),
            "table_name_sha256": _h("payroll-table"),
            "table_proxy_identity_sha256": _h("table-b-proxy"),
            "result_proxy_identity_sha256": _h("result-b-proxy"),
            "selected_rows": 5,
            "requested_rows": 5,
            "selected_columns": 4,
            "automatic_hash_calls": 0,
            "full_materialization_calls": 0,
            "budget": {
                "profile": "agent_dataframe",
                "max_depth": 8,
                "max_items": 200000,
                "max_rows": 10000,
                "max_bytes": 67108864,
                "timeout_ms": 30000,
                "cost_class": "full_scan",
            },
        },
        "terminal": {
            "origin_main_operation_identity_sha256": _h("main-operation"),
            "continuation_operation_identity_sha256": _h("continue-operation"),
            "terminal_operation_identity_sha256": _h("continue-operation"),
            "origin_capture_generation": 2,
            "finish_continue_call_count": 1,
            "state": "completed",
            "capture_present": False,
            "next_event_cursor": 73,
        },
    }


def _owned_processes() -> list[dict[str, object]]:
    return [
        {
            "role": role,
            "pid": 4000 + index,
            "create_time": 1000.0 + index,
            "exe": rf"C:\Program Files\1cv8\{role}.exe",
        }
        for index, role in enumerate(
            ("service", "mcp_a", "mcp_b", "designer", "dbgs", "onec")
        )
    ]


def _unrelated_processes() -> list[dict[str, object]]:
    return [
        {
            "role": "unrelated",
            "pid": 9001,
            "create_time": 777.0,
            "exe": r"C:\Windows\System32\notepad.exe",
        }
    ]


def _cleanup(
    owned: list[dict[str, object]] | None = None,
    unrelated: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    owned = _owned_processes() if owned is None else owned
    unrelated = _unrelated_processes() if unrelated is None else unrelated
    return {
        "schema": "onec-agent-capture-live-cleanup-v1",
        "attempt": 1,
        "runtime_owner_state": "absent",
        "owned": [
            {
                "role": item["role"],
                "identity_sha256": process_identity_sha256(1, item),
                "state": "absent",
            }
            for item in owned
        ],
        "unrelated": [
            {
                "identity_sha256": process_identity_sha256(1, item),
                "state": "alive",
            }
            for item in unrelated
        ],
        "errors": [],
    }


def _expected_cleanup_facts(
    *,
    owned_roles: tuple[str, ...] = (
        "service",
        "mcp_a",
        "mcp_b",
        "designer",
        "dbgs",
        "onec",
    ),
    errors: tuple[str, ...] = (),
    unrelated_states: tuple[str, ...] = ("alive",),
) -> ExpectedCaptureCleanupFacts:
    return ExpectedCaptureCleanupFacts(
        errors=errors,
        cleanup_error_count=len(errors),
        runtime_owner_state="absent",
        owned_roles=owned_roles,
        owned_states=("absent",) * len(owned_roles),
        unrelated_states=unrelated_states,
    )


def _write_pass(tmp_path: Path) -> Path:
    destination = tmp_path / "attempt-1"
    write_capture_live_evidence(
        destination,
        environment=_environment(),
        observations=_observations(),
        cleanup=_cleanup(),
    )
    return destination


def _rewrite_bundle(
    destination: Path, filename: str, value: object, *, attempt: int = 1
) -> None:
    (destination / filename).write_text(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    names = ("environment.json", "observations.json", "cleanup.json", "summary.json")
    (destination / "manifest.sha256").write_text(
        ("" if attempt == 1 else "# onec-agent-capture-live-attempt=2\n")
        + "".join(
            f"{sha256((destination / name).read_bytes()).hexdigest()}  {name}\n"
            for name in names
        ),
        encoding="ascii",
        newline="\n",
    )


def _resign_bundle(
    destination: Path, filename: str, value: object, *, attempt: int = 1
) -> None:
    """Re-sign a forged bundle so tests reach semantic/external validation."""
    (destination / filename).write_text(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    environment = json.loads(
        (destination / "environment.json").read_text(encoding="utf-8")
    )
    observations = json.loads(
        (destination / "observations.json").read_text(encoding="utf-8")
    )
    cleanup = json.loads(
        (destination / "cleanup.json").read_text(encoding="utf-8")
    )
    summary = build_capture_live_summary(
        attempt=attempt,
        environment=environment,
        observations=observations,
        cleanup=cleanup,
    )
    (destination / "summary.json").write_text(
        json.dumps(
            summary,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    _rewrite_bundle(destination, "summary.json", summary, attempt=attempt)


def _verify_pass(destination: Path) -> dict[str, object]:
    return verify_capture_live_evidence(
        destination,
        expected_result=ExpectedCaptureResult.PASS,
        expected_cleanup_facts=_expected_cleanup_facts(),
        expected_preflight=_preflight(),
        expected_owned_processes=_owned_processes(),
        expected_unrelated_processes=_unrelated_processes(),
        expected_database_identity_sha256=_h("database"),
        expected_pass_facts=_observations(),
    )


def test_verifier_requires_typed_external_cleanup_facts(tmp_path: Path) -> None:
    destination = _write_pass(tmp_path)
    arguments = {
        "expected_result": ExpectedCaptureResult.PASS,
        "expected_preflight": _preflight(),
        "expected_owned_processes": _owned_processes(),
        "expected_unrelated_processes": _unrelated_processes(),
        "expected_database_identity_sha256": _h("database"),
        "expected_pass_facts": _observations(),
    }

    with pytest.raises(TypeError, match="ExpectedCaptureCleanupFacts"):
        verify_capture_live_evidence(
            destination,
            expected_cleanup_facts={},  # type: ignore[arg-type]
            **arguments,
        )

    verified = verify_capture_live_evidence(
        destination,
        expected_cleanup_facts=_expected_cleanup_facts(),
        **arguments,
    )
    assert verified["status"] == "PASS"


@pytest.mark.parametrize(
    "changed_errors",
    [[], ["GracefulShutdownTimeout"]],
)
def test_verifier_rejects_resigned_fail_cleanup_error_mutation(
    tmp_path: Path, changed_errors: list[str]
) -> None:
    owned = [_owned_processes()[0]]
    observations = {
        "schema": "onec-agent-capture-live-observations-v1",
        "attempt": 1,
        "result": "FAIL",
        "call_sequence": list(CAPTURE_PASS_CALL_SEQUENCE[:1]),
        "failure": {
            "boundary": "runtime_start",
            "type": "SanitizedPlatformFailure",
            "last_completed_phase": "workspace_open",
        },
    }
    cleanup = _cleanup(owned=owned)
    cleanup["errors"] = ["ServiceShutdownTimeout"]
    destination = tmp_path / "attempt-1"
    write_capture_live_evidence(
        destination,
        environment=_environment(),
        observations=observations,
        cleanup=cleanup,
    )
    cleanup["errors"] = changed_errors
    _resign_bundle(destination, "cleanup.json", cleanup)

    with pytest.raises(CaptureEvidenceError, match="cleanup"):
        verify_capture_live_evidence(
            destination,
            expected_result=ExpectedCaptureResult.FAIL,
            expected_cleanup_facts=_expected_cleanup_facts(
                owned_roles=("service",),
                errors=("ServiceShutdownTimeout",),
            ),
            expected_preflight=_preflight(),
            expected_owned_processes=owned,
            expected_unrelated_processes=_unrelated_processes(),
            expected_database_identity_sha256=_h("database"),
            expected_fail_facts=observations,
        )


def test_committed_attempt_one_has_fresh_checkout_public_regression() -> None:
    workspace = Path(__file__).parents[2]
    evidence_root = (
        workspace
        / "docs"
        / "research"
        / "evidence"
        / "2026-08-20-mcp-capture"
    )
    expectation = load_capture_published_regression_expectation(
        evidence_root / "attempt-1-public-regression.json"
    )

    verified = verify_capture_published_regression(
        evidence_root / "attempt-1",
        expectation=expectation,
    )

    assert verified["status"] == "FAIL"
    assert verified["cleanup_error_count"] == 1
    assert expectation.attempt == 1
    assert expectation.provenance == "posthoc_from_published_bundle"


def test_published_regression_cli_reports_its_limited_scope() -> None:
    workspace = Path(__file__).parents[2]
    evidence_root = (
        workspace
        / "docs"
        / "research"
        / "evidence"
        / "2026-08-20-mcp-capture"
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "integration.evidence.capture_live_evidence",
            "--published-bundle",
            str(evidence_root / "attempt-1"),
            "--expectation",
            str(evidence_root / "attempt-1-public-regression.json"),
        ],
        cwd=workspace,
        env=os.environ | {"PYTHONPATH": "src"},
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "cleanup_error_count": 1,
        "historical_private_inputs_verified": False,
        "scope": "posthoc_public_bundle_regression",
        "status": "FAIL",
    }


def test_published_regression_cli_rejects_cross_attempt_renaming(
    tmp_path: Path,
) -> None:
    workspace = Path(__file__).parents[2]
    evidence_root = (
        workspace
        / "docs"
        / "research"
        / "evidence"
        / "2026-08-20-mcp-capture"
    )
    substituted = tmp_path / "attempt-2"
    shutil.copytree(evidence_root / "attempt-1", substituted)
    expectation = tmp_path / "attempt-2-public-regression.json"
    shutil.copy2(evidence_root / "attempt-1-public-regression.json", expectation)

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "integration.evidence.capture_live_evidence",
            "--published-bundle",
            str(substituted),
            "--expectation",
            str(expectation),
        ],
        cwd=workspace,
        env=os.environ | {"PYTHONPATH": "src"},
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "attempt" in completed.stderr


def test_verifier_accepts_exact_two_capture_bundle_and_derives_summary(
    tmp_path: Path,
) -> None:
    destination = _write_pass(tmp_path)

    verified = _verify_pass(destination)

    assert verified == build_capture_live_summary(
        environment=_environment(),
        observations=_observations(),
        cleanup=_cleanup(),
    )
    assert verified["status"] == "PASS"
    assert verified["capture_generations"] == [1, 2]
    assert verified["continuation_calls"] == 1
    assert verified["owned_process_count_after_cleanup"] == 0


@pytest.mark.parametrize(
    ("section", "path", "changed"),
    [
        ("observations.json", ("capture_b", "fence", "capture_generation"), 1),
        ("observations.json", ("capture_b", "fence", "stop_sequence"), 41),
        ("observations.json", ("capture_a", "fence", "source_sha256"), _h("other")),
        ("observations.json", ("capture_b", "fence", "operation_identity_sha256"), _h("stale-main")),
        ("observations.json", ("manager", "origin"), "runtime"),
        ("observations.json", ("table_a", "automatic_hash_calls"), 1),
        ("observations.json", ("table_a", "head", "requested_rows"), 5000),
        ("observations.json", ("hypothesis", "capture_state_after"), "completed"),
        ("observations.json", ("continuation", "call_count"), 2),
        ("observations.json", ("continuation", "old_proxy_stale_count"), 3),
        ("observations.json", ("terminal", "state"), "captured"),
        ("cleanup.json", ("owned", 0, "state"), "alive"),
    ],
)
def test_verifier_rejects_rehashed_semantic_mutations(
    tmp_path: Path,
    section: str,
    path: tuple[object, ...],
    changed: object,
) -> None:
    destination = _write_pass(tmp_path)
    value = json.loads((destination / section).read_text(encoding="utf-8"))
    target: object = value
    for part in path[:-1]:
        target = target[part]  # type: ignore[index]
    target[path[-1]] = changed  # type: ignore[index]
    _rewrite_bundle(destination, section, value)

    with pytest.raises(CaptureEvidenceError):
        _verify_pass(destination)


@pytest.mark.parametrize(
    ("section", "path", "changed"),
    [
        ("environment.json", ("database", "target_identity_sha256"), _h("forged-db")),
        ("observations.json", ("code", "main_revision"), 4),
        ("observations.json", ("capture_a", "location", "module"), "OtherModule"),
        ("observations.json", ("capture_a", "location", "procedure"), "OtherProcedure"),
        ("observations.json", ("capture_b", "location", "line"), 999),
        ("observations.json", ("manager", "table_count"), 4),
        ("observations.json", ("downstream", "result_proxy_identity_sha256"), _h("forged-result")),
        ("observations.json", ("terminal", "next_event_cursor"), 74),
    ],
)
def test_verifier_rejects_resigned_external_fact_forgeries(
    tmp_path: Path,
    section: str,
    path: tuple[object, ...],
    changed: object,
) -> None:
    destination = _write_pass(tmp_path)
    value = json.loads((destination / section).read_text(encoding="utf-8"))
    target: object = value
    for part in path[:-1]:
        target = target[part]  # type: ignore[index]
    target[path[-1]] = changed  # type: ignore[index]
    _resign_bundle(destination, section, value)

    with pytest.raises(CaptureEvidenceError):
        _verify_pass(destination)


def test_raw_mcp_privacy_uses_external_markers_and_exact_owned_pids() -> None:
    from integration.evidence.capture_live_evidence import assert_mcp_response_private_safe

    for unsafe in (
        {"value": "external-secret-token"},
        {"value": "prefix PRIVATE-SOURCE-MARKER suffix"},
        {"value": 45123},
        {"nested": [{"owner": "PID 45123"}]},
    ):
        with pytest.raises(CaptureEvidenceError, match="private"):
            assert_mcp_response_private_safe(
                unsafe,
                private_markers=("external-secret-token", "PRIVATE-SOURCE-MARKER"),
                owned_pids=(45123,),
            )


def test_raw_mcp_privacy_distinguishes_opaque_evidence_hashes_from_pid_leaks() -> None:
    from integration.evidence.capture_live_evidence import assert_mcp_response_private_safe

    collision = "b4af827076f077786b77be9101b81da254e79e38bd3ed29f5b253975fcf27a42"
    for field in (
        "artifact_sha256",
        "bin_identity_sha256",
        "capture_evidence_support_sha256",
        "capture_intent_identity_sha256",
        "capture_module_sha256",
        "continuation_operation_identity_sha256",
        "dataframe_identity_sha256",
        "diagnostic_id",
        "document_sha256",
        "evaluator_sha256",
        "executed_source_sha256",
        "execution_artifact_sha256",
        "extension_identity_sha256",
        "frontend_a_identity_sha256",
        "frontend_b_identity_sha256",
        "harness_sha256",
        "hypothesis_source_sha256",
        "identity_sha256",
        "inputs_sha256",
        "lowered_source_sha256",
        "main_source_sha256",
        "manager_identity_sha256",
        "module_type_identity_sha256",
        "object_identity_sha256",
        "operation_identity_sha256",
        "origin_local_name_sha256",
        "origin_main_operation_identity_sha256",
        "platform_diagnostic_sha256",
        "process_evidence_support_sha256",
        "property_identity_sha256",
        "proxy_identity_sha256",
        "replayed_operation_identity_sha256",
        "result_local_name_sha256",
        "result_proxy_identity_sha256",
        "runtime_tree_sha256",
        "service_python_identity_sha256",
        "service_python_sha256",
        "source_map_sha256",
        "source_sha256",
        "statement_source_sha256",
        "table_name_sha256",
        "table_proxy_identity_sha256",
        "target_identity_sha256",
        "terminal_operation_identity_sha256",
        "tree_sha256",
        "verifier_sha256",
        "visible_source_sha256",
        "worker_manifest_sha256",
        "worker_source_sha256",
    ):
        assert_mcp_response_private_safe(
            {field: collision},
            private_markers=(),
            owned_pids=(9101,),
        )

    for parent, child in (
        ("executables_sha256", "1cv8.exe"),
        ("source_files_sha256", "environment.json"),
        ("bundle_file_sha256", "manifest.sha256"),
    ):
        assert_mcp_response_private_safe(
            {parent: {child: collision}},
            private_markers=(),
            owned_pids=(9101,),
        )

    for parent in (
        "selected_name_sha256s",
        "dirty_root_name_sha256s",
        "acknowledged_root_name_sha256s",
        "old_proxy_identity_sha256s",
        "stale_proxy_identity_sha256s",
    ):
        assert_mcp_response_private_safe(
            {parent: [collision]},
            private_markers=(),
            owned_pids=(9101,),
        )

    for unsafe in (
        {"process": 9101},
        {"process": "raw process PID 9101"},
        {"identity_sha256": 9101},
        {"identity_sha256": "raw process PID 9101"},
        {"identity_sha256": collision.upper()},
        {"identity_sha256": collision[:-1]},
        {"identity_sha256": f"g{collision[1:]}"},
        {"unrecognized_sha256": collision},
        {"unrecognized_hashes": {"environment.json": collision}},
        {"source_files_sha256": {"unexpected.json": collision}},
        {"source_files_sha256": {"environment.json": collision.upper()}},
        {"source_files_sha256": [collision]},
        {"selected_name_sha256s": {"0": collision}},
    ):
        with pytest.raises(CaptureEvidenceError, match="private"):
            assert_mcp_response_private_safe(
                unsafe,
                private_markers=(),
                owned_pids=(9101,),
            )

    for marker in ("b4af", "identity_sha256"):
        with pytest.raises(CaptureEvidenceError, match="private"):
            assert_mcp_response_private_safe(
                {"identity_sha256": collision},
                private_markers=(marker,),
                owned_pids=(9101,),
            )

    with pytest.raises(CaptureEvidenceError, match="private"):
        assert_mcp_response_private_safe(
            {"source_files_sha256": {"environment.json": collision}},
            private_markers=("environment",),
            owned_pids=(9101,),
        )


@pytest.mark.parametrize(
    ("method", "response", "expected_private_fields"),
    [
        (
            "workspace.open",
            {
                "ok": True,
                "value": {
                    "workspace_id": "workspace",
                    "project_name": "private-workspace",
                    "project_root": r"C:\repo\private-workspace",
                    "current_runtime_id": None,
                    "capabilities": [],
                },
                "failure": None,
            },
            {"project_root": r"C:\repo\private-workspace"},
        ),
        (
            "code.get",
            {
                "ok": True,
                "value": {
                    "cell_id": "capture-main",
                    "revision": 3,
                    "source": "PRIVATE-SOURCE-MARKER",
                    "source_sha256": _h("approved-source"),
                    "document_sha256": _h("approved-notebook"),
                    "language": "bsl",
                    "mode": "main",
                    "outputs": [],
                },
                "failure": None,
            },
            {"source": "PRIVATE-SOURCE-MARKER"},
        ),
    ],
)
def test_raw_mcp_privacy_allows_expected_secret_only_in_exact_contract_field(
    method: str,
    response: dict[str, object],
    expected_private_fields: dict[str, str],
) -> None:
    from integration.evidence.capture_live_evidence import assert_mcp_response_private_safe

    assert_mcp_response_private_safe(
        response,
        method=method,
        expected_private_fields=expected_private_fields,
        private_markers=(
            r"C:\repo\private-workspace",
            "PRIVATE-SOURCE-MARKER",
            "D" * 6960,
        ),
        owned_pids=(),
    )


@pytest.mark.parametrize(
    ("method", "response", "expected_private_fields"),
    [
        (
            "workspace.open",
            {
                "ok": True,
                "value": {
                    "project_root": r"C:\repo\private-workspace",
                    "note": r"leaked C:\repo\private-workspace",
                },
                "failure": None,
            },
            {"project_root": r"C:\repo\private-workspace"},
        ),
        (
            "code.get",
            {
                "ok": True,
                "value": {
                    "source": "PRIVATE-SOURCE-MARKER",
                    "metadata": {"copy": "PRIVATE-SOURCE-MARKER"},
                },
                "failure": None,
            },
            {"source": "PRIVATE-SOURCE-MARKER"},
        ),
        (
            "workspace.open",
            {
                "ok": True,
                "value": {
                    "project_root": r"C:\repo\private-workspace\unexpected",
                },
                "failure": None,
            },
            {"project_root": r"C:\repo\private-workspace"},
        ),
        (
            "code.list",
            {
                "ok": True,
                "value": [{"source": "PRIVATE-SOURCE-MARKER"}],
                "failure": None,
            },
            {"source": "PRIVATE-SOURCE-MARKER"},
        ),
    ],
)
def test_raw_mcp_privacy_rejects_expected_secret_outside_exact_contract_field(
    method: str,
    response: dict[str, object],
    expected_private_fields: dict[str, str],
) -> None:
    from integration.evidence.capture_live_evidence import assert_mcp_response_private_safe

    with pytest.raises(CaptureEvidenceError, match="private"):
        assert_mcp_response_private_safe(
            response,
            method=method,
            expected_private_fields=expected_private_fields,
            private_markers=(r"C:\repo\private-workspace", "PRIVATE-SOURCE-MARKER"),
            owned_pids=(),
        )


def test_raw_mcp_privacy_rejects_snapshotted_external_pid_without_owning_it() -> None:
    from integration.evidence.capture_live_evidence import assert_mcp_response_private_safe

    for unsafe in ({"value": 58241}, {"value": "external PID 58241"}):
        with pytest.raises(CaptureEvidenceError, match="private"):
            assert_mcp_response_private_safe(
                unsafe,
                method="operation.wait",
                expected_private_fields={},
                private_markers=(),
                owned_pids=(),
                external_pids=(58241,),
            )


def test_public_verifier_rejects_values_matching_external_private_markers_and_pids(
    tmp_path: Path,
) -> None:
    destination = _write_pass(tmp_path)
    observations = json.loads(
        (destination / "observations.json").read_text(encoding="utf-8")
    )
    observations["terminal"]["next_event_cursor"] = 9001
    _resign_bundle(destination, "observations.json", observations)

    with pytest.raises(CaptureEvidenceError, match="private"):
        verify_capture_live_evidence(
            destination,
            expected_result=ExpectedCaptureResult.PASS,
            expected_cleanup_facts=_expected_cleanup_facts(),
            expected_preflight=_preflight(),
            expected_owned_processes=_owned_processes(),
            expected_unrelated_processes=_unrelated_processes(),
            expected_database_identity_sha256=_h("database"),
            expected_pass_facts=observations,
            private_markers=("external-secret-token",),
            private_pids=(9001,),
        )


def test_verifier_requires_external_result_and_trusted_preflight(
    tmp_path: Path,
) -> None:
    destination = _write_pass(tmp_path)
    with pytest.raises((CaptureEvidenceError, TypeError)):
        verify_capture_live_evidence(  # type: ignore[arg-type]
            destination,
            expected_result="PASS",
            expected_cleanup_facts=_expected_cleanup_facts(),
            expected_preflight=_preflight(),
            expected_owned_processes=_owned_processes(),
            expected_unrelated_processes=_unrelated_processes(),
            expected_database_identity_sha256=_h("database"),
            expected_pass_facts=_observations(),
        )
    expected = _preflight()
    expected["implementation"]["harness_sha256"] = _h("changed")  # type: ignore[index]
    with pytest.raises(CaptureEvidenceError, match="preflight"):
        verify_capture_live_evidence(
            destination,
            expected_result=ExpectedCaptureResult.PASS,
            expected_cleanup_facts=_expected_cleanup_facts(),
            expected_preflight=expected,
            expected_owned_processes=_owned_processes(),
            expected_unrelated_processes=_unrelated_processes(),
            expected_database_identity_sha256=_h("database"),
            expected_pass_facts=_observations(),
        )


def test_verifier_rejects_snapshot_drift_even_after_rehash(tmp_path: Path) -> None:
    destination = _write_pass(tmp_path)
    environment = json.loads(
        (destination / "environment.json").read_text(encoding="utf-8")
    )
    environment["snapshots"]["source"]["post"]["tree_sha256"] = _h("drift")
    _rewrite_bundle(destination, "environment.json", environment)

    with pytest.raises(CaptureEvidenceError, match="changed"):
        _verify_pass(destination)


def test_manifest_and_json_are_canonical_exact_and_duplicate_safe(
    tmp_path: Path,
) -> None:
    destination = _write_pass(tmp_path)
    manifest = destination / "manifest.sha256"
    manifest.write_text(
        manifest.read_text(encoding="ascii")
        + manifest.read_text(encoding="ascii").splitlines(keepends=True)[0],
        encoding="ascii",
        newline="\n",
    )
    with pytest.raises(CaptureEvidenceError, match="manifest"):
        _verify_pass(destination)

    destination = tmp_path / "duplicate-json" / "attempt-1"
    write_capture_live_evidence(
        destination,
        environment=_environment(),
        observations=_observations(),
        cleanup=_cleanup(),
    )
    path = destination / "environment.json"
    raw = path.read_text(encoding="utf-8")
    path.write_text(
        raw.replace('"attempt":1,', '"attempt":1,"attempt":1,', 1),
        encoding="utf-8",
        newline="\n",
    )
    names = ("environment.json", "observations.json", "cleanup.json", "summary.json")
    (destination / "manifest.sha256").write_text(
        "".join(
            f"{sha256((destination / name).read_bytes()).hexdigest()}  {name}\n"
            for name in names
        ),
        encoding="ascii",
        newline="\n",
    )
    with pytest.raises(CaptureEvidenceError, match="duplicate"):
        _verify_pass(destination)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("token", "secret"),
        ("pid", 4242),
        ("detail", "PID=4242"),
        ("transport", "raw RDBG reply"),
        ("artifact_path", r"C:\private\artifact.json"),
        ("raw_source", "Процедура Выполнить()"),
        ("business_rows", [{"employee": "Private"}]),
        ("platform_uuid", "123e4567-e89b-12d3-a456-426614174000"),
        ("runtime_id", "runtime-private"),
        ("private_artifact", ".runtime/private.json"),
    ],
)
def test_writer_recursively_rejects_private_evidence(
    tmp_path: Path, key: str, value: object
) -> None:
    observations = _observations()
    observations["capture_a"]["location"][key] = value  # type: ignore[index]

    with pytest.raises(CaptureEvidenceError, match="private"):
        write_capture_live_evidence(
            tmp_path / key / "attempt-1",
            environment=_environment(),
            observations=observations,
            cleanup=_cleanup(),
        )


def test_external_exact_process_identities_and_unrelated_preservation_are_bound(
    tmp_path: Path,
) -> None:
    destination = _write_pass(tmp_path)
    changed = _owned_processes()
    changed[0]["create_time"] = 9999.0
    with pytest.raises(CaptureEvidenceError, match="owned process"):
        verify_capture_live_evidence(
            destination,
            expected_result=ExpectedCaptureResult.PASS,
            expected_cleanup_facts=_expected_cleanup_facts(),
            expected_preflight=_preflight(),
            expected_owned_processes=changed,
            expected_unrelated_processes=_unrelated_processes(),
            expected_database_identity_sha256=_h("database"),
            expected_pass_facts=_observations(),
        )
    changed_unrelated = _unrelated_processes()
    changed_unrelated[0]["pid"] = 9002
    with pytest.raises(CaptureEvidenceError, match="unrelated process"):
        verify_capture_live_evidence(
            destination,
            expected_result=ExpectedCaptureResult.PASS,
            expected_cleanup_facts=_expected_cleanup_facts(),
            expected_preflight=_preflight(),
            expected_owned_processes=_owned_processes(),
            expected_unrelated_processes=changed_unrelated,
            expected_database_identity_sha256=_h("database"),
            expected_pass_facts=_observations(),
        )


def test_fail_is_immutable_first_class_evidence_under_external_fail_expectation(
    tmp_path: Path,
) -> None:
    owned = [_owned_processes()[0]]
    observations = {
        "schema": "onec-agent-capture-live-observations-v1",
        "attempt": 1,
        "result": "FAIL",
        "call_sequence": list(CAPTURE_PASS_CALL_SEQUENCE[:1]),
        "failure": {
            "boundary": "runtime_start",
            "type": "sanitized_platform_failure",
            "last_completed_phase": "workspace_open",
        },
    }
    destination = tmp_path / "attempt-1"
    write_capture_live_evidence(
        destination,
        environment=_environment(),
        observations=observations,
        cleanup=_cleanup(owned=owned),
    )

    verified = verify_capture_live_evidence(
        destination,
        expected_result=ExpectedCaptureResult.FAIL,
        expected_cleanup_facts=_expected_cleanup_facts(owned_roles=("service",)),
        expected_preflight=_preflight(),
        expected_owned_processes=owned,
        expected_unrelated_processes=_unrelated_processes(),
        expected_database_identity_sha256=_h("database"),
        expected_fail_facts=observations,
    )
    assert verified["status"] == "FAIL"
    assert verified["failure_boundary"] == "runtime_start"
    with pytest.raises(CaptureEvidenceError):
        _verify_pass(destination)
    with pytest.raises(CaptureEvidenceError, match="exists"):
        write_capture_live_evidence(
            destination,
            environment=_environment(),
            observations=observations,
            cleanup=_cleanup(owned=owned),
        )


def test_fail_verification_requires_external_fail_facts(tmp_path: Path) -> None:
    owned = [_owned_processes()[0]]
    observations = {
        "schema": "onec-agent-capture-live-observations-v1",
        "attempt": 1,
        "result": "FAIL",
        "call_sequence": list(CAPTURE_PASS_CALL_SEQUENCE[:1]),
        "failure": {
            "boundary": "runtime_start",
            "type": "SanitizedPlatformFailure",
            "last_completed_phase": "workspace_open",
        },
    }
    destination = tmp_path / "attempt-1"
    write_capture_live_evidence(
        destination,
        environment=_environment(),
        observations=observations,
        cleanup=_cleanup(owned=owned),
    )

    with pytest.raises(CaptureEvidenceError, match="FAIL requires external trusted facts"):
        verify_capture_live_evidence(
            destination,
            expected_result=ExpectedCaptureResult.FAIL,
            expected_cleanup_facts=_expected_cleanup_facts(
                owned_roles=("service",)
            ),
            expected_preflight=_preflight(),
            expected_owned_processes=owned,
            expected_unrelated_processes=_unrelated_processes(),
            expected_database_identity_sha256=_h("database"),
        )


def test_attempt_paths_and_identity_hashes_are_disjoint_for_exact_attempts_one_and_two(
    tmp_path: Path,
) -> None:
    attempt_one = capture_attempt_paths(tmp_path, attempt=1)
    attempt_two = capture_attempt_paths(tmp_path, attempt=2)

    assert attempt_one.public.name == attempt_one.private.name == "attempt-1"
    assert attempt_two.public.name == attempt_two.private.name == "attempt-2"
    assert len(
        {
            attempt_one.public,
            attempt_one.private,
            attempt_two.public,
            attempt_two.private,
        }
    ) == 4
    assert capture_identity_salt(1) == "onec-agent-capture-2026-08-20-attempt-1"
    assert capture_identity_salt(2) == "onec-agent-capture-2026-08-20-attempt-2"
    assert process_identity_sha256(1, _owned_processes()[0]) != process_identity_sha256(
        2, _owned_processes()[0]
    )
    for forbidden in (True, 0, 3):
        with pytest.raises((TypeError, ValueError), match="attempt"):
            capture_attempt_paths(tmp_path, attempt=forbidden)  # type: ignore[arg-type]
        with pytest.raises((TypeError, ValueError), match="attempt"):
            capture_identity_salt(forbidden)  # type: ignore[arg-type]
        with pytest.raises((TypeError, ValueError), match="attempt"):
            process_identity_sha256(
                forbidden, _owned_processes()[0]  # type: ignore[arg-type]
            )


def _attempt_two_pass_fixture() -> tuple[
    dict[str, object],
    dict[str, object],
    dict[str, object],
]:
    environment = _environment(attempt=2)
    observations = deepcopy(_observations())
    observations["attempt"] = 2
    cleanup = _cleanup()
    cleanup["attempt"] = 2
    cleanup["owned"] = [
        {
            "role": item["role"],
            "identity_sha256": process_identity_sha256(2, item),
            "state": "absent",
        }
        for item in _owned_processes()
    ]
    cleanup["unrelated"] = [
        {
            "identity_sha256": process_identity_sha256(2, item),
            "state": "alive",
        }
        for item in _unrelated_processes()
    ]
    return environment, observations, cleanup


def test_attempt_two_pass_and_fail_bundles_are_deterministic_and_exactly_bound(
    tmp_path: Path,
) -> None:
    environment, observations, cleanup = _attempt_two_pass_fixture()
    first = tmp_path / "first" / "attempt-2"
    second = tmp_path / "second" / "attempt-2"
    for destination in (first, second):
        write_capture_live_evidence(
            destination,
            attempt=2,
            environment=environment,
            observations=observations,
            cleanup=cleanup,
        )
    assert {
        item.name: item.read_bytes() for item in first.iterdir()
    } == {
        item.name: item.read_bytes() for item in second.iterdir()
    }
    assert (first / "manifest.sha256").read_text(encoding="ascii").startswith(
        "# onec-agent-capture-live-attempt=2\n"
    )
    verified = verify_capture_live_evidence(
        first,
        attempt=2,
        expected_result=ExpectedCaptureResult.PASS,
        expected_cleanup_facts=_expected_cleanup_facts(),
        expected_preflight=_preflight(),
        expected_owned_processes=_owned_processes(),
        expected_unrelated_processes=_unrelated_processes(),
        expected_database_identity_sha256=_h("database"),
        expected_pass_facts=observations,
    )
    assert verified["attempt"] == 2

    fail_observations = {
        "schema": "onec-agent-capture-live-observations-v1",
        "attempt": 2,
        "result": "FAIL",
        "call_sequence": list(CAPTURE_PASS_CALL_SEQUENCE[:1]),
        "failure": {
            "boundary": "runtime_start",
            "type": "SanitizedPlatformFailure",
            "last_completed_phase": "workspace_open",
        },
    }
    fail_cleanup = deepcopy(cleanup)
    fail_cleanup["owned"] = fail_cleanup["owned"][:1]  # type: ignore[index]
    fail_cleanup["errors"] = ["ServiceShutdownTimeout"]
    fail_destination = tmp_path / "fail-first" / "attempt-2"
    fail_copy = tmp_path / "fail-second" / "attempt-2"
    for destination in (fail_destination, fail_copy):
        write_capture_live_evidence(
            destination,
            attempt=2,
            environment=environment,
            observations=fail_observations,
            cleanup=fail_cleanup,
        )
    assert {
        item.name: item.read_bytes() for item in fail_destination.iterdir()
    } == {
        item.name: item.read_bytes() for item in fail_copy.iterdir()
    }
    fail_verified = verify_capture_live_evidence(
        fail_destination,
        attempt=2,
        expected_result=ExpectedCaptureResult.FAIL,
        expected_cleanup_facts=_expected_cleanup_facts(
            owned_roles=("service",), errors=("ServiceShutdownTimeout",)
        ),
        expected_preflight=_preflight(),
        expected_owned_processes=_owned_processes()[:1],
        expected_unrelated_processes=_unrelated_processes(),
        expected_database_identity_sha256=_h("database"),
        expected_fail_facts=fail_observations,
    )
    assert fail_verified["attempt"] == 2
    with pytest.raises(CaptureEvidenceError, match="already exists"):
        write_capture_live_evidence(
            fail_destination,
            attempt=2,
            environment=environment,
            observations=fail_observations,
            cleanup=fail_cleanup,
        )
    forged_cleanup = deepcopy(fail_cleanup)
    forged_cleanup["errors"] = []
    _resign_bundle(
        fail_destination,
        "cleanup.json",
        forged_cleanup,
        attempt=2,
    )
    with pytest.raises(CaptureEvidenceError, match="cleanup"):
        verify_capture_live_evidence(
            fail_destination,
            attempt=2,
            expected_result=ExpectedCaptureResult.FAIL,
            expected_cleanup_facts=_expected_cleanup_facts(
                owned_roles=("service",), errors=("ServiceShutdownTimeout",)
            ),
            expected_preflight=_preflight(),
            expected_owned_processes=_owned_processes()[:1],
            expected_unrelated_processes=_unrelated_processes(),
            expected_database_identity_sha256=_h("database"),
            expected_fail_facts=fail_observations,
        )


def _resign_complete_bundle_as_attempt(
    destination: Path, *, attempt: int
) -> dict[str, object]:
    environment = json.loads(
        (destination / "environment.json").read_text(encoding="utf-8")
    )
    observations = json.loads(
        (destination / "observations.json").read_text(encoding="utf-8")
    )
    cleanup = json.loads(
        (destination / "cleanup.json").read_text(encoding="utf-8")
    )
    environment["attempt"] = observations["attempt"] = cleanup["attempt"] = attempt
    owned_by_role = {item["role"]: item for item in _owned_processes()}
    for item in cleanup["owned"]:
        item["identity_sha256"] = process_identity_sha256(
            attempt, owned_by_role[item["role"]]
        )
    for item, private in zip(
        cleanup["unrelated"], _unrelated_processes(), strict=True
    ):
        item["identity_sha256"] = process_identity_sha256(attempt, private)
    summary = build_capture_live_summary(
        attempt=attempt,
        environment=environment,
        observations=observations,
        cleanup=cleanup,
    )
    for name, value in (
        ("environment.json", environment),
        ("observations.json", observations),
        ("cleanup.json", cleanup),
        ("summary.json", summary),
    ):
        _rewrite_bundle(destination, name, value, attempt=attempt)
    return observations


def test_fully_resigned_cross_attempt_mutation_cannot_substitute_for_attempt_two(
    tmp_path: Path,
) -> None:
    environment, observations, cleanup = _attempt_two_pass_fixture()
    substituted = tmp_path / "substituted" / "attempt-2"
    write_capture_live_evidence(
        substituted,
        attempt=2,
        environment=environment,
        observations=observations,
        cleanup=cleanup,
    )
    resigned_attempt_one = _resign_complete_bundle_as_attempt(
        substituted, attempt=1
    )
    assert resigned_attempt_one["attempt"] == 1
    assert (substituted / "summary.json").read_text(encoding="utf-8").find(
        '"attempt":1'
    ) >= 0
    assert not (substituted / "manifest.sha256").read_text(
        encoding="ascii"
    ).startswith("#")

    with pytest.raises(CaptureEvidenceError, match="attempt"):
        verify_capture_live_evidence(
            substituted,
            attempt=2,
            expected_result=ExpectedCaptureResult.PASS,
            expected_cleanup_facts=_expected_cleanup_facts(),
            expected_preflight=_preflight(),
            expected_owned_processes=_owned_processes(),
            expected_unrelated_processes=_unrelated_processes(),
            expected_database_identity_sha256=_h("database"),
            expected_pass_facts=observations,
        )


def test_attempt_two_bundle_cannot_use_an_attempt_one_leaf_path(
    tmp_path: Path,
) -> None:
    environment, observations, cleanup = _attempt_two_pass_fixture()
    wrong_path = tmp_path / "attempt-1"

    with pytest.raises(CaptureEvidenceError, match="attempt"):
        write_capture_live_evidence(
            wrong_path,
            attempt=2,
            environment=environment,
            observations=observations,
            cleanup=cleanup,
        )

    correct_path = tmp_path / "source" / "attempt-2"
    write_capture_live_evidence(
        correct_path,
        attempt=2,
        environment=environment,
        observations=observations,
        cleanup=cleanup,
    )
    shutil.copytree(correct_path, wrong_path)
    with pytest.raises(CaptureEvidenceError, match="attempt"):
        verify_capture_live_evidence(
            wrong_path,
            attempt=2,
            expected_result=ExpectedCaptureResult.PASS,
            expected_cleanup_facts=_expected_cleanup_facts(),
            expected_preflight=_preflight(),
            expected_owned_processes=_owned_processes(),
            expected_unrelated_processes=_unrelated_processes(),
            expected_database_identity_sha256=_h("database"),
            expected_pass_facts=observations,
        )


def test_evidence_apis_refuse_attempt_three_even_with_self_consistent_facts(
    tmp_path: Path,
) -> None:
    environment, observations, cleanup = _attempt_two_pass_fixture()
    for value in (environment, observations, cleanup):
        value["attempt"] = 3
    with pytest.raises((TypeError, ValueError), match="attempt"):
        build_capture_live_summary(
            attempt=3,
            environment=environment,
            observations=observations,
            cleanup=cleanup,
        )
    with pytest.raises((TypeError, ValueError), match="attempt"):
        write_capture_live_evidence(
            tmp_path / "attempt-3",
            attempt=3,
            environment=environment,
            observations=observations,
            cleanup=cleanup,
        )


def test_writer_rejects_fictitious_capture_head_budget() -> None:
    observations = _observations()
    observations["table_a"]["budget"] = {  # type: ignore[index]
        "profile": "capture_head",
        "max_depth": 8,
        "max_items": 20,
        "max_rows": 5,
        "max_bytes": 1048576,
        "timeout_ms": 30000,
        "cost_class": "bounded_scan",
    }
    with pytest.raises(CaptureEvidenceError, match="agent_dataframe"):
        build_capture_live_summary(
            environment=_environment(),
            observations=observations,
            cleanup=_cleanup(),
        )


def test_snapshot_requires_all_platform_and_harness_implementation_inputs() -> None:
    environment = _environment()
    del environment["snapshots"]["platform"]["pre"]["executables_sha256"]["1cv8.exe"]  # type: ignore[index]
    with pytest.raises(CaptureEvidenceError, match="key set"):
        build_capture_live_summary(
            environment=environment,
            observations=_observations(),
            cleanup=_cleanup(),
        )


def test_snapshot_drift_is_publishable_only_as_exact_sanitized_fail(
    tmp_path: Path,
) -> None:
    environment = _environment()
    environment["snapshots"]["source"]["post"]["tree_sha256"] = _h("drift")  # type: ignore[index]
    observations = {
        "schema": "onec-agent-capture-live-observations-v1",
        "attempt": 1,
        "result": "FAIL",
        "call_sequence": list(CAPTURE_PASS_CALL_SEQUENCE),
        "failure": {
            "boundary": "snapshot_drift_source",
            "type": "SnapshotDrift",
            "last_completed_phase": "postflight",
        },
    }
    destination = tmp_path / "drift-fail" / "attempt-1"
    write_capture_live_evidence(
        destination,
        environment=environment,
        observations=observations,
        cleanup=_cleanup(),
    )
    verified = verify_capture_live_evidence(
        destination,
        expected_result=ExpectedCaptureResult.FAIL,
        expected_cleanup_facts=_expected_cleanup_facts(),
        expected_preflight=_preflight(),
        expected_owned_processes=_owned_processes(),
        expected_unrelated_processes=_unrelated_processes(),
        expected_database_identity_sha256=_h("database"),
        expected_fail_facts=observations,
    )
    assert verified["failure_boundary"] == "snapshot_drift_source"


def test_pass_schema_requires_exact_stop_selection_proxy_and_lineage_facts() -> None:
    observations = _observations()
    del observations["capture_a"]["location"]["object_identity_sha256"]  # type: ignore[index]
    with pytest.raises(CaptureEvidenceError, match="key set"):
        build_capture_live_summary(
            environment=_environment(),
            observations=observations,
            cleanup=_cleanup(),
        )


def test_fail_evidence_requires_runtime_owner_and_every_owned_process_absent() -> None:
    cleanup = _cleanup(owned=[_owned_processes()[0]])
    cleanup["owned"][0]["state"] = "alive"  # type: ignore[index]
    observations = {
        "schema": "onec-agent-capture-live-observations-v1",
        "attempt": 1,
        "result": "FAIL",
        "call_sequence": [],
        "failure": {
            "boundary": "runtime_start",
            "type": "OfflineFailure",
            "last_completed_phase": "preflight",
        },
    }
    with pytest.raises(CaptureEvidenceError, match="zero owned processes"):
        build_capture_live_summary(
            environment=_environment(), observations=observations, cleanup=cleanup
        )
