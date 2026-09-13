from __future__ import annotations

from dataclasses import asdict
import json
import math
from uuid import UUID

import pytest

from onec_runtime_mcp.agent.contracts import to_wire
from onec_runtime_mcp.agent.proxies import (
    MeasurementCost,
    ProxyConsistency,
    ProxyLifetime,
    ProxyProvenance,
    ProxyRealm,
    ProxyRegistry,
    ReleasedProxy,
    SizeAccuracy,
    StaleProxy,
    ValueBudget,
    ValuePreview,
    ValueSize,
)


def provenance(*, parent_proxy_ids: tuple[str, ...] = ()) -> ProxyProvenance:
    return ProxyProvenance(
        "cell-main",
        3,
        "a" * 64,
        "op-9",
        parent_proxy_ids=parent_proxy_ids,
    )


def seeded_registry() -> tuple[ProxyRegistry, object, object]:
    registry = ProxyRegistry()
    onec = registry.register_context(
        qualified_name="bsl.КадровыеДанныеТЗ",
        type_name="ТаблицаЗначений",
        runtime_id="runtime-1",
        runtime_generation=7,
        context_generation=12,
        provenance=provenance(),
    )
    python = registry.register_python(
        qualified_name="python.kadry",
        type_name="pandas.DataFrame",
        python_generation=4,
        provenance=provenance(parent_proxy_ids=(onec.proxy_id,)),
    )
    return registry, onec, python


def test_context_proxy_is_fenced_and_contains_no_raw_value() -> None:
    registry = ProxyRegistry()
    secret_handle = object()
    proxy = registry.register_context(
        qualified_name="bsl.КадровыеДанныеТЗ",
        type_name="ТаблицаЗначений",
        runtime_id="runtime-1",
        runtime_generation=7,
        context_generation=12,
        provenance=provenance(),
        resolver_handle=secret_handle,
    )

    wire = to_wire(proxy)

    assert wire["realm"] == "onec"
    assert wire["lifetime"] == "context"
    assert wire["fence"] == {
        "runtime_id": "runtime-1",
        "runtime_generation": 7,
        "context_generation": 12,
        "python_generation": None,
    }
    assert "value" not in wire and "repr" not in wire
    assert "object at" not in repr(proxy)
    assert "object at" not in repr(asdict(proxy))
    assert registry.resolver_handle(proxy.proxy_id) is secret_handle
    json.dumps(wire)


def test_proxy_ids_are_opaque_uuids_and_names_can_repeat_across_realms() -> None:
    registry, onec, python = seeded_registry()
    same_name_python = registry.register_python(
        qualified_name=onec.qualified_name,
        type_name="str",
        python_generation=4,
        provenance=provenance(),
    )

    UUID(onec.proxy_id)
    UUID(python.proxy_id)
    UUID(same_name_python.proxy_id)
    assert len({onec.proxy_id, python.proxy_id, same_name_python.proxy_id}) == 3
    assert registry.resolve(onec.proxy_id).realm is ProxyRealm.ONEC
    assert registry.resolve(same_name_python.proxy_id).realm is ProxyRealm.PYTHON


def test_runtime_restart_makes_context_proxy_stale_but_keeps_python_proxy() -> None:
    registry, onec, python = seeded_registry()

    registry.invalidate_runtime_generation("runtime-1", 7)

    with pytest.raises(StaleProxy):
        registry.resolve(onec.proxy_id)
    assert registry.resolve(python.proxy_id) == python


def test_python_restart_invalidates_only_matching_workspace_generation() -> None:
    registry, onec, python = seeded_registry()

    registry.invalidate_python_generation(4)

    with pytest.raises(StaleProxy):
        registry.resolve(python.proxy_id)
    assert registry.resolve(onec.proxy_id) == onec


def test_exact_and_latest_binding_versions_are_distinct() -> None:
    registry = ProxyRegistry()
    exact = registry.register_context(
        qualified_name="bsl.Value",
        type_name="Число",
        runtime_id="runtime-1",
        runtime_generation=7,
        context_generation=12,
        provenance=provenance(),
        consistency=ProxyConsistency.EXACT,
    )
    latest = registry.register_context(
        qualified_name="bsl.Latest",
        type_name="Число",
        runtime_id="runtime-1",
        runtime_generation=7,
        context_generation=12,
        provenance=provenance(),
        consistency=ProxyConsistency.LATEST,
    )

    exact_v2 = registry.register_context(
        qualified_name="BSL.VALUE",
        type_name="Число",
        runtime_id="runtime-1",
        runtime_generation=7,
        context_generation=12,
        provenance=provenance(),
        consistency=ProxyConsistency.EXACT,
    )
    latest_v2 = registry.register_context(
        qualified_name="BSL.LATEST",
        type_name="Число",
        runtime_id="runtime-1",
        runtime_generation=7,
        context_generation=12,
        provenance=provenance(),
        consistency=ProxyConsistency.LATEST,
    )

    assert exact_v2.version == exact.version + 1
    assert latest_v2.version == latest.version + 1
    with pytest.raises(StaleProxy):
        registry.resolve(exact.proxy_id)
    assert registry.resolve(latest.proxy_id) == latest_v2


def test_exact_snapshot_does_not_mutate_latest_binding_and_stales_on_rebind() -> None:
    registry = ProxyRegistry()
    latest = registry.register_context(
        qualified_name="bsl.Value",
        type_name="Число",
        runtime_id="runtime-1",
        runtime_generation=7,
        context_generation=12,
        provenance=provenance(),
    )

    exact = registry.snapshot_exact(latest.proxy_id)

    assert exact.proxy_id != latest.proxy_id
    assert exact.consistency is ProxyConsistency.EXACT
    assert registry.resolve(latest.proxy_id) == latest
    assert registry.resolve(exact.proxy_id) == exact
    replacement = registry.register_context(
        qualified_name="BSL.VALUE",
        type_name="Число",
        runtime_id="runtime-1",
        runtime_generation=7,
        context_generation=12,
        provenance=provenance(),
    )
    assert registry.resolve(latest.proxy_id) == replacement
    with pytest.raises(StaleProxy):
        registry.resolve(exact.proxy_id)


def test_releasing_exact_snapshot_keeps_latest_binding_unchanged() -> None:
    registry, latest, _ = seeded_registry()
    exact = registry.snapshot_exact(latest.proxy_id)

    assert registry.is_exact_snapshot(exact.proxy_id)
    assert registry.release(exact.proxy_id)
    assert registry.resolve_name(latest.qualified_name) == latest
    assert registry.history(latest.qualified_name) == (latest,)


def test_latest_never_rebinds_across_runtime_fence() -> None:
    registry = ProxyRegistry()
    old = registry.register_context(
        qualified_name="bsl.Value",
        type_name="Число",
        runtime_id="runtime-1",
        runtime_generation=7,
        context_generation=12,
        provenance=provenance(),
    )
    registry.invalidate_runtime_generation("runtime-1", 7)
    registry.register_context(
        qualified_name="bsl.Value",
        type_name="Число",
        runtime_id="runtime-1",
        runtime_generation=8,
        context_generation=1,
        provenance=provenance(),
    )

    with pytest.raises(StaleProxy):
        registry.resolve(old.proxy_id)


def test_release_is_idempotent_and_does_not_delete_named_binding_version() -> None:
    registry, onec, _ = seeded_registry()

    assert registry.release(onec.proxy_id) is True
    assert registry.release(onec.proxy_id) is False
    with pytest.raises(ReleasedProxy):
        registry.resolve(onec.proxy_id)

    replacement = registry.register_context(
        qualified_name="BSL.КадровыеДанныеТЗ",
        type_name="ТаблицаЗначений",
        runtime_id="runtime-1",
        runtime_generation=7,
        context_generation=12,
        provenance=provenance(),
    )
    assert replacement.version == onec.version + 1
    assert registry.resolve(replacement.proxy_id) == replacement


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_depth", 0),
        ("max_items", -1),
        ("max_rows", 0),
        ("max_bytes", -1),
        ("timeout_seconds", 0.0),
        ("timeout_seconds", math.inf),
        ("timeout_seconds", math.nan),
    ],
)
def test_value_budget_requires_finite_positive_bounds(field: str, value: object) -> None:
    values: dict[str, object] = {
        "max_depth": 8,
        "max_items": 1_000,
        "max_rows": 10_000,
        "max_bytes": 1_000_000,
        "timeout_seconds": 30.0,
    }
    values[field] = value

    with pytest.raises((TypeError, ValueError), match="positive|finite"):
        ValueBudget(**values)  # type: ignore[arg-type]


def test_size_and_preview_are_bounded_and_json_safe() -> None:
    size = ValueSize(
        items=10,
        rows=None,
        bytes=320,
        accuracy=SizeAccuracy.LOWER_BOUND,
        cost=MeasurementCost.CHEAP,
    )
    preview = ValuePreview(
        type_name="Структура",
        scalar=None,
        sample=({"name": "Employee", "value": "bounded"},),
        truncated=True,
    )

    assert ValueSize.unknown("scan_required") == ValueSize(
        accuracy=SizeAccuracy.UNKNOWN,
        cost=MeasurementCost.SCAN_REQUIRED,
    )
    assert to_wire(size)["accuracy"] == "lower_bound"
    assert to_wire(preview)["sample"] == [
        {"name": "Employee", "value": "bounded"}
    ]
    json.dumps(to_wire(preview))

    with pytest.raises(ValueError, match="sample.*bounded"):
        ValuePreview(type_name="list", sample=tuple(range(101)))
    with pytest.raises(ValueError, match="type_name.*bounded"):
        ValuePreview(type_name="x" * 257)


def test_public_proxy_enum_values_are_exact() -> None:
    assert {item.value for item in ProxyRealm} == {"onec", "python"}
    assert {item.value for item in ProxyLifetime} == {
        "context",
        "frame",
        "snapshot",
        "workspace",
    }
    assert {item.value for item in ProxyConsistency} == {"exact", "latest"}
    assert {item.value for item in SizeAccuracy} == {
        "exact",
        "estimate",
        "lower_bound",
        "unknown",
    }
    assert {item.value for item in MeasurementCost} == {
        "cheap",
        "scan_required",
        "evaluation_required",
    }
