from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from types import MappingProxyType
from time import monotonic
from typing import Protocol
from uuid import uuid4

from onec_runtime_mcp.agent.contracts import (
    FailureCategory,
    MethodFailure,
    RetrySafety,
    ServiceResponse,
    StateChanged,
    to_wire,
)
from onec_runtime_mcp.agent.proxies import (
    ProxyDescriptor,
    ProxyProvenance,
    ProxyRealm,
    ProxyRegistry,
    MeasurementCost,
    ReleasedProxy,
    SizeAccuracy,
    StaleProxy,
    ValueBudget,
    ValuePreview,
    ValueSize,
)
from onec_runtime_mcp.agent.onec_values import OnecValueResolver
from onec_runtime_mcp.agent.observation import ValueSelection
from onec_runtime_mcp.agent.python_workspace import PythonWorkspace
from onec_runtime_mcp.agent.value_policy import (
    ValueBudgetProfiles,
    ValueCostClass,
    ValueInspection,
    inspection_actions,
)


class UnsupportedValueOperation(RuntimeError):
    """The value realm does not implement an explicitly requested operation."""


class ValueResolver(Protocol):
    def describe(self, proxy: ProxyDescriptor) -> ProxyDescriptor: ...

    def size(self, proxy: ProxyDescriptor) -> ValueSize: ...

    def preview(
        self, proxy: ProxyDescriptor, *, limits: Mapping[str, object]
    ) -> ValuePreview: ...

    def get(self, proxy: ProxyDescriptor, selector: object) -> ProxyDescriptor: ...

    def select(
        self, proxy: ProxyDescriptor, fields: Sequence[str]
    ) -> ProxyDescriptor: ...

    def project(
        self,
        proxy: ProxyDescriptor,
        selection: ValueSelection,
        *,
        budget: ValueBudget,
    ) -> ProxyDescriptor: ...

    def snapshot(
        self, proxy: ProxyDescriptor, *, budget: ValueBudget
    ) -> ProxyDescriptor: ...

    def materialize(
        self,
        proxy: ProxyDescriptor,
        *,
        target: str,
        policy: Mapping[str, object],
        budget: ValueBudget,
    ) -> ProxyDescriptor: ...

    def to_df(
        self,
        proxy: ProxyDescriptor,
        *,
        columns: tuple[str, ...] | None,
        refs: str,
        budget: ValueBudget,
    ) -> ProxyDescriptor: ...

    def compare(
        self,
        left: ProxyDescriptor,
        right: ProxyDescriptor,
        *,
        policy: Mapping[str, object],
        budget: ValueBudget,
    ) -> Mapping[str, object]: ...

    def release(self, proxy: ProxyDescriptor) -> bool: ...


class OnecMaterializationBridge:
    """Move typed 1C payloads into the managed Python object registry."""

    def __init__(
        self,
        onec: OnecValueResolver,
        python: PythonWorkspace,
    ) -> None:
        self._onec = onec
        self._python = python

    def describe(self, proxy: ProxyDescriptor) -> ProxyDescriptor:
        return self._onec.describe(proxy)

    def size(self, proxy: ProxyDescriptor) -> ValueSize:
        return self._onec.size(proxy)

    def preview(
        self, proxy: ProxyDescriptor, *, limits: Mapping[str, object]
    ) -> ValuePreview:
        return self._onec.preview(proxy, limits=limits)

    def get(self, proxy: ProxyDescriptor, selector: object) -> ProxyDescriptor:
        if not isinstance(selector, str):
            raise ValueError("1C child selector must be a name")
        return self._onec.get(proxy, selector)

    def select(
        self, proxy: ProxyDescriptor, fields: Sequence[str]
    ) -> ProxyDescriptor:
        return self._onec.select(proxy, fields)

    def project(
        self,
        proxy: ProxyDescriptor,
        selection: ValueSelection,
        *,
        budget: ValueBudget,
    ) -> ProxyDescriptor:
        deadline = monotonic() + budget.timeout_seconds
        transfer_budget = self._remaining_budget(budget, deadline)
        kind, payload = self._onec.project_payload(
            proxy,
            selection,
            budget=transfer_budget,
        )
        decode_budget = self._remaining_budget(budget, deadline)
        result = self._python.ingest_typed_payload(
            payload,
            kind=kind,
            options={
                "refs": "presentation",
                "max_depth": decode_budget.max_depth,
                "max_items": decode_budget.max_items,
                "max_rows": decode_budget.max_rows,
            },
            budget=decode_budget,
            provenance=self._derived_provenance(proxy),
        )
        self._remaining_budget(budget, deadline)
        return result

    def snapshot(
        self, proxy: ProxyDescriptor, *, budget: ValueBudget
    ) -> ProxyDescriptor:
        return self.materialize(
            proxy,
            target="python",
            policy={},
            budget=budget,
        )

    def materialize(
        self,
        proxy: ProxyDescriptor,
        *,
        target: str,
        policy: Mapping[str, object],
        budget: ValueBudget,
    ) -> ProxyDescriptor:
        if target != "python":
            raise UnsupportedValueOperation("only Python materialization is available")
        deadline = monotonic() + budget.timeout_seconds
        normalized = self._policy(policy)
        refs = normalized["refs"]
        route = self._onec.materialization_kind(
            proxy,
            timeout_s=self._remaining_budget(budget, deadline).timeout_seconds,
        )
        transfer_budget = self._remaining_budget(budget, deadline)
        if route == "value":
            payload = self._onec.value_payload(
                proxy, refs=refs, budget=transfer_budget
            )
            kind = "value"
            options: dict[str, object] = {
                "refs": refs,
                "max_depth": transfer_budget.max_depth,
                "max_items": transfer_budget.max_items,
            }
        elif route == "table":
            payload = self._onec.table_payload(
                proxy,
                refs=refs,
                ref_columns=normalized["ref_columns"],
                uuid_suffix=normalized["uuid_suffix"],
                budget=transfer_budget,
            )
            kind = "compact_table"
            options = {
                **normalized,
                "max_rows": transfer_budget.max_rows,
                "columns": None,
            }
        else:
            raise UnsupportedValueOperation("1C materialization route is unavailable")
        decode_budget = self._remaining_budget(budget, deadline)
        result = self._python.ingest_typed_payload(
            payload,
            kind=kind,
            options=options,
            budget=decode_budget,
            provenance=self._derived_provenance(proxy),
        )
        self._remaining_budget(budget, deadline)
        return result

    def to_df(
        self,
        proxy: ProxyDescriptor,
        *,
        columns: tuple[str, ...] | None,
        refs: str,
        budget: ValueBudget,
    ) -> ProxyDescriptor:
        deadline = monotonic() + budget.timeout_seconds
        normalized = self._policy({"refs": refs})
        if self._onec.materialization_kind(
            proxy,
            timeout_s=self._remaining_budget(budget, deadline).timeout_seconds,
        ) != "table":
            raise UnsupportedValueOperation("value.to_df requires a tabular 1C value")
        transfer_budget = self._remaining_budget(budget, deadline)
        payload = self._onec.table_payload(
            proxy,
            refs=refs,
            ref_columns=None,
            uuid_suffix=normalized["uuid_suffix"],
            budget=transfer_budget,
        )
        decode_budget = self._remaining_budget(budget, deadline)
        result = self._python.ingest_typed_payload(
            payload,
            kind="compact_table",
            options={
                **normalized,
                "max_rows": decode_budget.max_rows,
                "columns": None if columns is None else list(columns),
            },
            budget=decode_budget,
            provenance=self._derived_provenance(proxy),
        )
        self._remaining_budget(budget, deadline)
        return result

    def compare(
        self,
        left: ProxyDescriptor,
        right: ProxyDescriptor,
        *,
        policy: Mapping[str, object],
        budget: ValueBudget,
    ) -> Mapping[str, object]:
        del left, right, policy, budget
        raise UnsupportedValueOperation("1C proxy comparison is not implemented")

    def release(self, proxy: ProxyDescriptor) -> bool:
        return self._onec.release(proxy)

    @staticmethod
    def _remaining_budget(budget: ValueBudget, deadline: float) -> ValueBudget:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("materialization deadline exceeded")
        return replace(budget, timeout_seconds=remaining)

    @staticmethod
    def _policy(policy: Mapping[str, object]) -> dict[str, object]:
        if set(policy) - {"refs", "ref_columns", "uuid_suffix"}:
            raise ValueError("unsupported materialization policy")
        refs = policy.get("refs", "presentation")
        if refs not in {"presentation", "uuid", "both"}:
            raise ValueError("invalid reference policy")
        ref_columns = policy.get("ref_columns")
        if ref_columns is not None and (
            not isinstance(ref_columns, Mapping)
            or any(
                not isinstance(key, str)
                or value not in {"presentation", "uuid", "both"}
                for key, value in ref_columns.items()
            )
        ):
            raise ValueError("invalid reference column policy")
        suffix = policy.get("uuid_suffix", "__uuid")
        if not isinstance(suffix, str) or not suffix:
            raise ValueError("uuid_suffix must be non-empty")
        return {
            "refs": refs,
            "ref_columns": None if ref_columns is None else dict(ref_columns),
            "uuid_suffix": suffix,
        }

    @staticmethod
    def _derived_provenance(proxy: ProxyDescriptor) -> ProxyProvenance:
        source = proxy.provenance
        return ProxyProvenance(
            source.cell_id,
            source.revision,
            source.source_sha256,
            source.operation_id,
            parent_proxy_ids=(proxy.proxy_id,),
        )


class PythonValueResolver:
    """Bounded metadata and release operations for child-owned Python objects."""

    def __init__(self, workspace: PythonWorkspace) -> None:
        self._workspace = workspace

    def describe(self, proxy: ProxyDescriptor) -> ProxyDescriptor:
        return self._workspace.registry.resolve(proxy.proxy_id)

    def size(self, proxy: ProxyDescriptor) -> ValueSize:
        inspection = self._workspace.inspect(proxy.proxy_id)
        rows = inspection.shape[0] if inspection.shape else None
        items = None
        if inspection.shape:
            items = 1
            for dimension in inspection.shape:
                items *= dimension
        accuracy = (
            SizeAccuracy.EXACT
            if items is not None or inspection.memory_bytes is not None
            else SizeAccuracy.UNKNOWN
        )
        return ValueSize(
            items=items,
            rows=rows,
            bytes=inspection.memory_bytes,
            accuracy=accuracy,
            cost=MeasurementCost.CHEAP,
        )

    def preview(
        self, proxy: ProxyDescriptor, *, limits: Mapping[str, object]
    ) -> ValuePreview:
        del limits
        inspection = self._workspace.inspect(proxy.proxy_id)
        return ValuePreview(
            type_name=inspection.type_name,
            scalar=inspection.preview,
        )

    def get(self, proxy: ProxyDescriptor, selector: object) -> ProxyDescriptor:
        del proxy, selector
        raise UnsupportedValueOperation("Python child selection is not implemented")

    def select(
        self, proxy: ProxyDescriptor, fields: Sequence[str]
    ) -> ProxyDescriptor:
        del proxy, fields
        raise UnsupportedValueOperation("Python projection is not implemented")

    def project(
        self,
        proxy: ProxyDescriptor,
        selection: ValueSelection,
        *,
        budget: ValueBudget,
    ) -> ProxyDescriptor:
        del proxy, selection, budget
        raise UnsupportedValueOperation("Python projection is not implemented")

    def snapshot(
        self, proxy: ProxyDescriptor, *, budget: ValueBudget
    ) -> ProxyDescriptor:
        del proxy, budget
        raise UnsupportedValueOperation("Python snapshot is not implemented")

    def materialize(
        self,
        proxy: ProxyDescriptor,
        *,
        target: str,
        policy: Mapping[str, object],
        budget: ValueBudget,
    ) -> ProxyDescriptor:
        del policy, budget
        if target != "python":
            raise UnsupportedValueOperation("only Python materialization is available")
        return self._workspace.registry.resolve(proxy.proxy_id)

    def to_df(
        self,
        proxy: ProxyDescriptor,
        *,
        columns: tuple[str, ...] | None,
        refs: str,
        budget: ValueBudget,
    ) -> ProxyDescriptor:
        del columns, refs, budget
        if proxy.type_name != "pandas.DataFrame":
            raise UnsupportedValueOperation("Python value is not a DataFrame")
        return self._workspace.registry.resolve(proxy.proxy_id)

    def compare(
        self,
        left: ProxyDescriptor,
        right: ProxyDescriptor,
        *,
        policy: Mapping[str, object],
        budget: ValueBudget,
    ) -> Mapping[str, object]:
        del left, right, policy, budget
        raise UnsupportedValueOperation("Python comparison is not implemented")

    def release(self, proxy: ProxyDescriptor) -> bool:
        return self._workspace.release(proxy.proxy_id)


class ValueService:
    """Explicit bounded value-domain dispatch over generation-fenced proxies."""

    def __init__(
        self,
        registry: ProxyRegistry,
        resolvers: Mapping[ProxyRealm, ValueResolver],
    ) -> None:
        if not isinstance(registry, ProxyRegistry):
            raise TypeError("registry must be a ProxyRegistry")
        if not isinstance(resolvers, Mapping):
            raise TypeError("resolvers must be a mapping")
        self.registry = registry
        self._resolvers = resolvers
        self._budget_profiles = ValueBudgetProfiles()
        self._handlers = {
            "value.inspect": self._inspect,
            "value.describe": self._describe,
            "value.size": self._size,
            "value.preview": self._preview,
            "value.get": self._get,
            "value.select": self._select,
            "value.snapshot": self._snapshot,
            "value.materialize": self._materialize,
            "value.to_df": self._to_df,
            "value.compare": self._compare,
            "value.release": self._release,
        }

    def call(self, method: str, arguments: Mapping[str, object]) -> ServiceResponse:
        try:
            return ServiceResponse.success(self.dispatch(method, arguments))
        except (StaleProxy, ReleasedProxy):
            return self._failure(FailureCategory.STALE)
        except UnsupportedValueOperation:
            return self._failure(FailureCategory.UNSUPPORTED)
        except (TypeError, ValueError):
            return self._failure(FailureCategory.INVALID_REQUEST)
        except BaseException:
            return self._failure(FailureCategory.UNKNOWN)

    def dispatch(self, method: str, arguments: Mapping[str, object]) -> object:
        if not isinstance(method, str) or not isinstance(arguments, Mapping):
            raise TypeError("value method and arguments are required")
        if method == "value.export":
            raise UnsupportedValueOperation("value.export is unavailable")
        handler = self._handlers.get(method)
        if handler is None:
            raise ValueError("unknown value method")
        return handler(arguments)

    def inspect(
        self,
        proxy_id: str,
        *,
        detail: str = "auto",
        budget_profile: str = "agent_metadata",
        budget: ValueBudget | None = None,
    ) -> ValueInspection:
        selected_budget = (
            self._budget_profiles.resolve(budget_profile)
            if budget is None
            else budget
        )
        if not isinstance(selected_budget, ValueBudget):
            raise TypeError("budget must be a ValueBudget")
        return self._inspect_value(
            proxy_id,
            detail=detail,
            profile=budget_profile,
            budget=selected_budget,
        )

    def project(
        self,
        proxy_id: str,
        selection: ValueSelection,
        *,
        budget: ValueBudget,
    ) -> ProxyDescriptor:
        if not isinstance(selection, ValueSelection):
            raise TypeError("selection must be a ValueSelection")
        descriptor, resolver = self._resolve({"proxy_id": proxy_id}, "proxy_id")
        project = getattr(resolver, "project", None)
        if not callable(project):
            raise UnsupportedValueOperation("value projection is unavailable")
        result = project(descriptor, selection, budget=budget)
        return self._descriptor(result)

    def _inspect(self, arguments: Mapping[str, object]) -> ValueInspection:
        self._only(arguments, {"proxy_id", "detail", "budget_profile"})
        proxy_id = self._string(arguments, "proxy_id")
        detail = arguments.get("detail", "auto")
        profile = arguments.get("budget_profile", "agent_metadata")
        if detail not in {"auto", "metadata", "size", "preview"}:
            raise ValueError("unsupported inspection detail")
        if not isinstance(profile, str):
            raise ValueError("budget_profile must be a string")
        budget = self._budget_profiles.resolve(profile)
        return self._inspect_value(
            proxy_id,
            detail=detail,
            profile=profile,
            budget=budget,
        )

    def _inspect_value(
        self,
        proxy_id: str,
        *,
        detail: object,
        profile: str,
        budget: ValueBudget,
    ) -> ValueInspection:
        if detail not in {"auto", "metadata", "size", "preview"}:
            raise ValueError("unsupported inspection detail")
        descriptor = self.registry.resolve(proxy_id)
        known_size = descriptor.known_size
        bounded_preview = descriptor.bounded_preview
        if detail == "size" and known_size is None:
            if not self._budget_profiles.permits(profile, ValueCostClass.BOUNDED_SCAN):
                raise ValueError("budget profile does not permit size scan")
            resolver = self._resolvers.get(descriptor.realm)
            if resolver is None:
                raise UnsupportedValueOperation("value realm is unavailable")
            known_size = resolver.size(descriptor)
            if not isinstance(known_size, ValueSize):
                raise TypeError("resolver returned an invalid size")
        elif detail == "preview" and bounded_preview is None:
            if not self._budget_profiles.permits(profile, ValueCostClass.BOUNDED_SCAN):
                raise ValueError("budget profile does not permit preview scan")
            resolver = self._resolvers.get(descriptor.realm)
            if resolver is None:
                raise UnsupportedValueOperation("value realm is unavailable")
            bounded_preview = resolver.preview(
                descriptor,
                limits=MappingProxyType(
                    {
                        "items": min(budget.max_items, 100),
                        "bytes": min(budget.max_bytes, 16 * 1024),
                        "timeout_s": budget.timeout_seconds,
                    }
                ),
            )
            if not isinstance(bounded_preview, ValuePreview):
                raise TypeError("resolver returned an invalid preview")
        return ValueInspection(
            descriptor,
            known_size,
            bounded_preview,
            inspection_actions(
                descriptor,
                profile=profile,
                profiles=self._budget_profiles,
            ),
        )

    def _describe(self, arguments: Mapping[str, object]) -> ProxyDescriptor:
        self._only(arguments, {"proxy_id"})
        descriptor, resolver = self._resolve(arguments, "proxy_id")
        result = resolver.describe(descriptor)
        return self._descriptor(result)

    def _size(self, arguments: Mapping[str, object]) -> ValueSize:
        self._only(arguments, {"proxy_id", "budget"})
        if "budget" in arguments:
            self._budget(arguments["budget"])
        descriptor, resolver = self._resolve(arguments, "proxy_id")
        result = resolver.size(descriptor)
        if not isinstance(result, ValueSize):
            raise TypeError("resolver returned an invalid size")
        return result

    def _preview(self, arguments: Mapping[str, object]) -> ValuePreview:
        self._only(arguments, {"proxy_id", "limits"})
        limits = self._limits(arguments.get("limits"))
        descriptor, resolver = self._resolve(arguments, "proxy_id")
        result = resolver.preview(descriptor, limits=limits)
        if not isinstance(result, ValuePreview):
            raise TypeError("resolver returned an invalid preview")
        return result

    def _get(self, arguments: Mapping[str, object]) -> ProxyDescriptor:
        self._only(arguments, {"proxy_id", "selector"})
        if "selector" not in arguments:
            raise ValueError("selector is required")
        selector = arguments["selector"]
        if type(selector) not in {str, int}:
            raise ValueError("selector must be a string or integer")
        descriptor, resolver = self._resolve(arguments, "proxy_id")
        return self._descriptor(resolver.get(descriptor, selector))

    def _select(self, arguments: Mapping[str, object]) -> ProxyDescriptor:
        self._only(arguments, {"proxy_id", "fields"})
        fields = self._string_sequence(arguments.get("fields"), name="fields")
        descriptor, resolver = self._resolve(arguments, "proxy_id")
        return self._descriptor(resolver.select(descriptor, fields))

    def _snapshot(self, arguments: Mapping[str, object]) -> ProxyDescriptor:
        self._only(arguments, {"proxy_id", "budget"})
        budget = self._budget(arguments.get("budget"))
        descriptor, resolver = self._resolve(arguments, "proxy_id")
        return self._descriptor(resolver.snapshot(descriptor, budget=budget))

    def _materialize(self, arguments: Mapping[str, object]) -> ProxyDescriptor:
        self._only(arguments, {"proxy_id", "target", "policy", "budget"})
        budget = self._budget(arguments.get("budget"))
        target = arguments.get("target", "python")
        if target != "python":
            raise UnsupportedValueOperation("only Python materialization is available")
        policy = self._mapping(arguments.get("policy", {}), name="policy")
        descriptor, resolver = self._resolve(arguments, "proxy_id")
        result = resolver.materialize(
            descriptor,
            target="python",
            policy=policy,
            budget=budget,
        )
        return self._descriptor(result)

    def _to_df(self, arguments: Mapping[str, object]) -> ProxyDescriptor:
        self._only(arguments, {"proxy_id", "columns", "refs", "budget"})
        budget = self._budget(arguments.get("budget"))
        columns = (
            None
            if arguments.get("columns") is None
            else self._string_sequence(arguments["columns"], name="columns")
        )
        refs = arguments.get("refs", "presentation")
        if refs not in {"presentation", "uuid", "both"}:
            raise ValueError("refs must be presentation, uuid, or both")
        descriptor, resolver = self._resolve(arguments, "proxy_id")
        return self._descriptor(
            resolver.to_df(
                descriptor,
                columns=columns,
                refs=refs,
                budget=budget,
            )
        )

    def _compare(self, arguments: Mapping[str, object]) -> Mapping[str, object]:
        self._only(
            arguments,
            {"left_proxy_id", "right_proxy_id", "policy", "budget"},
        )
        budget = self._budget(arguments.get("budget"))
        left, resolver = self._resolve(arguments, "left_proxy_id")
        right, right_resolver = self._resolve(arguments, "right_proxy_id")
        if right.realm is not left.realm or right_resolver is not resolver:
            raise UnsupportedValueOperation("cross-realm comparison is unavailable")
        policy = self._mapping(arguments.get("policy", {}), name="policy")
        result = resolver.compare(left, right, policy=policy, budget=budget)
        if not isinstance(result, Mapping):
            raise TypeError("resolver returned an invalid comparison")
        to_wire(result)
        return result

    def _release(self, arguments: Mapping[str, object]) -> dict[str, bool]:
        self._only(arguments, {"proxy_id"})
        proxy_id = self._string(arguments, "proxy_id")
        descriptor = self.registry.metadata(proxy_id)
        resolver = self._resolvers.get(descriptor.realm)
        released = (
            self.registry.release(proxy_id)
            if resolver is None
            else resolver.release(descriptor)
        )
        return {
            "released": released,
            "binding_deleted": False,
        }

    def _resolve(
        self,
        arguments: Mapping[str, object],
        field_name: str,
    ) -> tuple[ProxyDescriptor, ValueResolver]:
        descriptor = self.registry.resolve(self._string(arguments, field_name))
        resolver = self._resolvers.get(descriptor.realm)
        if resolver is None:
            raise UnsupportedValueOperation("value realm is unavailable")
        return descriptor, resolver

    @staticmethod
    def _descriptor(value: object) -> ProxyDescriptor:
        if not isinstance(value, ProxyDescriptor):
            raise TypeError("resolver must return a proxy descriptor")
        return value

    @staticmethod
    def _budget(value: object) -> ValueBudget:
        if not isinstance(value, Mapping) or set(value) != {
            "depth",
            "items",
            "rows",
            "bytes",
            "timeout_s",
        }:
            raise ValueError("budget requires exact depth/items/rows/bytes/timeout_s")
        return ValueBudget(
            max_depth=value["depth"],  # type: ignore[arg-type]
            max_items=value["items"],  # type: ignore[arg-type]
            max_rows=value["rows"],  # type: ignore[arg-type]
            max_bytes=value["bytes"],  # type: ignore[arg-type]
            timeout_seconds=value["timeout_s"],  # type: ignore[arg-type]
        )

    @staticmethod
    def _limits(value: object) -> Mapping[str, object]:
        if not isinstance(value, Mapping) or set(value) != {"items", "bytes"}:
            raise ValueError("preview limits require exact items and bytes")
        items, byte_limit = value["items"], value["bytes"]
        if type(items) is not int or items <= 0 or items > 100:
            raise ValueError("preview items must be between 1 and 100")
        if type(byte_limit) is not int or byte_limit <= 0 or byte_limit > 16 * 1024:
            raise ValueError("preview bytes must be between 1 and 16384")
        return MappingProxyType({"items": items, "bytes": byte_limit})

    @staticmethod
    def _mapping(value: object, *, name: str) -> Mapping[str, object]:
        if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
            raise ValueError(f"{name} must be a string-keyed mapping")
        return MappingProxyType(dict(value))

    @staticmethod
    def _string_sequence(value: object, *, name: str) -> tuple[str, ...]:
        if isinstance(value, str) or not isinstance(value, (tuple, list)):
            raise ValueError(f"{name} must be a sequence")
        result = tuple(value)
        if not result or len(result) > 100 or any(
            not isinstance(item, str) or not item for item in result
        ):
            raise ValueError(f"{name} must contain bounded non-empty strings")
        return result

    @staticmethod
    def _string(arguments: Mapping[str, object], name: str) -> str:
        value = arguments.get(name)
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name} must be non-empty")
        return value

    @staticmethod
    def _only(arguments: Mapping[str, object], allowed: set[str]) -> None:
        if set(arguments) - allowed:
            raise ValueError("unsupported arguments")

    @staticmethod
    def _failure(category: FailureCategory) -> ServiceResponse:
        return ServiceResponse.fail(
            MethodFailure(
                category=category,
                state_changed=StateChanged.NO,
                safe_to_retry=RetrySafety.NO,
                current_state={"domain": "value"},
                diagnostic_id=f"diag_{uuid4().hex}",
            )
        )
