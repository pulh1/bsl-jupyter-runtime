from __future__ import annotations

from onec_runtime.bsl.diagnostics import NormalizedDiagnostic


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
