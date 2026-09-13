from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile
from hashlib import sha256

import pytest

from onec_runtime.bsl.parser_artifact_identity import (
    build_parser_artifact_manifest,
    parsergen_package_sha256,
    verify_parser_artifact_manifest,
)
from onec_runtime.bsl.parser_target import GeneratedParserMetadata, PythonParserTarget
from tools import generate_bsl_semantic_parser as generator
from tools.grammar_corpus_spike import build_combined_development_target


WORKSPACE = Path(__file__).parents[2]
PARSERGEN_SRC = os.environ.get(
    "ONEC_PARSERGEN_SRC",
    str(WORKSPACE / "tests" / "fixtures" / "parsergen" / "src"),
)
COMBINED_GRAMMAR = WORKSPACE / "grammar" / "bsl-server-strict.grammar"
FULL_ARTIFACT = (
    WORKSPACE / "src" / "onec_runtime" / "bsl" / "generated_semantic_parser.py"
)
COMMITTED_V2 = '"schema_version":2' in FULL_ARTIFACT.read_text(encoding="utf-8")


@pytest.mark.skipif(
    not PARSERGEN_SRC,
    reason="ONEC_PARSERGEN_SRC is required for parsergen validation tests",
)
def test_fresh_combined_parser_is_direct_and_runtime_only() -> None:
    """Break caught: the combined renderer must not retain a parser VM dependency."""
    rendered = generator.render_generated_module(COMBINED_GRAMMAR).decode("utf-8")

    assert 'PARSERGEN_BACKEND_ID = "python-semantic-direct-v1"' in rendered
    assert "from onec_runtime.bsl.source_maps import SourceSpan" in rendered
    assert "import parsergen" not in rendered and "from parsergen" not in rendered
    assert "PRODUCTIONS =" not in rendered
    assert "class _Frame" not in rendered


@pytest.mark.skipif(
    not COMMITTED_V2,
    reason="the schema-2 artifact is written after in-memory combined validation",
)
def test_committed_combined_parser_has_only_combined_provenance() -> None:
    """Break caught: the packaged parser must contain no split-generation residue."""
    text = FULL_ARTIFACT.read_text(encoding="utf-8")
    metadata = text.split("# </parsergen:artifact-metadata>", maxsplit=1)[0]

    for name in (
        "PARSER_ARTIFACT_MANIFEST_JSON",
        "PARSER_IDENTITY_SHA256",
        "GRAMMAR_SOURCE_SHA256",
        "PARSERGEN_PACKAGE_SHA256",
    ):
        assert metadata.count(f"{name} =") == 1
    for forbidden in (
        "SYNTAX_GRAMMAR_SHA256",
        "SEMANTIC_PROFILE_SHA256",
        "semantic_profile",
        "AppendNearestOwner",
        "owner_stack",
        "deferred_queue",
        "import parsergen",
        "from parsergen",
        "PRODUCTIONS =",
        "class _Frame",
    ):
        assert forbidden not in text


def test_parser_artifact_identity_binds_every_combined_codegen_input() -> None:
    """Break caught: changing a combined input must reject cache reuse."""
    manifest = build_parser_artifact_manifest(
        backend_id="python-semantic-direct-v1",
        artifact_role="bsl-server-full",
        grammar_bytes=b"combined\r\n",
        parsergen_package_sha256="1" * 64,
        entrypoints=(("module", "Модуль"),),
        lookahead=1,
        production_names=("Модуль",),
        optimizer_options=(("enabled", True),),
        codegen_options=(("runtime_source_span", True),),
    )

    assert manifest.schema_version == 2
    assert manifest.grammar_source_sha256 == sha256(b"combined\r\n").hexdigest()
    assert "syntax_sha256" not in manifest.to_json()
    assert "semantic_profile_sha256" not in manifest.to_json()
    assert (
        verify_parser_artifact_manifest(manifest.to_json()).identity_sha256
        == manifest.identity_sha256
    )

    baseline = {
        "backend_id": "python-semantic-direct-v1",
        "artifact_role": "bsl-server-full",
        "grammar_bytes": b"combined\r\n",
        "parsergen_package_sha256": "1" * 64,
        "entrypoints": (("module", "Модуль"),),
        "lookahead": 1,
        "production_names": ("Модуль",),
        "optimizer_options": (("enabled", True),),
        "codegen_options": (("runtime_source_span", True),),
    }
    variants = (
        {"backend_id": "python-semantic-direct-v2"},
        {"artifact_role": "alternative-artifact-role"},
        {"grammar_bytes": b"combined\n"},
        {"parsergen_package_sha256": "2" * 64},
        {"entrypoints": (("module", "Модуль"), ("notebook", "БлокНоутбука"))},
        {"lookahead": 2},
        {"production_names": ("Модуль", "ЭлементыМодуля")},
        {"optimizer_options": (("enabled", False),)},
        {"codegen_options": (("runtime_source_span", False),)},
    )
    for change in variants:
        candidate = build_parser_artifact_manifest(**(baseline | change))
        assert candidate.identity_sha256 != manifest.identity_sha256


def test_parser_artifact_manifest_rejects_schema_one_before_identity_checks() -> None:
    """Break caught: a split-input schema must never be accepted by schema 2 readers."""
    with pytest.raises(ValueError, match="^unsupported parser artifact manifest schema$"):
        verify_parser_artifact_manifest('{"schema_version":1}')


def test_generator_replaces_exactly_one_marked_section() -> None:
    """Break caught: a parsergen formatting change must not patch arbitrary text."""
    generated = (
        "before\n"
        "# <parsergen:source-span>\n"
        "old content\n"
        "# </parsergen:source-span>\n"
        "after\n"
    )

    assert generator._replace_generated_section(
        generated,
        "source-span",
        "from onec_runtime.bsl.source_maps import SourceSpan",
    ) == (
        "before\n"
        "# <parsergen:source-span>\n"
        "from onec_runtime.bsl.source_maps import SourceSpan\n"
        "# </parsergen:source-span>\n"
        "after\n"
    )

    with pytest.raises(RuntimeError, match="exactly one"):
        generator._replace_generated_section(
            "# <parsergen:source-span>\n# <parsergen:source-span>\n",
            "source-span",
            "replacement",
        )


def test_generated_metadata_rejects_a_truncated_manifest_identity() -> None:
    """Break caught: cache metadata cannot swap one combined parser for another."""
    manifest = build_parser_artifact_manifest(
        backend_id="python-semantic-direct-v1",
        artifact_role="bsl-server-full",
        grammar_bytes=b"combined",
        parsergen_package_sha256="3" * 64,
        entrypoints=(("module", "Модуль"),),
        lookahead=1,
        production_names=("Модуль",),
        optimizer_options=(("parser_ir_optimized", True),),
        codegen_options=(("runtime_source_span", True),),
    )

    assert GeneratedParserMetadata(
        manifest.identity_sha256,
        manifest.parsergen_package_sha256,
        manifest,
    ).manifest is manifest
    with pytest.raises(ValueError, match="identity is inconsistent"):
        GeneratedParserMetadata("0" * 64, manifest.parsergen_package_sha256, manifest)


@pytest.mark.skipif(
    not COMMITTED_V2,
    reason="the schema-2 artifact is written after in-memory combined validation",
)
def test_from_generated_verifies_the_embedded_schema_two_manifest() -> None:
    """Break caught: the installed target must load its verified combined manifest."""
    target = PythonParserTarget.from_generated()

    assert target.metadata.manifest is not None
    assert target.metadata.manifest.schema_version == 2
    assert target.metadata.manifest.grammar_source_sha256 == sha256(
        COMBINED_GRAMMAR.read_bytes()
    ).hexdigest()


def test_from_generated_requires_an_embedded_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: an unmarked artifact must not bypass schema-2 verification."""
    from onec_runtime.bsl import generated_semantic_parser

    monkeypatch.delattr(
        generated_semantic_parser,
        "PARSER_ARTIFACT_MANIFEST_JSON",
    )

    with pytest.raises(AttributeError, match="PARSER_ARTIFACT_MANIFEST_JSON"):
        PythonParserTarget.from_generated()


@pytest.mark.skipif(
    not PARSERGEN_SRC,
    reason="ONEC_PARSERGEN_SRC is required for parsergen validation tests",
)
def test_generator_embeds_a_verified_consumer_manifest(tmp_path: Path) -> None:
    """Break caught: a generated direct target must expose consumer provenance."""
    grammar = tmp_path / "minimal.grammar"
    grammar.write_text("#Name ::= ID\n<S> ::= @Named Name = #Name\n", encoding="utf-8")

    rendered = generator.build_combined_generated_target(
        grammar,
        {"start": "S"},
        artifact_label="minimal-full",
        runtime_source_span=True,
    ).rendered
    namespace: dict[str, object] = {}
    exec(rendered.decode("utf-8"), namespace)

    manifest = verify_parser_artifact_manifest(
        str(namespace["PARSER_ARTIFACT_MANIFEST_JSON"])
    )
    assert namespace["PARSERGEN_BACKEND_ID"] == "python-semantic-direct-v1"
    assert namespace["PARSER_IDENTITY_SHA256"] == manifest.identity_sha256
    assert namespace["GRAMMAR_SOURCE_SHA256"] == manifest.grammar_source_sha256
    assert manifest.grammar_source_sha256 == sha256(grammar.read_bytes()).hexdigest()
    metadata = rendered.split(b"# </parsergen:artifact-metadata>", maxsplit=1)[0]
    assert b"SYNTAX_GRAMMAR_SHA256" not in metadata
    assert b"SEMANTIC_PROFILE_SHA256" not in metadata


def test_generator_renders_minimal_combined_semantic_grammar(tmp_path: Path) -> None:
    grammar = tmp_path / "minimal.grammar"
    grammar.write_text("#Name ::= ID\n<S> ::= @Named Name = #Name\n", encoding="utf-8")

    rendered = generator.build_combined_generated_target(
        grammar,
        {"start": "S"},
        artifact_label="minimal-full",
        runtime_source_span=True,
    ).rendered

    text = rendered.decode("utf-8")
    assert "class Named:" in text
    assert "GRAMMAR_SOURCE_SHA256" in text
    assert "PARSER_IDENTITY_SHA256" in text
    assert "PARSERGEN_PACKAGE_SHA256" in text
    assert parsergen_package_sha256(Path(PARSERGEN_SRC))


@pytest.mark.skipif(
    not PARSERGEN_SRC,
    reason="ONEC_PARSERGEN_SRC is required for parsergen validation tests",
)
def test_combined_development_target_reports_validator_warnings(
    tmp_path: Path,
) -> None:
    grammar = tmp_path / "bsl-with-unreachable.grammar"
    grammar.write_text(
        COMBINED_GRAMMAR.read_text(encoding="utf-8")
        + "\n<НеиспользуемаяПродукция> ::= ID\n",
        encoding="utf-8",
    )
    target = build_combined_development_target(grammar, Path(PARSERGEN_SRC))

    assert target.validation_warnings == (
        {
            "code": "VAL102",
            "message": "production is unreachable from every entry point",
        },
    )


def test_fresh_generated_targets_have_independent_parser_instances() -> None:
    """A controller-local cursor must not leak into another controller."""
    rendered = generator.render_generated_module(COMBINED_GRAMMAR)
    namespace: dict[str, object] = {}
    exec(rendered.decode("utf-8"), namespace)
    manifest = verify_parser_artifact_manifest(
        str(namespace["PARSER_ARTIFACT_MANIFEST_JSON"])
    )
    metadata = GeneratedParserMetadata(
        manifest.identity_sha256,
        manifest.parsergen_package_sha256,
        manifest,
    )
    first = PythonParserTarget(
        namespace["GeneratedParser"],  # type: ignore[arg-type]
        namespace["GeneratedParseError"],  # type: ignore[arg-type]
        metadata,
    )
    second = first.new_instance()

    assert first.generated_parser is not second.generated_parser
    assert type(first.generated_parser) is type(second.generated_parser)
    assert first.metadata.parsergen_package_sha256 is not None
    assert first.parse_ast("Результат = 1;", "БлокНоутбука").First.Target.Root == "Результат"


@pytest.mark.skipif(
    not PARSERGEN_SRC,
    reason="ONEC_PARSERGEN_SRC is required for parsergen validation tests",
)
def test_from_files_builds_a_schema_two_combined_development_target() -> None:
    """Break caught: development compilation must retain one raw grammar identity."""
    target = PythonParserTarget.from_files(
        WORKSPACE / "grammar" / "bsl-server-strict.grammar",
        Path(PARSERGEN_SRC),
    )

    assert target.metadata.manifest is not None
    assert target.metadata.manifest.schema_version == 2
    assert target.metadata.manifest.grammar_source_sha256 == sha256(
        (WORKSPACE / "grammar" / "bsl-server-strict.grammar").read_bytes()
    ).hexdigest()
    assert target.parse_ast("Результат = 1;", "БлокНоутбука").First.Target.Root == "Результат"




@pytest.mark.skipif(
    not PARSERGEN_SRC,
    reason="ONEC_PARSERGEN_SRC is required for parsergen drift validation",
)
def test_generator_records_raw_grammar_line_endings_in_its_identity(tmp_path: Path) -> None:
    """Break caught: provenance must distinguish raw inputs before LF parsing."""
    crlf_grammar = tmp_path / "bsl-server-strict-crlf.grammar"
    crlf_grammar.write_bytes(
        COMBINED_GRAMMAR
        .read_bytes()
        .replace(b"\r\n", b"\n")
        .replace(b"\n", b"\r\n")
    )
    rendered = tmp_path / "generated_semantic_parser.py"
    environment = os.environ.copy()
    environment["ONEC_PARSERGEN_SRC"] = str(PARSERGEN_SRC)

    subprocess.run(
        [
            sys.executable,
            str(WORKSPACE / "tools" / "generate_bsl_semantic_parser.py"),
            "--grammar",
            str(crlf_grammar),
            "--output",
            str(rendered),
            "--write",
        ],
        check=True,
        cwd=WORKSPACE,
        env=environment,
    )

    namespace: dict[str, object] = {}
    exec(rendered.read_text(encoding="utf-8"), namespace)
    manifest = verify_parser_artifact_manifest(
        str(namespace["PARSER_ARTIFACT_MANIFEST_JSON"])
    )
    assert manifest.grammar_source_sha256 == sha256(crlf_grammar.read_bytes()).hexdigest()


@pytest.mark.skipif(
    not PARSERGEN_SRC,
    reason="ONEC_PARSERGEN_SRC is required for parsergen drift validation",
)
def test_generator_canonicalizes_parsergen_package_line_endings(tmp_path: Path) -> None:
    """Windows and Linux parsergen checkouts must carry identical provenance."""
    normalized_source = tmp_path / "parsergen-src"
    shutil.copytree(Path(PARSERGEN_SRC) / "parsergen", normalized_source / "parsergen")
    for source in (normalized_source / "parsergen").rglob("*.py"):
        source.write_bytes(
            source.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        )
    baseline = tmp_path / "baseline_semantic_parser.py"
    rendered = tmp_path / "generated_semantic_parser.py"
    environment = os.environ.copy()
    environment["ONEC_PARSERGEN_SRC"] = str(PARSERGEN_SRC)

    subprocess.run(
        [
            sys.executable,
            str(WORKSPACE / "tools" / "generate_bsl_semantic_parser.py"),
            "--grammar",
            str(COMBINED_GRAMMAR),
            "--output",
            str(baseline),
            "--write",
        ],
        check=True,
        cwd=WORKSPACE,
        env=environment,
    )
    environment["ONEC_PARSERGEN_SRC"] = str(normalized_source)

    subprocess.run(
        [
            sys.executable,
            str(WORKSPACE / "tools" / "generate_bsl_semantic_parser.py"),
            "--grammar",
            str(COMBINED_GRAMMAR),
            "--output",
            str(rendered),
            "--write",
        ],
        check=True,
        cwd=WORKSPACE,
        env=environment,
    )

    assert rendered.read_bytes() == baseline.read_bytes()


@pytest.mark.skipif(
    not PARSERGEN_SRC,
    reason="ONEC_PARSERGEN_SRC is required for parsergen drift validation",
)
def test_check_renders_and_checks_the_current_combined_parser_target(
    tmp_path: Path,
) -> None:
    frozen = tmp_path / "generated_semantic_parser.py"
    environment = os.environ.copy()
    environment["ONEC_PARSERGEN_SRC"] = str(PARSERGEN_SRC)

    subprocess.run(
        [
            sys.executable,
            str(WORKSPACE / "tools" / "generate_bsl_semantic_parser.py"),
            "--output",
            str(frozen),
            "--write",
        ],
        check=True,
        cwd=WORKSPACE,
        env=environment,
    )
    subprocess.run(
        [
            sys.executable,
            str(WORKSPACE / "tools" / "generate_bsl_semantic_parser.py"),
            "--output",
            str(frozen),
            "--check",
        ],
        check=True,
        cwd=WORKSPACE,
        env=environment,
    )

    assert b'PARSERGEN_BACKEND_ID = "python-semantic-direct-v1"' in frozen.read_bytes()
    assert b"from onec_runtime.bsl.source_maps import SourceSpan" in frozen.read_bytes()


@pytest.mark.packaging
@pytest.mark.skipif(
    shutil.which("uv") is None or not COMMITTED_V2,
    reason="uv and the regenerated schema-2 artifact are required for wheel smoke",
)
def test_wheel_installs_and_lowers_without_repository_parser_inputs(
    tmp_path: Path,
) -> None:
    """Omitting the generated module or reintroducing parsergen breaks this smoke."""
    dist = tmp_path / "dist"
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(dist)],
        check=True,
        cwd=WORKSPACE,
    )
    wheel = next(dist.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert "onec_runtime/bsl/generated_semantic_parser.py" in names
        assert "onec_runtime/bsl/generated_worker_semantic_parser.py" not in names

    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run(
        [str(python), "-m", "pip", "install", "--no-deps", str(wheel)],
        check=True,
    )
    subprocess.run(
        [
            str(python),
            "-I",
            "-c",
            "from onec_runtime.bsl import LoweringMode, SemanticNotebookLowerer; "
            "from onec_runtime.bsl.parser_target import PythonParserTarget; "
            "target = PythonParserTarget.from_generated(); "
            "result = SemanticNotebookLowerer(target).lower("
            "'Результат = 1;', mode=LoweringMode.MAIN); "
            "assert target.development is None; "
            "assert result.messages_intercepted == 0",
        ],
        check=True,
        cwd=tmp_path,
    )
