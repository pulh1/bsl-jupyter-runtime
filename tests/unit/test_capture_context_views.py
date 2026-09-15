from dataclasses import FrozenInstanceError, replace
import traceback

import pytest

from onec_runtime.errors import (
    CaptureBusyError,
    CaptureLookupError,
    CapturePathError,
    CaptureSourceUnavailableError,
    CaptureValueCheckError,
)


def api():
    from onec_runtime import capture_values
    return capture_values


class Backend:
    def __init__(self):
        self.fence = object()
        self.valid_fence = self.fence
        self.context = [
            ("Локальная", "local-old"),
            ("ВторойПараметр", "second"),
            ("ПервыйПараметр", "first"),
        ]
        self.frame = list(self.context)
        self.calls = []
        self.private = set()

    def validate_inspection(self, fence):
        self.calls.append(("validate", fence))
        if fence is not self.valid_fence:
            raise CaptureBusyError("pending", "inspection", "evaluating")

    def resolve_value(self, fence, path):
        self.calls.append(("resolve", fence, path))
        raise AssertionError("scope inventory has no value root")

    def project_values(self, fence, request):
        self.calls.append(("project", fence, request))
        assert fence is self.valid_fence
        values = self.context if request.path.root.kind.value == "context" else self.frame
        entries = [self._entry(name, preview) for name, preview in values]
        if request.role.value == "parameters":
            by_name = {entry.name.casefold(): entry for entry in entries}
            entries = [by_name[name.casefold()] for name in request.parameter_names
                       if name.casefold() in by_name]
        elif request.role.value == "locals":
            parameters = {name.casefold() for name in request.parameter_names}
            entries = [entry for entry in entries if entry.name.casefold() not in parameters]
        if request.exact is not None:
            entries = [entry for entry in entries
                       if entry.name.casefold() == request.exact.casefold()]
        total = len(entries)
        entries = entries[request.start:request.stop]
        return api().PrivateValueProjection(tuple(entries), total,
                                            request.stop if request.stop < total else None)

    @staticmethod
    def _entry(name, preview):
        return api().PrivateProjectedValue(
            name,
            lambda: api().ValueMetadata("Строка", preview, None, api().ValueShape.SCALAR),
        )


def setup_adapter(*, parameters=("ПервыйПараметр", "ВторойПараметр"), resolver=None):
    module = api()
    backend = Backend()
    if resolver is None:
        resolver = lambda root: parameters
    adapter = module.LocalCaptureValueAdapter(
        backend, backend.fence,
        policy=module.CaptureValuePolicy(),
        resolve_parameters=resolver,
    )
    return adapter, backend


def test_context_pages_are_fresh_snapshots_and_frame_is_a_distinct_live_root():
    adapter, backend = setup_adapter()
    old_context = adapter.context.locals[:20]
    old_frame = adapter.frame(0).locals[:20]
    backend.context[0] = ("Локальная", "local-new")

    new_context = adapter.context.locals[:20]
    new_frame = adapter.frame(0).locals[:20]

    assert old_context.items[0].preview == "local-old"
    assert new_context.items[0].preview == "local-new"
    assert old_frame.items[0].preview == new_frame.items[0].preview == "local-old"
    assert old_context is not new_context
    with pytest.raises(FrozenInstanceError):
        old_context.items = ()
    project_calls = [call for call in backend.calls if call[0] == "project"]
    assert [call[2].path.root.kind.value for call in project_calls] == [
        "context", "frame", "context", "frame",
    ]


def test_variables_need_no_source_and_parameters_keep_source_order_without_duplication():
    resolutions = []
    def resolve(root):
        resolutions.append(root)
        return ("ПервыйПараметр", "ВторойПараметр")
    adapter, _ = setup_adapter(resolver=resolve)

    variables = adapter.context.variables[:20]
    assert [item.name for item in variables.items] == [
        "Локальная", "ВторойПараметр", "ПервыйПараметр",
    ]
    assert resolutions == []
    assert [item.name for item in adapter.context.parameters[:20].items] == [
        "ПервыйПараметр", "ВторойПараметр",
    ]
    assert [item.name for item in adapter.context.locals[:20].items] == ["Локальная"]
    assert 1 <= len(resolutions) <= 2


def test_exact_lookup_is_case_insensitive_and_missing_or_ambiguous_is_typed():
    adapter, backend = setup_adapter()
    assert adapter.context.locals["локальная"].name == "Локальная"
    with pytest.raises(CaptureLookupError, match="not found"):
        adapter.context.locals["НетТакой"]
    backend.context.append(("ЛОКАЛЬНАЯ", "duplicate"))
    with pytest.raises(CaptureLookupError, match="ambiguous"):
        adapter.context.locals["локальная"]


def test_parameters_and_locals_direct_to_variables_when_source_is_unavailable():
    def unavailable(root):
        raise CaptureSourceUnavailableError("PRIVATE C:/customer/source/Module.bsl")
    adapter, backend = setup_adapter(resolver=unavailable)
    assert adapter.context.variables[:1].items
    with pytest.raises(CaptureSourceUnavailableError, match="variables") as parameters_error:
        adapter.context.parameters[:1]
    with pytest.raises(CaptureSourceUnavailableError, match="variables") as locals_error:
        adapter.frame(7).locals[:1]
    for raised in (parameters_error.value, locals_error.value):
        rendered = "".join(traceback.format_exception(raised))
        assert "PRIVATE" not in rendered and "customer" not in rendered
        assert "PRIVATE" not in repr(raised)
        assert raised.__cause__ is None and raised.__context__ is None
    assert sum(call[0] == "project" for call in backend.calls) == 1


def test_unexpected_parameter_resolver_failure_is_sanitized_without_a_chain():
    def broken(root):
        raise RuntimeError("PRIVATE_SOURCE_PATH_C:/customer/x.bsl")
    adapter, backend = setup_adapter(resolver=broken)

    with pytest.raises(CaptureSourceUnavailableError, match="variables") as raised:
        adapter.context.parameters[:1]

    rendered = "".join(traceback.format_exception(raised.value))
    assert "PRIVATE_SOURCE_PATH" not in rendered and "customer" not in rendered
    assert "PRIVATE_SOURCE_PATH" not in repr(raised.value)
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    assert backend.calls == [("validate", backend.fence)]


@pytest.mark.parametrize(
    "key",
    [slice(None), slice(-1, 1), slice(0, 101), slice(0, 2, 2), -1, True],
)
def test_variable_views_reject_unbounded_or_invalid_pages_after_lifecycle_check(key):
    adapter, backend = setup_adapter()
    with pytest.raises((CapturePathError, TypeError)):
        adapter.context.variables[key]
    assert backend.calls == [("validate", backend.fence)]


def test_lifecycle_error_wins_over_unsafe_lookup_and_source_resolution():
    resolved = []
    adapter, backend = setup_adapter(resolver=lambda root: resolved.append(root) or ())
    backend.valid_fence = object()
    with pytest.raises(CaptureBusyError):
        adapter.context.locals["X); ВыполнитьОпасное(); //"]
    assert resolved == []
    assert backend.calls == [("validate", backend.fence)]


@pytest.mark.parametrize("unsafe", ["X); ВыполнитьОпасное(); //", "Для", "变量"])
def test_safe_lookup_rejects_non_bsl_identifier_without_projection(unsafe):
    adapter, backend = setup_adapter()
    with pytest.raises(CapturePathError, match="identifier"):
        adapter.context.variables[unsafe]
    assert [call[0] for call in backend.calls] == ["validate"]


def test_pages_and_descriptors_render_only_saved_bounded_data():
    adapter, backend = setup_adapter()
    page = adapter.context.variables[:2]
    calls = len(backend.calls)
    rendered = repr(page) + str(page) + repr(page.items[0]) + repr(adapter.context)
    assert "КонтекстОтладки" in rendered and "Локальная" in rendered
    assert len(backend.calls) == calls
    assert "local-old" in rendered
    assert "object at" not in rendered


def test_parameter_names_are_valid_unique_bsl_identifiers():
    adapter, backend = setup_adapter(parameters=("Арг", "АРГ"))
    with pytest.raises(CaptureSourceUnavailableError, match="classification"):
        adapter.context.parameters[:20]
    assert [call[0] for call in backend.calls] == ["validate"]


def test_adapter_filters_and_orders_roles_when_backend_returns_plain_debugger_order():
    adapter, backend = setup_adapter()
    original = backend.project_values
    def ignore_role(fence, request):
        return original(
            fence,
            replace(
                request,
                role=api().VariableRole.VARIABLES,
                parameter_names=(),
            ),
        )
    backend.project_values = ignore_role

    parameters = adapter.context.parameters[:20]
    locals_page = adapter.context.locals[:20]

    assert [item.name for item in parameters.items] == [
        "ПервыйПараметр", "ВторойПараметр",
    ]
    assert [item.name for item in locals_page.items] == ["Локальная"]
    assert parameters.total == 2 and locals_page.total == 1


@pytest.mark.parametrize("page_slice", [slice(0, 1), slice(1, 2), slice(1, 20)])
def test_partial_parameter_page_rejects_role_ignoring_backend(page_slice):
    adapter, backend = setup_adapter()
    original = backend.project_values
    def ignore_role(fence, request):
        return original(
            fence,
            replace(
                request,
                role=api().VariableRole.VARIABLES,
                parameter_names=(),
            ),
        )
    backend.project_values = ignore_role

    with pytest.raises(CaptureValueCheckError, match="parameter page"):
        adapter.context.parameters[page_slice]


@pytest.mark.parametrize(
    ("page_slice", "expected", "total", "next_cursor"),
    [
        (slice(0, 1), ["ПервыйПараметр"], 2, 1),
        (slice(1, 2), ["ВторойПараметр"], 2, None),
        (slice(1, 20), ["ВторойПараметр"], 2, None),
        (slice(20, 21), [], 2, None),
    ],
)
def test_parameter_pages_have_absolute_source_order_and_inventory_cursors(
    page_slice, expected, total, next_cursor,
):
    adapter, _ = setup_adapter()

    page = adapter.context.parameters[page_slice]

    assert [item.name for item in page.items] == expected
    assert page.total == total and page.next_cursor == next_cursor


@pytest.mark.parametrize("start", [0, 3, 99])
def test_zero_width_variable_slice_is_empty_terminal_without_source_or_projection(start):
    resolutions = []
    adapter, backend = setup_adapter(
        resolver=lambda root: resolutions.append(root) or ("ПервыйПараметр",),
    )
    page = adapter.context.parameters[start:start]
    assert page.items == () and page.total == 0 and page.next_cursor is None
    assert resolutions == []
    assert backend.calls == [("validate", backend.fence)]


def test_value_adapter_can_bind_native_frame_scope_without_changing_stack_coordinates():
    from onec_runtime.capture_inspection import DebugFrame

    adapter, _ = setup_adapter()
    original = DebugFrame(native_level=7, source="Common.Module", line=12)
    frame = adapter.bind_frame(original)
    assert frame.native_level == 7 and frame.source == "Common.Module"
    assert frame.variables[:1].items[0].name == "Локальная"
    assert frame.parameters[:1].items[0].name == "ПервыйПараметр"
    assert frame.locals[:1].items[0].name == "Локальная"
    with pytest.raises(CaptureSourceUnavailableError, match="not attached"):
        original.variables[:1]
