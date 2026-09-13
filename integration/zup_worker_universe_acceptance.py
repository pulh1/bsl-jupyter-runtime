"""Bounded real-ZUP acceptance and compact performance evidence.

Private configuration sources are read from an already approved source export and
are never copied into the run directory.  Public evidence contains identities,
counts, timings and outcomes only.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, replace
from hashlib import sha256
import importlib.resources
import json
from math import ceil, isfinite
import os
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import NamedTemporaryFile

import psutil

from onec_runtime.artifacts import ArtifactWriter
from onec_runtime.bsl import (
    CommonModuleCatalogSnapshot,
    ParsedModuleModel,
    SessionCommonModuleCatalog,
    SourceUnitKind,
    SourceUnitRef,
    WorkerModuleUnit,
    mapped_visible_source,
    resolve_worker_dependencies,
    source_sha256,
    worker_model_candidate_names,
)
from onec_runtime.bsl.full_ast_worker_projection import parse_full_ast_module
from onec_runtime.bsl.module_universe import (
    AnalyzedWorkerModule,
    analyze_resolved_worker_module,
)
from onec_runtime.bsl.parser_artifact_identity import verify_parser_artifact_manifest
from onec_runtime.config import RuntimeConfig
from onec_runtime.errors import (
    BslExecutionError,
    ProtocolError,
    StaleWorkerGeneration,
    WorkerPromotionOutcomeUnknown,
)
from onec_runtime.extension_bundle import (
    fingerprint_extension_dump,
    packaged_extension_bundle,
    read_extension_manifest,
)
from onec_runtime.experiment import bsl_string_literal
from onec_runtime.kernel import SYNTHETIC_CAPTURE_A_MARKER
from onec_runtime.performance_profile import PARSER_CALL_COUNTERS, PhaseRecorder
from onec_runtime.prototype_runtime import CaptureCellResult
from onec_runtime.runtime_api import RuntimeReply, RuntimeReplyKind
from onec_runtime.session import RuntimeSession, RuntimeSessionConfig
from onec_runtime.toolchain import dump_target_extension_files
from onec_runtime.worker_universe import (
    WorkerGenerationHandle,
    _worker_promotion_failure_phase,
)

from integration.support.zup_sources import (
    ZupSourceBundle,
    admit_zup_source_bundle,
)


_CaptureContextFactory = Callable[[RuntimeSession], AbstractContextManager[None]]
_SESSION_CATALOG_PROFILE = "runtime-session-server-v1"
_DIRECT_BACKEND_ID = "python-semantic-direct-v1"
_RUNTIME_REVISION_PATHS = (
    "src/onec_runtime",
    "grammar/bsl-server-strict.grammar",
    "tools/generate_bsl_semantic_parser.py",
)


def require_clean_runtime_tree(workspace_root: Path) -> None:
    """Reject tracked or untracked runtime/build-input drift without leaking paths."""
    try:
        dirty = subprocess.run(
            [
                "git",
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--",
                *_RUNTIME_REVISION_PATHS,
            ],
            cwd=Path(workspace_root),
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        raise ProtocolError("runtime git identity is unavailable") from error
    if dirty:
        raise ProtocolError("runtime workspace is not clean")


def active_parser_acceptance_checkpoint() -> dict[str, object]:
    """Bind live acceptance to the sole active full-AST parser artifact."""
    from onec_runtime.bsl import generated_semantic_parser as full_generated

    workspace_root = Path(__file__).parents[1].resolve()
    require_clean_runtime_tree(workspace_root)
    full_relative = Path("src/onec_runtime/bsl/generated_semantic_parser.py")
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=workspace_root,
            check=True,
            capture_output=True,
            text=True,
            encoding="ascii",
        ).stdout.strip()
        committed_full = subprocess.run(
            ["git", "rev-parse", f"HEAD:{full_relative.as_posix()}"],
            cwd=workspace_root,
            check=True,
            capture_output=True,
            text=True,
            encoding="ascii",
        ).stdout.strip()
        worktree_full = subprocess.run(
            ["git", "hash-object", full_relative.as_posix()],
            cwd=workspace_root,
            check=True,
            capture_output=True,
            text=True,
            encoding="ascii",
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise ProtocolError("runtime git identity is unavailable") from error
    full = verify_parser_artifact_manifest(full_generated.PARSER_ARTIFACT_MANIFEST_JSON)
    full_path = Path(full_generated.__file__).resolve()
    if (
        full.backend_id != _DIRECT_BACKEND_ID
        or full_generated.PARSERGEN_BACKEND_ID != _DIRECT_BACKEND_ID
        or full.artifact_role != "bsl-server-full"
        or full.identity_sha256 != full_generated.PARSER_IDENTITY_SHA256
        or full_path != (workspace_root / full_relative).resolve()
        or worktree_full != committed_full
        or re.fullmatch(r"[0-9a-f]{40}", commit) is None
    ):
        raise ProtocolError("workspace parser identity is not exact")
    require_clean_runtime_tree(workspace_root)
    return {
        "runtime_git_commit": commit,
        "runtime_tree_clean": True,
        "active_parser_backend_id": full.backend_id,
        "active_parser_artifact_role": full.artifact_role,
        "active_parser_identity_sha256": full.identity_sha256,
    }


def require_fresh_parser_runtime(
    session: RuntimeSession,
    expected: Mapping[str, object],
) -> dict[str, object]:
    """Fail before BSL execution when the session/cache is stale or mismatched."""
    current = active_parser_acceptance_checkpoint()
    api = session.runtime_api
    fresh = bool(
        not api._worker_module_artifacts
        and not api._worker_active_modules
        and api.worker_generation_handle is None
        and api.operation_worker_generation is None
        and api._worker_catalog_snapshot is None
    )
    if current != dict(expected) or not fresh:
        raise ProtocolError("runtime parser session is not fresh and exact")
    return {**current, "fresh_empty_session_cache": True}

SCHEMA = "onec-worker-universe-zup-acceptance-v2"
REFERENCE_PLATFORM_VERSION = "8.3.27.2170"
REFERENCE_PLATFORM_SHA256 = (
    "01eb37ac23e5bb25a4359665c01abf9a178b5066e78a198997b197126456b6f2"
)
REFERENCE_INFOBASE_IDENTITY_SHA256 = (
    "081f7a40d3343d923d60954a5f08f52bee98c5de13dc3b5374c8cc2485c7282b"
)
REFERENCE_EXTENSION_SHA256 = (
    "a29e51f4e83ab96a958fef9926b788ebe61582d91132bf41a298c4bffa5358ff"
)
REFERENCE_SOURCE_SHA256 = {
    "КадровыйУчет": (
        "378f23bcb775aaeddc2c664ff77717a611add2c81b9b5a63d83cff2354a308b6"
    ),
    "КадровыйУчетРасширенный": (
        "ea83a38b89304dc33521cfa5c7758380481dcc61db6fa6497e5de13c81b1fd72"
    ),
}
REFERENCE_SOURCE_BYTES = {
    "КадровыйУчет": 831_889,
    "КадровыйУчетРасширенный": 1_981_297,
}
REFERENCE_SOURCE_LINES = {
    "КадровыйУчет": 11_103,
    "КадровыйУчетРасширенный": 24_591,
}

PHASES = (
    "semantic_parse",
    "ast_model_extract",
    "catalog_validation",
    "dependency_analysis",
    "resolved_analysis_adapter",
    "alias_transform",
    "source_map_composition",
    "admission",
    "epf_packaging",
    "artifact_staging",
    "generation_create_wire_probe",
    "root_swap",
    "end_to_end",
)
_STAGING_DRILL_DOWN_PHASES = (
    "artifact_stage_sealed_validation",
    "artifact_stage_base64",
    "artifact_stage_executor",
    "artifact_stage_batch",
)
_ARTIFACT_STAGING_PHASE_INDEX = PHASES.index("artifact_staging")
_MEASUREMENT_PHASE_STREAM = (
    *PHASES[:_ARTIFACT_STAGING_PHASE_INDEX],
    *_STAGING_DRILL_DOWN_PHASES,
    *PHASES[_ARTIFACT_STAGING_PHASE_INDEX:],
)
_COMPONENT_PHASES = PHASES[:-1]
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_WINDOWS_ABSOLUTE_RE = re.compile(r"(?i)[a-z]:[\\/]")
_UNC_ABSOLUTE_RE = re.compile(r"(?:^|[\s\"'])\\\\[^\\\s]+\\[^\\\s]+")
_POSIX_ABSOLUTE_RE = re.compile(r"(?:^|[\s\"'])/(?!/)[^\s\"']+")
_UUID_RE = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b"
)
_BSL_SOURCE_RE = re.compile(
    r"(?i)(?:^|\s)(?:процедура|функция|procedure|function|"
    r"если\s+.+\s+тогда|конецесли|возврат\s+|результат\s*=|"
    r"вызватьисключение\s+|новый\s+|"
    r"[A-Za-zА-Яа-яЁё_][0-9A-Za-zА-Яа-яЁё_]*\s*=\s*[^;\r\n]+;)"
)
_OBJECT_REPR_RE = re.compile(
    r"(?i)(?:external\s*processing\s*object|"
    r"внешняя\s*обработка\s*объект)"
)
_EVIDENCE_CODE_RE = re.compile(r"[a-z][a-z0-9_]{2,95}\Z")
_PUBLIC_MODULE_RE = re.compile(r"[A-Za-zА-Яа-яЁё_][0-9A-Za-zА-Яа-яЁё_]{0,127}\Z")
_TOP_LEVEL_FIELDS = {
    "schema",
    "status",
    "sla_claimed",
    "warmup_iterations",
    "measured_iterations",
    "catalog_setup_ms",
    "percentile_method",
    "diagnostic_reparse",
    "reference",
    "admission",
    "incremental",
    "catalog_extension",
    "modes",
    "gates",
    "cleanup",
    "compatibility_inventory",
}
_REFERENCE_FIELDS = {
    "platform_version",
    "platform_sha256",
    "infobase_identity_sha256",
    "source_sha256",
    "catalog_sha256",
    "extension_sha256",
    "target_extension_current",
    "session_fresh",
}
_INCREMENTAL_COUNTERS = (
    "semantic_parse",
    "dependency_analysis",
    "alias_transform",
    "source_map_composition",
    "admission",
    "epf_packaging",
    "artifact_staging",
)
_MODE_FIELDS = {
    "phase_samples_ms",
    "parser_calls",
    "percentiles_ms",
    "end_to_end",
    "dominant_phase",
    "phase_accounting",
}
_TERMINAL_STATUSES = {
    "incompatible_prerequisites",
    "incompatible_runtime",
    "incompatible_source",
}
SEMANTIC_PASS_GATES = {
    "main": "PASS",
    "capture": "PASS",
    "serialization": "PASS",
    "diagnostics": "PASS",
    "lifecycle": "PASS",
    "failure_cleanup": "PASS",
    "privacy": "PASS",
}
_PRIMARY_MODULE = "КадровыйУчет"
_EXTENDED_MODULE = "КадровыйУчетРасширенный"
_MAIN_PROCEDURE_CELL = '''Процедура __OnecTask10NotebookProcedure(Состояние, Значение)
    Состояние.Вставить("Значение", Значение + 1);
КонецПроцедуры'''
_MAIN_FUNCTION_CELL = '''Функция __OnecTask10NotebookFunction(Значение)
    Возврат Значение * 2;
КонецФункции'''
_MAIN_MIXED_CELL = '''Функция __OnecTask10NotebookMixed(Значение)
    Возврат Значение + 3;
КонецФункции;
Task10MainMixed = __OnecTask10NotebookMixed(4);
Результат = Task10MainMixed;'''
_CAPTURE_PROCEDURE_CELL = '''Процедура __OnecTask10CaptureProcedure(Состояние, Значение)
    Состояние.Вставить("Значение", Значение + 5);
КонецПроцедуры'''
_CAPTURE_FUNCTION_CELL = '''Функция __OnecTask10CaptureFunction(Значение)
    Возврат Значение * 3;
КонецФункции'''
_CAPTURE_MIXED_CELL = '''Процедура __OnecTask10CaptureMixed(Состояние, Значение)
    Состояние.Вставить("Значение", Значение * 2);
КонецПроцедуры;
Task10CaptureMixedState = Новый Структура("Значение", 10);
Task10CaptureMixedResult = Task10CaptureMixedState.Значение * 2;
РезультатИнструкции = Task10CaptureMixedResult;'''
_CAPTURE_MIXED_ERROR_CELL = '''Процедура __OnecTask10CaptureAfterError(
    Состояние, Значение)
    Состояние.Вставить("Значение", Значение + 1);
КонецПроцедуры;
Task10CaptureRecoverySeed = 901;
Task10CaptureError = 1 / 0;'''
_CAPTURE_MIXED_RECOVERY_CELL = '''Task10CaptureRecovery = Task10CaptureRecoverySeed + 1;
РезультатИнструкции = Task10CaptureRecovery;'''
_PRIVATE_KEYS = frozenset(
    {
        "absolute_path",
        "diagnostic_message",
        "password",
        "platform_message",
        "private_path",
        "raw_bsl",
        "registration",
        "registration_id",
        "root_handle",
        "root_key",
        "source_text",
        "target_object",
        "target_repr",
        "transcript",
        "username",
    }
)


@dataclass(frozen=True, slots=True)
class ReferenceObservation:
    platform_version: str
    platform_sha256: str
    infobase_identity_sha256: str
    source_sha256: Mapping[str, str]
    catalog_sha256: str
    extension_sha256: str
    target_extension_current: bool
    session_fresh: bool


@dataclass(frozen=True, slots=True)
class IncrementalAccounting:
    unchanged_module: str = "КадровыйУчет"
    changed_module: str = "КадровыйУчетРасширенный"
    unchanged_parse: int = 0
    unchanged_dependency_analysis: int = 0
    unchanged_lowering: int = 0
    unchanged_source_map_composition: int = 0
    unchanged_admission: int = 0
    unchanged_packaging: int = 0
    unchanged_artifact_staging: int = 0
    changed_parse: int = 1
    changed_dependency_analysis: int = 1
    changed_lowering: int = 1
    changed_source_map_composition: int = 1
    changed_admission: int = 1
    changed_packaging: int = 1
    changed_artifact_staging: int = 1
    fresh_objects_created: int = 2
    fresh_objects_wired: int = 2
    unchanged_module_sha256: str = REFERENCE_SOURCE_SHA256["КадровыйУчет"]
    changed_module_sha256: str = REFERENCE_SOURCE_SHA256[
        "КадровыйУчетРасширенный"
    ]

    @classmethod
    def clean_update(cls) -> IncrementalAccounting:
        return cls()


@dataclass(frozen=True, slots=True)
class SemanticAcceptanceResult:
    admission: Mapping[str, object]
    accounting: IncrementalAccounting
    checks: Mapping[str, bool]
    catalog_setup_ms: float | None = None
    catalog_snapshot: CommonModuleCatalogSnapshot | None = None
    phase_samples: Mapping[str, Mapping[str, Sequence[float]]] | None = None
    parser_call_samples: Mapping[str, Mapping[str, Sequence[int]]] | None = None
    catalog_extension: Mapping[str, object] | None = None

    def gates(self) -> dict[str, str]:
        if set(self.checks) != set(SEMANTIC_PASS_GATES) or any(
            value is not True for value in self.checks.values()
        ):
            raise ProtocolError("ZUP observed semantic checks are incomplete")
        return {name: "PASS" for name in SEMANTIC_PASS_GATES}


@dataclass(frozen=True, slots=True, repr=False)
class ZupStaticPreflight:
    inventory: tuple[dict[str, object], ...]
    source_bundle: ZupSourceBundle | None = None
    catalog: CommonModuleCatalogSnapshot | None = None
    units: tuple[WorkerModuleUnit, ...] = ()
    analyses: tuple[AnalyzedWorkerModule, ...] = ()
    extension_sha256: str = ""


@dataclass(frozen=True, slots=True)
class _CatalogSetupObservation:
    elapsed_ms: float
    snapshot: CommonModuleCatalogSnapshot


def nearest_rank(samples: Sequence[float], quantile: float) -> float:
    if (
        not isinstance(samples, Sequence)
        or not samples
        or isinstance(samples, (str, bytes, bytearray))
        or type(quantile) is not float
        or not 0 < quantile <= 1
    ):
        raise ValueError("nearest-rank inputs are invalid")
    values: list[float] = []
    for value in samples:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("nearest-rank inputs are invalid")
        number = float(value)
        if number < 0 or not isfinite(number):
            raise ValueError("nearest-rank inputs are invalid")
        values.append(number)
    ordered = sorted(values)
    return ordered[ceil(quantile * len(ordered)) - 1]


def _hash_is_valid(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _catalog_setup_is_valid(value: object) -> bool:
    return bool(
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and float(value) >= 0
        and isfinite(float(value))
    )


def _reference_is_exact(reference: ReferenceObservation) -> bool:
    return not reference_compatibility_inventory(reference)


def reference_compatibility_inventory(
    reference: ReferenceObservation,
) -> list[dict[str, object]]:
    """Return stable, privacy-safe reasons why a reference is not exact."""

    inventory: list[dict[str, object]] = []
    if reference.platform_version != REFERENCE_PLATFORM_VERSION:
        inventory.append({"code": "platform_version_mismatch", "count": 1})
    if reference.platform_sha256 != REFERENCE_PLATFORM_SHA256:
        inventory.append({"code": "platform_identity_mismatch", "count": 1})
    if reference.infobase_identity_sha256 != REFERENCE_INFOBASE_IDENTITY_SHA256:
        inventory.append({"code": "infobase_identity_mismatch", "count": 1})
    source_hashes = dict(reference.source_sha256)
    mismatched_sources = sum(
        source_hashes.get(name) != expected
        for name, expected in REFERENCE_SOURCE_SHA256.items()
    )
    mismatched_sources += sum(
        name not in REFERENCE_SOURCE_SHA256 for name in source_hashes
    )
    if mismatched_sources:
        inventory.append(
            {"code": "source_identity_mismatch", "count": mismatched_sources}
        )
    if not _hash_is_valid(reference.catalog_sha256):
        inventory.append({"code": "catalog_identity_invalid", "count": 1})
    if not _hash_is_valid(reference.extension_sha256):
        inventory.append({"code": "extension_identity_invalid", "count": 1})
    elif reference.extension_sha256 != REFERENCE_EXTENSION_SHA256:
        inventory.append({"code": "extension_identity_mismatch", "count": 1})
    if reference.target_extension_current is not True:
        inventory.append({"code": "target_extension_not_current", "count": 1})
    if reference.session_fresh is not True:
        inventory.append({"code": "stale_target_session", "count": 1})
    return inventory


def _module_accounting(
    name: str,
    source_hash: str,
    *,
    semantic_parse: int,
    dependency_analysis: int,
    alias_transform: int,
    source_map_composition: int,
    admission: int,
    epf_packaging: int,
    artifact_staging: int,
) -> dict[str, object]:
    return {
        "logical_name": name,
        "module_sha256": source_hash,
        "semantic_parse": semantic_parse,
        "dependency_analysis": dependency_analysis,
        "alias_transform": alias_transform,
        "source_map_composition": source_map_composition,
        "admission": admission,
        "epf_packaging": epf_packaging,
        "artifact_staging": artifact_staging,
    }


def _incremental_evidence(accounting: IncrementalAccounting) -> dict[str, object]:
    return {
        "unchanged_module_sha256": accounting.unchanged_module_sha256,
        "changed_module_sha256": accounting.changed_module_sha256,
        "unchanged": _module_accounting(
            accounting.unchanged_module,
            accounting.unchanged_module_sha256,
            semantic_parse=accounting.unchanged_parse,
            dependency_analysis=accounting.unchanged_dependency_analysis,
            alias_transform=accounting.unchanged_lowering,
            source_map_composition=accounting.unchanged_source_map_composition,
            admission=accounting.unchanged_admission,
            epf_packaging=accounting.unchanged_packaging,
            artifact_staging=accounting.unchanged_artifact_staging,
        ),
        "changed": _module_accounting(
            accounting.changed_module,
            accounting.changed_module_sha256,
            semantic_parse=accounting.changed_parse,
            dependency_analysis=accounting.changed_dependency_analysis,
            alias_transform=accounting.changed_lowering,
            source_map_composition=accounting.changed_source_map_composition,
            admission=accounting.changed_admission,
            epf_packaging=accounting.changed_packaging,
            artifact_staging=accounting.changed_artifact_staging,
        ),
        "fresh_objects_created": accounting.fresh_objects_created,
        "fresh_objects_wired": accounting.fresh_objects_wired,
    }


def _incremental_is_valid(incremental: object) -> bool:
    if not isinstance(incremental, dict) or set(incremental) != {
        "unchanged_module_sha256",
        "changed_module_sha256",
        "unchanged",
        "changed",
        "fresh_objects_created",
        "fresh_objects_wired",
    }:
        return False
    unchanged = incremental.get("unchanged")
    changed = incremental.get("changed")
    module_fields = {"logical_name", "module_sha256", *_INCREMENTAL_COUNTERS}
    return bool(
        isinstance(unchanged, dict)
        and set(unchanged) == module_fields
        and unchanged.get("logical_name") == _PRIMARY_MODULE
        and unchanged.get("module_sha256") == REFERENCE_SOURCE_SHA256[_PRIMARY_MODULE]
        and all(
            type(unchanged.get(counter)) is int and unchanged[counter] == 0
            for counter in _INCREMENTAL_COUNTERS
        )
        and isinstance(changed, dict)
        and set(changed) == module_fields
        and changed.get("logical_name") == _EXTENDED_MODULE
        and changed.get("module_sha256") == REFERENCE_SOURCE_SHA256[_EXTENDED_MODULE]
        and all(
            type(changed.get(counter)) is int and changed[counter] == 1
            for counter in _INCREMENTAL_COUNTERS
        )
        and incremental.get("unchanged_module_sha256")
        == REFERENCE_SOURCE_SHA256[_PRIMARY_MODULE]
        and incremental.get("changed_module_sha256")
        == REFERENCE_SOURCE_SHA256[_EXTENDED_MODULE]
        and type(incremental.get("fresh_objects_created")) is int
        and incremental.get("fresh_objects_created") == 2
        and type(incremental.get("fresh_objects_wired")) is int
        and incremental.get("fresh_objects_wired") == 2
    )


def _exact_build_counters(value: object, expected: int) -> bool:
    return bool(
        isinstance(value, dict)
        and set(value) == set(_INCREMENTAL_COUNTERS)
        and all(
            type(value.get(counter)) is int and value[counter] == expected
            for counter in _INCREMENTAL_COUNTERS
        )
    )


def _exact_rollback_build_counters(value: object) -> bool:
    return bool(
        isinstance(value, dict)
        and set(value) == set(_INCREMENTAL_COUNTERS)
        and all(
            type(value.get(counter)) is int
            and value[counter] == (4 if counter == "semantic_parse" else 0)
            for counter in _INCREMENTAL_COUNTERS
        )
    )


def _exact_existing_extension_counters(value: object) -> bool:
    return bool(
        isinstance(value, dict)
        and set(value) == set(_INCREMENTAL_COUNTERS)
        and all(
            type(value.get(counter)) is int
            and value[counter]
            == (
                2
                if counter == "dependency_analysis"
                else 0
            )
            for counter in _INCREMENTAL_COUNTERS
        )
    )


def _catalog_extension_is_valid(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {"success", "rollback"}:
        return False
    success = value.get("success")
    rollback = value.get("rollback")
    return bool(
        isinstance(success, dict)
        and set(success)
        == {
            "xml_reads",
            "revision_delta",
            "runtime_dispatches",
            "unchanged",
            "new",
        }
        and type(success.get("xml_reads")) is int
        and success["xml_reads"] == 2
        and type(success.get("revision_delta")) is int
        and success["revision_delta"] == 1
        and type(success.get("runtime_dispatches")) is int
        and success["runtime_dispatches"] == 1
        and _exact_existing_extension_counters(success.get("unchanged"))
        and _exact_build_counters(success.get("new"), 2)
        and isinstance(rollback, dict)
        and set(rollback)
        == {
            "attempts",
            "xml_reads",
            "revision_delta",
            "runtime_dispatches",
            "catalog_unchanged",
            "active_generation_unchanged",
            "artifact_cache_delta",
            "build",
        }
        and type(rollback.get("attempts")) is int
        and rollback["attempts"] == 2
        and type(rollback.get("xml_reads")) is int
        and rollback["xml_reads"] == 3
        and type(rollback.get("revision_delta")) is int
        and rollback["revision_delta"] == 0
        and type(rollback.get("runtime_dispatches")) is int
        and rollback["runtime_dispatches"] == 0
        and rollback.get("catalog_unchanged") is True
        and rollback.get("active_generation_unchanged") is True
        and type(rollback.get("artifact_cache_delta")) is int
        and rollback["artifact_cache_delta"] == 0
        and _exact_rollback_build_counters(rollback.get("build"))
    )


def _mode_evidence(
    phases: Mapping[str, Sequence[float]],
    parser_calls: Mapping[str, Sequence[int]],
) -> dict[str, object]:
    normalized = {
        phase: [float(value) for value in phases[phase]] for phase in PHASES
    }
    component_sums = [
        sum(normalized[phase][index] for phase in _COMPONENT_PHASES)
        for index in range(len(normalized["end_to_end"]))
    ]
    unattributed = [
        normalized["end_to_end"][index] - component_sums[index]
        for index in range(len(component_sums))
    ]
    if any(value < -1e-9 for value in unattributed):
        raise ProtocolError("ZUP phase accounting is impossible")
    if set(parser_calls) != set(PARSER_CALL_COUNTERS):
        raise ProtocolError("ZUP parser call evidence is invalid")
    normalized_parser_calls: dict[str, list[int]] = {}
    for counter in PARSER_CALL_COUNTERS:
        values = parser_calls[counter]
        if (
            not isinstance(values, Sequence)
            or isinstance(values, (str, bytes, bytearray))
            or len(values) != len(normalized["end_to_end"])
            or any(type(value) is not int or value < 0 for value in values)
        ):
            raise ProtocolError("ZUP parser call evidence is invalid")
        normalized_parser_calls[counter] = list(values)
    if any(
        tuple(normalized_parser_calls[counter][index] for counter in PARSER_CALL_COUNTERS)
        != (1, 0, 0)
        for index in range(len(normalized["end_to_end"]))
    ):
        raise ProtocolError("ZUP parser call evidence is incompatible")
    summaries = {
        phase: {
            "p50_ms": nearest_rank(normalized[phase], 0.50),
            "p95_ms": nearest_rank(normalized[phase], 0.95),
        }
        for phase in PHASES
    }
    dominant = max(
        _COMPONENT_PHASES,
        key=lambda phase: (summaries[phase]["p50_ms"], phase),
    )
    return {
        "phase_samples_ms": normalized,
        "parser_calls": normalized_parser_calls,
        "percentiles_ms": summaries,
        "end_to_end": summaries["end_to_end"],
        "dominant_phase": dominant,
        "phase_accounting": {
            "component_sum_ms": component_sums,
            "unattributed_ms": unattributed,
        },
    }


def _build_compact_evidence_candidate(
    *,
    reference: ReferenceObservation,
    warmup_iterations: int,
    measured_iterations: int,
    catalog_setup_ms: float,
    phase_samples: Mapping[str, Mapping[str, Sequence[float]]],
    parser_call_samples: Mapping[str, Mapping[str, Sequence[int]]],
    accounting: IncrementalAccounting,
    catalog_extension: Mapping[str, object],
    admission: Mapping[str, object],
    gates: Mapping[str, object],
) -> dict[str, object]:
    if not _reference_is_exact(reference):
        raise ProtocolError("ZUP reference target identity is not exact")
    if not _catalog_setup_is_valid(catalog_setup_ms):
        raise ProtocolError("ZUP catalog setup evidence is invalid")
    if (
        type(warmup_iterations) is not int
        or warmup_iterations != 3
        or type(measured_iterations) is not int
        or measured_iterations != 60
    ):
        raise ValueError("ZUP PASS requires exactly 3 warmup and 60 measured iterations")
    if (
        set(phase_samples) != {"main", "capture"}
        or set(parser_call_samples) != {"main", "capture"}
    ):
        raise ProtocolError("ZUP acceptance mode samples are incomplete")
    modes: dict[str, object] = {}
    for mode in ("main", "capture"):
        phases = phase_samples[mode]
        if set(phases) != set(PHASES) or any(
            len(phases[phase]) != measured_iterations for phase in PHASES
        ):
            raise ProtocolError("ZUP acceptance phase samples are incomplete")
        modes[mode] = _mode_evidence(phases, parser_call_samples[mode])

    payload: dict[str, object] = {
        "schema": SCHEMA,
        "status": "PASS",
        "sla_claimed": True,
        "reference": {
            "platform_version": reference.platform_version,
            "platform_sha256": reference.platform_sha256,
            "infobase_identity_sha256": reference.infobase_identity_sha256,
            "source_sha256": dict(reference.source_sha256),
            "catalog_sha256": reference.catalog_sha256,
            "extension_sha256": reference.extension_sha256,
            "target_extension_current": reference.target_extension_current,
            "session_fresh": reference.session_fresh,
        },
        "warmup_iterations": warmup_iterations,
        "measured_iterations": measured_iterations,
        "catalog_setup_ms": float(catalog_setup_ms),
        "percentile_method": "nearest-rank",
        "diagnostic_reparse": False,
        "admission": dict(admission),
        "incremental": _incremental_evidence(accounting),
        "catalog_extension": dict(catalog_extension),
        "modes": modes,
        "gates": dict(gates),
        "cleanup": {"owned_processes": 0, "private_source_files": 0},
        "compatibility_inventory": [],
    }
    return payload


def build_compact_evidence(
    *,
    reference: ReferenceObservation,
    warmup_iterations: int,
    measured_iterations: int,
    catalog_setup_ms: float,
    phase_samples: Mapping[str, Mapping[str, Sequence[float]]],
    parser_call_samples: Mapping[str, Mapping[str, Sequence[int]]],
    accounting: IncrementalAccounting,
    catalog_extension: Mapping[str, object],
    admission: Mapping[str, object],
    gates: Mapping[str, object],
) -> dict[str, object]:
    return verify_compact_evidence(
        _build_compact_evidence_candidate(
            reference=reference,
            warmup_iterations=warmup_iterations,
            measured_iterations=measured_iterations,
            catalog_setup_ms=catalog_setup_ms,
            phase_samples=phase_samples,
            parser_call_samples=parser_call_samples,
            accounting=accounting,
            catalog_extension=catalog_extension,
            admission=admission,
            gates=gates,
        )
    )


def _walk_private(value: object, *, key: str = "") -> None:
    if key.casefold() in _PRIVATE_KEYS:
        raise ProtocolError("ZUP compact evidence contains private evidence")
    if isinstance(value, Mapping):
        for nested_key, nested_value in value.items():
            if not isinstance(nested_key, str):
                raise ProtocolError("ZUP compact evidence contains private evidence")
            _walk_private(nested_value, key=nested_key)
        return
    if isinstance(value, list):
        for item in value:
            _walk_private(item)
        return
    if isinstance(value, str):
        folded = value.casefold()
        if (
            _WINDOWS_ABSOLUTE_RE.search(value)
            or _UNC_ABSOLUTE_RE.search(value)
            or _POSIX_ABSOLUTE_RE.search(value)
            or _UUID_RE.search(value)
            or _BSL_SOURCE_RE.search(value)
            or _OBJECT_REPR_RE.search(value)
            or "внешняяобработкаобъект" in folded
            or "externalprocessingobject" in folded
            or "password=" in folded
            or "token=" in folded
            or len(value) > 512
        ):
            raise ProtocolError("ZUP compact evidence contains private evidence")


def _write_atomic_json(
    run_dir: Path,
    name: str,
    payload: Mapping[str, object],
) -> None:
    _walk_private(payload)
    destination = Path(run_dir) / name
    temporary: Path | None = None
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _persist_nonpass_measurement(
    run_dir: Path,
    candidate: Mapping[str, object],
) -> None:
    admission = candidate["admission"]
    if not isinstance(admission, Mapping):
        raise ProtocolError("ZUP real-module admission evidence is invalid")
    payload: dict[str, object] = {
        "schema": "onec-worker-universe-zup-measurement-nonpass-v1",
        "status": "DONE_WITH_CONCERNS",
        "sla_claimed": False,
        "failure_code": "measured_sla_failed",
        "counts": {
            "catalog_modules": admission["catalog_modules"],
            "warmup_iterations": candidate["warmup_iterations"],
            "measured_iterations": candidate["measured_iterations"],
        },
        "catalog_setup_ms": candidate["catalog_setup_ms"],
        "modes": candidate["modes"],
    }
    _write_atomic_json(
        Path(run_dir),
        "nonpass-measurement.json",
        payload,
    )


def _persist_pass_measurement(
    run_dir: Path,
    verified: Mapping[str, object],
) -> None:
    if verified.get("status") != "PASS" or verified.get("sla_claimed") is not True:
        raise ProtocolError("ZUP PASS measurement evidence is invalid")
    _write_atomic_json(
        Path(run_dir),
        "pass-measurement.json",
        verified,
    )


def _admission_is_valid(admission: object) -> bool:
    return bool(
        isinstance(admission, dict)
        and set(admission)
        == {
            "catalog_modules",
            "accepted_modules",
            "accepted_bindings",
            "original_targets",
            "same_generation_targets",
            "unsupported",
        }
        and type(admission.get("catalog_modules")) is int
        and admission["catalog_modules"] >= 2
        and admission.get("accepted_modules") == 2
        and type(admission.get("accepted_bindings")) is int
        and admission["accepted_bindings"] > 0
        and type(admission.get("original_targets")) is int
        and type(admission.get("same_generation_targets")) is int
        and admission["original_targets"] >= 0
        and admission["same_generation_targets"] >= 0
        and admission["original_targets"] + admission["same_generation_targets"]
        == admission["accepted_bindings"]
        and admission.get("unsupported") == []
    )


def _reference_mapping_is_exact(reference: object) -> bool:
    if not isinstance(reference, dict) or set(reference) != _REFERENCE_FIELDS:
        return False
    try:
        observation = ReferenceObservation(
            platform_version=reference.get("platform_version"),
            platform_sha256=reference.get("platform_sha256"),
            infobase_identity_sha256=reference.get("infobase_identity_sha256"),
            source_sha256=reference.get("source_sha256", {}),
            catalog_sha256=reference.get("catalog_sha256"),
            extension_sha256=reference.get("extension_sha256"),
            target_extension_current=reference.get("target_extension_current"),
            session_fresh=reference.get("session_fresh"),
        )
        return _reference_is_exact(observation)
    except (TypeError, ValueError):
        return False


def verify_compact_evidence(payload: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise ProtocolError("ZUP compact evidence is invalid")
    result = json.loads(json.dumps(payload, ensure_ascii=False))
    _walk_private(result)
    if result.get("schema") != SCHEMA or set(result) != _TOP_LEVEL_FIELDS:
        raise ProtocolError("ZUP compact evidence is invalid")
    catalog_setup_ms = result.get("catalog_setup_ms")
    if not _catalog_setup_is_valid(catalog_setup_ms):
        raise ProtocolError("ZUP catalog setup evidence is invalid")
    measured = result.get("measured_iterations")
    warmup = result.get("warmup_iterations")
    if (
        type(measured) is not int
        or measured != 60
        or type(warmup) is not int
        or warmup != 3
    ):
        raise ProtocolError("ZUP iteration evidence is invalid")
    status = result.get("status")
    if status != "PASS":
        if (
            status not in _TERMINAL_STATUSES
            or result.get("sla_claimed") is not False
            or result.get("percentile_method") != "nearest-rank"
            or result.get("diagnostic_reparse") is not False
            or result.get("modes") != {}
            or not isinstance(result.get("compatibility_inventory"), list)
            or not result["compatibility_inventory"]
            or result.get("cleanup")
            != {"owned_processes": 0, "private_source_files": 0}
        ):
            raise ProtocolError("ZUP terminal compatibility evidence is invalid")
        for item in result["compatibility_inventory"]:
            if (
                not isinstance(item, dict)
                or not set(item) <= {
                    "code",
                    "construct_category",
                    "count",
                    "module",
                    "span",
                }
                or _EVIDENCE_CODE_RE.fullmatch(str(item.get("code", ""))) is None
                or type(item.get("count")) is not int
                or item["count"] <= 0
            ):
                raise ProtocolError("ZUP terminal compatibility evidence is invalid")
            category = item.get("construct_category")
            module = item.get("module")
            span = item.get("span")
            if category is not None and _EVIDENCE_CODE_RE.fullmatch(
                str(category)
            ) is None:
                raise ProtocolError("ZUP terminal compatibility evidence is invalid")
            if module is not None and _PUBLIC_MODULE_RE.fullmatch(str(module)) is None:
                raise ProtocolError("ZUP terminal compatibility evidence is invalid")
            if span is not None and (
                not isinstance(span, list)
                or len(span) != 2
                or any(type(value) is not int or value < 0 for value in span)
                or span[1] < span[0]
            ):
                raise ProtocolError("ZUP terminal compatibility evidence is invalid")
        if (
            result.get("gates") != {}
            or result.get("admission") != {}
            or result.get("incremental") != {}
            or result.get("catalog_extension") != {}
        ):
            raise ProtocolError("ZUP terminal compatibility evidence is invalid")
        return result

    if result.get("sla_claimed") is not True:
        raise ProtocolError("ZUP PASS evidence must claim the measured SLA")
    if result.get("compatibility_inventory") != []:
        raise ProtocolError("ZUP PASS compatibility inventory is invalid")
    if not _reference_mapping_is_exact(result.get("reference")):
        raise ProtocolError("ZUP reference evidence is invalid")
    if not _catalog_extension_is_valid(result.get("catalog_extension")):
        raise ProtocolError("ZUP catalog extension evidence is invalid")
    if result.get("percentile_method") != "nearest-rank" or result.get(
        "diagnostic_reparse"
    ) is not False:
        raise ProtocolError("ZUP performance methodology evidence is invalid")
    admission = result.get("admission")
    if not _admission_is_valid(admission):
        raise ProtocolError("ZUP real-module admission evidence is invalid")
    if result.get("gates") != SEMANTIC_PASS_GATES:
        raise ProtocolError("ZUP semantic acceptance gates are invalid")

    modes = result.get("modes")
    if not isinstance(modes, dict) or set(modes) != {"main", "capture"}:
        raise ProtocolError("ZUP mode evidence is invalid")
    for mode in ("main", "capture"):
        evidence = modes[mode]
        if not isinstance(evidence, dict) or set(evidence) != _MODE_FIELDS:
            raise ProtocolError("ZUP mode evidence is invalid")
        samples = evidence.get("phase_samples_ms")
        parser_calls = evidence.get("parser_calls")
        summaries = evidence.get("percentiles_ms")
        if (
            not isinstance(samples, dict)
            or set(samples) != set(PHASES)
            or not isinstance(summaries, dict)
            or set(summaries) != set(PHASES)
        ):
            raise ProtocolError("ZUP phase evidence is invalid")
        if (
            not isinstance(parser_calls, dict)
            or set(parser_calls) != set(PARSER_CALL_COUNTERS)
        ):
            raise ProtocolError("ZUP parser call evidence is invalid")
        for phase in PHASES:
            values = samples[phase]
            if not isinstance(values, list) or len(values) != measured:
                raise ProtocolError("ZUP phase evidence is invalid")
            expected = {
                "p50_ms": nearest_rank(values, 0.50),
                "p95_ms": nearest_rank(values, 0.95),
            }
            if summaries[phase] != expected:
                raise ProtocolError("ZUP percentile evidence is invalid")
        for counter in PARSER_CALL_COUNTERS:
            values = parser_calls[counter]
            if (
                not isinstance(values, list)
                or len(values) != measured
                or any(type(value) is not int or value < 0 for value in values)
            ):
                raise ProtocolError("ZUP parser call evidence is invalid")
        if any(
            tuple(parser_calls[counter][index] for counter in PARSER_CALL_COUNTERS)
            != (1, 0, 0)
            for index in range(measured)
        ):
            raise ProtocolError("ZUP parser call evidence is incompatible")
        component_sums = [
            sum(samples[phase][index] for phase in _COMPONENT_PHASES)
            for index in range(measured)
        ]
        unattributed = [
            samples["end_to_end"][index] - component_sums[index]
            for index in range(measured)
        ]
        if any(value < -1e-9 for value in unattributed) or evidence.get(
            "phase_accounting"
        ) != {
            "component_sum_ms": component_sums,
            "unattributed_ms": unattributed,
        }:
            raise ProtocolError("ZUP phase accounting evidence is invalid")
        end_to_end = summaries["end_to_end"]
        if evidence.get("end_to_end") != end_to_end:
            raise ProtocolError("ZUP end-to-end evidence is invalid")
        if end_to_end["p95_ms"] >= 2_500:
            raise ProtocolError("ZUP measured SLA gate failed")
        dominant = max(
            _COMPONENT_PHASES,
            key=lambda phase: (summaries[phase]["p50_ms"], phase),
        )
        if evidence.get("dominant_phase") != dominant:
            raise ProtocolError("ZUP dominant phase evidence is invalid")

    if not _incremental_is_valid(result.get("incremental")):
        raise ProtocolError("ZUP incremental evidence is invalid")
    cleanup = result.get("cleanup")
    if cleanup != {"owned_processes": 0, "private_source_files": 0}:
        raise ProtocolError("ZUP cleanup evidence is invalid")
    return result


def _terminal_payload(
    inventory: list[dict[str, object]],
    *,
    warmup_iterations: int,
    measured_iterations: int,
    status: str = "incompatible_prerequisites",
    admission: Mapping[str, object] | None = None,
    gates: Mapping[str, object] | None = None,
    reference: Mapping[str, object] | None = None,
    incremental: Mapping[str, object] | None = None,
) -> dict[str, object]:
    return {
        "schema": SCHEMA,
        "status": status,
        "sla_claimed": False,
        "warmup_iterations": warmup_iterations,
        "measured_iterations": measured_iterations,
        "catalog_setup_ms": 0.0,
        "percentile_method": "nearest-rank",
        "diagnostic_reparse": False,
        "reference": dict(reference or {}),
        "admission": dict(admission or {}),
        "incremental": dict(incremental or {}),
        "catalog_extension": {},
        "modes": {},
        "gates": dict(gates or {}),
        "cleanup": {"owned_processes": 0, "private_source_files": 0},
        "compatibility_inventory": inventory,
    }


def _platform_sha256(config: RuntimeConfig) -> str:
    digest = sha256()
    for name in ("1cv8.exe", "1cv8c.exe", "dbgs.exe"):
        executable = Path(config.platform_bin) / name
        digest.update(name.encode("utf-8"))
        try:
            with executable.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
        except OSError as error:
            raise ProtocolError("ZUP platform identity is unavailable") from error
    return digest.hexdigest()


def _infobase_identity_sha256(config: RuntimeConfig) -> str:
    return sha256(
        str(Path(config.infobase_dir).resolve()).casefold().encode("utf-8")
    ).hexdigest()


def _matching_target_process_count(infobase_dir: Path) -> int:
    marker = str(Path(infobase_dir).resolve()).casefold()
    process_names = {
        "1cv8.exe",
        "1cv8c.exe",
        "dbgs.exe",
        "ragent.exe",
        "rmngr.exe",
        "rphost.exe",
    }
    matches = 0
    for process in psutil.process_iter(("name", "cmdline")):
        try:
            name = str(process.info.get("name") or "").casefold()
            command = " ".join(process.info.get("cmdline") or ()).casefold()
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            continue
        if name in process_names and marker in command:
            matches += 1
    return matches


def build_profile_catalog(
    source_root: Path,
    models: tuple[ParsedModuleModel, ...],
) -> CommonModuleCatalogSnapshot:
    """Resolve the exact production candidate graph for approved ZUP modules."""

    catalog = SessionCommonModuleCatalog(
        source_root,
        profile=_SESSION_CATALOG_PROFILE,
        preprocessor_profile="server",
    )
    catalog.resolve_candidates(worker_model_candidate_names(models))
    return catalog.ensure_modules((_PRIMARY_MODULE, _EXTENDED_MODULE))


def _mirror_common_module_metadata(source_root: Path, private_root: Path) -> Path:
    owned_root = Path(private_root).resolve(strict=True)
    mirror_root = (owned_root / ".private-catalog-source").resolve()
    if (
        mirror_root.parent != owned_root
        or mirror_root.name != ".private-catalog-source"
        or mirror_root.exists()
        or mirror_root.is_symlink()
    ):
        raise ProtocolError("ZUP private catalog mirror is unsafe")
    supplied_source = Path(source_root)
    supplied_common_modules = supplied_source / "CommonModules"
    if supplied_source.is_symlink() or supplied_common_modules.is_symlink():
        raise ProtocolError("ZUP source catalog mirror is unsafe")
    try:
        approved_root = supplied_source.resolve(strict=True)
        common_modules = (approved_root / "CommonModules").resolve(strict=True)
    except (FileNotFoundError, OSError) as error:
        raise ProtocolError("ZUP source catalog mirror is unsafe") from error
    if (
        not approved_root.is_dir()
        or not common_modules.is_dir()
        or common_modules.parent != approved_root
    ):
        raise ProtocolError("ZUP source catalog mirror is unsafe")
    try:
        metadata_files = tuple(
            sorted(
                common_modules.glob("*.xml"),
                key=lambda path: path.name.casefold(),
            )
        )
    except OSError as error:
        raise ProtocolError("ZUP source catalog mirror is unavailable") from error
    if not metadata_files:
        raise ProtocolError("ZUP source catalog mirror is empty")
    destination = mirror_root / "CommonModules"
    destination.mkdir(parents=True)
    try:
        for metadata_path in metadata_files:
            if metadata_path.is_symlink():
                raise ProtocolError("ZUP source catalog mirror is unsafe")
            resolved = metadata_path.resolve(strict=True)
            if resolved.parent != common_modules or not resolved.is_file():
                raise ProtocolError("ZUP source catalog mirror is unsafe")
            shutil.copyfile(resolved, destination / metadata_path.name)
    except OSError as error:
        raise ProtocolError("ZUP source catalog mirror is unavailable") from error
    return mirror_root


def _require_matching_catalog_snapshots(
    preflight: CommonModuleCatalogSnapshot,
    measured: CommonModuleCatalogSnapshot,
) -> CommonModuleCatalogSnapshot:
    if (
        not isinstance(preflight, CommonModuleCatalogSnapshot)
        or not isinstance(measured, CommonModuleCatalogSnapshot)
        or preflight.profile != measured.profile
        or preflight.preprocessor_profile != measured.preprocessor_profile
        or preflight.revision != measured.revision
        or preflight.modules != measured.modules
        or preflight.sha256 != measured.sha256
    ):
        raise ProtocolError("ZUP measured catalog identity does not match preflight")
    return measured


class _AcceptanceSessionCommonModuleCatalog(SessionCommonModuleCatalog):
    def __init__(self, source_root: Path) -> None:
        super().__init__(
            source_root,
            profile=_SESSION_CATALOG_PROFILE,
            preprocessor_profile="server",
        )
        self.metadata_reads = 0

    def _read_metadata(self, metadata_path: Path):
        self.metadata_reads += 1
        return super()._read_metadata(metadata_path)


def _bind_catalog_read_audit(
    session: RuntimeSession,
    catalog_source_root: Path,
) -> _AcceptanceSessionCommonModuleCatalog:
    source_root = Path(catalog_source_root).resolve(strict=True)
    current = session._require_common_module_catalog()
    if (
        current.initialized
        or getattr(current, "_source_root", None) != source_root
        or getattr(current, "_profile", None) != _SESSION_CATALOG_PROFILE
        or getattr(current, "_preprocessor_profile", None) != "server"
    ):
        raise ProtocolError("ZUP session catalog audit binding is invalid")
    observed = _AcceptanceSessionCommonModuleCatalog(source_root)
    session._common_module_catalog = observed
    if session._require_common_module_catalog() is not observed:
        raise ProtocolError("ZUP session catalog audit binding is invalid")
    return observed


def _worker_units(
    bundle: ZupSourceBundle,
) -> tuple[WorkerModuleUnit, ...]:
    units: list[WorkerModuleUnit] = []
    for item in bundle.units:
        unit_ref = SourceUnitRef(
            SourceUnitKind.MODULE,
            item.name,
            17,
            source_sha256(item.source),
        )
        units.append(
            WorkerModuleUnit(
                item.name,
                "module",
                17,
                mapped_visible_source(item.source, unit_ref),
            )
        )
    return tuple(units)


def _admission_inventory(
    units: tuple[WorkerModuleUnit, ...],
    models: tuple[ParsedModuleModel, ...],
    catalog: CommonModuleCatalogSnapshot,
) -> tuple[tuple[AnalyzedWorkerModule, ...], list[dict[str, object]]]:
    analyses: list[AnalyzedWorkerModule] = []
    inventory: list[dict[str, object]] = []
    for unit, model in zip(units, models, strict=True):
        try:
            plan = resolve_worker_dependencies(model, catalog)
            analyses.append(analyze_resolved_worker_module(unit, plan))
        except Exception as error:
            code = getattr(error, "code", "admission_rejection")
            span = getattr(error, "span", None)
            item: dict[str, object] = {
                "code": "real_module_admission_rejected",
                "construct_category": (
                    code if isinstance(code, str) else "admission_rejection"
                ),
                "count": 1,
                "module": unit.logical_name,
            }
            start = getattr(span, "start", None)
            end = getattr(span, "end", None)
            if type(start) is int and type(end) is int and 0 <= start <= end:
                item["span"] = [start, end]
            inventory.append(item)
    return tuple(analyses), inventory


def _packaged_extension_sha256() -> str:
    root = importlib.resources.files("onec_runtime").joinpath(
        "resources", "extension", "extension-manifest.json"
    )
    try:
        with importlib.resources.as_file(root) as manifest_path:
            return read_extension_manifest(manifest_path).fingerprints.artifact_sha256
    except Exception as error:
        raise ProtocolError("packaged extension identity is unavailable") from error


def inspect_zup_static_preflight(
    config: RuntimeConfig,
    source_root: Path | None,
) -> ZupStaticPreflight:
    """Inspect the approved target without starting or mutating a 1C session."""

    inventory: list[dict[str, object]] = []
    platform_bin = Path(config.platform_bin)
    infobase_dir = Path(config.infobase_dir)
    if not platform_bin.is_dir():
        inventory.append({"code": "platform_directory_missing", "count": 1})
    if not (infobase_dir / "1Cv8.1CD").is_file():
        inventory.append({"code": "infobase_file_missing", "count": 1})
    if source_root is None or not Path(source_root).is_dir():
        inventory.append({"code": "source_root_missing", "count": 1})
    if inventory:
        return ZupStaticPreflight(tuple(inventory))

    missing_executables = sum(
        not (platform_bin / name).is_file()
        for name in ("1cv8.exe", "1cv8c.exe", "dbgs.exe")
    )
    if missing_executables:
        inventory.append(
            {"code": "platform_executable_missing", "count": missing_executables}
        )
    else:
        if platform_bin.parent.name != REFERENCE_PLATFORM_VERSION:
            inventory.append({"code": "platform_version_mismatch", "count": 1})
        try:
            if _platform_sha256(config) != REFERENCE_PLATFORM_SHA256:
                inventory.append({"code": "platform_identity_mismatch", "count": 1})
        except ProtocolError:
            inventory.append({"code": "platform_identity_unavailable", "count": 1})
    if _infobase_identity_sha256(config) != REFERENCE_INFOBASE_IDENTITY_SHA256:
        inventory.append({"code": "infobase_identity_mismatch", "count": 1})
    stale_processes = _matching_target_process_count(infobase_dir)
    if stale_processes:
        inventory.append({"code": "stale_target_session", "count": stale_processes})

    try:
        bundle = admit_zup_source_bundle(Path(source_root))
    except ProtocolError:
        inventory.append({"code": "source_bundle_invalid", "count": 1})
        return ZupStaticPreflight(tuple(inventory))
    mismatched_sources = sum(
        item.source_sha256 != REFERENCE_SOURCE_SHA256[item.name]
        or item.source_bytes != REFERENCE_SOURCE_BYTES[item.name]
        or item.source_lines != REFERENCE_SOURCE_LINES[item.name]
        for item in bundle.units
    )
    if mismatched_sources:
        inventory.append(
            {"code": "source_identity_mismatch", "count": mismatched_sources}
        )
        return ZupStaticPreflight(tuple(inventory), source_bundle=bundle)

    try:
        units = _worker_units(bundle)
        models = tuple(
            parse_full_ast_module(unit.mapped_source.text) for unit in units
        )
        catalog = build_profile_catalog(Path(source_root), models)
        analyses, admission_inventory = _admission_inventory(
            units,
            models,
            catalog,
        )
        inventory.extend(admission_inventory)
    except Exception:
        inventory.append({"code": "catalog_or_parser_incompatible", "count": 1})
        return ZupStaticPreflight(tuple(inventory), source_bundle=bundle)
    try:
        extension_sha256 = _packaged_extension_sha256()
    except ProtocolError:
        inventory.append({"code": "extension_identity_unavailable", "count": 1})
        extension_sha256 = ""
    else:
        if extension_sha256 != REFERENCE_EXTENSION_SHA256:
            inventory.append({"code": "extension_identity_mismatch", "count": 1})
    return ZupStaticPreflight(
        tuple(inventory),
        source_bundle=bundle,
        catalog=catalog,
        units=units,
        analyses=analyses,
        extension_sha256=extension_sha256,
    )


def _preflight_inventory(
    config: RuntimeConfig,
    source_root: Path | None,
) -> list[dict[str, object]]:
    return list(inspect_zup_static_preflight(config, source_root).inventory)


def _admission_summary(preflight: ZupStaticPreflight) -> dict[str, object]:
    catalog = preflight.catalog
    unit_names = {unit.logical_name.casefold() for unit in preflight.units}
    bindings = tuple(
        binding for analysis in preflight.analyses for binding in analysis.dependencies
    )
    return {
        "catalog_modules": len(catalog.modules) if catalog is not None else 0,
        "accepted_modules": len(preflight.analyses),
        "accepted_bindings": len(bindings),
        "original_targets": sum(
            binding.target_module.casefold() not in unit_names for binding in bindings
        ),
        "same_generation_targets": sum(
            binding.target_module.casefold() in unit_names for binding in bindings
        ),
        "unsupported": [],
    }


def _exact_reference_summary(
    preflight: ZupStaticPreflight,
    measured_catalog: CommonModuleCatalogSnapshot,
) -> dict[str, object]:
    preflight_catalog = preflight.catalog
    if preflight_catalog is None or not _hash_is_valid(preflight.extension_sha256):
        raise ProtocolError("ZUP exact reference summary is unavailable")
    catalog = _require_matching_catalog_snapshots(
        preflight_catalog,
        measured_catalog,
    )
    return {
        "platform_version": REFERENCE_PLATFORM_VERSION,
        "platform_sha256": REFERENCE_PLATFORM_SHA256,
        "infobase_identity_sha256": REFERENCE_INFOBASE_IDENTITY_SHA256,
        "source_sha256": dict(REFERENCE_SOURCE_SHA256),
        "catalog_sha256": catalog.sha256,
        "extension_sha256": preflight.extension_sha256,
        "target_extension_current": True,
        "session_fresh": True,
    }


def _append_module_source(source: str, addition: str) -> str:
    separator = "" if source.endswith(("\n", "\r")) else "\n"
    return f"{source}{separator}\n{addition.strip()}\n"


def _acceptance_module_sources(
    preflight: ZupStaticPreflight,
    *,
    extended_value: int,
) -> dict[str, str]:
    bundle = preflight.source_bundle
    if bundle is None:
        raise ProtocolError("ZUP real semantic source bundle is unavailable")
    raw = {item.name: item.source for item in bundle.units}
    if set(raw) != {_PRIMARY_MODULE, _EXTENDED_MODULE}:
        raise ProtocolError("ZUP real semantic source bundle is incomplete")
    primary = f'''
Функция __OnecTask10SameName() Экспорт
    Возврат 1000 + {_EXTENDED_MODULE}.__OnecTask10SameName();
КонецФункции

Функция __OnecTask10Function(Значение) Экспорт
    Возврат Значение + {_EXTENDED_MODULE}.__OnecTask10Function(0);
КонецФункции

Процедура __OnecTask10Procedure(Состояние) Экспорт
    {_EXTENDED_MODULE}.__OnecTask10Procedure(Состояние);
КонецПроцедуры

Функция __OnecTask10ValueReference() Экспорт
    ПервыйТип = ТипЗнч({_EXTENDED_MODULE});
    ВторойТип = ТипЗнч({_EXTENDED_MODULE});
    Возврат ПервыйТип = ВторойТип;
КонецФункции

Функция __OnecTask10CrossCall() Экспорт
    Возврат 1700 + {_EXTENDED_MODULE}.__OnecTask10SameName();
КонецФункции

Функция __OnecTask10CallThrow() Экспорт
    Возврат {_EXTENDED_MODULE}.__OnecTask10Throw();
КонецФункции'''
    extended = f'''
Функция __OnecTask10SameName() Экспорт
    Возврат {extended_value};
КонецФункции

Функция __OnecTask10Function(Значение) Экспорт
    Возврат Значение + {extended_value};
КонецФункции

Процедура __OnecTask10Procedure(Состояние) Экспорт
    Состояние.Вставить("Значение", {extended_value});
КонецПроцедуры

Функция __OnecTask10Throw() Экспорт
    ВызватьИсключение "task10-runtime-diagnostic";
КонецФункции'''
    return {
        _PRIMARY_MODULE: _append_module_source(raw[_PRIMARY_MODULE], primary),
        _EXTENDED_MODULE: _append_module_source(raw[_EXTENDED_MODULE], extended),
    }


def _acceptance_units(
    preflight: ZupStaticPreflight,
    *,
    extended_revision: int,
    extended_value: int,
) -> tuple[WorkerModuleUnit, ...]:
    sources = _acceptance_module_sources(
        preflight,
        extended_value=extended_value,
    )
    revisions = {
        _PRIMARY_MODULE: 17,
        _EXTENDED_MODULE: extended_revision,
    }
    return tuple(
        WorkerModuleUnit(
            name,
            "module",
            revisions[name],
            mapped_visible_source(
                sources[name],
                SourceUnitRef(
                    SourceUnitKind.MODULE,
                    name,
                    revisions[name],
                    source_sha256(sources[name]),
                ),
            ),
        )
        for name in (_PRIMARY_MODULE, _EXTENDED_MODULE)
    )


def _measurement_units(
    base_units: tuple[WorkerModuleUnit, ...],
    *,
    revision: int,
) -> tuple[WorkerModuleUnit, ...]:
    if (
        type(base_units) is not tuple
        or len(base_units) != 2
        or tuple(unit.logical_name for unit in base_units)
        != (_PRIMARY_MODULE, _EXTENDED_MODULE)
        or type(revision) is not int
        or revision <= base_units[1].revision
    ):
        raise ProtocolError("ZUP measurement base units are invalid")
    primary, extended = base_units
    canary_source = _append_module_source(
        extended.mapped_source.text,
        f'''Функция __OnecTask10ReloadCanary() Экспорт
    Возврат {revision};
КонецФункции''',
    )
    canary_ref = SourceUnitRef(
        SourceUnitKind.MODULE,
        extended.logical_name,
        revision,
        source_sha256(canary_source),
    )
    return (
        primary,
        WorkerModuleUnit(
            extended.logical_name,
            extended.kind,
            revision,
            mapped_visible_source(canary_source, canary_ref),
        ),
    )


def _measure_mode(
    session: RuntimeSession,
    mode: str,
    base_units: tuple[WorkerModuleUnit, ...],
    *,
    warmups: int = 3,
    iterations: int = 60,
    _parser_call_samples: dict[str, list[int]] | None = None,
) -> dict[str, list[float]]:
    if mode not in {"main", "capture"}:
        raise ValueError("ZUP measurement mode is invalid")
    if (
        type(warmups) is not int
        or warmups < 0
        or type(iterations) is not int
        or iterations <= 0
    ):
        raise ValueError("ZUP measurement iteration budget is invalid")
    if (
        type(base_units) is not tuple
        or len(base_units) != 2
        or tuple(unit.logical_name for unit in base_units)
        != (_PRIMARY_MODULE, _EXTENDED_MODULE)
    ):
        raise ProtocolError("ZUP measurement base units are invalid")

    samples = {phase: [] for phase in PHASES}
    parser_samples = {counter: [] for counter in PARSER_CALL_COUNTERS}
    schedule_size = warmups + iterations
    mode_offset = 0 if mode == "main" else schedule_size
    for ordinal in range(schedule_size):
        revision = base_units[1].revision + mode_offset + ordinal + 1
        units = _measurement_units(base_units, revision=revision)
        recorder = PhaseRecorder()
        handle = session.load_worker_modules(units, profiler=recorder)
        try:
            events = tuple(recorder.events)
            if (
                tuple(event.phase for event in events) != _MEASUREMENT_PHASE_STREAM
                or any(event.error_present for event in events)
            ):
                raise ProtocolError("ZUP reload phase stream is invalid")
            parser_calls = recorder.parser_calls
            events_by_phase = {
                event.phase: event
                for event in events
                if event.phase in PHASES
            }
            staging_drill_down = {
                event.phase: event
                for event in events
                if event.phase in _STAGING_DRILL_DOWN_PHASES
            }
            if (
                events_by_phase["epf_packaging"].item_count != 1
                or events_by_phase["artifact_staging"].item_count != 1
                or events_by_phase["generation_create_wire_probe"].item_count != 1
                or events_by_phase["root_swap"].item_count != 1
                or events_by_phase["end_to_end"].item_count != 2
                or staging_drill_down[
                    "artifact_stage_sealed_validation"
                ].item_count
                != 2
                or any(
                    staging_drill_down[phase].item_count != 1
                    for phase in _STAGING_DRILL_DOWN_PHASES[1:]
                )
            ):
                raise ProtocolError("ZUP reload incremental phase accounting is invalid")
            if ordinal >= warmups:
                if parser_calls.as_tuple() != (1, 0, 0):
                    raise ProtocolError(
                        "ZUP reload parser call evidence is incompatible"
                    )
                for event in events:
                    if event.phase not in PHASES:
                        continue
                    samples[event.phase].append(event.wall_ns / 1_000_000)
                for counter, value in parser_calls.as_dict().items():
                    parser_samples[counter].append(value)
        finally:
            session.release_worker_generation(handle)
    if _parser_call_samples is not None:
        _parser_call_samples.clear()
        _parser_call_samples.update(parser_samples)
    return samples


def _catalog_setup_from_first_load(
    session: RuntimeSession,
    recorder: PhaseRecorder,
) -> _CatalogSetupObservation:
    events = tuple(
        event for event in recorder.events if event.phase == "catalog_validation"
    )
    snapshot = session.runtime_api._worker_catalog_snapshot
    if (
        not isinstance(snapshot, CommonModuleCatalogSnapshot)
        or len(events) != 1
        or events[0].error_present
        or events[0].item_count != len(snapshot.modules)
    ):
        raise ProtocolError("ZUP catalog setup measurement is invalid")
    return _CatalogSetupObservation(
        elapsed_ms=events[0].wall_ns / 1_000_000,
        snapshot=snapshot,
    )


def _capture_pair_result_source(before: str, after: str) -> str:
    if (
        not isinstance(before, str)
        or _PUBLIC_MODULE_RE.fullmatch(before) is None
        or not isinstance(after, str)
        or _PUBLIC_MODULE_RE.fullmatch(after) is None
    ):
        raise ValueError("ZUP CAPTURE result identifiers are invalid")
    return (
        f'Результат = Формат({before}, "ЧГ=0") + "|" + '
        f'Формат({after}, "ЧГ=0");'
    )


def _pinned_capture_canary_source(module_name: str) -> str:
    if (
        not isinstance(module_name, str)
        or _PUBLIC_MODULE_RE.fullmatch(module_name) is None
    ):
        raise ValueError("ZUP CAPTURE canary module is invalid")
    return (
        f"РезультатИнструкции = {module_name}."
        "__OnecTask10CrossCall();"
    )


@contextmanager
def _controlled_capture_reload_context(session: RuntimeSession) -> Iterator[None]:
    capture_location = _synthetic_capture_location(session.config.runtime.runtime_dir)
    session.configure_capture_points((capture_location,))
    captured = _require_reply(
        session.execute_bsl(
            f"Task10MeasureBeforeCapture = {_PRIMARY_MODULE}."
            "__OnecTask10CrossCall();\n"
            "Task10MeasureSyntheticCapture = RuntimeKernelServer."
            "СинтетическийCapture(100);\n"
            f"Task10MeasureAfterCapture = {_PRIMARY_MODULE}."
            "__OnecTask10CrossCall();\n"
            + _capture_pair_result_source(
                "Task10MeasureBeforeCapture",
                "Task10MeasureAfterCapture",
            )
        ),
        kinds=(RuntimeReplyKind.CAPTURED,),
    )
    if (
        captured.location != capture_location
        or session.runtime_api.operation_worker_generation is None
    ):
        raise ProtocolError("ZUP CAPTURE measurement context is invalid")
    try:
        yield
    except BaseException:
        raise
    else:
        if session.runtime_api.operation_worker_generation is None:
            raise ProtocolError("ZUP CAPTURE measurement context is invalid")
        _require_reply(
            session.runtime_api.resume_capture(),
            kinds=(RuntimeReplyKind.MAIN_COMPLETED,),
            expected="1719|1719",
        )
        if session.runtime_api.operation_worker_generation is not None:
            raise ProtocolError("ZUP CAPTURE measurement context is invalid")


def _collect_mode_samples(
    session: RuntimeSession,
    base_units: tuple[WorkerModuleUnit, ...],
    *,
    warmups: int = 3,
    iterations: int = 60,
    _capture_context_factory: _CaptureContextFactory | None = None,
) -> tuple[
    dict[str, dict[str, list[float]]],
    dict[str, dict[str, list[int]]],
]:
    if session.runtime_api.operation_worker_generation is not None:
        raise ProtocolError("ZUP MAIN measurement context is invalid")
    main_parser_calls: dict[str, list[int]] = {}
    main = _measure_mode(
        session,
        "main",
        base_units,
        warmups=warmups,
        iterations=iterations,
        _parser_call_samples=main_parser_calls,
    )
    if session.runtime_api.operation_worker_generation is not None:
        raise ProtocolError("ZUP MAIN measurement context is invalid")
    capture_context = _capture_context_factory or _controlled_capture_reload_context
    with capture_context(session):
        if session.runtime_api.operation_worker_generation is None:
            raise ProtocolError("ZUP CAPTURE measurement context is invalid")
        capture_parser_calls: dict[str, list[int]] = {}
        capture = _measure_mode(
            session,
            "capture",
            base_units,
            warmups=warmups,
            iterations=iterations,
            _parser_call_samples=capture_parser_calls,
        )
    if session.runtime_api.operation_worker_generation is not None:
        raise ProtocolError("ZUP CAPTURE measurement context is invalid")
    return (
        {"main": main, "capture": capture},
        {"main": main_parser_calls, "capture": capture_parser_calls},
    )


def _synthetic_common_module_metadata(
    name: str,
    dependency: str,
    *,
    malformed: bool = False,
) -> str:
    server_property = "" if malformed else "<Server>true</Server>"
    return (
        "<MetaDataObject><CommonModule><Properties>"
        f"<Name>{name}</Name><Synonym/><Comment>depends-on:{dependency}</Comment>"
        f"<Global>false</Global>{server_property}"
        "<ClientManagedApplication>false</ClientManagedApplication>"
        "<ClientOrdinaryApplication>false</ClientOrdinaryApplication>"
        "</Properties></CommonModule></MetaDataObject>"
    )


def _synthetic_catalog_extension_units() -> tuple[WorkerModuleUnit, ...]:
    names = ("НовыйА", "НовыйБ")
    sources = (
        "Функция __OnecCatalogExtensionA() Экспорт\n"
        "    Возврат НовыйБ.__OnecCatalogExtensionB();\n"
        "КонецФункции",
        "Функция __OnecCatalogExtensionB() Экспорт\n"
        "    Возврат НовыйА.__OnecCatalogExtensionA();\n"
        "КонецФункции",
    )
    units = []
    for name, source in zip(names, sources, strict=True):
        reference = SourceUnitRef(
            SourceUnitKind.MODULE,
            name,
            1,
            source_sha256(source),
        )
        units.append(
            WorkerModuleUnit(
                name,
                "module",
                1,
                mapped_visible_source(source, reference),
            )
        )
    return tuple(units)


def _worker_module_cache_key(unit: WorkerModuleUnit) -> tuple[str, str, int, str, str]:
    return (
        unit.logical_name.casefold(),
        unit.kind,
        unit.revision,
        unit.mapped_source.artifact.source_sha256,
        unit.mapped_source.source_map_sha256,
    )


def _phase_build_counters(recorder: PhaseRecorder) -> dict[str, int]:
    result = {
        phase: sum(event.phase == phase for event in recorder.events)
        for phase in _INCREMENTAL_COUNTERS
    }
    staging_events = tuple(
        event for event in recorder.events if event.phase == "artifact_staging"
    )
    result["artifact_staging"] = sum(event.item_count for event in staging_events)
    return result


def _catalog_metadata_reads(catalog: SessionCommonModuleCatalog) -> int:
    value = getattr(catalog, "metadata_reads", None)
    if type(value) is not int or value < 0:
        raise ProtocolError("ZUP catalog metadata read audit is unavailable")
    return value


def _run_missing_module_extension_gate(
    session: RuntimeSession,
    catalog_source_root: Path,
    unchanged_units: tuple[WorkerModuleUnit, ...],
) -> dict[str, object]:
    source_root = Path(catalog_source_root).resolve(strict=True)
    common_modules = (source_root / "CommonModules").resolve(strict=True)
    catalog = session._require_common_module_catalog()
    if (
        type(unchanged_units) is not tuple
        or len(unchanged_units) != 2
        or tuple(unit.logical_name for unit in unchanged_units)
        != (_PRIMARY_MODULE, _EXTENDED_MODULE)
        or getattr(catalog, "_source_root", None) != source_root
        or not common_modules.is_dir()
        or common_modules.parent != source_root
    ):
        raise ProtocolError("ZUP private catalog extension fixture is unsafe")
    before = catalog.ensure_initialized()
    dependencies = {"НовыйА": "НовыйБ", "НовыйБ": "НовыйА"}
    if any((common_modules / f"{name}.xml").exists() for name in dependencies):
        raise ProtocolError("ZUP private catalog extension fixture is unsafe")
    new_units = _synthetic_catalog_extension_units()
    candidate_units = (*unchanged_units, *new_units)
    api = session.runtime_api
    cache = getattr(api, "_worker_module_artifacts", None)
    host = getattr(api, "_worker_universe", None)
    if not isinstance(cache, dict) or host is None:
        raise ProtocolError("ZUP catalog extension runtime audit is unavailable")

    rollback_reads = 0
    rollback_revision_delta = 0
    rollback_dispatches = 0
    rollback_cache_delta = 0
    rollback_build = {counter: 0 for counter in _INCREMENTAL_COUNTERS}
    catalog_unchanged = True
    active_unchanged = True
    for invalid_index, invalid_name in enumerate(dependencies):
        for name, dependency in dependencies.items():
            (common_modules / f"{name}.xml").write_text(
                _synthetic_common_module_metadata(
                    name,
                    dependency,
                    malformed=name == invalid_name,
                ),
                encoding="utf-8",
                newline="\n",
            )
        attempt_snapshot = catalog.ensure_initialized()
        attempt_active = host.active_handle
        attempt_cache = dict(api._worker_module_artifacts)
        reads_before = _catalog_metadata_reads(catalog)
        recorder = PhaseRecorder()
        try:
            session.load_worker_modules(candidate_units, profiler=recorder)
        except ProtocolError:
            pass
        else:
            raise ProtocolError("ZUP malformed catalog extension was admitted")
        current = catalog.ensure_initialized()
        rollback_reads += _catalog_metadata_reads(catalog) - reads_before
        rollback_revision_delta += current.revision - attempt_snapshot.revision
        rollback_dispatches += sum(
            event.phase == "generation_create_wire_probe"
            for event in recorder.events
        )
        current_cache = getattr(api, "_worker_module_artifacts", None)
        if not isinstance(current_cache, dict):
            raise ProtocolError("ZUP catalog extension runtime audit is unavailable")
        rollback_cache_delta += len(current_cache) - len(attempt_cache)
        build = _phase_build_counters(recorder)
        rollback_build = {
            counter: rollback_build[counter] + build[counter]
            for counter in _INCREMENTAL_COUNTERS
        }
        catalog_unchanged = catalog_unchanged and current is attempt_snapshot
        active_unchanged = active_unchanged and host.active_handle is attempt_active
        expected_reads = invalid_index + 1
        rollback_phases = tuple(event.phase for event in recorder.events)
        if (
            rollback_phases
            != (
                "semantic_parse",
                "ast_model_extract",
                "semantic_parse",
                "ast_model_extract",
                "catalog_validation",
                "end_to_end",
            )
            or any(event.error_present for event in recorder.events[:4])
            or any(not event.error_present for event in recorder.events[4:])
            or _catalog_metadata_reads(catalog) - reads_before != expected_reads
            or dict(current_cache) != attempt_cache
        ):
            raise ProtocolError("ZUP malformed catalog rollback evidence is invalid")

    for name, dependency in dependencies.items():
        (common_modules / f"{name}.xml").write_text(
            _synthetic_common_module_metadata(name, dependency),
            encoding="utf-8",
            newline="\n",
        )
    success_before = catalog.ensure_initialized()
    success_cache = dict(api._worker_module_artifacts)
    reads_before = _catalog_metadata_reads(catalog)
    success_recorder = PhaseRecorder()
    handle = session.load_worker_modules(
        candidate_units,
        profiler=success_recorder,
    )
    try:
        after = catalog.ensure_initialized()
        current_cache = getattr(api, "_worker_module_artifacts", None)
        if not isinstance(current_cache, dict):
            raise ProtocolError("ZUP catalog extension runtime audit is unavailable")
        cache_additions = set(current_cache) - set(success_cache)
        addition_names = {key[0] for key in cache_additions}
        new_names = {unit.logical_name.casefold() for unit in new_units}
        unchanged_names = {unit.logical_name.casefold() for unit in unchanged_units}
        observed_build = _phase_build_counters(success_recorder)
        new_cache_misses = sum(name in new_names for name in addition_names)
        unchanged_cache_misses = sum(
            name in unchanged_names for name in addition_names
        )
        unchanged_build = {
            counter: observed_build[counter] - new_cache_misses
            for counter in _INCREMENTAL_COUNTERS
        }
        new_build = {
            counter: observed_build[counter] - unchanged_build[counter]
            for counter in _INCREMENTAL_COUNTERS
        }
        unchanged_keys_were_cached = all(
            _worker_module_cache_key(unit) in success_cache
            for unit in unchanged_units
        )
        runtime_dispatches = sum(
            event.phase == "generation_create_wire_probe"
            for event in success_recorder.events
        )
        success_phase_counts = {
            phase: sum(event.phase == phase for event in success_recorder.events)
            for phase in (*PHASES, *_STAGING_DRILL_DOWN_PHASES)
        }
        expected_phase_counts = {
            phase: (
                4
                if phase == "dependency_analysis"
                else 2
                if phase
                in (
                    *_INCREMENTAL_COUNTERS[:-1],
                    "ast_model_extract",
                    "resolved_analysis_adapter",
                )
                else 1
            )
            for phase in PHASES
        }
        expected_phase_counts.update(
            {phase: 1 for phase in _STAGING_DRILL_DOWN_PHASES}
        )
        staging_drill_down = tuple(
            event
            for event in success_recorder.events
            if event.phase in _STAGING_DRILL_DOWN_PHASES
        )
        staging = tuple(
            event
            for event in success_recorder.events
            if event.phase == "artifact_staging"
        )
        promotion = tuple(
            event
            for event in success_recorder.events
            if event.phase in {"generation_create_wire_probe", "root_swap"}
        )
        end_to_end = tuple(
            event for event in success_recorder.events if event.phase == "end_to_end"
        )
        if (
            after.revision - success_before.revision != 1
            or _catalog_metadata_reads(catalog) - reads_before != 2
            or {item.canonical_name for item in after.modules}
            != {item.canonical_name for item in before.modules} | set(dependencies)
            or addition_names != new_names
            or new_cache_misses != 2
            or unchanged_cache_misses != 0
            or not unchanged_keys_were_cached
            or not _exact_build_counters(new_build, 2)
            or not _exact_existing_extension_counters(unchanged_build)
            or any(event.error_present for event in success_recorder.events)
            or len(success_recorder.events) != sum(expected_phase_counts.values())
            or success_phase_counts != expected_phase_counts
            or tuple(event.phase for event in staging_drill_down)
            != _STAGING_DRILL_DOWN_PHASES
            or any(
                event.item_count
                != (
                    len(candidate_units)
                    if event.phase == "artifact_stage_sealed_validation"
                    else len(new_units)
                )
                for event in staging_drill_down
            )
            or len(staging) != 1
            or staging[0].item_count != len(new_units)
            or len(promotion) != 2
            or any(event.item_count != 1 for event in promotion)
            or len(end_to_end) != 1
            or end_to_end[0].item_count != len(candidate_units)
            or runtime_dispatches != 1
            or host.active_handle is not handle
        ):
            raise ProtocolError("ZUP catalog extension success evidence is invalid")
    finally:
        session.release_worker_generation(handle)

    evidence: dict[str, object] = {
        "success": {
            "xml_reads": _catalog_metadata_reads(catalog) - reads_before,
            "revision_delta": after.revision - success_before.revision,
            "runtime_dispatches": runtime_dispatches,
            "unchanged": unchanged_build,
            "new": new_build,
        },
        "rollback": {
            "attempts": len(dependencies),
            "xml_reads": rollback_reads,
            "revision_delta": rollback_revision_delta,
            "runtime_dispatches": rollback_dispatches,
            "catalog_unchanged": catalog_unchanged,
            "active_generation_unchanged": active_unchanged,
            "artifact_cache_delta": rollback_cache_delta,
            "build": rollback_build,
        },
    }
    if (
        evidence
        != {
            "success": {
                "xml_reads": 2,
                "revision_delta": 1,
                "runtime_dispatches": 1,
                "unchanged": {
                    counter: (
                        2
                        if counter == "dependency_analysis"
                        else 0
                    )
                    for counter in _INCREMENTAL_COUNTERS
                },
                "new": {counter: 2 for counter in _INCREMENTAL_COUNTERS},
            },
            "rollback": {
                "attempts": 2,
                "xml_reads": 3,
                "revision_delta": 0,
                "runtime_dispatches": 0,
                "catalog_unchanged": True,
                "active_generation_unchanged": True,
                "artifact_cache_delta": 0,
                "build": {
                    counter: (4 if counter == "semantic_parse" else 0)
                    for counter in _INCREMENTAL_COUNTERS
                },
            },
        }
    ):
        raise ProtocolError("ZUP missing-module extension gate is invalid")
    return evidence


def _require_reply(
    reply: RuntimeReply,
    *,
    kinds: tuple[RuntimeReplyKind, ...],
    expected: object | None = None,
) -> RuntimeReply:
    if (
        not isinstance(reply, RuntimeReply)
        or reply.kind not in kinds
        or reply.succeeded is not True
        or (expected is not None and reply.result != expected)
    ):
        raise ProtocolError("ZUP real semantic reply failed")
    return reply


def _require_real_failure(operation: Callable[[], object]) -> object:
    try:
        result = operation()
    except BslExecutionError as error:
        if error.diagnostic is None:
            raise ProtocolError("ZUP real diagnostic is unavailable") from error
        return error.diagnostic
    if not isinstance(result, RuntimeReply) or result.succeeded is not False:
        raise ProtocolError("ZUP real negative gate did not fail")
    if result.diagnostic is None:
        raise ProtocolError("ZUP real diagnostic is unavailable")
    return result.diagnostic


def _verify_compile_diagnostic_contract(
    diagnostic: object,
    *,
    source: str,
    source_unit: SourceUnitRef,
    marker: str,
) -> None:
    from onec_runtime.bsl.diagnostics import (
        DiagnosticStage,
        MappingConfidence,
        NormalizedDiagnostic,
    )
    from onec_runtime.bsl.source_maps import SourceSpan

    offset = source.find(marker)
    location = getattr(diagnostic, "visible_location", None)
    if (
        not isinstance(diagnostic, NormalizedDiagnostic)
        or diagnostic.stage is not DiagnosticStage.COMPILATION
        or diagnostic.mapping_confidence is not MappingConfidence.EXACT
        or offset < 0
        or source.find(marker, offset + 1) >= 0
        or diagnostic.source_unit != source_unit
        or location is None
        or location.source_unit != source_unit
        or location.span != SourceSpan(offset, offset + 1)
    ):
        raise ProtocolError("ZUP compile diagnostic contract is invalid")


def _compile_diagnostic_probe() -> tuple[str, str]:
    marker = "Task10UndefinedCompileProcedure"
    return f"{marker}();\nРезультат = Истина;", marker


def _verify_runtime_diagnostic_contract(
    diagnostic: object,
    *,
    units: Sequence[tuple[SourceUnitRef, str]],
    markers: Sequence[str],
) -> None:
    from onec_runtime.bsl.diagnostics import (
        DiagnosticStage,
        MappingConfidence,
        NormalizedDiagnostic,
    )
    from onec_runtime.bsl.source_maps import SourceSpan

    if (
        not isinstance(diagnostic, NormalizedDiagnostic)
        or diagnostic.stage is not DiagnosticStage.EXECUTION
        or diagnostic.mapping_confidence is not MappingConfidence.EXACT
        or len(units) != 2
        or len(markers) != 2
        or len(diagnostic.worker_frames) != 2
    ):
        raise ProtocolError("ZUP runtime diagnostic contract is invalid")
    for frame, (unit, source), marker in zip(
        diagnostic.worker_frames, units, markers, strict=True
    ):
        offset = source.find(marker)
        location = frame.visible_location
        if (
            frame.logical_name != unit.unit_id
            or frame.revision != unit.revision
            or frame.mapping_confidence is not MappingConfidence.EXACT
            or frame.source_unit != unit
            or location is None
            or location.source_unit != unit
            or offset < 0
            or source.find(marker, offset + 1) >= 0
            or location.span != SourceSpan(offset, offset + 1)
        ):
            raise ProtocolError("ZUP runtime diagnostic contract is invalid")


def _synthetic_capture_location(runtime_dir: Path) -> object:
    repository = Path(__file__).resolve().parents[1]
    source_path = (
        repository
        / "onec"
        / "OnecInteractiveRuntime"
        / "CommonModules"
        / "RuntimeKernelServer"
        / "Ext"
        / "Module.bsl"
    )
    try:
        source = source_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as error:
        raise ProtocolError(
            "packaged synthetic capture source is unavailable"
        ) from error
    lines = tuple(
        number
        for number, text in enumerate(source.splitlines(), start=1)
        if SYNTHETIC_CAPTURE_A_MARKER in text
    )
    if len(lines) != 1:
        raise ProtocolError("packaged synthetic capture marker is ambiguous")
    manifest = packaged_extension_bundle(runtime_dir).manifest
    return replace(manifest.breakpoints.server_entry, line=lines[0])


def _capture_phase_result(phase: int, boundary: int) -> int:
    if type(phase) is not int or phase <= 0 or boundary not in {1, 2}:
        raise ProtocolError("ZUP capture semantic phase identity is invalid")
    return phase * 100 + boundary


def _begin_capture_semantic_operation(
    session: RuntimeSession,
    capture_location: object,
    *,
    result: int,
) -> object:
    captured = _require_reply(
        session.execute_bsl(
            "Task10CaptureBoundary = RuntimeKernelServer."
            "СинтетическийCapture(100);\n"
            f"Результат = {result};"
        ),
        kinds=(RuntimeReplyKind.CAPTURED,),
    )
    pin = session.runtime_api.operation_worker_generation
    if captured.location != capture_location or pin is None:
        raise ProtocolError("ZUP CAPTURE semantic operation pin is invalid")
    return pin


def _resume_capture_semantic_operation(
    session: RuntimeSession,
    *,
    expected: int,
) -> None:
    _require_reply(
        session.runtime_api.resume_capture(),
        kinds=(RuntimeReplyKind.MAIN_COMPLETED,),
        expected=expected,
    )
    if session.runtime_api.operation_worker_generation is not None:
        raise ProtocolError("ZUP CAPTURE semantic pin survived terminal resume")


def _exercise_capture_reload_visibility(
    session: RuntimeSession,
    capture_location: object,
    *,
    worker_source: str,
    probe_source: str,
    probe_expected: object,
    phase: int,
    worker_kinds: tuple[RuntimeReplyKind, ...] = (
        RuntimeReplyKind.WORKER_LOADED,
    ),
    worker_expected: object | None = None,
) -> None:
    """Prove a CAPTURE reload becomes visible only to the next operation."""
    loader_result = _capture_phase_result(phase, 1)
    visible_result = _capture_phase_result(phase, 2)
    original_pin = _begin_capture_semantic_operation(
        session,
        capture_location,
        result=loader_result,
    )
    _require_reply(
        session.execute_bsl(worker_source),
        kinds=worker_kinds,
        expected=worker_expected,
    )
    promoted = session.runtime_api.worker_generation_handle
    if (
        session.runtime_api.operation_worker_generation is not original_pin
        or promoted is None
        or promoted is original_pin
    ):
        raise ProtocolError("ZUP CAPTURE reload changed the pinned operation")
    _resume_capture_semantic_operation(session, expected=loader_result)

    visible_pin = _begin_capture_semantic_operation(
        session,
        capture_location,
        result=visible_result,
    )
    if visible_pin is not promoted:
        raise ProtocolError("ZUP CAPTURE reload was not pinned by the next operation")
    _require_reply(
        session.execute_bsl(probe_source),
        kinds=(RuntimeReplyKind.CAPTURE_CELL,),
        expected=probe_expected,
    )
    if session.runtime_api.operation_worker_generation is not visible_pin:
        raise ProtocolError("ZUP CAPTURE probe changed its operation pin")
    _resume_capture_semantic_operation(session, expected=visible_result)


def _materialize_proxy(session: RuntimeSession, name: str) -> object:
    from onec_runtime_jupyter.extension import OnecValueProxy

    snapshot = session.namespace_snapshot()
    if name not in snapshot.names:
        raise ProtocolError("ZUP proxy value is absent from the namespace")
    proxy = OnecValueProxy(
        session,
        name,
        runtime_generation=snapshot.runtime_generation,
        context_generation=snapshot.context_generation,
    )
    return proxy.materialize(max_depth=8, max_items=32, max_bytes=65_536)


def _exercise_capture_error_reload_visibility(
    session: RuntimeSession,
    capture_location: object,
    *,
    phase: int,
) -> None:
    loader_result = _capture_phase_result(phase, 1)
    visible_result = _capture_phase_result(phase, 2)
    original_pin = _begin_capture_semantic_operation(
        session,
        capture_location,
        result=loader_result,
    )
    _require_real_failure(lambda: session.execute_bsl(_CAPTURE_MIXED_ERROR_CELL))
    promoted = session.runtime_api.worker_generation_handle
    if (
        session.runtime_api.operation_worker_generation is not original_pin
        or promoted is None
        or promoted is original_pin
    ):
        raise ProtocolError("ZUP failed CAPTURE reload changed the pinned operation")
    _require_reply(
        session.execute_bsl(_CAPTURE_MIXED_RECOVERY_CELL),
        kinds=(RuntimeReplyKind.CAPTURE_CELL,),
        expected=902,
    )
    _resume_capture_semantic_operation(session, expected=loader_result)

    visible_pin = _begin_capture_semantic_operation(
        session,
        capture_location,
        result=visible_result,
    )
    if visible_pin is not promoted:
        raise ProtocolError(
            "ZUP failed CAPTURE reload was not pinned by the next operation"
        )
    _require_reply(
        session.execute_bsl(
            "Task10CaptureRecoveryState = Новый Структура;\n"
            "__OnecTask10CaptureAfterError(Task10CaptureRecoveryState, 901);\n"
            "РезультатИнструкции = "
            "Task10CaptureRecoveryState.Значение;"
        ),
        kinds=(RuntimeReplyKind.CAPTURE_CELL,),
        expected=902,
    )
    _require_reply(
        session.execute_bsl(
            'Task10CaptureProxy = Новый Структура("Статус,Значение", '
            '"PASS", 19);\nРезультатИнструкции = Истина;'
        ),
        kinds=(RuntimeReplyKind.CAPTURE_CELL,),
        expected=True,
    )
    if _materialize_proxy(session, "Task10CaptureProxy") != {
        "Статус": "PASS",
        "Значение": 19,
    }:
        raise ProtocolError("ZUP CAPTURE proxy serialization failed")
    if session.runtime_api.operation_worker_generation is not visible_pin:
        raise ProtocolError("ZUP failed CAPTURE probe changed its operation pin")
    _resume_capture_semantic_operation(session, expected=visible_result)


class _PrivateProxyRuntimeView:
    """Expose one reserved bare name to a proxy while delegating the real guard."""

    def __init__(self, runtime: object, name: str) -> None:
        self._runtime = runtime
        self._name = name

    def namespace_snapshot(self) -> object:
        snapshot = self._runtime.namespace_snapshot()
        names = tuple(snapshot.names)
        if self._name.casefold() not in {name.casefold() for name in names}:
            names = (*names, self._name)
        return replace(snapshot, names=names)

    def require_public_value_handle(self, handle: str) -> None:
        self._runtime.require_public_value_handle(handle)


def _require_private_proxy_rejection(
    session: RuntimeSession,
    name: str,
    *,
    path: tuple[str, ...] = (),
) -> None:
    from onec_runtime_jupyter.extension import OnecValueProxy

    view = _PrivateProxyRuntimeView(session, name)
    snapshot = view.namespace_snapshot()
    proxy = OnecValueProxy(
        view,
        name,
        runtime_generation=snapshot.runtime_generation,
        context_generation=snapshot.context_generation,
        path=path,
    )
    try:
        proxy.materialize(max_depth=2, max_items=2, max_bytes=1_024)
    except ProtocolError as error:
        if str(error) != "Worker generation objects are not public values":
            raise ProtocolError("ZUP private proxy rejection was not exact") from error
    else:
        raise ProtocolError("ZUP private proxy value escaped")


def _require_worker_proxy_privacy(session: RuntimeSession) -> None:
    _require_private_proxy_rejection(
        session,
        "RuntimeWorkerActiveGeneration",
    )
    _require_private_proxy_rejection(
        session,
        "RuntimeWorkerActiveGeneration",
        path=("Modules", _PRIMARY_MODULE),
    )


_INCREMENTAL_OBJECT_PROBE_PREFIX = "ONEC_ZUP_INCREMENTAL_OBJECT_PROBE_V1"
_INCREMENTAL_OBJECT_PROBE_FIELD_ORDER = (
    "primary_fresh",
    "extended_fresh",
    "objects_distinct",
    "primary_type",
    "extended_type",
    "primary_access",
    "extended_access",
    "dependency_wired",
)
_INCREMENTAL_OBJECT_PROBE_FIELDS = set(_INCREMENTAL_OBJECT_PROBE_FIELD_ORDER)


def _incremental_object_probe_source(
    *,
    extended_value: int,
    primary_registration: str,
    extended_registration: str,
) -> str:
    if (
        type(extended_value) is not int
        or extended_value <= 0
        or re.fullmatch(
            r"OnecRuntime_[0-9a-f]{8}_[0-9a-f]{16}",
            primary_registration,
            re.IGNORECASE,
        )
        is None
        or re.fullmatch(
            r"OnecRuntime_[0-9a-f]{8}_[0-9a-f]{16}",
            extended_registration,
            re.IGNORECASE,
        )
        is None
    ):
        raise ValueError("ZUP incremental probe revision value is invalid")
    old_modules = "__OnecPinnedWorkerGeneration.Modules.Получить"
    new_modules = "Контекст.RuntimeWorkerActiveGeneration.Modules.Получить"
    old_primary = f'{old_modules}("{_PRIMARY_MODULE}")'
    old_extended = f'{old_modules}("{_EXTENDED_MODULE}")'
    new_primary = f'{new_modules}("{_PRIMARY_MODULE}")'
    new_extended = f'{new_modules}("{_EXTENDED_MODULE}")'
    primary_type_probe = (
        "ТипЗнч(ВнешниеОбработки.Создать("
        f"{bsl_string_literal(primary_registration)}))"
    )
    extended_type_probe = (
        "ТипЗнч(ВнешниеОбработки.Создать("
        f"{bsl_string_literal(extended_registration)}))"
    )
    conditions = (
        f"{old_primary} <> {new_primary}",
        f"{old_extended} <> {new_extended}",
        f"{new_primary} <> {new_extended}",
        f"ТипЗнч({new_primary}) = {primary_type_probe}",
        f"ТипЗнч({new_extended}) = {extended_type_probe}",
        f"{new_primary}.__OnecTask10SameName() = {1000 + extended_value}",
        f"{new_extended}.__OnecTask10SameName() = {extended_value}",
        f"{new_primary}.__OnecTask10CrossCall() = {1700 + extended_value}",
    )
    lines = [
        "РезультатИнструкции = "
        f'{bsl_string_literal(_INCREMENTAL_OBJECT_PROBE_PREFIX)};'
    ]
    lines.extend(
        "РезультатИнструкции = РезультатИнструкции + "
        f'"|" + ?({condition}, "1", "0");'
        for condition in conditions
    )
    return "\n".join(lines)


def _verify_incremental_object_probe(observation: object) -> tuple[int, int]:
    if isinstance(observation, str):
        fields = observation.split("|")
        if (
            len(fields) != 1 + len(_INCREMENTAL_OBJECT_PROBE_FIELD_ORDER)
            or fields[0] != _INCREMENTAL_OBJECT_PROBE_PREFIX
            or any(field not in {"0", "1"} for field in fields[1:])
        ):
            raise ProtocolError("ZUP incremental object probe is invalid")
        observation = dict(
            zip(
                _INCREMENTAL_OBJECT_PROBE_FIELD_ORDER,
                (field == "1" for field in fields[1:]),
                strict=True,
            )
        )
    if (
        not isinstance(observation, Mapping)
        or set(observation) != _INCREMENTAL_OBJECT_PROBE_FIELDS
        or any(type(observation[field]) is not bool for field in observation)
        or any(observation[field] is not True for field in observation)
    ):
        raise ProtocolError("ZUP incremental object probe is invalid")
    created = sum(
        observation[field] is True
        for field in ("primary_fresh", "extended_fresh")
    )
    wired = sum(
        observation[field] is True
        for field in ("primary_access", "extended_access")
    )
    if created != 2 or wired != 2:
        raise ProtocolError("ZUP incremental object probe is invalid")
    return created, wired


def _execute_trusted_incremental_object_probe(api: object, source: str) -> object:
    """Run the internal old/new root comparison outside the user-source lowerer."""
    controller = getattr(api, "_controller", None)
    execute = getattr(controller, "execute_system_capture", None)
    if not callable(execute):
        raise ProtocolError("ZUP trusted incremental capture is unavailable")
    probe = execute(source)
    if not isinstance(probe, CaptureCellResult):
        raise ProtocolError("ZUP trusted incremental capture result is invalid")
    return probe.result


def _observe_incremental_promotion(
    session: RuntimeSession,
    units: tuple[WorkerModuleUnit, ...],
) -> tuple[WorkerGenerationHandle, IncrementalAccounting]:
    api = session.runtime_api
    cache_before = dict(api._worker_module_artifacts)

    handle = session.load_worker_modules(units)
    manifest = api._worker_universe.active_manifest
    if manifest is None:
        raise ProtocolError("ZUP incremental active manifest is unavailable")
    registrations = {
        module.logical_name: module.registration_name for module in manifest.modules
    }
    if set(registrations) != {_PRIMARY_MODULE, _EXTENDED_MODULE}:
        raise ProtocolError("ZUP incremental registration set is invalid")

    cache_after = dict(api._worker_module_artifacts)
    new_keys = set(cache_after) - set(cache_before)
    new_by_module = {
        name: sum(key[0] == name.casefold() for key in new_keys)
        for name in (_PRIMARY_MODULE, _EXTENDED_MODULE)
    }
    if new_by_module != {_PRIMARY_MODULE: 0, _EXTENDED_MODULE: 1}:
        raise ProtocolError("ZUP incremental module cache evidence is invalid")
    observation = _execute_trusted_incremental_object_probe(
        api,
        _incremental_object_probe_source(
            extended_value=18,
            primary_registration=registrations[_PRIMARY_MODULE],
            extended_registration=registrations[_EXTENDED_MODULE],
        ),
    )
    created, wired = _verify_incremental_object_probe(observation)
    accounting = IncrementalAccounting(
        unchanged_parse=0,
        unchanged_dependency_analysis=0,
        unchanged_lowering=0,
        unchanged_source_map_composition=0,
        unchanged_admission=0,
        unchanged_packaging=0,
        unchanged_artifact_staging=0,
        changed_parse=1,
        changed_dependency_analysis=1,
        changed_lowering=1,
        changed_source_map_composition=1,
        changed_admission=1,
        changed_packaging=1,
        changed_artifact_staging=1,
        fresh_objects_created=created,
        fresh_objects_wired=wired,
    )
    if not _incremental_is_valid(_incremental_evidence(accounting)):
        raise ProtocolError("ZUP incremental promotion evidence is invalid")
    return handle, accounting


def _exercise_captured_two_promotion_lifecycle(
    session: RuntimeSession,
    *,
    g17: WorkerGenerationHandle,
    g18_units: tuple[WorkerModuleUnit, ...],
    g19_units: tuple[WorkerModuleUnit, ...],
) -> tuple[WorkerGenerationHandle, WorkerGenerationHandle, IncrementalAccounting]:
    """Prove G17's pin survives automatic ownership retirement through G19."""
    api = session.runtime_api
    host = api._worker_universe
    original_operation_pin = api._operation_generation_pin
    if original_operation_pin is None:
        raise ProtocolError("ZUP CAPTURE original operation pin is invalid")
    if api.operation_worker_generation is not g17:
        raise ProtocolError("ZUP CAPTURE original generation pin is invalid")
    original_manifest_sha256 = g17.manifest_sha256
    g18, accounting = _observe_incremental_promotion(session, g18_units)
    original_record = host._generations.get(g17.generation)
    if original_record is None or original_record.handle is not g17:
        raise ProtocolError(
            "ZUP CAPTURE original generation identity is absent after first promotion"
        )
    if original_record.explicitly_retained:
        raise ProtocolError(
            "ZUP CAPTURE original explicit ownership survived first replacement"
        )
    if api.operation_worker_generation is not g17:
        raise ProtocolError(
            "ZUP CAPTURE original generation pin changed after first promotion"
        )
    if api._operation_generation_pin is not original_operation_pin:
        raise ProtocolError(
            "ZUP CAPTURE original operation pin changed after first promotion"
        )
    g19 = session.load_worker_modules(g19_units)
    if not (
        g18.generation == g17.generation + 1
        and g19.generation == g18.generation + 1
    ):
        raise ProtocolError("ZUP Worker generation sequence is invalid")
    original_record = host._generations.get(g17.generation)
    if original_record is None or original_record.handle is not g17:
        raise ProtocolError("ZUP CAPTURE original generation identity is absent")
    if original_record.explicitly_retained:
        raise ProtocolError(
            "ZUP CAPTURE original explicit ownership survived replacement"
        )
    if api.operation_worker_generation is not g17:
        raise ProtocolError("ZUP CAPTURE original generation pin changed on promotion")
    if api._operation_generation_pin is not original_operation_pin:
        raise ProtocolError("ZUP CAPTURE original operation pin changed on promotion")
    if host.active_handle is not g19:
        raise ProtocolError("ZUP CAPTURE current generation identity is invalid")
    paused_inventory = host._confirmed_live_inventory()
    if (
        paused_inventory is None
        or paused_inventory.manifest_sha256s
        != frozenset((original_manifest_sha256, g19.manifest_sha256))
    ):
        raise ProtocolError("ZUP CAPTURE confirmed manifest identity is invalid")
    paused_views = tuple(view.handle for view in host._retained_debug_views())
    if (
        len(paused_views) != 2
        or paused_views[0] is not g17
        or paused_views[1] is not g19
    ):
        raise ProtocolError("ZUP CAPTURE retained debug-view identity is invalid")
    for _ in range(2):
        prepared = api.prepare_capture_hypothesis(
            _pinned_capture_canary_source(_PRIMARY_MODULE)
        )
        _require_reply(
            api.execute_prepared_capture_hypothesis(prepared),
            kinds=(RuntimeReplyKind.CAPTURE_CELL,),
            expected=1719,
        )
        if api.operation_worker_generation is not g17:
            raise ProtocolError("ZUP CAPTURE hypothesis changed its generation")
        if api._operation_generation_pin is not original_operation_pin:
            raise ProtocolError("ZUP CAPTURE hypothesis changed its operation pin")
    _require_reply(
        api.resume_capture(),
        kinds=(RuntimeReplyKind.MAIN_COMPLETED,),
        expected="1717|1717",
    )
    if api.operation_worker_generation is not None:
        raise ProtocolError("ZUP CAPTURE original pin survived terminal completion")
    if api._operation_generation_pin is not None:
        raise ProtocolError(
            "ZUP CAPTURE original operation pin survived terminal completion"
        )
    terminal_inventory = host._confirmed_live_inventory()
    if (
        terminal_inventory is None
        or terminal_inventory.manifest_sha256s != frozenset((g19.manifest_sha256,))
    ):
        raise ProtocolError(
            "ZUP CAPTURE terminal confirmed manifest identity is invalid"
        )
    terminal_views = tuple(view.handle for view in host._retained_debug_views())
    if len(terminal_views) != 1 or terminal_views[0] is not g19:
        raise ProtocolError(
            "ZUP CAPTURE terminal retained debug-view identity is invalid"
        )
    if set(host._generations) != {g19.generation}:
        raise ProtocolError("ZUP CAPTURE terminal retention is invalid")
    _require_reply(
        session.execute_bsl(
            f"Результат = {_PRIMARY_MODULE}.__OnecTask10CrossCall();"
        ),
        kinds=(RuntimeReplyKind.MAIN_COMPLETED,),
        expected=1719,
    )
    return g18, g19, accounting


def _failure_cleanup_snapshot(session: RuntimeSession) -> tuple[object, ...]:
    api = session.runtime_api
    host = api._worker_universe
    target = api._worker_universe_target
    return (
        host.state,
        host.active_handle,
        host.active_manifest,
        host.active_root_key,
        host._pending,
        dict(host._generations),
        dict(host._handles),
        dict(host._registration_refcounts),
        dict(host._registration_artifacts),
        set(host._quarantine_holds),
        target._broken,
        {key: set(value) for key, value in target._candidate_registrations.items()},
    )


def _known_wire_failure_unit(
    units: tuple[WorkerModuleUnit, ...],
    catalog: CommonModuleCatalogSnapshot,
) -> WorkerModuleUnit:
    primary = next(unit for unit in units if unit.logical_name == _PRIMARY_MODULE)
    source = _append_module_source(
        primary.mapped_source.text,
        f'''
Функция __OnecTask10KnownWireFailure()
    Возврат {_EXTENDED_MODULE}.__OnecTask10SameName();
КонецФункции''',
    )
    return WorkerModuleUnit(
        _PRIMARY_MODULE,
        "module",
        99,
        mapped_visible_source(
            source,
            SourceUnitRef(
                SourceUnitKind.MODULE,
                _PRIMARY_MODULE,
                99,
                source_sha256(source),
            ),
        ),
    )


_KNOWN_WIRE_ATTRIBUTION_FIELDS = {
    "phase",
    "source_module",
    "target_module",
    "target_kind",
    "revision",
}


def _inject_known_wire_failure(
    source: str,
    *,
    binding_index: int,
    export_variable: str,
) -> str:
    """Null the admitted source object immediately before one real wire."""
    if (
        not isinstance(source, str)
        or type(binding_index) is not int
        or binding_index < 0
        or not isinstance(export_variable, str)
        or _PUBLIC_MODULE_RE.fullmatch(export_variable) is None
    ):
        raise ProtocolError("ZUP known wire failure injection is invalid")
    wire_markers = tuple(
        re.finditer(
            r'(?m)^\s*ЭтапПубликацииWorker(?:_[0-9]+)? = "wire";\s*$',
            source,
        )
    )
    assignment_pattern = re.compile(
        r"(?m)^(?P<indent>[ \t]+)"
        rf"(?P<source>ИсточникЗависимостиWorker{binding_index}(?:_[0-9]+)?)\."
        + re.escape(export_variable)
        + rf" = ЦельЗависимостиWorker{binding_index}(?:_[0-9]+)?;[ \t]*$"
    )
    assignments = tuple(assignment_pattern.finditer(source))
    if (
        len(wire_markers) != 1
        or len(assignments) != 1
        or wire_markers[0].start() >= assignments[0].start()
    ):
        raise ProtocolError("ZUP known wire failure injection is ambiguous")
    assignment = assignments[0]
    injection = (
        f'{assignment.group("indent")}{assignment.group("source")} = '
        "Неопределено;\n"
    )
    injected = source[: assignment.start()] + injection + source[assignment.start() :]
    if injected.count(injection) != 1:
        raise ProtocolError("ZUP known wire failure injection is ambiguous")
    return injected


def _verify_known_wire_failure(
    error: BaseException,
    attribution: object,
) -> None:
    if isinstance(error, WorkerPromotionOutcomeUnknown):
        raise ProtocolError("ZUP known wire failure outcome is unknown") from error
    if (
        not isinstance(error, BslExecutionError)
        or _worker_promotion_failure_phase(error) != "wire"
        or not isinstance(attribution, dict)
        or set(attribution) != _KNOWN_WIRE_ATTRIBUTION_FIELDS
        or attribution.get("phase") != "wire"
        or attribution.get("source_module") != _PRIMARY_MODULE
        or attribution.get("target_module") != _EXTENDED_MODULE
        or attribution.get("target_kind") != "overloaded"
        or type(attribution.get("revision")) is not int
        or attribution.get("revision") != 99
    ):
        raise ProtocolError("ZUP known wire failure evidence is invalid")


class _KnownWireFailureExecutor:
    """Inject one test-only failure into the exact pending promotion binding."""

    def __init__(self, target: object) -> None:
        executor = getattr(target, "_instruction_executor", None)
        if not callable(executor):
            raise ProtocolError("ZUP known wire executor is unavailable")
        self._target = target
        self.original = executor
        self.attribution: dict[str, object] | None = None
        self.observed_error: BaseException | None = None
        self.injections = 0

    def __call__(self, source: str) -> object:
        if "onec-worker-root-prepare-stage=" not in source:
            return self.original(source)
        host = getattr(self._target, "_host", None)
        candidate = getattr(host, "_pending", None)
        manifest = getattr(candidate, "manifest", None)
        wiring = getattr(manifest, "wiring", ())
        matching = tuple(
            (index, binding)
            for index, binding in enumerate(wiring)
            if getattr(binding, "source_module", None) == _PRIMARY_MODULE
            and getattr(binding, "target_module", None) == _EXTENDED_MODULE
            and getattr(binding, "target_kind", None) == "overloaded"
        )
        modules = tuple(
            module
            for module in getattr(manifest, "modules", ())
            if getattr(module, "logical_name", None) == _PRIMARY_MODULE
        )
        if len(matching) != 1 or len(modules) != 1:
            raise ProtocolError("ZUP known wire binding attribution is ambiguous")
        binding_index, binding = matching[0]
        revision = getattr(modules[0], "revision", None)
        export_variable = getattr(binding, "export_variable", None)
        attribution: dict[str, object] = {
            "phase": "wire",
            "source_module": _PRIMARY_MODULE,
            "target_module": _EXTENDED_MODULE,
            "target_kind": "overloaded",
            "revision": revision,
        }
        if self.injections != 0:
            raise ProtocolError("ZUP known wire failure injection was repeated")
        self.attribution = attribution
        self.injections += 1
        injected = _inject_known_wire_failure(
            source,
            binding_index=binding_index,
            export_variable=export_variable,
        )
        try:
            return self.original(injected)
        except BaseException as error:
            self.observed_error = error
            raise


def _run_real_semantic_acceptance(
    config: RuntimeConfig,
    source_root: Path,
    run_dir: Path,
    *,
    preflight: ZupStaticPreflight,
    iterations: int,
    warmup_iterations: int,
) -> SemanticAcceptanceResult:
    """Exercise real full-module semantics before performance compatibility.

    The approved source bundle and catalog arrive from the static preflight and
    remain in memory.  Production builders necessarily stage transformed source
    while creating EPFs, so their workspace is a single run-owned private
    directory that is removed before this function returns or raises.
    """

    if preflight.inventory:
        raise ProtocolError("ZUP real semantic acceptance requires exact preflight")
    if (
        preflight.source_bundle is None
        or preflight.catalog is None
        or len(preflight.units) != 2
        or len(preflight.analyses) != 2
    ):
        raise ProtocolError("ZUP real semantic preflight is incomplete")
    source_names = {item.name for item in preflight.source_bundle.units}
    if source_names != {_PRIMARY_MODULE, _EXTENDED_MODULE}:
        raise ProtocolError("ZUP real semantic source identity is incomplete")
    if not Path(source_root).is_dir():
        raise ProtocolError("ZUP real semantic source root changed after preflight")

    owned_run_dir = Path(run_dir).resolve()
    private_root = (owned_run_dir / ".private-semantic").resolve()
    if private_root.parent != owned_run_dir or private_root.name != ".private-semantic":
        raise ProtocolError("ZUP private semantic workspace is unsafe")
    if private_root.exists():
        raise ProtocolError("ZUP private semantic workspace is not fresh")
    private_root.mkdir(parents=True)

    live_config = replace(
        config,
        workspace=private_root,
        infobase_path=Path(config.infobase_dir),
    )
    catalog = preflight.catalog
    if catalog is None:
        raise ProtocolError("ZUP real semantic catalog is unavailable")
    required_modules = {_PRIMARY_MODULE.casefold(), _EXTENDED_MODULE.casefold()}
    available_modules = {item.canonical_name.casefold() for item in catalog.modules}
    if not required_modules <= available_modules:
        raise ProtocolError("ZUP acceptance catalog modules are missing")
    g17_units = _acceptance_units(
        preflight,
        extended_revision=17,
        extended_value=17,
    )
    g18_units = _acceptance_units(
        preflight,
        extended_revision=18,
        extended_value=18,
    )
    g19_units = _acceptance_units(
        preflight,
        extended_revision=19,
        extended_value=19,
    )
    if not (
        g17_units[0].mapped_source.text == g18_units[0].mapped_source.text
        == g19_units[0].mapped_source.text
        and g17_units[0].revision == g18_units[0].revision == g19_units[0].revision
    ):
        raise ProtocolError("ZUP unchanged-module incremental identity is invalid")

    session: RuntimeSession | None = None
    catalog_setup: _CatalogSetupObservation | None = None
    catalog_source_root: Path | None = None
    accounting: IncrementalAccounting | None = None
    catalog_extension: Mapping[str, object] | None = None
    checks = {name: False for name in SEMANTIC_PASS_GATES}
    active_error: BaseException | None = None
    parser_checkpoint = active_parser_acceptance_checkpoint()
    _write_atomic_json(
        owned_run_dir,
        "parser-checkpoint-preflight.json",
        parser_checkpoint,
    )
    try:
        catalog_source_root = _mirror_common_module_metadata(
            source_root,
            private_root,
        )
        packaged = packaged_extension_bundle(live_config.runtime_dir)
        installed_dump = private_root / "installed-extension"
        dump_target_extension_files(
            live_config,
            installed_dump,
            private_root / "installed-extension.log",
        )
        if (
            fingerprint_extension_dump(installed_dump, expected_product_id=None)
            != packaged.manifest.fingerprints
        ):
            raise ProtocolError("ZUP target extension identity is not exact")
        if _matching_target_process_count(Path(config.infobase_dir)):
            raise ProtocolError("ZUP target session is not fresh after identity check")
        session = RuntimeSession.start(
            RuntimeSessionConfig(
                live_config,
                private_root / "evidence",
                source_root=catalog_source_root,
            )
        )
        runtime_checkpoint = require_fresh_parser_runtime(session, parser_checkpoint)
        _write_atomic_json(
            owned_run_dir,
            "parser-checkpoint-runtime.json",
            runtime_checkpoint,
        )
        _bind_catalog_read_audit(session, catalog_source_root)

        # Ordinary notebook semantics run before the full-module universe so
        # procedure/function/mixed-cell behavior is independently observable.
        _require_reply(
            session.execute_bsl(_MAIN_PROCEDURE_CELL),
            kinds=(RuntimeReplyKind.WORKER_LOADED,),
        )
        _require_reply(
            session.execute_bsl(
                "Task10MainProcedureState = Новый Структура;\n"
                "__OnecTask10NotebookProcedure(Task10MainProcedureState, 6);\n"
                "Результат = Task10MainProcedureState.Значение;"
            ),
            kinds=(RuntimeReplyKind.MAIN_COMPLETED,),
            expected=7,
        )
        _require_reply(
            session.execute_bsl(_MAIN_FUNCTION_CELL),
            kinds=(RuntimeReplyKind.WORKER_LOADED,),
        )
        _require_reply(
            session.execute_bsl(
                "Результат = __OnecTask10NotebookFunction(6);"
            ),
            kinds=(RuntimeReplyKind.MAIN_COMPLETED,),
            expected=12,
        )
        _require_reply(
            session.execute_bsl(_MAIN_MIXED_CELL),
            kinds=(RuntimeReplyKind.MAIN_COMPLETED,),
            expected=7,
        )
        _require_reply(
            session.execute_bsl(
                'Task10MainProxy = Новый Структура("Статус,Значение", '
                '"PASS", 17);\nРезультат = Истина;'
            ),
            kinds=(RuntimeReplyKind.MAIN_COMPLETED,),
            expected=True,
        )
        if _materialize_proxy(session, "Task10MainProxy") != {
            "Статус": "PASS",
            "Значение": 17,
        }:
            raise ProtocolError("ZUP MAIN proxy serialization failed")
        compile_source, compile_marker = _compile_diagnostic_probe()
        compile_unit = SourceUnitRef(
            SourceUnitKind.NOTEBOOK_CELL,
            "task10-compile-diagnostic",
            17,
            source_sha256(compile_source),
        )
        compile_diagnostic = _require_real_failure(
            lambda: session.execute_bsl(compile_source, source_unit=compile_unit)
        )
        _verify_compile_diagnostic_contract(
            compile_diagnostic,
            source=compile_source,
            source_unit=compile_unit,
            marker=compile_marker,
        )

        # A reload performed while CAPTURE is paused belongs to the next user
        # operation.  Each loader operation proves its original generation pin
        # remains unchanged; the following operation then pins and exercises
        # the promoted notebook Worker generation.
        capture_location = _synthetic_capture_location(live_config.runtime_dir)
        session.configure_capture_points((capture_location,))
        _exercise_capture_reload_visibility(
            session,
            capture_location,
            worker_source=_CAPTURE_PROCEDURE_CELL,
            probe_source=(
                "Task10CaptureProcedureState = Новый Структура;\n"
                "__OnecTask10CaptureProcedure("
                "Task10CaptureProcedureState, 12);\n"
                "РезультатИнструкции = "
                "Task10CaptureProcedureState.Значение;"
            ),
            probe_expected=17,
            phase=11,
        )
        _exercise_capture_reload_visibility(
            session,
            capture_location,
            worker_source=_CAPTURE_FUNCTION_CELL,
            probe_source=(
                "РезультатИнструкции = __OnecTask10CaptureFunction(6);"
            ),
            probe_expected=18,
            phase=12,
        )
        _exercise_capture_reload_visibility(
            session,
            capture_location,
            worker_source=_CAPTURE_MIXED_CELL,
            worker_kinds=(RuntimeReplyKind.CAPTURE_CELL,),
            worker_expected=20,
            probe_source=(
                "Task10CaptureMixedProbeState = Новый Структура;\n"
                "__OnecTask10CaptureMixed(Task10CaptureMixedProbeState, 10);\n"
                "РезультатИнструкции = "
                "Task10CaptureMixedProbeState.Значение;"
            ),
            probe_expected=20,
            phase=13,
        )
        _exercise_capture_error_reload_visibility(
            session,
            capture_location,
            phase=14,
        )
        checks["serialization"] = True

        # Loading these units runs the production full-catalog analyzer,
        # transformer, packager and atomic target promotion path.
        first_load_profile = PhaseRecorder()
        g17 = session.load_worker_modules(g17_units, profiler=first_load_profile)
        catalog_setup = _catalog_setup_from_first_load(session, first_load_profile)
        _require_matching_catalog_snapshots(catalog, catalog_setup.snapshot)
        _require_reply(
            session.execute_bsl(
                f"Результат = {_PRIMARY_MODULE}.__OnecTask10CrossCall();"
            ),
            kinds=(RuntimeReplyKind.MAIN_COMPLETED,),
            expected=1717,
        )
        _require_reply(
            session.execute_bsl(
                f"Результат = {_PRIMARY_MODULE}.__OnecTask10Function(5);"
            ),
            kinds=(RuntimeReplyKind.MAIN_COMPLETED,),
            expected=22,
        )
        _require_reply(
            session.execute_bsl(
                "Task10ModuleProcedureState = Новый Структура;\n"
                f"{_PRIMARY_MODULE}.__OnecTask10Procedure("
                "Task10ModuleProcedureState);\n"
                "Результат = Task10ModuleProcedureState.Значение;"
            ),
            kinds=(RuntimeReplyKind.MAIN_COMPLETED,),
            expected=17,
        )
        _require_worker_proxy_privacy(session)
        checks["privacy"] = True
        _require_reply(
            session.execute_bsl(
                f"Результат = {_PRIMARY_MODULE}.__OnecTask10ValueReference();"
            ),
            kinds=(RuntimeReplyKind.MAIN_COMPLETED,),
            expected=True,
        )
        _require_reply(
            session.execute_bsl(
                f"Результат = {_PRIMARY_MODULE}.__OnecTask10SameName();"
            ),
            kinds=(RuntimeReplyKind.MAIN_COMPLETED,),
            expected=1017,
        )
        _require_reply(
            session.execute_bsl(
                f"Результат = {_EXTENDED_MODULE}.__OnecTask10SameName();"
            ),
            kinds=(RuntimeReplyKind.MAIN_COMPLETED,),
            expected=17,
        )
        checks["main"] = True

        session.configure_capture_points((capture_location,))
        captured = _require_reply(
            session.execute_bsl(
                f"Task10BeforeCapture = {_PRIMARY_MODULE}."
                "__OnecTask10CrossCall();\n"
                "Task10SyntheticCapture = RuntimeKernelServer."
                "СинтетическийCapture(100);\n"
                f"Task10AfterCapture = {_PRIMARY_MODULE}."
                "__OnecTask10CrossCall();\n"
                + _capture_pair_result_source(
                    "Task10BeforeCapture",
                    "Task10AfterCapture",
                )
            ),
            kinds=(RuntimeReplyKind.CAPTURED,),
        )
        if captured.location != capture_location:
            raise ProtocolError("ZUP CAPTURE location identity failed")
        if session.runtime_api.operation_worker_generation is not g17:
            raise ProtocolError("ZUP CAPTURE did not pin the original generation")

        _g18, g19, accounting = _exercise_captured_two_promotion_lifecycle(
            session,
            g17=g17,
            g18_units=g18_units,
            g19_units=g19_units,
        )
        checks["capture"] = True

        runtime_diagnostic = _require_real_failure(
            lambda: session.execute_bsl(
                f"Результат = {_PRIMARY_MODULE}.__OnecTask10CallThrow();"
            )
        )
        units_by_name = {unit.logical_name: unit for unit in g19_units}
        _verify_runtime_diagnostic_contract(
            runtime_diagnostic,
            units=(
                (
                    units_by_name[_EXTENDED_MODULE].mapped_source.source_map.segments[
                        0
                    ].origin_ref,
                    units_by_name[_EXTENDED_MODULE].mapped_source.text,
                ),
                (
                    units_by_name[_PRIMARY_MODULE].mapped_source.source_map.segments[
                        0
                    ].origin_ref,
                    units_by_name[_PRIMARY_MODULE].mapped_source.text,
                ),
            ),
            markers=(
                'ВызватьИсключение "task10-runtime-diagnostic"',
                f"Возврат {_EXTENDED_MODULE}.__OnecTask10Throw()",
            ),
        )
        checks["diagnostics"] = True
        _require_reply(
            session.execute_bsl(
                f"Результат = {_PRIMARY_MODULE}.__OnecTask10CrossCall();"
            ),
            kinds=(RuntimeReplyKind.MAIN_COMPLETED,),
            expected=1719,
        )
        failed_unit = _known_wire_failure_unit(g19_units, catalog)
        before_failure = _failure_cleanup_snapshot(session)
        target = session.runtime_api._worker_universe_target
        registrations_before_failure = dict(target._registrations)
        failure_executor = _KnownWireFailureExecutor(target)
        target._instruction_executor = failure_executor
        try:
            try:
                session.load_worker_modules(
                    (failed_unit, g19_units[1]),
                )
            except WorkerPromotionOutcomeUnknown as error:
                _verify_known_wire_failure(error, failure_executor.attribution)
            except BslExecutionError as error:
                _verify_known_wire_failure(error, failure_executor.attribution)
                if failure_executor.observed_error is None:
                    raise ProtocolError(
                        "ZUP known wire failure was not observed at the target"
                    )
                _verify_known_wire_failure(
                    failure_executor.observed_error,
                    failure_executor.attribution,
                )
            else:
                raise ProtocolError("ZUP known wire failure did not occur")
        finally:
            target._instruction_executor = failure_executor.original
        if failure_executor.injections != 1:
            raise ProtocolError("ZUP known wire failure injection count is invalid")
        if _failure_cleanup_snapshot(session) != before_failure:
            raise ProtocolError("ZUP known wire failure cleanup is incomplete")
        registrations_after_failure = dict(target._registrations)
        retained_failed_registrations = (
            set(registrations_after_failure) - set(registrations_before_failure)
        )
        if len(retained_failed_registrations) != 1:
            raise ProtocolError(
                "ZUP known wire failure registration retention is invalid"
            )
        retained_registration = retained_failed_registrations.pop()
        retained_owner = registrations_after_failure.get(retained_registration)
        if (
            retained_owner is None
            or retained_owner[0] != _PRIMARY_MODULE.casefold()
            or retained_registration
            not in target.privacy_registration_snapshot()
        ):
            raise ProtocolError(
                "ZUP known wire failure privacy ledger is incomplete"
            )
        if session.runtime_api._worker_universe.active_handle is not g19:
            raise ProtocolError("ZUP active G19 changed after known wire failure")
        _require_reply(
            session.execute_bsl(
                f"Результат = {_PRIMARY_MODULE}.__OnecTask10CrossCall();"
            ),
            kinds=(RuntimeReplyKind.MAIN_COMPLETED,),
            expected=1719,
        )
        checks["failure_cleanup"] = True
        try:
            session.release_worker_generation(g17)
        except StaleWorkerGeneration:
            pass
        else:
            raise ProtocolError("ZUP superseded generation did not become stale")
        checks["lifecycle"] = True

        if accounting is None:
            raise ProtocolError("ZUP incremental acceptance was not observed")
        if catalog_setup is None:
            raise ProtocolError("ZUP catalog setup measurement is unavailable")
        session.release_worker_generation(g19)
        phase_samples, parser_call_samples = _collect_mode_samples(
            session,
            g19_units,
            warmups=warmup_iterations,
            iterations=iterations,
        )
        final_measurement_units = _measurement_units(
            g19_units,
            revision=(
                g19_units[1].revision
                + 2 * (warmup_iterations + iterations)
            ),
        )
        catalog_extension = _run_missing_module_extension_gate(
            session,
            catalog_source_root,
            final_measurement_units,
        )
        return SemanticAcceptanceResult(
            admission=_admission_summary(preflight),
            accounting=accounting,
            checks=checks,
            catalog_setup_ms=catalog_setup.elapsed_ms,
            catalog_snapshot=catalog_setup.snapshot,
            phase_samples=phase_samples,
            parser_call_samples=parser_call_samples,
            catalog_extension=catalog_extension,
        )
    except BaseException as error:
        active_error = error
        raise
    finally:
        cleanup_errors: list[BaseException] = []
        if session is not None:
            try:
                session.close()
            except BaseException as error:
                cleanup_errors.append(error)
        try:
            owned_processes = _matching_target_process_count(Path(config.infobase_dir))
            if owned_processes:
                raise ProtocolError("ZUP owned target processes survived cleanup")
        except BaseException as error:
            cleanup_errors.append(error)
        try:
            if private_root.exists():
                shutil.rmtree(private_root)
            if private_root.exists():
                raise ProtocolError("ZUP private semantic workspace survived cleanup")
        except BaseException as error:
            cleanup_errors.append(error)
        if cleanup_errors:
            if active_error is not None:
                for cleanup_error in cleanup_errors:
                    active_error.add_note(
                        "ZUP semantic cleanup failed: "
                        f"{type(cleanup_error).__name__}"
                    )
            else:
                raise ProtocolError(
                    "ZUP real semantic cleanup failed"
                ) from cleanup_errors[0]


def _run_verified_live(
    config: RuntimeConfig,
    source_root: Path,
    *,
    iterations: int,
    warmup_iterations: int,
    preflight: ZupStaticPreflight,
    run_dir: Path,
    _semantic_runner: Callable[..., SemanticAcceptanceResult] | None = None,
) -> Mapping[str, object]:
    runner = _semantic_runner or _run_real_semantic_acceptance
    semantic = runner(
        config,
        source_root,
        Path(run_dir),
        preflight=preflight,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
    )
    if not isinstance(semantic, SemanticAcceptanceResult):
        raise ProtocolError("ZUP real semantic acceptance result is invalid")
    admission = semantic.admission
    gates = semantic.gates()
    if not isinstance(admission, Mapping):
        raise ProtocolError("ZUP real semantic acceptance gates failed")
    if (
        semantic.catalog_setup_ms is None
        or semantic.catalog_snapshot is None
        or semantic.phase_samples is None
        or semantic.parser_call_samples is None
        or semantic.catalog_extension is None
    ):
        raise ProtocolError("ZUP measured live acceptance result is incomplete")
    reference = ReferenceObservation(
        **_exact_reference_summary(preflight, semantic.catalog_snapshot)
    )
    candidate = _build_compact_evidence_candidate(
        reference=reference,
        warmup_iterations=warmup_iterations,
        measured_iterations=iterations,
        catalog_setup_ms=semantic.catalog_setup_ms,
        phase_samples=semantic.phase_samples,
        parser_call_samples=semantic.parser_call_samples,
        accounting=semantic.accounting,
        catalog_extension=semantic.catalog_extension,
        admission=admission,
        gates=gates,
    )
    try:
        verified = verify_compact_evidence(candidate)
    except ProtocolError as error:
        if error.args != ("ZUP measured SLA gate failed",):
            raise
        _persist_nonpass_measurement(Path(run_dir), candidate)
        raise
    _persist_pass_measurement(Path(run_dir), verified)
    return verified


def run_worker_universe_zup_acceptance(
    config: RuntimeConfig,
    *,
    iterations: int = 60,
    warmup_iterations: int = 3,
    source_root: Path | None,
    _live_runner: Callable[..., Mapping[str, object]] | None = None,
) -> Path:
    if (
        type(iterations) is not int
        or iterations <= 0
        or type(warmup_iterations) is not int
        or warmup_iterations < 0
    ):
        raise ValueError("ZUP acceptance iteration budget is invalid")
    artifacts = ArtifactWriter(
        config.artifacts_dir,
        "worker-universe-zup-acceptance",
    )
    preflight = inspect_zup_static_preflight(config, source_root)
    inventory = list(preflight.inventory)
    if inventory:
        payload = _terminal_payload(
            inventory,
            warmup_iterations=warmup_iterations,
            measured_iterations=iterations,
        )
    else:
        if _live_runner is None:
            payload = dict(
                _run_verified_live(
                    config,
                    Path(source_root),
                    iterations=iterations,
                    warmup_iterations=warmup_iterations,
                    preflight=preflight,
                    run_dir=artifacts.run_dir,
                )
            )
        else:
            payload = dict(
                _live_runner(
                    config,
                    Path(source_root),
                    iterations=iterations,
                    warmup_iterations=warmup_iterations,
                )
            )
    verified = verify_compact_evidence(payload)
    artifacts.write_json("summary.json", verified)
    return artifacts.run_dir


def verify_worker_universe_zup_evidence(run_dir: Path) -> dict[str, object]:
    path = Path(run_dir) / "summary.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ProtocolError("ZUP acceptance evidence is unavailable") from error
    return verify_compact_evidence(payload)
