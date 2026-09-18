from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
import pytest

from onec_runtime_mcp.agent.contracts import AgentOperationState, OperationDescriptor
from onec_runtime_mcp.agent.onec_values import OnecValueResolver, publish_onec_bindings
from onec_runtime_mcp.agent.proxies import (
    MeasurementCost,
    ProxyProvenance,
    ProxyRealm,
    ProxyRegistry,
    SizeAccuracy,
    StaleProxy,
)
from onec_runtime.errors import ProtocolError
from onec_runtime.runtime_models import RuntimeNamespaceSnapshot


@dataclass
class FakeValueBackend:
    runtime_id: str = "runtime-1"
    generation: int = 7
    context_generation: int = 12
    names: tuple[str, ...] = ("КадровыеДанныеТЗ", "Порог")
    closed: bool = False

    def __post_init__(self) -> None:
        self.evaluate_calls: list[tuple[str, dict[str, object]]] = []
        self.table_calls: list[tuple[str, dict[str, object]]] = []
        self.value: object = 42
        self.guard_calls: list[str] = []
        self.forbidden_handles: set[str] = set()

    def validate_value_reference(self, handle: str) -> None:
        self.guard_calls.append(handle)
        if handle in self.forbidden_handles:
            raise ProtocolError("Worker generation objects are not public values")

    @property
    def is_closed(self) -> bool:
        return self.closed

    def namespace_snapshot(self) -> RuntimeNamespaceSnapshot:
        if self.closed:
            raise ProtocolError("closed")
        return RuntimeNamespaceSnapshot(
            self.generation,
            self.context_generation,
            self.names,
        )

    def materialize_value(self, handle: str, **options: object) -> object:
        self.evaluate_calls.append((handle, options))
        return self.value

    def materialize_table(self, handle: str, **options: object) -> pd.DataFrame:
        self.table_calls.append((handle, options))
        return pd.DataFrame({"value": [1]})


def operation(
    operation_id: str = "op-1",
    *,
    state: AgentOperationState = AgentOperationState.COMPLETED,
) -> OperationDescriptor:
    return OperationDescriptor(
        operation_id=operation_id,
        state=state,
        runtime_id="runtime-1",
        runtime_generation=7,
        cell_id="cell-main",
        revision=3,
        source_sha256="a" * 64,
    )


def test_successful_main_publishes_symbolic_context_proxies_without_reading_values() -> None:
    backend = FakeValueBackend(names=("КадровыеДанныеТЗ", "Порог"))
    registry = ProxyRegistry()

    published = publish_onec_bindings(backend, registry, operation())

    assert [item.qualified_name for item in published] == [
        "bsl.КадровыеДанныеТЗ",
        "bsl.Порог",
    ]
    assert all(item.realm is ProxyRealm.ONEC for item in published)
    assert backend.evaluate_calls == []
    assert backend.table_calls == []


def test_mcp_does_not_register_runtime_rejected_worker_alias() -> None:
    backend = FakeValueBackend(names=("АлиасМодуля",))
    backend.forbidden_handles.add("e1cRuntimeКонтекст.АлиасМодуля")
    registry = ProxyRegistry()

    with pytest.raises(
        ProtocolError,
        match="^Worker generation objects are not public values$",
    ):
        publish_onec_bindings(backend, registry, operation())

    assert backend.guard_calls == ["e1cRuntimeКонтекст.АлиасМодуля"]


def test_original_bsl_spelling_is_kept_while_binding_identity_is_case_insensitive() -> None:
    backend = FakeValueBackend(names=("Порог",))
    registry = ProxyRegistry()
    first = publish_onec_bindings(backend, registry, operation("op-1"))[0]
    backend.names = ("ПОРОГ",)

    second = publish_onec_bindings(backend, registry, operation("op-2"))[0]

    assert first.qualified_name == "bsl.Порог"
    assert second.qualified_name == "bsl.ПОРОГ"
    assert second.version == first.version + 1


def test_failed_main_does_not_publish_namespace_delta() -> None:
    backend = FakeValueBackend(names=("НельзяПубликовать",))
    registry = ProxyRegistry()

    published = publish_onec_bindings(
        backend,
        registry,
        operation(state=AgentOperationState.FAILED),
    )

    assert published == ()
    assert backend.evaluate_calls == []


def test_context_proxy_rejects_changed_context_generation_before_value_read() -> None:
    backend = FakeValueBackend(names=("Порог",))
    registry = ProxyRegistry()
    proxy = publish_onec_bindings(backend, registry, operation())[0]
    resolver = OnecValueResolver(backend, registry)
    backend.context_generation += 1

    with pytest.raises(StaleProxy):
        resolver.preview(proxy, limits={"items": 5, "bytes": 1024})

    assert backend.evaluate_calls == []


def test_context_deletion_and_runtime_close_fail_before_value_read() -> None:
    backend = FakeValueBackend(names=("Порог",))
    registry = ProxyRegistry()
    proxy = publish_onec_bindings(backend, registry, operation())[0]
    resolver = OnecValueResolver(backend, registry)
    backend.names = ()

    with pytest.raises(StaleProxy, match="binding"):
        resolver.materialize(
            proxy,
            max_depth=3,
            max_items=10,
            max_bytes=1024,
        )

    backend.names = ("Порог",)
    backend.closed = True
    with pytest.raises(StaleProxy, match="closed"):
        resolver.preview(proxy, limits={"items": 5, "bytes": 1024})
    assert backend.evaluate_calls == []


def test_scalar_preview_is_bounded_and_size_does_not_hide_a_scan() -> None:
    backend = FakeValueBackend(names=("Порог",))
    registry = ProxyRegistry()
    proxy = publish_onec_bindings(backend, registry, operation())[0]
    resolver = OnecValueResolver(backend, registry)

    size = resolver.size(proxy)
    preview = resolver.preview(proxy, limits={"items": 5, "bytes": 1024})

    assert size.accuracy is SizeAccuracy.UNKNOWN
    assert size.cost is MeasurementCost.SCAN_REQUIRED
    assert preview.scalar == 42
    assert backend.evaluate_calls == [
        (
            "e1cRuntimeКонтекст.Порог",
            {
                "refs": "presentation",
                "max_depth": 2,
                "max_items": 5,
                "max_bytes": 1024,
            },
        )
    ]


def test_child_projection_remains_symbolic_and_uses_parent_provenance() -> None:
    backend = FakeValueBackend(names=("ДокументОбъект",))
    registry = ProxyRegistry()
    parent = publish_onec_bindings(backend, registry, operation())[0]
    resolver = OnecValueResolver(backend, registry)

    child = resolver.get(parent, "Товары")
    resolver.to_df(child, refs="both", chunk_size=2400)

    assert child.qualified_name == "bsl.ДокументОбъект.Товары"
    assert child.provenance.parent_proxy_ids == (parent.proxy_id,)
    assert backend.evaluate_calls == []
    assert backend.table_calls == [
        (
            "e1cRuntimeКонтекст.ДокументОбъект.Товары",
            {"refs": "both", "chunk_size": 2400},
        )
    ]


@pytest.mark.parametrize("name", ["", "Rows[0]", "Method()", "A; B", "A B"])
def test_child_projection_rejects_code_injection_without_backend_access(name: str) -> None:
    backend = FakeValueBackend(names=("ДокументОбъект",))
    registry = ProxyRegistry()
    parent = publish_onec_bindings(backend, registry, operation())[0]
    resolver = OnecValueResolver(backend, registry)

    with pytest.raises((TypeError, ValueError), match="identifier"):
        resolver.get(parent, name)

    assert backend.evaluate_calls == []
    assert backend.table_calls == []


def test_python_and_bsl_qualified_name_collision_remain_separate() -> None:
    backend = FakeValueBackend(names=("Value",))
    registry = ProxyRegistry()
    onec = publish_onec_bindings(backend, registry, operation())[0]
    python = registry.register_python(
        qualified_name="bsl.Value",
        type_name="int",
        python_generation=1,
        provenance=ProxyProvenance("cell-py", 1, "b" * 64, "op-py"),
    )

    assert registry.resolve(onec.proxy_id).realm is ProxyRealm.ONEC
    assert registry.resolve(python.proxy_id).realm is ProxyRealm.PYTHON
