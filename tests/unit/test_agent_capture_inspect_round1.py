from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from onec_runtime.errors import ProtocolError
from onec_runtime_mcp.agent.capture_contracts import CaptureFence
from onec_runtime_mcp.agent.capture_service import CaptureService
from onec_runtime_mcp.agent.contracts import to_wire
from onec_runtime_mcp.agent.observation import (
    ManagerOrigin,
    ObservationItem,
    ObservationPlan,
    ObservationResult,
    ObservationSource,
    ObservationSourceKind,
    SelectionKind,
    ValueSelection,
)
from onec_runtime_mcp.agent.proxies import ProxyProvenance, ProxyRegistry
from onec_runtime_mcp.agent.runtime_backend import OnecRuntimeBackend


def fence(generation: int = 1) -> CaptureFence:
    return CaptureFence("intent", "operation", 3, "a" * 64, generation, generation)


class StrictFrameBackend:
    runtime_id = "private-runtime-id"

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def require_public_value_handle(self, handle: str) -> None:
        del handle

    def frame_variables(self, capture, *, filters, cursor, limit):
        self.calls.append(("variables", capture, filters, cursor, limit))
        return {
            "items": (
                {"name": "Amount", "type_name": "Number", "role": "local", "handle": "frame-amount"},
            ),
            "total": 1,
            "next_cursor": None,
        }

    def resolve_manager_origin(self, capture, origin):
        self.calls.append(("manager", capture, origin))
        return {
            "key": "manager-1",
            "handle": "native-manager",
            "type_name": "МенеджерВременныхТаблиц",
        }

    def temporary_tables(self, capture, manager_handle, *, names, cursor, limit, selection):
        self.calls.append(("tables", capture, manager_handle, names, cursor, limit, selection))
        return {
            "items": (
                {
                    "name": "Staff", "schema": ("Employee", "Date"),
                    "handle": "selected-staff" if selection is not None else "staff",
                },
            ),
            "next_cursor": None,
        }


def _publication_state(
    service: CaptureService, registry: ProxyRegistry
) -> tuple[object, ...]:
    """Capture every service/registry collection changed by capture.inspect."""
    return (
        dict(service._managers),
        dict(service._manager_by_origin),
        dict(service._manager_by_key),
        dict(service._frame_proxies),
        dict(service._table_proxies),
        dict(service._table_descriptors),
        dict(service._table_handles),
        tuple(registry._records),
        dict(registry._bindings),
        dict(registry._binding_versions),
    )


def test_fresh_or_forged_fence_never_activates_or_touches_backend(tmp_path) -> None:
    service, backend = CaptureService(tmp_path), StrictFrameBackend()

    with pytest.raises(Exception):
        service.inspect(
            backend, ProxyRegistry(), fence=fence(), runtime_id=backend.runtime_id,
            runtime_generation=1, context_generation=1, filters={}, cursor=0, limit=1,
        )

    assert backend.calls == []


def test_backend_frame_page_is_bounded_and_public_variable_has_usable_proxy_and_no_private_fence(tmp_path) -> None:
    service, backend, registry = CaptureService(tmp_path), StrictFrameBackend(), ProxyRegistry()
    service.activate_capture(fence(), registry)

    result = service.inspect(
        backend, registry, fence=fence(), runtime_id=backend.runtime_id,
        runtime_generation=1, context_generation=1, filters={"name": "Amount"}, cursor=0, limit=1,
    )

    assert backend.calls == [("variables", fence(), {"name": "Amount"}, 0, 1)]
    assert registry.resolve(result.variables[0].proxy_id).proxy_id == result.variables[0].proxy_id
    encoded = json.dumps(to_wire(result), ensure_ascii=False)
    assert "private-runtime-id" not in encoded
    assert "runtime_generation" not in encoded
    assert "context_generation" not in encoded
    assert "private-runtime-id" not in json.dumps(asdict(result), ensure_ascii=False)


def test_selected_table_head_columns_stays_a_proxy_and_uses_bounded_backend_selector(tmp_path) -> None:
    service, backend, registry = CaptureService(tmp_path), StrictFrameBackend(), ProxyRegistry()
    service.activate_capture(fence(), registry)
    manager_plan = ObservationPlan((ObservationItem(
        "manager", ObservationSource(ObservationSourceKind.TEMPORARY_TABLE_MANAGER,
            origin=ManagerOrigin("frame", "Query", ("Manager",))),
    ),))
    manager = service.inspect(
        backend, registry, fence=fence(), runtime_id=backend.runtime_id,
        runtime_generation=1, context_generation=1, filters={}, cursor=0, limit=1, observe=manager_plan,
    ).temporary_table_managers[0]
    plan = ObservationPlan((ObservationItem(
        "head", ObservationSource(ObservationSourceKind.TEMPORARY_TABLE, manager_id=manager.manager_id, table="Staff"),
        ObservationResult.PROXY,
        ValueSelection(SelectionKind.TABLE_ROWS, offset=0, limit=10, columns=("Employee",)),
    ),))

    result = service.inspect(
        backend, registry, fence=fence(), runtime_id=backend.runtime_id,
        runtime_generation=1, context_generation=1, filters={}, cursor=0, limit=1, observe=plan,
    )

    assert result.temporary_tables[0].table_id
    assert backend.calls[-1][-1] == ValueSelection(SelectionKind.TABLE_ROWS, offset=0, limit=10, columns=("Employee",))


def test_non_string_backend_handles_fail_before_proxy_publication(tmp_path) -> None:
    service, backend, registry = CaptureService(tmp_path), StrictFrameBackend(), ProxyRegistry()
    service.activate_capture(fence(), registry)
    backend.frame_variables = lambda *args, **kwargs: {  # type: ignore[method-assign]
        "items": ({"name": "Amount", "type_name": "Number", "handle": object()},), "total": 1, "next_cursor": None
    }

    with pytest.raises(ValueError, match="handle"):
        service.inspect(backend, registry, fence=fence(), runtime_id=backend.runtime_id,
            runtime_generation=1, context_generation=1, filters={}, cursor=0, limit=1)
    assert registry.current() == ()


def test_frame_batch_privacy_failure_publishes_nothing(tmp_path) -> None:
    """Safe item 0 cannot survive a forbidden resolver at item 1."""
    service, backend, registry = CaptureService(tmp_path), StrictFrameBackend(), ProxyRegistry()
    service.activate_capture(fence(), registry)
    backend.frame_variables = lambda *args, **kwargs: {  # type: ignore[method-assign]
        "items": (
            {"name": "Safe", "type_name": "Number", "handle": "safe-frame"},
            {"name": "Forbidden", "type_name": "Object", "handle": "worker-root"},
        ),
        "total": 2,
        "next_cursor": None,
    }

    def require_public(handle: str) -> None:
        if handle == "worker-root":
            raise ProtocolError("Worker generation objects are not public values")

    backend.require_public_value_handle = require_public  # type: ignore[method-assign]
    before = _publication_state(service, registry)

    with pytest.raises(
        ProtocolError,
        match="^Worker generation objects are not public values$",
    ):
        service.inspect(
            backend,
            registry,
            fence=fence(),
            runtime_id=backend.runtime_id,
            runtime_generation=1,
            context_generation=1,
            filters={},
            cursor=0,
            limit=2,
        )

    assert _publication_state(service, registry) == before
    assert registry.current() == ()


def test_capture_inspect_does_not_snapshot_unrelated_proxy_history(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Admission cost is proportional to this inspect batch, not registry history."""
    service, backend, registry = CaptureService(tmp_path), StrictFrameBackend(), ProxyRegistry()
    for index in range(256):
        registry.register_context(
            qualified_name=f"Context.Unrelated{index}",
            type_name="Number",
            runtime_id="runtime",
            runtime_generation=1,
            context_generation=1,
            provenance=ProxyProvenance("test", 1, "a" * 64, "operation"),
            resolver_handle=f"unrelated-{index}",
        )
    service.activate_capture(fence(), registry)

    def reject_historical_snapshot(_capture_fence: CaptureFence) -> object:
        raise AssertionError("capture.inspect scanned historical proxy state")

    monkeypatch.setattr(registry, "_capture_state_snapshot", reject_historical_snapshot)

    result = service.inspect(
        backend,
        registry,
        fence=fence(),
        runtime_id=backend.runtime_id,
        runtime_generation=1,
        context_generation=1,
        filters={},
        cursor=0,
        limit=1,
    )

    assert len(result.variables) == 1
    assert len(registry.current()) == 257


def test_proxy_registry_rejects_overlapping_capture_publication_before_mutation() -> None:
    registry = ProxyRegistry()
    existing = registry.register_context(
        qualified_name="bsl.Existing",
        type_name="Number",
        runtime_id="runtime",
        runtime_generation=1,
        context_generation=1,
        provenance=ProxyProvenance("test", 1, "a" * 64, "operation"),
        resolver_handle="existing",
    )
    first = registry._begin_capture_publication(fence())
    published = registry.register_frame(
        qualified_name="bsl.FrameA",
        type_name="Number",
        runtime_id="runtime",
        runtime_generation=1,
        context_generation=1,
        capture_fence=fence(),
        provenance=ProxyProvenance("test", 1, "a" * 64, "operation"),
        resolver_handle="frame-a",
        _publication=first,
    )
    before_overlap = (
        tuple(registry._records),
        dict(registry._bindings),
        dict(registry._binding_versions),
    )

    with pytest.raises(
        ProtocolError,
        match="^capture publication transaction is already active$",
    ):
        registry._begin_capture_publication(fence())

    assert (
        tuple(registry._records),
        dict(registry._bindings),
        dict(registry._binding_versions),
    ) == before_overlap
    registry._commit_capture_publication(first)
    assert registry.resolve(existing.proxy_id) == existing
    assert registry.resolve(published.proxy_id) == published
    next_publication = registry._begin_capture_publication(fence())
    registry._rollback_capture_publication(next_publication)


def test_public_capture_inspect_reentrancy_fails_before_inner_publication(
    tmp_path,
) -> None:
    service, backend, registry = CaptureService(tmp_path), StrictFrameBackend(), ProxyRegistry()
    service.activate_capture(fence(), registry)
    original_frame_variables = backend.frame_variables
    reentered = False

    def frame_variables(capture, *, filters, cursor, limit):
        nonlocal reentered
        if not reentered:
            reentered = True
            service.inspect(
                backend,
                registry,
                fence=fence(),
                runtime_id=backend.runtime_id,
                runtime_generation=1,
                context_generation=1,
                filters={},
                cursor=0,
                limit=1,
            )
        return original_frame_variables(
            capture, filters=filters, cursor=cursor, limit=limit
        )

    backend.frame_variables = frame_variables  # type: ignore[method-assign]
    before = _publication_state(service, registry)

    with pytest.raises(
        ProtocolError,
        match="^capture publication transaction is already active$",
    ):
        service.inspect(
            backend,
            registry,
            fence=fence(),
            runtime_id=backend.runtime_id,
            runtime_generation=1,
            context_generation=1,
            filters={},
            cursor=0,
            limit=1,
        )

    assert _publication_state(service, registry) == before


def test_capture_publication_journal_never_scans_registry_mappings(tmp_path) -> None:
    class CountingDict(dict):
        def __init__(self, values: dict) -> None:
            super().__init__(values)
            self.scans = 0

        def __iter__(self):  # type: ignore[no-untyped-def]
            self.scans += 1
            return super().__iter__()

        def items(self):  # type: ignore[no-untyped-def]
            self.scans += 1
            return super().items()

        def keys(self):  # type: ignore[no-untyped-def]
            self.scans += 1
            return super().keys()

        def values(self):  # type: ignore[no-untyped-def]
            self.scans += 1
            return super().values()

        def copy(self):  # type: ignore[no-untyped-def]
            self.scans += 1
            return super().copy()

    service = CaptureService(tmp_path)
    backend = StrictFrameBackend()
    registry = ProxyRegistry()
    for index in range(256):
        registry.register_context(
            qualified_name=f"bsl.Unrelated{index}",
            type_name="Number",
            runtime_id="runtime",
            runtime_generation=1,
            context_generation=1,
            provenance=ProxyProvenance("test", 1, "a" * 64, "operation"),
            resolver_handle=f"unrelated-{index}",
        )
    service.activate_capture(fence(), registry)
    records = CountingDict(registry._records)
    bindings = CountingDict(registry._bindings)
    versions = CountingDict(registry._binding_versions)
    registry._records = records
    registry._bindings = bindings
    registry._binding_versions = versions

    result = service.inspect(
        backend,
        registry,
        fence=fence(),
        runtime_id=backend.runtime_id,
        runtime_generation=1,
        context_generation=1,
        filters={},
        cursor=0,
        limit=1,
    )

    assert len(result.variables) == 1
    assert records.scans == bindings.scans == versions.scans == 0


def test_manager_batch_privacy_failure_rolls_back_earlier_manager(tmp_path) -> None:
    service, backend, registry = CaptureService(tmp_path), StrictFrameBackend(), ProxyRegistry()
    service.activate_capture(fence(), registry)
    backend.frame_variables = lambda *args, **kwargs: {  # type: ignore[method-assign]
        "items": (), "total": 0, "next_cursor": None
    }

    def resolve_manager(_capture: object, origin: ManagerOrigin) -> dict[str, object]:
        name = origin.fields[-1]
        return {
            "key": f"manager-{name}",
            "handle": "worker-module" if name == "Forbidden" else "safe-manager",
            "type_name": "МенеджерВременныхТаблиц",
        }

    def require_public(handle: str) -> None:
        if handle == "worker-module":
            raise ProtocolError("Worker generation objects are not public values")

    backend.resolve_manager_origin = resolve_manager  # type: ignore[method-assign]
    backend.require_public_value_handle = require_public  # type: ignore[method-assign]
    plan = ObservationPlan(
        (
            ObservationItem(
                "safe",
                ObservationSource(
                    ObservationSourceKind.TEMPORARY_TABLE_MANAGER,
                    origin=ManagerOrigin("frame", "Query", ("Safe",)),
                ),
            ),
            ObservationItem(
                "forbidden",
                ObservationSource(
                    ObservationSourceKind.TEMPORARY_TABLE_MANAGER,
                    origin=ManagerOrigin("frame", "Query", ("Forbidden",)),
                ),
            ),
        )
    )
    before = _publication_state(service, registry)

    with pytest.raises(
        ProtocolError,
        match="^Worker generation objects are not public values$",
    ):
        service.inspect(
            backend,
            registry,
            fence=fence(),
            runtime_id=backend.runtime_id,
            runtime_generation=1,
            context_generation=1,
            filters={},
            cursor=0,
            limit=2,
            observe=plan,
        )

    assert _publication_state(service, registry) == before


def test_table_batch_privacy_failure_rolls_back_earlier_table_proxy(tmp_path) -> None:
    service, backend, registry = CaptureService(tmp_path), StrictFrameBackend(), ProxyRegistry()
    service.activate_capture(fence(), registry)
    manager_plan = ObservationPlan((ObservationItem(
        "manager", ObservationSource(ObservationSourceKind.TEMPORARY_TABLE_MANAGER,
            origin=ManagerOrigin("frame", "Query", ("Manager",))),
    ),))
    manager = service.inspect(
        backend, registry, fence=fence(), runtime_id=backend.runtime_id,
        runtime_generation=1, context_generation=1, filters={}, cursor=0, limit=2,
        observe=manager_plan,
    ).temporary_table_managers[0]
    backend.temporary_tables = lambda *args, **kwargs: {  # type: ignore[method-assign]
        "items": (
            {"name": "Staff", "schema": ("Employee",), "handle": "safe-table"},
            {"name": "Staff", "schema": ("Employee",), "handle": "worker-export"},
        ),
        "total": 2,
        "next_cursor": None,
    }

    def require_public(handle: str) -> None:
        if handle == "worker-export":
            raise ProtocolError("Worker generation objects are not public values")

    backend.require_public_value_handle = require_public  # type: ignore[method-assign]
    table_plan = ObservationPlan((ObservationItem(
        "staff", ObservationSource(
            ObservationSourceKind.TEMPORARY_TABLE,
            manager_id=manager.manager_id,
            table="Staff",
        ),
    ),))
    before = _publication_state(service, registry)

    with pytest.raises(
        ProtocolError,
        match="^Worker generation objects are not public values$",
    ):
        service.inspect(
            backend,
            registry,
            fence=fence(),
            runtime_id=backend.runtime_id,
            runtime_generation=1,
            context_generation=1,
            filters={},
            cursor=0,
            limit=2,
            observe=table_plan,
        )

    assert _publication_state(service, registry) == before


def test_new_capture_or_runtime_close_invalidates_the_old_fence_before_backend_access(tmp_path) -> None:
    service, backend, registry = CaptureService(tmp_path), StrictFrameBackend(), ProxyRegistry()
    first_fence, next_fence = fence(), fence(2)
    service.activate_capture(first_fence, registry)
    first = service.inspect(
        backend, registry, fence=first_fence, runtime_id=backend.runtime_id,
        runtime_generation=1, context_generation=1, filters={}, cursor=0, limit=1,
    )
    service.activate_capture(next_fence, registry)

    with pytest.raises(Exception):
        service.inspect(
            backend, registry, fence=first_fence, runtime_id=backend.runtime_id,
            runtime_generation=1, context_generation=1, filters={}, cursor=0, limit=1,
        )
    with pytest.raises(Exception):
        registry.resolve(first.variables[0].proxy_id)

    calls_before_close = len(backend.calls)
    service.invalidate_capture(registry)
    with pytest.raises(Exception):
        service.inspect(
            backend, registry, fence=next_fence, runtime_id=backend.runtime_id,
            runtime_generation=1, context_generation=1, filters={}, cursor=0, limit=1,
        )
    assert len(backend.calls) == calls_before_close


def test_duplicate_backend_table_aliases_publish_one_table_descriptor(tmp_path) -> None:
    service, backend, registry = CaptureService(tmp_path), StrictFrameBackend(), ProxyRegistry()
    service.activate_capture(fence(), registry)
    backend.temporary_tables = lambda *args, **kwargs: {  # type: ignore[method-assign]
        "items": (
            {"name": "Staff", "schema": ("Employee",), "handle": "staff"},
            {"name": "Staff", "schema": ("Employee",), "handle": "staff"},
        ),
        "total": 2,
        "next_cursor": None,
    }
    manager_plan = ObservationPlan((ObservationItem(
        "manager", ObservationSource(ObservationSourceKind.TEMPORARY_TABLE_MANAGER,
            origin=ManagerOrigin("frame", "Query", ("Manager",))),
    ),))
    manager = service.inspect(
        backend, registry, fence=fence(), runtime_id=backend.runtime_id,
        runtime_generation=1, context_generation=1, filters={}, cursor=0, limit=2, observe=manager_plan,
    ).temporary_table_managers[0]
    table_plan = ObservationPlan((ObservationItem(
        "staff", ObservationSource(ObservationSourceKind.TEMPORARY_TABLE, manager_id=manager.manager_id, table="Staff"),
    ),))

    result = service.inspect(
        backend, registry, fence=fence(), runtime_id=backend.runtime_id,
        runtime_generation=1, context_generation=1, filters={}, cursor=0, limit=2, observe=table_plan,
    )

    assert len(result.temporary_tables) == 1


def test_table_proxy_identity_keeps_full_and_bounded_selection_handles_distinct(tmp_path) -> None:
    service, backend, registry = CaptureService(tmp_path), StrictFrameBackend(), ProxyRegistry()
    service.activate_capture(fence(), registry)
    manager_plan = ObservationPlan((ObservationItem(
        "manager", ObservationSource(ObservationSourceKind.TEMPORARY_TABLE_MANAGER,
            origin=ManagerOrigin("frame", "Query", ("Manager",))),
    ),))
    manager = service.inspect(
        backend, registry, fence=fence(), runtime_id=backend.runtime_id,
        runtime_generation=1, context_generation=1, filters={}, cursor=0, limit=10, observe=manager_plan,
    ).temporary_table_managers[0]
    full_plan = ObservationPlan((ObservationItem(
        "full", ObservationSource(ObservationSourceKind.TEMPORARY_TABLE, manager_id=manager.manager_id, table="Staff"),
    ),))
    selected = ValueSelection(SelectionKind.TABLE_ROWS, offset=0, limit=10, columns=("Employee",))
    head_plan = ObservationPlan((ObservationItem(
        "head", ObservationSource(ObservationSourceKind.TEMPORARY_TABLE, manager_id=manager.manager_id, table="Staff"),
        select=selected,
    ),))

    full = service.inspect(backend, registry, fence=fence(), runtime_id=backend.runtime_id,
        runtime_generation=1, context_generation=1, filters={}, cursor=0, limit=10, observe=full_plan).temporary_tables[0]
    head = service.inspect(backend, registry, fence=fence(), runtime_id=backend.runtime_id,
        runtime_generation=1, context_generation=1, filters={}, cursor=0, limit=10, observe=head_plan).temporary_tables[0]
    same_head = service.inspect(backend, registry, fence=fence(), runtime_id=backend.runtime_id,
        runtime_generation=1, context_generation=1, filters={}, cursor=0, limit=10, observe=head_plan).temporary_tables[0]

    assert full.table_id != head.table_id
    assert registry.resolver_handle(full.table_id) == "staff"
    assert registry.resolver_handle(head.table_id) == "selected-staff"
    assert same_head.table_id == head.table_id
    assert sum(call[0] == "tables" for call in backend.calls) == 2


def test_production_backend_delegates_strict_bounded_frame_seams() -> None:
    class Session:
        def frame_variables(self, capture, *, filters, cursor, limit):
            assert filters == {"name": "Amount"} and cursor == 0 and limit == 1
            return {"items": (), "total": 0, "next_cursor": None}

        def resolve_manager_origin(self, capture, origin):
            return {
                "key": "manager",
                "handle": "manager",
                "type_name": "МенеджерВременныхТаблиц",
            }

        def temporary_tables(self, capture, manager_handle, *, names, cursor, limit, selection):
            assert manager_handle == "manager" and names == ("Staff",) and limit == 1
            return {"items": (), "next_cursor": None}

    backend = OnecRuntimeBackend("runtime", Session())  # type: ignore[arg-type]

    assert backend.frame_variables(fence(), filters={"name": "Amount"}, cursor=0, limit=1)["total"] == 0
    assert backend.resolve_manager_origin(fence(), object())["handle"] == "manager"
    assert backend.temporary_tables(fence(), "manager", names=("Staff",), cursor=0, limit=1, selection=None)["items"] == ()
