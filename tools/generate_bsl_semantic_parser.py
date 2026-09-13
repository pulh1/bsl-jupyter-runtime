"""Regenerate the runtime semantic BSL parser from its combined grammar."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import sys
from typing import Mapping


ROOT = Path(__file__).resolve().parents[1]
runtime_source = str(ROOT / "src")
if runtime_source not in sys.path:
    sys.path.insert(0, runtime_source)

from onec_runtime.bsl.parser_artifact_identity import (
    build_parser_artifact_manifest,
    parsergen_package_sha256,
)


GRAMMAR = ROOT / "grammar" / "bsl-server-strict.grammar"
OUTPUT = ROOT / "src" / "onec_runtime" / "bsl" / "generated_semantic_parser.py"
ENTRYPOINTS = {
    "module": "Модуль",
    "notebook": "БлокНоутбука",
    "notebook_cell": "ЯчейкаНоутбука",
    "expression": "ОтдельноеВыражение",
    "statement": "ОтдельнаяИнструкция",
    "preprocessor": "ДирективаПрепроцессора",
}


@dataclass(frozen=True, slots=True)
class InMemoryGeneratedTarget:
    """Development-only generated source and the Parser IR that produced it."""

    generated: object
    parser_ir: object
    production_names: tuple[str, ...]
    rendered: bytes


def _parsergen_source() -> Path:
    configured = os.environ.get("ONEC_PARSERGEN_SRC")
    source = (
        Path(configured).resolve()
        if configured
        else ROOT / "tests" / "fixtures" / "parsergen" / "src"
    )
    if not (source / "parsergen" / "grammar_parser.py").is_file():
        raise RuntimeError(f"parsergen source tree is missing or invalid: {source}")
    return source


def _replace_generated_section(text: str, name: str, replacement: str) -> str:
    """Replace precisely one parsergen-owned marker section."""
    open_marker = f"# <parsergen:{name}>"
    close_marker = f"# </parsergen:{name}>"
    if text.count(open_marker) != 1 or text.count(close_marker) != 1:
        raise RuntimeError(
            f"generated {name} section must contain exactly one open and close marker"
        )
    open_index = text.index(open_marker)
    close_index = text.index(close_marker)
    if open_index >= close_index:
        raise RuntimeError(f"generated {name} section markers are out of order")
    replacement = replacement.rstrip("\n")
    content = "" if not replacement else f"\n{replacement}"
    return (
        text[: open_index + len(open_marker)]
        + content
        + "\n"
        + text[close_index:]
    )


def _raise_if_errors(diagnostics: object, stage: str) -> None:
    errors = tuple(
        item
        for item in diagnostics  # type: ignore[union-attr]
        if getattr(getattr(item, "severity", None), "value", None) == "error"
    )
    if errors:
        raise RuntimeError(f"combined grammar {stage} failed: {errors}")


def build_combined_generated_target(
    grammar_path: Path,
    entrypoints: Mapping[str, str],
    *,
    artifact_label: str,
    runtime_source_span: bool,
) -> InMemoryGeneratedTarget:
    """Compile one combined source grammar into a direct runtime parser."""
    parsergen_source = _parsergen_source()
    parsergen_location = str(parsergen_source)
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
    _raise_if_errors(parsed.diagnostics, "parse")
    if parsed.grammar is None or parsed.source_grammar is None or parsed.lowering is None:
        raise RuntimeError(f"combined grammar parse failed: {parsed.diagnostics}")

    resolution = resolve_grammar(parsed.grammar)
    _raise_if_errors(resolution.diagnostics, "resolution")
    if resolution.grammar is None:
        raise RuntimeError(f"combined grammar resolution failed: {resolution.diagnostics}")

    analysis = compute_analysis(
        resolution.grammar,
        1,
        tuple(entrypoints.values()),
    )
    validation = validate_grammar(
        parsed.grammar,
        resolution.grammar,
        analysis,
        entrypoints,
        (*parsed.diagnostics, *resolution.diagnostics),
        lowering=parsed.lowering,
        source_grammar=parsed.source_grammar,
    )
    _raise_if_errors(validation.diagnostics, "validation")

    parser_ir = build_parser_ir(
        parsed.source_grammar,
        parsed.lowering,
        resolution.grammar,
        analysis,
        entrypoint_productions=tuple(entrypoints.values()),
    )
    generated = generate_python_semantic_parser(
        parsed.source_grammar,
        parser_ir,
        entrypoints,
    )
    production_names = tuple(
        production.name for production in parsed.source_grammar.productions
    )
    package_sha256 = parsergen_package_sha256(parsergen_source)
    manifest = build_parser_artifact_manifest(
        backend_id=PYTHON_SEMANTIC_BACKEND_ID,
        artifact_role=artifact_label,
        grammar_bytes=grammar_bytes,
        parsergen_package_sha256=package_sha256,
        entrypoints=tuple(entrypoints.items()),
        lookahead=1,
        production_names=production_names,
        optimizer_options=(("parser_ir_optimized", True),),
        codegen_options=(("runtime_source_span", runtime_source_span),),
    )
    generated_module_text = generated.module_text
    if runtime_source_span:
        generated_module_text = _replace_generated_section(
            generated_module_text,
            "source-span",
            "from onec_runtime.bsl.source_maps import SourceSpan",
        )
    generated_source = _replace_generated_section(
        generated_module_text,
        "artifact-metadata",
        "\n".join(
            (
                f"PARSER_ARTIFACT_MANIFEST_JSON = {manifest.to_json()!r}",
                f"PARSER_IDENTITY_SHA256 = {manifest.identity_sha256!r}",
                f"GRAMMAR_SOURCE_SHA256 = {manifest.grammar_source_sha256!r}",
                f"PARSERGEN_PACKAGE_SHA256 = {manifest.parsergen_package_sha256!r}",
            )
        ),
    )
    provenance = (
        "# DO NOT EDIT: generated by tools/generate_bsl_semantic_parser.py.\n"
        f"# Artifact label: {artifact_label}\n"
        f"# Grammar source SHA256: {manifest.grammar_source_sha256}\n"
        f"# Parser identity SHA256: {manifest.identity_sha256}\n"
        f"# Parsergen package SHA256: {manifest.parsergen_package_sha256}\n\n"
    )
    return InMemoryGeneratedTarget(
        generated=generated,
        parser_ir=parser_ir,
        production_names=production_names,
        rendered=(provenance + generated_source).encode("utf-8"),
    )


def render_generated_module(grammar_path: Path = GRAMMAR) -> bytes:
    """Return deterministic bytes for the six-entrypoint combined parser."""
    return build_combined_generated_target(
        grammar_path,
        ENTRYPOINTS,
        artifact_label="bsl-server-full",
        runtime_source_span=True,
    ).rendered


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grammar", type=Path, default=GRAMMAR)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--write", action="store_true")
    operation.add_argument("--check", action="store_true")
    args = parser.parse_args()

    rendered = render_generated_module(args.grammar.resolve())
    output = args.output.resolve()
    if args.check:
        if not output.is_file() or output.read_bytes() != rendered:
            raise SystemExit(f"generated parser drift: {output}")
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(rendered)


if __name__ == "__main__":
    main()
