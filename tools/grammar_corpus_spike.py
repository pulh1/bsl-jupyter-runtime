from __future__ import annotations

import argparse
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import sys
from time import monotonic
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
root_location = str(ROOT)
if root_location not in sys.path:
    sys.path.insert(0, root_location)

from onec_runtime.bsl import preprocess_server_source
from onec_runtime.bsl.parser_artifact_identity import (
    build_parser_artifact_manifest,
    parsergen_package_sha256,
)
from onec_runtime.bsl.parser_target import (
    DevelopmentParserDetails,
    GeneratedParserMetadata,
    PythonParserTarget,
)
from tools.generate_bsl_semantic_parser import ENTRYPOINTS
from tools.support.corpus import select_module_sample


def build_combined_development_target(
    grammar_path: Path,
    parsergen_src: Path,
) -> PythonParserTarget:
    """Compile the combined grammar with interpreted-parser details."""
    parsergen_location = str(parsergen_src.resolve())
    if parsergen_location not in sys.path:
        sys.path.insert(0, parsergen_location)

    from parsergen.analysis import compute_analysis
    from parsergen.grammar_parser import parse_grammar
    from parsergen.parser_ir import build_parser_ir
    from parsergen.python_direct_codegen import PYTHON_SEMANTIC_BACKEND_ID
    from parsergen.python_semantic_codegen import generate_python_semantic_parser
    from parsergen.resolver import resolve_grammar
    from parsergen.validation import validate_grammar

    grammar_bytes = grammar_path.read_bytes()
    parsed = parse_grammar(grammar_bytes.decode("utf-8"), str(grammar_path))
    parse_errors = [
        item
        for item in parsed.diagnostics
        if getattr(getattr(item, "severity", None), "value", None) == "error"
    ]
    if parse_errors or parsed.grammar is None or parsed.source_grammar is None or parsed.lowering is None:
        raise RuntimeError(f"combined parser input failed: {parsed.diagnostics}")

    resolution = resolve_grammar(parsed.grammar)
    resolution_errors = [
        item
        for item in resolution.diagnostics
        if getattr(getattr(item, "severity", None), "value", None) == "error"
    ]
    if resolution_errors or resolution.grammar is None:
        raise RuntimeError(f"combined parser resolution failed: {resolution.diagnostics}")

    analysis = compute_analysis(
        resolution.grammar,
        1,
        tuple(ENTRYPOINTS.values()),
    )
    validation = validate_grammar(
        parsed.grammar,
        resolution.grammar,
        analysis,
        ENTRYPOINTS,
        (*parsed.diagnostics, *resolution.diagnostics),
        lowering=parsed.lowering,
        source_grammar=parsed.source_grammar,
    )
    validation_errors = [
        item for item in validation.diagnostics if item.severity.value == "error"
    ]
    if validation_errors:
        raise RuntimeError(f"combined parser validation failed: {validation_errors}")

    parser_ir = build_parser_ir(
        parsed.source_grammar,
        parsed.lowering,
        resolution.grammar,
        analysis,
        entrypoint_productions=tuple(ENTRYPOINTS.values()),
    )
    generated = generate_python_semantic_parser(
        parsed.source_grammar,
        parser_ir,
        ENTRYPOINTS,
    )
    namespace: dict[str, Any] = {}
    exec(compile(generated.module_text, str(grammar_path), "exec"), namespace)
    package_sha256 = parsergen_package_sha256(parsergen_src)
    manifest = build_parser_artifact_manifest(
        backend_id=PYTHON_SEMANTIC_BACKEND_ID,
        artifact_role="bsl-development-combined",
        grammar_bytes=grammar_bytes,
        parsergen_package_sha256=package_sha256,
        entrypoints=tuple(ENTRYPOINTS.items()),
        lookahead=1,
        production_names=tuple(
            production.name for production in parsed.source_grammar.productions
        ),
        optimizer_options=(("parser_ir_optimized", True),),
        codegen_options=(("runtime_source_span", False),),
    )
    warnings = tuple(
        {"code": item.code, "message": item.message}
        for item in validation.diagnostics
        if item.severity.value != "error"
    )
    return PythonParserTarget(
        namespace["GeneratedParser"],
        namespace["GeneratedParseError"],
        GeneratedParserMetadata(
            manifest.identity_sha256,
            manifest.parsergen_package_sha256,
            manifest,
        ),
        generated_source=generated.module_text,
        development=DevelopmentParserDetails(
            resolution.grammar,
            analysis,
            parser_ir,
            warnings,
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--grammar", type=Path, required=True)
    parser.add_argument("--parsergen-src", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--fraction", type=float, default=0.10)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    started = monotonic()
    selection = select_module_sample(args.source_root, fraction=args.fraction)
    target = build_combined_development_target(args.grammar, args.parsergen_src)
    results: list[dict[str, object]] = []
    for relative in selection.selected:
        path = args.source_root / Path(relative)
        source_bytes = path.read_bytes()
        source = source_bytes.decode("utf-8-sig", errors="replace")
        try:
            effective = preprocess_server_source(source)
            target.parse(effective, target.ENTRYPOINTS["module"])
            status = "PASS"
            error = ""
            effective_sha256 = sha256(effective.encode("utf-8")).hexdigest()
        except Exception as caught:
            status = "FAIL"
            error = f"{type(caught).__name__}: {caught}"
            effective_sha256 = None
        results.append(
            {
                "module": relative,
                "status": status,
                "bytes": len(source_bytes),
                "sha256": sha256(source_bytes).hexdigest(),
                "server_effective_sha256": effective_sha256,
                "error": error,
            }
        )

    passed = sum(result["status"] == "PASS" for result in results)
    rendered = json.dumps(
        {
            "status": "PASS",
            "grammar": str(args.grammar.resolve()),
            "grammar_source_sha256": sha256(args.grammar.read_bytes()).hexdigest(),
            "parser_identity_sha256": target.grammar_sha256,
            "lookahead": target.lookahead,
            "entrypoint": target.ENTRYPOINTS["module"],
            "validation_warnings": target.validation_warnings,
            "selection": asdict(selection),
            "aggregate": {
                "selected": len(results),
                "parsed": passed,
                "failed": len(results) - passed,
                "parse_rate": passed / len(results) if results else 1.0,
                "duration_s": monotonic() - started,
            },
            "modules": results,
        },
        ensure_ascii=False,
        indent=2,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered + "\n", encoding="utf-8")
    sys.stdout.buffer.write((rendered + "\n").encode("utf-8"))


if __name__ == "__main__":
    main()
