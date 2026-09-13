from __future__ import annotations

from pathlib import Path
from hashlib import sha256
from threading import RLock
from types import SimpleNamespace
from uuid import uuid4
import json
import re

import nbformat
import pytest

from dataclasses import replace

import onec_runtime_mcp.agent.contracts as agent_contracts
from onec_runtime_mcp.agent.capture_contracts import CapturePointRequest, ResolvedCapturePoint
from onec_runtime_mcp.agent.capture_service import (
    CaptureArming,
    CaptureIntent,
    CaptureRunOutcome,
    CaptureService,
    CaptureStop,
)
from onec_runtime_mcp.agent.contracts import (
    AgentOperationState,
    BackendExecution,
    CapabilityMode,
    RuntimeDescriptor,
    ServiceResponse,
    to_wire,
)
from onec_runtime_mcp.agent.facade import AgentFacade
from onec_runtime_mcp.agent.runtime_backend import (
    MainPreparationSourceError,
    OnecRuntimeBackend,
)
from onec_runtime_mcp.agent.service import AgentWorkspaceService
from onec_runtime.session import RuntimeSession
from onec_runtime_mcp.agent.runtime_session import AgentRuntimeSession
from onec_runtime.errors import BslExecutionError, ProtocolError
from onec_runtime.bsl import SourceUnitKind, SourceUnitRef, mapped_visible_source
from onec_runtime.prototype_runtime import OperationState
from onec_runtime.rdbg.models import ModuleLocation
from onec_runtime.runtime_api import (
    _PreparedMainExecutionAttempt,
    CaptureCorrelationTicket,
    RuntimeNamespaceSnapshot,
    RuntimeReply,
    RuntimeReplyKind,
    RuntimeStatus,
)


class _CaptureClient:
    def __init__(self, view: dict[str, object]) -> None:
        self.view = view
        self.calls: list[tuple[str, dict[str, object]]] = []

    def call(self, method: str, arguments: dict[str, object]) -> ServiceResponse:
        self.calls.append((method, arguments))
        assert method == "capture.run_until"
        return ServiceResponse.success(self.view)


def test_run_until_routes_one_source_level_request_to_the_domain() -> None:
    # Break caught: removing the facade intent or routing it through a generic
    # code-run path would make the returned operation/view and method disagree.
    request = {
        "cell_id": "cell-main",
        "revision": 2,
        "source_sha256": "a" * 64,
        "points": [
            {
                "name": "before_limit",
                "project": "zup",
                "module": "Payroll",
                "procedure": "Run",
                "line": 17,
            }
        ],
        "request_id": "facade-request-1",
    }
    view = {
            "operation": {
                "operation_id": "op-1",
                "kind": "capture_run_until",
                "runtime_id": "runtime-1",
                "runtime_generation": 1,
                "cell_id": "cell-main",
                "revision": 2,
                "source_sha256": "a" * 64,
            },
            "state": "unknown",
            "messages": [],
            "next_message_cursor": 0,
            "next_event_cursor": 1,
            "changed_variables": [],
            "change_confidence": "unknown",
            "outputs": {},
            "capture": None,
            "failure": {"stage": "capture_correlation", "partial_results": {}},
            "recovery": [],
            "truncation": {
                "messages": False,
                "changed_variables": False,
                "outputs": False,
            },
    }
    client = _CaptureClient(view)

    result = AgentFacade(client).capture_run_until(request)

    assert result.state is AgentOperationState.UNKNOWN
    assert result.capture is None
    assert client.calls == [("capture.run_until", request)]


def test_capture_run_until_requires_a_public_idempotency_key() -> None:
    client = _CaptureClient({})

    with pytest.raises(ValueError, match="request_id"):
        AgentFacade(client).capture_run_until(
            {
                "cell_id": "cell-main",
                "revision": 2,
                "source_sha256": "a" * 64,
                "points": [{"name": "before", "line": 17}],
            }
        )

    assert client.calls == []


class _CaptureBackend:
    runtime_id = "runtime-1"

    def __init__(self, *, mismatch: str | None = None, terminal: bool = False) -> None:
        self.calls: list[str] = []
        self.mismatch = mismatch
        self.terminal = terminal
        self.terminal_state = AgentOperationState.COMPLETED
        self.terminal_runtime_state: str | None = None
        self.stop_location_mutation: tuple[str, object] | None = None
        self.controller_operation_id = 42
        self.capture_ticket = "runtime-ticket-42"
        self.intent: CaptureIntent | None = None
        self.stop: CaptureStop | None = None
        self.prepared = object()

    def resolve_capture_points(
        self, points: tuple[CapturePointRequest, ...]
    ) -> tuple[ResolvedCapturePoint, ...]:
        self.calls.append("resolve")
        return tuple(
            ResolvedCapturePoint(
                name=point.name,
                project="zup",
                module="Payroll",
                procedure="Run",
                line=17 + index,
                source_revision=7,
                source_sha256="b" * 64,
                executable_line=17 + index,
                excerpt="Выполнить();",
            )
            for index, point in enumerate(points)
        )

    def arm_capture(self, intent: CaptureIntent) -> CaptureArming:
        self.calls.append("arm")
        self.intent = intent
        return CaptureArming(self.capture_ticket, 42, 1)

    def run_prepared_main_until_capture(
        self, prepared: object, *, intent: CaptureIntent
    ) -> CaptureRunOutcome:
        assert prepared is self.prepared
        self.calls.append("run_prepared_main")
        if self.terminal:
            return CaptureRunOutcome(
                BackendExecution(
                    self.terminal_state,
                    (),
                    False,
                    self.terminal_runtime_state or self.terminal_state.value,
                )
            )
        stop = CaptureStop(
            1,
            intent.points[0],
            self.controller_operation_id,
            self.capture_ticket,
            self.controller_operation_id,
        )
        if self.mismatch == "capture_intent_id":
            object.__setattr__(stop, "ticket_id", "unrelated-runtime-ticket")
        elif self.mismatch == "operation_id":
            object.__setattr__(stop, "controller_operation_id", 99)
        elif self.mismatch == "source_revision":
            object.__setattr__(
                stop, "location", replace(stop.location, source_revision=999)
            )
        elif self.mismatch == "source_sha256":
            object.__setattr__(
                stop, "location", replace(stop.location, source_sha256="c" * 64)
            )
        elif self.mismatch == "capture_generation":
            object.__setattr__(stop, "stop_sequence", 99)
        if self.stop_location_mutation is not None:
            field, value = self.stop_location_mutation
            object.__setattr__(stop, "location", replace(stop.location, **{field: value}))
        self.stop = stop
        return CaptureRunOutcome(
            BackendExecution(AgentOperationState.CAPTURED, (), False, "captured"), stop
        )

    def disarm_capture(self, *, policy: str) -> None:
        assert policy in {"terminal_main", "terminal_no_stop"}
        self.calls.append("disarm")


def _point(name: str = "before") -> CapturePointRequest:
    return CapturePointRequest(
        name=name, project="zup", module="Payroll", procedure="Run", line=17
    )


def test_capture_service_correlates_all_fence_fields_before_publishing_capture(tmp_path: Path) -> None:
    # Break caught: accepting a delayed/unrelated stop would publish a frame
    # whose correlation fence does not name the newly durable intent.
    backend = _CaptureBackend()

    result = CaptureService(tmp_path).run_until(
        backend,
        operation_id="op-1",
        prepared_main=backend.prepared,
        source_revision=2,
        source_sha256="a" * 64,
        points=(_point(), _point("after")),
    )

    assert result.execution.terminal_state is AgentOperationState.CAPTURED
    assert result.capture is not None
    assert result.capture.fence.capture_intent_id == backend.intent.capture_intent_id  # type: ignore[union-attr]
    assert result.capture.fence.capture_generation == 1
    assert backend.calls == ["resolve", "arm", "run_prepared_main"]


@pytest.mark.parametrize(
    "field",
    ["capture_intent_id", "operation_id", "source_revision", "source_sha256", "capture_generation"],
)
def test_capture_service_rejects_each_unrelated_stop_without_continuing(
    tmp_path: Path, field: str
) -> None:
    backend = _CaptureBackend(mismatch=field)

    result = CaptureService(tmp_path).run_until(
        backend,
        operation_id="op-1",
        prepared_main=backend.prepared,
        source_revision=2,
        source_sha256="a" * 64,
        points=(_point(),),
    )

    assert result.execution.terminal_state is AgentOperationState.UNKNOWN
    assert result.capture is None
    assert result.failure == {"stage": "capture_correlation", "partial_results": {}}
    assert [to_wire(action) for action in result.recovery] == [
        {
            "method": "operation.wait",
            "arguments": {
                "operation_id": "op-1",
                "timeout_s": 0,
                "after_event_cursor": 0,
                "after_message_cursor": 0,
            },
        },
        {
            "method": "runtime.close",
            "arguments": {"policy": "abort_generation"},
        },
    ]
    assert backend.calls == ["resolve", "arm", "run_prepared_main"]
    assert backend.intent is not None
    assert backend.stop is not None
    independently_mutated = {
        "capture_intent_id": backend.stop.ticket_id != backend.capture_ticket,
        "operation_id": backend.stop.controller_operation_id != 42,
        "source_revision": backend.stop.location.source_revision != 7,
        "source_sha256": backend.stop.location.source_sha256 != "b" * 64,
        "capture_generation": backend.stop.stop_sequence != 1,
    }
    assert [name for name, changed in independently_mutated.items() if changed] == [
        field
    ]


def test_capture_service_rejects_stop_from_a_different_controller_operation(tmp_path: Path) -> None:
    backend = _CaptureBackend()
    backend.controller_operation_id = 43

    result = CaptureService(tmp_path).run_until(
        backend,
        operation_id="op-1",
        prepared_main=backend.prepared,
        source_revision=2,
        source_sha256="a" * 64,
        points=(_point(),),
    )

    assert result.execution.terminal_state is AgentOperationState.UNKNOWN
    assert result.capture is None


@pytest.mark.parametrize(
    "terminal_state",
    [AgentOperationState.COMPLETED, AgentOperationState.FAILED, AgentOperationState.UNKNOWN],
)
def test_capture_service_disarms_every_terminal_no_stop_outcome_without_continuing(
    tmp_path: Path, terminal_state: AgentOperationState
) -> None:
    backend = _CaptureBackend(terminal=True)
    backend.terminal_state = terminal_state

    result = CaptureService(tmp_path).run_until(
        backend,
        operation_id="op-1",
        prepared_main=backend.prepared,
        source_revision=2,
        source_sha256="a" * 64,
        points=(_point(),),
    )

    assert result.execution.terminal_state is terminal_state
    assert result.capture is None
    assert backend.calls == ["resolve", "arm", "run_prepared_main", "disarm"]


def test_capture_service_reports_an_unexpected_debug_stop_with_recovery(tmp_path: Path) -> None:
    backend = _CaptureBackend(terminal=True)
    backend.terminal_state = AgentOperationState.UNKNOWN
    backend.terminal_runtime_state = "debug_stopped"

    result = CaptureService(tmp_path).run_until(
        backend,
        operation_id="op-1",
        prepared_main=backend.prepared,
        source_revision=2,
        source_sha256="a" * 64,
        points=(_point(),),
    )

    assert result.execution.runtime_state == "debug_stopped"
    assert result.failure == {"stage": "unexpected_breakpoint", "partial_results": {}}
    assert [action.method for action in result.recovery] == [
        "workspace.status", "operation.wait", "runtime.close"
    ]
    assert backend.calls == ["resolve", "arm", "run_prepared_main", "disarm"]


def test_capture_service_disarms_after_transport_exception_without_continuing(tmp_path: Path) -> None:
    class TransportBackend(_CaptureBackend):
        def run_prepared_main_until_capture(
            self, prepared: object, *, intent: CaptureIntent
        ) -> CaptureRunOutcome:
            assert prepared is self.prepared
            self.calls.append("run_prepared_main")
            raise OSError("controller transport lost")

    backend = TransportBackend()
    result = CaptureService(tmp_path).run_until(
        backend,
        operation_id="op-1",
        prepared_main=backend.prepared,
        source_revision=2,
        source_sha256="a" * 64,
        points=(_point(),),
    )

    assert result.execution.terminal_state is AgentOperationState.UNKNOWN
    assert result.failure == {"stage": "capture_transport", "partial_results": {}}
    assert [action.method for action in result.recovery] == [
        "operation.wait", "runtime.close"
    ]
    assert backend.calls == ["resolve", "arm", "run_prepared_main", "disarm"]


@pytest.mark.parametrize(
    ("failure_path", "expected_stage"),
    [
        ("no_stop_captured", "capture_correlation"),
        ("disarm", "capture_disarm"),
        ("arm_cleanup", "capture_arming"),
    ],
)
def test_each_run_until_uncertainty_path_returns_capture_profile_recovery(
    tmp_path: Path, failure_path: str, expected_stage: str
) -> None:
    class RecoveryBackend(_CaptureBackend):
        def arm_capture(self, intent: CaptureIntent) -> CaptureArming:
            if failure_path == "arm_cleanup":
                self.calls.append("arm")
                raise OSError("arming failed ambiguously")
            return super().arm_capture(intent)

        def run_prepared_main_until_capture(
            self, prepared: object, *, intent: CaptureIntent
        ) -> CaptureRunOutcome:
            assert prepared is self.prepared
            if failure_path == "no_stop_captured":
                self.calls.append("run_prepared_main")
                return CaptureRunOutcome(
                    BackendExecution(
                        AgentOperationState.CAPTURED, (), False, "captured"
                    )
                )
            return super().run_prepared_main_until_capture(
                prepared, intent=intent
            )

        def disarm_capture(self, *, policy: str) -> None:
            self.calls.append("disarm")
            if failure_path in {"disarm", "arm_cleanup"}:
                raise OSError("disarm uncertain")

    backend = RecoveryBackend(terminal=failure_path == "disarm")
    result = CaptureService(tmp_path).run_until(
        backend,
        operation_id="op-recovery",
        prepared_main=backend.prepared,
        source_revision=2,
        source_sha256="a" * 64,
        points=(_point(),),
    )

    assert result.execution.terminal_state is AgentOperationState.UNKNOWN
    assert result.failure is not None and result.failure["stage"] == expected_stage
    assert [to_wire(action) for action in result.recovery] == [
        {
            "method": "operation.wait",
            "arguments": {
                "operation_id": "op-recovery",
                "timeout_s": 0,
                "after_event_cursor": 0,
                "after_message_cursor": 0,
            },
        },
        {
            "method": "runtime.close",
            "arguments": {"policy": "abort_generation"},
        },
    ]


def test_real_zup_adapter_disarms_failed_capture_before_the_next_main(tmp_path: Path) -> None:
    class Session:
        def __init__(self) -> None:
            self.armed = False
            self.policies: list[str] = []
            self.executions = 0

        def resolve_capture_points(
            self, points: tuple[CapturePointRequest, ...]
        ) -> tuple[ResolvedCapturePoint, ...]:
            return _CaptureBackend().resolve_capture_points(points)

        def arm_capture_intent(self, intent: CaptureIntent) -> CaptureArming:
            del intent
            self.armed = True
            return CaptureArming("runtime-ticket-21", 21, 1)

        def execute_bsl(self, source: str) -> RuntimeReply:
            del source
            self.executions += 1
            if self.executions == 1:
                return RuntimeReply(
                    RuntimeReplyKind.MAIN_COMPLETED,
                    21,
                    OperationState.FAILED,
                    error="runtime execution failed",
                    succeeded=False,
                )
            assert self.armed is False
            return RuntimeReply(RuntimeReplyKind.MAIN_COMPLETED, 22, OperationState.COMPLETED)

        def prepare_main_for_capture(
            self, source: str, *, source_unit: SourceUnitRef
        ) -> object:
            assert source_unit.source_sha256 == sha256(source.encode()).hexdigest()
            return source

        def execute_prepared_main_for_capture(self, prepared: object) -> object:
            assert isinstance(prepared, str)
            return _PreparedMainExecutionAttempt(
                reply=self.execute_bsl(prepared),
                user_main_dispatched=True,
            )

        def activate_prepared_main_for_capture(self, prepared: object) -> object:
            return prepared

        def disarm_capture_intent(self, *, policy: str) -> None:
            self.policies.append(policy)
            self.armed = False

    session = Session()
    backend = OnecRuntimeBackend("runtime-zup", session)  # type: ignore[arg-type]
    source = "Результат = 1;"
    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL,
        "cell-main",
        2,
        sha256(source.encode()).hexdigest(),
    )
    prepared = backend.prepare_main_for_capture(source, source_unit=unit)
    prepared = backend.activate_prepared_main_for_capture(prepared)
    result = CaptureService(tmp_path).run_until(
        backend,
        operation_id="op-1",
        prepared_main=prepared,
        source_revision=2,
        source_sha256=sha256(source.encode()).hexdigest(),
        points=(_point(),),
    )

    assert result.execution.terminal_state is AgentOperationState.FAILED
    assert result.execution.failure_stage == "execution"
    assert session.policies == ["terminal_no_stop"]
    assert backend.execute_bsl("СледующийБезопасныйMain();").terminal_state is AgentOperationState.COMPLETED


@pytest.mark.parametrize("mutation", ["operation_id", "stop_sequence", "ticket", "location"])
def test_real_zup_adapter_rejects_mutated_runtime_capture_evidence(
    tmp_path: Path, mutation: str
) -> None:
    class Session:
        def __init__(self) -> None:
            self.point = _CaptureBackend().resolve_capture_points((_point(),))[0]

        def resolve_capture_points(
            self, points: tuple[CapturePointRequest, ...]
        ) -> tuple[ResolvedCapturePoint, ...]:
            del points
            return (self.point,)

        def arm_capture_intent(self, intent: CaptureIntent) -> CaptureArming:
            del intent
            return CaptureArming("runtime-ticket-42", 42, 1)

        def execute_bsl(self, source: str) -> RuntimeReply:
            del source
            reply = RuntimeReply(
                RuntimeReplyKind.CAPTURED,
                42,
                OperationState.CAPTURED,
                location=object(),
                stop_sequence=1,
                capture_ticket="runtime-ticket-42",
            )
            if mutation == "operation_id":
                return replace(reply, operation_id=999)
            if mutation == "stop_sequence":
                return replace(reply, stop_sequence=2)
            if mutation == "ticket":
                return replace(reply, capture_ticket="delayed-ticket")
            self.point = replace(self.point, source_sha256="c" * 64)
            return reply

        def prepare_main_for_capture(
            self, source: str, *, source_unit: SourceUnitRef
        ) -> object:
            assert source_unit.source_sha256 == sha256(source.encode()).hexdigest()
            return source

        def execute_prepared_main_for_capture(self, prepared: object) -> object:
            assert isinstance(prepared, str)
            return _PreparedMainExecutionAttempt(
                reply=self.execute_bsl(prepared),
                user_main_dispatched=True,
            )

        def activate_prepared_main_for_capture(self, prepared: object) -> object:
            return prepared

        def capture_location(
            self, location: object, *, ticket_id: str | None, intent: CaptureIntent
        ) -> ResolvedCapturePoint:
            del location, ticket_id, intent
            return self.point

    backend = OnecRuntimeBackend("runtime-zup", Session())  # type: ignore[arg-type]
    source = "Результат = 1;"
    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL,
        "cell-main",
        2,
        sha256(source.encode()).hexdigest(),
    )
    prepared = backend.prepare_main_for_capture(source, source_unit=unit)
    prepared = backend.activate_prepared_main_for_capture(prepared)
    result = CaptureService(tmp_path).run_until(
        backend,
        operation_id="op-1",
        prepared_main=prepared,
        source_revision=2,
        source_sha256=sha256(source.encode()).hexdigest(),
        points=(_point(),),
    )

    assert result.execution.terminal_state is AgentOperationState.UNKNOWN
    assert result.capture is None
    assert result.failure == {"stage": "capture_correlation", "partial_results": {}}


@pytest.mark.parametrize(
    "field,value",
    [
        ("project", "other_project"),
        ("module", "OtherModule"),
        ("procedure", "OtherProcedure"),
        ("line", 999),
        ("executable_line", 999),
        ("source_sha256", "c" * 64),
    ],
)
def test_capture_service_rejects_same_name_stop_with_wrong_resolved_source_identity(
    tmp_path: Path, field: str, value: object
) -> None:
    backend = _CaptureBackend()
    backend.stop_location_mutation = (field, value)

    result = CaptureService(tmp_path).run_until(
        backend,
        operation_id="op-1",
        prepared_main=backend.prepared,
        source_revision=2,
        source_sha256="a" * 64,
        points=(_point(),),
    )

    assert result.execution.terminal_state is AgentOperationState.UNKNOWN
    assert result.capture is None
    assert backend.calls == ["resolve", "arm", "run_prepared_main"]


def _write_capture_source(root: Path) -> str:
    source = "\n".join(
        (
            "Процедура СоздатьВТЗарплатаКВыплате() Экспорт",
            *("Строка = 1;" for _ in range(18)),
            "КонецПроцедуры",
        )
    )
    module = root / "CommonModules" / "Payroll"
    module.mkdir(parents=True)
    (module / "Payroll.mdo").write_text(
        '<mdclass:CommonModule xmlns:mdclass="urn:test" '
        'uuid="11111111-2222-3333-4444-555555555555"/>',
        encoding="utf-8",
    )
    (module / "Module.bsl").write_text(source, encoding="utf-8")
    return source


def test_capture_source_resolver_rejects_wrong_project_and_procedure(
    tmp_path: Path,
) -> None:
    session, _runtime_api, _source = _runtime_session_for_capture(tmp_path)

    with pytest.raises(ValueError, match="project"):
        session.resolve_capture_points((_point("before").__class__(
            name="before", project="other", module="Payroll", procedure="СоздатьВТЗарплатаКВыплате", line=17
        ),))
    with pytest.raises(ValueError, match="procedure"):
        session.resolve_capture_points((_point("before").__class__(
            name="before", project="zup", module="Payroll", procedure="Wrong", line=17
        ),))


def _runtime_session_for_capture(
    tmp_path: Path,
    *,
    fail_artifact: bool = False,
) -> tuple[RuntimeSession, object, str]:
    source = _write_capture_source(tmp_path)

    class RuntimeApi:
        def __init__(self) -> None:
            self.configured: tuple[object, ...] = ()
            self.prepared = 0

        def configure_capture_points(self, locations: tuple[object, ...]) -> None:
            self.configured = locations

        def prepare_capture_ticket(self) -> CaptureCorrelationTicket:
            self.prepared += 1
            return CaptureCorrelationTicket("private-runtime-ticket", 42, 1)

    class Artifacts:
        def __init__(self) -> None:
            self.records: list[dict[str, object]] = []

        def append_jsonl(self, _name: str, value: dict[str, object]) -> None:
            if fail_artifact:
                raise OSError("artifact write failed")
            self.records.append(value)

    session = object.__new__(RuntimeSession)
    session.runtime_api = RuntimeApi()
    session.artifacts = Artifacts()
    session._operation_lock = RLock()
    session._capture_locations = {}
    session._capture_source_resolver = None
    session._capture_source_bindings = {}
    session._active_capture_points = ()
    session._active_capture_locations = ()
    session._active_capture_ticket = None
    session.configure_capture_source("zup", tmp_path)
    return session, session.runtime_api, source


def _resolved_demo_point() -> CapturePointRequest:
    return CapturePointRequest(
        name="before",
        project="zup",
        module="Payroll",
        procedure="СоздатьВТЗарплатаКВыплате",
        line=17,
    )


def test_runtime_arm_artifact_failure_happens_before_live_capture_points(
    tmp_path: Path,
) -> None:
    session, runtime_api, _source = _runtime_session_for_capture(
        tmp_path, fail_artifact=True
    )
    points = session.resolve_capture_points((_resolved_demo_point(),))
    intent = CaptureIntent("intent-1", "op-1", 1, "a" * 64, 1, points)

    with pytest.raises(OSError, match="artifact write failed"):
        session.arm_capture_intent(intent)

    assert runtime_api.configured == ()
    assert session._active_capture_points == ()
    assert session._active_capture_ticket is None


def test_capture_service_journal_failure_disarms_runtime_session_and_keeps_ticket_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, runtime_api, source = _runtime_session_for_capture(tmp_path)
    backend = OnecRuntimeBackend("runtime-zup", AgentRuntimeSession(session))
    service = CaptureService(tmp_path)
    original_append = service._append
    calls = 0

    def fail_second_append(payload: dict[str, object]) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("fsync failed")
        original_append(payload)

    monkeypatch.setattr(service, "_append", fail_second_append)
    result = service.run_until(
        backend,
        operation_id="op-1",
        prepared_main=object(),
        source_revision=1,
        source_sha256=sha256(source.encode()).hexdigest(),
        points=(_resolved_demo_point(),),
    )

    assert result.failure == {"stage": "capture_arming", "partial_results": {}}
    assert result.execution.runtime_state == "capture_journal_failed"
    assert runtime_api.configured == ()
    assert session._active_capture_points == ()
    assert session._active_capture_ticket is None
    records = session.artifacts.records
    assert len(records) == 1
    assert records[0]["capture_ticket_planned"] is True
    assert records[0]["capture_generation"] == 1
    assert records[0]["source_revision"] == 1
    assert records[0]["source_sha256"] == sha256(source.encode()).hexdigest()

    def contains_private_ticket(value: object) -> bool:
        if isinstance(value, str):
            return "private-runtime-ticket" in value
        if isinstance(value, dict):
            return any(contains_private_ticket(item) for item in value.values())
        if isinstance(value, (list, tuple)):
            return any(contains_private_ticket(item) for item in value)
        return False

    assert contains_private_ticket(records) is False


class _ServiceCaptureBackend(_CaptureBackend):
    runtime_id = "runtime-capture"

    def __init__(self) -> None:
        super().__init__()
        self.closed = False
        self.prepared_provenance = None
        self.provenance_prepared = None

    def status(self) -> RuntimeDescriptor:
        return RuntimeDescriptor(
            self.runtime_id, 1, "ready", CapabilityMode.EXPERIMENT
        )

    def namespace_snapshot(self) -> RuntimeNamespaceSnapshot:
        return RuntimeNamespaceSnapshot(1, 1, ())

    def _seal_main_provenance(
        self,
        source: str,
        source_unit: object,
        *,
        prepared: object | None = None,
    ) -> None:
        digest = sha256(source.encode()).hexdigest()
        assert getattr(source_unit, "source_sha256") == digest
        mapped = mapped_visible_source(source, source_unit)  # type: ignore[arg-type]
        self.prepared_provenance = agent_contracts.OperationExecutionProvenance(
            visible_source_sha256=digest,
            executed_source_sha256=mapped.artifact.source_sha256,
            source_map_sha256=mapped.source_map_sha256,
            mode="main",
        )
        self.provenance_prepared = self.prepared if prepared is None else prepared

    def prepare_main_for_capture(
        self, source: str, *, source_unit: SourceUnitRef
    ) -> object:
        diagnostic = AgentWorkspaceService._prepare_bsl_source(
            source,
            source_unit=source_unit,
        )
        self.calls.append("prepare_main")
        if diagnostic is not None:
            raise MainPreparationSourceError(diagnostic)
        self._seal_main_provenance(source, source_unit)
        return self.prepared

    def prepared_main_execution_provenance(self, prepared: object) -> object:
        assert prepared is self.provenance_prepared
        assert self.prepared_provenance is not None
        return self.prepared_provenance

    def activate_prepared_main_for_capture(self, prepared: object) -> object:
        assert prepared is self.prepared
        self.calls.append("activate_main")
        return prepared

    def discard_prepared_main_for_capture(self, prepared: object) -> None:
        assert prepared is self.prepared
        self.calls.append("discard_main")

    def close(self) -> None:
        self.closed = True


class _CaptureFactory:
    def __init__(self, backend: _ServiceCaptureBackend) -> None:
        self.backend = backend

    def start(self, *, mode: CapabilityMode) -> _ServiceCaptureBackend:
        assert mode is CapabilityMode.EXPERIMENT
        return self.backend


def test_service_run_until_returns_the_correlated_durable_operation_view(tmp_path: Path) -> None:
    # Break caught: bypassing the service operation lane would return an
    # unjournaled capture or lose the operation/source identity in its view.
    source = "Результат = 1;"
    source_sha256 = sha256(source.encode()).hexdigest()
    notebook = tmp_path / "demo.ipynb"
    cell = nbformat.v4.new_code_cell(source=source, id="cell-main")
    cell.metadata["onec_runtime"] = {
        "revision": 1,
        "language": "bsl",
        "mode": "main",
        "source_sha256": source_sha256,
    }
    nbformat.write(nbformat.v4.new_notebook(cells=[cell]), notebook)
    backend = _ServiceCaptureBackend()
    service = AgentWorkspaceService(
        tmp_path, _CaptureFactory(backend), maximum_mode=CapabilityMode.EXPERIMENT
    )
    operation_id = ""
    try:
        _ready(service)
        assert service.call("code.list", {"container": "demo.ipynb"}).ok
        request = {
                "cell_id": "cell-main",
                "revision": 1,
                "source_sha256": source_sha256,
                "request_id": "capture-request-1",
                "points": [
                    {
                        "name": "before",
                        "project": "zup",
                        "module": "Payroll",
                        "procedure": "Run",
                        "line": 17,
                    }
                ],
                "wait_s": 2.0,
            }
        missing_key = dict(request)
        missing_key.pop("request_id")
        rejected = service.call("capture.run_until", missing_key)
        assert rejected.ok is False
        assert backend.calls == []
        result = service.call("capture.run_until", request)
        assert result.ok
        assert result.value.state is AgentOperationState.CAPTURED
        assert result.value.capture is not None
        assert backend.calls == [
            "prepare_main",
            "activate_main",
            "resolve",
            "arm",
            "run_prepared_main",
        ]
        # Simulates a caller that lost the first response: a durable request
        # identity attaches to the captured operation instead of re-arming.
        replay = service.call("capture.run_until", request)
        assert replay.ok
        assert replay.value.operation.operation_id == result.value.operation.operation_id
        operation_id = result.value.operation.operation_id
        assert backend.calls == [
            "prepare_main",
            "activate_main",
            "resolve",
            "arm",
            "run_prepared_main",
        ]
        conflicting = {
            **request,
            "points": [{**request["points"][0], "line": 18}],
        }
        conflict = service.call("capture.run_until", conflicting)
        assert conflict.ok is False
        assert backend.calls == [
            "prepare_main",
            "activate_main",
            "resolve",
            "arm",
            "run_prepared_main",
        ]
    finally:
        service.close()
    reopened_backend = _ServiceCaptureBackend()
    reopened = AgentWorkspaceService(
        tmp_path, _CaptureFactory(reopened_backend), maximum_mode=CapabilityMode.EXPERIMENT
    )
    try:
        assert reopened.call("code.list", {"container": "demo.ipynb"}).ok
        replay_after_restart = reopened.call("capture.run_until", request)
        assert replay_after_restart.ok
        assert replay_after_restart.value.operation.operation_id == operation_id
        assert reopened_backend.calls == []
    finally:
        reopened.close()


def test_run_until_source_failure_is_admitted_before_capture_activation(
    tmp_path: Path,
) -> None:
    source = "Результат = ;"
    digest = sha256(source.encode()).hexdigest()
    cell = nbformat.v4.new_code_cell(source=source, id="cell-invalid-capture-main")
    cell.metadata["onec_runtime"] = {
        "revision": 1,
        "language": "bsl",
        "mode": "main",
        "source_sha256": digest,
    }
    nbformat.write(nbformat.v4.new_notebook(cells=[cell]), tmp_path / "demo.ipynb")
    backend = _ServiceCaptureBackend()
    service = AgentWorkspaceService(
        tmp_path,
        _CaptureFactory(backend),
        maximum_mode=CapabilityMode.EXPERIMENT,
    )
    try:
        _ready(service)
        assert service.call("code.list", {"container": "demo.ipynb"}).ok

        response = service.call(
            "capture.run_until",
            {
                "cell_id": "cell-invalid-capture-main",
                "revision": 1,
                "source_sha256": digest,
                "request_id": "invalid-capture-main-request",
                "points": [
                    {
                        "name": "before",
                        "project": "zup",
                        "module": "Payroll",
                        "procedure": "Run",
                        "line": 17,
                    }
                ],
                "wait_s": 2.0,
            },
        )

        assert response.ok
        view = response.value
        assert view.state is AgentOperationState.FAILED
        assert view.failure["stage"] == "parsing"
        assert view.failure["state_changed"] == "no"
        assert view.failure["diagnostic"]["stage"] == "parsing"
        assert view.operation.cell_id == "cell-invalid-capture-main"
        assert view.operation.revision == 1
        assert view.operation.source_sha256 == digest
        assert backend.calls == ["prepare_main"]
    finally:
        service.close()


def test_run_until_prepares_main_before_resolution_and_uses_only_sealed_result(
    tmp_path: Path,
) -> None:
    source = "Результат = 1;"
    digest = sha256(source.encode()).hexdigest()
    cell = nbformat.v4.new_code_cell(source=source, id="cell-prepared-main")
    cell.metadata["onec_runtime"] = {
        "revision": 1,
        "language": "bsl",
        "mode": "main",
        "source_sha256": digest,
    }
    nbformat.write(nbformat.v4.new_notebook(cells=[cell]), tmp_path / "demo.ipynb")

    class PreparingBackend(_ServiceCaptureBackend):
        prepared = object()

        def prepare_main_for_capture(self, exact_source: str, *, source_unit: object) -> object:
            assert exact_source == source
            assert getattr(source_unit, "unit_id") == "cell-prepared-main"
            self.calls.append("prepare_main")
            self._seal_main_provenance(exact_source, source_unit)
            return self.prepared

        def run_main_until_capture(self, source: str, *, intent: CaptureIntent) -> CaptureRunOutcome:
            del source, intent
            pytest.fail("capture execution must not receive reparsable source")

        def run_prepared_main_until_capture(
            self, prepared: object, *, intent: CaptureIntent
        ) -> CaptureRunOutcome:
            assert prepared is self.prepared
            self.calls.append("run_prepared_main")
            stop = CaptureStop(
                1,
                intent.points[0],
                self.controller_operation_id,
                self.capture_ticket,
                self.controller_operation_id,
            )
            return CaptureRunOutcome(
                BackendExecution(AgentOperationState.CAPTURED, (), False, "captured"),
                stop,
            )

    backend = PreparingBackend()
    service = AgentWorkspaceService(
        tmp_path, _CaptureFactory(backend), maximum_mode=CapabilityMode.EXPERIMENT
    )
    try:
        _ready(service)
        assert service.call("code.list", {"container": "demo.ipynb"}).ok
        response = service.call(
            "capture.run_until",
            {
                "cell_id": "cell-prepared-main",
                "revision": 1,
                "source_sha256": digest,
                "request_id": "prepared-main-request",
                "points": [
                    {
                        "name": "before",
                        "project": "zup",
                        "module": "Payroll",
                        "procedure": "Run",
                        "line": 17,
                    }
                ],
                "wait_s": 2.0,
            },
        )

        assert response.ok
        assert response.value.state is AgentOperationState.CAPTURED
        assert backend.calls == [
            "prepare_main",
            "activate_main",
            "resolve",
            "arm",
            "run_prepared_main",
        ]
    finally:
        service.close()


def test_mixed_cell_worker_activation_precedes_capture_ticket_and_preserves_preparation(
    tmp_path: Path,
) -> None:
    """Break caught: Worker system MAINs must not consume the armed user ticket."""
    source = (
        "Функция Посчитать() Экспорт\n"
        "Возврат 42;\n"
        "КонецФункции;\n"
        "Результат = Посчитать();"
    )
    digest = sha256(source.encode()).hexdigest()
    cell = nbformat.v4.new_code_cell(source=source, id="cell-mixed-capture")
    cell.metadata["onec_runtime"] = {
        "revision": 1,
        "language": "bsl",
        "mode": "main",
        "source_sha256": digest,
    }
    nbformat.write(nbformat.v4.new_notebook(cells=[cell]), tmp_path / "demo.ipynb")

    class MixedCellBackend(_ServiceCaptureBackend):
        phase_one = object()
        phase_two = object()

        def __init__(self) -> None:
            super().__init__()
            self.controller_operation_id = 10
            self.build_calls = 0
            self.lower_calls = 0
            self.activation_calls = 0
            self.provenance_before_activation: list[object] = []
            self.provenance = agent_contracts.OperationExecutionProvenance(
                visible_source_sha256=digest,
                executed_source_sha256="b" * 64,
                source_map_sha256="c" * 64,
                mode="main",
                worker_generation=7,
                worker_manifest_sha256="d" * 64,
            )

        def prepare_main_for_capture(
            self, exact_source: str, *, source_unit: SourceUnitRef
        ) -> object:
            assert exact_source == source
            assert source_unit.source_sha256 == digest
            self.calls.append("prepare_main")
            self.build_calls += 1
            self.lower_calls += 1
            return self.phase_one

        def prepared_main_execution_provenance(self, prepared: object) -> object:
            assert prepared is self.phase_one
            return self.provenance

        def activate_prepared_main_for_capture(self, prepared: object) -> object:
            assert prepared is self.phase_one
            descriptor = service._operations.list()[-1]
            self.provenance_before_activation.append(
                service._operations.view_snapshot(
                    descriptor.operation_id
                ).execution_provenance
            )
            self.calls.append("activate_main")
            self.activation_calls += 1
            # Upload, connect, probe, and swap are trusted system MAINs.
            self.controller_operation_id += 4
            return self.phase_two

        def arm_capture(self, intent: CaptureIntent) -> CaptureArming:
            self.calls.append("arm")
            self.intent = intent
            expected = self.controller_operation_id + 1
            self.capture_ticket = f"runtime-ticket-{expected}"
            return CaptureArming(self.capture_ticket, expected, 1)

        def run_prepared_main_until_capture(
            self, prepared: object, *, intent: CaptureIntent
        ) -> CaptureRunOutcome:
            self.calls.append("run_prepared_main")
            # Model the old combined path until the Agent explicitly performs
            # the phase transition before capture resolution and arming.
            if prepared is self.phase_one:
                prepared = self.activate_prepared_main_for_capture(prepared)
            assert prepared is self.phase_two
            self.controller_operation_id += 1
            stop = CaptureStop(
                1,
                intent.points[0],
                self.controller_operation_id,
                self.capture_ticket,
                self.controller_operation_id,
            )
            return CaptureRunOutcome(
                BackendExecution(
                    AgentOperationState.CAPTURED, (), False, "captured"
                ),
                stop,
            )

    backend = MixedCellBackend()
    service = AgentWorkspaceService(
        tmp_path, _CaptureFactory(backend), maximum_mode=CapabilityMode.EXPERIMENT
    )
    try:
        _ready(service)
        assert service.call("code.list", {"container": "demo.ipynb"}).ok
        response = service.call(
            "capture.run_until",
            {
                "cell_id": "cell-mixed-capture",
                "revision": 1,
                "source_sha256": digest,
                "request_id": "mixed-capture-request",
                "points": [
                    {
                        "name": "before",
                        "project": "zup",
                        "module": "Payroll",
                        "procedure": "Run",
                        "line": 17,
                    }
                ],
                "wait_s": 2.0,
            },
        )

        assert response.ok
        assert response.value.state is AgentOperationState.CAPTURED
        assert backend.calls == [
            "prepare_main",
            "activate_main",
            "resolve",
            "arm",
            "run_prepared_main",
        ]
        assert backend.build_calls == 1
        assert backend.lower_calls == 1
        assert backend.activation_calls == 1
        assert backend.controller_operation_id == 15
        assert backend.provenance_before_activation == [backend.provenance]
        assert response.value.execution_provenance == backend.provenance
    finally:
        service.close()


def test_run_until_provenance_journal_failure_aborts_before_worker_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: capture Worker activation survives a failed provenance fsync."""
    source = "Результат = 1;"
    digest = sha256(source.encode()).hexdigest()
    cell = nbformat.v4.new_code_cell(source=source, id="cell-provenance-failure")
    cell.metadata["onec_runtime"] = {
        "revision": 1,
        "language": "bsl",
        "mode": "main",
        "source_sha256": digest,
    }
    nbformat.write(nbformat.v4.new_notebook(cells=[cell]), tmp_path / "demo.ipynb")

    class Backend(_ServiceCaptureBackend):
        def __init__(self) -> None:
            super().__init__()
            self.activation_calls = 0

        def prepared_main_execution_provenance(self, prepared: object) -> object:
            assert prepared is self.prepared
            return agent_contracts.OperationExecutionProvenance(
                digest,
                "b" * 64,
                "c" * 64,
                "main",
            )

        def activate_prepared_main_for_capture(self, prepared: object) -> object:
            self.activation_calls += 1
            return super().activate_prepared_main_for_capture(prepared)

    backend = Backend()
    service = AgentWorkspaceService(
        tmp_path,
        _CaptureFactory(backend),
        maximum_mode=CapabilityMode.EXPERIMENT,
    )
    try:
        _ready(service)
        assert service.call("code.list", {"container": "demo.ipynb"}).ok
        monkeypatch.setattr(
            service._operations,
            "set_execution_provenance",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("provenance fsync failed")
            ),
        )

        response = service.call(
            "capture.run_until",
            {
                "cell_id": "cell-provenance-failure",
                "revision": 1,
                "source_sha256": digest,
                "request_id": "capture-provenance-failure",
                "points": [
                    {
                        "name": "before",
                        "project": "zup",
                        "module": "Payroll",
                        "procedure": "Run",
                        "line": 17,
                    }
                ],
                "wait_s": 2.0,
            },
        )

        assert response.ok
        assert response.value.state is AgentOperationState.UNKNOWN
        assert backend.activation_calls == 0
        assert backend.calls == ["prepare_main"]
    finally:
        service.close()


def test_run_until_missing_provenance_capability_aborts_before_activation(
    tmp_path: Path,
) -> None:
    """Break caught: a legacy prepared MAIN can activate without a manifest."""
    source = "Результат = 1;"
    digest = sha256(source.encode()).hexdigest()
    cell = nbformat.v4.new_code_cell(source=source, id="cell-missing-provenance")
    cell.metadata["onec_runtime"] = {
        "revision": 1,
        "language": "bsl",
        "mode": "main",
        "source_sha256": digest,
    }
    nbformat.write(nbformat.v4.new_notebook(cells=[cell]), tmp_path / "demo.ipynb")

    class Backend(_ServiceCaptureBackend):
        prepared_main_execution_provenance = None

        def __init__(self) -> None:
            super().__init__()
            self.activation_calls = 0

        def activate_prepared_main_for_capture(self, prepared: object) -> object:
            self.activation_calls += 1
            return super().activate_prepared_main_for_capture(prepared)

    backend = Backend()
    service = AgentWorkspaceService(
        tmp_path,
        _CaptureFactory(backend),
        maximum_mode=CapabilityMode.EXPERIMENT,
    )
    try:
        _ready(service)
        assert service.call("code.list", {"container": "demo.ipynb"}).ok
        response = service.call(
            "capture.run_until",
            {
                "cell_id": "cell-missing-provenance",
                "revision": 1,
                "source_sha256": digest,
                "request_id": "capture-missing-provenance",
                "points": [
                    {
                        "name": "before",
                        "project": "zup",
                        "module": "Payroll",
                        "procedure": "Run",
                        "line": 17,
                    }
                ],
                "wait_s": 2.0,
            },
        )

        assert response.ok
        assert response.value.state is AgentOperationState.UNKNOWN
        assert response.value.execution_provenance is None
        assert backend.activation_calls == 0
        assert backend.calls == ["prepare_main"]
    finally:
        service.close()


def test_run_until_preparation_infrastructure_failure_is_bounded_unknown(
    tmp_path: Path,
) -> None:
    source = "Результат = 1;"
    digest = sha256(source.encode()).hexdigest()
    cell = nbformat.v4.new_code_cell(source=source, id="cell-preparation-fault")
    cell.metadata["onec_runtime"] = {
        "revision": 1,
        "language": "bsl",
        "mode": "main",
        "source_sha256": digest,
    }
    nbformat.write(nbformat.v4.new_notebook(cells=[cell]), tmp_path / "demo.ipynb")
    secret = "rdbg_pid=9182 token=do-not-persist"

    class FailingPreparationBackend(_ServiceCaptureBackend):
        def prepare_main_for_capture(self, exact_source: str, *, source_unit: object) -> object:
            del exact_source, source_unit
            self.calls.append("prepare_main")
            raise RuntimeError(secret)

    backend = FailingPreparationBackend()
    service = AgentWorkspaceService(
        tmp_path, _CaptureFactory(backend), maximum_mode=CapabilityMode.EXPERIMENT
    )
    try:
        _ready(service)
        assert service.call("code.list", {"container": "demo.ipynb"}).ok
        response = service.call(
            "capture.run_until",
            {
                "cell_id": "cell-preparation-fault",
                "revision": 1,
                "source_sha256": digest,
                "request_id": "preparation-fault-request",
                "points": [
                    {
                        "name": "before",
                        "project": "zup",
                        "module": "Payroll",
                        "procedure": "Run",
                        "line": 17,
                    }
                ],
                "wait_s": 2.0,
            },
        )

        assert response.ok
        view = response.value
        assert view.state is AgentOperationState.UNKNOWN
        assert view.failure == {
            "stage": "capture_preparation",
            "partial_results": {},
            "state_changed": "unknown",
        }
        assert tuple(action.method for action in view.recovery) == (
            "workspace.status",
            "runtime.close",
            "runtime.ensure",
        )
        assert backend.calls == ["prepare_main"]
        assert secret not in json.dumps(to_wire(view), ensure_ascii=False)
    finally:
        service.close()


def test_run_until_worker_activation_failure_is_unknown_and_never_arms(
    tmp_path: Path,
) -> None:
    source = (
        "Функция Посчитать() Экспорт\n"
        "Возврат 42;\n"
        "КонецФункции;\n"
        "Результат = Посчитать();"
    )
    digest = sha256(source.encode()).hexdigest()
    cell = nbformat.v4.new_code_cell(source=source, id="cell-activation-fault")
    cell.metadata["onec_runtime"] = {
        "revision": 1,
        "language": "bsl",
        "mode": "main",
        "source_sha256": digest,
    }
    nbformat.write(nbformat.v4.new_notebook(cells=[cell]), tmp_path / "demo.ipynb")
    secret = "worker_pid=9182 bearer=do-not-persist"

    class FailingActivationBackend(_ServiceCaptureBackend):
        phase_one = object()

        def prepare_main_for_capture(
            self, exact_source: str, *, source_unit: SourceUnitRef
        ) -> object:
            assert exact_source == source
            assert source_unit.source_sha256 == digest
            self.calls.append("prepare_main")
            self._seal_main_provenance(
                exact_source,
                source_unit,
                prepared=self.phase_one,
            )
            return self.phase_one

        def activate_prepared_main_for_capture(self, prepared: object) -> object:
            assert prepared is self.phase_one
            self.calls.append("activate_main")
            raise RuntimeError(secret)

    backend = FailingActivationBackend()
    service = AgentWorkspaceService(
        tmp_path, _CaptureFactory(backend), maximum_mode=CapabilityMode.EXPERIMENT
    )
    try:
        _ready(service)
        assert service.call("code.list", {"container": "demo.ipynb"}).ok
        response = service.call(
            "capture.run_until",
            {
                "cell_id": "cell-activation-fault",
                "revision": 1,
                "source_sha256": digest,
                "request_id": "activation-fault-request",
                "points": [
                    {
                        "name": "before",
                        "project": "zup",
                        "module": "Payroll",
                        "procedure": "Run",
                        "line": 17,
                    }
                ],
                "wait_s": 2.0,
            },
        )

        assert response.ok
        view = response.value
        assert view.state is AgentOperationState.UNKNOWN
        assert view.failure == {
            "stage": "capture_activation",
            "partial_results": {},
            "state_changed": "unknown",
        }
        assert tuple(action.method for action in view.recovery) == (
            "workspace.status",
            "runtime.close",
            "runtime.ensure",
        )
        assert backend.calls == ["prepare_main", "activate_main"]
        assert secret not in json.dumps(to_wire(view), ensure_ascii=False)
    finally:
        service.close()


def test_run_until_resolution_failure_after_activation_is_bounded_unknown(
    tmp_path: Path,
) -> None:
    source = "Результат = 1;"
    digest = sha256(source.encode()).hexdigest()
    cell = nbformat.v4.new_code_cell(source=source, id="cell-resolution-fault")
    cell.metadata["onec_runtime"] = {
        "revision": 1,
        "language": "bsl",
        "mode": "main",
        "source_sha256": digest,
    }
    nbformat.write(nbformat.v4.new_notebook(cells=[cell]), tmp_path / "demo.ipynb")
    secret = "resolver_pid=9182 token=do-not-persist"

    class FailingResolutionBackend(_ServiceCaptureBackend):
        def resolve_capture_points(
            self, points: tuple[CapturePointRequest, ...]
        ) -> tuple[ResolvedCapturePoint, ...]:
            del points
            self.calls.append("resolve")
            raise RuntimeError(secret)

    backend = FailingResolutionBackend()
    service = AgentWorkspaceService(
        tmp_path, _CaptureFactory(backend), maximum_mode=CapabilityMode.EXPERIMENT
    )
    try:
        _ready(service)
        assert service.call("code.list", {"container": "demo.ipynb"}).ok
        response = service.call(
            "capture.run_until",
            {
                "cell_id": "cell-resolution-fault",
                "revision": 1,
                "source_sha256": digest,
                "request_id": "resolution-fault-request",
                "points": [
                    {
                        "name": "before",
                        "project": "zup",
                        "module": "Payroll",
                        "procedure": "Run",
                        "line": 17,
                    }
                ],
                "wait_s": 2.0,
            },
        )

        assert response.ok
        view = response.value
        assert view.state is AgentOperationState.UNKNOWN
        assert view.failure == {
            "stage": "capture_setup_after_activation",
            "partial_results": {},
            "state_changed": "unknown",
        }
        assert tuple(action.method for action in view.recovery) == (
            "workspace.status",
            "runtime.close",
            "runtime.ensure",
        )
        assert backend.calls == [
            "prepare_main",
            "activate_main",
            "resolve",
            "discard_main",
        ]
        assert secret not in json.dumps(to_wire(view), ensure_ascii=False)
    finally:
        service.close()


_MIXED_CAPTURE_SETUP_SOURCE = (
    "\u0424\u0443\u043d\u043a\u0446\u0438\u044f \u041f\u043e\u0441\u0447\u0438\u0442\u0430\u0442\u044c() \u042d\u043a\u0441\u043f\u043e\u0440\u0442\n"
    "\u0412\u043e\u0437\u0432\u0440\u0430\u0442 42;\n"
    "\u041a\u043e\u043d\u0435\u0446\u0424\u0443\u043d\u043a\u0446\u0438\u0438;\n"
    "\u0420\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442 = \u041f\u043e\u0441\u0447\u0438\u0442\u0430\u0442\u044c();"
)


class _PostActivationSetupFailureBackend(_ServiceCaptureBackend):
    def __init__(self) -> None:
        super().__init__()
        self.phase_one = object()
        self.phase_two = object()
        self.phase_two_discarded = False

    def prepare_main_for_capture(
        self, source: str, *, source_unit: SourceUnitRef
    ) -> object:
        assert source == _MIXED_CAPTURE_SETUP_SOURCE
        assert source_unit.source_sha256 == sha256(source.encode()).hexdigest()
        self.calls.append("prepare_main")
        self._seal_main_provenance(
            source,
            source_unit,
            prepared=self.phase_one,
        )
        return self.phase_one

    def activate_prepared_main_for_capture(self, prepared: object) -> object:
        assert prepared is self.phase_one
        self.calls.append("activate_main")
        return self.phase_two

    def discard_prepared_main_for_capture(self, prepared: object) -> None:
        assert prepared is self.phase_two
        assert self.phase_two_discarded is False
        self.phase_two_discarded = True
        self.calls.append("discard_main")

    def run_prepared_main_until_capture(
        self, prepared: object, *, intent: CaptureIntent
    ) -> CaptureRunOutcome:
        del prepared, intent
        self.calls.append("run_prepared_main")
        raise AssertionError("post-activation setup failure dispatched the user MAIN")


def _capture_setup_service(
    tmp_path: Path, backend: _PostActivationSetupFailureBackend
) -> tuple[AgentWorkspaceService, str]:
    digest = sha256(_MIXED_CAPTURE_SETUP_SOURCE.encode()).hexdigest()
    cell = nbformat.v4.new_code_cell(
        source=_MIXED_CAPTURE_SETUP_SOURCE,
        id="cell-post-activation-setup",
    )
    cell.metadata["onec_runtime"] = {
        "revision": 1,
        "language": "bsl",
        "mode": "main",
        "source_sha256": digest,
    }
    nbformat.write(nbformat.v4.new_notebook(cells=[cell]), tmp_path / "demo.ipynb")
    service = AgentWorkspaceService(
        tmp_path,
        _CaptureFactory(backend),
        maximum_mode=CapabilityMode.EXPERIMENT,
    )
    _ready(service)
    assert service.call("code.list", {"container": "demo.ipynb"}).ok
    return service, digest


def _run_post_activation_setup_failure(
    service: AgentWorkspaceService, digest: str, request_id: str
) -> object:
    response = service.call(
        "capture.run_until",
        {
            "cell_id": "cell-post-activation-setup",
            "revision": 1,
            "source_sha256": digest,
            "request_id": request_id,
            "points": [
                {
                    "name": "before",
                    "project": "zup",
                    "module": "Payroll",
                    "procedure": "Run",
                    "line": 17,
                }
            ],
            "wait_s": 2.0,
        },
    )
    assert response.ok
    return response.value


def _assert_post_activation_setup_is_quarantined(
    service: AgentWorkspaceService,
    backend: _PostActivationSetupFailureBackend,
    view: object,
    *,
    secret: str,
) -> None:
    assert view.state is AgentOperationState.UNKNOWN  # type: ignore[attr-defined]
    assert view.failure == {  # type: ignore[attr-defined]
        "stage": "capture_setup_after_activation",
        "partial_results": {},
        "state_changed": "unknown",
    }
    assert tuple(action.method for action in view.recovery) == (  # type: ignore[attr-defined]
        "workspace.status",
        "runtime.close",
        "runtime.ensure",
    )
    assert service._runtime is not None and service._runtime.closing is True
    assert backend.phase_two_discarded is True
    assert "run_prepared_main" not in backend.calls
    assert "continue" not in backend.calls
    assert secret not in json.dumps(to_wire(view), ensure_ascii=False)


def test_run_until_arm_exception_after_activation_quarantines_generation(
    tmp_path: Path,
) -> None:
    """Break caught: handled arm exceptions must not publish usable FAILED state."""
    secret = "arm_pid=9182 bearer=do-not-persist"

    class ArmExceptionBackend(_PostActivationSetupFailureBackend):
        def arm_capture(self, intent: CaptureIntent) -> CaptureArming:
            del intent
            self.calls.append("arm")
            raise RuntimeError(secret)

    backend = ArmExceptionBackend()
    service, digest = _capture_setup_service(tmp_path, backend)
    try:
        view = _run_post_activation_setup_failure(
            service, digest, "post-activation-arm-exception"
        )

        _assert_post_activation_setup_is_quarantined(
            service, backend, view, secret=secret
        )
        assert backend.calls == [
            "prepare_main",
            "activate_main",
            "resolve",
            "arm",
            "disarm",
            "discard_main",
        ]
    finally:
        service.close()


def test_run_until_invalid_arming_after_activation_quarantines_generation(
    tmp_path: Path,
) -> None:
    """Break caught: invalid returned arming evidence must quarantine activation."""
    secret = "invalid_arm_pid=9182 bearer=do-not-persist"

    class InvalidArmingBackend(_PostActivationSetupFailureBackend):
        def arm_capture(self, intent: CaptureIntent) -> CaptureArming:
            del intent
            self.calls.append("arm")
            return CaptureArming(secret, 0, 0)

    backend = InvalidArmingBackend()
    service, digest = _capture_setup_service(tmp_path, backend)
    try:
        view = _run_post_activation_setup_failure(
            service, digest, "post-activation-invalid-arming"
        )

        _assert_post_activation_setup_is_quarantined(
            service, backend, view, secret=secret
        )
        assert backend.calls == [
            "prepare_main",
            "activate_main",
            "resolve",
            "arm",
            "disarm",
            "discard_main",
        ]
    finally:
        service.close()


def test_run_until_arming_journal_failure_after_activation_quarantines_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Break caught: handled arming-journal faults must quarantine activation."""
    secret = "journal_pid=9182 bearer=do-not-persist"
    backend = _PostActivationSetupFailureBackend()
    service, digest = _capture_setup_service(tmp_path, backend)

    def fail_arming_journal(intent: CaptureIntent, arming: CaptureArming) -> None:
        del intent, arming
        raise OSError(secret)

    monkeypatch.setattr(service._capture, "_journal_arming", fail_arming_journal)
    try:
        view = _run_post_activation_setup_failure(
            service, digest, "post-activation-arming-journal"
        )

        _assert_post_activation_setup_is_quarantined(
            service, backend, view, secret=secret
        )
        assert backend.calls == [
            "prepare_main",
            "activate_main",
            "resolve",
            "arm",
            "disarm",
            "discard_main",
        ]
    finally:
        service.close()


def test_stale_phase_two_validation_before_controller_call_quarantines_generation(
    tmp_path: Path,
) -> None:
    """Break caught: RuntimeApi pre-dispatch drift must quarantine the changed Worker."""
    from collections import deque

    from onec_runtime.bsl.parser_target import PythonParserTarget
    from onec_runtime.bsl.semantic_lowering import SemanticNotebookLowerer
    from onec_runtime.config import RuntimeConfig
    from onec_runtime.prototype_runtime import (
        CaptureCellResult,
        MainCompletion,
        OperationHandle,
    )
    from onec_runtime.runtime_api import PrototypeRuntimeApi
    from onec_runtime.server_worker import NotebookWorkerArtifactBuilder

    platform = tmp_path / "platform"
    platform.mkdir()
    for executable in ("1cv8.exe", "1cv8c.exe", "dbgs.exe"):
        (platform / executable).write_bytes(b"stub")

        def worker_universe_result(source: str) -> object | None:
            from onec_runtime.worker_stage_protocol import (
                WORKER_STAGE_SCHEMA,
                WORKER_STAGE_SCHEMA_VERSION,
            )

            if f'"{WORKER_STAGE_SCHEMA}"' in source:
                header = re.search(
                    r'"onec-worker-stage-batch-receipt", 2, '
                    r'"([0-9a-f-]{36})", (\d+), (\d+), "([0-9a-f]{64})", '
                    r'СтатусWorker',
                    source,
                )
                entries = re.findall(
                    r'Новый Структура\('
                    r'"registration_name,artifact_sha256,temp_storage_url", '
                    r'"([A-Za-z_][A-Za-z0-9_]*)", "([0-9a-f]{64})", '
                    r'АдресАртефактаWorker(\d+)\);',
                    source,
                )
                assert header is not None
                transaction_id, batch_index, batch_count, batch_digest = header.groups()
                return json.dumps(
                    {
                        "schema": WORKER_STAGE_SCHEMA,
                        "schema_version": WORKER_STAGE_SCHEMA_VERSION,
                        "transaction_id": transaction_id,
                        "batch_index": int(batch_index),
                        "batch_count": int(batch_count),
                        "batch_digest": batch_digest,
                        "status": "succeeded",
                        "connected": [
                            {
                                "registration_name": registration,
                                "artifact_sha256": artifact_sha256,
                                "temp_storage_url": (
                                    f"e1cib/tempstorage/{transaction_id}-{item_index}"
                                    "?seanceId=agent-capture-test"
                                ),
                            }
                            for registration, artifact_sha256, item_index in entries
                        ],
                        "failure": False,
                    },
                    separators=(",", ":"),
                )
            if "onec-worker-root-prepare-stage=" in source:
                transaction = re.search(
                    r"onec-worker-prepared-root-receipt-v1\|([0-9a-f-]{36})\|",
                    source,
                )
                generation = re.search(r'Вставить\("Generation", (\d+)\);', source)
                manifest = re.search(
                    r'Вставить\("ManifestSha256", "([0-9a-f]{64})"\);',
                    source,
                )
                root = re.search(
                    r'Вставить\("CandidateRootKey", "([^"|]+)"\);',
                    source,
                )
                previous = re.search(
                    r'Вставить\("PreviousRootKey", "([^"|]*)"\);',
                    source,
                )
                assert transaction and generation and manifest and root and previous
                return (
                    "onec-worker-prepared-root-receipt-v1|"
                    f"{transaction.group(1)}|{generation.group(1)}|{manifest.group(1)}|"
                    f"{root.group(1)}|{previous.group(1) or '-'}|13"
                )
            if "onec-worker-root-swap-stage=guard" in source:
                transaction = re.search(
                    r"onec-worker-root-swap-receipt-v1\|([0-9a-f-]{36})\|",
                    source,
                )
                generation = re.search(
                    r'Формат\((\d+), "ЧГ=0; ЧДЦ=0; ЧН=0"\)', source
                )
                identity = re.search(
                    r'"([0-9a-f]{64})\|(generation-\d+)\|([^|" ]+)\|1\|"',
                    source,
                )
                assert transaction and generation and identity
                return (
                    "onec-worker-root-swap-receipt-v1|"
                    f"{transaction.group(1)}|{generation.group(1)}|{identity.group(1)}|"
                    f"{identity.group(2)}|{identity.group(3)}|1|13|2"
                )
            return None

    class Controller:
        runtime_generation = 1

        def __init__(self) -> None:
            self.state = OperationState.COMPLETED
            self.operation_id = 0
            self.stop_sequence = 0
            self.lowerer = SemanticNotebookLowerer(
                PythonParserTarget.from_generated()
            )
            self.worker_results = deque((True,))
            self.system_main_calls = 0
            self.user_main_calls = 0

        def execute_system_main(self, source: str) -> MainCompletion:
            self.system_main_calls += 1
            self.operation_id += 1
            operation = OperationHandle(self.operation_id, source, source)
            universe_result = worker_universe_result(source)
            return MainCompletion(
                operation,
                (
                    universe_result
                    if universe_result is not None
                    else self.worker_results.popleft()
                ),
                "",
                True,
            )

        def execute_system_capture(self, source: str) -> CaptureCellResult:
            return CaptureCellResult(
                self.operation_id,
                source,
                source,
                self.worker_results.popleft(),
            )

        def execute_main(self, source: str, **kwargs: object) -> MainCompletion:
            del source, kwargs
            self.user_main_calls += 1
            raise AssertionError("stale phase two entered controller user MAIN")

        def execute_mapped_main(
            self,
            _visible_source: str,
            mapped_source: object,
            **kwargs: object,
        ) -> MainCompletion:
            return self.execute_main(mapped_source.text, **kwargs)  # type: ignore[attr-defined]

    controller = Controller()
    api = PrototypeRuntimeApi(
        controller,  # type: ignore[arg-type]
        notebook_worker_builder=NotebookWorkerArtifactBuilder(
            RuntimeConfig(tmp_path, platform)
        ),
    )

    class StalePhaseTwoSession(RuntimeSession):
        def __init__(self) -> None:
            self.runtime_api = api
            self._operation_lock = RLock()
            self._closed = False
            self.disarm_calls: list[str] = []
            self.runtime_activated: object | None = None

        def execute_prepared_main_for_capture(self, prepared: object) -> object:
            self.runtime_activated = prepared
            return super().execute_prepared_main_for_capture(prepared)

        def resolve_capture_points(
            self, points: tuple[CapturePointRequest, ...]
        ) -> tuple[ResolvedCapturePoint, ...]:
            return _CaptureBackend().resolve_capture_points(points)

        def arm_capture_intent(self, intent: CaptureIntent) -> CaptureArming:
            del intent
            api.configure_capture_points(
                (ModuleLocation("ConfigModule", "", uuid4(), uuid4(), 17),)
            )
            ticket = api.prepare_capture_ticket()
            controller.operation_id += 1
            return CaptureArming(
                ticket.ticket_id,
                ticket.expected_operation_id,
                ticket.expected_stop_sequence,
            )

        def disarm_capture_intent(self, *, policy: str) -> None:
            self.disarm_calls.append(policy)

        def owned_process_snapshot(self) -> tuple[dict[str, object], ...]:
            return ()

        def close(self) -> None:
            self._closed = True

    digest = sha256(_MIXED_CAPTURE_SETUP_SOURCE.encode()).hexdigest()
    cell = nbformat.v4.new_code_cell(
        source=_MIXED_CAPTURE_SETUP_SOURCE,
        id="cell-stale-phase-two",
    )
    cell.metadata["onec_runtime"] = {
        "revision": 1,
        "language": "bsl",
        "mode": "main",
        "source_sha256": digest,
    }
    nbformat.write(nbformat.v4.new_notebook(cells=[cell]), tmp_path / "demo.ipynb")
    session = StalePhaseTwoSession()
    backend = OnecRuntimeBackend(
        "runtime-stale-phase-two",
        session,
        mode=CapabilityMode.EXPERIMENT,
    )
    service = AgentWorkspaceService(
        tmp_path,
        _ZupFactory(backend),
        maximum_mode=CapabilityMode.EXPERIMENT,
    )
    try:
        _ready(service)
        assert service.call("code.list", {"container": "demo.ipynb"}).ok

        response = service.call(
            "capture.run_until",
            {
                "cell_id": "cell-stale-phase-two",
                "revision": 1,
                "source_sha256": digest,
                "request_id": "stale-phase-two-validation",
                "points": [
                    {
                        "name": "before",
                        "project": "zup",
                        "module": "Payroll",
                        "procedure": "Run",
                        "line": 17,
                    }
                ],
                "wait_s": 2.0,
            },
        )

        assert response.ok
        view = response.value
        assert view.state is AgentOperationState.UNKNOWN
        assert view.failure == {
            "stage": "capture_setup_after_activation",
            "partial_results": {},
            "state_changed": "unknown",
        }
        assert tuple(action.method for action in view.recovery) == (
            "workspace.status",
            "runtime.close",
            "runtime.ensure",
        )
        assert service._runtime is not None and service._runtime.closing is True
        assert api.worker_generation_handle is not None
        assert controller.system_main_calls == 3
        assert controller.user_main_calls == 0
        assert session.disarm_calls == ["terminal_no_stop"]
        assert session.runtime_activated is not None
        with pytest.raises(ProtocolError, match="already consumed"):
            api.execute_prepared_main_for_capture(session.runtime_activated)
        assert "user_main_dispatched" not in json.dumps(
            to_wire(view), ensure_ascii=False
        )
    finally:
        service.close()


def test_run_until_request_journal_failure_terminates_submission_and_releases_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A post-submit request-journal fault must not strand the sole lane."""
    source = "Результат = 1;"
    source_sha256 = sha256(source.encode()).hexdigest()
    cell = nbformat.v4.new_code_cell(source=source, id="cell-main")
    cell.metadata["onec_runtime"] = {
        "revision": 1,
        "language": "bsl",
        "mode": "main",
        "source_sha256": source_sha256,
    }
    nbformat.write(nbformat.v4.new_notebook(cells=[cell]), tmp_path / "demo.ipynb")
    backend = _ServiceCaptureBackend()
    service = AgentWorkspaceService(
        tmp_path, _CaptureFactory(backend), maximum_mode=CapabilityMode.EXPERIMENT
    )
    gates: list[object] = []
    original_startup = service._startup_operation_id

    def traced_startup(gate, holder):  # type: ignore[no-untyped-def]
        gates.append(gate)
        return original_startup(gate, holder)

    original_record = service._capture.record_request
    failures = [OSError("request journal fsync failed after submit")]

    def fail_once(request_id: str, fingerprint: str, operation_id: str) -> None:
        if failures:
            raise failures.pop()
        original_record(request_id, fingerprint, operation_id)

    monkeypatch.setattr(service, "_startup_operation_id", traced_startup)
    monkeypatch.setattr(service._capture, "record_request", fail_once)
    failed_operation_id = ""
    try:
        _ready(service)
        assert service.call("code.list", {"container": "demo.ipynb"}).ok
        request = {
            "cell_id": "cell-main",
            "revision": 1,
            "source_sha256": source_sha256,
            "request_id": "journal-failure-run-until",
            "points": [
                {
                    "name": "before",
                    "project": "zup",
                    "module": "Payroll",
                    "procedure": "Run",
                    "line": 17,
                }
            ],
            "wait_s": 0.05,
        }

        failed = service.call("capture.run_until", request)
        follow_up = service._operations.submit(
            {
                "operation_kind": "code_run",
                "runtime_id": backend.runtime_id,
                "runtime_generation": 1,
                "code_id": "after-journal-failure",
                "revision": 1,
                "source_sha256": "f" * 64,
                "inputs_sha256": "follow-up-run-until",
            },
            lambda: BackendExecution(
                AgentOperationState.COMPLETED, (), False, "ready"
            ),
        )
        follow_up = service._operations.wait(follow_up.operation_id, 0.05)

        assert (failed.ok, follow_up.state) == (
            True,
            AgentOperationState.COMPLETED,
        )
        assert failed.value.state is AgentOperationState.FAILED
        assert failed.value.failure == {
            "stage": "capture_request_journal",
            "partial_results": {},
        }
        failed_operation_id = failed.value.operation.operation_id
        assert backend.calls == []
        replay = service.call("capture.run_until", request)
        assert replay.ok
        assert replay.value.operation.operation_id == failed.value.operation.operation_id
        assert replay.value.state is AgentOperationState.FAILED
        assert backend.calls == []
    finally:
        for gate in gates:
            gate.set()  # type: ignore[attr-defined]
        service.close()

    reopened_backend = _ServiceCaptureBackend()
    reopened = AgentWorkspaceService(
        tmp_path,
        _CaptureFactory(reopened_backend),
        maximum_mode=CapabilityMode.EXPERIMENT,
    )
    try:
        assert reopened.call("code.list", {"container": "demo.ipynb"}).ok
        durable_replay = reopened.call("capture.run_until", request)
        assert durable_replay.ok
        assert durable_replay.value.operation.operation_id == failed_operation_id
        assert durable_replay.value.state is AgentOperationState.FAILED
        assert durable_replay.value.failure == {
            "stage": "capture_request_journal",
            "partial_results": {},
        }
        assert reopened_backend.calls == []
    finally:
        reopened.close()


class _SessionWithExecutionError:
    def __init__(self) -> None:
        self.sources: list[str] = []

    def execute_bsl(
        self,
        source: str,
        *,
        source_unit: SourceUnitRef | None = None,
        on_execution_provenance=None,  # type: ignore[no-untyped-def]
    ) -> RuntimeReply:
        diagnostic = AgentWorkspaceService._prepare_bsl_source(source)
        if diagnostic is not None:
            return RuntimeReply(
                RuntimeReplyKind.SOURCE_FAILED,
                1,
                OperationState.FAILED,
                error=diagnostic.runtime_summary,
                succeeded=False,
                diagnostic=diagnostic,
            )
        assert source_unit is not None
        if on_execution_provenance is not None:
            on_execution_provenance(
                agent_contracts.OperationExecutionProvenance(
                    visible_source_sha256=source_unit.source_sha256,
                    executed_source_sha256="b" * 64,
                    source_map_sha256="c" * 64,
                    mode="main",
                )
            )
        self.sources.append(source)
        raise BslExecutionError("compile error", messages=("compile error",))

    def status(self) -> RuntimeStatus:
        return RuntimeStatus(OperationState.COMPLETED, 1, 1, None)

    def namespace_snapshot(self) -> RuntimeNamespaceSnapshot:
        return RuntimeNamespaceSnapshot(1, 1, ())

    def owned_process_snapshot(self) -> tuple[dict[str, object], ...]:
        return ()

    def close(self) -> None:
        pass


class _ZupFactory:
    def __init__(self, backend: OnecRuntimeBackend) -> None:
        self.backend = backend

    def start(self, *, mode: CapabilityMode) -> OnecRuntimeBackend:
        return self.backend


def _zup_service(
    tmp_path: Path, *, saved_source: str | None = None
) -> tuple[AgentWorkspaceService, _SessionWithExecutionError]:
    notebook = tmp_path / "demo.ipynb"
    cells = []
    if saved_source is not None:
        cell = nbformat.v4.new_code_cell(source=saved_source, id="cell-main")
        cell.metadata["onec_runtime"] = {
            "revision": 1,
            "language": "bsl",
            "mode": "main",
            "source_sha256": sha256(saved_source.encode()).hexdigest(),
        }
        cells.append(cell)
    nbformat.write(nbformat.v4.new_notebook(cells=cells), notebook)
    session = _SessionWithExecutionError()
    backend = OnecRuntimeBackend(
        "runtime-1", session, mode=CapabilityMode.EXPERIMENT
    )  # type: ignore[arg-type]
    return (
        AgentWorkspaceService(
            tmp_path, _ZupFactory(backend), maximum_mode=CapabilityMode.EXPERIMENT
        ),
        session,
    )


def _ready(service: AgentWorkspaceService) -> None:
    response = service.call("runtime.ensure", {"mode": "experiment"})
    assert response.ok
    if response.value.__class__.__name__ == "OperationDescriptor":
        waited = service.call(
            "operation.wait", {"operation_id": response.value.operation_id, "timeout_s": 2.0}
        )
        assert waited.ok
    runtime = service._runtime
    assert runtime is not None
    selected = service.call("runtime.select", {"runtime_id": runtime.runtime_id})
    assert selected.ok


@pytest.mark.parametrize(
    ("source", "stage"),
    [("Результат = ;", "parsing"), ("КонтекстОтладки.Значение = 1;", "lowering")],
)
def test_invalid_inline_bsl_is_admitted_before_preparation_without_target_execution(
    tmp_path: Path, source: str, stage: str
) -> None:
    service, session = _zup_service(tmp_path)
    try:
        _ready(service)
        response = service.call(
            "code.run_inline",
            {
                "language": "bsl",
                "mode": "main",
                "source": source,
                "inputs": {},
                "wait_s": 2.0,
            },
        )
        assert response.ok
        view = service.call(
            "operation.view", {"operation_id": response.value.operation_id}
        ).value
        assert view.state is AgentOperationState.FAILED
        assert view.failure["state_changed"] == "no"
        assert view.failure["stage"] == stage
        assert view.failure["diagnostic"]["stage"] == stage
        assert session.sources == []
        assert len(service._inline) == 1
        inline = next(iter(service._inline.values()))
        assert view.operation.cell_id == inline.cell_id
        assert view.operation.revision == inline.revision
        assert view.operation.source_sha256 == inline.source_sha256
    finally:
        service.close()


def test_invalid_saved_bsl_is_admitted_before_preparation_without_target_execution(
    tmp_path: Path,
) -> None:
    source = "Результат = ;"
    service, session = _zup_service(tmp_path, saved_source=source)
    try:
        _ready(service)
        assert service.call("code.list", {"container": "demo.ipynb"}).ok
        response = service.call(
            "code.run",
            {
                "cell_id": "cell-main",
                "revision": 1,
                "source_sha256": sha256(source.encode()).hexdigest(),
                "inputs": {},
                "wait_s": 2.0,
            },
        )
        assert response.ok
        view = service.call(
            "operation.view", {"operation_id": response.value.operation_id}
        ).value
        assert view.state is AgentOperationState.FAILED
        assert view.failure["state_changed"] == "no"
        assert view.failure["stage"] == "parsing"
        assert view.failure["diagnostic"]["stage"] == "parsing"
        assert view.operation.cell_id == "cell-main"
        assert view.operation.revision == 1
        assert view.operation.source_sha256 == sha256(source.encode()).hexdigest()
        assert session.sources == []
    finally:
        service.close()


def test_proven_main_bsl_execution_error_has_execution_stage(tmp_path: Path) -> None:
    service, session = _zup_service(tmp_path)
    try:
        _ready(service)
        response = service.call(
            "code.run_inline",
            {"language": "bsl", "mode": "main", "source": "Результат = 1;", "inputs": {}, "wait_s": 2.0},
        )
        assert response.ok, response.failure.current_state
        view = service.call("operation.view", {"operation_id": response.value.operation_id}).value
        assert view.state is AgentOperationState.FAILED
        assert view.failure["stage"] == "execution"
        assert view.messages == ("BSL execution failed",)
        assert session.sources == ["Результат = 1;"]
    finally:
        service.close()


def test_backend_bounds_collected_messages_and_suppresses_raw_reply_error() -> None:
    class _ReplySession:
        def execute_bsl(self, source: str) -> RuntimeReply:
            del source
            return RuntimeReply(
                RuntimeReplyKind.MAIN_COMPLETED,
                9,
                OperationState.FAILED,
                error="compiler diagnostic",
                succeeded=False,
                messages=("x" * 5000,) * 101,
            )

    backend = OnecRuntimeBackend("runtime-1", _ReplySession())  # type: ignore[arg-type]

    outcome = backend.execute_bsl("Результат = 1;")

    assert outcome.terminal_state is AgentOperationState.FAILED
    assert outcome.failure_stage == "execution"
    assert len(outcome.messages) <= 100
    assert all(len(message) <= 4096 for message in outcome.messages)

    class _ErrorOnlySession:
        def execute_bsl(self, source: str) -> RuntimeReply:
            del source
            return RuntimeReply(
                RuntimeReplyKind.MAIN_COMPLETED, 10, OperationState.FAILED,
                error="compiler diagnostic", succeeded=False,
            )

    error_only = OnecRuntimeBackend("runtime-2", _ErrorOnlySession())  # type: ignore[arg-type]
    assert error_only.execute_bsl("Результат = 1;").messages == (
        "BSL execution failed",
    )
