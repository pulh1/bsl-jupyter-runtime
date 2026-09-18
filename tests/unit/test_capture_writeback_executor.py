"""CAPTURE root writeback keeps an exact per-root recovery ledger."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from importlib import import_module
from typing import Callable
from uuid import UUID

import pytest

from onec_runtime.capture import (
    build_live_capture_root_transfer_call,
    build_temporary_storage_value_expression,
)
from onec_runtime.rdbg.models import (
    EvaluationResult,
    ModifyResult,
    PendingEvaluation,
    TargetId,
)


TARGET = TargetId(UUID(int=71), "runtime_test")
_EMPTY = object()


@dataclass(frozen=True)
class _AfterDispatch:
    error: Exception


def _contract():
    try:
        return import_module("onec_runtime.execution.capture.writeback")
    except ModuleNotFoundError as error:
        pytest.fail(f"CAPTURE writeback executor is missing: {error}")


class _Port:
    """Replace remote RDBG I/O while retaining real pending capabilities."""

    def __init__(
        self,
        exports: list[object],
        modifies: list[object],
    ) -> None:
        self.exports = deque(exports)
        self.modifies = deque(modifies)
        self.starts: list[tuple[str, int]] = []
        self.waits: list[PendingEvaluation] = []
        self.modify_calls: list[tuple[str, str]] = []
        self._pending_exports: dict[PendingEvaluation, deque[object]] = {}

    def start_evaluation(
        self,
        expression: str,
        *,
        max_text_size: int,
        stack_level: int,
        timeout_s: float,
    ) -> PendingEvaluation:
        assert max_text_size == 307_200
        assert timeout_s == 30.0
        self.starts.append((expression, stack_level))
        pending = PendingEvaluation(TARGET, UUID(int=len(self.starts)), self)
        response = self.exports.popleft()
        self._pending_exports[pending] = deque(
            response if isinstance(response, list) else [response]
        )
        return pending

    def wait_evaluation_event(
        self, pending: PendingEvaluation, *, timeout_s: float
    ) -> EvaluationResult:
        assert timeout_s > 0
        self.waits.append(pending)
        response = self._pending_exports[pending].popleft()
        if response is _EMPTY:
            from onec_runtime.errors import CommandTimeout

            raise CommandTimeout("empty result interval")
        if isinstance(response, Exception):
            raise response
        type_name, presentation, error_occurred, error_text, value_string = response
        return EvaluationResult(
            pending.result_id,
            type_name,
            presentation,
            error_occurred,
            error_text,
            value_string=value_string,
        )

    def modify(
        self,
        variable: str,
        value_expression: str,
        *,
        on_transport_dispatch: Callable[[], None],
    ) -> ModifyResult:
        self.modify_calls.append((variable, value_expression))
        response = self.modifies.popleft()
        if isinstance(response, Exception):
            raise response
        on_transport_dispatch()
        if isinstance(response, _AfterDispatch):
            raise response.error
        error_occurred, error_text = response
        return ModifyResult(
            UUID(int=len(self.modify_calls)),
            "Булево",
            "Истина" if not error_occurred else "Ложь",
            error_occurred,
            error_text,
        )


def _address(value: str) -> tuple[str, str, bool, str, str]:
    return ("Строка", f'"{value}"', False, "", value)


def test_single_root_exports_once_then_modifies_original_frame_root() -> None:
    contract = _contract()
    port = _Port([_address("storage-a")], [(False, "")])
    ledger = contract.RootWritebackLedger(("Скаляр",))

    contract.CaptureWritebackExecutor().flush(port, ledger, stack_level=2)

    record = ledger.record("Скаляр")
    assert record.phase is contract.RootWritePhase.SUCCEEDED
    assert ledger.disposition is contract.WritebackDisposition.READY
    assert port.starts == [(build_live_capture_root_transfer_call("Скаляр"), 2)]
    assert port.modify_calls == [
        ("Скаляр", build_temporary_storage_value_expression("storage-a"))
    ]


def test_empty_result_intervals_reuse_one_export_pending_capability() -> None:
    contract = _contract()
    port = _Port([[_EMPTY, _EMPTY, _address("storage-a")]], [(False, "")])
    ledger = contract.RootWritebackLedger(("Скаляр",))

    contract.CaptureWritebackExecutor().flush(port, ledger, stack_level=2)

    assert len(port.starts) == 1
    assert len(port.waits) == 3
    assert all(pending is port.waits[0] for pending in port.waits)


def test_confirmed_first_export_failure_keeps_frame_paused_and_skips_modify() -> None:
    contract = _contract()
    port = _Port([("Ошибка", "", True, "private BSL error", None)], [])
    ledger = contract.RootWritebackLedger(("А", "Б"))

    with pytest.raises(contract.CaptureExportFailed):
        contract.CaptureWritebackExecutor().flush(port, ledger, stack_level=2)

    assert ledger.disposition is contract.WritebackDisposition.PAUSED_EXPORT_FAILED
    assert ledger.record("А").phase is contract.RootWritePhase.FAILED
    assert ledger.record("Б").phase is contract.RootWritePhase.UNATTEMPTED
    assert port.modify_calls == []


def test_confirmed_second_export_failure_records_partial_write() -> None:
    contract = _contract()
    port = _Port(
        [_address("storage-a"), ("Ошибка", "", True, "private BSL error", None)],
        [(False, "")],
    )
    ledger = contract.RootWritebackLedger(("А", "Б", "В"))

    with pytest.raises(contract.CaptureExportFailed):
        contract.CaptureWritebackExecutor().flush(port, ledger, stack_level=2)

    assert ledger.disposition is contract.WritebackDisposition.PARTIAL_WRITE
    assert [ledger.record(root).phase for root in ("А", "Б", "В")] == [
        contract.RootWritePhase.SUCCEEDED,
        contract.RootWritePhase.FAILED,
        contract.RootWritePhase.UNATTEMPTED,
    ]


def test_modify_unknown_is_never_replayed_or_followed_by_another_root() -> None:
    from onec_runtime.errors import RdbgTransportError

    contract = _contract()
    port = _Port(
        [_address("storage-a"), _address("storage-b")],
        [_AfterDispatch(RdbgTransportError("network lost")), (False, "")],
    )
    ledger = contract.RootWritebackLedger(("А", "Б"))
    writer = contract.CaptureWritebackExecutor()

    with pytest.raises(RdbgTransportError):
        writer.flush(port, ledger, stack_level=2)
    assert ledger.disposition is contract.WritebackDisposition.OUTCOME_UNKNOWN
    assert ledger.record("А").phase is contract.RootWritePhase.UNKNOWN
    assert ledger.record("Б").phase is contract.RootWritePhase.UNATTEMPTED
    with pytest.raises(contract.WritebackBlocked):
        writer.flush(port, ledger, stack_level=2)
    assert len(port.starts) == 1
    assert len(port.modify_calls) == 1


def test_confirmed_predispatch_modify_rejection_retries_only_modify() -> None:
    contract = _contract()
    port = _Port([_address("storage-a")], [ValueError("before dispatch"), (False, "")])
    ledger = contract.RootWritebackLedger(("А",))
    writer = contract.CaptureWritebackExecutor()

    with pytest.raises(ValueError):
        writer.flush(port, ledger, stack_level=2)
    assert ledger.record("А").phase is contract.RootWritePhase.EXPORTED
    writer.flush(port, ledger, stack_level=2)
    assert ledger.record("А").phase is contract.RootWritePhase.SUCCEEDED
    assert len(port.starts) == 1
    assert len(port.modify_calls) == 2


def test_confirmed_modify_error_is_failed_write_and_does_not_continue() -> None:
    contract = _contract()
    port = _Port([_address("storage-a")], [(True, "private remote error")])
    ledger = contract.RootWritebackLedger(("А", "Б"))

    with pytest.raises(contract.CaptureModifyFailed) as raised:
        contract.CaptureWritebackExecutor().flush(port, ledger, stack_level=2)

    assert "private remote error" not in str(raised.value)
    assert ledger.record("А").phase is contract.RootWritePhase.FAILED
    assert ledger.record("Б").phase is contract.RootWritePhase.UNATTEMPTED
    assert ledger.disposition is contract.WritebackDisposition.PARTIAL_WRITE


def test_export_unknown_is_not_redispatched() -> None:
    from onec_runtime.errors import RdbgTransportError

    contract = _contract()
    port = _Port([[RdbgTransportError("network lost")]], [])
    ledger = contract.RootWritebackLedger(("А",))
    writer = contract.CaptureWritebackExecutor()

    with pytest.raises(RdbgTransportError):
        writer.flush(port, ledger, stack_level=2)
    assert ledger.record("А").phase is contract.RootWritePhase.UNKNOWN
    with pytest.raises(contract.WritebackBlocked):
        writer.flush(port, ledger, stack_level=2)
    assert len(port.starts) == 1
    assert port.modify_calls == []


def test_safe_retry_skips_already_written_root_and_confirmed_export() -> None:
    contract = _contract()
    port = _Port(
        [_address("storage-a"), _address("storage-b")],
        [(False, ""), ValueError("before dispatch"), (False, "")],
    )
    ledger = contract.RootWritebackLedger(("А", "Б"))
    writer = contract.CaptureWritebackExecutor()

    with pytest.raises(ValueError):
        writer.flush(port, ledger, stack_level=2)
    assert ledger.record("А").phase is contract.RootWritePhase.SUCCEEDED
    assert ledger.record("Б").phase is contract.RootWritePhase.EXPORTED
    writer.flush(port, ledger, stack_level=2)

    assert ledger.disposition is contract.WritebackDisposition.READY
    assert len(port.starts) == 2
    assert [root for root, _ in port.modify_calls] == ["А", "Б", "Б"]


def test_non_string_export_is_confirmed_failure_even_with_text_presentation() -> None:
    contract = _contract()
    port = _Port([("Ссылка", '"looks-like-address"', False, "", "")], [])
    ledger = contract.RootWritebackLedger(("А",))

    with pytest.raises(contract.CaptureExportFailed):
        contract.CaptureWritebackExecutor().flush(port, ledger, stack_level=2)

    assert ledger.disposition is contract.WritebackDisposition.PAUSED_EXPORT_FAILED
    assert port.modify_calls == []


def test_explicit_retry_of_confirmed_export_error_keeps_prior_writes() -> None:
    contract = _contract()
    port = _Port(
        [_address("storage-a"), ("Ошибка", "", True, "error", None), _address("storage-b")],
        [(False, ""), (False, "")],
    )
    ledger = contract.RootWritebackLedger(("А", "Б"))
    writer = contract.CaptureWritebackExecutor()

    with pytest.raises(contract.CaptureExportFailed):
        writer.flush(port, ledger, stack_level=2)
    ledger.retry_confirmed_export("Б")
    writer.flush(port, ledger, stack_level=2)

    assert ledger.disposition is contract.WritebackDisposition.READY
    assert [root for root, _ in port.modify_calls] == ["А", "Б"]
    assert len(port.starts) == 3


def test_unknown_cannot_be_reset_as_confirmed_export_failure() -> None:
    from onec_runtime.errors import RdbgTransportError

    contract = _contract()
    port = _Port([[RdbgTransportError("lost")]], [])
    ledger = contract.RootWritebackLedger(("А",))
    with pytest.raises(RdbgTransportError):
        contract.CaptureWritebackExecutor().flush(port, ledger, stack_level=2)

    with pytest.raises(contract.WritebackBlocked):
        ledger.retry_confirmed_export("А")


def test_writeback_runs_through_owned_arbiter_modify_port() -> None:
    from onec_runtime.execution.arbiter import RdbgArbiter, RouteToken, Settlement
    from onec_runtime.rdbg.models import EvaluationResult
    from test_execution_route_sequence import RouteSession

    class WritebackSession(RouteSession):
        def wait_evaluation_event(self, pending, *, timeout_s, on_transport_dispatch):
            on_transport_dispatch()
            self._record("wait_eval")
            self.pending = None
            return EvaluationResult(
                pending.result_id, "Строка", '"storage-a"', False,
                value_string="storage-a",
            )

    contract = _contract()
    session = WritebackSession()
    route = RouteToken("writeback-test", 1, 0, "capture")
    arbiter = RdbgArbiter(session, route)
    ledger = contract.RootWritebackLedger(("А",))

    def plan(port):
        contract.CaptureWritebackExecutor().flush(port, ledger, stack_level=2)
        return Settlement(None)

    try:
        ticket = arbiter.submit(route, plan)
        arbiter.dispatch(ticket)
        assert ticket.wait(3) is None
        assert ledger.disposition is contract.WritebackDisposition.READY
        assert [name for name, _ in session.calls] == [
            "start_eval", "wait_eval", "modify",
        ]
        assert len({thread for _, thread in session.calls}) == 1
    finally:
        arbiter.close(timeout=3)
