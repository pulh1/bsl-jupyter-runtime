"""Public BSL entry points over the single-owner execution path."""

from contextlib import contextmanager
from threading import Event
from types import SimpleNamespace

import pytest

from onec_runtime.bsl.source_maps import SourceUnitKind, SourceUnitRef, source_sha256
from onec_runtime.errors import ProtocolError
from onec_runtime.execution.arbiter import ArbiterBusy
from onec_runtime.execution.contracts import PreparedCell
from onec_runtime.execution.public_facade import PublicExecutionFacade
from onec_runtime.execution.source_identity import NotebookSourceIdentityFactory
from onec_runtime.runtime_contracts import OperationExecutionProvenance


def unit(source: str) -> SourceUnitRef:
    return SourceUnitRef(SourceUnitKind.NOTEBOOK_CELL, "facade", 1, source_sha256(source))


class _Pipeline:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def execute(
        self, source, source_unit, *, wait_handoff=None,
        on_prepared=None, on_admitted=None,
    ):
        self.calls.append((source, source_unit))
        with wait_handoff():
            self.calls.append(("wait",))
        prepared = PreparedCell(object(), object(), "prepared")
        if on_prepared is not None:
            self.calls.append(("provenance prepared",))
            on_prepared(prepared)
        if on_admitted is not None:
            on_admitted(prepared)
            self.calls.append(("admitted",))
        return "reply"


class _Ticket:
    def __init__(self, value="reply") -> None:
        self.value = value
        self.calls: list[object] = []
        self.settled = Event()
        self.settled.set()

    def wait_initiator(self, timeout=None):
        self.calls.append(("wait_initiator", timeout))
        if timeout == 0:
            raise TimeoutError("local wait elapsed")
        return self.value

    def status(self):
        return SimpleNamespace(settled=self.settled.is_set())

    def wait_settled(self, timeout=None):
        self.calls.append(("wait_settled", timeout))
        self.settled.wait(1)
        return self.value

    def detach_waiter(self):
        self.calls.append("detach")


class _Controller:
    def __init__(self) -> None:
        self.resume = _Ticket()
        self.debug_resume = _Ticket()
        self.calls: list[str] = []

    def submit_resume(self, *, dirty_roots=(), successor_locations=None):
        self.calls.append(("capture", dirty_roots, successor_locations))
        return self.resume

    def submit_resume_debug_stop(self):
        self.calls.append("debug")
        return self.debug_resume

    def configure_capture_points(self, locations):
        self.calls.append(("capture points", locations))


class _Arbiter:
    def __init__(self) -> None:
        self.heartbeat = _Ticket("heartbeat")
        self.close_calls: list[object] = []
        self.close_error: BaseException | None = None

    def try_heartbeat(self):
        return self.heartbeat

    def close(self, timeout=None):
        self.close_calls.append(timeout)
        if self.close_error is not None:
            raise self.close_error


def facade(*, provenance_reader=None):
    pipeline = _Pipeline()
    controller = _Controller()
    arbiter = _Arbiter()
    result = PublicExecutionFacade(
        pipeline, controller, arbiter,
        source_unit_factory=unit,
        status_reader=lambda: "status",
        namespace_reader=lambda: "namespace",
        provenance_reader=provenance_reader,
    )
    return result, pipeline, controller, arbiter


def test_public_facade_configures_capture_points_through_controller() -> None:
    api, _, controller, _ = facade()
    locations = (object(),)

    api.configure_capture_points(locations)

    assert controller.calls == [("capture points", locations)]


def test_public_facade_routes_value_transfers_without_mode_logic() -> None:
    from onec_runtime.table_materialization import ReferencePolicy
    from onec_runtime.value_materialization import MaterializationOptions

    class Router:
        def __init__(self) -> None:
            self.calls = []

        def materialize_value(self, handle, options=None):
            self.calls.append(("value", handle, options))
            return 42

        def to_df(self, handle, policy=None, *, max_rows, max_bytes):
            self.calls.append(("table", handle, policy, max_rows, max_bytes))
            return "frame"

        def materialize(self, handle, options=None, *, table_policy=None):
            self.calls.append(("dynamic", handle, options, table_policy))
            return "value or frame"

        def head_to_df(self, handle, count, *, policy=None, max_bytes):
            self.calls.append(("head", handle, count, policy, max_bytes))
            return "head frame"

    router = Router()
    api = PublicExecutionFacade(
        _Pipeline(), _Controller(), _Arbiter(),
        source_unit_factory=unit, status_reader=lambda: "status",
        value_router=router,
    )
    options = MaterializationOptions(max_bytes=4096)
    policy = ReferencePolicy(refs="uuid")

    assert api.materialize_value("Контекст.X", options) == 42
    assert api.to_df("Контекст.Таблица", policy, max_rows=10, max_bytes=4096) == "frame"
    assert api.materialize("Контекст.X", options, table_policy=policy) == "value or frame"
    assert api.head_to_df("Контекст.Таблица", 2, policy=policy, max_bytes=4096) == "head frame"
    assert router.calls == [
        ("value", "Контекст.X", options),
        ("table", "Контекст.Таблица", policy, 10, 4096),
        ("dynamic", "Контекст.X", options, policy),
        ("head", "Контекст.Таблица", 2, policy, 4096),
    ]


def test_public_facade_exposes_fenced_capture_inspection() -> None:
    from onec_runtime.execution.capture.public_inspection import CaptureInspection
    from test_capture_inspection_bridge import _Controller as InspectionController
    from test_capture_stack_inventory_adapter import ready_scope

    controller = InspectionController(ready_scope())
    api = PublicExecutionFacade(
        _Pipeline(), controller, _Arbiter(),
        source_unit_factory=unit, status_reader=lambda: "status",
    )

    inspection = api.capture_inspection()

    assert isinstance(inspection, CaptureInspection)
    assert inspection.stack[:1].total == 2


def test_public_facade_builds_capture_view_from_bridge_and_local_ledger() -> None:
    from onec_runtime.capture_evaluation import CaptureEvaluationKind, CapturePhase
    from onec_runtime.capture_inspection import CaptureView
    from onec_runtime.execution.capture.evaluation_ledger import CaptureEvaluationLedger
    from test_capture_inspection_bridge import _Controller as InspectionController
    from test_capture_stack_inventory_adapter import ready_scope

    scope = ready_scope()

    class Controller(InspectionController):
        def __init__(self):
            super().__init__(scope)
            self.ledger = CaptureEvaluationLedger(scope, is_current=lambda: self.capture_scope is scope)

        def capture_evaluation_ledger(self):
            return self.ledger

    controller = Controller()
    api = PublicExecutionFacade(
        _Pipeline(), controller, _Arbiter(),
        source_unit_factory=unit, status_reader=lambda: "status",
    )
    controller.ledger.begin("capture-view", CaptureEvaluationKind.USER_BSL)

    view = api.current_capture()
    assert isinstance(view, CaptureView)
    assert view.operation_id == scope.identity.main_command_id
    assert view.capture_generation == scope.identity.runtime_generation
    assert view.stop_sequence == scope.identity.local_stop_sequence
    assert view.status().phase is CapturePhase.EVALUATING
    assert view.wait(timeout_s=0).evaluation_id == "capture-view"
    assert view.stack[:1].total == 2
    assert view.context is view.context


def test_public_facade_validates_direct_value_reference_without_rdbg() -> None:
    api, _, _, _ = facade()

    assert api.validate_value_reference("Контекст.Таблица") == "Контекст.Таблица"
    with pytest.raises(ProtocolError, match="Worker generation"):
        api.validate_value_reference("Контекст.RuntimeWorkerActiveGeneration")


def test_execute_bsl_releases_bound_session_lock_only_at_pipeline_waits() -> None:
    api, pipeline, _, _ = facade()
    events: list[str] = []

    @contextmanager
    def release_lock():
        events.append("release")
        yield
        events.append("reacquire")

    with api.execution_caller_handoff(release_lock):
        assert api.execute_bsl("Результат = 1;") == "reply"
    assert pipeline.calls == [("Результат = 1;", unit("Результат = 1;")), ("wait",)]
    assert events == ["release", "reacquire"]


def test_notebook_provenance_without_reader_fails_before_pipeline_execution() -> None:
    api, pipeline, _, _ = facade()
    with pytest.raises(ProtocolError, match="provenance"):
        api.execute_bsl("Результат = 1;", on_execution_provenance=lambda value: None)
    assert pipeline.calls == []


def test_explicit_source_identity_conflict_is_rejected_before_preparation() -> None:
    original = unit("old source")
    replacement = SourceUnitRef(
        original.kind, original.unit_id, original.revision,
        source_sha256("new source"),
    )
    pipeline = _Pipeline()
    api = PublicExecutionFacade(
        pipeline, _Controller(), _Arbiter(),
        source_unit_factory=unit,
        source_identity=NotebookSourceIdentityFactory(lambda: (original,)),
        status_reader=lambda: "status",
    )
    with pytest.raises(ProtocolError, match="conflicts"):
        api.execute_bsl("new source", source_unit=replacement)
    assert pipeline.calls == []


def test_provenance_reader_receives_only_admitted_prepared_cell() -> None:
    digest = source_sha256("source")
    provenance = OperationExecutionProvenance(digest, digest, digest, "main")
    read: list[PreparedCell] = []
    published: list[OperationExecutionProvenance] = []

    def read_provenance(prepared):
        read.append(prepared)
        return provenance

    api, _, _, _ = facade(provenance_reader=read_provenance)
    assert api.execute_bsl("source", on_execution_provenance=published.append) == "reply"
    assert len(read) == 1
    assert read[0].payload == "prepared"
    assert published == [provenance]


def test_provenance_reader_failure_prevents_admitted_publication() -> None:
    published: list[object] = []

    def reject(prepared):
        raise ProtocolError("Worker artifact unavailable")

    api, pipeline, _, _ = facade(provenance_reader=reject)
    with pytest.raises(ProtocolError, match="Worker artifact"):
        api.execute_bsl("source", on_execution_provenance=published.append)
    assert published == []
    assert ("provenance prepared",) in pipeline.calls
    assert ("admitted",) not in pipeline.calls


def test_resume_capture_rejects_unbound_continuation_before_dispatch() -> None:
    api, _, controller, _ = facade()
    with pytest.raises(ProtocolError, match="continuation"):
        api.resume_capture(continuation_attempt_id="attempt-1")
    assert controller.calls == []


def test_resume_capture_passes_roots_and_successor_points_in_one_admission() -> None:
    api, _, controller, _ = facade()
    successor = object()
    assert api.resume_capture(
        dirty_roots=("Таблица",), successor_locations=(successor,),
    ) == "reply"
    assert controller.calls == [
        ("capture", ("Таблица",), (successor,)),
    ]


def test_resume_capture_delivers_attached_completion_and_releases_wait_lock() -> None:
    api, _, controller, _ = facade()
    outcomes: list[tuple[object, BaseException | None]] = []
    events: list[str] = []

    @contextmanager
    def release_lock():
        events.append("release")
        yield
        events.append("reacquire")

    with api.execution_caller_handoff(release_lock):
        assert api.resume_capture(on_completion=lambda reply, error: outcomes.append((reply, error))) == "reply"
    assert controller.calls == [("capture", (), None)]
    assert controller.resume.calls == [("wait_initiator", None)]
    assert events == ["release", "reacquire"]
    assert outcomes == [("reply", None)]


def test_resume_capture_wait_timeout_detaches_observer_without_cancelling_ticket() -> None:
    api, _, controller, _ = facade()
    controller.resume.settled.clear()
    detached = Event()
    outcomes: list[tuple[object, BaseException | None]] = []

    def complete(reply, error):
        outcomes.append((reply, error))
        detached.set()

    with pytest.raises(TimeoutError, match="local wait"):
        api.resume_capture(timeout_s=0, on_detached_completion=complete)
    controller.resume.settled.set()
    assert detached.wait(1)
    assert controller.resume.calls == [
        ("wait_initiator", 0), "detach", ("wait_settled", None),
    ]
    assert outcomes == [("reply", None)]


def test_confirmed_timeout_error_is_reported_as_operation_failure() -> None:
    api, _, controller, _ = facade()
    outcomes: list[tuple[object, BaseException | None]] = []

    def confirmed_timeout(*, timeout=None):
        raise TimeoutError("confirmed policy failure")

    controller.resume.wait_initiator = confirmed_timeout
    controller.resume.wait_settled = confirmed_timeout
    with pytest.raises(TimeoutError, match="confirmed policy failure"):
        api.resume_capture(on_completion=lambda reply, error: outcomes.append((reply, error)))
    assert len(outcomes) == 1
    assert outcomes[0][0] is None
    assert isinstance(outcomes[0][1], TimeoutError)
    assert "detach" not in controller.resume.calls


def test_debug_resume_status_and_heartbeat_use_only_injected_owners() -> None:
    api, _, controller, arbiter = facade()
    assert api.resume_debug_stop() == "reply"
    assert controller.calls == ["debug"]
    assert api.status() == "status"
    assert api.namespace_snapshot() == "namespace"
    assert api.try_heartbeat_ticket() is arbiter.heartbeat


def test_namespace_snapshot_without_owner_reader_fails_closed() -> None:
    api = PublicExecutionFacade(
        _Pipeline(), _Controller(), _Arbiter(),
        source_unit_factory=unit, status_reader=lambda: "status",
    )
    with pytest.raises(ProtocolError, match="namespace"):
        api.namespace_snapshot()


def test_facade_close_retains_arbiter_ownership_until_idle() -> None:
    api, _, _, arbiter = facade()
    assert api.owns_debug_ui_stream()
    arbiter.close_error = ArbiterBusy("remote operation is still active")
    with pytest.raises(ArbiterBusy, match="active"):
        api.close()
    assert arbiter.close_calls == [3.0]
    arbiter.close_error = None
    api.close()
    assert arbiter.close_calls == [3.0, 3.0]
