from __future__ import annotations

from collections.abc import Mapping, Sequence
from math import isfinite
from typing import Protocol
import re

import pandas as pd

from onec_runtime_mcp.agent.contracts import AgentOperationState, OperationDescriptor
from onec_runtime_mcp.agent.proxies import (
    MeasurementCost,
    ProxyDescriptor,
    ProxyLifetime,
    ProxyProvenance,
    ProxyRealm,
    ProxyRegistry,
    ReleasedProxy,
    StaleProxy,
    ValuePreview,
    ValueBudget,
    ValueSize,
)
from onec_runtime_mcp.agent.observation import ValueSelection
from onec_runtime.errors import ProtocolError
from onec_runtime.runtime_api import RuntimeNamespaceSnapshot


_BSL_IDENTIFIER = re.compile(r"[^\W\d]\w*", re.UNICODE)
_CONTEXT_CAPABILITIES = (
    "describe",
    "size",
    "preview",
    "get",
    "select",
    "snapshot",
    "materialize",
    "to_df",
)


class OnecValueBackend(Protocol):
    runtime_id: str

    @property
    def is_closed(self) -> bool: ...

    def namespace_snapshot(self) -> RuntimeNamespaceSnapshot: ...

    def validate_value_reference(self, handle: str) -> str: ...

    def materialize_value(self, handle: str, **options: object) -> object: ...

    def materialize_table(self, handle: str, **options: object) -> object: ...

    def materialization_kind(
        self, handle: str, *, timeout_s: float | None = None
    ) -> str: ...

    def materialize_value_payload(self, handle: str, **options: object) -> bytes: ...

    def materialize_table_payload(self, handle: str, **options: object) -> bytes: ...

    def project_value_payload(
        self, handle: str, selection: ValueSelection, **options: object
    ) -> tuple[str, bytes]: ...


def publish_onec_bindings(
    backend: OnecValueBackend,
    registry: ProxyRegistry,
    operation: OperationDescriptor,
    *,
    names: Sequence[str] | None = None,
) -> tuple[ProxyDescriptor, ...]:
    """Publish only the successful MAIN semantic catalog, never its values."""
    if not isinstance(operation, OperationDescriptor):
        raise TypeError("operation must be an OperationDescriptor")
    if operation.state is not AgentOperationState.COMPLETED:
        return ()
    if (
        operation.runtime_id != backend.runtime_id
        or operation.runtime_generation is None
        or operation.cell_id is None
        or operation.revision is None
        or operation.source_sha256 is None
    ):
        raise ProtocolError("completed operation lacks exact namespace provenance")
    if backend.is_closed:
        raise StaleProxy("1C runtime is closed")
    snapshot = backend.namespace_snapshot()
    if snapshot.runtime_generation != operation.runtime_generation:
        raise StaleProxy("operation and namespace runtime generations differ")
    provenance = ProxyProvenance(
        operation.cell_id,
        operation.revision,
        operation.source_sha256,
        operation.operation_id,
    )
    selected_names = snapshot.names if names is None else tuple(names)
    available = {name.casefold(): name for name in snapshot.names}
    if any(
        not isinstance(name, str) or name.casefold() not in available
        for name in selected_names
    ):
        raise ProtocolError("published binding is absent from runtime namespace")
    for name in selected_names:
        backend.validate_value_reference(f"Контекст.{name}")
    return registry.register_context_batch(
        tuple(
            {
                "qualified_name": f"bsl.{available[name.casefold()]}",
                "type_name": "Неизвестно",
                "runtime_id": backend.runtime_id,
                "runtime_generation": snapshot.runtime_generation,
                "context_generation": snapshot.context_generation,
                "provenance": provenance,
                "resolver_handle": f"Контекст.{name}",
                "capabilities": _CONTEXT_CAPABILITIES,
            }
            for name in selected_names
        )
    )


class OnecValueResolver:
    """Resolve fenced symbolic 1C handles through the existing runtime owner."""

    def __init__(self, backend: OnecValueBackend, registry: ProxyRegistry) -> None:
        self._backend = backend
        self._registry = registry

    def describe(self, proxy: ProxyDescriptor | str) -> ProxyDescriptor:
        return self._validated(proxy)[0]

    def size(self, proxy: ProxyDescriptor | str) -> ValueSize:
        descriptor, _ = self._validated(proxy)
        return descriptor.known_size or ValueSize.unknown(MeasurementCost.SCAN_REQUIRED)

    def preview(
        self,
        proxy: ProxyDescriptor | str,
        *,
        limits: Mapping[str, object],
    ) -> ValuePreview:
        items, byte_limit, timeout_s = self._preview_limits(limits)
        descriptor, handle = self._validated(proxy)
        options: dict[str, object] = {
            "refs": "presentation",
            "max_depth": 2,
            "max_items": items,
            "max_bytes": byte_limit,
        }
        if timeout_s is not None:
            options["timeout_s"] = timeout_s
        value = self._backend.materialize_value(handle, **options)
        return self._bounded_preview(descriptor.type_name, value, items)

    def get(
        self,
        proxy: ProxyDescriptor | str,
        name_or_index: str,
    ) -> ProxyDescriptor:
        if not isinstance(name_or_index, str) or not _BSL_IDENTIFIER.fullmatch(
            name_or_index
        ):
            raise ValueError("child name must be one BSL identifier")
        parent, handle = self._validated(proxy)
        child_handle = f"{handle}.{name_or_index}"
        self._backend.validate_value_reference(child_handle)
        provenance = parent.provenance
        return self._registry.register_context(
            qualified_name=f"{parent.qualified_name}.{name_or_index}",
            type_name="Неизвестно",
            runtime_id=parent.fence.runtime_id or "",
            runtime_generation=parent.fence.runtime_generation or 0,
            context_generation=parent.fence.context_generation or 0,
            provenance=ProxyProvenance(
                provenance.cell_id,
                provenance.revision,
                provenance.source_sha256,
                provenance.operation_id,
                parent_proxy_ids=(parent.proxy_id,),
            ),
            resolver_handle=child_handle,
            capabilities=_CONTEXT_CAPABILITIES,
        )

    def select(
        self,
        proxy: ProxyDescriptor | str,
        fields: Sequence[str],
    ) -> ProxyDescriptor:
        self._validated(proxy)
        if isinstance(fields, str) or not fields or len(fields) > 100:
            raise ValueError("selection fields must be a bounded sequence")
        if any(not isinstance(item, str) or not _BSL_IDENTIFIER.fullmatch(item) for item in fields):
            raise ValueError("selection fields must be BSL identifiers")
        raise ProtocolError("1C projection snapshots require the value service")

    def snapshot(self, proxy: ProxyDescriptor | str) -> ProxyDescriptor:
        self._validated(proxy)
        raise ProtocolError("1C snapshots require the managed Python workspace")

    def materialize(
        self,
        proxy: ProxyDescriptor | str,
        *,
        max_depth: int,
        max_items: int,
        max_bytes: int,
        refs: str = "presentation",
    ) -> object:
        for name, value in (
            ("max_depth", max_depth),
            ("max_items", max_items),
            ("max_bytes", max_bytes),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be positive")
        _, handle = self._validated(proxy)
        return self._backend.materialize_value(
            handle,
            refs=refs,
            max_depth=max_depth,
            max_items=max_items,
            max_bytes=max_bytes,
        )

    def to_df(
        self,
        proxy: ProxyDescriptor | str,
        *,
        refs: str = "presentation",
        chunk_size: int = 2400,
    ) -> object:
        if type(chunk_size) is not int or chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        _, handle = self._validated(proxy)
        return self._backend.materialize_table(
            handle,
            refs=refs,
            chunk_size=chunk_size,
        )

    def materialization_kind(
        self, proxy: ProxyDescriptor | str, *, timeout_s: float | None = None
    ) -> str:
        _, handle = self._validated(proxy)
        return self._backend.materialization_kind(handle, timeout_s=timeout_s)

    def value_payload(
        self,
        proxy: ProxyDescriptor | str,
        *,
        refs: str,
        budget: ValueBudget,
    ) -> bytes:
        _, handle = self._validated(proxy)
        return self._backend.materialize_value_payload(
            handle,
            refs=refs,
            max_depth=budget.max_depth,
            max_items=budget.max_items,
            max_bytes=budget.max_bytes,
            timeout_s=budget.timeout_seconds,
        )

    def table_payload(
        self,
        proxy: ProxyDescriptor | str,
        *,
        refs: str,
        ref_columns: Mapping[str, str] | None,
        uuid_suffix: str,
        budget: ValueBudget,
    ) -> bytes:
        _, handle = self._validated(proxy)
        return self._backend.materialize_table_payload(
            handle,
            refs=refs,
            ref_columns=None if ref_columns is None else dict(ref_columns),
            uuid_suffix=uuid_suffix,
            max_rows=budget.max_rows,
            max_bytes=budget.max_bytes,
            timeout_s=budget.timeout_seconds,
        )

    def project_payload(
        self,
        proxy: ProxyDescriptor | str,
        selection: ValueSelection,
        *,
        budget: ValueBudget,
    ) -> tuple[str, bytes]:
        if not isinstance(selection, ValueSelection):
            raise TypeError("selection must be a ValueSelection")
        _, handle = self._validated(proxy)
        return self._backend.project_value_payload(
            handle,
            selection,
            max_depth=budget.max_depth,
            max_items=budget.max_items,
            max_rows=budget.max_rows,
            max_bytes=budget.max_bytes,
            timeout_s=budget.timeout_seconds,
        )

    def release(self, proxy: ProxyDescriptor | str) -> bool:
        proxy_id = proxy.proxy_id if isinstance(proxy, ProxyDescriptor) else proxy
        try:
            descriptor = self._registry.resolve(proxy_id)
        except ReleasedProxy:
            return False
        if descriptor.lifetime is ProxyLifetime.FRAME:
            return self._registry.release(proxy_id)
        if self._registry.is_exact_snapshot(proxy_id):
            return self._registry.release(proxy_id)
        descriptor, handle, should_release = self._registry.release_target(proxy_id)
        if not should_release:
            return False
        self._registry.register_context(
            qualified_name=descriptor.qualified_name,
            type_name=descriptor.type_name,
            runtime_id=descriptor.fence.runtime_id or "",
            runtime_generation=descriptor.fence.runtime_generation or 0,
            context_generation=descriptor.fence.context_generation or 0,
            provenance=descriptor.provenance,
            consistency=descriptor.consistency,
            resolver_handle=handle,
            capabilities=descriptor.capabilities,
            known_size=descriptor.known_size,
            bounded_preview=descriptor.bounded_preview,
        )
        released = self._registry.release(descriptor.proxy_id)
        if proxy_id != descriptor.proxy_id:
            self._registry.release(proxy_id)
        return released

    def _validated(self, proxy: ProxyDescriptor | str) -> tuple[ProxyDescriptor, str]:
        proxy_id = proxy.proxy_id if isinstance(proxy, ProxyDescriptor) else proxy
        if not isinstance(proxy_id, str):
            raise TypeError("proxy must be a descriptor or proxy ID")
        descriptor = self._registry.resolve(proxy_id)
        if descriptor.realm is not ProxyRealm.ONEC:
            raise ProtocolError("proxy is not a 1C value")
        if self._backend.is_closed:
            raise StaleProxy("1C runtime is closed")
        fence = descriptor.fence
        if fence.runtime_id != self._backend.runtime_id:
            raise StaleProxy("1C proxy belongs to another runtime")
        if descriptor.lifetime is ProxyLifetime.FRAME:
            handle = self._registry.resolver_handle(descriptor.proxy_id)
            if not isinstance(handle, str) or not handle:
                raise ProtocolError("frame proxy has no opaque resolver handle")
            self._backend.validate_value_reference(handle)
            return descriptor, handle
        if descriptor.lifetime is not ProxyLifetime.CONTEXT:
            raise ProtocolError("proxy is not a persistent or captured 1C value")
        try:
            snapshot = self._backend.namespace_snapshot()
        except BaseException as error:
            raise StaleProxy("1C runtime is closed or unavailable") from error
        if (
            snapshot.runtime_generation != fence.runtime_generation
            or snapshot.context_generation != fence.context_generation
        ):
            raise StaleProxy("1C proxy generation fence is stale")
        root = descriptor.qualified_name.removeprefix("bsl.").split(".", 1)[0]
        if root.casefold() not in {name.casefold() for name in snapshot.names}:
            raise StaleProxy("1C context binding no longer exists")
        handle = self._registry.resolver_handle(descriptor.proxy_id)
        if not isinstance(handle, str) or not handle.startswith("Контекст."):
            raise ProtocolError("1C proxy has no symbolic context handle")
        self._backend.validate_value_reference(handle)
        return descriptor, handle

    @staticmethod
    def _preview_limits(limits: Mapping[str, object]) -> tuple[int, int, float | None]:
        if not isinstance(limits, Mapping) or set(limits) not in (
            {"items", "bytes"},
            {"items", "bytes", "timeout_s"},
        ):
            raise ValueError("preview limits require exact items, bytes, and optional timeout")
        items = limits["items"]
        byte_limit = limits["bytes"]
        if type(items) is not int or items <= 0 or items > 100:
            raise ValueError("preview items must be between 1 and 100")
        if type(byte_limit) is not int or byte_limit <= 0 or byte_limit > 16 * 1024:
            raise ValueError("preview bytes must be between 1 and 16384")
        timeout = limits.get("timeout_s")
        if timeout is not None and (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not isfinite(float(timeout))
            or float(timeout) <= 0
        ):
            raise ValueError("preview timeout must be a finite positive number")
        return items, byte_limit, None if timeout is None else float(timeout)

    @staticmethod
    def _bounded_preview(type_name: str, value: object, items: int) -> ValuePreview:
        if value is None or type(value) in {bool, int, float}:
            return ValuePreview(type_name=type_name, scalar=value)
        if isinstance(value, str):
            truncated = len(value.encode("utf-8")) > 8 * 1024
            return ValuePreview(
                type_name=type_name,
                scalar=value[:4096] if truncated else value,
                truncated=truncated,
            )
        if isinstance(value, Mapping):
            sample = tuple(
                {"name": key, "value": item}
                for key, item in list(value.items())[:items]
                if isinstance(key, str) and _is_json_value(item)
            )
            return ValuePreview(
                type_name=type_name,
                sample=sample,
                truncated=len(value) > len(sample),
            )
        if isinstance(value, pd.DataFrame):
            sample = tuple(
                row
                for row in value.head(items).to_dict(orient="records")
                if _is_json_value(row)
            )
            return ValuePreview(
                type_name=type_name,
                sample=sample,
                # A transfer that exactly reaches its server-owned row cap
                # cannot prove the source ended there, so report truncation.
                truncated=len(value.index) >= items,
            )
        if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
            sample = tuple(item for item in value[:items] if _is_json_value(item))
            return ValuePreview(
                type_name=type_name,
                sample=sample,
                truncated=len(value) > len(sample),
            )
        return ValuePreview(type_name=type_name, truncated=True)


def _is_json_value(value: object) -> bool:
    if value is None or type(value) in {bool, int, float, str}:
        return True
    if isinstance(value, (tuple, list)):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, Mapping):
        return all(type(key) is str and _is_json_value(item) for key, item in value.items())
    return False
