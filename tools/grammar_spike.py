from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
root_location = str(ROOT)
if root_location not in sys.path:
    sys.path.insert(0, root_location)

from tools.grammar_corpus_spike import build_combined_development_target

VISIBLE_CELL = (
    "НДФЛ = Расчет.Ндфл.Посчитать(); "
    "Расчет.Ндфл.ИзменитьРезультат(НДФЛ);"
)


def _node_count(node: object) -> int:
    children = getattr(node, "children", ())
    return 1 + sum(_node_count(child) for child in children if hasattr(child, "children"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--grammar", type=Path, required=True)
    parser.add_argument("--parsergen-src", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    from onec_runtime.bsl import LoweringMode, SemanticNotebookLowerer

    target = build_combined_development_target(args.grammar, args.parsergen_src)
    samples = {
        "notebook": VISIBLE_CELL,
        "expression": "Расчет.Ндфл.Посчитать()",
        "statement": "Расчет.Ндфл.ИзменитьРезультат(НДФЛ)",
        "module": (
            "Процедура Пересчитать() Экспорт\n"
            "НДФЛ = Расчет.Ндфл.Посчитать();\n"
            "Расчет.Ндфл.ИзменитьРезультат(НДФЛ);\n"
            "КонецПроцедуры"
        ),
    }
    parsed_samples = {
        name: _node_count(target.parse(source, target.ENTRYPOINTS[name]))
        for name, source in samples.items()
    }
    lowered = SemanticNotebookLowerer(
        target,
        context_names=("Расчет",),
    ).lower(VISIBLE_CELL, mode=LoweringMode.MAIN).source
    expected = (
        'e1cRuntimeКонтекст.Вставить("НДФЛ", e1cRuntimeКонтекст.Расчет.Ндфл.Посчитать()); '
        "e1cRuntimeКонтекст.Расчет.Ндфл.ИзменитьРезультат(e1cRuntimeКонтекст.НДФЛ);"
    )
    if lowered != expected:
        raise RuntimeError(f"Unexpected lowering: {lowered}")

    result = {
        "status": "PASS",
        "grammar": str(args.grammar.resolve()),
        "grammar_source_sha256": sha256(args.grammar.read_bytes()).hexdigest(),
        "parser_identity_sha256": target.grammar_sha256,
        "lookahead": target.lookahead,
        "entrypoints": target.ENTRYPOINTS,
        "validation_warnings": target.validation_warnings,
        "parsed_samples": parsed_samples,
        "visible": VISIBLE_CELL,
        "lowered": lowered,
    }
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    sys.stdout.buffer.write((rendered + "\n").encode("utf-8"))


if __name__ == "__main__":
    main()
