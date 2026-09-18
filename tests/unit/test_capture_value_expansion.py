from dataclasses import FrozenInstanceError
import json
import traceback

import pytest

from onec_runtime.errors import (
    CaptureBusyError,
    CapturePathError,
    CaptureShapeUnsupportedError,
    CaptureValueAccessDeniedError,
    CaptureValueCheckError,
)
from onec_runtime.privacy import public_artifact_value


def api():
    from onec_runtime import capture_values
    return capture_values


class Handle:
    def __init__(self, identity):
        self.identity = identity

    def __repr__(self):
        return f"PRIVATE<{self.identity}>"


class Value:
    def __init__(self, type_name, preview, shape, children=(), *, size=None, identity=None):
        self.type_name = type_name
        self.preview = preview
        self.shape = shape
        self.children = list(children)
        self.size = len(self.children) if size is None and shape != "scalar" else size
        self.handle = Handle(identity if identity is not None else id(self))
        self.describe_calls = 0


class Backend:
    def __init__(self, roots):
        self.fence = object()
        self.valid_fence = self.fence
        self.roots = roots
        self.private = set()
        self.calls = []
        self.row_reads = 0
        self.field_reads = 0

    def validate_inspection(self, fence):
        self.calls.append(("validate", fence))
        if fence is not self.valid_fence:
            raise CaptureBusyError("pending", "inspection", "evaluating")

    def resolve_value(self, fence, path):
        self.calls.append(("resolve", fence, path))
        value = self._resolve(path)
        return self._entry(path.segments[-1].display_name, value)

    def project_values(self, fence, request):
        self.calls.append(("project", fence, request))
        if request.path.segments:
            parent = self._resolve(request.path)
            if request.view.value == "table_columns":
                values = list(parent.children[0][1].children) if parent.children else []
            else:
                values = list(parent.children)
        else:
            values = list(self.roots.items())
        if request.view.value == "table_rows":
            self.row_reads += 1
        if request.view.value == "row_fields":
            self.field_reads += 1
        entries = [
            self._entry(
                name, value,
                cycle=bool(request.path.segments) and value.handle is parent.handle,
            )
            for name, value in values
        ]
        if request.exact is not None:
            if isinstance(request.exact, str):
                entries = [entry for entry in entries
                           if str(entry.name).casefold() == request.exact.casefold()]
            else:
                entries = [entry for entry in entries if entry.name == request.exact]
        total = len(entries)
        selected = entries[request.start:request.stop]
        return api().PrivateValueProjection(tuple(selected), total,
                                            request.stop if request.stop < total else None)

    def discover_table_columns(self, fence, path, limit):
        self.calls.append(("schema", fence, path, limit))
        value = self._resolve(path)
        if value.shape == "value_table":
            fields = value.children[0][1].children if value.children else ()
        else:
            fields = value.children
        names = tuple(name for name, _ in fields)
        return names[:limit]

    def _resolve(self, path):
        current = None
        for index, segment in enumerate(path.segments):
            values = self.roots.items() if index == 0 else current.children
            if isinstance(segment.key, str):
                matches = [value for name, value in values
                           if str(name).casefold() == segment.key.casefold()]
            else:
                matches = [value for name, value in values if name == segment.key]
            if len(matches) != 1:
                raise AssertionError(f"bad fixture path {path}")
            current = matches[0]
        return current

    def _entry(self, name, value, *, cycle=False):
        def describe():
            value.describe_calls += 1
            return api().ValueMetadata(
                value.type_name, value.preview, value.size, api().ValueShape(value.shape),
            )
        return api().PrivateProjectedValue(
            name,
            describe,
            denied=value.handle.identity in self.private,
            cycle=cycle,
        )


def scalar(preview="1", *, identity=None):
    return Value("Число", preview, "scalar", identity=identity)


def fixture():
    structure = Value("Структура", "ignored", "structure", [
        ("Поле", scalar("42")), ("Другое", scalar("43")),
    ])
    fixed_structure = Value("ФиксированнаяСтруктура", "ignored", "fixed_structure", [
        ("Поле", scalar("fixed")),
    ])
    array = Value("Массив", "ignored", "array", [(0, scalar("a")), (1, scalar("b"))])
    fixed_array = Value("ФиксированныйМассив", "ignored", "fixed_array", [(0, scalar("fa"))])
    rows = []
    for index in range(3):
        rows.append((index, Value("СтрокаТаблицыЗначений", "ignored", "value_table_row", [
            ("Код", scalar(str(index))), ("Имя", Value("Строка", f"row-{index}", "scalar")),
        ])))
    table = Value("ТаблицаЗначений", "ignored", "value_table", rows)
    unsupported = Value("Соответствие", "ignored", "map", [("Ключ", scalar())])
    roots = {
        "Структура": structure, "ФиксСтруктура": fixed_structure,
        "Массив": array, "ФиксМассив": fixed_array,
        "Таблица": table, "Карта": unsupported,
    }
    return roots


def setup_values(*, roots=None, max_depth=8, max_items=100, max_bytes=64 * 1024):
    module = api()
    backend = Backend(fixture() if roots is None else roots)
    adapter = module.LocalCaptureValueAdapter(
        backend, backend.fence,
        policy=module.CaptureValuePolicy(
            max_depth=max_depth, max_items=max_items, max_bytes=max_bytes,
        ),
        resolve_parameters=lambda root: (),
    )
    return adapter, backend


def test_supported_shapes_have_universal_children_and_semantic_aliases():
    adapter, backend = setup_values()
    structure = adapter.context.variables["структура"]
    assert [item.name for item in structure.children[:20].items] == ["Поле", "Другое"]
    assert structure.fields["поле"].preview == "42"
    assert adapter.context.variables["ФиксСтруктура"].fields[:20].items[0].preview == "fixed"
    assert adapter.context.variables["Массив"].items[1].preview == "b"
    assert adapter.context.variables["ФиксМассив"].children[0].preview == "fa"
    table = adapter.context.variables["Таблица"]
    assert [item.name for item in table.columns[:20].items] == ["Код", "Имя"]
    row = table.rows[1]
    assert row.fields["имя"].preview == "row-1"
    assert row.children["Код"].preview == "1"
    assert backend.row_reads == 1


def test_every_child_slice_reprojects_the_saved_safe_path_and_membership_is_not_cached():
    adapter, backend = setup_values()
    structure = adapter.context.variables["Структура"]
    first = structure.fields[:20]
    backend.roots["Структура"].children[0] = ("Поле", scalar("changed"))
    second = structure.fields[:20]
    assert first.items[0].preview == "42"
    assert second.items[0].preview == "changed"
    assert sum(call[0] == "resolve" for call in backend.calls) == 2
    assert sum(call[0] == "project" for call in backend.calls) == 3


def test_paths_are_frozen_symbolic_segments_without_expressions_or_handles():
    adapter, backend = setup_values()
    node = adapter.context.variables["Структура"].fields["Поле"]
    assert [(part.kind.value, part.key) for part in node.path.segments] == [
        ("variable", "Структура"), ("field", "Поле"),
    ]
    with pytest.raises(FrozenInstanceError):
        node.path.segments = ()
    rendered = repr(node.path) + repr(node)
    assert "PRIVATE" not in rendered and "object at" not in rendered
    with pytest.raises(CapturePathError):
        adapter.context.variables["Структура"].fields["Поле); Удалить(); //"]
    assert all("Удалить" not in repr(call) for call in backend.calls)


@pytest.mark.parametrize(
    "key", [slice(None), slice(-1, 1), slice(0, 101), slice(0, 2, 2), -1, True],
)
def test_child_pages_are_finite_nonnegative_unit_step_and_at_most_100(key):
    adapter, backend = setup_values()
    node = adapter.context.variables["Структура"]
    prior = len(backend.calls)
    with pytest.raises((CapturePathError, TypeError)):
        node.fields[key]
    assert [call[0] for call in backend.calls[prior:]] == ["validate"]


def test_preview_and_summary_are_bounded_and_page_bytes_are_enforced():
    roots = {"Большое": Value("Строка", "я" * 1000, "scalar")}
    adapter, _ = setup_values(roots=roots)
    node = adapter.context.variables["Большое"]
    assert len(node.preview) == 512 and node.preview.endswith("…")
    assert not node.expandable

    adapter, _ = setup_values(roots=roots, max_bytes=20)
    with pytest.raises(CaptureValueCheckError, match="byte budget"):
        adapter.context.variables[:1]


@pytest.mark.parametrize("shape", ["map", "value_tree", "application_object", "undocumented"])
def test_unsupported_property_shapes_fail_without_projecting_children(shape):
    adapter, backend = setup_values(roots={
        "Значение": Value("НеподдерживаемыйТип", "ignored", shape, [("Поле", scalar())]),
    })
    node = adapter.context.variables["Значение"]
    prior = len(backend.calls)
    with pytest.raises(CaptureShapeUnsupportedError):
        node.children[:20]
    assert [call[0] for call in backend.calls[prior:]] == ["validate"]


def test_scalar_property_and_fabricated_aliases_are_not_claimed():
    adapter, backend = setup_values(roots={"Число": scalar()})
    node = adapter.context.variables["Число"]
    for descriptor in (node.children, node.fields, node.items, node.rows, node.columns):
        with pytest.raises(CaptureShapeUnsupportedError):
            descriptor[:1]
    assert sum(call[0] == "project" for call in backend.calls) == 1


def test_wide_table_is_rejected_from_schema_before_any_row_value_fetch():
    row = Value("СтрокаТаблицыЗначений", "ignored", "value_table_row",
                [(f"Поле{i}", scalar()) for i in range(101)])
    table = Value("ТаблицаЗначений", "ignored", "value_table", [(0, row)])
    adapter, backend = setup_values(roots={"Таблица": table})
    node = adapter.context.variables["Таблица"]
    with pytest.raises(CaptureShapeUnsupportedError, match="100 columns"):
        node.rows[:1]
    with pytest.raises(CaptureShapeUnsupportedError, match="100 columns"):
        node.columns[:1]
    assert backend.row_reads == 0
    assert [call[0] for call in backend.calls[-3:]] == ["validate", "resolve", "schema"]


def test_denied_page_entry_is_redacted_and_exact_access_is_denied_before_describe():
    roots = fixture()
    adapter, backend = setup_values(roots=roots)
    denied = roots["Структура"]
    backend.private.add(denied.handle.identity)
    page = adapter.context.variables[:20]
    node = next(item for item in page.items if item.name == "Структура")
    assert type(node).__name__ == "DeniedValueNode"


def test_denied_child_uses_the_exact_three_field_wire_model_without_a_guard_callback():
    roots = fixture()
    adapter, backend = setup_values(roots=roots)
    backend.private.add(roots["Структура"].handle.identity)

    node = next(
        item for item in adapter.context.variables[:20].items if item.name == "Структура"
    )

    assert type(node).__name__ == "DeniedValueNode"
    assert public_artifact_value(node) == {
        "name": "Структура",
        "access": "denied",
        "expandable": False,
    }
    assert "private_guard" not in api().CaptureValuePolicy.__dataclass_fields__
    with pytest.raises(CaptureValueAccessDeniedError):
        adapter.context.variables["структура"]
    with pytest.raises(CaptureValueAccessDeniedError):
        adapter.context.variables[0]


def test_unavailable_entry_keeps_siblings_visible_and_exact_lookup_can_be_retried():
    adapter, backend = setup_values(roots={
        "Недоступное": scalar("hidden"), "Доступное": scalar("42"),
    })
    original_project = backend.project_values
    def with_unavailable(fence, request):
        projection = original_project(fence, request)
        entries = tuple(
            api().PrivateProjectedValue(
                entry.name,
                lambda: (_ for _ in ()).throw(AssertionError("must not describe")),
                unavailable=True,
            ) if entry.name == "Недоступное" else entry
            for entry in projection.entries
        )
        return api().PrivateValueProjection(entries, projection.total, projection.next_cursor)
    backend.project_values = with_unavailable

    page = adapter.context.variables[:2]
    unavailable, available = page.items
    assert type(unavailable).__name__ == "UnavailableValueNode"
    assert unavailable.name == "Недоступное"
    assert unavailable.access == "unavailable"
    assert unavailable.expandable is False
    assert available.preview == "42"
    assert public_artifact_value(unavailable) == {
        "name": "Недоступное", "access": "unavailable", "expandable": False,
    }
    with pytest.raises(CaptureValueCheckError, match="unavailable"):
        adapter.context.variables["Недоступное"]
    backend.project_values = original_project
    assert adapter.context.variables["Недоступное"].preview == "hidden"


def test_saved_value_cannot_expand_when_fresh_resolve_is_unavailable():
    adapter, backend = setup_values(roots={"Значение": Value(
        "Структура", "ignored", "structure", [("Поле", scalar())],
    )})
    saved = adapter.context.variables["Значение"]
    backend.resolve_value = lambda fence, path: api().PrivateProjectedValue(
        "Значение", lambda: (_ for _ in ()).throw(AssertionError("must not describe")),
        unavailable=True,
    )

    with pytest.raises(CaptureValueCheckError, match="unavailable"):
        saved.fields[:1]


def test_unavailable_public_node_rejects_unsafe_name():
    with pytest.raises(CapturePathError):
        api().UnavailableValueNode("Значение);Опасно()")


def test_backend_admission_redacts_alias_and_nested_descendant_before_metadata():
    shared = scalar("secret", identity="worker-generation")
    roots = {
        "Прямое": shared,
        "Псевдоним": shared,
        "Контейнер": Value("Структура", "ignored", "structure", [("Вложенное", shared)]),
    }
    adapter, backend = setup_values(roots=roots)
    backend.private.add("worker-generation")
    page = adapter.context.variables[:20]
    assert [item.access for item in page.items[:2]] == ["denied", "denied"]
    nested = page.items[2].fields[:20].items[0]
    assert nested.access == "denied" and not nested.expandable
    assert shared.describe_calls == 0


def test_lifecycle_error_wins_before_backend_admission():
    adapter, backend = setup_values()
    node = adapter.context.variables["Структура"]
    calls = len(backend.calls)
    backend.valid_fence = object()
    with pytest.raises(CaptureBusyError):
        node.fields["Поле); Опасно(); //"]
    assert len(backend.calls) == calls + 1
    assert backend.calls[-1][0] == "validate"


def test_root_is_rechecked_before_child_projection_and_denial_stops_projection():
    adapter, backend = setup_values()
    node = adapter.context.variables["Структура"]
    backend.private.add(backend.roots["Структура"].handle.identity)
    prior_projects = sum(call[0] == "project" for call in backend.calls)
    with pytest.raises(CaptureValueAccessDeniedError):
        node.fields[:20]
    assert sum(call[0] == "project" for call in backend.calls) == prior_projects


def test_cycles_are_nonexpandable_and_depth_limit_rejects_next_request():
    cyclic = Value("Структура", "ignored", "structure", [], identity="cycle")
    cyclic.children.append(("Себя", cyclic))
    adapter, _ = setup_values(roots={"Циклическая": cyclic}, max_depth=2)
    root = adapter.context.variables["Циклическая"]
    child = root.fields["Себя"]
    assert child.cycle and not child.expandable
    with pytest.raises(CapturePathError, match="cycle"):
        child.children[:1]

    nested = Value("Структура", "ignored", "structure", [
        ("B", Value("Структура", "ignored", "structure", [("C", scalar())]))
    ])
    adapter, _ = setup_values(roots={"A": nested}, max_depth=1)
    at_limit = adapter.context.variables["A"].fields["B"]
    with pytest.raises(CapturePathError, match="depth"):
        at_limit.fields[:1]


def test_policy_item_budget_can_be_stricter_than_api_page_bound():
    adapter, backend = setup_values(max_items=1)
    with pytest.raises(CapturePathError, match="item budget"):
        adapter.context.variables[:2]
    assert [call[0] for call in backend.calls] == ["validate"]


def test_saved_value_page_rendering_does_no_backend_work_and_hides_private_handles():
    adapter, backend = setup_values()
    page = adapter.context.variables[:2]
    count = len(backend.calls)
    rendered = str(page) + repr(page) + "".join(repr(item) for item in page.items)
    assert len(backend.calls) == count
    assert "PRIVATE" not in rendered and "object at" not in rendered


def test_backend_cannot_publish_a_nonprogressing_or_out_of_range_cursor():
    adapter, backend = setup_values()
    backend.project_values = lambda fence, request: api().PrivateValueProjection((), 2, 0)
    with pytest.raises(CaptureValueCheckError, match="cursor"):
        adapter.context.variables[:1]


def test_backend_metadata_failure_is_bounded_after_admission():
    adapter, backend = setup_values()
    def broken():
        raise RuntimeError("PRIVATE METADATA DETAIL")
    entry = api().PrivateProjectedValue("Значение", broken)
    backend.project_values = lambda fence, request: api().PrivateValueProjection((entry,), 1, None)
    with pytest.raises(CaptureValueCheckError) as raised:
        adapter.context.variables[:1]
    rendered = "".join(traceback.format_exception(raised.value))
    assert "PRIVATE METADATA DETAIL" not in rendered
    assert "PRIVATE METADATA DETAIL" not in repr(raised.value)
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    assert backend.calls[-1][0] == "validate"


@pytest.mark.parametrize("letter", ["A", "Я"])
def test_byte_budget_counts_the_complete_recursive_public_page(letter):
    child_names = tuple(f"{letter * 180}{index}" for index in range(5))
    leaf = Value(
        "Структура", "ignored", "structure",
        [(name, scalar(str(index))) for index, name in enumerate(child_names)],
    )
    chain = []
    nested = leaf
    for index in range(5):
        name = f"{letter * 180}{index + 10}"
        chain.append(name)
        nested = Value("Структура", "ignored", "structure", [(name, nested)])
    chain.reverse()
    roots = {"Корень": nested}

    def final_page(limit):
        adapter, _ = setup_values(
            roots=roots, max_depth=8, max_items=100, max_bytes=limit,
        )
        node = adapter.context.variables["Корень"]
        for name in chain:
            node = node.fields[name]
        return node.fields[:5]

    page = final_page(10_000_000)
    public_bytes = len(json.dumps(
        public_artifact_value(page), ensure_ascii=False,
        separators=(",", ":"), sort_keys=True,
    ).encode("utf-8"))
    assert len(final_page(public_bytes).items) == 5
    with pytest.raises(CaptureValueCheckError, match="byte budget"):
        final_page(public_bytes - 1)


def test_direct_row_rejects_more_than_100_columns_before_field_projection():
    row = Value(
        "СтрокаТаблицыЗначений", "ignored", "value_table_row",
        [(f"Поле{i}", scalar()) for i in range(101)],
    )
    adapter, backend = setup_values(roots={"Строка": row})
    node = adapter.context.variables["Строка"]
    with pytest.raises(CaptureShapeUnsupportedError, match="100 columns"):
        node.fields[:100]
    assert backend.field_reads == 0
    assert backend.calls[-1][0] == "schema"


def test_table_derived_row_revalidates_unique_casefold_schema_before_fields():
    row = Value(
        "СтрокаТаблицыЗначений", "ignored", "value_table_row",
        [("Код", scalar("1")), ("Имя", scalar("one"))],
    )
    table = Value("ТаблицаЗначений", "ignored", "value_table", [(0, row)])
    adapter, backend = setup_values(roots={"Таблица": table})
    saved_row = adapter.context.variables["Таблица"].rows[0]
    row.children[1] = ("КОД", scalar("duplicate"))
    prior = backend.field_reads
    with pytest.raises(CaptureShapeUnsupportedError, match="unique"):
        saved_row.fields[:2]
    assert backend.field_reads == prior


def test_table_columns_must_match_discovered_schema_order_and_count():
    adapter, backend = setup_values()
    table = adapter.context.variables["Таблица"]
    original = backend.project_values
    def reverse_columns(fence, request):
        result = original(fence, request)
        if request.view is api().ValueViewKind.TABLE_COLUMNS:
            return api().PrivateValueProjection(
                tuple(reversed(result.entries)), result.total, result.next_cursor,
            )
        return result
    backend.project_values = reverse_columns
    with pytest.raises(CaptureValueCheckError, match="schema"):
        table.columns[:2]

    backend.project_values = original
    original_schema = backend.discover_table_columns
    backend.discover_table_columns = (
        lambda fence, path, limit:
        original_schema(fence, path, limit) + ("Лишняя",)
    )
    with pytest.raises(CaptureValueCheckError, match="schema"):
        table.columns[:2]


@pytest.mark.parametrize("start", [0, 2, 99])
def test_zero_width_child_slice_is_empty_terminal_without_resolve_or_projection(start):
    adapter, backend = setup_values()
    node = adapter.context.variables["Структура"]
    prior = len(backend.calls)
    page = node.fields[start:start]
    assert page.items == () and page.total == 0 and page.next_cursor is None
    assert [call[0] for call in backend.calls[prior:]] == ["validate"]
