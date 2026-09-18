"""Post-bootstrap execution composition stays separate from RuntimeSession.start."""

import pytest
from base64 import b64encode
from decimal import Decimal
from hashlib import sha256

from onec_runtime.bsl.source_maps import SourceUnitKind, SourceUnitRef, source_sha256
from onec_runtime.capture_evaluation import CapturePhase
from onec_runtime.errors import ProtocolError, StaleCaptureError
from onec_runtime.execution.namespace import RuntimeNamespaceOwner
from onec_runtime.execution.post_bootstrap import (
    compose_fresh_post_bootstrap_execution, compose_post_bootstrap_execution,
)
from onec_runtime.execution.settlement import RouteSettlementService
from onec_runtime.execution.termination import FileTargetProcessLease
from onec_runtime.execution.worker_activation import WorkerActivationSnapshot
from onec_runtime.execution.worker_activation import WorkerMaterializationSnapshot
from onec_runtime.execution.value_materialization_router import ValueMaterializationRouter
from onec_runtime.rdbg.models import DebugTarget, StopEvent
from onec_runtime.rdbg.session import SessionState
from onec_runtime.runtime_api import RuntimeReplyKind
from onec_runtime.rdbg.models import EvaluationResult
from onec_runtime.table_materialization import ReferencePolicy
from onec_runtime.value_materialization import MaterializationOptions
from onec_runtime.worker_breakpoints import WorkerBreakpointResolution

from test_execution_controller_routes import BUSINESS, KERNEL, TARGET, CompleteSession
from test_main_idle_materialization import Session as ValueSession, TARGET as VALUE_TARGET
from test_compact_table import compact_payload
from test_worker_breakpoints import _debug_view


class _Replies:
    def diagnostic_reply(self, diagnostic):  # type: ignore[no-untyped-def]
        raise AssertionError(diagnostic.message)

    def unavailable_reply(self, unavailable):  # type: ignore[no-untyped-def]
        raise AssertionError(unavailable.reason)


def test_post_bootstrap_factory_builds_one_core_with_public_facade() -> None:
    session = CompleteSession()
    worker = lambda: WorkerActivationSnapshot(0, (), None, None)
    namespace = RuntimeNamespaceOwner(7, 3, worker_snapshot=worker)
    settlement = RouteSettlementService(namespace)
    retained: list[SourceUnitRef] = []
    resolved_frames: list[tuple[int, ...]] = []

    def resolve_sources(frames):  # type: ignore[no-untyped-def]
        resolved_frames.append(tuple(frame.level for frame in frames))
        return (None,) * len(frames)

    composed = compose_post_bootstrap_execution(
        session,
        KERNEL,
        runtime_generation=7,
        context_generation=3,
        initial_target_id=TARGET,
        capture_locations=(),
        namespace=namespace,
        worker_snapshot=worker,
        worker_materialization_snapshot=lambda: WorkerMaterializationSnapshot(0, ()),
        settlement_services=settlement,
        reply_presenter=_Replies(),
        retained_source_units=lambda: tuple(retained),
        resolve_capture_sources=resolve_sources,
    )
    try:
        assert composed.core.arbiter is composed.facade._arbiter
        assert composed.status.namespace_snapshot().runtime_generation == 7
        assert composed.source_identity("Результат = 1;").revision == 1
        assert isinstance(composed.facade._value_router, ValueMaterializationRouter)
        retained.append(SourceUnitRef(
            SourceUnitKind.NOTEBOOK_CELL, "retained", 1, source_sha256("old"),
        ))
        conflicting = SourceUnitRef(
            SourceUnitKind.NOTEBOOK_CELL, "retained", 1, source_sha256("new"),
        )
        with pytest.raises(ProtocolError, match="source"):
            composed.facade.execute_bsl("new", source_unit=conflicting)
        assert session.calls == []

        composed.facade.configure_capture_points((BUSINESS,))
        first = composed.facade.execute_bsl("Результат = 1;")
        assert first.kind is RuntimeReplyKind.CAPTURED
        assert composed.facade.capture_inspection().stack[:1].total >= 1
        assert resolved_frames
        cell = composed.facade.execute_bsl("Результат = 2;")
        assert cell.kind is RuntimeReplyKind.CAPTURE_CELL
        capture_view = composed.facade.current_capture()
        stack = composed.facade.capture_inspection().stack
        composed.facade.invalidate_capture_inspection()
        assert capture_view.status().phase is CapturePhase.PAUSED
        assert capture_view.status().can_inspect is False
        assert capture_view.status().can_resume_capture is True
        with pytest.raises(StaleCaptureError):
            _ = stack[:1]
        final = composed.facade.resume_capture()
        assert final.kind is RuntimeReplyKind.MAIN_COMPLETED
    finally:
        composed.facade.close()


def test_fresh_composition_rejects_source_identity_reuse_while_main_is_suspended() -> None:
    session = CompleteSession()
    composed = compose_fresh_post_bootstrap_execution(
        session, KERNEL,
        runtime_generation=7,
        stopped_target=session.target,
        capture_locations=(BUSINESS,),
        notebook_builder=lambda *_args, **_kwargs: None,
    )
    assert composed.worker_module_service.arbiter is composed.execution.core.arbiter
    assert composed.execution.facade.last_worker_breakpoint_reload_report() is None
    first_source = "Результат = 1;"
    changed_source = "Результат = 2;"
    first_unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL, "notebook-cell", 4,
        source_sha256(first_source),
    )
    changed_unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL, "notebook-cell", 4,
        source_sha256(changed_source),
    )
    try:
        captured = composed.execution.facade.execute_bsl(
            first_source, source_unit=first_unit,
        )
        assert captured.kind is RuntimeReplyKind.CAPTURED
        calls_before = len(session.calls)

        with pytest.raises(ProtocolError, match="source identity conflicts"):
            composed.execution.facade.execute_bsl(
                changed_source, source_unit=changed_unit,
            )
        assert len(session.calls) == calls_before

        completed = composed.execution.facade.resume_capture()
        assert completed.kind is RuntimeReplyKind.MAIN_COMPLETED
        assert composed.execution.source_identity.next_unit(
            changed_source, explicit=changed_unit,
        ) == changed_unit
    finally:
        composed.execution.facade.close()


def test_post_bootstrap_facade_materializes_on_initial_main_idle_route() -> None:
    payload = b'{"version":1,"root":{"t":"number","v":"12.50"}}'
    encoded = b64encode(payload).decode("ascii")
    response = [
        f"R|7|3|{len(payload)}|{sha256(payload).hexdigest()}|{len(encoded)}",
        encoded, "Истина",
    ]
    session = ValueSession(response + response)
    worker = lambda: WorkerActivationSnapshot(0, (), None, None)
    namespace = RuntimeNamespaceOwner(7, 3, worker_snapshot=worker)
    composed = compose_post_bootstrap_execution(
        session, KERNEL,
        runtime_generation=7, context_generation=3,
        initial_target_id=VALUE_TARGET, capture_locations=(),
        namespace=namespace, worker_snapshot=worker,
        worker_materialization_snapshot=lambda: WorkerMaterializationSnapshot(0, ()),
        settlement_services=RouteSettlementService(namespace),
        reply_presenter=_Replies(), retained_source_units=lambda: (),
    )
    try:
        assert composed.facade.materialize_value(
            "Контекст.Сумма", MaterializationOptions(max_bytes=4096),
        ) == Decimal("12.50")
        kind, projected = composed.facade.project_value_payload(
            "Контекст.Сумма", kind="slice", offset=0, limit=1,
            columns=(), names=(), max_depth=2, max_items=1,
            max_rows=1, max_bytes=4096,
        )
        assert kind == "value"
        assert projected == payload
        assert len([call for call in session.calls if call[0] == "start"]) == 6
    finally:
        composed.facade.close()


def test_failed_capture_cell_can_be_repaired_then_materialized_and_resumed() -> None:
    payload = compact_payload()
    encoded = b64encode(payload).decode("ascii")

    class CaptureSession(CompleteSession):
        def __init__(self) -> None:
            super().__init__()
            self.rejected = False

        def wait_evaluation_event(self, pending, *, timeout_s, on_transport_dispatch):
            expression = self.expression
            if "ОшибочнаяКоманда" in expression and not self.rejected:
                self.rejected = True
                on_transport_dispatch()
                self._record("wait_eval")
                self.pending = None
                return EvaluationResult(pending.result_id, "Ошибка", "", True, "planned")
            if "СериализоватьКомпактнуюТаблицу" in expression:
                value = f"R|7|3|{len(payload)}|{sha256(payload).hexdigest()}|{len(encoded)}"
            elif expression.startswith(
                "RuntimeKernelServer.ЗабратьКомпактнуюМатериализациюИзКонтекста("
            ):
                value = encoded
            elif "Контекст.Удалить(" in expression:
                value = "Истина"
            else:
                return super().wait_evaluation_event(
                    pending, timeout_s=timeout_s,
                    on_transport_dispatch=on_transport_dispatch,
                )
            on_transport_dispatch()
            self._record("wait_eval")
            self.pending = None
            return EvaluationResult(
                pending.result_id, "Строка", f'"{value}"', False,
                value_string=value,
            )

    session = CaptureSession()
    worker = lambda: WorkerActivationSnapshot(0, (), None, None)
    namespace = RuntimeNamespaceOwner(7, 3, worker_snapshot=worker)
    composed = compose_post_bootstrap_execution(
        session, KERNEL,
        runtime_generation=7, context_generation=3,
        initial_target_id=TARGET, capture_locations=(BUSINESS,),
        namespace=namespace, worker_snapshot=worker,
        worker_materialization_snapshot=lambda: WorkerMaterializationSnapshot(0, ()),
        settlement_services=RouteSettlementService(namespace),
        reply_presenter=_Replies(), retained_source_units=lambda: (),
    )
    try:
        assert composed.facade.execute_bsl("Результат = 1;").kind is RuntimeReplyKind.CAPTURED
        failed = composed.facade.execute_bsl("ОшибочнаяКоманда();")
        assert failed.kind is RuntimeReplyKind.CAPTURE_CELL
        assert failed.succeeded is False
        assert composed.facade.capture_inspection().stack[:1].total >= 1
        repaired = composed.facade.execute_bsl("Результат = 2;")
        assert repaired.kind is RuntimeReplyKind.CAPTURE_CELL
        assert repaired.succeeded is True
        frame = composed.facade.to_df(
            "Контекст.Таблица",
            ReferencePolicy(ref_columns={"Employee": "both", "Department": "uuid"}),
            max_rows=10, max_bytes=4096,
        )
        assert frame["Name"].tolist() == ["Alice", "Bob"]
        assert composed.facade.resume_capture().kind is RuntimeReplyKind.MAIN_COMPLETED
    finally:
        composed.facade.close()


def test_fresh_post_bootstrap_composes_worker_snapshot_and_one_breakpoint_owner() -> None:
    session = CompleteSession()
    composed = compose_fresh_post_bootstrap_execution(
        session, KERNEL,
        runtime_generation=7,
        stopped_target=session.target,
        capture_locations=(BUSINESS,),
        notebook_builder=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("worker builder must not run during fresh composition")
        ),
    )
    try:
        assert composed.namespace.namespace_snapshot().context_generation == 1
        assert composed.worker_activation.snapshot() == WorkerActivationSnapshot(0, (), None, None)
        assert composed.execution.status.status().worker_generation is None
        assert composed.execution.core.breakpoint_routes is composed.breakpoint_routes
        assert composed.breakpoint_routes.worker_owner is composed.breakpoint_workspace
        assert composed.worker_activation._breakpoint_workspace._workspace is composed.breakpoint_workspace
    finally:
        composed.execution.facade.close()


def test_fresh_composition_routes_worker_breakpoint_install_through_its_arbiter(
    tmp_path,
) -> None:
    session = CompleteSession()
    composed = compose_fresh_post_bootstrap_execution(
        session, KERNEL,
        runtime_generation=7,
        stopped_target=session.target,
        capture_locations=(BUSINESS,),
        notebook_builder=lambda *_args, **_kwargs: None,
    )
    try:
        view = _debug_view(tmp_path)
        module = view.modules[0]
        composed.worker_breakpoints.set_views((view,))

        status = composed.execution.facade.add_worker_breakpoint(
            module.source_unit, module.canonical_module, 2,
        )

        assert status.resolution is WorkerBreakpointResolution.RESOLVED
        assert composed.worker_breakpoint_service.worker_breakpoint_status(
            status.breakpoint.id,
        ) == status
        assert status.installed_binding_count == 1
        assert composed.breakpoint_workspace.confirmed_snapshot.worker_slots

        assert composed.execution.facade.execute_bsl(
            "Результат = 1;",
        ).kind is RuntimeReplyKind.CAPTURED
        disabled = composed.execution.facade.set_worker_breakpoint_enabled(
            status.breakpoint.id, False,
        )
        assert disabled.enabled is False
        assert composed.execution.facade.resume_capture().kind is RuntimeReplyKind.MAIN_COMPLETED

        breakpoint_calls = [thread for name, thread in session.calls
                            if name == "set_breakpoints"]
        assert len(breakpoint_calls) >= 2
        assert all(thread.name == "rdbg-arbiter" for thread in breakpoint_calls)
    finally:
        composed.execution.facade.close()


def test_fresh_post_bootstrap_requires_the_exact_stopped_target() -> None:
    session = CompleteSession()
    session.target = type(session.target)(TARGET, "CLIENT", "started", 1)

    with pytest.raises(ProtocolError, match="stopped target"):
        compose_fresh_post_bootstrap_execution(
            session, KERNEL,
            runtime_generation=7,
            stopped_target=session.target,
            capture_locations=(),
            notebook_builder=lambda *_args, **_kwargs: None,
        )


def test_fresh_post_bootstrap_rejects_a_different_stopped_target() -> None:
    session = CompleteSession()
    foreign = DebugTarget(type(TARGET)(TARGET.id, "another-target"), "CLIENT", "stopped", 1)

    with pytest.raises(ProtocolError, match="exact stopped target"):
        compose_fresh_post_bootstrap_execution(
            session, KERNEL,
            runtime_generation=7,
            stopped_target=foreign,
            capture_locations=(),
            notebook_builder=lambda *_args, **_kwargs: None,
        )


def test_fresh_post_bootstrap_accepts_exact_observed_stop_over_old_registry_state() -> None:
    session = CompleteSession()
    session.target = DebugTarget(TARGET, "Server", "Started", 1)
    session.state = SessionState.READY
    stop = StopEvent(TARGET, KERNEL, "breakpoint")

    composed = compose_fresh_post_bootstrap_execution(
        session, KERNEL,
        runtime_generation=7,
        stopped_target=session.target,
        bootstrap_stop=stop,
        capture_locations=(),
        notebook_builder=lambda *_args, **_kwargs: None,
    )
    try:
        assert composed.execution.core.controller._initial_target_id == TARGET
    finally:
        composed.execution.facade.close()


def test_fresh_post_bootstrap_rejects_foreign_observed_stop() -> None:
    session = CompleteSession()
    session.target = DebugTarget(TARGET, "Server", "Started", 1)
    session.state = SessionState.READY
    foreign = type(TARGET)(TARGET.id, "another-target")

    with pytest.raises(ProtocolError, match="exact stopped target"):
        compose_fresh_post_bootstrap_execution(
            session, KERNEL,
            runtime_generation=7,
            stopped_target=session.target,
            bootstrap_stop=StopEvent(foreign, KERNEL, "breakpoint"),
            capture_locations=(),
            notebook_builder=lambda *_args, **_kwargs: None,
        )


def test_fresh_file_composition_requires_and_forwards_exact_debuggee_lease() -> None:
    session = CompleteSession()
    session.target = DebugTarget(TARGET, "ServerEmulation", "stopped", 1)

    class Process:
        pid = 1234

    class OwnedDebuggee:
        pid = 1234
        process = Process()

        def close(self, timeout_s):
            raise AssertionError("composition must not stop the debuggee")

    lease = FileTargetProcessLease(TARGET, OwnedDebuggee())
    with pytest.raises(ProtocolError, match="file debuggee lease"):
        compose_fresh_post_bootstrap_execution(
            session, KERNEL,
            runtime_generation=7,
            stopped_target=session.target,
            capture_locations=(),
            notebook_builder=lambda *_args, **_kwargs: None,
        )

    composed = compose_fresh_post_bootstrap_execution(
        session, KERNEL,
        runtime_generation=7,
        stopped_target=session.target,
        capture_locations=(),
        notebook_builder=lambda *_args, **_kwargs: None,
        file_target_lease=lease,
    )
    try:
        assert composed.execution.core.arbiter._file_target_lease is lease
    finally:
        composed.execution.facade.close()


def test_fresh_file_composition_rejects_foreign_debuggee_lease() -> None:
    session = CompleteSession()
    session.target = DebugTarget(TARGET, "ServerEmulation", "stopped", 1)
    foreign = type(TARGET)(TARGET.id, "another-target")

    class OwnedDebuggee:
        pid = 1234

    with pytest.raises(ProtocolError, match="exact stopped file target"):
        compose_fresh_post_bootstrap_execution(
            session, KERNEL,
            runtime_generation=7,
            stopped_target=session.target,
            capture_locations=(),
            notebook_builder=lambda *_args, **_kwargs: None,
            file_target_lease=FileTargetProcessLease(foreign, OwnedDebuggee()),
        )


def test_fresh_file_composition_rejects_mismatched_selected_target_type() -> None:
    session = CompleteSession()
    session.target = DebugTarget(TARGET, "Server", "stopped", 1)
    file_target = DebugTarget(TARGET, "ServerEmulation", "stopped", 1)

    with pytest.raises(ProtocolError, match="exact stopped target"):
        compose_fresh_post_bootstrap_execution(
            session, KERNEL,
            runtime_generation=7,
            stopped_target=file_target,
            capture_locations=(),
            notebook_builder=lambda *_args, **_kwargs: None,
        )
