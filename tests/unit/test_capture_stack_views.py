from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from uuid import UUID

import pytest

from onec_runtime.bsl.full_ast_worker_projection import (
    full_ast_parser_identity, parse_full_ast_module,
)
from onec_runtime.bsl.module_syntax import ModuleIdentity, ModuleSyntaxRegistry
from onec_runtime.capture_source import SourceVersionRef
from onec_runtime.errors import (
    CaptureSourceUnavailableError, ProtocolError, StaleCaptureError,
)
from onec_runtime.rdbg.models import ModuleLocation, StackFrame, StopEvent, TargetId
from onec_runtime.runtime_api import PrototypeRuntimeApi

from test_prototype_runtime import CAPTURE_A, SERVICE, ScriptedSession, captured_controller


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


class FreshStackSession(ScriptedSession):
    def __init__(self) -> None:
        super().__init__((CAPTURE_A,), stacks=((CAPTURE_A, LOCATION, SERVICE),))
        target = self.target.target_id
        self.live_frames = (
            StackFrame(target, 0, CAPTURE_A),
            StackFrame(target, 1, LOCATION),
            StackFrame(target, 2, SERVICE),
        )
        self.stack_reads = 0

    def read_current_stack(self, *, timeout_s: float) -> StopEvent:
        assert timeout_s > 0
        self.stack_reads += 1
        locations = tuple(frame.location for frame in self.live_frames)
        return StopEvent(
            self.live_frames[0].target_id, locations[0], "recoveredCallStack", stack=locations,
            stack_frames=self.live_frames,
        )


def captured_stack_api() -> tuple[PrototypeRuntimeApi, object, FreshStackSession]:
    session = FreshStackSession()
    controller = captured_controller(session)
    return PrototypeRuntimeApi(controller), controller, session


def test_runtime_capture_view_attaches_fresh_visible_and_native_stack_pages() -> None:
    runtime, _, session = captured_stack_api()
    capture = runtime.current_capture()

    first = capture.stack[:20]
    session.live_frames = (
        session.live_frames[0],
        replace(session.live_frames[1], location=replace(LOCATION, line=8)),
        session.live_frames[2],
    )
    second = capture.stack[:20]
    native = capture.stack.native[:20]

    assert session.stack_reads == 3
    assert first.total == second.total == 1
    first_frame = next(frame for frame in first.frames if isinstance(frame, api().DebugFrame))
    second_frame = next(frame for frame in second.frames if isinstance(frame, api().DebugFrame))
    assert first_frame.native_level == second_frame.native_level == 1
    assert first_frame.line == 2 and second_frame.line == 8
    assert native.total == 3
    assert [frame.native_level for frame in native.frames] == [0, 1, 2]
    assert native.frames[0].runtime_kernel and native.frames[2].runtime_kernel


def test_runtime_stack_validates_the_exact_capture_fence_before_rdbg() -> None:
    runtime, controller, session = captured_stack_api()
    capture = runtime.current_capture()
    controller.stop_sequence += 1

    with pytest.raises(StaleCaptureError):
        capture.stack[:1]

    assert session.stack_reads == 0


def test_runtime_native_stack_bypasses_config_source_resolution_and_ast() -> None:
    runtime, _, session = captured_stack_api()
    source_calls = []
    runtime._capture_stack_source_resolver = (
        lambda frames: source_calls.append(frames) or (None,) * len(frames)
    )

    page = runtime.current_capture().stack.native[:20]

    assert page.total == 3 and session.stack_reads == 1
    assert source_calls == []


def test_runtime_frame_scope_is_explicitly_deferred_to_value_binding() -> None:
    runtime, _, _ = captured_stack_api()
    frame = runtime.current_capture().stack[0]

    for attribute in ("variables", "parameters", "locals"):
        with pytest.raises(CaptureSourceUnavailableError, match="not attached"):
            getattr(frame, attribute)


def test_session_current_capture_binds_sources_methods_and_frame_value_scope() -> None:
    from onec_runtime.capture_inspection import ResolvedFrameSource
    from onec_runtime.capture_values import (
        CaptureValuePolicy, LocalCaptureValueAdapter, PrivateValueProjection,
    )
    from onec_runtime.session import RuntimeSession

    runtime, _, session = captured_stack_api()
    pin = SourceVersionRef.worker(
        artifact_id="binding-fixture", generation=1, source_text=SOURCE,
    )
    resolved = ResolvedFrameSource("Common.RunFixture", 2, IDENTITY, pin)

    class ValueBackend:
        def validate_inspection(self, fence):
            return None
        def project_values(self, fence, request):
            return PrivateValueProjection((), 0, None)
        def resolve_value(self, fence, path):
            raise AssertionError("not used")
        def discover_table_columns(self, fence, path, limit):
            raise AssertionError("not used")

    values = LocalCaptureValueAdapter(
        ValueBackend(), object(),
        policy=CaptureValuePolicy(lambda fence, handle: False),
        resolve_parameters=lambda root: ("Arg",),
    )
    core = object.__new__(RuntimeSession)
    core.runtime_api = runtime
    core._capture_stack_source_resolver = (
        lambda frames: tuple(resolved if frame.level == 1 else None for frame in frames)
    )
    core._capture_stack_frame_binder = values.bind_frame

    capture = core.current_capture()
    frame = capture.stack[0]
    detailed = frame.with_method()

    assert session.stack_reads == 1
    assert detailed.method.name == "RunFixture"
    assert detailed.method.parameters == ("Arg",)
    assert frame.variables._root.native_level == 1
    assert frame.parameters._root.native_level == 1
    assert frame.locals._root.native_level == 1


def test_stack_views_keep_target_urls_and_physical_ids_out_of_ordinary_repr() -> None:
    runtime, _, _ = captured_stack_api()
    capture = runtime.current_capture()
    page = capture.stack[:20]
    public = repr(capture) + repr(page) + repr(page.frames[0])

    assert "private-alias" not in public
    assert LOCATION.url not in public
    assert str(LOCATION.object_id) not in public
    assert str(LOCATION.property_id) not in public
    assert "identity=" not in public and "_resolved" not in public


def test_runtime_stack_uses_main_pinned_worker_source_and_shared_syntax(tmp_path) -> None:
    from test_runtime_api import (
        _common_module_catalog, _semantic_snapshot_runtime, _worker_module_unit,
    )
    from onec_runtime.worker_breakpoints import resolve_source_line

    catalog = _common_module_catalog("МодульА")
    first_unit = _worker_module_unit("МодульА", 1, catalog)
    second_unit = _worker_module_unit("МодульА", 2, catalog)
    worker_runtime = _semantic_snapshot_runtime(tmp_path, catalog)
    first_generation = worker_runtime.load_worker_modules(
        (first_unit,), common_modules=catalog,
    )
    operation_pin = worker_runtime._worker_universe.pin_active()
    first_view = worker_runtime._worker_universe._operation_debug_view(operation_pin)
    first_module = first_view.modules[0]
    generated_line = resolve_source_line(
        first_module, first_module.source_unit, 1,
    ).generated_line
    assert generated_line is not None

    _, controller, session = captured_stack_api()
    worker_runtime._controller = controller
    worker_runtime._operation_generation_pin = operation_pin
    worker_runtime.load_worker_modules((second_unit,), common_modules=catalog)
    session.live_frames = (
        session.live_frames[0],
        StackFrame(
            session.live_frames[0].target_id,
            1,
            first_module.registration.module_location(generated_line),
        ),
        session.live_frames[2],
    )

    frame = worker_runtime.current_capture().stack[0]
    detailed = frame.with_method()

    assert frame.source == "МодульА"
    assert frame.source_status == "runtime_verified"
    assert detailed.method.name == "Версия"
    assert detailed.source_sha256 == first_unit.mapped_source.artifact.source_sha256
    assert first_generation is operation_pin.handle
    assert worker_runtime.worker_generation_handle is not first_generation


def test_runtime_stack_resolves_multiple_notebook_cells_from_historical_generation(
    tmp_path,
) -> None:
    from test_runtime_api import _common_module_catalog, _semantic_snapshot_runtime
    from onec_runtime.bsl.source_maps import SourceUnitKind, SourceUnitRef, source_sha256
    from onec_runtime.worker_breakpoints import resolve_source_line

    worker_runtime = _semantic_snapshot_runtime(tmp_path, _common_module_catalog())
    sources = (
        "Процедура Первый()\n    А = 1;\nКонецПроцедуры;",
        "Процедура Второй()\n    Б = 2;\nКонецПроцедуры;",
        "Процедура Третий()\n    В = 3;\nКонецПроцедуры;",
    )
    units = tuple(
        SourceUnitRef(
            SourceUnitKind.NOTEBOOK_CELL,
            f"Cell{index}",
            index,
            source_sha256(source),
        )
        for index, source in enumerate(sources, start=1)
    )
    for source, unit in zip(sources[:2], units[:2], strict=True):
        worker_runtime.execute_bsl(source, source_unit=unit)
    historical_pin = worker_runtime._worker_universe.pin_active()
    historical_view = worker_runtime._worker_universe._operation_debug_view(
        historical_pin
    )
    historical_module = historical_view.modules[0]
    generated_lines = tuple(
        resolve_source_line(historical_module, unit, 2).generated_line
        for unit in units[:2]
    )
    assert all(line is not None for line in generated_lines)

    worker_runtime.execute_bsl(sources[2], source_unit=units[2])
    _, controller, session = captured_stack_api()
    worker_runtime._controller = controller
    worker_runtime._operation_generation_pin = historical_pin
    session.live_frames = (
        session.live_frames[0],
        *(
            StackFrame(
                session.live_frames[0].target_id,
                level,
                historical_module.registration.module_location(line),
            )
            for level, line in enumerate(generated_lines, start=1)
            if line is not None
        ),
        replace(session.live_frames[2], level=3),
    )

    page = worker_runtime.current_capture().stack[:20].with_methods()
    frames = tuple(frame for frame in page.frames if isinstance(frame, api().DebugFrame))

    assert [frame.source_status for frame in frames] == [
        "runtime_verified",
        "runtime_verified",
    ]
    assert [frame.method.name for frame in frames] == ["Первый", "Второй"]
    assert [frame._resolved.version.generation for frame in frames] == [
        historical_pin.handle.generation,
        historical_pin.handle.generation,
    ]
    assert worker_runtime.worker_generation_handle is not historical_pin.handle


def test_common_source_is_repinned_after_notebook_publication_and_history_survives(
    tmp_path,
) -> None:
    from test_runtime_api import (
        _common_module_catalog, _semantic_snapshot_runtime, _worker_module_unit,
    )
    from onec_runtime.bsl.source_maps import SourceUnitKind, SourceUnitRef, source_sha256
    from onec_runtime.worker_breakpoints import resolve_source_line

    catalog = _common_module_catalog("МодульА")
    worker_runtime = _semantic_snapshot_runtime(tmp_path, catalog)
    first_handle = worker_runtime.load_worker_modules(
        (_worker_module_unit("МодульА", 1, catalog),), common_modules=catalog,
    )
    historical_pin = worker_runtime._worker_universe.pin_active()
    notebook_source = "Процедура ИзНоутбука()\n    А = 1;\nКонецПроцедуры;"
    notebook_unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL,
        "CellAfterCommon",
        1,
        source_sha256(notebook_source),
    )
    worker_runtime.execute_bsl(notebook_source, source_unit=notebook_unit)
    current_pin = worker_runtime._worker_universe.pin_active()
    current_view = worker_runtime._worker_universe._operation_debug_view(current_pin)
    common_module = next(
        module for module in current_view.modules
        if module.canonical_module == "модульа"
    )
    generated_line = resolve_source_line(
        common_module, common_module.source_unit, 2,
    ).generated_line
    assert generated_line is not None

    _, controller, session = captured_stack_api()
    worker_runtime._controller = controller
    worker_runtime._operation_generation_pin = current_pin
    session.live_frames = (
        session.live_frames[0],
        StackFrame(
            session.live_frames[0].target_id,
            1,
            common_module.registration.module_location(generated_line),
        ),
        session.live_frames[2],
    )

    current_frame = worker_runtime.current_capture().stack[0]
    worker_runtime._operation_generation_pin = historical_pin
    historical_module = worker_runtime._worker_universe._operation_debug_view(
        historical_pin
    ).modules[0]
    historical_line = resolve_source_line(
        historical_module, historical_module.source_unit, 2,
    ).generated_line
    assert historical_line is not None
    session.live_frames = (
        session.live_frames[0],
        StackFrame(
            session.live_frames[0].target_id,
            1,
            historical_module.registration.module_location(historical_line),
        ),
        session.live_frames[2],
    )
    historical_frame = worker_runtime.current_capture().stack[0]

    assert current_frame.source_status == historical_frame.source_status == "runtime_verified"
    assert current_frame._resolved.version.generation == current_pin.handle.generation
    assert historical_frame._resolved.version.generation == first_handle.generation


def test_shared_source_unit_keeps_distinct_common_and_notebook_physical_owners(
    tmp_path,
) -> None:
    from test_runtime_api import (
        _common_module_catalog, _semantic_snapshot_runtime, _worker_module_unit,
    )
    from onec_runtime.bsl.source_maps import SourceUnitKind, SourceUnitRef, source_sha256
    from onec_runtime.worker_breakpoints import resolve_source_line

    catalog = _common_module_catalog("МодульА")
    common_unit = _worker_module_unit("МодульА", 1, catalog)
    shared_source = common_unit.mapped_source.text
    shared_ref = common_unit.mapped_source.source_map.map_offset(0).unit
    assert shared_ref is not None
    worker_runtime = _semantic_snapshot_runtime(tmp_path, catalog)
    worker_runtime.load_worker_modules((common_unit,), common_modules=catalog)
    historical_pin = worker_runtime._worker_universe.pin_active()

    worker_runtime.execute_bsl(shared_source, source_unit=shared_ref)
    shared_pin = worker_runtime._worker_universe.pin_active()
    shared_view = worker_runtime._worker_universe._operation_debug_view(shared_pin)
    common_module = next(
        module for module in shared_view.modules
        if module.canonical_module == "модульа"
    )
    notebook_module = next(
        module for module in shared_view.modules
        if module.canonical_module == "worker"
    )
    current_lines = tuple(
        resolve_source_line(module, shared_ref, 2).generated_line
        for module in (common_module, notebook_module)
    )
    assert all(line is not None for line in current_lines)

    later_source = "Процедура Позже()\n    А = 3;\nКонецПроцедуры;"
    later_ref = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL,
        "PrivateLaterCell",
        3,
        source_sha256(later_source),
    )
    worker_runtime.execute_bsl(later_source, source_unit=later_ref)
    _, controller, session = captured_stack_api()
    worker_runtime._controller = controller
    worker_runtime._operation_generation_pin = shared_pin
    session.live_frames = (
        session.live_frames[0],
        *(
            StackFrame(
                session.live_frames[0].target_id,
                level,
                module.registration.module_location(line),
            )
            for level, (module, line) in enumerate(
                zip((common_module, notebook_module), current_lines, strict=True),
                start=1,
            )
            if line is not None
        ),
        replace(session.live_frames[2], level=3),
    )

    current_page = worker_runtime.current_capture().stack[:20].with_methods()
    current_frames = tuple(
        frame for frame in current_page.frames if isinstance(frame, api().DebugFrame)
    )

    historical_module = worker_runtime._worker_universe._operation_debug_view(
        historical_pin
    ).modules[0]
    historical_line = resolve_source_line(
        historical_module, shared_ref, 2,
    ).generated_line
    assert historical_line is not None
    worker_runtime._operation_generation_pin = historical_pin
    session.live_frames = (
        session.live_frames[0],
        StackFrame(
            session.live_frames[0].target_id,
            1,
            historical_module.registration.module_location(historical_line),
        ),
        session.live_frames[2],
    )
    historical_frame = worker_runtime.current_capture().stack[0].with_method()

    assert [frame.source for frame in current_frames] == [
        "МодульА",
        "ЯчейкаНоутбука",
    ]
    assert current_frames[0]._resolved.identity != current_frames[1]._resolved.identity
    assert [frame.method.name for frame in current_frames] == ["Версия", "Версия"]
    assert [frame._resolved.version.generation for frame in current_frames] == [
        shared_pin.handle.generation,
        shared_pin.handle.generation,
    ]
    assert historical_frame.source == "МодульА"
    assert historical_frame._resolved.version.generation == historical_pin.handle.generation
    public = repr(current_page) + repr(historical_frame)
    assert "PrivateLaterCell" not in public
    assert "SourceUnitRef" not in public and "identity=" not in public


@pytest.mark.parametrize(
    "failure",
    (
        PermissionError(13, "denied", r"C:\\private\\export\\Common.xml"),
        FileNotFoundError(2, "missing", r"C:\\private\\export\\Common.xml"),
        ProtocolError("configuration module metadata is invalid"),
    ),
)
def test_public_stack_sanitizes_configuration_source_filesystem_failures(failure) -> None:
    runtime, _, _ = captured_stack_api()

    def fail_resolution(_frames):
        if isinstance(failure, ProtocolError):
            try:
                raise PermissionError(
                    13, "denied", r"C:\\private\\export\\Common.xml"
                )
            except PermissionError as error:
                raise failure from error
        raise failure

    runtime._capture_stack_source_resolver = fail_resolution

    page = runtime.current_capture().stack[:20]

    frame = next(frame for frame in page.frames if isinstance(frame, api().DebugFrame))
    assert frame.source_status == "unavailable"
    assert r"C:\private\export" not in repr(page)
