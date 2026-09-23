"""Target-neutral MCP adapter for the headless 1C runtime."""

from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path
from secrets import token_urlsafe
from threading import RLock
from collections.abc import Mapping
from typing import Protocol
from collections.abc import Callable
from uuid import uuid4

import psutil

from onec_runtime_mcp.agent.contracts import (
    AgentOperationState,
    CapabilityMode,
    OperationExecutionProvenance,
    RuntimeDescriptor,
    StateChanged,
    sanitize_normalized_diagnostic,
)
from onec_runtime_mcp.agent.operations import BackendExecution
from onec_runtime_mcp.agent.capture_contracts import CapturePointRequest, ResolvedCapturePoint
from onec_runtime_mcp.agent.capture_service import (
    CaptureArming,
    CaptureContinuationAttempt,
    CaptureContinuationEvidence,
    CaptureIntent,
    CaptureRunOutcome,
    CaptureSuccessorPreparationError,
    CaptureStop,
)
from onec_runtime_mcp.agent.observation import ValueSelection
from onec_runtime_mcp.agent.observation import ManagerOrigin
from onec_runtime_mcp.agent.capture_contracts import CaptureFence
from onec_runtime_mcp.agent.runtime_session import AgentRuntimeSession
from onec_runtime.capture_source import CaptureSourceConfig
from onec_runtime.bsl import NormalizedDiagnostic, SemanticLoweringError, SourceUnitRef
from onec_runtime.config import RuntimeConfig
from onec_runtime.bsl.lexer import BslLexError
from onec_runtime.bsl.parser_target import BslParseError
from onec_runtime.errors import BslExecutionError, ProtocolError
from onec_runtime.execution.public_facade import PreparedMainExecutionAttempt
from onec_runtime.runtime_models import (
    OperationState,
    PartialWritebackError,
    RuntimeNamespaceSnapshot,
    RuntimeReply,
    RuntimeReplyKind,
    RuntimeStatus,
)
from onec_runtime.session import ExtensionMode, RuntimeSession, RuntimeSessionConfig


_PUBLIC_EXECUTION_FAILURE_MESSAGE = "BSL execution failed"


class CapabilityDenied(RuntimeError):
    """The requested service capability is unavailable for this runtime."""


class RuntimeBackendStatusError(RuntimeError):
    """A sanitized status failure that exposes only its diagnostic identifier."""

    def __init__(self, diagnostic_id: str) -> None:
        self.diagnostic_id = diagnostic_id
        super().__init__(f"runtime status is unavailable ({diagnostic_id})")


class CaptureHypothesisPreparationError(ValueError):
    """Deterministic, normalized CAPTURE preparation failure."""

    def __init__(self, diagnostic: NormalizedDiagnostic) -> None:
        safe = sanitize_normalized_diagnostic(diagnostic)
        if safe is None or safe.stage.value not in {
            "parsing",
            "lowering",
        }:
            raise ValueError("capture preparation diagnostic is invalid")
        self.diagnostic = safe
        self.stage = safe.stage.value
        super().__init__(self.stage)


class MainPreparationSourceError(ValueError):
    """Deterministic, normalized MAIN preparation failure."""

    def __init__(self, diagnostic: NormalizedDiagnostic) -> None:
        safe = sanitize_normalized_diagnostic(diagnostic)
        if safe is None or safe.stage.value not in {"parsing", "lowering"}:
            raise ValueError("MAIN preparation diagnostic is invalid")
        self.diagnostic = safe
        self.stage = safe.stage.value
        super().__init__(self.stage)


class _PreparedBackendMain:
    """Backend-owned wrapper around an opaque RuntimeApi preparation."""

    __slots__ = ("__owner", "__token", "__source_sha256", "__runtime_prepared")

    def __init__(
        self,
        owner: object,
        token: str,
        source_sha256: str,
        runtime_prepared: object,
    ) -> None:
        object.__setattr__(self, "_PreparedBackendMain__owner", owner)
        object.__setattr__(self, "_PreparedBackendMain__token", token)
        object.__setattr__(
            self, "_PreparedBackendMain__source_sha256", source_sha256
        )
        object.__setattr__(
            self, "_PreparedBackendMain__runtime_prepared", runtime_prepared
        )

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("prepared backend MAIN executions are immutable")

    def __repr__(self) -> str:
        return "<redacted prepared backend main>"

    def __deepcopy__(self, memo: dict[int, object]) -> str:
        del memo
        return "<redacted prepared backend main>"

    def contents(self, owner: object) -> tuple[str, str, object]:
        if self.__owner is not owner:
            raise ProtocolError("Runtime backend requires an owned prepared main")
        return self.__token, self.__source_sha256, self.__runtime_prepared


class _ActivatedBackendMain:
    """Backend-owned wrapper around an activated RuntimeApi MAIN capability."""

    __slots__ = ("__owner", "__token", "__source_sha256", "__runtime_activated")

    def __init__(
        self,
        owner: object,
        token: str,
        source_sha256: str,
        runtime_activated: object,
    ) -> None:
        object.__setattr__(self, "_ActivatedBackendMain__owner", owner)
        object.__setattr__(self, "_ActivatedBackendMain__token", token)
        object.__setattr__(
            self, "_ActivatedBackendMain__source_sha256", source_sha256
        )
        object.__setattr__(
            self, "_ActivatedBackendMain__runtime_activated", runtime_activated
        )

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("activated backend MAIN executions are immutable")

    def __repr__(self) -> str:
        return "<redacted activated backend main>"

    def __deepcopy__(self, memo: dict[int, object]) -> str:
        del memo
        return "<redacted activated backend main>"

    def contents(self, owner: object) -> tuple[str, str, object]:
        if self.__owner is not owner:
            raise ProtocolError("Runtime backend requires an owned activated main")
        return self.__token, self.__source_sha256, self.__runtime_activated


class RuntimeBackend(Protocol):
    runtime_id: str

    def execute_bsl(self, source: str) -> BackendExecution:
        raise RuntimeError("Protocol declaration")

    def execute_bsl_with_provenance(
        self,
        source: str,
        *,
        source_unit: SourceUnitRef,
        on_execution_provenance: Callable[
            [OperationExecutionProvenance],
            None,
        ],
    ) -> BackendExecution:
        raise RuntimeError("Protocol declaration")

    def prepare_main_for_capture(
        self, source: str, *, source_unit: SourceUnitRef
    ) -> object:
        raise RuntimeError("Protocol declaration")

    def activate_prepared_main_for_capture(self, prepared: object) -> object:
        raise RuntimeError("Protocol declaration")

    def prepared_main_execution_provenance(
        self,
        prepared: object,
    ) -> OperationExecutionProvenance:
        raise RuntimeError("Protocol declaration")

    def discard_prepared_main_for_capture(self, prepared: object) -> None:
        raise RuntimeError("Protocol declaration")

    def run_prepared_main_until_capture(
        self, prepared: object, *, intent: CaptureIntent
    ) -> CaptureRunOutcome:
        raise RuntimeError("Protocol declaration")

    def prepare_capture_hypothesis(
        self, source: str, capture: CaptureFence
    ) -> object:
        raise RuntimeError("Protocol declaration")

    def execute_capture_hypothesis(
        self, prepared: object, capture: CaptureFence
    ) -> BackendExecution:
        raise RuntimeError("Protocol declaration")

    def prepared_capture_hypothesis_provenance(
        self,
        prepared: object,
    ) -> OperationExecutionProvenance:
        raise RuntimeError("Protocol declaration")

    def quarantine_capture_inspection(self, capture: CaptureFence) -> None:
        raise RuntimeError("Protocol declaration")


    def status(self) -> RuntimeDescriptor:
        raise RuntimeError("Protocol declaration")

    @property
    def is_closed(self) -> bool:
        raise RuntimeError("Protocol declaration")

    def namespace_snapshot(self) -> RuntimeNamespaceSnapshot:
        raise RuntimeError("Protocol declaration")

    def validate_value_reference(self, handle: str) -> str:
        raise RuntimeError("Protocol declaration")

    def materialize_value(self, handle: str, **options: object) -> object:
        raise RuntimeError("Protocol declaration")

    def materialize_table(self, handle: str, **options: object) -> object:
        raise RuntimeError("Protocol declaration")

    def materialization_kind(self, handle: str, *, timeout_s: float | None = None) -> str:
        raise RuntimeError("Protocol declaration")

    def materialize_value_payload(self, handle: str, **options: object) -> bytes:
        raise RuntimeError("Protocol declaration")

    def materialize_table_payload(self, handle: str, **options: object) -> bytes:
        raise RuntimeError("Protocol declaration")

    def project_value_payload(
        self, handle: str, selection: ValueSelection, **options: object
    ) -> tuple[str, bytes]:
        raise RuntimeError("Protocol declaration")

    def close(self) -> None:
        raise RuntimeError("Protocol declaration")

    def resolve_capture_points(
        self, points: tuple[CapturePointRequest, ...]
    ) -> tuple[ResolvedCapturePoint, ...]:
        raise RuntimeError("Protocol declaration")

    def arm_capture(self, intent: CaptureIntent) -> CaptureArming:
        raise RuntimeError("Protocol declaration")

    def prepare_capture_successor(
        self,
        intent: CaptureIntent | None,
        *,
        attempt: CaptureContinuationAttempt,
    ) -> object:
        raise RuntimeError("Protocol declaration")

    def continue_capture(
        self,
        *,
        dirty_roots: tuple[str, ...],
        attempt_id: str,
    ) -> CaptureRunOutcome:
        raise RuntimeError("Protocol declaration")

    def disarm_capture(self, *, policy: str) -> None:
        raise RuntimeError("Protocol declaration")

    def frame_variables(self, capture: CaptureFence, *, filters: Mapping[str, object], cursor: int, limit: int, timeout_s: float | None = None) -> Mapping[str, object]:
        raise RuntimeError("Protocol declaration")

    def capture_stack(self, capture: CaptureFence, *, cursor: int, limit: int, timeout_s: float | None = None) -> Mapping[str, object]:
        raise RuntimeError("Protocol declaration")

    def capture_frame(self, capture: CaptureFence, *, level: int, cursor: int, limit: int, name: str | None = None, timeout_s: float | None = None) -> Mapping[str, object]:
        raise RuntimeError("Protocol declaration")

    def resolve_manager_origin(self, capture: CaptureFence, origin: ManagerOrigin, *, timeout_s: float | None = None) -> Mapping[str, object]:
        raise RuntimeError("Protocol declaration")

    def temporary_tables(self, capture: CaptureFence, manager_handle: str, *, names: tuple[str, ...] | None, cursor: int, limit: int, selection: ValueSelection | None, timeout_s: float | None = None) -> Mapping[str, object]:
        raise RuntimeError("Protocol declaration")

    def resume_capture(self, *, dirty_roots: tuple[str, ...] = ()) -> RuntimeReply:
        raise RuntimeError("Protocol declaration")


class RuntimeBackendFactory(Protocol):
    def start(self, *, mode: CapabilityMode) -> RuntimeBackend:
        raise RuntimeError("Protocol declaration")


class OnecRuntimeBackend:
    """Redact runtime replies into the agent's durable backend contract."""

    def __init__(
        self,
        runtime_id: str,
        session: AgentRuntimeSession,
        *,
        mode: CapabilityMode = CapabilityMode.OBSERVE,
    ) -> None:
        if not isinstance(runtime_id, str) or not runtime_id:
            raise ValueError("runtime_id must be a non-empty string")
        if not isinstance(mode, CapabilityMode):
            raise TypeError("mode must be a CapabilityMode")
        self.runtime_id = runtime_id
        self._session = session
        self._mode = mode
        self._closed = False
        self._armed_capture_intent: CaptureIntent | None = None
        self._prepared_main_owner = object()
        self._consumed_main_preparations: set[str] = set()

    def execute_bsl(self, source: str) -> BackendExecution:
        try:
            reply = self._session.execute_bsl(source)
        except BslExecutionError as error:
            return self._execution_from_bsl_error(
                error,
                runtime_state="failed",
            )
        except BaseException:
            return BackendExecution(
                terminal_state=AgentOperationState.UNKNOWN,
                messages=(),
                result_present=False,
                runtime_state="unknown",
            )
        return self._execution_from_reply(reply)

    def execute_bsl_with_provenance(
        self,
        source: str,
        *,
        source_unit: SourceUnitRef,
        on_execution_provenance: Callable[
            [OperationExecutionProvenance],
            None,
        ],
    ) -> BackendExecution:
        if not callable(on_execution_provenance):
            raise TypeError("on_execution_provenance must be callable")
        callback_failure: list[BaseException] = []

        def persist(provenance: OperationExecutionProvenance) -> None:
            try:
                on_execution_provenance(provenance)
            except BaseException as error:
                callback_failure.append(error)
                raise

        try:
            reply = self._session.execute_bsl(
                source,
                source_unit=source_unit,
                on_execution_provenance=persist,
            )
        except BaseException as error:
            if callback_failure:
                raise callback_failure[0]
            if isinstance(error, BslExecutionError):
                return self._execution_from_bsl_error(
                    error,
                    runtime_state="failed",
                )
            return BackendExecution(
                terminal_state=AgentOperationState.UNKNOWN,
                messages=(),
                result_present=False,
                runtime_state="unknown",
            )
        return self._execution_from_reply(reply)

    def prepare_main_for_capture(
        self, source: str, *, source_unit: SourceUnitRef
    ) -> object:
        if self._closed:
            raise ProtocolError("runtime backend is closed")
        if (
            not isinstance(source_unit, SourceUnitRef)
            or source_unit.source_sha256
            != sha256(source.encode("utf-8")).hexdigest()
        ):
            raise ProtocolError("MAIN preparation source identity is invalid")
        try:
            runtime_prepared = self._session.prepare_main_for_capture(
                source,
                source_unit=source_unit,
            )
        except (BslParseError, BslLexError, SemanticLoweringError):
            raise ProtocolError("MAIN preparation was not normalized") from None
        if isinstance(runtime_prepared, RuntimeReply):
            if (
                runtime_prepared.kind is RuntimeReplyKind.SOURCE_FAILED
                and runtime_prepared.diagnostic is not None
            ):
                safe = sanitize_normalized_diagnostic(
                    runtime_prepared.diagnostic
                )
                if safe is None or safe.stage.value not in {
                    "parsing",
                    "lowering",
                }:
                    raise ProtocolError(
                        "MAIN preparation returned an invalid diagnostic"
                    )
                raise MainPreparationSourceError(safe) from None
            raise ProtocolError("MAIN preparation returned an invalid reply")
        token = f"backend_main_prepared_{uuid4().hex}"
        return _PreparedBackendMain(
            self._prepared_main_owner,
            token,
            source_unit.source_sha256,
            runtime_prepared,
        )

    def activate_prepared_main_for_capture(self, prepared: object) -> object:
        if self._closed:
            raise ProtocolError("runtime backend is closed")
        if not isinstance(prepared, _PreparedBackendMain):
            raise ProtocolError("runtime backend requires a prepared main")
        token, prepared_source_sha256, runtime_prepared = prepared.contents(
            self._prepared_main_owner
        )
        if token in self._consumed_main_preparations:
            raise ProtocolError("Prepared backend main was already consumed")
        self._consumed_main_preparations.add(token)
        runtime_activated = self._session.activate_prepared_main_for_capture(
            runtime_prepared
        )
        return _ActivatedBackendMain(
            self._prepared_main_owner,
            f"backend_main_activated_{uuid4().hex}",
            prepared_source_sha256,
            runtime_activated,
        )

    def prepared_main_execution_provenance(
        self,
        prepared: object,
    ) -> OperationExecutionProvenance:
        if self._closed:
            raise ProtocolError("runtime backend is closed")
        if not isinstance(prepared, _PreparedBackendMain):
            raise ProtocolError("runtime backend requires a prepared main")
        token, _, runtime_prepared = prepared.contents(
            self._prepared_main_owner
        )
        if token in self._consumed_main_preparations:
            raise ProtocolError("Prepared backend main was already consumed")
        provenance = self._session.prepared_main_execution_provenance(
            runtime_prepared
        )
        if not isinstance(provenance, OperationExecutionProvenance):
            raise ProtocolError("prepared MAIN provenance is invalid")
        return provenance

    def discard_prepared_main_for_capture(self, prepared: object) -> None:
        if not isinstance(prepared, _ActivatedBackendMain):
            raise ProtocolError("runtime backend requires an activated main")
        token, _, runtime_activated = prepared.contents(self._prepared_main_owner)
        if token in self._consumed_main_preparations:
            raise ProtocolError("Activated backend main was already consumed")
        self._consumed_main_preparations.add(token)
        self._session.discard_prepared_main_for_capture(runtime_activated)

    def prepare_capture_hypothesis(
        self, source: str, capture: CaptureFence
    ) -> object:
        if self._closed:
            raise ProtocolError("runtime backend is closed")
        try:
            prepared = self._session.prepare_capture_hypothesis(source, capture)
        except (BslParseError, BslLexError, SemanticLoweringError):
            # RuntimeSession is the structured normalization boundary.
            # Raw source exceptions here mean a non-conforming session and
            # cannot be truthfully projected without its exact mapped branch.
            raise ProtocolError("CAPTURE preparation was not normalized") from None
        if isinstance(prepared, RuntimeReply):
            if (
                prepared.kind is RuntimeReplyKind.SOURCE_FAILED
                and prepared.diagnostic is not None
            ):
                raise CaptureHypothesisPreparationError(
                    prepared.diagnostic
                ) from None
            raise ProtocolError("CAPTURE preparation returned an invalid reply")
        return prepared

    def prepared_capture_hypothesis_provenance(
        self,
        prepared: object,
    ) -> OperationExecutionProvenance:
        if self._closed:
            raise ProtocolError("runtime backend is closed")
        provenance = self._session.prepared_capture_hypothesis_provenance(
            prepared
        )
        if not isinstance(provenance, OperationExecutionProvenance):
            raise ProtocolError("prepared CAPTURE provenance is invalid")
        return provenance

    def execute_capture_hypothesis(
        self, prepared: object, capture: CaptureFence
    ) -> BackendExecution:
        if self._closed:
            raise ProtocolError("runtime backend is closed")
        try:
            reply = self._session.execute_prepared_capture_hypothesis(
                prepared, capture
            )
        except BslExecutionError as error:
            return self._execution_from_bsl_error(
                error,
                runtime_state="captured",
            )
        except BaseException:
            return BackendExecution(
                AgentOperationState.UNKNOWN,
                (),
                False,
                "unknown",
            )
        return self._execution_from_reply(reply)

    def quarantine_capture_inspection(self, capture: CaptureFence) -> None:
        if self._closed:
            raise ProtocolError("runtime backend is closed")
        try:
            self._session.quarantine_capture_inspection(capture)
        finally:
            self._armed_capture_intent = None


    def resolve_capture_points(
        self, points: tuple[CapturePointRequest, ...]
    ) -> tuple[ResolvedCapturePoint, ...]:
        return self._session.resolve_capture_points(points)

    def arm_capture(self, intent: CaptureIntent) -> CaptureArming:
        arming = self._session.arm_capture_intent(intent)
        self._armed_capture_intent = intent
        return arming

    def prepare_capture_successor(
        self,
        intent: CaptureIntent | None,
        *,
        attempt: CaptureContinuationAttempt,
    ) -> object:
        previous_intent = self._armed_capture_intent
        try:
            session_admission = self._session.prepare_capture_successor(
                intent, attempt=attempt
            )
        except BaseException as error:
            uncertain = True
            classifier = getattr(
                self._session, "continuation_admission_is_uncertain", None
            )
            if callable(classifier):
                try:
                    uncertain = bool(classifier())
                except BaseException:
                    uncertain = True
            raise CaptureSuccessorPreparationError(
                uncertain=uncertain
            ) from error
        self._armed_capture_intent = intent
        backend = self

        class _BackendContinuationAdmission:
            arming = session_admission.arming
            closed = False

            def commit(self) -> None:
                if not self.closed:
                    session_admission.commit()
                    self.closed = True

            def rollback(self) -> None:
                if not self.closed:
                    session_admission.rollback()
                    backend._armed_capture_intent = previous_intent
                    self.closed = True

            def quarantine(self) -> None:
                if not self.closed:
                    try:
                        session_admission.quarantine()
                    finally:
                        backend._armed_capture_intent = None
                        self.closed = True

        return _BackendContinuationAdmission()

    def continue_capture(
        self,
        *,
        dirty_roots: tuple[str, ...],
        attempt_id: str,
    ) -> CaptureRunOutcome:
        if self._closed:
            raise ProtocolError("runtime backend is closed")
        intent = self._armed_capture_intent
        try:
            reply = self._session.resume_capture(
                dirty_roots=dirty_roots,
                continuation_attempt_id=attempt_id,
            )
        except PartialWritebackError:
            evidence = self._session.continuation_attempt_evidence(attempt_id)
            continuation = CaptureContinuationEvidence(
                evidence.root_statuses, evidence.continue_state
            )
            return CaptureRunOutcome(
                BackendExecution(
                    AgentOperationState.FAILED, (), False, "partial_writeback_failure",
                    failure_stage="execution",
                ),
                partial_results=dict(evidence.root_statuses),
                continuation=continuation,
            )
        except BaseException:
            try:
                evidence = self._session.continuation_attempt_evidence(attempt_id)
                continuation = CaptureContinuationEvidence(
                    evidence.root_statuses, evidence.continue_state
                )
            except BaseException:
                continuation = None
            return CaptureRunOutcome(
                BackendExecution(
                    AgentOperationState.UNKNOWN, (), False, "unknown"
                ),
                partial_results=(
                    {}
                    if continuation is None
                    else dict(continuation.root_statuses)
                ),
                continuation=continuation,
            )
        execution = self._execution_from_reply(reply)
        evidence = self._session.continuation_attempt_evidence(attempt_id)
        continuation = CaptureContinuationEvidence(
            evidence.root_statuses, evidence.continue_state
        )
        if execution.terminal_state is not AgentOperationState.CAPTURED or intent is None:
            return CaptureRunOutcome(
                execution,
                partial_results=dict(evidence.root_statuses),
                continuation=continuation,
            )
        location = self._session.capture_location(
            reply.location, ticket_id=reply.capture_ticket, intent=intent
        )
        if location is None or reply.stop_sequence is None:
            return CaptureRunOutcome(
                execution,
                partial_results=dict(evidence.root_statuses),
                continuation=continuation,
            )
        return CaptureRunOutcome(
            execution,
            CaptureStop(reply.stop_sequence, location, reply.operation_id, reply.capture_ticket, reply.observed_command_id),
            dict(evidence.root_statuses),
            continuation,
        )

    def rearm_capture_successor(self, points: tuple[CapturePointRequest, ...]) -> None:
        if points:
            raise ValueError("empty successor rearm accepts no capture points")
        self._session.rearm_capture_successor(())

    def run_prepared_main_until_capture(
        self, prepared: object, *, intent: CaptureIntent
    ) -> CaptureRunOutcome:
        if not isinstance(prepared, _ActivatedBackendMain):
            return CaptureRunOutcome(
                BackendExecution(
                    AgentOperationState.UNKNOWN, (), False, "capture_preparation_mismatch"
                )
            )
        try:
            token, prepared_source_sha256, runtime_activated = prepared.contents(
                self._prepared_main_owner
            )
        except BaseException:
            return CaptureRunOutcome(
                BackendExecution(
                    AgentOperationState.UNKNOWN,
                    (),
                    False,
                    "capture_preparation_mismatch",
                )
            )
        if token in self._consumed_main_preparations:
            return CaptureRunOutcome(
                BackendExecution(
                    AgentOperationState.UNKNOWN,
                    (),
                    False,
                    "capture_preparation_mismatch",
                )
            )
        self._consumed_main_preparations.add(token)
        if prepared_source_sha256 != intent.source_sha256:
            try:
                self._session.discard_prepared_main_for_capture(runtime_activated)
            except BaseException:
                pass
            return CaptureRunOutcome(
                BackendExecution(
                    AgentOperationState.UNKNOWN,
                    (),
                    False,
                    "capture_preparation_mismatch",
                )
            )
        try:
            attempt = self._session.execute_prepared_main_for_capture(runtime_activated)
        except BaseException:
            return CaptureRunOutcome(
                BackendExecution(AgentOperationState.UNKNOWN, (), False, "unknown")
            )
        if type(attempt) is not PreparedMainExecutionAttempt:
            return CaptureRunOutcome(
                BackendExecution(AgentOperationState.UNKNOWN, (), False, "unknown")
            )
        user_main_dispatched = attempt.user_main_dispatched
        if user_main_dispatched is not None and type(user_main_dispatched) is not bool:
            return CaptureRunOutcome(
                BackendExecution(AgentOperationState.UNKNOWN, (), False, "unknown")
            )
        try:
            reply = attempt.reply()
        except BslExecutionError as error:
            return CaptureRunOutcome(
                self._execution_from_bsl_error(
                    error,
                    runtime_state="failed",
                ),
                user_main_dispatched=user_main_dispatched,
            )
        except BaseException:
            return CaptureRunOutcome(
                BackendExecution(AgentOperationState.UNKNOWN, (), False, "unknown"),
                user_main_dispatched=user_main_dispatched,
            )
        try:
            execution = self._execution_from_reply(reply)
            if execution.terminal_state is not AgentOperationState.CAPTURED:
                return CaptureRunOutcome(
                    execution,
                    user_main_dispatched=user_main_dispatched,
                )
            location = self._session.capture_location(
                reply.location, ticket_id=reply.capture_ticket, intent=intent
            )
            if location is None or reply.stop_sequence is None:
                return CaptureRunOutcome(
                    execution,
                    user_main_dispatched=user_main_dispatched,
                )
            return CaptureRunOutcome(
                execution,
                CaptureStop(
                    reply.stop_sequence,
                    location,
                    reply.operation_id,
                    reply.capture_ticket,
                    reply.observed_command_id,
                ),
                user_main_dispatched=user_main_dispatched,
            )
        except BaseException:
            return CaptureRunOutcome(
                BackendExecution(AgentOperationState.UNKNOWN, (), False, "unknown"),
                user_main_dispatched=user_main_dispatched,
            )

    def disarm_capture(self, *, policy: str) -> None:
        self._session.disarm_capture_intent(policy=policy)

    def frame_variables(self, capture: CaptureFence, *, filters: Mapping[str, object], cursor: int, limit: int, timeout_s: float | None = None) -> Mapping[str, object]:
        if self._closed:
            raise ProtocolError("runtime backend is closed")
        if timeout_s is None:
            return self._session.frame_variables(
                capture, filters=filters, cursor=cursor, limit=limit
            )
        return self._session.frame_variables(
            capture, filters=filters, cursor=cursor, limit=limit,
            timeout_s=timeout_s,
        )

    def capture_stack(self, capture: CaptureFence, *, cursor: int, limit: int, timeout_s: float | None = None) -> Mapping[str, object]:
        if self._closed:
            raise ProtocolError("runtime backend is closed")
        return self._session.capture_stack(
            capture, cursor=cursor, limit=limit, timeout_s=timeout_s,
        )

    def capture_frame(self, capture: CaptureFence, *, level: int, cursor: int, limit: int, name: str | None = None, timeout_s: float | None = None) -> Mapping[str, object]:
        if self._closed:
            raise ProtocolError("runtime backend is closed")
        return self._session.capture_frame(
            capture, level=level, cursor=cursor, limit=limit, name=name,
            timeout_s=timeout_s,
        )

    def resolve_manager_origin(self, capture: CaptureFence, origin: ManagerOrigin, *, timeout_s: float | None = None) -> Mapping[str, object]:
        if self._closed:
            raise ProtocolError("runtime backend is closed")
        if timeout_s is None:
            return self._session.resolve_manager_origin(capture, origin)
        return self._session.resolve_manager_origin(
            capture, origin, timeout_s=timeout_s
        )

    def temporary_tables(self, capture: CaptureFence, manager_handle: str, *, names: tuple[str, ...] | None, cursor: int, limit: int, selection: ValueSelection | None, timeout_s: float | None = None) -> Mapping[str, object]:
        if self._closed:
            raise ProtocolError("runtime backend is closed")
        if timeout_s is None:
            return self._session.temporary_tables(
                capture, manager_handle, names=names, cursor=cursor,
                limit=limit, selection=selection,
            )
        return self._session.temporary_tables(
            capture, manager_handle, names=names, cursor=cursor, limit=limit,
            selection=selection, timeout_s=timeout_s,
        )

    def resume_capture(self, *, dirty_roots: tuple[str, ...] = ()) -> RuntimeReply:
        if self._closed:
            raise ProtocolError("runtime backend is closed")
        return self._session.resume_capture(dirty_roots=dirty_roots)

    def add_capture_resume_listener(self, listener: Callable[[CaptureFence], None]) -> None:
        self._session.add_capture_resume_listener(listener)

    def ownership_snapshot(self) -> tuple[dict[str, object], ...]:
        return self._session.owned_process_snapshot()

    @property
    def is_closed(self) -> bool:
        return self._closed

    def namespace_snapshot(self) -> RuntimeNamespaceSnapshot:
        if self._closed:
            raise ProtocolError("runtime backend is closed")
        return self._session.namespace_snapshot()

    def validate_value_reference(self, handle: str) -> str:
        if self._closed:
            raise ProtocolError("runtime backend is closed")
        return self._session.validate_value_reference(handle)

    def materialize_value(self, handle: str, **options: object) -> object:
        if self._closed:
            raise ProtocolError("runtime backend is closed")
        return self._session.materialize(handle, **options)

    def materialize_table(self, handle: str, **options: object) -> object:
        if self._closed:
            raise ProtocolError("runtime backend is closed")
        return self._session.to_df(handle, **options)

    def materialization_kind(self, handle: str, *, timeout_s: float | None = None) -> str:
        if self._closed:
            raise ProtocolError("runtime backend is closed")
        return self._session.materialization_kind(handle, timeout_s=timeout_s)

    def materialize_value_payload(self, handle: str, **options: object) -> bytes:
        if self._closed:
            raise ProtocolError("runtime backend is closed")
        return self._session.materialize_value_payload(handle, **options)

    def materialize_table_payload(self, handle: str, **options: object) -> bytes:
        if self._closed:
            raise ProtocolError("runtime backend is closed")
        return self._session.materialize_table_payload(handle, **options)

    def project_value_payload(
        self, handle: str, selection: ValueSelection, **options: object
    ) -> tuple[str, bytes]:
        if self._closed:
            raise ProtocolError("runtime backend is closed")
        return self._session.project_value_payload(handle, selection, **options)

    def status(self) -> RuntimeDescriptor:
        failure: RuntimeBackendStatusError | None = None
        try:
            status = self._session.status()
            return self._descriptor(status)
        except BaseException:
            failure = RuntimeBackendStatusError(f"diag_{token_urlsafe(18)}")
        raise failure

    def close(self) -> None:
        if self._closed:
            return
        self._session.close()
        self._closed = True

    def _descriptor(self, status: RuntimeStatus) -> RuntimeDescriptor:
        return RuntimeDescriptor(
            runtime_id=self.runtime_id,
            generation=status.runtime_generation,
            state=status.state.value,
            mode=self._mode,
            active_operation_id=str(status.operation_id),
        )

    @staticmethod
    def _terminal_state(reply: RuntimeReply) -> AgentOperationState:
        # A capture-cell compilation/runtime error leaves the controller at
        # CAPTURED, but it is not a successful cell.  Preserve both facts for
        # the service: FAILED expresses the cell result; runtime_state keeps
        # the proven paused controller state.
        if not reply.succeeded:
            return AgentOperationState.FAILED
        if reply.state is OperationState.COMPLETED:
            return AgentOperationState.COMPLETED
        if reply.state is OperationState.CAPTURED:
            return AgentOperationState.CAPTURED
        if reply.state in {
            OperationState.FAILED,
            OperationState.PARTIAL_WRITEBACK_FAILURE,
            OperationState.BREAKPOINT_RESTORE_FAILURE,
        }:
            return AgentOperationState.FAILED
        return AgentOperationState.UNKNOWN

    @classmethod
    def _execution_from_reply(cls, reply: RuntimeReply) -> BackendExecution:
        if reply.kind is RuntimeReplyKind.RUNTIME_UNAVAILABLE:
            return BackendExecution(
                AgentOperationState.UNKNOWN,
                (),
                False,
                reply.state.value,
            )
        state = cls._terminal_state(reply)
        messages = cls._bounded_messages(reply.messages)
        supplied_diagnostic = reply.diagnostic
        diagnostic = sanitize_normalized_diagnostic(supplied_diagnostic)
        if supplied_diagnostic is not None and diagnostic is None:
            return BackendExecution(
                AgentOperationState.UNKNOWN,
                (),
                False,
                "unknown",
            )
        deterministic_source_failure = (
            reply.kind is RuntimeReplyKind.SOURCE_FAILED
            and diagnostic is not None
            and diagnostic.stage.value in {"parsing", "lowering"}
        )
        if reply.kind is RuntimeReplyKind.SOURCE_FAILED and not deterministic_source_failure:
            return BackendExecution(
                AgentOperationState.UNKNOWN,
                (),
                False,
                "unknown",
            )
        if diagnostic is not None and not reply.messages:
            messages = (diagnostic.runtime_summary,)
        if deterministic_source_failure:
            assert diagnostic is not None
            messages = (diagnostic.runtime_summary,)
        elif state is AgentOperationState.FAILED and not messages:
            _, messages = cls._admit_public_failure(diagnostic)
        return BackendExecution(
            terminal_state=state,
            messages=messages,
            result_present=reply.result is not None,
            runtime_state=reply.state.value,
            changed_roots=reply.changed_roots,
            failure_stage=(
                diagnostic.stage.value
                if deterministic_source_failure
                else "execution"
                if state is AgentOperationState.FAILED
                else None
            ),
            capture_dirty_roots=reply.capture_dirty_roots,
            diagnostic=diagnostic,
            state_changed=(
                StateChanged.NO
                if deterministic_source_failure
                else StateChanged.UNKNOWN
            ),
        )

    @classmethod
    def _execution_from_bsl_error(
        cls,
        error: BslExecutionError,
        *,
        runtime_state: str,
    ) -> BackendExecution:
        diagnostic, messages = cls._admit_public_failure(error.diagnostic)
        return BackendExecution(
            terminal_state=AgentOperationState.FAILED,
            messages=messages,
            result_present=False,
            runtime_state=runtime_state,
            failure_stage="execution",
            diagnostic=diagnostic,
        )

    @staticmethod
    def _admit_public_failure(
        diagnostic: object,
    ) -> tuple[NormalizedDiagnostic | None, tuple[str, ...]]:
        """Admit only runtime-authored summaries, never raw platform prose."""
        safe = sanitize_normalized_diagnostic(diagnostic)
        return (
            safe,
            (
                _PUBLIC_EXECUTION_FAILURE_MESSAGE
                if safe is None
                else safe.runtime_summary,
            ),
        )

    @staticmethod
    def _bounded_messages(messages: tuple[str, ...] | object) -> tuple[str, ...]:
        if isinstance(messages, str) or not isinstance(messages, tuple):
            return ()
        return tuple(
            item[:4096] for item in messages[:100] if isinstance(item, str)
        )


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Required runtime environment variable is missing: {name}")
    return value


def _configured_extension_mode() -> ExtensionMode:
    value = os.environ.get("ONEC_RUNTIME_EXTENSION_MODE", "auto").strip()
    try:
        return ExtensionMode(value)
    except ValueError:
        raise ValueError(
            "ONEC_RUNTIME_EXTENSION_MODE must be 'auto' or 'manual'"
        ) from None


def _configured_debug_port() -> int:
    value = os.environ.get("ONEC_RUNTIME_DEBUG_PORT")
    if value is None:
        return 1550
    stripped = value.strip()
    if not stripped.isascii() or not stripped.isdecimal():
        raise ValueError(
            "ONEC_RUNTIME_DEBUG_PORT must be an integer from 1 to 65535"
        )
    port = int(stripped)
    if not 1 <= port <= 65535:
        raise ValueError(
            "ONEC_RUNTIME_DEBUG_PORT must be an integer from 1 to 65535"
        )
    return port


class OnecRuntimeFactory:
    def __init__(self, workspace: Path) -> None:
        self._workspace = Path(workspace)
        self._startup_cleanup_lock = RLock()
        self._pending_startup_cleanup: Callable[[], None] | None = None

    def start(self, *, mode: CapabilityMode) -> RuntimeBackend:
        with self._startup_cleanup_lock:
            pending_cleanup = self._pending_startup_cleanup
        if pending_cleanup is not None and not self.reconcile_unknown_startup():
            raise ProtocolError("Previous runtime startup cleanup is incomplete")
        if mode not in {CapabilityMode.OBSERVE, CapabilityMode.EXPERIMENT}:
            raise CapabilityDenied("runtime supports observe and experiment modes")
        extension_mode = _configured_extension_mode()
        platform_bin = Path(_required_environment("ONEC_RUNTIME_PLATFORM_BIN"))
        connection_string = _required_environment("ONEC_RUNTIME_CONNECTION_STRING")
        username = _required_environment("ONEC_RUNTIME_USERNAME")
        debug_host = (
            os.environ.get("ONEC_RUNTIME_DEBUG_HOST", "").strip() or "127.0.0.1"
        )
        debug_port = _configured_debug_port()
        debug_alias = os.environ.get("ONEC_RUNTIME_DEBUG_ALIAS", "").strip() or None
        project = os.environ.get("ONEC_RUNTIME_PROJECT", "").strip()
        source_root = os.environ.get("ONEC_RUNTIME_SOURCE_ROOT", "").strip()
        if bool(project) != bool(source_root):
            raise ValueError(
                "ONEC_RUNTIME_PROJECT and ONEC_RUNTIME_SOURCE_ROOT must be "
                "provided together"
            )
        capture_source = (
            CaptureSourceConfig(project, Path(source_root))
            if project and source_root
            else None
        )
        configured_evidence = os.environ.get("ONEC_RUNTIME_EVIDENCE_DIR", "").strip()
        evidence_root = (
            Path(configured_evidence)
            if configured_evidence
            else self._workspace / ".runtime" / "evidence"
        )
        runtime = RuntimeConfig(
            workspace=self._workspace,
            platform_bin=platform_bin,
            connection_string=connection_string,
            username=username,
            debug_host=debug_host,
            debug_port=debug_port,
            debug_alias=debug_alias,
        )
        try:
            core = RuntimeSession.start(
                RuntimeSessionConfig(
                    runtime,
                    evidence_root,
                    capture_source=capture_source,
                    extension_mode=extension_mode,
                )
            )
        except BaseException as error:
            retry_cleanup = getattr(error, "retry_cleanup", None)
            if callable(retry_cleanup):
                with self._startup_cleanup_lock:
                    self._pending_startup_cleanup = retry_cleanup
            raise
        try:
            session = AgentRuntimeSession(core)
            return OnecRuntimeBackend(str(uuid4()), session, mode=mode)
        except BaseException:
            core.close()
            raise

    def reconcile_unknown_startup(self, **_facts: object) -> bool:
        with self._startup_cleanup_lock:
            retry_cleanup = self._pending_startup_cleanup
            if retry_cleanup is None:
                return False
            try:
                retry_cleanup()
            except BaseException:
                return False
            if self._pending_startup_cleanup is retry_cleanup:
                self._pending_startup_cleanup = None
            return True

    def reconcile_orphaned_runtime(self, ownership: object) -> bool:
        if not isinstance(ownership, dict):
            return False
        identities = ownership.get("processes")
        if not isinstance(identities, list) or not identities:
            return False
        validated: list[psutil.Process] = []
        for identity in identities:
            if not isinstance(identity, dict):
                return False
            pid = identity.get("pid")
            create_time = identity.get("create_time")
            executable = identity.get("executable")
            if (
                type(pid) is not int
                or pid <= 0
                or isinstance(create_time, bool)
                or not isinstance(create_time, (int, float))
                or not isinstance(executable, str)
                or not executable
            ):
                return False
            try:
                process = psutil.Process(pid)
                if (
                    abs(process.create_time() - float(create_time)) > 0.001
                    or Path(process.exe()).resolve() != Path(executable).resolve()
                ):
                    return False
            except psutil.NoSuchProcess:
                continue
            except (psutil.AccessDenied, OSError, ValueError):
                return False
            validated.append(process)
        for process in validated:
            try:
                process.terminate()
            except psutil.NoSuchProcess:
                continue
            except (psutil.AccessDenied, OSError):
                return False
        _gone, alive = psutil.wait_procs(validated, timeout=10.0)
        for process in alive:
            try:
                process.kill()
            except psutil.NoSuchProcess:
                continue
            except (psutil.AccessDenied, OSError):
                return False
        if alive:
            _gone, alive = psutil.wait_procs(alive, timeout=5.0)
        return not alive
