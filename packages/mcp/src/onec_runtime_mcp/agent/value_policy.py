"""Server-owned budgets and cost metadata for agent value inspection."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from onec_runtime_mcp.agent.proxies import ProxyDescriptor, ValueBudget, ValuePreview, ValueSize


class ValueCostClass(StrEnum):
    METADATA = "metadata"
    EVALUATION = "evaluation"
    BOUNDED_SCAN = "bounded_scan"
    FULL_SCAN = "full_scan"


_COST_ORDER = {
    ValueCostClass.METADATA: 0,
    ValueCostClass.EVALUATION: 1,
    ValueCostClass.BOUNDED_SCAN: 2,
    ValueCostClass.FULL_SCAN: 3,
}


@dataclass(frozen=True, slots=True)
class ValueAction:
    name: str
    cost: ValueCostClass
    budget_profile: str
    available: bool


@dataclass(frozen=True, slots=True)
class ValueInspection:
    descriptor: ProxyDescriptor
    known_size: ValueSize | None
    bounded_preview: ValuePreview | None
    actions: tuple[ValueAction, ...]


class ValueBudgetProfiles:
    """Finite policy profiles; callers select names, never arbitrary limits."""

    _PROFILES = {
        "agent_metadata": ValueBudget(1, 20, 20, 16 * 1024, 1.0),
        "agent_preview": ValueBudget(4, 100, 100, 256 * 1024, 5.0),
        "agent_dataframe": ValueBudget(8, 200_000, 10_000, 64 * 1024 * 1024, 30.0),
    }
    _MAX_COST = {
        "agent_metadata": ValueCostClass.METADATA,
        "agent_preview": ValueCostClass.BOUNDED_SCAN,
        "agent_dataframe": ValueCostClass.FULL_SCAN,
    }

    def resolve(self, name: str) -> ValueBudget:
        if not isinstance(name, str):
            raise TypeError("budget profile must be a string")
        try:
            return self._PROFILES[name]
        except KeyError as error:
            raise ValueError("unknown value budget profile") from error

    def permits(self, name: str, cost: ValueCostClass) -> bool:
        self.resolve(name)
        if not isinstance(cost, ValueCostClass):
            raise TypeError("cost must be a ValueCostClass")
        return _COST_ORDER[cost] <= _COST_ORDER[self._MAX_COST[name]]

    def required_profile(self, cost: ValueCostClass) -> str:
        if cost is ValueCostClass.METADATA:
            return "agent_metadata"
        if cost in {ValueCostClass.EVALUATION, ValueCostClass.BOUNDED_SCAN}:
            return "agent_preview"
        return "agent_dataframe"


VALUE_POLICY_WIRE_DATACLASSES: tuple[type[object], ...] = (
    ValueAction,
    ValueInspection,
)


def inspection_actions(
    descriptor: ProxyDescriptor,
    *,
    profile: str,
    profiles: ValueBudgetProfiles,
) -> tuple[ValueAction, ...]:
    size_cost = (
        ValueCostClass.METADATA
        if descriptor.known_size is not None
        else ValueCostClass.BOUNDED_SCAN
    )
    preview_cost = (
        ValueCostClass.METADATA
        if descriptor.bounded_preview is not None
        else ValueCostClass.BOUNDED_SCAN
    )
    definitions = (
        ("size", size_cost),
        ("preview", preview_cost),
        ("materialize", ValueCostClass.FULL_SCAN),
        ("to_df", ValueCostClass.FULL_SCAN),
    )
    return tuple(
        ValueAction(
            name,
            cost,
            profiles.required_profile(cost),
            profiles.permits(profile, cost),
        )
        for name, cost in definitions
    )
