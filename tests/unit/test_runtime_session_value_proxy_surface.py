from __future__ import annotations

from contextlib import contextmanager
from threading import RLock
from types import SimpleNamespace

import pytest

from onec_runtime.errors import ProtocolError
from onec_runtime.bsl import (
    CommonModuleCatalogSnapshot,
    CommonModuleDescriptor,
    CommonModuleScope,
    SourceUnitKind,
    SourceUnitRef,
    WorkerModuleUnit,
    mapped_visible_source,
    source_sha256,
)
from onec_runtime.session import RuntimeSession
from onec_runtime.worker_breakpoints import WorkerBreakpointReloadPolicy
from onec_runtime.worker_universe import WorkerGenerationHandle


class FakeRuntimeApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object, dict[str, object]]] = []
        self.guard_calls: list[str] = []
        self.forbidden_handles: set[str] = set()
        self.execution_handoff_factories: list[object] = []
        self.handoff_entries = 0
        self._caller_handoff_factory = None
        self.handle = WorkerGenerationHandle(1, 1, 1, "a" * 64)
        self.active_units: dict[str, WorkerModuleUnit] = {}

    def validate_value_reference(self, handle: str) -> None:
        self.guard_calls.append(handle)
        if handle in self.forbidden_handles or handle.casefold().startswith(
            (
                "e1cruntimeконтекст.runtimeworkeractivegeneration",
                "__onecpinnedworkergeneration",
            )
        ):
            raise ProtocolError("Worker generation objects are not public values")

    @contextmanager
    def execution_caller_handoff(self, factory):  # type: ignore[no-untyped-def]
        self.execution_handoff_factories.append(factory)
        self._caller_handoff_factory = factory
        try:
            yield
        finally:
            self._caller_handoff_factory = None

    def _record_value_call(
        self, name: str, handle: str, selection: object, options: dict[str, object],
    ) -> None:
        factory = self._caller_handoff_factory
        assert factory is not None
        with factory():
            self.handoff_entries += 1
        self.calls.append((name, handle, selection, options))

    def materialize_session_table(self, handle: str, **options: object) -> object:
        self._record_value_call("materialize_session_table", handle, None, options)
        return "table"

    def materialize_session_value(self, handle: str, **options: object) -> object:
        self._record_value_call("materialize_session_value", handle, None, options)
        return {"value": 5}

    def project_value(
        self, handle: str, selection: dict[str, object], **options: object
    ) -> object:
        self._record_value_call("project_value", handle, selection, options)
        return [3, 5]

    def project_to_df(
        self, handle: str, selection: dict[str, object], **options: object
    ) -> object:
        self._record_value_call("project_to_df", handle, selection, options)
        return "frame"

    def materialization_kind(self, handle: str, **options: object) -> str:
        self._record_value_call("materialization_kind", handle, None, options)
        return "table"

    def materialize_value_payload(self, handle: str, **options: object) -> bytes:
        self._record_value_call("materialize_value_payload", handle, None, options)
        return b"value"

    def materialize_table_payload(self, handle: str, **options: object) -> bytes:
        self._record_value_call("materialize_table_payload", handle, None, options)
        return b"table"

    def project_value_payload(self, handle: str, **options: object) -> tuple[str, bytes]:
        self._record_value_call("project_value_payload", handle, None, options)
        return "value", b"projection"

    def load_worker_modules(
        self,
        units: tuple[WorkerModuleUnit, ...],
        *,
        common_modules: object,
        breakpoint_policy: object = None,
        profiler: object = None,
    ) -> WorkerGenerationHandle:
        self.calls.append(
            (
                "load_worker_modules",
                "",
                units,
                {
                    "common_modules": common_modules,
                    "breakpoint_policy": breakpoint_policy,
                    "profiler": profiler,
                },
            )
        )
        self.active_units.update((unit.logical_name.casefold(), unit) for unit in units)
        return self.handle

    def confirmed_worker_module_units(
        self, handle: WorkerGenerationHandle,
    ) -> tuple[WorkerModuleUnit, ...]:
        assert handle is self.handle and self.active_units
        return tuple(self.active_units[name] for name in sorted(self.active_units))

    def release_worker_generation(self, handle: object) -> None:
        self.calls.append(("release_worker_generation", "", handle, {}))


def _session(api: FakeRuntimeApi) -> RuntimeSession:
    session = object.__new__(RuntimeSession)
    session.runtime_api = api
    session._operation_lock = RLock()
    session.config = SimpleNamespace(chunk_size=128)
    session._active_worker_file_units = {}
    return session


def test_runtime_session_exposes_value_proxy_materialization_surface() -> None:
    api = FakeRuntimeApi()
    session = _session(api)

    assert session.materialize_value(
        "e1cRuntimeКонтекст.Счетчик", max_depth=8, max_items=32, max_bytes=65536
    ) == {"value": 5}
    assert session.project_value(
        "e1cRuntimeКонтекст.Числа",
        {"offset": 0, "limit": 2},
        max_depth=8,
        max_items=32,
        max_bytes=65536,
    ) == [3, 5]
    assert session.project_to_df(
        "e1cRuntimeКонтекст.Таблица",
        {"offset": 0, "limit": 2},
        chunk_size=128,
    ) == "frame"

    assert [call[0] for call in api.calls] == [
        "materialize_session_value",
        "project_value",
        "project_to_df",
    ]
    assert api.calls[0][1] == "e1cRuntimeКонтекст.Счетчик"
    assert api.calls[1][2] == {"offset": 0, "limit": 2}
    assert api.calls[2][2] == {"offset": 0, "limit": 2}
    assert len(api.execution_handoff_factories) == 3
    assert api.handoff_entries == 3


@pytest.mark.parametrize(
    ("route", "invoke"),
    (
        ("to_df", lambda session: session.to_df("e1cRuntimeКонтекст.Таблица")),
        (
            "project_to_df",
            lambda session: session.project_to_df(
                "e1cRuntimeКонтекст.Таблица", {"offset": 0, "limit": 1}
            ),
        ),
        ("materialize", lambda session: session.materialize("e1cRuntimeКонтекст.Значение")),
        (
            "materialize_value",
            lambda session: session.materialize_value("e1cRuntimeКонтекст.Значение"),
        ),
        (
            "project_value",
            lambda session: session.project_value(
                "e1cRuntimeКонтекст.Значение", {"offset": 0, "limit": 1}
            ),
        ),
        (
            "materialization_kind",
            lambda session: session.materialization_kind("e1cRuntimeКонтекст.Таблица"),
        ),
        (
            "materialize_value_payload",
            lambda session: session.materialize_value_payload("e1cRuntimeКонтекст.Значение"),
        ),
        (
            "materialize_table_payload",
            lambda session: session.materialize_table_payload("e1cRuntimeКонтекст.Таблица"),
        ),
        (
            "project_value_payload",
            lambda session: session.project_value_payload(
                "e1cRuntimeКонтекст.Значение",
                SimpleNamespace(
                    kind=SimpleNamespace(value="slice"),
                    offset=0,
                    limit=1,
                    columns=(),
                    names=(),
                ),
            ),
        ),
    ),
)
def test_every_session_materialization_route_binds_capture_waiter(
    route: str,
    invoke,
) -> None:  # type: ignore[no-untyped-def]
    api = FakeRuntimeApi()
    session = _session(api)

    invoke(session)

    expected_call = {
        "to_df": "materialize_session_table",
        "materialize": "materialize_session_value",
        "materialize_value": "materialize_session_value",
    }.get(route, route)
    assert [call[0] for call in api.calls] == [expected_call]
    assert len(api.execution_handoff_factories) == 1
    assert api.handoff_entries == 1
    # The synthetic API enters the wait callback during the route call, then
    # returns with the Session operation lock restored.
    session.validate_value_reference("e1cRuntimeКонтекст.ПовторнаяПроверка")


def test_runtime_session_forwards_worker_universe_descriptors() -> None:
    api = FakeRuntimeApi()
    session = _session(api)
    source = "Функция Версия() Экспорт\n    Возврат 1;\nКонецФункции\n"
    reference = SourceUnitRef(
        SourceUnitKind.MODULE,
        "МодульА",
        1,
        source_sha256(source),
    )
    units = (
        WorkerModuleUnit(
            "МодульА",
            "module",
            1,
            mapped_visible_source(source, reference),
        ),
    )
    catalog = CommonModuleCatalogSnapshot.create(
        profile="runtime-session-server-v1",
        preprocessor_profile="server",
        revision=1,
        modules=(CommonModuleDescriptor("МодульА", CommonModuleScope.SERVER),),
    )
    catalog_manager = SimpleNamespace(
        ensure_modules=lambda names: catalog
    )
    session._common_module_catalog = catalog_manager

    assert session.load_worker_modules(units) is api.handle
    session.release_worker_generation(api.handle)

    assert api.calls == [
        (
            "load_worker_modules",
            "",
            units,
            {
                "common_modules": catalog_manager,
                "breakpoint_policy": WorkerBreakpointReloadPolicy.STRICT,
                "profiler": None,
            },
        ),
        ("release_worker_generation", "", api.handle, {}),
    ]


@pytest.mark.parametrize(
    ("method", "arguments"),
    (
        ("materialize_value", ("e1cRuntimeКонтекст.RuntimeWorkerActiveGeneration",)),
        (
            "project_value",
            (
                "e1cRuntimeКонтекст.RuntimeWorkerActiveGeneration.Modules.МодульА",
                {"offset": 0, "limit": 1},
            ),
        ),
        (
            "project_to_df",
            (
                "__OnecPinnedWorkerGeneration.Modules.МодульА",
                {"offset": 0, "limit": 1},
            ),
        ),
    ),
)
def test_runtime_session_rejects_worker_generation_objects_before_proxy_backend(
    method: str,
    arguments: tuple[object, ...],
) -> None:
    """Break caught: active roots/modules must never become public proxy values."""
    api = FakeRuntimeApi()
    session = _session(api)

    with pytest.raises(
        ProtocolError,
        match="^Worker generation objects are not public values$",
    ):
        getattr(session, method)(*arguments)

    assert api.calls == []


def test_runtime_session_uses_runtime_identity_guard_for_worker_alias() -> None:
    api = FakeRuntimeApi()
    api.forbidden_handles.add("e1cRuntimeКонтекст.АлиасМодуля")
    session = _session(api)

    with pytest.raises(
        ProtocolError,
        match="^Worker generation objects are not public values$",
    ):
        session.materialize_value("e1cRuntimeКонтекст.АлиасМодуля")

    assert api.guard_calls == ["e1cRuntimeКонтекст.АлиасМодуля"]
    assert api.calls == []
