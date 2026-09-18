from __future__ import annotations

from typing import TYPE_CHECKING

from onec_runtime.bsl.diagnostics import NormalizedDiagnostic

if TYPE_CHECKING:
    from onec_runtime.capture_evaluation import (
        CaptureEvaluationKind,
        CaptureFailureDiagnostic,
        CapturePhase,
    )
    from onec_runtime.rdbg.models import PendingEvaluation


class RuntimeProbeError(Exception):
    """Base class for runtime probe failures."""


class ModuleUniverseAdmissionError(RuntimeProbeError):
    """A module or common-module catalog entry is not safe to admit."""


class ExtensionBundleError(RuntimeProbeError):
    """The packaged runtime extension contract is malformed or corrupt."""


class ExtensionIdentityConflict(RuntimeProbeError):
    """A named installed extension does not belong to this product."""


class ExtensionLifecycleError(RuntimeProbeError):
    """Automatic runtime extension preparation failed closed."""


class ExtensionLockTimeout(ExtensionLifecycleError):
    """Another process held the target extension lifecycle lock too long."""


class ProcessStartError(RuntimeProbeError):
    """A required local process failed to start or produce its output."""


class ExtensionNotInstalled(RuntimeProbeError):
    """The fixed product extension name is absent from the target infobase."""


class ProtocolError(RuntimeProbeError):
    """The RDBG peer returned an invalid or unsuccessful response."""


def _safe_error_text(value: object, *, default: str) -> str:
    if not isinstance(value, str):
        return default
    cleaned = "".join(character if ord(character) >= 0x20 else " " for character in value)
    cleaned = " ".join(cleaned.split())
    return cleaned[:1024] or default


class CaptureEvaluationPendingError(ProtocolError):
    """An acknowledged CAPTURE evaluation outlived its initiating waiter."""

    __slots__ = ("evaluation_id", "evaluation_kind")

    def __init__(self, evaluation_id: str, evaluation_kind: CaptureEvaluationKind) -> None:
        from onec_runtime.capture_evaluation import CaptureEvaluationKind as Kind

        if not isinstance(evaluation_id, str) or not evaluation_id:
            raise ValueError("evaluation_id is invalid")
        try:
            kind = evaluation_kind if isinstance(evaluation_kind, Kind) else Kind(evaluation_kind)
        except (TypeError, ValueError) as error:
            raise ValueError("evaluation_kind is invalid") from error
        self.evaluation_id = _safe_error_text(evaluation_id, default="<unknown>")
        self.evaluation_kind = kind
        super().__init__(
            "CAPTURE evaluation remains pending "
            f"(evaluation_id={self.evaluation_id}, kind={kind.value}); "
            "use capture.wait()"
        )


class CaptureInspectionTimeout(ProtocolError):
    """A bounded CAPTURE inspection read exhausted its local deadline."""

    __slots__ = ()

    def __init__(self, message: str = "CAPTURE inspection timed out") -> None:
        super().__init__(
            _safe_error_text(message, default="CAPTURE inspection timed out")
        )


class CaptureBusyError(ProtocolError):
    """The current capture is owned by another controller operation."""

    __slots__ = ("evaluation_id", "evaluation_kind", "phase")

    def __init__(
        self,
        evaluation_id: str | None,
        evaluation_kind: CaptureEvaluationKind | None,
        phase: CapturePhase,
    ) -> None:
        from onec_runtime.capture_evaluation import (
            CaptureEvaluationKind as Kind,
            CapturePhase as Phase,
        )

        try:
            current_phase = phase if isinstance(phase, Phase) else Phase(phase)
        except (TypeError, ValueError) as error:
            raise ValueError("capture lifecycle identity is invalid") from error
        if current_phase is Phase.RESUMING:
            if evaluation_id is not None or evaluation_kind is not None:
                raise ValueError("resuming capture cannot name an evaluation")
            self.evaluation_id = None
            self.evaluation_kind = None
            self.phase = current_phase
            super().__init__("CAPTURE is busy (phase=resuming)")
            return
        if not isinstance(evaluation_id, str) or not evaluation_id:
            raise ValueError("evaluation_id is invalid")
        try:
            kind = evaluation_kind if isinstance(evaluation_kind, Kind) else Kind(evaluation_kind)
        except (TypeError, ValueError) as error:
            raise ValueError("capture lifecycle identity is invalid") from error
        self.evaluation_id = _safe_error_text(evaluation_id, default="<unknown>")
        self.evaluation_kind = kind
        self.phase = current_phase
        super().__init__(
            "CAPTURE is busy "
            f"(evaluation_id={self.evaluation_id}, kind={kind.value}, phase={current_phase.value})"
        )


class CaptureOutcomeUnknownError(ProtocolError):
    """The remote outcome of a CAPTURE evaluation cannot be established."""

    __slots__ = ("evaluation_id", "diagnostic")

    def __init__(
        self,
        evaluation_id: str | None = None,
        diagnostic: CaptureFailureDiagnostic | str | None = None,
    ) -> None:
        from onec_runtime.capture_evaluation import CaptureFailureDiagnostic

        self.evaluation_id = (
            _safe_error_text(evaluation_id, default="<unknown>")
            if evaluation_id is not None
            else None
        )
        self.diagnostic = _coerce_diagnostic(diagnostic, default_code="outcome_unknown")
        super().__init__(_diagnostic_message("CAPTURE evaluation outcome is unknown", self.diagnostic))


class CaptureRecoveryRequiredError(ProtocolError):
    """The CAPTURE controller requires recovery before data-plane operations."""

    __slots__ = ("diagnostic",)

    def __init__(self, diagnostic: CaptureFailureDiagnostic | str | None = None) -> None:
        self.diagnostic = _coerce_diagnostic(diagnostic, default_code="recovery_required")
        super().__init__(_diagnostic_message("CAPTURE recovery is required", self.diagnostic))


class CaptureEvaluationDeliveryError(ProtocolError):
    """A confirmed local result/delivery failure with a safe diagnostic."""

    __slots__ = ("diagnostic",)

    def __init__(self, diagnostic: CaptureFailureDiagnostic | str | None = None) -> None:
        self.diagnostic = _coerce_diagnostic(diagnostic, default_code="result_delivery_failed")
        super().__init__(_diagnostic_message("CAPTURE result delivery failed", self.diagnostic))


class NoActiveCaptureError(ProtocolError):
    """No current CAPTURE fence is available."""

    __slots__ = ("runtime_state",)

    def __init__(self, runtime_state: str | None = None) -> None:
        self.runtime_state = (
            _safe_error_text(runtime_state, default="<unknown>")
            if runtime_state is not None
            else None
        )
        suffix = f" (state={self.runtime_state})" if self.runtime_state else ""
        super().__init__(f"No active CAPTURE{suffix}")


class NoCaptureEvaluationError(ProtocolError):
    """No matching retained or pending evaluation exists for the capture."""

    __slots__ = ("evaluation_id",)

    def __init__(self, evaluation_id: str | None = None) -> None:
        self.evaluation_id = (
            _safe_error_text(evaluation_id, default="<unknown>")
            if evaluation_id is not None
            else None
        )
        suffix = f" (evaluation_id={self.evaluation_id})" if self.evaluation_id else ""
        super().__init__(f"No CAPTURE evaluation is available{suffix}")


class StaleCaptureError(ProtocolError):
    """The supplied CAPTURE view no longer names the current stop."""

    __slots__ = ()

    def __init__(self, message: str = "CAPTURE view is stale") -> None:
        super().__init__(_safe_error_text(message, default="CAPTURE view is stale"))


class CaptureValueCheckError(ProtocolError):
    """A public-value check or bounded value payload was inconclusive."""


class CaptureLookupError(ProtocolError):
    """A case-insensitive exact capture lookup was missing or ambiguous."""


class CapturePathError(ProtocolError):
    """A capture request did not describe a finite safe symbolic path."""


class CaptureSourceUnavailableError(ProtocolError):
    """Source-dependent capture classification cannot be established."""


class CaptureShapeUnsupportedError(ProtocolError):
    """The value shape has no qualified bounded inspection adapter."""


class CaptureValueAccessDeniedError(ProtocolError):
    """A capture value belongs to a private runtime generation."""


def _coerce_diagnostic(
    diagnostic: CaptureFailureDiagnostic | str | None,
    *,
    default_code: str,
) -> CaptureFailureDiagnostic | None:
    from onec_runtime.capture_evaluation import CaptureFailureDiagnostic

    if diagnostic is None:
        return None
    if isinstance(diagnostic, CaptureFailureDiagnostic):
        return diagnostic
    return CaptureFailureDiagnostic(
        code=default_code,
        message=_safe_error_text(diagnostic, default="controller diagnostic unavailable"),
        recommended_action="inspect capture.status()",
    )


def _diagnostic_message(prefix: str, diagnostic: CaptureFailureDiagnostic | None) -> str:
    if diagnostic is None:
        return prefix
    return f"{prefix}: {diagnostic.code}: {diagnostic.message}"[:1024]


class RdbgDebugUiNotRegistered(ProtocolError):
    """The shared debugger no longer recognizes this session's UI identity."""


class PoisonedRuntimeError(ProtocolError):
    """An acknowledged target mutation left host/runtime state unusable."""


class WorkerPromotionOutcomeUnknown(ProtocolError):
    """The target may have swapped Worker generation without acknowledgement."""

    __slots__ = ("generation", "manifest_sha256")

    def __init__(self, generation: int, manifest_sha256: str) -> None:
        if (
            type(generation) is not int
            or generation <= 0
            or not isinstance(manifest_sha256, str)
            or len(manifest_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in manifest_sha256
            )
        ):
            raise ValueError("worker promotion outcome identity is invalid")
        self.generation = generation
        self.manifest_sha256 = manifest_sha256
        super().__init__(
            "Worker promotion outcome is unknown "
            f"(generation={generation}, manifest_sha256={manifest_sha256})"
        )


class CaptureSourceNotConfigured(ProtocolError):
    """Symbolic capture requires a matching exported configuration source."""


class ExtensionHandshakeError(ProtocolError):
    """A live extension frame did not match the packaged runtime contract."""


class ManualExtensionUnavailable(ProtocolError):
    """A user-managed runtime extension did not expose its required bootstrap ABI."""


class ManualExtensionVersionWarning(RuntimeWarning):
    """The user-managed extension version differs from the packaged release."""


class MaterializationError(ProtocolError):
    """A 1C value could not be converted into a lossless Python snapshot."""


class UnsupportedOnecType(MaterializationError):
    """The value type has no explicitly registered materialization adapter."""


class MaterializationCycleError(MaterializationError):
    """Recursive 1C containers form a cycle that cannot be snapshotted."""


class MaterializationLimitError(MaterializationError):
    """A configured materialization depth, item, or byte limit was exceeded."""


class MaterializationKeyError(MaterializationError):
    """A 1C Map key cannot be represented losslessly by a Python dict."""


class ValuePayloadError(MaterializationError):
    """A typed value payload is malformed or violates its wire contract."""


class StaleWorkerGeneration(ProtocolError):
    """The supplied worker handle no longer names the active module."""


class UnexpectedStop(RuntimeProbeError):
    """The debug target stopped outside the service breakpoint."""


class CommandTimeout(RuntimeProbeError):
    """A command did not complete by its monotonic deadline."""


class LocalVariablesResultTimeout(CommandTimeout):
    """A read-only local-variable request returned no matching result in time."""


class StopWaitIntervalElapsed(CommandTimeout):
    """A stop polling interval ended normally without a matching stop event."""


class BslExecutionError(RuntimeProbeError):
    """The kernel captured a BSL execution exception."""

    __slots__ = ("_diagnostic", "_messages")

    def __init__(
        self,
        message: str = "",
        *,
        messages: tuple[str, ...] = (),
        diagnostic: NormalizedDiagnostic | None = None,
    ) -> None:
        super().__init__(message)
        self._messages = tuple(messages)
        self._diagnostic = diagnostic

    @property
    def messages(self) -> tuple[str, ...]:
        return self._messages

    @property
    def diagnostic(self) -> NormalizedDiagnostic | None:
        return self._diagnostic


class TargetLost(RuntimeProbeError):
    """The selected 1C debug target disappeared."""


class RdbgTransportError(ProtocolError):
    """The HTTP/RDBG transport failed before a protocol outcome was proven."""


class RdbgTransportTimeout(RdbgTransportError, CommandTimeout):
    """The HTTP/RDBG transport exceeded a caller-supplied finite deadline."""


class EvaluationDispatchUnknown(RdbgTransportError):
    """An evalExpr request entered transport without a confirmed outcome."""

    __slots__ = ("pending",)

    def __init__(self, pending: PendingEvaluation) -> None:
        super().__init__("RDBG expression dispatch outcome is unknown")
        self.pending = pending


class TransportRecoveryError(RuntimeProbeError):
    """A same-process RDBG reconnect could not produce sufficient evidence."""


class RecoveryIdentityMismatch(TransportRecoveryError):
    """Recovered target or frame evidence differs from the checkpoint."""


class RuntimeGenerationLost(TransportRecoveryError):
    """The active runtime generation can no longer be controlled safely."""


class SupervisorError(RuntimeProbeError):
    """A supervised runtime generation request could not be accepted."""


class LeaseConflict(SupervisorError):
    """Another frontend owns the current generation lease."""


class LeaseExpired(SupervisorError):
    """The owner lease expired and the generation is terminating."""


class StaleLeaseEpoch(SupervisorError):
    """The supplied lease epoch is not current."""


class StaleRuntimeGeneration(SupervisorError):
    """The supplied generation is no longer active."""


class StaleProxy(ProtocolError):
    """A value proxy no longer names the generation it was fenced to."""


class ReleasedProxy(ProtocolError):
    """A value proxy handle was explicitly released."""


class ControllerUnavailable(SupervisorError):
    """The current generation cannot accept mutating requests."""


class InvalidMessageSequence(SupervisorError):
    """An IPC message was duplicated or delivered out of order."""
