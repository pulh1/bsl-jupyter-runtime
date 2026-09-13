"""Agent-owned capture composition over the headless runtime session."""

from __future__ import annotations

from collections.abc import Sequence

from onec_runtime_mcp.agent.capture_contracts import (
    CaptureFence,
    CapturePointRequest,
    ResolvedCapturePoint,
)
from onec_runtime_mcp.agent.capture_service import (
    CaptureArming,
    CaptureContinuationAttempt,
    CaptureIntent,
)
from onec_runtime.session import RuntimeSession


class AgentRuntimeSession:
    """Add source-aware capture ownership without coupling core to a target."""

    def __init__(self, core: RuntimeSession) -> None:
        self.core = core

    def __getattr__(self, name: str) -> object:
        return getattr(self.core, name)

    def resolve_capture_points(
        self, points: Sequence[CapturePointRequest]
    ) -> tuple[ResolvedCapturePoint, ...]:
        return self.core.resolve_capture_points(points)

    def arm_capture_intent(self, intent: CaptureIntent) -> CaptureArming:
        arming = self.core.arm_capture_intent(intent)
        return CaptureArming(
            arming.ticket_id,
            arming.expected_controller_operation_id,
            arming.expected_stop_sequence,
        )

    def prepare_capture_successor(
        self,
        intent: CaptureIntent | None,
        *,
        attempt: CaptureContinuationAttempt,
    ) -> object:
        admission = self.core.prepare_capture_successor(intent, attempt=attempt)
        arming = getattr(admission, "arming", None)

        class _AgentAdmission:
            def __init__(self) -> None:
                self.arming = (
                    None
                    if arming is None
                    else CaptureArming(
                        arming.ticket_id,
                        arming.expected_controller_operation_id,
                        arming.expected_stop_sequence,
                    )
                )

            def commit(self) -> None:
                admission.commit()

            def rollback(self) -> None:
                admission.rollback()

            def quarantine(self) -> None:
                admission.quarantine()

        return _AgentAdmission()

    def add_capture_resume_listener(
        self, listener: object
    ) -> None:
        if not callable(listener):
            raise TypeError("capture resume listener must be callable")

        def adapt(fence: object) -> None:
            listener(  # type: ignore[operator]
                CaptureFence(
                    fence.capture_intent_id,
                    fence.operation_id,
                    fence.source_revision,
                    fence.source_sha256,
                    fence.capture_generation,
                    fence.stop_sequence,
                )
            )

        self.core.add_capture_resume_listener(adapt)
