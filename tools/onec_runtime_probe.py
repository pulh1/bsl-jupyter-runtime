from __future__ import annotations

import argparse
from datetime import timedelta
from hashlib import sha256
import json
import os
import platform
from pathlib import Path
import shutil
import sys
from time import monotonic

from onec_runtime.artifacts import ArtifactWriter, utc_now
from onec_runtime.bsl import LoweringMode, SemanticNotebookLowerer, WorkerExport
from onec_runtime.bsl.parser_target import PythonParserTarget
from onec_runtime.config import RuntimeConfig
from integration.zup_worker_universe_acceptance import (
    run_worker_universe_zup_acceptance,
    verify_worker_universe_zup_evidence,
)
from onec_runtime.errors import BslExecutionError
from onec_runtime.server_worker import build_worker_artifact
from onec_runtime.experiment import (
    INIT_COUNTER,
    INCREMENT,
    RAISE_ERROR,
    KernelExperiment,
    bsl_string_literal,
)
from onec_runtime.extension_bundle import packaged_extension_bundle
from onec_runtime.processes import FileModeProcesses
from onec_runtime.rdbg.models import ModuleLocation
from onec_runtime.rdbg.session import RdbgSession
from onec_runtime.rdbg.transport import RdbgTransport
from onec_runtime.table_value import OnecTableValue, evaluation_to_python
from onec_runtime.toolchain import (
    build_runtime_extension,
    create_build_infobase,
    create_empty_infobase,
    deploy_runtime_extension,
)


DEFAULT_PLATFORM = Path(r"C:\Program Files\1cv8\8.3.27.2170\bin")


def _hash(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _duration(value: str) -> float:
    parts = value.split(":")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("duration must be HH:MM:SS")
    hours, minutes, seconds = map(int, parts)
    return timedelta(hours=hours, minutes=minutes, seconds=seconds).total_seconds()


def _config(args: argparse.Namespace) -> RuntimeConfig:
    return RuntimeConfig(
        Path(args.workspace),
        Path(args.platform_bin),
        connection_string=args.connection_string,
        username=args.username,
    )


def provision(config: RuntimeConfig) -> None:
    create_empty_infobase(config)
    create_build_infobase(config)
    build_runtime_extension(config)
    deploy_runtime_extension(config)


def _copy_logs(config: RuntimeConfig, artifacts: ArtifactWriter) -> None:
    destination = artifacts.run_dir / "logs"
    destination.mkdir(exist_ok=True)
    if config.logs_dir.is_dir():
        for source in config.logs_dir.iterdir():
            if source.is_file():
                try:
                    shutil.copy2(source, destination / source.name)
                except PermissionError:
                    pass


def run_experiment(
    config: RuntimeConfig,
    *,
    scenario: str,
    iterations: int = 10,
    duration_s: float | None = None,
    command_timeout_s: float = 60.0,
    kernel_mode: str = "client",
    break_on_next: bool = False,
    thick_client: bool = False,
) -> Path:
    artifacts = ArtifactWriter(config.artifacts_dir, scenario)
    bundle = packaged_extension_bundle(config.runtime_dir)
    if kernel_mode == "server":
        module_path = (
            (config.workspace / "onec" / "OnecInteractiveRuntime")
            / "CommonModules"
            / "RuntimeKernelServer"
            / "Ext"
            / "Module.bsl"
        )
        location = bundle.manifest.breakpoints.server_service
    else:
        module_path = (config.workspace / "onec" / "OnecInteractiveRuntime") / "Ext" / "ManagedApplicationModule.bsl"
        location = bundle.manifest.breakpoints.managed
    artifacts.write_json(
        "environment.json",
        {
            "started_at": utc_now(),
            "scenario": scenario,
            "kernel_mode": kernel_mode,
            "python": sys.version,
            "os": platform.platform(),
            "platform_bin": str(config.platform_bin),
            "infobase": str(config.infobase_dir),
            "extension_cfe_sha256": _hash(bundle.cfe_path),
            "module_sha256": _hash(module_path),
            "location": {
                "module_type": location.module_type,
                "extension_name": location.extension_name,
                "object_id": str(location.object_id),
                "property_id": str(location.property_id),
                "line": location.line,
            },
        },
    )
    processes = FileModeProcesses(config)
    transport: RdbgTransport | None = None
    session: RdbgSession | None = None
    status = "FAIL"
    error = ""
    started = monotonic()
    try:
        debug_port = processes.start_debug_server()
        transport = RdbgTransport(
            config.debug_host, debug_port, transcript=artifacts.transcript
        )
        startup_location = bundle.manifest.breakpoints.managed
        session = RdbgSession(
            transport, startup_location, break_on_next=break_on_next
        )
        session.initialize()
        session.set_service_breakpoint()
        debuggee = processes.start_debuggee(
            debug_port, execute_external=False, thick_client=thick_client
        )
        artifacts.write_json(
            "processes.json",
            {
                "python_pid": __import__("os").getpid(),
                "dbgs_pid": processes.debug_server.pid if processes.debug_server else None,
                "onec_pid": debuggee.pid,
                "debug_port": debug_port,
            },
        )
        session.wait_for_service_stop(timeout_s=90.0)
        if kernel_mode == "server":
            session.modify(
                "ТекущаяИнструкция",
                bsl_string_literal("RuntimeKernelServer.Запустить();"),
            )
            session.modify("ИдентификаторКоманды", "1")
            session.expected_location = location
            session.set_service_breakpoint()
            session.continue_()
            session.wait_for_service_stop(timeout_s=90.0)
        experiment = KernelExperiment(
            session, processes, artifacts, command_timeout_s=command_timeout_s
        )
        experiment.python_context["marker"] = 777
        initialized = experiment.execute(INIT_COUNTER)
        if initialized.observed_counter != 0:
            raise RuntimeError(f"Counter initialized to {initialized.observed_counter}")
        next_heartbeat = monotonic() + 120.0
        if scenario == "exception-recovery":
            try:
                experiment.execute(RAISE_ERROR)
            except BslExecutionError:
                pass
            recovered = experiment.execute(INCREMENT)
            if recovered.observed_counter != 1:
                raise RuntimeError("Counter did not recover after the planned exception")
        elif scenario == "hot-reload":
            worker_v1 = config.build_dir / "Worker-v1.epf"
            worker_v2 = config.build_dir / "Worker-v2.epf"
            for worker in (worker_v1, worker_v2):
                if not worker.is_file():
                    raise RuntimeError(f"Missing hot-reload worker: {worker}")
            path_v1 = str(worker_v1).replace('"', '""')
            path_v2 = str(worker_v2).replace('"', '""')
            first = experiment.execute(
                'Контекст.Вставить("Маркер", 777); '
                f'Контекст.Вставить("Расчет", ВнешниеОбработки.Создать("{path_v1}", Ложь)); '
                'Контекст.Вставить("НДФЛ", Контекст.Расчет.Посчитать()); '
                'Контекст.Расчет.ИзменитьРезультат(Контекст.НДФЛ); '
                'Результат = Контекст.Расчет.Версия();'
            )
            if first.result.strip('"') != "v1":
                raise RuntimeError(f"Worker v1 returned {first.result}")
            second = experiment.execute(
                f'Контекст.Вставить("Расчет", ВнешниеОбработки.Создать("{path_v2}", Ложь)); '
                'Контекст.Вставить("НДФЛ", Контекст.Расчет.Посчитать()); '
                'Контекст.Расчет.ИзменитьРезультат(Контекст.НДФЛ); '
                'Результат = Контекст.Расчет.Версия();'
            )
            if second.result.strip('"') != "v2":
                raise RuntimeError(f"Worker v2 returned {second.result}")
            checks = {
                expression: evaluation_to_python(session.evaluate(expression))
                for expression in (
                    "Контекст.Маркер",
                    "Контекст.НДФЛ",
                    "Контекст.Расчет.ПолучитьРезультат()",
                    "Контекст.Расчет.Версия()",
                )
            }
            if checks != {
                "Контекст.Маркер": 777,
                "Контекст.НДФЛ": 29,
                "Контекст.Расчет.ПолучитьРезультат()": 29,
                "Контекст.Расчет.Версия()": "v2",
            }:
                raise RuntimeError(f"Hot-reload checks failed: {checks}")
            artifacts.write_json("hot-reload.json", checks)
        elif scenario == "table-to-df":
            experiment.execute(
                'Таблица = Новый ТаблицаЗначений; '
                'Таблица.Колонки.Добавить("Имя"); Таблица.Колонки.Добавить("Сумма"); '
                'СтрокаТаблицы = Таблица.Добавить(); СтрокаТаблицы.Имя = "А"; СтрокаТаблицы.Сумма = 10; '
                'СтрокаТаблицы = Таблица.Добавить(); СтрокаТаблицы.Имя = "Б"; СтрокаТаблицы.Сумма = 20; '
                'Контекст.Вставить("Таблица", Таблица); Результат = Таблица.Количество();'
            )
            frame = OnecTableValue(session, "Контекст.Таблица").to_df_legacy()
            rows = frame.to_dict(orient="records")
            expected_rows = [{"Имя": "А", "Сумма": 10}, {"Имя": "Б", "Сумма": 20}]
            if rows != expected_rows:
                raise RuntimeError(f"to_df mismatch: {rows}")
            artifacts.write_json(
                "dataframe.json",
                {"columns": list(frame.columns), "dtypes": frame.dtypes.astype(str).to_dict(), "rows": rows},
            )
        elif scenario == "grammar-cell":
            worker = config.build_dir / "Worker-v2.epf"
            if not worker.is_file():
                raise RuntimeError(f"Missing grammar-cell worker: {worker}")
            worker_source = (
                config.workspace
                / "onec"
                / "Worker"
                / "Worker"
                / "Ext"
                / "ObjectModule.bsl"
            )
            catalog = (
                WorkerExport("Расчет.Ндфл.Посчитать", "Посчитать"),
                WorkerExport("Расчет.Ндфл.ИзменитьРезультат", "ИзменитьРезультат"),
                WorkerExport("Расчет.Ндфл.Версия", "Версия"),
            )
            worker_artifact = build_worker_artifact(
                logical_name="Worker",
                source_path=worker_source,
                artifact_path=worker,
                expected_version="v2",
                expected_value=29,
                exports=catalog,
            )
            worker_path = str(worker).replace('"', '""')
            experiment.execute(
                f'Контекст.Вставить("RuntimeWorker", ВнешниеОбработки.Создать("{worker_path}", Ложь)); '
                'Результат = Контекст.RuntimeWorker.Версия();'
            )
            visible = (
                "НДФЛ = Расчет.Ндфл.Посчитать(); "
                "Расчет.Ндфл.ИзменитьРезультат(НДФЛ);"
            )
            target = PythonParserTarget.from_generated()
            lowered = SemanticNotebookLowerer(
                target,
                worker_exports=worker_artifact.exports,
            ).lower(visible, mode=LoweringMode.MAIN).source
            sent = lowered + " Результат = Контекст.НДФЛ;"
            experiment.execute(sent)
            observed_instruction = evaluation_to_python(
                session.evaluate("ТекущаяИнструкция")
            )
            if observed_instruction != sent:
                raise RuntimeError(
                    "1C instruction trace differs from sent lowering: "
                    f"{observed_instruction!r}"
                )
            checks = {
                expression: evaluation_to_python(session.evaluate(expression))
                for expression in (
                    "Контекст.НДФЛ",
                    "Контекст.RuntimeWorker.ПолучитьРезультат()",
                )
            }
            if checks != {
                "Контекст.НДФЛ": 29,
                "Контекст.RuntimeWorker.ПолучитьРезультат()": 29,
            }:
                raise RuntimeError(f"Grammar-cell checks failed: {checks}")
            artifacts.write_json(
                "grammar-cell.json",
                {
                    "visible": visible,
                    "lowered": lowered,
                    "sent": sent,
                    "observed_by_1c": observed_instruction,
                    "checks": checks,
                },
            )
        elif duration_s is not None:
            counter = 0
            deadline = monotonic() + duration_s
            while monotonic() < deadline:
                if monotonic() >= next_heartbeat:
                    experiment.heartbeat()
                    next_heartbeat = monotonic() + 120.0
                counter += 1
                record = experiment.execute(INCREMENT)
                if record.observed_counter != counter:
                    raise RuntimeError(
                        f"Counter mismatch: {record.observed_counter}, expected {counter}"
                    )
        else:
            for counter in range(1, iterations + 1):
                if monotonic() >= next_heartbeat:
                    experiment.heartbeat()
                    next_heartbeat = monotonic() + 120.0
                record = experiment.execute(INCREMENT)
                if record.observed_counter != counter:
                    raise RuntimeError(
                        f"Counter mismatch: {record.observed_counter}, expected {counter}"
                    )
        if experiment.python_context.get("marker") != 777:
            raise RuntimeError("Python context marker did not survive the experiment")
        experiment.python_context["survived"] = True
        artifacts.write_json("python-context.json", experiment.python_context)
        status = "PASS"
    except BaseException as caught:
        error = f"{type(caught).__name__}: {caught}"
        raise
    finally:
        if session is not None:
            try:
                session.detach()
            except Exception:
                pass
        if transport is not None:
            transport.close()
        processes.close()
        _copy_logs(config, artifacts)
        artifacts.write_json(
            "summary.json",
            {
                "status": status,
                "error": error,
                "duration_s": monotonic() - started,
                "finished_at": utc_now(),
            },
        )
    return artifacts.run_dir


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--workspace", default=str(Path.cwd()))
    result.add_argument("--platform-bin", default=str(DEFAULT_PLATFORM))
    result.add_argument("--command-timeout", type=float, default=60.0)
    result.add_argument("--connection-string")
    result.add_argument("--username", default="")
    result.add_argument("--kernel-mode", choices=("server", "client"), default="client")
    result.add_argument("--break-on-next", action="store_true")
    result.add_argument("--thick-client", action="store_true")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("provision")
    worker_universe_zup = commands.add_parser(
        "worker-universe-zup-acceptance"
    )
    worker_universe_zup.add_argument("--iterations", type=int, default=60)
    worker_universe_zup.add_argument(
        "--warmup-iterations", type=int, default=3
    )
    run = commands.add_parser("run")
    run.add_argument("--iterations", type=int, default=10)
    run.add_argument(
        "--scenario",
        choices=("counter", "exception-recovery", "hot-reload", "table-to-df", "grammar-cell"),
        default="counter",
    )
    soak = commands.add_parser("soak")
    soak.add_argument("--duration", type=_duration, default=_duration("01:00:00"))
    return result


def main() -> None:
    args = parser().parse_args()
    config = _config(args)
    if args.command == "provision":
        provision(config)
        print("PASS: dedicated runtime/build infobases and OnecInteractiveRuntime.cfe are ready")
        return
    if args.command == "worker-universe-zup-acceptance":
        configured_source = os.environ.get("ONEC_ZUP_SOURCE_ROOT", "").strip()
        run_dir = run_worker_universe_zup_acceptance(
            config,
            iterations=args.iterations,
            warmup_iterations=args.warmup_iterations,
            source_root=Path(configured_source) if configured_source else None,
        )
        outcome = verify_worker_universe_zup_evidence(run_dir)
        print(
            json.dumps(
                {"status": outcome["status"], "artifacts": str(run_dir)},
                ensure_ascii=False,
            )
        )
        return
    if args.command == "run":
        run_dir = run_experiment(
            config,
            scenario=args.scenario,
            iterations=args.iterations,
            command_timeout_s=args.command_timeout,
            kernel_mode=args.kernel_mode,
            break_on_next=args.break_on_next,
            thick_client=args.thick_client,
        )
    else:
        run_dir = run_experiment(
            config,
            scenario="soak",
            duration_s=args.duration,
            command_timeout_s=args.command_timeout,
            kernel_mode=args.kernel_mode,
            break_on_next=args.break_on_next,
            thick_client=args.thick_client,
        )
    print(json.dumps({"status": "PASS", "artifacts": str(run_dir)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
