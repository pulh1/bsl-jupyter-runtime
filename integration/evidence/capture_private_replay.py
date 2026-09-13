"""Private, create-once replay authority for CAPTURE live evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
import hmac
import json
from math import isfinite
import os
from pathlib import Path, PureWindowsPath
import re
import shutil

from integration.evidence.capture_live_evidence import (
    CAPTURE_PASS_CALL_SEQUENCE,
    CaptureEvidenceError,
    ExpectedCaptureCleanupFacts,
    ExpectedCaptureResult,
    assert_capture_live_evidence_private_safe,
    process_identity_sha256,
    verify_capture_live_evidence,
    write_capture_live_evidence,
)


_HASH = re.compile(r"^[0-9a-f]{64}$")
_ZERO_HASH = "0" * 64
_FIXTURE_FILE = "fixture.json"
_FIXTURE_MANIFEST = "fixture.sha256"
_JOURNAL_FILE = "journal.jsonl"
_FIXTURE_SCHEMA = "onec-agent-capture-private-fixture-v1"
_JOURNAL_SCHEMA = "onec-agent-capture-private-journal-entry-v1"
_FORBIDDEN_CREDENTIAL_KEYS = {
    "access_token",
    "api_key",
    "credential",
    "credentials",
    "password",
    "refresh_token",
    "secret",
    "token",
}
_STATIC_MARKER_KINDS = {
    "username",
    "workspace",
    "platform_bin",
    "infobase",
    "source_root",
    "discovery_source",
    "main_source",
    "hypothesis_source",
}
_METHOD = re.compile(r"^[a-z][a-z0-9_.]{0,127}$")
_EVENT_KINDS = {
    "call_response",
    "call_started",
    "cleanup",
    "complete",
    "outcome",
    "postflight",
    "private_marker",
    "stage",
}
_PRIVATE_ID_KEYS = {
    "capture_intent_id",
    "manager_id",
    "operation_id",
    "proxy_id",
    "runtime_id",
    "table_id",
    "workspace_id",
}


class CapturePrivateReplayError(RuntimeError):
    """A private replay input is missing, mutable, unsafe, or inconsistent."""


@dataclass(frozen=True, slots=True)
class CapturePrivateJournal:
    """Handle for one create-once attempt-2 private journal."""

    private_attempt: Path

    def append(
        self, kind: str, payload: Mapping[str, object]
    ) -> dict[str, object]:
        """Append one canonical event after authenticating the existing prefix."""
        return _append_journal_event(self.private_attempt, kind=kind, payload=payload)


@dataclass(frozen=True, slots=True)
class CapturePrivateReplayExpectation:
    """Typed verifier inputs reconstructed only from one private attempt."""

    attempt: int
    expected_result: ExpectedCaptureResult
    expected_environment: Mapping[str, object]
    expected_preflight: Mapping[str, object]
    expected_cleanup_facts: ExpectedCaptureCleanupFacts
    expected_owned_processes: tuple[Mapping[str, object], ...]
    expected_unrelated_processes: tuple[Mapping[str, object], ...]
    expected_database_identity_sha256: str
    expected_pass_facts: Mapping[str, object] | None
    expected_fail_facts: Mapping[str, object] | None
    target_database_process_count: int | None
    control_token_state: str
    control_descriptor_state: str
    private_markers: tuple[str, ...]
    private_pids: tuple[int, ...]


def create_capture_private_replay(
    private_attempt: Path,
    *,
    fixture: Mapping[str, object],
) -> CapturePrivateJournal:
    """Create the attempt-2 fixture and its first hash-chained journal entry."""
    private_attempt = _require_private_attempt_path(private_attempt)
    fixture_value = _validate_fixture(fixture)
    payload = _canonical(fixture_value)
    fixture_digest = sha256(payload).hexdigest()
    if private_attempt.exists():
        raise CapturePrivateReplayError("private replay attempt already exists")
    private_attempt.parent.mkdir(parents=True, exist_ok=True)
    private_attempt.mkdir(mode=0o700, exist_ok=False)
    _write_create_once(private_attempt / _FIXTURE_FILE, payload)
    _write_create_once(
        private_attempt / _FIXTURE_MANIFEST,
        f"{fixture_digest}  {_FIXTURE_FILE}\n".encode("ascii"),
    )
    first = _journal_entry(
        sequence=1,
        previous_sha256=_ZERO_HASH,
        kind="fixture",
        payload={"fixture_sha256": fixture_digest},
    )
    _write_create_once(private_attempt / _JOURNAL_FILE, _canonical(first))
    return CapturePrivateJournal(private_attempt)


def load_capture_private_journal_prefix(
    private_attempt: Path,
) -> tuple[dict[str, object], ...]:
    """Read and authenticate a truthful journal prefix, complete or incomplete."""
    private_attempt = _require_private_attempt_path(private_attempt)
    fixture_digest = _load_fixture_digest(private_attempt)
    journal_path = private_attempt / _JOURNAL_FILE
    try:
        raw_lines = journal_path.read_bytes().splitlines(keepends=True)
    except OSError as error:
        raise CapturePrivateReplayError("private replay journal is missing") from error
    if not raw_lines:
        raise CapturePrivateReplayError("private replay journal is empty")
    entries: list[dict[str, object]] = []
    previous = _ZERO_HASH
    for expected_sequence, raw in enumerate(raw_lines, start=1):
        if not raw.endswith(b"\n"):
            raise CapturePrivateReplayError("private replay journal is not canonical")
        entry = _read_canonical_object(raw, name="private replay journal entry")
        _exact(
            entry,
            {
                "schema",
                "attempt",
                "sequence",
                "previous_sha256",
                "kind",
                "payload",
                "entry_sha256",
            },
            "private replay journal entry",
        )
        if entry["kind"] not in {*_EVENT_KINDS, "fixture"}:
            raise CapturePrivateReplayError(
                "private replay journal event kind is invalid"
            )
        if (
            entry["schema"] != _JOURNAL_SCHEMA
            or entry["attempt"] != 2
            or entry["sequence"] != expected_sequence
            or entry["previous_sha256"] != previous
            or not isinstance(entry["kind"], str)
            or not isinstance(entry["payload"], Mapping)
        ):
            raise CapturePrivateReplayError("private replay journal chain is invalid")
        claimed = entry["entry_sha256"]
        _require_hash(claimed, "private replay journal entry")
        unsigned = dict(entry)
        del unsigned["entry_sha256"]
        actual = sha256(_canonical(unsigned)).hexdigest()
        if not hmac.compare_digest(str(claimed), actual):
            raise CapturePrivateReplayError("private replay journal hash is invalid")
        entries.append(entry)
        previous = actual
    first = entries[0]
    if first["kind"] != "fixture" or first["payload"] != {
        "fixture_sha256": fixture_digest
    }:
        raise CapturePrivateReplayError("private fixture is not bound to the journal")
    return tuple(entries)


def record_capture_private_raw_response(
    private_attempt: Path,
    *,
    index: int,
    method: str,
    payload: object,
) -> dict[str, object]:
    """Create one canonical raw MCP envelope and return its immutable binding."""
    private_attempt = _require_private_attempt_path(private_attempt)
    load_capture_private_journal_prefix(private_attempt)
    if type(index) is not int or index <= 0 or _METHOD.fullmatch(method) is None:
        raise CapturePrivateReplayError("raw MCP response identity is invalid")
    _reject_credentials(payload)
    safe_method = method.replace(".", "-")
    relative = f"raw-mcp/{index:04d}-{safe_method}.json"
    raw_payload = _canonical(payload)
    raw_dir = private_attempt / "raw-mcp"
    raw_dir.mkdir(mode=0o700, exist_ok=True)
    _write_create_once(private_attempt / relative, raw_payload)
    return {
        "index": index,
        "method": method,
        "path": relative,
        "sha256": sha256(raw_payload).hexdigest(),
    }


def verify_capture_private_replay(
    published_bundle: Path,
    *,
    private_attempt: Path,
    ephemeral_private_markers: Sequence[str] = (),
) -> dict[str, object]:
    """Verify public attempt 2 from durable private inputs only."""
    private_attempt = _require_private_attempt_path(private_attempt)
    if not (private_attempt / _FIXTURE_FILE).is_file():
        raise CapturePrivateReplayError("private fixture is missing")
    published_bundle = Path(published_bundle)
    if published_bundle.name != "attempt-2":
        raise CapturePrivateReplayError("public bundle does not bind attempt 2")
    replay = load_capture_private_replay(private_attempt)
    try:
        public_environment = _read_canonical_object(
            (published_bundle / "environment.json").read_bytes(),
            name="public environment",
        )
    except (OSError, CapturePrivateReplayError) as error:
        raise CapturePrivateReplayError("public bundle is incomplete") from error
    if not hmac.compare_digest(
        _canonical(public_environment), _canonical(replay.expected_environment)
    ):
        raise CapturePrivateReplayError("public bundle diverges from private replay")
    try:
        return verify_capture_live_evidence(
            published_bundle,
            attempt=2,
            expected_result=replay.expected_result,
            expected_cleanup_facts=replay.expected_cleanup_facts,
            expected_preflight=replay.expected_preflight,
            expected_owned_processes=replay.expected_owned_processes,
            expected_unrelated_processes=replay.expected_unrelated_processes,
            expected_database_identity_sha256=(
                replay.expected_database_identity_sha256
            ),
            expected_pass_facts=replay.expected_pass_facts,
            expected_fail_facts=replay.expected_fail_facts,
            private_markers=(
                *replay.private_markers,
                *ephemeral_private_markers,
            ),
            private_pids=replay.private_pids,
        )
    except (CaptureEvidenceError, OSError) as error:
        raise CapturePrivateReplayError(
            "public bundle diverges from private replay"
        ) from error


def publish_capture_private_replay(
    published_bundle: Path,
    *,
    private_attempt: Path,
    environment: Mapping[str, object],
    observations: Mapping[str, object],
    cleanup: Mapping[str, object],
    ephemeral_private_markers: Sequence[str] = (),
) -> dict[str, object]:
    """Verify a private staging bundle before atomically publishing attempt 2."""
    published_bundle = Path(published_bundle)
    private_attempt = _require_private_attempt_path(private_attempt)
    if published_bundle.name != "attempt-2":
        raise CapturePrivateReplayError("public bundle does not bind attempt 2")
    if published_bundle.exists():
        raise CapturePrivateReplayError("public replay attempt already exists")
    replay = load_capture_private_replay(private_attempt)
    if replay.expected_cleanup_facts.runtime_owner_state != "absent":
        raise CapturePrivateReplayError("runtime owner cleanup is not publishable")
    if replay.target_database_process_count != 0:
        raise CapturePrivateReplayError(
            "target database cleanup is not publishable"
        )
    if (
        replay.control_token_state != "absent"
        or replay.control_descriptor_state != "absent"
    ):
        raise CapturePrivateReplayError("control credentials cleanup is not publishable")
    try:
        assert_capture_live_evidence_private_safe(
            attempt=2,
            environment=environment,
            observations=observations,
            cleanup=cleanup,
            private_markers=(
                *replay.private_markers,
                *ephemeral_private_markers,
            ),
            private_pids=replay.private_pids,
        )
    except (CaptureEvidenceError, TypeError) as error:
        raise CapturePrivateReplayError(
            "public bundle diverges from private replay"
        ) from error
    staging_root = private_attempt / "public-staging"
    staging_bundle = staging_root / "attempt-2"
    if staging_root.exists():
        raise CapturePrivateReplayError("private replay staging already exists")
    try:
        write_capture_live_evidence(
            staging_bundle,
            attempt=2,
            environment=environment,
            observations=observations,
            cleanup=cleanup,
        )
        verified = verify_capture_private_replay(
            staging_bundle,
            private_attempt=private_attempt,
            ephemeral_private_markers=ephemeral_private_markers,
        )
        published_bundle.parent.mkdir(parents=True, exist_ok=True)
        if published_bundle.exists():
            raise CapturePrivateReplayError("public replay attempt already exists")
        staging_bundle.rename(published_bundle)
    except BaseException:
        if staging_bundle.exists():
            shutil.rmtree(staging_bundle)
        if staging_root.exists():
            staging_root.rmdir()
        raise
    try:
        staging_root.rmdir()
    except OSError:
        pass
    return verified


def load_capture_private_replay(
    private_attempt: Path,
) -> CapturePrivateReplayExpectation:
    """Reconstruct exact external verifier facts from authenticated private files."""
    private_attempt = _require_private_attempt_path(private_attempt)
    fixture = _load_fixture(private_attempt)
    entries = load_capture_private_journal_prefix(private_attempt)
    if entries[-1]["kind"] != "complete":
        raise CapturePrivateReplayError("private replay journal is incomplete")
    for entry in entries[1:]:
        _validate_event(str(entry["kind"]), _mapping(entry["payload"], "event"))
    cleanup_entry = _one_event(entries, "cleanup")
    postflight_entry = _one_event(entries, "postflight")
    outcome_entry = _one_event(entries, "outcome")
    complete_entry = _one_event(entries, "complete")
    ordered_sequences = [
        int(item["sequence"])
        for item in (cleanup_entry, postflight_entry, outcome_entry, complete_entry)
    ]
    if ordered_sequences != sorted(ordered_sequences) or complete_entry is not entries[-1]:
        raise CapturePrivateReplayError("private replay completion order is invalid")
    call_sequence, dynamic_markers = _load_call_evidence(
        private_attempt, entries
    )
    contract = _mapping(fixture["pass_contract"], "PASS contract")
    expected_sequence = contract["call_sequence"]
    if not isinstance(expected_sequence, list) or call_sequence != expected_sequence[: len(call_sequence)]:
        raise CapturePrivateReplayError("private call sequence is not an exact prefix")
    (
        owned,
        unrelated,
        cleanup_facts,
        target_database_process_count,
        control_token_state,
        control_descriptor_state,
    ) = _load_cleanup(
        private_attempt,
        fixture=fixture,
        event=_mapping(cleanup_entry["payload"], "cleanup event"),
    )
    preflight = dict(_mapping(fixture["preflight"], "preflight"))
    postflight_payload = _mapping(postflight_entry["payload"], "postflight event")
    snapshots = _mapping(postflight_payload["snapshots"], "postflight snapshots")
    _exact(
        snapshots,
        {"platform", "source", "notebook", "implementation"},
        "postflight snapshots",
    )
    database_digest = _database_identity_sha256(
        _mapping(fixture["target_database"], "target database")
    )
    environment: dict[str, object] = {
        "schema": "onec-agent-capture-live-environment-v1",
        "attempt": 2,
        "profile": "capture",
        "maximum_mode": "experiment",
        "database": {
            "target_identity_sha256": database_digest,
            "mutation_requested": False,
        },
        "snapshots": {
            name: {"pre": preflight[name], "post": snapshots[name]}
            for name in ("platform", "source", "notebook", "implementation")
        },
    }
    outcome = _mapping(outcome_entry["payload"], "outcome event")
    complete = _mapping(complete_entry["payload"], "complete event")
    if complete.get("result") != outcome.get("result"):
        raise CapturePrivateReplayError("private replay result is inconsistent")
    result = outcome.get("result")
    if result == "FAIL":
        _exact(outcome, {"result", "failure"}, "FAIL outcome")
        failure = _mapping(outcome["failure"], "private failure")
        _exact(
            failure,
            {
                "boundary",
                "public_type",
                "raw_error_type",
                "last_completed_phase",
            },
            "private failure",
        )
        for field in (
            "boundary",
            "public_type",
            "raw_error_type",
            "last_completed_phase",
        ):
            if (
                not isinstance(failure[field], str)
                or not failure[field].isidentifier()
                or len(failure[field]) > 128
            ):
                raise CapturePrivateReplayError("private failure is not sanitized")
        expected_fail: Mapping[str, object] | None = {
            "schema": "onec-agent-capture-live-observations-v1",
            "attempt": 2,
            "result": "FAIL",
            "call_sequence": call_sequence,
            "failure": {
                "boundary": failure["boundary"],
                "type": failure["public_type"],
                "last_completed_phase": failure["last_completed_phase"],
            },
        }
        expected_pass = None
        expected_result = ExpectedCaptureResult.FAIL
    elif result == "PASS":
        _exact(outcome, {"result", "observations"}, "PASS outcome")
        observations = dict(
            _mapping(outcome["observations"], "private PASS observations")
        )
        if (
            observations.get("schema")
            != "onec-agent-capture-live-observations-v1"
            or observations.get("attempt") != 2
            or observations.get("result") != "PASS"
            or observations.get("call_sequence") != call_sequence
        ):
            raise CapturePrivateReplayError(
                "private PASS observations are inconsistent"
            )
        expected_pass = observations
        expected_fail = None
        expected_result = ExpectedCaptureResult.PASS
    else:
        raise CapturePrivateReplayError("private replay result is invalid")
    static_markers = tuple(
        str(_mapping(item, "private marker")["value"])
        for item in fixture["private_markers"]  # type: ignore[union-attr]
    )
    explicit_markers = tuple(
        str(_mapping(entry["payload"], "private marker event")["value"])
        for entry in entries
        if entry["kind"] == "private_marker"
    )
    private_markers = _ordered_unique(
        (*static_markers, *explicit_markers, *dynamic_markers)
    )
    private_pids = tuple(
        int(item["pid"]) for item in (*owned, *unrelated)
    )
    return CapturePrivateReplayExpectation(
        attempt=2,
        expected_result=expected_result,
        expected_environment=environment,
        expected_preflight=preflight,
        expected_cleanup_facts=cleanup_facts,
        expected_owned_processes=tuple(owned),
        expected_unrelated_processes=tuple(unrelated),
        expected_database_identity_sha256=database_digest,
        expected_pass_facts=expected_pass,
        expected_fail_facts=expected_fail,
        target_database_process_count=target_database_process_count,
        control_token_state=control_token_state,
        control_descriptor_state=control_descriptor_state,
        private_markers=private_markers,
        private_pids=private_pids,
    )


def _append_journal_event(
    private_attempt: Path,
    *,
    kind: str,
    payload: Mapping[str, object],
) -> dict[str, object]:
    private_attempt = _require_private_attempt_path(private_attempt)
    if kind not in _EVENT_KINDS or not isinstance(payload, Mapping):
        raise CapturePrivateReplayError("private replay journal event is invalid")
    event_payload = dict(payload)
    if (
        kind == "private_marker"
        and isinstance(event_payload.get("kind"), str)
        and str(event_payload["kind"]).casefold() in _FORBIDDEN_CREDENTIAL_KEYS
    ):
        raise CapturePrivateReplayError("credential or token material is forbidden")
    _reject_credentials(event_payload)
    _validate_event(kind, event_payload)
    prefix = load_capture_private_journal_prefix(private_attempt)
    if prefix[-1]["kind"] == "complete":
        raise CapturePrivateReplayError("private replay journal is already complete")
    entry = _journal_entry(
        sequence=len(prefix) + 1,
        previous_sha256=str(prefix[-1]["entry_sha256"]),
        kind=kind,
        payload=event_payload,
    )
    try:
        with (private_attempt / _JOURNAL_FILE).open("ab") as stream:
            stream.write(_canonical(entry))
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as error:
        raise CapturePrivateReplayError("private replay journal append failed") from error
    return entry


def _validate_event(kind: str, payload: Mapping[str, object]) -> None:
    if kind == "call_started":
        _exact(
            payload,
            {
                "index",
                "stage",
                "public_label",
                "operation_group",
                "method",
                "arguments",
            },
            "call_started event",
        )
        _validate_call_identity(payload)
        if not isinstance(payload["arguments"], Mapping):
            raise CapturePrivateReplayError("call_started arguments are invalid")
        return
    if kind == "call_response":
        _exact(
            payload,
            {
                "accepted",
                "index",
                "stage",
                "public_label",
                "operation_group",
                "method",
                "raw",
            },
            "call_response event",
        )
        _validate_call_identity(payload)
        if type(payload["accepted"]) is not bool:
            raise CapturePrivateReplayError("call_response acceptance is invalid")
        raw = _mapping(payload["raw"], "raw MCP binding")
        _exact(raw, {"index", "method", "path", "sha256"}, "raw MCP binding")
        if (
            raw["index"] != payload["index"]
            or raw["method"] != payload["method"]
            or not isinstance(raw["path"], str)
            or raw["path"]
            != f"raw-mcp/{int(payload['index']):04d}-{str(payload['method']).replace('.', '-')}.json"
        ):
            raise CapturePrivateReplayError("raw MCP binding identity is invalid")
        _require_hash(raw["sha256"], "raw MCP binding")
        return
    if kind == "stage":
        if set(payload) not in ({"name"}, {"name", "public_label"}):
            raise CapturePrivateReplayError("stage event key set is not exact")
        if not isinstance(payload["name"], str) or not payload["name"].isidentifier():
            raise CapturePrivateReplayError("stage event name is invalid")
        public_label = payload.get("public_label")
        if public_label is not None and (
            not isinstance(public_label, str) or not public_label
        ):
            raise CapturePrivateReplayError("stage public label is invalid")
        return
    if kind == "private_marker":
        _exact(payload, {"kind", "value"}, "private marker event")
        if (
            payload["kind"] != "service_instance_id"
            or not isinstance(payload["value"], str)
            or not payload["value"]
        ):
            raise CapturePrivateReplayError("private marker event is invalid")
        return
    if kind == "cleanup":
        _exact(
            payload,
            {
                "owned_ledger",
                "owned",
                "unrelated",
                "errors",
                "runtime_owner_state",
                "target_database_process_count",
                "control_token_state",
                "control_descriptor_state",
            },
            "cleanup event",
        )
        target_count = payload["target_database_process_count"]
        if (
            payload["runtime_owner_state"] not in {"absent", "alive", "unknown"}
            or payload["control_token_state"] not in {"absent", "alive", "unknown"}
            or payload["control_descriptor_state"]
            not in {"absent", "alive", "unknown"}
            or not (
                target_count is None
                or (type(target_count) is int and target_count >= 0)
            )
        ):
            raise CapturePrivateReplayError("private cleanup boundary is invalid")
        return
    if kind == "postflight":
        _exact(payload, {"snapshots"}, "postflight event")
        if not isinstance(payload["snapshots"], Mapping):
            raise CapturePrivateReplayError("postflight snapshots are invalid")
        return
    if kind == "outcome":
        if payload.get("result") == "FAIL":
            _exact(payload, {"result", "failure"}, "FAIL outcome")
            if not isinstance(payload["failure"], Mapping):
                raise CapturePrivateReplayError("FAIL outcome is invalid")
        elif payload.get("result") == "PASS":
            _exact(payload, {"result", "observations"}, "PASS outcome")
            if not isinstance(payload["observations"], Mapping):
                raise CapturePrivateReplayError("PASS outcome is invalid")
        else:
            raise CapturePrivateReplayError("outcome result is invalid")
        return
    if kind == "complete":
        _exact(payload, {"result"}, "complete event")
        if payload["result"] not in {"PASS", "FAIL"}:
            raise CapturePrivateReplayError("complete result is invalid")
        return
    raise CapturePrivateReplayError("private replay journal event kind is invalid")


def _validate_call_identity(payload: Mapping[str, object]) -> None:
    index = payload["index"]
    method = payload["method"]
    stage = payload["stage"]
    public_label = payload["public_label"]
    operation_group = payload["operation_group"]
    if (
        type(index) is not int
        or index <= 0
        or not isinstance(method, str)
        or _METHOD.fullmatch(method) is None
        or not isinstance(stage, str)
        or not stage
        or (public_label is not None and not isinstance(public_label, str))
        or (operation_group is not None and not isinstance(operation_group, str))
    ):
        raise CapturePrivateReplayError("private call identity is invalid")


def _validate_fixture(value: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise CapturePrivateReplayError("private fixture must be an object")
    fixture = dict(value)
    _reject_credentials(fixture)
    _exact(
        fixture,
        {
            "schema",
            "attempt",
            "authorization",
            "paths",
            "invocation",
            "target_database",
            "unrelated_processes",
            "private_markers",
            "preflight",
            "bindings",
            "pass_contract",
        },
        "private fixture",
    )
    if fixture["schema"] != _FIXTURE_SCHEMA or fixture["attempt"] != 2:
        raise CapturePrivateReplayError("private fixture attempt is invalid")
    authorization = _mapping(fixture["authorization"], "authorization")
    _exact(authorization, {"capture_attempt", "live_flag"}, "authorization")
    if authorization != {"capture_attempt": "2", "live_flag": "1"}:
        raise CapturePrivateReplayError("private fixture authorization is invalid")
    paths = _mapping(fixture["paths"], "fixture paths")
    _exact(paths, {"public_leaf", "private_leaf"}, "fixture paths")
    if paths != {"public_leaf": "attempt-2", "private_leaf": "attempt-2"}:
        raise CapturePrivateReplayError("private fixture paths are invalid")
    invocation = _mapping(fixture["invocation"], "invocation")
    _exact(
        invocation,
        {
            "profile",
            "maximum_mode",
            "mutation_requested",
            "username",
            "roots",
            "request_ids",
            "timeouts",
        },
        "invocation",
    )
    if (
        invocation["profile"] != "capture"
        or invocation["maximum_mode"] != "experiment"
        or invocation["mutation_requested"] is not False
        or not isinstance(invocation["username"], str)
        or not invocation["username"]
    ):
        raise CapturePrivateReplayError("private fixture invocation is invalid")
    roots = _mapping(invocation["roots"], "invocation roots")
    _exact(
        roots,
        {"workspace", "platform_bin", "infobase", "source_root", "python"},
        "invocation roots",
    )
    if any(not _is_absolute_text(item) for item in roots.values()):
        raise CapturePrivateReplayError("private fixture roots must be absolute")
    request_ids = _mapping(invocation["request_ids"], "request IDs")
    expected_request_ids = {
        "main": "capture-task-6-main-attempt-2",
        "hypothesis": "capture-task-6-hypothesis-attempt-2",
        "continue_b": "capture-task-6-continue-b-attempt-2",
        "finish_main": "capture-task-6-finish-main-attempt-2",
    }
    if request_ids != expected_request_ids:
        raise CapturePrivateReplayError("private fixture request IDs are invalid")
    timeouts = _mapping(invocation["timeouts"], "timeout policy")
    if timeouts != {
        "overall_timeout_s": 1800.0,
        "sdk_outer_timeout_s": 600.0,
        "control_timeout_s": 180.0,
        "schema_wait_max_s": 30.0,
    }:
        raise CapturePrivateReplayError("private fixture timeout policy is invalid")
    database = _mapping(fixture["target_database"], "target database")
    _exact(database, {"filesystem_key", "size"}, "target database")
    if (
        not isinstance(database["filesystem_key"], str)
        or not database["filesystem_key"]
        or type(database["size"]) is not int
        or database["size"] < 0
    ):
        raise CapturePrivateReplayError("target database preimage is invalid")
    unrelated = fixture["unrelated_processes"]
    if not isinstance(unrelated, list) or len(unrelated) > 100:
        raise CapturePrivateReplayError("unrelated process preimages are invalid")
    for item in unrelated:
        _validate_process_wire(item, allowed_role_prefix="unrelated_")
    markers = fixture["private_markers"]
    if not isinstance(markers, list) or not markers or len(markers) > 10_000:
        raise CapturePrivateReplayError("private fixture markers are invalid")
    observed_kinds: list[str] = []
    for item in markers:
        marker = _mapping(item, "private marker")
        _exact(marker, {"kind", "value"}, "private marker")
        kind = marker["kind"]
        marker_value = marker["value"]
        if (
            kind not in _STATIC_MARKER_KINDS
            or not isinstance(marker_value, str)
            or not marker_value
        ):
            raise CapturePrivateReplayError("private fixture marker is invalid")
        observed_kinds.append(str(kind))
    if len(set(observed_kinds)) != len(observed_kinds):
        raise CapturePrivateReplayError("private fixture markers are duplicated")
    preflight = _mapping(fixture["preflight"], "preflight")
    _exact(
        preflight,
        {"platform", "source", "notebook", "implementation"},
        "preflight",
    )
    bindings = _mapping(fixture["bindings"], "fixture bindings")
    _exact(
        bindings,
        {
            "platform_sha256",
            "source_sha256",
            "notebook_sha256",
            "implementation_sha256",
        },
        "fixture bindings",
    )
    expected_bindings = {
        "platform_sha256": _mapping(preflight["platform"], "platform preflight").get(
            "bin_identity_sha256"
        ),
        "source_sha256": _mapping(preflight["source"], "source preflight").get(
            "tree_sha256"
        ),
        "notebook_sha256": _mapping(
            preflight["notebook"], "notebook preflight"
        ).get("artifact_sha256"),
        "implementation_sha256": _mapping(
            preflight["implementation"], "implementation preflight"
        ).get("runtime_tree_sha256"),
    }
    if bindings != expected_bindings:
        raise CapturePrivateReplayError("private fixture hash bindings are invalid")
    for digest in bindings.values():
        _require_hash(digest, "private fixture binding")
    contract = _mapping(fixture["pass_contract"], "PASS contract")
    _exact(
        contract,
        {
            "call_sequence",
            "main",
            "hypothesis",
            "stop_bindings",
            "capture_a_local_names",
            "table_name",
            "downstream_result_local",
            "budgets",
        },
        "PASS contract",
    )
    if contract["call_sequence"] != list(CAPTURE_PASS_CALL_SEQUENCE):
        raise CapturePrivateReplayError("private fixture PASS sequence is invalid")
    return fixture


def _load_fixture_digest(private_attempt: Path) -> str:
    fixture = _load_fixture(private_attempt)
    return sha256(_canonical(fixture)).hexdigest()


def _load_fixture(private_attempt: Path) -> dict[str, object]:
    fixture_path = private_attempt / _FIXTURE_FILE
    manifest_path = private_attempt / _FIXTURE_MANIFEST
    if not fixture_path.is_file():
        raise CapturePrivateReplayError("private fixture is missing")
    try:
        payload = fixture_path.read_bytes()
        manifest = manifest_path.read_text(encoding="ascii")
    except (OSError, UnicodeError) as error:
        raise CapturePrivateReplayError("private fixture is unreadable") from error
    fixture = _read_canonical_object(payload, name="private fixture")
    _validate_fixture(fixture)
    digest = sha256(payload).hexdigest()
    if manifest != f"{digest}  {_FIXTURE_FILE}\n":
        raise CapturePrivateReplayError("private fixture hash is invalid")
    return fixture


def _one_event(
    entries: Sequence[Mapping[str, object]], kind: str
) -> Mapping[str, object]:
    matches = [entry for entry in entries if entry.get("kind") == kind]
    if len(matches) != 1:
        raise CapturePrivateReplayError(
            f"private replay requires exactly one {kind} event"
        )
    return matches[0]


def _load_call_evidence(
    private_attempt: Path,
    entries: Sequence[Mapping[str, object]],
) -> tuple[list[str], tuple[str, ...]]:
    starts: dict[int, Mapping[str, object]] = {}
    responded: set[int] = set()
    public_calls: list[str] = []
    markers: list[str] = []
    referenced: set[str] = set()
    expected_index = 1
    for entry in entries:
        kind = entry.get("kind")
        payload = _mapping(entry.get("payload"), "call journal payload")
        if kind == "call_started":
            index = payload["index"]
            if index != expected_index or index in starts:
                raise CapturePrivateReplayError("private call order is invalid")
            starts[int(index)] = payload
            expected_index += 1
        elif kind == "call_response":
            index = int(payload["index"])
            start = starts.get(index)
            if start is None or index in responded:
                raise CapturePrivateReplayError("private call response order is invalid")
            for field in (
                "index",
                "stage",
                "public_label",
                "operation_group",
                "method",
            ):
                if payload[field] != start[field]:
                    raise CapturePrivateReplayError(
                        "private call response differs from its start"
                    )
            raw = _mapping(payload["raw"], "raw MCP binding")
            relative = str(raw["path"])
            path = private_attempt / Path(relative)
            try:
                raw_payload = path.read_bytes()
            except OSError as error:
                raise CapturePrivateReplayError("raw MCP response is missing") from error
            if not hmac.compare_digest(
                sha256(raw_payload).hexdigest(), str(raw["sha256"])
            ):
                raise CapturePrivateReplayError("raw MCP response hash is invalid")
            raw_value = _read_canonical_value(raw_payload, name="raw MCP response")
            _collect_private_markers(raw_value, markers)
            if payload["accepted"]:
                envelope = _mapping(raw_value, "accepted raw MCP response")
                if envelope.get("ok") is not True or "value" not in envelope:
                    raise CapturePrivateReplayError(
                        "accepted raw MCP response is invalid"
                    )
                label = payload["public_label"]
                if isinstance(label, str):
                    public_calls.append(label)
            referenced.add(relative)
            responded.add(index)
        elif kind == "stage":
            label = payload.get("public_label")
            if isinstance(label, str):
                public_calls.append(label)
    if responded != set(starts):
        raise CapturePrivateReplayError("private replay has an unfinished call")
    raw_dir = private_attempt / "raw-mcp"
    actual = (
        {path.relative_to(private_attempt).as_posix() for path in raw_dir.glob("*.json")}
        if raw_dir.is_dir()
        else set()
    )
    if actual != referenced:
        raise CapturePrivateReplayError("raw MCP response file set is not exact")
    return public_calls, tuple(markers)


def _load_cleanup(
    private_attempt: Path,
    *,
    fixture: Mapping[str, object],
    event: Mapping[str, object],
) -> tuple[
    tuple[Mapping[str, object], ...],
    tuple[Mapping[str, object], ...],
    ExpectedCaptureCleanupFacts,
    int | None,
    str,
    str,
]:
    ledger_binding = _mapping(event["owned_ledger"], "owned ledger binding")
    _exact(ledger_binding, {"path", "sha256"}, "owned ledger binding")
    if ledger_binding["path"] != "owned-processes.json":
        raise CapturePrivateReplayError("owned ledger path is invalid")
    _require_hash(ledger_binding["sha256"], "owned ledger")
    ledger_path = private_attempt / "owned-processes.json"
    try:
        ledger_payload = ledger_path.read_bytes()
    except OSError as error:
        raise CapturePrivateReplayError("owned ledger is missing") from error
    if not hmac.compare_digest(
        sha256(ledger_payload).hexdigest(), str(ledger_binding["sha256"])
    ):
        raise CapturePrivateReplayError("owned ledger hash is invalid")
    ledger = _read_json_object(ledger_payload, name="owned ledger")
    _exact(ledger, {"attempt", "owned"}, "owned ledger")
    if ledger["attempt"] != 2 or not isinstance(ledger["owned"], list):
        raise CapturePrivateReplayError("owned ledger is invalid")
    owned = tuple(_mapping(item, "owned process preimage") for item in ledger["owned"])
    for wire in owned:
        _validate_process_wire(wire, allowed_role_prefix="")
    unrelated_value = fixture["unrelated_processes"]
    if not isinstance(unrelated_value, list):
        raise CapturePrivateReplayError("unrelated process preimages are invalid")
    unrelated = tuple(
        _mapping(item, "unrelated process preimage") for item in unrelated_value
    )
    actual_owned = event["owned"]
    actual_unrelated = event["unrelated"]
    errors = event["errors"]
    if (
        not isinstance(actual_owned, list)
        or not isinstance(actual_unrelated, list)
        or not isinstance(errors, list)
        or any(
            not isinstance(item, str)
            or not item.isidentifier()
            or len(item) > 128
            for item in errors
        )
    ):
        raise CapturePrivateReplayError("private cleanup observations are invalid")
    expected_owned = []
    owned_states: list[str] = []
    owned_roles: list[str] = []
    for wire, item in zip(owned, actual_owned, strict=True):
        observed = _mapping(item, "owned cleanup observation")
        _exact(
            observed,
            {"role", "identity_sha256", "state"},
            "owned cleanup observation",
        )
        expected = {
            "role": wire["role"],
            "identity_sha256": process_identity_sha256(2, wire),
            "state": observed["state"],
        }
        if observed != expected or observed["state"] != "absent":
            raise CapturePrivateReplayError("owned cleanup observation is invalid")
        expected_owned.append(expected)
        owned_roles.append(str(wire["role"]))
        owned_states.append(str(observed["state"]))
    if len(expected_owned) != len(actual_owned):
        raise CapturePrivateReplayError("owned cleanup count is not exact")
    unrelated_states: list[str] = []
    expected_unrelated = []
    for wire, item in zip(unrelated, actual_unrelated, strict=True):
        observed = _mapping(item, "unrelated cleanup observation")
        _exact(
            observed,
            {"identity_sha256", "state"},
            "unrelated cleanup observation",
        )
        expected = {
            "identity_sha256": process_identity_sha256(2, wire),
            "state": observed["state"],
        }
        if observed != expected or observed["state"] != "alive":
            raise CapturePrivateReplayError("unrelated cleanup observation is invalid")
        expected_unrelated.append(expected)
        unrelated_states.append(str(observed["state"]))
    if len(expected_unrelated) != len(actual_unrelated):
        raise CapturePrivateReplayError("unrelated cleanup count is not exact")
    cleanup_facts = ExpectedCaptureCleanupFacts(
        errors=tuple(errors),
        cleanup_error_count=len(errors),
        runtime_owner_state=str(event["runtime_owner_state"]),
        owned_roles=tuple(owned_roles),
        owned_states=tuple(owned_states),
        unrelated_states=tuple(unrelated_states),
    )
    target_count = event["target_database_process_count"]
    if target_count is not None and type(target_count) is not int:
        raise CapturePrivateReplayError("target database process count is invalid")
    return (
        owned,
        unrelated,
        cleanup_facts,
        target_count,
        str(event["control_token_state"]),
        str(event["control_descriptor_state"]),
    )


def _database_identity_sha256(value: Mapping[str, object]) -> str:
    digest = sha256()
    for part in (
        "onec-capture-database-v1",
        str(value["filesystem_key"]),
        str(value["size"]),
    ):
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _collect_private_markers(value: object, result: list[str]) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if (
                isinstance(key, str)
                and key.casefold() in _PRIVATE_ID_KEYS
                and isinstance(nested, str)
                and nested
                and nested not in result
            ):
                result.append(nested)
            _collect_private_markers(nested, result)
    elif isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        for nested in value:
            _collect_private_markers(nested, result)


def _ordered_unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _journal_entry(
    *,
    sequence: int,
    previous_sha256: str,
    kind: str,
    payload: Mapping[str, object],
) -> dict[str, object]:
    unsigned: dict[str, object] = {
        "schema": _JOURNAL_SCHEMA,
        "attempt": 2,
        "sequence": sequence,
        "previous_sha256": previous_sha256,
        "kind": kind,
        "payload": dict(payload),
    }
    return {**unsigned, "entry_sha256": sha256(_canonical(unsigned)).hexdigest()}


def _reject_credentials(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise CapturePrivateReplayError("private fixture key is invalid")
            if key.casefold() in _FORBIDDEN_CREDENTIAL_KEYS:
                raise CapturePrivateReplayError(
                    "credential or token material is forbidden"
                )
            _reject_credentials(nested)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for nested in value:
            _reject_credentials(nested)
        return
    if isinstance(value, str) and value.casefold().startswith(("bearer ", "basic ")):
        raise CapturePrivateReplayError("credential or token material is forbidden")


def _validate_process_wire(value: object, *, allowed_role_prefix: str) -> None:
    wire = _mapping(value, "process preimage")
    _exact(wire, {"role", "pid", "create_time", "exe"}, "process preimage")
    role = wire["role"]
    created = wire["create_time"]
    if (
        not isinstance(role, str)
        or not role.startswith(allowed_role_prefix)
        or not role.isidentifier()
        or type(wire["pid"]) is not int
        or wire["pid"] <= 0
        or type(created) not in {int, float}
        or not isfinite(float(created))
        or float(created) <= 0
        or not _is_absolute_text(wire["exe"])
    ):
        raise CapturePrivateReplayError("process preimage is invalid")


def _require_private_attempt_path(value: Path) -> Path:
    path = Path(value)
    if path.name != "attempt-2" or path.parent.name != "capture-live-private":
        raise CapturePrivateReplayError("private replay path must bind attempt 2")
    return path


def _write_create_once(path: Path, payload: bytes) -> None:
    try:
        with path.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(path, 0o600)
    except FileExistsError as error:
        raise CapturePrivateReplayError("private replay file already exists") from error
    except OSError as error:
        raise CapturePrivateReplayError("private replay file creation failed") from error


def _read_canonical_object(payload: bytes, *, name: str) -> dict[str, object]:
    value = _read_canonical_value(payload, name=name)
    if not isinstance(value, dict):
        raise CapturePrivateReplayError(f"{name} must be an object")
    return value


def _read_canonical_value(payload: bytes, *, name: str) -> object:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise CapturePrivateReplayError(f"{name} has duplicate keys")
            result[key] = value
        return result

    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=pairs)
    except CapturePrivateReplayError:
        raise
    except (UnicodeError, json.JSONDecodeError) as error:
        raise CapturePrivateReplayError(f"{name} is not valid JSON") from error
    if payload != _canonical(value):
        raise CapturePrivateReplayError(f"{name} is not canonical")
    return value


def _read_json_object(payload: bytes, *, name: str) -> dict[str, object]:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise CapturePrivateReplayError(f"{name} has duplicate keys")
            result[key] = value
        return result

    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=pairs)
    except CapturePrivateReplayError:
        raise
    except (UnicodeError, json.JSONDecodeError) as error:
        raise CapturePrivateReplayError(f"{name} is not valid JSON") from error
    if not isinstance(value, dict):
        raise CapturePrivateReplayError(f"{name} must be an object")
    return value


def _canonical(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise CapturePrivateReplayError("private replay data is not canonical JSON") from error


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise CapturePrivateReplayError(f"{name} must be an object")
    return value


def _exact(value: Mapping[str, object], keys: set[str], name: str) -> None:
    if set(value) != keys:
        raise CapturePrivateReplayError(f"{name} key set is not exact")


def _require_hash(value: object, name: str) -> None:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise CapturePrivateReplayError(f"{name} must be a SHA-256")


def _is_absolute_text(value: object) -> bool:
    return isinstance(value, str) and bool(value) and (
        Path(value).is_absolute() or PureWindowsPath(value).is_absolute()
    )


__all__ = [
    "CapturePrivateJournal",
    "CapturePrivateReplayExpectation",
    "CapturePrivateReplayError",
    "create_capture_private_replay",
    "load_capture_private_journal_prefix",
    "load_capture_private_replay",
    "publish_capture_private_replay",
    "record_capture_private_raw_response",
    "verify_capture_private_replay",
]
