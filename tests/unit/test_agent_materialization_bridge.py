from __future__ import annotations

import json
from pathlib import Path

import pytest

from onec_runtime_mcp.agent.contracts import AgentOperationState, OperationDescriptor
from onec_runtime_mcp.agent.onec_values import OnecValueResolver, publish_onec_bindings
from onec_runtime_mcp.agent.observation import SelectionKind, ValueSelection
from onec_runtime_mcp.agent.proxies import (
    ProxyRealm,
    ProxyRegistry,
    ValueBudget,
)
from onec_runtime_mcp.agent.python_protocol import PythonWorkspaceLimits
from onec_runtime_mcp.agent.python_workspace import PythonWorkspace
from onec_runtime_mcp.agent.value_service import OnecMaterializationBridge
from onec_runtime.errors import ProtocolError
from onec_runtime.runtime_api import RuntimeNamespaceSnapshot


def table_payload(*, rows: int = 10_000) -> bytes:
    columns = [f"Column{index}" for index in range(15)]
    schema = {
        "version": 1,
        "columns": columns,
        "kinds": ["integer"] * 15,
        "reference_modes": {},
    }
    return "".join(
        json.dumps(record, separators=(",", ":")) + "\n"
        for record in (schema, *([index] * 15 for index in range(rows)))
    ).encode()


def value_payload(root: object) -> bytes:
    return json.dumps(
        {"version": 1, "root": root},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()


class FakePayloadBackend:
    runtime_id = "runtime-1"
    is_closed = False

    def __init__(self, payloads: dict[str, tuple[str, bytes]]) -> None:
        self.payloads = payloads
        self.value_calls: list[str] = []
        self.table_calls: list[tuple[str, dict[str, object]]] = []
        self.project_calls: list[tuple[str, ValueSelection, dict[str, object]]] = []

    def namespace_snapshot(self) -> RuntimeNamespaceSnapshot:
        return RuntimeNamespaceSnapshot(7, 12, tuple(self.payloads))

    def validate_value_reference(self, handle: str) -> None:
        del handle

    def materialization_kind(
        self, handle: str, *, timeout_s: float | None = None
    ) -> str:
        del timeout_s
        return self.payloads[handle.removeprefix("e1cRuntimeКонтекст.")][0]

    def materialize_value_payload(self, handle: str, **options: object) -> bytes:
        self.value_calls.append(handle)
        return self.payloads[handle.removeprefix("e1cRuntimeКонтекст.")][1]

    def materialize_table_payload(self, handle: str, **options: object) -> bytes:
        self.table_calls.append((handle, options))
        return self.payloads[handle.removeprefix("e1cRuntimeКонтекст.")][1]

    def project_value_payload(
        self, handle: str, selection: ValueSelection, **options: object
    ) -> tuple[str, bytes]:
        self.project_calls.append((handle, selection, options))
        return "value", value_payload(
            {"t": "array", "v": [{"t": "number", "v": "1"}]}
        )

    def materialize_value(self, handle: str, **options: object) -> object:
        raise AssertionError(f"service-side value decode is forbidden: {handle} {options}")

    def materialize_table(self, handle: str, **options: object) -> object:
        raise AssertionError(f"service-side DataFrame build is forbidden: {handle} {options}")


def operation() -> OperationDescriptor:
    return OperationDescriptor(
        operation_id="op-main",
        state=AgentOperationState.COMPLETED,
        runtime_id="runtime-1",
        runtime_generation=7,
        cell_id="cell-main",
        revision=2,
        source_sha256="a" * 64,
    )


def workspace_limits() -> PythonWorkspaceLimits:
    return PythonWorkspaceLimits(
        timeout_seconds=10,
        max_code_bytes=64 * 1024,
        max_request_bytes=256 * 1024,
        max_response_bytes=256 * 1024,
        max_stdout_bytes=4096,
        max_stderr_bytes=4096,
        max_variables=100,
        allowed_imports=("pandas", "numpy"),
    )


def budget(**changes: object) -> ValueBudget:
    values: dict[str, object] = {
        "max_depth": 16,
        "max_items": 200_000,
        "max_rows": 20_000,
        "max_bytes": 16 * 1024 * 1024,
        "timeout_seconds": 10,
    }
    values.update(changes)
    return ValueBudget(**values)  # type: ignore[arg-type]


def seeded_bridge(
    tmp_path: Path,
    payloads: dict[str, tuple[str, bytes]],
) -> tuple[OnecMaterializationBridge, dict[str, object], PythonWorkspace, FakePayloadBackend]:
    registry = ProxyRegistry()
    backend = FakePayloadBackend(payloads)
    proxies = publish_onec_bindings(backend, registry, operation())
    by_name = {item.qualified_name.removeprefix("bsl."): item for item in proxies}
    python = PythonWorkspace.start(
        tmp_path,
        tmp_path / ".runtime",
        workspace_limits(),
        registry=registry,
    )
    return OnecMaterializationBridge(OnecValueResolver(backend, registry), python), by_name, python, backend


def test_to_df_returns_python_proxy_and_preserves_onec_provenance(
    tmp_path: Path,
) -> None:
    bridge, proxies, python, backend = seeded_bridge(
        tmp_path, {"Таблица": ("table", table_payload())}
    )
    try:
        onec_proxy = proxies["Таблица"]
        frame_proxy = bridge.to_df(
            onec_proxy,
            columns=None,
            refs="both",
            budget=budget(),
        )

        assert frame_proxy.realm is ProxyRealm.PYTHON
        assert frame_proxy.type_name == "pandas.DataFrame"
        assert frame_proxy.provenance.parent_proxy_ids == (onec_proxy.proxy_id,)
        assert python.inspect(frame_proxy.proxy_id).shape == (10_000, 15)
        assert len(backend.table_calls) == 1
        handle, options = backend.table_calls[0]
        assert handle == "e1cRuntimeКонтекст.Таблица"
        timeout_s = options.pop("timeout_s")
        assert isinstance(timeout_s, float) and 0 < timeout_s <= 10
        assert options == {
            "refs": "both",
            "ref_columns": None,
            "uuid_suffix": "__uuid",
            "max_rows": 20_000,
            "max_bytes": 16 * 1024 * 1024,
        }
        assert backend.value_calls == []
    finally:
        python.close()


def test_compact_transfer_and_python_decode_share_one_monotonic_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Break caught: the 1C compact transfer and child Python decode each used
    # a fresh copy of the full budget, yielding two independent timeouts.
    class Clock:
        value = 500.0

        def __call__(self) -> float:
            return self.value

        def advance(self, seconds: float) -> None:
            self.value += seconds

    clock = Clock()
    import onec_runtime_mcp.agent.value_service as value_service_module

    monkeypatch.setattr(value_service_module, "monotonic", clock, raising=False)
    bridge, proxies, python, backend = seeded_bridge(
        tmp_path, {"Таблица": ("table", table_payload(rows=1))}
    )
    original_transfer = backend.materialize_table_payload
    original_ingest = python.ingest_typed_payload
    decode_timeouts: list[float] = []

    def delayed_transfer(handle: str, **options: object) -> bytes:
        payload = original_transfer(handle, **options)
        clock.advance(4.0)
        return payload

    def recording_ingest(payload: bytes, **options: object):  # type: ignore[no-untyped-def]
        decode_budget = options["budget"]
        decode_timeouts.append(decode_budget.timeout_seconds)
        return original_ingest(payload, **options)

    monkeypatch.setattr(backend, "materialize_table_payload", delayed_transfer)
    monkeypatch.setattr(python, "ingest_typed_payload", recording_ingest)
    try:
        result = bridge.to_df(
            proxies["Таблица"],
            columns=None,
            refs="presentation",
            budget=budget(timeout_seconds=10),
        )

        assert result.realm is ProxyRealm.PYTHON
        assert backend.table_calls[0][1]["timeout_s"] == 10
        assert len(decode_timeouts) == 1
        assert 0 < decode_timeouts[0] <= 6
    finally:
        python.close()


def test_recursive_value_is_decoded_in_worker_and_can_feed_derived_python(
    tmp_path: Path,
) -> None:
    recursive = value_payload(
        {
            "t": "array",
            "v": [
                {"t": "null"},
                {"t": "fixed_array", "v": [{"t": "string", "v": "x"}]},
                {
                    "t": "structure",
                    "v": [["Amount", {"t": "number", "v": "12.5"}]],
                },
                {
                    "t": "reference",
                    "type": "СправочникСсылка.Сотрудники",
                    "uuid": "12345678-1234-5678-1234-567812345678",
                    "presentation": "Иванов",
                    "empty": False,
                },
                {
                    "t": "enum",
                    "type": "ПеречислениеСсылка.Вид",
                    "name": "Основной",
                    "presentation": "Основной",
                },
                {"t": "binary", "v": "AAEC"},
            ],
        }
    )
    bridge, proxies, python, _ = seeded_bridge(
        tmp_path, {"Значение": ("value", recursive)}
    )
    try:
        onec_proxy = proxies["Значение"]
        snapshot = bridge.materialize(
            onec_proxy,
            target="python",
            policy={"refs": "both"},
            budget=budget(),
        )
        derived = python.run(
            "count = len(source)",
            inputs={"source": snapshot},
            outputs=("count",),
        ).outputs["count"]

        assert python.inspect(snapshot.proxy_id).type_name == "builtins.list"
        assert python.inspect(derived.proxy_id).preview == 6
        assert snapshot.provenance.parent_proxy_ids == (onec_proxy.proxy_id,)
    finally:
        python.close()


def test_bounded_projection_is_built_in_1c_before_python_ingest(tmp_path: Path) -> None:
    bridge, proxies, python, backend = seeded_bridge(
        tmp_path, {"Значение": ("value", value_payload({"t": "array", "v": []}))}
    )
    selected_budget = budget(max_items=100, max_rows=100)
    selection = ValueSelection(SelectionKind.SLICE, offset=10, limit=5)
    try:
        projected = bridge.project(
            proxies["Значение"], selection, budget=selected_budget
        )

        assert projected.realm is ProxyRealm.PYTHON
        assert python.inspect(projected.proxy_id).type_name == "builtins.list"
        count = python.run(
            "count = len(source)",
            inputs={"source": projected},
            outputs=("count",),
        ).outputs["count"]
        assert python.inspect(count.proxy_id).preview == 1
        assert len(backend.project_calls) == 1
        handle, actual_selection, options = backend.project_calls[0]
        assert handle == "e1cRuntimeКонтекст.Значение"
        assert actual_selection == selection
        timeout_s = options.pop("timeout_s")
        assert isinstance(timeout_s, float)
        assert 0 < timeout_s <= selected_budget.timeout_seconds
        assert options == {
            "max_depth": selected_budget.max_depth,
            "max_items": selected_budget.max_items,
            "max_rows": selected_budget.max_rows,
            "max_bytes": selected_budget.max_bytes,
        }
    finally:
        python.close()


def test_row_and_byte_budgets_fail_without_leaving_staged_payload(
    tmp_path: Path,
) -> None:
    bridge, proxies, python, _ = seeded_bridge(
        tmp_path, {"Таблица": ("table", table_payload(rows=3))}
    )
    try:
        with pytest.raises(ValueError, match="budget"):
            bridge.to_df(
                proxies["Таблица"],
                columns=None,
                refs="uuid",
                budget=budget(max_rows=2),
            )
        with pytest.raises(ValueError, match="byte budget"):
            bridge.to_df(
                proxies["Таблица"],
                columns=None,
                refs="uuid",
                budget=budget(max_bytes=8),
            )
        assert list((tmp_path / ".runtime" / "python-transfers").iterdir()) == []
    finally:
        python.close()


def test_corrupt_typed_payload_fails_closed_and_cleans_transfer_file(
    tmp_path: Path,
) -> None:
    bridge, proxies, python, _ = seeded_bridge(
        tmp_path, {"Значение": ("value", b"not-json")}
    )
    try:
        with pytest.raises(ProtocolError, match="rejected typed payload"):
            bridge.materialize(
                proxies["Значение"],
                target="python",
                policy={},
                budget=budget(),
            )
        assert list((tmp_path / ".runtime" / "python-transfers").iterdir()) == []
    finally:
        python.close()
