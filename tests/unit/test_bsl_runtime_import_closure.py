from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

WORKSPACE = Path(__file__).parents[2]
sys.path.insert(0, str(WORKSPACE / "src"))

from onec_runtime.bsl.parser_target import PythonParserTarget
from onec_runtime_build import parser_target_development


PRODUCT_ROOTS = (
    WORKSPACE / "src" / "onec_runtime",
    WORKSPACE / "packages" / "mcp" / "src" / "onec_runtime_mcp",
    WORKSPACE / "packages" / "jupyter" / "src" / "onec_runtime_jupyter",
)
FORBIDDEN_RUNTIME_IMPORTS = frozenset(
    {"parsergen", "win32com", "pythoncom", "comtypes"}
)


def test_runtime_has_no_separate_worker_parser_artifacts() -> None:
    """Runtime parsing has one full-AST artifact, not a Worker sidecar."""
    removed = (
        WORKSPACE / "grammar/bsl-worker-reload.semantic",
        WORKSPACE / "src/onec_runtime/bsl/generated_worker_semantic_parser.py",
        WORKSPACE / "src/onec_runtime/bsl/worker_projection.py",
    )

    assert all(not path.exists() for path in removed)


def test_product_runtime_sources_have_no_parsergen_or_com_imports() -> None:
    """Build-time parsergen and COM adapters must stay outside product imports."""
    violations: list[tuple[str, str]] = []
    for root in PRODUCT_ROOTS:
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                for imported in _imported_modules(node):
                    if (
                        imported.split(".", 1)[0].casefold()
                        in FORBIDDEN_RUNTIME_IMPORTS
                    ):
                        violations.append(
                            (path.relative_to(WORKSPACE).as_posix(), imported)
                        )

    assert violations == []


def test_clean_worker_projection_runtime_does_not_load_parsergen_or_com() -> None:
    """A clean production Worker parse/resolve/lower path loads no forbidden SDK."""
    script = r'''
import json
from pathlib import Path
import sys

workspace = Path.cwd()
sys.path[:0] = [
    str(workspace / "src"),
    str(workspace / "packages" / "mcp" / "src"),
    str(workspace / "packages" / "jupyter" / "src"),
]

import onec_runtime.runtime_api
import onec_runtime.session
from onec_runtime.bsl.module_catalog import (
    CommonModuleCatalogSnapshot,
    CommonModuleDescriptor,
    CommonModuleScope,
)
from onec_runtime.bsl.module_universe import WorkerModuleUnit
from onec_runtime.bsl.source_maps import (
    SourceUnitKind,
    SourceUnitRef,
    mapped_visible_source,
    source_sha256,
)
from onec_runtime.runtime_api import PrototypeRuntimeApi
from onec_runtime.worker_universe import WorkerGenerationHandle, WorkerModuleArtifact, WorkerModuleBinaryKey

source = "Процедура P()\n    Локальная = 1;\nКонецПроцедуры"
catalog = CommonModuleCatalogSnapshot.create(
    profile="runtime-import-fence",
    preprocessor_profile="server",
    revision=0,
    modules=(
        CommonModuleDescriptor("ImportFence", CommonModuleScope.SERVER),
    ),
)
unit_ref = SourceUnitRef(
    SourceUnitKind.TEST_MODULE,
    "ImportFence",
    1,
    source_sha256(source),
)
unit = WorkerModuleUnit(
    "ImportFence",
    "test-module",
    1,
    mapped_visible_source(source, unit_ref),
)

class Controller:
    runtime_generation = 1

class Builder:
    def build(self, lowered, **_kwargs):
        key = WorkerModuleBinaryKey.create(
            lowered,
            packer_version="import-fence",
            target_profile="runtime-import-fence",
        )
        return WorkerModuleArtifact(
            logical_name=lowered.analysis.unit.logical_name,
            revision=lowered.analysis.unit.revision,
            source_sha256=lowered.mapped_source.artifact.source_sha256,
            binary_key=key,
            artifact_sha256="0" * 64,
            dependency_bindings=lowered.analysis.dependencies,
            exports=(),
            source_map_sha256=lowered.mapped_source.source_map_sha256,
            worker_artifact=None,
        )

api = PrototypeRuntimeApi(Controller(), worker_module_builder=Builder())
api._publish_worker_artifacts_locked = lambda artifacts, **_kwargs: WorkerGenerationHandle(1, 1, 1, "0" * 64)
api._prune_worker_caches_locked = lambda: None
api.load_worker_modules((unit,), common_modules=catalog)

forbidden = {"parsergen", "win32com", "pythoncom", "comtypes"}
loaded = sorted(
    name
    for name in sys.modules
    if name.split(".", 1)[0].casefold() in forbidden
)
build_adapters = sorted(
    name for name in sys.modules if name.split(".", 1)[0] == "onec_runtime_build"
)
lsp_source_publication = sorted(
    name for name in sys.modules if name == "onec_runtime.source_state"
)
print(json.dumps({
    "forbidden": loaded,
    "build_adapters": build_adapters,
    "lsp_source_publication": lsp_source_publication,
}))
'''
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=WORKSPACE,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "forbidden": [],
        "build_adapters": [],
        "lsp_source_publication": [],
    }


def test_combined_from_files_routes_through_development_adapter(
    monkeypatch,
    tmp_path: Path,
) -> None:
    expected = object()
    grammar_path = tmp_path / "legacy.grammar"
    parser_source = tmp_path / "parser-source"
    calls: list[tuple[object, Path, Path, int]] = []

    def build(target_type, grammar, source, *, lookahead):
        calls.append((target_type, grammar, source, lookahead))
        return expected

    monkeypatch.setattr(
        parser_target_development,
        "build_combined_python_parser_target",
        build,
    )

    actual = PythonParserTarget.from_files(
        grammar_path,
        parser_source,
        lookahead=3,
    )

    assert actual is expected
    assert calls == [(PythonParserTarget, grammar_path, parser_source, 3)]


@pytest.mark.packaging
@pytest.mark.skipif(
    shutil.which("uv") is None,
    reason="uv is required to build the isolated wheel smoke test",
)
def test_wheel_keeps_combined_from_files_adapter(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(dist)],
        cwd=WORKSPACE,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(dist.glob("*.whl"))
    script = r'''
from pathlib import Path
import sys

sys.path.insert(0, sys.argv[1])

from onec_runtime.bsl.parser_target import PythonParserTarget
from onec_runtime_build import parser_target_development

expected = object()
calls = []

def build(target_type, grammar, source, *, lookahead):
    calls.append((target_type, grammar, source, lookahead))
    return expected

parser_target_development.build_combined_python_parser_target = build
actual = PythonParserTarget.from_files(
    Path("legacy.grammar"),
    Path("parser-source"),
    lookahead=5,
)
assert actual is expected
assert calls == [(
    PythonParserTarget,
    Path("legacy.grammar"),
    Path("parser-source"),
    5,
)]
assert ".whl" in parser_target_development.__file__
'''
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [sys.executable, "-c", script, str(wheel)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )

    assert completed.returncode == 0, completed.stderr


def _imported_modules(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.Import):
        return tuple(alias.name for alias in node.names)
    if isinstance(node, ast.ImportFrom) and node.level == 0:
        return () if node.module is None else (node.module,)
    if isinstance(node, ast.Call):
        target = node.func
        is_dynamic_import = (
            isinstance(target, ast.Name)
            and target.id in {"__import__", "import_module"}
        ) or (
            isinstance(target, ast.Attribute)
            and target.attr == "import_module"
        )
        module = (
            node.args[0]
            if node.args
            else next(
                (
                    keyword.value
                    for keyword in node.keywords
                    if keyword.arg == "name"
                ),
                None,
            )
        )
        if (
            is_dynamic_import
            and isinstance(module, ast.Constant)
            and isinstance(module.value, str)
        ):
            return (module.value,)
    return ()


def test_import_scan_catches_constant_dynamic_imports() -> None:
    tree = ast.parse(
        """
importlib.import_module("parsergen.analysis")
import_module("win32com.client")
__import__("comtypes")
__import__(name="pythoncom")
"""
    )

    imported = {
        module
        for node in ast.walk(tree)
        for module in _imported_modules(node)
    }

    assert imported == {
        "parsergen.analysis",
        "win32com.client",
        "comtypes",
        "pythoncom",
    }
