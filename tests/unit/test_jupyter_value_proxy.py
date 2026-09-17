from __future__ import annotations

from dataclasses import dataclass
import gc
import weakref

import pandas as pd
import pytest

from onec_runtime.errors import ProtocolError
from onec_runtime_jupyter.extension import (
    BSL_NAMESPACE_NAME,
    OnecValueProxy,
    install_runtime,
    synchronize_bsl_namespace,
)
from onec_runtime.prototype_runtime import OperationState
from onec_runtime.runtime_api import RuntimeNamespaceSnapshot, RuntimeStatus


class FakeShell:
    def __init__(self) -> None:
        self.user_ns: dict[str, object] = {}


@dataclass
class FakeRuntime:
    names: tuple[str, ...] = ("КадровыеДанныеТЗ",)
    runtime_generation: int = 3
    context_generation: int = 7

    def __post_init__(self) -> None:
        self.materializations: list[tuple[str, dict[str, object]]] = []
        self.value_materializations: list[tuple[str, dict[str, object]]] = []
        self.table_projections: list[
            tuple[str, dict[str, object], dict[str, object]]
        ] = []
        self.value_projections: list[
            tuple[str, dict[str, object], dict[str, object]]
        ] = []
        self.guard_calls: list[str] = []
        self.forbidden_handles: set[str] = set()

    def validate_value_reference(self, handle: str) -> None:
        self.guard_calls.append(handle)
        if handle in self.forbidden_handles:
            raise ProtocolError("Worker generation objects are not public values")

    def namespace_snapshot(self) -> RuntimeNamespaceSnapshot:
        return RuntimeNamespaceSnapshot(
            self.runtime_generation,
            self.context_generation,
            self.names,
        )

    def status(self) -> RuntimeStatus:
        return RuntimeStatus(OperationState.COMPLETED, self.runtime_generation, 1, None)

    def to_df(self, handle: str, **kwargs: object) -> pd.DataFrame:
        self.materializations.append((handle, kwargs))
        return pd.DataFrame({"value": [1]})

    def materialize_value(self, handle: str, **kwargs: object) -> object:
        self.value_materializations.append((handle, kwargs))
        return {"handle": handle}

    def project_to_df(
        self, handle: str, selection: dict[str, object], **kwargs: object
    ) -> pd.DataFrame:
        self.table_projections.append((handle, selection, kwargs))
        return pd.DataFrame({"value": [1, 2]})

    def project_value(
        self, handle: str, selection: dict[str, object], **kwargs: object
    ) -> object:
        self.value_projections.append((handle, selection, kwargs))
        return [5, 6]


def test_namespace_sync_validates_every_name_locally_without_caching() -> None:
    shell = FakeShell()
    runtime = FakeRuntime(names=("Первое", "Второе", "Третье", "Четвертое", "Пятое"))
    install_runtime(shell, runtime)
    expected = [f"Контекст.{name}" for name in runtime.names]
    assert runtime.guard_calls == expected
    proxies = {name: shell.user_ns[name] for name in runtime.names}

    synchronize_bsl_namespace(shell)
    assert runtime.guard_calls == [*expected, *expected]
    assert all(shell.user_ns[name] is proxy for name, proxy in proxies.items())


def test_failed_local_validation_keeps_entire_previous_namespace() -> None:
    shell = FakeShell()
    runtime = FakeRuntime(names=("СтароеИмя",))
    install_runtime(shell, runtime)
    old_proxy = shell.user_ns["СтароеИмя"]
    old_namespace = shell.user_ns[BSL_NAMESPACE_NAME]
    runtime.names = ("СтароеИмя", "НовоеИмя", "АлиасМодуля")
    runtime.forbidden_handles.add("Контекст.АлиасМодуля")

    with pytest.raises(ProtocolError, match="Worker generation objects are not public values"):
        synchronize_bsl_namespace(shell)

    assert runtime.guard_calls[-3:] == [
        "Контекст.СтароеИмя",
        "Контекст.НовоеИмя",
        "Контекст.АлиасМодуля",
    ]
    assert shell.user_ns["СтароеИмя"] is old_proxy
    assert shell.user_ns[BSL_NAMESPACE_NAME] is old_namespace
    assert "НовоеИмя" not in shell.user_ns
    assert "АлиасМодуля" not in shell.user_ns
    with pytest.raises(AttributeError):
        getattr(old_namespace, "НовоеИмя")


def test_sync_injects_lazy_proxy_and_delegates_to_symbolic_context_handle() -> None:
    shell = FakeShell()
    runtime = FakeRuntime()
    install_runtime(shell, runtime)

    synchronize_bsl_namespace(shell)

    proxy = shell.user_ns["КадровыеДанныеТЗ"]
    assert isinstance(proxy, OnecValueProxy)
    assert runtime.materializations == []

    frame = proxy.to_df(refs="both", chunk_size=2400)

    assert frame.to_dict("records") == [{"value": 1}]
    assert runtime.materializations == [
        (
            "Контекст.КадровыеДанныеТЗ",
            {
                "refs": "both",
                "ref_columns": None,
                "uuid_suffix": "__uuid",
                "chunk_size": 2400,
            },
        )
    ]


def test_jupyter_namespace_does_not_publish_runtime_rejected_worker_alias() -> None:
    shell = FakeShell()
    runtime = FakeRuntime(names=("АлиасМодуля",))
    runtime.forbidden_handles.add("Контекст.АлиасМодуля")
    with pytest.raises(
        ProtocolError,
        match="^Worker generation objects are not public values$",
    ):
        install_runtime(shell, runtime)

    assert runtime.guard_calls == ["Контекст.АлиасМодуля"]
    assert "АлиасМодуля" not in shell.user_ns


def test_jupyter_sync_validates_whole_snapshot_before_atomic_proxy_commit() -> None:
    shell = FakeShell()
    runtime = FakeRuntime(names=("СтароеИмя",))
    install_runtime(shell, runtime)
    old_proxy = shell.user_ns["СтароеИмя"]
    bsl = shell.user_ns[BSL_NAMESPACE_NAME]

    runtime.names = ("СтароеИмя", "НовоеИмя", "АлиасМодуля")
    runtime.forbidden_handles.add("Контекст.АлиасМодуля")

    with pytest.raises(
        ProtocolError,
        match="^Worker generation objects are not public values$",
    ):
        synchronize_bsl_namespace(shell)

    assert shell.user_ns["СтароеИмя"] is old_proxy
    assert "НовоеИмя" not in shell.user_ns
    assert "АлиасМодуля" not in shell.user_ns
    assert dir(bsl) == ["СтароеИмя"]
    assert runtime.guard_calls[-3:] == [
        "Контекст.СтароеИмя",
        "Контекст.НовоеИмя",
        "Контекст.АлиасМодуля",
    ]


def test_proxy_materializes_recursively_through_universal_runtime_api() -> None:
    shell = FakeShell()
    runtime = FakeRuntime()
    install_runtime(shell, runtime)
    proxy = shell.user_ns["КадровыеДанныеТЗ"]
    assert isinstance(proxy, OnecValueProxy)

    result = proxy.materialize(
        refs="both", max_depth=8, max_items=123, max_bytes=4096
    )

    assert result == {"handle": "Контекст.КадровыеДанныеТЗ"}
    assert runtime.value_materializations == [
        (
            "Контекст.КадровыеДанныеТЗ",
            {
                "refs": "both",
                "ref_columns": None,
                "uuid_suffix": "__uuid",
                "chunk_size": None,
                "max_depth": 8,
                "max_items": 123,
                "max_bytes": 4096,
            },
        )
    ]


def test_head_returns_lazy_projection_and_to_df_projects_before_transfer() -> None:
    shell = FakeShell()
    runtime = FakeRuntime()
    install_runtime(shell, runtime)
    proxy = shell.user_ns["КадровыеДанныеТЗ"]
    assert isinstance(proxy, OnecValueProxy)

    head = proxy.head(10)

    assert isinstance(head, OnecValueProxy)
    assert runtime.table_projections == []
    frame = head.to_df(refs="both", chunk_size=2400)
    assert frame.to_dict("records") == [{"value": 1}, {"value": 2}]
    assert runtime.table_projections == [
        (
            "Контекст.КадровыеДанныеТЗ",
            {"offset": 0, "limit": 10},
            {
                "refs": "both",
                "ref_columns": None,
                "uuid_suffix": "__uuid",
                "chunk_size": 2400,
            },
        )
    ]
    assert runtime.materializations == []


def test_slice_returns_lazy_projection_for_recursive_materialization() -> None:
    shell = FakeShell()
    runtime = FakeRuntime(names=("Массив",))
    install_runtime(shell, runtime)
    proxy = shell.user_ns["Массив"]
    assert isinstance(proxy, OnecValueProxy)

    selected = proxy[5:20]

    assert runtime.value_projections == []
    assert selected.materialize(max_items=100) == [5, 6]
    assert runtime.value_projections == [
        (
            "Контекст.Массив",
            {"offset": 5, "limit": 15},
            {
                "refs": "presentation",
                "ref_columns": None,
                "uuid_suffix": "__uuid",
                "chunk_size": None,
                "max_depth": 32,
                "max_items": 100,
                "max_bytes": 64 * 1024 * 1024,
            },
        )
    ]


@pytest.mark.parametrize(
    "selection",
    (0, slice(None), slice(-1, 2), slice(2, 2), slice(0, 10, 2)),
)
def test_proxy_slice_rejects_unbounded_negative_empty_or_stepped_selection(
    selection: object,
) -> None:
    shell = FakeShell()
    runtime = FakeRuntime()
    install_runtime(shell, runtime)
    proxy = shell.user_ns["КадровыеДанныеТЗ"]
    assert isinstance(proxy, OnecValueProxy)

    with pytest.raises(ProtocolError, match="bounded slice"):
        proxy[selection]  # type: ignore[index]

    assert runtime.table_projections == []
    assert runtime.value_projections == []


def test_projected_proxy_cannot_drop_selection_through_nested_path() -> None:
    shell = FakeShell()
    runtime = FakeRuntime(names=("ДокументОбъект",))
    install_runtime(shell, runtime)
    proxy = shell.user_ns["ДокументОбъект"]
    assert isinstance(proxy, OnecValueProxy)

    with pytest.raises(ProtocolError, match="bounded projection"):
        proxy.head(10).tabular_section("Товары")

    assert runtime.materializations == []
    assert runtime.value_materializations == []


def test_proxy_slice_rejects_unbounded_numeric_offset_before_bsl_generation() -> None:
    shell = FakeShell()
    runtime = FakeRuntime(names=("Массив",))
    install_runtime(shell, runtime)
    proxy = shell.user_ns["Массив"]
    assert isinstance(proxy, OnecValueProxy)
    huge = 10**5000

    with pytest.raises(ProtocolError, match="bounded slice"):
        proxy[huge : huge + 1]

    assert runtime.value_projections == []


def test_tabular_section_proxy_uses_safe_dotted_context_path() -> None:
    shell = FakeShell()
    runtime = FakeRuntime(names=("ДокументОбъект",))
    install_runtime(shell, runtime)
    proxy = shell.user_ns["ДокументОбъект"]
    assert isinstance(proxy, OnecValueProxy)

    section = proxy.tabular_section("Товары")
    frame = section.to_df(refs="uuid")

    assert frame.to_dict("records") == [{"value": 1}]
    assert runtime.materializations[0][0] == "Контекст.ДокументОбъект.Товары"
    assert "ДокументОбъект.Товары" in repr(section)


@pytest.mark.parametrize(
    "name",
    ("", "0Rows", "Rows[0]", "Rows.Method()", "Rows; Сообщить(1)", "Rows Name"),
)
def test_tabular_section_rejects_non_identifier_without_runtime_io(name: str) -> None:
    shell = FakeShell()
    runtime = FakeRuntime(names=("ДокументОбъект",))
    install_runtime(shell, runtime)
    proxy = shell.user_ns["ДокументОбъект"]
    assert isinstance(proxy, OnecValueProxy)

    with pytest.raises(ProtocolError, match="tabular section"):
        proxy.tabular_section(name)

    assert runtime.materializations == []
    assert runtime.value_materializations == []


def test_nested_proxy_validates_persistent_root_before_runtime_io() -> None:
    shell = FakeShell()
    runtime = FakeRuntime(names=("ДокументОбъект",))
    install_runtime(shell, runtime)
    proxy = shell.user_ns["ДокументОбъект"]
    assert isinstance(proxy, OnecValueProxy)
    section = proxy.tabular_section("Товары")
    runtime.names = ()

    with pytest.raises(ProtocolError, match="no longer persistent"):
        section.materialize()

    assert runtime.value_materializations == []


def test_sync_never_overwrites_python_value_and_bsl_namespace_remains_available() -> None:
    shell = FakeShell()
    runtime = FakeRuntime()
    python_value = object()
    shell.user_ns["КадровыеДанныеТЗ"] = python_value
    install_runtime(shell, runtime)

    synchronize_bsl_namespace(shell)

    assert shell.user_ns["КадровыеДанныеТЗ"] is python_value
    namespace = shell.user_ns[BSL_NAMESPACE_NAME]
    assert isinstance(namespace.КадровыеДанныеТЗ, OnecValueProxy)
    assert namespace["КадровыеДанныеТЗ"].name == "КадровыеДанныеТЗ"


def test_install_runtime_rejects_unrelated_reserved_bsl_namespace() -> None:
    shell = FakeShell()
    shell.user_ns[BSL_NAMESPACE_NAME] = object()

    with pytest.raises(ProtocolError, match="reserved Python name 'bsl'"):
        install_runtime(shell, FakeRuntime())


def test_reinstall_removes_only_old_runtime_proxies() -> None:
    shell = FakeShell()
    first = FakeRuntime(names=("СтароеИмя",))
    install_runtime(shell, first)
    user_value = object()
    shell.user_ns["ПользовательскоеИмя"] = user_value

    second = FakeRuntime(names=("НовоеИмя",), runtime_generation=4)
    install_runtime(shell, second)

    assert "СтароеИмя" not in shell.user_ns
    assert isinstance(shell.user_ns["НовоеИмя"], OnecValueProxy)
    assert shell.user_ns["ПользовательскоеИмя"] is user_value


def test_reinstall_rejects_saved_old_proxy_and_bsl_alias_even_with_equal_generations() -> None:
    shell = FakeShell()
    first = FakeRuntime(names=("Данные",), runtime_generation=3, context_generation=7)
    install_runtime(shell, first)
    old_proxy = shell.user_ns["Данные"]
    old_slice = old_proxy[:1]
    old_section = old_proxy.tabular_section("Строки")
    old_bsl = shell.user_ns[BSL_NAMESPACE_NAME]
    shell.user_ns["СтарыйПрокси"] = old_proxy
    shell.user_ns["СтарыйBSL"] = old_bsl

    second = FakeRuntime(names=("Данные",), runtime_generation=3, context_generation=7)
    install_runtime(shell, second)

    with pytest.raises(ProtocolError, match="stale"):
        old_proxy.to_df()
    with pytest.raises(ProtocolError, match="stale"):
        old_slice.materialize()
    with pytest.raises(ProtocolError, match="stale"):
        old_section.to_df()
    with pytest.raises(ProtocolError, match="stale"):
        getattr(old_bsl, "Данные")
    assert first.materializations == []
    assert shell.user_ns["СтарыйПрокси"] is old_proxy
    assert shell.user_ns["СтарыйBSL"] is old_bsl
    assert shell.user_ns["Данные"].to_df().to_dict("records") == [{"value": 1}]
    assert len(second.materializations) == 1


def test_detached_bridge_cannot_republish_old_runtime_proxies() -> None:
    shell = FakeShell()
    first = FakeRuntime(names=("Данные",), runtime_generation=3, context_generation=7)
    install_runtime(shell, first)
    old_bsl = shell.user_ns[BSL_NAMESPACE_NAME]

    second = FakeRuntime(names=("Данные",), runtime_generation=3, context_generation=7)
    install_runtime(shell, second)
    current_proxy = shell.user_ns["Данные"]

    with pytest.raises(ProtocolError, match="stale"):
        old_bsl._bridge.sync(shell.user_ns)
    assert shell.user_ns["Данные"] is current_proxy
    assert dir(old_bsl) == []


def test_proxy_fails_closed_after_generation_change_or_name_removal() -> None:
    shell = FakeShell()
    runtime = FakeRuntime()
    install_runtime(shell, runtime)
    synchronize_bsl_namespace(shell)
    proxy = shell.user_ns["КадровыеДанныеТЗ"]
    assert isinstance(proxy, OnecValueProxy)

    runtime.runtime_generation = 4
    with pytest.raises(ProtocolError, match="stale"):
        proxy.to_df()

    runtime.runtime_generation = 3
    runtime.names = ()
    with pytest.raises(ProtocolError, match="no longer persistent"):
        proxy.to_df()


def test_proxy_does_not_keep_runtime_alive_or_expose_value_in_repr() -> None:
    shell = FakeShell()
    runtime = FakeRuntime()
    install_runtime(shell, runtime)
    synchronize_bsl_namespace(shell)
    proxy = shell.user_ns["КадровыеДанныеТЗ"]
    assert isinstance(proxy, OnecValueProxy)
    runtime_ref = weakref.ref(runtime)

    del shell.user_ns["_onec_runtime"]
    del shell.user_ns[BSL_NAMESPACE_NAME]
    del runtime
    gc.collect()

    assert runtime_ref() is None
    assert "КадровыеДанныеТЗ" in repr(proxy)
    with pytest.raises(ProtocolError, match="no longer available"):
        proxy.to_df()
