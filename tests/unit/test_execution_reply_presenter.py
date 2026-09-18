"""Pipeline diagnostics retain the established RuntimeReply contract."""

from onec_runtime.bsl.diagnostics import DiagnosticStage, VisibleSourceContext
from onec_runtime.bsl.lexer import BslLexError
from onec_runtime.bsl.source_maps import SourceSpan, SourceUnitKind, SourceUnitRef, mapped_visible_source, source_sha256
from onec_runtime.execution.contracts import CommonCell, SourceDiagnostic, Unavailable
from onec_runtime.execution.pipeline import CellExecutionPipeline
from onec_runtime.runtime_models import OperationState
from onec_runtime.runtime_models import RuntimeReplyKind, RuntimeStatus


def _status() -> RuntimeStatus:
    return RuntimeStatus(OperationState.CAPTURED, 7, 19, None)


def _diagnostic(stage: DiagnosticStage) -> SourceDiagnostic:
    source = "Результат = СекретнаяПеременная;"
    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL, "reply-presenter", 3, source_sha256(source),
    )
    return SourceDiagnostic(
        "unsafe internal diagnostic text",
        error=BslLexError(
            "credential=never-publish", span=SourceSpan(0, 1), code="planned_error",
        ),
        mapped_source=mapped_visible_source(source, unit),
        visible_source_context=VisibleSourceContext({unit: source}),
        stage=stage,
    )


def test_presenter_normalizes_admission_diagnostics_without_exposing_error_text() -> None:
    from onec_runtime.execution.reply_presenter import RuntimeReplyPresenter

    reply = RuntimeReplyPresenter(_status).diagnostic_reply(
        _diagnostic(DiagnosticStage.PARSING)
    )

    assert reply.kind is RuntimeReplyKind.SOURCE_FAILED
    assert reply.operation_id == 19
    assert reply.state is OperationState.CAPTURED
    assert reply.succeeded is False
    assert reply.error == "BSL parsing failed"
    assert reply.diagnostic is not None
    assert reply.diagnostic.stage is DiagnosticStage.PARSING
    assert "credential" not in reply.error
    assert "credential" not in repr(reply.diagnostic)


def test_presenter_normalizes_lowering_diagnostics_with_current_reply_context() -> None:
    from onec_runtime.execution.reply_presenter import RuntimeReplyPresenter

    reply = RuntimeReplyPresenter(_status).diagnostic_reply(
        _diagnostic(DiagnosticStage.LOWERING)
    )

    assert reply.kind is RuntimeReplyKind.SOURCE_FAILED
    assert reply.operation_id == 19
    assert reply.state is OperationState.CAPTURED
    assert reply.error == "BSL lowering failed"
    assert reply.diagnostic is not None
    assert reply.diagnostic.stage is DiagnosticStage.LOWERING


def test_presenter_returns_failed_reply_for_an_unavailable_route() -> None:
    from onec_runtime.execution.reply_presenter import RuntimeReplyPresenter

    reply = RuntimeReplyPresenter(_status).unavailable_reply(
        Unavailable("RDBG operation is still active")
    )

    assert reply.kind is RuntimeReplyKind.SOURCE_FAILED
    assert reply.operation_id == 19
    assert reply.state is OperationState.CAPTURED
    assert reply.succeeded is False
    assert reply.error == "RDBG operation is still active"
    assert reply.diagnostic is None


def test_pipeline_presents_local_diagnostics_and_route_unavailability() -> None:
    from onec_runtime.execution.reply_presenter import RuntimeReplyPresenter

    diagnostic = _diagnostic(DiagnosticStage.LOWERING)

    class DiagnosticParser:
        def prepare(self, _source, _unit):  # type: ignore[no-untyped-def]
            return diagnostic

    class UnavailableParser:
        def prepare(self, _source, source_unit):  # type: ignore[no-untyped-def]
            return CommonCell(source_unit, object(), object(), source_unit.source_sha256)

    class UnavailableController:
        def await_preparation_context(self):
            return Unavailable("No stable MAIN or CAPTURE route is available")

    presenter = RuntimeReplyPresenter(_status)
    source_unit = diagnostic.mapped_source.source_map.segments[0].origin_ref
    assert isinstance(source_unit, SourceUnitRef)

    diagnostic_reply = CellExecutionPipeline(
        DiagnosticParser(), object(), object(), presenter,
    ).execute("Результат = 1;", source_unit)
    unavailable_reply = CellExecutionPipeline(
        UnavailableParser(), UnavailableController(), object(), presenter,
    ).execute("Результат = 1;", source_unit)

    assert diagnostic_reply.kind is RuntimeReplyKind.SOURCE_FAILED
    assert diagnostic_reply.error == "BSL lowering failed"
    assert unavailable_reply.kind is RuntimeReplyKind.SOURCE_FAILED
    assert unavailable_reply.error == "No stable MAIN or CAPTURE route is available"
