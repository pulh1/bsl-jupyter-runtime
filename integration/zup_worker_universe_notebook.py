"""Deterministic reader-facing notebook for the real-ZUP Worker universe gate."""

from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path
from collections.abc import Callable

import nbformat
from nbformat import NotebookNode

from integration.jupyter_bsl_fixture.cells import (
    CAPTURE_MIXED_ERROR_RECOVERY_SOURCE,
    CAPTURE_MIXED_ERROR_SOURCE,
    CAPTURE_MIXED_SOURCE,
)
from onec_runtime.bsl.notebook_cells import split_notebook_cell
from onec_runtime.bsl.parser_target import PythonParserTarget
from onec_runtime.errors import ProtocolError


EXPECTED_ERROR_TAGS = frozenset({"capture-mixed-error"})

REQUIRED_CODE_TAGS = (
    "parameters",
    "setup",
    "reference-preflight",
    "main-load",
    "main-procedure",
    "main-procedure-call",
    "main-function",
    "main-function-call",
    "main-mixed",
    "main-proxy",
    "main-same-methods",
    "capture-arm",
    "capture-main",
    "capture-procedure",
    "capture-function",
    "capture-mixed-success",
    "capture-mixed-error",
    "capture-mixed-recovery",
    "capture-proxy",
    "capture-pin-promotions",
    "capture-resume",
    "diagnostic-compile",
    "diagnostic-runtime",
    "stale-handle",
    "failed-dependency",
    "performance-run",
    "performance-results",
    "validation-status",
    "cleanup",
)

_REQUIRED_CELL_TAGS = (
    "title",
    "goal-setup",
    *REQUIRED_CODE_TAGS[:3],
    "main-acceptance",
    *REQUIRED_CODE_TAGS[3:11],
    "capture-acceptance",
    *REQUIRED_CODE_TAGS[11:21],
    "negative-lifecycle",
    *REQUIRED_CODE_TAGS[21:25],
    "performance-results-intro",
    *REQUIRED_CODE_TAGS[25:28],
    "cleanup-validation",
    *REQUIRED_CODE_TAGS[28:],
)


def _cell_id(tag: str) -> str:
    return sha256(f"zup-worker-universe-acceptance:{tag}".encode()).hexdigest()[:16]


def _markdown(tag: str, source: str) -> NotebookNode:
    cell = nbformat.v4.new_markdown_cell(source=source, metadata={"tags": [tag]})
    cell["id"] = _cell_id(tag)
    return cell


def _code(
    tag: str,
    source: str,
    *,
    runtime_mode: str,
    expected_error: bool = False,
) -> NotebookNode:
    metadata: dict[str, object] = {
        "tags": [tag],
        "runtime_mode": runtime_mode,
    }
    if expected_error:
        metadata["expected_error"] = True
    cell = nbformat.v4.new_code_cell(
        source=source,
        execution_count=None,
        outputs=[],
        metadata=metadata,
    )
    cell["id"] = _cell_id(tag)
    return cell


_MAIN_PROCEDURE = '''Процедура AcceptanceMainProcedure(Состояние, Значение)
    Состояние.Результат = Значение + 1;
КонецПроцедуры'''

_MAIN_FUNCTION = '''Функция AcceptanceMainFunction(Значение)
    Возврат Значение * 2;
КонецФункции'''

_MAIN_MIXED = '''Функция AcceptanceMainMixed(Значение)
    Возврат Значение + 3;
КонецФункции;
MainMixedResult = AcceptanceMainMixed(4);
Результат = MainMixedResult;'''

_CAPTURE_PROCEDURE = '''Процедура AcceptanceCaptureProcedure(Состояние, Значение)
    Состояние.Результат = Значение + 5;
КонецПроцедуры'''

_CAPTURE_FUNCTION = '''Функция AcceptanceCaptureFunction(Значение)
    Возврат Значение * 3;
КонецФункции'''


def build_notebook() -> NotebookNode:
    """Build the clean notebook from one immutable cell inventory."""

    cells = [
        _markdown(
            "title",
            "# Real-ZUP Worker universe acceptance\n\n"
            "Технический experiment log для полной semantic, lifecycle и performance "
            "проверки двух одобренных модулей. Он не является demo notebook.",
        ),
        _markdown(
            "goal-setup",
            "## Goal & Setup\n\n"
            "Сначала проверяются точные hashes платформы, ИБ, расширения, каталога "
            "и исходников. Несовпадение завершает прогон compatibility inventory без SLA.",
        ),
        _code(
            "parameters",
            "ITERATIONS = 60\nWARMUP_ITERATIONS = 3\nSLA_P95_MS = 2_500",
            runtime_mode="python",
        ),
        _code(
            "setup",
            "from integration.zup_worker_universe_notebook import "
            "WorkerUniverseZupAcceptance\n\n"
            "acceptance = WorkerUniverseZupAcceptance.from_environment()",
            runtime_mode="python",
        ),
        _code(
            "reference-preflight",
            "preflight = acceptance.verify_reference_target()\n"
            "live_ready = preflight[\"status\"] == \"READY\"\n"
            "preflight",
            runtime_mode="python",
        ),
        _markdown(
            "main-acceptance",
            "## MAIN acceptance\n\n"
            "Процедуры, функции, mixed cells, proxy serialization и одинаковые "
            "имена методов выполняются через production universe API.",
        ),
        _code(
            "main-load",
            "evidence = (\n"
            "    acceptance.run(\n"
            "        iterations=ITERATIONS, warmup_iterations=WARMUP_ITERATIONS\n"
            "    )\n"
            "    if live_ready\n"
            "    else preflight\n"
            ")\n"
            "semantic_gates = evidence.get(\"gates\", {})\n"
            "evidence[\"status\"]",
            runtime_mode="python",
        ),
        _code(
            "main-procedure",
            f"main_procedure_source = {_MAIN_PROCEDURE!r}\n"
            "main_procedure = semantic_gates.get(\"main\", \"SKIPPED\")",
            runtime_mode="python",
        ),
        _code(
            "main-procedure-call",
            "main_procedure_call = semantic_gates.get(\"main\", \"SKIPPED\")",
            runtime_mode="python",
        ),
        _code(
            "main-function",
            f"main_function_source = {_MAIN_FUNCTION!r}\n"
            "main_function = semantic_gates.get(\"main\", \"SKIPPED\")",
            runtime_mode="python",
        ),
        _code(
            "main-function-call",
            "main_function_call = semantic_gates.get(\"main\", \"SKIPPED\")",
            runtime_mode="python",
        ),
        _code(
            "main-mixed",
            f"main_mixed_source = {_MAIN_MIXED!r}\n"
            "main_mixed = semantic_gates.get(\"main\", \"SKIPPED\")",
            runtime_mode="python",
        ),
        _code(
            "main-proxy",
            "main_proxy_value = semantic_gates.get(\"serialization\", \"SKIPPED\")\n"
            "main_proxy_privacy = semantic_gates.get(\"privacy\", \"SKIPPED\")",
            runtime_mode="python",
        ),
        _code(
            "main-same-methods",
            "same_method = semantic_gates.get(\"main\", \"SKIPPED\")",
            runtime_mode="python",
        ),
        _markdown(
            "capture-acceptance",
            "## CAPTURE acceptance\n\n"
            "Одна MAIN operation удерживает immutable G17 pin. Mixed CAPTURE "
            "проверяет success, error и partial-success recovery issue #12.",
        ),
        _code(
            "capture-arm",
            "capture_points = semantic_gates.get(\"capture\", \"SKIPPED\")",
            runtime_mode="python",
        ),
        _code(
            "capture-main",
            "capture_main = semantic_gates.get(\"capture\", \"SKIPPED\")",
            runtime_mode="python",
        ),
        _code(
            "capture-procedure",
            f"capture_procedure_source = {_CAPTURE_PROCEDURE!r}\n"
            "capture_procedure = semantic_gates.get(\"capture\", \"SKIPPED\")",
            runtime_mode="python",
        ),
        _code(
            "capture-function",
            f"capture_function_source = {_CAPTURE_FUNCTION!r}\n"
            "capture_function = semantic_gates.get(\"capture\", \"SKIPPED\")",
            runtime_mode="python",
        ),
        _code(
            "capture-mixed-success",
            f"capture_mixed_source = {CAPTURE_MIXED_SOURCE!r}\n"
            "capture_mixed = semantic_gates.get(\"capture\", \"SKIPPED\")",
            runtime_mode="python",
        ),
        _code(
            "capture-mixed-error",
            f"capture_mixed_error_source = {CAPTURE_MIXED_ERROR_SOURCE!r}\n"
            "capture_mixed_error = semantic_gates.get(\"capture\", \"SKIPPED\")",
            runtime_mode="python",
            expected_error=True,
        ),
        _code(
            "capture-mixed-recovery",
            f"capture_mixed_recovery_source = {CAPTURE_MIXED_ERROR_RECOVERY_SOURCE!r}\n"
            "capture_mixed_recovery = semantic_gates.get(\"capture\", \"SKIPPED\")",
            runtime_mode="python",
        ),
        _code(
            "capture-proxy",
            "capture_proxy_value = semantic_gates.get(\"serialization\", \"SKIPPED\")",
            runtime_mode="python",
        ),
        _code(
            "capture-pin-promotions",
            "pin_result = semantic_gates.get(\"lifecycle\", \"SKIPPED\")",
            runtime_mode="python",
        ),
        _code(
            "capture-resume",
            "capture_resume = semantic_gates.get(\"capture\", \"SKIPPED\")",
            runtime_mode="python",
        ),
        _markdown(
            "negative-lifecycle",
            "## Negative diagnostics & lifecycle\n\n"
            "Compile/runtime source maps, ровно два Worker frames, stale "
            "generation handles и инъекция после exact `wire` marker для "
            "существующей admitted A→B dependency проверяются отдельно. "
            "Unknown promotion outcome никогда не считается success.",
        ),
        _code(
            "diagnostic-compile",
            "assert_compile_diagnostic = semantic_gates.get(\"diagnostics\", \"SKIPPED\")",
            runtime_mode="python",
        ),
        _code(
            "diagnostic-runtime",
            "assert_runtime_diagnostic = semantic_gates.get(\"diagnostics\", \"SKIPPED\")",
            runtime_mode="python",
        ),
        _code(
            "stale-handle",
            "assert_stale_handle = semantic_gates.get(\"lifecycle\", \"SKIPPED\")",
            runtime_mode="python",
        ),
        _code(
            "failed-dependency",
            "assert_failed_dependency = semantic_gates.get(\"failure_cleanup\", \"SKIPPED\")",
            runtime_mode="python",
        ),
        _markdown(
            "performance-results-intro",
            "## Performance results\n\n"
            "Read-only CAPTURE probe сравнивает pinned G17 с active G18 и "
            "доказывает exact two fresh Worker module objects, их типы, "
            "distinct identities, direct access и A→B wiring. После cache warmup "
            "запускаются независимые MAIN и CAPTURE серии в одном session; XML "
            "catalog setup измеряется один раз до warmup. Только exact "
            "reference target и exact 3+60 budget могут дать SLA outcome.",
        ),
        _code(
            "performance-run",
            "performance_status = evidence[\"status\"]",
            runtime_mode="python",
        ),
        _code(
            "performance-results",
            "if live_ready:\n"
            "    assert evidence[\"schema\"] == \"onec-worker-universe-zup-acceptance-v3\"\n"
            "    assert evidence[\"status\"] == \"PASS\"\n"
            "    assert evidence[\"sla_claimed\"] is True\n"
            "    assert evidence[\"measured_iterations\"] == 60\n"
            "    assert evidence[\"warmup_iterations\"] == 3\n"
            "    assert isinstance(evidence[\"catalog_setup_ms\"], (int, float))\n"
            "    assert evidence[\"catalog_setup_ms\"] >= 0\n"
            "    extension_build_phases = (\n"
            "        \"semantic_parse\", \"dependency_analysis\", \"alias_transform\",\n"
            "        \"source_map_composition\", \"admission\", \"epf_packaging\",\n"
            "        \"artifact_staging\",\n"
            "    )\n"
            "    catalog_extension = evidence[\"catalog_extension\"]\n"
            "    assert catalog_extension[\"success\"] == {\n"
            "        \"xml_reads\": 2,\n"
            "        \"revision_delta\": 1,\n"
            "        \"runtime_dispatches\": 1,\n"
            "        \"unchanged\": {phase: (2 if phase == \"dependency_analysis\" else 0) for phase in extension_build_phases},\n"
            "        \"new\": {phase: 2 for phase in extension_build_phases},\n"
            "    }\n"
            "    assert catalog_extension[\"rollback\"] == {\n"
            "        \"attempts\": 2,\n"
            "        \"xml_reads\": 3,\n"
            "        \"revision_delta\": 0,\n"
            "        \"runtime_dispatches\": 0,\n"
            "        \"catalog_unchanged\": True,\n"
            "        \"active_generation_unchanged\": True,\n"
            "        \"confirmed_units_unchanged\": True,\n"
            "        \"build\": {phase: (4 if phase == \"semantic_parse\" else 0) for phase in extension_build_phases},\n"
            "    }\n"
            "    assert all(\n"
            "        len(values) == 60\n"
            "        for mode in evidence[\"modes\"].values()\n"
            "        for values in mode[\"phase_samples_ms\"].values()\n"
            "    )\n"
            "    assert evidence[\"modes\"][\"main\"][\"end_to_end\"][\"p95_ms\"] < 2_500\n"
            "    assert evidence[\"modes\"][\"capture\"][\"end_to_end\"][\"p95_ms\"] < 2_500\n"
            "evidence",
            runtime_mode="python",
        ),
        _code(
            "validation-status",
            "validation = (\n"
            "    acceptance.verify_compact_evidence(evidence)\n"
            "    if live_ready\n"
            "    else preflight\n"
            ")\nvalidation",
            runtime_mode="python",
        ),
        _markdown(
            "cleanup-validation",
            "## Cleanup & validation status\n\n"
            "Cleanup подтверждает отсутствие owned processes и raw private source. "
            "Notebook считается выполненным только после top-to-bottom run.",
        ),
        _code(
            "cleanup",
            "cleanup = acceptance.close()\ncleanup",
            runtime_mode="python",
        ),
    ]
    return nbformat.v4.new_notebook(
        cells=cells,
        metadata={
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            },
            "language_info": {"name": "python", "version": "3.12"},
            "onec_worker_universe_acceptance": {
                "schema": "onec-worker-universe-zup-notebook-v1",
                "live_status": "UNVERIFIED",
            },
        },
    )


def serialize_notebook(notebook: NotebookNode) -> str:
    return nbformat.writes(notebook, version=4) + "\n"


def verify_notebook(path: Path, *, allow_outputs: bool) -> None:
    try:
        notebook = nbformat.read(path, as_version=4)
        nbformat.validate(notebook)
    except Exception as error:
        raise ProtocolError("ZUP Worker universe notebook is invalid") from error

    tags = [cell.metadata.get("tags", [""])[0] for cell in notebook.cells]
    if tuple(tags) != _REQUIRED_CELL_TAGS:
        raise ProtocolError("ZUP Worker universe notebook cells are out of order")
    identifiers = [cell.get("id", "") for cell in notebook.cells]
    if not all(identifiers) or len(identifiers) != len(set(identifiers)):
        raise ProtocolError("ZUP Worker universe notebook cell ids are invalid")

    code_cells = [cell for cell in notebook.cells if cell.cell_type == "code"]
    if tuple(cell.metadata["tags"][0] for cell in code_cells) != REQUIRED_CODE_TAGS:
        raise ProtocolError("ZUP Worker universe notebook cells are out of order")
    parser = PythonParserTarget.from_generated()
    for cell in code_cells:
        tag = cell.metadata["tags"][0]
        if cell.metadata.get("runtime_mode") not in {"python", "main", "capture"}:
            raise ProtocolError(f"ZUP Worker universe notebook {tag} mode is invalid")
        if not allow_outputs and cell.get("outputs"):
            raise ProtocolError(f"ZUP Worker universe notebook {tag} contains outputs")
        if not allow_outputs and cell.get("execution_count") is not None:
            raise ProtocolError(
                f"ZUP Worker universe notebook {tag} contains execution count"
            )
        if bool(cell.metadata.get("expected_error")) != (tag in EXPECTED_ERROR_TAGS):
            raise ProtocolError(
                f"ZUP Worker universe notebook {tag} error metadata is invalid"
            )
        if cell.source.startswith("%%bsl\n"):
            split_notebook_cell(parser, cell.source.removeprefix("%%bsl\n"))

    serialized = serialize_notebook(notebook)
    forbidden = (
        "C:\\",
        "1Cv8.1CD",
        "ONEC_RUNTIME_USERNAME",
        "ONEC_RUNTIME_PASSWORD",
        "source_text",
        "target_object",
    )
    if any(value in serialized for value in forbidden):
        raise ProtocolError(
            "ZUP Worker universe notebook contains private or machine-specific data"
        )
    expected = build_notebook()
    if [cell.source for cell in notebook.cells] != [cell.source for cell in expected.cells]:
        raise ProtocolError("ZUP Worker universe notebook source differs from contract")


class WorkerUniverseZupAcceptance:
    """Fail-closed notebook facade over the compact acceptance runner."""

    def __init__(
        self,
        config: object | None,
        source_root: Path | None,
        *,
        preflight_inspector: Callable[[object, Path], object] | None = None,
        acceptance_runner: Callable[..., Path] | None = None,
        evidence_reader: Callable[[Path], dict[str, object]] | None = None,
    ) -> None:
        self._config = config
        self._source_root = source_root
        self._preflight_inspector = preflight_inspector
        self._acceptance_runner = acceptance_runner
        self._evidence_reader = evidence_reader
        self._preflight: object | None = None
        self._preflight_ready = False
        self._evidence: dict[str, object] | None = None

    @classmethod
    def from_environment(cls) -> WorkerUniverseZupAcceptance:
        from onec_runtime.config import RuntimeConfig

        required = {
            name: os.environ.get(name, "").strip()
            for name in (
                "ONEC_RUNTIME_WORKSPACE",
                "ONEC_RUNTIME_PLATFORM_BIN",
                "ONEC_RUNTIME_INFOBASE",
                "ONEC_ZUP_SOURCE_ROOT",
            )
        }
        if not all(required.values()):
            return cls(None, None)
        infobase = Path(required["ONEC_RUNTIME_INFOBASE"])
        try:
            config = RuntimeConfig(
                workspace=Path(required["ONEC_RUNTIME_WORKSPACE"]),
                platform_bin=Path(required["ONEC_RUNTIME_PLATFORM_BIN"]),
                connection_string=f'File="{infobase}";',
                username=os.environ.get("ONEC_RUNTIME_USERNAME", ""),
            )
        except (OSError, ValueError) as error:
            raise ProtocolError("ZUP notebook runtime configuration is invalid") from error
        return cls(config, Path(required["ONEC_ZUP_SOURCE_ROOT"]))

    def verify_reference_target(self) -> dict[str, object]:
        if self._config is None or self._source_root is None:
            return {
                "status": "incompatible_prerequisites",
                "sla_claimed": False,
                "compatibility_inventory": [
                    {"code": "notebook_live_environment_missing", "count": 1}
                ],
                "validation_command": (
                    "uv run python tools/build_zup_worker_universe_notebook.py --check"
                ),
            }
        if self._preflight_inspector is None:
            from integration.zup_worker_universe_acceptance import (
                inspect_zup_static_preflight,
            )

            inspector = inspect_zup_static_preflight
        else:
            inspector = self._preflight_inspector
        preflight = inspector(self._config, self._source_root)
        self._preflight = preflight
        if preflight.inventory:
            self._preflight_ready = False
            return {
                "status": "incompatible_prerequisites",
                "sla_claimed": False,
                "compatibility_inventory": list(preflight.inventory),
                "validation_command": (
                    "uv run python tools/build_zup_worker_universe_notebook.py --check"
                ),
            }
        self._preflight_ready = True
        return {
            "status": "READY",
            "sla_claimed": False,
            "compatibility_inventory": [],
            "validation_command": (
                "uv run python tools/build_zup_worker_universe_notebook.py --check"
            ),
        }

    def run(
        self,
        *,
        iterations: int,
        warmup_iterations: int,
    ) -> dict[str, object]:
        if self._config is None or self._source_root is None:
            raise ProtocolError("ZUP notebook live environment is unavailable")
        if (
            type(iterations) is not int
            or iterations != 60
            or type(warmup_iterations) is not int
            or warmup_iterations != 3
        ):
            raise ProtocolError("ZUP notebook requires exactly 3 warmup and 60 measured")
        if not self._preflight_ready:
            preflight = self.verify_reference_target()
            if preflight["status"] != "READY":
                raise ProtocolError("ZUP notebook exact preflight is unavailable")
        if self._acceptance_runner is None:
            from integration.zup_worker_universe_acceptance import (
                run_worker_universe_zup_acceptance,
            )

            runner = run_worker_universe_zup_acceptance
        else:
            runner = self._acceptance_runner
        if self._evidence_reader is None:
            from integration.zup_worker_universe_acceptance import (
                verify_worker_universe_zup_evidence,
            )

            reader = verify_worker_universe_zup_evidence
        else:
            reader = self._evidence_reader
        run_dir = runner(
            self._config,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            source_root=self._source_root,
        )
        self._evidence = dict(reader(run_dir))
        return dict(self._evidence)

    def run_performance(
        self,
        *,
        iterations: int,
        warmup_iterations: int,
    ) -> dict[str, object]:
        return self.run(
            iterations=iterations,
            warmup_iterations=warmup_iterations,
        )

    @staticmethod
    def verify_compact_evidence(evidence: object) -> dict[str, object]:
        from integration.zup_worker_universe_acceptance import verify_compact_evidence

        return verify_compact_evidence(evidence)

    def close(self) -> dict[str, object]:
        if self._evidence is None:
            return {"status": "NOT_STARTED"}
        cleanup = self._evidence.get("cleanup")
        if cleanup != {"owned_processes": 0, "private_source_files": 0}:
            raise ProtocolError("ZUP notebook cleanup evidence is invalid")
        return dict(cleanup)
