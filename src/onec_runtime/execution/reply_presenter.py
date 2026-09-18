"""Present local pipeline outcomes through the established public reply type."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from onec_runtime.bsl.diagnostics import normalize_source_error
from onec_runtime.execution.contracts import SourceDiagnostic, Unavailable

if TYPE_CHECKING:
    from onec_runtime.runtime_api import RuntimeStatus


class RuntimeReplyPresenter:
    """Convert pre-dispatch pipeline outcomes without accessing RDBG.

    The status reader supplies the current public operation identity and state.
    Source errors are normalized from their mapped source; their original text
    is deliberately never copied into the public ``RuntimeReply.error`` field.
    """

    def __init__(self, status_reader: Callable[[], RuntimeStatus]) -> None:
        if not callable(status_reader):
            raise TypeError("reply presenter status reader must be callable")
        self._status_reader = status_reader

    def diagnostic_reply(self, diagnostic: SourceDiagnostic) -> object:
        """Return a failed public reply for a local parsing or lowering error."""

        if not isinstance(diagnostic, SourceDiagnostic):
            raise TypeError("pipeline diagnostic is invalid")
        status = self._status()
        normalized = self._normalize(diagnostic)
        from onec_runtime.runtime_api import RuntimeReply, RuntimeReplyKind

        return RuntimeReply(
            RuntimeReplyKind.SOURCE_FAILED,
            status.operation_id,
            status.state,
            error=(
                normalized.runtime_summary
                if normalized is not None
                else "BSL source processing failed"
            ),
            succeeded=False,
            diagnostic=normalized,
        )

    def unavailable_reply(self, unavailable: Unavailable) -> object:
        """Return a failed public reply when no stable local route is available."""

        if not isinstance(unavailable, Unavailable):
            raise TypeError("pipeline unavailable outcome is invalid")
        status = self._status()
        from onec_runtime.runtime_api import RuntimeReply, RuntimeReplyKind

        return RuntimeReply(
            RuntimeReplyKind.SOURCE_FAILED,
            status.operation_id,
            status.state,
            error=unavailable.reason,
            succeeded=False,
        )

    def _status(self) -> RuntimeStatus:
        status = self._status_reader()
        from onec_runtime.runtime_api import RuntimeStatus

        if not isinstance(status, RuntimeStatus):
            raise TypeError("reply presenter status reader returned an invalid value")
        return status

    @staticmethod
    def _normalize(diagnostic: SourceDiagnostic):
        if (
            diagnostic.error is None
            or diagnostic.mapped_source is None
            or diagnostic.visible_source_context is None
            or diagnostic.stage is None
        ):
            return None
        try:
            return normalize_source_error(
                diagnostic.error,
                diagnostic.mapped_source,
                stage=diagnostic.stage,
                visible_source_context=diagnostic.visible_source_context,
            )
        except (TypeError, ValueError):
            # A malformed local diagnostic must remain a safe failed reply.
            return None
