"""Cold first-load benchmark for the minimal Worker module projection.

The parent process owns only scheduling and privacy-safe checkpoints.  Parse
microgates and every live end-to-end sample run in a distinct Python child.
"""

from __future__ import annotations

import argparse
from base64 import b64decode
from binascii import Error as Base64DecodeError
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
from secrets import token_bytes
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import perf_counter_ns, time_ns
from typing import Any


SCHEMA = "onec-minimal-worker-reload-benchmark-v2"
CHECKPOINT_SCHEMA = "onec-minimal-worker-reload-checkpoint-v2"
_REFERENCE_PLATFORM_VERSION = "8.3.27.2170"
_REFERENCE_PLATFORM_SHA256 = (
    "01eb37ac23e5bb25a4359665c01abf9a178b5066e78a198997b197126456b6f2"
)
_REFERENCE_INFOBASE_PATH_IDENTITY_SHA256 = (
    "081f7a40d3343d923d60954a5f08f52bee98c5de13dc3b5374c8cc2485c7282b"
)
_REFERENCE_EXTENSION_SHA256 = (
    "a29e51f4e83ab96a958fef9926b788ebe61582d91132bf41a298c4bffa5358ff"
)
_REFERENCE_RAW_SOURCES = (
    (
        "КадровыйУчет",
        "378f23bcb775aaeddc2c664ff77717a611add2c81b9b5a63d83cff2354a308b6",
        831_889,
        11_103,
    ),
    (
        "КадровыйУчетРасширенный",
        "ea83a38b89304dc33521cfa5c7758380481dcc61db6fa6497e5de13c81b1fd72",
        1_981_297,
        24_591,
    ),
)
_REFERENCE_MEASURED_UNITS = (
    (
        "КадровыйУчет",
        "fbc31dfbd7422c2e5ff1bdcea4a5740f0e1db9871243b0eb09a83d9427f4190f",
        833_133,
        11_129,
    ),
    (
        "КадровыйУчетРасширенный",
        "6294f11586c29f04ac37e770c6410b6bd80b99ce4a95917813fac967d9541f96",
        1_981_851,
        24_607,
    ),
)
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
_WINDOWS_PATH_RE = re.compile(r"(?i)(?:[a-z]:[\\/]|\\\\[^\\\s]+\\)")
_POSIX_PATH_RE = re.compile(r"(?:^|[\s=\"'])/(?!/)[^\s\"']+")
_UUID_RE = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}\b"
)
_SOURCE_RE = re.compile(
    r"(?i)(?:^|\s)(?:процедура|функция|конецпроцедуры|конецфункции|"
    r"procedure|function|результат\s*=|возврат\s+)"
)
_PRIVATE_KEYS = frozenset(
    {
        "password",
        "token",
        "source_text",
        "source_path",
        "source_root",
        "infobase_path",
        "workspace_path",
        "command_line",
        "credential",
        "diagnostic",
        "pid",
        "secret",
        "stderr",
        "stdout",
        "username",
        "uuid",
    }
)


class BenchmarkContractError(ValueError):
    """A privacy-safe benchmark contract violation."""


@dataclass(frozen=True, slots=True)
class ColdSampleRequest:
    sequence: int
    mode: str
    mode_ordinal: int
    warmup: bool = False


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def _source_inventory(
    values: Sequence[tuple[str, str, int, int]],
) -> list[dict[str, object]]:
    return [
        {
            "logical_name": name,
            "sha256": source_hash,
            "bytes": byte_count,
            "lines": line_count,
        }
        for name, source_hash, byte_count, line_count in values
    ]


def benchmark_reference(database_file_identity_sha256: str) -> dict[str, object]:
    """Return the benchmark-owned, privacy-safe frozen live reference."""
    if (
        not isinstance(database_file_identity_sha256, str)
        or _HASH_RE.fullmatch(database_file_identity_sha256) is None
    ):
        raise BenchmarkContractError("target database identity is invalid")
    return {
        "platform_version": _REFERENCE_PLATFORM_VERSION,
        "platform_sha256": _REFERENCE_PLATFORM_SHA256,
        "infobase_path_identity_sha256": (
            _REFERENCE_INFOBASE_PATH_IDENTITY_SHA256
        ),
        "database_file_identity_sha256": database_file_identity_sha256,
        "extension_sha256": _REFERENCE_EXTENSION_SHA256,
        "raw_sources": _source_inventory(_REFERENCE_RAW_SOURCES),
        "measured_units": _source_inventory(_REFERENCE_MEASURED_UNITS),
    }


def benchmark_implementation_identity() -> dict[str, object]:
    """Bind a run to its schemas, generated parser and executed source bytes."""
    repository = Path(__file__).resolve().parents[1]
    integration_harness_paths = (
        repository / "integration/support/zup_sources.py",
        repository / "integration/zup_worker_universe_acceptance.py",
    )
    paths = tuple(
        sorted(
            (
                *repository.joinpath("src", "onec_runtime").rglob("*.py"),
                *repository.joinpath("grammar").glob("*.grammar"),
                *repository.joinpath("grammar").glob("*.semantic"),
                *integration_harness_paths,
                Path(__file__).resolve(),
            ),
            key=lambda item: item.relative_to(repository).as_posix(),
        )
    )
    digest = sha256()
    for path in paths:
        relative = path.relative_to(repository).as_posix()
        try:
            payload = path.read_bytes()
        except OSError as error:
            raise BenchmarkContractError(
                "benchmark implementation identity is unavailable"
            ) from error
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha256(payload).digest())
    harness_digest = sha256()
    for path in integration_harness_paths:
        relative = path.relative_to(repository).as_posix()
        try:
            payload = path.read_bytes()
        except OSError as error:
            raise BenchmarkContractError(
                "benchmark implementation identity is unavailable"
            ) from error
        harness_digest.update(relative.encode("utf-8"))
        harness_digest.update(b"\0")
        harness_digest.update(sha256(payload).digest())

    try:
        python_payload = Path(sys.executable).read_bytes()
    except OSError as error:
        raise BenchmarkContractError(
            "benchmark interpreter identity is unavailable"
        ) from error

    from onec_runtime.bsl.full_ast_worker_projection import full_ast_parser_identity

    parser_identity, parsergen_package = full_ast_parser_identity()
    schema_material = {
        "benchmark_schema": SCHEMA,
        "checkpoint_schema": CHECKPOINT_SCHEMA,
        "cold_phase_calls": _COLD_PHASE_CALLS,
        "cold_phase_item_counts": _COLD_PHASE_ITEM_COUNTS,
    }
    return {
        "benchmark_schema": SCHEMA,
        "checkpoint_schema": CHECKPOINT_SCHEMA,
        "schema_identity_sha256": _canonical_sha256(schema_material),
        "implementation_sha256": digest.hexdigest(),
        "implementation_file_count": len(paths),
        "integration_harness_sha256": harness_digest.hexdigest(),
        "integration_harness_file_count": len(integration_harness_paths),
        "python_implementation": sys.implementation.name,
        "python_full_version": sys.version,
        "python_executable_sha256": sha256(python_payload).hexdigest(),
        "active_parser_identity_sha256": parser_identity,
        "parsergen_package_sha256": parsergen_package,
    }


def measured_unit_inventory(units: Sequence[object]) -> list[dict[str, object]]:
    """Describe the exact augmented UTF-8 text passed to the timed API."""
    inventory: list[dict[str, object]] = []
    for unit in units:
        try:
            name = unit.logical_name
            text = unit.mapped_source.text
        except AttributeError:
            raise BenchmarkContractError("measured Worker unit is invalid") from None
        if not isinstance(name, str) or not name or not isinstance(text, str):
            raise BenchmarkContractError("measured Worker unit is invalid")
        payload = text.encode("utf-8")
        inventory.append(
            {
                "logical_name": name,
                "sha256": sha256(payload).hexdigest(),
                "bytes": len(payload),
                "lines": len(text.splitlines()),
            }
        )
    return inventory


def build_cold_schedule(
    cold_reloads: int,
    modes: Sequence[str],
) -> tuple[ColdSampleRequest, ...]:
    """Return deterministic AB/BA pairs with one cold sample per mode."""
    normalized = tuple(str(mode).upper() for mode in modes)
    if (
        type(cold_reloads) is not int
        or cold_reloads <= 0
        or normalized != ("MAIN", "CAPTURE")
    ):
        raise BenchmarkContractError("cold schedule configuration is invalid")
    schedule: list[ColdSampleRequest] = []
    sequence = 0
    for pair_index in range(cold_reloads):
        order = normalized if pair_index % 2 == 0 else tuple(reversed(normalized))
        for mode in order:
            sequence += 1
            schedule.append(
                ColdSampleRequest(
                    sequence=sequence,
                    mode=mode,
                    mode_ordinal=pair_index + 1,
                )
            )
    return tuple(schedule)


def aggregate_phase_events(events: Sequence[object]) -> dict[str, dict[str, int | float]]:
    """Aggregate repeated per-module PhaseEvents without overwriting either."""
    aggregate: dict[str, dict[str, int]] = {}
    for event in events:
        phase = getattr(event, "phase", None)
        if not isinstance(phase, str) or not phase:
            raise BenchmarkContractError("phase event is invalid")
        values = {
            "wall_ns": getattr(event, "wall_ns", None),
            "cpu_ns": getattr(event, "cpu_ns", None),
            "input_bytes": getattr(event, "input_bytes", None),
            "output_bytes": getattr(event, "output_bytes", None),
            "item_count": getattr(event, "item_count", None),
        }
        if any(type(value) is not int or value < 0 for value in values.values()):
            raise BenchmarkContractError("phase event is invalid")
        bucket = aggregate.setdefault(
            phase,
            {
                "calls": 0,
                "wall_ns": 0,
                "cpu_ns": 0,
                "input_bytes": 0,
                "output_bytes": 0,
                "item_count": 0,
            },
        )
        bucket["calls"] += 1
        for name, value in values.items():
            bucket[name] += value
    return {
        phase: {
            "calls": values["calls"],
            "wall_ms": values["wall_ns"] / 1_000_000,
            "cpu_ms": values["cpu_ns"] / 1_000_000,
            "input_bytes": values["input_bytes"],
            "output_bytes": values["output_bytes"],
            "item_count": values["item_count"],
        }
        for phase, values in sorted(aggregate.items())
    }


def nearest_rank_summary(samples_ms: Sequence[float]) -> dict[str, int | float]:
    values = [float(value) for value in samples_ms]
    if not values or any(not math.isfinite(value) or value < 0 for value in values):
        raise BenchmarkContractError("timing samples are invalid")
    ordered = sorted(values)

    def percentile(fraction: float) -> float:
        return ordered[math.ceil(len(ordered) * fraction) - 1]

    return {
        "count": len(values),
        "first_ms": values[0],
        "p50_ms": percentile(0.50),
        "p95_ms": percentile(0.95),
        "max_ms": ordered[-1],
    }


def measure_parse_batch(
    batch_index: int,
    iterations: int,
    sources: tuple[str, str],
    *,
    parse_one: Callable[[str], object],
    clock_ns: Callable[[], int] = perf_counter_ns,
    process_identity_sha256: str,
) -> dict[str, object]:
    """Warm a fresh parser process once, then time complete two-module parses."""
    if (
        type(batch_index) is not int
        or batch_index <= 0
        or type(iterations) is not int
        or iterations <= 0
        or type(sources) is not tuple
        or len(sources) != 2
        or any(not isinstance(source, str) or not source for source in sources)
        or _HASH_RE.fullmatch(process_identity_sha256) is None
    ):
        raise BenchmarkContractError("parse batch configuration is invalid")
    for source in sources:
        parse_one(source)
    samples_ms: list[float] = []
    for _ in range(iterations):
        started = clock_ns()
        for source in sources:
            parse_one(source)
        samples_ms.append((clock_ns() - started) / 1_000_000)
    return {
        "batch_index": batch_index,
        "process_identity_sha256": process_identity_sha256,
        "warmup_iterations": 1,
        "measured_iterations": iterations,
        "samples_ms": samples_ms,
    }


_COLD_PHASE_CALLS = {
    "tokenize": 2,
    "server_token_filter": 2,
    "full_ast_generated_parse": 2,
    "full_ast_model_extract": 2,
    "semantic_parse": 2,
    "ast_model_extract": 2,
    "catalog_validation": 1,
    "dependency_analysis": 2,
    "resolved_analysis_adapter": 2,
    "alias_transform": 2,
    "source_map_composition": 2,
    "admission": 2,
    "epf_packaging": 2,
    "artifact_stage_sealed_validation": 1,
    "artifact_stage_base64": 1,
    "artifact_stage_executor": 1,
    "artifact_stage_batch": 1,
    "artifact_staging": 1,
    "generation_create_wire_probe": 1,
    "root_swap": 1,
    "end_to_end": 1,
}
_COLD_PHASE_ITEM_COUNTS = {
    "epf_packaging": 2,
    "artifact_stage_sealed_validation": 2,
    "artifact_stage_base64": 2,
    "artifact_stage_executor": 2,
    "artifact_stage_batch": 2,
    "artifact_staging": 2,
    "generation_create_wire_probe": 1,
    "root_swap": 1,
    "end_to_end": 2,
}
_DRILL_DOWN_PHASES = frozenset(
    {
        "full_ast_generated_parse",
        "full_ast_model_extract",
        "artifact_stage_sealed_validation",
        "artifact_stage_batch",
        "artifact_stage_base64",
        "artifact_stage_executor",
    }
)
_DERIVED_SUBTOTALS = frozenset({"runtime_transport_promotion"})
_TOP_LEVEL_PHASES = tuple(
    phase
    for phase in _COLD_PHASE_CALLS
    if phase != "end_to_end" and phase not in _DRILL_DOWN_PHASES
)
_PHASE_EVIDENCE_FIELDS = frozenset(
    {
        "calls",
        "wall_ms",
        "cpu_ms",
        "input_bytes",
        "output_bytes",
        "item_count",
    }
)


def _cold_phases_are_well_formed(
    phases: Mapping[str, Mapping[str, object]],
) -> bool:
    if set(phases) != set(_COLD_PHASE_CALLS):
        return False
    for phase, expected_calls in _COLD_PHASE_CALLS.items():
        evidence = phases.get(phase)
        if (
            not isinstance(evidence, Mapping)
            or set(evidence) != _PHASE_EVIDENCE_FIELDS
        ):
            return False
        if (
            type(evidence.get("calls")) is not int
            or evidence["calls"] != expected_calls
            or any(
                type(evidence.get(field)) is not int or evidence[field] < 0
                for field in ("input_bytes", "output_bytes", "item_count")
            )
            or any(
                type(evidence.get(field)) not in (int, float)
                or not math.isfinite(float(evidence[field]))
                or float(evidence[field]) < 0
                for field in ("wall_ms", "cpu_ms")
            )
        ):
            return False
    return all(
        phases[name]["item_count"] == count
        for name, count in _COLD_PHASE_ITEM_COUNTS.items()
    )


def validate_cold_phase_contract(
    phases: Mapping[str, Mapping[str, object]],
    parser_calls: Mapping[str, object],
) -> None:
    """Require the uncached two-module product path, including all duplicates."""
    if not _cold_phases_are_well_formed(phases):
        raise BenchmarkContractError("cold phase contract does not match")
    expected_parser_calls = {
        "full_module_parses": 2,
        "delta_method_parses": 0,
        "packaging_validation_parses": 0,
        "worker_profile_parses": 0,
    }
    if set(parser_calls) != set(expected_parser_calls) or any(
        type(parser_calls[name]) is not int
        or parser_calls[name] != expected
        for name, expected in expected_parser_calls.items()
    ):
        raise BenchmarkContractError("cold parser phase contract does not match")


def cold_phase_accounting(
    phases: Mapping[str, Mapping[str, object]],
    *,
    end_to_end_ms: object,
) -> dict[str, float]:
    """Return a disjoint decomposition plus a separately labelled subtotal."""
    if (
        not _cold_phases_are_well_formed(phases)
        or type(end_to_end_ms) not in (int, float)
        or not math.isfinite(float(end_to_end_ms))
        or end_to_end_ms < 0
    ):
        raise BenchmarkContractError("cold phase accounting is invalid")
    wall = {phase: float(evidence["wall_ms"]) for phase, evidence in phases.items()}
    tolerance_ms = 1e-9
    if (
        wall["full_ast_generated_parse"]
        > wall["semantic_parse"] + tolerance_ms
        or wall["full_ast_model_extract"]
        > wall["ast_model_extract"] + tolerance_ms
        or wall["artifact_stage_batch"]
        > wall["artifact_staging"] + tolerance_ms
        or wall["artifact_stage_base64"] + wall["artifact_stage_executor"]
        > wall["artifact_stage_batch"] + tolerance_ms
    ):
        raise BenchmarkContractError("cold phase accounting overlaps")
    component_ms = sum(wall[phase] for phase in _TOP_LEVEL_PHASES)
    recorded_end_to_end_ms = wall["end_to_end"]
    # Timings originate as integer nanoseconds.  This tolerance only absorbs
    # binary-float summation after the independent conversions to milliseconds.
    if (
        component_ms > recorded_end_to_end_ms + tolerance_ms
        or component_ms + wall["artifact_stage_sealed_validation"]
        > recorded_end_to_end_ms + tolerance_ms
        or recorded_end_to_end_ms > float(end_to_end_ms) + tolerance_ms
    ):
        raise BenchmarkContractError("cold phase accounting is impossible")
    host_exclusive_ms = recorded_end_to_end_ms - component_ms
    return {
        "host_orchestration_exclusive_ms": max(0.0, host_exclusive_ms),
        "runtime_transport_promotion_ms": sum(
            wall[phase]
            for phase in (
                "artifact_staging",
                "generation_create_wire_probe",
                "root_swap",
            )
        ),
    }


def validate_cold_transport_contract(transport: Mapping[str, object]) -> None:
    """Require two redacted artifact proofs inside one observed batch call."""
    expected_fields = {
        "artifact_count",
        "batch_count",
        "executor_count",
        "artifact_bytes",
        "base64_chars",
        "artifacts",
        "batches",
        "target_filesystem_dependency",
    }
    artifacts = transport.get("artifacts")
    batches = transport.get("batches")
    if (
        set(transport) != expected_fields
        or transport.get("artifact_count") != 2
        or type(transport.get("artifact_count")) is not int
        or transport.get("batch_count") != 1
        or type(transport.get("batch_count")) is not int
        or transport.get("executor_count") != 1
        or type(transport.get("executor_count")) is not int
        or transport.get("target_filesystem_dependency") is not False
        or not isinstance(artifacts, list)
        or len(artifacts) != 2
        or not isinstance(batches, list)
        or len(batches) != 1
    ):
        raise BenchmarkContractError("cold transport contract does not match")
    batch = batches[0]
    expected_batch = {
        "batch_ordinal": 1,
        "batch_count": 1,
        "artifact_count": 2,
        "executor_count": 1,
    }
    if (
        not isinstance(batch, Mapping)
        or set(batch) != set(expected_batch)
        or any(
            type(batch.get(field)) is not int or batch[field] != expected
            for field, expected in expected_batch.items()
        )
    ):
        raise BenchmarkContractError("cold transport contract does not match")
    for proof in artifacts:
        if (
            not isinstance(proof, Mapping)
            or set(proof)
            != {
                "artifact_bytes",
                "base64_chars",
                "target_filesystem_dependency",
            }
            or type(proof.get("artifact_bytes")) is not int
            or proof["artifact_bytes"] <= 0
            or type(proof.get("base64_chars")) is not int
            or proof["base64_chars"]
            != 4 * ((proof["artifact_bytes"] + 2) // 3)
            or proof.get("target_filesystem_dependency") is not False
        ):
            raise BenchmarkContractError("cold transport contract does not match")
    if (
        type(transport.get("artifact_bytes")) is not int
        or transport["artifact_bytes"]
        != sum(int(proof["artifact_bytes"]) for proof in artifacts)
        or type(transport.get("base64_chars")) is not int
        or transport["base64_chars"]
        != sum(int(proof["base64_chars"]) for proof in artifacts)
    ):
        raise BenchmarkContractError("cold transport contract does not match")


def assert_fresh_worker_state(session: object) -> dict[str, int | bool]:
    """Fail closed unless all session, API, host and target Worker caches are empty."""
    try:
        catalog = session._require_common_module_catalog()
        api = session.runtime_api
        index = catalog._path_index
        state = {
            "catalog_initialized": bool(catalog.initialized),
            "catalog_index_entries": 0 if index is None else len(index),
            "catalog_resolved_entries": len(catalog._resolved),
            "active_models": len(api._worker_active_modules),
            "descriptor_artifacts": len(api._worker_module_artifacts),
            "binary_artifacts": len(api._worker_module_builder._cache._capsules),
            "host_generations": len(api._worker_universe._generations),
            "target_registrations": len(api._worker_universe_target._registrations),
            "active_generation": (
                api._worker_generation_handle is not None
                or api._worker_universe._active_generation is not None
            ),
        }
    except (AttributeError, TypeError):
        raise BenchmarkContractError("Worker state is not inspectable") from None
    if (
        index is not None
        or api._worker_catalog_snapshot is not None
        or any(
        value is not False if isinstance(value, bool) else value != 0
        for value in state.values()
        )
    ):
        raise BenchmarkContractError("Worker session is not cold")
    return state


def validate_stage_instruction(
    instruction: str,
    *,
    artifact_bytes: int,
    private_markers: Sequence[str],
    required_tokens: Sequence[str],
) -> dict[str, int | bool]:
    """Prove target upload is Base64 temporary-storage transport, never a host path."""
    if (
        not isinstance(instruction, str)
        or type(artifact_bytes) is not int
        or artifact_bytes <= 0
        or not required_tokens
        or any(token not in instruction for token in required_tokens)
        or any(marker and marker.casefold() in instruction.casefold() for marker in private_markers)
        or _WINDOWS_PATH_RE.search(instruction)
        or _POSIX_PATH_RE.search(instruction)
    ):
        raise BenchmarkContractError("Worker transport proof failed")
    return {
        "artifact_bytes": artifact_bytes,
        "base64_chars": 4 * ((artifact_bytes + 2) // 3),
        "target_filesystem_dependency": False,
    }


def validate_stage_batch_instruction(
    instruction: str,
    *,
    batch: object,
    batch_count: int,
    private_markers: Sequence[str],
) -> dict[str, list[dict[str, int | bool]]]:
    """Authenticate every payload and registration in one generated batch."""
    from onec_runtime.worker_stage_protocol import (
        WORKER_STAGE_SCHEMA,
        WORKER_STAGE_SCHEMA_VERSION,
        WorkerStageBatch,
        worker_stage_batch_digest,
    )

    if (
        type(batch) is not WorkerStageBatch
        or type(batch_count) is not int
        or batch_count <= 0
        or batch.batch_index >= batch_count
        or not isinstance(instruction, str)
        or any(
            marker and marker.casefold() in instruction.casefold()
            for marker in private_markers
        )
        or _WINDOWS_PATH_RE.search(instruction)
        or _POSIX_PATH_RE.search(instruction)
    ):
        raise BenchmarkContractError("Worker batch transport proof failed")

    entries = batch.entries
    expected_indices = tuple(range(len(entries)))
    payload_fields = tuple(
        (int(index), encoded)
        for index, encoded in re.findall(
            r"ДанныеАртефактаWorker(0|[1-9][0-9]*) = "
            r'Base64Значение\("([A-Za-z0-9+/]+={0,2})"\);',
            instruction,
        )
    )
    connection_fields = tuple(
        (int(index), registration)
        for index, registration in re.findall(
            r"ИмяАртефактаWorker(0|[1-9][0-9]*) = "
            r"ВнешниеОбработки\.Подключить\(АдресАртефактаWorker\1, "
            r'"([A-Za-z_][A-Za-z0-9_]*)", Ложь\);',
            instruction,
        )
    )
    receipt_prefix = (
        '"schema,schema_version,transaction_id,batch_index,batch_count,'
        'batch_digest,status,connected,failure", '
        f'"{WORKER_STAGE_SCHEMA}", {WORKER_STAGE_SCHEMA_VERSION}, '
    )
    receipt_suffix = (
        f", {batch.batch_index}, {batch_count}, "
        f'"{worker_stage_batch_digest(batch, batch_count)}", '
    )
    try:
        decoded_payloads = tuple(
            b64decode(encoded, validate=True) for _, encoded in payload_fields
        )
    except (Base64DecodeError, ValueError):
        raise BenchmarkContractError("Worker batch transport proof failed") from None

    if (
        tuple(index for index, _ in payload_fields) != expected_indices
        or tuple(index for index, _ in connection_fields) != expected_indices
        or decoded_payloads != tuple(entry.artifact_bytes for entry in entries)
        or tuple(registration for _, registration in connection_fields)
        != tuple(entry.registration_name for entry in entries)
        or instruction.count(receipt_prefix) != 1
        or instruction.count(receipt_suffix) != 1
        or len(_UUID_RE.findall(instruction)) != 1
        or any(
            sha256(payload).hexdigest() != entry.artifact_sha256
            for payload, entry in zip(decoded_payloads, entries, strict=True)
        )
        or instruction.count("Base64Значение(") != len(entries)
        or instruction.count("ПоместитьВоВременноеХранилище(") != len(entries)
        or instruction.count("ВнешниеОбработки.Подключить(") != len(entries)
    ):
        raise BenchmarkContractError("Worker batch transport proof failed")

    return {
        "artifacts": [
            {
                "artifact_bytes": len(payload),
                "base64_chars": len(encoded),
                "target_filesystem_dependency": False,
            }
            for (_, encoded), payload in zip(
                payload_fields,
                decoded_payloads,
                strict=True,
            )
        ],
        "batches": [
            {
                "batch_ordinal": batch.batch_index + 1,
                "batch_count": batch_count,
                "artifact_count": len(entries),
                "executor_count": 1,
            }
        ],
    }


def canonical_database_identity(infobase: Path) -> str:
    """Hash file identity so aliases converge without publishing its path."""
    database = Path(infobase).resolve(strict=True) / "1Cv8.1CD"
    try:
        stat = database.stat()
    except OSError as error:
        raise BenchmarkContractError("target database identity is unavailable") from error
    if not database.is_file() or stat.st_dev < 0 or stat.st_ino <= 0:
        raise BenchmarkContractError("target database identity is unavailable")
    material = f"onec-cold-db-v1\0{stat.st_dev:x}\0{stat.st_ino:x}\0{stat.st_size}"
    return sha256(material.encode("ascii")).hexdigest()


def validate_public_evidence(value: object, *, key: str = "") -> None:
    """Reject paths, source, credentials and runtime identities recursively."""
    if key.casefold() in _PRIVATE_KEYS:
        raise BenchmarkContractError("benchmark evidence contains private data")
    if isinstance(value, Mapping):
        for nested_key, nested_value in value.items():
            if not isinstance(nested_key, str):
                raise BenchmarkContractError("benchmark evidence contains private data")
            validate_public_evidence(nested_value, key=nested_key)
        return
    if isinstance(value, (tuple, list)):
        for item in value:
            validate_public_evidence(item)
        return
    if isinstance(value, str):
        folded = value.casefold()
        if (
            len(value) > 512
            or _WINDOWS_PATH_RE.search(value)
            or _POSIX_PATH_RE.search(value)
            or _UUID_RE.search(value)
            or _SOURCE_RE.search(value)
            or "password=" in folded
            or "token=" in folded
        ):
            raise BenchmarkContractError("benchmark evidence contains private data")


def _validate_expected_reference(
    value: Mapping[str, object],
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise BenchmarkContractError("benchmark reference is invalid")
    database_identity = value.get("database_file_identity_sha256")
    if not isinstance(database_identity, str):
        raise BenchmarkContractError("benchmark reference is invalid")
    expected = benchmark_reference(database_identity)
    if dict(value) != expected:
        raise BenchmarkContractError("benchmark reference is invalid")
    validate_public_evidence(expected)
    return expected


def _checkpoint_configuration(
    schedule: Sequence[ColdSampleRequest],
    *,
    expected_reference: Mapping[str, object],
) -> dict[str, object]:
    if not schedule:
        raise BenchmarkContractError("cold schedule is empty")
    modes = tuple(dict.fromkeys(item.mode for item in schedule))
    return {
        "cold_reloads": max(item.mode_ordinal for item in schedule),
        "modes": list(modes),
        "reference": _validate_expected_reference(expected_reference),
        "implementation": benchmark_implementation_identity(),
    }


def pending_cold_samples(
    schedule: Sequence[ColdSampleRequest],
    checkpoint: Mapping[str, object],
    *,
    expected_reference: Mapping[str, object],
) -> tuple[ColdSampleRequest, ...]:
    configuration = _checkpoint_configuration(
        schedule,
        expected_reference=expected_reference,
    )
    if (
        checkpoint.get("schema") != CHECKPOINT_SCHEMA
        or checkpoint.get("configuration") != configuration
        or not isinstance(checkpoint.get("cold_samples"), list)
    ):
        raise BenchmarkContractError("checkpoint configuration does not match")
    completed = checkpoint["cold_samples"]
    for index, sample in enumerate(completed):
        request = schedule[index] if index < len(schedule) else None
        if (
            request is None
            or not isinstance(sample, Mapping)
            or sample.get("sequence") != request.sequence
            or sample.get("mode") != request.mode
            or sample.get("mode_ordinal") != request.mode_ordinal
            or sample.get("warmup") is not False
            or _HASH_RE.fullmatch(str(sample.get("process_identity_sha256", ""))) is None
            or sample.get("reference") != configuration["reference"]
            or sample.get("implementation") != configuration["implementation"]
            or sample.get("database_file_identity_stable") is not True
        ):
            raise BenchmarkContractError("checkpoint sample identity is invalid")
    return tuple(schedule[len(completed) :])


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    validate_public_evidence(payload)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)


def run_cold_parent_schedule(
    schedule: Sequence[ColdSampleRequest],
    checkpoint_path: Path,
    *,
    spawn_sample: Callable[[ColdSampleRequest], Mapping[str, object]],
    expected_reference: Mapping[str, object],
) -> dict[str, object]:
    """Run/resume samples and durably checkpoint after every fresh child."""
    path = Path(checkpoint_path)
    configuration = _checkpoint_configuration(
        schedule,
        expected_reference=expected_reference,
    )
    if path.is_file():
        checkpoint = json.loads(path.read_text(encoding="utf-8"))
        validate_public_evidence(checkpoint)
        pending = pending_cold_samples(
            schedule,
            checkpoint,
            expected_reference=expected_reference,
        )
    else:
        checkpoint = {
            "schema": CHECKPOINT_SCHEMA,
            "configuration": configuration,
            "cold_samples": [],
        }
        pending = tuple(schedule)
    samples = checkpoint["cold_samples"]
    identities = {
        str(sample["process_identity_sha256"])
        for sample in samples
    }
    for request in pending:
        sample = dict(spawn_sample(request))
        identity = sample.get("process_identity_sha256")
        if (
            sample.get("sequence") != request.sequence
            or sample.get("mode") != request.mode
            or sample.get("mode_ordinal") != request.mode_ordinal
            or sample.get("warmup") is not False
            or not isinstance(identity, str)
            or _HASH_RE.fullmatch(identity) is None
            or sample.get("reference") != configuration["reference"]
            or sample.get("implementation") != configuration["implementation"]
            or sample.get("database_file_identity_stable") is not True
        ):
            raise BenchmarkContractError("cold child result identity is invalid")
        if identity in identities:
            raise BenchmarkContractError("cold sample did not use a fresh process")
        validate_public_evidence(sample)
        identities.add(identity)
        samples.append(sample)
        _write_json_atomic(path, checkpoint)
    return checkpoint


def cold_child_command(request: ColdSampleRequest) -> tuple[str, ...]:
    """Build a child command containing identifiers only; secrets stay in env."""
    return (
        sys.executable,
        "-m",
        "tools.minimal_worker_reload_benchmark",
        "--cold-child",
        "--sequence",
        str(request.sequence),
        "--sample-mode",
        request.mode,
        "--mode-ordinal",
        str(request.mode_ordinal),
    )


def parse_child_command(batch_index: int, *, iterations: int) -> tuple[str, ...]:
    """Build a parse-microgate child command without source locations."""
    if type(batch_index) is not int or batch_index <= 0:
        raise BenchmarkContractError("parse batch index is invalid")
    if type(iterations) is not int or iterations <= 0:
        raise BenchmarkContractError("parse iteration count is invalid")
    return (
        sys.executable,
        "-m",
        "tools.minimal_worker_reload_benchmark",
        "--parse-child",
        "--batch-index",
        str(batch_index),
        "--parse-iterations",
        str(iterations),
    )


def _spawn_json_child(
    command: tuple[str, ...],
    *,
    environment: Mapping[str, str],
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> dict[str, object]:
    completed = runner(
        command,
        env=dict(environment),
        capture_output=True,
        text=True,
        check=False,
        timeout=900,
    )
    if completed.returncode != 0:
        # stderr can contain a private path, source fragment or target command.
        raise BenchmarkContractError("benchmark child failed")
    try:
        result = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError):
        raise BenchmarkContractError("benchmark child returned invalid JSON") from None
    if not isinstance(result, dict):
        raise BenchmarkContractError("benchmark child returned invalid JSON")
    validate_public_evidence(result)
    return result


def spawn_cold_child(
    request: ColdSampleRequest,
    *,
    environment: Mapping[str, str],
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, object]:
    """Execute exactly one cold sample in exactly one fresh Python process."""
    return _spawn_json_child(
        cold_child_command(request),
        environment=environment,
        runner=runner,
    )


def spawn_parse_child(
    batch_index: int,
    iterations: int,
    *,
    environment: Mapping[str, str],
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, object]:
    return _spawn_json_child(
        parse_child_command(batch_index, iterations=iterations),
        environment=environment,
        runner=runner,
    )


def run_parse_parent_batches(
    process_count: int,
    iterations: int,
    *,
    spawn_batch: Callable[[int, int], Mapping[str, object]],
    expected_reference: Mapping[str, object],
) -> list[dict[str, object]]:
    """Run parse batches in distinct children and enforce the microgate contract."""
    if type(process_count) is not int or process_count <= 0:
        raise BenchmarkContractError("parse process count is invalid")
    if type(iterations) is not int or iterations <= 0:
        raise BenchmarkContractError("parse iteration count is invalid")
    batches: list[dict[str, object]] = []
    identities: set[str] = set()
    reference = _validate_expected_reference(expected_reference)
    implementation = benchmark_implementation_identity()
    for batch_index in range(1, process_count + 1):
        batch = dict(spawn_batch(batch_index, iterations))
        identity = batch.get("process_identity_sha256")
        samples = batch.get("samples_ms")
        if (
            batch.get("batch_index") != batch_index
            or batch.get("warmup_iterations") != 1
            or batch.get("measured_iterations") != iterations
            or not isinstance(identity, str)
            or _HASH_RE.fullmatch(identity) is None
            or identity in identities
            or not isinstance(samples, list)
            or len(samples) != iterations
            or batch.get("reference") != reference
            or batch.get("implementation") != implementation
            or batch.get("database_file_identity_stable") is not True
        ):
            raise BenchmarkContractError("parse child result is invalid")
        nearest_rank_summary(samples)
        validate_public_evidence(batch)
        identities.add(identity)
        batches.append(batch)
    return batches


def assemble_final_report(
    checkpoint: Mapping[str, object],
    parse_batches: Sequence[Mapping[str, object]],
    *,
    sla_ms: float,
    expected_reference: Mapping[str, object],
) -> dict[str, object]:
    """Build the privacy-safe result and apply the strict (<, never <=) SLA."""
    if not math.isfinite(sla_ms) or sla_ms <= 0:
        raise BenchmarkContractError("SLA is invalid")
    reference = _validate_expected_reference(expected_reference)
    implementation = benchmark_implementation_identity()
    configuration = checkpoint.get("configuration")
    if (
        checkpoint.get("schema") != CHECKPOINT_SCHEMA
        or not isinstance(configuration, Mapping)
        or configuration.get("reference") != reference
        or configuration.get("implementation") != implementation
    ):
        raise BenchmarkContractError("benchmark configuration identity changed")
    samples = checkpoint.get("cold_samples")
    if not isinstance(samples, list) or not samples:
        raise BenchmarkContractError("cold benchmark is incomplete")
    identities = [
        str(item.get("process_identity_sha256", ""))
        for item in (*samples, *parse_batches)
    ]
    if (
        any(_HASH_RE.fullmatch(identity) is None for identity in identities)
        or len(identities) != len(set(identities))
        or any(item.get("reference") != reference for item in samples)
        or any(item.get("implementation") != implementation for item in samples)
        or any(item.get("reference") != reference for item in parse_batches)
        or any(
            item.get("implementation") != implementation
            for item in parse_batches
        )
        or any(
            item.get("database_file_identity_stable") is not True
            for item in parse_batches
        )
    ):
        raise BenchmarkContractError("benchmark did not use fresh processes")
    summaries: dict[str, dict[str, int | float | bool]] = {}
    phase_summaries: dict[str, dict[str, dict[str, int | float]]] = {}
    all_checks_pass = True
    for mode in ("MAIN", "CAPTURE"):
        mode_samples = [sample for sample in samples if sample.get("mode") == mode]
        for sample in mode_samples:
            phases = sample.get("phases")
            parser_calls = sample.get("parser_calls")
            transport = sample.get("transport")
            if not isinstance(phases, Mapping) or not isinstance(
                parser_calls, Mapping
            ) or not isinstance(transport, Mapping):
                raise BenchmarkContractError("cold sample evidence is incomplete")
            validate_cold_phase_contract(phases, parser_calls)
            validate_cold_transport_contract(transport)
            accounting = cold_phase_accounting(
                phases,
                end_to_end_ms=sample.get("end_to_end_ms"),
            )
            if any(
                type(sample.get(field)) not in (int, float)
                or not math.isclose(
                    float(sample[field]),
                    expected,
                    rel_tol=0.0,
                    abs_tol=1e-9,
                )
                for field, expected in accounting.items()
            ):
                raise BenchmarkContractError("cold sample accounting is invalid")
        summary = nearest_rank_summary(
            [float(sample["end_to_end_ms"]) for sample in mode_samples]
        )
        summary["sla_passed"] = bool(summary["p95_ms"] < sla_ms)
        summaries[mode] = summary
        all_checks_pass = all_checks_pass and bool(summary["sla_passed"])
        all_checks_pass = all_checks_pass and all(
            sample.get("cleanup_passed") is True
            and sample.get("semantic_canary_passed") is True
            and sample.get("target_filesystem_dependency") is False
            and sample.get("database_file_identity_stable") is True
            and sample.get("warmup") is False
            for sample in mode_samples
        )
        phase_summaries[mode] = {
            phase: nearest_rank_summary(
                [float(sample["phases"][phase]["wall_ms"]) for sample in mode_samples]
            )
            for phase in _COLD_PHASE_CALLS
        }
        phase_summaries[mode]["host_orchestration_exclusive"] = (
            nearest_rank_summary(
                [
                    float(sample["host_orchestration_exclusive_ms"])
                    for sample in mode_samples
                ]
            )
        )
        phase_summaries[mode]["runtime_transport_promotion"] = (
            nearest_rank_summary(
                [
                    float(sample["runtime_transport_promotion_ms"])
                    for sample in mode_samples
                ]
            )
        )
    parse_samples = [
        float(value)
        for batch in parse_batches
        for value in batch.get("samples_ms", ())
    ]
    report: dict[str, object] = {
        "schema": SCHEMA,
        "status": "PASS" if all_checks_pass else "NON_PASS",
        "reference": reference,
        "implementation": implementation,
        "methodology": {
            "worker_reload_warmups": 0,
            "runtime_start_excluded": True,
            "fresh_python_process_per_reload": True,
            "counterbalanced_order": True,
            "parse_processes": len(parse_batches),
            "parse_warmups_per_process": 1,
            "top_level_phases": list(_TOP_LEVEL_PHASES),
            "drill_down_phases": sorted(_DRILL_DOWN_PHASES),
            "derived_subtotals": sorted(_DERIVED_SUBTOTALS),
        },
        "sla_ms_exclusive": sla_ms,
        "summary": summaries,
        "phase_summary": phase_summaries,
        "parse_summary": nearest_rank_summary(parse_samples),
        "cold_samples": samples,
        "parse_batches": list(parse_batches),
    }
    validate_public_evidence(report)
    return report


def _process_identity_sha256() -> str:
    import psutil

    material = b"\0".join(
        (
            str(os.getpid()).encode("ascii"),
            repr(psutil.Process().create_time()).encode("ascii"),
            str(time_ns()).encode("ascii"),
            token_bytes(32),
        )
    )
    return sha256(material).hexdigest()


def _exact_live_inputs() -> tuple[object, Path, tuple[object, ...], dict[str, object]]:
    """Admit private env configuration and return only separately safe evidence."""
    required = (
        "ONEC_RUNTIME_PLATFORM_BIN",
        "ONEC_RUNTIME_INFOBASE",
        "ONEC_RUNTIME_USERNAME",
        "ONEC_ZUP_SOURCE_ROOT",
    )
    if any(not os.environ.get(name) for name in required):
        raise BenchmarkContractError("live benchmark environment is incomplete")
    if os.environ.get("ONEC_RUNTIME_PASSWORD", ""):
        raise BenchmarkContractError("live benchmark password must be empty")

    from integration.support.zup_sources import admit_zup_source_bundle
    from integration.zup_worker_universe_acceptance import (
        ZupStaticPreflight,
        _acceptance_units,
        _infobase_identity_sha256,
        _matching_target_process_count,
        _packaged_extension_sha256,
        _platform_sha256,
    )
    from onec_runtime.config import RuntimeConfig

    workspace = Path(__file__).resolve().parents[1]
    source_root = Path(os.environ["ONEC_ZUP_SOURCE_ROOT"])
    config = RuntimeConfig(
        workspace=workspace,
        platform_bin=Path(os.environ["ONEC_RUNTIME_PLATFORM_BIN"]),
        connection_string=f'File="{os.environ["ONEC_RUNTIME_INFOBASE"]}";',
        username=os.environ["ONEC_RUNTIME_USERNAME"],
    )
    bundle = admit_zup_source_bundle(source_root)
    raw_inventory = [
        {
            "logical_name": item.name,
            "sha256": item.source_sha256,
            "bytes": item.source_bytes,
            "lines": item.source_lines,
        }
        for item in bundle.units
    ]
    reference = benchmark_reference(
        canonical_database_identity(config.infobase_dir)
    )
    if (
        config.platform_bin.parent.name != _REFERENCE_PLATFORM_VERSION
        or _platform_sha256(config) != _REFERENCE_PLATFORM_SHA256
        or _infobase_identity_sha256(config)
        != _REFERENCE_INFOBASE_PATH_IDENTITY_SHA256
        or _packaged_extension_sha256() != _REFERENCE_EXTENSION_SHA256
        or _matching_target_process_count(config.infobase_dir) != 0
        or raw_inventory != reference["raw_sources"]
    ):
        raise BenchmarkContractError("live benchmark identity is incompatible")
    units = _acceptance_units(
        ZupStaticPreflight((), source_bundle=bundle),
        extended_revision=17,
        extended_value=17,
    )
    if measured_unit_inventory(units) != reference["measured_units"]:
        raise BenchmarkContractError("measured Worker unit identity is incompatible")
    validate_public_evidence(reference)
    return config, source_root, units, reference


def _parent_benchmark_reference() -> dict[str, object]:
    """Resolve the private target once and expose only its canonical identity."""
    infobase = os.environ.get("ONEC_RUNTIME_INFOBASE")
    if not infobase:
        raise BenchmarkContractError("live benchmark environment is incomplete")
    return benchmark_reference(canonical_database_identity(Path(infobase)))


@contextmanager
def _granular_projection_phases(recorder: object):
    """Observe the active full-AST extraction boundaries without semantic changes."""
    from onec_runtime.bsl import full_ast_worker_projection

    target_type = full_ast_worker_projection.PythonParserTarget

    originals = {
        "tokenize": full_ast_worker_projection.tokenize,
        "select_server_effective_tokens": full_ast_worker_projection.select_server_effective_tokens,
        "parse_tokens_ast": target_type.parse_tokens_ast,
        "project_full_ast_module": full_ast_worker_projection.project_full_ast_module,
    }

    def profiled_tokenize(source: str) -> object:
        return recorder.measure(
            "tokenize",
            lambda: originals["tokenize"](source),
            input_bytes=len(source.encode("utf-8")),
            item_count=len,
        )

    def profiled_filter(*args: object, **kwargs: object) -> object:
        return recorder.measure(
            "server_token_filter",
            lambda: originals["select_server_effective_tokens"](*args, **kwargs),
            item_count=len,
        )

    def profiled_parse(*args: object, **kwargs: object) -> object:
        return recorder.measure(
            "full_ast_generated_parse",
            lambda: originals["parse_tokens_ast"](*args, **kwargs),
        )

    def profiled_extract(*args: object, **kwargs: object) -> object:
        return recorder.measure(
            "full_ast_model_extract",
            lambda: originals["project_full_ast_module"](*args, **kwargs),
            item_count=lambda result: len(result.methods),
        )

    full_ast_worker_projection.tokenize = profiled_tokenize
    full_ast_worker_projection.select_server_effective_tokens = profiled_filter
    target_type.parse_tokens_ast = profiled_parse
    full_ast_worker_projection.project_full_ast_module = profiled_extract
    try:
        yield
    finally:
        full_ast_worker_projection.tokenize = originals["tokenize"]
        full_ast_worker_projection.select_server_effective_tokens = originals[
            "select_server_effective_tokens"
        ]
        target_type.parse_tokens_ast = originals["parse_tokens_ast"]
        full_ast_worker_projection.project_full_ast_module = originals[
            "project_full_ast_module"
        ]


class _DeferredStageBatchProof:
    """Seal minimal timed captures before authenticating their full contents."""

    __slots__ = ("_pending", "_private_markers", "_sealed")

    def __init__(self, private_markers: Sequence[str]) -> None:
        self._private_markers = tuple(private_markers)
        self._pending: list[tuple[str, object, int]] | None = []
        self._sealed: tuple[tuple[str, object, int], ...] | None = None

    def capture(self, instruction: str, batch: object, batch_count: int) -> None:
        pending = self._pending
        if pending is None:
            raise BenchmarkContractError("Worker batch transport proof is sealed")
        pending.append((instruction, batch, batch_count))

    def seal(self) -> None:
        pending = self._pending
        if pending is None:
            return
        self._sealed = tuple(pending)
        self._pending = None

    def validate(self) -> dict[str, list[dict[str, int | bool]]]:
        captures = self._sealed
        if captures is None:
            raise BenchmarkContractError("Worker batch transport proof is not sealed")
        observation: dict[str, list[dict[str, int | bool]]] = {
            "artifacts": [],
            "batches": [],
        }
        for instruction, batch, batch_count in captures:
            proof = validate_stage_batch_instruction(
                instruction,
                batch=batch,
                batch_count=batch_count,
                private_markers=self._private_markers,
            )
            observation["artifacts"].extend(proof["artifacts"])
            observation["batches"].extend(proof["batches"])
        return observation


@contextmanager
def _base64_transport_proof(private_markers: Sequence[str]):
    from onec_runtime import worker_universe

    original = worker_universe.stage_worker_batch_instruction
    deferred_proof = _DeferredStageBatchProof(private_markers)

    def observed(
        batch: object,
        *,
        batch_count: int,
        transaction_id: object,
    ) -> str:
        instruction = original(
            batch,
            batch_count=batch_count,
            transaction_id=transaction_id,
        )
        deferred_proof.capture(instruction, batch, batch_count)
        return instruction

    worker_universe.stage_worker_batch_instruction = observed
    try:
        yield deferred_proof
    finally:
        worker_universe.stage_worker_batch_instruction = original
        deferred_proof.seal()


def _prepare_mode(session: object, mode: str) -> None:
    if mode == "MAIN":
        return
    if mode != "CAPTURE":
        raise BenchmarkContractError("cold sample mode is invalid")
    from integration.zup_worker_universe_acceptance import (
        _require_reply,
        _synthetic_capture_location,
    )
    from onec_runtime.runtime_api import RuntimeReplyKind

    location = _synthetic_capture_location(session.config.runtime.runtime_dir)
    session.configure_capture_points((location,))
    reply = session.execute_bsl(
        "ColdCapture = RuntimeKernelServer.СинтетическийCapture(100);\n"
        "Результат = 41;"
    )
    _require_reply(reply, kinds=(RuntimeReplyKind.CAPTURED,))
    if session.runtime_api.operation_worker_generation is not None:
        raise BenchmarkContractError("CAPTURE sample unexpectedly pinned a generation")


def _verify_semantic_canary(session: object, mode: str) -> None:
    from integration.zup_worker_universe_acceptance import _require_reply
    from onec_runtime.runtime_api import RuntimeReplyKind

    if mode == "CAPTURE":
        if session.runtime_api.operation_worker_generation is not None:
            raise BenchmarkContractError("CAPTURE reload changed the current pin")
        _require_reply(
            session.runtime_api.resume_capture(),
            kinds=(RuntimeReplyKind.MAIN_COMPLETED,),
            expected=41,
        )
    _require_reply(
        session.execute_bsl(
            "Результат = КадровыйУчет.__OnecTask10CrossCall();"
        ),
        kinds=(RuntimeReplyKind.MAIN_COMPLETED,),
        expected=1717,
    )


def _run_cold_child(request: ColdSampleRequest) -> dict[str, object]:
    from integration.zup_worker_universe_acceptance import (
        _matching_target_process_count,
    )
    from onec_runtime.performance_profile import PhaseRecorder
    from onec_runtime.session import RuntimeSession, RuntimeSessionConfig

    config, source_root, units, reference = _exact_live_inputs()
    expected_database_identity = str(
        reference["database_file_identity_sha256"]
    )
    implementation = benchmark_implementation_identity()
    identity = _process_identity_sha256()
    runtime_private_root = config.runtime_dir / "minimal-worker-projection-private"
    runtime_private_root.mkdir(parents=True, exist_ok=True)
    session = None
    sample: dict[str, object] | None = None
    with TemporaryDirectory(prefix="cold-child-", dir=runtime_private_root) as private:
        private_root = Path(private)
        try:
            # Runtime startup and mode arming are deliberately outside t0.
            session = RuntimeSession.start(
                RuntimeSessionConfig(
                    config,
                    private_root / "evidence",
                    source_root=source_root,
                )
            )
            if (
                canonical_database_identity(config.infobase_dir)
                != expected_database_identity
            ):
                raise BenchmarkContractError(
                    "target database identity changed during runtime startup"
                )
            if _matching_target_process_count(config.infobase_dir) <= 0:
                raise BenchmarkContractError("runtime target identity was not observed")
            cache_before = assert_fresh_worker_state(session)
            _prepare_mode(session, request.mode)
            if assert_fresh_worker_state(session) != cache_before:
                raise BenchmarkContractError("mode preparation warmed Worker state")

            recorder = PhaseRecorder()
            private_markers = (
                str(config.platform_bin),
                str(config.infobase_dir),
                str(source_root),
                str(private_root),
            )
            with (
                _granular_projection_phases(recorder),
                _base64_transport_proof(private_markers) as deferred_transport_proof,
            ):
                started = perf_counter_ns()
                handle = session.load_worker_modules(units, profiler=recorder)
                end_to_end_ms = (perf_counter_ns() - started) / 1_000_000
            transport_observation = deferred_transport_proof.validate()
            phases = aggregate_phase_events(tuple(recorder.events))
            parser = recorder.parser_calls
            parser_calls = {
                "full_module_parses": parser.full_module_parses,
                "delta_method_parses": parser.delta_method_parses,
                "packaging_validation_parses": parser.packaging_validation_parses,
                "worker_profile_parses": parser.worker_profile_parses,
            }
            validate_cold_phase_contract(phases, parser_calls)
            _verify_semantic_canary(session, request.mode)
            session.release_worker_generation(handle)

            artifact_proofs = transport_observation["artifacts"]
            batch_records = transport_observation["batches"]
            transport = {
                "artifact_count": len(artifact_proofs),
                "batch_count": len(batch_records),
                "executor_count": sum(
                    int(item["executor_count"]) for item in batch_records
                ),
                "artifact_bytes": sum(
                    int(item["artifact_bytes"]) for item in artifact_proofs
                ),
                "base64_chars": sum(
                    int(item["base64_chars"]) for item in artifact_proofs
                ),
                "artifacts": artifact_proofs,
                "batches": batch_records,
                "target_filesystem_dependency": False,
            }
            validate_cold_transport_contract(transport)
            accounting = cold_phase_accounting(
                phases,
                end_to_end_ms=end_to_end_ms,
            )
            catalog = session.runtime_api._worker_catalog_snapshot
            if catalog is None:
                raise BenchmarkContractError("cold catalog publication is absent")
            sample = {
                "sequence": request.sequence,
                "mode": request.mode,
                "mode_ordinal": request.mode_ordinal,
                "warmup": False,
                "process_identity_sha256": identity,
                "reference": reference,
                "implementation": implementation,
                "cache_before": cache_before,
                "end_to_end_ms": end_to_end_ms,
                "phases": phases,
                "parser_calls": parser_calls,
                **accounting,
                "catalog": {
                    "modules": len(catalog.modules),
                    "revision": catalog.revision,
                    "sha256": catalog.sha256,
                },
                "transport": transport,
                "target_filesystem_dependency": False,
                "semantic_canary_passed": True,
            }
        finally:
            if session is not None:
                session.close()
    if sample is None:
        raise BenchmarkContractError("cold sample did not complete")
    if (
        canonical_database_identity(config.infobase_dir)
        != expected_database_identity
    ):
        raise BenchmarkContractError(
            "target database identity changed during live sample"
        )
    sample["database_file_identity_stable"] = True
    sample["cleanup_passed"] = (
        _matching_target_process_count(config.infobase_dir) == 0
        and not private_root.exists()
    )
    validate_public_evidence(sample)
    return sample


def _run_parse_child(batch_index: int, iterations: int) -> dict[str, object]:
    from onec_runtime.bsl.full_ast_worker_projection import parse_full_ast_module

    config, _source_root, units, reference = _exact_live_inputs()
    expected_database_identity = str(
        reference["database_file_identity_sha256"]
    )
    result = measure_parse_batch(
        batch_index,
        iterations,
        tuple(unit.mapped_source.text for unit in units),
        parse_one=parse_full_ast_module,
        process_identity_sha256=_process_identity_sha256(),
    )
    result["reference"] = reference
    result["implementation"] = benchmark_implementation_identity()
    if (
        canonical_database_identity(config.infobase_dir)
        != expected_database_identity
    ):
        raise BenchmarkContractError(
            "target database identity changed during parse batch"
        )
    result["database_file_identity_stable"] = True
    validate_public_evidence(result)
    return result


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real-zup", action="store_true")
    parser.add_argument("--cold-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--parse-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--sequence", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--sample-mode", help=argparse.SUPPRESS)
    parser.add_argument("--mode-ordinal", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--batch-index", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--parse-processes", type=int, default=3)
    parser.add_argument("--parse-iterations", type=int, default=30)
    parser.add_argument("--cold-reloads", type=int, default=30)
    parser.add_argument("--modes", nargs="+", default=("MAIN", "CAPTURE"))
    parser.add_argument("--output")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _argument_parser().parse_args(argv)
    try:
        if args.cold_child:
            if (
                args.parse_child
                or args.sequence is None
                or args.sample_mode not in {"MAIN", "CAPTURE"}
                or args.mode_ordinal is None
            ):
                raise BenchmarkContractError("cold child arguments are invalid")
            result = _run_cold_child(
                ColdSampleRequest(
                    args.sequence,
                    args.sample_mode,
                    args.mode_ordinal,
                )
            )
            print(json.dumps(result, ensure_ascii=True, sort_keys=True))
            return 0
        if args.parse_child:
            if args.batch_index is None or args.parse_iterations <= 0:
                raise BenchmarkContractError("parse child arguments are invalid")
            result = _run_parse_child(args.batch_index, args.parse_iterations)
            print(json.dumps(result, ensure_ascii=True, sort_keys=True))
            return 0
        if (
            not args.real_zup
            or not args.output
            or args.parse_processes < 3
            or args.parse_iterations < 30
            or args.cold_reloads < 30
        ):
            raise BenchmarkContractError("full real-ZUP benchmark arguments are invalid")

        environment = dict(os.environ)
        expected_reference = _parent_benchmark_reference()
        parse_batches = run_parse_parent_batches(
            args.parse_processes,
            args.parse_iterations,
            spawn_batch=lambda index, iterations: spawn_parse_child(
                index,
                iterations,
                environment=environment,
            ),
            expected_reference=expected_reference,
        )
        schedule = build_cold_schedule(args.cold_reloads, tuple(args.modes))
        output = Path(args.output)
        checkpoint = output.with_name(f"{output.name}.checkpoint")
        cold = run_cold_parent_schedule(
            schedule,
            checkpoint,
            spawn_sample=lambda request: spawn_cold_child(
                request,
                environment=environment,
            ),
            expected_reference=expected_reference,
        )
        report = assemble_final_report(
            cold,
            parse_batches,
            sla_ms=2_500.0,
            expected_reference=expected_reference,
        )
        _write_json_atomic(output, report)
        return 0 if report["status"] == "PASS" else 1
    except BaseException as error:  # noqa: BLE001 - CLI must redact live details
        print(f"NON_PASS: {type(error).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
