from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
from io import BytesIO, TextIOWrapper
import json
import os
from pathlib import Path
import subprocess
import sys
from time import perf_counter_ns
from uuid import UUID
from types import SimpleNamespace

import pytest

from onec_runtime.performance_profile import PhaseEvent
from onec_runtime.bsl.full_ast_worker_projection import parse_full_ast_module
import tools.minimal_worker_reload_benchmark as benchmark
from tools.minimal_worker_reload_benchmark import (
    BenchmarkContractError,
    ColdSampleRequest,
    aggregate_phase_events,
    assemble_final_report,
    assert_fresh_worker_state,
    benchmark_implementation_identity,
    benchmark_reference,
    build_cold_schedule,
    canonical_database_identity,
    cold_child_command,
    measure_parse_batch,
    measured_unit_inventory,
    nearest_rank_summary,
    parse_child_command,
    pending_cold_samples,
    run_cold_parent_schedule,
    run_parse_parent_batches,
    spawn_cold_child,
    validate_cold_phase_contract,
    validate_public_evidence,
    validate_stage_instruction,
    _granular_projection_phases,
)


_TEST_DATABASE_FILE_IDENTITY = "c" * 64


def _reference() -> dict[str, object]:
    return benchmark_reference(_TEST_DATABASE_FILE_IDENTITY)


def test_cold_schedule_counterbalances_thirty_samples_per_mode() -> None:
    schedule = build_cold_schedule(30, ("MAIN", "CAPTURE"))

    assert len(schedule) == 60
    assert [item.mode for item in schedule[:8]] == [
        "MAIN",
        "CAPTURE",
        "CAPTURE",
        "MAIN",
        "MAIN",
        "CAPTURE",
        "CAPTURE",
        "MAIN",
    ]
    assert [item.mode_ordinal for item in schedule if item.mode == "MAIN"] == list(
        range(1, 31)
    )
    assert [item.mode_ordinal for item in schedule if item.mode == "CAPTURE"] == list(
        range(1, 31)
    )
    assert all(item.warmup is False for item in schedule)


def test_duplicate_module_phases_are_summed_without_losing_call_count() -> None:
    events = (
        PhaseEvent(1, "semantic_parse", 10_000_000, 7_000_000, item_count=1),
        PhaseEvent(2, "semantic_parse", 30_000_000, 20_000_000, item_count=1),
        PhaseEvent(3, "catalog_validation", 5_000_000, 4_000_000, item_count=2),
    )

    assert aggregate_phase_events(events) == {
        "catalog_validation": {
            "calls": 1,
            "wall_ms": 5.0,
            "cpu_ms": 4.0,
            "input_bytes": 0,
            "output_bytes": 0,
            "item_count": 2,
        },
        "semantic_parse": {
            "calls": 2,
            "wall_ms": 40.0,
            "cpu_ms": 27.0,
            "input_bytes": 0,
            "output_bytes": 0,
            "item_count": 2,
        },
    }


def test_full_ast_projection_profiler_records_two_real_module_boundaries() -> None:
    """Break caught: cold profiling must reach the sole full-AST parser path."""
    from onec_runtime.performance_profile import PhaseRecorder

    recorder = PhaseRecorder()
    source = "Процедура P()\nX = Y;\nКонецПроцедуры"

    with _granular_projection_phases(recorder):
        parse_full_ast_module(source, profiler=recorder)
        parse_full_ast_module(source, profiler=recorder)

    phases = aggregate_phase_events(tuple(recorder.events))
    assert {name: phases[name]["calls"] for name in (
        "tokenize",
        "server_token_filter",
        "full_ast_generated_parse",
        "full_ast_model_extract",
        "semantic_parse",
        "ast_model_extract",
    )} == {
        "tokenize": 2,
        "server_token_filter": 2,
        "full_ast_generated_parse": 2,
        "full_ast_model_extract": 2,
        "semantic_parse": 2,
        "ast_model_extract": 2,
    }
    assert recorder.parser_calls.full_module_parses == 2
    assert recorder.parser_calls.worker_profile_parses == 0


def test_cold_child_reaches_full_ast_staging_and_reload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Break caught: a deleted Worker adapter must not stop cold reload early."""
    from onec_runtime.performance_profile import PhaseRecorder
    from onec_runtime.session import RuntimeSession
    import integration.zup_worker_universe_acceptance as acceptance

    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    config = SimpleNamespace(
        runtime_dir=runtime_dir,
        infobase_dir=tmp_path / "infobase",
        platform_bin=tmp_path / "platform" / "bin",
    )
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = "Процедура P()\nX = Y;\nКонецПроцедуры"
    units = tuple(
        SimpleNamespace(
            logical_name=name,
            mapped_source=SimpleNamespace(text=source),
        )
        for name in ("Первый", "Второй")
    )
    reference = benchmark_reference("d" * 64)
    catalog = SimpleNamespace(modules=units, revision=1, sha256="e" * 64)

    class FakeSession:
        closed = False

        def __init__(self) -> None:
            self.runtime_api = SimpleNamespace(_worker_catalog_snapshot=None)

        def load_worker_modules(self, loaded_units, *, profiler: PhaseRecorder):
            for unit in loaded_units:
                parse_full_ast_module(unit.mapped_source.text, profiler=profiler)
            for phase, calls, items in (
                ("catalog_validation", 1, 0),
                ("dependency_analysis", 2, 0),
                ("resolved_analysis_adapter", 2, 0),
                ("alias_transform", 2, 0),
                ("source_map_composition", 2, 0),
                ("admission", 2, 0),
                ("epf_packaging", 2, 2),
                ("artifact_stage_sealed_validation", 1, 2),
                ("artifact_stage_base64", 1, 2),
                ("artifact_stage_executor", 1, 2),
                ("artifact_stage_batch", 1, 2),
                ("artifact_staging", 1, 2),
                ("generation_create_wire_probe", 1, 1),
                ("root_swap", 1, 1),
                ("end_to_end", 1, 2),
            ):
                for _ in range(calls):
                    profiler.record_duration(
                        phase,
                        wall_ns={
                            "artifact_stage_batch": 3,
                            "artifact_staging": 5,
                            "end_to_end": 9_000_000,
                        }.get(phase, 1),
                        item_count=items // calls,
                    )
            self.runtime_api._worker_catalog_snapshot = catalog
            return object()

        def release_worker_generation(self, _handle: object) -> None:
            pass

        def close(self) -> None:
            self.closed = True

    session = FakeSession()

    order: list[str] = []

    class DeferredTransportProof:
        def validate(self):
            order.append("proof")
            return {
                "artifacts": [
                    {
                        "artifact_bytes": 1,
                        "base64_chars": 4,
                        "target_filesystem_dependency": False,
                    },
                    {
                        "artifact_bytes": 1,
                        "base64_chars": 4,
                        "target_filesystem_dependency": False,
                    },
                ],
                "batches": [
                    {
                        "batch_ordinal": 1,
                        "batch_count": 1,
                        "artifact_count": 2,
                        "executor_count": 1,
                    }
                ],
            }

    @contextmanager
    def transport_proof(_private_markers):
        yield DeferredTransportProof()

    ticks = iter((10_000_000, 20_000_000))

    def measured_clock() -> int:
        order.append(f"t{len(order)}")
        return next(ticks)

    monkeypatch.setattr(
        benchmark,
        "_exact_live_inputs",
        lambda: (config, source_root, units, reference),
    )
    monkeypatch.setattr(benchmark, "_process_identity_sha256", lambda: "a" * 64)
    monkeypatch.setattr(benchmark, "canonical_database_identity", lambda _path: "d" * 64)
    monkeypatch.setattr(benchmark, "assert_fresh_worker_state", lambda _session: {})
    monkeypatch.setattr(benchmark, "_prepare_mode", lambda _session, _mode: None)
    monkeypatch.setattr(benchmark, "_verify_semantic_canary", lambda _session, _mode: None)
    monkeypatch.setattr(benchmark, "_base64_transport_proof", transport_proof)
    monkeypatch.setattr(benchmark, "perf_counter_ns", measured_clock)
    monkeypatch.setattr(RuntimeSession, "start", lambda _config: session)
    monkeypatch.setattr(
        acceptance,
        "_matching_target_process_count",
        lambda _path: 0 if session.closed else 1,
    )

    sample = benchmark._run_cold_child(ColdSampleRequest(1, "MAIN", 1))

    assert order == ["t0", "t1", "proof"]
    assert sample["phases"]["artifact_staging"]["calls"] == 1
    assert sample["phases"]["artifact_stage_batch"]["calls"] == 1
    assert sample["phases"]["root_swap"]["calls"] == 1
    assert sample["transport"]["artifact_count"] == 2
    assert sample["transport"]["batch_count"] == 1
    assert sample["transport"]["executor_count"] == 1
    assert sample["transport"]["batches"] == [
        {
            "batch_ordinal": 1,
            "batch_count": 1,
            "artifact_count": 2,
            "executor_count": 1,
        }
    ]
    assert sample["parser_calls"] == {
        "full_module_parses": 2,
        "delta_method_parses": 0,
        "packaging_validation_parses": 0,
        "worker_profile_parses": 0,
    }


def test_nearest_rank_summary_keeps_first_sample_distinct_from_minimum() -> None:
    values = [91.0, *[float(value) for value in range(1, 30)]]

    assert nearest_rank_summary(values) == {
        "count": 30,
        "first_ms": 91.0,
        "p50_ms": 15.0,
        "p95_ms": 29.0,
        "max_ms": 91.0,
    }


def _fresh_session() -> SimpleNamespace:
    catalog = SimpleNamespace(
        initialized=False,
        _path_index=None,
        _resolved={},
    )
    cache = SimpleNamespace(_capsules={})
    api = SimpleNamespace(
        _worker_catalog_snapshot=None,
        _worker_active_modules={},
        _worker_module_artifacts={},
        _worker_generation_handle=None,
        _worker_module_builder=SimpleNamespace(_cache=cache),
        _worker_universe=SimpleNamespace(_generations={}, _active_generation=None),
        _worker_universe_target=SimpleNamespace(_registrations={}),
    )
    return SimpleNamespace(
        _require_common_module_catalog=lambda: catalog,
        runtime_api=api,
    )


@pytest.mark.parametrize(
    "mutate",
    (
        lambda session: setattr(
            session._require_common_module_catalog(), "initialized", True
        ),
        lambda session: setattr(
            session._require_common_module_catalog(), "_path_index", {}
        ),
        lambda session: session._require_common_module_catalog()._resolved.update(
            {"known": None}
        ),
        lambda session: setattr(
            session.runtime_api, "_worker_catalog_snapshot", object()
        ),
        lambda session: session.runtime_api._worker_active_modules.update(
            {"known": object()}
        ),
        lambda session: session.runtime_api._worker_module_artifacts.update(
            {("known",): object()}
        ),
        lambda session: setattr(
            session.runtime_api, "_worker_generation_handle", object()
        ),
        lambda session: session.runtime_api._worker_module_builder._cache._capsules.update(
            {"known": object()}
        ),
        lambda session: session.runtime_api._worker_universe._generations.update(
            {1: object()}
        ),
        lambda session: setattr(
            session.runtime_api._worker_universe, "_active_generation", 1
        ),
        lambda session: session.runtime_api._worker_universe_target._registrations.update(
            {"known": object()}
        ),
    ),
)
def test_fresh_worker_guard_rejects_every_warm_state(mutate: object) -> None:
    session = _fresh_session()
    mutate(session)

    with pytest.raises(BenchmarkContractError, match="not cold"):
        assert_fresh_worker_state(session)


def test_fresh_worker_guard_returns_only_public_zero_counts() -> None:
    assert assert_fresh_worker_state(_fresh_session()) == {
        "catalog_initialized": False,
        "catalog_index_entries": 0,
        "catalog_resolved_entries": 0,
        "active_models": 0,
        "descriptor_artifacts": 0,
        "binary_artifacts": 0,
        "host_generations": 0,
        "target_registrations": 0,
        "active_generation": False,
    }


def test_stage_instruction_proves_base64_transport_without_target_file_path() -> None:
    instruction = (
        'Address = PutToTempStorage(Base64Value("YWJj"));\n'
        'Name = ExternalProcessors.Connect(Address, "Worker", False);'
    )

    assert validate_stage_instruction(
        instruction,
        artifact_bytes=3,
        private_markers=("C:/private/source",),
        required_tokens=(
            "Base64Value(",
            "PutToTempStorage(",
            "ExternalProcessors.Connect(",
        ),
    ) == {
        "artifact_bytes": 3,
        "base64_chars": 4,
        "target_filesystem_dependency": False,
    }


def test_batch_stage_instruction_proves_two_payloads_in_one_executor_call() -> None:
    from onec_runtime import worker_universe
    from onec_runtime.worker_stage_protocol import (
        WorkerStageEntry,
        build_worker_stage_batches,
    )

    payloads = (b"one", b"two-two")
    batch = build_worker_stage_batches(
        tuple(
            WorkerStageEntry(
                logical_name=f"Module{index}",
                artifact_sha256=sha256(payload).hexdigest(),
                registration_name=f"OnecRuntime_{index}",
                artifact_bytes=payload,
            )
            for index, payload in enumerate(payloads)
        )
    )[0]

    with benchmark._base64_transport_proof(()) as deferred_proof:
        worker_universe.stage_worker_batch_instruction(
            batch,
            batch_count=1,
            transaction_id=UUID(int=1),
        )
    observation = deferred_proof.validate()

    assert observation == {
        "artifacts": [
            {
                "artifact_bytes": 3,
                "base64_chars": 4,
                "target_filesystem_dependency": False,
            },
            {
                "artifact_bytes": 7,
                "base64_chars": 12,
                "target_filesystem_dependency": False,
            },
        ],
        "batches": [
            {
                "batch_ordinal": 1,
                "batch_count": 1,
                "artifact_count": 2,
                "executor_count": 1,
            }
        ],
    }


def test_batch_transport_hook_defers_deep_proof_until_capture_is_sealed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from onec_runtime import worker_universe
    from onec_runtime.worker_stage_protocol import (
        WorkerStageEntry,
        build_worker_stage_batches,
    )

    payload = b"immutable-payload"
    batch = build_worker_stage_batches(
        (
            WorkerStageEntry(
                logical_name="Module",
                artifact_sha256=sha256(payload).hexdigest(),
                registration_name="OnecRuntime_Module",
                artifact_bytes=payload,
            ),
        )
    )[0]
    calls: list[tuple[str, object, int]] = []

    def validate(instruction, *, batch, batch_count, private_markers):
        calls.append((instruction, batch, batch_count))
        return {
            "artifacts": [
                {
                    "artifact_bytes": len(payload),
                    "base64_chars": 24,
                    "target_filesystem_dependency": False,
                }
            ],
            "batches": [
                {
                    "batch_ordinal": 1,
                    "batch_count": 1,
                    "artifact_count": 1,
                    "executor_count": 1,
                }
            ],
        }

    monkeypatch.setattr(benchmark, "validate_stage_batch_instruction", validate)

    with benchmark._base64_transport_proof(()) as deferred_proof:
        instruction = worker_universe.stage_worker_batch_instruction(
            batch,
            batch_count=1,
            transaction_id=UUID(int=1),
        )
        assert calls == []
        with pytest.raises(BenchmarkContractError, match="sealed"):
            deferred_proof.validate()

    assert calls == []
    assert deferred_proof.validate()["batches"][0]["artifact_count"] == 1
    assert calls == [(instruction, batch, 1)]


def test_batch_transport_hook_cost_is_negligible_beside_post_measurement_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from onec_runtime import worker_universe
    from onec_runtime.worker_stage_protocol import (
        WorkerStageEntry,
        build_worker_stage_batches,
        stage_worker_batch_instruction,
    )

    payloads = (b"A" * (256 * 1024), b"B" * (512 * 1024))
    batch = build_worker_stage_batches(
        tuple(
            WorkerStageEntry(
                logical_name=f"Module{index}",
                artifact_sha256=sha256(payload).hexdigest(),
                registration_name=f"OnecRuntime_{index}",
                artifact_bytes=payload,
            )
            for index, payload in enumerate(payloads)
        )
    )[0]
    instruction = stage_worker_batch_instruction(
        batch,
        batch_count=1,
        transaction_id=UUID(int=1),
    )
    monkeypatch.setattr(
        worker_universe,
        "stage_worker_batch_instruction",
        lambda _batch, *, batch_count, transaction_id: instruction,
    )

    repetitions = 2_000
    capture_samples = []
    for _ in range(3):
        with benchmark._base64_transport_proof(()):
            started = perf_counter_ns()
            for _ in range(repetitions):
                worker_universe.stage_worker_batch_instruction(
                    batch,
                    batch_count=1,
                    transaction_id=UUID(int=1),
                )
            capture_samples.append((perf_counter_ns() - started) / repetitions)
    capture_ns_per_call = min(capture_samples)

    proof_samples = []
    for _ in range(3):
        with benchmark._base64_transport_proof(()) as representative:
            worker_universe.stage_worker_batch_instruction(
                batch,
                batch_count=1,
                transaction_id=UUID(int=1),
            )
        started = perf_counter_ns()
        representative.validate()
        proof_samples.append(perf_counter_ns() - started)
    proof_ns = min(proof_samples)

    assert capture_ns_per_call * 100 < proof_ns


def test_batch_stage_instruction_rejects_reordered_expected_entries() -> None:
    from onec_runtime.worker_stage_protocol import (
        WorkerStageBatch,
        WorkerStageEntry,
        stage_worker_batch_instruction,
    )

    entries = tuple(
        WorkerStageEntry(
            logical_name=f"Module{index}",
            artifact_sha256=sha256(payload).hexdigest(),
            registration_name=f"OnecRuntime_{index}",
            artifact_bytes=payload,
        )
        for index, payload in enumerate((b"one", b"two-two"))
    )
    source = stage_worker_batch_instruction(
        WorkerStageBatch(0, entries),
        batch_count=1,
        transaction_id=UUID(int=1),
    )

    with pytest.raises(BenchmarkContractError, match="transport"):
        benchmark.validate_stage_batch_instruction(
            source,
            batch=WorkerStageBatch(0, tuple(reversed(entries))),
            batch_count=1,
            private_markers=(),
        )


@pytest.mark.parametrize(
    "instruction",
    (
        'ExternalProcessors.Connect("C:/private/source/Worker.epf", False);',
        'Address = PutToTempStorage("YWJj");',
    ),
)
def test_stage_instruction_rejects_file_connect_or_missing_base64(
    instruction: str,
) -> None:
    with pytest.raises(BenchmarkContractError, match="transport"):
        validate_stage_instruction(
            instruction,
            artifact_bytes=3,
            private_markers=("C:/private/source",),
            required_tokens=(
                "Base64Value(",
                "PutToTempStorage(",
                "ExternalProcessors.Connect(",
            ),
        )


def test_database_identity_is_stable_for_hardlink_alias(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    database = first / "1Cv8.1CD"
    database.write_bytes(b"database")
    try:
        os.link(database, second / "1Cv8.1CD")
    except OSError:
        pytest.skip("hard links are unavailable")

    assert canonical_database_identity(first) == canonical_database_identity(second)


def test_database_identity_changes_when_file_size_changes(tmp_path: Path) -> None:
    infobase = tmp_path / "database"
    infobase.mkdir()
    database = infobase / "1Cv8.1CD"
    database.write_bytes(b"before")
    before = canonical_database_identity(infobase)

    database.write_bytes(b"after-size-change")

    assert canonical_database_identity(infobase) != before


def _sample(request: ColdSampleRequest) -> dict[str, object]:
    return {
        "sequence": request.sequence,
        "mode": request.mode,
        "mode_ordinal": request.mode_ordinal,
        "warmup": False,
        "process_identity_sha256": f"{request.sequence:064x}",
        "end_to_end_ms": float(request.sequence),
        "reference": _reference(),
        "implementation": benchmark_implementation_identity(),
        "database_file_identity_stable": True,
    }


def _cold_phases(scale: float) -> dict[str, dict[str, int | float]]:
    calls = {
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
    items = {
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
    wall_ms = {name: scale / 100 for name in calls}
    wall_ms.update(
        {
            "full_ast_generated_parse": scale / 200,
            "full_ast_model_extract": scale / 200,
            "artifact_stage_sealed_validation": scale / 1_000,
            "artifact_stage_base64": scale / 500,
            "artifact_stage_executor": scale / 250,
            "artifact_stage_batch": scale * 8 / 1_000,
            "end_to_end": scale,
        }
    )
    return {
        name: {
            "calls": count,
            "wall_ms": wall_ms[name],
            "cpu_ms": wall_ms[name] / 2,
            "input_bytes": 0,
            "output_bytes": 0,
            "item_count": items.get(name, 0),
        }
        for name, count in calls.items()
    }


def _cold_transport() -> dict[str, object]:
    artifacts = [
        {
            "artifact_bytes": 3,
            "base64_chars": 4,
            "target_filesystem_dependency": False,
        },
        {
            "artifact_bytes": 7,
            "base64_chars": 12,
            "target_filesystem_dependency": False,
        },
    ]
    batches = [
        {
            "batch_ordinal": 1,
            "batch_count": 1,
            "artifact_count": 2,
            "executor_count": 1,
        }
    ]
    return {
        "artifact_count": 2,
        "batch_count": 1,
        "executor_count": 1,
        "artifact_bytes": 10,
        "base64_chars": 16,
        "artifacts": artifacts,
        "batches": batches,
        "target_filesystem_dependency": False,
    }


def test_parent_checkpoints_each_fresh_child_and_resumes_after_interruption(
    tmp_path: Path,
) -> None:
    schedule = build_cold_schedule(2, ("MAIN", "CAPTURE"))
    checkpoint = tmp_path / "checkpoint.json"
    first_calls: list[int] = []

    def interrupting_spawn(request: ColdSampleRequest) -> dict[str, object]:
        first_calls.append(request.sequence)
        if request.sequence == 3:
            raise RuntimeError("interrupted")
        return _sample(request)

    with pytest.raises(RuntimeError, match="interrupted"):
        run_cold_parent_schedule(
            schedule,
            checkpoint,
            spawn_sample=interrupting_spawn,
            expected_reference=_reference(),
        )

    partial = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert [item["sequence"] for item in partial["cold_samples"]] == [1, 2]
    assert [
        item.sequence
        for item in pending_cold_samples(
            schedule,
            partial,
            expected_reference=_reference(),
        )
    ] == [3, 4]

    resumed_calls: list[int] = []
    completed = run_cold_parent_schedule(
        schedule,
        checkpoint,
        spawn_sample=lambda request: (
            resumed_calls.append(request.sequence) or _sample(request)
        ),
        expected_reference=_reference(),
    )

    assert first_calls == [1, 2, 3]
    assert resumed_calls == [3, 4]
    assert [item["sequence"] for item in completed["cold_samples"]] == [1, 2, 3, 4]


def test_parent_rejects_child_reuse_or_schedule_mismatch(tmp_path: Path) -> None:
    schedule = build_cold_schedule(2, ("MAIN", "CAPTURE"))
    checkpoint = tmp_path / "checkpoint.json"
    reused = "a" * 64

    def spawn(request: ColdSampleRequest) -> dict[str, object]:
        sample = _sample(request)
        sample["process_identity_sha256"] = reused
        return sample

    with pytest.raises(BenchmarkContractError, match="fresh process"):
        run_cold_parent_schedule(
            schedule,
            checkpoint,
            spawn_sample=spawn,
            expected_reference=_reference(),
        )

    malformed = {
        "schema": "onec-minimal-worker-reload-checkpoint-v2",
        "configuration": {
            "cold_reloads": 99,
            "modes": ["MAIN", "CAPTURE"],
            "reference": _reference(),
            "implementation": benchmark_implementation_identity(),
        },
        "cold_samples": [],
    }
    checkpoint.write_text(json.dumps(malformed), encoding="utf-8")
    with pytest.raises(BenchmarkContractError, match="configuration"):
        run_cold_parent_schedule(
            schedule,
            checkpoint,
            spawn_sample=lambda request: _sample(request),
            expected_reference=_reference(),
        )


def test_child_commands_carry_no_source_database_or_credentials() -> None:
    request = ColdSampleRequest(7, "CAPTURE", 4)

    cold = cold_child_command(request)
    parse = parse_child_command(2, iterations=30)

    assert cold[0] == sys.executable
    assert cold[-6:] == (
        "--sequence",
        "7",
        "--sample-mode",
        "CAPTURE",
        "--mode-ordinal",
        "4",
    )
    assert parse[-4:] == ("--batch-index", "2", "--parse-iterations", "30")
    rendered = " ".join((*cold, *parse)).casefold()
    assert "source-root" not in rendered
    assert "infobase" not in rendered
    assert "password" not in rendered


@pytest.mark.parametrize(
    "argv",
    (
        ("--cold-child", "--sequence", "1", "--sample-mode", "MAIN", "--mode-ordinal", "1"),
        ("--parse-child", "--batch-index", "1", "--parse-iterations", "1"),
    ),
)
def test_machine_child_output_survives_cp1252_stdout_with_cyrillic(
    monkeypatch: pytest.MonkeyPatch,
    argv: tuple[str, ...],
) -> None:
    """Break caught: child JSON must cross Windows console stdout as ASCII."""
    payload = {"logical_name": "КадровыйУчет"}
    buffer = BytesIO()
    stdout = TextIOWrapper(buffer, encoding="cp1252")
    monkeypatch.setattr(benchmark, "_run_cold_child", lambda _request: payload)
    monkeypatch.setattr(
        benchmark,
        "_run_parse_child",
        lambda _batch_index, _iterations: payload,
    )
    monkeypatch.setattr(sys, "stdout", stdout)

    assert benchmark.main(argv) == 0
    stdout.flush()

    encoded = buffer.getvalue()
    assert encoded.decode("ascii").rstrip("\r\n") == json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
    )
    assert json.loads(encoded) == payload


def test_spawn_cold_child_uses_one_subprocess_and_parses_only_json_result() -> None:
    request = ColdSampleRequest(1, "MAIN", 1)
    expected = _sample(request)
    calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

    def run(
        command: tuple[str, ...],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs["env"]))
        return subprocess.CompletedProcess(command, 0, json.dumps(expected), "")

    environment = {
        "ONEC_RUNTIME_INFOBASE": "C:/private/database",
        "ONEC_ZUP_SOURCE_ROOT": "C:/private/source",
    }
    assert spawn_cold_child(request, environment=environment, runner=run) == expected
    assert len(calls) == 1
    assert calls[0][0] == cold_child_command(request)
    assert calls[0][1]["ONEC_RUNTIME_INFOBASE"] == "C:/private/database"


def test_parse_parent_uses_three_fresh_processes_with_warmup_outside_samples() -> None:
    calls: list[int] = []

    def spawn(batch_index: int, iterations: int) -> dict[str, object]:
        calls.append(batch_index)
        return {
            "batch_index": batch_index,
            "process_identity_sha256": f"{batch_index + 100:064x}",
            "warmup_iterations": 1,
            "measured_iterations": iterations,
            "samples_ms": [float(batch_index)] * iterations,
            "reference": _reference(),
            "implementation": benchmark_implementation_identity(),
            "database_file_identity_stable": True,
        }

    batches = run_parse_parent_batches(
        3,
        30,
        spawn_batch=spawn,
        expected_reference=_reference(),
    )

    assert calls == [1, 2, 3]
    assert len(batches) == 3
    assert all(batch["measured_iterations"] == 30 for batch in batches)
    assert all(len(batch["samples_ms"]) == 30 for batch in batches)


def test_parse_child_warms_once_then_measures_the_two_module_pair() -> None:
    parsed: list[str] = []
    ticks = iter(range(0, 1_000_000_000, 1_000_000))

    result = measure_parse_batch(
        2,
        3,
        ("primary", "extended"),
        parse_one=lambda source: parsed.append(source),
        clock_ns=lambda: next(ticks),
        process_identity_sha256="c" * 64,
    )

    assert parsed == [
        "primary",
        "extended",
        "primary",
        "extended",
        "primary",
        "extended",
        "primary",
        "extended",
    ]
    assert result == {
        "batch_index": 2,
        "process_identity_sha256": "c" * 64,
        "warmup_iterations": 1,
        "measured_iterations": 3,
        "samples_ms": [1.0, 1.0, 1.0],
    }


def test_reference_distinguishes_frozen_raw_sources_from_measured_units() -> None:
    reference = _reference()

    assert reference["infobase_path_identity_sha256"] == (
        "081f7a40d3343d923d60954a5f08f52bee98c5de13dc3b5374c8cc2485c7282b"
    )
    assert reference["database_file_identity_sha256"] == (
        _TEST_DATABASE_FILE_IDENTITY
    )
    assert reference["raw_sources"] == [
        {
            "logical_name": "КадровыйУчет",
            "sha256": "378f23bcb775aaeddc2c664ff77717a611add2c81b9b5a63d83cff2354a308b6",
            "bytes": 831_889,
            "lines": 11_103,
        },
        {
            "logical_name": "КадровыйУчетРасширенный",
            "sha256": "ea83a38b89304dc33521cfa5c7758380481dcc61db6fa6497e5de13c81b1fd72",
            "bytes": 1_981_297,
            "lines": 24_591,
        },
    ]
    assert reference["measured_units"] == [
        {
            "logical_name": "КадровыйУчет",
            "sha256": "fbc31dfbd7422c2e5ff1bdcea4a5740f0e1db9871243b0eb09a83d9427f4190f",
            "bytes": 833_133,
            "lines": 11_129,
        },
        {
            "logical_name": "КадровыйУчетРасширенный",
            "sha256": "6294f11586c29f04ac37e770c6410b6bd80b99ce4a95917813fac967d9541f96",
            "bytes": 1_981_851,
            "lines": 24_607,
        },
    ]
    assert reference["raw_sources"] != reference["measured_units"]


def test_implementation_identity_versions_the_hardened_output_and_checkpoint() -> None:
    identity = benchmark_implementation_identity()

    assert identity["benchmark_schema"] == "onec-minimal-worker-reload-benchmark-v2"
    assert identity["checkpoint_schema"] == "onec-minimal-worker-reload-checkpoint-v2"
    assert len(identity["schema_identity_sha256"]) == 64
    assert len(identity["implementation_sha256"]) == 64
    assert identity["implementation_file_count"] >= 70

    repository = Path(__file__).resolve().parents[2]
    harness_digest = sha256()
    for relative in (
        "integration/support/zup_sources.py",
        "integration/zup_worker_universe_acceptance.py",
    ):
        payload = (repository / relative).read_bytes()
        harness_digest.update(relative.encode("utf-8"))
        harness_digest.update(b"\0")
        harness_digest.update(sha256(payload).digest())
    assert identity["integration_harness_file_count"] == 2
    assert identity["integration_harness_sha256"] == harness_digest.hexdigest()
    assert identity["python_implementation"] == sys.implementation.name
    assert identity["python_full_version"] == sys.version
    assert identity["python_executable_sha256"] == sha256(
        Path(sys.executable).read_bytes()
    ).hexdigest()
    assert "python_executable" not in identity


def test_measured_inventory_hashes_the_actual_utf8_unit_text() -> None:
    units = (
        SimpleNamespace(
            logical_name="Первый",
            mapped_source=SimpleNamespace(text="Строка = \"ёж\";\n"),
        ),
    )

    assert measured_unit_inventory(units) == [
        {
            "logical_name": "Первый",
            "sha256": "8aea1b708bd2ec4c55c20b4fd0cfda5d68be8582d29d5e009214d4e8bcc0adb0",
            "bytes": 23,
            "lines": 1,
        }
    ]


def test_resume_rejects_reference_implementation_and_sample_identity_drift(
    tmp_path: Path,
) -> None:
    schedule = build_cold_schedule(1, ("MAIN", "CAPTURE"))
    checkpoint_path = tmp_path / "checkpoint.json"
    completed = run_cold_parent_schedule(
        schedule,
        checkpoint_path,
        spawn_sample=_sample,
        expected_reference=_reference(),
    )

    for mutate in (
        lambda payload: payload["configuration"]["reference"].update(
            {"infobase_path_identity_sha256": "f" * 64}
        ),
        lambda payload: payload["configuration"]["implementation"].update(
            {"implementation_sha256": "e" * 64}
        ),
        lambda payload: payload["cold_samples"][0]["reference"].update(
            {"infobase_path_identity_sha256": "d" * 64}
        ),
    ):
        changed = json.loads(json.dumps(completed))
        mutate(changed)
        checkpoint_path.write_text(json.dumps(changed), encoding="utf-8")
        with pytest.raises(BenchmarkContractError, match="identity|configuration"):
            run_cold_parent_schedule(
                schedule,
                checkpoint_path,
                spawn_sample=_sample,
                expected_reference=_reference(),
            )


def test_parse_parent_rejects_reference_or_implementation_drift() -> None:
    def spawn(batch_index: int, iterations: int) -> dict[str, object]:
        return {
            "batch_index": batch_index,
            "process_identity_sha256": f"{batch_index + 200:064x}",
            "warmup_iterations": 1,
            "measured_iterations": iterations,
            "samples_ms": [1.0] * iterations,
            "reference": _reference(),
            "implementation": benchmark_implementation_identity(),
            "database_file_identity_stable": True,
        }

    valid = run_parse_parent_batches(
        1,
        1,
        spawn_batch=spawn,
        expected_reference=_reference(),
    )
    assert len(valid) == 1

    def drifted(batch_index: int, iterations: int) -> dict[str, object]:
        result = spawn(batch_index, iterations)
        result["reference"] = {"wrong": True}
        return result

    with pytest.raises(BenchmarkContractError, match="parse child result"):
        run_parse_parent_batches(
            1,
            1,
            spawn_batch=drifted,
            expected_reference=_reference(),
        )


def test_cold_phase_contract_requires_complete_two_module_first_load() -> None:
    phases = _cold_phases(1.0)

    validate_cold_phase_contract(
        phases,
        {
            "full_module_parses": 2,
            "delta_method_parses": 0,
            "packaging_validation_parses": 0,
            "worker_profile_parses": 0,
        },
    )

    phases["semantic_parse"]["calls"] = 1
    with pytest.raises(BenchmarkContractError, match="phase contract"):
        validate_cold_phase_contract(phases, {"full_module_parses": 2})

    phases = _cold_phases(1.0)
    with pytest.raises(BenchmarkContractError, match="parser phase contract"):
        validate_cold_phase_contract(
            phases,
            {
                "full_module_parses": 2,
                "delta_method_parses": False,
                "packaging_validation_parses": 0,
                "worker_profile_parses": 0,
            },
        )


def test_cold_accounting_subtracts_each_top_level_phase_once() -> None:
    phases = _cold_phases(1.0)
    for evidence in phases.values():
        evidence["wall_ms"] = 0.0
        evidence["cpu_ms"] = 0.0
    for phase, wall_ms in {
        "tokenize": 10.0,
        "server_token_filter": 2.0,
        "semantic_parse": 20.0,
        "ast_model_extract": 5.0,
        "artifact_staging": 7.0,
        "generation_create_wire_probe": 11.0,
        "root_swap": 1.0,
        "end_to_end": 70.0,
        "full_ast_generated_parse": 20.0,
        "full_ast_model_extract": 5.0,
        "artifact_stage_sealed_validation": 1.0,
        "artifact_stage_batch": 6.0,
        "artifact_stage_base64": 2.0,
        "artifact_stage_executor": 3.0,
    }.items():
        phases[phase]["wall_ms"] = wall_ms

    assert benchmark.cold_phase_accounting(phases, end_to_end_ms=80.0) == {
        "host_orchestration_exclusive_ms": 14.0,
        "runtime_transport_promotion_ms": 19.0,
    }


def test_cold_accounting_accepts_slow_pre_staging_sealed_validation() -> None:
    phases = _cold_phases(1.0)
    for evidence in phases.values():
        evidence["wall_ms"] = 0.0
        evidence["cpu_ms"] = 0.0
    for phase, wall_ms in {
        "artifact_stage_sealed_validation": 30.0,
        "artifact_stage_base64": 0.1,
        "artifact_stage_executor": 0.7,
        "artifact_stage_batch": 0.9,
        "artifact_staging": 1.0,
        "end_to_end": 40.0,
    }.items():
        phases[phase]["wall_ms"] = wall_ms

    assert benchmark.cold_phase_accounting(phases, end_to_end_ms=40.0) == {
        "host_orchestration_exclusive_ms": 39.0,
        "runtime_transport_promotion_ms": 1.0,
    }


def test_cold_accounting_accepts_sealed_validation_equal_to_host_residual() -> None:
    phases = _cold_phases(1.0)
    for evidence in phases.values():
        evidence["wall_ms"] = 0.0
        evidence["cpu_ms"] = 0.0
    phases["artifact_stage_sealed_validation"]["wall_ms"] = 39.0
    phases["artifact_stage_batch"]["wall_ms"] = 1.0
    phases["artifact_staging"]["wall_ms"] = 1.0
    phases["end_to_end"]["wall_ms"] = 40.0

    assert benchmark.cold_phase_accounting(phases, end_to_end_ms=40.0) == {
        "host_orchestration_exclusive_ms": 39.0,
        "runtime_transport_promotion_ms": 1.0,
    }


def test_cold_accounting_rejects_sealed_validation_above_host_residual() -> None:
    phases = _cold_phases(1.0)
    for evidence in phases.values():
        evidence["wall_ms"] = 0.0
        evidence["cpu_ms"] = 0.0
    phases["artifact_stage_sealed_validation"]["wall_ms"] = 50.0
    phases["artifact_stage_batch"]["wall_ms"] = 1.0
    phases["artifact_staging"]["wall_ms"] = 1.0
    phases["end_to_end"]["wall_ms"] = 40.0

    with pytest.raises(BenchmarkContractError, match="phase accounting"):
        benchmark.cold_phase_accounting(phases, end_to_end_ms=40.0)


@pytest.mark.parametrize(
    "mutate",
    (
        lambda phases: phases.pop("artifact_stage_executor"),
        lambda phases: phases["artifact_stage_batch"].update(
            {"wall_ms": phases["artifact_staging"]["wall_ms"] + 1.0}
        ),
        lambda phases: phases["artifact_stage_executor"].update(
            {"wall_ms": phases["artifact_stage_batch"]["wall_ms"]}
        ),
        lambda phases: phases["artifact_stage_base64"].update(
            {"wall_ms": phases["artifact_stage_batch"]["wall_ms"]}
        ),
        lambda phases: phases["semantic_parse"].update({"wall_ms": float("nan")}),
    ),
)
def test_cold_accounting_rejects_missing_overlapping_or_malformed_phases(
    mutate: object,
) -> None:
    phases = _cold_phases(100.0)
    mutate(phases)

    with pytest.raises(BenchmarkContractError, match="phase accounting"):
        benchmark.cold_phase_accounting(phases, end_to_end_ms=100.0)


@pytest.mark.parametrize(
    ("recorded_end_to_end_ms", "external_end_to_end_ms"),
    (
        (0.01, 100.0),
        (101.0, 100.0),
        (True, 100.0),
        (float("nan"), 100.0),
        (float("inf"), 100.0),
        (100.0, True),
        (100.0, float("nan")),
        (100.0, float("inf")),
    ),
)
def test_cold_accounting_authenticates_recorded_end_to_end_parent(
    recorded_end_to_end_ms: object,
    external_end_to_end_ms: object,
) -> None:
    phases = _cold_phases(100.0)
    phases["end_to_end"]["wall_ms"] = recorded_end_to_end_ms

    with pytest.raises(BenchmarkContractError, match="phase accounting"):
        benchmark.cold_phase_accounting(
            phases,
            end_to_end_ms=external_end_to_end_ms,
        )


def test_cold_transport_contract_rejects_extra_executor_call() -> None:
    transport = _cold_transport()
    transport["executor_count"] = 2

    with pytest.raises(BenchmarkContractError, match="transport"):
        benchmark.validate_cold_transport_contract(transport)


def test_cold_transport_contract_rejects_boolean_batch_count() -> None:
    transport = _cold_transport()
    transport["batches"][0]["executor_count"] = True

    with pytest.raises(BenchmarkContractError, match="transport"):
        benchmark.validate_cold_transport_contract(transport)


def test_final_report_has_per_mode_first_percentiles_and_strict_sla() -> None:
    schedule = build_cold_schedule(2, ("MAIN", "CAPTURE"))
    samples = [_sample(request) for request in schedule]
    for sample in samples:
        phases = _cold_phases(float(sample["sequence"]))
        sample.update(
            {
                "cleanup_passed": True,
                "semantic_canary_passed": True,
                "target_filesystem_dependency": False,
                "database_file_identity_stable": True,
                "phases": phases,
                "parser_calls": {
                    "full_module_parses": 2,
                    "delta_method_parses": 0,
                    "packaging_validation_parses": 0,
                    "worker_profile_parses": 0,
                },
                **benchmark.cold_phase_accounting(
                    phases,
                    end_to_end_ms=float(sample["end_to_end_ms"]),
                ),
                "transport": _cold_transport(),
            }
        )
    checkpoint = {
        "schema": "onec-minimal-worker-reload-checkpoint-v2",
        "configuration": {
            "cold_reloads": 2,
            "modes": ["MAIN", "CAPTURE"],
            "reference": _reference(),
            "implementation": benchmark_implementation_identity(),
        },
        "cold_samples": samples,
    }
    parse_batches = [
        {
            "batch_index": index,
            "process_identity_sha256": f"{index + 100:064x}",
            "warmup_iterations": 1,
            "measured_iterations": 2,
            "samples_ms": [10.0, 20.0],
            "reference": _reference(),
            "implementation": benchmark_implementation_identity(),
            "database_file_identity_stable": True,
        }
        for index in (1, 2, 3)
    ]

    report = assemble_final_report(
        checkpoint,
        parse_batches,
        sla_ms=2_500.0,
        expected_reference=_reference(),
    )

    assert report["status"] == "PASS"
    assert report["methodology"]["worker_reload_warmups"] == 0
    assert report["methodology"]["runtime_start_excluded"] is True
    assert report["methodology"]["derived_subtotals"] == [
        "runtime_transport_promotion"
    ]
    assert report["summary"]["MAIN"] == {
        "count": 2,
        "first_ms": 1.0,
        "p50_ms": 1.0,
        "p95_ms": 4.0,
        "max_ms": 4.0,
        "sla_passed": True,
    }
    assert report["summary"]["CAPTURE"]["first_ms"] == 2.0
    assert report["phase_summary"]["MAIN"]["semantic_parse"] == {
        "count": 2,
        "first_ms": 0.01,
        "p50_ms": 0.01,
        "p95_ms": 0.04,
        "max_ms": 0.04,
    }
    assert report["phase_summary"]["CAPTURE"][
        "runtime_transport_promotion"
    ]["p95_ms"] == 0.09

    slow = [dict(sample) for sample in samples]
    slow[-1]["end_to_end_ms"] = 2_500.0
    slow[-1].update(
        benchmark.cold_phase_accounting(
            slow[-1]["phases"],
            end_to_end_ms=2_500.0,
        )
    )
    checkpoint["cold_samples"] = slow
    assert assemble_final_report(
        checkpoint,
        parse_batches,
        sla_ms=2_500.0,
        expected_reference=_reference(),
    )[
        "status"
    ] == "NON_PASS"

    unstable = [dict(sample) for sample in samples]
    unstable[-1]["database_file_identity_stable"] = False
    checkpoint["cold_samples"] = unstable
    assert assemble_final_report(
        checkpoint,
        parse_batches,
        sla_ms=2_500.0,
        expected_reference=_reference(),
    )["status"] == "NON_PASS"


@pytest.mark.parametrize(
    "private_value",
    (
        "C:/private/source/module.bsl",
        "00000000-0000-4000-8000-000000000000",
        "Процедура Секрет()\nКонецПроцедуры",
        "token=secret",
    ),
)
def test_public_evidence_rejects_paths_source_uuid_and_credentials(
    private_value: str,
) -> None:
    with pytest.raises(BenchmarkContractError, match="private"):
        validate_public_evidence({"safe": {"value": private_value}})


def test_public_evidence_accepts_hashes_metrics_and_module_names() -> None:
    validate_public_evidence(
        {
            "source": {
                "logical_name": "КадровыйУчет",
                "sha256": "a" * 64,
                "bytes": 833135,
            },
            "p95_ms": 2100.5,
            "target_filesystem_dependency": False,
        }
    )
