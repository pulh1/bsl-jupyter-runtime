from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

import tools.onec_runtime_probe as cli
from tools.onec_runtime_probe import parser


def test_worker_universe_zup_acceptance_uses_fixed_protocol_defaults() -> None:
    arguments = parser().parse_args(["worker-universe-zup-acceptance"])

    assert (arguments.iterations, arguments.warmup_iterations) == (60, 3)


@pytest.mark.parametrize(
    "command",
    (
        "capture-spike",
        "prototype-e2e-spike",
        "reentrancy-spike",
        "transport-recovery-spike",
        "controller-supervisor-spike",
        "server-hot-reload-spike",
        "capture-hot-reload-spike",
        "hot-reload-transaction-spike",
        "zup-cross-module-reload-spike",
        "jupyter-adapter-spike",
        "server-to-df-spike",
        "value-materialization-spike",
        "server-stress-spike",
        "server-soak-spike",
        "capture-cycle-diagnostic-spike",
    ),
)
def test_removed_spike_cli_commands_are_not_advertised(command: str) -> None:
    with pytest.raises(SystemExit):
        parser().parse_args([command])


@pytest.mark.parametrize(
    "status",
    ["PASS", "incompatible_prerequisites", "incompatible_instrumentation"],
)
def test_worker_universe_zup_acceptance_routes_compact_verified_evidence(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: str,
) -> None:
    calls: list[tuple[object, int, int, Path]] = []
    config = object()
    run_dir = Path("worker-universe-zup-evidence")
    monkeypatch.setenv("ONEC_ZUP_SOURCE_ROOT", "approved-source-root")
    monkeypatch.setattr(cli, "_config", lambda args: config)
    monkeypatch.setattr(
        cli,
        "run_worker_universe_zup_acceptance",
        lambda actual, *, iterations, warmup_iterations, source_root: (
            calls.append((actual, iterations, warmup_iterations, source_root))
            or run_dir
        ),
    )
    monkeypatch.setattr(
        cli,
        "verify_worker_universe_zup_evidence",
        lambda actual: {"status": status} if actual == run_dir else {},
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "onec-runtime",
            "worker-universe-zup-acceptance",
            "--iterations",
            "60",
            "--warmup-iterations",
            "3",
        ],
    )

    cli.main()

    assert calls == [(config, 60, 3, Path("approved-source-root"))]
    assert json.loads(capsys.readouterr().out) == {
        "status": status,
        "artifacts": str(run_dir),
    }
