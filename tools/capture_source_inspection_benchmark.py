"""Deterministic synthetic benchmark for CAPTURE source inspection.

The correctness gate is expressed only in work counters.  Generated source
trees and their paths never enter the returned evidence.
"""

from __future__ import annotations

from argparse import ArgumentParser
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import UUID

from onec_runtime.bsl.full_ast_worker_projection import parse_full_ast_module
from onec_runtime.bsl.module_syntax import ModuleSyntaxRegistry
from onec_runtime.capture_inspection import (
    ConfigurationFrameResolver,
    DebugFrame,
    LocalStackAdapter,
)
from onec_runtime.capture_source import (
    CaptureSourceCatalog,
    CaptureSourceConfig,
    SourceVersionRef,
)
from onec_runtime.kernel import COMMON_MODULE_PROPERTY_ID
from onec_runtime.rdbg.models import ModuleLocation, StackFrame, TargetId


SCHEMA = "onec-capture-source-inspection-benchmark-v1"
FRAME_COUNT = 18


class BenchmarkContractError(RuntimeError):
    """The real source-inspection path exceeded its deterministic work gate."""


@dataclass(slots=True)
class _WorkCounters:
    directory_scans: int = 0
    metadata_reads: int = 0
    source_reads: int = 0
    parses: int = 0
    cache_hits: int = 0

    def snapshot(self) -> dict[str, int]:
        return asdict(self)

    def since(self, earlier: dict[str, int]) -> dict[str, int]:
        current = self.snapshot()
        return {name: value - earlier[name] for name, value in current.items()}


class _CountingRegistry(ModuleSyntaxRegistry):
    def __init__(self, counters: _WorkCounters) -> None:
        super().__init__(capacity=FRAME_COUNT)
        self._counters = counters

    def get(self, module, source_sha256, parser_identity):  # type: ignore[no-untyped-def]
        result = super().get(module, source_sha256, parser_identity)
        if result is not None:
            self._counters.cache_hits += 1
        return result


class _StackBackend:
    def __init__(self, frames: tuple[StackFrame, ...]) -> None:
        self.frames = frames
        self.fence = object()
        self.reads = 0

    def read_stack(self, fence: object) -> tuple[StackFrame, ...]:
        if fence is not self.fence:
            raise BenchmarkContractError("benchmark stack fence changed")
        self.reads += 1
        return self.frames


def _module_name(index: int) -> str:
    return f"Module{index:02d}"


def _module_id(index: int) -> UUID:
    return UUID(int=1_000 + index)


def _module_source(index: int) -> str:
    return (
        f"Procedure Method{index:02d}()\n"
        f"    Value = {index};\n"
        "EndProcedure\n"
    )


def _write_synthetic_tree(root: Path, *, edt: bool) -> None:
    if edt:
        source_root = root / "src"
        configuration = source_root / "Configuration"
        configuration.mkdir(parents=True)
        (configuration / "Configuration.mdo").write_text(
            '<mdclass:Configuration xmlns:mdclass="urn:synthetic" '
            f'uuid="{UUID(int=100)}"><name>SyntheticConfiguration</name>'
            "</mdclass:Configuration>",
            encoding="utf-8",
        )
    else:
        source_root = root
        source_root.mkdir(parents=True)
        (source_root / "Configuration.xml").write_text(
            f'<MetaDataObject><Configuration uuid="{UUID(int=100)}">'
            "<Properties><Name>SyntheticConfiguration</Name></Properties>"
            "</Configuration></MetaDataObject>",
            encoding="utf-8",
        )
    common_modules = source_root / "CommonModules"
    common_modules.mkdir()
    for index in range(FRAME_COUNT):
        name = _module_name(index)
        module = common_modules / name
        module.mkdir()
        if edt:
            (module / f"{name}.mdo").write_text(
                '<mdclass:CommonModule xmlns:mdclass="urn:synthetic" '
                f'uuid="{_module_id(index)}"><name>{name}</name>'
                "<server>true</server></mdclass:CommonModule>",
                encoding="utf-8",
            )
            source = module / "Module.bsl"
        else:
            (common_modules / f"{name}.xml").write_text(
                f'<MetaDataObject><CommonModule uuid="{_module_id(index)}">'
                f"<Properties><Name>{name}</Name><Server>true</Server>"
                "</Properties></CommonModule></MetaDataObject>",
                encoding="utf-8",
            )
            extension = module / "Ext"
            extension.mkdir()
            source = extension / "Module.bsl"
        source.write_text(_module_source(index), encoding="utf-8")


def _frames() -> tuple[StackFrame, ...]:
    target = TargetId(UUID(int=1), "synthetic")
    property_id = UUID(COMMON_MODULE_PROPERTY_ID)
    return tuple(
        StackFrame(
            target,
            index,
            ModuleLocation(
                "ConfigModule",
                "",
                _module_id(index),
                property_id,
                2,
            ),
        )
        for index in range(FRAME_COUNT)
    )


def _run_case(case: str, source_root: Path) -> dict[str, object]:
    counters = _WorkCounters()
    registry = _CountingRegistry(counters)
    backend = _StackBackend(_frames())
    original_scandir = os.scandir
    original_metadata_read = Path.read_bytes
    original_source_read = SourceVersionRef.read_text

    def scan(path):  # type: ignore[no-untyped-def]
        counters.directory_scans += 1
        return original_scandir(path)

    def metadata_read(path: Path) -> bytes:
        counters.metadata_reads += 1
        return original_metadata_read(path)

    def source_read(version: SourceVersionRef) -> str:
        counters.source_reads += 1
        return original_source_read(version)

    def parse(source: str):  # type: ignore[no-untyped-def]
        counters.parses += 1
        return parse_full_ast_module(source)

    with (
        patch("os.scandir", scan),
        patch.object(Path, "read_bytes", metadata_read),
        patch.object(SourceVersionRef, "read_text", source_read),
    ):
        start = counters.snapshot()
        catalog = CaptureSourceCatalog(
            (CaptureSourceConfig("synthetic", source_root),)
        )
        adapter = LocalStackAdapter(
            backend,
            backend.fence,
            resolve_sources=ConfigurationFrameResolver(catalog),
            is_runtime_frame=lambda _frame: False,
            registry=registry,
            command_timeout_s=60,
            parse_module=parse,
        )
        page = adapter.stack[:FRAME_COUNT]
        fast_stack = counters.since(start)
        start = counters.snapshot()
        detailed = page.with_methods()
        first_enrichment = counters.since(start)
        start = counters.snapshot()
        cached = page.with_methods()
        cached_enrichment = counters.since(start)

    resolved = tuple(
        frame
        for frame in detailed.frames
        if isinstance(frame, DebugFrame) and frame.method_status == "resolved"
    )
    cached_resolved = tuple(
        frame
        for frame in cached.frames
        if isinstance(frame, DebugFrame) and frame.method_status == "resolved"
    )
    return {
        "case": case,
        "methods_resolved": len(resolved),
        "cached_methods_resolved": len(cached_resolved),
        "stack_reads": backend.reads,
        "fast_stack": fast_stack,
        "first_enrichment": first_enrichment,
        "cached_enrichment": cached_enrichment,
    }


def _validate_case(case: dict[str, object]) -> None:
    zero = {
        "directory_scans": 0,
        "metadata_reads": 0,
        "source_reads": 0,
        "parses": 0,
        "cache_hits": 0,
    }
    expected_fast = dict(zero, directory_scans=1, metadata_reads=FRAME_COUNT + 1)
    expected_first = dict(
        zero,
        source_reads=FRAME_COUNT,
        parses=FRAME_COUNT,
    )
    expected_cached = dict(
        zero,
        source_reads=FRAME_COUNT,
        cache_hits=FRAME_COUNT,
    )
    if (
        case["methods_resolved"] != FRAME_COUNT
        or case["cached_methods_resolved"] != FRAME_COUNT
        or case["stack_reads"] != 1
        or case["fast_stack"] != expected_fast
        or case["first_enrichment"] != expected_first
        or case["cached_enrichment"] != expected_cached
    ):
        raise BenchmarkContractError("capture source work counters exceeded the gate")


def run_benchmark(workspace: Path) -> dict[str, object]:
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    designer = workspace / "designer"
    edt = workspace / "edt"
    _write_synthetic_tree(designer, edt=False)
    _write_synthetic_tree(edt, edt=True)
    cases = [
        _run_case("designer", designer),
        _run_case("edt_project_root", edt),
        _run_case("edt_src_root", edt / "src"),
    ]
    for case in cases:
        _validate_case(case)
    return {"schema": SCHEMA, "frame_count": FRAME_COUNT, "cases": cases}


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    with TemporaryDirectory(prefix="onec-capture-source-") as directory:
        result = run_benchmark(Path(directory))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
