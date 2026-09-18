from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest

from onec_runtime.bsl.full_ast_worker_projection import (
    full_ast_parser_identity, parse_full_ast_module,
)
from onec_runtime.bsl.module_syntax import ModuleIdentity, ModuleSyntaxRegistry
from onec_runtime.capture_source import SourceVersionRef
from onec_runtime.errors import (
    ProtocolError,
)
from onec_runtime.rdbg.models import (
    ModuleLocation,
    StackFrame,
    TargetId,
)


SOURCE = "Procedure RunFixture(Arg)\nX = 1;\nEndProcedure"
TARGET = TargetId(UUID(int=1), "private-alias")
LOCATION = ModuleLocation("ConfigModule", "file:///private/secret.bsl", UUID(int=2), UUID(int=3), 2)
IDENTITY = ModuleIdentity("opaque", "worker", "common", "fixture", "Module")


def api():
    from onec_runtime import capture_inspection
    return capture_inspection


class Backend:
    def __init__(self):
        self.fence = object()
        self.calls = []
        self.frames = tuple(StackFrame(TARGET, n, LOCATION) for n in range(6))
        self.phase = "CAPTURE"

    def read_stack(self, fence):
        self.calls.append(fence)
        if fence is not self.fence:
            raise ProtocolError("stale exact fence")
        return self.frames


def setup_stack(*, sources=None, runtime=(), registry=None, parser=None, clock=None,
                command_timeout_s=5, max_source_bytes=1024):
    module = api()
    backend = Backend()
    resolutions = []
    if sources is None:
        pin = SourceVersionRef.worker(artifact_id="private-artifact", generation=1, source_text=SOURCE)
        sources = {n: module.ResolvedFrameSource("Common.RunFixture", 2, IDENTITY, pin) for n in range(6)}

    def resolve(frames):
        resolutions.append(frames)
        return tuple(sources.get(frame.level) for frame in frames)

    kwargs = {}
    if parser is not None:
        kwargs["parse_module"] = parser
    if clock is not None:
        kwargs["clock"] = clock
    adapter = module.LocalStackAdapter(
        backend, backend.fence, resolve_sources=resolve,
        is_runtime_frame=lambda frame: frame.level in runtime,
        registry=registry or ModuleSyntaxRegistry(),
        command_timeout_s=command_timeout_s, max_source_bytes=max_source_bytes,
        **kwargs,
    )
    return adapter, backend, resolutions


def forbidden(*args, **kwargs):
    raise AssertionError("unexpected source/parser operation")


def test_fast_pages_are_fresh_immutable_and_render_without_parsing(monkeypatch):
    adapter, backend, resolutions = setup_stack(parser=forbidden)
    first = adapter.stack[:2]
    backend.frames = (replace(backend.frames[0], location=replace(LOCATION, line=8)),)
    second = adapter.stack[:2]
    assert first.total == 6 and second.total == 1
    assert len(first.frames) == 2
    assert first.detail == "line" and first.frames[0].method_status == "not_requested"
    with pytest.raises(FrozenInstanceError):
        first.frames[0].line = 99
    with pytest.raises(FrozenInstanceError):
        first.frames = ()
    assert backend.calls == [backend.fence, backend.fence]
    assert len(resolutions) == 2
    monkeypatch.setattr(Path, "iterdir", forbidden)
    rendered = repr(first) + str(first) + repr(first.frames[0])
    assert "Common.RunFixture" in rendered
    for private in ("secret", "private", str(LOCATION.object_id), str(LOCATION.property_id), SOURCE):
        assert private not in rendered


def test_visible_levels_skip_collapsed_runtime_markers_and_unknowns_stay_visible():
    adapter, backend, resolutions = setup_stack(sources={}, runtime=(0, 1, 3, 4))
    page = adapter.stack[:20]
    assert page.total == 2
    assert [frame.native_level for frame in page.frames if isinstance(frame, api().DebugFrame)] == [2, 5]
    assert [frame.count for frame in page.frames if isinstance(frame, api().RuntimeFrameMarker)] == [2, 2]
    frame = adapter.stack[0]
    assert frame.native_level == 2 and frame.line == 2
    assert frame.source_status == "unavailable"
    assert "исходный файл не найден" in str(frame)
    assert [frame.level for frame in resolutions[0]] == [2, 5]


def test_visible_frame_labels_match_indexes_across_hidden_runs_and_nonzero_slices():
    adapter, _, _ = setup_stack(runtime=(0, 1, 3, 4))
    page = adapter.stack[:20]
    frames = tuple(frame for frame in page.frames if isinstance(frame, api().DebugFrame))
    assert [str(frame).split(" ", 1)[0] for frame in frames] == ["#0", "#1"]
    assert [frame.visible_index for frame in frames] == [0, 1]
    assert [frame.native_level for frame in frames] == [2, 5]
    assert str(adapter.stack[0]).startswith("#0 ")
    assert str(adapter.stack[1]).startswith("#1 ")
    assert "#2 " not in str(page) and "#5 " not in str(page)

    later = adapter.stack[1:2]
    later_frame = next(frame for frame in later.frames if isinstance(frame, api().DebugFrame))
    assert later_frame.visible_index == 1 and later_frame.native_level == 5
    assert str(later_frame).startswith("#1 ")
    enriched = later.with_methods()
    enriched_frame = next(frame for frame in enriched.frames if isinstance(frame, api().DebugFrame))
    assert enriched_frame.method_status == "resolved"
    assert str(enriched_frame).startswith("#1 ") and enriched_frame.native_level == 5


def test_native_stack_uses_physical_levels_across_parser_gaps():
    from onec_runtime.rdbg.xml_codec import parse_call_stack

    def frame(level: int, *, addressable: bool = True) -> str:
        module = (
            f"<moduleID><type>ConfigModule</type>"
            f"<objectID>{UUID(int=100 + level)}</objectID>"
            f"<propertyID>{UUID(int=200 + level)}</propertyID></moduleID>"
            if addressable
            else "<moduleID/>"
        )
        return f"<callStack>{module}<lineNo>{10 + level}</lineNo></callStack>"

    payload = (
        "<response>"
        + frame(3)
        + frame(2)
        + frame(1, addressable=False)
        + frame(0)
        + "</response>"
    ).encode()
    parsed = tuple(parse_call_stack(payload, TARGET))
    assert [item.level for item in parsed] == [0, 2, 3]
    adapter, backend, _ = setup_stack()
    backend.frames = parsed

    assert adapter.stack.native[2].native_level == 2
    with pytest.raises(IndexError):
        adapter.stack.native[1]
    middle = adapter.stack.native[1:3]
    last = adapter.stack.native[3:4]
    visible = adapter.stack[:20]

    assert middle.total == last.total == 4
    assert [item.native_level for item in middle.frames] == [2]
    assert middle.next_cursor == 3
    assert [item.native_level for item in last.frames] == [3]
    assert last.next_cursor is None
    visible_frames = tuple(
        item for item in visible.frames if isinstance(item, api().DebugFrame)
    )
    assert visible.total == 3
    assert [item.visible_index for item in visible_frames] == [0, 1, 2]
    assert [item.native_level for item in visible_frames] == [0, 2, 3]


def test_native_frame_labels_explicitly_identify_the_physical_coordinate():
    adapter, _, _ = setup_stack(runtime=(0, 1))
    assert str(adapter.stack.native[0]).startswith("native #0 ")
    frame = adapter.stack.native[3]
    assert str(frame).startswith("native #3 ")
    assert frame.visible_index is None and frame.native_level == 3


def test_saved_page_rejects_a_mutable_frame_container():
    with pytest.raises(TypeError, match="tuple"):
        api().StackPage([], 0, None)


def test_visible_slice_beyond_last_source_frame_does_not_repeat_trailing_markers():
    adapter, _, _ = setup_stack(runtime=(1, 2, 3, 4, 5))
    assert len(adapter.stack[:1].frames) == 2
    assert adapter.stack[1:2].frames == ()
    with pytest.raises(IndexError):
        adapter.stack[1]


def test_native_mode_has_no_source_or_directory_or_parser_work(monkeypatch):
    adapter, backend, resolutions = setup_stack(runtime=(0, 1), parser=forbidden)
    monkeypatch.setattr(Path, "iterdir", forbidden)
    monkeypatch.setattr(Path, "stat", forbidden)
    first = adapter.stack.native[:20]
    second = adapter.stack.native[3]
    assert len(first.frames) == 6 and first.total == 6
    assert second.native_level == 3
    assert resolutions == [] and len(backend.calls) == 2
    assert first.frames[0].runtime_kernel
    assert "secret" not in repr(first) + str(first)
    assert str(LOCATION.object_id) not in repr(second)
    assert second.physical.object_id == LOCATION.object_id
    assert not hasattr(second.physical, "url")


def test_fresh_requests_pass_exact_fence_and_reject_stale_before_resolution():
    adapter, backend, resolutions = setup_stack()
    backend.fence = object()
    with pytest.raises(ProtocolError, match="stale"):
        adapter.stack[:2]
    with pytest.raises(ProtocolError, match="stale"):
        adapter.stack.native[0]
    assert resolutions == []


@pytest.mark.parametrize("key", [-1, slice(None), slice(0, 101), slice(0, 2, 2), True])
def test_stack_requests_reject_unbounded_or_invalid_coordinates_before_backend(key):
    adapter, backend, _ = setup_stack()
    with pytest.raises((ValueError, TypeError)):
        adapter.stack[key]
    assert backend.calls == []


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("step", [True, False, 1.0, 2.0])
def test_slice_rejects_boolean_and_float_steps_before_reading_inventory(native, step):
    adapter, backend, resolutions = setup_stack()
    descriptor = adapter.stack.native if native else adapter.stack
    with pytest.raises(TypeError):
        descriptor[slice(0, 2, step)]
    assert backend.calls == [] and resolutions == []


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("start", [0, 1, 6, 9])
def test_empty_slice_has_no_continuation_cursor(native, start):
    adapter, backend, _ = setup_stack(runtime=(0, 1, 3, 4))
    descriptor = adapter.stack.native if native else adapter.stack
    page = descriptor[start:start:1]
    assert page.frames == ()
    assert page.next_cursor is None
    assert page.total == (6 if native else 2)
    assert backend.calls == [backend.fence]


@pytest.mark.parametrize("step", [None, 1])
def test_nonempty_unit_step_slices_advance_visible_cursor_then_end(step):
    adapter, _, _ = setup_stack(runtime=(0, 1, 3, 4))
    first = adapter.stack[slice(0, 1, step)]
    assert first.next_cursor == 1 and first.total == 2
    last = adapter.stack[slice(1, 2, step)]
    assert last.next_cursor is None and last.total == 2


def test_enrichment_parses_one_missing_exact_version_without_rereading_saved_frames():
    calls = []
    def parse(text):
        calls.append(text)
        return parse_full_ast_module(text)
    registry = ModuleSyntaxRegistry()
    adapter, backend, resolutions = setup_stack(parser=parse, registry=registry)
    page = adapter.stack[:4]
    backend.fence = object()  # Local enrichment remains available after the stop ends.
    detailed = page.with_methods()
    assert calls == [SOURCE]
    assert len(backend.calls) == 1 and len(resolutions) == 1
    assert all(frame.method.name == "RunFixture" for frame in detailed.frames)
    assert all(frame.method.parameters == ("Arg",) for frame in detailed.frames)
    assert detailed.detail == "method" and page.detail == "line"
    assert page.frames[0].method is None
    assert detailed.frames[0].source_sha256 == parse_full_ast_module(SOURCE).source_sha256
    assert registry.get(IDENTITY, detailed.frames[0].source_sha256, full_ast_parser_identity()) is not None
    assert page.frames[0].with_method().method.name == "RunFixture"
    assert calls == [SOURCE]
    assert backend.phase == "CAPTURE"


def test_evicted_saved_page_reparses_exact_source_without_fast_work(monkeypatch):
    registry = ModuleSyntaxRegistry(capacity=1)
    original_index = parse_full_ast_module(SOURCE).syntax_index
    registry.publish(IDENTITY, original_index)
    parse_calls = []
    source_reads = []
    original_read = SourceVersionRef.read_text

    def observed_parse(text):
        parse_calls.append(text)
        return parse_full_ast_module(text)

    def observed_read(version):
        source_reads.append(version)
        return original_read(version)

    monkeypatch.setattr(SourceVersionRef, "read_text", observed_read)
    adapter, backend, _ = setup_stack(parser=observed_parse, registry=registry)

    saved_page = adapter.stack[:4]
    assert parse_calls == []
    assert source_reads == []

    other_source = SOURCE.replace("RunFixture", "OtherFixture")
    registry.publish(
        replace(IDENTITY, object_id="other"),
        parse_full_ast_module(other_source).syntax_index,
    )
    assert registry.get(
        IDENTITY, original_index.source_sha256, original_index.parser_identity
    ) is None
    backend.fence = object()

    detailed = saved_page.with_methods()

    assert parse_calls == [SOURCE]
    assert len(source_reads) == 1
    assert all(frame.method.name == "RunFixture" for frame in detailed.frames)
    assert registry.get(
        IDENTITY, original_index.source_sha256, original_index.parser_identity
    ) is not None


def test_existing_registry_version_and_old_worker_pin_need_no_parse():
    registry = ModuleSyntaxRegistry()
    old = parse_full_ast_module(SOURCE).syntax_index
    new = parse_full_ast_module(SOURCE.replace("RunFixture", "NewFixture")).syntax_index
    registry.publish(IDENTITY, old)
    registry.publish(IDENTITY, new)
    adapter, _, _ = setup_stack(registry=registry, parser=forbidden)
    assert adapter.stack[0].with_method().method.name == "RunFixture"


def test_native_physical_identity_keeps_extension_discriminator_without_private_text():
    adapter, backend, _ = setup_stack()
    backend.frames = (
        StackFrame(TARGET, 0, LOCATION),
        StackFrame(TARGET, 1, replace(LOCATION, module_type="ExtensionModule", extension_name="Addon")),
    )
    page = adapter.stack.native[:2]
    assert page.frames[0].physical != page.frames[1].physical
    assert page.frames[1].physical.extension_name == "Addon"
    backend.frames = (replace(backend.frames[0], location=replace(LOCATION, line=42)),)
    assert adapter.stack.native[0].line == 42
    assert page.frames[0].line == 2


def test_display_bounds_a_large_method_signature():
    large = "P" * 900
    source = f"Procedure {large}()\nEndProcedure"
    pin = SourceVersionRef.worker(artifact_id="artifact", generation=1, source_text=source)
    sources = {0: api().ResolvedFrameSource("Common.Module", 1, IDENTITY, pin)}
    adapter, _, _ = setup_stack(sources=sources, max_source_bytes=2000)
    frame = adapter.stack[0].with_method()
    assert frame.method.name == large
    assert len(str(frame)) < 600


def export_sources(tmp_path, texts):
    result = {}
    for level, text in enumerate(texts):
        path = tmp_path / f"module{level}.bsl"
        path.write_text(text, encoding="utf-8-sig")
        result[level] = api().ResolvedFrameSource(
            f"Common.Module{level}", 2, replace(IDENTITY, object_id=f"module{level}"),
            SourceVersionRef.trusted_export(path),
        )
    return result


def test_changed_export_gets_per_frame_status_and_retains_safe_line_output(tmp_path):
    sources = export_sources(tmp_path, [SOURCE, SOURCE])
    adapter, _, _ = setup_stack(sources=sources)
    page = adapter.stack[:2]
    sources[0].version.path.write_text(SOURCE + "\n// changed", encoding="utf-8")
    result = page.with_methods()
    assert [frame.method_status for frame in result.frames] == ["source_changed", "resolved"]
    assert result.frames[0].line == 2 and result.frames[0].source == "Common.Module0"
    assert str(tmp_path) not in repr(result)


def test_export_shared_pin_reads_once_and_reuses_exact_registry_version(tmp_path, monkeypatch):
    sources = export_sources(tmp_path, [SOURCE])
    sources[1] = sources[0]
    registry = ModuleSyntaxRegistry()
    registry.publish(sources[0].identity, parse_full_ast_module(sources[0].version.read_text()).syntax_index)
    adapter, _, _ = setup_stack(sources=sources, registry=registry, parser=forbidden)
    reads = []
    original = SourceVersionRef.read_text
    def read(pin):
        reads.append(pin)
        return original(pin)
    monkeypatch.setattr(SourceVersionRef, "read_text", read)
    page = adapter.stack[:2].with_methods()
    assert [frame.method.name for frame in page.frames] == ["RunFixture", "RunFixture"]
    assert len(reads) == 1


def test_basic_and_native_paths_never_call_generated_parser(monkeypatch):
    from onec_runtime.bsl.parser_target import PythonParserTarget
    monkeypatch.setattr(PythonParserTarget, "parse_tokens_ast", forbidden)
    adapter, _, _ = setup_stack()
    assert "Common.RunFixture" in str(adapter.stack[:2])
    assert "Модуль конфигурации" in repr(adapter.stack.native[:2])


class Clock:
    now = 0.0
    def __call__(self):
        return self.now


def test_soft_budget_checks_after_synchronous_parse_and_keeps_prior_results(tmp_path):
    clock = Clock()
    calls = []
    def parse(text):
        calls.append(text)
        if len(calls) == 2:
            clock.now += 2
        return parse_full_ast_module(text)
    sources = export_sources(tmp_path, [SOURCE, SOURCE, SOURCE])
    adapter, backend, _ = setup_stack(sources=sources, parser=parse, clock=clock, command_timeout_s=1)
    result = adapter.stack[:3].with_methods(work_budget_s=100)
    assert [frame.method_status for frame in result.frames] == ["resolved", "timeout", "timeout"]
    assert len(calls) == 2 and backend.phase == "CAPTURE"


def test_budget_is_checked_before_read_and_after_read_before_parse(tmp_path, monkeypatch):
    clock = Clock()
    sources = export_sources(tmp_path, [SOURCE])
    adapter, _, _ = setup_stack(sources=sources, parser=forbidden, clock=clock)
    page = adapter.stack[:1]
    reads = []
    original = SourceVersionRef.read_text
    def read(pin):
        reads.append(pin)
        clock.now += 2
        return original(pin)
    monkeypatch.setattr(SourceVersionRef, "read_text", read)
    assert page.with_methods(work_budget_s=0).frames[0].method_status == "timeout"
    assert reads == []
    assert page.with_methods(work_budget_s=1).frames[0].method_status == "timeout"
    assert len(reads) == 1


@pytest.mark.parametrize("changed", [False, True])
def test_budget_is_checked_after_a_failed_source_read(tmp_path, monkeypatch, changed):
    from onec_runtime.capture_source import CaptureSourceChangedError
    clock = Clock()
    sources = export_sources(tmp_path, [SOURCE])
    adapter, backend, _ = setup_stack(sources=sources, parser=forbidden, clock=clock)
    page = adapter.stack[:1]
    def read(pin):
        clock.now += 2
        raise CaptureSourceChangedError("source_changed") if changed else OSError("private path")
    monkeypatch.setattr(SourceVersionRef, "read_text", read)
    result = page.with_methods(work_budget_s=1)
    assert result.frames[0].method_status == "timeout"
    assert "private path" not in repr(result)
    assert backend.phase == "CAPTURE"


def test_oversized_export_is_rejected_before_read_and_parse(tmp_path, monkeypatch):
    sources = export_sources(tmp_path, [SOURCE * 50])
    adapter, _, _ = setup_stack(sources=sources, parser=forbidden, max_source_bytes=100)
    page = adapter.stack[:1]
    monkeypatch.setattr(SourceVersionRef, "read_text", forbidden)
    frame = page.with_methods().frames[0]
    assert frame.method_status == "unavailable" and frame.method_reason == "source_too_large"
    assert frame.line == 2 and "Common.Module0" in str(frame)


def test_unparseable_and_ambiguous_lines_fall_back_without_claiming_a_method(tmp_path):
    sources = export_sources(tmp_path, ["Procedure Bad(\n?", "Procedure P()\nEndProcedure Procedure Q()\nEndProcedure"])
    adapter, _, _ = setup_stack(sources=sources)
    result = adapter.stack[:2].with_methods()
    assert [frame.method_status for frame in result.frames] == ["unavailable", "unavailable"]
    assert all(frame.method is None for frame in result.frames)


def test_configuration_resolver_builds_stable_private_binding_namespaces():
    from onec_runtime.capture_source import CaptureSourceCatalog, CaptureSourceConfig
    from onec_runtime.kernel import COMMON_MODULE_PROPERTY_ID
    root = Path(__file__).parents[1] / "fixtures" / "onec" / "capture_sources"
    catalog = CaptureSourceCatalog(tuple(CaptureSourceConfig("demo", root / f"designer_{layer}")
                                         for layer in ("base", "extension")))
    resolver = api().ConfigurationFrameResolver(catalog)
    location = replace(LOCATION, object_id=UUID("11111111-2222-3333-4444-555555555555"),
                       property_id=UUID(COMMON_MODULE_PROPERTY_ID))
    frames = (StackFrame(TARGET, 0, location), StackFrame(TARGET, 1, replace(
        location, module_type="ExtensionModule", extension_name="Дополнение")))
    first = resolver(frames)
    second = resolver(frames)
    assert first[0].identity == second[0].identity
    assert first[0].identity != first[1].identity
    assert first[0].identity.namespace != first[1].identity.namespace
    assert str(root) not in first[0].identity.namespace
    assert first[0].source == "ОбщийМодуль.Общий.Модуль"
    assert first[0].version.source_status == "trusted_export"
