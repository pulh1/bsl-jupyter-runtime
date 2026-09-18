"""The public Session value signatures must retain their supported semantics."""

import pytest
from contextlib import nullcontext
from math import inf, nan
from threading import RLock
from types import SimpleNamespace

from onec_runtime.performance_profile import PhaseRecorder
from onec_runtime.table_materialization import ReferencePolicy
from onec_runtime.value_materialization import MaterializationOptions


class RecordingRouter:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def materialize(self, handle, options=None, *, table_policy=None, timeout_s=None):
        self.calls.append(("materialize", handle, options, table_policy))
        return {"value": 7}

    def to_df(self, handle, policy=None, *, max_rows, max_bytes):
        self.calls.append(("to_df", handle, policy, max_rows, max_bytes))
        return "frame"


def test_materialize_preserves_public_reference_and_value_limits() -> None:
    from onec_runtime.execution.session_value_adapter import SessionValueMaterializationAdapter

    router = RecordingRouter()
    adapter = SessionValueMaterializationAdapter(router)

    result = adapter.materialize(
        "e1cRuntimeКонтекст.Значение",
        refs="both",
        ref_columns={"Ссылка": "uuid"},
        uuid_suffix="_ид",
        max_depth=5,
        max_items=40,
        max_bytes=8192,
    )

    assert result == {"value": 7}
    assert router.calls == [(
        "materialize",
        "e1cRuntimeКонтекст.Значение",
        MaterializationOptions(refs="both", max_depth=5, max_items=40, max_bytes=8192),
        ReferencePolicy(refs="both", ref_columns={"Ссылка": "uuid"}, uuid_suffix="_ид"),
    )]


def test_to_df_applies_explicit_row_and_byte_budgets() -> None:
    from onec_runtime.execution.session_value_adapter import SessionValueMaterializationAdapter

    router = RecordingRouter()
    adapter = SessionValueMaterializationAdapter(router)

    assert adapter.to_df(
        "e1cRuntimeКонтекст.Таблица",
        refs="uuid",
        ref_columns={"Сотрудник": "both"},
        uuid_suffix="_uuid",
    ) == "frame"
    assert router.calls == [(
        "to_df",
        "e1cRuntimeКонтекст.Таблица",
        ReferencePolicy(refs="uuid", ref_columns={"Сотрудник": "both"}, uuid_suffix="_uuid"),
        100_000,
        64 * 1024 * 1024,
    )]


def test_materialize_value_keeps_proxy_alias_signature() -> None:
    from onec_runtime.execution.session_value_adapter import SessionValueMaterializationAdapter

    router = RecordingRouter()
    adapter = SessionValueMaterializationAdapter(router)

    assert adapter.materialize_value("e1cRuntimeКонтекст.X", max_items=3) == {"value": 7}
    assert router.calls == [(
        "materialize",
        "e1cRuntimeКонтекст.X",
        MaterializationOptions(max_items=3),
        ReferencePolicy(refs="presentation", ref_columns=None, uuid_suffix="__uuid"),
    )]


def test_timeout_is_forwarded_to_routed_materialization() -> None:
    from onec_runtime.execution.session_value_adapter import SessionValueMaterializationAdapter

    class TimeoutRouter(RecordingRouter):
        def materialize(self, handle, options=None, *, table_policy=None, timeout_s=None):
            self.calls.append(("timeout", timeout_s))
            return 7

    router = TimeoutRouter()
    adapter = SessionValueMaterializationAdapter(router)

    assert adapter.materialize("e1cRuntimeКонтекст.Значение", timeout_s=0.25) == 7
    assert router.calls == [("timeout", 0.25)]


@pytest.mark.parametrize("timeout_s", [0, -1, True, inf, nan, "2"])
def test_invalid_local_wait_timeout_fails_before_router(timeout_s) -> None:
    from onec_runtime.execution.session_value_adapter import SessionValueMaterializationAdapter

    router = RecordingRouter()
    adapter = SessionValueMaterializationAdapter(router)

    with pytest.raises(ValueError, match="timeout_s"):
        adapter.materialize("e1cRuntimeКонтекст.Значение", timeout_s=timeout_s)
    assert router.calls == []


@pytest.mark.parametrize("options", [
    {"refs": "opaque"},
    {"ref_columns": {"Ссылка": "opaque"}},
    {"ref_columns": {"": "uuid"}},
    {"uuid_suffix": ""},
])
def test_invalid_reference_options_fail_before_table_transfer(options) -> None:
    from onec_runtime.execution.session_value_adapter import SessionValueMaterializationAdapter

    router = RecordingRouter()
    adapter = SessionValueMaterializationAdapter(router)

    with pytest.raises((TypeError, ValueError)):
        adapter.to_df("e1cRuntimeКонтекст.Таблица", **options)
    assert router.calls == []


@pytest.mark.parametrize("method, expected_route", [
    ("to_df", "to_df"),
    ("materialize", "materialize"),
])
def test_current_session_default_chunk_size_is_advisory(method, expected_route) -> None:
    """RuntimeSession injects 2400; routed transfers accept this hint."""

    from onec_runtime.execution.public_facade import PublicExecutionFacade
    from onec_runtime.session import RuntimeSession
    from test_public_execution_facade import _Arbiter, _Controller, _Pipeline, unit

    router = RecordingRouter()
    api = PublicExecutionFacade(
        _Pipeline(), _Controller(), _Arbiter(),
        source_unit_factory=unit,
        status_reader=lambda: "status",
        value_router=router,
    )
    session = SimpleNamespace(
        _operation_lock=RLock(),
        config=SimpleNamespace(chunk_size=2400),
        runtime_api=api,
        validate_value_reference=lambda handle: handle,
        _capture_materialization_caller_handoff=nullcontext,
    )

    getattr(RuntimeSession, method)(session, "e1cRuntimeКонтекст.Таблица")
    assert len(router.calls) == 1
    assert router.calls[0][0] == expected_route


def test_runtime_session_value_calls_reach_composed_facade_adapter() -> None:
    """The public Session signatures must use the new facade's value route."""

    from onec_runtime.execution.public_facade import PublicExecutionFacade
    from onec_runtime.session import RuntimeSession
    from test_public_execution_facade import _Arbiter, _Controller, _Pipeline, unit

    router = RecordingRouter()
    api = PublicExecutionFacade(
        _Pipeline(), _Controller(), _Arbiter(),
        source_unit_factory=unit,
        status_reader=lambda: "status",
        value_router=router,
    )
    session = SimpleNamespace(
        _operation_lock=RLock(),
        config=SimpleNamespace(chunk_size=2400),
        runtime_api=api,
        validate_value_reference=lambda handle: handle,
        _capture_materialization_caller_handoff=nullcontext,
    )

    assert RuntimeSession.to_df(session, "e1cRuntimeКонтекст.Таблица", refs="uuid") == "frame"
    assert RuntimeSession.materialize(
        session, "e1cRuntimeКонтекст.Значение", max_items=3, timeout_s=0.25,
    ) == {"value": 7}
    assert router.calls[0][0] == "to_df"
    assert router.calls[0][2].refs == "uuid"
    assert router.calls[1][0] == "materialize"
    assert router.calls[1][2].max_items == 3

    with pytest.raises(ValueError, match="chunk_size"):
        RuntimeSession.to_df(session, "e1cRuntimeКонтекст.Таблица", chunk_size=0)
    with pytest.raises(ValueError, match="chunk_size"):
        RuntimeSession.materialize(session, "e1cRuntimeКонтекст.Значение", chunk_size=0)
    assert len(router.calls) == 2


@pytest.mark.parametrize("method", ["to_df", "materialize"])
@pytest.mark.parametrize("chunk_size", [0, -1, True, 1.5, "128"])
def test_chunk_hint_must_be_positive_integer(method, chunk_size) -> None:
    from onec_runtime.execution.session_value_adapter import SessionValueMaterializationAdapter

    router = RecordingRouter()
    adapter = SessionValueMaterializationAdapter(router)

    with pytest.raises(ValueError, match="chunk_size"):
        getattr(adapter, method)("e1cRuntimeКонтекст.Таблица", chunk_size=chunk_size)
    assert router.calls == []


@pytest.mark.parametrize("method", ["to_df", "materialize"])
def test_invalid_profiler_fails_before_routed_transfer(method) -> None:
    from onec_runtime.execution.session_value_adapter import SessionValueMaterializationAdapter

    router = RecordingRouter()
    adapter = SessionValueMaterializationAdapter(router)

    with pytest.raises(TypeError, match="profiler"):
        getattr(adapter, method)("e1cRuntimeКонтекст.Значение", profiler=object())
    assert router.calls == []


@pytest.mark.parametrize("method, phase", [
    ("to_df", "table.routed_transfer"),
    ("materialize", "materialization.routed_transfer"),
])
def test_profiler_records_routed_transfer_phase(method, phase) -> None:
    from onec_runtime.execution.session_value_adapter import SessionValueMaterializationAdapter

    router = RecordingRouter()
    adapter = SessionValueMaterializationAdapter(router)
    profiler = PhaseRecorder()

    getattr(adapter, method)("e1cRuntimeКонтекст.Значение", profiler=profiler)

    assert len(router.calls) == 1
    assert [event.phase for event in profiler.events] == [phase]
    assert profiler.events[0].error_present is False


def test_profiler_records_routed_failure_without_private_payload() -> None:
    from onec_runtime.execution.session_value_adapter import SessionValueMaterializationAdapter

    class FailingRouter(RecordingRouter):
        def materialize(self, handle, options=None, *, table_policy=None, timeout_s=None):
            raise RuntimeError("remote transfer failed")

    profiler = PhaseRecorder()
    adapter = SessionValueMaterializationAdapter(FailingRouter())

    with pytest.raises(RuntimeError, match="remote transfer failed"):
        adapter.materialize("e1cRuntimeКонтекст.Секрет", profiler=profiler)

    assert [event.phase for event in profiler.events] == ["materialization.routed_transfer"]
    assert profiler.events[0].error_present is True
    assert profiler.events[0].input_bytes == 0
    assert profiler.events[0].output_bytes == 0
