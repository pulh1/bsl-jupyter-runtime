"""CAPTURE inspection uses the owned RDBG worker without poisoning its stop."""

from threading import get_ident
from uuid import UUID

import pytest

from onec_runtime.errors import CommandTimeout, RdbgTransportTimeout
from onec_runtime.execution.arbiter import (
    OutcomeUnknown,
    RdbgArbiter,
    RouteToken,
    Settlement,
)
from onec_runtime.execution.capture.scope import (
    CaptureContextState,
    CaptureFrameIdentity,
    CaptureScope,
)
from onec_runtime.execution.evaluation import EvaluationSuspended
from onec_runtime.rdbg.models import (
    DebugTarget,
    EvaluationResult,
    FrameVariable,
    LocalVariablesResult,
    ModuleLocation,
    PendingEvaluation,
    StackFrame,
    StopEvent,
    TargetId,
)


TARGET = TargetId(UUID(int=1), "test")
BUSINESS = ModuleLocation("ExtensionModule", "", UUID(int=2), UUID(int=3), 50, "Runtime")
KERNEL = ModuleLocation("ExtensionModule", "", UUID(int=20), UUID(int=21), 60, "Runtime")
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


def ready_scope(stop: StopEvent = STOP) -> CaptureScope:
    scope = CaptureScope.from_stop(7, 42, stop, 3)
    scope.record_locals(())
    scope.record_transfer("temporary-address")
    scope.record_kernel_frame(2)
    assert scope.record_main_command(42)
    scope.record_context_begun()
    scope.mark_ready()
    return scope


class Session:
    target = DebugTarget(TARGET, "Server", "stopped")

    def __init__(self, local_results: list[object], events: list[object] | None = None) -> None:
        self.local_results = local_results
        self.events = [] if events is None else events
        self.calls: list[tuple[str, object, int]] = []
        self.pending = PendingEvaluation(TARGET, UUID(int=4), self)
        self._starts = 0

    def local_variables(self, stack_level=0, *, timeout_s, on_transport_dispatch):
        on_transport_dispatch()
        self.calls.append(("locals", (stack_level, timeout_s), get_ident()))
        outcome = self.local_results.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

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


def test_confirmed_variable_read_failure_settles_only_request_and_retry_succeeds() -> None:
    from onec_runtime.execution.capture.inspection import (
        CaptureInspectionExecutor,
        CaptureInspectionUnavailable,
    )

    rejected = LocalVariablesResult(
        UUID(int=10), (), True, "secret target path from debugger"
    )
    variable = FrameVariable("Номер", "Число", "42")
    accepted = LocalVariablesResult(UUID(int=11), (variable,))
    session = Session([rejected, accepted])
    route = RouteToken("runtime", 1, 0, "capture")
    arbiter = RdbgArbiter(session, route)
    scope = ready_scope()
    executor = CaptureInspectionExecutor(request_timeout_s=3)

    first = arbiter.submit(
        route,
        lambda port: Settlement(
            executor.read_variable(scope, "Номер", stack_level=0, port=port)
        ),
    )
    arbiter.dispatch(first)
    with pytest.raises(CaptureInspectionUnavailable) as failure:
        first.wait(3)

    assert "secret target path" not in str(failure.value)
    assert first.status().settled
    assert arbiter.active_ticket is None
    assert scope.context_state is CaptureContextState.READY
    assert scope.frame_identity is CaptureFrameIdentity.CONFIRMED

    second = arbiter.submit(
        route,
        lambda port: Settlement(
            executor.read_variable(scope, "номер", stack_level=0, port=port)
        ),
    )
    arbiter.dispatch(second)
    assert second.wait(3) is variable
    assert [kind for kind, _, _ in session.calls] == ["locals", "locals"]
    assert {thread for _, _, thread in session.calls} != {get_ident()}
    assert len({thread for _, _, thread in session.calls}) == 1
    arbiter.close(timeout=3)


def test_native_variable_page_exposes_only_bounded_names_and_cursor() -> None:
    from onec_runtime.execution.capture.inspection import CaptureInspectionExecutor

    variables = (
        FrameVariable("Первый", "Строка", "private value A"),
        FrameVariable("Второй", "Строка", "private value B"),
        FrameVariable("Третий", "Строка", "private value C"),
        FrameVariable("Четвертый", "Строка", "private value D"),
    )
    session = Session([LocalVariablesResult(UUID(int=12), variables)])
    route = RouteToken("runtime", 1, 0, "capture")
    arbiter = RdbgArbiter(session, route)
    scope = ready_scope()
    executor = CaptureInspectionExecutor(request_timeout_s=3)
    ticket = arbiter.submit(
        route,
        lambda port: Settlement(
            executor.read_variable_page(
                scope, stack_level=0, start=1, stop=3, port=port
            )
        ),
    )
    arbiter.dispatch(ticket)

    page = ticket.wait(3)
    assert page.names == ("Второй", "Третий")
    assert page.total == 4
    assert page.next_cursor == 3
    assert "private value" not in repr(page)
    assert [(kind, payload) for kind, payload, _ in session.calls] == [
        ("locals", (0, 3))
    ]
    assert session.calls[0][2] != get_ident()
    arbiter.close(timeout=3)


def test_typed_variable_page_bounds_metadata_for_nonroot_frame_without_presentation() -> None:
    from onec_runtime.execution.capture.inspection import CaptureInspectionExecutor

    variables = (
        FrameVariable("Первый", "Строка", "private value A", 3),
        FrameVariable("Второй", "ТаблицаЗначений", "private value B", 10_000),
        FrameVariable("Третий", "Число", "private value C"),
    )
    session = Session([LocalVariablesResult(UUID(int=121), variables)])
    route = RouteToken("runtime", 1, 0, "capture")
    arbiter = RdbgArbiter(session, route)
    try:
        ticket = arbiter.submit(
            route,
            lambda port: Settlement(
                CaptureInspectionExecutor(request_timeout_s=3).read_typed_variable_page(
                    ready_scope(), stack_level=1, start=1, stop=3, port=port,
                )
            ),
        )
        arbiter.dispatch(ticket)
        page = ticket.wait(3)
        assert tuple(
            (item.name, item.type_name, item.collection_size)
            for item in page.variables
        ) == (("Второй", "ТаблицаЗначений", 10_000), ("Третий", "Число", None))
        assert page.total == 3
        assert page.next_cursor is None
        assert "private value" not in repr(page)
        assert not hasattr(page.variables[0], "presentation")
        assert session.calls[0][1] == (1, 3)
    finally:
        arbiter.close(timeout=3)


@pytest.mark.parametrize(
    "variable",
    [
        FrameVariable("Amount", "T" * 257, "secret"),
        FrameVariable("Amount", "Число", "secret", -1),
        FrameVariable("Amount", "Число", "secret", 10_001),
        FrameVariable("Amount", "Число", "secret", True),
    ],
)
def test_typed_variable_page_rejects_unbounded_metadata_without_leaking_values(
    variable: FrameVariable,
) -> None:
    from onec_runtime.execution.capture.inspection import (
        CaptureInspectionExecutor, CaptureInspectionUnavailable,
    )

    session = Session([LocalVariablesResult(UUID(int=122), (variable,))])
    route = RouteToken("runtime", 1, 0, "capture")
    arbiter = RdbgArbiter(session, route)
    try:
        ticket = arbiter.submit(
            route,
            lambda port: Settlement(
                CaptureInspectionExecutor().read_typed_variable_page(
                    ready_scope(), stack_level=1, start=0, stop=1, port=port,
                )
            ),
        )
        arbiter.dispatch(ticket)
        with pytest.raises(CaptureInspectionUnavailable) as failure:
            ticket.wait(3)
        assert "secret" not in str(failure.value)
    finally:
        arbiter.close(timeout=3)


def test_typed_variable_page_rejects_kernel_and_oversized_page_before_rdbg() -> None:
    from onec_runtime.execution.capture.inspection import CaptureInspectionExecutor

    session = Session([])
    route = RouteToken("runtime", 1, 0, "capture")
    arbiter = RdbgArbiter(session, route)
    try:
        for stack_level, stop, expected in ((2, 1, "kernel"), (1, 101, "100")):
            ticket = arbiter.submit(
                route,
                lambda port, stack_level=stack_level, stop=stop: Settlement(
                    CaptureInspectionExecutor().read_typed_variable_page(
                        ready_scope(), stack_level=stack_level, start=0, stop=stop,
                        port=port,
                    )
                ),
            )
            arbiter.dispatch(ticket)
            with pytest.raises(ValueError, match=expected):
                ticket.wait(3)
        assert session.calls == []
    finally:
        arbiter.close(timeout=3)


def test_native_variable_page_rejects_kernel_and_invalid_bounds_before_rdbg() -> None:
    from onec_runtime.execution.capture.inspection import CaptureInspectionExecutor

    session = Session([])
    route = RouteToken("runtime", 1, 0, "capture")
    arbiter = RdbgArbiter(session, route)
    scope = ready_scope()
    executor = CaptureInspectionExecutor()

    for level, start, stop, expected in (
        (2, 0, 1, "kernel"),
        (0, 0, 101, "at most 100"),
        (0, -1, 1, "nonnegative"),
    ):
        ticket = arbiter.submit(
            route,
            lambda port, level=level, start=start, stop=stop: Settlement(
                executor.read_variable_page(
                    scope, stack_level=level, start=start, stop=stop, port=port
                )
            ),
        )
        arbiter.dispatch(ticket)
        with pytest.raises(ValueError, match=expected):
            ticket.wait(3)

    assert session.calls == []
    assert scope.context_state is CaptureContextState.READY
    arbiter.close(timeout=3)


def test_confirmed_native_variable_page_error_allows_retry_on_same_scope() -> None:
    from onec_runtime.execution.capture.inspection import (
        CaptureInspectionExecutor,
        CaptureInspectionUnavailable,
    )

    rejected = LocalVariablesResult(UUID(int=12), (), True, "private debugger path")
    accepted = LocalVariablesResult(
        UUID(int=13), (FrameVariable("Счетчик", "Число", "25"),)
    )
    session = Session([rejected, accepted])
    route = RouteToken("runtime", 1, 0, "capture")
    arbiter = RdbgArbiter(session, route)
    scope = ready_scope()
    executor = CaptureInspectionExecutor()

    first = arbiter.submit(
        route,
        lambda port: Settlement(
            executor.read_variable_page(
                scope, stack_level=0, start=0, stop=1, port=port
            )
        ),
    )
    arbiter.dispatch(first)
    with pytest.raises(CaptureInspectionUnavailable) as failure:
        first.wait(3)
    assert "private debugger path" not in str(failure.value)
    assert first.status().settled
    assert scope.frame_identity is CaptureFrameIdentity.CONFIRMED

    second = arbiter.submit(
        route,
        lambda port: Settlement(
            executor.read_variable_page(
                scope, stack_level=0, start=0, stop=1, port=port
            )
        ),
    )
    arbiter.dispatch(second)
    assert second.wait(3).names == ("Счетчик",)
    arbiter.close(timeout=3)


def test_native_variable_page_rejects_duplicate_or_unsafe_inventory() -> None:
    from onec_runtime.execution.capture.inspection import (
        CaptureInspectionExecutor,
        CaptureInspectionUnavailable,
    )

    inventories = (
        (FrameVariable("Имя", "Строка", "a"), FrameVariable("имя", "Строка", "b")),
        (FrameVariable("Недопустимое имя", "Строка", "private"),),
    )
    session = Session(
        [LocalVariablesResult(UUID(int=14 + index), values)
         for index, values in enumerate(inventories)]
    )
    route = RouteToken("runtime", 1, 0, "capture")
    arbiter = RdbgArbiter(session, route)
    scope = ready_scope()
    executor = CaptureInspectionExecutor()
    for _ in inventories:
        ticket = arbiter.submit(
            route,
            lambda port: Settlement(
                executor.read_variable_page(
                    scope, stack_level=0, start=0, stop=1, port=port
                )
            ),
        )
        arbiter.dispatch(ticket)
        with pytest.raises(CaptureInspectionUnavailable) as failure:
            ticket.wait(3)
        assert "private" not in str(failure.value)
        assert scope.context_state is CaptureContextState.READY
    arbiter.close(timeout=3)


def test_native_variable_page_rejects_oversized_private_inventory() -> None:
    from onec_runtime.execution.capture.inspection import (
        CaptureInspectionExecutor,
        CaptureInspectionUnavailable,
    )

    oversized = tuple(
        FrameVariable(f"V{index}", "Число", "private presentation")
        for index in range(10_001)
    )
    session = Session([LocalVariablesResult(UUID(int=16), oversized)])
    route = RouteToken("runtime", 1, 0, "capture")
    arbiter = RdbgArbiter(session, route)
    scope = ready_scope()
    ticket = arbiter.submit(
        route,
        lambda port: Settlement(
            CaptureInspectionExecutor().read_variable_page(
                scope, stack_level=0, start=0, stop=1, port=port
            )
        ),
    )
    arbiter.dispatch(ticket)

    with pytest.raises(CaptureInspectionUnavailable) as failure:
        ticket.wait(3)
    assert "private presentation" not in str(failure.value)
    assert scope.frame_identity is CaptureFrameIdentity.CONFIRMED
    arbiter.close(timeout=3)


def test_native_variable_page_rejects_scope_target_mismatch_before_rdbg() -> None:
    from onec_runtime.execution.capture.inspection import CaptureInspectionExecutor

    session = Session([])
    route = RouteToken("runtime", 1, 0, "capture")
    arbiter = RdbgArbiter(session, route)
    scope = ready_scope()
    scope.inspection_target_id = TargetId(UUID(int=99), "test")
    ticket = arbiter.submit(
        route,
        lambda port: Settlement(
            CaptureInspectionExecutor().read_variable_page(
                scope, stack_level=0, start=0, stop=1, port=port
            )
        ),
    )
    arbiter.dispatch(ticket)

    with pytest.raises(RuntimeError, match="not ready"):
        ticket.wait(3)
    assert session.calls == []
    arbiter.close(timeout=3)


def test_helper_eval_dispatches_once_and_reuses_pending_across_empty_intervals() -> None:
    from onec_runtime.execution.capture.inspection import CaptureInspectionExecutor

    result = EvaluationResult(UUID(int=4), "Строка", "value", False)
    session = Session([], [CommandTimeout("empty"), CommandTimeout("empty"), result])
    route = RouteToken("runtime", 1, 0, "capture")
    arbiter = RdbgArbiter(session, route)
    executor = CaptureInspectionExecutor(request_timeout_s=3, wait_interval_s=0.25)
    ticket = arbiter.submit(
        route,
        lambda port: Settlement(
            executor.evaluate_helper(
                ready_scope(),
                "TrustedHelper()",
                stack_level=2,
                max_text_size=512,
                port=port,
                result_policy=lambda observed: observed.presentation,
            )
        ),
    )
    arbiter.dispatch(ticket)

    assert ticket.wait(3) == "value"
    assert [kind for kind, _, _ in session.calls] == ["start", "wait", "wait", "wait"]
    assert session.calls[0][1] == (
        "TrustedHelper()",
        {"max_text_size": 512, "stack_level": 2, "timeout_s": 3},
    )
    assert [entry[1] for entry in session.calls[1:]] == [
        (session.pending, 0.25)
    ] * 3
    assert len({thread for _, _, thread in session.calls}) == 1
    assert session.calls[0][2] != get_ident()
    arbiter.close(timeout=3)


def test_confirmed_helper_bsl_error_is_request_failure_and_does_not_call_policy() -> None:
    from onec_runtime.execution.capture.inspection import (
        CaptureInspectionExecutor,
        CaptureInspectionUnavailable,
    )

    error_result = EvaluationResult(
        UUID(int=4), "Неопределено", "", True, "secret target expression"
    )
    variable = FrameVariable("Номер", "Число", "42")
    session = Session(
        [LocalVariablesResult(UUID(int=10), (variable,))], [error_result]
    )
    route = RouteToken("runtime", 1, 0, "capture")
    arbiter = RdbgArbiter(session, route)
    scope = ready_scope()
    executor = CaptureInspectionExecutor()
    policy_calls: list[EvaluationResult] = []
    first = arbiter.submit(
        route,
        lambda port: Settlement(
            executor.evaluate_helper(
                scope,
                "TrustedHelper()",
                stack_level=2,
                port=port,
                result_policy=lambda result: policy_calls.append(result),
            )
        ),
    )
    arbiter.dispatch(first)

    with pytest.raises(CaptureInspectionUnavailable) as failure:
        first.wait(3)
    assert "secret target expression" not in str(failure.value)
    assert policy_calls == []
    assert first.status().settled
    assert scope.context_state is CaptureContextState.READY
    assert scope.frame_identity is CaptureFrameIdentity.CONFIRMED

    second = arbiter.submit(
        route,
        lambda port: Settlement(
            executor.read_variable(scope, "Номер", stack_level=0, port=port)
        ),
    )
    arbiter.dispatch(second)
    assert second.wait(3) is variable
    arbiter.close(timeout=3)


def test_unknown_helper_wait_retains_one_pending_owner_and_fences_next_inspection() -> None:
    from onec_runtime.execution.capture.inspection import CaptureInspectionExecutor

    result = EvaluationResult(UUID(int=4), "Строка", "value", False)
    session = Session([], [RdbgTransportTimeout("network interval failed"), result])
    route = RouteToken("runtime", 1, 0, "capture")
    arbiter = RdbgArbiter(session, route)
    scope = ready_scope()
    executor = CaptureInspectionExecutor(wait_interval_s=0.01)
    ticket = arbiter.submit(
        route,
        lambda port: Settlement(
            executor.evaluate_helper(
                scope,
                "TrustedHelper()",
                stack_level=2,
                port=port,
                result_policy=lambda observed: observed.presentation,
            )
        ),
    )
    arbiter.dispatch(ticket)

    assert ticket.wait_unknown(3)
    assert isinstance(ticket._error, OutcomeUnknown)
    assert ticket.status().pending_capability is session.pending
    assert arbiter.active_ticket is ticket
    assert [kind for kind, _, _ in session.calls] == ["start", "wait"]
    assert scope.context_state is CaptureContextState.READY
    assert scope.frame_identity is CaptureFrameIdentity.CONFIRMED

    next_ticket = arbiter.submit(route, lambda port: Settlement("next inspection"))
    arbiter.dispatch(next_ticket)
    with pytest.raises(TimeoutError):
        next_ticket.wait(0)

    arbiter.reconcile(
        ticket,
        lambda port: Settlement(
            port.wait_evaluation_event(session.pending, timeout_s=0.01).presentation
        ),
    )
    assert ticket.wait_settled(3) == "value"
    assert next_ticket.wait(3) == "next inspection"
    assert [kind for kind, _, _ in session.calls].count("start") == 1
    arbiter.close(timeout=3)


def test_helper_stop_retains_exact_pending_and_stop_for_reconciliation() -> None:
    from onec_runtime.execution.capture.inspection import CaptureInspectionExecutor

    result = EvaluationResult(UUID(int=4), "Строка", "value", False)
    session = Session([], [STOP, result])
    route = RouteToken("runtime", 1, 0, "capture")
    arbiter = RdbgArbiter(session, route)
    scope = ready_scope()
    executor = CaptureInspectionExecutor(wait_interval_s=0.01)
    ticket = arbiter.submit(
        route,
        lambda port: Settlement(
            executor.evaluate_helper(
                scope,
                "TrustedHelper()",
                stack_level=2,
                port=port,
                result_policy=lambda observed: observed.presentation,
            )
        ),
    )
    arbiter.dispatch(ticket)

    assert ticket.wait_unknown(3)
    assert isinstance(ticket._error, EvaluationSuspended)
    assert ticket._error.pending is session.pending
    assert ticket._error.stop is STOP
    assert ticket.status().pending_capability is session.pending
    assert [kind for kind, _, _ in session.calls] == ["start", "wait"]

    def reconcile(port):
        port.continue_evaluation(session.pending, STOP)
        observed = port.wait_evaluation_event(session.pending, timeout_s=0.01)
        return Settlement(observed.presentation)

    arbiter.reconcile(ticket, reconcile)
    assert ticket.wait_settled(3) == "value"
    assert [kind for kind, _, _ in session.calls].count("start") == 1
    assert scope.context_state is CaptureContextState.READY
    arbiter.close(timeout=3)


def test_variable_read_rejects_other_runtime_kernel_frame_before_rdbg() -> None:
    from onec_runtime.execution.capture.inspection import CaptureInspectionExecutor

    other_kernel_line = ModuleLocation(
        KERNEL.module_type,
        KERNEL.url,
        KERNEL.object_id,
        KERNEL.property_id,
        55,
        KERNEL.extension_name,
    )
    stop = StopEvent(
        TARGET,
        BUSINESS,
        "callStackFormed",
        stack=(BUSINESS, other_kernel_line, KERNEL),
        stack_frames=tuple(
            StackFrame(TARGET, level, location)
            for level, location in enumerate((BUSINESS, other_kernel_line, KERNEL))
        ),
    )
    session = Session([])
    route = RouteToken("runtime", 1, 0, "capture")
    arbiter = RdbgArbiter(session, route)
    scope = ready_scope(stop)
    executor = CaptureInspectionExecutor()
    ticket = arbiter.submit(
        route,
        lambda port: Settlement(
            executor.read_variable(scope, "Контекст", stack_level=1, port=port)
        ),
    )
    arbiter.dispatch(ticket)

    with pytest.raises(ValueError, match="kernel"):
        ticket.wait(3)
    assert session.calls == []
    assert scope.context_state is CaptureContextState.READY
    arbiter.close(timeout=3)


def test_helper_decode_failure_settles_request_and_preserves_frame() -> None:
    from onec_runtime.execution.capture.inspection import CaptureInspectionExecutor

    result = EvaluationResult(UUID(int=4), "Строка", "malformed payload", False)
    variable = FrameVariable("Номер", "Число", "42")
    session = Session([LocalVariablesResult(UUID(int=10), (variable,))], [result])
    route = RouteToken("runtime", 1, 0, "capture")
    arbiter = RdbgArbiter(session, route)
    scope = ready_scope()
    executor = CaptureInspectionExecutor()

    def reject_payload(_result):
        raise ValueError("invalid private payload")

    first = arbiter.submit(
        route,
        lambda port: Settlement(
            executor.evaluate_helper(
                scope,
                "TrustedHelper()",
                stack_level=2,
                port=port,
                result_policy=reject_payload,
            )
        ),
    )
    arbiter.dispatch(first)
    with pytest.raises(ValueError, match="invalid private payload"):
        first.wait(3)

    assert first.status().settled
    assert arbiter.active_ticket is None
    assert scope.context_state is CaptureContextState.READY
    assert scope.frame_identity is CaptureFrameIdentity.CONFIRMED
    second = arbiter.submit(
        route,
        lambda port: Settlement(
            executor.read_variable(scope, "Номер", stack_level=0, port=port)
        ),
    )
    arbiter.dispatch(second)
    assert second.wait(3) is variable
    arbiter.close(timeout=3)
