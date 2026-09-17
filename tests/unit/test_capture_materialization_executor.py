"""One CAPTURE transfer owns its temporary key and every RDBG helper step."""

from dataclasses import dataclass
from hashlib import sha256
from threading import get_ident
from typing import Callable
from uuid import UUID

import pytest

from onec_runtime.capture_evaluation import AdmissionEnvelopeV1, CaptureTransferPlan
from onec_runtime.errors import (
    CaptureValueAccessDeniedError,
    CaptureValueCheckError,
    RdbgTransportTimeout,
)
from onec_runtime.execution.arbiter import RdbgArbiter, RouteToken, Settlement
from onec_runtime.execution.capture.operation_executor import (
    ConfirmedTemporaryKeyCleanupFailure,
    TemporaryKeyCleanupOutcomeUnknown,
)
from onec_runtime.execution.capture.resources import TemporaryCleanupState
from onec_runtime.execution.capture.scope import (
    CaptureContextState,
    CaptureFrameIdentity,
    CaptureScope,
)
from onec_runtime.execution.evaluation import EvaluationSuspended
from onec_runtime.rdbg.models import (
    DebugTarget,
    EvaluationResult,
    ModuleLocation,
    PendingEvaluation,
    StackFrame,
    StopEvent,
    TargetId,
)


TARGET = TargetId(UUID(int=1), "test")
BUSINESS = ModuleLocation("ExtensionModule", "", UUID(int=2), UUID(int=3), 50, "Runtime")
KERNEL = ModuleLocation("ExtensionModule", "", UUID(int=20), UUID(int=21), 60, "Runtime")
SHIELDED = (KERNEL,)
FULL = (KERNEL, BUSINESS)
STOP = StopEvent(
    TARGET,
    BUSINESS,
    "callStackFormed",
    stack=(BUSINESS, BUSINESS, KERNEL),
    stack_frames=tuple(
        StackFrame(TARGET, level, location)
        for level, location in enumerate((BUSINESS, BUSINESS, KERNEL))
    ),
)


def ready_scope() -> CaptureScope:
    scope = CaptureScope.from_stop(7, 42, STOP, 3)
    scope.record_locals(())
    scope.record_transfer("temporary-address")
    scope.record_kernel_frame(2)
    assert scope.record_main_command(42)
    scope.record_context_begun()
    scope.mark_ready()
    return scope


class Session:
    target = DebugTarget(TARGET, "Server", "stopped")

    def __init__(self, events: list[object]) -> None:
        self.events = events
        self.calls: list[tuple[str, object, int]] = []
        self.pending = PendingEvaluation(TARGET, UUID(int=4), self)
        self._starts = 0

    def set_breakpoints(self, locations, *, on_transport_dispatch):
        on_transport_dispatch()
        self.calls.append(("breakpoints", locations, get_ident()))

    def start_evaluation(self, expression, *, on_transport_dispatch, **kwargs):
        on_transport_dispatch()
        self._starts += 1
        self.pending = PendingEvaluation(TARGET, UUID(int=3 + self._starts), self)
        self.calls.append(("start", (expression, kwargs), get_ident()))
        return self.pending

    def wait_evaluation_event(self, pending, *, timeout_s, on_transport_dispatch):
        assert pending is self.pending
        on_transport_dispatch()
        self.calls.append(("wait", (pending, timeout_s), get_ident()))
        event = self.events.pop(0)
        if isinstance(event, BaseException):
            raise event
        return event

    def continue_evaluation(self, pending, stop, *, on_transport_dispatch):
        assert pending is self.pending and stop is STOP
        on_transport_dispatch()
        self.calls.append(("continue-eval", pending, get_ident()))


def transfer_plan(key: str) -> CaptureTransferPlan:
    def admit(value: object) -> AdmissionEnvelopeV1:
        return AdmissionEnvelopeV1.parse(
            value, max_payload_bytes=1024, max_base64_chars=2048
        )

    def decode(metadata: object, content: str) -> bytes:
        assert isinstance(metadata, AdmissionEnvelopeV1)
        assert content == "YQ=="
        return b"a"

    return CaptureTransferPlan(
        'Результат = "admission";',
        key,
        f'Контекст.Удалить("{key}");\nРезультат = Истина;',
        2048,
        decode,
        admit,
    )


@dataclass(frozen=True)
class IndependentTransferPlan:
    instruction: str
    private_key: str
    cleanup_instruction: str
    max_text_size: int
    decode: Callable[[object, str], bytes]
    admit_metadata: Callable[[object], object]


def plan_call(scope, plan):
    from onec_runtime.execution.capture.materialization import CaptureMaterializationExecutor

    executor = CaptureMaterializationExecutor(wait_interval_s=0.01)

    def run(port):
        return executor.execute(
            scope,
            plan,
            port=port,
            shield_workspace=lambda worker: worker.set_breakpoints(SHIELDED),
            restore_workspace=lambda worker: worker.set_breakpoints(FULL),
        )

    return run


def test_independent_plan_port_does_not_require_legacy_coordinator_type() -> None:
    key = "__onec_compact_table_" + "f" * 32
    admission = AdmissionEnvelopeV1(7, 1, 1, sha256(b"a").hexdigest(), 4)
    plan = IndependentTransferPlan(
        'Результат = "admission";',
        key,
        f'Контекст.Удалить("{key}");\nРезультат = Истина;',
        2048,
        lambda metadata, payload: b"a",
        lambda value: AdmissionEnvelopeV1.parse(
            value, max_payload_bytes=1024, max_base64_chars=2048
        ),
    )
    session = Session([
        EvaluationResult(UUID(int=4), "Строка", admission.encode(), False),
        EvaluationResult(UUID(int=5), "Строка", "YQ==", False),
        EvaluationResult(UUID(int=6), "Булево", "Истина", False),
    ])
    route = RouteToken("runtime", 1, 0, "capture")
    arbiter = RdbgArbiter(session, route)
    scope = ready_scope()
    ticket = arbiter.submit(route, plan_call(scope, plan))
    arbiter.dispatch(ticket)

    assert ticket.wait(3) == b"a"
    assert scope.temporary_cleanup_debts == ()
    assert scope.context_state is CaptureContextState.READY
    assert [kind for kind, _, _ in session.calls].count("start") == 3
    arbiter.close(timeout=3)


@pytest.mark.parametrize(
    ("admission_wire", "failure_type"),
    [
        ("D|worker_generation_value", CaptureValueAccessDeniedError),
        ("E|value_admission_failed", CaptureValueCheckError),
    ],
)
def test_denied_admission_cleans_key_without_payload_read_and_preserves_scope(
    admission_wire: str, failure_type: type[Exception],
) -> None:
    denial = EvaluationResult(UUID(int=4), "Строка", admission_wire, False)
    cleanup = EvaluationResult(UUID(int=5), "Булево", "Истина", False)
    session = Session([denial, cleanup])
    route = RouteToken("runtime", 1, 0, "capture")
    arbiter = RdbgArbiter(session, route)
    scope = ready_scope()
    key = "__onec_compact_table_" + "a" * 32
    ticket = arbiter.submit(route, plan_call(scope, transfer_plan(key)))
    arbiter.dispatch(ticket)

    with pytest.raises(failure_type):
        ticket.wait(3)
    assert ticket.status().settled
    assert arbiter.active_ticket is None
    assert scope.context_state is CaptureContextState.READY
    assert scope.frame_identity is CaptureFrameIdentity.CONFIRMED
    assert scope.temporary_cleanup_debts == ()
    kinds = [kind for kind, _, _ in session.calls]
    assert kinds == [
        "breakpoints", "start", "wait", "breakpoints", "start", "wait"
    ]
    sources = [value[0] for kind, value, _ in session.calls if kind == "start"]
    assert "ВыполнитьКодВКонтекстеОтладки" in sources[0]
    assert "admission" in sources[0]
    assert "Контекст.Удалить" in sources[1]
    assert all("ЗабратьКомпактнуюМатериализациюИзКонтекста" not in source for source in sources)
    assert len({thread for _, _, thread in session.calls}) == 1
    assert session.calls[0][2] != get_ident()

    next_ticket = arbiter.submit(route, lambda port: Settlement("next CAPTURE request"))
    arbiter.dispatch(next_ticket)
    assert next_ticket.wait(3) == "next CAPTURE request"
    arbiter.close(timeout=3)


def test_success_reads_payload_then_confirms_key_deletion() -> None:
    admission = AdmissionEnvelopeV1(7, 1, 1, sha256(b"a").hexdigest(), 4)
    session = Session([
        EvaluationResult(UUID(int=4), "Строка", admission.encode(), False),
        EvaluationResult(UUID(int=5), "Строка", "YQ==", False),
        EvaluationResult(UUID(int=6), "Булево", "Истина", False),
    ])
    route = RouteToken("runtime", 1, 0, "capture")
    arbiter = RdbgArbiter(session, route)
    scope = ready_scope()
    key = "__onec_compact_table_" + "b" * 32
    ticket = arbiter.submit(route, plan_call(scope, transfer_plan(key)))
    arbiter.dispatch(ticket)

    assert ticket.wait(3) == b"a"
    assert scope.temporary_cleanup_debts == ()
    sources = [value[0] for kind, value, _ in session.calls if kind == "start"]
    assert len(sources) == 3
    assert "ЗабратьКомпактнуюМатериализациюИзКонтекста" in sources[1]
    assert "Контекст.Удалить" in sources[2]
    assert scope.context_state is CaptureContextState.READY
    arbiter.close(timeout=3)


def test_confirmed_cleanup_rejection_is_key_debt_and_releases_ticket() -> None:
    session = Session([
        EvaluationResult(UUID(int=4), "Строка", "D|worker_generation_value", False),
        EvaluationResult(UUID(int=5), "Ошибка", "", True, "private cleanup error"),
    ])
    route = RouteToken("runtime", 1, 0, "capture")
    arbiter = RdbgArbiter(session, route)
    scope = ready_scope()
    key = "__onec_compact_table_" + "c" * 32
    ticket = arbiter.submit(route, plan_call(scope, transfer_plan(key)))
    arbiter.dispatch(ticket)

    with pytest.raises(ConfirmedTemporaryKeyCleanupFailure) as failure:
        ticket.wait(3)
    assert isinstance(failure.value.policy_error, CaptureValueAccessDeniedError)
    assert "private cleanup error" not in str(failure.value)
    assert ticket.status().settled
    assert arbiter.active_ticket is None
    assert scope.context_state is CaptureContextState.READY
    assert scope.frame_identity is CaptureFrameIdentity.CONFIRMED
    assert [(debt.key, debt.state) for debt in scope.temporary_cleanup_debts] == [
        (key, TemporaryCleanupState.CONFIRMED_FAILURE)
    ]
    next_ticket = arbiter.submit(route, lambda port: Settlement("next CAPTURE request"))
    arbiter.dispatch(next_ticket)
    assert next_ticket.wait(3) == "next CAPTURE request"
    arbiter.close(timeout=3)


def test_unknown_cleanup_retains_pending_and_blocks_next_request() -> None:
    session = Session([
        EvaluationResult(UUID(int=4), "Строка", "D|worker_generation_value", False),
        RdbgTransportTimeout("cleanup transport uncertain"),
        EvaluationResult(UUID(int=5), "Булево", "Истина", False),
    ])
    route = RouteToken("runtime", 1, 0, "capture")
    arbiter = RdbgArbiter(session, route)
    scope = ready_scope()
    key = "__onec_compact_table_" + "d" * 32
    ticket = arbiter.submit(route, plan_call(scope, transfer_plan(key)))
    arbiter.dispatch(ticket)

    assert ticket.wait_unknown(3)
    assert isinstance(ticket._error, TemporaryKeyCleanupOutcomeUnknown)
    assert isinstance(ticket._error.policy_error, CaptureValueAccessDeniedError)
    assert ticket.status().pending_capability is session.pending
    assert arbiter.active_ticket is ticket
    assert [(debt.key, debt.state) for debt in scope.temporary_cleanup_debts] == [
        (key, TemporaryCleanupState.UNKNOWN)
    ]
    assert scope.context_state is CaptureContextState.READY
    assert scope.frame_identity is CaptureFrameIdentity.CONFIRMED
    next_ticket = arbiter.submit(route, lambda port: Settlement("next CAPTURE request"))
    arbiter.dispatch(next_ticket)
    with pytest.raises(TimeoutError):
        next_ticket.wait(0)

    def reconcile(port):
        result = port.wait_evaluation_event(session.pending, timeout_s=0.01)
        assert not result.error_occurred
        scope.confirm_temporary_cleanup(key)
        return Settlement("cleanup confirmed")

    arbiter.reconcile(ticket, reconcile)
    assert ticket.wait_settled(3) == "cleanup confirmed"
    assert next_ticket.wait(3) == "next CAPTURE request"
    assert [kind for kind, _, _ in session.calls].count("start") == 2
    assert scope.temporary_cleanup_debts == ()
    arbiter.close(timeout=3)


def test_cleanup_debugger_stop_preserves_suspended_capability_and_key_debt() -> None:
    from onec_runtime.execution.capture.materialization import (
        TemporaryKeyCleanupSuspended,
    )

    session = Session([
        EvaluationResult(UUID(int=4), "Строка", "D|worker_generation_value", False),
        STOP,
        EvaluationResult(UUID(int=5), "Булево", "Истина", False),
    ])
    route = RouteToken("runtime", 1, 0, "capture")
    arbiter = RdbgArbiter(session, route)
    scope = ready_scope()
    key = "__onec_compact_table_" + "e" * 32
    ticket = arbiter.submit(route, plan_call(scope, transfer_plan(key)))
    arbiter.dispatch(ticket)

    assert ticket.wait_unknown(3)
    assert isinstance(ticket._error, EvaluationSuspended)
    assert isinstance(ticket._error, TemporaryKeyCleanupSuspended)
    assert ticket._error.pending is session.pending
    assert ticket._error.stop is STOP
    assert ticket._error.key == key
    assert isinstance(ticket._error.policy_error, CaptureValueAccessDeniedError)
    assert ticket.status().pending_capability is session.pending
    assert [(debt.key, debt.state) for debt in scope.temporary_cleanup_debts] == [
        (key, TemporaryCleanupState.UNKNOWN)
    ]

    def reconcile(port):
        port.continue_evaluation(session.pending, STOP)
        result = port.wait_evaluation_event(session.pending, timeout_s=0.01)
        assert not result.error_occurred
        scope.confirm_temporary_cleanup(key)
        return Settlement("cleanup confirmed")

    arbiter.reconcile(ticket, reconcile)
    assert ticket.wait_settled(3) == "cleanup confirmed"
    assert [kind for kind, _, _ in session.calls].count("start") == 2
    arbiter.close(timeout=3)
