from __future__ import annotations

from dataclasses import dataclass

import pytest

from onec_runtime_mcp.agent.proxies import (
    ProxyDescriptor,
    ProxyProvenance,
    ProxyRealm,
    ProxyRegistry,
    ValuePreview,
    ValueSize,
)
from onec_runtime_mcp.agent.value_policy import (
    ValueBudgetProfiles,
    ValueCostClass,
)
from onec_runtime_mcp.agent.value_service import ValueService


@dataclass
class RecordingResolver:
    calls: list[object]

    def describe(self, proxy: ProxyDescriptor) -> ProxyDescriptor:
        self.calls.append("describe")
        return proxy

    def size(self, proxy: ProxyDescriptor) -> ValueSize:
        self.calls.append("size")
        return ValueSize(items=100)

    def preview(self, proxy: ProxyDescriptor, *, limits: object) -> ValuePreview:
        self.calls.append(("preview", limits))
        return ValuePreview(proxy.type_name, sample=(1, 2), truncated=True)


def _service(*, known: bool = False) -> tuple[ValueService, ProxyDescriptor, RecordingResolver]:
    registry = ProxyRegistry()
    proxy = registry.register_python(
        qualified_name="python.value",
        type_name="list",
        python_generation=1,
        provenance=ProxyProvenance("cell", 1, "a" * 64, "op"),
        known_size=ValueSize(items=2) if known else None,
        bounded_preview=ValuePreview("list", sample=(1, 2)) if known else None,
    )
    resolver = RecordingResolver([])
    return ValueService(registry, {ProxyRealm.PYTHON: resolver}), proxy, resolver


def test_auto_inspect_never_calls_resolver_for_unknown_metadata() -> None:
    service, proxy, resolver = _service()

    inspection = service.inspect(
        proxy.proxy_id,
        detail="auto",
        budget_profile="agent_metadata",
    )

    assert resolver.calls == []
    assert inspection.descriptor.proxy_id == proxy.proxy_id
    assert inspection.known_size is None
    assert {action.name: action.cost for action in inspection.actions} == {
        "size": ValueCostClass.BOUNDED_SCAN,
        "preview": ValueCostClass.BOUNDED_SCAN,
        "materialize": ValueCostClass.FULL_SCAN,
        "to_df": ValueCostClass.FULL_SCAN,
    }


def test_auto_returns_existing_size_and_preview_without_backend_access() -> None:
    service, proxy, resolver = _service(known=True)
    inspection = service.inspect(
        proxy.proxy_id, detail="auto", budget_profile="agent_metadata"
    )
    assert inspection.known_size.items == 2
    assert inspection.bounded_preview.sample == (1, 2)
    assert resolver.calls == []


def test_explicit_preview_uses_server_profile_and_rejects_escalation() -> None:
    service, proxy, resolver = _service()
    with pytest.raises(ValueError):
        service.inspect(
            proxy.proxy_id,
            detail="preview",
            budget_profile="agent_metadata",
        )
    assert resolver.calls == []

    inspection = service.inspect(
        proxy.proxy_id,
        detail="preview",
        budget_profile="agent_preview",
    )
    assert inspection.bounded_preview.sample == (1, 2)
    assert resolver.calls and resolver.calls[0][0] == "preview"


def test_budget_profiles_are_finite_server_owned_and_unknown_is_rejected() -> None:
    profiles = ValueBudgetProfiles()
    assert profiles.resolve("agent_metadata").max_rows < profiles.resolve(
        "agent_dataframe"
    ).max_rows
    assert profiles.resolve("agent_preview").timeout_seconds > 0
    with pytest.raises(ValueError):
        profiles.resolve("unlimited")
