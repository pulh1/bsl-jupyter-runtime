"""Projection of durable Domain operations into the compact agent view."""

from __future__ import annotations

from collections.abc import Callable
from types import MappingProxyType

from onec_runtime_mcp.agent.facade_contracts import (
    AgentOperationIdentity,
    AgentOperationView,
    OperationTruncation,
)
from onec_runtime_mcp.agent.operations import OperationRegistry
from onec_runtime_mcp.agent.proxies import ProxyDescriptor


class OperationViewProjector:
    def __init__(
        self,
        registry: OperationRegistry,
        resolve_proxy: Callable[[str], ProxyDescriptor | None],
    ) -> None:
        self._registry = registry
        self._resolve_proxy = resolve_proxy

    def project(
        self,
        operation_id: str,
        *,
        after_message_cursor: int = 0,
        message_limit: int = 20,
        changed_limit: int = 100,
        output_limit: int = 100,
    ) -> AgentOperationView:
        for name, value in (
            ("message_limit", message_limit),
            ("changed_limit", changed_limit),
            ("output_limit", output_limit),
        ):
            if type(value) is not int or not 1 <= value <= 100:
                raise ValueError(f"{name} is outside allowed bounds")
        snapshot = self._registry.view_snapshot(operation_id)
        descriptor = snapshot.operation
        output = self._registry.output(
            operation_id,
            after_cursor=after_message_cursor,
            limits={"messages": message_limit},
        )
        changed_facts = snapshot.facts.changed_variables
        output_items = tuple(snapshot.facts.outputs.items())
        changed = changed_facts[:changed_limit]
        outputs = dict(output_items[:output_limit])
        return AgentOperationView(
            operation=AgentOperationIdentity(
                operation_id=descriptor.operation_id,
                kind=snapshot.kind,
                runtime_id=descriptor.runtime_id,
                runtime_generation=descriptor.runtime_generation,
                cell_id=descriptor.cell_id,
                revision=descriptor.revision,
                source_sha256=descriptor.source_sha256,
            ),
            state=descriptor.state,
            messages=output.messages,
            next_message_cursor=output.next_cursor,
            next_event_cursor=descriptor.event_cursor,
            changed_variables=changed,
            change_confidence=snapshot.facts.change_confidence,
            outputs=MappingProxyType(outputs),
            capture=snapshot.facts.capture,
            # Compact views use only the public operation journal. Private
            # excerpts/details are resolved exclusively by explain_failure.
            failure=snapshot.facts.failure,
            recovery=snapshot.facts.recovery,
            truncation=OperationTruncation(
                messages=output.has_more,
                changed_variables=len(changed_facts) > changed_limit,
                outputs=len(output_items) > output_limit,
            ),
            execution_provenance=snapshot.execution_provenance,
        )

__all__ = ["OperationViewProjector"]
