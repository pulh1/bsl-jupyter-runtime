"""RuntimeSession's prepared CAPTURE calls use the public single-owner facade."""

from contextlib import nullcontext
from threading import RLock

from onec_runtime.execution.public_facade import PublicExecutionFacade
from onec_runtime.session import RuntimeSession


def test_session_capture_hypothesis_uses_public_prepared_cell_contract() -> None:
    calls: list[tuple[object, ...]] = []
    facade = object.__new__(PublicExecutionFacade)
    prepared = object()
    provenance = object()
    reply = object()
    facade.prepare_bsl = lambda source: calls.append(("prepare", source)) or prepared
    facade.prepared_bsl_execution_provenance = (
        lambda candidate: calls.append(("provenance", candidate)) or provenance
    )
    facade.execute_prepared_bsl = (
        lambda candidate: calls.append(("execute", candidate)) or reply
    )
    facade.execution_caller_handoff = lambda release: nullcontext()

    session = object.__new__(RuntimeSession)
    session.runtime_api = facade
    session._operation_lock = RLock()
    session._closed = False
    capture = object()
    session._require_capture_fence = (
        lambda actual: calls.append(("fence", actual))
    )

    assert session.prepare_capture_hypothesis("Результат = 1;", capture) is prepared
    assert session.prepared_capture_hypothesis_provenance(prepared) is provenance
    assert session.execute_prepared_capture_hypothesis(prepared, capture) is reply
    assert calls == [
        ("fence", capture),
        ("prepare", "Результат = 1;"),
        ("provenance", prepared),
        ("fence", capture),
        ("execute", prepared),
    ]
