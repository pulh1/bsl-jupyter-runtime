from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

from onec_runtime.bsl.parser_artifact_identity import (
    build_parser_artifact_manifest,
    parsergen_package_sha256,
)
from onec_runtime.bsl.parser_target import (
    DevelopmentParserDetails,
    GeneratedParserMetadata,
    PythonParserTarget,
)


def build_combined_python_parser_target(
    target_type: type[PythonParserTarget],
    grammar_path: Path,
    parsergen_src: Path,
    *,
    lookahead: int = 1,
) -> PythonParserTarget:
    """Compile the combined grammar for development validation."""
    parsergen_location = str(parsergen_src.resolve())
    if parsergen_location not in sys.path:
        sys.path.insert(0, parsergen_location)

    from parsergen import generate_python_semantic_parser
    from parsergen.analysis import compute_analysis
    from parsergen.grammar_parser import parse_grammar
    from parsergen.parser_ir import build_parser_ir
    from parsergen.python_direct_codegen import PYTHON_SEMANTIC_BACKEND_ID
    from parsergen.resolver import resolve_grammar
    from parsergen.validation import validate_grammar

    grammar_bytes = grammar_path.read_bytes()
    parsed = parse_grammar(grammar_bytes.decode("utf-8"), str(grammar_path))
    if parsed.grammar is None:
        raise RuntimeError(f"Grammar parse failed: {parsed.diagnostics}")
    if parsed.source_grammar is None or parsed.lowering is None:
        raise RuntimeError("Grammar parse did not produce source lowering")
    resolution = resolve_grammar(parsed.grammar)
    if resolution.grammar is None:
        raise RuntimeError(f"Grammar resolution failed: {resolution.diagnostics}")
    analysis = compute_analysis(
        resolution.grammar,
        lookahead,
        tuple(target_type.ENTRYPOINTS.values()),
    )
    validation = validate_grammar(
        parsed.grammar,
        resolution.grammar,
        analysis,
        target_type.ENTRYPOINTS,
        (*parsed.diagnostics, *resolution.diagnostics),
        lowering=parsed.lowering,
        source_grammar=parsed.source_grammar,
    )
    errors = [
        item for item in validation.diagnostics if item.severity.value == "error"
    ]
    if errors:
        raise RuntimeError(f"Grammar validation failed: {errors}")
    warnings = tuple(
        {"code": item.code, "message": item.message}
        for item in validation.diagnostics
        if item.severity.value != "error"
    )
    parser_ir = build_parser_ir(
        parsed.source_grammar,
        parsed.lowering,
        resolution.grammar,
        analysis,
        entrypoint_productions=tuple(target_type.ENTRYPOINTS.values()),
    )
    generated = generate_python_semantic_parser(
        parsed.source_grammar,
        parser_ir,
        target_type.ENTRYPOINTS,
    )
    namespace: dict[str, Any] = {}
    exec(
        compile(generated.module_text, str(grammar_path), "exec"),
        namespace,
    )
    manifest = build_parser_artifact_manifest(
        backend_id=PYTHON_SEMANTIC_BACKEND_ID,
        artifact_role="bsl-development-combined",
        grammar_bytes=grammar_bytes,
        parsergen_package_sha256=parsergen_package_sha256(parsergen_src),
        entrypoints=tuple(target_type.ENTRYPOINTS.items()),
        lookahead=lookahead,
        production_names=tuple(
            production.name for production in parsed.source_grammar.productions
        ),
        optimizer_options=(("parser_ir_optimized", True),),
        codegen_options=(("runtime_source_span", False),),
    )
    return target_type(
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
