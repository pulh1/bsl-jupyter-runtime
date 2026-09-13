from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

import pytest
import integration.evidence.capture_private_replay as private_replay

from integration.evidence.capture_live_evidence import (
    CAPTURE_PASS_CALL_SEQUENCE,
    ExpectedCaptureResult,
    capture_attempt_paths,
    process_identity_sha256,
    write_capture_live_evidence,
)
from integration.evidence.capture_private_replay import (
    CapturePrivateReplayError,
    create_capture_private_replay,
    load_capture_private_journal_prefix,
    load_capture_private_replay,
    record_capture_private_raw_response,
    verify_capture_private_replay,
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


def _stop_binding(name: str, *, line: int) -> dict[str, object]:
    return {
        "name": name,
        "project": "zup",
        "module": "Payroll",
        "procedure": "Calculate",
        "line": line,
        "executable_line": line,
        "module_type_identity_sha256": _h(f"{name}-module-type"),
        "extension_identity_sha256": _h(f"{name}-extension"),
        "object_identity_sha256": _h(f"{name}-object"),
        "property_identity_sha256": _h(f"{name}-property"),
    }


def _fixture(workspace: Path) -> dict[str, object]:
    roots = {
        "workspace": str(workspace.resolve()),
        "platform_bin": str((workspace / "platform" / "8.3.27.2170").resolve()),
        "infobase": str((workspace / "infobase").resolve()),
        "source_root": str((workspace / "source").resolve()),
        "python": str((workspace / "python.exe").resolve()),
    }
    preflight = _preflight()
    return {
        "schema": "onec-agent-capture-private-fixture-v1",
        "attempt": 2,
        "authorization": {
            "capture_attempt": "2",
            "live_flag": "1",
        },
        "paths": {
            "public_leaf": "attempt-2",
            "private_leaf": "attempt-2",
        },
        "invocation": {
            "profile": "capture",
            "maximum_mode": "experiment",
            "mutation_requested": False,
            "username": "capture_user",
            "roots": roots,
            "request_ids": {
                "main": "capture-task-6-main-attempt-2",
                "hypothesis": "capture-task-6-hypothesis-attempt-2",
                "continue_b": "capture-task-6-continue-b-attempt-2",
                "finish_main": "capture-task-6-finish-main-attempt-2",
            },
            "timeouts": {
                "overall_timeout_s": 1800.0,
                "sdk_outer_timeout_s": 600.0,
                "control_timeout_s": 180.0,
                "schema_wait_max_s": 30.0,
            },
        },
        "target_database": {
            "filesystem_key": "1a:2b",
            "size": 4096,
        },
        "unrelated_processes": [
            {
                "role": "unrelated_designer",
                "pid": 9101,
                "create_time": 1700000101.25,
                "exe": str((workspace / "unrelated" / "1cv8.exe").resolve()),
            }
        ],
        "private_markers": [
            {"kind": "username", "value": "capture_user"},
            {"kind": "workspace", "value": roots["workspace"]},
            {"kind": "platform_bin", "value": roots["platform_bin"]},
            {"kind": "infobase", "value": roots["infobase"]},
            {"kind": "source_root", "value": roots["source_root"]},
            {"kind": "discovery_source", "value": "private discovery source"},
            {"kind": "main_source", "value": "private main source"},
            {"kind": "hypothesis_source", "value": "private hypothesis source"},
        ],
        "preflight": preflight,
        "bindings": {
            "platform_sha256": preflight["platform"]["bin_identity_sha256"],
            "source_sha256": preflight["source"]["tree_sha256"],
            "notebook_sha256": preflight["notebook"]["artifact_sha256"],
            "implementation_sha256": preflight["implementation"][
                "runtime_tree_sha256"
            ],
        },
        "pass_contract": {
            "call_sequence": list(CAPTURE_PASS_CALL_SEQUENCE),
            "main": {
                "cell_id": "zup_capture_main",
                "revision": 3,
                "source_sha256": preflight["notebook"]["main_source_sha256"],
            },
            "hypothesis": {
                "cell_id": "zup_capture_hypothesis",
                "revision": 2,
                "source_sha256": preflight["notebook"][
                    "hypothesis_source_sha256"
                ],
            },
            "stop_bindings": {
                "capture_a": _stop_binding("capture_a", line=117),
                "capture_b": _stop_binding("capture_b", line=241),
            },
            "capture_a_local_names": ["ManagerLocal", "DescriptionLocal"],
            "table_name": "PayrollTable",
            "downstream_result_local": "DescriptionLocal",
            "budgets": {
                "agent_metadata": {
                    "profile": "agent_metadata",
                    "max_depth": 1,
                    "max_items": 20,
                    "max_rows": 20,
                    "max_bytes": 16384,
                    "timeout_ms": 1000,
                    "cost_class": "metadata",
                },
                "agent_dataframe": {
                    "profile": "agent_dataframe",
                    "max_depth": 8,
                    "max_items": 200000,
                    "max_rows": 10000,
                    "max_bytes": 67108864,
                    "timeout_ms": 30000,
                    "cost_class": "full_scan",
                },
            },
        },
    }


def test_private_fixture_is_canonical_create_once_and_does_not_publish_evidence(
    tmp_path: Path,
) -> None:
    paths = capture_attempt_paths(tmp_path, attempt=2)
    fixture = _fixture(tmp_path)

    create_capture_private_replay(paths.private, fixture=fixture)

    assert paths.private.is_dir()
    assert paths.public.exists() is False
    payload = (
        json.dumps(
            fixture,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    assert (paths.private / "fixture.json").read_bytes() == payload
    assert (paths.private / "fixture.sha256").read_text(encoding="ascii") == (
        f"{sha256(payload).hexdigest()}  fixture.json\n"
    )
    prefix = load_capture_private_journal_prefix(paths.private)
    assert len(prefix) == 1
    assert prefix[0]["kind"] == "fixture"
    assert prefix[0]["payload"] == {"fixture_sha256": sha256(payload).hexdigest()}

    with pytest.raises(CapturePrivateReplayError, match="already exists"):
        create_capture_private_replay(paths.private, fixture=fixture)


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("invocation", "password", "not-allowed"),
        ("invocation", "token", "not-allowed"),
        ("invocation", "credentials", {"secret": "not-allowed"}),
    ],
)
def test_private_fixture_rejects_credentials_before_creating_attempt_path(
    tmp_path: Path, section: str, key: str, value: object
) -> None:
    paths = capture_attempt_paths(tmp_path, attempt=2)
    fixture = _fixture(tmp_path)
    fixture[section][key] = value

    with pytest.raises(CapturePrivateReplayError, match="credential"):
        create_capture_private_replay(paths.private, fixture=fixture)

    assert paths.private.exists() is False


def test_public_only_bundle_cannot_claim_historical_private_verification(
    tmp_path: Path,
) -> None:
    paths = capture_attempt_paths(tmp_path, attempt=2)
    paths.public.mkdir(parents=True)

    with pytest.raises(CapturePrivateReplayError, match="private fixture"):
        verify_capture_private_replay(
            paths.public,
            private_attempt=paths.private,
        )


def _call_started_payload(workspace: Path) -> dict[str, object]:
    return {
        "index": 1,
        "stage": "A:workspace.open",
        "public_label": "A:workspace.open",
        "operation_group": None,
        "method": "workspace.open",
        "arguments": {"project": str(workspace.resolve())},
    }


def test_private_journal_appends_hash_chained_call_and_create_once_raw_response(
    tmp_path: Path,
) -> None:
    paths = capture_attempt_paths(tmp_path, attempt=2)
    journal = create_capture_private_replay(
        paths.private, fixture=_fixture(tmp_path)
    )
    started = _call_started_payload(tmp_path)

    journal.append("call_started", started)
    envelope = {
        "ok": True,
        "value": {
            "workspace_id": "private-workspace-id",
            "project_root": str(tmp_path.resolve()),
        },
        "failure": None,
    }
    raw = record_capture_private_raw_response(
        paths.private,
        index=1,
        method="workspace.open",
        payload=envelope,
    )
    journal.append(
        "call_response",
        {
            **{key: started[key] for key in (
                "index",
                "stage",
                "public_label",
                "operation_group",
                "method",
            )},
            "accepted": True,
            "raw": raw,
        },
    )

    expected_raw = (
        json.dumps(
            envelope,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    assert raw == {
        "index": 1,
        "method": "workspace.open",
        "path": "raw-mcp/0001-workspace-open.json",
        "sha256": sha256(expected_raw).hexdigest(),
    }
    assert (paths.private / raw["path"]).read_bytes() == expected_raw
    prefix = load_capture_private_journal_prefix(paths.private)
    assert [item["sequence"] for item in prefix] == [1, 2, 3]
    assert [item["kind"] for item in prefix] == [
        "fixture",
        "call_started",
        "call_response",
    ]
    assert prefix[1]["entry_sha256"] == prefix[2]["previous_sha256"]

    with pytest.raises(CapturePrivateReplayError, match="already exists"):
        record_capture_private_raw_response(
            paths.private,
            index=1,
            method="workspace.open",
            payload=envelope,
        )


def test_crash_prefix_is_truthful_but_cannot_verify_a_public_bundle(
    tmp_path: Path,
) -> None:
    paths = capture_attempt_paths(tmp_path, attempt=2)
    journal = create_capture_private_replay(
        paths.private, fixture=_fixture(tmp_path)
    )
    journal.append("call_started", _call_started_payload(tmp_path))

    prefix = load_capture_private_journal_prefix(paths.private)
    assert [item["kind"] for item in prefix] == ["fixture", "call_started"]
    assert prefix[-1]["payload"]["stage"] == "A:workspace.open"
    with pytest.raises(CapturePrivateReplayError, match="incomplete"):
        verify_capture_private_replay(
            paths.public,
            private_attempt=paths.private,
        )


def test_private_journal_rejects_reorder_and_credentials(
    tmp_path: Path,
) -> None:
    paths = capture_attempt_paths(tmp_path, attempt=2)
    journal = create_capture_private_replay(
        paths.private, fixture=_fixture(tmp_path)
    )
    before = (paths.private / "journal.jsonl").read_bytes()
    with pytest.raises(CapturePrivateReplayError, match="credential"):
        journal.append("private_marker", {"kind": "token", "value": "secret"})
    assert (paths.private / "journal.jsonl").read_bytes() == before

    journal.append("stage", {"name": "preflight_complete"})
    journal.append("stage", {"name": "launch_reserved"})
    journal_path = paths.private / "journal.jsonl"
    lines = journal_path.read_bytes().splitlines(keepends=True)
    journal_path.write_bytes(lines[0] + lines[2] + lines[1])

    with pytest.raises(CapturePrivateReplayError, match="chain"):
        load_capture_private_journal_prefix(paths.private)


def test_private_journal_loader_rejects_unknown_persisted_event_kind(
    tmp_path: Path,
) -> None:
    paths = capture_attempt_paths(tmp_path, attempt=2)
    create_capture_private_replay(paths.private, fixture=_fixture(tmp_path))
    prefix = load_capture_private_journal_prefix(paths.private)
    unsigned = {
        "schema": "onec-agent-capture-private-journal-entry-v1",
        "attempt": 2,
        "sequence": 2,
        "previous_sha256": prefix[-1]["entry_sha256"],
        "kind": "unknown_event",
        "payload": {},
    }
    canonical_unsigned = (
        json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    entry = {**unsigned, "entry_sha256": sha256(canonical_unsigned).hexdigest()}
    with (paths.private / "journal.jsonl").open("ab") as stream:
        stream.write(
            (
                json.dumps(
                    entry,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                + "\n"
            ).encode("utf-8")
        )

    with pytest.raises(CapturePrivateReplayError, match="event kind"):
        load_capture_private_journal_prefix(paths.private)


def _database_hash(fixture: dict[str, object]) -> str:
    database = fixture["target_database"]
    digest = sha256()
    for part in (
        "onec-capture-database-v1",
        database["filesystem_key"],
        str(database["size"]),
    ):
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _complete_fail_replay(
    tmp_path: Path,
    *,
    public_boundary: str = "before_A_runtime_ensure",
    publish: bool = True,
    target_database_process_count: int | None = 0,
    runtime_owner_state: str = "absent",
    control_token_state: str = "absent",
    control_descriptor_state: str = "absent",
) -> tuple[
    object,
    dict[str, object],
    dict[str, object],
    dict[str, object],
    dict[str, object],
]:
    paths = capture_attempt_paths(tmp_path, attempt=2)
    fixture = _fixture(tmp_path)
    journal = create_capture_private_replay(paths.private, fixture=fixture)
    started = _call_started_payload(tmp_path)
    journal.append("call_started", started)
    raw = record_capture_private_raw_response(
        paths.private,
        index=1,
        method="workspace.open",
        payload={
            "ok": True,
            "value": {
                "workspace_id": "private-workspace-id",
                "project_root": str(tmp_path.resolve()),
            },
            "failure": None,
        },
    )
    journal.append(
        "call_response",
        {
            **{key: started[key] for key in (
                "index",
                "stage",
                "public_label",
                "operation_group",
                "method",
            )},
            "accepted": True,
            "raw": raw,
        },
    )
    owned = [
        {
            "role": "service",
            "pid": 8101,
            "create_time": 1700000001.5,
            "exe": str((tmp_path / "python.exe").resolve()),
        }
    ]
    ledger = {"attempt": 2, "owned": owned}
    ledger_payload = (
        json.dumps(
            ledger,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    (paths.private / "owned-processes.json").write_bytes(ledger_payload)
    unrelated = fixture["unrelated_processes"]
    cleanup = {
        "schema": "onec-agent-capture-live-cleanup-v1",
        "attempt": 2,
        "runtime_owner_state": runtime_owner_state,
        "owned": [
            {
                "role": "service",
                "identity_sha256": process_identity_sha256(2, owned[0]),
                "state": "absent",
            }
        ],
        "unrelated": [
            {
                "identity_sha256": process_identity_sha256(2, unrelated[0]),
                "state": "alive",
            }
        ],
        "errors": ["ServiceShutdownTimeout"],
    }
    journal.append(
        "cleanup",
        {
            "owned_ledger": {
                "path": "owned-processes.json",
                "sha256": sha256(ledger_payload).hexdigest(),
            },
            "owned": cleanup["owned"],
            "unrelated": cleanup["unrelated"],
            "errors": cleanup["errors"],
            "runtime_owner_state": runtime_owner_state,
            "target_database_process_count": target_database_process_count,
            "control_token_state": control_token_state,
            "control_descriptor_state": control_descriptor_state,
        },
    )
    journal.append("postflight", {"snapshots": deepcopy(fixture["preflight"])})
    private_failure = {
        "boundary": "before_A_runtime_ensure",
        "public_type": "TimeoutError",
        "raw_error_type": "TimeoutError",
        "last_completed_phase": "A_workspace_open",
    }
    journal.append(
        "outcome",
        {
            "result": "FAIL",
            "failure": private_failure,
        },
    )
    journal.append("complete", {"result": "FAIL"})

    environment = {
        "schema": "onec-agent-capture-live-environment-v1",
        "attempt": 2,
        "profile": "capture",
        "maximum_mode": "experiment",
        "database": {
            "target_identity_sha256": _database_hash(fixture),
            "mutation_requested": False,
        },
        "snapshots": {
            name: {"pre": deepcopy(value), "post": deepcopy(value)}
            for name, value in fixture["preflight"].items()
        },
    }
    observations = {
        "schema": "onec-agent-capture-live-observations-v1",
        "attempt": 2,
        "result": "FAIL",
        "call_sequence": ["A:workspace.open"],
        "failure": {
            **private_failure,
        },
    }
    del observations["failure"]["public_type"]
    del observations["failure"]["raw_error_type"]
    observations["failure"]["type"] = "TimeoutError"
    observations["failure"]["boundary"] = public_boundary
    if publish:
        write_capture_live_evidence(
            paths.public,
            attempt=2,
            environment=environment,
            observations=observations,
            cleanup=cleanup,
        )
    return paths, private_failure, environment, observations, cleanup


def test_private_fail_replay_reconstructs_every_external_verifier_input(
    tmp_path: Path,
) -> None:
    paths, private_failure, *_ = _complete_fail_replay(tmp_path)

    replay = load_capture_private_replay(paths.private)
    assert replay.attempt == 2
    assert replay.expected_result is ExpectedCaptureResult.FAIL
    assert replay.expected_fail_facts["failure"] == {
        "boundary": private_failure["boundary"],
        "type": private_failure["public_type"],
        "last_completed_phase": private_failure["last_completed_phase"],
    }
    assert replay.expected_cleanup_facts.errors == ("ServiceShutdownTimeout",)
    assert len(replay.expected_owned_processes) == 1
    assert len(replay.expected_unrelated_processes) == 1
    assert replay.private_pids == (8101, 9101)
    assert "capture_user" in replay.private_markers

    verified = verify_capture_private_replay(
        paths.public,
        private_attempt=paths.private,
    )
    assert verified["status"] == "FAIL"
    assert verified["failure_boundary"] == "before_A_runtime_ensure"


def test_private_replay_rejects_valid_public_fail_that_diverges_from_private_outcome(
    tmp_path: Path,
) -> None:
    paths, *_ = _complete_fail_replay(
        tmp_path,
        public_boundary="before_A_code_run_inline_discovery",
    )

    with pytest.raises(CapturePrivateReplayError, match="public bundle"):
        verify_capture_private_replay(
            paths.public,
            private_attempt=paths.private,
        )


def test_publish_private_replay_verifies_staging_before_one_shot_publication(
    tmp_path: Path,
) -> None:
    paths, _, environment, observations, cleanup = _complete_fail_replay(
        tmp_path, publish=False
    )

    verified = private_replay.publish_capture_private_replay(
        paths.public,
        private_attempt=paths.private,
        environment=environment,
        observations=observations,
        cleanup=cleanup,
        ephemeral_private_markers=("ephemeral-not-present",),
    )

    assert verified["status"] == "FAIL"
    assert paths.public.is_dir()
    assert (paths.private / "public-staging").exists() is False


def test_publish_private_replay_leaves_public_absent_when_private_replay_is_invalid(
    tmp_path: Path,
) -> None:
    paths, _, environment, observations, cleanup = _complete_fail_replay(
        tmp_path, publish=False
    )
    record_capture_private_raw_response(
        paths.private,
        index=99,
        method="runtime.status",
        payload={"ok": True, "value": {"state": "ready"}, "failure": None},
    )

    with pytest.raises(CapturePrivateReplayError, match="file set"):
        private_replay.publish_capture_private_replay(
            paths.public,
            private_attempt=paths.private,
            environment=environment,
            observations=observations,
            cleanup=cleanup,
        )

    assert paths.public.exists() is False
    assert (paths.private / "public-staging").exists() is False


def test_publish_private_replay_rejects_ephemeral_token_leak_before_staging_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, _, environment, observations, cleanup = _complete_fail_replay(
        tmp_path, publish=False
    )

    writer_called = False

    def forbidden_writer(*args: object, **kwargs: object) -> None:
        nonlocal writer_called
        writer_called = True
        raise AssertionError("staging writer must not run before privacy validation")

    monkeypatch.setattr(private_replay, "write_capture_live_evidence", forbidden_writer)

    with pytest.raises(CapturePrivateReplayError, match="public bundle"):
        private_replay.publish_capture_private_replay(
            paths.public,
            private_attempt=paths.private,
            environment=environment,
            observations=observations,
            cleanup=cleanup,
            ephemeral_private_markers=("before_A_runtime_ensure",),
        )

    assert writer_called is False
    assert paths.public.exists() is False
    assert (paths.private / "public-staging").exists() is False


def test_publish_private_replay_rejects_private_pid_before_staging_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, _, environment, observations, cleanup = _complete_fail_replay(
        tmp_path, publish=False
    )
    cleanup = deepcopy(cleanup)
    cleanup["errors"] = ["PrivatePid8101"]
    writer_called = False

    def forbidden_writer(*args: object, **kwargs: object) -> None:
        nonlocal writer_called
        writer_called = True
        raise AssertionError("staging writer must not run before privacy validation")

    monkeypatch.setattr(private_replay, "write_capture_live_evidence", forbidden_writer)

    with pytest.raises(CapturePrivateReplayError, match="public bundle"):
        private_replay.publish_capture_private_replay(
            paths.public,
            private_attempt=paths.private,
            environment=environment,
            observations=observations,
            cleanup=cleanup,
        )

    assert writer_called is False
    assert paths.public.exists() is False
    assert (paths.private / "public-staging").exists() is False


def test_successful_publication_survives_staging_root_cleanup_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, _, environment, observations, cleanup = _complete_fail_replay(
        tmp_path, publish=False
    )
    staging_root = paths.private / "public-staging"
    original_rmdir = Path.rmdir

    def controlled_rmdir(path: Path) -> None:
        if path == staging_root:
            raise PermissionError("synthetic post-commit cleanup failure")
        original_rmdir(path)

    monkeypatch.setattr(Path, "rmdir", controlled_rmdir)

    verified = private_replay.publish_capture_private_replay(
        paths.public,
        private_attempt=paths.private,
        environment=environment,
        observations=observations,
        cleanup=cleanup,
    )

    assert verified["status"] == "FAIL"
    assert paths.public.is_dir()


def test_residual_target_process_is_durable_but_blocks_publication(
    tmp_path: Path,
) -> None:
    paths, _, environment, observations, cleanup = _complete_fail_replay(
        tmp_path,
        publish=False,
        target_database_process_count=1,
    )

    replay = load_capture_private_replay(paths.private)
    assert replay.target_database_process_count == 1
    with pytest.raises(CapturePrivateReplayError, match="target database"):
        private_replay.publish_capture_private_replay(
            paths.public,
            private_attempt=paths.private,
            environment=environment,
            observations=observations,
            cleanup=cleanup,
        )

    assert paths.public.exists() is False


@pytest.mark.parametrize("runtime_owner_state", ["alive", "unknown"])
def test_non_absent_runtime_owner_is_durable_but_blocks_publication(
    tmp_path: Path, runtime_owner_state: str,
) -> None:
    paths, _, environment, observations, cleanup = _complete_fail_replay(
        tmp_path,
        publish=False,
        runtime_owner_state=runtime_owner_state,
    )

    replay = load_capture_private_replay(paths.private)
    assert replay.expected_cleanup_facts.runtime_owner_state == runtime_owner_state
    with pytest.raises(CapturePrivateReplayError, match="runtime owner"):
        private_replay.publish_capture_private_replay(
            paths.public,
            private_attempt=paths.private,
            environment=environment,
            observations=observations,
            cleanup=cleanup,
        )

    assert paths.public.exists() is False


@pytest.mark.parametrize(
    ("control_token_state", "control_descriptor_state"),
    [("alive", "absent"), ("unknown", "absent"), ("absent", "alive")],
)
def test_non_absent_control_credentials_are_durable_but_block_publication(
    tmp_path: Path,
    control_token_state: str,
    control_descriptor_state: str,
) -> None:
    paths, _, environment, observations, cleanup = _complete_fail_replay(
        tmp_path,
        publish=False,
        control_token_state=control_token_state,
        control_descriptor_state=control_descriptor_state,
    )

    replay = load_capture_private_replay(paths.private)
    assert replay.control_token_state == control_token_state
    assert replay.control_descriptor_state == control_descriptor_state
    with pytest.raises(CapturePrivateReplayError, match="control credentials"):
        private_replay.publish_capture_private_replay(
            paths.public,
            private_attempt=paths.private,
            environment=environment,
            observations=observations,
            cleanup=cleanup,
        )

    assert paths.public.exists() is False


def _complete_pass_replay(
    tmp_path: Path,
) -> tuple[object, dict[str, object], dict[str, object]]:
    paths = capture_attempt_paths(tmp_path, attempt=2)
    fixture = _fixture(tmp_path)
    journal = create_capture_private_replay(paths.private, fixture=fixture)
    call_index = 0
    for label in CAPTURE_PASS_CALL_SEQUENCE:
        if label in {
            "A:frontend.exit",
            "B:value.inspect.stale",
            "owner:service.shutdown",
        }:
            journal.append(
                "stage",
                {
                    "name": label.replace(":", "_").replace(".", "_"),
                    "public_label": label,
                },
            )
            continue
        call_index += 1
        index = call_index
        started = {
            "index": index,
            "stage": label,
            "public_label": label,
            "operation_group": label,
            "method": "operation.wait",
            "arguments": {"operation_id": f"private-operation-{index}"},
        }
        journal.append("call_started", started)
        raw = record_capture_private_raw_response(
            paths.private,
            index=index,
            method="operation.wait",
            payload={"ok": True, "value": {"state": "completed"}, "failure": None},
        )
        journal.append(
            "call_response",
            {
                **{
                    key: started[key]
                    for key in (
                        "index",
                        "stage",
                        "public_label",
                        "operation_group",
                        "method",
                    )
                },
                "accepted": True,
                "raw": raw,
            },
        )
    owned = [
        {
            "role": role,
            "pid": 8101 + index,
            "create_time": 1700000001.5 + index,
            "exe": str((tmp_path / f"{role}.exe").resolve()),
        }
        for index, role in enumerate(
            ("service", "mcp_a", "mcp_b", "designer", "dbgs", "onec")
        )
    ]
    ledger = {"attempt": 2, "owned": owned}
    ledger_payload = (
        json.dumps(
            ledger,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    (paths.private / "owned-processes.json").write_bytes(ledger_payload)
    unrelated = fixture["unrelated_processes"]
    cleanup = {
        "schema": "onec-agent-capture-live-cleanup-v1",
        "attempt": 2,
        "runtime_owner_state": "absent",
        "owned": [
            {
                "role": item["role"],
                "identity_sha256": process_identity_sha256(2, item),
                "state": "absent",
            }
            for item in owned
        ],
        "unrelated": [
            {
                "identity_sha256": process_identity_sha256(2, item),
                "state": "alive",
            }
            for item in unrelated
        ],
        "errors": [],
    }
    journal.append(
        "cleanup",
        {
            "owned_ledger": {
                "path": "owned-processes.json",
                "sha256": sha256(ledger_payload).hexdigest(),
            },
            "owned": cleanup["owned"],
            "unrelated": cleanup["unrelated"],
            "errors": [],
            "runtime_owner_state": "absent",
            "target_database_process_count": 0,
            "control_token_state": "absent",
            "control_descriptor_state": "absent",
        },
    )
    journal.append("postflight", {"snapshots": deepcopy(fixture["preflight"])})
    from tests.unit.test_agent_capture_live_evidence import _observations

    observations = _observations()
    observations["attempt"] = 2
    journal.append("outcome", {"result": "PASS", "observations": observations})
    journal.append("complete", {"result": "PASS"})
    return paths, observations, cleanup


def test_private_pass_replay_reconstructs_durable_expected_observations(
    tmp_path: Path,
) -> None:
    paths, observations, _ = _complete_pass_replay(tmp_path)

    replay = load_capture_private_replay(paths.private)

    assert replay.expected_result is ExpectedCaptureResult.PASS
    assert replay.expected_pass_facts == observations
    assert replay.expected_fail_facts is None


def test_private_pass_replay_verifies_complete_bundle_before_publication(
    tmp_path: Path,
) -> None:
    paths, observations, cleanup = _complete_pass_replay(tmp_path)
    replay = load_capture_private_replay(paths.private)

    verified = private_replay.publish_capture_private_replay(
        paths.public,
        private_attempt=paths.private,
        environment=replay.expected_environment,
        observations=observations,
        cleanup=cleanup,
    )

    assert verified["status"] == "PASS"
    assert paths.public.is_dir()
