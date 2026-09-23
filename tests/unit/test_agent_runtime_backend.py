from __future__ import annotations

import json
import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import onec_runtime_mcp.agent.contracts as agent_contracts
import onec_runtime.privacy as privacy
from onec_runtime_mcp.agent.contracts import (
    AgentOperationState,
    CapabilityMode,
    StateChanged,
    to_wire,
)
from onec_runtime_mcp.agent.runtime_backend import (
    CapabilityDenied,
    OnecRuntimeBackend,
    OnecRuntimeFactory,
)
from onec_runtime_mcp.agent.operations import OperationRegistry
from onec_runtime.bsl import (
    DiagnosticStage,
    MappingConfidence,
    NormalizedDiagnostic,
    SourceSpan,
    SourceUnitKind,
    SourceUnitRef,
    VisibleSourceContext,
    VisibleSourceLocation,
    mapped_visible_source,
    parse_platform_diagnostic,
    remap_platform_diagnostic,
)
from onec_runtime_mcp.agent.capture_service import CaptureIntent
from onec_runtime.capture_source import CapturePointRequest, CaptureSourceConfig
from onec_runtime.errors import (
    BslExecutionError,
    CaptureSourceNotConfigured,
    ProtocolError,
)
from onec_runtime.execution.public_facade import PreparedMainExecutionAttempt
from onec_runtime.runtime_models import (
    OperationState,
    RuntimeNamespaceSnapshot,
    RuntimeReply,
    RuntimeReplyKind,
    RuntimeStatus,
)
from onec_runtime.session import ExtensionMode, RuntimeSession, RuntimeSessionConfig


@pytest.fixture(autouse=True)
def _clear_optional_runtime_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ONEC_RUNTIME_EXTENSION_MODE",
        "ONEC_RUNTIME_CONNECTION_STRING",
        "ONEC_RUNTIME_SERVER_INFOBASE",
        "ONEC_RUNTIME_DEBUG_HOST",
        "ONEC_RUNTIME_DEBUG_PORT",
        "ONEC_RUNTIME_DEBUG_ALIAS",
    ):
        monkeypatch.delenv(name, raising=False)


class SecretOnecValue:
    def __repr__(self) -> str:
        return "Пароль=super-secret"


class FakeDemoSession:
    def __init__(self, reply: RuntimeReply, status: RuntimeStatus | None = None) -> None:
        self.reply = reply
        self._status = status or RuntimeStatus(OperationState.COMPLETED, 3, 7, None)
        self.sources: list[str] = []
        self.close_calls = 0

    def execute_bsl(self, source: str) -> RuntimeReply:
        self.sources.append(source)
        return self.reply

    def status(self) -> RuntimeStatus:
        return self._status

    def namespace_snapshot(self) -> RuntimeNamespaceSnapshot:
        return RuntimeNamespaceSnapshot(
            self._status.runtime_generation,
            5,
            ("Порог",),
        )

    def close(self) -> None:
        self.close_calls += 1


class SecretBackendError(RuntimeError):
    marker = "credential=super-secret"

    def __init__(self) -> None:
        super().__init__(self.marker)
        self.__cause__ = ValueError(self.marker)
        self.add_note(self.marker)

    def __repr__(self) -> str:
        return f"SecretBackendError({self.marker})"


_RAW_PRIVATE_FAILURE = (
    r"source=C:\Users\private-user\AppData\Local\Temp\onec\private-cell.bsl "
    "pid=55123 process_id=process-private-55123 "
    "rdbg_target_id=rdbg-target-private "
    "rdbg_process_id=rdbg-process-private "
    "rdbg_session_id=rdbg-session-private "
    "token=token-private credential=credential-private "
    "password=password-private Authorization: Bearer bearer-private"
)
_PRIVATE_FAILURE_MARKERS = (
    r"C:\Users\private-user",
    "55123",
    "process-private-55123",
    "rdbg-target-private",
    "rdbg-process-private",
    "rdbg-session-private",
    "token-private",
    "credential-private",
    "password-private",
    "bearer-private",
)


def _publish_and_reload_failure(
    tmp_path: Path,
    outcome: agent_contracts.BackendExecution,
    *,
    source_sha256: str,
) -> tuple[list[dict[str, object]], str]:
    registry = OperationRegistry(tmp_path)
    submitted = registry.submit(
        {
            "runtime_id": "runtime-private-failure",
            "runtime_generation": 1,
            "code_id": "cell-private-failure",
            "revision": 1,
            "source_sha256": source_sha256,
            "inputs_sha256": "inputs-private-failure",
        },
        lambda: outcome,
    )
    terminal = registry.wait(submitted.operation_id, timeout_s=2.0)
    public = registry.output(terminal.operation_id)
    public_snapshot = registry.view_snapshot(terminal.operation_id)
    public_path = (
        tmp_path / ".runtime" / "agent-service" / "operations.jsonl"
    )
    public_journal = public_path.read_text(encoding="utf-8")
    events = [json.loads(line) for line in public_journal.splitlines()]

    assert terminal.state is outcome.terminal_state
    assert public.messages == ("BSL execution failed",)
    assert public.has_more is False
    assert [
        event["message"] for event in events if event["event"] == "message"
    ] == ["BSL execution failed"]
    if outcome.diagnostic is not None:
        assert public_snapshot.facts.failure is not None
        assert public_snapshot.facts.failure["diagnostic"] == {
            "diagnostic_id": outcome.diagnostic.diagnostic_id,
            "stage": outcome.diagnostic.stage.value,
            "mapping_confidence": outcome.diagnostic.mapping_confidence.value,
            "visible_location": {
                "line": 1,
                "column": 1,
                "span": {"start": 0, "end": 1},
            },
            "related_visible_span": None,
            "excerpt": None,
            "synthetic_region": None,
        }
    for marker in _PRIVATE_FAILURE_MARKERS:
        assert marker not in json.dumps(to_wire(public), ensure_ascii=False)
        assert marker not in public_journal
    registry.shutdown()

    recovered = OperationRegistry(tmp_path)
    recovered_output = recovered.output(terminal.operation_id)
    recovered_snapshot = recovered.view_snapshot(terminal.operation_id)
    assert recovered_output.messages == ("BSL execution failed",)
    assert recovered_snapshot.facts == public_snapshot.facts
    recovered.shutdown()
    return events, terminal.operation_id


class FailingDemoSession:
    def __init__(self, *, execute: bool) -> None:
        self._execute = execute

    def execute_bsl(self, _source: str) -> RuntimeReply:
        if self._execute:
            raise SecretBackendError()
        raise AssertionError("execute_bsl is not expected")

    def status(self) -> RuntimeStatus:
        if not self._execute:
            raise SecretBackendError()
        raise AssertionError("status is not expected")

    def close(self) -> None:
        pass


def test_backend_executes_exact_source_and_redacts_result() -> None:
    session = FakeDemoSession(
        RuntimeReply(
            RuntimeReplyKind.MAIN_COMPLETED,
            7,
            OperationState.COMPLETED,
            result=SecretOnecValue(),
            messages=("строка 1", "строка 2"),
        )
    )
    backend = OnecRuntimeBackend("runtime-1", session)

    outcome = backend.execute_bsl("Ответ = 42;")

    assert session.sources == ["Ответ = 42;"]
    assert outcome.terminal_state is AgentOperationState.COMPLETED
    assert outcome.result_present is True
    assert outcome.messages == ("строка 1", "строка 2")
    assert b"SecretOnecValue" not in json.dumps(to_wire(outcome)).encode()
    assert b"super-secret" not in json.dumps(to_wire(outcome)).encode()


def test_backend_threads_predispatch_provenance_and_does_not_swallow_journal_failure() -> None:
    """Break caught: backend redaction turns a failed provenance write into target execution."""
    source = "Ответ = 42;"
    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL,
        "backend-cell",
        5,
        hashlib.sha256(source.encode()).hexdigest(),
    )
    provenance = agent_contracts.OperationExecutionProvenance(
        "a" * 64,
        "b" * 64,
        "c" * 64,
        "main",
    )

    class Session:
        target_calls = 0

        def execute_bsl(
            self,
            exact_source: str,
            *,
            source_unit: SourceUnitRef,
            on_execution_provenance,
        ) -> RuntimeReply:  # type: ignore[no-untyped-def]
            assert exact_source == source
            assert source_unit == unit
            on_execution_provenance(provenance)
            self.target_calls += 1
            return RuntimeReply(
                RuntimeReplyKind.MAIN_COMPLETED,
                1,
                OperationState.COMPLETED,
            )

    session = Session()
    backend = OnecRuntimeBackend("runtime-1", session)  # type: ignore[arg-type]
    observed: list[object] = []

    outcome = backend.execute_bsl_with_provenance(
        source,
        source_unit=unit,
        on_execution_provenance=observed.append,
    )

    assert outcome.terminal_state is AgentOperationState.COMPLETED
    assert observed == [provenance]
    assert session.target_calls == 1
    with pytest.raises(OSError, match="journal failed"):
        backend.execute_bsl_with_provenance(
            source,
            source_unit=unit,
            on_execution_provenance=lambda _value: (_ for _ in ()).throw(
                OSError("journal failed")
            ),
        )
    assert session.target_calls == 1


def test_backend_exposes_only_owner_checked_prepared_provenance() -> None:
    """Break caught: prepared source/capability is serialized to obtain its hashes."""
    provenance = agent_contracts.OperationExecutionProvenance(
        "a" * 64,
        "b" * 64,
        "c" * 64,
        "main",
    )
    runtime_main = object()
    runtime_capture = object()

    class Session:
        def prepare_main_for_capture(
            self, source: str, *, source_unit: SourceUnitRef
        ) -> object:
            del source, source_unit
            return runtime_main

        def prepared_main_execution_provenance(self, prepared: object) -> object:
            assert prepared is runtime_main
            return provenance

        def prepared_capture_hypothesis_provenance(
            self, prepared: object
        ) -> object:
            assert prepared is runtime_capture
            return provenance

    backend = OnecRuntimeBackend("runtime-1", Session())  # type: ignore[arg-type]
    source = "Результат = 1;"
    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL,
        "backend-main",
        2,
        hashlib.sha256(source.encode()).hexdigest(),
    )
    prepared_main = backend.prepare_main_for_capture(source, source_unit=unit)

    assert backend.prepared_main_execution_provenance(prepared_main) == provenance
    assert (
        backend.prepared_capture_hypothesis_provenance(runtime_capture)
        == provenance
    )
    with pytest.raises(ProtocolError, match="prepared main"):
        backend.prepared_main_execution_provenance(object())


def test_backend_maps_captured_and_debug_stopped_without_collapsing_runtime_state() -> None:
    captured = OnecRuntimeBackend(
        "runtime-captured",
        FakeDemoSession(
            RuntimeReply(RuntimeReplyKind.CAPTURED, 8, OperationState.CAPTURED)
        ),
    ).execute_bsl("Остановить;")
    debug_stopped = OnecRuntimeBackend(
        "runtime-debug",
        FakeDemoSession(
            RuntimeReply(
                RuntimeReplyKind.DEBUG_STOPPED,
                9,
                OperationState.DEBUG_STOPPED,
            )
        ),
    ).execute_bsl("ТочкаОстанова;")

    assert captured.terminal_state is AgentOperationState.CAPTURED
    assert captured.runtime_state == "captured"
    assert debug_stopped.terminal_state is AgentOperationState.UNKNOWN
    assert debug_stopped.runtime_state == "debug_stopped"


def test_backend_preserves_structured_deterministic_source_failure() -> None:
    diagnostic = NormalizedDiagnostic(
        "a" * 64,
        "BSL parsing failed",
        DiagnosticStage.PARSING,
        MappingConfidence.UNKNOWN,
        code="unexpected_token",
    )
    backend = OnecRuntimeBackend(
        "runtime-source-failure",
        FakeDemoSession(
            RuntimeReply(
                RuntimeReplyKind.SOURCE_FAILED,
                8,
                OperationState.FAILED,
                error=diagnostic.runtime_summary,
                succeeded=False,
                diagnostic=diagnostic,
            )
        ),
    )

    outcome = backend.execute_bsl("Результат = ;")

    assert outcome.terminal_state is AgentOperationState.FAILED
    assert outcome.failure_stage == "parsing"
    assert outcome.state_changed is StateChanged.NO
    assert outcome.diagnostic == diagnostic
    assert outcome.messages == ("BSL parsing failed",)


def test_backend_keeps_runtime_unavailability_outside_bsl_failure_stages() -> None:
    """Break caught: route admission failure is reported as BSL execution."""

    backend = OnecRuntimeBackend(
        "runtime-unavailable",
        FakeDemoSession(
            RuntimeReply(
                RuntimeReplyKind.RUNTIME_UNAVAILABLE,
                8,
                OperationState.IDLE,
                error="RDBG operation is still active",
                succeeded=False,
            )
        ),
    )

    outcome = backend.execute_bsl("Результат = 1;")

    assert outcome.terminal_state is AgentOperationState.UNKNOWN
    assert outcome.runtime_state == "idle"
    assert outcome.failure_stage is None
    assert outcome.messages == ()
    assert outcome.state_changed is StateChanged.UNKNOWN


def test_backend_acceptance_preserves_exact_visible_diagnostic_without_source_leakage() -> None:
    """Break caught: backend sanitization drops exact coordinates or exposes BSL."""
    source = 'Первая = "😀";\r\nОшибка();'
    visible_sha256 = (
        "7526b9f0fdbb59335fc382867727b045be12280f6b963b428d58c5162c745512"
    )
    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL,
        "backend-acceptance",
        8,
        visible_sha256,
    )
    mapped = mapped_visible_source(source, unit)
    diagnostic = remap_platform_diagnostic(
        parse_platform_diagnostic(
            "{<Неизвестный модуль>(2,1)}: "
            "rdbg_pid=9182 token=private-backend"
        ),
        mapped,
        stage=DiagnosticStage.EXECUTION,
        visible_source_context=VisibleSourceContext({unit: source}),
    )
    session = FakeDemoSession(
        RuntimeReply(
            RuntimeReplyKind.MAIN_COMPLETED,
            18,
            OperationState.FAILED,
            error="RAW process_id=9182 token=private-backend",
            succeeded=False,
            diagnostic=diagnostic,
        )
    )

    outcome = OnecRuntimeBackend("runtime-acceptance", session).execute_bsl(
        source
    )

    assert session.sources == [source]
    assert outcome.terminal_state is AgentOperationState.FAILED
    assert outcome.state_changed is StateChanged.UNKNOWN
    assert outcome.diagnostic is not None
    assert outcome.diagnostic.execution_artifact_sha256 == visible_sha256
    assert outcome.diagnostic.source_map_sha256 == (
        "72ed9615a4e042b9c81d7dbc367a0c84adcaed11ac15abe0ff8e387f77256709"
    )
    public = privacy.diagnostic_to_public_wire(outcome.diagnostic)
    assert public == {
        "diagnostic_id": (
            "b780744fd71274caaff6702f8cb1eef3fffca065953e0cd3e052fed679435d12"
        ),
        "runtime_summary": "BSL execution failed",
        "stage": "execution",
        "mapping_confidence": "exact",
        "visible_location": {
            "line": 2,
            "column": 1,
            "span": {"start": 15, "end": 16},
        },
        "related_visible_span": None,
        "excerpt": None,
        "synthetic_region": None,
    }
    encoded = json.dumps(public, ensure_ascii=False)
    for forbidden in (source, "RAW", "process_id", "9182", "private-backend"):
        assert forbidden not in encoded


def test_backend_rejects_malformed_diagnostic_without_copying_untrusted_fields() -> None:
    secret = "rdbg_pid=9182 bearer=do-not-persist"
    source_unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL,
        "cell-main",
        1,
        "b" * 64,
    )
    diagnostic = NormalizedDiagnostic(
        secret * 10,
        secret * 500,
        DiagnosticStage.PARSING,
        MappingConfidence.EXACT,
        code=secret * 10,
        source_unit=source_unit,
        visible_location=VisibleSourceLocation(
            source_unit,
            10**20,
            10**20,
            SourceSpan(0, 10**20),
        ),
        synthetic_region=secret * 10,
    )
    backend = OnecRuntimeBackend(
        "runtime-malformed-diagnostic",
        FakeDemoSession(
            RuntimeReply(
                RuntimeReplyKind.SOURCE_FAILED,
                8,
                OperationState.FAILED,
                error=secret,
                succeeded=False,
                diagnostic=diagnostic,
            )
        ),
    )

    outcome = backend.execute_bsl("Результат = ;")

    rendered = json.dumps(to_wire(outcome), ensure_ascii=False)
    assert outcome.terminal_state is AgentOperationState.UNKNOWN
    assert outcome.state_changed is StateChanged.UNKNOWN
    assert outcome.diagnostic is None
    assert outcome.messages == ()
    assert secret not in rendered


def test_backend_derives_public_summary_from_allowlisted_diagnostic_stage() -> None:
    secret = "pid=9182 token=do-not-persist"
    diagnostic = NormalizedDiagnostic(
        "c" * 64,
        secret,
        DiagnosticStage.LOWERING,
        MappingConfidence.UNKNOWN,
        code="capture_namespace_mode",
    )
    backend = OnecRuntimeBackend(
        "runtime-safe-diagnostic",
        FakeDemoSession(
            RuntimeReply(
                RuntimeReplyKind.SOURCE_FAILED,
                8,
                OperationState.FAILED,
                error=secret,
                succeeded=False,
                diagnostic=diagnostic,
            )
        ),
    )

    outcome = backend.execute_bsl("КонтекстОтладки.Значение = 1;")

    assert outcome.terminal_state is AgentOperationState.FAILED
    assert outcome.diagnostic is not None
    assert outcome.diagnostic.runtime_summary == "BSL lowering failed"
    assert outcome.messages == ("BSL lowering failed",)
    assert secret not in json.dumps(
        {
            "messages": outcome.messages,
            "diagnostic_id": outcome.diagnostic.diagnostic_id,
            "runtime_summary": outcome.diagnostic.runtime_summary,
            "code": outcome.diagnostic.code,
        },
        ensure_ascii=False,
    )


def test_direct_main_bsl_error_publishes_only_validated_diagnostic_summary(
    tmp_path: Path,
) -> None:
    """Break caught: raw MAIN exception prose reaches public operation messages."""
    source = "Результат = Ошибка();"
    source_hash = hashlib.sha256(source.encode()).hexdigest()
    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL,
        "cell-private-direct-main",
        1,
        source_hash,
    )
    mapped = mapped_visible_source(source, unit)
    diagnostic = remap_platform_diagnostic(
        parse_platform_diagnostic(
            "{<Неизвестный модуль>(1,1)}: "
            + _RAW_PRIVATE_FAILURE
            + " "
            + ("private-tail-" * 500)
        ),
        mapped,
        stage=DiagnosticStage.EXECUTION,
        visible_source_context=VisibleSourceContext({unit: source}),
    )

    class DirectMainSession:
        calls = 0

        def execute_bsl(
            self,
            exact_source: str,
            *,
            source_unit: SourceUnitRef,
            on_execution_provenance,
        ) -> RuntimeReply:  # type: ignore[no-untyped-def]
            del on_execution_provenance
            assert exact_source == source
            assert source_unit == unit
            self.calls += 1
            raise BslExecutionError(
                _RAW_PRIVATE_FAILURE,
                messages=(_RAW_PRIVATE_FAILURE,),
                diagnostic=diagnostic,
            )

    session = DirectMainSession()
    outcome = OnecRuntimeBackend(
        "runtime-private-direct-main", session  # type: ignore[arg-type]
    ).execute_bsl_with_provenance(
        source,
        source_unit=unit,
        on_execution_provenance=lambda _provenance: None,
    )

    assert session.calls == 1
    assert outcome.terminal_state is AgentOperationState.FAILED
    assert outcome.runtime_state == "failed"
    assert outcome.failure_stage == "execution"
    assert outcome.diagnostic == diagnostic
    assert outcome.messages == ("BSL execution failed",)
    _publish_and_reload_failure(
        tmp_path,
        outcome,
        source_sha256=source_hash,
    )

    private_path = (
        tmp_path
        / ".runtime"
        / "agent-service"
        / "diagnostics.private.jsonl"
    )
    private_records = [
        json.loads(line)
        for line in private_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(private_records) == 1
    assert private_records[0]["diagnostic_id"] == diagnostic.diagnostic_id
    assert private_records[0]["platform_diagnostic"] == diagnostic.platform_diagnostic
    assert private_records[0]["platform_diagnostic_truncated"] is False
    assert private_records[0]["platform_diagnostic_redacted"] is False


def test_prepared_capture_main_bsl_error_publishes_only_generic_summary(
    tmp_path: Path,
) -> None:
    """Break caught: prepared CAPTURE MAIN journals raw error messages."""
    source = "Результат = Ошибка();"
    source_hash = hashlib.sha256(source.encode()).hexdigest()
    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL,
        "cell-private-prepared-main",
        2,
        source_hash,
    )

    class PreparedMainSession:
        runtime_prepared = object()
        runtime_activated = object()

        def __init__(self) -> None:
            self.prepared: list[str] = []
            self.activated: list[object] = []
            self.executed: list[object] = []

        def prepare_main_for_capture(
            self, exact_source: str, *, source_unit: SourceUnitRef
        ) -> object:
            assert source_unit == unit
            self.prepared.append(exact_source)
            return self.runtime_prepared

        def activate_prepared_main_for_capture(self, prepared: object) -> object:
            self.activated.append(prepared)
            return self.runtime_activated

        def execute_prepared_main_for_capture(self, prepared: object) -> object:
            self.executed.append(prepared)
            ticket = object()
            return PreparedMainExecutionAttempt(
                result=None,
                error=BslExecutionError(
                    _RAW_PRIVATE_FAILURE,
                    messages=(_RAW_PRIVATE_FAILURE,),
                ),
                ticket=ticket,  # type: ignore[arg-type]
                read_dispatch=lambda submitted: submitted is ticket,
            )

    session = PreparedMainSession()
    backend = OnecRuntimeBackend(
        "runtime-private-prepared-main", session  # type: ignore[arg-type]
    )
    prepared = backend.prepare_main_for_capture(source, source_unit=unit)
    activated = backend.activate_prepared_main_for_capture(prepared)
    result = backend.run_prepared_main_until_capture(
        activated,
        intent=CaptureIntent(
            "capture-private-main",
            "operation-private-main",
            2,
            source_hash,
            1,
            (),
        ),
    )

    assert result.user_main_dispatched is True
    assert session.prepared == [source]
    assert session.activated == [session.runtime_prepared]
    assert session.executed == [session.runtime_activated]
    assert result.execution.terminal_state is AgentOperationState.FAILED
    assert result.execution.runtime_state == "failed"
    assert result.execution.messages == ("BSL execution failed",)
    _publish_and_reload_failure(
        tmp_path,
        result.execution,
        source_sha256=source_hash,
    )


def test_public_prepared_main_keeps_unresolved_dispatch_evidence() -> None:
    source = "Результат = 1;"
    source_hash = hashlib.sha256(source.encode()).hexdigest()
    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL, "cell-public-main", 1, source_hash,
    )

    class PreparedSession:
        prepared = object()

        def prepare_main_for_capture(self, exact_source, *, source_unit):
            assert exact_source == source and source_unit == unit
            return self.prepared

        def activate_prepared_main_for_capture(self, prepared):
            assert prepared is self.prepared
            return prepared

        def execute_prepared_main_for_capture(self, prepared):
            assert prepared is self.prepared
            return PreparedMainExecutionAttempt(
                None, KeyboardInterrupt(), object(), lambda _ticket: None,
            )

    backend = OnecRuntimeBackend(
        "runtime-public-main", PreparedSession()  # type: ignore[arg-type]
    )
    prepared = backend.activate_prepared_main_for_capture(
        backend.prepare_main_for_capture(source, source_unit=unit)
    )
    result = backend.run_prepared_main_until_capture(
        prepared,
        intent=CaptureIntent("capture-public", "op-public", 1, source_hash, 1, ()),
    )

    assert result.user_main_dispatched is None
    assert result.execution.terminal_state is AgentOperationState.UNKNOWN


def test_failed_reply_without_valid_diagnostic_never_publishes_reply_error(
    tmp_path: Path,
) -> None:
    """Break caught: RuntimeReply.error is treated as a public message."""
    source = "Результат = Ошибка();"
    source_hash = hashlib.sha256(source.encode()).hexdigest()
    session = FakeDemoSession(
        RuntimeReply(
            RuntimeReplyKind.MAIN_COMPLETED,
            17,
            OperationState.FAILED,
            error=_RAW_PRIVATE_FAILURE,
            succeeded=False,
            diagnostic=None,
        )
    )

    outcome = OnecRuntimeBackend(
        "runtime-private-reply", session
    ).execute_bsl(source)

    assert session.sources == [source]
    assert outcome.terminal_state is AgentOperationState.FAILED
    assert outcome.runtime_state == "failed"
    assert outcome.failure_stage == "execution"
    assert outcome.diagnostic is None
    assert outcome.messages == ("BSL execution failed",)
    _publish_and_reload_failure(
        tmp_path,
        outcome,
        source_sha256=source_hash,
    )


def test_backend_main_preparation_is_private_one_use_and_backend_owned() -> None:
    source = "Результат = 1;"
    source_hash = hashlib.sha256(source.encode()).hexdigest()
    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL,
        "cell-main",
        4,
        source_hash,
    )

    class PreparedSession(FakeDemoSession):
        def __init__(self) -> None:
            super().__init__(
                RuntimeReply(
                    RuntimeReplyKind.MAIN_COMPLETED,
                    9,
                    OperationState.COMPLETED,
                )
            )
            self.runtime_prepared = object()
            self.runtime_activated = object()
            self.activated: list[object] = []
            self.executed: list[object] = []

        def prepare_main_for_capture(
            self, exact_source: str, *, source_unit: SourceUnitRef
        ) -> object:
            assert exact_source == source
            assert source_unit == unit
            return self.runtime_prepared

        def activate_prepared_main_for_capture(self, prepared: object) -> object:
            self.activated.append(prepared)
            return self.runtime_activated

        def execute_prepared_main_for_capture(self, prepared: object) -> object:
            self.executed.append(prepared)
            ticket = object()
            return PreparedMainExecutionAttempt(
                result=self.reply, error=None, ticket=ticket,  # type: ignore[arg-type]
                read_dispatch=lambda submitted: submitted is ticket,
            )

    session = PreparedSession()
    backend = OnecRuntimeBackend("runtime-main-prepared", session)  # type: ignore[arg-type]
    other = OnecRuntimeBackend("runtime-other", PreparedSession())  # type: ignore[arg-type]
    prepared = backend.prepare_main_for_capture(source, source_unit=unit)
    intent = CaptureIntent("capture-1", "op-1", 4, source_hash, 1, ())

    assert repr(prepared) == "<redacted prepared backend main>"
    with pytest.raises(ProtocolError, match="owned prepared main"):
        other.activate_prepared_main_for_capture(prepared)
    activated = backend.activate_prepared_main_for_capture(prepared)
    assert repr(activated) == "<redacted activated backend main>"
    with pytest.raises(TypeError, match="wire-safe"):
        to_wire(activated)
    with pytest.raises(ProtocolError, match="already consumed"):
        backend.activate_prepared_main_for_capture(prepared)
    assert backend.run_prepared_main_until_capture(
        prepared, intent=intent
    ).execution.terminal_state is AgentOperationState.UNKNOWN
    assert other.run_prepared_main_until_capture(
        activated, intent=intent
    ).execution.terminal_state is AgentOperationState.UNKNOWN
    first = backend.run_prepared_main_until_capture(activated, intent=intent)
    reused = backend.run_prepared_main_until_capture(activated, intent=intent)
    mismatched_prepared = backend.prepare_main_for_capture(source, source_unit=unit)
    mismatched_activated = backend.activate_prepared_main_for_capture(
        mismatched_prepared
    )
    wrong_intent = CaptureIntent("capture-2", "op-2", 4, "b" * 64, 2, ())
    mismatch = backend.run_prepared_main_until_capture(
        mismatched_activated, intent=wrong_intent
    )
    mismatch_reuse = backend.run_prepared_main_until_capture(
        mismatched_activated, intent=intent
    )

    assert first.execution.terminal_state is AgentOperationState.COMPLETED
    assert reused.execution.terminal_state is AgentOperationState.UNKNOWN
    assert mismatch.execution.terminal_state is AgentOperationState.UNKNOWN
    assert mismatch_reuse.execution.terminal_state is AgentOperationState.UNKNOWN
    assert session.activated == [session.runtime_prepared, session.runtime_prepared]
    assert session.executed == [session.runtime_activated]


def test_backend_discards_unused_activated_main_without_target_execution() -> None:
    """Break caught: an early setup return must consume both capability layers."""
    source = "\u0420\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442 = 1;"
    source_hash = hashlib.sha256(source.encode()).hexdigest()
    unit = SourceUnitRef(SourceUnitKind.NOTEBOOK_CELL, "cell-main", 4, source_hash)

    class DiscardSession(FakeDemoSession):
        def __init__(self) -> None:
            super().__init__(
                RuntimeReply(
                    RuntimeReplyKind.MAIN_COMPLETED,
                    9,
                    OperationState.COMPLETED,
                )
            )
            self.runtime_prepared = object()
            self.runtime_activated = object()
            self.discarded: list[object] = []
            self.executed: list[object] = []

        def prepare_main_for_capture(
            self, exact_source: str, *, source_unit: SourceUnitRef
        ) -> object:
            assert exact_source == source
            assert source_unit == unit
            return self.runtime_prepared

        def activate_prepared_main_for_capture(self, prepared: object) -> object:
            assert prepared is self.runtime_prepared
            return self.runtime_activated

        def discard_prepared_main_for_capture(self, prepared: object) -> None:
            self.discarded.append(prepared)

        def execute_prepared_main_for_capture(self, prepared: object) -> object:
            self.executed.append(prepared)
            ticket = object()
            return PreparedMainExecutionAttempt(
                result=self.reply, error=None, ticket=ticket,  # type: ignore[arg-type]
                read_dispatch=lambda submitted: submitted is ticket,
            )

    session = DiscardSession()
    backend = OnecRuntimeBackend("runtime-main-discard", session)  # type: ignore[arg-type]
    other = OnecRuntimeBackend("runtime-other", DiscardSession())  # type: ignore[arg-type]
    activated = backend.activate_prepared_main_for_capture(
        backend.prepare_main_for_capture(source, source_unit=unit)
    )

    with pytest.raises(ProtocolError, match="owned activated main"):
        other.discard_prepared_main_for_capture(activated)
    backend.discard_prepared_main_for_capture(activated)

    with pytest.raises(ProtocolError, match="already consumed"):
        backend.discard_prepared_main_for_capture(activated)
    assert backend.run_prepared_main_until_capture(
        activated,
        intent=CaptureIntent("capture-1", "op-1", 4, source_hash, 1, ()),
    ).execution.terminal_state is AgentOperationState.UNKNOWN
    assert session.discarded == [session.runtime_activated]
    assert session.executed == []


def test_backend_status_keeps_public_generation_operation_and_state() -> None:
    backend = OnecRuntimeBackend(
        "runtime-1",
        FakeDemoSession(
            RuntimeReply(RuntimeReplyKind.MAIN_COMPLETED, 7, OperationState.COMPLETED),
            RuntimeStatus(OperationState.DEBUG_STOPPED, 4, 12, None),
        ),
        mode=CapabilityMode.EXPERIMENT,
    )

    descriptor = backend.status()

    assert descriptor.runtime_id == "runtime-1"
    assert descriptor.generation == 4
    assert descriptor.active_operation_id == "12"
    assert descriptor.state == "debug_stopped"
    assert descriptor.mode is CapabilityMode.EXPERIMENT


def test_backend_exposes_local_namespace_snapshot_without_value_read() -> None:
    session = FakeDemoSession(
        RuntimeReply(RuntimeReplyKind.MAIN_COMPLETED, 7, OperationState.COMPLETED)
    )
    backend = OnecRuntimeBackend("runtime-1", session)

    snapshot = backend.namespace_snapshot()

    assert snapshot == RuntimeNamespaceSnapshot(3, 5, ("Порог",))
    assert session.sources == []


def test_backend_close_is_idempotent() -> None:
    session = FakeDemoSession(
        RuntimeReply(RuntimeReplyKind.MAIN_COMPLETED, 7, OperationState.COMPLETED)
    )
    backend = OnecRuntimeBackend("runtime-1", session)

    backend.close()
    backend.close()

    assert session.close_calls == 1


def test_concrete_backend_close_retries_only_incomplete_session_cleanup() -> None:
    class RecordingTransport:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    class FailOnceProcesses:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1
            if self.close_calls == 1:
                raise RuntimeError("transient process cleanup failure")

    transport = RecordingTransport()
    processes = FailOnceProcesses()
    class Facade:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    facade = Facade()
    session = RuntimeSession(
        SimpleNamespace(
            source_root=None,
            runtime=SimpleNamespace(is_server_infobase=False),
        ),
        processes,  # type: ignore[arg-type]
        transport,  # type: ignore[arg-type]
        SimpleNamespace(heartbeat=lambda: None),  # type: ignore[arg-type]
        facade,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
    )
    backend = OnecRuntimeBackend("runtime-1", session)

    with pytest.raises(ProtocolError, match="cleanup failed"):
        backend.close()

    assert backend._closed is False
    assert session._closed is False
    backend.close()
    backend.close()

    assert transport.close_calls == 1
    assert processes.close_calls == 2
    assert facade.close_calls == 1
    assert session._closed is True
    assert backend._closed is True


def test_execute_failure_is_unknown_and_does_not_leak_backend_exception() -> None:
    backend = OnecRuntimeBackend("runtime-1", FailingDemoSession(execute=True))  # type: ignore[arg-type]

    outcome = backend.execute_bsl("Ответ = 42;")

    payload = json.dumps(to_wire(outcome))
    assert outcome.terminal_state is AgentOperationState.UNKNOWN
    assert outcome.runtime_state == "unknown"
    assert outcome.messages == ()
    assert outcome.result_present is False
    assert SecretBackendError.marker not in payload


def test_status_failure_is_typed_and_cannot_reach_backend_exception_details() -> None:
    backend = OnecRuntimeBackend("runtime-1", FailingDemoSession(execute=False))  # type: ignore[arg-type]

    with pytest.raises(RuntimeError) as raised:
        backend.status()

    error = raised.value
    payload = json.dumps(
        to_wire(
            {
                "type": type(error).__name__,
                "diagnostic_id": getattr(error, "diagnostic_id", ""),
            }
        )
    )
    rendered = " ".join(
        (
            str(error),
            repr(error),
            repr(error.__cause__),
            repr(error.__context__),
            repr(getattr(error, "__notes__", ())),
            payload,
        )
    )
    assert type(error).__name__ == "RuntimeBackendStatusError"
    assert isinstance(getattr(error, "diagnostic_id", None), str)
    assert getattr(error, "diagnostic_id", "")
    assert error.__cause__ is None
    assert error.__context__ is None
    assert SecretBackendError.marker not in rendered


def test_factory_validates_environment_before_starting_processes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from onec_runtime_mcp.agent import runtime_backend

    monkeypatch.delenv("ONEC_RUNTIME_PLATFORM_BIN", raising=False)
    monkeypatch.delenv("ONEC_RUNTIME_INFOBASE", raising=False)
    monkeypatch.delenv("ONEC_RUNTIME_USERNAME", raising=False)
    monkeypatch.delenv("ONEC_RUNTIME_PROJECT", raising=False)
    monkeypatch.delenv("ONEC_RUNTIME_SOURCE_ROOT", raising=False)
    monkeypatch.setattr(
        runtime_backend.RuntimeSession,
        "start",
        lambda *_args, **_kwargs: pytest.fail("process startup is forbidden"),
    )

    with pytest.raises(ValueError, match="ONEC_RUNTIME_PLATFORM_BIN"):
        OnecRuntimeFactory(tmp_path).start(mode=CapabilityMode.OBSERVE)


def test_runtime_backend_exposes_only_target_neutral_public_names() -> None:
    from onec_runtime_mcp.agent import runtime_backend

    assert runtime_backend.OnecRuntimeBackend
    assert runtime_backend.OnecRuntimeFactory
    assert not hasattr(runtime_backend, "ZupDemoRuntimeBackend")
    assert not hasattr(runtime_backend, "ZupDemoRuntimeFactory")


def _set_required_runtime_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    platform = tmp_path / "bin"
    platform.mkdir()
    for executable in ("1cv8.exe", "1cv8c.exe", "dbgs.exe"):
        (platform / executable).touch()
    infobase = tmp_path / "base"
    infobase.mkdir()
    (infobase / "1Cv8.1CD").touch()
    monkeypatch.setenv("ONEC_RUNTIME_PLATFORM_BIN", str(platform))
    monkeypatch.setenv("ONEC_RUNTIME_CONNECTION_STRING", f'File="{infobase}";')
    monkeypatch.setenv("ONEC_RUNTIME_USERNAME", "agent")
    monkeypatch.delenv("ONEC_RUNTIME_PROJECT", raising=False)
    monkeypatch.delenv("ONEC_RUNTIME_SOURCE_ROOT", raising=False)


def test_factory_starts_headless_for_supported_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from onec_runtime_mcp.agent import runtime_backend

    platform = tmp_path / "bin"
    platform.mkdir()
    for executable in ("1cv8.exe", "1cv8c.exe", "dbgs.exe"):
        (platform / executable).touch()
    infobase = tmp_path / "base"
    infobase.mkdir()
    (infobase / "1Cv8.1CD").touch()
    for name, value in {
        "ONEC_RUNTIME_PLATFORM_BIN": str(platform),
        "ONEC_RUNTIME_CONNECTION_STRING": f'File="{infobase}";',
        "ONEC_RUNTIME_USERNAME": "agent",
        "ONEC_RUNTIME_PROJECT": "ut",
        "ONEC_RUNTIME_SOURCE_ROOT": str(tmp_path / "ut"),
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("ONEC_RUNTIME_EXTENSION_MODE", raising=False)
    session = FakeDemoSession(
        RuntimeReply(RuntimeReplyKind.MAIN_COMPLETED, 1, OperationState.COMPLETED)
    )
    starts: list[tuple[object, dict[str, object]]] = []
    monkeypatch.setattr(
        runtime_backend.RuntimeSession,
        "start",
        lambda config: starts.append((config, {})) or session,
    )
    monkeypatch.setattr(runtime_backend, "AgentRuntimeSession", lambda core: core)

    backend = OnecRuntimeFactory(tmp_path).start(mode=CapabilityMode.OBSERVE)

    started_config = cast(RuntimeSessionConfig, starts[0][0])
    assert backend.status().mode is CapabilityMode.OBSERVE
    assert started_config.runtime.workspace == tmp_path.resolve()
    assert started_config.runtime.infobase_dir == infobase.resolve()
    assert not started_config.runtime.is_server_infobase
    assert started_config.runtime.debug_host == "127.0.0.1"
    assert started_config.runtime.debug_port == 1550
    assert started_config.runtime.debug_alias is None
    assert started_config.capture_source == CaptureSourceConfig(
        "ut", tmp_path / "ut"
    )
    assert started_config.extension_mode is ExtensionMode.AUTO
    assert not hasattr(started_config.runtime, "extension_" + "profile")
    assert starts[0][1] == {}


def test_factory_maps_server_infobase_and_debugger_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from onec_runtime_mcp.agent import runtime_backend

    platform = tmp_path / "bin"
    platform.mkdir()
    for executable in ("1cv8.exe", "1cv8c.exe"):
        (platform / executable).touch()
    monkeypatch.setenv("ONEC_RUNTIME_PLATFORM_BIN", str(platform))
    monkeypatch.delenv("ONEC_RUNTIME_INFOBASE", raising=False)
    monkeypatch.setenv(
        "ONEC_RUNTIME_CONNECTION_STRING",
        'Srvr="localhost";Ref="runtime_test";',
    )
    monkeypatch.setenv("ONEC_RUNTIME_USERNAME", "agent")
    monkeypatch.setenv("ONEC_RUNTIME_DEBUG_HOST", "debugger.internal")
    monkeypatch.setenv("ONEC_RUNTIME_DEBUG_PORT", "1650")
    monkeypatch.setenv("ONEC_RUNTIME_DEBUG_ALIAS", "runtime-alias")
    monkeypatch.delenv("ONEC_RUNTIME_PROJECT", raising=False)
    monkeypatch.delenv("ONEC_RUNTIME_SOURCE_ROOT", raising=False)
    starts: list[RuntimeSessionConfig] = []
    monkeypatch.setattr(
        RuntimeSession,
        "start",
        lambda config: starts.append(config)
        or FakeDemoSession(
            RuntimeReply(RuntimeReplyKind.MAIN_COMPLETED, 1, OperationState.COMPLETED)
        ),
    )
    monkeypatch.setattr(runtime_backend, "AgentRuntimeSession", lambda core: core)

    OnecRuntimeFactory(tmp_path).start(mode=CapabilityMode.OBSERVE)

    runtime = starts[0].runtime
    assert runtime.connection_string == 'Srvr="localhost";Ref="runtime_test";'
    assert runtime.infobase_arguments == ("/S", r"localhost\runtime_test")
    assert runtime.debug_host == "debugger.internal"
    assert runtime.debug_port == 1650
    assert runtime.debug_alias == "runtime-alias"


def test_factory_requires_connection_string_even_with_legacy_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_required_runtime_environment(monkeypatch, tmp_path)
    file_value = str(tmp_path / "private-file-base")
    server_value = r"private-host\private-server-base"
    monkeypatch.setenv("ONEC_RUNTIME_INFOBASE", file_value)
    monkeypatch.setenv("ONEC_RUNTIME_SERVER_INFOBASE", server_value)
    monkeypatch.delenv("ONEC_RUNTIME_CONNECTION_STRING", raising=False)
    monkeypatch.setattr(
        RuntimeSession,
        "start",
        lambda *_args, **_kwargs: pytest.fail("core startup is forbidden"),
    )

    with pytest.raises(ValueError) as raised:
        OnecRuntimeFactory(tmp_path).start(mode=CapabilityMode.OBSERVE)

    message = str(raised.value)
    assert "ONEC_RUNTIME_CONNECTION_STRING" in message
    assert file_value not in message
    assert server_value not in message


def test_factory_requires_connection_string_when_no_target_is_configured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    platform = tmp_path / "bin"
    platform.mkdir()
    monkeypatch.setenv("ONEC_RUNTIME_PLATFORM_BIN", str(platform))
    monkeypatch.delenv("ONEC_RUNTIME_INFOBASE", raising=False)
    monkeypatch.delenv("ONEC_RUNTIME_SERVER_INFOBASE", raising=False)
    monkeypatch.delenv("ONEC_RUNTIME_CONNECTION_STRING", raising=False)
    monkeypatch.setenv("ONEC_RUNTIME_USERNAME", "agent")
    monkeypatch.setattr(
        RuntimeSession,
        "start",
        lambda *_args, **_kwargs: pytest.fail("core startup is forbidden"),
    )

    with pytest.raises(ValueError, match="ONEC_RUNTIME_CONNECTION_STRING"):
        OnecRuntimeFactory(tmp_path).start(mode=CapabilityMode.OBSERVE)


@pytest.mark.parametrize(
    "configured_port",
    ("", "0", "65536", "+1550", "1_550", "1550.0", "not-a-port"),
)
def test_factory_rejects_malformed_debug_port_before_core_start(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    configured_port: str,
) -> None:
    _set_required_runtime_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("ONEC_RUNTIME_DEBUG_PORT", configured_port)
    monkeypatch.setattr(
        RuntimeSession,
        "start",
        lambda *_args, **_kwargs: pytest.fail("core startup is forbidden"),
    )

    with pytest.raises(ValueError, match="ONEC_RUNTIME_DEBUG_PORT"):
        OnecRuntimeFactory(tmp_path).start(mode=CapabilityMode.OBSERVE)


def test_factory_passes_manual_extension_mode_to_core(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from onec_runtime_mcp.agent import runtime_backend

    _set_required_runtime_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("ONEC_RUNTIME_EXTENSION_MODE", "manual")
    starts: list[RuntimeSessionConfig] = []
    monkeypatch.setattr(
        RuntimeSession,
        "start",
        lambda config: starts.append(config)
        or FakeDemoSession(
            RuntimeReply(RuntimeReplyKind.MAIN_COMPLETED, 1, OperationState.COMPLETED)
        ),
    )
    monkeypatch.setattr(runtime_backend, "AgentRuntimeSession", lambda core: core)

    OnecRuntimeFactory(tmp_path).start(mode=CapabilityMode.OBSERVE)

    assert starts[0].extension_mode is ExtensionMode.MANUAL


def test_factory_rejects_invalid_extension_mode_before_core_start(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_required_runtime_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("ONEC_RUNTIME_EXTENSION_MODE", "MANUAL")
    monkeypatch.setattr(
        RuntimeSession,
        "start",
        lambda *_args, **_kwargs: pytest.fail("core startup is forbidden"),
    )

    with pytest.raises(ValueError, match="ONEC_RUNTIME_EXTENSION_MODE.*auto.*manual"):
        OnecRuntimeFactory(tmp_path).start(mode=CapabilityMode.OBSERVE)


def test_factory_retains_and_reconciles_incomplete_startup_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from onec_runtime_mcp.agent import runtime_backend

    _set_required_runtime_environment(monkeypatch, tmp_path)
    cleanup_calls = 0
    cleanup_succeeds = False

    def retry_cleanup() -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        if not cleanup_succeeds:
            raise ProtocolError("private cleanup failure")

    startup_error = ProtocolError("private startup failure")
    setattr(startup_error, "retry_cleanup", retry_cleanup)
    runtime_start_calls = 0

    def start_runtime(*_args: object, **_kwargs: object) -> object:
        nonlocal runtime_start_calls
        runtime_start_calls += 1
        if runtime_start_calls == 1:
            raise startup_error
        return FakeDemoSession(
            RuntimeReply(RuntimeReplyKind.MAIN_COMPLETED, 1, OperationState.COMPLETED)
        )

    monkeypatch.setattr(
        RuntimeSession,
        "start",
        start_runtime,
    )
    monkeypatch.setattr(runtime_backend, "AgentRuntimeSession", lambda core: core)
    factory = OnecRuntimeFactory(tmp_path)

    with pytest.raises(ProtocolError) as raised:
        factory.start(mode=CapabilityMode.OBSERVE)

    assert raised.value is startup_error
    assert factory.reconcile_unknown_startup(operation_id="startup-1") is False
    assert cleanup_calls == 1
    assert runtime_start_calls == 1
    cleanup_succeeds = True
    backend = factory.start(mode=CapabilityMode.OBSERVE)
    assert cleanup_calls == 2
    assert runtime_start_calls == 2
    assert backend.status().mode is CapabilityMode.OBSERVE
    assert factory.reconcile_unknown_startup(operation_id="startup-1") is False


def test_factory_starts_without_optional_capture_source_pair(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from onec_runtime_mcp.agent import runtime_backend

    platform = tmp_path / "bin"
    platform.mkdir()
    for executable in ("1cv8.exe", "1cv8c.exe", "dbgs.exe"):
        (platform / executable).touch()
    infobase = tmp_path / "base"
    infobase.mkdir()
    (infobase / "1Cv8.1CD").touch()
    monkeypatch.setenv("ONEC_RUNTIME_PLATFORM_BIN", str(platform))
    monkeypatch.setenv("ONEC_RUNTIME_CONNECTION_STRING", f'File="{infobase}";')
    monkeypatch.setenv("ONEC_RUNTIME_USERNAME", "agent")
    monkeypatch.delenv("ONEC_RUNTIME_PROJECT", raising=False)
    monkeypatch.delenv("ONEC_RUNTIME_SOURCE_ROOT", raising=False)
    session = FakeDemoSession(
        RuntimeReply(RuntimeReplyKind.MAIN_COMPLETED, 1, OperationState.COMPLETED)
    )
    starts: list[RuntimeSessionConfig] = []

    def start(config: RuntimeSessionConfig) -> FakeDemoSession:
        starts.append(config)
        return session

    monkeypatch.setattr(
        RuntimeSession,
        "start",
        start,
    )
    monkeypatch.setattr(runtime_backend, "AgentRuntimeSession", lambda core: core)

    OnecRuntimeFactory(tmp_path).start(mode=CapabilityMode.OBSERVE)

    assert starts[0].capture_source is None


def test_agent_session_without_bootstrap_source_fails_only_on_symbolic_resolution() -> None:
    from onec_runtime_mcp.agent.runtime_session import AgentRuntimeSession

    from tests.unit.test_capture_source_configuration import (
        RecordingCaptureApi,
        bare_capture_session,
    )

    core = bare_capture_session(RecordingCaptureApi())
    session = AgentRuntimeSession(core)

    with pytest.raises(CaptureSourceNotConfigured):
        session.resolve_capture_points(
            (CapturePointRequest("point", "ut", "Продажи", "Провести", 3),)
        )

    assert vars(session) == {"core": core}


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    ("present", "missing"),
    (
        ("ONEC_RUNTIME_PROJECT", "ONEC_RUNTIME_SOURCE_ROOT"),
        ("ONEC_RUNTIME_SOURCE_ROOT", "ONEC_RUNTIME_PROJECT"),
    ),
)
def test_factory_rejects_incomplete_optional_capture_source_pair_before_start(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    present: str,
    missing: str,
) -> None:
    from onec_runtime_mcp.agent import runtime_backend

    platform = tmp_path / "bin"
    platform.mkdir()
    for executable in ("1cv8.exe", "1cv8c.exe", "dbgs.exe"):
        (platform / executable).touch()
    infobase = tmp_path / "base"
    infobase.mkdir()
    (infobase / "1Cv8.1CD").touch()
    monkeypatch.setenv("ONEC_RUNTIME_PLATFORM_BIN", str(platform))
    monkeypatch.setenv("ONEC_RUNTIME_CONNECTION_STRING", f'File="{infobase}";')
    monkeypatch.setenv("ONEC_RUNTIME_USERNAME", "agent")
    monkeypatch.setenv(present, "ut")
    monkeypatch.delenv(missing, raising=False)
    monkeypatch.setattr(
        RuntimeSession,
        "start",
        lambda *_args, **_kwargs: pytest.fail("process startup is forbidden"),
    )

    with pytest.raises(ValueError) as raised:
        OnecRuntimeFactory(tmp_path).start(mode=CapabilityMode.OBSERVE)

    assert "ONEC_RUNTIME_PROJECT" in str(raised.value)
    assert "ONEC_RUNTIME_SOURCE_ROOT" in str(raised.value)


@pytest.mark.parametrize("mode", [CapabilityMode.COMMIT, CapabilityMode.ADMIN])
def test_factory_rejects_unsupported_modes_before_environment_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: CapabilityMode,
) -> None:
    with pytest.raises(CapabilityDenied, match="observe and experiment"):
        OnecRuntimeFactory(tmp_path).start(mode=mode)


def test_factory_closes_started_session_when_adapter_construction_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from onec_runtime_mcp.agent import runtime_backend

    session = FakeDemoSession(
        RuntimeReply(RuntimeReplyKind.MAIN_COMPLETED, 1, OperationState.COMPLETED)
    )
    monkeypatch.setattr(runtime_backend, "_required_environment", lambda _name: str(tmp_path))
    monkeypatch.setattr(runtime_backend, "RuntimeConfig", lambda **_kwargs: object())
    monkeypatch.setattr(
        runtime_backend.RuntimeSession,
        "start",
        lambda *_args, **_kwargs: session,
    )
    monkeypatch.setattr(runtime_backend, "AgentRuntimeSession", lambda core: core)

    def fail_adapter(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("adapter construction failed")

    monkeypatch.setattr(runtime_backend, "OnecRuntimeBackend", fail_adapter)

    with pytest.raises(RuntimeError, match="adapter construction failed"):
        OnecRuntimeFactory(tmp_path).start(mode=CapabilityMode.EXPERIMENT)

    assert session.close_calls == 1
