from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path
import platform
import sys
from time import perf_counter
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
root_location = str(ROOT)
if root_location not in sys.path:
    sys.path.insert(0, root_location)

from onec_runtime.bsl import preprocess_server_source
from tools.support.benchmark import TimingSummary, summarize_durations
from onec_runtime.bsl.lexer import Token, tokenize
from onec_runtime.bsl.parser_target import PythonParserTarget
from tools.grammar_corpus_spike import build_combined_development_target
from tools.support.corpus import select_module_sample


@dataclass(frozen=True, slots=True)
class ModuleInput:
    relative: str
    source: str
    tokens: tuple[Token, ...]
    byte_count: int


def _measure(
    modules: list[ModuleInput],
    operation: Callable[[ModuleInput], object],
) -> TimingSummary:
    durations: list[float] = []
    started = perf_counter()
    for module in modules:
        item_started = perf_counter()
        operation(module)
        durations.append(perf_counter() - item_started)
    elapsed = perf_counter() - started
    return summarize_durations(
        durations,
        elapsed_s=elapsed,
        byte_count=sum(module.byte_count for module in modules),
    )


def _microbenchmark(
    operation: Callable[[], object],
    *,
    iterations: int,
    repeats: int,
) -> dict[str, object]:
    for _ in range(100):
        operation()
    batches: list[float] = []
    for _ in range(repeats):
        started = perf_counter()
        for _ in range(iterations):
            operation()
        batches.append(perf_counter() - started)
    ordered = sorted(batches)
    median_batch = ordered[len(ordered) // 2]
    return {
        "iterations_per_batch": iterations,
        "repeats": repeats,
        "median_batch_s": median_batch,
        "median_us_per_operation": median_batch / iterations * 1_000_000,
        "operations_s": iterations / median_batch,
    }


def _speedup(slower: dict[str, object], faster: dict[str, object]) -> float:
    return float(slower["elapsed_s"]) / float(faster["elapsed_s"])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--grammar", type=Path, required=True)
    parser.add_argument("--parsergen-src", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--fraction", type=float, default=0.10)
    parser.add_argument("--micro-iterations", type=int, default=2_000)
    parser.add_argument("--micro-repeats", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    selection = select_module_sample(args.source_root, fraction=args.fraction)
    target = build_combined_development_target(args.grammar, args.parsergen_src)
    entrypoint = target.ENTRYPOINTS["module"]
    modules: list[ModuleInput] = []
    for relative in selection.selected:
        source = preprocess_server_source(
            (args.source_root / Path(relative)).read_text(
                encoding="utf-8-sig",
                errors="replace",
            )
        )
        modules.append(
            ModuleInput(
                relative=relative,
                source=source,
                tokens=tuple(tokenize(source)),
                byte_count=len(source.encode("utf-8")),
            )
        )

    generated_failures: list[dict[str, str]] = []
    interpreted_failures: list[dict[str, str]] = []
    common: list[ModuleInput] = []
    for module in modules:
        generated_error = None
        interpreted_error = None
        try:
            target.parse_tokens(module.tokens, entrypoint)
        except Exception as caught:
            generated_error = f"{type(caught).__name__}: {caught}"
            generated_failures.append(
                {"module": module.relative, "error": generated_error}
            )
        try:
            target.recognize_tokens_interpreted(module.tokens, entrypoint)
        except Exception as caught:
            interpreted_error = f"{type(caught).__name__}: {caught}"
            interpreted_failures.append(
                {"module": module.relative, "error": interpreted_error}
            )
        if generated_error is None and interpreted_error is None:
            common.append(module)

    for module in common[: min(25, len(common))]:
        target.parse_tokens(module.tokens, entrypoint)
        target.recognize_tokens_interpreted(module.tokens, entrypoint)

    generated_parser_only = _measure(
        common,
        lambda module: target.parse_tokens(module.tokens, entrypoint),
    )
    interpreted_recognizer_only = _measure(
        common,
        lambda module: target.recognize_tokens_interpreted(
            module.tokens,
            entrypoint,
        ),
    )
    interpreted_tree_only = _measure(
        common,
        lambda module: target.parse_tokens_interpreted(module.tokens, entrypoint),
    )
    generated_end_to_end = _measure(
        common,
        lambda module: target.parse(module.source, entrypoint),
    )
    interpreted_end_to_end = _measure(
        common,
        lambda module: target.recognize_tokens_interpreted(
            tokenize(module.source),
            entrypoint,
        ),
    )

    small_source = "ГДФЛ = Расчет.Ндфл.Посчитать();"
    small_tokens = tuple(tokenize(small_source))
    generated_small = _microbenchmark(
        lambda: target.parse_tokens(small_tokens, entrypoint),
        iterations=args.micro_iterations,
        repeats=args.micro_repeats,
    )
    interpreted_small = _microbenchmark(
        lambda: target.recognize_tokens_interpreted(small_tokens, entrypoint),
        iterations=args.micro_iterations,
        repeats=args.micro_repeats,
    )
    interpreted_tree_small = _microbenchmark(
        lambda: target.parse_tokens_interpreted(small_tokens, entrypoint),
        iterations=args.micro_iterations,
        repeats=args.micro_repeats,
    )

    result = {
        "status": "PASS",
        "methodology": {
            "generated": "generated Parser IR/DAG syntax recognizer",
            "interpreted_recognizer": (
                "resolved-grammar recursive interpreter without tree allocation"
            ),
            "interpreted_tree": (
                "original resolved-grammar recursive interpreter with ParseNode tree"
            ),
            "fair_comparison": "generated vs interpreted_recognizer",
            "warmup_modules": min(25, len(common)),
        },
        "environment": {
            "python": sys.version,
            "platform": platform.platform(),
        },
        "parser_inputs": {
            "grammar_path": str(args.grammar.resolve()),
            "grammar_source_sha256": sha256(args.grammar.read_bytes()).hexdigest(),
            "parser_identity_sha256": target.grammar_sha256,
            "lookahead": target.lookahead,
            "validation_warnings": target.validation_warnings,
            "generated_module_sha256": sha256(
                target.generated_source.encode("utf-8")
            ).hexdigest(),
        },
        "selection": asdict(selection),
        "reliability": {
            "selected": len(modules),
            "generated_passed": len(modules) - len(generated_failures),
            "interpreted_passed": len(modules) - len(interpreted_failures),
            "common_modules": len(common),
            "generated_failures": generated_failures,
            "interpreted_failures": interpreted_failures,
        },
        "measurements": {
            "corpus_parser_only": {
                "generated": generated_parser_only,
                "interpreted_recognizer": interpreted_recognizer_only,
                "interpreted_tree": interpreted_tree_only,
                "generated_speedup_vs_recognizer": _speedup(
                    interpreted_recognizer_only,
                    generated_parser_only,
                ),
                "generated_speedup_vs_tree": _speedup(
                    interpreted_tree_only,
                    generated_parser_only,
                ),
            },
            "corpus_lexer_and_parser": {
                "generated": generated_end_to_end,
                "interpreted_recognizer": interpreted_end_to_end,
                "generated_speedup_vs_recognizer": _speedup(
                    interpreted_end_to_end,
                    generated_end_to_end,
                ),
            },
            "small_cell_parser_only": {
                "source": small_source,
                "generated": generated_small,
                "interpreted_recognizer": interpreted_small,
                "interpreted_tree": interpreted_tree_small,
                "generated_speedup_vs_recognizer": (
                    float(interpreted_small["median_us_per_operation"])
                    / float(generated_small["median_us_per_operation"])
                ),
                "generated_speedup_vs_tree": (
                    float(interpreted_tree_small["median_us_per_operation"])
                    / float(generated_small["median_us_per_operation"])
                ),
            },
        },
    }
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    sys.stdout.buffer.write(rendered.encode("utf-8"))


if __name__ == "__main__":
    main()
