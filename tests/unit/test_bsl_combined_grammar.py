from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from onec_runtime.bsl.parser_target import BslParseError, PythonParserTarget


WORKSPACE = Path(__file__).parents[2]
COMBINED = WORKSPACE / "grammar" / "bsl-server-strict.grammar"
FULL_ARTIFACT = (
    WORKSPACE / "src" / "onec_runtime" / "bsl" / "generated_semantic_parser.py"
)
COMBINED_ARTIFACT_SHA256 = "b30f838ee5479983e2535c0579e3e7171163338b18c47307661a30ceaf09487e"
ENTRYPOINTS = {
    "module": "Модуль",
    "notebook": "БлокНоутбука",
    "notebook_cell": "ЯчейкаНоутбука",
    "expression": "ОтдельноеВыражение",
    "statement": "ОтдельнаяИнструкция",
    "preprocessor": "ДирективаПрепроцессора",
}

# The final fixture is grammar-valid and rejected later by full-AST extraction
# as a bare access-chain statement, so it has no GeneratedParseError to compare.
POSITIVE_FULL_AST_SOURCES = (
    (
        "basic-dependencies",
        "Перем Модульная Экспорт;\n"
        "Функция Проверить(Знач Параметр) Экспорт\n"
        "    Перем Локальная;\n"
        "    Локальная = КадровыйУчет.Рассчитать(Параметр);\n"
        "    Возврат ОбщегоНазначения.Значение(Локальная);\n"
        "КонецФункции",
    ),
    (
        "directives-loops-implicit-local",
        "#Если Сервер Тогда\r\n"
        "&НаСервере\r\n"
        "Процедура Обход()\r\n"
        "    Для Каждого Элемент Из КадровыйУчет.Получить() Цикл\r\n"
        "        НоваяЛокальная = Элемент;\r\n"
        "    КонецЦикла;\r\n"
        "КонецПроцедуры\r\n"
        "#КонецЕсли",
    ),
    (
        "internal-and-platform-shadowing",
        "Процедура Внутренний()\n"
        "КонецПроцедуры\n"
        "Процедура Вызов()\n"
        "    Внутренний();\n"
        "    Метаданные.Найти();\n"
        "    Результат = КадровыйУчет.Получить();\n"
        "КонецПроцедуры",
    ),
)
NEGATIVE_FULL_AST_SOURCES = (
    "Процедура P( КонецПроцедуры",
    "Процедура P()\nF(X;\nКонецПроцедуры",
    "Процедура P()\nX.;\nКонецПроцедуры",
    "Процедура P()\nX = ;\nКонецПроцедуры",
    "Процедура P()\nX..Y();\nКонецПроцедуры",
    "Процедура P()\nX[Y()];\nКонецПроцедуры",
)
SYNTACTIC_NEGATIVE_FULL_AST_SOURCES = NEGATIVE_FULL_AST_SOURCES[:-1]


@dataclass(frozen=True, slots=True)
class CombinedGeneration:
    source_production_count: int
    parser_ir_production_count: int
    module_text: str


COMBINED_GENERATION_SCRIPT = """
import json
from pathlib import Path
import sys

import parsergen
parsergen_src = Path(sys.argv[2]).resolve()
if not Path(parsergen.__file__).resolve().is_relative_to(parsergen_src):
    raise RuntimeError("parsergen import did not use the requested source")

from parsergen.analysis import compute_analysis
from parsergen.grammar_parser import parse_grammar
from parsergen.parser_ir import build_parser_ir
from parsergen.python_semantic_codegen import generate_python_semantic_parser
from parsergen.resolver import resolve_grammar

grammar_path = Path(sys.argv[1])

entrypoints = {
    "module": "Модуль",
    "notebook": "БлокНоутбука",
    "notebook_cell": "ЯчейкаНоутбука",
    "expression": "ОтдельноеВыражение",
    "statement": "ОтдельнаяИнструкция",
    "preprocessor": "ДирективаПрепроцессора",
}
parsed = parse_grammar(grammar_path.read_text(encoding="utf-8"), str(grammar_path))
if parsed.diagnostics or parsed.grammar is None or parsed.source_grammar is None or parsed.lowering is None:
    raise RuntimeError(f"combined grammar parse failed: {parsed.diagnostics!r}")
resolved = resolve_grammar(parsed.grammar)
if resolved.diagnostics or resolved.grammar is None:
    raise RuntimeError(f"combined grammar resolution failed: {resolved.diagnostics!r}")
analysis = compute_analysis(resolved.grammar, 1, tuple(entrypoints.values()))
parser_ir = build_parser_ir(
    parsed.source_grammar,
    parsed.lowering,
    resolved.grammar,
    analysis,
    entrypoint_productions=tuple(entrypoints.values()),
)
generated = generate_python_semantic_parser(parsed.source_grammar, parser_ir, entrypoints)
print(json.dumps({
    "source_production_count": len(parsed.source_grammar.productions),
    "parser_ir_production_count": len(parser_ir.productions),
    "module_text": generated.module_text,
}))
"""


def _build_combined_generation(
    grammar_path: Path,
    parsergen_src: Path,
) -> CombinedGeneration:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        (
            str(parsergen_src.resolve()),
            environment.get("PYTHONPATH", ""),
        )
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            COMBINED_GENERATION_SCRIPT,
            str(grammar_path.resolve()),
            str(parsergen_src.resolve()),
        ],
        check=True,
        capture_output=True,
        cwd=WORKSPACE,
        encoding="utf-8",
        env=environment,
    )
    generated = json.loads(completed.stdout)
    return CombinedGeneration(
        source_production_count=generated["source_production_count"],
        parser_ir_production_count=generated["parser_ir_production_count"],
        module_text=generated["module_text"],
    )


def generate_combined_module_text(grammar_path: Path, parsergen_src: Path) -> str:
    """Generate the direct module text from the combined grammar."""
    return _build_combined_generation(grammar_path, parsergen_src).module_text

def _current_parsergen_source() -> Path:
    configured = os.environ.get("ONEC_PARSERGEN_SRC")
    source = (
        Path(configured).resolve()
        if configured
        else Path(__file__).parents[2] / "tests" / "fixtures" / "parsergen" / "src"
    )
    if not (source / "parsergen" / "__init__.py").is_file():
        pytest.fail("parsergen source tree is missing or invalid")
    return source


def _ast_snapshot(value: object) -> object:
    """Compare AST node names, field order, values and all nested SourceSpans."""
    if is_dataclass(value) and not isinstance(value, type):
        return (
            type(value).__name__,
            tuple(
                (field.name, _ast_snapshot(getattr(value, field.name)))
                for field in fields(value)
            ),
        )
    if isinstance(value, tuple):
        return ("tuple", tuple(_ast_snapshot(item) for item in value))
    return ("scalar", type(value).__name__, value)


def _error_snapshot(target: PythonParserTarget, source: str) -> tuple[object, ...]:
    with pytest.raises(BslParseError) as caught:
        target.parse_ast(source, "Модуль")

    error = caught.value
    generated_error = error.__cause__
    assert generated_error is not None
    return (
        type(error).__name__,
        error.args,
        error.code,
        error.span,
        type(generated_error).__name__,
        generated_error.args,
        generated_error.actual,
        generated_error.expected,
    )


def test_committed_combined_artifact_has_the_recorded_sha256() -> None:
    assert sha256(FULL_ARTIFACT.read_bytes()).hexdigest() == COMBINED_ARTIFACT_SHA256


@pytest.fixture(scope="module")
def fresh_combined_target() -> PythonParserTarget:
    parsergen_src = _current_parsergen_source()
    namespace: dict[str, object] = {}
    generation = _build_combined_generation(COMBINED, parsergen_src)
    assert generation.source_production_count == 63
    assert generation.parser_ir_production_count == 63
    module_text = generation.module_text
    exec(compile(module_text, str(COMBINED), "exec"), namespace)
    committed = PythonParserTarget.from_generated()
    return PythonParserTarget(
        namespace["GeneratedParser"],  # type: ignore[arg-type]
        namespace["GeneratedParseError"],  # type: ignore[arg-type]
        committed.metadata,
    )


@pytest.fixture(scope="module")
def committed_target() -> PythonParserTarget:
    return PythonParserTarget.from_generated()


@pytest.mark.parametrize(
    ("case_name", "source"),
    tuple(case for case in POSITIVE_FULL_AST_SOURCES if not case[1].startswith("#")),
    ids=[
        case_name
        for case_name, source in POSITIVE_FULL_AST_SOURCES
        if not source.startswith("#")
    ],
)
def test_combined_parser_preserves_ast_field_and_span_parity(
    case_name: str,
    source: str,
    fresh_combined_target: PythonParserTarget,
    committed_target: PythonParserTarget,
) -> None:
    """Current generator and committed parser agree on AST fields and offsets."""
    assert case_name
    assert _ast_snapshot(fresh_combined_target.parse_ast(source, "Модуль")) == (
        _ast_snapshot(committed_target.parse_ast(source, "Модуль"))
    )


@pytest.mark.parametrize("source", SYNTACTIC_NEGATIVE_FULL_AST_SOURCES)
def test_combined_parser_preserves_parse_error_parity(
    source: str,
    fresh_combined_target: PythonParserTarget,
    committed_target: PythonParserTarget,
) -> None:
    """Current generator and committed parser agree on parse diagnostics."""
    assert _error_snapshot(fresh_combined_target, source) == _error_snapshot(
        committed_target,
        source,
    )
