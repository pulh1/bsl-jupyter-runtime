from __future__ import annotations

from dataclasses import dataclass
import json

import pytest

from onec_runtime_mcp.agent.contracts import FailureCategory, to_wire
from onec_runtime_mcp.agent.proxies import (
    MeasurementCost,
    ProxyDescriptor,
    ProxyProvenance,
    ProxyRealm,
    ProxyRegistry,
    SizeAccuracy,
    ValuePreview,
    ValueSize,
)
from onec_runtime_mcp.agent.value_service import ValueService


def provenance() -> ProxyProvenance:
    return ProxyProvenance("cell-main", 1, "a" * 64, "op-1")


def cheap_budget() -> dict[str, object]:
    return {
        "depth": 4,
        "items": 100,
        "rows": 1000,
        "bytes": 1_000_000,
        "timeout_s": 5.0,
    }


@dataclass
class FakeResolver:
    registry: ProxyRegistry
    size_value: ValueSize = ValueSize.unknown("scan_required")

    def __post_init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.materialize_calls: list[object] = []

    def describe(self, proxy: ProxyDescriptor) -> ProxyDescriptor:
        self.calls.append(("describe", proxy.proxy_id))
        return proxy

    def size(self, proxy: ProxyDescriptor) -> ValueSize:
        self.calls.append(("size", proxy.proxy_id))
        return self.size_value

    def preview(self, proxy: ProxyDescriptor, *, limits: object) -> ValuePreview:
        self.calls.append(("preview", limits))
        return ValuePreview(type_name=proxy.type_name, scalar=42)

    def get(self, proxy: ProxyDescriptor, selector: object) -> ProxyDescriptor:
        self.calls.append(("get", selector))
        return self.registry.register_python(
            qualified_name=f"{proxy.qualified_name}.child",
            type_name="int",
            python_generation=proxy.fence.python_generation or 1,
            provenance=ProxyProvenance(
                "cell-py",
                1,
                "b" * 64,
                "op-child",
                parent_proxy_ids=(proxy.proxy_id,),
            ),
        )

    def select(self, proxy: ProxyDescriptor, fields: object) -> ProxyDescriptor:
        self.calls.append(("select", fields))
        return self.get(proxy, "projection")

    def snapshot(self, proxy: ProxyDescriptor, *, budget: object) -> ProxyDescriptor:
        self.calls.append(("snapshot", budget))
        return self.get(proxy, "snapshot")

    def materialize(
        self, proxy: ProxyDescriptor, *, target: str, policy: object, budget: object
    ) -> ProxyDescriptor:
        self.materialize_calls.append((proxy.proxy_id, target, policy, budget))
        return self.get(proxy, "materialized")

    def to_df(
        self, proxy: ProxyDescriptor, *, columns: object, refs: str, budget: object
    ) -> ProxyDescriptor:
        self.calls.append(("to_df", (columns, refs, budget)))
        return self.get(proxy, "frame")

    def compare(
        self, left: ProxyDescriptor, right: ProxyDescriptor, *, policy: object, budget: object
    ) -> dict[str, object]:
        self.calls.append(("compare", (left.proxy_id, right.proxy_id, policy, budget)))
        return {"equal": True, "truncated": False}

    def release(self, proxy: ProxyDescriptor) -> bool:
        return self.registry.release(proxy.proxy_id)


def seeded_value_service(
    *, size: ValueSize | None = None
) -> tuple[ValueService, ProxyDescriptor, FakeResolver]:
    registry = ProxyRegistry()
    proxy = registry.register_python(
        qualified_name="python.value",
        type_name="int",
        python_generation=3,
        provenance=provenance(),
    )
    resolver = FakeResolver(registry, size or ValueSize.unknown("scan_required"))
    service = ValueService(registry, {ProxyRealm.PYTHON: resolver})
    return service, proxy, resolver


def test_size_reports_cost_without_hidden_scan() -> None:
    service, proxy, resolver = seeded_value_service(
        size=ValueSize.unknown("scan_required")
    )

    response = service.call(
        "value.size",
        {"proxy_id": proxy.proxy_id, "budget": cheap_budget()},
    )

    assert response.ok is True
    assert response.value.accuracy is SizeAccuracy.UNKNOWN
    assert response.value.cost is MeasurementCost.SCAN_REQUIRED
    assert resolver.materialize_calls == []


def test_materialization_budget_is_rejected_before_backend_access() -> None:
    service, proxy, resolver = seeded_value_service()

    response = service.call(
        "value.materialize",
        {"proxy_id": proxy.proxy_id, "budget": {"bytes": -1}},
    )

    assert response.ok is False
    assert response.failure.category is FailureCategory.INVALID_REQUEST
    assert resolver.calls == []
    assert resolver.materialize_calls == []


def test_explicit_methods_dispatch_by_realm_and_preserve_projection_provenance() -> None:
    service, proxy, resolver = seeded_value_service()

    described = service.call("value.describe", {"proxy_id": proxy.proxy_id})
    preview = service.call(
        "value.preview",
        {"proxy_id": proxy.proxy_id, "limits": {"items": 5, "bytes": 1024}},
    )
    child = service.call(
        "value.get",
        {"proxy_id": proxy.proxy_id, "selector": "field"},
    )
    projection = service.call(
        "value.select",
        {"proxy_id": proxy.proxy_id, "fields": ["a", "b"]},
    )

    assert described.value == proxy
    assert preview.value.scalar == 42
    assert child.value.provenance.parent_proxy_ids == (proxy.proxy_id,)
    assert projection.value.provenance.parent_proxy_ids == (proxy.proxy_id,)
    assert [call[0] for call in resolver.calls] == [
        "describe",
        "preview",
        "get",
        "select",
        "get",
    ]


def test_snapshot_materialize_to_df_and_compare_require_finite_budget() -> None:
    service, proxy, resolver = seeded_value_service()

    snapshot = service.call(
        "value.snapshot",
        {"proxy_id": proxy.proxy_id, "budget": cheap_budget()},
    )
    materialized = service.call(
        "value.materialize",
        {
            "proxy_id": proxy.proxy_id,
            "target": "python",
            "policy": {"refs": "both"},
            "budget": cheap_budget(),
        },
    )
    frame = service.call(
        "value.to_df",
        {
            "proxy_id": proxy.proxy_id,
            "columns": ["Employee"],
            "refs": "uuid",
            "budget": cheap_budget(),
        },
    )
    compared = service.call(
        "value.compare",
        {
            "left_proxy_id": proxy.proxy_id,
            "right_proxy_id": snapshot.value.proxy_id,
            "policy": {"mode": "exact"},
            "budget": cheap_budget(),
        },
    )

    assert materialized.value.realm is ProxyRealm.PYTHON
    assert frame.value.realm is ProxyRealm.PYTHON
    assert compared.value == {"equal": True, "truncated": False}
    assert len(resolver.materialize_calls) == 1


def test_stale_proxy_and_missing_realm_resolver_fail_closed() -> None:
    service, proxy, resolver = seeded_value_service()
    service.registry.invalidate_python_generation(3)

    stale = service.call("value.describe", {"proxy_id": proxy.proxy_id})

    assert stale.ok is False
    assert stale.failure.category is FailureCategory.STALE
    assert resolver.calls == []

    registry = ProxyRegistry()
    onec = registry.register_context(
        qualified_name="bsl.Value",
        type_name="Число",
        runtime_id="runtime-1",
        runtime_generation=1,
        context_generation=1,
        provenance=provenance(),
    )
    unavailable = ValueService(registry, {}).call(
        "value.describe", {"proxy_id": onec.proxy_id}
    )
    assert unavailable.failure.category is FailureCategory.UNSUPPORTED


def test_release_is_idempotent_and_never_deletes_named_binding() -> None:
    service, proxy, _ = seeded_value_service()

    first = service.call("value.release", {"proxy_id": proxy.proxy_id})
    second = service.call("value.release", {"proxy_id": proxy.proxy_id})

    assert first.value == {"released": True, "binding_deleted": False}
    assert second.value == {"released": False, "binding_deleted": False}


def test_unknown_or_raw_resolver_results_never_reach_wire_or_journal() -> None:
    service, proxy, resolver = seeded_value_service()
    secret = "Пароль=super-secret"

    def raw_result(*_args: object, **_kwargs: object) -> object:
        return {"raw": secret}

    resolver.materialize = raw_result  # type: ignore[method-assign]
    response = service.call(
        "value.materialize",
        {"proxy_id": proxy.proxy_id, "budget": cheap_budget()},
    )

    assert response.ok is False
    encoded = json.dumps(to_wire(response), ensure_ascii=False)
    assert secret not in encoded


@pytest.mark.parametrize(
    "method",
    [
        "value.describe",
        "value.size",
        "value.preview",
        "value.get",
        "value.select",
        "value.snapshot",
        "value.materialize",
        "value.to_df",
        "value.compare",
        "value.release",
    ],
)
def test_value_methods_reject_unknown_arguments_before_resolver(
    method: str,
) -> None:
    service, proxy, resolver = seeded_value_service()
    args: dict[str, object] = {"proxy_id": proxy.proxy_id, "unexpected": True}
    if method == "value.compare":
        args = {
            "left_proxy_id": proxy.proxy_id,
            "right_proxy_id": proxy.proxy_id,
            "unexpected": True,
        }

    response = service.call(method, args)

    assert response.failure.category is FailureCategory.INVALID_REQUEST
    assert resolver.calls == []
    assert resolver.materialize_calls == []


def test_value_export_is_truthfully_unsupported() -> None:
    service, proxy, _ = seeded_value_service()

    response = service.call("value.export", {"proxy_id": proxy.proxy_id})

    assert response.failure.category is FailureCategory.UNSUPPORTED
