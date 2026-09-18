from __future__ import annotations

from pathlib import Path

import pytest

from onec_runtime.bsl.parser_target import PythonParserTarget
from onec_runtime.bsl.worker_reload_source_map import CompactReloadSourceMap
from onec_runtime.worker_breakpoints import map_generated_line, resolve_source_line
from worker_debug_fixtures import worker_debug_view


def test_breakpoint_hot_path_reuses_admitted_map_without_parse_or_materialize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    view = worker_debug_view(tmp_path)
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
    padding = tuple(f"// padding {index}" for index in range(5_000))
    source = "\n".join(
        (*padding, "Функция Версия() Экспорт", '    Возврат "large";', "КонецФункции", "")
    )
    module = worker_debug_view(tmp_path, revision=1, source=source).modules[0]
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
