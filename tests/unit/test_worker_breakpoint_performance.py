from __future__ import annotations

from pathlib import Path

import pytest

from onec_runtime.bsl.parser_target import PythonParserTarget
from onec_runtime.bsl.source_maps import SourceUnitKind, SourceUnitRef
from onec_runtime.bsl.worker_reload_source_map import CompactReloadSourceMap
from onec_runtime.performance_profile import PhaseRecorder
from onec_runtime.worker_breakpoints import map_generated_line, resolve_source_line
from test_worker_breakpoints import _debug_view
from test_runtime_api import (
    _attach_breakpoint_workspace,
    _common_module_catalog,
    _semantic_snapshot_runtime,
    _worker_module_unit,
    _worker_module_source_unit,
)


def test_breakpoint_hot_path_reuses_admitted_map_without_parse_or_materialize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    view = _debug_view(tmp_path)
    module = view.modules[0]
    assert isinstance(module.mapped_source.source_map, CompactReloadSourceMap)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("breakpoint hot path repeated admission work")

    monkeypatch.setattr(PythonParserTarget, "parse_ast", forbidden)
    monkeypatch.setattr(CompactReloadSourceMap, "materialize_generic", forbidden)

    for _ in range(1_000):
        mapping = resolve_source_line(module, module.source_unit, 2)
        assert mapping.reason is None
        assert mapping.generated_line is not None
        source = map_generated_line(module, mapping.generated_line)
        assert source is not None
        assert source.source_unit == module.source_unit
        assert source.line == 2


def test_source_line_resolution_uses_bounded_compact_map_lookups(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _common_module_catalog("МодульА")
    padding = tuple(f"// padding {index}" for index in range(5_000))
    source = "\n".join(
        (*padding, "Функция Версия() Экспорт", '    Возврат "large";', "КонецФункции", "")
    )
    unit = _worker_module_source_unit("МодульА", 1, source)
    api = _semantic_snapshot_runtime(tmp_path, catalog)
    api.load_worker_modules((unit,), common_modules=catalog)
    pin = api._worker_universe.pin_active()
    module = api._worker_universe._operation_debug_view(pin).modules[0]
    api._worker_universe.release_pin(pin)
    source_map = module.mapped_source.source_map
    assert isinstance(source_map, CompactReloadSourceMap)
    original = CompactReloadSourceMap.map_offset
    lookup_count = 0

    def counted(self: CompactReloadSourceMap, offset: int):  # type: ignore[no-untyped-def]
        nonlocal lookup_count
        lookup_count += 1
        return original(self, offset)

    monkeypatch.setattr(CompactReloadSourceMap, "map_offset", counted)

    mapping = resolve_source_line(module, module.source_unit, 5_002)

    assert mapping.generated_line == 5_002
    assert lookup_count < 100


def test_generation_publication_records_breakpoint_map_and_workspace_phases(
    tmp_path: Path,
) -> None:
    catalog = _common_module_catalog("МодульА")
    unit = _worker_module_unit("МодульА", 1, catalog)
    api = _semantic_snapshot_runtime(tmp_path, catalog)
    _attach_breakpoint_workspace(api._controller)
    api.add_worker_breakpoint(
        SourceUnitRef(
            SourceUnitKind.MODULE,
            unit.logical_name,
            unit.revision,
            unit.mapped_source.artifact.source_sha256,
        ),
        unit.logical_name.casefold(),
        2,
    )
    profiler = PhaseRecorder()

    api.load_worker_modules((unit,), common_modules=catalog, profiler=profiler)

    phases = {event.phase: event for event in profiler.events}
    assert phases["worker_breakpoint_promotion_map"].item_count == 1
    assert phases["worker_breakpoint_workspace_install"].item_count == 1
