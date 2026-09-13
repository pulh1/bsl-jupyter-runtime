"""Publication-safe evidence contracts for two-point MCP CAPTURE acceptance."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
import hmac
import json
from math import isfinite
from pathlib import Path, PureWindowsPath
import re


CAPTURE_PASS_CALL_SEQUENCE = (
    "A:workspace.open",
    "A:runtime.ensure",
    "A:operation.wait.runtime",
    "A:code.run_inline.discovery",
    "A:code.list",
    "A:code.get.main",
    "A:code.get.hypothesis",
    "A:capture.run_until.a",
    "A:frontend.exit",
    "B:workspace.open",
    "B:runtime.ensure",
    "B:operation.replay",
    "B:capture.inspect.locals",
    "B:capture.inspect.manager",
    "B:capture.inspect.table",
    "B:value.to_df.head",
    "B:code.get.hypothesis",
    "B:capture.hypothesis",
    "B:capture.continue",
    "B:value.inspect.stale",
    "B:capture.inspect.downstream.manager",
    "B:capture.inspect.downstream.table",
    "B:value.inspect.result",
    "B:capture.continue.terminal",
    "B:runtime.close",
    "owner:service.shutdown",
)
CAPTURE_OWNED_ROLES = (
    "service",
    "mcp_a",
    "mcp_b",
    "designer",
    "dbgs",
    "onec",
)
_FILES = (
    "environment.json",
    "observations.json",
    "cleanup.json",
    "summary.json",
)
_SNAPSHOT_NAMES = ("platform", "source", "notebook", "implementation")
_SNAPSHOT_SENTINEL_FIELDS = {
    "platform": "bin_identity_sha256",
    "source": "tree_sha256",
    "notebook": "artifact_sha256",
    "implementation": "runtime_tree_sha256",
}
_HASH = re.compile(r"^[0-9a-f]{64}$")
_PUBLIC_EVIDENCE_SHA256_LEAF_FIELDS = frozenset(
    {
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
    }
)
_PUBLIC_EVIDENCE_SHA256_MAP_CHILDREN = {
    "bundle_file_sha256": frozenset((*_FILES, "manifest.sha256")),
    "executables_sha256": frozenset({"1cv8.exe", "1cv8c.exe", "dbgs.exe"}),
    "source_files_sha256": frozenset(
        {"environment.json", "observations.json", "cleanup.json"}
    ),
}
_PUBLIC_EVIDENCE_SHA256_SEQUENCE_FIELDS = frozenset(
    {
        "acknowledged_root_name_sha256s",
        "dirty_root_name_sha256s",
        "old_proxy_identity_sha256s",
        "selected_name_sha256s",
        "stale_proxy_identity_sha256s",
    }
)
_UUID = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b"
)
_PRIVATE_TEXT = re.compile(
    r"(?i)(?:\b(?:pid|process[_ ]?id)\s*[:=#]\s*\d+\b|\brdbg\b|\btoken\b|\.runtime[\\/])"
)
_PATH_TEXT = re.compile(
    r"(?i)(?:[a-z]:[\\/]|\\\\|(?:^|[\s\"'])/(?:home|users|tmp|var|opt|workspace|repo)/)"
)
_PRIVATE_KEYS = {
    "artifact_path",
    "business_rows",
    "cmdline",
    "command",
    "create_time",
    "database_path",
    "exe",
    "executable",
    "infobase_path",
    "password",
    "path",
    "pid",
    "platform_identity",
    "platform_uuid",
    "private_artifact",
    "process_id",
    "raw_response",
    "raw_rows",
    "raw_source",
    "rdbg",
    "rows",
    "runtime_id",
    "source_text",
    "token",
    "username",
    "uuid",
}
_TRI_STATES = {"absent", "alive", "unknown"}
_CAPTURE_ATTEMPTS = frozenset({1, 2})


class CaptureEvidenceError(RuntimeError):
    """The CAPTURE evidence bundle is incomplete, mutable, or private."""


class ExpectedCaptureResult(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"


@dataclass(frozen=True, slots=True)
class ExpectedCaptureCleanupFacts:
    """Externally supplied public cleanup facts for one evidence result."""

    errors: tuple[str, ...]
    cleanup_error_count: int
    runtime_owner_state: str
    owned_roles: tuple[str, ...]
    owned_states: tuple[str, ...]
    unrelated_states: tuple[str, ...]

    def __post_init__(self) -> None:
        tuple_fields = (
            self.errors,
            self.owned_roles,
            self.owned_states,
            self.unrelated_states,
        )
        if any(type(value) is not tuple for value in tuple_fields):
            raise TypeError("expected cleanup sequences must be tuples")
        if (
            type(self.cleanup_error_count) is not int
            or self.cleanup_error_count < 0
            or self.cleanup_error_count != len(self.errors)
        ):
            raise ValueError("expected cleanup error count is not exact")
        if self.runtime_owner_state not in _TRI_STATES:
            raise ValueError("expected runtime owner state is invalid")
        if (
            len(self.errors) > 100
            or any(
                not isinstance(error, str)
                or not error.isidentifier()
                or len(error) > 128
                for error in self.errors
            )
        ):
            raise ValueError("expected cleanup errors are not sanitized")
        if (
            len(self.owned_roles) > 20
            or len(self.owned_roles) != len(self.owned_states)
            or len(set(self.owned_roles)) != len(self.owned_roles)
            or any(
                not isinstance(role, str) or not role.isidentifier()
                for role in self.owned_roles
            )
        ):
            raise ValueError("expected owned cleanup roles are invalid")
        if (
            len(self.unrelated_states) > 100
            or any(state not in _TRI_STATES for state in self.owned_states)
            or any(state not in _TRI_STATES for state in self.unrelated_states)
        ):
            raise ValueError("expected cleanup states are invalid")


@dataclass(frozen=True, slots=True)
class CapturePublishedRegressionExpectation:
    """Post-hoc expectation that locks one already-published public bundle."""

    attempt: int
    provenance: str
    expected_result: ExpectedCaptureResult
    bundle_file_sha256: tuple[tuple[str, str], ...]
    expected_cleanup_facts: ExpectedCaptureCleanupFacts

    def __post_init__(self) -> None:
        _require_capture_attempt(self.attempt)
        if self.provenance != "posthoc_from_published_bundle":
            raise ValueError("published regression provenance is invalid")
        if type(self.expected_result) is not ExpectedCaptureResult:
            raise TypeError("published expected result must be ExpectedCaptureResult")
        if type(self.bundle_file_sha256) is not tuple or any(
            type(item) is not tuple or len(item) != 2
            for item in self.bundle_file_sha256
        ):
            raise TypeError("published bundle hashes must be typed pairs")
        hashes = dict(self.bundle_file_sha256)
        expected_names = {*_FILES, "manifest.sha256"}
        if set(hashes) != expected_names or len(hashes) != len(
            self.bundle_file_sha256
        ):
            raise ValueError("published bundle hash file set is not exact")
        for name, digest in hashes.items():
            _require_hash(digest, f"published {name}")
        if type(self.expected_cleanup_facts) is not ExpectedCaptureCleanupFacts:
            raise TypeError(
                "published cleanup expectation must be ExpectedCaptureCleanupFacts"
            )


@dataclass(frozen=True, slots=True)
class CaptureAttemptPaths:
    public: Path
    private: Path


def _require_capture_attempt(attempt: int) -> int:
    if type(attempt) is not int:
        raise TypeError("capture attempt must be an integer")
    if attempt not in _CAPTURE_ATTEMPTS:
        raise ValueError("capture attempt must be exactly 1 or 2")
    return attempt


def capture_identity_salt(attempt: int) -> str:
    """Return the public domain-separation salt for one authorized attempt."""
    attempt = _require_capture_attempt(attempt)
    return f"onec-agent-capture-2026-08-20-attempt-{attempt}"


def _require_attempt_destination(destination: Path, *, attempt: int) -> Path:
    attempt = _require_capture_attempt(attempt)
    destination = Path(destination)
    if destination.name != f"attempt-{attempt}":
        raise CaptureEvidenceError(
            "evidence destination leaf does not bind the exact attempt"
        )
    return destination


def snapshot_unavailable_sha256(*, stage: str, state: str, prior: str) -> str:
    """Derive the public postflight sentinel for one unavailable input."""
    if stage not in _SNAPSHOT_SENTINEL_FIELDS:
        raise ValueError("snapshot stage is invalid")
    if state not in {"missing", "unreadable"}:
        raise ValueError("snapshot input state is invalid")
    if not isinstance(prior, str) or _HASH.fullmatch(prior) is None:
        raise ValueError("prior snapshot hash is invalid")
    return sha256(
        (
            "onec-capture-unavailable-snapshot-v1\0"
            + stage
            + "\0"
            + state
            + "\0"
            + prior
        ).encode("utf-8")
    ).hexdigest()


def capture_attempt_paths(workspace: Path, *, attempt: int) -> CaptureAttemptPaths:
    """Return disjoint paths for either authorized immutable live attempt."""
    attempt = _require_capture_attempt(attempt)
    root = Path(workspace).resolve()
    return CaptureAttemptPaths(
        public=(
            root
            / "docs"
            / "research"
            / "evidence"
            / "2026-08-20-mcp-capture"
            / f"attempt-{attempt}"
        ),
        private=(
            root
            / ".runtime"
            / "agent-service"
            / "capture-live-private"
            / f"attempt-{attempt}"
        ),
    )


def process_identity_sha256(attempt: int, identity: Mapping[str, object]) -> str:
    """Bind a private exact PID/create-time/executable identity to public evidence."""
    attempt = _require_capture_attempt(attempt)
    _exact(identity, {"role", "pid", "create_time", "exe"}, "process identity")
    role = identity["role"]
    pid = identity["pid"]
    created = identity["create_time"]
    executable = identity["exe"]
    if not isinstance(role, str) or not role.isidentifier() or len(role) > 64:
        raise ValueError("process role is invalid")
    if type(pid) is not int or pid <= 0:
        raise ValueError("process PID is invalid")
    if (
        type(created) not in {int, float}
        or not isfinite(float(created))
        or float(created) <= 0
    ):
        raise ValueError("process create_time is invalid")
    if not isinstance(executable, str) or not executable or len(executable) > 4096:
        raise ValueError("process executable is invalid")
    if not (Path(executable).is_absolute() or PureWindowsPath(executable).is_absolute()):
        raise ValueError("process executable must be absolute")
    digest = sha256()
    for part in (
        "onec-capture-process-v1",
        str(attempt),
        role,
        str(pid),
        float(created).hex(),
        str(PureWindowsPath(executable)).casefold(),
    ):
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def build_capture_live_summary(
    *,
    attempt: int = 1,
    environment: Mapping[str, object],
    observations: Mapping[str, object],
    cleanup: Mapping[str, object],
) -> dict[str, object]:
    """Recompute the compact outcome from allowlisted source facts."""
    attempt = _require_capture_attempt(attempt)
    _assert_public(environment)
    _assert_public(observations)
    _assert_public(cleanup)
    result = observations.get("result")
    recorded_attempt, drift = _validate_environment(
        environment,
        expected_attempt=attempt,
        allow_drift=result == ExpectedCaptureResult.FAIL.value,
    )
    if result == "PASS":
        _validate_pass_observations(
            observations, environment=environment, expected_attempt=attempt
        )
        _validate_cleanup(
            cleanup,
            expected_result=ExpectedCaptureResult.PASS,
            expected_attempt=attempt,
        )
        capture_a = _mapping(observations["capture_a"], "capture_a")
        capture_b = _mapping(observations["capture_b"], "capture_b")
        continuation = _mapping(observations["continuation"], "continuation")
        terminal = _mapping(observations["terminal"], "terminal")
        summary: dict[str, object] = {
            "schema": "onec-agent-capture-live-summary-v1",
            "status": "PASS",
            "attempt": recorded_attempt,
            "capture_generations": [
                _mapping(capture_a["fence"], "capture_a fence")["capture_generation"],
                _mapping(capture_b["fence"], "capture_b fence")["capture_generation"],
            ],
            "continuation_calls": continuation["call_count"],
            "terminal_state": terminal["state"],
            "owned_process_count_after_cleanup": 0,
            "cleanup_states": [item["state"] for item in cleanup["owned"]],  # type: ignore[index]
        }
    elif result == "FAIL":
        _validate_fail_observations(observations, expected_attempt=attempt)
        _validate_fail_drift(observations, drift=drift, environment=environment)
        _validate_cleanup(
            cleanup,
            expected_result=ExpectedCaptureResult.FAIL,
            expected_attempt=attempt,
        )
        failure = _mapping(observations["failure"], "failure")
        summary = {
            "schema": "onec-agent-capture-live-summary-v1",
            "status": "FAIL",
            "attempt": recorded_attempt,
            "failure_boundary": failure["boundary"],
            "failure_type": failure["type"],
            "completed_call_count": len(observations["call_sequence"]),  # type: ignore[arg-type]
            "cleanup_states": [item["state"] for item in cleanup["owned"]],  # type: ignore[index]
            "cleanup_error_count": len(cleanup["errors"]),  # type: ignore[arg-type]
        }
    else:
        raise CaptureEvidenceError("observations result must be PASS or FAIL")
    if (
        observations.get("attempt") != recorded_attempt
        or cleanup.get("attempt") != recorded_attempt
    ):
        raise CaptureEvidenceError("attempt identity differs across evidence files")
    summary["source_files_sha256"] = {
        "environment.json": sha256(_canonical(environment)).hexdigest(),
        "observations.json": sha256(_canonical(observations)).hexdigest(),
        "cleanup.json": sha256(_canonical(cleanup)).hexdigest(),
    }
    _assert_public(summary)
    return summary


def write_capture_live_evidence(
    destination: Path,
    *,
    attempt: int = 1,
    environment: Mapping[str, object],
    observations: Mapping[str, object],
    cleanup: Mapping[str, object],
) -> dict[str, object]:
    """Create a canonical immutable bundle; an attempt is never overwritten."""
    attempt = _require_capture_attempt(attempt)
    destination = _require_attempt_destination(destination, attempt=attempt)
    if destination.exists():
        raise CaptureEvidenceError("evidence attempt already exists")
    summary = build_capture_live_summary(
        attempt=attempt,
        environment=environment,
        observations=observations,
        cleanup=cleanup,
    )
    values: dict[str, Mapping[str, object]] = {
        "environment.json": environment,
        "observations.json": observations,
        "cleanup.json": cleanup,
        "summary.json": summary,
    }
    destination.mkdir(parents=True, exist_ok=False)
    hashes: dict[str, str] = {}
    for name in _FILES:
        payload = _canonical(values[name])
        (destination / name).write_bytes(payload)
        hashes[name] = sha256(payload).hexdigest()
    (destination / "manifest.sha256").write_text(
        _manifest_attempt_prefix(attempt)
        + "".join(f"{hashes[name]}  {name}\n" for name in _FILES),
        encoding="ascii",
        newline="\n",
    )
    return summary


def assert_capture_live_evidence_private_safe(
    *,
    attempt: int = 1,
    environment: Mapping[str, object],
    observations: Mapping[str, object],
    cleanup: Mapping[str, object],
    private_markers: Sequence[str] = (),
    private_pids: Sequence[int] = (),
) -> None:
    """Validate public evidence privacy completely before any file is written."""
    summary = build_capture_live_summary(
        attempt=attempt,
        environment=environment,
        observations=observations,
        cleanup=cleanup,
    )
    for value in (environment, observations, cleanup, summary):
        _assert_public(value)
        _assert_external_private_absent(
            value,
            private_markers=private_markers,
            private_pids=private_pids,
        )


def verify_capture_live_evidence(
    destination: Path,
    *,
    attempt: int = 1,
    expected_result: ExpectedCaptureResult,
    expected_cleanup_facts: ExpectedCaptureCleanupFacts,
    expected_preflight: Mapping[str, object],
    expected_owned_processes: Sequence[Mapping[str, object]],
    expected_unrelated_processes: Sequence[Mapping[str, object]],
    expected_database_identity_sha256: str | None = None,
    expected_pass_facts: Mapping[str, object] | None = None,
    expected_fail_facts: Mapping[str, object] | None = None,
    private_markers: Sequence[str] = (),
    private_pids: Sequence[int] = (),
) -> dict[str, object]:
    """Verify hashes, semantics, privacy, and externally held expectations."""
    attempt = _require_capture_attempt(attempt)
    if type(expected_result) is not ExpectedCaptureResult:
        raise TypeError("expected_result must be ExpectedCaptureResult")
    if type(expected_cleanup_facts) is not ExpectedCaptureCleanupFacts:
        raise TypeError("expected_cleanup_facts must be ExpectedCaptureCleanupFacts")
    destination = _require_attempt_destination(destination, attempt=attempt)
    expected_names = {*_FILES, "manifest.sha256"}
    if not destination.is_dir() or {item.name for item in destination.iterdir()} != expected_names:
        raise CaptureEvidenceError("evidence file set is not exact")
    values = {name: _read_object(destination / name) for name in _FILES}
    _verify_manifest(destination, expected_attempt=attempt)
    for value in values.values():
        _assert_public(value)
        _assert_external_private_absent(
            value, private_markers=private_markers, private_pids=private_pids
        )
    environment = values["environment.json"]
    observations = values["observations.json"]
    cleanup = values["cleanup.json"]
    _, drift = _validate_environment(
        environment,
        expected_attempt=attempt,
        expected_preflight=expected_preflight,
        allow_drift=expected_result is ExpectedCaptureResult.FAIL,
    )
    database = _mapping(environment["database"], "database")
    if expected_database_identity_sha256 is None:
        raise CaptureEvidenceError("external target database identity is required")
    _require_hash(expected_database_identity_sha256, "trusted database identity")
    if not hmac.compare_digest(
        str(database["target_identity_sha256"]), expected_database_identity_sha256
    ):
        raise CaptureEvidenceError("target database differs from trusted identity")
    if observations.get("result") != expected_result.value:
        raise CaptureEvidenceError("result differs from the trusted expected result")
    if expected_result is ExpectedCaptureResult.PASS:
        _validate_pass_observations(
            observations, environment=environment, expected_attempt=attempt
        )
        if expected_pass_facts is None:
            raise CaptureEvidenceError("PASS requires external trusted facts")
        _validate_pass_observations(
            expected_pass_facts,
            environment=environment,
            expected_attempt=attempt,
        )
        if not hmac.compare_digest(
            _canonical(observations), _canonical(expected_pass_facts)
        ):
            raise CaptureEvidenceError("PASS differs from external trusted facts")
    else:
        _validate_fail_observations(observations, expected_attempt=attempt)
        _validate_fail_drift(observations, drift=drift, environment=environment)
        if expected_fail_facts is None:
            raise CaptureEvidenceError("FAIL requires external trusted facts")
        _validate_fail_observations(
            expected_fail_facts, expected_attempt=attempt
        )
        if not hmac.compare_digest(
            _canonical(observations), _canonical(expected_fail_facts)
        ):
            raise CaptureEvidenceError("FAIL differs from external trusted facts")
    _validate_cleanup(
        cleanup, expected_result=expected_result, expected_attempt=attempt
    )
    _bind_expected_cleanup(cleanup, expected=expected_cleanup_facts)
    _bind_expected_processes(
        cleanup,
        attempt=attempt,
        expected_owned_processes=expected_owned_processes,
        expected_unrelated_processes=expected_unrelated_processes,
    )
    recomputed = build_capture_live_summary(
        attempt=attempt,
        environment=environment,
        observations=observations,
        cleanup=cleanup,
    )
    if not hmac.compare_digest(
        _canonical(values["summary.json"]), _canonical(recomputed)
    ):
        raise CaptureEvidenceError("summary is not independently derived")
    return recomputed


def load_capture_published_regression_expectation(
    path: Path,
) -> CapturePublishedRegressionExpectation:
    """Load a canonical non-secret post-hoc public-bundle regression fixture."""
    path = Path(path)
    match = re.fullmatch(r"attempt-([12])-public-regression\.json", path.name)
    if match is None:
        raise CaptureEvidenceError(
            "published regression filename does not bind an authorized attempt"
        )
    attempt = _require_capture_attempt(int(match.group(1)))
    value = _read_object(path)
    _exact(
        value,
        {
            "schema",
            "provenance",
            "expected_result",
            "bundle_file_sha256",
            "expected_cleanup",
        },
        "published regression expectation",
    )
    if value["schema"] != "onec-agent-capture-published-regression-v1":
        raise CaptureEvidenceError("published regression schema is invalid")
    try:
        expected_result = ExpectedCaptureResult(value["expected_result"])
    except (TypeError, ValueError) as error:
        raise CaptureEvidenceError("published expected result is invalid") from error
    cleanup = _mapping(value["expected_cleanup"], "published expected cleanup")
    _exact(
        cleanup,
        {
            "errors",
            "cleanup_error_count",
            "runtime_owner_state",
            "owned_roles",
            "owned_states",
            "unrelated_states",
        },
        "published expected cleanup",
    )
    bundle_hashes = _mapping(
        value["bundle_file_sha256"], "published bundle hashes"
    )
    try:
        cleanup_facts = ExpectedCaptureCleanupFacts(
            errors=tuple(cleanup["errors"]),  # type: ignore[arg-type]
            cleanup_error_count=cleanup["cleanup_error_count"],  # type: ignore[arg-type]
            runtime_owner_state=cleanup["runtime_owner_state"],  # type: ignore[arg-type]
            owned_roles=tuple(cleanup["owned_roles"]),  # type: ignore[arg-type]
            owned_states=tuple(cleanup["owned_states"]),  # type: ignore[arg-type]
            unrelated_states=tuple(cleanup["unrelated_states"]),  # type: ignore[arg-type]
        )
        return CapturePublishedRegressionExpectation(
            attempt=attempt,
            provenance=value["provenance"],  # type: ignore[arg-type]
            expected_result=expected_result,
            bundle_file_sha256=tuple(sorted(bundle_hashes.items())),  # type: ignore[arg-type]
            expected_cleanup_facts=cleanup_facts,
        )
    except (TypeError, ValueError) as error:
        raise CaptureEvidenceError("published regression expectation is invalid") from error


def verify_capture_published_regression(
    destination: Path,
    *,
    expectation: CapturePublishedRegressionExpectation,
) -> dict[str, object]:
    """Verify committed public bytes and semantics without private live inputs.

    This is a post-hoc regression check.  It does not verify historical private
    identity preimages, the original marker set, or process observations.
    """
    if type(expectation) is not CapturePublishedRegressionExpectation:
        raise TypeError(
            "expectation must be CapturePublishedRegressionExpectation"
        )
    destination = Path(destination)
    if destination.name != f"attempt-{expectation.attempt}":
        raise CaptureEvidenceError(
            "published bundle path differs from the expected attempt"
        )
    expected_names = {*_FILES, "manifest.sha256"}
    if not destination.is_dir() or {
        item.name for item in destination.iterdir()
    } != expected_names:
        raise CaptureEvidenceError("evidence file set is not exact")
    for name, expected_digest in expectation.bundle_file_sha256:
        actual_digest = sha256((destination / name).read_bytes()).hexdigest()
        if not hmac.compare_digest(actual_digest, expected_digest):
            raise CaptureEvidenceError(
                f"published {name} differs from the post-hoc regression fixture"
            )
    values = {name: _read_object(destination / name) for name in _FILES}
    _verify_manifest(destination, expected_attempt=expectation.attempt)
    for value in values.values():
        _assert_public(value)
    environment = values["environment.json"]
    observations = values["observations.json"]
    cleanup = values["cleanup.json"]
    _, drift = _validate_environment(
        environment,
        expected_attempt=expectation.attempt,
        allow_drift=expectation.expected_result is ExpectedCaptureResult.FAIL,
    )
    if observations.get("result") != expectation.expected_result.value:
        raise CaptureEvidenceError(
            "result differs from the published regression expectation"
        )
    if expectation.expected_result is ExpectedCaptureResult.PASS:
        _validate_pass_observations(
            observations,
            environment=environment,
            expected_attempt=expectation.attempt,
        )
    else:
        _validate_fail_observations(
            observations, expected_attempt=expectation.attempt
        )
        _validate_fail_drift(observations, drift=drift, environment=environment)
    _validate_cleanup(
        cleanup,
        expected_result=expectation.expected_result,
        expected_attempt=expectation.attempt,
    )
    _bind_expected_cleanup(cleanup, expected=expectation.expected_cleanup_facts)
    recomputed = build_capture_live_summary(
        attempt=expectation.attempt,
        environment=environment,
        observations=observations,
        cleanup=cleanup,
    )
    if not hmac.compare_digest(
        _canonical(values["summary.json"]), _canonical(recomputed)
    ):
        raise CaptureEvidenceError("summary is not independently derived")
    return recomputed


def _validate_environment(
    value: Mapping[str, object],
    *,
    expected_attempt: int,
    expected_preflight: Mapping[str, object] | None = None,
    allow_drift: bool = False,
) -> tuple[int, tuple[str, ...]]:
    expected_attempt = _require_capture_attempt(expected_attempt)
    _exact(
        value,
        {"schema", "attempt", "profile", "maximum_mode", "database", "snapshots"},
        "environment",
    )
    if (
        value["schema"] != "onec-agent-capture-live-environment-v1"
        or value["attempt"] != expected_attempt
        or value["profile"] != "capture"
        or value["maximum_mode"] != "experiment"
    ):
        raise CaptureEvidenceError("environment identity is invalid")
    database = _mapping(value["database"], "database")
    _exact(database, {"target_identity_sha256", "mutation_requested"}, "database")
    _require_hash(database["target_identity_sha256"], "database identity")
    if database["mutation_requested"] is not False:
        raise CaptureEvidenceError("acceptance must not request database mutation")
    snapshots = _mapping(value["snapshots"], "snapshots")
    _exact(snapshots, set(_SNAPSHOT_NAMES), "snapshots")
    preflight: dict[str, object] = {}
    drift: list[str] = []
    for name in _SNAPSHOT_NAMES:
        pair = _mapping(snapshots[name], f"{name} snapshot")
        _exact(pair, {"pre", "post"}, f"{name} snapshot")
        pre = _mapping(pair["pre"], f"{name} pre snapshot")
        post = _mapping(pair["post"], f"{name} post snapshot")
        _validate_snapshot(name, pre)
        _validate_snapshot(name, post)
        if not hmac.compare_digest(_canonical(pre), _canonical(post)):
            if not allow_drift:
                raise CaptureEvidenceError(f"{name} changed during the attempt")
            drift.append(name)
        preflight[name] = dict(pre)
    if expected_preflight is not None:
        _exact(expected_preflight, set(_SNAPSHOT_NAMES), "trusted preflight")
        if not hmac.compare_digest(
            _canonical(preflight), _canonical(expected_preflight)
        ):
            raise CaptureEvidenceError("recorded preflight differs from trusted preflight")
    return expected_attempt, tuple(drift)


def _validate_snapshot(name: str, value: Mapping[str, object]) -> None:
    if name == "platform":
        _exact(
            value,
            {
                "version", "bin_identity_sha256", "service_python_identity_sha256",
                "service_python_sha256", "executables_sha256",
            },
            "platform snapshot",
        )
        if value["version"] != "8.3.27.2170":
            raise CaptureEvidenceError("platform version is invalid")
        _require_hash(value["bin_identity_sha256"], "platform bin")
        _require_hash(
            value["service_python_identity_sha256"], "service Python identity"
        )
        _require_hash(value["service_python_sha256"], "service Python executable")
        executables = _mapping(value["executables_sha256"], "platform executables")
        _exact(
            executables,
            {"1cv8.exe", "1cv8c.exe", "dbgs.exe"},
            "platform executables",
        )
        for executable, digest in executables.items():
            _require_hash(digest, executable)
        return
    if name == "source":
        _exact(
            value,
            {"tree_sha256", "capture_module_sha256"},
            "source snapshot",
        )
    elif name == "notebook":
        _exact(
            value,
            {"artifact_sha256", "main_source_sha256", "hypothesis_source_sha256"},
            "notebook snapshot",
        )
    elif name == "implementation":
        _exact(
            value,
            {
                "runtime_tree_sha256",
                "harness_sha256",
                "capture_evidence_support_sha256",
                "process_evidence_support_sha256",
                "verifier_sha256",
                "evaluator_sha256",
            },
            "implementation snapshot",
        )
    else:
        raise CaptureEvidenceError("unknown snapshot category")
    for field, digest in value.items():
        _require_hash(digest, f"{name} {field}")


def _validate_pass_observations(
    value: Mapping[str, object],
    *,
    environment: Mapping[str, object],
    expected_attempt: int,
) -> None:
    expected_attempt = _require_capture_attempt(expected_attempt)
    _exact(
        value,
        {
            "schema", "attempt", "result", "call_sequence", "call_metrics", "code", "frontend",
            "capture_a", "locals", "manager", "table_a", "hypothesis",
            "continuation", "capture_b", "downstream", "terminal",
        },
        "PASS observations",
    )
    if (
        value["schema"] != "onec-agent-capture-live-observations-v1"
        or value["attempt"] != expected_attempt
        or value["result"] != "PASS"
        or tuple(value["call_sequence"]) != CAPTURE_PASS_CALL_SEQUENCE  # type: ignore[arg-type]
    ):
        raise CaptureEvidenceError("PASS observations identity or call sequence is invalid")
    call_metrics = _mapping(value["call_metrics"], "call metrics")
    _exact(
        call_metrics,
        {"automatic_hash_calls", "full_materialization_calls"},
        "call metrics",
    )
    if call_metrics != {
        "automatic_hash_calls": 0,
        "full_materialization_calls": 0,
    }:
        raise CaptureEvidenceError("actual call log contains a forbidden full scan")
    notebook = _mapping(
        _mapping(
            _mapping(environment["snapshots"], "snapshots")["notebook"],
            "notebook snapshots",
        )["pre"],
        "notebook preflight",
    )
    source = _mapping(
        _mapping(
            _mapping(environment["snapshots"], "snapshots")["source"],
            "source snapshots",
        )["pre"],
        "source preflight",
    )
    code = _mapping(value["code"], "code")
    _exact(
        code,
        {
            "main_cell_id", "main_revision", "main_source_sha256",
            "hypothesis_cell_id", "hypothesis_revision", "hypothesis_source_sha256",
        },
        "code",
    )
    if (
        code["main_cell_id"] != "zup_capture_main"
        or code["hypothesis_cell_id"] != "zup_capture_hypothesis"
        or type(code["main_revision"]) is not int
        or code["main_revision"] <= 0
        or type(code["hypothesis_revision"]) is not int
        or code["hypothesis_revision"] <= 0
        or code["main_source_sha256"] != notebook["main_source_sha256"]
        or code["hypothesis_source_sha256"] != notebook["hypothesis_source_sha256"]
    ):
        raise CaptureEvidenceError("saved code is not bound to trusted notebook sources")
    frontend = _mapping(value["frontend"], "frontend")
    _exact(
        frontend,
        {
            "sessions",
            "replacement_count",
            "frontend_a_identity_sha256",
            "frontend_b_identity_sha256",
            "frontend_a_state_before_b",
            "replayed_operation_identity_sha256",
        },
        "frontend",
    )
    for field in (
        "frontend_a_identity_sha256",
        "frontend_b_identity_sha256",
        "replayed_operation_identity_sha256",
    ):
        _require_hash(frontend[field], field)
    if (
        frontend["sessions"] != ["A", "B"]
        or frontend["replacement_count"] != 1
        or frontend["frontend_a_state_before_b"] != "absent"
        or frontend["frontend_a_identity_sha256"]
        == frontend["frontend_b_identity_sha256"]
    ):
        raise CaptureEvidenceError("frontend replacement evidence is invalid")

    capture_a = _validate_capture(value["capture_a"], name="capture_a")
    capture_b = _validate_capture(value["capture_b"], name="capture_b")
    fence_a = _mapping(capture_a["fence"], "capture_a fence")
    fence_b = _mapping(capture_b["fence"], "capture_b fence")
    if (
        fence_a["capture_generation"] != 1
        or fence_b["capture_generation"] != 2
        or fence_b["stop_sequence"] <= fence_a["stop_sequence"]
        or fence_a["source_revision"] != 1
        or fence_b["source_revision"] != 1
        or fence_a["source_sha256"] != source["capture_module_sha256"]
        or fence_b["source_sha256"] != source["capture_module_sha256"]
        or fence_a["capture_intent_identity_sha256"] == fence_b["capture_intent_identity_sha256"]
        or frontend["replayed_operation_identity_sha256"]
        != fence_a["operation_identity_sha256"]
    ):
        raise CaptureEvidenceError("capture correlation fence progression is invalid")

    locals_view = _mapping(value["locals"], "locals")
    _exact(
        locals_view,
        {"selected_count", "proxy_count", "selected_name_sha256s", "budget"},
        "locals",
    )
    selected_local_names = _hash_sequence(
        locals_view["selected_name_sha256s"], "selected local names"
    )
    if (
        type(locals_view["selected_count"]) is not int
        or not 0 < locals_view["selected_count"] <= 20
        or locals_view["proxy_count"] != locals_view["selected_count"]
        or len(selected_local_names) != locals_view["selected_count"]
    ):
        raise CaptureEvidenceError("bounded local inspection is invalid")
    _validate_budget(
        locals_view["budget"],
        profile="agent_metadata",
        max_depth=1,
        max_items=20,
        max_rows=20,
        max_bytes=16384,
        timeout_ms=1000,
        cost_class="metadata",
    )
    manager = _mapping(value["manager"], "manager")
    _exact(
        manager,
        {
            "origin", "alias", "origin_local_name_sha256", "table_name_sha256",
            "manager_identity_sha256", "table_count",
        },
        "manager",
    )
    if (
        manager["origin"] != "frame_local"
        or manager["alias"] != "manager_a"
        or type(manager["table_count"]) is not int
        or not 0 < manager["table_count"] <= 100
    ):
        raise CaptureEvidenceError("manager origin or bounded metadata is invalid")
    _require_hash(manager["manager_identity_sha256"], "manager identity")
    _require_hash(manager["origin_local_name_sha256"], "manager origin local")
    _require_hash(manager["table_name_sha256"], "manager table name")
    _validate_table_a(value["table_a"], manager=manager)

    hypothesis = _mapping(value["hypothesis"], "hypothesis")
    _exact(
        hypothesis,
        {
            "operation_identity_sha256", "operation_state", "observation_state",
            "failure_stage", "capture_state_before", "capture_state_after",
            "capture_generation_before", "capture_generation_after", "dirty_root_count",
        },
        "hypothesis",
    )
    _require_hash(hypothesis["operation_identity_sha256"], "hypothesis operation")
    if (
        hypothesis["operation_state"] != "captured"
        or hypothesis["observation_state"] != "partial"
        or hypothesis["failure_stage"] != "observation"
        or hypothesis["capture_state_before"] != "captured"
        or hypothesis["capture_state_after"] != "captured"
        or hypothesis["capture_generation_before"] != 1
        or hypothesis["capture_generation_after"] != 1
        or type(hypothesis["dirty_root_count"]) is not int
        or not 0 < hypothesis["dirty_root_count"] <= 100
    ):
        raise CaptureEvidenceError("paused hypothesis success/partial semantics are invalid")

    continuation = _mapping(value["continuation"], "continuation")
    _exact(
        continuation,
        {
            "operation_identity_sha256", "call_count", "continue_state",
            "dirty_root_name_sha256s", "acknowledged_root_name_sha256s",
            "old_proxy_check_count", "old_proxy_stale_count",
            "old_proxy_identity_sha256s", "stale_proxy_identity_sha256s",
            "old_proxy_capture_generation",
        },
        "continuation",
    )
    _require_hash(continuation["operation_identity_sha256"], "continuation operation")
    roots = _hash_sequence(continuation["dirty_root_name_sha256s"], "dirty roots")
    acknowledged = _hash_sequence(
        continuation["acknowledged_root_name_sha256s"], "acknowledged roots"
    )
    old_proxies = _hash_sequence(
        continuation["old_proxy_identity_sha256s"], "old proxy identities"
    )
    stale_proxies = _hash_sequence(
        continuation["stale_proxy_identity_sha256s"], "stale proxy identities"
    )
    if (
        continuation["call_count"] != 1
        or continuation["continue_state"] != "acknowledged"
        or roots != acknowledged
        or len(roots) != hypothesis["dirty_root_count"]
        or continuation["old_proxy_check_count"] != continuation["old_proxy_stale_count"]
        or type(continuation["old_proxy_check_count"]) is not int
        or continuation["old_proxy_check_count"] <= 0
        or len(old_proxies) != continuation["old_proxy_check_count"]
        or old_proxies != stale_proxies
        or continuation["old_proxy_capture_generation"] != 1
        or fence_b["operation_identity_sha256"] != continuation["operation_identity_sha256"]
    ):
        raise CaptureEvidenceError("one-use continuation or stale proxy evidence is invalid")

    _validate_downstream(value["downstream"])
    terminal = _mapping(value["terminal"], "terminal")
    _exact(
        terminal,
        {
            "origin_main_operation_identity_sha256",
            "continuation_operation_identity_sha256",
            "terminal_operation_identity_sha256", "origin_capture_generation",
            "finish_continue_call_count", "state", "capture_present", "next_event_cursor",
        },
        "terminal MAIN",
    )
    _require_hash(terminal["origin_main_operation_identity_sha256"], "origin MAIN")
    _require_hash(terminal["continuation_operation_identity_sha256"], "continuation MAIN")
    _require_hash(terminal["terminal_operation_identity_sha256"], "terminal MAIN")
    if (
        terminal["origin_main_operation_identity_sha256"] != fence_a["operation_identity_sha256"]
        or terminal["continuation_operation_identity_sha256"]
        != fence_b["operation_identity_sha256"]
        or terminal["terminal_operation_identity_sha256"]
        != terminal["continuation_operation_identity_sha256"]
        or terminal["origin_capture_generation"] != 2
        or terminal["finish_continue_call_count"] != 1
        or terminal["state"] != "completed"
        or terminal["capture_present"] is not False
        or type(terminal["next_event_cursor"]) is not int
        or terminal["next_event_cursor"] <= 0
    ):
        raise CaptureEvidenceError("terminal MAIN identity or state is invalid")


def _validate_capture(value: object, *, name: str) -> Mapping[str, object]:
    capture = _mapping(value, name)
    _exact(capture, {"fence", "location", "state"}, name)
    if capture["state"] != "captured":
        raise CaptureEvidenceError(f"{name} is not paused")
    fence = _mapping(capture["fence"], f"{name} fence")
    _exact(
        fence,
        {
            "capture_intent_identity_sha256", "operation_identity_sha256",
            "source_revision", "source_sha256", "capture_generation", "stop_sequence",
        },
        f"{name} fence",
    )
    for field in ("capture_intent_identity_sha256", "operation_identity_sha256", "source_sha256"):
        _require_hash(fence[field], f"{name} {field}")
    for field in ("source_revision", "capture_generation", "stop_sequence"):
        if type(fence[field]) is not int or fence[field] <= 0:
            raise CaptureEvidenceError(f"{name} {field} is invalid")
    location = _mapping(capture["location"], f"{name} location")
    _exact(
        location,
        {
            "name", "project", "module", "procedure", "line", "executable_line",
            "source_revision", "source_sha256", "module_type_identity_sha256",
            "extension_identity_sha256", "object_identity_sha256",
            "property_identity_sha256",
        },
        f"{name} location",
    )
    if (
        location["name"] != name
        or any(
            not isinstance(location[field], str)
            or not location[field].isidentifier()
            for field in ("name", "project", "module", "procedure")
        )
        or type(location["line"]) is not int
        or location["line"] <= 0
        or type(location["executable_line"]) is not int
        or location["executable_line"] <= 0
        or location["source_revision"] != fence["source_revision"]
        or location["source_sha256"] != fence["source_sha256"]
    ):
        raise CaptureEvidenceError(f"{name} location is not bound to its fence")
    for field in (
        "module_type_identity_sha256",
        "extension_identity_sha256",
        "object_identity_sha256",
        "property_identity_sha256",
    ):
        _require_hash(location[field], f"{name} {field}")
    return capture


def _validate_table_a(value: object, *, manager: Mapping[str, object]) -> None:
    table = _mapping(value, "table_a")
    _exact(
        table,
        {
            "manager_identity_sha256", "proxy_identity_sha256", "known_size",
            "schema_column_count", "automatic_hash_calls", "full_materialization_calls",
            "alias", "table_name_sha256", "head", "budget",
        },
        "table_a",
    )
    if (
        table["manager_identity_sha256"] != manager["manager_identity_sha256"]
        or table["alias"] != "table_a"
        or table["table_name_sha256"] != manager["table_name_sha256"]
    ):
        raise CaptureEvidenceError("table manager identity changed")
    _require_hash(table["proxy_identity_sha256"], "table proxy")
    _require_hash(table["table_name_sha256"], "table name")
    if (
        type(table["known_size"]) is not int
        or table["known_size"] < 0
        or type(table["schema_column_count"]) is not int
        or not 0 < table["schema_column_count"] <= 1000
        or table["automatic_hash_calls"] != 0
        or table["full_materialization_calls"] != 0
    ):
        raise CaptureEvidenceError("table metadata or no-full-scan gate is invalid")
    head = _mapping(table["head"], "table head")
    _exact(
        head,
        {"requested_rows", "returned_rows", "returned_columns", "transfer_bytes", "dataframe_identity_sha256"},
        "table head",
    )
    _require_hash(head["dataframe_identity_sha256"], "head dataframe")
    if (
        head["requested_rows"] != 5
        or type(head["returned_rows"]) is not int
        or not 0 <= head["returned_rows"] <= 5
        or type(head["returned_columns"]) is not int
        or not 0 < head["returned_columns"] <= table["schema_column_count"]
        or type(head["transfer_bytes"]) is not int
        or not 0 <= head["transfer_bytes"] <= 67108864
        or head["returned_rows"] > table["known_size"]
    ):
        raise CaptureEvidenceError("bounded table head is invalid")
    _validate_budget(
        table["budget"],
        profile="agent_dataframe",
        max_depth=8,
        max_items=200000,
        max_rows=10000,
        max_bytes=67108864,
        timeout_ms=30000,
        cost_class="full_scan",
    )


def _validate_downstream(value: object) -> None:
    downstream = _mapping(value, "downstream")
    _exact(
        downstream,
        {
            "table_proxy_identity_sha256", "result_proxy_identity_sha256",
            "manager_alias", "table_alias", "result_local_name_sha256",
            "table_name_sha256", "requested_rows", "selected_rows",
            "selected_columns", "automatic_hash_calls", "full_materialization_calls", "budget",
        },
        "downstream",
    )
    _require_hash(downstream["table_proxy_identity_sha256"], "downstream table")
    _require_hash(downstream["result_proxy_identity_sha256"], "downstream result")
    _require_hash(downstream["result_local_name_sha256"], "downstream result local")
    _require_hash(downstream["table_name_sha256"], "downstream table name")
    if (
        downstream["manager_alias"] != "manager_b"
        or downstream["table_alias"] != "table_b"
        or downstream["requested_rows"] != 5
        or type(downstream["selected_rows"]) is not int
        or not 0 <= downstream["selected_rows"] <= 5
        or type(downstream["selected_columns"]) is not int
        or not 0 < downstream["selected_columns"] <= 100
        or downstream["automatic_hash_calls"] != 0
        or downstream["full_materialization_calls"] != 0
    ):
        raise CaptureEvidenceError("bounded downstream inspection is invalid")
    _validate_budget(
        downstream["budget"],
        profile="agent_dataframe",
        max_depth=8,
        max_items=200000,
        max_rows=10000,
        max_bytes=67108864,
        timeout_ms=30000,
        cost_class="full_scan",
    )


def _validate_budget(
    value: object,
    *,
    profile: str,
    max_depth: int,
    max_items: int,
    max_rows: int,
    max_bytes: int,
    timeout_ms: int,
    cost_class: str,
) -> None:
    budget = _mapping(value, "budget")
    _exact(
        budget,
        {
            "profile", "max_depth", "max_items", "max_rows", "max_bytes",
            "timeout_ms", "cost_class",
        },
        "budget",
    )
    if budget != {
        "profile": profile,
        "max_depth": max_depth,
        "max_items": max_items,
        "max_rows": max_rows,
        "max_bytes": max_bytes,
        "timeout_ms": timeout_ms,
        "cost_class": cost_class,
    }:
        raise CaptureEvidenceError(
            f"observation budget is not the exact {profile} server profile"
        )


def _validate_fail_drift(
    value: Mapping[str, object],
    *,
    drift: Sequence[str],
    environment: Mapping[str, object],
) -> None:
    failure = _mapping(value["failure"], "failure")
    boundary = failure["boundary"]
    if drift:
        drift_boundary = "snapshot_drift_" + "_".join(drift)
        unavailable_boundaries = (
            {
                "snapshot_missing_" + drift[0],
                "snapshot_unreadable_" + drift[0],
            }
            if len(drift) == 1
            else set()
        )
        valid_drift = (
            boundary == drift_boundary and failure["type"] == "SnapshotDrift"
        )
        valid_unavailable = (
            boundary in unavailable_boundaries
            and failure["type"] == "SnapshotInputFailure"
        )
        if not (valid_drift or valid_unavailable):
            raise CaptureEvidenceError("snapshot drift FAIL facts are not exact")
        if valid_unavailable:
            assert isinstance(boundary, str)
            state = (
                "missing"
                if boundary.startswith("snapshot_missing_")
                else "unreadable"
            )
            stage = drift[0]
            snapshots = _mapping(environment["snapshots"], "snapshots")
            pair = _mapping(snapshots[stage], f"{stage} snapshot")
            pre = _mapping(pair["pre"], f"{stage} pre snapshot")
            post = _mapping(pair["post"], f"{stage} post snapshot")
            field = _SNAPSHOT_SENTINEL_FIELDS[stage]
            expected_post = dict(pre)
            expected_post[field] = snapshot_unavailable_sha256(
                stage=stage,
                state=state,
                prior=str(pre[field]),
            )
            if not hmac.compare_digest(_canonical(post), _canonical(expected_post)):
                raise CaptureEvidenceError(
                    "unavailable snapshot postflight sentinel is not exact"
                )
    elif isinstance(boundary, str) and boundary.startswith(
        ("snapshot_drift_", "snapshot_missing_", "snapshot_unreadable_")
    ):
        raise CaptureEvidenceError("snapshot failure was claimed without observed drift")


def _validate_fail_observations(
    value: Mapping[str, object], *, expected_attempt: int
) -> None:
    expected_attempt = _require_capture_attempt(expected_attempt)
    _exact(
        value,
        {"schema", "attempt", "result", "call_sequence", "failure"},
        "FAIL observations",
    )
    calls = value["call_sequence"]
    if (
        value["schema"] != "onec-agent-capture-live-observations-v1"
        or value["attempt"] != expected_attempt
        or value["result"] != "FAIL"
        or not isinstance(calls, list)
        or calls != list(CAPTURE_PASS_CALL_SEQUENCE[: len(calls)])
    ):
        raise CaptureEvidenceError("FAIL observations or completed call prefix is invalid")
    failure = _mapping(value["failure"], "failure")
    _exact(failure, {"boundary", "type", "last_completed_phase"}, "failure")
    for field in ("boundary", "type", "last_completed_phase"):
        item = failure[field]
        if not isinstance(item, str) or not item.isidentifier() or len(item) > 128:
            raise CaptureEvidenceError("FAIL boundary evidence is not sanitized")


def _validate_cleanup(
    value: Mapping[str, object],
    *,
    expected_result: ExpectedCaptureResult,
    expected_attempt: int,
) -> None:
    expected_attempt = _require_capture_attempt(expected_attempt)
    _exact(
        value,
        {"schema", "attempt", "runtime_owner_state", "owned", "unrelated", "errors"},
        "cleanup",
    )
    if (
        value["schema"] != "onec-agent-capture-live-cleanup-v1"
        or value["attempt"] != expected_attempt
    ):
        raise CaptureEvidenceError("cleanup identity is invalid")
    owned = value["owned"]
    unrelated = value["unrelated"]
    errors = value["errors"]
    if value["runtime_owner_state"] != "absent":
        raise CaptureEvidenceError("runtime owner must be absent after cleanup")
    if not isinstance(owned, list) or len(owned) > 20:
        raise CaptureEvidenceError("owned cleanup list is invalid")
    if not isinstance(unrelated, list) or len(unrelated) > 100:
        raise CaptureEvidenceError("unrelated preservation list is invalid")
    owned_keys: set[tuple[str, str]] = set()
    roles: list[str] = []
    for item in owned:
        mapped = _mapping(item, "owned cleanup identity")
        _exact(mapped, {"role", "identity_sha256", "state"}, "owned cleanup identity")
        role = mapped["role"]
        if not isinstance(role, str) or not role.isidentifier():
            raise CaptureEvidenceError("owned role is invalid")
        _require_hash(mapped["identity_sha256"], "owned process identity")
        if mapped["state"] not in _TRI_STATES:
            raise CaptureEvidenceError("owned cleanup state is not tri-state")
        key = (role, str(mapped["identity_sha256"]))
        if key in owned_keys:
            raise CaptureEvidenceError("owned cleanup identity is duplicated")
        owned_keys.add(key)
        roles.append(role)
    unrelated_hashes: set[str] = set()
    for item in unrelated:
        mapped = _mapping(item, "unrelated process identity")
        _exact(mapped, {"identity_sha256", "state"}, "unrelated process identity")
        digest = str(mapped["identity_sha256"])
        _require_hash(digest, "unrelated process identity")
        if mapped["state"] not in _TRI_STATES:
            raise CaptureEvidenceError("unrelated process state is not tri-state")
        if digest in unrelated_hashes:
            raise CaptureEvidenceError("unrelated process identity is duplicated")
        unrelated_hashes.add(digest)
    if (
        not isinstance(errors, list)
        or len(errors) > 100
        or any(
            not isinstance(error, str)
            or not error.isidentifier()
            or len(error) > 128
            for error in errors
        )
    ):
        raise CaptureEvidenceError("cleanup errors are not bounded sanitized types")
    if any(item["state"] != "alive" for item in unrelated):
        raise CaptureEvidenceError("unrelated process preservation failed")
    if any(item["state"] != "absent" for item in owned):
        raise CaptureEvidenceError("cleanup requires zero owned processes")
    if expected_result is ExpectedCaptureResult.PASS:
        if tuple(roles) != CAPTURE_OWNED_ROLES:
            raise CaptureEvidenceError("PASS owned process roles are not exact")
        if errors:
            raise CaptureEvidenceError("PASS requires zero owned processes and no cleanup errors")


def _bind_expected_processes(
    cleanup: Mapping[str, object],
    *,
    attempt: int,
    expected_owned_processes: Sequence[Mapping[str, object]],
    expected_unrelated_processes: Sequence[Mapping[str, object]],
) -> None:
    attempt = _require_capture_attempt(attempt)
    expected_owned = [
        {
            "role": item.get("role"),
            "identity_sha256": process_identity_sha256(attempt, item),
        }
        for item in expected_owned_processes
    ]
    actual_owned = [
        {"role": item["role"], "identity_sha256": item["identity_sha256"]}
        for item in cleanup["owned"]  # type: ignore[index]
    ]
    if not hmac.compare_digest(_canonical(actual_owned), _canonical(expected_owned)):
        raise CaptureEvidenceError("public cleanup differs from exact owned process identities")
    expected_unrelated = [
        process_identity_sha256(attempt, item) for item in expected_unrelated_processes
    ]
    actual_unrelated = [
        item["identity_sha256"] for item in cleanup["unrelated"]  # type: ignore[index]
    ]
    if not hmac.compare_digest(
        _canonical(actual_unrelated), _canonical(expected_unrelated)
    ):
        raise CaptureEvidenceError("public cleanup differs from unrelated process identities")


def _bind_expected_cleanup(
    cleanup: Mapping[str, object], *, expected: ExpectedCaptureCleanupFacts
) -> None:
    actual = ExpectedCaptureCleanupFacts(
        errors=tuple(cleanup["errors"]),  # type: ignore[arg-type]
        cleanup_error_count=len(cleanup["errors"]),  # type: ignore[arg-type]
        runtime_owner_state=str(cleanup["runtime_owner_state"]),
        owned_roles=tuple(item["role"] for item in cleanup["owned"]),  # type: ignore[index]
        owned_states=tuple(item["state"] for item in cleanup["owned"]),  # type: ignore[index]
        unrelated_states=tuple(
            item["state"] for item in cleanup["unrelated"]  # type: ignore[index]
        ),
    )
    if actual != expected:
        raise CaptureEvidenceError(
            "cleanup differs from external expected cleanup facts"
        )


def _hash_sequence(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or len(value) > 100:
        raise CaptureEvidenceError(f"{name} are invalid")
    for digest in value:
        _require_hash(digest, name)
    normalized = tuple(str(item) for item in value)
    if len(set(normalized)) != len(normalized):
        raise CaptureEvidenceError(f"{name} are duplicated")
    return normalized


def _assert_public(value: object, *, trail: tuple[str, ...] = ()) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise CaptureEvidenceError("private evidence contains a non-text key")
            folded = key.casefold()
            if folded in _PRIVATE_KEYS or folded.endswith("_path"):
                raise CaptureEvidenceError(
                    f"private evidence key is forbidden: {'.'.join((*trail, key))}"
                )
            _assert_public(nested, trail=(*trail, key))
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, nested in enumerate(value):
            _assert_public(nested, trail=(*trail, str(index)))
        return
    if isinstance(value, str) and (
        _UUID.search(value) or _PRIVATE_TEXT.search(value) or _PATH_TEXT.search(value)
    ):
        raise CaptureEvidenceError(
            f"private evidence text is forbidden: {'.'.join(trail)}"
        )


def _validated_private_inputs(
    *, private_markers: Sequence[str], private_pids: Sequence[int]
) -> tuple[tuple[str, ...], frozenset[int]]:
    if len(private_markers) > 10_000 or any(
        not isinstance(marker, str) or not marker or len(marker) > 1_000_000
        for marker in private_markers
    ):
        raise TypeError("private markers must be bounded non-empty strings")
    if any(type(pid) is not int or pid <= 0 for pid in private_pids):
        raise TypeError("private PIDs must be positive integers")
    return (
        tuple(marker.casefold() for marker in private_markers),
        frozenset(private_pids),
    )


def _assert_external_private_absent(
    value: object,
    *,
    private_markers: Sequence[str],
    private_pids: Sequence[int],
    trail: tuple[str, ...] = (),
    allowed_exact_values: Mapping[tuple[str, ...], str] | None = None,
) -> None:
    markers, pids = _validated_private_inputs(
        private_markers=private_markers, private_pids=private_pids
    )

    allowed = dict(allowed_exact_values or {})
    observed_allowed: set[tuple[str, ...]] = set()

    def visit(
        item: object,
        current: tuple[str, ...],
        *,
        parent_is_sequence: bool = False,
    ) -> None:
        if isinstance(item, Mapping):
            for key, nested in item.items():
                if not isinstance(key, str):
                    raise CaptureEvidenceError("private evidence contains a non-text key")
                key_folded = key.casefold()
                if any(marker in key_folded for marker in markers):
                    raise CaptureEvidenceError(
                        f"private marker occurs in evidence key: {'.'.join((*current, key))}"
                    )
                visit(nested, (*current, key))
            return
        if isinstance(item, Sequence) and not isinstance(
            item, (str, bytes, bytearray)
        ):
            for index, nested in enumerate(item):
                visit(
                    nested,
                    (*current, str(index)),
                    parent_is_sequence=True,
                )
            return
        if type(item) is int and item in pids:
            raise CaptureEvidenceError(
                f"private PID occurs in evidence: {'.'.join(current)}"
            )
        if isinstance(item, str):
            expected = allowed.get(current)
            if expected is not None and item == expected:
                observed_allowed.add(current)
                return
            folded = item.casefold()
            marker_occurs = any(marker in folded for marker in markers)
            pid_occurs = any(
                re.search(rf"(?<!\d){pid}(?!\d)", item) for pid in pids
            )
            map_children = (
                _PUBLIC_EVIDENCE_SHA256_MAP_CHILDREN.get(current[-2])
                if len(current) >= 2 and not parent_is_sequence
                else None
            )
            is_public_evidence_hash = (
                bool(current)
                and _HASH.fullmatch(item) is not None
                and (
                    (
                        not parent_is_sequence
                        and current[-1] in _PUBLIC_EVIDENCE_SHA256_LEAF_FIELDS
                    )
                    or (
                        len(current) >= 2
                        and parent_is_sequence
                        and current[-2]
                        in _PUBLIC_EVIDENCE_SHA256_SEQUENCE_FIELDS
                    )
                    or (
                        map_children is not None
                        and current[-1] in map_children
                    )
                )
            )
            if marker_occurs or (pid_occurs and not is_public_evidence_hash):
                raise CaptureEvidenceError(
                    f"private marker occurs in evidence: {'.'.join(current)}"
                )

    visit(value, trail)
    if observed_allowed != set(allowed):
        raise CaptureEvidenceError("expected private contract field is absent or changed")


def assert_mcp_response_private_safe(
    value: object,
    *,
    method: str | None = None,
    expected_private_fields: Mapping[str, str] | None = None,
    private_markers: Sequence[str],
    owned_pids: Sequence[int],
    external_pids: Sequence[int] = (),
) -> None:
    """Reject private material before deriving facts from an MCP response.

    Two responses necessarily carry a private value used by the acceptance
    harness.  Only the exact pre-bound value at the exact response field is
    exempted; the same value anywhere else remains a privacy violation.
    """
    if method is not None and (not isinstance(method, str) or not method):
        raise TypeError("MCP method must be a non-empty string")
    expected = dict(expected_private_fields or {})
    permitted_fields = {
        "workspace.open": frozenset({"project_root"}),
        "code.get": frozenset({"source"}),
    }.get(method, frozenset())
    if (
        any(
            not isinstance(key, str)
            or key not in permitted_fields
            or not isinstance(item, str)
            or not item
            for key, item in expected.items()
        )
        or set(expected) - permitted_fields
    ):
        raise CaptureEvidenceError("private MCP contract field is not permitted")
    private_pids = (*owned_pids, *external_pids)
    _assert_external_private_absent(
        value,
        private_markers=private_markers,
        private_pids=private_pids,
        allowed_exact_values={
            ("value", field): item for field, item in expected.items()
        },
    )


def _manifest_attempt_prefix(attempt: int) -> str:
    attempt = _require_capture_attempt(attempt)
    # Attempt 1 predates explicit manifest-domain binding; its empty prefix is
    # part of the published immutable byte contract. Attempt 2 is new and can
    # carry the stronger binding without rewriting history.
    return "" if attempt == 1 else "# onec-agent-capture-live-attempt=2\n"


def _verify_manifest(destination: Path, *, expected_attempt: int) -> None:
    expected_attempt = _require_capture_attempt(expected_attempt)
    try:
        text = (destination / "manifest.sha256").read_text(encoding="ascii")
    except (OSError, UnicodeError) as error:
        raise CaptureEvidenceError("manifest is unreadable") from error
    expected_lines = [
        f"{sha256((destination / name).read_bytes()).hexdigest()}  {name}"
        for name in _FILES
    ]
    expected_text = _manifest_attempt_prefix(expected_attempt) + "\n".join(
        expected_lines
    ) + "\n"
    if text != expected_text:
        raise CaptureEvidenceError(
            "manifest does not bind the exact attempt or file hashes"
        )
    lines = text.removeprefix(_manifest_attempt_prefix(expected_attempt)).splitlines()
    if lines != expected_lines or len(
        {line.rsplit("  ", 1)[-1] for line in lines}
    ) != len(_FILES):
        raise CaptureEvidenceError("manifest is not exact or contains duplicate entries")


def _read_object(path: Path) -> dict[str, object]:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise CaptureEvidenceError(f"duplicate JSON key in {path.name}")
            result[key] = value
        return result

    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
    except CaptureEvidenceError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CaptureEvidenceError(f"{path.name} is not valid JSON") from error
    if not isinstance(value, dict):
        raise CaptureEvidenceError(f"{path.name} must contain an object")
    if raw != _canonical(value):
        raise CaptureEvidenceError(f"{path.name} is not canonical JSON")
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
        raise CaptureEvidenceError("evidence is not canonical JSON data") from error


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise CaptureEvidenceError(f"{name} must be an object")
    return value


def _exact(value: Mapping[str, object], keys: set[str], name: str) -> None:
    if set(value) != keys:
        raise CaptureEvidenceError(f"{name} key set is not exact")


def _require_hash(value: object, name: str) -> None:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise CaptureEvidenceError(f"{name} must be a lowercase SHA-256")


__all__ = [
    "CAPTURE_OWNED_ROLES",
    "CAPTURE_PASS_CALL_SEQUENCE",
    "CaptureAttemptPaths",
    "CaptureEvidenceError",
    "CapturePublishedRegressionExpectation",
    "ExpectedCaptureCleanupFacts",
    "ExpectedCaptureResult",
    "assert_capture_live_evidence_private_safe",
    "assert_mcp_response_private_safe",
    "build_capture_live_summary",
    "capture_attempt_paths",
    "capture_identity_salt",
    "load_capture_published_regression_expectation",
    "process_identity_sha256",
    "snapshot_unavailable_sha256",
    "verify_capture_live_evidence",
    "verify_capture_published_regression",
    "write_capture_live_evidence",
]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m integration.evidence.capture_live_evidence",
        description=(
            "Verify a published CAPTURE bundle against its post-hoc public "
            "regression fixture. This does not verify historical private inputs."
        ),
    )
    parser.add_argument("--published-bundle", type=Path, required=True)
    parser.add_argument("--expectation", type=Path, required=True)
    args = parser.parse_args(argv)
    expectation = load_capture_published_regression_expectation(args.expectation)
    verified = verify_capture_published_regression(
        args.published_bundle,
        expectation=expectation,
    )
    print(
        json.dumps(
            {
                "cleanup_error_count": verified.get("cleanup_error_count"),
                "historical_private_inputs_verified": False,
                "scope": "posthoc_public_bundle_regression",
                "status": verified["status"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
